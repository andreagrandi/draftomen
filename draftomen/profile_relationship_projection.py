"""Compile source-bound projections without changing the confirmed input."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_condition_projection import condition_statements
from draftomen.semantic_capability_records import (
    CapabilityAction,
    CapabilityPrerequisite,
    CapabilityQuantity,
    CapabilityZone,
    CardCapability,
    PrerequisiteKind,
    QuantityRelation,
    _enum_member,
)
from draftomen.semantic_enrichment import (
    SemanticEnrichmentArtifact,
    _reject_constant,
    _strict_object,
    card_source_sha256,
)
from draftomen.semantic_enrichment_records import (
    CardSourcePin,
    FindingReview,
    FindingStatus,
    OracleEvidence,
    OracleFact,
    SemanticEnrichmentError,
    _exact_text,
)
from draftomen.semantic_relationship_records import (
    CardRelationship,
    PrerequisiteProjectionError,
    QualificationKind,
    QualificationOutcome,
    RelationshipParticipant,
    RelationshipPrerequisite,
    RelationshipPrerequisiteProjection,
    RelationshipQualification,
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
    oracle_evidence_window,
    validate_prerequisite_sources,
    validate_relationship_pins,
    validate_relationship_sources,
)
from draftomen.semantic_roles import (
    Role,
    friendly_untap_statement,
    hone_payoff_statement,
    hone_source_statement,
    token_replacement_statement,
)
from draftomen.set_enrichment_candidates import ROLE_COMPATIBILITY_RULES
from draftomen.set_enrichment_extraction import (
    _canonical_evidence,
    _participant_oracle_text,
    _projection_evidence,
    _relationship_participant,
)

__all__ = [
    "RelationshipConversion",
    "RelationshipConversionOutcome",
    "RelationshipProjectionCompilation",
    "compile_capability_facts",
    "compile_confirmed_relationship_projections",
    "compile_hone_relationships",
    "compile_token_replacement_relationships",
]

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

# A capability that cites one of the closed token-production instructions produces a creature
# through that instruction alone, so the instruction decides what the enabler supplies and the
# statements around it decide under which conditions.  Recognition reads the cited instructions and
# the selected face's own Oracle text; no card, family or mechanic identity takes part in it.
_AMASS_INSTRUCTION_PATTERN = re.compile(
    r"\bamass(?:es)?\s+(?P<subtype>[A-Za-z][A-Za-z'’-]+)\s+(?P<count>\d+|X)\b",
    re.IGNORECASE,
)
_AMASS_PARENTHETICAL_PATTERN = re.compile(r"\([^()]*\)")
_AMASS_REMINDER_PATTERN = re.compile(r"\bcounters? on an Army\b", re.IGNORECASE)
_RECRUIT_INSTRUCTION_PATTERN = re.compile(r"\brecruit\b", re.IGNORECASE)
_DISCARD_CONDITION_PATTERN = re.compile(
    r"If you discarded a nonland card, create [^.]+\.", re.IGNORECASE
)
_NO_ARMY_CONDITION_PATTERN = re.compile(r"If (?:you|they) don['’]t control an Army")
_PARTY_AMASS_PATTERN = re.compile(r"\b(?:Its|Their) controller amasses\b")
_OWN_CONTROLLER_PATTERN = re.compile(r"If you controlled\b[^.]*\.")
_THRESHOLD_CONDITION_PATTERN = re.compile(
    r"If you control (?P<count>[A-Za-z]+|\d+) or more (?P<kind>[A-Za-z][A-Za-z'’-]+)s\b",
    re.IGNORECASE,
)
_CONDITIONAL_CREATION_PATTERN = re.compile(r"If you do, create [^.]+\.", re.IGNORECASE)
_CREATED_TOKEN_PATTERN = re.compile(r"\bcreate [^.;:]*?creature tokens?\b", re.IGNORECASE)
# Replacement wording only redirects another creator's event ("would create ... instead"), so it
# declares no production family of its own even though it prints a create verb.
_REPLACEMENT_CREATION_PATTERN = re.compile(r"\bwould (?:be )?create\b|\binstead\b", re.IGNORECASE)
_OTHER_PLAYER_CREATION_PATTERN = re.compile(
    r"\b(?:they|their controller|an opponent)\b[^.]*\bcreate",
    re.IGNORECASE,
)
_ADVENTURE_SEQUENCE_PATTERN = re.compile(
    r"\(Then exile this card\. You may cast the [A-Za-z]+ later from exile\.\)"
)
_REMINDER_PATTERN = re.compile(r"\s*\((?P<text>[^()]*)\)")
_MODAL_FRAME_PATTERN = re.compile(r"^choose (?:one|two|three)\b[^:]*[—:-]\s*$", re.IGNORECASE)
_TYPE_CHOICE_PATTERN = re.compile(r"\bchoose a creature type\b[^.]*\.", re.IGNORECASE)
_ADDITIONAL_COST_PATTERN = re.compile(r"As an additional cost to cast [^,]+, (?P<alternatives>[^.]+)\.")
_ACTIVATION_RESTRICTION_PATTERN = re.compile(r"Activate only [^.]+\.", re.IGNORECASE)
_CONVERSION_PATTERN = re.compile(
    r"becomes? an? (?P<subtype>[A-Z][a-z'’-]+) in addition to its other types\."
)
_TIMING_FRAME_PATTERN = re.compile(r"^(?:At the beginning of|When|Whenever|During)\b", re.IGNORECASE)
_PARENT_CONDITION_PATTERN = re.compile(r"^(?:If|Unless|As long as|While|During)\b", re.IGNORECASE)
# One closed effect grammar no typed clause can bind: a mill followed by a bounded selection of the
# milled cards into hand.  The instruction prints its own mill count, selection limit, card type and
# destination, so recognition reads structure only and never a card identity.
_MILL_RETURN_PATTERN = re.compile(
    r"\bMill (?P<mill>[A-Za-z]+|\d+) cards?, then put up to (?P<limit>[A-Za-z]+|\d+) "
    r"(?P<types>(?:[A-Za-z][A-Za-z'’-]*\s+)+?)cards from among them into your hand\.",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARIES = ".\n"

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
# A retained prerequisite keeps its own statement, so its closed kind decides the closed
# qualification kind; the mapping carries no semantic verdict beyond that statement.
_QUALIFICATION_KINDS: Mapping[PrerequisiteKind, QualificationKind] = {
    PrerequisiteKind.COST: QualificationKind.COST,
    PrerequisiteKind.TRIGGER: QualificationKind.TIMING,
    PrerequisiteKind.CONDITION: QualificationKind.CONDITION,
    PrerequisiteKind.THRESHOLD: QualificationKind.QUANTITY,
}


class RelationshipConversionOutcome(StrEnum):
    """Closed outcome of one stored relationship's offline projection attempt."""

    DECODED = "decoded"
    QUALIFIED = "qualified"
    CONTRADICTION = "contradiction"
    MISSING_EVIDENCE = "missing_evidence"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class RelationshipConversion:
    """One stored relationship's deterministic outcome with the gate that decided it."""

    finding_id: str
    mechanism: str
    outcome: RelationshipConversionOutcome
    reason: str

    def __post_init__(self) -> None:
        _exact_text(self.finding_id, "finding_id")
        _exact_text(self.mechanism, "mechanism")
        _enum_member(self.outcome, "outcome", RelationshipConversionOutcome)
        _exact_text(self.reason, "reason")

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible conversion object."""
        return {
            "finding_id": self.finding_id,
            "mechanism": self.mechanism,
            "outcome": self.outcome.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RelationshipProjectionCompilation:
    """One artifact's compiled relationship rows and their per-finding conversions."""

    relationships: tuple[CardRelationship, ...]
    conversions: tuple[RelationshipConversion, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.relationships, tuple) or any(
            type(item) is not CardRelationship for item in self.relationships
        ):
            raise SemanticEnrichmentError("relationships must be a tuple of CardRelationship records.")
        if not isinstance(self.conversions, tuple) or any(
            type(item) is not RelationshipConversion for item in self.conversions
        ):
            raise SemanticEnrichmentError("conversions must be a tuple of RelationshipConversion records.")
        if tuple(row.finding_id for row in self.relationships) != tuple(
            item.finding_id for item in self.conversions
        ):
            raise SemanticEnrichmentError(
                "conversions must account for every stored relationship in stored order."
            )


