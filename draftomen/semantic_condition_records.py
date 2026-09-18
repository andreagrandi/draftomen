"""Strict immutable records for derived draft-potential condition maps.

These records carry compiler-derived helper associations that are independent
from reviewed relationship findings: a condition source is a pinned card face
with its own field fingerprint, and capabilities are bounded helper or payoff
nodes joined by explicit, source-supported interactions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
from typing import Any, Literal, Self

from draftomen.semantic_capability_records import (
    CapabilityQuantity,
    QuantityRelation,
    _canonical_json_bytes,
    _enum_from_json,
    _enum_member,
    _optional_text,
)
from draftomen.semantic_enrichment_records import (
    CardSourcePin,
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
)
from draftomen.semantic_relationship_records import QualificationKind, _resolved_span

CONDITION_MAP_SCHEMA_VERSION = 1
CONDITION_CAPABILITY_ID_PREFIX = "condition-capability:"

CONDITION_PAYOFF_KINDS: Mapping[str, str] = {
    "ferocious": "power_threshold",
    "landfall": "land_entry_event",
    "storied": "storied_attainment",
}
CONDITION_ENABLER_KINDS: Mapping[str, frozenset[str]] = {
    "ferocious": frozenset({"amass_growth", "created_creature_power", "creature_power"}),
    "landfall": frozenset({"additional_land_play", "land_card", "land_entry"}),
    "storied": frozenset({"created_qualifying_permanents", "qualifying_permanent"}),
}

ConditionFamily = Literal["landfall", "ferocious", "storied"]
ConditionRole = Literal["enabler", "payoff"]
ConditionEvidenceField = Literal["type_line", "oracle_text", "power"]
ConditionController = Literal["you", "opponent", "any"]
ConditionSupport = Literal["can_enable", "contributes"]

_FAMILIES: frozenset[str] = frozenset(CONDITION_PAYOFF_KINDS)
_ROLES: frozenset[str] = frozenset({"enabler", "payoff"})
_EVIDENCE_FIELDS: frozenset[str] = frozenset({"type_line", "oracle_text", "power"})
_CONTROLLERS: frozenset[str] = frozenset({"you", "opponent", "any"})
_SUPPORTS: frozenset[str] = frozenset({"can_enable", "contributes"})
_SCOPES: frozenset[str] = frozenset({"draft_potential"})
_QUANTITY_RELATIONS: Mapping[str, frozenset[QuantityRelation]] = {
    "additional_land_play": frozenset(
        {QuantityRelation.AT_MOST, QuantityRelation.EXACTLY, QuantityRelation.VARIABLE}
    ),
    "amass_growth": frozenset({QuantityRelation.EXACTLY, QuantityRelation.VARIABLE}),
    "created_creature_power": frozenset({QuantityRelation.EXACTLY}),
    "created_qualifying_permanents": frozenset(
        {QuantityRelation.AT_MOST, QuantityRelation.EXACTLY, QuantityRelation.VARIABLE}
    ),
    "creature_power": frozenset({QuantityRelation.EXACTLY}),
    "land_card": frozenset({QuantityRelation.EXACTLY}),
    "land_entry": frozenset({QuantityRelation.AT_MOST, QuantityRelation.EXACTLY, QuantityRelation.VARIABLE}),
    "land_entry_event": frozenset(),
    "power_threshold": frozenset({QuantityRelation.AT_LEAST}),
    "qualifying_permanent": frozenset({QuantityRelation.EXACTLY}),
    "storied_attainment": frozenset({QuantityRelation.AT_LEAST}),
}
_NULLABLE_QUANTITY_KINDS: frozenset[str] = frozenset({"land_entry", "land_entry_event"})
_FIXED_QUANTITY_VALUES: Mapping[str, int] = {
    "land_card": 1,
    "qualifying_permanent": 1,
    "storied_attainment": 3,
}
_PLACEHOLDER_IDENTITY: str = "0" * 64


def _declared_kinds(*, family: str, role: str) -> frozenset[str]:
    """Return the closed capability kinds one condition family and role declares."""
    if role == "payoff":
        return frozenset({CONDITION_PAYOFF_KINDS[family]})
    return CONDITION_ENABLER_KINDS[family]


def _condition_quantity(*, kind: str, quantity: CapabilityQuantity | None) -> CapabilityQuantity | None:
    """Require the closed quantity shape one condition capability kind declares."""
    if quantity is None:
        if kind in _NULLABLE_QUANTITY_KINDS:
            return None
        raise SemanticEnrichmentError(f"condition capability kind {kind} requires a quantity.")
    if not isinstance(quantity, CapabilityQuantity):
        raise SemanticEnrichmentError("quantity must be a CapabilityQuantity or null.")
    if quantity.relation not in _QUANTITY_RELATIONS[kind]:
        raise SemanticEnrichmentError(f"condition capability kind {kind} cannot carry that quantity relation.")
    required = _FIXED_QUANTITY_VALUES.get(kind)
    if required is not None and quantity.value != required:
        raise SemanticEnrichmentError(f"condition capability kind {kind} must state the quantity {required}.")
    return quantity


def _source_payload(
    *,
    card_id: int,
    face_index: int | None,
    card_source_sha256: str,
    type_line: str | None,
    oracle_text: str | None,
    power: str | None,
) -> dict[str, object]:
    """Return the shared JSON shape one condition source fingerprint covers."""
    return {
        "card_id": card_id,
        "face_index": face_index,
        "card_source_sha256": card_source_sha256,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "power": power,
    }


def condition_source_sha256(
    *,
    card_id: int,
    face_index: int | None,
    card_source_sha256: str,
    type_line: str | None,
    oracle_text: str | None,
    power: str | None,
) -> str:
    """Return the fingerprint of one source's copied printed fields."""
    payload = _source_payload(
        card_id=card_id,
        face_index=face_index,
        card_source_sha256=card_source_sha256,
        type_line=type_line,
        oracle_text=oracle_text,
        power=power,
    )
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class ConditionSource:
    """One pinned card face with the printed fields its evidence resolves in."""

    card_id: int
    face_index: int | None
    card_source_sha256: str
    type_line: str | None
    oracle_text: str | None
    power: str | None
    source_sha256: str

    def __post_init__(self) -> None:
        card_id = _integer(self.card_id, "card_id", positive=True)
        face_index = _optional_integer(self.face_index, "face_index")
        card_source_sha256 = _hash(self.card_source_sha256, "card_source_sha256")
        type_line = _optional_text(self.type_line, "type_line")
        oracle_text = _optional_text(self.oracle_text, "oracle_text")
        power = _optional_text(self.power, "power")
        source_sha256 = _hash(self.source_sha256, "source_sha256")
        expected = condition_source_sha256(
            card_id=card_id,
            face_index=face_index,
            card_source_sha256=card_source_sha256,
            type_line=type_line,
            oracle_text=oracle_text,
            power=power,
        )
        if source_sha256 != expected:
            raise SemanticEnrichmentError("source_sha256 does not match the copied condition source fields.")
        object.__setattr__(self, "card_id", card_id)
        object.__setattr__(self, "face_index", face_index)
        object.__setattr__(self, "card_source_sha256", card_source_sha256)
        object.__setattr__(self, "type_line", type_line)
        object.__setattr__(self, "oracle_text", oracle_text)
        object.__setattr__(self, "power", power)
        object.__setattr__(self, "source_sha256", source_sha256)

    @property
    def face_sort_key(self) -> int:
        """Return the canonical ordering key of this source's face."""
        return -1 if self.face_index is None else self.face_index

    @classmethod
    def create(
        cls,
        *,
        card_id: int,
        face_index: int | None,
        card_source_sha256: str,
        type_line: str | None,
        oracle_text: str | None,
        power: str | None,
    ) -> Self:
        """Build one source whose fingerprint is derived from its copied fields."""
        return cls(
            card_id=card_id,
            face_index=face_index,
            card_source_sha256=card_source_sha256,
            type_line=type_line,
            oracle_text=oracle_text,
            power=power,
            source_sha256=condition_source_sha256(
                card_id=card_id,
                face_index=face_index,
                card_source_sha256=card_source_sha256,
                type_line=type_line,
                oracle_text=oracle_text,
                power=power,
            ),
        )

    def to_json(self) -> dict[str, object]:
        return {
            **_source_payload(
                card_id=self.card_id,
                face_index=self.face_index,
                card_source_sha256=self.card_source_sha256,
                type_line=self.type_line,
                oracle_text=self.oracle_text,
                power=self.power,
            ),
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("condition source must be an object.")
        _keys(
            value,
            {"card_id", "face_index", "card_source_sha256", "type_line", "oracle_text", "power", "source_sha256"},
            "condition source",
        )
        return cls(
            card_id=value["card_id"],
            face_index=value["face_index"],
            card_source_sha256=value["card_source_sha256"],
            type_line=value["type_line"],
            oracle_text=value["oracle_text"],
            power=value["power"],
            source_sha256=value["source_sha256"],
        )


@dataclass(frozen=True, slots=True)
class ConditionEvidence:
    """One exact selector inside a named field of a capability's own source face."""

    field: ConditionEvidenceField
    kind: QualificationKind
    selector: str
    occurrence: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "field", _closed_string(self.field, "field", _EVIDENCE_FIELDS))
        _enum_member(self.kind, "kind", QualificationKind)
        object.__setattr__(self, "selector", _exact_text(self.selector, "selector"))
        object.__setattr__(self, "occurrence", _integer(self.occurrence, "occurrence"))

    def to_json(self) -> dict[str, object]:
        return {
            "field": self.field,
            "kind": self.kind.value,
            "selector": self.selector,
            "occurrence": self.occurrence,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("condition evidence must be an object.")
        _keys(value, {"field", "kind", "selector", "occurrence"}, "condition evidence")
        return cls(
            field=value["field"],
            kind=_enum_from_json(value["kind"], "kind", QualificationKind),
            selector=value["selector"],
            occurrence=value["occurrence"],
        )


def _capability_payload(capability: ConditionCapability) -> dict[str, object]:
    """Return the shared JSON shape of one condition capability without its identity."""
    return {
        "family": capability.family,
        "role": capability.role,
        "kind": capability.kind,
        "source_card_id": capability.source_card_id,
        "source_face_index": capability.source_face_index,
        "controller": capability.controller,
        "quantity": capability.quantity.to_json() if capability.quantity is not None else None,
        "evidence": [item.to_json() for item in capability.evidence],
        "source_finding_ids": list(capability.source_finding_ids),
    }


def condition_capability_id(*, capability: ConditionCapability, source_sha256: str) -> str:
    """Return the derived identity of one condition capability for its source fingerprint."""
    payload = _capability_payload(capability)
    payload["source_sha256"] = _hash(source_sha256, "source_sha256")
    digest = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return f"{CONDITION_CAPABILITY_ID_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class ConditionCapability:
    """One bounded helper or payoff node derived from a pinned card source."""

    capability_id: str
    family: ConditionFamily
    role: ConditionRole
    kind: str
    source_card_id: int
    source_face_index: int | None
    controller: ConditionController
    quantity: CapabilityQuantity | None
    evidence: tuple[ConditionEvidence, ...]
    source_finding_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        capability_id = _identifier(self.capability_id, "capability_id")
        if not capability_id.startswith(CONDITION_CAPABILITY_ID_PREFIX):
            raise SemanticEnrichmentError("capability_id must be a derived condition capability identity.")
        digest = _hash(capability_id[len(CONDITION_CAPABILITY_ID_PREFIX) :], "capability_id")
        object.__setattr__(self, "capability_id", f"{CONDITION_CAPABILITY_ID_PREFIX}{digest}")
        family = _closed_string(self.family, "family", _FAMILIES)
        role = _closed_string(self.role, "role", _ROLES)
        kind = _identifier(self.kind, "kind")
        if kind not in _declared_kinds(family=family, role=role):
            raise SemanticEnrichmentError("kind must be a declared condition capability kind for its family and role.")
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "controller", _closed_string(self.controller, "controller", _CONTROLLERS))
        object.__setattr__(self, "source_card_id", _integer(self.source_card_id, "source_card_id", positive=True))
        object.__setattr__(self, "source_face_index", _optional_integer(self.source_face_index, "source_face_index"))
        object.__setattr__(self, "quantity", _condition_quantity(kind=kind, quantity=self.quantity))
        evidence = _tuple(self.evidence, "evidence")
        if not evidence:
            raise SemanticEnrichmentError("evidence must not be empty.")
        if any(not isinstance(item, ConditionEvidence) for item in evidence):
            raise SemanticEnrichmentError("evidence must contain ConditionEvidence records.")
        object.__setattr__(
            self,
            "evidence",
            _canonical(evidence, field_name="evidence", key=lambda item: _canonical_json_bytes(item.to_json())),
        )
        finding_ids = tuple(
            _identifier(item, "source_finding_id")
            for item in _tuple(self.source_finding_ids, "source_finding_ids")
        )
        object.__setattr__(
            self,
            "source_finding_ids",
            _canonical(finding_ids, field_name="source_finding_ids", key=lambda item: item),
        )

    @classmethod
    def create(
        cls,
        *,
        source: ConditionSource,
        family: ConditionFamily,
        role: ConditionRole,
        kind: str,
        controller: ConditionController,
        quantity: CapabilityQuantity | None = None,
        evidence: tuple[ConditionEvidence, ...] = (),
        source_finding_ids: tuple[str, ...] = (),
    ) -> Self:
        """Build one capability whose identity is derived from its pinned source."""
        provisional = cls(
            capability_id=f"{CONDITION_CAPABILITY_ID_PREFIX}{_PLACEHOLDER_IDENTITY}",
            family=family,
            role=role,
            kind=kind,
            source_card_id=source.card_id,
            source_face_index=source.face_index,
            controller=controller,
            quantity=quantity,
            evidence=evidence,
            source_finding_ids=source_finding_ids,
        )
        return replace(
            provisional,
            capability_id=condition_capability_id(capability=provisional, source_sha256=source.source_sha256),
        )

    def to_json(self) -> dict[str, object]:
        return {"capability_id": self.capability_id, **_capability_payload(self)}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("condition capability must be an object.")
        _keys(
            value,
            {
                "capability_id",
                "family",
                "role",
                "kind",
                "source_card_id",
                "source_face_index",
                "controller",
                "quantity",
                "evidence",
                "source_finding_ids",
            },
            "condition capability",
        )
        return cls(
            capability_id=value["capability_id"],
            family=value["family"],
            role=value["role"],
            kind=value["kind"],
            source_card_id=value["source_card_id"],
            source_face_index=value["source_face_index"],
            controller=value["controller"],
            quantity=None
            if value["quantity"] is None
            else CapabilityQuantity.from_json(value["quantity"]),
            evidence=_nested_json_array(value["evidence"], "evidence", ConditionEvidence.from_json),
            source_finding_ids=_json_array(value["source_finding_ids"], "source_finding_ids"),
        )


