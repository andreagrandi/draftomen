from __future__ import annotations

import csv
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import draftomen.benchmark as benchmark_module
import draftomen.cli as cli_module
from draftomen.benchmark import (
    _database_with_rating_metadata,
    build_pick_benchmark_report_from_rows,
    format_pick_benchmark_report,
)
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.cli import main
from draftomen.events import EXPECTED_PICKS_PER_PACK, EXPECTED_TOTAL_PICKS
from draftomen.pickengine import PickEngine, ScoredPack
from draftomen.set_profile import (
    SetProfile,
    dump_set_profile,
    load_set_profile,
    set_profile_path,
)
from draftomen.seventeen import (
    PREMIER_DRAFT_FORMAT,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsData,
    SeventeenLandsFormatData,
    save_17lands_format_data,
)

PROFILE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "set-profiles"


def test_pick_benchmark_compares_wr_and_do_score_by_phase() -> None:
    report = build_pick_benchmark_report_from_rows(
        set_code="TST",
        event_format=PREMIER_DRAFT_FORMAT,
        rows=_benchmark_rows(),
        card_database=_benchmark_card_database(),
        ratings_data=_benchmark_ratings_data(),
    )
    output = format_pick_benchmark_report(report)

    win_rate_summary = report.ranking_summaries[0]
    score_summary = report.ranking_summaries[1]

    assert report.draft_count == 1
    assert len(report.picks) == 3
    assert win_rate_summary.ranking_mode == "win_rate"
    assert win_rate_summary.top_1_count == 1
    assert win_rate_summary.top_3_count == 3
    assert win_rate_summary.average_actual_pick_rank == pytest.approx(5 / 3)
    assert score_summary.ranking_mode == "score"
    assert score_summary.top_1_count == 2
    assert score_summary.top_3_count == 3
    assert score_summary.average_actual_pick_rank == pytest.approx(4 / 3)
    assert report.comparison.better_count == 1
    assert report.comparison.same_count == 2
    assert report.comparison.worse_count == 0
    assert "Ranking comparison:" in output
    assert "17L WR" in output
    assert "DO Score" in output
    assert "Top-1" in output
    assert "Top-3" in output
    assert "Top-5" in output
    assert "Avg actual-pick rank" in output
    assert "Phase breakdown:" in output
    assert "open" in output
    assert "locked" in output
    assert "DO vs 17L actual-pick rank: better 1 (33.3%)" in output
    assert "Default ranking decision: DO Score is the default" in output
    assert "Non-ML heuristic candidate from misses" in output


def test_pick_benchmark_propagates_profile_and_complete_stage_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _benchmark_profile()
    constructor_profiles: list[object] = []
    score_calls: list[dict[str, object]] = []

    class SpyPickEngine(PickEngine):
        def __init__(self, **kwargs: object) -> None:
            constructor_profiles.append(kwargs.get("set_profile"))
            super().__init__(**kwargs)

        def score_pack(self, **kwargs: object) -> ScoredPack:
            score_calls.append(
                {
                    key: kwargs[key]
                    for key in (
                        "pool_grp_ids",
                        "pack_number",
                        "pick_number",
                        "pick_index",
                        "global_pick_index",
                        "estimated_remaining_picks",
                    )
                }
            )
            return super().score_pack(**kwargs)

    monkeypatch.setattr(benchmark_module, "PickEngine", SpyPickEngine)
    report = build_pick_benchmark_report_from_rows(
        set_code="TST",
        event_format=PREMIER_DRAFT_FORMAT,
        rows=_benchmark_rows(),
        card_database=_benchmark_card_database(),
        ratings_data=_benchmark_ratings_data(),
        set_profile=profile,
    )

    assert constructor_profiles == [profile]
    assert score_calls == [
        {
            "pool_grp_ids": (),
            "pack_number": 0,
            "pick_number": 0,
            "pick_index": 1,
            "global_pick_index": 1,
            "estimated_remaining_picks": EXPECTED_TOTAL_PICKS - 1,
        },
        {
            "pool_grp_ids": (1,),
            "pack_number": 0,
            "pick_number": 1,
            "pick_index": 2,
            "global_pick_index": 2,
            "estimated_remaining_picks": EXPECTED_TOTAL_PICKS - 2,
        },
        {
            "pool_grp_ids": (1, 2),
            "pack_number": 1,
            "pick_number": 2,
            "pick_index": EXPECTED_PICKS_PER_PACK + 3,
            "global_pick_index": EXPECTED_PICKS_PER_PACK + 3,
            "estimated_remaining_picks": (
                EXPECTED_TOTAL_PICKS - EXPECTED_PICKS_PER_PACK - 3
            ),
        },
    ]
    assert all(
        rank.actual_card.contextual_profile_maturity == profile.maturity.value
        for pick in report.picks
        for rank in pick.rankings
    )


