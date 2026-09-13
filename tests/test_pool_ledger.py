from __future__ import annotations

from dataclasses import dataclass, replace
import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import DECK_BUILDER
from draftomen.pickengine import PickEngine
from draftomen.pool_ledger import (
    COMPLETED_POOL,
    PRE_PICK_PROJECTION,
    LedgerMode,
    PoolRoleLedger,
    RelationshipSupport,
    _likely_projection,
    evaluate_completed_pool_role_ledger,
    project_pool_role_ledger,
)
from draftomen.semantic_capability_records import (
    CapabilityQuantity,
    CapabilityZone,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import SEMANTIC_ENRICHMENT_SCHEMA_VERSION, card_source_sha256
from draftomen.semantic_enrichment_records import (
    ArtifactReview,
    CardSourcePin,
    FindingReview,
    FindingStatus,
    GuideEvidence,
    GuideSourcePin,
    ModelRun,
    OracleEvidence,
    ReasoningConfig,
)
from draftomen.semantic_relationship_records import (
    CardRelationship,
    RelationshipParticipant,
    RelationshipPrerequisite,
    RelationshipPrerequisiteProjection,
    RelationshipTiming,
    RelationshipZone,
)
from draftomen.semantic_roles import (
    CompiledRoleProfile,
    ProfileCard,
    RemovalCharacteristics,
    Role,
    RoleAssignment,
    resolve_card_roles,
)
from draftomen.set_profile import (
    EnhancementCardData,
    PairProfile,
    ProfileMaturity,
    RemovalTarget,
    RoleTarget,
    SampleSummary,
    SetProfile,
    SetProfileEnhancement,
    SourceMetadata,
)


def test_canonical_stage_coordinates_and_remaining_boundary() -> None:
    database = _database(_card(1, colors=("W",)))
    final = project_pool_role_ledger(
        pool_before_pick=(1,),
        pack_number=2,
        pick_number=13,
        global_pick_index=42,
        estimated_remaining_picks=0,
        card_database=database,
    )
    assert final.stage is not None
    assert final.stage.global_pick_index == 42
    assert final.remaining_picks == 0

    with pytest.raises(ValueError, match="global_pick_index"):
        project_pool_role_ledger(
            pool_before_pick=(1,),
            pack_number=2,
            pick_number=13,
            global_pick_index=41,
            estimated_remaining_picks=1,
            card_database=database,
        )


def test_projection_selects_a_bounded_deck_shape() -> None:
    cards = tuple(_card(index, colors=("W",)) for index in range(1, 25))
    ledger = project_pool_role_ledger(
        pool_before_pick=tuple(range(1, 25)),
        pack_number=0,
        pick_number=4,
        global_pick_index=5,
        estimated_remaining_picks=37,
        card_database=_database(*cards),
    )

    assert ledger.pool_size == 24
    assert ledger.playable_count == 23
    assert ledger.cut_count == 1

def test_projection_enforces_creature_and_expensive_caps_during_quotas() -> None:
    cards = (
        *(
            _card(index, colors=("W",), mana_value=2)
            for index in range(1, 6)
        ),
        *(
            _card(index, colors=("W",), mana_value=6)
            for index in range(6, 18)
        ),
        *(
            _card(index, colors=("W",), mana_value=3)
            for index in range(18, 27)
        ),
        *(
            _card(index, colors=(), mana_value=3, types=("Artifact",))
            for index in range(27, 33)
        ),
    )
    database = _database(*cards)
    projected = _likely_projection(
        pool_grp_ids=tuple(range(1, 33)),
        card_database=database,
        set_profile=None,
        pair="WU",
        ratings_data=_ProjectionRatings(high_rating_grp_ids=frozenset(range(6, 18))),
    )
    projected_cards = tuple(database.lookup(grp_id=grp_id) for grp_id in projected)

    assert len(projected) == DECK_BUILDER.target_spell_count
    assert sum(any("Creature" in type_line for type_line in card.types) for card in projected_cards) >= DECK_BUILDER.creature_floor
    assert sum(any("Creature" in type_line for type_line in card.types) for card in projected_cards) <= DECK_BUILDER.creature_ceiling
    assert sum(
        card.mana_value is not None
        and card.mana_value >= DECK_BUILDER.expensive_spell_mana_value
        for card in projected_cards
    ) <= DECK_BUILDER.maximum_expensive_spells


def test_projection_excludes_nonbasic_lands_with_mixed_sources_outside_pair() -> None:
    spells = tuple(_card(index, colors=("W",)) for index in range(1, 24))
    rg_only_land = _land_card(
        24,
        colors=("R", "G"),
        produced_mana=("R", "G"),
    )
    wr_split_dual = _land_card(
        25,
        colors=("W", "R"),
        produced_mana=("W", "R"),
    )
    wu_dual = _land_card(
        26,
        colors=("W", "U"),
        produced_mana=("W", "U"),
    )
    database = _database(*spells, rg_only_land, wr_split_dual, wu_dual)

    projected = _likely_projection(
        pool_grp_ids=tuple(range(1, 27)),
        card_database=database,
        set_profile=None,
        pair="WU",
        ratings_data=None,
    )

    assert len(projected) == DECK_BUILDER.target_spell_count + 1
    assert 24 not in projected
    assert 25 not in projected
    assert 26 in projected



def test_pre_pick_projection_is_deterministic_and_completed_mode_has_no_stage() -> None:
    database = _database(_card(1, colors=("W",)))
    projected = project_pool_role_ledger(
        pool_before_pick=(1, 1),
        pack_number=0,
        pick_number=4,
        global_pick_index=5,
        estimated_remaining_picks=40,
        card_database=database,
    )
    repeated = project_pool_role_ledger(
        pool_before_pick=(1, 1),
        pack_number=0,
        pick_number=4,
        global_pick_index=5,
        estimated_remaining_picks=40,
        card_database=database,
    )
    completed = evaluate_completed_pool_role_ledger(
        final_pool=(1, 1),
        card_database=database,
    )

    assert projected.mode is PRE_PICK_PROJECTION
    assert projected.stage is not None
    assert projected.pool_size == 2
    assert projected.unique_card_count == 1
    assert projected.profile_fingerprint is None
    assert projected.to_json()["profile_fingerprint"] is None
    assert projected.to_json() == repeated.to_json()
    assert completed.mode is COMPLETED_POOL
    assert completed.stage is None
    assert completed.remaining_picks is None
    assert completed.profile_fingerprint is None


def test_missing_role_urgency_is_monotone_and_preferred_target_removes_late_bonus() -> None:
    database = _database(_card(1, colors=("W",)))
    profile = _profile(
        cards=(),
        pair=PairProfile(pair="WU", role_targets=(RoleTarget(Role.DRAW, 1),)),
    )
    early = project_pool_role_ledger(
        pool_before_pick=(1,),
        pack_number=0,
        pick_number=4,
        global_pick_index=5,
        estimated_remaining_picks=37,
        card_database=database,
        set_profile=profile,
        likely_pair="WU",
    )
    late = project_pool_role_ledger(
        pool_before_pick=(1,),
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
        card_database=database,
        set_profile=profile,
        likely_pair="WU",
    )
    met = project_pool_role_ledger(
        pool_before_pick=(1, 2),
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
        card_database=_database(_card(1, colors=("W",)), _card(2, colors=("U",))),
        set_profile=_profile(
            cards=(
                ProfileCard(
                    key="arena_id:2",
                    assignments=(RoleAssignment(Role.DRAW),),
                ),
            ),
            pair=PairProfile(pair="WU", role_targets=(RoleTarget(Role.DRAW, 1),)),
        ),
        likely_pair="WU",
    )

    assert early.urgency < late.urgency
    assert late.urgency > 0
    assert met.urgency == 0


def test_removal_subtypes_stay_distinct_and_saturate() -> None:
    assignments = tuple(
        ProfileCard(
            key=f"arena_id:{index}",
            assignments=(
                RoleAssignment(
                    role,
                    parameters=RemovalCharacteristics(
                        kind=kind,
                        effective_score=1.0,
                        temporary=temporary,
                    ),
                ),
            ),
        )
        for index, role, kind, temporary in (
            (1, Role.HARD_REMOVAL, "destroy", False),
            (2, Role.DAMAGE_REMOVAL, "damage", False),
            (3, Role.DISABLING_REMOVAL, "disable", False),
            (4, Role.CONDITIONAL_REMOVAL, "destroy", False),
            (5, Role.BOUNCE, "bounce", True),
            (6, Role.TEMPORARY_TAP, "tap", True),
            (7, Role.HARD_REMOVAL, "exile", False),
        )
    )
    ledger = evaluate_completed_pool_role_ledger(
        final_pool=tuple(range(1, 8)),
        card_database=_database(*(_card(index, colors=("W",)) for index in range(1, 8))),
        set_profile=_profile(cards=assignments),
    )

    contributions = dict(ledger.removal_by_kind)
    assert contributions["destroy"] > contributions["damage"]
    assert contributions["damage"] > contributions["disable"]
    assert contributions["disable"] > contributions["temporary"]
    assert contributions["conditional"] > 0
    assert contributions["bounce"] > 0
    assert contributions["temporary"] > 0
    assert ledger.effective_removal == pytest.approx(sum(contributions.values()))
    assert ledger.effective_removal > 1.0
    assert ledger.removal_saturation == 1.0
    assert all(0.0 <= value <= 1.0 for _, value in ledger.removal_saturation_by_kind)


def test_tap_target_uses_temporary_bucket_and_profile_confidence() -> None:
    profile = _profile(
        cards=(
            ProfileCard(
                key="arena_id:1",
                assignments=(
                    RoleAssignment(
                        Role.TEMPORARY_TAP,
                        parameters=RemovalCharacteristics(
                            kind="tap",
                            effective_score=1.0,
                        ),
                    ),
                ),
            ),
        ),
        pair=PairProfile(
            pair="WU",
            removal_targets=(RemovalTarget(kind="tap", value=0.4),),
        ),
    )
    ledger = evaluate_completed_pool_role_ledger(
        final_pool=(1,),
        card_database=_database(_card(1, colors=("W",))),
        set_profile=profile,
        likely_pair="WU",
    )

    coverage = ledger.target_coverage_map["removal:tap"]
    assert coverage.count == ledger.removal_by_kind[-1][1]
    assert coverage.count == pytest.approx(0.4)
    assert coverage.coverage == 1.0
    assert coverage.deficit == 0.0
    assert coverage.confidence == profile.confidence


def test_cut_count_excludes_projected_nonbasic_lands() -> None:
    cards = tuple(_card(index, colors=("W",)) for index in range(1, 24))
    land = _land_card(24, colors=("W",))
    ledger = project_pool_role_ledger(
        pool_before_pick=tuple(range(1, 25)),
        pack_number=0,
        pick_number=4,
        global_pick_index=5,
        estimated_remaining_picks=37,
        card_database=_database(*cards, land),
    )

    assert ledger.pool_size == 24
    assert ledger.playable_count == 23
    assert ledger.cut_count == 0


def test_unsupported_payoff_is_not_counted_as_supported_package() -> None:
    profile = _profile(
        cards=(
            ProfileCard(
                key="arena_id:1",
                assignments=(RoleAssignment(Role.GO_WIDE_PAYOFF),),
            ),
        )
    )
    ledger = evaluate_completed_pool_role_ledger(
        final_pool=(1,),
        card_database=_database(_card(1, colors=("W",))),
        set_profile=profile,
    )

    assert dict(ledger.payoff_counts)["go_wide"] == 1
    assert dict(ledger.supported_payoff_counts)["go_wide"] == 0
    assert dict(ledger.unsupported_payoff_counts)["go_wide"] == 1
    assert dict(ledger.package_density)["go_wide"] == 0


    serialized = ledger.to_json()
    assert serialized["profile_fingerprint"] == profile.fingerprint
    assert serialized["fixing_count"] == ledger.fixing_count
    assert serialized["card_advantage_count"] == ledger.card_advantage_count
    for field_name in (
        "enabler_counts",
        "payoff_counts",
        "supported_payoff_counts",
        "unsupported_payoff_counts",
        "target_deficit",
        "target_saturation",
        "target_confidence",
        "target_diminishing_returns",
    ):
        assert serialized[field_name] == [
            list(item) for item in getattr(ledger, field_name)
        ]


def test_pick_contextual_terms_are_stage_aware_and_saturate_at_target() -> None:
    profile = _profile(
        cards=(
            ProfileCard(
                key="arena_id:2",
                assignments=(RoleAssignment(Role.DRAW),),
            ),
        ),
        pair=PairProfile(pair="WU", role_targets=(RoleTarget(Role.DRAW, 1),)),
    )
    database = _database(
        _card(1, colors=("W",)),
        _card(2, colors=("U",)),
    )
    engine = PickEngine(set_profile=profile)

    early = engine.score_pack(
        offered_grp_ids=(2,),
        card_database=database,
        pool_grp_ids=(1,),
        pack_number=0,
        pick_number=4,
        global_pick_index=5,
        estimated_remaining_picks=37,
    ).cards[0]
    late = engine.score_pack(
        offered_grp_ids=(2,),
        card_database=database,
        pool_grp_ids=(1,),
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
    ).cards[0]
    met = engine.score_pack(
        offered_grp_ids=(2,),
        card_database=database,
        pool_grp_ids=(1, 2),
        pack_number=2,
        pick_number=6,
        global_pick_index=35,
        estimated_remaining_picks=7,
    ).cards[0]

    assert (
        0
        < early.contextual_breakdown.role
        < late.contextual_breakdown.role
        <= 2.5
    )
    assert 0 < early.contextual_breakdown.urgency < late.contextual_breakdown.urgency
    assert early.contextual_evidence
    assert met.contextual_breakdown.role == 0
    assert met.contextual_breakdown.urgency == 0
    assert met.contextual_breakdown.redundancy < 0
    assert any("redundancy pressure" in item for item in met.contextual_evidence)
    rationale = met.rationale
    assert tuple(reason.kind for reason in rationale.reasons) == (
        "rating",
        "color",
        "redundancy",
    )
    assert rationale.reasons[-1].evidence == "redundancy pressure for draw"




def _profile(*, cards: tuple[ProfileCard, ...], pair: PairProfile | None = None) -> SetProfile:
    role_profile = (
        CompiledRoleProfile(set_code="TST", cards=cards)
        if cards
        else None
    )
    return SetProfile(
        set_code="TST",
        event_format="quickdraft",
        profile_version="test-1",
        generated_at="1970-01-01T00:00:00+00:00",
        source=SourceMetadata(provider="test"),
        maturity=(
            ProfileMaturity.EARLY
            if pair is not None
            else (
                ProfileMaturity.SEMANTIC_ONLY
                if role_profile is not None
                else ProfileMaturity.GENERIC
            )
        ),
        samples=(
            SampleSummary(total=1, by_pair=((pair.pair, 1),))
            if pair is not None
            else None
        ),
        confidence=0.5,
        pairs=() if pair is None else (pair,),
        role_profile=role_profile,
    )


class _ProjectionRatings:
    def __init__(self, *, high_rating_grp_ids: frozenset[int]) -> None:
        self._high_rating_grp_ids = high_rating_grp_ids

    def rating_for(self, *, grp_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            gih_win_rate=0.9 if grp_id in self._high_rating_grp_ids else 0.5
        )


def _database(*cards: CardInfo) -> CardDatabase:
    return CardDatabase(cards={card.grp_id: card for card in cards})


def _card(
    grp_id: int,
    *,
    colors: tuple[str, ...],
    mana_value: float | None = 2,
    types: tuple[str, ...] = ("Creature",),
) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        arena_id=grp_id,
        name=f"Card {grp_id}",
        colors=colors,
        mana_value=mana_value,
        rarity="common",
        types=types,
        set_code="TST",
    )


