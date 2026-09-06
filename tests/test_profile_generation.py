from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import COLOR_PAIRS, DeckBuilderConfig
from draftomen.pickengine import PickEngine
from draftomen.profile_generation import (
    ProfileGenerationConfig,
    ProfileGenerationError,
    ProfileGenerationStage,
    deterministic_profile_gzip,
    generate_set_profile,
)
from draftomen.public_dump import PublicDumpManifest, PublicDumpSource
from draftomen.profile_statistics import BetaPrior
from draftomen.seventeen import (
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsFormatData,
)
from draftomen.set_profile import ProfileMaturity, SetProfile


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "profile-generation"
GENERATED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _source(name: str) -> PublicDumpSource:
    path = FIXTURE_DIR / name
    return PublicDumpSource(
        name=name,
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        retrieved_at="2026-08-30T00:00:00+00:00",
        attribution="fixture public data",
        license="CC0",
    )


def _database() -> CardDatabase:
    return CardDatabase(
        cards={
            1: CardInfo(
                grp_id=1,
                name="Support Creature",
                colors=("W", "U"),
                mana_value=2,
                rarity="common",
                types=("Creature",),
                type_line="Creature — Advisor",
                oracle_text="Whenever this enters the battlefield, draw a card.",
                oracle_id="support-id",
                set_code="TST",
            ),
            2: CardInfo(
                grp_id=2,
                name="Removal Spell",
                colors=("W", "U"),
                mana_value=2,
                rarity="common",
                types=("Instant",),
                type_line="Instant",
                oracle_text="Destroy target creature.",
                oracle_id="removal-id",
                set_code="TST",
            ),
        }
    )


def _ratings() -> SeventeenLandsFormatData:
    return SeventeenLandsFormatData(
        set_code="TST",
        event_format="QuickDraft",
        fetched_at=GENERATED_AT,
        card_ratings={
            1: SeventeenCardStats(
                grp_id=1,
                name="Support Creature",
                color="WU",
                rarity="common",
                average_last_seen_at=3.5,
                gih_win_rate=0.60,
                opening_hand_win_rate=None,
                drawn_improvement_win_rate=None,
                sample_counts=RatingSampleCounts(100, 50, 40, 20, 10),
            )
        },
        pair_win_rates={
            pair: ColorPairWinRate(pair=pair, wins=6, games=10, win_rate=0.60)
            for pair in COLOR_PAIRS[:1]
        },
    )


def _config() -> ProfileGenerationConfig:
    return ProfileGenerationConfig(
        card_prior=BetaPrior(mean=0.50, strength=500.0),
        pair_prior=BetaPrior(mean=0.50, strength=500.0),
        deck_builder_config=DeckBuilderConfig(
            deck_size=40,
            structure_min_land_count=14,
            structure_max_land_count=20,
        ),
    )


def test_metadata_stage_is_explicit_and_has_no_empirical_evidence() -> None:
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage=ProfileGenerationStage.METADATA,
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("no-data.csv"),)),
        generated_at=GENERATED_AT,
    )

    assert result.profile.maturity is ProfileMaturity.METADATA_ONLY
    assert result.profile.pairs == ()
    assert result.profile.card_ratings == ()
    assert result.report.samples.total == 0
    assert "path" not in result.report.to_bytes().decode()


def test_early_stage_has_all_pairs_and_beta_binomial_rates() -> None:
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("no-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )

    assert result.profile.maturity is ProfileMaturity.EARLY
    assert tuple(pair.pair for pair in result.profile.pairs) == COLOR_PAIRS
    assert result.profile.pair("WU").performance.samples == 10  # type: ignore[union-attr]
    assert result.profile.pair("WB").performance.samples == 0  # type: ignore[union-attr]
    card = result.profile.card_ratings[0]
    assert card.gih_win_rate.samples == 10
    assert card.gih_win_rate.value == pytest.approx((6 + 250) / 510)
    assert SetProfile.from_json(json.loads(result.profile.to_bytes())) == result.profile


def test_generated_early_profile_changes_public_pick_order_with_published_rate() -> None:
    generated = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage=ProfileGenerationStage.EARLY,
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("no-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=replace(
            _config(),
            card_prior=BetaPrior(mean=0.55, strength=10.0),
        ),
    )
    loaded = SetProfile.from_json(json.loads(generated.profile.to_bytes()))

    baseline = PickEngine().score_pack(
        offered_grp_ids=(2, 1),
        card_database=_database(),
    )
    scored = PickEngine(set_profile=loaded).score_pack(
        offered_grp_ids=(2, 1),
        card_database=_database(),
    )
    published = loaded.card_ratings[0].gih_win_rate
    profile_card = next(card for card in scored.cards if card.card.grp_id == 1)

    assert baseline.cards[0].card.grp_id == 2
    assert scored.cards[0].card.grp_id == 1
    assert published.value == pytest.approx((6 + (0.55 * 10)) / 20)
    assert published.raw_value == pytest.approx(0.60)
    assert published.samples == 10
    assert profile_card.rating.gih_win_rate == pytest.approx(published.value)
    assert profile_card.rating.sample_counts.games_in_hand == published.samples


