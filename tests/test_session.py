from __future__ import annotations

import ast
import http.client
import io
import json
import threading
import urllib.error
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import draftomen.session as session_module
from draftomen.profile_client import (
    ProfileClient,
    ProfileRefreshOutcome,
    ProfileRefreshResult,
)
from draftomen.audit import load_draft_audit_records
from draftomen.backtest import (
    BacktestPickResult as DomainBacktestPickResult,
    BacktestReport as DomainBacktestReport,
)
from draftomen.card_data_client import CardDataClient
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.cardimages import CardImageError, CardImageService
from draftomen.events import (
    EXPECTED_PICKS_PER_PACK,
    EXPECTED_TOTAL_PICKS,
    AccountEvent,
    DraftCompletedEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
    QuickDraftDetectedEvent,
)
from draftomen.pickengine import (
    ColorCommitment,
    ContextualScoreBreakdown,
    PickScoringContext,
    ScoreNormalization,
    ScoredCard,
    ScoredPack,
    build_pick_scoring_context,
)
from draftomen.pool import (
    DraftPick,
    DraftPoolStore,
    DraftState,
    draft_state_path,
    load_draft_state,
    save_draft_state,
)
from draftomen.session import (
    AccountIdentity,
    ApplicationPhase,
    ApplicationStatus,
    BacktestPickResult,
    BacktestResult,
    BuildCard,
    BuildLand,
    BuildPairOption,
    BuildResult,
    CardDataState,
    CardImageState,
    CardView,
    ChangeRanking,
    ChangeSplashPreference,
    ChooseAccount,
    ChooseRecommendation,
    DataLoadPhase,
    DismissError,
    DraftIdentity,
    FocusBuildCard,
    LiveSession,
    LOG_SETUP_GUIDANCE,
    LiveSessionCommand,
    LiveSessionEvent,
    LiveSessionSnapshot,
    OperationKind,
    PoolCard,
    PoolState,
    ProfileRefreshRequest,
    ProgressState,
    RatingsLoader,
    RatingsState,
    Recommendation,
    RecommendationState,
    RequestBacktest,
    RequestBuild,
    RequestRatingsDownload,
    RetryError,
    SetProfileState,
    SessionError,
)
from draftomen.seventeen import (
    DownloadProgressCallback,
    PREMIER_DRAFT_FORMAT,
    QUICK_DRAFT_FORMAT,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsData,
    SeventeenLandsDownloadProgress,
    SeventeenLandsError,
    SeventeenLandsFormatData,
    load_or_refresh_17lands_data,
    save_17lands_format_data,
)
from draftomen.set_profile import (
    SetProfile,
    dump_set_profile,
    load_set_profile,
    set_profile_path,
)
from draftomen.set_card_data import SetCardData
from draftomen.splash import SplashState

PROJECT_ROOT = Path(__file__).parent.parent
FIXTURE_LOG_PATH = PROJECT_ROOT / "tests" / "fixtures" / "quick-draft-msh-player.log"
FIXTURE_PROFILE_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "set-profiles" / "mature.json"
)
FIXTURE_ACCOUNT_ID = "FIXTURECLIENTID1234567890"
FIXTURE_DRAFT_ID = "00000000-0000-4000-8000-000000000004"
CONTEXT_EVENT_NAME = "QuickDraft_TST_20260829"
CONTEXT_PACK_NUMBER = 1
CONTEXT_PICK_NUMBER = 2
CONTEXT_OFFERED_GRP_IDS = (104894, 104976)


def test_default_live_session_snapshot_has_neutral_initial_state() -> None:
    snapshot = LiveSessionSnapshot()

    assert snapshot.status == ApplicationStatus(
        phase=ApplicationPhase.STARTING,
        message="Starting Draft Omen.",
    )
    assert snapshot.accounts == ()
    assert snapshot.active_account is None
    assert snapshot.draft is None
    assert snapshot.card_data == CardDataState()
    assert snapshot.ratings == RatingsState()
    assert snapshot.set_profile == SetProfileState()
    assert snapshot.recommendations == RecommendationState()
    assert snapshot.pool == PoolState()
    assert snapshot.progress is None
    assert snapshot.errors == ()
    assert snapshot.build is None
    assert snapshot.backtest is None


def test_card_view_forwards_authoritative_unknown_flag() -> None:
    card = CardInfo(
        grp_id=9001,
        name="Partial Arena Card",
        colors=("R",),
        mana_value=3.0,
        rarity="rare",
        types=("Unknown",),
        unknown=True,
    )

    view = session_module._card_view(card=card)

    assert view.unknown is True


def test_live_session_exposes_injected_card_database_update_time(
    tmp_path: Path,
) -> None:
    timestamp = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=replace(_fixture_card_database(), generated_at=timestamp),
    )

    assert session.snapshot.card_data.last_successful_update == (
        timestamp.isoformat()
    )


def test_pool_aggregates_apply_card_quantities_and_skip_lands() -> None:
    colorless_card = replace(_card(), grp_id=124, colors=(), mana_value=None)
    blue_card = replace(_card(), grp_id=125, colors=("U",), mana_value=7.0)
    land_card = replace(
        _card(),
        grp_id=126,
        types=("Basic Land",),
        mana_cost=None,
        mana_value=0.0,
    )
    cards = (
        PoolCard(card=_card(), quantity=2),
        PoolCard(card=colorless_card, quantity=3),
        PoolCard(card=blue_card, quantity=1),
        PoolCard(card=land_card, quantity=4),
    )

    color_distribution, mana_curve, average_mana_value = LiveSession._pool_aggregates(
        cards=cards
    )

    assert color_distribution == (
        ("W", 6),
        ("U", 1),
        ("B", 0),
        ("R", 0),
        ("G", 0),
        ("C", 3),
    )
    assert mana_curve == (0, 0, 2, 0, 0, 0, 1)
    assert average_mana_value == pytest.approx(11 / 3)


def test_pool_state_publishes_canonical_commitment_colors(
    tmp_path: Path,
) -> None:
    commitment = ColorCommitment(
        pick_index=7,
        pool_size=6,
        color_weights=(
            ("W", 3.0),
            ("U", 2.0),
            ("B", 0.0),
            ("R", 0.0),
            ("G", 0.0),
        ),
        inferred_pair="WU",
        level=0.5,
    )
    scored_pack = ScoredPack(
        cards=(),
        normalization=ScoreNormalization(
            lower_rating=0.0,
            upper_rating=1.0,
            neutral_rating=0.5,
        ),
        source_summary="fixture",
        commitment=commitment,
        splash_state=SplashState(
            enabled=True,
            base_pair="WU",
            active_color=None,
            picked_card_count=6,
            fixing_sources=(),
            aggressive=False,
        ),
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        card_database=CardDatabase(cards={}),
    )

    pool = session._pool_state(pool_grp_ids=(), scored_pack=scored_pack)

    assert pool.current_colors == ("W", "U")


def test_live_session_snapshot_covers_complete_frontend_state_immutably() -> None:
    card = _card()
    account = AccountIdentity(account_id="account-1", screen_name="Player#12345")
    draft = DraftIdentity(
        account_id=account.account_id,
        draft_id="draft-1",
        event_name="QuickDraft_TST_20260823",
        set_code="TST",
        course_id="course-1",
        pack_number=0,
        pick_number=1,
        completed=False,
    )
    recommendation = Recommendation(
        rank=1,
        card=card,
        score=87,
        win_rate=0.61,
        average_last_seen_at=2.4,
        source_label="Quick Draft",
        color_fit="on color",
        no_data=False,
        letter_grade="A-",
        explanation="Best pool-aware score.",
    )
    pool_card = PoolCard(card=card, quantity=2)
    build_card = BuildCard(card=card, quantity=2)
    build = BuildResult(
        selected_pair="WU",
        pair_options=(
            BuildPairOption(
                pair="WU",
                score=81.5,
                selected=True,
                automatic=True,
            ),
        ),
        spells=(build_card,),
        lands=(
            BuildLand(
                name="Plains",
                quantity=9,
                source_colors=("W",),
            ),
        ),
        bench=(),
        deck_size=40,
        warnings=("Fixture warning",),
        spell_count=23,
        land_count=17,
        creature_count=2,
        instant_count=0,
    )
    backtest = BacktestResult(
        ranking_mode="score",
        rows=(
            BacktestPickResult(
                pack_number=0,
                pick_number=0,
                recommended=card,
                actual=card,
                match=True,
                skipped_reason=None,
                data_source="Quick Draft",
            ),
        ),
        match_count=1,
        compared_count=1,
        skipped_count=0,
        data_sources=("Quick Draft",),
    )
    snapshot = LiveSessionSnapshot(
        status=ApplicationStatus(
            phase=ApplicationPhase.DRAFTING,
            message="Pack 1, pick 2",
        ),
        accounts=(account,),
        active_account=account,
        draft=draft,
        recommendations=RecommendationState(
            ranking_mode="score",
            cards=(recommendation,),
            selected_grp_id=card.grp_id,
            source_summary="Quick Draft",
        ),
        pool=PoolState(
            cards=(pool_card,),
            total_cards=2,
            target_cards=42,
            inferred_pair="WU",
            current_colors=("W", "U"),
            commitment=0.5,
        ),
        progress=ProgressState(
            operation=OperationKind.RATINGS,
            message="Downloading ratings",
            completed=1,
            total=2,
        ),
        errors=(
            SessionError(
                error_id="ratings-1",
                code="ratings_unavailable",
                message="Ratings are temporarily unavailable.",
                recoverable=True,
                operation=OperationKind.RATINGS,
            ),
        ),
        build=build,
        backtest=backtest,
    )

    assert snapshot.active_account == account
    assert snapshot.draft == draft
    assert snapshot.recommendations.cards == (recommendation,)
    assert snapshot.pool.cards == (pool_card,)
    assert snapshot.pool.current_colors == ("W", "U")
    assert snapshot.pool.target_cards == 42
    assert snapshot.progress is not None
    assert snapshot.progress.completed == 1
    assert snapshot.errors[0].recoverable is True
    assert snapshot.build == build
    assert snapshot.backtest == backtest
    with pytest.raises(FrozenInstanceError):
        snapshot.status = ApplicationStatus()
    with pytest.raises(FrozenInstanceError):
        snapshot.pool.total_cards = 3


def test_live_session_commands_capture_explicit_user_intentions() -> None:
    commands: tuple[LiveSessionCommand, ...] = (
        ChooseAccount(account_id="account-1"),
        ChooseRecommendation(grp_id=123),
        FocusBuildCard(grp_id=456),
        ChangeRanking(ranking_mode="win_rate"),
        ChangeSplashPreference(enabled=False),
        RequestRatingsDownload(set_code="TST"),
        RequestBuild(pair_override="WU", allow_splash=False),
        RequestBacktest(account_id="account-1", draft_id="draft-1"),
        DismissError(error_id="ratings-1"),
        RetryError(error_id="ratings-1"),
    )

    assert commands == (
        ChooseAccount(account_id="account-1"),
        ChooseRecommendation(grp_id=123),
        FocusBuildCard(grp_id=456),
        ChangeRanking(ranking_mode="win_rate"),
        ChangeSplashPreference(enabled=False),
        RequestRatingsDownload(set_code="TST"),
        RequestBuild(pair_override="WU", allow_splash=False),
        RequestBacktest(account_id="account-1", draft_id="draft-1"),
        DismissError(error_id="ratings-1"),
        RetryError(error_id="ratings-1"),
    )
    with pytest.raises(FrozenInstanceError):
        commands[0].account_id = "account-2"


def test_session_contract_does_not_import_frontend_frameworks() -> None:
    source = (PROJECT_ROOT / "draftomen" / "session.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_roots = {
        name.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for name in node.names
    }
    imported_roots.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert imported_roots.isdisjoint(
        {"PyQt6", "PySide6", "qml", "rich", "textual"}
    )


@pytest.mark.parametrize("unreadable", (False, True))
def test_live_session_guides_when_player_log_is_missing_or_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unreadable: bool,
) -> None:
    log_path = tmp_path / "Player.log"
    if unreadable:
        log_path.touch()
        monkeypatch.setattr(
            session_module,
            "is_log_readable",
            lambda *, path: False,
        )

    session = LiveSession(log_path=log_path, app_dir=tmp_path / "app")
    status = session.snapshot.status

    assert status.phase == ApplicationPhase.WAITING_FOR_DRAFT
    assert status.setup_guidance is True
    assert status.message == LOG_SETUP_GUIDANCE
    for requirement in (
        "No draft or readable Player.log",
        "Detailed Logs (Plugin Support)",
        "Account settings",
        "platform or Arena version",
        "Restart Arena if required",
        "return to Draft Omen and try again while Arena is running",
    ):
        assert requirement in status.message
    assert str(log_path) not in status.message


def test_live_session_uses_ordinary_waiting_for_readable_empty_log(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")

    session = LiveSession(log_path=log_path, app_dir=tmp_path / "app")

    assert session.snapshot.status == ApplicationStatus(
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message="Waiting for a Quick Draft.",
        setup_guidance=False,
    )


@pytest.mark.parametrize("unreadable", (False, True))
def test_live_session_clears_log_guidance_when_log_becomes_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unreadable: bool,
) -> None:
    log_path = tmp_path / "Player.log"
    if unreadable:
        log_path.touch()
        monkeypatch.setattr(
            session_module,
            "is_log_readable",
            lambda *, path: False,
        )
    session = LiveSession(log_path=log_path, app_dir=tmp_path / "app")
    assert session.snapshot.status.setup_guidance is True

    log_path.write_text("", encoding="utf-8")
    if unreadable:
        monkeypatch.setattr(
            session_module,
            "is_log_readable",
            lambda *, path: True,
        )
    snapshot = session.poll_once()

    assert snapshot.status == ApplicationStatus(
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message="Waiting for a Quick Draft.",
        setup_guidance=False,
    )


def test_live_session_poll_refresh_preserves_concurrent_command_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")
    session = LiveSession(log_path=log_path, app_dir=tmp_path / "app")
    command_started = False

    def unreadable_after_command(*, path: Path) -> bool:
        nonlocal command_started
        if not command_started:
            command_started = True
            session.dispatch(command=ChangeRanking(ranking_mode="win_rate"))
        return False

    monkeypatch.setattr(
        session_module,
        "is_log_readable",
        unreadable_after_command,
    )
    monkeypatch.setattr(
        session.follower,
        "poll",
        lambda: pytest.fail("setup guidance should skip strict follower polling"),
    )

    snapshot = session.poll_once()

    assert snapshot.status.setup_guidance is True
    assert snapshot.recommendations.ranking_mode == "win_rate"
    assert session.snapshot is snapshot


def test_live_session_refreshes_setup_guidance_after_neutral_login_context(
    tmp_path: Path,
) -> None:
    session = LiveSession(log_path=tmp_path / "Player.log", app_dir=tmp_path / "app")
    session.process_lines(
        lines=(
            _auth_line(account_id="first-account", screen_name="First"),
            _course_line(event_name="QuickDraft_ONE_20260823", course_id="first-draft"),
        )
    )

    session.process_lines(
        lines=(
            "[Accounts - Login] Logged in successfully. Display Name: Second#12345",
            _auth_line(account_id="second-account", screen_name="Second"),
        )
    )
    session.dispatch(command=ChooseAccount(account_id="second-account"))

    assert session.snapshot.status == ApplicationStatus(
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message="Waiting for a Quick Draft.",
    )
    snapshot = session.poll_once()

    assert snapshot.status.setup_guidance is True
    assert snapshot.active_account == AccountIdentity(
        account_id="second-account",
        screen_name="Second",
    )


def test_live_session_draft_event_overrides_log_setup_guidance(
    tmp_path: Path,
) -> None:
    session = LiveSession(log_path=tmp_path / "Player.log", app_dir=tmp_path / "app")
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()

    snapshot = session.process_lines(lines=fixture_lines[:3])

    assert snapshot.status == ApplicationStatus(
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message="Preparing Quick Draft data for MSH.",
        setup_guidance=False,
    )
    assert session.poll_once() is snapshot


