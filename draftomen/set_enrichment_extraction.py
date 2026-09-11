"""Pure source-bound extraction contracts for frozen set guides."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from enum import StrEnum
import hashlib
import json
import math
from typing import Any

from draftomen.carddb import CardInfo
from draftomen.semantic_enrichment import EnrichmentSources, GuideSource
from draftomen.semantic_enrichment_records import (
    FindingReview,
    FindingStatus,
    GuideClaim,
    GuideEvidence,
    RejectedFinding,
    SemanticEnrichmentError,
)


SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION = 1
GUIDE_EXTRACTION_PROMPT_ID = "draftomen-guide-extraction-v1"
GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID = "draftomen-guide-extraction-response-v1"
GUIDE_EXTRACTION_SCHEMA_NAME = "draftomen_guide_extraction_v1"

_GUIDE_SYSTEM_PROMPT = (
    "Extract only claims stated in the supplied frozen guide. Return an exact guide quotation "
    "for every finding and list exactly the IDs of all supplied cards whose full canonical "
    "names appear in the claim. Exact quotation proves source presence, not semantic "
    "correctness: do not mark a guide claim accepted. Mark extracted interpretations uncertain "
    "with a nonblank reason or reject unsupported candidates with a nonblank reason. Use "
    "category format_finding, mechanic, archetype, or strategy; named card interactions are "
    "strategy guide claims, never Oracle-validated relationships."
)

_MALFORMED_RESPONSE_REASON = "response does not match guide extraction schema version 1."
_SEMANTIC_REVIEW_REASON = "guide claim requires semantic review beyond exact-source validation."

_EVIDENCE_GUIDE_REASON = "guide evidence does not reference the selected guide."
_EVIDENCE_QUOTE_REASON = "guide evidence quote is not an exact source substring."
_UNKNOWN_CARD_REASON = "guide claim references a card outside the frozen source set."
_CARD_NAME_REASON = "referenced card IDs do not match card names stated in the claim."

_GUIDE_CATEGORIES = frozenset({"format_finding", "mechanic", "archetype", "strategy"})
_RESPONSE_STATUSES = frozenset({status.value for status in FindingStatus})
_RESPONSE_KEYS = frozenset({"schema_version", "findings"})
_FINDING_KEYS = frozenset(
    {
        "finding_id",
        "category",
        "name",
        "claim",
        "card_ids",
        "evidence",
        "review",
    }
)
_EVIDENCE_KEYS = frozenset({"guide_id", "quote"})
_REVIEW_KEYS = frozenset({"status", "reason"})
_MAX_SCHEMA_DEPTH = 64


class ExtractionOutcome(StrEnum):
    """Terminal outcome of one extraction response."""

    SUCCESS = "success"
    MALFORMED = "malformed"


class SetEnrichmentExtractionError(ValueError):
    """Raised when trusted extraction inputs violate their contract."""


class _MalformedGuideResponse(Exception):
    """Signal one response that violates the pinned guide extraction schema."""


def _utf8_encodable(value: str) -> bool:
    """Report whether one string can be encoded as UTF-8."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _identifier(value: Any, field_name: str) -> str:
    """Validate and strip a nonblank identifier."""
    if not isinstance(value, str) or not value.strip() or not _utf8_encodable(value):
        raise SetEnrichmentExtractionError(f"{field_name} must be a nonblank string.")
    return value.strip()


def _exact_text(value: Any, field_name: str) -> str:
    """Validate nonblank text without changing it."""
    if not isinstance(value, str) or not value.strip() or not _utf8_encodable(value):
        raise SetEnrichmentExtractionError(f"{field_name} must be nonblank text.")
    return value


def _canonical_bytes(value: Any) -> bytes:
    """Serialize one JSON-native value as canonical compact UTF-8 bytes."""
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
        raise SetEnrichmentExtractionError("value cannot be encoded as canonical JSON.") from error


