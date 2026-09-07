from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from draftomen.audit import load_draft_audit_records
from draftomen.carddb import CardDatabase, CardInfo, build_card_database_from_bulk_file
from draftomen.events import EXPECTED_PICKS_PER_PACK
from draftomen.pool import draft_state_path, load_draft_state
from draftomen.profile_client import (
    ProfileClient,
    ProfileNetworkPolicy,
    ProfileRefreshOutcome,
    ProfileRefreshResult,
)
from draftomen.session import DataLoadPhase
from draftomen.set_profile import (
    CardRating,
    RateEstimate,
    SetProfile,
    dump_set_profile,
    load_set_profile,
    set_profile_path,
)

from draftomen.seventeen import QUICK_DRAFT_FORMAT

from draftomen.watch import PlainLogWatcher

FIXTURE_LOG_PATH = Path(__file__).parent / "fixtures" / "quick-draft-msh-player.log"
SCRYFALL_BULK_SAMPLE_PATH = (
    Path(__file__).parent / "fixtures" / "scryfall-default-cards-sample.jsonl"
)
FIXTURE_ACCOUNT_ID = "FIXTURECLIENTID1234567890"
FIXTURE_DRAFT_ID = "00000000-0000-4000-8000-000000000004"


def test_plain_watch_processes_appended_lines_incrementally(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")
    watcher = PlainLogWatcher(
        log_path=log_path,
        app_dir=app_dir,
        card_database=_fixture_card_database(),
        poll_interval=0.01,
    )
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()

    assert watcher.poll_once() == ""

    _append_lines(path=log_path, lines=fixture_lines[:7])
    pack_output = watcher.poll_once()

    assert pack_output.startswith(
        "Active account: FixturePlayer (FIXTURECLIENTID1234567890)\n"
        "Status: active account FixturePlayer (FIXTURECLIENTID1234567890)\n\n"
        "Draft started: QuickDraft_MSH_20260702 "
        "(set MSH, draft 00000000-0000-4000-8000-000000000004)\n"
    )
    assert "Active account: FixturePlayer (FIXTURECLIENTID1234567890)" in pack_output
    assert "Draft started: QuickDraft_MSH_20260702" in pack_output
    assert "Status: active account FixturePlayer" in pack_output
    assert "data neutral prior" in pack_output
    assert "Pack 1 Pick 1" in pack_output
    assert "Fixture Spider (grpId 105097)" in pack_output
    assert "Chosen card:" not in pack_output
    assert pack_output.index("Status: active account FixturePlayer") < (
        pack_output.index("Pack 1 Pick 1")
    )
    assert pack_output.index("Pack 1 Pick 1") < pack_output.index("Data source: ")
    assert pack_output.index("Data source: ") < pack_output.index("Offered cards:")

    _append_lines(path=log_path, lines=fixture_lines[7:8])
    pick_output = watcher.poll_once()

    assert pick_output == "Chosen card: Fixture Spider [G] (grpId 105097)\n\n"
    assert watcher.poll_once() == ""


def test_plain_watch_loads_selected_card_data_during_poll_and_formats_current_db(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")
    started = Event()
    release = Event()
    calls: list[tuple[str, bool]] = []
    database = _fixture_card_database()
    selected_card = replace(database.cards[105097], name="Selected Spider")
    selected_database = replace(
        database,
        cards={**database.cards, 105097: selected_card},
    )

    def loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        calls.append((set_code, allow_network))
        started.set()
        release.wait(timeout=5.0)
        return selected_database

    watcher = PlainLogWatcher(
        log_path=log_path,
        app_dir=tmp_path / "app",
        set_card_data_loader=loader,
        poll_interval=0.01,
    )
    assert calls == []
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()

    _append_lines(path=log_path, lines=fixture_lines[:7])
    output: list[str] = []
    poll_thread = Thread(target=lambda: output.append(watcher.poll_once()))
    poll_thread.start()
    try:
        assert started.wait(timeout=1.0)
        assert poll_thread.is_alive()
        assert calls == [("MSH", True)]
        release.set()
        poll_thread.join(timeout=5.0)
        assert not poll_thread.is_alive()
        assert "Selected Spider (grpId 105097)" in output[0]
        assert watcher.session.card_database is selected_database
    finally:
        release.set()
        watcher.close()


def test_plain_watch_surfaces_selected_card_data_failure(tmp_path: Path) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")

    def loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        assert set_code == "MSH"
        assert allow_network
        raise RuntimeError("card cache unavailable")

    watcher = PlainLogWatcher(
        log_path=log_path,
        app_dir=tmp_path / "app",
        set_card_data_loader=loader,
        poll_interval=0.01,
    )
    try:
        _append_lines(
            path=log_path,
            lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()[:3],
        )
        output = watcher.poll_once()
        assert "Card metadata failed to load: card cache unavailable" in output
        assert watcher.session.snapshot.card_data.phase == DataLoadPhase.FAILED
    finally:
        watcher.close()


def test_plain_watch_renders_accountless_draft_event_without_crashing(
    tmp_path: Path,
) -> None:
    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        poll_interval=0.01,
    )

    output = watcher.process_lines(
        lines=[
            _course_line(
                event_name="QuickDraft_MSH_20260703",
                course_id="new-draft",
            )
        ]
    )

    assert "Draft started: QuickDraft_MSH_20260703" in output
    assert "Status: active account unknown, draft new-draft" in output


def test_plain_watch_scores_accountless_pack_through_shared_session(
    tmp_path: Path,
) -> None:
    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        poll_interval=0.01,
    )

    output = watcher.process_lines(
        lines=[
            _pack_line(
                event_name="QuickDraft_MSH_20260703",
                pack_number=0,
                pick_number=0,
                draft_pack=(105097, 104894),
                picked_cards=(),
            )
        ]
    )

    assert "Status: active account unknown, pick P1P1" in output
    assert "Pack 1 Pick 1" in output
    assert "Fixture Spider (grpId 105097)" in output
    snapshot = watcher.session.snapshot
    assert snapshot.current_scored_pack is not None
    assert snapshot.current_scored_pack.scoring_context is None
    assert snapshot.recommendations.cards


