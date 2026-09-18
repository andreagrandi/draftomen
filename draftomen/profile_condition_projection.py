"""Compile source-bound draft-potential condition maps from a pinned enrichment artifact.

The compiler enumerates every pinned card face once and derives only the bounded helper and payoff
capabilities the three frozen condition families state in their own printed text: landfall land
entries, ferocious power thresholds, and storied qualifying permanents.  The artifact's pins, the
card database and the frozen Oracle text are the only inputs; no derived node borrows another
card's or face's text, and no provider, guide, card refresh or network call takes part.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from draftomen.carddb import CardDatabase
from draftomen.semantic_capability_records import CapabilityQuantity, QuantityRelation
from draftomen.semantic_condition_records import (
    CONDITION_MAP_SCHEMA_VERSION,
    ConditionCapability,
    ConditionController,
    ConditionEvidence,
    ConditionFamily,
    ConditionInteraction,
    ConditionMap,
    ConditionRole,
    ConditionSource,
    ConditionSupport,
)
from draftomen.semantic_enrichment import SemanticEnrichmentArtifact, card_source_sha256
from draftomen.semantic_enrichment_records import FindingStatus, SemanticEnrichmentError
from draftomen.semantic_relationship_records import (
    QualificationKind,
    _COUNT_WORDS,
    _resolved_span,
    _spans_overlap,
)

__all__ = ["compile_condition_map", "condition_statements"]

_LANDFALL_ENTRY_CLAUSE = "Whenever a land you control enters"
_ENDURING_STORY_CLAUSE = "you have an enduring story"
_STORIED_REMINDER = (
    "Storied (If you control three or more artifacts, legendaries, and/or Sagas, "
    "you have an enduring story for the rest of the game.)"
)
_STORIED_THRESHOLD = 3

_LANDFALL_KEYWORD = re.compile(r"\bLandfall\b")
_FEROCIOUS_KEYWORD = re.compile(r"\bFerocious\b")
_STORIED_KEYWORD = re.compile(r"\bStoried\b")
_FEROCIOUS_LABEL = re.compile(r"\AFerocious\s*[—–-]\s*")
_FEROCIOUS_PREDICATE = re.compile(
    r"\byou control a creature with power (?P<power>\d+) or greater\b"
)
# The complete attack/combat windows the frozen ferocious abilities print.
_FEROCIOUS_WINDOWS: tuple[str, ...] = (
    "Whenever this creature attacks",
    "Whenever you attack",
    "At the beginning of combat on your turn",
)
_MODAL_FRAME = re.compile(r"choose (?:one|two|three|four|any number)\s*—")
_OPTIONAL_PAYMENT = re.compile(r"you may pay (?:\{[^}]+\})+")
_SENTENCE_BOUNDARY = re.compile(r"[.\n]")
_BULLET_PREFIX = "•"

# Land-entry grammar: one instruction that moves a land card onto the battlefield.  The object the
# instruction moves is either a land-naming phrase or a pronoun whose land antecedent is stated in
# the same sentence, so tutoring, recursion to hand and mana production never become entries.
_LAND_MOVE = re.compile(
    r"\b(?:put|puts|return|returns)\b(?P<object>[^.]*?)\b(?:onto|to) the battlefield\b",
    re.IGNORECASE,
)
_LAND_CARD_ANTECEDENT = re.compile(
    r"\b(?:(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|x|\d+|each|up to \w+)\s+)?"
    r"(?:basic\s+)?(?:land|Forest|Mountain|Island|Swamp|Plains) cards?\b",
    re.IGNORECASE,
)
_ADDITIONAL_LAND_PLAY = re.compile(
    r"\bplay (?P<count>\d+|an|one|two|three|four|five|X|any number of) additional lands?\b",
    re.IGNORECASE,
)
_OTHER_PLAYER_PATTERN = re.compile(
    r"\b(?:target opponent|each opponent|an opponent|defending player|"
    r"its controller|their controller|that player|each player|they)\b",
    re.IGNORECASE,
)

# Ferocious helper grammar: fixed printed power, explicitly created creature tokens, and the
# closed amass instruction with its printed N or X.
_CREATURE_TYPE_WORD = re.compile(r"\bCreature\b")
_POSITIVE_INTEGER = re.compile(r"[0-9]+")
_CREATED_CREATURE_TOKEN = re.compile(
    r"\bcreat(?:e|es)\s+[^.]*?(?P<power>\d+)/\d+[^.]*?\bcreature tokens?\b", re.IGNORECASE
)
_AMASS = re.compile(
    r"\bamass(?:es)?\s+(?P<subtype>[A-Za-z][A-Za-z'’-]*)\s+(?P<count>\d+|X)\b", re.IGNORECASE
)

# Storied helper grammar: type-line eligibility and explicitly created qualifying permanents.
_QUALIFYING_WORDS: tuple[str, ...] = ("Artifact", "Legendary", "Saga")
_CREATED_TOKEN_CLAUSE = re.compile(r"\bcreat(?:e|es)\s+(?P<body>[^.;]*?)\btokens?\b", re.IGNORECASE)
_LEADING_COUNT = re.compile(
    r"\A(?P<count>[A-Za-z]+|\d+|any number of|that many)\b", re.IGNORECASE
)
_ARTIFACT_WORD = re.compile(r"\bartifact\b", re.IGNORECASE)
_LEGENDARY_WORD = re.compile(r"\blegendary\b", re.IGNORECASE)
_SAGA_WORD = re.compile(r"\bSaga\b")
_TREASURE_WORD = re.compile(r"\bTreasure\b")
_TREASURE_DEFINITION = re.compile(r"\b(?:is|it's|they're)\s+an artifact\b", re.IGNORECASE)
_VARIABLE_COUNTS: frozenset[str] = frozenset({"x", "any number of", "that many", "for each"})

_LANDFALL_SUPPORTS: Mapping[str, ConditionSupport] = {
    "additional_land_play": "contributes",
    "land_card": "can_enable",
    "land_entry": "can_enable",
}


def compile_condition_map(
    *,
    artifact: SemanticEnrichmentArtifact,
    card_database: CardDatabase,
) -> ConditionMap | None:
    """Compile one artifact's pinned card faces into a derived draft-potential condition map."""
    if not isinstance(artifact, SemanticEnrichmentArtifact):
        raise SemanticEnrichmentError("artifact must be a SemanticEnrichmentArtifact record.")
    if not isinstance(card_database, CardDatabase):
        raise SemanticEnrichmentError("card_database must be a CardDatabase record.")
    faces = _pinned_faces(artifact=artifact, card_database=card_database)
    facts = _accepted_fact_quotes(artifact=artifact, card_database=card_database)
    sources: dict[tuple[int, int | None], ConditionSource] = {}
    capabilities: dict[str, ConditionCapability] = {}
    for face in faces:
        nodes = _face_capabilities(face=face, facts=facts)
        if not nodes:
            continue
        sources[(face.card_id, face.face_index)] = _condition_source(face=face)
        capabilities.update({node.capability_id: node for node in nodes})
    stored = tuple(capabilities.values())
    if not any(capability.role == "payoff" for capability in stored):
        return None
    return ConditionMap(
        schema_version=CONDITION_MAP_SCHEMA_VERSION,
        scope="draft_potential",
        sources=tuple(sources.values()),
        capabilities=stored,
        interactions=_interactions(capabilities=stored),
    )


