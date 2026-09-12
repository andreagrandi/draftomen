"""Strict immutable records for typed directional relationship prerequisites."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Literal, Self

from draftomen.carddb import CardInfo
from draftomen.semantic_capability_records import (
    CapabilityPrerequisite,
    CapabilityQuantity,
    CapabilityZone,
    PrerequisiteKind,
    QuantityRelation,
    _canonical_json_bytes,
    _enum_from_json,
    _enum_member,
    _optional_record,
)
from draftomen.semantic_enrichment_records import (
    CardSourcePin,
    FindingReview,
    FindingStatus,
    GuideEvidence,
    OracleEvidence,
    SemanticEnrichmentError,
    _canonical,
    _closed_string,
    _exact_text,
    _hash,
    _identifier,
    _integer,
    _json_array,
    _keys,
    _nested_json_array,
    _optional_integer,
    _tuple,
    require_utf8_text,
)
from draftomen.semantic_roles import Role


RELATIONSHIP_PREREQUISITE_PROJECTION_SCHEMA_VERSION = 1
PREREQUISITE_INCOMPLETE_MESSAGE = "relationship prerequisites are incomplete."
PREREQUISITE_CONTRADICTION_MESSAGE = "relationship prerequisites contradict their source evidence."

_SUBJECTS: frozenset[str] = frozenset({"participant", "output", "input", "event"})
_OPERATIONS: frozenset[str] = frozenset(
    {
        "attack",
        "cast",
        "control",
        "count",
        "create",
        "die",
        "discard",
        "draw",
        "enter",
        "leave",
        "mill",
        "none",
        "return",
        "sacrifice",
    }
)
_OBJECT_KINDS: frozenset[str] = frozenset({"card", "permanent", "spell", "token"})
_TYPE_OPERATORS: frozenset[str] = frozenset({"unrestricted", "any_of", "all_of"})
_CARD_TYPES: frozenset[str] = frozenset(
    {
        "artifact",
        "battle",
        "creature",
        "enchantment",
        "instant",
        "kindred",
        "land",
        "planeswalker",
        "sorcery",
    }
)
_TOKEN_RESTRICTIONS: frozenset[str] = frozenset({"unrestricted", "token", "nontoken"})
_EXCLUSIONS: frozenset[str] = frozenset({"none", "ability_source"})
_COLOR_OPERATORS: frozenset[str] = frozenset({"unrestricted", "exact", "any_of", "all_of"})
_COLORS: tuple[str, ...] = ("W", "U", "B", "R", "G")
_COLOR_MEMBERS: frozenset[str] = frozenset(_COLORS)
_CONTROLLERS: frozenset[str] = frozenset({"you", "opponent", "any", "not_applicable"})
RELATIONSHIP_ZONE_PLAYERS: frozenset[str] = frozenset({"you", "opponent", "owner", "any"})
_TIMING_WINDOWS: frozenset[str] = frozenset(
    {
        "unrestricted",
        "upkeep",
        "combat",
        "beginning_of_combat",
        "attack",
        "enters",
        "landfall",
        "main_phase",
        "end_step",
        "dies",
        "sorcery_speed",
        "same_turn",
        "next_turn",
    }
)
_TIMING_TURNS: frozenset[str] = frozenset({"any", "your", "opponent"})

_Subject = Literal["participant", "output", "input", "event"]
_Operation = Literal[
    "create",
    "sacrifice",
    "discard",
    "mill",
    "return",
    "draw",
    "die",
    "control",
    "count",
    "cast",
    "enter",
    "leave",
    "attack",
    "none",
]
_ObjectKind = Literal["card", "permanent", "spell", "token"]
_TypeOperator = Literal["unrestricted", "any_of", "all_of"]
_CardType = Literal[
    "artifact",
    "battle",
    "creature",
    "enchantment",
    "instant",
    "kindred",
    "land",
    "planeswalker",
    "sorcery",
]
_TokenRestriction = Literal["unrestricted", "token", "nontoken"]
_Exclusion = Literal["none", "ability_source"]
_ColorOperator = Literal["unrestricted", "exact", "any_of", "all_of"]
_Color = Literal["W", "U", "B", "R", "G"]
_Controller = Literal["you", "opponent", "any", "not_applicable"]
_ZonePlayer = Literal["you", "opponent", "owner", "any"]
_TimingWindow = Literal[
    "unrestricted",
    "upkeep",
    "combat",
    "beginning_of_combat",
    "attack",
    "enters",
    "landfall",
    "main_phase",
    "end_step",
    "dies",
    "sorcery_speed",
    "same_turn",
    "next_turn",
]
_TimingTurn = Literal["any", "your", "opponent"]


class PrerequisiteProjectionError(SemanticEnrichmentError):
    """Raised when typed relationship prerequisites fail one projection gate."""

    def __init__(self, *, code: Literal["incomplete", "contradiction"]) -> None:
        if code == "incomplete":
            message, resolved = PREREQUISITE_INCOMPLETE_MESSAGE, "incomplete"
        elif code == "contradiction":
            message, resolved = PREREQUISITE_CONTRADICTION_MESSAGE, "contradiction"
        else:
            raise SemanticEnrichmentError("prerequisite projection code is not supported.")
        super().__init__(message)
        self._code = resolved

    @property
    def code(self) -> Literal["incomplete", "contradiction"]:
        """Return the fixed projection failure code."""
        return self._code


@dataclass(frozen=True, slots=True)
class RelationshipZone:
    """One game zone together with the player that owns it."""

    zone: CapabilityZone
    player: _ZonePlayer

    def __post_init__(self) -> None:
        _enum_member(self.zone, "zone", CapabilityZone)
        object.__setattr__(self, "player", _closed_string(self.player, "player", RELATIONSHIP_ZONE_PLAYERS))

    def to_json(self) -> dict[str, object]:
        return {"zone": self.zone.value, "player": self.player}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("relationship zone must be an object.")
        _keys(value, {"zone", "player"}, "relationship zone")
        return cls(
            zone=_enum_from_json(value["zone"], "zone", CapabilityZone),
            player=value["player"],
        )


@dataclass(frozen=True, slots=True)
class RelationshipTiming:
    """One closed timing window with its turn scope and per-turn limit."""

    window: _TimingWindow
    turn: _TimingTurn
    max_per_turn: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "window", _closed_string(self.window, "window", _TIMING_WINDOWS))
        object.__setattr__(self, "turn", _closed_string(self.turn, "turn", _TIMING_TURNS))
        object.__setattr__(
            self,
            "max_per_turn",
            _optional_integer(self.max_per_turn, "max_per_turn", positive=True),
        )

    def to_json(self) -> dict[str, object]:
        return {"window": self.window, "turn": self.turn, "max_per_turn": self.max_per_turn}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("relationship timing must be an object.")
        _keys(value, {"window", "turn", "max_per_turn"}, "relationship timing")
        return cls(
            window=value["window"],
            turn=value["turn"],
            max_per_turn=value["max_per_turn"],
        )


@dataclass(frozen=True, slots=True)
class RelationshipPrerequisite:
    """One complete atomic prerequisite clause bound to exact Oracle evidence."""

    kind: PrerequisiteKind
    subject: _Subject
    operation: _Operation
    object_kind: _ObjectKind
    card_types: tuple[_CardType, ...]
    type_operator: _TypeOperator
    token_restriction: _TokenRestriction
    exclusion: _Exclusion
    subtype: str | None
    color_operator: _ColorOperator
    colors: tuple[_Color, ...]
    controller: _Controller
    owner: _Controller
    quantity: CapabilityQuantity | None
    source_zone: RelationshipZone | None
    destination_zone: RelationshipZone | None
    timing: RelationshipTiming
    required_card_id: int | None
    evidence: OracleEvidence
    operation_quote: str
    operation_occurrence: int
    object_quote: str
    object_occurrence: int
    capability_prerequisite_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        _enum_member(self.kind, "kind", PrerequisiteKind)
        object.__setattr__(self, "subject", _closed_string(self.subject, "subject", _SUBJECTS))
        object.__setattr__(self, "operation", _closed_string(self.operation, "operation", _OPERATIONS))
        object.__setattr__(self, "object_kind", _closed_string(self.object_kind, "object_kind", _OBJECT_KINDS))
        card_types = _tuple(self.card_types, "card_types")
        for card_type in card_types:
            _closed_string(card_type, "card_types", _CARD_TYPES)
        object.__setattr__(
            self,
            "card_types",
            _canonical(card_types, field_name="card_types", key=str),
        )
        object.__setattr__(
            self,
            "type_operator",
            _closed_string(self.type_operator, "type_operator", _TYPE_OPERATORS),
        )
        object.__setattr__(
            self,
            "token_restriction",
            _closed_string(self.token_restriction, "token_restriction", _TOKEN_RESTRICTIONS),
        )
        object.__setattr__(self, "exclusion", _closed_string(self.exclusion, "exclusion", _EXCLUSIONS))
        if self.subtype is not None:
            object.__setattr__(self, "subtype", _identifier(self.subtype, "subtype").casefold())
        object.__setattr__(
            self,
            "color_operator",
            _closed_string(self.color_operator, "color_operator", _COLOR_OPERATORS),
        )
        colors = _tuple(self.colors, "colors")
        for color in colors:
            _closed_string(color, "colors", _COLOR_MEMBERS)
        object.__setattr__(
            self,
            "colors",
            _canonical(colors, field_name="colors", key=_COLORS.index),
        )
        object.__setattr__(self, "controller", _closed_string(self.controller, "controller", _CONTROLLERS))
        object.__setattr__(self, "owner", _closed_string(self.owner, "owner", _CONTROLLERS))
        if self.quantity is not None and not isinstance(self.quantity, CapabilityQuantity):
            raise SemanticEnrichmentError("quantity must be a CapabilityQuantity or null.")
        if self.source_zone is not None and not isinstance(self.source_zone, RelationshipZone):
            raise SemanticEnrichmentError("source_zone must be a RelationshipZone or null.")
        if self.destination_zone is not None and not isinstance(self.destination_zone, RelationshipZone):
            raise SemanticEnrichmentError("destination_zone must be a RelationshipZone or null.")
        if not isinstance(self.timing, RelationshipTiming):
            raise SemanticEnrichmentError("timing must be a RelationshipTiming.")
        object.__setattr__(
            self,
            "required_card_id",
            _optional_integer(self.required_card_id, "required_card_id", positive=True),
        )
        if type(self.evidence) is not OracleEvidence:
            raise SemanticEnrichmentError("evidence must be an OracleEvidence record.")
        object.__setattr__(self, "operation_quote", _exact_text(self.operation_quote, "operation_quote"))
        object.__setattr__(
            self,
            "operation_occurrence",
            _integer(self.operation_occurrence, "operation_occurrence"),
        )
        object.__setattr__(self, "object_quote", _exact_text(self.object_quote, "object_quote"))
        object.__setattr__(self, "object_occurrence", _integer(self.object_occurrence, "object_occurrence"))
        indices = _tuple(self.capability_prerequisite_indices, "capability_prerequisite_indices")
        indices = tuple(
            _integer(index, "capability_prerequisite_indices") for index in indices
        )
        object.__setattr__(
            self,
            "capability_prerequisite_indices",
            _canonical(indices, field_name="capability_prerequisite_indices", key=lambda index: index),
        )
        if (self.type_operator == "unrestricted") != (self.card_types == ()):
            raise SemanticEnrichmentError("type_operator and card_types must agree.")
        if self.object_kind == "token" and self.token_restriction != "token":
            raise SemanticEnrichmentError("token object kinds require the token restriction.")
        if self.color_operator == "unrestricted" and self.colors:
            raise SemanticEnrichmentError("unrestricted color operators require no colors.")
        if self.color_operator in ("any_of", "all_of") and not self.colors:
            raise SemanticEnrichmentError("color alternatives require at least one color.")
        if self.operation == "none" and self.kind is not PrerequisiteKind.CONDITION:
            raise SemanticEnrichmentError("operation none is only valid for conditions.")

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "subject": self.subject,
            "operation": self.operation,
            "object_kind": self.object_kind,
            "card_types": list(self.card_types),
            "type_operator": self.type_operator,
            "token_restriction": self.token_restriction,
            "exclusion": self.exclusion,
            "subtype": self.subtype,
            "color_operator": self.color_operator,
            "colors": list(self.colors),
            "controller": self.controller,
            "owner": self.owner,
            "quantity": self.quantity.to_json() if self.quantity is not None else None,
            "source_zone": self.source_zone.to_json() if self.source_zone is not None else None,
            "destination_zone": (
                self.destination_zone.to_json() if self.destination_zone is not None else None
            ),
            "timing": self.timing.to_json(),
            "required_card_id": self.required_card_id,
            "evidence": self.evidence.to_json(),
            "operation_quote": self.operation_quote,
            "operation_occurrence": self.operation_occurrence,
            "object_quote": self.object_quote,
            "object_occurrence": self.object_occurrence,
            "capability_prerequisite_indices": list(self.capability_prerequisite_indices),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("relationship prerequisite must be an object.")
        _keys(
            value,
            {
                "kind",
                "subject",
                "operation",
                "object_kind",
                "card_types",
                "type_operator",
                "token_restriction",
                "exclusion",
                "subtype",
                "color_operator",
                "colors",
                "controller",
                "owner",
                "quantity",
                "source_zone",
                "destination_zone",
                "timing",
                "required_card_id",
                "evidence",
                "operation_quote",
                "operation_occurrence",
                "object_quote",
                "object_occurrence",
                "capability_prerequisite_indices",
            },
            "relationship prerequisite",
        )
        return cls(
            kind=_enum_from_json(value["kind"], "kind", PrerequisiteKind),
            subject=value["subject"],
            operation=value["operation"],
            object_kind=value["object_kind"],
            card_types=_json_array(value["card_types"], "card_types"),
            type_operator=value["type_operator"],
            token_restriction=value["token_restriction"],
            exclusion=value["exclusion"],
            subtype=value["subtype"],
            color_operator=value["color_operator"],
            colors=_json_array(value["colors"], "colors"),
            controller=value["controller"],
            owner=value["owner"],
            quantity=_optional_record(value["quantity"], CapabilityQuantity.from_json),
            source_zone=_optional_record(value["source_zone"], RelationshipZone.from_json),
            destination_zone=_optional_record(value["destination_zone"], RelationshipZone.from_json),
            timing=RelationshipTiming.from_json(value["timing"]),
            required_card_id=value["required_card_id"],
            evidence=OracleEvidence.from_json(value["evidence"]),
            operation_quote=value["operation_quote"],
            operation_occurrence=value["operation_occurrence"],
            object_quote=value["object_quote"],
            object_occurrence=value["object_occurrence"],
            capability_prerequisite_indices=_json_array(
                value["capability_prerequisite_indices"],
                "capability_prerequisite_indices",
            ),
        )


@dataclass(frozen=True, slots=True)
class RelationshipParticipant:
    """One directional participant of a typed prerequisite projection."""

    card_id: int
    capability_id: str
    card_name: str
    face_index: int | None
    face_name: str | None
    card_source_sha256: str
    role: Role
    capability_prerequisites: tuple[CapabilityPrerequisite, ...]
    prerequisites: tuple[RelationshipPrerequisite, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_id", _integer(self.card_id, "card_id", positive=True))
        object.__setattr__(self, "capability_id", _identifier(self.capability_id, "capability_id"))
        object.__setattr__(self, "card_name", _identifier(self.card_name, "card_name"))
        object.__setattr__(self, "face_index", _optional_integer(self.face_index, "face_index"))
        if self.face_name is not None:
            object.__setattr__(self, "face_name", _exact_text(self.face_name, "face_name"))
        object.__setattr__(
            self,
            "card_source_sha256",
            _hash(self.card_source_sha256, "card_source_sha256"),
        )
        _enum_member(self.role, "role", Role)
        capability_prerequisites = _tuple(self.capability_prerequisites, "capability_prerequisites")
        if any(type(item) is not CapabilityPrerequisite for item in capability_prerequisites):
            raise SemanticEnrichmentError(
                "capability_prerequisites must contain CapabilityPrerequisite records."
            )
        object.__setattr__(self, "capability_prerequisites", capability_prerequisites)
        prerequisites = _tuple(self.prerequisites, "prerequisites")
        if not prerequisites:
            raise SemanticEnrichmentError("prerequisites must not be empty.")
        if any(type(item) is not RelationshipPrerequisite for item in prerequisites):
            raise SemanticEnrichmentError("prerequisites must contain RelationshipPrerequisite records.")
        object.__setattr__(
            self,
            "prerequisites",
            _canonical(
                prerequisites,
                field_name="prerequisites",
                key=lambda item: _canonical_json_bytes(item.to_json()),
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "capability_id": self.capability_id,
            "card_name": self.card_name,
            "face_index": self.face_index,
            "face_name": self.face_name,
            "card_source_sha256": self.card_source_sha256,
            "role": self.role.value,
            "capability_prerequisites": [item.to_json() for item in self.capability_prerequisites],
            "prerequisites": [item.to_json() for item in self.prerequisites],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("relationship participant must be an object.")
        _keys(
            value,
            {
                "card_id",
                "capability_id",
                "card_name",
                "face_index",
                "face_name",
                "card_source_sha256",
                "role",
                "capability_prerequisites",
                "prerequisites",
            },
            "relationship participant",
        )
        return cls(
            card_id=value["card_id"],
            capability_id=value["capability_id"],
            card_name=value["card_name"],
            face_index=value["face_index"],
            face_name=value["face_name"],
            card_source_sha256=value["card_source_sha256"],
            role=_enum_from_json(value["role"], "role", Role),
            capability_prerequisites=_nested_json_array(
                value["capability_prerequisites"],
                "capability_prerequisites",
                CapabilityPrerequisite.from_json,
            ),
            prerequisites=_nested_json_array(
                value["prerequisites"],
                "prerequisites",
                RelationshipPrerequisite.from_json,
            ),
        )


@dataclass(frozen=True, slots=True)
class RelationshipPrerequisiteProjection:
    """Typed directional prerequisites for one accepted relationship candidate."""

    source: RelationshipParticipant
    target: RelationshipParticipant
    schema_version: int = RELATIONSHIP_PREREQUISITE_PROJECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        version = self.schema_version
        if isinstance(version, bool) or not isinstance(version, int):
            raise SemanticEnrichmentError("schema_version must be an integer.")
        if version != RELATIONSHIP_PREREQUISITE_PROJECTION_SCHEMA_VERSION:
            raise SemanticEnrichmentError("relationship prerequisite projection schema_version is unsupported.")
        object.__setattr__(self, "schema_version", version)
        if not isinstance(self.source, RelationshipParticipant):
            raise SemanticEnrichmentError("source must be a RelationshipParticipant.")
        if not isinstance(self.target, RelationshipParticipant):
            raise SemanticEnrichmentError("target must be a RelationshipParticipant.")
        validate_prerequisite_projection(projection=self)

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source": self.source.to_json(),
            "target": self.target.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("relationship prerequisite projection must be an object.")
        _keys(value, {"schema_version", "source", "target"}, "relationship prerequisite projection")
        return cls(
            source=RelationshipParticipant.from_json(value["source"]),
            target=RelationshipParticipant.from_json(value["target"]),
            schema_version=value["schema_version"],
        )


_CARD_RELATIONSHIP_KEYS = {
    "finding_id",
    "mechanism",
    "participants",
    "claim",
    "prerequisites",
    "oracle_evidence",
    "guide_evidence",
    "review",
    "run_id",
}


@dataclass(frozen=True, slots=True)
class CardRelationship:
    """A card or package relationship with Oracle support."""

    finding_id: str
    mechanism: str
    participants: tuple[int, ...]
    claim: str
    prerequisites: tuple[str, ...]
    oracle_evidence: tuple[OracleEvidence, ...]
    guide_evidence: tuple[GuideEvidence, ...]
    review: FindingReview
    run_id: str
    prerequisite_projection: RelationshipPrerequisiteProjection | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", _identifier(self.finding_id, "finding_id"))
        object.__setattr__(self, "mechanism", _identifier(self.mechanism, "mechanism").casefold())
        participants = _tuple(self.participants, "participants")
        if len(participants) < 2:
            raise SemanticEnrichmentError("participants must contain at least two cards.")
        if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in participants):
            raise SemanticEnrichmentError("participants must contain positive integers.")
        canonical_participants = _canonical(participants, field_name="participants", key=lambda item: item)
        object.__setattr__(self, "participants", canonical_participants)
        object.__setattr__(self, "claim", _exact_text(self.claim, "claim"))
        prerequisites = _tuple(self.prerequisites, "prerequisites")
        if not prerequisites:
            raise SemanticEnrichmentError("prerequisites must not be empty.")
        if any(not isinstance(item, str) or not item.strip() for item in prerequisites):
            raise SemanticEnrichmentError("prerequisites must contain nonblank strings.")
        for prerequisite in prerequisites:
            require_utf8_text(prerequisite, "prerequisites")
        object.__setattr__(
            self,
            "prerequisites",
            _canonical(prerequisites, field_name="prerequisites", key=lambda item: item),
        )
        oracle_evidence = _tuple(self.oracle_evidence, "oracle_evidence")
        if not oracle_evidence:
            raise SemanticEnrichmentError("oracle_evidence must not be empty.")
        if any(not isinstance(item, OracleEvidence) for item in oracle_evidence):
            raise SemanticEnrichmentError("oracle_evidence must contain OracleEvidence records.")
        object.__setattr__(
            self,
            "oracle_evidence",
            _canonical(
                oracle_evidence,
                field_name="oracle_evidence",
                key=lambda item: (
                    item.card_id,
                    -1 if item.face_index is None else item.face_index,
                    item.quote,
                ),
            ),
        )
        guide_evidence = _tuple(self.guide_evidence, "guide_evidence")
        if any(not isinstance(item, GuideEvidence) for item in guide_evidence):
            raise SemanticEnrichmentError("guide_evidence must contain GuideEvidence records.")
        object.__setattr__(
            self,
            "guide_evidence",
            _canonical(guide_evidence, field_name="guide_evidence", key=lambda item: (item.guide_id, item.quote)),
        )
        if not isinstance(self.review, FindingReview):
            raise SemanticEnrichmentError("review must be a FindingReview.")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        projection = self.prerequisite_projection
        if projection is None:
            return
        if not isinstance(projection, RelationshipPrerequisiteProjection):
            raise SemanticEnrichmentError(
                "prerequisite_projection must be a RelationshipPrerequisiteProjection or null."
            )
        if self.review.status is not FindingStatus.ACCEPTED:
            raise SemanticEnrichmentError("projected relationships require an accepted review.")
        if {projection.source.card_id, projection.target.card_id} != set(self.participants):
            raise SemanticEnrichmentError("projection participants must match the relationship participants.")
        source_evidence = set(self.oracle_evidence)
        for participant in (projection.source, projection.target):
            for clause in participant.prerequisites:
                if clause.evidence not in source_evidence:
                    raise SemanticEnrichmentError(
                        "projection clause evidence must be relationship Oracle evidence."
                    )

    @property
    def identity(self) -> tuple[str, tuple[int, ...], tuple[int | str, ...]]:
        """Return the duplicate identity of this relationship."""
        projection = self.prerequisite_projection
        if projection is None:
            direction: tuple[int | str, ...] = ()
        else:
            direction = (
                projection.source.card_id,
                projection.source.capability_id,
                -1 if projection.source.face_index is None else projection.source.face_index,
                projection.target.card_id,
                projection.target.capability_id,
                -1 if projection.target.face_index is None else projection.target.face_index,
            )
        return self.mechanism, self.participants, direction

    def to_json(self) -> dict[str, object]:
        encoded: dict[str, object] = {
            "finding_id": self.finding_id,
            "mechanism": self.mechanism,
            "participants": list(self.participants),
            "claim": self.claim,
            "prerequisites": list(self.prerequisites),
            "oracle_evidence": [item.to_json() for item in self.oracle_evidence],
            "guide_evidence": [item.to_json() for item in self.guide_evidence],
            "review": self.review.to_json(),
            "run_id": self.run_id,
        }
        if self.prerequisite_projection is not None:
            encoded["prerequisite_projection"] = self.prerequisite_projection.to_json()
        return encoded

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("card relationship must be an object.")
        projection: RelationshipPrerequisiteProjection | None = None
        if "prerequisite_projection" in value:
            _keys(value, _CARD_RELATIONSHIP_KEYS | {"prerequisite_projection"}, "card relationship")
            encoded_projection = value["prerequisite_projection"]
            if encoded_projection is None:
                raise SemanticEnrichmentError(
                    "card relationship prerequisite_projection must be an object when present."
                )
            projection = RelationshipPrerequisiteProjection.from_json(encoded_projection)
        else:
            _keys(value, _CARD_RELATIONSHIP_KEYS, "card relationship")
        return cls(
            finding_id=value["finding_id"],
            mechanism=value["mechanism"],
            participants=_json_array(value["participants"], "participants"),
            claim=value["claim"],
            prerequisites=_json_array(value["prerequisites"], "prerequisites"),
            oracle_evidence=_nested_json_array(
                value["oracle_evidence"], "oracle_evidence", OracleEvidence.from_json
            ),
            guide_evidence=_nested_json_array(
                value["guide_evidence"], "guide_evidence", GuideEvidence.from_json
            ),
            review=FindingReview.from_json(value["review"]),
            run_id=value["run_id"],
            prerequisite_projection=projection,
        )


def validate_prerequisite_projection(*, projection: RelationshipPrerequisiteProjection) -> None:
    """Require an anchored projection whose clauses bind to their own quotations."""
    for participant, other in ((projection.source, projection.target), (projection.target, projection.source)):
        validate_relationship_participant_sources(participant=participant, other=other)
    for participant in (projection.source, projection.target):
        _validate_participant_bindings(participant=participant)
        if not role_anchor_covered(participant=participant):
            raise PrerequisiteProjectionError(code="incomplete")


def validate_relationship_participant_sources(
    *,
    participant: RelationshipParticipant,
    other: RelationshipParticipant | None = None,
    oracle_text: str | None = None,
    evidence: tuple[OracleEvidence, ...] | None = None,
) -> None:
    """Run one participant's evidence ownership, selector and qualifier checks on its quotations."""
    paragraphs = oracle_text.split("\n") if oracle_text is not None else None
    for clause in participant.prerequisites:
        if clause.evidence.card_id != participant.card_id:
            raise PrerequisiteProjectionError(code="contradiction")
        if clause.evidence.face_index != participant.face_index:
            raise PrerequisiteProjectionError(code="contradiction")
        paragraph = clause.evidence.quote
        if paragraphs is not None and paragraph not in paragraphs:
            raise PrerequisiteProjectionError(code="contradiction")
        if evidence is not None:
            if not evidence:
                raise SemanticEnrichmentError("projection participant has no capability evidence.")
            if not any(item.quote in paragraph for item in evidence):
                raise PrerequisiteProjectionError(code="contradiction")
        operation_span, object_span = _clause_binding(clause)
        _validate_clause_source(
            clause=clause,
            paragraph=paragraph,
            operation_span=operation_span,
            object_span=object_span,
            participant=participant,
            other=other,
        )


