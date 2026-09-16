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
from collections.abc import Sequence
from pathlib import Path

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_relationship_projection import compile_confirmed_relationship_projections
from draftomen.semantic_enrichment import EnrichmentSources, SemanticEnrichmentArtifact
from draftomen.semantic_enrichment_records import OracleFact
from draftomen.semantic_relationship_records import (
    CardRelationship,
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


def _cards() -> tuple[CardInfo, ...]:
    """Return the pinned HOB and fixture cards the confirmation was built with."""
    return tuple(CardInfo.from_json(entry) for entry in _FIXTURE["cards"])


def _sources() -> EnrichmentSources:
    """Return the frozen card sources of the compact confirmation."""
    return EnrichmentSources(set_code="hob", cards=_cards(), guides=())


def _confirmation() -> SemanticEnrichmentArtifact:
    """Decode the compact confirmation through the artifact's own source-validating loader."""
    return SemanticEnrichmentArtifact.from_json(_FIXTURE["artifact"], sources=_sources())


def _compile(artifact: SemanticEnrichmentArtifact) -> tuple[CardRelationship, ...]:
    """Compile one artifact's confirmed projections against its own pinned cards."""
    return compile_confirmed_relationship_projections(
        artifact=artifact,
        card_database=CardDatabase(cards={card.grp_id: card for card in _cards()}),
    )


def _row(rows: Sequence[CardRelationship], suffix: str) -> CardRelationship:
    """Return the one stored row whose finding id ends with a local subject."""
    return next(row for row in rows if row.finding_id.endswith(suffix))


def _with_facts(
    artifact: SemanticEnrichmentArtifact,
    facts: tuple[OracleFact, ...],
) -> SemanticEnrichmentArtifact:
    """Rebuild the confirmation around a different stored fact set."""
    return dataclasses.replace(artifact, oracle_facts=facts, sources=_sources())


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
    stored = _row(artifact.confirmed_relationships, _MILL)
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


def test_trailing_condition_inside_the_action_never_yields_a_projection() -> None:
    """A mill instruction that states its own "if ..." condition states no clean effect."""
    artifact = _confirmation()
    stored = _row(artifact.confirmed_relationships, _TRAILING_MILL)
    relationship = _row(_compile(artifact), _TRAILING_MILL)
    assert relationship is stored
    assert relationship.prerequisite_projection is None


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
    stored = _row(artifact.confirmed_relationships, _ARMY_PAIR)
    assert stored.prerequisite_projection is None
    variant = _with_subtype(artifact, _ARMY_CAPABILITY, subtype)
    relationship = _row(_compile(variant), _ARMY_PAIR)
    assert relationship is _row(variant.confirmed_relationships, _ARMY_PAIR)
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
    stored = _row(artifact.confirmed_relationships, _AMASS)
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
    rows = _compile(stripped)
    for suffix in affected:
        relationship = _row(rows, suffix)
        assert relationship is _row(stripped.confirmed_relationships, suffix)
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
    assert relationship is _row(variant.confirmed_relationships, _MILL)
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
    rows = _compile(variant)
    assert not any(row.prerequisite_projection is not None for row in rows)
    assert all(
        row is stored
        for row, stored in zip(rows, variant.confirmed_relationships)
    )


def test_model_decided_relationship_is_returned_exactly_as_stored() -> None:
    """A relationship that is not this artifact's local pair matcher keeps its stored payload."""
    artifact = _confirmation()
    stored = _row(artifact.confirmed_relationships, _RECURSION)
    legacy = dataclasses.replace(
        stored,
        finding_id=f"relationship:{_RECURSION}",
        run_id=_SOURCE_RUN,
    )
    variant = _with_relationships(artifact, (legacy,))
    rows = _compile(variant)
    assert rows == variant.confirmed_relationships
    assert rows[0] is variant.confirmed_relationships[0]


def test_already_projected_relationship_is_returned_exactly_as_stored() -> None:
    """A projection that survives the artifact's own gates is retained without recompilation."""
    artifact = _confirmation()
    first = _compile(artifact)
    assert _row(first, _MILL).prerequisite_projection is not None
    # rebuilding the confirmation around the compiled rows re-validates every projection source
    variant = _with_relationships(artifact, first)
    rows = _compile(variant)
    stored = {row.finding_id: row for row in variant.confirmed_relationships}
    assert [row is stored[row.finding_id] for row in rows] == [True] * len(rows)
    assert _row(rows, _MILL).to_json() == _row(first, _MILL).to_json()


def test_compilation_preserves_the_artifact_bytes_and_stored_rows() -> None:
    """The compiler keeps stored order and leaves the confirmation it reads byte-identical."""
    artifact = _confirmation()
    before = artifact.to_bytes()
    rows = _compile(artifact)
    assert artifact.to_bytes() == before
    assert [row.finding_id for row in rows] == [
        row.finding_id for row in artifact.confirmed_relationships
    ]
    assert [row.prerequisite_projection is not None for row in rows] == [
        True,
        False,
        True,
        True,
        False,
        False,
    ]
    assert _row(rows, _AMASS) is _row(artifact.confirmed_relationships, _AMASS)
    assert all(
        "prerequisite_projection" not in stored for stored in json.loads(before)["relationships"]
    )
