"""Regressions for deterministic source-bound projections of confirmed local relationships.

The compiler is exercised against a compact confirmation extracted from the zero-cost saved HOB
run whose digest is ``edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84``: the
reviewed real pairs, their strict v2 capability facts, the pinned cards that bind them and the
real model runs behind them.  Besides the three original mill/recursion pairs it carries the
recovered production families - recruit, the reminder-free Adventure amass face, Azog's
controlled amass, the Misty Mountains Cold chapter, the two-line chosen-type anthem, the subtype
matrix around Goblin and Elf requirements, Beorn's same-face type conversion and the recovered
sacrifice outlets.  The same confirmation carries fixture-grammar pairs for the closed guards
this module defends - a trailing condition inside one action, a condition on the next
instruction only, a retained token subtype that states one of the two explicit words, and a
chosen-type payoff whose paragraph the face prints twice.  The fixture carries its own frozen
sources, so these tests never read a home directory and never skip when an operator's files are
absent.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_relationship_projection import (
    RelationshipConversion,
    RelationshipConversionOutcome,
    RelationshipProjectionCompilation,
    _decode_capability,
    compile_confirmed_relationship_projections,
    compile_token_replacement_relationships,
)
from draftomen.semantic_enrichment import EnrichmentSources, SemanticEnrichmentArtifact
from draftomen.semantic_enrichment_records import OracleEvidence, OracleFact
from draftomen.semantic_relationship_records import (
    QualificationKind,
    CardRelationship,
    PrerequisiteProjectionError,
    RelationshipQualification,
    validate_relationship_pins,
    validate_relationship_sources,
)

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/hob-relationship-projection-confirmation.json").read_text(
        encoding="utf-8"
    )
)
# The exact local subjects of the reviewed real pairs the saved pair matcher decided.
_MILL = (
    "mill-graveyard-payoff:103546:103546-f1-self-mill:"
    "103422:103422-0-threshold-graveyard-payoff"
)
_RECURSION = (
    "recursion-graveyard-payoff:103442:103442-return-creature-card:"
    "103422:103422-0-threshold-graveyard-payoff"
)
_AMASS = (
    "token-go-wide-payoff:103442:103442-amass-goblin-army-token:103381:103381-go-wide-power"
)
# The fixture-grammar subjects of the closed guards.
_TRAILING_MILL = (
    "mill-graveyard-payoff:990001:990001-fixture-trailing-mill:"
    "103422:103422-0-threshold-graveyard-payoff"
)
_NEXT_ACTION_MILL = (
    "mill-graveyard-payoff:990002:990002-fixture-next-action-mill:"
    "103422:103422-0-threshold-graveyard-payoff"
)
_ARMY_PAIR = (
    "token-go-wide-payoff:990003:990003-fixture-army-maker:"
    "990010:990010-fixture-wide-payoff"
)
_MILL_CAPABILITY = "103546-f1-self-mill"
_THRESHOLD_CAPABILITY = "103422-0-threshold-graveyard-payoff"
_ARMY_CAPABILITY = "990003-fixture-army-maker"
_ARMY_INSTRUCTION = "Create a 0/0 black Goblin Army creature token."
_LOCAL_RUN = "local-c7a4f08030a1bfb7"
_SOURCE_RUN = "work-50c6eb907e6820331f23105b216a8fad1807a8c3e308f147b386801762c09a07"
# The real threshold line, cited with retained kinds and spans no typed clause can bind: the
# statement itself is never rewritten, only the retained kind and the cited span change.
_THRESHOLD_STATEMENT = (
    "This creature gets +1/+1 as long as there are seven or more cards in your graveyard."
)
_THRESHOLD_COUNT = "seven or more cards in your graveyard."
_UNTYPABLE_PREREQUISITES = (
    ("trigger", _THRESHOLD_STATEMENT, "timing"),
    ("cost", _THRESHOLD_STATEMENT, "cost"),
    ("condition", _THRESHOLD_COUNT, "condition"),
    ("threshold", _THRESHOLD_COUNT, "quantity"),
)
# The recovered family rows of the expanded confirmation, keyed by their stored finding subjects.
_RECRUIT = "token-go-wide-payoff:103381:103381-recruit-token:990010:990010-fixture-wide-payoff"
_ADVENTURE_AMASS = (
    "token-go-wide-payoff:103449:103449-f1-amass-token:990010:990010-fixture-wide-payoff"
)
_AZOG = "token-go-wide-payoff:103435:103435-token-maker-1:103381:103381-go-wide-power"
_MISTY = "token-go-wide-payoff:103482:103482-dragon-token-1:103381:103381-go-wide-power"
_CHOSEN_TYPE = (
    "token-go-wide-payoff:103442:103442-amass-goblin-army-token:103397:103397-f0-go-wide-payoff"
)
_REPEATED_PAYOFF = (
    "token-go-wide-payoff:103442:103442-amass-goblin-army-token:"
    "990020:990020-fixture-repeated-payoff"
)
_GOBLIN_ARMY_OUTLET = (
    "token-sacrifice-outlet:103442:103442-amass-goblin-army-token:103529:103529-sacrifice-outlet"
)
_LOOKOUT_OUTLET = (
    "token-sacrifice-outlet:103386:103386-dies-create-token-token-maker:"
    "103529:103529-sacrifice-outlet"
)
_COMPANY_ANTHEM = (
    "token-go-wide-payoff:103526:103526-recruit-attack-token-maker:"
    "103546:103546-f0-go-wide-payoff"
)
_BEORN = "token-go-wide-payoff:103381:103381-recruit-token:103499:103499-bear-buff-go-wide"
_SACRIFICE_COST = (
    "token-sacrifice-outlet:103442:103442-amass-goblin-army-token:103460:103460-sacrifice-cost"
)
_SACRIFICE_OUTLET = (
    "token-sacrifice-outlet:103442:103442-amass-goblin-army-token:103491:103491-sacrifice-outlet"
)
_RECRUIT_CAPABILITY = "103381-recruit-token"
_ADVENTURE_AMASS_CAPABILITY = "103449-f1-amass-token"
_AZOG_CAPABILITY = "103435-token-maker-1"
_MISTY_CAPABILITY = "103482-dragon-token-1"
_CHOSEN_TYPE_CAPABILITY = "103397-f0-go-wide-payoff"
_LOOKOUT_CAPABILITY = "103386-dies-create-token-token-maker"
_COMPANY_CAPABILITY = "103526-recruit-attack-token-maker"
_OUTLET_CAPABILITY = "103529-sacrifice-outlet"
_BEORN_CAPABILITY = "103499-bear-buff-go-wide"
_SACRIFICE_COST_CAPABILITY = "103460-sacrifice-cost"
_SACRIFICE_OUTLET_CAPABILITY = "103491-sacrifice-outlet"
# The frozen statements the recovered qualifications must retain, verbatim from the saved run.
_RECRUIT_INSTRUCTION = (
    "When this creature enters, recruit. (Draw a card, then discard a card. "
    "If you discarded a nonland card, create a 1/1 white Human Soldier creature token.)"
)
_RECRUIT_FRAME = "When this creature enters, recruit."
_RECRUIT_CONDITION = (
    "If you discarded a nonland card, create a 1/1 white Human Soldier creature token."
)
_ADVENTURE_AMASS_INSTRUCTION = "Amass Goblins 2."
_ADVENTURE_REMINDER = "(Then exile this card."
_ADVENTURE_SEQUENCE = (
    "(Then exile this card. You may cast the creature later from exile.)"
)
_MENACE_LINE = "Each creature you control with a +1/+1 counter on it has menace."
_WRONG_FACE_NAME = "Great Ugly-Looking Goblin"
_AZOG_MODE = "Its controller amasses Goblins X, where X is that creature's power."
_AZOG_PARTY = (
    "Its controller amasses Goblins X, where X is that creature's power. "
    "If you controlled that creature, draw a card."
)
_AZOG_CONDITION = "If they don't control an Army"
_AZOG_TIMING = "When Azog enters"
_AZOG_REMINDER = (
    "If they don't control an Army, they create a 0/0 black Goblin Army creature token first."
)
_MISTY_CHAPTER = (
    "I, II, III, IV — Create a Treasure token. Then if you control four or more Treasures, "
    "sacrifice this Saga. If you do, create a 6/6 red Dragon creature token with flying. "
    '(A Treasure token is an artifact with "{T}, Sacrifice this token: Add one mana of any color.")'
)
_MISTY_RANGE = "I, II, III, IV"
_MISTY_QUANTITY = "if you control four or more Treasures"
_MISTY_SEQUENCE = (
    "Then if you control four or more Treasures, sacrifice this Saga. If you do, create a "
    "6/6 red Dragon creature token with flying."
)
_CHOSEN_TYPE_CHOICE = "As this enchantment enters, choose a creature type."
_CHOSEN_TYPE_PAYOFF = "Creatures you control of the chosen type get +2/+2."
_CHOSEN_TYPE_PARAGRAPH = f"{_CHOSEN_TYPE_CHOICE}\n{_CHOSEN_TYPE_PAYOFF}"
_GOBLIN_OUTLET_QUOTE = "{T}, Sacrifice another Goblin: Add {B}{R}."
_GOBLIN_OUTLET_COST = "{T}, Sacrifice another Goblin"
_BEORN_PAYOFF = "Other Bears you control get +2/+2."
_BEORN_COMBAT_ABILITY = (
    "At the beginning of combat on your turn, put a trample counter on up to one target "
    "creature you control. It becomes a Bear in addition to its other types. Then if you "
    "control three or more Bears, draw two cards."
)
_BEORN_CONVERSION = "It becomes a Bear in addition to its other types."
_SACRIFICE_STATEMENT = (
    "As an additional cost to cast this spell, sacrifice an artifact or creature or pay {4}."
)
_SACRIFICE_CHOICE = "sacrifice an artifact or creature or pay {4}"
_OUTLET_COST = "Sacrifice another creature or artifact"
_OUTLET_RESTRICTION = "Activate only during your turn and only once each turn."
# The recovered condition-family rows of the expanded confirmation, keyed by finding subject.
_LANDFALL_BEAR = "token-go-wide-payoff:103503:103503-token-maker-1:103381:103381-go-wide-power"
_CHAPTER_LANDFALL = (
    "token-go-wide-payoff:103504:103504-landfall-1-token-maker:"
    "103546:103546-f0-go-wide-payoff"
)
_CHAPTER_BEAR = (
    "token-go-wide-payoff:103504:103504-landfall-1-token-maker:103499:103499-bear-buff-go-wide"
)
_THRANDUIL_LANDFALL = (
    "token-go-wide-payoff:103546:103546-f0-token-maker:103381:103381-go-wide-power"
)
_FILI_DWARVES = "token-sacrifice-outlet:103382:103382-dwarf-token-trigger:103460:103460-sacrifice-cost"
_WOLF_CREATION = (
    "token-go-wide-payoff:103451:103451-death-token-token-maker:103381:103381-go-wide-power"
)
_WOLF_ANTHEM = (
    "token-go-wide-payoff:103451:103451-death-token-token-maker:"
    "103546:103546-f0-go-wide-payoff"
)
_LOOKOUT_ANTHEM = (
    "token-go-wide-payoff:103386:103386-dies-create-token-token-maker:"
    "103546:103546-f0-go-wide-payoff"
)
_ELF_OUTLET = (
    "token-sacrifice-outlet:103504:103504-landfall-1-token-maker:103529:103529-sacrifice-outlet"
)
_VARIABLE_DWARVES = (
    "token-go-wide-payoff:103397:103397-f1-token-maker:103521:103521-go-wide-payoff-1"
)
_RECURSION_SCALING = (
    "recursion-graveyard-payoff:103546:103546-f1-recursion:103420:103420-graveyard-scaling"
)
_RECURSION_THRESHOLD = (
    "recursion-graveyard-payoff:103546:103546-f1-recursion:"
    "103422:103422-0-threshold-graveyard-payoff"
)
_STORIED_ANTHEM = (
    "token-go-wide-payoff:103482:103482-dragon-token-1:103382:103382-enduring-story-anthem"
)
_BARD_REPLACEMENT = (
    "token-go-wide-payoff:103524:103524-token-creation-replacement:"
    "103546:103546-f0-go-wide-payoff"
)
_BEAR_INSTRUCTION = (
    "Landfall — Whenever a land you control enters, create a 2/2 green Bear creature token."
)
_ELF_INSTRUCTION = (
    "Landfall — Whenever a land you control enters, create a 1/1 green Elf creature token."
)
_DWARF_INSTRUCTION = (
    "Whenever Fíli or another nontoken Dwarf you control enters, create a 2/2 red Dwarf creature "
    "token."
)
_WOLF_PARENT = "If a creature an opponent controls would die, exile it instead."
_WOLF_INSTRUCTION = "When you do, create a 2/2 green Wolf creature token."
_VARIABLE_INSTRUCTION = "Create X 2/2 red Dwarf creature tokens."
_MILL_RETURN_INSTRUCTION = (
    "Mill four cards, then put up to two land cards from among them into your hand."
)


def _cards() -> tuple[CardInfo, ...]:
    """Return the pinned HOB and fixture cards the confirmation was built with."""
    return tuple(CardInfo.from_json(entry) for entry in _FIXTURE["cards"])


def _sources() -> EnrichmentSources:
    """Return the frozen card sources of the compact confirmation."""
    return EnrichmentSources(set_code="hob", cards=_cards(), guides=())


def _confirmation() -> SemanticEnrichmentArtifact:
    """Decode the compact confirmation through the artifact's own source-validating loader."""
    return SemanticEnrichmentArtifact.from_json(_FIXTURE["artifact"], sources=_sources())