def test_plain_watch_auto_loads_conventional_profile_through_shared_session(
    tmp_path: Path,
) -> None:
    profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json",
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )
    app_dir = tmp_path / "app"
    dump_set_profile(
        profile,
        set_profile_path(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )
    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
        poll_interval=0.01,
    )

    output = watcher.process_lines(
        lines=[
            _pack_line(
                event_name="QuickDraft_TST_20260829",
                pack_number=1,
                pick_number=2,
                draft_pack=(104894, 104976),
                picked_cards=(104894,) * 44,
            )
        ]
    )
    assert "Status: Profile: mature" in output

    snapshot = watcher.session.snapshot
    scored_pack = snapshot.current_scored_pack
    assert scored_pack is not None
    context = scored_pack.scoring_context
    assert context is not None
    assert context.set_profile == profile
    assert context.set_profile.fingerprint == profile.fingerprint
    assert context.set_profile.source == profile.source
    assert context.role_ledger.profile_fingerprint == profile.fingerprint
    assert context.role_ledger.profile_source == f"profile:{profile.maturity.value}"
    assert snapshot.recommendations.cards
    scored_cards = {card.card.grp_id: card for card in scored_pack.cards}
    for recommendation in snapshot.recommendations.cards:
        scored_card = scored_cards[recommendation.card.grp_id]
        assert recommendation.contextual_breakdown == scored_card.contextual_breakdown
        assert recommendation.contextual_evidence == scored_card.contextual_evidence
        assert recommendation.contextual_pair == scored_card.contextual_pair
        assert recommendation.contextual_theme == scored_card.contextual_theme
        assert (
            recommendation.contextual_profile_maturity
            == scored_card.contextual_profile_maturity
        )
    assert (
        recommendation.contextual_profile_confidence
        == scored_card.contextual_profile_confidence
    )
