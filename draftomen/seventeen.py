"""17Lands ratings fetch, cache, and fallback handling.
Keep network access isolated so CI can exercise recorded responses only.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from os import PathLike
from pathlib import Path
from statistics import median
from typing import Any, TypeAlias

from draftomen import __version__
from draftomen.carddb import (
    CardDatabase,
    CardDatabaseError,
    CardInfo,
    CardMetadataSeed,
    augment_card_database_with_mtgjson_set,
    save_card_database,
)
from draftomen.config import (
    COLOR_PAIRS,
    DECK_BUILDER,
    PICK_ENGINE,
    RATINGS_CACHE_TTL_HOURS,
    DeckBuilderConfig,
)
from draftomen.paths import app_data_dir
from draftomen.public_dump import PublicDumpError, iter_public_dump_rows

PathInput: TypeAlias = str | PathLike[str]
Clock: TypeAlias = Callable[[], datetime]
FetchJson: TypeAlias = Callable[[str, int], Any]

SEVENTEEN_LANDS_ATTRIBUTION = "Card data from 17Lands (17lands.com)"
SEVENTEEN_LANDS_BASE_URL = "https://www.17lands.com"
SEVENTEEN_LANDS_EXPANSIONS_ENDPOINT = f"{SEVENTEEN_LANDS_BASE_URL}/data/expansions"
CARD_RATINGS_ENDPOINT = f"{SEVENTEEN_LANDS_BASE_URL}/api/card_data"
COLOR_RATINGS_ENDPOINT = f"{SEVENTEEN_LANDS_BASE_URL}/color_ratings/data"
PUBLIC_DRAFT_DATA_URL_TEMPLATE = (
    "https://17lands-public.s3.amazonaws.com/analysis_data/draft_data/"
    "draft_data_public.{set_code}.{event_format}.csv.gz"
)
SEVENTEEN_LANDS_USER_AGENT = (
    f"draftomen/{__version__} "
    "(+https://github.com/andreagrandi/draftomen)"
)
QUICK_DRAFT_FORMAT = "QuickDraft"
PREMIER_DRAFT_FORMAT = "PremierDraft"
FORMAT_RATING_SOURCE = "format"
NEUTRAL_PRIOR_SOURCE = "neutral-prior"
CACHE_SCHEMA_VERSION = 2
STRUCTURE_CACHE_SCHEMA_VERSION = 1
SEVENTEEN_CACHE_DIRECTORY_NAME = "17lands"
STRUCTURE_TARGET_SOURCE = "17lands-public-draft-data"
CURVE_BUCKETS = ("0-1", "2", "3", "4", "5", "6+")
HTTP_TIMEOUT_SECONDS = 60
ALL_TIME_PERIOD = "ALL_TIME"
GRADE_STEP_STANDARD_DEVIATIONS = 0.33
GRADE_LABELS = (
    "F",
    "D-",
    "D",
    "D+",
    "C-",
    "C",
    "C+",
    "B-",
    "B",
    "B+",
    "A-",
    "A",
    "A+",
)
GRADE_CENTER_INDEX = GRADE_LABELS.index("C")
RELIABILITY_PREMIER_FACTOR = 0.65
RELIABILITY_SAMPLE_DEPTH_CAP = 20_000
RELIABILITY_LOW_MAXIMUM = 24
RELIABILITY_MEDIUM_MINIMUM = 50
RELIABILITY_HIGH_MINIMUM = 70
RELIABILITY_VERY_HIGH_MINIMUM = 85


class SeventeenLandsError(RuntimeError):
    """Base error for 17Lands load, refresh, and parse failures.
    Callers can surface this as a concise CLI diagnostic.
    """


class SeventeenLandsCacheMissingError(SeventeenLandsError):
    """Raised when a 17Lands cache has not been built yet.
    The auto-refresh path normally handles this before callers see it.
    """


class SeventeenLandsCacheOutdatedError(SeventeenLandsError):
    """Raised when a legacy cache predates the current 17Lands data API.
    Live loaders replace it automatically instead of serving incomplete data.
    """


@dataclass(frozen=True, slots=True)
class SeventeenLandsExpansionDiagnostic:
    """Describe one malformed or duplicate 17Lands expansion entry."""

    reason: str
    entry: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class SeventeenLandsExpansionInventory:
    """Normalized 17Lands expansion inventory with source provenance."""

    expansion_codes: tuple[str, ...]
    source_url: str
    source_payload_digest: str
    diagnostics: tuple[SeventeenLandsExpansionDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(code, str) for code in self.expansion_codes):
            raise TypeError("17Lands expansion codes must be strings.")
        codes = tuple(
            sorted({code.strip().upper() for code in self.expansion_codes if code.strip()})
        )
        if not isinstance(self.source_url, str) or not self.source_url.strip():
            raise ValueError("17Lands expansion source URL must be non-empty.")
        if (
            not isinstance(self.source_payload_digest, str)
            or not self.source_payload_digest.strip()
        ):
            raise ValueError("17Lands expansion source payload digest must be non-empty.")
        if any(
            not isinstance(item, SeventeenLandsExpansionDiagnostic)
            for item in self.diagnostics
        ):
            raise TypeError("17Lands expansion diagnostics must be typed diagnostics.")
        object.__setattr__(self, "expansion_codes", codes)
        object.__setattr__(
            self,
            "diagnostics",
            tuple(
                sorted(
                    self.diagnostics,
                    key=lambda item: (item.reason, item.entry or "", item.detail),
                )
            ),
        )


def parse_17lands_expansion_inventory(
    payload: Any,
    *,
    source_url: str = SEVENTEEN_LANDS_EXPANSIONS_ENDPOINT,
) -> SeventeenLandsExpansionInventory:
    """Parse a decoded ``/data/expansions`` response without network access."""

    if not isinstance(payload, list):
        raise SeventeenLandsError("Malformed 17Lands expansions response; expected a JSON list.")

    canonical_entries = tuple(
        sorted(
            json.dumps(
                entry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            for entry in payload
        )
    )
    canonical_payload = json.dumps(
        canonical_entries,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

    codes: set[str] = set()
    diagnostics: list[SeventeenLandsExpansionDiagnostic] = []
    for entry in payload:
        if not isinstance(entry, str):
            diagnostics.append(
                SeventeenLandsExpansionDiagnostic(
                    reason="malformed-entry",
                    entry=None,
                    detail=f"expected a non-empty string, got {type(entry).__name__}",
                )
            )
            continue
        code = entry.strip().upper()
        if not code:
            diagnostics.append(
                SeventeenLandsExpansionDiagnostic(
                    reason="malformed-entry",
                    entry=entry,
                    detail="expected a non-empty string",
                )
            )
            continue
        if code in codes:
            diagnostics.append(
                SeventeenLandsExpansionDiagnostic(
                    reason="duplicate-entry",
                    entry=code,
                    detail="normalized expansion code already present",
                )
            )
            continue
        codes.add(code)

    return SeventeenLandsExpansionInventory(
        expansion_codes=tuple(codes),
        source_url=source_url,
        source_payload_digest=payload_digest,
        diagnostics=tuple(diagnostics),
    )


def fetch_17lands_expansion_inventory(
    *,
    fetch_json: FetchJson | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> SeventeenLandsExpansionInventory:
    """Fetch and parse the authoritative 17Lands expansion inventory."""

    json_fetcher = _default_fetch_json if fetch_json is None else fetch_json
    payload = json_fetcher(SEVENTEEN_LANDS_EXPANSIONS_ENDPOINT, timeout_seconds)
    return parse_17lands_expansion_inventory(payload)


@dataclass(frozen=True, slots=True)
class SeventeenLandsDownloadProgress:
    """Describe completed network requests during a ratings download.
    TUI callers can render determinate progress without owning fetch details.
    """

    completed_requests: int
    total_requests: int
    message: str


DownloadProgressCallback: TypeAlias = Callable[[SeventeenLandsDownloadProgress], None]


@dataclass(frozen=True, slots=True)
class RatingSampleCounts:
    """Sample counts reported by 17Lands for one card row.
    These counts let callers distinguish strong signals from thin data.
    """

    seen: int
    picked: int
    games_played: int
    opening_hand: int
    games_in_hand: int

    def to_json(self) -> dict[str, int]:
        """Convert sample counts to Draftomen's cache shape.
        The field names stay close to the 17Lands metric names.
        """

        return {
            "seen": self.seen,
            "picked": self.picked,
            "games_played": self.games_played,
            "opening_hand": self.opening_hand,
            "games_in_hand": self.games_in_hand,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RatingSampleCounts:
        """Load sample counts from Draftomen's cache shape.
        Parsing is strict so corrupted caches fail loudly.
        """

        return cls(
            seen=_required_int(data.get("seen"), field_name="sample_counts.seen"),
            picked=_required_int(data.get("picked"), field_name="sample_counts.picked"),
            games_played=_required_int(
                data.get("games_played"),
                field_name="sample_counts.games_played",
            ),
            opening_hand=_required_int(
                data.get("opening_hand"),
                field_name="sample_counts.opening_hand",
            ),
            games_in_hand=_required_int(
                data.get("games_in_hand"),
                field_name="sample_counts.games_in_hand",
            ),
        )


@dataclass(frozen=True, slots=True)
class SeventeenCardStats:
    """One normalized card-rating row from 17Lands.
    Win rates are stored as fractions, matching the upstream JSON endpoint.
    """

    grp_id: int
    name: str
    color: str
    rarity: str
    average_last_seen_at: float | None
    gih_win_rate: float | None
    opening_hand_win_rate: float | None
    drawn_improvement_win_rate: float | None
    sample_counts: RatingSampleCounts

    def to_json(self) -> dict[str, object]:
        """Convert this rating to Draftomen's cache shape.
        Cards are keyed by grpId at the container level.
        """

        return {
            "grp_id": self.grp_id,
            "name": self.name,
            "color": self.color,
            "rarity": self.rarity,
            "average_last_seen_at": self.average_last_seen_at,
            "gih_win_rate": self.gih_win_rate,
            "opening_hand_win_rate": self.opening_hand_win_rate,
            "drawn_improvement_win_rate": self.drawn_improvement_win_rate,
            "sample_counts": self.sample_counts.to_json(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SeventeenCardStats:
        """Load one card rating from Draftomen's cache shape.
        Schema mismatches fail before a partial rating table is used.
        """

        sample_counts_value = data.get("sample_counts")
        if not isinstance(sample_counts_value, dict):
            raise SeventeenLandsError(
                "17Lands card rating is missing sample_counts object."
            )

        return cls(
            grp_id=_required_int(data.get("grp_id"), field_name="card.grp_id"),
            name=_required_str(data.get("name"), field_name="card.name"),
            color=_required_str(data.get("color"), field_name="card.color"),
            rarity=_required_str(data.get("rarity"), field_name="card.rarity"),
            average_last_seen_at=_optional_float(
                data.get("average_last_seen_at"),
                field_name="card.average_last_seen_at",
            ),
            gih_win_rate=_optional_float(
                data.get("gih_win_rate"),
                field_name="card.gih_win_rate",
            ),
            opening_hand_win_rate=_optional_float(
                data.get("opening_hand_win_rate"),
                field_name="card.opening_hand_win_rate",
            ),
            drawn_improvement_win_rate=_optional_float(
                data.get("drawn_improvement_win_rate"),
                field_name="card.drawn_improvement_win_rate",
            ),
            sample_counts=RatingSampleCounts.from_json(data=sample_counts_value),
        )


@dataclass(frozen=True, slots=True)
class ColorPairWinRate:
    """Aggregate game record for one two-color pair.
    The win_rate field is None when 17Lands reports zero games.
    """

    pair: str
    wins: int
    games: int
    win_rate: float | None

    def to_json(self) -> dict[str, object]:
        """Convert this pair record to Draftomen's cache shape.
        Pair records are keyed by pair at the container level.
        """

        return {
            "pair": self.pair,
            "wins": self.wins,
            "games": self.games,
            "win_rate": self.win_rate,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ColorPairWinRate:
        """Load one pair record from Draftomen's cache shape.
        Pair codes are validated against the configured color-pair list.
        """

        pair = _required_pair(data.get("pair"), field_name="pair.pair")
        wins = _required_int(data.get("wins"), field_name="pair.wins")
        games = _required_int(data.get("games"), field_name="pair.games")
        win_rate = _optional_float(data.get("win_rate"), field_name="pair.win_rate")
        _ensure_non_negative(value=wins, field_name="pair.wins")
        _ensure_non_negative(value=games, field_name="pair.games")
        return cls(pair=pair, wins=wins, games=games, win_rate=win_rate)


@dataclass(frozen=True, slots=True)
class StructuralTargets:
    """Empirical deck-structure targets for one set and color pair.
    They are derived from 17Lands public draft dumps, not scraped pages.
    """

    set_code: str
    event_format: str
    pair: str
    sample_size: int
    average_creature_count: float
    average_land_count: float
    average_spell_count: float
    average_two_drop_count: float
    average_expensive_spell_count: float
    average_curve: tuple[tuple[str, float], ...]
    source: str
    source_url: str | None
    computed_at: datetime

    def to_json(self) -> dict[str, object]:
        """Convert structural targets to Draftomen's cache shape.
        Curve buckets are sorted in the configured display order.
        """

        return {
            "set_code": self.set_code,
            "event_format": self.event_format,
            "pair": self.pair,
            "sample_size": self.sample_size,
            "average_creature_count": self.average_creature_count,
            "average_land_count": self.average_land_count,
            "average_spell_count": self.average_spell_count,
            "average_two_drop_count": self.average_two_drop_count,
            "average_expensive_spell_count": self.average_expensive_spell_count,
            "average_curve": dict(self.average_curve),
            "source": self.source,
            "source_url": self.source_url,
            "computed_at": self.computed_at.astimezone(UTC).isoformat(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> StructuralTargets:
        """Load structural targets from the cache shape.
        Parsing is strict so bad target caches never affect deck building.
        """

        curve_value = data.get("average_curve")
        if not isinstance(curve_value, dict):
            raise SeventeenLandsError("17Lands structure target is missing average_curve.")

        pair = _required_pair(data.get("pair"), field_name="structure.pair")
        average_curve = tuple(
            (
                bucket,
                _required_float(
                    curve_value.get(bucket),
                    field_name=f"structure.average_curve.{bucket}",
                ),
            )
            for bucket in CURVE_BUCKETS
        )
        sample_size = _required_int(
            data.get("sample_size"),
            field_name="structure.sample_size",
        )
        _ensure_non_negative(value=sample_size, field_name="structure.sample_size")
        return cls(
            set_code=_required_str(data.get("set_code"), field_name="structure.set_code"),
            event_format=_required_str(
                data.get("event_format"),
                field_name="structure.event_format",
            ),
            pair=pair,
            sample_size=sample_size,
            average_creature_count=_required_float(
                data.get("average_creature_count"),
                field_name="structure.average_creature_count",
            ),
            average_land_count=_required_float(
                data.get("average_land_count"),
                field_name="structure.average_land_count",
            ),
            average_spell_count=_required_float(
                data.get("average_spell_count"),
                field_name="structure.average_spell_count",
            ),
            average_two_drop_count=_required_float(
                data.get("average_two_drop_count"),
                field_name="structure.average_two_drop_count",
            ),
            average_expensive_spell_count=_required_float(
                data.get("average_expensive_spell_count"),
                field_name="structure.average_expensive_spell_count",
            ),
            average_curve=average_curve,
            source=_required_str(data.get("source"), field_name="structure.source"),
            source_url=_optional_str(
                data.get("source_url"),
                field_name="structure.source_url",
            ),
            computed_at=_required_datetime(
                data.get("computed_at"),
                field_name="structure.computed_at",
            ),
        )


@dataclass(frozen=True, slots=True)
class SeventeenLandsStructureTargets:
    """Cached empirical structural targets for one set and format.
    The target table is keyed by canonical two-color pair.
    """

    set_code: str
    event_format: str
    computed_at: datetime
    source: str
    source_url: str | None
    total_decks: int
    targets: dict[str, StructuralTargets]

    def to_json(self) -> dict[str, object]:
        """Convert this structure-target cache to stable JSON.
        Pair keys stay sorted for inspectable cache files.
        """

        return {
            "schema_version": STRUCTURE_CACHE_SCHEMA_VERSION,
            "source": self.source,
            "source_url": self.source_url,
            "set_code": self.set_code,
            "event_format": self.event_format,
            "computed_at": self.computed_at.astimezone(UTC).isoformat(),
            "total_decks": self.total_decks,
            "targets": {
                pair: target.to_json()
                for pair, target in sorted(self.targets.items())
            },
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SeventeenLandsStructureTargets:
        """Load one cached structure-target table.
        Pair keys and embedded pair values must agree.
        """

        schema_version = _required_int(
            data.get("schema_version"),
            field_name="schema_version",
        )
        if schema_version != STRUCTURE_CACHE_SCHEMA_VERSION:
            raise SeventeenLandsError(
                "Unsupported 17Lands structure cache schema "
                f"{schema_version}; expected {STRUCTURE_CACHE_SCHEMA_VERSION}."
            )

        targets_value = data.get("targets")
        if not isinstance(targets_value, dict):
            raise SeventeenLandsError("17Lands structure cache is missing targets object.")

        targets: dict[str, StructuralTargets] = {}
        for key, value in targets_value.items():
            pair = _required_pair(key, field_name="structure targets key")
            if not isinstance(value, dict):
                raise SeventeenLandsError(
                    f"17Lands structure target {key!r} is not an object."
                )

            target = StructuralTargets.from_json(data=value)
            if target.pair != pair:
                raise SeventeenLandsError(
                    f"17Lands structure key {pair} does not match entry pair "
                    f"{target.pair}."
                )

            targets[pair] = target

        total_decks = _required_int(data.get("total_decks"), field_name="total_decks")
        _ensure_non_negative(value=total_decks, field_name="total_decks")
        return cls(
            set_code=_required_str(data.get("set_code"), field_name="set_code"),
            event_format=_required_str(data.get("event_format"), field_name="event_format"),
            computed_at=_required_datetime(data.get("computed_at"), field_name="computed_at"),
            source=_required_str(data.get("source"), field_name="source"),
            source_url=_optional_str(data.get("source_url"), field_name="source_url"),
            total_decks=total_decks,
            targets=targets,
        )


@dataclass(frozen=True, slots=True)
class _DeckStructureMetrics:
    """Per-deck structure facts before pair-level averaging.
    Keeping this private avoids exposing unfinished analysis details.
    """

    pair: str
    creature_count: int
    land_count: int
    spell_count: int
    two_drop_count: int
    expensive_spell_count: int
    curve: dict[str, int]


@dataclass(frozen=True, slots=True)
class SeventeenLandsFormatData:
    """Cached 17Lands data for one set and event format.
    It contains both card ratings and two-color pair win rates.
    """

    set_code: str
    event_format: str
    fetched_at: datetime
    card_ratings: dict[int, SeventeenCardStats]
    pair_win_rates: dict[str, ColorPairWinRate]

    @property
    def attribution(self) -> str:
        """Return the required 17Lands attribution string.
        UI and build-sheet callers can display this verbatim.
        """

        return SEVENTEEN_LANDS_ATTRIBUTION

    def letter_grade_for(self, *, grp_id: int) -> str | None:
        """Return the 17Lands-style GIH grade for one card.
        Grades use the selected format's GIH distribution.
        """

        stats = self.card_ratings.get(grp_id)
        if stats is None:
            return None

        return letter_grade_for_metric(
            value=stats.gih_win_rate,
            distribution=_gih_win_rate_distribution(stats=self.card_ratings.values()),
        )

    def to_json(self) -> dict[str, object]:
        """Convert this dataset to Draftomen's cache shape.
        Keys are sorted so cache files remain stable and inspectable.
        """

        return {
            "schema_version": CACHE_SCHEMA_VERSION,
            "source": "17lands",
            "set_code": self.set_code,
            "event_format": self.event_format,
            "fetched_at": self.fetched_at.astimezone(UTC).isoformat(),
            "card_ratings": {
                str(grp_id): card.to_json()
                for grp_id, card in sorted(self.card_ratings.items())
            },
            "pair_win_rates": {
                pair: win_rate.to_json()
                for pair, win_rate in sorted(self.pair_win_rates.items())
            },
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SeventeenLandsFormatData:
        """Load a cached 17Lands dataset from Draftomen's JSON shape.
        Schema mismatches and corrupted entries fail loudly.
        """

        schema_version = _required_int(
            data.get("schema_version"),
            field_name="schema_version",
        )
        if schema_version < CACHE_SCHEMA_VERSION:
            raise SeventeenLandsCacheOutdatedError(
                "Outdated 17Lands cache schema "
                f"{schema_version}; current schema is {CACHE_SCHEMA_VERSION}."
            )

        if schema_version != CACHE_SCHEMA_VERSION:
            raise SeventeenLandsError(
                "Unsupported 17Lands cache schema "
                f"{schema_version}; expected {CACHE_SCHEMA_VERSION}."
            )

        card_ratings_value = data.get("card_ratings")
        if not isinstance(card_ratings_value, dict):
            raise SeventeenLandsError("17Lands cache is missing card_ratings object.")

        pair_win_rates_value = data.get("pair_win_rates")
        if not isinstance(pair_win_rates_value, dict):
            raise SeventeenLandsError("17Lands cache is missing pair_win_rates object.")

        card_ratings: dict[int, SeventeenCardStats] = {}
        for key, value in card_ratings_value.items():
            grp_id = _required_int(key, field_name="card_ratings key")
            if not isinstance(value, dict):
                raise SeventeenLandsError(
                    f"17Lands card rating {key!r} is not an object."
                )

            card = SeventeenCardStats.from_json(data=value)
            if card.grp_id != grp_id:
                raise SeventeenLandsError(
                    f"17Lands card key {grp_id} does not match entry "
                    f"grp_id {card.grp_id}."
                )

            card_ratings[grp_id] = card

        pair_win_rates: dict[str, ColorPairWinRate] = {}
        for key, value in pair_win_rates_value.items():
            pair = _required_pair(key, field_name="pair_win_rates key")
            if not isinstance(value, dict):
                raise SeventeenLandsError(
                    f"17Lands pair win rate {key!r} is not an object."
                )

            win_rate = ColorPairWinRate.from_json(data=value)
            if win_rate.pair != pair:
                raise SeventeenLandsError(
                    f"17Lands pair key {pair} does not match entry pair "
                    f"{win_rate.pair}."
                )

            pair_win_rates[pair] = win_rate

        return cls(
            set_code=_required_str(data.get("set_code"), field_name="set_code"),
            event_format=_required_str(
                data.get("event_format"),
                field_name="event_format",
            ),
            fetched_at=_required_datetime(data.get("fetched_at"), field_name="fetched_at"),
            card_ratings=card_ratings,
            pair_win_rates=pair_win_rates,
        )


@dataclass(frozen=True, slots=True)
class RatingSourceMetadata:
    """Explain where a resolved card rating came from.
    Fallback metadata is explicit so UI callers can flag Premier or neutral data.
    """

    requested_format: str
    source: str
    source_format: str | None
    fallback_reason: str | None


@dataclass(frozen=True, slots=True)
class ResolvedCardRating:
    """A card rating after applying the fallback chain.
    Neutral-prior rows keep any useful ALSA-like context from cached data.
    """

    grp_id: int
    name: str
    color: str | None
    rarity: str | None
    average_last_seen_at: float | None
    gih_win_rate: float | None
    opening_hand_win_rate: float | None
    drawn_improvement_win_rate: float | None
    sample_counts: RatingSampleCounts
    letter_grade: str | None
    neutral_prior_score: float | None
    metadata: RatingSourceMetadata

    @property
    def neutral_prior(self) -> bool:
        """Return whether this rating is the neutral-prior fallback.
        Callers can use this to downplay unavailable 17Lands signals.
        """

        return self.metadata.source == NEUTRAL_PRIOR_SOURCE


@dataclass(frozen=True, slots=True)
class SetDataReliability:
    """One aggregate 17Lands reliability value for a set before drafting.
    It is presentation metadata and is never an input to card scoring.
    """

    set_code: str
    score: int
    tier: str


@dataclass(frozen=True, slots=True)
class SeventeenLandsData:
    """High-level 17Lands view with Quick→Premier→neutral fallback.
    Pair win rates come from the requested primary event format.
    """

    set_code: str
    requested_format: str
    primary: SeventeenLandsFormatData
    fallback: SeventeenLandsFormatData | None
    structure_targets: dict[str, StructuralTargets] = field(default_factory=dict)
    pair_card_ratings: dict[str, SeventeenLandsFormatData] = field(default_factory=dict)
    pair_card_ratings_loader: Callable[[str], SeventeenLandsFormatData | None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    missing_pair_card_ratings: set[str] = field(
        default_factory=set,
        repr=False,
        compare=False,
    )
    thin_sample_minimum: int = PICK_ENGINE.thin_sample_minimum
    attribution: str = SEVENTEEN_LANDS_ATTRIBUTION

    @property
    def pair_win_rates(self) -> dict[str, ColorPairWinRate]:
        """Return pair win rates for the requested primary format.
        The returned dictionary is the cached primary pair table.
        """

        return self.primary.pair_win_rates

    @property
    def set_reliability(self) -> SetDataReliability:
        """Return one aggregate reliability value for this set's data.
        The calculation is independent from rating resolution and ranking.
        """

        return _set_data_reliability(data=self)

    @property
    def ratings(self) -> dict[int, ResolvedCardRating]:
        """Return resolved ratings for cards seen in cached format data.
        Arbitrary absent grpIds can still be resolved through rating_for.
        """

        grp_ids = set(self.primary.card_ratings)
        if self.fallback is not None:
            grp_ids.update(self.fallback.card_ratings)

        return {
            grp_id: self.rating_for(grp_id=grp_id)
            for grp_id in sorted(grp_ids)
        }

    def rating_for(self, *, grp_id: int) -> ResolvedCardRating:
        """Resolve one grpId through Quick, Premier, then neutral prior.
        Missing and thin primary data are flagged in the returned metadata.
        """

        primary_stats = self.primary.card_ratings.get(grp_id)
        if _has_strong_gih_signal(
            stats=primary_stats,
            thin_sample_minimum=self.thin_sample_minimum,
        ):
            return _resolved_from_stats(
                stats=primary_stats,
                format_data=self.primary,
                requested_format=self.requested_format,
                source_format=self.primary.event_format,
                fallback_reason=None,
            )

        fallback_reason = "primary-missing" if primary_stats is None else "primary-thin"
        fallback_stats = None
        if self.fallback is not None:
            fallback_stats = self.fallback.card_ratings.get(grp_id)

        if _has_strong_gih_signal(
            stats=fallback_stats,
            thin_sample_minimum=self.thin_sample_minimum,
        ):
            return _resolved_from_stats(
                stats=fallback_stats,
                format_data=self.fallback,
                requested_format=self.requested_format,
                source_format=self.fallback.event_format if self.fallback is not None else None,
                fallback_reason=fallback_reason,
            )

        neutral_stats = primary_stats or fallback_stats
        if neutral_stats is None:
            neutral_reason = "fallback-missing"
        elif fallback_stats is None and primary_stats is not None:
            neutral_reason = "fallback-missing"
        else:
            neutral_reason = "fallback-thin"

        return _neutral_rating(
            grp_id=grp_id,
            stats=neutral_stats,
            requested_format=self.requested_format,
            fallback_reason=neutral_reason,
        )

    def structure_targets_for(self, *, pair: str) -> StructuralTargets | None:
        """Return empirical build targets for a pair when cached.
        Deck building falls back to consensus defaults when this is None.
        """

        return self.structure_targets.get(pair)

    def pair_rating_for(
        self,
        *,
        grp_id: int,
        pair: str,
        allow_thin: bool = False,
    ) -> ResolvedCardRating:
        """Resolve a card through locked-pair data when GIH evidence exists.
        Missing pair rows fall back to all-decks resolution. Thin pair rows
        are available to callers that will apply their own shrinkage.
        """

        pair_data = self._pair_card_data(pair=pair)
        pair_stats = None if pair_data is None else pair_data.card_ratings.get(grp_id)
        has_pair_signal = (
            _has_strong_gih_signal(
                stats=pair_stats,
                thin_sample_minimum=self.thin_sample_minimum,
            )
            or (
                allow_thin
                and pair_stats is not None
                and pair_stats.gih_win_rate is not None
            )
        )
        if has_pair_signal:
            return _resolved_from_stats(
                stats=pair_stats,
                format_data=pair_data,
                requested_format=self.requested_format,
                source_format=pair_data.event_format if pair_data is not None else None,
                fallback_reason=None,
            )

        return self.rating_for(grp_id=grp_id)

    def _pair_card_data(self, *, pair: str) -> SeventeenLandsFormatData | None:
        pair_data = self.pair_card_ratings.get(pair)
        if pair_data is not None:
            return pair_data

        if pair in self.missing_pair_card_ratings or self.pair_card_ratings_loader is None:
            return None

        try:
            pair_data = self.pair_card_ratings_loader(pair)
        except SeventeenLandsError:
            self.missing_pair_card_ratings.add(pair)
            return None

        if pair_data is None:
            self.missing_pair_card_ratings.add(pair)
            return None

        self.pair_card_ratings[pair] = pair_data
        return pair_data


def _augment_loaded_ratings(
    *,
    database: CardDatabase,
    set_code: str,
    ratings_data: SeventeenLandsData,
    app_dir: PathInput | None,
    persist_database: bool,
) -> SeventeenLandsData:
    augment_card_database_from_ratings(
        database=database,
        set_code=set_code,
        ratings_data=ratings_data,
        app_dir=app_dir,
        persist_database=persist_database,
    )
    return ratings_data


def metadata_augmenting_ratings_loader(
    *,
    database: CardDatabase,
    load_ratings: Callable[[str], SeventeenLandsData],
    app_dir: PathInput | None = None,
    persist_database: bool = True,
) -> Callable[[str], SeventeenLandsData]:
    """Wrap ratings loading with the shared current-set metadata recovery.
    Terminal and desktop frontends must use this same database lifecycle.
    """

    def load_and_augment(set_code: str) -> SeventeenLandsData:
        return _augment_loaded_ratings(
            database=database,
            set_code=set_code,
            ratings_data=load_ratings(set_code),
            app_dir=app_dir,
            persist_database=persist_database,
        )

    return load_and_augment


def metadata_augmenting_ratings_progress_loader(
    *,
    database: CardDatabase,
    load_ratings: Callable[
        [str, DownloadProgressCallback, bool],
        SeventeenLandsData,
    ],
    app_dir: PathInput | None = None,
    persist_database: bool = True,
) -> Callable[[str, DownloadProgressCallback, bool], SeventeenLandsData]:
    """Wrap progress-aware ratings loading with current-set metadata recovery.
    The returned loader preserves the frontend-neutral progress contract.
    """

    def load_and_augment(
        set_code: str,
        progress_callback: DownloadProgressCallback,
        *,
        refresh: bool,
    ) -> SeventeenLandsData:
        return _augment_loaded_ratings(
            database=database,
            set_code=set_code,
            ratings_data=load_ratings(
                set_code,
                progress_callback,
                refresh=refresh,
            ),
            app_dir=app_dir,
            persist_database=persist_database,
        )

    return load_and_augment


def augment_card_database_from_ratings(
    *,
    database: CardDatabase,
    set_code: str,
    ratings_data: SeventeenLandsData,
    app_dir: PathInput | None = None,
    persist_database: bool = True,
) -> None:
    """Recover current Arena grpIds from ratings and MTGJSON metadata.
    Successful recovery updates the shared in-memory database and cache.
    """

    seeds = _metadata_seeds_from_ratings(ratings=ratings_data.ratings.values())
    if not seeds:
        return

    try:
        augmented = augment_card_database_with_mtgjson_set(
            database,
            set_code=set_code,
            seeds=seeds,
        )
    except CardDatabaseError:
        return
    if augmented is database:
        return

    database.cards.clear()
    database.cards.update(augmented.cards)
    if not persist_database:
        return

    try:
        save_card_database(database, app_dir=app_dir)
    except OSError:
        return


def _metadata_seeds_from_ratings(
    *,
    ratings: Iterable[ResolvedCardRating],
) -> tuple[CardMetadataSeed, ...]:
    seeds: dict[int, CardMetadataSeed] = {}
    for rating in ratings:
        if rating.name.startswith("Unknown card "):
            continue

        seeds[rating.grp_id] = CardMetadataSeed(
            grp_id=rating.grp_id,
            name=rating.name,
            colors=_rating_colors(color=rating.color),
            rarity=rating.rarity or "unknown",
        )

    return tuple(seeds.values())


def _rating_colors(*, color: str | None) -> tuple[str, ...]:
    if color is None:
        return ()

    return tuple(symbol for symbol in "WUBRG" if symbol in color)


def seventeen_lands_cache_path(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None = None,
) -> Path:
    """Return the on-disk cache path for one set and format.
    The parent directory is created only when a refresh writes the cache.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir)
    filename = f"{_path_segment(set_code.upper())}-{_path_segment(event_format)}.json"
    return root / SEVENTEEN_CACHE_DIRECTORY_NAME / filename