def condition_statements(
    *,
    oracle_text: str,
    quote: str,
) -> tuple[tuple[QualificationKind, str], ...] | None:
    """Return the condition-family statements one quoted ability cites, exactly as printed.

    The empty tuple means the quotation cites no landfall, ferocious or storied condition.  A
    recognised condition ability returns one exact ``(kind, selector)`` statement per retained
    printed statement, in printed order, with every selector an exact substring of ``oracle_text``:
    the complete ability window under ``CONDITION``, its choice frame under ``CHOICE``, its optional
    payment under ``COST``, its attack or combat window under ``TIMING`` and its retained power
    threshold under ``QUANTITY``.  The storied reminder is a ``CONDITION`` statement of its own.
    ``None`` means the quotation names a condition-family cue whose required definition - the
    land-entry clause, the power predicate or the storied reminder - does not bind inside the same
    face, or binds ambiguously.
    """
    if not isinstance(oracle_text, str) or not isinstance(quote, str):
        return ()
    if not oracle_text or not quote or quote not in oracle_text:
        return ()
    for family, state, cued in (
        ("landfall", _landfall_state(oracle_text=oracle_text), _landfall_cue(quote=quote)),
        ("ferocious", _ferocious_state(oracle_text=oracle_text), _ferocious_cue(quote=quote)),
        ("storied", _storied_state(oracle_text=oracle_text), _storied_cue(quote=quote)),
    ):
        overlapping = _overlapping_abilities(
            abilities=state.abilities,
            oracle_text=oracle_text,
            quote=quote,
        )
        if len(overlapping) == 1:
            return overlapping[0].statements
        if len(overlapping) > 1:
            return None
        if not cued:
            continue
        if state.unbound and _family_keyword(family).search(quote) is not None:
            return None
        if len(state.abilities) == 1:
            return state.abilities[0].statements
        return None
    return ()


@dataclass(frozen=True, slots=True)
class _PinnedFace:
    """One pinned card face with the printed fields the condition rules read."""

    card_id: int
    face_index: int | None
    name: str
    type_line: str | None
    oracle_text: str | None
    power: str | None
    card_source_sha256: str

    @property
    def label(self) -> str:
        """Return the face's context label for diagnostics."""
        return "single" if self.face_index is None else str(self.face_index)


@dataclass(frozen=True, slots=True)
class _NodeSpec:
    """One derived condition capability before its identity and provenance are computed."""

    family: ConditionFamily
    role: ConditionRole
    kind: str
    controller: ConditionController
    quantity: CapabilityQuantity | None
    evidence: tuple[ConditionEvidence, ...]


