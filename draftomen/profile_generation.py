"""Deterministic, offline generation of staged set-profile artifacts.

The generator deliberately sits between the neutral public-dump reader and the
validated runtime profile model.  It accepts already-normalized 17Lands data,
uses :func:`read_public_dump` for public rows, and emits only compact profile
objects plus privacy-safe provenance.  Stages are explicit: no amount of data
silently upgrades a metadata profile.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
import gzip
import hashlib
import io
import json
import math
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import COLOR_PAIRS, DECK_BUILDER, PICK_ENGINE, DeckBuilderConfig
from draftomen.public_dump import (
    PUBLIC_DUMP_MANIFEST_SCHEMA_VERSION,
    PublicDumpChecksumError,
    PublicDumpError,
    PublicDumpManifest,
    PublicDumpParseError,
    PublicDumpReadReport,
    PublicDumpSource,
    read_public_dump,
)
from draftomen.profile_statistics import (
    RATE_PRIOR_STRENGTH,
    STATISTICS_VERSION,
    TARGET_PRIOR_STRENGTH,
    BetaPrior,
    beta_binomial_estimate,
    shrink_mean,
)
from draftomen.seventeen import (
    CURVE_BUCKETS,
    RELIABILITY_PREMIER_FACTOR,
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsError,
    SeventeenLandsFormatData,
    build_17lands_structure_targets_from_draft_rows,
)
from draftomen.semantic_roles import (
    Role,
    RoleClassifier,
    compile_role_profile,
)
from draftomen.set_profile import (
    AggregateEvidence,
    CardRating,
    NumericTarget,
    PairProfile,
    ProfileMaturity,
    RateEstimate,
    RemovalTarget,
    RoleTarget,
    SampleSummary,
    SetProfile,
    SourceMetadata,
    profile_card_key,
)


PROFILE_GENERATOR_VERSION = "2"
PROFILE_GENERATION_SCHEMA_VERSION = 1

AGGREGATE_SUPPORT_MINIMUM = PICK_ENGINE.thin_sample_minimum
AGGREGATE_FALLBACK_CONFIDENCE_FACTOR = RELIABILITY_PREMIER_FACTOR

_CARD_RATE_SOURCE = "17lands:card-ratings"
_PAIR_RATE_SOURCE = "17lands:color-ratings"
_STRUCTURE_SOURCE = "17lands:public-draft-structure"
_ROLE_SOURCE = "17lands:public-draft-roles"
_REMOVAL_SOURCE = "17lands:public-draft-removals"


class ProfileGenerationError(ValueError):
    """Raised when generation arguments or normalized inputs are invalid."""


class ProfileGenerationStage(str, Enum):
    """An explicit generation stage with no implicit promotion."""
    METADATA = "metadata"
    METADATA_ONLY = "metadata"
    EARLY = "early"
    MATURE = "mature"
    STAGE_A = "metadata"
    STAGE_B = "early"
    STAGE_C = "mature"

    @classmethod
    def normalize(cls, value: ProfileGenerationStage | str) -> str:
        candidate = value.value if isinstance(value, cls) else value
        if not isinstance(candidate, str):
            raise ProfileGenerationError("profile generation stage must be metadata, early, or mature.")
        normalized = candidate.strip().casefold().replace("_", "-")
        aliases = {
            "metadata-only": cls.METADATA.value,
            "metadata": cls.METADATA.value,
            "early": cls.EARLY.value,
            "mature": cls.MATURE.value,
        }
        try:
            return aliases[normalized]
        except KeyError as error:
            raise ProfileGenerationError(
                f"Unsupported profile generation stage {value!r}; expected metadata, early, or mature."
            ) from error


@dataclass(frozen=True, slots=True)
class ProfileGenerationConfig:
    """Versioned knobs for deterministic generation.

    Prior objects are part of the configuration rather than hidden globals, so
    changing methodology creates a visibly different report and artifact.
    """

    generator_version: str = PROFILE_GENERATOR_VERSION
    statistics_version: int = STATISTICS_VERSION
    card_prior: BetaPrior = field(
        default_factory=lambda: BetaPrior(mean=0.55, strength=RATE_PRIOR_STRENGTH)
    )
    pair_prior: BetaPrior = field(
        default_factory=lambda: BetaPrior(mean=0.50, strength=RATE_PRIOR_STRENGTH)
    )
    target_prior_strength: float = TARGET_PRIOR_STRENGTH
    deck_builder_config: DeckBuilderConfig = DECK_BUILDER
    confidence_sample_scale: float = 1000.0
    include_role_profile: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.generator_version, str) or not self.generator_version.strip():
            raise ProfileGenerationError("generator_version must be non-empty.")
        if (
            isinstance(self.statistics_version, bool)
            or not isinstance(self.statistics_version, int)
            or self.statistics_version != STATISTICS_VERSION
        ):
            raise ProfileGenerationError(
                f"Unsupported statistics version {self.statistics_version}; expected {STATISTICS_VERSION}."
            )
        for name in ("card_prior", "pair_prior"):
            value = getattr(self, name)
            if not isinstance(value, BetaPrior):
                try:
                    value = BetaPrior(mean=float(value), strength=RATE_PRIOR_STRENGTH)
                except (TypeError, ValueError) as error:
                    raise ProfileGenerationError(f"{name} must be a BetaPrior or a finite mean.") from error
                object.__setattr__(self, name, value)
        if (
            isinstance(self.target_prior_strength, bool)
            or not isinstance(self.target_prior_strength, (int, float))
            or not math.isfinite(float(self.target_prior_strength))
        ):
            raise ProfileGenerationError("target_prior_strength must be finite.")
        if self.target_prior_strength <= 0:
            raise ProfileGenerationError("target_prior_strength must be greater than zero.")
        if not isinstance(self.deck_builder_config, DeckBuilderConfig):
            raise ProfileGenerationError("deck_builder_config must be a DeckBuilderConfig.")
        if (
            isinstance(self.confidence_sample_scale, bool)
            or not isinstance(self.confidence_sample_scale, (int, float))
            or not math.isfinite(float(self.confidence_sample_scale))
        ):
            raise ProfileGenerationError("confidence_sample_scale must be finite.")
        if self.confidence_sample_scale <= 0:
            raise ProfileGenerationError("confidence_sample_scale must be greater than zero.")
        object.__setattr__(self, "generator_version", self.generator_version.strip())


DEFAULT_PROFILE_GENERATION_CONFIG = ProfileGenerationConfig()


@dataclass(frozen=True, slots=True)
class ProfileGenerationSource:
    """Privacy-safe source descriptor retained in a generation report."""

    name: str
    sha256: str
    url: str | None = None
    retrieved_at: str | None = None
    attribution: str = ""
    license: str = ""

    @classmethod
    def from_source(cls, source: PublicDumpSource) -> ProfileGenerationSource:
        # Local paths are intentionally not copied.  The logical source name,
        # digest, and caller-provided provenance are enough to reproduce it.
        if source.sha256 is None:
            raise ProfileGenerationError(f"Source {source.name!r} is not pinned.")
        return cls(
            name=source.name,
            sha256=source.sha256,
            url=source.url,
            retrieved_at=source.retrieved_at,
            attribution=source.attribution,
            license=source.license,
        )

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"name": self.name, "sha256": self.sha256}
        for key, value in (
            ("url", self.url),
            ("retrieved_at", self.retrieved_at),
            ("attribution", self.attribution),
            ("license", self.license),
        ):
            if value:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class ProfileGenerationReport:
    """Canonical, sanitized report accompanying one generated profile."""

    generator_version: str
    statistics_version: int
    profile_generation_schema_version: int
    set_profile_schema_version: int
    public_dump_manifest_schema_version: int
    set_code: str
    event_format: str
    stage: str
    generated_at: str
    sources: tuple[ProfileGenerationSource, ...] = ()
    samples: SampleSummary = field(default_factory=lambda: SampleSummary(total=0))
    card_games: int = 0
    pair_games: int = 0
    skip_reasons: Mapping[str, int] = field(default_factory=dict)
    error_reasons: Mapping[str, int] = field(default_factory=dict)
    input_checksums: Mapping[str, str] = field(default_factory=dict)
    profile_sha256: str = ""
    profile_bytes: int = 0
    gzip_sha256: str = ""
    gzip_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.samples, SampleSummary):
            raise ProfileGenerationError("report.samples must be a SampleSummary.")
        for name in ("card_games", "pair_games", "profile_bytes", "gzip_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProfileGenerationError(f"report.{name} must be a non-negative integer.")
        object.__setattr__(self, "sources", tuple(sorted(self.sources, key=lambda item: item.name)))
        for name in ("skip_reasons", "error_reasons", "input_checksums"):
            values = getattr(self, name)
            if not isinstance(values, Mapping):
                raise ProfileGenerationError(f"report.{name} must be a mapping.")
            if any(not isinstance(key, str) or not key for key in values):
                raise ProfileGenerationError(f"report.{name} keys must be non-empty strings.")
            if name != "input_checksums" and any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in values.values()
            ):
                raise ProfileGenerationError(f"report.{name} values must be non-negative integers.")
            if name == "input_checksums" and any(not isinstance(value, str) or not value for value in values.values()):
                raise ProfileGenerationError("report.input_checksums values must be non-empty strings.")
            object.__setattr__(self, name, MappingProxyType(dict(sorted(values.items()))))
        object.__setattr__(self, "stage", ProfileGenerationStage.normalize(self.stage))

    @property
    def skips(self) -> Mapping[str, int]:
        """Compatibility alias for callers that call skipped rows ``skips``."""

        return self.skip_reasons

    @property
    def errors(self) -> Mapping[str, int]:
        return self.error_reasons

    @property
    def input_sha256s(self) -> Mapping[str, str]:
        return self.input_checksums

    @property
    def source_manifest(self) -> tuple[ProfileGenerationSource, ...]:
        return self.sources

    @property
    def sample_summary(self) -> SampleSummary:
        return self.samples

    def to_json(self) -> dict[str, object]:
        inputs = dict(self.input_checksums)
        sources = [source.to_json() for source in self.sources]
        return {
            "checksums": {
                "gzip": self.gzip_sha256,
                "inputs": inputs,
                "profile": self.profile_sha256,
            },
            "event_format": self.event_format,
            "generated_at": self.generated_at,
            "generator_version": self.generator_version,
            "gzip_bytes": self.gzip_bytes,
            "gzip_sha256": self.gzip_sha256,
            "input_checksums": inputs,
            "pair_games": self.pair_games,
            "profile_bytes": self.profile_bytes,
            "profile_generation_schema_version": self.profile_generation_schema_version,
            "profile_sha256": self.profile_sha256,
            "public_dump_manifest_schema_version": self.public_dump_manifest_schema_version,
            "samples": self.samples.to_json(),
            "card_games": self.card_games,
            "set_code": self.set_code,
            "set_profile_schema_version": self.set_profile_schema_version,
            "skip_reasons": dict(self.skip_reasons),
            "error_reasons": dict(self.error_reasons),
            "stage": self.stage,
            "statistics_version": self.statistics_version,
            "sources": sources,
            "schema_version": self.profile_generation_schema_version,
            "source_manifest": sources,
        }
    def to_bytes(self) -> bytes:
        return (json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class ProfileGenerationResult:
    """A validated runtime profile and its deterministic safe report."""

    profile: SetProfile
    report: ProfileGenerationReport

    @property
    def profile_bytes(self) -> bytes:
        return self.profile.to_bytes()

    @property
    def artifact_bytes(self) -> bytes:
        return self.profile_bytes
    @property
    def profile_gzip(self) -> bytes:
        return self.gzip_bytes

    @property
    def gzip_bytes(self) -> bytes:
        return deterministic_profile_gzip(self.profile_bytes)

    @property
    def compressed_bytes(self) -> bytes:
        return self.gzip_bytes

    def to_bytes(self) -> bytes:
        return self.profile_bytes


@dataclass(frozen=True, slots=True)
class _Deck:
    cards: tuple[CardInfo, ...]
    pair: str
    metrics: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class _CardObservation:
    grp_id: int
    raw_value: float | None
    successes: int
    samples: int


@dataclass(frozen=True, slots=True)
class _PairObservation:
    pair: str
    wins: int
    games: int

def _normalized_aggregate_datasets(
    *,
    ratings: SeventeenLandsFormatData | None,
    fallback_ratings: Sequence[SeventeenLandsFormatData],
    set_code: str,
    event_format: str,
) -> tuple[tuple[str, SeventeenLandsFormatData], ...]:
    if ratings is not None and not isinstance(ratings, SeventeenLandsFormatData):
        raise ProfileGenerationError("ratings must be a SeventeenLandsFormatData value.")
    if ratings is not None and (
        not isinstance(ratings.set_code, str)
        or not isinstance(ratings.event_format, str)
        or ratings.set_code.casefold() != set_code
        or ratings.event_format.casefold() != event_format
    ):
        raise ProfileGenerationError("ratings set_code and event_format must match generation inputs.")
    if not isinstance(fallback_ratings, Sequence) or any(
        not isinstance(candidate, SeventeenLandsFormatData)
        for candidate in fallback_ratings
    ):
        raise ProfileGenerationError("fallback ratings must contain SeventeenLandsFormatData values.")
    fallback_by_format: dict[str, SeventeenLandsFormatData] = {}
    for candidate in fallback_ratings:
        if (
            not isinstance(candidate.set_code, str)
            or candidate.set_code.casefold() != set_code
        ):
            raise ProfileGenerationError("fallback ratings must match the requested set.")
        if not isinstance(candidate.event_format, str):
            raise ProfileGenerationError("fallback ratings must be PremierDraft or TradDraft.")
        candidate_format = candidate.event_format.strip().casefold()
        if candidate_format not in {"premierdraft", "traddraft"}:
            raise ProfileGenerationError("fallback ratings must be PremierDraft or TradDraft.")
        if candidate_format in fallback_by_format or (
            ratings is not None and candidate_format == event_format
        ):
            raise ProfileGenerationError("duplicate aggregate source format.")
        fallback_by_format[candidate_format] = candidate

    aggregate_list: list[tuple[str, SeventeenLandsFormatData]] = []
    if ratings is not None:
        aggregate_list.append((event_format, ratings))
    aggregate_list.extend(
        (candidate_format, fallback_by_format[candidate_format])
        for candidate_format in ("premierdraft", "traddraft")
        if candidate_format in fallback_by_format
    )
    return tuple(aggregate_list)


def _select_supported_card_observation(
    *,
    values: Mapping[int, _CardObservation],
    group_ids: Sequence[int],
) -> _CardObservation | None:
    return next(
        (
            values[grp_id]
            for grp_id in group_ids
            if grp_id in values
            and values[grp_id].samples >= AGGREGATE_SUPPORT_MINIMUM
        ),
        None,
    )


def _select_supported_pair_observation(
    *,
    values: Mapping[str, Sequence[_PairObservation]],
    pair: str,
) -> _PairObservation | None:
    return next(
        (
            observation
            for observation in values.get(pair, ())
            if observation.games >= AGGREGATE_SUPPORT_MINIMUM
        ),
        None,
    )


def aggregate_evidence_needs_fallback(
    *,
    set_code: str,
    event_format: str,
    card_database: CardDatabase,
    ratings: SeventeenLandsFormatData | None = None,
    fallback_ratings: Sequence[SeventeenLandsFormatData] = (),
) -> bool:
    """Return whether QuickDraft aggregate targets have a supported evidence gap."""

    if not isinstance(card_database, CardDatabase):
        raise ProfileGenerationError("card_database must be a CardDatabase.")
    normalized_set = _component(set_code, "set_code")
    normalized_format = _component(event_format, "event_format")
    aggregate_datasets = _normalized_aggregate_datasets(
        ratings=ratings,
        fallback_ratings=fallback_ratings,
        set_code=normalized_set,
        event_format=normalized_format,
    )
    if normalized_format != "quickdraft":
        return False

    card_observations = tuple(
        _validated_card_observations(
            ratings=dataset,
            card_database=card_database,
            set_code=normalized_set,
            skip_counts=Counter(),
        )[0]
        for _, dataset in aggregate_datasets
    )
    canonical_groups: dict[str, list[int]] = defaultdict(list)
    for grp_id, card in card_database.cards.items():
        if card.unknown or card.set_code is None or card.set_code.casefold() != normalized_set:
            continue
        canonical_groups[profile_card_key(card)].append(grp_id)
    for group_ids in canonical_groups.values():
        sorted_group_ids = tuple(sorted(group_ids))
        if not any(
            _select_supported_card_observation(values=values, group_ids=sorted_group_ids)
            is not None
            for values in card_observations
        ):
            return True

    pair_observations = tuple(
        _validated_pair_observations(ratings=dataset, skip_counts=Counter())[0]
        for _, dataset in aggregate_datasets
    )
    return any(
        not any(
            _select_supported_pair_observation(values=values, pair=pair) is not None
            for values in pair_observations
        )
        for pair in COLOR_PAIRS
    )



def deterministic_profile_gzip(profile_bytes: bytes) -> bytes:
    """Compress profile bytes with a stable gzip header and timestamp."""

    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        stream.write(profile_bytes)
    return output.getvalue()


def generate_set_profile(
    *,
    set_code: str,
    event_format: str,
    stage: ProfileGenerationStage | str,
    card_database: CardDatabase,
    source_manifest: PublicDumpManifest | None = None,
    generated_at: datetime,
    profile_version: str = "1.0",
    ratings: SeventeenLandsFormatData | None = None,
    fallback_ratings: Sequence[SeventeenLandsFormatData] = (),
    draft_source_name: str | None = None,
    config: ProfileGenerationConfig = DEFAULT_PROFILE_GENERATION_CONFIG,
) -> ProfileGenerationResult:
    """Generate one explicitly requested metadata, early, or mature profile.

    ``source_manifest`` is only read for empirical stages.  Every source is
    represented in the report, while only a selected local draft source is
    consumed for deck targets.  The neutral reader performs all container,
    gzip, CSV, and checksum handling.
    """

    if not isinstance(card_database, CardDatabase):
        raise ProfileGenerationError("card_database must be a CardDatabase.")
    if not isinstance(config, ProfileGenerationConfig):
        raise ProfileGenerationError("config must be a ProfileGenerationConfig.")
    normalized_stage = ProfileGenerationStage.normalize(stage)
    normalized_set = _component(set_code, "set_code")
    normalized_format = _component(event_format, "event_format")
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
        raise ProfileGenerationError("generated_at must be a timezone-aware datetime.")
    timestamp = generated_at.astimezone(UTC).isoformat()
    if not isinstance(profile_version, str) or not profile_version.strip():
        raise ProfileGenerationError("profile_version must be non-empty.")
    aggregate_datasets = _normalized_aggregate_datasets(
        ratings=ratings,
        fallback_ratings=fallback_ratings,
        set_code=normalized_set,
        event_format=normalized_format,
    )
    fallback_by_format = {
        source_format: dataset
        for source_format, dataset in aggregate_datasets
        if ratings is None or dataset is not ratings
    }

    manifest = source_manifest
    sources = () if manifest is None else tuple(ProfileGenerationSource.from_source(source) for source in manifest.sources)
    requested_card_database = _requested_card_database(card_database, normalized_set)
    input_checksums = {} if manifest is None else {source.name: source.sha256 for source in manifest.sources if source.sha256 is not None}
    input_checksums["ratings"] = _ratings_input_checksum(ratings)
    for candidate_format in ("premierdraft", "traddraft"):
        candidate = fallback_by_format.get(candidate_format)
        if candidate is not None:
            input_checksums[f"fallback_ratings:{candidate_format}"] = _ratings_input_checksum(candidate)
    input_checksums["card_database"] = _card_database_input_checksum(requested_card_database)
    skip_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    rows: tuple[Mapping[str, str], ...] = ()
    selected_source: PublicDumpSource | None = None

    if normalized_stage != ProfileGenerationStage.METADATA and manifest is not None:
        selected_source = _select_draft_source(manifest=manifest, requested=draft_source_name)
        if selected_source is None:
            skip_counts["draft_source_not_selected"] += 1
        elif selected_source.path is None:
            skip_counts["remote_source_not_read"] += 1
        else:
            try:
                read_result = read_public_dump(selected_source)
            except PublicDumpParseError as error:
                # A malformed container/CSV has a privacy-safe report.  Keep
                # the rating-backed profile usable and retain only reason counts.
                read_report = error.report
                _merge_reader_report(read_report, skip_counts, error_counts)
            except PublicDumpChecksumError:
                raise
            except PublicDumpError:
                # Do not include exception text: it can contain a local path or
                # a source value.  Keep only a stable diagnostic category.
                error_counts["public_dump_error"] += 1
            else:
                rows = read_result.rows
                _merge_reader_report(read_result.report, skip_counts, error_counts)

    valid_rows, row_skips = _filter_draft_rows(
        rows=rows,
        set_code=normalized_set,
        event_format=event_format,
        card_database=card_database,
        config=config.deck_builder_config,
    )
    skip_counts.update(row_skips)
    decks = _accepted_decks(
        rows=valid_rows,
        card_database=card_database,
        set_code=normalized_set,
        config=config.deck_builder_config,
        skip_counts=skip_counts,
    )

    pair_decks: dict[str, list[_Deck]] = {pair: [] for pair in COLOR_PAIRS}
    for deck in decks:
        pair_decks[deck.pair].append(deck)
    pair_decks = {pair: sorted(values, key=_deck_sort_key) for pair, values in pair_decks.items()}
    deck_counts = {pair: len(pair_decks[pair]) for pair in COLOR_PAIRS}
    samples = SampleSummary(total=sum(deck_counts.values()), by_pair=tuple(deck_counts.items()))

    cards = ()
    pair_profiles: tuple[PairProfile, ...] = ()
    normalized_structure_targets: Mapping[str, Any] = {}
    if normalized_stage != ProfileGenerationStage.METADATA and valid_rows:
        try:
            normalized_structure = build_17lands_structure_targets_from_draft_rows(
                set_code=normalized_set,
                event_format=(
                    _text(valid_rows[0].get("event_type")) or event_format
                ),
                card_database=requested_card_database,
                rows=valid_rows,
                source_url=selected_source.url if selected_source is not None else None,
                computed_at=generated_at,
                config=config.deck_builder_config,
            )
        except (TypeError, ValueError, SeventeenLandsError):
            error_counts["structure_target_error"] += 1
        else:
            normalized_structure_targets = normalized_structure.targets
    if normalized_stage == ProfileGenerationStage.MATURE:
        if not decks:
            raise ProfileGenerationError(
                "Mature profile generation requires accepted deck evidence."
            )
        accepted_pairs = {deck.pair for deck in decks}
        if accepted_pairs.difference(normalized_structure_targets):
            raise ProfileGenerationError(
                "Mature profile generation requires Stage C targets for every "
                "accepted color pair."
            )

    role_profile = None
    if normalized_stage != ProfileGenerationStage.METADATA:
        cards = _card_ratings(
            datasets=aggregate_datasets if normalized_format == "quickdraft" else (
                ((normalized_format, ratings),) if ratings is not None else ()
            ),
            requested_format=normalized_format,
            card_database=card_database,
            set_code=normalized_set,
            config=config,
            skip_counts=skip_counts,
        )
        pair_profiles = _pair_profiles(
            datasets=aggregate_datasets if normalized_format == "quickdraft" else (
                ((normalized_format, ratings),) if ratings is not None else ()
            ),
            requested_format=normalized_format,
            pair_decks=pair_decks,
            config=config,
            skip_counts=skip_counts,
            structure_targets=normalized_structure_targets,
        )
        if config.include_role_profile:
            role_profile = _compile_roles(
                card_database=card_database,
                set_code=normalized_set,
                skip_counts=skip_counts,
            )
        if normalized_stage == ProfileGenerationStage.MATURE:
            pair_profiles = _mature_pair_profiles(
                pair_profiles=pair_profiles,
                pair_decks=pair_decks,
                card_database=card_database,
                config=config,
            )

    maturity = {
        ProfileGenerationStage.METADATA: ProfileMaturity.METADATA_ONLY,
        ProfileGenerationStage.EARLY: ProfileMaturity.EARLY,
        ProfileGenerationStage.MATURE: ProfileMaturity.MATURE,
    }[normalized_stage]
    confidence = _confidence(
        maturity=maturity,
        samples=samples,
        card_ratings=cards,
        pair_profiles=pair_profiles,
        config=config,
    )
    profile_schema_version = 1 if normalized_stage == ProfileGenerationStage.METADATA else 2
    profile = SetProfile(
        set_code=normalized_set,
        event_format=normalized_format,
        profile_version=profile_version.strip(),
        generated_at=timestamp,
        source=SourceMetadata(
            provider="draftomen-profile-generator",
            artifact="set-profile",
            revision=config.generator_version,
        ),
        maturity=maturity,
        samples=None if maturity is ProfileMaturity.METADATA_ONLY else samples,
        confidence=confidence,
        pairs=pair_profiles,
        role_profile=role_profile,
        card_ratings=cards,
        schema_version=profile_schema_version,
    )
    profile_bytes = profile.to_bytes()
    compressed = deterministic_profile_gzip(profile_bytes)
    report = ProfileGenerationReport(
        generator_version=config.generator_version,
        statistics_version=config.statistics_version,
        profile_generation_schema_version=PROFILE_GENERATION_SCHEMA_VERSION,
        set_profile_schema_version=profile.schema_version,
        public_dump_manifest_schema_version=(
            PUBLIC_DUMP_MANIFEST_SCHEMA_VERSION if manifest is None else manifest.schema_version
        ),
        set_code=normalized_set,
        event_format=normalized_format,
        stage=normalized_stage,
        generated_at=timestamp,
        sources=sources,
        samples=samples if maturity is not ProfileMaturity.METADATA_ONLY else SampleSummary(total=0),
        card_games=_card_game_count(card_ratings=cards) if maturity is not ProfileMaturity.METADATA_ONLY else 0,
        pair_games=_pair_game_count(pair_profiles=pair_profiles) if maturity is not ProfileMaturity.METADATA_ONLY else 0,
        skip_reasons=skip_counts,
        error_reasons=error_counts,
        input_checksums=input_checksums,
        profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
        profile_bytes=len(profile_bytes),
        gzip_sha256=hashlib.sha256(compressed).hexdigest(),
        gzip_bytes=len(compressed),
    )
    return ProfileGenerationResult(profile=profile, report=report)


# A shorter name is useful to callers that treat this as a build operation.
generate_profile = generate_set_profile


def _component(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileGenerationError(f"{field_name} must be non-empty.")
    normalized = value.strip().casefold()
    if "/" in normalized or "\\" in normalized:
        raise ProfileGenerationError(f"{field_name} cannot contain path separators.")
    return normalized


def _merge_reader_report(report: PublicDumpReadReport, skips: Counter[str], errors: Counter[str]) -> None:
    skips.update(report.skip_reasons)
    errors.update(report.error_reasons)


def _select_draft_source(*, manifest: PublicDumpManifest, requested: str | None) -> PublicDumpSource | None:
    if requested is not None:
        return next((source for source in manifest.sources if source.name == requested), None)
    if len(manifest.sources) == 1:
        return manifest.sources[0]
    candidates = tuple(
        source
        for source in manifest.sources
        if "draft" in source.name.casefold() or "structure" in source.name.casefold()
    )
    return candidates[0] if len(candidates) == 1 else None


def _filter_draft_rows(
    *,
    rows: Iterable[Mapping[str, str]],
    set_code: str,
    event_format: str,
    card_database: CardDatabase,
    config: DeckBuilderConfig,
)-> tuple[tuple[Mapping[str, str], ...], Counter[str]]:
    names = _card_name_index(card_database, set_code=set_code)
    skips: Counter[str] = Counter()
    result: list[Mapping[str, str]] = []
    trophy_wins = 3 if event_format.startswith("Trad") else 7
    for row in rows:
        if not isinstance(row, Mapping):
            skips["malformed_normalized_row"] += 1
            continue
        expansion = _text(row.get("expansion"))
        if expansion and expansion.casefold() != set_code:
            skips["outside_set"] += 1
            continue
        row_format = _text(row.get("event_type"))
        if row_format and row_format.casefold() != event_format.casefold():
            skips["outside_format"] += 1
            continue
        wins = _integer(row.get("event_match_wins"))
        if wins != trophy_wins:
            skips["not_trophy_deck"] += 1
            continue
        maindeck_rate = _number(row.get("pick_maindeck_rate"))
        if maindeck_rate is None:
            skips["missing_maindeck_rate"] += 1
            continue
        if maindeck_rate < config.structure_maindeck_rate_threshold:
            skips["below_maindeck_threshold"] += 1
            continue
        if not _text(row.get("draft_id")):
            skips["missing_draft_id"] += 1
            continue
        pick = _text(row.get("pick"))
        if not pick:
            skips["missing_pick"] += 1
            continue
        if _lookup_card(names, pick) is None:
            skips["unknown_card"] += 1
            continue
        result.append(row)
    return tuple(result), skips


def _accepted_decks(
    *,
    rows: Iterable[Mapping[str, str]],
    card_database: CardDatabase,
    set_code: str,
    config: DeckBuilderConfig,
    skip_counts: Counter[str],
) -> tuple[_Deck, ...]:
    names = _card_name_index(card_database, set_code=set_code)
    grouped: dict[str, list[CardInfo]] = defaultdict(list)
    for row in rows:
        draft_id = _text(row.get("draft_id"))
        card = _lookup_card(names, _text(row.get("pick")))
        if draft_id and card is not None:
            grouped[draft_id].append(card)
    result: list[_Deck] = []
    for cards in grouped.values():
        nonlands = tuple(card for card in cards if not _is_land(card))
        land_count = config.deck_size - len(nonlands)
        if not config.structure_min_land_count <= land_count <= config.structure_max_land_count:
            skip_counts["deck_inferred_lands_out_of_range"] += 1
            continue
        pair = _pair_for_cards(nonlands)
        if pair is None:
            skip_counts["deck_unresolved_two_color_pair"] += 1
            continue
        metrics = _deck_metrics(
            nonlands=nonlands,
            land_count=land_count,
            config=config,
        )
        result.append(_Deck(cards=tuple(cards), pair=pair, metrics=metrics))
    return tuple(result)


def _deck_metrics(
    *,
    nonlands: Sequence[CardInfo],
    land_count: int,
    config: DeckBuilderConfig,
) -> Mapping[str, float]:
    curve = {bucket: 0 for bucket in CURVE_BUCKETS}
    for card in nonlands:
        curve[_curve_bucket(card=card, config=config)] += 1
    return {
        "average_creature_count": float(sum(_is_creature(card) for card in nonlands)),
        "average_land_count": float(land_count),
        "average_spell_count": float(len(nonlands)),
        "average_two_drop_count": float(curve["2"]),
        "average_expensive_spell_count": float(curve["6+"]),
        **{f"curve_{bucket}": float(curve[bucket]) for bucket in CURVE_BUCKETS},
    }


def _validated_card_observations(
    *,
    ratings: SeventeenLandsFormatData,
    card_database: CardDatabase,
    set_code: str,
    skip_counts: Counter[str],
) -> tuple[Mapping[int, _CardObservation], frozenset[str]]:
    valid: dict[int, _CardObservation] = {}
    invalid_keys: set[str] = set()
    for map_grp_id, stats in sorted(ratings.card_ratings.items(), key=lambda item: str(item[0])):
        card = card_database.cards.get(map_grp_id)
        card_key = None
        if card is not None and not card.unknown and card.set_code is not None:
            if card.set_code.casefold() == set_code:
                card_key = profile_card_key(card)
        if not isinstance(stats, SeventeenCardStats):
            skip_counts["card_rating_malformed"] += 1
            if card_key is not None:
                invalid_keys.add(card_key)
            continue
        if map_grp_id != stats.grp_id:
            skip_counts["card_rating_identity_mismatch"] += 1
            if card_key is not None:
                invalid_keys.add(card_key)
            continue
        if card is None or card.unknown or card.set_code is None:
            skip_counts["card_rating_unmatched_metadata"] += 1
            continue
        if card.set_code.casefold() != set_code:
            skip_counts["card_rating_out_of_set"] += 1
            continue
        key = profile_card_key(card)
        if not isinstance(stats.sample_counts, RatingSampleCounts):
            skip_counts["card_rating_invalid_sample_count"] += 1
            invalid_keys.add(key)
            continue
        games = stats.sample_counts.games_in_hand
        if isinstance(games, bool) or not isinstance(games, int) or games < 0:
            skip_counts["card_rating_invalid_sample_count"] += 1
            invalid_keys.add(key)
            continue
        raw = stats.gih_win_rate
        raw_value: float | None = None
        if raw is not None:
            if isinstance(raw, bool):
                skip_counts["card_rating_out_of_range"] += 1
                invalid_keys.add(key)
                continue
            try:
                raw_number = float(raw)
            except (TypeError, ValueError, OverflowError):
                skip_counts["card_rating_out_of_range"] += 1
                invalid_keys.add(key)
                continue
            if not math.isfinite(raw_number) or not 0.0 <= raw_number <= 1.0:
                skip_counts["card_rating_out_of_range"] += 1
                invalid_keys.add(key)
                continue
            raw_value = raw_number if games > 0 else None
        if games > 0 and raw_value is None:
            skip_counts["card_rating_missing_rate"] += 1
            invalid_keys.add(key)
            continue
        successes = 0 if games == 0 else int(round(raw_value * games))
        valid[map_grp_id] = _CardObservation(
            grp_id=map_grp_id,
            raw_value=raw_value,
            successes=successes,
            samples=games,
        )
    return valid, frozenset(invalid_keys)


def _validated_pair_observations(
    *,
    ratings: SeventeenLandsFormatData,
    skip_counts: Counter[str],
) -> tuple[Mapping[str, tuple[_PairObservation, ...]], frozenset[str]]:
    valid: dict[str, list[_PairObservation]] = defaultdict(list)
    invalid_pairs: set[str] = set()
    for map_pair, record in sorted(ratings.pair_win_rates.items(), key=lambda item: str(item[0])):
        normalized_map_pair = map_pair.strip().upper() if isinstance(map_pair, str) else None
        normalized_record_pair = record.pair.strip().upper() if (
            isinstance(record, ColorPairWinRate) and isinstance(record.pair, str)
        ) else None
        if (
            normalized_map_pair is None
            or normalized_record_pair is None
            or normalized_map_pair != normalized_record_pair
        ):
            skip_counts["pair_performance_identity_mismatch"] += 1
            if normalized_map_pair in COLOR_PAIRS:
                invalid_pairs.add(normalized_map_pair)
            continue
        if normalized_map_pair not in COLOR_PAIRS:
            skip_counts["pair_performance_malformed"] += 1
            continue
        if not isinstance(record, ColorPairWinRate):
            skip_counts["pair_performance_malformed"] += 1
            invalid_pairs.add(normalized_map_pair)
            continue
        games = record.games
        wins = record.wins
        if (
            isinstance(games, bool)
            or not isinstance(games, int)
            or isinstance(wins, bool)
            or not isinstance(wins, int)
            or games < 0
            or wins < 0
            or wins > games
        ):
            skip_counts["pair_performance_malformed"] += 1
            invalid_pairs.add(normalized_map_pair)
            continue
        valid[normalized_map_pair].append(
            _PairObservation(pair=normalized_map_pair, wins=wins, games=games)
        )
    return (
        {pair: tuple(values) for pair, values in valid.items()},
        frozenset(invalid_pairs),
    )


def _pair_profiles(
    *,
    datasets: Sequence[tuple[str, SeventeenLandsFormatData]],
    requested_format: str,
    pair_decks: Mapping[str, Sequence[_Deck]],
    config: ProfileGenerationConfig,
    skip_counts: Counter[str],
    structure_targets: Mapping[str, Any],
) -> tuple[PairProfile, ...]:
    prepared = tuple(
        (
            source_format,
            *_validated_pair_observations(ratings=ratings, skip_counts=skip_counts),
        )
        for source_format, ratings in datasets
    )
    result: list[PairProfile] = []
    for pair in COLOR_PAIRS:
        exact_values: tuple[_PairObservation, ...] = ()
        exact_invalid = False
        for source_format, values, invalid_pairs in prepared:
            if source_format == requested_format:
                exact_values = values.get(pair, ())
                exact_invalid = pair in invalid_pairs
                break
        chosen: _PairObservation | None = None
        chosen_format = requested_format
        for source_format, values, _ in prepared:
            if source_format != requested_format and requested_format != "quickdraft":
                continue
            candidate = _select_supported_pair_observation(values=values, pair=pair)
            if candidate is not None:
                chosen = candidate
                chosen_format = source_format
                break
        if chosen is None and exact_values:
            chosen = exact_values[0]
            chosen_format = requested_format
        if chosen is None:
            games = 0
            wins = 0
            authority = None
        else:
            games = chosen.games
            wins = chosen.wins
            authority = None
            if games > 0:
                if chosen_format == requested_format:
                    confidence = min(1.0, games / config.confidence_sample_scale)
                    reason = None
                else:
                    if exact_values:
                        reason = "thin-exact-evidence"
                    elif exact_invalid:
                        reason = "invalid-exact-evidence"
                    else:
                        reason = "missing-exact-evidence"
                    confidence = (
                        AGGREGATE_FALLBACK_CONFIDENCE_FACTOR
                        * min(1.0, games / config.confidence_sample_scale)
                    )
                authority = AggregateEvidence(
                    source_format=chosen_format,
                    fallback_reason=reason,
                    confidence=confidence,
                )
        estimate = _rate_estimate(
            raw_value=None if games == 0 else wins / games,
            successes=wins,
            samples=games,
            prior=config.pair_prior,
            source=_PAIR_RATE_SOURCE,
            aggregate_evidence=authority,
        )
        structural = ()
        if pair in structure_targets:
            structural = _structural_targets(
                decks=pair_decks[pair],
                set_prior=_set_metric_priors(
                    tuple(deck for values in pair_decks.values() for deck in values)
                ),
                config=config,
            )
        result.append(PairProfile(pair=pair, performance=estimate, structural_targets=structural))
    return tuple(result)


def _mature_pair_profiles(
    *,
    pair_profiles: Sequence[PairProfile],
    pair_decks: Mapping[str, Sequence[_Deck]],
    card_database: CardDatabase,
    config: ProfileGenerationConfig,
) -> tuple[PairProfile, ...]:
    all_decks = tuple(deck for pair in COLOR_PAIRS for deck in pair_decks[pair])
    set_prior = _set_metric_priors(all_decks)
    role_assignments = _classifications(card_database=card_database)
    set_role_prior, set_removal_prior = _set_semantic_priors(decks=all_decks, assignments=role_assignments)
    result: list[PairProfile] = []
    for base in pair_profiles:
        decks = tuple(pair_decks[base.pair])
        structural = _structural_targets(decks=decks, set_prior=set_prior, config=config)
        roles = _role_targets(
            decks=decks,
            assignments=role_assignments,
            set_prior=set_role_prior,
            config=config,
        )
        removals = _removal_targets(
            decks=decks,
            assignments=role_assignments,
            set_prior=set_removal_prior,
            config=config,
        )
        result.append(
            PairProfile(
                pair=base.pair,
                performance=base.performance,
                structural_targets=structural,
                role_targets=roles,
                removal_targets=removals,
            )
        )
    return tuple(result)


def _structural_targets(
    *,
    decks: Sequence[_Deck],
    set_prior: Mapping[str, float],
    config: ProfileGenerationConfig,
) -> tuple[NumericTarget, ...]:
    if not decks:
        return ()
    sample_count = len(decks)
    targets: list[NumericTarget] = []
    for name in (
        "average_creature_count",
        "average_land_count",
        "average_spell_count",
        "average_two_drop_count",
        "average_expensive_spell_count",
        *(f"curve_{bucket}" for bucket in CURVE_BUCKETS),
    ):
        raw = sum(float(deck.metrics[name]) for deck in decks) / sample_count
        prior = float(set_prior.get(name, raw))
        targets.append(
            NumericTarget(
                name=name,
                value=shrink_mean(
                    raw_value=raw,
                    samples=sample_count,
                    prior_value=prior,
                    prior_strength=config.target_prior_strength,
                ),
                raw_value=raw,
                prior_value=prior,
                samples=sample_count,
                source=_STRUCTURE_SOURCE,
            )
        )
    return tuple(targets)


def _set_metric_priors(decks: Sequence[_Deck]) -> Mapping[str, float]:
    if not decks:
        return {}
    names = tuple(decks[0].metrics)
    return {name: sum(float(deck.metrics[name]) for deck in decks) / len(decks) for name in names}


def _compile_roles(*, card_database: CardDatabase, set_code: str, skip_counts: Counter[str]):
    classifications = []
    for card in sorted(card_database.cards.values(), key=lambda value: (value.oracle_id or "", value.grp_id)):
        if card.unknown or (card.set_code is not None and card.set_code.casefold() != set_code):
            continue
        candidate = card if card.set_code is not None else replace(card, set_code=set_code)
        try:
            result = RoleClassifier().classify(candidate)
        except (TypeError, ValueError):
            skip_counts["role_classification_failed"] += 1
            continue
        if not result.is_unknown:
            classifications.append(result)
    if not classifications:
        return None
    try:
        return compile_role_profile(set_code=set_code, results=classifications)
    except (TypeError, ValueError):
        skip_counts["role_profile_compile_failed"] += 1
        return None


def _classifications(*, card_database: CardDatabase):
    result = {}
    for card in sorted(card_database.cards.values(), key=lambda value: (value.oracle_id or "", value.grp_id)):
        if card.unknown:
            continue
        try:
            classified = RoleClassifier().classify(card)
        except (TypeError, ValueError):
            continue
        if not classified.is_unknown:
            result[profile_card_key(card)] = classified.assignments
    return result


def _set_semantic_priors(*, decks: Sequence[_Deck], assignments: Mapping[str, Sequence[Any]]):
    if not decks:
        return {}, {}
    role_totals: Counter[Role] = Counter()
    removal_totals: Counter[str] = Counter()
    for deck in decks:
        roles, removals = _deck_semantics(deck=deck, assignments=assignments)
        role_totals.update(roles)
        removal_totals.update(removals)
    count = len(decks)
    return (
        {role: value / count for role, value in role_totals.items()},
        {kind: value / count for kind, value in removal_totals.items()},
    )


def _role_targets(*, decks: Sequence[_Deck], assignments: Mapping[str, Sequence[Any]], set_prior: Mapping[Role, float], config: ProfileGenerationConfig):
    if not decks:
        return ()
    observations: dict[Role, list[float]] = defaultdict(list)
    for deck in decks:
        roles, _ = _deck_semantics(deck=deck, assignments=assignments)
        for role in set(set_prior) | set(roles):
            observations[role].append(float(roles.get(role, 0)))
    targets = []
    for role in sorted(observations, key=lambda value: value.value):
        raw = sum(observations[role]) / len(observations[role])
        prior = float(set_prior.get(role, raw))
        if raw == 0 and prior == 0:
            continue
        targets.append(
            RoleTarget(
                role=role,
                value=shrink_mean(raw_value=raw, samples=len(decks), prior_value=prior, prior_strength=config.target_prior_strength),
                raw_value=raw,
                prior_value=prior,
                samples=len(decks),
                source=_ROLE_SOURCE,
            )
        )
    return tuple(targets)


def _removal_targets(*, decks: Sequence[_Deck], assignments: Mapping[str, Sequence[Any]], set_prior: Mapping[str, float], config: ProfileGenerationConfig):
    if not decks:
        return ()
    observations: dict[str, list[float]] = defaultdict(list)
    for deck in decks:
        _, removals = _deck_semantics(deck=deck, assignments=assignments)
        for kind in set(set_prior) | set(removals):
            observations[kind].append(float(removals.get(kind, 0)))
    targets = []
    for kind in sorted(observations):
        raw = sum(observations[kind]) / len(observations[kind])
        prior = float(set_prior.get(kind, raw))
        if raw == 0 and prior == 0:
            continue
        targets.append(
            RemovalTarget(
                kind=kind,
                value=shrink_mean(raw_value=raw, samples=len(decks), prior_value=prior, prior_strength=config.target_prior_strength),
                raw_value=raw,
                prior_value=prior,
                samples=len(decks),
                source=_REMOVAL_SOURCE,
            )
        )
    return tuple(targets)


def _deck_semantics(*, deck: _Deck, assignments: Mapping[str, Sequence[Any]]):
    roles: Counter[Role] = Counter()
    removals: Counter[str] = Counter()
    for card in deck.cards:
        for assignment in assignments.get(profile_card_key(card), ()):
            roles[assignment.role] += 1
            if assignment.removal is not None:
                removals[assignment.removal.kind] += 1
    return roles, removals


def _card_ratings(
    *,
    datasets: Sequence[tuple[str, SeventeenLandsFormatData]],
    requested_format: str,
    card_database: CardDatabase,
    set_code: str,
    config: ProfileGenerationConfig,
    skip_counts: Counter[str],
) -> tuple[CardRating, ...]:
    prepared = tuple(
        (
            source_format,
            *_validated_card_observations(
                ratings=ratings,
                card_database=card_database,
                set_code=set_code,
                skip_counts=skip_counts,
            ),
        )
        for source_format, ratings in datasets
    )
    canonical_groups: dict[str, list[int]] = defaultdict(list)
    for grp_id, card in card_database.cards.items():
        if card.unknown or card.set_code is None or card.set_code.casefold() != set_code:
            continue
        canonical_groups[profile_card_key(card)].append(grp_id)

    result: list[CardRating] = []
    for key in sorted(canonical_groups):
        group_ids = tuple(sorted(canonical_groups[key]))
        exact_values: tuple[_CardObservation, ...] = ()
        exact_invalid = False
        for source_format, values, invalid_keys in prepared:
            if source_format == requested_format:
                exact_values = tuple(
                    values[grp_id] for grp_id in group_ids if grp_id in values
                )
                exact_invalid = key in invalid_keys
                break
        chosen: _CardObservation | None = None
        chosen_format = requested_format
        for source_format, values, _ in prepared:
            if source_format != requested_format and requested_format != "quickdraft":
                continue
            candidate = _select_supported_card_observation(
                values=values,
                group_ids=group_ids,
            )
            if candidate is not None:
                chosen = candidate
                chosen_format = source_format
                break
        if chosen is None and exact_values:
            chosen = exact_values[0]
            chosen_format = requested_format
        if chosen is None:
            continue

        authority = None
        if chosen.samples > 0:
            if chosen_format == requested_format:
                confidence = min(
                    1.0,
                    chosen.samples / config.confidence_sample_scale,
                )
                reason = None
            else:
                if exact_values:
                    reason = "thin-exact-evidence"
                elif exact_invalid:
                    reason = "invalid-exact-evidence"
                else:
                    reason = "missing-exact-evidence"
                confidence = AGGREGATE_FALLBACK_CONFIDENCE_FACTOR * min(
                    1.0,
                    chosen.samples / config.confidence_sample_scale,
                )
            authority = AggregateEvidence(
                source_format=chosen_format,
                fallback_reason=reason,
                confidence=confidence,
            )
        estimate = _rate_estimate(
            raw_value=chosen.raw_value,
            successes=chosen.successes,
            samples=chosen.samples,
            prior=config.card_prior,
            source=_CARD_RATE_SOURCE,
            aggregate_evidence=authority,
        )
        result.append(CardRating(card_key=key, gih_win_rate=estimate))
    return tuple(result)


def _rate_estimate(
    *,
    raw_value: float | None,
    successes: int,
    samples: int,
    prior: BetaPrior,
    source: str,
    aggregate_evidence: AggregateEvidence | None = None,
) -> RateEstimate:
    if samples == 0:
        return RateEstimate(
            raw_value=None,
            value=prior.mean,
            samples=0,
            prior_value=prior.mean,
            source=source,
        )
    value = beta_binomial_estimate(successes=successes, trials=samples, prior=prior)
    return RateEstimate(
        raw_value=raw_value,
        value=value,
        samples=samples,
        prior_value=prior.mean,
        source=source,
        aggregate_evidence=aggregate_evidence,
    )


def _confidence(*, maturity: ProfileMaturity, samples: SampleSummary, card_ratings: Sequence[CardRating], pair_profiles: Sequence[PairProfile], config: ProfileGenerationConfig) -> float:
    if maturity is ProfileMaturity.METADATA_ONLY:
        return 0.0
    evidence = max(
        samples.total,
        sum(rating.gih_win_rate.samples for rating in card_ratings),
        sum(pair.performance.samples for pair in pair_profiles if pair.performance is not None),
    )
    return min(1.0, evidence / config.confidence_sample_scale)


def _card_game_count(*, card_ratings: Sequence[CardRating]) -> int:
    return sum(rating.gih_win_rate.samples for rating in card_ratings)


def _pair_game_count(*, pair_profiles: Sequence[PairProfile]) -> int:
    return sum(
        pair.performance.samples
        for pair in pair_profiles
        if pair.performance is not None
    )


def _requested_card_database(card_database: CardDatabase, set_code: str) -> CardDatabase:
    cards = {
        grp_id: card
        for grp_id, card in card_database.cards.items()
        if not card.unknown
        and card.set_code is not None
        and card.set_code.casefold() == set_code
    }
    return CardDatabase(cards=cards)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _checksum_value(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return {"invalid_float": "nan"}
        if math.isinf(value):
            return {"invalid_float": "+inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, (list, tuple)):
        return [_checksum_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _checksum_value(item) for key, item in value.items()}
    return value


def _checksum_pair_identity(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else _checksum_value(value)


def _card_rating_checksum(grp_id: Any, stats: Any) -> Mapping[str, Any]:
    result: dict[str, Any] = {"grp_id": _checksum_value(grp_id)}
    if not isinstance(stats, SeventeenCardStats):
        result["record"] = _checksum_value(stats)
        return result
    result["record_grp_id"] = _checksum_value(stats.grp_id)
    result["gih_win_rate"] = _checksum_value(stats.gih_win_rate)
    if isinstance(stats.sample_counts, RatingSampleCounts):
        result["games_in_hand"] = _checksum_value(stats.sample_counts.games_in_hand)
    else:
        result["invalid_sample_counts"] = _checksum_value(stats.sample_counts)
    return result


def _pair_rating_checksum(pair: Any, rate: Any) -> Mapping[str, Any]:
    result: dict[str, Any] = {"pair": _checksum_pair_identity(pair)}
    if not isinstance(rate, ColorPairWinRate):
        result["record"] = _checksum_value(rate)
        return result
    result["record_pair"] = _checksum_pair_identity(rate.pair)
    result["wins"] = _checksum_value(rate.wins)
    result["games"] = _checksum_value(rate.games)
    return result


def _ratings_input_checksum(ratings: SeventeenLandsFormatData | None) -> str:
    if ratings is None:
        payload: object = {"present": False}
    else:
        payload = {
            "present": True,
            "set_code": ratings.set_code.casefold(),
            "event_format": ratings.event_format.casefold(),
            "card_ratings": [
                _card_rating_checksum(grp_id, stats)
                for grp_id, stats in sorted(
                    ratings.card_ratings.items(),
                    key=lambda item: str(item[0]),
                )
            ],
            "pair_win_rates": [
                _pair_rating_checksum(pair, rate)
                for pair, rate in sorted(
                    ratings.pair_win_rates.items(),
                    key=lambda item: str(item[0]),
                )
            ],
        }
    return _canonical_sha256(_checksum_value(payload))


def _card_database_input_checksum(card_database: CardDatabase) -> str:
    cards = []
    for grp_id, card in sorted(card_database.cards.items(), key=lambda item: str(item[0])):
        normalized = card.to_json()
        # Image URLs and provenance are not generation inputs.  Excluding them
        # also keeps this checksum tied to normalized card semantics only.
        normalized.pop("image_uri", None)
        normalized.pop("source_provenance", None)
        normalized["database_key"] = grp_id
        cards.append(normalized)
    return _canonical_sha256({"cards": cards})


def _card_name_index(
    card_database: CardDatabase,
    *,
    set_code: str | None = None,
) -> Mapping[str, CardInfo]:
    result: dict[str, CardInfo] = {}
    for card in card_database.cards.values():
        if set_code is not None and (
            card.set_code is None or card.set_code.casefold() != set_code
        ):
            continue
        for name in (card.name, *(part.strip() for part in card.name.split("//") if part.strip())):
            result.setdefault(_normalize_name(name), card)
    return result


def _lookup_card(index: Mapping[str, CardInfo], name: str | None) -> CardInfo | None:
    return None if not name else index.get(_normalize_name(name))


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())




def _pair_for_cards(cards: Sequence[CardInfo]) -> str | None:
    counts = Counter(color for card in cards for color in card.colors if color in {"W", "U", "B", "R", "G"})
    colors = sorted(counts, key=lambda color: (-counts[color], "WUBRG".index(color)))
    if len(colors) < 2:
        return None
    selected = frozenset(colors[:2])
    return next((pair for pair in COLOR_PAIRS if frozenset(pair) == selected), None)


def _is_land(card: CardInfo) -> bool:
    return any("Land" in type_line for type_line in card.types)


def _is_creature(card: CardInfo) -> bool:
    return any("Creature" in type_line for type_line in card.types)


def _curve_bucket(*, card: CardInfo, config: DeckBuilderConfig) -> str:
    mana_value = card.mana_value or 0.0
    if mana_value < config.two_drop_mana_value:
        return "0-1"
    if mana_value >= config.expensive_spell_mana_value:
        return "6+"
    return str(int(mana_value))


def _deck_sort_key(deck: _Deck) -> tuple[str, tuple[str, ...]]:
    return (deck.pair, tuple(sorted(profile_card_key(card) for card in deck.cards)))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _integer(value: Any) -> int | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    text = _text(value)
    if text is None:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = [
    "DEFAULT_PROFILE_GENERATION_CONFIG",
    "PROFILE_GENERATION_SCHEMA_VERSION",
    "PROFILE_GENERATOR_VERSION",
    "ProfileGenerationConfig",
    "ProfileGenerationError",
    "ProfileGenerationReport",
    "ProfileGenerationResult",
    "ProfileGenerationSource",
    "ProfileGenerationStage",
    "aggregate_evidence_needs_fallback",
    "deterministic_profile_gzip",
    "generate_profile",
    "generate_set_profile",
]