def test_live_session_polling_and_rotation_publish_complete_persisted_lifecycle(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    published: list[LiveSessionSnapshot] = []
    log_path.write_text("", encoding="utf-8")
    session = LiveSession(
        log_path=log_path,
        app_dir=app_dir,
        poll_interval=0.01,
        snapshot_publisher=published.append,
    )

    _write_lines(path=log_path, lines=fixture_lines[:20])
    first_snapshot = session.poll_once()
    _append_lines(path=log_path, lines=fixture_lines[20:70])
    log_path.rename(previous_log_path)
    _write_lines(path=log_path, lines=fixture_lines[70:])
    completed_snapshot = session.poll_once()
    unchanged_snapshot = session.poll_once()

    assert first_snapshot.status.phase == ApplicationPhase.DRAFTING
    assert first_snapshot.active_account == AccountIdentity(
        account_id=FIXTURE_ACCOUNT_ID,
        screen_name="FixturePlayer",
    )
    assert completed_snapshot.status == ApplicationStatus(
        phase=ApplicationPhase.DRAFT_COMPLETE,
        message="Draft complete.",
    )
    assert completed_snapshot.draft is not None
    assert completed_snapshot.draft.draft_id == FIXTURE_DRAFT_ID
    assert completed_snapshot.draft.pack_number == 2
    assert completed_snapshot.draft.pick_number == 13
    assert completed_snapshot.draft.completed is True
    assert completed_snapshot.pool.total_cards == 42
    assert unchanged_snapshot is completed_snapshot
    assert published[-1] is completed_snapshot
    assert all(isinstance(snapshot, LiveSessionSnapshot) for snapshot in published)

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
    assert audit_records[0]["record_type"] == "draft_started"
    assert sum(record["record_type"] == "choice_made" for record in audit_records) == 42
    assert audit_records[-1]["record_type"] == "draft_completed"


def test_live_session_publishes_consumed_events_with_resulting_state(
    tmp_path: Path,
) -> None:
    published: list[LiveSessionEvent] = []
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        event_publisher=published.append,
    )
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()

    session.process_lines(lines=fixture_lines[:7])

    assert [type(item.event) for item in published] == [
        AccountEvent,
        QuickDraftDetectedEvent,
        DraftStartedEvent,
        PackOfferedEvent,
    ]
    assert published[-1].snapshot is session.snapshot
    assert published[-1].scored_pack is not None
    assert published[-1].snapshot.recommendations.cards


def test_live_session_profiled_scoring_publishes_context_and_matching_evidence(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    pool_before_pick = _fixture_pool_before_pick(
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        set_profile=profile,
    )

    snapshot = session.process_lines(
        lines=_profiled_history_lines(pool_before_pick=pool_before_pick)
    )

    event = snapshot.current_pack_event
    scored_pack = snapshot.current_scored_pack
    assert event is not None
    assert scored_pack is not None
    context = _assert_profile_context(
        scored_pack=scored_pack,
        profile=profile,
        event=event,
    )
    scored_cards = _assert_recommendation_context_parity(
        snapshot=snapshot,
        scored_pack=scored_pack,
    )
    assert any(
        recommendation.contextual_pair == "WU"
        and recommendation.contextual_theme == "tempo flyers"
        and recommendation.contextual_profile_maturity == "mature"
        and recommendation.contextual_profile_confidence == pytest.approx(0.91)
        for recommendation in snapshot.recommendations.cards
    )

    audit_records = load_draft_audit_records(
        account_id="profiled-account",
        draft_id="profiled-draft",
        app_dir=tmp_path / "app",
    )
    decision = next(
        record
        for record in reversed(audit_records)
        if record["record_type"] == "decision_evaluated"
    )
    assert decision["pool_before_pick"] == list(pool_before_pick)
    assert "pool_before_pick" not in decision["context_provenance"]
    assert decision["context_provenance"]["stage"] == {
        "pack_number": context.stage.pack_number,
        "pick_number": context.stage.pick_number,
        "global_pick_index": context.stage.global_pick_index,
        "estimated_remaining_picks": context.stage.estimated_remaining_picks,
    }
    recommended_id = decision["recommended_grp_id"]
    assert recommended_id is not None
    source_card = scored_cards[recommended_id]
    recommendation_payload = decision["recommendation"]
    assert recommendation_payload["contextual_breakdown"] == (
        source_card.contextual_breakdown.to_json()
    )
    assert recommendation_payload["contextual_evidence"] == list(
        source_card.contextual_evidence
    )
    assert recommendation_payload["contextual_pair"] == source_card.contextual_pair
    assert recommendation_payload["contextual_theme"] == source_card.contextual_theme
    assert recommendation_payload["contextual_profile_maturity"] == (
        source_card.contextual_profile_maturity
    )
    assert recommendation_payload["contextual_profile_confidence"] == (
        source_card.contextual_profile_confidence
    )


def test_live_session_preloads_profile_before_first_score(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    app_dir = tmp_path / "app"
    dump_set_profile(
        profile,
        set_profile_path(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )
    pool_before_pick = _fixture_pool_before_pick(
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
    )
    published: list[LiveSessionSnapshot] = []
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
        snapshot_publisher=published.append,
    )

    snapshot = session.process_lines(
        lines=_profiled_history_lines(pool_before_pick=pool_before_pick)
    )

    event = snapshot.current_pack_event
    scored_pack = snapshot.current_scored_pack
    assert event is not None
    assert scored_pack is not None
    _assert_profile_context(scored_pack=scored_pack, profile=profile, event=event)
    first_scored_index = next(
        index
        for index, published_snapshot in enumerate(published)
        if published_snapshot.current_scored_pack is not None
    )
    assert any(
        published_snapshot.set_profile.set_code == event.set_code
        and published_snapshot.set_profile.phase is DataLoadPhase.READY
        for published_snapshot in published[:first_scored_index]
    )
    assert snapshot.set_profile.source == "local-mature"
    _assert_recommendation_context_parity(
        snapshot=snapshot,
        scored_pack=scored_pack,
    )


def test_live_session_auto_profile_is_cached_per_set_across_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _fixture_set_profile()
    app_dir = tmp_path / "app"
    profile_path = set_profile_path(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        app_dir=app_dir,
    )
    dump_set_profile(profile, profile_path)
    load_calls: list[tuple[str, str, Path]] = []
    real_loader = session_module.load_scoring_profile

    def load_profile(
        set_code: str,
        event_format: str,
        *,
        app_dir: Path,
    ) -> SetProfile | None:
        load_calls.append((set_code, event_format, app_dir))
        return real_loader(set_code, event_format, app_dir=app_dir)

    monkeypatch.setattr(session_module, "load_scoring_profile", load_profile)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
    )

    session._set_active_set_code(set_code="tst")
    assert session.snapshot.set_profile.set_code == "TST"
    assert session.snapshot.set_profile.phase is DataLoadPhase.READY
    profile_path.unlink()
    session._set_active_set_code(set_code=None)
    session._set_active_set_code(set_code="TST")
    assert session.snapshot.set_profile.source == "local-mature"
    assert session.snapshot.set_profile.phase is DataLoadPhase.READY

    assert load_calls == [("TST", QUICK_DRAFT_FORMAT, app_dir)]
    snapshot = session.process_lines(
        lines=(
            _profiled_pack_line(
                pool_before_pick=_fixture_pool_before_pick(
                    pack_number=CONTEXT_PACK_NUMBER,
                    pick_number=CONTEXT_PICK_NUMBER,
                )
            ),
        )
    )
    event = snapshot.current_pack_event
    scored_pack = snapshot.current_scored_pack
    assert event is not None
    assert scored_pack is not None
    _assert_profile_context(
        scored_pack=scored_pack,
        profile=profile,
        event=event,
    )
    assert any(
        recommendation.contextual_pair == "WU"
        and recommendation.contextual_theme == "tempo flyers"
        and recommendation.contextual_profile_maturity == "mature"
        and recommendation.contextual_profile_confidence == pytest.approx(0.91)
        for recommendation in snapshot.recommendations.cards
    )


def test_live_session_explicit_profile_remains_authoritative_over_local_profile(
    tmp_path: Path,
) -> None:
    explicit_profile = _fixture_set_profile()
    local_profile = replace(explicit_profile, confidence=0.12)
    app_dir = tmp_path / "app"
    dump_set_profile(
        local_profile,
        set_profile_path(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        set_profile=explicit_profile,
    )

    session._set_active_set_code(set_code="TST")

    assert session._set_profile is explicit_profile
    assert session._set_profiles_by_set == {}


class _ProfileClientStub:
    manifest_url = "https://profiles.example.test/manifest.json"
    network_policy = "allowed"

    def __init__(self, profiles: dict[str, SetProfile | None]) -> None:
        self.profiles = profiles
        self.load_calls: list[tuple[str, str]] = []

    def load_cached(self, set_code: str, event_format: str, **kwargs):
        del kwargs
        self.load_calls.append((set_code, event_format))
        profile = self.profiles.get(set_code)
        if profile is None:
            profile = SetProfile.generic(
                set_code=set_code,
                event_format=event_format,
            )
            source = "generic"
        else:
            source = f"local-{profile.maturity.value}"
        return SimpleNamespace(profile=profile, source=source)




def test_live_session_cold_start_loads_bundled_profile_without_network(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    assert not app_dir.exists()
    network_calls: list[object] = []

    def unexpected_network_call(*args: object, **kwargs: object) -> None:
        network_calls.append((args, kwargs))
        raise AssertionError("bundled profile cold start must remain offline")

    client = ProfileClient(
        app_dir=app_dir,
        opener=unexpected_network_call,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        profile_client=client,
    )

    session._set_active_set_code(set_code="hob")
    state = session.snapshot.set_profile

    assert state.set_code == "HOB"
    assert state.event_format == QUICK_DRAFT_FORMAT
    assert state.maturity == "metadata-only"
    assert state.profile_version == "1.0"
    assert state.source == "bundled-metadata-only"
    assert state.phase is DataLoadPhase.READY
    assert network_calls == []
    assert session.profile_refresh_request() is None
    assert not client.profile_path("hob", QUICK_DRAFT_FORMAT).exists()


def test_live_session_profile_activation_is_local_first_and_queues_one_request(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    client = _ProfileClientStub({"TST": profile})
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        profile_client=client,
    )

    session._set_active_set_code(set_code="tst")
    request = session.profile_refresh_request()

    assert client.load_calls == [("TST", QUICK_DRAFT_FORMAT)]
    assert session.snapshot.set_profile.maturity == "mature"
    assert session.snapshot.set_profile.profile_version == profile.profile_version
    assert request is not None
    assert request.set_code == "TST"
    assert request.event_format == QUICK_DRAFT_FORMAT
    assert session.profile_refresh_request() is request


def test_live_session_profile_switch_retires_stale_refresh_request(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    client = _ProfileClientStub({"TST": profile})
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        profile_client=client,
    )

    session._set_active_set_code(set_code="TST")
    old_request = session.profile_refresh_request()
    assert old_request is not None
    session._set_active_set_code(set_code="ABC")
    new_request = session.profile_refresh_request()
    assert new_request is not None
    assert new_request != old_request

    session.complete_profile_refresh(
        request=old_request,
        result=ProfileRefreshResult(
            profile=profile,
            outcome=ProfileRefreshOutcome.UPDATED,
        ),
    )

    assert session.snapshot.set_profile.set_code == "ABC"
    assert session.profile_refresh_request() is new_request


def test_live_session_newer_profile_result_updates_state_and_scores_current_pack(
    tmp_path: Path,
) -> None:
    client = _ProfileClientStub({})
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        profile_client=client,
    )
    snapshot = session.process_lines(
        lines=_profiled_history_lines(
            pool_before_pick=_fixture_pool_before_pick(
                pack_number=CONTEXT_PACK_NUMBER,
                pick_number=CONTEXT_PICK_NUMBER,
            )
        )
    )
    request = session.profile_refresh_request()
    assert request is not None
    newer = _fixture_set_profile()
    newer = replace(newer, profile_version="2.0")

    session.complete_profile_refresh(
        request=request,
        result=ProfileRefreshResult(
            profile=newer,
            outcome=ProfileRefreshOutcome.UPDATED,
        ),
    )

    assert session.snapshot.set_profile.profile_version == "2.0"
    assert session.snapshot.set_profile.refresh_outcome == "updated"
    assert session.snapshot.current_scored_pack is not snapshot.current_scored_pack
    assert session.snapshot.current_scored_pack is not None
    assert session.snapshot.current_scored_pack.scoring_context is not None
    assert session.snapshot.current_scored_pack.scoring_context.set_profile == newer


@pytest.mark.parametrize(
    "outcome",
    (ProfileRefreshOutcome.UNCHANGED, ProfileRefreshOutcome.CACHED),
)
def test_live_session_adopts_newer_external_cached_profile_and_rescores(
    tmp_path: Path,
    outcome: ProfileRefreshOutcome,
) -> None:
    profile = _fixture_set_profile()
    client = _ProfileClientStub({"TST": profile})
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        profile_client=client,
    )
    snapshot = session.process_lines(
        lines=_profiled_history_lines(
            pool_before_pick=_fixture_pool_before_pick(
                pack_number=CONTEXT_PACK_NUMBER,
                pick_number=CONTEXT_PICK_NUMBER,
            )
        )
    )
    request = session.profile_refresh_request()
    assert request is not None
    newer = replace(
        profile,
        profile_version="2.0",
        generated_at="2026-08-30T00:00:00+00:00",
    )

    session.complete_profile_refresh(
        request=request,
        result=ProfileRefreshResult(profile=newer, outcome=outcome),
    )

    assert session._set_profile is newer
    assert session.snapshot.set_profile.profile_version == "2.0"
    assert session.snapshot.set_profile.refresh_outcome == outcome.value
    assert session.snapshot.current_scored_pack is not snapshot.current_scored_pack
    assert session.snapshot.current_scored_pack is not None
    assert session.snapshot.current_scored_pack.scoring_context is not None
    assert session.snapshot.current_scored_pack.scoring_context.set_profile == newer


def test_live_session_profile_unchanged_and_failed_refreshes_retain_last_good_state(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    client = _ProfileClientStub({"TST": profile})
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        profile_client=client,
    )
    session._set_active_set_code(set_code="TST")
    request = session.profile_refresh_request()
    assert request is not None
    session.complete_profile_refresh(
        request=request,
        result=ProfileRefreshResult(
            profile=profile,
            outcome=ProfileRefreshOutcome.UNCHANGED,
        ),
    )
    unchanged = session.snapshot
    assert session._set_profile is profile
    assert unchanged.set_profile.refresh_outcome == "unchanged"
    assert unchanged.set_profile.phase is DataLoadPhase.READY

    session._set_active_set_code(set_code=None)
    session._set_active_set_code(set_code="TST")
    request = session.profile_refresh_request()
    assert request is not None
    session.fail_profile_refresh(request=request, error_message="private diagnostic")

    assert session._set_profile is profile
    assert session.snapshot.set_profile.profile_version == profile.profile_version
    assert session.snapshot.set_profile.phase is DataLoadPhase.FAILED
    assert session.snapshot.set_profile.refresh_outcome == "remote-failed"
    assert "private diagnostic" not in session.snapshot.set_profile.message


def test_live_session_explicit_profile_is_authoritative_without_refresh_request(
    tmp_path: Path,
) -> None:
    explicit = _fixture_set_profile()
    client = _ProfileClientStub({"TST": replace(explicit, profile_version="local")})
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        set_profile=explicit,
        profile_client=client,
    )

    session._set_active_set_code(set_code="TST")

    assert client.load_calls == []
    assert session.profile_refresh_request() is None
    assert session._set_profile is explicit
    assert session.snapshot.set_profile.source == "injected"