def validate_prerequisite_sources(
    *,
    projection: RelationshipPrerequisiteProjection,
    oracle_text: Mapping[tuple[int, int | None], str],
    participant_evidence: Mapping[tuple[int, int | None], tuple[OracleEvidence, ...]] | None = None,
) -> None:
    """Require every clause to bind exactly to its participant's frozen Oracle text."""
    for participant, other in ((projection.source, projection.target), (projection.target, projection.source)):
        source = oracle_text.get((participant.card_id, participant.face_index))
        if source is None:
            raise SemanticEnrichmentError("projection participant has no frozen Oracle text.")
        evidence = (
            None
            if participant_evidence is None
            else participant_evidence.get((participant.card_id, participant.face_index), ())
        )
        validate_relationship_participant_sources(
            participant=participant,
            other=other,
            oracle_text=source,
            evidence=evidence,
        )


def validate_relationship_sources(
    *,
    relationship: CardRelationship,
    cards: Mapping[int, CardInfo],
    pins: Mapping[int, CardSourcePin],
) -> None:
    """Require a stored relationship projection to match its frozen card sources."""
    projection = relationship.prerequisite_projection
    if projection is None:
        return
    validate_relationship_pins(relationship=relationship, pins=pins)
    oracle_text: dict[tuple[int, int | None], str] = {}
    for participant in (projection.source, projection.target):
        card = cards.get(participant.card_id)
        if card is None:
            raise SemanticEnrichmentError("projection participant references an unknown source card.")
        if card.faces:
            if participant.face_index is None:
                raise SemanticEnrichmentError("projection participant requires a face index.")
            if participant.face_index >= len(card.faces):
                raise SemanticEnrichmentError("projection participant face_index is outside the card's faces.")
            face = card.faces[participant.face_index]
            selected_text = face.oracle_text
            selected_name = face.name
        else:
            if participant.face_index is not None:
                raise SemanticEnrichmentError("projection participant face_index must be null for one-faced cards.")
            selected_text = card.oracle_text
            selected_name = card.name
        if not isinstance(selected_text, str):
            raise SemanticEnrichmentError("projection participant requires frozen Oracle text.")
        if participant.card_name != card.name:
            raise SemanticEnrichmentError("projection participant card name does not match its source card.")
        if participant.face_name is not None and participant.face_name != selected_name:
            raise SemanticEnrichmentError("projection participant face name does not match its source face.")
        oracle_text[(participant.card_id, participant.face_index)] = selected_text
    validate_prerequisite_sources(projection=projection, oracle_text=oracle_text)
    relationship_evidence = set(relationship.oracle_evidence)
    for participant in (projection.source, projection.target):
        for clause in participant.prerequisites:
            if clause.evidence not in relationship_evidence:
                raise SemanticEnrichmentError(
                    "projection clause evidence must be part of the relationship Oracle evidence."
                )