@dataclass(frozen=True, slots=True)
class _ConditionAbility:
    """One recognised condition-family ability with the printed statements it retains."""

    family: ConditionFamily
    window: str
    statements: tuple[tuple[QualificationKind, str], ...]


@dataclass(frozen=True, slots=True)
class _FamilyState:
    """One face's recognised condition-family abilities and its unbound printed keywords."""

    abilities: tuple[_ConditionAbility, ...]
    unbound: tuple[str, ...]


def _pinned_faces(
    *,
    artifact: SemanticEnrichmentArtifact,
    card_database: CardDatabase,
) -> tuple[_PinnedFace, ...]:
    """Return every pinned card face once, after the artifact and card-source gates pass."""
    faces: list[_PinnedFace] = []
    for pin in artifact.cards:
        card = card_database.cards.get(pin.card_id)
        if card is None or card.unknown:
            raise SemanticEnrichmentError(
                f"condition card {pin.card_id} is missing from the card database."
            )
        if card_source_sha256(card) != pin.sha256:
            raise SemanticEnrichmentError(
                f"condition card {pin.card_id} does not match its artifact card-source pin."
            )
        if card.faces:
            for index, face in enumerate(card.faces):
                if not 0 <= index < len(card.faces):
                    raise SemanticEnrichmentError(
                        f"condition card {pin.card_id} face {index} is outside the card's faces."
                    )
                faces.append(
                    _PinnedFace(
                        card_id=card.grp_id,
                        face_index=index,
                        name=face.name or card.name,
                        type_line=face.type_line,
                        oracle_text=face.oracle_text,
                        power=face.power,
                        card_source_sha256=pin.sha256,
                    )
                )
            continue
        faces.append(
            _PinnedFace(
                card_id=card.grp_id,
                face_index=None,
                name=card.name,
                type_line=card.type_line,
                oracle_text=card.oracle_text,
                power=card.power,
                card_source_sha256=pin.sha256,
            )
        )
    return tuple(faces)


def _accepted_fact_quotes(
    *,
    artifact: SemanticEnrichmentArtifact,
    card_database: CardDatabase,
) -> Mapping[tuple[int, int | None], tuple[tuple[str, str], ...]]:
    """Index accepted Oracle facts once, by card face, for capability provenance."""
    index: dict[tuple[int, int | None], list[tuple[str, str]]] = {}
    for fact in artifact.oracle_facts:
        if fact.review.status is not FindingStatus.ACCEPTED:
            continue
        card = card_database.cards.get(fact.card_id)
        if card is None or card.unknown:
            continue
        for evidence in fact.evidence:
            if evidence.card_id != fact.card_id:
                continue
            if card.faces:
                if evidence.face_index is None:
                    raise SemanticEnrichmentError(
                        f"Oracle fact {fact.finding_id} requires a face index for card {fact.card_id}."
                    )
                if not 0 <= evidence.face_index < len(card.faces):
                    raise SemanticEnrichmentError(
                        f"Oracle fact {fact.finding_id} names face {evidence.face_index} "
                        f"outside the faces of card {fact.card_id}."
                    )
            elif evidence.face_index is not None:
                raise SemanticEnrichmentError(
                    f"Oracle fact {fact.finding_id} names a face for faceless card {fact.card_id}."
                )
            index.setdefault((fact.card_id, evidence.face_index), []).append(
                (fact.finding_id, evidence.quote)
            )
    return {key: tuple(value) for key, value in index.items()}


def _condition_source(*, face: _PinnedFace) -> ConditionSource:
    """Build one pinned source whose fingerprint covers its copied printed fields."""
    return ConditionSource.create(
        card_id=face.card_id,
        face_index=face.face_index,
        card_source_sha256=face.card_source_sha256,
        type_line=face.type_line,
        oracle_text=face.oracle_text,
        power=face.power,
    )


def _face_capabilities(
    *,
    face: _PinnedFace,
    facts: Mapping[tuple[int, int | None], tuple[tuple[str, str], ...]],
) -> tuple[ConditionCapability, ...]:
    """Return every bounded capability one pinned face states, with its provenance."""
    oracle_text = face.oracle_text or ""
    specs: list[_NodeSpec] = []
    specs.extend(_landfall_specs(face=face, oracle_text=oracle_text))
    specs.extend(_ferocious_specs(face=face, oracle_text=oracle_text))
    specs.extend(_storied_specs(face=face, oracle_text=oracle_text))
    if not specs:
        return ()
    source = _condition_source(face=face)
    nodes: dict[str, ConditionCapability] = {}
    for spec in specs:
        finding_ids = _finding_ids(
            facts=facts,
            face=face,
            evidence=spec.evidence,
        )
        capability = ConditionCapability.create(
            source=source,
            family=spec.family,
            role=spec.role,
            kind=spec.kind,
            controller=spec.controller,
            quantity=spec.quantity,
            evidence=spec.evidence,
            source_finding_ids=finding_ids,
        )
        nodes.setdefault(capability.capability_id, capability)
    return tuple(nodes.values())