@dataclass(frozen=True, slots=True)
class _CompiledRelationship:
    """One stored relationship row together with the gate that decided its conversion."""

    relationship: CardRelationship
    outcome: RelationshipConversionOutcome
    reason: str


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


@dataclass(frozen=True, slots=True)
class _TokenFamily:
    """One token-production family a capability's own cited instruction declares."""

    produced: tuple[str, ...]
    qualifications: tuple[RelationshipQualification, ...]


def compile_confirmed_relationship_projections(
    *,
    artifact: SemanticEnrichmentArtifact,
    card_database: CardDatabase,
) -> RelationshipProjectionCompilation:
    """Compile eligible projections with one deterministic conversion per stored relationship."""
    if not isinstance(artifact, SemanticEnrichmentArtifact):
        raise SemanticEnrichmentError("artifact must be a SemanticEnrichmentArtifact record.")
    if not isinstance(card_database, CardDatabase):
        raise SemanticEnrichmentError("card_database must be a CardDatabase record.")
    facts = _capability_facts(artifact)
    pins = {pin.card_id: pin for pin in artifact.cards}
    relationships: list[CardRelationship] = []
    conversions: list[RelationshipConversion] = []
    for relationship in artifact.confirmed_relationships:
        compiled = _compile_relationship(
            relationship=relationship,
            facts=facts,
            pins=pins,
            cards=card_database.cards,
        )
        relationships.append(compiled.relationship)
        conversions.append(
            RelationshipConversion(
                finding_id=relationship.finding_id,
                mechanism=relationship.mechanism,
                outcome=compiled.outcome,
                reason=compiled.reason,
            )
        )
    return RelationshipProjectionCompilation(
        relationships=tuple(relationships),
        conversions=tuple(conversions),
    )


def compile_token_replacement_relationships(
    *,
    artifact: SemanticEnrichmentArtifact,
    card_database: CardDatabase,
) -> tuple[CardRelationship, ...]:
    """Derive actual token-source support for retained replacement effects."""
    if not isinstance(artifact, SemanticEnrichmentArtifact):
        raise SemanticEnrichmentError("artifact must be a SemanticEnrichmentArtifact record.")
    if not isinstance(card_database, CardDatabase):
        raise SemanticEnrichmentError("card_database must be a CardDatabase record.")
    facts = _capability_facts(artifact)
    pins = {pin.card_id: pin for pin in artifact.cards}
    targets = tuple(
        sorted(
            (
                item
                for item in facts.values()
                if item.role is Role.TOKEN_REPLACEMENT
                and item.review.status in (FindingStatus.ACCEPTED, FindingStatus.UNCERTAIN)
            ),
            key=lambda item: (item.card_id, item.face_index is not None, item.face_index or 0),
        )
    )
    sources = _distinct_token_sources(facts=facts, cards=card_database.cards)
    relationships: list[CardRelationship] = []
    for target in targets:
        target_card = card_database.cards.get(target.card_id)
        if target_card is None or target_card.unknown or target.card_id not in pins:
            continue
        target_participant = _replacement_participant(
            capability=target,
            card=target_card,
        )
        if target_participant is None:
            continue
        for source, family in sources:
            if source.card_id == target.card_id:
                continue
            source_card = card_database.cards.get(source.card_id)
            if source_card is None or source_card.unknown or source.card_id not in pins:
                continue
            source_participant = _compile_participant(
                capability=source,
                other=target,
                card=source_card,
                family=family,
            )
            if source_participant is None:
                continue
            projection = RelationshipPrerequisiteProjection(
                source=source_participant,
                target=target_participant,
            )
            evidence = tuple(
                sorted(
                    {
                        item.evidence
                        for participant in (source_participant, target_participant)
                        for item in (*participant.prerequisites, *participant.qualifications)
                    },
                    key=lambda item: (
                        item.card_id,
                        -1 if item.face_index is None else item.face_index,
                        item.quote,
                    ),
                )
            )
            relationships.append(
                CardRelationship(
                    finding_id=(
                        f"{target.run_id}:relationship:token-source-replacement:"
                        f"{source.card_id}:{source.finding_id}:"
                        f"{target.card_id}:{target.finding_id}"
                    ),
                    mechanism="token-source-replacement",
                    participants=(source.card_id, target.card_id),
                    claim="A separate token creation event enables the replacement effect.",
                    prerequisites=(
                        "The source must create a token under your control.",
                        "The replacement modifies that event without supplying a token.",
                    ),
                    oracle_evidence=evidence,
                    guide_evidence=(),
                    review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
                    run_id=target.run_id,
                    prerequisite_projection=projection,
                )
            )
    return tuple(relationships)


