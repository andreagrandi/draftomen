"""Regressions for the derived condition compiler and its frozen printed condition grammar.

The fixtures pin the real HOB Oracle text of the cards the frozen mechanic matrix names - Silvan
Reveler, Dancing from Dark to Dawn, Thranduil's Company, Down in the Valley, Elven Raft-Steerer,
Wilderland Scrounger, Nasty Little Rabbit, The Chief Warg, The Misty Mountains Cold, Fili the
Pathfinder and Bombur - under their own real card ids.  The grammar boundaries the plan calls out
(land-to-hand tutoring, opponent-only placement, power 3 versus power 4, a naked 0/0 Army, a
Treasure without its definition, an unbound keyword) use 99xxxx fixture ids with matching source
pins, so no frozen real text is ever rewritten under a synthetic card.
"""

from __future__ import annotations

import pytest

from draftomen.carddb import CardDatabase, CardFace, CardInfo
from draftomen.profile_condition_projection import compile_condition_map, condition_statements
from draftomen.semantic_capability_records import CapabilityQuantity, QuantityRelation
from draftomen.semantic_condition_records import (
    CONDITION_CAPABILITY_ID_PREFIX,
    ConditionCapability,
    ConditionMap,
)
from draftomen.semantic_enrichment import (
    EnrichmentSources,
    SemanticEnrichmentArtifact,
    card_source_sha256,
    set_source_sha256,
)
from draftomen.semantic_enrichment_records import (
    ArtifactReview,
    CardSourcePin,
    FindingReview,
    FindingStatus,
    ModelRun,
    OracleEvidence,
    OracleFact,
    ReasoningConfig,
    SemanticEnrichmentError,
)
from draftomen.semantic_relationship_records import QualificationKind

_SET_CODE = "hob"
_RUN_ID = "work-fixture-condition-0000000000000000000000000000000000000000000000"
_COMPLETED_AT = "2026-09-15T00:00:00Z"
_CREATED_AT = "2026-09-16T00:00:00Z"
_STORIED_REMINDER = (
    "Storied (If you control three or more artifacts, legendaries, and/or Sagas, "
    "you have an enduring story for the rest of the game.)"
)