def test_plain_watch_refreshes_profile_off_poll_loop_and_renders_shared_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json",
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )

    def empirical_profile(
        *,
        first_rate: float,
        second_rate: float,
        profile_version: str,
        generated_at: str,
    ) -> SetProfile:
        data = base_profile.to_json()
        data.update(profile_version=profile_version, generated_at=generated_at)
        data["card_ratings"] = [
            {
                "card_key": f"oracle_id:{card_key}",
                "gih_win_rate": {
                    "raw_value": rate,
                    "value": rate,
                    "samples": 100,
                    "prior_value": 0.5,
                    "source": "fixture",
                },
            }
            for card_key, rate in (
                ("cached-first", first_rate),
                ("cached-second", second_rate),
            )
        ]
        return SetProfile.from_json(data)

    cached_profile = empirical_profile(
        first_rate=0.72,
        second_rate=0.58,
        profile_version="1.0-cached",
        generated_at="2026-08-29T00:00:00+00:00",
    )
    refreshed_profile = empirical_profile(
        first_rate=0.41,
        second_rate=0.83,
        profile_version="2.0-refreshed",
        generated_at="2026-08-30T00:00:00+00:00",
    )
    base_database = _small_card_database()
    card_database = replace(
        base_database,
        cards={
            1: replace(
                base_database.cards[101],
                grp_id=1,
                name="Cached First",
                set_code="tst",
                oracle_id="cached-first",
            ),
            2: replace(
                base_database.cards[102],
                grp_id=2,
                name="Cached Second",
                set_code="tst",
                oracle_id="cached-second",
            ),
        },
    )
    started = Event()
    release = Event()
    refreshed = Event()
    cached_calls: list[tuple[str, str]] = []
    refresh_calls: list[tuple[str, str, bool]] = []

    class BlockingProfileClient:
        manifest_url = "https://profiles.example.test/m.json"
        network_policy = "allowed"

        def load_cached(self, set_code: str, event_format: str, **kwargs):
            del kwargs
            cached_calls.append((set_code, event_format))
            return SimpleNamespace(profile=cached_profile, source="cache")

        def refresh(self, set_code: str, event_format: str, *, force: bool):
            refresh_calls.append((set_code, event_format, force))
            started.set()
            assert release.wait(timeout=5.0)
            return ProfileRefreshResult(
                profile=refreshed_profile,
                outcome=ProfileRefreshOutcome.UPDATED,
            )

    def fail_urlopen(request: object, *_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"unexpected provider request: {request!r}")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        card_database=card_database,
        profile_client=BlockingProfileClient(),
    )

    def capture_snapshot(snapshot) -> None:
        if (
            snapshot.set_profile.profile_version == refreshed_profile.profile_version
            and snapshot.current_scored_pack is not None
            and tuple(card.card.grp_id for card in snapshot.current_scored_pack.cards)
            == (2, 1)
        ):
            refreshed.set()

    watcher.session._snapshot_publisher = capture_snapshot
    try:
        watcher.session._set_active_set_code(set_code="TST")
        with watcher.session._state_lock:
            watcher.session._queue_profile_refresh_locked(force=True)
        watcher._schedule_profile_refresh()
        assert started.wait(timeout=5.0)
        poll_started = time.monotonic()
        cached_status = watcher.poll_once()
        assert time.monotonic() - poll_started < 1.0
        assert "Profile: mature (cached)" in cached_status
        blocked_output = watcher.process_lines(
            lines=[
                _pack_line(
                    event_name="QuickDraft_TST_20260905",
                    pack_number=0,
                    pick_number=0,
                    draft_pack=(1, 2),
                    picked_cards=(),
                )
            ]
        )
        assert cached_calls == [("TST", QUICK_DRAFT_FORMAT)]
        assert refresh_calls == [("TST", QUICK_DRAFT_FORMAT, True)]
        assert "Data source: set profile" in blocked_output
        assert blocked_output.index("Cached First") < blocked_output.index("Cached Second")

        release.set()
        assert refreshed.wait(timeout=5.0)
        snapshot = watcher.session.snapshot
        assert snapshot.current_scored_pack is not None
        assert tuple(card.card.grp_id for card in snapshot.current_scored_pack.cards) == (2, 1)
        assert tuple(card.card.grp_id for card in snapshot.recommendations.cards) == (2, 1)
        refreshed_output = watcher.process_lines(lines=[])
        assert "Status: Profile: mature (updated)" in refreshed_output
        assert snapshot.set_profile.profile_version == refreshed_profile.profile_version
    finally:
        release.set()
        watcher.close()


