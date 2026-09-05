from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
import time
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from textual.containers import VerticalScroll
from textual.pilot import Pilot
from textual.widgets import DataTable, ProgressBar, Select, Static, Switch

from draftomen.audit import load_draft_audit_records
from draftomen.carddb import (
    CardDatabase,
    CardDatabaseError,
    CardInfo,
    build_card_database_from_bulk_file,
)
from draftomen.cardimages import CardImageService, card_image_cache_dir
from draftomen.preferences import TuiVisibilityPreferences, tui_preferences_path
from draftomen.pickengine import ScoredCard
from draftomen.pool import (
    DraftPick,
    DraftPoolStore,
    DraftState,
    account_profile_path,
    draft_state_path,
    save_draft_state,
)
from draftomen.seventeen import (
    QUICK_DRAFT_FORMAT,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsDownloadProgress,
    SeventeenLandsData,
    SeventeenLandsFormatData,
)
from draftomen.set_profile import dump_set_profile, load_set_profile, set_profile_path
from draftomen.session import (
    ApplicationPhase,
    CardView,
    DataLoadPhase,
    OperationKind,
    RatingsProgressLoader,
    SetCardDataLoader,
)
from draftomen.splash import SplashAssessment
from draftomen.tui import (
    MANA_CARD_TYPE_GLYPHS,
    MANA_ICON_GLYPHS,
    DraftomenTuiApp,
    MissingRatingsScreen,
    _card_info_from_view,
    _card_view_is_unknown,
    _format_card_colors,
    _format_card_types,
    _format_pick_card_name,
    _format_splash_details,
    _format_tui_card_view,
)

FIXTURE_LOG_PATH = Path(__file__).parent / "fixtures" / "quick-draft-msh-player.log"
FIXTURE_ACCOUNT_ID = "FIXTURECLIENTID1234567890"
FIXTURE_DRAFT_ID = "00000000-0000-4000-8000-000000000004"
SCRYFALL_BULK_SAMPLE_PATH = (
    Path(__file__).parent / "fixtures" / "scryfall-default-cards-sample.jsonl"
)


def test_tui_renders_shared_setup_guidance_before_draft(tmp_path: Path) -> None:
    asyncio.run(_assert_tui_renders_shared_setup_guidance(tmp_path=tmp_path))


async def _assert_tui_renders_shared_setup_guidance(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        readiness = app.query_one("#pre-draft-readiness", Static)
        guidance = str(readiness.render())

        assert readiness.display
        for requirement in (
            "No draft or readable Player.log",
            "Detailed Logs (Plugin Support)",
            "Account settings",
            "platform or Arena version",
            "Restart Arena if required",
            "return to Draft Omen and try again while Arena is running",
        ):
            assert requirement in guidance


def test_tui_fixture_stream_updates_pack_panel_and_status_bar(tmp_path: Path) -> None:
    asyncio.run(_assert_fixture_stream_updates_pack_panel(tmp_path=tmp_path))


def test_tui_auto_loads_conventional_profile_through_shared_session(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_tui_auto_loads_conventional_profile(tmp_path=tmp_path))


async def _assert_tui_auto_loads_conventional_profile(tmp_path: Path) -> None:
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
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=[line.replace("MSH", "TST") for line in _first_pack_lines()])
        await pilot.pause()

        snapshot = app.session.snapshot
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


def test_tui_splash_details_use_plain_color_and_mana_language() -> None:
    splash = SplashAssessment(
        classification="splash-ready",
        splash_color="R",
        off_color_pips=1,
        picked_card_count=0,
        fixing_sources=2,
        planned_basic_sources=1,
        available_sources=3,
        required_sources=3,
        grade="A",
        score_advantage=12.0,
        aggressive=False,
        reasons=("elite single-pip card has sufficient mana support",),
    )
    scored_card = cast(
        ScoredCard,
        SimpleNamespace(
            card=CardInfo(
                grp_id=1,
                name="Example Bomb",
                colors=("R",),
                mana_value=5.0,
                rarity="rare",
                types=("Creature",),
            ),
            color_fit="splash-ready",
            splash=splash,
        ),
    )

    details = _format_splash_details(scored_card=scored_card)

    assert "Splash recommendation: Red" in details
    assert "Recommended — mana is supported" in details
    assert "3 of 3 sources" in details
    assert "2 fixing lands, 1 planned Mountain" in details
    assert _format_pick_card_name(scored_card=scored_card) == (
        "SPLASH RED — Example Bomb"
    )


def test_tui_audit_records_ranking_visible_when_the_pick_is_made(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_tui_audit_records_visible_ranking(tmp_path=tmp_path))


async def _assert_tui_audit_records_visible_ranking(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()

        app.process_lines(lines=[_first_pick_lines()[7]])
        await pilot.pause()

    records = load_draft_audit_records(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=tmp_path / "app",
    )
    assert [record["record_type"] for record in records] == [
        "draft_started",
        "decision_evaluated",
        "choice_made",
    ]
    choice = records[-1]
    assert choice["ranking_mode"] == "win_rate"
    assert choice["chosen_grp_id"] == 105097
    assert choice["recommended_grp_id"] == 104894
    assert choice["recommendation_followed"] is False


def test_tui_shows_one_set_reliability_value_only_before_p1p1(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_one_set_reliability_value_only_before_p1p1(tmp_path=tmp_path))


async def _assert_one_set_reliability_value_only_before_p1p1(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path, ratings_loader=_graded_ratings_data)
    expected_reliability = (
        "17Lands reliability for MSH — Marvel Super Heroes Quick Draft: "
        "Medium — 58/100"
    )

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines()[:3])
        readiness = app.query_one("#pre-draft-readiness", Static)
        for _ in range(40):
            await pilot.pause(0.05)
            readiness_text = str(readiness.render())
            if expected_reliability in readiness_text:
                break

        readiness_text = str(readiness.render())
        assert readiness.display
        assert expected_reliability in readiness_text
        assert readiness_text.count("/100") == 1
        table = app.query_one("#pack-table", DataTable)
        assert table.display is False

        app.process_lines(lines=_first_pack_lines()[3:])
        for _ in range(40):
            await pilot.pause(0.05)
            if readiness.display is False and table.display and table.row_count == 14:
                break

        assert readiness.display is False
        assert table.display
        assert table.row_count == 14
        assert "/100" not in _status_text(app=app)
        assert all(
            "/100" not in str(table.get_row_at(index))
            for index in range(table.row_count)
        )


def test_tui_ignores_pre_draft_detection_from_historical_scan(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_historical_detection_is_ignored(tmp_path=tmp_path))


async def _assert_historical_detection_is_ignored(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path, ratings_loader=_graded_ratings_data)

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(
            lines=_first_pack_lines()[:3],
            include_pre_draft_detection=False,
        )
        await pilot.pause()

        readiness = app.query_one("#pre-draft-readiness", Static)
        assert readiness.display
        assert "Quick Draft set not detected yet" in str(readiness.render())
        assert app.loading_rating_sets == frozenset()