# Frozen Oracle text, verbatim from the paid run's card database.
_DANCING_TEXT = (
    "Whenever you cast a creature spell, put X +1/+1 counters on target creature you control, "
    "where X is that spell's mana value.\n"
    "Landfall — Whenever a land you control enters, create a 2/2 green Bear creature token."
)
_SILVAN_TEXT = (
    "When this creature enters, draw a card, then discard a card. If you discard a land card this "
    "way, put it from your graveyard onto the battlefield tapped.\n"
    "Landfall — Whenever a land you control enters, you may pay {1}{G}{U}. If you do, return "
    "this card from your graveyard to your hand."
)
_COMPANY_TEXT = (
    "As long as you control another Elf, you may play an additional land on each of your turns.\n"
    "Landfall — Whenever a land you control enters, put two +1/+1 counters on target creature "
    "you control. It gains vigilance until end of turn."
)
_VALLEY_TEXT = (
    "(As this Saga enters and after your draw step, add a lore counter. Sacrifice after IV.)\n"
    "I — Search your library for a basic land card, reveal it, put it into your hand, then "
    "shuffle.\n"
    'II — This Saga gains "Landfall — Whenever a land you control enters, create a 1/1 green '
    'Elf creature token."\n'
    "III, IV — Elves you control get +1/+0 and gain vigilance until end of turn."
)
_RAFTER_TEXT = (
    "Landfall — Whenever a land you control enters, choose one —\n"
    "• Tap target creature an opponent controls.\n"
    "• Untap target creature you control."
)
_SCROUNGER_TEXT = (
    "Ferocious — Whenever this creature attacks while you control a creature with power 4 or "
    "greater, put a +1/+1 counter on each creature you control."
)
_RABBIT_TEXT = (
    "Ferocious — At the beginning of combat on your turn, if you control a creature with power "
    "4 or greater, put a +1/+1 counter on this creature."
)
_CHIEF_WARG_TEXT = (
    "Menace (This creature can't be blocked except by two or more creatures.)\n"
    "Ferocious — Whenever you attack while you control a creature with power 4 or greater, you "
    "draw a card and lose 1 life."
)
_MISTY_TEXT = (
    "(As this Saga enters and after your draw step, add a lore counter. Sacrifice after IV.)\n"
    "I, II, III, IV — Create a Treasure token. Then if you control four or more Treasures, "
    "sacrifice this Saga. If you do, create a 6/6 red Dragon creature token with flying. "
    '(A Treasure token is an artifact with "{T}, Sacrifice this token: Add one mana of any color.")'
)
_MISTY_LINE = (
    "I, II, III, IV — Create a Treasure token. Then if you control four or more Treasures, "
    "sacrifice this Saga. If you do, create a 6/6 red Dragon creature token with flying. "
    '(A Treasure token is an artifact with "{T}, Sacrifice this token: Add one mana of any color.")'
)
_FILI_TEXT = (
    f"{_STORIED_REMINDER}\n"
    "As long as you have an enduring story, creatures you control get +1/+1.\n"
    "Whenever Fíli or another nontoken Dwarf you control enters, create a 2/2 red Dwarf creature "
    "token."
)
_BOMBUR_TEXT = (
    f"{_STORIED_REMINDER}\n"
    "Bombur doesn't untap during your untap step unless you have an enduring story."
)
_TIDINGS_TEXT = (
    "Amass Goblins 1. If this spell was cast from a graveyard, amass Goblins 3 instead. "
    "(To amass Goblins X, put X +1/+1 counters on an Army you control. It's also a Goblin. "
    "If you don't control an Army, create a 0/0 black Goblin Army creature token first.)\n"
    "Flashback {3}{R} (You may cast this card from your graveyard for its flashback cost. "
    "Then exile it.)"
)
_TREASURE_DEFINITION = (
    '(A Treasure token is an artifact with "{T}, Sacrifice this token: Add one mana of any '
    'color.")'
)
_THRANDUIL_TEXT = (
    "Other Elves you control get +1/+1.\n"
    "Landfall — Whenever a land you control enters, create a 1/1 green Elf creature token."
)
_SILVAN_RALLY_TEXT = (
    "Mill four cards, then put up to two land cards from among them into your hand. "
    "(Then exile this card. You may cast the creature later from exile.)"
)
_BOMBUR_CLAUSE = (
    "Bombur doesn't untap during your untap step unless you have an enduring story."
)
_FILI_ANTHEM = "As long as you have an enduring story, creatures you control get +1/+1."
_FILI_TRIGGER = (
    "Whenever Fíli or another nontoken Dwarf you control enters, create a 2/2 red Dwarf creature "
    "token."
)
_SILVAN_ENTRY = (
    "If you discard a land card this way, put it from your graveyard onto the battlefield tapped."
)
_SILVAN_PAYOFF = (
    "Landfall — Whenever a land you control enters, you may pay {1}{G}{U}. If you do, return "
    "this card from your graveyard to your hand."
)
_DANCING_PAYOFF = (
    "Landfall — Whenever a land you control enters, create a 2/2 green Bear creature token."
)
_RAFTER_PAYOFF = (
    "Landfall — Whenever a land you control enters, choose one —\n"
    "• Tap target creature an opponent controls.\n"
    "• Untap target creature you control."
)
_SCROUNGER_PAYOFF = (
    "Ferocious — Whenever this creature attacks while you control a creature with power 4 or "
    "greater, put a +1/+1 counter on each creature you control."
)
_MISTY_DRAGON = "create a 6/6 red Dragon creature token"
_SCROUNGER_ATTACK = "Whenever this creature attacks"
_SCROUNGER_THRESHOLD = "you control a creature with power 4 or greater"
_SILVAN_ENTRY_FACT = "work-fixture:103543-recursion-etb"
_SILVAN_PAYOFF_FACT = "work-fixture:103543-recursion-landfall"

_ACCEPTED = FindingReview(status=FindingStatus.ACCEPTED, reason=None)
_UNCERTAIN = FindingReview(status=FindingStatus.UNCERTAIN, reason="fixture uncertainty")
_CARD_TYPES = frozenset(
    {
        "Artifact",
        "Battle",
        "Creature",
        "Enchantment",
        "Instant",
        "Kindred",
        "Land",
        "Planeswalker",
        "Sorcery",
        "Tribal",
    }
)


def _printed_types(*, type_line: str | None) -> tuple[str, ...]:
    """Return the card types one printed type line declares."""
    if type_line is None:
        return ()
    head = type_line.split("—")[0]
    return tuple(word for word in head.split() if word in _CARD_TYPES)


def _card(
    card_id: int,
    name: str,
    *,
    type_line: str | None = None,
    oracle_text: str | None = None,
    power: str | None = None,
    faces: tuple[CardFace, ...] = (),
) -> CardInfo:
    """Build one fixture card whose printed fields are its own frozen source."""
    return CardInfo(
        grp_id=card_id,
        name=name,
        colors=(),
        mana_value=None,
        rarity="common",
        types=_printed_types(type_line=type_line),
        oracle_text=oracle_text,
        type_line=type_line,
        power=power,
        faces=faces,
        set_code=_SET_CODE,
        collector_number=str(card_id),
        oracle_id=f"fixture-oracle-{card_id}",
        source_provenance=("fixture",),
    )