def compile_hone_relationships(
    *,
    artifact: SemanticEnrichmentArtifact,
    card_database: CardDatabase,
) -> tuple[CardRelationship, ...]:
    """Derive cross-card Hone support from retained source and Equipment facts."""
    if not isinstance(artifact, SemanticEnrichmentArtifact):
        raise SemanticEnrichmentError("artifact must be a SemanticEnrichmentArtifact record.")
    if not isinstance(card_database, CardDatabase):
        raise SemanticEnrichmentError("card_database must be a CardDatabase record.")
    facts = _capability_facts(artifact)
    pins = {pin.card_id: pin for pin in artifact.cards}
    sources = sorted(
        (item for item in facts.values() if item.role is Role.HONE_COUNTER_SOURCE),
        key=lambda item: (item.card_id, item.finding_id),
    )
    targets = sorted(
        (
            item
            for item in facts.values()
            if item.role is Role.HONE_EQUIPMENT_PAYOFF
            and (card := card_database.cards.get(item.card_id)) is not None
            and "equipment" in card.type_line.casefold()
        ),
        key=lambda item: (item.card_id, item.finding_id),
    )
    relationships: list[CardRelationship] = []
    for source in sources:
        source_card = card_database.cards.get(source.card_id)
        if source_card is None or source_card.unknown or source.card_id not in pins:
            continue
        for target in targets:
            if source.card_id == target.card_id:
                continue
            target_card = card_database.cards.get(target.card_id)
            if target_card is None or target_card.unknown or target.card_id not in pins:
                continue
            source_participant = _hone_participant(
                capability=source,
                card=source_card,
                source=True,
            )
            target_participant = _hone_participant(
                capability=target,
                card=target_card,
                source=False,
            )
            if source_participant is None or target_participant is None:
                continue
            projection = RelationshipPrerequisiteProjection(
                source=source_participant,
                target=target_participant,
            )
            evidence = _canonical_evidence(
                tuple(
                    qualification.evidence
                    for participant in (source_participant, target_participant)
                    for qualification in participant.qualifications
                )
            )
            relationships.append(
                CardRelationship(
                    finding_id=(
                        f"{target.run_id}:relationship:hone-equipment-payoff:"
                        f"{source.card_id}:{source.finding_id}:"
                        f"{target.card_id}:{target.finding_id}"
                    ),
                    mechanism="hone-equipment-payoff",
                    participants=(source.card_id, target.card_id),
                    claim="The Hone source adds counters whose printed Equipment rule grants power.",
                    prerequisites=(
                        "The source must put hone counters on Equipment you control.",
                        "The target must be Equipment with the printed hone-counter effect.",
                    ),
                    oracle_evidence=evidence,
                    guide_evidence=(),
                    review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
                    run_id=target.run_id,
                    prerequisite_projection=projection,
                )
            )
    return tuple(relationships)


def _hone_participant(
    *,
    capability: CardCapability,
    card: CardInfo,
    source: bool,
) -> RelationshipParticipant | None:
    """Build one Hone participant while retaining its printed timing and effect."""
    if len(capability.evidence) != 1:
        return None
    evidence = capability.evidence[0].quote
    oracle_text = _participant_oracle_text(capability, card)
    window = _evidence_window(
        oracle_text=oracle_text,
        capability=capability,
        quote=evidence,
    )
    if window is None:
        return None
    recognized = hone_source_statement(window) if source else hone_payoff_statement(window)
    if recognized is None:
        return None
    selector, _ = recognized
    selectors: tuple[tuple[QualificationKind, str], ...]
    if source:
        timing = re.search(r"Whenever [^,\n]+ enters or attacks", window)
        selectors = (
            (QualificationKind.TIMING, timing.group(0)),
            (QualificationKind.MODE, selector),
        ) if timing is not None else ((QualificationKind.MODE, selector),)
    else:
        selectors = ((QualificationKind.CONDITION, selector),)
    qualifications = tuple(
        qualification
        for kind, item in selectors
        if (
            qualification := _qualification(
                kind=kind,
                capability=capability,
                evidence=window,
                selector=item,
            )
        ) is not None
    )
    if len(qualifications) != len(selectors):
        return None
    try:
        return _relationship_participant(
            capability=capability,
            card=card,
            clauses=(),
            qualifications=qualifications,
        )
    except SemanticEnrichmentError:
        return None


def _distinct_token_sources(
    *,
    facts: Mapping[tuple[int, str], CardCapability],
    cards: Mapping[int, CardInfo],
) -> tuple[tuple[CardCapability, _TokenFamily], ...]:
    """Return canonical actual creators, excluding replacement and Army-growth records."""
    distinct: dict[
        tuple[int, int | None, tuple[str, ...]],
        tuple[CardCapability, _TokenFamily],
    ] = {}
    for capability in sorted(
        facts.values(),
        key=lambda item: (
            item.card_id,
            item.face_index is not None,
            item.face_index or 0,
            item.finding_id,
        ),
    ):
        if capability.role is not Role.TOKEN_MAKER or capability.action is not CapabilityAction.CREATE:
            continue
        quotes = tuple(item.quote for item in capability.evidence)
        if any(
            _AMASS_INSTRUCTION_PATTERN.search(quote) is not None
            or _NO_ARMY_CONDITION_PATTERN.search(quote) is not None
            or _OTHER_PLAYER_CREATION_PATTERN.search(quote) is not None
            for quote in quotes
        ):
            continue
        card = cards.get(capability.card_id)
        if card is None or card.unknown:
            continue
        family = _token_family(
            capability=capability,
            oracle_text=_participant_oracle_text(capability, card),
        )
        if family is None:
            continue
        distinct.setdefault(
            (capability.card_id, capability.face_index, quotes),
            (capability, family),
        )
    return tuple(distinct.values())


def _replacement_participant(
    *,
    capability: CardCapability,
    card: CardInfo,
) -> RelationshipParticipant | None:
    """Compile one replacement payoff with its source dependency and effect retained."""
    if len(capability.evidence) != 1:
        return None
    quote = capability.evidence[0].quote
    oracle_text = _participant_oracle_text(capability, card)
    window = _evidence_window(
        oracle_text=oracle_text,
        capability=capability,
        quote=quote,
    )
    if window is None or token_replacement_statement(window) is None:
        return None
    qualifications: list[RelationshipQualification] = []
    for kind, selector in (
        (QualificationKind.CONDITION, "If one or more tokens would be created under your control"),
        (QualificationKind.QUANTITY, "twice that many"),
        (QualificationKind.MODE, "created instead"),
    ):
        qualification = _qualification(
            kind=kind,
            capability=capability,
            evidence=window,
            selector=selector,
        )
        if qualification is None:
            return None
        qualifications.append(qualification)
    return _relationship_participant(
        capability=capability,
        card=card,
        clauses=(),
        qualifications=tuple(qualifications),
    )


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