def _compile(artifact: SemanticEnrichmentArtifact) -> RelationshipProjectionCompilation:
    """Compile one artifact's confirmed projections against its own pinned cards."""
    return compile_confirmed_relationship_projections(
        artifact=artifact,
        card_database=CardDatabase(cards={card.grp_id: card for card in _cards()}),
    )


def _row(compilation: RelationshipProjectionCompilation, suffix: str) -> CardRelationship:
    """Return the one compiled row whose finding id ends with a local subject."""
    return next(row for row in compilation.relationships if row.finding_id.endswith(suffix))


def _stored(artifact: SemanticEnrichmentArtifact, suffix: str) -> CardRelationship:
    """Return the one stored relationship whose finding id ends with a local subject."""
    return next(row for row in artifact.confirmed_relationships if row.finding_id.endswith(suffix))


def _conversion(compilation: RelationshipProjectionCompilation, suffix: str) -> RelationshipConversion:
    """Return the one stored-order conversion whose finding id ends with a local subject."""
    return next(item for item in compilation.conversions if item.finding_id.endswith(suffix))


def _with_facts(
    artifact: SemanticEnrichmentArtifact,
    facts: tuple[OracleFact, ...],
) -> SemanticEnrichmentArtifact:
    """Rebuild the confirmation around a different stored fact set."""
    return dataclasses.replace(artifact, oracle_facts=facts, sources=_sources())


def _with_capability(
    artifact: SemanticEnrichmentArtifact,
    capability: str,
    *,
    claim: dict[str, object] | None = None,
    evidence: tuple[OracleEvidence, ...] | None = None,
) -> SemanticEnrichmentArtifact:
    """Rebuild the confirmation with different claim fields or evidence for one capability."""
    facts: list[OracleFact] = []
    for fact in artifact.oracle_facts:
        if fact.finding_id.endswith(f":{capability}"):
            changes: dict[str, object] = {}
            if claim is not None:
                fields = json.loads(fact.claim)
                fields.update(claim)
                changes["claim"] = json.dumps(fields, separators=(",", ":"), sort_keys=True)
            if evidence is not None:
                changes["evidence"] = evidence
            fact = dataclasses.replace(fact, **changes)
        facts.append(fact)
    return _with_facts(artifact, tuple(facts))


def _revalidate(artifact: SemanticEnrichmentArtifact, relationship: CardRelationship) -> None:
    """Re-prove one compiled row against the frozen cards and pins the confirmation carries."""
    validate_relationship_sources(
        relationship=relationship,
        cards={card.grp_id: card for card in _cards()},
        pins={pin.card_id: pin for pin in artifact.cards},
    )


def _without_projection(
    compilation: RelationshipProjectionCompilation,
    artifact: SemanticEnrichmentArtifact,
    suffix: str,
    outcome: RelationshipConversionOutcome,
    reason: str,
) -> None:
    """Assert one stored row keeps its exact payload because the named gate stopped it."""
    relationship = _row(compilation, suffix)
    assert relationship is _stored(artifact, suffix)
    assert relationship.prerequisite_projection is None
    conversion = _conversion(compilation, suffix)
    assert conversion.outcome is outcome
    assert conversion.reason == reason


