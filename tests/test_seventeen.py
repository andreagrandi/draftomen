from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from draftomen.carddb import (
    CardDatabase,
    CardInfo,
    CardMetadataSeed,
    card_database_cache_path,
    load_card_database,
)
from draftomen.config import COLOR_PAIRS, PICK_ENGINE
from draftomen.seventeen import (
    card_ratings_url,
    NEUTRAL_PRIOR_SOURCE,
    PREMIER_DRAFT_FORMAT,
    QUICK_DRAFT_FORMAT,
    SEVENTEEN_LANDS_ATTRIBUTION,
    SEVENTEEN_LANDS_EXPANSIONS_ENDPOINT,
    SeventeenLandsDownloadProgress,
    SeventeenLandsError,
    fetch_17lands_expansion_inventory,
    parse_17lands_expansion_inventory,
    build_17lands_structure_targets_from_draft_rows,
    augment_card_database_from_ratings,
    has_cached_17lands_data,
    load_17lands_structure_targets,
    load_17lands_format_data,
    load_cached_17lands_data,
    load_or_refresh_17lands_data,
    load_or_refresh_17lands_format_data,
    metadata_augmenting_ratings_progress_loader,
    save_17lands_structure_targets,
    seventeen_lands_cache_path,
    seventeen_lands_pair_card_cache_path,
    seventeen_lands_structure_targets_cache_path,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
QUICK_CARD_RATINGS_PATH = FIXTURE_DIR / "17lands-card-ratings-quick.json"
PREMIER_CARD_RATINGS_PATH = FIXTURE_DIR / "17lands-card-ratings-premier.json"
COLOR_RATINGS_PATH = FIXTURE_DIR / "17lands-color-ratings.json"
PROFILE_GENERATION_RATINGS_PATH = FIXTURE_DIR / "profile-generation" / "ratings.json"


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingFetcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.urls: list[str] = []
        self.quick_card_ratings = _load_json(path=QUICK_CARD_RATINGS_PATH)
        self.premier_card_ratings = _load_json(path=PREMIER_CARD_RATINGS_PATH)
        self.pair_card_ratings = [_pair_card_rating_row()]
        self.color_ratings = _load_json(path=COLOR_RATINGS_PATH)

    def __call__(self, url: str, timeout_seconds: int) -> Any:
        self.urls.append(url)
        if self.fail:
            raise SeventeenLandsError("network unavailable")

        if "/api/card_data" in url and "colors=WU" in url:
            return {"data": self.pair_card_ratings}

        if "/api/card_data" in url and "event_type=PremierDraft" in url:
            return {"data": self.premier_card_ratings}

        if "/api/card_data" in url:
            return {"data": self.quick_card_ratings}

        if "/color_ratings/data" in url:
            return self.color_ratings

        raise AssertionError(f"unexpected URL {url}")


def test_card_ratings_url_canonicalizes_hosted_event_formats() -> None:
    for event_format, expected_format in (
        ("premierdraft", "PremierDraft"),
        ("traddraft", "TradDraft"),
        ("quickdraft", "QuickDraft"),
        ("picktwodraft", "PickTwoDraft"),
    ):
        assert card_ratings_url(set_code="tst", event_format=event_format) == (
            "https://api.17lands.com/api/card_data?"
            f"expansion=TST&event_type={expected_format}&time_period=ALL_TIME"
        )

    assert card_ratings_url(
        set_code="tst",
        event_format="quickdraft",
        colors="WU",
    ) == (
        "https://api.17lands.com/api/card_data?"
        "expansion=TST&event_type=QuickDraft&time_period=ALL_TIME&colors=WU"
    )
    assert card_ratings_url(set_code="tst", event_format="customdraft") == (
        "https://api.17lands.com/api/card_data?"
        "expansion=TST&event_type=customdraft&time_period=ALL_TIME"
    )


def test_17lands_expansion_inventory_normalizes_and_keeps_valid_neighbors() -> None:
    result = parse_17lands_expansion_inventory(
        [" tst ", "NEW", "", None, "TST", {"code": "BAD"}],
        source_url="https://fixture.invalid/expansions",
    )

    assert result.expansion_codes == ("NEW", "TST")
    assert result.source_url == "https://fixture.invalid/expansions"
    assert [(item.reason, item.entry) for item in result.diagnostics] == [
        ("duplicate-entry", "TST"),
        ("malformed-entry", ""),
        ("malformed-entry", None),
        ("malformed-entry", None),
    ]


def test_17lands_expansion_inventory_is_stable_when_payload_order_changes() -> None:
    payload = ["Y26ECL", " ecl ", "", None, "Y26ECL"]

    first = parse_17lands_expansion_inventory(payload)
    second = parse_17lands_expansion_inventory(list(reversed(payload)))

    assert second == first


def test_17lands_expansion_inventory_digest_changes_with_source_payload() -> None:
    first = parse_17lands_expansion_inventory(["TST"])
    changed = parse_17lands_expansion_inventory(["TST", "NEW"])

    assert changed.source_payload_digest != first.source_payload_digest


def test_fetch_17lands_expansion_inventory_uses_injected_fetcher() -> None:
    calls: list[tuple[str, int]] = []

    def fetcher(url: str, timeout_seconds: int) -> list[str]:
        calls.append((url, timeout_seconds))
        return [" tst ", "NEW"]

    result = fetch_17lands_expansion_inventory(fetch_json=fetcher, timeout_seconds=7)

    assert calls == [(SEVENTEEN_LANDS_EXPANSIONS_ENDPOINT, 7)]
    assert result.expansion_codes == ("NEW", "TST")
    assert result.source_url == SEVENTEEN_LANDS_EXPANSIONS_ENDPOINT


def test_parse_17lands_expansion_inventory_rejects_non_list_payload() -> None:
    with pytest.raises(SeventeenLandsError, match="expected a JSON list"):
        parse_17lands_expansion_inventory({"expansions": ["TST"]})


def test_17lands_format_data_is_cached_and_not_refetched_within_24h(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC))
    fetcher = RecordingFetcher()

    first = load_or_refresh_17lands_format_data(
        set_code="tst",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=clock,
        fetch_json=fetcher,
    )
    clock.now = clock.now + timedelta(hours=23, minutes=59)
    second = load_or_refresh_17lands_format_data(
        set_code="tst",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=clock,
        fetch_json=fetcher,
    )

    assert len(fetcher.urls) == 2
    assert fetcher.urls[0].startswith("https://api.17lands.com/api/card_data?")
    assert "time_period=ALL_TIME" in fetcher.urls[0]
    assert "start_date" not in fetcher.urls[0]
    assert second.fetched_at == first.fetched_at
    assert second.card_ratings[1001].gih_win_rate == 0.61
    assert seventeen_lands_cache_path(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
    ).exists()