def _finding_ids(
    *,
    facts: Mapping[tuple[int, int | None], tuple[tuple[str, str], ...]],
    face: _PinnedFace,
    evidence: tuple[ConditionEvidence, ...],
) -> tuple[str, ...]:
    """Return the accepted facts whose same-face evidence overlaps this capability's window."""
    oracle_text = face.oracle_text or ""
    selected: list[str] = []
    for finding_id, quote in facts.get((face.card_id, face.face_index), ()):
        fact_span = _resolved_span(quote=oracle_text, selector=quote, occurrence=0)
        if fact_span is None:
            continue
        for item in evidence:
            if item.field != "oracle_text":
                continue
            item_span = _resolved_span(
                quote=oracle_text,
                selector=item.selector,
                occurrence=item.occurrence,
            )
            if item_span is not None and _spans_overlap(fact_span, item_span):
                selected.append(finding_id)
                break
    return tuple(dict.fromkeys(selected))


def _landfall_specs(*, face: _PinnedFace, oracle_text: str) -> tuple[_NodeSpec, ...]:
    """Return one face's landfall payoffs and its printed land-entry helpers."""
    state = _landfall_state(oracle_text=oracle_text)
    _require_bound(state=state, face=face)
    specs: list[_NodeSpec] = [
        _NodeSpec(
            family="landfall",
            role="payoff",
            kind="land_entry_event",
            controller="you",
            quantity=None,
            evidence=_oracle_evidence(statements=ability.statements),
        )
        for ability in state.abilities
    ]
    specs.extend(_land_entry_specs(face=face, oracle_text=oracle_text))
    specs.extend(_additional_land_specs(face=face, oracle_text=oracle_text))
    land_word = _land_card_word(face=face)
    if land_word is not None:
        specs.append(
            _NodeSpec(
                family="landfall",
                role="enabler",
                kind="land_card",
                controller="you",
                quantity=CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
                evidence=(
                    ConditionEvidence(
                        field="type_line",
                        kind=QualificationKind.PARTY,
                        selector=land_word,
                        occurrence=0,
                    ),
                ),
            )
        )
    return tuple(specs)


def _land_entry_specs(*, face: _PinnedFace, oracle_text: str) -> tuple[_NodeSpec, ...]:
    """Return one face's explicit land-onto-the-battlefield instructions as land-entry helpers."""
    specs: list[_NodeSpec] = []
    for match in _LAND_MOVE.finditer(oracle_text):
        if _in_reminder(text=oracle_text, index=match.start()):
            continue
        start, end = _sentence_span(text=oracle_text, index=match.start())
        sentence = oracle_text[start:end]
        prefix = oracle_text[start : match.start()]
        moved = _moved_land(move=match, prefix=prefix)
        if moved is None:
            continue
        specs.append(
            _NodeSpec(
                family="landfall",
                role="enabler",
                kind="land_entry",
                controller=_controller(prefix=prefix),
                quantity=_moved_land_quantity(moved=moved),
                evidence=_oracle_evidence(
                    statements=((QualificationKind.CONDITION, sentence),),
                ),
            )
        )
    return tuple(specs)


def _moved_land(*, move: re.Match[str], prefix: str) -> str | None:
    """Return the land phrase one move instruction carries, or None when it moves no land."""
    moved = _moved_object(move=move)
    if _LAND_CARD_ANTECEDENT.search(moved) is not None:
        return moved
    antecedent = _LAND_CARD_ANTECEDENT.search(prefix)
    if antecedent is None:
        return None
    return antecedent.group(0)


def _moved_object(*, move: re.Match[str]) -> str:
    """Return the object phrase one move instruction carries, without its source zone."""
    return re.split(r"\s+from\s+", move.group("object").strip(), maxsplit=1)[0]


def _moved_land_quantity(*, moved: str) -> CapabilityQuantity | None:
    """Return the land count one entry instruction moves, or its stated variable count."""
    folded = moved.casefold()
    if "any number of" in folded or "that many" in folded or re.search(r"\bx\b", folded):
        return CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)
    up_to = re.search(r"\bup to (?P<count>[A-Za-z]+|\d+)\b", moved, re.IGNORECASE)
    if up_to is not None:
        count = _count_value(text=up_to.group("count"))
        if count is not None:
            return CapabilityQuantity(value=count, relation=QuantityRelation.AT_MOST)
    leading = _LEADING_COUNT.match(moved)
    count = None if leading is None else _count_value(text=leading.group("count"))
    if count is not None:
        return CapabilityQuantity(value=count, relation=QuantityRelation.EXACTLY)
    return None