def _dancing() -> CardInfo:
    return _card(
        103503,
        "Dancing from Dark to Dawn",
        type_line="Enchantment",
        oracle_text=_DANCING_TEXT,
    )


def _silvan() -> CardInfo:
    return _card(
        103543,
        "Silvan Reveler",
        type_line="Creature — Elf Citizen",
        oracle_text=_SILVAN_TEXT,
        power="3",
    )


def _company() -> CardInfo:
    return _card(
        103549,
        "Thranduil's Company",
        type_line="Creature — Elf Soldier",
        oracle_text=_COMPANY_TEXT,
        power="3",
    )


def _valley() -> CardInfo:
    return _card(
        103504,
        "Down in the Valley",
        type_line="Enchantment — Saga",
        oracle_text=_VALLEY_TEXT,
    )


def _rafter() -> CardInfo:
    return _card(
        103409,
        "Elven Raft-Steerer",
        type_line="Creature — Elf Pilot",
        oracle_text=_RAFTER_TEXT,
        power="3",
    )


def _scrounger() -> CardInfo:
    return _card(
        103521,
        "Wilderland Scrounger",
        type_line="Creature — Wolf",
        oracle_text=_SCROUNGER_TEXT,
        power="3",
    )


def _rabbit() -> CardInfo:
    return _card(
        103510,
        "Nasty Little Rabbit",
        type_line="Creature — Rabbit",
        oracle_text=_RABBIT_TEXT,
        power="1",
    )


def _chief_warg() -> CardInfo:
    return _card(
        103530,
        "The Chief Warg",
        type_line="Legendary Creature — Wolf",
        oracle_text=_CHIEF_WARG_TEXT,
        power="3",
    )


def _misty() -> CardInfo:
    return _card(
        103482,
        "The Misty Mountains Cold",
        type_line="Enchantment — Saga",
        oracle_text=_MISTY_TEXT,
    )


def _fili() -> CardInfo:
    return _card(
        103382,
        "Fíli the Pathfinder",
        type_line="Legendary Creature — Dwarf Scout",
        oracle_text=_FILI_TEXT,
        power="2",
    )


def _bombur() -> CardInfo:
    return _card(
        103464,
        "Bombur, Gentle Dreamer",
        type_line="Legendary Creature — Dwarf Bard",
        oracle_text=_BOMBUR_TEXT,
        power="5",
    )


def _thranduil() -> CardInfo:
    return _card(
        103546,
        "Thranduil, Sindarin Liege // Silvan Rally",
        type_line="Legendary Creature — Elf Noble // Sorcery — Adventure",
        oracle_text=f"{_THRANDUIL_TEXT} // {_SILVAN_RALLY_TEXT}",
        power="2",
        faces=(
            CardFace(
                name="Thranduil, Sindarin Liege",
                type_line="Legendary Creature — Elf Noble",
                oracle_text=_THRANDUIL_TEXT,
                power="2",
                toughness="3",
            ),
            CardFace(
                name="Silvan Rally",
                type_line="Sorcery — Adventure",
                oracle_text=_SILVAN_RALLY_TEXT,
            ),
        ),
    )


def _run() -> ModelRun:
    return ModelRun(
        run_id=_RUN_ID,
        provider="fixture",
        model="fixture",
        reasoning=ReasoningConfig(enabled=None, effort=None, max_tokens=None, exclude=None),
        prompt_id="condition-fixture",
        prompt_sha256="0" * 64,
        response_schema_id="condition-fixture",
        response_schema_sha256="1" * 64,
        started_at=_COMPLETED_AT,
        completed_at=_COMPLETED_AT,
        input_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        cost_usd=None,
    )


def _artifact(*cards: CardInfo, facts: tuple[OracleFact, ...] = ()) -> SemanticEnrichmentArtifact:
    """Build one pending fixture artifact whose pins cover exactly these cards."""
    sources = EnrichmentSources(set_code=_SET_CODE, cards=tuple(cards), guides=())
    return SemanticEnrichmentArtifact(
        set_code=_SET_CODE,
        set_source_id="hob-condition-fixture",
        set_source_sha256=set_source_sha256(sources),
        created_at=_CREATED_AT,
        cards=tuple(
            CardSourcePin(
                card_id=card.grp_id,
                oracle_id=card.oracle_id,
                collector_number=card.collector_number,
                sha256=card_source_sha256(card),
            )
            for card in cards
        ),
        guides=(),
        runs=(_run(),),
        oracle_facts=facts,
        guide_claims=(),
        relationships=(),
        rejected_findings=(),
        review=ArtifactReview(state="pending", reviewer_id=None, reviewed_at=None),
        confirmed_relationship_ids=(),
        sources=sources,
    )


