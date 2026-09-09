from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import COLOR_PAIRS, PickEngineConfig
from draftomen.events import PackOfferedEvent
from draftomen.pickengine import (
    ContextualScoreBreakdown,
    MAX_CONTEXTUAL_ADJUSTMENT,
    MAX_FIXING_TERM,
    MAX_REDUNDANCY_TERM,
    MAX_ROLE_TERM,
    MAX_SYNERGY_TERM,
    MAX_UNSUPPORTED_PAYOFF_TERM,
    MAX_URGENCY_TERM,
    PickEngine,
    PickReason,
    PickRationale,
    PickScoringContext,
    ScoredPack,
    _recommendation_comparison_summary,
    build_pick_scoring_context,
    recommendation_confidence_summary,
    render_pick_rationale_concise,
    render_pick_rationale_detailed,
    score_pack,
)
from draftomen.pool_ledger import (
    COMPLETED_POOL,
    PoolRoleLedger,
    project_pool_role_ledger,
)
from draftomen.profile_generation import generate_set_profile
from draftomen.ranking import RANKING_MODES, rank_scored_cards
from draftomen.replay import format_pack_offered_event
from draftomen.set_profile import (
    AggregateEvidence,
    CardPairSynergy,
    CardRating,
    PairProfile,
    ProfileMaturity,
    RateEstimate,
    RoleTarget,
    SampleSummary,
    SetProfile,
    SourceMetadata,
)
from draftomen.semantic_roles import (
    CompiledRoleProfile,
    ProfileCard,
    ProducedResources,
    Role,
    RoleAssignment,
)
from draftomen.seventeen import (
    PREMIER_DRAFT_FORMAT,
    QUICK_DRAFT_FORMAT,
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsData,
    SeventeenLandsFormatData,
)
from draftomen.splash import card_is_castable_in_pair, splash_requirement


def test_pick_engine_scores_and_sorts_with_fallback_sources() -> None:
    engine = PickEngine(ratings_data=_ratings_data())

    scored_pack = engine.score_pack(
        offered_grp_ids=(4, 3, 2, 1),
        card_database=_card_database(),
    )
    assert [card.card.grp_id for card in scored_pack.cards] == [1, 4, 2, 3]
    assert scored_pack.scoring_context is None

    assert [card.score for card in scored_pack.cards] == sorted(
        [card.score for card in scored_pack.cards],
        reverse=True,
    )
    assert scored_pack.cards[0].card.grp_id == 1
    assert {card.card.grp_id: card.source_label for card in scored_pack.cards} == {
        1: "Quick",
        2: "Premier",
        3: "Quick",
        4: "Prior*",
    }
    assert scored_pack.source_summary == "QuickDraft + Premier fallback + neutral prior"
    assert all(0 <= card.score <= 100 for card in scored_pack.cards)
    assert all(isinstance(card.score, int) for card in scored_pack.cards)


def test_every_engine_row_has_an_immutable_ordered_pick_rationale() -> None:
    scored_pack = PickEngine(ratings_data=_ratings_data()).score_pack(
        offered_grp_ids=(4, 3, 2, 1),
        card_database=_card_database(),
    )

    assert all(
        isinstance(reason, PickReason)
        for card in scored_pack.cards
        for reason in card.rationale.reasons
    )
    assert all(
        isinstance(card.rationale.reasons, tuple)
        for card in scored_pack.cards
    )
    assert all(
        reason.kind in {
            "rating",
            "color",
            "role",
            "urgency",
            "synergy",
            "redundancy",
            "unsupported_payoff",
            "fixing",
            "splash",
            "tiebreaker",
        }
        for card in scored_pack.cards
        for reason in card.rationale.reasons
    )
    with pytest.raises(FrozenInstanceError):
        scored_pack.cards[0].rationale.reasons = ()


def test_rationale_score_accounting_preserves_open_ramp_and_locked_math() -> None:
    engine = PickEngine(ratings_data=_ratings_data())
    database = _card_database()
    database = replace(
        database,
        cards={
            **database.cards,
            1: replace(database.cards[1], set_code="TST", arena_id=1),
            7: replace(database.cards[7], set_code="TST", arena_id=7),
        },
    )

    for pick_index in (3, 10, 16):
        card = engine.score_pack(
            offered_grp_ids=(7,),
            card_database=database,
            pool_grp_ids=(1, 2),
            pick_index=pick_index,
        ).cards[0]
        color_reason = next(
            reason for reason in card.rationale.reasons if reason.kind == "color"
        )
        assert color_reason.contribution == pytest.approx(
            card.base_score * (card.color_factor - 1.0)
        )
        assert next(
            reason for reason in card.rationale.reasons if reason.kind == "rating"
        ).contribution is None
        assert card.base_score + card.rationale.total_contribution == pytest.approx(
            card.raw_score
        )
    cap_profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:1",
                assignments=(RoleAssignment(Role.GO_WIDE_PAYOFF),),
            ),
            ProfileCard(
                key="arena_id:7",
                assignments=(
                    RoleAssignment(Role.DRAW),
                    RoleAssignment(Role.GO_WIDE_ENABLER),
                    RoleAssignment(
                        Role.FIXING,
                        parameters=ProducedResources(("W", "U")),
                    ),
                ),
            ),
        ),
        role_targets=(
            RoleTarget(Role.DRAW, 1),
            RoleTarget(Role.FIXING, 1),
        ),
    )
    capped = _score_with_context(
        database=database,
        profile=cap_profile,
        offered_grp_ids=(7,),
        pool_grp_ids=(1, 2),
        pack_number=2,
        pick_number=13,
        global_pick_index=42,
        estimated_remaining_picks=0,
    ).cards[0]
    assert capped.contextual_breakdown.aggregate == MAX_CONTEXTUAL_ADJUSTMENT
    assert capped.contextual_breakdown.to_json()["aggregate"] == 6.0
    assert capped.raw_score == pytest.approx(63.5)
    assert capped.score == 63
    assert capped.rationale.attributed_contribution == pytest.approx(16.0)
    assert capped.rationale.unattributed_contribution == pytest.approx(-2.5)
    assert capped.rationale.total_contribution == pytest.approx(13.5)

    clamped = engine.score_pack(
        offered_grp_ids=(9,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=16,
    ).cards[0]
    assert clamped.base_score == pytest.approx(100.0)
    assert clamped.raw_score == pytest.approx(100.0)
    assert clamped.score == 100
    assert clamped.rationale.attributed_contribution == pytest.approx(15.0)
    assert clamped.rationale.unattributed_contribution == pytest.approx(-15.0)
    assert clamped.rationale.total_contribution == pytest.approx(0.0)


def test_contextual_rationale_keeps_material_term_order_and_evidence() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:1",
                assignments=(RoleAssignment(Role.GO_WIDE_PAYOFF),),
            ),
            ProfileCard(
                key="arena_id:2",
                assignments=(RoleAssignment(Role.DRAW_SECOND_PAYOFF),),
            ),
            ProfileCard(
                key="arena_id:5",
                assignments=(RoleAssignment(Role.DRAW),),
            ),
            ProfileCard(
                key="arena_id:7",
                assignments=(
                    RoleAssignment(Role.DRAW),
                    RoleAssignment(Role.GO_WIDE_ENABLER),
                    RoleAssignment(Role.DRAW_SECOND_PAYOFF),
                    RoleAssignment(
                        Role.FIXING,
                        parameters=ProducedResources(("W", "U")),
                    ),
                ),
            ),
        ),
        role_targets=(
            RoleTarget(Role.DRAW, 2),
            RoleTarget(Role.FIXING, 2),
        ),
    )
    card = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(7,),
        pool_grp_ids=(1, 2, 5),
    ).cards[0]

    contextual_reasons = tuple(
        reason
        for reason in card.rationale.reasons
        if reason.kind
        in {
            "role",
            "urgency",
            "synergy",
            "redundancy",
            "unsupported_payoff",
            "fixing",
        }
    )
    assert tuple(reason.kind for reason in contextual_reasons) == (
        "role",
        "urgency",
        "synergy",
        "redundancy",
        "unsupported_payoff",
        "fixing",
    )
    assert len(card.contextual_evidence) == 6
    assert tuple(reason.evidence for reason in contextual_reasons) == (
        card.contextual_evidence
    )
    assert tuple(reason.contribution for reason in contextual_reasons) == pytest.approx(
        (
            card.contextual_breakdown.role,
            card.contextual_breakdown.urgency,
            card.contextual_breakdown.synergy,
            card.contextual_breakdown.redundancy,
            card.contextual_breakdown.unsupported_payoff,
            card.contextual_breakdown.fixing,
        )
    )

@pytest.mark.parametrize(
    ("confidence", "material"),
    (
        # Profile confidence contributes to target pressure and evidence weight.
        (0.068402, False),
        (0.06841, True),
    ),
)
def test_contextual_reasons_require_more_than_one_hundredth_point(
    confidence: float,
    material: bool,
) -> None:
    card = _score_with_context(
        database=_contextual_database(),
        profile=_contextual_profile(
            cards=(
                ProfileCard(
                    key="arena_id:7",
                    assignments=(RoleAssignment(Role.DRAW),),
                ),
            ),
            role_targets=(RoleTarget(Role.DRAW, 1),),
            confidence=confidence,
        ),
        offered_grp_ids=(7,),
        pool_grp_ids=(),
    ).cards[0]

    assert (
        card.contextual_breakdown.role == pytest.approx(0.01)
        if not material
        else card.contextual_breakdown.role > 0.01
    )
    role_reasons = tuple(
        reason for reason in card.rationale.reasons if reason.kind == "role"
    )
    assert bool(role_reasons) is material
    assert bool(card.contextual_evidence) is material
    if material:
        assert role_reasons[0].evidence == "fills draw deficit (0/1)"


def test_splash_and_actual_pair_tiebreak_reasons_retain_provenance() -> None:
    splash_card = next(
        card
        for card in PickEngine(ratings_data=_splash_ratings_data())
        .score_pack(
            offered_grp_ids=(105,),
            card_database=_splash_card_database(),
            pool_grp_ids=(101, 102, 101, 102, 103, 104),
            pick_index=10,
        )
        .cards
    )
    splash_reason = next(
        reason for reason in splash_card.rationale.reasons if reason.kind == "splash"
    )
    assert splash_reason.contribution is None
    assert splash_reason.evidence == splash_card.splash.reasons

    tiebreak_pack = PickEngine(
        ratings_data=_msh_pair_tiebreaker_data(),
    ).score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=3,
    )
    assert any(
        reason.kind == "tiebreaker" for reason in tiebreak_pack.cards[0].rationale.reasons
    )
    assert not any(
        reason.kind == "tiebreaker" for reason in tiebreak_pack.cards[1].rationale.reasons
    )


def test_concise_rationale_is_hedged_and_uses_human_whole_point_terms() -> None:
    card = PickEngine().score_pack(
        offered_grp_ids=(6,),
        card_database=_card_database(),
    ).cards[0]
    concise = render_pick_rationale_concise(scored_card=card)

    assert concise == (
        "Rating: neutral-prior estimate with no GIH data. "
        "Color fit contributes +0 DO points."
    )
    assert len([sentence for sentence in concise.split(".") if sentence]) <= 3
    assert "aggregate" not in concise
    assert "unsupported_payoff" not in concise



def test_concise_reasons_use_magnitude_ties_and_stable_nonadditive_fallback() -> None:
    card = PickEngine().score_pack(
        offered_grp_ids=(6,),
        card_database=_card_database(),
    ).cards[0]
    card = replace(
        card,
        rationale=PickRationale(
            reasons=(
                PickReason(kind="rating", phrase="known rating"),
                PickReason(
                    kind="color",
                    contribution=0.2,
                    phrase="internal color evidence",
                    evidence=("raw_color_label",),
                ),
                PickReason(
                    kind="role",
                    contribution=1.25,
                    phrase="internal role evidence",
                    evidence=("raw_role_label",),
                ),
                PickReason(
                    kind="urgency",
                    contribution=-1.25,
                    phrase="internal urgency evidence",
                    evidence=("raw_urgency_label",),
                ),
                PickReason(
                    kind="splash",
                    phrase="splash speculative assessment",
                    evidence=("raw_splash_label",),
                ),
                PickReason(
                    kind="tiebreaker",
                    phrase="pair-rate comparison",
                    evidence=("raw_tiebreaker_label",),
                ),
            )
        ),
    )

    concise = render_pick_rationale_concise(scored_card=card)

    assert concise == (
        "Rating: known rating. "
        "Role fit contributes +1 DO points. "
        "Timing contributes -1 DO points."
    )
    assert "raw_" not in concise
    assert "internal" not in concise
    fallback = replace(
        card,
        rationale=PickRationale(
            reasons=(
                PickReason(kind="rating", phrase="known rating"),
                PickReason(
                    kind="color",
                    contribution=0.0,
                    phrase="color",
                    evidence=("raw_color_label",),
                ),
                PickReason(
                    kind="splash",
                    phrase="splash speculative assessment",
                    evidence=("raw_splash_label",),
                ),
                PickReason(
                    kind="tiebreaker",
                    phrase="pair-rate comparison",
                    evidence=("raw_tiebreaker_label",),
                ),
            )
        ),
    )
    assert render_pick_rationale_concise(scored_card=fallback) == (
        "Rating: known rating. "
        "Color fit contributes +0 DO points. "
        "Speculative splash is a consideration."
    )


def test_rating_copy_retains_profile_confidence_for_neutral_and_fallback_ratings() -> None:
    profile = _contextual_profile(cards=(), confidence=0.42)

    neutral_card = _score_with_context(
        database=_contextual_database(),
        profile=profile,
        offered_grp_ids=(7,),
        pool_grp_ids=(1,),
    ).cards[0]
    fallback_card = _score_with_context(
        database=_contextual_database(),
        profile=profile,
        offered_grp_ids=(7,),
        pool_grp_ids=(1,),
        ratings_data=_ratings_data(),
    ).cards[0]

    assert render_pick_rationale_concise(
        scored_card=neutral_card
    ).startswith(
        "Rating: neutral-prior estimate with no GIH data "
        "(mature profile, 42% confidence)."
    )
    assert render_pick_rationale_concise(
        scored_card=fallback_card
    ).startswith("Rating: Quick GIH win rate 55.0% (mature profile, 42% confidence).")


def test_detailed_rationale_omits_context_without_scoring_context() -> None:
    card = PickEngine(ratings_data=_ratings_data()).score_pack(
        offered_grp_ids=(6,),
        card_database=_card_database(),
    ).cards[0]

    explanation = render_pick_rationale_detailed(scored_card=card)

    assert "context " not in explanation
    assert "unknown profile" not in explanation


