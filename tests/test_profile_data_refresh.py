from __future__ import annotations

from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import draftomen.profile_data_refresh as refresh
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_generation import generate_set_profile
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestArtifact,
)
from draftomen.set_profile import SetProfile
from draftomen.seventeen import (
    ColorPairWinRate,
    RatingSampleCounts,
    SEVENTEEN_LANDS_ATTRIBUTION,
    SeventeenCardStats,
    SeventeenLandsError,
    SeventeenLandsFormatData,
    save_17lands_format_data,
    seventeen_lands_cache_path,
)
from draftomen.set_card_data import SetCardData


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _database(*, set_code: str, set_name: str) -> CardDatabase:
    return CardDatabase(
        cards={
            1: CardInfo(
                grp_id=1,
                name=f"{set_name} Card",
                colors=("W", "U"),
                mana_value=2,
                rarity="common",
                types=("Creature",),
                type_line="Creature — Wizard",
                set_code=set_code,
                arena_id=1,
            )
        }
    )


def _write_card_artifact(
    directory: Path,
    *,
    set_code: str,
    set_name: str,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    artifact = SetCardData.from_card_database(
        _database(set_code=set_code, set_name=set_name),
        set_code=set_code,
        set_name=set_name,
    )
    path = directory / f"{set_code}.json.gz"
    path.write_bytes(artifact.to_gzip_bytes())
    return path


def _filters(
    *,
    available: dict[str, list[str]],
    live: dict[str, list[str] | None] | None = None,
) -> dict[str, Any]:
    return {
        "formats_by_expansion": available,
        "live_formats_by_expansion": {} if live is None else live,
    }


def _ratings(*, set_code: str, event_format: str, fetched_at: datetime = NOW) -> SeventeenLandsFormatData:
    return SeventeenLandsFormatData(
        set_code=set_code.upper(),
        event_format=event_format,
        fetched_at=fetched_at,
        card_ratings={
            1: SeventeenCardStats(
                grp_id=1,
                name="Set Card",
                color="WU",
                rarity="common",
                average_last_seen_at=3.0,
                gih_win_rate=0.60,
                opening_hand_win_rate=0.55,
                drawn_improvement_win_rate=0.58,
                sample_counts=RatingSampleCounts(100, 80, 60, 40, 30),
            )
        },
        pair_win_rates={
            "WU": ColorPairWinRate(pair="WU", wins=60, games=100, win_rate=0.60)
        },
    )


def _empty_ratings(
    *, set_code: str, event_format: str, fetched_at: datetime = NOW
) -> SeventeenLandsFormatData:
    return SeventeenLandsFormatData(
        set_code=set_code.upper(),
        event_format=event_format,
        fetched_at=fetched_at,
        card_ratings={},
        pair_win_rates={},
    )


def _manifest_for_profile(*, set_code: str, event_format: str) -> ProfileManifest:
    generation = generate_set_profile(
        set_code=set_code,
        event_format=event_format,
        stage="metadata",
        card_database=_database(set_code=set_code, set_name="Old Set"),
        generated_at=NOW,
    )
    report = generation.report
    return ProfileManifest(
        artifacts=(
            ProfileManifestArtifact(
                set_code=report.set_code,
                event_format=report.event_format,
                set_profile_schema_version=report.set_profile_schema_version,
                profile_version=generation.profile.profile_version,
                generated_at=report.generated_at,
                url=(
                    "https://www.draftomen.com/profiles/objects/"
                    f"{report.gzip_sha256}.json.gz"
                ),
                gzip_bytes=report.gzip_bytes,
                profile_bytes=report.profile_bytes,
                gzip_sha256=report.gzip_sha256,
                profile_sha256=report.profile_sha256,
                maturity=generation.profile.maturity,
            ),
        ),
        published_at=NOW.isoformat(),
    )


def _write_manifest(directory: Path, manifest: ProfileManifest) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_bytes(manifest.to_bytes())
    return path


def _write_manifest_object(
    profiles_dir: Path,
    *,
    manifest: ProfileManifest,
    set_code: str,
    event_format: str,
) -> tuple[Path, bytes]:
    artifact = manifest.select(set_code=set_code, event_format=event_format)
    assert artifact is not None
    generation = generate_set_profile(
        set_code=set_code,
        event_format=event_format,
        stage="metadata",
        card_database=_database(set_code=set_code, set_name="Old Set"),
        generated_at=NOW,
    )
    payload = generation.gzip_bytes
    assert artifact.gzip_sha256 == generation.report.gzip_sha256
    path = profiles_dir / "objects" / f"{artifact.gzip_sha256}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path, payload


def test_prepare_selects_supported_formats_in_set_and_format_order(
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    _write_card_artifact(card_dir, set_code="bbb", set_name="Beta Set")
    _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.prepare_profile_data_refresh(
        card_data_dir=card_dir,
        fetch_json=lambda _url, _timeout: _filters(
            available={
                "BBB": ["PickTwoDraft", "PremierDraft", "PremierDraft", "Sealed"],
                "AAA": ["QuickDraft", "TradDraft", "Unsupported"],
                "PSEUDO": ["PremierDraft"],
            }
        ),
    )

    assert [(pair.set_code, pair.event_format) for pair in plan.pairs] == [
        ("aaa", "TradDraft"),
        ("aaa", "QuickDraft"),
        ("bbb", "PremierDraft"),
        ("bbb", "PickTwoDraft"),
    ]
    assert all(pair.set_code != "pseudo" for pair in plan.pairs)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("active", [("aaa", "PremierDraft"), ("aaa", "TradDraft")]),
        ("historical", []),
    ],
)
def test_prepare_supports_active_and_historical_pair_selection(
    mode: str,
    expected: list[tuple[str, str]],
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.prepare_profile_data_refresh(
        card_data_dir=card_dir,
        mode=mode,
        fetch_json=lambda _url, _timeout: _filters(
            available={"AAA": ["PremierDraft", "TradDraft"]},
            live={"AAA": ["PremierDraft"]},
        ),
    )

    assert [(pair.set_code, pair.event_format) for pair in plan.pairs] == expected


def test_prepare_keeps_hob_quickdraft_active_when_another_format_is_live(
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    _write_card_artifact(card_dir, set_code="hob", set_name="HOB")
    filters = _filters(
        available={"HOB": ["QuickDraft", "PremierDraft"]},
        live={"HOB": ["PremierDraft"]},
    )

    active = refresh.prepare_profile_data_refresh(
        card_data_dir=card_dir,
        mode="active",
        fetch_json=lambda _url, _timeout: filters,
    )
    historical = refresh.prepare_profile_data_refresh(
        card_data_dir=card_dir,
        mode="historical",
        fetch_json=lambda _url, _timeout: filters,
    )

    assert [(pair.set_code, pair.event_format) for pair in active.pairs] == [
        ("hob", "PremierDraft"),
        ("hob", "QuickDraft"),
    ]
    assert [(pair.set_code, pair.event_format) for pair in historical.pairs] == []


def test_prepare_selector_matches_code_or_full_name_and_rejects_ambiguous(
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    _write_card_artifact(card_dir, set_code="bbb", set_name="Beta Set")
    fetcher = lambda _url, _timeout: _filters(
        available={"AAA": ["PremierDraft"], "BBB": ["PremierDraft"]}
    )

    by_code = refresh.prepare_profile_data_refresh(
        " AaA ", card_data_dir=card_dir, fetch_json=fetcher
    )
    by_name = refresh.prepare_profile_data_refresh(
        "beta set", card_data_dir=card_dir, fetch_json=fetcher
    )
    assert [pair.set_code for pair in by_code.pairs] == ["aaa"]
    assert [pair.set_code for pair in by_name.pairs] == ["bbb"]

    with pytest.raises(refresh.ProfileDataRefreshError, match="does not match"):
        refresh.prepare_profile_data_refresh(
            "alp", card_data_dir=card_dir, fetch_json=fetcher
        )

    _write_card_artifact(card_dir, set_code="alpha", set_name="AAA")
    with pytest.raises(refresh.ProfileDataRefreshError, match="ambiguous"):
        refresh.prepare_profile_data_refresh(
            "aaa", card_data_dir=card_dir, fetch_json=fetcher
        )


def test_prepare_fails_closed_for_malformed_filters_before_execution(
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    with pytest.raises(refresh.ProfileDataRefreshError, match="malformed"):
        refresh.prepare_profile_data_refresh(
            card_data_dir=card_dir,
            fetch_json=lambda _url, _timeout: {"formats_by_expansion": {}},
        )


def test_execute_uses_real_aggregate_loader_and_two_unfiltered_calls_once(
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    card_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.Plan(
        pairs=(refresh.Pair("aaa", "Alpha Set", "QuickDraft", card_path),)
    )
    profiles_dir = tmp_path / "profiles"
    _write_manifest(profiles_dir, _manifest_for_profile(set_code="aaa", event_format="QuickDraft"))
    manifest_path = profiles_dir / "manifest.json"
    calls: list[str] = []

    def fetcher(url: str, _timeout: int) -> Any:
        calls.append(url)
        if "/api/card_data" in url:
            return {
                "data": [
                    {
                        "mtga_id": 1,
                        "name": "Alpha Set Card",
                        "color": "WU",
                        "rarity": "common",
                        "avg_seen": 3,
                        "ever_drawn_win_rate": 0.6,
                        "opening_hand_win_rate": 0.55,
                        "drawn_improvement_win_rate": 0.58,
                        "seen_count": 100,
                        "pick_count": 80,
                        "game_count": 60,
                        "opening_hand_game_count": 40,
                        "ever_drawn_game_count": 30,
                    }
                ]
            }
        return [{"short_name": "WU", "wins": 60, "games": 100}]

    clock = FrozenClock(NOW)
    first = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
        fetch_json=fetcher,
        clock=clock,
    )
    first_manifest_bytes = manifest_path.read_bytes()
    first_manifest_mtime = manifest_path.stat().st_mtime_ns
    first_manifest = ProfileManifest.from_bytes(first_manifest_bytes)
    first_artifact = first_manifest.select(set_code="aaa", event_format="QuickDraft")
    assert first_artifact is not None
    object_path = profiles_dir / "objects" / f"{first_artifact.gzip_sha256}.json.gz"
    first_object_bytes = object_path.read_bytes()
    first_object_mtime = object_path.stat().st_mtime_ns
    assert first_artifact.gzip_sha256 == hashlib.sha256(first_object_bytes).hexdigest()
    SetProfile.from_json(json.loads(gzip.decompress(first_object_bytes)))
    clock.value = NOW + timedelta(hours=2)
    second = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
        fetch_json=fetcher,
        clock=clock,
    )

    assert first.failures == ()
    assert second.failures == ()
    assert len(calls) == 2
    assert all("colors=" not in url for url in calls)
    assert first.manifest_changed is True
    assert second.manifest_changed is False
    assert manifest_path.read_bytes() == first_manifest_bytes
    assert manifest_path.stat().st_mtime_ns == first_manifest_mtime
    assert object_path.read_bytes() == first_object_bytes
    assert object_path.stat().st_mtime_ns == first_object_mtime


def test_execute_rejects_stale_cache_after_offline_refresh_and_preserves_state(
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    card_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.Plan(
        pairs=(refresh.Pair("aaa", "Alpha Set", "QuickDraft", card_path),)
    )
    cache_dir = tmp_path / "cache"
    save_17lands_format_data(
        _ratings(
            set_code="aaa",
            event_format="QuickDraft",
            fetched_at=NOW - timedelta(hours=25),
        ),
        app_dir=cache_dir,
    )
    profiles_dir = tmp_path / "profiles"
    manifest = _manifest_for_profile(set_code="aaa", event_format="QuickDraft")
    manifest_path = _write_manifest(profiles_dir, manifest)
    old_manifest_bytes = manifest_path.read_bytes()
    old_manifest_mtime = manifest_path.stat().st_mtime_ns
    old_object_path, old_object_bytes = _write_manifest_object(
        profiles_dir,
        manifest=manifest,
        set_code="aaa",
        event_format="QuickDraft",
    )
    old_object_mtime = old_object_path.stat().st_mtime_ns
    object_names = tuple(sorted(path.name for path in old_object_path.parent.iterdir()))

    def offline_fetcher(_url: str, _timeout: int) -> Any:
        raise SeventeenLandsError("offline")

    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=cache_dir,
        fetch_json=offline_fetcher,
        clock=FrozenClock(NOW),
    )

    assert result.succeeded is False
    assert result.successful_pairs == ()
    assert [(failure.set_code, failure.category) for failure in result.failures] == [
        ("aaa", "ratings-unavailable")
    ]
    assert manifest_path.read_bytes() == old_manifest_bytes
    assert manifest_path.stat().st_mtime_ns == old_manifest_mtime
    assert old_object_path.read_bytes() == old_object_bytes
    assert old_object_path.stat().st_mtime_ns == old_object_mtime
    assert tuple(sorted(path.name for path in old_object_path.parent.iterdir())) == object_names


def test_execute_accepts_fresh_cache_without_network(
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    card_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.Plan(
        pairs=(refresh.Pair("aaa", "Alpha Set", "QuickDraft", card_path),)
    )
    cache_dir = tmp_path / "cache"
    save_17lands_format_data(
        _ratings(
            set_code="aaa",
            event_format="QuickDraft",
            fetched_at=NOW - timedelta(hours=23),
        ),
        app_dir=cache_dir,
    )
    profiles_dir = tmp_path / "profiles"
    _write_manifest(
        profiles_dir,
        _manifest_for_profile(set_code="aaa", event_format="QuickDraft"),
    )
    calls: list[str] = []

    def offline_fetcher(url: str, _timeout: int) -> Any:
        calls.append(url)
        raise SeventeenLandsError("network should not be used")

    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=cache_dir,
        fetch_json=offline_fetcher,
        clock=FrozenClock(NOW),
    )

    assert result.succeeded is True
    assert result.successful_pairs == plan.pairs
    assert result.failures == ()
    assert calls == []


def test_execute_reads_only_exact_static_artifact_and_never_scryfall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_dir = tmp_path / "card-data"
    card_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.Plan(
        pairs=(refresh.Pair("aaa", "Alpha Set", "QuickDraft", card_path),)
    )
    profiles_dir = tmp_path / "profiles"
    _write_manifest(profiles_dir, _manifest_for_profile(set_code="aaa", event_format="QuickDraft"))
    loaded: list[str] = []

    def fake_loader(**kwargs: Any) -> SeventeenLandsFormatData:
        loaded.append(kwargs["event_format"])
        return _ratings(set_code="aaa", event_format=kwargs["event_format"])

    monkeypatch.setattr(refresh, "load_or_refresh_17lands_format_data", fake_loader)
    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
        clock=FrozenClock(NOW),
    )

    assert result.failures == ()
    assert loaded == ["QuickDraft"]
    assert "scryfall" not in json.dumps(result.to_json()).casefold()


def test_execute_merges_successful_identity_and_preserves_failed_sibling(
    tmp_path: Path,
) -> None:
    card_dir = tmp_path / "card-data"
    first_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    second_path = _write_card_artifact(card_dir, set_code="bbb", set_name="Beta Set")
    plan = refresh.Plan(
        pairs=(
            refresh.Pair("aaa", "Alpha Set", "QuickDraft", first_path),
            refresh.Pair("bbb", "Beta Set", "QuickDraft", second_path),
        )
    )
    profiles_dir = tmp_path / "profiles"
    old_manifest_a = _manifest_for_profile(set_code="aaa", event_format="QuickDraft")
    old_manifest_b = _manifest_for_profile(set_code="bbb", event_format="QuickDraft")
    old_manifest = ProfileManifest(
        artifacts=old_manifest_a.artifacts + old_manifest_b.artifacts,
        published_at=NOW.isoformat(),
    )
    manifest_path = _write_manifest(profiles_dir, old_manifest)
    old_b_object_path, old_b_object_bytes = _write_manifest_object(
        profiles_dir,
        manifest=old_manifest,
        set_code="bbb",
        event_format="QuickDraft",
    )
    old_b_object_mtime = old_b_object_path.stat().st_mtime_ns
    corrupt_cache_path = seventeen_lands_cache_path(
        set_code="bbb",
        event_format="QuickDraft",
        app_dir=tmp_path / "cache",
    )
    corrupt_cache_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_cache_path.write_bytes(b"\xff\xfe not utf-8")

    def fetcher(url: str, _timeout: int) -> Any:
        if "/api/card_data" in url:
            return {
                "data": [
                    {
                        "mtga_id": 1,
                        "name": "Alpha Set Card",
                        "color": "WU",
                        "rarity": "common",
                        "avg_seen": 3,
                        "ever_drawn_win_rate": 0.6,
                        "opening_hand_win_rate": 0.55,
                        "drawn_improvement_win_rate": 0.58,
                        "seen_count": 100,
                        "pick_count": 80,
                        "game_count": 60,
                        "opening_hand_game_count": 40,
                        "ever_drawn_game_count": 30,
                    }
                ]
            }
        return [{"short_name": "WU", "wins": 60, "games": 100}]

    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
        fetch_json=fetcher,
        clock=FrozenClock(NOW),
    )

    assert [(pair.set_code, pair.event_format) for pair in result.successful_pairs] == [
        ("aaa", "QuickDraft")
    ]
    assert [(failure.set_code, failure.category) for failure in result.failures] == [
        ("bbb", "ratings-unavailable")
    ]
    merged = ProfileManifest.from_bytes(manifest_path.read_bytes())
    assert merged.select(set_code="aaa", event_format="QuickDraft") != old_manifest_a.select(
        set_code="aaa", event_format="QuickDraft"
    )
    assert merged.select(set_code="bbb", event_format="QuickDraft") == old_manifest_b.select(
        set_code="bbb", event_format="QuickDraft"
    )
    assert old_b_object_path.read_bytes() == old_b_object_bytes
    assert old_b_object_path.stat().st_mtime_ns == old_b_object_mtime


def test_execute_contains_set_profile_error_and_preserves_failed_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_dir = tmp_path / "card-data"
    first_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    second_path = _write_card_artifact(card_dir, set_code="bbb", set_name="Beta Set")
    plan = refresh.Plan(
        pairs=(
            refresh.Pair("aaa", "Alpha Set", "QuickDraft", first_path),
            refresh.Pair("bbb", "Beta Set", "QuickDraft", second_path),
        )
    )
    profiles_dir = tmp_path / "profiles"
    old_manifest_a = _manifest_for_profile(set_code="aaa", event_format="QuickDraft")
    old_manifest_b = _manifest_for_profile(set_code="bbb", event_format="QuickDraft")
    old_manifest = ProfileManifest(
        artifacts=old_manifest_a.artifacts + old_manifest_b.artifacts,
        published_at=NOW.isoformat(),
    )
    manifest_path = _write_manifest(profiles_dir, old_manifest)
    old_b_object_path, old_b_object_bytes = _write_manifest_object(
        profiles_dir,
        manifest=old_manifest,
        set_code="bbb",
        event_format="QuickDraft",
    )
    old_b_object_mtime = old_b_object_path.stat().st_mtime_ns

    def fake_loader(**kwargs: Any) -> SeventeenLandsFormatData:
        if kwargs["set_code"] == "bbb":
            return _empty_ratings(
                set_code=kwargs["set_code"],
                event_format=kwargs["event_format"],
            )
        return _ratings(
            set_code=kwargs["set_code"],
            event_format=kwargs["event_format"],
        )

    monkeypatch.setattr(refresh, "load_or_refresh_17lands_format_data", fake_loader)
    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
        clock=FrozenClock(NOW),
    )

    assert result.succeeded is False
    assert [(pair.set_code, pair.event_format) for pair in result.successful_pairs] == [
        ("aaa", "QuickDraft")
    ]
    assert [(failure.set_code, failure.category) for failure in result.failures] == [
        ("bbb", "profile-generation-failed")
    ]
    merged = ProfileManifest.from_bytes(manifest_path.read_bytes())
    assert merged.select(set_code="aaa", event_format="QuickDraft") != old_manifest_a.select(
        set_code="aaa", event_format="QuickDraft"
    )
    assert merged.select(set_code="bbb", event_format="QuickDraft") == old_manifest_b.select(
        set_code="bbb", event_format="QuickDraft"
    )
    assert old_b_object_path.read_bytes() == old_b_object_bytes
    assert old_b_object_path.stat().st_mtime_ns == old_b_object_mtime