def compile_capability_facts(
    *, artifact: SemanticEnrichmentArtifact
) -> tuple[CardCapability, ...]:
    """Return every unambiguous typed capability retained by an artifact.
    Publication consumes these Oracle-derived facts independently of relationships.
    """

    return tuple(
        capability
        for _, capability in sorted(
            _capability_facts(artifact).items(),
            key=lambda item: item[0],
        )
    )


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
        capability = CardCapability.from_json(payload)
    except SemanticEnrichmentError:
        return None
    if (
        capability.role is Role.DISABLING_REMOVAL
        and capability.evidence
        and all(friendly_untap_statement(item.quote) is not None for item in capability.evidence)
    ):
        return replace(capability, role=Role.UNTAP_SUPPORT)
    if (
        capability.role is Role.TOKEN_MAKER
        and capability.evidence
        and all(
            token_replacement_statement(item.quote) is not None
            for item in capability.evidence
        )
    ):
        return replace(
            capability,
            role=Role.TOKEN_REPLACEMENT,
            action=CapabilityAction.REPLACE,
            quantity=None,
            source_zone=None,
            destination_zone=None,
        )
    if (
        capability.role is Role.COUNTERS
        and capability.evidence
        and all(hone_source_statement(item.quote) is not None for item in capability.evidence)
    ):
        return replace(capability, role=Role.HONE_COUNTER_SOURCE)
    if (
        capability.role is Role.EQUIPMENT_PAYOFF
        and capability.evidence
        and all(hone_payoff_statement(item.quote) is not None for item in capability.evidence)
    ):
        return replace(capability, role=Role.HONE_EQUIPMENT_PAYOFF)
    return capability


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
) -> _CompiledRelationship:
    """Compile one projection or name the first gate that decided the stored row's conversion."""
    if relationship.prerequisite_projection is not None:
        outcome = (
            RelationshipConversionOutcome.QUALIFIED
            if relationship.prerequisite_projection.outcome is QualificationOutcome.QUALIFIED
            else RelationshipConversionOutcome.DECODED
        )
        return _CompiledRelationship(relationship=relationship, outcome=outcome, reason="projected")
    pair = _local_pair(relationship)
    if pair is None:
        return _unconverted(relationship, RelationshipConversionOutcome.UNSUPPORTED, "not_a_local_pair")
    link = _ROLE_LINKS.get(pair.mechanism)
    if link is None:
        return _unconverted(
            relationship, RelationshipConversionOutcome.UNSUPPORTED, "mechanism_unlinked"
        )
    source = facts.get((pair.source_card_id, pair.source_capability_id))
    if source is None:
        return _unconverted(
            relationship,
            RelationshipConversionOutcome.MISSING_EVIDENCE,
            "source_capability_fact_unusable",
        )
    target = facts.get((pair.target_card_id, pair.target_capability_id))
    if target is None:
        return _unconverted(
            relationship,
            RelationshipConversionOutcome.MISSING_EVIDENCE,
            "target_capability_fact_unusable",
        )
    if (
        source.role is Role.TOKEN_REPLACEMENT
        and link.enabler is Role.TOKEN_MAKER
    ):
        return _unconverted(
            relationship,
            RelationshipConversionOutcome.UNSUPPORTED,
            "token_replacement_requires_separate_source",
        )
    if source.role is not link.enabler:
        return _unconverted(
            relationship,
            RelationshipConversionOutcome.MISSING_EVIDENCE,
            "source_capability_fact_unusable",
        )
    if target.role is not link.payoff:
        return _unconverted(
            relationship,
            RelationshipConversionOutcome.MISSING_EVIDENCE,
            "target_capability_fact_unusable",
        )
    source_card = cards.get(source.card_id)
    target_card = cards.get(target.card_id)
    if (
        source_card is None
        or target_card is None
        or source_card.unknown
        or target_card.unknown
        or pins.get(source.card_id) is None
        or pins.get(target.card_id) is None
    ):
        return _unconverted(
            relationship,
            RelationshipConversionOutcome.MISSING_EVIDENCE,
            "participant_card_unpinned",
        )
    contradicted = _zone_supply_contradiction(source=source, target=target, facts=facts)
    if contradicted:
        return _unconverted(
            relationship,
            RelationshipConversionOutcome.CONTRADICTION,
            "zone_supply_contradiction:" + ",".join(zone.value for zone in contradicted),
        )
    family = _token_family(
        capability=source,
        oracle_text=_participant_oracle_text(source, source_card),
    )
    source_participant = _compile_participant(
        capability=source,
        other=target,
        card=source_card,
        family=family,
    )
    if source_participant is None and (
        _mill_return_instruction(
            capability=source,
            oracle_text=_participant_oracle_text(source, source_card),
        )
        or (
            source.action is CapabilityAction.RETURN
            and (_is_adventure_card(source_card) or _is_adventure_card(target_card))
        )
    ):
        source_participant = _qualified_effect_participant(
            capability=source,
            other=target,
            card=source_card,
        )
    if source_participant is None:
        return _unconverted(
            relationship, RelationshipConversionOutcome.UNSUPPORTED, "source_clause_unbound"
        )
    target_participant = _compile_participant(
        capability=target,
        other=source,
        card=target_card,
        family=None,
    )
    if family is not None:
        opposed = _opposed_token_subtype(
            family=family,
            target=target,
            target_oracle=_participant_oracle_text(target, target_card),
        )
        if opposed is None:
            return _unconverted(
                relationship, RelationshipConversionOutcome.UNSUPPORTED, "target_clause_unbound"
            )
        if opposed:
            return _unconverted(
                relationship,
                RelationshipConversionOutcome.CONTRADICTION,
                "token_subtype_contradiction",
            )
    if target_participant is None and family is not None:
        target_participant = _qualified_effect_participant(
            capability=target,
            other=source,
            card=target_card,
        )
    if target_participant is None:
        return _unconverted(
            relationship, RelationshipConversionOutcome.UNSUPPORTED, "target_clause_unbound"
        )
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
        projected = _projected_relationship(
            relationship=relationship, projection=projection, pins=pins, cards=cards
        )
    except (SemanticEnrichmentError, PrerequisiteProjectionError) as error:
        return _unconverted(
            relationship,
            RelationshipConversionOutcome.UNSUPPORTED,
            f"publish_validation_rejected:{type(error).__name__}",
        )
    outcome = (
        RelationshipConversionOutcome.QUALIFIED
        if projection.outcome is QualificationOutcome.QUALIFIED
        else RelationshipConversionOutcome.DECODED
    )
    return _CompiledRelationship(relationship=projected, outcome=outcome, reason="projected")


def _unconverted(
    relationship: CardRelationship,
    outcome: RelationshipConversionOutcome,
    reason: str,
) -> _CompiledRelationship:
    """Return one stored relationship unchanged with the gate that stopped its conversion."""
    return _CompiledRelationship(relationship=relationship, outcome=outcome, reason=reason)


def _counted_zones(*, capability: CardCapability) -> frozenset[CapabilityZone]:
    """Return every zone one capability's counted prerequisites read."""
    return frozenset(
        zone
        for prerequisite in capability.prerequisites
        if prerequisite.quantity is not None
        for zone, _, _ in _stated_zones(prerequisite.evidence.quote)
    )


def _removed_zones(*, capability: CardCapability) -> frozenset[CapabilityZone]:
    """Return every zone one capability's own statements consume without stating a return."""
    removed: set[CapabilityZone] = set()
    if (
        capability.action is not CapabilityAction.COUNT
        and capability.source_zone is not None
        and capability.source_zone is not capability.destination_zone
    ):
        removed.add(capability.source_zone)
    for prerequisite in capability.prerequisites:
        if prerequisite.source_zone is not None and prerequisite.destination_zone is None:
            removed.add(prerequisite.source_zone)
    return frozenset(removed)


def _refilled(
    *,
    capability: CardCapability,
    zone: CapabilityZone,
    facts: Mapping[tuple[int, str], CardCapability],
) -> bool:
    """Return whether another capability of the same card face moves cards into one zone."""
    return any(
        other is not capability
        and other.card_id == capability.card_id
        and other.face_index == capability.face_index
        and other.destination_zone is zone
        and other.source_zone in (CapabilityZone.LIBRARY, CapabilityZone.HAND)
        for other in facts.values()
    )


def _zone_supply_contradiction(
    *,
    source: CardCapability,
    target: CardCapability,
    facts: Mapping[tuple[int, str], CardCapability],
) -> tuple[CapabilityZone, ...]:
    """Return the zones one enabler consumes as the supply another participant counts."""
    contradicted = {
        zone
        for zone in _counted_zones(capability=target) & _removed_zones(capability=source)
        if not _refilled(capability=source, zone=zone, facts=facts)
    }
    return tuple(sorted(contradicted, key=lambda zone: zone.value))