def test_concise_splash_reasons_are_short_and_detailed_reasons_are_not_duplicated() -> None:
    database = _splash_card_database()
    engine = PickEngine(ratings_data=_splash_ratings_data())
    speculative = next(
        card
        for card in engine.score_pack(
            offered_grp_ids=(105,),
            card_database=database,
            pool_grp_ids=(101, 102, 101, 102),
            pick_index=10,
        ).cards
    )
    fixer = next(
        card
        for card in engine.score_pack(
            offered_grp_ids=(103,),
            card_database=database,
            pool_grp_ids=(101, 102, 101, 102, 104, 105),
            pick_index=12,
        ).cards
    )

    assert "Speculative splash is a consideration." in (
        render_pick_rationale_concise(scored_card=speculative)
    )
    assert "Splash fixing is available." in render_pick_rationale_concise(
        scored_card=fixer
    )
    detailed = render_pick_rationale_detailed(
        scored_card=speculative,
    )
    assert detailed.count("splash: ") == 1
    assert "splash: splash:" not in detailed


def test_populated_equal_pair_rates_do_not_create_a_tiebreak_reason() -> None:
    scored_pack = PickEngine(
        ratings_data=_msh_pair_tiebreaker_data(wu_rate=0.55, br_rate=0.55),
    ).score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=3,
    )

    assert all(
        not any(reason.kind == "tiebreaker" for reason in card.rationale.reasons)
        for card in scored_pack.cards
    )

def test_set_reliability_calculation_does_not_change_card_scores() -> None:
    ratings_data = _ratings_data()
    database = _card_database()
    before = PickEngine(ratings_data=ratings_data).score_pack(
        offered_grp_ids=(4, 3, 2, 1),
        card_database=database,
    )

    reliability = ratings_data.set_reliability

    after = PickEngine(ratings_data=ratings_data).score_pack(
        offered_grp_ids=(4, 3, 2, 1),
        card_database=database,
    )
    assert reliability.set_code == ratings_data.set_code
    assert after == before


def test_alsa_adjusts_neutral_prior_when_gih_is_absent() -> None:
    engine = PickEngine(ratings_data=_ratings_data())

    scored_pack = engine.score_pack(
        offered_grp_ids=(4, 5, 6),
        card_database=_card_database(),
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert by_id[4].prior_adjusted_by_alsa is True
    assert by_id[5].prior_adjusted_by_alsa is True
    assert by_id[6].prior_adjusted_by_alsa is False
    assert by_id[4].base_rating > engine.normalization.neutral_rating
    assert by_id[5].base_rating < engine.normalization.neutral_rating
    assert by_id[6].score == 50


def test_missing_ratings_data_still_scores_every_card_with_marked_prior() -> None:
    engine = PickEngine()

    scored_pack = engine.score_pack(
        offered_grp_ids=(1, 2, 3),
        card_database=_card_database(),
    )

    assert [card.score for card in scored_pack.cards] == [50, 50, 50]
    assert all(card.source_label == "Prior*" for card in scored_pack.cards)
    assert scored_pack.source_summary == "neutral prior"


def test_profile_identity_precedence_casefolding_and_zero_arena_id() -> None:
    database = CardDatabase(
        cards={
            1: _card(
                grp_id=1,
                name="Card 1",
                colors=("W",),
                set_code="tSt",
                collector_number="1",
                arena_id=0,
                oracle_id="ORACLE-1",
            ),
            2: _card(
                grp_id=2,
                name="Card 2",
                colors=("U",),
                set_code="tSt",
                collector_number="2",
                arena_id=2,
            ),
            3: _card(
                grp_id=3,
                name="Card 3",
                colors=("B",),
                set_code="TST",
                arena_id=0,
            ),
            4: _card(
                grp_id=4,
                name="Card 4",
                colors=("R",),
                set_code="TST",
            ),
            5: _card(
                grp_id=5,
                name="Card 5",
                colors=("G",),
                set_code="TST",
                collector_number="5",
                arena_id=5,
            ),
            6: _card(
                grp_id=6,
                name="Card 6",
                colors=("W",),
                set_code="TST",
                collector_number="6",
                oracle_id="ORACLE-1",
            ),
        }
    )
    profile = _test_profile(
        card_ratings=tuple(
            _profile_card(*item)
            for item in (
                ("ORACLE_ID:ORACLE-1", 0.71),
                ("SET:TST:1", 0.72),
                ("ARENA_ID:0", 0.63),
                ("GRP_ID:1", 0.74),
                ("SET:TST:2", 0.64),
                ("ARENA_ID:2", 0.73),
                ("GRP_ID:4", 0.59),
                ("ARENA_ID:5", 0.99),
            )
        )
    )
    ratings = _ratings_variant(
        card_ratings={
            5: _stats(
                grp_id=5,
                name="Card 5",
                color="G",
                gih=0.10,
                games_in_hand=900,
            )
        }
    )
    scored = PickEngine(ratings_data=ratings, set_profile=profile).score_pack(
        offered_grp_ids=(5, 4, 3, 2, 1, 6),
        card_database=database,
    )
    by_id = {card.card.grp_id: card for card in scored.cards}

    assert [
        by_id[grp_id].rating.gih_win_rate for grp_id in (1, 2, 3, 4, 5, 6)
    ] == [
        pytest.approx(value)
        for value in (0.71, 0.64, 0.63, 0.59, 0.10, 0.71)
    ]
    assert by_id[5].source_label == "Quick"
    assert scored.normalization.lower_rating == pytest.approx(0.4005)
    assert scored.normalization.upper_rating == pytest.approx(0.6995)


def test_profile_rating_preserves_evidence_and_computes_grade() -> None:
    database = _contextual_database()
    profile = _test_profile(
        maturity=ProfileMaturity.EARLY,
        card_ratings=(
            CardRating(
                card_key="ARENA_ID:1",
                gih_win_rate=RateEstimate(
                    raw_value=0.64,
                    value=0.61,
                    samples=7,
                    prior_value=0.50,
                    source="test",
                ),
                average_last_seen_at=3.5,
            ),
            CardRating(
                card_key="ARENA_ID:2",
                gih_win_rate=RateEstimate(
                    raw_value=None,
                    value=0.52,
                    samples=0,
                    prior_value=0.50,
                    source="test",
                ),
                average_last_seen_at=2.0,
            ),
            CardRating(
                card_key="ARENA_ID:3",
                gih_win_rate=RateEstimate(
                    raw_value=0.55,
                    value=0.55,
                    samples=5,
                    prior_value=0.50,
                    source="test",
                ),
            ),
        ),
    )
    scored = PickEngine(set_profile=profile).score_pack(
        offered_grp_ids=(1, 2, 3),
        card_database=database,
    )
    by_id = {card.card.grp_id: card for card in scored.cards}
    positive = by_id[1].rating
    zero = by_id[2].rating
    middle = by_id[3].rating

    assert positive.gih_win_rate == pytest.approx(0.61)
    assert positive.sample_counts.games_in_hand == 7
    assert positive.average_last_seen_at == pytest.approx(3.5)
    assert positive.letter_grade == "B"
    assert zero.gih_win_rate == pytest.approx(0.52)
    assert zero.sample_counts.games_in_hand == 0
    assert zero.average_last_seen_at == pytest.approx(2.0)
    assert zero.letter_grade is None
    assert middle.letter_grade == "D"
    for rating in (positive, zero, middle):
        assert rating.opening_hand_win_rate is None
        assert rating.drawn_improvement_win_rate is None
        assert rating.neutral_prior_score is None
        assert rating.metadata.fallback_reason is None
        assert rating.metadata.source == "profile"
        assert rating.neutral_prior is False

def test_profile_rating_metadata_consumes_versioned_aggregate_authority() -> None:
    database = _contextual_database()
    fallback_rate = replace(
        _profile_rate(0.61, samples=7),
        aggregate_evidence=AggregateEvidence(
            source_format="PremierDraft",
            fallback_reason="missing-exact-evidence",
            confidence=0.65,
        ),
    )
    exact_rate = replace(
        _profile_rate(0.55, samples=5),
        aggregate_evidence=AggregateEvidence(
            source_format="QuickDraft",
            fallback_reason=None,
            confidence=1.0,
        ),
    )
    zero_rate = RateEstimate(
        raw_value=None,
        value=0.52,
        samples=0,
        prior_value=0.50,
        source="test",
    )
    schema_two = _test_profile(
        maturity=ProfileMaturity.EARLY,
        schema_version=2,
        card_ratings=(
            CardRating(
                card_key="ARENA_ID:1",
                gih_win_rate=fallback_rate,
                average_last_seen_at=3.5,
            ),
            CardRating(card_key="ARENA_ID:2", gih_win_rate=zero_rate),
            CardRating(card_key="ARENA_ID:3", gih_win_rate=exact_rate),
        ),
    )
    historical = _test_profile(
        maturity=ProfileMaturity.EARLY,
        card_ratings=(
            CardRating(
                card_key="ARENA_ID:1",
                gih_win_rate=replace(fallback_rate, aggregate_evidence=None),
                average_last_seen_at=3.5,
            ),
            CardRating(card_key="ARENA_ID:2", gih_win_rate=zero_rate),
            CardRating(
                card_key="ARENA_ID:3",
                gih_win_rate=replace(exact_rate, aggregate_evidence=None),
            ),
        ),
    )

    current = PickEngine(set_profile=schema_two).score_pack(
        offered_grp_ids=(1, 2, 3),
        card_database=database,
    )
    legacy = PickEngine(set_profile=historical).score_pack(
        offered_grp_ids=(1, 2, 3),
        card_database=database,
    )
    current_by_id = {card.card.grp_id: card for card in current.cards}
    legacy_by_id = {card.card.grp_id: card for card in legacy.cards}

    for grp_id in (1, 2, 3):
        current_card = current_by_id[grp_id]
        legacy_card = legacy_by_id[grp_id]
        assert current_card.score == legacy_card.score
        assert current_card.raw_score == pytest.approx(legacy_card.raw_score)
        assert current_card.base_rating == pytest.approx(legacy_card.base_rating)
        assert current_card.rating.gih_win_rate == pytest.approx(
            legacy_card.rating.gih_win_rate
        )
        assert current_card.rating.sample_counts == legacy_card.rating.sample_counts
        assert current_card.rating.letter_grade == legacy_card.rating.letter_grade

    assert current_by_id[1].rating.metadata.requested_format == "quickdraft"
    assert current_by_id[1].rating.metadata.source == "profile"
    assert current_by_id[1].rating.metadata.source_format == "premierdraft"
    assert (
        current_by_id[1].rating.metadata.fallback_reason
        == "missing-exact-evidence"
    )
    assert current_by_id[3].rating.metadata.source_format == "quickdraft"
    assert current_by_id[3].rating.metadata.fallback_reason is None
    assert current_by_id[2].rating.metadata.source_format is None
    assert current_by_id[2].rating.metadata.fallback_reason is None

    for card in legacy.cards:
        assert card.rating.metadata.requested_format == "quickdraft"
        assert card.rating.metadata.source_format == (
            None if card.rating.sample_counts.games_in_hand == 0 else "quickdraft"
        )
        assert card.rating.metadata.fallback_reason is None



def test_non_empirical_profiles_preserve_deterministic_fallback_and_partial_legacy() -> None:
    database, ratings = _card_database(), _ratings_data()
    profiles = (
        None,
        SetProfile.generic(set_code="TST", event_format="quickdraft"),
        _test_profile(maturity=ProfileMaturity.METADATA_ONLY, total_samples=None),
        _test_profile(
            maturity=ProfileMaturity.SEMANTIC_ONLY,
            total_samples=None,
            role_profile=CompiledRoleProfile(set_code="TST", cards=()),
        ),
    )
    expected = None
    for profile in profiles:
        for _ in range(2):
            scored = PickEngine(
                ratings_data=ratings,
                set_profile=profile,
            ).score_pack(
                offered_grp_ids=(4, 3, 2, 1),
                card_database=database,
            )
            current = tuple(
                (card.card.grp_id, card.score, card.source_label)
                for card in scored.cards
            )
            if expected is None:
                expected = current
            assert current == expected
    partial = _test_profile(
        maturity=ProfileMaturity.EARLY,
        card_ratings=(_profile_card("ARENA_ID:1", 0.51, samples=1),),
    )
    scored = PickEngine(
        ratings_data=ratings,
        set_profile=partial,
    ).score_pack(
        offered_grp_ids=(2, 1),
        card_database=_contextual_database(),
    )
    by_id = {card.card.grp_id: card for card in scored.cards}
    assert {
        grp_id: (card.source_label, card.rating.gih_win_rate)
        for grp_id, card in by_id.items()
    } == {
        1: ("Profile", pytest.approx(0.51)),
        2: ("Premier", pytest.approx(0.58)),
    }


def test_freely_available_basic_land_scores_zero_and_ranks_last() -> None:
    scored_pack = PickEngine(ratings_data=_ratings_data()).score_pack(
        offered_grp_ids=(13, 3, 10),
        card_database=_card_database(),
    )

    assert [card.card.name for card in scored_pack.cards] == [
        "Quick Filler",
        "Blue Filler",
        "Arena Plains",
    ]
    assert all(card.score > 0 for card in scored_pack.cards[:-1])
    basic_land = scored_pack.cards[-1]
    assert basic_land.score == 0
    assert basic_land.source_label == "Basic"
    assert basic_land.no_data is False
    assert basic_land.freely_available_basic is True
    assert render_pick_rationale_detailed(
        scored_card=basic_land,
    ) == (
        "Arena Plains is freely available during deck building, so it receives "
        "0 DO points and ranks after draftable cards."
    )


def test_special_and_nonbasic_lands_retain_normal_scoring() -> None:
    scored_pack = PickEngine().score_pack(
        offered_grp_ids=(14, 15, 16),
        card_database=_card_database(),
    )

    assert {
        card.card.name: (
            card.score,
            card.source_label,
            card.no_data,
            card.freely_available_basic,
        )
        for card in scored_pack.cards
    } == {
        "Wastes": (50, "Prior*", True, False),
        "Snow-Covered Plains": (50, "Prior*", True, False),
        "Prairie Sanctuary": (50, "Prior*", True, False),
    }


def test_canonical_basic_overrides_rating_and_loses_zero_score_tie() -> None:
    scored_pack = PickEngine(ratings_data=_rated_basic_and_zero_data()).score_pack(
        offered_grp_ids=(121, 120),
        card_database=_splash_card_database(),
    )

    assert [card.card.name for card in scored_pack.cards] == [
        "Draftable Zero",
        "Mountain",
    ]
    assert [card.score for card in scored_pack.cards] == [0, 0]
    for ranking_mode in RANKING_MODES:
        assert [
            card.card.name
            for card in rank_scored_cards(
                cards=scored_pack.cards,
                ranking_mode=ranking_mode,
            )
        ] == ["Draftable Zero", "Mountain"]

    mountain = scored_pack.cards[-1]
    assert mountain.rating.gih_win_rate == 0.70
    assert mountain.source_label == "Basic"
    assert mountain.no_data is False
    assert recommendation_confidence_summary(
        cards=scored_pack.cards,
        ranking_mode="score",
        phase="building",
    ) is None


def test_recommendation_confidence_summary_uses_shared_open_pick_copy() -> None:
    scored_pack = PickEngine().score_pack(
        offered_grp_ids=(1,),
        card_database=_card_database(),
    )

    assert recommendation_confidence_summary(
        cards=scored_pack.cards,
        ranking_mode="score",
        phase="open",
    ) == "early/open pick — stay flexible"
    assert recommendation_confidence_summary(
        cards=scored_pack.cards,
        ranking_mode="score",
        phase="building",
    ) is None


def test_engine_comparison_filters_basics_and_wires_real_score_pack() -> None:
    engine = PickEngine(ratings_data=_ratings_data())
    database = _card_database()

    assert (
        engine.score_pack(
            offered_grp_ids=(),
            card_database=database,
        ).comparison_summary
        is None
    )
    assert (
        engine.score_pack(
            offered_grp_ids=(1,),
            card_database=database,
        ).comparison_summary
        is None
    )
    assert (
        engine.score_pack(
            offered_grp_ids=(13,),
            card_database=database,
        ).comparison_summary
        is None
    )
    assert (
        engine.score_pack(
            offered_grp_ids=(1, 13),
            card_database=database,
        ).comparison_summary
        is None
    )

    scored_pack = engine.score_pack(
        offered_grp_ids=(1, 2, 13),
        card_database=database,
    )
    summary = scored_pack.comparison_summary
    assert summary is not None
    assert summary.startswith("DO recommendation: ")
    assert scored_pack.cards[0].card.name in summary
    assert scored_pack.cards[1].card.name in summary
    assert "Arena Plains" not in summary

    higher_looking_basic_pack = PickEngine(
        ratings_data=_rated_basic_and_zero_data(),
    ).score_pack(
        offered_grp_ids=(120, 105, 121),
        card_database=_splash_card_database(),
    )
    higher_looking_summary = higher_looking_basic_pack.comparison_summary
    assert higher_looking_summary is not None
    assert "Mountain" not in higher_looking_summary


def test_real_msh_pair_comparison_uses_pair_rates_and_open_hedge() -> None:
    scored_pack = PickEngine(
        ratings_data=_msh_pair_tiebreaker_data(),
    ).score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=3,
    )

    summary = scored_pack.comparison_summary
    assert summary is not None
    assert "Blue WU Lane Card ranks ahead of Red BR Lane Card" in summary
    assert "favors WU by 6.7 percentage points." in summary
    assert "DO point" not in summary
    assert summary.endswith("early/open close pick; stay flexible.")