def test_live_session_recovered_profiled_pack_uses_shared_context(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    pool_before_pick = _fixture_pool_before_pick(
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
    )
    pending_pick = DraftPick(
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
        offered_grp_ids=CONTEXT_OFFERED_GRP_IDS,
        pool_before_pick=pool_before_pick,
    )
    state = replace(
        _draft_state(
            account_id="recovered-account",
            screen_name="Recovered",
            draft_id="recovered-draft",
            updated_at="2026-08-29T10:00:00+00:00",
            pool_grp_ids=pool_before_pick,
        ),
        picks=(pending_pick,),
    )
    app_dir = tmp_path / "app"
    save_draft_state(state=state, app_dir=app_dir)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
        set_profile=profile,
    )

    snapshot = session.process_lines(
        lines=(_auth_line(account_id="recovered-account", screen_name="Recovered"),)
    )

    event = snapshot.current_pack_event
    scored_pack = snapshot.current_scored_pack
    assert event is not None
    assert event.pool_grp_ids == pending_pick.pool_before_pick
    assert scored_pack is not None
    context = _assert_profile_context(
        scored_pack=scored_pack,
        profile=profile,
        event=event,
    )
    assert snapshot.recommendations.cards
    assert snapshot.recommendations.cards[0].contextual_pair == (
        scored_pack.cards[0].contextual_pair
    )


def test_live_session_accountless_profiled_pack_uses_shared_context(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    pool_before_pick = _fixture_pool_before_pick(
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        set_profile=profile,
    )

    snapshot = session.process_lines(
        lines=(
            _profiled_pack_line(pool_before_pick=pool_before_pick),
        )
    )

    assert snapshot.active_account is None
    event = snapshot.current_pack_event
    scored_pack = snapshot.current_scored_pack
    assert event is not None
    assert event.pool_grp_ids == pool_before_pick
    assert scored_pack is not None
    context = _assert_profile_context(
        scored_pack=scored_pack,
        profile=profile,
        event=event,
    )
    assert snapshot.recommendations.cards
    assert any(
        recommendation.contextual_pair == "WU"
        for recommendation in snapshot.recommendations.cards
    )


def test_live_session_without_profile_keeps_generic_scoring(
    tmp_path: Path,
) -> None:
    pool_before_pick = _fixture_pool_before_pick(
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
    )

    snapshot = session.process_lines(
        lines=(
            _profiled_pack_line(pool_before_pick=pool_before_pick),
        )
    )

    scored_pack = snapshot.current_scored_pack
    assert scored_pack is not None
    assert scored_pack.scoring_context is None
    assert snapshot.recommendations.cards
    assert all(
        recommendation.contextual_breakdown == ContextualScoreBreakdown()
        and recommendation.contextual_evidence == ()
        and recommendation.contextual_pair is None
        and recommendation.contextual_theme is None
        and recommendation.contextual_profile_maturity is None
        and recommendation.contextual_profile_confidence is None
        for recommendation in snapshot.recommendations.cards
    )


def test_session_backtest_projection_preserves_domain_context_fields() -> None:
    profile = _fixture_set_profile()
    database = _fixture_card_database()
    pool_before_pick = _fixture_pool_before_pick(
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
    )
    projection_event = PackOfferedEvent(
        event_name=CONTEXT_EVENT_NAME,
        set_code="TST",
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
        offered_grp_ids=CONTEXT_OFFERED_GRP_IDS,
        pool_grp_ids=pool_before_pick,
        account_id="account-1",
    )
    global_pick_index = session_module._draft_pick_index(event=projection_event)
    estimated_remaining_picks = max(0, EXPECTED_TOTAL_PICKS - global_pick_index)
    context = build_pick_scoring_context(
        pool_grp_ids=pool_before_pick,
        card_database=database,
        set_profile=profile,
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
        global_pick_index=global_pick_index,
        estimated_remaining_picks=estimated_remaining_picks,
    )
    assert context is not None
    source_card = SimpleNamespace(
        card=database.lookup(grp_id=104894),
        score=73,
        rating=SimpleNamespace(gih_win_rate=0.64),
        contextual_evidence=("profile evidence",),
    )
    state = _draft_state(
        account_id="account-1",
        screen_name="Player",
        draft_id="draft-1",
        updated_at="2026-08-29T10:00:00+00:00",
        pool_grp_ids=pool_before_pick,
    )
    report = DomainBacktestReport(
        state=state,
        ranking_mode="score",
        rows=(
            DomainBacktestPickResult(
                pack_number=CONTEXT_PACK_NUMBER,
                pick_number=CONTEXT_PICK_NUMBER,
                pool_size=len(pool_before_pick),
                offered_count=2,
                recommended=source_card,
                actual=database.lookup(grp_id=104894),
                match=True,
                skipped_reason=None,
                data_source="fixture",
                role_ledger=context.role_ledger,
                scoring_context=context,
                contextual_evidence=source_card.contextual_evidence,
            ),
        ),
    )

    projected = session_module._backtest_result(report=report).rows[0]

    assert projected.role_ledger is context.role_ledger
    assert projected.scoring_context is context
    assert projected.contextual_evidence == source_card.contextual_evidence


def test_live_session_account_pick_retains_recommendations_and_colors(
    tmp_path: Path,
) -> None:
    published: list[LiveSessionEvent] = []
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        ratings_loader=lambda set_code: _fixture_ratings_data(set_code=set_code),
        event_publisher=published.append,
    )
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()

    session.process_lines(lines=fixture_lines[:10])
    before_pick = session.snapshot
    before_scores = tuple(
        (recommendation.score, recommendation.win_rate)
        for recommendation in before_pick.recommendations.cards
    )
    assert before_pick.recommendations.cards
    assert before_pick.pool.current_colors
    assert before_pick.ratings.phase == DataLoadPhase.READY
    published.clear()

    session.process_lines(lines=(fixture_lines[10],))

    pick_publication = next(
        item for item in published if isinstance(item.event, PickMadeEvent)
    )
    after_pick = pick_publication.snapshot
    assert after_pick is session.snapshot
    assert after_pick.recommendations.cards == before_pick.recommendations.cards
    assert tuple(
        (recommendation.score, recommendation.win_rate)
        for recommendation in after_pick.recommendations.cards
    ) == before_scores
    assert after_pick.ratings == before_pick.ratings
    assert after_pick.current_scored_pack is before_pick.current_scored_pack
    assert after_pick.pool.current_colors == before_pick.pool.current_colors


def test_live_session_accountless_pick_retains_recommendations_and_colors(
    tmp_path: Path,
) -> None:
    published: list[LiveSessionEvent] = []
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        ratings_loader=lambda set_code: _fixture_ratings_data(set_code=set_code),
        event_publisher=published.append,
    )
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()

    session.process_lines(lines=fixture_lines[2:10])
    before_pick = session.snapshot
    before_scores = tuple(
        (recommendation.score, recommendation.win_rate)
        for recommendation in before_pick.recommendations.cards
    )
    assert before_pick.active_account is None
    assert before_pick.recommendations.cards
    assert before_pick.pool.current_colors
    assert before_pick.ratings.phase == DataLoadPhase.READY
    published.clear()

    session.process_lines(lines=(fixture_lines[10],))

    pick_publication = next(
        item for item in published if isinstance(item.event, PickMadeEvent)
    )
    after_pick = pick_publication.snapshot
    assert after_pick is session.snapshot
    assert after_pick.recommendations.cards == before_pick.recommendations.cards
    assert tuple(
        (recommendation.score, recommendation.win_rate)
        for recommendation in after_pick.recommendations.cards
    ) == before_scores
    assert after_pick.ratings == before_pick.ratings
    assert after_pick.current_scored_pack is before_pick.current_scored_pack
    assert after_pick.pool.current_colors == before_pick.pool.current_colors

    published.clear()
    session.process_lines(lines=(fixture_lines[132],))

    completion_publication = next(
        item for item in published if isinstance(item.event, DraftCompletedEvent)
    )
    cleared = completion_publication.snapshot
    assert cleared.recommendations.cards == ()
    assert cleared.pool.current_colors == ()


def test_live_session_startup_scan_processes_previous_then_current_once(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    _write_lines(path=previous_log_path, lines=fixture_lines[:70])
    _write_lines(path=log_path, lines=fixture_lines[70:])
    session = LiveSession(
        log_path=log_path,
        app_dir=app_dir,
        previous_log_path=previous_log_path,
    )

    startup_snapshot = session.scan_startup_files()
    polled_snapshot = session.poll_once()

    assert startup_snapshot.status.phase == ApplicationPhase.DRAFT_COMPLETE
    assert startup_snapshot.pool.total_cards == 42
    assert polled_snapshot is startup_snapshot
    audit_records = load_draft_audit_records(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=app_dir,
    )
    assert len(audit_records) == 44


def test_live_session_startup_scan_refreshes_setup_for_account_only_previous_log(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    _write_lines(
        path=previous_log_path,
        lines=[_auth_line(account_id="account-1", screen_name="Player")],
    )
    session = LiveSession(
        log_path=log_path,
        app_dir=tmp_path / "app",
        previous_log_path=previous_log_path,
    )

    snapshot = session.scan_startup_files()

    assert snapshot.active_account == AccountIdentity(
        account_id="account-1",
        screen_name="Player",
    )
    assert snapshot.draft is None
    assert snapshot.status == ApplicationStatus(
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message=LOG_SETUP_GUIDANCE,
        setup_guidance=True,
    )


def test_live_session_login_change_clears_prior_account_context(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
    )
    session.process_lines(
        lines=(
            _auth_line(account_id="first-account", screen_name="First"),
            _course_line(event_name="QuickDraft_ONE_20260823", course_id="first-draft"),
        )
    )

    snapshot_after_missing_account = session.process_lines(
        lines=(
            "[Accounts - Login] Logged in successfully. Display Name: Second#12345",
            _course_line(
                event_name="QuickDraft_TWO_20260823",
                course_id="second-draft",
            ),
        )
    )

    assert snapshot_after_missing_account.status == ApplicationStatus(
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message="Draft detected; waiting for an Arena account ID.",
    )
    assert snapshot_after_missing_account.active_account is None
    assert snapshot_after_missing_account.draft is None
    assert snapshot_after_missing_account.recommendations == RecommendationState()
    assert snapshot_after_missing_account.pool == PoolState()
    assert snapshot_after_missing_account.progress is None
    assert snapshot_after_missing_account.build is None
    assert snapshot_after_missing_account.backtest is None
    assert not draft_state_path(
        account_id="first-account",
        draft_id="second-draft",
        app_dir=app_dir,
    ).exists()

    second_snapshot = session.process_lines(
        lines=(
            _auth_line(account_id="second-account", screen_name="Second"),
            _course_line(
                event_name="QuickDraft_TWO_20260823",
                course_id="second-draft",
            ),
        )
    )

    assert second_snapshot.active_account == AccountIdentity(
        account_id="second-account",
        screen_name="Second",
    )
    assert second_snapshot.draft is not None
    assert second_snapshot.draft.draft_id == "second-draft"
    assert draft_state_path(
        account_id="second-account",
        draft_id="second-draft",
        app_dir=app_dir,
    ).exists()


def test_live_session_account_without_draft_clears_profile_and_retires_request(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    client = _ProfileClientStub({})
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        profile_client=client,
    )
    session.process_lines(
        lines=(
            _auth_line(account_id="first-account", screen_name="First"),
            _course_line(event_name="QuickDraft_TST_20260823", course_id="first-draft"),
        )
    )
    request = session.profile_refresh_request()
    assert request is not None
    assert session.snapshot.set_profile.set_code == "TST"

    session.process_lines(
        lines=(_auth_line(account_id="second-account", screen_name="Second"),)
    )
    switched = session.dispatch(
        command=ChooseAccount(account_id="second-account")
    )

    assert switched.status == ApplicationStatus(
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message="Waiting for a Quick Draft.",
    )
    assert switched.set_profile == SetProfileState()
    assert session._active_set_code() is None
    assert session._set_profile is None
    assert session.profile_refresh_request() is None

    session.complete_profile_refresh(
        request=request,
        result=ProfileRefreshResult(
            profile=profile,
            outcome=ProfileRefreshOutcome.UPDATED,
        ),
    )
    assert session.snapshot is switched


def test_live_session_recovers_login_profile_and_selects_latest_account_draft(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    first = _draft_state(
        account_id="alpha-account",
        screen_name="Alpha",
        draft_id="alpha-old",
        updated_at="2026-08-21T10:00:00+00:00",
        pool_grp_ids=(101,),
    )
    latest = replace(
        first,
        draft_id="alpha-latest",
        course_id="alpha-latest",
        updated_at="2026-08-22T10:00:00+00:00",
        pool_grp_ids=(101, 102),
    )
    beta = _draft_state(
        account_id="beta-account",
        screen_name="Beta",
        draft_id="beta-draft",
        updated_at="2026-08-22T09:00:00+00:00",
        pool_grp_ids=(201,),
    )
    for state in (first, latest, beta):
        save_draft_state(state=state, app_dir=app_dir)
    profile_store = DraftPoolStore(app_dir=app_dir)
    profile_store.set_active_account(
        account_id="alpha-account",
        screen_name="Alpha",
    )
    profile_store.set_active_account(
        account_id="beta-account",
        screen_name="Beta",
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
    )

    recovered = session.process_lines(
        lines=(
            "[Accounts - Login] Logged in successfully. Display Name: Alpha#98765",
        )
    )
    selected = session.dispatch(
        command=ChooseAccount(account_id="beta-account"),
    )
    session.process_lines(
        lines=(
            "[Accounts - Login] Logged in successfully. Display Name: Unknown#12345",
        )
    )
    session.dispatch(command=ChooseAccount(account_id="beta-account"))
    continued = session.process_lines(
        lines=(
            _course_line(
                event_name="QuickDraft_TST_20260824",
                course_id="beta-new-draft",
            ),
        )
    )

    assert tuple(account.account_id for account in recovered.accounts) == (
        "alpha-account",
        "beta-account",
    )
    assert recovered.active_account == AccountIdentity(
        account_id="alpha-account",
        screen_name="Alpha",
    )
    assert recovered.draft is not None
    assert recovered.draft.draft_id == "alpha-latest"
    assert recovered.pool.total_cards == 2
    assert selected.active_account == AccountIdentity(
        account_id="beta-account",
        screen_name="Beta",
    )
    assert selected.draft is not None
    assert selected.draft.draft_id == "beta-draft"
    assert selected.pool.total_cards == 1
    assert continued.active_account == selected.active_account
    assert continued.draft is not None
    assert continued.draft.draft_id == "beta-new-draft"
    assert draft_state_path(
        account_id="beta-account",
        draft_id="beta-new-draft",
        app_dir=app_dir,
    ).exists()

    state_payload = json.loads(
        draft_state_path(
            account_id="alpha-account",
            draft_id="alpha-latest",
            app_dir=app_dir,
        ).read_text(encoding="utf-8")
    )
    assert state_payload["schema_version"] == 1
    assert state_payload["account_id"] == "alpha-account"
    assert state_payload["draft_id"] == "alpha-latest"
    account_payload = json.loads(
        (app_dir / "accounts" / "alpha-account.json").read_text(
            encoding="utf-8"
        )
    )
    assert account_payload == {
        "account_id": "alpha-account",
        "schema_version": 1,
        "screen_name": "Alpha",
    }


def test_live_session_rejects_unknown_account_selection(tmp_path: Path) -> None:
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
    )

    with pytest.raises(ValueError, match="Unknown Arena account 'missing-account'"):
        session.dispatch(command=ChooseAccount(account_id="missing-account"))


def test_live_session_loads_cached_ratings_scores_all_ranking_modes_and_audits_choice(
    tmp_path: Path,
) -> None:
    cache_checks: list[str] = []
    rating_loads: list[str] = []

    def cache_checker(set_code: str) -> bool:
        cache_checks.append(set_code)
        return True

    def ratings_loader(set_code: str) -> SeventeenLandsData:
        rating_loads.append(set_code)
        return _fixture_ratings_data(set_code=set_code)

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        ratings_loader=ratings_loader,
        ratings_cache_checker=cache_checker,
    )
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    pack_line_index = _process_until_recommendations(
        session=session,
        lines=fixture_lines,
    )

    assert cache_checks == ["MSH"]
    assert rating_loads == ["MSH"]
    assert session.snapshot.card_data.phase == DataLoadPhase.READY
    assert session.snapshot.ratings == RatingsState(
        set_code="MSH",
        phase=DataLoadPhase.READY,
        message="17Lands ratings are ready for MSH.",
        rated_cards=2,
        total_cards=14,
        last_successful_update="2026-08-23T12:00:00+00:00",
    )
    assert session.snapshot.recommendations.source_summary == (
        "QuickDraft + Premier fallback + neutral prior"
    )
    assert {card.source_label for card in session.snapshot.recommendations.cards} == {
        "Premier",
        "Prior*",
        "Quick",
    }
    recommendations = session.snapshot.recommendations.cards
    explanations = tuple(
        recommendation.explanation for recommendation in recommendations
    )
    assert all(
        explanation is not None and "DO-point candidate" in explanation
        for explanation in explanations
    )
    top_recommendation = recommendations[0]
    assert str(top_recommendation.score) in (top_recommendation.explanation or "")
    assert top_recommendation.source_label in (
        top_recommendation.explanation or ""
    )

    top_cards_by_mode: dict[str, int] = {}
    supported_modes = session.snapshot.recommendations.supported_ranking_modes
    for ranking_mode in supported_modes:
        snapshot = session.dispatch(
            command=ChangeRanking(ranking_mode=ranking_mode),
        )
        top_cards_by_mode[ranking_mode] = snapshot.recommendations.cards[0].card.grp_id
        assert snapshot.recommendations.ranking_mode == ranking_mode
        assert tuple(card.rank for card in snapshot.recommendations.cards) == tuple(
            range(1, 15)
        )

    assert top_cards_by_mode == {
        "score": 104894,
        "win_rate": 104894,
        "alsa": 104976,
        "mv": 105080,
    }
    expected_recommendation = top_cards_by_mode["mv"]
    for line in fixture_lines[pack_line_index + 1 :]:
        session.process_lines(lines=(line,))
        records = load_draft_audit_records(
            account_id=FIXTURE_ACCOUNT_ID,
            draft_id=FIXTURE_DRAFT_ID,
            app_dir=tmp_path / "app",
        )
        if records and records[-1]["record_type"] == "choice_made":
            break

    assert records[-2]["record_type"] == "decision_evaluated"
    assert records[-1]["record_type"] == "choice_made"
    assert records[-1]["ranking_mode"] == "mv"
    assert records[-1]["recommended_grp_id"] == expected_recommendation
    assert records[-1]["evaluation_id"] == records[-2]["evaluation_id"]


@pytest.mark.parametrize(
    "materialized_pair",
    (False, True),
    ids=("absent-pair-data", "materialized-pair-data"),
)
def test_live_session_locked_pair_scoring_never_invokes_lazy_loader(
    tmp_path: Path,
    materialized_pair: bool,
) -> None:
    lazy_calls: list[str] = []
    ratings_data = _fixture_ratings_data(set_code="TST")
    pair_card_ratings: dict[str, SeventeenLandsFormatData] = {}
    if materialized_pair:
        pair_card_ratings["WU"] = replace(
            ratings_data.primary,
            card_ratings={
                104894: _fixture_stats(
                    grp_id=104894,
                    color="WU",
                    gih_win_rate=0.40,
                    average_last_seen_at=3.0,
                ),
                104976: _fixture_stats(
                    grp_id=104976,
                    color="WU",
                    gih_win_rate=0.90,
                    average_last_seen_at=1.0,
                ),
            },
        )

    def lazy_pair_loader(pair: str) -> SeventeenLandsFormatData:
        lazy_calls.append(pair)
        raise SeventeenLandsError(f"lazy pair loader invoked for {pair}")

    input_ratings = replace(
        ratings_data,
        pair_card_ratings=pair_card_ratings,
        pair_card_ratings_loader=lazy_pair_loader,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        card_database=_fixture_card_database(),
        ratings_loader=lambda _set_code: input_ratings,
    )

    snapshot = session.process_lines(
        lines=(
            _profiled_pack_line(
                pool_before_pick=_fixture_pool_before_pick(
                    pack_number=CONTEXT_PACK_NUMBER,
                    pick_number=CONTEXT_PICK_NUMBER,
                )
            ),
        )
    )

    scored_pack = snapshot.current_scored_pack
    assert scored_pack is not None
    assert scored_pack.commitment.phase == "locked"
    assert scored_pack.commitment.inferred_pair == "WU"
    assert lazy_calls == []
    if materialized_pair:
        assert snapshot.recommendations.cards[0].card.grp_id == 104976
    else:
        assert snapshot.recommendations.cards[0].card.grp_id == 104894


def test_live_session_locked_pair_refresh_never_restores_lazy_loader(
    tmp_path: Path,
) -> None:
    lazy_calls: list[tuple[int, str]] = []
    loaded_ratings: list[SeventeenLandsData] = []

    def ratings_loader(set_code: str) -> SeventeenLandsData:
        load_number = len(loaded_ratings) + 1
        ratings_data = _fixture_ratings_data(set_code=set_code)
        if load_number == 2:
            ratings_data = replace(
                ratings_data,
                primary=replace(
                    ratings_data.primary,
                    card_ratings={
                        104894: _fixture_stats(
                            grp_id=104894,
                            color="WU",
                            gih_win_rate=0.40,
                            average_last_seen_at=3.0,
                        ),
                        104976: _fixture_stats(
                            grp_id=104976,
                            color="WU",
                            gih_win_rate=0.90,
                            average_last_seen_at=1.0,
                        ),
                    },
                ),
            )

        def lazy_pair_loader(pair: str) -> SeventeenLandsFormatData:
            lazy_calls.append((load_number, pair))
            raise SeventeenLandsError(f"lazy pair loader invoked for {pair}")

        ratings_data = replace(
            ratings_data,
            pair_card_ratings_loader=lazy_pair_loader,
        )
        loaded_ratings.append(ratings_data)
        return ratings_data

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        card_database=_fixture_card_database(),
        ratings_loader=ratings_loader,
    )
    locked_pool = _fixture_pool_before_pick(
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
    )
    initial = session.process_lines(
        lines=(_profiled_pack_line(pool_before_pick=locked_pool),)
    )

    assert initial.current_scored_pack is not None
    assert initial.current_scored_pack.commitment.phase == "locked"
    assert initial.recommendations.cards[0].card.grp_id == 104894

    refreshed = session.dispatch(command=RequestRatingsDownload(set_code="TST"))

    assert len(loaded_ratings) == 2
    assert refreshed.current_scored_pack is not None
    assert refreshed.current_scored_pack.commitment.phase == "locked"
    assert refreshed.recommendations.cards[0].card.grp_id == 104976
    assert lazy_calls == []


