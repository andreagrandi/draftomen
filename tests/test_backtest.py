from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import draftomen.cli as cli_module
from draftomen.backtest import (
    format_backtest_report,
    generate_backtest_report,
    load_persisted_backtest_state,
)
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.cli import main
from draftomen.pickengine import (
    MAX_CONTEXTUAL_ADJUSTMENT,
    MAX_SYNERGY_TERM,
    ContextualScoreBreakdown,
    PickEngine,
)
from draftomen.pool import DraftPick, DraftState, draft_state_path, save_draft_state
from draftomen.semantic_roles import (
    CompiledRoleProfile,
    ProfileCard,
    Role,
    RoleAssignment,
)
from draftomen.set_profile import (
    CardRating,
    PairProfile,
    ProfileMaturity,
    RateEstimate,
    RoleTarget,
    SampleSummary,
    SetProfile,
    SourceMetadata,
    dump_set_profile,
    load_set_profile,
    set_profile_path,
)
from draftomen.seventeen import QUICK_DRAFT_FORMAT

FIXTURE_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC).isoformat()


def test_backtest_uses_saved_pool_before_pick_for_recommendation() -> None:
    state = _draft_state(
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=0,
                offered_grp_ids=(4, 3),
                pool_before_pick=(),
                chosen_grp_id=3,
            ),
            DraftPick(
                pack_number=0,
                pick_number=5,
                offered_grp_ids=(4, 3),
                pool_before_pick=(1, 2),
                chosen_grp_id=3,
            ),
        ),
        pool_grp_ids=(4, 4, 4),
    )

    report = generate_backtest_report(
        state=state,
        card_database=_card_database(),
    )
    output = format_backtest_report(report)

    assert report.rows[0].recommended is not None
    assert report.rows[0].recommended.card.grp_id == 4
    assert report.rows[0].actual is not None
    assert report.rows[0].actual.grp_id == 3
    assert report.rows[0].match is False
    assert report.rows[1].recommended is not None
    assert report.rows[1].recommended.card.grp_id == 3
    assert report.rows[1].pool_size == 2
    assert report.rows[1].role_ledger is not None
    assert report.rows[1].role_ledger.pool_size == 2
    assert report.rows[1].role_ledger.stage is not None
    assert report.rows[1].role_ledger.stage.global_pick_index == 6
    assert report.rows[1].offered_count == 2
    assert report.rows[1].match is True
    assert "Ranking: DO Score" in output
    assert "Data sources: neutral prior" in output
    assert "Red Temptation [R] (grpId 4)" in output
    assert "White Followup [W] (grpId 3)" in output
    assert "Summary: 1/2 recommendations matched actual picks (50.0%)." in output


def test_backtest_retains_profile_context_and_recommendation_evidence() -> None:
    state = _draft_state(
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=5,
                offered_grp_ids=(4, 3),
                pool_before_pick=(1, 2),
                chosen_grp_id=3,
            ),
        ),
        pool_grp_ids=(1, 2, 3),
    )
    profile = _set_profile()

    report = generate_backtest_report(
        state=state,
        card_database=_card_database(),
        set_profile=profile,
    )

    row = report.rows[0]
    assert row.recommended is not None
    assert row.scoring_context is not None
    assert row.scoring_context.set_profile is profile
    assert row.scoring_context.stage.pack_number == 0
    assert row.scoring_context.stage.pick_number == 5
    assert row.scoring_context.stage.global_pick_index == 6
    assert row.scoring_context.stage.estimated_remaining_picks == (
        42 - row.scoring_context.stage.global_pick_index
    )
    assert row.scoring_context.role_ledger.pool_size == 2
    assert row.contextual_evidence is row.recommended.contextual_evidence
    assert row.contextual_evidence == row.recommended.contextual_evidence