def test_execute_all_failure_preserves_manifest_and_lists_ordered_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_dir = tmp_path / "card-data"
    first_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    second_path = _write_card_artifact(card_dir, set_code="bbb", set_name="Beta Set")
    plan = refresh.Plan(
        pairs=(
            refresh.Pair("aaa", "Alpha Set", "QuickDraft", first_path),
            refresh.Pair("bbb", "Beta Set", "QuickDraft", second_path),
        )
    )
    profiles_dir = tmp_path / "profiles"
    manifest_path = _write_manifest(
        profiles_dir,
        _manifest_for_profile(set_code="aaa", event_format="QuickDraft"),
    )
    before = manifest_path.read_bytes()
    monkeypatch.setattr(
        refresh,
        "load_or_refresh_17lands_format_data",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("network details")),
    )

    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
    )

    assert result.successful_pairs == ()
    assert [(failure.set_code, failure.category) for failure in result.failures] == [
        ("aaa", "ratings-unavailable"),
        ("bbb", "ratings-unavailable"),
    ]
    assert manifest_path.read_bytes() == before


def test_execute_reports_content_object_conflict_without_manifest_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_dir = tmp_path / "card-data"
    card_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.Plan(
        pairs=(refresh.Pair("aaa", "Alpha Set", "QuickDraft", card_path),)
    )
    profiles_dir = tmp_path / "profiles"
    _write_manifest(
        profiles_dir,
        _manifest_for_profile(set_code="aaa", event_format="QuickDraft"),
    )
    ratings = _ratings(set_code="aaa", event_format="QuickDraft")
    generation = generate_set_profile(
        set_code="aaa",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(set_code="aaa", set_name="Alpha Set"),
        generated_at=ratings.fetched_at,
        ratings=ratings,
    )
    object_path = profiles_dir / "objects" / f"{generation.report.gzip_sha256}.json.gz"
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(b"conflicting bytes")
    object_mtime = object_path.stat().st_mtime_ns
    manifest_path = profiles_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_mtime = manifest_path.stat().st_mtime_ns
    monkeypatch.setattr(
        refresh,
        "load_or_refresh_17lands_format_data",
        lambda **kwargs: ratings,
    )

    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
        clock=FrozenClock(NOW),
    )

    assert result.successful_pairs == ()
    assert [(failure.category, failure.set_code) for failure in result.failures] == [
        ("object-publish-failed", "aaa")
    ]
    assert manifest_path.read_bytes() == manifest_bytes
    assert manifest_path.stat().st_mtime_ns == manifest_mtime
    assert object_path.read_bytes() == b"conflicting bytes"
    assert object_path.stat().st_mtime_ns == object_mtime