def validate_relationship_pins(
    *,
    relationship: CardRelationship,
    pins: Mapping[int, CardSourcePin],
) -> None:
    """Require a stored relationship projection to match its provenance pins."""
    projection = relationship.prerequisite_projection
    if projection is None:
        return
    participants = set(relationship.participants)
    if not participants <= set(pins):
        raise SemanticEnrichmentError("relationship participants must be pinned card sources.")
    if {projection.source.card_id, projection.target.card_id} != participants:
        raise SemanticEnrichmentError("projection participants must match the relationship participants.")
    for participant in (projection.source, projection.target):
        pin = pins.get(participant.card_id)
        if pin is None or pin.sha256 != participant.card_source_sha256:
            raise SemanticEnrichmentError("projection participant hash must match its card source pin.")
        for clause in participant.prerequisites:
            required = clause.required_card_id
            if required is None:
                continue
            if required not in participants:
                raise SemanticEnrichmentError("projection required card must be a relationship participant.")
            if required not in pins:
                raise SemanticEnrichmentError("projection required card must be a pinned card source.")


def _validate_participant_bindings(*, participant: RelationshipParticipant) -> None:
    """Require one complete atomic clause per selected object and full index coverage."""
    if not participant.prerequisites:
        raise PrerequisiteProjectionError(code="incomplete")
    bindings: dict[tuple[object, ...], bytes] = {}
    paragraph_spans: dict[str, list[tuple[int, int]]] = {}
    referenced: dict[int, RelationshipPrerequisite] = {}
    originals = participant.capability_prerequisites
    for clause in participant.prerequisites:
        operation_span, object_span = _clause_binding(clause)
        binding = (
            clause.evidence.card_id,
            clause.evidence.face_index,
            clause.evidence.quote,
            operation_span,
            object_span,
        )
        encoded = _canonical_json_bytes(clause.to_json())
        previous = bindings.get(binding)
        if previous is not None and previous != encoded:
            raise PrerequisiteProjectionError(code="incomplete")
        bindings[binding] = encoded
        for span in paragraph_spans.setdefault(clause.evidence.quote, []):
            if _spans_overlap(span, object_span):
                raise PrerequisiteProjectionError(code="incomplete")
        paragraph_spans[clause.evidence.quote].append(object_span)
        for index in clause.capability_prerequisite_indices:
            if index >= len(originals):
                raise PrerequisiteProjectionError(code="contradiction")
            if index in referenced:
                raise PrerequisiteProjectionError(code="incomplete")
            referenced[index] = clause
    for index, clause in referenced.items():
        _validate_original_prerequisite(
            participant=participant,
            original=originals[index],
            clause=clause,
        )
    # Coverage completeness is reported last so an unreferenced index cannot hide a contradiction.
    if set(referenced) != set(range(len(originals))):
        raise PrerequisiteProjectionError(code="incomplete")