def test_backtest_contextual_mode_changes_saved_pick_scoring() -> None:
    state = _draft_state(
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=5,
                offered_grp_ids=(3, 4),
                pool_before_pick=(1, 2),
                chosen_grp_id=3,
            ),
        ),
        pool_grp_ids=(1, 2, 3),
    )
    database = _contextual_backtest_database()
    profile = _contextual_backtest_profile()

    enabled = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=profile,
    ).rows[0]
    disabled = generate_backtest_report(
        state=state,
        card_database=database,
        contextual_adjustments_enabled=False,
        set_profile=profile,
    ).rows[0]

    assert enabled.recommended is not None
    assert disabled.recommended is not None
    assert enabled.recommended.card.grp_id == disabled.recommended.card.grp_id == 3
    assert enabled.recommended.raw_score > disabled.recommended.raw_score
    assert enabled.contextual_evidence
    assert disabled.contextual_evidence == ()
    assert disabled.recommended.rating.metadata.source == "profile"


def test_backtest_injected_engine_configuration_takes_precedence() -> None:
    state = _draft_state(
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=5,
                offered_grp_ids=(3, 4),
                pool_before_pick=(1, 2),
                chosen_grp_id=3,
            ),
        ),
        pool_grp_ids=(1, 2, 3),
    )
    profile = _contextual_backtest_profile()
    engine = PickEngine(
        contextual_adjustments_enabled=False,
        set_profile=profile,
    )

    row = generate_backtest_report(
        state=state,
        card_database=_contextual_backtest_database(),
        contextual_adjustments_enabled=True,
        pick_engine=engine,
    ).rows[0]

    assert row.recommended is not None
    assert row.contextual_evidence == ()
    assert row.recommended.contextual_evidence == ()
    assert engine.contextual_adjustments_enabled is False


def test_backtest_no_profile_retains_generic_scoring_without_context() -> None:
    state = _draft_state(
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=5,
                offered_grp_ids=(4, 3),
                pool_before_pick=(1, 2),
                chosen_grp_id=3,
            ),
        ),
        pool_grp_ids=(1, 2, 3),
    )

    row = generate_backtest_report(
        state=state,
        card_database=_card_database(),
    ).rows[0]

    assert row.scoring_context is None
    assert row.contextual_evidence == ()
    assert row.recommended is not None
    assert row.recommended.contextual_evidence == ()


def _set_profile() -> SetProfile:
    return SetProfile(
        set_code="TST",
        event_format="quickdraft",
        profile_version="backtest-context-test",
        generated_at="1970-01-01T00:00:00+00:00",
        source=SourceMetadata(provider="test"),
        maturity=ProfileMaturity.MATURE,
        samples=SampleSummary(total=1, by_pair=(("WU", 1),)),
        confidence=1.0,
        pairs=(PairProfile(pair="WU"),),
    )


def _contextual_backtest_profile() -> SetProfile:
    return SetProfile(
        set_code="TST",
        event_format="quickdraft",
        profile_version="backtest-contextual-mode-test",
        generated_at="1970-01-01T00:00:00+00:00",
        source=SourceMetadata(provider="test"),
        maturity=ProfileMaturity.MATURE,
        samples=SampleSummary(total=100, by_pair=(("WU", 100),)),
        confidence=1.0,
        pairs=(
            PairProfile(
                pair="WU",
                role_targets=(RoleTarget(role=Role.DRAW, value=1),),
            ),
        ),
        role_profile=CompiledRoleProfile(
            set_code="TST",
            cards=(
                ProfileCard(
                    key="arena_id:3",
                    assignments=(RoleAssignment(role=Role.DRAW),),
                ),
            ),
        ),
        card_ratings=tuple(
            CardRating(
                card_key=f"arena_id:{grp_id}",
                gih_win_rate=RateEstimate(
                    raw_value=value,
                    value=value,
                    samples=100,
                    prior_value=0.5,
                    source="test",
                ),
            )
            for grp_id, value in ((3, 0.72), (4, 0.68), (5, 0.90))
        ),
    )


