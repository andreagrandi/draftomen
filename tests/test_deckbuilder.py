from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from draftomen.carddb import CardDatabase, CardInfo, build_card_database_from_bulk_file
from draftomen.config import DECK_BUILDER, DeckBuilderConfig
from draftomen.deckbuilder import (
    BuildPool,
    DeckBuilderError,
    ManaBase,
    SpellConstraints,
    SpellCounts,
    _build_optimizer_context,
    _card_quantity_key,
    _greedy_feasible_package,
    _optimizer_card_keys,
    _optimizer_creature_score,
    _optimizer_curve_score,
    _optimizer_objective,
    _select_with_constraints,
    build_deck_from_pool,
    format_build_result,
    load_persisted_pool,
    load_pool_file,
    select_build_sheet,
    select_color_pair,
    select_deck_spells,
    select_mana_base,
)
from draftomen.pickengine import PickEngine, ScoredCard, ScoredPack
from draftomen.pool import DraftState, save_draft_state
from draftomen.seventeen import (
    QUICK_DRAFT_FORMAT,
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsData,
    SeventeenLandsFormatData,
    StructuralTargets,
)
from draftomen.set_profile import (
    CardPairSynergy,
    PairProfile,
    ProfileMaturity,
    SampleSummary,
    ScarcityTarget,
    SetProfile,
    SourceMetadata,
)

FIXTURE_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC).isoformat()
FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDEN_DIR = Path(__file__).parent / "golden"


def test_pair_selection_scores_pool_after_draft_and_reports_gap() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        pair_score_card_weight=1.0,
        pair_score_win_rate_weight=0.0,
    )

    selection = select_color_pair(
        pool_grp_ids=(1, 2, 3, 4, 5, 6),
        card_database=_card_database(),
        ratings_data=_ratings_data(),
        config=config,
    )

    assert selection.chosen.pair == "WU"
    assert selection.runner_up.pair != "WU"
    assert selection.score_gap > 0
    assert selection.chosen.playable_count == 4
    assert selection.chosen.playable_score_sum > selection.runner_up.playable_score_sum



def test_pair_selection_can_be_forced_without_hiding_automatic_best() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        pair_score_card_weight=1.0,
        pair_score_win_rate_weight=0.0,
    )

    selection = select_color_pair(
        pool_grp_ids=(1, 2, 3, 4, 5, 6),
        card_database=_card_database(),
        ratings_data=_ratings_data(),
        forced_pair="BR",
        config=config,
    )

    assert selection.chosen.pair == "BR"
    assert selection.automatic.pair == "WU"
    assert selection.score_gap < 0


def test_pair_selection_refuses_when_every_pair_has_zero_playables() -> None:
    database = CardDatabase(
        cards={
            91: _card(
                grp_id=91,
                name="Fixture Land",
                colors=(),
                mana_value=0.0,
                types=("Land",),
            ),
        }
    )

    with pytest.raises(DeckBuilderError) as error:
        select_color_pair(
            pool_grp_ids=(91,),
            card_database=database,
            ratings_data=_ratings_data(),
        )

    assert "no playable spells" in str(error.value)
    assert "automatic color pair" in str(error.value)


def test_build_deck_from_pool_rejects_all_unknown_card_metadata() -> None:
    pool = BuildPool(
        set_code="TST",
        pool_grp_ids=(9001, 9002, 9003),
        source_label="unknown metadata fixture",
    )

    with pytest.raises(DeckBuilderError) as error:
        build_deck_from_pool(
            pool=pool,
            card_database=CardDatabase(cards={}),
            ratings_data=_ratings_data(),
        )

    message = str(error.value)
    assert "Card metadata is missing for 3/3 picked cards" in message
    assert "The build cannot be trusted" in message
    assert "no deck was produced" in message
    assert "Unresolved grpIds: 9001, 9002, 9003" in message
    assert "automatic" not in message


def test_pair_win_rate_blend_uses_configured_weights() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        pair_score_card_weight=0.0,
        pair_score_win_rate_weight=1.0,
    )

    selection = select_color_pair(
        pool_grp_ids=(1, 2, 3, 4),
        card_database=_card_database(),
        ratings_data=_ratings_data(),
        config=config,
    )

    assert selection.chosen.pair == "BR"
    assert selection.chosen.pair_win_rate == 0.7



def test_pair_selection_is_deterministic_across_runs() -> None:
    config = replace(DECK_BUILDER, target_spell_count=4)
    kwargs = {
        "pool_grp_ids": (1, 2, 3, 4, 5, 6),
        "card_database": _card_database(),
        "ratings_data": _ratings_data(),
        "config": config,
    }

    first = select_color_pair(**kwargs)
    second = select_color_pair(**kwargs)

    assert first == second
    assert [score.pair for score in first.ranked_scores] == [
        score.pair for score in second.ranked_scores
    ]



def test_select_deck_spells_fills_exact_23_with_structural_constraints() -> None:
    selection = select_deck_spells(
        pool_grp_ids=_constrained_pool_ids(),
        card_database=_constrained_card_database(),
        pair="WU",
    )

    assert selection.counts.total == DECK_BUILDER.target_spell_count
    assert DECK_BUILDER.creature_floor <= selection.counts.creatures
    assert selection.counts.creatures <= DECK_BUILDER.creature_ceiling
    assert selection.counts.two_drops >= DECK_BUILDER.minimum_two_drops
    assert selection.counts.expensive <= DECK_BUILDER.maximum_expensive_spells
    assert selection.applied_relaxations == ()



def test_spell_counts_include_quantity_aware_creatures_and_instants() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=5,
        creature_floor=0,
        creature_ceiling=5,
        minimum_two_drops=0,
        maximum_expensive_spells=5,
        bench_card_count=0,
    )
    database = CardDatabase(
        cards={
            701: _card(
                grp_id=701,
                name="Creature // Instant",
                colors=("W",),
                types=("Creature // Instant",),
            ),
            702: _card(
                grp_id=702,
                name="Instant",
                colors=("U",),
                types=("Instant",),
            ),
            703: _card(
                grp_id=703,
                name="Creature",
                colors=("W",),
                types=("Creature",),
            ),
            704: _card(
                grp_id=704,
                name="Sorcery",
                colors=("U",),
                types=("Sorcery",),
            ),
        }
    )

    selection = select_deck_spells(
        pool_grp_ids=(701, 701, 702, 703, 704),
        card_database=database,
        pair="WU",
        config=config,
    )

    assert selection.counts.total == 5
    assert selection.counts.creatures == 3
    assert selection.counts.instants == 3


def test_spell_selection_ignores_scored_duplicates_beyond_pool_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
        bench_card_count=0,
    )
    original_score_pack = PickEngine.score_pack

    def duplicated_score_pack(self: PickEngine, **kwargs: Any) -> ScoredPack:
        scored_pack = original_score_pack(self, **kwargs)
        return replace(scored_pack, cards=(scored_pack.cards[0], *scored_pack.cards))

    monkeypatch.setattr(PickEngine, "score_pack", duplicated_score_pack)

    selection = select_deck_spells(
        pool_grp_ids=(1, 2, 3, 4),
        card_database=_card_database(),
        pair="WU",
        ratings_data=_ratings_data(),
        config=config,
    )

    selected_grp_ids = [spell.card.grp_id for spell in selection.spells]
    assert selected_grp_ids.count(1) == 1
    assert selection.eligible_count == 4
    assert selection.counts.total == 4