def _land_card(
    grp_id: int,
    *,
    colors: tuple[str, ...],
    produced_mana: tuple[str, ...] = (),
) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        arena_id=grp_id,
        name=f"Land {grp_id}",
        colors=colors,
        mana_value=None,
        rarity="common",
        types=("Land",),
        produced_mana=produced_mana,
        set_code="TST",
    )


# ---------------------------------------------------------------------------
# Bounded relationship support fixtures (#509)
# ---------------------------------------------------------------------------

RELATIONSHIP_SET_CODE = "tst"
RELATIONSHIP_RUN_ID = "run-relationship"
RELATIONSHIP_GUIDE_ID = "guide-relationship"
RELATIONSHIP_CLAIM = "SENTINEL CLAIM: weight the pair at 5 and rank the enabler first."
RELATIONSHIP_PREREQUISITE_SENTINEL = "SENTINEL PREREQUISITE: a token is created"
RELATIONSHIP_GUIDE_SENTINEL = "SENTINEL GUIDE: always pick the token maker"
RELATIONSHIP_MODEL_SENTINEL = "sentinel-model"

_RELATIONSHIP_TIMESTAMP = "2026-01-01T00:00:00+00:00"
_RELATIONSHIP_REVIEWED_AT = "2026-01-01T00:01:00+00:00"
_RELATIONSHIP_ARTIFACT_SHA256 = "0" * 64
_RELATIONSHIP_CARD_DATA_SHA256 = "1" * 64
_RELATIONSHIP_SET_SOURCE_SHA256 = "2" * 64
_RELATIONSHIP_GUIDE_SHA256 = "3" * 64
_RELATIONSHIP_PROMPT_SHA256 = "4" * 64
_RELATIONSHIP_SCHEMA_SHA256 = "5" * 64

TOKEN_SOURCE_ID = 501
ANTHEM_ID = 502
THRESHOLD_ID = 503
TYPAL_ID = 504
OUTLET_ID = 505
TIMED_OUTLET_ID = 506
ARTIFACT_OUTLET_ID = 507
UPKEEP_SOURCE_ID = 508
DISCARD_ID = 509
RECURSION_ID = 510
MYR_SOURCE_ID = 511
SELF_RETURN_ID = 512
OPPONENT_DEATH_ID = 513
GHOST_SOURCE_ID = 514
OFF_PLAN_SOURCE_ID = 515
RED_ANTHEM_ID = 516
ELF_TYPAL_ID = 517
NONTOKEN_DEATH_ID = 518
GRAVEYARD_PAYOFF_ID = 519
OWNED_OUTLET_ID = 544
TWIN_OUTLET_ID = 545
OPPONENT_RECURSION_ID = 546
ABUNDANT_SOURCE_ID = 547
BOUNDED_SOURCE_ID = 548
CAPPED_SOURCE_ID = 549
MILL_SOURCE_ID = 550
EXACT_OUTLET_ID = 551
UP_TO_OUTLET_ID = 552