def _compile_participant(
    *,
    capability: CardCapability,
    other: CardCapability,
    card: CardInfo,
    family: _TokenFamily | None,
) -> RelationshipParticipant | None:
    """Compile every clause and retained qualification one participant declares, or None.

    A participant whose capability declares a token-production family keeps the family's own
    instruction, condition, controller branch or threshold statements next to the typed clauses the
    ordinary path binds, because a keyword instruction alone cannot carry its conditions.  Only the
    enabler side passes a family, so a payoff never borrows another participant's instruction.
    """
    oracle_text = _participant_oracle_text(capability, card)
    if capability.prerequisites:
        clauses: list[RelationshipPrerequisite] = []
        qualifications: list[RelationshipQualification] = []
        for index, prerequisite in enumerate(capability.prerequisites):
            clause = _compile_bound_clause(
                capability=capability,
                other=other,
                prerequisite=prerequisite,
                index=index,
                oracle_text=oracle_text,
            )
            if clause is not None:
                clauses.append(clause)
                continue
            if family is not None and _family_retains(family=family, prerequisite=prerequisite):
                continue
            qualification = _retained_qualification(
                capability=capability,
                prerequisite=prerequisite,
                oracle_text=oracle_text,
            )
            if qualification is None:
                return None
            qualifications.append(qualification)
    else:
        clause = _compile_effect_clause(capability=capability, other=other, oracle_text=oracle_text)
        if clause is None and family is None:
            return None
        clauses = [] if clause is None else [clause]
        qualifications = []
    if family is not None:
        qualifications.extend(family.qualifications)
    conditions = _condition_qualifications(capability=capability, oracle_text=oracle_text)
    if conditions is None:
        return None
    qualifications.extend(conditions)
    adventure = _adventure_sequence_qualifications(
        capability=capability,
        card=card,
        oracle_text=oracle_text,
    )
    if adventure is None:
        return None
    qualifications.extend(adventure)
    try:
        return _relationship_participant(
            capability=capability,
            card=card,
            clauses=tuple(clauses),
            qualifications=tuple(dict.fromkeys(qualifications)),
        )
    except SemanticEnrichmentError:
        return None


def _adventure_sequence_qualifications(
    *,
    capability: CardCapability,
    card: CardInfo,
    oracle_text: str,
) -> tuple[RelationshipQualification, ...] | None:
    """Retain the printed exile-then-cast sequence on an Adventure face.

    The parent card id remains the drafted-card identity. Only the face whose type line declares
    Adventure may carry the sequence. A face that omits reminder text keeps its exact capability
    statement as the component-mode anchor; ambiguous source text blocks projection.
    """
    face_index = capability.face_index
    if face_index is None or not 0 <= face_index < len(card.faces):
        return ()
    face = card.faces[face_index]
    if "Adventure" not in face.subtypes and (
        face.type_line is None or "Adventure" not in face.type_line.split()
    ):
        return ()
    matches = tuple(_ADVENTURE_SEQUENCE_PATTERN.finditer(oracle_text))
    if len(matches) > 1:
        return None
    if matches:
        selector = matches[0].group(0)
        evidence = oracle_text
    else:
        # Some printed Adventure faces omit reminder text. Their Adventure type and parent layout
        # still define the same component sequence; retain the capability's exact cited statement
        # as the mode anchor instead of inventing rules text that is absent from the source.
        if len(capability.evidence) != 1:
            return None
        selector = capability.evidence[0].quote
        evidence = _evidence_window(
            oracle_text=oracle_text,
            capability=capability,
            quote=selector,
        )
        if evidence is None:
            return None
    qualification = _qualification(
        kind=QualificationKind.MODE,
        capability=capability,
        evidence=evidence,
        selector=selector,
    )
    return None if qualification is None else (qualification,)


def _is_adventure_card(card: CardInfo) -> bool:
    """Return whether canonical card data declares the Adventure layout."""
    return card.layout == "adventure" and any(
        "Adventure" in face.subtypes
        or (face.type_line is not None and "Adventure" in face.type_line.split())
        for face in card.faces
    )


def _condition_qualifications(
    *,
    capability: CardCapability,
    oracle_text: str,
) -> tuple[RelationshipQualification, ...] | None:
    """Retain the condition-family statements one capability's own citations declare.

    A citation the shared recognizer cannot place returns nothing; a citation it recognizes as a
    family ability whose required same-face definition or frame cannot bind fails the participant
    closed instead of silently keeping only the effect.  Every statement is retained inside the
    smallest complete-line window of the selected face that also covers its own citation, so a
    storied clause carries the reminder it references and never borrows another face's text.
    """
    retained: list[RelationshipQualification] = []
    for item in capability.evidence:
        statements = condition_statements(oracle_text=oracle_text, quote=item.quote)
        if statements is None:
            return None
        if not statements:
            continue
        quoted = _occurrences(oracle_text, item.quote)
        if len(quoted) != 1:
            return None
        covered = [quoted[0]]
        for _, selector in statements:
            found = _occurrences(oracle_text, selector)
            if len(found) != 1:
                return None
            covered.append(found[0])
        evidence = _covered_lines(oracle_text=oracle_text, spans=covered)
        if evidence is None:
            return None
        for kind, selector in statements:
            qualification = _qualification(
                kind=kind,
                capability=capability,
                evidence=evidence,
                selector=selector,
            )
            if qualification is None:
                return None
            retained.append(qualification)
    return tuple(dict.fromkeys(retained))


def _token_family(*, capability: CardCapability, oracle_text: str) -> _TokenFamily | None:
    """Recognize the token-production family a capability's own cited instruction declares.

    A closed keyword instruction (amass, recruit), a chapter that conditions a second creation on
    the printed count of the first token, or an explicit ``create ... creature token`` instruction
    decides what the capability produces; the instruction, the branch or condition it states and the
    reminder it owns stay retained inside complete-line windows of the selected face.  A citation
    without a resolvable window or without one of those instructions declares no family, so a plain
    effect statement never reads as qualified production.
    """
    produced: set[str] = set()
    qualifications: list[RelationshipQualification] = []
    recognized = False
    for item in capability.evidence:
        window = _evidence_window(
            oracle_text=oracle_text,
            capability=capability,
            quote=item.quote,
        )
        if window is None:
            return None
        amass = _amass_retention(window=window, capability=capability)
        retained = amass
        if retained is None:
            retained = _recruit_retention(window=window, capability=capability)
        if retained is None:
            retained = _chapter_retention(window=window, capability=capability)
        if retained is None:
            retained = _creation_retention(window=window, capability=capability)
        if retained is None:
            continue
        recognized = True
        qualifications.extend(retained)
        produced.update(
            _amass_subtypes(window) if amass is not None else _created_subtypes(window)
        )
    if not recognized:
        return None
    return _TokenFamily(
        produced=tuple(sorted(produced)),
        qualifications=tuple(dict.fromkeys(qualifications)),
    )


