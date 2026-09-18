"""Regressions for deterministic source-bound projections of confirmed local relationships.

The compiler is exercised against a compact confirmation extracted from the zero-cost saved HOB
run whose digest is ``edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84``: three
reviewed real pairs, their strict v2 capability facts, the pinned cards that bind them and the
real model runs behind them.  The same confirmation carries fixture-grammar pairs for the closed
guards this module defends - a trailing condition inside one action, a condition on the next
instruction only, and a retained token subtype that states one of the two explicit words.  The
fixture carries its own frozen sources, so these tests never read a home directory and never skip
when an operator's files are absent.
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
    compile_confirmed_relationship_projections,
)
from draftomen.semantic_enrichment import EnrichmentSources, SemanticEnrichmentArtifact
from draftomen.semantic_enrichment_records import OracleEvidence, OracleFact
from draftomen.semantic_relationship_records import (
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
def test_partial_or_derived_token_subtype_never_yields_a_projection(subtype: str | None) -> None:
    """A retained subtype must state the whole explicit "Goblin Army" sequence, or none."""
    artifact = _confirmation()
    stored = _stored(artifact, _ARMY_PAIR)
    assert stored.prerequisite_projection is None
    variant = _with_subtype(artifact, _ARMY_CAPABILITY, subtype)
    relationship = _row(_compile(variant), _ARMY_PAIR)
    assert relationship is _stored(variant, _ARMY_PAIR)
    assert relationship.prerequisite_projection is None


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


def test_unrepresentable_amass_antecedent_leaves_the_relationship_unprojected() -> None:
    """An amass reminder's "If you don't control an Army" antecedent has no typed field."""
    artifact = _confirmation()
    # both facts of the pair are present: the negative comes from the omitted antecedent alone
    facts = {(fact.card_id, fact.finding_id.rsplit(":", 1)[-1]) for fact in artifact.oracle_facts}
    assert (103442, "103442-amass-goblin-army-token") in facts
    assert (103381, "103381-go-wide-power") in facts
    stored = _stored(artifact, _AMASS)
    relationship = _row(_compile(artifact), _AMASS)
    assert relationship is stored
    assert relationship.prerequisite_projection is None


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
    rows = _compile(variant).relationships
    assert not any(row.prerequisite_projection is not None for row in rows)
    assert all(
        row is stored
        for row, stored in zip(rows, variant.confirmed_relationships)
    )


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
    # the recursion pair is rejected by the zone gate, so only the two mill pairs project
    assert [row.prerequisite_projection is not None for row in rows] == [
        True,
        False,
        True,
        False,
        False,
        False,
    ]
    assert _row(compilation, _AMASS) is _stored(artifact, _AMASS)
    assert all(
        "prerequisite_projection" not in stored for stored in json.loads(before)["relationships"]
    )


def test_every_stored_relationship_receives_one_conversion_in_stored_order() -> None:
    """The compilation accounts for each stored row once, with a closed outcome and reason."""
    artifact = _confirmation()
    compilation = _compile(artifact)
    assert len(artifact.confirmed_relationships) == 6
    assert [item.finding_id for item in compilation.conversions] == [
        row.finding_id for row in artifact.confirmed_relationships
    ]
    assert [(item.outcome.value, item.reason) for item in compilation.conversions] == [
        ("decoded", "projected"),
        ("unsupported", "source_clause_unbound"),
        ("decoded", "projected"),
        ("contradiction", "zone_supply_contradiction:graveyard"),
        ("unsupported", "target_clause_unbound"),
        ("unsupported", "source_clause_unbound"),
    ]
    assert [item.mechanism for item in compilation.conversions] == [
        row.mechanism for row in artifact.confirmed_relationships
    ]
    assert all(
        item.outcome in tuple(RelationshipConversionOutcome) for item in compilation.conversions
    )
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
        assert _conversion(compilation, suffix).outcome is RelationshipConversionOutcome.DECODED


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