def _compile_with(
    artifact: SemanticEnrichmentArtifact,
    *cards: CardInfo,
) -> ConditionMap | None:
    """Compile one artifact against exactly these card sources."""
    return compile_condition_map(
        artifact=artifact,
        card_database=CardDatabase(cards={card.grp_id: card for card in cards}),
    )


def _compile(*cards: CardInfo, facts: tuple[OracleFact, ...] = ()) -> ConditionMap | None:
    """Compile a pending fixture artifact built from exactly these cards."""
    return _compile_with(_artifact(*cards, facts=facts), *cards)


def _nodes(
    condition_map: ConditionMap,
    card_id: int,
    kind: str,
) -> tuple[ConditionCapability, ...]:
    """Return every node one card states under one capability kind."""
    return tuple(
        capability
        for capability in condition_map.capabilities
        if capability.source_card_id == card_id and capability.kind == kind
    )


def _node(condition_map: ConditionMap, card_id: int, kind: str) -> ConditionCapability:
    """Return the one node one card states under one capability kind."""
    nodes = _nodes(condition_map, card_id, kind)
    assert len(nodes) == 1, f"expected one {kind} node for {card_id}, found {len(nodes)}"
    return nodes[0]


def _payoff(condition_map: ConditionMap, card_id: int, family: str) -> ConditionCapability:
    """Return the one payoff node one card states for one family."""
    nodes = tuple(
        capability
        for capability in condition_map.capabilities
        if capability.source_card_id == card_id
        and capability.role == "payoff"
        and capability.family == family
    )
    assert len(nodes) == 1, f"expected one {family} payoff for {card_id}, found {len(nodes)}"
    return nodes[0]


def _supports(
    condition_map: ConditionMap,
    *,
    enabler: ConditionCapability,
    payoff_card_id: int,
) -> set[str]:
    """Return the support kinds one enabler supplies toward one card's payoffs."""
    by_id = {capability.capability_id: capability for capability in condition_map.capabilities}
    return {
        edge.support
        for edge in condition_map.interactions
        if edge.enabler_id == enabler.capability_id
        and by_id[edge.payoff_id].source_card_id == payoff_card_id
    }


def _selectors(capability: ConditionCapability, *, field: str = "oracle_text") -> tuple[str, ...]:
    """Return the exact selectors one capability retains in one named field."""
    return tuple(item.selector for item in capability.evidence if item.field == field)


def test_compile_condition_map_gates_its_pinned_inputs() -> None:
    """A wrong record, a missing card, and a changed source all fail the compiler."""
    with pytest.raises(SemanticEnrichmentError):
        compile_condition_map(artifact=object(), card_database=CardDatabase(cards={}))
    with pytest.raises(SemanticEnrichmentError):
        compile_condition_map(artifact=_artifact(_dancing()), card_database=object())
    with pytest.raises(SemanticEnrichmentError):
        _compile_with(_artifact(_dancing()))

    changed = _card(
        103503,
        "Dancing from Dark to Dawn",
        type_line="Enchantment",
        oracle_text=f"{_DANCING_TEXT}\nNothing else.",
    )
    with pytest.raises(SemanticEnrichmentError):
        _compile_with(_artifact(_dancing()), changed)


def test_landfall_helpers_gate_on_printed_land_entries() -> None:
    """A land entry enables an own landfall payoff; hand moves and opponent entries do not."""
    to_hand = _card(
        990101,
        "Fixture Hand Recursion",
        type_line="Sorcery",
        oracle_text=(
            "Search your library for a basic land card, reveal it, put it into your hand, "
            "then shuffle."
        ),
    )
    opponent_entry = _card(
        990102,
        "Fixture Opponent Ramp",
        type_line="Sorcery",
        oracle_text=(
            "Target opponent searches their library for a basic land card, puts it onto the "
            "battlefield tapped, then shuffles."
        ),
    )
    fixture_land = _card(
        990103,
        "Fixture Land",
        type_line="Basic Land — Forest",
        oracle_text="({T}: Add {G}.)",
    )
    ramp = _card(
        990104,
        "Fixture Ramp",
        type_line="Sorcery",
        oracle_text=(
            "Search your library for a basic land card, put it onto the battlefield tapped, "
            "then shuffle."
        ),
    )
    condition_map = _compile(
        _dancing(),
        _silvan(),
        _company(),
        to_hand,
        opponent_entry,
        fixture_land,
        ramp,
    )
    assert condition_map is not None

    payoff = _payoff(condition_map, 103503, "landfall")
    assert _selectors(payoff) == (_DANCING_PAYOFF,)
    assert payoff.quantity is None
    assert payoff.controller == "you"

    reveler_entry = _node(condition_map, 103543, "land_entry")
    assert _selectors(reveler_entry) == (_SILVAN_ENTRY,)
    assert _supports(condition_map, enabler=reveler_entry, payoff_card_id=103503) == {"can_enable"}

    assert _nodes(condition_map, 990101, "land_entry") == ()
    opponent_node = _node(condition_map, 990102, "land_entry")
    assert opponent_node.controller == "opponent"
    assert _supports(condition_map, enabler=opponent_node, payoff_card_id=103503) == set()

    land_node = _node(condition_map, 990103, "land_card")
    assert land_node.quantity == CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    assert land_node.controller == "you"
    assert _supports(condition_map, enabler=land_node, payoff_card_id=103503) == {"can_enable"}

    ramp_node = _node(condition_map, 990104, "land_entry")
    assert ramp_node.quantity == CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    assert _supports(condition_map, enabler=ramp_node, payoff_card_id=103503) == {"can_enable"}


