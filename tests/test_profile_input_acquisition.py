from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_generation import generate_set_profile
from draftomen.profile_input_acquisition import (
    CARD_METADATA_ADAPTER_VERSION,
    PUBLIC_DRAFT_ADAPTER_VERSION,
    RATINGS_ADAPTER_VERSION,
    CardMetadataAdapter,
    ProfileInputAcquisitionError,
    ProfileInputAcquisitionOutcome,
    ProfileInputAcquisitionResult,
    SeventeenLandsPublicDraftAdapter,
    SeventeenLandsRatingsAdapter,
    acquire_card_metadata_bundle,
    acquire_profile_build_bundle,
)
from draftomen.profile_input_cache import (
    ProfileInputCache,
    ProfileInputCacheCapacityError,
    ProfileInputCacheOutcome,
    ProfileInputCachePolicy,
)
from draftomen.refresh_plan import PlannedEnvironment
from draftomen.set_profile import ProfileMaturity
from draftomen.seventeen import (
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsFormatData,
    fetch_17lands_format_data,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


class FrozenClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class StubCardDatabaseFetcher:
    def __init__(self, database: CardDatabase) -> None:
        self.database = database
        self.calls: list[tuple[str, int]] = []
        self.error: Exception | None = None

    def __call__(self, *, set_code: str, timeout_seconds: int) -> CardDatabase:
        self.calls.append((set_code, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.database


class StubRatingsFetcher:
    def __init__(self, ratings: SeventeenLandsFormatData) -> None:
        self.ratings = ratings
        self.calls: list[tuple[str, str, datetime, int]] = []
        self.error: Exception | None = None

    def __call__(
        self,
        *,
        set_code: str,
        event_format: str,
        fetched_at: datetime,
        timeout_seconds: int,
    ) -> SeventeenLandsFormatData:
        self.calls.append((set_code, event_format, fetched_at, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.ratings


class StubPublicDraftFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, Path, int]] = []
        self.error: Exception | None = None

    def __call__(
        self,
        *,
        set_code: str,
        event_format: str,
        path: Path,
        timeout_seconds: int,
    ) -> None:
        self.calls.append((set_code, event_format, path, timeout_seconds))
        if self.error is not None:
            raise self.error
        path.write_bytes(self.payload)


def _environment(*, event_format: str = "QuickDraft") -> PlannedEnvironment:
    return PlannedEnvironment(
        set_code="TST",
        event_format=event_format,
        lifecycle="active",
        reasons=("manual-selection",),
    )


def _database() -> CardDatabase:
    return CardDatabase(
        cards={
            1: CardInfo(
                grp_id=1,
                name="Requested Card",
                colors=("U",),
                mana_value=2,
                rarity="common",
                types=("Creature",),
                set_code="TST",
            ),
            2: CardInfo(
                grp_id=2,
                name="Other Set Card",
                colors=("R",),
                mana_value=3,
                rarity="common",
                types=("Creature",),
                set_code="OTH",
            ),
        },
        image_uris_by_name={"requested card": "https://images.example.test/card.jpg"},
    )


def _ratings() -> SeventeenLandsFormatData:
    return SeventeenLandsFormatData(
        set_code="TST",
        event_format="quickdraft",
        fetched_at=NOW,
        card_ratings={
            1: SeventeenCardStats(
                grp_id=1,
                name="Requested Card",
                color="U",
                rarity="common",
                average_last_seen_at=3.5,
                gih_win_rate=0.60,
                opening_hand_win_rate=None,
                drawn_improvement_win_rate=None,
                sample_counts=RatingSampleCounts(
                    seen=100,
                    picked=50,
                    games_played=40,
                    opening_hand=20,
                    games_in_hand=10,
                ),
            )
        },
        pair_win_rates={
            "WU": ColorPairWinRate(pair="WU", wins=6, games=10, win_rate=0.60)
        },
    )


def _public_drafts(
    *,
    set_code: str = "TST",
    event_format: str = "QuickDraft",
) -> bytes:
    csv_text = (
        "draft_id,expansion,event_type,event_match_wins,pick,pick_maindeck_rate\n"
        f"draft-one,{set_code},{event_format},7,Requested Card,1.0\n"
    )
    return gzip.compress(data=csv_text.encode(encoding="utf-8"), mtime=0)


def _cache(tmp_path: Path, *, clock: FrozenClock) -> ProfileInputCache:
    return ProfileInputCache(
        tmp_path / "profile-input-cache",
        policy=ProfileInputCachePolicy(
            freshness_ttl=timedelta(hours=1),
            max_entry_bytes=100_000,
            max_total_bytes=300_000,
            max_records=10,
            max_versions_per_source=3,
        ),
        clock=clock,
    )


def _adapter(fetcher: StubCardDatabaseFetcher) -> CardMetadataAdapter:
    return CardMetadataAdapter(fetch_database=fetcher, timeout_seconds=17)


def _ratings_adapter(fetcher: StubRatingsFetcher) -> SeventeenLandsRatingsAdapter:
    return SeventeenLandsRatingsAdapter(fetch_ratings=fetcher, timeout_seconds=19)


def _public_draft_adapter(
    fetcher: StubPublicDraftFetcher,
) -> SeventeenLandsPublicDraftAdapter:
    return SeventeenLandsPublicDraftAdapter(
        fetch_public_drafts=fetcher,
        timeout_seconds=23,
    )


def _acquire(
    *,
    cache: ProfileInputCache,
    adapter: CardMetadataAdapter,
    clock: FrozenClock,
    event_format: str = "QuickDraft",
    offline: bool = False,
) -> ProfileInputAcquisitionResult:
    return acquire_card_metadata_bundle(
        environment=_environment(event_format=event_format),
        cache=cache,
        adapter=adapter,
        offline=offline,
        clock=clock,
    )

def _acquire_profile(
    *,
    cache: ProfileInputCache,
    card_adapter: CardMetadataAdapter,
    ratings_adapter: SeventeenLandsRatingsAdapter,
    public_draft_adapter: SeventeenLandsPublicDraftAdapter | None = None,
    clock: FrozenClock,
    offline: bool = False,
    include_public_drafts: bool = True,
) -> ProfileInputAcquisitionResult:
    return acquire_profile_build_bundle(
        environment=_environment(),
        cache=cache,
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=(
            public_draft_adapter
            or _public_draft_adapter(StubPublicDraftFetcher(_public_drafts()))
        ),
        offline=offline,
        clock=clock,
        include_public_drafts=include_public_drafts,
    )


def test_acquisition_builds_set_scoped_bundle_and_metadata_profile(tmp_path: Path) -> None:
    clock = FrozenClock()
    fetcher = StubCardDatabaseFetcher(_database())
    result = _acquire(
        cache=_cache(tmp_path, clock=clock),
        adapter=_adapter(fetcher),
        clock=clock,
    )

    assert result.succeeded
    assert result.source.outcome is ProfileInputAcquisitionOutcome.ACQUIRED
    assert result.source.cache_lookup_outcome is ProfileInputCacheOutcome.MISSING
    assert result.source.cache_store_outcome is ProfileInputCacheOutcome.FRESH
    assert result.source.acquired_at == NOW
    assert result.source.card_count == 1
    assert result.source.source_version == (
        f"v{CARD_METADATA_ADAPTER_VERSION}-20260831T120000000000Z-{result.source.sha256}"
    )
    assert fetcher.calls == [("TST", 17)]
    assert result.bundle is not None
    assert tuple(result.bundle.card_database.cards) == (1,)
    assert result.bundle.card_database.image_uris_by_name == {}

    generated = generate_set_profile(
        stage="metadata",
        generated_at=NOW,
        **result.bundle.generator_inputs(),
    )
    assert generated.profile.maturity is ProfileMaturity.METADATA_ONLY
    assert generated.profile.set_code == "tst"
    assert generated.profile.event_format == "quickdraft"

    repeated = _acquire(
        cache=_cache(tmp_path / "repeat", clock=clock),
        adapter=_adapter(StubCardDatabaseFetcher(_database())),
        clock=clock,
    )
    report = result.to_bytes().decode("utf-8")
    assert result.to_bytes() == repeated.to_bytes()
    assert str(tmp_path) not in report
    assert "Requested Card" not in report
    assert "Other Set Card" not in report
    assert "images.example.test" not in report


def test_fresh_cache_is_reused_without_fetching_again(tmp_path: Path) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    fetcher = StubCardDatabaseFetcher(_database())
    adapter = _adapter(fetcher)

    acquired = _acquire(cache=cache, adapter=adapter, clock=clock)
    cached = _acquire(cache=cache, adapter=adapter, clock=clock)

    assert acquired.source.outcome is ProfileInputAcquisitionOutcome.ACQUIRED
    assert cached.source.outcome is ProfileInputAcquisitionOutcome.CACHED
    assert cached.source.cache_lookup_outcome is ProfileInputCacheOutcome.FRESH
    assert cached.source.cache_store_outcome is None
    assert cached.source.source_version == acquired.source.source_version
    assert fetcher.calls == [("TST", 17)]


def test_offline_acquisition_reuses_verified_metadata_without_fetching(tmp_path: Path) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    fetcher = StubCardDatabaseFetcher(_database())
    adapter = _adapter(fetcher)
    _acquire(cache=cache, adapter=adapter, clock=clock)
    fetcher.error = RuntimeError("network must not be called")

    result = _acquire(
        cache=cache,
        adapter=adapter,
        offline=True,
        clock=clock,
    )

    assert result.succeeded
    assert result.source.outcome is ProfileInputAcquisitionOutcome.OFFLINE_REUSED
    assert result.source.cache_lookup_outcome is ProfileInputCacheOutcome.OFFLINE_REUSED
    assert fetcher.calls == [("TST", 17)]


def test_offline_cache_miss_is_explicit_and_does_not_fetch(tmp_path: Path) -> None:
    clock = FrozenClock()
    fetcher = StubCardDatabaseFetcher(_database())
    result = _acquire(
        cache=_cache(tmp_path, clock=clock),
        adapter=_adapter(fetcher),
        offline=True,
        clock=clock,
    )

    assert not result.succeeded
    assert result.bundle is None
    assert result.source.outcome is ProfileInputAcquisitionOutcome.MISSING
    assert result.source.cache_lookup_outcome is ProfileInputCacheOutcome.MISSING
    assert result.skip_reasons == ("card-metadata-cache-missing",)
    assert fetcher.calls == []


def test_stale_metadata_survives_failed_refresh_with_bounded_reason(tmp_path: Path) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    fetcher = StubCardDatabaseFetcher(_database())
    adapter = _adapter(fetcher)
    acquired = _acquire(cache=cache, adapter=adapter, clock=clock)
    clock.value = NOW + timedelta(hours=2)
    fetcher.error = RuntimeError("token=secret at /private/source")

    result = _acquire(cache=cache, adapter=adapter, clock=clock)

    assert result.succeeded
    assert result.source.outcome is ProfileInputAcquisitionOutcome.STALE
    assert result.source.cache_lookup_outcome is ProfileInputCacheOutcome.STALE
    assert result.source.source_version == acquired.source.source_version
    assert result.skip_reasons == ("card-metadata-refresh-failed",)
    assert result.source.diagnostics == ("card-metadata-acquisition-failed",)
    serialized = result.to_bytes().decode("utf-8")
    assert "secret" not in serialized
    assert "/private/source" not in serialized


def test_corrupt_cache_and_failed_refresh_return_path_free_failure(tmp_path: Path) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    fetcher = StubCardDatabaseFetcher(_database())
    adapter = _adapter(fetcher)
    _acquire(cache=cache, adapter=adapter, clock=clock)
    lookup = cache.lookup(source=adapter.source_for(environment=_environment()))
    assert lookup.content_path is not None
    lookup.content_path.write_bytes(b"corrupt")
    fetcher.error = RuntimeError(f"credential at {tmp_path}")

    result = _acquire(cache=cache, adapter=adapter, clock=clock)

    assert not result.succeeded
    assert result.source.outcome is ProfileInputAcquisitionOutcome.CORRUPT
    assert result.source.cache_lookup_outcome is ProfileInputCacheOutcome.CORRUPT
    assert result.skip_reasons == ("card-metadata-cache-corrupt",)
    serialized = result.to_bytes().decode("utf-8")
    assert str(tmp_path) not in serialized
    assert "credential" not in serialized


def test_unavailable_or_wrong_set_metadata_never_bypasses_cache(tmp_path: Path) -> None:
    clock = FrozenClock()
    fetcher = StubCardDatabaseFetcher(CardDatabase(cards={2: _database().cards[2]}))
    result = _acquire(
        cache=_cache(tmp_path, clock=clock),
        adapter=_adapter(fetcher),
        clock=clock,
    )

    assert not result.succeeded
    assert result.source.outcome is ProfileInputAcquisitionOutcome.UNAVAILABLE
    assert result.source.cache_lookup_outcome is ProfileInputCacheOutcome.MISSING
    assert result.skip_reasons == ("card-metadata-unavailable",)
    assert result.source.source_version is None
    assert result.source.card_count == 0


def test_cache_store_failure_does_not_return_uncached_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    fetcher = StubCardDatabaseFetcher(_database())

    def fail_store(**kwargs: object) -> None:
        del kwargs
        raise ProfileInputCacheCapacityError("injected capacity failure")

    monkeypatch.setattr(cache, "store", fail_store)
    result = _acquire(
        cache=cache,
        adapter=_adapter(fetcher),
        clock=clock,
    )

    assert not result.succeeded
    assert result.source.outcome is ProfileInputAcquisitionOutcome.UNAVAILABLE
    assert result.source.diagnostics == ("card-metadata-cache-store-failed",)
    assert result.skip_reasons == ("card-metadata-cache-store-failed",)


def test_card_metadata_cache_is_shared_across_formats_for_one_set(tmp_path: Path) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    fetcher = StubCardDatabaseFetcher(_database())
    adapter = _adapter(fetcher)
    _acquire(cache=cache, adapter=adapter, clock=clock)

    result = _acquire(
        cache=cache,
        adapter=adapter,
        clock=clock,
        event_format="PremierDraft",
    )

    assert result.succeeded
    assert result.source.outcome is ProfileInputAcquisitionOutcome.CACHED
    assert result.source.source.event_format is None
    assert result.bundle is not None
    assert result.bundle.environment.event_format == "premierdraft"
    assert fetcher.calls == [("TST", 17)]


def test_profile_bundle_acquires_ratings_and_generates_early_profile(tmp_path: Path) -> None:
    clock = FrozenClock()
    card_fetcher = StubCardDatabaseFetcher(_database())
    ratings_fetcher = StubRatingsFetcher(_ratings())
    public_draft_fetcher = StubPublicDraftFetcher(_public_drafts())
    result = _acquire_profile(
        cache=_cache(tmp_path, clock=clock),
        card_adapter=_adapter(card_fetcher),
        ratings_adapter=_ratings_adapter(ratings_fetcher),
        public_draft_adapter=_public_draft_adapter(public_draft_fetcher),
        clock=clock,
    )

    assert result.succeeded
    assert tuple(report.source.name for report in result.sources) == (
        "card-metadata",
        "17lands-ratings",
        "17lands-public-drafts",
    )
    assert result.ratings_source is not None
    assert result.ratings_source.outcome is ProfileInputAcquisitionOutcome.ACQUIRED
    assert result.ratings_source.cache_lookup_outcome is ProfileInputCacheOutcome.MISSING
    assert result.ratings_source.cache_store_outcome is ProfileInputCacheOutcome.FRESH
    assert result.ratings_source.rating_rows == 1
    assert result.ratings_source.rating_samples == 10
    assert result.ratings_source.to_json()["sample_availability"] == {
        "rating_rows": 1,
        "rating_samples": 10,
    }
    assert result.ratings_source.source_version == (
        f"v{RATINGS_ADAPTER_VERSION}-20260831T120000000000Z-"
        f"{result.ratings_source.sha256}"
    )
    assert ratings_fetcher.calls == [("TST", "quickdraft", NOW, 19)]
    assert result.public_draft_source is not None
    assert result.public_draft_source.outcome is ProfileInputAcquisitionOutcome.ACQUIRED
    assert result.public_draft_source.cache_lookup_outcome is ProfileInputCacheOutcome.MISSING
    assert result.public_draft_source.cache_store_outcome is ProfileInputCacheOutcome.FRESH
    assert result.public_draft_source.draft_rows == 1
    assert result.public_draft_source.to_json()["sample_availability"] == {
        "draft_rows": 1
    }
    assert result.public_draft_source.source_version == (
        f"v{PUBLIC_DRAFT_ADAPTER_VERSION}-20260831T120000000000Z-"
        f"{result.public_draft_source.sha256}"
    )
    assert len(public_draft_fetcher.calls) == 1
    assert public_draft_fetcher.calls[0][:2] == ("TST", "quickdraft")
    assert public_draft_fetcher.calls[0][3] == 23
    assert result.bundle is not None
    assert result.bundle.ratings == _ratings()
    assert result.bundle.public_drafts is not None

    generated = generate_set_profile(
        stage="early",
        generated_at=NOW,
        **result.bundle.generator_inputs(),
    )
    assert generated.profile.maturity is ProfileMaturity.EARLY
    assert len(generated.profile.card_ratings) == 1
    assert generated.profile.card_ratings[0].gih_win_rate.samples == 10
    assert generated.profile.pair("WU") is not None
    assert generated.profile.pair("WU").performance.samples == 10  # type: ignore[union-attr]
    assert tuple(source.name for source in generated.report.sources) == (
        "17lands-public-drafts",
    )
    assert generated.report.input_checksums["17lands-public-drafts"] == (
        result.public_draft_source.sha256
    )

    repeated = _acquire_profile(
        cache=_cache(tmp_path / "repeat", clock=clock),
        card_adapter=_adapter(StubCardDatabaseFetcher(_database())),
        ratings_adapter=_ratings_adapter(StubRatingsFetcher(_ratings())),
        clock=clock,
    )
    serialized = result.to_bytes().decode("utf-8")
    assert result.to_bytes() == repeated.to_bytes()
    assert str(tmp_path) not in serialized
    assert "Requested Card" not in serialized


def test_fresh_ratings_cache_is_reused_without_fetching_again(tmp_path: Path) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    card_fetcher = StubCardDatabaseFetcher(_database())
    ratings_fetcher = StubRatingsFetcher(_ratings())
    public_draft_fetcher = StubPublicDraftFetcher(_public_drafts())
    card_adapter = _adapter(card_fetcher)
    ratings_adapter = _ratings_adapter(ratings_fetcher)
    public_draft_adapter = _public_draft_adapter(public_draft_fetcher)

    acquired = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_draft_adapter,
        clock=clock,
    )
    cached = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_draft_adapter,
        clock=clock,
    )

    assert acquired.ratings_source is not None
    assert acquired.ratings_source.outcome is ProfileInputAcquisitionOutcome.ACQUIRED
    assert cached.ratings_source is not None
    assert cached.ratings_source.outcome is ProfileInputAcquisitionOutcome.CACHED
    assert cached.ratings_source.cache_lookup_outcome is ProfileInputCacheOutcome.FRESH
    assert cached.ratings_source.source_version == acquired.ratings_source.source_version
    assert acquired.public_draft_source is not None
    assert acquired.public_draft_source.outcome is ProfileInputAcquisitionOutcome.ACQUIRED
    assert cached.public_draft_source is not None
    assert cached.public_draft_source.outcome is ProfileInputAcquisitionOutcome.CACHED
    assert cached.public_draft_source.cache_lookup_outcome is ProfileInputCacheOutcome.FRESH
    assert cached.public_draft_source.source_version == (
        acquired.public_draft_source.source_version
    )
    assert card_fetcher.calls == [("TST", 17)]
    assert ratings_fetcher.calls == [("TST", "quickdraft", NOW, 19)]
    assert len(public_draft_fetcher.calls) == 1


def test_offline_profile_acquisition_reuses_verified_ratings(tmp_path: Path) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    card_fetcher = StubCardDatabaseFetcher(_database())
    ratings_fetcher = StubRatingsFetcher(_ratings())
    public_draft_fetcher = StubPublicDraftFetcher(_public_drafts())
    card_adapter = _adapter(card_fetcher)
    ratings_adapter = _ratings_adapter(ratings_fetcher)
    public_draft_adapter = _public_draft_adapter(public_draft_fetcher)
    _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_draft_adapter,
        clock=clock,
    )
    card_fetcher.error = RuntimeError("network must not be called")
    ratings_fetcher.error = RuntimeError("network must not be called")
    public_draft_fetcher.error = RuntimeError("network must not be called")

    result = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_draft_adapter,
        clock=clock,
        offline=True,
    )

    assert result.succeeded
    assert result.source.outcome is ProfileInputAcquisitionOutcome.OFFLINE_REUSED
    assert result.ratings_source is not None
    assert result.ratings_source.outcome is ProfileInputAcquisitionOutcome.OFFLINE_REUSED
    assert result.public_draft_source is not None
    assert result.public_draft_source.outcome is (
        ProfileInputAcquisitionOutcome.OFFLINE_REUSED
    )
    assert result.public_draft_source.draft_rows == 1
    assert result.bundle is not None
    assert result.bundle.ratings == _ratings()
    assert result.bundle.public_drafts is not None
    assert card_fetcher.calls == [("TST", 17)]
    assert ratings_fetcher.calls == [("TST", "quickdraft", NOW, 19)]
    assert len(public_draft_fetcher.calls) == 1