def _additional_land_specs(*, face: _PinnedFace, oracle_text: str) -> tuple[_NodeSpec, ...]:
    """Return one face's explicit additional-land permissions as partial helpers."""
    specs: list[_NodeSpec] = []
    for match in _ADDITIONAL_LAND_PLAY.finditer(oracle_text):
        if _in_reminder(text=oracle_text, index=match.start()):
            continue
        start, end = _sentence_span(text=oracle_text, index=match.start())
        sentence = oracle_text[start:end]
        printed = match.group("count").casefold()
        if printed in _VARIABLE_COUNTS:
            quantity = CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)
        elif printed in _COUNT_WORDS:
            quantity = CapabilityQuantity(
                value=_COUNT_WORDS[printed], relation=QuantityRelation.EXACTLY
            )
        elif _POSITIVE_INTEGER.fullmatch(printed) is not None:
            quantity = CapabilityQuantity(
                value=int(printed), relation=QuantityRelation.EXACTLY
            )
        else:
            continue
        specs.append(
            _NodeSpec(
                family="landfall",
                role="enabler",
                kind="additional_land_play",
                controller=_controller(prefix=oracle_text[start : match.start()]),
                quantity=quantity,
                evidence=_oracle_evidence(
                    statements=((QualificationKind.CONDITION, sentence),),
                ),
            )
        )
    return tuple(specs)


def _ferocious_specs(*, face: _PinnedFace, oracle_text: str) -> tuple[_NodeSpec, ...]:
    """Return one face's ferocious payoffs and its printed power helpers."""
    state = _ferocious_state(oracle_text=oracle_text)
    _require_bound(state=state, face=face)
    payoffs: list[_NodeSpec] = []
    for ability in state.abilities:
        predicate = _FEROCIOUS_PREDICATE.search(ability.window)
        if predicate is None:
            raise SemanticEnrichmentError(
                f"card {face.card_id} ({face.name}) face {face.label} prints Ferocious "
                "without its power predicate."
            )
        payoffs.append(
            _NodeSpec(
                family="ferocious",
                role="payoff",
                kind="power_threshold",
                controller="you",
                quantity=CapabilityQuantity(
                    value=int(predicate.group("power")), relation=QuantityRelation.AT_LEAST
                ),
                evidence=_oracle_evidence(statements=ability.statements),
            )
        )
    specs: list[_NodeSpec] = list(payoffs)
    power = _creature_power(face=face)
    if power is not None:
        specs.append(
            _NodeSpec(
                family="ferocious",
                role="enabler",
                kind="creature_power",
                controller="you",
                quantity=CapabilityQuantity(value=power, relation=QuantityRelation.EXACTLY),
                evidence=(
                    ConditionEvidence(
                        field="power",
                        kind=QualificationKind.QUANTITY,
                        selector=face.power or "",
                        occurrence=0,
                    ),
                ),
            )
        )
    specs.extend(_created_creature_specs(oracle_text=oracle_text))
    specs.extend(_amass_specs(oracle_text=oracle_text))
    return tuple(specs)


def _created_creature_specs(*, oracle_text: str) -> tuple[_NodeSpec, ...]:
    """Return one face's explicit fixed-power creature token creation as power helpers."""
    specs: list[_NodeSpec] = []
    for match in _CREATED_CREATURE_TOKEN.finditer(oracle_text):
        if _in_reminder(text=oracle_text, index=match.start()):
            continue
        power = int(match.group("power"))
        if power < 1:
            continue
        line_start, line_end = _line_span(text=oracle_text, index=match.start())
        sentence_start, _ = _sentence_span(text=oracle_text, index=match.start())
        specs.append(
            _NodeSpec(
                family="ferocious",
                role="enabler",
                kind="created_creature_power",
                controller=_controller(prefix=oracle_text[sentence_start : match.start()]),
                quantity=CapabilityQuantity(value=power, relation=QuantityRelation.EXACTLY),
                evidence=_oracle_evidence(
                    statements=(
                        (QualificationKind.CONDITION, oracle_text[line_start:line_end]),
                        (QualificationKind.QUANTITY, match.group(0)),
                    )
                ),
            )
        )
    return tuple(specs)


def _amass_specs(*, oracle_text: str) -> tuple[_NodeSpec, ...]:
    """Return one face's separate amass instructions, each with its own printed growth."""
    specs: list[_NodeSpec] = []
    for match in _AMASS.finditer(oracle_text):
        if _in_reminder(text=oracle_text, index=match.start()):
            continue
        printed = match.group("count").casefold()
        if printed in _VARIABLE_COUNTS or printed == "x":
            quantity = CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)
        elif _POSITIVE_INTEGER.fullmatch(printed) is not None and int(printed) > 0:
            quantity = CapabilityQuantity(value=int(printed), relation=QuantityRelation.EXACTLY)
        else:
            continue
        line_start, line_end = _line_span(text=oracle_text, index=match.start())
        sentence_start, _ = _sentence_span(text=oracle_text, index=match.start())
        specs.append(
            _NodeSpec(
                family="ferocious",
                role="enabler",
                kind="amass_growth",
                controller=_controller(prefix=oracle_text[sentence_start : match.start()]),
                quantity=quantity,
                evidence=_oracle_evidence(
                    statements=(
                        (QualificationKind.CONDITION, oracle_text[line_start:line_end]),
                        (QualificationKind.QUANTITY, match.group(0)),
                    )
                ),
            )
        )
    return tuple(specs)


