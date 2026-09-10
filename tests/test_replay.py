from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import draftomen.cli as cli_module
from draftomen.carddb import (
    CardDatabase,
    CardInfo,
    build_card_database_from_bulk_file,
    card_database_cache_path,
    refresh_card_database,
)
from draftomen.cli import main
from draftomen.events import (
    EXPECTED_PICKS_PER_PACK,
    EXPECTED_TOTAL_PICKS,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
)
from draftomen.pickengine import (
    PickEngine,
    ScoredPack,
    render_pick_rationale_detailed,
)
from draftomen.replay import (
    format_pack_offered_event,
    render_replay_events,
    replay_log_file,
)
from draftomen.set_profile import SetProfile, dump_set_profile, set_profile_path
from draftomen.seventeen import (
    PREMIER_DRAFT_FORMAT,
    QUICK_DRAFT_FORMAT,
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsData,
    SeventeenLandsFormatData,
    save_17lands_format_data,
)

FIXTURE_LOG_PATH = Path(__file__).parent / "fixtures" / "quick-draft-msh-player.log"
SCRYFALL_BULK_SAMPLE_PATH = (
    Path(__file__).parent / "fixtures" / "scryfall-default-cards-sample.jsonl"
)
GOLDEN_REPLAY_PATH = (
    Path(__file__).parent / "golden" / "quick-draft-msh-player.replay.txt"
)


def test_replay_fixture_matches_committed_golden_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        argv=[
            "replay",
            str(FIXTURE_LOG_PATH),
            "--bulk-file",
            str(SCRYFALL_BULK_SAMPLE_PATH),
            "--app-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == GOLDEN_REPLAY_PATH.read_text(encoding="utf-8")
    assert captured.err == ""


def test_replay_output_is_byte_identical_across_runs() -> None:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)

    first_output = replay_log_file(logfile=FIXTURE_LOG_PATH, card_database=database)
    second_output = replay_log_file(logfile=FIXTURE_LOG_PATH, card_database=database)

    assert first_output == second_output
    assert first_output == GOLDEN_REPLAY_PATH.read_text(encoding="utf-8")


def test_replay_with_ratings_data_shows_fallback_sources() -> None:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)

    output = replay_log_file(
        logfile=FIXTURE_LOG_PATH,
        card_database=database,
        ratings_data=_fixture_ratings_data(),
    )

    assert "Data source: QuickDraft + Premier fallback + neutral prior" in output
    assert "Fixture Split Card (grpId 104894)   WU         Open    62.0%" in output
    assert "Fixture Blue Card (grpId 105134)    U          Open    58.0%" in output
    assert "Premier" in output


def test_replay_warns_when_card_metadata_is_incomplete(tmp_path: Path) -> None:
    partial_log = tmp_path / "partial-player.log"
    partial_log.write_text(
        "\n".join(FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()[:7]),
        encoding="utf-8",
    )

    output = replay_log_file(logfile=partial_log, card_database=CardDatabase(cards={}))

    assert "Warning: 14 unresolved card metadata" in output