def has_cached_17lands_data(
    *,
    set_code: str,
    event_format: str = QUICK_DRAFT_FORMAT,
    app_dir: PathInput | None = None,
) -> bool:
    """Return whether primary ratings exist locally for a set and format.
    Callers can request consent before the first network download.
    """

    return seventeen_lands_cache_path(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
    ).is_file()


def seventeen_lands_pair_card_cache_path(
    *,
    set_code: str,
    event_format: str,
    pair: str,
    app_dir: PathInput | None = None,
) -> Path:
    """Return the cache path for pair-filtered card ratings.
    Pair ratings are separate so all-decks caches stay small and stable.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir)
    filename = (
        f"{_path_segment(set_code.upper())}-"
        f"{_path_segment(event_format)}-"
        f"{_required_pair(pair, field_name='pair')}-cards.json"
    )
    return root / SEVENTEEN_CACHE_DIRECTORY_NAME / filename


def seventeen_lands_structure_targets_cache_path(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None = None,
) -> Path:
    """Return the cache path for empirical structure targets.
    Targets are loaded opportunistically by the deck builder when present.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir)
    filename = (
        f"{_path_segment(set_code.upper())}-"
        f"{_path_segment(event_format)}-structure-targets.json"
    )
    return root / SEVENTEEN_CACHE_DIRECTORY_NAME / filename