def _with_prerequisite(
    artifact: SemanticEnrichmentArtifact,
    capability: str,
    *,
    kind: str,
    quote: str,
) -> SemanticEnrichmentArtifact:
    """Rebuild the confirmation with a different retained kind and span for one prerequisite."""
    facts: list[OracleFact] = []
    for fact in artifact.oracle_facts:
        if fact.finding_id.endswith(f":{capability}"):
            claim = json.loads(fact.claim)
            claim["prerequisites"][0]["kind"] = kind
            claim["prerequisites"][0]["evidence"]["quote"] = quote
            fact = dataclasses.replace(
                fact,
                claim=json.dumps(claim, separators=(",", ":"), sort_keys=True),
            )
        facts.append(fact)
    return _with_facts(artifact, tuple(facts))


def _without_card(card_id: int) -> CardDatabase:
    """Return the pinned card database without the card one participant needs."""
    return CardDatabase(cards={card.grp_id: card for card in _cards() if card.grp_id != card_id})


def _with_relationships(
    artifact: SemanticEnrichmentArtifact,
    relationships: tuple[CardRelationship, ...],
) -> SemanticEnrichmentArtifact:
    """Rebuild the confirmation around a different stored relationship set."""
    return dataclasses.replace(
        artifact,
        relationships=relationships,
        confirmed_relationship_ids=tuple(row.finding_id for row in relationships),
        sources=_sources(),
    )


def _with_subtype(
    artifact: SemanticEnrichmentArtifact,
    capability: str,
    subtype: str | None,
) -> SemanticEnrichmentArtifact:
    """Rebuild the confirmation with a different retained token subtype."""
    facts: list[OracleFact] = []
    for fact in artifact.oracle_facts:
        if fact.finding_id.endswith(f":{capability}"):
            claim = json.loads(fact.claim)
            claim["qualifier"]["subtype"] = subtype
            fact = dataclasses.replace(
                fact,
                claim=json.dumps(claim, separators=(",", ":"), sort_keys=True),
            )
        facts.append(fact)
    return _with_facts(artifact, tuple(facts))


def test_friendly_untap_fact_is_recovered_without_disabling_removal() -> None:
    """The offline decoder corrects the role while preserving the saved source record."""
    template = _confirmation().oracle_facts[0]
    quote = (
        "When this creature enters, untap another target creature you control. "
        "If that creature is a Bear, put a +1/+1 counter on it."
    )
    claim = {
        "action": "other",
        "card_name": "Little Bear",
        "destination_zone": "battlefield",
        "face_index": None,
        "face_name": None,
        "prerequisites": [
            {
                "destination_zone": "battlefield",
                "evidence": {
                    "card_id": 103508,
                    "face_index": None,
                    "quote": "When this creature enters",
                },
                "kind": "trigger",
                "quantity": None,
                "source_zone": "battlefield",
                "timing": "when this creature enters",
            }
        ],
        "qualifier": {
            "card_types": ["creature"],
            "mana_value": None,
            "subtype": None,
            "token_restriction": "unrestricted",
        },
        "quantity": None,
        "role": "disabling_removal",
        "source_zone": "battlefield",
        "timing": "when this creature enters",
        "zone": "battlefield",
    }
    fact = dataclasses.replace(
        template,
        finding_id=f"{template.run_id}:103508-untap-another-creature",
        card_id=103508,
        kind="disabling_removal",
        claim=json.dumps(claim, separators=(",", ":"), sort_keys=True),
        evidence=(OracleEvidence(card_id=103508, face_index=None, quote=quote),),
    )

    capability = _decode_capability(fact)

    assert capability is not None
    assert capability.role.value == "untap_support"
    assert capability.action.value == "other"
    assert capability.finding_id == "103508-untap-another-creature"
    assert capability.evidence == fact.evidence
    assert capability.qualifier.card_types[0].value == "creature"
    assert capability.prerequisites[0].evidence.quote == "When this creature enters"


def test_token_replacement_fact_is_recovered_without_token_supply() -> None:
    """The offline decoder preserves Bard's dependency without producer semantics."""
    template = _confirmation().oracle_facts[0]
    quote = (
        "If one or more tokens would be created under your control, "
        "twice that many of those tokens are created instead."
    )
    claim = {
        "action": "create",
        "card_name": "Bard, King of Dale",
        "destination_zone": "battlefield",
        "face_index": None,
        "face_name": None,
        "prerequisites": [
            {
                "destination_zone": "battlefield",
                "evidence": {"card_id": 103524, "face_index": None, "quote": quote},
                "kind": "trigger",
                "quantity": {"relation": "at_least", "value": 1},
                "source_zone": None,
                "timing": None,
            }
        ],
        "qualifier": {
            "card_types": [],
            "mana_value": None,
            "subtype": None,
            "token_restriction": "unrestricted",
        },
        "quantity": {"relation": "variable", "value": None},
        "role": "token_maker",
        "source_zone": None,
        "timing": None,
        "zone": "battlefield",
    }
    fact = dataclasses.replace(
        template,
        finding_id=f"{template.run_id}:103524-token-creation-replacement",
        card_id=103524,
        kind="token_maker",
        claim=json.dumps(claim, separators=(",", ":"), sort_keys=True),
        evidence=(OracleEvidence(card_id=103524, face_index=None, quote=quote),),
    )

    capability = _decode_capability(fact)

    assert capability is not None
    assert capability.role.value == "token_replacement"
    assert capability.action.value == "replace"
    assert capability.quantity is None
    assert capability.source_zone is None
    assert capability.destination_zone is None
    assert capability.prerequisites[0].quantity is not None
    assert capability.prerequisites[0].quantity.value == 1
    assert capability.evidence == fact.evidence


def test_safe_mill_pair_gains_a_source_bound_projection() -> None:
    """A confirmed self-mill/counted-state pair gains exact typed clauses under the gates."""
    artifact = _confirmation()
    stored = _stored(artifact, _MILL)
    relationship = _row(_compile(artifact), _MILL)
    projection = relationship.prerequisite_projection
    assert stored.prerequisite_projection is None
    assert projection is not None
    # the stored relationship itself is preserved and only the compiled projection is added
    assert relationship.mechanism == stored.mechanism
    assert relationship.participants == stored.participants
    assert relationship.claim == stored.claim
    assert relationship.prerequisites == stored.prerequisites
    assert relationship.run_id == stored.run_id
    assert relationship.review == stored.review
    # the safe mill instruction binds its own effect with no coarse prerequisite of its own
    assert (
        projection.source.card_id,
        projection.source.capability_id,
        projection.source.role.value,
        projection.source.face_index,
    ) == (103546, _MILL_CAPABILITY, "self_mill", 1)
    assert projection.source.capability_prerequisites == ()
    source_clause = projection.source.prerequisites[0]
    assert (source_clause.kind.value, source_clause.subject, source_clause.operation) == (
        "condition",
        "event",
        "mill",
    )
    assert source_clause.object_kind == "card"
    assert source_clause.object_quote == "four cards"
    assert (source_clause.quantity.value, source_clause.quantity.relation.value) == (4, "exactly")
    assert source_clause.source_zone is not None
    assert source_clause.source_zone.zone.value == "library"
    assert source_clause.destination_zone is not None
    assert source_clause.destination_zone.zone.value == "graveyard"
    assert source_clause.capability_prerequisite_indices == ()
    # the payoff's retained counted state binds the one clause its own line cites
    assert (
        projection.target.card_id,
        projection.target.capability_id,
        projection.target.role.value,
        projection.target.face_index,
        projection.target.face_name,
    ) == (
        103422,
        _THRESHOLD_CAPABILITY,
        "graveyard_payoff",
        0,
        "Most Decrepit Old Bird",
    )
    target_clause = projection.target.prerequisites[0]
    assert target_clause.capability_prerequisite_indices == (0,)
    assert (target_clause.kind.value, target_clause.operation) == ("condition", "count")
    assert (target_clause.quantity.value, target_clause.quantity.relation.value) == (7, "at_least")
    assert target_clause.source_zone is not None
    assert target_clause.source_zone.zone.value == "graveyard"
    # every clause quote is a copied span of the line it cites
    clauses = (*projection.source.prerequisites, *projection.target.prerequisites)
    for clause in clauses:
        assert clause.object_quote in clause.evidence.quote
        assert clause.operation_quote in clause.evidence.quote
    # all stored evidence survives and each clause's exact quote is unioned in
    assert set(stored.oracle_evidence) <= set(relationship.oracle_evidence)
    for clause in clauses:
        assert any(item.quote == clause.evidence.quote for item in relationship.oracle_evidence)
    # the projection re-proves itself against the pins and frozen cards the confirmation carries
    pins = {pin.card_id: pin for pin in artifact.cards}
    validate_relationship_pins(relationship=relationship, pins=pins)
    validate_relationship_sources(
        relationship=relationship,
        cards={card.grp_id: card for card in _cards()},
        pins=pins,
    )