def test_plain_watch_hands_off_obsolete_refresh_to_same_set_lifecycle(
    tmp_path: Path,
) -> None:
    obsolete_profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "set-profiles" / "early.json",
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )
    replacement_profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json",
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )
    first_event_name = "QuickDraft_TST_20260905"
    first_course_id = "draft-a"
    replacement_event_name = "QuickDraft_TST_20260906"
    replacement_course_id = "draft-b"
    first_started = Event()
    first_release = Event()
    replacement_started = Event()
    replacement_release = Event()
    replacement_published = Event()
    refresh_count = 0

    class BlockingProfileClient:
        manifest_url = "https://profiles.example.test/m.json"
        network_policy = "allowed"

        def load_cached(self, set_code: str, event_format: str, **kwargs):
            del kwargs
            return SimpleNamespace(
                profile=SetProfile.generic(
                    set_code=set_code,
                    event_format=event_format,
                ),
                source="generic",
            )

        def refresh(self, set_code: str, event_format: str, *, force: bool):
            assert force is False
            nonlocal refresh_count
            assert (set_code, event_format) == ("TST", QUICK_DRAFT_FORMAT)
            refresh_count += 1
            if refresh_count == 1:
                first_started.set()
                assert first_release.wait(timeout=5.0)
                return ProfileRefreshResult(
                    profile=obsolete_profile,
                    outcome=ProfileRefreshOutcome.UPDATED,
                )
            assert refresh_count == 2
            replacement_started.set()
            assert replacement_release.wait(timeout=5.0)
            return ProfileRefreshResult(
                profile=replacement_profile,
                outcome=ProfileRefreshOutcome.UPDATED,
            )

    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_small_card_database(),
        profile_client=BlockingProfileClient(),
    )
    published_snapshots = []

    def capture_snapshot(snapshot) -> None:
        published_snapshots.append(snapshot)
        if snapshot.set_profile.profile_version == replacement_profile.profile_version:
            replacement_published.set()

    watcher.session._snapshot_publisher = capture_snapshot
    rendered_outputs: list[str] = []
    try:
        rendered_outputs.append(
            watcher.process_lines(
                lines=[
                    _auth_line(client_id="ACCOUNT-A", screen_name="First"),
                    _course_line(
                        event_name=first_event_name,
                        course_id=first_course_id,
                    ),
                ]
            )
        )
        assert first_started.wait(timeout=5.0)
        first_request = watcher.profile_refresh_in_flight
        assert first_request is not None

        rendered_outputs.append(
            watcher.process_lines(
                lines=[
                    _course_line(
                        event_name=replacement_event_name,
                        course_id=replacement_course_id,
                    ),
                    _pack_line(
                        event_name=replacement_event_name,
                        pack_number=0,
                        pick_number=0,
                        draft_pack=(101, 102),
                        picked_cards=(),
                    ),
                ]
            )
        )
        pending_request = watcher.session.profile_refresh_request()
        assert pending_request is not None
        assert pending_request != first_request
        assert watcher.session.snapshot.draft is not None
        assert watcher.session.snapshot.draft.event_name == replacement_event_name
        assert watcher.session.snapshot.draft.draft_id == replacement_course_id

        first_release.set()
        assert replacement_started.wait(timeout=5.0)
        assert not replacement_published.is_set()

        stale_snapshot = watcher.session.snapshot
        assert stale_snapshot.set_profile.profile_version != obsolete_profile.profile_version
        assert stale_snapshot.set_profile.maturity != obsolete_profile.maturity.value
        assert all(
            snapshot.set_profile.profile_version != obsolete_profile.profile_version
            and all(
                recommendation.contextual_profile_maturity
                != obsolete_profile.maturity.value
                for recommendation in snapshot.recommendations.cards
            )
            for snapshot in published_snapshots
        )
        assert all(
            recommendation.contextual_profile_maturity
            != obsolete_profile.maturity.value
            for recommendation in stale_snapshot.recommendations.cards
        )
        rendered_outputs.append(watcher.process_lines(lines=[]))
        assert f"Profile: {obsolete_profile.maturity.value}" not in rendered_outputs[-1]

        replacement_release.set()
        assert replacement_published.wait(timeout=5.0)

        replacement_snapshot = watcher.session.snapshot
        assert replacement_snapshot.set_profile.profile_version == (
            replacement_profile.profile_version
        )
        assert replacement_snapshot.set_profile.maturity == (
            replacement_profile.maturity.value
        )
        assert replacement_snapshot.recommendations.cards
        assert all(
            recommendation.contextual_profile_maturity
            != obsolete_profile.maturity.value
            for recommendation in replacement_snapshot.recommendations.cards
        )
        rendered_outputs.append(watcher.process_lines(lines=[]))
        assert f"Profile: {obsolete_profile.maturity.value}" not in "".join(
            rendered_outputs
        )
        assert f"Profile: {replacement_profile.maturity.value}" in rendered_outputs[-1]
    finally:
        first_release.set()
        replacement_release.set()
        watcher.close()