def test_live_session_locked_pair_scoring_does_not_open_scryfall_or_17lands_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    ratings_data = _fixture_ratings_data(
        set_code="TST",
        fetched_at=datetime.now(tz=UTC),
    )
    save_17lands_format_data(ratings_data.primary, app_dir=app_dir)
    network_calls: list[str] = []

    def unexpected_network(
        request: object,
        *_args: object,
        **_kwargs: object,
    ) -> object:
        network_calls.append(getattr(request, "full_url", str(request)))
        raise urllib.error.URLError("locked pair scoring network blocked")

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
        ratings_loader=lambda set_code: load_or_refresh_17lands_data(
            set_code=set_code,
            app_dir=app_dir,
            premier_fallback_enabled=False,
        ),
    )
    detected = QuickDraftDetectedEvent(
        event_name=CONTEXT_EVENT_NAME,
        set_code="TST",
        account_id=None,
    )
    session._consume_detected_event(event=detected)
    assert session.snapshot.ratings.phase is DataLoadPhase.READY
    session._consume_event(
        event=DraftStartedEvent(
            event_name=detected.event_name,
            set_code=detected.set_code,
            course_id="course-tst",
            account_id=None,
        ),
        state=None,
    )

    monkeypatch.setattr(
        "draftomen.carddb.urllib.request.urlopen",
        unexpected_network,
    )
    monkeypatch.setattr(
        "draftomen.seventeen.urllib.request.urlopen",
        unexpected_network,
    )
    session._consume_event(
        event=PackOfferedEvent(
            event_name=CONTEXT_EVENT_NAME,
            set_code="TST",
            pack_number=CONTEXT_PACK_NUMBER,
            pick_number=CONTEXT_PICK_NUMBER,
            offered_grp_ids=CONTEXT_OFFERED_GRP_IDS,
            pool_grp_ids=_fixture_pool_before_pick(
                pack_number=CONTEXT_PACK_NUMBER,
                pick_number=CONTEXT_PICK_NUMBER,
            ),
            account_id=None,
        ),
        state=None,
    )

    snapshot = session.snapshot
    assert snapshot.current_scored_pack is not None
    assert snapshot.current_scored_pack.commitment.phase == "locked"
    assert snapshot.current_scored_pack.commitment.inferred_pair == "WU"
    assert snapshot.recommendations.cards[0].card.grp_id == 104894
    assert network_calls == []


def test_live_session_uses_one_based_later_pick_index_in_scores_and_audit(
    tmp_path: Path,
) -> None:
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
    )
    for line in FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines():
        snapshot = session.process_lines(lines=(line,))
        if (
            snapshot.draft is not None
            and snapshot.draft.pack_number == 0
            and snapshot.draft.pick_number == 5
            and snapshot.recommendations.cards
        ):
            break

    records = load_draft_audit_records(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=tmp_path / "app",
    )
    evaluation = records[-1]

    assert evaluation["record_type"] == "decision_evaluated"
    assert evaluation["pack_number"] == 0
    assert evaluation["pick_number"] == 5
    assert evaluation["pick_index"] == 6
    assert evaluation["commitment"]["pick_index"] == 6
    assert evaluation["commitment"]["level"] > 0.0


def test_live_session_missing_ratings_use_neutral_priors_until_download_completes(
    tmp_path: Path,
) -> None:
    published: list[LiveSessionSnapshot] = []
    load_calls: list[tuple[str, bool]] = []

    def ratings_loader(
        set_code: str,
        progress_callback: DownloadProgressCallback,
        *,
        refresh: bool,
    ) -> SeventeenLandsData:
        load_calls.append((set_code, refresh))
        progress_callback(
            SeventeenLandsDownloadProgress(
                completed_requests=1,
                total_requests=4,
                message="Downloaded Quick Draft ratings",
            )
        )
        progress_callback(
            SeventeenLandsDownloadProgress(
                completed_requests=4,
                total_requests=4,
                message="Downloaded Premier Draft ratings",
            )
        )
        return _fixture_ratings_data(set_code=set_code)

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        ratings_progress_loader=ratings_loader,
        ratings_cache_checker=lambda set_code: False,
        snapshot_publisher=published.append,
    )
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    _process_until_recommendations(session=session, lines=fixture_lines)

    assert load_calls == []
    assert session.snapshot.ratings.phase == DataLoadPhase.MISSING
    assert session.snapshot.recommendations.source_summary == "neutral prior"
    assert all(card.no_data for card in session.snapshot.recommendations.cards)
    assert all(card.score == 50 for card in session.snapshot.recommendations.cards)

    published.clear()
    downloaded = session.dispatch(command=RequestRatingsDownload(set_code="MSH"))

    assert load_calls == [("MSH", True)]
    assert downloaded.ratings.phase == DataLoadPhase.READY
    assert downloaded.progress is None
    assert downloaded.recommendations.cards[0].card.grp_id == 104894
    assert any(
        snapshot.ratings.phase == DataLoadPhase.LOADING
        and snapshot.progress == ProgressState(
            operation=OperationKind.RATINGS,
            message="Downloaded Quick Draft ratings",
            completed=1,
            total=4,
        )
        for snapshot in published
    )
    assert any(
        snapshot.progress is not None and snapshot.progress.completed == 4
        for snapshot in published
    )


def test_live_session_explicit_ratings_refresh_reloads_ready_data(
    tmp_path: Path,
) -> None:
    published: list[LiveSessionSnapshot] = []
    load_calls: list[tuple[str, bool]] = []

    def ratings_loader(
        set_code: str,
        progress_callback: DownloadProgressCallback,
        *,
        refresh: bool,
    ) -> SeventeenLandsData:
        load_calls.append((set_code, refresh))
        progress_callback(
            SeventeenLandsDownloadProgress(
                completed_requests=1,
                total_requests=4,
                message="Refreshing Quick Draft ratings",
            )
        )
        fetched_at = datetime(
            2026,
            8,
            23 if len(load_calls) == 1 else 24,
            12,
            tzinfo=UTC,
        )
        return _fixture_ratings_data(set_code=set_code, fetched_at=fetched_at)

    cache_checks: list[str] = []
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        ratings_progress_loader=ratings_loader,
        ratings_cache_checker=lambda set_code: (
            cache_checks.append(set_code) or True
        ),
        snapshot_publisher=published.append,
    )
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    assert load_calls == [("MSH", False)]
    assert cache_checks == ["MSH"]
    assert session.snapshot.ratings.phase == DataLoadPhase.READY
    assert session.snapshot.ratings.last_successful_update == (
        "2026-08-23T12:00:00+00:00"
    )

    published.clear()
    refreshed = session.dispatch(command=RequestRatingsDownload(set_code="MSH"))

    assert load_calls == [("MSH", False), ("MSH", True)]
    assert cache_checks == ["MSH"]
    assert refreshed.ratings.phase == DataLoadPhase.READY
    assert refreshed.ratings.last_successful_update == (
        "2026-08-24T12:00:00+00:00"
    )
    assert refreshed.progress is None
    assert any(
        snapshot.ratings.phase == DataLoadPhase.LOADING
        and snapshot.progress == ProgressState(
            operation=OperationKind.RATINGS,
            message="Refreshing Quick Draft ratings",
            completed=1,
            total=4,
        )
        for snapshot in published
    )
    assert any(
        snapshot.ratings.phase == DataLoadPhase.READY
        and snapshot.progress is None
        for snapshot in published
    )
def test_live_session_ratings_refresh_retains_timestamp_while_loading_and_on_failure(
    tmp_path: Path,
) -> None:
    attempts = 0
    published: list[LiveSessionSnapshot] = []

    def ratings_loader(set_code: str) -> SeventeenLandsData:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("temporary service failure")
        fetched_at = datetime(
            2026,
            8,
            23 if attempts == 1 else 24,
            12,
            tzinfo=UTC,
        )
        return _fixture_ratings_data(set_code=set_code, fetched_at=fetched_at)

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        ratings_loader=ratings_loader,
        ratings_cache_checker=lambda set_code: True,
        snapshot_publisher=published.append,
    )
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    previous_timestamp = "2026-08-23T12:00:00+00:00"
    assert session.snapshot.ratings.last_successful_update == previous_timestamp
    card_timestamp = session.snapshot.card_data.last_successful_update

    published.clear()
    failed = session.dispatch(command=RequestRatingsDownload(set_code="MSH"))

    assert failed.ratings.phase == DataLoadPhase.FAILED
    assert failed.ratings.last_successful_update == previous_timestamp
    assert failed.card_data.last_successful_update == card_timestamp
    assert any(
        snapshot.ratings.phase == DataLoadPhase.LOADING
        and snapshot.ratings.last_successful_update == previous_timestamp
        for snapshot in published
    )


def test_live_session_failed_ratings_download_is_recoverable_and_retry_rescores(
    tmp_path: Path,
) -> None:
    attempts = 0
    lazy_calls: list[str] = []

    def ratings_loader(set_code: str) -> SeventeenLandsData:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary service failure")

        def lazy_pair_loader(pair: str) -> SeventeenLandsFormatData:
            lazy_calls.append(pair)
            raise SeventeenLandsError(f"lazy pair loader invoked for {pair}")

        return replace(
            _fixture_ratings_data(set_code=set_code),
            pair_card_ratings_loader=lazy_pair_loader,
        )

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        ratings_loader=ratings_loader,
        ratings_cache_checker=lambda set_code: False,
    )
    initial = session.process_lines(
        lines=(
            _profiled_pack_line(
                pool_before_pick=_fixture_pool_before_pick(
                    pack_number=CONTEXT_PACK_NUMBER,
                    pick_number=CONTEXT_PICK_NUMBER,
                )
            ),
        )
    )
    assert initial.current_scored_pack is not None
    assert initial.current_scored_pack.commitment.phase == "locked"
    assert initial.current_scored_pack.commitment.inferred_pair == "WU"

    failed = session.dispatch(command=RequestRatingsDownload(set_code="TST"))

    assert failed.ratings.phase == DataLoadPhase.FAILED
    assert failed.progress is None
    assert failed.errors == (
        SessionError(
            error_id="ratings:TST",
            code="ratings_unavailable",
            message="17Lands ratings failed for TST: temporary service failure.",
            recoverable=True,
            operation=OperationKind.RATINGS,
        ),
    )
    assert failed.recommendations.source_summary == "neutral prior"

    recovered = session.dispatch(command=RetryError(error_id="ratings:TST"))

    assert attempts == 2
    assert recovered.ratings.phase == DataLoadPhase.READY
    assert recovered.errors == ()
    assert recovered.current_scored_pack is not None
    assert recovered.current_scored_pack.commitment.phase == "locked"
    assert recovered.current_scored_pack.commitment.inferred_pair == "WU"
    assert recovered.recommendations.cards[0].card.grp_id == 104894
    assert lazy_calls == []