def _validate_original_prerequisite(
    *,
    participant: RelationshipParticipant,
    original: CapabilityPrerequisite,
    clause: RelationshipPrerequisite,
) -> None:
    """Bind one retained capability prerequisite to the clause that references it."""
    if original.kind is not clause.kind:
        raise PrerequisiteProjectionError(code="contradiction")
    if original.evidence.card_id != participant.card_id or original.evidence.face_index != participant.face_index:
        raise PrerequisiteProjectionError(code="contradiction")
    covered = tuple(
        candidate
        for candidate in participant.prerequisites
        if candidate.kind is original.kind and _original_covers(original=original, clause=candidate)
    )
    if len(covered) != 1:
        raise PrerequisiteProjectionError(code="incomplete")
    if covered[0] is not clause:
        raise PrerequisiteProjectionError(code="contradiction")
    _compare_original_values(original=original, clause=clause)


def _original_covers(*, original: CapabilityPrerequisite, clause: RelationshipPrerequisite) -> bool:
    """Return whether one coarse original quote covers a clause's selected spans."""
    if original.evidence.card_id != clause.evidence.card_id:
        return False
    if original.evidence.face_index != clause.evidence.face_index:
        return False
    paragraph = clause.evidence.quote
    quote = original.evidence.quote
    operation_span, object_span = _clause_binding(clause)
    position = paragraph.find(quote)
    while position >= 0:
        end = position + len(quote)
        if (
            position <= operation_span[0]
            and operation_span[1] <= end
            and position <= object_span[0]
            and object_span[1] <= end
        ):
            return True
        position = paragraph.find(quote, position + 1)
    return False


def _compare_original_values(*, original: CapabilityPrerequisite, clause: RelationshipPrerequisite) -> None:
    """Compare one uniquely bound original prerequisite with its atomic clause."""
    quantity = original.quantity
    if quantity is not None:
        declared = clause.quantity
        if declared is None:
            raise PrerequisiteProjectionError(code="incomplete")
        if declared != quantity:
            if declared.relation is QuantityRelation.VARIABLE or quantity.relation is QuantityRelation.VARIABLE:
                raise PrerequisiteProjectionError(code="incomplete")
            raise PrerequisiteProjectionError(code="contradiction")
    for original_zone, declared_zone in (
        (original.source_zone, clause.source_zone),
        (original.destination_zone, clause.destination_zone),
    ):
        if original_zone is None:
            continue
        if declared_zone is None:
            raise PrerequisiteProjectionError(code="incomplete")
        if declared_zone.zone is not original_zone:
            raise PrerequisiteProjectionError(code="contradiction")
    window = _recognized_timing_window(original.timing)
    if window is not None and window != clause.timing.window:
        raise PrerequisiteProjectionError(code="contradiction")


def _clause_binding(clause: RelationshipPrerequisite) -> tuple[tuple[int, int], tuple[int, int]]:
    """Resolve one clause's operation and object spans inside its own evidence."""
    paragraph = clause.evidence.quote
    operation_span = _resolved_span(
        quote=paragraph,
        selector=clause.operation_quote,
        occurrence=clause.operation_occurrence,
    )
    object_span = _resolved_span(
        quote=paragraph,
        selector=clause.object_quote,
        occurrence=clause.object_occurrence,
    )
    if operation_span is None or object_span is None:
        raise PrerequisiteProjectionError(code="contradiction")
    return operation_span, object_span