def test_replay_uses_cached_card_database_without_refreshing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refresh_card_database(app_dir=tmp_path, bulk_file=SCRYFALL_BULK_SAMPLE_PATH)

    exit_code = main(
        argv=[
            "replay",
            str(FIXTURE_LOG_PATH),
            "--app-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == GOLDEN_REPLAY_PATH.read_text(encoding="utf-8")
    assert captured.err == ""


def test_replay_with_schema_five_incomplete_scryfall_card_stays_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refresh_card_database(app_dir=tmp_path, bulk_file=SCRYFALL_BULK_SAMPLE_PATH)
    cache_path = card_database_cache_path(app_dir=tmp_path)
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["schema_version"] == 5
    cache["cards"]["105097"]["mana_cost"] = None
    cache["cards"]["105097"]["source_provenance"] = ["scryfall"]
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    save_17lands_format_data(
        SeventeenLandsFormatData(
            set_code="MSH",
            event_format=QUICK_DRAFT_FORMAT,
            fetched_at=datetime(2999, 1, 1, tzinfo=UTC),
            card_ratings={
                105097: _stats(
                    grp_id=105097,
                    name="Fixture Spider",
                    color="G",
                    gih=0.60,
                    games_in_hand=900,
                    alsa=2.1,
                )
            },
            pair_win_rates=_pair_win_rates(),
        ),
        app_dir=tmp_path,
    )

    def fail_mtgjson(**_kwargs: object) -> None:
        pytest.fail("replay must not invoke MTGJSON metadata augmentation")

    monkeypatch.setattr("draftomen.carddb.download_mtgjson_set_cards", fail_mtgjson)
    monkeypatch.setattr("draftomen.seventeen.augment_card_database_from_ratings", fail_mtgjson)

    exit_code = main(
        argv=[
            "replay",
            str(FIXTURE_LOG_PATH),
            "--app-dir",
            str(tmp_path),
            "--no-splash",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Data source: QuickDraft" in captured.out
    assert "Fixture Spider (grpId 105097)" in captured.out
    assert "60.0%" in captured.out
    assert captured.err == ""


def test_replay_without_ratings_cache_uses_neutral_prior_scores(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refresh_card_database(app_dir=tmp_path, bulk_file=SCRYFALL_BULK_SAMPLE_PATH)

    exit_code = main(
        argv=[
            "replay",
            str(FIXTURE_LOG_PATH),
            "--app-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Data source: neutral prior" in captured.out
    assert "Score" in captured.out
    assert "Prior*" in captured.out
    assert "Recommendation: Fixture Split Card receives 50 DO points." in captured.out
    assert "Rating evidence: neutral-prior estimate with no GIH data." in captured.out
    assert captured.err == ""


def test_replay_basic_recommendation_keeps_legacy_explanation() -> None:
    basic_land = CardInfo(
        grp_id=9001,
        name="Plains",
        colors=("W",),
        mana_value=0.0,
        rarity="basic",
        types=("Basic Land — Plains",),
        mana_cost=None,
    )
    event = PackOfferedEvent(
        event_name="QuickDraft_TST_basic",
        set_code="TST",
        pack_number=0,
        pick_number=0,
        offered_grp_ids=(9001,),
        pool_grp_ids=(),
        account_id="BASICACCOUNT",
    )

    lines = format_pack_offered_event(
        event=event,
        card_database=CardDatabase(cards={9001: basic_land}),
    )

    assert lines[-1] == (
        "Recommendation: Plains is freely available during deck building, "
        "so it receives 0 DO points and ranks after draftable cards."
    )


def test_replay_without_card_cache_returns_actionable_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        argv=[
            "replay",
            str(FIXTURE_LOG_PATH),
            "--app-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Run refresh-data first" in captured.err


def test_replay_uses_pre_pick_context_for_recommendation_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)
    database = CardDatabase(
        cards={
            grp_id: replace(card, set_code="MSH")
            if grp_id in {105003, 105097, 105134}
            else card
            for grp_id, card in database.cards.items()
        }
    )
    profile = _replay_semantic_profile()
    calls: list[tuple[dict[str, object], ScoredPack]] = []
    score_pack = PickEngine.score_pack

    def record_score_pack(self: PickEngine, **kwargs: object) -> ScoredPack:
        scored_pack = score_pack(self, **kwargs)
        calls.append((kwargs, scored_pack))
        return scored_pack

    monkeypatch.setattr(PickEngine, "score_pack", record_score_pack)
    output = render_replay_events(
        events=_replay_context_events(),
        card_database=database,
        set_profile=profile,
        splash_enabled=False,
    )

    assert len(calls) == 1
    kwargs, scored_pack = calls[0]
    expected_global_pick_index = (
        1 * EXPECTED_PICKS_PER_PACK + 2 + 1
    )
    expected_remaining_picks = EXPECTED_TOTAL_PICKS - expected_global_pick_index
    assert kwargs["pool_grp_ids"] == (105097, 105134)
    assert kwargs["pack_number"] == 1
    assert kwargs["pick_number"] == 2
    assert kwargs["pick_index"] == expected_global_pick_index
    assert kwargs["global_pick_index"] == expected_global_pick_index
    assert kwargs["estimated_remaining_picks"] == expected_remaining_picks
    assert scored_pack.scoring_context is not None
    assert scored_pack.scoring_context.stage.pack_number == 1
    assert scored_pack.scoring_context.stage.pick_number == 2
    assert (
        scored_pack.scoring_context.stage.global_pick_index
        == expected_global_pick_index
    )
    assert (
        scored_pack.scoring_context.stage.estimated_remaining_picks
        == expected_remaining_picks
    )
    assert scored_pack.scoring_context.role_ledger.profile_source == (
        "profile:early"
    )
    assert scored_pack.scoring_context.set_profile is profile
    recommended_card = scored_pack.cards[0]
    assert recommended_card.contextual_profile_maturity == "early"
    assert recommended_card.contextual_profile_confidence == pytest.approx(0.8)
    assert recommended_card.contextual_breakdown.role > 0
    assert any(
        "fills draw deficit" in evidence
        for evidence in recommended_card.contextual_evidence
    )
    assert "Recommendation: " in output
    assert (
        "Recommendation: "
        + render_pick_rationale_detailed(scored_card=recommended_card)
        in output
    )
    assert "splash disabled" in output
    assert "Helps fill your deck's draw gap" in output


def test_replay_cli_loads_local_profile_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_dir = tmp_path / "app"
    dump_set_profile(
        _replay_semantic_profile(),
        set_profile_path(
            set_code="MSH",
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )

    calls: list[tuple[str, str, Path]] = []
    real_load = cli_module.load_scoring_profile

    def record_load(
        set_code: str,
        event_format: str,
        *,
        app_dir: Path | None = None,
        **kwargs: object,
    ) -> SetProfile | None:
        assert app_dir is not None
        calls.append((set_code, event_format, app_dir))
        return real_load(
            set_code=set_code,
            event_format=event_format,
            app_dir=app_dir,
            **kwargs,
        )

    monkeypatch.setattr(cli_module, "load_scoring_profile", record_load)
    exit_code = main(
        argv=[
            "replay",
            str(FIXTURE_LOG_PATH),
            "--bulk-file",
            str(SCRYFALL_BULK_SAMPLE_PATH),
            "--app-dir",
            str(app_dir),
            "--no-splash",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == [("MSH", QUICK_DRAFT_FORMAT, app_dir)]
    assert "Recommendation: " in captured.out
    assert "splash disabled" in captured.out
    assert captured.err == ""


def test_replay_explains_profile_context_without_material_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)
    database = CardDatabase(
        cards={
            grp_id: replace(card, set_code="MSH")
            if grp_id in {105003, 105097, 105134}
            else card
            for grp_id, card in database.cards.items()
        }
    )
    profile = replace(_replay_semantic_profile(), role_profile=None)
    calls: list[ScoredPack] = []
    score_pack = PickEngine.score_pack

    def record_score_pack(self: PickEngine, **kwargs: object) -> ScoredPack:
        scored_pack = score_pack(self, **kwargs)
        calls.append(scored_pack)
        return scored_pack

    monkeypatch.setattr(PickEngine, "score_pack", record_score_pack)
    output = render_replay_events(
        events=_replay_context_events(),
        card_database=database,
        set_profile=profile,
        splash_enabled=False,
    )

    assert len(calls) == 1
    recommended_card = calls[0].cards[0]
    assert recommended_card.contextual_profile_confidence == pytest.approx(0.8)
    assert recommended_card.contextual_evidence == ()
    assert recommended_card.contextual_breakdown.aggregate == 0.0
    assert "Recommendation: " in output
    assert (
        "Recommendation: "
        + render_pick_rationale_detailed(scored_card=recommended_card)
        in output
    )
    assert "context UG" not in output
    assert "theme replay tempo" not in output
    assert "Helps fill" not in output
    assert "Filling a missing role matters more" not in output
    assert "Works with support already" not in output
    assert "Overlaps with roles" not in output
    assert "Needs support" not in output
    assert "produce the colors" not in output


def test_replay_profile_loader_is_skipped_for_explicit_profile() -> None:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)
    calls: list[str] = []
    profile = _replay_semantic_profile()

    def fail_loader(set_code: str) -> SetProfile:
        calls.append(set_code)
        raise AssertionError("explicit profile must remain authoritative")

    render_replay_events(
        events=_replay_context_events(),
        card_database=database,
        set_profile=profile,
        profile_loader=fail_loader,
        splash_enabled=False,
    )

    assert calls == []


def _replay_context_events() -> tuple[
    DraftStartedEvent | PickMadeEvent | PackOfferedEvent, ...
]:
    event_name = "QuickDraft_MSH_replay-context"
    account_id = "REPLAYACCOUNT"
    return (
        DraftStartedEvent(
            event_name=event_name,
            set_code="MSH",
            course_id="REPLAYDRAFT",
            account_id=account_id,
        ),
        PickMadeEvent(
            event_name=event_name,
            set_code="MSH",
            pack_number=0,
            pick_number=0,
            chosen_grp_id=105097,
            account_id=account_id,
        ),
        PickMadeEvent(
            event_name=event_name,
            set_code="MSH",
            pack_number=0,
            pick_number=1,
            chosen_grp_id=105134,
            account_id=account_id,
        ),
        PackOfferedEvent(
            event_name=event_name,
            set_code="MSH",
            pack_number=1,
            pick_number=2,
            offered_grp_ids=(105003,),
            pool_grp_ids=(105097, 105134),
            account_id=account_id,
        ),
    )


def _replay_semantic_profile() -> SetProfile:
    return SetProfile.from_json(
        {
            "schema_version": 1,
            "set_code": "MSH",
            "format": "quickdraft",
            "profile_version": "replay-test",
            "generated_at": "2026-08-29T00:00:00+00:00",
            "source": {"provider": "replay-test"},
            "maturity": "early",
            "confidence": 0.8,
            "pair_profiles": [
                {
                    "pair": "UG",
                    "theme": "replay tempo",
                    "role_targets": [
                        {"role": "draw", "value": 3},
                    ],
                }
            ],
            "role_profile": {
                "schema_version": 2,
                "set_code": "MSH",
                "classifier_version": "1.1",
                "role_schema_version": 2,
                "profile_schema_version": 2,
                "cards": [
                    {
                        "key": "arena_id:105003",
                        "card_name": "Fixture Card 105003",
                        "roles": [
                            {
                                "role": "draw",
                                "confidence": 1.0,
                                "provenance": ["replay-test"],
                                "evidence": ["replay test role"],
                            }
                        ],
                    }
                ],
            },
        }
    )


def _fixture_ratings_data() -> SeventeenLandsData:
    primary = SeventeenLandsFormatData(
        set_code="MSH",
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
                games_in_hand=850,
                alsa=2.1,
            ),
            105134: _stats(
                grp_id=105134,
                name="Fixture Blue Card",
                color="U",
                gih=None,
                games_in_hand=120,
                alsa=4.4,
            ),
            105182: _stats(
                grp_id=105182,
                name="Fixture Final Pick",
                color="R",
                gih=None,
                games_in_hand=80,
                alsa=8.0,
            ),
            105003: _stats(
                grp_id=105003,
                name="Fixture Card 105003",
                color="C",
                gih=0.51,
                games_in_hand=700,
                alsa=6.2,
            ),
            105054: _stats(
                grp_id=105054,
                name="Fixture Card 105054",
                color="C",
                gih=0.50,
                games_in_hand=650,
                alsa=6.8,
            ),
        },
        pair_win_rates=_pair_win_rates(),
    )
    fallback = SeventeenLandsFormatData(
        set_code="MSH",
        event_format=PREMIER_DRAFT_FORMAT,
        fetched_at=datetime(2999, 1, 1, tzinfo=UTC),
        card_ratings={
            105134: _stats(
                grp_id=105134,
                name="Fixture Blue Card",
                color="U",
                gih=0.58,
                games_in_hand=900,
                alsa=3.6,
            ),
            104989: _stats(
                grp_id=104989,
                name="Fixture Card 104989",
                color="C",
                gih=0.57,
                games_in_hand=650,
                alsa=4.2,
            ),
        },
        pair_win_rates=_pair_win_rates(),
    )
    return SeventeenLandsData(
        set_code="MSH",
        requested_format=QUICK_DRAFT_FORMAT,
        primary=primary,
        fallback=fallback,
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


def _pair_win_rates() -> dict[str, ColorPairWinRate]:
    pairs = ("WU", "WB", "WR", "WG", "UB", "UR", "UG", "BR", "BG", "RG")
    return {
        pair: ColorPairWinRate(pair=pair, wins=50, games=100, win_rate=0.5)
        for pair in pairs
    }