def public_draft_data_url(*, set_code: str, event_format: str) -> str:
    """Return the preferred 17Lands public draft dump URL.
    Public dumps are CC BY and preferred by the usage guidelines.
    """

    return PUBLIC_DRAFT_DATA_URL_TEMPLATE.format(
        set_code=set_code.upper(),
        event_format=event_format,
    )


def iter_17lands_draft_data_rows(*, path: PathInput) -> Iterable[Mapping[str, str]]:
    """Iterate rows from a public 17Lands draft-data dump.

    The neutral public-dump reader owns container handling and CSV parsing;
    this compatibility boundary preserves the historical row mapping API and
    error type.
    """

    try:
        yield from iter_public_dump_rows(path)
    except PublicDumpError as error:
        raise SeventeenLandsError(str(error)) from error


def card_ratings_url(
    *,
    set_code: str,
    event_format: str,
    colors: str | None = None,
) -> str:
    """Build the 17Lands card-ratings endpoint URL.
    ALL_TIME preserves historical samples when an old set returns to draft.
    """

    params = {
        "expansion": set_code.upper(),
        "event_type": event_format,
        "time_period": ALL_TIME_PERIOD,
    }
    if colors is not None:
        params["colors"] = colors

    return _url_with_query(endpoint=CARD_RATINGS_ENDPOINT, params=params)