@pytest.mark.parametrize(("kind", "quote", "qualification_kind"), _UNTYPABLE_PREREQUISITES)
def test_untypable_prerequisite_kinds_project_as_retained_qualifications(
    kind: str,
    quote: str,
    qualification_kind: str,
) -> None:
    """A prerequisite no typed clause can bind keeps its exact statement under its own kind."""
    artifact = _confirmation()
    variant = _with_prerequisite(artifact, _THRESHOLD_CAPABILITY, kind=kind, quote=quote)
    compilation = _compile(variant)
    relationship = _row(compilation, _MILL)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    assert _conversion(compilation, _MILL).outcome is RelationshipConversionOutcome.QUALIFIED
    assert _conversion(compilation, _MILL).reason == "projected"
    # the payoff keeps no typed clause for the retained prerequisite, only its exact statement
    assert projection.target.prerequisites == ()
    assert projection.target.capability_prerequisites[0].kind.value == kind
    qualification = projection.target.qualifications[0]
    assert qualification.kind.value == qualification_kind
    assert qualification.selector == quote
    assert qualification.occurrence == 0
    assert qualification.evidence.card_id == 103422
    assert qualification.evidence.face_index == 0
    assert qualification.evidence.quote == (
        "Threshold — This creature gets +1/+1 as long as there are seven or more cards in "
        "your graveyard."
    )
    assert qualification.selector in qualification.evidence.quote
    # the source keeps its own typed effect clause: only the untranslated half is retained
    assert [clause.object_quote for clause in projection.source.prerequisites] == ["four cards"]
    # the retained statement reaches the relationship's own evidence union and re-proves
    assert any(item.quote == qualification.evidence.quote for item in relationship.oracle_evidence)
    validate_relationship_sources(
        relationship=relationship,
        cards={card.grp_id: card for card in _cards()},
        pins={pin.card_id: pin for pin in artifact.cards},
    )


def test_trailing_condition_inside_the_action_never_yields_a_projection() -> None:
    """A mill instruction that states its own "if ..." condition binds no clean effect clause."""
    artifact = _confirmation()
    stored = _stored(artifact, _TRAILING_MILL)
    compilation = _compile(artifact)
    relationship = _row(compilation, _TRAILING_MILL)
    assert relationship is stored
    assert relationship.prerequisite_projection is None
    conversion = _conversion(compilation, _TRAILING_MILL)
    assert conversion.outcome is RelationshipConversionOutcome.UNSUPPORTED
    assert conversion.reason == "source_clause_unbound"


def test_condition_on_the_next_instruction_does_not_taint_the_first_mill() -> None:
    """The following ", then if ..." instruction owns its condition; the mill keeps its own."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _NEXT_ACTION_MILL)
    projection = relationship.prerequisite_projection
    assert projection is not None
    clause = projection.source.prerequisites[0]
    assert (clause.operation, clause.object_quote) == ("mill", "four cards")
    assert (clause.quantity.value, clause.quantity.relation.value) == (4, "exactly")
    assert clause.controller == "any"
    assert clause.exclusion == "none"
    assert clause.source_zone is not None
    assert clause.source_zone.zone.value == "library"
    assert clause.destination_zone is not None
    assert clause.destination_zone.zone.value == "graveyard"


@pytest.mark.parametrize("subtype", ("goblin", "army", None))
def test_partial_retained_subtype_never_binds_a_clause_but_keeps_the_family(
    subtype: str | None,
) -> None:
    """A partial retained subtype binds no typed clause; the printed instruction still decides."""
    artifact = _confirmation()
    stored = _stored(artifact, _ARMY_PAIR)
    assert stored.prerequisite_projection is None
    variant = _with_subtype(artifact, _ARMY_CAPABILITY, subtype)
    compilation = _compile(variant)
    relationship = _row(compilation, _ARMY_PAIR)
    assert relationship is not _stored(variant, _ARMY_PAIR)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    # the partial claim binds no typed clause, so the cited creation instruction carries the pair
    assert projection.source.prerequisites == ()
    selectors = [item.selector for item in projection.source.qualifications]
    assert _ARMY_INSTRUCTION in selectors
    assert _conversion(compilation, _ARMY_PAIR).outcome is RelationshipConversionOutcome.QUALIFIED


def test_storied_anthem_keeps_the_reminder_it_references() -> None:
    """Fíli's anthem cites both its payoff and the Storied reminder that defines the story."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _STORIED_ANTHEM)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    target = projection.target
    assert (target.card_id, target.capability_id) == (103382, "103382-enduring-story-anthem")
    reminder = "Storied (If you control three or more artifacts, legendaries, and/or Sagas, you have an enduring story for the rest of the game.)"
    storied = next(item for item in target.qualifications if reminder in item.selector)
    assert storied.kind is QualificationKind.CONDITION
    # the reminder and the bound clause are cited from the same face's complete lines
    assert reminder in storied.evidence.quote
    assert "As long as you have an enduring story" in storied.evidence.quote
    _revalidate(artifact, relationship)


def test_complete_token_subtype_still_compiles_the_pair() -> None:
    """The same pair compiles once the retained subtype states the whole explicit sequence."""
    artifact = _confirmation()
    variant = _with_subtype(artifact, _ARMY_CAPABILITY, "goblin army")
    projection = _row(_compile(variant), _ARMY_PAIR).prerequisite_projection
    assert projection is not None
    clause = projection.source.prerequisites[0]
    assert (clause.subject, clause.operation, clause.object_kind) == ("output", "create", "token")
    assert clause.card_types == ("creature",)
    assert clause.subtype == "goblin army"
    assert clause.object_quote == "a 0/0 black Goblin Army creature token"


def test_untypable_amass_antecedent_keeps_the_pair_qualified() -> None:
    """An amass reminder's no-Army antecedent has no typed field, so the pair stays qualified."""
    artifact = _confirmation()
    # both facts of the pair are present: the qualification states the antecedent those facts print
    facts = {(fact.card_id, fact.finding_id.rsplit(":", 1)[-1]) for fact in artifact.oracle_facts}
    assert (103442, "103442-amass-goblin-army-token") in facts
    assert (103381, "103381-go-wide-power") in facts
    stored = _stored(artifact, _AMASS)
    relationship = _row(_compile(artifact), _AMASS)
    assert relationship is not stored
    assert relationship.finding_id == stored.finding_id
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.source.prerequisites == ()
    qualifications = {item.kind: item for item in projection.source.qualifications}
    assert set(qualifications) == {QualificationKind.MODE, QualificationKind.CONDITION}
    # one instruction body: the counters the keyword grows and the single body it creates instead
    mode = qualifications[QualificationKind.MODE]
    assert "Amass Goblins 3." in mode.selector
    assert "counters on an Army" in mode.selector
    assert "If you don't control an Army" in mode.selector
    assert qualifications[QualificationKind.CONDITION].selector == "If you don't control an Army"
    # the power-setting payoff keeps its own statement and no branch becomes a typed creation
    assert [item.kind for item in projection.target.qualifications] == [QualificationKind.CONDITION]
    assert not [
        clause
        for clause in (*projection.source.prerequisites, *projection.target.prerequisites)
        if clause.operation == "create" and clause.quantity is not None
    ]


@pytest.mark.parametrize(
    ("capability", "affected"),
    (
        (_MILL_CAPABILITY, (_MILL,)),
        (_THRESHOLD_CAPABILITY, (_MILL, _NEXT_ACTION_MILL)),
    ),
)
def test_missing_capability_fact_never_yields_a_projection(
    capability: str,
    affected: tuple[str, ...],
) -> None:
    """Without both strict v2 facts of a pair, its stored relationship is returned untouched."""
    artifact = _confirmation()
    stripped = _with_facts(
        artifact,
        tuple(
            fact
            for fact in artifact.oracle_facts
            if not fact.finding_id.endswith(f":{capability}")
        ),
    )
    assert len(stripped.oracle_facts) == len(artifact.oracle_facts) - 1
    compilation = _compile(stripped)
    for suffix in affected:
        relationship = _row(compilation, suffix)
        assert relationship is _stored(stripped, suffix)
        assert _conversion(compilation, suffix).outcome is RelationshipConversionOutcome.MISSING_EVIDENCE
        assert relationship.prerequisite_projection is None