def _resolved_span(*, quote: str, selector: str, occurrence: int) -> tuple[int, int] | None:
    """Return the exact span of one non-overlapping selector occurrence."""
    position = 0
    for _ in range(occurrence + 1):
        position = quote.find(selector, position)
        if position < 0:
            return None
        position += len(selector)
    return position - len(selector), position


def _spans_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Return whether two half-open spans share any character."""
    return first[0] < second[1] and second[0] < first[1]


_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_COUNT_TOKEN_PATTERN = re.compile(r"\d+|[A-Za-z][A-Za-z'’-]*")
_CONNECTIVE_ARTICLES: frozenset[str] = frozenset({"a", "an", "the"})
_P_T_PATTERN = re.compile(r"[+-]?\d+\s*/\s*[+-]?\d+")
_BRACE_PATTERN = re.compile(r"\{[^}]*\}")
_OTHER_PATTERN = re.compile(r"\bfor each\b", re.IGNORECASE)
_STATE_PATTERN = re.compile(r"\b(?:tapped|untapped|attacking|blocking|unblocked)\b", re.IGNORECASE)
_STATE_COMPARISON_PATTERN = re.compile(
    r"\b(?:power|toughness)\b[^.]{0,40}\b(?:greater|less|equal|more|fewer)\b",
    re.IGNORECASE,
)
_MANA_VALUE_PATTERN = re.compile(r"\bmana (?:value|cost)\b|\bconverted mana cost\b", re.IGNORECASE)
_UNCOVERED_RESTRICTION_PATTERN = re.compile(r"\b(?:only|until|before|after)\b", re.IGNORECASE)
_PAY_OR_TAP_PATTERN = re.compile(r"\b(?:pay|tap)\b", re.IGNORECASE)
_OR_PATTERN = re.compile(r"\bor\b", re.IGNORECASE)
_CONJUNCTION_PATTERN = re.compile(
    r"\b(?:a|an|the|one|each|another|other|target|this|that)\b[^,;:.]*?"
    r"\b(?:and|or)\b\s+(?:a|an|the|one|each|another|other|target|this|that)\b",
    re.IGNORECASE,
)
_HISTORY_PATTERN = re.compile(
    r"\bthis turn\b|\bso far\b|\b(?:has|have) died\b|\b(?:was|were) cast\b|\bif you(?:'ve| have)\b",
    re.IGNORECASE,
)
_TOKEN_WORD_PATTERN = re.compile(r"\btokens?\b", re.IGNORECASE)
_NONTOKEN_WORD_PATTERN = re.compile(r"\bnon-?tokens?\b", re.IGNORECASE)
_SUBTYPE_PATTERN = re.compile(
    r"\b([A-Z][a-z]{2,})\s+"
    r"(?:artifacts?|battles?|cards?|creatures?|enchantments?|instants?|kindred|lands?|"
    r"permanents?|planeswalkers?|sorceries|spells?|tokens?)\b"
)

_COLOR_WORDS: Mapping[str, str] = {
    "white": "W",
    "blue": "U",
    "black": "B",
    "red": "R",
    "green": "G",
}
_OBJECT_KIND_WORDS: Mapping[str, str] = {
    "card": "card",
    "cards": "card",
    "permanent": "permanent",
    "permanents": "permanent",
    "spell": "spell",
    "spells": "spell",
    "token": "token",
    "tokens": "token",
}
_CARD_TYPE_WORDS: Mapping[str, str] = {
    "artifact": "artifact",
    "artifacts": "artifact",
    "battle": "battle",
    "battles": "battle",
    "creature": "creature",
    "creatures": "creature",
    "enchantment": "enchantment",
    "enchantments": "enchantment",
    "instant": "instant",
    "instants": "instant",
    "kindred": "kindred",
    "land": "land",
    "lands": "land",
    "planeswalker": "planeswalker",
    "planeswalkers": "planeswalker",
    "sorcery": "sorcery",
    "sorceries": "sorcery",
}
_COUNT_WORDS: Mapping[str, int] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_OPERATION_LEXEMES: Mapping[str, str] = {
    "attack": "attack",
    "attacked": "attack",
    "attacking": "attack",
    "attacks": "attack",
    "cast": "cast",
    "casting": "cast",
    "casts": "cast",
    "control": "control",
    "controlled": "control",
    "controlling": "control",
    "controls": "control",
    "count": "count",
    "counted": "count",
    "counting": "count",
    "counts": "count",
    "create": "create",
    "created": "create",
    "creates": "create",
    "creating": "create",
    "die": "die",
    "died": "die",
    "dies": "die",
    "dying": "die",
    "discard": "discard",
    "discarded": "discard",
    "discarding": "discard",
    "discards": "discard",
    "draw": "draw",
    "drawing": "draw",
    "drawn": "draw",
    "draws": "draw",
    "drew": "draw",
    "enter": "enter",
    "entered": "enter",
    "entering": "enter",
    "enters": "enter",
    "leave": "leave",
    "leaves": "leave",
    "leaving": "leave",
    "left": "leave",
    "mill": "mill",
    "milled": "mill",
    "milling": "mill",
    "mills": "mill",
    "number": "count",
    "put": "mill",
    "return": "return",
    "returned": "return",
    "returning": "return",
    "returns": "return",
    "sacrifice": "sacrifice",
    "sacrificed": "sacrifice",
    "sacrifices": "sacrifice",
    "sacrificing": "sacrifice",
}
_IRREGULAR_SUBTYPES: Mapping[str, tuple[str, ...]] = {
    "elf": ("elves",),
    "dwarf": ("dwarves",),
    "wolf": ("wolves",),
    "werewolf": ("werewolves",),
    "mouse": ("mice",),
    "ox": ("oxen",),
}
_ZONE_WORDS: Mapping[str, CapabilityZone] = {
    "battlefield": CapabilityZone.BATTLEFIELD,
    "battlefields": CapabilityZone.BATTLEFIELD,
    "exile": CapabilityZone.EXILE,
    "graveyard": CapabilityZone.GRAVEYARD,
    "graveyards": CapabilityZone.GRAVEYARD,
    "hand": CapabilityZone.HAND,
    "hands": CapabilityZone.HAND,
    "library": CapabilityZone.LIBRARY,
    "libraries": CapabilityZone.LIBRARY,
    "stack": CapabilityZone.STACK,
}
_ZONE_PLAYER_PATTERN = re.compile(r"\b(your|opponent's|opponents'|owner's|their|a|any|the)\s*$", re.IGNORECASE)
_ZONE_PREPOSITION_PATTERN = re.compile(r"\b(from|to|into|onto|in|on)\s*$", re.IGNORECASE)
_ZONE_PLAYERS: Mapping[str, str] = {
    "your": "you",
    "opponent's": "opponent",
    "opponents'": "opponent",
    "owner's": "owner",
}
_OperationTransition = tuple[tuple[CapabilityZone, str | None] | None, tuple[CapabilityZone, str] | None]
_OPERATION_TRANSITIONS: Mapping[str, _OperationTransition] = {
    "discard": ((CapabilityZone.HAND, "you"), (CapabilityZone.GRAVEYARD, "you")),
    "mill": ((CapabilityZone.LIBRARY, "you"), (CapabilityZone.GRAVEYARD, "you")),
    "die": (None, (CapabilityZone.GRAVEYARD, "owner")),
    "sacrifice": (None, (CapabilityZone.GRAVEYARD, "owner")),
}
_CHECKED_TIMING_PHRASES: tuple[tuple[str, str], ...] = (
    (r"\b(?:at the )?beginning of (?:your|each) upkeep\b|\b(?:your|each) upkeep\b", "upkeep"),
    (r"\bbeginning of (?:your )?combat\b", "beginning_of_combat"),
    (r"\bonly as a sorcery\b|\bas a sorcery\b|\bsorcery timing\b|\bsorcery speed\b", "sorcery_speed"),
    (r"\bonly during your turn\b", "your"),
    (r"\bonly during (?:an |each )?opponents?' turn\b", "opponent"),
    (r"\bonly once each turn\b|\bonce each turn\b", "once"),
)
_RECOGNIZED_TIMING_PATTERNS: tuple[str, ...] = tuple(
    pattern for pattern, _ in _CHECKED_TIMING_PHRASES
)
_ORIGINAL_TIMING_WINDOWS: Mapping[str, str] = {
    "beginning of combat": "beginning_of_combat",
    "beginning of each upkeep": "upkeep",
    "beginning of your upkeep": "upkeep",
    "combat": "combat",
    "dies": "dies",
    "end step": "end_step",
    "enters": "enters",
    "landfall": "landfall",
    "main phase": "main_phase",
    "next turn": "next_turn",
    "sorcery speed": "sorcery_speed",
    "sorcery timing": "sorcery_speed",
    "this turn": "same_turn",
    "upkeep": "upkeep",
    "your turn": "main_phase",
}
_CLIPPING_WORDS: frozenset[str] = (
    frozenset(_COLOR_WORDS)
    | {"colorless", "nontoken", "nontokens", "token", "tokens", "other", "another"}
    | frozenset(_OBJECT_KIND_WORDS)
    | frozenset(_CARD_TYPE_WORDS)
    | frozenset(_COUNT_WORDS)
)
_CLIPPING_PHRASES: tuple[str, ...] = (
    r"you control",
    r"you own",
    r"(?:an?\s+|each\s+|all\s+)?opponents?\s+(?:controls?|control)",
    r"(?:an?\s+|each\s+|all\s+)?opponents?\s+(?:owns?|own)",
    r"this card",
    r"this creature",
    r"at least",
    r"up to",
    r"or more",
)


def _validate_clause_source(
    *,
    clause: RelationshipPrerequisite,
    paragraph: str,
    operation_span: tuple[int, int],
    object_span: tuple[int, int],
    participant: RelationshipParticipant,
    other: RelationshipParticipant,
) -> None:
    """Run the bounded qualifier checks for one clause against its source paragraph."""
    window, window_start = _clause_window(
        paragraph=paragraph,
        operation_span=operation_span,
        object_span=object_span,
    )
    phrase = clause.object_quote
    _check_operation_lexemes(clause=clause)
    _check_conjunction(phrase=phrase)
    _check_colors(clause=clause, phrase=phrase)
    _check_types_and_tokens(clause=clause, phrase=phrase)
    _check_subtype(clause=clause, window=window, participant=participant, other=other)
    _check_identity(
        clause=clause,
        paragraph=paragraph,
        object_span=object_span,
        participant=participant,
        other=other,
    )
    _check_exclusion(clause=clause, window=window)
    _check_controller_and_owner(clause=clause, phrase=phrase)
    _check_zones(clause=clause, window=window)
    _check_quantity(clause=clause, phrase=phrase)
    _check_timing(clause=clause, paragraph=paragraph)
    _check_clipping(paragraph=paragraph, object_span=object_span)
    _check_unsupported(
        clause=clause,
        paragraph=paragraph,
        window=window,
        object_span=object_span,
        operation_span=operation_span,
        window_start=window_start,
    )


def _clause_window(
    *,
    paragraph: str,
    operation_span: tuple[int, int],
    object_span: tuple[int, int],
) -> tuple[str, int]:
    """Return the clause-local source window that starts at its operation."""
    start = min(operation_span[0], object_span[0])
    end = paragraph.find(".", object_span[1])
    if end < 0:
        end = len(paragraph)
    return paragraph[start:end], start


def _stated_expression(phrase: str, vocabulary: Mapping[str, str]) -> tuple[tuple[str, ...], str | None]:
    """Collect one phrase's contiguous vocabulary expression and its connective."""
    hits = [
        (match.start(), match.end(), vocabulary[match.group(0).casefold()])
        for match in _WORD_PATTERN.finditer(phrase)
        if match.group(0).casefold() in vocabulary
    ]
    if not hits:
        return (), None
    groups: list[list[str]] = [[]]
    connectives: set[str] = set()
    previous_end: int | None = None
    for start, end, value in hits:
        if previous_end is not None:
            between = phrase[previous_end:start].strip(" ,").casefold()
            connective = _connective_word(between)
            if connective is not None:
                connectives.add(connective)
            elif between:
                groups.append([])
        groups[-1].append(value)
        previous_end = end
    values = tuple(dict.fromkeys(value for group in groups for value in group))
    if len(connectives) > 1:
        return values, "mixed"
    return values, next(iter(connectives)) if connectives else None


