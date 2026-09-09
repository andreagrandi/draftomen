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
    aggregate_evidence_needs_fallback,
    deterministic_profile_gzip,
    generate_set_profile,
)
from draftomen.profile_publication import validate_profile_generation
from draftomen.public_dump import PublicDumpManifest, PublicDumpSource
from draftomen.profile_statistics import BetaPrior
from draftomen.seventeen import (
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsFormatData,
)
from draftomen.semantic_roles import Role
from draftomen.set_profile import (
    ProfileMaturity,
    SetProfile,
    dump_set_profile,
    load_set_profile,
)


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


def _format_ratings(
    event_format: str,
    *,
    card1_rate: float = 0.60,
    card1_games: int = 1000,
    card2_rate: float = 0.40,
    card2_games: int = 1000,
    pair_wins: int = 600,
    pair_games: int = 1000,
) -> SeventeenLandsFormatData:
    base = _ratings()
    template = base.card_ratings[1]
    card_ratings = {
        1: replace(
            template,
            gih_win_rate=card1_rate if card1_games else None,
            sample_counts=replace(
                template.sample_counts,
                games_in_hand=card1_games,
            ),
        ),
        2: replace(
            template,
            grp_id=2,
            name="Removal Spell",
            gih_win_rate=card2_rate if card2_games else None,
            sample_counts=replace(
                template.sample_counts,
                games_in_hand=card2_games,
            ),
        ),
    }
    return replace(
        base,
        event_format=event_format,
        card_ratings=card_ratings,
        pair_win_rates={
            "WU": replace(
                base.pair_win_rates["WU"],
                wins=pair_wins,
                games=pair_games,
                win_rate=None if pair_games == 0 else pair_wins / pair_games,
            )
        },
    )


def _complete_ratings(event_format: str = "QuickDraft") -> SeventeenLandsFormatData:
    ratings = _format_ratings(event_format)
    return replace(
        ratings,
        pair_win_rates={
            pair: ColorPairWinRate(pair=pair, wins=600, games=1000, win_rate=0.60)
            for pair in COLOR_PAIRS
        },
    )


def test_aggregate_evidence_coverage_requires_all_cards_and_pairs() -> None:
    complete = _complete_ratings()
    assert not aggregate_evidence_needs_fallback(
        set_code="TST",
        event_format="QuickDraft",
        card_database=_database(),
        ratings=complete,
    )

    card_gap = replace(complete, card_ratings={1: complete.card_ratings[1]})
    assert aggregate_evidence_needs_fallback(
        set_code="TST",
        event_format="QuickDraft",
        card_database=_database(),
        ratings=card_gap,
    )

    pair_gap = replace(
        complete,
        pair_win_rates={
            pair: value
            for pair, value in complete.pair_win_rates.items()
            if pair != COLOR_PAIRS[-1]
        },
    )
    assert aggregate_evidence_needs_fallback(
        set_code="TST",
        event_format="QuickDraft",
        card_database=_database(),
        ratings=pair_gap,
    )


def test_aggregate_evidence_coverage_accepts_one_supported_duplicate_arena_id() -> None:
    database = CardDatabase(
        cards={
            **_database().cards,
            3: replace(_database().cards[1], grp_id=3),
        }
    )
    complete = _complete_ratings()
    duplicate_id_ratings = replace(
        complete,
        card_ratings={
            2: complete.card_ratings[2],
            3: replace(complete.card_ratings[1], grp_id=3),
        },
    )
    assert not aggregate_evidence_needs_fallback(
        set_code="TST",
        event_format="QuickDraft",
        card_database=database,
        ratings=duplicate_id_ratings,
    )


