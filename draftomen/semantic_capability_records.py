"""Strict immutable records for source-bound card capabilities."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
import json
from typing import Any, Self

from draftomen.semantic_enrichment_records import (
    FindingReview,
    FindingStatus,
    OracleEvidence,
    SemanticEnrichmentError,
    require_utf8_text,
)
from draftomen.semantic_roles import Role


class CapabilityZone(StrEnum):
    """Game zone referenced by a source-bound capability."""

    LIBRARY = "library"
    HAND = "hand"
    BATTLEFIELD = "battlefield"
    GRAVEYARD = "graveyard"
    EXILE = "exile"
    STACK = "stack"


class QuantityRelation(StrEnum):
    """Explicit relation between a stated quantity and its value."""

    EXACTLY = "exactly"
    AT_LEAST = "at_least"
    AT_MOST = "at_most"
    VARIABLE = "variable"


class PrerequisiteKind(StrEnum):
    """Kind of prerequisite bound to a capability."""

    COST = "cost"
    TRIGGER = "trigger"
    CONDITION = "condition"
    THRESHOLD = "threshold"


def _keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    """Require exactly the expected object keys."""
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        details: list[str] = []
        if unknown:
            details.append("unknown fields: " + ", ".join(sorted(repr(item) for item in unknown)))
        if missing:
            details.append("missing fields: " + ", ".join(sorted(repr(item) for item in missing)))
        raise SemanticEnrichmentError(f"{field_name} has invalid keys ({'; '.join(details)}).")


def _identifier(value: Any, field_name: str) -> str:
    """Validate and strip a nonblank identifier."""
    if not isinstance(value, str) or not value.strip():
        raise SemanticEnrichmentError(f"{field_name} must be a nonblank string.")
    return require_utf8_text(value, field_name).strip()


def _exact_text(value: Any, field_name: str) -> str:
    """Validate nonblank text without changing it."""
    if not isinstance(value, str) or not value.strip():
        raise SemanticEnrichmentError(f"{field_name} must be nonblank text.")
    return require_utf8_text(value, field_name)


def _optional_text(value: Any, field_name: str) -> str | None:
    """Validate nullable nonblank text."""
    if value is None:
        return None
    return _exact_text(value, field_name)


def _integer(value: Any, field_name: str, *, positive: bool = False) -> int:
    """Validate an integer with an optional positive lower bound."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticEnrichmentError(f"{field_name} must be an integer.")
    lower_bound = 1 if positive else 0
    if value < lower_bound:
        bound = "positive" if positive else "non-negative"
        raise SemanticEnrichmentError(f"{field_name} must be a {bound} integer.")
    return value


def _optional_integer(value: Any, field_name: str, *, positive: bool = False) -> int | None:
    """Validate a nullable integer."""
    if value is None:
        return None
    return _integer(value, field_name, positive=positive)


def _tuple(value: Any, field_name: str) -> tuple[Any, ...]:
    """Require a Python tuple instead of coercing an iterable."""
    if not isinstance(value, tuple):
        raise SemanticEnrichmentError(f"{field_name} must be a tuple.")
    return value


def _json_array(value: Any, field_name: str) -> tuple[Any, ...]:
    """Decode only JSON arrays as tuple-valued fields."""
    if not isinstance(value, list):
        raise SemanticEnrichmentError(f"{field_name} must be a JSON array.")
    return tuple(value)


def _enum_member(value: Any, field_name: str, enum_type: type[Enum]) -> Any:
    """Require an enum instance instead of a raw value."""
    if not isinstance(value, enum_type):
        raise SemanticEnrichmentError(f"{field_name} must be a {enum_type.__name__}.")
    return value


def _enum_from_json(value: Any, field_name: str, enum_type: type[Enum]) -> Any:
    """Decode an enum member from its exact JSON string."""
    if not isinstance(value, str):
        raise SemanticEnrichmentError(f"{field_name} must be a string.")
    try:
        return enum_type(value)
    except ValueError as error:
        raise SemanticEnrichmentError(f"{field_name} has an unsupported value.") from error


def _optional_enum_member(value: Any, field_name: str, enum_type: type[Enum]) -> Any:
    """Require a nullable enum instance."""
    if value is None:
        return None
    return _enum_member(value, field_name, enum_type)


def _optional_enum_from_json(value: Any, field_name: str, enum_type: type[Enum]) -> Any:
    """Decode a nullable enum member from its exact JSON string."""
    if value is None:
        return None
    return _enum_from_json(value, field_name, enum_type)


def _optional_record(value: Any, loader: Callable[[Mapping[str, Any]], Any]) -> Any:
    """Decode a nullable nested record."""
    if value is None:
        return None
    return loader(value)