_CUT_CASE_FILLER_IDS = tuple(range(520, 544))

TOKEN_PARAGRAPH = "Create two 1/1 white Soldier creature tokens."
ANTHEM_PARAGRAPH = "Creatures you control get +1/+1."
THRESHOLD_PARAGRAPH = "As long as you control three or more creatures, this creature gets +1/+1."
TYPAL_PARAGRAPH = "Soldier creatures you control get +1/+1."
OUTLET_PARAGRAPH = "Sacrifice a creature: Draw a card."
TIMED_OUTLET_PARAGRAPH = (
    "Sacrifice a creature: Draw a card. Activate only during your turn and only once each turn."
)
ARTIFACT_OUTLET_PARAGRAPH = "Sacrifice an artifact creature: Draw a card."
UPKEEP_PARAGRAPH = "At the beginning of your upkeep, create a 1/1 white Soldier creature token."
DISCARD_PARAGRAPH = "Discard a creature card."
RECURSION_PARAGRAPH = "Return target creature card from your graveyard to your hand."
MYR_PARAGRAPH = "Create a 1/1 colorless Myr artifact creature token."
SELF_RETURN_PARAGRAPH = "Return this creature card from your graveyard to your hand."
OPPONENT_DEATH_PARAGRAPH = "Whenever a creature an opponent controls dies, scry 1."
RED_ANTHEM_PARAGRAPH = "Red creatures you control get +1/+1."
ELF_TYPAL_PARAGRAPH = "Elf creatures you control get +1/+1."
NONTOKEN_DEATH_PARAGRAPH = "Whenever a nontoken creature you control dies, scry 1."
GRAVEYARD_PAYOFF_PARAGRAPH = (
    "As long as you have three or more creature cards in your graveyard, this creature gets +1/+1."
)
OWNED_OUTLET_PARAGRAPH = "Sacrifice a creature you own: Draw a card."
TWIN_OUTLET_PARAGRAPH = "Sacrifice a creature: Draw a card.\nSacrifice an artifact: Draw a card."
OPPONENT_RECURSION_PARAGRAPH = (
    "Return target creature card from an opponent's graveyard to your hand."
)
EXACT_OUTLET_PARAGRAPH = "Sacrifice three creatures: Draw a card."
UP_TO_OUTLET_PARAGRAPH = "Sacrifice up to two creatures: Draw a card."
ABUNDANT_PARAGRAPH = "Create three or more 1/1 white Soldier creature tokens."
BOUNDED_PARAGRAPH = "Create up to two 1/1 white Soldier creature tokens."
CAPPED_PARAGRAPH = (
    "Create two 1/1 white Soldier creature tokens. "
    "Activate only during your turn and only once each turn."
)
MILL_PARAGRAPH = "Mill three cards."


def _relationship_card(
    grp_id: int,
    name: str,
    oracle_text: str,
    *,
    colors: tuple[str, ...] = ("W",),
    mana_value: float = 2,
) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        arena_id=grp_id,
        name=name,
        colors=colors,
        mana_value=mana_value,
        rarity="common",
        types=("Creature",),
        oracle_text=oracle_text,
        set_code=RELATIONSHIP_SET_CODE,
    )


TOKEN_SOURCE = _relationship_card(TOKEN_SOURCE_ID, "Token Enabler", TOKEN_PARAGRAPH)
ANTHEM = _relationship_card(ANTHEM_ID, "Anthem Payoff", ANTHEM_PARAGRAPH)
THRESHOLD = _relationship_card(THRESHOLD_ID, "Threshold Payoff", THRESHOLD_PARAGRAPH)
TYPAL = _relationship_card(TYPAL_ID, "Typal Payoff", TYPAL_PARAGRAPH)
OUTLET = _relationship_card(OUTLET_ID, "Outlet Payoff", OUTLET_PARAGRAPH)
TIMED_OUTLET = _relationship_card(TIMED_OUTLET_ID, "Timed Outlet", TIMED_OUTLET_PARAGRAPH)
ARTIFACT_OUTLET = _relationship_card(
    ARTIFACT_OUTLET_ID,
    "Artifact Outlet",
    ARTIFACT_OUTLET_PARAGRAPH,
)
UPKEEP_SOURCE = _relationship_card(UPKEEP_SOURCE_ID, "Upkeep Enabler", UPKEEP_PARAGRAPH)
DISCARD = _relationship_card(DISCARD_ID, "Discard Enabler", DISCARD_PARAGRAPH)
RECURSION = _relationship_card(RECURSION_ID, "Recursion Payoff", RECURSION_PARAGRAPH)
MYR_SOURCE = _relationship_card(MYR_SOURCE_ID, "Myr Enabler", MYR_PARAGRAPH)
SELF_RETURN = _relationship_card(SELF_RETURN_ID, "Self Return", SELF_RETURN_PARAGRAPH)
OPPONENT_DEATH = _relationship_card(OPPONENT_DEATH_ID, "Opponent Death", OPPONENT_DEATH_PARAGRAPH)
GHOST_SOURCE = _relationship_card(GHOST_SOURCE_ID, "Ghost Enabler", TOKEN_PARAGRAPH)
OFF_PLAN_SOURCE = _relationship_card(
    OFF_PLAN_SOURCE_ID,
    "Off Plan Enabler",
    TOKEN_PARAGRAPH,
    mana_value=6,
)
RED_ANTHEM = _relationship_card(RED_ANTHEM_ID, "Red Anthem", RED_ANTHEM_PARAGRAPH)
ELF_TYPAL = _relationship_card(ELF_TYPAL_ID, "Elf Typal", ELF_TYPAL_PARAGRAPH)
NONTOKEN_DEATH = _relationship_card(
    NONTOKEN_DEATH_ID,
    "Nontoken Death",
    NONTOKEN_DEATH_PARAGRAPH,
)
GRAVEYARD_PAYOFF = _relationship_card(
    GRAVEYARD_PAYOFF_ID,
    "Graveyard Payoff",
    GRAVEYARD_PAYOFF_PARAGRAPH,
)
OWNED_OUTLET = _relationship_card(OWNED_OUTLET_ID, "Owned Outlet", OWNED_OUTLET_PARAGRAPH)
TWIN_OUTLET = _relationship_card(TWIN_OUTLET_ID, "Twin Outlet", TWIN_OUTLET_PARAGRAPH)
OPPONENT_RECURSION = _relationship_card(
    OPPONENT_RECURSION_ID,
    "Opponent Recursion",
    OPPONENT_RECURSION_PARAGRAPH,
)
EXACT_OUTLET = _relationship_card(EXACT_OUTLET_ID, "Exact Outlet", EXACT_OUTLET_PARAGRAPH)
UP_TO_OUTLET = _relationship_card(UP_TO_OUTLET_ID, "Up To Outlet", UP_TO_OUTLET_PARAGRAPH)
ABUNDANT_SOURCE = _relationship_card(ABUNDANT_SOURCE_ID, "Abundant Enabler", ABUNDANT_PARAGRAPH)
BOUNDED_SOURCE = _relationship_card(BOUNDED_SOURCE_ID, "Bounded Enabler", BOUNDED_PARAGRAPH)
CAPPED_SOURCE = _relationship_card(CAPPED_SOURCE_ID, "Capped Enabler", CAPPED_PARAGRAPH)
MILL_SOURCE = _relationship_card(MILL_SOURCE_ID, "Mill Enabler", MILL_PARAGRAPH)

TOKEN_SOURCE_PREREQUISITE = (
    "source:condition/create/token;types=all_of:creature;token=token;subtype=soldier;"
    "color=exact:W;controller=you;qty=exactly/2;zones=none->battlefield/you"
)
UPKEEP_SOURCE_PREREQUISITE = (
    "source:condition/create/token;types=all_of:creature;token=token;subtype=soldier;"
    "color=exact:W;controller=you;qty=exactly/1;zones=none->battlefield/you;"
    "timing=upkeep/your/none"
)
MYR_SOURCE_PREREQUISITE = (
    "source:condition/create/token;types=all_of:artifact,creature;token=token;subtype=myr;"
    "color=exact:;controller=you;qty=exactly/1;zones=none->battlefield/you"
)
DISCARD_PREREQUISITE = (
    "source:condition/discard/card;types=all_of:creature;controller=you;qty=exactly/1;"
    "zones=hand/you->graveyard/you"
)
ANTHEM_PREREQUISITE = "target:condition/control/permanent;types=all_of:creature;controller=you"
THRESHOLD_PREREQUISITE = (
    "target:threshold/control/permanent;types=all_of:creature;controller=you;qty=at_least/3"
)
TYPAL_PREREQUISITE = (
    "target:condition/control/permanent;types=all_of:creature;subtype=soldier;controller=you"
)
RECURSION_PREREQUISITE = (
    "target:condition/return/card;types=all_of:creature;controller=you;"
    "zones=graveyard/you->hand/you"
)
ARTIFACT_OUTLET_PREREQUISITE = (
    "target:cost/sacrifice/permanent;types=all_of:artifact,creature;controller=you;"
    "qty=exactly/1;zones=none->graveyard/owner"
)
TIMED_OUTLET_PREREQUISITE = (
    "target:cost/sacrifice/permanent;types=all_of:creature;controller=you;qty=exactly/1;"
    "zones=none->graveyard/owner;timing=unrestricted/your/1"
)
TWIN_CREATURE_OUTLET_PREREQUISITE = (
    "target:cost/sacrifice/permanent;types=all_of:creature;controller=you;qty=exactly/1;"
    "zones=none->graveyard/owner"
)
TWIN_ARTIFACT_OUTLET_PREREQUISITE = (
    "target:cost/sacrifice/permanent;types=all_of:artifact;controller=you;qty=exactly/1;"
    "zones=none->graveyard/owner"
)