def test_pick_benchmark_without_profile_keeps_generic_scoring() -> None:
    report = build_pick_benchmark_report_from_rows(
        set_code="TST",
        event_format=PREMIER_DRAFT_FORMAT,
        rows=_benchmark_rows(),
        card_database=_benchmark_card_database(),
        ratings_data=_benchmark_ratings_data(),
        set_profile=None,
    )

    assert all(
        rank.actual_card.contextual_profile_maturity is None
        and rank.actual_card.contextual_profile_confidence is None
        and rank.actual_card.contextual_evidence == ()
        for pick in report.picks
        for rank in pick.rankings
    )


def test_benchmark_metadata_augmentation_preserves_card_data_update_time() -> None:
    timestamp = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    augmented = _database_with_rating_metadata(
        card_database=CardDatabase(cards={}, generated_at=timestamp),
        ratings_data=_benchmark_ratings_data(),
    )

    assert augmented.generated_at == timestamp


def test_rating_metadata_fallback_keeps_colors_eligible_for_scoring() -> None:
    database = _database_with_rating_metadata(
        card_database=CardDatabase(cards={}),
        ratings_data=_benchmark_ratings_data(),
    )
    fallback = database.lookup(grp_id=1)

    assert fallback.unknown is False
    assert fallback.colors == ("W",)
    assert fallback.types == ("Unknown",)
    assert fallback.arena_id == 1

    scored = PickEngine(ratings_data=_benchmark_ratings_data()).score_pack(
        offered_grp_ids=(3,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=5,
    )
    weights = dict(scored.commitment.color_weights)
    assert weights["W"] > 0
    assert weights["U"] > 0


def test_benchmark_picks_cli_reads_local_public_draft_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_dir, bulk_file, draft_data_file = _prepare_benchmark_cli_fixture(
        directory=tmp_path,
    )
    exit_code = main(
        argv=[
            "benchmark-picks",
            "--set-code",
            "TST",
            "--format",
            PREMIER_DRAFT_FORMAT,
            "--draft-data-file",
            str(draft_data_file),
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(app_dir),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Draft Omen trophy pick benchmark" in captured.out
    assert "Set: TST" in captured.out
    assert "Format: PremierDraft" in captured.out
    assert "Rows: 3 compared, 0 skipped" in captured.out
    assert "DO vs 17L actual-pick rank" in captured.out
    assert "benchmark-picks: loading 17Lands ratings" in captured.err
    assert "benchmark-picks: scoring public draft rows" in captured.err
    assert "benchmark-picks: done" in captured.err


def test_benchmark_picks_cli_passes_valid_local_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir, bulk_file, draft_data_file = _prepare_benchmark_cli_fixture(
        directory=tmp_path,
    )
    profile = _benchmark_profile()
    dump_set_profile(
        profile,
        set_profile_path(
            set_code="TST",
            event_format=PREMIER_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )

    observed: dict[str, object] = {}
    real_generate = cli_module.generate_pick_benchmark_report

    def spy_generate(**kwargs: object):
        observed["set_profile"] = kwargs["set_profile"]
        return real_generate(**kwargs)

    monkeypatch.setattr(cli_module, "generate_pick_benchmark_report", spy_generate)
    assert (
        main(
            argv=[
                "benchmark-picks",
                "--set-code",
                "TST",
                "--format",
                PREMIER_DRAFT_FORMAT,
                "--draft-data-file",
                str(draft_data_file),
                "--bulk-file",
                str(bulk_file),
                "--app-dir",
                str(app_dir),
            ]
        )
        == 0
    )
    assert observed["set_profile"] == profile


def _prepare_benchmark_cli_fixture(
    *,
    directory: Path,
) -> tuple[Path, Path, Path]:
    app_dir = directory / "app"
    bulk_file = _write_benchmark_bulk_file(directory=directory)
    draft_data_file = _write_benchmark_draft_data_file(directory=directory)
    save_17lands_format_data(dataset=_benchmark_format_data(), app_dir=app_dir)
    return app_dir, bulk_file, draft_data_file


def _benchmark_profile() -> SetProfile:
    profile = load_set_profile(PROFILE_FIXTURE_DIR / "mature.json")
    return replace(profile, event_format=PREMIER_DRAFT_FORMAT)


def _benchmark_rows() -> tuple[dict[str, str], ...]:
    return (
        _draft_row(
            pick_number="1",
            pick="White Bomb",
            offered=("White Bomb", "Red Temptation", "Blue Good"),
        ),
        _draft_row(
            pick_number="2",
            pick="Blue Good",
            offered=("Blue Good", "Red Temptation", "White Roleplayer"),
        ),
        _draft_row(
            pack_number="1",
            pick_number="3",
            pick="White Roleplayer",
            offered=("White Roleplayer", "Red Temptation", "Green Filler"),
        ),
    )


def _draft_row(
    *,
    pick_number: str,
    pick: str,
    offered: tuple[str, ...],
    pack_number: str = "0",
) -> dict[str, str]:
    row = {
        "expansion": "TST",
        "event_type": PREMIER_DRAFT_FORMAT,
        "draft_id": "draft-a",
        "event_match_wins": "7",
        "pack_number": pack_number,
        "pick_number": pick_number,
        "pick": pick,
        "pick_2": "",
        "pick_maindeck_rate": "1.0",
    }
    for name in offered:
        row[f"pack_card_{name}"] = "1"

    return row


def _benchmark_card_database() -> CardDatabase:
    return CardDatabase(
        cards={
            1: _card(grp_id=1, name="White Bomb", colors=("W",)),
            2: _card(grp_id=2, name="Blue Good", colors=("U",)),
            3: replace(
                _card(grp_id=3, name="Red Temptation", colors=("R",)),
                mana_cost="{R}{R}{R}",
            ),
            4: _card(grp_id=4, name="White Roleplayer", colors=("W",)),
            5: _card(grp_id=5, name="Green Filler", colors=("G",)),
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


def _benchmark_ratings_data() -> SeventeenLandsData:
    return SeventeenLandsData(
        set_code="TST",
        requested_format=PREMIER_DRAFT_FORMAT,
        primary=_benchmark_format_data(),
        fallback=None,
        thin_sample_minimum=500,
    )


def _benchmark_format_data() -> SeventeenLandsFormatData:
    return SeventeenLandsFormatData(
        set_code="TST",
        event_format=PREMIER_DRAFT_FORMAT,
        fetched_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        card_ratings={
            1: _stats(grp_id=1, name="White Bomb", color="W", gih=0.62),
            2: _stats(grp_id=2, name="Blue Good", color="U", gih=0.58),
            3: _stats(grp_id=3, name="Red Temptation", color="R", gih=0.60),
            4: _stats(grp_id=4, name="White Roleplayer", color="W", gih=0.57),
            5: _stats(grp_id=5, name="Green Filler", color="G", gih=0.50),
        },
        pair_win_rates={},
    )


def _stats(*, grp_id: int, name: str, color: str, gih: float) -> SeventeenCardStats:
    return SeventeenCardStats(
        grp_id=grp_id,
        name=name,
        color=color,
        rarity="common",
        average_last_seen_at=3.0,
        gih_win_rate=gih,
        opening_hand_win_rate=None,
        drawn_improvement_win_rate=None,
        sample_counts=RatingSampleCounts(
            seen=1000,
            picked=500,
            games_played=900,
            opening_hand=200,
            games_in_hand=900,
        ),
    )


def _write_benchmark_bulk_file(*, directory: Path) -> Path:
    path = directory / "benchmark-bulk.jsonl"
    rows = [
        _scryfall_row(grp_id=1, name="White Bomb", colors=["W"]),
        _scryfall_row(grp_id=2, name="Blue Good", colors=["U"]),
        _scryfall_row(grp_id=3, name="Red Temptation", colors=["R"]),
        _scryfall_row(grp_id=4, name="White Roleplayer", colors=["W"]),
        _scryfall_row(grp_id=5, name="Green Filler", colors=["G"]),
    ]
    path.write_text(
        "".join(f"{json.dumps(row)}\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _write_benchmark_draft_data_file(*, directory: Path) -> Path:
    path = directory / "draft-data.csv"
    fieldnames = [
        "expansion",
        "event_type",
        "draft_id",
        "event_match_wins",
        "pack_number",
        "pick_number",
        "pick",
        "pick_2",
        "pick_maindeck_rate",
        "pack_card_White Bomb",
        "pack_card_Blue Good",
        "pack_card_Red Temptation",
        "pack_card_White Roleplayer",
        "pack_card_Green Filler",
    ]
    with path.open(mode="w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in _benchmark_rows():
            writer.writerow(row)

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