@pytest.mark.parametrize(
    ("base_scores", "contributions", "expected"),
    [
        (
            (70.0, 60.0),
            ({}, {}),
            "DO recommendation: Alpha leads Beta by 10 DO points, mainly from rating.",
        ),
        (
            (60.0, 60.0),
            ({"color": 10.0}, {}),
            "DO recommendation: Alpha leads Beta by 10 DO points, mainly from color fit.",
        ),
        (
            (60.0, 60.0),
            ({"role": 4.0}, {}),
            "DO recommendation: Alpha leads Beta by 4 DO points, mainly from role fit.",
        ),
    ],
)
def test_comparison_attributes_rating_color_and_context(
    base_scores: tuple[float, float],
    contributions: tuple[dict[str, float], dict[str, float]],
    expected: str,
) -> None:
    cards = _comparison_cards(
        base_scores=base_scores,
        contributions=contributions,
    )

    assert _render_comparison(cards, phase="building") == expected


def test_comparison_selects_majority_prefix_and_omits_opposing_factors() -> None:
    majority_cards = _comparison_cards(
        base_scores=(60.0, 60.0),
        contributions=(
            {"role": 4.0, "urgency": 3.0, "synergy": 3.0},
            {},
        ),
    )
    assert _render_comparison(majority_cards, phase="building") == (
        "DO recommendation: Alpha leads Beta by 10 DO points, mainly from "
        "role fit and timing."
    )

    opposing_cards = _comparison_cards(
        base_scores=(60.0, 60.0),
        contributions=(
            {"color": 8.0},
            {"color": 2.0, "role": 4.0},
        ),
    )
    assert _render_comparison(opposing_cards, phase="building") == (
        "DO recommendation: Alpha leads Beta by 2 DO points, mainly from "
        "color fit. close pick."
    )
    assert "role fit" not in _render_comparison(opposing_cards, phase="building")


def test_comparison_attributes_cap_accounting_remainder() -> None:
    engine = PickEngine(ratings_data=_ratings_data())
    database = _card_database()
    database = replace(
        database,
        cards={
            **database.cards,
            1: replace(database.cards[1], set_code="TST", arena_id=1),
            7: replace(database.cards[7], set_code="TST", arena_id=7),
        },
    )
    clamped = engine.score_pack(
        offered_grp_ids=(9,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=16,
    ).cards[0]
    runner_up = replace(
        clamped,
        card=replace(clamped.card, name="Beta"),
        original_index=1,
        raw_score=95.0,
        score=95,
        rationale=replace(
            clamped.rationale,
            unattributed_contribution=-20.0,
        ),
    )
    top = replace(
        clamped,
        card=replace(clamped.card, name="Alpha"),
        original_index=0,
    )

    assert _render_comparison((top, runner_up), phase="building") == (
        "DO recommendation: Alpha leads Beta by 5 DO points, mainly from "
        "score limits and small adjustments."
    )


def test_displayed_score_ties_explain_each_actual_resolution() -> None:
    unrounded = _comparison_cards(
        base_scores=(60.0, 60.0),
        raw_scores=(60.4, 60.1),
        scores=(60, 60),
    )
    assert _render_comparison(unrounded, phase="building") == (
        "DO recommendation: Alpha and Beta tie on the displayed score; "
        "Alpha ranks first on the unrounded score. close pick."
    )

    base_rating = _comparison_cards(
        base_scores=(60.0, 60.0),
        base_ratings=(0.70, 0.60),
        raw_scores=(60.0, 60.0),
        scores=(60, 60),
    )
    assert _render_comparison(base_rating, phase="building") == (
        "DO recommendation: Alpha and Beta tie on the displayed score; "
        "Alpha ranks first on the base rating. close pick."
    )

    offered_order = _comparison_cards(
        base_scores=(60.0, 60.0),
        base_ratings=(0.60, 0.60),
        raw_scores=(60.0, 60.0),
        scores=(60, 60),
    )
    assert _render_comparison(offered_order, phase="building") == (
        "DO recommendation: Alpha and Beta tie on the displayed score; "
        "Alpha appeared earlier in the offered pack. close pick."
    )


def test_pair_rate_precedence_uses_direct_runner_up_and_compact_hedge() -> None:
    cards = _comparison_cards(
        base_scores=(60.0, 60.0),
        raw_scores=(60.0, 60.0),
        scores=(60, 60),
        pair_rates=(0.606, 0.539),
        pair_names=("WU", "BR"),
        tiebreaker_indices=(0,),
    )

    assert _render_comparison(cards, phase="open") == (
        "DO recommendation: Alpha ranks ahead of Beta because the open-pick "
        "pair tiebreaker favors WU by 6.7 percentage points. "
        "early/open close pick; stay flexible."
    )

    small_difference = _comparison_cards(
        base_scores=(60.0, 60.0),
        raw_scores=(60.0, 60.0),
        scores=(60, 60),
        pair_rates=(0.6001, 0.6000),
        pair_names=("WU", "BR"),
        tiebreaker_indices=(0,),
    )
    assert "less than 0.1 percentage points" in _render_comparison(
        small_difference,
        phase="open",
    )


def test_pair_rate_comparison_does_not_quote_stale_third_card_provenance() -> None:
    cards = _comparison_cards(
        names=("Alpha", "Beta", "Gamma"),
        base_scores=(60.0, 60.0, 60.0),
        raw_scores=(60.0, 60.0, 60.0),
        scores=(60, 60, 60),
        pair_rates=(0.606, 0.539, 0.700),
        pair_names=("WU", "BR", "RG"),
        tiebreaker_indices=(0,),
        tiebreaker_evidence="pair-rate comparison selected WU over RG",
    )

    summary = _render_comparison(cards, phase="open")
    assert "favors WU by 6.7 percentage points" in summary
    assert "BR" not in summary
    assert "Gamma" not in summary
    assert "RG" not in summary


@pytest.mark.parametrize(
    ("pair_rates", "pair_names"),
    [
        ((0.600, 0.600), ("WU", "BR")),
        ((None, 0.600), ("WU", "BR")),
        ((0.606, 0.539), (None, "BR")),
    ],
)
def test_equal_or_missing_pair_metadata_falls_through_to_score_explanation(
    pair_rates: tuple[float | None, float | None],
    pair_names: tuple[str | None, str | None],
) -> None:
    cards = _comparison_cards(
        base_scores=(62.0, 60.0),
        pair_rates=pair_rates,
        pair_names=pair_names,
        tiebreaker_indices=(0,),
    )

    assert _render_comparison(cards, phase="open") == (
        "DO recommendation: Alpha leads Beta by 2 DO points, mainly from "
        "rating. early/open close pick; stay flexible."
    )


def test_close_group_fallback_describes_order_without_inventing_gap() -> None:
    cards = _comparison_cards(
        base_scores=(60.0, 61.0),
        raw_scores=(60.0, 61.0),
        scores=(60, 61),
    )

    assert _render_comparison(cards, phase="building") == (
        "DO recommendation: Alpha ranks ahead of Beta after deterministic "
        "close-pick ordering. close pick."
    )


def test_comparison_uses_displayed_whole_point_gap_and_open_hedge() -> None:
    cards = _comparison_cards(
        base_scores=(60.6, 59.4),
        raw_scores=(60.6, 59.4),
        scores=(61, 59),
    )
    assert _render_comparison(cards, phase="building") == (
        "DO recommendation: Alpha leads Beta by 2 DO points, mainly from "
        "rating. close pick."
    )

    open_cards = _comparison_cards(
        base_scores=(70.0, 60.0),
        raw_scores=(70.0, 60.0),
        scores=(70, 60),
    )
    assert _render_comparison(open_cards, phase="open") == (
        "DO recommendation: Alpha leads Beta by 10 DO points, mainly from "
        "rating. early/open pick — stay flexible."
    )


def test_commitment_ramp_changes_same_card_score_by_pick_index() -> None:
    engine = PickEngine(ratings_data=_ratings_data())
    database = _card_database()

    early = engine.score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=5,
    )
    mid = engine.score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=10,
    )
    late = engine.score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=16,
    )

    assert early.commitment.inferred_pair == "WU"
    assert early.commitment.level == 0.0
    assert mid.commitment.phase == "building"
    assert late.commitment.phase == "locked"
    assert early.cards[0].color_fit == "open"
    assert mid.cards[0].color_fit == "on-color"
    assert early.cards[0].score < mid.cards[0].score < late.cards[0].score


def test_explicit_global_pick_index_drives_commitment_and_ledger_stage() -> None:
    scored_pack = PickEngine().score_pack(
        offered_grp_ids=(7,),
        card_database=_card_database(),
        pool_grp_ids=(1, 2),
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
    )

    assert scored_pack.commitment.pick_index == 35
    assert scored_pack.commitment.phase == "locked"
    assert scored_pack.role_ledger is not None
    assert scored_pack.role_ledger.global_pick_index == 35


def test_context_stage_index_controls_commitment_and_rejects_conflicts() -> None:
    database = _card_database()
    profile = _context_profile()
    context = PickScoringContext(
        set_profile=profile,
        role_ledger=_context_ledger(profile=profile, database=database),
    )
    engine = PickEngine(scoring_context=context)

    scored_pack = engine.score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
    )

    assert scored_pack.commitment.pick_index == 35
    assert scored_pack.commitment.phase == "locked"

    with pytest.raises(ValueError, match="conflicts"):
        engine.score_pack(
            offered_grp_ids=(7,),
            card_database=database,
            pool_grp_ids=(1, 2),
            pick_index=34,
        )
    with pytest.raises(ValueError, match="conflicts"):
        engine.score_pack(
            offered_grp_ids=(7,),
            card_database=database,
            pool_grp_ids=(1, 2),
            global_pick_index=34,
        )
    with pytest.raises(ValueError, match="conflict"):
        engine.score_pack(
            offered_grp_ids=(7,),
            card_database=database,
            pool_grp_ids=(1, 2),
            pick_index=34,
            global_pick_index=35,
        )


@pytest.mark.parametrize(
    ("coordinate", "value"),
    [
        ("pack_number", 1),
        ("pick_number", 5),
        ("pick_index", 34),
        ("global_pick_index", 34),
        ("estimated_remaining_picks", 6),
    ],
)
def test_context_rejects_conflicting_stage_coordinates(
    coordinate: str,
    value: int,
) -> None:
    database = _card_database()
    profile = _context_profile()
    context = PickScoringContext(
        set_profile=profile,
        role_ledger=_context_ledger(profile=profile, database=database),
    )

    with pytest.raises(ValueError, match=coordinate):
        PickEngine(scoring_context=context).score_pack(
            offered_grp_ids=(7,),
            card_database=database,
            pool_grp_ids=(1, 2),
            **{coordinate: value},
        )


def test_context_accepts_matching_redundant_stage_coordinates() -> None:
    database = _card_database()
    profile = _context_profile()
    context = PickScoringContext(
        set_profile=profile,
        role_ledger=_context_ledger(profile=profile, database=database),
    )

    scored_pack = PickEngine(scoring_context=context).score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pack_number=2,
        pick_number=6,
        pick_index=35,
        global_pick_index=35,
        estimated_remaining_picks=7,
    )

    assert scored_pack.scoring_context is context
    assert scored_pack.commitment.pick_index == 35


def test_off_color_cards_are_penalized_and_marked_when_committed() -> None:
    engine = PickEngine(ratings_data=_ratings_data())

    scored_pack = engine.score_pack(
        offered_grp_ids=(7, 8),
        card_database=_card_database(),
        pool_grp_ids=(1, 2),
        pick_index=16,
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert scored_pack.commitment.inferred_pair == "WU"
    assert by_id[7].color_fit == "on-color"
    assert by_id[8].color_fit == "off-color"
    assert by_id[7].score > by_id[8].score


def test_pool_weight_uses_card_quality_when_inferring_pair() -> None:
    engine = PickEngine(ratings_data=_ratings_data())

    scored_pack = engine.score_pack(
        offered_grp_ids=(7,),
        card_database=_card_database(),
        pool_grp_ids=(9, 10, 11),
        pick_index=16,
    )

    assert scored_pack.commitment.inferred_pair == "WR"


def test_profile_ratings_drive_pool_pair_inference_and_public_context() -> None:
    database = CardDatabase(
        cards={
            1: _card(
                grp_id=1,
                name="White Pool Card",
                colors=("W",),
                set_code="TST",
                arena_id=1,
            ),
            2: _card(
                grp_id=2,
                name="Blue Pool Card",
                colors=("U",),
                set_code="TST",
                arena_id=2,
            ),
            3: _card(
                grp_id=3,
                name="Red Pool Card",
                colors=("R",),
                set_code="TST",
                arena_id=3,
            ),
        }
    )
    profile = _test_profile(
        total_samples=10_000,
        card_ratings=(
            _profile_card("ARENA_ID:1", 0.50),
            _profile_card("ARENA_ID:2", 0.50),
            _profile_card("ARENA_ID:3", 0.90),
        ),
    )
    context = build_pick_scoring_context(
        pool_grp_ids=(1, 2, 3),
        card_database=database,
        set_profile=profile,
        pick_index=16,
    )
    assert context is not None

    neutral = PickEngine().score_pack(
        offered_grp_ids=(1,),
        card_database=database,
        pool_grp_ids=(1, 2, 3),
        pick_index=16,
    )
    scored = PickEngine(set_profile=profile).score_pack(
        offered_grp_ids=(1,),
        card_database=database,
        pool_grp_ids=(1, 2, 3),
        pick_index=16,
    )

    assert context.role_ledger.likely_pair == "WR"
    assert neutral.commitment.inferred_pair == "WU"
    assert scored.commitment.inferred_pair == "WR"
    assert scored.scoring_context == context
    assert scored.cards[0].contextual_pair == "WR"


def test_open_pick_pair_win_rate_tiebreaker_prefers_higher_rate_pair() -> None:
    engine = PickEngine(ratings_data=_msh_pair_tiebreaker_data())

    scored_pack = engine.score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=3,
    )
    ranked_cards = rank_scored_cards(cards=scored_pack.cards, ranking_mode="score")
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert scored_pack.commitment.phase == "open"
    assert by_id[31].pair_tiebreaker_pair == "WU"
    assert by_id[31].pair_tiebreaker_win_rate == 0.606
    assert by_id[32].pair_tiebreaker_pair == "BR"
    assert by_id[32].pair_tiebreaker_win_rate == 0.539
    assert by_id[31].raw_score < by_id[32].raw_score
    assert scored_pack.cards[0].card.grp_id == 31
    assert ranked_cards[0].card.grp_id == 31