def test_conflicting_capability_facts_never_yield_a_projection() -> None:
    """A second fact for one card and capability that disagrees makes the identity ambiguous."""
    artifact = _confirmation()
    fact = next(
        item for item in artifact.oracle_facts if item.finding_id.endswith(f":{_MILL_CAPABILITY}")
    )
    claim = json.loads(fact.claim)
    claim["role"] = "token_maker"
    conflicting = dataclasses.replace(
        fact,
        finding_id=f"{_LOCAL_RUN}:{_MILL_CAPABILITY}",
        run_id=_LOCAL_RUN,
        claim=json.dumps(claim, separators=(",", ":"), sort_keys=True),
    )
    variant = _with_facts(artifact, (*artifact.oracle_facts, conflicting))
    relationship = _row(_compile(variant), _MILL)
    assert relationship is _stored(variant, _MILL)
    assert relationship.prerequisite_projection is None


def test_identical_duplicate_capability_fact_keeps_the_projection() -> None:
    """Duplicating the same strict fact under another identity leaves its key unambiguous."""
    artifact = _confirmation()
    fact = next(
        item for item in artifact.oracle_facts if item.finding_id.endswith(f":{_MILL_CAPABILITY}")
    )
    duplicate = dataclasses.replace(fact, finding_id=f"{_LOCAL_RUN}:{_MILL_CAPABILITY}")
    variant = _with_facts(artifact, (*artifact.oracle_facts, duplicate))
    relationship = _row(_compile(variant), _MILL)
    assert relationship.prerequisite_projection is not None


@pytest.mark.parametrize(
    "claim",
    (
        '{"role":"self_mill","role":"token_maker"}',
        '{"role":NaN,"card_name":"Thranduil, Sindarin Liege // Silvan Rally"}',
    ),
)
def test_unreadable_capability_claim_never_introduces_a_projection(claim: str) -> None:
    """Duplicate keys and non-finite constants are no facts, so no clause may be invented."""
    artifact = _confirmation()
    fact = next(
        item
        for item in artifact.oracle_facts
        if item.finding_id.endswith(f":{_THRESHOLD_CAPABILITY}")
    )
    variant = _with_facts(
        artifact,
        tuple(
            dataclasses.replace(item, claim=claim) if item is fact else item
            for item in artifact.oracle_facts
        ),
    )
    rows = {row.finding_id: row for row in _compile(variant).relationships}
    stored = {row.finding_id: row for row in variant.confirmed_relationships}
    for row in rows.values():
        if row.finding_id.endswith((_MILL, _NEXT_ACTION_MILL)):
            assert row is stored[row.finding_id]
            assert row.prerequisite_projection is None


def test_model_decided_relationship_is_returned_exactly_as_stored() -> None:
    """A relationship that is not this artifact's local pair matcher keeps its stored payload."""
    artifact = _confirmation()
    stored = _stored(artifact, _RECURSION)
    legacy = dataclasses.replace(
        stored,
        finding_id=f"relationship:{_RECURSION}",
        run_id=_SOURCE_RUN,
    )
    variant = _with_relationships(artifact, (legacy,))
    rows = _compile(variant)
    assert rows.relationships == variant.confirmed_relationships
    assert rows.relationships[0] is variant.confirmed_relationships[0]


def test_already_projected_relationship_is_returned_exactly_as_stored() -> None:
    """A projection that survives the artifact's own gates is retained without recompilation."""
    artifact = _confirmation()
    first = _compile(artifact)
    assert _row(first, _MILL).prerequisite_projection is not None
    # rebuilding the confirmation around the compiled rows re-validates every projection source
    variant = _with_relationships(artifact, first.relationships)
    compiled = _compile(variant)
    stored = {row.finding_id: row for row in variant.confirmed_relationships}
    assert [row is stored[row.finding_id] for row in compiled.relationships] == [True] * len(
        compiled.relationships
    )
    assert _row(compiled, _MILL).to_json() == _row(first, _MILL).to_json()


def test_compilation_preserves_the_artifact_bytes_and_stored_rows() -> None:
    """The compiler keeps stored order and leaves the confirmation it reads byte-identical."""
    artifact = _confirmation()
    before = artifact.to_bytes()
    compilation = _compile(artifact)
    rows = compilation.relationships
    assert artifact.to_bytes() == before
    assert [row.finding_id for row in rows] == [
        row.finding_id for row in artifact.confirmed_relationships
    ]
    # an unconverted row is returned as the identical stored payload; every projected row is a
    # fresh record that carries its compiled clauses and qualifications
    unconverted = {
        RelationshipConversionOutcome.UNSUPPORTED,
        RelationshipConversionOutcome.CONTRADICTION,
        RelationshipConversionOutcome.MISSING_EVIDENCE,
    }
    stored_rows = {row.finding_id: row for row in artifact.confirmed_relationships}
    assert all(
        (row is stored_rows[row.finding_id]) is (item.outcome in unconverted)
        for row, item in zip(rows, compilation.conversions)
    )
    # a recovered row is recompiled into a fresh record that carries its compiled projection
    assert _row(compilation, _AMASS) is not _stored(artifact, _AMASS)
    # the closed guards stay unprojected whichever way their gate refuses them
    assert _row(compilation, _TRAILING_MILL).prerequisite_projection is None
    assert _row(compilation, _RECURSION).prerequisite_projection is None
    assert all(
        "prerequisite_projection" not in stored for stored in json.loads(before)["relationships"]
    )


def test_every_stored_relationship_receives_one_conversion_in_stored_order() -> None:
    """The compilation accounts for each stored row once, with a closed outcome and reason."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    assert len(artifact.confirmed_relationships) == 32
    assert [item.finding_id for item in compilation.conversions] == [
        row.finding_id for row in artifact.confirmed_relationships
    ]
    assert [item.mechanism for item in compilation.conversions] == [
        row.mechanism for row in artifact.confirmed_relationships
    ]
    assert all(
        item.outcome in tuple(RelationshipConversionOutcome) for item in compilation.conversions
    )
    # every row reports either a projection or the exact gate that stopped it
    projected = {RelationshipConversionOutcome.DECODED, RelationshipConversionOutcome.QUALIFIED}
    assert all(
        (item.reason == "projected") is (item.outcome in projected)
        for item in compilation.conversions
    )
    # the closed guard rows keep the exact gate that decides them
    assert [
        (_conversion(compilation, suffix).outcome, _conversion(compilation, suffix).reason)
        for suffix in (_TRAILING_MILL, _RECURSION, _ARMY_PAIR)
    ] == [
        (RelationshipConversionOutcome.UNSUPPORTED, "source_clause_unbound"),
        (RelationshipConversionOutcome.CONTRADICTION, "zone_supply_contradiction:graveyard"),
        (RelationshipConversionOutcome.QUALIFIED, "projected"),
    ]
    # the projected rows and the stored rows stay in the same stored order
    assert [row.finding_id for row in compilation.relationships] == [
        item.finding_id for item in compilation.conversions
    ]


def test_graveyard_consumption_of_a_counted_zone_is_rejected() -> None:
    """An enabler that removes a counted zone cannot supply the payoff's own count."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    stored = _stored(artifact, _RECURSION)
    rejected = _row(compilation, _RECURSION)
    assert rejected is stored
    assert rejected.prerequisite_projection is None
    conversion = _conversion(compilation, _RECURSION)
    assert conversion.outcome is RelationshipConversionOutcome.CONTRADICTION
    assert conversion.reason == "zone_supply_contradiction:graveyard"
    # the mill pairs read the same counted zone but move cards into it, so they keep projecting
    for suffix in (_MILL, _NEXT_ACTION_MILL):
        assert _row(compilation, suffix).prerequisite_projection is not None
        expected = (
            RelationshipConversionOutcome.QUALIFIED
            if suffix == _MILL
            else RelationshipConversionOutcome.DECODED
        )
        assert _conversion(compilation, suffix).outcome is expected