def test_aggregate_evidence_non_quick_validates_candidates_then_returns_false() -> None:
    with pytest.raises(ProfileGenerationError, match="fallback ratings"):
        aggregate_evidence_needs_fallback(
            set_code="TST",
            event_format="PremierDraft",
            card_database=_database(),
            ratings=_format_ratings("PremierDraft"),
            fallback_ratings=(object(),),  # type: ignore[tuple-item]
        )
    assert not aggregate_evidence_needs_fallback(
        set_code="TST",
        event_format="PremierDraft",
        card_database=_database(),
        ratings=_format_ratings("PremierDraft"),
        fallback_ratings=(_format_ratings("TradDraft"),),
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


def test_early_stage_compiles_semantic_roles_without_public_drafts() -> None:
    kwargs = dict(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=_ratings(),
    )
    result = generate_set_profile(config=_config(), **kwargs)
    loaded = SetProfile.from_json(json.loads(result.profile.to_bytes()))

    assert loaded == result.profile
    assert loaded.maturity is ProfileMaturity.EARLY
    assert loaded.roles_are_compatible
    assert loaded.samples is not None
    assert loaded.samples.total == 0
    wu = loaded.pair("WU")
    assert wu is not None
    assert wu.role_targets == ()
    assert wu.removal_targets == ()

    draw = loaded.resolve_roles(_database().cards[1])
    assert draw.source == "compiled_profile"
    assert any(assignment.role is Role.DRAW for assignment in draw.assignments)
    removal = loaded.resolve_roles(_database().cards[2])
    assert removal.source == "compiled_profile"
    assert any(assignment.role is Role.HARD_REMOVAL for assignment in removal.assignments)

    repeated = generate_set_profile(config=_config(), **kwargs)
    assert repeated.profile.to_bytes() == result.profile.to_bytes()

    scaled = generate_set_profile(
        config=replace(_config(), confidence_sample_scale=100.0),
        **kwargs,
    )
    assert scaled.profile.confidence != result.profile.confidence
    assert scaled.profile.role_profile == result.profile.role_profile

    without_roles = generate_set_profile(
        config=replace(_config(), include_role_profile=False),
        **kwargs,
    )
    assert without_roles.profile.role_profile is None
    assert without_roles.profile.pairs == result.profile.pairs
    assert without_roles.profile.card_ratings == result.profile.card_ratings


def test_early_role_compilation_skips_unresolved_cards_safely() -> None:
    database = _database()
    unresolved_card = replace(
        database.cards[2],
        types=("Instant",),
        type_line="Instant",
        oracle_text=None,
        keywords=(),
    )
    database = CardDatabase(cards={**database.cards, 2: unresolved_card})
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=database,
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )
    loaded = SetProfile.from_json(json.loads(result.profile.to_bytes()))

    assert loaded.maturity is ProfileMaturity.EARLY
    assert loaded.role_profile is not None
    draw = loaded.resolve_roles(database.cards[1])
    assert draw.source == "compiled_profile"
    assert any(assignment.role is Role.DRAW for assignment in draw.assignments)
    assert loaded.resolve_roles(database.cards[2]).assignments == ()

    all_unclassifiable = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=CardDatabase(cards={2: unresolved_card}),
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=_ratings(),
        config=_config(),
    )
    assert all_unclassifiable.profile.maturity is ProfileMaturity.EARLY
    assert all_unclassifiable.profile.role_profile is None
    all_unclassifiable_wu = all_unclassifiable.profile.pair("WU")
    assert all_unclassifiable_wu is not None
    assert all_unclassifiable_wu.performance is not None
    assert all_unclassifiable_wu.performance.samples == 10


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
            2: replace(_ratings().card_ratings[1], grp_id=2),
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



def test_aggregate_card_and_pair_precedence_are_independent() -> None:
    exact = _format_ratings(
        "QuickDraft",
        card1_rate=0.60,
        card1_games=500,
        card2_rate=0.40,
        card2_games=500,
        pair_wins=499,
        pair_games=499,
    )
    premier = _format_ratings(
        "PremierDraft",
        card1_rate=0.20,
        card2_rate=0.80,
        pair_wins=700,
        pair_games=1000,
    )
    trad = _format_ratings(
        "TradDraft",
        card1_rate=0.80,
        card2_rate=0.20,
        pair_wins=800,
        pair_games=1000,
    )
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=exact,
        fallback_ratings=(trad, premier),
        config=_config(),
    )
    cards = {card.card_key: card.gih_win_rate for card in result.profile.card_ratings}
    assert all(rate.aggregate_evidence is not None for rate in cards.values())
    assert all(rate.aggregate_evidence.source_format == "quickdraft" for rate in cards.values())
    pair = result.profile.pair("WU")
    assert pair is not None and pair.performance is not None
    assert pair.performance.aggregate_evidence is not None
    assert pair.performance.aggregate_evidence.source_format == "premierdraft"
    assert pair.performance.aggregate_evidence.fallback_reason == "thin-exact-evidence"
    assert pair.performance.aggregate_evidence.confidence == pytest.approx(0.65)

    exact_supported_pair = _format_ratings(
        "QuickDraft",
        card1_rate=0.60,
        card1_games=500,
        card2_rate=0.40,
        card2_games=500,
        pair_wins=250,
        pair_games=500,
    )
    restored = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=exact_supported_pair,
        fallback_ratings=(premier, trad),
        config=_config(),
    )
    restored_pair = restored.profile.pair("WU")
    assert restored_pair is not None and restored_pair.performance is not None
    assert restored_pair.performance.aggregate_evidence is not None
    assert restored_pair.performance.aggregate_evidence.source_format == "quickdraft"