def test_explicit_ratings_loader_accepts_case_insensitive_format() -> None:
    dataset = load_17lands_format_data(
        set_code="tst",
        event_format="quickdraft",
        cache_path=PROFILE_GENERATION_RATINGS_PATH,
    )

    assert dataset.event_format == "QuickDraft"


def test_explicit_ratings_loader_rejects_a_different_format() -> None:
    with pytest.raises(
        SeventeenLandsError,
        match="17Lands cache .* is for format QuickDraft, not PremierDraft.",
    ):
        load_17lands_format_data(
            set_code="TST",
            event_format="PremierDraft",
            cache_path=PROFILE_GENERATION_RATINGS_PATH,
        )


def test_17lands_refresh_refetches_fresh_cache(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC))
    fetcher = RecordingFetcher()

    load_or_refresh_17lands_data(
        set_code="TST",
        app_dir=tmp_path,
        clock=clock,
        fetch_json=fetcher,
    )
    fetcher.urls.clear()

    load_or_refresh_17lands_data(
        set_code="TST",
        app_dir=tmp_path,
        clock=clock,
        fetch_json=fetcher,
        refresh=True,
    )

    assert len(fetcher.urls) == 4


def test_first_17lands_download_reports_request_progress_and_builds_cache(
    tmp_path: Path,
) -> None:
    progress: list[SeventeenLandsDownloadProgress] = []

    assert not has_cached_17lands_data(set_code="TST", app_dir=tmp_path)

    load_or_refresh_17lands_data(
        set_code="TST",
        app_dir=tmp_path,
        clock=FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
        fetch_json=RecordingFetcher(),
        progress_callback=progress.append,
    )

    assert has_cached_17lands_data(set_code="TST", app_dir=tmp_path)
    assert [
        (update.completed_requests, update.total_requests)
        for update in progress
    ] == [
        (0, 4),
        (1, 4),
        (2, 4),
        (3, 4),
        (4, 4),
    ]
    assert progress[-1].message == "Downloaded all-time PremierDraft color ratings"