def test_missing_evidence_reports_the_unusable_fact_or_the_unpinned_card() -> None:
    """A pair without a usable fact or a resolvable pinned card reports missing evidence."""
    artifact = _confirmation()
    stripped = _with_facts(
        artifact,
        tuple(
            fact
            for fact in artifact.oracle_facts
            if not fact.finding_id.endswith(f":{_MILL_CAPABILITY}")
        ),
    )
    compilation = _compile(stripped)
    conversion = _conversion(compilation, _MILL)
    assert conversion.outcome is RelationshipConversionOutcome.MISSING_EVIDENCE
    assert conversion.reason == "source_capability_fact_unusable"
    # an artifact pins every source card it stores, so the unpinned variant drops the card from
    # the pinned card database the compiler resolves participants against
    unpinned = compile_confirmed_relationship_projections(
        artifact=artifact,
        card_database=_without_card(103546),
    )
    conversion = _conversion(unpinned, _MILL)
    assert conversion.outcome is RelationshipConversionOutcome.MISSING_EVIDENCE
    assert conversion.reason == "participant_card_unpinned"
    unpinned_target = compile_confirmed_relationship_projections(
        artifact=artifact,
        card_database=_without_card(103422),
    )
    assert _conversion(unpinned_target, _MILL).outcome is RelationshipConversionOutcome.MISSING_EVIDENCE
    assert _conversion(unpinned_target, _MILL).reason == "participant_card_unpinned"


def test_two_compilations_produce_identical_rows_and_conversions() -> None:
    """Identical pinned inputs produce byte-identical rows, conversions and JSON."""
    first = _compile(_confirmation())
    second = _compile(_confirmation())
    assert [row.to_json() for row in first.relationships] == [
        row.to_json() for row in second.relationships
    ]
    assert [item.to_json() for item in first.conversions] == [
        item.to_json() for item in second.conversions
    ]
    assert json.dumps([item.to_json() for item in first.conversions], sort_keys=True) == json.dumps(
        [item.to_json() for item in second.conversions], sort_keys=True
    )


def test_qualification_quoting_another_card_fails_source_validation() -> None:
    """A retained qualification that quotes another card's line cannot re-prove its source."""
    artifact = _confirmation()
    variant = _with_prerequisite(
        artifact,
        _THRESHOLD_CAPABILITY,
        kind="trigger",
        quote=_THRESHOLD_STATEMENT,
    )
    relationship = _row(_compile(variant), _MILL)
    projection = relationship.prerequisite_projection
    assert projection is not None
    cards = {card.grp_id: card for card in _cards()}
    pins = {pin.card_id: pin for pin in artifact.cards}
    validate_relationship_sources(relationship=relationship, cards=cards, pins=pins)
    qualification = projection.target.qualifications[0]
    other_line = next(
        line
        for line in (cards[990010].oracle_text or "").split("\n")
        if "seven or more creatures you control" in line
    )
    tampered = RelationshipQualification(
        kind=qualification.kind,
        evidence=OracleEvidence(
            card_id=qualification.evidence.card_id,
            face_index=qualification.evidence.face_index,
            quote=other_line,
        ),
        selector="seven or more creatures you control",
        occurrence=0,
    )
    forged = dataclasses.replace(
        relationship,
        oracle_evidence=(
            *relationship.oracle_evidence,
            tampered.evidence,
        ),
        prerequisite_projection=dataclasses.replace(
            projection,
            target=dataclasses.replace(projection.target, qualifications=(tampered,)),
        ),
    )
    with pytest.raises(PrerequisiteProjectionError):
        validate_relationship_sources(relationship=forged, cards=cards, pins=pins)


def test_frozen_recruit_payoff_keeps_the_discard_condition_and_the_draw_discard_order() -> None:
    """Recruit stays qualified by its real nonland-discard clause, never an optional self-draw."""
    artifact = _confirmation()
    stored = _stored(artifact, _RECRUIT)
    assert stored.prerequisite_projection is None
    relationship = _row(_compile(artifact), _RECRUIT)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    source = projection.source
    assert (source.card_id, source.capability_id, source.role.value, source.face_index) == (
        103381,
        _RECRUIT_CAPABILITY,
        "token_maker",
        None,
    )
    # the keyword instruction alone carries no typed clause, so both statements are retained
    assert source.prerequisites == ()
    qualifications = {item.kind: item for item in source.qualifications}
    assert set(qualifications) == {QualificationKind.CONDITION, QualificationKind.TIMING}
    assert qualifications[QualificationKind.CONDITION].selector == _RECRUIT_CONDITION
    assert qualifications[QualificationKind.TIMING].selector == _RECRUIT_FRAME
    # every citation is the complete printed instruction, draw before discard and condition after
    assert all(item.evidence.quote == _RECRUIT_INSTRUCTION for item in source.qualifications)
    # no fabricated starting-hand requirement and no optional draw or discard appears anywhere
    retained = [f"{item.selector}\n{item.evidence.quote}" for item in source.qualifications]
    assert not [text for text in retained if "starting hand" in text]
    assert not [text for text in retained if "may draw" in text or "may discard" in text]
    _revalidate(artifact, relationship)


def test_adventure_amass_face_keeps_its_closed_keyword_instruction() -> None:
    """A reminder-free Adventure face keeps one exact mode and no invented creation quantity."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    relationship = _row(compilation, _ADVENTURE_AMASS)
    conversion = _conversion(compilation, _ADVENTURE_AMASS)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert conversion.outcome is RelationshipConversionOutcome.QUALIFIED
    source = projection.source
    assert (
        source.card_id,
        source.capability_id,
        source.role.value,
        source.face_index,
        source.face_name,
    ) == (103449, _ADVENTURE_AMASS_CAPABILITY, "token_maker", 1, "Clap! Snap!")
    assert source.prerequisites == ()
    assert [item.kind for item in source.qualifications] == [
        QualificationKind.MODE,
        QualificationKind.MODE,
    ]
    qualifications = {item.selector: item for item in source.qualifications}
    mode = qualifications[_ADVENTURE_AMASS_INSTRUCTION]
    sequence = qualifications[_ADVENTURE_SEQUENCE]
    assert mode.selector == _ADVENTURE_AMASS_INSTRUCTION
    assert sequence.selector == _ADVENTURE_SEQUENCE
    assert sequence.evidence.face_index == 1
    assert source.card_id == relationship.participants[0]
    # the closed keyword body creates one Army; the number counts the counters it grows, never
    # tokens, so its reminder is neither printed here nor copied from another amass instruction
    assert "counters on an Army" not in mode.selector
    assert "counters on an Army" not in mode.evidence.quote
    assert _ADVENTURE_REMINDER in mode.evidence.quote
    assert not [
        clause
        for clause in (*source.prerequisites, *projection.target.prerequisites)
        if clause.operation == "create"
    ]
    assert all(
        qualification.selector != _ADVENTURE_SEQUENCE
        for qualification in projection.target.qualifications
    )


def test_amass_capability_pinned_to_the_other_face_never_borrows_its_instruction() -> None:
    """A capability pinned to the creature face cannot read the Adventure face's amass line."""
    artifact = _confirmation()
    variant = _with_capability(
        artifact,
        _ADVENTURE_AMASS_CAPABILITY,
        claim={"face_index": 0, "face_name": _WRONG_FACE_NAME},
        evidence=(OracleEvidence(card_id=103449, face_index=0, quote=_MENACE_LINE),),
    )
    _without_projection(
        _compile(variant),
        variant,
        _ADVENTURE_AMASS,
        RelationshipConversionOutcome.UNSUPPORTED,
        "source_clause_unbound",
    )