def color_ratings_url(
    *,
    set_code: str,
    event_format: str,
) -> str:
    """Build the 17Lands color-ratings endpoint URL.
    ALL_TIME keeps exact pair rows useful across repeated draft windows.
    """

    return _url_with_query(
        endpoint=COLOR_RATINGS_ENDPOINT,
        params={
            "expansion": set_code.upper(),
            "event_type": event_format,
            "time_period": ALL_TIME_PERIOD,
            "combine_splash": "false",
        },
    )


def load_or_refresh_17lands_data(
    *,
    set_code: str,
    event_format: str = QUICK_DRAFT_FORMAT,
    app_dir: PathInput | None = None,
    refresh: bool = False,
    clock: Clock | None = None,
    fetch_json: FetchJson | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    cache_ttl: timedelta | None = None,
    thin_sample_minimum: int = PICK_ENGINE.thin_sample_minimum,
    premier_fallback_enabled: bool = PICK_ENGINE.premier_fallback_enabled,
    progress_callback: DownloadProgressCallback | None = None,
) -> SeventeenLandsData:
    """Load 17Lands data with the configured fallback chain.
    Premier fallback failures degrade to neutral-prior ratings.
    """

    total_requests = (
        4
        if premier_fallback_enabled and event_format != PREMIER_DRAFT_FORMAT
        else 2
    )
    _report_download_progress(
        callback=progress_callback,
        completed_requests=0,
        total_requests=total_requests,
        message=f"Checking local {event_format} ratings",
    )
    primary = load_or_refresh_17lands_format_data(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
        refresh=refresh,
        clock=clock,
        fetch_json=fetch_json,
        timeout_seconds=timeout_seconds,
        cache_ttl=cache_ttl,
        progress_callback=progress_callback,
        progress_offset=0,
        progress_total=total_requests,
    )
    fallback = None
    if premier_fallback_enabled and event_format != PREMIER_DRAFT_FORMAT:
        try:
            fallback = load_or_refresh_17lands_format_data(
                set_code=set_code,
                event_format=PREMIER_DRAFT_FORMAT,
                app_dir=app_dir,
                refresh=refresh,
                clock=clock,
                fetch_json=fetch_json,
                timeout_seconds=timeout_seconds,
                cache_ttl=cache_ttl,
                progress_callback=progress_callback,
                progress_offset=2,
                progress_total=total_requests,
            )
        except SeventeenLandsError:
            fallback = None

    structure_targets = _load_optional_structure_targets(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
    )
    return SeventeenLandsData(
        set_code=set_code.upper(),
        requested_format=event_format,
        primary=primary,
        fallback=fallback,
        structure_targets=(
            {} if structure_targets is None else structure_targets.targets
        ),
        pair_card_ratings_loader=lambda pair: load_or_refresh_17lands_pair_card_data(
            set_code=set_code,
            event_format=event_format,
            pair=pair,
            app_dir=app_dir,
            refresh=refresh,
            clock=clock,
            fetch_json=fetch_json,
            timeout_seconds=timeout_seconds,
            cache_ttl=cache_ttl,
        ),
        thin_sample_minimum=thin_sample_minimum,
    )


