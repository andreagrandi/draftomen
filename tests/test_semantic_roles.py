from __future__ import annotations

import json
from pathlib import Path
import string

import pytest

import draftomen.semantic_roles as semantic_roles
from draftomen.carddb import CardFace, CardInfo
from draftomen.semantic_roles import (
    CLASSIFIER_VERSION,
    ROLE_DEFINITIONS,
    ROLE_SCHEMA_VERSION,
    CompiledRoleProfile,
    OverrideSet,
    ProfileCard,
    ReviewedOverride,
    Role,
    RoleAssignment,
    RoleClassifier,
    RoleProfileError,
    RoleSchemaError,
    ThresholdParameters,
    TypalIdentity,
    classify_card,
    compile_role_profile,
    dump_role_profile,
    rebuild_role_profile,
    role_definition,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "semantic-roles.json"
# The reviewed generic rules vocabulary: a definition may use only these words, so no set,
# card, archetype or guide name can enter the shared vocabulary by rewording.  Every entry
# is a rules term, and adding one is a deliberate decision that an entity name never
# qualifies for; a failure here means the new word needs that decision.
_GENERIC_DEFINITION_WORDS = frozenset(
    {
        "a", "abilities", "ability", "according", "additional", "again", "alone", "among", "an",
        "and", "any", "applies", "around", "artifact", "artifacts", "as", "attack", "attacking",
        "basic", "battle", "battlefield", "be", "blocking", "build", "can", "card", "cards",
        "carries", "carrying", "cast", "casting", "causes", "changes", "chosen", "clues", "color",
        "combat", "condition", "contents", "control", "controlling", "converts", "cost", "count",
        "counters", "counters-theme", "creates", "creature", "creature's", "creatures", "damage",
        "deals", "death", "declared", "dependent", "destroys", "die", "dies", "discard",
        "discarding", "discards", "do", "domain", "drawing", "draws", "dying", "each", "effect",
        "enables", "enchantment", "enchantments", "end", "enters", "entry", "equip", "equipment",
        "equipped", "evasion", "excess", "exile", "exiles", "extra", "fills", "filters", "five",
        "flying", "food", "for", "four", "from", "gains", "graveyard", "group", "hand", "in",
        "instead", "instruction", "into", "is", "it", "its", "itself", "keeps", "keywords", "land",
        "lands", "less", "lets", "library", "life", "lifelink", "loss", "maker", "makes", "mana",
        "many", "meets", "membership", "menace", "metadata", "milling", "modified", "more",
        "net-positive", "next", "nonland", "not", "number", "of", "off", "on", "one", "only",
        "onto", "opposing", "optional", "or", "orders", "other", "owner's", "package", "payment",
        "pays", "per-creature", "permanent", "permanents", "places", "planeswalker", "play",
        "playing", "power", "produced-mana", "produces", "provides", "puts", "putting", "qualify",
        "records", "recursion", "references", "refers", "removal", "replacing", "request",
        "required", "resource", "return", "returns", "rewards", "role", "sacrificed",
        "sacrifices", "satisfies", "scales", "searches", "second", "selection", "shadow",
        "sharing", "so", "spell", "spells", "stack", "state", "stated", "static", "strips",
        "subtype", "such", "supplies", "supports", "taps", "target", "targeted", "temporarily",
        "text", "than", "that", "the", "them", "then", "this", "threshold", "through", "to",
        "token", "tokens", "top", "toughness", "treasure", "treasures", "trigger", "triggers",
        "turn", "turns", "two", "typal", "type", "types", "unblockable", "untapping", "until",
        "used", "value", "variable", "when", "whenever", "whether", "whose", "with", "you", "your",
    }
)


def _fixtures() -> dict[str, dict[str, object]]:
    rows = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return {row["oracle_id"]: row for row in rows}


def _roles(result) -> set[Role]:
    return {assignment.role for assignment in result.assignments}


def test_role_definitions_cover_every_vocabulary_member() -> None:
    assert set(ROLE_DEFINITIONS) == set(Role)

    for role, definition in ROLE_DEFINITIONS.items():
        assert type(definition) is str
        assert definition == definition.strip()
        assert definition.strip()
        assert len(definition) <= 200
        assert definition.endswith(".")
        assert definition.isascii()
        assert ". " not in definition
        assert not any(word[:1].isupper() for word in definition.split()[1:]), role

    assert len(set(ROLE_DEFINITIONS.values())) == len(ROLE_DEFINITIONS)


def test_role_definitions_stay_set_independent() -> None:
    words = {
        word.strip(string.punctuation)
        for definition in ROLE_DEFINITIONS.values()
        for word in definition.casefold().split()
    }

    assert words <= _GENERIC_DEFINITION_WORDS, sorted(words - _GENERIC_DEFINITION_WORDS)


def test_role_definition_lookup_returns_the_pinned_text_and_rejects_bad_input() -> None:
    assert role_definition(Role.SACRIFICE_FODDER) == ROLE_DEFINITIONS[Role.SACRIFICE_FODDER]
    assert role_definition(Role.POWER_N_ENABLER) == role_definition(Role.POWER_THRESHOLD_ENABLER)

    with pytest.raises(RoleSchemaError):
        role_definition("draw")  # type: ignore[arg-type]


def test_missing_role_definition_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(semantic_roles._ROLE_DEFINITIONS, Role.DRAW)

    with pytest.raises(RoleSchemaError):
        role_definition(Role.DRAW)


def test_representative_role_families_and_typed_parameters() -> None:
    rows = _fixtures()
    assert Role.DRAW_SECOND_PAYOFF in _roles(classify_card(rows["role-draw-payoff"]))
    assert Role.EXTRA_DRAW_ENABLER in _roles(classify_card(rows["role-draw-enabler"]))

    destroy = classify_card(rows["role-destroy"])
    assert Role.HARD_REMOVAL in _roles(destroy)
    hard = next(assignment for assignment in destroy.assignments if assignment.role is Role.HARD_REMOVAL)
    assert hard.removal is not None
    assert hard.removal.effective_score == 0.92
    assert Role.CONDITIONAL_REMOVAL in _roles(destroy)
    assert Role.DAMAGE_REMOVAL in _roles(classify_card(rows["role-damage"]))
    assert Role.DISABLING_REMOVAL in _roles(classify_card(rows["role-disable"]))
    temporary = classify_card(rows["role-temporary-tap"])
    assert Role.TEMPORARY_TAP in _roles(temporary)
    assert any(assignment.removal and assignment.removal.temporary for assignment in temporary.assignments)
    assert Role.BOUNCE in _roles(classify_card(rows["role-bounce"]))
    assert Role.COUNTERSPELL in _roles(classify_card(rows["role-counter"]))
    assert Role.COMBAT_TRICK in _roles(classify_card(rows["role-trick"]))

    creature = classify_card(rows["role-creature"])
    assert {Role.LOW_COST_CREATURE, Role.LARGE_CREATURE, Role.EVASIVE_THREAT, Role.TYPAL_MEMBER} <= _roles(creature)
    assert creature.assignments[-1].typal_identity is not None
    assert Role.TOKEN_MAKER in _roles(classify_card(rows["role-token-wide"]))
    assert Role.SACRIFICE_OUTLET in _roles(classify_card(rows["role-sacrifice"]))
    assert {Role.DIES_TRIGGER, Role.DEATH_PAYOFF} <= _roles(classify_card(rows["role-dies"]))

    graveyard = classify_card(rows["role-graveyard"])
    assert {Role.SELF_MILL, Role.RECURSION, Role.GRAVEYARD_FILLER} <= _roles(graveyard)
    assert Role.ARTIFACT_PAYOFF in _roles(classify_card(rows["role-artifact"]))
    assert {Role.EQUIPMENT, Role.EQUIPMENT_PAYOFF} <= _roles(classify_card(rows["role-equipment"]))

    land = classify_card(rows["role-land"])
    assert {Role.MANA_PRODUCER, Role.FIXING} <= _roles(land)
    assert any(assignment.produced_resources is not None for assignment in land.assignments)
    assert Role.RAMP in _roles(classify_card(rows["role-ramp"]))
    assert {Role.LANDFALL_PAYOFF, Role.COUNTERS, Role.COUNTERS_THEME} <= _roles(classify_card(rows["role-landfall"]))

    power = classify_card(rows["role-power-threshold"])
    assert Role.POWER_THRESHOLD_PAYOFF in _roles(power)
    assert any(assignment.threshold and assignment.threshold.value == 4 for assignment in power.assignments)
    permanent = classify_card(rows["role-permanent-threshold"])
    assert Role.PERMANENT_TYPE_THRESHOLD in _roles(permanent)
    assert permanent.assignments[-1].threshold is not None
    assert permanent.assignments[-1].threshold.permanent_type == "artifacts"


def test_multiface_roles_are_union_and_order_is_stable() -> None:
    row = _fixtures()["role-mdfc"]
    first = classify_card(row)
    second = classify_card(dict(reversed(tuple(row.items()))))
    assert first.to_bytes() == second.to_bytes()
    assert Role.DRAW in _roles(first)
    assert Role.MANA_PRODUCER in _roles(first)
    assert first.provenance.source == "local_classifier"
    assert first.classifier_version == CLASSIFIER_VERSION
    assert first.role_schema_version == ROLE_SCHEMA_VERSION


def test_reviewed_override_is_data_only_and_deterministic() -> None:
    row = _fixtures()["reviewed-example-220"]
    result = classify_card(row)
    assert _roles(result) == {Role.CARD_SELECTION}
    assert "reviewed_override:oracle_id:reviewed-example-220" in result.diagnostics
    assert result.provenance.source == "local_classifier_with_reviewed_override"

    override = OverrideSet.from_json(
        {
            "schema_version": 1,
            "overrides": [
                {
                    "key": "oracle_id:custom",
                    "add": [{"role": "mana_sink", "confidence": 0.9}],
                    "remove": [],
                    "rationale": "Reviewed exceptional mana sink.",
                }
            ],
        }
    )
    custom = classify_card({**row, "oracle_id": "custom"}, overrides=override)
    assert _roles(custom) == {Role.MANA_SINK}


def test_unknown_and_unsafe_cards_are_conservative_and_actionable() -> None:
    rows = _fixtures()
    unknown = classify_card(rows["role-unknown-mechanic"])
    assert unknown.assignments == ()
    assert unknown.unknown_reports[0].mechanic == "novel mechanic xyz"
    assert "add a reusable primitive" in unknown.unknown_reports[0].reason
    prose = classify_card({"name": "ordinary prose", "oracle_text": "The archivist smiles at the sunset."})
    assert prose.unknown_reports == ()
    assert prose.assignments == ()

    unsafe = classify_card(rows["role-unsafe"])
    assert unsafe.assignments == ()
    assert any("source_disagreement" in report.reason for report in unsafe.unknown_reports)
    assert "draw a card" not in "ordinary prose"


def test_profile_authority_and_wholly_local_fallback() -> None:
    rows = _fixtures()
    card = rows["role-draw-payoff"]
    local = classify_card(card)
    profile = compile_role_profile(set_code="tst", results=[local])
    authoritative = RoleClassifier().resolve(card, profile=profile)
    assert authoritative.source == "compiled_profile"
    assert authoritative.status == "authoritative_exact_set_profile"
    assert authoritative.assignments == profile.cards[0].assignments
    assert "compiled_profile" in authoritative.assignments[0].provenance

    wrong_set = RoleClassifier().resolve({**card, "set": "other"}, profile=profile)
    assert wrong_set.source == "local_classifier"
    assert wrong_set.diagnostics == ("profile_wrong_set:used_local_classifier",)
    incompatible = CompiledRoleProfile(
        set_code="tst",
        cards=profile.cards,
        classifier_version="0.0",
    )
    fallback = RoleClassifier().resolve(card, profile=incompatible)
    assert fallback.source == "local_classifier"
    assert fallback.diagnostics == ("profile_incompatible_versions:used_local_classifier",)
    assert fallback.assignments == local.assignments


def test_profile_serialization_rejects_unsupported_or_malformed_data() -> None:
    result = classify_card(_fixtures()["role-draw-payoff"])
    profile = compile_role_profile(set_code="tst", results=[result])
    assert profile.to_bytes() == CompiledRoleProfile.from_json(profile.to_json()).to_bytes()
    with pytest.raises(RoleProfileError, match="Unsupported profile schema"):
        CompiledRoleProfile.from_json({**profile.to_json(), "schema_version": 99})
    with pytest.raises(RoleSchemaError, match="Unsupported semantic role"):
        RoleAssignment.from_json({"role": "not-a-role"})
    with pytest.raises(RoleSchemaError, match="Threshold value"):
        ThresholdParameters.from_json({"kind": "threshold", "value": -1})
    with pytest.raises(RoleSchemaError, match="at least one subtype"):
        TypalIdentity(())


def test_profile_does_not_merge_incompatible_sources() -> None:
    rows = _fixtures()
    local = classify_card(rows["role-draw-payoff"])
    profile = CompiledRoleProfile(
        set_code="tst",
        cards=(ProfileCard(key=local.card_key, assignments=(RoleAssignment(Role.RAMP),)),),
        role_schema_version=999,
    )
    resolved = RoleClassifier().resolve(rows["role-draw-payoff"], profile=profile)
    assert resolved.source == "local_classifier"
    assert Role.RAMP not in _roles(resolved.classification)
    assert Role.DRAW_SECOND_PAYOFF in _roles(resolved.classification)


def test_removal_parameters_round_trip_through_assignment_and_profile() -> None:
    row = _fixtures()["role-destroy"]
    result = classify_card(row)
    hard = next(assignment for assignment in result.assignments if assignment.role is Role.HARD_REMOVAL)
    assert hard.to_json()["parameters"]["kind"] == "removal"
    assert hard.to_json()["parameters"]["removal_kind"] == "destroy"
    assert RoleAssignment.from_json(hard.to_json()) == hard
    profile = compile_role_profile(set_code="tst", results=[result])
    assert CompiledRoleProfile.from_json(profile.to_json()).to_bytes() == profile.to_bytes()


def test_canonical_keywords_drive_known_roles_and_unknown_reports() -> None:
    rows = _fixtures()
    flyer = CardInfo(
        grp_id=901,
        name="Canonical Flyer",
        colors=(),
        mana_value=2,
        rarity="common",
        types=("Creature",),
        keywords=("Flying",),
        type_line="Creature — Bird",
        subtypes=("Bird",),
        set_code="tst",
        collector_number="901",
        arena_id=901,
        source_provenance=("scryfall",),
    )
    assert Role.EVASIVE_THREAT in _roles(classify_card(flyer))
    face = CardFace(
        name="Face Flyer",
        keywords=("Flying",),
        type_line="Creature — Bird",
        subtypes=("Bird",),
        mana_value=2,
    )
    assert Role.EVASIVE_THREAT in _roles(classify_card(face))
    multi = {
        "oracle_id": "keyword-multiface",
        "name": "Keyword Faces",
        "set": "tst",
        "layout": "modal_dfc",
        "type_line": "Instant",
        "types": [],
        "faces": [
            {
                "name": "Creature Face",
                "keywords": ["Flying"],
                "type_line": "Creature — Bird",
                "types": ["Creature"],
            },
            {
                "name": "Spell Face",
                "keywords": [],
                "type_line": "Sorcery",
                "types": [],
            },
        ],
    }
    assert Role.EVASIVE_THREAT in _roles(classify_card(multi))
    unknown = classify_card(rows["role-keyword-unknown"])
    assert unknown.assignments == ()
    assert unknown.unknown_reports[0].mechanic == "Futurecraft"


def test_removal_negative_targets_are_not_removal() -> None:
    rows = _fixtures()
    assert Role.HARD_REMOVAL not in _roles(classify_card(rows["role-hard-graveyard"]))
    assert Role.HARD_REMOVAL not in _roles(classify_card(rows["role-hard-own"]))
    assert Role.DAMAGE_REMOVAL not in _roles(classify_card(rows["role-burn-player"]))


def test_token_ordering_fixings_and_ramp_boundaries() -> None:
    rows = _fixtures()
    token_roles = _roles(classify_card(rows["role-token-treasure"]))
    assert Role.TOKEN_MAKER in token_roles
    assert Role.GO_WIDE_ENABLER not in token_roles
    loot_roles = _roles(classify_card(rows["role-loot"]))
    rummage_roles = _roles(classify_card(rows["role-rummage"]))
    assert Role.LOOT in loot_roles and Role.RUMMAGE not in loot_roles
    assert Role.RUMMAGE in rummage_roles and Role.LOOT not in rummage_roles
    rock_roles = _roles(classify_card(rows["role-rock-fixing"]))
    assert {Role.MANA_PRODUCER, Role.FIXING} <= rock_roles
    assert Role.RAMP not in _roles(classify_card(rows["role-ordinary-land"]))
    assert Role.RAMP not in _roles(classify_card(rows["role-land"]))


def test_typed_thresholds_and_textual_power_values() -> None:
    rows = _fixtures()
    second = classify_card(rows["role-spell-second"])
    second_threshold = next(
        assignment.threshold
        for assignment in second.assignments
        if assignment.role is Role.SPELL_COUNT_THRESHOLD
    )
    assert second_threshold == ThresholdParameters(2, relation="exactly")
    three = classify_card(rows["role-spell-three"])
    three_threshold = next(
        assignment.threshold
        for assignment in three.assignments
        if assignment.role is Role.SPELL_COUNT_THRESHOLD
    )
    assert three_threshold == ThresholdParameters(3)
    conditional = classify_card(rows["role-destroy"])
    assert Role.POWER_THRESHOLD_ENABLER not in _roles(conditional)
    power = classify_card(rows["role-power-threshold"])
    assert next(
        assignment.threshold
        for assignment in power.assignments
        if assignment.role is Role.POWER_THRESHOLD_PAYOFF
    ) == ThresholdParameters(4)
    at_most = classify_card(
        {
            "oracle_id": "power-at-most",
            "name": "Small Matters",
            "set": "tst",
            "type_line": "Creature",
            "types": ["Creature"],
            "power": "2",
            "oracle_text": "If you control a creature with power 2 or less, draw a card.",
        }
    )
    assert next(
        assignment.threshold
        for assignment in at_most.assignments
        if assignment.role is Role.POWER_THRESHOLD_PAYOFF
    ) == ThresholdParameters(2, relation="at_most")
    variable = classify_card(
        {
            "oracle_id": "variable-power",
            "name": "Variable Body",
            "set": "tst",
            "type_line": "Creature",
            "types": ["Creature"],
            "power": "*",
            "oracle_text": "",
        }
    )
    assert Role.POWER_THRESHOLD_ENABLER not in _roles(variable)


def test_unknown_profiles_fall_back_and_reviews_can_resolve_mechanics() -> None:
    rows = _fixtures()
    unknown = classify_card(rows["role-unknown-mechanic"])
    profile = compile_role_profile(set_code="tst", results=[unknown])
    assert profile.cards == ()
    resolved = RoleClassifier().resolve(rows["role-unknown-mechanic"], profile=profile)
    assert resolved.source == "local_classifier"
    assert resolved.classification.unknown_reports
    override = OverrideSet(
        (
            ReviewedOverride(
                key="oracle_id:role-keyword-unknown",
                add=(RoleAssignment(Role.CARD_SELECTION, confidence=0.95),),
                rationale="Reviewed mapping for a supported exceptional mechanic.",
            ),
        )
    )
    reviewed = classify_card(rows["role-keyword-unknown"], overrides=override)
    assert _roles(reviewed) == {Role.CARD_SELECTION}
    assert not reviewed.unknown_reports
    unsafe_override = OverrideSet(
        (
            ReviewedOverride(
                key="oracle_id:role-unsafe",
                add=(RoleAssignment(Role.CARD_SELECTION),),
                rationale="Must not override unsafe source metadata.",
            ),
        )
    )
    unsafe = classify_card(rows["role-unsafe"], overrides=unsafe_override)
    assert unsafe.assignments == ()
    assert unsafe.unknown_reports


def test_parameter_roles_and_override_keys_are_strict() -> None:
    with pytest.raises(RoleSchemaError, match="not valid"):
        RoleAssignment(Role.RAMP, parameters=ThresholdParameters(2))
    with pytest.raises(RoleSchemaError, match="requires typed"):
        RoleAssignment(Role.MANA_PRODUCER)
    with pytest.raises(RoleSchemaError, match="display names"):
        ReviewedOverride(
            key="name:unstable",
            add=(RoleAssignment(Role.DRAW),),
            rationale="Not a stable identity.",
        )


def test_disabling_go_wide_sacrifice_recursion_and_typal_negatives() -> None:
    rows = _fixtures()
    assert Role.DISABLING_REMOVAL not in _roles(classify_card(rows["role-disable-self"]))
    assert Role.DISABLING_REMOVAL not in _roles(classify_card(rows["role-disable-own"]))
    assert Role.GO_WIDE_PAYOFF not in _roles(classify_card(rows["role-go-wide-dies"]))
    assert Role.GO_WIDE_PAYOFF not in _roles(classify_card(rows["role-go-wide-anthem"]))
    sacrifice = _roles(classify_card(rows["role-sacrifice-cost"]))
    assert Role.SACRIFICE_OUTLET in sacrifice
    assert Role.SACRIFICE_FODDER not in sacrifice
    recursion = _roles(classify_card(rows["role-recursion-only"]))
    assert recursion == {Role.RECURSION}
    assert Role.TYPAL_PAYOFF not in _roles(classify_card(rows["role-typal-generic"]))


def test_arbitrary_card_draws_and_actual_provenance_evidence() -> None:
    rows = _fixtures()
    assert _roles(classify_card(rows["role-draw-four"])) == {Role.DRAW}
    assert _roles(classify_card(rows["role-draw-x"])) == {Role.DRAW}
    evidence = classify_card(rows["role-creature"]).provenance.input_fields
    assert {"mana_value", "power", "type_line"} <= set(evidence)


def test_legacy_unknown_cardinfo_and_malformed_canonical_shapes_are_unknown() -> None:
    legacy = CardInfo.from_json(
        {
            "grp_id": 902,
            "name": "Legacy",
            "colors": [],
            "mana_value": 2,
            "rarity": "common",
            "types": ["Creature"],
            "source_provenance": ["unknown"],
        }
    )
    legacy_result = classify_card(legacy)
    assert legacy_result.assignments == ()
    assert legacy_result.unknown_reports
    malformed_resource = classify_card(
        {
            "oracle_id": "malformed-resource",
            "name": "Malformed",
            "set": "tst",
            "type_line": "Artifact",
            "types": ["Artifact"],
            "produced_mana": "W",
        }
    )
    assert malformed_resource.assignments == ()
    assert any("produced_mana" in report.reason for report in malformed_resource.unknown_reports)
    malformed_number = classify_card(
        {
            "oracle_id": "malformed-number",
            "name": "Malformed Number",
            "set": "tst",
            "type_line": "Creature",
            "types": ["Creature"],
            "mana_value": "2",
        }
    )
    assert malformed_number.assignments == ()
    assert any("mana_value" in report.reason for report in malformed_number.unknown_reports)
    malformed_face = classify_card(_fixtures()["role-malformed-face"])
    assert malformed_face.assignments == ()
    assert any("faces[1]" in report.reason for report in malformed_face.unknown_reports)


def test_shared_identity_profile_resolution_and_atomic_rebuild(tmp_path, monkeypatch) -> None:
    normalized = {
        "arena_id": 903,
        "grp_id": 903,
        "oracle_id": "shared-oracle",
        "name": "Shared Identity",
        "set": "tst",
        "collector_number": "903",
        "layout": "normal",
        "oracle_text": "Draw four cards.",
        "type_line": "Sorcery",
        "types": [],
        "mana_value": 5,
    }
    card = CardInfo(
        grp_id=903,
        name="Shared Identity",
        colors=(),
        mana_value=5,
        rarity="common",
        types=(),
        oracle_text="Draw four cards.",
        type_line="Sorcery",
        set_code="tst",
        collector_number="903",
        arena_id=None,
        source_provenance=("scryfall",),
    )

    assert card.arena_id is None
    local = classify_card(normalized)
    profile = compile_role_profile(set_code="tst", results=[local])
    resolved = RoleClassifier().resolve(card, profile=profile)
    assert resolved.source == "compiled_profile"
    assert Role.DRAW in _roles(resolved.classification)

    override = OverrideSet(
        (
            ReviewedOverride(
                key="grp_id:903",
                add=(RoleAssignment(Role.MANA_SINK),),
                rationale="Exercise the alternate numeric identity alias.",
            ),
        )
    )
    overridden = classify_card(normalized, overrides=override)
    assert _roles(overridden) >= {Role.DRAW, Role.MANA_SINK}
    assert "reviewed_override:grp_id:903" in overridden.diagnostics
    normalized_path = tmp_path / "rows.jsonl"
    normalized_path.write_text(
        json.dumps(normalized) + "\n" + json.dumps(normalized) + "\n",
        encoding="utf-8",
    )
    rebuilt = rebuild_role_profile(
        normalized_path=normalized_path,
        set_code="tst",
        output_path=tmp_path / "rebuilt.json",
    )
    assert len(rebuilt.cards) == 1
    conflict_path = tmp_path / "conflict.jsonl"
    conflict = {**normalized, "oracle_text": "Counter target spell."}
    conflict_path.write_text(
        json.dumps(normalized) + "\n" + json.dumps(conflict) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RoleProfileError, match="Conflicting duplicate"):
        rebuild_role_profile(
            normalized_path=conflict_path,
            set_code="tst",
            output_path=tmp_path / "conflict.json",
        )
    destination = tmp_path / "existing.json"
    destination.write_bytes(b"previous")
    monkeypatch.setattr(
        semantic_roles.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(OSError, match="boom"):
        dump_role_profile(profile, destination)
    assert destination.read_bytes() == b"previous"


def test_faces_do_not_leak_conditions_between_assignments() -> None:
    result = classify_card(_fixtures()["role-face-leak"])
    assert Role.HARD_REMOVAL in _roles(result)
    assert Role.CONDITIONAL_REMOVAL not in _roles(result)


def test_cardinfo_faces_isolate_combined_top_level_text() -> None:
    card = CardInfo(
        grp_id=904,
        name="CardInfo Independent Faces",
        colors=(),
        mana_value=3,
        rarity="common",
        types=(),
        oracle_text="Destroy target creature.\nDraw a card if you control an artifact.",
        type_line="Instant",
        faces=(
            CardFace(
                name="Removal Face",
                oracle_text="Destroy target creature.",
                type_line="Instant",
            ),
            CardFace(
                name="Condition Face",
                oracle_text="Draw a card if you control an artifact.",
                type_line="Sorcery",
            ),
        ),
        set_code="tst",
        source_provenance=("scryfall",),
    )
    result = classify_card(card)
    assert Role.HARD_REMOVAL in _roles(result)
    assert Role.CONDITIONAL_REMOVAL not in _roles(result)