def test_azog_controlled_amass_never_projects_as_unqualified_self_supply() -> None:
    """Azog's controller branch survives even when the stored fact cites only the reminder."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _AZOG)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    source = projection.source
    assert (source.card_id, source.capability_id, source.role.value) == (
        103435,
        _AZOG_CAPABILITY,
        "token_maker",
    )
    assert source.prerequisites == ()
    qualifications = {item.kind: item for item in source.qualifications}
    assert set(qualifications) == {
        QualificationKind.CONDITION,
        QualificationKind.MODE,
        QualificationKind.PARTY,
        QualificationKind.TIMING,
    }
    # the instruction keeps the destruction frame, the controller amass and the own-controller draw
    assert qualifications[QualificationKind.MODE].selector == _AZOG_MODE
    assert qualifications[QualificationKind.PARTY].selector == _AZOG_PARTY
    assert qualifications[QualificationKind.CONDITION].selector == _AZOG_CONDITION
    assert qualifications[QualificationKind.TIMING].selector == _AZOG_TIMING
    # the same row from a reminder-only citation still resolves the complete instruction line, so
    # the reminder's own wording can never be presented as the player's unqualified supply
    reminder_only = _with_capability(
        artifact,
        _AZOG_CAPABILITY,
        evidence=(OracleEvidence(card_id=103435, face_index=None, quote=_AZOG_REMINDER),),
    )
    variant = _row(_compile(reminder_only), _AZOG)
    variant_projection = variant.prerequisite_projection
    assert variant_projection is not None
    assert variant_projection.outcome.value == "qualified"
    retained = {item.kind: item for item in variant_projection.source.qualifications}
    assert set(retained) == set(qualifications)
    assert retained[QualificationKind.PARTY].selector == _AZOG_PARTY


def test_misty_chapter_keeps_the_post_treasure_threshold_and_conditional_sacrifice() -> None:
    """The Dragon needs four Treasures and a sacrifice on any chapter, never a finished Saga."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _MISTY)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    source = projection.source
    assert (source.card_id, source.capability_id, source.role.value) == (
        103482,
        _MISTY_CAPABILITY,
        "token_maker",
    )
    assert source.prerequisites == ()
    qualifications = {item.kind: item for item in source.qualifications}
    assert set(qualifications) == {
        QualificationKind.CONDITION,
        QualificationKind.QUANTITY,
        QualificationKind.TIMING,
    }
    # the payoff is the whole post-Treasure sequence: threshold, sacrifice, then conditional Dragon
    condition = qualifications[QualificationKind.CONDITION]
    assert condition.selector.startswith(_MISTY_SEQUENCE)
    assert condition.selector.index(_MISTY_QUANTITY) < condition.selector.index("If you do, create")
    # the count is checked after the Treasure is created, and no single chapter substitutes for it
    assert qualifications[QualificationKind.QUANTITY].selector == _MISTY_QUANTITY
    assert qualifications[QualificationKind.TIMING].selector == _MISTY_RANGE
    assert not [item for item in source.qualifications if item.selector == "IV"]
    assert not [item for item in source.qualifications if "Sacrifice after IV" in item.selector]
    # every citation stays the complete printed chapter, Treasure reminder included
    assert all(item.evidence.quote == _MISTY_CHAPTER for item in source.qualifications)


def test_two_line_chosen_type_payoff_round_trips_as_one_complete_citation() -> None:
    """The chosen-type choice and its selected-type payoff stay one complete-line citation."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _CHOSEN_TYPE)
    projection = relationship.prerequisite_projection
    assert projection is not None
    target = projection.target
    assert (target.card_id, target.capability_id, target.face_index) == (
        103397,
        _CHOSEN_TYPE_CAPABILITY,
        0,
    )
    retained = (*target.prerequisites, *target.qualifications)
    assert len(retained) == 1
    assert retained[0].evidence.quote == _CHOSEN_TYPE_PARAGRAPH
    assert _CHOSEN_TYPE_CHOICE in retained[0].evidence.quote
    assert _CHOSEN_TYPE_PAYOFF in retained[0].evidence.quote
    # the compiled row survives its own JSON round trip and re-proves against the pinned cards
    restored = CardRelationship.from_json(json.loads(json.dumps(relationship.to_json())))
    restored_target = restored.prerequisite_projection.target
    restored_retained = (*restored_target.prerequisites, *restored_target.qualifications)
    assert restored_retained[0].evidence.quote == _CHOSEN_TYPE_PARAGRAPH
    _revalidate(artifact, restored)


def test_chosen_type_evidence_citing_only_the_choice_line_never_binds_the_payoff() -> None:
    """A citation of the choice line alone cannot borrow the selected-type payoff sentence."""
    artifact = _confirmation()
    variant = _with_capability(
        artifact,
        _CHOSEN_TYPE_CAPABILITY,
        evidence=(OracleEvidence(card_id=103397, face_index=0, quote=_CHOSEN_TYPE_CHOICE),),
    )
    _without_projection(
        _compile(variant),
        variant,
        _CHOSEN_TYPE,
        RelationshipConversionOutcome.UNSUPPORTED,
        "target_clause_unbound",
    )


def test_chosen_type_evidence_on_another_face_never_yields_a_projection() -> None:
    """A face pin whose retained prerequisite lies on the other face is unusable evidence."""
    artifact = _confirmation()
    variant = _with_capability(
        artifact,
        _CHOSEN_TYPE_CAPABILITY,
        claim={"face_index": 1, "face_name": "At the Door"},
        evidence=(
            OracleEvidence(card_id=103397, face_index=1, quote="Create X 2/2 red Dwarf creature tokens."),
        ),
    )
    _without_projection(
        _compile(variant),
        variant,
        _CHOSEN_TYPE,
        RelationshipConversionOutcome.MISSING_EVIDENCE,
        "target_capability_fact_unusable",
    )


def test_repeated_chosen_type_paragraph_is_ambiguous_evidence() -> None:
    """A face that prints the paragraph twice resolves to no window, so nothing is borrowed."""
    artifact = _confirmation()
    _without_projection(
        _compile(artifact),
        artifact,
        _REPEATED_PAYOFF,
        RelationshipConversionOutcome.UNSUPPORTED,
        "target_clause_unbound",
    )


def _capability_subtype(artifact: SemanticEnrichmentArtifact, capability: str) -> str | None:
    """Return the coarse token subtype one stored capability fact retains, or None."""
    fact = next(
        item for item in artifact.oracle_facts if item.finding_id.endswith(f":{capability}")
    )
    return json.loads(fact.claim)["qualifier"]["subtype"]


def test_goblin_army_satisfies_a_goblin_only_sacrifice_outlet() -> None:
    """A Goblin Army token is a Goblin even though its qualifier states two words at once."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    relationship = _row(compilation, _GOBLIN_ARMY_OUTLET)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    assert projection.source.capability_id == "103442-amass-goblin-army-token"
    target = projection.target
    assert (target.card_id, target.capability_id) == (103529, _OUTLET_CAPABILITY)
    assert target.prerequisites == ()
    qualifications = {item.kind: item for item in target.qualifications}
    assert set(qualifications) == {QualificationKind.CONDITION, QualificationKind.COST}
    assert qualifications[QualificationKind.CONDITION].selector == _GOBLIN_OUTLET_QUOTE
    assert qualifications[QualificationKind.COST].selector == _GOBLIN_OUTLET_COST
    conversion = _conversion(compilation, _GOBLIN_ARMY_OUTLET)
    assert conversion.outcome is RelationshipConversionOutcome.QUALIFIED


def test_human_soldier_tokens_never_satisfy_a_goblin_only_sacrifice_outlet() -> None:
    """A recruit whose qualifier omits its subtype still contradicts a Goblin-only outlet."""
    artifact = _confirmation()
    # the coarse qualifier states no subtype at all: the produced subtype is read from the exact
    # token instruction, so a missing word is never treated as compatible
    assert _capability_subtype(artifact, _LOOKOUT_CAPABILITY) is None
    _without_projection(
        _compile(artifact),
        artifact,
        _LOOKOUT_OUTLET,
        RelationshipConversionOutcome.CONTRADICTION,
        "token_subtype_contradiction",
    )


def test_human_soldier_tokens_never_satisfy_an_elf_anthem() -> None:
    """The same recruit family contradicts an Elf anthem instead of borrowing a match."""
    artifact = _confirmation()
    assert _capability_subtype(artifact, _COMPANY_CAPABILITY) is None
    _without_projection(
        _compile(artifact),
        artifact,
        _COMPANY_ANTHEM,
        RelationshipConversionOutcome.CONTRADICTION,
        "token_subtype_contradiction",
    )