def test_profile_pair_performance_changes_open_pick_order_without_legacy_ratings() -> None:
    profile = _test_profile(
        total_samples=10_000,
        by_pair=(("WU", 10_000), ("BR", 10_000)),
        pairs=tuple(
            PairProfile(pair=pair, performance=_profile_rate(rate, samples=10_000))
            for pair, rate in (("WU", 0.90), ("BR", 0.40))
        ),
    )
    kwargs = {
        "offered_grp_ids": (32, 31),
        "card_database": _msh_pair_tiebreaker_database(),
        "pool_grp_ids": (20, 21),
        "pick_index": 3,
    }
    baseline = PickEngine().score_pack(**kwargs)
    scored = PickEngine(set_profile=profile).score_pack(**kwargs)
    by_id = {card.card.grp_id: card for card in scored.cards}

    assert baseline.cards[0].card.grp_id == 32
    assert scored.cards[0].card.grp_id == 31
    assert {
        grp_id: (card.pair_tiebreaker_pair, card.pair_tiebreaker_win_rate)
        for grp_id, card in by_id.items()
    } == {
        31: ("WU", pytest.approx(0.90)),
        32: ("BR", pytest.approx(0.40)),
    }

def test_open_pair_rate_tiebreaker_preserves_legacy_subpp_ordering_without_profile() -> None:
    engine = PickEngine(
        ratings_data=_msh_pair_tiebreaker_data(wu_rate=0.506, br_rate=0.500)
    )

    scored_pack = engine.score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=3,
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    higher_rate = by_id[31].pair_tiebreaker_win_rate
    lower_rate = by_id[32].pair_tiebreaker_win_rate
    assert higher_rate is not None
    assert lower_rate is not None
    assert 0 < higher_rate - lower_rate < 0.01
    assert by_id[31].raw_score < by_id[32].raw_score
    assert scored_pack.cards[0].card.grp_id == 31


def test_thin_shrunken_pair_margin_cannot_override_base_order() -> None:
    profile = _pair_shrinkage_profile(total_samples=1, pair_samples=1)

    scored_pack = _score_msh_pair_pack(profile=profile)
    by_id = {card.card.grp_id: card for card in scored_pack.cards}
    first_rate = by_id[31].pair_tiebreaker_win_rate
    second_rate = by_id[32].pair_tiebreaker_win_rate

    assert first_rate is not None
    assert second_rate is not None
    assert scored_pack.cards[0].card.grp_id == 32
    assert by_id[32].raw_score > by_id[31].raw_score
    assert abs(first_rate - second_rate) <= 0.01
    assert all(
        not any(reason.kind == "tiebreaker" for reason in card.rationale.reasons)
        for card in scored_pack.cards
    )



def test_supported_material_shrunken_pair_margin_breaks_close_pick() -> None:
    profile = _pair_shrinkage_profile(
        total_samples=10_000,
        pair_samples=10_000,
    )

    scored_pack = _score_msh_pair_pack(profile=profile)
    by_id = {card.card.grp_id: card for card in scored_pack.cards}
    higher_rate = by_id[31].pair_tiebreaker_win_rate
    lower_rate = by_id[32].pair_tiebreaker_win_rate

    assert higher_rate is not None
    assert lower_rate is not None
    assert scored_pack.cards[0].card.grp_id == 31
    assert by_id[31].raw_score < by_id[32].raw_score
    assert higher_rate - lower_rate > 0.01
    winner_tiebreaker = next(
        reason
        for reason in by_id[31].rationale.reasons
        if reason.kind == "tiebreaker"
    )
    assert winner_tiebreaker.contribution is None
    assert winner_tiebreaker.evidence == (
        "pair-rate comparison selected WU at 56.9% over BR at 50.0%"
    )
    assert not any(
        reason.kind == "tiebreaker" for reason in by_id[32].rationale.reasons
    )
    winner_tiebreaker = next(
        reason
        for reason in by_id[31].rationale.reasons
        if reason.kind == "tiebreaker"
    )
    assert winner_tiebreaker.contribution is None
    assert winner_tiebreaker.evidence == (
        "pair-rate comparison selected WU at 56.9% over BR at 50.0%"
    )
    assert not any(
        reason.kind == "tiebreaker" for reason in by_id[32].rationale.reasons
    )



def test_pair_win_rate_tiebreaker_does_not_override_colorless_card_score() -> None:
    engine = PickEngine(ratings_data=_msh_pair_tiebreaker_data())

    scored_pack = engine.score_pack(
        offered_grp_ids=(31, 33),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=3,
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert by_id[33].pair_tiebreaker_win_rate is None
    assert by_id[33].raw_score > by_id[31].raw_score
    assert scored_pack.cards[0].card.grp_id == 33
    assert all(
        not any(reason.kind == "tiebreaker" for reason in card.rationale.reasons)
        for card in scored_pack.cards
    )


def test_pair_win_rate_tiebreaker_does_not_override_later_pick_score() -> None:
    engine = PickEngine(ratings_data=_msh_pair_tiebreaker_data())

    scored_pack = engine.score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=6,
    )

    assert scored_pack.commitment.phase == "building"
    assert scored_pack.cards[0].card.grp_id == 32
    assert all(
        not any(reason.kind == "tiebreaker" for reason in card.rationale.reasons)
        for card in scored_pack.cards
    )


def test_pair_win_rate_tiebreaker_does_not_override_later_color_signal() -> None:
    engine = PickEngine(ratings_data=_msh_pair_tiebreaker_data(red_gih=0.55))

    scored_pack = engine.score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(21, 22),
        pick_index=16,
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert scored_pack.commitment.inferred_pair == "BR"
    assert by_id[31].color_fit == "off-color"
    assert by_id[32].color_fit == "on-color"
    assert scored_pack.cards[0].card.grp_id == 32
    assert all(
        not any(reason.kind == "tiebreaker" for reason in card.rationale.reasons)
        for card in scored_pack.cards
    )



def test_pair_win_rate_tiebreaker_does_not_override_clear_card_signal() -> None:
    engine = PickEngine(ratings_data=_msh_pair_tiebreaker_data(red_gih=0.62))

    scored_pack = engine.score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=3,
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert by_id[32].raw_score - by_id[31].raw_score > 3.0
    assert scored_pack.cards[0].card.grp_id == 32
    assert all(
        not any(reason.kind == "tiebreaker" for reason in card.rationale.reasons)
        for card in scored_pack.cards
    )


def test_locked_pair_uses_pair_filtered_rating_when_samples_are_adequate() -> None:
    engine = PickEngine(ratings_data=_ratings_data_with_pair_filter())

    scored_pack = engine.score_pack(
        offered_grp_ids=(12,),
        card_database=_card_database(),
        pool_grp_ids=(1, 2),
        pick_index=16,
    )

    assert scored_pack.commitment.inferred_pair == "WU"
    assert scored_pack.cards[0].base_rating == 0.64



def test_scoring_never_lazily_fetches_pair_cards_for_profile_and_legacy_cards() -> None:
    database = CardDatabase(
        cards={
            **_contextual_database().cards,
            2: replace(_contextual_database().cards[2], colors=("U",)),
        }
    )
    profile = _test_profile(
        maturity=ProfileMaturity.EARLY,
        card_ratings=(_profile_card("ARENA_ID:1", 0.70, samples=1),),
    )
    for legacy_cards, expected in (
        ({}, ("Prior*", None)),
        (
            {
                2: _stats(
                    grp_id=2,
                    name="Context Card 2",
                    color="W",
                    gih=0.58,
                    games_in_hand=900,
                )
            },
            ("Quick", 0.58),
        ),
    ):
        calls: list[str] = []

        def forbidden_loader(pair: str) -> SeventeenLandsFormatData | None:
            calls.append(pair)
            raise AssertionError(f"unexpected lazy pair-card load for {pair}")

        ratings = _ratings_variant(
            card_ratings=legacy_cards,
            pair_card_ratings_loader=forbidden_loader,
        )
        for pick_index, phase in ((3, "open"), (16, "locked")):
            scored = PickEngine(
                ratings_data=ratings,
                set_profile=profile,
            ).score_pack(
                offered_grp_ids=(1, 2),
                card_database=database,
                pool_grp_ids=(1, 2),
                pick_index=pick_index,
            )
            by_id = {card.card.grp_id: card for card in scored.cards}
            assert scored.commitment.phase == phase
            assert {
                grp_id: (card.source_label, card.rating.gih_win_rate)
                for grp_id, card in by_id.items()
            } == {
                1: ("Profile", pytest.approx(0.70)),
                2: (expected[0], expected[1]),
            }
        assert calls == []


def test_open_pair_rate_shrinks_toward_neutral_with_thin_profile_evidence() -> None:
    low_profile = _pair_shrinkage_profile(total_samples=1, pair_samples=1)
    high_profile = _pair_shrinkage_profile(
        total_samples=10_000,
        pair_samples=10_000,
    )

    low = _score_msh_pair_pack(profile=low_profile).cards[0]
    high = _score_msh_pair_pack(profile=high_profile).cards[0]

    assert low.pair_tiebreaker_win_rate is not None
    assert high.pair_tiebreaker_win_rate is not None
    assert abs(low.pair_tiebreaker_win_rate - 0.5) < abs(
        high.pair_tiebreaker_win_rate - 0.5
    )
    assert high.pair_tiebreaker_win_rate < 0.606


def test_open_pair_rate_shrinks_low_aggregate_games_toward_neutral() -> None:
    profile = _pair_shrinkage_profile(
        total_samples=10_000,
        pair_samples=10_000,
    )
    low = _score_msh_pair_pack(profile=profile, pair_games=1).cards[0]
    high = _score_msh_pair_pack(profile=profile, pair_games=10_000).cards[0]

    assert low.pair_tiebreaker_win_rate is not None
    assert high.pair_tiebreaker_win_rate is not None
    assert abs(low.pair_tiebreaker_win_rate - 0.5) < abs(
        high.pair_tiebreaker_win_rate - 0.5
    )


def test_open_pair_rate_influence_is_monotonic_and_bounded() -> None:
    rates = []
    for samples in (1, 100, 10_000):
        profile = _pair_shrinkage_profile(
            total_samples=samples,
            pair_samples=samples,
        )
        card = _score_msh_pair_pack(profile=profile).cards[0]
        assert card.pair_tiebreaker_win_rate is not None
        rates.append(card.pair_tiebreaker_win_rate)

    assert rates == sorted(rates)
    assert all(0.5 <= rate <= 0.606 for rate in rates)


def test_locked_pair_card_rate_shrinks_low_gih_samples_toward_global_card_rate() -> None:
    profile = _pair_shrinkage_profile(total_samples=10_000, pair_samples=10_000)
    low = _score_locked_pair_pack(profile=profile, pair_games_in_hand=10).cards[0]
    high = _score_locked_pair_pack(
        profile=profile,
        pair_games_in_hand=10_000,
    ).cards[0]

    assert 0.50 < low.base_rating < high.base_rating < 0.64


def test_locked_pair_keeps_raw_rating_metadata_when_base_is_shrunk() -> None:
    card = _score_locked_pair_pack(
        profile=_pair_shrinkage_profile(total_samples=10_000, pair_samples=10_000),
        pair_gih=0.90,
        pair_games_in_hand=10,
    ).cards[0]

    assert card.rating.gih_win_rate == 0.90
    assert card.rating.sample_counts.games_in_hand == 10
    assert card.rating.letter_grade == "C"
    assert card.rating.metadata.source_format == QUICK_DRAFT_FORMAT
    assert card.rating.metadata.fallback_reason is None
    assert 0.50 < card.base_rating < 0.90


def test_lower_profile_maturity_and_confidence_reduce_pair_card_influence() -> None:
    weak_profile = _pair_shrinkage_profile(
        total_samples=10_000,
        pair_samples=10_000,
        confidence=0.25,
        maturity=ProfileMaturity.EARLY,
    )
    strong_profile = _pair_shrinkage_profile(
        total_samples=10_000,
        pair_samples=10_000,
    )
    weak = _score_locked_pair_pack(
        profile=weak_profile,
        pair_games_in_hand=10_000,
    ).cards[0]
    strong = _score_locked_pair_pack(
        profile=strong_profile,
        pair_games_in_hand=10_000,
    ).cards[0]

    assert abs(weak.base_rating - 0.50) < abs(strong.base_rating - 0.50)


def test_thin_pair_card_evidence_cannot_dominate_clear_global_quality() -> None:
    profile = _pair_shrinkage_profile(total_samples=10_000, pair_samples=10_000)
    scored_pack = _score_locked_pair_pack(
        profile=profile,
        pair_gih=0.99,
        pair_games_in_hand=10,
        offered_grp_ids=(12, 9),
    )

    assert scored_pack.cards[0].card.grp_id == 9
    pair_card = next(
        card for card in scored_pack.cards if card.card.grp_id == 12
    )
    assert pair_card.base_rating < 0.62