def test_plain_watch_close_quiesces_blocked_refresh_without_late_publication(
    tmp_path: Path,
) -> None:
    profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json",
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )
    started = Event()
    release = Event()
    close_finished = Event()

    class BlockingProfileClient:
        manifest_url = "https://profiles.example.test/m.json"
        network_policy = "allowed"

        def load_cached(self, set_code: str, event_format: str, **kwargs):
            del kwargs
            return SimpleNamespace(
                profile=SetProfile.generic(
                    set_code=set_code,
                    event_format=event_format,
                ),
                source="generic",
            )

        def refresh(self, set_code: str, event_format: str, *, force: bool):
            assert force is False
            assert (set_code, event_format) == ("TST", QUICK_DRAFT_FORMAT)
            started.set()
            assert release.wait(timeout=5.0)
            return ProfileRefreshResult(
                profile=profile,
                outcome=ProfileRefreshOutcome.UPDATED,
            )

    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        card_database=_fixture_card_database(),
        profile_client=BlockingProfileClient(),
    )
    published = []
    watcher.session._snapshot_publisher = published.append
    watcher.session._set_active_set_code(set_code="TST")
    watcher._schedule_profile_refresh()
    assert started.wait(timeout=5.0)

    def close_watcher() -> None:
        watcher.close()
        close_finished.set()

    closer = Thread(target=close_watcher, daemon=True)
    closer.start()
    try:
        assert not close_finished.wait(timeout=0.05)
        deadline = time.monotonic() + 5.0
        while (
            watcher.session.snapshot.status.phase.value != "stopped"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert watcher.session.snapshot.status.phase.value == "stopped"
        stopped = watcher.session.snapshot
        publication_count = len(published)

        release.set()
        assert close_finished.wait(timeout=5.0)
        closer.join(timeout=5.0)

        assert watcher._profile_refresh_executor is None
        assert watcher.profile_refresh_in_flight is None
        assert watcher.session.snapshot is stopped
        assert len(published) == publication_count
    finally:
        release.set()
        closer.join(timeout=5.0)
        watcher.close()



def test_plain_watch_does_not_assign_post_login_draft_events_to_prior_account(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
        poll_interval=0.01,
    )

    output = watcher.process_lines(
        lines=[
            _auth_line(client_id="first-account", screen_name="First"),
            "[Accounts - Login] Logged in successfully. "
            "Display Name: Second#12345",
            _course_line(
                event_name="QuickDraft_MSH_20260703",
                course_id="second-draft",
            ),
        ]
    )

    assert "Status: active account unknown, draft second-draft" in output
    assert not draft_state_path(
        account_id="first-account",
        draft_id="second-draft",
        app_dir=app_dir,
    ).exists()


def test_plain_watch_degrades_to_neutral_when_profile_cache_fails(
    tmp_path: Path,
) -> None:
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    app_dir = tmp_path / "app"
    profile_path = set_profile_path(
        set_code="MSH",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=app_dir,
    )
    profile_path.parent.mkdir(parents=True)
    profile_path.write_bytes(b"not-json")
    provider_calls: list[object] = []

    def fail_provider(*args: object, **kwargs: object) -> object:
        provider_calls.append((args, kwargs))
        raise AssertionError("provider access during cached-profile fallback")

    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
        poll_interval=0.01,
        profile_client=ProfileClient(
            app_dir=app_dir,
            network_policy=ProfileNetworkPolicy.OFFLINE,
            opener=fail_provider,
        ),
    )
    try:
        output = watcher.process_lines(lines=fixture_lines[:7])
    finally:
        watcher.close()

    assert provider_calls == []
    assert "Status: active account FixturePlayer" in output
    assert "data neutral prior" in output
    assert "Fixture Spider (grpId 105097)" in output
    assert "Prior*" in output