def _storied_specs(*, face: _PinnedFace, oracle_text: str) -> tuple[_NodeSpec, ...]:
    """Return one face's storied payoffs and its printed qualifying permanents."""
    state = _storied_state(oracle_text=oracle_text)
    _require_bound(state=state, face=face)
    specs: list[_NodeSpec] = [
        _NodeSpec(
            family="storied",
            role="payoff",
            kind="storied_attainment",
            controller="you",
            quantity=CapabilityQuantity(
                value=_STORIED_THRESHOLD, relation=QuantityRelation.AT_LEAST
            ),
            evidence=_oracle_evidence(statements=ability.statements),
        )
        for ability in state.abilities
    ]
    words = _qualifying_words(face=face)
    if words:
        specs.append(
            _NodeSpec(
                family="storied",
                role="enabler",
                kind="qualifying_permanent",
                controller="you",
                quantity=CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
                evidence=tuple(
                    ConditionEvidence(
                        field="type_line",
                        kind=QualificationKind.PARTY,
                        selector=word,
                        occurrence=0,
                    )
                    for word in words
                ),
            )
        )
    specs.extend(_created_permanent_specs(oracle_text=oracle_text))
    return tuple(specs)


def _created_permanent_specs(*, oracle_text: str) -> tuple[_NodeSpec, ...]:
    """Return one face's explicit artifact, legendary or Saga token creation as partial helpers."""
    defines_treasure = (
        _TREASURE_WORD.search(oracle_text) is not None
        and _TREASURE_DEFINITION.search(oracle_text) is not None
    )
    specs: list[_NodeSpec] = []
    for match in _CREATED_TOKEN_CLAUSE.finditer(oracle_text):
        if _in_reminder(text=oracle_text, index=match.start()):
            continue
        body = match.group("body")
        count = _LEADING_COUNT.match(body)
        if count is None:
            continue
        printed = count.group("count").casefold()
        if printed in _VARIABLE_COUNTS or printed == "x":
            quantity = CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)
        elif printed in _COUNT_WORDS:
            quantity = CapabilityQuantity(
                value=_COUNT_WORDS[printed], relation=QuantityRelation.EXACTLY
            )
        elif _POSITIVE_INTEGER.fullmatch(printed) is not None and int(printed) > 0:
            quantity = CapabilityQuantity(value=int(printed), relation=QuantityRelation.EXACTLY)
        else:
            continue
        descriptor = body[count.end() :]
        qualifies = (
            _ARTIFACT_WORD.search(descriptor) is not None
            or _LEGENDARY_WORD.search(descriptor) is not None
            or _SAGA_WORD.search(descriptor) is not None
            or (defines_treasure and _TREASURE_WORD.search(descriptor) is not None)
        )
        if not qualifies:
            continue
        line_start, line_end = _line_span(text=oracle_text, index=match.start())
        sentence_start, _ = _sentence_span(text=oracle_text, index=match.start())
        specs.append(
            _NodeSpec(
                family="storied",
                role="enabler",
                kind="created_qualifying_permanents",
                controller=_controller(prefix=oracle_text[sentence_start : match.start()]),
                quantity=quantity,
                evidence=_oracle_evidence(
                    statements=(
                        (QualificationKind.CONDITION, oracle_text[line_start:line_end]),
                        (QualificationKind.QUANTITY, match.group(0)),
                    )
                ),
            )
        )
    return tuple(specs)


def _interactions(
    *,
    capabilities: Sequence[ConditionCapability],
) -> tuple[ConditionInteraction, ...]:
    """Join each own-controlled enabler to the payoffs of its own family it supports."""
    payoffs: dict[str, list[ConditionCapability]] = {}
    for capability in capabilities:
        if capability.role == "payoff":
            payoffs.setdefault(capability.family, []).append(capability)
    edges: dict[tuple[str, str], ConditionInteraction] = {}
    for enabler in capabilities:
        if enabler.role != "enabler" or enabler.controller != "you":
            continue
        for payoff in payoffs.get(enabler.family, ()):
            support = _support(enabler=enabler, payoff=payoff)
            if support is None:
                continue
            edges[(enabler.capability_id, payoff.capability_id)] = ConditionInteraction(
                enabler_id=enabler.capability_id,
                payoff_id=payoff.capability_id,
                support=support,
            )
    return tuple(edges.values())