def test_legacy_date_range_cache_is_replaced_with_all_time_data(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC))
    load_or_refresh_17lands_format_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=clock,
        fetch_json=RecordingFetcher(),
    )
    cache_path = seventeen_lands_cache_path(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
    )
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    replacement_fetcher = RecordingFetcher()

    refreshed = load_or_refresh_17lands_format_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=clock,
        fetch_json=replacement_fetcher,
    )

    assert len(replacement_fetcher.urls) == 2
    assert refreshed.card_ratings[1001].gih_win_rate == 0.61
    refreshed_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert refreshed_payload["schema_version"] == 2


def test_stale_17lands_cache_serves_last_good_data_when_offline(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC))
    first = load_or_refresh_17lands_format_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=clock,
        fetch_json=RecordingFetcher(),
    )
    clock.now = clock.now + timedelta(hours=25)
    offline_fetcher = RecordingFetcher(fail=True)

    stale = load_or_refresh_17lands_format_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=clock,
        fetch_json=offline_fetcher,
    )

    assert len(offline_fetcher.urls) == 1
    assert stale.fetched_at == first.fetched_at
    assert stale.card_ratings[1001].name == "Fixture Quick Bomb"


def test_quickdraft_ratings_fall_back_to_premier_then_neutral_prior(
    tmp_path: Path,
) -> None:
    data = load_or_refresh_17lands_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
        fetch_json=RecordingFetcher(),
        thin_sample_minimum=500,
    )

    quick_rating = data.rating_for(grp_id=1001)
    thin_fallback = data.rating_for(grp_id=1002)
    missing_fallback = data.rating_for(grp_id=1003)
    neutral = data.rating_for(grp_id=9999)

    assert quick_rating.gih_win_rate == 0.61
    assert quick_rating.letter_grade == "C"
    assert quick_rating.metadata.source_format == QUICK_DRAFT_FORMAT
    assert quick_rating.metadata.fallback_reason is None

    assert thin_fallback.gih_win_rate == 0.55
    assert thin_fallback.letter_grade == "D"
    assert thin_fallback.metadata.source_format == PREMIER_DRAFT_FORMAT
    assert thin_fallback.metadata.fallback_reason == "primary-thin"

    assert missing_fallback.name == "Fixture Premier Only Card"
    assert missing_fallback.letter_grade == "B"
    assert missing_fallback.metadata.source_format == PREMIER_DRAFT_FORMAT
    assert missing_fallback.metadata.fallback_reason == "primary-missing"

    assert neutral.neutral_prior is True
    assert neutral.metadata.source == NEUTRAL_PRIOR_SOURCE
    assert neutral.metadata.fallback_reason == "fallback-missing"
    assert neutral.neutral_prior_score == PICK_ENGINE.neutral_prior_score
    assert data.attribution == SEVENTEEN_LANDS_ATTRIBUTION