def _clause(
    card: CardInfo,
    *,
    operation_quote: str,
    object_quote: str,
    **overrides: Any,
) -> RelationshipPrerequisite:
    """Build one typed clause bound to its own card paragraph."""
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.CONDITION,
        "subject": "output",
        "operation": "create",
        "object_kind": "token",
        "card_types": ("creature",),
        "type_operator": "all_of",
        "token_restriction": "token",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": (),
        "controller": "you",
        "owner": "not_applicable",
        "quantity": CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
        "source_zone": None,
        "destination_zone": RelationshipZone(zone=CapabilityZone.BATTLEFIELD, player="you"),
        "timing": RelationshipTiming(window="unrestricted", turn="any", max_per_turn=None),
        "required_card_id": None,
        "evidence": OracleEvidence(
            card_id=card.grp_id,
            face_index=None,
            quote=card.oracle_text or "",
        ),
        "operation_quote": operation_quote,
        "operation_occurrence": 0,
        "object_quote": object_quote,
        "object_occurrence": 0,
        "capability_prerequisite_indices": (),
    }
    values.update(overrides)
    return RelationshipPrerequisite(**values)


def _create_token_clause(
    *,
    card: CardInfo,
    object_quote: str,
    quantity: int,
    **overrides: Any,
) -> RelationshipPrerequisite:
    overrides.setdefault("operation_quote", "Create")
    return _clause(
        card,
        object_quote=object_quote,
        quantity=CapabilityQuantity(value=quantity, relation=QuantityRelation.EXACTLY),
        **overrides,
    )


def _token_output_clause(card: CardInfo = TOKEN_SOURCE) -> RelationshipPrerequisite:
    return _create_token_clause(
        card=card,
        object_quote="two 1/1 white Soldier creature tokens",
        quantity=2,
        subtype="soldier",
        color_operator="exact",
        colors=("W",),
    )


def _upkeep_output_clause() -> RelationshipPrerequisite:
    return _create_token_clause(
        card=UPKEEP_SOURCE,
        object_quote="a 1/1 white Soldier creature token",
        quantity=1,
        operation_quote="create",
        subtype="soldier",
        color_operator="exact",
        colors=("W",),
        timing=RelationshipTiming(window="upkeep", turn="your", max_per_turn=None),
    )


def _myr_output_clause() -> RelationshipPrerequisite:
    return _create_token_clause(
        card=MYR_SOURCE,
        object_quote="a 1/1 colorless Myr artifact creature token",
        quantity=1,
        card_types=("artifact", "creature"),
        subtype="myr",
        color_operator="exact",
        colors=(),
    )


def _soldier_token_clause(
    *,
    card: CardInfo,
    object_quote: str,
    quantity: CapabilityQuantity,
    **overrides: Any,
) -> RelationshipPrerequisite:
    """Build one white Soldier token clause with an explicit quantity relation."""
    return _clause(
        card,
        operation_quote="Create",
        object_quote=object_quote,
        quantity=quantity,
        subtype="soldier",
        color_operator="exact",
        colors=("W",),
        **overrides,
    )


def _abundant_output_clause() -> RelationshipPrerequisite:
    return _soldier_token_clause(
        card=ABUNDANT_SOURCE,
        object_quote="three or more 1/1 white Soldier creature tokens",
        quantity=CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST),
    )


def _bounded_output_clause() -> RelationshipPrerequisite:
    return _soldier_token_clause(
        card=BOUNDED_SOURCE,
        object_quote="up to two 1/1 white Soldier creature tokens",
        quantity=CapabilityQuantity(value=2, relation=QuantityRelation.AT_MOST),
    )


def _capped_output_clause() -> RelationshipPrerequisite:
    return _soldier_token_clause(
        card=CAPPED_SOURCE,
        object_quote="two 1/1 white Soldier creature tokens",
        quantity=CapabilityQuantity(value=2, relation=QuantityRelation.EXACTLY),
        timing=RelationshipTiming(window="unrestricted", turn="your", max_per_turn=1),
    )


def _creature_condition_clause(
    *,
    card: CardInfo,
    operation_quote: str,
    object_quote: str,
    **overrides: Any,
) -> RelationshipPrerequisite:
    values: dict[str, Any] = {
        "subject": "participant",
        "operation": "control",
        "object_kind": "permanent",
        "token_restriction": "unrestricted",
        "quantity": None,
        "destination_zone": None,
    }
    values.update(overrides)
    return _clause(card, operation_quote=operation_quote, object_quote=object_quote, **values)


def _anthem_clause() -> RelationshipPrerequisite:
    return _creature_condition_clause(
        card=ANTHEM,
        operation_quote="control",
        object_quote="Creatures you control",
    )


def _variable_anthem_clause() -> RelationshipPrerequisite:
    return _creature_condition_clause(
        card=ANTHEM,
        operation_quote="control",
        object_quote="Creatures you control",
        quantity=CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE),
    )


def _red_anthem_clause() -> RelationshipPrerequisite:
    return _creature_condition_clause(
        card=RED_ANTHEM,
        operation_quote="control",
        object_quote="Red creatures you control",
        color_operator="exact",
        colors=("R",),
    )


def _soldier_typal_clause() -> RelationshipPrerequisite:
    return _creature_condition_clause(
        card=TYPAL,
        operation_quote="control",
        object_quote="Soldier creatures you control",
        subtype="soldier",
    )


def _elf_typal_clause() -> RelationshipPrerequisite:
    return _creature_condition_clause(
        card=ELF_TYPAL,
        operation_quote="control",
        object_quote="Elf creatures you control",
        subtype="elf",
    )


def _threshold_clause() -> RelationshipPrerequisite:
    return _creature_condition_clause(
        card=THRESHOLD,
        operation_quote="control",
        object_quote="you control three or more creatures",
        kind=PrerequisiteKind.THRESHOLD,
        subject="input",
        quantity=CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST),
    )


def _capped_threshold_clause() -> RelationshipPrerequisite:
    """Raise the payoff's own per-turn limit above the source's, so the smaller one caps."""
    return replace(
        _threshold_clause(),
        timing=RelationshipTiming(window="unrestricted", turn="any", max_per_turn=4),
    )


def _graveyard_payoff_clause() -> RelationshipPrerequisite:
    return _creature_condition_clause(
        card=GRAVEYARD_PAYOFF,
        operation_quote="have",
        object_quote="three or more creature cards in your graveyard",
        kind=PrerequisiteKind.THRESHOLD,
        subject="input",
        operation="count",
        object_kind="card",
        quantity=CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST),
        source_zone=RelationshipZone(zone=CapabilityZone.GRAVEYARD, player="you"),
    )


def _typed_only_graveyard_payoff_clause() -> RelationshipPrerequisite:
    """Drop the declared graveyard zone of a count clause proven only by its quote."""
    return replace(_graveyard_payoff_clause(), source_zone=None)


def _outlet_clause(
    *,
    card: CardInfo = OUTLET,
    object_quote: str = "a creature",
    **overrides: Any,
) -> RelationshipPrerequisite:
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.COST,
        "subject": "input",
        "operation": "sacrifice",
        "object_kind": "permanent",
        "token_restriction": "unrestricted",
        "destination_zone": RelationshipZone(zone=CapabilityZone.GRAVEYARD, player="owner"),
        "operation_quote": "Sacrifice",
    }
    values.update(overrides)
    return _clause(card, object_quote=object_quote, **values)


def _timed_outlet_clause() -> RelationshipPrerequisite:
    return _outlet_clause(
        card=TIMED_OUTLET,
        timing=RelationshipTiming(window="unrestricted", turn="your", max_per_turn=1),
    )


def _artifact_outlet_clause() -> RelationshipPrerequisite:
    return _outlet_clause(
        card=ARTIFACT_OUTLET,
        object_quote="an artifact creature",
        card_types=("artifact", "creature"),
    )


def _owned_outlet_clause() -> RelationshipPrerequisite:
    return _outlet_clause(card=OWNED_OUTLET, object_quote="a creature you own", owner="you")