def test_spell_selection_allows_duplicate_cards_when_pool_has_multiple_copies() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=2,
        creature_floor=0,
        creature_ceiling=2,
        minimum_two_drops=0,
        maximum_expensive_spells=2,
        bench_card_count=0,
    )

    selection = select_deck_spells(
        pool_grp_ids=(1, 1, 2),
        card_database=_card_database(),
        pair="WU",
        ratings_data=_ratings_data(),
        config=config,
    )
    assert [spell.card.grp_id for spell in selection.spells] == [1, 1]


def test_spell_optimizer_compares_complete_packages_not_individual_scores() -> None:
    candidates = _optimizer_candidates()
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        optimizer_beam_width=64,
        optimizer_max_search_nodes=10_000,
        optimizer_max_evaluations=0,
        optimizer_local_improvement_rounds=0,
        optimizer_quality_weight=0.0,
        optimizer_curve_weight=1.0,
        optimizer_creature_structure_weight=0.0,
    )
    constraints = SpellConstraints(
        spell_count=4,
        creature_floor=2,
        creature_ceiling=2,
        minimum_two_drops=2,
        maximum_expensive_spells=4,
    )

    selected = _select_with_constraints(
        candidates=candidates,
        available_quantities=Counter(
            _card_quantity_key(card=card.card) for card in candidates
        ),
        pair="WU",
        constraints=constraints,
        config=config,
    )

    assert selected is not None
    assert {card.card.grp_id for card in selected} == {803, 804, 805, 806}


def test_spell_optimizer_is_deterministic_for_same_candidates_and_config() -> None:
    candidates = _optimizer_candidates()
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        optimizer_beam_width=8,
        optimizer_max_search_nodes=256,
        optimizer_max_evaluations=32,
        optimizer_local_improvement_rounds=1,
        optimizer_local_improvement_candidates=2,
    )
    constraints = SpellConstraints(
        spell_count=4,
        creature_floor=1,
        creature_ceiling=3,
        minimum_two_drops=1,
        maximum_expensive_spells=3,
    )
    available_quantities = Counter(
        _card_quantity_key(card=card.card) for card in candidates
    )

    first = _select_with_constraints(
        candidates=candidates,
        available_quantities=available_quantities,
        pair="WU",
        constraints=constraints,
        config=config,
    )
    second = _select_with_constraints(
        candidates=candidates,
        available_quantities=available_quantities,
        pair="WU",
        constraints=constraints,
        config=config,
    )

    assert first == second


def test_hob_local_classifier_roles_support_complete_package() -> None:
    candidates, config, constraints, available_quantities, profile = _hob_optimizer_setup(
        spell_count=4
    )
    baseline = _greedy_feasible_package(
        candidates=candidates,
        available_quantities=available_quantities,
        pair="UR",
        constraints=constraints,
        config=config,
    )
    selected = _select_with_constraints(
        candidates=candidates,
        available_quantities=available_quantities,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=profile,
    )
    unclassified_candidates = tuple(
        replace(card, card=replace(card.card, oracle_text=None))
        for card in candidates
    )
    unclassified_selection = _select_with_constraints(
        candidates=unclassified_candidates,
        available_quantities=available_quantities,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=profile,
    )

    assert baseline is not None
    assert selected is not None
    assert unclassified_selection is not None
    baseline_names = {card.card.name for card in baseline}
    selected_names = {card.card.name for card in selected}
    unclassified_names = {card.card.name for card in unclassified_selection}
    assert "Patient Instructor" not in baseline_names
    assert "Patient Instructor" in selected_names
    assert "Patient Instructor" not in unclassified_names
    assert "Master's Councillor" in selected_names


def test_legacy_profile_roles_do_not_change_optimizer_package_selection() -> None:
    candidates, config, constraints, available_quantities, profile = _hob_optimizer_setup(
        spell_count=4,
    )
    legacy_profile = _legacy_profile_with_role(
        profile=profile,
        card_key="arena_id:1902",
        card_name="Patient Instructor",
        role="go_wide_enabler",
    )
    clean_selection = _select_with_constraints(
        candidates=candidates,
        available_quantities=available_quantities,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=profile,
    )
    legacy_selection = _select_with_constraints(
        candidates=candidates,
        available_quantities=available_quantities,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=legacy_profile,
    )

    assert clean_selection is not None
    assert legacy_selection is not None
    assert Counter(card.card.grp_id for card in clean_selection) == Counter(
        card.card.grp_id for card in legacy_selection
    )
    assert "Patient Instructor" in {card.card.name for card in clean_selection}
    assert "Patient Instructor" in {card.card.name for card in legacy_selection}


def test_hob_local_classifier_does_not_force_weak_redundant_enabler() -> None:
    candidates, config, constraints, available_quantities, profile = _hob_optimizer_setup(
        spell_count=5
    )
    assert any(card.card.name == "Weak Master's Councillor" for card in candidates)
    selected = _select_with_constraints(
        candidates=candidates,
        available_quantities=available_quantities,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=profile,
    )

    assert selected is not None
    selected_names = {card.card.name for card in selected}
    assert "Weak Master's Councillor" not in selected_names
    assert "Patient Instructor" in selected_names


def test_empirical_pair_and_scarcity_evidence_reorders_complete_packages() -> None:
    candidates = _hob_optimizer_candidates()
    masters = tuple(
        card for card in candidates if card.card.name == "Master's Councillor"
    )
    patient = next(card for card in candidates if card.card.name == "Patient Instructor")
    filler_one = next(card for card in candidates if card.card.name == "Generic Filler One")
    filler_two = next(card for card in candidates if card.card.name == "Generic Filler Two")
    generic_package = (masters[0], masters[1], filler_one, filler_two)
    empirical_package = (masters[0], masters[1], patient, filler_one)
    constraints = SpellConstraints(
        spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
    )
    config = replace(
        DECK_BUILDER,
        optimizer_quality_weight=1.0,
        optimizer_curve_weight=0.0,
        optimizer_creature_structure_weight=0.0,
        optimizer_role_target_weight=0.0,
        optimizer_effective_removal_weight=0.0,
        optimizer_enabler_payoff_weight=0.0,
        optimizer_semantic_synergy_weight=0.1,
        optimizer_empirical_context_weight=0.2,
        optimizer_redundancy_weight=0.0,
        optimizer_unsupported_payoff_weight=0.0,
        optimizer_mana_strain_weight=0.0,
    )
    empirical_profile = _empirical_pair_profile(
        synergy=CardPairSynergy(
            first_card="arena_id:1901",
            second_card="arena_id:1902",
            value=2.0,
        ),
        scarcity=ScarcityTarget(card_key="arena_id:1902", value=1.0),
    )
    generic_score = _optimizer_objective(
        cards=generic_package,
        pair="UR",
        constraints=constraints,
        config=config,
    )
    generic_empirical_score = _optimizer_objective(
        cards=empirical_package,
        pair="UR",
        constraints=constraints,
        config=config,
    )
    profile_score = _optimizer_objective(
        cards=generic_package,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=empirical_profile,
    )
    profile_empirical_score = _optimizer_objective(
        cards=empirical_package,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=empirical_profile,
    )

    assert generic_score > generic_empirical_score
    assert profile_empirical_score > profile_score