def test_set_reliability_is_one_aggregate_quick_and_premier_value(
    tmp_path: Path,
) -> None:
    data = load_or_refresh_17lands_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
        fetch_json=RecordingFetcher(),
        thin_sample_minimum=500,
    )

    reliability = data.set_reliability

    assert reliability.set_code == "TST"
    assert reliability.score == 43
    assert reliability.tier == "Low"


def test_cached_17lands_data_uses_empty_primary_when_cache_is_missing(
    tmp_path: Path,
) -> None:
    data = load_cached_17lands_data(set_code="TST", app_dir=tmp_path)

    rating = data.rating_for(grp_id=9999)

    assert data.primary.card_ratings == {}
    assert data.fallback is None
    assert data.set_reliability.score == 0
    assert data.set_reliability.tier == "Very low"
    assert rating.neutral_prior is True
    assert rating.metadata.source == NEUTRAL_PRIOR_SOURCE


def test_schema_three_ratings_wrapper_skips_mtgjson_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratings_data = load_or_refresh_17lands_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path / "ratings",
        clock=FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
        fetch_json=RecordingFetcher(),
        thin_sample_minimum=500,
    )
    ratings_data = replace(
        ratings_data,
        fallback=None,
        primary=replace(
            ratings_data.primary,
            card_ratings={1001: ratings_data.primary.card_ratings[1001]},
        ),
    )

    cache_path = card_database_cache_path(app_dir=tmp_path / "cards")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "source": "scryfall-default-cards",
                "generated_at": None,
                "cards": {
                    "1001": {
                        "grp_id": 1001,
                        "name": "Legacy Card",
                        "colors": ["W"],
                        "mana_value": 2,
                        "rarity": "common",
                        "types": ["Creature"],
                    }
                },
                "image_uris_by_name": {},
            }
        ),
        encoding="utf-8",
    )
    database = load_card_database(app_dir=tmp_path / "cards")

    def fail_mtgjson_download(**_kwargs: object) -> tuple[object, ...]:
        pytest.fail("resolved schema-3 cards must not download MTGJSON")

    monkeypatch.setattr(
        "draftomen.carddb.download_mtgjson_set_cards",
        fail_mtgjson_download,
    )
    loader = metadata_augmenting_ratings_progress_loader(
        database=database,
        load_ratings=lambda set_code, progress_callback, *, refresh: ratings_data,
        app_dir=tmp_path / "persist",
        persist_database=False,
    )

    loaded = loader("TST", lambda progress: None, refresh=False)

    assert loaded is ratings_data
    assert database.lookup(grp_id=1001).source_provenance == ("unknown",)


def test_progress_loader_recovers_and_persists_current_set_card_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratings_data = load_or_refresh_17lands_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path / "ratings",
        clock=FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
        fetch_json=RecordingFetcher(),
        thin_sample_minimum=500,
    )
    database = CardDatabase(cards={})
    recovered_card = CardInfo(
        grp_id=1001,
        name="Fixture Quick Bomb",
        colors=("W",),
        mana_value=2.0,
        rarity="rare",
        types=("Creature",),
    )
    captured_seeds: list[tuple[int, str]] = []
    refresh_values: list[bool] = []

    def recover_metadata(
        base: CardDatabase,
        *,
        set_code: str,
        seeds: Iterable[CardMetadataSeed],
    ) -> CardDatabase:
        assert base is database
        assert set_code == "TST"
        captured_seeds.extend((seed.grp_id, seed.name) for seed in seeds)
        return CardDatabase(cards={recovered_card.grp_id: recovered_card})

    monkeypatch.setattr(
        "draftomen.seventeen.augment_card_database_with_mtgjson_set",
        recover_metadata,
    )
    app_dir = tmp_path / "app"
    loader = metadata_augmenting_ratings_progress_loader(
        database=database,
        load_ratings=lambda set_code, progress_callback, *, refresh: (
            refresh_values.append(refresh) or ratings_data
        ),
        app_dir=app_dir,
    )

    loaded = loader("TST", lambda progress: None, refresh=True)

    assert loaded is ratings_data
    assert refresh_values == [True]
    assert (1001, "Fixture Quick Bomb") in captured_seeds
    assert database.lookup(grp_id=1001) == recovered_card
    assert load_card_database(app_dir=app_dir).lookup(grp_id=1001) == recovered_card