def test_missing_offline_empirical_sources_preserve_metadata_only_bundle(
    tmp_path: Path,
) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    card_fetcher = StubCardDatabaseFetcher(_database())
    card_adapter = _adapter(card_fetcher)
    _acquire(cache=cache, adapter=card_adapter, clock=clock)
    ratings_fetcher = StubRatingsFetcher(_ratings())

    result = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=_ratings_adapter(ratings_fetcher),
        clock=clock,
        offline=True,
    )

    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.ratings is None
    assert "ratings" not in result.bundle.generator_inputs()
    assert result.bundle.public_drafts is None
    assert "source_manifest" not in result.bundle.generator_inputs()
    assert result.ratings_source is not None
    assert result.ratings_source.outcome is ProfileInputAcquisitionOutcome.MISSING
    assert result.ratings_source.rating_rows == 0
    assert result.ratings_source.rating_samples == 0
    assert result.public_draft_source is not None
    assert result.public_draft_source.outcome is ProfileInputAcquisitionOutcome.MISSING
    assert result.public_draft_source.draft_rows == 0
    assert result.skip_reasons == (
        "17lands-public-drafts-cache-missing",
        "17lands-ratings-cache-missing",
    )
    assert ratings_fetcher.calls == []


def test_stale_ratings_survive_failed_refresh_with_bounded_reason(tmp_path: Path) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    card_adapter = _adapter(StubCardDatabaseFetcher(_database()))
    endpoint_calls: list[str] = []
    fixture_dir = Path(__file__).parent / "fixtures"
    card_payload = json.loads(
        (fixture_dir / "17lands-card-ratings-quick.json").read_text()
    )
    color_payload = json.loads(
        (fixture_dir / "17lands-color-ratings.json").read_text()
    )

    def fetch_ratings(
        *,
        set_code: str,
        event_format: str,
        fetched_at: datetime,
        timeout_seconds: int,
    ) -> SeventeenLandsFormatData:
        def fetch_json(url: str, timeout: int) -> object:
            assert timeout == timeout_seconds
            endpoint_calls.append(url)
            if len(endpoint_calls) == 4:
                raise RuntimeError("token=secret at /private/ratings")
            return card_payload if len(endpoint_calls) % 2 else color_payload

        return fetch_17lands_format_data(
            set_code=set_code,
            event_format=event_format,
            fetched_at=fetched_at,
            fetch_json=fetch_json,
            timeout_seconds=timeout_seconds,
        )

    ratings_adapter = SeventeenLandsRatingsAdapter(
        fetch_ratings=fetch_ratings,
        timeout_seconds=19,
    )
    acquired = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        clock=clock,
    )
    assert acquired.ratings_source is not None
    cached = cache.lookup(source=ratings_adapter.source_for(environment=_environment()))
    assert cached.content_path is not None
    cached_bytes = cached.content_path.read_bytes()
    clock.value = NOW + timedelta(hours=2)

    result = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        clock=clock,
    )

    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.ratings == acquired.bundle.ratings
    assert result.ratings_source is not None
    assert result.ratings_source.outcome is ProfileInputAcquisitionOutcome.STALE
    assert result.ratings_source.source_version == acquired.ratings_source.source_version
    assert result.ratings_source.sha256 == acquired.ratings_source.sha256
    assert result.ratings_source.content_bytes == acquired.ratings_source.content_bytes
    assert result.ratings_source.acquired_at == acquired.ratings_source.acquired_at
    refreshed_cache = cache.lookup(
        source=ratings_adapter.source_for(environment=_environment())
    )
    assert refreshed_cache.content_path is not None
    assert refreshed_cache.content_path.read_bytes() == cached_bytes
    assert len(endpoint_calls) == 4
    assert result.skip_reasons == ("17lands-ratings-refresh-failed",)
    serialized = result.to_bytes().decode("utf-8")
    assert "secret" not in serialized
    assert "/private/ratings" not in serialized