def test_additional_land_permission_is_partial_and_keeps_its_restrictions() -> None:
    """Thranduil's Company's permission contributes, retaining the Elf and turn restrictions."""
    condition_map = _compile(_dancing(), _company())
    assert condition_map is not None
    permission = _node(condition_map, 103549, "additional_land_play")
    assert permission.quantity == CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    assert permission.controller == "you"
    assert _selectors(permission) == (
        "As long as you control another Elf, you may play an additional land on each of your "
        "turns.",
    )
    assert _supports(condition_map, enabler=permission, payoff_card_id=103503) == {"contributes"}
    assert _supports(condition_map, enabler=permission, payoff_card_id=103549) == {"contributes"}


def test_landfall_payoffs_keep_their_modal_and_granted_windows() -> None:
    """A modal ability and a chapter-granted ability retain their complete printed windows."""
    condition_map = _compile(_rafter(), _valley())
    assert condition_map is not None
    rafter = _payoff(condition_map, 103409, "landfall")
    assert _selectors(rafter) == ("choose one —", _RAFTER_PAYOFF)

    valley = _payoff(condition_map, 103504, "landfall")
    assert _selectors(valley) == (
        'II — This Saga gains "Landfall — Whenever a land you control enters, create a 1/1 '
        'green Elf creature token."',
    )


def test_landfall_payoff_cites_its_optional_payment() -> None:
    """Silvan Reveler's landfall payoff keeps the optional {1}{G}{U} payment as evidence."""
    condition_map = _compile(_silvan(), _dancing())
    assert condition_map is not None
    payoff = _payoff(condition_map, 103543, "landfall")
    assert _selectors(payoff) == (
        _SILVAN_PAYOFF,
        "Landfall — Whenever a land you control enters, you may pay {1}{G}{U}.",
    )
    assert {item.kind for item in payoff.evidence} == {
        QualificationKind.CONDITION,
        QualificationKind.COST,
    }


def test_power_three_does_not_enable_ferocious_while_power_four_does() -> None:
    """Only fixed printed power that reaches the threshold supports the ferocious payoff."""
    three = _card(990201, "Fixture Power Three", type_line="Creature — Bear", power="3")
    four = _card(990202, "Fixture Power Four", type_line="Creature — Bear", power="4")
    variable = _card(
        990203,
        "Fixture Star Power",
        type_line="Creature — Bear",
        oracle_text="Fixture Star Power's power is equal to the number of creatures you control.",
        power="*",
    )
    vehicle = _card(990204, "Fixture Vehicle", type_line="Artifact — Vehicle", power="4")

    condition_map = _compile(_scrounger(), three, four, variable, vehicle)
    assert condition_map is not None
    payoff = _payoff(condition_map, 103521, "ferocious")
    assert payoff.quantity == CapabilityQuantity(value=4, relation=QuantityRelation.AT_LEAST)
    assert _selectors(payoff) == (_SCROUNGER_PAYOFF, _SCROUNGER_THRESHOLD, _SCROUNGER_ATTACK)

    three_node = _node(condition_map, 990201, "creature_power")
    four_node = _node(condition_map, 990202, "creature_power")
    assert _selectors(three_node, field="power") == ("3",)
    assert _supports(condition_map, enabler=three_node, payoff_card_id=103521) == set()
    assert _supports(condition_map, enabler=four_node, payoff_card_id=103521) == {"can_enable"}
    assert _nodes(condition_map, 990203, "creature_power") == ()
    assert _nodes(condition_map, 990204, "creature_power") == ()