def test_non_bear_tokens_reach_beorn_only_through_its_own_type_conversion() -> None:
    """A Human Soldier reaches the Bear payoff only with Beorn's own conversion instruction."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _BEORN)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    assert projection.source.capability_id == _RECRUIT_CAPABILITY
    target = projection.target
    assert (target.card_id, target.capability_id) == (103499, _BEORN_CAPABILITY)
    assert target.prerequisites == ()
    selectors = [item.selector for item in target.qualifications]
    assert _BEORN_PAYOFF in selectors
    conversions = [
        item
        for item in target.qualifications
        if item.kind is QualificationKind.CONDITION and item.selector.startswith(_BEORN_CONVERSION)
    ]
    assert len(conversions) == 1
    # the conversion and the payoff are cited from the same face's complete lines
    assert conversions[0].evidence.quote == f"{_BEORN_PAYOFF}\n{_BEORN_COMBAT_ABILITY}"


def test_sacrifice_cost_keeps_its_artifact_or_creature_and_mana_alternatives() -> None:
    """The recovered cost keeps both the sacrifice alternative and the {4} mana alternative."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _SACRIFICE_COST)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    target = projection.target
    assert (target.card_id, target.capability_id) == (103460, _SACRIFICE_COST_CAPABILITY)
    assert target.prerequisites == ()
    qualifications = {item.kind: item for item in target.qualifications}
    assert set(qualifications) == {
        QualificationKind.CHOICE,
        QualificationKind.CONDITION,
        QualificationKind.COST,
    }
    assert qualifications[QualificationKind.COST].selector == _SACRIFICE_STATEMENT
    choice = qualifications[QualificationKind.CHOICE]
    assert choice.selector == _SACRIFICE_CHOICE
    # paying {4} is the stated alternative, so sacrificing is never presented as mandatory
    assert "sacrifice an artifact or creature" in choice.selector
    assert "pay {4}" in choice.selector


def test_sacrifice_outlet_keeps_its_alternatives_and_activation_restrictions() -> None:
    """A creature-or-artifact outlet keeps its printed restrictions after recovery."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _SACRIFICE_OUTLET)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    target = projection.target
    assert (target.card_id, target.capability_id) == (103491, _SACRIFICE_OUTLET_CAPABILITY)
    assert target.prerequisites == ()
    qualifications = {item.kind: item for item in target.qualifications}
    assert set(qualifications) == {
        QualificationKind.CONDITION,
        QualificationKind.COST,
        QualificationKind.TIMING,
    }
    assert qualifications[QualificationKind.COST].selector == _OUTLET_COST
    assert qualifications[QualificationKind.TIMING].selector == _OUTLET_RESTRICTION


@pytest.mark.parametrize(
    ("suffix", "card_id", "selector"),
    (
        (_LANDFALL_BEAR, 103503, _BEAR_INSTRUCTION),
        (_THRANDUIL_LANDFALL, 103546, _ELF_INSTRUCTION),
        (_FILI_DWARVES, 103382, _DWARF_INSTRUCTION),
    ),
)
def test_cited_creation_instruction_carries_a_landfall_or_storied_token_maker(
    suffix: str,
    card_id: int,
    selector: str,
) -> None:
    """The cited creation instruction, not the role, decides what a token maker produces."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    relationship = _row(compilation, suffix)
    assert relationship is not _stored(artifact, suffix)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    source = projection.source
    assert source.card_id == card_id
    # the retained instruction is the complete printed statement and cites its own line
    qualification = next(
        item for item in source.qualifications if item.selector == selector
    )
    assert qualification.evidence.card_id == card_id
    assert selector in qualification.evidence.quote
    assert _conversion(compilation, suffix).outcome is RelationshipConversionOutcome.QUALIFIED


def test_chapter_granted_landfall_keeps_the_chapter_that_grants_it() -> None:
    """A Saga that gains the landfall clause retains the chapter and does not imply it earlier."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _CHAPTER_LANDFALL)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    selectors = [item.selector for item in projection.source.qualifications]
    granted = next(item for item in selectors if "create a 1/1 green Elf creature token" in item)
    assert granted.startswith("II — This Saga gains")


def test_conditional_wolf_creation_keeps_its_parent_condition() -> None:
    """Head of the Hunt's Wolf stays conditional on the exile replacement that precedes it."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _WOLF_CREATION)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    source = projection.source
    selectors = [item.selector for item in source.qualifications]
    assert _WOLF_INSTRUCTION in selectors
    parent = next(item for item in source.qualifications if item.selector == _WOLF_PARENT)
    assert parent.kind is QualificationKind.CONDITION
    # the opposing creature in the trigger decides nothing about who receives the token
    assert QualificationKind.PARTY not in {item.kind for item in source.qualifications}


def test_creation_subtype_contradictions_keep_no_projection() -> None:
    """The audited token-subtype negatives stay contradictions however the target would bind."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    for suffix in (_WOLF_ANTHEM, _ELF_OUTLET, _COMPANY_ANTHEM, _LOOKOUT_ANTHEM, _LOOKOUT_OUTLET):
        _without_projection(
            compilation,
            artifact,
            suffix,
            RelationshipConversionOutcome.CONTRADICTION,
            "token_subtype_contradiction",
        )


def test_variable_dwarf_creation_keeps_the_printed_x() -> None:
    """At the Door's variable X stays printed, so the pair against Scrounger stays qualified."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _VARIABLE_DWARVES)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    source = projection.source
    assert source.prerequisites == ()
    assert {item.selector for item in source.qualifications} == {
        _VARIABLE_INSTRUCTION,
        "(Then exile this card. You may cast the enchantment later from exile.)",
    }


def test_elf_tokens_never_reach_the_bear_anthem_without_beorns_conversion() -> None:
    """An Elf source reaches Beorn only through his own same-face type conversion."""
    artifact = _confirmation()
    relationship = _row(_compile(artifact), _CHAPTER_BEAR)
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert projection.outcome.value == "qualified"
    target = projection.target
    assert (target.card_id, target.capability_id) == (103499, _BEORN_CAPABILITY)
    conversions = [
        item
        for item in target.qualifications
        if item.kind is QualificationKind.CONDITION and item.selector.startswith(_BEORN_CONVERSION)
    ]
    assert len(conversions) == 1


def test_mill_then_return_grammar_recovers_the_adventure_recursion_rows() -> None:
    """Both saved recursion rows recover from the closed mill-then-return instruction alone."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    for suffix in (_RECURSION_SCALING, _RECURSION_THRESHOLD):
        relationship = _row(compilation, suffix)
        projection = relationship.prerequisite_projection
        assert projection is not None
        assert projection.outcome.value == "qualified"
        source = projection.source
        assert (source.card_id, source.face_index) == (103546, 1)
        assert source.prerequisites == ()
        assert {item.selector for item in source.qualifications} == {
            _MILL_RETURN_INSTRUCTION,
            _ADVENTURE_SEQUENCE,
        }
        # the Adventure face never acquires a landfall clause it does not print
        assert "Whenever a land you control enters" not in source.qualifications[0].evidence.quote
        assert all(item.evidence.face_index == 1 for item in source.qualifications)
        assert source.card_id in relationship.participants
        _revalidate(artifact, relationship)


def test_bard_replacement_wording_declares_no_creation_family() -> None:
    """A token-replacement instruction redirects another creator and stays an accounted defect."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    relationship = _row(compilation, _BARD_REPLACEMENT)
    assert relationship.prerequisite_projection is None
    conversion = _conversion(compilation, _BARD_REPLACEMENT)
    assert conversion.outcome is RelationshipConversionOutcome.UNSUPPORTED
    assert conversion.reason == "token_replacement_requires_separate_source"


def test_actual_token_sources_gain_deterministic_replacement_support() -> None:
    """Actual creators point to Bard while Army growth and Bard supply stay excluded."""
    artifact = _confirmation()
    database = CardDatabase(cards={card.grp_id: card for card in _cards()})

    first = compile_token_replacement_relationships(
        artifact=artifact,
        card_database=database,
    )
    second = compile_token_replacement_relationships(
        artifact=artifact,
        card_database=database,
    )

    assert first
    assert first == second
    assert all(item.mechanism == "token-source-replacement" for item in first)
    assert all(item.prerequisite_projection is not None for item in first)
    projections = tuple(item.prerequisite_projection for item in first)
    assert all(item is not None for item in projections)
    assert all(item.source.role.value == "token_maker" for item in projections if item)
    assert all(item.target.card_id == 103524 for item in projections if item)
    assert all(item.target.role.value == "token_replacement" for item in projections if item)
    assert all(item.source.card_id != 103524 for item in projections if item)
    assert all(item.source.card_id != 103442 for item in projections if item)
    assert {
        qualification.selector
        for projection in projections
        if projection is not None
        for qualification in projection.target.qualifications
    } == {
        "If one or more tokens would be created under your control",
        "twice that many",
        "created instead",
    }