def _snapshot_node(value: Any, *, depth: int, seen: set[int]) -> Any:
    """Detach one JSON-native schema node, rejecting unsupported values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SetEnrichmentExtractionError("schema must contain only finite JSON numbers.")
        return value
    if depth > _MAX_SCHEMA_DEPTH:
        raise SetEnrichmentExtractionError("schema must not nest beyond the supported depth.")
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            raise SetEnrichmentExtractionError("schema must not contain cyclic references.")
        seen.add(marker)
        try:
            snapshot: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not _utf8_encodable(key):
                    raise SetEnrichmentExtractionError("schema keys must be UTF-8 encodable strings.")
                snapshot[key] = _snapshot_node(item, depth=depth + 1, seen=seen)
            return snapshot
        finally:
            seen.discard(marker)
    if isinstance(value, list):
        marker = id(value)
        if marker in seen:
            raise SetEnrichmentExtractionError("schema must not contain cyclic references.")
        seen.add(marker)
        try:
            return [_snapshot_node(item, depth=depth + 1, seen=seen) for item in value]
        finally:
            seen.discard(marker)
    raise SetEnrichmentExtractionError("schema must contain only JSON-native values.")


def _snapshot_schema(schema: Any) -> dict[str, Any]:
    """Validate one caller schema and detach it from later mutation."""
    if not isinstance(schema, Mapping):
        raise SetEnrichmentExtractionError("schema must be a JSON object.")
    snapshot = _snapshot_node(schema, depth=1, seen=set())
    if not isinstance(snapshot, dict) or not snapshot:
        raise SetEnrichmentExtractionError("schema must be a nonempty JSON object.")
    return snapshot


def _schema_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Rebuild one schema object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SetEnrichmentExtractionError("schema snapshot contains duplicate keys.")
        result[key] = value
    return result


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    """Build one closed strict object schema with every property required."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _guide_response_schema() -> dict[str, Any]:
    """Return a fresh strict schema for one guide extraction response."""
    return _object_schema(
        {
            "schema_version": {
                "type": "integer",
                "const": SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
            },
            "findings": {
                "type": "array",
                "uniqueItems": True,
                "items": _object_schema(
                    {
                        "finding_id": {"type": "string", "minLength": 1},
                        "category": {"type": "string", "enum": sorted(_GUIDE_CATEGORIES)},
                        "name": {"type": "string", "minLength": 1},
                        "claim": {"type": "string", "minLength": 1},
                        "card_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "items": _object_schema(
                                {
                                    "guide_id": {"type": "string", "minLength": 1},
                                    "quote": {"type": "string", "minLength": 1},
                                }
                            ),
                        },
                        "review": _object_schema(
                            {
                                "status": {"type": "string", "enum": sorted(_RESPONSE_STATUSES)},
                                "reason": {"type": ["string", "null"], "minLength": 1},
                            }
                        ),
                    }
                ),
            },
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractionRequest:
    """Pinned immutable request built only from frozen inputs."""

    contract_version: int
    prompt_id: str
    system_prompt: str
    user_prompt: str
    response_schema_id: str
    response_schema_name: str
    schema: InitVar[Mapping[str, Any]]
    _schema_json: bytes = field(init=False, repr=False)

    def __post_init__(self, schema: Mapping[str, Any]) -> None:
        if isinstance(self.contract_version, bool) or not isinstance(self.contract_version, int):
            raise SetEnrichmentExtractionError("contract_version must be an integer.")
        if self.contract_version != SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION:
            raise SetEnrichmentExtractionError("contract_version is not supported.")
        for field_name in ("prompt_id", "response_schema_id", "response_schema_name"):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        for field_name in ("system_prompt", "user_prompt"):
            object.__setattr__(self, field_name, _exact_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "_schema_json", _canonical_bytes(_snapshot_schema(schema)))

    def response_schema(self) -> dict[str, Any]:
        """Return a fresh JSON-native copy of the pinned response schema."""
        decoded = json.loads(self._schema_json.decode("utf-8"), object_pairs_hook=_schema_object)
        if not isinstance(decoded, dict):
            raise SetEnrichmentExtractionError("schema must be a JSON object.")
        return decoded

    @property
    def prompt_sha256(self) -> str:
        """Return the digest of the canonical prompt pair."""
        payload = {"system_prompt": self.system_prompt, "user_prompt": self.user_prompt}
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

    @property
    def response_schema_sha256(self) -> str:
        """Return the digest of the exact stored schema bytes."""
        return hashlib.sha256(self._schema_json).hexdigest()