def test_a_source_only_power_change_moves_the_condition_fingerprint_only() -> None:
    """Power changes move the condition source fingerprint but not the historical card hash."""
    three = _card(990301, "Fixture Bear", type_line="Creature — Bear", power="3")
    four = _card(990301, "Fixture Bear", type_line="Creature — Bear", power="4")
    assert card_source_sha256(three) == card_source_sha256(four)

    low = _compile(_scrounger(), three)
    high = _compile(_scrounger(), four)
    assert low is not None and high is not None
    assert low.to_json() != high.to_json()
    low_source = next(source for source in low.sources if source.card_id == 990301)
    high_source = next(source for source in high.sources if source.card_id == 990301)
    assert low_source.source_sha256 != high_source.source_sha256
    assert low_source.card_source_sha256 == high_source.card_source_sha256
    assert low_source.power == "3" and high_source.power == "4"
    assert _supports(low, enabler=_node(low, 990301, "creature_power"), payoff_card_id=103521) == set()
    assert _supports(high, enabler=_node(high, 990301, "creature_power"), payoff_card_id=103521) == {
        "can_enable"
    }


def test_created_dragon_enables_ferocious_with_its_conditions() -> None:
    """The conditional 6/6 Dragon enables ferocious; Treasures and a naked Army do not."""
    treasure_only = _card(
        990401,
        "Fixture Treasure Maker",
        type_line="Creature — Goblin",
        power="1",
        oracle_text=f"When this creature enters, create a Treasure token. {_TREASURE_DEFINITION}",
    )
    naked_army = _card(
        990402,
        "Fixture Army Maker",
        type_line="Sorcery",
        oracle_text="Create a 0/0 black Goblin Army creature token.",
    )
    condition_map = _compile(_scrounger(), _misty(), treasure_only, naked_army)
    assert condition_map is not None

    dragon = _node(condition_map, 103482, "created_creature_power")
    assert dragon.quantity == CapabilityQuantity(value=6, relation=QuantityRelation.EXACTLY)
    assert _selectors(dragon) == (_MISTY_LINE, _MISTY_DRAGON)
    assert "four or more Treasures" in _selectors(dragon)[0]
    assert "sacrifice this Saga" in _selectors(dragon)[0]
    assert "with flying" in _selectors(dragon)[0]
    assert _supports(condition_map, enabler=dragon, payoff_card_id=103521) == {"can_enable"}

    assert _nodes(condition_map, 990402, "created_creature_power") == ()
    assert _nodes(condition_map, 990401, "created_creature_power") == ()
    treasure = _node(condition_map, 990401, "created_qualifying_permanents")
    assert treasure.quantity == CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    assert _supports(condition_map, enabler=treasure, payoff_card_id=103521) == set()


def test_amass_is_partial_unless_a_single_instruction_reaches_the_threshold() -> None:
    """Amass 1 and X are partial, amass 4 enables, and exclusive alternatives never sum."""
    amass_one = _card(990501, "Fixture Amass One", type_line="Sorcery", oracle_text="Amass Goblins 1.")
    amass_four = _card(
        990502, "Fixture Amass Four", type_line="Sorcery", oracle_text="Amass Goblins 4."
    )
    tidings = _card(103494, "Tidings of War", type_line="Sorcery", oracle_text=_TIDINGS_TEXT)
    condition_map = _compile(_scrounger(), amass_one, amass_four, tidings)
    assert condition_map is not None

    one = _node(condition_map, 990501, "amass_growth")
    assert one.quantity == CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    assert one.controller == "you"
    assert _supports(condition_map, enabler=one, payoff_card_id=103521) == {"contributes"}

    four = _node(condition_map, 990502, "amass_growth")
    assert four.quantity == CapabilityQuantity(value=4, relation=QuantityRelation.EXACTLY)
    assert _supports(condition_map, enabler=four, payoff_card_id=103521) == {"can_enable"}

    alternatives = _nodes(condition_map, 103494, "amass_growth")
    assert {node.quantity for node in alternatives} == {
        CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
        CapabilityQuantity(value=3, relation=QuantityRelation.EXACTLY),
    }
    for node in alternatives:
        assert _supports(condition_map, enabler=node, payoff_card_id=103521) == {"contributes"}
        assert "If this spell was cast from a graveyard" in _selectors(node)[0]


