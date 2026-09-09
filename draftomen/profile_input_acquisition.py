"""Acquire normalized card metadata for profile generation.
Keep cache, provenance, and failure outcomes deterministic and path-free.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from draftomen.carddb import (
    HTTP_TIMEOUT_SECONDS,
    CardDatabase,
    CardDatabaseError,
    download_scryfall_card_database,
)
from draftomen.profile_input_cache import (
    ProfileInputCache,
    ProfileInputCacheError,
    ProfileInputCacheOutcome,
    ProfileInputCacheResult,
    ProfileInputSource,
)
from draftomen.public_dump import (
    PublicDumpError,
    PublicDumpManifest,
    PublicDumpReader,
    PublicDumpSource,
)
from draftomen.refresh_plan import PlannedEnvironment
from draftomen.seventeen import (
    SeventeenLandsError,
    SeventeenLandsFormatData,
    download_public_draft_data,
    fetch_17lands_format_data,
    public_draft_data_url,
)

PROFILE_INPUT_ACQUISITION_SCHEMA_VERSION = 1
CARD_METADATA_ADAPTER_VERSION = 1
CARD_METADATA_SOURCE_NAME = "card-metadata"
RATINGS_ADAPTER_VERSION = 1
RATINGS_SOURCE_NAME = "17lands-ratings"
PUBLIC_DRAFT_ADAPTER_VERSION = 1
PUBLIC_DRAFT_SOURCE_NAME = "17lands-public-drafts"
PUBLIC_DRAFT_ATTRIBUTION = "Public draft data: 17Lands"
PUBLIC_DRAFT_LICENSE = "CC BY 4.0"
Clock = Callable[[], datetime]
_PUBLIC_DRAFT_REQUIRED_FIELDS = frozenset(
    {
        "draft_id",
        "event_match_wins",
        "event_type",
        "expansion",
        "pick",
        "pick_maindeck_rate",
    }
)


class ProfileInputAcquisitionError(ValueError):
    """Report invalid acquisition arguments or normalized source data.
    Operational source failures are returned as bounded outcomes instead.
    """


class ProfileInputAcquisitionOutcome(str, Enum):
    """Describe how a usable or unavailable profile input was resolved.
    Values distinguish acquisition from cache and failure behavior.
    """

    ACQUIRED = "acquired"
    CACHED = "cached"
    OFFLINE_REUSED = "offline-reused"
    STALE = "stale"
    MISSING = "missing"
    CORRUPT = "corrupt"
    UNAVAILABLE = "unavailable"


class CardDatabaseFetcher(Protocol):
    """Fetch a card database for a normalized set identity.
    Implementations accept named parameters so tests remain explicit.
    """

    def __call__(self, *, set_code: str, timeout_seconds: int) -> CardDatabase:
        ...


class RatingsFetcher(Protocol):
    """Fetch normalized ratings for one set and event format.
    Implementations accept named parameters so tests remain explicit.
    """

    def __call__(
        self,
        *,
        set_code: str,
        event_format: str,
        fetched_at: datetime,
        timeout_seconds: int,
    ) -> SeventeenLandsFormatData:
        ...


class PublicDraftFetcher(Protocol):
    """Fetch one format-scoped public-draft dump to a local staging path.
    Implementations never return raw rows through the acquisition report.
    """

    def __call__(
        self,
        *,
        set_code: str,
        event_format: str,
        path: Path,
        timeout_seconds: int,
    ) -> None:
        ...


def _fetch_default_card_database(*, set_code: str, timeout_seconds: int) -> CardDatabase:
    del set_code
    return download_scryfall_card_database(timeout_seconds=timeout_seconds)


def _fetch_default_ratings(
    *,
    set_code: str,
    event_format: str,
    fetched_at: datetime,
    timeout_seconds: int,
) -> SeventeenLandsFormatData:
    return fetch_17lands_format_data(
        set_code=set_code,
        event_format=event_format,
        fetched_at=fetched_at,
        timeout_seconds=timeout_seconds,
    )


def _fetch_default_public_drafts(
    *,
    set_code: str,
    event_format: str,
    path: Path,
    timeout_seconds: int,
) -> None:
    download_public_draft_data(
        url=public_draft_data_url(
            set_code=set_code,
            event_format=event_format,
        ),
        path=path,
        timeout_seconds=timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class ProfileInputSourceReport:
    """Record one source decision using portable provenance only.
    Paths, card rows, credentials, and exception text are never retained.
    """

    source: ProfileInputSource
    outcome: ProfileInputAcquisitionOutcome
    cache_lookup_outcome: ProfileInputCacheOutcome | None
    cache_store_outcome: ProfileInputCacheOutcome | None = None
    source_version: str | None = None
    acquired_at: datetime | None = None
    sha256: str | None = None
    content_bytes: int | None = None
    card_count: int = 0
    rating_rows: int | None = None
    rating_samples: int | None = None
    draft_rows: int | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("card_count", "rating_rows", "rating_samples", "draft_rows"):
            count = getattr(self, name)
            if count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count < 0
            ):
                raise ProfileInputAcquisitionError(f"{name} must be a non-negative integer.")
        if (self.rating_rows is None) != (self.rating_samples is None):
            raise ProfileInputAcquisitionError(
                "Rating row and sample availability must be recorded together."
            )
        if self.draft_rows is not None and self.rating_rows is not None:
            raise ProfileInputAcquisitionError(
                "Draft and rating availability must use separate source reports."
            )
        object.__setattr__(self, "diagnostics", tuple(dict.fromkeys(self.diagnostics)))

    def to_json(self) -> dict[str, Any]:
        """Return deterministic provenance and sample availability.
        Optional cache-record fields appear only for verified inputs.
        """

        if self.draft_rows is not None:
            sample_availability = {"draft_rows": self.draft_rows}
        elif self.rating_rows is not None:
            sample_availability = {
                "rating_rows": self.rating_rows,
                "rating_samples": self.rating_samples,
            }
        else:
            sample_availability = {"card_count": self.card_count}
        value: dict[str, Any] = {
            "cache_lookup_outcome": (
                None if self.cache_lookup_outcome is None else self.cache_lookup_outcome.value
            ),
            "cache_store_outcome": (
                None if self.cache_store_outcome is None else self.cache_store_outcome.value
            ),
            "diagnostics": list(self.diagnostics),
            "outcome": self.outcome.value,
            "sample_availability": sample_availability,
            "source": self.source.to_json(),
        }
        if self.source_version is not None:
            value["source_version"] = self.source_version
        if self.acquired_at is not None:
            value["acquired_at"] = self.acquired_at.astimezone(UTC).isoformat()
        if self.sha256 is not None:
            value["sha256"] = self.sha256
        if self.content_bytes is not None:
            value["content_bytes"] = self.content_bytes
        return value


@dataclass(frozen=True, slots=True)
class ProfileBuildBundle:
    """Carry normalized inputs required by the existing profile generator.
    Raw source contents remain outside serialized acquisition reports.
    """

    environment: PlannedEnvironment
    card_database: CardDatabase = field(repr=False)
    card_metadata: ProfileInputSourceReport
    ratings: SeventeenLandsFormatData | None = field(default=None, repr=False)
    ratings_source: ProfileInputSourceReport | None = None
    public_drafts: PublicDumpManifest | None = field(default=None, repr=False)
    public_draft_source: ProfileInputSourceReport | None = None

    def __post_init__(self) -> None:
        if self.card_metadata.source.set_code != self.environment.set_code:
            raise ProfileInputAcquisitionError("Card metadata source does not match the bundle.")
        _validate_card_database(
            database=self.card_database,
            environment=self.environment,
            acquired_at=self.card_metadata.acquired_at,
        )
        if self.ratings_source is not None and (
            self.ratings_source.source.set_code != self.environment.set_code
            or self.ratings_source.source.event_format != self.environment.event_format
        ):
            raise ProfileInputAcquisitionError("Ratings source does not match the bundle.")
        if self.ratings is not None:
            if self.ratings_source is None:
                raise ProfileInputAcquisitionError("Ratings provenance is missing from the bundle.")
            _validate_ratings(
                ratings=self.ratings,
                environment=self.environment,
                acquired_at=self.ratings_source.acquired_at,
            )
        if self.public_draft_source is not None and (
            self.public_draft_source.source.set_code != self.environment.set_code
            or self.public_draft_source.source.event_format != self.environment.event_format
        ):
            raise ProfileInputAcquisitionError("Public-draft source does not match the bundle.")
        if self.public_drafts is not None:
            if self.public_draft_source is None:
                raise ProfileInputAcquisitionError(
                    "Public-draft provenance is missing from the bundle."
                )
            _validate_public_draft_manifest(
                manifest=self.public_drafts,
                report=self.public_draft_source,
            )

    def generator_inputs(self) -> dict[str, object]:
        """Return the values accepted by the existing profile generator.
        Stage, timestamp, and publication choices remain caller inputs.
        """

        values: dict[str, object] = {
            "card_database": self.card_database,
            "event_format": self.environment.event_format,
            "set_code": self.environment.set_code,
        }
        if self.ratings is not None:
            values["ratings"] = self.ratings
        if self.public_drafts is not None:
            values["source_manifest"] = self.public_drafts
        return values


@dataclass(frozen=True, slots=True)
class ProfileInputAcquisitionResult:
    """Return a usable bundle or a bounded required-source failure.
    Serialized reports expose source facts without the bundle contents.
    """

    environment: PlannedEnvironment
    source: ProfileInputSourceReport
    bundle: ProfileBuildBundle | None = field(default=None, repr=False)
    ratings_source: ProfileInputSourceReport | None = None
    public_draft_source: ProfileInputSourceReport | None = None
    skip_reasons: tuple[str, ...] = ()
    schema_version: int = PROFILE_INPUT_ACQUISITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROFILE_INPUT_ACQUISITION_SCHEMA_VERSION:
            raise ProfileInputAcquisitionError("Unsupported profile-input acquisition schema.")
        object.__setattr__(self, "skip_reasons", tuple(sorted(set(self.skip_reasons))))

    @property
    def succeeded(self) -> bool:
        """Return whether required metadata produced a build bundle.
        Stale and offline-reused verified inputs remain successful.
        """

        return self.bundle is not None

    @property
    def sources(self) -> tuple[ProfileInputSourceReport, ...]:
        """Return source reports in deterministic acquisition order.
        Required card metadata precedes optional empirical inputs.
        """

        optional = tuple(
            report
            for report in (self.ratings_source, self.public_draft_source)
            if report is not None
        )
        return (self.source, *optional)

    def to_json(self) -> dict[str, Any]:
        """Return a deterministic path-free acquisition report.
        The bundle is represented only by its availability flag.
        """

        return {
            "bundle_available": self.succeeded,
            "environment": self.environment.to_json(),
            "schema_version": self.schema_version,
            "skip_reasons": list(self.skip_reasons),
            "sources": [source.to_json() for source in self.sources],
        }

    def to_bytes(self) -> bytes:
        """Serialize the acquisition report as canonical JSON.
        Output has sorted keys, compact separators, and one final newline.
        """

        return (
            json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _CardMetadataSnapshot:
    database: CardDatabase
    payload: bytes
    source_version: str
    acquired_at: datetime
    sha256: str


@dataclass(frozen=True, slots=True)
class _RatingsSnapshot:
    ratings: SeventeenLandsFormatData
    payload: bytes
    source_version: str
    acquired_at: datetime
    sha256: str


@dataclass(frozen=True, slots=True)
class _RatingsAcquisition:
    report: ProfileInputSourceReport
    ratings: SeventeenLandsFormatData | None = field(default=None, repr=False)
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _PublicDraftSnapshot:
    source_version: str
    acquired_at: datetime
    sha256: str
    draft_rows: int


@dataclass(frozen=True, slots=True)
class _PublicDraftAcquisition:
    report: ProfileInputSourceReport
    manifest: PublicDumpManifest | None = field(default=None, repr=False)
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CardMetadataAdapter:
    """Fetch and normalize set-scoped card metadata into cacheable bytes.
    The default uses Scryfall bulk data without its separate runtime cache.
    """

    fetch_database: CardDatabaseFetcher = field(default=_fetch_default_card_database)
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS
    source_name: str = CARD_METADATA_SOURCE_NAME

    def __post_init__(self) -> None:
        if not callable(self.fetch_database):
            raise ProfileInputAcquisitionError("fetch_database must be callable.")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds <= 0
        ):
            raise ProfileInputAcquisitionError("timeout_seconds must be positive.")
        normalized = ProfileInputSource(name=self.source_name, set_code="TST")
        object.__setattr__(self, "source_name", normalized.name)

    def source_for(self, *, environment: PlannedEnvironment) -> ProfileInputSource:
        """Return the format-independent cache identity for one set.
        Card metadata can be shared across every event format for that set.
        """

        return ProfileInputSource(name=self.source_name, set_code=environment.set_code)

    def acquire(
        self,
        *,
        environment: PlannedEnvironment,
        acquired_at: datetime,
    ) -> _CardMetadataSnapshot:
        """Fetch, select, and serialize the requested set metadata.
        Empty or invalid data fails before the cache is mutated.
        """

        timestamp = _timestamp(acquired_at)
        database = self.fetch_database(
            set_code=environment.set_code,
            timeout_seconds=self.timeout_seconds,
        )
        normalized = _normalize_card_database(
            database=database,
            environment=environment,
            acquired_at=timestamp,
        )
        payload = _card_database_bytes(normalized)
        sha256 = hashlib.sha256(payload).hexdigest()
        version_time = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
        return _CardMetadataSnapshot(
            database=normalized,
            payload=payload,
            source_version=f"v{CARD_METADATA_ADAPTER_VERSION}-{version_time}-{sha256}",
            acquired_at=timestamp,
            sha256=sha256,
        )


@dataclass(frozen=True, slots=True)
class SeventeenLandsRatingsAdapter:
    """Fetch format-scoped 17Lands ratings as canonical cache bytes.
    The existing normalized ratings model remains the generator contract.
    """

    fetch_ratings: RatingsFetcher = field(default=_fetch_default_ratings)
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS
    source_name: str = RATINGS_SOURCE_NAME

    def __post_init__(self) -> None:
        if not callable(self.fetch_ratings):
            raise ProfileInputAcquisitionError("fetch_ratings must be callable.")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds <= 0
        ):
            raise ProfileInputAcquisitionError("timeout_seconds must be positive.")
        normalized = ProfileInputSource(
            name=self.source_name,
            set_code="TST",
            event_format="QuickDraft",
        )
        object.__setattr__(self, "source_name", normalized.name)

    def source_for(self, *, environment: PlannedEnvironment) -> ProfileInputSource:
        """Return the set-and-format cache identity for ratings.
        Quick Draft and Premier Draft evidence remain independent.
        """

        return ProfileInputSource(
            name=self.source_name,
            set_code=environment.set_code,
            event_format=environment.event_format,
        )

    def acquire(
        self,
        *,
        environment: PlannedEnvironment,
        acquired_at: datetime,
    ) -> _RatingsSnapshot:
        """Fetch and serialize normalized ratings for one environment.
        Empty, mismatched, or invalid data fails before cache mutation.
        """

        timestamp = _timestamp(acquired_at)
        fetched = self.fetch_ratings(
            set_code=environment.set_code,
            event_format=environment.event_format,
            fetched_at=timestamp,
            timeout_seconds=self.timeout_seconds,
        )
        _validate_ratings(
            ratings=fetched,
            environment=environment,
            acquired_at=timestamp,
        )
        ratings = SeventeenLandsFormatData.from_json(data=fetched.to_json())
        _validate_ratings(
            ratings=ratings,
            environment=environment,
            acquired_at=timestamp,
        )
        payload = _ratings_bytes(ratings)
        sha256 = hashlib.sha256(payload).hexdigest()
        version_time = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
        return _RatingsSnapshot(
            ratings=ratings,
            payload=payload,
            source_version=f"v{RATINGS_ADAPTER_VERSION}-{version_time}-{sha256}",
            acquired_at=timestamp,
            sha256=sha256,
        )


@dataclass(frozen=True, slots=True)
class SeventeenLandsPublicDraftAdapter:
    """Fetch and validate format-scoped 17Lands public draft evidence.
    Cache bytes remain the generator's pinned local source.
    """

    fetch_public_drafts: PublicDraftFetcher = field(default=_fetch_default_public_drafts)
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS
    source_name: str = PUBLIC_DRAFT_SOURCE_NAME

    def __post_init__(self) -> None:
        if not callable(self.fetch_public_drafts):
            raise ProfileInputAcquisitionError("fetch_public_drafts must be callable.")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds <= 0
        ):
            raise ProfileInputAcquisitionError("timeout_seconds must be positive.")
        normalized = ProfileInputSource(
            name=self.source_name,
            set_code="TST",
            event_format="QuickDraft",
        )
        object.__setattr__(self, "source_name", normalized.name)

    def source_for(self, *, environment: PlannedEnvironment) -> ProfileInputSource:
        """Return the set-and-format cache identity for public drafts.
        Quick Draft and Premier Draft evidence remain independent.
        """

        return ProfileInputSource(
            name=self.source_name,
            set_code=environment.set_code,
            event_format=environment.event_format,
        )

    def acquire(
        self,
        *,
        environment: PlannedEnvironment,
        acquired_at: datetime,
        path: Path,
    ) -> _PublicDraftSnapshot:
        """Fetch and validate one public-draft dump without retaining rows.
        Only matching supported rows can reach the input cache.
        """

        timestamp = _timestamp(acquired_at)
        self.fetch_public_drafts(
            set_code=environment.set_code,
            event_format=environment.event_format,
            path=path,
            timeout_seconds=self.timeout_seconds,
        )
        sha256 = _file_sha256(path=path)
        draft_rows = _validated_public_draft_rows(
            source=PublicDumpSource(
                name=self.source_name,
                path=path,
                sha256=sha256,
            ),
            environment=environment,
        )
        if draft_rows == 0:
            raise ProfileInputAcquisitionError(
                "Public-draft data did not contain any supported rows."
            )
        version_time = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
        return _PublicDraftSnapshot(
            source_version=f"v{PUBLIC_DRAFT_ADAPTER_VERSION}-{version_time}-{sha256}",
            acquired_at=timestamp,
            sha256=sha256,
            draft_rows=draft_rows,
        )


def acquire_card_metadata_bundle(
    *,
    environment: PlannedEnvironment,
    cache: ProfileInputCache,
    adapter: CardMetadataAdapter | None = None,
    offline: bool = False,
    clock: Clock | None = None,
) -> ProfileInputAcquisitionResult:
    """Resolve required card metadata through the bounded input cache.
    Online refresh preserves a verified stale fallback and reports failure.
    """

    if not isinstance(environment, PlannedEnvironment):
        raise ProfileInputAcquisitionError("environment must be a PlannedEnvironment.")
    if not isinstance(cache, ProfileInputCache):
        raise ProfileInputAcquisitionError("cache must be a ProfileInputCache.")
    selected_adapter = adapter or CardMetadataAdapter()
    source = selected_adapter.source_for(environment=environment)

    try:
        lookup = cache.lookup(source=source, offline=offline)
    except ProfileInputCacheError:
        return _failure(
            environment=environment,
            report=ProfileInputSourceReport(
                source=source,
                outcome=ProfileInputAcquisitionOutcome.UNAVAILABLE,
                cache_lookup_outcome=None,
                diagnostics=("cache-lookup-failed",),
            ),
            reason="card-metadata-cache-unavailable",
        )

    cached, content_diagnostics = _load_cached_database(
        result=lookup,
        environment=environment,
    )
    cache_is_corrupt = lookup.record is not None and cached is None
    if cached is not None and lookup.outcome in {
        ProfileInputCacheOutcome.FRESH,
        ProfileInputCacheOutcome.OFFLINE_REUSED,
    }:
        outcome = (
            ProfileInputAcquisitionOutcome.CACHED
            if lookup.outcome is ProfileInputCacheOutcome.FRESH
            else ProfileInputAcquisitionOutcome.OFFLINE_REUSED
        )
        return _success(
            environment=environment,
            database=cached,
            report=_report_from_cache(
                result=lookup,
                outcome=outcome,
                database=cached,
                diagnostics=content_diagnostics,
            ),
        )

    if offline:
        outcome = (
            ProfileInputAcquisitionOutcome.CORRUPT
            if cache_is_corrupt or lookup.outcome is ProfileInputCacheOutcome.CORRUPT
            else ProfileInputAcquisitionOutcome.MISSING
        )
        reason = (
            "card-metadata-cache-corrupt"
            if outcome is ProfileInputAcquisitionOutcome.CORRUPT
            else "card-metadata-cache-missing"
        )
        return _failure(
            environment=environment,
            report=_report_from_cache(
                result=lookup,
                outcome=outcome,
                diagnostics=content_diagnostics,
            ),
            reason=reason,
        )

    acquired_at = _now(clock)
    try:
        snapshot = selected_adapter.acquire(
            environment=environment,
            acquired_at=acquired_at,
        )
    except Exception:  # noqa: BLE001 - source failures become bounded outcomes.
        return _refresh_failure(
            environment=environment,
            lookup=lookup,
            cached=cached,
            cache_is_corrupt=cache_is_corrupt,
            diagnostics=(*content_diagnostics, "card-metadata-acquisition-failed"),
            stale_reason="card-metadata-refresh-failed",
            unavailable_reason="card-metadata-unavailable",
        )

    try:
        stored = cache.store(
            source=source,
            source_version=snapshot.source_version,
            input_stream=BytesIO(snapshot.payload),
            expected_sha256=snapshot.sha256,
            acquired_at=snapshot.acquired_at,
        )
    except ProfileInputCacheError:
        return _refresh_failure(
            environment=environment,
            lookup=lookup,
            cached=cached,
            cache_is_corrupt=cache_is_corrupt,
            diagnostics=(*content_diagnostics, "card-metadata-cache-store-failed"),
            stale_reason="card-metadata-cache-store-failed",
            unavailable_reason="card-metadata-cache-store-failed",
        )

    return _success(
        environment=environment,
        database=snapshot.database,
        report=_report_from_cache(
            result=stored,
            outcome=ProfileInputAcquisitionOutcome.ACQUIRED,
            database=snapshot.database,
            cache_lookup_outcome=ProfileInputCacheOutcome(lookup.outcome),
            cache_store_outcome=ProfileInputCacheOutcome(stored.outcome),
            diagnostics=content_diagnostics,
        ),
    )


def acquire_profile_build_bundle(
    *,
    environment: PlannedEnvironment,
    cache: ProfileInputCache,
    card_metadata_adapter: CardMetadataAdapter | None = None,
    ratings_adapter: SeventeenLandsRatingsAdapter | None = None,
    public_draft_adapter: SeventeenLandsPublicDraftAdapter | None = None,
    offline: bool = False,
    clock: Clock | None = None,
    include_public_drafts: bool = True,
) -> ProfileInputAcquisitionResult:
    """Acquire required metadata and independent empirical sources.
    Optional failures preserve every successfully acquired input.
    """

    if not isinstance(include_public_drafts, bool):
        raise ProfileInputAcquisitionError("include_public_drafts must be a bool.")
    metadata_result = acquire_card_metadata_bundle(
        environment=environment,
        cache=cache,
        adapter=card_metadata_adapter,
        offline=offline,
        clock=clock,
    )
    if metadata_result.bundle is None:
        return metadata_result

    ratings_result = _acquire_ratings(
        environment=environment,
        cache=cache,
        adapter=ratings_adapter or SeventeenLandsRatingsAdapter(),
        offline=offline,
        clock=clock,
    )
    public_draft_result = (
        _acquire_public_drafts(
            environment=environment,
            cache=cache,
            adapter=public_draft_adapter or SeventeenLandsPublicDraftAdapter(),
            offline=offline,
            clock=clock,
        )
        if include_public_drafts
        else None
    )
    skip_reasons = list(metadata_result.skip_reasons)
    if ratings_result.skip_reason is not None:
        skip_reasons.append(ratings_result.skip_reason)
    if public_draft_result is not None and public_draft_result.skip_reason is not None:
        skip_reasons.append(public_draft_result.skip_reason)
    return ProfileInputAcquisitionResult(
        environment=environment,
        source=metadata_result.source,
        bundle=ProfileBuildBundle(
            environment=environment,
            card_database=metadata_result.bundle.card_database,
            card_metadata=metadata_result.source,
            ratings=ratings_result.ratings,
            ratings_source=ratings_result.report,
            public_drafts=(
                None if public_draft_result is None else public_draft_result.manifest
            ),
            public_draft_source=(
                None if public_draft_result is None else public_draft_result.report
            ),
        ),
        ratings_source=ratings_result.report,
        public_draft_source=(
            None if public_draft_result is None else public_draft_result.report
        ),
        skip_reasons=tuple(skip_reasons),
    )


def _acquire_ratings(
    *,
    environment: PlannedEnvironment,
    cache: ProfileInputCache,
    adapter: SeventeenLandsRatingsAdapter,
    offline: bool,
    clock: Clock | None,
) -> _RatingsAcquisition:
    source = adapter.source_for(environment=environment)
    try:
        lookup = cache.lookup(source=source, offline=offline)
    except ProfileInputCacheError:
        return _RatingsAcquisition(
            report=ProfileInputSourceReport(
                source=source,
                outcome=ProfileInputAcquisitionOutcome.UNAVAILABLE,
                cache_lookup_outcome=None,
                rating_rows=0,
                rating_samples=0,
                diagnostics=("17lands-ratings-cache-lookup-failed",),
            ),
            skip_reason="17lands-ratings-cache-unavailable",
        )

    cached, content_diagnostics = _load_cached_ratings(
        result=lookup,
        environment=environment,
    )
    cache_is_corrupt = lookup.record is not None and cached is None
    if cached is not None and lookup.outcome in {
        ProfileInputCacheOutcome.FRESH,
        ProfileInputCacheOutcome.OFFLINE_REUSED,
    }:
        outcome = (
            ProfileInputAcquisitionOutcome.CACHED
            if lookup.outcome is ProfileInputCacheOutcome.FRESH
            else ProfileInputAcquisitionOutcome.OFFLINE_REUSED
        )
        return _RatingsAcquisition(
            ratings=cached,
            report=_ratings_report_from_cache(
                result=lookup,
                outcome=outcome,
                ratings=cached,
                diagnostics=content_diagnostics,
            ),
        )

    if offline:
        corrupt = cache_is_corrupt or lookup.outcome is ProfileInputCacheOutcome.CORRUPT
        return _RatingsAcquisition(
            report=_ratings_report_from_cache(
                result=lookup,
                outcome=(
                    ProfileInputAcquisitionOutcome.CORRUPT
                    if corrupt
                    else ProfileInputAcquisitionOutcome.MISSING
                ),
                diagnostics=content_diagnostics,
            ),
            skip_reason=(
                "17lands-ratings-cache-corrupt"
                if corrupt
                else "17lands-ratings-cache-missing"
            ),
        )

    acquired_at = _now(clock)
    try:
        snapshot = adapter.acquire(
            environment=environment,
            acquired_at=acquired_at,
        )
    except Exception:  # noqa: BLE001 - source failures become bounded outcomes.
        return _ratings_refresh_failure(
            lookup=lookup,
            cached=cached,
            cache_is_corrupt=cache_is_corrupt,
            diagnostics=(*content_diagnostics, "17lands-ratings-acquisition-failed"),
            stale_reason="17lands-ratings-refresh-failed",
            unavailable_reason="17lands-ratings-unavailable",
        )

    try:
        stored = cache.store(
            source=source,
            source_version=snapshot.source_version,
            input_stream=BytesIO(snapshot.payload),
            expected_sha256=snapshot.sha256,
            acquired_at=snapshot.acquired_at,
        )
    except ProfileInputCacheError:
        return _ratings_refresh_failure(
            lookup=lookup,
            cached=cached,
            cache_is_corrupt=cache_is_corrupt,
            diagnostics=(*content_diagnostics, "17lands-ratings-cache-store-failed"),
            stale_reason="17lands-ratings-cache-store-failed",
            unavailable_reason="17lands-ratings-cache-store-failed",
        )

    return _RatingsAcquisition(
        ratings=snapshot.ratings,
        report=_ratings_report_from_cache(
            result=stored,
            outcome=ProfileInputAcquisitionOutcome.ACQUIRED,
            ratings=snapshot.ratings,
            cache_lookup_outcome=ProfileInputCacheOutcome(lookup.outcome),
            cache_store_outcome=ProfileInputCacheOutcome(stored.outcome),
            diagnostics=content_diagnostics,
        ),
    )


def _acquire_public_drafts(
    *,
    environment: PlannedEnvironment,
    cache: ProfileInputCache,
    adapter: SeventeenLandsPublicDraftAdapter,
    offline: bool,
    clock: Clock | None,
) -> _PublicDraftAcquisition:
    source = adapter.source_for(environment=environment)
    try:
        lookup = cache.lookup(source=source, offline=offline)
    except ProfileInputCacheError:
        return _PublicDraftAcquisition(
            report=ProfileInputSourceReport(
                source=source,
                outcome=ProfileInputAcquisitionOutcome.UNAVAILABLE,
                cache_lookup_outcome=None,
                draft_rows=0,
                diagnostics=("17lands-public-drafts-cache-lookup-failed",),
            ),
            skip_reason="17lands-public-drafts-cache-unavailable",
        )

    cached, draft_rows, content_diagnostics = _load_cached_public_drafts(
        result=lookup,
        environment=environment,
    )
    cache_is_corrupt = lookup.record is not None and cached is None
    if cached is not None and lookup.outcome in {
        ProfileInputCacheOutcome.FRESH,
        ProfileInputCacheOutcome.OFFLINE_REUSED,
    }:
        outcome = (
            ProfileInputAcquisitionOutcome.CACHED
            if lookup.outcome is ProfileInputCacheOutcome.FRESH
            else ProfileInputAcquisitionOutcome.OFFLINE_REUSED
        )
        return _PublicDraftAcquisition(
            manifest=cached,
            report=_public_draft_report_from_cache(
                result=lookup,
                outcome=outcome,
                draft_rows=draft_rows,
                diagnostics=content_diagnostics,
            ),
        )

    if offline:
        corrupt = cache_is_corrupt or lookup.outcome is ProfileInputCacheOutcome.CORRUPT
        return _PublicDraftAcquisition(
            report=_public_draft_report_from_cache(
                result=lookup,
                outcome=(
                    ProfileInputAcquisitionOutcome.CORRUPT
                    if corrupt
                    else ProfileInputAcquisitionOutcome.MISSING
                ),
                diagnostics=content_diagnostics,
            ),
            skip_reason=(
                "17lands-public-drafts-cache-corrupt"
                if corrupt
                else "17lands-public-drafts-cache-missing"
            ),
        )

    acquired_at = _now(clock)
    try:
        with tempfile.TemporaryDirectory(prefix="draftomen-profile-input-") as directory:
            path = Path(directory) / "public-drafts.bin"
            try:
                snapshot = adapter.acquire(
                    environment=environment,
                    acquired_at=acquired_at,
                    path=path,
                )
            except Exception:  # noqa: BLE001 - source failures become bounded outcomes.
                return _public_draft_refresh_failure(
                    lookup=lookup,
                    cached=cached,
                    draft_rows=draft_rows,
                    cache_is_corrupt=cache_is_corrupt,
                    diagnostics=(
                        *content_diagnostics,
                        "17lands-public-drafts-acquisition-failed",
                    ),
                    stale_reason="17lands-public-drafts-refresh-failed",
                    unavailable_reason="17lands-public-drafts-unavailable",
                )
            try:
                with path.open(mode="rb") as input_stream:
                    stored = cache.store(
                        source=source,
                        source_version=snapshot.source_version,
                        input_stream=input_stream,
                        expected_sha256=snapshot.sha256,
                        acquired_at=snapshot.acquired_at,
                    )
            except (OSError, ProfileInputCacheError):
                return _public_draft_refresh_failure(
                    lookup=lookup,
                    cached=cached,
                    draft_rows=draft_rows,
                    cache_is_corrupt=cache_is_corrupt,
                    diagnostics=(
                        *content_diagnostics,
                        "17lands-public-drafts-cache-store-failed",
                    ),
                    stale_reason="17lands-public-drafts-cache-store-failed",
                    unavailable_reason="17lands-public-drafts-cache-store-failed",
                )
    except OSError:
        return _public_draft_refresh_failure(
            lookup=lookup,
            cached=cached,
            draft_rows=draft_rows,
            cache_is_corrupt=cache_is_corrupt,
            diagnostics=(
                *content_diagnostics,
                "17lands-public-drafts-acquisition-failed",
            ),
            stale_reason="17lands-public-drafts-refresh-failed",
            unavailable_reason="17lands-public-drafts-unavailable",
        )

    manifest = _public_draft_manifest(result=stored)
    return _PublicDraftAcquisition(
        manifest=manifest,
        report=_public_draft_report_from_cache(
            result=stored,
            outcome=ProfileInputAcquisitionOutcome.ACQUIRED,
            draft_rows=snapshot.draft_rows,
            cache_lookup_outcome=ProfileInputCacheOutcome(lookup.outcome),
            cache_store_outcome=ProfileInputCacheOutcome(stored.outcome),
            diagnostics=content_diagnostics,
        ),
    )


def _normalize_card_database(
    *,
    database: CardDatabase,
    environment: PlannedEnvironment,
    acquired_at: datetime,
) -> CardDatabase:
    if not isinstance(database, CardDatabase):
        raise ProfileInputAcquisitionError("Card metadata adapter returned an invalid database.")
    requested_set = environment.set_code.casefold()
    selected = CardDatabase(
        cards={
            grp_id: card
            for grp_id, card in database.cards.items()
            if card.set_code is not None and card.set_code.casefold() == requested_set
        },
        generated_at=acquired_at,
    )
    normalized = CardDatabase.from_json(data=selected.to_json())
    _validate_card_database(
        database=normalized,
        environment=environment,
        acquired_at=acquired_at,
    )
    return normalized


def _validate_card_database(
    *,
    database: CardDatabase,
    environment: PlannedEnvironment,
    acquired_at: datetime | None,
) -> None:
    if not database.cards:
        raise ProfileInputAcquisitionError("Card metadata did not contain the requested set.")
    requested_set = environment.set_code.casefold()
    if any(
        card.set_code is None or card.set_code.casefold() != requested_set
        for card in database.cards.values()
    ):
        raise ProfileInputAcquisitionError("Card metadata contains an unexpected set.")
    if acquired_at is None or database.generated_at is None:
        raise ProfileInputAcquisitionError("Card metadata acquisition timestamp is missing.")
    if _timestamp(database.generated_at) != _timestamp(acquired_at):
        raise ProfileInputAcquisitionError("Card metadata timestamp does not match its record.")


def _card_database_bytes(database: CardDatabase) -> bytes:
    return (
        json.dumps(database.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _load_cached_database(
    *,
    result: ProfileInputCacheResult,
    environment: PlannedEnvironment,
) -> tuple[CardDatabase | None, tuple[str, ...]]:
    if result.record is None or result.content_path is None:
        return None, result.diagnostics
    try:
        payload = result.content_path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ProfileInputAcquisitionError("Card metadata cache is not an object.")
        database = CardDatabase.from_json(data=value)
        if payload != _card_database_bytes(database):
            raise ProfileInputAcquisitionError("Card metadata cache is not canonical.")
        _validate_card_database(
            database=database,
            environment=environment,
            acquired_at=result.record.acquired_at,
        )
    except (
        CardDatabaseError,
        OSError,
        ProfileInputAcquisitionError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None, (*result.diagnostics, "card-metadata-content-invalid")
    return database, result.diagnostics


def _validate_ratings(
    *,
    ratings: SeventeenLandsFormatData,
    environment: PlannedEnvironment,
    acquired_at: datetime | None,
) -> None:
    if not isinstance(ratings, SeventeenLandsFormatData):
        raise ProfileInputAcquisitionError("Ratings adapter returned an invalid dataset.")
    if ratings.set_code.casefold() != environment.set_code.casefold():
        raise ProfileInputAcquisitionError("Ratings contain an unexpected set.")
    if ratings.event_format.casefold() != environment.event_format.casefold():
        raise ProfileInputAcquisitionError("Ratings contain an unexpected event format.")
    if not ratings.card_ratings:
        raise ProfileInputAcquisitionError("Ratings did not contain any card rows.")
    if acquired_at is None:
        raise ProfileInputAcquisitionError("Ratings acquisition timestamp is missing.")
    if _timestamp(ratings.fetched_at) != _timestamp(acquired_at):
        raise ProfileInputAcquisitionError("Ratings timestamp does not match its record.")
    for row in ratings.card_ratings.values():
        for name in ("seen", "picked", "games_played", "opening_hand", "games_in_hand"):
            count = getattr(row.sample_counts, name)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ProfileInputAcquisitionError("Ratings contain invalid sample counts.")


def _ratings_bytes(ratings: SeventeenLandsFormatData) -> bytes:
    return (
        json.dumps(ratings.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _rating_sample_count(ratings: SeventeenLandsFormatData) -> int:
    return sum(row.sample_counts.games_in_hand for row in ratings.card_ratings.values())


def _load_cached_ratings(
    *,
    result: ProfileInputCacheResult,
    environment: PlannedEnvironment,
) -> tuple[SeventeenLandsFormatData | None, tuple[str, ...]]:
    if result.record is None or result.content_path is None:
        return None, result.diagnostics
    try:
        payload = result.content_path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ProfileInputAcquisitionError("Ratings cache is not an object.")
        ratings = SeventeenLandsFormatData.from_json(data=value)
        if payload != _ratings_bytes(ratings):
            raise ProfileInputAcquisitionError("Ratings cache is not canonical.")
        _validate_ratings(
            ratings=ratings,
            environment=environment,
            acquired_at=result.record.acquired_at,
        )
    except (
        OSError,
        ProfileInputAcquisitionError,
        SeventeenLandsError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None, (*result.diagnostics, "17lands-ratings-content-invalid")
    return ratings, result.diagnostics


def _file_sha256(*, path: Path) -> str:
    digest = hashlib.sha256()
    with path.open(mode="rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_public_draft_rows(
    *,
    source: PublicDumpSource,
    environment: PlannedEnvironment,
) -> int:
    rows = 0
    reader = PublicDumpReader(source=source)
    for row in reader.iter_rows():
        if not _PUBLIC_DRAFT_REQUIRED_FIELDS.issubset(row):
            raise ProfileInputAcquisitionError(
                "Public-draft data does not use the supported row schema."
            )
        if row["expansion"].strip().casefold() != environment.set_code.casefold():
            raise ProfileInputAcquisitionError(
                "Public-draft data contains an unexpected set."
            )
        if row["event_type"].strip().casefold() != environment.event_format.casefold():
            raise ProfileInputAcquisitionError(
                "Public-draft data contains an unexpected event format."
            )
        rows += 1
    return rows


def _public_draft_manifest(*, result: ProfileInputCacheResult) -> PublicDumpManifest:
    if result.record is None or result.content_path is None:
        raise ProfileInputAcquisitionError("Verified public-draft cache data is missing.")
    return PublicDumpManifest(
        sources=(
            PublicDumpSource(
                name=result.source.name,
                path=result.content_path,
                sha256=result.record.sha256,
                retrieved_at=result.record.acquired_at.astimezone(UTC).isoformat(),
                attribution=PUBLIC_DRAFT_ATTRIBUTION,
                license=PUBLIC_DRAFT_LICENSE,
            ),
        )
    )


def _validate_public_draft_manifest(
    *,
    manifest: PublicDumpManifest,
    report: ProfileInputSourceReport,
) -> None:
    if len(manifest.sources) != 1:
        raise ProfileInputAcquisitionError(
            "Profile bundles require exactly one public-draft source."
        )
    source = manifest.sources[0]
    if (
        source.name != report.source.name
        or source.sha256 != report.sha256
        or source.path is None
        or report.draft_rows is None
        or report.draft_rows == 0
    ):
        raise ProfileInputAcquisitionError(
            "Public-draft manifest does not match its acquisition report."
        )


def _load_cached_public_drafts(
    *,
    result: ProfileInputCacheResult,
    environment: PlannedEnvironment,
) -> tuple[PublicDumpManifest | None, int, tuple[str, ...]]:
    if result.record is None or result.content_path is None:
        return None, 0, result.diagnostics
    try:
        manifest = _public_draft_manifest(result=result)
        draft_rows = _validated_public_draft_rows(
            source=manifest.sources[0],
            environment=environment,
        )
        if draft_rows == 0:
            raise ProfileInputAcquisitionError(
                "Public-draft cache did not contain supported rows."
            )
    except (OSError, ProfileInputAcquisitionError, PublicDumpError):
        return None, 0, (
            *result.diagnostics,
            "17lands-public-drafts-content-invalid",
        )
    return manifest, draft_rows, result.diagnostics


def _refresh_failure(
    *,
    environment: PlannedEnvironment,
    lookup: ProfileInputCacheResult,
    cached: CardDatabase | None,
    cache_is_corrupt: bool,
    diagnostics: tuple[str, ...],
    stale_reason: str,
    unavailable_reason: str,
) -> ProfileInputAcquisitionResult:
    if cached is not None and lookup.outcome is ProfileInputCacheOutcome.STALE:
        return _success(
            environment=environment,
            database=cached,
            report=_report_from_cache(
                result=lookup,
                outcome=ProfileInputAcquisitionOutcome.STALE,
                database=cached,
                diagnostics=diagnostics,
            ),
            reason=stale_reason,
        )
    corrupt = cache_is_corrupt or lookup.outcome is ProfileInputCacheOutcome.CORRUPT
    return _failure(
        environment=environment,
        report=_report_from_cache(
            result=lookup,
            outcome=(
                ProfileInputAcquisitionOutcome.CORRUPT
                if corrupt
                else ProfileInputAcquisitionOutcome.UNAVAILABLE
            ),
            diagnostics=diagnostics,
        ),
        reason=("card-metadata-cache-corrupt" if corrupt else unavailable_reason),
    )


def _ratings_refresh_failure(
    *,
    lookup: ProfileInputCacheResult,
    cached: SeventeenLandsFormatData | None,
    cache_is_corrupt: bool,
    diagnostics: tuple[str, ...],
    stale_reason: str,
    unavailable_reason: str,
) -> _RatingsAcquisition:
    if cached is not None and lookup.outcome is ProfileInputCacheOutcome.STALE:
        return _RatingsAcquisition(
            ratings=cached,
            report=_ratings_report_from_cache(
                result=lookup,
                outcome=ProfileInputAcquisitionOutcome.STALE,
                ratings=cached,
                diagnostics=diagnostics,
            ),
            skip_reason=stale_reason,
        )
    corrupt = cache_is_corrupt or lookup.outcome is ProfileInputCacheOutcome.CORRUPT
    return _RatingsAcquisition(
        report=_ratings_report_from_cache(
            result=lookup,
            outcome=(
                ProfileInputAcquisitionOutcome.CORRUPT
                if corrupt
                else ProfileInputAcquisitionOutcome.UNAVAILABLE
            ),
            diagnostics=diagnostics,
        ),
        skip_reason=("17lands-ratings-cache-corrupt" if corrupt else unavailable_reason),
    )


def _public_draft_refresh_failure(
    *,
    lookup: ProfileInputCacheResult,
    cached: PublicDumpManifest | None,
    draft_rows: int,
    cache_is_corrupt: bool,
    diagnostics: tuple[str, ...],
    stale_reason: str,
    unavailable_reason: str,
) -> _PublicDraftAcquisition:
    if cached is not None and lookup.outcome is ProfileInputCacheOutcome.STALE:
        return _PublicDraftAcquisition(
            manifest=cached,
            report=_public_draft_report_from_cache(
                result=lookup,
                outcome=ProfileInputAcquisitionOutcome.STALE,
                draft_rows=draft_rows,
                diagnostics=diagnostics,
            ),
            skip_reason=stale_reason,
        )
    corrupt = cache_is_corrupt or lookup.outcome is ProfileInputCacheOutcome.CORRUPT
    return _PublicDraftAcquisition(
        report=_public_draft_report_from_cache(
            result=lookup,
            outcome=(
                ProfileInputAcquisitionOutcome.CORRUPT
                if corrupt
                else ProfileInputAcquisitionOutcome.UNAVAILABLE
            ),
            diagnostics=diagnostics,
        ),
        skip_reason=(
            "17lands-public-drafts-cache-corrupt" if corrupt else unavailable_reason
        ),
    )


def _report_from_cache(
    *,
    result: ProfileInputCacheResult,
    outcome: ProfileInputAcquisitionOutcome,
    database: CardDatabase | None = None,
    cache_lookup_outcome: ProfileInputCacheOutcome | None = None,
    cache_store_outcome: ProfileInputCacheOutcome | None = None,
    diagnostics: tuple[str, ...] = (),
) -> ProfileInputSourceReport:
    record = result.record
    lookup_outcome = ProfileInputCacheOutcome(result.outcome)
    return ProfileInputSourceReport(
        source=result.source,
        outcome=outcome,
        cache_lookup_outcome=(
            lookup_outcome if cache_lookup_outcome is None else cache_lookup_outcome
        ),
        cache_store_outcome=cache_store_outcome,
        source_version=None if record is None else record.source_version,
        acquired_at=None if record is None else record.acquired_at,
        sha256=None if record is None else record.sha256,
        content_bytes=None if record is None else record.content_bytes,
        card_count=0 if database is None else len(database),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
    )


def _ratings_report_from_cache(
    *,
    result: ProfileInputCacheResult,
    outcome: ProfileInputAcquisitionOutcome,
    ratings: SeventeenLandsFormatData | None = None,
    cache_lookup_outcome: ProfileInputCacheOutcome | None = None,
    cache_store_outcome: ProfileInputCacheOutcome | None = None,
    diagnostics: tuple[str, ...] = (),
) -> ProfileInputSourceReport:
    record = (
        result.record
        if outcome
        in (
            ProfileInputAcquisitionOutcome.ACQUIRED,
            ProfileInputAcquisitionOutcome.CACHED,
            ProfileInputAcquisitionOutcome.OFFLINE_REUSED,
            ProfileInputAcquisitionOutcome.STALE,
        )
        else None
    )
    lookup_outcome = ProfileInputCacheOutcome(result.outcome)
    return ProfileInputSourceReport(
        source=result.source,
        outcome=outcome,
        cache_lookup_outcome=(
            lookup_outcome if cache_lookup_outcome is None else cache_lookup_outcome
        ),
        cache_store_outcome=cache_store_outcome,
        source_version=None if record is None else record.source_version,
        acquired_at=None if record is None else record.acquired_at,
        sha256=None if record is None else record.sha256,
        content_bytes=None if record is None else record.content_bytes,
        rating_rows=0 if ratings is None else len(ratings.card_ratings),
        rating_samples=0 if ratings is None else _rating_sample_count(ratings),
        diagnostics=diagnostics,
    )


def _public_draft_report_from_cache(
    *,
    result: ProfileInputCacheResult,
    outcome: ProfileInputAcquisitionOutcome,
    draft_rows: int = 0,
    cache_lookup_outcome: ProfileInputCacheOutcome | None = None,
    cache_store_outcome: ProfileInputCacheOutcome | None = None,
    diagnostics: tuple[str, ...] = (),
) -> ProfileInputSourceReport:
    record = (
        result.record
        if outcome
        in (
            ProfileInputAcquisitionOutcome.ACQUIRED,
            ProfileInputAcquisitionOutcome.CACHED,
            ProfileInputAcquisitionOutcome.OFFLINE_REUSED,
            ProfileInputAcquisitionOutcome.STALE,
        )
        else None
    )
    lookup_outcome = ProfileInputCacheOutcome(result.outcome)
    return ProfileInputSourceReport(
        source=result.source,
        outcome=outcome,
        cache_lookup_outcome=(
            lookup_outcome if cache_lookup_outcome is None else cache_lookup_outcome
        ),
        cache_store_outcome=cache_store_outcome,
        source_version=None if record is None else record.source_version,
        acquired_at=None if record is None else record.acquired_at,
        sha256=None if record is None else record.sha256,
        content_bytes=None if record is None else record.content_bytes,
        draft_rows=draft_rows,
        diagnostics=diagnostics,
    )


def _success(
    *,
    environment: PlannedEnvironment,
    database: CardDatabase,
    report: ProfileInputSourceReport,
    reason: str | None = None,
) -> ProfileInputAcquisitionResult:
    return ProfileInputAcquisitionResult(
        environment=environment,
        source=report,
        bundle=ProfileBuildBundle(
            environment=environment,
            card_database=database,
            card_metadata=report,
        ),
        skip_reasons=() if reason is None else (reason,),
    )


def _failure(
    *,
    environment: PlannedEnvironment,
    report: ProfileInputSourceReport,
    reason: str,
) -> ProfileInputAcquisitionResult:
    return ProfileInputAcquisitionResult(
        environment=environment,
        source=report,
        skip_reasons=(reason,),
    )


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProfileInputAcquisitionError("Acquisition timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def _now(clock: Clock | None) -> datetime:
    if clock is not None and not callable(clock):
        raise ProfileInputAcquisitionError("clock must be callable.")
    return _timestamp(datetime.now(tz=UTC) if clock is None else clock())


__all__ = [
    "CARD_METADATA_ADAPTER_VERSION",
    "CARD_METADATA_SOURCE_NAME",
    "PROFILE_INPUT_ACQUISITION_SCHEMA_VERSION",
    "PUBLIC_DRAFT_ADAPTER_VERSION",
    "PUBLIC_DRAFT_ATTRIBUTION",
    "PUBLIC_DRAFT_LICENSE",
    "PUBLIC_DRAFT_SOURCE_NAME",
    "RATINGS_ADAPTER_VERSION",
    "RATINGS_SOURCE_NAME",
    "CardDatabaseFetcher",
    "CardMetadataAdapter",
    "ProfileBuildBundle",
    "ProfileInputAcquisitionError",
    "ProfileInputAcquisitionOutcome",
    "ProfileInputAcquisitionResult",
    "ProfileInputSourceReport",
    "PublicDraftFetcher",
    "RatingsFetcher",
    "SeventeenLandsPublicDraftAdapter",
    "SeventeenLandsRatingsAdapter",
    "acquire_card_metadata_bundle",
    "acquire_profile_build_bundle",
]
