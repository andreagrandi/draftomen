"""Compile source-bound projections without changing the confirmed input."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.semantic_capability_records import (
    CapabilityAction,
    CapabilityPrerequisite,
    CapabilityQuantity,
    CapabilityZone,
    CardCapability,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import (
    SemanticEnrichmentArtifact,
    _reject_constant,
    _strict_object,
    card_source_sha256,
)
from draftomen.semantic_enrichment_records import (
    CardSourcePin,
    OracleEvidence,
    OracleFact,
    SemanticEnrichmentError,
)
from draftomen.semantic_relationship_records import (
    CardRelationship,
    PrerequisiteProjectionError,
    RelationshipParticipant,
    RelationshipPrerequisite,
    RelationshipPrerequisiteProjection,
    RelationshipTiming,
    RelationshipZone,
    _CARD_TYPE_WORDS,
    _CHECKED_TIMING_PHRASES,
    _COLORS,
    _COLOR_WORDS,
    _COUNT_WORDS,
    _OBJECT_KIND_WORDS,
    _OPERATION_LEXEMES,
    _OPERATION_TRANSITIONS,
    _clause_binding,
    _clause_window,
    _recognized_timing_window,
    _stated_expression,
    _stated_zones,
    _subtype_atoms,
    _subtype_forms,
    _validate_clause_source,
    validate_prerequisite_sources,
    validate_relationship_pins,
    validate_relationship_sources,
)
from draftomen.semantic_roles import Role
from draftomen.set_enrichment_candidates import ROLE_COMPATIBILITY_RULES
from draftomen.set_enrichment_extraction import (
    _canonical_evidence,
    _participant_oracle_text,
    _projection_evidence,
    _relationship_participant,
)

__all__ = ["compile_confirmed_relationship_projections"]

_LOCAL_RUN_PREFIX = "local-"
_LOCAL_SEED_LENGTH = 16
_LOCAL_SEED_CHARACTERS = frozenset("0123456789abcdef")
_LOCAL_SUBJECT = re.compile(
    r"relationship:(?P<mechanism>[a-z][a-z-]*):"
    r"(?P<source_card>\d+):(?P<source_capability>[^:]+):"
    r"(?P<target_card>\d+):(?P<target_capability>[^:]+)\Z"
)
_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_COUNT_TOKEN_PATTERN = re.compile(r"\d+|[A-Za-z][A-Za-z'’-]*")
_POWER_TOUGHNESS_PATTERN = re.compile(r"[+-]?\d+\s*/\s*[+-]?\d+")
_BRACE_PATTERN = re.compile(r"\{[^}]*\}")
_OTHER_WORD_PATTERN = re.compile(r"\b(?:other|another)\b", re.IGNORECASE)
_COLORLESS_PATTERN = re.compile(r"\bcolorless\b", re.IGNORECASE)
_TRAILING_CONDITION_PATTERN = re.compile(
    r"\b(?:if|unless|when(?:ever)?|as\s+long\s+as|while|during)\b", re.IGNORECASE
)
_SUBTYPE_SEQUENCE_PATTERN = re.compile(
    r"\b((?:[A-Z][a-z]{2,}\s+)+)(?:"
    + "|".join(sorted(_CARD_TYPE_WORDS | _OBJECT_KIND_WORDS))
    + r")\b"
)

# The closed object grammar: a run continues while its words are object vocabulary and the text
# between them is number, power/toughness, possessive or list punctuation.
_RUN_CONNECTORS = frozenset(" ,/+'-\u2019" + "0123456789")
_DIGITS = "0123456789"
_DETERMINER_WORDS = frozenset(
    {"a", "an", "the", "another", "other", "each", "all", "target", "this", "that", "both", "any", "its"}
)
_COUNT_MODIFIER_WORDS = frozenset(
    {"up", "to", "at", "least", "more", "or", "and", "fewer", "than", "half", "plus"}
)
_PARTY_WORDS = frozenset(
    {
        "you",
        "your",
        "opponent",
        "opponents",
        "opponent's",
        "control",
        "controls",
        "controlled",
        "controlling",
        "own",
        "owns",
        "owned",
    }
)
_PERMANENT_TYPES = frozenset(
    {"artifact", "battle", "creature", "enchantment", "kindred", "land", "planeswalker"}
)
_NONPERMANENT_TYPES = frozenset({"instant", "sorcery"})

# A clause's own kind already carries its leading connective, so a citation may state one signal
# phrase of that kind before the clause window.  Text in front of the signal is the frame the clause
# hangs on and is not part of the predicate; text between the signal and the window is the predicate
# itself, and the record can only carry it if the clause window covers it.  The vocabularies are
# closed, so an unlisted connective fails closed instead of binding a clause to a condition the
# typed record cannot express.
_SIGNAL_PHRASES: Mapping[PrerequisiteKind, tuple[str, ...]] = {
    PrerequisiteKind.TRIGGER: (
        "at the beginning of",
        "at the end of",
        "whenever",
        "when",
        "each",
    ),
    PrerequisiteKind.COST: (
        "as an additional cost to cast this spell",
        "as an additional cost to cast",
        "as an additional cost",
        "as you cast",
    ),
    PrerequisiteKind.CONDITION: (
        "as long as",
        "so long as",
        "only if",
        "unless",
        "if",
        "while",
        "during",
        "there are",
        "there is",
    ),
    PrerequisiteKind.THRESHOLD: ("as long as", "there are", "there is"),
}
_SIGNAL_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(
        sorted(
            {phrase for phrases in _SIGNAL_PHRASES.values() for phrase in phrases},
            key=len,
            reverse=True,
        )
    )
    + r")\b",
    re.IGNORECASE,
)
# Connectives that may stand between a signal and the clause window: the copula, articles and the
# quantifier frame of a counted state ("there are seven or more cards in your graveyard").
_SIGNAL_GLUE = frozenset(
    {
        "there",
        "are",
        "is",
        "the",
        "a",
        "an",
        "of",
        "at",
        "least",
        "more",
        "less",
        "than",
        "or",
        "and",
        "up",
        "to",
        "half",
    }
)
# Labels of the record's own kinds may prefix a citation ("Threshold — ...") as frame text.
_KIND_LABELS = frozenset(kind.value for kind in PrerequisiteKind)
# The copula frame of a counted state, which extends the connective that introduces it.
_COPULA_SIGNALS = frozenset({"there are", "there is"})
_COUNTED_STATE_PATTERN = re.compile(r"\bthere (?:are|is)\b", re.IGNORECASE)
_FACT_CLAIM_KEYS = frozenset({"card_id", "evidence", "finding_id", "review", "run_id"})
_ROLE_LINKS = {link.mechanism: link for link in ROLE_COMPATIBILITY_RULES}


@dataclass(frozen=True, slots=True)
class _LocalPair:
    """The exact enabler-to-payoff pair one local relationship's finding id names."""

    mechanism: str
    source_card_id: int
    source_capability_id: str
    target_card_id: int
    target_capability_id: str