def test_live_session_card_data_load_failure_and_retry_publish_complete_states(
    tmp_path: Path,
) -> None:
    attempts = 0
    published: list[LiveSessionSnapshot] = []

    def card_data_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        assert set_code == "TST"
        assert allow_network is True
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("card cache unavailable")
        return replace(
            _fixture_set_card_database(set_code="TST"),
            generated_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        )

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        set_card_data_loader=card_data_loader,
        app_dir=tmp_path / "app",
        snapshot_publisher=published.append,
    )

    assert session.snapshot.card_data.phase == DataLoadPhase.IDLE

    session._consume_detected_event(
        event=QuickDraftDetectedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            account_id=None,
        )
    )
    failed = session.snapshot
    assert failed.card_data.phase == DataLoadPhase.FAILED
    assert failed.card_data.last_successful_update is None
    assert failed.progress is None
    assert failed.errors[0].error_id == "card-data"
    assert any(
        snapshot.card_data.phase == DataLoadPhase.LOADING
        and snapshot.progress is not None
        and snapshot.progress.operation == OperationKind.CARD_DATA
        for snapshot in published
    )

    ready = session.dispatch(command=RetryError(error_id="card-data"))

    assert attempts == 2
    assert ready.card_data == CardDataState(
        phase=DataLoadPhase.READY,
        message="Card metadata is ready.",
        last_successful_update="2026-08-23T12:00:00+00:00",
    )
    assert ready.errors == ()


def test_live_session_reopens_network_for_same_named_detection_after_local_retry(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, bool]] = []
    attempts = 0
    database = _fixture_set_card_database(set_code="TST")

    def card_data_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        nonlocal attempts
        calls.append((set_code, allow_network))
        attempts += 1
        if attempts < 3:
            raise RuntimeError("card cache unavailable")
        return database

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        set_card_data_loader=card_data_loader,
        app_dir=tmp_path / "app",
    )
    detected = QuickDraftDetectedEvent(
        event_name="QuickDraft_TST_20260823",
        set_code="TST",
        account_id=None,
    )
    session._consume_detected_event(event=detected)
    assert calls == [("TST", True)]

    session._consume_detected_event(event=detected)
    assert calls == [("TST", True)]

    session._consume_event(
        event=DraftStartedEvent(
            event_name=detected.event_name,
            set_code=detected.set_code,
            course_id="course-tst",
            account_id=None,
        ),
        state=None,
    )
    assert calls == [("TST", True), ("TST", False)]
    assert session.snapshot.card_data.phase == DataLoadPhase.FAILED

    session._consume_detected_event(event=detected)

    assert calls == [("TST", True), ("TST", False), ("TST", True)]
    assert session.snapshot.card_data.phase == DataLoadPhase.READY


def test_live_session_loads_set_card_data_before_ratings(
    tmp_path: Path,
) -> None:
    factory_calls: list[CardDatabase] = []
    cache_checks: list[str] = []
    rating_loads: list[str] = []
    card_data_loads: list[tuple[str, bool]] = []
    database = _fixture_set_card_database(set_code="TST")

    def card_data_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        card_data_loads.append((set_code, allow_network))
        return database

    def ratings_loader_factory(database: CardDatabase) -> RatingsLoader:
        factory_calls.append(database)

        def ratings_loader(set_code: str) -> SeventeenLandsData:
            rating_loads.append(set_code)
            return _fixture_ratings_data(set_code=set_code)

        return ratings_loader

    def cache_checker(set_code: str) -> bool:
        cache_checks.append(set_code)
        return True

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        set_card_data_loader=card_data_loader,
        ratings_loader_factory=ratings_loader_factory,
        ratings_cache_checker=cache_checker,
    )

    session._consume_detected_event(
        event=QuickDraftDetectedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            account_id=None,
        )
    )

    assert card_data_loads == [("TST", True)]
    assert factory_calls == [database]
    assert cache_checks == ["TST"]
    assert rating_loads == ["TST"]
    assert session.snapshot.card_data.phase == DataLoadPhase.READY
    assert session.snapshot.ratings.phase == DataLoadPhase.READY
    assert session.snapshot.ratings.set_code == "TST"


def test_live_session_avoids_duplicate_card_load_and_reloads_on_set_change(
    tmp_path: Path,
) -> None:
    database_by_set = {
        "TST": _fixture_set_card_database(set_code="TST"),
        "MSH": _fixture_set_card_database(set_code="MSH"),
    }
    calls: list[tuple[str, bool]] = []

    def card_data_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        calls.append((set_code, allow_network))
        return database_by_set[set_code]

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        set_card_data_loader=card_data_loader,
    )
    first = QuickDraftDetectedEvent(
        event_name="QuickDraft_TST_20260823",
        set_code="TST",
        account_id=None,
    )
    session._consume_detected_event(event=first)
    session._consume_detected_event(event=first)
    session._consume_detected_event(
        event=QuickDraftDetectedEvent(
            event_name="QuickDraft_TST_20260824",
            set_code="TST",
            account_id=None,
        )
    )
    session._consume_detected_event(
        event=QuickDraftDetectedEvent(
            event_name="QuickDraft_MSH_20260824",
            set_code="MSH",
            account_id=None,
        )
    )

    assert calls == [("TST", True), ("MSH", True)]
    assert session.card_database is database_by_set["MSH"]


def test_live_session_late_card_recovery_is_local_and_pack_never_loads(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, bool]] = []
    database = _fixture_set_card_database(set_code="TST")

    def card_data_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        calls.append((set_code, allow_network))
        return database

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        set_card_data_loader=card_data_loader,
    )
    session._consume_event(
        event=DraftStartedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            course_id="course-tst",
            account_id=None,
        ),
        state=None,
    )
    session._consume_event(
        event=PackOfferedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(),
            pool_grp_ids=(),
            account_id=None,
        ),
        state=None,
    )

    assert calls == [("TST", False)]
    assert session.card_database is database


def test_live_session_recovered_state_uses_offline_card_data_and_closes_fence(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    state = _draft_state(
        account_id="recovered-account",
        screen_name="Recovered",
        draft_id="recovered-draft",
        updated_at="2026-08-30T10:00:00+00:00",
        pool_grp_ids=(104894,),
    )
    save_draft_state(state=state, app_dir=app_dir)
    calls: list[tuple[str, bool]] = []
    database = _fixture_set_card_database(set_code="TST")

    def card_data_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        calls.append((set_code, allow_network))
        if allow_network:
            raise AssertionError("recovered card-data load must be local-only")
        return database

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        set_card_data_loader=card_data_loader,
        app_dir=app_dir,
    )

    recovered = session.dispatch(
        command=ChooseAccount(account_id="recovered-account")
    )

    assert calls == [("TST", False)]
    assert session._card_data_network_open is False
    assert recovered.card_data.phase == DataLoadPhase.READY
    assert recovered.active_account == AccountIdentity(
        account_id="recovered-account",
        screen_name="Recovered",
    )
    assert recovered.draft is not None


def test_live_session_recovery_resets_local_attempt_for_new_set(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    recovered_state = replace(
        _draft_state(
            account_id="account-b",
            screen_name="Beta",
            draft_id="draft-b",
            updated_at="2026-08-30T10:00:00+00:00",
            pool_grp_ids=(104894,),
        ),
        set_code="MSH",
    )
    save_draft_state(state=recovered_state, app_dir=app_dir)
    calls: list[tuple[str, bool]] = []

    def card_data_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        calls.append((set_code, allow_network))
        return _fixture_set_card_database(set_code=set_code)

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        set_card_data_loader=card_data_loader,
        app_dir=app_dir,
    )
    session._consume_event(
        event=DraftStartedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            course_id="course-a",
            account_id="account-a",
        ),
        state=None,
    )
    assert calls == [("TST", False)]

    recovered = session.dispatch(command=ChooseAccount(account_id="account-b"))

    assert calls == [("TST", False), ("MSH", False)]
    assert recovered.active_account == AccountIdentity(
        account_id="account-b",
        screen_name="Beta",
    )
    assert recovered.card_data.phase == DataLoadPhase.READY


def test_live_session_detection_snapshot_restores_active_account(
    tmp_path: Path,
) -> None:
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        card_database=_fixture_card_database(),
        app_dir=tmp_path / "app",
    )

    session._consume_detected_event(
        event=QuickDraftDetectedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            account_id="account-a",
        )
    )
    assert session.snapshot.active_account == AccountIdentity(
        account_id="account-a",
        screen_name=None,
    )

    session._consume_detected_event(
        event=QuickDraftDetectedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            account_id="account-b",
        )
    )
    assert session.snapshot.active_account == AccountIdentity(
        account_id="account-b",
        screen_name=None,
    )


def test_live_session_rejects_wrong_set_database_without_installing_it(
    tmp_path: Path,
) -> None:
    source = _fixture_card_database()
    wrong = replace(
        source,
        cards={
            grp_id: replace(card, set_code="msh")
            for grp_id, card in source.cards.items()
        },
    )
    calls: list[tuple[str, bool]] = []

    def card_data_loader(set_code: str, *, allow_network: bool) -> CardDatabase:
        calls.append((set_code, allow_network))
        return wrong

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        set_card_data_loader=card_data_loader,
    )
    session._consume_detected_event(
        event=QuickDraftDetectedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            account_id=None,
        )
    )

    assert calls == [("TST", True)]
    assert session.card_database is None
    assert session.snapshot.card_data.phase == DataLoadPhase.FAILED
    assert session.snapshot.errors[0].error_id == "card-data"


class _SessionCardDataResponse:
    def __init__(self, payload: bytes, *, url: str) -> None:
        self._stream = io.BytesIO(payload)
        self.url = url
        self.status = 200

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        return None


def test_live_session_card_data_client_cold_load_then_offline_cache_hit(
    tmp_path: Path,
) -> None:
    source = _fixture_card_database()
    database = replace(
        source,
        cards={
            grp_id: replace(card, set_code="tst")
            for grp_id, card in source.cards.items()
        },
    )
    payload = SetCardData.from_card_database(
        database,
        set_code="tst",
        set_name="Test Set",
    ).to_gzip_bytes()
    base_url = "https://cards.example.test/card-data/"
    request_urls: list[str] = []

    def opener(request: object, *, timeout: float) -> _SessionCardDataResponse:
        del timeout
        request_urls.append(request.full_url)
        return _SessionCardDataResponse(
            payload,
            url=request.full_url,
        )

    app_dir = tmp_path / "app"
    first = LiveSession(
        log_path=tmp_path / "first.log",
        app_dir=app_dir,
        set_card_data_loader=CardDataClient(
            app_dir=app_dir,
            base_url=base_url,
            opener=opener,
        ).load,
    )
    first._consume_detected_event(
        event=QuickDraftDetectedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            account_id=None,
        )
    )

    assert request_urls == [f"{base_url}tst.json.gz"]
    assert first.card_database is not None

    def fail_opener(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline cache hit must not open the network")

    second = LiveSession(
        log_path=tmp_path / "second.log",
        app_dir=app_dir,
        set_card_data_loader=CardDataClient(
            app_dir=app_dir,
            base_url=base_url,
            opener=fail_opener,
        ).load,
    )
    second._consume_event(
        event=DraftStartedEvent(
            event_name="QuickDraft_TST_20260823",
            set_code="TST",
            course_id="course-tst",
            account_id=None,
        ),
        state=None,
    )

    assert second.snapshot.card_data.phase == DataLoadPhase.READY
    assert second.card_database is not None


def test_inactive_ratings_worker_caches_result_without_publishing_stale_state(
    tmp_path: Path,
) -> None:
    load_started = threading.Event()
    release_load = threading.Event()
    load_calls: list[str] = []
    worker_errors: list[BaseException] = []
    published: list[LiveSessionSnapshot] = []

    def ratings_loader(
        set_code: str,
        progress_callback: DownloadProgressCallback,
        *,
        refresh: bool,
    ) -> SeventeenLandsData:
        load_calls.append(set_code)
        load_started.set()
        assert release_load.wait(timeout=2.0)
        progress_callback(
            SeventeenLandsDownloadProgress(
                completed_requests=4,
                total_requests=4,
                message=f"Downloaded ratings for {set_code}",
            )
        )
        return _fixture_ratings_data(set_code=set_code)

    def request_download() -> None:
        try:
            session.dispatch(command=RequestRatingsDownload(set_code="AAA"))
        except BaseException as error:
            worker_errors.append(error)

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        ratings_progress_loader=ratings_loader,
        ratings_cache_checker=lambda set_code: False,
        snapshot_publisher=published.append,
    )
    session.process_lines(
        lines=(
            _auth_line(account_id="account-1", screen_name="Player"),
            _course_line(
                event_name="QuickDraft_AAA_20260823",
                course_id="draft-aaa",
            ),
        )
    )
    worker = threading.Thread(target=request_download, daemon=True)
    worker.start()
    assert load_started.wait(timeout=2.0)

    try:
        session.dispatch(command=RequestRatingsDownload(set_code="AAA"))
        switched = session.process_lines(
            lines=(
                _course_line(
                    event_name="QuickDraft_BBB_20260823",
                    course_id="draft-bbb",
                ),
            )
        )
        assert load_calls == ["AAA"]
        assert switched.draft is not None
        assert switched.draft.set_code == "BBB"
        assert switched.ratings.set_code == "BBB"
        assert switched.ratings.phase == DataLoadPhase.MISSING
        published.clear()
    finally:
        release_load.set()
        worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert worker_errors == []
    assert published == []
    assert session.snapshot.draft is not None
    assert session.snapshot.draft.set_code == "BBB"
    assert session.snapshot.ratings.set_code == "BBB"
    assert session.snapshot.ratings.phase == DataLoadPhase.MISSING
    assert session.snapshot.progress is None

    restored = session.process_lines(
        lines=(
            _course_line(
                event_name="QuickDraft_AAA_20260823",
                course_id="draft-aaa",
            ),
        )
    )

    assert load_calls == ["AAA"]
    assert restored.draft is not None
    assert restored.draft.set_code == "AAA"
    assert restored.ratings.set_code == "AAA"
    assert restored.ratings.phase == DataLoadPhase.READY


def test_live_session_build_request_publishes_structured_ordered_result(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    database = _fixture_card_database()
    state = _draft_state(
        account_id="account-1",
        screen_name="Player",
        draft_id="draft-1",
        updated_at="2026-08-23T10:00:00+00:00",
        pool_grp_ids=tuple(database.cards),
    )
    save_draft_state(state=state, app_dir=app_dir)
    published: list[LiveSessionSnapshot] = []
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
        snapshot_publisher=published.append,
    )
    session.dispatch(command=ChooseAccount(account_id="account-1"))

    snapshot = session.dispatch(
        command=RequestBuild(pair_override="WU", allow_splash=False)
    )

    assert any(
        item.progress == ProgressState(
            operation=OperationKind.BUILD,
            message="Building deck",
        )
        for item in published
    )
    assert snapshot.progress is None
    assert snapshot.errors == ()
    assert snapshot.build is not None
    assert snapshot.build.selected_pair == "WU"
    assert snapshot.build.pair_override == "WU"
    assert snapshot.build.domain_mana_base is not None
    assert (
        snapshot.build.average_mana_value
        == snapshot.build.domain_mana_base.average_mana_value
    )
    assert sum(card.quantity for card in snapshot.build.spells) == len(database.cards)
    assert all(card.score is not None for card in snapshot.build.spells)
    assert [
        card.card.mana_value for card in snapshot.build.spells
    ] == sorted(card.card.mana_value for card in snapshot.build.spells)
    assert sum(land.quantity for land in snapshot.build.lands) + sum(
        card.quantity for card in snapshot.build.spells
    ) == snapshot.build.deck_size
    assert snapshot.build.spell_count == sum(
        card.quantity for card in snapshot.build.spells
    )
    assert snapshot.build.land_count == sum(
        land.quantity for land in snapshot.build.lands
    )
    assert snapshot.build.domain_spell_selection is not None
    assert snapshot.build.creature_count == (
        snapshot.build.domain_spell_selection.counts.creatures
    )
    assert snapshot.build.instant_count == (
        snapshot.build.domain_spell_selection.counts.instants
    )
    assert any(
        option.pair == "WU" and option.selected and option.automatic
        for option in snapshot.build.pair_options
    )