def test_optimizer_negative_pair_synergy_lowers_bounded_objective() -> None:
    candidates = _hob_optimizer_candidates()
    masters = tuple(
        card for card in candidates if card.card.name == "Master's Councillor"
    )
    patient = next(card for card in candidates if card.card.name == "Patient Instructor")
    filler_one = next(card for card in candidates if card.card.name == "Generic Filler One")
    filler_two = next(card for card in candidates if card.card.name == "Generic Filler Two")
    filler_three = next(card for card in candidates if card.card.name == "Generic Filler Three")
    penalized_package = (masters[0], patient, filler_one, filler_two)
    neutral_package = (masters[0], filler_one, filler_two, filler_three)
    profile = _empirical_pair_profile(
        synergy=CardPairSynergy(
            first_card="arena_id:1901",
            second_card="arena_id:1902",
            value=-2.0,
            samples=100,
        ),
    )

    penalized_score = _optimizer_objective(
        cards=penalized_package,
        pair="UR",
        constraints=_unconstrained_four_spell_constraints(),
        config=_empirical_only_optimizer_config(),
        set_profile=profile,
    )
    neutral_score = _optimizer_objective(
        cards=neutral_package,
        pair="UR",
        constraints=_unconstrained_four_spell_constraints(),
        config=_empirical_only_optimizer_config(),
        set_profile=profile,
    )

    assert penalized_score < neutral_score


def test_optimizer_zero_samples_have_no_empirical_effect() -> None:
    candidates = _hob_optimizer_candidates()
    masters = tuple(
        card for card in candidates if card.card.name == "Master's Councillor"
    )
    patient = next(card for card in candidates if card.card.name == "Patient Instructor")
    filler_one = next(card for card in candidates if card.card.name == "Generic Filler One")
    filler_two = next(card for card in candidates if card.card.name == "Generic Filler Two")
    package = (masters[0], patient, filler_one, filler_two)
    zero_profile = _empirical_pair_profile(
        synergy=CardPairSynergy(
            first_card="arena_id:1901",
            second_card="arena_id:1902",
            value=2.0,
            samples=0,
        ),
        scarcity=ScarcityTarget(
            card_key="arena_id:1902",
            value=1.0,
            samples=0,
        ),
    )
    supported_profile = _empirical_pair_profile(
        synergy=CardPairSynergy(
            first_card="arena_id:1901",
            second_card="arena_id:1902",
            value=2.0,
        ),
        scarcity=ScarcityTarget(card_key="arena_id:1902", value=1.0),
    )
    constraints = _unconstrained_four_spell_constraints()
    config = _empirical_only_optimizer_config()

    generic_score = _optimizer_objective(
        cards=package,
        pair="UR",
        constraints=constraints,
        config=config,
    )
    zero_score = _optimizer_objective(
        cards=package,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=zero_profile,
    )
    supported_score = _optimizer_objective(
        cards=package,
        pair="UR",
        constraints=constraints,
        config=config,
        set_profile=supported_profile,
    )

    assert zero_score == generic_score
    assert supported_score > generic_score


def test_optimizer_resolves_oracle_id_for_empirical_card_evidence() -> None:
    candidates = _hob_optimizer_candidates()
    master = next(card for card in candidates if card.card.name == "Master's Councillor")
    patient = next(card for card in candidates if card.card.name == "Patient Instructor")
    oracle_master = replace(
        master,
        card=replace(master.card, oracle_id="Oracle-Master"),
    )
    oracle_patient = replace(
        patient,
        card=replace(patient.card, oracle_id="Oracle-Patient"),
    )
    profile = _empirical_pair_profile(
        synergy=CardPairSynergy(
            first_card="oracle_id:oracle-master",
            second_card="oracle_id:oracle-patient",
            value=1.0,
            samples=100,
        ),
        scarcity=ScarcityTarget(
            card_key="oracle_id:oracle-patient",
            value=1.0,
            samples=100,
        ),
    )

    context = _build_optimizer_context(
        candidates=(oracle_master, oracle_patient),
        pair="UR",
        set_profile=profile,
    )

    assert "oracle_id:oracle-master" in _optimizer_card_keys(card=oracle_master.card)
    assert "oracle_id:oracle-patient" in _optimizer_card_keys(card=oracle_patient.card)
    assert context.synergy_lookup[
        ("oracle_id:oracle-master", "oracle_id:oracle-patient")
    ] == pytest.approx(0.5)
    assert context.card_evidence[1].scarcity == pytest.approx(0.5)


def _empirical_only_optimizer_config() -> DeckBuilderConfig:
    return replace(
        DECK_BUILDER,
        optimizer_quality_weight=0.0,
        optimizer_curve_weight=0.0,
        optimizer_creature_structure_weight=0.0,
        optimizer_role_target_weight=0.0,
        optimizer_effective_removal_weight=0.0,
        optimizer_enabler_payoff_weight=0.0,
        optimizer_semantic_synergy_weight=0.0,
        optimizer_empirical_context_weight=1.0,
        optimizer_redundancy_weight=0.0,
        optimizer_unsupported_payoff_weight=0.0,
        optimizer_mana_strain_weight=0.0,
    )


def _unconstrained_four_spell_constraints() -> SpellConstraints:
    return SpellConstraints(
        spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
    )


def _empirical_pair_profile(
    *,
    synergy: CardPairSynergy | None = None,
    scarcity: ScarcityTarget | None = None,
) -> SetProfile:
    return SetProfile(
        set_code="HOB",
        event_format="quickdraft",
        profile_version="hob-empirical-behavior-test",
        generated_at=FIXTURE_NOW,
        source=SourceMetadata(provider="test"),
        maturity=ProfileMaturity.MATURE,
        samples=SampleSummary(total=100, by_pair=(("UR", 100),)),
        confidence=1.0,
        pairs=(
            PairProfile(
                pair="UR",
                synergy=() if synergy is None else (synergy,),
                scarcity=() if scarcity is None else (scarcity,),
            ),
        ),
    )


def _legacy_profile_with_role(
    *,
    profile: SetProfile,
    card_key: str,
    card_name: str,
    role: str,
) -> SetProfile:
    payload = profile.to_json()
    payload["schema_version"] = 3
    payload["enhancement_status"] = "not-enhanced"
    payload["role_profile"] = {
        "cards": [
            {
                "card_name": card_name,
                "key": card_key,
                "roles": [
                    {
                        "confidence": 1.0,
                        "evidence": ["oracle_text"],
                        "provenance": ["historical"],
                        "role": role,
                    }
                ],
            }
        ],
        "classifier_version": "1.5",
        "profile_schema_version": 2,
        "role_schema_version": 6,
        "schema_version": 2,
        "set_code": profile.set_code,
    }
    return SetProfile.from_json(payload)


def test_optimizer_requires_generic_term_even_when_profile_weights_are_nonzero() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        optimizer_quality_weight=0.0,
        optimizer_curve_weight=0.0,
        optimizer_creature_structure_weight=0.0,
    )

    with pytest.raises(DeckBuilderError, match="generic quality"):
        select_deck_spells(
            pool_grp_ids=(1, 2, 3, 4),
            card_database=_card_database(),
            pair="WU",
            config=config,
        )


