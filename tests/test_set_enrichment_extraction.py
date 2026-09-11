"""Behavior tests for the pure guide extraction contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
import re
from typing import Any

import pytest

from draftomen.carddb import CardInfo
from draftomen.openrouter_client import OpenRouterClient
from draftomen.semantic_enrichment import EnrichmentSources, GuideSource
from draftomen.semantic_enrichment_records import (
    CardRelationship,
    FindingReview,
    FindingStatus,
    GuideClaim,
    GuideEvidence,
    RejectedFinding,
)
import draftomen.set_enrichment_extraction as extraction_module
from draftomen.set_enrichment_extraction import (
    GUIDE_EXTRACTION_PROMPT_ID,
    GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID,
    GUIDE_EXTRACTION_SCHEMA_NAME,
    SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
    ExtractionOutcome,
    ExtractionRequest,
    GuideExtractionResult,
    SetEnrichmentExtractionError,
    build_guide_extraction_request,
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


def test_public_surface_pins_contract_values_and_outcomes() -> None:
    assert set(extraction_module.__all__) == {
        "ExtractionOutcome",
        "ExtractionRequest",
        "GUIDE_EXTRACTION_PROMPT_ID",
        "GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID",
        "GUIDE_EXTRACTION_SCHEMA_NAME",
        "GuideExtractionResult",
        "SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION",
        "SetEnrichmentExtractionError",
        "build_guide_extraction_request",
        "parse_guide_extraction_response",
    }
    assert isinstance(SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION, int)
    assert SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION == 1
    assert GUIDE_EXTRACTION_PROMPT_ID == "draftomen-guide-extraction-v1"
    assert GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID == "draftomen-guide-extraction-response-v1"
    assert GUIDE_EXTRACTION_SCHEMA_NAME == "draftomen_guide_extraction_v1"
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