def test_live_session_build_average_is_unavailable_for_unknown_spell_mana(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    database = _fixture_card_database()
    unknown_grp_id = next(iter(database.cards))
    unknown_card = database.cards[unknown_grp_id]
    database = replace(
        database,
        cards={
            **database.cards,
            unknown_grp_id: replace(unknown_card, mana_value=None),
        },
    )
    save_draft_state(
        state=_draft_state(
            account_id="account-1",
            screen_name="Player",
            draft_id="draft-unknown-mana",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=tuple(database.cards),
        ),
        app_dir=app_dir,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
    )
    session.dispatch(command=ChooseAccount(account_id="account-1"))

    snapshot = session.dispatch(command=RequestBuild())

    assert snapshot.build is not None
    assert snapshot.build.domain_mana_base is not None
    assert snapshot.build.domain_mana_base.average_mana_value > 0
    assert snapshot.build.average_mana_value is None


def test_live_session_build_request_reports_empty_and_invalid_builds(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    database = _fixture_card_database()
    save_draft_state(
        state=_draft_state(
            account_id="account-1",
            screen_name="Player",
            draft_id="draft-empty",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=(),
        ),
        app_dir=app_dir,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
    )
    session.dispatch(command=ChooseAccount(account_id="account-1"))

    empty = session.dispatch(command=RequestBuild())

    assert empty.build is None
    assert empty.errors[-1].code == "build_failed"
    assert "pool is empty" in empty.errors[-1].message
    assert empty.errors[-1].recoverable is True

    save_draft_state(
        state=_draft_state(
            account_id="account-1",
            screen_name="Player",
            draft_id="draft-full",
            updated_at="2026-08-23T11:00:00+00:00",
            pool_grp_ids=tuple(database.cards),
        ),
        app_dir=app_dir,
    )
    session.dispatch(command=ChooseAccount(account_id="account-1"))

    invalid = session.dispatch(command=RequestBuild(pair_override="ZZ"))

    assert invalid.build is None
    assert invalid.errors[-1].code == "build_failed"
    assert "ZZ" in invalid.errors[-1].message


def test_live_session_discards_build_completion_after_account_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    database = _fixture_card_database()
    save_draft_state(
        state=_draft_state(
            account_id="account-a",
            screen_name="Player A",
            draft_id="draft-a",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=tuple(database.cards),
        ),
        app_dir=app_dir,
    )
    save_draft_state(
        state=_draft_state(
            account_id="account-b",
            screen_name="Player B",
            draft_id="draft-b",
            updated_at="2026-08-23T11:00:00+00:00",
            pool_grp_ids=(),
        ),
        app_dir=app_dir,
    )
    started = threading.Event()
    release = threading.Event()
    real_builder = session_module.build_deck_from_pool

    def blocking_builder(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(timeout=2.0)
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(session_module, "build_deck_from_pool", blocking_builder)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
    )
    session.dispatch(command=ChooseAccount(account_id="account-a"))
    worker_errors: list[Exception] = []

    def request_build() -> None:
        try:
            session.dispatch(command=RequestBuild(pair_override="WU"))
        except Exception as error:
            worker_errors.append(error)

    worker = threading.Thread(target=request_build, daemon=True)
    worker.start()
    assert started.wait(timeout=2.0)

    switched = session.dispatch(command=ChooseAccount(account_id="account-b"))
    assert switched.draft is not None
    assert switched.draft.draft_id == "draft-b"
    assert switched.progress is None

    release.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert worker_errors == []
    assert session.snapshot.draft is not None
    assert session.snapshot.draft.draft_id == "draft-b"
    assert session.snapshot.build is None
    assert not any(error.operation == OperationKind.BUILD for error in session.snapshot.errors)


def test_live_session_splash_change_discards_in_flight_build_and_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    database = _fixture_card_database()
    save_draft_state(
        state=_draft_state(
            account_id="account-1",
            screen_name="Player",
            draft_id="draft-1",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=tuple(database.cards),
        ),
        app_dir=app_dir,
    )
    started = threading.Event()
    release = threading.Event()
    real_builder = session_module.build_deck_from_pool

    def blocking_builder(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(timeout=2.0)
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(session_module, "build_deck_from_pool", blocking_builder)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
    )
    session.dispatch(command=ChooseAccount(account_id="account-1"))
    worker_errors: list[Exception] = []

    def request_build() -> None:
        try:
            session.dispatch(command=RequestBuild(allow_splash=True))
        except Exception as error:
            worker_errors.append(error)

    worker = threading.Thread(
        target=request_build,
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=2.0)

    changed = session.dispatch(command=ChangeSplashPreference(enabled=False))

    assert changed.progress is None
    assert changed.build is None
    assert changed.recommendations.splash_enabled is False

    release.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert worker_errors == []
    assert session.snapshot.progress is None
    assert session.snapshot.build is None


def test_live_session_backtest_request_preserves_comparisons_and_missing_history(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    database = _fixture_card_database()
    offered_grp_ids = tuple(database.cards)[:2]
    state = replace(
        _draft_state(
            account_id="account-1",
            screen_name="Player",
            draft_id="draft-1",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=(offered_grp_ids[0], offered_grp_ids[1]),
        ),
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=0,
                offered_grp_ids=offered_grp_ids,
                pool_before_pick=(),
                chosen_grp_id=offered_grp_ids[0],
            ),
            DraftPick(
                pack_number=0,
                pick_number=1,
                offered_grp_ids=None,
                pool_before_pick=(offered_grp_ids[0],),
                chosen_grp_id=offered_grp_ids[1],
            ),
        ),
    )
    save_draft_state(state=state, app_dir=app_dir)
    published: list[LiveSessionSnapshot] = []
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
        snapshot_publisher=published.append,
    )

    snapshot = session.dispatch(
        command=RequestBacktest(account_id="account-1", draft_id="draft-1")
    )

    assert any(
        item.progress == ProgressState(
            operation=OperationKind.BACKTEST,
            message="Running backtest",
        )
        for item in published
    )
    assert snapshot.progress is None
    assert snapshot.errors == ()
    assert snapshot.backtest is not None
    assert snapshot.backtest.compared_count == 1
    assert snapshot.backtest.skipped_count == 1
    assert snapshot.backtest.match_count == 1
    assert snapshot.backtest.rows[0].recommended is not None
    assert snapshot.backtest.rows[0].actual is not None
    assert snapshot.backtest.rows[0].match is True
    assert snapshot.backtest.rows[0].pool_size == 0
    assert snapshot.backtest.rows[0].offered_count == 2
    assert snapshot.backtest.rows[0].recommended_score is not None
    assert snapshot.backtest.rows[1].skipped_reason == "missing offered-card history"


def test_live_session_backtest_result_identifies_explicit_cross_account_draft(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    active_state = _draft_state(
        account_id="account-a",
        screen_name="Player A",
        draft_id="draft-a",
        updated_at="2026-08-23T10:00:00+00:00",
        pool_grp_ids=(),
    )
    target_state = replace(
        _draft_state(
            account_id="account-b",
            screen_name="Player B",
            draft_id="draft-b",
            updated_at="2026-08-23T11:00:00+00:00",
            pool_grp_ids=(),
        ),
        completed=True,
        completed_at="2026-08-23T11:30:00+00:00",
    )
    save_draft_state(state=active_state, app_dir=app_dir)
    save_draft_state(state=target_state, app_dir=app_dir)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
    )
    session.dispatch(command=ChooseAccount(account_id="account-a"))

    snapshot = session.dispatch(
        command=RequestBacktest(account_id="account-b", draft_id="draft-b")
    )

    assert snapshot.draft is not None
    assert snapshot.draft.account_id == "account-a"
    assert snapshot.backtest is not None
    assert snapshot.backtest.account_id == "account-b"
    assert snapshot.backtest.account_screen_name == "Player B"
    assert snapshot.backtest.draft_id == "draft-b"
    assert snapshot.backtest.set_code == "TST"
    assert snapshot.backtest.event_name == "QuickDraft_TST_20260823"
    assert snapshot.backtest.completed is True
    assert snapshot.backtest.chosen_pick_count == 0


def test_live_session_backtest_request_returns_empty_success(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    save_draft_state(
        state=_draft_state(
            account_id="account-1",
            screen_name="Player",
            draft_id="draft-empty",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=(),
        ),
        app_dir=app_dir,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
    )

    snapshot = session.dispatch(
        command=RequestBacktest(
            account_id="account-1",
            draft_id="draft-empty",
        )
    )

    assert snapshot.errors == ()
    assert snapshot.backtest == BacktestResult(
        ranking_mode="score",
        rows=(),
        match_count=0,
        compared_count=0,
        skipped_count=0,
        data_sources=(),
        account_id="account-1",
        account_screen_name="Player",
        draft_id="draft-empty",
        set_code="TST",
        event_name="QuickDraft_TST_20260823",
        completed=False,
        chosen_pick_count=0,
    )


def test_live_session_backtest_failure_can_retry_saved_request(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
    )
    command = RequestBacktest(account_id="account-1", draft_id="draft-1")

    failed = session.dispatch(command=command)

    assert failed.backtest is None
    assert failed.errors[-1].code == "backtest_failed"
    assert failed.errors[-1].recoverable is True

    save_draft_state(
        state=_draft_state(
            account_id="account-1",
            screen_name="Player",
            draft_id="draft-1",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=(),
        ),
        app_dir=app_dir,
    )

    retried = session.dispatch(command=RetryError(error_id="backtest"))

    assert retried.errors == ()
    assert retried.backtest is not None
    assert retried.backtest.rows == ()


def test_live_session_discards_older_overlapping_backtest_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    for draft_id, updated_at in (
        ("draft-1", "2026-08-23T10:00:00+00:00"),
        ("draft-2", "2026-08-23T11:00:00+00:00"),
    ):
        save_draft_state(
            state=_draft_state(
                account_id="account-1",
                screen_name="Player",
                draft_id=draft_id,
                updated_at=updated_at,
                pool_grp_ids=(),
            ),
            app_dir=app_dir,
        )
    started = threading.Event()
    release = threading.Event()
    real_generator = session_module.generate_backtest_report

    def blocking_generator(*args: object, **kwargs: object) -> object:
        state = kwargs["state"]
        assert isinstance(state, DraftState)
        if state.draft_id == "draft-1":
            started.set()
            assert release.wait(timeout=2.0)

        return real_generator(*args, **kwargs)

    monkeypatch.setattr(
        session_module,
        "generate_backtest_report",
        blocking_generator,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
    )
    worker_errors: list[Exception] = []

    def request_first_backtest() -> None:
        try:
            session.dispatch(
                command=RequestBacktest(
                    account_id="account-1",
                    draft_id="draft-1",
                )
            )
        except Exception as error:
            worker_errors.append(error)

    worker = threading.Thread(target=request_first_backtest, daemon=True)
    worker.start()
    assert started.wait(timeout=2.0)

    newer = session.dispatch(
        command=RequestBacktest(
            account_id="account-1",
            draft_id="draft-2",
        )
    )
    assert newer.backtest is not None
    assert newer.backtest.draft_id == "draft-2"

    release.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert worker_errors == []
    assert session.snapshot.backtest is not None
    assert session.snapshot.backtest.draft_id == "draft-2"


def test_live_session_ranking_change_discards_in_flight_backtest_and_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    save_draft_state(
        state=_draft_state(
            account_id="account-1",
            screen_name="Player",
            draft_id="draft-1",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=(),
        ),
        app_dir=app_dir,
    )
    started = threading.Event()
    release = threading.Event()
    real_generator = session_module.generate_backtest_report

    def blocking_generator(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(timeout=2.0)
        return real_generator(*args, **kwargs)

    monkeypatch.setattr(
        session_module,
        "generate_backtest_report",
        blocking_generator,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=_fixture_card_database(),
    )
    worker_errors: list[Exception] = []

    def request_backtest() -> None:
        try:
            session.dispatch(
                command=RequestBacktest(
                    account_id="account-1",
                    draft_id="draft-1",
                )
            )
        except Exception as error:
            worker_errors.append(error)

    worker = threading.Thread(
        target=request_backtest,
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=2.0)

    changed = session.dispatch(command=ChangeRanking(ranking_mode="win_rate"))

    assert changed.progress is None
    assert changed.backtest is None
    assert changed.recommendations.ranking_mode == "win_rate"

    release.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert worker_errors == []
    assert session.snapshot.progress is None
    assert session.snapshot.backtest is None


def test_live_session_context_change_clears_derived_errors_and_retries(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    database = _fixture_card_database()
    save_draft_state(
        state=_draft_state(
            account_id="account-a",
            screen_name="Player A",
            draft_id="draft-a",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=(),
        ),
        app_dir=app_dir,
    )
    save_draft_state(
        state=_draft_state(
            account_id="account-b",
            screen_name="Player B",
            draft_id="draft-b",
            updated_at="2026-08-23T11:00:00+00:00",
            pool_grp_ids=tuple(database.cards),
        ),
        app_dir=app_dir,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
    )
    session.dispatch(command=ChooseAccount(account_id="account-a"))
    session.dispatch(command=RequestBuild())
    failed = session.dispatch(
        command=RequestBacktest(
            account_id="account-a",
            draft_id="missing-draft",
        )
    )
    assert {
        error.operation for error in failed.errors
    } >= {OperationKind.BUILD, OperationKind.BACKTEST}

    switched = session.dispatch(command=ChooseAccount(account_id="account-b"))

    assert switched.draft is not None
    assert switched.draft.draft_id == "draft-b"
    assert not any(
        error.operation in {OperationKind.BUILD, OperationKind.BACKTEST}
        for error in switched.errors
    )
    with pytest.raises(ValueError, match="Unknown session error 'build'"):
        session.dispatch(command=RetryError(error_id="build"))
    with pytest.raises(ValueError, match="Unknown session error 'backtest'"):
        session.dispatch(command=RetryError(error_id="backtest"))


class _CardImageResponse:
    def __init__(self, *, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _CardImageResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self, limit: int) -> bytes:
        return self._payload[:limit]


class _ControlledCardImageService:
    def __init__(
        self,
        *,
        cache_dir: Path,
        failing_uris: frozenset[str] = frozenset(),
    ) -> None:
        self._cache_dir = cache_dir
        self._failing_uris = failing_uris
        self._started: dict[str, threading.Event] = {}
        self._released: dict[str, threading.Event] = {}
        self._finished: dict[str, threading.Event] = {}

    def started(self, *, image_uri: str) -> threading.Event:
        return self._started.setdefault(image_uri, threading.Event())

    def release(self, *, image_uri: str) -> threading.Event:
        return self._released.setdefault(image_uri, threading.Event())

    def finished(self, *, image_uri: str) -> threading.Event:
        return self._finished.setdefault(image_uri, threading.Event())

    def fail(self, *, image_uri: str) -> None:
        self._failing_uris = frozenset((image_uri,))

    def resolve_image_uri(
        self,
        *,
        card: CardInfo,
        card_database: CardDatabase,
    ) -> str | None:
        if card.image_uri is not None:
            return card.image_uri
        return card_database.image_uri_for_name(name=card.name)

    def resolve_focused_image_uri(
        self,
        *,
        card: CardInfo,
        card_database: CardDatabase,
    ) -> str | None:
        return self.resolve_image_uri(card=card, card_database=card_database)

    def fetch(self, *, image_uri: str) -> Path:
        self.started(image_uri=image_uri).set()
        try:
            if not self.release(image_uri=image_uri).wait(timeout=1):
                raise RuntimeError("Fixture image fetch was not released.")
            if image_uri in self._failing_uris:
                raise RuntimeError("Fixture image fetch failed.")
            return self._cache_dir / Path(image_uri).name
        finally:
            self.finished(image_uri=image_uri).set()


def _new_controlled_image_session(
    *,
    tmp_path: Path,
) -> tuple[LiveSession, _ControlledCardImageService]:
    service = _ControlledCardImageService(cache_dir=tmp_path)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(with_image_uris=True),
        card_image_service=service,
    )
    return session, service


def test_live_session_publishes_selected_card_image_loading_then_ready(
    tmp_path: Path,
) -> None:
    session, service = _new_controlled_image_session(tmp_path=tmp_path)
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    request = session.selected_card_image_request()

    assert request is not None
    assert session.snapshot.card_image.phase == DataLoadPhase.LOADING
    service.release(image_uri=request.image_uri).set()
    image_result = session.fetch_card_image(request=request)
    image_path = image_result.image_path
    session.complete_card_image_request(
        request=request,
        image_path=image_path,
        image_uri=image_result.image_uri,
    )
    assert session.snapshot.card_image.image_path == str(
        tmp_path / Path(request.image_uri).name
    )
    selected = next(
        recommendation
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id == request.grp_id
    )
    assert selected.card.image_path == str(tmp_path / Path(request.image_uri).name)
    next_grp_id = next(
        recommendation.card.grp_id
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id != request.grp_id
    )
    session.dispatch(command=ChooseRecommendation(grp_id=next_grp_id))

    next_request = session.selected_card_image_request()
    assert next_request is not None
    assert next_request.grp_id == next_grp_id
    assert session.snapshot.card_image.image_path is None
    assert session.snapshot.card_image.phase == DataLoadPhase.LOADING


def test_live_session_queues_each_uncached_recommendation_image(
    tmp_path: Path,
) -> None:
    session, _ = _new_controlled_image_session(tmp_path=tmp_path)
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    requests = session.recommendation_image_requests()

    assert {request.grp_id for request in requests} == {
        recommendation.card.grp_id
        for recommendation in session.snapshot.recommendations.cards
    }
    assert session.selected_card_image_request() is not None


def test_live_session_publishes_one_recommendation_image_without_reselecting(
    tmp_path: Path,
) -> None:
    session, _ = _new_controlled_image_session(tmp_path=tmp_path)
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    selected_grp_id = session.snapshot.recommendations.selected_grp_id
    request = next(
        request
        for request in session.recommendation_image_requests()
        if request.grp_id != selected_grp_id
    )

    session.complete_recommendation_image_request(
        request=request,
        image_path=tmp_path / "recommendation.jpg",
    )

    updated = next(
        recommendation
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id == request.grp_id
    )
    untouched = next(
        recommendation
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id != request.grp_id
    )
    assert updated.card.image_path == str(tmp_path / "recommendation.jpg")
    assert untouched.card.image_path is None
    assert request not in session.recommendation_image_requests()


def test_live_session_retains_deferred_recommendation_image_after_rescore(
    tmp_path: Path,
) -> None:
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        card_image_service=_ControlledCardImageService(cache_dir=tmp_path),
    )
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    request = next(iter(session.recommendation_image_requests()))
    assert request.image_uri is None
    image_path = tmp_path / "deferred-recommendation.jpg"
    resolved_uri = "https://cards.example/deferred-recommendation.jpg"
    session.complete_recommendation_image_request(
        request=request,
        image_path=image_path,
        image_uri=resolved_uri,
    )

    rescored = session.dispatch(command=ChangeRanking(ranking_mode="win_rate"))
    rescored = session.dispatch(command=ChangeSplashPreference(enabled=False))

    recommendation = next(
        recommendation
        for recommendation in rescored.recommendations.cards
        if recommendation.card.grp_id == request.grp_id
    )
    assert recommendation.card.image_path == str(image_path)
    assert all(
        queued.grp_id != request.grp_id
        for queued in session.recommendation_image_requests()
    )


def test_live_session_recommendation_cache_skips_background_request(
    tmp_path: Path,
) -> None:
    database = _fixture_card_database(with_image_uris=True)
    service = CardImageService(
        cache_dir=tmp_path / "card-images",
        opener=lambda *_args, **_kwargs: pytest.fail("cached image was fetched"),
    )
    cached_card = database.lookup(grp_id=next(iter(database.cards)))
    assert cached_card.image_uri is not None
    cached_path = service.cached_path(image_uri=cached_card.image_uri)
    cached_path.parent.mkdir(parents=True)
    cached_path.write_bytes(b"fixture")
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=database,
        card_image_service=service,
    )
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    cached_recommendation = next(
        recommendation
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id == cached_card.grp_id
    )
    assert cached_recommendation.card.image_path == str(cached_path)
    assert cached_card.grp_id not in {
        request.grp_id for request in session.recommendation_image_requests()
    }