def test_optimizer_no_profile_is_exactly_generic_quality_curve_and_structure() -> None:
    cards = _optimizer_candidates()[:4]
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
    )
    constraints = SpellConstraints(
        spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
    )
    counts = SpellCounts(
        total=4,
        creatures=sum("Creature" in card.card.types[0] for card in cards),
        two_drops=sum(card.card.mana_value == 2.0 for card in cards),
        expensive=sum((card.card.mana_value or 0.0) >= 6.0 for card in cards),
    )
    raw_score_sum = sum(card.raw_score for card in cards)
    mana_value_sum = sum(card.card.mana_value or 0.0 for card in cards)
    expected = (
        config.optimizer_quality_weight * (raw_score_sum / counts.total)
        + config.optimizer_curve_weight
        * _optimizer_curve_score(
            counts=counts,
            mana_value_sum=mana_value_sum,
            constraints=constraints,
            config=config,
        )
        + config.optimizer_creature_structure_weight
        * _optimizer_creature_score(
            creature_count=counts.creatures,
            constraints=constraints,
        )
    )

    assert _optimizer_objective(
        cards=cards,
        pair="WU",
        constraints=constraints,
        config=config,
    ) == expected


def test_spell_optimizer_never_exceeds_available_card_quantities() -> None:
    candidates = _optimizer_candidates()
    duplicate = replace(candidates[0], original_index=100)
    candidates = (candidates[0], duplicate, *candidates[1:])
    available_quantities = Counter(
        {
            _card_quantity_key(card=candidates[0].card): 1,
            _card_quantity_key(card=candidates[2].card): 1,
        }
    )
    constraints = SpellConstraints(
        spell_count=2,
        creature_floor=0,
        creature_ceiling=2,
        minimum_two_drops=0,
        maximum_expensive_spells=2,
    )
    config = replace(
        DECK_BUILDER,
        target_spell_count=2,
        optimizer_beam_width=8,
        optimizer_max_search_nodes=64,
        optimizer_max_evaluations=0,
        optimizer_local_improvement_rounds=0,
    )

    selected = _select_with_constraints(
        candidates=candidates,
        available_quantities=available_quantities,
        pair="WU",
        constraints=constraints,
        config=config,
    )

    assert selected is not None
    selected_quantities = Counter(
        _card_quantity_key(card=card.card) for card in selected
    )
    assert all(
        count <= available_quantities[quantity_key]
        for quantity_key, count in selected_quantities.items()
    )


def test_optimizer_profile_weights_require_finite_non_negative_values() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        optimizer_semantic_synergy_weight=float("nan"),
    )

    with pytest.raises(DeckBuilderError, match="finite and non-negative"):
        select_deck_spells(
            pool_grp_ids=(1, 2, 3, 4),
            card_database=_card_database(),
            pair="WU",
            config=config,
        )


def test_spell_optimizer_uses_exact_spell_constraints() -> None:
    selection = select_deck_spells(
        pool_grp_ids=_constrained_pool_ids(),
        card_database=_constrained_card_database(),
        pair="WU",
    )

    assert selection.constraints == SpellConstraints(
        spell_count=23,
        creature_floor=14,
        creature_ceiling=17,
        minimum_two_drops=5,
        maximum_expensive_spells=3,
        maximum_splash_spells=0,
    )
    assert selection.counts.total == selection.constraints.spell_count


def test_spell_optimizer_without_set_profile_uses_generic_fallback() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
        bench_card_count=0,
    )

    selection = select_deck_spells(
        pool_grp_ids=(1, 2, 3, 4),
        card_database=_card_database(),
        pair="WU",
        ratings_data=_ratings_data(),
        config=config,
    )

    assert selection.counts.total == 4
    assert selection.spells


def test_spell_optimizer_respects_configured_bounds_and_falls_back_feasibly() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
        optimizer_beam_width=1,
        optimizer_max_search_nodes=1,
        optimizer_max_evaluations=0,
        optimizer_local_improvement_rounds=0,
        bench_card_count=0,
    )

    selection = select_deck_spells(
        pool_grp_ids=(1, 2, 3, 4),
        card_database=_card_database(),
        pair="WU",
        ratings_data=_ratings_data(),
        config=config,
    )

    assert selection.counts.total == 4
    assert selection.counts.total == selection.constraints.spell_count


def test_bounded_optimizer_build_preserves_deck_size_lands_and_pips() -> None:
    database = _pip_card_database(white_pips=20, blue_pips=3)
    config = replace(DECK_BUILDER, optimizer_max_search_nodes=512)

    build_sheet = select_build_sheet(
        pool_grp_ids=tuple(range(501, 524)),
        card_database=database,
        pair="WU",
        config=config,
    )

    assert build_sheet.mana_base.total_cards == config.deck_size
    assert (
        build_sheet.mana_base.spell_count + build_sheet.mana_base.land_count
        == config.deck_size
    )
    assert build_sheet.mana_base.land_count == config.default_land_count
    assert dict(build_sheet.mana_base.pip_counts) == {"W": 20, "U": 3}





def test_build_sheet_reselects_aggressive_spell_count_to_keep_40_cards() -> None:
    database = _curve_card_database(mana_value=2.0, count=30)

    build_sheet = select_build_sheet(
        pool_grp_ids=tuple(range(401, 431)),
        card_database=database,
        pair="WU",
    )

    assert build_sheet.spell_selection.counts.total == 24
    assert build_sheet.mana_base.land_count == DECK_BUILDER.aggressive_land_count
    assert build_sheet.mana_base.total_cards == DECK_BUILDER.deck_size



def test_build_sheet_reselects_top_heavy_spell_count_to_keep_40_cards() -> None:
    database = _curve_card_database(mana_value=4.0, count=30)

    build_sheet = select_build_sheet(
        pool_grp_ids=tuple(range(401, 431)),
        card_database=database,
        pair="WU",
    )

    assert build_sheet.spell_selection.counts.total == 22
    assert build_sheet.mana_base.land_count == DECK_BUILDER.top_heavy_land_count
    assert build_sheet.mana_base.total_cards == DECK_BUILDER.deck_size



def test_mana_base_splits_basics_by_pips_with_source_floor() -> None:
    database = _pip_card_database(white_pips=20, blue_pips=3)
    spell_selection = select_deck_spells(
        pool_grp_ids=tuple(range(501, 524)),
        card_database=database,
        pair="WU",
    )

    mana_base = select_mana_base(
        pool_grp_ids=tuple(range(501, 524)),
        card_database=database,
        pair="WU",
        spell_selection=spell_selection,
    )

    assert _basic_count(mana_base=mana_base, name="Plains") == 10
    assert _basic_count(mana_base=mana_base, name="Island") == 7
    assert dict(mana_base.source_counts) == {"W": 10, "U": 7}



def test_mana_base_rounds_toward_double_pip_heavy_color() -> None:
    database = _rounding_card_database()
    spell_selection = select_deck_spells(
        pool_grp_ids=tuple(range(601, 624)),
        card_database=database,
        pair="WU",
    )

    mana_base = select_mana_base(
        pool_grp_ids=tuple(range(601, 624)),
        card_database=database,
        pair="WU",
        spell_selection=spell_selection,
    )

    assert dict(mana_base.pip_counts) == {"W": 9, "U": 9}
    assert dict(mana_base.double_pip_counts) == {"W": 0, "U": 1}
    assert _basic_count(mana_base=mana_base, name="Plains") == 8
    assert _basic_count(mana_base=mana_base, name="Island") == 9