def test_profile_pair_omissions_do_not_remove_canonical_pair_candidates() -> None:
    ratings_data = _msh_pair_tiebreaker_data()
    ratings_data = replace(
        ratings_data,
        primary=replace(
            ratings_data.primary,
            pair_win_rates={
                pair: ColorPairWinRate(
                    pair=pair,
                    wins=60,
                    games=100,
                    win_rate=0.6,
                )
                for pair in COLOR_PAIRS
            },
        ),
    )
    pair_grp_ids = tuple(range(100, 100 + len(COLOR_PAIRS)))
    base_database = _msh_pair_tiebreaker_database()
    database = CardDatabase(
        cards={
            **base_database.cards,
            **{
                grp_id: _card(
                    grp_id=grp_id,
                    name=f"{pair} pair card",
                    colors=tuple(pair),
                )
                for grp_id, pair in zip(pair_grp_ids, COLOR_PAIRS)
            },
        }
    )
    profile = _pair_shrinkage_profile(total_samples=1, pair_samples=1)
    assert profile.pair("BR") is None

    scored_pack = PickEngine(
        ratings_data=ratings_data,
        set_profile=profile,
    ).score_pack(
        offered_grp_ids=pair_grp_ids,
        card_database=database,
        pool_grp_ids=(20, 21),
        pick_index=3,
    )

    scored_by_id = {card.card.grp_id: card for card in scored_pack.cards}
    for grp_id, pair in zip(pair_grp_ids, COLOR_PAIRS):
        card = scored_by_id[grp_id]
        assert card.pair_tiebreaker_pair == pair
        assert card.pair_tiebreaker_win_rate is not None
        if pair != "WU":
            assert card.pair_tiebreaker_win_rate == 0.5


def test_supported_single_pip_bomb_is_marked_as_a_splash_and_can_win_pick() -> None:
    engine = PickEngine(ratings_data=_splash_ratings_data())
    database = _splash_card_database()

    scored_pack = engine.score_pack(
        offered_grp_ids=(105, 108),
        card_database=database,
        pool_grp_ids=(101, 102, 101, 102, 103, 104),
        pick_index=10,
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert scored_pack.commitment.inferred_pair == "WU"
    assert by_id[105].color_fit == "splash-ready"
    assert by_id[105].splash.splash_color == "R"
    assert by_id[105].splash.available_sources == 3
    assert by_id[105].splash.required_sources == 3
    assert scored_pack.cards[0].card.grp_id == 105
    splash_reason = next(
        reason
        for reason in by_id[105].rationale.reasons
        if reason.kind == "splash"
    )
    assert splash_reason.contribution is None
    assert splash_reason.evidence == by_id[105].splash.reasons
    rendered = "\n".join(
        format_pack_offered_event(
            event=PackOfferedEvent(
                event_name="QuickDraft_TST_20260727",
                set_code="TST",
                pack_number=0,
                pick_number=9,
                offered_grp_ids=(105, 108),
                pool_grp_ids=(101, 102, 101, 102, 103, 104),
                account_id="account",
            ),
            card_database=database,
            scored_pack=scored_pack,
        )
    )
    assert "Splash R" in rendered


def test_freely_available_basic_does_not_block_splash_candidate() -> None:
    scored_pack = PickEngine(ratings_data=_splash_ratings_data()).score_pack(
        offered_grp_ids=(121, 105),
        card_database=_splash_card_database(),
        pool_grp_ids=(101, 102, 101, 102, 103, 104),
        pick_index=10,
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert by_id[121].score == 0
    assert by_id[105].color_fit == "splash-ready"
    assert by_id[105].splash.score_advantage is None


def test_disabling_splash_treats_same_bomb_as_an_ordinary_off_color_card() -> None:
    engine = PickEngine(
        ratings_data=_splash_ratings_data(),
        splash_enabled=False,
    )

    scored_pack = engine.score_pack(
        offered_grp_ids=(105, 108),
        card_database=_splash_card_database(),
        pool_grp_ids=(101, 102, 101, 102, 103, 104),
        pick_index=10,
    )
    red_bomb = next(
        card for card in scored_pack.cards if card.card.grp_id == 105
    )

    assert scored_pack.splash_state.enabled is False
    assert red_bomb.color_fit == "off-color"
    assert red_bomb.splash.reasons == ("splashing is disabled",)


def test_active_splash_rejects_a_second_third_color_and_double_pips() -> None:
    engine = PickEngine(ratings_data=_splash_ratings_data())

    scored_pack = engine.score_pack(
        offered_grp_ids=(106, 107, 109),
        card_database=_splash_card_database(),
        pool_grp_ids=(101, 102, 101, 102, 103, 104, 110, 105),
        pick_index=12,
    )
    by_id = {card.card.grp_id: card for card in scored_pack.cards}

    assert scored_pack.splash_state.active_color == "R"
    assert by_id[106].color_fit == "splash-ready"
    assert by_id[106].splash.required_sources == 4
    assert by_id[107].color_fit == "off-color"
    assert by_id[107].splash.reasons == ("the active splash color is R",)
    assert by_id[109].color_fit == "off-color"
    assert by_id[109].splash.reasons == ("card has too many off-color mana pips",)


def test_supported_existing_splash_color_beats_stronger_unsupported_color() -> None:
    scored_pack = PickEngine(ratings_data=_splash_ratings_data()).score_pack(
        offered_grp_ids=(108,),
        card_database=_splash_card_database(),
        pool_grp_ids=(101, 102, 101, 102, 103, 104, 105, 107),
        pick_index=12,
    )

    assert scored_pack.splash_state.active_color == "R"


def test_fixing_land_is_marked_when_it_completes_active_splash_sources() -> None:
    scored_pack = PickEngine(ratings_data=_splash_ratings_data()).score_pack(
        offered_grp_ids=(103, 108),
        card_database=_splash_card_database(),
        pool_grp_ids=(101, 102, 101, 102, 104, 105),
        pick_index=12,
    )
    fixing_land = next(card for card in scored_pack.cards if card.card.grp_id == 103)
    splash_reason = next(
        reason
        for reason in fixing_land.rationale.reasons
        if reason.kind == "splash"
    )
    assert splash_reason.contribution is None
    assert splash_reason.evidence == fixing_land.splash.reasons

    assert fixing_land.color_fit == "splash-fixer"
    assert fixing_land.splash.splash_color == "R"
    assert fixing_land.splash.fixing_sources == 2
    assert fixing_land.splash.planned_basic_sources == 1
    assert fixing_land.splash.available_sources == 3
    assert fixing_land.splash.required_sources == 3


def test_drafted_basic_is_not_counted_as_extra_splash_fixing() -> None:
    scored_pack = PickEngine(ratings_data=_splash_ratings_data()).score_pack(
        offered_grp_ids=(105, 108),
        card_database=_splash_card_database(),
        pool_grp_ids=(101, 102, 101, 102, 103, 121),
        pick_index=10,
    )
    red_bomb = next(card for card in scored_pack.cards if card.card.grp_id == 105)

    assert scored_pack.splash_state.fixing_for(color="R") == 1
    assert red_bomb.color_fit == "splash-speculative"
    assert red_bomb.splash.available_sources == 2


def test_unsupported_a_grade_bomb_is_speculative_only_before_color_lock() -> None:
    engine = PickEngine(ratings_data=_splash_ratings_data())
    database = _splash_card_database()
    pool = (101, 102, 101, 102)

    building = engine.score_pack(
        offered_grp_ids=(105, 108),
        card_database=database,
        pool_grp_ids=pool,
        pick_index=10,
    )
    locked = engine.score_pack(
        offered_grp_ids=(105, 108),
        card_database=database,
        pool_grp_ids=pool,
        pick_index=16,
    )
    building_bomb = next(card for card in building.cards if card.card.grp_id == 105)
    locked_bomb = next(card for card in locked.cards if card.card.grp_id == 105)

    assert building_bomb.color_fit == "splash-speculative"
    assert building_bomb.splash.available_sources == 1
    assert building_bomb.splash.required_sources == 3
    assert locked_bomb.color_fit == "off-color"
    assert "speculative splashes are disabled after color lock" in (
        locked_bomb.splash.reasons
    )
    splash_reason = next(
        reason
        for reason in building_bomb.rationale.reasons
        if reason.kind == "splash"
    )
    assert splash_reason.contribution is None
    assert splash_reason.evidence == building_bomb.splash.reasons


def test_aggressive_pool_does_not_take_an_unsupported_speculative_splash() -> None:
    scored_pack = PickEngine(ratings_data=_splash_ratings_data()).score_pack(
        offered_grp_ids=(105, 108),
        card_database=_splash_card_database(),
        pool_grp_ids=(101, 102, 101, 102, 101, 102, 101, 102),
        pick_index=10,
    )
    red_bomb = next(card for card in scored_pack.cards if card.card.grp_id == 105)

    assert scored_pack.splash_state.aggressive is True
    assert red_bomb.color_fit == "off-color"
    assert "aggressive pools require supported exceptional splashes" in (
        red_bomb.splash.reasons
    )


def test_hybrid_symbol_payable_with_primary_color_does_not_require_splash() -> None:
    hybrid_card = _card(
        grp_id=120,
        name="Hybrid Primary Card",
        colors=("W", "R"),
        mana_cost="{2}{W/R}",
    )

    assert card_is_castable_in_pair(card=hybrid_card, base_pair="WU") is True
    assert splash_requirement(card=hybrid_card, base_pair="WU") == (None, 0)


def _ratings_data() -> SeventeenLandsData:
    return SeventeenLandsData(
        set_code="TST",
        requested_format=QUICK_DRAFT_FORMAT,
        primary=SeventeenLandsFormatData(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            fetched_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
            card_ratings={
                1: _stats(grp_id=1, name="Quick Bomb", gih=0.62, games_in_hand=900),
                2: _stats(
                    grp_id=2,
                    name="Thin Quick Card",
                    gih=None,
                    games_in_hand=120,
                    alsa=5.5,
                ),
                3: _stats(grp_id=3, name="Quick Filler", gih=0.50, games_in_hand=800),
                4: _stats(
                    grp_id=4,
                    name="Early Prior Card",
                    gih=None,
                    games_in_hand=0,
                    alsa=1.0,
                ),
                5: _stats(
                    grp_id=5,
                    name="Late Prior Card",
                    gih=None,
                    games_in_hand=0,
                    alsa=8.0,
                ),
                6: _stats(
                    grp_id=6,
                    name="Unknown Prior Card",
                    gih=None,
                    games_in_hand=0,
                    alsa=None,
                ),
                7: _stats(
                    grp_id=7,
                    name="White Test Card",
                    color="W",
                    gih=0.55,
                    games_in_hand=900,
                ),
                8: _stats(
                    grp_id=8,
                    name="Red Test Card",
                    color="R",
                    gih=0.55,
                    games_in_hand=900,
                ),
                9: _stats(
                    grp_id=9,
                    name="White Bomb",
                    color="W",
                    gih=0.65,
                    games_in_hand=900,
                ),
                10: _stats(
                    grp_id=10,
                    name="Blue Filler",
                    color="U",
                    gih=0.50,
                    games_in_hand=900,
                ),
                11: _stats(
                    grp_id=11,
                    name="Red Playable",
                    color="R",
                    gih=0.55,
                    games_in_hand=900,
                ),
                12: _stats(
                    grp_id=12,
                    name="Pair Filtered Card",
                    color="W",
                    gih=0.50,
                    games_in_hand=900,
                ),
            },
            pair_win_rates=_pair_win_rates(),
        ),
        fallback=SeventeenLandsFormatData(
            set_code="TST",
            event_format=PREMIER_DRAFT_FORMAT,
            fetched_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
            card_ratings={
                2: _stats(
                    grp_id=2,
                    name="Thin Quick Card",
                    gih=0.58,
                    games_in_hand=700,
                    alsa=4.0,
                ),
            },
            pair_win_rates=_pair_win_rates(),
        ),
        thin_sample_minimum=500,
    )


def _rated_basic_and_zero_data() -> SeventeenLandsData:
    data = _ratings_data()
    primary = replace(
        data.primary,
        card_ratings={
            **data.primary.card_ratings,
            120: _stats(
                grp_id=120,
                name="Draftable Zero",
                color="W",
                gih=-1.0,
                games_in_hand=900,
            ),
            121: _stats(
                grp_id=121,
                name="Mountain",
                color="C",
                gih=0.70,
                games_in_hand=900,
            ),
        },
    )
    return replace(data, primary=primary)


def _ratings_data_with_pair_filter(
    *,
    pair_gih: float = 0.64,
    pair_games_in_hand: int = 900,
) -> SeventeenLandsData:
    data = _ratings_data()
    pair_data = SeventeenLandsFormatData(
        set_code="TST",
        event_format=QUICK_DRAFT_FORMAT,
        fetched_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        card_ratings={
            12: _stats(
                grp_id=12,
                name="Pair Filtered Card",
                color="W",
                gih=pair_gih,
                games_in_hand=pair_games_in_hand,
            ),
        },
        pair_win_rates=_pair_win_rates(),
    )
    return SeventeenLandsData(
        set_code=data.set_code,
        requested_format=data.requested_format,
        primary=data.primary,
        fallback=data.fallback,
        pair_card_ratings={"WU": pair_data},
        thin_sample_minimum=data.thin_sample_minimum,
    )


def _msh_pair_tiebreaker_data(
    *,
    red_gih: float = 0.552,
    pair_games: int = 1000,
    wu_rate: float = 0.606,
    br_rate: float = 0.539,
) -> SeventeenLandsData:
    return SeventeenLandsData(
        set_code="MSH",
        requested_format=QUICK_DRAFT_FORMAT,
        primary=SeventeenLandsFormatData(
            set_code="MSH",
            event_format=QUICK_DRAFT_FORMAT,
            fetched_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
            card_ratings={
                20: _stats(
                    grp_id=20,
                    name="White Start",
                    color="W",
                    gih=0.55,
                    games_in_hand=900,
                ),
                21: _stats(
                    grp_id=21,
                    name="Black Start",
                    color="B",
                    gih=0.55,
                    games_in_hand=900,
                ),
                22: _stats(
                    grp_id=22,
                    name="Red Start",
                    color="R",
                    gih=0.55,
                    games_in_hand=900,
                ),
                31: _stats(
                    grp_id=31,
                    name="Blue WU Lane Card",
                    color="U",
                    gih=0.55,
                    games_in_hand=900,
                ),
                32: _stats(
                    grp_id=32,
                    name="Red BR Lane Card",
                    color="R",
                    gih=red_gih,
                    games_in_hand=900,
                ),
                33: _stats(
                    grp_id=33,
                    name="Colorless Close Card",
                    color="C",
                    gih=0.552,
                    games_in_hand=900,
                ),
            },
            pair_win_rates=_msh_pair_win_rates(
                games=pair_games,
                wu_rate=wu_rate,
                br_rate=br_rate,
            ),
        ),
        fallback=None,
        thin_sample_minimum=500,
    )


def _splash_ratings_data() -> SeventeenLandsData:
    card_ratings = {
        101: _stats(
            grp_id=101,
            name="White Base Card",
            color="W",
            gih=0.55,
            games_in_hand=900,
        ),
        102: _stats(
            grp_id=102,
            name="Blue Base Card",
            color="U",
            gih=0.55,
            games_in_hand=900,
        ),
        105: _stats(
            grp_id=105,
            name="Red Splash Bomb",
            color="R",
            gih=0.70,
            games_in_hand=900,
        ),
        106: _stats(
            grp_id=106,
            name="Second Red Splash Bomb",
            color="R",
            gih=0.69,
            games_in_hand=900,
        ),
        107: _stats(
            grp_id=107,
            name="Green Splash Bomb",
            color="G",
            gih=0.72,
            games_in_hand=900,
        ),
        108: _stats(
            grp_id=108,
            name="White Solid Card",
            color="W",
            gih=0.58,
            games_in_hand=900,
        ),
        109: _stats(
            grp_id=109,
            name="Double Red Bomb",
            color="R",
            gih=0.71,
            games_in_hand=900,
        ),
        121: _stats(
            grp_id=121,
            name="Mountain",
            color="C",
            gih=0.70,
            games_in_hand=900,
        ),
    }
    card_ratings.update(
        {
            grp_id: _stats(
                grp_id=grp_id,
                name=f"Distribution Card {grp_id}",
                color="B",
                gih=0.50 + ((grp_id - 200) * 0.003),
                games_in_hand=900,
            )
            for grp_id in range(200, 220)
        }
    )
    return SeventeenLandsData(
        set_code="TST",
        requested_format=QUICK_DRAFT_FORMAT,
        primary=SeventeenLandsFormatData(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            fetched_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
            card_ratings=card_ratings,
            pair_win_rates=_pair_win_rates(),
        ),
        fallback=None,
        thin_sample_minimum=500,
    )


def _stats(
    *,
    grp_id: int,
    name: str,
    gih: float | None,
    games_in_hand: int,
    alsa: float | None = 4.5,
    color: str = "W",
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
    return {
        "WU": ColorPairWinRate(pair="WU", wins=60, games=100, win_rate=0.6),
    }


def _msh_pair_win_rates(
    *,
    games: int = 1000,
    wu_rate: float = 0.606,
    br_rate: float = 0.539,
) -> dict[str, ColorPairWinRate]:
    return {
        "WU": ColorPairWinRate(
            pair="WU",
            wins=round(wu_rate * games),
            games=games,
            win_rate=wu_rate,
        ),
        "BR": ColorPairWinRate(
            pair="BR",
            wins=round(br_rate * games),
            games=games,
            win_rate=br_rate,
        ),
    }


def _card_database() -> CardDatabase:
    return CardDatabase(
        cards={
            1: _card(grp_id=1, name="Quick Bomb", colors=("W",)),
            2: _card(grp_id=2, name="Thin Quick Card", colors=("U",)),
            3: _card(grp_id=3, name="Quick Filler", colors=("W",)),
            4: _card(grp_id=4, name="Early Prior Card", colors=("W",)),
            5: _card(grp_id=5, name="Late Prior Card", colors=("W",)),
            6: _card(grp_id=6, name="Unknown Prior Card", colors=("W",)),
            7: _card(grp_id=7, name="White Test Card", colors=("W",)),
            8: _card(grp_id=8, name="Red Test Card", colors=("R",)),
            9: _card(grp_id=9, name="White Bomb", colors=("W",)),
            10: _card(grp_id=10, name="Blue Filler", colors=("U",)),
            11: _card(grp_id=11, name="Red Playable", colors=("R",)),
            12: _card(grp_id=12, name="Pair Filtered Card", colors=("W",)),
            13: _card(
                grp_id=13,
                name="Arena Plains",
                colors=(),
                types=("Basic Land — Plains",),
                mana_cost=None,
            ),
            14: _card(
                grp_id=14,
                name="Wastes",
                colors=(),
                types=("Basic Land",),
                mana_cost=None,
            ),
            15: _card(
                grp_id=15,
                name="Snow-Covered Plains",
                colors=(),
                types=("Basic Snow Land — Plains",),
                mana_cost=None,
            ),
            16: _card(
                grp_id=16,
                name="Prairie Sanctuary",
                colors=(),
                types=("Land — Plains",),
                mana_cost=None,
            ),
        }
    )


def _msh_pair_tiebreaker_database() -> CardDatabase:
    return CardDatabase(
        cards={
            20: _card(grp_id=20, name="White Start", colors=("W",)),
            21: _card(grp_id=21, name="Black Start", colors=("B",)),
            22: _card(grp_id=22, name="Red Start", colors=("R",)),
            31: _card(grp_id=31, name="Blue WU Lane Card", colors=("U",)),
            32: _card(grp_id=32, name="Red BR Lane Card", colors=("R",)),
            33: _card(grp_id=33, name="Colorless Close Card", colors=()),
        }
    )


def _score_msh_pair_pack(
    *,
    profile: SetProfile,
    pair_games: int = 1000,
) -> ScoredPack:
    return PickEngine(
        ratings_data=_msh_pair_tiebreaker_data(pair_games=pair_games),
        set_profile=profile,
    ).score_pack(
        offered_grp_ids=(31, 32),
        card_database=_msh_pair_tiebreaker_database(),
        pool_grp_ids=(20, 21),
        pick_index=3,
    )


def _score_locked_pair_pack(
    *,
    profile: SetProfile,
    pair_gih: float = 0.64,
    pair_games_in_hand: int = 900,
    offered_grp_ids: tuple[int, ...] = (12,),
) -> ScoredPack:
    return PickEngine(
        ratings_data=_ratings_data_with_pair_filter(
            pair_gih=pair_gih,
            pair_games_in_hand=pair_games_in_hand,
        ),
        set_profile=profile,
    ).score_pack(
        offered_grp_ids=offered_grp_ids,
        card_database=_card_database(),
        pool_grp_ids=(1, 2),
        pick_index=16,
    )


def _splash_card_database() -> CardDatabase:
    return CardDatabase(
        cards={
            101: _card(grp_id=101, name="White Base Card", colors=("W",)),
            102: _card(grp_id=102, name="Blue Base Card", colors=("U",)),
            103: _card(
                grp_id=103,
                name="Red Fixing Land One",
                colors=(),
                types=("Land",),
                mana_cost=None,
                produced_mana=("R",),
            ),
            104: _card(
                grp_id=104,
                name="Red Fixing Land Two",
                colors=(),
                types=("Land",),
                mana_cost=None,
                produced_mana=("R",),
            ),
            110: _card(
                grp_id=110,
                name="Red Fixing Land Three",
                colors=(),
                types=("Land",),
                mana_cost=None,
                produced_mana=("R",),
            ),
            120: _card(
                grp_id=120,
                name="Draftable Zero",
                colors=("W",),
            ),
            121: _card(
                grp_id=121,
                name="Mountain",
                colors=(),
                types=("Basic Land — Mountain",),
                mana_cost=None,
                mana_value=0.0,
                produced_mana=("R",),
            ),
            105: _card(
                grp_id=105,
                name="Red Splash Bomb",
                colors=("R",),
                mana_cost="{4}{R}",
            ),
            106: _card(
                grp_id=106,
                name="Second Red Splash Bomb",
                colors=("R",),
                mana_cost="{3}{R}",
            ),
            107: _card(
                grp_id=107,
                name="Green Splash Bomb",
                colors=("G",),
                mana_cost="{4}{G}",
            ),
            108: _card(
                grp_id=108,
                name="White Solid Card",
                colors=("W",),
                mana_cost="{2}{W}",
            ),
            109: _card(
                grp_id=109,
                name="Double Red Bomb",
                colors=("R",),
                mana_cost="{3}{R}{R}",
            ),
        }
    )


def _card(
    *,
    grp_id: int,
    name: str,
    colors: tuple[str, ...],
    types: tuple[str, ...] = ("Creature",),
    mana_cost: str | None = "{2}",
    mana_value: float | None = 2.0,
    produced_mana: tuple[str, ...] = (),
    set_code: str | None = None,
    collector_number: str | None = None,
    arena_id: int | None = None,
    oracle_id: str | None = None,
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
        set_code=set_code,
        collector_number=collector_number,
        arena_id=arena_id,
        oracle_id=oracle_id,
    )


def _profile_rate(
    value: float,
    *,
    samples: int = 100,
    prior_value: float = 0.50,
) -> RateEstimate:
    return RateEstimate(
        raw_value=value,
        value=value,
        samples=samples,
        prior_value=prior_value,
        source="test",
    )


def _profile_card(key: str, value: float, *, samples: int = 100) -> CardRating:
    return CardRating(
        card_key=key,
        gih_win_rate=_profile_rate(value, samples=samples),
    )


def _test_profile(
    *,
    set_code: str = "TST",
    maturity: ProfileMaturity = ProfileMaturity.MATURE,
    total_samples: int | None = 1,
    by_pair: tuple[tuple[str, int], ...] = (("WU", 1),),
    pairs: tuple[PairProfile, ...] = (),
    card_ratings: tuple[CardRating, ...] = (),
    role_profile: CompiledRoleProfile | None = None,
    schema_version: int = 1,
) -> SetProfile:
    return SetProfile(
        set_code=set_code,
        event_format="quickdraft",
        profile_version="test",
        generated_at="1970-01-01T00:00:00+00:00",
        source=SourceMetadata(provider="test"),
        maturity=maturity,
        samples=(
            None
            if total_samples is None
            else SampleSummary(total=total_samples, by_pair=by_pair)
        ),
        confidence=1.0,
        pairs=pairs,
        role_profile=role_profile,
        card_ratings=card_ratings,
        schema_version=schema_version,
    )


def _ratings_variant(
    *,
    card_ratings: dict[int, SeventeenCardStats],
    pair_card_ratings_loader: Callable[[str], SeventeenLandsFormatData | None]
    | None = None,
) -> SeventeenLandsData:
    base = _ratings_data()
    return replace(
        base,
        primary=replace(
            base.primary,
            card_ratings=card_ratings,
            pair_win_rates={},
        ),
        fallback=None,
        pair_card_ratings_loader=pair_card_ratings_loader,
    )


def test_pick_scoring_context_validates_its_pre_pick_contract() -> None:
    profile = _context_profile()
    ledger = _context_ledger(profile=profile, database=_card_database())
    context = PickScoringContext(set_profile=profile, role_ledger=ledger)
    assert ledger.profile_fingerprint == profile.fingerprint

    assert tuple(field.name for field in fields(PickScoringContext)) == (
        "set_profile",
        "role_ledger",
    )
    assert context.set_profile is profile
    assert context.role_ledger is ledger
    assert context.stage is ledger.stage
    assert not hasattr(context, "profile")
    assert not hasattr(context, "ledger")
    with pytest.raises(FrozenInstanceError):
        context.set_profile = profile
    with pytest.raises(TypeError):
        PickScoringContext(
            set_profile=profile,
            role_ledger=ledger,
            ledger=ledger,
        )
    with pytest.raises(TypeError, match="must be a SetProfile"):
        PickScoringContext(set_profile=object(), role_ledger=ledger)
    with pytest.raises(TypeError, match="must be a PoolRoleLedger"):
        PickScoringContext(set_profile=profile, role_ledger=object())
    with pytest.raises(ValueError, match="pre-pick projection"):
        PickScoringContext(
            set_profile=profile,
            role_ledger=replace(
                ledger,
                mode=COMPLETED_POOL,
                stage=None,
            ),
        )
    with pytest.raises(TypeError, match="stage must be a LedgerStage"):
        PickScoringContext(
            set_profile=profile,
            role_ledger=replace(ledger, stage=object()),
        )
    different_profile = replace(profile, confidence=0.75)
    with pytest.raises(ValueError, match="fingerprint"):
        PickScoringContext(
            set_profile=different_profile,
            role_ledger=ledger,
        )
    with pytest.raises(ValueError, match="does not match"):
        PickScoringContext(
            set_profile=profile,
            role_ledger=replace(ledger, profile_source="profile:early"),
        )


def test_build_pick_scoring_context_preserves_full_stage_provenance() -> None:
    profile = _context_profile()
    database = _card_database()
    full = build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        set_profile=profile,
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
    )
    pick_only = build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        set_profile=profile,
        pick_index=35,
    )

    assert full is not None
    assert pick_only is not None
    assert full.stage == pick_only.stage
    assert full.stage.pack_number == 2
    assert full.stage.pick_number == 6
    assert full.stage.global_pick_index == 35
    assert full.stage.estimated_remaining_picks == 7
    assert full.role_ledger.profile_source == "profile:mature"
    assert full.role_ledger.profile_fingerprint == profile.fingerprint
    assert full.role_ledger.likely_pair == "WU"


def test_build_pick_scoring_context_keeps_explicit_context_authoritative() -> None:
    profile = _context_profile()
    database = _card_database()
    context = build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        set_profile=profile,
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
    )

    assert build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        set_profile=profile,
        scoring_context=context,
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
    ) is context
    with pytest.raises(ValueError, match="pack_number"):
        build_pick_scoring_context(
            pool_grp_ids=(),
            card_database=database,
            scoring_context=context,
            pack_number=1,
        )
    with pytest.raises(ValueError, match="pick_index and global_pick_index"):
        build_pick_scoring_context(
            pool_grp_ids=(),
            card_database=database,
            scoring_context=context,
            pick_index=34,
            global_pick_index=35,
        )