def test_execute_manifest_failure_preserves_old_manifest_and_leaves_valid_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_dir = tmp_path / "card-data"
    card_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.Plan(
        pairs=(refresh.Pair("aaa", "Alpha Set", "QuickDraft", card_path),)
    )
    profiles_dir = tmp_path / "profiles"
    _write_manifest(
        profiles_dir,
        _manifest_for_profile(set_code="aaa", event_format="QuickDraft"),
    )
    manifest_path = profiles_dir / "manifest.json"
    old_manifest_bytes = manifest_path.read_bytes()
    old_manifest_mtime = manifest_path.stat().st_mtime_ns
    ratings = _ratings(set_code="aaa", event_format="QuickDraft")
    generation = generate_set_profile(
        set_code="aaa",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(set_code="aaa", set_name="Alpha Set"),
        generated_at=ratings.fetched_at,
        ratings=ratings,
    )
    object_path = profiles_dir / "objects" / f"{generation.report.gzip_sha256}.json.gz"
    real_atomic_write = refresh._atomic_write

    def fail_manifest(*, path: Path, payload: bytes) -> None:
        if path.name == "manifest.json":
            raise OSError("manifest write failed")
        real_atomic_write(path=path, payload=payload)

    monkeypatch.setattr(refresh, "_atomic_write", fail_manifest)
    monkeypatch.setattr(
        refresh,
        "load_or_refresh_17lands_format_data",
        lambda **kwargs: ratings,
    )

    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
        clock=FrozenClock(NOW),
    )

    assert result.successful_pairs == ()
    assert [(failure.category, failure.set_code) for failure in result.failures] == [
        ("manifest-publish-failed", "aaa")
    ]
    assert manifest_path.read_bytes() == old_manifest_bytes
    assert manifest_path.stat().st_mtime_ns == old_manifest_mtime
    published_object = object_path.read_bytes()
    assert hashlib.sha256(published_object).hexdigest() == generation.report.gzip_sha256
    SetProfile.from_json(json.loads(gzip.decompress(published_object)))