def test_ratings_metadata_noop_preserves_cards_without_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ratings_data = load_or_refresh_17lands_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path / "ratings",
        clock=FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
        fetch_json=RecordingFetcher(),
        thin_sample_minimum=500,
    )
    existing_cards = {
        1001: CardInfo(
            grp_id=1001,
            name="Existing Card",
            colors=("W",),
            mana_value=2.0,
            rarity="common",
            types=("Creature",),
        ),
        2001: CardInfo(
            grp_id=2001,
            name="Another Existing Card",
            colors=("U",),
            mana_value=3.0,
            rarity="uncommon",
            types=("Instant",),
        ),
    }
    database = CardDatabase(cards=existing_cards.copy())
    save_calls: list[tuple[object, object]] = []

    def return_same_database(
        base: CardDatabase,
        *,
        set_code: str,
        seeds: Iterable[CardMetadataSeed],
    ) -> CardDatabase:
        assert base is database
        assert set_code == "TST"
        assert tuple(seeds)
        return base

    monkeypatch.setattr(
        "draftomen.seventeen.augment_card_database_with_mtgjson_set",
        return_same_database,
    )
    monkeypatch.setattr(
        "draftomen.seventeen.save_card_database",
        lambda *args, **kwargs: save_calls.append((args, kwargs)),
    )

    augment_card_database_from_ratings(
        database=database,
        set_code="TST",
        ratings_data=ratings_data,
        app_dir=tmp_path / "app",
    )

    assert database.cards == existing_cards
    assert save_calls == []


def test_pair_win_rates_are_available_for_all_ten_pairs(tmp_path: Path) -> None:
    dataset = load_or_refresh_17lands_format_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
        fetch_json=RecordingFetcher(),
    )

    assert set(dataset.pair_win_rates) == set(COLOR_PAIRS)
    assert dataset.pair_win_rates["WU"].wins == 60
    assert dataset.pair_win_rates["WU"].games == 100
    assert dataset.pair_win_rates["WU"].win_rate == 0.6
    assert dataset.attribution == SEVENTEEN_LANDS_ATTRIBUTION