def _canonical(
    values: tuple[Any, ...],
    *,
    field_name: str,
    key: Callable[[Any], object],
) -> tuple[Any, ...]:
    """Reject duplicate identities and sort a tuple canonically."""
    identities = [key(item) for item in values]
    if len(set(identities)) != len(identities):
        raise SemanticEnrichmentError(f"{field_name} contains duplicate entries.")
    return tuple(sorted(values, key=key))


def _canonical_json_bytes(value: Any) -> bytes:
    """Encode a value as canonical compact JSON bytes."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise SemanticEnrichmentError("value cannot be encoded as canonical JSON.") from error


def _nested_json_array(
    value: Any,
    field_name: str,
    loader: Callable[[Mapping[str, Any]], Any],
) -> tuple[Any, ...]:
    """Decode a JSON array of nested records."""
    return tuple(loader(item) for item in _json_array(value, field_name))


@dataclass(frozen=True, slots=True)
class CapabilityQuantity:
    """A stated quantity with its explicit relation."""

    value: int | None
    relation: QuantityRelation

    def __post_init__(self) -> None:
        relation = _enum_member(self.relation, "relation", QuantityRelation)
        object.__setattr__(self, "relation", relation)
        value = self.value
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SemanticEnrichmentError("value must be a positive integer or null.")
            if relation is QuantityRelation.VARIABLE:
                raise SemanticEnrichmentError("variable quantities must not carry a value.")
        elif relation is not QuantityRelation.VARIABLE:
            raise SemanticEnrichmentError("fixed quantities require an integer value.")

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible quantity object."""
        return {"value": self.value, "relation": self.relation.value}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        """Decode and validate one capability quantity object."""
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("capability quantity must be an object.")
        _keys(value, {"value", "relation"}, "capability quantity")
        return cls(
            value=value["value"],
            relation=_enum_from_json(value["relation"], "relation", QuantityRelation),
        )