def test_plain_watch_loads_locked_pair_ratings_through_cached_profile(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    dump_set_profile(
        _empirical_pair_profile(),
        set_profile_path(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )
    provider_calls: list[object] = []

    def fail_provider(*args: object, **kwargs: object) -> object:
        provider_calls.append((args, kwargs))
        raise AssertionError("provider access during cached-profile scoring")

    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_pair_ratings_card_database(),
        poll_interval=0.01,
        profile_client=ProfileClient(
            app_dir=app_dir,
            network_policy=ProfileNetworkPolicy.OFFLINE,
            opener=fail_provider,
        ),
    )
    try:
        output = watcher.process_lines(
            lines=[
                _pack_line(
                    event_name="QuickDraft_TST_20260703",
                    pack_number=1,
                    pick_number=1,
                    draft_pack=(3, 4),
                    picked_cards=(1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1),
                )
            ]
        )
    finally:
        watcher.close()

    assert provider_calls == []
    assert "commitment 100% (locked)" in output
    assert "65.0%" in output
    assert "Data source: set profile" in output
    assert "GIH WR" in output
    assert output.index("All-Decks Leader (grpId 4)") < output.index(
        "Pair Upgrade (grpId 3)"
    )


def test_plain_watch_scoring_never_requests_network_with_real_profile_client(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    dump_set_profile(
        _empirical_pair_profile(),
        set_profile_path(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )
    attempted_urls: list[str] = []

    def fail_urlopen(
        request: object,
        *_args: object,
        **_kwargs: object,
    ) -> object:
        attempted_urls.append(getattr(request, "full_url", str(request)))
        raise AssertionError("network request during pack scoring")

    client = ProfileClient(
        app_dir=app_dir,
        manifest_url="https://profiles.example.test/manifest.json",
        network_policy=ProfileNetworkPolicy.OFFLINE,
        opener=fail_urlopen,
    )
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")
    watcher = PlainLogWatcher(
        log_path=log_path,
        app_dir=app_dir,
        card_database=_pair_ratings_card_database(),
        poll_interval=0.01,
        profile_client=client,
    )
    try:
        _append_lines(
            path=log_path,
            lines=_locked_pair_setup_lines(),
        )
        first_output = watcher.poll_once()
        _append_lines(
            path=log_path,
            lines=_locked_pair_continuation_lines(),
        )
        second_output = watcher.poll_once()
    finally:
        watcher.close()

    output = first_output + second_output
    assert attempted_urls == []
    assert "commitment 100% (locked)" in first_output
    assert "All-Decks Leader (grpId 4)" in first_output
    assert first_output.index("All-Decks Leader (grpId 4)") < first_output.index(
        "Pair Upgrade (grpId 3)"
    )
    assert "65.0%" in first_output
    assert "Data source: set profile" in first_output
    assert "Pack 2 Pick 3" in second_output
    assert "commitment 100% (locked)" in second_output
    assert "All-Decks Leader (grpId 4)" in output


def test_plain_watch_recovers_rotation_tail_without_loss_or_duplication(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    watcher = PlainLogWatcher(
        log_path=log_path,
        app_dir=app_dir,
        card_database=_fixture_card_database(),
        poll_interval=0.01,
    )

    _write_lines(path=log_path, lines=fixture_lines[:8])
    output = watcher.poll_once()
    _append_lines(path=log_path, lines=fixture_lines[8:20])
    log_path.rename(previous_log_path)
    _write_lines(path=log_path, lines=fixture_lines[20:])

    output += watcher.poll_once()
    output += watcher.poll_once()

    assert _line_count(output=output, prefix="Pack ") == 42
    assert output.count("Chosen card:") == 42
    assert output.count("Draft complete: 42 cards (explicit completion)") == 1

    state = load_draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=app_dir,
    )
    assert state.completed is True
    assert state.chosen_pick_count == 42
    assert len(state.pool_grp_ids) == 42
    audit_records = load_draft_audit_records(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=app_dir,
    )
    assert len(audit_records) == 86
    assert sum(
        record["record_type"] == "decision_evaluated" for record in audit_records
    ) == 42
    assert sum(record["record_type"] == "choice_made" for record in audit_records) == 42
    assert audit_records[-1]["record_type"] == "draft_completed"


def test_plain_watch_account_switch_announces_and_separates_state(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_small_card_database(),
        poll_interval=0.01,
    )

    output = watcher.process_lines(lines=_two_account_log_lines())

    assert "Active account: First (ACCOUNT-A)" in output
    assert "Account switched: First (ACCOUNT-A) -> Second (ACCOUNT-B)" in output
    assert "Status: active account Second (ACCOUNT-B), pick P1P1" in output
    assert "inferred pair open, commitment 0% (open), pool 0" in output
    assert output.count("Pool: watch ACCOUNT-A/draft-a") == 1
    assert output.count("Pool: watch ACCOUNT-B/draft-b") == 1
    assert output.index("Pool: watch ACCOUNT-A/draft-a") < output.index(
        "Account switched: First (ACCOUNT-A) -> Second (ACCOUNT-B)"
    )
    assert output.index("Account switched: First (ACCOUNT-A) -> Second (ACCOUNT-B)") < (
        output.index("Pool: watch ACCOUNT-B/draft-b")
    )

    first_state = load_draft_state(
        account_id="ACCOUNT-A",
        draft_id="draft-a",
        app_dir=app_dir,
    )
    second_state = load_draft_state(
        account_id="ACCOUNT-B",
        draft_id="draft-b",
        app_dir=app_dir,
    )
    assert first_state.pool_grp_ids == (101,)
    assert second_state.pool_grp_ids == (201,)
    assert first_state.completed is True
    assert second_state.completed is True


def _fixture_card_database() -> CardDatabase:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)
    return replace(
        database,
        cards={
            grp_id: replace(card, set_code="msh")
            for grp_id, card in database.cards.items()
        },
    )


def _small_card_database() -> CardDatabase:
    return CardDatabase(
        cards={
            101: CardInfo(
                grp_id=101,
                name="First Account Pick",
                colors=("G",),
                mana_value=2.0,
                rarity="common",
                types=("Creature",),
            ),
            102: CardInfo(
                grp_id=102,
                name="First Account Other",
                colors=("U",),
                mana_value=3.0,
                rarity="common",
                types=("Instant",),
            ),
            201: CardInfo(
                grp_id=201,
                name="Second Account Pick",
                colors=("R",),
                mana_value=2.0,
                rarity="common",
                types=("Creature",),
            ),
            202: CardInfo(
                grp_id=202,
                name="Second Account Other",
                colors=("W",),
                mana_value=3.0,
                rarity="common",
                types=("Sorcery",),
            ),
        }
    )


def _pair_ratings_card_database() -> CardDatabase:
    return CardDatabase(
        cards={
            1: CardInfo(
                grp_id=1,
                name="White Pool Card",
                colors=("W",),
                mana_value=2.0,
                rarity="common",
                types=("Creature",),
                set_code="tst",
            ),
            2: CardInfo(
                grp_id=2,
                name="Blue Pool Card",
                colors=("U",),
                mana_value=2.0,
                rarity="common",
                set_code="tst",
                types=("Creature",),
            ),
            3: CardInfo(
                grp_id=3,
                name="Pair Upgrade",
                colors=("W",),
                mana_value=3.0,
                rarity="common",
                set_code="tst",
                types=("Creature",),
            ),
            4: CardInfo(
                grp_id=4,
                name="All-Decks Leader",
                colors=("U",),
                mana_value=3.0,
                rarity="common",
                set_code="tst",
                types=("Creature",),
            ),
        }
    )
def _empirical_pair_profile() -> SetProfile:
    profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json",
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )
    return replace(
        profile,
        profile_version="fixture-empirical",
        card_ratings=(
            CardRating(
                card_key="grp_id:3",
                gih_win_rate=RateEstimate(
                    raw_value=0.50,
                    value=0.50,
                    samples=1_000,
                    prior_value=0.50,
                    source="fixture-empirical",
                ),
                average_last_seen_at=3.0,
            ),
            CardRating(
                card_key="grp_id:4",
                gih_win_rate=RateEstimate(
                    raw_value=0.65,
                    value=0.65,
                    samples=1_000,
                    prior_value=0.50,
                    source="fixture-empirical",
                ),
                average_last_seen_at=3.0,
            ),
        ),
    )




def _locked_pair_setup_lines() -> list[str]:
    event_name = "QuickDraft_TST_20260905"
    lines = [
        _auth_line(client_id="watch-account", screen_name="Watch"),
        _course_line(event_name=event_name, course_id="watch-draft"),
    ]
    pool: tuple[int, ...] = ()
    for pick_index in range(15):
        pack_number, pick_number = divmod(
            pick_index,
            EXPECTED_PICKS_PER_PACK,
        )
        chosen_card = 1 if pick_index % 2 == 0 else 2
        lines.extend(
            (
                _pack_line(
                    event_name=event_name,
                    pack_number=pack_number,
                    pick_number=pick_number,
                    draft_pack=(1, 2),
                    picked_cards=pool,
                ),
                _pick_request_line(
                    event_name=event_name,
                    request_id=f"watch-pick-{pick_index}",
                    card_id=chosen_card,
                    pack_number=pack_number,
                    pick_number=pick_number,
                ),
            )
        )
        pool += (chosen_card,)

    lines.append(
        _pack_line(
            event_name=event_name,
            pack_number=1,
            pick_number=1,
            draft_pack=(3, 4),
            picked_cards=pool,
        )
    )
    return lines


def _locked_pair_continuation_lines() -> list[str]:
    event_name = "QuickDraft_TST_20260905"
    pool = tuple(1 if pick_index % 2 == 0 else 2 for pick_index in range(15))
    return [
        _pick_request_line(
            event_name=event_name,
            request_id="watch-target-pick",
            card_id=3,
            pack_number=1,
            pick_number=1,
        ),
        _pack_line(
            event_name=event_name,
            pack_number=1,
            pick_number=2,
            draft_pack=(3, 4),
            picked_cards=(*pool, 3),
        ),
    ]




def _two_account_log_lines() -> list[str]:
    return [
        _auth_line(client_id="ACCOUNT-A", screen_name="First"),
        _course_line(
            event_name="QuickDraft_ABC_20260703",
            course_id="draft-a",
        ),
        _pack_line(
            event_name="QuickDraft_ABC_20260703",
            pack_number=0,
            pick_number=0,
            draft_pack=(101, 102),
            picked_cards=(),
        ),
        _pick_request_line(
            event_name="QuickDraft_ABC_20260703",
            request_id="pick-a",
            card_id=101,
            pack_number=0,
            pick_number=0,
        ),
        _completed_line(
            event_name="QuickDraft_ABC_20260703",
            pack_number=0,
            pick_number=0,
            picked_cards=(101,),
        ),
        _auth_line(client_id="ACCOUNT-B", screen_name="Second"),
        _course_line(
            event_name="QuickDraft_DEF_20260703",
            course_id="draft-b",
        ),
        _pack_line(
            event_name="QuickDraft_DEF_20260703",
            pack_number=0,
            pick_number=0,
            draft_pack=(201, 202),
            picked_cards=(),
        ),
        _pick_request_line(
            event_name="QuickDraft_DEF_20260703",
            request_id="pick-b",
            card_id=201,
            pack_number=0,
            pick_number=0,
        ),
        _completed_line(
            event_name="QuickDraft_DEF_20260703",
            pack_number=0,
            pick_number=0,
            picked_cards=(201,),
        ),
    ]


def _auth_line(*, client_id: str, screen_name: str) -> str:
    return json.dumps(
        {
            "authenticateResponse": {
                "clientId": client_id,
                "screenName": screen_name,
            },
        }
    )


def _course_line(*, event_name: str, course_id: str) -> str:
    return json.dumps(
        {
            "Course": {
                "CourseId": course_id,
                "InternalEventName": event_name,
                "CurrentModule": "BotDraft",
            },
        }
    )


def _pack_line(
    *,
    event_name: str,
    pack_number: int,
    pick_number: int,
    draft_pack: tuple[int, ...],
    picked_cards: tuple[int, ...],
) -> str:
    return _payload_line(
        module="BotDraft",
        payload={
            "Result": "Success",
            "EventName": event_name,
            "DraftStatus": "PickNext",
            "PackNumber": pack_number,
            "PickNumber": pick_number,
            "NumCardsToPick": 1,
            "DraftPack": [str(grp_id) for grp_id in draft_pack],
            "PickedCards": [str(grp_id) for grp_id in picked_cards],
        },
    )


def _pick_request_line(
    *,
    event_name: str,
    request_id: str,
    card_id: int,
    pack_number: int,
    pick_number: int,
) -> str:
    request = {
        "EventName": event_name,
        "PickInfo": {
            "EventName": event_name,
            "CardIds": [str(card_id)],
            "PackNumber": pack_number,
            "PickNumber": pick_number,
        },
    }
    envelope = {"id": request_id, "request": json.dumps(request)}
    return f"[UnityCrossThreadLogger]==> BotDraftDraftPick {json.dumps(envelope)}"


def _completed_line(
    *,
    event_name: str,
    pack_number: int,
    pick_number: int,
    picked_cards: tuple[int, ...],
) -> str:
    return _payload_line(
        module="DeckSelect",
        payload={
            "Result": "Success",
            "EventName": event_name,
            "DraftStatus": "Completed",
            "PackNumber": pack_number,
            "PickNumber": pick_number,
            "NumCardsToPick": 1,
            "DraftPack": [],
            "PickedCards": [str(grp_id) for grp_id in picked_cards],
        },
    )


def _payload_line(*, module: str, payload: dict[str, object]) -> str:
    return json.dumps({"CurrentModule": module, "Payload": json.dumps(payload)})


def _append_lines(*, path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as log_file:
        for line in lines:
            log_file.write(f"{line}\n")


def _write_lines(*, path: Path, lines: list[str]) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _line_count(*, output: str, prefix: str) -> int:
    return sum(1 for line in output.splitlines() if line.startswith(prefix))