@dataclass(frozen=True, slots=True)
class _ParticipantNames:
    """The identity fields the shared clause checks read from one participant."""

    card_id: int
    card_name: str
    face_name: str | None


def compile_confirmed_relationship_projections(
    *,
    artifact: SemanticEnrichmentArtifact,
    card_database: CardDatabase,
) -> tuple[CardRelationship, ...]:
    """Compile eligible projections while preserving confirmed rows and source identity."""
    if not isinstance(artifact, SemanticEnrichmentArtifact):
        raise SemanticEnrichmentError("artifact must be a SemanticEnrichmentArtifact record.")
    if not isinstance(card_database, CardDatabase):
        raise SemanticEnrichmentError("card_database must be a CardDatabase record.")
    facts = _capability_facts(artifact)
    pins = {pin.card_id: pin for pin in artifact.cards}
    projected: list[CardRelationship] = []
    for relationship in artifact.confirmed_relationships:
        if relationship.prerequisite_projection is not None:
            projected.append(relationship)
            continue
        compiled = _compile_relationship(
            relationship=relationship,
            facts=facts,
            pins=pins,
            cards=card_database.cards,
        )
        projected.append(relationship if compiled is None else compiled)
    return tuple(projected)


def _capability_facts(artifact: SemanticEnrichmentArtifact) -> Mapping[tuple[int, str], CardCapability]:
    """Index every strict v2 capability fact the artifact stores, excluding ambiguous identities."""
    facts: dict[tuple[int, str], CardCapability] = {}
    ambiguous: set[tuple[int, str]] = set()
    for fact in artifact.oracle_facts:
        key = (fact.card_id, _capability_finding_id(fact.finding_id))
        if key in ambiguous:
            continue
        capability = _decode_capability(fact)
        existing = facts.get(key)
        if existing is None:
            if capability is None:
                ambiguous.add(key)
            else:
                facts[key] = capability
            continue
        if capability is None or capability.to_json() != existing.to_json():
            ambiguous.add(key)
            facts.pop(key)
    return facts


def _capability_finding_id(finding_id: str) -> str:
    """Return the capability's own finding id from its namespaced artifact fact id."""
    return finding_id.rsplit(":", 1)[-1]


def _decode_capability(fact: OracleFact) -> CardCapability | None:
    """Decode one stored fact as its strict v2 capability, or None when it is not one."""
    claim = _claim_object(fact.claim)
    if claim is None or _FACT_CLAIM_KEYS & claim.keys():
        return None
    payload = dict(claim)
    payload.update(
        {
            "finding_id": _capability_finding_id(fact.finding_id),
            "card_id": fact.card_id,
            "evidence": [item.to_json() for item in fact.evidence],
            "review": fact.review.to_json(),
            "run_id": fact.run_id,
        }
    )
    try:
        return CardCapability.from_json(payload)
    except SemanticEnrichmentError:
        return None