def test_storied_payoff_keeps_its_threshold_and_persistence() -> None:
    """Three qualifying permanents are required, persist, and each face contributes one node."""
    legendary_artifact = _card(
        990601,
        "Fixture Legendary Artifact",
        type_line="Legendary Artifact — Equipment",
        oracle_text="Equipped creature gets +1/+0.",
    )
    forest = _card(
        990602,
        "Fixture Forest",
        type_line="Basic Land — Forest",
        oracle_text="({T}: Add {G}.)",
    )
    ordinary = _card(990603, "Fixture Wolf", type_line="Creature — Wolf", power="2")
    condition_map = _compile(_fili(), _misty(), _bombur(), legendary_artifact, forest, ordinary)
    assert condition_map is not None

    payoff = _payoff(condition_map, 103382, "storied")
    assert payoff.quantity == CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST)
    assert _selectors(payoff) == (_FILI_ANTHEM, _STORIED_REMINDER)
    assert "for the rest of the game" in _selectors(payoff)[1]
    assert "three or more artifacts, legendaries, and/or Sagas" in _selectors(payoff)[1]
    assert _FILI_TRIGGER not in _selectors(payoff)

    bombur = _payoff(condition_map, 103464, "storied")
    assert _selectors(bombur) == (_BOMBUR_CLAUSE, _STORIED_REMINDER)

    saga = _node(condition_map, 103482, "qualifying_permanent")
    assert _selectors(saga, field="type_line") == ("Saga",)
    assert _supports(condition_map, enabler=saga, payoff_card_id=103382) == {"contributes"}

    legend = _node(condition_map, 990601, "qualifying_permanent")
    assert set(_selectors(legend, field="type_line")) == {"Legendary", "Artifact"}
    assert _supports(condition_map, enabler=legend, payoff_card_id=103382) == {"contributes"}

    assert _nodes(condition_map, 990602, "qualifying_permanent") == ()
    assert _nodes(condition_map, 990603, "qualifying_permanent") == ()
    treasure = _node(condition_map, 103482, "created_qualifying_permanents")
    assert treasure.quantity == CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    assert _supports(condition_map, enabler=treasure, payoff_card_id=103382) == {"contributes"}
    assert {
        edge.support
        for edge in condition_map.interactions
        if edge.payoff_id == payoff.capability_id
    } == {"contributes"}


def test_storied_definitions_must_bind_to_the_printed_reminder() -> None:
    """An enduring-story clause without the reminder is incomplete, not a payoff."""
    incomplete = _card(
        990701,
        "Fixture Unbound Story",
        type_line="Legendary Creature — Dwarf",
        power="2",
        oracle_text=(
            "Storied\nAs long as you have an enduring story, creatures you control get +1/+1."
        ),
    )
    with pytest.raises(SemanticEnrichmentError) as error:
        _compile(incomplete)
    assert "990701" in str(error.value)

    text = incomplete.oracle_text or ""
    assert (
        condition_statements(
            oracle_text=text,
            quote="As long as you have an enduring story, creatures you control get +1/+1.",
        )
        is None
    )


def test_condition_statements_never_invents_text_and_keeps_faces_apart() -> None:
    """Every statement is exact printed text; unrelated abilities and faces stay unrelated."""
    assert condition_statements(oracle_text=_DANCING_TEXT, quote=_DANCING_PAYOFF) == (
        (QualificationKind.CONDITION, _DANCING_PAYOFF),
    )
    assert condition_statements(oracle_text=_SILVAN_TEXT, quote=_SILVAN_ENTRY) == ()
    assert condition_statements(oracle_text=_FILI_TEXT, quote=_FILI_TRIGGER) == ()
    assert (
        condition_statements(
            oracle_text=_SILVAN_RALLY_TEXT,
            quote="Mill four cards, then put up to two land cards from among them into your hand.",
        )
        == ()
    )

    thranduil_line = _THRANDUIL_TEXT.splitlines()[1]
    assert condition_statements(oracle_text=_THRANDUIL_TEXT, quote=thranduil_line) == (
        (QualificationKind.CONDITION, thranduil_line),
    )

    ferocious = condition_statements(oracle_text=_SCROUNGER_TEXT, quote=_SCROUNGER_PAYOFF)
    assert ferocious == (
        (QualificationKind.CONDITION, _SCROUNGER_PAYOFF),
        (QualificationKind.TIMING, _SCROUNGER_ATTACK),
        (QualificationKind.QUANTITY, _SCROUNGER_THRESHOLD),
    )
    for _, selector in ferocious or ():
        assert _SCROUNGER_TEXT.count(selector) == 1

    storied = condition_statements(oracle_text=_FILI_TEXT, quote=_FILI_ANTHEM)
    assert storied == (
        (QualificationKind.CONDITION, _STORIED_REMINDER),
        (QualificationKind.CONDITION, _FILI_ANTHEM),
    )
    assert condition_statements(oracle_text=_FILI_TEXT, quote="Storied") == storied
    assert condition_statements(oracle_text=None, quote="Storied") == ()
    assert condition_statements(oracle_text=_FILI_TEXT, quote="not printed here") == ()