def _family_retains(*, family: _TokenFamily, prerequisite: CapabilityPrerequisite) -> bool:
    """Return whether one family qualification already retains a prerequisite's own statement."""
    kind = _QUALIFICATION_KINDS[prerequisite.kind]
    quote = prerequisite.evidence.quote
    return any(
        qualification.kind is kind
        and (quote in qualification.selector or quote in qualification.evidence.quote)
        for qualification in family.qualifications
    )


def _amass_retention(
    *,
    window: str,
    capability: CardCapability,
) -> tuple[RelationshipQualification, ...] | None:
    """Retain one amass instruction, the reminder it owns and the controller branch it states."""
    instructions = _instructions(window, _AMASS_INSTRUCTION_PATTERN)
    if not instructions:
        return None
    retained: list[RelationshipQualification] = []
    for match in instructions:
        statement = _statement_span(window, match.start(), match.end())
        end = _reminder_end(window, statement[1], _AMASS_REMINDER_PATTERN)
        qualification = _qualification(
            kind=QualificationKind.MODE,
            capability=capability,
            evidence=window,
            selector=window[statement[0] : end],
        )
        if qualification is None:
            return None
        retained.append(qualification)
    condition = _NO_ARMY_CONDITION_PATTERN.search(window)
    if condition is not None:
        qualification = _qualification(
            kind=QualificationKind.CONDITION,
            capability=capability,
            evidence=window,
            selector=condition.group(0),
        )
        if qualification is None:
            return None
        retained.append(qualification)
    party = _PARTY_AMASS_PATTERN.search(window)
    if party is not None:
        statement = _statement_span(window, party.start(), party.end())
        branch = _OWN_CONTROLLER_PATTERN.search(window, statement[0])
        end = statement[1] if branch is None else branch.end()
        qualification = _qualification(
            kind=QualificationKind.PARTY,
            capability=capability,
            evidence=window,
            selector=window[statement[0] : end],
        )
        if qualification is None:
            return None
        retained.append(qualification)
    return tuple(retained)


def _recruit_retention(
    *,
    window: str,
    capability: CardCapability,
) -> tuple[RelationshipQualification, ...] | None:
    """Retain one recruit instruction, the frame it hangs on and its discard condition."""
    match = _RECRUIT_INSTRUCTION_PATTERN.search(window)
    if match is None:
        return None
    retained: list[RelationshipQualification] = []
    statement = _statement_span(window, match.start(), match.end())
    frame = window[statement[0] : statement[1]]
    qualification = _qualification(
        kind=(
            QualificationKind.CONDITION
            if _PARENT_CONDITION_PATTERN.match(frame) is not None
            else QualificationKind.TIMING
        ),
        capability=capability,
        evidence=window,
        selector=frame,
    )
    if qualification is None:
        return None
    retained.append(qualification)
    condition = _DISCARD_CONDITION_PATTERN.search(window)
    if condition is not None:
        qualification = _qualification(
            kind=QualificationKind.CONDITION,
            capability=capability,
            evidence=window,
            selector=condition.group(0),
        )
        if qualification is None:
            return None
        retained.append(qualification)
    return tuple(retained)


def _chapter_retention(
    *,
    window: str,
    capability: CardCapability,
) -> tuple[RelationshipQualification, ...] | None:
    """Retain the threshold and conditional creation of one chapter that makes two tokens."""
    threshold = _THRESHOLD_CONDITION_PATTERN.search(window)
    if threshold is None:
        return None
    creation = _CONDITIONAL_CREATION_PATTERN.search(window, threshold.end())
    if creation is None:
        return None
    sequence = _statement_span(window, threshold.start(), threshold.end())
    end = _statement_span(window, creation.start(), creation.end())[1]
    retained: list[RelationshipQualification] = []
    for kind, selector in (
        (QualificationKind.QUANTITY, window[threshold.start() : threshold.end()]),
        (QualificationKind.CONDITION, window[sequence[0] : end]),
    ):
        qualification = _qualification(
            kind=kind,
            capability=capability,
            evidence=window,
            selector=selector,
        )
        if qualification is None:
            return None
        retained.append(qualification)
    return tuple(retained)


def _creation_retention(
    *,
    window: str,
    capability: CardCapability,
) -> tuple[RelationshipQualification, ...] | None:
    """Retain one explicit creature-creation instruction, its frame and its parent condition.

    The retained instruction is the closed ``create ... creature token`` statement the cited window
    itself states; replacement wording ("would create ... instead") only redirects another creator's
    event, so it declares no production.  The statement keeps the closed kind of the frame it hangs
    on, and a directly printed parent condition stays its own retained statement, because neither a
    keyword nor a bare creation verb can carry the conditions the token's arrival depends on.
    """
    instructions = tuple(
        match
        for match in _instructions(window, _CREATED_TOKEN_PATTERN)
        if not _REPLACEMENT_CREATION_PATTERN.search(
            _statement(window, match.start(), match.end())
        )
    )
    if not instructions:
        return None
    first = _statement_span(window, instructions[0].start(), instructions[0].end())
    parent = _parent_condition(window=window, start=first[0])
    retained: list[RelationshipQualification] = []
    if parent is not None:
        qualification = _qualification(
            kind=QualificationKind.CONDITION,
            capability=capability,
            evidence=window,
            selector=parent,
        )
        if qualification is None:
            return None
        retained.append(qualification)
    for match in instructions:
        statement = _statement_span(window, match.start(), match.end())
        instruction = window[statement[0] : statement[1]]
        qualification = _qualification(
            kind=(
                QualificationKind.CONDITION
                if _PARENT_CONDITION_PATTERN.match(instruction) is not None
                else QualificationKind.TIMING
            ),
            capability=capability,
            evidence=window,
            selector=instruction,
        )
        if qualification is None:
            return None
        retained.append(qualification)
    return tuple(retained)


def _parent_condition(*, window: str, start: int) -> str | None:
    """Return the complete parent condition statement directly introducing one instruction."""
    end = start
    while end > 0 and window[end - 1] in " \t":
        end -= 1
    if end == 0 or window[end - 1] not in _SENTENCE_BOUNDARIES:
        return None
    left = end - 1
    while left > 0 and window[left - 1] not in _SENTENCE_BOUNDARIES:
        left -= 1
    statement = window[left:end].strip()
    if _PARENT_CONDITION_PATTERN.match(statement) is None:
        return None
    return statement


def _amass_subtypes(window: str) -> tuple[str, ...]:
    """Return the creature subtypes an amass instruction produces: its own subtype and Army."""
    return tuple(
        match.group("subtype").casefold()
        for match in _instructions(window, _AMASS_INSTRUCTION_PATTERN)
    ) + ("army",)


def _instructions(paragraph: str, pattern: re.Pattern[str]) -> tuple[re.Match[str], ...]:
    """Return the matches of one instruction pattern a window itself states.

    Parenthetical reminder text never states an instruction: it explains a keyword the window has
    already printed, so its own wording is not the effect the card declares.
    """
    return tuple(
        pattern.finditer(
            _AMASS_PARENTHETICAL_PATTERN.sub(lambda match: " " * len(match.group(0)), paragraph)
        )
    )