def _connective_word(between: str) -> str | None:
    """Return the `and`/`or` connective of one inter-object phrase, ignoring articles."""
    words = [match.group(0).casefold() for match in _WORD_PATTERN.finditer(between)]
    if not words or words[0] not in {"and", "or"}:
        return None
    if all(word in _CONNECTIVE_ARTICLES for word in words[1:]):
        return words[0]
    return None


def _check_operation_lexemes(*, clause: RelationshipPrerequisite) -> None:
    """Require a selected operation phrase to denote the declared operation."""
    words = {match.group(0).casefold() for match in _WORD_PATTERN.finditer(clause.operation_quote)}
    for word, operation in _OPERATION_LEXEMES.items():
        if word not in words or operation == clause.operation:
            continue
        if {operation, clause.operation} == {"control", "count"}:
            continue
        if operation == "mill" and word == "put":
            continue
        raise PrerequisiteProjectionError(code="contradiction")


def _check_conjunction(*, phrase: str) -> None:
    """Require one selected object phrase to name a single determined object."""
    if _CONJUNCTION_PATTERN.search(phrase) is not None:
        raise PrerequisiteProjectionError(code="incomplete")


def _check_colors(*, clause: RelationshipPrerequisite, phrase: str) -> None:
    """Require directly stated colors to agree with the typed color fields."""
    if re.search(r"\bcolorless\b", phrase, re.IGNORECASE):
        if clause.colors:
            raise PrerequisiteProjectionError(code="contradiction")
        if clause.color_operator != "exact":
            raise PrerequisiteProjectionError(code="incomplete")
        return
    stated, connective = _stated_expression(phrase, _COLOR_WORDS)
    if connective == "mixed":
        raise PrerequisiteProjectionError(code="incomplete")
    if not stated:
        return
    ordered = tuple(sorted(stated, key=_COLORS.index))
    if set(ordered) != set(clause.colors):
        raise PrerequisiteProjectionError(code="contradiction")
    if clause.subject == "output" and clause.object_kind == "token":
        if clause.color_operator != "exact":
            raise PrerequisiteProjectionError(code="incomplete")
        return
    if connective == "or":
        if clause.color_operator != "any_of":
            raise PrerequisiteProjectionError(code="incomplete")
        return
    if len(ordered) > 1 and clause.color_operator != "all_of":
        raise PrerequisiteProjectionError(code="incomplete")


def _check_types_and_tokens(*, clause: RelationshipPrerequisite, phrase: str) -> None:
    """Require stated object kinds, token restrictions and types to agree with the record."""
    kinds = {
        _OBJECT_KIND_WORDS[match.group(0).casefold()]
        for match in _WORD_PATTERN.finditer(phrase)
        if match.group(0).casefold() in _OBJECT_KIND_WORDS
    }
    if len(kinds) > 1:
        raise PrerequisiteProjectionError(code="incomplete")
    if kinds and kinds != {clause.object_kind}:
        raise PrerequisiteProjectionError(code="contradiction")
    nontoken = _NONTOKEN_WORD_PATTERN.search(phrase) is not None
    token = _TOKEN_WORD_PATTERN.search(phrase) is not None
    if nontoken and token:
        raise PrerequisiteProjectionError(code="incomplete")
    if nontoken:
        if clause.token_restriction != "nontoken":
            raise PrerequisiteProjectionError(code="contradiction")
    elif token:
        if clause.token_restriction != "token":
            raise PrerequisiteProjectionError(code="contradiction")
    elif clause.token_restriction != "unrestricted":
        raise PrerequisiteProjectionError(code="contradiction")
    stated, connective = _stated_expression(phrase, _CARD_TYPE_WORDS)
    if connective == "mixed":
        raise PrerequisiteProjectionError(code="incomplete")
    if not stated:
        return
    ordered = tuple(sorted(set(stated)))
    if set(ordered) != set(clause.card_types):
        raise PrerequisiteProjectionError(code="contradiction")
    if connective == "or":
        if clause.type_operator != "any_of":
            raise PrerequisiteProjectionError(code="incomplete")
    elif len(ordered) > 1 and clause.type_operator != "all_of":
        raise PrerequisiteProjectionError(code="incomplete")


def _subtype_forms(value: str) -> tuple[str, ...]:
    """Return the bounded evidence forms accepted for one stored subtype."""
    forms = [value, f"{value}s", f"{value}es"]
    if value.endswith("y"):
        forms.append(f"{value[:-1]}ies")
    forms.extend(_IRREGULAR_SUBTYPES.get(value, ()))
    return tuple(forms)


def _subtype_atoms(
    *,
    window: str,
    participant: RelationshipParticipant,
    other: RelationshipParticipant | None,
) -> tuple[str, ...]:
    """Return the capitalised subtype atoms stated next to a type or kind word."""
    names = tuple(
        name.casefold()
        for item in (participant, other)
        if item is not None
        for name in (item.card_name, item.face_name)
        if name is not None
    )
    atoms: list[str] = []
    for match in _SUBTYPE_PATTERN.finditer(window):
        folded = match.group(1).casefold()
        if folded in _CARD_TYPE_WORDS or folded in _OBJECT_KIND_WORDS:
            continue
        if any(folded in name for name in names):
            continue
        atoms.append(folded)
    return tuple(dict.fromkeys(atoms))


