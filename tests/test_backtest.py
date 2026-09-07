from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import draftomen.cli as cli_module
from draftomen.backtest import format_backtest_report, generate_backtest_report
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.cli import main
from draftomen.pickengine import PickEngine
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