def _finding_tuple(
    value: Any,
    *,
    field_name: str,
    expected_type: type,
) -> tuple[Any, ...]:
    """Require exact record types, then canonicalize one tuple by finding id."""
    if not isinstance(value, tuple):
        raise SetEnrichmentExtractionError(f"{field_name} must be a tuple.")
    if any(type(item) is not expected_type for item in value):
        raise SetEnrichmentExtractionError(f"{field_name} must contain {expected_type.__name__} records.")
    identities = [item.finding_id for item in value]
    if len(set(identities)) != len(identities):
        raise SetEnrichmentExtractionError(f"{field_name} contains duplicate entries.")
    return tuple(sorted(value, key=lambda item: item.finding_id))


@dataclass(frozen=True, slots=True)
class GuideExtractionResult:
    """Terminal result of parsing one untrusted guide response."""

    outcome: ExtractionOutcome
    accepted_findings: tuple[GuideClaim, ...]
    uncertain_findings: tuple[GuideClaim, ...]
    rejected_findings: tuple[RejectedFinding, ...]
    malformed_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ExtractionOutcome):
            raise SetEnrichmentExtractionError("outcome must be an ExtractionOutcome.")
        accepted = _finding_tuple(
            self.accepted_findings,
            field_name="accepted_findings",
            expected_type=GuideClaim,
        )
        uncertain = _finding_tuple(
            self.uncertain_findings,
            field_name="uncertain_findings",
            expected_type=GuideClaim,
        )
        rejected = _finding_tuple(
            self.rejected_findings,
            field_name="rejected_findings",
            expected_type=RejectedFinding,
        )
        for claim in accepted:
            if claim.review.status is not FindingStatus.ACCEPTED or claim.review.reason is not None:
                raise SetEnrichmentExtractionError("accepted findings must be accepted without a reason.")
        for claim in uncertain:
            if claim.review.status is not FindingStatus.UNCERTAIN:
                raise SetEnrichmentExtractionError("uncertain findings must be uncertain.")
        for finding in rejected:
            if finding.source_kind != "guide":
                raise SetEnrichmentExtractionError("rejected findings must be guide findings.")
        identities = [claim.finding_id for claim in (*accepted, *uncertain)]
        identities.extend(finding.finding_id for finding in rejected)
        if len(set(identities)) != len(identities):
            raise SetEnrichmentExtractionError("findings must not repeat a finding_id.")
        if self.malformed_reason is not None:
            object.__setattr__(
                self,
                "malformed_reason",
                _exact_text(self.malformed_reason, "malformed_reason"),
            )
        if self.outcome is ExtractionOutcome.SUCCESS:
            if self.malformed_reason is not None:
                raise SetEnrichmentExtractionError("successful extraction must not retain a malformed reason.")
        else:
            if self.malformed_reason != _MALFORMED_RESPONSE_REASON:
                raise SetEnrichmentExtractionError("malformed extraction must state the fixed malformed reason.")
            if accepted or uncertain or rejected:
                raise SetEnrichmentExtractionError("malformed extraction must not retain findings.")
        object.__setattr__(self, "accepted_findings", accepted)
        object.__setattr__(self, "uncertain_findings", uncertain)
        object.__setattr__(self, "rejected_findings", rejected)

    @property
    def guide_claims(self) -> tuple[GuideClaim, ...]:
        """Return accepted and uncertain guide claims in global finding order."""
        claims = (*self.accepted_findings, *self.uncertain_findings)
        return tuple(sorted(claims, key=lambda claim: claim.finding_id))


def _selected_guide(sources: Any, guide_id: Any) -> GuideSource:
    """Select exactly one frozen guide source by identifier."""
    if not isinstance(sources, EnrichmentSources):
        raise SetEnrichmentExtractionError("sources must be an EnrichmentSources record.")
    normalized = ""
    if isinstance(guide_id, str) and guide_id.strip() and _utf8_encodable(guide_id):
        normalized = guide_id.strip()
    matches = [guide for guide in sources.guides if guide.guide_id == normalized]
    if not normalized or len(matches) != 1:
        raise SetEnrichmentExtractionError("guide_id must identify exactly one frozen guide source.")
    return matches[0]