def _mill_return_instruction(*, capability: CardCapability, oracle_text: str) -> bool:
    """Return whether every citation of one capability states the closed mill-then-return grammar.

    The grammar prints its own mill count, selection limit, card type and hand destination, so the
    check reads the instruction structure and never a card identity, name or audit tag.  A citation
    without a resolvable window or without any of those elements matches nothing.
    """
    if not capability.evidence:
        return False
    for item in capability.evidence:
        window = _evidence_window(
            oracle_text=oracle_text,
            capability=capability,
            quote=item.quote,
        )
        if window is None or _MILL_RETURN_PATTERN.search(window) is None:
            return False
    return True


def _created_subtypes(window: str) -> tuple[str, ...]:
    """Return the creature subtypes every created token of one cited window states."""
    return tuple(
        subtype
        for match in _CREATED_TOKEN_PATTERN.finditer(window)
        for subtype in _stated_subtypes(match.group(0))
    )


def _stated_subtypes(clause: str) -> tuple[str, ...]:
    """Return the subtype words one created token clause states, in printed order."""
    return tuple(
        match.group(0).casefold()
        for match in _WORD_PATTERN.finditer(clause)
        if match.group(0).casefold() not in _COLOR_WORDS
        and match.group(0).casefold() not in _CARD_TYPE_WORDS
        and match.group(0).casefold() not in _OBJECT_KIND_WORDS
        and match.group(0).casefold() not in _COUNT_WORDS
        and match.group(0).casefold() not in _DETERMINER_WORDS
        and match.group(0).casefold() not in _OPERATION_LEXEMES
        and len(match.group(0)) > 1
    )


def _statement_span(paragraph: str, start: int, end: int) -> tuple[int, int]:
    """Return the sentence span one instruction occupies inside its own cited window."""
    left = start
    while left > 0 and paragraph[left - 1] not in _SENTENCE_BOUNDARIES:
        left -= 1
    while left < end and paragraph[left] == " ":
        left += 1
    right = end
    while right < len(paragraph) and paragraph[right] not in _SENTENCE_BOUNDARIES:
        right += 1
    if right < len(paragraph):
        right += 1
    return left, right


def _reminder_end(paragraph: str, start: int, marker: re.Pattern[str]) -> int:
    """Return the end of the parenthetical reminder one statement owns, or the statement end."""
    match = _REMINDER_PATTERN.match(paragraph, start)
    if match is None or marker.search(match.group("text")) is None:
        return start
    return match.end()


def _opposed_token_subtype(
    *,
    family: _TokenFamily,
    target: CardCapability,
    target_oracle: str,
) -> bool | None:
    """Return whether a family's produced subtypes contradict the target's required subtype.

    None means the comparison cannot be made at all: the target states a subtype requirement and
    the family states no creature subtype it produces, so the pair fails closed instead of assuming
    the tokens fit.  A same-face type-changing ability that states the required subtype is a
    conditional bridge, so a token that is not that subtype yet can still be made into one.
    """
    required = target.qualifier.subtype
    if required is None:
        return False
    if not family.produced:
        return None
    if all(
        any(stated in _subtype_forms(word) for stated in family.produced)
        for word in required.casefold().split()
    ):
        return False
    if _conversion_statement(oracle_text=target_oracle, subtype=required) is not None:
        return False
    return True


def _qualified_effect_participant(
    *,
    capability: CardCapability,
    other: CardCapability,
    card: CardInfo,
) -> RelationshipParticipant | None:
    """Retain one family effect no typed clause can express as qualifications, or None.

    The fallback runs only for a pair whose family recognition and subtype gates already accepted
    the connection.  Every prerequisite the ordinary path cannot bind keeps the qualification kind
    its own closed kind maps to, and the complete effect statement plus the frames it depends on
    are retained inside complete-line windows of the capability's own selected face.  A missing or
    ambiguous statement fails the whole participant closed rather than dropping either side.
    """
    oracle_text = _participant_oracle_text(capability, card)
    clauses: list[RelationshipPrerequisite] = []
    qualifications: list[RelationshipQualification] = []
    for index, prerequisite in enumerate(capability.prerequisites):
        clause = _compile_bound_clause(
            capability=capability,
            other=other,
            prerequisite=prerequisite,
            index=index,
            oracle_text=oracle_text,
        )
        if clause is not None:
            clauses.append(clause)
            continue
        qualification = _retained_qualification(
            capability=capability,
            prerequisite=prerequisite,
            oracle_text=oracle_text,
        )
        if qualification is None:
            return None
        qualifications.append(qualification)
    retained = _effect_qualifications(capability=capability, oracle_text=oracle_text)
    if retained is None:
        return None
    qualifications.extend(retained)
    conditions = _condition_qualifications(capability=capability, oracle_text=oracle_text)
    if conditions is None:
        return None
    qualifications.extend(conditions)
    adventure = _adventure_sequence_qualifications(
        capability=capability,
        card=card,
        oracle_text=oracle_text,
    )
    if adventure is None:
        return None
    qualifications.extend(adventure)
    try:
        return _relationship_participant(
            capability=capability,
            card=card,
            clauses=tuple(clauses),
            qualifications=tuple(dict.fromkeys(qualifications)),
        )
    except SemanticEnrichmentError:
        return None


def _effect_qualifications(
    *,
    capability: CardCapability,
    oracle_text: str,
) -> tuple[RelationshipQualification, ...] | None:
    """Return one capability's complete cited effect as retained statements, or None.

    Each cited statement keeps its own text, so a mode, frame, cost, conversion or restriction the
    effect depends on is stated under the closed kind it belongs to.  The evidence a qualification
    cites is the smallest contiguous span of complete Oracle lines covering those statements: the
    cited text itself plus the statement it hangs on, and nothing else from a neighbouring line.
    """
    selected: list[tuple[QualificationKind, str]] = []
    for item in capability.evidence:
        window = _evidence_window(
            oracle_text=oracle_text,
            capability=capability,
            quote=item.quote,
        )
        if window is None:
            return None
        selected.extend(
            _effect_statements(
                capability=capability,
                oracle_text=oracle_text,
                quote=item.quote,
                window=window,
            )
        )
    if not selected:
        return None
    spans: list[tuple[int, int]] = []
    for _, selector in selected:
        found = _occurrences(oracle_text, selector)
        if len(found) != 1:
            return None
        spans.append(found[0])
    evidence = _covered_lines(oracle_text=oracle_text, spans=spans)
    if evidence is None:
        return None
    retained: list[RelationshipQualification] = []
    for kind, selector in selected:
        qualification = _qualification(
            kind=kind,
            capability=capability,
            evidence=evidence,
            selector=selector,
        )
        if qualification is None:
            return None
        retained.append(qualification)
    return tuple(dict.fromkeys(retained))