def test_call_scoring_context_overrides_constructor_profile_and_normalization() -> None:
    database = _contextual_database()
    constructor_profile = _test_profile(
        maturity=ProfileMaturity.EARLY,
        card_ratings=(_profile_card("ARENA_ID:1", 0.60),),
    )
    call_profile = _test_profile(
        maturity=ProfileMaturity.EARLY,
        card_ratings=(_profile_card("ARENA_ID:1", 0.90),),
    )
    context = build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        set_profile=call_profile,
        pick_index=35,
    )
    assert context is not None

    engine = PickEngine(set_profile=constructor_profile)
    constructor = engine.score_pack(
        offered_grp_ids=(1,),
        card_database=database,
    )
    scored = engine.score_pack(
        offered_grp_ids=(1,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=35,
        scoring_context=context,
    )

    assert constructor.cards[0].rating.gih_win_rate == pytest.approx(0.60)
    assert constructor.normalization.lower_rating == pytest.approx(0.50)
    assert constructor.normalization.upper_rating == pytest.approx(0.60)
    assert scored.cards[0].rating.gih_win_rate == pytest.approx(0.90)
    assert scored.cards[0].source_label == "Profile"
    assert scored.normalization.lower_rating == pytest.approx(0.20)
    assert scored.normalization.upper_rating == pytest.approx(0.90)
    assert scored.scoring_context is context


def test_generic_profile_is_normalized_to_no_context_with_stage_ledger() -> None:
    database = _card_database()
    generic = SetProfile.generic(set_code="TST", event_format="quickdraft")

    assert build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        set_profile=generic,
        pick_index=35,
    ) is None

    engine = PickEngine(set_profile=generic)
    assert engine.set_profile is None
    scored_pack = engine.score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=35,
    )

    assert scored_pack.scoring_context is None
    assert scored_pack.role_ledger is not None
    assert scored_pack.role_ledger.profile_source == "generic"
    assert scored_pack.role_ledger.stage is not None
    assert scored_pack.role_ledger.stage.global_pick_index == 35
    card = scored_pack.cards[0]
    assert card.contextual_breakdown == ContextualScoreBreakdown()
    assert card.contextual_evidence == ()
    assert card.contextual_pair is None
    assert card.contextual_theme is None
    assert card.contextual_profile_maturity is None
    assert card.contextual_profile_confidence is None