def build_guide_extraction_request(
    *,
    sources: EnrichmentSources,
    guide_id: str,
) -> ExtractionRequest:
    """Build the pinned guide extraction request from frozen inputs."""
    selected = _selected_guide(sources, guide_id)
    user_prompt = _canonical_bytes(
        {
            "contract_version": SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
            "set_code": sources.set_code,
            "guide": {
                "guide_id": selected.guide_id,
                "sha256": selected.text_sha256,
                "text": selected.text,
            },
            "cards": [{"card_id": card.grp_id, "name": card.name} for card in sources.cards],
        }
    ).decode("utf-8")
    return ExtractionRequest(
        contract_version=SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
        prompt_id=GUIDE_EXTRACTION_PROMPT_ID,
        system_prompt=_GUIDE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_schema_id=GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID,
        response_schema_name=GUIDE_EXTRACTION_SCHEMA_NAME,
        schema=_guide_response_schema(),
    )


def _response_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Rebuild one response object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _MalformedGuideResponse
        result[key] = value
    return result


def _response_constant(value: str) -> Any:
    """Reject the NaN and Infinity JSON constants."""
    raise _MalformedGuideResponse


def _decode_response_document(content: Any) -> Any:
    """Decode strict JSON without duplicate keys or non-finite numbers."""
    if not isinstance(content, str):
        raise _MalformedGuideResponse
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _MalformedGuideResponse from error
    try:
        return json.loads(
            content,
            object_pairs_hook=_response_object,
            parse_constant=_response_constant,
        )
    except (ValueError, RecursionError) as error:
        raise _MalformedGuideResponse from error


def _require_keys(value: Mapping[str, Any], expected: frozenset[str]) -> None:
    """Require exactly the expected keys on one response object."""
    if set(value) != expected:
        raise _MalformedGuideResponse


def _response_text(value: Any) -> str:
    """Require nonblank UTF-8 response text without rewriting it."""
    if not isinstance(value, str) or not value.strip():
        raise _MalformedGuideResponse
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _MalformedGuideResponse from error
    return value