def test_ratings_require_requested_set_metadata_and_bounded_rates() -> None:
    database = _database()
    other = replace(database.cards[2], set_code="OTH")
    database = CardDatabase(cards={**database.cards, 2: other})
    ratings = replace(
        _ratings(),
        card_ratings={
            1: replace(_ratings().card_ratings[1], gih_win_rate=2.0),
            2: _ratings().card_ratings[1],
        },
    )
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=database,
        source_manifest=PublicDumpManifest(sources=(_source("no-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=ratings,
        config=_config(),
    )

    assert result.profile.card_ratings == ()
    assert result.report.skip_reasons["card_rating_out_of_range"] == 1
    assert result.report.skip_reasons["card_rating_out_of_set"] == 1


def test_mature_stage_emits_pair_targets_and_semantic_profile() -> None:
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage=ProfileGenerationStage.MATURE,
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("mature-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )

    pair = result.profile.pair("WU")
    assert pair is not None
    assert pair.structural_targets
    assert pair.role_targets
    assert pair.removal_targets
    assert all(target.samples == 2 for target in pair.structural_targets)
    assert result.profile.role_profile is not None
    assert result.profile.roles_are_compatible


def test_reader_malformed_rows_are_reported_without_raw_values() -> None:
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("malformed-row.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )

    assert result.report.skip_reasons["extra_fields"] == 1
    serialized = result.report.to_bytes().decode()
    assert "unexpected" not in serialized
    assert "alpha" not in serialized


def test_sparse_targets_keep_raw_prior_sample_and_source_evidence() -> None:
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="mature",
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("low-sample.csv"),)),
        generated_at=GENERATED_AT,
        config=_config(),
    )

    pair = result.profile.pair("WU")
    assert pair is not None
    target = pair.removal_targets[0]
    assert target.samples == 1
    assert target.raw_value == pytest.approx(24.0)
    assert target.prior_value == pytest.approx(24.0)
    assert target.source == "17lands:public-draft-removals"


def test_profile_bytes_are_canonical_when_rows_are_reordered(tmp_path: Path) -> None:
    original = FIXTURE_DIR / "mature-data.csv"
    rows = original.read_text(encoding="utf-8").splitlines()
    reordered = tmp_path / "reordered.csv"
    reordered.write_text("\n".join([rows[0], *reversed(rows[1:])]) + "\n", encoding="utf-8")
    source = PublicDumpSource(
        name="reordered.csv",
        path=reordered,
        sha256=hashlib.sha256(reordered.read_bytes()).hexdigest(),
    )
    kwargs = dict(
        set_code="TST",
        event_format="QuickDraft",
        stage="mature",
        card_database=_database(),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )
    first = generate_set_profile(source_manifest=PublicDumpManifest(sources=(_source("mature-data.csv"),)), **kwargs)
    second = generate_set_profile(source_manifest=PublicDumpManifest(sources=(source,)), **kwargs)
    assert first.profile.to_bytes() == second.profile.to_bytes()


def test_profile_gzip_is_compact_deterministic_and_strictly_loadable() -> None:
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("no-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )

    compressed = deterministic_profile_gzip(result.profile.to_bytes())
    assert compressed == result.gzip_bytes
    assert len(compressed) < len(result.profile.to_bytes())
    assert result.report.profile_sha256 == hashlib.sha256(result.profile.to_bytes()).hexdigest()
    assert result.report.gzip_sha256 == hashlib.sha256(compressed).hexdigest()


def test_generated_card_ratings_never_include_alsa() -> None:
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("no-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )

    assert result.profile.card_ratings
    assert result.profile.card_ratings[0].average_last_seen_at is None
    assert "average_last_seen_at" not in result.profile.to_bytes().decode()


def test_report_pins_canonical_rating_and_requested_card_inputs() -> None:
    kwargs = dict(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        source_manifest=PublicDumpManifest(sources=(_source("no-data.csv"),)),
        generated_at=GENERATED_AT,
        config=_config(),
    )
    first = generate_set_profile(card_database=_database(), ratings=_ratings(), **kwargs)
    second = generate_set_profile(card_database=_database(), ratings=_ratings(), **kwargs)
    changed_ratings = replace(
        _ratings(),
        card_ratings={
            1: replace(_ratings().card_ratings[1], gih_win_rate=0.61),
        },
    )
    changed_database = CardDatabase(
        cards={
            **_database().cards,
            2: replace(_database().cards[2], oracle_text="Exile target creature."),
        }
    )
    changed_rating_result = generate_set_profile(
        card_database=_database(), ratings=changed_ratings, **kwargs
    )
    changed_database_result = generate_set_profile(
        card_database=changed_database, ratings=_ratings(), **kwargs
    )

    assert set(first.report.input_checksums) == {
        "no-data.csv",
        "ratings",
        "card_database",
    }
    assert first.report.to_bytes() == second.report.to_bytes()
    assert (
        first.report.input_checksums["ratings"]
        != changed_rating_result.report.input_checksums["ratings"]
    )
    assert (
        first.report.input_checksums["card_database"]
        != changed_database_result.report.input_checksums["card_database"]
    )
    serialized = first.report.to_bytes().decode()
    assert "Support Creature" not in serialized
    assert "no-data.csv" in serialized


def test_mature_stage_rejects_missing_accepted_decks() -> None:
    with pytest.raises(ProfileGenerationError, match="accepted deck evidence"):
        generate_set_profile(
            set_code="TST",
            event_format="QuickDraft",
            stage="mature",
            card_database=_database(),
            source_manifest=PublicDumpManifest(sources=(_source("no-data.csv"),)),
            generated_at=GENERATED_AT,
            ratings=_ratings(),
            config=_config(),
        )


def test_accepted_deck_rejections_have_stable_aggregate_reasons(tmp_path: Path) -> None:
    rows = [
        "draft_id,expansion,event_type,event_match_wins,pick,pick_maindeck_rate",
        *(
            f"mono,TST,QuickDraft,7,Support Creature,1.0"
            for _ in range(24)
        ),
        *(
            f"short,TST,QuickDraft,7,Removal Spell,1.0"
            for _ in range(10)
        ),
    ]
    path = tmp_path / "rejected-decks.csv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    source = PublicDumpSource(
        name="rejected-decks.csv",
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    database = CardDatabase(
        cards={
            1: replace(_database().cards[1], colors=("W",)),
            2: replace(_database().cards[2], colors=("W",)),
        }
    )
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=database,
        source_manifest=PublicDumpManifest(sources=(source,)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )

    assert result.report.skip_reasons["deck_unresolved_two_color_pair"] == 1
    assert result.report.skip_reasons["deck_inferred_lands_out_of_range"] == 1


def test_early_fixture_drives_structural_targets_deterministically() -> None:
    kwargs = dict(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=PublicDumpManifest(sources=(_source("early-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )
    first = generate_set_profile(**kwargs)
    second = generate_set_profile(**kwargs)
    pair = first.profile.pair("WU")

    assert first.report.samples.total == 1
    assert pair is not None
    assert pair.structural_targets
    assert first.profile.to_bytes() == second.profile.to_bytes()
    assert first.report.to_bytes() == second.report.to_bytes()


def test_generated_curve_metrics_honor_configured_thresholds() -> None:
    curve_database = CardDatabase(
        cards={
            **_database().cards,
            2: replace(_database().cards[2], mana_value=5.0),
        }
    )
    default = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="mature",
        card_database=curve_database,
        source_manifest=PublicDumpManifest(sources=(_source("mature-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )
    custom_config = replace(
        _config(),
        deck_builder_config=replace(
            _config().deck_builder_config,
            two_drop_mana_value=3.0,
            expensive_spell_mana_value=5.0,
        ),
    )
    custom = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="mature",
        card_database=curve_database,
        source_manifest=PublicDumpManifest(sources=(_source("mature-data.csv"),)),
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=custom_config,
    )

    default_targets = default.profile.pair("WU").structural_targets  # type: ignore[union-attr]
    custom_targets = custom.profile.pair("WU").structural_targets  # type: ignore[union-attr]
    default_two_drop_target = next(
        target for target in default_targets if target.name == "average_two_drop_count"
    )
    custom_two_drop_target = next(
        target for target in custom_targets if target.name == "average_two_drop_count"
    )
    default_expensive_target = next(
        target
        for target in default_targets
        if target.name == "average_expensive_spell_count"
    )
    custom_expensive_target = next(
        target
        for target in custom_targets
        if target.name == "average_expensive_spell_count"
    )

    assert default_two_drop_target.raw_value == pytest.approx(12.0)
    assert custom_two_drop_target.raw_value == pytest.approx(0.0)
    assert default_expensive_target.raw_value == pytest.approx(0.0)
    assert custom_expensive_target.raw_value == pytest.approx(12.0)