def test_explicit_mature_profile_identity_is_preserved_for_scoring_context() -> None:
    profile = _context_profile()
    database = _card_database()

    engine = PickEngine(set_profile=profile)
    context = build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        set_profile=profile,
        pick_index=35,
    )
    scored_pack = engine.score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=35,
    )

    assert engine.set_profile is profile
    assert context is not None
    assert context.set_profile is profile
    assert scored_pack.scoring_context is not None
    assert scored_pack.scoring_context.set_profile is profile


def test_build_pick_scoring_context_keeps_no_profile_generic_compatibility() -> None:
    database = _card_database()
    assert build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        pick_index=35,
    ) is None

    scored_pack = PickEngine().score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=35,
    )
    assert scored_pack.scoring_context is None
    assert scored_pack.role_ledger is not None
    assert scored_pack.role_ledger.stage is not None
    assert scored_pack.role_ledger.stage.global_pick_index == 35


def test_build_pick_scoring_context_uses_custom_pair_inference_config() -> None:
    profile = _context_profile()
    database = _card_database()
    custom_config = PickEngineConfig(minimum_pair_colors=3)
    custom_context = build_pick_scoring_context(
        pool_grp_ids=(1, 2),
        card_database=database,
        set_profile=profile,
        config=custom_config,
        pick_index=35,
    )
    assert custom_context is not None

    scored_pack = PickEngine(
        config=custom_config,
        set_profile=profile,
    ).score_pack(
        offered_grp_ids=(7,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pick_index=35,
    )

    assert scored_pack.scoring_context == custom_context
    assert scored_pack.role_ledger == custom_context.role_ledger
    assert custom_context.role_ledger.likely_pair == "WU"
    assert scored_pack.commitment.inferred_pair is None
    assert scored_pack.commitment.phase == "open"
    assert scored_pack.cards[0].contextual_pair == "WU"


def test_scoring_context_preserves_generic_scores_but_exposes_context() -> None:
    database = _card_database()
    ratings_data = _ratings_data()
    profile = _context_profile()
    context = PickScoringContext(
        set_profile=profile,
        role_ledger=_context_ledger(profile=profile, database=database),
    )
    baseline = PickEngine(ratings_data=ratings_data).score_pack(
        offered_grp_ids=(4, 3, 2, 1),
        card_database=database,
    )
    through_constructor = PickEngine(
        ratings_data=ratings_data,
        scoring_context=context,
    ).score_pack(
        offered_grp_ids=(4, 3, 2, 1),
        card_database=database,
    )
    through_call = PickEngine(ratings_data=ratings_data).score_pack(
        offered_grp_ids=(4, 3, 2, 1),
        card_database=database,
        scoring_context=context,
    )

    assert through_constructor.scoring_context is context
    assert through_call.scoring_context is context
    assert tuple(card.score for card in baseline.cards) == (90, 67, 67, 22)
    assert tuple(card.score for card in through_constructor.cards) == (
        90,
        67,
        67,
        22,
    )
    assert tuple(card.card.grp_id for card in through_constructor.cards) == tuple(
        card.card.grp_id for card in baseline.cards
    )
    assert tuple(card.score for card in through_call.cards) == (
        90,
        67,
        67,
        22,
    )
    assert tuple(card.score for card in through_call.cards) == tuple(
        card.score for card in baseline.cards
    )
    assert through_constructor.commitment.pick_index == context.stage.global_pick_index
    assert through_call.commitment.pick_index == context.stage.global_pick_index


def test_freely_available_basic_land_ignores_contextual_adjustments() -> None:
    generic_database = _card_database()
    basic_land = replace(
        generic_database.lookup(grp_id=13),
        set_code="TST",
        arena_id=13,
        produced_mana=("W", "U"),
    )
    database = CardDatabase(cards={**generic_database.cards, 13: basic_land})
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:13",
                assignments=(
                    RoleAssignment(
                        Role.FIXING,
                        parameters=ProducedResources(("W", "U")),
                    ),
                ),
            ),
        ),
        role_targets=(RoleTarget(Role.FIXING, 1),),
    )
    card = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(13,),
        pool_grp_ids=(),
    ).cards[0]

    assert card.freely_available_basic is True
    assert card.contextual_breakdown == ContextualScoreBreakdown()
    assert card.contextual_evidence == ()
    assert card.raw_score == 0
    assert card.score == 0


def test_contextual_terms_have_finite_individual_and_collective_bounds() -> None:
    positive = ContextualScoreBreakdown(
        role=MAX_ROLE_TERM,
        urgency=MAX_URGENCY_TERM,
        synergy=MAX_SYNERGY_TERM,
        fixing=MAX_FIXING_TERM,
    )
    negative = ContextualScoreBreakdown(
        redundancy=-MAX_REDUNDANCY_TERM,
        unsupported_payoff=-MAX_UNSUPPORTED_PAYOFF_TERM,
    )

    assert positive.aggregate == MAX_CONTEXTUAL_ADJUSTMENT
    assert negative.aggregate == -4.0
    serialized = positive.to_json()
    assert all(
        -MAX_CONTEXTUAL_ADJUSTMENT
        <= value
        <= MAX_CONTEXTUAL_ADJUSTMENT
        for value in (*serialized.values(), negative.aggregate)
    )
    assert serialized == {
        "role": MAX_ROLE_TERM,
        "urgency": MAX_URGENCY_TERM,
        "synergy": MAX_SYNERGY_TERM,
        "redundancy": 0.0,
        "unsupported_payoff": 0.0,
        "fixing": MAX_FIXING_TERM,
        "aggregate": MAX_CONTEXTUAL_ADJUSTMENT,
    }
    with pytest.raises(ValueError, match="finite"):
        ContextualScoreBreakdown(role=float("nan"))
    with pytest.raises(ValueError, match="between"):
        ContextualScoreBreakdown(urgency=MAX_URGENCY_TERM + 0.01)


def test_early_quality_dominates_a_small_contextual_role_bonus() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:7",
                assignments=(RoleAssignment(Role.DRAW),),
            ),
        ),
        role_targets=(RoleTarget(Role.DRAW, 1),),
    )
    scored_pack = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(7, 8),
        pool_grp_ids=(1,),
        global_pick_index=5,
        pack_number=0,
        pick_number=4,
        estimated_remaining_picks=37,
        ratings_data=_contextual_ratings(),
    )

    by_id = {card.card.grp_id: card for card in scored_pack.cards}
    assert scored_pack.cards[0].card.grp_id == 8
    assert 0 < by_id[7].contextual_breakdown.role < MAX_ROLE_TERM
    assert by_id[8].contextual_breakdown.aggregate == 0


def test_supported_semantic_package_adds_value_without_forcing_the_card() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:1",
                assignments=(RoleAssignment(Role.GO_WIDE_ENABLER),),
            ),
            ProfileCard(
                key="arena_id:2",
                assignments=(RoleAssignment(Role.GO_WIDE_PAYOFF),),
            ),
        )
    )
    scored_pack = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(2, 3),
        pool_grp_ids=(1,),
        ratings_data=_contextual_ratings(),
    )

    by_id = {card.card.grp_id: card for card in scored_pack.cards}
    assert by_id[2].contextual_breakdown.synergy > 0
    assert by_id[2].contextual_breakdown.unsupported_payoff == 0
    assert scored_pack.cards[0].card.grp_id == 3


def test_empirical_card_pair_synergy_does_not_change_contextual_score() -> None:
    database = _contextual_database()
    cards = (
        ProfileCard(
            key="arena_id:1",
            assignments=(RoleAssignment(Role.GO_WIDE_ENABLER),),
        ),
        ProfileCard(
            key="arena_id:2",
            assignments=(RoleAssignment(Role.GO_WIDE_PAYOFF),),
        ),
    )
    plain_profile = _contextual_profile(cards=cards)
    empirical_profile = _contextual_profile(
        cards=cards,
        synergy=(
            CardPairSynergy(
                first_card="arena_id:1",
                second_card="arena_id:2",
                value=99.0,
                samples=10000,
            ),
        ),
    )

    plain = _score_with_context(
        database=database,
        profile=plain_profile,
        offered_grp_ids=(2,),
        pool_grp_ids=(1,),
    ).cards[0]
    empirical = _score_with_context(
        database=database,
        profile=empirical_profile,
        offered_grp_ids=(2,),
        pool_grp_ids=(1,),
    ).cards[0]

    assert empirical.contextual_breakdown == plain.contextual_breakdown
    assert empirical.score == plain.score


def test_unsupported_payoff_is_penalized_without_an_enabler() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:1",
                assignments=(RoleAssignment(Role.GO_WIDE_PAYOFF),),
            ),
            ProfileCard(
                key="arena_id:2",
                assignments=(RoleAssignment(Role.GO_WIDE_PAYOFF),),
            ),
        )
    )
    card = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(2,),
        pool_grp_ids=(1,),
    ).cards[0]

    assert card.contextual_breakdown.unsupported_payoff < 0
    assert any("unsupported go_wide payoff" in item for item in card.contextual_evidence)

def test_dual_role_candidate_supplies_its_own_payoff_enabler() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:1",
                assignments=(RoleAssignment(Role.GO_WIDE_PAYOFF),),
            ),
            ProfileCard(
                key="arena_id:2",
                assignments=(
                    RoleAssignment(Role.GO_WIDE_ENABLER),
                    RoleAssignment(Role.GO_WIDE_PAYOFF),
                ),
            ),
        )
    )

    card = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(2,),
        pool_grp_ids=(1,),
    ).cards[0]

    assert card.contextual_breakdown.unsupported_payoff == 0
    assert not any(
        "unsupported go_wide payoff" in item for item in card.contextual_evidence
    )


def test_fixing_and_redundancy_terms_use_projected_pool_evidence() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:4",
                assignments=(RoleAssignment(Role.DRAW),),
            ),
            ProfileCard(
                key="arena_id:5",
                assignments=(RoleAssignment(Role.DRAW),),
            ),
            ProfileCard(
                key="arena_id:6",
                assignments=(
                    RoleAssignment(
                        Role.FIXING,
                        parameters=ProducedResources(("W", "U")),
                    ),
                ),
            ),
        ),
        role_targets=(
            RoleTarget(Role.DRAW, 1),
            RoleTarget(Role.FIXING, 2),
        ),
    )
    scored_pack = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(5, 6),
        pool_grp_ids=(4,),
    )

    by_id = {card.card.grp_id: card for card in scored_pack.cards}
    assert by_id[5].contextual_breakdown.redundancy < 0
    assert by_id[6].contextual_breakdown.fixing > 0
    assert any("redundancy pressure" in item for item in by_id[5].contextual_evidence)
    assert any("fixing need" in item for item in by_id[6].contextual_evidence)

def test_fixing_does_not_fallback_when_target_is_met_or_zero() -> None:
    database = _contextual_database()
    cards = (
        ProfileCard(
            key="arena_id:6",
            assignments=(
                RoleAssignment(
                    Role.FIXING,
                    parameters=ProducedResources(("W", "U")),
                ),
            ),
        ),
    )
    met_profile = _contextual_profile(
        cards=cards,
        role_targets=(RoleTarget(Role.FIXING, 1),),
    )
    zero_profile = _contextual_profile(
        cards=cards,
        role_targets=(RoleTarget(Role.FIXING, 0),),
    )

    met_card = _score_with_context(
        database=database,
        profile=met_profile,
        offered_grp_ids=(6,),
        pool_grp_ids=(6,),
    ).cards[0]
    zero_card = _score_with_context(
        database=database,
        profile=zero_profile,
        offered_grp_ids=(6,),
        pool_grp_ids=(),
    ).cards[0]

    assert met_card.contextual_breakdown.fixing == 0
    assert zero_card.contextual_breakdown.fixing == 0


def test_role_evidence_tie_uses_target_name_without_comparing_assignments() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:6",
                assignments=(
                    RoleAssignment(
                        Role.FIXING,
                        parameters=ProducedResources(("W",)),
                    ),
                    RoleAssignment(
                        Role.FIXING,
                        parameters=ProducedResources(("U",)),
                    ),
                ),
            ),
        ),
        role_targets=(RoleTarget(Role.FIXING, 1),),
    )

    card = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(6,),
        pool_grp_ids=(),
    ).cards[0]

    assert card.contextual_breakdown.role > 0
    assert any("fills fixing deficit" in item for item in card.contextual_evidence)


def test_generic_target_confidence_scales_redundancy_penalty() -> None:
    database = _contextual_database()
    cards = (
        ProfileCard(
            key="arena_id:5",
            assignments=(RoleAssignment(Role.DRAW),),
        ),
    )
    generic_card = _score_with_context(
        database=database,
        profile=_contextual_profile(cards=cards),
        offered_grp_ids=(5,),
        pool_grp_ids=(5,),
    ).cards[0]
    explicit_card = _score_with_context(
        database=database,
        profile=_contextual_profile(
            cards=cards,
            role_targets=(RoleTarget(Role.DRAW, 1),),
        ),
        offered_grp_ids=(5,),
        pool_grp_ids=(5,),
    ).cards[0]

    assert generic_card.contextual_breakdown.redundancy == pytest.approx(
        explicit_card.contextual_breakdown.redundancy * 0.25, abs=5e-7
    )


def test_low_confidence_target_scales_targeted_fixing() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:6",
                assignments=(
                    RoleAssignment(
                        Role.FIXING,
                        parameters=ProducedResources(("W", "U")),
                    ),
                ),
            ),
        ),
        role_targets=(RoleTarget(Role.FIXING, 2),),
    )
    high_confidence = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(6,),
        pool_grp_ids=(),
    ).cards[0]

    ledger = _context_ledger(profile=profile, database=database)
    low_confidence_coverage = tuple(
        replace(item, confidence=0.25)
        if item.name == Role.FIXING.value
        else item
        for item in ledger.target_coverage
    )
    low_confidence_ledger = replace(
        ledger,
        target_coverage=low_confidence_coverage,
    )
    low_confidence = PickEngine(
        scoring_context=PickScoringContext(
            set_profile=profile,
            role_ledger=low_confidence_ledger,
        )
    ).score_pack(
        offered_grp_ids=(6,),
        card_database=database,
        pool_grp_ids=(),
    ).cards[0]

    assert low_confidence.contextual_breakdown.fixing == pytest.approx(
        high_confidence.contextual_breakdown.fixing * 0.25
    )


def test_explanation_exposes_context_metadata_and_material_late_terms() -> None:
    database = _contextual_database()
    profile = _contextual_profile(
        cards=(
            ProfileCard(
                key="arena_id:7",
                assignments=(RoleAssignment(Role.DRAW),),
            ),
        ),
        role_targets=(RoleTarget(Role.DRAW, 1),),
        theme="patient card advantage",
    )
    card = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(7,),
        pool_grp_ids=(1,),
    ).cards[0]
    explanation = render_pick_rationale_detailed(
        scored_card=card,
    )

    assert "context WU, theme patient card advantage" in explanation
    assert "mature profile, 100% confidence" in explanation
    assert "material terms:" not in explanation
    assert "role +" in explanation
    assert "urgency +" in explanation
    assert "late missing-role urgency" in explanation