def _validated_candidate(item: Any) -> Mapping[str, Any]:
    """Validate one response finding structurally before any record is built."""
    if not isinstance(item, Mapping):
        raise _MalformedGuideResponse
    _require_keys(item, _FINDING_KEYS)
    _response_text(item["finding_id"])
    category = item["category"]
    if not isinstance(category, str) or category not in _GUIDE_CATEGORIES:
        raise _MalformedGuideResponse
    _response_text(item["name"])
    _response_text(item["claim"])
    card_ids = item["card_ids"]
    if not isinstance(card_ids, list):
        raise _MalformedGuideResponse
    seen_ids: set[int] = set()
    for card_id in card_ids:
        if isinstance(card_id, bool) or not isinstance(card_id, int) or card_id <= 0:
            raise _MalformedGuideResponse
        if card_id in seen_ids:
            raise _MalformedGuideResponse
        seen_ids.add(card_id)
    evidence = item["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise _MalformedGuideResponse
    seen_evidence: set[tuple[str, str]] = set()
    for entry in evidence:
        if not isinstance(entry, Mapping):
            raise _MalformedGuideResponse
        _require_keys(entry, _EVIDENCE_KEYS)
        reference = (_response_text(entry["guide_id"]), _response_text(entry["quote"]))
        if reference in seen_evidence:
            raise _MalformedGuideResponse
        seen_evidence.add(reference)
    review = item["review"]
    if not isinstance(review, Mapping):
        raise _MalformedGuideResponse
    _require_keys(review, _REVIEW_KEYS)
    status = review["status"]
    if not isinstance(status, str) or status not in _RESPONSE_STATUSES:
        raise _MalformedGuideResponse
    reason = review["reason"]
    if status == FindingStatus.ACCEPTED.value:
        if reason is not None:
            raise _MalformedGuideResponse
    else:
        _response_text(reason)
    return item


def _response_candidates(document: Any) -> list[Mapping[str, Any]]:
    """Validate the whole response document before retaining any finding."""
    if not isinstance(document, Mapping):
        raise _MalformedGuideResponse
    _require_keys(document, _RESPONSE_KEYS)
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _MalformedGuideResponse
    if version != SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION:
        raise _MalformedGuideResponse
    findings = document["findings"]
    if not isinstance(findings, list):
        raise _MalformedGuideResponse
    candidates: list[Mapping[str, Any]] = []
    identities: set[str] = set()
    for item in findings:
        candidate = _validated_candidate(item)
        finding_id = candidate["finding_id"].strip()
        if finding_id in identities:
            raise _MalformedGuideResponse
        identities.add(finding_id)
        candidates.append(candidate)
    return candidates


def _source_reference_reason(
    evidence: tuple[GuideEvidence, ...],
    card_ids: tuple[int, ...],
    *,
    guide: GuideSource,
    known_card_ids: frozenset[int],
) -> str | None:
    """Return the first fixed reason for a broken source reference."""
    if any(item.guide_id != guide.guide_id for item in evidence):
        return _EVIDENCE_GUIDE_REASON
    if any(item.quote not in guide.text for item in evidence):
        return _EVIDENCE_QUOTE_REASON
    if any(card_id not in known_card_ids for card_id in card_ids):
        return _UNKNOWN_CARD_REASON
    return None


def _rejected(
    candidate: Mapping[str, Any],
    *,
    reason: str,
    run_id: str,
) -> RejectedFinding:
    """Build one guide diagnostic that preserves the candidate identity."""
    return RejectedFinding(
        finding_id=candidate["finding_id"],
        source_kind="guide",
        summary=candidate["claim"],
        reason=reason,
        run_id=run_id,
    )


def _classify_candidates(
    candidates: list[Mapping[str, Any]],
    *,
    guide: GuideSource,
    cards: tuple[CardInfo, ...],
    run_id: str,
) -> tuple[list[GuideClaim], list[RejectedFinding]]:
    """Validate source references and demote every guide claim to uncertain."""
    known_card_ids = frozenset(card.grp_id for card in cards)
    uncertain: list[GuideClaim] = []
    rejected: list[RejectedFinding] = []
    for candidate in candidates:
        evidence = tuple(
            GuideEvidence(guide_id=entry["guide_id"], quote=entry["quote"])
            for entry in candidate["evidence"]
        )
        card_ids = tuple(candidate["card_ids"])
        status = FindingStatus(candidate["review"]["status"])
        reason = candidate["review"]["reason"]
        reference_reason = _source_reference_reason(
            evidence,
            card_ids,
            guide=guide,
            known_card_ids=known_card_ids,
        )
        if reference_reason is not None:
            rejected.append(_rejected(candidate, reason=reference_reason, run_id=run_id))
            continue
        if status is FindingStatus.REJECTED:
            rejected.append(_rejected(candidate, reason=reason, run_id=run_id))
            continue
        named_card_ids = {card.grp_id for card in cards if card.name in candidate["claim"]}
        if named_card_ids != set(card_ids):
            rejected.append(_rejected(candidate, reason=_CARD_NAME_REASON, run_id=run_id))
            continue
        if status is FindingStatus.ACCEPTED:
            claim_review = FindingReview(status=FindingStatus.UNCERTAIN, reason=_SEMANTIC_REVIEW_REASON)
        else:
            claim_review = FindingReview(status=FindingStatus.UNCERTAIN, reason=reason)
        uncertain.append(
            GuideClaim(
                finding_id=candidate["finding_id"],
                category=candidate["category"],
                name=candidate["name"],
                claim=candidate["claim"],
                card_ids=card_ids,
                evidence=evidence,
                review=claim_review,
                run_id=run_id,
            )
        )
    return uncertain, rejected


def _malformed_result() -> GuideExtractionResult:
    """Return the fixed all-or-nothing malformed outcome."""
    return GuideExtractionResult(
        outcome=ExtractionOutcome.MALFORMED,
        accepted_findings=(),
        uncertain_findings=(),
        rejected_findings=(),
        malformed_reason=_MALFORMED_RESPONSE_REASON,
    )


def parse_guide_extraction_response(
    *,
    content: str,
    sources: EnrichmentSources,
    guide_id: str,
    run_id: str,
) -> GuideExtractionResult:
    """Parse one untrusted guide response against frozen sources."""
    guide = _selected_guide(sources, guide_id)
    normalized_run_id = _identifier(run_id, "run_id")
    try:
        candidates = _response_candidates(_decode_response_document(content))
    except (_MalformedGuideResponse, SemanticEnrichmentError):
        return _malformed_result()
    try:
        uncertain, rejected = _classify_candidates(
            candidates,
            guide=guide,
            cards=sources.cards,
            run_id=normalized_run_id,
        )
    except SemanticEnrichmentError:
        return _malformed_result()
    return GuideExtractionResult(
        outcome=ExtractionOutcome.SUCCESS,
        accepted_findings=(),
        uncertain_findings=tuple(uncertain),
        rejected_findings=tuple(rejected),
        malformed_reason=None,
    )


__all__ = [
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
]