def test_mana_base_slots_in_pair_nonbasics_before_basics() -> None:
    database = _pip_card_database(white_pips=12, blue_pips=12)
    database.cards[524] = _card(
        grp_id=524,
        name="Tranquil Cove",
        colors=(),
        mana_value=0.0,
        types=("Land",),
        produced_mana=("W", "U"),
    )
    spell_selection = select_deck_spells(
        pool_grp_ids=tuple(range(501, 524)) + (524,),
        card_database=database,
        pair="WU",
    )

    mana_base = select_mana_base(
        pool_grp_ids=tuple(range(501, 524)) + (524,),
        card_database=database,
        pair="WU",
        spell_selection=spell_selection,
    )

    assert [land.card.name for land in mana_base.nonbasic_lands] == ["Tranquil Cove"]
    assert sum(basic.count for basic in mana_base.basic_lands) == 16
    assert dict(mana_base.source_counts) == {"W": 9, "U": 9}



def test_build_sheet_uses_structure_targets_and_prints_similarity() -> None:
    config = replace(
        DECK_BUILDER,
        minimum_two_drops=0,
        maximum_expensive_spells=10,
        bench_card_count=0,
    )
    pool = BuildPool(
        set_code="TST",
        pool_grp_ids=tuple(range(701, 731)),
        source_label="structure target test",
    )
    ratings_data = _ratings_data_with_structure_targets(
        _structure_targets(
            pair="WU",
            average_creature_count=10.0,
            average_land_count=18.0,
            average_two_drop_count=0.0,
            average_expensive_spell_count=0.0,
            sample_size=3,
        )
    )
    selection, build_sheet = build_deck_from_pool(
        pool=pool,
        card_database=_targeted_card_database(),
        ratings_data=ratings_data,
        config=config,
    )

    output = format_build_result(
        pool=pool,
        selection=selection,
        spell_selection=build_sheet.spell_selection,
        mana_base=build_sheet.mana_base,
        config=config,
    )

    assert build_sheet.spell_selection.counts.total == 22
    assert build_sheet.spell_selection.counts.creatures == 10
    assert build_sheet.spell_selection.constraints.creature_floor == 10
    assert build_sheet.spell_selection.constraints.creature_ceiling == 10
    assert build_sheet.mana_base.land_count == 18
    assert "Similarity: 17Lands trophy WU decks in TST (n=3)" in output
    assert "avg 10.0 creatures / 18.0 lands; your build: 10 / 18" in output



def test_allow_splash_selects_at_most_two_elite_off_pair_cards_with_fixing() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
        bench_card_count=3,
    )
    pool_ids = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)

    no_splash = select_deck_spells(
        pool_grp_ids=pool_ids,
        card_database=_splash_card_database(fixing_count=3),
        pair="WU",
        ratings_data=_splash_ratings_data(),
        allow_splash=False,
        config=config,
    )
    splash = select_deck_spells(
        pool_grp_ids=pool_ids,
        card_database=_splash_card_database(fixing_count=3),
        pair="WU",
        ratings_data=_splash_ratings_data(),
        allow_splash=True,
        config=config,
    )

    assert no_splash.counts.splashes == 0
    assert no_splash.eligible_count == 4
    assert splash.splash_color == "R"
    assert splash.splash_fixing_sources == 3
    assert splash.splash_planned_basic_sources == 1
    assert splash.counts.splashes == 2
    assert [card.card.grp_id for card in splash.spells[:2]] == [5, 6]
    assert 7 not in {card.card.grp_id for card in splash.spells}



def test_allow_splash_caps_target_when_splash_limit_makes_pool_short() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=8,
        creature_floor=0,
        creature_ceiling=8,
        minimum_two_drops=0,
        maximum_expensive_spells=8,
        bench_card_count=0,
    )

    selection = select_deck_spells(
        pool_grp_ids=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10),
        card_database=_splash_card_database(fixing_count=3),
        pair="WU",
        ratings_data=_splash_ratings_data(),
        allow_splash=True,
        config=config,
    )

    assert selection.counts.total == 6
    assert selection.counts.splashes == 2
    assert "eligible-card shortage" in selection.applied_relaxations



def test_allow_splash_requires_two_fixing_sources() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
        bench_card_count=0,
    )

    selection = select_deck_spells(
        pool_grp_ids=(1, 2, 3, 4, 5, 8),
        card_database=_splash_card_database(fixing_count=1),
        pair="WU",
        ratings_data=_splash_ratings_data(),
        allow_splash=True,
        config=config,
    )

    assert selection.splash_fixing_sources == 1
    assert selection.counts.splashes == 0
    assert selection.eligible_count == 4


def test_single_splash_card_uses_two_fixing_lands_and_one_planned_basic() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=4,
        creature_floor=0,
        creature_ceiling=4,
        minimum_two_drops=0,
        maximum_expensive_spells=4,
        bench_card_count=0,
    )
    pool_ids = (1, 2, 3, 4, 5, 8, 9)
    database = _splash_card_database(fixing_count=2)

    selection = select_deck_spells(
        pool_grp_ids=pool_ids,
        card_database=database,
        pair="WU",
        ratings_data=_splash_ratings_data(),
        allow_splash=True,
        config=config,
    )
    mana_base = select_mana_base(
        pool_grp_ids=pool_ids,
        card_database=database,
        pair="WU",
        spell_selection=selection,
        config=config,
    )

    assert selection.counts.splashes == 1
    assert selection.constraints.maximum_splash_spells == 1
    assert _basic_count(mana_base=mana_base, name="Mountain") == 1
    assert dict(mana_base.source_counts)["R"] == 3



def test_near_tie_creature_preference_applies_while_floor_unmet() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=2,
        creature_floor=1,
        creature_ceiling=2,
        minimum_two_drops=0,
        maximum_expensive_spells=2,
        bench_card_count=0,
    )
    database = CardDatabase(
        cards={
            31: _card(
                grp_id=31,
                name="Slightly Better Trick",
                colors=("W",),
                mana_value=3.0,
                types=("Instant",),
            ),
            32: _card(
                grp_id=32,
                name="Near Tie Creature",
                colors=("W",),
                mana_value=3.0,
                types=("Creature — Soldier",),
            ),
            33: _card(
                grp_id=33,
                name="Distant Trick",
                colors=("U",),
                mana_value=3.0,
                types=("Instant",),
            ),
        }
    )
    ratings_data = _ratings_data_from_entries(
        entries=(
            (31, "Slightly Better Trick", "W", 0.560),
            (32, "Near Tie Creature", "W", 0.559),
            (33, "Distant Trick", "U", 0.500),
        )
    )

    selection = select_deck_spells(
        pool_grp_ids=(31, 32, 33),
        card_database=database,
        pair="WU",
        ratings_data=ratings_data,
        config=config,
    )

    assert [spell.card.grp_id for spell in selection.spells] == [32, 31]