def _support(
    *,
    enabler: ConditionCapability,
    payoff: ConditionCapability,
) -> ConditionSupport | None:
    """Return the support one own-controlled helper supplies toward one payoff, or None."""
    if payoff.quantity is None or enabler.quantity is None:
        if enabler.family == "landfall":
            return _LANDFALL_SUPPORTS[enabler.kind]
        return None
    threshold = payoff.quantity.value or 0
    sufficient = (
        enabler.quantity.relation is QuantityRelation.EXACTLY
        and enabler.quantity.value is not None
        and enabler.quantity.value >= threshold
    )
    if enabler.family == "landfall":
        return _LANDFALL_SUPPORTS[enabler.kind]
    if enabler.kind in ("creature_power", "created_creature_power"):
        return "can_enable" if sufficient else None
    return "can_enable" if sufficient else "contributes"


def _landfall_state(*, oracle_text: str) -> _FamilyState:
    """Recognise every landfall ability one face prints with its own entry clause."""
    abilities: list[_ConditionAbility] = []
    unbound: list[str] = []
    for match in _LANDFALL_KEYWORD.finditer(oracle_text):
        start, end = _ability_span(text=oracle_text, index=match.start())
        window = oracle_text[start:end]
        clause = window.find(_LANDFALL_ENTRY_CLAUSE)
        if clause < 0 or clause < match.start() - start:
            unbound.append("Landfall")
            continue
        statements: list[tuple[QualificationKind, str]] = [(QualificationKind.CONDITION, window)]
        frame = _MODAL_FRAME.search(window)
        if frame is not None:
            statements.append((QualificationKind.CHOICE, frame.group(0)))
        payment = _OPTIONAL_PAYMENT.search(window)
        if payment is not None:
            sentence_start, sentence_end = _sentence_span(text=window, index=payment.start())
            statements.append(
                (QualificationKind.COST, window[sentence_start:sentence_end])
            )
        abilities.append(
            _ConditionAbility(
                family="landfall",
                window=window,
                statements=tuple(statements),
            )
        )
    return _FamilyState(
        abilities=tuple(dict.fromkeys(abilities)),
        unbound=tuple(dict.fromkeys(unbound)),
    )


def _ferocious_state(*, oracle_text: str) -> _FamilyState:
    """Recognise every ferocious ability one face prints with its own power predicate."""
    abilities: list[_ConditionAbility] = []
    unbound: list[str] = []
    for match in _FEROCIOUS_KEYWORD.finditer(oracle_text):
        line_start, line_end = _line_span(text=oracle_text, index=match.start())
        window = oracle_text[line_start:line_end]
        predicate = _FEROCIOUS_PREDICATE.search(window)
        timing = next((phrase for phrase in _FEROCIOUS_WINDOWS if phrase in window), None)
        if _FEROCIOUS_LABEL.match(window) is None or predicate is None or timing is None:
            unbound.append("Ferocious")
            continue
        abilities.append(
            _ConditionAbility(
                family="ferocious",
                window=window,
                statements=(
                    (QualificationKind.CONDITION, window),
                    (QualificationKind.TIMING, timing),
                    (QualificationKind.QUANTITY, predicate.group(0)),
                ),
            )
        )
    return _FamilyState(
        abilities=tuple(dict.fromkeys(abilities)),
        unbound=tuple(dict.fromkeys(unbound)),
    )


def _storied_state(*, oracle_text: str) -> _FamilyState:
    """Recognise every storied clause one face binds to the printed definition."""
    reminder = oracle_text.find(_STORIED_REMINDER)
    unbound: tuple[str, ...] = ()
    if reminder < 0:
        if _STORIED_KEYWORD.search(oracle_text) is not None:
            unbound = ("Storied",)
        return _FamilyState(abilities=(), unbound=unbound)
    abilities: list[_ConditionAbility] = []
    for match in re.finditer(re.escape(_ENDURING_STORY_CLAUSE), oracle_text):
        if reminder <= match.start() < reminder + len(_STORIED_REMINDER):
            continue
        start, end = _sentence_span(text=oracle_text, index=match.start())
        clause = oracle_text[start:end]
        abilities.append(
            _ConditionAbility(
                family="storied",
                window=clause,
                statements=(
                    (QualificationKind.CONDITION, _STORIED_REMINDER),
                    (QualificationKind.CONDITION, clause),
                ),
            )
        )
    return _FamilyState(
        abilities=tuple(dict.fromkeys(abilities)),
        unbound=unbound,
    )


def _require_bound(*, state: _FamilyState, face: _PinnedFace) -> None:
    """Fail one face that prints a condition-family keyword without its required definition."""
    if not state.unbound:
        return
    printed = ", ".join(state.unbound)
    raise SemanticEnrichmentError(
        f"card {face.card_id} ({face.name}) face {face.label} prints {printed} "
        "without its required condition definition."
    )


def _overlapping_abilities(
    *,
    abilities: Sequence[_ConditionAbility],
    oracle_text: str,
    quote: str,
) -> tuple[_ConditionAbility, ...]:
    """Return the recognised abilities one quotation cites inside its own window."""
    quote_span = _resolved_span(quote=oracle_text, selector=quote, occurrence=0)
    if quote_span is None:
        return ()
    selected: list[_ConditionAbility] = []
    for ability in abilities:
        window_span = _resolved_span(quote=oracle_text, selector=ability.window, occurrence=0)
        if window_span is not None and _spans_overlap(quote_span, window_span):
            selected.append(ability)
    return tuple(selected)