def _twin_outlet_clauses() -> tuple[RelationshipPrerequisite, RelationshipPrerequisite]:
    """Build the two separate sacrifice requirements of one outlet card."""
    creature, artifact = TWIN_OUTLET_PARAGRAPH.split("\n")
    return (
        _outlet_clause(
            card=TWIN_OUTLET,
            object_quote="a creature",
            evidence=OracleEvidence(card_id=TWIN_OUTLET.grp_id, face_index=None, quote=creature),
        ),
        _outlet_clause(
            card=TWIN_OUTLET,
            object_quote="an artifact",
            card_types=("artifact",),
            evidence=OracleEvidence(card_id=TWIN_OUTLET.grp_id, face_index=None, quote=artifact),
        ),
    )


def _exact_outlet_clause() -> RelationshipPrerequisite:
    return _outlet_clause(
        card=EXACT_OUTLET,
        object_quote="three creatures",
        quantity=CapabilityQuantity(value=3, relation=QuantityRelation.EXACTLY),
    )


def _up_to_outlet_clause() -> RelationshipPrerequisite:
    return _outlet_clause(
        card=UP_TO_OUTLET,
        object_quote="up to two creatures",
        quantity=CapabilityQuantity(value=2, relation=QuantityRelation.AT_MOST),
    )


def _death_payoff_clause(
    *,
    card: CardInfo,
    object_quote: str,
    **overrides: Any,
) -> RelationshipPrerequisite:
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.TRIGGER,
        "subject": "event",
        "operation": "die",
        "object_kind": "permanent",
        "token_restriction": "unrestricted",
        "destination_zone": RelationshipZone(zone=CapabilityZone.GRAVEYARD, player="owner"),
        "operation_quote": "dies",
    }
    values.update(overrides)
    return _clause(card, object_quote=object_quote, **values)


def _opponent_death_clause() -> RelationshipPrerequisite:
    return _death_payoff_clause(
        card=OPPONENT_DEATH,
        object_quote="a creature an opponent controls",
        controller="opponent",
    )


def _nontoken_death_clause() -> RelationshipPrerequisite:
    return _death_payoff_clause(
        card=NONTOKEN_DEATH,
        object_quote="a nontoken creature you control",
        token_restriction="nontoken",
    )


def _discard_clause() -> RelationshipPrerequisite:
    return _clause(
        DISCARD,
        operation_quote="Discard",
        object_quote="a creature card",
        subject="input",
        operation="discard",
        object_kind="card",
        token_restriction="unrestricted",
        source_zone=RelationshipZone(zone=CapabilityZone.HAND, player="you"),
        destination_zone=RelationshipZone(zone=CapabilityZone.GRAVEYARD, player="you"),
    )


def _return_clause() -> RelationshipPrerequisite:
    return _clause(
        RECURSION,
        operation_quote="Return",
        object_quote="target creature card from your graveyard",
        subject="output",
        operation="return",
        object_kind="card",
        token_restriction="unrestricted",
        quantity=None,
        source_zone=RelationshipZone(zone=CapabilityZone.GRAVEYARD, player="you"),
        destination_zone=RelationshipZone(zone=CapabilityZone.HAND, player="you"),
    )


def _self_return_clause() -> RelationshipPrerequisite:
    return replace(
        _return_clause(),
        subject="participant",
        object_quote="this creature card",
        required_card_id=SELF_RETURN.grp_id,
        evidence=OracleEvidence(
            card_id=SELF_RETURN.grp_id,
            face_index=None,
            quote=SELF_RETURN_PARAGRAPH,
        ),
    )


def _opponent_recursion_clause() -> RelationshipPrerequisite:
    # The article of "an opponent's graveyard" is read as one stated card, so the
    # clause declares the exact single card it returns.
    return _clause(
        OPPONENT_RECURSION,
        operation_quote="Return",
        object_quote="target creature card from an opponent's graveyard",
        subject="output",
        operation="return",
        object_kind="card",
        token_restriction="unrestricted",
        quantity=CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
        source_zone=RelationshipZone(zone=CapabilityZone.GRAVEYARD, player="opponent"),
        destination_zone=RelationshipZone(zone=CapabilityZone.HAND, player="you"),
    )


def _mill_clause() -> RelationshipPrerequisite:
    return _clause(
        MILL_SOURCE,
        operation_quote="Mill",
        object_quote="three cards",
        subject="input",
        operation="mill",
        object_kind="card",
        token_restriction="unrestricted",
        quantity=CapabilityQuantity(value=3, relation=QuantityRelation.EXACTLY),
        source_zone=RelationshipZone(zone=CapabilityZone.LIBRARY, player="you"),
        destination_zone=RelationshipZone(zone=CapabilityZone.GRAVEYARD, player="you"),
    )


def _relationship_participant(
    card: CardInfo,
    role: Role,
    *prerequisites: RelationshipPrerequisite,
) -> RelationshipParticipant:
    return RelationshipParticipant(
        card_id=card.grp_id,
        capability_id=f"capability-{card.grp_id}",
        card_name=card.name,
        face_index=None,
        face_name=None,
        card_source_sha256=card_source_sha256(card),
        role=role,
        capability_prerequisites=(),
        prerequisites=prerequisites,
    )


def _relationship(
    *,
    mechanism: str,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    finding_id: str | None = None,
    projection: bool = True,
) -> CardRelationship:
    evidence = tuple(
        dict.fromkeys(
            clause.evidence
            for participant in (source, target)
            for clause in participant.prerequisites
        )
    )
    return CardRelationship(
        finding_id=(
            f"relationship:{mechanism}:{source.card_id}:{target.card_id}"
            if finding_id is None
            else finding_id
        ),
        mechanism=mechanism,
        participants=(source.card_id, target.card_id),
        claim=RELATIONSHIP_CLAIM,
        prerequisites=(RELATIONSHIP_PREREQUISITE_SENTINEL,),
        oracle_evidence=evidence,
        guide_evidence=(
            GuideEvidence(guide_id=RELATIONSHIP_GUIDE_ID, quote=RELATIONSHIP_GUIDE_SENTINEL),
        ),
        review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
        run_id=RELATIONSHIP_RUN_ID,
        prerequisite_projection=(
            RelationshipPrerequisiteProjection(source=source, target=target) if projection else None
        ),
    )


def _role_card(card: CardInfo, role: Role, confidence: float) -> ProfileCard:
    return ProfileCard(
        key=f"arena_id:{card.grp_id}",
        assignments=(RoleAssignment(role, confidence=confidence),),
    )


def _relationship_profile(
    *,
    cards: tuple[CardInfo, ...],
    relationships: tuple[CardRelationship, ...],
    role_cards: tuple[ProfileCard, ...],
) -> SetProfile:
    """Build one real schema-three profile carrying confirmed typed enhancement records."""
    return SetProfile(
        set_code=RELATIONSHIP_SET_CODE,
        event_format="quickdraft",
        profile_version="relationship-tests-1",
        generated_at=_RELATIONSHIP_TIMESTAMP,
        source=SourceMetadata(provider="test"),
        maturity=ProfileMaturity.SEMANTIC_ONLY,
        samples=None,
        confidence=0.5,
        pairs=(),
        role_profile=CompiledRoleProfile(set_code=RELATIONSHIP_SET_CODE, cards=role_cards),
        schema_version=3,
        enhancement=SetProfileEnhancement(
            artifact_schema_version=SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
            artifact_sha256=_RELATIONSHIP_ARTIFACT_SHA256,
            set_code=RELATIONSHIP_SET_CODE,
            set_source_id="test-card-data",
            set_source_sha256=_RELATIONSHIP_SET_SOURCE_SHA256,
            created_at=_RELATIONSHIP_TIMESTAMP,
            card_data=EnhancementCardData(
                source="test-cards",
                sha256=_RELATIONSHIP_CARD_DATA_SHA256,
                card_count=len(cards),
            ),
            cards=tuple(
                CardSourcePin(
                    card_id=card.grp_id,
                    oracle_id=None,
                    collector_number=None,
                    sha256=card_source_sha256(card),
                )
                for card in cards
            ),
            guides=(
                GuideSourcePin(
                    guide_id=RELATIONSHIP_GUIDE_ID,
                    url="https://guide.example.test/relationship-tests",
                    sha256=_RELATIONSHIP_GUIDE_SHA256,
                    retrieved_at=_RELATIONSHIP_TIMESTAMP,
                ),
            ),
            runs=(
                ModelRun(
                    run_id=RELATIONSHIP_RUN_ID,
                    provider="test-provider",
                    model=RELATIONSHIP_MODEL_SENTINEL,
                    reasoning=ReasoningConfig(enabled=True, effort="low", max_tokens=1024, exclude=None),
                    prompt_id="relationship-tests-prompt",
                    prompt_sha256=_RELATIONSHIP_PROMPT_SHA256,
                    response_schema_id="relationship-tests-schema",
                    response_schema_sha256=_RELATIONSHIP_SCHEMA_SHA256,
                    started_at=_RELATIONSHIP_TIMESTAMP,
                    completed_at=_RELATIONSHIP_REVIEWED_AT,
                    input_tokens=10,
                    output_tokens=10,
                    reasoning_tokens=None,
                    cost_usd=None,
                ),
            ),
            mechanics=(),
            relationships=relationships,
            review=ArtifactReview(
                state="confirmed",
                reviewer_id="test-reviewer",
                reviewed_at=_RELATIONSHIP_REVIEWED_AT,
            ),
            confidence=0.8,
        ),
    )