def test_generated_early_semantic_profile_contributes_without_live_ratings() -> None:
    database = _contextual_database()
    database = replace(
        database,
        cards={
            **database.cards,
            7: replace(
                database.cards[7],
                types=("Instant",),
                type_line="Instant",
                oracle_text="Draw two cards.",
            ),
        },
    )
    ratings = replace(
        _contextual_ratings().primary,
        set_code="TST",
        event_format="QuickDraft",
    )
    generated = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="early",
        card_database=database,
        source_manifest=None,
        generated_at=datetime(1970, 1, 1, tzinfo=UTC),
        ratings=ratings,
    )
    profile = SetProfile.from_json(json.loads(generated.profile.to_bytes()))

    enabled = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(7,),
        pool_grp_ids=(1,),
        ratings_data=None,
    ).cards[0]
    disabled = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(7,),
        pool_grp_ids=(1,),
        ratings_data=None,
        contextual_adjustments_enabled=False,
    ).cards[0]

    assert enabled.contextual_breakdown.role > 0
    assert enabled.contextual_evidence
    assert enabled.contextual_profile_maturity == "early"
    assert disabled.contextual_breakdown.role == 0
    assert disabled.contextual_evidence == ()
    assert disabled.rating.gih_win_rate == enabled.rating.gih_win_rate


def test_contextual_adjustments_can_be_disabled_without_bypassing_profile_scoring() -> None:
    database = _contextual_database()
    profile = replace(
        _contextual_profile(
            cards=(
                ProfileCard(
                    key="arena_id:7",
                    assignments=(RoleAssignment(Role.DRAW),),
                ),
            ),
            role_targets=(RoleTarget(Role.DRAW, 1),),
            theme="patient card advantage",
        ),
        card_ratings=(
            _profile_card("ARENA_ID:7", 0.72),
            _profile_card("ARENA_ID:8", 0.68),
        ),
    )
    ratings_data = _contextual_ratings()
    enabled = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(7, 8),
        pool_grp_ids=(1,),
        ratings_data=ratings_data,
    )
    disabled = _score_with_context(
        database=database,
        profile=profile,
        offered_grp_ids=(7, 8),
        pool_grp_ids=(1,),
        ratings_data=ratings_data,
        contextual_adjustments_enabled=False,
    )

    assert enabled.cards[0].contextual_breakdown.role > 0
    assert all(
        card.contextual_breakdown == ContextualScoreBreakdown()
        and card.contextual_evidence == ()
        for card in disabled.cards
    )
    assert tuple(
        (card.card.grp_id, card.raw_score, card.score, card.source_label)
        for card in disabled.cards
    ) == (
        (7, 100.0, 100, "Profile"),
        (8, 88.6904761904762, 89, "Profile"),
    )
    disabled_card = next(card for card in disabled.cards if card.card.grp_id == 7)
    assert disabled_card.rating.metadata.source == "profile"
    assert disabled_card.contextual_pair == "WU"
    assert disabled_card.contextual_theme == "patient card advantage"
    assert not any(
        reason.kind
        in {
            "role",
            "urgency",
            "synergy",
            "redundancy",
            "unsupported_payoff",
            "fixing",
        }
        for reason in disabled_card.rationale.reasons
    )
    assert disabled_card.contextual_profile_maturity == "mature"
    assert disabled_card.contextual_profile_confidence == pytest.approx(1.0)
    explanation = render_pick_rationale_detailed(
        scored_card=disabled_card,
    )
    assert "material terms:" not in explanation
    assert "fills " not in explanation
    assert "context " not in explanation
    assert "mature profile, 100% confidence" in explanation

    wrapped = score_pack(
        offered_grp_ids=(7, 8),
        card_database=database,
        ratings_data=ratings_data,
        pool_grp_ids=(1,),
        scoring_context=enabled.scoring_context,
        contextual_adjustments_enabled=False,
    )
    assert tuple(card.score for card in wrapped.cards) == tuple(
        card.score for card in disabled.cards
    )
    assert all(
        card.contextual_breakdown == ContextualScoreBreakdown()
        and card.contextual_evidence == ()
        for card in wrapped.cards
    )


def test_disabled_contextual_adjustments_preserve_open_pair_tiebreaking() -> None:
    profile = _pair_shrinkage_profile(
        total_samples=10_000,
        pair_samples=10_000,
    )
    score_kwargs = {
        "offered_grp_ids": (32, 31),
        "card_database": _msh_pair_tiebreaker_database(),
        "pool_grp_ids": (20, 21),
        "pick_index": 3,
    }
    ratings_data = _msh_pair_tiebreaker_data()
    disabled = PickEngine(
        contextual_adjustments_enabled=False,
        ratings_data=ratings_data,
        set_profile=profile,
    ).score_pack(**score_kwargs)
    expected_pair_metadata = (
        ("WU", 0.569274),
        ("BR", 0.5),
    )

    assert disabled.commitment.phase == "open"
    assert tuple(
        (card.card.grp_id, card.raw_score, card.score)
        for card in disabled.cards
    ) == (
        (31, 50.0, 50),
        (32, 52.0, 52),
    )
    assert tuple(
        (card.pair_tiebreaker_pair, card.pair_tiebreaker_win_rate)
        for card in disabled.cards
    ) == expected_pair_metadata
    assert disabled.cards[0].card.grp_id == 31
    assert disabled.cards[0].raw_score < disabled.cards[1].raw_score
    assert all(
        card.contextual_breakdown == ContextualScoreBreakdown()
        and card.contextual_evidence == ()
        for card in disabled.cards
    )


def test_disabled_contextual_adjustments_preserve_locked_splash_behavior() -> None:
    database = _splash_card_database()
    profile = _test_profile(
        pairs=(
            PairProfile(
                pair="WU",
                role_targets=(RoleTarget(Role.DRAW, 1),),
            ),
        ),
        role_profile=CompiledRoleProfile(
            set_code="TST",
            cards=(
                ProfileCard(
                    key="arena_id:105",
                    assignments=(RoleAssignment(Role.DRAW),),
                ),
            ),
        ),
    )
    database = CardDatabase(
        cards={
            grp_id: replace(card, set_code="TST", arena_id=grp_id)
            for grp_id, card in database.cards.items()
        }
    )
    score_kwargs = {
        "offered_grp_ids": (106, 107, 109),
        "card_database": database,
        "pool_grp_ids": (101, 102, 101, 102, 103, 104, 110, 105),
        "pick_index": 16,
    }
    ratings_data = _splash_ratings_data()
    disabled = PickEngine(
        contextual_adjustments_enabled=False,
        ratings_data=ratings_data,
        set_profile=profile,
    ).score_pack(**score_kwargs)
    expected_raw_scores = (
        89.99201277955271,
        75.0,
        75.0,
    )

    assert disabled.commitment.phase == "locked"
    assert disabled.commitment.inferred_pair == "WU"
    assert disabled.splash_state.active_color == "R"
    assert tuple(card.card.grp_id for card in disabled.cards) == (106, 107, 109)
    assert tuple(card.raw_score for card in disabled.cards) == pytest.approx(
        expected_raw_scores
    )
    assert tuple(card.score for card in disabled.cards) == (90, 75, 75)
    assert tuple(card.color_fit for card in disabled.cards) == (
        "splash-ready",
        "off-color",
        "off-color",
    )
    assert all(
        card.contextual_breakdown == ContextualScoreBreakdown()
        and card.contextual_evidence == ()
        for card in disabled.cards
    )



def _render_comparison(
    cards: tuple,
    *,
    phase: str,
    require_material_rate_margin: bool = False,
) -> str | None:
    return _recommendation_comparison_summary(
        cards=cards,
        phase=phase,
        config=PickEngine().config,
        require_material_rate_margin=require_material_rate_margin,
    )


def _comparison_cards(
    *,
    names: tuple[str, ...] = ("Alpha", "Beta"),
    base_scores: tuple[float, ...] = (60.0, 60.0),
    raw_scores: tuple[float, ...] | None = None,
    scores: tuple[int, ...] | None = None,
    base_ratings: tuple[float, ...] | None = None,
    contributions: tuple[dict[str, float], ...] | None = None,
    remainders: tuple[float, ...] | None = None,
    pair_rates: tuple[float | None, ...] | None = None,
    pair_names: tuple[str | None, ...] | None = None,
    pair_weights: tuple[float | None, ...] | None = None,
    tiebreaker_indices: tuple[int, ...] = (),
    tiebreaker_evidence: str | None = None,
) -> tuple:
    pack = PickEngine().score_pack(
        offered_grp_ids=tuple(range(1, len(names) + 1)),
        card_database=_card_database(),
    )
    if contributions is None:
        contributions = tuple({} for _ in names)
    if remainders is None:
        if raw_scores is None:
            remainders = tuple(0.0 for _ in names)
        else:
            remainders = tuple(
                raw
                - base_scores[index]
                - sum(contributions[index].values())
                for index, raw in enumerate(raw_scores)
            )
    if raw_scores is None:
        raw_scores = tuple(
            base_scores[index]
            + sum(contributions[index].values())
            + remainders[index]
            for index in range(len(names))
        )
    if scores is None:
        scores = tuple(int(raw + 0.5) for raw in raw_scores)
    if base_ratings is None:
        base_ratings = tuple(base / 100.0 for base in base_scores)
    if pair_rates is None:
        pair_rates = tuple(None for _ in names)
    if pair_names is None:
        pair_names = tuple(None for _ in names)
    if pair_weights is None:
        pair_weights = tuple(
            1.0 if pair_rate is not None else None
            for pair_rate in pair_rates
        )

    comparison_cards = []
    for index, card in enumerate(pack.cards):
        reasons = [
            PickReason(
                kind="rating",
                phrase="synthetic baseline rating",
            )
        ]
        reasons.extend(
            PickReason(
                kind=kind,
                contribution=contribution,
                phrase=f"synthetic {kind} contribution",
            )
            for kind, contribution in contributions[index].items()
        )
        if index in tiebreaker_indices:
            reasons.append(
                PickReason(
                    kind="tiebreaker",
                    phrase="synthetic pair-rate comparison",
                    evidence=(
                        tiebreaker_evidence
                        or "pair-rate comparison selected the first card"
                    ),
                )
            )
        comparison_cards.append(
            replace(
                card,
                card=replace(card.card, name=names[index]),
                original_index=index,
                base_rating=base_ratings[index],
                base_score=base_scores[index],
                raw_score=raw_scores[index],
                score=scores[index],
                pair_tiebreaker_pair=pair_names[index],
                pair_tiebreaker_win_rate=pair_rates[index],
                pair_tiebreaker_weight=pair_weights[index],
                score_sort_index=index,
                freely_available_basic=False,
                rationale=PickRationale(
                    reasons=tuple(reasons),
                    unattributed_contribution=remainders[index],
                ),
            )
        )
    return tuple(comparison_cards)


def _score_with_context(
    *,
    database: CardDatabase,
    profile: SetProfile,
    offered_grp_ids: tuple[int, ...],
    pool_grp_ids: tuple[int, ...],
    ratings_data: SeventeenLandsData | None = None,
    contextual_adjustments_enabled: bool = True,
    pack_number: int = 2,
    pick_number: int = 6,
    global_pick_index: int = 35,
    estimated_remaining_picks: int = 7,
) -> ScoredPack:
    ledger = project_pool_role_ledger(
        pool_before_pick=pool_grp_ids,
        pack_number=pack_number,
        pick_number=pick_number,
        global_pick_index=global_pick_index,
        estimated_remaining_picks=estimated_remaining_picks,
        card_database=database,
        ratings_data=ratings_data,
        set_profile=profile,
        likely_pair="WU",
    )
    context = PickScoringContext(set_profile=profile, role_ledger=ledger)
    return PickEngine(
        ratings_data=ratings_data,
        scoring_context=context,
        contextual_adjustments_enabled=contextual_adjustments_enabled,
    ).score_pack(
        offered_grp_ids=offered_grp_ids,
        card_database=database,
        pool_grp_ids=pool_grp_ids,
    )


def _contextual_profile(
    *,
    cards: tuple[ProfileCard, ...],
    role_targets: tuple[RoleTarget, ...] = (),
    theme: str | None = None,
    confidence: float = 1.0,
    synergy: tuple[CardPairSynergy, ...] = (),
) -> SetProfile:
    pair = PairProfile(
        pair="WU",
        role_targets=role_targets,
        synergy=synergy,
        theme=theme,
    )
    return SetProfile(
        set_code="TST",
        event_format="quickdraft",
        profile_version="contextual-test",
        generated_at="1970-01-01T00:00:00+00:00",
        source=SourceMetadata(provider="test"),
        maturity=ProfileMaturity.MATURE,
        samples=SampleSummary(total=1, by_pair=(("WU", 1),)),
        confidence=confidence,
        pairs=(pair,),
        role_profile=CompiledRoleProfile(set_code="TST", cards=cards),
    )


def _contextual_database() -> CardDatabase:
    return CardDatabase(
        cards={
            grp_id: _card(
                grp_id=grp_id,
                name=f"Context Card {grp_id}",
                colors=("W",),
                set_code="TST",
                arena_id=grp_id,
            )
            for grp_id in (1, 2, 3, 4, 7, 8)
        }
        | {
            5: _card(
                grp_id=5,
                name="Context Draw Redundant",
                colors=("W",),
                set_code="TST",
                arena_id=5,
            ),
            6: _card(
                grp_id=6,
                name="Context Fixing",
                colors=(),
                types=("Land",),
                mana_cost=None,
                mana_value=None,
                produced_mana=("W", "U"),
                set_code="TST",
                arena_id=6,
            ),
        },
    )


def _contextual_ratings() -> SeventeenLandsData:
    data = _ratings_data()
    primary = replace(
        data.primary,
        card_ratings={
            grp_id: _stats(
                grp_id=grp_id,
                name=f"Context Card {grp_id}",
                color="W",
                gih=0.9 if grp_id == 3 or grp_id == 8 else 0.5,
                games_in_hand=900,
            )
            for grp_id in (1, 2, 3, 4, 5, 6, 7, 8)
        },
    )
    return replace(data, primary=primary)


def _context_profile() -> SetProfile:
    return SetProfile(
        set_code="TST",
        event_format="quickdraft",
        profile_version="context-test",
        generated_at="1970-01-01T00:00:00+00:00",
        source=SourceMetadata(provider="test"),
        maturity=ProfileMaturity.MATURE,
        samples=SampleSummary(total=1, by_pair=(("WU", 1),)),
        confidence=1.0,
        pairs=(PairProfile(pair="WU"),),
    )


def _pair_shrinkage_profile(
    *,
    total_samples: int,
    pair_samples: int,
    confidence: float = 1.0,
    maturity: ProfileMaturity = ProfileMaturity.MATURE,
) -> SetProfile:
    return replace(
        _context_profile(),
        maturity=maturity,
        samples=SampleSummary(
            total=total_samples,
            by_pair=(("WU", pair_samples),),
        ),
        confidence=confidence,
    )


def _context_ledger(
    *,
    profile: SetProfile,
    database: CardDatabase,
) -> PoolRoleLedger:
    return project_pool_role_ledger(
        pool_before_pick=(),
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
        card_database=database,
        set_profile=profile,
        likely_pair="WU",
    )
