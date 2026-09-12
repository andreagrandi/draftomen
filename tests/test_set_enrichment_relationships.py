"""Behavior tests for the pinned relationship validation contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
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
from draftomen.semantic_enrichment import EnrichmentSources
from draftomen.semantic_enrichment_records import (
    FindingReview,
    FindingStatus,
    OracleEvidence,
    RejectedFinding,
)
from draftomen.semantic_roles import Role
from draftomen.set_enrichment_candidates import CANDIDATE_REASON, CandidatePackage
from draftomen.set_enrichment_extraction import (
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
MALFORMED_REASON = "response does not match relationship validation schema version 1."
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
    schema_version: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "verdict": verdict,
        "claim": claim,
        "reason": reason,
        "evidence": _coverage_evidence() if evidence is None else list(evidence),
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
    assert forward.contract_version == SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION
    assert forward.prompt_id == RELATIONSHIP_VALIDATION_PROMPT_ID
    assert forward.response_schema_id == RELATIONSHIP_VALIDATION_RESPONSE_SCHEMA_ID
    assert forward.response_schema_name == RELATIONSHIP_VALIDATION_SCHEMA_NAME
    assert RELATIONSHIP_VALIDATION_PROMPT_ID == "draftomen-relationship-validation-v1"
    assert (
        RELATIONSHIP_VALIDATION_RESPONSE_SCHEMA_ID
        == "draftomen-relationship-validation-response-v1"
    )
    assert RELATIONSHIP_VALIDATION_SCHEMA_NAME == "draftomen_relationship_validation_v1"
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
    }
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["schema_version"] == {
        "type": "integer",
        "enum": [SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION],
    }
    assert schema["properties"]["verdict"] == {
        "type": "string",
        "enum": ["accepted", "rejected", "uncertain"],
    }
    assert schema["properties"]["claim"] == {"type": "string", "minLength": 1}
    assert schema["properties"]["reason"] == {"type": ["string", "null"]}
    evidence_schema = schema["properties"]["evidence"]
    assert evidence_schema["type"] == "array"
    assert evidence_schema["items"]["additionalProperties"] is False
    assert set(evidence_schema["items"]["properties"]) == {"card_id", "face_index", "quote"}
    assert set(evidence_schema["items"]["required"]) == set(evidence_schema["items"]["properties"])


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

    assert set(prompt) == {"contract_version", "set_code", "mechanism", "source", "target"}
    assert prompt["contract_version"] == SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION
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
            _content(_response()).replace('"schema_version": 1', '"schema_version": NaN'),
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
            _content(_response(schema_version=2)),
            id="unsupported-schema-version",
        ),
        pytest.param(
            _content(_response(verdict="partial")),
            id="unsupported-verdict",
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