def test_aggregate_candidates_are_validated_before_selection() -> None:
    premier = _format_ratings("PremierDraft")
    with pytest.raises(ProfileGenerationError, match="fallback ratings must match the requested set"):
        generate_set_profile(
            set_code="TST",
            event_format="QuickDraft",
            stage="early",
            card_database=_database(),
            source_manifest=None,
            generated_at=GENERATED_AT,
            ratings=_ratings(),
            fallback_ratings=(replace(premier, set_code="OTH"),),
            config=_config(),
        )
    with pytest.raises(ProfileGenerationError, match="duplicate aggregate source format"):
        generate_set_profile(
            set_code="TST",
            event_format="QuickDraft",
            stage="early",
            card_database=_database(),
            source_manifest=None,
            generated_at=GENERATED_AT,
            fallback_ratings=(premier, premier),
            config=_config(),
        )
    non_quick = generate_set_profile(
        set_code="TST",
        event_format="PremierDraft",
        stage="early",
        card_database=_database(),
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=_format_ratings("PremierDraft", card1_games=10, card2_games=10),
        fallback_ratings=(_format_ratings("TradDraft"),),
        config=_config(),
    )
    assert all(
        card.gih_win_rate.aggregate_evidence.source_format == "premierdraft"
        for card in non_quick.profile.card_ratings
        if card.gih_win_rate.samples
    )


def test_exact_thin_data_is_preserved_and_identity_changes_are_checksummed() -> None:
    thin = _format_ratings(
        "QuickDraft",
        card1_games=10,
        card2_games=0,
        pair_wins=6,
        pair_games=10,
    )
    fallback = _format_ratings("PremierDraft", card1_games=499, card2_games=499, pair_games=499)
    result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=thin,
        fallback_ratings=(fallback,),
        config=_config(),
    )
    card = next(card for card in result.profile.card_ratings if card.card_key == "oracle_id:support-id")
    assert card.gih_win_rate.samples == 10
    assert card.gih_win_rate.aggregate_evidence is not None
    assert card.gih_win_rate.aggregate_evidence.source_format == "quickdraft"
    changed_card = replace(thin.card_ratings[1], grp_id=2)
    changed = replace(thin, card_ratings={1: changed_card})
    changed_result = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=_database(),
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=changed,
        fallback_ratings=(fallback,),
        config=_config(),
    )
    assert changed_result.report.input_checksums["ratings"] != result.report.input_checksums["ratings"]


def _generate_tst(
    *,
    ratings: SeventeenLandsFormatData | None,
    fallback_ratings: tuple[SeventeenLandsFormatData, ...] = (),
    event_format: str = "QuickDraft",
    card_database: CardDatabase | None = None,
    config: ProfileGenerationConfig | None = None,
):
    return generate_set_profile(
        set_code="TST",
        event_format=event_format,
        stage="early",
        card_database=_database() if card_database is None else card_database,
        source_manifest=None,
        generated_at=GENERATED_AT,
        ratings=ratings,
        fallback_ratings=fallback_ratings,
        config=_config() if config is None else config,
    )