def test_corrupt_ratings_preserve_metadata_bundle_and_report_failed_source(
    tmp_path: Path,
) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    card_adapter = _adapter(StubCardDatabaseFetcher(_database()))
    ratings_fetcher = StubRatingsFetcher(_ratings())
    ratings_adapter = _ratings_adapter(ratings_fetcher)
    _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        clock=clock,
    )
    lookup = cache.lookup(source=ratings_adapter.source_for(environment=_environment()))
    assert lookup.content_path is not None
    lookup.content_path.write_bytes(b"corrupt")
    ratings_fetcher.error = RuntimeError(f"credential at {tmp_path}")

    result = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        clock=clock,
    )

    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.ratings is None
    assert result.ratings_source is not None
    assert result.ratings_source.source.name == "17lands-ratings"
    assert result.ratings_source.outcome is ProfileInputAcquisitionOutcome.CORRUPT
    assert result.skip_reasons == ("17lands-ratings-cache-corrupt",)
    serialized = result.to_bytes().decode("utf-8")
    assert str(tmp_path) not in serialized
    assert "credential" not in serialized
    assert "Requested Card" not in serialized


def test_mismatched_ratings_preserve_metadata_bundle_as_unavailable(tmp_path: Path) -> None:
    clock = FrozenClock()
    ratings_fetcher = StubRatingsFetcher(replace(_ratings(), event_format="PremierDraft"))
    result = _acquire_profile(
        cache=_cache(tmp_path, clock=clock),
        card_adapter=_adapter(StubCardDatabaseFetcher(_database())),
        ratings_adapter=_ratings_adapter(ratings_fetcher),
        clock=clock,
    )

    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.ratings is None
    assert result.bundle.public_drafts is not None
    assert result.ratings_source is not None
    assert result.ratings_source.outcome is ProfileInputAcquisitionOutcome.UNAVAILABLE
    assert result.ratings_source.source_version is None
    assert result.ratings_source.rating_rows == 0
    assert result.ratings_source.rating_samples == 0
    assert result.public_draft_source is not None
    assert result.public_draft_source.outcome is ProfileInputAcquisitionOutcome.ACQUIRED
    assert result.public_draft_source.draft_rows == 1
    assert result.skip_reasons == ("17lands-ratings-unavailable",)