def test_live_session_recommendation_image_failure_keeps_queue_moving(
    tmp_path: Path,
) -> None:
    session, _ = _new_controlled_image_session(tmp_path=tmp_path)
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )
    selected_grp_id = session.snapshot.recommendations.selected_grp_id
    request = next(
        request
        for request in session.recommendation_image_requests()
        if request.grp_id != selected_grp_id
    )
    remaining = next(
        queued
        for queued in session.recommendation_image_requests()
        if queued != request
    )

    session.fail_recommendation_image_request(
        request=request,
        error_message="network down",
    )

    assert session.recommendation_image_request() == remaining


def test_live_session_ignores_recommendation_image_from_replaced_pack(
    tmp_path: Path,
) -> None:
    session, _ = _new_controlled_image_session(tmp_path=tmp_path)
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )
    old_request = session.recommendation_image_requests()[0]
    current_pack = session.current_pack_event
    assert current_pack is not None
    session._current_pack_event = replace(
        current_pack,
        pick_number=current_pack.pick_number + 1,
    )
    session._score_current_pack()

    session.complete_recommendation_image_request(
        request=old_request,
        image_path=tmp_path / "stale.jpg",
    )

    assert all(
        recommendation.card.image_path != str(tmp_path / "stale.jpg")
        for recommendation in session.snapshot.recommendations.cards
    )


def test_live_session_retains_recommendation_details_when_card_image_fails(
    tmp_path: Path,
) -> None:
    session, service = _new_controlled_image_session(tmp_path=tmp_path)
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    request = session.selected_card_image_request()
    assert request is not None
    selected_while_loading = next(
        recommendation
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id == request.grp_id
    )
    service.fail(image_uri=request.image_uri)
    service.release(image_uri=request.image_uri).set()

    with pytest.raises(RuntimeError, match="Fixture image fetch failed"):
        session.fetch_card_image(request=request)
    session.fail_card_image_request(
        request=request,
        error_message="Fixture image fetch failed.",
    )

    assert session.snapshot.card_image.phase == DataLoadPhase.FAILED
    selected = next(
        recommendation
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id == request.grp_id
    )
    assert selected == selected_while_loading
    session.dispatch(command=ChooseRecommendation(grp_id=request.grp_id))

    retry_request = session.selected_card_image_request()

    assert retry_request is not None
    assert retry_request.generation != request.generation
    assert session.snapshot.card_image.phase == DataLoadPhase.LOADING


def test_live_session_keeps_metadata_protocol_failure_local_until_explicit_retry(
    tmp_path: Path,
) -> None:
    metadata_requests = 0

    def metadata_opener(*_args: object, **_kwargs: object) -> object:
        nonlocal metadata_requests
        metadata_requests += 1
        raise http.client.IncompleteRead(b'{"image_uris":', 100)

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(),
        card_image_service=CardImageService(
            cache_dir=tmp_path / "card-images",
            metadata_opener=metadata_opener,
        ),
    )
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    selected_grp_id = session.snapshot.recommendations.selected_grp_id
    request = session.selected_card_image_request()
    assert selected_grp_id is not None
    assert request is not None
    assert request.image_uri is None
    assert session.snapshot.card_image.phase == DataLoadPhase.LOADING
    assert metadata_requests == 0

    with pytest.raises(CardImageError, match="Card metadata lookup failed"):
        session.fetch_card_image(request=request)
    session.fail_card_image_request(request=request, error_message="protocol error")

    failed = session.snapshot.card_image
    assert failed.phase == DataLoadPhase.FAILED
    assert session.selected_card_image_request() is None
    assert metadata_requests == 1

    session.process_lines(lines=("unrelated log text",))

    assert session.snapshot.card_image == failed
    assert metadata_requests == 1

    session.dispatch(command=ChooseRecommendation(grp_id=selected_grp_id))
    retry_request = session.selected_card_image_request()

    assert session.snapshot.card_image.phase == DataLoadPhase.LOADING
    assert retry_request is not None
    assert retry_request.image_uri is None
    assert metadata_requests == 1


def test_live_session_ignores_stale_selected_card_image_completion(
    tmp_path: Path,
) -> None:
    session, service = _new_controlled_image_session(tmp_path=tmp_path)
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )
    first_request = session.selected_card_image_request()
    assert first_request is not None
    second_grp_id = next(
        recommendation.card.grp_id
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id != first_request.grp_id
    )

    session.dispatch(command=ChooseRecommendation(grp_id=second_grp_id))
    second_request = session.selected_card_image_request()
    service.release(image_uri=first_request.image_uri).set()
    stale_result = session.fetch_card_image(request=first_request)
    service.release(image_uri=second_request.image_uri).set()
    current_result = session.fetch_card_image(request=second_request)
    session.complete_card_image_request(
        request=second_request,
        image_path=current_result.image_path,
        image_uri=current_result.image_uri,
    )
    session.complete_card_image_request(
        request=first_request,
        image_path=stale_result.image_path,
        image_uri=stale_result.image_uri,
    )

    assert session.snapshot.recommendations.selected_grp_id == second_request.grp_id
    assert session.snapshot.card_image.grp_id == second_request.grp_id
    assert session.snapshot.card_image.phase == DataLoadPhase.READY


def test_live_session_focuses_current_build_card_image_only(
    tmp_path: Path,
) -> None:
    service = _ControlledCardImageService(cache_dir=tmp_path)
    database = _fixture_card_database(with_image_uris=True)
    app_dir = tmp_path / "app"
    state = _draft_state(
        account_id="account-1",
        screen_name="Player",
        draft_id="draft-1",
        updated_at="2026-08-23T10:00:00+00:00",
        pool_grp_ids=tuple(database.cards),
    )
    save_draft_state(state=state, app_dir=app_dir)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
        card_image_service=service,
    )
    session.dispatch(command=ChooseAccount(account_id="account-1"))
    build_snapshot = session.dispatch(command=RequestBuild())
    assert build_snapshot.build is not None
    focused_grp_id = build_snapshot.build.spells[0].card.grp_id

    session.dispatch(command=FocusBuildCard(grp_id=focused_grp_id))

    request = session.selected_card_image_request()
    assert request is not None
    assert request.grp_id == focused_grp_id
    assert session.snapshot.card_image == CardImageState(
        grp_id=focused_grp_id,
        phase=DataLoadPhase.LOADING,
        message=f"Loading image for {build_snapshot.build.spells[0].card.name}.",
    )
    focused_card_image = session.snapshot.card_image
    empty_poll = session.process_lines(lines=())

    assert empty_poll.card_image == focused_card_image
    assert session.selected_card_image_request() == request
    with pytest.raises(ValueError, match="is not in the current build"):
        session.dispatch(command=FocusBuildCard(grp_id=-1))


def test_live_session_ratings_rescore_preserves_current_build_image_request(
    tmp_path: Path,
) -> None:
    service = _ControlledCardImageService(cache_dir=tmp_path)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=_fixture_card_database(with_image_uris=True),
        card_image_service=service,
        ratings_loader=lambda set_code: _fixture_ratings_data(set_code=set_code),
        ratings_cache_checker=lambda _set_code: False,
    )
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    session.process_lines(lines=fixture_lines[:10])
    build_snapshot = session.dispatch(command=RequestBuild())

    assert build_snapshot.build is not None
    focused_grp_id = build_snapshot.build.spells[0].card.grp_id
    session.dispatch(command=FocusBuildCard(grp_id=focused_grp_id))
    build_image_request = session.selected_card_image_request()
    assert build_image_request is not None
    build_image_state = session.snapshot.card_image

    rescored = session.dispatch(command=RequestRatingsDownload(set_code="MSH"))

    assert rescored.ratings.phase == DataLoadPhase.READY
    assert rescored.card_image == build_image_state
    assert session.selected_card_image_request() == build_image_request


def test_live_session_stop_retires_in_flight_card_image_request(
    tmp_path: Path,
) -> None:
    session, _ = _new_controlled_image_session(tmp_path=tmp_path)
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    assert session.snapshot.card_image.phase == DataLoadPhase.LOADING
    stopped = session.stop()

    assert stopped.status.phase == ApplicationPhase.STOPPED
    assert stopped.card_image.phase != DataLoadPhase.LOADING
    assert session.selected_card_image_request() is None


def test_live_session_stop_rejects_late_profile_refresh_completion(
    tmp_path: Path,
) -> None:
    profile = _fixture_set_profile()
    client = _ProfileClientStub({"TST": profile})
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        profile_client=client,
    )
    session._set_active_set_code(set_code="TST")
    request = session.profile_refresh_request()
    assert request is not None

    stopped = session.stop()
    session.complete_profile_refresh(
        request=request,
        result=ProfileRefreshResult(
            profile=replace(
                profile,
                profile_version="2.0",
                generated_at="2026-08-30T00:00:00+00:00",
            ),
            outcome=ProfileRefreshOutcome.UPDATED,
        ),
    )

    assert session.snapshot is stopped
    assert session.snapshot.status.phase is ApplicationPhase.STOPPED
    assert session.profile_refresh_request() is None


@pytest.mark.parametrize(
    "image_phase",
    (DataLoadPhase.LOADING, DataLoadPhase.READY),
)
def test_live_session_switch_to_account_without_pending_pack_retires_card_image(
    tmp_path: Path,
    image_phase: DataLoadPhase,
) -> None:
    app_dir = tmp_path / "app"
    service = _ControlledCardImageService(cache_dir=tmp_path)
    database = _fixture_card_database(with_image_uris=True)
    save_draft_state(
        state=replace(
            _draft_state(
                account_id="account-without-pack",
                screen_name="No Pending Pack",
                draft_id="draft-without-pack",
                updated_at="2026-08-23T12:00:00+00:00",
                pool_grp_ids=(),
            ),
            completed=True,
            completed_at="2026-08-23T12:00:00+00:00",
        ),
        app_dir=app_dir,
    )
    published: list[LiveSessionSnapshot] = []
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
        card_image_service=service,
        snapshot_publisher=published.append,
    )
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )
    request = session.selected_card_image_request()
    assert request is not None

    if image_phase == DataLoadPhase.READY:
        service.release(image_uri=request.image_uri).set()
        image_result = session.fetch_card_image(request=request)
        session.complete_card_image_request(
            request=request,
            image_path=image_result.image_path,
            image_uri=image_result.image_uri,
        )

    assert session.snapshot.card_image.phase == image_phase
    switched = session.dispatch(
        command=ChooseAccount(account_id="account-without-pack")
    )

    assert switched.active_account is not None
    assert switched.active_account.account_id == "account-without-pack"
    assert switched.current_pack_event is None
    assert switched.recommendations.cards == ()
    assert switched.recommendations.selected_grp_id is None
    assert switched.card_image == CardImageState()
    assert session.selected_card_image_request() is None
    account_snapshots = tuple(
        snapshot
        for snapshot in published
        if snapshot.active_account is not None
        and snapshot.active_account.account_id == "account-without-pack"
    )
    assert account_snapshots
    assert all(
        snapshot.card_image == CardImageState()
        for snapshot in account_snapshots
    )