def test_published_profile_preserves_empirical_17lands_sources_and_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_dir = tmp_path / "card-data"
    card_path = _write_card_artifact(card_dir, set_code="aaa", set_name="Alpha Set")
    plan = refresh.Plan(
        pairs=(refresh.Pair("aaa", "Alpha Set", "QuickDraft", card_path),)
    )
    profiles_dir = tmp_path / "profiles"
    _write_manifest(
        profiles_dir,
        _manifest_for_profile(set_code="aaa", event_format="QuickDraft"),
    )
    monkeypatch.setattr(
        refresh,
        "load_or_refresh_17lands_format_data",
        lambda **kwargs: _ratings(
            set_code=kwargs["set_code"],
            event_format=kwargs["event_format"],
        ),
    )

    result = refresh.execute_profile_data_refresh(
        plan,
        profiles_dir=profiles_dir,
        cache_dir=tmp_path / "cache",
        clock=FrozenClock(NOW),
    )

    assert result.failures == ()
    manifest = ProfileManifest.from_bytes((profiles_dir / "manifest.json").read_bytes())
    artifact = manifest.select(set_code="aaa", event_format="QuickDraft")
    assert artifact is not None
    object_path = profiles_dir / "objects" / f"{artifact.gzip_sha256}.json.gz"
    profile = SetProfile.from_json(json.loads(gzip.decompress(object_path.read_bytes())))
    assert {rating.gih_win_rate.source for rating in profile.card_ratings} == {
        "17lands:card-ratings"
    }
    assert {
        pair.performance.source
        for pair in profile.pairs
        if pair.performance is not None
    } == {"17lands:color-ratings"}
    ratings = _ratings(set_code="aaa", event_format="QuickDraft")
    assert ratings.attribution == SEVENTEEN_LANDS_ATTRIBUTION
