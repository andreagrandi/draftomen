"""Boundary tests for derived draft-potential condition map records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from draftomen.semantic_capability_records import CapabilityQuantity, QuantityRelation
from draftomen.semantic_condition_records import (
    CONDITION_CAPABILITY_ID_PREFIX,
    CONDITION_MAP_SCHEMA_VERSION,
    ConditionCapability,
    ConditionEvidence,
    ConditionInteraction,
    ConditionMap,
    ConditionSource,
    condition_capability_id,
    condition_source_sha256,
    validate_condition_map_pins,
)
from draftomen.semantic_enrichment_records import CardSourcePin, SemanticEnrichmentError
from draftomen.semantic_relationship_records import QualificationKind

LAND_ORACLE_TEXT = "({T}: Add {G}.)"
ELF_ENTRY_TEXT = "When this creature enters, put a land card from your hand onto the battlefield tapped."
ELF_TRIGGER_TEXT = "Whenever a land you control enters, you gain 1 life."
ADDITIONAL_LAND_TEXT = "You may play an additional land on each of your turns."
DRAGON_TEXT = "Whenever this creature attacks, if you control a creature with power 4 or greater, draw a card."
TOKEN_TEXT = "When this creature enters, create a 3/3 green Beast creature token."
AMASS_TEXT = "Amass Orcs 4."
STORIED_TEXT = (
    "Storied (If you control three or more artifacts, legendaries, and/or Sagas, you have an enduring story "
    "for the rest of the game.)"
)
STORIED_TRIGGER_TEXT = f"{STORIED_TEXT}\nWhenever this creature attacks, draw a card."
TREASURE_TEXT = f"{STORIED_TEXT}\nWhen this creature enters, create up to three Treasure tokens."
LAND_ENTRY_SELECTOR = "put a land card from your hand onto the battlefield tapped"
LAND_TRIGGER_SELECTOR = "Whenever a land you control enters"
ADDITIONAL_LAND_SELECTOR = "You may play an additional land on each of your turns."
POWER_THRESHOLD_SELECTOR = "if you control a creature with power 4 or greater"
TOKEN_SELECTOR = "a 3/3 green Beast creature token"
AMASS_SELECTOR = "Amass Orcs 4."
STORIED_SELECTOR = "If you control three or more artifacts, legendaries, and/or Sagas"
TREASURE_SELECTOR = "create up to three Treasure tokens"

# kind -> (family, role, card_id, face_index, type_line, oracle_text, power, evidence field, evidence kind, selector)
_KINDS: Mapping[
    str,
    tuple[str, str, int, int | None, str | None, str | None, str | None, str, QualificationKind, str],
] = {
    "land_card": (
        "landfall",
        "enabler",
        201,
        None,
        "Land — Forest",
        LAND_ORACLE_TEXT,
        None,
        "type_line",
        QualificationKind.CONDITION,
        "Land",
    ),
    "land_entry": (
        "landfall",
        "enabler",
        202,
        0,
        "Creature — Elf Scout",
        ELF_ENTRY_TEXT,
        "1",
        "oracle_text",
        QualificationKind.CONDITION,
        LAND_ENTRY_SELECTOR,
    ),
    "additional_land_play": (
        "landfall",
        "enabler",
        203,
        None,
        "Creature — Elf Druid",
        ADDITIONAL_LAND_TEXT,
        "2",
        "oracle_text",
        QualificationKind.TIMING,
        ADDITIONAL_LAND_SELECTOR,
    ),
    "land_entry_event": (
        "landfall",
        "payoff",
        202,
        1,
        "Creature — Elf Warrior",
        ELF_TRIGGER_TEXT,
        "2",
        "oracle_text",
        QualificationKind.TIMING,
        LAND_TRIGGER_SELECTOR,
    ),
    "creature_power": (
        "ferocious",
        "enabler",
        301,
        None,
        "Creature — Dragon",
        DRAGON_TEXT,
        "4",
        "oracle_text",
        QualificationKind.CONDITION,
        POWER_THRESHOLD_SELECTOR,
    ),
    "created_creature_power": (
        "ferocious",
        "enabler",
        302,
        None,
        "Creature — Elf Shaman",
        TOKEN_TEXT,
        "2",
        "oracle_text",
        QualificationKind.CONDITION,
        TOKEN_SELECTOR,
    ),
    "amass_growth": (
        "ferocious",
        "enabler",
        303,
        None,
        "Sorcery",
        AMASS_TEXT,
        None,
        "oracle_text",
        QualificationKind.QUANTITY,
        AMASS_SELECTOR,
    ),
    "power_threshold": (
        "ferocious",
        "payoff",
        304,
        None,
        "Creature — Dragon",
        DRAGON_TEXT,
        "4",
        "oracle_text",
        QualificationKind.CONDITION,
        POWER_THRESHOLD_SELECTOR,
    ),
    "qualifying_permanent": (
        "storied",
        "enabler",
        401,
        None,
        "Legendary Artifact — Equipment",
        "Equipped creature gets +2/+2.",
        None,
        "type_line",
        QualificationKind.CONDITION,
        "Legendary Artifact",
    ),
    "created_qualifying_permanents": (
        "storied",
        "enabler",
        402,
        None,
        "Creature — Dwarf",
        TREASURE_TEXT,
        "1",
        "oracle_text",
        QualificationKind.CONDITION,
        TREASURE_SELECTOR,
    ),
    "storied_attainment": (
        "storied",
        "payoff",
        403,
        None,
        "Enchantment",
        STORIED_TRIGGER_TEXT,
        None,
        "oracle_text",
        QualificationKind.CONDITION,
        STORIED_SELECTOR,
    ),
}


def _source(declared: str, **overrides: Any) -> ConditionSource:
    """Build one synthetic source face for a declared kind with optional field overrides."""
    _, _, card_id, face_index, type_line, oracle_text, power = _KINDS[declared][:7]
    fields: dict[str, Any] = {
        "card_id": card_id,
        "face_index": face_index,
        "card_source_sha256": f"{card_id:064d}",
        "type_line": type_line,
        "oracle_text": oracle_text,
        "power": power,
    }
    fields.update(overrides)
    return ConditionSource.create(**fields)


def _evidence(declared: str, **overrides: Any) -> ConditionEvidence:
    """Build the declared evidence selector of one condition kind."""
    field, evidence_kind, selector = _KINDS[declared][7:10]
    values: dict[str, Any] = {
        "field": field,
        "kind": evidence_kind,
        "selector": selector,
        "occurrence": 0,
    }
    values.update(overrides)
    return ConditionEvidence(**values)


def _capability(
    declared: str,
    quantity: CapabilityQuantity | None = None,
    *,
    source: ConditionSource | None = None,
    **overrides: Any,
) -> ConditionCapability:
    """Build one capability for a declared kind over its own source face."""
    family, role = _KINDS[declared][:2]
    values: dict[str, Any] = {
        "source": _source(declared) if source is None else source,
        "family": family,
        "role": role,
        "kind": declared,
        "controller": "any",
        "quantity": quantity,
        "evidence": (_evidence(declared),),
    }
    values.update(overrides)
    return ConditionCapability.create(**values)


def _node(declared: str, quantity: CapabilityQuantity | None = None) -> tuple[ConditionSource, ConditionCapability]:
    """Build one synthetic source face together with its capability node."""
    source = _source(declared)
    return source, _capability(declared, quantity, source=source)


def _map_parts(
    *,
    sources: tuple[Any, ...],
    capabilities: tuple[Any, ...],
    interactions: tuple[Any, ...] = (),
) -> ConditionMap:
    """Build one condition map from explicit parts."""
    return ConditionMap(
        schema_version=CONDITION_MAP_SCHEMA_VERSION,
        scope="draft_potential",
        sources=sources,
        capabilities=capabilities,
        interactions=interactions,
    )


def _exactly(value: int) -> CapabilityQuantity:
    """Return an exactly stated quantity."""
    return CapabilityQuantity(value=value, relation=QuantityRelation.EXACTLY)


def _full_map() -> ConditionMap:
    """Build one map covering every declared family, role, and interaction support kind."""
    nodes = (
        _node("land_card", _exactly(1)),
        _node("land_entry", _exactly(1)),
        _node("additional_land_play", _exactly(1)),
        _node("land_entry_event"),
        _node("creature_power", _exactly(4)),
        _node("created_creature_power", _exactly(3)),
        _node("amass_growth", CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)),
        _node("power_threshold", CapabilityQuantity(value=4, relation=QuantityRelation.AT_LEAST)),
        _node("qualifying_permanent", _exactly(1)),
        _node("created_qualifying_permanents", CapabilityQuantity(value=3, relation=QuantityRelation.AT_MOST)),
        _node("storied_attainment", CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST)),
    )
    capabilities = {capability.kind: capability for _, capability in nodes}
    return _map_parts(
        sources=tuple(source for source, _ in nodes),
        capabilities=tuple(capability for _, capability in nodes),
        interactions=(
            ConditionInteraction(
                enabler_id=capabilities["land_entry"].capability_id,
                payoff_id=capabilities["land_entry_event"].capability_id,
                support="can_enable",
            ),
            ConditionInteraction(
                enabler_id=capabilities["creature_power"].capability_id,
                payoff_id=capabilities["power_threshold"].capability_id,
                support="can_enable",
            ),
            ConditionInteraction(
                enabler_id=capabilities["created_qualifying_permanents"].capability_id,
                payoff_id=capabilities["storied_attainment"].capability_id,
                support="contributes",
            ),
        ),
    )


def test_condition_map_round_trip_is_canonical_and_keeps_null_faces() -> None:
    condition_map = _full_map()

    encoded = condition_map.to_json()
    assert [item["card_id"] for item in encoded["sources"]] == [201, 202, 202, 203, 301, 302, 303, 304, 401, 402, 403]
    assert [item["face_index"] for item in encoded["sources"]] == [
        None,
        0,
        1,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ]
    assert [item["capability_id"] for item in encoded["capabilities"]] == sorted(
        item["capability_id"] for item in encoded["capabilities"]
    )
    assert [item["enabler_id"] for item in encoded["interactions"]] == sorted(
        item["enabler_id"] for item in encoded["interactions"]
    )
    assert sorted(item["support"] for item in encoded["interactions"]) == [
        "can_enable",
        "can_enable",
        "contributes",
    ]
    assert "-1" not in json.dumps(encoded)
    assert next(source.face_sort_key for source in condition_map.sources if source.card_id == 201) == -1
    assert next(source.face_sort_key for source in condition_map.sources if source.face_index == 1) == 1

    restored = ConditionMap.from_json(json.loads(json.dumps(encoded)))
    assert restored == condition_map
    assert restored.to_json() == encoded
    assert restored.sources[0].face_index is None
    assert restored.sources[1].face_index == 0


def test_condition_source_fingerprint_covers_every_copied_field() -> None:
    fixed = _source("creature_power", power="3")
    upgraded = _source("creature_power", power="4")

    assert fixed.source_sha256 != upgraded.source_sha256
    assert fixed.source_sha256 == condition_source_sha256(
        card_id=301,
        face_index=None,
        card_source_sha256=f"{301:064d}",
        type_line="Creature — Dragon",
        oracle_text=DRAGON_TEXT,
        power="3",
    )
    with pytest.raises(SemanticEnrichmentError, match="source_sha256"):
        replace(fixed, type_line="Creature — Wurm")
    with pytest.raises(SemanticEnrichmentError, match="source_sha256"):
        replace(fixed, source_sha256="0" * 64)


def test_condition_capability_identity_changes_with_its_source_and_decodes_strictly() -> None:
    weak_source = _source("creature_power", power="3")
    strong_source = _source("creature_power", power="4")
    weak = _capability("creature_power", _exactly(3), source=weak_source)
    strong = _capability("creature_power", _exactly(4), source=strong_source)

    assert weak.capability_id != strong.capability_id
    assert strong.capability_id == condition_capability_id(
        capability=strong,
        source_sha256=strong_source.source_sha256,
    )
    forged = f"{CONDITION_CAPABILITY_ID_PREFIX}{'a' * 64}"
    with pytest.raises(SemanticEnrichmentError, match="derived identity"):
        _map_parts(sources=(strong_source,), capabilities=(replace(strong, capability_id=forged),))
    with pytest.raises(SemanticEnrichmentError, match="capability_id"):
        _map_parts(
            sources=(strong_source,),
            capabilities=(replace(strong, capability_id="capability-attack-payoff"),),
        )
    with pytest.raises(SemanticEnrichmentError, match="capability_id"):
        _map_parts(sources=(strong_source,), capabilities=(replace(strong, capability_id=True),))


def test_condition_map_rejects_unpinned_and_wrong_face_sources() -> None:
    entry_source, entry = _node("land_entry", _exactly(1))
    _, payoff = _node("land_entry_event")
    other_source = _source("land_entry", card_id=205)
    other = _capability("land_entry", _exactly(1), source=other_source)

    with pytest.raises(SemanticEnrichmentError, match="pinned condition source"):
        _map_parts(sources=(entry_source,), capabilities=(other,))
    with pytest.raises(SemanticEnrichmentError, match="pinned condition source"):
        _map_parts(sources=(entry_source,), capabilities=(payoff,))
    with pytest.raises(SemanticEnrichmentError, match="sources must contain"):
        _map_parts(sources=(entry_source, "card-202"), capabilities=(entry,))
    with pytest.raises(SemanticEnrichmentError, match="capabilities must contain"):
        _map_parts(sources=(entry_source,), capabilities=(entry, "capability-1"))
    with pytest.raises(SemanticEnrichmentError, match="interactions must contain"):
        _map_parts(sources=(entry_source,), capabilities=(entry,), interactions=("interaction-1",))


def test_condition_evidence_must_resolve_inside_its_own_source_field() -> None:
    source, entry = _node("land_entry", _exactly(1))
    payoff_source, payoff = _node("land_entry_event")

    borrowed = _capability(
        "land_entry",
        _exactly(1),
        source=source,
        evidence=(_evidence("land_entry", selector=ELF_TRIGGER_TEXT),),
    )
    with pytest.raises(SemanticEnrichmentError, match="resolve exactly"):
        _map_parts(sources=(source, payoff_source), capabilities=(borrowed, payoff))

    misplaced = _capability(
        "land_entry",
        _exactly(1),
        source=source,
        evidence=(_evidence("land_entry", occurrence=1),),
    )
    with pytest.raises(SemanticEnrichmentError, match="resolve exactly"):
        _map_parts(sources=(source,), capabilities=(misplaced,))

    in_power = _capability(
        "land_entry",
        _exactly(1),
        source=source,
        evidence=(_evidence("land_entry", field="power"),),
    )
    with pytest.raises(SemanticEnrichmentError, match="resolve exactly"):
        _map_parts(sources=(source,), capabilities=(in_power,))

    unpowered = _source("creature_power", power=None)
    unpowered_capability = _capability(
        "creature_power",
        _exactly(4),
        source=unpowered,
        evidence=(_evidence("creature_power", field="power", selector="4"),),
    )
    with pytest.raises(SemanticEnrichmentError, match="resolve exactly"):
        _map_parts(sources=(unpowered,), capabilities=(unpowered_capability,))

    silent = _source("land_entry_event", oracle_text=None)
    with pytest.raises(SemanticEnrichmentError, match="resolve exactly"):
        _map_parts(sources=(silent,), capabilities=(_capability("land_entry_event", source=silent),))


def test_condition_evidence_rejects_blank_selectors_and_invalid_occurrences() -> None:
    with pytest.raises(SemanticEnrichmentError, match="selector"):
        _evidence("land_card", selector="   ")
    with pytest.raises(SemanticEnrichmentError, match="occurrence"):
        _evidence("land_card", occurrence=-1)
    with pytest.raises(SemanticEnrichmentError, match="occurrence"):
        _evidence("land_card", occurrence=True)
    with pytest.raises(SemanticEnrichmentError, match="field"):
        _evidence("land_card", field="name")
    with pytest.raises(SemanticEnrichmentError, match="kind"):
        _evidence("land_card", kind="strategy")


def test_condition_source_rejects_invalid_card_and_field_values() -> None:
    with pytest.raises(SemanticEnrichmentError, match="card_id"):
        _source("land_card", card_id=0)
    with pytest.raises(SemanticEnrichmentError, match="face_index"):
        _source("land_card", face_index=-1)
    with pytest.raises(SemanticEnrichmentError, match="power"):
        _source("creature_power", power="  ")
    with pytest.raises(SemanticEnrichmentError, match="card_source_sha256"):
        _source("land_card", card_source_sha256="not-a-digest")
    with pytest.raises(SemanticEnrichmentError, match="type_line"):
        _source("land_card", type_line=7)


@pytest.mark.parametrize(
    ("family", "role", "kind"),
    (
        ("landfall", "enabler", "land_entry_event"),
        ("landfall", "payoff", "land_card"),
        ("ferocious", "enabler", "power_threshold"),
        ("ferocious", "payoff", "created_creature_power"),
        ("storied", "enabler", "storied_attainment"),
        ("storied", "payoff", "qualifying_permanent"),
        ("landfall", "payoff", "creature_power"),
    ),
)
def test_condition_capability_rejects_unknown_family_role_kind_combinations(
    family: str,
    role: str,
    kind: str,
) -> None:
    with pytest.raises(SemanticEnrichmentError, match="declared condition capability kind"):
        _capability(
            "creature_power",
            _exactly(4),
            family=family,
            role=role,
            kind=kind,
        )


def test_condition_capability_rejects_open_family_role_controller_and_empty_evidence() -> None:
    _, capability = _node("creature_power", _exactly(4))

    with pytest.raises(SemanticEnrichmentError, match="family"):
        replace(capability, family="battalion")
    with pytest.raises(SemanticEnrichmentError, match="role"):
        replace(capability, role="helper")
    with pytest.raises(SemanticEnrichmentError, match="controller"):
        replace(capability, controller="owner")
    with pytest.raises(SemanticEnrichmentError, match="kind"):
        replace(capability, kind="  ")
    with pytest.raises(SemanticEnrichmentError, match="evidence"):
        replace(capability, evidence=())
    with pytest.raises(SemanticEnrichmentError, match="evidence must be a tuple"):
        replace(capability, evidence=[_evidence("creature_power")])


@pytest.mark.parametrize(
    ("kind", "quantity"),
    (
        ("land_card", _exactly(1)),
        ("land_entry", None),
        ("land_entry", CapabilityQuantity(value=2, relation=QuantityRelation.AT_MOST)),
        ("additional_land_play", _exactly(1)),
        ("additional_land_play", CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)),
        ("land_entry_event", None),
        ("creature_power", _exactly(4)),
        ("created_creature_power", _exactly(6)),
        ("amass_growth", _exactly(4)),
        ("amass_growth", CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)),
        ("power_threshold", CapabilityQuantity(value=4, relation=QuantityRelation.AT_LEAST)),
        ("qualifying_permanent", _exactly(1)),
        ("created_qualifying_permanents", _exactly(3)),
        ("created_qualifying_permanents", CapabilityQuantity(value=2, relation=QuantityRelation.AT_MOST)),
        ("created_qualifying_permanents", CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)),
        ("storied_attainment", CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST)),
    ),
)
def test_condition_capability_accepts_its_declared_quantities(kind: str, quantity: CapabilityQuantity | None) -> None:
    _, capability = _node(kind, quantity)

    assert capability.quantity == quantity


@pytest.mark.parametrize(
    ("kind", "quantity"),
    (
        ("land_card", CapabilityQuantity(value=2, relation=QuantityRelation.EXACTLY)),
        ("land_card", CapabilityQuantity(value=1, relation=QuantityRelation.AT_LEAST)),
        ("land_entry", CapabilityQuantity(value=1, relation=QuantityRelation.AT_LEAST)),
        ("additional_land_play", None),
        ("land_entry_event", _exactly(1)),
        ("creature_power", None),
        ("creature_power", CapabilityQuantity(value=4, relation=QuantityRelation.AT_MOST)),
        ("creature_power", CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)),
        ("created_creature_power", CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST)),
        ("amass_growth", None),
        ("amass_growth", CapabilityQuantity(value=4, relation=QuantityRelation.AT_MOST)),
        ("power_threshold", None),
        ("power_threshold", _exactly(4)),
        ("qualifying_permanent", CapabilityQuantity(value=2, relation=QuantityRelation.EXACTLY)),
        ("created_qualifying_permanents", CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST)),
        ("storied_attainment", CapabilityQuantity(value=2, relation=QuantityRelation.AT_LEAST)),
        ("storied_attainment", _exactly(3)),
    ),
)
def test_condition_capability_rejects_undeclared_quantities(kind: str, quantity: CapabilityQuantity | None) -> None:
    with pytest.raises(SemanticEnrichmentError, match="quantity"):
        _node(kind, quantity)


def test_condition_quantities_cannot_express_zero_power_or_growth() -> None:
    with pytest.raises(SemanticEnrichmentError, match="positive integer"):
        CapabilityQuantity(value=0, relation=QuantityRelation.EXACTLY)
    with pytest.raises(SemanticEnrichmentError, match="positive integer"):
        CapabilityQuantity(value=-1, relation=QuantityRelation.EXACTLY)
    with pytest.raises(SemanticEnrichmentError, match="variable quantities"):
        CapabilityQuantity(value=4, relation=QuantityRelation.VARIABLE)


def test_condition_map_rejects_duplicate_source_capability_and_edge_identities() -> None:
    entry_source, entry = _node("land_entry", _exactly(1))
    payoff_source, payoff = _node("land_entry_event")
    edge = ConditionInteraction(
        enabler_id=entry.capability_id,
        payoff_id=payoff.capability_id,
        support="can_enable",
    )

    with pytest.raises(SemanticEnrichmentError, match="duplicate"):
        _map_parts(
            sources=(entry_source, payoff_source, entry_source),
            capabilities=(entry, payoff, entry),
        )
    with pytest.raises(SemanticEnrichmentError, match="duplicate"):
        _map_parts(
            sources=(entry_source, payoff_source),
            capabilities=(entry, payoff, payoff),
        )
    with pytest.raises(SemanticEnrichmentError, match="duplicate"):
        _map_parts(
            sources=(entry_source, payoff_source),
            capabilities=(entry, payoff),
            interactions=(edge, edge),
        )


def test_condition_map_rejects_unresolved_wrong_role_and_cross_family_edges() -> None:
    entry_source, entry = _node("land_entry", _exactly(1))
    payoff_source, payoff = _node("land_entry_event")
    ferocious_source, ferocious = _node("creature_power", _exactly(4))
    sources = (entry_source, payoff_source, ferocious_source)
    capabilities = (entry, payoff, ferocious)

    with pytest.raises(SemanticEnrichmentError, match="stored capabilities"):
        _map_parts(
            sources=sources,
            capabilities=capabilities,
            interactions=(
                ConditionInteraction(
                    enabler_id=f"{CONDITION_CAPABILITY_ID_PREFIX}{'b' * 64}",
                    payoff_id=payoff.capability_id,
                    support="can_enable",
                ),
            ),
        )
    with pytest.raises(SemanticEnrichmentError, match="enabler to a payoff"):
        _map_parts(
            sources=sources,
            capabilities=capabilities,
            interactions=(
                ConditionInteraction(
                    enabler_id=payoff.capability_id,
                    payoff_id=entry.capability_id,
                    support="contributes",
                ),
            ),
        )
    with pytest.raises(SemanticEnrichmentError, match="enabler to a payoff"):
        _map_parts(
            sources=(entry_source,),
            capabilities=(entry,),
            interactions=(
                ConditionInteraction(
                    enabler_id=entry.capability_id,
                    payoff_id=entry.capability_id,
                    support="contributes",
                ),
            ),
        )
    with pytest.raises(SemanticEnrichmentError, match="one family"):
        _map_parts(
            sources=sources,
            capabilities=capabilities,
            interactions=(
                ConditionInteraction(
                    enabler_id=ferocious.capability_id,
                    payoff_id=payoff.capability_id,
                    support="can_enable",
                ),
            ),
        )
    with pytest.raises(SemanticEnrichmentError, match="support"):
        _map_parts(
            sources=sources,
            capabilities=capabilities,
            interactions=(
                ConditionInteraction(
                    enabler_id=entry.capability_id,
                    payoff_id=payoff.capability_id,
                    support="enables",
                ),
            ),
        )


def test_condition_map_rejects_unsupported_schema_scope_and_unknown_keys() -> None:
    _, entry = _node("land_entry", _exactly(1))
    condition_map = _map_parts(sources=(_source("land_entry"),), capabilities=(entry,))
    encoded = condition_map.to_json()

    with pytest.raises(SemanticEnrichmentError, match="Unsupported condition map schema"):
        replace(condition_map, schema_version=2)
    with pytest.raises(SemanticEnrichmentError, match="Unsupported condition map schema"):
        replace(condition_map, schema_version=True)
    with pytest.raises(SemanticEnrichmentError, match="scope"):
        replace(condition_map, scope="battlefield")
    with pytest.raises(SemanticEnrichmentError, match="must not be empty"):
        replace(condition_map, capabilities=())
    with pytest.raises(SemanticEnrichmentError, match="unknown fields"):
        ConditionMap.from_json({**encoded, "future": []})
    with pytest.raises(SemanticEnrichmentError, match="missing fields"):
        ConditionMap.from_json({key: value for key, value in encoded.items() if key != "interactions"})
    with pytest.raises(SemanticEnrichmentError, match="invalid keys"):
        ConditionMap.from_json({**encoded, "capabilities": [{}]})
    with pytest.raises(SemanticEnrichmentError, match="JSON array"):
        ConditionMap.from_json({**encoded, "sources": {}})
    with pytest.raises(SemanticEnrichmentError, match="must be an object"):
        ConditionMap.from_json([])


def test_condition_map_pins_its_sources_to_enhancement_card_pins() -> None:
    condition_map = _full_map()

    pins = {
        source.card_id: CardSourcePin(
            card_id=source.card_id,
            oracle_id=None,
            collector_number=None,
            sha256=source.card_source_sha256,
        )
        for source in condition_map.sources
    }
    validate_condition_map_pins(condition_map=condition_map, pins=pins)

    with pytest.raises(SemanticEnrichmentError, match="pinned card sources"):
        validate_condition_map_pins(
            condition_map=condition_map,
            pins={card_id: pin for card_id, pin in pins.items() if card_id != 202},
        )
    with pytest.raises(SemanticEnrichmentError, match="card source pin"):
        validate_condition_map_pins(
            condition_map=condition_map,
            pins={**pins, 201: replace(pins[201], sha256="0" * 64)},
        )


def test_condition_finding_references_are_normalized_and_unique() -> None:
    _, entry = _node("land_entry", _exactly(1))
    referenced = _capability("land_entry", _exactly(1), source_finding_ids=(" z-finding ", "a-finding"))

    assert referenced.source_finding_ids == ("a-finding", "z-finding")
    assert entry.source_finding_ids == ()
    with pytest.raises(SemanticEnrichmentError, match="duplicate"):
        replace(entry, source_finding_ids=("finding-elf-entry", "finding-elf-entry"))
    with pytest.raises(SemanticEnrichmentError, match="source_finding_id"):
        replace(entry, source_finding_ids=("  ",))
    with pytest.raises(SemanticEnrichmentError, match="source_finding_ids must be a tuple"):
        replace(entry, source_finding_ids=["finding-elf-entry"])