def _contextual_backtest_database() -> CardDatabase:
    cards = {
        grp_id: replace(card, set_code="TST", arena_id=grp_id)
        for grp_id, card in _card_database().cards.items()
    }
    cards[5] = replace(
        _card(grp_id=5, name="White Ceiling", colors=("W",)),
        set_code="TST",
        arena_id=5,
    )
    return CardDatabase(cards=cards)


def test_backtest_cli_skips_missing_offered_history_without_mutating_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_dir = tmp_path / "app"
    bulk_file = _write_bulk_file(directory=tmp_path)
    state = _draft_state(
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=0,
                offered_grp_ids=None,
                pool_before_pick=(),
                chosen_grp_id=3,
            ),
        ),
        pool_grp_ids=(3,),
    )
    save_draft_state(state=state, app_dir=app_dir)
    state_path = draft_state_path(
        account_id=state.account_id,
        draft_id=state.draft_id,
        app_dir=app_dir,
    )
    before = state_path.read_text(encoding="utf-8")

    exit_code = main(
        argv=[
            "backtest",
            "--account",
            state.account_id,
            "--draft-id",
            state.draft_id,
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(app_dir),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Draft Omen backtest" in captured.out
    assert "Ranking: DO Score" in captured.out
    assert "skipped: missing offered-card history" in captured.out
    assert "Summary: no comparable picks; 1 skipped." in captured.out
    assert captured.err == ""
    assert state_path.read_text(encoding="utf-8") == before


def test_backtest_cli_loads_state_profile_once_and_passes_it_to_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_dir = tmp_path / "app"
    bulk_file = _write_bulk_file(directory=tmp_path)
    state = _draft_state(
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=5,
                offered_grp_ids=(4, 3),
                pool_before_pick=(1, 2),
                chosen_grp_id=3,
            ),
        ),
        pool_grp_ids=(1, 2, 3),
    )
    save_draft_state(state=state, app_dir=app_dir)
    profile = _set_profile()
    dump_set_profile(
        profile,
        set_profile_path(
            set_code=state.set_code,
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )

    profile_calls: list[tuple[str, str, Path]] = []
    real_load = cli_module.load_scoring_profile

    def record_load(
        set_code: str,
        event_format: str,
        *,
        app_dir: Path | None = None,
        **kwargs: object,
    ) -> SetProfile | None:
        assert app_dir is not None
        profile_calls.append((set_code, event_format, app_dir))
        return real_load(
            set_code=set_code,
            event_format=event_format,
            app_dir=app_dir,
            **kwargs,
        )

    observed: dict[str, object] = {}
    real_generate = cli_module.generate_backtest_report

    def record_generate(**kwargs: object):
        observed["set_profile"] = kwargs["set_profile"]
        return real_generate(**kwargs)

    monkeypatch.setattr(cli_module, "load_scoring_profile", record_load)
    monkeypatch.setattr(cli_module, "generate_backtest_report", record_generate)
    exit_code = main(
        argv=[
            "backtest",
            "--account",
            state.account_id,
            "--draft-id",
            state.draft_id,
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(app_dir),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert profile_calls == [(state.set_code, QUICK_DRAFT_FORMAT, app_dir)]
    assert observed["set_profile"] == profile
    assert "Draft Omen backtest" in captured.out
    assert captured.err == ""


def _draft_state(
    *,
    picks: tuple[DraftPick, ...],
    pool_grp_ids: tuple[int, ...],
) -> DraftState:
    return DraftState(
        account_id="acct",
        account_screen_name="Tester",
        draft_id="draft",
        event_name="QuickDraft_TST_20260703",
        set_code="TST",
        course_id="draft",
        started_at=FIXTURE_NOW,
        updated_at=FIXTURE_NOW,
        completed_at=FIXTURE_NOW,
        completed=True,
        picks=picks,
        pool_grp_ids=pool_grp_ids,
    )


def _card_database() -> CardDatabase:
    return CardDatabase(
        cards={
            1: _card(grp_id=1, name="White Prior", colors=("W",)),
            2: _card(grp_id=2, name="Blue Prior", colors=("U",)),
            3: _card(grp_id=3, name="White Followup", colors=("W",)),
            4: _card(grp_id=4, name="Red Temptation", colors=("R",)),
        }
    )


def _card(*, grp_id: int, name: str, colors: tuple[str, ...]) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=colors,
        mana_value=2.0,
        rarity="common",
        types=("Creature",),
    )


def _write_bulk_file(*, directory: Path) -> Path:
    path = directory / "backtest-bulk.jsonl"
    rows = [
        _scryfall_row(grp_id=1, name="White Prior", colors=["W"]),
        _scryfall_row(grp_id=2, name="Blue Prior", colors=["U"]),
        _scryfall_row(grp_id=3, name="White Followup", colors=["W"]),
        _scryfall_row(grp_id=4, name="Red Temptation", colors=["R"]),
    ]
    path.write_text(
        "".join(f"{json.dumps(row)}\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _scryfall_row(*, grp_id: int, name: str, colors: list[str]) -> dict[str, object]:
    return {
        "arena_id": grp_id,
        "name": name,
        "colors": colors,
        "cmc": 2,
        "rarity": "common",
        "type_line": "Creature — Fixture",
    }


_HOB_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_HOB_PROFILE_FIXTURE_PATH = (
    _HOB_REPOSITORY_ROOT / "tests" / "fixtures" / "hob-relationship-scoring-profile.json"
)
_HOB_STATE_FIXTURE_PATH = (
    _HOB_REPOSITORY_ROOT / "tests" / "fixtures" / "hob-relationship-scoring-state.json"
)


def _load_hob_smoke_module():
    """Load the offline HOB smoke wrapper once for its calibration constants."""
    module_path = _HOB_REPOSITORY_ROOT / "scripts" / "hob_relationship_scoring_smoke.py"
    spec = importlib.util.spec_from_file_location(
        "hob_relationship_scoring_smoke", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_HOB_SMOKE = _load_hob_smoke_module()
_HOB_CARD_ARTIFACT_SHA256 = _HOB_SMOKE.CARD_ARTIFACT_SHA256
_HOB_R1_FINDING_ID = _HOB_SMOKE.R1_FINDING_ID
_HOB_R6_FINDING_ID = _HOB_SMOKE.R6_FINDING_ID
_HOB_R1_MECHANISM = _HOB_SMOKE.R1_MECHANISM
_HOB_R6_MECHANISM = _HOB_SMOKE.R6_MECHANISM
_HOB_R1_RAW_SCORE_DELTA = _HOB_SMOKE.R1_RAW_SCORE_DELTA
_HOB_R6_RAW_SCORE_DELTA = _HOB_SMOKE.R6_RAW_SCORE_DELTA
# The saved offers, pools, and choices stay hardcoded as an independent fixture check.
_HOB_EXPECTED_OFFERS = (
    (103382, 103526, 103525),
    (103531, 103526, 103525),
    (103458, 103530),
    (103526, 103525),
)
_HOB_EXPECTED_POOLS = (
    (103377, 103405, 103410, 103415),
    (103377, 103405, 103410, 103415, 103382),
    (103377, 103405, 103410, 103415, 103382, 103531),
    (103377, 103405, 103410, 103415, 103382),
)
_HOB_EXPECTED_RECOMMENDATIONS = (103526, 103526, 103458, 103526)
_CONTEXTUAL_PICK_REASON_KINDS = frozenset(
    {
        "role",
        "urgency",
        "synergy",
        "redundancy",
        "unsupported_payoff",
        "fixing",
    }
)


def _hob_relationship_fixtures() -> tuple[CardDatabase, SetProfile, DraftState]:
    """Load the tracked HOB relationship calibration fixtures once."""
    _artifact_path, _artifact_digest, card_database = _HOB_SMOKE._load_card_database()
    profile = load_set_profile(
        _HOB_PROFILE_FIXTURE_PATH,
        expected_set_code="hob",
        expected_format="quickdraft",
    )
    state = DraftState.from_json(
        json.loads(_HOB_STATE_FIXTURE_PATH.read_text(encoding="utf-8"))
    )
    return card_database, profile, state


def _hob_project_row(row) -> dict[str, object]:
    """Project one backtest row into the compared control shape."""
    recommended = row.recommended
    assert recommended is not None
    return {
        "recommended_grp_id": recommended.card.grp_id,
        "raw_score": recommended.raw_score,
        "score": recommended.score,
        "contextual_breakdown": recommended.contextual_breakdown.to_json(),
        "contextual_evidence": list(row.contextual_evidence),
        "rationale_reasons": [
            reason.to_json() for reason in recommended.rationale.reasons
        ],
        "relationship_support": [
            support.to_json() for support in row.role_ledger.relationship_support
        ],
    }


def _hob_report_projections(report) -> list[dict[str, object]]:
    return [_hob_project_row(row) for row in report.rows]


def test_hob_relationship_scoring_controls_are_deterministic_and_profile_isolated() -> None:
    database, profile, state = _hob_relationship_fixtures()
    removed_profile = replace(profile, enhancement=None)

    enhanced_first = generate_backtest_report(
        state=state, card_database=database, set_profile=profile
    )
    enhanced_second = generate_backtest_report(
        state=state, card_database=database, set_profile=profile
    )
    removed_first = generate_backtest_report(
        state=state, card_database=database, set_profile=removed_profile
    )
    removed_second = generate_backtest_report(
        state=state, card_database=database, set_profile=removed_profile
    )

    assert _hob_report_projections(enhanced_first) == _hob_report_projections(
        enhanced_second
    )
    assert _hob_report_projections(removed_first) == _hob_report_projections(
        removed_second
    )
    assert profile.enhancement is not None
    assert removed_profile.enhancement is None
    expected_removed_json = profile.to_json()
    del expected_removed_json["enhancement"]
    expected_removed_json["enhancement_status"] = "not-enhanced"
    assert removed_profile.to_json() == expected_removed_json

    for index, row in enumerate(enhanced_first.rows):
        pick = state.picks[index]
        assert (row.pack_number, row.pick_number) == (pick.pack_number, pick.pick_number)
        assert pick.offered_grp_ids == _HOB_EXPECTED_OFFERS[index]
        assert pick.pool_before_pick == _HOB_EXPECTED_POOLS[index]


def test_hob_relationship_scoring_exact_reviewed_deltas_and_evidence() -> None:
    database, profile, state = _hob_relationship_fixtures()
    enhanced = generate_backtest_report(
        state=state, card_database=database, set_profile=profile
    )
    removed = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=replace(profile, enhancement=None),
    )

    r1_enhanced, r1_removed = enhanced.rows[1].recommended, removed.rows[1].recommended
    assert r1_enhanced.card.grp_id == 103526
    assert r1_removed.card.grp_id == 103526
    assert round(r1_enhanced.contextual_breakdown.synergy, 6) - round(
        r1_removed.contextual_breakdown.synergy, 6
    ) == pytest.approx(_HOB_R1_RAW_SCORE_DELTA, abs=1e-9)
    assert round(r1_enhanced.raw_score, 6) - round(r1_removed.raw_score, 6) == pytest.approx(
        _HOB_R1_RAW_SCORE_DELTA, abs=1e-9
    )
    r1_supports = enhanced.rows[1].role_ledger.relationship_support
    assert [support.finding_id for support in r1_supports] == [_HOB_R1_FINDING_ID]
    assert r1_supports[0].mechanism == _HOB_R1_MECHANISM
    assert r1_supports[0].source_card_id == 103382
    assert r1_supports[0].target_card_id == 103526
    r1_evidence = "\n".join(enhanced.rows[1].contextual_evidence)
    assert _HOB_R1_FINDING_ID in r1_evidence
    assert _HOB_R1_MECHANISM in r1_evidence
    assert "Fíli the Pathfinder [103382]" in r1_evidence

    r6_enhanced, r6_removed = enhanced.rows[2].recommended, removed.rows[2].recommended
    assert r6_enhanced.card.grp_id == 103458
    assert r6_removed.card.grp_id == 103458
    assert round(r6_enhanced.contextual_breakdown.synergy, 6) - round(
        r6_removed.contextual_breakdown.synergy, 6
    ) == pytest.approx(_HOB_R6_RAW_SCORE_DELTA, abs=1e-9)
    assert round(r6_enhanced.raw_score, 6) - round(r6_removed.raw_score, 6) == pytest.approx(
        _HOB_R6_RAW_SCORE_DELTA, abs=1e-9
    )
    r6_supports = enhanced.rows[2].role_ledger.relationship_support
    assert [support.finding_id for support in r6_supports] == [_HOB_R6_FINDING_ID]
    assert r6_supports[0].mechanism == _HOB_R6_MECHANISM
    assert r6_supports[0].source_card_id == 103531
    assert r6_supports[0].target_card_id == 103458
    r6_evidence = "\n".join(enhanced.rows[2].contextual_evidence)
    assert _HOB_R6_FINDING_ID in r6_evidence
    assert _HOB_R6_MECHANISM in r6_evidence
    assert "Chief Warg's Company [103531]" in r6_evidence


def test_hob_relationship_scoring_unsupported_and_saturated_rows() -> None:
    database, profile, state = _hob_relationship_fixtures()
    enhanced = generate_backtest_report(
        state=state, card_database=database, set_profile=profile
    )
    removed = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=replace(profile, enhancement=None),
    )

    unsupported_enhanced = enhanced.rows[0].recommended
    unsupported_removed = removed.rows[0].recommended
    assert unsupported_enhanced.card.grp_id == 103526
    assert unsupported_removed.card.grp_id == 103526
    assert enhanced.rows[0].role_ledger.relationship_support == ()
    assert unsupported_enhanced.contextual_breakdown.synergy == 0.0
    assert unsupported_removed.contextual_breakdown.synergy == 0.0
    assert (
        unsupported_enhanced.raw_score - unsupported_removed.raw_score == 0.0
    )

    saturated_enhanced = enhanced.rows[3].recommended
    saturated_removed = removed.rows[3].recommended
    assert saturated_enhanced.card.grp_id == 103526
    assert saturated_removed.card.grp_id == 103526
    assert saturated_enhanced.contextual_breakdown.synergy == MAX_SYNERGY_TERM
    assert saturated_removed.contextual_breakdown.synergy == MAX_SYNERGY_TERM
    assert (
        saturated_enhanced.raw_score - saturated_removed.raw_score == 0.0
    )
    for row in enhanced.rows:
        assert row.recommended is not None
        assert abs(row.recommended.contextual_breakdown.aggregate) <= (
            MAX_CONTEXTUAL_ADJUSTMENT
        )
    saturation_supports = enhanced.rows[3].role_ledger.relationship_support
    assert len(saturation_supports) == 1
    assert saturation_supports[0].finding_id == _HOB_R1_FINDING_ID
    assert saturation_supports[0].target_card_id == 103526
    assert saturation_supports[0].target_card_id == saturated_enhanced.card.grp_id
    saturated_reasons = saturated_enhanced.rationale.reasons
    assert not any(
        _HOB_R1_FINDING_ID in (reason.evidence or "") for reason in saturated_reasons
    )
    synergy_evidence = [
        reason.evidence or "" for reason in saturated_reasons if reason.kind == "synergy"
    ]
    assert len(synergy_evidence) == 1
    assert "semantic package" in synergy_evidence[0]


def test_hob_relationship_scoring_context_disabled_controls() -> None:
    database, profile, state = _hob_relationship_fixtures()
    enhanced = generate_backtest_report(
        state=state, card_database=database, set_profile=profile
    )
    disabled = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=profile,
        contextual_adjustments_enabled=False,
    )

    for index, row in enumerate(disabled.rows):
        recommended = row.recommended
        assert recommended is not None
        assert recommended.card.grp_id == _HOB_EXPECTED_RECOMMENDATIONS[index]
        assert recommended.contextual_breakdown == ContextualScoreBreakdown()
        assert recommended.contextual_breakdown.aggregate == 0.0
        assert row.contextual_evidence == ()
        disabled_kinds = {reason.kind for reason in recommended.rationale.reasons}
        assert disabled_kinds.isdisjoint(_CONTEXTUAL_PICK_REASON_KINDS)
        assert recommended.rating.metadata.source == "profile"
    assert disabled.rows[1].role_ledger.relationship_support
    assert enhanced.rows[1].contextual_evidence


def test_hob_relationship_scoring_gate_empties_ledger_support_and_terms() -> None:
    database, profile, state = _hob_relationship_fixtures()
    enhanced = generate_backtest_report(
        state=state, card_database=database, set_profile=profile
    )
    gated = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=profile,
        enhanced_relationships_enabled=False,
    )

    assert all(
        row.role_ledger.relationship_support == () for row in gated.rows
    )
    assert enhanced.rows[1].role_ledger.relationship_support
    for index in (1, 2):
        enhanced_recommended = enhanced.rows[index].recommended
        gated_recommended = gated.rows[index].recommended
        assert enhanced_recommended is not None
        assert gated_recommended is not None
        assert (
            gated_recommended.contextual_breakdown.synergy
            < enhanced_recommended.contextual_breakdown.synergy
        )
    assert gated.rows[1].contextual_evidence
    context_disabled = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=profile,
        contextual_adjustments_enabled=False,
    )
    assert context_disabled.rows[1].role_ledger.relationship_support