def test_live_session_resolves_name_indexed_selected_card_image_uri(
    tmp_path: Path,
) -> None:
    database = _fixture_card_database()
    database = replace(
        database,
        image_uris_by_name={
            card.name.lower(): f"https://images.example/{card.grp_id}.jpg"
            for card in database.cards.values()
        },
    )
    service = _ControlledCardImageService(cache_dir=tmp_path)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=database,
        card_image_service=service,
    )
    _process_until_recommendations(
        session=session,
        lines=FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines(),
    )

    request = session.selected_card_image_request()

    assert request is not None
    assert request.image_uri == f"https://images.example/{request.grp_id}.jpg"
    service.release(image_uri=request.image_uri).set()
    image_result = session.fetch_card_image(request=request)
    session.complete_card_image_request(
        request=request,
        image_path=image_result.image_path,
        image_uri=image_result.image_uri,
    )
    selected = next(
        recommendation
        for recommendation in session.snapshot.recommendations.cards
        if recommendation.card.grp_id == request.grp_id
    )
    assert selected.card.image_path == str(image_result.image_path)


def test_live_session_account_recovery_requests_selected_card_image(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    database = _fixture_card_database(with_image_uris=True)
    offered_grp_ids = tuple(database.cards)[:2]
    recovered_state = replace(
        _draft_state(
            account_id="account-recovery",
            screen_name="Recovered",
            draft_id="draft-recovery",
            updated_at="2026-08-23T10:00:00+00:00",
            pool_grp_ids=(),
        ),
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=0,
                offered_grp_ids=offered_grp_ids,
                pool_before_pick=(),
                chosen_grp_id=None,
            ),
        ),
    )
    save_draft_state(state=recovered_state, app_dir=app_dir)
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        card_database=database,
        card_image_service=_ControlledCardImageService(cache_dir=tmp_path),
    )

    recovered = session.dispatch(
        command=ChooseAccount(account_id="account-recovery")
    )
    request = session.selected_card_image_request()

    assert recovered.card_image.phase == DataLoadPhase.LOADING
    assert request is not None
    assert request.grp_id == recovered.recommendations.selected_grp_id


def test_live_session_queues_metadata_missing_focus_without_blocking_resolution(
    tmp_path: Path,
) -> None:
    database = _fixture_card_database()
    historical_grp_ids = (
        104905,
        105032,
        105003,
        105142,
        105054,
        105076,
        105143,
        104938,
        105111,
        105087,
        105011,
        105182,
    )
    database = replace(
        database,
        cards={
            **database.cards,
            **{
                grp_id: CardInfo(
                    grp_id=grp_id,
                    name=f"Fixture Card {grp_id}",
                    colors=("W",),
                    mana_value=2.0,
                    rarity="common",
                    types=("Creature",),
                )
                for grp_id in historical_grp_ids
            },
        },
    )
    metadata_requests: list[str] = []

    def metadata_opener(request: object, *, timeout: float) -> _CardImageResponse:
        metadata_requests.append(request.full_url)  # type: ignore[attr-defined]
        return _CardImageResponse(
            payload=b'{"image_uris":{"normal":"https://cards.example/final.jpg"}}'
        )

    service = CardImageService(
        cache_dir=tmp_path / "images",
        metadata_opener=metadata_opener,
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=database,
        card_image_service=service,
    )
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()

    snapshot = session.process_lines(
        lines=tuple(fixture_lines[index] for index in (1, 2, 3, 6, 7, 9, 10, 12))
    )
    request = session.selected_card_image_request()
    selected_grp_id = snapshot.recommendations.selected_grp_id

    assert snapshot.current_pack_event is not None
    assert snapshot.current_pack_event.pick_number == 2
    assert request is not None
    assert request.grp_id == selected_grp_id
    assert selected_grp_id is not None
    assert request.image_uri is None
    assert metadata_requests == []


def test_live_session_build_focus_resolves_nighthowl_without_row_metadata_io(
    tmp_path: Path,
) -> None:
    card = CardInfo(
        grp_id=103454,
        name="Nighthowl Pursuer",
        colors=("B",),
        mana_value=3.0,
        rarity="uncommon",
        types=("Creature",),
    )
    database = CardDatabase(cards={card.grp_id: card})
    metadata_requests: list[str] = []

    def metadata_opener(request: object, *, timeout: float) -> _CardImageResponse:
        metadata_requests.append(request.full_url)  # type: ignore[attr-defined]
        return _CardImageResponse(
            payload=(
                b'{"name":"Nighthowl Pursuer","image_uris":'
                b'{"normal":"https://cards.example/nighthowl.jpg"}}'
            )
        )

    service = CardImageService(
        cache_dir=tmp_path / "images",
        metadata_opener=metadata_opener,
        opener=lambda request, timeout: _CardImageResponse(payload=b"image bytes"),
    )
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=database,
        card_image_service=service,
    )

    assert session._card_view(card=card).image_path is None
    assert metadata_requests == []
    session._publish(
        snapshot=replace(
            session.snapshot,
            build=BuildResult(
                selected_pair="B",
                pair_options=(),
                spells=(BuildCard(card=session._card_view(card=card), quantity=1),),
                lands=(),
                bench=(),
                deck_size=1,
            ),
        )
    )

    session.dispatch(command=FocusBuildCard(grp_id=card.grp_id))
    request = session.selected_card_image_request()

    assert session.snapshot.card_image.phase == DataLoadPhase.LOADING
    assert request is not None
    assert request.image_uri is None
    assert metadata_requests == []
    focused_card_image = session.snapshot.card_image
    empty_poll = session.process_lines(lines=())

    assert empty_poll.card_image == focused_card_image
    assert session.selected_card_image_request() == request
    assert metadata_requests == []
    image_result = session.fetch_card_image(request=request)
    session.complete_card_image_request(
        request=request,
        image_path=image_result.image_path,
        image_uri=image_result.image_uri,
    )

    assert session.snapshot.card_image.image_path == str(image_result.image_path)
    assert image_result.image_path.read_bytes() == b"image bytes"


def test_live_session_retries_focused_metadata_failure(tmp_path: Path) -> None:
    card = CardInfo(
        grp_id=103454,
        name="Nighthowl Pursuer",
        colors=("B",),
        mana_value=3.0,
        rarity="uncommon",
        types=("Creature",),
    )
    database = CardDatabase(cards={card.grp_id: card})
    metadata_attempts = 0

    def metadata_opener(request: object, *, timeout: float) -> _CardImageResponse:
        nonlocal metadata_attempts
        metadata_attempts += 1
        if metadata_attempts == 1:
            raise OSError("offline")
        return _CardImageResponse(
            payload=b'{"image_uris":{"normal":"https://cards.example/nighthowl.jpg"}}'
        )

    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        card_database=database,
        card_image_service=CardImageService(
            cache_dir=tmp_path / "images",
            monotonic_clock=lambda: 10.0,
            sleep=lambda seconds: None,
            metadata_opener=metadata_opener,
            opener=lambda *_args, **_kwargs: _CardImageResponse(
                payload=b"image bytes"
            ),
        ),
    )

    session._start_focused_card_image_load(grp_id=card.grp_id)
    request = session.selected_card_image_request()
    assert request is not None
    assert request.image_uri is None
    assert session.snapshot.card_image.phase == DataLoadPhase.LOADING

    with pytest.raises(CardImageError, match="Card metadata lookup failed"):
        session.fetch_card_image(request=request)
    session.fail_card_image_request(request=request, error_message="offline")

    assert session.snapshot.card_image.phase == DataLoadPhase.FAILED
    assert session.selected_card_image_request() is None

    session._start_focused_card_image_load(grp_id=card.grp_id)
    retry_request = session.selected_card_image_request()
    assert retry_request is not None
    assert retry_request.image_uri is None
    assert metadata_attempts == 1
    result = session.fetch_card_image(request=retry_request)
    assert result.image_uri == "https://cards.example/nighthowl.jpg"
    assert metadata_attempts == 2


def _card() -> CardView:
    return CardView(
        grp_id=123,
        name="Fixture Card",
        colors=("W",),
        rarity="uncommon",
        types=("Creature",),
        mana_cost="{1}{W}",
        mana_value=2.0,
        image_path="cache/card.jpg",
    )


def _process_until_recommendations(
    *,
    session: LiveSession,
    lines: list[str],
) -> int:
    for line_index, line in enumerate(lines):
        snapshot = session.process_lines(lines=(line,))
        if snapshot.recommendations.cards:
            return line_index

    raise AssertionError("Fixture did not publish pack recommendations.")


def _fixture_card_database(*, with_image_uris: bool = False) -> CardDatabase:
    offered_grp_ids = (
        104894,
        104976,
        105080,
        104995,
        105027,
        105030,
        105170,
        104932,
        104893,
        105091,
        104969,
        105097,
        104979,
        105164,
    )
    mana_values = {
        104894: 4.0,
        104976: 3.0,
        105080: 1.0,
    }
    return CardDatabase(
        cards={
            grp_id: CardInfo(
                grp_id=grp_id,
                name=f"Fixture Card {grp_id}",
                colors=("W", "U"),
                mana_value=mana_values.get(grp_id, 5.0),
                rarity="common",
                types=("Creature",),
                image_uri=(
                    f"https://images.example/{grp_id}.jpg"
                    if with_image_uris
                    else None
                ),
            )
            for grp_id in offered_grp_ids
        }
    )


def _fixture_set_card_database(*, set_code: str) -> CardDatabase:
    database = _fixture_card_database()
    return replace(
        database,
        cards={
            grp_id: replace(card, set_code=set_code.casefold())
            for grp_id, card in database.cards.items()
        },
    )


def _fixture_ratings_data(
    *,
    set_code: str,
    fetched_at: datetime | None = None,
) -> SeventeenLandsData:
    fetched_at = (
        datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
        if fetched_at is None
        else fetched_at
    )
    return SeventeenLandsData(
        set_code=set_code,
        requested_format=QUICK_DRAFT_FORMAT,
        primary=SeventeenLandsFormatData(
            set_code=set_code,
            event_format=QUICK_DRAFT_FORMAT,
            fetched_at=fetched_at,
            card_ratings={
                104894: _fixture_stats(
                    grp_id=104894,
                    color="WU",
                    gih_win_rate=0.65,
                    average_last_seen_at=3.0,
                ),
            },
            pair_win_rates={},
        ),
        fallback=SeventeenLandsFormatData(
            set_code=set_code,
            event_format=PREMIER_DRAFT_FORMAT,
            fetched_at=fetched_at,
            card_ratings={
                104976: _fixture_stats(
                    grp_id=104976,
                    color="WU",
                    gih_win_rate=0.60,
                    average_last_seen_at=1.0,
                ),
            },
            pair_win_rates={},
        ),
    )


def _fixture_stats(
    *,
    grp_id: int,
    color: str,
    gih_win_rate: float,
    average_last_seen_at: float,
) -> SeventeenCardStats:
    return SeventeenCardStats(
        grp_id=grp_id,
        name=f"Fixture Card {grp_id}",
        color=color,
        rarity="common",
        average_last_seen_at=average_last_seen_at,
        gih_win_rate=gih_win_rate,
        opening_hand_win_rate=gih_win_rate,
        drawn_improvement_win_rate=0.0,
        sample_counts=RatingSampleCounts(
            seen=2_000,
            picked=1_500,
            games_played=1_200,
            opening_hand=800,
            games_in_hand=1_000,
        ),
    )


def _fixture_set_profile() -> SetProfile:
    return load_set_profile(
        FIXTURE_PROFILE_PATH,
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )


def _fixture_pool_before_pick(
    *,
    pack_number: int,
    pick_number: int,
) -> tuple[int, ...]:
    return (104894,) * (pack_number * EXPECTED_PICKS_PER_PACK + pick_number)


def _profiled_history_lines(*, pool_before_pick: tuple[int, ...]) -> list[str]:
    lines = [
        _auth_line(account_id="profiled-account", screen_name="Profiled"),
        _course_line(
            event_name=CONTEXT_EVENT_NAME,
            course_id="profiled-draft",
        ),
    ]
    for pick_index, picked_card in enumerate(pool_before_pick):
        pack_number, pick_number = divmod(
            pick_index,
            EXPECTED_PICKS_PER_PACK,
        )
        lines.extend(
            (
                _pack_line(
                    event_name=CONTEXT_EVENT_NAME,
                    pack_number=pack_number,
                    pick_number=pick_number,
                    draft_pack=(picked_card,),
                    picked_cards=pool_before_pick[:pick_index],
                ),
                _pick_request_line(
                    event_name=CONTEXT_EVENT_NAME,
                    request_id=f"profiled-pick-{pick_index}",
                    card_id=picked_card,
                    pack_number=pack_number,
                    pick_number=pick_number,
                ),
            )
        )
    lines.append(_profiled_pack_line(pool_before_pick=pool_before_pick))
    return lines


def _profiled_pack_line(*, pool_before_pick: tuple[int, ...]) -> str:
    return _pack_line(
        event_name=CONTEXT_EVENT_NAME,
        pack_number=CONTEXT_PACK_NUMBER,
        pick_number=CONTEXT_PICK_NUMBER,
        draft_pack=CONTEXT_OFFERED_GRP_IDS,
        picked_cards=pool_before_pick,
    )


def _assert_profile_context(
    *,
    scored_pack: ScoredPack,
    profile: SetProfile,
    event: PackOfferedEvent,
) -> PickScoringContext:
    context = scored_pack.scoring_context
    assert context is not None
    assert context.set_profile == profile
    assert context.set_profile.fingerprint == profile.fingerprint
    assert context.set_profile.source == profile.source
    assert context.stage.pack_number == event.pack_number
    assert context.stage.pick_number == event.pick_number
    global_pick_index = session_module._draft_pick_index(event=event)
    assert context.stage.global_pick_index == global_pick_index
    assert context.stage.estimated_remaining_picks == max(
        0,
        EXPECTED_TOTAL_PICKS - global_pick_index,
    )
    assert context.role_ledger.pool_size == len(event.pool_grp_ids)
    assert context.role_ledger.profile_fingerprint == profile.fingerprint
    assert context.role_ledger.profile_source == f"profile:{profile.maturity.value}"
    return context


def _assert_recommendation_context_parity(
    *,
    snapshot: LiveSessionSnapshot,
    scored_pack: ScoredPack,
) -> dict[int, ScoredCard]:
    recommendations = {
        recommendation.card.grp_id: recommendation
        for recommendation in snapshot.recommendations.cards
    }
    scored_cards = {card.card.grp_id: card for card in scored_pack.cards}
    assert recommendations
    assert recommendations.keys() == scored_cards.keys()
    for grp_id, recommendation in recommendations.items():
        scored_card = scored_cards[grp_id]
        for field_name in (
            "contextual_breakdown",
            "contextual_evidence",
            "contextual_pair",
            "contextual_theme",
            "contextual_profile_maturity",
            "contextual_profile_confidence",
        ):
            assert getattr(recommendation, field_name) == getattr(
                scored_card,
                field_name,
            )
    return scored_cards


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


def _payload_line(*, module: str, payload: dict[str, object]) -> str:
    return json.dumps({"CurrentModule": module, "Payload": json.dumps(payload)})


def _write_lines(*, path: Path, lines: list[str]) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _append_lines(*, path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as log_file:
        log_file.writelines(f"{line}\n" for line in lines)


def _auth_line(*, account_id: str, screen_name: str) -> str:
    return json.dumps(
        {
            "authenticateResponse": {
                "clientId": account_id,
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


def _draft_state(
    *,
    account_id: str,
    screen_name: str,
    draft_id: str,
    updated_at: str,
    pool_grp_ids: tuple[int, ...],
) -> DraftState:
    return DraftState(
        account_id=account_id,
        account_screen_name=screen_name,
        draft_id=draft_id,
        event_name="QuickDraft_TST_20260823",
        set_code="TST",
        course_id=draft_id,
        started_at="2026-08-20T10:00:00+00:00",
        updated_at=updated_at,
        completed_at=None,
        completed=False,
        picks=(),
        pool_grp_ids=pool_grp_ids,
    )
