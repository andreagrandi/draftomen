from __future__ import annotations

from types import SimpleNamespace

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import DECK_BUILDER
from draftomen.pickengine import PickEngine
from draftomen.pool_ledger import (
    COMPLETED_POOL,
    PRE_PICK_PROJECTION,
    LedgerMode,
    _likely_projection,
    evaluate_completed_pool_role_ledger,
    project_pool_role_ledger,
)
from draftomen.semantic_roles import (
    CompiledRoleProfile,
    ProfileCard,
    RemovalCharacteristics,
    Role,
    RoleAssignment,
)
from draftomen.set_profile import (
    PairProfile,
    ProfileMaturity,
    RemovalTarget,
    RoleTarget,
    SampleSummary,
    SetProfile,
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
