"""Behavior tests for the pinned relationship validation contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import re
from typing import Any

import pytest

from draftomen.carddb import CardFace, CardInfo
from draftomen.semantic_capability_records import (
    CapabilityPrerequisite,
    CapabilityQuantity,
    CapabilityZone,
    CardCapability,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import EnrichmentSources, card_source_sha256
from draftomen.semantic_enrichment_records import (
    CardSourcePin,
    FindingReview,
    FindingStatus,
    OracleEvidence,
    RejectedFinding,
    SemanticEnrichmentError,
)
from draftomen.semantic_relationship_records import (
    RELATIONSHIP_PREREQUISITE_PROJECTION_SCHEMA_VERSION,
    CardRelationship,
    PrerequisiteProjectionError,
    RelationshipParticipant,
    RelationshipPrerequisite,
    RelationshipPrerequisiteProjection,
    RelationshipTiming,
    RelationshipZone,
    role_anchor_covered,
    validate_prerequisite_projection,
    validate_prerequisite_sources,
    validate_relationship_pins,
    validate_relationship_sources,
)
from draftomen.semantic_roles import Role, role_definition
from draftomen.set_enrichment_candidates import CANDIDATE_REASON, CandidatePackage
from draftomen.set_enrichment_extraction import (
    RELATIONSHIP_VALIDATION_CONTRACT_VERSION,
    RELATIONSHIP_VALIDATION_PROMPT_ID,
    RELATIONSHIP_VALIDATION_RESPONSE_SCHEMA_ID,
    RELATIONSHIP_VALIDATION_SCHEMA_NAME,
    SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
    ExtractionOutcome,
    RelationshipValidationResult,
    SetEnrichmentExtractionError,
    ValidatedRelationship,
    build_relationship_validation_request,
    parse_relationship_validation_response,
    relationship_source_sha256,
    relationship_subject_id,
)


SET_CODE = "tst"
RUN_ID = "run-1"
MECHANISM = "token-go-wide-payoff"

TOKEN_ID = 301
WIDE_ID = 311
MULTIFACE_ID = 321
FOREIGN_CARD_ID = 999
DEATH_ID = 331
NONTOKEN_DEATH_ID = 341
LOOT_ID = 351
TOKEN_DEATH_MECHANISM = "token-death-payoff"

TOKEN_CARD_NAME = "Token Enabler"
TOKEN_PREREQUISITE_QUOTE = "You may sacrifice a creature."
TOKEN_QUOTE = "create two 1/1 colorless Soldier artifact creature tokens"
TOKEN_CARD_TEXT = f"{TOKEN_PREREQUISITE_QUOTE} When you do, {TOKEN_QUOTE}."
TOKEN_TIMING = "beginning of combat"
WIDE_CARD_NAME = "Wide Payoff"
WIDE_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
WIDE_CARD_TEXT = f"{WIDE_QUOTE} Until end of turn."
FABRICATED_QUOTE = "create three 1/1 colorless Soldier artifact creature tokens"
TOKEN_DEATH_CLAIM = (
    "The enabler creates a creature token, and a creature token that dies triggers the payoff's "
    "reward for creatures dying."
)
DEATH_QUOTE = "Whenever one or more other creatures die, scry 1."
DEATH_CARD_TEXT = (
    f"{DEATH_QUOTE} (Look at the top card of your library. You may put that card on the bottom.)"
)
NONTOKEN_DEATH_QUOTE = "Whenever one or more nontoken creatures you control die, scry 1."
NONTOKEN_DEATH_CARD_TEXT = (
    f"{NONTOKEN_DEATH_QUOTE} "
    "(Look at the top card of your library. You may put that card on the bottom.)"
)
DEATH_CARD_NAME = "Death Payoff"
NONTOKEN_DEATH_CARD_NAME = "Nontoken Death Payoff"
LOOT_CARD_NAME = "Loot With Tokens"

MULTIFACE_CARD_NAME = "Vanguard // Rearguard"
FRONT_FACE_NAME = "Vanguard"
FRONT_FACE_TEXT = "At the beginning of combat on your turn, create a 1/1 colorless Soldier token."
BACK_FACE_NAME = "Rearguard"
BACK_FACE_TEXT = "Whenever you attack, creatures you control get +1/+0 until end of turn."
MULTIFACE_CARD_TEXT = f"{FRONT_FACE_TEXT} // {BACK_FACE_TEXT}"

RELATIONSHIP_CLAIM = "The tokens this card creates make the payoff creature larger."
MODEL_REJECTION_REASON = "The stated mechanism is not supported by both Oracle texts."
MODEL_UNCERTAINTY_REASON = "Both Oracle texts support the interaction but the timing is ambiguous."

CAPABILITY_REVIEW_REASON = "capability requires semantic review beyond exact-source validation."
MALFORMED_REASON = "response does not match relationship validation schema version 2."
NOT_COMPLETE_STATUS = "unsupported"
EVIDENCE_OWNER_REASON = "relationship Oracle evidence does not belong to a candidate participant."
EVIDENCE_QUOTE_REASON = "relationship Oracle evidence quote is not an exact source substring."
EVIDENCE_COVERAGE_REASON = (
    "accepted relationships require Oracle evidence for both participants."
)
REASON_FIELD_REASON = "relationship verdict and reason do not agree."
CARD_SELECTION_ERROR = "card_id must identify exactly one frozen canonical card."
PARTICIPANT_FACE_ERROR = "participant face_index must identify a face of its card."

TOKEN_QUANTITY = CapabilityQuantity(value=2, relation=QuantityRelation.EXACTLY)
SCALED_QUANTITY = CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST)


def _card(grp_id: int, name: str, oracle_text: str) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=("U", "B"),
        mana_value=3.0,
        rarity="uncommon",
        types=("Creature",),
        oracle_text=oracle_text,
        set_code=SET_CODE,
    )


def _multiface_card() -> CardInfo:
    return CardInfo(
        grp_id=MULTIFACE_ID,
        name=MULTIFACE_CARD_NAME,
        colors=("R",),
        mana_value=4.0,
        rarity="rare",
        types=("Creature",),
        oracle_text=MULTIFACE_CARD_TEXT,
        type_line="Creature — Soldier // Creature — Soldier",
        layout="transform",
        faces=(
            CardFace(
                name=FRONT_FACE_NAME,
                type_line="Creature — Soldier",
                oracle_text=FRONT_FACE_TEXT,
            ),
            CardFace(
                name=BACK_FACE_NAME,
                type_line="Creature — Soldier",
                oracle_text=BACK_FACE_TEXT,
            ),
        ),
        set_code=SET_CODE,
    )


def _sources() -> EnrichmentSources:
    return EnrichmentSources(
        set_code=SET_CODE,
        cards=(
            _card(TOKEN_ID, TOKEN_CARD_NAME, TOKEN_CARD_TEXT),
            _card(WIDE_ID, WIDE_CARD_NAME, WIDE_CARD_TEXT),
            _card(DEATH_ID, DEATH_CARD_NAME, DEATH_CARD_TEXT),
            _card(NONTOKEN_DEATH_ID, NONTOKEN_DEATH_CARD_NAME, NONTOKEN_DEATH_CARD_TEXT),
            _card(LOOT_ID, LOOT_CARD_NAME, TOKEN_CARD_TEXT),
            _multiface_card(),
        ),
        guides=(),
    )


@pytest.fixture
def sources() -> EnrichmentSources:
    return _sources()


def _capability(
    *,
    finding_id: str,
    card_id: int,
    card_name: str,
    role: Role,
    quote: str,
    face_index: int | None = None,
    face_name: str | None = None,
    quantity: CapabilityQuantity | None = None,
    timing: str | None = None,
    source_zone: CapabilityZone | None = None,
    destination_zone: CapabilityZone | None = None,
    prerequisites: tuple[CapabilityPrerequisite, ...] = (),
    status: FindingStatus = FindingStatus.ACCEPTED,
    run_id: str = RUN_ID,
) -> CardCapability:
    """Build one real capability record around a single Oracle evidence quote."""
    return CardCapability(
        finding_id=finding_id,
        card_id=card_id,
        card_name=card_name,
        face_index=face_index,
        face_name=face_name,
        role=role,
        quantity=quantity,
        timing=timing,
        source_zone=source_zone,
        destination_zone=destination_zone,
        prerequisites=prerequisites,
        evidence=(OracleEvidence(card_id=card_id, face_index=face_index, quote=quote),),
        review=FindingReview(
            status=status,
            reason=None if status is FindingStatus.ACCEPTED else CAPABILITY_REVIEW_REASON,
        ),
        run_id=run_id,
    )


def _token_enabler(
    *,
    status: FindingStatus = FindingStatus.ACCEPTED,
    run_id: str = RUN_ID,
    quantity: CapabilityQuantity | None = TOKEN_QUANTITY,
) -> CardCapability:
    """Build one token-making candidate participant carrying every semantic field."""
    return _capability(
        finding_id="capability-tokens",
        card_id=TOKEN_ID,
        card_name=TOKEN_CARD_NAME,
        role=Role.TOKEN_MAKER,
        quote=TOKEN_QUOTE,
        quantity=quantity,
        timing=TOKEN_TIMING,
        source_zone=CapabilityZone.BATTLEFIELD,
        destination_zone=CapabilityZone.BATTLEFIELD,
        prerequisites=(
            CapabilityPrerequisite(
                kind=PrerequisiteKind.COST,
                quantity=None,
                timing=None,
                source_zone=None,
                destination_zone=None,
                evidence=OracleEvidence(
                    card_id=TOKEN_ID,
                    face_index=None,
                    quote=TOKEN_PREREQUISITE_QUOTE,
                ),
            ),
        ),
        status=status,
        run_id=run_id,
    )


def _second_token_enabler() -> CardCapability:
    """Build a second token-making capability declared on the same card."""
    return _capability(
        finding_id="capability-tokens-two",
        card_id=TOKEN_ID,
        card_name=TOKEN_CARD_NAME,
        role=Role.TOKEN_MAKER,
        quote=TOKEN_QUOTE,
    )


def _wide_payoff(*, run_id: str = RUN_ID) -> CardCapability:
    """Build one go-wide payoff candidate participant."""
    return _capability(
        finding_id="capability-wide",
        card_id=WIDE_ID,
        card_name=WIDE_CARD_NAME,
        role=Role.GO_WIDE_PAYOFF,
        quote=WIDE_QUOTE,
        run_id=run_id,
    )


def _death_payoff() -> CardCapability:
    """Build one creature-death payoff candidate participant."""
    return _capability(
        finding_id="capability-death",
        card_id=DEATH_ID,
        card_name=DEATH_CARD_NAME,
        role=Role.DEATH_PAYOFF,
        quote=DEATH_QUOTE,
    )


def _nontoken_death_payoff() -> CardCapability:
    """Build one death payoff whose quoted reward names nontoken creatures."""
    return _capability(
        finding_id="capability-nontoken-death",
        card_id=NONTOKEN_DEATH_ID,
        card_name=NONTOKEN_DEATH_CARD_NAME,
        role=Role.DEATH_PAYOFF,
        quote=NONTOKEN_DEATH_QUOTE,
    )


def _loot_token_enabler() -> CardCapability:
    """Build one loot participant whose quoted ability creates a creature token."""
    return _capability(
        finding_id="capability-loot",
        card_id=LOOT_ID,
        card_name=LOOT_CARD_NAME,
        role=Role.LOOT,
        quote=TOKEN_QUOTE,
    )


def _front_face_enabler() -> CardCapability:
    """Build one token-making participant bound to the front face of a card."""
    return _capability(
        finding_id="capability-front",
        card_id=MULTIFACE_ID,
        card_name=MULTIFACE_CARD_NAME,
        role=Role.TOKEN_MAKER,
        quote=FRONT_FACE_TEXT,
        face_index=0,
        face_name=FRONT_FACE_NAME,
    )


def _multiface_participant(face_index: int) -> CardCapability:
    """Build one participant declaring an index into the multiface card's faces."""
    return _capability(
        finding_id=f"capability-face-{face_index}",
        card_id=MULTIFACE_ID,
        card_name=MULTIFACE_CARD_NAME,
        role=Role.TOKEN_MAKER,
        quote=FRONT_FACE_TEXT,
        face_index=face_index,
    )


def _negative_face_participant() -> CardCapability:
    """Build one participant carrying a face index the record constructor rejects."""
    participant = _multiface_participant(0)
    object.__setattr__(participant, "face_index", -1)
    return participant


def _evidence(
    *,
    card_id: int = TOKEN_ID,
    face_index: int | None = None,
    quote: str = TOKEN_QUOTE,
) -> dict[str, Any]:
    return {"card_id": card_id, "face_index": face_index, "quote": quote}


def _coverage_evidence() -> list[dict[str, Any]]:
    return [_evidence(), _evidence(card_id=WIDE_ID, quote=WIDE_QUOTE)]


def _participant_coverage(source: CardCapability, target: CardCapability) -> list[dict[str, Any]]:
    """Quote the exact frozen Oracle text each participant capability binds to."""
    return [
        _evidence(card_id=item.card_id, face_index=item.face_index, quote=item.evidence[0].quote)
        for item in (source, target)
    ]


def _response(
    *,
    verdict: str = "accepted",
    claim: str = RELATIONSHIP_CLAIM,
    reason: str | None = None,
    evidence: list[Any] | None = None,
    prerequisite_status: str = NOT_COMPLETE_STATUS,
    source_prerequisites: list[Any] | None = None,
    target_prerequisites: list[Any] | None = None,
    schema_version: int = RELATIONSHIP_VALIDATION_CONTRACT_VERSION,
) -> dict[str, Any]:
    """Build one strict v2 relationship document around the default participant pair."""
    return {
        "schema_version": schema_version,
        "verdict": verdict,
        "claim": claim,
        "reason": reason,
        "evidence": _coverage_evidence() if evidence is None else list(evidence),
        "prerequisite_status": prerequisite_status,
        "source_prerequisites": [] if source_prerequisites is None else list(source_prerequisites),
        "target_prerequisites": [] if target_prerequisites is None else list(target_prerequisites),
    }


def _content(response: Any) -> str:
    return json.dumps(response)


def _parse(
    content: str,
    sources: EnrichmentSources,
    *,
    mechanism: str = MECHANISM,
    source: CardCapability | None = None,
    target: CardCapability | None = None,
) -> RelationshipValidationResult:
    return parse_relationship_validation_response(
        content=content,
        sources=sources,
        mechanism=mechanism,
        source=_token_enabler() if source is None else source,
        target=_wide_payoff() if target is None else target,
        run_id=RUN_ID,
    )


def _subject_id(
    *,
    source: CardCapability | None = None,
    target: CardCapability | None = None,
) -> str:
    return relationship_subject_id(
        mechanism=MECHANISM,
        source=_token_enabler() if source is None else source,
        target=_wide_payoff() if target is None else target,
    )


def _identity() -> tuple[str, int, str, int, str]:
    """Return the candidate identity of the default participant pair."""
    source = _token_enabler()
    target = _wide_payoff()
    return (MECHANISM, source.card_id, source.finding_id, target.card_id, target.finding_id)


def test_relationship_request_identity_is_stable_across_equivalent_inputs(
    sources: EnrichmentSources,
) -> None:
    forward = build_relationship_validation_request(
        sources=sources,
        mechanism=MECHANISM,
        source=_token_enabler(),
        target=_wide_payoff(),
    )
    equivalent = build_relationship_validation_request(
        sources=sources,
        mechanism=MECHANISM,
        source=_token_enabler(status=FindingStatus.UNCERTAIN, run_id="run-2"),
        target=_wide_payoff(run_id="run-2"),
    )

    assert equivalent.user_prompt == forward.user_prompt
    assert equivalent.system_prompt == forward.system_prompt
    assert equivalent.prompt_sha256 == forward.prompt_sha256
    assert equivalent.response_schema() == forward.response_schema()
    assert equivalent.response_schema_sha256 == forward.response_schema_sha256
    assert forward.contract_version == RELATIONSHIP_VALIDATION_CONTRACT_VERSION
    assert forward.prompt_id == RELATIONSHIP_VALIDATION_PROMPT_ID
    assert forward.response_schema_id == RELATIONSHIP_VALIDATION_RESPONSE_SCHEMA_ID
    assert forward.response_schema_name == RELATIONSHIP_VALIDATION_SCHEMA_NAME
    assert RELATIONSHIP_VALIDATION_CONTRACT_VERSION == 2
    assert SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION == 1
    assert RELATIONSHIP_VALIDATION_PROMPT_ID == "draftomen-relationship-validation-v2"
    assert (
        RELATIONSHIP_VALIDATION_RESPONSE_SCHEMA_ID
        == "draftomen-relationship-validation-response-v2"
    )
    assert RELATIONSHIP_VALIDATION_SCHEMA_NAME == "draftomen_relationship_validation_v2"
    assert re.fullmatch(r"[0-9a-f]{64}", forward.prompt_sha256) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", forward.response_schema_sha256) is not None


def test_relationship_response_schema_is_a_strict_closed_object(
    sources: EnrichmentSources,
) -> None:
    request = build_relationship_validation_request(
        sources=sources,
        mechanism=MECHANISM,
        source=_token_enabler(),
        target=_wide_payoff(),
    )

    schema = request.response_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "schema_version",
        "verdict",
        "claim",
        "reason",
        "evidence",
        "prerequisite_status",
        "source_prerequisites",
        "target_prerequisites",
    }
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["schema_version"] == {
        "type": "integer",
        "enum": [RELATIONSHIP_VALIDATION_CONTRACT_VERSION],
    }
    assert schema["properties"]["verdict"] == {
        "type": "string",
        "enum": ["accepted", "rejected", "uncertain"],
    }
    assert schema["properties"]["claim"] == {"type": "string", "minLength": 1}
    assert schema["properties"]["reason"] == {"type": ["string", "null"]}
    assert schema["properties"]["prerequisite_status"] == {
        "type": "string",
        "enum": ["complete", "uncertain", "unsupported"],
    }
    evidence_schema = schema["properties"]["evidence"]
    assert evidence_schema["type"] == "array"
    assert evidence_schema["items"]["additionalProperties"] is False
    assert set(evidence_schema["items"]["properties"]) == {"card_id", "face_index", "quote"}
    assert set(evidence_schema["items"]["required"]) == set(evidence_schema["items"]["properties"])
    for field_name in ("source_prerequisites", "target_prerequisites"):
        clause_schema = schema["properties"][field_name]
        assert clause_schema["type"] == "array"
        assert clause_schema["items"]["additionalProperties"] is False
        assert set(clause_schema["items"]["required"]) == set(clause_schema["items"]["properties"])
        assert set(clause_schema["items"]["properties"]) == {
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
        }
        assert clause_schema["items"]["properties"]["quantity"]["anyOf"] == [
            {"type": "null"},
            {
                "type": "object",
                "properties": {
                    "value": {"type": ["integer", "null"], "minimum": 1},
                    "relation": {
                        "type": "string",
                        "enum": ["at_least", "at_most", "exactly", "variable"],
                    },
                },
                "required": ["value", "relation"],
                "additionalProperties": False,
            },
        ]
        assert clause_schema["items"]["properties"]["evidence"] == evidence_schema["items"]


