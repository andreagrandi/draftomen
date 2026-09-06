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
from draftomen.config import COLOR_PAIRS, DECK_BUILDER, DeckBuilderConfig
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
    CardRating,
    NumericTarget,
    PairProfile,
    ProfileMaturity,
    RateEstimate,
    RemovalTarget,
    RoleTarget,
    SampleSummary,
    SET_PROFILE_SCHEMA_VERSION,
    SetProfile,
    SourceMetadata,
    profile_card_key,
)


PROFILE_GENERATOR_VERSION = "1"
PROFILE_GENERATION_SCHEMA_VERSION = 1

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
    if ratings is not None and (
        ratings.set_code.casefold() != normalized_set or ratings.event_format.casefold() != normalized_format
    ):
        raise ProfileGenerationError("ratings set_code and event_format must match generation inputs.")

    manifest = source_manifest
    sources = () if manifest is None else tuple(ProfileGenerationSource.from_source(source) for source in manifest.sources)
    requested_card_database = _requested_card_database(card_database, normalized_set)
    input_checksums = {} if manifest is None else {source.name: source.sha256 for source in manifest.sources if source.sha256 is not None}
    input_checksums["ratings"] = _ratings_input_checksum(ratings)
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
            ratings=ratings,
            card_database=card_database,
            set_code=normalized_set,
            config=config,
            skip_counts=skip_counts,
        )
        pair_profiles = _pair_profiles(
            ratings=ratings,
            pair_decks=pair_decks,
            config=config,
            skip_counts=skip_counts,
            structure_targets=normalized_structure_targets,
        )
        if normalized_stage == ProfileGenerationStage.MATURE:
            if config.include_role_profile:
                role_profile = _compile_roles(
                    card_database=card_database,
                    set_code=normalized_set,
                    skip_counts=skip_counts,
                )
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
    )
    profile_bytes = profile.to_bytes()
    compressed = deterministic_profile_gzip(profile_bytes)
    report = ProfileGenerationReport(
        generator_version=config.generator_version,
        statistics_version=config.statistics_version,
        profile_generation_schema_version=PROFILE_GENERATION_SCHEMA_VERSION,
        set_profile_schema_version=SET_PROFILE_SCHEMA_VERSION,
        public_dump_manifest_schema_version=(
            PUBLIC_DUMP_MANIFEST_SCHEMA_VERSION if manifest is None else manifest.schema_version
        ),
        set_code=normalized_set,
        event_format=normalized_format,
        stage=normalized_stage,
        generated_at=timestamp,
        sources=sources,
        samples=samples if maturity is not ProfileMaturity.METADATA_ONLY else SampleSummary(total=0),
        card_games=_card_game_count(ratings=ratings),
        pair_games=_pair_game_count(ratings=ratings),
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


def _pair_profiles(
    *,
    ratings: SeventeenLandsFormatData | None,
    pair_decks: Mapping[str, Sequence[_Deck]],
    config: ProfileGenerationConfig,
    skip_counts: Counter[str],
    structure_targets: Mapping[str, Any],
) -> tuple[PairProfile, ...]:
    result: list[PairProfile] = []
    for pair in COLOR_PAIRS:
        record = None if ratings is None else ratings.pair_win_rates.get(pair)
        games = 0
        wins = 0
        if record is not None:
            candidate_games = record.games
            candidate_wins = record.wins
            valid = (
                isinstance(candidate_games, int)
                and not isinstance(candidate_games, bool)
                and isinstance(candidate_wins, int)
                and not isinstance(candidate_wins, bool)
                and candidate_games >= 0
                and 0 <= candidate_wins <= candidate_games
            )
            if not valid:
                skip_counts["pair_performance_malformed"] += 1
            else:
                games = candidate_games
                wins = candidate_wins
        estimate = _rate_estimate(
            raw_value=None if games == 0 else wins / games,
            successes=wins,
            samples=games,
            prior=config.pair_prior,
            source=_PAIR_RATE_SOURCE,
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
    ratings: SeventeenLandsFormatData | None,
    card_database: CardDatabase,
    set_code: str,
    config: ProfileGenerationConfig,
    skip_counts: Counter[str],
) -> tuple[CardRating, ...]:
    if ratings is None:
        return ()
    result: list[CardRating] = []
    seen: set[str] = set()
    for grp_id, stats in sorted(ratings.card_ratings.items()):
        card = card_database.cards.get(grp_id)
        if card is None or card.unknown or card.set_code is None:
            skip_counts["card_rating_unmatched_metadata"] += 1
            continue
        if card.set_code.casefold() != set_code:
            skip_counts["card_rating_out_of_set"] += 1
            continue
        key = profile_card_key(card)
        if key in seen:
            skip_counts["duplicate_card_rating_key"] += 1
            continue
        seen.add(key)
        games = stats.sample_counts.games_in_hand
        raw = stats.gih_win_rate
        if isinstance(games, bool) or not isinstance(games, int) or games < 0:
            skip_counts["card_rating_invalid_sample_count"] += 1
            continue
        raw_value: float | None = None
        if raw is not None:
            if isinstance(raw, bool):
                skip_counts["card_rating_out_of_range"] += 1
                continue
            try:
                raw_number = float(raw)
            except (TypeError, ValueError, OverflowError):
                skip_counts["card_rating_out_of_range"] += 1
                continue
            if not math.isfinite(raw_number) or not 0.0 <= raw_number <= 1.0:
                skip_counts["card_rating_out_of_range"] += 1
                continue
            raw_value = raw_number
        if games > 0 and raw_value is None:
            skip_counts["card_rating_missing_rate"] += 1
            continue
        successes = 0 if games == 0 else int(round(raw_value * games))
        estimate = _rate_estimate(
            raw_value=raw_value,
            successes=successes,
            samples=games,
            prior=config.card_prior,
            source=_CARD_RATE_SOURCE,
        )
        result.append(
            CardRating(
                card_key=key,
                gih_win_rate=estimate,
            )
        )
    return tuple(result)


def _rate_estimate(
    *,
    raw_value: float | None,
    successes: int,
    samples: int,
    prior: BetaPrior,
    source: str,
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


def _card_game_count(*, ratings: SeventeenLandsFormatData | None) -> int:
    if ratings is None:
        return 0
    return sum(max(0, stats.sample_counts.games_in_hand) for stats in ratings.card_ratings.values())


def _pair_game_count(*, ratings: SeventeenLandsFormatData | None) -> int:
    if ratings is None:
        return 0
    return sum(max(0, rate.games) for rate in ratings.pair_win_rates.values())


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


def _ratings_input_checksum(ratings: SeventeenLandsFormatData | None) -> str:
    if ratings is None:
        payload: object = {"present": False}
    else:
        payload = {
            "present": True,
            "set_code": ratings.set_code.casefold(),
            "event_format": ratings.event_format.casefold(),
            "card_ratings": [
                {
                    "grp_id": grp_id,
                    "gih_win_rate": stats.gih_win_rate,
                    "games_in_hand": stats.sample_counts.games_in_hand,
                }
                for grp_id, stats in sorted(
                    ratings.card_ratings.items(),
                    key=lambda item: str(item[0]),
                )
            ],
            "pair_win_rates": [
                {
                    "pair": pair.casefold(),
                    "wins": rate.wins,
                    "games": rate.games,
                }
                for pair, rate in sorted(
                    ratings.pair_win_rates.items(),
                    key=lambda item: str(item[0]),
                )
            ],
        }
    return _canonical_sha256(payload)


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
    "deterministic_profile_gzip",
    "generate_profile",
    "generate_set_profile",
]
