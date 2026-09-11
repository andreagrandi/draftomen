"""Deterministic, reusable Limited semantic roles.

This module is a deliberately small domain boundary between enriched card metadata
and downstream draft/deck intelligence.  It does not mutate card metadata or make
recommendation decisions.  Classification is conservative: incomplete or
conflicting metadata produces diagnostics and no guessed role assignments.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

from draftomen.carddb import CardFace, CardInfo, UNKNOWN_SOURCE_PROVENANCE

ROLE_SCHEMA_VERSION = 2
CLASSIFIER_VERSION = "1.1"
PROFILE_SCHEMA_VERSION = 2
RESULT_SCHEMA_VERSION = 1
OVERRIDE_SCHEMA_VERSION = 1



class SemanticRoleError(ValueError):
    """Base error for invalid semantic-role data or configuration."""


class RoleSchemaError(SemanticRoleError):
    """Raised when public role JSON uses an unsupported or malformed schema."""


class RoleProfileError(SemanticRoleError):
    """Raised when a compiled set role profile is malformed or incompatible."""


class Role(str, Enum):
    """The generic, intentionally set-independent Limited role vocabulary."""

    # Interaction
    HARD_REMOVAL = "hard_removal"
    DAMAGE_REMOVAL = "damage_removal"
    DISABLING_REMOVAL = "disabling_removal"
    CONDITIONAL_REMOVAL = "conditional_removal"
    BOUNCE = "bounce"
    TEMPORARY_TAP = "temporary_tap"
    COUNTERSPELL = "counterspell"
    COMBAT_TRICK = "combat_trick"
    # Card advantage and selection
    DRAW = "draw"
    EXTRA_DRAW_ENABLER = "extra_draw_enabler"
    DRAW_SECOND_PAYOFF = "draw_second_payoff"
    LOOT = "loot"
    RUMMAGE = "rummage"
    CANTRIP = "cantrip"
    RECURSION = "recursion"
    CARD_SELECTION = "card_selection"
    TUTOR = "tutor"
    # Creatures, tokens, and typal themes
    LOW_COST_CREATURE = "low_cost_creature"
    EVASIVE_THREAT = "evasive_threat"
    LARGE_CREATURE = "large_creature"
    TOKEN_MAKER = "token_maker"
    GO_WIDE_ENABLER = "go_wide_enabler"
    GO_WIDE_PAYOFF = "go_wide_payoff"
    TYPAL_MEMBER = "typal_member"
    TYPAL_PAYOFF = "typal_payoff"
    # Sacrifice and death
    SACRIFICE_FODDER = "sacrifice_fodder"
    SACRIFICE_OUTLET = "sacrifice_outlet"
    DEATH_PAYOFF = "death_payoff"
    DIES_TRIGGER = "dies_trigger"
    RECURSION_PAYOFF = "recursion_payoff"
    # Graveyard
    SELF_MILL = "self_mill"
    DISCARD_ENABLER = "discard_enabler"
    GRAVEYARD_FILLER = "graveyard_filler"
    GRAVEYARD_PAYOFF = "graveyard_payoff"
    # Permanent types and counters
    ARTIFACT_ENABLER = "artifact_enabler"
    ARTIFACT_PAYOFF = "artifact_payoff"
    ENCHANTMENT_ENABLER = "enchantment_enabler"
    ENCHANTMENT_PAYOFF = "enchantment_payoff"
    EQUIPMENT = "equipment"
    EQUIPMENT_PAYOFF = "equipment_payoff"
    MODIFIED = "modified"
    COUNTERS = "counters"
    # Lands and mana
    MANA_PRODUCER = "mana_producer"
    FIXING = "fixing"
    RAMP = "ramp"
    EXTRA_LAND_ENABLER = "extra_land_enabler"
    LANDFALL_PAYOFF = "landfall_payoff"
    DOMAIN_SUPPORT = "domain_support"
    MANA_SINK = "mana_sink"
    # Threshold and state themes
    POWER_THRESHOLD_ENABLER = "power_threshold_enabler"
    POWER_THRESHOLD_PAYOFF = "power_threshold_payoff"
    POWER_N_ENABLER = "power_threshold_enabler"
    POWER_N_PAYOFF = "power_threshold_payoff"
    PERMANENT_TYPE_THRESHOLD = "permanent_type_threshold"
    SPELL_COUNT_THRESHOLD = "spell_count_threshold"
    COUNTERS_THEME = "counters_theme"
    CAST_FROM_EXILE = "cast_from_exile"
    LIFE_GAIN = "life_gain"
    LIFE_LOSS = "life_loss"
    ATTACK_MATTERS = "attack_matters"


LimitedRole = Role
SemanticRole = Role

# Every canonical member carries one pinned, set-independent definition.  These
# sentences document the existing classifier conditions in player-visible terms;
# they never widen a classifier branch, and downstream prompts quote them verbatim.
_ROLE_DEFINITIONS: dict[Role, str] = {
    # Interaction
    Role.HARD_REMOVAL: "Destroys or exiles a targeted battlefield creature or other permanent you do not control.",
    Role.DAMAGE_REMOVAL: "Deals damage to a targeted creature, planeswalker, battle, permanent or any target.",
    Role.DISABLING_REMOVAL: "Taps an opposing permanent, keeps it from untapping, attacking or blocking, or strips its abilities.",
    Role.CONDITIONAL_REMOVAL: "Removal that the card text makes dependent on a stated condition.",
    Role.BOUNCE: "Returns a targeted battlefield creature or other permanent you do not control to its owner's hand.",
    Role.TEMPORARY_TAP: "Taps an opposing target until end of turn or until your next turn.",
    Role.COUNTERSPELL: "Counters a spell on the stack.",
    Role.COMBAT_TRICK: "Temporarily changes a targeted creature's power, toughness or combat keywords.",
    # Card advantage and selection
    Role.DRAW: "Draws one or more cards.",
    Role.EXTRA_DRAW_ENABLER: "Enables drawing an additional card, or an extra card each turn.",
    Role.DRAW_SECOND_PAYOFF: "Rewards drawing a second card in a turn.",
    Role.LOOT: "Draws cards and then discards cards for selection.",
    Role.RUMMAGE: "Discards cards and then draws cards for selection.",
    Role.CANTRIP: "Draws a card as it enters or as you cast it, replacing itself.",
    Role.RECURSION: "Returns a card from the graveyard so it can be used again.",
    Role.CARD_SELECTION: "Filters or orders the top cards of your library.",
    Role.TUTOR: "Searches your library for a chosen card.",
    # Creatures, tokens, and typal themes
    Role.LOW_COST_CREATURE: "Is a creature with mana value two or less.",
    Role.EVASIVE_THREAT: "Carries an evasion ability such as flying, menace, unblockable or shadow.",
    Role.LARGE_CREATURE: "Is a creature with power four or more, or mana value five or more.",
    Role.TOKEN_MAKER: "Creates one or more creature tokens; treasure, food and other artifact tokens alone do not qualify.",
    Role.GO_WIDE_ENABLER: "Creates two or more creature tokens with one instruction.",
    Role.GO_WIDE_PAYOFF: "Rewards controlling many creatures, such as through a per-creature count or an attack trigger.",
    Role.TYPAL_MEMBER: "Is a creature carrying a subtype that a typal package can build around.",
    Role.TYPAL_PAYOFF: "Rewards a group of creatures sharing a subtype.",
    # Sacrifice and death
    Role.SACRIFICE_FODDER: "The card is a creature that sacrifices itself for value or returns from the graveyard to be sacrificed again; a card that only creates creature tokens is a token maker instead.",
    Role.SACRIFICE_OUTLET: "Provides an ability whose cost or effect sacrifices a creature or artifact you control other than itself.",
    Role.DEATH_PAYOFF: "Rewards creatures dying, whether through a trigger when one or more creatures die or a static ability that pays off a creature dying.",
    Role.DIES_TRIGGER: "Triggers only when this card itself dies.",
    Role.RECURSION_PAYOFF: "Turns a death or a graveyard return into recursion value.",
    # Graveyard
    Role.SELF_MILL: "Puts cards from your library into your graveyard.",
    Role.DISCARD_ENABLER: "Makes you discard cards, putting them into a graveyard.",
    Role.GRAVEYARD_FILLER: "Fills a graveyard with cards through milling or discarding.",
    Role.GRAVEYARD_PAYOFF: "Scales with the number or contents of cards in a graveyard.",
    # Permanent types and counters
    Role.ARTIFACT_ENABLER: "Is an artifact whose text refers to artifacts, clues or treasures.",
    Role.ARTIFACT_PAYOFF: "Rewards artifacts you control or artifact entry triggers.",
    Role.ENCHANTMENT_ENABLER: "Is an enchantment whose text supports an enchantment permanent package.",
    Role.ENCHANTMENT_PAYOFF: "Rewards enchantments you control or enchantment entry triggers.",
    Role.EQUIPMENT: "Is equipment or provides an equip ability.",
    Role.EQUIPMENT_PAYOFF: "Rewards equipped creatures or the equipment you control.",
    Role.MODIFIED: "References the modified state of a permanent.",
    Role.COUNTERS: "Places or references counters on a permanent.",
    # Lands and mana
    Role.MANA_PRODUCER: "Produces mana according to the declared produced-mana metadata in the request.",
    Role.FIXING: "Produces more than one color or resource, or is itself more than one color.",
    Role.RAMP: "Puts an extra land onto the battlefield, or produces net-positive mana as a nonland permanent.",
    Role.EXTRA_LAND_ENABLER: "Lets you play an additional land each turn.",
    Role.LANDFALL_PAYOFF: "Triggers whenever a land enters the battlefield.",
    Role.DOMAIN_SUPPORT: "References domain or the basic land types among your lands.",
    Role.MANA_SINK: "Converts excess mana into variable value or an optional payment.",
    # Threshold and state themes
    Role.POWER_THRESHOLD_ENABLER: "Supplies a creature with power four or more, or one that satisfies a stated power threshold.",
    Role.POWER_THRESHOLD_PAYOFF: "Rewards a creature that meets a stated power threshold.",
    Role.PERMANENT_TYPE_THRESHOLD: "References a required number of permanents of one type.",
    Role.SPELL_COUNT_THRESHOLD: "References a required count of spells.",
    Role.COUNTERS_THEME: "Records counters-theme membership whenever the counters role applies.",
    Role.CAST_FROM_EXILE: "Rewards casting or playing cards from exile.",
    Role.LIFE_GAIN: "Gains life or references lifelink.",
    Role.LIFE_LOSS: "Causes or references life loss.",
    Role.ATTACK_MATTERS: "Rewards attacking or attacking creatures.",
}

ROLE_DEFINITIONS: Mapping[Role, str] = MappingProxyType(_ROLE_DEFINITIONS)


def role_definition(role: Role) -> str:
    """Return the pinned, set-independent definition of one role."""

    if not isinstance(role, Role):
        raise RoleSchemaError("role must be a Role member.")
    definition = _ROLE_DEFINITIONS.get(role)
    if definition is None:
        raise RoleSchemaError(f"role {role.value} has no definition.")
    return definition


@dataclass(frozen=True, slots=True)
class RemovalCharacteristics:
    """Typed details describing how much and what kind of interaction removes."""

    kind: str
    effective_score: float
    targets: tuple[str, ...] = ()
    conditional: bool = False
    scalable: bool = False
    temporary: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise RoleSchemaError("Removal characteristics need a non-empty kind.")
        if not isinstance(self.effective_score, (int, float)) or not math.isfinite(
            float(self.effective_score)
        ) or not 0 <= float(self.effective_score) <= 1:
            raise RoleSchemaError("Removal effective_score must be a finite number from 0 to 1.")
        normalized_kind = self.kind.strip().lower()
        if normalized_kind not in {"destroy", "exile", "damage", "bounce", "disable", "tap"}:
            raise RoleSchemaError(f"Unsupported removal subtype {normalized_kind!r}.")
        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "effective_score", float(self.effective_score))
        object.__setattr__(self, "targets", _string_tuple(self.targets, "removal.targets"))

    def to_json(self) -> dict[str, object]:
        return {
            "conditional": self.conditional,
            "effective_score": self.effective_score,
            "kind": "removal",
            "removal_kind": self.kind,
            "scalable": self.scalable,
            "targets": list(self.targets),
            "temporary": self.temporary,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RemovalCharacteristics:
        _object(value, "removal parameters")
        if value.get("kind") != "removal":
            raise RoleSchemaError("Removal parameters have an invalid kind.")
        return cls(
            kind=_required_str(value.get("removal_kind"), "removal.removal_kind"),
            effective_score=_required_number(value.get("effective_score"), "removal.effective_score"),
            targets=_json_string_tuple(value.get("targets", []), "removal.targets"),
            conditional=_required_bool(value.get("conditional", False), "removal.conditional"),
            scalable=_required_bool(value.get("scalable", False), "removal.scalable"),
            temporary=_required_bool(value.get("temporary", False), "removal.temporary"),
        )


@dataclass(frozen=True, slots=True)
class TypalIdentity:
    """The creature subtypes which make a card relevant to a typal package."""

    subtypes: tuple[str, ...]

    def __post_init__(self) -> None:
        values = tuple(dict.fromkeys(item.strip() for item in self.subtypes if item.strip()))
        if not values:
            raise RoleSchemaError("Typal identity needs at least one subtype.")
        object.__setattr__(self, "subtypes", tuple(sorted(values, key=str.casefold)))

    def to_json(self) -> dict[str, object]:
        return {"kind": "typal_identity", "subtypes": list(self.subtypes)}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TypalIdentity:
        _object(value, "typal identity parameters")
        if value.get("kind") not in {None, "typal_identity"}:
            raise RoleSchemaError("Typal identity parameters have an invalid kind.")
        return cls(_json_string_tuple(value.get("subtypes"), "typal.subtypes"))


@dataclass(frozen=True, slots=True)
class ProducedResources:
    """Typed mana/resources produced by a card or face."""

    resources: tuple[str, ...]

    def __post_init__(self) -> None:
        values = tuple(dict.fromkeys(item.strip().upper() for item in self.resources if item.strip()))
        if not values:
            raise RoleSchemaError("Produced resources need at least one resource.")
        object.__setattr__(self, "resources", tuple(sorted(values)))

    def to_json(self) -> dict[str, object]:
        return {"kind": "produced_resources", "resources": list(self.resources)}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ProducedResources:
        _object(value, "produced-resource parameters")
        if value.get("kind") not in {None, "produced_resources"}:
            raise RoleSchemaError("Produced-resource parameters have an invalid kind.")
        return cls(
            resources=_json_string_tuple(value.get("resources"), "produced.resources"),
        )


@dataclass(frozen=True, slots=True)
class ThresholdParameters:
    """Numeric state threshold, optionally constrained to a permanent type."""

    value: int
    relation: str = "at_least"
    permanent_type: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise RoleSchemaError("Threshold value must be a non-negative integer.")
        if self.relation not in {"at_least", "at_most", "exactly"}:
            raise RoleSchemaError(f"Unsupported threshold relation {self.relation!r}.")
        if self.permanent_type is not None:
            if not isinstance(self.permanent_type, str) or not self.permanent_type.strip():
                raise RoleSchemaError("Threshold permanent_type must be a non-empty string.")
            object.__setattr__(self, "permanent_type", self.permanent_type.strip().lower())

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": "threshold",
            "relation": self.relation,
            "value": self.value,
        }
        if self.permanent_type is not None:
            result["permanent_type"] = self.permanent_type
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ThresholdParameters:
        _object(value, "threshold parameters")
        if value.get("kind") not in {None, "threshold"}:
            raise RoleSchemaError("Threshold parameters have an invalid kind.")
        permanent_type = value.get("permanent_type")
        return cls(
            value=_required_int(value.get("value"), "threshold.value"),
            relation=_required_str(value.get("relation", "at_least"), "threshold.relation"),
            permanent_type=(
                None if permanent_type is None else _required_str(permanent_type, "threshold.permanent_type")
            ),
        )


RoleParameters: TypeAlias = (
    RemovalCharacteristics | TypalIdentity | ProducedResources | ThresholdParameters
)


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    """One role assignment with typed parameters and explainable confidence."""

    role: Role
    confidence: float = 1.0
    provenance: tuple[str, ...] = ("classifier",)
    evidence: tuple[str, ...] = ()
    parameters: RoleParameters | None = None

    def __post_init__(self) -> None:
        try:
            role = self.role if isinstance(self.role, Role) else Role(self.role)
        except (TypeError, ValueError) as error:
            raise RoleSchemaError(f"Unsupported semantic role {self.role!r}.") from error
        if not isinstance(self.confidence, (int, float)) or not math.isfinite(float(self.confidence)) or not 0 <= float(
            self.confidence
        ) <= 1:
            raise RoleSchemaError("Role confidence must be a finite number from 0 to 1.")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "provenance", _string_tuple(self.provenance, "role.provenance"))
        object.__setattr__(self, "evidence", _string_tuple(self.evidence, "role.evidence"))
        parameter_type = type(self.parameters)
        allowed: dict[Role, tuple[type[RoleParameters], ...]] = {
            Role.HARD_REMOVAL: (RemovalCharacteristics,),
            Role.DAMAGE_REMOVAL: (RemovalCharacteristics,),
            Role.DISABLING_REMOVAL: (RemovalCharacteristics,),
            Role.CONDITIONAL_REMOVAL: (RemovalCharacteristics,),
            Role.BOUNCE: (RemovalCharacteristics,),
            Role.TEMPORARY_TAP: (RemovalCharacteristics,),
            Role.TYPAL_MEMBER: (TypalIdentity,),
            Role.TYPAL_PAYOFF: (TypalIdentity,),
            Role.MANA_PRODUCER: (ProducedResources,),
            Role.FIXING: (ProducedResources,),
            Role.POWER_THRESHOLD_ENABLER: (ThresholdParameters,),
            Role.POWER_THRESHOLD_PAYOFF: (ThresholdParameters,),
            Role.PERMANENT_TYPE_THRESHOLD: (ThresholdParameters,),
            Role.SPELL_COUNT_THRESHOLD: (ThresholdParameters,),
        }
        expected = allowed.get(role, ())
        optional_parameter_roles = {Role.CONDITIONAL_REMOVAL}
        if self.parameters is None:
            if role in allowed and role not in optional_parameter_roles:
                raise RoleSchemaError(f"Role {role.value!r} requires typed parameters.")
        elif not expected or parameter_type not in expected:
            raise RoleSchemaError(
                f"Parameters of type {parameter_type.__name__} are not valid for role {role.value!r}."
            )

    @property
    def removal(self) -> RemovalCharacteristics | None:
        return self.parameters if isinstance(self.parameters, RemovalCharacteristics) else None

    @property
    def typal_identity(self) -> TypalIdentity | None:
        return self.parameters if isinstance(self.parameters, TypalIdentity) else None

    @property
    def produced_resources(self) -> ProducedResources | None:
        return self.parameters if isinstance(self.parameters, ProducedResources) else None

    @property
    def threshold(self) -> ThresholdParameters | None:
        return self.parameters if isinstance(self.parameters, ThresholdParameters) else None

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "provenance": list(self.provenance),
            "role": self.role.value,
        }
        if self.parameters is not None:
            result["parameters"] = self.parameters.to_json()
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RoleAssignment:
        _object(value, "role assignment")
        parameters_value = value.get("parameters")
        parameters: RoleParameters | None = None
        if parameters_value is not None:
            _object(parameters_value, "role assignment parameters")
            kind = parameters_value.get("kind")
            if kind == "removal":
                parameters = RemovalCharacteristics.from_json(parameters_value)
            elif kind == "typal_identity":
                parameters = TypalIdentity.from_json(parameters_value)
            elif kind == "produced_resources":
                parameters = ProducedResources.from_json(parameters_value)
            elif kind == "threshold":
                parameters = ThresholdParameters.from_json(parameters_value)
            else:
                raise RoleSchemaError(f"Unsupported role parameter kind {kind!r}.")
        return cls(
            role=value.get("role"),
            confidence=_required_number(value.get("confidence", 1.0), "role.confidence"),
            provenance=_json_string_tuple(value.get("provenance", []), "role.provenance"),
            evidence=_json_string_tuple(value.get("evidence", []), "role.evidence"),
            parameters=parameters,
        )


@dataclass(frozen=True, slots=True)
class ClassificationProvenance:
    """Whole-result provenance, separate from each role's evidence."""

    source: str
    sources: tuple[str, ...] = ()
    input_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise RoleSchemaError("Classification provenance needs a source.")
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "sources", _string_tuple(self.sources, "provenance.sources"))
        object.__setattr__(self, "input_fields", _string_tuple(self.input_fields, "provenance.input_fields"))

    def to_json(self) -> dict[str, object]:
        return {
            "input_fields": list(self.input_fields),
            "source": self.source,
            "sources": list(self.sources),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ClassificationProvenance:
        _object(value, "classification provenance")
        return cls(
            source=_required_str(value.get("source"), "provenance.source"),
            sources=_json_string_tuple(value.get("sources", []), "provenance.sources"),
            input_fields=_json_string_tuple(value.get("input_fields", []), "provenance.input_fields"),
        )


@dataclass(frozen=True, slots=True)
class UnknownMechanicReport:
    """Actionable explanation for an unsupported explicit mechanic or card."""

    card_key: str
    card_name: str | None
    mechanic: str | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.card_key, str) or not self.card_key.strip():
            raise RoleSchemaError("Unknown report needs a card key.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise RoleSchemaError("Unknown report needs an actionable reason.")
        object.__setattr__(self, "card_key", self.card_key.strip())
        object.__setattr__(self, "mechanic", None if self.mechanic is None else self.mechanic.strip())

    def to_json(self) -> dict[str, object]:
        return {
            "card_key": self.card_key,
            "card_name": self.card_name,
            "mechanic": self.mechanic,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> UnknownMechanicReport:
        _object(value, "unknown report")
        card_name = value.get("card_name")
        return cls(
            card_key=_required_str(value.get("card_key"), "unknown.card_key"),
            card_name=None if card_name is None else _required_str(card_name, "unknown.card_name"),
            mechanic=(
                None
                if value.get("mechanic") is None
                else _required_str(value.get("mechanic"), "unknown.mechanic")
            ),
            reason=_required_str(value.get("reason"), "unknown.reason"),
        )


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Stable result for one card, including unknown diagnostics."""

    card_key: str
    card_name: str | None
    set_code: str | None
    assignments: tuple[RoleAssignment, ...]
    classifier_version: str = CLASSIFIER_VERSION
    role_schema_version: int = ROLE_SCHEMA_VERSION
    provenance: ClassificationProvenance = field(default_factory=lambda: ClassificationProvenance("local_classifier"))
    unknown_reports: tuple[UnknownMechanicReport, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.card_key, str) or not self.card_key.strip():
            raise RoleSchemaError("Classification result needs a card key.")
        if self.classifier_version != CLASSIFIER_VERSION:
            raise RoleSchemaError(f"Unsupported classifier version {self.classifier_version!r}.")
        if self.role_schema_version != ROLE_SCHEMA_VERSION:
            raise RoleSchemaError(f"Unsupported role schema version {self.role_schema_version!r}.")
        object.__setattr__(self, "card_key", self.card_key.strip())
        object.__setattr__(self, "set_code", _optional_code(self.set_code))
        assignments = _stable_assignments(self.assignments)
        object.__setattr__(self, "assignments", assignments)
        reports = tuple(sorted(self.unknown_reports, key=_unknown_sort_key))
        object.__setattr__(self, "unknown_reports", reports)
        object.__setattr__(self, "diagnostics", tuple(sorted(set(self.diagnostics))))

    @property
    def is_unknown(self) -> bool:
        return bool(self.unknown_reports) or not self.assignments

    @property
    def roles(self) -> tuple[RoleAssignment, ...]:
        return self.assignments

    def to_json(self) -> dict[str, object]:
        return {
            "card_key": self.card_key,
            "card_name": self.card_name,
            "classifier_version": self.classifier_version,
            "diagnostics": list(self.diagnostics),
            "role_schema_version": self.role_schema_version,
            "roles": [assignment.to_json() for assignment in self.assignments],
            "provenance": self.provenance.to_json(),
            "reports": [report.to_json() for report in self.unknown_reports],
            "schema_version": RESULT_SCHEMA_VERSION,
            "set_code": self.set_code,
        }

    def to_bytes(self) -> bytes:
        return _json_bytes(self.to_json())

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ClassificationResult:
        _object(value, "classification result")
        if value.get("schema_version") != RESULT_SCHEMA_VERSION:
            raise RoleSchemaError(
                f"Unsupported classification result schema "
                f"{value.get('schema_version')!r}; expected {RESULT_SCHEMA_VERSION}."
            )
        roles = value.get("roles")
        reports = value.get("reports", ())
        if not isinstance(roles, list) or not isinstance(reports, list):
            raise RoleSchemaError("Classification result roles and reports must be arrays.")
        return cls(
            card_key=_required_str(value.get("card_key"), "result.card_key"),
            card_name=(
                None
                if value.get("card_name") is None
                else _required_str(value.get("card_name"), "result.card_name")
            ),
            set_code=(
                None
                if value.get("set_code") is None
                else _required_str(value.get("set_code"), "result.set_code")
            ),
            assignments=tuple(
                RoleAssignment.from_json(item)
                for item in _mapping_items(roles, "result.roles")
            ),
            classifier_version=_required_str(value.get("classifier_version"), "result.classifier_version"),
            role_schema_version=_required_int(value.get("role_schema_version"), "result.role_schema_version"),
            provenance=ClassificationProvenance.from_json(value.get("provenance"))
            if isinstance(value.get("provenance"), Mapping)
            else _invalid("result.provenance"),
            unknown_reports=tuple(
                UnknownMechanicReport.from_json(item)
                for item in _mapping_items(reports, "result.reports")
            ),
            diagnostics=_json_string_tuple(value.get("diagnostics", []), "result.diagnostics"),
        )


@dataclass(frozen=True, slots=True)
class ReviewedOverride:
    """A data-only correction keyed by stable identity, never by runtime conditionals."""

    key: str
    add: tuple[RoleAssignment, ...] = ()
    remove: tuple[Role, ...] = ()
    rationale: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise RoleSchemaError("Reviewed override needs a stable card key.")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise RoleSchemaError("Reviewed override needs a rationale.")
        normalized_key = self.key.strip().lower()
        if not _is_stable_identity_key(normalized_key):
            raise RoleSchemaError(
                "Reviewed override keys must use oracle_id, numeric Arena/group ID, "
                "or set plus collector identity; display names are not stable."
            )
        object.__setattr__(self, "key", normalized_key)
        object.__setattr__(self, "add", _stable_assignments(self.add))
        roles: list[Role] = []
        for role in self.remove:
            try:
                parsed = role if isinstance(role, Role) else Role(role)
            except (TypeError, ValueError) as error:
                raise RoleSchemaError(f"Override has unsupported removed role {role!r}.") from error
            roles.append(parsed)
        object.__setattr__(self, "remove", tuple(sorted(set(roles), key=lambda item: item.value)))

    def to_json(self) -> dict[str, object]:
        return {
            "add": [assignment.to_json() for assignment in self.add],
            "key": self.key,
            "rationale": self.rationale,
            "remove": [role.value for role in self.remove],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ReviewedOverride:
        _object(value, "reviewed override")
        added = value.get("add", ())
        removed = value.get("remove", ())
        if not isinstance(added, list) or not isinstance(removed, list):
            raise RoleSchemaError("Override add and remove must be arrays.")
        return cls(
            key=_required_str(value.get("key"), "override.key"),
            add=tuple(
                RoleAssignment.from_json(item)
                for item in _mapping_items(added, "override.add")
            ),
            remove=tuple(removed),
            rationale=_required_str(value.get("rationale"), "override.rationale"),
        )


@dataclass(frozen=True, slots=True)
class OverrideSet:
    """Validated, deterministic collection of reviewed data corrections."""

    overrides: tuple[ReviewedOverride, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.overrides, key=lambda item: item.key))
        if len({item.key for item in ordered}) != len(ordered):
            raise RoleSchemaError("Reviewed overrides contain duplicate stable keys.")
        object.__setattr__(self, "overrides", ordered)

    def for_key(self, key: str) -> ReviewedOverride | None:
        normalized = key.strip().lower()
        return next((item for item in self.overrides if item.key == normalized), None)

    def to_json(self) -> dict[str, object]:
        return {
            "overrides": [item.to_json() for item in self.overrides],
            "schema_version": OVERRIDE_SCHEMA_VERSION,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> OverrideSet:
        _object(value, "override set")
        if value.get("schema_version") != OVERRIDE_SCHEMA_VERSION:
            raise RoleSchemaError(
                f"Unsupported override schema {value.get('schema_version')!r}; expected {OVERRIDE_SCHEMA_VERSION}."
            )
        items = value.get("overrides")
        if not isinstance(items, list):
            raise RoleSchemaError("Override set needs an overrides array.")
        return cls(
            overrides=tuple(
                ReviewedOverride.from_json(item)
                for item in _mapping_items(items, "override")
            )
        )


@dataclass(frozen=True, slots=True)
class ProfileCard:
    """One card's compiled assignments inside a set profile."""

    key: str
    assignments: tuple[RoleAssignment, ...]
    card_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise RoleProfileError("Profile card needs a stable key.")
        object.__setattr__(self, "key", self.key.strip().lower())
        assignments = _stable_assignments(self.assignments)
        if not assignments:
            raise RoleProfileError("Profile cards must contain at least one safe role assignment.")
        object.__setattr__(self, "assignments", assignments)

    def to_json(self) -> dict[str, object]:
        return {
            "card_name": self.card_name,
            "key": self.key,
            "roles": [assignment.to_json() for assignment in self.assignments],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ProfileCard:
        _object(value, "profile card")
        roles = value.get("roles")
        if not isinstance(roles, list):
            raise RoleProfileError("Profile card roles must be an array.")
        card_name = value.get("card_name")
        return cls(
            key=_required_str(value.get("key"), "profile.card.key"),
            card_name=None if card_name is None else _required_str(card_name, "profile.card.card_name"),
            assignments=tuple(
                RoleAssignment.from_json(item)
                for item in _mapping_items(roles, "profile.roles")
            ),
        )


@dataclass(frozen=True, slots=True)
class CompiledRoleProfile:
    """Versioned, exact-set compiled role assignments."""

    set_code: str
    cards: tuple[ProfileCard, ...]
    classifier_version: str = CLASSIFIER_VERSION
    role_schema_version: int = ROLE_SCHEMA_VERSION
    profile_schema_version: int = PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.set_code, str) or not self.set_code.strip():
            raise RoleProfileError("Compiled profile needs a set code.")
        if not isinstance(self.classifier_version, str) or not self.classifier_version.strip():
            raise RoleProfileError("Compiled profile classifier_version must be a non-empty string.")
        if isinstance(self.role_schema_version, bool) or not isinstance(self.role_schema_version, int):
            raise RoleProfileError("Compiled profile role_schema_version must be an integer.")
        if self.profile_schema_version != PROFILE_SCHEMA_VERSION:
            raise RoleProfileError("Unsupported compiled profile schema version.")
        object.__setattr__(self, "classifier_version", self.classifier_version.strip())
        object.__setattr__(self, "set_code", self.set_code.strip().lower())
        ordered = tuple(sorted(self.cards, key=lambda item: item.key))
        if len({item.key for item in ordered}) != len(ordered):
            raise RoleProfileError("Compiled profile contains duplicate card keys.")
        object.__setattr__(self, "cards", ordered)

    def card(self, key: str) -> ProfileCard | None:
        normalized = key.strip().lower()
        return next((item for item in self.cards if item.key == normalized), None)

    def is_compatible(self) -> bool:
        return (
            self.profile_schema_version == PROFILE_SCHEMA_VERSION
            and self.classifier_version == CLASSIFIER_VERSION
            and self.role_schema_version == ROLE_SCHEMA_VERSION
        )

    def to_json(self) -> dict[str, object]:
        return {
            "cards": [card.to_json() for card in self.cards],
            "classifier_version": self.classifier_version,
            "profile_schema_version": self.profile_schema_version,
            "role_schema_version": self.role_schema_version,
            "schema_version": PROFILE_SCHEMA_VERSION,
            "set_code": self.set_code,
        }

    def to_bytes(self) -> bytes:
        return _json_bytes(self.to_json())

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CompiledRoleProfile:
        _object(value, "compiled role profile")
        if value.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise RoleProfileError(
                f"Unsupported profile schema {value.get('schema_version')!r}; expected {PROFILE_SCHEMA_VERSION}."
            )
        if value.get("profile_schema_version", PROFILE_SCHEMA_VERSION) != PROFILE_SCHEMA_VERSION:
            raise RoleProfileError("Unsupported compiled profile profile_schema_version.")
        cards = value.get("cards")
        if not isinstance(cards, list):
            raise RoleProfileError("Compiled profile cards must be an array.")
        return cls(
            set_code=_required_str(value.get("set_code"), "profile.set_code"),
            cards=tuple(ProfileCard.from_json(item) for item in _mapping_items(cards, "profile.cards")),
            classifier_version=_required_str(value.get("classifier_version"), "profile.classifier_version"),
            role_schema_version=_required_int(value.get("role_schema_version"), "profile.role_schema_version"),
            profile_schema_version=_required_int(value.get("schema_version"), "profile.schema_version"),
        )


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """Classification plus explicit profile/fallback status and diagnostics."""

    classification: ClassificationResult
    source: str
    status: str
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source not in {"compiled_profile", "local_classifier"}:
            raise RoleSchemaError(f"Unsupported resolution source {self.source!r}.")
        object.__setattr__(self, "diagnostics", tuple(sorted(set(self.diagnostics))))

    @property
    def assignments(self) -> tuple[RoleAssignment, ...]:
        return self.classification.assignments

    def to_json(self) -> dict[str, object]:
        return {
            "classification": self.classification.to_json(),
            "diagnostics": list(self.diagnostics),
            "source": self.source,
            "status": self.status,
        }


class RoleClassifier:
    """Deterministic metadata classifier and exact-set profile resolver."""

    classifier_version = CLASSIFIER_VERSION
    role_schema_version = ROLE_SCHEMA_VERSION

    def __init__(self, *, overrides: OverrideSet | None = None) -> None:
        self.overrides = overrides or BUNDLED_REVIEWED_OVERRIDES

    def classify(self, card: CardInfo | CardFace | Mapping[str, Any]) -> ClassificationResult:
        return _classify(card, overrides=self.overrides)

    def classify_many(
        self, cards: Iterable[CardInfo | CardFace | Mapping[str, Any]]
    ) -> tuple[ClassificationResult, ...]:
        results = tuple(self.classify(card) for card in cards)
        return tuple(sorted(results, key=lambda result: result.card_key))

    def compile_profile(
        self,
        *,
        set_code: str,
        results: Iterable[ClassificationResult],
    ) -> CompiledRoleProfile:
        return compile_role_profile(set_code=set_code, results=results)

    def resolve(
        self,
        card: CardInfo | CardFace | Mapping[str, Any],
        *,
        profile: CompiledRoleProfile | None = None,
    ) -> ResolutionResult:
        """Resolve one card using the explicit profile precedence contract."""

        mapping = _card_mapping(card)
        key = _card_key(mapping)
        card_set = _optional_code(mapping.get("set_code", mapping.get("set")))
        diagnostics: list[str] = []
        if profile is None:
            diagnostics.append("profile_unavailable:used_local_classifier")
        elif not profile.is_compatible():
            diagnostics.append("profile_incompatible_versions:used_local_classifier")
        elif card_set != profile.set_code:
            diagnostics.append("profile_wrong_set:used_local_classifier")
        else:
            compiled = None
            for candidate in _card_identity_keys(mapping):
                compiled = profile.card(candidate)
                if compiled is not None:
                    break
            if compiled is not None:
                result = ClassificationResult(
                    card_key=key,
                    card_name=compiled.card_name or _optional_str(mapping.get("name")),
                    set_code=card_set,
                    assignments=compiled.assignments,
                    provenance=ClassificationProvenance(
                        source="compiled_profile",
                        sources=("compiled_profile",),
                        input_fields=("set", "stable_card_key"),
                    ),
                )
                return ResolutionResult(
                    classification=result,
                    source="compiled_profile",
                    status="authoritative_exact_set_profile",
                )
            diagnostics.append("profile_missing_card:used_local_classifier")

        result = self.classify(mapping)
        return ResolutionResult(
            classification=result,
            source="local_classifier",
            status="fallback_local_classifier_with_overrides",
            diagnostics=tuple(diagnostics),
        )


def classify_card(
    card: CardInfo | CardFace | Mapping[str, Any],
    *,
    overrides: OverrideSet | Mapping[str, Any] | None = None,
) -> ClassificationResult:
    """Classify one normalized card without mutating its metadata."""

    return _classify(card, overrides=_override_set(overrides))


def classify_cards(
    cards: Iterable[CardInfo | CardFace | Mapping[str, Any]],
    *,
    overrides: OverrideSet | Mapping[str, Any] | None = None,
) -> tuple[ClassificationResult, ...]:
    """Classify cards in stable card-key order."""

    override_set = _override_set(overrides)
    return tuple(sorted((_classify(card, overrides=override_set) for card in cards), key=lambda item: item.card_key))


def resolve_card_roles(
    card: CardInfo | CardFace | Mapping[str, Any],
    *,
    profile: CompiledRoleProfile | None = None,
    overrides: OverrideSet | Mapping[str, Any] | None = None,
) -> ResolutionResult:
    """Resolve one card using the explicit profile precedence contract."""

    return RoleClassifier(overrides=_override_set(overrides)).resolve(card, profile=profile)


def compile_role_profile(
    *,
    set_code: str,
    results: Iterable[ClassificationResult],
) -> CompiledRoleProfile:
    """Compile deterministic local results into an exact-set authoritative profile."""

    normalized_set = _optional_code(set_code)
    if normalized_set is None:
        raise RoleProfileError("A compiled role profile needs a non-empty set code.")
    by_key: dict[str, ClassificationResult] = {}
    for result in results:
        if result.set_code != normalized_set:
            raise RoleProfileError(
                f"Result {result.card_key!r} belongs to {result.set_code!r}, not profile set {normalized_set!r}."
            )
        if result.is_unknown:
            # A blank or unsafe entry must never become an authoritative profile hit.
            continue
        previous = by_key.get(result.card_key.lower())
        if previous is not None:
            if previous.assignments != result.assignments:
                raise RoleProfileError(
                    f"Conflicting duplicate classification for stable card key {result.card_key!r}."
                )
            if (result.card_name or "").casefold() < (previous.card_name or "").casefold():
                by_key[result.card_key.lower()] = result
            continue
        by_key[result.card_key.lower()] = result
    cards = [
        ProfileCard(
            key=result.card_key,
            card_name=result.card_name,
            assignments=tuple(
                replace(
                    assignment,
                    provenance=tuple(dict.fromkeys((*assignment.provenance, "compiled_profile"))),
                )
                for assignment in result.assignments
            ),
        )
        for result in sorted(by_key.values(), key=lambda item: item.card_key.lower())
    ]
    return CompiledRoleProfile(set_code=normalized_set, cards=tuple(cards))


def load_role_profile(path: str | Path) -> CompiledRoleProfile:
    """Load and strictly validate a compiled profile from JSON."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RoleProfileError(f"Could not load role profile {path}: {error}.") from error
    if not isinstance(value, Mapping):
        raise RoleProfileError("Compiled role profile JSON must be an object.")
    return CompiledRoleProfile.from_json(value)


def dump_role_profile(profile: CompiledRoleProfile, path: str | Path) -> Path:
    """Write a stable profile artifact atomically."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(profile.to_bytes())
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return output


def rebuild_role_profile(
    *,
    normalized_path: str | Path,
    set_code: str,
    output_path: str | Path,
    classifier: RoleClassifier | None = None,
) -> CompiledRoleProfile:
    """Classify normalized JSONL and emit a deterministic profile.

    Corpus acquisition remains separate: callers provide an already-built
    normalized artifact, making rebuilds offline and reproducible.
    """

    from draftomen.corpus import load_normalized_rows

    active_classifier = classifier or RoleClassifier()
    rows = tuple(
        row
        for row in load_normalized_rows(normalized_path)
        if _optional_code(row.get("set_code", row.get("set"))) == _optional_code(set_code)
    )
    results = active_classifier.classify_many(rows)
    profile = active_classifier.compile_profile(set_code=set_code, results=results)
    dump_role_profile(profile, output_path)
    return profile


# Mechanics are intentionally an explicit metadata field.  We do not scan prose
# for capitalized words, so ordinary oracle text cannot become an unknown mechanic.
SUPPORTED_MECHANICS = frozenset(
    {
        "deathtouch",
        "defender",
        "double strike",
        "enchant",
        "equip",
        "first strike",
        "flash",
        "flying",
        "haste",
        "hexproof",
        "indestructible",
        "lifelink",
        "menace",
        "prowess",
        "reach",
        "trample",
        "vigilance",
        "ward",
        "kicker",
        "landfall",
        "threshold",
        "domain",
        "cycling",
        "transform",
        "adventure",
        "daybound",
        "nightbound",
        "saga",
        "training",
        "connive",
        "explore",
        "discover",
        "for mirrodin",
        "offspring",
        "bargain",
        "incubate",
        "craft",
        "plot",
        "disguise",
    }
)


def _classify(
    card: CardInfo | CardFace | Mapping[str, Any],
    *,
    overrides: OverrideSet,
) -> ClassificationResult:
    mapping = _card_mapping(card)
    key = _card_key(mapping)
    name = _optional_str(mapping.get("name"))
    set_code = _optional_code(mapping.get("set_code", mapping.get("set")))
    reports: list[UnknownMechanicReport] = []
    diagnostics: list[str] = []

    def report(reason: str, mechanic: str | None = None) -> None:
        reports.append(UnknownMechanicReport(key, name, mechanic, reason))

    if bool(mapping.get("unknown")):
        report("Card metadata is marked unknown; no role mapping is safe.")
    if bool(mapping.get("unsafe_to_classify")):
        reasons = mapping.get("unsafe_reasons", ())
        if isinstance(reasons, (list, tuple, set, frozenset)) and all(
            isinstance(item, str) for item in reasons
        ):
            reason_text = ", ".join(item.strip() for item in reasons if item.strip()) or "metadata is marked unsafe"
        else:
            reason_text = "metadata has malformed unsafe reasons"
        report(f"Unsafe metadata ({reason_text}); resolve source disagreements before classifying.")
    if isinstance(card, CardInfo) and (
        UNKNOWN_SOURCE_PROVENANCE in card.source_provenance
        or (
            not card.source_provenance
            and not mapping.get("oracle_text")
            and not mapping.get("layout")
            and not mapping.get("faces")
        )
    ):
        report("Card metadata has unknown or incomplete source provenance; classification is conservative.")
    layout = _optional_str(mapping.get("layout"))
    if layout and layout not in _SUPPORTED_LAYOUTS:
        report(f"Unsupported card layout {layout!r}; add a face normalizer before classifying.")
    for reason in _validate_canonical_mapping(mapping):
        report(reason)
    if name is None:
        report("Card has no stable name or identity metadata.")
    mechanics = _mechanics(mapping)
    for mechanic in mechanics:
        if mechanic.casefold() not in SUPPORTED_MECHANICS:
            report(
                f"No semantic mapping exists for mechanic {mechanic!r}; add a reusable primitive or reviewed override.",
                mechanic,
            )
    text, faces = _card_text(mapping)
    if not text and not _has_structural_metadata(mapping):
        report("Oracle/type metadata is missing; role classification is conservative.")
    if reports:
        assignments: tuple[RoleAssignment, ...] = ()
    else:
        try:
            assignments = _infer_assignments(mapping, text, faces)
        except (RoleSchemaError, TypeError, ValueError) as error:
            report(f"Metadata cannot be safely classified: {error}.")
            assignments = ()
    override = None
    for candidate in _card_identity_keys(mapping):
        override = overrides.for_key(candidate)
        if override is not None:
            break
    applied_override = override is not None and (not reports or _override_can_resolve(reports))
    if applied_override and override is not None:
        assignments = tuple(
            assignment for assignment in assignments if assignment.role not in override.remove
        ) + override.add
        diagnostics.append(f"reviewed_override:{override.key}")
        reports = []
    source = "local_classifier_with_reviewed_override" if applied_override else "local_classifier"
    provenance_sources = ("normalized_metadata", "local_classifier")
    if applied_override:
        provenance_sources += ("reviewed_override",)
    return ClassificationResult(
        card_key=key,
        card_name=name,
        set_code=set_code,
        assignments=assignments,
        provenance=ClassificationProvenance(
            source=source,
            sources=provenance_sources,
            input_fields=_consumed_fields(mapping, faces),
        ),
        unknown_reports=tuple(reports),
        diagnostics=tuple(diagnostics),
    )


def _infer_assignments(
    mapping: Mapping[str, Any],
    text: str,
    faces: Sequence[Mapping[str, Any]],
) -> tuple[RoleAssignment, ...]:
    if not faces:
        return _infer_assignments_single(mapping, text)
    assignments: list[RoleAssignment] = []
    # Keep each face's text and mechanics isolated.  Card-level fields are shared
    # only as structural defaults; face-local fields always override them.
    for face in faces:
        source = dict(mapping)
        source.update(face)
        source["faces"] = ()
        if "type_line" in face:
            source["types"] = face.get("types", ())
            source["subtypes"] = face.get("subtypes", ())
        for field_name in ("colors", "produced_mana", "mana_value", "power", "toughness"):
            if field_name not in face:
                source[field_name] = ()
        if "keywords" not in face:
            source["keywords"] = ()
        assignments.extend(
            _infer_assignments_single(source, _optional_str(face.get("oracle_text")) or "")
        )
    return _stable_assignments(assignments)


def _infer_assignments_single(
    mapping: Mapping[str, Any],
    text: str,
) -> tuple[RoleAssignment, ...]:
    assignments: list[RoleAssignment] = []
    lower = text.casefold()
    type_line = _optional_str(mapping.get("type_line")) or ""
    types = {part.casefold() for part in _string_tuple(mapping.get("types", ()), "types")}
    if not types:
        types = {part.casefold() for part in re.split(r"[ —-]+", type_line) if part}
    subtypes = _string_tuple(mapping.get("subtypes", ()), "subtypes")
    if not subtypes and "—" in type_line:
        subtypes = tuple(item.strip() for item in type_line.split("—", 1)[1].split())
    mana_value = _optional_number(mapping.get("mana_value", mapping.get("cmc")))
    power = _power(mapping)
    keywords = {
        value.casefold() for value in _string_tuple(mapping.get("keywords", ()), "keywords")
    }
    evidence = _evidence(mapping, ())
    add = assignments.append

    def role(
        semantic_role: Role,
        *,
        confidence: float = 0.82,
        parameters: RoleParameters | None = None,
        why: str,
    ) -> None:
        add(
            RoleAssignment(
                role=semantic_role,
                confidence=confidence,
                evidence=(why, *evidence),
                parameters=parameters,
            )
        )

    # Interaction, retaining characteristics rather than reducing all removal to bool.
    hard_match = re.search(
        r"\b(destroy|exile)\s+target\s+"
        r"(?:(?:nonland|nonbasic)\s+)?"
        r"(creature|permanent|artifact|enchantment|planeswalker|battle)\b",
        lower,
    )
    if hard_match and _valid_battlefield_target(lower, hard_match.start()):
        kind = "exile" if hard_match.group(1) == "exile" else "destroy"
        role(
            Role.HARD_REMOVAL,
            confidence=0.9,
            parameters=RemovalCharacteristics(
                kind=kind,
                effective_score=1.0 if kind == "exile" else 0.92,
                targets=_targets(lower),
                conditional=bool(re.search(r"\b(if|unless|only if|as long as)\b", lower)),
            ),
            why="targeted permanent removal",
        )
    damage_match = re.search(
        r"deals?\s+(?:\{?\w+\}?|\d+|x)\s+damage\s+to\s+"
        r"(?:target\s+)?(creature|planeswalker|battle|permanent)\b|"
        r"deals?\s+(?:\{?\w+\}?|\d+|x)\s+damage\s+to\s+any\s+target\b",
        lower,
    )
    if damage_match:
        role(
            Role.DAMAGE_REMOVAL,
            confidence=0.86,
            parameters=RemovalCharacteristics(
                kind="damage",
                effective_score=0.72,
                targets=_targets(lower),
                conditional=bool(re.search(r"\b(if|unless)\b", lower)),
                scalable=bool(re.search(r"x damage|\d+ or more damage", lower)),
            ),
            why="damage can remove a creature",
        )
    bounce_match = re.search(
        r"return\s+target\s+(?:creature|permanent|artifact|enchantment|planeswalker|battle)\b"
        r"[^.]*\s+to\s+its\s+owner.s\s+hand",
        lower,
    )
    if bounce_match and _valid_battlefield_target(lower, bounce_match.start()):
        role(
            Role.BOUNCE,
            confidence=0.86,
            parameters=RemovalCharacteristics(
                kind="bounce",
                effective_score=0.42,
                targets=_targets(lower),
                temporary=True,
            ),
            why="returns a target to hand",
        )
    temporary_interaction = bool(
        re.search(
            r"until end of turn|until your next turn|"
            r"doesn.t untap during (?:its|the) controller.s next untap step",
            lower,
        )
    )
    disabling_match = re.search(
        r"tap\s+target|doesn.t\s+untap|can.t\s+attack(?:\s+or\s+block)?|"
        r"can.t\s+block|loses\s+all\s+abilities",
        lower,
    )
    if disabling_match and _opposing_subject(lower):
        role(
            Role.DISABLING_REMOVAL,
            confidence=0.78,
            parameters=RemovalCharacteristics(
                kind="disable",
                effective_score=0.5,
                targets=_targets(lower),
                temporary=temporary_interaction,
            ),
            why="disables an opposing permanent",
        )
    if re.search(r"tap target", lower) and temporary_interaction and _opposing_subject(lower):
        role(
            Role.TEMPORARY_TAP,
            confidence=0.84,
            parameters=RemovalCharacteristics(
                kind="tap",
                effective_score=0.3,
                targets=_targets(lower),
                temporary=True,
            ),
            why="temporarily taps a target",
        )
    if re.search(r"counter target spell", lower):
        role(Role.COUNTERSPELL, confidence=0.86, why="counters a spell")
    if re.search(
        r"target creature gets? [+-]?\d+|"
        r"target creature gains? (first strike|flying|trample|hexproof)",
        lower,
    ):
        role(Role.COMBAT_TRICK, confidence=0.79, why="temporarily improves a creature in combat")
    if any(word in lower for word in ("unless", "if", "where")) and any(
        assignment.role in {Role.HARD_REMOVAL, Role.DAMAGE_REMOVAL, Role.DISABLING_REMOVAL}
        for assignment in assignments
    ):
        role(Role.CONDITIONAL_REMOVAL, confidence=0.75, why="removal has an explicit condition")

    # Card advantage and selection.
    if re.search(r"\bdraw(?:\s+\w+){0,4}\s+cards?\b|\bdraw\s+cards?\b", lower):
        role(Role.DRAW, confidence=0.87, why="draws cards")
    if re.search(
        r"draw\s+(?:an additional|a second)\s+card|draw .* card each turn",
        lower,
    ):
        role(Role.EXTRA_DRAW_ENABLER, confidence=0.79, why="creates extra-card-draw opportunities")
    if re.search(r"draw (?:your )?second card|whenever you draw your second", lower):
        role(Role.DRAW_SECOND_PAYOFF, confidence=0.92, why="rewards drawing a second card")
    if re.search(r"draw .*then discard", lower) and not re.search(r"discard .*then draw", lower):
        role(Role.LOOT, confidence=0.84, why="draws then discards for selection")
    if re.search(r"discard .*then draw", lower):
        role(Role.RUMMAGE, confidence=0.84, why="discards then draws for selection")
    if "draw a card" in lower and (
        "enters the battlefield" in lower or "when you cast" in lower or "when this" in lower
    ):
        role(Role.CANTRIP, confidence=0.7, why="replaces itself with a card")
    if re.search(r"return target card .*graveyard|return .* from your graveyard|regrow", lower):
        role(Role.RECURSION, confidence=0.86, why="returns a card from the graveyard")
    if re.search(r"scry|look at the top .* cards? of your library", lower):
        role(Role.CARD_SELECTION, confidence=0.84, why="filters or orders library cards")
    if re.search(r"search your library for", lower):
        role(Role.TUTOR, confidence=0.86, why="searches the library for a card")

    # Creature and typal roles.
    is_creature = "creature" in types or "creature" in type_line.casefold()
    if is_creature and mana_value is not None and mana_value <= 2:
        role(Role.LOW_COST_CREATURE, confidence=0.93, why="creature has mana value at most two")
    if is_creature and (
        (mana_value is not None and mana_value >= 5) or (power is not None and power >= 4)
    ):
        role(Role.LARGE_CREATURE, confidence=0.9, why="creature has substantial power or mana value")
    evasive_keywords = {"flying", "menace", "unblockable", "skulk", "shadow", "horsemanship"}
    if keywords & evasive_keywords or re.search(
        r"\b(flying|menace|unblockable|skulk|shadow|horsemanship)\b", lower
    ):
        role(Role.EVASIVE_THREAT, confidence=0.86, why="has an evasive combat ability")
    token_match = re.search(
        r"\bcreate\s+(one|two|three|four|five|six|seven|eight|nine|ten|\d+|x|a|an)\s+"
        r"([^.;]*?)\s+tokens?\b",
        lower,
    )
    if token_match or re.search(r"\bcreate\s+[^.;]*\btoken\b", lower):
        role(Role.TOKEN_MAKER, confidence=0.88, why="creates one or more tokens")
        if token_match:
            count = _number_token(token_match.group(1))
            body = token_match.group(2)
            if count is None or count > 1 or token_match.group(1) == "x":
                if "creature" in body:
                    role(Role.GO_WIDE_ENABLER, confidence=0.76, why="adds multiple creature bodies to the board")
            if "creature" in body:
                role(Role.SACRIFICE_FODDER, confidence=0.62, why="provides expendable creature bodies")
    if re.search(
        r"for each creature you control|"
        r"whenever (?:one or more )?creatures you control attack|"
        r"two or more creatures you control|"
        r"create .* for each creature you control",
        lower,
    ):
        role(Role.GO_WIDE_PAYOFF, confidence=0.76, why="rewards a wide creature board")
    if is_creature and subtypes:
        role(
            Role.TYPAL_MEMBER,
            confidence=0.68,
            parameters=TypalIdentity(subtypes=subtypes),
            why="creature carries a reusable subtype identity",
        )
    mentioned_types = _mentioned_types(lower, ())
    if mentioned_types or (
        "creatures of the same type" in lower and subtypes
    ):
        role(
            Role.TYPAL_PAYOFF,
            confidence=0.82,
            parameters=TypalIdentity(subtypes=mentioned_types or subtypes),
            why="references a typal group",
        )

    # Sacrifice/death and graveyard.
    sacrifice_match = re.search(
        r"sacrifice (?:a|one|another|any) (?:creature|permanent|artifact|token)",
        lower,
    )
    if sacrifice_match:
        if re.search(r"sacrifice .*:\s|sacrifice .* to\b|as an additional cost", lower):
            role(Role.SACRIFICE_OUTLET, confidence=0.84, why="offers a sacrifice cost or outlet")
    if re.search(r"when(?:ever)? .* dies|whenever .* creature dies", lower):
        role(Role.DIES_TRIGGER, confidence=0.88, why="triggers when a creature dies")
        role(Role.DEATH_PAYOFF, confidence=0.78, why="benefits from a death event")
    if re.search(r"when .* dies, .* return|whenever .* return .* from .*graveyard", lower):
        role(Role.RECURSION_PAYOFF, confidence=0.79, why="turns death or graveyard into recursion")
    if re.search(r"\bmill\b", lower):
        role(Role.SELF_MILL, confidence=0.88, why="puts cards from the library into its graveyard")
        role(Role.GRAVEYARD_FILLER, confidence=0.7, why="fills a graveyard")
    if re.search(r"discard (?:a|one|two|any) card", lower):
        role(Role.DISCARD_ENABLER, confidence=0.82, why="puts cards into a graveyard through discard")
        role(Role.GRAVEYARD_FILLER, confidence=0.62, why="puts cards into a graveyard")
    if re.search(r"for each card in (?:your )?graveyard|as long as .*graveyard|graveyard has", lower):
        role(Role.GRAVEYARD_PAYOFF, confidence=0.81, why="scales with graveyard contents")

    # Permanent types, artifacts, enchantments, equipment, and counters.
    is_artifact = "artifact" in types or "artifact" in type_line.casefold()
    is_enchantment = "enchantment" in types or "enchantment" in type_line.casefold()
    if is_artifact and re.search(r"artifact|create .*clue|create .*treasure", lower):
        role(Role.ARTIFACT_ENABLER, confidence=0.68, why="supports an artifact permanent package")
    if re.search(r"whenever .*artifact|for each artifact|artifacts? you control", lower):
        role(Role.ARTIFACT_PAYOFF, confidence=0.82, why="rewards artifacts")
    if is_enchantment and ("enchantment" in lower or "constellation" in lower):
        role(Role.ENCHANTMENT_ENABLER, confidence=0.68, why="supports an enchantment permanent package")
    if re.search(r"whenever .*enchantment|for each enchantment|enchantments? you control", lower):
        role(Role.ENCHANTMENT_PAYOFF, confidence=0.82, why="rewards enchantments")
    if "equipment" in type_line.casefold() or "equip " in lower or "equip" in keywords:
        role(Role.EQUIPMENT, confidence=0.88, why="is or supplies an equipment effect")
    if re.search(r"whenever .*equipped|equipped creature|for each equipment", lower):
        role(Role.EQUIPMENT_PAYOFF, confidence=0.82, why="rewards equipped creatures or equipment")
    if "modified" in lower:
        role(Role.MODIFIED, confidence=0.84, why="references the modified state")
    if re.search(r"\+1/\+1 counter|counter on|counters? on", lower):
        role(Role.COUNTERS, confidence=0.79, why="creates or references counters")
        role(Role.COUNTERS_THEME, confidence=0.7, why="participates in a counters theme")

    # Lands and mana. Produced mana is carried as typed data, not inferred from prose.
    produced = _string_tuple(mapping.get("produced_mana", ()), "produced_mana")
    if produced:
        resources = ProducedResources(resources=produced)
        role(Role.MANA_PRODUCER, confidence=0.92, parameters=resources, why="metadata declares produced resources")
        colors = _string_tuple(mapping.get("colors", ()), "colors")
        if len(set(produced)) > 1 or len(colors) > 1:
            role(Role.FIXING, confidence=0.85, parameters=resources, why="mana metadata supports multiple colors")
    if re.search(r"search your library for .*basic land|put .*land card .*battlefield", lower):
        role(Role.RAMP, confidence=0.77, why="puts an additional land into play")
    elif (
        "land" not in types
        and "land" not in type_line.casefold()
        and re.search(r"add\s+(?:\{[wubrgc]\}\s*){2,}", lower)
    ):
        role(Role.RAMP, confidence=0.77, why="nonland permanent produces net-positive mana")
    if re.search(r"play an additional land", lower):
        role(Role.EXTRA_LAND_ENABLER, confidence=0.91, why="permits an additional land play")
    if "landfall" in lower or "landfall" in keywords or re.search(r"whenever a land enters", lower):
        role(Role.LANDFALL_PAYOFF, confidence=0.9, why="triggers on land entry")
    if "domain" in lower or "domain" in keywords or re.search(r"basic land types? among lands", lower):
        role(Role.DOMAIN_SUPPORT, confidence=0.88, why="references domain or basic land types")
    if re.search(r"\{x\}|x in its mana cost|pay \{[0-9]+\}:|you may pay .* to", lower):
        role(Role.MANA_SINK, confidence=0.72, why="converts excess mana into variable value")

    # Numeric thresholds and state-based packages.
    for value, relation in _power_thresholds(lower):
        if re.search(r"\btarget\b[^.]{0,80}\bpower\s+\d+\s+or\s+(?:greater|less)\b", lower):
            continue
        if _power_threshold_reward(lower, value):
            role(
                Role.POWER_THRESHOLD_PAYOFF,
                confidence=0.83,
                parameters=ThresholdParameters(value=value, relation=relation),
                why=f"rewards a power-{value} threshold",
            )
        elif power is not None and (
            (relation == "at_least" and power >= value)
            or (relation == "at_most" and power <= value)
        ):
            role(
                Role.POWER_THRESHOLD_ENABLER,
                confidence=0.78,
                parameters=ThresholdParameters(value=value, relation=relation),
                why=f"card power satisfies a power-{value} threshold",
            )
    if is_creature and power is not None and power >= 4 and not _power_thresholds(lower):
        role(
            Role.POWER_THRESHOLD_ENABLER,
            confidence=0.72,
            parameters=ThresholdParameters(value=power),
            why="card power supplies a power threshold body",
        )
    for value, permanent_type in _permanent_thresholds(lower):
        role(
            Role.PERMANENT_TYPE_THRESHOLD,
            confidence=0.84,
            parameters=ThresholdParameters(value=value, permanent_type=permanent_type),
            why=f"references {value} {permanent_type} permanents",
        )
    for value, relation in _spell_thresholds(lower):
        role(
            Role.SPELL_COUNT_THRESHOLD,
            confidence=0.84,
            parameters=ThresholdParameters(value=value, relation=relation),
            why=f"references a spell-count threshold of {value}",
        )
    if re.search(r"cast .* from exile|play .* from exile", lower):
        role(Role.CAST_FROM_EXILE, confidence=0.86, why="rewards casting or playing from exile")
    if re.search(r"gain[s]? life|you gain|lifelink", lower) or "lifelink" in keywords:
        role(Role.LIFE_GAIN, confidence=0.78, why="gains life or references lifelink")
    if re.search(r"lose[s]? life|you lose|pay life", lower):
        role(Role.LIFE_LOSS, confidence=0.78, why="causes or references life loss")
    if re.search(r"whenever .* attack|when .* attacks|attacking creatures", lower):
        role(Role.ATTACK_MATTERS, confidence=0.8, why="rewards attacking or attacking creatures")
    return tuple(assignments)


def _card_mapping(card: CardInfo | CardFace | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(card, Mapping):
        return card
    if isinstance(card, CardInfo):
        value = card.to_json()
        value["set_code"] = card.set_code
        value["types"] = list(card.types)
        return value
    if isinstance(card, CardFace):
        return card.to_json()


def _card_key(card: Mapping[str, Any]) -> str:
    return _card_identity_keys(card)[0]


def _card_identity_keys(card: Mapping[str, Any]) -> tuple[str, ...]:
    keys: list[str] = []
    numeric_values: list[int] = []
    for field in ("arena_id", "grp_id"):
        value = card.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            if value not in numeric_values:
                numeric_values.append(value)
        elif value is not None and str(value).strip():
            keys.append(f"{field}:{value}".lower())
    for value in numeric_values:
        keys.extend((f"arena_id:{value}".lower(), f"grp_id:{value}".lower()))
    set_code = _optional_code(card.get("set_code", card.get("set")))
    collector = _optional_str(card.get("collector_number"))
    if set_code and collector:
        keys.append(f"set:{set_code}:{collector}".lower())
    oracle_id = _optional_str(card.get("oracle_id"))
    if oracle_id:
        keys.append(f"oracle_id:{oracle_id}".lower())
    name = _optional_str(card.get("name"))
    if name:
        keys.append(f"name:{name.casefold()}")
    return tuple(dict.fromkeys(keys)) or ("unknown:card",)


def _card_text(card: Mapping[str, Any]) -> tuple[str, tuple[Mapping[str, Any], ...]]:
    faces_value = card.get("faces", ())
    faces = tuple(item for item in faces_value if isinstance(item, Mapping)) if isinstance(
        faces_value, (list, tuple)
    ) else ()
    pieces = [_optional_str(card.get("oracle_text")) or ""]
    pieces.extend(_optional_str(face.get("oracle_text")) or "" for face in faces)
    return "\n".join(piece for piece in pieces if piece), faces


def _mechanics(card: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for field in ("keywords", "mechanics", "unknown_mechanics"):
        value = card.get(field)
        if isinstance(value, (list, tuple, set, frozenset)):
            values.extend(item for item in value if isinstance(item, str))
    faces = card.get("faces", ())
    if isinstance(faces, (list, tuple)):
        for face in faces:
            if isinstance(face, Mapping):
                value = face.get("keywords", ())
                if isinstance(value, (list, tuple, set, frozenset)):
                    values.extend(item for item in value if isinstance(item, str))
    return tuple(sorted({item.strip() for item in values if item.strip()}, key=str.casefold))


def _validate_canonical_mapping(card: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    array_fields = (
        "keywords",
        "mechanics",
        "unknown_mechanics",
        "types",
        "subtypes",
        "colors",
        "produced_mana",
        "unsafe_reasons",
    )
    scalar_string_fields = (
        "name",
        "oracle_text",
        "type_line",
        "layout",
        "set",
        "set_code",
        "collector_number",
        "mana_cost",
        "oracle_id",
    )
    for field_name in array_fields:
        if field_name in card and not isinstance(card[field_name], (list, tuple)):
            errors.append(f"Malformed {field_name}: expected an array.")
        elif field_name in card and not all(isinstance(item, str) for item in card[field_name]):
            errors.append(f"Malformed {field_name}: expected an array of strings.")
    for field_name in scalar_string_fields:
        if field_name in card and card[field_name] is not None and not isinstance(card[field_name], str):
            errors.append(f"Malformed {field_name}: expected a string.")
    for field_name in ("unknown", "unsafe_to_classify"):
        if field_name in card and not isinstance(card[field_name], bool):
            errors.append(f"Malformed {field_name}: expected a boolean.")
    for field_name in ("mana_value", "cmc"):
        if field_name in card and card[field_name] is not None and (
            isinstance(card[field_name], bool)
            or not isinstance(card[field_name], (int, float))
            or not math.isfinite(float(card[field_name]))
        ):
            errors.append(f"Malformed {field_name}: expected a finite number.")
    for field_name in ("power", "toughness"):
        if field_name in card and card[field_name] is not None and not isinstance(card[field_name], str):
            errors.append(f"Malformed {field_name}: expected a textual value.")
    for field_name in ("arena_id", "grp_id"):
        if field_name in card and card[field_name] is not None and (
            isinstance(card[field_name], bool) or not isinstance(card[field_name], int)
        ):
            errors.append(f"Malformed {field_name}: expected an integer identity.")
    source_provenance = card.get("source_provenance")
    if source_provenance is not None and not isinstance(source_provenance, (list, tuple, Mapping)):
        errors.append("Malformed source_provenance: expected an array or object.")
    faces_value = card.get("faces", ())
    if faces_value not in (None, (), []) and not isinstance(faces_value, (list, tuple)):
        errors.append("Malformed faces: expected an array of face objects.")
    elif isinstance(faces_value, (list, tuple)):
        for index, face in enumerate(faces_value):
            if not isinstance(face, Mapping):
                errors.append(f"Malformed faces[{index}]: expected an object.")
                continue
            for error in _validate_canonical_mapping(face):
                errors.append(f"Malformed faces[{index}].{error.removeprefix('Malformed ')}")
    return tuple(dict.fromkeys(errors))


def _has_structural_metadata(card: Mapping[str, Any]) -> bool:
    fields = ("type_line", "types", "mana_value", "mana_cost", "produced_mana", "power")
    return any(card.get(field) not in (None, (), [], "") for field in fields)


def _evidence(card: Mapping[str, Any], faces: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    fields = (
        "oracle_text",
        "keywords",
        "mechanics",
        "unknown_mechanics",
        "type_line",
        "types",
        "subtypes",
        "colors",
        "mana_value",
        "cmc",
        "power",
        "layout",
        "produced_mana",
        "unknown",
        "unsafe_to_classify",
        "unsafe_reasons",
        "source_provenance",
    )
    consumed = [field for field in fields if card.get(field) not in (None, (), [], "", False)]
    if faces:
        consumed.append("faces")
        for field in ("oracle_text", "keywords", "type_line", "types", "subtypes", "colors", "mana_value", "power", "produced_mana"):
            if any(face.get(field) not in (None, (), [], "") for face in faces):
                consumed.append(f"face.{field}")
    return tuple(dict.fromkeys(consumed))


def _consumed_fields(card: Mapping[str, Any], faces: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return _evidence(card, faces)


def _targets(text: str) -> tuple[str, ...]:
    found = re.findall(
        r"target (creature|permanent|artifact|enchantment|planeswalker|battle|player|spell)",
        text,
    )
    return tuple(sorted(set(found)))


def _valid_battlefield_target(text: str, start: int) -> bool:
    clause = text[start : text.find(".", start) if text.find(".", start) >= 0 else len(text)]
    if re.search(
        r"\b(?:from|in|on)\s+(?:a|the|your|an|its)\s+"
        r"(?:graveyard|library|exile|hand|stack)\b",
        clause,
    ):
        return False
    if re.search(r"\b(?:you|this card|this permanent|its controller)\s+control\b", clause):
        return False
    if re.search(r"\bcontrolled by you\b|\bunder your control\b", clause):
        return False
    return True


def _opposing_subject(text: str) -> bool:
    if re.search(r"\bthis creature\b[^.]{0,80}\b(?:can.t|doesn.t|loses)\b", text):
        return False
    if re.search(r"\bcreatures?\s+you control\b[^.]{0,80}\b(?:can.t|doesn.t|loses)\b", text):
        return False
    if re.search(r"\b(?:you|your)\s+control\b|\bcontrolled by you\b|\bunder your control\b", text):
        return False
    if re.search(r"\btarget\s+(?:player|spell)\b", text):
        return False
    return bool(
        re.search(r"\btarget\s+(?:creature|permanent|artifact|enchantment|planeswalker|battle)\b", text)
        or re.search(r"\b(?:enchanted|equipped)\s+(?:creature|permanent)\b", text)
    )


def _mentioned_types(text: str, fallback: Sequence[str] = ()) -> tuple[str, ...]:
    known = (
        "goblin", "wizard", "elf", "soldier", "human", "spirit", "zombie",
        "vampire", "warrior", "merfolk", "dragon",
    )
    found = tuple(item.title() for item in known if re.search(rf"\b{item}s?\b", text))
    return tuple(sorted(dict.fromkeys(found), key=str.casefold)) or tuple(fallback)


def _number_token(value: str) -> int | None:
    normalized = value.casefold().strip()
    if normalized.isdigit():
        return int(normalized)
    return _ORACLE_CARDINALS.get(normalized)


_ORACLE_CARDINALS = {
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
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


def _power_thresholds(text: str) -> tuple[tuple[int, str], ...]:
    matches = re.findall(r"\bpower\s+(\d+)\s+or\s+(greater|less)\b", text)
    return tuple(
        sorted(
            {
                (int(value), "at_least" if relation == "greater" else "at_most")
                for value, relation in matches
            }
        )
    )


def _power_threshold_reward(text: str, value: int) -> bool:
    return bool(
        re.search(
            rf"\b(?:if|as long as|whenever|for each)\b[^.]*"
            rf"(?:you control|your|creatures?)\b[^.]*\bpower\s+{value}\s+or\s+(?:greater|less)\b",
            text,
        )
    )


def _permanent_thresholds(text: str) -> tuple[tuple[int, str], ...]:
    permanent_types = r"(artifacts?|enchantments?|creatures?|lands?|permanents?)"
    matches = re.findall(rf"\b(\d+)\s+or\s+more\s+{permanent_types}\b", text)
    written_cardinals = "|".join(_ORACLE_CARDINALS)
    matches.extend(
        re.findall(
            rf"\b(?:controls?|has|have|there\s+(?:are|is))\s+"
            rf"({written_cardinals})\s+or\s+more\s+{permanent_types}\b",
            text,
        )
    )
    return tuple(
        sorted(
            {
                (int(value) if value.isdigit() else _ORACLE_CARDINALS[value], kind)
                for value, kind in matches
            }
        )
    )


def _spell_thresholds(text: str) -> tuple[tuple[int, str], ...]:
    matches: set[tuple[int, str]] = set()
    for value in re.findall(r"\bcast\s+(\d+|[a-z]+)\s+or\s+more\s+spells?\b", text):
        number = _number_token(value)
        if number is not None:
            matches.add((number, "at_least"))
    if re.search(r"\b(?:cast|draw) your second spell\b|\bcast your second spell\b", text):
        matches.add((2, "exactly"))
    return tuple(sorted(matches))


def _power(card: Mapping[str, Any]) -> int | None:
    value = card.get("power")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def _stable_assignments(assignments: Iterable[RoleAssignment]) -> tuple[RoleAssignment, ...]:
    unique: dict[tuple[Role, bytes], RoleAssignment] = {}
    for assignment in assignments:
        if not isinstance(assignment, RoleAssignment):
            raise RoleSchemaError("Role assignments must be RoleAssignment values.")
        parameter_key = _json_bytes(
            assignment.parameters.to_json() if assignment.parameters is not None else None
        )
        key = (assignment.role, parameter_key)
        current = unique.get(key)
        if current is None or (
            assignment.confidence,
            _json_bytes(assignment.to_json()),
        ) > (
            current.confidence,
            _json_bytes(current.to_json()),
        ):
            unique[key] = assignment
    return tuple(sorted(unique.values(), key=lambda item: (item.role.value, _json_bytes(item.to_json()))))


def _is_stable_identity_key(key: str) -> bool:
    return bool(
        re.fullmatch(r"oracle_id:[^:\s]+", key)
        or re.fullmatch(r"(?:arena_id|grp_id):\d+", key)
        or re.fullmatch(r"set:[^:\s]+:[^:\s]+", key)
    )


def _override_can_resolve(reports: Sequence[UnknownMechanicReport]) -> bool:
    return bool(reports) and all(
        report.mechanic is not None
        and "malformed" not in report.reason.casefold()
        and "unsafe" not in report.reason.casefold()
        and "missing" not in report.reason.casefold()
        and "unsupported card layout" not in report.reason.casefold()
        for report in reports
    )

def _unknown_sort_key(report: UnknownMechanicReport) -> tuple[str, str, str]:
    return (report.card_key, report.mechanic or "", report.reason)


def _override_set(value: OverrideSet | Mapping[str, Any] | None) -> OverrideSet:
    if value is None:
        return BUNDLED_REVIEWED_OVERRIDES
    if isinstance(value, OverrideSet):
        return value
    if isinstance(value, Mapping):
        # Ergonomic mapping form: {"key": {"add": [...], "remove": [...], "rationale": ...}}
        if "schema_version" in value:
            return OverrideSet.from_json(value)
        items = []
        for key, item in value.items():
            if not isinstance(item, Mapping):
                raise RoleSchemaError(f"Override {key!r} must be an object.")
            items.append(ReviewedOverride.from_json({"key": key, **item}))
        return OverrideSet(tuple(items))
    raise RoleSchemaError("Overrides must be an OverrideSet or JSON object.")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _json_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RoleSchemaError(f"{field_name} must be a JSON array of strings.")
    return _string_tuple(value, field_name)


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise RoleSchemaError(f"{field_name} must be an array of strings.")
    if not all(isinstance(item, str) for item in value):
        raise RoleSchemaError(f"{field_name} must be an array of strings.")
    return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_code(value: Any) -> str | None:
    result = _optional_str(value)
    return result.casefold() if result else None


def _required_str(value: Any, field_name: str) -> str:
    result = _optional_str(value)
    if result is None:
        raise RoleSchemaError(f"{field_name} must be a non-empty string.")
    return result


def _required_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoleSchemaError(f"{field_name} must be an integer.")
    return value


def _required_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoleSchemaError(f"{field_name} must be a number.")
    return float(value)


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def _required_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RoleSchemaError(f"{field_name} must be a boolean.")
    return value


def _object(value: Any, field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise RoleSchemaError(f"{field_name} must be an object.")


def _invalid(field_name: str) -> Any:
    raise RoleSchemaError(f"{field_name} is malformed.")


def _mapping_items(items: Iterable[Any], field_name: str) -> Iterable[Mapping[str, Any]]:
    for item in items:
        if isinstance(item, Mapping):
            yield item
        else:
            _invalid(f"{field_name} item")


_SUPPORTED_LAYOUTS = frozenset(
    {
        "normal",
        "split",
        "flip",
        "transform",
        "modal_dfc",
        "meld",
        "leveler",
        "saga",
        "class",
        "prototype",
        "adventure",
        "reversible_card",
        "augment",
        "host",
        "planar",
        "scheme",
        "vanguard",
        "phenomenon",
        "token",
        "double_faced_token",
        "emblem",
        "art_series",
        "battle",
        "case",
    }
)

# This intentionally uses a stable oracle identity, not a card-name condition.  The
# example is a harmless correction for a fixture-like synthetic identity; applications
# may extend this data with reviewed production identities without code changes.
BUNDLED_REVIEWED_OVERRIDES = OverrideSet(
    overrides=(
        ReviewedOverride(
            key="oracle_id:reviewed-example-220",
            add=(
                RoleAssignment(
                    role=Role.CARD_SELECTION,
                    confidence=0.95,
                    provenance=("reviewed_override",),
                    evidence=("bundled reviewed semantic correction",),
                ),
            ),
            rationale="Example data-only correction used to exercise the reviewed override layer.",
        ),
    )
)
DEFAULT_OVERRIDES = BUNDLED_REVIEWED_OVERRIDES


RoleResolver = RoleClassifier
RoleClassification = ClassificationResult
UnknownCardReport = UnknownMechanicReport


def load_role_overrides(path: str | Path) -> OverrideSet:
    """Load and strictly validate reviewed data overrides from JSON."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RoleSchemaError(f"Could not load role overrides {path}: {error}.") from error
    if not isinstance(value, Mapping):
        raise RoleSchemaError("Role overrides JSON must be an object.")
    return OverrideSet.from_json(value)