def test_relationship_prompt_carries_only_the_declared_mechanism_and_participants(
    sources: EnrichmentSources,
) -> None:
    source = _token_enabler()
    target = _wide_payoff()

    request = build_relationship_validation_request(
        sources=sources,
        mechanism=MECHANISM,
        source=source,
        target=target,
    )

    prompt = json.loads(request.user_prompt)

    assert set(prompt) == {
        "contract_version",
        "set_code",
        "mechanism",
        "source",
        "target",
        "source_oracle_text",
        "target_oracle_text",
        "source_role_definition",
        "target_role_definition",
    }
    assert prompt["contract_version"] == RELATIONSHIP_VALIDATION_CONTRACT_VERSION
    assert prompt["set_code"] == SET_CODE
    assert prompt["mechanism"] == MECHANISM
    assert set(prompt["source"]) == {
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
    }
    assert prompt["source"]["finding_id"] == source.finding_id
    assert prompt["source"]["card_id"] == TOKEN_ID
    assert prompt["source"]["role"] == Role.TOKEN_MAKER.value
    assert prompt["source"]["quantity"] == {"value": 2, "relation": "exactly"}
    assert prompt["source"]["timing"] == TOKEN_TIMING
    assert prompt["source"]["source_zone"] == "battlefield"
    assert prompt["source"]["prerequisites"] == [
        prerequisite.to_json() for prerequisite in source.prerequisites
    ]
    assert prompt["source"]["evidence"] == [entry.to_json() for entry in source.evidence]
    assert prompt["target"]["finding_id"] == target.finding_id
    assert prompt["target"]["card_id"] == WIDE_ID
    assert prompt["target"]["role"] == Role.GO_WIDE_PAYOFF.value
    assert "review" not in prompt["source"]
    assert "run_id" not in prompt["source"]
    assert "review" not in prompt["target"]
    assert "run_id" not in prompt["target"]
    assert prompt["source_oracle_text"] == TOKEN_CARD_TEXT
    assert prompt["target_oracle_text"] == WIDE_CARD_TEXT
    assert prompt["source_role_definition"] == role_definition(Role.TOKEN_MAKER)
    assert prompt["target_role_definition"] == role_definition(Role.GO_WIDE_PAYOFF)
    assert request.user_prompt == json.dumps(
        prompt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert RUN_ID not in request.user_prompt
    assert RUN_ID not in request.system_prompt
    assert CAPABILITY_REVIEW_REASON not in request.user_prompt


def test_relationship_source_sha256_binds_the_exact_prompt_content(
    sources: EnrichmentSources,
) -> None:
    source = _token_enabler()
    target = _wide_payoff()
    request = build_relationship_validation_request(
        sources=sources,
        mechanism=MECHANISM,
        source=source,
        target=target,
    )
    prompt = json.loads(request.user_prompt)
    bound_content = {key: prompt[key] for key in ("mechanism", "source", "target")}
    expected_bytes = json.dumps(
        bound_content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    digest = relationship_source_sha256(mechanism=MECHANISM, source=source, target=target)

    assert digest == hashlib.sha256(expected_bytes).hexdigest()
    assert digest == relationship_source_sha256(
        mechanism=MECHANISM,
        source=_token_enabler(status=FindingStatus.UNCERTAIN, run_id="run-2"),
        target=_wide_payoff(run_id="run-2"),
    )


def test_relationship_identity_helpers_validate_their_inputs() -> None:
    helpers: tuple[Callable[..., str], ...] = (
        relationship_subject_id,
        relationship_source_sha256,
    )
    source = _token_enabler()

    for helper in helpers:
        with pytest.raises(SetEnrichmentExtractionError) as blank_mechanism:
            helper(mechanism="   ", source=source, target=_wide_payoff())
        assert str(blank_mechanism.value) == "mechanism must be a nonblank string."

        with pytest.raises(SetEnrichmentExtractionError) as same_card:
            helper(mechanism=MECHANISM, source=source, target=_second_token_enabler())
        assert str(same_card.value) == "participants must be different cards."

        with pytest.raises(SetEnrichmentExtractionError) as raw_participant:
            helper(mechanism=MECHANISM, source=source, target={"card_id": WIDE_ID})
        assert str(raw_participant.value) == "participants must be CardCapability records."


def test_relationship_identity_and_source_hash_track_every_bound_input() -> None:
    source = _token_enabler()
    target = _wide_payoff()
    subject_id = relationship_subject_id(mechanism=MECHANISM, source=source, target=target)
    digest = relationship_source_sha256(mechanism=MECHANISM, source=source, target=target)

    assert subject_id == (
        f"relationship:{MECHANISM}:{TOKEN_ID}:{source.finding_id}:{WIDE_ID}:{target.finding_id}"
    )
    assert subject_id == _subject_id()
    assert subject_id != relationship_subject_id(
        mechanism="token-sacrifice-outlet",
        source=source,
        target=target,
    )
    assert subject_id != relationship_subject_id(
        mechanism=MECHANISM,
        source=target,
        target=source,
    )
    assert subject_id == _subject_id(target=_wide_payoff(run_id="run-2"))
    assert subject_id != _subject_id(source=_second_token_enabler())
    assert re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    assert digest != relationship_source_sha256(
        mechanism="token-sacrifice-outlet",
        source=source,
        target=target,
    )
    assert digest != relationship_source_sha256(
        mechanism=MECHANISM,
        source=_token_enabler(quantity=SCALED_QUANTITY),
        target=target,
    )
    assert digest != relationship_source_sha256(
        mechanism=MECHANISM,
        source=_second_token_enabler(),
        target=target,
    )
    assert digest == relationship_source_sha256(
        mechanism=MECHANISM,
        source=_token_enabler(status=FindingStatus.UNCERTAIN, run_id="run-2"),
        target=_wide_payoff(run_id="run-2"),
    )


def test_build_relationship_request_requires_both_participant_cards(
    sources: EnrichmentSources,
) -> None:
    with pytest.raises(SetEnrichmentExtractionError) as error:
        build_relationship_validation_request(
            sources=sources,
            mechanism=MECHANISM,
            source=_token_enabler(),
            target=_capability(
                finding_id="capability-foreign",
                card_id=FOREIGN_CARD_ID,
                card_name="Foreign Card",
                role=Role.GO_WIDE_PAYOFF,
                quote=WIDE_QUOTE,
            ),
        )

    assert str(error.value) == CARD_SELECTION_ERROR


@pytest.mark.parametrize(
    "participant",
    (
        pytest.param(_multiface_participant(99), id="face-index-beyond-card-faces"),
        pytest.param(_negative_face_participant(), id="negative-face-index"),
    ),
)
@pytest.mark.parametrize("participant_role", ("source", "target"))
def test_participants_must_bind_to_a_real_face_of_their_frozen_card(
    participant: CardCapability,
    participant_role: str,
    sources: EnrichmentSources,
) -> None:
    pair: dict[str, CardCapability] = {"source": _token_enabler(), "target": _wide_payoff()}
    pair[participant_role] = participant

    with pytest.raises(SetEnrichmentExtractionError) as request_error:
        build_relationship_validation_request(
            sources=sources,
            mechanism=MECHANISM,
            source=pair["source"],
            target=pair["target"],
        )
    assert str(request_error.value) == PARTICIPANT_FACE_ERROR

    with pytest.raises(SetEnrichmentExtractionError) as parse_error:
        _parse(
            _content(_response(evidence=_participant_coverage(pair["source"], pair["target"]))),
            sources,
            source=pair["source"],
            target=pair["target"],
        )
    assert str(parse_error.value) == PARTICIPANT_FACE_ERROR


def test_accepted_verdict_preserves_the_constructed_candidate(
    sources: EnrichmentSources,
) -> None:
    source = _token_enabler()
    target = _wide_payoff()

    result = _parse(_content(_response()), sources, source=source, target=target)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.rejected is None
    assert result.malformed_reason is None
    relationship = result.relationship
    assert relationship is not None
    assert type(relationship) is ValidatedRelationship
    assert relationship.mechanism == MECHANISM
    assert relationship.source is source
    assert relationship.target is target
    assert relationship.claim == RELATIONSHIP_CLAIM
    assert relationship.source.card_id == TOKEN_ID
    assert relationship.source.card_name == TOKEN_CARD_NAME
    assert relationship.source.role is Role.TOKEN_MAKER
    assert relationship.source.quantity == TOKEN_QUANTITY
    assert relationship.source.timing == TOKEN_TIMING
    assert relationship.source.source_zone is CapabilityZone.BATTLEFIELD
    assert relationship.source.destination_zone is CapabilityZone.BATTLEFIELD
    assert relationship.source.prerequisites == source.prerequisites
    assert relationship.source.evidence == source.evidence
    assert relationship.target.role is Role.GO_WIDE_PAYOFF
    assert relationship.target.prerequisites == ()
    assert relationship.target.evidence == target.evidence
    assert relationship.evidence == (
        OracleEvidence(card_id=TOKEN_ID, face_index=None, quote=TOKEN_QUOTE),
        OracleEvidence(card_id=WIDE_ID, face_index=None, quote=WIDE_QUOTE),
    )
    assert relationship.review == FindingReview(status=FindingStatus.ACCEPTED, reason=None)
    assert relationship.run_id == RUN_ID
    assert relationship.identity == (
        MECHANISM,
        TOKEN_ID,
        source.finding_id,
        WIDE_ID,
        target.finding_id,
    )
    assert relationship.identity == CandidatePackage(
        mechanism=MECHANISM,
        source=source,
        target=target,
        reason=CANDIDATE_REASON,
    ).identity
    assert relationship.finding_id == relationship_subject_id(
        mechanism=MECHANISM,
        source=source,
        target=target,
    )


def test_mixed_case_mechanism_keeps_its_case_in_identity_and_finding_id(
    sources: EnrichmentSources,
) -> None:
    mechanism = "Token-Go-Wide-Payoff"
    source = _token_enabler()
    target = _wide_payoff()

    result = _parse(
        _content(_response()),
        sources,
        mechanism=mechanism,
        source=source,
        target=target,
    )

    relationship = result.relationship
    assert relationship is not None
    assert relationship.mechanism == mechanism
    assert relationship.identity == (
        mechanism,
        TOKEN_ID,
        source.finding_id,
        WIDE_ID,
        target.finding_id,
    )
    assert relationship.identity == CandidatePackage(
        mechanism=mechanism,
        source=source,
        target=target,
        reason=CANDIDATE_REASON,
    ).identity
    assert relationship.finding_id == relationship_subject_id(
        mechanism=mechanism,
        source=source,
        target=target,
    )
    assert relationship.finding_id == (
        f"relationship:{mechanism}:{TOKEN_ID}:{source.finding_id}:{WIDE_ID}:{target.finding_id}"
    )
    assert relationship.to_json()["mechanism"] == mechanism
    assert ValidatedRelationship.from_json(relationship.to_json()) == relationship
    assert RelationshipValidationResult.from_json(result.to_json()) == result


def test_uncertain_verdict_keeps_the_model_reason_and_evidence(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content(
            _response(
                verdict="uncertain",
                reason=MODEL_UNCERTAINTY_REASON,
                evidence=[_evidence(card_id=WIDE_ID, quote=WIDE_QUOTE)],
            )
        ),
        sources,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.rejected is None
    relationship = result.relationship
    assert relationship is not None
    assert relationship.review == FindingReview(
        status=FindingStatus.UNCERTAIN,
        reason=MODEL_UNCERTAINTY_REASON,
    )
    assert relationship.evidence == (
        OracleEvidence(card_id=WIDE_ID, face_index=None, quote=WIDE_QUOTE),
    )
    assert relationship.identity == _identity()


def test_model_rejected_verdict_keeps_its_reason_as_a_diagnostic(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content(
            _response(
                verdict="rejected",
                reason=MODEL_REJECTION_REASON,
                evidence=[_evidence(card_id=WIDE_ID, quote=WIDE_QUOTE)],
            )
        ),
        sources,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.relationship is None
    assert result.malformed_reason is None
    assert result.rejected is not None
    assert result.rejected.source_kind == "relationship"
    assert result.rejected.finding_id == _subject_id()
    assert result.rejected.summary == RELATIONSHIP_CLAIM
    assert result.rejected.reason == MODEL_REJECTION_REASON
    assert result.rejected.run_id == RUN_ID


def test_token_death_pair_is_decided_before_the_model_verdict(
    sources: EnrichmentSources,
) -> None:
    source = _token_enabler()
    target = _death_payoff()

    result = _parse(
        _content(_response(verdict="rejected", reason=MODEL_REJECTION_REASON)),
        sources,
        mechanism=TOKEN_DEATH_MECHANISM,
        source=source,
        target=target,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.rejected is None
    assert result.malformed_reason is None
    relationship = result.relationship
    assert relationship is not None
    assert type(relationship) is ValidatedRelationship
    assert relationship.review == FindingReview(status=FindingStatus.ACCEPTED, reason=None)
    assert relationship.claim == TOKEN_DEATH_CLAIM
    assert relationship.evidence == (
        OracleEvidence(card_id=TOKEN_ID, face_index=None, quote=TOKEN_QUOTE),
        OracleEvidence(card_id=DEATH_ID, face_index=None, quote=DEATH_QUOTE),
    )
    assert relationship.identity == (
        TOKEN_DEATH_MECHANISM,
        TOKEN_ID,
        source.finding_id,
        DEATH_ID,
        target.finding_id,
    )
    assert relationship.identity == CandidatePackage(
        mechanism=TOKEN_DEATH_MECHANISM,
        source=source,
        target=target,
        reason=CANDIDATE_REASON,
    ).identity
    assert relationship.finding_id == relationship_subject_id(
        mechanism=TOKEN_DEATH_MECHANISM,
        source=source,
        target=target,
    )


def test_token_death_verdict_is_identical_across_model_draws(
    sources: EnrichmentSources,
) -> None:
    source = _token_enabler()
    target = _death_payoff()

    results = [
        _parse(
            _content(_response(verdict=verdict, reason=reason)),
            sources,
            mechanism=TOKEN_DEATH_MECHANISM,
            source=source,
            target=target,
        )
        for verdict, reason in (
            ("accepted", None),
            ("uncertain", MODEL_UNCERTAINTY_REASON),
            ("rejected", MODEL_REJECTION_REASON),
        )
    ]

    assert results[1] == results[0]
    assert results[2] == results[0]
    for result in results:
        relationship = result.relationship
        assert relationship is not None
        assert relationship.review == FindingReview(status=FindingStatus.ACCEPTED, reason=None)
        assert relationship.claim == TOKEN_DEATH_CLAIM
        assert relationship.evidence == (
            OracleEvidence(card_id=TOKEN_ID, face_index=None, quote=TOKEN_QUOTE),
            OracleEvidence(card_id=DEATH_ID, face_index=None, quote=DEATH_QUOTE),
        )


def test_nontoken_death_payoff_keeps_the_model_rejection(
    sources: EnrichmentSources,
) -> None:
    source = _token_enabler()
    target = _nontoken_death_payoff()

    result = _parse(
        _content(
            _response(
                verdict="rejected",
                reason=MODEL_REJECTION_REASON,
                evidence=_participant_coverage(source, target),
            )
        ),
        sources,
        mechanism=TOKEN_DEATH_MECHANISM,
        source=source,
        target=target,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.relationship is None
    assert result.rejected is not None
    assert result.rejected.reason == MODEL_REJECTION_REASON


def test_token_death_pair_under_another_mechanism_keeps_the_model_rejection(
    sources: EnrichmentSources,
) -> None:
    source = _token_enabler()
    target = _death_payoff()

    result = _parse(
        _content(
            _response(
                verdict="rejected",
                reason=MODEL_REJECTION_REASON,
                evidence=_participant_coverage(source, target),
            )
        ),
        sources,
        source=source,
        target=target,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.relationship is None
    assert result.rejected is not None
    assert result.rejected.reason == MODEL_REJECTION_REASON


def test_payoff_without_the_death_role_keeps_the_model_rejection(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content(_response(verdict="rejected", reason=MODEL_REJECTION_REASON)),
        sources,
        mechanism=TOKEN_DEATH_MECHANISM,
        source=_token_enabler(),
        target=_wide_payoff(),
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.relationship is None
    assert result.rejected is not None
    assert result.rejected.reason == MODEL_REJECTION_REASON


def test_enabler_without_the_token_role_keeps_the_model_rejection(
    sources: EnrichmentSources,
) -> None:
    source = _loot_token_enabler()
    target = _death_payoff()

    result = _parse(
        _content(
            _response(
                verdict="rejected",
                reason=MODEL_REJECTION_REASON,
                evidence=_participant_coverage(source, target),
            )
        ),
        sources,
        mechanism=TOKEN_DEATH_MECHANISM,
        source=source,
        target=target,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.relationship is None
    assert result.rejected is not None
    assert result.rejected.reason == MODEL_REJECTION_REASON


def test_covered_pair_publishes_participant_evidence_not_the_model_evidence(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content(_response(evidence=[_evidence(card_id=FOREIGN_CARD_ID, quote=TOKEN_QUOTE)])),
        sources,
        mechanism=TOKEN_DEATH_MECHANISM,
        source=_token_enabler(),
        target=_death_payoff(),
    )

    relationship = result.relationship
    assert relationship is not None
    assert relationship.review.status is FindingStatus.ACCEPTED
    assert {item.card_id for item in relationship.evidence} == {TOKEN_ID, DEATH_ID}


def test_undecodable_response_for_a_covered_pair_is_still_malformed(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content({"verdict": "accepted"}),
        sources,
        mechanism=TOKEN_DEATH_MECHANISM,
        source=_token_enabler(),
        target=_death_payoff(),
    )

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == MALFORMED_REASON
    assert result.relationship is None
    assert result.rejected is None


def test_legacy_version_one_document_is_malformed(
    sources: EnrichmentSources,
) -> None:
    legacy = {
        "schema_version": 1,
        "verdict": "accepted",
        "claim": RELATIONSHIP_CLAIM,
        "reason": None,
        "evidence": _coverage_evidence(),
    }

    result = _parse(_content(legacy), sources)

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == MALFORMED_REASON
    assert result.relationship is None
    assert result.rejected is None


def test_identical_token_death_evidence_shapes_share_one_verdict(
    sources: EnrichmentSources,
) -> None:
    first = _parse(
        _content(_response(verdict="rejected", reason=MODEL_REJECTION_REASON)),
        sources,
        mechanism=TOKEN_DEATH_MECHANISM,
        source=_token_enabler(),
        target=_death_payoff(),
    )
    second = _parse(
        _content(_response(verdict="rejected", reason=MODEL_REJECTION_REASON)),
        sources,
        mechanism=TOKEN_DEATH_MECHANISM,
        source=_second_token_enabler(),
        target=_death_payoff(),
    )

    first_relationship = first.relationship
    second_relationship = second.relationship
    assert first_relationship is not None
    assert second_relationship is not None
    assert first_relationship.review.status is FindingStatus.ACCEPTED
    assert second_relationship.review.status is FindingStatus.ACCEPTED
    assert first_relationship.claim == TOKEN_DEATH_CLAIM == second_relationship.claim
    assert first_relationship.identity != second_relationship.identity


def test_uncertain_verdict_without_evidence_is_malformed(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content(
            _response(
                verdict="uncertain",
                reason=MODEL_UNCERTAINTY_REASON,
                evidence=[],
            )
        ),
        sources,
    )

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == MALFORMED_REASON
    assert result.relationship is None
    assert result.rejected is None


def test_rejected_verdict_without_evidence_keeps_the_model_reason(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content(
            _response(
                verdict="rejected",
                reason=MODEL_REJECTION_REASON,
                evidence=[],
            )
        ),
        sources,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.relationship is None
    assert result.rejected is not None
    assert result.rejected.reason == MODEL_REJECTION_REASON


def test_relationship_evidence_is_canonical_and_records_are_frozen(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content(
            _response(
                evidence=[
                    _evidence(card_id=WIDE_ID, quote=WIDE_QUOTE),
                    _evidence(),
                    _evidence(),
                ]
            )
        ),
        sources,
    )

    relationship = result.relationship
    assert relationship is not None
    assert relationship.evidence == (
        OracleEvidence(card_id=TOKEN_ID, face_index=None, quote=TOKEN_QUOTE),
        OracleEvidence(card_id=WIDE_ID, face_index=None, quote=WIDE_QUOTE),
    )
    with pytest.raises(FrozenInstanceError):
        relationship.claim = "A different claim."  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.relationship = None  # type: ignore[misc]


def test_validated_relationship_and_result_round_trip_through_stored_json(
    sources: EnrichmentSources,
) -> None:
    accepted = _parse(_content(_response()), sources)
    rejected = _parse(
        _content(_response(verdict="rejected", reason=MODEL_REJECTION_REASON)),
        sources,
    )
    malformed = _parse("not json", sources)

    relationship = accepted.relationship
    assert relationship is not None
    assert ValidatedRelationship.from_json(relationship.to_json()) == relationship
    assert RelationshipValidationResult.from_json(accepted.to_json()) == accepted
    assert RelationshipValidationResult.from_json(rejected.to_json()) == rejected
    assert RelationshipValidationResult.from_json(malformed.to_json()) == malformed


@pytest.mark.parametrize(
    ("content", "expected_reason"),
    (
        pytest.param(
            _content(_response(evidence=[_evidence(quote=FABRICATED_QUOTE)])),
            EVIDENCE_QUOTE_REASON,
            id="fabricated-quote",
        ),
        pytest.param(
            _content(
                _response(
                    evidence=[
                        _evidence(card_id=FOREIGN_CARD_ID),
                        _evidence(card_id=WIDE_ID, quote=WIDE_QUOTE),
                    ]
                )
            ),
            EVIDENCE_OWNER_REASON,
            id="foreign-card-id",
        ),
        pytest.param(
            _content(
                _response(
                    evidence=[
                        _evidence(face_index=0),
                        _evidence(card_id=WIDE_ID, quote=WIDE_QUOTE),
                    ]
                )
            ),
            EVIDENCE_OWNER_REASON,
            id="wrong-face-index",
        ),
        pytest.param(
            _content(
                _response(
                    evidence=[
                        _evidence(quote=WIDE_QUOTE),
                        _evidence(card_id=WIDE_ID, quote=TOKEN_QUOTE),
                    ]
                )
            ),
            EVIDENCE_QUOTE_REASON,
            id="quotes-swapped-between-participants",
        ),
        pytest.param(
            _content(_response(evidence=[_evidence()])),
            EVIDENCE_COVERAGE_REASON,
            id="missing-participant-coverage",
        ),
        pytest.param(
            _content(_response(reason=MODEL_UNCERTAINTY_REASON)),
            REASON_FIELD_REASON,
            id="accepted-with-a-reason",
        ),
        pytest.param(
            _content(_response(verdict="uncertain")),
            REASON_FIELD_REASON,
            id="uncertain-without-a-reason",
        ),
    ),
)
def test_rejected_verdicts_report_the_fixed_reason(
    content: str,
    expected_reason: str,
    sources: EnrichmentSources,
) -> None:
    result = _parse(content, sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.relationship is None
    assert result.malformed_reason is None
    assert result.rejected is not None
    assert result.rejected.source_kind == "relationship"
    assert result.rejected.reason == expected_reason
    assert result.rejected.finding_id == _subject_id()
    assert result.rejected.summary == RELATIONSHIP_CLAIM
    assert result.rejected.run_id == RUN_ID


def test_multiface_participant_rejects_evidence_bound_to_another_face(
    sources: EnrichmentSources,
) -> None:
    result = _parse(
        _content(
            _response(
                evidence=[
                    {"card_id": MULTIFACE_ID, "face_index": 1, "quote": BACK_FACE_TEXT},
                    _evidence(card_id=WIDE_ID, quote=WIDE_QUOTE),
                ]
            )
        ),
        sources,
        source=_front_face_enabler(),
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.relationship is None
    assert result.rejected is not None
    assert result.rejected.reason == EVIDENCE_OWNER_REASON


@pytest.mark.parametrize(
    "content",
    (
        pytest.param(
            _content({key: value for key, value in _response().items() if key != "reason"}),
            id="missing-key",
        ),
        pytest.param(
            _content({**_response(), "confidence": 0.9}),
            id="extra-key",
        ),
        pytest.param(
            _content(_response()).replace(
                '"verdict": "accepted"',
                '"verdict": "accepted", "verdict": "accepted"',
            ),
            id="duplicate-key",
        ),
        pytest.param(
            _content(_response()).replace('"schema_version": 2', '"schema_version": NaN'),
            id="non-finite-number",
        ),
        pytest.param("[1, 2]", id="non-object-body"),
        pytest.param(
            _content({**_response(), "evidence": _evidence()}),
            id="non-list-evidence",
        ),
        pytest.param(
            _content(_response(evidence=[_evidence(quote="   ")])),
            id="blank-quote",
        ),
        pytest.param(
            _content(_response(schema_version=1)),
            id="unsupported-schema-version-one",
        ),
        pytest.param(
            _content(_response(schema_version=3)),
            id="unsupported-schema-version",
        ),
        pytest.param(
            _content(_response(verdict="partial")),
            id="unsupported-verdict",
        ),
        pytest.param(
            _content(_response(prerequisite_status="partial")),
            id="unsupported-prerequisite-status",
        ),
        pytest.param(
            _content(
                {
                    key: value
                    for key, value in _response().items()
                    if key != "prerequisite_status"
                }
            ),
            id="missing-prerequisite-status",
        ),
        pytest.param(
            _content(
                {
                    key: value
                    for key, value in _response().items()
                    if key != "source_prerequisites"
                }
            ),
            id="missing-source-prerequisites",
        ),
        pytest.param(
            _content({**_response(), "source_prerequisites": {}}),
            id="non-list-source-prerequisites",
        ),
        pytest.param(
            _content({**_response(), "target_prerequisites": "none"}),
            id="non-list-target-prerequisites",
        ),
        pytest.param(
            _content(
                _response(
                    evidence=[
                        {
                            "card_id": TOKEN_ID,
                            "face_index": None,
                            "quote": TOKEN_QUOTE,
                            "extra": 1,
                        }
                    ]
                )
            ),
            id="extra-evidence-key",
        ),
    ),
)
def test_malformed_responses_produce_the_fixed_outcome(
    content: str,
    sources: EnrichmentSources,
) -> None:
    result = _parse(content, sources)

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == MALFORMED_REASON
    assert result.relationship is None
    assert result.rejected is None


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param(
            {"review": FindingReview(status=FindingStatus.REJECTED, reason=MODEL_REJECTION_REASON)},
            id="rejected-review-status",
        ),
        pytest.param(
            {"evidence": OracleEvidence(card_id=TOKEN_ID, face_index=None, quote=TOKEN_QUOTE)},
            id="evidence-is-not-a-tuple",
        ),
        pytest.param({"evidence": ()}, id="empty-evidence"),
        pytest.param(
            {
                "evidence": (
                    OracleEvidence(card_id=FOREIGN_CARD_ID, face_index=None, quote=TOKEN_QUOTE),
                )
            },
            id="evidence-outside-the-participants",
        ),
        pytest.param(
            {
                "evidence": (
                    OracleEvidence(card_id=TOKEN_ID, face_index=None, quote=TOKEN_QUOTE),
                    OracleEvidence(card_id=TOKEN_ID, face_index=None, quote=TOKEN_QUOTE),
                )
            },
            id="repeated-evidence",
        ),
    ),
)
def test_validated_relationship_rejects_invalid_construction(overrides: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "mechanism": MECHANISM,
        "source": _token_enabler(),
        "target": _wide_payoff(),
        "claim": RELATIONSHIP_CLAIM,
        "evidence": (
            OracleEvidence(card_id=TOKEN_ID, face_index=None, quote=TOKEN_QUOTE),
            OracleEvidence(card_id=WIDE_ID, face_index=None, quote=WIDE_QUOTE),
        ),
        "review": FindingReview(status=FindingStatus.ACCEPTED, reason=None),
        "run_id": RUN_ID,
    }
    values.update(overrides)

    with pytest.raises(SetEnrichmentExtractionError):
        ValidatedRelationship(**values)


def test_relationship_validation_result_rejects_invalid_construction(
    sources: EnrichmentSources,
) -> None:
    result = _parse(_content(_response()), sources)
    relationship = result.relationship
    assert relationship is not None
    assert type(relationship) is ValidatedRelationship

    with pytest.raises(SetEnrichmentExtractionError):
        RelationshipValidationResult(
            outcome=ExtractionOutcome.MALFORMED,
            relationship=relationship,
            rejected=None,
            malformed_reason=MALFORMED_REASON,
        )
    with pytest.raises(SetEnrichmentExtractionError):
        RelationshipValidationResult(
            outcome=ExtractionOutcome.MALFORMED,
            relationship=None,
            rejected=None,
            malformed_reason="a different reason",
        )
    with pytest.raises(SetEnrichmentExtractionError):
        RelationshipValidationResult(
            outcome=ExtractionOutcome.SUCCESS,
            relationship=None,
            rejected=None,
            malformed_reason=None,
        )
    with pytest.raises(SetEnrichmentExtractionError):
        RelationshipValidationResult(
            outcome=ExtractionOutcome.SUCCESS,
            relationship=relationship,
            rejected=RejectedFinding(
                finding_id=relationship.finding_id,
                source_kind="oracle",
                summary=RELATIONSHIP_CLAIM,
                reason=MODEL_REJECTION_REASON,
                run_id=RUN_ID,
            ),
            malformed_reason=None,
        )


PREREQUISITE_INCOMPLETE_MESSAGE = "relationship prerequisites are incomplete."
PREREQUISITE_CONTRADICTION_MESSAGE = (
    "relationship prerequisites contradict their source evidence."
)

RECORD_SOURCE_CARD_ID = 301
RECORD_TARGET_CARD_ID = 11
RECORD_SOURCE_CARD_NAME = "Record Token Enabler"
RECORD_TARGET_CARD_NAME = "Record Wide Payoff"
RECORD_SOURCE_CAPABILITY_ID = "capability-record-tokens"
RECORD_TARGET_CAPABILITY_ID = "capability-record-anthem"
RECORD_TOKEN_PARAGRAPH = "Create two 1/1 white Soldier creature tokens."
RECORD_REPEATED_PARAGRAPH = (
    "Whenever you create a creature token, create a 1/1 white Soldier creature token."
)
RECORD_PAIR_PARAGRAPH = (
    "Create a 1/1 white Soldier creature token and a 2/2 red Goblin creature token."
)
RECORD_COST_PARAGRAPH = "Sacrifice a creature: Draw a card."
RECORD_COPY_PARAGRAPH = "Create a token that's a copy of Record Wide Payoff."
RECORD_SOURCE_TEXT = "\n".join(
    (
        RECORD_TOKEN_PARAGRAPH,
        RECORD_REPEATED_PARAGRAPH,
        RECORD_PAIR_PARAGRAPH,
        RECORD_COST_PARAGRAPH,
        RECORD_COPY_PARAGRAPH,
    )
)
RECORD_TARGET_TEXT = "Creatures you control get +1/+1."
RECORD_SOURCE_SHA256 = hashlib.sha256(RECORD_SOURCE_TEXT.encode("utf-8")).hexdigest()
RECORD_TARGET_SHA256 = hashlib.sha256(RECORD_TARGET_TEXT.encode("utf-8")).hexdigest()


def _record_timing(
    *,
    window: Any = "unrestricted",
    turn: Any = "any",
    max_per_turn: Any = None,
) -> RelationshipTiming:
    """Build one relationship timing record."""
    return RelationshipTiming(window=window, turn=turn, max_per_turn=max_per_turn)


def _record_zone(
    *,
    zone: CapabilityZone = CapabilityZone.BATTLEFIELD,
    player: Any = "you",
) -> RelationshipZone:
    """Build one relationship zone record."""
    return RelationshipZone(zone=zone, player=player)


def _record_prerequisite(**overrides: Any) -> RelationshipPrerequisite:
    """Build one complete source-side output clause."""
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.CONDITION,
        "subject": "output",
        "operation": "create",
        "object_kind": "token",
        "card_types": ("creature",),
        "type_operator": "all_of",
        "token_restriction": "token",
        "exclusion": "none",
        "subtype": "Soldier",
        "color_operator": "exact",
        "colors": ("W",),
        "controller": "you",
        "owner": "not_applicable",
        "quantity": TOKEN_QUANTITY,
        "source_zone": None,
        "destination_zone": _record_zone(),
        "timing": _record_timing(),
        "required_card_id": None,
        "evidence": OracleEvidence(
            card_id=RECORD_SOURCE_CARD_ID,
            face_index=None,
            quote=RECORD_TOKEN_PARAGRAPH,
        ),
        "operation_quote": "Create",
        "operation_occurrence": 0,
        "object_quote": "two 1/1 white Soldier creature tokens",
        "object_occurrence": 0,
        "capability_prerequisite_indices": (),
    }
    values.update(overrides)
    return RelationshipPrerequisite(**values)


def _record_target_prerequisite(**overrides: Any) -> RelationshipPrerequisite:
    """Build one complete target-side condition clause."""
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.CONDITION,
        "subject": "participant",
        "operation": "control",
        "object_kind": "permanent",
        "card_types": ("creature",),
        "type_operator": "all_of",
        "token_restriction": "unrestricted",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": (),
        "controller": "you",
        "owner": "not_applicable",
        "quantity": None,
        "source_zone": None,
        "destination_zone": None,
        "timing": _record_timing(),
        "required_card_id": None,
        "evidence": OracleEvidence(
            card_id=RECORD_TARGET_CARD_ID,
            face_index=None,
            quote=RECORD_TARGET_TEXT,
        ),
        "operation_quote": "control",
        "operation_occurrence": 0,
        "object_quote": "Creatures you control",
        "object_occurrence": 0,
        "capability_prerequisite_indices": (),
    }
    values.update(overrides)
    return RelationshipPrerequisite(**values)


def _record_cost_prerequisite(**overrides: Any) -> RelationshipPrerequisite:
    """Build one complete sacrifice cost clause bound to a frozen prerequisite."""
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.COST,
        "subject": "input",
        "operation": "sacrifice",
        "object_kind": "permanent",
        "card_types": ("creature",),
        "type_operator": "all_of",
        "token_restriction": "unrestricted",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": (),
        "controller": "you",
        "owner": "not_applicable",
        "quantity": CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
        "source_zone": None,
        "destination_zone": _record_zone(zone=CapabilityZone.GRAVEYARD, player="owner"),
        "timing": _record_timing(),
        "required_card_id": None,
        "evidence": OracleEvidence(
            card_id=RECORD_SOURCE_CARD_ID,
            face_index=None,
            quote=RECORD_COST_PARAGRAPH,
        ),
        "operation_quote": "Sacrifice",
        "operation_occurrence": 0,
        "object_quote": "a creature",
        "object_occurrence": 0,
        "capability_prerequisite_indices": (0,),
    }
    values.update(overrides)
    return RelationshipPrerequisite(**values)


def _record_copy_prerequisite(**overrides: Any) -> RelationshipPrerequisite:
    """Build one complete token-copy clause naming the other record participant."""
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.CONDITION,
        "subject": "output",
        "operation": "create",
        "object_kind": "token",
        "card_types": (),
        "type_operator": "unrestricted",
        "token_restriction": "token",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": (),
        "controller": "you",
        "owner": "not_applicable",
        "quantity": CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
        "source_zone": None,
        "destination_zone": _record_zone(),
        "timing": _record_timing(),
        "required_card_id": RECORD_TARGET_CARD_ID,
        "evidence": OracleEvidence(
            card_id=RECORD_SOURCE_CARD_ID,
            face_index=None,
            quote=RECORD_COPY_PARAGRAPH,
        ),
        "operation_quote": "Create",
        "operation_occurrence": 0,
        "object_quote": "a token that's a copy of Record Wide Payoff",
        "object_occurrence": 0,
        "capability_prerequisite_indices": (),
    }
    values.update(overrides)
    return RelationshipPrerequisite(**values)


def _record_capability_prerequisite(**overrides: Any) -> CapabilityPrerequisite:
    """Build one frozen capability prerequisite covering the cost paragraph."""
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.COST,
        "quantity": None,
        "timing": None,
        "source_zone": None,
        "destination_zone": None,
        "evidence": OracleEvidence(
            card_id=RECORD_SOURCE_CARD_ID,
            face_index=None,
            quote=RECORD_COST_PARAGRAPH,
        ),
    }
    values.update(overrides)
    return CapabilityPrerequisite(**values)


def _record_participant(**overrides: Any) -> RelationshipParticipant:
    """Build the source participant of the record fixture pair."""
    values: dict[str, Any] = {
        "card_id": RECORD_SOURCE_CARD_ID,
        "capability_id": RECORD_SOURCE_CAPABILITY_ID,
        "card_name": RECORD_SOURCE_CARD_NAME,
        "face_index": None,
        "face_name": None,
        "card_source_sha256": RECORD_SOURCE_SHA256,
        "role": Role.TOKEN_MAKER,
        "capability_prerequisites": (),
        "prerequisites": (_record_prerequisite(),),
    }
    values.update(overrides)
    return RelationshipParticipant(**values)


def _record_target_participant(**overrides: Any) -> RelationshipParticipant:
    """Build the target participant of the record fixture pair."""
    values: dict[str, Any] = {
        "card_id": RECORD_TARGET_CARD_ID,
        "capability_id": RECORD_TARGET_CAPABILITY_ID,
        "card_name": RECORD_TARGET_CARD_NAME,
        "face_index": None,
        "face_name": None,
        "card_source_sha256": RECORD_TARGET_SHA256,
        "role": Role.GO_WIDE_PAYOFF,
        "capability_prerequisites": (),
        "prerequisites": (_record_target_prerequisite(),),
    }
    values.update(overrides)
    return RelationshipParticipant(**values)


def _record_projection(**overrides: Any) -> RelationshipPrerequisiteProjection:
    """Build one complete typed prerequisite projection."""
    values: dict[str, Any] = {
        "source": _record_participant(),
        "target": _record_target_participant(),
    }
    values.update(overrides)
    return RelationshipPrerequisiteProjection(**values)


def _record_pair_prerequisites() -> tuple[RelationshipPrerequisite, RelationshipPrerequisite]:
    """Build one white Soldier and one red Goblin token clause of a single ability."""
    soldier = replace(
        _record_prerequisite(),
        evidence=OracleEvidence(
            card_id=RECORD_SOURCE_CARD_ID,
            face_index=None,
            quote=RECORD_PAIR_PARAGRAPH,
        ),
        quantity=CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
        object_quote="a 1/1 white Soldier creature token",
    )
    goblin = replace(
        soldier,
        colors=("R",),
        subtype="Goblin",
        object_quote="a 2/2 red Goblin creature token",
    )
    return soldier, goblin


def _record_pair_participant() -> RelationshipParticipant:
    """Build the source participant that creates both separate token objects."""
    return replace(_record_participant(), prerequisites=_record_pair_prerequisites())


def _record_relationship(**overrides: Any) -> CardRelationship:
    """Build one accepted relationship carrying the record projection."""
    values: dict[str, Any] = {
        "finding_id": (
            f"relationship:{MECHANISM}:{RECORD_SOURCE_CARD_ID}:{RECORD_SOURCE_CAPABILITY_ID}:"
            f"{RECORD_TARGET_CARD_ID}:{RECORD_TARGET_CAPABILITY_ID}"
        ),
        "mechanism": MECHANISM,
        "participants": (RECORD_SOURCE_CARD_ID, RECORD_TARGET_CARD_ID),
        "claim": RELATIONSHIP_CLAIM,
        "prerequisites": ("The tokens this card creates make the payoff creature larger.",),
        "oracle_evidence": (
            OracleEvidence(
                card_id=RECORD_SOURCE_CARD_ID,
                face_index=None,
                quote=RECORD_TOKEN_PARAGRAPH,
            ),
            OracleEvidence(
                card_id=RECORD_TARGET_CARD_ID,
                face_index=None,
                quote=RECORD_TARGET_TEXT,
            ),
        ),
        "guide_evidence": (),
        "review": FindingReview(status=FindingStatus.ACCEPTED, reason=None),
        "run_id": RUN_ID,
        "prerequisite_projection": _record_projection(),
    }
    values.update(overrides)
    return CardRelationship(**values)


def _record_pins() -> dict[int, CardSourcePin]:
    """Build source pins matching both record participants."""
    return {
        RECORD_SOURCE_CARD_ID: CardSourcePin(
            card_id=RECORD_SOURCE_CARD_ID,
            oracle_id=None,
            collector_number=None,
            sha256=RECORD_SOURCE_SHA256,
        ),
        RECORD_TARGET_CARD_ID: CardSourcePin(
            card_id=RECORD_TARGET_CARD_ID,
            oracle_id=None,
            collector_number=None,
            sha256=RECORD_TARGET_SHA256,
        ),
    }


def _relationship_json() -> dict[str, Any]:
    """Return a fresh JSON payload for the record relationship."""
    return _record_relationship().to_json()


def _relationship_json_without_clause_key(key: str) -> dict[str, Any]:
    """Return a relationship payload whose source clause drops one key."""
    payload = _relationship_json()
    del payload["prerequisite_projection"]["source"]["prerequisites"][0][key]
    return payload


def test_prerequisite_record_json_objects_have_the_pinned_key_sets() -> None:
    clause = _record_prerequisite()
    participant = _record_participant()
    projection = _record_projection()
    relationship = _record_relationship()

    assert set(clause.to_json()) == {
        "capability_prerequisite_indices",
        "card_types",
        "color_operator",
        "colors",
        "controller",
        "destination_zone",
        "evidence",
        "exclusion",
        "kind",
        "object_kind",
        "object_occurrence",
        "object_quote",
        "operation",
        "operation_occurrence",
        "operation_quote",
        "owner",
        "quantity",
        "required_card_id",
        "source_zone",
        "subject",
        "subtype",
        "timing",
        "token_restriction",
        "type_operator",
    }
    assert set(participant.to_json()) == {
        "card_id",
        "capability_id",
        "card_name",
        "face_index",
        "face_name",
        "card_source_sha256",
        "role",
        "capability_prerequisites",
        "prerequisites",
    }
    assert set(projection.to_json()) == {"schema_version", "source", "target"}
    assert set(_record_timing().to_json()) == {"window", "turn", "max_per_turn"}
    assert set(_record_zone().to_json()) == {"zone", "player"}
    assert set(relationship.to_json()) == {
        "finding_id",
        "mechanism",
        "participants",
        "claim",
        "prerequisites",
        "oracle_evidence",
        "guide_evidence",
        "review",
        "run_id",
        "prerequisite_projection",
    }
    assert relationship.to_json()["prerequisite_projection"] == projection.to_json()


def test_prerequisite_record_json_round_trips_preserve_every_field() -> None:
    timing = _record_timing(max_per_turn=1)
    zone = _record_zone()
    clause = _record_prerequisite()
    participant = _record_participant()
    projection = _record_projection()
    relationship = _record_relationship()

    assert RelationshipTiming.from_json(timing.to_json()) == timing
    assert RelationshipZone.from_json(zone.to_json()) == zone
    assert RelationshipPrerequisite.from_json(clause.to_json()) == clause
    assert RelationshipParticipant.from_json(participant.to_json()) == participant
    assert RelationshipPrerequisiteProjection.from_json(projection.to_json()) == projection
    assert CardRelationship.from_json(relationship.to_json()) == relationship

    payload = clause.to_json()
    assert payload["colors"] == ["W"]
    assert payload["card_types"] == ["creature"]
    assert payload["quantity"] == {"value": 2, "relation": "exactly"}
    assert payload["timing"] == {"window": "unrestricted", "turn": "any", "max_per_turn": None}
    assert payload["destination_zone"] == {"zone": "battlefield", "player": "you"}
    assert payload["evidence"] == {
        "card_id": RECORD_SOURCE_CARD_ID,
        "face_index": None,
        "quote": RECORD_TOKEN_PARAGRAPH,
    }


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param({"kind": "condition"}, id="raw-kind-string"),
        pytest.param({"subject": "recipient"}, id="unsupported-subject"),
        pytest.param({"operation": "vanish"}, id="unsupported-operation"),
        pytest.param({"object_kind": "creature"}, id="unsupported-object-kind"),
        pytest.param({"type_operator": "exact"}, id="unsupported-type-operator"),
        pytest.param({"card_types": ("Creature",)}, id="non-lowercase-card-type"),
        pytest.param({"card_types": ["creature"]}, id="card-types-not-a-tuple"),
        pytest.param({"token_restriction": "sometimes"}, id="unsupported-token-restriction"),
        pytest.param({"exclusion": "self"}, id="unsupported-exclusion"),
        pytest.param({"subtype": "   "}, id="blank-subtype"),
        pytest.param({"color_operator": "always"}, id="unsupported-color-operator"),
        pytest.param({"colors": ("Z",)}, id="unsupported-color"),
        pytest.param({"controller": "controller"}, id="unsupported-controller"),
        pytest.param({"owner": "controller"}, id="unsupported-owner"),
        pytest.param({"quantity": "two"}, id="quantity-not-a-record"),
        pytest.param(
            {"source_zone": CapabilityZone.HAND},
            id="source-zone-is-not-a-relationship-zone",
        ),
        pytest.param({"destination_zone": "battlefield"}, id="destination-zone-not-a-record"),
        pytest.param({"timing": "upkeep"}, id="timing-not-a-record"),
        pytest.param({"required_card_id": 0}, id="non-positive-required-card"),
        pytest.param({"required_card_id": "301"}, id="required-card-not-an-integer"),
        pytest.param(
            {
                "evidence": {
                    "card_id": RECORD_SOURCE_CARD_ID,
                    "face_index": None,
                    "quote": RECORD_TOKEN_PARAGRAPH,
                }
            },
            id="evidence-not-a-record",
        ),
        pytest.param({"operation_quote": "   "}, id="blank-operation-quote"),
        pytest.param({"object_quote": ""}, id="empty-object-quote"),
    ),
)
def test_prerequisite_record_rejects_closed_values_and_bad_field_types(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(SemanticEnrichmentError) as error:
        replace(_record_prerequisite(), **overrides)

    assert type(error.value) is SemanticEnrichmentError


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param({"required_card_id": True}, id="boolean-required-card-id"),
        pytest.param({"operation_occurrence": True}, id="boolean-operation-occurrence"),
        pytest.param({"object_occurrence": True}, id="boolean-object-occurrence"),
        pytest.param({"operation_occurrence": -1}, id="negative-operation-occurrence"),
        pytest.param({"object_occurrence": -1}, id="negative-object-occurrence"),
        pytest.param({"capability_prerequisite_indices": (True,)}, id="boolean-index"),
        pytest.param({"capability_prerequisite_indices": (-1,)}, id="negative-index"),
        pytest.param({"capability_prerequisite_indices": (0, 0)}, id="duplicate-index"),
        pytest.param({"capability_prerequisite_indices": [0]}, id="indices-not-a-tuple"),
    ),
)
def test_prerequisite_record_rejects_boolean_and_negative_numbers(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(SemanticEnrichmentError) as error:
        replace(_record_prerequisite(), **overrides)

    assert type(error.value) is SemanticEnrichmentError


def test_prerequisite_record_timing_requires_a_positive_max_per_turn() -> None:
    assert _record_timing().max_per_turn is None
    assert _record_timing(max_per_turn=1).max_per_turn == 1

    for invalid in (True, 0, -1):
        with pytest.raises(SemanticEnrichmentError) as error:
            _record_timing(max_per_turn=invalid)
        assert type(error.value) is SemanticEnrichmentError


def test_prerequisite_record_canonicalizes_colors_card_types_and_indices() -> None:
    colors = replace(_record_prerequisite(), color_operator="any_of", colors=("G", "W", "U"))
    exact_colors = replace(_record_prerequisite(), color_operator="exact", colors=("U", "W"))
    card_types = replace(
        _record_prerequisite(),
        type_operator="any_of",
        card_types=("sorcery", "creature", "artifact"),
    )
    indices = replace(_record_prerequisite(), capability_prerequisite_indices=(2, 0, 1))

    assert colors.colors == ("W", "U", "G")
    assert exact_colors.color_operator == "exact"
    assert exact_colors.colors == ("W", "U")
    assert card_types.card_types == ("artifact", "creature", "sorcery")
    assert indices.capability_prerequisite_indices == (0, 1, 2)

    with pytest.raises(SemanticEnrichmentError):
        replace(_record_prerequisite(), color_operator="any_of", colors=("W", "W"))
    with pytest.raises(SemanticEnrichmentError):
        replace(
            _record_prerequisite(),
            type_operator="any_of",
            card_types=("creature", "creature"),
        )


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param(
            {"type_operator": "unrestricted", "card_types": ("creature",)},
            id="unrestricted-with-card-types",
        ),
        pytest.param({"type_operator": "any_of", "card_types": ()}, id="any-of-without-types"),
        pytest.param({"type_operator": "all_of", "card_types": ()}, id="all-of-without-types"),
        pytest.param(
            {"object_kind": "token", "token_restriction": "unrestricted"},
            id="token-with-unrestricted-restriction",
        ),
        pytest.param(
            {"object_kind": "token", "token_restriction": "nontoken"},
            id="token-with-nontoken-restriction",
        ),
        pytest.param(
            {"color_operator": "unrestricted", "colors": ("W",)},
            id="unrestricted-colors-with-values",
        ),
        pytest.param(
            {"color_operator": "any_of", "colors": ()},
            id="any-of-colors-without-values",
        ),
        pytest.param(
            {"color_operator": "all_of", "colors": ()},
            id="all-of-colors-without-values",
        ),
    ),
)
def test_prerequisite_record_enforces_closed_field_invariants(overrides: dict[str, Any]) -> None:
    with pytest.raises(SemanticEnrichmentError) as error:
        replace(_record_prerequisite(), **overrides)

    assert type(error.value) is SemanticEnrichmentError


def test_prerequisite_record_accepts_unrestricted_and_colorless_clauses() -> None:
    unrestricted = replace(
        _record_prerequisite(),
        object_kind="permanent",
        token_restriction="unrestricted",
        type_operator="unrestricted",
        card_types=(),
        color_operator="unrestricted",
        colors=(),
    )
    colorless = replace(_record_prerequisite(), color_operator="exact", colors=())

    assert unrestricted.card_types == ()
    assert unrestricted.colors == ()
    assert colorless.colors == ()


def test_prerequisite_record_requires_condition_kind_for_the_none_operation() -> None:
    descriptive = replace(
        _record_prerequisite(),
        kind=PrerequisiteKind.CONDITION,
        operation="none",
        operation_quote="Create",
    )

    assert descriptive.operation == "none"

    for kind in (PrerequisiteKind.COST, PrerequisiteKind.TRIGGER, PrerequisiteKind.THRESHOLD):
        with pytest.raises(SemanticEnrichmentError) as error:
            replace(_record_prerequisite(), kind=kind, operation="none")
        assert type(error.value) is SemanticEnrichmentError


def test_prerequisite_record_stores_subtypes_casefolded() -> None:
    assert replace(_record_prerequisite(), subtype="SOLDIER").subtype == "soldier"
    assert replace(_record_prerequisite(), subtype="soldier").subtype == "soldier"
    assert _record_prerequisite().to_json()["subtype"] == "soldier"


def test_prerequisite_record_rejects_unknown_projection_versions() -> None:
    assert RELATIONSHIP_PREREQUISITE_PROJECTION_SCHEMA_VERSION == 1
    assert _record_projection().schema_version == 1
    assert _record_projection().to_json()["schema_version"] == 1

    for version in (0, 2, 3):
        with pytest.raises(SemanticEnrichmentError):
            _record_projection(schema_version=version)
        with pytest.raises(SemanticEnrichmentError):
            RelationshipPrerequisiteProjection.from_json(
                {**_record_projection().to_json(), "schema_version": version}
            )


@pytest.mark.parametrize(
    ("reader", "payload"),
    (
        pytest.param(
            RelationshipPrerequisite.from_json,
            {**_record_prerequisite().to_json(), "score": 0.9},
            id="clause-extra-key",
        ),
        pytest.param(
            RelationshipPrerequisite.from_json,
            {
                key: value
                for key, value in _record_prerequisite().to_json().items()
                if key != "kind"
            },
            id="clause-missing-key",
        ),
        pytest.param(
            RelationshipParticipant.from_json,
            {
                key: value
                for key, value in _record_participant().to_json().items()
                if key != "role"
            },
            id="participant-missing-key",
        ),
        pytest.param(
            RelationshipParticipant.from_json,
            {**_record_participant().to_json(), "prerequisites": []},
            id="participant-without-clauses",
        ),
        pytest.param(
            RelationshipPrerequisiteProjection.from_json,
            {
                key: value
                for key, value in _record_projection().to_json().items()
                if key != "target"
            },
            id="projection-missing-key",
        ),
        pytest.param(
            RelationshipPrerequisiteProjection.from_json,
            {**_record_projection().to_json(), "score": 1},
            id="projection-extra-key",
        ),
        pytest.param(
            RelationshipPrerequisiteProjection.from_json,
            {**_record_projection().to_json(), "source": None},
            id="projection-null-participant",
        ),
        pytest.param(
            CardRelationship.from_json,
            {**_relationship_json(), "adjustment": 1},
            id="relationship-extra-key",
        ),
        pytest.param(
            CardRelationship.from_json,
            {**_relationship_json(), "prerequisite_projection": None},
            id="relationship-explicit-null-projection",
        ),
        pytest.param(
            CardRelationship.from_json,
            {**_relationship_json(), "prerequisite_projection": "complete"},
            id="relationship-projection-not-an-object",
        ),
        pytest.param(
            CardRelationship.from_json,
            _relationship_json_without_clause_key("kind"),
            id="relationship-clause-missing-key",
        ),
        pytest.param(
            CardRelationship.from_json,
            _relationship_json_without_clause_key("capability_prerequisite_indices"),
            id="relationship-clause-missing-indices",
        ),
    ),
)
def test_prerequisite_record_json_readers_reject_malformed_documents(
    reader: Callable[[dict[str, Any]], object],
    payload: dict[str, Any],
) -> None:
    with pytest.raises(SemanticEnrichmentError):
        reader(payload)


def test_prerequisite_record_keeps_capability_prerequisites_in_frozen_order() -> None:
    trigger = _record_capability_prerequisite(kind=PrerequisiteKind.TRIGGER)
    cost = _record_capability_prerequisite()
    participant = replace(_record_participant(), capability_prerequisites=(trigger, cost))

    assert participant.capability_prerequisites == (trigger, cost)
    assert participant.to_json()["capability_prerequisites"] == [
        trigger.to_json(),
        cost.to_json(),
    ]
    assert RelationshipParticipant.from_json(participant.to_json()).capability_prerequisites == (
        trigger,
        cost,
    )


def test_prerequisite_record_participant_requires_at_least_one_clause() -> None:
    with pytest.raises(SemanticEnrichmentError) as error:
        replace(_record_participant(), prerequisites=())

    assert type(error.value) is SemanticEnrichmentError


def test_prerequisite_record_resolves_clause_selectors_case_sensitively() -> None:
    validate_prerequisite_projection(projection=_record_projection())

    with pytest.raises(PrerequisiteProjectionError) as error:
        _record_projection(
            source=replace(
                _record_participant(),
                prerequisites=(replace(_record_prerequisite(), operation_quote="create"),),
            )
        )

    assert error.value.code == "contradiction"
    assert str(error.value) == PREREQUISITE_CONTRADICTION_MESSAGE


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param({"operation_occurrence": 1}, id="operation-occurrence-out-of-range"),
        pytest.param({"object_occurrence": 1}, id="object-occurrence-out-of-range"),
        pytest.param(
            {"object_quote": "two Soldier creature tokens"},
            id="absent-object-selector",
        ),
    ),
)
def test_prerequisite_record_rejects_unresolvable_clause_selectors(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(PrerequisiteProjectionError) as error:
        _record_projection(
            source=replace(
                _record_participant(),
                prerequisites=(replace(_record_prerequisite(), **overrides),),
            )
        )

    assert error.value.code == "contradiction"
    assert str(error.value) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_prerequisite_record_second_occurrence_selects_the_later_span() -> None:
    repeated = replace(
        _record_prerequisite(),
        evidence=OracleEvidence(
            card_id=RECORD_SOURCE_CARD_ID,
            face_index=None,
            quote=RECORD_REPEATED_PARAGRAPH,
        ),
        quantity=CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
        operation_quote="create",
        operation_occurrence=1,
        object_quote="a 1/1 white Soldier creature token",
        object_occurrence=0,
    )

    assert RECORD_REPEATED_PARAGRAPH.count("create") == 2

    validate_prerequisite_projection(
        projection=_record_projection(
            source=replace(_record_participant(), prerequisites=(repeated,)),
        )
    )


def test_prerequisite_record_rejects_duplicate_and_colliding_clauses() -> None:
    clause = _record_prerequisite()

    with pytest.raises(SemanticEnrichmentError):
        replace(_record_participant(), prerequisites=(clause, clause))

    # One selected object cannot carry two partial clauses: the split predicates collide on the
    # same binding even though each clause agrees with the quoted phrase on its own.
    with pytest.raises(PrerequisiteProjectionError) as collision_error:
        _record_projection(
            source=replace(
                _record_participant(),
                prerequisites=(
                    clause,
                    replace(clause, timing=_record_timing(window="upkeep", turn="your")),
                ),
            )
        )

    assert collision_error.value.code == "incomplete"
    assert str(collision_error.value) == PREREQUISITE_INCOMPLETE_MESSAGE

    # A nested selection of the same object is an ambiguous binding, never a second object.
    with pytest.raises(PrerequisiteProjectionError) as overlap_error:
        _record_projection(
            source=replace(
                _record_participant(),
                prerequisites=(
                    clause,
                    replace(clause, object_quote="1/1 white Soldier creature tokens"),
                ),
            )
        )

    assert overlap_error.value.code == "incomplete"


def test_prerequisite_record_keeps_distinct_object_bindings_separate() -> None:
    first, second = _record_pair_prerequisites()
    participant = _record_pair_participant()

    assert participant.prerequisites == (second, first)
    assert set(participant.prerequisites) == {first, second}

    validate_prerequisite_projection(projection=_record_projection(source=participant))


def test_prerequisite_record_indices_track_the_frozen_capability_prerequisites() -> None:
    cost = _record_cost_prerequisite()
    frozen = _record_capability_prerequisite()
    participant = replace(
        _record_participant(),
        capability_prerequisites=(frozen,),
        prerequisites=(cost, _record_prerequisite()),
    )

    validate_prerequisite_projection(projection=_record_projection(source=participant))

    with pytest.raises(PrerequisiteProjectionError) as kind_error:
        _record_projection(
            source=replace(
                participant,
                prerequisites=(
                    replace(cost, kind=PrerequisiteKind.CONDITION),
                    _record_prerequisite(),
                ),
            )
        )

    assert kind_error.value.code == "contradiction"

    with pytest.raises(PrerequisiteProjectionError) as repeated_error:
        _record_projection(
            source=replace(
                participant,
                prerequisites=(
                    cost,
                    replace(_record_prerequisite(), capability_prerequisite_indices=(0,)),
                ),
            )
        )

    assert repeated_error.value.code == "incomplete"

    with pytest.raises(PrerequisiteProjectionError) as unreferenced_error:
        _record_projection(
            source=replace(
                participant,
                prerequisites=(
                    replace(cost, capability_prerequisite_indices=()),
                    _record_prerequisite(),
                ),
            )
        )

    assert unreferenced_error.value.code == "incomplete"

    with pytest.raises(PrerequisiteProjectionError) as unknown_index_error:
        _record_projection(source=replace(_record_participant(), prerequisites=(cost,)))

    assert unknown_index_error.value.code == "contradiction"


def test_prerequisite_record_direction_identity_survives_canonical_participants() -> None:
    relationship = _record_relationship()
    legacy = replace(relationship, prerequisite_projection=None)

    assert relationship.participants == (RECORD_TARGET_CARD_ID, RECORD_SOURCE_CARD_ID)
    assert relationship.identity == (
        MECHANISM,
        (RECORD_TARGET_CARD_ID, RECORD_SOURCE_CARD_ID),
        (
            RECORD_SOURCE_CARD_ID,
            RECORD_SOURCE_CAPABILITY_ID,
            -1,
            RECORD_TARGET_CARD_ID,
            RECORD_TARGET_CAPABILITY_ID,
            -1,
        ),
    )
    assert legacy.identity == (MECHANISM, (RECORD_TARGET_CARD_ID, RECORD_SOURCE_CARD_ID), ())
    assert replace(relationship, finding_id="relationship:other").identity == relationship.identity
    assert replace(
        relationship,
        participants=(RECORD_SOURCE_CARD_ID, RECORD_TARGET_CARD_ID),
    ).participants == (RECORD_TARGET_CARD_ID, RECORD_SOURCE_CARD_ID)

    reversed_relationship = replace(
        relationship,
        prerequisite_projection=_record_projection(
            source=_record_target_participant(),
            target=_record_participant(),
        ),
    )

    assert reversed_relationship.identity != relationship.identity
    assert reversed_relationship.identity[2][0] == RECORD_TARGET_CARD_ID

    faced_clause = replace(
        _record_prerequisite(),
        evidence=OracleEvidence(
            card_id=RECORD_SOURCE_CARD_ID,
            face_index=0,
            quote=RECORD_TOKEN_PARAGRAPH,
        ),
    )
    faced = replace(
        relationship,
        oracle_evidence=(
            OracleEvidence(
                card_id=RECORD_SOURCE_CARD_ID,
                face_index=0,
                quote=RECORD_TOKEN_PARAGRAPH,
            ),
            OracleEvidence(
                card_id=RECORD_TARGET_CARD_ID,
                face_index=None,
                quote=RECORD_TARGET_TEXT,
            ),
        ),
        prerequisite_projection=_record_projection(
            source=replace(
                _record_participant(),
                face_index=0,
                prerequisites=(faced_clause,),
            ),
        ),
    )

    assert faced.identity[2][2] == 0
    assert faced.identity != relationship.identity
    assert relationship.identity[2][2] == -1


def test_prerequisite_record_relationship_projection_requires_accepted_participants() -> None:
    relationship = _record_relationship()

    assert relationship.review.status is FindingStatus.ACCEPTED
    assert relationship.prerequisite_projection == _record_projection()

    with pytest.raises(SemanticEnrichmentError) as review_error:
        replace(
            relationship,
            review=FindingReview(
                status=FindingStatus.UNCERTAIN,
                reason=MODEL_UNCERTAINTY_REASON,
            ),
        )

    assert type(review_error.value) is SemanticEnrichmentError

    foreign_target = replace(
        _record_target_participant(),
        card_id=FOREIGN_CARD_ID,
        card_name="Foreign Payoff",
        prerequisites=(
            replace(
                _record_target_prerequisite(),
                evidence=OracleEvidence(
                    card_id=FOREIGN_CARD_ID,
                    face_index=None,
                    quote=RECORD_TARGET_TEXT,
                ),
            ),
        ),
    )

    with pytest.raises(SemanticEnrichmentError) as participant_error:
        replace(
            relationship,
            prerequisite_projection=_record_projection(target=foreign_target),
        )

    assert type(participant_error.value) is SemanticEnrichmentError


def test_prerequisite_record_clause_evidence_must_join_the_relationship_evidence() -> None:
    relationship = _record_relationship()

    with pytest.raises(SemanticEnrichmentError) as error:
        replace(
            relationship,
            oracle_evidence=(
                OracleEvidence(
                    card_id=RECORD_TARGET_CARD_ID,
                    face_index=None,
                    quote=RECORD_TARGET_TEXT,
                ),
            ),
        )

    assert type(error.value) is SemanticEnrichmentError


def test_prerequisite_record_clause_evidence_ownership_follows_its_participant() -> None:
    oracle_text = {
        (RECORD_SOURCE_CARD_ID, None): RECORD_SOURCE_TEXT,
        (RECORD_TARGET_CARD_ID, None): RECORD_TARGET_TEXT,
    }

    with pytest.raises(PrerequisiteProjectionError) as error:
        validate_prerequisite_sources(
            projection=_record_projection(
                source=replace(
                    _record_participant(),
                    prerequisites=(_record_target_prerequisite(),),
                ),
            ),
            oracle_text=oracle_text,
        )

    assert error.value.code == "contradiction"
    assert str(error.value) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_prerequisite_record_pins_cover_participants_and_required_cards() -> None:
    relationship = _record_relationship()
    pins = _record_pins()
    copy_evidence = OracleEvidence(
        card_id=RECORD_SOURCE_CARD_ID,
        face_index=None,
        quote=RECORD_COPY_PARAGRAPH,
    )

    validate_relationship_pins(relationship=relationship, pins=pins)

    eligible = replace(
        relationship,
        oracle_evidence=(*relationship.oracle_evidence, copy_evidence),
        prerequisite_projection=_record_projection(
            source=replace(
                _record_participant(),
                prerequisites=(_record_prerequisite(), _record_copy_prerequisite()),
            ),
        ),
    )
    validate_relationship_pins(relationship=eligible, pins=pins)

    with pytest.raises(SemanticEnrichmentError) as missing_pin_error:
        validate_relationship_pins(
            relationship=relationship,
            pins={RECORD_TARGET_CARD_ID: pins[RECORD_TARGET_CARD_ID]},
        )

    assert type(missing_pin_error.value) is SemanticEnrichmentError

    mismatched = replace(
        relationship,
        prerequisite_projection=_record_projection(
            source=replace(_record_participant(), card_source_sha256="0" * 64),
        ),
    )
    with pytest.raises(SemanticEnrichmentError) as hash_error:
        validate_relationship_pins(relationship=mismatched, pins=pins)

    assert type(hash_error.value) is SemanticEnrichmentError

    # A third card identity never reaches pin validation: the projection gate rejects it first.
    with pytest.raises(PrerequisiteProjectionError) as foreign_error:
        _record_projection(
            source=replace(
                _record_participant(),
                prerequisites=(
                    _record_copy_prerequisite(required_card_id=FOREIGN_CARD_ID),
                ),
            ),
        )

    assert foreign_error.value.code == "incomplete"


def test_prerequisite_record_resolves_participants_against_frozen_cards() -> None:
    relationship = _record_relationship()
    pins = _record_pins()
    cards = {
        RECORD_SOURCE_CARD_ID: _card(
            RECORD_SOURCE_CARD_ID,
            RECORD_SOURCE_CARD_NAME,
            RECORD_SOURCE_TEXT,
        ),
        RECORD_TARGET_CARD_ID: _card(
            RECORD_TARGET_CARD_ID,
            RECORD_TARGET_CARD_NAME,
            RECORD_TARGET_TEXT,
        ),
    }

    legacy = replace(relationship, prerequisite_projection=None)
    validate_relationship_sources(relationship=legacy, cards={}, pins={})
    validate_relationship_pins(relationship=legacy, pins={})

    with pytest.raises(SemanticEnrichmentError) as unknown_card_error:
        validate_relationship_sources(
            relationship=relationship,
            cards={RECORD_TARGET_CARD_ID: cards[RECORD_TARGET_CARD_ID]},
            pins=pins,
        )

    assert type(unknown_card_error.value) is SemanticEnrichmentError

    renamed = {
        **cards,
        RECORD_SOURCE_CARD_ID: _card(
            RECORD_SOURCE_CARD_ID,
            "Renamed Enabler",
            RECORD_SOURCE_TEXT,
        ),
    }
    with pytest.raises(SemanticEnrichmentError) as name_error:
        validate_relationship_sources(relationship=relationship, cards=renamed, pins=pins)

    assert type(name_error.value) is SemanticEnrichmentError

    faced_clause = replace(
        _record_prerequisite(),
        evidence=OracleEvidence(
            card_id=RECORD_SOURCE_CARD_ID,
            face_index=1,
            quote=RECORD_TOKEN_PARAGRAPH,
        ),
    )
    faced = replace(
        relationship,
        oracle_evidence=(
            OracleEvidence(
                card_id=RECORD_SOURCE_CARD_ID,
                face_index=1,
                quote=RECORD_TOKEN_PARAGRAPH,
            ),
            OracleEvidence(
                card_id=RECORD_TARGET_CARD_ID,
                face_index=None,
                quote=RECORD_TARGET_TEXT,
            ),
        ),
        prerequisite_projection=_record_projection(
            source=replace(
                _record_participant(),
                face_index=1,
                prerequisites=(faced_clause,),
            ),
        ),
    )
    with pytest.raises(SemanticEnrichmentError) as face_error:
        validate_relationship_sources(relationship=faced, cards=cards, pins=pins)

    assert type(face_error.value) is SemanticEnrichmentError


def test_prerequisite_record_legacy_relationship_json_stays_byte_stable() -> None:
    legacy = replace(_record_relationship(), prerequisite_projection=None)
    payload = legacy.to_json()

    assert "prerequisite_projection" not in payload
    assert set(payload) == {
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

    dumped = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    restored = CardRelationship.from_json(json.loads(dumped))

    assert restored == legacy
    assert restored.prerequisite_projection is None
    assert json.dumps(
        restored.to_json(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) == dumped


def test_prerequisite_record_invalid_projection_never_decodes_as_absent() -> None:
    indexed = _relationship_json()
    indexed["prerequisite_projection"]["source"]["prerequisites"][0][
        "capability_prerequisite_indices"
    ] = [0]

    with pytest.raises(SemanticEnrichmentError):
        CardRelationship.from_json(indexed)

    dropped_evidence = _relationship_json()
    dropped_evidence["oracle_evidence"] = [
        item
        for item in dropped_evidence["oracle_evidence"]
        if item["card_id"] != RECORD_SOURCE_CARD_ID
    ]

    with pytest.raises(SemanticEnrichmentError):
        CardRelationship.from_json(dropped_evidence)

    assert CardRelationship.from_json(_relationship_json()).prerequisite_projection is not None


def _contradict_the_quoted_object_color(payload: dict[str, Any]) -> None:
    """Declare a blue output for the quotation that states a red Goblin token."""
    goblin = next(
        clause for clause in payload["source"]["prerequisites"] if clause["colors"] == ["R"]
    )
    goblin["colors"] = ["U"]


def _mismatch_the_clause_evidence_face(payload: dict[str, Any]) -> None:
    """Declare a face index the participant's clause evidence does not carry."""
    payload["source"]["face_index"] = 0


@pytest.mark.parametrize(
    "mutate",
    (_contradict_the_quoted_object_color, _mismatch_the_clause_evidence_face),
)
def test_prerequisite_record_decoded_projection_must_agree_with_its_quotation(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _record_projection(source=_record_pair_participant()).to_json()
    mutate(payload)

    with pytest.raises(PrerequisiteProjectionError) as projection_error:
        RelationshipPrerequisiteProjection.from_json(payload)

    assert projection_error.value.code == "contradiction"

    with pytest.raises(PrerequisiteProjectionError):
        CardRelationship.from_json(
            {**_relationship_json(), "prerequisite_projection": payload}
        )


RECORD_FODDER_CARD_ID = 401
RECORD_FODDER_CARD_NAME = "Record Fodder"
RECORD_FODDER_SACRIFICE_PARAGRAPH = "Sacrifice this creature: Draw a card."
RECORD_FODDER_RETURN_PARAGRAPH = "Return this card from your graveyard to your hand."
RECORD_FODDER_DIES_PARAGRAPH = "Whenever this creature dies, draw a card."


def _record_self_prerequisite(
    *,
    paragraph: str,
    kind: PrerequisiteKind = PrerequisiteKind.CONDITION,
    operation: str = "sacrifice",
    operation_quote: str | None = None,
    object_kind: str = "permanent",
    card_types: tuple[str, ...] = ("creature",),
    object_quote: str = "this creature",
    source_zone: RelationshipZone | None = None,
    destination_zone: RelationshipZone | None = None,
) -> RelationshipPrerequisite:
    """Build one self-bound clause of a sacrifice-fodder participant."""
    return RelationshipPrerequisite(
        kind=kind,
        subject="participant",
        operation=operation,
        object_kind=object_kind,
        card_types=card_types,
        type_operator="all_of",
        token_restriction="unrestricted",
        exclusion="none",
        subtype=None,
        color_operator="unrestricted",
        colors=(),
        controller="you",
        owner="not_applicable",
        quantity=None,
        source_zone=source_zone,
        destination_zone=destination_zone,
        timing=_record_timing(),
        required_card_id=RECORD_FODDER_CARD_ID,
        evidence=OracleEvidence(
            card_id=RECORD_FODDER_CARD_ID,
            face_index=None,
            quote=paragraph,
        ),
        operation_quote=operation.capitalize() if operation_quote is None else operation_quote,
        operation_occurrence=0,
        object_quote=object_quote,
        object_occurrence=0,
        capability_prerequisite_indices=(),
    )


def _record_fodder_participant(clause: RelationshipPrerequisite) -> RelationshipParticipant:
    """Build one sacrifice-fodder participant carrying a single self-bound clause."""
    return RelationshipParticipant(
        card_id=RECORD_FODDER_CARD_ID,
        capability_id="capability-record-fodder",
        card_name=RECORD_FODDER_CARD_NAME,
        face_index=None,
        face_name=None,
        card_source_sha256=card_source_sha256(
            _card(RECORD_FODDER_CARD_ID, RECORD_FODDER_CARD_NAME, clause.evidence.quote)
        ),
        role=Role.SACRIFICE_FODDER,
        capability_prerequisites=(),
        prerequisites=(clause,),
    )


def test_prerequisite_record_sacrifice_fodder_anchor_requires_a_self_bound_clause() -> None:
    sacrifice = _record_self_prerequisite(
        paragraph=RECORD_FODDER_SACRIFICE_PARAGRAPH,
        destination_zone=_record_zone(zone=CapabilityZone.GRAVEYARD, player="owner"),
    )
    returned = _record_self_prerequisite(
        paragraph=RECORD_FODDER_RETURN_PARAGRAPH,
        operation="return",
        object_kind="card",
        object_quote="this card",
        source_zone=_record_zone(zone=CapabilityZone.GRAVEYARD, player="you"),
        destination_zone=_record_zone(zone=CapabilityZone.HAND, player="you"),
    )
    dies = _record_self_prerequisite(
        paragraph=RECORD_FODDER_DIES_PARAGRAPH,
        kind=PrerequisiteKind.TRIGGER,
        operation="die",
        operation_quote="dies",
        destination_zone=_record_zone(zone=CapabilityZone.GRAVEYARD, player="owner"),
    )

    assert role_anchor_covered(participant=_record_fodder_participant(sacrifice)) is True
    assert role_anchor_covered(participant=_record_fodder_participant(returned)) is True
    assert role_anchor_covered(participant=_record_fodder_participant(dies)) is False

    for clause in (sacrifice, returned):
        validate_prerequisite_projection(
            projection=_record_projection(
                source=_record_fodder_participant(clause),
                target=_record_target_participant(),
            )
        )

    with pytest.raises(PrerequisiteProjectionError) as anchor_error:
        _record_projection(
            source=_record_fodder_participant(dies),
            target=_record_target_participant(),
        )

    assert anchor_error.value.code == "incomplete"


TYPED_SOURCE_ID = 301
TYPED_ANTHEM_ID = 11
TYPED_THRESHOLD_ID = 12
TYPED_TYPAL_ID = 13
TYPED_OUTLET_ID = 14
TYPED_OTHER_OUTLET_ID = 15
TYPED_SELF_OUTLET_ID = 16
TYPED_UPKEEP_ID = 17
TYPED_TIMED_OUTLET_ID = 18
TYPED_DISCARD_ID = 19
TYPED_RECURSION_ID = 20
TYPED_SELF_RETURN_ID = 21
TYPED_TOKEN_COST_ID = 22
TYPED_REPEATED_ID = 23
TYPED_TAPPED_OUTLET_ID = 24
TYPED_ALTERNATE_OUTLET_ID = 25
TYPED_LOOT_ONE_ID = 26
TYPED_LOOT_TWO_ID = 27
TYPED_DEATH_ID = 28
TYPED_CONJUNCTIVE_OUTLET_ID = 29
TYPED_ARTIFACT_CREATURE_ID = 30
TYPED_OR_TYPE_OUTLET_ID = 31
TYPED_PLANESWALKER_RETURN_ID = 32
TYPED_MULTICOLOR_TOKEN_ID = 33
TYPED_OWNED_OUTLET_ID = 34
TYPED_HISTORY_TURN_ID = 35
TYPED_HISTORY_DIED_ID = 36
TYPED_HISTORY_CAST_ID = 37
TYPED_HISTORY_SO_FAR_ID = 38
TYPED_PLAIN_TOKEN_ID = 39

TYPED_TOKEN_PARAGRAPH = "Create two 1/1 white Soldier creature tokens."
TYPED_ANTHEM_PARAGRAPH = "Creatures you control get +1/+1."
TYPED_THRESHOLD_PARAGRAPH = (
    "As long as you control three or more creatures, this creature gets +1/+1."
)
TYPED_TYPAL_PARAGRAPH = "Soldier creatures you control get +1/+1."
TYPED_OUTLET_PARAGRAPH = "Sacrifice a creature: Draw a card."
TYPED_OTHER_OUTLET_PARAGRAPH = "Sacrifice another creature: Draw a card."
TYPED_SELF_OUTLET_PARAGRAPH = "Sacrifice this creature: Draw a card."
TYPED_UPKEEP_PARAGRAPH = (
    "At the beginning of your upkeep, create a 1/1 white Soldier creature token."
)
TYPED_TIMED_OUTLET_PARAGRAPH = (
    "Sacrifice a creature: Draw a card. Activate only during your turn and only once each turn."
)
TYPED_DISCARD_PARAGRAPH = "Discard a creature card."
TYPED_RECURSION_PARAGRAPH = "Return target creature card from your graveyard to your hand."
TYPED_SELF_RETURN_PARAGRAPH = "Return this card from your graveyard to your hand."
TYPED_TOKEN_COST_PARAGRAPH = "Sacrifice a token: Draw a card."
TYPED_DRAW_PARAGRAPH = "Draw a card."
TYPED_REPEATED_SOURCE_TEXT = f"{TYPED_TOKEN_PARAGRAPH}\n{TYPED_DRAW_PARAGRAPH}"
TYPED_TAPPED_OUTLET_PARAGRAPH = "Sacrifice a tapped creature: Draw a card."
TYPED_ALTERNATE_OUTLET_PARAGRAPH = "Sacrifice a creature or an artifact: Draw a card."
TYPED_CONJUNCTIVE_OUTLET_PARAGRAPH = "Sacrifice a creature and an artifact: Draw a card."
TYPED_ARTIFACT_CREATURE_PARAGRAPH = "Sacrifice an artifact creature: Draw a card."
TYPED_OR_TYPE_OUTLET_PARAGRAPH = "Sacrifice an artifact or creature: Draw a card."
TYPED_PLANESWALKER_RETURN_PARAGRAPH = (
    "Return target creature or planeswalker card from your graveyard to your hand."
)
TYPED_MULTICOLOR_TOKEN_PARAGRAPH = "Create a 1/1 white and blue Bird creature token."
TYPED_OWNED_OUTLET_PARAGRAPH = "Sacrifice a creature you own: Draw a card."
TYPED_HISTORY_TURN_PARAGRAPH = (
    "If you cast a spell this turn, create a 1/1 white Soldier creature token."
)
TYPED_HISTORY_DIED_PARAGRAPH = (
    "Create a 1/1 white Soldier creature token if a creature has died this turn."
)
TYPED_HISTORY_CAST_PARAGRAPH = (
    "Create a 1/1 white Soldier creature token if a spell was cast this turn."
)
TYPED_HISTORY_SO_FAR_PARAGRAPH = (
    "Create a 1/1 white Soldier creature token if you've drawn a card so far."
)
TYPED_PLAIN_TOKEN_PARAGRAPH = "Create a 1/1 white Soldier creature token."
TYPED_LOOT_PARAGRAPH = "Draw a card. Discard a card."
TYPED_DEATH_PARAGRAPH = "Whenever one or more other creatures die, scry 1."

SACRIFICE_MECHANISM = "token-sacrifice-outlet"
RECURSION_MECHANISM = "discard-recursion-payoff"
LOOT_MECHANISM = "loot-mirror"

TYPED_CLAIM = "The enabler supplies the payoff with the objects the payoff asks for."
TYPED_SCORING_CLAIM = (
    "The enabler scores 0.9 against the payoff, so weight the pair 5 and adjust the ranking by +2."
)


def _typed_sources() -> EnrichmentSources:
    """Return the frozen card set of the typed prerequisite matrix."""
    return EnrichmentSources(
        set_code=SET_CODE,
        cards=(
            _card(TYPED_SOURCE_ID, "Batch Enabler", TYPED_TOKEN_PARAGRAPH),
            _card(TYPED_ANTHEM_ID, "Batch Anthem", TYPED_ANTHEM_PARAGRAPH),
            _card(TYPED_THRESHOLD_ID, "Batch Threshold", TYPED_THRESHOLD_PARAGRAPH),
            _card(TYPED_TYPAL_ID, "Batch Typal", TYPED_TYPAL_PARAGRAPH),
            _card(TYPED_OUTLET_ID, "Batch Outlet", TYPED_OUTLET_PARAGRAPH),
            _card(TYPED_OTHER_OUTLET_ID, "Batch Other Outlet", TYPED_OTHER_OUTLET_PARAGRAPH),
            _card(TYPED_SELF_OUTLET_ID, "Batch Self Outlet", TYPED_SELF_OUTLET_PARAGRAPH),
            _card(TYPED_UPKEEP_ID, "Batch Upkeep", TYPED_UPKEEP_PARAGRAPH),
            _card(TYPED_TIMED_OUTLET_ID, "Batch Timed Outlet", TYPED_TIMED_OUTLET_PARAGRAPH),
            _card(TYPED_DISCARD_ID, "Batch Discard", TYPED_DISCARD_PARAGRAPH),
            _card(TYPED_RECURSION_ID, "Batch Recursion", TYPED_RECURSION_PARAGRAPH),
            _card(TYPED_SELF_RETURN_ID, "Batch Self Return", TYPED_SELF_RETURN_PARAGRAPH),
            _card(TYPED_TOKEN_COST_ID, "Batch Token Cost", TYPED_TOKEN_COST_PARAGRAPH),
            _card(TYPED_REPEATED_ID, "Batch Repeated", TYPED_REPEATED_SOURCE_TEXT),
            _card(TYPED_TAPPED_OUTLET_ID, "Batch Tapped Outlet", TYPED_TAPPED_OUTLET_PARAGRAPH),
            _card(
                TYPED_ALTERNATE_OUTLET_ID,
                "Batch Alternate Outlet",
                TYPED_ALTERNATE_OUTLET_PARAGRAPH,
            ),
            _card(
                TYPED_CONJUNCTIVE_OUTLET_ID,
                "Batch Conjunctive Outlet",
                TYPED_CONJUNCTIVE_OUTLET_PARAGRAPH,
            ),
            _card(
                TYPED_ARTIFACT_CREATURE_ID,
                "Batch Artifact Creature",
                TYPED_ARTIFACT_CREATURE_PARAGRAPH,
            ),
            _card(TYPED_OR_TYPE_OUTLET_ID, "Batch Or Type Outlet", TYPED_OR_TYPE_OUTLET_PARAGRAPH),
            _card(
                TYPED_PLANESWALKER_RETURN_ID,
                "Batch Planeswalker Return",
                TYPED_PLANESWALKER_RETURN_PARAGRAPH,
            ),
            _card(
                TYPED_MULTICOLOR_TOKEN_ID,
                "Batch Multicolor Token",
                TYPED_MULTICOLOR_TOKEN_PARAGRAPH,
            ),
            _card(TYPED_OWNED_OUTLET_ID, "Batch Owned Outlet", TYPED_OWNED_OUTLET_PARAGRAPH),
            _card(TYPED_HISTORY_TURN_ID, "Batch History Turn", TYPED_HISTORY_TURN_PARAGRAPH),
            _card(TYPED_HISTORY_DIED_ID, "Batch History Died", TYPED_HISTORY_DIED_PARAGRAPH),
            _card(TYPED_HISTORY_CAST_ID, "Batch History Cast", TYPED_HISTORY_CAST_PARAGRAPH),
            _card(TYPED_HISTORY_SO_FAR_ID, "Batch History So Far", TYPED_HISTORY_SO_FAR_PARAGRAPH),
            _card(TYPED_PLAIN_TOKEN_ID, "Batch Plain Token", TYPED_PLAIN_TOKEN_PARAGRAPH),
            _card(TYPED_LOOT_ONE_ID, "Batch Loot One", TYPED_LOOT_PARAGRAPH),
            _card(TYPED_LOOT_TWO_ID, "Batch Loot Two", TYPED_LOOT_PARAGRAPH),
            _card(TYPED_DEATH_ID, "Batch Death", TYPED_DEATH_PARAGRAPH),
        ),
        guides=(),
    )


def _typed_card(card_id: int) -> CardInfo:
    """Return one frozen card of the typed prerequisite matrix."""
    return next(card for card in _typed_sources().cards if card.grp_id == card_id)


def _timing_json(
    *,
    window: str = "unrestricted",
    turn: str = "any",
    max_per_turn: int | None = None,
) -> dict[str, Any]:
    return {"window": window, "turn": turn, "max_per_turn": max_per_turn}


def _zone_json(zone: str, player: str) -> dict[str, Any]:
    return {"zone": zone, "player": player}


def _quantity_json(value: int, relation: str = "exactly") -> dict[str, Any]:
    return {"value": value, "relation": relation}


def _token_output_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `Create two 1/1 white Soldier creature tokens.` output clause."""
    values: dict[str, Any] = {
        "kind": "condition",
        "subject": "output",
        "operation": "create",
        "object_kind": "token",
        "card_types": ["creature"],
        "type_operator": "all_of",
        "token_restriction": "token",
        "exclusion": "none",
        "subtype": "Soldier",
        "color_operator": "exact",
        "colors": ["W"],
        "controller": "you",
        "owner": "not_applicable",
        "quantity": _quantity_json(2),
        "source_zone": None,
        "destination_zone": _zone_json("battlefield", "you"),
        "timing": _timing_json(),
        "required_card_id": None,
        "evidence": _evidence(card_id=TYPED_SOURCE_ID, quote=TYPED_TOKEN_PARAGRAPH),
        "operation_quote": "Create",
        "operation_occurrence": 0,
        "object_quote": "two 1/1 white Soldier creature tokens",
        "object_occurrence": 0,
        "capability_prerequisite_indices": [],
    }
    values.update(overrides)
    return values


def _one_token_output_clause(
    *,
    card_id: int,
    paragraph: str,
    operation_quote: str = "Create",
    **overrides: Any,
) -> dict[str, Any]:
    """Build one complete single-creature-token output clause of the given paragraph."""
    values = _token_output_clause(
        quantity=_quantity_json(1),
        evidence=_evidence(card_id=card_id, quote=paragraph),
        operation_quote=operation_quote,
        object_quote="a 1/1 white Soldier creature token",
    )
    values.update(overrides)
    return values


def _upkeep_output_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete upkeep-triggered creature token output clause."""
    values = _one_token_output_clause(
        card_id=TYPED_UPKEEP_ID,
        paragraph=TYPED_UPKEEP_PARAGRAPH,
        operation_quote="create",
        timing=_timing_json(window="upkeep", turn="your"),
    )
    values.update(overrides)
    return values


def _draw_effect_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete draw-effect clause."""
    values = _token_output_clause(
        operation="draw",
        object_kind="card",
        card_types=[],
        type_operator="unrestricted",
        token_restriction="unrestricted",
        subtype=None,
        color_operator="unrestricted",
        colors=[],
        quantity=_quantity_json(1),
        destination_zone=None,
        evidence=_evidence(card_id=TYPED_REPEATED_ID, quote=TYPED_DRAW_PARAGRAPH),
        operation_quote="Draw",
        object_quote="a card",
    )
    values.update(overrides)
    return values


def _permanent_condition_clause(
    *,
    card_id: int,
    paragraph: str,
    object_quote: str,
    **overrides: Any,
) -> dict[str, Any]:
    """Build one complete target-side permanent condition clause."""
    values: dict[str, Any] = {
        "kind": "condition",
        "subject": "participant",
        "operation": "control",
        "object_kind": "permanent",
        "card_types": ["creature"],
        "type_operator": "all_of",
        "token_restriction": "unrestricted",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": [],
        "controller": "you",
        "owner": "not_applicable",
        "quantity": None,
        "source_zone": None,
        "destination_zone": None,
        "timing": _timing_json(),
        "required_card_id": None,
        "evidence": _evidence(card_id=card_id, quote=paragraph),
        "operation_quote": "control",
        "operation_occurrence": 0,
        "object_quote": object_quote,
        "object_occurrence": 0,
        "capability_prerequisite_indices": [],
    }
    values.update(overrides)
    return values


def _anthem_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `Creatures you control get +1/+1.` condition clause."""
    values = _permanent_condition_clause(
        card_id=TYPED_ANTHEM_ID,
        paragraph=TYPED_ANTHEM_PARAGRAPH,
        object_quote="Creatures you control",
    )
    values.update(overrides)
    return values


def _typal_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `Soldier creatures you control get +1/+1.` condition clause."""
    values = _permanent_condition_clause(
        card_id=TYPED_TYPAL_ID,
        paragraph=TYPED_TYPAL_PARAGRAPH,
        object_quote="Soldier creatures you control",
    )
    values.update({"subtype": "soldier"})
    values.update(overrides)
    return values


def _threshold_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `As long as you control three or more creatures, ...` clause."""
    values = _permanent_condition_clause(
        card_id=TYPED_THRESHOLD_ID,
        paragraph=TYPED_THRESHOLD_PARAGRAPH,
        object_quote="you control three or more creatures",
    )
    values.update(
        {
            "kind": "threshold",
            "subject": "input",
            "quantity": _quantity_json(3, "at_least"),
        }
    )
    values.update(overrides)
    return values


def _sacrifice_clause(
    *,
    card_id: int,
    paragraph: str,
    object_quote: str,
    **overrides: Any,
) -> dict[str, Any]:
    """Build one complete sacrifice cost clause."""
    values: dict[str, Any] = {
        "kind": "cost",
        "subject": "input",
        "operation": "sacrifice",
        "object_kind": "permanent",
        "card_types": ["creature"],
        "type_operator": "all_of",
        "token_restriction": "unrestricted",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": [],
        "controller": "you",
        "owner": "not_applicable",
        "quantity": _quantity_json(1),
        "source_zone": None,
        "destination_zone": _zone_json("graveyard", "owner"),
        "timing": _timing_json(),
        "required_card_id": None,
        "evidence": _evidence(card_id=card_id, quote=paragraph),
        "operation_quote": "Sacrifice",
        "operation_occurrence": 0,
        "object_quote": object_quote,
        "object_occurrence": 0,
        "capability_prerequisite_indices": [],
    }
    values.update(overrides)
    return values


def _outlet_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `Sacrifice a creature: Draw a card.` cost clause."""
    return _sacrifice_clause(
        card_id=TYPED_OUTLET_ID,
        paragraph=TYPED_OUTLET_PARAGRAPH,
        object_quote="a creature",
        **overrides,
    )


def _discard_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `Discard a creature card.` condition clause."""
    values: dict[str, Any] = {
        "kind": "condition",
        "subject": "input",
        "operation": "discard",
        "object_kind": "card",
        "card_types": ["creature"],
        "type_operator": "all_of",
        "token_restriction": "unrestricted",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": [],
        "controller": "you",
        "owner": "not_applicable",
        "quantity": _quantity_json(1),
        "source_zone": _zone_json("hand", "you"),
        "destination_zone": _zone_json("graveyard", "you"),
        "timing": _timing_json(),
        "required_card_id": None,
        "evidence": _evidence(card_id=TYPED_DISCARD_ID, quote=TYPED_DISCARD_PARAGRAPH),
        "operation_quote": "Discard",
        "operation_occurrence": 0,
        "object_quote": "a creature card",
        "object_occurrence": 0,
        "capability_prerequisite_indices": [],
    }
    values.update(overrides)
    return values


def _return_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `Return target creature card from your graveyard to your hand.` clause."""
    values: dict[str, Any] = {
        "kind": "condition",
        "subject": "output",
        "operation": "return",
        "object_kind": "card",
        "card_types": ["creature"],
        "type_operator": "all_of",
        "token_restriction": "unrestricted",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": [],
        "controller": "you",
        "owner": "not_applicable",
        "quantity": None,
        "source_zone": _zone_json("graveyard", "you"),
        "destination_zone": _zone_json("hand", "you"),
        "timing": _timing_json(),
        "required_card_id": None,
        "evidence": _evidence(card_id=TYPED_RECURSION_ID, quote=TYPED_RECURSION_PARAGRAPH),
        "operation_quote": "Return",
        "operation_occurrence": 0,
        "object_quote": "target creature card from your graveyard",
        "object_occurrence": 0,
        "capability_prerequisite_indices": [],
    }
    values.update(overrides)
    return values


def _self_return_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `Return this card from your graveyard to your hand.` clause."""
    values = _return_clause(
        card_types=[],
        type_operator="unrestricted",
        evidence=_evidence(card_id=TYPED_SELF_RETURN_ID, quote=TYPED_SELF_RETURN_PARAGRAPH),
        object_quote="this card",
        required_card_id=TYPED_SELF_RETURN_ID,
    )
    values.update(overrides)
    return values


def _death_clause(**overrides: Any) -> dict[str, Any]:
    """Build one complete `Whenever one or more other creatures die, scry 1.` trigger clause."""
    values: dict[str, Any] = {
        "kind": "trigger",
        "subject": "event",
        "operation": "die",
        "object_kind": "permanent",
        "card_types": ["creature"],
        "type_operator": "all_of",
        "token_restriction": "unrestricted",
        "exclusion": "ability_source",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": [],
        "controller": "any",
        "owner": "not_applicable",
        "quantity": _quantity_json(1, "at_least"),
        "source_zone": None,
        "destination_zone": _zone_json("graveyard", "owner"),
        "timing": _timing_json(),
        "required_card_id": None,
        "evidence": _evidence(card_id=TYPED_DEATH_ID, quote=TYPED_DEATH_PARAGRAPH),
        "operation_quote": "die",
        "operation_occurrence": 0,
        "object_quote": "one or more other creatures",
        "object_occurrence": 0,
        "capability_prerequisite_indices": [],
    }
    values.update(overrides)
    return values


def _typed_capability(
    *,
    card_id: int,
    role: Role,
    paragraph: str,
    finding_id: str,
) -> CardCapability:
    """Build one real capability bound to a typed matrix card and paragraph."""
    return _capability(
        finding_id=finding_id,
        card_id=card_id,
        card_name=_typed_card(card_id).name,
        role=role,
        quote=paragraph,
    )


def _typed_token_source(
    *,
    card_id: int = TYPED_SOURCE_ID,
    paragraph: str = TYPED_TOKEN_PARAGRAPH,
) -> CardCapability:
    """Build the token-making participant of the typed matrix."""
    return _typed_capability(
        card_id=card_id,
        role=Role.TOKEN_MAKER,
        paragraph=paragraph,
        finding_id="capability-batch-tokens",
    )


def _typed_upkeep_source() -> CardCapability:
    """Build the upkeep-triggered token-making participant of the timing fixture."""
    return _typed_capability(
        card_id=TYPED_UPKEEP_ID,
        role=Role.TOKEN_MAKER,
        paragraph=TYPED_UPKEEP_PARAGRAPH,
        finding_id="capability-batch-upkeep-tokens",
    )


def _typed_payoff(*, card_id: int, paragraph: str) -> CardCapability:
    """Build one go-wide payoff participant of the typed matrix."""
    return _typed_capability(
        card_id=card_id,
        role=Role.GO_WIDE_PAYOFF,
        paragraph=paragraph,
        finding_id="capability-batch-payoff",
    )


def _typed_outlet(
    *,
    card_id: int = TYPED_OUTLET_ID,
    paragraph: str = TYPED_OUTLET_PARAGRAPH,
) -> CardCapability:
    """Build one sacrifice outlet participant of the typed matrix."""
    return _typed_capability(
        card_id=card_id,
        role=Role.SACRIFICE_OUTLET,
        paragraph=paragraph,
        finding_id="capability-batch-outlet",
    )


def _typed_discard_enabler() -> CardCapability:
    """Build the discard participant of the recursion fixture."""
    return _typed_capability(
        card_id=TYPED_DISCARD_ID,
        role=Role.DISCARD_ENABLER,
        paragraph=TYPED_DISCARD_PARAGRAPH,
        finding_id="capability-batch-discard",
    )


def _typed_recursion(*, card_id: int = TYPED_RECURSION_ID, paragraph: str = TYPED_RECURSION_PARAGRAPH) -> CardCapability:
    """Build one graveyard-return participant of the recursion fixture."""
    return _typed_capability(
        card_id=card_id,
        role=Role.RECURSION_PAYOFF,
        paragraph=paragraph,
        finding_id="capability-batch-recursion",
    )


def _typed_death_payoff() -> CardCapability:
    """Build the death payoff participant of the token-death fixture."""
    return _typed_capability(
        card_id=TYPED_DEATH_ID,
        role=Role.DEATH_PAYOFF,
        paragraph=TYPED_DEATH_PARAGRAPH,
        finding_id="capability-batch-death",
    )


def _typed_response(
    *,
    source: CardCapability,
    target: CardCapability,
    source_clauses: list[dict[str, Any]],
    target_clauses: list[dict[str, Any]],
    prerequisite_status: str = "complete",
    verdict: str = "accepted",
    claim: str = TYPED_CLAIM,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build one v2 relationship document around one typed matrix participant pair."""
    return _response(
        verdict=verdict,
        claim=claim,
        reason=reason,
        evidence=_participant_coverage(source, target),
        prerequisite_status=prerequisite_status,
        source_prerequisites=source_clauses,
        target_prerequisites=target_clauses,
    )


def _parse_typed(
    *,
    response: dict[str, Any],
    source: CardCapability,
    target: CardCapability,
    mechanism: str = MECHANISM,
) -> RelationshipValidationResult:
    """Parse one relationship document against the typed matrix source set."""
    return parse_relationship_validation_response(
        content=_content(response),
        sources=_typed_sources(),
        mechanism=mechanism,
        source=source,
        target=target,
        run_id=RUN_ID,
    )


def _typed_projection(
    result: RelationshipValidationResult,
) -> RelationshipPrerequisiteProjection:
    """Return the projection of one successful typed parse."""
    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.rejected is None
    relationship = result.relationship
    assert relationship is not None
    projection = relationship.prerequisite_projection
    assert projection is not None
    return projection


def _typed_pair_result(
    *,
    source: CardCapability,
    target: CardCapability,
    source_clauses: list[dict[str, Any]],
    target_clauses: list[dict[str, Any]],
    mechanism: str = MECHANISM,
    prerequisite_status: str = "complete",
    verdict: str = "accepted",
    claim: str = TYPED_CLAIM,
    reason: str | None = None,
) -> RelationshipValidationResult:
    """Parse one complete typed fixture without a projection requirement."""
    return _parse_typed(
        response=_typed_response(
            source=source,
            target=target,
            source_clauses=source_clauses,
            target_clauses=target_clauses,
            prerequisite_status=prerequisite_status,
            verdict=verdict,
            claim=claim,
            reason=reason,
        ),
        source=source,
        target=target,
        mechanism=mechanism,
    )


def _advisory_relationship(result: RelationshipValidationResult) -> ValidatedRelationship:
    """Return the accepted advisory relationship that carries no projection."""
    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.rejected is None
    relationship = result.relationship
    assert relationship is not None
    assert relationship.prerequisite_projection is None
    assert relationship.review.status is FindingStatus.ACCEPTED
    return relationship


def _contradiction_reason(result: RelationshipValidationResult) -> str:
    """Return the fixed contradiction reason of one rejected typed parse."""
    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.relationship is None
    assert result.rejected is not None
    return result.rejected.reason


def _loot_clause(
    *,
    card_id: int,
    operation: str,
    operation_quote: str,
    object_occurrence: int,
) -> RelationshipPrerequisite:
    """Build one LOOT clause of the mixed-direction record pair."""
    draw = operation == "draw"
    return RelationshipPrerequisite(
        kind=PrerequisiteKind.CONDITION,
        subject="output" if draw else "input",
        operation=operation,
        object_kind="card",
        card_types=(),
        type_operator="unrestricted",
        token_restriction="unrestricted",
        exclusion="none",
        subtype=None,
        color_operator="unrestricted",
        colors=(),
        controller="any",
        owner="not_applicable",
        quantity=CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
        source_zone=None if draw else _record_zone(zone=CapabilityZone.HAND, player="you"),
        destination_zone=(
            None if draw else _record_zone(zone=CapabilityZone.GRAVEYARD, player="you")
        ),
        timing=_record_timing(),
        required_card_id=None,
        evidence=OracleEvidence(card_id=card_id, face_index=None, quote=TYPED_LOOT_PARAGRAPH),
        operation_quote=operation_quote,
        operation_occurrence=0,
        object_quote="a card",
        object_occurrence=object_occurrence,
        capability_prerequisite_indices=(),
    )


def _loot_participant(*, card_id: int) -> RelationshipParticipant:
    """Build one LOOT participant carrying a draw clause and a discard clause."""
    return RelationshipParticipant(
        card_id=card_id,
        capability_id=f"capability-batch-loot-{card_id}",
        card_name=_typed_card(card_id).name,
        face_index=None,
        face_name=None,
        card_source_sha256=card_source_sha256(_typed_card(card_id)),
        role=Role.LOOT,
        capability_prerequisites=(),
        prerequisites=(
            _loot_clause(
                card_id=card_id,
                operation="draw",
                operation_quote="Draw",
                object_occurrence=0,
            ),
            _loot_clause(
                card_id=card_id,
                operation="discard",
                operation_quote="Discard",
                object_occurrence=1,
            ),
        ),
    )


def _loot_relationship(*, source_card_id: int, target_card_id: int) -> CardRelationship:
    """Build one accepted LOOT relationship in the given direction."""
    source = _loot_participant(card_id=source_card_id)
    target = _loot_participant(card_id=target_card_id)
    return CardRelationship(
        finding_id=(
            f"relationship:{LOOT_MECHANISM}:{source_card_id}:{source.capability_id}"
            f":{target_card_id}:{target.capability_id}"
        ),
        mechanism=LOOT_MECHANISM,
        participants=(source_card_id, target_card_id),
        claim=TYPED_CLAIM,
        prerequisites=("Both cards loot cards for selection.",),
        oracle_evidence=(
            OracleEvidence(card_id=source_card_id, face_index=None, quote=TYPED_LOOT_PARAGRAPH),
            OracleEvidence(card_id=target_card_id, face_index=None, quote=TYPED_LOOT_PARAGRAPH),
        ),
        guide_evidence=(),
        review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
        run_id=RUN_ID,
        prerequisite_projection=RelationshipPrerequisiteProjection(source=source, target=target),
    )


def test_typed_enabler_payoff_projection_preserves_every_dimension() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_anthem_clause()],
    )

    projection = _typed_projection(result)
    assert _typed_card(TYPED_SOURCE_ID).colors == ("U", "B")
    assert projection.source.card_id == TYPED_SOURCE_ID
    assert projection.target.card_id == TYPED_ANTHEM_ID
    assert projection.source.card_id > projection.target.card_id
    assert projection.source.capability_id == source.finding_id
    assert projection.source.role is Role.TOKEN_MAKER
    assert projection.source.card_source_sha256 == card_source_sha256(_typed_card(TYPED_SOURCE_ID))
    assert projection.target.card_source_sha256 == card_source_sha256(_typed_card(TYPED_ANTHEM_ID))
    assert projection.target.role is Role.GO_WIDE_PAYOFF
    clause = projection.source.prerequisites[0]
    assert clause.kind is PrerequisiteKind.CONDITION
    assert clause.subject == "output"
    assert clause.operation == "create"
    assert clause.object_kind == "token"
    assert clause.card_types == ("creature",)
    assert clause.type_operator == "all_of"
    assert clause.token_restriction == "token"
    assert clause.exclusion == "none"
    assert clause.subtype == "soldier"
    assert clause.color_operator == "exact"
    assert clause.colors == ("W",)
    assert clause.quantity == TOKEN_QUANTITY
    assert clause.controller == "you"
    assert clause.destination_zone == RelationshipZone(
        zone=CapabilityZone.BATTLEFIELD,
        player="you",
    )
    assert clause.timing == RelationshipTiming(
        window="unrestricted",
        turn="any",
        max_per_turn=None,
    )
    assert clause.required_card_id is None
    assert clause.evidence == OracleEvidence(
        card_id=TYPED_SOURCE_ID,
        face_index=None,
        quote=TYPED_TOKEN_PARAGRAPH,
    )
    payoff_clause = projection.target.prerequisites[0]
    assert payoff_clause.operation == "control"
    assert payoff_clause.card_types == ("creature",)
    assert payoff_clause.controller == "you"
    assert payoff_clause.quantity is None

    relationship = result.relationship
    assert relationship is not None
    assert relationship.identity == (
        MECHANISM,
        TYPED_SOURCE_ID,
        "capability-batch-tokens",
        TYPED_ANTHEM_ID,
        "capability-batch-payoff",
    )
    assert relationship.identity[2] == source.finding_id
    assert ValidatedRelationship.from_json(relationship.to_json()) == relationship


def test_typed_enabler_payoff_output_color_must_match_the_source() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause(colors=["U"])],
        target_clauses=[_anthem_clause()],
    )

    aligned = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause(colors=["W"])],
        target_clauses=[_anthem_clause()],
    )

    assert _typed_projection(aligned).source.prerequisites[0].colors == ("W",)
    assert _contradiction_reason(result) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_typed_threshold_projection_keeps_the_count_and_controller() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_THRESHOLD_ID, paragraph=TYPED_THRESHOLD_PARAGRAPH)

    projection = _typed_projection(
        _typed_pair_result(
            source=source,
            target=target,
            source_clauses=[_token_output_clause()],
            target_clauses=[_threshold_clause()],
        )
    )

    clause = projection.target.prerequisites[0]
    assert clause.kind is PrerequisiteKind.THRESHOLD
    assert clause.operation == "control"
    assert clause.card_types == ("creature",)
    assert clause.quantity == SCALED_QUANTITY
    assert clause.controller == "you"
    assert clause.evidence.quote == TYPED_THRESHOLD_PARAGRAPH
    # `this creature gets +1/+1` is the payoff's later effect text, not part of the selected
    # object phrase, so the clause claims no exact card even though the paragraph names one.
    assert clause.required_card_id is None
    assert projection.target.card_id == TYPED_THRESHOLD_ID


def test_typed_threshold_identity_must_be_supported_by_the_object_phrase() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_THRESHOLD_ID, paragraph=TYPED_THRESHOLD_PARAGRAPH)

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_threshold_clause(required_card_id=TYPED_THRESHOLD_ID)],
    )

    assert _contradiction_reason(result) == PREREQUISITE_CONTRADICTION_MESSAGE


@pytest.mark.parametrize(
    "quantity",
    (
        pytest.param(None, id="missing-quantity"),
        pytest.param({"value": None, "relation": "variable"}, id="variable-quantity"),
    ),
)
def test_typed_threshold_without_a_fixed_count_is_not_evaluable(quantity: Any) -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_THRESHOLD_ID, paragraph=TYPED_THRESHOLD_PARAGRAPH)

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_threshold_clause(quantity=quantity)],
    )

    assert _advisory_relationship(result).claim == TYPED_CLAIM


def test_typed_threshold_count_must_match_the_source_evidence() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_THRESHOLD_ID, paragraph=TYPED_THRESHOLD_PARAGRAPH)

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_threshold_clause(quantity=_quantity_json(2, "at_least"))],
    )

    assert _contradiction_reason(result) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_typed_recursion_keeps_both_zone_transitions_distinct() -> None:
    source = _typed_discard_enabler()
    target = _typed_recursion()

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_discard_clause()],
        target_clauses=[_return_clause()],
        mechanism=RECURSION_MECHANISM,
    )

    projection = _typed_projection(result)
    discarded = projection.source.prerequisites[0]
    returned = projection.target.prerequisites[0]
    assert discarded.operation == "discard"
    assert discarded.object_kind == "card"
    assert discarded.source_zone == RelationshipZone(zone=CapabilityZone.HAND, player="you")
    assert discarded.destination_zone == RelationshipZone(
        zone=CapabilityZone.GRAVEYARD,
        player="you",
    )
    assert returned.operation == "return"
    assert returned.source_zone == RelationshipZone(zone=CapabilityZone.GRAVEYARD, player="you")
    assert returned.destination_zone == RelationshipZone(zone=CapabilityZone.HAND, player="you")
    assert discarded != returned
    relationship = result.relationship
    assert relationship is not None
    assert relationship.identity == (
        RECURSION_MECHANISM,
        TYPED_DISCARD_ID,
        "capability-batch-discard",
        TYPED_RECURSION_ID,
        "capability-batch-recursion",
    )


@pytest.mark.parametrize(
    "source_zone",
    (
        pytest.param(_zone_json("hand", "you"), id="wrong-source-zone"),
        pytest.param(_zone_json("graveyard", "opponent"), id="wrong-zone-owner"),
    ),
)
def test_typed_recursion_rejects_a_wrong_target_source_zone(source_zone: dict[str, Any]) -> None:
    source = _typed_discard_enabler()
    target = _typed_recursion()

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_discard_clause()],
        target_clauses=[_return_clause(source_zone=source_zone)],
        mechanism=RECURSION_MECHANISM,
    )

    assert _contradiction_reason(result) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_typed_typal_projection_keeps_the_exact_subtype() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_TYPAL_ID, paragraph=TYPED_TYPAL_PARAGRAPH)

    projection = _typed_projection(
        _typed_pair_result(
            source=source,
            target=target,
            source_clauses=[_token_output_clause()],
            target_clauses=[_typal_clause()],
        )
    )

    clause = projection.target.prerequisites[0]
    assert clause.subtype == "soldier"
    assert clause.object_quote == "Soldier creatures you control"
    assert clause.card_types == ("creature",)
    assert clause.controller == "you"
    assert clause.color_operator == "unrestricted"
    assert clause.colors == ()


def test_typed_typal_subtype_must_match_the_stated_atom() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_TYPAL_ID, paragraph=TYPED_TYPAL_PARAGRAPH)

    rejected = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_typal_clause(subtype="elf")],
    )
    unclassified = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_typal_clause(subtype=None)],
    )

    assert _contradiction_reason(rejected) == PREREQUISITE_CONTRADICTION_MESSAGE
    assert _advisory_relationship(unclassified).claim == TYPED_CLAIM


def test_typed_sacrifice_cost_stays_separate_from_its_draw_effect() -> None:
    source = _typed_token_source()
    target = _typed_outlet()

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_outlet_clause()],
        mechanism=SACRIFICE_MECHANISM,
    )

    projection = _typed_projection(result)
    assert "Draw a card." in TYPED_OUTLET_PARAGRAPH
    assert len(projection.target.prerequisites) == 1
    clause = projection.target.prerequisites[0]
    assert clause.kind is PrerequisiteKind.COST
    assert clause.subject == "input"
    assert clause.operation == "sacrifice"
    assert clause.object_kind == "permanent"
    assert clause.card_types == ("creature",)
    assert clause.token_restriction == "unrestricted"
    assert clause.exclusion == "none"
    assert clause.quantity == CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    assert clause.source_zone is None
    assert clause.destination_zone == RelationshipZone(
        zone=CapabilityZone.GRAVEYARD,
        player="owner",
    )
    assert clause.required_card_id is None
    assert all(
        prerequisite.operation != "draw"
        for participant in (projection.source, projection.target)
        for prerequisite in participant.prerequisites
    )


def test_typed_token_maker_anchor_requires_a_token_output() -> None:
    source = _typed_token_source(
        card_id=TYPED_TOKEN_COST_ID,
        paragraph=TYPED_TOKEN_COST_PARAGRAPH,
    )
    target = _typed_outlet()

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[
            _sacrifice_clause(
                card_id=TYPED_TOKEN_COST_ID,
                paragraph=TYPED_TOKEN_COST_PARAGRAPH,
                object_quote="a token",
                object_kind="token",
                token_restriction="token",
                card_types=[],
                type_operator="unrestricted",
            )
        ],
        target_clauses=[_outlet_clause()],
        mechanism=SACRIFICE_MECHANISM,
    )

    assert _advisory_relationship(result).claim == TYPED_CLAIM


def test_typed_outlet_exclusion_tracks_the_stated_other_creature() -> None:
    source = _typed_token_source()
    target = _typed_outlet(
        card_id=TYPED_OTHER_OUTLET_ID,
        paragraph=TYPED_OTHER_OUTLET_PARAGRAPH,
    )
    clause = _sacrifice_clause(
        card_id=TYPED_OTHER_OUTLET_ID,
        paragraph=TYPED_OTHER_OUTLET_PARAGRAPH,
        object_quote="another creature",
        exclusion="ability_source",
        quantity=None,
    )

    projection = _typed_projection(
        _typed_pair_result(
            source=source,
            target=target,
            source_clauses=[_token_output_clause()],
            target_clauses=[clause],
            mechanism=SACRIFICE_MECHANISM,
        )
    )
    untyped = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[{**clause, "exclusion": "none"}],
        mechanism=SACRIFICE_MECHANISM,
    )

    assert projection.target.prerequisites[0].exclusion == "ability_source"
    assert _advisory_relationship(untyped).claim == TYPED_CLAIM


def test_typed_self_sacrifice_cannot_anchor_the_outlet() -> None:
    source = _typed_token_source()
    target = _typed_outlet(
        card_id=TYPED_SELF_OUTLET_ID,
        paragraph=TYPED_SELF_OUTLET_PARAGRAPH,
    )

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[
            _sacrifice_clause(
                card_id=TYPED_SELF_OUTLET_ID,
                paragraph=TYPED_SELF_OUTLET_PARAGRAPH,
                object_quote="this creature",
                required_card_id=TYPED_SELF_OUTLET_ID,
                quantity=None,
            )
        ],
        mechanism=SACRIFICE_MECHANISM,
    )

    assert _advisory_relationship(result).claim == TYPED_CLAIM


def test_typed_timing_keeps_independent_windows() -> None:
    source = _typed_upkeep_source()
    target = _typed_outlet(
        card_id=TYPED_TIMED_OUTLET_ID,
        paragraph=TYPED_TIMED_OUTLET_PARAGRAPH,
    )

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_upkeep_output_clause()],
        target_clauses=[
            _sacrifice_clause(
                card_id=TYPED_TIMED_OUTLET_ID,
                paragraph=TYPED_TIMED_OUTLET_PARAGRAPH,
                object_quote="a creature",
                timing=_timing_json(turn="your", max_per_turn=1),
            )
        ],
        mechanism=SACRIFICE_MECHANISM,
    )

    projection = _typed_projection(result)
    assert projection.source.prerequisites[0].timing == RelationshipTiming(
        window="upkeep",
        turn="your",
        max_per_turn=None,
    )
    assert projection.target.prerequisites[0].timing == RelationshipTiming(
        window="unrestricted",
        turn="your",
        max_per_turn=1,
    )


@pytest.mark.parametrize(
    "timing",
    (
        pytest.param(_timing_json(), id="omitted-turn-and-once-restrictions"),
        pytest.param(_timing_json(turn="your"), id="omitted-once-restriction"),
        pytest.param(_timing_json(turn="opponent", max_per_turn=1), id="contradictory-turn"),
    ),
)
def test_typed_timing_restrictions_must_match_their_source(timing: dict[str, Any]) -> None:
    source = _typed_upkeep_source()
    target = _typed_outlet(
        card_id=TYPED_TIMED_OUTLET_ID,
        paragraph=TYPED_TIMED_OUTLET_PARAGRAPH,
    )

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_upkeep_output_clause()],
        target_clauses=[
            _sacrifice_clause(
                card_id=TYPED_TIMED_OUTLET_ID,
                paragraph=TYPED_TIMED_OUTLET_PARAGRAPH,
                object_quote="a creature",
                timing=timing,
            )
        ],
        mechanism=SACRIFICE_MECHANISM,
    )

    assert _contradiction_reason(result) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_typed_supplied_contradiction_outranks_status_and_completeness() -> None:
    source = _typed_token_source()
    typal = _typed_payoff(card_id=TYPED_TYPAL_ID, paragraph=TYPED_TYPAL_PARAGRAPH)
    anthem = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)

    uncertain = _typed_pair_result(
        source=source,
        target=typal,
        source_clauses=[_token_output_clause()],
        target_clauses=[_typal_clause(subtype="elf")],
        prerequisite_status="uncertain",
    )
    empty_opposite_side = _typed_pair_result(
        source=source,
        target=typal,
        source_clauses=[],
        target_clauses=[_typal_clause(subtype="elf")],
    )
    unanchored = _typed_pair_result(
        source=source,
        target=anthem,
        source_clauses=[_token_output_clause()],
        target_clauses=[_anthem_clause(controller="opponent")],
    )

    assert _contradiction_reason(uncertain) == PREREQUISITE_CONTRADICTION_MESSAGE
    assert _contradiction_reason(empty_opposite_side) == PREREQUISITE_CONTRADICTION_MESSAGE
    assert _contradiction_reason(unanchored) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_typed_object_qualifier_clipping_never_projects() -> None:
    source = _typed_token_source()
    owned = _typed_outlet(
        card_id=TYPED_OWNED_OUTLET_ID,
        paragraph=TYPED_OWNED_OUTLET_PARAGRAPH,
    )

    def pair(
        *,
        object_quote: str = "a creature",
        **overrides: Any,
    ) -> RelationshipValidationResult:
        return _typed_pair_result(
            source=source,
            target=owned,
            source_clauses=[_token_output_clause()],
            target_clauses=[
                _sacrifice_clause(
                    card_id=TYPED_OWNED_OUTLET_ID,
                    paragraph=TYPED_OWNED_OUTLET_PARAGRAPH,
                    object_quote=object_quote,
                    **overrides,
                )
            ],
            mechanism=SACRIFICE_MECHANISM,
        )

    clipped = pair()
    honest = pair(object_quote="a creature you own", owner="you")
    contradictory = pair(object_quote="a creature you own", owner="opponent")

    assert _advisory_relationship(clipped).claim == TYPED_CLAIM
    assert _typed_projection(honest).target.prerequisites[0].owner == "you"
    assert _contradiction_reason(contradictory) == PREREQUISITE_CONTRADICTION_MESSAGE


@pytest.mark.parametrize(
    ("card_id", "paragraph", "operation_quote"),
    (
        pytest.param(
            TYPED_HISTORY_TURN_ID,
            TYPED_HISTORY_TURN_PARAGRAPH,
            "create",
            id="cast-a-spell-this-turn",
        ),
        pytest.param(
            TYPED_HISTORY_DIED_ID,
            TYPED_HISTORY_DIED_PARAGRAPH,
            "Create",
            id="a-creature-has-died",
        ),
        pytest.param(
            TYPED_HISTORY_CAST_ID,
            TYPED_HISTORY_CAST_PARAGRAPH,
            "Create",
            id="a-spell-was-cast",
        ),
        pytest.param(
            TYPED_HISTORY_SO_FAR_ID,
            TYPED_HISTORY_SO_FAR_PARAGRAPH,
            "Create",
            id="you-have-drawn-so-far",
        ),
    ),
)
def test_typed_bounded_history_phrases_never_project(
    card_id: int,
    paragraph: str,
    operation_quote: str,
) -> None:
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)
    control = _typed_pair_result(
        source=_typed_token_source(
            card_id=TYPED_PLAIN_TOKEN_ID,
            paragraph=TYPED_PLAIN_TOKEN_PARAGRAPH,
        ),
        target=target,
        source_clauses=[
            _one_token_output_clause(
                card_id=TYPED_PLAIN_TOKEN_ID,
                paragraph=TYPED_PLAIN_TOKEN_PARAGRAPH,
            )
        ],
        target_clauses=[_anthem_clause()],
    )
    historical = _typed_pair_result(
        source=_typed_token_source(card_id=card_id, paragraph=paragraph),
        target=target,
        source_clauses=[
            _one_token_output_clause(
                card_id=card_id,
                paragraph=paragraph,
                operation_quote=operation_quote,
            )
        ],
        target_clauses=[_anthem_clause()],
    )

    retained = _typed_projection(control).source.prerequisites[0]
    assert retained.object_quote == "a 1/1 white Soldier creature token"
    assert retained.quantity == CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    assert _advisory_relationship(historical).claim == TYPED_CLAIM


@pytest.mark.parametrize(
    ("prerequisite_status", "verdict", "reason", "expected_review", "projects"),
    (
        pytest.param(
            "complete",
            "accepted",
            None,
            FindingStatus.ACCEPTED,
            True,
            id="complete",
        ),
        pytest.param(
            "uncertain",
            "accepted",
            None,
            FindingStatus.ACCEPTED,
            False,
            id="uncertain-prerequisites",
        ),
        pytest.param(
            "uncertain",
            "uncertain",
            MODEL_UNCERTAINTY_REASON,
            FindingStatus.UNCERTAIN,
            False,
            id="uncertain-verdict",
        ),
        pytest.param(
            "unsupported",
            "accepted",
            None,
            FindingStatus.ACCEPTED,
            False,
            id="unsupported-prerequisites",
        ),
    ),
)
def test_typed_prerequisite_status_gates_the_projection(
    prerequisite_status: str,
    verdict: str,
    reason: str | None,
    expected_review: FindingStatus,
    projects: bool,
) -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_anthem_clause()],
        prerequisite_status=prerequisite_status,
        verdict=verdict,
        reason=reason,
    )

    relationship = result.relationship
    assert relationship is not None
    assert relationship.review.status is expected_review
    assert relationship.review.reason == reason
    assert (relationship.prerequisite_projection is not None) is projects


def test_typed_empty_prerequisite_side_never_projects() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)

    missing_source = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[],
        target_clauses=[_anthem_clause()],
    )
    missing_target = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[],
    )

    assert _advisory_relationship(missing_source).claim == TYPED_CLAIM
    assert _advisory_relationship(missing_target).claim == TYPED_CLAIM


@pytest.mark.parametrize(
    "evidence",
    (
        pytest.param(_evidence(card_id=TYPED_ANTHEM_ID), id="evidence-of-the-other-participant"),
        pytest.param(_evidence(face_index=0), id="face-index-the-card-does-not-have"),
        pytest.param(_evidence(quote=FABRICATED_QUOTE), id="quote-that-is-not-a-paragraph"),
    ),
)
def test_typed_clause_evidence_must_bind_its_own_participant(evidence: dict[str, Any]) -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause(evidence=evidence)],
        target_clauses=[_anthem_clause()],
    )

    assert _contradiction_reason(result) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_typed_clause_evidence_must_quote_its_own_ability() -> None:
    source = _typed_token_source(
        card_id=TYPED_REPEATED_ID,
        paragraph=TYPED_TOKEN_PARAGRAPH,
    )
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_draw_effect_clause()],
        target_clauses=[_anthem_clause()],
    )

    assert TYPED_REPEATED_SOURCE_TEXT.split("\n") == [
        TYPED_TOKEN_PARAGRAPH,
        TYPED_DRAW_PARAGRAPH,
    ]
    assert _contradiction_reason(result) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_typed_unrepresented_alternative_stays_advisory() -> None:
    source = _typed_token_source()
    plain = _typed_outlet()
    alternative = _typed_outlet(
        card_id=TYPED_ALTERNATE_OUTLET_ID,
        paragraph=TYPED_ALTERNATE_OUTLET_PARAGRAPH,
    )

    control = _typed_pair_result(
        source=source,
        target=plain,
        source_clauses=[_token_output_clause()],
        target_clauses=[_outlet_clause()],
        mechanism=SACRIFICE_MECHANISM,
    )
    omitted = _typed_pair_result(
        source=source,
        target=alternative,
        source_clauses=[_token_output_clause()],
        target_clauses=[
            _sacrifice_clause(
                card_id=TYPED_ALTERNATE_OUTLET_ID,
                paragraph=TYPED_ALTERNATE_OUTLET_PARAGRAPH,
                object_quote="a creature",
            )
        ],
        mechanism=SACRIFICE_MECHANISM,
    )

    assert _typed_projection(control).target.prerequisites[0].card_types == ("creature",)
    assert _advisory_relationship(omitted).claim == TYPED_CLAIM


def test_typed_conjoined_object_phrase_is_two_object_bindings() -> None:
    source = _typed_token_source()
    conjunctive = _typed_outlet(
        card_id=TYPED_CONJUNCTIVE_OUTLET_ID,
        paragraph=TYPED_CONJUNCTIVE_OUTLET_PARAGRAPH,
    )
    alternative = _typed_outlet(
        card_id=TYPED_ALTERNATE_OUTLET_ID,
        paragraph=TYPED_ALTERNATE_OUTLET_PARAGRAPH,
    )

    def pair(*, target: CardCapability, clause: dict[str, Any]) -> RelationshipValidationResult:
        return _typed_pair_result(
            source=source,
            target=target,
            source_clauses=[_token_output_clause()],
            target_clauses=[clause],
            mechanism=SACRIFICE_MECHANISM,
        )

    one_atomic_clause = pair(
        target=conjunctive,
        clause=_sacrifice_clause(
            card_id=TYPED_CONJUNCTIVE_OUTLET_ID,
            paragraph=TYPED_CONJUNCTIVE_OUTLET_PARAGRAPH,
            object_quote="a creature and an artifact",
            card_types=["artifact", "creature"],
            type_operator="all_of",
        ),
    )
    one_disjunctive_clause = pair(
        target=alternative,
        clause=_sacrifice_clause(
            card_id=TYPED_ALTERNATE_OUTLET_ID,
            paragraph=TYPED_ALTERNATE_OUTLET_PARAGRAPH,
            object_quote="a creature or an artifact",
            card_types=["artifact", "creature"],
            type_operator="any_of",
        ),
    )
    two_bindings = pair(
        target=conjunctive,
        clause=_sacrifice_clause(
            card_id=TYPED_CONJUNCTIVE_OUTLET_ID,
            paragraph=TYPED_CONJUNCTIVE_OUTLET_PARAGRAPH,
            object_quote="a creature",
        ),
    )
    split_bindings = _typed_pair_result(
        source=source,
        target=conjunctive,
        source_clauses=[_token_output_clause()],
        target_clauses=[
            _sacrifice_clause(
                card_id=TYPED_CONJUNCTIVE_OUTLET_ID,
                paragraph=TYPED_CONJUNCTIVE_OUTLET_PARAGRAPH,
                object_quote="a creature",
            ),
            _sacrifice_clause(
                card_id=TYPED_CONJUNCTIVE_OUTLET_ID,
                paragraph=TYPED_CONJUNCTIVE_OUTLET_PARAGRAPH,
                object_quote="an artifact",
                card_types=["artifact"],
            ),
        ],
        mechanism=SACRIFICE_MECHANISM,
    )

    assert _advisory_relationship(one_atomic_clause).claim == TYPED_CLAIM
    assert _advisory_relationship(one_disjunctive_clause).claim == TYPED_CLAIM
    assert _typed_projection(two_bindings) is not None

    clauses = _typed_projection(split_bindings).target.prerequisites
    assert {clause.object_quote: clause.card_types for clause in clauses} == {
        "a creature": ("creature",),
        "an artifact": ("artifact",),
    }
    assert {clause.kind for clause in clauses} == {PrerequisiteKind.COST}
    assert {clause.quantity for clause in clauses} == {
        CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY)
    }


def test_typed_single_object_type_phrase_keeps_its_operator() -> None:
    source = _typed_token_source()
    artifact_creature = _typed_outlet(
        card_id=TYPED_ARTIFACT_CREATURE_ID,
        paragraph=TYPED_ARTIFACT_CREATURE_PARAGRAPH,
    )
    typed_alternative = _typed_outlet(
        card_id=TYPED_OR_TYPE_OUTLET_ID,
        paragraph=TYPED_OR_TYPE_OUTLET_PARAGRAPH,
    )

    def pair(*, target: CardCapability, clause: dict[str, Any]) -> RelationshipValidationResult:
        return _typed_pair_result(
            source=source,
            target=target,
            source_clauses=[_token_output_clause()],
            target_clauses=[clause],
            mechanism=SACRIFICE_MECHANISM,
        )

    conjunction = pair(
        target=artifact_creature,
        clause=_sacrifice_clause(
            card_id=TYPED_ARTIFACT_CREATURE_ID,
            paragraph=TYPED_ARTIFACT_CREATURE_PARAGRAPH,
            object_quote="an artifact creature",
            card_types=["artifact", "creature"],
        ),
    )
    forced_disjunction = pair(
        target=artifact_creature,
        clause=_sacrifice_clause(
            card_id=TYPED_ARTIFACT_CREATURE_ID,
            paragraph=TYPED_ARTIFACT_CREATURE_PARAGRAPH,
            object_quote="an artifact creature",
            card_types=["artifact", "creature"],
            type_operator="any_of",
        ),
    )
    alternative = pair(
        target=typed_alternative,
        clause=_sacrifice_clause(
            card_id=TYPED_OR_TYPE_OUTLET_ID,
            paragraph=TYPED_OR_TYPE_OUTLET_PARAGRAPH,
            object_quote="an artifact or creature",
            card_types=["artifact", "creature"],
            type_operator="any_of",
        ),
    )
    forced_conjunction = pair(
        target=typed_alternative,
        clause=_sacrifice_clause(
            card_id=TYPED_OR_TYPE_OUTLET_ID,
            paragraph=TYPED_OR_TYPE_OUTLET_PARAGRAPH,
            object_quote="an artifact or creature",
            card_types=["artifact", "creature"],
            type_operator="all_of",
        ),
    )

    conjunction_clause = _typed_projection(conjunction).target.prerequisites[0]
    alternative_clause = _typed_projection(alternative).target.prerequisites[0]
    assert conjunction_clause.card_types == ("artifact", "creature")
    assert conjunction_clause.type_operator == "all_of"
    assert alternative_clause.card_types == ("artifact", "creature")
    assert alternative_clause.type_operator == "any_of"
    assert _advisory_relationship(forced_disjunction).claim == TYPED_CLAIM
    assert _advisory_relationship(forced_conjunction).claim == TYPED_CLAIM


def test_typed_single_object_alternative_selects_any_of() -> None:
    source = _typed_discard_enabler()
    target = _typed_recursion(
        card_id=TYPED_PLANESWALKER_RETURN_ID,
        paragraph=TYPED_PLANESWALKER_RETURN_PARAGRAPH,
    )

    def pair(*, type_operator: str) -> RelationshipValidationResult:
        return _typed_pair_result(
            source=source,
            target=target,
            source_clauses=[_discard_clause()],
            target_clauses=[
                _return_clause(
                    card_types=["creature", "planeswalker"],
                    type_operator=type_operator,
                    object_quote="target creature or planeswalker card from your graveyard",
                    evidence=_evidence(
                        card_id=TYPED_PLANESWALKER_RETURN_ID,
                        quote=TYPED_PLANESWALKER_RETURN_PARAGRAPH,
                    ),
                )
            ],
            mechanism=RECURSION_MECHANISM,
        )

    clause = _typed_projection(pair(type_operator="any_of")).target.prerequisites[0]

    assert clause.object_quote == "target creature or planeswalker card from your graveyard"
    assert clause.card_types == ("creature", "planeswalker")
    assert clause.type_operator == "any_of"
    assert _advisory_relationship(pair(type_operator="all_of")).claim == TYPED_CLAIM


def test_typed_multicolor_token_output_requires_the_exact_operator() -> None:
    source = _typed_token_source(
        card_id=TYPED_MULTICOLOR_TOKEN_ID,
        paragraph=TYPED_MULTICOLOR_TOKEN_PARAGRAPH,
    )
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)
    clause = _one_token_output_clause(
        card_id=TYPED_MULTICOLOR_TOKEN_ID,
        paragraph=TYPED_MULTICOLOR_TOKEN_PARAGRAPH,
        object_quote="a 1/1 white and blue Bird creature token",
        colors=["W", "U"],
        subtype="Bird",
    )

    def pair(*, color_operator: str) -> RelationshipValidationResult:
        return _typed_pair_result(
            source=source,
            target=target,
            source_clauses=[{**clause, "color_operator": color_operator}],
            target_clauses=[_anthem_clause()],
        )

    retained = _typed_projection(pair(color_operator="exact")).source.prerequisites[0]

    assert retained.colors == ("W", "U")
    assert retained.color_operator == "exact"
    assert retained.subtype == "bird"
    assert _advisory_relationship(pair(color_operator="any_of")).claim == TYPED_CLAIM


def test_typed_untracked_object_state_stays_advisory() -> None:
    source = _typed_token_source()
    plain = _typed_outlet()
    tapped = _typed_outlet(
        card_id=TYPED_TAPPED_OUTLET_ID,
        paragraph=TYPED_TAPPED_OUTLET_PARAGRAPH,
    )

    control = _typed_pair_result(
        source=source,
        target=plain,
        source_clauses=[_token_output_clause()],
        target_clauses=[_outlet_clause()],
        mechanism=SACRIFICE_MECHANISM,
    )
    result = _typed_pair_result(
        source=source,
        target=tapped,
        source_clauses=[_token_output_clause()],
        target_clauses=[
            _sacrifice_clause(
                card_id=TYPED_TAPPED_OUTLET_ID,
                paragraph=TYPED_TAPPED_OUTLET_PARAGRAPH,
                object_quote="a tapped creature",
            )
        ],
        mechanism=SACRIFICE_MECHANISM,
    )

    assert _typed_projection(control).target.prerequisites[0].object_quote == "a creature"
    assert _advisory_relationship(result).claim == TYPED_CLAIM


def test_typed_token_death_comparator_forwards_only_a_validated_projection() -> None:
    source = _typed_token_source()
    target = _typed_death_payoff()

    comparator = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_death_clause()],
        mechanism=TOKEN_DEATH_MECHANISM,
    )
    contradictory = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause(colors=["U"])],
        target_clauses=[_death_clause()],
        mechanism=TOKEN_DEATH_MECHANISM,
    )
    incomplete = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[],
        target_clauses=[],
        mechanism=TOKEN_DEATH_MECHANISM,
        prerequisite_status=NOT_COMPLETE_STATUS,
    )

    relationship = comparator.relationship
    assert relationship is not None
    assert relationship.review == FindingReview(status=FindingStatus.ACCEPTED, reason=None)
    assert relationship.claim == TOKEN_DEATH_CLAIM
    projection = _typed_projection(comparator)
    assert {
        prerequisite.evidence
        for participant in (projection.source, projection.target)
        for prerequisite in participant.prerequisites
    } <= set(relationship.evidence)
    assert {item.card_id for item in relationship.evidence} == {TYPED_SOURCE_ID, TYPED_DEATH_ID}
    assert _contradiction_reason(contradictory) == PREREQUISITE_CONTRADICTION_MESSAGE
    assert _advisory_relationship(incomplete).review.status is FindingStatus.ACCEPTED


@pytest.mark.parametrize(
    "required_card_id",
    (
        pytest.param(FOREIGN_CARD_ID, id="unknown-required-card"),
        pytest.param(TYPED_OUTLET_ID, id="unrelated-frozen-card"),
    ),
)
def test_typed_exact_identity_rejects_an_unstated_required_card(required_card_id: int) -> None:
    source = _typed_discard_enabler()
    target = _typed_recursion(
        card_id=TYPED_SELF_RETURN_ID,
        paragraph=TYPED_SELF_RETURN_PARAGRAPH,
    )

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_discard_clause()],
        target_clauses=[_self_return_clause(required_card_id=required_card_id)],
        mechanism=RECURSION_MECHANISM,
    )

    assert _advisory_relationship(result).claim == TYPED_CLAIM


def test_typed_exact_identity_must_support_a_claim_on_the_other_participant() -> None:
    source = _typed_discard_enabler()
    target = _typed_recursion(
        card_id=TYPED_SELF_RETURN_ID,
        paragraph=TYPED_SELF_RETURN_PARAGRAPH,
    )

    result = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_discard_clause()],
        target_clauses=[_self_return_clause(required_card_id=TYPED_DISCARD_ID)],
        mechanism=RECURSION_MECHANISM,
    )

    assert _contradiction_reason(result) == PREREQUISITE_CONTRADICTION_MESSAGE


def test_typed_exact_identity_binds_the_target_card() -> None:
    source = _typed_discard_enabler()
    target = _typed_recursion(
        card_id=TYPED_SELF_RETURN_ID,
        paragraph=TYPED_SELF_RETURN_PARAGRAPH,
    )

    projection = _typed_projection(
        _typed_pair_result(
            source=source,
            target=target,
            source_clauses=[_discard_clause()],
            target_clauses=[_self_return_clause()],
            mechanism=RECURSION_MECHANISM,
        )
    )
    unstated = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_discard_clause()],
        target_clauses=[_self_return_clause(required_card_id=None)],
        mechanism=RECURSION_MECHANISM,
    )

    clause = projection.target.prerequisites[0]
    assert clause.required_card_id == TYPED_SELF_RETURN_ID
    assert clause.required_card_id != source.card_id
    assert clause.evidence.card_id == TYPED_SELF_RETURN_ID
    assert clause.object_quote == "this card"
    assert projection.source.prerequisites[0].required_card_id is None
    # The selected phrase names the ability's own object, so leaving the exact card unstated is
    # not itself a contradiction: the clause stays evaluable without an exact-card binding.
    assert _typed_projection(unstated).target.prerequisites[0].required_card_id is None


@pytest.mark.parametrize(
    "document",
    (
        pytest.param({**_response(), "score": 0.9}, id="top-level-score"),
        pytest.param({**_response(), "adjustment": -1}, id="top-level-adjustment"),
        pytest.param(
            _response(
                prerequisite_status="complete",
                source_prerequisites=[_token_output_clause(weight=0.4)],
                target_prerequisites=[_anthem_clause()],
            ),
            id="clause-weight",
        ),
        pytest.param(
            _response(
                prerequisite_status="complete",
                source_prerequisites=[_token_output_clause(confidence=0.9)],
                target_prerequisites=[_anthem_clause()],
            ),
            id="clause-extra-key",
        ),
    ),
)
def test_typed_model_scoring_fields_are_malformed(document: dict[str, Any]) -> None:
    result = _parse_typed(
        response=document,
        source=_typed_token_source(),
        target=_typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH),
    )

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == MALFORMED_REASON
    assert result.relationship is None
    assert result.rejected is None


@pytest.mark.parametrize(
    "clause",
    (
        pytest.param(
            {
                key: value
                for key, value in _token_output_clause().items()
                if key != "object_quote"
            },
            id="clause-missing-field",
        ),
        pytest.param(_token_output_clause(subject="recipient"), id="clause-bad-enum"),
        pytest.param(
            _token_output_clause(operation_occurrence=-1),
            id="clause-negative-occurrence",
        ),
    ),
)
def test_typed_malformed_clauses_produce_the_fixed_outcome(clause: dict[str, Any]) -> None:
    document = _response(
        prerequisite_status="complete",
        source_prerequisites=[clause],
        target_prerequisites=[_anthem_clause()],
    )

    result = _parse_typed(
        response=document,
        source=_typed_token_source(),
        target=_typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH),
    )

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == MALFORMED_REASON


def test_typed_duplicate_clauses_are_malformed() -> None:
    clause = _token_output_clause()
    document = _response(
        prerequisite_status="complete",
        source_prerequisites=[clause, dict(clause)],
        target_prerequisites=[_anthem_clause()],
    )

    result = _parse_typed(
        response=document,
        source=_typed_token_source(),
        target=_typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH),
    )

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.relationship is None


def test_typed_projection_is_independent_of_model_prose() -> None:
    source = _typed_token_source()
    target = _typed_payoff(card_id=TYPED_ANTHEM_ID, paragraph=TYPED_ANTHEM_PARAGRAPH)
    plain = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_anthem_clause()],
        claim=TYPED_CLAIM,
    )
    scored = _typed_pair_result(
        source=source,
        target=target,
        source_clauses=[_token_output_clause()],
        target_clauses=[_anthem_clause()],
        claim=TYPED_SCORING_CLAIM,
    )

    plain_relationship = plain.relationship
    scored_relationship = scored.relationship
    assert plain_relationship is not None
    assert scored_relationship is not None
    assert plain_relationship.claim == TYPED_CLAIM
    assert scored_relationship.claim == TYPED_SCORING_CLAIM
    plain_projection = plain_relationship.prerequisite_projection
    assert plain_projection is not None
    assert scored_relationship.prerequisite_projection == plain_projection
    assert scored_relationship.prerequisite_projection is not None
    assert scored_relationship.prerequisite_projection.to_json() == plain_projection.to_json()

    reference = _record_relationship()
    described = replace(
        reference,
        claim=TYPED_SCORING_CLAIM,
        prerequisites=("Different descriptive prose with no typed content.",),
    )
    assert described.identity == reference.identity
    assert described.prerequisite_projection == reference.prerequisite_projection
    assert (
        described.to_json()["prerequisite_projection"]
        == reference.to_json()["prerequisite_projection"]
    )


def test_typed_mixed_direction_loot_records_keep_reversed_edges() -> None:
    forward = _loot_relationship(
        source_card_id=TYPED_LOOT_ONE_ID,
        target_card_id=TYPED_LOOT_TWO_ID,
    )
    reversed_edge = _loot_relationship(
        source_card_id=TYPED_LOOT_TWO_ID,
        target_card_id=TYPED_LOOT_ONE_ID,
    )

    assert forward.participants == reversed_edge.participants
    assert forward.participants == (TYPED_LOOT_ONE_ID, TYPED_LOOT_TWO_ID)
    assert forward.identity != reversed_edge.identity
    assert forward.identity[2][:3] == (
        TYPED_LOOT_ONE_ID,
        f"capability-batch-loot-{TYPED_LOOT_ONE_ID}",
        -1,
    )
    assert reversed_edge.identity[2][:3] == (
        TYPED_LOOT_TWO_ID,
        f"capability-batch-loot-{TYPED_LOOT_TWO_ID}",
        -1,
    )
    assert len({forward.identity, reversed_edge.identity}) == 2

    duplicate = CardRelationship.from_json(forward.to_json())
    assert duplicate == forward
    assert len({forward.identity, duplicate.identity}) == 1

    for relationship in (forward, reversed_edge):
        restored = CardRelationship.from_json(relationship.to_json())
        assert restored == relationship
        assert restored.prerequisite_projection == relationship.prerequisite_projection
        assert restored.prerequisite_projection is not None
        assert {
            clause.operation for clause in restored.prerequisite_projection.source.prerequisites
        } == {"draw", "discard"}
        assert {
            clause.operation for clause in restored.prerequisite_projection.target.prerequisites
        } == {"draw", "discard"}