@dataclass(frozen=True)
class _RelationshipCase:
    """One supported typed relationship together with its expected projection."""

    source: CardInfo
    target: CardInfo
    relationship: CardRelationship
    mechanism: str
    source_role: Role
    target_role: Role
    prerequisites: tuple[str, ...]
    source_count: int = 1
    source_confidence: float = 0.9
    target_confidence: float = 0.8

    @property
    def cards(self) -> tuple[CardInfo, ...]:
        return (self.source, self.target)

    def profile(self) -> SetProfile:
        return _relationship_profile(
            cards=self.cards,
            relationships=(self.relationship,),
            role_cards=(
                _role_card(self.source, self.source_role, self.source_confidence),
                _role_card(self.target, self.target_role, self.target_confidence),
            ),
        )

    def ledger(self, *, pool: tuple[int, ...] | None = None) -> PoolRoleLedger:
        return project_pool_role_ledger(
            pool_before_pick=(self.source.grp_id,) * self.source_count if pool is None else pool,
            pack_number=0,
            pick_number=4,
            global_pick_index=5,
            estimated_remaining_picks=37,
            card_database=_database(*self.cards),
            set_profile=self.profile(),
        )


@dataclass(frozen=True)
class _WithheldCase:
    """One typed relationship that must never reach the ledger."""

    profile: SetProfile
    database: CardDatabase
    pool: tuple[int, ...]
    relationship_cards: tuple[CardInfo, CardInfo]
    ratings_data: Any = None


def _evaluate_withheld_case(case: _WithheldCase) -> PoolRoleLedger:
    return project_pool_role_ledger(
        pool_before_pick=case.pool,
        pack_number=0,
        pick_number=4,
        global_pick_index=5,
        estimated_remaining_picks=37,
        card_database=case.database,
        ratings_data=case.ratings_data,
        set_profile=case.profile,
    )


def _typed_withheld_case(
    *,
    mechanism: str,
    source_card: CardInfo,
    source_role: Role,
    source_clauses: tuple[RelationshipPrerequisite, ...],
    target_card: CardInfo,
    target_role: Role,
    target_clauses: tuple[RelationshipPrerequisite, ...],
    pool: tuple[int, ...] | None = None,
    database_cards: tuple[CardInfo, ...] | None = None,
    role_cards: tuple[ProfileCard, ...] | None = None,
    ratings_data: Any = None,
    projection: bool = True,
) -> _WithheldCase:
    relationship = _relationship(
        mechanism=mechanism,
        source=_relationship_participant(source_card, source_role, *source_clauses),
        target=_relationship_participant(target_card, target_role, *target_clauses),
        projection=projection,
    )
    return _WithheldCase(
        profile=_relationship_profile(
            cards=(source_card, target_card),
            relationships=(relationship,),
            role_cards=(
                (
                    _role_card(source_card, source_role, 0.9),
                    _role_card(target_card, target_role, 0.8),
                )
                if role_cards is None
                else role_cards
            ),
        ),
        database=_database(
            *(source_card, target_card) if database_cards is None else database_cards
        ),
        pool=(source_card.grp_id,) if pool is None else pool,
        relationship_cards=(source_card, target_card),
        ratings_data=ratings_data,
    )


def _directional_enabler_payoff_case() -> _RelationshipCase:
    return _RelationshipCase(
        source=TOKEN_SOURCE,
        target=ANTHEM,
        relationship=_relationship(
            mechanism="token-go-wide-payoff",
            source=_relationship_participant(TOKEN_SOURCE, Role.TOKEN_MAKER, _token_output_clause()),
            target=_relationship_participant(ANTHEM, Role.GO_WIDE_PAYOFF, _anthem_clause()),
        ),
        mechanism="token-go-wide-payoff",
        source_role=Role.TOKEN_MAKER,
        target_role=Role.GO_WIDE_PAYOFF,
        prerequisites=(TOKEN_SOURCE_PREREQUISITE, ANTHEM_PREREQUISITE),
    )


def _fixed_threshold_case() -> _RelationshipCase:
    return _RelationshipCase(
        source=TOKEN_SOURCE,
        target=THRESHOLD,
        relationship=_relationship(
            mechanism="token-go-wide-payoff",
            source=_relationship_participant(TOKEN_SOURCE, Role.TOKEN_MAKER, _token_output_clause()),
            target=_relationship_participant(THRESHOLD, Role.GO_WIDE_PAYOFF, _threshold_clause()),
        ),
        mechanism="token-go-wide-payoff",
        source_role=Role.TOKEN_MAKER,
        target_role=Role.GO_WIDE_PAYOFF,
        prerequisites=(TOKEN_SOURCE_PREREQUISITE, THRESHOLD_PREREQUISITE),
        source_count=2,
    )


def _discard_to_return_case() -> _RelationshipCase:
    return _RelationshipCase(
        source=DISCARD,
        target=RECURSION,
        relationship=_relationship(
            mechanism="discard-recursion-payoff",
            source=_relationship_participant(
                DISCARD,
                Role.DISCARD_ENABLER,
                _discard_clause(),
            ),
            target=_relationship_participant(RECURSION, Role.RECURSION_PAYOFF, _return_clause()),
        ),
        mechanism="discard-recursion-payoff",
        source_role=Role.DISCARD_ENABLER,
        target_role=Role.RECURSION_PAYOFF,
        prerequisites=(DISCARD_PREREQUISITE, RECURSION_PREREQUISITE),
    )


def _soldier_typal_case() -> _RelationshipCase:
    return _RelationshipCase(
        source=TOKEN_SOURCE,
        target=TYPAL,
        relationship=_relationship(
            mechanism="token-go-wide-payoff",
            source=_relationship_participant(TOKEN_SOURCE, Role.TOKEN_MAKER, _token_output_clause()),
            target=_relationship_participant(TYPAL, Role.GO_WIDE_PAYOFF, _soldier_typal_clause()),
        ),
        mechanism="token-go-wide-payoff",
        source_role=Role.TOKEN_MAKER,
        target_role=Role.GO_WIDE_PAYOFF,
        prerequisites=(TOKEN_SOURCE_PREREQUISITE, TYPAL_PREREQUISITE),
    )


def _myr_artifact_outlet_case() -> _RelationshipCase:
    return _RelationshipCase(
        source=MYR_SOURCE,
        target=ARTIFACT_OUTLET,
        relationship=_relationship(
            mechanism="token-sacrifice-outlet",
            source=_relationship_participant(MYR_SOURCE, Role.TOKEN_MAKER, _myr_output_clause()),
            target=_relationship_participant(
                ARTIFACT_OUTLET,
                Role.SACRIFICE_OUTLET,
                _artifact_outlet_clause(),
            ),
        ),
        mechanism="token-sacrifice-outlet",
        source_role=Role.TOKEN_MAKER,
        target_role=Role.SACRIFICE_OUTLET,
        prerequisites=(MYR_SOURCE_PREREQUISITE, ARTIFACT_OUTLET_PREREQUISITE),
    )


def _restricted_timing_case() -> _RelationshipCase:
    return _RelationshipCase(
        source=UPKEEP_SOURCE,
        target=TIMED_OUTLET,
        relationship=_relationship(
            mechanism="token-sacrifice-outlet",
            source=_relationship_participant(
                UPKEEP_SOURCE,
                Role.TOKEN_MAKER,
                _upkeep_output_clause(),
            ),
            target=_relationship_participant(
                TIMED_OUTLET,
                Role.SACRIFICE_OUTLET,
                _timed_outlet_clause(),
            ),
        ),
        mechanism="token-sacrifice-outlet",
        source_role=Role.TOKEN_MAKER,
        target_role=Role.SACRIFICE_OUTLET,
        prerequisites=(UPKEEP_SOURCE_PREREQUISITE, TIMED_OUTLET_PREREQUISITE),
    )


def _conjunctive_outlet_case() -> _RelationshipCase:
    """Prove every sacrifice requirement of one outlet with one artifact creature token."""
    creature, artifact = _twin_outlet_clauses()
    return _RelationshipCase(
        source=MYR_SOURCE,
        target=TWIN_OUTLET,
        relationship=_relationship(
            mechanism="token-sacrifice-outlet",
            source=_relationship_participant(MYR_SOURCE, Role.TOKEN_MAKER, _myr_output_clause()),
            target=_relationship_participant(
                TWIN_OUTLET,
                Role.SACRIFICE_OUTLET,
                creature,
                artifact,
            ),
        ),
        mechanism="token-sacrifice-outlet",
        source_role=Role.TOKEN_MAKER,
        target_role=Role.SACRIFICE_OUTLET,
        prerequisites=tuple(
            sorted(
                (
                    MYR_SOURCE_PREREQUISITE,
                    TWIN_ARTIFACT_OUTLET_PREREQUISITE,
                    TWIN_CREATURE_OUTLET_PREREQUISITE,
                )
            )
        ),
    )


