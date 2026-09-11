"""Behavior tests for the pure guide and card capability extraction contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
import re
from typing import Any

import pytest

from draftomen.carddb import CardFace, CardInfo
from draftomen.openrouter_client import OpenRouterClient
from draftomen.semantic_capability_records import (
    CapabilityPrerequisite,
    CapabilityQuantity,
    CapabilityZone,
    CardCapability,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import EnrichmentSources, GuideSource, card_source_sha256
from draftomen.semantic_enrichment_records import (
    CardRelationship,
    FindingReview,
    FindingStatus,
    GuideClaim,
    GuideEvidence,
    OracleEvidence,
    RejectedFinding,
    SemanticEnrichmentError,
)
from draftomen.semantic_roles import Role
import draftomen.set_enrichment_extraction as extraction_module
from draftomen.set_enrichment_extraction import (
    CARD_CAPABILITY_EXTRACTION_PROMPT_ID,
    CARD_CAPABILITY_EXTRACTION_RESPONSE_SCHEMA_ID,
    CARD_CAPABILITY_EXTRACTION_SCHEMA_NAME,
    GUIDE_EXTRACTION_PROMPT_ID,
    GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID,
    GUIDE_EXTRACTION_SCHEMA_NAME,
    SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
    CardCapabilityExtractionResult,
    ExtractionOutcome,
    ExtractionRequest,
    GuideExtractionResult,
    SetEnrichmentExtractionError,
    build_card_capability_extraction_request,
    build_guide_extraction_request,
    parse_card_capability_extraction_response,
    parse_guide_extraction_response,
)


SET_CODE = "tst"
GUIDE_ID = "guide-1"
FOREIGN_GUIDE_ID = "guide-2"
GUIDE_URL = "https://guides.example.test/tst-review"
RETRIEVED_AT = "2026-09-01T12:00:00Z"
RUN_ID = "run-1"

ALPHA_ID = 101
BETA_ID = 102
GAMMA_ID = 103
UNKNOWN_CARD_ID = 999

CARD_ORACLE_TEXT = "Flying. When this creature enters, draw a card."

FORMAT_SENTENCE = "Midrange decks in this format lean on cheap interaction and resilient threats."
FORMAT_CLAIM = "Midrange is the format's default fair deck."
MECHANIC_SENTENCE = (
    "The squad mechanic lets you pay extra mana to create additional tokens "
    "when the permanent enters."
)
MECHANIC_CLAIM = "Squad converts extra mana into additional tokens as the permanent enters."
ARCHETYPE_SENTENCE = "The control archetype wins long games by trading resources efficiently."
INTERACTION_SENTENCE = "Alpha pairs with Beta to close games quickly once the board stalls."
OUT_OF_SET_CLAIM = "Alpha pairs with Delta to close games quickly."
FABRICATED_QUOTE = "Alpha tutors for Beta every turn."
NEGATED_LINE_BREAK_QUOTE = "Alpha\nis unbeatable in the mirror match."
NEGATED_LINE_BREAK_CLAIM = "Alpha is unbeatable in the mirror match."
NEGATED_HTML_QUOTE = "Alpha always wins</em>."
NEGATED_HTML_CLAIM = "Alpha always wins."

GUIDE_TEXT = (
    "\n".join(
        (
            FORMAT_SENTENCE,
            MECHANIC_SENTENCE,
            ARCHETYPE_SENTENCE,
            INTERACTION_SENTENCE,
            "Do not assume that Alpha",
            "is unbeatable in the mirror match.",
            "Do not trust the claim that <em>Alpha always wins</em>.",
        )
    )
    + "\n"
)

MALFORMED_REASON = "response does not match guide extraction schema version 1."
SEMANTIC_REVIEW_REASON = "guide claim requires semantic review beyond exact-source validation."
EVIDENCE_GUIDE_REASON = "guide evidence does not reference the selected guide."
EVIDENCE_QUOTE_REASON = "guide evidence quote is not an exact source substring."
UNKNOWN_CARD_REASON = "guide claim references a card outside the frozen source set."
CARD_NAME_REASON = "referenced card IDs do not match card names stated in the claim."

MODEL_REJECTION_REASON = "The guide does not support this interaction."
MODEL_UNCERTAINTY_REASON = "The quote is exact but the interpretation needs review."

PLAIN_CARD_ID = 201
TWO_FACE_CARD_ID = 202
NULL_FACE_NAME_CARD_ID = 203
FOREIGN_CARD_ID = 999

PLAIN_CARD_NAME = "Solo Sentinel"
PLAIN_CARD_TYPE_LINE = "Creature — Bird"
PLAIN_CARD_TEXT = "Flying. When this creature enters, draw a card."
TWO_FACE_CARD_NAME = "Alpha // Beta"
TWO_FACE_CARD_TYPE_LINE = "Creature — Soldier // Enchantment — Aura"
FRONT_FACE_NAME = "Alpha"
FRONT_FACE_TYPE_LINE = "Creature — Soldier"
FRONT_FACE_TEXT = "Flying. Whenever this creature attacks, draw a card."
BACK_FACE_NAME = "Beta"
BACK_FACE_TYPE_LINE = "Enchantment — Aura"
BACK_FACE_TEXT = "At the beginning of your upkeep, each opponent mills two cards."
TWO_FACE_CARD_TEXT = f"{FRONT_FACE_TEXT} // {BACK_FACE_TEXT}"
NULL_FACE_NAME_CARD_NAME = "Gamma // Delta"
NULL_FACE_NAME_CARD_TYPE_LINE = "Instant // Sorcery"
NULL_FACE_NAME_FRONT_TYPE_LINE = "Instant"
NULL_FACE_NAME_FACE_TEXT = "Draw a card, then discard a card."
NULL_FACE_NAME_BACK_FACE_NAME = "Delta"
NULL_FACE_NAME_BACK_TYPE_LINE = "Sorcery"
NULL_FACE_NAME_BACK_FACE_TEXT = "Add one mana of any color."
NULL_FACE_NAME_CARD_TEXT = f"{NULL_FACE_NAME_FACE_TEXT} // {NULL_FACE_NAME_BACK_FACE_TEXT}"

FRONT_EFFECT_QUOTE = "draw a card"
BACK_TRIGGER_QUOTE = "At the beginning of your upkeep"
BACK_MILL_QUOTE = "each opponent mills two cards"
SURROGATE_RELATION = chr(0xD800)

CARD_MALFORMED_REASON = "response does not match card capability extraction schema version 1."
CAPABILITY_REVIEW_REASON = "capability requires semantic review beyond exact-source validation."
CAPABILITY_VOCABULARY_REASON = "capability or condition uses unsupported vocabulary."
CAPABILITY_CARD_ID_REASON = "capability references a card other than the selected canonical card."
CAPABILITY_CARD_NAME_REASON = "capability card name does not match the selected canonical card."
CAPABILITY_FACE_REASON = "capability face identity does not match the selected canonical card."
CAPABILITY_EVIDENCE_OWNER_REASON = "Oracle evidence does not belong to the selected card face."
CAPABILITY_EVIDENCE_QUOTE_REASON = "Oracle evidence quote is not an exact source substring."
CARD_SELECTION_ERROR = "card_id must identify exactly one frozen canonical card."


def _card(grp_id: int, name: str) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=("U", "B"),
        mana_value=3.0,
        rarity="uncommon",
        types=("Creature",),
        oracle_text=CARD_ORACLE_TEXT,
        set_code=SET_CODE,
    )


def _guide() -> GuideSource:
    return GuideSource(
        guide_id=GUIDE_ID,
        url=GUIDE_URL,
        text=GUIDE_TEXT,
        retrieved_at=RETRIEVED_AT,
    )


def _sources(card_order: tuple[int, ...] = (ALPHA_ID, BETA_ID, GAMMA_ID)) -> EnrichmentSources:
    names = {ALPHA_ID: "Alpha", BETA_ID: "Beta", GAMMA_ID: "Gamma"}
    return EnrichmentSources(
        set_code=SET_CODE,
        cards=tuple(_card(card_id, names[card_id]) for card_id in card_order),
        guides=(_guide(),),
    )


@pytest.fixture
def sources() -> EnrichmentSources:
    return _sources()


def _evidence(*, guide_id: str = GUIDE_ID, quote: str = INTERACTION_SENTENCE) -> dict[str, str]:
    return {"guide_id": guide_id, "quote": quote}


def _finding(
    *,
    finding_id: str,
    category: str,
    name: str,
    claim: str,
    card_ids: list[int],
    quote: str,
    status: str = "accepted",
    reason: str | None = None,
    guide_id: str = GUIDE_ID,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "category": category,
        "name": name,
        "claim": claim,
        "card_ids": list(card_ids),
        "evidence": [_evidence(guide_id=guide_id, quote=quote)],
        "review": {"status": status, "reason": reason},
    }


def _response(findings: list[dict[str, Any]], *, schema_version: int = 1) -> dict[str, Any]:
    return {"schema_version": schema_version, "findings": list(findings)}


def _content(response: dict[str, Any] | list[Any]) -> str:
    return json.dumps(response)


def _format_finding() -> dict[str, Any]:
    return _finding(
        finding_id="claim-format",
        category="format_finding",
        name="Midrange core",
        claim=FORMAT_CLAIM,
        card_ids=[],
        quote=FORMAT_SENTENCE,
    )


def _mechanic_finding() -> dict[str, Any]:
    return _finding(
        finding_id="claim-mechanic",
        category="mechanic",
        name="Squad",
        claim=MECHANIC_CLAIM,
        card_ids=[],
        quote=MECHANIC_SENTENCE,
    )


def _archetype_finding() -> dict[str, Any]:
    return _finding(
        finding_id="claim-archetype",
        category="archetype",
        name="Control",
        claim=ARCHETYPE_SENTENCE,
        card_ids=[],
        quote=ARCHETYPE_SENTENCE,
    )


def _interaction_finding() -> dict[str, Any]:
    return _finding(
        finding_id="claim-interaction",
        category="strategy",
        name="Alpha and Beta closing games",
        claim=INTERACTION_SENTENCE,
        card_ids=[BETA_ID, ALPHA_ID],
        quote=INTERACTION_SENTENCE,
    )


def _variant(**overrides: Any) -> dict[str, Any]:
    finding = _interaction_finding()
    finding.update(overrides)
    return finding


def _claim(
    finding_id: str,
    *,
    status: FindingStatus = FindingStatus.UNCERTAIN,
    reason: str | None = SEMANTIC_REVIEW_REASON,
) -> GuideClaim:
    return GuideClaim(
        finding_id=finding_id,
        category="strategy",
        name="Alpha and Beta closing games",
        claim=INTERACTION_SENTENCE,
        card_ids=(ALPHA_ID, BETA_ID),
        evidence=(GuideEvidence(guide_id=GUIDE_ID, quote=INTERACTION_SENTENCE),),
        review=FindingReview(status=status, reason=reason),
        run_id=RUN_ID,
    )


class _DerivedClaim(GuideClaim):
    """Subclass used to prove exact concrete finding types are required."""


def _derived_claim(finding_id: str) -> _DerivedClaim:
    """Build an accepted-review claim of a derived type."""
    base = _claim(finding_id, status=FindingStatus.ACCEPTED, reason=None)
    return _DerivedClaim(
        finding_id=base.finding_id,
        category=base.category,
        name=base.name,
        claim=base.claim,
        card_ids=base.card_ids,
        evidence=base.evidence,
        review=base.review,
        run_id=base.run_id,
    )


def _rejected_finding(finding_id: str, *, source_kind: str = "guide") -> RejectedFinding:
    return RejectedFinding(
        finding_id=finding_id,
        source_kind=source_kind,
        summary=INTERACTION_SENTENCE,
        reason=EVIDENCE_QUOTE_REASON,
        run_id=RUN_ID,
    )


def _result(**overrides: Any) -> GuideExtractionResult:
    values: dict[str, Any] = {
        "outcome": ExtractionOutcome.SUCCESS,
        "accepted_findings": (),
        "uncertain_findings": (),
        "rejected_findings": (),
        "malformed_reason": None,
    }
    values.update(overrides)
    return GuideExtractionResult(**values)


def _simple_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"value": {"type": "string", "minLength": 1}},
        "required": ["value"],
        "additionalProperties": False,
    }


def _cyclic_schema() -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object"}
    schema["properties"] = schema
    return schema


def _request(**overrides: Any) -> ExtractionRequest:
    fields: dict[str, Any] = {
        "contract_version": SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
        "prompt_id": GUIDE_EXTRACTION_PROMPT_ID,
        "system_prompt": "Extract only claims stated in the supplied frozen guide.",
        "user_prompt": '{"cards":[],"contract_version":1,"set_code":"tst"}',
        "response_schema_id": GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID,
        "response_schema_name": GUIDE_EXTRACTION_SCHEMA_NAME,
        "schema": _simple_schema(),
    }
    fields.update(overrides)
    return ExtractionRequest(**fields)


def _object_nodes(value: Any) -> list[dict[str, Any]]:
    """Collect every object-typed node in one JSON schema."""
    if isinstance(value, dict):
        nodes = [value] if value.get("type") == "object" else []
        for item in value.values():
            nodes.extend(_object_nodes(item))
        return nodes
    if isinstance(value, list):
        return [node for item in value for node in _object_nodes(item)]
    return []


def _parse(content: str | None, sources: EnrichmentSources) -> GuideExtractionResult:
    return parse_guide_extraction_response(
        content=content,  # type: ignore[arg-type]
        sources=sources,
        guide_id=GUIDE_ID,
        run_id=RUN_ID,
    )


def _plain_card() -> CardInfo:
    return CardInfo(
        grp_id=PLAIN_CARD_ID,
        name=PLAIN_CARD_NAME,
        colors=("W",),
        mana_value=2.0,
        rarity="common",
        types=("Creature",),
        oracle_text=PLAIN_CARD_TEXT,
        type_line=PLAIN_CARD_TYPE_LINE,
        set_code=SET_CODE,
    )


def _two_face_card_faces() -> tuple[CardFace, ...]:
    return (
        CardFace(
            name=FRONT_FACE_NAME,
            type_line=FRONT_FACE_TYPE_LINE,
            oracle_text=FRONT_FACE_TEXT,
        ),
        CardFace(
            name=BACK_FACE_NAME,
            type_line=BACK_FACE_TYPE_LINE,
            oracle_text=BACK_FACE_TEXT,
        ),
    )


def _two_face_card(
    *,
    name: str = TWO_FACE_CARD_NAME,
    faces: tuple[CardFace, ...] | None = None,
) -> CardInfo:
    return CardInfo(
        grp_id=TWO_FACE_CARD_ID,
        name=name,
        colors=("U", "B"),
        mana_value=4.0,
        rarity="rare",
        types=("Creature",),
        oracle_text=TWO_FACE_CARD_TEXT,
        type_line=TWO_FACE_CARD_TYPE_LINE,
        layout="transform",
        faces=_two_face_card_faces() if faces is None else faces,
        set_code=SET_CODE,
    )


def _null_face_name_card() -> CardInfo:
    return CardInfo(
        grp_id=NULL_FACE_NAME_CARD_ID,
        name=NULL_FACE_NAME_CARD_NAME,
        colors=("R",),
        mana_value=3.0,
        rarity="uncommon",
        types=("Instant",),
        oracle_text=NULL_FACE_NAME_CARD_TEXT,
        type_line=NULL_FACE_NAME_CARD_TYPE_LINE,
        layout="split",
        faces=(
            CardFace(
                name=None,
                type_line=NULL_FACE_NAME_FRONT_TYPE_LINE,
                oracle_text=NULL_FACE_NAME_FACE_TEXT,
            ),
            CardFace(
                name=NULL_FACE_NAME_BACK_FACE_NAME,
                type_line=NULL_FACE_NAME_BACK_TYPE_LINE,
                oracle_text=NULL_FACE_NAME_BACK_FACE_TEXT,
            ),
        ),
        set_code=SET_CODE,
    )


def _equivalent_two_face_card() -> CardInfo:
    """Build the same canonical projection from different raw card metadata."""
    return CardInfo(
        grp_id=TWO_FACE_CARD_ID,
        name=f"  {TWO_FACE_CARD_NAME}  ",
        colors=(),
        mana_value=None,
        rarity="mythic",
        types=("Enchantment",),
        image_uri="https://cards.example.test/alpha-beta.png",
        keywords=("Flying",),
        oracle_text=TWO_FACE_CARD_TEXT,
        type_line=TWO_FACE_CARD_TYPE_LINE,
        layout="transform",
        faces=(
            CardFace(
                name=FRONT_FACE_NAME,
                type_line=FRONT_FACE_TYPE_LINE,
                oracle_text=FRONT_FACE_TEXT,
                mana_cost="{3}{U}",
            ),
            CardFace(
                name=BACK_FACE_NAME,
                type_line=BACK_FACE_TYPE_LINE,
                oracle_text=BACK_FACE_TEXT,
                power="4",
                toughness="4",
            ),
        ),
        set_code=SET_CODE,
    )


def _equivalent_null_face_name_card() -> CardInfo:
    """Build the same canonical projection from different raw card metadata."""
    return CardInfo(
        grp_id=NULL_FACE_NAME_CARD_ID,
        name=NULL_FACE_NAME_CARD_NAME,
        colors=(),
        mana_value=None,
        rarity="rare",
        types=("Sorcery",),
        keywords=("Flashback",),
        oracle_text=NULL_FACE_NAME_CARD_TEXT,
        type_line=NULL_FACE_NAME_CARD_TYPE_LINE,
        layout="split",
        faces=(
            CardFace(
                name=None,
                type_line=NULL_FACE_NAME_FRONT_TYPE_LINE,
                oracle_text=NULL_FACE_NAME_FACE_TEXT,
                mana_cost="{1}{R}",
            ),
            CardFace(
                name=NULL_FACE_NAME_BACK_FACE_NAME,
                type_line=NULL_FACE_NAME_BACK_TYPE_LINE,
                oracle_text=NULL_FACE_NAME_BACK_FACE_TEXT,
            ),
        ),
        set_code=SET_CODE,
    )


def _card_sources(*cards: CardInfo) -> EnrichmentSources:
    return EnrichmentSources(set_code=SET_CODE, cards=tuple(cards), guides=(_guide(),))


def _capability_sources() -> EnrichmentSources:
    return _card_sources(_plain_card(), _two_face_card(), _null_face_name_card())


@pytest.fixture
def capability_sources() -> EnrichmentSources:
    return _capability_sources()


def _evidence_entry(
    *,
    card_id: int = TWO_FACE_CARD_ID,
    face_index: int | None = 1,
    quote: str = BACK_MILL_QUOTE,
) -> dict[str, Any]:
    return {"card_id": card_id, "face_index": face_index, "quote": quote}


def _quantity(*, value: int | None = None, relation: str = "variable") -> dict[str, Any]:
    return {"value": value, "relation": relation}


def _prerequisite_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": "cost",
        "quantity": None,
        "timing": None,
        "source_zone": None,
        "destination_zone": None,
        "evidence": _evidence_entry(),
    }
    entry.update(overrides)
    return entry


def _capability_candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "finding_id": "capability-draw",
        "card_id": TWO_FACE_CARD_ID,
        "card_name": TWO_FACE_CARD_NAME,
        "face_index": 1,
        "face_name": BACK_FACE_NAME,
        "role": "draw",
        "quantity": None,
        "timing": None,
        "source_zone": None,
        "destination_zone": None,
        "prerequisites": [],
        "evidence": [_evidence_entry()],
        "review": {"status": "accepted", "reason": None},
    }
    candidate.update(overrides)
    return candidate


def _capability_response(
    capabilities: list[Any],
    *,
    schema_version: int = 1,
) -> dict[str, Any]:
    return {"schema_version": schema_version, "capabilities": list(capabilities)}


def _parse_card(
    content: str | None,
    sources: EnrichmentSources,
    card_id: int = TWO_FACE_CARD_ID,
) -> CardCapabilityExtractionResult:
    return parse_card_capability_extraction_response(
        content=content,  # type: ignore[arg-type]
        sources=sources,
        card_id=card_id,
        run_id=RUN_ID,
    )


def _reviewed_capability(
    finding_id: str,
    *,
    status: FindingStatus = FindingStatus.UNCERTAIN,
    reason: str | None = CAPABILITY_REVIEW_REASON,
) -> CardCapability:
    return CardCapability(
        finding_id=finding_id,
        card_id=TWO_FACE_CARD_ID,
        card_name=TWO_FACE_CARD_NAME,
        face_index=1,
        face_name=BACK_FACE_NAME,
        role=Role.DRAW,
        quantity=None,
        timing=None,
        source_zone=None,
        destination_zone=None,
        prerequisites=(),
        evidence=(OracleEvidence(card_id=TWO_FACE_CARD_ID, face_index=1, quote=BACK_FACE_TEXT),),
        review=FindingReview(status=status, reason=reason),
        run_id=RUN_ID,
    )


class _DerivedCapability(CardCapability):
    """Subclass used to prove exact concrete capability types are required."""


def _derived_capability(finding_id: str) -> _DerivedCapability:
    """Build an uncertain capability of a derived type."""
    base = _reviewed_capability(finding_id)
    return _DerivedCapability(
        finding_id=base.finding_id,
        card_id=base.card_id,
        card_name=base.card_name,
        face_index=base.face_index,
        face_name=base.face_name,
        role=base.role,
        quantity=base.quantity,
        timing=base.timing,
        source_zone=base.source_zone,
        destination_zone=base.destination_zone,
        prerequisites=base.prerequisites,
        evidence=base.evidence,
        review=base.review,
        run_id=base.run_id,
    )


def _rejected_capability(finding_id: str, *, source_kind: str = "oracle") -> RejectedFinding:
    return RejectedFinding(
        finding_id=finding_id,
        source_kind=source_kind,
        summary="draw",
        reason=CAPABILITY_EVIDENCE_QUOTE_REASON,
        run_id=RUN_ID,
    )


def _card_result(**overrides: Any) -> CardCapabilityExtractionResult:
    values: dict[str, Any] = {
        "outcome": ExtractionOutcome.SUCCESS,
        "accepted_capabilities": (),
        "uncertain_capabilities": (),
        "rejected_capabilities": (),
        "malformed_reason": None,
    }
    values.update(overrides)
    return CardCapabilityExtractionResult(**values)


def _prerequisite_record(
    *,
    card_id: int = TWO_FACE_CARD_ID,
    face_index: int | None = 1,
    quote: str = BACK_MILL_QUOTE,
) -> CapabilityPrerequisite:
    """Build one cost prerequisite bound to an exact card face."""
    return CapabilityPrerequisite(
        kind=PrerequisiteKind.COST,
        quantity=None,
        timing=None,
        source_zone=None,
        destination_zone=None,
        evidence=OracleEvidence(card_id=card_id, face_index=face_index, quote=quote),
    )


def _capability_with_prerequisite(
    prerequisite: CapabilityPrerequisite,
    *,
    card_id: int = TWO_FACE_CARD_ID,
    card_name: str = TWO_FACE_CARD_NAME,
    face_index: int | None = 1,
    face_name: str | None = BACK_FACE_NAME,
    quote: str = BACK_MILL_QUOTE,
) -> CardCapability:
    """Build one uncertain capability around a single prerequisite."""
    evidence = OracleEvidence(card_id=card_id, face_index=face_index, quote=quote)
    return CardCapability(
        finding_id="capability-owned",
        card_id=card_id,
        card_name=card_name,
        face_index=face_index,
        face_name=face_name,
        role=Role.DRAW,
        quantity=None,
        timing=None,
        source_zone=None,
        destination_zone=None,
        prerequisites=(prerequisite,),
        evidence=(evidence,),
        review=FindingReview(status=FindingStatus.UNCERTAIN, reason=CAPABILITY_REVIEW_REASON),
        run_id=RUN_ID,
    )


def test_public_surface_pins_contract_values_and_outcomes() -> None:
    assert set(extraction_module.__all__) == {
        "CARD_CAPABILITY_EXTRACTION_PROMPT_ID",
        "CARD_CAPABILITY_EXTRACTION_RESPONSE_SCHEMA_ID",
        "CARD_CAPABILITY_EXTRACTION_SCHEMA_NAME",
        "CardCapabilityExtractionResult",
        "ExtractionOutcome",
        "ExtractionRequest",
        "GUIDE_EXTRACTION_PROMPT_ID",
        "GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID",
        "GUIDE_EXTRACTION_SCHEMA_NAME",
        "GuideExtractionResult",
        "SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION",
        "SetEnrichmentExtractionError",
        "build_card_capability_extraction_request",
        "build_guide_extraction_request",
        "parse_card_capability_extraction_response",
        "parse_guide_extraction_response",
    }
    assert isinstance(SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION, int)
    assert SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION == 1
    assert GUIDE_EXTRACTION_PROMPT_ID == "draftomen-guide-extraction-v1"
    assert GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID == "draftomen-guide-extraction-response-v1"
    assert GUIDE_EXTRACTION_SCHEMA_NAME == "draftomen_guide_extraction_v1"
    assert CARD_CAPABILITY_EXTRACTION_PROMPT_ID == "draftomen-card-capability-extraction-v1"
    assert (
        CARD_CAPABILITY_EXTRACTION_RESPONSE_SCHEMA_ID
        == "draftomen-card-capability-extraction-response-v1"
    )
    assert CARD_CAPABILITY_EXTRACTION_SCHEMA_NAME == "draftomen_card_capability_extraction_v1"
    assert ExtractionOutcome.SUCCESS.value == "success"
    assert ExtractionOutcome.MALFORMED.value == "malformed"
    assert issubclass(SetEnrichmentExtractionError, ValueError)


def test_four_model_accepted_findings_parse_as_ordered_uncertain_claims(
    sources: EnrichmentSources,
) -> None:
    response = _response(
        [_mechanic_finding(), _interaction_finding(), _format_finding(), _archetype_finding()]
    )

    result = _parse(_content(response), sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.accepted_findings == ()
    assert result.rejected_findings == ()
    assert tuple(claim.finding_id for claim in result.uncertain_findings) == (
        "claim-archetype",
        "claim-format",
        "claim-interaction",
        "claim-mechanic",
    )
    assert result.guide_claims == result.uncertain_findings
    assert {claim.review.status for claim in result.uncertain_findings} == {FindingStatus.UNCERTAIN}
    assert {claim.review.reason for claim in result.uncertain_findings} == {SEMANTIC_REVIEW_REASON}
    assert {claim.claim for claim in result.uncertain_findings} == {
        FORMAT_CLAIM,
        MECHANIC_CLAIM,
        ARCHETYPE_SENTENCE,
        INTERACTION_SENTENCE,
    }

    claims = {claim.finding_id: claim for claim in result.uncertain_findings}
    assert claims["claim-format"].category == "format_finding"
    assert claims["claim-format"].name == "Midrange core"
    assert claims["claim-format"].card_ids == ()
    assert claims["claim-format"].evidence == (
        GuideEvidence(guide_id=GUIDE_ID, quote=FORMAT_SENTENCE),
    )
    assert claims["claim-mechanic"].category == "mechanic"
    assert claims["claim-mechanic"].name == "Squad"
    assert claims["claim-mechanic"].card_ids == ()
    assert claims["claim-mechanic"].evidence == (
        GuideEvidence(guide_id=GUIDE_ID, quote=MECHANIC_SENTENCE),
    )
    assert claims["claim-archetype"].category == "archetype"
    assert claims["claim-archetype"].name == "Control"
    assert claims["claim-archetype"].card_ids == ()
    assert claims["claim-archetype"].evidence == (
        GuideEvidence(guide_id=GUIDE_ID, quote=ARCHETYPE_SENTENCE),
    )

    interaction_claim = claims["claim-interaction"]
    assert isinstance(interaction_claim, GuideClaim)
    assert not isinstance(interaction_claim, CardRelationship)
    assert interaction_claim.category == "strategy"
    assert interaction_claim.name == "Alpha and Beta closing games"
    assert interaction_claim.claim == INTERACTION_SENTENCE
    assert interaction_claim.card_ids == (ALPHA_ID, BETA_ID)
    assert interaction_claim.evidence == (
        GuideEvidence(guide_id=GUIDE_ID, quote=INTERACTION_SENTENCE),
    )
    assert {claim.run_id for claim in result.uncertain_findings} == {RUN_ID}


def test_referenced_card_id_order_does_not_change_the_outcome(sources: EnrichmentSources) -> None:
    ascending = _variant(card_ids=[ALPHA_ID, BETA_ID])
    descending = _variant(card_ids=[BETA_ID, ALPHA_ID])

    ascending_result = _parse(_content(_response([ascending])), sources)
    descending_result = _parse(_content(_response([descending])), sources)

    assert ascending_result.outcome is ExtractionOutcome.SUCCESS
    assert ascending_result.rejected_findings == ()
    assert ascending_result.uncertain_findings[0].card_ids == (ALPHA_ID, BETA_ID)
    assert descending_result.outcome is ExtractionOutcome.SUCCESS
    assert descending_result.rejected_findings == ()
    assert descending_result.uncertain_findings[0].card_ids == (ALPHA_ID, BETA_ID)


def test_model_accepted_with_a_reason_is_malformed(sources: EnrichmentSources) -> None:
    finding = _variant(
        evidence=[_evidence(quote=ARCHETYPE_SENTENCE)],
        review={"status": "accepted", "reason": "Looks correct."},
    )

    result = _parse(_content(_response([finding])), sources)

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == MALFORMED_REASON
    assert result.accepted_findings == ()
    assert result.uncertain_findings == ()
    assert result.rejected_findings == ()
    assert result.guide_claims == ()


@pytest.mark.parametrize(
    ("overrides", "expected_kind", "expected_reason"),
    (
        pytest.param(
            {"evidence": [_evidence(quote=FABRICATED_QUOTE)]},
            "rejected",
            EVIDENCE_QUOTE_REASON,
            id="fabricated-quote",
        ),
        pytest.param(
            {"evidence": [_evidence(guide_id=FOREIGN_GUIDE_ID)]},
            "rejected",
            EVIDENCE_GUIDE_REASON,
            id="foreign-guide-id",
        ),
        pytest.param(
            {"claim": OUT_OF_SET_CLAIM, "card_ids": [ALPHA_ID, UNKNOWN_CARD_ID]},
            "rejected",
            UNKNOWN_CARD_REASON,
            id="card-outside-frozen-set",
        ),
        pytest.param(
            {"review": {"status": "rejected", "reason": MODEL_REJECTION_REASON}},
            "rejected",
            MODEL_REJECTION_REASON,
            id="model-rejection-keeps-its-reason",
        ),
        pytest.param(
            {"card_ids": [ALPHA_ID]},
            "rejected",
            CARD_NAME_REASON,
            id="omitted-in-set-card",
        ),
        pytest.param(
            {"card_ids": [ALPHA_ID, GAMMA_ID]},
            "rejected",
            CARD_NAME_REASON,
            id="unrelated-in-set-card",
        ),
        pytest.param(
            {"evidence": [_evidence(quote=ARCHETYPE_SENTENCE)]},
            "uncertain",
            SEMANTIC_REVIEW_REASON,
            id="accepted-with-unrelated-exact-quote",
        ),
        pytest.param(
            {
                "claim": NEGATED_LINE_BREAK_CLAIM,
                "card_ids": [ALPHA_ID],
                "evidence": [_evidence(quote=NEGATED_LINE_BREAK_QUOTE)],
            },
            "uncertain",
            SEMANTIC_REVIEW_REASON,
            id="negated-context-across-line-break",
        ),
        pytest.param(
            {
                "claim": NEGATED_HTML_CLAIM,
                "card_ids": [ALPHA_ID],
                "evidence": [_evidence(quote=NEGATED_HTML_QUOTE)],
            },
            "uncertain",
            SEMANTIC_REVIEW_REASON,
            id="negated-context-inside-inline-html",
        ),
        pytest.param(
            {"review": {"status": "uncertain", "reason": MODEL_UNCERTAINTY_REASON}},
            "uncertain",
            MODEL_UNCERTAINTY_REASON,
            id="model-uncertainty-keeps-its-reason",
        ),
    ),
)
def test_source_and_review_matrix(
    sources: EnrichmentSources,
    overrides: dict[str, Any],
    expected_kind: str,
    expected_reason: str,
) -> None:
    finding = _variant(**overrides)

    result = _parse(_content(_response([finding])), sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.accepted_findings == ()
    if expected_kind == "rejected":
        assert result.uncertain_findings == ()
        assert len(result.rejected_findings) == 1
        rejected = result.rejected_findings[0]
        assert rejected.source_kind == "guide"
        assert rejected.finding_id == finding["finding_id"]
        assert rejected.summary == finding["claim"]
        assert rejected.reason == expected_reason
        assert rejected.run_id == RUN_ID
    else:
        assert result.rejected_findings == ()
        assert len(result.uncertain_findings) == 1
        claim = result.uncertain_findings[0]
        assert claim.review == FindingReview(
            status=FindingStatus.UNCERTAIN,
            reason=expected_reason,
        )
        assert claim.finding_id == finding["finding_id"]
        assert claim.claim == finding["claim"]
        assert claim.run_id == RUN_ID


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    (
        pytest.param(
            {
                "evidence": [_evidence(guide_id=FOREIGN_GUIDE_ID), _evidence(quote=FABRICATED_QUOTE)],
                "card_ids": [UNKNOWN_CARD_ID],
            },
            EVIDENCE_GUIDE_REASON,
            id="foreign-guide-outranks-quote-and-unknown-card",
        ),
        pytest.param(
            {"evidence": [_evidence(quote=FABRICATED_QUOTE)], "card_ids": [UNKNOWN_CARD_ID]},
            EVIDENCE_QUOTE_REASON,
            id="fabricated-quote-outranks-unknown-card",
        ),
        pytest.param(
            {"card_ids": [ALPHA_ID, UNKNOWN_CARD_ID]},
            UNKNOWN_CARD_REASON,
            id="unknown-card-outranks-card-name-mismatch",
        ),
        pytest.param(
            {
                "card_ids": [ALPHA_ID],
                "review": {"status": "rejected", "reason": MODEL_REJECTION_REASON},
            },
            MODEL_REJECTION_REASON,
            id="source-valid-rejection-keeps-the-model-reason",
        ),
        pytest.param(
            {
                "evidence": [_evidence(quote=FABRICATED_QUOTE)],
                "review": {"status": "rejected", "reason": MODEL_REJECTION_REASON},
            },
            EVIDENCE_QUOTE_REASON,
            id="invalid-evidence-outranks-the-model-reason",
        ),
        pytest.param(
            {
                "card_ids": [ALPHA_ID],
                "review": {"status": "uncertain", "reason": MODEL_UNCERTAINTY_REASON},
            },
            CARD_NAME_REASON,
            id="uncertain-still-requires-the-exact-named-card-set",
        ),
    ),
)
def test_diagnostic_precedence_pins_the_first_applicable_reason(
    sources: EnrichmentSources,
    overrides: dict[str, Any],
    expected_reason: str,
) -> None:
    finding = _variant(**overrides)

    result = _parse(_content(_response([finding])), sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.accepted_findings == ()
    assert result.uncertain_findings == ()
    assert len(result.rejected_findings) == 1
    rejected = result.rejected_findings[0]
    assert rejected.reason == expected_reason
    assert rejected.finding_id == finding["finding_id"]
    assert rejected.summary == finding["claim"]
    assert rejected.run_id == RUN_ID


def test_trusted_argument_failures_raise_with_pinned_messages(sources: EnrichmentSources) -> None:
    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_guide_extraction_response(
            content=_content(_response([])),
            sources=None,  # type: ignore[arg-type]
            guide_id=GUIDE_ID,
            run_id=RUN_ID,
        )
    assert str(error.value) == "sources must be an EnrichmentSources record."

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_guide_extraction_response(
            content=_content(_response([])),
            sources=sources,
            guide_id="guide-missing",
            run_id=RUN_ID,
        )
    assert str(error.value) == "guide_id must identify exactly one frozen guide source."

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_guide_extraction_response(
            content=_content(_response([])),
            sources=sources,
            guide_id=GUIDE_ID,
            run_id="   ",
        )
    assert str(error.value) == "run_id must be a nonblank string."

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_guide_extraction_response(
            content=_content(_response([])),
            sources=sources,
            guide_id="bad\ud800",
            run_id=RUN_ID,
        )
    assert str(error.value) == "guide_id must identify exactly one frozen guide source."

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_guide_extraction_response(
            content=_content(_response([])),
            sources=sources,
            guide_id=GUIDE_ID,
            run_id="bad\ud800",
        )
    assert str(error.value) == "run_id must be a nonblank string."

    malformed = _parse("not json", sources)
    assert malformed.outcome is ExtractionOutcome.MALFORMED


@pytest.mark.parametrize(
    "content",
    (
        pytest.param(None, id="non-string-content"),
        pytest.param("", id="blank-content"),
        pytest.param("not json", id="invalid-json"),
        pytest.param("bad\ud800", id="non-utf8-content"),
        pytest.param(_content([]), id="root-array"),
        pytest.param(
            '{"schema_version": 1, "schema_version": 1, "findings": []}',
            id="duplicate-json-keys",
        ),
        pytest.param(
            _content(_response([], schema_version=2)),
            id="wrong-schema-version",
        ),
        pytest.param(
            _content({"schema_version": "1", "findings": []}),
            id="non-integer-schema-version",
        ),
        pytest.param(_content({"schema_version": 1}), id="missing-findings-key"),
        pytest.param(
            _content({"schema_version": 1, "findings": [], "notes": "extra"}),
            id="extra-root-key",
        ),
        pytest.param(_content({"schema_version": 1, "findings": {}}), id="findings-not-a-list"),
        pytest.param(_content(_response([_variant(card_ids=[True])])), id="card-ids-boolean"),
        pytest.param(_content(_response([_variant(card_ids=["101"])])), id="card-ids-non-integer"),
        pytest.param(
            _content(_response([_variant(card_ids=[ALPHA_ID, ALPHA_ID])])),
            id="card-ids-duplicate",
        ),
        pytest.param(_content(_response([_variant(evidence=[])])), id="empty-evidence"),
        pytest.param(
            _content(_response([_variant(evidence=[_evidence(), _evidence()])])),
            id="duplicate-evidence",
        ),
        pytest.param(_content(_response([_variant(category="sideboard")])), id="unknown-category"),
        pytest.param(
            _content(_response([_variant(review={"status": "maybe", "reason": "Unsure."})])),
            id="unknown-status",
        ),
        pytest.param(
            _content(_response([_variant(review={"status": "uncertain"})])),
            id="missing-uncertain-reason",
        ),
        pytest.param(
            _content(_response([_variant(review={"status": "accepted", "reason": "Looks correct."})])),
            id="accepted-with-reason",
        ),
        pytest.param(
            _content(_response([_variant(review={"status": "uncertain", "reason": None})])),
            id="uncertain-with-null-reason",
        ),
        pytest.param(
            _content(_response([_variant(review={"status": "rejected", "reason": "   "})])),
            id="rejected-with-blank-reason",
        ),
        pytest.param(
            _content(_response([_variant(review={"status": " uncertain ", "reason": "Needs review."})])),
            id="padded-status",
        ),
        pytest.param(_content(_response([_variant(category=" strategy ")])), id="padded-category"),
        pytest.param('{"schema_version": NaN, "findings": []}', id="non-finite-json-constant"),
        pytest.param(
            _content(
                _response([_variant(), _variant(claim="Alpha wins with Beta on the play.")])
            ),
            id="duplicate-finding-id",
        ),
    ),
)
def test_structural_defects_are_malformed_whole_responses(
    sources: EnrichmentSources,
    content: str | None,
) -> None:
    result = _parse(content, sources)

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == MALFORMED_REASON
    assert result.accepted_findings == ()
    assert result.uncertain_findings == ()
    assert result.rejected_findings == ()
    assert result.guide_claims == ()


def test_valid_empty_response_is_success_without_findings(sources: EnrichmentSources) -> None:
    result = _parse(_content({"schema_version": 1, "findings": []}), sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.accepted_findings == ()
    assert result.uncertain_findings == ()
    assert result.rejected_findings == ()
    assert result.guide_claims == ()


def test_uncertain_and_rejected_findings_stay_distinguishable_from_empty_success(
    sources: EnrichmentSources,
) -> None:
    uncertain = _variant(review={"status": "uncertain", "reason": MODEL_UNCERTAINTY_REASON})
    rejected = _variant(
        finding_id="claim-rejected",
        evidence=[_evidence(quote=FABRICATED_QUOTE)],
    )

    result = _parse(_content(_response([uncertain, rejected])), sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert len(result.uncertain_findings) == 1
    assert result.uncertain_findings[0].review.reason == MODEL_UNCERTAINTY_REASON
    assert len(result.rejected_findings) == 1
    assert result.rejected_findings[0].reason == EVIDENCE_QUOTE_REASON
    assert result.guide_claims == result.uncertain_findings


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param({"accepted_findings": []}, id="accepted-not-a-tuple"),
        pytest.param({"uncertain_findings": [_claim("claim-a")]}, id="uncertain-not-a-tuple"),
        pytest.param({"rejected_findings": [_rejected_finding("claim-a")]}, id="rejected-not-a-tuple"),
        pytest.param(
            {"accepted_findings": (_rejected_finding("claim-a"),)},
            id="accepted-holds-a-rejected-finding",
        ),
        pytest.param(
            {"accepted_findings": (_claim("claim-a"),)},
            id="accepted-holds-an-uncertain-claim",
        ),
        pytest.param(
            {"accepted_findings": (_derived_claim("claim-a"),)},
            id="accepted-holds-a-derived-claim",
        ),
        pytest.param(
            {
                "accepted_findings": (
                    _claim("claim-a", status=FindingStatus.REJECTED, reason="Rejected."),
                )
            },
            id="accepted-holds-a-rejected-claim",
        ),
        pytest.param(
            {"uncertain_findings": (_claim("claim-a", status=FindingStatus.ACCEPTED, reason=None),)},
            id="uncertain-holds-an-accepted-claim",
        ),
        pytest.param(
            {"rejected_findings": (_rejected_finding("claim-a", source_kind="oracle"),)},
            id="rejected-holds-an-oracle-finding",
        ),
        pytest.param(
            {
                "accepted_findings": (
                    _claim("duplicate", status=FindingStatus.ACCEPTED, reason=None),
                ),
                "uncertain_findings": (_claim("duplicate"),),
            },
            id="duplicate-finding-id-across-collections",
        ),
        pytest.param(
            {
                "outcome": ExtractionOutcome.MALFORMED,
                "malformed_reason": "some other reason",
            },
            id="malformed-with-a-different-reason",
        ),
        pytest.param(
            {
                "outcome": ExtractionOutcome.MALFORMED,
                "malformed_reason": MALFORMED_REASON,
                "uncertain_findings": (_claim("claim-a"),),
            },
            id="malformed-with-findings",
        ),
        pytest.param({"malformed_reason": MALFORMED_REASON}, id="success-with-a-malformed-reason"),
        pytest.param({"outcome": "success"}, id="outcome-is-not-an-enum-member"),
    ),
)
def test_result_rejects_invalid_construction(overrides: dict[str, Any]) -> None:
    with pytest.raises(SetEnrichmentExtractionError):
        _result(**overrides)


def test_valid_result_is_frozen_and_canonicalizes_findings() -> None:
    result = _result(
        accepted_findings=(_claim("claim-c", status=FindingStatus.ACCEPTED, reason=None),),
        uncertain_findings=(_claim("claim-b"), _claim("claim-a")),
        rejected_findings=(_rejected_finding("claim-e"), _rejected_finding("claim-d")),
    )

    assert tuple(claim.finding_id for claim in result.accepted_findings) == ("claim-c",)
    assert tuple(claim.finding_id for claim in result.uncertain_findings) == ("claim-a", "claim-b")
    assert tuple(finding.finding_id for finding in result.rejected_findings) == ("claim-d", "claim-e")
    assert tuple(claim.finding_id for claim in result.guide_claims) == (
        "claim-a",
        "claim-b",
        "claim-c",
    )
    assert result.malformed_reason is None
    with pytest.raises(FrozenInstanceError):
        result.outcome = ExtractionOutcome.MALFORMED  # type: ignore[misc]


def test_request_identity_is_stable_across_equivalent_source_orders() -> None:
    forward = build_guide_extraction_request(
        sources=_sources((ALPHA_ID, BETA_ID, GAMMA_ID)),
        guide_id=GUIDE_ID,
    )
    reordered = build_guide_extraction_request(
        sources=_sources((GAMMA_ID, BETA_ID, ALPHA_ID)),
        guide_id=GUIDE_ID,
    )

    assert reordered.user_prompt == forward.user_prompt
    assert reordered.prompt_sha256 == forward.prompt_sha256
    assert reordered.response_schema() == forward.response_schema()
    assert reordered.response_schema_sha256 == forward.response_schema_sha256
    assert forward.contract_version == SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION
    assert forward.prompt_id == GUIDE_EXTRACTION_PROMPT_ID
    assert forward.response_schema_id == GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID
    assert forward.response_schema_name == GUIDE_EXTRACTION_SCHEMA_NAME
    assert re.fullmatch(r"[0-9a-f]{64}", forward.prompt_sha256) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", forward.response_schema_sha256) is not None


def test_request_hashes_match_pinned_canonical_bytes() -> None:
    system_prompt = "Extraia apenas afirmações do guia congelado — citações exactas."
    user_prompt = "guião congelado: Ætherling, 日本語"
    schema = {"type": "object", "enum": ["Æther", "日本語"]}
    reordered_schema = {"enum": ["Æther", "日本語"], "type": "object"}

    request = _request(system_prompt=system_prompt, user_prompt=user_prompt, schema=schema)
    reordered = _request(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema=reordered_schema,
    )

    expected_prompt_bytes = (
        '{"system_prompt":"' + system_prompt + '","user_prompt":"' + user_prompt + '"}'
    ).encode("utf-8")
    expected_schema_bytes = '{"enum":["Æther","日本語"],"type":"object"}'.encode("utf-8")

    assert request.prompt_sha256 == hashlib.sha256(expected_prompt_bytes).hexdigest()
    assert request.response_schema_sha256 == hashlib.sha256(expected_schema_bytes).hexdigest()
    assert reordered.prompt_sha256 == request.prompt_sha256
    assert reordered.response_schema_sha256 == request.response_schema_sha256
    assert reordered.response_schema() == {"enum": ["Æther", "日本語"], "type": "object"}


def test_prompt_and_schema_hashes_are_independent_identities() -> None:
    baseline = _request()
    system_variant = _request(system_prompt="Extract only claims the frozen guide states.")
    user_variant = _request(user_prompt='{"cards":[],"contract_version":1,"set_code":"oth"}')
    schema_variant = _request(schema={"type": "object", "properties": {"other": {"type": "string"}}})

    assert system_variant.prompt_sha256 != baseline.prompt_sha256
    assert system_variant.response_schema_sha256 == baseline.response_schema_sha256
    assert user_variant.prompt_sha256 != baseline.prompt_sha256
    assert user_variant.response_schema_sha256 == baseline.response_schema_sha256
    assert schema_variant.prompt_sha256 == baseline.prompt_sha256
    assert schema_variant.response_schema_sha256 != baseline.response_schema_sha256


def test_request_snapshot_is_isolated_from_caller_and_reader_mutation() -> None:
    schema = _simple_schema()
    request = _request(schema=schema)
    baseline_hash = request.response_schema_sha256
    baseline_schema = request.response_schema()

    schema["type"] = "array"
    schema["properties"]["value"]["minLength"] = 99

    assert request.response_schema_sha256 == baseline_hash
    assert request.response_schema() == baseline_schema

    returned = request.response_schema()
    returned["type"] = "array"
    returned["properties"]["value"]["minLength"] = 99

    assert request.response_schema_sha256 == baseline_hash
    assert request.response_schema() == baseline_schema


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param({"contract_version": 2}, id="unsupported-contract-version"),
        pytest.param({"contract_version": True}, id="boolean-contract-version"),
        pytest.param({"prompt_id": "   "}, id="blank-prompt-id"),
        pytest.param({"system_prompt": "   "}, id="blank-system-prompt"),
        pytest.param({"user_prompt": ""}, id="blank-user-prompt"),
        pytest.param({"schema": []}, id="non-object-schema"),
        pytest.param({"schema": {}}, id="empty-schema"),
        pytest.param({"schema": {"type": "number", "minimum": float("inf")}}, id="non-finite-number"),
        pytest.param({"schema": {1: "object"}}, id="non-string-schema-key"),
        pytest.param({"schema": {"enum": ("a", "b")}}, id="tuple-schema-node"),
        pytest.param({"schema": {"type": "bad\ud800"}}, id="non-utf8-schema-value"),
        pytest.param({"prompt_id": "bad\ud800"}, id="non-utf8-prompt-id"),
        pytest.param({"system_prompt": "bad\ud800"}, id="non-utf8-system-prompt"),
        pytest.param({"schema": _cyclic_schema()}, id="cyclic-schema"),
    ),
)
def test_request_rejects_invalid_construction(overrides: dict[str, Any]) -> None:
    with pytest.raises(SetEnrichmentExtractionError):
        _request(**overrides)


def test_generated_schema_is_a_strict_closed_object() -> None:
    request = build_guide_extraction_request(sources=_sources(), guide_id=GUIDE_ID)

    schema = request.response_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"schema_version", "findings"}
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["schema_version"] == {
        "type": "integer",
        "const": SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
    }

    findings_schema = schema["properties"]["findings"]
    assert findings_schema["type"] == "array"
    assert findings_schema["uniqueItems"] is True
    finding_schema = findings_schema["items"]
    assert finding_schema["additionalProperties"] is False
    assert set(finding_schema["properties"]) == {
        "finding_id",
        "category",
        "name",
        "claim",
        "card_ids",
        "evidence",
        "review",
    }
    assert set(finding_schema["required"]) == set(finding_schema["properties"])
    assert finding_schema["properties"]["category"]["enum"] == [
        "archetype",
        "format_finding",
        "mechanic",
        "strategy",
    ]
    assert finding_schema["properties"]["card_ids"] == {
        "type": "array",
        "uniqueItems": True,
        "items": {"type": "integer", "minimum": 1},
    }

    evidence_schema = finding_schema["properties"]["evidence"]
    assert evidence_schema["type"] == "array"
    assert evidence_schema["minItems"] == 1
    assert evidence_schema["items"]["additionalProperties"] is False
    assert set(evidence_schema["items"]["properties"]) == {"guide_id", "quote"}
    assert set(evidence_schema["items"]["required"]) == set(evidence_schema["items"]["properties"])

    review_schema = finding_schema["properties"]["review"]
    assert review_schema["additionalProperties"] is False
    assert set(review_schema["properties"]) == {"status", "reason"}
    assert review_schema["properties"]["status"]["enum"] == ["accepted", "rejected", "uncertain"]
    assert review_schema["properties"]["reason"] == {"type": ["string", "null"], "minLength": 1}

    for node in _object_nodes(schema):
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


def test_user_prompt_carries_only_frozen_guide_and_card_inputs(
    sources: EnrichmentSources,
) -> None:
    request = build_guide_extraction_request(sources=sources, guide_id=GUIDE_ID)

    prompt = json.loads(request.user_prompt)

    assert set(prompt) == {"contract_version", "set_code", "guide", "cards"}
    assert prompt["contract_version"] == SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION
    assert prompt["set_code"] == SET_CODE
    assert set(prompt["guide"]) == {"guide_id", "sha256", "text"}
    assert prompt["guide"]["guide_id"] == GUIDE_ID
    assert prompt["guide"]["sha256"] == sources.guides[0].text_sha256
    assert prompt["guide"]["text"] == GUIDE_TEXT
    assert prompt["cards"] == [
        {"card_id": ALPHA_ID, "name": "Alpha"},
        {"card_id": BETA_ID, "name": "Beta"},
        {"card_id": GAMMA_ID, "name": "Gamma"},
    ]
    assert request.user_prompt == json.dumps(
        prompt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert RETRIEVED_AT not in request.user_prompt
    assert GUIDE_URL not in request.user_prompt
    assert CARD_ORACLE_TEXT not in request.user_prompt


def test_build_request_requires_an_existing_guide(sources: EnrichmentSources) -> None:
    with pytest.raises(SetEnrichmentExtractionError) as error:
        build_guide_extraction_request(sources=sources, guide_id="guide-missing")

    assert str(error.value) == "guide_id must identify exactly one frozen guide source."


def test_generated_request_constructs_the_openrouter_client_locally(
    sources: EnrichmentSources,
) -> None:
    request = build_guide_extraction_request(sources=sources, guide_id=GUIDE_ID)

    client = OpenRouterClient(
        model="openai/fixture-model",
        schema_name=request.response_schema_name,
        schema=request.response_schema(),
        reasoning_effort="medium",
        max_tokens=1024,
    )

    assert request.response_schema_name == GUIDE_EXTRACTION_SCHEMA_NAME
    assert client.schema_name == request.response_schema_name


def test_card_request_carries_one_canonical_card_with_every_indexed_face(
    capability_sources: EnrichmentSources,
) -> None:
    request = build_card_capability_extraction_request(
        sources=capability_sources,
        card_id=TWO_FACE_CARD_ID,
    )

    assert type(request) is ExtractionRequest
    assert not isinstance(request, (tuple, list))

    prompt = json.loads(request.user_prompt)

    assert set(prompt) == {"contract_version", "set_code", "card_source_sha256", "card"}
    assert prompt["contract_version"] == SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION
    assert prompt["set_code"] == SET_CODE
    assert prompt["card_source_sha256"] == card_source_sha256(_two_face_card())
    assert request.user_prompt == json.dumps(
        prompt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert request.prompt_id == CARD_CAPABILITY_EXTRACTION_PROMPT_ID
    assert request.response_schema_id == CARD_CAPABILITY_EXTRACTION_RESPONSE_SCHEMA_ID
    assert request.response_schema_name == CARD_CAPABILITY_EXTRACTION_SCHEMA_NAME
    assert re.fullmatch(r"[0-9a-f]{64}", request.prompt_sha256) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", request.response_schema_sha256) is not None
    assert "guide" not in prompt
    assert GUIDE_ID not in request.user_prompt
    assert GUIDE_TEXT not in request.user_prompt
    assert RUN_ID not in request.user_prompt

    card = prompt["card"]
    assert set(card) == {
        "card_id",
        "oracle_id",
        "set_code",
        "collector_number",
        "name",
        "layout",
        "type_line",
        "oracle_text",
        "faces",
    }
    assert card["card_id"] == TWO_FACE_CARD_ID
    assert card["name"] == TWO_FACE_CARD_NAME
    assert card["set_code"] == SET_CODE
    assert card["layout"] == "transform"
    assert card["type_line"] == TWO_FACE_CARD_TYPE_LINE
    assert card["oracle_text"] == TWO_FACE_CARD_TEXT

    faces = card["faces"]
    assert [face["face_index"] for face in faces] == [0, 1]
    assert len({face["face_index"] for face in faces}) == len(faces)
    for face in faces:
        assert set(face) == {"name", "type_line", "oracle_text", "face_index"}
    assert faces[0]["face_index"] == 0
    assert faces[1]["face_index"] == 1
    assert [face["name"] for face in faces] == [FRONT_FACE_NAME, BACK_FACE_NAME]
    assert [face["oracle_text"] for face in faces] == [FRONT_FACE_TEXT, BACK_FACE_TEXT]

    plain_request = build_card_capability_extraction_request(
        sources=capability_sources,
        card_id=PLAIN_CARD_ID,
    )
    plain_prompt = json.loads(plain_request.user_prompt)

    assert set(plain_prompt) == {"contract_version", "set_code", "card_source_sha256", "card"}
    assert plain_prompt["card"]["card_id"] == PLAIN_CARD_ID
    assert plain_prompt["card"]["name"] == PLAIN_CARD_NAME
    assert plain_prompt["card"]["faces"] == []
    assert plain_prompt["card"]["oracle_text"] == PLAIN_CARD_TEXT
    assert plain_request.prompt_id == CARD_CAPABILITY_EXTRACTION_PROMPT_ID
    assert plain_request.response_schema_id == CARD_CAPABILITY_EXTRACTION_RESPONSE_SCHEMA_ID
    assert plain_request.response_schema_name == CARD_CAPABILITY_EXTRACTION_SCHEMA_NAME


def test_model_accepted_and_uncertain_capabilities_parse_as_ordered_typed_records(
    capability_sources: EnrichmentSources,
) -> None:
    accepted_candidate = _capability_candidate(
        finding_id="capability-draw",
        quantity=_quantity(value=None, relation="variable"),
        timing=BACK_TRIGGER_QUOTE,
        source_zone="battlefield",
        destination_zone="graveyard",
        prerequisites=[
            _prerequisite_entry(
                quantity=_quantity(value=1, relation="exactly"),
                timing=BACK_TRIGGER_QUOTE,
                evidence=_evidence_entry(quote=BACK_TRIGGER_QUOTE),
            )
        ],
        review={"status": "accepted", "reason": None},
    )
    uncertain_candidate = _capability_candidate(
        finding_id="capability-mill",
        role="self_mill",
        quantity=_quantity(value=2, relation="exactly"),
        timing=BACK_TRIGGER_QUOTE,
        review={"status": "uncertain", "reason": MODEL_UNCERTAINTY_REASON},
    )

    result = _parse_card(
        _content(_capability_response([uncertain_candidate, accepted_candidate])),
        capability_sources,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.accepted_capabilities == ()
    assert tuple(capability.finding_id for capability in result.capabilities) == (
        "capability-draw",
        "capability-mill",
    )
    assert result.capabilities == result.uncertain_capabilities

    draw = result.capabilities[0]
    assert type(draw) is CardCapability
    assert draw.finding_id == "capability-draw"
    assert draw.card_id == TWO_FACE_CARD_ID
    assert draw.card_name == TWO_FACE_CARD_NAME
    assert draw.face_index == 1
    assert draw.face_name == BACK_FACE_NAME
    assert draw.role is Role.DRAW
    assert draw.quantity == CapabilityQuantity(value=None, relation=QuantityRelation.VARIABLE)
    assert draw.timing == BACK_TRIGGER_QUOTE
    assert draw.source_zone is CapabilityZone.BATTLEFIELD
    assert draw.destination_zone is CapabilityZone.GRAVEYARD
    assert draw.prerequisites == (
        CapabilityPrerequisite(
            kind=PrerequisiteKind.COST,
            quantity=CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
            timing=BACK_TRIGGER_QUOTE,
            source_zone=None,
            destination_zone=None,
            evidence=OracleEvidence(
                card_id=TWO_FACE_CARD_ID,
                face_index=1,
                quote=BACK_TRIGGER_QUOTE,
            ),
        ),
    )
    assert draw.evidence == (
        OracleEvidence(card_id=TWO_FACE_CARD_ID, face_index=1, quote=BACK_MILL_QUOTE),
    )
    assert draw.review == FindingReview(
        status=FindingStatus.UNCERTAIN,
        reason=CAPABILITY_REVIEW_REASON,
    )
    assert draw.run_id == RUN_ID

    mill = result.capabilities[1]
    assert mill.role is Role.SELF_MILL
    assert mill.quantity == CapabilityQuantity(value=2, relation=QuantityRelation.EXACTLY)
    assert mill.prerequisites == ()
    assert mill.review == FindingReview(
        status=FindingStatus.UNCERTAIN,
        reason=MODEL_UNCERTAINTY_REASON,
    )
    assert mill.run_id == RUN_ID

    for capability in result.capabilities:
        assert CardCapability.from_json(capability.to_json()) == capability
        for prerequisite in capability.prerequisites:
            assert CapabilityPrerequisite.from_json(prerequisite.to_json()) == prerequisite
        for evidence in capability.evidence:
            assert OracleEvidence.from_json(evidence.to_json()) == evidence

    prerequisite = draw.prerequisites[0]
    prerequisite_quantity = prerequisite.quantity
    assert prerequisite_quantity is not None
    assert CapabilityPrerequisite.from_json(prerequisite.to_json()) == prerequisite
    assert CapabilityQuantity.from_json(prerequisite_quantity.to_json()) == prerequisite_quantity


@pytest.mark.parametrize(
    ("card_id", "overrides", "expected_reason"),
    (
        pytest.param(
            TWO_FACE_CARD_ID,
            {"evidence": [_evidence_entry(quote=FABRICATED_QUOTE)]},
            CAPABILITY_EVIDENCE_QUOTE_REASON,
            id="fabricated-quote",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"card_id": FOREIGN_CARD_ID},
            CAPABILITY_CARD_ID_REASON,
            id="foreign-card-id",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"card_name": "Alpha // Gamma"},
            CAPABILITY_CARD_NAME_REASON,
            id="wrong-card-name",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"face_index": None},
            CAPABILITY_FACE_REASON,
            id="null-face-index-on-a-face-card",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"face_index": 2},
            CAPABILITY_FACE_REASON,
            id="out-of-range-face-index",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"face_index": 1, "face_name": FRONT_FACE_NAME},
            CAPABILITY_FACE_REASON,
            id="mismatched-face-index-and-name",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"face_name": None},
            CAPABILITY_FACE_REASON,
            id="null-face-name-for-a-named-face",
        ),
        pytest.param(
            PLAIN_CARD_ID,
            {
                "card_id": PLAIN_CARD_ID,
                "card_name": PLAIN_CARD_NAME,
                "face_index": 0,
                "face_name": None,
                "evidence": [
                    _evidence_entry(
                        card_id=PLAIN_CARD_ID,
                        face_index=None,
                        quote=PLAIN_CARD_TEXT,
                    )
                ],
            },
            CAPABILITY_FACE_REASON,
            id="non-null-face-index-on-a-no-face-card",
        ),
        pytest.param(
            PLAIN_CARD_ID,
            {
                "card_id": PLAIN_CARD_ID,
                "card_name": PLAIN_CARD_NAME,
                "face_index": None,
                "face_name": PLAIN_CARD_NAME,
                "evidence": [
                    _evidence_entry(
                        card_id=PLAIN_CARD_ID,
                        face_index=None,
                        quote=PLAIN_CARD_TEXT,
                    )
                ],
            },
            CAPABILITY_FACE_REASON,
            id="non-null-face-name-on-a-no-face-card",
        ),
        pytest.param(
            NULL_FACE_NAME_CARD_ID,
            {
                "card_id": NULL_FACE_NAME_CARD_ID,
                "card_name": NULL_FACE_NAME_CARD_NAME,
                "face_index": 0,
                "face_name": "Ghost",
                "evidence": [
                    _evidence_entry(
                        card_id=NULL_FACE_NAME_CARD_ID,
                        face_index=0,
                        quote=NULL_FACE_NAME_FACE_TEXT,
                    )
                ],
            },
            CAPABILITY_FACE_REASON,
            id="non-null-face-name-for-a-nullable-face-name",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"evidence": [_evidence_entry(card_id=PLAIN_CARD_ID)]},
            CAPABILITY_EVIDENCE_OWNER_REASON,
            id="capability-evidence-from-another-card",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"evidence": [_evidence_entry(face_index=0)]},
            CAPABILITY_EVIDENCE_OWNER_REASON,
            id="capability-evidence-from-the-wrong-face",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"prerequisites": [_prerequisite_entry(evidence=_evidence_entry(card_id=FOREIGN_CARD_ID))]},
            CAPABILITY_EVIDENCE_OWNER_REASON,
            id="prerequisite-evidence-from-a-foreign-card",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"evidence": [_evidence_entry(quote=FRONT_FACE_TEXT)]},
            CAPABILITY_EVIDENCE_QUOTE_REASON,
            id="wrong-face-exact-quote",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"prerequisites": [_prerequisite_entry(evidence=_evidence_entry(quote=FABRICATED_QUOTE))]},
            CAPABILITY_EVIDENCE_QUOTE_REASON,
            id="fabricated-prerequisite-quote",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"role": "sideboard"},
            CAPABILITY_VOCABULARY_REASON,
            id="unsupported-role",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"source_zone": "nowhere"},
            CAPABILITY_VOCABULARY_REASON,
            id="unsupported-source-zone",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"destination_zone": "nowhere"},
            CAPABILITY_VOCABULARY_REASON,
            id="unsupported-destination-zone",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"quantity": _quantity(value=2, relation="about")},
            CAPABILITY_VOCABULARY_REASON,
            id="unsupported-quantity-relation",
        ),
        pytest.param(
            TWO_FACE_CARD_ID,
            {"prerequisites": [_prerequisite_entry(kind="sideboard")]},
            CAPABILITY_VOCABULARY_REASON,
            id="unsupported-prerequisite-kind",
        ),
    ),
)
def test_capability_source_and_vocabulary_failures_are_rejected_with_fixed_reasons(
    capability_sources: EnrichmentSources,
    card_id: int,
    overrides: dict[str, Any],
    expected_reason: str,
) -> None:
    candidate = _capability_candidate(**overrides)

    result = _parse_card(
        _content(_capability_response([candidate])),
        capability_sources,
        card_id,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.capabilities == ()
    assert len(result.rejected_capabilities) == 1
    rejected = result.rejected_capabilities[0]
    assert rejected.finding_id == candidate["finding_id"]
    assert rejected.source_kind == "oracle"
    assert rejected.summary == candidate["role"]
    assert rejected.reason == expected_reason
    assert rejected.run_id == RUN_ID


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    (
        pytest.param(
            {
                "card_id": FOREIGN_CARD_ID,
                "review": {"status": "rejected", "reason": MODEL_REJECTION_REASON},
            },
            CAPABILITY_CARD_ID_REASON,
            id="foreign-card-outranks-model-rejection",
        ),
        pytest.param(
            {
                "evidence": [_evidence_entry(quote=FABRICATED_QUOTE)],
                "review": {"status": "rejected", "reason": MODEL_REJECTION_REASON},
            },
            CAPABILITY_EVIDENCE_QUOTE_REASON,
            id="fabricated-quote-outranks-model-rejection",
        ),
        pytest.param(
            {
                "role": "sideboard",
                "review": {"status": "rejected", "reason": MODEL_REJECTION_REASON},
            },
            MODEL_REJECTION_REASON,
            id="model-rejection-outranks-unsupported-vocabulary",
        ),
        pytest.param(
            {
                "role": "sideboard",
                "evidence": [_evidence_entry(quote=FABRICATED_QUOTE)],
            },
            CAPABILITY_EVIDENCE_QUOTE_REASON,
            id="fabricated-quote-outranks-unsupported-vocabulary",
        ),
        pytest.param(
            {
                "face_index": 9,
                "evidence": [_evidence_entry(face_index=0)],
            },
            CAPABILITY_FACE_REASON,
            id="face-identity-outranks-evidence-ownership",
        ),
        pytest.param(
            {"evidence": [_evidence_entry(face_index=0, quote=FABRICATED_QUOTE)]},
            CAPABILITY_EVIDENCE_OWNER_REASON,
            id="evidence-ownership-outranks-a-bad-quote",
        ),
        pytest.param(
            {"review": {"status": "rejected", "reason": MODEL_REJECTION_REASON}},
            MODEL_REJECTION_REASON,
            id="source-valid-rejection-keeps-the-model-reason",
        ),
    ),
)
def test_capability_diagnostic_precedence_pins_the_first_applicable_reason(
    capability_sources: EnrichmentSources,
    overrides: dict[str, Any],
    expected_reason: str,
) -> None:
    candidate = _capability_candidate(**overrides)

    result = _parse_card(_content(_capability_response([candidate])), capability_sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.capabilities == ()
    assert len(result.rejected_capabilities) == 1
    rejected = result.rejected_capabilities[0]
    assert rejected.finding_id == candidate["finding_id"]
    assert rejected.summary == candidate["role"]
    assert rejected.reason == expected_reason
    assert rejected.run_id == RUN_ID


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param(
            {
                "role": "extra_draw_enabler",
                "face_index": 0,
                "face_name": FRONT_FACE_NAME,
                "evidence": [_evidence_entry(face_index=0, quote=FRONT_FACE_TEXT)],
            },
            id="draw-trigger-mislabeled-as-a-draw-producer",
        ),
        pytest.param(
            {
                "role": "self_mill",
                "evidence": [_evidence_entry(quote=BACK_MILL_QUOTE)],
            },
            id="opponent-mill-mislabeled-as-self-mill",
        ),
        pytest.param(
            {
                "face_index": 0,
                "face_name": FRONT_FACE_NAME,
                "evidence": [_evidence_entry(face_index=0, quote=FRONT_EFFECT_QUOTE)],
            },
            id="controlling-clause-omitted-from-the-quote",
        ),
        pytest.param(
            {
                "face_index": 0,
                "face_name": FRONT_FACE_NAME,
                "source_zone": "graveyard",
                "destination_zone": "library",
                "evidence": [_evidence_entry(face_index=0, quote=FRONT_FACE_TEXT)],
            },
            id="swapped-draw-zones",
        ),
        pytest.param(
            {
                "face_index": 0,
                "face_name": FRONT_FACE_NAME,
                "quantity": _quantity(value=2, relation="at_least"),
                "evidence": [_evidence_entry(face_index=0, quote=FRONT_FACE_TEXT)],
            },
            id="wrong-quantity-relation",
        ),
        pytest.param(
            {
                "quantity": _quantity(value=2, relation="exactly"),
                "evidence": [_evidence_entry(quote=BACK_MILL_QUOTE)],
            },
            id="borrowed-count-for-a-draw-quantity",
        ),
        pytest.param(
            {
                "evidence": [_evidence_entry(quote=BACK_TRIGGER_QUOTE)],
                "prerequisites": [
                    _prerequisite_entry(
                        quantity=_quantity(value=2, relation="exactly"),
                        evidence=_evidence_entry(quote=BACK_MILL_QUOTE),
                    )
                ],
            },
            id="effect-mislabeled-as-a-prerequisite-cost",
        ),
        pytest.param(
            {
                "role": "self_mill",
                "evidence": [_evidence_entry(quote=BACK_MILL_QUOTE)],
                "prerequisites": [
                    _prerequisite_entry(
                        kind="condition",
                        evidence=_evidence_entry(quote=BACK_TRIGGER_QUOTE),
                    )
                ],
            },
            id="unrelated-prerequisite-from-the-same-face",
        ),
    ),
)
def test_source_valid_capabilities_remain_uncertain_beyond_exact_source_validation(
    capability_sources: EnrichmentSources,
    overrides: dict[str, Any],
) -> None:
    candidate = _capability_candidate(**overrides)

    result = _parse_card(_content(_capability_response([candidate])), capability_sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.accepted_capabilities == ()
    assert result.rejected_capabilities == ()
    assert len(result.uncertain_capabilities) == 1
    capability = result.uncertain_capabilities[0]
    assert capability.finding_id == candidate["finding_id"]
    assert capability.role is Role(candidate["role"])
    assert capability.review == FindingReview(
        status=FindingStatus.UNCERTAIN,
        reason=CAPABILITY_REVIEW_REASON,
    )
    assert capability.run_id == RUN_ID
    assert result.capabilities == result.uncertain_capabilities


@pytest.mark.parametrize(
    "content",
    (
        pytest.param(None, id="non-string-content"),
        pytest.param("", id="blank-content"),
        pytest.param("not json", id="invalid-json"),
        pytest.param("bad\ud800", id="non-utf8-content"),
        pytest.param(_content([]), id="root-array"),
        pytest.param(
            '{"schema_version": 1, "schema_version": 1, "capabilities": []}',
            id="duplicate-json-keys",
        ),
        pytest.param(_content(_capability_response([], schema_version=2)), id="wrong-schema-version"),
        pytest.param(
            _content({"schema_version": "1", "capabilities": []}),
            id="non-integer-schema-version",
        ),
        pytest.param(_content({"schema_version": 1}), id="missing-capabilities-key"),
        pytest.param(
            _content({"schema_version": 1, "capabilities": [], "notes": "extra"}),
            id="extra-root-key",
        ),
        pytest.param(_content({"schema_version": 1, "capabilities": {}}), id="capabilities-not-a-list"),
        pytest.param(
            _content(_capability_response(["not-an-object"])),
            id="capability-not-an-object",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [
                        {
                            key: value
                            for key, value in _capability_candidate().items()
                            if key != "review"
                        }
                    ]
                )
            ),
            id="missing-capability-key",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(notes="extra")])),
            id="extra-capability-key",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(card_id=True)])),
            id="boolean-card-id",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(card_id="202")])),
            id="non-integer-card-id",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(face_index=True)])),
            id="boolean-face-index",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(role=7)])),
            id="non-string-role",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(role="   ")])),
            id="blank-role",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(quantity="variable")])),
            id="quantity-not-an-object",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(quantity={"value": None})])),
            id="quantity-missing-keys",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [
                        _capability_candidate(
                            quantity={"value": None, "relation": "variable", "unit": "cards"}
                        )
                    ]
                )
            ),
            id="quantity-extra-keys",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(quantity={"value": True, "relation": "exactly"})]
                )
            ),
            id="boolean-quantity-value",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(quantity={"value": 0, "relation": "exactly"})]
                )
            ),
            id="zero-quantity-value",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(quantity={"value": 2, "relation": "variable"})]
                )
            ),
            id="variable-quantity-with-an-integer-value",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(quantity={"value": None, "relation": "exactly"})]
                )
            ),
            id="fixed-quantity-with-a-null-value",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(prerequisites={})])),
            id="prerequisites-not-a-list",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [
                        _capability_candidate(
                            prerequisites=[_prerequisite_entry(), _prerequisite_entry()]
                        )
                    ]
                )
            ),
            id="duplicate-prerequisites",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [
                        _capability_candidate(
                            prerequisites=[
                                {
                                    key: value
                                    for key, value in _prerequisite_entry().items()
                                    if key != "timing"
                                }
                            ]
                        )
                    ]
                )
            ),
            id="prerequisite-missing-a-key",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [
                        _capability_candidate(
                            prerequisites=[_prerequisite_entry(evidence=[_evidence_entry()])]
                        )
                    ]
                )
            ),
            id="prerequisite-evidence-not-an-object",
        ),
        pytest.param(
            _content(
                _capability_response([_capability_candidate(evidence=[_evidence_entry(quote="   ")])])
            ),
            id="blank-quote",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(evidence=[])])),
            id="empty-capability-evidence",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(evidence=[_evidence_entry(), _evidence_entry()])]
                )
            ),
            id="duplicate-capability-evidence",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(review={"status": "maybe", "reason": "Unsure."})]
                )
            ),
            id="unknown-review-status",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(review={"status": "accepted", "reason": "Looks correct."})]
                )
            ),
            id="accepted-with-a-reason",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(review={"status": "uncertain", "reason": None})]
                )
            ),
            id="uncertain-with-a-null-reason",
        ),
        pytest.param(
            _content(
                _capability_response(
                    [_capability_candidate(review={"status": "uncertain", "reason": "   "})]
                )
            ),
            id="uncertain-with-a-blank-reason",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(timing="   ")])),
            id="blank-timing",
        ),
        pytest.param(
            _content(_capability_response([_capability_candidate(), _capability_candidate()])),
            id="duplicate-finding-id",
        ),
        pytest.param('{"schema_version": NaN, "capabilities": []}', id="non-finite-json-constant"),
    ),
)
def test_card_structural_defects_are_malformed_whole_responses(
    capability_sources: EnrichmentSources,
    content: str | None,
) -> None:
    result = _parse_card(content, capability_sources)

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == CARD_MALFORMED_REASON
    assert result.accepted_capabilities == ()
    assert result.uncertain_capabilities == ()
    assert result.rejected_capabilities == ()
    assert result.capabilities == ()


@pytest.mark.parametrize(
    "quantity",
    (
        pytest.param({"value": None, "relation": "about"}, id="unknown-relation-with-a-null-value"),
        pytest.param({"value": 2, "relation": "about"}, id="unknown-relation-with-an-integer-value"),
    ),
)
def test_unknown_quantity_relations_are_rejected_rather_than_malformed(
    capability_sources: EnrichmentSources,
    quantity: dict[str, Any],
) -> None:
    candidate = _capability_candidate(quantity=quantity)

    result = _parse_card(_content(_capability_response([candidate])), capability_sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.capabilities == ()
    assert len(result.rejected_capabilities) == 1
    rejected = result.rejected_capabilities[0]
    assert rejected.finding_id == candidate["finding_id"]
    assert rejected.summary == candidate["role"]
    assert rejected.reason == CAPABILITY_VOCABULARY_REASON
    assert rejected.run_id == RUN_ID


@pytest.mark.parametrize(
    ("capabilities", "expected_uncertain", "expected_rejected"),
    (
        pytest.param([], 0, 0, id="valid-empty"),
        pytest.param(
            [
                _capability_candidate(
                    review={"status": "uncertain", "reason": MODEL_UNCERTAINTY_REASON}
                )
            ],
            1,
            0,
            id="uncertain-only",
        ),
        pytest.param(
            [_capability_candidate(evidence=[_evidence_entry(quote=FABRICATED_QUOTE)])],
            0,
            1,
            id="rejected-only",
        ),
        pytest.param(
            [
                _capability_candidate(
                    review={"status": "uncertain", "reason": MODEL_UNCERTAINTY_REASON}
                ),
                _capability_candidate(
                    finding_id="capability-broken",
                    evidence=[_evidence_entry(quote=FABRICATED_QUOTE)],
                ),
            ],
            1,
            1,
            id="mixed-uncertain-and-rejected",
        ),
    ),
)
def test_valid_card_documents_are_success_with_distinguishable_buckets(
    capability_sources: EnrichmentSources,
    capabilities: list[dict[str, Any]],
    expected_uncertain: int,
    expected_rejected: int,
) -> None:
    result = _parse_card(_content(_capability_response(capabilities)), capability_sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.accepted_capabilities == ()
    assert len(result.uncertain_capabilities) == expected_uncertain
    assert len(result.rejected_capabilities) == expected_rejected
    assert result.capabilities == result.uncertain_capabilities
    if expected_uncertain:
        assert result.uncertain_capabilities[0].review.reason == MODEL_UNCERTAINTY_REASON
    if expected_rejected:
        assert result.rejected_capabilities[0].reason == CAPABILITY_EVIDENCE_QUOTE_REASON


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param({"accepted_capabilities": []}, id="accepted-not-a-tuple"),
        pytest.param(
            {"uncertain_capabilities": [_reviewed_capability("capability-a")]},
            id="uncertain-not-a-tuple",
        ),
        pytest.param(
            {"rejected_capabilities": [_rejected_capability("capability-a")]},
            id="rejected-not-a-tuple",
        ),
        pytest.param(
            {"accepted_capabilities": (_reviewed_capability("capability-a"),)},
            id="accepted-holds-an-uncertain-capability",
        ),
        pytest.param(
            {
                "accepted_capabilities": (
                    _reviewed_capability(
                        "capability-a",
                        status=FindingStatus.ACCEPTED,
                        reason="Looks correct.",
                    ),
                )
            },
            id="accepted-holds-an-accepted-capability-with-a-reason",
        ),
        pytest.param(
            {"uncertain_capabilities": (_derived_capability("capability-a"),)},
            id="uncertain-holds-a-derived-capability",
        ),
        pytest.param(
            {
                "uncertain_capabilities": (
                    _reviewed_capability("capability-a", status=FindingStatus.ACCEPTED, reason=None),
                )
            },
            id="uncertain-holds-an-accepted-capability",
        ),
        pytest.param(
            {"rejected_capabilities": (_rejected_capability("capability-a", source_kind="guide"),)},
            id="rejected-holds-a-guide-finding",
        ),
        pytest.param(
            {
                "accepted_capabilities": (
                    _reviewed_capability("duplicate", status=FindingStatus.ACCEPTED, reason=None),
                ),
                "uncertain_capabilities": (_reviewed_capability("duplicate"),),
            },
            id="duplicate-finding-id-across-collections",
        ),
        pytest.param(
            {
                "outcome": ExtractionOutcome.MALFORMED,
                "malformed_reason": "some other reason",
            },
            id="malformed-with-a-different-reason",
        ),
        pytest.param(
            {
                "outcome": ExtractionOutcome.MALFORMED,
                "malformed_reason": CARD_MALFORMED_REASON,
                "uncertain_capabilities": (_reviewed_capability("capability-a"),),
            },
            id="malformed-with-capabilities",
        ),
        pytest.param(
            {"malformed_reason": CARD_MALFORMED_REASON},
            id="success-with-a-malformed-reason",
        ),
        pytest.param({"outcome": "success"}, id="outcome-is-not-an-enum-member"),
    ),
)
def test_card_result_rejects_invalid_construction(overrides: dict[str, Any]) -> None:
    with pytest.raises(SetEnrichmentExtractionError):
        _card_result(**overrides)


def test_explicitly_reviewed_accepted_capability_is_frozen_and_ordered_first() -> None:
    result = _card_result(
        accepted_capabilities=(
            _reviewed_capability("capability-alpha", status=FindingStatus.ACCEPTED, reason=None),
        ),
        uncertain_capabilities=(
            _reviewed_capability("capability-gamma"),
            _reviewed_capability("capability-beta"),
        ),
    )

    assert tuple(capability.finding_id for capability in result.accepted_capabilities) == (
        "capability-alpha",
    )
    assert tuple(capability.finding_id for capability in result.uncertain_capabilities) == (
        "capability-beta",
        "capability-gamma",
    )
    assert result.capabilities == (
        _reviewed_capability("capability-alpha", status=FindingStatus.ACCEPTED, reason=None),
        _reviewed_capability("capability-beta"),
        _reviewed_capability("capability-gamma"),
    )
    assert result.capabilities[0].review == FindingReview(
        status=FindingStatus.ACCEPTED,
        reason=None,
    )
    assert result.malformed_reason is None
    with pytest.raises(FrozenInstanceError):
        result.outcome = ExtractionOutcome.MALFORMED  # type: ignore[misc]


def test_card_request_identity_is_stable_across_equivalent_card_inputs() -> None:
    forward = build_card_capability_extraction_request(
        sources=_card_sources(_two_face_card()),
        card_id=TWO_FACE_CARD_ID,
    )
    equivalent = build_card_capability_extraction_request(
        sources=_card_sources(_equivalent_two_face_card()),
        card_id=TWO_FACE_CARD_ID,
    )
    null_forward = build_card_capability_extraction_request(
        sources=_card_sources(_null_face_name_card()),
        card_id=NULL_FACE_NAME_CARD_ID,
    )
    null_equivalent = build_card_capability_extraction_request(
        sources=_card_sources(_equivalent_null_face_name_card()),
        card_id=NULL_FACE_NAME_CARD_ID,
    )

    assert equivalent.user_prompt == forward.user_prompt
    assert equivalent.prompt_sha256 == forward.prompt_sha256
    assert equivalent.response_schema() == forward.response_schema()
    assert equivalent.response_schema_sha256 == forward.response_schema_sha256
    assert null_equivalent.user_prompt == null_forward.user_prompt
    assert null_equivalent.prompt_sha256 == null_forward.prompt_sha256
    assert null_equivalent.response_schema() == null_forward.response_schema()
    assert null_equivalent.response_schema_sha256 == null_forward.response_schema_sha256

    assert card_source_sha256(_equivalent_two_face_card()) == card_source_sha256(_two_face_card())

    padded_prompt = json.loads(equivalent.user_prompt)
    assert padded_prompt["card"]["name"] == TWO_FACE_CARD_NAME
    assert padded_prompt["card_source_sha256"] == card_source_sha256(_two_face_card())

    null_prompt = json.loads(null_equivalent.user_prompt)
    assert null_prompt["card"]["name"] == NULL_FACE_NAME_CARD_NAME
    assert null_prompt["card"]["faces"][0]["name"] is None
    assert null_prompt["card"]["faces"][1]["name"] == NULL_FACE_NAME_BACK_FACE_NAME


def test_reversed_face_order_changes_the_indexed_input_and_prompt_hash() -> None:
    front, back = _two_face_card_faces()
    forward = build_card_capability_extraction_request(
        sources=_card_sources(_two_face_card()),
        card_id=TWO_FACE_CARD_ID,
    )
    rebuilt = build_card_capability_extraction_request(
        sources=_card_sources(_two_face_card()),
        card_id=TWO_FACE_CARD_ID,
    )
    reversed_request = build_card_capability_extraction_request(
        sources=_card_sources(_two_face_card(faces=(back, front))),
        card_id=TWO_FACE_CARD_ID,
    )

    forward_faces = json.loads(forward.user_prompt)["card"]["faces"]
    reversed_faces = json.loads(reversed_request.user_prompt)["card"]["faces"]

    assert [(face["face_index"], face["name"]) for face in forward_faces] == [
        (0, FRONT_FACE_NAME),
        (1, BACK_FACE_NAME),
    ]
    assert [(face["face_index"], face["name"]) for face in reversed_faces] == [
        (0, BACK_FACE_NAME),
        (1, FRONT_FACE_NAME),
    ]
    assert [face["oracle_text"] for face in reversed_faces] == [BACK_FACE_TEXT, FRONT_FACE_TEXT]
    assert rebuilt.user_prompt == forward.user_prompt
    assert rebuilt.prompt_sha256 == forward.prompt_sha256
    assert reversed_request.user_prompt != forward.user_prompt
    assert reversed_request.prompt_sha256 != forward.prompt_sha256
    assert reversed_request.response_schema() == forward.response_schema()
    assert reversed_request.response_schema_sha256 == forward.response_schema_sha256


def test_nullable_face_name_is_requested_and_parsed_as_null(
    capability_sources: EnrichmentSources,
) -> None:
    request = build_card_capability_extraction_request(
        sources=capability_sources,
        card_id=NULL_FACE_NAME_CARD_ID,
    )

    prompt = json.loads(request.user_prompt)

    assert prompt["card"]["faces"][0] == {
        "name": None,
        "type_line": NULL_FACE_NAME_FRONT_TYPE_LINE,
        "oracle_text": NULL_FACE_NAME_FACE_TEXT,
        "face_index": 0,
    }

    identity: dict[str, Any] = {
        "finding_id": "capability-null-name",
        "card_id": NULL_FACE_NAME_CARD_ID,
        "card_name": NULL_FACE_NAME_CARD_NAME,
        "face_index": 0,
        "face_name": None,
        "evidence": [
            _evidence_entry(
                card_id=NULL_FACE_NAME_CARD_ID,
                face_index=0,
                quote=NULL_FACE_NAME_FACE_TEXT,
            )
        ],
    }

    result = _parse_card(
        _content(_capability_response([_capability_candidate(**identity)])),
        capability_sources,
        NULL_FACE_NAME_CARD_ID,
    )

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.rejected_capabilities == ()
    assert len(result.uncertain_capabilities) == 1
    capability = result.uncertain_capabilities[0]
    assert capability.card_id == NULL_FACE_NAME_CARD_ID
    assert capability.card_name == NULL_FACE_NAME_CARD_NAME
    assert capability.face_index == 0
    assert capability.face_name is None
    assert capability.evidence == (
        OracleEvidence(
            card_id=NULL_FACE_NAME_CARD_ID,
            face_index=0,
            quote=NULL_FACE_NAME_FACE_TEXT,
        ),
    )

    rejected_result = _parse_card(
        _content(
            _capability_response([_capability_candidate(**{**identity, "face_name": "Ghost"})])
        ),
        capability_sources,
        NULL_FACE_NAME_CARD_ID,
    )

    assert rejected_result.outcome is ExtractionOutcome.SUCCESS
    assert rejected_result.capabilities == ()
    assert len(rejected_result.rejected_capabilities) == 1
    assert rejected_result.rejected_capabilities[0].reason == CAPABILITY_FACE_REASON


def test_generated_card_schema_is_a_strict_closed_object(
    capability_sources: EnrichmentSources,
) -> None:
    request = build_card_capability_extraction_request(
        sources=capability_sources,
        card_id=TWO_FACE_CARD_ID,
    )

    schema = request.response_schema()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"schema_version", "capabilities"}
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["schema_version"] == {
        "type": "integer",
        "const": SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
    }

    capabilities_schema = schema["properties"]["capabilities"]
    assert capabilities_schema["type"] == "array"
    assert capabilities_schema["uniqueItems"] is True
    capability_schema = capabilities_schema["items"]
    assert capability_schema["additionalProperties"] is False
    assert set(capability_schema["properties"]) == {
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
    }
    assert set(capability_schema["required"]) == set(capability_schema["properties"])
    assert capability_schema["properties"]["finding_id"] == {"type": "string", "minLength": 1}
    assert capability_schema["properties"]["card_id"] == {"type": "integer", "minimum": 1}
    assert capability_schema["properties"]["card_name"] == {"type": "string", "minLength": 1}
    assert capability_schema["properties"]["face_index"] == {
        "type": ["integer", "null"],
        "minimum": 0,
    }
    assert capability_schema["properties"]["face_name"] == {
        "type": ["string", "null"],
        "minLength": 1,
    }
    assert capability_schema["properties"]["role"] == {
        "type": "string",
        "enum": sorted(member.value for member in Role),
    }
    assert capability_schema["properties"]["quantity"] == {
        "anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "properties": {
                    "value": {"type": ["integer", "null"], "minimum": 1},
                    "relation": {
                        "type": "string",
                        "enum": sorted(member.value for member in QuantityRelation),
                    },
                },
                "required": ["value", "relation"],
                "additionalProperties": False,
            },
        ]
    }
    assert capability_schema["properties"]["timing"] == {
        "type": ["string", "null"],
        "minLength": 1,
    }
    zone_schema = {
        "type": ["string", "null"],
        "enum": [*sorted(member.value for member in CapabilityZone), None],
    }
    assert capability_schema["properties"]["source_zone"] == zone_schema
    assert capability_schema["properties"]["destination_zone"] == zone_schema

    prerequisites_schema = capability_schema["properties"]["prerequisites"]
    assert prerequisites_schema["type"] == "array"
    assert prerequisites_schema["uniqueItems"] is True
    prerequisite_schema = prerequisites_schema["items"]
    assert prerequisite_schema["additionalProperties"] is False
    assert set(prerequisite_schema["properties"]) == {
        "kind",
        "quantity",
        "timing",
        "source_zone",
        "destination_zone",
        "evidence",
    }
    assert set(prerequisite_schema["required"]) == set(prerequisite_schema["properties"])
    assert prerequisite_schema["properties"]["kind"] == {
        "type": "string",
        "enum": sorted(member.value for member in PrerequisiteKind),
    }
    assert (
        prerequisite_schema["properties"]["quantity"]
        == capability_schema["properties"]["quantity"]
    )
    assert prerequisite_schema["properties"]["timing"] == capability_schema["properties"]["timing"]
    assert prerequisite_schema["properties"]["source_zone"] == zone_schema
    assert prerequisite_schema["properties"]["destination_zone"] == zone_schema

    oracle_evidence_schema = prerequisite_schema["properties"]["evidence"]
    assert oracle_evidence_schema["type"] == "object"
    assert oracle_evidence_schema["additionalProperties"] is False
    assert set(oracle_evidence_schema["properties"]) == {"card_id", "face_index", "quote"}
    assert set(oracle_evidence_schema["required"]) == set(oracle_evidence_schema["properties"])
    assert oracle_evidence_schema["properties"]["card_id"] == {"type": "integer", "minimum": 1}
    assert oracle_evidence_schema["properties"]["face_index"] == {
        "type": ["integer", "null"],
        "minimum": 0,
    }
    assert oracle_evidence_schema["properties"]["quote"] == {"type": "string", "minLength": 1}

    evidence_schema = capability_schema["properties"]["evidence"]
    assert evidence_schema["type"] == "array"
    assert evidence_schema["uniqueItems"] is True
    assert evidence_schema["minItems"] == 1
    assert evidence_schema["items"] == oracle_evidence_schema

    review_schema = capability_schema["properties"]["review"]
    assert review_schema["additionalProperties"] is False
    assert set(review_schema["properties"]) == {"status", "reason"}
    assert set(review_schema["required"]) == set(review_schema["properties"])
    assert review_schema["properties"]["status"]["enum"] == ["accepted", "rejected", "uncertain"]
    assert review_schema["properties"]["reason"] == {"type": ["string", "null"], "minLength": 1}

    object_nodes = _object_nodes(schema)
    assert object_nodes
    for node in object_nodes:
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])


def test_card_request_snapshots_are_isolated_from_reader_mutation() -> None:
    request = build_card_capability_extraction_request(
        sources=_capability_sources(),
        card_id=TWO_FACE_CARD_ID,
    )
    baseline_prompt = request.user_prompt
    baseline_hash = request.response_schema_sha256
    baseline_schema = request.response_schema()

    schema = request.response_schema()
    schema["type"] = "array"
    schema["properties"]["capabilities"]["items"]["properties"]["role"]["enum"].append("sideboard")

    prompt = json.loads(request.user_prompt)
    prompt["card"]["name"] = "Mutated"
    prompt["card"]["faces"][0]["oracle_text"] = "Mutated."

    assert request.response_schema_sha256 == baseline_hash
    assert request.response_schema() == baseline_schema

    rebuilt = build_card_capability_extraction_request(
        sources=_capability_sources(),
        card_id=TWO_FACE_CARD_ID,
    )
    assert rebuilt.user_prompt == baseline_prompt
    assert rebuilt.prompt_sha256 == request.prompt_sha256
    assert json.loads(rebuilt.user_prompt)["card"]["name"] == TWO_FACE_CARD_NAME


def test_card_trusted_argument_failures_raise_with_pinned_messages(
    capability_sources: EnrichmentSources,
) -> None:
    with pytest.raises(SetEnrichmentExtractionError) as error:
        build_card_capability_extraction_request(
            sources=None,  # type: ignore[arg-type]
            card_id=TWO_FACE_CARD_ID,
        )
    assert str(error.value) == "sources must be an EnrichmentSources record."

    with pytest.raises(SetEnrichmentExtractionError) as error:
        build_card_capability_extraction_request(
            sources=capability_sources,
            card_id=FOREIGN_CARD_ID,
        )
    assert str(error.value) == CARD_SELECTION_ERROR

    with pytest.raises(SetEnrichmentExtractionError) as error:
        build_card_capability_extraction_request(
            sources=capability_sources,
            card_id=True,  # type: ignore[arg-type]
        )
    assert str(error.value) == CARD_SELECTION_ERROR

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_card_capability_extraction_response(
            content=_content(_capability_response([])),
            sources=None,  # type: ignore[arg-type]
            card_id=TWO_FACE_CARD_ID,
            run_id=RUN_ID,
        )
    assert str(error.value) == "sources must be an EnrichmentSources record."

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_card_capability_extraction_response(
            content=_content(_capability_response([])),
            sources=capability_sources,
            card_id=FOREIGN_CARD_ID,
            run_id=RUN_ID,
        )
    assert str(error.value) == CARD_SELECTION_ERROR

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_card_capability_extraction_response(
            content=_content(_capability_response([])),
            sources=capability_sources,
            card_id=True,  # type: ignore[arg-type]
            run_id=RUN_ID,
        )
    assert str(error.value) == CARD_SELECTION_ERROR

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_card_capability_extraction_response(
            content=_content(_capability_response([])),
            sources=capability_sources,
            card_id=TWO_FACE_CARD_ID,
            run_id="   ",
        )
    assert str(error.value) == "run_id must be a nonblank string."

    with pytest.raises(SetEnrichmentExtractionError) as error:
        parse_card_capability_extraction_response(
            content=_content(_capability_response([])),
            sources=capability_sources,
            card_id=TWO_FACE_CARD_ID,
            run_id="bad\ud800",
        )
    assert str(error.value) == "run_id must be a nonblank string."

    malformed = _parse_card("not json", capability_sources)
    assert malformed.outcome is ExtractionOutcome.MALFORMED


@pytest.mark.parametrize(
    "capabilities",
    (
        pytest.param(
            [_capability_candidate(quantity=_quantity(relation=SURROGATE_RELATION))],
            id="surrogate-capability-quantity-relation",
        ),
        pytest.param(
            [
                _capability_candidate(
                    prerequisites=[
                        _prerequisite_entry(quantity=_quantity(relation=SURROGATE_RELATION))
                    ]
                )
            ],
            id="surrogate-prerequisite-quantity-relation",
        ),
        pytest.param(
            [
                _capability_candidate(),
                _capability_candidate(
                    finding_id="capability-surrogate",
                    quantity=_quantity(relation=SURROGATE_RELATION),
                ),
            ],
            id="valid-capability-beside-a-surrogate-capability",
        ),
    ),
)
def test_lone_surrogate_quantity_relations_are_malformed_whole_responses(
    capability_sources: EnrichmentSources,
    capabilities: list[dict[str, Any]],
) -> None:
    content = _content(_capability_response(capabilities))

    assert r"\ud800" in content

    result = _parse_card(content, capability_sources)

    assert result.outcome is ExtractionOutcome.MALFORMED
    assert result.malformed_reason == CARD_MALFORMED_REASON
    assert result.accepted_capabilities == ()
    assert result.uncertain_capabilities == ()
    assert result.rejected_capabilities == ()
    assert result.capabilities == ()


@pytest.mark.parametrize(
    "quantity",
    (
        pytest.param({"value": None, "relation": "about"}, id="unknown-relation-with-a-null-value"),
        pytest.param({"value": 2, "relation": "about"}, id="unknown-relation-with-an-integer-value"),
    ),
)
def test_model_rejection_outranks_unknown_quantity_vocabulary(
    capability_sources: EnrichmentSources,
    quantity: dict[str, Any],
) -> None:
    candidate = _capability_candidate(
        quantity=quantity,
        review={"status": "rejected", "reason": MODEL_REJECTION_REASON},
    )

    result = _parse_card(_content(_capability_response([candidate])), capability_sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.accepted_capabilities == ()
    assert result.uncertain_capabilities == ()
    assert result.capabilities == ()
    assert len(result.rejected_capabilities) == 1
    rejected = result.rejected_capabilities[0]
    assert rejected.finding_id == candidate["finding_id"]
    assert rejected.source_kind == "oracle"
    assert rejected.summary == candidate["role"]
    assert rejected.reason == MODEL_REJECTION_REASON
    assert rejected.run_id == RUN_ID


def test_kind_and_evidence_prerequisite_is_sufficient_without_semantics(
    capability_sources: EnrichmentSources,
) -> None:
    candidate = _capability_candidate(prerequisites=[_prerequisite_entry()])

    result = _parse_card(_content(_capability_response([candidate])), capability_sources)

    assert result.outcome is ExtractionOutcome.SUCCESS
    assert result.malformed_reason is None
    assert result.accepted_capabilities == ()
    assert len(result.uncertain_capabilities) == 1
    capability = result.uncertain_capabilities[0]
    assert capability.role is Role.DRAW
    assert capability.prerequisites == (
        CapabilityPrerequisite(
            kind=PrerequisiteKind.COST,
            quantity=None,
            timing=None,
            source_zone=None,
            destination_zone=None,
            evidence=OracleEvidence(
                card_id=TWO_FACE_CARD_ID,
                face_index=1,
                quote=BACK_MILL_QUOTE,
            ),
        ),
    )
    assert capability.review == FindingReview(
        status=FindingStatus.UNCERTAIN,
        reason=CAPABILITY_REVIEW_REASON,
    )
    assert result.capabilities == result.uncertain_capabilities
    assert CardCapability.from_json(capability.to_json()) == capability


def test_same_face_prerequisite_round_trips_exactly() -> None:
    record = _capability_with_prerequisite(_prerequisite_record())

    assert record.prerequisites == (_prerequisite_record(),)
    assert CardCapability.from_json(record.to_json()) == record


@pytest.mark.parametrize(
    "prerequisite",
    (
        pytest.param(_prerequisite_record(card_id=FOREIGN_CARD_ID), id="foreign-card-evidence"),
        pytest.param(_prerequisite_record(face_index=0), id="wrong-face-evidence"),
    ),
)
def test_capability_rejects_a_prerequisite_from_another_card_face(
    prerequisite: CapabilityPrerequisite,
) -> None:
    with pytest.raises(SemanticEnrichmentError):
        _capability_with_prerequisite(prerequisite)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("card_id", FOREIGN_CARD_ID, id="foreign-card-evidence"),
        pytest.param("face_index", 0, id="wrong-face-evidence"),
    ),
)
def test_capability_from_json_rejects_a_prerequisite_from_another_card_face(
    field: str,
    value: int,
) -> None:
    mutated: dict[str, Any] = _capability_with_prerequisite(_prerequisite_record()).to_json()
    mutated["prerequisites"][0]["evidence"][field] = value

    with pytest.raises(SemanticEnrichmentError):
        CardCapability.from_json(mutated)


def test_null_face_prerequisite_evidence_is_compared_exactly() -> None:
    null_face_prerequisite = _prerequisite_record(face_index=None)

    with pytest.raises(SemanticEnrichmentError):
        _capability_with_prerequisite(null_face_prerequisite)

    no_face_prerequisite = _prerequisite_record(
        card_id=PLAIN_CARD_ID,
        face_index=None,
        quote=PLAIN_CARD_TEXT,
    )
    no_face_record = _capability_with_prerequisite(
        no_face_prerequisite,
        card_id=PLAIN_CARD_ID,
        card_name=PLAIN_CARD_NAME,
        face_index=None,
        face_name=None,
        quote=PLAIN_CARD_TEXT,
    )

    assert CardCapability.from_json(no_face_record.to_json()) == no_face_record