def test_aggregate_support_threshold_and_canonical_printing_precedence() -> None:
    database = CardDatabase(
        cards={
            **_database().cards,
            3: replace(_database().cards[1], grp_id=3),
        }
    )
    template = _ratings().card_ratings[1]
    exact = replace(
        _format_ratings(
            "QuickDraft",
            card1_rate=0.10,
            card1_games=10,
            card2_games=10,
            pair_wins=250,
            pair_games=500,
        ),
        card_ratings={
            1: replace(
                template,
                gih_win_rate=0.10,
                sample_counts=replace(template.sample_counts, games_in_hand=10),
            ),
            3: replace(
                template,
                grp_id=3,
                gih_win_rate=0.90,
                sample_counts=replace(template.sample_counts, games_in_hand=500),
            ),
        },
    )
    premier = _format_ratings(
        "PremierDraft",
        card1_rate=0.20,
        card1_games=1000,
        card2_games=1000,
        pair_wins=600,
        pair_games=1000,
    )
    canonical = _generate_tst(
        ratings=exact,
        fallback_ratings=(premier,),
        card_database=database,
    )
    support = next(
        card for card in canonical.profile.card_ratings if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    assert support.raw_value == pytest.approx(0.90)
    assert support.samples == 500
    assert support.aggregate_evidence is not None
    assert support.aggregate_evidence.source_format == "quickdraft"
    assert support.aggregate_evidence.confidence == pytest.approx(0.5)

    missing_exact = _generate_tst(ratings=None, fallback_ratings=(premier,))
    missing_card = next(
        card
        for card in missing_exact.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    assert missing_card.raw_value == pytest.approx(0.20)
    assert missing_card.aggregate_evidence is not None
    assert missing_card.aggregate_evidence.source_format == "premierdraft"
    assert missing_card.aggregate_evidence.fallback_reason == "missing-exact-evidence"

    premier_thin = _format_ratings(
        "PremierDraft",
        card1_games=499,
        card2_games=499,
        pair_wins=299,
        pair_games=499,
    )
    trad_supported = _format_ratings(
        "TradDraft",
        card1_rate=0.80,
        card2_rate=0.20,
        card1_games=500,
        card2_games=500,
        pair_wins=500,
        pair_games=500,
    )
    trad_result = _generate_tst(
        ratings=None,
        fallback_ratings=(trad_supported, premier_thin),
    )
    trad_card = next(
        card
        for card in trad_result.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    trad_pair = trad_result.profile.pair("WU")
    assert trad_card.aggregate_evidence is not None
    assert trad_card.aggregate_evidence.source_format == "traddraft"
    assert trad_card.aggregate_evidence.confidence == pytest.approx(0.325)
    assert trad_pair is not None and trad_pair.performance is not None
    assert trad_pair.performance.aggregate_evidence is not None
    assert trad_pair.performance.aggregate_evidence.source_format == "traddraft"
    assert trad_pair.performance.samples == 500


def test_thin_fallback_never_displaces_valid_exact_evidence_or_non_quick_exact() -> None:
    exact = _format_ratings(
        "QuickDraft",
        card1_games=10,
        card2_games=10,
        pair_wins=6,
        pair_games=10,
    )
    thin_fallback = _format_ratings(
        "PremierDraft",
        card1_games=499,
        card2_games=499,
        pair_wins=299,
        pair_games=499,
    )
    result = _generate_tst(ratings=exact, fallback_ratings=(thin_fallback,))
    card = next(
        card for card in result.profile.card_ratings if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    pair = result.profile.pair("WU")
    assert card.samples == 10
    assert card.aggregate_evidence is not None
    assert card.aggregate_evidence.source_format == "quickdraft"
    assert card.aggregate_evidence.fallback_reason is None
    assert pair is not None and pair.performance is not None
    assert pair.performance.samples == 10
    assert pair.performance.aggregate_evidence is not None
    assert pair.performance.aggregate_evidence.source_format == "quickdraft"

    non_quick = _generate_tst(
        ratings=_format_ratings(
            "PremierDraft",
            card1_games=10,
            card2_games=10,
            pair_wins=6,
            pair_games=10,
        ),
        fallback_ratings=(_format_ratings("TradDraft"),),
        event_format="PremierDraft",
    )
    non_quick_card = next(
        card
        for card in non_quick.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    non_quick_pair = non_quick.profile.pair("WU")
    assert non_quick_card.aggregate_evidence is not None
    assert non_quick_card.aggregate_evidence.source_format == "premierdraft"
    assert non_quick_pair is not None and non_quick_pair.performance is not None
    assert non_quick_pair.performance.aggregate_evidence is not None
    assert non_quick_pair.performance.aggregate_evidence.source_format == "premierdraft"


def test_aggregate_confidence_is_local_and_preserves_rate_math_and_roles() -> None:
    for samples, exact_confidence, fallback_confidence in (
        (500, 0.50, 0.325),
        (1000, 1.0, 0.65),
    ):
        exact = _format_ratings(
            "QuickDraft",
            card1_rate=0.60,
            card1_games=samples,
            card2_rate=0.40,
            card2_games=samples,
            pair_wins=round(0.60 * samples),
            pair_games=samples,
        )
        premier = _format_ratings(
            "PremierDraft",
            card1_rate=0.60,
            card1_games=samples,
            card2_rate=0.40,
            card2_games=samples,
            pair_wins=round(0.60 * samples),
            pair_games=samples,
        )
        exact_result = _generate_tst(ratings=exact, fallback_ratings=(premier,))
        fallback_result = _generate_tst(ratings=None, fallback_ratings=(premier,))
        exact_card = next(
            card
            for card in exact_result.profile.card_ratings
            if card.card_key == "oracle_id:support-id"
        ).gih_win_rate
        fallback_card = next(
            card
            for card in fallback_result.profile.card_ratings
            if card.card_key == "oracle_id:support-id"
        ).gih_win_rate
        exact_pair = exact_result.profile.pair("WU")
        fallback_pair = fallback_result.profile.pair("WU")
        assert exact_pair is not None and exact_pair.performance is not None
        assert fallback_pair is not None and fallback_pair.performance is not None

        assert (
            exact_card.raw_value,
            exact_card.value,
            exact_card.prior_value,
            exact_card.samples,
        ) == (
            fallback_card.raw_value,
            fallback_card.value,
            fallback_card.prior_value,
            fallback_card.samples,
        )
        expected_value = (round(0.60 * samples) + (0.50 * 500.0)) / (samples + 500.0)
        assert exact_card.raw_value == pytest.approx(0.60)
        assert exact_card.value == pytest.approx(expected_value)
        assert exact_pair.performance.raw_value == pytest.approx(0.60)
        assert exact_pair.performance.value == pytest.approx(expected_value)
        assert exact_pair.performance.raw_value == fallback_pair.performance.raw_value
        assert exact_pair.performance.value == fallback_pair.performance.value
        assert exact_pair.performance.prior_value == fallback_pair.performance.prior_value
        assert exact_pair.performance.samples == fallback_pair.performance.samples
        assert exact_card.aggregate_evidence is not None
        assert fallback_card.aggregate_evidence is not None
        assert exact_card.aggregate_evidence.source_format == "quickdraft"
        assert fallback_card.aggregate_evidence.source_format == "premierdraft"
        assert exact_card.aggregate_evidence.confidence == pytest.approx(exact_confidence)
        assert fallback_card.aggregate_evidence.confidence == pytest.approx(
            fallback_confidence
        )
        assert exact_pair.performance.aggregate_evidence is not None
        assert fallback_pair.performance.aggregate_evidence is not None
        assert exact_pair.performance.aggregate_evidence.confidence == pytest.approx(
            exact_confidence
        )
        assert fallback_pair.performance.aggregate_evidence.confidence == pytest.approx(
            fallback_confidence
        )
        assert exact_result.profile.role_profile == fallback_result.profile.role_profile
        for card in _database().cards.values():
            assert exact_result.profile.resolve_roles(card) == fallback_result.profile.resolve_roles(
                card
            )


def test_malformed_and_identity_mismatches_are_not_recovered_by_display_names() -> None:
    premier = _format_ratings(
        "PremierDraft",
        card1_rate=0.20,
        card1_games=1000,
        card2_games=1000,
        pair_wins=700,
        pair_games=1000,
    )
    malformed_exact = _format_ratings(
        "QuickDraft",
        card1_rate=float("nan"),
        card1_games=1000,
        card2_games=1000,
        pair_wins=500,
        pair_games=1000,
    )
    malformed = _generate_tst(
        ratings=malformed_exact,
        fallback_ratings=(premier,),
    )
    malformed_card = next(
        card
        for card in malformed.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    assert malformed_card.aggregate_evidence is not None
    assert malformed_card.aggregate_evidence.source_format == "premierdraft"
    assert malformed_card.aggregate_evidence.fallback_reason == "invalid-exact-evidence"

    unknown_stats = replace(
        malformed_exact.card_ratings[1],
        grp_id=999,
        name="Support Creature",
        gih_win_rate=0.99,
    )
    unknown = _generate_tst(
        ratings=replace(malformed_exact, card_ratings={999: unknown_stats}),
        fallback_ratings=(premier,),
    )
    unknown_card = next(
        card
        for card in unknown.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    assert unknown_card.aggregate_evidence is not None
    assert unknown_card.aggregate_evidence.source_format == "premierdraft"
    assert unknown_card.aggregate_evidence.fallback_reason == "missing-exact-evidence"

    card_identity = replace(
        malformed_exact,
        card_ratings={
            1: replace(malformed_exact.card_ratings[1], grp_id=2)
        },
    )
    pair_identity = replace(
        malformed_exact,
        pair_win_rates={
            "WU": replace(malformed_exact.pair_win_rates["WU"], pair="WB")
        },
    )
    card_identity_result = _generate_tst(
        ratings=card_identity,
        fallback_ratings=(premier,),
    )
    pair_identity_result = _generate_tst(
        ratings=pair_identity,
        fallback_ratings=(premier,),
    )
    card_identity_rate = next(
        card
        for card in card_identity_result.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    pair_identity_profile = pair_identity_result.profile.pair("WU")
    assert card_identity_rate.aggregate_evidence is not None
    assert card_identity_rate.aggregate_evidence.source_format == "premierdraft"
    assert card_identity_rate.aggregate_evidence.fallback_reason == "invalid-exact-evidence"
    assert pair_identity_profile is not None and pair_identity_profile.performance is not None
    assert pair_identity_profile.performance.aggregate_evidence is not None
    assert pair_identity_profile.performance.aggregate_evidence.source_format == "premierdraft"
    assert pair_identity_profile.performance.aggregate_evidence.fallback_reason == (
        "invalid-exact-evidence"
    )

    assert malformed.report.input_checksums["ratings"] != card_identity_result.report.input_checksums[
        "ratings"
    ]
    assert malformed.report.input_checksums["ratings"] != pair_identity_result.report.input_checksums[
        "ratings"
    ]


def test_embedded_identities_alone_change_checksums_and_selected_authority() -> None:
    exact = _format_ratings(
        "QuickDraft",
        card1_rate=0.60,
        card1_games=1000,
        card2_games=1000,
        pair_wins=600,
        pair_games=1000,
    )
    premier = _format_ratings(
        "PremierDraft",
        card1_rate=0.20,
        card1_games=1000,
        card2_games=1000,
        pair_wins=700,
        pair_games=1000,
    )
    changed_card = replace(
        exact,
        card_ratings={
            **exact.card_ratings,
            1: replace(exact.card_ratings[1], grp_id=2),
        },
    )
    changed_pair = replace(
        exact,
        pair_win_rates={
            **exact.pair_win_rates,
            "WU": replace(exact.pair_win_rates["WU"], pair="WB"),
        },
    )
    non_string_pair = replace(
        exact,
        pair_win_rates={
            **exact.pair_win_rates,
            "WU": replace(exact.pair_win_rates["WU"], pair=7),  # type: ignore[arg-type]
        },
    )
    baseline = _generate_tst(ratings=exact, fallback_ratings=(premier,))
    card_changed = _generate_tst(ratings=changed_card, fallback_ratings=(premier,))
    pair_changed = _generate_tst(ratings=changed_pair, fallback_ratings=(premier,))
    non_string_changed = _generate_tst(
        ratings=non_string_pair,
        fallback_ratings=(premier,),
    )

    baseline_card = next(
        card
        for card in baseline.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    changed_card_rate = next(
        card
        for card in card_changed.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    assert baseline_card.aggregate_evidence is not None
    assert baseline_card.aggregate_evidence.source_format == "quickdraft"
    assert changed_card_rate.aggregate_evidence is not None
    assert changed_card_rate.aggregate_evidence.source_format == "premierdraft"
    assert changed_card_rate.aggregate_evidence.fallback_reason == "invalid-exact-evidence"

    for result in (pair_changed, non_string_changed):
        pair = result.profile.pair("WU")
        assert pair is not None and pair.performance is not None
        assert pair.performance.aggregate_evidence is not None
        assert pair.performance.aggregate_evidence.source_format == "premierdraft"
        assert pair.performance.aggregate_evidence.fallback_reason == "invalid-exact-evidence"

    baseline_checksum = baseline.report.input_checksums["ratings"]
    assert card_changed.report.input_checksums["ratings"] != baseline_checksum
    assert pair_changed.report.input_checksums["ratings"] != baseline_checksum
    assert non_string_changed.report.input_checksums["ratings"] != baseline_checksum


def test_valid_thin_exact_printing_determines_fallback_reason_over_malformed_printing() -> None:
    database = CardDatabase(
        cards={
            **_database().cards,
            3: replace(_database().cards[1], grp_id=3),
        }
    )
    template = _ratings().card_ratings[1]
    exact = replace(
        _format_ratings("QuickDraft", card1_games=10, card2_games=1000),
        card_ratings={
            1: replace(
                template,
                gih_win_rate=0.60,
                sample_counts=replace(template.sample_counts, games_in_hand=10),
            ),
            3: replace(
                template,
                grp_id=3,
                gih_win_rate=float("nan"),
                sample_counts=replace(template.sample_counts, games_in_hand=1000),
            ),
        },
    )
    premier = _format_ratings(
        "PremierDraft",
        card1_rate=0.20,
        card1_games=1000,
        card2_games=1000,
    )
    result = _generate_tst(
        ratings=exact,
        fallback_ratings=(premier,),
        card_database=database,
    )
    rate = next(
        card
        for card in result.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate

    assert rate.aggregate_evidence is not None
    assert rate.aggregate_evidence.source_format == "premierdraft"
    assert rate.aggregate_evidence.fallback_reason == "thin-exact-evidence"


def test_invalid_nested_sample_counts_change_selection_and_checksum() -> None:
    exact = _format_ratings(
        "QuickDraft",
        card1_rate=0.60,
        card1_games=1000,
        card2_games=1000,
    )
    invalid_stats = replace(exact.card_ratings[1], sample_counts=1000)  # type: ignore[arg-type]
    invalid_exact = replace(
        exact,
        card_ratings={**exact.card_ratings, 1: invalid_stats},
    )
    premier = _format_ratings(
        "PremierDraft",
        card1_rate=0.20,
        card1_games=1000,
        card2_games=1000,
    )
    exact_result = _generate_tst(ratings=exact, fallback_ratings=(premier,))
    invalid_result = _generate_tst(
        ratings=invalid_exact,
        fallback_ratings=(premier,),
    )
    exact_rate = next(
        card
        for card in exact_result.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate
    invalid_rate = next(
        card
        for card in invalid_result.profile.card_ratings
        if card.card_key == "oracle_id:support-id"
    ).gih_win_rate

    assert exact_rate.aggregate_evidence is not None
    assert exact_rate.aggregate_evidence.source_format == "quickdraft"
    assert invalid_rate.aggregate_evidence is not None
    assert invalid_rate.aggregate_evidence.source_format == "premierdraft"
    assert invalid_rate.aggregate_evidence.fallback_reason == "invalid-exact-evidence"
    assert exact_result.report.input_checksums["ratings"] != invalid_result.report.input_checksums[
        "ratings"
    ]


def _lci_database() -> CardDatabase:
    return CardDatabase.from_json(
        json.loads((FIXTURE_DIR / "lci-card-database.json").read_text(encoding="utf-8"))
    )


def _lci_ratings(
    event_format: str,
    *,
    chart_rate: float,
    bitter_rate: float,
    card_games: int = 1000,
    pair_wins: int | None = None,
    pair_games: int = 0,
) -> SeventeenLandsFormatData:
    cards = {
        87185: SeventeenCardStats(
            grp_id=87185,
            name="Chart a Course",
            color="U",
            rarity="uncommon",
            average_last_seen_at=None,
            gih_win_rate=chart_rate if card_games else None,
            opening_hand_win_rate=None,
            drawn_improvement_win_rate=None,
            sample_counts=RatingSampleCounts(
                seen=0,
                picked=0,
                games_played=0,
                opening_hand=0,
                games_in_hand=card_games,
            ),
        ),
        87235: SeventeenCardStats(
            grp_id=87235,
            name="Bitter Triumph",
            color="B",
            rarity="uncommon",
            average_last_seen_at=None,
            gih_win_rate=bitter_rate if card_games else None,
            opening_hand_win_rate=None,
            drawn_improvement_win_rate=None,
            sample_counts=RatingSampleCounts(
                seen=0,
                picked=0,
                games_played=0,
                opening_hand=0,
                games_in_hand=card_games,
            ),
        ),
    }
    pairs = {}
    if pair_wins is not None and pair_games:
        pairs["UB"] = ColorPairWinRate(
            pair="UB",
            wins=pair_wins,
            games=pair_games,
            win_rate=pair_wins / pair_games,
        )
    return SeventeenLandsFormatData(
        set_code="LCI",
        event_format=event_format,
        fetched_at=datetime(2026, 9, 9, tzinfo=UTC),
        card_ratings=cards,
        pair_win_rates=pairs,
    )


def test_lci_aggregate_generation_serialization_scoring_and_order_are_canonical(
    tmp_path: Path,
) -> None:
    database = _lci_database()
    config = replace(
        _config(),
        card_prior=BetaPrior(mean=0.5, strength=10.0),
        pair_prior=BetaPrior(mean=0.5, strength=10.0),
    )
    premier = _lci_ratings(
        "PremierDraft",
        chart_rate=0.70,
        bitter_rate=0.40,
        pair_wins=600,
        pair_games=1000,
    )
    trad = _lci_ratings(
        "TradDraft",
        chart_rate=0.40,
        bitter_rate=0.70,
        pair_wins=550,
        pair_games=1000,
    )
    complete_fallback = generate_set_profile(
        set_code="LCI",
        event_format="QuickDraft",
        stage="early",
        card_database=database,
        source_manifest=None,
        generated_at=datetime(2026, 9, 9, tzinfo=UTC),
        ratings=None,
        fallback_ratings=(trad, premier),
        config=config,
    )
    validate_profile_generation(
        generation=complete_fallback,
        set_code="lci",
        event_format="quickdraft",
        stage="early",
    )
    path = tmp_path / "lci-quickdraft.json"
    dump_set_profile(complete_fallback.profile, path)
    loaded = load_set_profile(
        path,
        expected_set_code="LCI",
        expected_format="QuickDraft",
    )
    assert loaded.to_bytes() == complete_fallback.profile.to_bytes()
    assert loaded.schema_version == 2
    assert loaded.set_code == "lci"
    assert loaded.event_format == "quickdraft"

    chart_key = "oracle_id:05878e49-93ad-4144-9c50-a0bb86126c2e"
    bitter_key = "oracle_id:776341cb-d2ec-423f-9250-92dc8bd8d503"
    cards = {card.card_key: card for card in loaded.card_ratings}
    assert cards[chart_key].gih_win_rate.value == pytest.approx(705 / 1010)
    assert cards[bitter_key].gih_win_rate.value == pytest.approx(405 / 1010)
    assert cards[chart_key].gih_win_rate.samples == 1000
    assert cards[bitter_key].gih_win_rate.samples == 1000
    for key in (chart_key, bitter_key):
        evidence = cards[key].gih_win_rate.aggregate_evidence
        assert evidence is not None
        assert evidence.source_format == "premierdraft"
        assert evidence.fallback_reason == "missing-exact-evidence"
        assert evidence.confidence == pytest.approx(0.65)
    ub = loaded.pair("UB")
    assert ub is not None and ub.performance is not None
    assert ub.performance.value == pytest.approx(605 / 1010)
    assert ub.performance.samples == 1000
    assert ub.performance.aggregate_evidence is not None
    assert ub.performance.aggregate_evidence.source_format == "premierdraft"

    scored = PickEngine(set_profile=loaded).score_pack(
        offered_grp_ids=(87185, 87235),
        card_database=database,
    )
    assert [card.card.grp_id for card in scored.cards] == [87185, 87235]
    for grp_id, key in ((87185, chart_key), (87235, bitter_key)):
        scored_card = next(card for card in scored.cards if card.card.grp_id == grp_id)
        evidence = cards[key].gih_win_rate.aggregate_evidence
        assert evidence is not None
        assert scored_card.rating.metadata.requested_format == "quickdraft"
        assert scored_card.rating.metadata.source == "profile"
        assert scored_card.rating.metadata.source_format == evidence.source_format
        assert scored_card.rating.metadata.fallback_reason == evidence.fallback_reason
        assert scored_card.rating.gih_win_rate == pytest.approx(
            cards[key].gih_win_rate.value
        )

    assert complete_fallback.report.card_games == 2000
    assert complete_fallback.report.pair_games == 1000
    assert set(complete_fallback.report.input_checksums) == {
        "ratings",
        "fallback_ratings:premierdraft",
        "fallback_ratings:traddraft",
        "card_database",
    }

    exact_mixed = _lci_ratings(
        "QuickDraft",
        chart_rate=0.40,
        bitter_rate=0.70,
        pair_wins=None,
        pair_games=0,
    )
    mixed = generate_set_profile(
        set_code="LCI",
        event_format="QuickDraft",
        stage="early",
        card_database=database,
        source_manifest=None,
        generated_at=datetime(2026, 9, 9, tzinfo=UTC),
        ratings=exact_mixed,
        fallback_ratings=(trad, premier),
        config=config,
    )
    mixed_cards = {card.card_key: card for card in mixed.profile.card_ratings}
    for key in (chart_key, bitter_key):
        evidence = mixed_cards[key].gih_win_rate.aggregate_evidence
        assert evidence is not None
        assert evidence.source_format == "quickdraft"
        assert evidence.fallback_reason is None
        assert evidence.confidence == pytest.approx(1.0)
    mixed_pair = mixed.profile.pair("UB")
    assert mixed_pair is not None and mixed_pair.performance is not None
    assert mixed_pair.performance.aggregate_evidence is not None
    assert mixed_pair.performance.aggregate_evidence.source_format == "premierdraft"
    assert mixed_pair.performance.aggregate_evidence.fallback_reason == (
        "missing-exact-evidence"
    )
    mixed_scored = PickEngine(set_profile=mixed.profile).score_pack(
        offered_grp_ids=(87185, 87235),
        card_database=database,
    )
    assert [card.card.grp_id for card in mixed_scored.cards] == [87235, 87185]
    for scored_card in mixed_scored.cards:
        assert scored_card.rating.metadata.requested_format == "quickdraft"
        assert scored_card.rating.metadata.source == "profile"
        assert scored_card.rating.metadata.source_format == "quickdraft"
        assert scored_card.rating.metadata.fallback_reason is None
    assert mixed.profile.role_profile == complete_fallback.profile.role_profile
    assert SetProfile.from_json(json.loads(mixed.profile.to_bytes())) == mixed.profile
    assert mixed.report.card_games == 2000
    assert mixed.report.pair_games == 1000
    assert mixed.report.input_checksums["ratings"] != complete_fallback.report.input_checksums[
        "ratings"
    ]
    assert set(mixed.report.input_checksums) == {
        "ratings",
        "fallback_ratings:premierdraft",
        "fallback_ratings:traddraft",
        "card_database",
    }

    reversed_candidates = generate_set_profile(
        set_code="LCI",
        event_format="QuickDraft",
        stage="early",
        card_database=database,
        source_manifest=None,
        generated_at=datetime(2026, 9, 9, tzinfo=UTC),
        ratings=None,
        fallback_ratings=(premier, trad),
        config=config,
    )
    assert reversed_candidates.profile.to_bytes() == complete_fallback.profile.to_bytes()
    assert reversed_candidates.report.to_bytes() == complete_fallback.report.to_bytes()