def _color_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=RED_ANTHEM,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_red_anthem_clause(),),
    )


def _type_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-sacrifice-outlet",
        source_card=UPKEEP_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_upkeep_output_clause(),),
        target_card=ARTIFACT_OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(_artifact_outlet_clause(),),
    )


def _subtype_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=ELF_TYPAL,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_elf_typal_clause(),),
    )


def _token_restriction_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-death-payoff",
        source_card=UPKEEP_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_upkeep_output_clause(),),
        target_card=NONTOKEN_DEATH,
        target_role=Role.DEATH_PAYOFF,
        target_clauses=(_nontoken_death_clause(),),
    )


def _controller_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-death-payoff",
        source_card=UPKEEP_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_upkeep_output_clause(),),
        target_card=OPPONENT_DEATH,
        target_role=Role.DEATH_PAYOFF,
        target_clauses=(_opponent_death_clause(),),
    )


def _quantity_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=THRESHOLD,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_threshold_clause(),),
    )


def _object_identity_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="discard-recursion-payoff",
        source_card=DISCARD,
        source_role=Role.DISCARD_ENABLER,
        source_clauses=(_discard_clause(),),
        target_card=SELF_RETURN,
        target_role=Role.RECURSION_PAYOFF,
        target_clauses=(_self_return_clause(),),
    )


def _role_mechanism_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(_outlet_clause(),),
    )


def _free_text_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=ANTHEM,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_anthem_clause(),),
        projection=False,
    )


def _missing_source_card_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=GHOST_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(GHOST_SOURCE),),
        target_card=ANTHEM,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_anthem_clause(),),
        pool=(GHOST_SOURCE_ID,),
        database_cards=(ANTHEM,),
    )


def _local_role_resolution_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=ANTHEM,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_anthem_clause(),),
        role_cards=(_role_card(ANTHEM, Role.GO_WIDE_PAYOFF, 0.8),),
    )


def _cut_source_case() -> _WithheldCase:
    fillers = tuple(
        _relationship_card(grp_id, f"Filler {grp_id}", "Filler.", mana_value=2)
        for grp_id in _CUT_CASE_FILLER_IDS
    )
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=OFF_PLAN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(OFF_PLAN_SOURCE),),
        target_card=ANTHEM,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_anthem_clause(),),
        pool=(*_CUT_CASE_FILLER_IDS, OFF_PLAN_SOURCE_ID),
        database_cards=(*fillers, OFF_PLAN_SOURCE, ANTHEM),
        ratings_data=_ProjectionRatings(high_rating_grp_ids=frozenset(_CUT_CASE_FILLER_IDS)),
    )


def _return_only_recursion_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="recursion-graveyard-payoff",
        source_card=RECURSION,
        source_role=Role.RECURSION,
        source_clauses=(_return_clause(),),
        target_card=GRAVEYARD_PAYOFF,
        target_role=Role.GRAVEYARD_PAYOFF,
        target_clauses=(_graveyard_payoff_clause(),),
    )


def _timing_withheld_case() -> _WithheldCase:
    return _typed_withheld_case(
        mechanism="token-sacrifice-outlet",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=TIMED_OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(_timed_outlet_clause(),),
    )


def _returned_fodder_case() -> _WithheldCase:
    # The fodder's declared transition is graveyard to hand: the object never
    # reaches the graveyard a sacrifice outlet requires.
    return _typed_withheld_case(
        mechanism="fodder-sacrifice-outlet",
        source_card=SELF_RETURN,
        source_role=Role.SACRIFICE_FODDER,
        source_clauses=(_self_return_clause(),),
        target_card=OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(_outlet_clause(),),
    )


def _owner_withheld_case() -> _WithheldCase:
    # The outlet requires a creature its controller owns; the token source
    # declares no owner for the object it creates.
    return _typed_withheld_case(
        mechanism="token-sacrifice-outlet",
        source_card=UPKEEP_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_upkeep_output_clause(),),
        target_card=OWNED_OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(_owned_outlet_clause(),),
    )


def _zone_player_withheld_case() -> _WithheldCase:
    # The discarded card reaches your graveyard, but the payoff returns a card
    # from another player's graveyard.
    return _typed_withheld_case(
        mechanism="discard-recursion-payoff",
        source_card=DISCARD,
        source_role=Role.DISCARD_ENABLER,
        source_clauses=(_discard_clause(),),
        target_card=OPPONENT_RECURSION,
        target_role=Role.RECURSION_PAYOFF,
        target_clauses=(_opponent_recursion_clause(),),
    )


def _partial_requirement_withheld_case() -> _WithheldCase:
    # The outlet demands a creature and an artifact; the Soldier token is not
    # an artifact, so one of its two requirements stays unproven.
    creature, artifact = _twin_outlet_clauses()
    return _typed_withheld_case(
        mechanism="token-sacrifice-outlet",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=TWIN_OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(creature, artifact),
    )


def _at_least_exactly_withheld_case() -> _WithheldCase:
    # A lower-bounded supply satisfies the threshold value but never proves an
    # exact amount.
    return _typed_withheld_case(
        mechanism="token-sacrifice-outlet",
        source_card=ABUNDANT_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_abundant_output_clause(),),
        target_card=EXACT_OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(_exact_outlet_clause(),),
    )


def _at_least_at_most_withheld_case() -> _WithheldCase:
    # The guaranteed minimum already exceeds the declared upper bound.
    return _typed_withheld_case(
        mechanism="token-sacrifice-outlet",
        source_card=ABUNDANT_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_abundant_output_clause(),),
        target_card=UP_TO_OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(_up_to_outlet_clause(),),
    )


def _at_most_source_withheld_case() -> _WithheldCase:
    # An upper-bounded source proves no amount at all, even when its bound would
    # fit under the payoff's own upper bound.
    return _typed_withheld_case(
        mechanism="token-sacrifice-outlet",
        source_card=BOUNDED_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_bounded_output_clause(),),
        target_card=UP_TO_OUTLET,
        target_role=Role.SACRIFICE_OUTLET,
        target_clauses=(_up_to_outlet_clause(),),
    )


def _variable_target_quantity_withheld_case() -> _WithheldCase:
    # An unknown required amount cannot be proven by any supply.
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=TOKEN_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_token_output_clause(),),
        target_card=ANTHEM,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_variable_anthem_clause(),),
    )


def _capped_supply_withheld_case() -> _WithheldCase:
    # Both limits apply and the smaller one caps the supply: two copies of an
    # exactly-two source are shortened to one token per turn, which cannot prove
    # a threshold of three.
    return _typed_withheld_case(
        mechanism="token-go-wide-payoff",
        source_card=CAPPED_SOURCE,
        source_role=Role.TOKEN_MAKER,
        source_clauses=(_capped_output_clause(),),
        target_card=THRESHOLD,
        target_role=Role.GO_WIDE_PAYOFF,
        target_clauses=(_capped_threshold_clause(),),
        pool=(CAPPED_SOURCE_ID, CAPPED_SOURCE_ID),
    )


def _quote_only_graveyard_withheld_case() -> _WithheldCase:
    # The count clause names a graveyard only in its object quote: no typed
    # zone declares it, so the #509 dispatchers must not select it.
    return _typed_withheld_case(
        mechanism="mill-graveyard-payoff",
        source_card=MILL_SOURCE,
        source_role=Role.SELF_MILL,
        source_clauses=(_mill_clause(),),
        target_card=GRAVEYARD_PAYOFF,
        target_role=Role.GRAVEYARD_PAYOFF,
        target_clauses=(_typed_only_graveyard_payoff_clause(),),
    )


def _multi_relationship_profile() -> tuple[SetProfile, tuple[CardInfo, ...], tuple[int, ...]]:
    """Build one profile with four supported relationships across three targets."""
    cards = (
        TOKEN_SOURCE,
        UPKEEP_SOURCE,
        MYR_SOURCE,
        DISCARD,
        ANTHEM,
        TYPAL,
        RECURSION,
    )
    relationships = (
        _relationship(
            mechanism="token-go-wide-payoff",
            source=_relationship_participant(TOKEN_SOURCE, Role.TOKEN_MAKER, _token_output_clause()),
            target=_relationship_participant(TYPAL, Role.GO_WIDE_PAYOFF, _soldier_typal_clause()),
            finding_id="relationship-typal-first",
        ),
        _relationship(
            mechanism="token-go-wide-payoff",
            source=_relationship_participant(
                UPKEEP_SOURCE,
                Role.TOKEN_MAKER,
                _upkeep_output_clause(),
            ),
            target=_relationship_participant(TYPAL, Role.GO_WIDE_PAYOFF, _soldier_typal_clause()),
            finding_id="relationship-typal-second",
        ),
        _relationship(
            mechanism="token-go-wide-payoff",
            source=_relationship_participant(MYR_SOURCE, Role.TOKEN_MAKER, _myr_output_clause()),
            target=_relationship_participant(ANTHEM, Role.GO_WIDE_PAYOFF, _anthem_clause()),
            finding_id="relationship-anthem",
        ),
        _relationship(
            mechanism="discard-recursion-payoff",
            source=_relationship_participant(
                DISCARD,
                Role.DISCARD_ENABLER,
                _discard_clause(),
            ),
            target=_relationship_participant(RECURSION, Role.RECURSION_PAYOFF, _return_clause()),
            finding_id="relationship-recursion",
        ),
    )
    return (
        _relationship_profile(
            cards=cards,
            relationships=relationships,
            role_cards=(
                _role_card(TOKEN_SOURCE, Role.TOKEN_MAKER, 0.9),
                _role_card(UPKEEP_SOURCE, Role.TOKEN_MAKER, 0.9),
                _role_card(MYR_SOURCE, Role.TOKEN_MAKER, 0.9),
                _role_card(DISCARD, Role.DISCARD_ENABLER, 0.9),
                _role_card(ANTHEM, Role.GO_WIDE_PAYOFF, 0.8),
                _role_card(TYPAL, Role.GO_WIDE_PAYOFF, 0.8),
                _role_card(RECURSION, Role.RECURSION_PAYOFF, 0.8),
            ),
        ),
        cards,
        (TOKEN_SOURCE_ID, UPKEEP_SOURCE_ID, MYR_SOURCE_ID, DISCARD_ID),
    )