def test_hob_relationship_scoring_gate_overrides_a_supplied_default_engine() -> None:
    database, profile, state = _hob_relationship_fixtures()
    supplied_engine = PickEngine(set_profile=profile)
    enhanced = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=profile,
    )
    gated = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=profile,
        pick_engine=supplied_engine,
        enhanced_relationships_enabled=False,
    )
    constructed_gated = generate_backtest_report(
        state=state,
        card_database=database,
        set_profile=profile,
        enhanced_relationships_enabled=False,
    )

    assert supplied_engine.enhanced_relationships_enabled is True
    assert supplied_engine.set_profile is profile
    assert all(row.role_ledger.relationship_support == () for row in gated.rows)
    assert enhanced.rows[1].role_ledger.relationship_support
    assert _hob_report_projections(gated) == _hob_report_projections(constructed_gated)
    for index in (1, 2):
        gated_recommended = gated.rows[index].recommended
        enhanced_recommended = enhanced.rows[index].recommended
        assert gated_recommended is not None
        assert enhanced_recommended is not None
        assert (
            gated_recommended.contextual_breakdown.synergy
            < enhanced_recommended.contextual_breakdown.synergy
        )
    assert _HOB_R1_FINDING_ID not in "\n".join(gated.rows[1].contextual_evidence)
    assert supplied_engine.enhanced_relationships_enabled is True