def _check_subtype(
    *,
    clause: RelationshipPrerequisite,
    window: str,
    participant: RelationshipParticipant,
    other: RelationshipParticipant,
) -> None:
    """Require the stored subtype to match its stated evidence atom."""
    atoms = _subtype_atoms(window=window, participant=participant, other=other)
    declared = clause.subtype
    if declared is None:
        if atoms:
            raise PrerequisiteProjectionError(code="incomplete")
        return
    for form in _subtype_forms(declared):
        if re.search(rf"\b{re.escape(form)}\b", window, re.IGNORECASE):
            return
    if atoms:
        raise PrerequisiteProjectionError(code="contradiction")
    raise PrerequisiteProjectionError(code="incomplete")


def _check_identity(
    *,
    clause: RelationshipPrerequisite,
    paragraph: str,
    object_span: tuple[int, int],
    participant: RelationshipParticipant,
    other: RelationshipParticipant | None,
) -> None:
    """Require identity claims to be named inside the clause's selected object phrase."""
    phrase = _identity_phrase(paragraph=paragraph, object_span=object_span)
    participant_names = _participant_names(participant)
    other_names = () if other is None else _participant_names(other)
    required = clause.required_card_id
    if required is None:
        return
    if required != participant.card_id and (other is None or required != other.card_id):
        raise PrerequisiteProjectionError(code="incomplete")
    participant_named = any(name in phrase for name in participant_names)
    other_named = any(name in phrase for name in other_names)
    if participant_named and other_named:
        raise PrerequisiteProjectionError(code="incomplete")
    if required == participant.card_id:
        if participant_named or _self_phrase(phrase):
            return
        raise PrerequisiteProjectionError(code="contradiction")
    if not other_named:
        raise PrerequisiteProjectionError(code="contradiction")


def _identity_phrase(*, paragraph: str, object_span: tuple[int, int]) -> str:
    """Return the selected object phrase plus any adjoining self-identity qualifier."""
    prefix = paragraph[: object_span[0]].rstrip()
    start = object_span[0]
    for qualifier in ("this card", "this creature", "this"):
        if prefix.casefold().endswith(qualifier):
            start = len(prefix) - len(qualifier)
            break
    return paragraph[start:object_span[1]]


def _self_phrase(phrase: str) -> bool:
    """Return whether a phrase states a self reference to the ability's own object."""
    folded = phrase.casefold()
    return "this card" in folded or "this creature" in folded


def _participant_names(participant: RelationshipParticipant) -> tuple[str, ...]:
    """Return the canonical and selected-face names of one participant."""
    names = [participant.card_name]
    if participant.face_name is not None:
        names.append(participant.face_name)
    return tuple(names)


def _check_exclusion(*, clause: RelationshipPrerequisite, window: str) -> None:
    """Require other/another exclusions to exclude the ability-bearing object."""
    ordinary = re.search(r"\b(?:other|another)\b", window, re.IGNORECASE) is not None
    if ordinary and clause.exclusion != "ability_source":
        raise PrerequisiteProjectionError(code="incomplete")
    if not ordinary and clause.exclusion != "none":
        raise PrerequisiteProjectionError(code="contradiction")


def _check_controller_and_owner(*, clause: RelationshipPrerequisite, phrase: str) -> None:
    """Require stated control and ownership predicates to agree with the record."""
    folded = phrase.casefold()
    if re.search(r"\byou control\b", folded) and clause.controller != "you":
        raise PrerequisiteProjectionError(code="contradiction")
    if re.search(r"\b(?:an?\s+)?opponents?\s+controls?\b", folded) and clause.controller != "opponent":
        raise PrerequisiteProjectionError(code="contradiction")
    if re.search(r"\byou own\b", folded) and clause.owner != "you":
        raise PrerequisiteProjectionError(code="contradiction")
    if re.search(r"\b(?:an?\s+)?opponents?\s+owns?\b", folded) and clause.owner != "opponent":
        raise PrerequisiteProjectionError(code="contradiction")


def _stated_zones(window: str) -> tuple[tuple[CapabilityZone, str | None, str | None], ...]:
    """Return the zone mentions stated with their player and direction."""
    folded = window.casefold()
    stated: list[tuple[CapabilityZone, str | None, str | None]] = []
    for match in _WORD_PATTERN.finditer(folded):
        zone = _ZONE_WORDS.get(match.group(0))
        if zone is None:
            continue
        prefix = folded[max(0, match.start() - 24):match.start()]
        player: str | None = None
        player_match = _ZONE_PLAYER_PATTERN.search(prefix)
        if player_match is not None:
            player = _ZONE_PLAYERS.get(player_match.group(1))
            prefix = prefix[: player_match.start()]
        direction: str | None = None
        preposition_match = _ZONE_PREPOSITION_PATTERN.search(prefix)
        if preposition_match is not None:
            preposition = preposition_match.group(1)
            if preposition == "from":
                direction = "source"
            elif preposition in {"to", "into", "onto"}:
                direction = "destination"
        stated.append((zone, player, direction))
    return tuple(stated)


def _check_zones(*, clause: RelationshipPrerequisite, window: str) -> None:
    """Require stated and recognized transitions to agree with the typed zones."""
    for zone, player, direction in _stated_zones(window):
        declared = clause.source_zone if direction == "source" else clause.destination_zone
        if direction is None:
            continue
        if declared is None:
            raise PrerequisiteProjectionError(code="incomplete")
        if declared.zone is not zone:
            raise PrerequisiteProjectionError(code="contradiction")
        if player is not None and declared.player != player:
            raise PrerequisiteProjectionError(code="contradiction")
    expected_source, expected_destination = _OPERATION_TRANSITIONS.get(clause.operation, (None, None))
    for expected, declared in (
        (expected_source, clause.source_zone),
        (expected_destination, clause.destination_zone),
    ):
        if expected is None:
            continue
        zone, player = expected
        if declared is None:
            raise PrerequisiteProjectionError(code="incomplete")
        if declared.zone is not zone:
            raise PrerequisiteProjectionError(code="contradiction")
        if player is not None and declared.player != player:
            raise PrerequisiteProjectionError(code="contradiction")
    if clause.operation == "return":
        if clause.source_zone is None or clause.destination_zone is None:
            raise PrerequisiteProjectionError(code="incomplete")
        if clause.source_zone.zone is not CapabilityZone.GRAVEYARD:
            raise PrerequisiteProjectionError(code="contradiction")
    if clause.operation in {"die", "sacrifice"}:
        if clause.source_zone is not None and clause.source_zone.zone is not CapabilityZone.BATTLEFIELD:
            raise PrerequisiteProjectionError(code="contradiction")


def _check_quantity(*, clause: RelationshipPrerequisite, phrase: str) -> None:
    """Require the one recognized object count to agree with the typed quantity."""
    cleaned = _P_T_PATTERN.sub(" ", _BRACE_PATTERN.sub(" ", phrase))
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
        following = [
            tokens[position].group(0).casefold()
            for position in range(index + 1, min(index + 3, len(tokens)))
        ]
        if index >= 2 and previous == "least" and tokens[index - 2].group(0).casefold() == "at":
            relation = QuantityRelation.AT_LEAST
        elif following[:2] == ["or", "more"]:
            relation = QuantityRelation.AT_LEAST
        elif index >= 2 and previous == "to" and tokens[index - 2].group(0).casefold() == "up":
            relation = QuantityRelation.AT_MOST
        found.add((value, relation))
    if len(found) > 1:
        raise PrerequisiteProjectionError(code="incomplete")
    if not found:
        return
    value, relation = next(iter(found))
    declared = clause.quantity
    if declared is None or declared.relation is QuantityRelation.VARIABLE:
        raise PrerequisiteProjectionError(code="incomplete")
    if declared.value != value or declared.relation is not relation:
        raise PrerequisiteProjectionError(code="contradiction")


def _check_timing(*, clause: RelationshipPrerequisite, paragraph: str) -> None:
    """Require recognized timing phrases to agree with the typed timing."""
    folded = paragraph.casefold()
    for pattern, expected in _CHECKED_TIMING_PHRASES:
        if re.search(pattern, folded) is None:
            continue
        if expected == "once":
            if clause.timing.max_per_turn != 1:
                raise PrerequisiteProjectionError(code="contradiction")
        elif expected in _TIMING_TURNS:
            if clause.timing.turn != expected:
                raise PrerequisiteProjectionError(code="contradiction")
        elif clause.timing.window != expected:
            raise PrerequisiteProjectionError(code="contradiction")
    remaining = folded
    for pattern in _RECOGNIZED_TIMING_PATTERNS:
        remaining = re.sub(pattern, " ", remaining)
    if _UNCOVERED_RESTRICTION_PATTERN.search(remaining) is not None:
        raise PrerequisiteProjectionError(code="incomplete")


def _recognized_timing_window(text: str | None) -> str | None:
    """Return the closed window of an exactly recognized timing phrase."""
    if text is None:
        return None
    normalized = " ".join(text.casefold().split())
    return _ORIGINAL_TIMING_WINDOWS.get(normalized)


def _check_clipping(*, paragraph: str, object_span: tuple[int, int]) -> None:
    """Require adjoining recognized qualifiers to be part of the selected object."""
    prefix = paragraph[: object_span[0]].rstrip()
    suffix = paragraph[object_span[1]:].lstrip()
    if prefix and _qualifier_ends(prefix):
        raise PrerequisiteProjectionError(code="incomplete")
    if suffix and _qualifier_starts(suffix):
        raise PrerequisiteProjectionError(code="incomplete")