def test_stale_public_drafts_survive_failed_refresh_with_bounded_reason(
    tmp_path: Path,
) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    card_adapter = _adapter(StubCardDatabaseFetcher(_database()))
    ratings_fetcher = StubRatingsFetcher(_ratings())
    ratings_adapter = _ratings_adapter(ratings_fetcher)
    public_draft_fetcher = StubPublicDraftFetcher(_public_drafts())
    public_draft_adapter = _public_draft_adapter(public_draft_fetcher)
    acquired = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_draft_adapter,
        clock=clock,
    )
    clock.value = NOW + timedelta(hours=2)
    ratings_fetcher.ratings = replace(_ratings(), fetched_at=clock.value)
    public_draft_fetcher.error = RuntimeError("token=secret at /private/drafts")

    result = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_draft_adapter,
        clock=clock,
    )

    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.public_drafts is not None
    assert result.public_draft_source is not None
    assert result.public_draft_source.outcome is ProfileInputAcquisitionOutcome.STALE
    assert acquired.public_draft_source is not None
    assert result.public_draft_source.source_version == (
        acquired.public_draft_source.source_version
    )
    assert result.public_draft_source.draft_rows == 1
    assert result.skip_reasons == ("17lands-public-drafts-refresh-failed",)
    serialized = result.to_bytes().decode("utf-8")
    assert "secret" not in serialized
    assert "/private/drafts" not in serialized