def test_hob_relationship_scoring_persists_state_bytes() -> None:
    database, profile, state = _hob_relationship_fixtures()
    with tempfile.TemporaryDirectory() as temp_dir:
        app_dir = Path(temp_dir)
        persisted_path = save_draft_state(state=state, app_dir=app_dir)
        persisted_bytes = persisted_path.read_bytes()

        generate_backtest_report(state=state, card_database=database, set_profile=profile)
        generate_backtest_report(
            state=state,
            card_database=database,
            set_profile=replace(profile, enhancement=None),
        )
        generate_backtest_report(
            state=state,
            card_database=database,
            set_profile=profile,
            contextual_adjustments_enabled=False,
        )

        assert persisted_path.read_bytes() == persisted_bytes
        reloaded = load_persisted_backtest_state(
            app_dir=app_dir,
            account_id=state.account_id,
            draft_id=state.draft_id,
        )
        assert reloaded.to_json() == state.to_json()


def test_hob_relationship_scoring_smoke_main_reports_contract(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _HOB_SMOKE
    assert module.main() == 0
    report = json.loads(capsys.readouterr().out)

    assert report["profile"]["version"] == "hob-relationship-scoring-1"
    assert report["profile"]["enhancement_status"] == "enhanced"
    assert report["profile"]["set_code"] == "hob"
    assert report["card_artifact"]["sha256"] == _HOB_CARD_ARTIFACT_SHA256
    assert report["caps"] == {
        "max_synergy_term": 1.5,
        "max_contextual_adjustment": 6.0,
    }

    factors = report["relationship_support_factors"]
    assert len(factors) == 9
    assert [factor["mechanism"] for factor in factors] == sorted(
        factor["mechanism"] for factor in factors
    )
    assert all(factor["factor"] == 0.5 for factor in factors)
    observed = {
        factor["mechanism"]
        for factor in factors
        if factor["evidence"] == "hob-observed"
    }
    assert observed == {_HOB_R1_MECHANISM, _HOB_R6_MECHANISM}

    assert [row["row"] for row in report["rows"]] == [
        "unsupported",
        "r1",
        "r6",
        "saturation",
    ]
    for index, row in enumerate(report["rows"]):
        assert row["offered_grp_ids"] == list(_HOB_EXPECTED_OFFERS[index])
        assert row["pool_before_pick"] == list(_HOB_EXPECTED_POOLS[index])
        assert set(row["controls"]) == {
            "enhanced",
            "enhancement_removed",
            "context_disabled",
        }
    deltas = {row["row"]: row["deltas"]["raw_score"] for row in report["rows"]}
    assert deltas["r1"] == pytest.approx(_HOB_R1_RAW_SCORE_DELTA, abs=1e-9)
    assert deltas["r6"] == pytest.approx(_HOB_R6_RAW_SCORE_DELTA, abs=1e-9)
    assert deltas["unsupported"] == 0.0
    assert deltas["saturation"] == 0.0

    support_ids = {
        support["finding_id"]
        for row in report["rows"]
        for support in row["relationship_support"]
    }
    assert {_HOB_R1_FINDING_ID, _HOB_R6_FINDING_ID} <= support_ids

    projection_notes = report["relationship_projection_notes"]
    assert isinstance(projection_notes, list)
    assert [entry["finding_id"] for entry in projection_notes] == [_HOB_R1_FINDING_ID]
    projection_note = projection_notes[0]["note"]
    assert isinstance(projection_note, str)
    assert projection_note.strip()

    report_text = json.dumps(report)
    for forbidden_key in (
        "prompt",
        "response",
        "claim",
        "guide",
        "oracle_text",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "provider",
        "model",
    ):
        assert f'"{forbidden_key}"' not in report_text
    for oracle_quote in (
        "Whenever Fíli or another nontoken Dwarf you control enters",
        "Other creatures you control get +1/+1",
        "At the beginning of your upkeep, create a 2/2 green Wolf creature token",
        "you may sacrifice another creature",
    ):
        assert oracle_quote not in report_text

    monkeypatch.setattr(module, "CARD_ARTIFACT_SHA256", "0" * 64)
    assert module.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("HOB relationship scoring smoke failed: ")


def test_hob_relationship_scoring_smoke_rejects_structurally_invalid_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed_state_path = tmp_path / "malformed-hob-state.json"
    malformed_state_path.write_text(
        json.dumps({"schema_version": 1, "picks": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(_HOB_SMOKE, "STATE_RELATIVE_PATH", malformed_state_path)

    assert _HOB_SMOKE.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("HOB relationship scoring smoke failed: ")