async def _assert_fixture_stream_updates_pack_panel(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        await pilot.pause()

        table = app.query_one("#pack-table", DataTable)
        rows = [table.get_row_at(index) for index in range(table.row_count)]
        status = _status_text(app=app)
        snapshot = app.session.snapshot

        assert table.row_count == 14
        assert snapshot.status.phase == ApplicationPhase.DRAFTING
        assert snapshot.active_account is not None
        assert snapshot.active_account.account_id == FIXTURE_ACCOUNT_ID
        assert snapshot.draft is not None
        assert snapshot.draft.draft_id == FIXTURE_DRAFT_ID
        assert snapshot.pool.total_cards == 0
        assert len(snapshot.recommendations.cards) == 14
        assert snapshot.current_pack_event is not None
        assert snapshot.current_pack_event.account_id == FIXTURE_ACCOUNT_ID
        assert snapshot.current_scored_pack is not None
        assert snapshot.current_scored_pack.scoring_context is None
        assert {
            recommendation.card.grp_id
            for recommendation in snapshot.recommendations.cards
        } == {
            card.card.grp_id for card in snapshot.current_scored_pack.cards
        }
        assert DraftomenTuiApp.TITLE == "Draft Omen"
        assert any("Fixture Spider" in row for row in _card_cells(rows=rows))
        assert "Account: FixturePlayer" in status
        assert "Pair: open" in status
        assert "Pick: P1P1" in status
        assert "Pool: 0" in status
        assert "Data: neutral prior" in status
        assert "Card data from 17Lands (17lands.com)" in status


def test_tui_account_indicator_uses_login_display_name_without_auth_screen_name(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_account_indicator_uses_login_display_name_without_auth_screen_name(
            tmp_path=tmp_path,
        )
    )


def test_tui_explains_when_cached_17lands_samples_are_not_usable(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_cached_thin_ratings_are_explained(tmp_path=tmp_path))


async def _assert_cached_thin_ratings_are_explained(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path, ratings_loader=_thin_ratings_data)

    async with app.run_test(size=(140, 30)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                app.session.snapshot.ratings.phase == DataLoadPhase.READY
                and app.session.snapshot.ratings.total_cards == 14
            ):
                break

        assert app.session.snapshot.ratings.phase == DataLoadPhase.READY
        assert app.session.snapshot.ratings.total_cards == 14
        assert (
            "Data: neutral prior (17Lands cached; samples unavailable or thin)"
            in _status_text(app=app)
        )


async def _assert_account_indicator_uses_login_display_name_without_auth_screen_name(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_account_lines_without_screen_name())
        await pilot.pause()

        status = _status_text(app=app)
        assert "Account: FixturePlayer#12345" in status
        assert f"Account: {FIXTURE_ACCOUNT_ID}" not in status


def test_tui_does_not_assign_post_login_draft_events_to_the_prior_account(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_post_login_draft_events_do_not_use_the_prior_account(
            tmp_path=tmp_path,
        )
    )


async def _assert_post_login_draft_events_do_not_use_the_prior_account(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(
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
        await pilot.pause()

        assert not draft_state_path(
            account_id="first-account",
            draft_id="second-draft",
            app_dir=tmp_path / "app",
        ).exists()
        assert "Account: unknown" in _status_text(app=app)
        assert "Error:" not in _status_text(app=app)


def test_tui_cycle_binds_an_unmatched_login_name_to_its_observed_legacy_draft(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_cycle_binds_unmatched_login_name_to_observed_legacy_draft(
            tmp_path=tmp_path,
        )
    )


async def _assert_cycle_binds_unmatched_login_name_to_observed_legacy_draft(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)
    for account_id, draft_id, course_id in (
        ("legacy-account", "legacy-draft", "current-course"),
        ("other-account", "other-draft", "other-course"),
    ):
        save_draft_state(
            state=replace(
                _draft_state(
                    account_id=account_id,
                    draft_id=draft_id,
                    event_name="QuickDraft_MSH_20260702",
                    pool_grp_ids=(104894, 105097),
                ),
                course_id=course_id,
            ),
            app_dir=tmp_path / "app",
        )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(
            lines=[
                "[Accounts - Login] Logged in successfully. "
                "Display Name: MagoAnubiTest#26785",
                _course_snapshot_line(course_id="current-course"),
            ]
        )
        await pilot.pause()

        assert (tmp_path / "app" / "accounts" / "legacy-account.json").exists()

        await pilot.press("a")
        await pilot.pause()

        assert "Account: MagoAnubiTest#26785" in _status_text(app=app)

    restarted_app = _tui_app(tmp_path=tmp_path)
    async with restarted_app.run_test(size=(140, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()

        assert "Account: MagoAnubiTest#26785" in _status_text(app=restarted_app)


def test_tui_does_not_assign_unmatched_login_name_to_ambiguous_course_owners(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_unmatched_login_name_is_not_assigned_to_ambiguous_course_owners(
            tmp_path=tmp_path,
        )
    )


async def _assert_unmatched_login_name_is_not_assigned_to_ambiguous_course_owners(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)
    for account_id in ("first-account", "second-account"):
        save_draft_state(
            state=replace(
                _draft_state(
                    account_id=account_id,
                    draft_id=f"{account_id}-draft",
                    event_name="QuickDraft_MSH_20260702",
                    pool_grp_ids=(104894,),
                ),
                course_id="ambiguous-course",
            ),
            app_dir=tmp_path / "app",
        )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(
            lines=[
                "[Accounts - Login] Logged in successfully. "
                "Display Name: Unmatched#12345",
                _course_snapshot_line(course_id="ambiguous-course"),
            ]
        )
        await pilot.pause()

        for account_id in ("first-account", "second-account"):
            assert not account_profile_path(
                account_id=account_id,
                app_dir=tmp_path / "app",
            ).exists()


def test_tui_account_cycle_visits_each_account_once_and_uses_its_latest_draft(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_account_cycle_visits_each_account_once_and_uses_its_latest_draft(
            tmp_path=tmp_path,
        )
    )


async def _assert_account_cycle_visits_each_account_once_and_uses_its_latest_draft(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)
    old_state = _draft_state(
        account_id="alpha-account",
        account_screen_name="Alpha",
        draft_id="alpha-old",
        event_name="QuickDraft_MSH_20260702",
        pool_grp_ids=(104894,),
    )
    latest_state = replace(
        old_state,
        draft_id="alpha-latest",
        course_id="alpha-latest",
        updated_at="2026-07-05T13:00:00+00:00",
    )
    second_account_state = _draft_state(
        account_id="beta-account",
        account_screen_name="Beta",
        draft_id="beta-draft",
        event_name="QuickDraft_MSH_20260702",
        pool_grp_ids=(105097,),
    )
    for state in (old_state, latest_state, second_account_state):
        save_draft_state(state=state, app_dir=tmp_path / "app")

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == "alpha-account"
        assert app._draft_id == "alpha-latest"
        assert "Account: Alpha" in _status_text(app=app)
        assert "alpha-account" not in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == "beta-account"
        assert app._draft_id == "beta-draft"
        assert "Account: Beta" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == "alpha-account"
        assert app._draft_id == "alpha-latest"


def test_tui_account_cycle_returns_to_live_account_without_a_saved_draft(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_account_cycle_returns_to_live_account_without_a_saved_draft(
            tmp_path=tmp_path,
        )
    )


def test_tui_account_cycle_never_pairs_new_account_with_prior_pack(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_account_cycle_pack_state_is_atomic(tmp_path=tmp_path))


async def _assert_account_cycle_pack_state_is_atomic(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)
    pending_state = replace(
        _draft_state(
            account_id="alpha-account",
            account_screen_name="Alpha",
            draft_id="alpha-live",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(),
        ),
        completed=False,
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=0,
                offered_grp_ids=(104894, 105097),
                pool_before_pick=(),
            ),
        ),
    )
    completed_state = _draft_state(
        account_id="beta-account",
        account_screen_name="Beta",
        draft_id="beta-complete",
        event_name="QuickDraft_MSH_20260702",
        pool_grp_ids=(104894, 105097),
    )
    for state in (pending_state, completed_state):
        save_draft_state(state=state, app_dir=tmp_path / "app")

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()

        assert app.session.snapshot.active_account is not None
        assert app.session.snapshot.active_account.account_id == "alpha-account"
        assert app.session.snapshot.current_pack_event is not None
        assert app.session.snapshot.current_pack_event.account_id == "alpha-account"
        assert app._current_pack_event is not None

        await pilot.press("a")
        await pilot.pause()

        snapshot = app.session.snapshot
        assert snapshot.active_account is not None
        assert snapshot.active_account.account_id == "beta-account"
        assert snapshot.draft is not None
        assert snapshot.draft.completed is True
        assert snapshot.current_pack_event is None
        assert snapshot.current_scored_pack is None
        assert app._current_pack_event is None
        assert app._current_pack is None
        assert "Account: Beta" in _status_text(app=app)


async def _assert_account_cycle_returns_to_live_account_without_a_saved_draft(
    tmp_path: Path,
) -> None:
    live_account_id = "mago-anubi"
    recovered_account_id = "mago-anubi-test"
    app = _tui_app(tmp_path=tmp_path)
    save_draft_state(
        state=_draft_state(
            account_id=recovered_account_id,
            account_screen_name="MagoAnubiTest",
            draft_id="recovered-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(
            lines=[_auth_line(client_id=live_account_id, screen_name="MagoAnubi")]
        )
        await pilot.pause()
        assert app._active_account_id == live_account_id

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == recovered_account_id
        assert app._draft_id == "recovered-draft"
        assert "Account: MagoAnubiTest" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == live_account_id
        assert app._draft_id is None
        assert app._pool_grp_ids == ()
        assert "Account: MagoAnubi" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == recovered_account_id
        assert app._draft_id == "recovered-draft"


def test_tui_login_name_resolves_profile_only_account_and_cycles_back_to_it(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_login_name_resolves_profile_only_account_and_cycles_back_to_it(
            tmp_path=tmp_path,
        )
    )


async def _assert_login_name_resolves_profile_only_account_and_cycles_back_to_it(
    tmp_path: Path,
) -> None:
    live_account_id = "mago-anubi"
    recovered_account_id = "mago-anubi-test"
    app_dir = tmp_path / "app"
    store = DraftPoolStore(app_dir=app_dir)
    store.set_active_account(
        account_id=live_account_id,
        screen_name="MagoAnubi",
    )
    save_draft_state(
        state=_draft_state(
            account_id=recovered_account_id,
            account_screen_name="MagoAnubiTest",
            draft_id="recovered-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=app_dir,
    )
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(
            lines=[
                _auth_line(
                    client_id=recovered_account_id,
                    screen_name="MagoAnubiTest",
                ),
                _course_line(
                    event_name="QuickDraft_MSH_20260702",
                    course_id="recovered-draft",
                ),
                "[Accounts - Login] Logged in successfully. "
                "Display Name: MagoAnubi#57647",
            ]
        )
        await pilot.pause()

        assert app._active_account_id == recovered_account_id
        assert "Account: MagoAnubiTest" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == live_account_id
        assert app._draft_id is None
        assert "Account: MagoAnubi" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == recovered_account_id
        assert "Account: MagoAnubiTest" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == live_account_id
        assert app._draft_id is None
        assert "Account: MagoAnubi" in _status_text(app=app)

        app.process_lines(
            lines=[
                _course_line(
                    event_name="QuickDraft_MSH_20260702",
                    course_id="live-draft",
                ),
                _first_pack_lines()[6],
            ]
        )
        await pilot.pause()

        table = app.query_one("#pack-table", DataTable)
        assert app._draft_id == "live-draft"
        assert table.row_count == 14
        assert "Account: MagoAnubi" in _status_text(app=app)
        assert draft_state_path(
            account_id=live_account_id,
            draft_id="live-draft",
            app_dir=app_dir,
        ).exists()
        assert not draft_state_path(
            account_id=recovered_account_id,
            draft_id="live-draft",
            app_dir=app_dir,
        ).exists()

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == recovered_account_id
        assert "Account: MagoAnubiTest" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert app._active_account_id == live_account_id
        assert app._draft_id == "live-draft"
        assert table.row_count == 14
        assert app._current_pack_event is not None
        assert "Pick: P1P1" in _status_text(app=app)
        assert "View: pack" in _status_text(app=app)
        assert "Account: MagoAnubi" in _status_text(app=app)


def test_tui_newer_account_name_survives_cycling_a_cached_legacy_draft(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_newer_account_name_survives_cycling_a_cached_legacy_draft(
            tmp_path=tmp_path,
        )
    )


async def _assert_newer_account_name_survives_cycling_a_cached_legacy_draft(
    tmp_path: Path,
) -> None:
    account_id = "legacy-account"
    app = _tui_app(tmp_path=tmp_path)
    save_draft_state(
        state=_draft_state(
            account_id=account_id,
            account_screen_name="OldName",
            draft_id="legacy-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()
        assert "Account: OldName" in _status_text(app=app)

        app.process_lines(lines=[_auth_line(client_id=account_id, screen_name="NewName")])
        await pilot.pause()
        assert "Account: NewName" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()
        assert "Account: NewName" in _status_text(app=app)

    restarted_app = _tui_app(tmp_path=tmp_path)
    async with restarted_app.run_test(size=(140, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()

        assert "Account: NewName" in _status_text(app=restarted_app)


def test_tui_recovered_account_name_replaces_account_id_fallback(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_recovered_account_name_replaces_account_id_fallback(tmp_path=tmp_path)
    )


async def _assert_recovered_account_name_replaces_account_id_fallback(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)
    save_draft_state(
        state=_draft_state(
            account_id=FIXTURE_ACCOUNT_ID,
            account_screen_name="PersistedPlayer",
            draft_id="recovered-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=[_auth_line_without_screen_name()])
        await pilot.pause()

        status = _status_text(app=app)
        assert "Account: PersistedPlayer" in status
        assert f"Account: {FIXTURE_ACCOUNT_ID}" not in status


def test_tui_selected_account_handles_draft_events_without_account_id(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_selected_account_handles_draft_events_without_account_id(
            tmp_path=tmp_path,
        )
    )


async def _assert_selected_account_handles_draft_events_without_account_id(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)
    save_draft_state(
        state=_draft_state(
            account_id=FIXTURE_ACCOUNT_ID,
            account_screen_name="PersistedPlayer",
            draft_id="persisted-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause()

        app.process_lines(
            lines=[
                _course_line(
                    event_name="QuickDraft_MSH_20260703",
                    course_id="new-draft",
                )
            ]
        )
        await pilot.pause()

        status = _status_text(app=app)
        assert "Error:" not in status
        assert "Account: PersistedPlayer" in status
        assert "Pick: waiting" in status


def test_tui_current_login_does_not_relabel_recovered_accountless_draft(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_current_login_does_not_relabel_recovered_accountless_draft(
            tmp_path=tmp_path,
        )
    )


async def _assert_current_login_does_not_relabel_recovered_accountless_draft(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)
    save_draft_state(
        state=_draft_state(
            account_id="test-account",
            account_screen_name="MagoAnubiTest",
            draft_id="draft-from-prev-log",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(
            lines=[
                _course_line(
                    event_name="QuickDraft_MSH_20260702",
                    course_id="draft-from-prev-log",
                ),
                *_account_lines_without_screen_name(),
            ]
        )
        await pilot.pause()

        status = _status_text(app=app)
        assert "Error:" not in status
        assert "Account: MagoAnubiTest" in status
        assert "Account: FixturePlayer" not in status


def test_tui_accountless_draft_event_does_not_show_repeating_error(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _assert_accountless_draft_event_does_not_show_repeating_error(tmp_path=tmp_path)
    )


async def _assert_accountless_draft_event_does_not_show_repeating_error(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(
            lines=[
                _course_line(
                    event_name="QuickDraft_MSH_20260703",
                    course_id="new-draft",
                )
            ]
        )
        await pilot.pause()

        status = _status_text(app=app)
        assert "Error: Draft event is missing an MTGA account id." not in status
        assert "Account: unknown" in status
        assert "Pick: waiting" in status


def test_tui_accountless_live_path_retains_picks_and_completion(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_accountless_live_path_completes(tmp_path=tmp_path))


async def _assert_accountless_live_path_completes(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)
    accountless_lines = _full_fixture_lines()[2:]

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=accountless_lines)
        await pilot.pause()

        snapshot = app.session.snapshot
        assert snapshot.status.phase == ApplicationPhase.DRAFT_COMPLETE
        assert snapshot.active_account is None
        assert snapshot.draft is None
        assert snapshot.pool.total_cards == 42
        assert snapshot.current_pack_event is None
        assert snapshot.current_scored_pack is None
        assert snapshot.build is not None
        assert app._pool_size == 42
        assert app._current_pack_event is None
        assert app.build_view_text.startswith("[bold]Suggested deck[/bold]\n\n")
        assert "Account: unknown" in _status_text(app=app)
        assert "Pick: complete" in _status_text(app=app)
        state_root = tmp_path / "app" / "state"
        assert not state_root.exists() or not tuple(state_root.rglob("*.json"))


def test_tui_pack_rows_show_17lands_win_rate_grade_and_do_score(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_pack_rows_show_17lands_stats(tmp_path=tmp_path))


async def _assert_pack_rows_show_17lands_stats(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path, ratings_loader=_graded_ratings_data)

    async with app.run_test(size=(140, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        table = app.query_one("#pack-table", DataTable)
        card_index = app.visible_column_keys.index("card")
        win_rate_index = app.visible_column_keys.index("win_rate")
        grade_index = app.visible_column_keys.index("grade")
        score_index = app.visible_column_keys.index("score")
        split_card_row: list[object] | None = None
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                app.session.snapshot.ratings.phase == DataLoadPhase.READY
                and app.session.snapshot.ratings.total_cards == 14
            ):
                rows = [
                    table.get_row_at(index) for index in range(table.row_count)
                ]
                split_card_row = next(
                    (
                        row
                        for row in rows
                        if str(row[card_index]) == "Fixture Split Card"
                    ),
                    None,
                )
                if split_card_row is not None:
                    break

        assert app.session.snapshot.ratings.phase == DataLoadPhase.READY
        assert app.session.snapshot.ratings.total_cards == 14
        assert split_card_row is not None

        assert str(split_card_row[win_rate_index]) == "62.0%"
        assert str(split_card_row[grade_index]) == "B+"
        assert str(split_card_row[score_index]).isdigit()

def test_tui_render_ignores_publications_during_screen_teardown(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_render_ignores_screen_teardown(tmp_path=tmp_path))


async def _assert_render_ignores_screen_teardown(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(140, 24)):
        sidebar = app.query_one("#sidebar")
        await sidebar.remove()

        app._render_all()


def test_tui_status_shows_close_pick_confidence(tmp_path: Path) -> None:
    asyncio.run(_assert_status_shows_close_pick_confidence(tmp_path=tmp_path))


async def _assert_status_shows_close_pick_confidence(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path, ratings_loader=_close_pick_ratings_data)

    async with app.run_test(size=(150, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        for _ in range(40):
            await pilot.pause(0.05)
            if (
                app.session.snapshot.ratings.phase == DataLoadPhase.READY
                and app.session.snapshot.ratings.total_cards == 14
            ):
                break

        assert app.session.snapshot.ratings.phase == DataLoadPhase.READY
        assert app.session.snapshot.ratings.total_cards == 14
        status = _status_text(app=app)

        assert "Ranking: DO Score" in status
        assert "Confidence: early/open close pick" in status
        assert "DO points" in status


def test_tui_warns_when_card_metadata_is_incomplete(tmp_path: Path) -> None:
    asyncio.run(_assert_tui_warns_when_card_metadata_is_incomplete(tmp_path=tmp_path))


async def _assert_tui_warns_when_card_metadata_is_incomplete(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path, card_database=CardDatabase(cards={}))

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        await pilot.pause()

        assert "Warning: 14 unresolved card metadata" in _status_text(app=app)
        assert "Metadata warning: 14 unresolved card metadata" in _pool_summary_text(
            app=app,
        )


def test_tui_build_view_refuses_unknown_metadata_pool(tmp_path: Path) -> None:
    asyncio.run(_assert_build_view_refuses_unknown_metadata_pool(tmp_path=tmp_path))


async def _assert_build_view_refuses_unknown_metadata_pool(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path, card_database=CardDatabase(cards={}))

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        await pilot.press("b")
        await pilot.pause()

        title = app.query_one("#pack-title", Static)
        pool_summary = app.query_one("#pool-summary", Static)
        assert str(title.render()).startswith("Build view unavailable")
        assert "automatic" not in str(title.render())
        assert pool_summary.display is False
        assert "Build view unavailable: Card metadata is missing" in app.build_view_text
        assert "The build cannot be trusted" in app.build_view_text
        assert "no deck was produced" in app.build_view_text
        assert "Picked pool (1)" in app.build_view_text
        assert "[unresolved] Unknown card 105097 (grpId 105097)" in app.build_view_text
        assert "Build sheet:" not in app.build_view_text
        assert "Build: Card metadata is missing" in _status_text(app=app)
        assert "Build action: cannot build — Card metadata is missing" in _status_text(
            app=app,
        )


def test_tui_config_updates_columns_and_rank_cycle(tmp_path: Path) -> None:
    asyncio.run(_assert_config_updates_columns_and_rank_cycle(tmp_path=tmp_path))


def test_tui_pack_navigation_preserves_focus_and_updates_details(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_pack_navigation_preserves_focus_and_details(tmp_path=tmp_path))


async def _assert_config_updates_columns_and_rank_cycle(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        await pilot.pause()

        assert app.visible_column_keys == (
            "rank",
            "win_rate",
            "grade",
            "score",
            "card",
            "colors",
            "fit",
            "alsa",
            "mv",
            "source",
        )
        assert "Splash: On" in _status_text(app=app)

        await _save_tui_config(
            app=app,
            pilot=pilot,
            secondary_columns=False,
            splash_enabled=False,
        )
        assert app.visible_column_keys == (
            "rank",
            "win_rate",
            "grade",
            "score",
            "card",
            "colors",
        )
        assert app.visibility_preferences.splash_enabled is False
        assert app._current_pack is not None
        assert app._current_pack.splash_state.enabled is False
        assert "Splash: Off" in _status_text(app=app)

        assert app.sort_mode == "score"
        assert "Ranking: DO Score" in _status_text(app=app)

        await pilot.press("s")
        assert app.sort_mode == "win_rate"
        assert "Ranking: 17L WR" in _status_text(app=app)

        await pilot.press("s")
        assert app.sort_mode == "alsa"
        assert "Ranking: ALSA" in _status_text(app=app)

        await pilot.press("s")
        assert app.sort_mode == "mv"
        assert "Ranking: MV" in _status_text(app=app)

        await pilot.press("q")


async def _assert_pack_navigation_preserves_focus_and_details(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(140, 30)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        await pilot.pause()

        table = app.query_one("#pack-table", DataTable)
        card_index = app.visible_column_keys.index("card")
        second_card_name = str(table.get_row_at(1)[card_index])

        assert app.focused == table
        assert table.cursor_type == "row"
        assert "Focused card details" in _focused_card_text(app=app)

        await pilot.press("down")
        await pilot.pause()

        assert table.cursor_coordinate.row == 1
        assert second_card_name in _focused_card_text(app=app)

        app.process_lines(lines=[])
        await pilot.pause()

        assert app.focused == table
        assert table.cursor_coordinate.row == 1
        assert second_card_name in _focused_card_text(app=app)

        await pilot.press("tab")
        await pilot.pause()

        assert app.focused == table

        await pilot.press("right")
        await pilot.pause()

        assert app.focused == table
        assert table.cursor_coordinate.row == 2

        await pilot.press("left")
        await pilot.pause()

        assert table.cursor_coordinate.row == 1

        await pilot.press("up")
        await pilot.pause()

        assert table.cursor_coordinate.row == 0


def test_tui_sidebar_updates_pool_distribution_and_curve(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_sidebar_updates_pool_summary(tmp_path=tmp_path))


def test_tui_mana_icon_mapping_preserves_plain_fallback() -> None:
    multicolor = CardInfo(
        grp_id=1,
        name="Azorius Fixture",
        colors=("W", "U"),
        mana_value=2.0,
        rarity="common",
        types=("Creature — Wizard",),
    )
    colorless = CardInfo(
        grp_id=2,
        name="Colorless Fixture",
        colors=(),
        mana_value=3.0,
        rarity="common",
        types=("Artifact",),
    )
    unknown = CardInfo.unknown_card(grp_id=3)

    assert _format_card_colors(card=multicolor) == "WU"
    assert _format_card_colors(card=colorless) == "Colorless"
    assert _format_card_colors(card=unknown) == "Unknown"
    assert _format_card_colors(
        card=multicolor,
        mana_icons_enabled=True,
    ) == f"{MANA_ICON_GLYPHS['W']}{MANA_ICON_GLYPHS['U']}"
    assert _format_card_colors(
        card=colorless,
        mana_icons_enabled=True,
    ) == f"{MANA_ICON_GLYPHS['C']} Colorless"
    assert _format_card_colors(
        card=colorless,
        mana_icons_enabled=True,
        long_colorless=False,
    ) == f"{MANA_ICON_GLYPHS['C']} C"
    assert _format_card_colors(card=unknown, mana_icons_enabled=True) == "Unknown"
    assert _format_card_types(
        card=multicolor,
        mana_icons_enabled=True,
    ) == f"{MANA_CARD_TYPE_GLYPHS['Creature']} Creature — Wizard"


def test_tui_card_view_roundtrip_preserves_partial_arena_unknown() -> None:
    card = CardView(
        grp_id=9001,
        name="Partial Arena Card",
        colors=("R",),
        rarity="rare",
        types=("Unknown",),
        mana_cost="{2}{R}",
        mana_value=3.0,
        image_path=None,
        unknown=True,
        arena_id=9001,
        source_provenance=("arena",),
    )

    restored = _card_info_from_view(card=card)

    assert card.unknown is True
    assert _card_view_is_unknown(card=card) is True
    assert restored.unknown is True
    assert restored.rarity == "rare"
    assert restored.types == ("Unknown",)
    assert restored.arena_id == 9001
    assert _format_tui_card_view(card=card) == (
        "Partial Arena Card [Unknown] (grpId 9001)"
    )


def test_tui_mana_icons_toggle_updates_tui_surfaces(tmp_path: Path) -> None:
    asyncio.run(_assert_mana_icons_toggle_updates_surfaces(tmp_path=tmp_path))


async def _assert_sidebar_updates_pool_summary(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        summary = _pool_summary_text(app=app)

        assert "Pool size: 1" in summary
        assert "Colors:" in summary
        assert "G █████ 1" in summary
        assert "Set: MSH — Marvel Super Heroes" in summary
        assert "Curve:" in summary
        assert "4█1" in summary


async def _assert_mana_icons_toggle_updates_surfaces(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(140, 30)) as pilot:
        app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        table = app.query_one("#pack-table", DataTable)
        rows = [table.get_row_at(index) for index in range(table.row_count)]
        card_index = app.visible_column_keys.index("card")
        colors_index = app.visible_column_keys.index("colors")
        blue_row = next(
            row for row in rows if "Fixture Blue Card" in str(row[card_index])
        )

        assert str(blue_row[colors_index]) == "U"
        assert "Mana icons: off" in _status_text(app=app)

        await pilot.press("m")
        await pilot.pause()

        rows = [table.get_row_at(index) for index in range(table.row_count)]
        blue_row = next(
            row for row in rows if "Fixture Blue Card" in str(row[card_index])
        )
        blue_icon = MANA_ICON_GLYPHS["U"]
        green_icon = MANA_ICON_GLYPHS["G"]

        assert str(blue_row[colors_index]) == blue_icon
        assert f"{green_icon} █████ 1" in _pool_summary_text(app=app)
        assert "Mana icons: on" in _status_text(app=app)

        await pilot.press("b")
        await pilot.pause()

        assert green_icon in app.build_view_text
        assert any(
            MANA_ICON_GLYPHS[color] in _status_text(app=app)
            for color in ("W", "U", "B", "R", "G")
        )

        await _save_tui_config(
            app=app,
            pilot=pilot,
            build_details=True,
        )

        assert f"01. Fixture Spider | Colors {green_icon} | MV 4" in app.build_view_text


def test_tui_build_view_lists_full_picked_pool_with_card_details(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_build_view_lists_full_picked_pool(tmp_path=tmp_path))


async def _assert_build_view_lists_full_picked_pool(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)
    save_draft_state(
        state=_draft_state(
            account_id="FIXTURECLIENTID1234567890",
            draft_id="pool-details-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_account_lines())
        await pilot.pause()

        assert "Picked pool" not in app.build_view_text

        await _save_tui_config(
            app=app,
            pilot=pilot,
            build_details=True,
        )

        text = app.build_view_text
        assert "Picked pool (2)" in text
        assert "01. Fixture Split Card | Colors WU | MV 3" in text
        assert "02. Fixture Spider | Colors G | MV 4" in text


def test_tui_build_keybinding_opens_build_view_on_demand(tmp_path: Path) -> None:
    asyncio.run(_assert_build_keybinding_opens_build_view(tmp_path=tmp_path))


async def _assert_build_keybinding_opens_build_view(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        await pilot.press("b")
        await pilot.pause()

        title = app.query_one("#pack-title", Static)
        assert str(title.render()).startswith("Build view — pair")
        assert app.session.snapshot.build is not None
        assert app.build_view_text.startswith("[bold]Suggested deck[/bold]\n\n")
        assert "Set: MSH — Marvel Super Heroes" in app.build_view_text
        assert "Pool size: 1 cards" not in app.build_view_text
        assert "Average mana value:" in app.build_view_text
        assert "Mana curve: 0:" not in app.build_view_text
        assert "Selected spells by mana value" in app.build_view_text
        assert "Picked pool" not in app.build_view_text
        assert "Color-pair reasoning" not in app.build_view_text
        assert "Pair scores" not in app.build_view_text
        assert "Details hidden: open config with c" in app.build_view_text
        assert "Build action: rebuilt current pool" in _status_text(app=app)

        await _save_tui_config(
            app=app,
            pilot=pilot,
            build_details=True,
        )

        assert "Build context" in app.build_view_text
        assert "Pool size: 1 cards" in app.build_view_text
        assert "Mana curve: 0:" in app.build_view_text
        assert "Picked pool (1)" in app.build_view_text
        assert "01. Fixture Spider | Colors G | MV 4" in app.build_view_text

        await pilot.press("b")
        await pilot.pause()

        assert "Build action: no build needed — current pool already shown" in _status_text(
            app=app,
        )


def test_tui_build_view_collapses_duplicate_selected_spells(tmp_path: Path) -> None:
    asyncio.run(
        _assert_build_view_collapses_duplicate_selected_spells(tmp_path=tmp_path)
    )


async def _assert_build_view_collapses_duplicate_selected_spells(
    tmp_path: Path,
) -> None:
    app = _tui_app(
        tmp_path=tmp_path,
        card_database=CardDatabase(
            cards={
                1: CardInfo(
                    grp_id=1,
                    name="Copy",
                    colors=("G",),
                    mana_value=4.0,
                    rarity="common",
                    types=("Creature",),
                ),
                2: CardInfo(
                    grp_id=2,
                    name="One",
                    colors=("W",),
                    mana_value=2.0,
                    rarity="common",
                    types=("Creature",),
                ),
            }
        ),
    )
    save_draft_state(
        state=_draft_state(
            account_id="FIXTURECLIENTID1234567890",
            draft_id="duplicate-card-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(1, 1, 1, 2),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_account_lines())
        await pilot.pause()

        text = app.build_view_text
        assert "Selected spells by mana value (4)" in text
        assert "MV 4 (3)" in text
        assert "Copy (G) x3" in text
        assert text.count("Copy (G)") == 1
        assert "One (W)" in text
        assert "One (W) x" not in text
        assert "DO" not in text
        assert "50 Copy (G) x3" in text
        assert "50 One (W)" in text
        assert "Lands: 36" in text
        assert "Basics: 9 Plains, 27 Forest" in text
        assert "Land count:" not in text


def test_tui_completion_switches_to_build_view_and_pair_override_keybinding(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_completion_build_view_and_pair_override(tmp_path=tmp_path))


async def _assert_completion_build_view_and_pair_override(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_full_fixture_lines())
        await pilot.pause()

        title = app.query_one("#pack-title", Static)
        table = app.query_one("#pack-table", DataTable)
        build_scroll = app.query_one("#build-scroll", VerticalScroll)
        pool_summary = app.query_one("#pool-summary", Static)

        assert str(title.render()).startswith("Build view — pair WU (automatic)")
        assert table.display is False
        assert build_scroll.display is True
        assert pool_summary.display is False
        assert app.session.snapshot.status.phase == ApplicationPhase.DRAFT_COMPLETE
        assert app.session.snapshot.draft is not None
        assert app.session.snapshot.draft.completed is True
        assert app.session.snapshot.pool.total_cards == 42
        assert app.build_view_text.startswith("[bold]Suggested deck[/bold]\n\n")
        assert "Selected spells by mana value" in app.build_view_text
        assert "Color pair: WU (automatic" in app.build_view_text
        assert "Average mana value:" in app.build_view_text
        assert "Mana curve: 0:" not in app.build_view_text
        assert "▶" in app.build_view_text
        assert "Selected card 1/" in _focused_card_text(app=app)

        await pilot.press("down")
        await pilot.pause()

        assert app.focused == build_scroll
        assert "Selected card 2/" in _focused_card_text(app=app)

        await pilot.press("tab")
        await pilot.pause()

        assert app.focused == build_scroll

        await pilot.press("right")
        await pilot.pause()

        assert app.focused == build_scroll
        assert "Selected card 3/" in _focused_card_text(app=app)

        await pilot.press("s")
        await pilot.pause()

        assert "Selected spells by score" in app.build_view_text
        assert "Build sort: score" in _status_text(app=app)

        await _save_tui_config(
            app=app,
            pilot=pilot,
            build_details=True,
        )

        assert "Color-pair reasoning" in app.build_view_text
        assert "top 23 sum" in app.build_view_text
        assert "Bench" in app.build_view_text

        await pilot.press("pagedown")
        await pilot.pause()

        assert build_scroll.scroll_y > 0

        await pilot.press("p")
        await pilot.pause()

        assert str(title.render()).startswith("Build view — pair WB (forced WB)")
        assert "Color pair: WB (forced" in app.build_view_text
        assert "Override: WB" in _status_text(app=app)


def test_tui_backtest_keybinding_opens_recommendation_report(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_backtest_keybinding_opens_report(tmp_path=tmp_path))


async def _assert_backtest_keybinding_opens_report(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(160, 40)) as pilot:
        app.process_lines(lines=_full_fixture_lines())
        await pilot.pause()
        state_path = draft_state_path(
            account_id=FIXTURE_ACCOUNT_ID,
            draft_id=FIXTURE_DRAFT_ID,
            app_dir=tmp_path / "app",
        )
        before = state_path.read_text(encoding="utf-8")

        await pilot.press("t")
        await pilot.pause()

        title = app.query_one("#pack-title", Static)
        table = app.query_one("#pack-table", DataTable)
        build_scroll = app.query_one("#build-scroll", VerticalScroll)
        pool_summary = app.query_one("#pool-summary", Static)

        assert str(title.render()).startswith("Backtest view — DO Score recommendations")
        assert app.session.snapshot.backtest is not None
        assert table.display is False
        assert build_scroll.display is True
        assert pool_summary.display is False
        assert app.focused == build_scroll
        assert app.backtest_view_text.startswith("Draft Omen backtest\n")
        assert "Ranking: DO Score" in app.backtest_view_text
        assert "Picks: 42 chosen, 42 compared, 0 skipped" in app.backtest_view_text
        assert "Pack  Pick  Pool  17L WR  DO" in app.backtest_view_text
        assert "Recommended" in app.backtest_view_text
        assert "Actual" in app.backtest_view_text
        assert "Match" in app.backtest_view_text
        assert "Fixture Spider [G] (grpId 105097)" in app.backtest_view_text
        assert "Summary:" in app.backtest_view_text
        assert "View: backtest" in _status_text(app=app)
        assert "Backtest action: rebuilt DO Score recommendation comparison" in _status_text(
            app=app,
        )
        assert state_path.read_text(encoding="utf-8") == before



def test_tui_failed_backtest_refresh_replaces_stale_report(tmp_path: Path) -> None:
    asyncio.run(_assert_failed_backtest_refresh_replaces_stale_report(tmp_path=tmp_path))


async def _assert_failed_backtest_refresh_replaces_stale_report(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(160, 40)) as pilot:
        app.process_lines(lines=_full_fixture_lines())
        await pilot.pause()
        state_path = draft_state_path(
            account_id=FIXTURE_ACCOUNT_ID,
            draft_id=FIXTURE_DRAFT_ID,
            app_dir=tmp_path / "app",
        )

        await pilot.press("t")
        await pilot.pause()
        assert app.backtest_view_text.startswith("Draft Omen backtest\n")

        state_path.unlink()
        await pilot.press("s")
        for _ in range(20):
            await pilot.pause(0.05)
            if any(
                error.operation == OperationKind.BACKTEST
                for error in app.session.snapshot.errors
            ):
                break

        assert app.session.snapshot.backtest is None
        assert app.backtest_view_text.startswith("Backtest view unavailable:")
        assert "Draft Omen backtest\n" not in app.backtest_view_text
        assert "Backtest action: cannot backtest" in _status_text(app=app)


def test_tui_backtest_view_reports_missing_history_without_mutating_state(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_backtest_missing_history_is_read_only(tmp_path=tmp_path))


async def _assert_backtest_missing_history_is_read_only(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)
    state = _draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id="missing-history-draft",
        event_name="QuickDraft_MSH_20260702",
        pool_grp_ids=(105097,),
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=0,
                offered_grp_ids=None,
                pool_before_pick=(),
                chosen_grp_id=105097,
            ),
        ),
    )
    save_draft_state(state=state, app_dir=tmp_path / "app")
    state_path = draft_state_path(
        account_id=state.account_id,
        draft_id=state.draft_id,
        app_dir=tmp_path / "app",
    )
    before = state_path.read_text(encoding="utf-8")

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_account_lines())
        await pilot.pause()

        await pilot.press("t")
        await pilot.pause()

        assert "Picks: 1 chosen, 0 compared, 1 skipped" in app.backtest_view_text
        assert "skipped: missing offered-card history" in app.backtest_view_text
        assert "Summary: no comparable picks; 1 skipped." in app.backtest_view_text
        assert state_path.read_text(encoding="utf-8") == before



def test_tui_card_image_preserves_ratio_with_auto_height(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _assert_card_image_preserves_ratio_with_auto_height(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
        )
    )


async def _assert_card_image_preserves_ratio_with_auto_height(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_sizes: list[tuple[int | None, object]] = []

    class FakeTgpImage:
        def __init__(
            self,
            image: str,
            width: int | None = None,
            height: object = None,
        ) -> None:
            captured_sizes.append((width, height))

        def __rich_console__(self, *args: object) -> list[str]:
            return ["<image>"]

    def successful_opener(*args: object, **kwargs: object) -> BytesIO:
        return BytesIO(b"image")

    monkeypatch.setattr("draftomen.tui.TgpImage", FakeTgpImage)
    fixture_database = _fixture_card_database()
    app = _tui_app(
        tmp_path=tmp_path,
        card_database=CardDatabase(
            cards=fixture_database.cards,
            image_uris_by_name={"fixture spider": "https://cards.example/spider.jpg"},
        ),
        image_preview_enabled=True,
        card_image_opener=successful_opener,
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        await pilot.press("b")
        for _ in range(10):
            await pilot.pause(0.05)
            if captured_sizes:
                break

        assert captured_sizes
        width, height = captured_sizes[-1]
        assert width is not None and width <= 30
        assert height == "auto"


def test_tui_build_focused_card_image_fetch_failure_stays_nonblocking(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_build_image_fetch_failure_stays_nonblocking(tmp_path=tmp_path))


async def _assert_build_image_fetch_failure_stays_nonblocking(tmp_path: Path) -> None:
    started = threading.Event()

    def failing_opener(request: object, **kwargs: object) -> BytesIO:
        assert getattr(request, "full_url") == "https://cards.example/spider.jpg"
        started.set()
        raise OSError("network down")

    fixture_database = _fixture_card_database()
    app = _tui_app(
        tmp_path=tmp_path,
        card_database=CardDatabase(
            cards=fixture_database.cards,
            image_uris_by_name={"fixture spider": "https://cards.example/spider.jpg"},
        ),
        image_preview_enabled=True,
        card_image_opener=failing_opener,
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        await pilot.press("b")
        await pilot.pause()

        assert "Selected card 1/" in _focused_card_text(app=app)
        assert await asyncio.to_thread(started.wait, 0.5)
        for _ in range(10):
            await pilot.pause(0.05)
            image_text = _card_image_preview_text(app=app)
            if "Image preview unavailable" in image_text:
                break

        image_panel = app.query_one("#card-image-preview", Static)
        assert image_panel.display is True
        assert "Image preview unavailable" in _card_image_preview_text(app=app)
        assert "network down" in _card_image_preview_text(app=app)
        assert "Error:" not in _status_text(app=app)
        assert app.build_view_text.startswith("[bold]Suggested deck[/bold]\n\n")


def test_tui_account_event_recovers_latest_persisted_state(tmp_path: Path) -> None:
    asyncio.run(_assert_account_event_recovers_latest_persisted_state(tmp_path=tmp_path))


async def _assert_account_event_recovers_latest_persisted_state(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)
    save_draft_state(
        state=_draft_state(
            account_id="FIXTURECLIENTID1234567890",
            draft_id="recovered-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_account_lines())
        await pilot.pause()

        assert "Account: FixturePlayer" in _status_text(app=app)
        assert "Draft: recovered-draft" not in app.build_view_text
        assert "Pool size: 2 cards" not in app.build_view_text

        await _save_tui_config(
            app=app,
            pilot=pilot,
            build_details=True,
        )

        assert "Draft: recovered-draft" in app.build_view_text
        assert "Pool size: 2 cards" in app.build_view_text


def test_tui_account_key_cycles_recovered_drafts(tmp_path: Path) -> None:
    asyncio.run(_assert_account_key_cycles_recovered_drafts(tmp_path=tmp_path))


async def _assert_account_key_cycles_recovered_drafts(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)
    save_draft_state(
        state=_draft_state(
            account_id="test-account",
            account_screen_name="TestUser",
            draft_id="test-draft",
            event_name="QuickDraft_MSH_20260702",
            pool_grp_ids=(104894, 105097),
        ),
        app_dir=tmp_path / "app",
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_full_fixture_lines())
        await pilot.pause()

        assert "Account: FixturePlayer" in _status_text(app=app)

        await pilot.press("a")
        await pilot.pause()

        assert "Account: TestUser" in _status_text(app=app)
        assert "Draft: test-draft" not in app.build_view_text
        assert "Pool size: 2 cards" not in app.build_view_text

        await _save_tui_config(
            app=app,
            pilot=pilot,
            build_details=True,
        )

        assert "Draft: test-draft" in app.build_view_text
        assert "Pool size: 2 cards" in app.build_view_text


def test_tui_narrow_width_hides_secondary_columns_first(tmp_path: Path) -> None:
    asyncio.run(_assert_narrow_width_hides_secondary_columns(tmp_path=tmp_path))


async def _assert_narrow_width_hides_secondary_columns(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(60, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        await pilot.pause()

        assert app.visible_column_keys == (
            "rank",
            "win_rate",
            "grade",
            "score",
            "card",
            "colors",
        )


def test_tui_loads_card_metadata_after_detected_set(tmp_path: Path) -> None:
    asyncio.run(_assert_card_metadata_load_starts_on_detected_set(tmp_path=tmp_path))


def test_tui_startup_recovery_hands_off_to_exactly_once_polling(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_startup_recovery_serializes_polling(tmp_path=tmp_path))


async def _assert_startup_recovery_serializes_polling(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    card_calls: list[tuple[str, bool]] = []
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        "\n".join(_first_pack_lines()) + "\n",
        encoding="utf-8",
    )

    def card_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        card_calls.append((set_code, allow_network))
        return _fixture_card_database()

    def slow_loader(set_code: str) -> SeventeenLandsData:
        started.set()
        release.wait(timeout=5.0)
        return _graded_ratings_data(set_code)

    app = _tui_app(
        tmp_path=tmp_path,
        set_card_data_loader=card_loader,
        ratings_loader=slow_loader,
        poll_enabled=True,
        startup_scan=True,
        poll_interval=0.01,
    )

    try:
        async with app.run_test(size=(120, 30)) as pilot:
            assert await asyncio.to_thread(started.wait, 0.5)
            assert card_calls == [("MSH", True)]
            with log_path.open(mode="a", encoding="utf-8") as log_file:
                log_file.write(_first_pick_lines()[7] + "\n")

            await pilot.pause(0.1)
            assert app.session.snapshot.pool.total_cards == 0

            release.set()
            for _ in range(40):
                await pilot.pause(0.05)
                if app.session.snapshot.pool.total_cards == 1:
                    break

            assert app.session.snapshot.pool.total_cards == 1
            audit_records = load_draft_audit_records(
                account_id=FIXTURE_ACCOUNT_ID,
                draft_id=FIXTURE_DRAFT_ID,
                app_dir=tmp_path / "app",
            )
            assert [record["record_type"] for record in audit_records] == [
                "draft_started",
                "decision_evaluated",
                "choice_made",
            ]
    finally:
        release.set()


async def _assert_card_metadata_load_starts_on_detected_set(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, bool]] = []
    database = _fixture_card_database()
    selected_card = replace(database.cards[105097], name="Selected Spider")
    selected_database = replace(
        database,
        cards={**database.cards, 105097: selected_card},
    )
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")

    def slow_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        assert set_code == "MSH"
        assert allow_network
        calls.append((set_code, allow_network))
        started.set()
        if not release.wait(timeout=5.0):
            raise TimeoutError("Test did not release card-data loader.")
        return selected_database

    app = _tui_app(
        tmp_path=tmp_path,
        set_card_data_loader=slow_loader,
        poll_enabled=True,
        poll_interval=0.01,
    )

    async with app.run_test(size=(120, 24)) as pilot:
        try:
            await pilot.pause()
            assert not started.is_set()
            log_path.write_text("\n".join(_first_pack_lines()) + "\n", encoding="utf-8")
            for _ in range(40):
                if started.is_set():
                    break
                await asyncio.sleep(0.05)
            assert started.is_set()
            assert app.card_database_loading
            assert "Loading card metadata" in _status_text(app=app)
            assert app.query_one("#pack-table", DataTable).loading

            release.set()
            table = app.query_one("#pack-table", DataTable)
            for _ in range(40):
                await pilot.pause(0.05)
                if (
                    app.session.snapshot.card_data.phase == DataLoadPhase.READY
                    and table.row_count == 14
                ):
                    break

            assert not app.card_database_loading
            assert not table.loading
            assert table.row_count == 14
            assert calls == [("MSH", True)]
            rows = [list(table.get_row_at(index)) for index in range(table.row_count)]
            assert any("Selected Spider" in row for row in _card_cells(rows=rows))
        finally:
            release.set()


async def _assert_card_metadata_load_failure_is_visible(tmp_path: Path) -> None:
    calls: list[tuple[str, bool]] = []

    def card_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        calls.append((set_code, allow_network))
        assert set_code == "MSH"
        if len(calls) == 1:
            raise CardDatabaseError("Scryfall is unavailable")
        return _fixture_card_database()

    app = _tui_app(
        tmp_path=tmp_path,
        set_card_data_loader=card_loader,
        poll_enabled=True,
    )

    async with app.run_test(size=(120, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines()[:3])
        await pilot.pause(0.1)
        for _ in range(10):
            await pilot.pause(0.05)
            if app.session.snapshot.card_data.phase == DataLoadPhase.FAILED:
                break

        table = app.query_one("#pack-table", DataTable)
        status = _status_text(app=app)
        assert app.session.snapshot.card_data.phase == DataLoadPhase.FAILED
        assert not table.loading
        assert table.row_count == 0
        assert "Card metadata failed to load: Scryfall is unavailable" in status
        assert "Press r to retry card metadata" in status
        assert "available only before draft start" in status
        assert "after draft start, retries are local-only" in status

        app.action_retry_card_data()
        for _ in range(20):
            await pilot.pause(0.05)
            if app.session.snapshot.card_data.phase == DataLoadPhase.READY:
                break

        assert calls == [("MSH", True), ("MSH", True)]
        assert app.session.snapshot.card_data.phase == DataLoadPhase.READY

        app.action_retry_card_data()
        await pilot.pause()
        assert "No recoverable card-data error to retry." in _status_text(app=app)


def test_tui_shows_actionable_error_when_card_metadata_load_fails(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_card_metadata_load_failure_is_visible(tmp_path=tmp_path))


def test_tui_slow_ratings_refresh_stays_responsive(tmp_path: Path) -> None:
    asyncio.run(_assert_slow_ratings_refresh_stays_responsive(tmp_path=tmp_path))


def test_tui_drops_session_publication_after_shutdown(tmp_path: Path) -> None:
    asyncio.run(_assert_tui_drops_session_publication_after_shutdown(tmp_path=tmp_path))


async def _assert_tui_drops_session_publication_after_shutdown(
    tmp_path: Path,
) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)):
        ratings_label = app.query_one("#ratings-download-label", Static)
        await ratings_label.remove()
        app._pick_label = "before-shutdown"
        app._session_error = "before-shutdown"
        app._running = False
        assert not app.is_running

        app._apply_session_snapshot(app.session.snapshot)
        app._publish_session_snapshot(app.session.snapshot)

        assert app._pick_label == "before-shutdown"
        assert app._session_error == "before-shutdown"


def test_tui_offers_missing_ratings_download_and_rescores_when_ready(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_missing_ratings_download_rescores_pack(tmp_path=tmp_path))


async def _assert_missing_ratings_download_rescores_pack(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def progress_loader(
        set_code: str,
        progress_callback: Callable[[SeventeenLandsDownloadProgress], None],
        *,
        refresh: bool,
    ) -> SeventeenLandsData:
        progress_callback(
            SeventeenLandsDownloadProgress(
                completed_requests=1,
                total_requests=4,
                message="Downloaded QuickDraft card ratings",
            )
        )
        started.set()
        release.wait(timeout=1.0)
        for completed, message in (
            (2, "Downloaded QuickDraft color ratings"),
            (3, "Downloaded PremierDraft card ratings"),
            (4, "Downloaded PremierDraft color ratings"),
        ):
            progress_callback(
                SeventeenLandsDownloadProgress(
                    completed_requests=completed,
                    total_requests=4,
                    message=message,
                )
            )
        return _graded_ratings_data(set_code)

    app = _tui_app(
        tmp_path=tmp_path,
        ratings_progress_loader=progress_loader,
        ratings_cache_checker=lambda set_code: False,
    )

    async with app.run_test(size=(120, 30)) as pilot:
        try:
            app.process_lines(lines=_first_pack_lines())
            await pilot.pause()

            assert isinstance(app.screen, MissingRatingsScreen)
            assert "No local 17Lands data for MSH" in str(
                app.screen.query_one("#missing-ratings-title", Static).content
            )
            assert not started.is_set()

            await pilot.click("#cancel-ratings-download")
            await pilot.pause()
            assert "Press d to download" in str(
                app.query_one("#ratings-download-label", Static).content
            )
            assert "Data: neutral prior (no local 17Lands data)" in _status_text(
                app=app
            )

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, MissingRatingsScreen)
            await pilot.click("#download-ratings")
            assert await asyncio.to_thread(started.wait, 0.5)
            await pilot.pause()

            progress_bar = app.query_one(
                "#ratings-download-progress",
                ProgressBar,
            )
            assert progress_bar.display
            assert progress_bar.progress == 1
            assert progress_bar.total == 4
            assert "1/4" in str(
                app.query_one("#ratings-download-label", Static).content
            )
            assert "Data: neutral prior (downloading ratings)" in _status_text(
                app=app
            )

            release.set()
            for _ in range(40):
                await pilot.pause(0.05)
                if (
                    app.session.snapshot.ratings.phase == DataLoadPhase.READY
                    and app.session.snapshot.ratings.total_cards == 14
                    and not progress_bar.display
                ):
                    break

            assert app.session.snapshot.ratings.phase == DataLoadPhase.READY
            assert app.session.snapshot.ratings.total_cards == 14
            assert "MSH" not in app.loading_rating_sets
            assert not progress_bar.display
            ready_notice = str(
                app.query_one("#ratings-download-label", Static).content
            )
            assert "17Lands data ready for MSH; scores recalculated" in ready_notice
            assert "5/14 offered cards have usable ratings" in ready_notice

            table = app.query_one("#pack-table", DataTable)
            rows = [table.get_row_at(index) for index in range(table.row_count)]
            assert any(str(row[1]) != "—" for row in rows)
            assert "Data: QuickDraft + neutral prior" in _status_text(app=app)
        finally:
            release.set()


async def _assert_slow_ratings_refresh_stays_responsive(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_loader(set_code: str) -> SeventeenLandsData:
        started.set()
        release.wait(timeout=5.0)
        return _ratings_data(set_code=set_code)

    app = _tui_app(tmp_path=tmp_path, ratings_loader=slow_loader)

    async with app.run_test(size=(120, 24)) as pilot:
        try:
            start = time.monotonic()
            app.process_lines(lines=_first_pack_lines())
            elapsed = time.monotonic() - start
            await pilot.pause()

            assert elapsed < 0.25
            assert await asyncio.to_thread(started.wait, 0.5)
            assert "MSH" in app.loading_rating_sets
            assert app.session.snapshot.ratings.phase == DataLoadPhase.LOADING
            assert app.session.snapshot.progress is not None
            assert app.session.snapshot.progress.operation == OperationKind.RATINGS

            await pilot.press("s")
            assert app.sort_mode == "win_rate"

            release.set()
            for _ in range(10):
                await pilot.pause(0.05)
                if "MSH" not in app.loading_rating_sets:
                    break

            assert "MSH" not in app.loading_rating_sets
            assert app.session.snapshot.ratings.phase == DataLoadPhase.READY
            assert app.session.snapshot.progress is None
        finally:
            release.set()


def test_tui_clears_automatic_ratings_progress_notice_when_ready(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_automatic_ratings_notice_clears(tmp_path=tmp_path))


async def _assert_automatic_ratings_notice_clears(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path, ratings_loader=_graded_ratings_data)

    async with app.run_test(size=(120, 30)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        for _ in range(20):
            await pilot.pause(0.05)
            if app.session.snapshot.ratings.phase == DataLoadPhase.READY:
                break

        assert app.session.snapshot.ratings.phase == DataLoadPhase.READY
        assert app.query_one("#ratings-download-panel").display is False
        assert "MSH" not in app._rating_notices_by_set


def test_tui_clears_session_error_after_successful_ratings_retry(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_ratings_retry_clears_session_error(tmp_path=tmp_path))


async def _assert_ratings_retry_clears_session_error(tmp_path: Path) -> None:
    load_count = 0

    def retrying_loader(set_code: str) -> SeventeenLandsData:
        nonlocal load_count
        load_count += 1
        if load_count == 1:
            raise RuntimeError("temporary ratings failure")

        return _graded_ratings_data(set_code)

    app = _tui_app(tmp_path=tmp_path, ratings_loader=retrying_loader)

    async with app.run_test(size=(120, 30)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        for _ in range(20):
            await pilot.pause(0.05)
            if app.session.snapshot.ratings.phase == DataLoadPhase.FAILED:
                break

        assert app.session.snapshot.ratings.phase == DataLoadPhase.FAILED
        assert "Error: 17Lands ratings failed for MSH" in _status_text(app=app)

        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, MissingRatingsScreen)
        await pilot.click("#download-ratings")
        for _ in range(20):
            await pilot.pause(0.05)
            if app.session.snapshot.ratings.phase == DataLoadPhase.READY:
                break

        assert load_count == 2
        assert app.session.snapshot.ratings.phase == DataLoadPhase.READY
        assert app.session.snapshot.errors == ()
        assert "temporary ratings failure" not in _status_text(app=app)
        assert "Error: 17Lands ratings failed for MSH" not in _status_text(app=app)


def test_tui_visibility_dialog_saves_preferences_and_restores_them_after_restart(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_visibility_dialog_persists_preferences(tmp_path=tmp_path))


async def _assert_visibility_dialog_persists_preferences(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 30)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()
        dialog = app.screen
        secondary_columns = dialog.query_one(
            "#visibility-secondary-columns",
            Switch,
        )
        secondary_columns.focus()
        await pilot.press("space")
        dialog.query_one("#visibility-account-identifier", Switch).value = False
        dialog.query_one("#visibility-attribution", Switch).value = False
        dialog.query_one("#visibility-card-image-preview", Select).value = "hide"
        await pilot.click("#save-visibility")
        await pilot.pause()

        assert app.show_secondary_columns is False
        assert app.visibility_preferences.account_identifier is False
        assert app.visibility_preferences.attribution is False
        assert app.visibility_preferences.card_image_preview == "hide"
        assert app.visible_column_keys == (
            "rank",
            "win_rate",
            "grade",
            "score",
            "card",
            "colors",
        )
        assert "Account:" not in _status_text(app=app)
        assert "Card data from 17Lands" not in _status_text(app=app)
        assert tui_preferences_path(app_dir=tmp_path / "app").exists()

    restarted_app = _tui_app(tmp_path=tmp_path)
    async with restarted_app.run_test(size=(120, 30)) as pilot:
        restarted_app.process_lines(lines=_first_pack_lines())
        await pilot.pause()

        assert restarted_app.show_secondary_columns is False
        assert restarted_app.visibility_preferences.account_identifier is False
        assert restarted_app.visibility_preferences.attribution is False
        assert restarted_app.visibility_preferences.card_image_preview == "hide"
        assert restarted_app.visible_column_keys == (
            "rank",
            "win_rate",
            "grade",
            "score",
            "card",
            "colors",
        )


def test_tui_config_persists_pack_and_build_preferences(tmp_path: Path) -> None:
    asyncio.run(_assert_config_persists_pack_and_build_preferences(tmp_path=tmp_path))


async def _assert_config_persists_pack_and_build_preferences(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        await _save_tui_config(
            app=app,
            pilot=pilot,
            secondary_columns=False,
        )
        assert app.show_secondary_columns is False

        await pilot.press("b")
        await pilot.pause()
        await _save_tui_config(
            app=app,
            pilot=pilot,
            build_details=True,
        )
        assert app.visibility_preferences.build_details is True
        assert "Build context" in app.build_view_text

    restarted_app = _tui_app(tmp_path=tmp_path)
    async with restarted_app.run_test(size=(140, 40)) as pilot:
        restarted_app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        assert restarted_app.show_secondary_columns is False
        await pilot.press("b")
        await pilot.pause()
        assert "Build context" in restarted_app.build_view_text


def test_tui_visibility_dialog_cancel_and_reset_do_not_apply_preferences(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_visibility_dialog_cancel_and_reset(tmp_path=tmp_path))


async def _assert_visibility_dialog_cancel_and_reset(tmp_path: Path) -> None:
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("c")
        await pilot.pause()
        dialog = app.screen
        secondary_columns = dialog.query_one(
            "#visibility-secondary-columns",
            Switch,
        )
        secondary_columns.focus()
        await pilot.press("space")
        assert secondary_columns.value is False

        await pilot.click("#reset-visibility")
        await pilot.pause()
        assert secondary_columns.value is True

        await pilot.click("#cancel-visibility")
        await pilot.pause()

        assert app.show_secondary_columns is True
        assert app.visibility_preferences == TuiVisibilityPreferences()
        assert not tui_preferences_path(app_dir=tmp_path / "app").exists()


def test_tui_visibility_preferences_filter_sidebar_build_and_image_elements(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_visibility_preferences_filter_elements(tmp_path=tmp_path))


async def _assert_visibility_preferences_filter_elements(tmp_path: Path) -> None:
    image_fetches: list[str] = []
    fixture_database = _fixture_card_database()

    def image_opener(request: object, **kwargs: object) -> BytesIO:
        image_fetches.append(str(getattr(request, "full_url")))
        raise AssertionError("hidden image previews must not fetch images")

    preferences = TuiVisibilityPreferences(
        build_details=True,
        pool_metadata=False,
        pool_color_distribution=False,
        pool_mana_curve=False,
        account_identifier=False,
        draft_identifier=False,
        mana_pips_and_sources=False,
        attribution=False,
        card_image_preview="hide",
    )
    app = _tui_app(
        tmp_path=tmp_path,
        card_database=CardDatabase(
            cards=fixture_database.cards,
            image_uris_by_name={"fixture spider": "https://cards.example/spider.jpg"},
        ),
        image_preview_enabled=True,
        card_image_opener=image_opener,
        visibility_preferences=preferences,
    )

    async with app.run_test(size=(140, 40)) as pilot:
        app.process_lines(lines=_first_pick_lines())
        await pilot.pause()

        pool_summary = app.query_one("#pool-summary", Static)
        assert pool_summary.display is False
        assert "Account:" not in _status_text(app=app)
        assert "Card data from 17Lands" not in _status_text(app=app)

        await pilot.press("b")
        await pilot.pause()

        assert "Build context" in app.build_view_text
        assert "Mana curve:" not in app.build_view_text
        assert "Pool: live draft" not in app.build_view_text
        assert "Pool size:" not in app.build_view_text
        assert "Account:" not in app.build_view_text
        assert "Draft:" not in app.build_view_text
        assert "Mana pips:" not in app.build_view_text
        assert "Mana sources:" not in app.build_view_text
        assert app.query_one("#card-image-preview", Static).display is False
        assert image_fetches == []


def test_tui_narrow_layout_does_not_change_enabled_visibility_preferences(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_narrow_layout_keeps_preferences(tmp_path=tmp_path))


async def _assert_narrow_layout_keeps_preferences(tmp_path: Path) -> None:
    preferences = TuiVisibilityPreferences()
    app = _tui_app(tmp_path=tmp_path, visibility_preferences=preferences)

    async with app.run_test(size=(60, 24)) as pilot:
        app.process_lines(lines=_first_pack_lines())
        await pilot.pause()

        assert app.visibility_preferences.secondary_columns is True
        assert app.visible_column_keys == (
            "rank",
            "win_rate",
            "grade",
            "score",
            "card",
            "colors",
        )
        assert app.query_one("#sidebar").display is True


def test_tui_shows_non_fatal_preferences_load_warning(tmp_path: Path) -> None:
    asyncio.run(_assert_preferences_load_warning_is_visible(tmp_path=tmp_path))


async def _assert_preferences_load_warning_is_visible(tmp_path: Path) -> None:
    path = tui_preferences_path(app_dir=tmp_path / "app")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    app = _tui_app(tmp_path=tmp_path)

    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()

        assert "Could not load TUI preferences" in _status_text(app=app)


async def _save_tui_config(
    *,
    app: DraftomenTuiApp,
    pilot: Pilot,
    **values: bool | str,
) -> None:
    await pilot.press("c")
    await pilot.pause()
    dialog = app.screen
    for field_name, value in values.items():
        control_id = f"#visibility-{field_name.replace('_', '-')}"
        if type(value) is bool:
            dialog.query_one(control_id, Switch).value = value
        else:
            dialog.query_one(control_id, Select).value = value
    await pilot.click("#save-visibility")
    await pilot.pause()


def _tui_app(
    *,
    tmp_path: Path,
    card_database: CardDatabase | None = None,
    set_card_data_loader: SetCardDataLoader | None = None,
    ratings_loader: Callable[[str], SeventeenLandsData] | None = None,
    ratings_progress_loader: RatingsProgressLoader | None = None,
    ratings_cache_checker: Callable[[str], bool] | None = None,
    image_preview_enabled: bool | None = None,
    mana_icons_enabled: bool = False,
    card_image_opener: Callable[..., object] | None = None,
    poll_enabled: bool = False,
    startup_scan: bool = False,
    poll_interval: float = 1.0,
    visibility_preferences: TuiVisibilityPreferences | None = None,
) -> DraftomenTuiApp:
    return DraftomenTuiApp(
        log_path=tmp_path / "Player.log",
        card_database=(
            None
            if set_card_data_loader is not None
            else card_database if card_database is not None else _fixture_card_database()
        ),
        set_card_data_loader=set_card_data_loader,
        app_dir=tmp_path / "app",
        ratings_loader=ratings_loader,
        ratings_progress_loader=ratings_progress_loader,
        ratings_cache_checker=ratings_cache_checker,
        poll_enabled=poll_enabled,
        startup_scan=startup_scan,
        poll_interval=poll_interval,
        image_preview_enabled=image_preview_enabled,
        mana_icons_enabled=mana_icons_enabled,
        card_image_service=(
            None
            if card_image_opener is None
            else CardImageService(
                cache_dir=card_image_cache_dir(app_dir=tmp_path / "app"),
                opener=card_image_opener,
            )
        ),
        visibility_preferences=visibility_preferences,
    )


def _fixture_card_database() -> CardDatabase:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)
    return replace(
        database,
        cards={
            grp_id: replace(card, set_code="msh")
            for grp_id, card in database.cards.items()
        },
    )


def _draft_state(
    *,
    account_id: str,
    draft_id: str,
    event_name: str,
    pool_grp_ids: tuple[int, ...],
    account_screen_name: str | None = None,
    picks: tuple[DraftPick, ...] = (),
) -> DraftState:
    now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC).isoformat()
    return DraftState(
        account_id=account_id,
        draft_id=draft_id,
        event_name=event_name,
        set_code="MSH",
        course_id=draft_id,
        started_at=now,
        updated_at=now,
        completed_at=now,
        completed=True,
        picks=picks,
        pool_grp_ids=pool_grp_ids,
        account_screen_name=account_screen_name,
    )


def _account_lines() -> list[str]:
    return FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()[:2]


def _account_lines_without_screen_name() -> list[str]:
    return [
        "[Accounts - Login] Logged in successfully. "
        "Display Name: FixturePlayer#12345",
        _auth_line_without_screen_name(),
    ]


def _auth_line_without_screen_name() -> str:
    return json.dumps(
        {
            "authenticateResponse": {
                "clientId": FIXTURE_ACCOUNT_ID,
                "sessionId": "00000000-0000-4000-8000-000000000002",
            },
        }
    )


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


def _course_snapshot_line(*, course_id: str) -> str:
    return json.dumps(
        {
            "Courses": [
                {
                    "CourseId": course_id,
                    "InternalEventName": "QuickDraft_MSH_20260702",
                    "CurrentModule": "CreateMatch",
                }
            ]
        }
    )


def _first_pack_lines() -> list[str]:
    return FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()[:7]


def _first_pick_lines() -> list[str]:
    return FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()[:10]


def _full_fixture_lines() -> list[str]:
    return FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()


def _status_text(*, app: DraftomenTuiApp) -> str:
    status = app.query_one("#status-bar", Static)
    return str(status.render())


def _pool_summary_text(*, app: DraftomenTuiApp) -> str:
    summary = app.query_one("#pool-summary", Static)
    return str(summary.render())


def _focused_card_text(*, app: DraftomenTuiApp) -> str:
    focused_card = app.query_one("#focused-card", Static)
    return str(focused_card.render())


def _card_image_preview_text(*, app: DraftomenTuiApp) -> str:
    preview = app.query_one("#card-image-preview", Static)
    return str(preview.render())


def _card_cells(*, rows: list[list[object]]) -> list[str]:
    return [str(row[4]) for row in rows]


def _close_pick_ratings_data(set_code: str) -> SeventeenLandsData:
    primary = SeventeenLandsFormatData(
        set_code=set_code,
        event_format=QUICK_DRAFT_FORMAT,
        fetched_at=datetime(2999, 1, 1, tzinfo=UTC),
        card_ratings={
            104894: _stats(
                grp_id=104894,
                name="Fixture Split Card",
                color="WU",
                gih=0.605,
                games_in_hand=900,
                alsa=1.2,
            ),
            105097: _stats(
                grp_id=105097,
                name="Fixture Spider",
                color="G",
                gih=0.604,
                games_in_hand=900,
                alsa=2.1,
            ),
        },
        pair_win_rates={},
    )
    return SeventeenLandsData(
        set_code=set_code,
        requested_format=QUICK_DRAFT_FORMAT,
        primary=primary,
        fallback=None,
        thin_sample_minimum=500,
    )


def _thin_ratings_data(set_code: str) -> SeventeenLandsData:
    primary = SeventeenLandsFormatData(
        set_code=set_code,
        event_format=QUICK_DRAFT_FORMAT,
        fetched_at=datetime(2999, 1, 1, tzinfo=UTC),
        card_ratings={
            104894: _stats(
                grp_id=104894,
                name="Fixture Split Card",
                color="WU",
                gih=None,
                games_in_hand=100,
                alsa=1.2,
            ),
        },
        pair_win_rates={},
    )
    return SeventeenLandsData(
        set_code=set_code,
        requested_format=QUICK_DRAFT_FORMAT,
        primary=primary,
        fallback=None,
        thin_sample_minimum=500,
    )


def _graded_ratings_data(set_code: str) -> SeventeenLandsData:
    primary = SeventeenLandsFormatData(
        set_code=set_code,
        event_format=QUICK_DRAFT_FORMAT,
        fetched_at=datetime(2999, 1, 1, tzinfo=UTC),
        card_ratings={
            104894: _stats(
                grp_id=104894,
                name="Fixture Split Card",
                color="WU",
                gih=0.62,
                games_in_hand=900,
                alsa=1.2,
            ),
            105097: _stats(
                grp_id=105097,
                name="Fixture Spider",
                color="G",
                gih=0.60,
                games_in_hand=900,
                alsa=2.1,
            ),
            104976: _stats(
                grp_id=104976,
                name="Fixture Red Card",
                color="R",
                gih=0.56,
                games_in_hand=900,
                alsa=4.0,
            ),
            105080: _stats(
                grp_id=105080,
                name="Fixture Black Card",
                color="B",
                gih=0.54,
                games_in_hand=900,
                alsa=5.0,
            ),
            104995: _stats(
                grp_id=104995,
                name="Fixture Filler Card",
                color="C",
                gih=0.52,
                games_in_hand=900,
                alsa=6.0,
            ),
        },
        pair_win_rates={},
    )
    return SeventeenLandsData(
        set_code=set_code,
        requested_format=QUICK_DRAFT_FORMAT,
        primary=primary,
        fallback=None,
        thin_sample_minimum=500,
    )


def _ratings_data(*, set_code: str) -> SeventeenLandsData:
    primary = SeventeenLandsFormatData(
        set_code=set_code,
        event_format=QUICK_DRAFT_FORMAT,
        fetched_at=datetime(2999, 1, 1, tzinfo=UTC),
        card_ratings={
            104894: _stats(
                grp_id=104894,
                name="Fixture Split Card",
                color="WU",
                gih=0.62,
                games_in_hand=900,
                alsa=1.2,
            ),
        },
        pair_win_rates={},
    )
    return SeventeenLandsData(
        set_code=set_code,
        requested_format=QUICK_DRAFT_FORMAT,
        primary=primary,
        fallback=None,
        thin_sample_minimum=500,
    )


def _stats(
    *,
    grp_id: int,
    name: str,
    color: str,
    gih: float | None,
    games_in_hand: int,
    alsa: float | None,
) -> SeventeenCardStats:
    return SeventeenCardStats(
        grp_id=grp_id,
        name=name,
        color=color,
        rarity="common",
        average_last_seen_at=alsa,
        gih_win_rate=gih,
        opening_hand_win_rate=None,
        drawn_improvement_win_rate=None,
        sample_counts=RatingSampleCounts(
            seen=1000,
            picked=500,
            games_played=games_in_hand,
            opening_hand=200,
            games_in_hand=games_in_hand,
        ),
    )