@dataclass(frozen=True, slots=True)
class ConditionInteraction:
    """One supported helper association between an enabler and a payoff node."""

    enabler_id: str
    payoff_id: str
    support: ConditionSupport

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabler_id", _identifier(self.enabler_id, "enabler_id"))
        object.__setattr__(self, "payoff_id", _identifier(self.payoff_id, "payoff_id"))
        object.__setattr__(self, "support", _closed_string(self.support, "support", _SUPPORTS))

    def to_json(self) -> dict[str, object]:
        return {"enabler_id": self.enabler_id, "payoff_id": self.payoff_id, "support": self.support}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("condition interaction must be an object.")
        _keys(value, {"enabler_id", "payoff_id", "support"}, "condition interaction")
        return cls(
            enabler_id=value["enabler_id"],
            payoff_id=value["payoff_id"],
            support=value["support"],
        )


@dataclass(frozen=True, slots=True)
class ConditionMap:
    """Derived draft-potential condition sources, capabilities, and interactions."""

    schema_version: int
    scope: Literal["draft_potential"]
    sources: tuple[ConditionSource, ...]
    capabilities: tuple[ConditionCapability, ...]
    interactions: tuple[ConditionInteraction, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != CONDITION_MAP_SCHEMA_VERSION
        ):
            raise SemanticEnrichmentError(
                f"Unsupported condition map schema {self.schema_version!r}; "
                f"expected {CONDITION_MAP_SCHEMA_VERSION}."
            )
        object.__setattr__(self, "scope", _closed_string(self.scope, "scope", _SCOPES))
        sources = _tuple(self.sources, "sources")
        if any(not isinstance(item, ConditionSource) for item in sources):
            raise SemanticEnrichmentError("sources must contain ConditionSource records.")
        object.__setattr__(
            self,
            "sources",
            _canonical(sources, field_name="sources", key=lambda item: (item.card_id, item.face_sort_key)),
        )
        capabilities = _tuple(self.capabilities, "capabilities")
        if any(not isinstance(item, ConditionCapability) for item in capabilities):
            raise SemanticEnrichmentError("capabilities must contain ConditionCapability records.")
        if not capabilities:
            raise SemanticEnrichmentError("capabilities must not be empty.")
        capabilities = _canonical(capabilities, field_name="capabilities", key=lambda item: item.capability_id)
        object.__setattr__(self, "capabilities", capabilities)
        interactions = _tuple(self.interactions, "interactions")
        if any(not isinstance(item, ConditionInteraction) for item in interactions):
            raise SemanticEnrichmentError("interactions must contain ConditionInteraction records.")
        object.__setattr__(
            self,
            "interactions",
            _canonical(
                interactions,
                field_name="interactions",
                key=lambda item: (item.enabler_id, item.payoff_id),
            ),
        )
        self._validate_bindings()

    def _validate_bindings(self) -> None:
        """Require pinned sources, derived identities, resolvable evidence, and resolved edges."""
        by_source = {(source.card_id, source.face_index): source for source in self.sources}
        by_capability = {capability.capability_id: capability for capability in self.capabilities}
        for capability in self.capabilities:
            source = by_source.get((capability.source_card_id, capability.source_face_index))
            if source is None:
                raise SemanticEnrichmentError("every condition capability must reference a pinned condition source.")
            expected = condition_capability_id(capability=capability, source_sha256=source.source_sha256)
            if capability.capability_id != expected:
                raise SemanticEnrichmentError("capability_id must be the derived identity of its condition capability.")
            for evidence in capability.evidence:
                text = getattr(source, evidence.field)
                if text is None or _resolved_span(
                    quote=text,
                    selector=evidence.selector,
                    occurrence=evidence.occurrence,
                ) is None:
                    raise SemanticEnrichmentError(
                        "condition evidence must resolve exactly inside its own source field."
                    )
        for interaction in self.interactions:
            enabler = by_capability.get(interaction.enabler_id)
            payoff = by_capability.get(interaction.payoff_id)
            if enabler is None or payoff is None:
                raise SemanticEnrichmentError("condition interactions must reference stored capabilities.")
            if enabler.role != "enabler" or payoff.role != "payoff":
                raise SemanticEnrichmentError("condition interactions must join an enabler to a payoff.")
            if enabler.family != payoff.family:
                raise SemanticEnrichmentError("condition interactions must join capabilities of one family.")

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "sources": [item.to_json() for item in self.sources],
            "capabilities": [item.to_json() for item in self.capabilities],
            "interactions": [item.to_json() for item in self.interactions],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("condition map must be an object.")
        _keys(value, {"schema_version", "scope", "sources", "capabilities", "interactions"}, "condition map")
        return cls(
            schema_version=value["schema_version"],
            scope=value["scope"],
            sources=_nested_json_array(value["sources"], "sources", ConditionSource.from_json),
            capabilities=_nested_json_array(value["capabilities"], "capabilities", ConditionCapability.from_json),
            interactions=_nested_json_array(value["interactions"], "interactions", ConditionInteraction.from_json),
        )


def validate_condition_map_pins(*, condition_map: ConditionMap, pins: Mapping[int, CardSourcePin]) -> None:
    """Require every stored condition source to match its enhancement card pin."""
    for source in condition_map.sources:
        pin = pins.get(source.card_id)
        if pin is None:
            raise SemanticEnrichmentError("condition sources must be pinned card sources.")
        if pin.sha256 != source.card_source_sha256:
            raise SemanticEnrichmentError("condition source hash must match its card source pin.")


__all__ = [
    "CONDITION_CAPABILITY_ID_PREFIX",
    "CONDITION_ENABLER_KINDS",
    "CONDITION_MAP_SCHEMA_VERSION",
    "CONDITION_PAYOFF_KINDS",
    "ConditionCapability",
    "ConditionController",
    "ConditionEvidence",
    "ConditionEvidenceField",
    "ConditionFamily",
    "ConditionInteraction",
    "ConditionMap",
    "ConditionRole",
    "ConditionSource",
    "ConditionSupport",
    "condition_capability_id",
    "condition_source_sha256",
    "validate_condition_map_pins",
]