def load_cached_17lands_data(
    *,
    set_code: str,
    event_format: str = QUICK_DRAFT_FORMAT,
    app_dir: PathInput | None = None,
    thin_sample_minimum: int = PICK_ENGINE.thin_sample_minimum,
    premier_fallback_enabled: bool = PICK_ENGINE.premier_fallback_enabled,
) -> SeventeenLandsData:
    """Load cached 17Lands data without network access.
    Missing primary data becomes an empty neutral-prior dataset.
    """

    primary = _load_cached_or_empty_format_data(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
    )
    fallback = None
    if premier_fallback_enabled and event_format != PREMIER_DRAFT_FORMAT:
        fallback = _load_optional_cached_format_data(
            set_code=set_code,
            event_format=PREMIER_DRAFT_FORMAT,
            app_dir=app_dir,
        )

    structure_targets = _load_optional_structure_targets(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
    )
    return SeventeenLandsData(
        set_code=set_code.upper(),
        requested_format=event_format,
        primary=primary,
        fallback=fallback,
        structure_targets=(
            {} if structure_targets is None else structure_targets.targets
        ),
        pair_card_ratings_loader=lambda pair: _load_optional_pair_card_data(
            set_code=set_code,
            event_format=event_format,
            pair=pair,
            app_dir=app_dir,
        ),
        thin_sample_minimum=thin_sample_minimum,
    )


def load_or_refresh_17lands_pair_card_data(
    *,
    set_code: str,
    event_format: str = QUICK_DRAFT_FORMAT,
    pair: str,
    app_dir: PathInput | None = None,
    refresh: bool = False,
    clock: Clock | None = None,
    fetch_json: FetchJson | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    cache_ttl: timedelta | None = None,
) -> SeventeenLandsFormatData:
    """Load pair-filtered card ratings, refreshing only when needed.
    These ratings are fetched lazily once scoring locks onto a pair.
    """

    now = _now(clock=clock)
    ttl = _cache_ttl(cache_ttl=cache_ttl)
    cached = _load_optional_pair_card_data(
        set_code=set_code,
        event_format=event_format,
        pair=pair,
        app_dir=app_dir,
    )
    if cached is not None and not refresh and not _is_stale(
        fetched_at=cached.fetched_at,
        now=now,
        cache_ttl=ttl,
    ):
        return cached

    try:
        return refresh_17lands_pair_card_data(
            set_code=set_code,
            event_format=event_format,
            pair=pair,
            app_dir=app_dir,
            fetched_at=now,
            fetch_json=fetch_json,
            timeout_seconds=timeout_seconds,
        )
    except SeventeenLandsError:
        if cached is not None and not refresh:
            return cached

        raise


def load_or_refresh_17lands_format_data(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None = None,
    refresh: bool = False,
    clock: Clock | None = None,
    fetch_json: FetchJson | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    cache_ttl: timedelta | None = None,
    progress_callback: DownloadProgressCallback | None = None,
    progress_offset: int = 0,
    progress_total: int = 2,
) -> SeventeenLandsFormatData:
    """Load cached format data, refreshing only when stale.
    Stale but valid cache data is served when a refresh fails offline.
    """

    now = _now(clock=clock)
    ttl = _cache_ttl(cache_ttl=cache_ttl)
    cached = _load_existing_format_data(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
    )
    if cached is not None and not refresh and not _is_stale(
        fetched_at=cached.fetched_at,
        now=now,
        cache_ttl=ttl,
    ):
        _report_download_progress(
            callback=progress_callback,
            completed_requests=progress_offset + 2,
            total_requests=progress_total,
            message=f"Loaded cached {event_format} ratings",
        )
        return cached

    try:
        return refresh_17lands_format_data(
            set_code=set_code,
            event_format=event_format,
            app_dir=app_dir,
            fetched_at=now,
            fetch_json=fetch_json,
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
            progress_offset=progress_offset,
            progress_total=progress_total,
        )
    except SeventeenLandsError:
        if cached is not None and not refresh:
            _report_download_progress(
                callback=progress_callback,
                completed_requests=progress_offset + 2,
                total_requests=progress_total,
                message=f"Using cached {event_format} ratings",
            )
            return cached

        raise