@pytest.mark.parametrize(
    "build",
    (
        pytest.param(_directional_enabler_payoff_case, id="directional-enabler-payoff"),
        pytest.param(_fixed_threshold_case, id="fixed-threshold-payoff"),
        pytest.param(_discard_to_return_case, id="discard-to-return-recursion"),
        pytest.param(_soldier_typal_case, id="soldier-typal-match"),
        pytest.param(_myr_artifact_outlet_case, id="myr-artifact-creature-outlet"),
        pytest.param(_restricted_timing_case, id="restricted-same-turn-timing"),
        pytest.param(_conjunctive_outlet_case, id="conjunctive-outlet-requirements"),
    ),
)
def test_pre_pick_relationship_support_matches_supported_typed_categories(
    build: Callable[[], _RelationshipCase],
) -> None:
    case = build()
    ledger = case.ledger()

    assert len(ledger.relationship_support) == 1
    support = ledger.relationship_support[0]
    assert isinstance(support, RelationshipSupport)
    assert support.finding_id == case.relationship.finding_id
    assert support.mechanism == case.mechanism
    assert support.source_card_id == case.source.grp_id
    assert support.source_card_name == case.source.name
    assert support.source_card_count == case.source_count
    assert support.source_role is case.source_role
    assert support.source_role_confidence == case.source_confidence
    assert support.target_card_id == case.target.grp_id
    assert support.target_card_name == case.target.name
    assert support.target_role is case.target_role
    assert support.target_role_confidence == case.target_confidence
    assert support.satisfied_prerequisites == case.prerequisites
    assert support.to_json() == {
        "finding_id": case.relationship.finding_id,
        "mechanism": case.mechanism,
        "source": {
            "count": case.source_count,
            "grp_id": case.source.grp_id,
            "name": case.source.name,
            "role": case.source_role.value,
            "role_confidence": case.source_confidence,
        },
        "target": {
            "grp_id": case.target.grp_id,
            "name": case.target.name,
            "role": case.target_role.value,
            "role_confidence": case.target_confidence,
        },
        "satisfied_prerequisites": list(case.prerequisites),
    }
    assert ledger.to_json()["relationship_support"] == [support.to_json()]


@pytest.mark.parametrize(
    "build",
    (
        pytest.param(_color_withheld_case, id="color-mismatch"),
        pytest.param(_type_withheld_case, id="type-mismatch"),
        pytest.param(_subtype_withheld_case, id="subtype-mismatch"),
        pytest.param(_token_restriction_withheld_case, id="token-restriction-mismatch"),
        pytest.param(_controller_withheld_case, id="controller-mismatch"),
        pytest.param(_timing_withheld_case, id="timing-turn-mismatch"),
        pytest.param(_quantity_withheld_case, id="insufficient-fixed-quantity"),
        pytest.param(_object_identity_withheld_case, id="required-object-identity"),
        pytest.param(_role_mechanism_withheld_case, id="role-mechanism-mismatch"),
        pytest.param(_free_text_withheld_case, id="free-text-without-projection"),
        pytest.param(_missing_source_card_case, id="missing-source-card"),
        pytest.param(_return_only_recursion_case, id="return-only-recursion-payoff"),
        pytest.param(_returned_fodder_case, id="fodder-returns-to-hand-not-graveyard"),
        pytest.param(_owner_withheld_case, id="owner-not-declared-by-source"),
        pytest.param(_zone_player_withheld_case, id="graveyard-zone-player-mismatch"),
        pytest.param(_partial_requirement_withheld_case, id="unproven-conjunct-requirement"),
        pytest.param(_at_least_exactly_withheld_case, id="at-least-source-exact-target"),
        pytest.param(_at_least_at_most_withheld_case, id="at-least-source-at-most-target"),
        pytest.param(_at_most_source_withheld_case, id="at-most-source-withholds"),
        pytest.param(_variable_target_quantity_withheld_case, id="variable-target-quantity"),
        pytest.param(_capped_supply_withheld_case, id="both-caps-cap-the-supply"),
        pytest.param(_quote_only_graveyard_withheld_case, id="quote-only-graveyard-predicate"),
    ),
)
def test_pre_pick_relationship_support_withholds_cross_clause_incompatibility(
    build: Callable[[], _WithheldCase],
) -> None:
    case = build()

    assert _evaluate_withheld_case(case).relationship_support == ()


def test_relationship_support_requires_the_source_to_survive_the_likely_projection() -> None:
    case = _cut_source_case()
    set_profile = case.profile
    ratings_data = _ProjectionRatings(high_rating_grp_ids=frozenset(_CUT_CASE_FILLER_IDS))
    projected = _likely_projection(
        pool_grp_ids=case.pool,
        card_database=case.database,
        set_profile=set_profile,
        pair=None,
        ratings_data=ratings_data,
    )

    assert OFF_PLAN_SOURCE_ID not in projected
    assert set(projected) <= set(_CUT_CASE_FILLER_IDS)
    assert len(projected) == DECK_BUILDER.target_spell_count
    assert _evaluate_withheld_case(case).relationship_support == ()


def test_relationship_support_caps_supply_with_the_smaller_declared_limit() -> None:
    # Two copies of an exactly-two enabler supply four tokens, which satisfies the
    # threshold of three; limiting the enabler to one per turn while the payoff
    # declares a looser limit of four caps that supply at one and withholds it.
    uncapped = _fixed_threshold_case()
    capped = _capped_supply_withheld_case()

    assert len(uncapped.ledger().relationship_support) == 1
    assert _evaluate_withheld_case(capped).relationship_support == ()


def test_relationship_support_requires_a_compiled_profile_role_assignment() -> None:
    case = _local_role_resolution_case()
    source, target = case.relationship_cards
    profile = case.profile

    assert resolve_card_roles(source, profile=profile.role_profile).source == "local_classifier"
    assert resolve_card_roles(target, profile=profile.role_profile).source == "compiled_profile"
    assert _evaluate_withheld_case(case).relationship_support == ()


def test_completed_pool_evaluation_stays_relationship_neutral() -> None:
    case = _directional_enabler_payoff_case()
    completed = evaluate_completed_pool_role_ledger(
        final_pool=(case.source.grp_id, case.target.grp_id),
        card_database=_database(*case.cards),
        set_profile=case.profile(),
    )

    assert completed.mode is COMPLETED_POOL
    assert completed.relationship_support == ()
    assert completed.to_json()["relationship_support"] == []
    assert len(case.ledger().relationship_support) == 1


def test_relationship_support_orders_records_and_keeps_raw_model_text_out() -> None:
    profile, cards, pool = _multi_relationship_profile()
    database = _database(*cards)

    def ledger() -> PoolRoleLedger:
        return project_pool_role_ledger(
            pool_before_pick=pool,
            pack_number=0,
            pick_number=4,
            global_pick_index=5,
            estimated_remaining_picks=37,
            card_database=database,
            set_profile=profile,
        )

    first = ledger()
    repeated = ledger()
    payload = json.dumps(first.to_json(), sort_keys=True)

    assert first.to_json() == repeated.to_json()
    assert [
        (item.target_card_id, item.mechanism, item.finding_id, item.source_card_id)
        for item in first.relationship_support
    ] == [
        (ANTHEM_ID, "token-go-wide-payoff", "relationship-anthem", MYR_SOURCE_ID),
        (TYPAL_ID, "token-go-wide-payoff", "relationship-typal-first", TOKEN_SOURCE_ID),
        (TYPAL_ID, "token-go-wide-payoff", "relationship-typal-second", UPKEEP_SOURCE_ID),
        (RECURSION_ID, "discard-recursion-payoff", "relationship-recursion", DISCARD_ID),
    ]
    assert "zones=hand/you->graveyard/you" in payload
    assert "zones=graveyard/you->hand/you" in payload
    for sentinel in (
        RELATIONSHIP_CLAIM,
        RELATIONSHIP_PREREQUISITE_SENTINEL,
        RELATIONSHIP_GUIDE_SENTINEL,
        RELATIONSHIP_MODEL_SENTINEL,
    ):
        assert sentinel not in payload