def _effect_statements(
    *,
    capability: CardCapability,
    oracle_text: str,
    quote: str,
    window: str,
) -> tuple[tuple[QualificationKind, str], ...]:
    """Return the statements one cited effect depends on, with the closed kind of each."""
    statements: list[tuple[QualificationKind, str]] = [(QualificationKind.CONDITION, quote)]
    frame = _modal_frame(oracle_text=oracle_text, window=window)
    if frame is not None:
        statements.append((QualificationKind.CHOICE, frame))
    choice = _TYPE_CHOICE_PATTERN.search(window)
    if choice is not None:
        statements.append(
            (QualificationKind.CHOICE, _statement(window, choice.start(), choice.end()))
        )
    cost = _cost_statement(quote=quote)
    if cost is not None:
        statements.append((QualificationKind.COST, cost[0]))
        if cost[1] is not None:
            statements.append((QualificationKind.CHOICE, cost[1]))
    restriction = _ACTIVATION_RESTRICTION_PATTERN.search(window)
    if restriction is not None:
        statements.append(
            (QualificationKind.TIMING, _statement(window, restriction.start(), restriction.end()))
        )
    conversion = _conversion_statement(
        oracle_text=oracle_text,
        subtype=capability.qualifier.subtype,
    )
    if conversion is not None:
        statements.append((QualificationKind.CONDITION, conversion[0]))
        if conversion[1] is not None:
            statements.append((QualificationKind.TIMING, conversion[1]))
    return tuple(statements)


def _modal_frame(*, oracle_text: str, window: str) -> str | None:
    """Return the printed modal frame line one cited mode hangs on, or None."""
    start = oracle_text.find(window)
    if start <= 0:
        return None
    previous_end = oracle_text.rfind("\n", 0, start)
    if previous_end < 0:
        return None
    previous_start = oracle_text.rfind("\n", 0, previous_end) + 1
    frame = oracle_text[previous_start:previous_end]
    if _MODAL_FRAME_PATTERN.match(frame) is None:
        return None
    return frame


def _cost_statement(*, quote: str) -> tuple[str, str | None] | None:
    """Return the cost frame one cited effect states and the alternative frame it offers."""
    additional = _ADDITIONAL_COST_PATTERN.match(quote)
    if additional is not None:
        alternatives = additional.group("alternatives")
        offers = alternatives if re.search(r"\bor\b", alternatives, re.IGNORECASE) else None
        return quote, offers
    head, separator, _ = quote.partition(": ")
    if not separator:
        return None
    folded = head.casefold()
    if "{" in head or "sacrifice" in folded or "tap" in folded:
        return head, None
    return None


def _conversion_statement(
    *,
    oracle_text: str,
    subtype: str | None,
) -> tuple[str, str | None] | None:
    """Return the same-face type-changing instruction for one required subtype, and its frame."""
    if subtype is None:
        return None
    for match in _CONVERSION_PATTERN.finditer(oracle_text):
        stated = match.group("subtype").casefold()
        if not any(stated == form for form in _subtype_forms(subtype.casefold())):
            continue
        statement = _statement_span(oracle_text, match.start(), match.end())
        return oracle_text[statement[0] : statement[1]], _conversion_frame(oracle_text, statement)
    return None


def _conversion_frame(oracle_text: str, statement: tuple[int, int]) -> str | None:
    """Return the timing frame of the instruction that introduces one conversion, or None."""
    if statement[0] == 0:
        return None
    previous = _statement_span(oracle_text, statement[0] - 1, statement[0] - 1)
    before = oracle_text[previous[0] : previous[1]]
    comma = before.find(",")
    frame = before if comma < 0 else before[:comma]
    if _TIMING_FRAME_PATTERN.match(frame) is None:
        return None
    return frame


def _covered_lines(*, oracle_text: str, spans: Sequence[tuple[int, int]]) -> str | None:
    """Return the smallest span of complete Oracle lines covering every one of these spans."""
    if not spans:
        return None
    start = oracle_text.rfind("\n", 0, min(span[0] for span in spans)) + 1
    end = oracle_text.find("\n", max(span[1] for span in spans))
    return oracle_text[start:] if end < 0 else oracle_text[start:end]


def _statement(paragraph: str, start: int, end: int) -> str:
    """Return the exact sentence text one match occupies inside its own window."""
    span = _statement_span(paragraph, start, end)
    return paragraph[span[0] : span[1]]


def _evidence_window(
    *,
    oracle_text: str,
    capability: CardCapability,
    quote: str,
) -> str | None:
    """Return the unique smallest complete-line window citing a quote and a capability quote.

    A cited prerequisite may be a fragment of the ability a capability quotes, so the window is
    taken from the capability's own exact evidence rather than from a physical line: the smaller
    of the two windows whose complete-line span already holds the other quote.
    """
    selected = oracle_evidence_window(oracle_text=oracle_text, quote=quote)
    if selected is None:
        return None
    candidates: set[str] = set()
    if any(item.quote in selected for item in capability.evidence):
        candidates.add(selected)
    for item in capability.evidence:
        window = oracle_evidence_window(oracle_text=oracle_text, quote=item.quote)
        if window is not None and quote in window:
            candidates.add(window)
    if not candidates:
        return None
    smallest = min(candidates, key=len)
    if sum(1 for window in candidates if len(window) == len(smallest)) != 1:
        return None
    return smallest


def _retained_qualification(
    *,
    capability: CardCapability,
    prerequisite: CapabilityPrerequisite,
    oracle_text: str,
) -> RelationshipQualification | None:
    """Retain one untypable prerequisite verbatim inside its own cited paragraph, or None."""
    quote = prerequisite.evidence.quote
    paragraph = _evidence_window(oracle_text=oracle_text, capability=capability, quote=quote)
    if paragraph is None:
        return None
    return _qualification(
        kind=_QUALIFICATION_KINDS[prerequisite.kind],
        capability=capability,
        evidence=paragraph,
        selector=quote,
    )


def _qualification(
    *,
    kind: QualificationKind,
    capability: CardCapability,
    evidence: str,
    selector: str,
) -> RelationshipQualification | None:
    """Retain one exact statement of a capability's own cited window, or None when ambiguous."""
    if len(_occurrences(evidence, selector)) != 1:
        return None
    try:
        return RelationshipQualification(
            kind=kind,
            evidence=OracleEvidence(
                card_id=capability.card_id,
                face_index=capability.face_index,
                quote=evidence,
            ),
            selector=selector,
            occurrence=0,
        )
    except SemanticEnrichmentError:
        return None


def _compile_bound_clause(
    *,
    capability: CardCapability,
    other: CardCapability,
    prerequisite: CapabilityPrerequisite,
    index: int,
    oracle_text: str,
) -> RelationshipPrerequisite | None:
    """Compile the clause one retained coarse prerequisite binds, or None when it cannot bind one."""
    quote = prerequisite.evidence.quote
    paragraph = _evidence_window(oracle_text=oracle_text, capability=capability, quote=quote)
    if paragraph is None:
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
    oracle_text: str,
) -> RelationshipPrerequisite | None:
    """Compile the clause a capability declares without any coarse prerequisite, or None."""
    accepted: set[RelationshipPrerequisite] = set()
    for evidence in capability.evidence:
        paragraph = _evidence_window(
            oracle_text=oracle_text,
            capability=capability,
            quote=evidence.quote,
        )
        if paragraph is None:
            continue
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