def _qualifier_ends(text: str) -> bool:
    """Return whether text ends with one closed qualifier."""
    folded = text.casefold()
    if any(re.search(rf"(?:{pattern})$", folded) is not None for pattern in _CLIPPING_PHRASES):
        return True
    match = re.search(r"([^\s,;:.]+)$", folded)
    if match is None:
        return False
    token = match.group(1).strip(".,;:")
    return token in _CLIPPING_WORDS or token.isdigit()


def _qualifier_starts(text: str) -> bool:
    """Return whether text starts with one closed qualifier."""
    folded = text.casefold()
    if any(re.match(rf"(?:{pattern})\b", folded) is not None for pattern in _CLIPPING_PHRASES):
        return True
    match = re.match(r"([^\s,;:.]+)", folded)
    if match is None:
        return False
    token = match.group(1).strip(".,;:")
    return token in _CLIPPING_WORDS or token.isdigit()


def _check_unsupported(
    *,
    clause: RelationshipPrerequisite,
    paragraph: str,
    window: str,
    object_span: tuple[int, int],
    operation_span: tuple[int, int],
    window_start: int,
) -> None:
    """Fail completeness for stated restrictions the closed vocabulary cannot express."""
    if _BRACE_PATTERN.search(paragraph) is not None:
        raise PrerequisiteProjectionError(code="incomplete")
    sentence_start = paragraph.rfind(".", 0, operation_span[0]) + 1
    activation_prefix = paragraph[sentence_start:operation_span[0]]
    if _PAY_OR_TAP_PATTERN.search(activation_prefix) is not None:
        raise PrerequisiteProjectionError(code="incomplete")
    if _HISTORY_PATTERN.search(activation_prefix + window) is not None:
        raise PrerequisiteProjectionError(code="incomplete")
    if _STATE_COMPARISON_PATTERN.search(window) is not None:
        raise PrerequisiteProjectionError(code="incomplete")
    if _MANA_VALUE_PATTERN.search(window) is not None:
        raise PrerequisiteProjectionError(code="incomplete")
    if _STATE_PATTERN.search(window) is not None:
        raise PrerequisiteProjectionError(code="incomplete")
    if _OTHER_PATTERN.search(window) is not None:
        raise PrerequisiteProjectionError(code="incomplete")
    object_start = object_span[0] - window_start
    object_end = object_span[1] - window_start
    for match in _OR_PATTERN.finditer(window):
        if object_start <= match.start() < object_end:
            continue
        raise PrerequisiteProjectionError(code="incomplete")


def role_anchor_covered(*, participant: RelationshipParticipant) -> bool:
    """Return whether one participant's clauses cover its declared role anchor."""
    clauses = participant.prerequisites
    role = participant.role
    if role is Role.TOKEN_MAKER:
        return any(_produced_token_clause(clause) for clause in clauses)
    if role is Role.GO_WIDE_PAYOFF:
        return any(_wide_payoff_clause(clause) for clause in clauses)
    if role is Role.SACRIFICE_OUTLET:
        return any(_outlet_clause(clause=clause, participant=participant) for clause in clauses)
    if role is Role.DEATH_PAYOFF:
        return any(_death_payoff_clause(clause=clause, participant=participant) for clause in clauses)
    if role is Role.SELF_MILL:
        return any(_self_mill_clause(clause) for clause in clauses)
    if role is Role.DISCARD_ENABLER:
        return any(_discard_clause(clause) for clause in clauses)
    if role is Role.LOOT:
        return any(_draw_clause(clause) for clause in clauses) and any(
            _discard_clause(clause) for clause in clauses
        )
    if role is Role.RECURSION:
        return any(_return_clause(clause) for clause in clauses)
    if role is Role.RECURSION_PAYOFF:
        return any(_recursion_payoff_clause(clause) for clause in clauses)
    if role is Role.GRAVEYARD_PAYOFF:
        return any(_graveyard_payoff_clause(clause) for clause in clauses)
    if role is Role.SACRIFICE_FODDER:
        return any(_fodder_clause(clause=clause, participant=participant) for clause in clauses)
    return False


def _produced_token_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause is a creature token the capability produces."""
    return (
        clause.kind is PrerequisiteKind.CONDITION
        and clause.subject == "output"
        and clause.operation == "create"
        and clause.object_kind == "token"
        and "creature" in clause.card_types
    )


def _wide_payoff_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause pays off controlling, counting or attacking with creatures."""
    if clause.controller != "you" or "creature" not in clause.card_types:
        return False
    if clause.kind in (PrerequisiteKind.CONDITION, PrerequisiteKind.THRESHOLD):
        return clause.operation in ("control", "count")
    return clause.kind is PrerequisiteKind.TRIGGER and clause.operation == "attack"


def _outlet_clause(*, clause: RelationshipPrerequisite, participant: RelationshipParticipant) -> bool:
    """Return whether one clause sacrifices an object other than its own ability source."""
    return (
        clause.kind in (PrerequisiteKind.COST, PrerequisiteKind.CONDITION)
        and clause.subject == "input"
        and clause.operation == "sacrifice"
        and clause.required_card_id != participant.card_id
        and clause.controller == "you"
        and clause.exclusion in ("none", "ability_source")
        and ("creature" in clause.card_types or "artifact" in clause.card_types)
    )


def _death_payoff_clause(
    *, clause: RelationshipPrerequisite, participant: RelationshipParticipant
) -> bool:
    """Return whether one clause rewards creatures dying beyond its own ability source."""
    return (
        clause.kind in (PrerequisiteKind.TRIGGER, PrerequisiteKind.CONDITION)
        and clause.operation == "die"
        and clause.required_card_id != participant.card_id
        and "creature" in clause.card_types
    )


def _self_mill_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause mills from your library into your graveyard."""
    return (
        clause.kind is PrerequisiteKind.CONDITION
        and clause.operation == "mill"
        and _declared_zone(clause.source_zone, CapabilityZone.LIBRARY, "you")
        and _declared_zone(clause.destination_zone, CapabilityZone.GRAVEYARD, "you")
    )


def _discard_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause discards your card from hand into your graveyard."""
    return (
        clause.kind in (PrerequisiteKind.COST, PrerequisiteKind.CONDITION)
        and clause.operation == "discard"
        and clause.object_kind == "card"
        and _declared_zone(clause.source_zone, CapabilityZone.HAND, "you")
        and _declared_zone(clause.destination_zone, CapabilityZone.GRAVEYARD, "you")
    )


def _draw_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause is a card-drawing condition."""
    return clause.kind is PrerequisiteKind.CONDITION and clause.operation == "draw"


def _return_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause is a condition that returns a card."""
    return clause.kind is PrerequisiteKind.CONDITION and clause.operation == "return"


def _recursion_payoff_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause rewards a return, a death, or leaving the graveyard."""
    if clause.kind not in (PrerequisiteKind.TRIGGER, PrerequisiteKind.CONDITION):
        return False
    if clause.operation in ("return", "die"):
        return True
    return clause.operation == "leave" and _declared_zone(
        clause.source_zone, CapabilityZone.GRAVEYARD, None
    )


def _graveyard_payoff_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause counts the contents of a graveyard."""
    if clause.kind not in (PrerequisiteKind.CONDITION, PrerequisiteKind.THRESHOLD):
        return False
    if clause.operation != "count":
        return False
    if _declared_zone(clause.source_zone, CapabilityZone.GRAVEYARD, None):
        return True
    if _declared_zone(clause.destination_zone, CapabilityZone.GRAVEYARD, None):
        return True
    return "graveyard" in clause.object_quote.casefold()


def _fodder_clause(*, clause: RelationshipPrerequisite, participant: RelationshipParticipant) -> bool:
    """Return whether one clause sacrifices or returns its own creature participant."""
    if clause.subject != "participant" or clause.required_card_id != participant.card_id:
        return False
    if "creature" not in clause.card_types:
        return False
    if clause.kind is PrerequisiteKind.COST:
        return clause.operation == "sacrifice"
    if clause.kind is PrerequisiteKind.CONDITION:
        return clause.operation in ("sacrifice", "return")
    return False


def _declared_zone(
    zone: RelationshipZone | None,
    expected: CapabilityZone,
    player: str | None,
) -> bool:
    """Return whether one declared zone states an expected zone and optional player."""
    if zone is None or zone.zone is not expected:
        return False
    return player is None or zone.player == player


__all__ = [
    "CardRelationship",
    "PrerequisiteProjectionError",
    "RELATIONSHIP_PREREQUISITE_PROJECTION_SCHEMA_VERSION",
    "RELATIONSHIP_ZONE_PLAYERS",
    "RelationshipParticipant",
    "RelationshipPrerequisite",
    "RelationshipPrerequisiteProjection",
    "RelationshipTiming",
    "RelationshipZone",
    "role_anchor_covered",
    "validate_prerequisite_projection",
    "validate_prerequisite_sources",
    "validate_relationship_participant_sources",
    "validate_relationship_pins",
    "validate_relationship_sources",
]