def test_corrupt_public_drafts_preserve_ratings_and_report_failed_source(
    tmp_path: Path,
) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    card_adapter = _adapter(StubCardDatabaseFetcher(_database()))
    ratings_adapter = _ratings_adapter(StubRatingsFetcher(_ratings()))
    public_draft_fetcher = StubPublicDraftFetcher(_public_drafts())
    public_draft_adapter = _public_draft_adapter(public_draft_fetcher)
    _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_draft_adapter,
        clock=clock,
    )
    lookup = cache.lookup(
        source=public_draft_adapter.source_for(environment=_environment())
    )
    assert lookup.content_path is not None
    lookup.content_path.write_bytes(b"private-corrupt-row")
    public_draft_fetcher.error = RuntimeError(f"credential at {tmp_path}")

    result = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_draft_adapter,
        clock=clock,
    )

    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.ratings == _ratings()
    assert result.bundle.public_drafts is None
    assert result.public_draft_source is not None
    assert result.public_draft_source.source.name == "17lands-public-drafts"
    assert result.public_draft_source.outcome is ProfileInputAcquisitionOutcome.CORRUPT
    assert result.public_draft_source.draft_rows == 0
    assert result.skip_reasons == ("17lands-public-drafts-cache-corrupt",)
    serialized = result.to_bytes().decode("utf-8")
    assert str(tmp_path) not in serialized
    assert "credential" not in serialized
    assert "private-corrupt-row" not in serialized