def test_spell_selection_lookahead_preserves_overlapping_quotas() -> None:
    config = replace(
        DECK_BUILDER,
        target_spell_count=5,
        creature_floor=2,
        creature_ceiling=2,
        minimum_two_drops=2,
        maximum_expensive_spells=5,
        bench_card_count=0,
    )
    database = CardDatabase(
        cards={
            301: _card(
                grp_id=301,
                name="Three-Drop Creature A",
                colors=("W",),
                mana_value=3.0,
            ),
            302: _card(
                grp_id=302,
                name="Three-Drop Creature B",
                colors=("U",),
                mana_value=3.0,
            ),
            303: _card(
                grp_id=303,
                name="Two-Drop Creature A",
                colors=("W",),
                mana_value=2.0,
            ),
            304: _card(
                grp_id=304,
                name="Two-Drop Creature B",
                colors=("U",),
                mana_value=2.0,
            ),
            305: _card(
                grp_id=305,
                name="Noncreature A",
                colors=("W",),
                mana_value=3.0,
                types=("Instant",),
            ),
            306: _card(
                grp_id=306,
                name="Noncreature B",
                colors=("U",),
                mana_value=3.0,
                types=("Instant",),
            ),
            307: _card(
                grp_id=307,
                name="Noncreature C",
                colors=("W",),
                mana_value=3.0,
                types=("Instant",),
            ),
        }
    )

    selection = select_deck_spells(
        pool_grp_ids=(301, 302, 303, 304, 305, 306, 307),
        card_database=database,
        pair="WU",
        config=config,
    )

    assert [spell.card.grp_id for spell in selection.spells[:2]] == [303, 304]
    assert selection.counts.creatures == 2
    assert selection.counts.two_drops == 2
    assert selection.applied_relaxations == ()



def test_spell_selection_reports_relaxed_two_drop_quota_when_pool_is_short() -> None:
    config = replace(DECK_BUILDER, bench_card_count=0)
    pool_ids = tuple(range(201, 224))
    database = CardDatabase(
        cards={
            grp_id: _card(
                grp_id=grp_id,
                name=f"Fixture Spell {grp_id}",
                colors=("W",) if grp_id % 2 == 0 else ("U",),
                mana_value=2.0 if grp_id < 204 else 3.0,
                types=("Creature — Fixture",) if grp_id < 216 else ("Instant",),
            )
            for grp_id in pool_ids
        }
    )

    selection = select_deck_spells(
        pool_grp_ids=pool_ids,
        card_database=database,
        pair="WU",
        config=config,
    )

    assert selection.counts.total == DECK_BUILDER.target_spell_count
    assert selection.counts.two_drops == 3
    assert "minimum two-drop quota" in selection.applied_relaxations



def test_format_build_result_reports_spell_selection_and_attribution(tmp_path: Path) -> None:
    config = replace(DECK_BUILDER, target_spell_count=4)
    pool = load_pool_file(path=_write_pool_file(tmp_path), set_code="TST")
    selection = select_color_pair(
        pool_grp_ids=pool.pool_grp_ids,
        card_database=_card_database(),
        ratings_data=_ratings_data(),
        config=config,
    )
    build_sheet = select_build_sheet(
        pool_grp_ids=pool.pool_grp_ids,
        card_database=_card_database(),
        pair=selection.chosen.pair,
        ratings_data=_ratings_data(),
        config=config,
    )

    output = format_build_result(
        pool=pool,
        selection=selection,
        spell_selection=build_sheet.spell_selection,
        mana_base=build_sheet.mana_base,
        config=config,
    )

    assert output.startswith("Suggested deck\n")
    assert "Set: TST" in output
    assert "Average mana value:" in output
    assert "Mana curve: 0:" in output
    assert "Chosen pair:" in output
    assert "Runner-up:" in output
    assert "Strength gap:" in output
    assert "Pair strengths:" in output
    assert "Deck summary:" in output
    assert "Deck size: 40 cards" in output
    assert "Selected spells:" in output
    assert "Structure checks:" in output
    assert "Selected spells: 4/23" in output
    assert "Lands:" in output
    assert "Splash: enabled;" in output
    assert "Card data from 17Lands" in output



def test_constrained_build_output_matches_golden_fixture() -> None:
    pool = load_pool_file(path=Path("tests/fixtures/deckbuilder-constrained-pool.json"))
    database = build_card_database_from_bulk_file(
        path=FIXTURES_DIR / "deckbuilder-constrained-bulk.jsonl"
    )
    selection, build_sheet = build_deck_from_pool(
        pool=pool,
        card_database=database,
    )

    output = format_build_result(
        pool=pool,
        selection=selection,
        spell_selection=build_sheet.spell_selection,
        mana_base=build_sheet.mana_base,
    )

    assert output == (GOLDEN_DIR / "deckbuilder-constrained-build.txt").read_text(
        encoding="utf-8"
    )