@dataclass(frozen=True, slots=True)
class CapabilityPrerequisite:
    """A structured prerequisite bound to one capability."""

    kind: PrerequisiteKind
    quantity: CapabilityQuantity | None
    timing: str | None
    source_zone: CapabilityZone | None
    destination_zone: CapabilityZone | None
    evidence: OracleEvidence

    def __post_init__(self) -> None:
        _enum_member(self.kind, "kind", PrerequisiteKind)
        if self.quantity is not None and not isinstance(self.quantity, CapabilityQuantity):
            raise SemanticEnrichmentError("quantity must be a CapabilityQuantity or null.")
        object.__setattr__(self, "timing", _optional_text(self.timing, "timing"))
        _optional_enum_member(self.source_zone, "source_zone", CapabilityZone)
        _optional_enum_member(self.destination_zone, "destination_zone", CapabilityZone)
        if type(self.evidence) is not OracleEvidence:
            raise SemanticEnrichmentError("evidence must be an OracleEvidence record.")

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible prerequisite object."""
        return {
            "kind": self.kind.value,
            "quantity": self.quantity.to_json() if self.quantity is not None else None,
            "timing": self.timing,
            "source_zone": self.source_zone.value if self.source_zone is not None else None,
            "destination_zone": (
                self.destination_zone.value if self.destination_zone is not None else None
            ),
            "evidence": self.evidence.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        """Decode and validate one capability prerequisite object."""
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("capability prerequisite must be an object.")
        _keys(
            value,
            {"kind", "quantity", "timing", "source_zone", "destination_zone", "evidence"},
            "capability prerequisite",
        )
        return cls(
            kind=_enum_from_json(value["kind"], "kind", PrerequisiteKind),
            quantity=_optional_record(value["quantity"], CapabilityQuantity.from_json),
            timing=value["timing"],
            source_zone=_optional_enum_from_json(
                value["source_zone"], "source_zone", CapabilityZone
            ),
            destination_zone=_optional_enum_from_json(
                value["destination_zone"], "destination_zone", CapabilityZone
            ),
            evidence=OracleEvidence.from_json(value["evidence"]),
        )


@dataclass(frozen=True, slots=True)
class CardCapability:
    """One source-bound capability of a single canonical card face."""

    finding_id: str
    card_id: int
    card_name: str
    face_index: int | None
    face_name: str | None
    role: Role
    quantity: CapabilityQuantity | None
    timing: str | None
    source_zone: CapabilityZone | None
    destination_zone: CapabilityZone | None
    prerequisites: tuple[CapabilityPrerequisite, ...]
    evidence: tuple[OracleEvidence, ...]
    review: FindingReview
    run_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", _identifier(self.finding_id, "finding_id"))
        card_id = _integer(self.card_id, "card_id", positive=True)
        object.__setattr__(self, "card_id", card_id)
        object.__setattr__(self, "card_name", _identifier(self.card_name, "card_name"))
        face_index = _optional_integer(self.face_index, "face_index")
        object.__setattr__(self, "face_index", face_index)
        object.__setattr__(self, "face_name", _optional_text(self.face_name, "face_name"))
        _enum_member(self.role, "role", Role)
        if self.quantity is not None and not isinstance(self.quantity, CapabilityQuantity):
            raise SemanticEnrichmentError("quantity must be a CapabilityQuantity or null.")
        object.__setattr__(self, "timing", _optional_text(self.timing, "timing"))
        _optional_enum_member(self.source_zone, "source_zone", CapabilityZone)
        _optional_enum_member(self.destination_zone, "destination_zone", CapabilityZone)
        prerequisites = _tuple(self.prerequisites, "prerequisites")
        if any(type(item) is not CapabilityPrerequisite for item in prerequisites):
            raise SemanticEnrichmentError(
                "prerequisites must contain CapabilityPrerequisite records."
            )
        for prerequisite in prerequisites:
            if (
                prerequisite.evidence.card_id != card_id
                or prerequisite.evidence.face_index != face_index
            ):
                raise SemanticEnrichmentError(
                    "prerequisites must belong to the capability card face."
                )
        object.__setattr__(
            self,
            "prerequisites",
            _canonical(
                prerequisites,
                field_name="prerequisites",
                key=lambda item: _canonical_json_bytes(item.to_json()),
            ),
        )
        evidence = _tuple(self.evidence, "evidence")
        if not evidence:
            raise SemanticEnrichmentError("evidence must not be empty.")
        if any(type(item) is not OracleEvidence for item in evidence):
            raise SemanticEnrichmentError("evidence must contain OracleEvidence records.")
        if any(item.card_id != card_id or item.face_index != face_index for item in evidence):
            raise SemanticEnrichmentError("evidence must belong to the capability card face.")
        object.__setattr__(
            self,
            "evidence",
            _canonical(
                evidence,
                field_name="evidence",
                key=lambda item: (
                    item.card_id,
                    item.face_index is not None,
                    item.face_index if item.face_index is not None else 0,
                    item.quote,
                ),
            ),
        )
        if not isinstance(self.review, FindingReview):
            raise SemanticEnrichmentError("review must be a FindingReview.")
        if self.review.status not in (FindingStatus.ACCEPTED, FindingStatus.UNCERTAIN):
            raise SemanticEnrichmentError("review must be accepted or uncertain.")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible capability object."""
        return {
            "finding_id": self.finding_id,
            "card_id": self.card_id,
            "card_name": self.card_name,
            "face_index": self.face_index,
            "face_name": self.face_name,
            "role": self.role.value,
            "quantity": self.quantity.to_json() if self.quantity is not None else None,
            "timing": self.timing,
            "source_zone": self.source_zone.value if self.source_zone is not None else None,
            "destination_zone": (
                self.destination_zone.value if self.destination_zone is not None else None
            ),
            "prerequisites": [item.to_json() for item in self.prerequisites],
            "evidence": [item.to_json() for item in self.evidence],
            "review": self.review.to_json(),
            "run_id": self.run_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        """Decode and validate one card capability object."""
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("capability must be an object.")
        _keys(
            value,
            {
                "finding_id",
                "card_id",
                "card_name",
                "face_index",
                "face_name",
                "role",
                "quantity",
                "timing",
                "source_zone",
                "destination_zone",
                "prerequisites",
                "evidence",
                "review",
                "run_id",
            },
            "capability",
        )
        return cls(
            finding_id=value["finding_id"],
            card_id=value["card_id"],
            card_name=value["card_name"],
            face_index=value["face_index"],
            face_name=value["face_name"],
            role=_enum_from_json(value["role"], "role", Role),
            quantity=_optional_record(value["quantity"], CapabilityQuantity.from_json),
            timing=value["timing"],
            source_zone=_optional_enum_from_json(
                value["source_zone"], "source_zone", CapabilityZone
            ),
            destination_zone=_optional_enum_from_json(
                value["destination_zone"], "destination_zone", CapabilityZone
            ),
            prerequisites=_nested_json_array(
                value["prerequisites"],
                "prerequisites",
                CapabilityPrerequisite.from_json,
            ),
            evidence=_nested_json_array(value["evidence"], "evidence", OracleEvidence.from_json),
            review=FindingReview.from_json(value["review"]),
            run_id=value["run_id"],
        )


__all__ = [
    "CapabilityPrerequisite",
    "CapabilityQuantity",
    "CapabilityZone",
    "CardCapability",
    "PrerequisiteKind",
    "QuantityRelation",
]