def test_mismatched_public_drafts_preserve_ratings_as_unavailable(tmp_path: Path) -> None:
    clock = FrozenClock()
    public_draft_fetcher = StubPublicDraftFetcher(
        _public_drafts(set_code="OTH")
    )
    result = _acquire_profile(
        cache=_cache(tmp_path, clock=clock),
        card_adapter=_adapter(StubCardDatabaseFetcher(_database())),
        ratings_adapter=_ratings_adapter(StubRatingsFetcher(_ratings())),
        public_draft_adapter=_public_draft_adapter(public_draft_fetcher),
        clock=clock,
    )

    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.ratings == _ratings()
    assert result.bundle.public_drafts is None
    assert result.public_draft_source is not None
    assert result.public_draft_source.outcome is ProfileInputAcquisitionOutcome.UNAVAILABLE
    assert result.public_draft_source.source_version is None
    assert result.public_draft_source.draft_rows == 0
    assert result.skip_reasons == ("17lands-public-drafts-unavailable",)
    serialized = result.to_bytes().decode("utf-8")
    assert "OTH" not in serialized
    assert "draft-one" not in serialized

@pytest.mark.parametrize(
    "cached_ratings",
    (
        pytest.param(replace(_ratings(), set_code="OTH"), id="set"),
        pytest.param(replace(_ratings(), event_format="premierdraft"), id="format"),
    ),
)
def test_mismatched_cached_ratings_never_become_empirical_evidence(
    tmp_path: Path,
    cached_ratings: SeventeenLandsFormatData,
) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    ratings_fetcher = StubRatingsFetcher(_ratings())
    ratings_fetcher.error = RuntimeError("deterministic live fetch failure")
    ratings_adapter = _ratings_adapter(ratings_fetcher)
    cache.store(
        source=ratings_adapter.source_for(environment=_environment()),
        source_version="mismatched-v1",
        input_stream=BytesIO(
            (
                json.dumps(
                    cached_ratings.to_json(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        ),
        acquired_at=NOW,
    )

    result = _acquire_profile(
        cache=cache,
        card_adapter=_adapter(StubCardDatabaseFetcher(_database())),
        ratings_adapter=ratings_adapter,
        clock=clock,
        include_public_drafts=False,
    )

    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.ratings is None
    assert result.ratings_source is not None
    assert result.ratings_source.outcome is ProfileInputAcquisitionOutcome.CORRUPT
    assert result.ratings_source.sha256 is None
    assert result.ratings_source.acquired_at is None



def test_aggregate_only_skips_public_draft_adapter_and_report(
    tmp_path: Path,
) -> None:
    clock = FrozenClock()
    cache = _cache(tmp_path, clock=clock)
    card_fetcher = StubCardDatabaseFetcher(_database())
    ratings_fetcher = StubRatingsFetcher(_ratings())
    public_draft_fetcher = StubPublicDraftFetcher(_public_drafts())
    public_source_calls: list[str] = []

    class FailOnPublicDraftSource(SeventeenLandsPublicDraftAdapter):
        def source_for(self, **_: object):
            public_source_calls.append("source")
            raise AssertionError("public drafts must not be touched")

    card_adapter = _adapter(card_fetcher)
    ratings_adapter = _ratings_adapter(ratings_fetcher)
    public_adapter = FailOnPublicDraftSource(
        fetch_public_drafts=public_draft_fetcher,
        timeout_seconds=23,
    )
    result = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
        clock=clock,
        include_public_drafts=False,
    )
    offline = _acquire_profile(
        cache=cache,
        card_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
        clock=clock,
        offline=True,
        include_public_drafts=False,
    )
    assert result.succeeded
    assert result.bundle is not None
    assert result.bundle.ratings == _ratings()
    assert result.bundle.public_drafts is None
    assert result.bundle.public_draft_source is None
    assert result.public_draft_source is None
    assert tuple(report.source.name for report in result.sources) == (
        "card-metadata",
        "17lands-ratings",
    )
    assert not any("public-drafts" in reason for reason in result.skip_reasons)

    assert public_draft_fetcher.calls == []
    assert public_source_calls == []
    assert len(card_fetcher.calls) == 1
    assert len(ratings_fetcher.calls) == 1
    assert offline.succeeded
    assert offline.ratings_source is not None
    assert offline.ratings_source.outcome is ProfileInputAcquisitionOutcome.OFFLINE_REUSED
    assert offline.public_draft_source is None
    assert offline.bundle is not None
    assert offline.bundle.public_drafts is None
    assert not any("public-drafts" in reason for reason in offline.skip_reasons)


def test_include_public_drafts_must_be_bool_before_acquisition(tmp_path: Path) -> None:
    card_fetcher = StubCardDatabaseFetcher(_database())
    with pytest.raises(ProfileInputAcquisitionError, match="include_public_drafts"):
        _acquire_profile(
            cache=_cache(tmp_path, clock=FrozenClock()),
            card_adapter=_adapter(card_fetcher),
            ratings_adapter=_ratings_adapter(StubRatingsFetcher(_ratings())),
            clock=FrozenClock(),
            include_public_drafts=1,  # type: ignore[arg-type]
        )
    assert card_fetcher.calls == []