def _landfall_cue(*, quote: str) -> bool:
    """Return whether one quotation names the landfall condition or its own entry clause."""
    return (
        _LANDFALL_KEYWORD.search(quote) is not None
        or _LANDFALL_ENTRY_CLAUSE in quote
    )


def _ferocious_cue(*, quote: str) -> bool:
    """Return whether one quotation names the ferocious condition or its power predicate."""
    return (
        _FEROCIOUS_KEYWORD.search(quote) is not None
        or _FEROCIOUS_PREDICATE.search(quote) is not None
    )


def _storied_cue(*, quote: str) -> bool:
    """Return whether one quotation names the storied condition or an enduring story."""
    return _STORIED_KEYWORD.search(quote) is not None or _ENDURING_STORY_CLAUSE in quote


def _family_keyword(family: str) -> re.Pattern[str]:
    """Return the printed keyword pattern of one condition family."""
    return {
        "landfall": _LANDFALL_KEYWORD,
        "ferocious": _FEROCIOUS_KEYWORD,
        "storied": _STORIED_KEYWORD,
    }[family]


def _creature_power(*, face: _PinnedFace) -> int | None:
    """Return the positive printed power of one creature face, or None when it states none."""
    if face.power is None or face.type_line is None:
        return None
    if _CREATURE_TYPE_WORD.search(face.type_line) is None:
        return None
    printed = face.power.strip()
    if _POSITIVE_INTEGER.fullmatch(printed) is None:
        return None
    value = int(printed)
    return value if value > 0 else None


def _land_card_word(*, face: _PinnedFace) -> str | None:
    """Return the printed Land card-type word of one face's type line, or None."""
    type_line = face.type_line
    if type_line is None:
        return None
    card_types = type_line.split("—")[0]
    match = re.search(r"\bLand\b", card_types)
    return None if match is None else match.group(0)


def _qualifying_words(*, face: _PinnedFace) -> tuple[str, ...]:
    """Return the printed type-line words that make one face a qualifying permanent."""
    type_line = face.type_line
    if type_line is None:
        return ()
    return tuple(
        word for word in _QUALIFYING_WORDS if re.search(rf"\b{word}\b", type_line) is not None
    )


def _controller(*, prefix: str) -> ConditionController:
    """Return the controller one instruction's own printed subject states, or you."""
    if _OTHER_PLAYER_PATTERN.search(prefix) is not None:
        if re.search(
            r"\b(?:target opponent|each opponent|an opponent|defending player)\b",
            prefix,
            re.IGNORECASE,
        ) is not None:
            return "opponent"
        return "any"
    return "you"


def _count_value(*, text: str) -> int | None:
    """Return the positive integer one printed count word or digit states, or None."""
    printed = text.casefold()
    if printed in _COUNT_WORDS:
        return _COUNT_WORDS[printed]
    if _POSITIVE_INTEGER.fullmatch(printed) is not None and int(printed) > 0:
        return int(printed)
    return None


def _oracle_evidence(
    *,
    statements: Sequence[tuple[QualificationKind, str]],
) -> tuple[ConditionEvidence, ...]:
    """Return exact Oracle-text evidence records for one ability's printed statements."""
    return tuple(
        ConditionEvidence(
            field="oracle_text",
            kind=kind,
            selector=selector,
            occurrence=0,
        )
        for kind, selector in statements
    )


def _in_reminder(*, text: str, index: int) -> bool:
    """Return whether one index sits inside a parenthetical reminder."""
    return text.count("(", 0, index) > text.count(")", 0, index)


def _line_span(*, text: str, index: int) -> tuple[int, int]:
    """Return the complete Oracle line one index sits on."""
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return start, len(text) if end < 0 else end


def _ability_span(*, text: str, index: int) -> tuple[int, int]:
    """Return the complete ability block one index sits on, including its own bullet lines."""
    start, end = _line_span(text=text, index=index)
    while end < len(text):
        next_start = end + 1
        next_end = text.find("\n", next_start)
        next_end = len(text) if next_end < 0 else next_end
        if not text[next_start:next_end].startswith(_BULLET_PREFIX):
            break
        end = next_end
    return start, end


def _sentence_span(*, text: str, index: int) -> tuple[int, int]:
    """Return the complete sentence one index sits in, bounded by periods or lines."""
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text, 0, index):
        start = match.end()
    end = len(text)
    for match in _SENTENCE_BOUNDARY.finditer(text, index):
        end = match.end() if match.group(0) == "." else match.start()
        break
    while start < end and text[start] in " \n":
        start += 1
    while end > start and text[end - 1] in " \n":
        end -= 1
    return start, end