def load_17lands_format_data(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
) -> SeventeenLandsFormatData:
    """Load one cached 17Lands set/format dataset without network calls.
    This is the fully offline path used after a successful refresh.
    """

    path = _format_cache_path(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
        cache_path=cache_path,
    )
    if not path.exists():
        raise SeventeenLandsCacheMissingError(
            f"17Lands cache does not exist at {path}. Refresh ratings first."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SeventeenLandsError(f"Malformed 17Lands cache {path}: {error}") from error

    if not isinstance(data, dict):
        raise SeventeenLandsError(f"Malformed 17Lands cache {path}: expected object.")

    dataset = SeventeenLandsFormatData.from_json(data=data)
    if dataset.set_code != set_code.upper():
        raise SeventeenLandsError(
            f"17Lands cache {path} is for set {dataset.set_code}, not {set_code.upper()}."
        )

    if dataset.event_format.casefold() != event_format.casefold():
        raise SeventeenLandsError(
            f"17Lands cache {path} is for format {dataset.event_format}, "
            f"not {event_format}."
        )

    return dataset


def load_17lands_structure_targets(
    *,
    set_code: str,
    event_format: str = QUICK_DRAFT_FORMAT,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
) -> SeventeenLandsStructureTargets:
    """Load cached empirical structure targets without network access.
    Missing caches are intentionally optional for normal deck building.
    """

    path = _structure_cache_path(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
        cache_path=cache_path,
    )
    if not path.exists():
        raise SeventeenLandsCacheMissingError(
            f"17Lands structure cache does not exist at {path}."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SeventeenLandsError(
            f"Malformed 17Lands structure cache {path}: {error}"
        ) from error

    if not isinstance(data, dict):
        raise SeventeenLandsError(
            f"Malformed 17Lands structure cache {path}: expected object."
        )

    targets = SeventeenLandsStructureTargets.from_json(data=data)
    if targets.set_code != set_code.upper():
        raise SeventeenLandsError(
            f"17Lands structure cache {path} is for set {targets.set_code}, "
            f"not {set_code.upper()}."
        )

    if targets.event_format != event_format:
        raise SeventeenLandsError(
            f"17Lands structure cache {path} is for format {targets.event_format}, "
            f"not {event_format}."
        )

    return targets


def save_17lands_structure_targets(
    targets: SeventeenLandsStructureTargets,
    *,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
) -> Path:
    """Write empirical structure targets atomically.
    The file is small because it stores only per-pair aggregates.
    """

    path = _structure_cache_path(
        set_code=targets.set_code,
        event_format=targets.event_format,
        app_dir=app_dir,
        cache_path=cache_path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(targets.to_json(), indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=path.parent,
        encoding="utf-8",
    ) as temporary_file:
        temporary_file.write(payload)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)

    temporary_path.replace(path)
    return path


def refresh_17lands_structure_targets(
    *,
    set_code: str,
    event_format: str = QUICK_DRAFT_FORMAT,
    card_database: CardDatabase,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
    draft_data_file: PathInput | None = None,
    draft_data_url: str | None = None,
    computed_at: datetime | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    config: DeckBuilderConfig = DECK_BUILDER,
) -> SeventeenLandsStructureTargets:
    """Compute and cache structure targets from public draft data.
    Public dumps are the usage-guideline-compliant source for deck-level data.
    """

    source_url = draft_data_url or public_draft_data_url(
        set_code=set_code,
        event_format=event_format,
    )
    if draft_data_file is None:
        with tempfile.TemporaryDirectory(prefix="draftomen-17lands-") as directory:
            temporary_path = Path(directory) / "draft-data.csv.gz"
            download_public_draft_data(
                url=source_url,
                path=temporary_path,
                timeout_seconds=timeout_seconds,
            )
            targets = compute_17lands_structure_targets(
                set_code=set_code,
                event_format=event_format,
                card_database=card_database,
                draft_data_file=temporary_path,
                source_url=source_url,
                computed_at=computed_at,
                config=config,
            )
    else:
        targets = compute_17lands_structure_targets(
            set_code=set_code,
            event_format=event_format,
            card_database=card_database,
            draft_data_file=draft_data_file,
            source_url=source_url if draft_data_url is not None else None,
            computed_at=computed_at,
            config=config,
        )

    save_17lands_structure_targets(
        targets,
        app_dir=app_dir,
        cache_path=cache_path,
    )
    return targets


def compute_17lands_structure_targets(
    *,
    set_code: str,
    event_format: str = QUICK_DRAFT_FORMAT,
    card_database: CardDatabase,
    draft_data_file: PathInput,
    source_url: str | None = None,
    computed_at: datetime | None = None,
    config: DeckBuilderConfig = DECK_BUILDER,
) -> SeventeenLandsStructureTargets:
    """Compute per-pair targets from a 17Lands public draft dump.
    Only trophy drafts are included, grouped by draft id.
    """

    return build_17lands_structure_targets_from_draft_rows(
        set_code=set_code,
        event_format=event_format,
        card_database=card_database,
        rows=iter_17lands_draft_data_rows(path=draft_data_file),
        source_url=source_url,
        computed_at=computed_at,
        config=config,
    )


def build_17lands_structure_targets_from_draft_rows(
    *,
    set_code: str,
    event_format: str = QUICK_DRAFT_FORMAT,
    card_database: CardDatabase,
    rows: Iterable[Mapping[str, str]],
    source_url: str | None = None,
    computed_at: datetime | None = None,
    config: DeckBuilderConfig = DECK_BUILDER,
) -> SeventeenLandsStructureTargets:
    """Build structure targets from already-loaded draft rows.
    Tests use this to exercise parsing without network or compressed files.
    """

    timestamp = datetime.now(tz=UTC) if computed_at is None else computed_at.astimezone(UTC)
    decks = _trophy_decks_from_rows(
        rows=rows,
        set_code=set_code,
        event_format=event_format,
        card_database=card_database,
        config=config,
    )
    metrics_by_pair: dict[str, list[_DeckStructureMetrics]] = {
        pair: [] for pair in COLOR_PAIRS
    }
    for cards in decks.values():
        metrics = _deck_structure_metrics(cards=tuple(cards), config=config)
        if metrics is not None:
            metrics_by_pair[metrics.pair].append(metrics)

    targets = {
        pair: _structural_targets_from_metrics(
            set_code=set_code.upper(),
            event_format=event_format,
            pair=pair,
            metrics=tuple(metrics),
            source_url=source_url,
            computed_at=timestamp,
        )
        for pair, metrics in metrics_by_pair.items()
        if metrics
    }
    return SeventeenLandsStructureTargets(
        set_code=set_code.upper(),
        event_format=event_format,
        computed_at=timestamp,
        source=STRUCTURE_TARGET_SOURCE,
        source_url=source_url,
        total_decks=sum(len(metrics) for metrics in metrics_by_pair.values()),
        targets=targets,
    )


def save_17lands_format_data(
    dataset: SeventeenLandsFormatData,
    *,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
) -> Path:
    """Write a 17Lands dataset cache atomically.
    The destination parent directory is created if needed.
    """

    path = _format_cache_path(
        set_code=dataset.set_code,
        event_format=dataset.event_format,
        app_dir=app_dir,
        cache_path=cache_path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dataset.to_json(), indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=path.parent,
        encoding="utf-8",
    ) as temporary_file:
        temporary_file.write(payload)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)

    temporary_path.replace(path)
    return path


def refresh_17lands_format_data(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
    fetched_at: datetime | None = None,
    fetch_json: FetchJson | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    progress_callback: DownloadProgressCallback | None = None,
    progress_offset: int = 0,
    progress_total: int = 2,
) -> SeventeenLandsFormatData:
    """Fetch 17Lands endpoints and write the normalized cache.
    Tests pass a fetch_json callable so no live network is required.
    """

    timestamp = datetime.now(tz=UTC) if fetched_at is None else fetched_at.astimezone(UTC)
    dataset = fetch_17lands_format_data(
        set_code=set_code,
        event_format=event_format,
        fetched_at=timestamp,
        fetch_json=fetch_json,
        timeout_seconds=timeout_seconds,
        progress_callback=progress_callback,
        progress_offset=progress_offset,
        progress_total=progress_total,
    )
    save_17lands_format_data(dataset, app_dir=app_dir, cache_path=cache_path)
    return dataset


def refresh_17lands_pair_card_data(
    *,
    set_code: str,
    event_format: str,
    pair: str,
    app_dir: PathInput | None = None,
    fetched_at: datetime | None = None,
    fetch_json: FetchJson | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> SeventeenLandsFormatData:
    """Fetch pair-filtered card ratings and write their separate cache.
    Pair caches contain only card ratings; aggregate pair rates stay primary.
    """

    timestamp = datetime.now(tz=UTC) if fetched_at is None else fetched_at.astimezone(UTC)
    dataset = fetch_17lands_pair_card_data(
        set_code=set_code,
        event_format=event_format,
        pair=pair,
        fetched_at=timestamp,
        fetch_json=fetch_json,
        timeout_seconds=timeout_seconds,
    )
    save_17lands_format_data(
        dataset,
        cache_path=seventeen_lands_pair_card_cache_path(
            set_code=set_code,
            event_format=event_format,
            pair=pair,
            app_dir=app_dir,
        ),
    )
    return dataset


def fetch_17lands_format_data(
    *,
    set_code: str,
    event_format: str,
    fetched_at: datetime | None = None,
    fetch_json: FetchJson | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    progress_callback: DownloadProgressCallback | None = None,
    progress_offset: int = 0,
    progress_total: int = 2,
) -> SeventeenLandsFormatData:
    """Fetch and normalize one set/format from 17Lands.
    The fetch_json hook receives each URL and timeout in seconds.
    """

    timestamp = datetime.now(tz=UTC) if fetched_at is None else fetched_at.astimezone(UTC)
    json_fetcher = _default_fetch_json if fetch_json is None else fetch_json
    card_payload = json_fetcher(
        card_ratings_url(
            set_code=set_code,
            event_format=event_format,
        ),
        timeout_seconds,
    )
    _report_download_progress(
        callback=progress_callback,
        completed_requests=progress_offset + 1,
        total_requests=progress_total,
        message=f"Downloaded all-time {event_format} card ratings",
    )
    color_payload = json_fetcher(
        color_ratings_url(
            set_code=set_code,
            event_format=event_format,
        ),
        timeout_seconds,
    )
    _report_download_progress(
        callback=progress_callback,
        completed_requests=progress_offset + 2,
        total_requests=progress_total,
        message=f"Downloaded all-time {event_format} color ratings",
    )
    return build_17lands_format_data(
        set_code=set_code,
        event_format=event_format,
        fetched_at=timestamp,
        card_ratings_payload=card_payload,
        color_ratings_payload=color_payload,
    )


def fetch_17lands_pair_card_data(
    *,
    set_code: str,
    event_format: str,
    pair: str,
    fetched_at: datetime | None = None,
    fetch_json: FetchJson | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> SeventeenLandsFormatData:
    """Fetch and normalize one pair-filtered card-rating table.
    Only the card endpoint is needed because pair win rates live in primary data.
    """

    timestamp = datetime.now(tz=UTC) if fetched_at is None else fetched_at.astimezone(UTC)
    json_fetcher = _default_fetch_json if fetch_json is None else fetch_json
    card_payload = json_fetcher(
        card_ratings_url(
            set_code=set_code,
            event_format=event_format,
            colors=_required_pair(pair, field_name="pair"),
        ),
        timeout_seconds,
    )
    return build_17lands_format_data(
        set_code=set_code,
        event_format=event_format,
        fetched_at=timestamp,
        card_ratings_payload=card_payload,
        color_ratings_payload=[],
    )


def build_17lands_format_data(
    *,
    set_code: str,
    event_format: str,
    fetched_at: datetime,
    card_ratings_payload: Any,
    color_ratings_payload: Any,
) -> SeventeenLandsFormatData:
    """Normalize recorded 17Lands endpoint responses into cache data.
    This powers tests and the live refresh path with identical parsing.
    """

    return SeventeenLandsFormatData(
        set_code=set_code.upper(),
        event_format=event_format,
        fetched_at=fetched_at.astimezone(UTC),
        card_ratings=_parse_card_ratings(payload=card_ratings_payload),
        pair_win_rates=_parse_pair_win_rates(payload=color_ratings_payload),
    )


def _load_existing_format_data(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None,
) -> SeventeenLandsFormatData | None:
    try:
        return load_17lands_format_data(
            set_code=set_code,
            event_format=event_format,
            app_dir=app_dir,
        )
    except (SeventeenLandsCacheMissingError, SeventeenLandsCacheOutdatedError):
        return None


def _load_cached_or_empty_format_data(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None,
) -> SeventeenLandsFormatData:
    cached = _load_optional_cached_format_data(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
    )
    if cached is not None:
        return cached

    return SeventeenLandsFormatData(
        set_code=set_code.upper(),
        event_format=event_format,
        fetched_at=datetime.now(tz=UTC),
        card_ratings={},
        pair_win_rates={},
    )


def _load_optional_cached_format_data(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None,
) -> SeventeenLandsFormatData | None:
    try:
        return load_17lands_format_data(
            set_code=set_code,
            event_format=event_format,
            app_dir=app_dir,
        )
    except (SeventeenLandsCacheMissingError, SeventeenLandsCacheOutdatedError):
        return None


def _load_optional_pair_card_data(
    *,
    set_code: str,
    event_format: str,
    pair: str,
    app_dir: PathInput | None,
) -> SeventeenLandsFormatData | None:
    try:
        return load_17lands_format_data(
            set_code=set_code,
            event_format=event_format,
            cache_path=seventeen_lands_pair_card_cache_path(
                set_code=set_code,
                event_format=event_format,
                pair=pair,
                app_dir=app_dir,
            ),
        )
    except (SeventeenLandsCacheMissingError, SeventeenLandsCacheOutdatedError):
        return None


def _load_optional_structure_targets(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None,
) -> SeventeenLandsStructureTargets | None:
    try:
        return load_17lands_structure_targets(
            set_code=set_code,
            event_format=event_format,
            app_dir=app_dir,
        )
    except SeventeenLandsCacheMissingError:
        return None


def _trophy_decks_from_rows(
    *,
    rows: Iterable[Mapping[str, str]],
    set_code: str,
    event_format: str,
    card_database: CardDatabase,
    config: DeckBuilderConfig,
) -> dict[str, list[CardInfo]]:
    name_index = _card_name_index(card_database=card_database)
    decks: dict[str, list[CardInfo]] = {}
    for row in rows:
        if not _draft_row_matches(
            row=row,
            set_code=set_code,
            event_format=event_format,
        ):
            continue

        if _optional_int(
            row.get("event_match_wins"),
            field_name="event_match_wins",
        ) != _trophy_wins(event_format=event_format):
            continue

        maindeck_rate = _optional_float(
            row.get("pick_maindeck_rate"),
            field_name="pick_maindeck_rate",
        )
        if maindeck_rate is None or maindeck_rate < config.structure_maindeck_rate_threshold:
            continue

        draft_id = _required_str(row.get("draft_id"), field_name="draft_id")
        pick_name = _required_str(row.get("pick"), field_name="pick")
        card = name_index.get(_normalize_card_name(pick_name))
        if card is None:
            continue

        decks.setdefault(draft_id, []).append(card)

    return decks


def _draft_row_matches(
    *,
    row: Mapping[str, str],
    set_code: str,
    event_format: str,
) -> bool:
    row_set = row.get("expansion")
    if row_set not in {None, "", set_code.upper()}:
        return False

    row_format = row.get("event_type")
    return row_format in {None, "", event_format}




def download_public_draft_data(
    *,
    url: str,
    path: Path,
    timeout_seconds: int,
) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/gzip,application/octet-stream;q=0.9,*/*;q=0.8",
            "User-Agent": SEVENTEEN_LANDS_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            with path.open(mode="wb") as output_file:
                shutil.copyfileobj(response, output_file)
    except urllib.error.URLError as error:
        raise SeventeenLandsError(
            f"Failed to download 17Lands public draft data: {error}"
        ) from error


def _card_name_index(*, card_database: CardDatabase) -> dict[str, CardInfo]:
    index: dict[str, CardInfo] = {}
    for card in card_database.cards.values():
        for name in _card_lookup_names(card=card):
            index.setdefault(_normalize_card_name(name), card)

    return index


def _card_lookup_names(*, card: CardInfo) -> tuple[str, ...]:
    names = [card.name]
    names.extend(part.strip() for part in card.name.split("//") if part.strip())
    return tuple(dict.fromkeys(names))


def _normalize_card_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _deck_structure_metrics(
    *,
    cards: tuple[CardInfo, ...],
    config: DeckBuilderConfig,
) -> _DeckStructureMetrics | None:
    nonland_cards = tuple(card for card in cards if not _structure_is_land_card(card=card))
    land_count = config.deck_size - len(nonland_cards)
    if land_count < config.structure_min_land_count:
        return None

    if land_count > config.structure_max_land_count:
        return None

    pair = _structure_pair(cards=nonland_cards)
    if pair is None:
        return None

    curve = {bucket: 0 for bucket in CURVE_BUCKETS}
    for card in nonland_cards:
        curve[_curve_bucket(card=card, config=config)] += 1

    return _DeckStructureMetrics(
        pair=pair,
        creature_count=sum(
            1 for card in nonland_cards if _structure_is_creature_card(card=card)
        ),
        land_count=land_count,
        spell_count=len(nonland_cards),
        two_drop_count=curve["2"],
        expensive_spell_count=curve["6+"],
        curve=curve,
    )


def _structure_pair(*, cards: tuple[CardInfo, ...]) -> str | None:
    color_counts = _empty_structure_color_counts()
    for card in cards:
        for color in card.colors:
            if color in color_counts:
                color_counts[color] += 1

    colors = tuple(
        color
        for color, count in sorted(
            color_counts.items(),
            key=lambda item: (-item[1], _color_order_index(color=item[0])),
        )
        if count > 0
    )
    if len(colors) < 2:
        return None

    return _canonical_pair(colors=colors[:2])


def _canonical_pair(*, colors: tuple[str, str]) -> str:
    color_set = set(colors)
    for pair in COLOR_PAIRS:
        if set(pair) == color_set:
            return pair

    raise SeventeenLandsError(f"Could not canonicalize color pair {colors}.")


def _empty_structure_color_counts() -> dict[str, int]:
    colors: dict[str, int] = {}
    for pair in COLOR_PAIRS:
        for color in pair:
            colors.setdefault(color, 0)

    return colors


def _color_order_index(*, color: str) -> int:
    colors = tuple(_empty_structure_color_counts())
    return colors.index(color)


def _structure_is_land_card(*, card: CardInfo) -> bool:
    return any("Land" in type_line for type_line in card.types)


def _structure_is_creature_card(*, card: CardInfo) -> bool:
    return any("Creature" in type_line for type_line in card.types)


def _curve_bucket(*, card: CardInfo, config: DeckBuilderConfig) -> str:
    mana_value = card.mana_value or 0.0
    if mana_value < config.two_drop_mana_value:
        return "0-1"

    if mana_value >= config.expensive_spell_mana_value:
        return "6+"

    return f"{int(mana_value)}"


def _trophy_wins(*, event_format: str) -> int:
    if event_format.startswith("Trad"):
        return 3

    return 7


def _structural_targets_from_metrics(
    *,
    set_code: str,
    event_format: str,
    pair: str,
    metrics: tuple[_DeckStructureMetrics, ...],
    source_url: str | None,
    computed_at: datetime,
) -> StructuralTargets:
    sample_size = len(metrics)
    if sample_size <= 0:
        raise SeventeenLandsError("Cannot build structure targets without decks.")

    return StructuralTargets(
        set_code=set_code,
        event_format=event_format,
        pair=pair,
        sample_size=sample_size,
        average_creature_count=_average(
            values=tuple(metric.creature_count for metric in metrics)
        ),
        average_land_count=_average(values=tuple(metric.land_count for metric in metrics)),
        average_spell_count=_average(values=tuple(metric.spell_count for metric in metrics)),
        average_two_drop_count=_average(
            values=tuple(metric.two_drop_count for metric in metrics)
        ),
        average_expensive_spell_count=_average(
            values=tuple(metric.expensive_spell_count for metric in metrics)
        ),
        average_curve=tuple(
            (
                bucket,
                _average(values=tuple(metric.curve[bucket] for metric in metrics)),
            )
            for bucket in CURVE_BUCKETS
        ),
        source=STRUCTURE_TARGET_SOURCE,
        source_url=source_url,
        computed_at=computed_at,
    )


def _average(*, values: tuple[int, ...]) -> float:
    if not values:
        return 0.0

    return sum(values) / len(values)


def _structure_cache_path(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None,
    cache_path: PathInput | None,
) -> Path:
    if cache_path is not None:
        return Path(cache_path)

    return seventeen_lands_structure_targets_cache_path(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
    )


def _parse_card_ratings(*, payload: Any) -> dict[int, SeventeenCardStats]:
    if isinstance(payload, dict):
        payload = payload.get("data")

    if not isinstance(payload, list):
        raise SeventeenLandsError(
            "17Lands card ratings payload does not contain a data list."
        )

    ratings: dict[int, SeventeenCardStats] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise SeventeenLandsError(
                f"17Lands card ratings item {index} is not an object."
            )

        stats = _card_stats_from_endpoint_row(row=item, index=index)
        ratings[stats.grp_id] = stats

    return ratings


def _card_stats_from_endpoint_row(
    *,
    row: Mapping[str, Any],
    index: int,
) -> SeventeenCardStats:
    grp_id = _required_int(row.get("mtga_id"), field_name=f"card_ratings[{index}].mtga_id")
    return SeventeenCardStats(
        grp_id=grp_id,
        name=_required_str(row.get("name"), field_name=f"card {grp_id}.name"),
        color=_card_color(row.get("color")),
        rarity=_required_str(row.get("rarity"), field_name=f"card {grp_id}.rarity"),
        average_last_seen_at=_optional_float(
            row.get("avg_seen"),
            field_name=f"card {grp_id}.avg_seen",
        ),
        gih_win_rate=_optional_float(
            row.get("ever_drawn_win_rate"),
            field_name=f"card {grp_id}.ever_drawn_win_rate",
        ),
        opening_hand_win_rate=_optional_float(
            row.get("opening_hand_win_rate"),
            field_name=f"card {grp_id}.opening_hand_win_rate",
        ),
        drawn_improvement_win_rate=_optional_float(
            row.get("drawn_improvement_win_rate"),
            field_name=f"card {grp_id}.drawn_improvement_win_rate",
        ),
        sample_counts=RatingSampleCounts(
            seen=_optional_int(row.get("seen_count"), field_name=f"card {grp_id}.seen_count")
            or 0,
            picked=_optional_int(
                row.get("pick_count"),
                field_name=f"card {grp_id}.pick_count",
            )
            or 0,
            games_played=_optional_int(
                row.get("game_count"),
                field_name=f"card {grp_id}.game_count",
            )
            or 0,
            opening_hand=_optional_int(
                row.get("opening_hand_game_count"),
                field_name=f"card {grp_id}.opening_hand_game_count",
            )
            or 0,
            games_in_hand=_optional_int(
                row.get("ever_drawn_game_count"),
                field_name=f"card {grp_id}.ever_drawn_game_count",
            )
            or 0,
        ),
    )


def _card_color(value: Any) -> str:
    if value is None or value == "":
        return "C"

    return _required_str(value, field_name="card.color")


def _parse_pair_win_rates(*, payload: Any) -> dict[str, ColorPairWinRate]:
    if not isinstance(payload, list):
        raise SeventeenLandsError("17Lands color ratings payload is not a list.")

    pairs: dict[str, ColorPairWinRate] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise SeventeenLandsError(
                f"17Lands color ratings item {index} is not an object."
            )

        pair = item.get("short_name")
        if pair not in COLOR_PAIRS:
            continue

        wins = _required_int(item.get("wins"), field_name=f"color_ratings[{index}].wins")
        games = _required_int(item.get("games"), field_name=f"color_ratings[{index}].games")
        _ensure_non_negative(value=wins, field_name=f"color_ratings[{index}].wins")
        _ensure_non_negative(value=games, field_name=f"color_ratings[{index}].games")
        pairs[pair] = ColorPairWinRate(
            pair=pair,
            wins=wins,
            games=games,
            win_rate=(wins / games) if games else None,
        )

    return pairs


def _resolved_from_stats(
    *,
    stats: SeventeenCardStats | None,
    format_data: SeventeenLandsFormatData | None,
    requested_format: str,
    source_format: str | None,
    fallback_reason: str | None,
) -> ResolvedCardRating:
    if stats is None:
        raise SeventeenLandsError("Cannot resolve a rating from missing card stats.")

    return ResolvedCardRating(
        grp_id=stats.grp_id,
        name=stats.name,
        color=stats.color,
        rarity=stats.rarity,
        average_last_seen_at=stats.average_last_seen_at,
        gih_win_rate=stats.gih_win_rate,
        opening_hand_win_rate=stats.opening_hand_win_rate,
        drawn_improvement_win_rate=stats.drawn_improvement_win_rate,
        sample_counts=stats.sample_counts,
        letter_grade=(
            None
            if format_data is None
            else format_data.letter_grade_for(grp_id=stats.grp_id)
        ),
        neutral_prior_score=None,
        metadata=RatingSourceMetadata(
            requested_format=requested_format,
            source=FORMAT_RATING_SOURCE,
            source_format=source_format,
            fallback_reason=fallback_reason,
        ),
    )


def _neutral_rating(
    *,
    grp_id: int,
    stats: SeventeenCardStats | None,
    requested_format: str,
    fallback_reason: str,
) -> ResolvedCardRating:
    sample_counts = (
        stats.sample_counts
        if stats is not None
        else RatingSampleCounts(
            seen=0,
            picked=0,
            games_played=0,
            opening_hand=0,
            games_in_hand=0,
        )
    )
    return ResolvedCardRating(
        grp_id=grp_id,
        name=stats.name if stats is not None else f"Unknown card {grp_id}",
        color=stats.color if stats is not None else None,
        rarity=stats.rarity if stats is not None else None,
        average_last_seen_at=stats.average_last_seen_at if stats is not None else None,
        gih_win_rate=None,
        opening_hand_win_rate=stats.opening_hand_win_rate if stats is not None else None,
        drawn_improvement_win_rate=(
            stats.drawn_improvement_win_rate if stats is not None else None
        ),
        sample_counts=sample_counts,
        letter_grade=None,
        neutral_prior_score=PICK_ENGINE.neutral_prior_score,
        metadata=RatingSourceMetadata(
            requested_format=requested_format,
            source=NEUTRAL_PRIOR_SOURCE,
            source_format=None,
            fallback_reason=fallback_reason,
        ),
    )


def _gih_win_rate_distribution(
    *,
    stats: Iterable[SeventeenCardStats],
) -> tuple[float, ...]:
    return tuple(
        card.gih_win_rate
        for card in stats
        if card.gih_win_rate is not None
    )


def letter_grade_for_metric(
    *,
    value: float | None,
    distribution: tuple[float, ...],
) -> str | None:
    if value is None or not distribution:
        return None

    mean = sum(distribution) / len(distribution)
    variance = sum((metric - mean) ** 2 for metric in distribution) / len(distribution)
    standard_deviation = math.sqrt(variance)
    if standard_deviation <= 0:
        return "C"

    grade_offset = _rounded_grade_offset(
        value=(value - mean) / standard_deviation / GRADE_STEP_STANDARD_DEVIATIONS
    )
    grade_index = _clamp_int(
        value=GRADE_CENTER_INDEX + grade_offset,
        lower=0,
        upper=len(GRADE_LABELS) - 1,
    )
    return GRADE_LABELS[grade_index]


def _rounded_grade_offset(*, value: float) -> int:
    if value >= 0:
        return math.floor(value + 0.5)

    return math.ceil(value - 0.5)


def _clamp_int(*, value: int, lower: int, upper: int) -> int:
    return min(max(value, lower), upper)


def _has_strong_gih_signal(
    *,
    stats: SeventeenCardStats | None,
    thin_sample_minimum: int,
) -> bool:
    if stats is None:
        return False

    return (
        stats.gih_win_rate is not None
        and stats.sample_counts.games_in_hand >= thin_sample_minimum
    )


def _set_data_reliability(*, data: SeventeenLandsData) -> SetDataReliability:
    card_ids = set(data.primary.card_ratings)
    if data.fallback is not None:
        card_ids.update(data.fallback.card_ratings)

    if not card_ids:
        return SetDataReliability(
            set_code=data.set_code,
            score=0,
            tier=_reliability_tier(score=0),
        )

    quick_samples: list[int] = []
    premier_samples: list[int] = []
    for grp_id in card_ids:
        quick_stats = data.primary.card_ratings.get(grp_id)
        if _has_strong_gih_signal(
            stats=quick_stats,
            thin_sample_minimum=data.thin_sample_minimum,
        ):
            if quick_stats is None:
                raise AssertionError("Strong Quick Draft stats cannot be missing.")
            quick_samples.append(quick_stats.sample_counts.games_in_hand)
            continue

        premier_stats = (
            None
            if data.fallback is None
            else data.fallback.card_ratings.get(grp_id)
        )
        if _has_strong_gih_signal(
            stats=premier_stats,
            thin_sample_minimum=data.thin_sample_minimum,
        ):
            if premier_stats is None:
                raise AssertionError("Strong Premier Draft stats cannot be missing.")
            premier_samples.append(premier_stats.sample_counts.games_in_hand)

    card_count = len(card_ids)
    quick_share = len(quick_samples) / card_count
    premier_share = len(premier_samples) / card_count
    coverage_quality = (
        quick_share + RELIABILITY_PREMIER_FACTOR * premier_share
    )
    depth_quality = (
        quick_share
        * _normalized_reliability_sample_depth(
            samples=quick_samples,
            minimum=data.thin_sample_minimum,
        )
        + RELIABILITY_PREMIER_FACTOR
        * premier_share
        * _normalized_reliability_sample_depth(
            samples=premier_samples,
            minimum=data.thin_sample_minimum,
        )
    )
    score = math.floor(
        100.0 * (0.5 * coverage_quality + 0.5 * depth_quality) + 0.5
    )
    score = _clamp_int(value=score, lower=0, upper=100)
    return SetDataReliability(
        set_code=data.set_code,
        score=score,
        tier=_reliability_tier(score=score),
    )


def _normalized_reliability_sample_depth(
    *,
    samples: list[int],
    minimum: int,
) -> float:
    if not samples:
        return 0.0

    sample_median = float(median(samples))
    if sample_median <= minimum:
        return 0.0

    if minimum >= RELIABILITY_SAMPLE_DEPTH_CAP:
        return 1.0

    depth = math.log(sample_median / minimum) / math.log(
        RELIABILITY_SAMPLE_DEPTH_CAP / minimum
    )
    return min(max(depth, 0.0), 1.0)


def _reliability_tier(*, score: int) -> str:
    if score >= RELIABILITY_VERY_HIGH_MINIMUM:
        return "Very high"
    if score >= RELIABILITY_HIGH_MINIMUM:
        return "High"
    if score >= RELIABILITY_MEDIUM_MINIMUM:
        return "Medium"
    if score > RELIABILITY_LOW_MAXIMUM:
        return "Low"

    return "Very low"


def _default_fetch_json(url: str, timeout_seconds: int) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json;q=0.9,*/*;q=0.8",
            "User-Agent": SEVENTEEN_LANDS_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise SeventeenLandsError(f"Failed to query 17Lands data: {error}") from error
    except json.JSONDecodeError as error:
        raise SeventeenLandsError(f"Malformed 17Lands JSON response: {error}") from error


def _report_download_progress(
    *,
    callback: DownloadProgressCallback | None,
    completed_requests: int,
    total_requests: int,
    message: str,
) -> None:
    if callback is None:
        return

    callback(
        SeventeenLandsDownloadProgress(
            completed_requests=completed_requests,
            total_requests=total_requests,
            message=message,
        )
    )


def _url_with_query(*, endpoint: str, params: Mapping[str, str]) -> str:
    return f"{endpoint}?{urllib.parse.urlencode(params)}"


def _format_cache_path(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None,
    cache_path: PathInput | None,
) -> Path:
    if cache_path is not None:
        return Path(cache_path)

    return seventeen_lands_cache_path(
        set_code=set_code,
        event_format=event_format,
        app_dir=app_dir,
    )


def _is_stale(*, fetched_at: datetime, now: datetime, cache_ttl: timedelta) -> bool:
    return now.astimezone(UTC) - fetched_at.astimezone(UTC) >= cache_ttl


def _cache_ttl(*, cache_ttl: timedelta | None) -> timedelta:
    if cache_ttl is not None:
        return cache_ttl

    return timedelta(hours=RATINGS_CACHE_TTL_HOURS)


def _now(*, clock: Clock | None) -> datetime:
    if clock is None:
        return datetime.now(tz=UTC)

    return clock().astimezone(UTC)


def _required_datetime(value: Any, *, field_name: str) -> datetime:
    text = _required_str(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise SeventeenLandsError(
            f"Missing or invalid {field_name}; expected ISO datetime."
        ) from error

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)

    return parsed.astimezone(UTC)


def _required_pair(value: Any, *, field_name: str) -> str:
    pair = _required_str(value, field_name=field_name)
    if pair not in COLOR_PAIRS:
        raise SeventeenLandsError(
            f"Missing or invalid {field_name}; expected a two-color pair."
        )

    return pair


def _path_segment(value: str) -> str:
    if value in {"", ".", ".."}:
        raise SeventeenLandsError("Invalid 17Lands cache path segment.")

    return value.replace("/", "_").replace("\\", "_").replace(":", "_")


def _ensure_non_negative(*, value: int, field_name: str) -> None:
    if value < 0:
        raise SeventeenLandsError(f"Missing or invalid {field_name}; expected >= 0.")


def _required_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise SeventeenLandsError(
            f"Missing or invalid {field_name}; expected non-empty string."
        )

    return value


def _optional_str(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None

    return _required_str(value, field_name=field_name)


def _required_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise SeventeenLandsError(f"Missing or invalid {field_name}; expected integer.")

    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise SeventeenLandsError(
            f"Missing or invalid {field_name}; expected integer."
        ) from error


def _optional_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None

    return _required_int(value, field_name=field_name)


def _required_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise SeventeenLandsError(f"Missing or invalid {field_name}; expected number.")

    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise SeventeenLandsError(
            f"Missing or invalid {field_name}; expected number."
        ) from error


def _optional_float(value: Any, *, field_name: str) -> float | None:
    if value is None:
        return None

    return _required_float(value, field_name=field_name)