def test_unbound_family_keywords_fail_with_card_context() -> None:
    """A printed keyword whose clause or predicate is missing is an error, not an exclusion."""
    landfall = _card(
        990801,
        "Fixture Unbound Landfall",
        type_line="Creature — Elf",
        power="2",
        oracle_text="Landfall — Whenever you attack, draw a card.",
    )
    ferocious = _card(
        990802,
        "Fixture Unbound Ferocious",
        type_line="Creature — Wolf",
        power="2",
        oracle_text="Ferocious — Whenever this creature attacks, it gets +1/+0 until end of turn.",
    )
    for card in (landfall, ferocious):
        with pytest.raises(SemanticEnrichmentError) as error:
            _compile(card)
        assert str(card.grp_id) in str(error.value)
        quote = (card.oracle_text or "").splitlines()[0]
        assert condition_statements(oracle_text=card.oracle_text or "", quote=quote) is None


def test_printed_targets_keep_their_own_faces_and_payoff_windows() -> None:
    """Every compiled printed target keeps a payoff node on its own face and printed window."""
    condition_map = _compile(
        _rafter(),
        _scrounger(),
        _rabbit(),
        _chief_warg(),
        _dancing(),
        _silvan(),
        _valley(),
        _company(),
        _misty(),
        _fili(),
        _bombur(),
        _thranduil(),
    )
    assert condition_map is not None

    assert {
        capability.source_card_id
        for capability in condition_map.capabilities
        if capability.family == "landfall" and capability.role == "payoff"
    } == {103409, 103503, 103504, 103543, 103546, 103549}
    assert {
        capability.source_card_id
        for capability in condition_map.capabilities
        if capability.family == "ferocious" and capability.role == "payoff"
    } == {103510, 103521, 103530}
    assert {
        capability.source_card_id
        for capability in condition_map.capabilities
        if capability.family == "storied" and capability.role == "payoff"
    } == {103382, 103464}

    assert _payoff(condition_map, 103546, "landfall").source_face_index == 0
    assert not [
        capability
        for capability in condition_map.capabilities
        if capability.source_card_id == 103546 and capability.source_face_index == 1
    ]
    assert _payoff(condition_map, 103510, "ferocious").quantity == CapabilityQuantity(
        value=4, relation=QuantityRelation.AT_LEAST
    )
    assert _selectors(_payoff(condition_map, 103530, "ferocious"))[1] == _SCROUNGER_THRESHOLD
    assert _selectors(_payoff(condition_map, 103530, "ferocious"))[2] == "Whenever you attack"

    capability_ids = {capability.capability_id for capability in condition_map.capabilities}
    assert len(capability_ids) == len(condition_map.capabilities)
    assert all(
        capability_id.startswith(CONDITION_CAPABILITY_ID_PREFIX)
        for capability_id in capability_ids
    )


def test_accepted_fact_provenance_binds_only_to_its_own_ability() -> None:
    """Only accepted facts overlapping a node's own window supply its provenance."""
    entry_fact = OracleFact(
        finding_id=_SILVAN_ENTRY_FACT,
        card_id=103543,
        kind="ramp",
        claim="fixture land entry",
        evidence=(OracleEvidence(card_id=103543, face_index=None, quote=_SILVAN_ENTRY),),
        review=_ACCEPTED,
        run_id=_RUN_ID,
    )
    uncertain_fact = OracleFact(
        finding_id="work-fixture:103543-uncertain-payoff",
        card_id=103543,
        kind="landfall_payoff",
        claim="fixture landfall payoff",
        evidence=(OracleEvidence(card_id=103543, face_index=None, quote=_SILVAN_PAYOFF),),
        review=_UNCERTAIN,
        run_id=_RUN_ID,
    )
    payoff_fact = OracleFact(
        finding_id=_SILVAN_PAYOFF_FACT,
        card_id=103543,
        kind="recursion",
        claim="fixture landfall recursion",
        evidence=(OracleEvidence(card_id=103543, face_index=None, quote=_SILVAN_PAYOFF),),
        review=_ACCEPTED,
        run_id=_RUN_ID,
    )
    condition_map = _compile(
        _silvan(),
        _dancing(),
        facts=(entry_fact, uncertain_fact, payoff_fact),
    )
    assert condition_map is not None

    entry = _node(condition_map, 103543, "land_entry")
    assert entry.source_finding_ids == (_SILVAN_ENTRY_FACT,)
    payoff = _payoff(condition_map, 103543, "landfall")
    assert payoff.source_finding_ids == (_SILVAN_PAYOFF_FACT,)
    assert uncertain_fact.finding_id not in entry.source_finding_ids
    assert uncertain_fact.finding_id not in payoff.source_finding_ids
    assert _payoff(condition_map, 103503, "landfall").source_finding_ids == ()