def test_pair_filtered_card_ratings_are_loaded_lazily_and_cached(
    tmp_path: Path,
) -> None:
    fetcher = RecordingFetcher()
    data = load_or_refresh_17lands_data(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
        clock=FrozenClock(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
        fetch_json=fetcher,
        thin_sample_minimum=500,
    )

    assert len(fetcher.urls) == 4

    pair_rating = data.pair_rating_for(grp_id=1002, pair="WU")
    second_pair_rating = data.pair_rating_for(grp_id=1002, pair="WU")
    cached_data = load_cached_17lands_data(set_code="TST", app_dir=tmp_path)
    cached_pair_rating = cached_data.pair_rating_for(grp_id=1002, pair="WU")

    assert pair_rating.gih_win_rate == 0.66
    assert second_pair_rating.gih_win_rate == 0.66
    assert cached_pair_rating.gih_win_rate == 0.66
    assert "WU" in data.pair_card_ratings
    assert len(fetcher.urls) == 5
    assert any("colors=WU" in url for url in fetcher.urls)
    assert seventeen_lands_pair_card_cache_path(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        pair="WU",
        app_dir=tmp_path,
    ).exists()


def test_structure_targets_are_computed_cached_and_loaded_with_ratings(
    tmp_path: Path,
) -> None:
    computed_at = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    targets = build_17lands_structure_targets_from_draft_rows(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        card_database=_structure_card_database(),
        rows=_structure_rows(),
        source_url="https://17lands-public.example/draft.csv.gz",
        computed_at=computed_at,
    )

    save_17lands_structure_targets(targets, app_dir=tmp_path)
    loaded_targets = load_17lands_structure_targets(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
    )
    data = load_cached_17lands_data(set_code="TST", app_dir=tmp_path)
    wu_targets = data.structure_targets_for(pair="WU")

    assert loaded_targets.total_decks == 2
    assert set(loaded_targets.targets) == {"WU"}
    assert wu_targets is not None
    assert wu_targets.sample_size == 2
    assert wu_targets.average_creature_count == 15.0
    assert wu_targets.average_land_count == 16.5
    assert wu_targets.average_two_drop_count == 6.0
    assert seventeen_lands_structure_targets_cache_path(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=tmp_path,
    ).exists()


def _load_json(*, path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _structure_card_database() -> CardDatabase:
    cards: dict[int, CardInfo] = {}
    for offset in range(16):
        grp_id = 2000 + offset
        cards[grp_id] = _structure_card(
            grp_id=grp_id,
            name=f"Structure Creature {offset}",
            colors=("W",) if offset % 2 == 0 else ("U",),
            mana_value=2.0 if offset < 6 else 3.0,
            types=("Creature — Fixture",),
        )

    for offset in range(8):
        grp_id = 2100 + offset
        cards[grp_id] = _structure_card(
            grp_id=grp_id,
            name=f"Structure Spell {offset}",
            colors=("W",) if offset % 2 == 0 else ("U",),
            mana_value=3.0,
            types=("Instant",),
        )

    cards[2200] = _structure_card(
        grp_id=2200,
        name="Ignored Red Card",
        colors=("R",),
        mana_value=2.0,
        types=("Creature — Fixture",),
    )
    return CardDatabase(cards=cards)


def _structure_card(
    *,
    grp_id: int,
    name: str,
    colors: tuple[str, ...],
    mana_value: float,
    types: tuple[str, ...],
) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=colors,
        mana_value=mana_value,
        rarity="common",
        types=types,
    )


def _structure_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    rows.extend(
        _structure_deck_rows(
            draft_id="draft-a",
            wins="7",
            creature_count=16,
            spell_count=23,
        )
    )
    rows.extend(
        _structure_deck_rows(
            draft_id="draft-b",
            wins="7",
            creature_count=14,
            spell_count=24,
        )
    )
    rows.extend(
        _structure_deck_rows(
            draft_id="draft-c",
            wins="6",
            creature_count=16,
            spell_count=23,
        )
    )
    return rows


def _structure_deck_rows(
    *,
    draft_id: str,
    wins: str,
    creature_count: int,
    spell_count: int,
) -> list[dict[str, str]]:
    creature_names = [f"Structure Creature {index % 16}" for index in range(creature_count)]
    spell_names = [
        f"Structure Spell {index % 8}"
        for index in range(spell_count - creature_count)
    ]
    return [
        {
            "draft_id": draft_id,
            "expansion": "TST",
            "event_type": QUICK_DRAFT_FORMAT,
            "event_match_wins": wins,
            "pick": name,
            "pick_maindeck_rate": "1.0",
        }
        for name in (*creature_names, *spell_names)
    ]


def _pair_card_rating_row() -> dict[str, object]:
    return {
        "name": "Fixture Pair Filtered Card",
        "mtga_id": 1002,
        "color": "U",
        "rarity": "uncommon",
        "avg_seen": 3.25,
        "seen_count": 900,
        "pick_count": 600,
        "game_count": 700,
        "opening_hand_game_count": 180,
        "opening_hand_win_rate": 0.64,
        "ever_drawn_game_count": 650,
        "ever_drawn_win_rate": 0.66,
        "drawn_improvement_win_rate": 0.04,
    }