def _claim_object(claim: str) -> Mapping[str, Any] | None:
    """Decode a strict JSON object, rejecting duplicate keys and non-finite constants."""
    try:
        value = json.loads(claim, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (SemanticEnrichmentError, ValueError):
        return None
    if not isinstance(value, Mapping):
        return None
    return value


def _local_pair(relationship: CardRelationship) -> _LocalPair | None:
    """Parse the exact local subject of one stored relationship, or None when it is not local."""
    prefix = f"{relationship.run_id}:"
    if not relationship.finding_id.startswith(prefix):
        return None
    seed = relationship.run_id.removeprefix(_LOCAL_RUN_PREFIX)
    if len(seed) != _LOCAL_SEED_LENGTH or any(
        character not in _LOCAL_SEED_CHARACTERS for character in seed
    ):
        return None
    match = _LOCAL_SUBJECT.fullmatch(relationship.finding_id[len(prefix) :])
    if match is None:
        return None
    pair = _LocalPair(
        mechanism=match.group("mechanism"),
        source_card_id=int(match.group("source_card")),
        source_capability_id=match.group("source_capability"),
        target_card_id=int(match.group("target_card")),
        target_capability_id=match.group("target_capability"),
    )
    if pair.mechanism != relationship.mechanism:
        return None
    if pair.source_card_id == pair.target_card_id:
        return None
    if {pair.source_card_id, pair.target_card_id} != set(relationship.participants):
        return None
    return pair


def _compile_relationship(
    *,
    relationship: CardRelationship,
    facts: Mapping[tuple[int, str], CardCapability],
    pins: Mapping[int, CardSourcePin],
    cards: Mapping[int, CardInfo],
) -> CardRelationship | None:
    """Compile, fully validate and return one projected relationship, or None when it is ineligible."""
    pair = _local_pair(relationship)
    if pair is None:
        return None
    link = _ROLE_LINKS.get(pair.mechanism)
    if link is None:
        return None
    source = facts.get((pair.source_card_id, pair.source_capability_id))
    target = facts.get((pair.target_card_id, pair.target_capability_id))
    if source is None or target is None:
        return None
    if source.role is not link.enabler or target.role is not link.payoff:
        return None
    source_card = cards.get(source.card_id)
    target_card = cards.get(target.card_id)
    if source_card is None or target_card is None or source_card.unknown or target_card.unknown:
        return None
    source_participant = _compile_participant(capability=source, other=target, card=source_card)
    if source_participant is None:
        return None
    target_participant = _compile_participant(capability=target, other=source, card=target_card)
    if target_participant is None:
        return None
    try:
        projection = RelationshipPrerequisiteProjection(
            source=source_participant,
            target=target_participant,
        )
        _validate_projection(
            projection=projection,
            source=source,
            target=target,
            source_card=source_card,
            target_card=target_card,
        )
        return _projected_relationship(relationship=relationship, projection=projection, pins=pins, cards=cards)
    except (SemanticEnrichmentError, PrerequisiteProjectionError):
        return None


def _compile_participant(
    *,
    capability: CardCapability,
    other: CardCapability,
    card: CardInfo,
) -> RelationshipParticipant | None:
    """Compile every clause one participant may declare, or None when it cannot bind them all."""
    lines = _participant_oracle_text(capability, card).split("\n")
    if capability.prerequisites:
        clauses: list[RelationshipPrerequisite] = []
        for index, prerequisite in enumerate(capability.prerequisites):
            clause = _compile_bound_clause(
                capability=capability,
                other=other,
                prerequisite=prerequisite,
                index=index,
                lines=lines,
            )
            if clause is None:
                return None
            clauses.append(clause)
    else:
        clause = _compile_effect_clause(capability=capability, other=other, lines=lines)
        if clause is None:
            return None
        clauses = [clause]
    try:
        return _relationship_participant(capability=capability, card=card, clauses=tuple(clauses))
    except SemanticEnrichmentError:
        return None


def _compile_bound_clause(
    *,
    capability: CardCapability,
    other: CardCapability,
    prerequisite: CapabilityPrerequisite,
    index: int,
    lines: Sequence[str],
) -> RelationshipPrerequisite | None:
    """Compile the clause one retained coarse prerequisite binds, or None when it cannot bind one."""
    quote = prerequisite.evidence.quote
    containing = [line for line in lines if quote in line]
    if len(containing) != 1:
        return None
    paragraph = containing[0]
    if not any(item.quote in paragraph for item in capability.evidence):
        return None
    spans = _occurrences(paragraph, quote)
    if len(spans) != 1:
        return None
    return _compile_clause(
        capability=capability,
        other=other,
        paragraph=paragraph,
        quote_span=spans[0],
        bound_start=spans[0][0],
        kind=prerequisite.kind,
        indices=(index,),
        prerequisite=prerequisite,
    )


def _compile_effect_clause(
    *,
    capability: CardCapability,
    other: CardCapability,
    lines: Sequence[str],
) -> RelationshipPrerequisite | None:
    """Compile the clause a capability declares without any coarse prerequisite, or None."""
    accepted: set[RelationshipPrerequisite] = set()
    for evidence in capability.evidence:
        containing = [line for line in lines if evidence.quote in line]
        if len(containing) != 1:
            continue
        paragraph = containing[0]
        for span in _occurrences(paragraph, evidence.quote):
            clause = _compile_clause(
                capability=capability,
                other=other,
                paragraph=paragraph,
                quote_span=span,
                bound_start=span[0],
                kind=PrerequisiteKind.CONDITION,
                indices=(),
                prerequisite=None,
            )
            if clause is not None:
                accepted.add(clause)
    if len(accepted) != 1:
        return None
    return next(iter(accepted))


def _compile_clause(
    *,
    capability: CardCapability,
    other: CardCapability,
    paragraph: str,
    quote_span: tuple[int, int],
    bound_start: int,
    kind: PrerequisiteKind,
    indices: tuple[int, ...],
    prerequisite: CapabilityPrerequisite | None,
) -> RelationshipPrerequisite | None:
    """Compile one source-bound clause only when its cited predicate is fully represented."""
    counted_state = _counted_state_predicate(
        capability=capability,
        prerequisite=prerequisite,
        paragraph=paragraph,
        bound_start=bound_start,
    )
    operation = counted_state
    if operation is None:
        operation = _declared_operation(
            capability=capability,
            quote=paragraph[quote_span[0] : quote_span[1]],
        )
    if operation is None:
        return None
    regions = _evidence_regions(paragraph=paragraph, capability=capability, prerequisite=prerequisite)
    operation_spans = (
        ()
        if counted_state is not None
        else tuple(
            span
            for span in _operation_spans(paragraph, operation.value)
            if any(region[0] <= span[0] and span[1] <= region[1] for region in regions)
        )
    )
    accepted: list[RelationshipPrerequisite] = []
    for object_span in _object_phrase_runs(
        paragraph=paragraph,
        regions=regions,
        capability=capability,
        other=other,
    ):
        for operation_span in (object_span,) if counted_state is not None else operation_spans:
            if not _adjacent(paragraph, operation_span, object_span):
                continue
            if counted_state is not None and set(
                _stated_counts(paragraph[object_span[0] : object_span[1]])
            ) != _declared_counts(prerequisite):
                continue
            if not _represents_citation(
                kind=kind,
                paragraph=paragraph,
                bound_start=bound_start,
                window_start=min(operation_span[0], object_span[0]),
            ):
                continue
            clause = _assemble_clause(
                capability=capability,
                other=other,
                prerequisite=prerequisite,
                kind=kind,
                indices=indices,
                paragraph=paragraph,
                operation=operation,
                operation_span=operation_span,
                object_span=object_span,
            )
            if clause is not None:
                accepted.append(clause)
    distinct = set(_maximal_clauses(accepted))
    if len(distinct) != 1:
        return None
    return next(iter(distinct))


def _counted_state_predicate(
    *,
    capability: CardCapability,
    prerequisite: CapabilityPrerequisite | None,
    paragraph: str,
    bound_start: int,
) -> CapabilityAction | None:
    """Recognize a counted state backed by the retained prerequisite's quantity and zone."""
    if prerequisite is None or capability.action is not CapabilityAction.OTHER:
        return None
    if prerequisite.quantity is None:
        return None
    if prerequisite.source_zone is None and prerequisite.destination_zone is None:
        return None
    if _COUNTED_STATE_PATTERN.search(paragraph[bound_start:]) is None:
        return None
    return CapabilityAction.COUNT


def _declared_counts(prerequisite: CapabilityPrerequisite | None) -> set[tuple[int, QuantityRelation]]:
    """Return the count the retained prerequisite states, as the quantity check reads it."""
    if prerequisite is None or prerequisite.quantity is None:
        return set()
    return {(prerequisite.quantity.value, prerequisite.quantity.relation)}


def _represents_citation(
    *,
    kind: PrerequisiteKind,
    paragraph: str,
    bound_start: int,
    window_start: int,
) -> bool:
    """Require each cited leading predicate to be represented by its typed clause."""
    prefix = paragraph[bound_start:window_start]
    if not prefix.strip(" \t,;-\u2014"):
        return True
    signals = [
        match
        for match in _SIGNAL_PATTERN.finditer(prefix)
        if match.group(0).casefold() in _SIGNAL_PHRASES[kind]
    ]
    if not signals:
        return False
    effective = signals[-1]
    if effective.group(0).casefold() in _COPULA_SIGNALS:
        if len(signals) > 2:
            return False
        if len(signals) == 2 and not _glue_only(prefix[signals[0].end() : effective.start()]):
            return False
    elif len(signals) > 1:
        return False
    return _glue_only(prefix[effective.end() :])


def _glue_only(text: str) -> bool:
    """Return whether one span holds nothing but the closed connective vocabulary."""
    return all(
        match.group(0).casefold() in _SIGNAL_GLUE for match in _WORD_PATTERN.finditer(text)
    )


def _declared_operation(*, capability: CardCapability, quote: str) -> CapabilityAction | None:
    """Return the operation one clause may declare, or None when the cited text leaves it open."""
    if capability.action is not CapabilityAction.OTHER:
        return capability.action
    stated = {
        _OPERATION_LEXEMES[match.group(0).casefold()]
        for match in _WORD_PATTERN.finditer(quote)
        if match.group(0).casefold() in _OPERATION_LEXEMES
    }
    if len(stated) != 1:
        return None
    return CapabilityAction(next(iter(stated)))


def _assemble_clause(
    *,
    capability: CardCapability,
    other: CardCapability,
    prerequisite: CapabilityPrerequisite | None,
    kind: PrerequisiteKind,
    indices: tuple[int, ...],
    paragraph: str,
    operation: CapabilityAction,
    operation_span: tuple[int, int],
    object_span: tuple[int, int],
) -> RelationshipPrerequisite | None:
    """Build one typed clause from a fixed operation and object span of its cited line."""
    operation_quote = paragraph[operation_span[0] : operation_span[1]]
    object_quote = paragraph[object_span[0] : object_span[1]]
    window, _ = _clause_window(paragraph=paragraph, operation_span=operation_span, object_span=object_span)
    selected_end = max(operation_span[1], object_span[1])
    window_start = min(operation_span[0], object_span[0])
    if _TRAILING_CONDITION_PATTERN.search(window[selected_end - window_start :]):
        return None
    required_card_id = _selected_card_id(
        phrase=_identity_phrase(paragraph=paragraph, object_span=object_span),
        capability=capability,
        other=other,
    )
    subject = _declared_subject(
        capability=capability,
        operation=operation,
        required_card_id=required_card_id,
    )
    card_types, type_operator = _decoded_types(object_quote)
    retained_types = tuple(item.value for item in capability.qualifier.card_types)
    if retained_types and set(retained_types) != set(card_types):
        return None
    object_kind = _decoded_kind(object_quote, card_types)
    if object_kind is None:
        return None
    token_restriction = _decoded_token_restriction(object_quote)
    retained_restriction = capability.qualifier.token_restriction.value
    if retained_restriction != "unrestricted" and retained_restriction != token_restriction:
        return None
    color_operator, colors = _decoded_colors(
        phrase=object_quote,
        subject=subject,
        object_kind=object_kind,
    )
    controller, owner = _decoded_party(object_quote)
    exclusion = "ability_source" if _OTHER_WORD_PATTERN.search(window) is not None else "none"
    subtype, usable = _decoded_subtype(window=window, capability=capability, other=other)
    if not usable:
        return None
    quantity, usable = _declared_quantity(
        phrase=object_quote,
        prerequisite=prerequisite,
        capability=capability,
    )
    if not usable:
        return None
    source_zone, destination_zone, usable = _merged_zones(
        operation=operation.value,
        window=window,
        prerequisite=prerequisite,
    )
    if not usable:
        return None
    timing = _declared_timing(paragraph)
    if timing is None:
        return None
    retained_window = _recognized_timing_window(
        None if prerequisite is None else prerequisite.timing
    )
    if retained_window is not None and retained_window != timing.window:
        return None
    try:
        clause = RelationshipPrerequisite(
            kind=kind,
            subject=subject,
            operation=operation.value,
            object_kind=object_kind,
            card_types=card_types,
            type_operator=type_operator,
            token_restriction=token_restriction,
            exclusion=exclusion,
            subtype=subtype,
            color_operator=color_operator,
            colors=colors,
            controller=controller,
            owner=owner,
            quantity=quantity,
            source_zone=source_zone,
            destination_zone=destination_zone,
            timing=timing,
            required_card_id=required_card_id,
            evidence=OracleEvidence(
                card_id=capability.card_id,
                face_index=capability.face_index,
                quote=paragraph,
            ),
            operation_quote=operation_quote,
            operation_occurrence=_occurrences(paragraph, operation_quote).index(operation_span),
            object_quote=object_quote,
            object_occurrence=_occurrences(paragraph, object_quote).index(object_span),
            capability_prerequisite_indices=indices,
        )
    except SemanticEnrichmentError:
        return None
    try:
        _validate_clause_source(
            clause=clause,
            paragraph=paragraph,
            operation_span=operation_span,
            object_span=object_span,
            participant=_named(capability),
            other=_named(other),
        )
    except PrerequisiteProjectionError:
        return None
    return clause


def _projected_relationship(
    *,
    relationship: CardRelationship,
    projection: RelationshipPrerequisiteProjection,
    pins: Mapping[int, CardSourcePin],
    cards: Mapping[int, CardInfo],
) -> CardRelationship:
    """Return one stored relationship with its compiled projection and unioned evidence."""
    projected = CardRelationship(
        finding_id=relationship.finding_id,
        mechanism=relationship.mechanism,
        participants=relationship.participants,
        claim=relationship.claim,
        prerequisites=relationship.prerequisites,
        oracle_evidence=_canonical_evidence(
            relationship.oracle_evidence + _projection_evidence(projection=projection)
        ),
        guide_evidence=relationship.guide_evidence,
        review=relationship.review,
        run_id=relationship.run_id,
        prerequisite_projection=projection,
    )
    validate_relationship_pins(relationship=projected, pins=pins)
    validate_relationship_sources(relationship=projected, cards=cards, pins=pins)
    return projected


def _validate_projection(
    *,
    projection: RelationshipPrerequisiteProjection,
    source: CardCapability,
    target: CardCapability,
    source_card: CardInfo,
    target_card: CardInfo,
) -> None:
    """Re-prove one compiled projection against the pinned Oracle text it quotes."""
    oracle_text = {
        (source.card_id, source.face_index): _participant_oracle_text(source, source_card),
        (target.card_id, target.face_index): _participant_oracle_text(target, target_card),
    }
    participant_evidence = {
        (source.card_id, source.face_index): source.evidence,
        (target.card_id, target.face_index): target.evidence,
    }
    validate_prerequisite_sources(
        projection=projection,
        oracle_text=oracle_text,
        participant_evidence=participant_evidence,
    )


def _maximal_clauses(clauses: Sequence[RelationshipPrerequisite]) -> list[RelationshipPrerequisite]:
    """Keep only the clauses whose object phrase no other accepted clause contains."""
    spans = [(_clause_binding(clause)[1], clause) for clause in clauses]
    return [
        clause
        for span, clause in spans
        if not any(
            other[0] <= span[0] and span[1] <= other[1] and other != span
            for other, _ in spans
        )
    ]


def _evidence_regions(
    *,
    paragraph: str,
    capability: CardCapability,
    prerequisite: CapabilityPrerequisite | None,
) -> tuple[tuple[int, int], ...]:
    """Locate the exact spans cited by the capability and its retained prerequisite."""
    regions: list[tuple[int, int]] = []
    quotes = [item.quote for item in capability.evidence]
    if prerequisite is not None:
        quotes.append(prerequisite.evidence.quote)
    for quote in quotes:
        regions.extend(_occurrences(paragraph, quote))
    return tuple(regions)


def _object_phrase_runs(
    *,
    paragraph: str,
    regions: Sequence[tuple[int, int]],
    capability: CardCapability,
    other: CardCapability,
) -> tuple[tuple[int, int], ...]:
    """Return every maximal object-grammar run inside the cited regions of one line."""
    allowed = _allowed_words(capability=capability, other=other)
    words = [
        match
        for match in _WORD_PATTERN.finditer(paragraph)
        if any(region[0] <= match.start() and match.end() <= region[1] for region in regions)
    ]
    runs: list[tuple[int, int]] = []
    start: int | None = None
    previous_end: int | None = None
    for match in words:
        if start is not None and previous_end is not None:
            if any(character not in _RUN_CONNECTORS for character in paragraph[previous_end : match.start()]):
                runs.append((start, previous_end))
                start = None
        if match.group(0).casefold() in allowed or _subtype_atom(paragraph, match):
            start = match.start() if start is None else start
            previous_end = match.end()
        else:
            if start is not None and previous_end is not None:
                runs.append((start, previous_end))
            start = None
            previous_end = None
    if start is not None and previous_end is not None:
        runs.append((start, previous_end))
    kept: list[tuple[int, int]] = []
    for left, right in runs:
        while left < right and paragraph[left] in " ,":
            left += 1
        while right > left and paragraph[right - 1] in " ,":
            right -= 1
        text = paragraph[left:right]
        if re.search(r"[A-Za-z]", text) is None or text[:1].casefold() in _DIGITS:
            continue
        first = _WORD_PATTERN.match(text)
        if first is not None and first.group(0).casefold() in _OPERATION_LEXEMES:
            continue
        if not any(
            match.group(0).casefold() in (_CARD_TYPE_WORDS | _OBJECT_KIND_WORDS | _COLOR_WORDS)
            for match in _WORD_PATTERN.finditer(text)
        ):
            continue
        kept.append((left, right))
    return tuple(kept)


def _subtype_atom(paragraph: str, match: re.Match[str]) -> bool:
    """Return whether one word is a capitalised subtype atom naming a following object type."""
    if not match.group(0)[:1].isupper():
        return False
    following = re.match(r"\s+([A-Za-z][A-Za-z'’-]*)", paragraph[match.end() :])
    if following is None:
        return False
    return following.group(1).casefold() in (_CARD_TYPE_WORDS | _OBJECT_KIND_WORDS)


def _allowed_words(*, capability: CardCapability, other: CardCapability) -> frozenset[str]:
    """Return the closed object-grammar vocabulary of one participant."""
    words = set(_COUNT_WORDS) | set(_COLOR_WORDS) | set(_OBJECT_KIND_WORDS) | set(_CARD_TYPE_WORDS)
    words |= _DETERMINER_WORDS | _COUNT_MODIFIER_WORDS | _PARTY_WORDS
    if capability.qualifier.subtype is not None:
        words |= set(capability.qualifier.subtype.split())
        for form in _subtype_forms(capability.qualifier.subtype):
            words |= set(form.split())
    for participant in (capability, other):
        for name in (participant.card_name, participant.face_name):
            if name is not None:
                words |= {part.casefold() for part in name.split()}
    return frozenset(words)


def _adjacent(paragraph: str, first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Return whether two spans touch or nest without intervening words."""
    if first[0] >= second[0] and first[1] <= second[1]:
        return True
    if second[0] >= first[0] and second[1] <= first[1]:
        return True
    left, right = sorted((first, second))
    between = paragraph[left[1] : right[0]]
    return all(character in " \t,;" for character in between)


def _occurrences(paragraph: str, selector: str) -> tuple[tuple[int, int], ...]:
    """Return the non-overlapping occurrences of one selector as the validators resolve them."""
    if not selector:
        return ()
    spans: list[tuple[int, int]] = []
    position = paragraph.find(selector)
    while position >= 0:
        spans.append((position, position + len(selector)))
        position = paragraph.find(selector, position + len(selector))
    return tuple(spans)


def _operation_spans(paragraph: str, operation: str) -> tuple[tuple[int, int], ...]:
    """Return every occurrence of a lexeme that denotes one declared operation."""
    return tuple(
        (match.start(), match.end())
        for match in _WORD_PATTERN.finditer(paragraph)
        if _OPERATION_LEXEMES.get(match.group(0).casefold()) == operation
    )


def _identity_phrase(*, paragraph: str, object_span: tuple[int, int]) -> str:
    """Return the selected object phrase plus any adjoining self-identity qualifier."""
    prefix = paragraph[: object_span[0]].rstrip()
    start = object_span[0]
    for qualifier in ("this card", "this creature", "this"):
        if prefix.casefold().endswith(qualifier):
            start = len(prefix) - len(qualifier)
            break
    return paragraph[start : object_span[1]]


def _selected_card_id(*, phrase: str, capability: CardCapability, other: CardCapability) -> int | None:
    """Decode which participant one object phrase names, or none when it names neither."""
    self_named = any(name in phrase for name in _known_names(capability))
    self_named = self_named or "this card" in phrase.casefold() or "this creature" in phrase.casefold()
    other_named = any(name in phrase for name in _known_names(other))
    if self_named and not other_named:
        return capability.card_id
    if other_named and not self_named:
        return other.card_id
    return None


def _known_names(capability: CardCapability) -> tuple[str, ...]:
    """Return the canonical and selected-face names of one capability."""
    names = [capability.card_name]
    if capability.face_name is not None:
        names.append(capability.face_name)
    return tuple(names)


def _declared_subject(
    *,
    capability: CardCapability,
    operation: CapabilityAction,
    required_card_id: int | None,
) -> str:
    """Derive the clause subject from the declared role's own anchor shape."""
    if capability.role is Role.TOKEN_MAKER and operation is CapabilityAction.CREATE:
        return "output"
    if capability.role is Role.SACRIFICE_OUTLET and operation is CapabilityAction.SACRIFICE:
        return "input"
    if (
        capability.role is Role.SACRIFICE_FODDER
        and operation is CapabilityAction.SACRIFICE
        and required_card_id == capability.card_id
    ):
        return "participant"
    return "event"


def _decoded_types(phrase: str) -> tuple[tuple[str, ...], str]:
    """Decode stated card types and their operator from one phrase."""
    stated, connective = _stated_expression(phrase, _CARD_TYPE_WORDS)
    if not stated:
        return (), "unrestricted"
    ordered = tuple(sorted(set(stated)))
    if connective == "or":
        return ordered, "any_of"
    return ordered, "all_of"


def _decoded_kind(phrase: str, card_types: Sequence[str]) -> str | None:
    """Decode the object kind of one phrase, or None when the phrase states several."""
    kinds = {
        _OBJECT_KIND_WORDS[match.group(0).casefold()]
        for match in _WORD_PATTERN.finditer(phrase)
        if match.group(0).casefold() in _OBJECT_KIND_WORDS
    }
    if len(kinds) > 1:
        return None
    if kinds:
        return next(iter(kinds))
    if card_types and set(card_types) <= _PERMANENT_TYPES:
        return "permanent"
    if card_types and set(card_types) <= _NONPERMANENT_TYPES:
        return "spell"
    return "card"


def _decoded_colors(*, phrase: str, subject: str, object_kind: str) -> tuple[str, tuple[str, ...]]:
    """Decode stated colors and their operator from one phrase."""
    if _COLORLESS_PATTERN.search(phrase) is not None:
        return "exact", ()
    stated, connective = _stated_expression(phrase, _COLOR_WORDS)
    if not stated:
        return "unrestricted", ()
    ordered = tuple(sorted(set(stated), key=_COLORS.index))
    if subject == "output" and object_kind == "token":
        return "exact", ordered
    if connective == "or":
        return "any_of", ordered
    if len(ordered) > 1:
        return "all_of", ordered
    return "exact", ordered


def _decoded_token_restriction(phrase: str) -> str:
    """Decode the token restriction stated by one phrase."""
    if re.search(r"\bnon-?tokens?\b", phrase, re.IGNORECASE) is not None:
        return "nontoken"
    if re.search(r"\btokens?\b", phrase, re.IGNORECASE) is not None:
        return "token"
    return "unrestricted"


def _decoded_party(phrase: str) -> tuple[str, str]:
    """Decode stated control and ownership predicates of one phrase."""
    folded = phrase.casefold()
    controller = "any"
    if re.search(r"\byou control\b", folded) is not None:
        controller = "you"
    elif re.search(r"\b(?:an?\s+)?opponents?\s+controls?\b", folded) is not None:
        controller = "opponent"
    owner = "any"
    if re.search(r"\byou own\b", folded) is not None:
        owner = "you"
    elif re.search(r"\b(?:an?\s+)?opponents?\s+owns?\b", folded) is not None:
        owner = "opponent"
    return controller, owner


def _decoded_subtype(
    *,
    window: str,
    capability: CardCapability,
    other: CardCapability,
) -> tuple[str | None, bool]:
    """Return the subtype one clause may declare, and whether the pinned text supports it."""
    atoms = _subtype_atoms(window=window, participant=_named(capability), other=_named(other))
    declared = capability.qualifier.subtype
    if declared is None:
        if not atoms:
            return None, True
        if len(set(atoms)) != 1:
            return None, False
        declared = atoms[0]
    forms = _subtype_forms(declared)
    for match in _SUBTYPE_SEQUENCE_PATTERN.finditer(window):
        words = match.group(1).casefold().split()
        while words and (words[0] in _OPERATION_LEXEMES or words[0] in _DETERMINER_WORDS):
            words.pop(0)
        if words and " ".join(words) not in forms:
            return None, False
    for form in forms:
        if re.search(rf"\b{re.escape(form)}\b", window, re.IGNORECASE) is not None:
            return declared, True
    return None, False


def _declared_quantity(
    *,
    phrase: str,
    prerequisite: CapabilityPrerequisite | None,
    capability: CardCapability,
) -> tuple[CapabilityQuantity | None, bool]:
    """Return the quantity one clause may declare, and whether the text leaves it unambiguous."""
    retained = capability.quantity if prerequisite is None else prerequisite.quantity or capability.quantity
    stated = _stated_counts(phrase)
    if len(stated) > 1:
        return None, False
    if retained is not None:
        return retained, True
    if not stated:
        return None, True
    value, relation = stated[0]
    return CapabilityQuantity(value=value, relation=relation), True


def _stated_counts(phrase: str) -> tuple[tuple[int, QuantityRelation], ...]:
    """Return the counts the unchanged quantity check reads out of one phrase."""
    cleaned = _POWER_TOUGHNESS_PATTERN.sub(" ", _BRACE_PATTERN.sub(" ", phrase))
    tokens = list(_COUNT_TOKEN_PATTERN.finditer(cleaned))
    found: set[tuple[int, QuantityRelation]] = set()
    for index, match in enumerate(tokens):
        token = match.group(0).casefold()
        if token.isdigit():
            value = int(token)
        else:
            if token in {"a", "an"} and index + 1 < len(tokens):
                following = tokens[index + 1].group(0).casefold()
                if following in {"opponent", "opponents", "player", "players"}:
                    continue
            value = _COUNT_WORDS.get(token)
            if value is None:
                continue
        relation = QuantityRelation.EXACTLY
        previous = tokens[index - 1].group(0).casefold() if index > 0 else ""
        following_two = [
            tokens[position].group(0).casefold()
            for position in range(index + 1, min(index + 3, len(tokens)))
        ]
        if index >= 2 and previous == "least" and tokens[index - 2].group(0).casefold() == "at":
            relation = QuantityRelation.AT_LEAST
        elif following_two[:2] == ["or", "more"]:
            relation = QuantityRelation.AT_LEAST
        elif index >= 2 and previous == "to" and tokens[index - 2].group(0).casefold() == "up":
            relation = QuantityRelation.AT_MOST
        found.add((value, relation))
    return tuple(sorted(found))


def _merged_zones(
    *,
    operation: str,
    window: str,
    prerequisite: CapabilityPrerequisite | None,
) -> tuple[RelationshipZone | None, RelationshipZone | None, bool]:
    """Merge declared, retained and stated zones into one declared transition."""
    declared: dict[str, RelationshipZone] = {}
    expected_source, expected_destination = _OPERATION_TRANSITIONS.get(operation, (None, None))
    for expected, resolved in ((expected_source, "source"), (expected_destination, "destination")):
        if expected is None:
            continue
        zone, player = expected
        declared[resolved] = RelationshipZone(zone=zone, player=player or "any")
    retained = {
        "source": None if prerequisite is None else prerequisite.source_zone,
        "destination": None if prerequisite is None else prerequisite.destination_zone,
    }
    for resolved, zone in retained.items():
        if zone is None:
            continue
        current = declared.get(resolved)
        if current is not None and current.zone is not zone:
            return None, None, False
        declared.setdefault(resolved, RelationshipZone(zone=zone, player="any"))
    for zone, player, direction in _stated_zones(window):
        if direction is None:
            continue
        resolved = "source" if direction == "source" else "destination"
        current = declared.get(resolved)
        if current is not None and current.zone is not zone:
            return None, None, False
        declared[resolved] = RelationshipZone(
            zone=zone,
            player=player or (current.player if current is not None else "any"),
        )
    source_zone = declared.get("source")
    destination_zone = declared.get("destination")
    if operation == "return":
        if source_zone is None or destination_zone is None:
            return None, None, False
        if source_zone.zone is not CapabilityZone.GRAVEYARD:
            return None, None, False
    if operation in ("die", "sacrifice"):
        if source_zone is not None and source_zone.zone is not CapabilityZone.BATTLEFIELD:
            return None, None, False
    return source_zone, destination_zone, True


def _declared_timing(paragraph: str) -> RelationshipTiming | None:
    """Decode the closed timing window the unchanged timing check demands of one paragraph."""
    folded = paragraph.casefold()
    window = "unrestricted"
    turn = "any"
    max_per_turn: int | None = None
    for pattern, expected in _CHECKED_TIMING_PHRASES:
        if re.search(pattern, folded) is None:
            continue
        if expected == "once":
            max_per_turn = 1
        elif expected in ("your", "opponent"):
            turn = expected
        else:
            window = expected
        if expected == "upkeep" and re.search(r"\byour upkeep\b", folded) is not None:
            turn = "your"
    try:
        return RelationshipTiming(window=window, turn=turn, max_per_turn=max_per_turn)
    except SemanticEnrichmentError:
        return None


def _named(capability: CardCapability) -> _ParticipantNames:
    """Return the identity fields of one capability in the shape the clause checks read."""
    return _ParticipantNames(
        card_id=capability.card_id,
        card_name=capability.card_name,
        face_name=capability.face_name,
    )