def test_load_pool_file_supports_state_json_shape(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    path.write_text(
        '{"set_code":"tst","account_id":"acct","draft_id":"draft",'
        '"pool_grp_ids":[1,2,3]}',
        encoding="utf-8",
    )

    pool = load_pool_file(path=path)

    assert pool.set_code == "TST"
    assert pool.pool_grp_ids == (1, 2, 3)
    assert pool.account_id == "acct"
    assert pool.draft_id == "draft"



def test_load_pool_file_requires_set_code_for_simple_lists(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(DeckBuilderError, match="--set-code"):
        load_pool_file(path=path)

    pool = load_pool_file(path=path, set_code="tst")
    assert pool.set_code == "TST"
    assert pool.pool_grp_ids == (1, 2, 3)



def test_load_persisted_pool_defaults_to_latest_and_filters(tmp_path: Path) -> None:
    older = _draft_state(
        account_id="acct-a",
        draft_id="draft-a",
        updated_at="2026-07-03T10:00:00+00:00",
    )
    newer = _draft_state(
        account_id="acct-b",
        draft_id="draft-b",
        updated_at="2026-07-03T11:00:00+00:00",
    )
    save_draft_state(state=older, app_dir=tmp_path)
    save_draft_state(state=newer, app_dir=tmp_path)

    latest = load_persisted_pool(app_dir=tmp_path)
    filtered = load_persisted_pool(app_dir=tmp_path, account_id="acct-a")

    assert latest.account_id == "acct-b"
    assert latest.draft_id == "draft-b"
    assert filtered.account_id == "acct-a"
    assert filtered.draft_id == "draft-a"



def _write_pool_file(directory: Path) -> Path:
    path = directory / ".deckbuilder-test-pool.json"
    path.write_text("[1, 2, 3, 4, 5, 6]", encoding="utf-8")
    return path



def _ratings_data_with_structure_targets(
    target: StructuralTargets,
) -> SeventeenLandsData:
    data = _ratings_data()
    return SeventeenLandsData(
        set_code=data.set_code,
        requested_format=data.requested_format,
        primary=data.primary,
        fallback=data.fallback,
        structure_targets={target.pair: target},
        thin_sample_minimum=data.thin_sample_minimum,
    )



def _structure_targets(
    *,
    pair: str,
    average_creature_count: float,
    average_land_count: float,
    average_two_drop_count: float,
    average_expensive_spell_count: float,
    sample_size: int,
) -> StructuralTargets:
    return StructuralTargets(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        pair=pair,
        sample_size=sample_size,
        average_creature_count=average_creature_count,
        average_land_count=average_land_count,
        average_spell_count=40.0 - average_land_count,
        average_two_drop_count=average_two_drop_count,
        average_expensive_spell_count=average_expensive_spell_count,
        average_curve=(
            ("0-1", 0.0),
            ("2", average_two_drop_count),
            ("3", 40.0 - average_land_count - average_two_drop_count),
            ("4", 0.0),
            ("5", 0.0),
            ("6+", average_expensive_spell_count),
        ),
        source="17lands-public-draft-data",
        source_url="https://17lands-public.example/draft.csv.gz",
        computed_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
    )



def _targeted_card_database() -> CardDatabase:
    cards: dict[int, CardInfo] = {}
    for offset in range(30):
        grp_id = 701 + offset
        cards[grp_id] = _card(
            grp_id=grp_id,
            name=f"Target Fixture {grp_id}",
            colors=("W",) if offset % 2 == 0 else ("U",),
            mana_value=3.0,
            types=("Creature — Fixture",) if offset < 10 else ("Instant",),
            mana_cost="{W}" if offset % 2 == 0 else "{U}",
        )

    return CardDatabase(cards=cards)



def _splash_card_database(*, fixing_count: int) -> CardDatabase:
    cards = {
        1: _card(grp_id=1, name="White Playable", colors=("W",)),
        2: _card(grp_id=2, name="Blue Playable", colors=("U",)),
        3: _card(grp_id=3, name="Second White Playable", colors=("W",)),
        4: _card(grp_id=4, name="Second Blue Playable", colors=("U",)),
        5: _card(
            grp_id=5,
            name="Red Bomb One",
            colors=("R",),
            mana_cost="{4}{R}",
        ),
        6: _card(
            grp_id=6,
            name="Red Bomb Two",
            colors=("R",),
            mana_cost="{3}{R}",
        ),
        7: _card(
            grp_id=7,
            name="Double Red Bomb",
            colors=("R",),
            mana_cost="{3}{R}{R}",
        ),
    }
    if fixing_count >= 1:
        cards[8] = _card(
            grp_id=8,
            name="Red Fixing Land One",
            colors=(),
            mana_value=0.0,
            types=("Land",),
            produced_mana=("R",),
        )

    if fixing_count >= 2:
        cards[9] = _card(
            grp_id=9,
            name="Red Fixing Land Two",
            colors=(),
            mana_value=0.0,
            types=("Land",),
            produced_mana=("R",),
        )

    if fixing_count >= 3:
        cards[10] = _card(
            grp_id=10,
            name="Red Fixing Land Three",
            colors=(),
            mana_value=0.0,
            types=("Land",),
            produced_mana=("R",),
        )

    return CardDatabase(cards=cards)



def _splash_ratings_data() -> SeventeenLandsData:
    return _ratings_data_from_entries(
        entries=(
            (1, "White Playable", "W", 0.55),
            (2, "Blue Playable", "U", 0.55),
            (3, "Second White Playable", "W", 0.54),
            (4, "Second Blue Playable", "U", 0.54),
            (5, "Red Bomb One", "R", 0.70),
            (6, "Red Bomb Two", "R", 0.69),
            (7, "Red Bomb Three", "R", 0.68),
            *tuple(
                (
                    grp_id,
                    f"Distribution Card {grp_id}",
                    "B",
                    0.50 + ((grp_id - 100) * 0.002),
                )
                for grp_id in range(100, 120)
            ),
        )
    )



def _ratings_data() -> SeventeenLandsData:
    return _ratings_data_from_entries(
        entries=(
            (1, "White Bomb", "W", 0.65),
            (2, "Blue Bomb", "U", 0.64),
            (3, "White Playable", "W", 0.62),
            (4, "Blue Playable", "U", 0.61),
            (5, "Red Filler", "R", 0.52),
            (6, "Black Filler", "B", 0.51),
        )
    )




def _ratings_data_from_entries(
    *,
    entries: tuple[tuple[int, str, str, float], ...],
) -> SeventeenLandsData:
    return SeventeenLandsData(
        set_code="TST",
        requested_format=QUICK_DRAFT_FORMAT,
        primary=SeventeenLandsFormatData(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            fetched_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
            card_ratings={
                grp_id: _stats(grp_id=grp_id, name=name, color=color, gih=gih)
                for grp_id, name, color, gih in entries
            },
            pair_win_rates={
                "WU": ColorPairWinRate(pair="WU", wins=60, games=100, win_rate=0.6),
                "BR": ColorPairWinRate(pair="BR", wins=70, games=100, win_rate=0.7),
            },
        ),
        fallback=None,
        thin_sample_minimum=500,
    )



def _stats(*, grp_id: int, name: str, color: str, gih: float) -> SeventeenCardStats:
    return SeventeenCardStats(
        grp_id=grp_id,
        name=name,
        color=color,
        rarity="common",
        average_last_seen_at=4.0,
        gih_win_rate=gih,
        opening_hand_win_rate=None,
        drawn_improvement_win_rate=None,
        sample_counts=RatingSampleCounts(
            seen=1000,
            picked=600,
            games_played=900,
            opening_hand=300,
            games_in_hand=900,
        ),
    )



def _card_database() -> CardDatabase:
    return CardDatabase(
        cards={
            1: _card(grp_id=1, name="White Bomb", colors=("W",)),
            2: _card(grp_id=2, name="Blue Bomb", colors=("U",)),
            3: _card(grp_id=3, name="White Playable", colors=("W",)),
            4: _card(grp_id=4, name="Blue Playable", colors=("U",)),
            5: _card(grp_id=5, name="Red Filler", colors=("R",)),
            6: _card(grp_id=6, name="Black Filler", colors=("B",)),
        }
    )


def _optimizer_candidates() -> tuple[ScoredCard, ...]:
    database = CardDatabase(
        cards={
            801: _card(
                grp_id=801,
                name="High Mana Spell One",
                colors=("W",),
                mana_value=6.0,
                types=("Instant",),
            ),
            802: _card(
                grp_id=802,
                name="High Mana Spell Two",
                colors=("U",),
                mana_value=6.0,
                types=("Sorcery",),
            ),
            803: _card(
                grp_id=803,
                name="Two-Drop Creature One",
                colors=("W",),
                mana_value=2.0,
            ),
            804: _card(
                grp_id=804,
                name="Two-Drop Creature Two",
                colors=("U",),
                mana_value=2.0,
            ),
            805: _card(
                grp_id=805,
                name="Midrange Creature One",
                colors=("W",),
                mana_value=3.0,
                types=("Instant",),
            ),
            806: _card(
                grp_id=806,
                name="Midrange Creature Two",
                colors=("U",),
                mana_value=3.0,
                types=("Sorcery",),
            ),
        }
    )
    scored = PickEngine().score_pack(
        offered_grp_ids=tuple(database.cards),
        card_database=database,
        pool_grp_ids=(),
        pick_index=1,
    ).cards
    score_by_grp_id = {
        801: 100.0,
        802: 99.0,
        803: 90.0,
        804: 89.0,
        805: 88.0,
        806: 87.0,
    }
    return tuple(
        replace(
            card,
            raw_score=score_by_grp_id[card.card.grp_id],
            score=int(score_by_grp_id[card.card.grp_id]),
        )
        for card in scored
    )


def _hob_optimizer_candidates() -> tuple[ScoredCard, ...]:
    fixture = json.loads(
        (FIXTURES_DIR / "deckbuilder-hob-pool.json").read_text(encoding="utf-8")
    )
    cards = fixture["cards"]
    oracle_text_by_grp_id = {
        901: "Draw an additional card each turn.",
        902: "Whenever you draw your second card each turn, this creature gets +1/+1.",
    }
    database = CardDatabase(
        cards={
            item["grp_id"]: replace(
                CardInfo.from_json(item),
                oracle_text=oracle_text_by_grp_id.get(item["grp_id"]),
            )
            for item in cards
        }
    )
    scored = PickEngine().score_pack(
        offered_grp_ids=tuple(fixture["offered_grp_ids"]),
        card_database=database,
        pool_grp_ids=(),
        pick_index=1,
    ).cards
    score_by_grp_id = {
        item["grp_id"]: item["raw_score"]
        for item in cards
    }
    return tuple(
        replace(
            card,
            raw_score=score_by_grp_id[card.card.grp_id],
            score=int(score_by_grp_id[card.card.grp_id]),
        )
        for card in scored
    )


def _hob_optimizer_setup(
    *,
    spell_count: int,
    redundancy_weight: float | None = None,
) -> tuple[
    tuple[ScoredCard, ...],
    DeckBuilderConfig,
    SpellConstraints,
    Counter[tuple[str, str]],
    SetProfile,
]:
    candidates = _hob_optimizer_candidates()
    config = replace(
        DECK_BUILDER,
        target_spell_count=spell_count,
        creature_floor=0,
        creature_ceiling=spell_count,
        minimum_two_drops=0,
        maximum_expensive_spells=spell_count,
        optimizer_beam_width=64,
        optimizer_max_search_nodes=10_000,
        optimizer_local_improvement_rounds=0,
        optimizer_max_evaluations=0,
        optimizer_redundancy_weight=(
            DECK_BUILDER.optimizer_redundancy_weight
            if redundancy_weight is None
            else redundancy_weight
        ),
        bench_card_count=0,
    )
    constraints = SpellConstraints(
        spell_count=spell_count,
        creature_floor=0,
        creature_ceiling=spell_count,
        minimum_two_drops=0,
        maximum_expensive_spells=spell_count,
    )
    available_quantities = Counter(
        _card_quantity_key(card=card.card) for card in candidates
    )
    profile = _empirical_pair_profile()
    return candidates, config, constraints, available_quantities, profile


def _constrained_pool_ids() -> tuple[int, ...]:
    return tuple(range(101, 129))



def _constrained_card_database() -> CardDatabase:
    cards: dict[int, CardInfo] = {}
    for grp_id in range(101, 106):
        cards[grp_id] = _card(
            grp_id=grp_id,
            name=f"Two-Drop Creature {grp_id}",
            colors=("W",) if grp_id % 2 == 0 else ("U",),
            mana_value=2.0,
            types=("Creature — Fixture",),
        )

    for grp_id in range(106, 115):
        cards[grp_id] = _card(
            grp_id=grp_id,
            name=f"Mid Curve Creature {grp_id}",
            colors=("W",) if grp_id % 2 == 0 else ("U",),
            mana_value=3.0,
            types=("Creature — Fixture",),
        )

    for grp_id in range(115, 119):
        cards[grp_id] = _card(
            grp_id=grp_id,
            name=f"Expensive Creature {grp_id}",
            colors=("W",) if grp_id % 2 == 0 else ("U",),
            mana_value=6.0,
            types=("Creature — Fixture",),
        )

    for grp_id in range(119, 129):
        cards[grp_id] = _card(
            grp_id=grp_id,
            name=f"Noncreature Spell {grp_id}",
            colors=("W",) if grp_id % 2 == 0 else ("U",),
            mana_value=3.0,
            types=("Instant",),
        )

    return CardDatabase(cards=cards)



def _curve_card_database(*, mana_value: float, count: int) -> CardDatabase:
    cards: dict[int, CardInfo] = {}
    for offset in range(count):
        grp_id = 401 + offset
        cards[grp_id] = _card(
            grp_id=grp_id,
            name=f"Curve Fixture {grp_id}",
            colors=("W",) if offset % 2 == 0 else ("U",),
            mana_value=mana_value,
            types=("Creature — Fixture",) if offset < 18 else ("Instant",),
            mana_cost="{W}" if offset % 2 == 0 else "{U}",
        )

    return CardDatabase(cards=cards)



def _pip_card_database(*, white_pips: int, blue_pips: int) -> CardDatabase:
    cards: dict[int, CardInfo] = {}
    for offset in range(23):
        grp_id = 501 + offset
        if offset < 10:
            colors = ("W",)
            mana_cost = "{W}{W}" if white_pips >= 20 else "{W}"
        elif offset < 13:
            colors = ("U",)
            mana_cost = "{U}"
        else:
            colors = ()
            mana_cost = None

        cards[grp_id] = _card(
            grp_id=grp_id,
            name=f"Pip Fixture {grp_id}",
            colors=colors,
            mana_value=3.0,
            types=("Creature — Fixture",) if offset < 14 else ("Instant",),
            mana_cost=mana_cost,
        )

    if white_pips == 12:
        cards[501] = _card(
            grp_id=501,
            name="Double White Fixture",
            colors=("W",),
            mana_value=3.0,
            types=("Creature — Fixture",),
            mana_cost="{W}{W}{W}",
        )

    if blue_pips == 12:
        cards[511] = _card(
            grp_id=511,
            name="Double Blue Fixture",
            colors=("U",),
            mana_value=3.0,
            types=("Creature — Fixture",),
            mana_cost="{U}{U}{U}{U}{U}{U}{U}{U}{U}{U}",
        )

    return CardDatabase(cards=cards)



def _rounding_card_database() -> CardDatabase:
    cards: dict[int, CardInfo] = {}
    for offset in range(23):
        grp_id = 601 + offset
        if offset < 9:
            colors = ("W",)
            mana_cost = "{W}"
        elif offset < 16:
            colors = ("U",)
            mana_cost = "{U}"
        elif offset == 16:
            colors = ("U",)
            mana_cost = "{U}{U}"
        else:
            colors = ()
            mana_cost = None

        cards[grp_id] = _card(
            grp_id=grp_id,
            name=f"Rounding Fixture {grp_id}",
            colors=colors,
            mana_value=3.0,
            types=("Creature — Fixture",) if offset < 14 else ("Instant",),
            mana_cost=mana_cost,
        )

    return CardDatabase(cards=cards)



def _basic_count(*, mana_base: ManaBase, name: str) -> int:
    return sum(basic.count for basic in mana_base.basic_lands if basic.name == name)



def _card(
    *,
    grp_id: int,
    name: str,
    colors: tuple[str, ...],
    mana_value: float = 2.0,
    types: tuple[str, ...] = ("Creature",),
    mana_cost: str | None = None,
    produced_mana: tuple[str, ...] = (),
) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=colors,
        mana_value=mana_value,
        rarity="common",
        types=types,
        mana_cost=mana_cost,
        produced_mana=produced_mana,
    )



def _draft_state(*, account_id: str, draft_id: str, updated_at: str) -> DraftState:
    return DraftState(
        account_id=account_id,
        draft_id=draft_id,
        event_name="QuickDraft_TST_20260703",
        set_code="TST",
        course_id=draft_id,
        started_at=FIXTURE_NOW,
        updated_at=updated_at,
        completed_at=None,
        completed=False,
        picks=(),
        pool_grp_ids=(1, 2, 3, 4),
    )
