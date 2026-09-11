"""Pure source-bound extraction contracts for frozen set guides, canonical cards and constructed relationship candidates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, field
from enum import Enum, StrEnum
import hashlib
import json
import math
from typing import Any

from draftomen.carddb import CardInfo
from draftomen.semantic_capability_records import (
    CapabilityPrerequisite,
    CapabilityQuantity,
    CapabilityZone,
    CardCapability,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import (
    EnrichmentSources,
    GuideSource,
    card_source_projection,
    card_source_sha256,
)
from draftomen.semantic_enrichment_records import (
    FindingReview,
    FindingStatus,
    GuideClaim,
    GuideEvidence,
    OracleEvidence,
    RejectedFinding,
    SemanticEnrichmentError,
)
from draftomen.semantic_roles import Role


SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION = 1
GUIDE_EXTRACTION_PROMPT_ID = "draftomen-guide-extraction-v1"
GUIDE_EXTRACTION_RESPONSE_SCHEMA_ID = "draftomen-guide-extraction-response-v1"
GUIDE_EXTRACTION_SCHEMA_NAME = "draftomen_guide_extraction_v1"
CARD_CAPABILITY_EXTRACTION_PROMPT_ID = "draftomen-card-capability-extraction-v1"
CARD_CAPABILITY_EXTRACTION_RESPONSE_SCHEMA_ID = "draftomen-card-capability-extraction-response-v1"
CARD_CAPABILITY_EXTRACTION_SCHEMA_NAME = "draftomen_card_capability_extraction_v1"
RELATIONSHIP_VALIDATION_PROMPT_ID = "draftomen-relationship-validation-v1"
RELATIONSHIP_VALIDATION_RESPONSE_SCHEMA_ID = "draftomen-relationship-validation-response-v1"
RELATIONSHIP_VALIDATION_SCHEMA_NAME = "draftomen_relationship_validation_v1"

_GUIDE_SYSTEM_PROMPT = (
    "Extract only claims stated in the supplied frozen guide. Return an exact guide quotation "
    "for every finding and list exactly the IDs of all supplied cards whose full canonical "
    "names appear in the claim. Exact quotation proves source presence, not semantic "
    "correctness: do not mark a guide claim accepted. Mark extracted interpretations uncertain "
    "with a nonblank reason or reject unsupported candidates with a nonblank reason. Use "
    "category format_finding, mechanic, archetype, or strategy; named card interactions are "
    "strategy guide claims, never Oracle-validated relationships."
)

_CARD_CAPABILITY_SYSTEM_PROMPT = (
    "Extract only capabilities stated by a complete ability of the supplied canonical card. "
    "Treat the canonical card and all faces as one request. Use the supplied semantic role "
    "vocabulary. A triggered ability takes the role of its trigger condition before the role of its "
    "effect: an ability that triggers when one or more creatures die is a death payoff "
    "(death_payoff) even when its effect draws, scries, damages, or gains life, and an ability that "
    "triggers only on this card's own death is a dies trigger (dies_trigger). Use an effect-derived "
    "role when no more specific trigger role applies."
    " Quote the complete controlling cost, trigger, or condition together with its "
    "effect, and bind every capability and prerequisite to the selected card and face with exact "
    "Oracle quotations. Preserve quantities, timing, source and destination zones, and structured "
    "prerequisites only when the quoted ability states them; otherwise use null or an empty list. "
    "Mark an interpretation uncertain with a nonblank reason whenever any semantic field is "
    "ambiguous, and reject unsupported candidates with a nonblank reason. Never infer another card "
    "or emit cross-card relationships."
)

_RELATIONSHIP_SYSTEM_PROMPT = (
    "Validate only the declared mechanism between the two listed cards. Accept the declared "
    "interaction only when both supplied Oracle texts support it, and quote substrings copied "
    "exactly from the listed Oracle text of each participant. Never rename, re-identify, or "
    "introduce another card, and never restate a mechanism other than the declared one. Return "
    "only the pinned JSON object: an accepted verdict carries a null reason, while uncertain "
    "and rejected verdicts require a nonblank reason."
)

_MALFORMED_RESPONSE_REASON = "response does not match guide extraction schema version 1."
_SEMANTIC_REVIEW_REASON = "guide claim requires semantic review beyond exact-source validation."

_EVIDENCE_GUIDE_REASON = "guide evidence does not reference the selected guide."
_EVIDENCE_QUOTE_REASON = "guide evidence quote is not an exact source substring."
_UNKNOWN_CARD_REASON = "guide claim references a card outside the frozen source set."
_CARD_NAME_REVIEW_REASON = (
    "guide claim referenced card IDs that are not named in the claim, so those references were dropped."
)

_CARD_MALFORMED_RESPONSE_REASON = (
    "response does not match card capability extraction schema version 1."
)
_CAPABILITY_SEMANTIC_REVIEW_REASON = (
    "capability requires semantic review beyond exact-source validation."
)
_CAPABILITY_VOCABULARY_REASON = "capability or condition uses unsupported vocabulary."
_CAPABILITY_CARD_ID_REASON = "capability references a card other than the selected canonical card."
_CAPABILITY_CARD_NAME_REASON = "capability card name does not match the selected canonical card."
_CAPABILITY_FACE_REASON = "capability face identity does not match the selected canonical card."
_CAPABILITY_EVIDENCE_OWNER_REASON = "Oracle evidence does not belong to the selected card face."
_CAPABILITY_EVIDENCE_QUOTE_REASON = "Oracle evidence quote is not an exact source substring."
_CARD_SELECTION_ERROR = "card_id must identify exactly one frozen canonical card."

_RELATIONSHIP_MALFORMED_RESPONSE_REASON = (
    "response does not match relationship validation schema version 1."
)
_RELATIONSHIP_EVIDENCE_OWNER_REASON = (
    "relationship Oracle evidence does not belong to a candidate participant."
)
_RELATIONSHIP_EVIDENCE_QUOTE_REASON = (
    "relationship Oracle evidence quote is not an exact source substring."
)
_RELATIONSHIP_EVIDENCE_COVERAGE_REASON = (
    "accepted relationships require Oracle evidence for both participants."
)
_RELATIONSHIP_REASON_FIELD_REASON = "relationship verdict and reason do not agree."
_RELATIONSHIP_PARTICIPANT_FACE_ERROR = "participant face_index must identify a face of its card."

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
_GUIDE_RESULT_KEYS = frozenset(
    {
        "outcome",
        "accepted_findings",
        "uncertain_findings",
        "rejected_findings",
        "malformed_reason",
    }
)
_MAX_SCHEMA_DEPTH = 64

_CARD_RESPONSE_KEYS = frozenset({"schema_version", "capabilities"})
_CAPABILITY_KEYS = frozenset(
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
    }
)
_QUANTITY_KEYS = frozenset({"value", "relation"})
_PREREQUISITE_KEYS = frozenset(
    {
        "kind",
        "quantity",
        "timing",
        "source_zone",
        "destination_zone",
        "evidence",
    }
)
_ORACLE_EVIDENCE_KEYS = frozenset({"card_id", "face_index", "quote"})
_CARD_RESULT_KEYS = frozenset(
    {
        "outcome",
        "accepted_capabilities",
        "uncertain_capabilities",
        "rejected_capabilities",
        "malformed_reason",
    }
)
_RELATIONSHIP_RESPONSE_KEYS = frozenset(
    {"schema_version", "verdict", "claim", "reason", "evidence"}
)
_RELATIONSHIP_KEYS = frozenset(
    {"mechanism", "source", "target", "claim", "evidence", "review", "run_id"}
)
_RELATIONSHIP_RESULT_KEYS = frozenset(
    {"outcome", "relationship", "rejected", "malformed_reason"}
)

_STATUS_VALUES = sorted(status.value for status in FindingStatus)
_ROLE_VALUES = sorted(member.value for member in Role)
_ZONE_VALUES = sorted(member.value for member in CapabilityZone)
_QUANTITY_RELATION_VALUES = sorted(member.value for member in QuantityRelation)
_PREREQUISITE_KIND_VALUES = sorted(member.value for member in PrerequisiteKind)


class ExtractionOutcome(StrEnum):
    """Terminal outcome of one extraction response."""

    SUCCESS = "success"
    MALFORMED = "malformed"


class SetEnrichmentExtractionError(ValueError):
    """Raised when trusted extraction inputs violate their contract."""


class _MalformedResponse(Exception):
    """Signal one response that violates a pinned extraction schema."""


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


# OpenAI strict structured outputs reject `uniqueItems`, so response array uniqueness is enforced by
# the response validators (`_response_candidates`, `_validated_candidate`,
# `_validated_capability_candidate`, `_card_capability_candidates`) instead of by these schemas.
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
                "items": _object_schema(
                    {
                        "finding_id": {"type": "string", "minLength": 1},
                        "category": {"type": "string", "enum": sorted(_GUIDE_CATEGORIES)},
                        "name": {"type": "string", "minLength": 1},
                        "claim": {"type": "string", "minLength": 1},
                        "card_ids": {
                            "type": "array",
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


def _quantity_schema() -> dict[str, Any]:
    """Return a fresh strict schema for one capability quantity object."""
    return _object_schema(
        {
            "value": {"type": ["integer", "null"], "minimum": 1},
            "relation": {"type": "string", "enum": list(_QUANTITY_RELATION_VALUES)},
        }
    )


def _nullable_quantity_schema() -> dict[str, Any]:
    """Return a fresh schema accepting null or one quantity object."""
    return {"anyOf": [{"type": "null"}, _quantity_schema()]}


def _nullable_zone_schema() -> dict[str, Any]:
    """Return a fresh schema accepting null or one zone value."""
    return {"type": ["string", "null"], "enum": [*_ZONE_VALUES, None]}


def _oracle_evidence_schema() -> dict[str, Any]:
    """Return a fresh strict schema for one Oracle evidence object."""
    return _object_schema(
        {
            "card_id": {"type": "integer", "minimum": 1},
            "face_index": {"type": ["integer", "null"], "minimum": 0},
            "quote": {"type": "string", "minLength": 1},
        }
    )


def _prerequisite_schema() -> dict[str, Any]:
    """Return a fresh strict schema for one capability prerequisite object."""
    return _object_schema(
        {
            "kind": {"type": "string", "enum": list(_PREREQUISITE_KIND_VALUES)},
            "quantity": _nullable_quantity_schema(),
            "timing": {"type": ["string", "null"], "minLength": 1},
            "source_zone": _nullable_zone_schema(),
            "destination_zone": _nullable_zone_schema(),
            "evidence": _oracle_evidence_schema(),
        }
    )


def _capability_schema() -> dict[str, Any]:
    """Return a fresh strict schema for one card capability object."""
    return _object_schema(
        {
            "finding_id": {"type": "string", "minLength": 1},
            "card_id": {"type": "integer", "minimum": 1},
            "card_name": {"type": "string", "minLength": 1},
            "face_index": {"type": ["integer", "null"], "minimum": 0},
            "face_name": {"type": ["string", "null"], "minLength": 1},
            "role": {"type": "string", "enum": list(_ROLE_VALUES)},
            "quantity": _nullable_quantity_schema(),
            "timing": {"type": ["string", "null"], "minLength": 1},
            "source_zone": _nullable_zone_schema(),
            "destination_zone": _nullable_zone_schema(),
            "prerequisites": {
                "type": "array",
                "items": _prerequisite_schema(),
            },
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": _oracle_evidence_schema(),
            },
            "review": _object_schema(
                {
                    "status": {"type": "string", "enum": sorted(_RESPONSE_STATUSES)},
                    "reason": {"type": ["string", "null"], "minLength": 1},
                }
            ),
        }
    )


def _card_capability_response_schema() -> dict[str, Any]:
    """Return a fresh strict schema for one card capability extraction response."""
    return _object_schema(
        {
            "schema_version": {
                "type": "integer",
                "const": SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
            },
            "capabilities": {
                "type": "array",
                "items": _capability_schema(),
            },
        }
    )


def _relationship_response_schema() -> dict[str, Any]:
    """Return a fresh strict schema for one relationship validation response."""
    return _object_schema(
        {
            "schema_version": {
                "type": "integer",
                "enum": [SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION],
            },
            "verdict": {"type": "string", "enum": list(_STATUS_VALUES)},
            "claim": {"type": "string", "minLength": 1},
            "reason": {"type": ["string", "null"]},
            "evidence": {"type": "array", "items": _oracle_evidence_schema()},
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


def _capability_prompt_projection(capability: CardCapability) -> dict[str, Any]:
    """Return the semantic capability fields one relationship request carries."""
    projection: dict[str, Any] = capability.to_json()
    del projection["review"]
    del projection["run_id"]
    return projection


def _validated_participants(source: Any, target: Any) -> tuple[CardCapability, CardCapability]:
    """Require two distinct concrete capabilities as relationship participants."""
    if type(source) is not CardCapability or type(target) is not CardCapability:
        raise SetEnrichmentExtractionError("participants must be CardCapability records.")
    if source.card_id == target.card_id:
        raise SetEnrichmentExtractionError("participants must be different cards.")
    return source, target


def _validated_participant_faces(
    source: CardCapability,
    target: CardCapability,
    *,
    source_card: CardInfo,
    target_card: CardInfo,
) -> None:
    """Require every participant to bind to a real face of its frozen card."""
    for capability, card in ((source, source_card), (target, target_card)):
        face_index = capability.face_index
        if face_index is not None and not 0 <= face_index < len(card.faces):
            raise SetEnrichmentExtractionError(_RELATIONSHIP_PARTICIPANT_FACE_ERROR)


def _evidence_order(item: OracleEvidence) -> tuple[int, int, str]:
    """Return the canonical order key of one Oracle evidence record."""
    return (item.card_id, -1 if item.face_index is None else item.face_index, item.quote)


def _canonical_evidence(value: tuple[OracleEvidence, ...]) -> tuple[OracleEvidence, ...]:
    """Deduplicate and canonically order decoded Oracle evidence records."""
    return tuple(sorted(set(value), key=_evidence_order))


def _participant_evidence(
    value: Any,
    *,
    source: CardCapability,
    target: CardCapability,
) -> tuple[OracleEvidence, ...]:
    """Require exact evidence bound to one of the two relationship participants."""
    if not isinstance(value, tuple):
        raise SetEnrichmentExtractionError("evidence must be a tuple.")
    if not value:
        raise SetEnrichmentExtractionError("evidence must not be empty.")
    if any(type(item) is not OracleEvidence for item in value):
        raise SetEnrichmentExtractionError("evidence must contain OracleEvidence records.")
    if len(set(value)) != len(value):
        raise SetEnrichmentExtractionError("evidence must not repeat an entry.")
    owned = {(source.card_id, source.face_index), (target.card_id, target.face_index)}
    if any((item.card_id, item.face_index) not in owned for item in value):
        raise SetEnrichmentExtractionError("evidence must belong to a candidate participant.")
    return tuple(sorted(value, key=_evidence_order))


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

    def to_json(self) -> dict[str, object]:
        """Return fresh JSON-compatible stored bytes for this result."""
        return {
            "outcome": self.outcome.value,
            "accepted_findings": [claim.to_json() for claim in self.accepted_findings],
            "uncertain_findings": [claim.to_json() for claim in self.uncertain_findings],
            "rejected_findings": [finding.to_json() for finding in self.rejected_findings],
            "malformed_reason": self.malformed_reason,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> GuideExtractionResult:
        """Decode validated stored bytes into one exact guide extraction result."""
        _result_keys(value, _GUIDE_RESULT_KEYS, "guide extraction result")
        return cls(
            outcome=_extraction_outcome(value["outcome"]),
            accepted_findings=_nested_records(
                value["accepted_findings"],
                "accepted_findings",
                GuideClaim.from_json,
            ),
            uncertain_findings=_nested_records(
                value["uncertain_findings"],
                "uncertain_findings",
                GuideClaim.from_json,
            ),
            rejected_findings=_nested_records(
                value["rejected_findings"],
                "rejected_findings",
                RejectedFinding.from_json,
            ),
            malformed_reason=value["malformed_reason"],
        )


@dataclass(frozen=True, slots=True)
class CardCapabilityExtractionResult:
    """Terminal result of parsing one untrusted card capability response."""

    outcome: ExtractionOutcome
    accepted_capabilities: tuple[CardCapability, ...]
    uncertain_capabilities: tuple[CardCapability, ...]
    rejected_capabilities: tuple[RejectedFinding, ...]
    malformed_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ExtractionOutcome):
            raise SetEnrichmentExtractionError("outcome must be an ExtractionOutcome.")
        accepted = _finding_tuple(
            self.accepted_capabilities,
            field_name="accepted_capabilities",
            expected_type=CardCapability,
        )
        uncertain = _finding_tuple(
            self.uncertain_capabilities,
            field_name="uncertain_capabilities",
            expected_type=CardCapability,
        )
        rejected = _finding_tuple(
            self.rejected_capabilities,
            field_name="rejected_capabilities",
            expected_type=RejectedFinding,
        )
        for capability in accepted:
            if (
                capability.review.status is not FindingStatus.ACCEPTED
                or capability.review.reason is not None
            ):
                raise SetEnrichmentExtractionError(
                    "accepted capabilities must be accepted without a reason."
                )
        for capability in uncertain:
            if capability.review.status is not FindingStatus.UNCERTAIN:
                raise SetEnrichmentExtractionError("uncertain capabilities must be uncertain.")
        for finding in rejected:
            if finding.source_kind != "oracle":
                raise SetEnrichmentExtractionError("rejected capabilities must be oracle findings.")
        identities = [capability.finding_id for capability in (*accepted, *uncertain)]
        identities.extend(finding.finding_id for finding in rejected)
        if len(set(identities)) != len(identities):
            raise SetEnrichmentExtractionError("capabilities must not repeat a finding_id.")
        if self.malformed_reason is not None:
            object.__setattr__(
                self,
                "malformed_reason",
                _exact_text(self.malformed_reason, "malformed_reason"),
            )
        if self.outcome is ExtractionOutcome.SUCCESS:
            if self.malformed_reason is not None:
                raise SetEnrichmentExtractionError(
                    "successful extraction must not retain a malformed reason."
                )
        else:
            if self.malformed_reason != _CARD_MALFORMED_RESPONSE_REASON:
                raise SetEnrichmentExtractionError(
                    "malformed extraction must state the fixed malformed reason."
                )
            if accepted or uncertain or rejected:
                raise SetEnrichmentExtractionError(
                    "malformed extraction must not retain capabilities."
                )
        object.__setattr__(self, "accepted_capabilities", accepted)
        object.__setattr__(self, "uncertain_capabilities", uncertain)
        object.__setattr__(self, "rejected_capabilities", rejected)

    @property
    def capabilities(self) -> tuple[CardCapability, ...]:
        """Return accepted and uncertain capabilities in global finding order."""
        capabilities = (*self.accepted_capabilities, *self.uncertain_capabilities)
        return tuple(sorted(capabilities, key=lambda capability: capability.finding_id))

    def to_json(self) -> dict[str, object]:
        """Return fresh JSON-compatible stored bytes for this result."""
        return {
            "outcome": self.outcome.value,
            "accepted_capabilities": [
                capability.to_json() for capability in self.accepted_capabilities
            ],
            "uncertain_capabilities": [
                capability.to_json() for capability in self.uncertain_capabilities
            ],
            "rejected_capabilities": [
                finding.to_json() for finding in self.rejected_capabilities
            ],
            "malformed_reason": self.malformed_reason,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CardCapabilityExtractionResult:
        """Decode validated stored bytes into one exact card capability result."""
        _result_keys(value, _CARD_RESULT_KEYS, "card capability extraction result")
        return cls(
            outcome=_extraction_outcome(value["outcome"]),
            accepted_capabilities=_nested_records(
                value["accepted_capabilities"],
                "accepted_capabilities",
                CardCapability.from_json,
            ),
            uncertain_capabilities=_nested_records(
                value["uncertain_capabilities"],
                "uncertain_capabilities",
                CardCapability.from_json,
            ),
            rejected_capabilities=_nested_records(
                value["rejected_capabilities"],
                "rejected_capabilities",
                RejectedFinding.from_json,
            ),
            malformed_reason=value["malformed_reason"],
        )


@dataclass(frozen=True, slots=True)
class ValidatedRelationship:
    """One model verdict bound to a constructed candidate's exact participants."""

    mechanism: str
    source: CardCapability
    target: CardCapability
    claim: str
    evidence: tuple[OracleEvidence, ...]
    review: FindingReview
    run_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mechanism", _identifier(self.mechanism, "mechanism"))
        source, target = _validated_participants(self.source, self.target)
        object.__setattr__(self, "claim", _exact_text(self.claim, "claim"))
        object.__setattr__(
            self,
            "evidence",
            _participant_evidence(self.evidence, source=source, target=target),
        )
        if not isinstance(self.review, FindingReview):
            raise SetEnrichmentExtractionError("review must be a FindingReview.")
        if self.review.status is FindingStatus.REJECTED:
            raise SetEnrichmentExtractionError("rejected relationships must be diagnostics.")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))

    @property
    def finding_id(self) -> str:
        """Return the durable identity shared with the constructed candidate package."""
        return relationship_subject_id(
            mechanism=self.mechanism,
            source=self.source,
            target=self.target,
        )

    @property
    def identity(self) -> tuple[str, int, str, int, str]:
        """Return the candidate identity this relationship is bound to."""
        return (
            self.mechanism,
            self.source.card_id,
            self.source.finding_id,
            self.target.card_id,
            self.target.finding_id,
        )

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible relationship object."""
        return {
            "mechanism": self.mechanism,
            "source": self.source.to_json(),
            "target": self.target.to_json(),
            "claim": self.claim,
            "evidence": [item.to_json() for item in self.evidence],
            "review": self.review.to_json(),
            "run_id": self.run_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ValidatedRelationship:
        """Decode validated stored bytes into one exact validated relationship."""
        _result_keys(value, _RELATIONSHIP_KEYS, "validated relationship")
        return cls(
            mechanism=value["mechanism"],
            source=_stored_record(value["source"], "source", CardCapability.from_json),
            target=_stored_record(value["target"], "target", CardCapability.from_json),
            claim=value["claim"],
            evidence=_nested_records(value["evidence"], "evidence", OracleEvidence.from_json),
            review=_stored_record(value["review"], "review", FindingReview.from_json),
            run_id=value["run_id"],
        )


@dataclass(frozen=True, slots=True)
class RelationshipValidationResult:
    """Terminal result of parsing one untrusted relationship validation response."""

    outcome: ExtractionOutcome
    relationship: ValidatedRelationship | None
    rejected: RejectedFinding | None
    malformed_reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ExtractionOutcome):
            raise SetEnrichmentExtractionError("outcome must be an ExtractionOutcome.")
        if self.relationship is not None and type(self.relationship) is not ValidatedRelationship:
            raise SetEnrichmentExtractionError("relationship must be a ValidatedRelationship.")
        if self.rejected is not None and type(self.rejected) is not RejectedFinding:
            raise SetEnrichmentExtractionError("rejected must be a RejectedFinding.")
        if self.malformed_reason is not None:
            object.__setattr__(
                self,
                "malformed_reason",
                _exact_text(self.malformed_reason, "malformed_reason"),
            )
        if self.outcome is ExtractionOutcome.SUCCESS:
            if self.malformed_reason is not None:
                raise SetEnrichmentExtractionError(
                    "successful validation must not retain a malformed reason."
                )
            if (self.relationship is None) == (self.rejected is None):
                raise SetEnrichmentExtractionError(
                    "successful validation must retain exactly one verdict."
                )
        else:
            if self.malformed_reason != _RELATIONSHIP_MALFORMED_RESPONSE_REASON:
                raise SetEnrichmentExtractionError(
                    "malformed validation must state the fixed malformed reason."
                )
            if self.relationship is not None or self.rejected is not None:
                raise SetEnrichmentExtractionError(
                    "malformed validation must not retain a verdict."
                )
        if self.rejected is not None and self.rejected.source_kind != "relationship":
            raise SetEnrichmentExtractionError(
                "rejected validation must be a relationship diagnostic."
            )

    def to_json(self) -> dict[str, object]:
        """Return fresh JSON-compatible stored bytes for this result."""
        return {
            "outcome": self.outcome.value,
            "relationship": (
                self.relationship.to_json() if self.relationship is not None else None
            ),
            "rejected": self.rejected.to_json() if self.rejected is not None else None,
            "malformed_reason": self.malformed_reason,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RelationshipValidationResult:
        """Decode validated stored bytes into one exact relationship validation result."""
        _result_keys(value, _RELATIONSHIP_RESULT_KEYS, "relationship validation result")
        relationship = value["relationship"]
        rejected = value["rejected"]
        return cls(
            outcome=_extraction_outcome(value["outcome"]),
            relationship=(
                None
                if relationship is None
                else _stored_record(
                    relationship,
                    "relationship",
                    ValidatedRelationship.from_json,
                )
            ),
            rejected=(
                None
                if rejected is None
                else _stored_record(rejected, "rejected", RejectedFinding.from_json)
            ),
            malformed_reason=value["malformed_reason"],
        )


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


def _selected_card(sources: Any, card_id: Any) -> CardInfo:
    """Select exactly one frozen canonical card by identifier."""
    if not isinstance(sources, EnrichmentSources):
        raise SetEnrichmentExtractionError("sources must be an EnrichmentSources record.")
    matches: list[CardInfo] = []
    if isinstance(card_id, int) and not isinstance(card_id, bool) and card_id > 0:
        matches = [card for card in sources.cards if card.grp_id == card_id]
    if len(matches) != 1:
        raise SetEnrichmentExtractionError(_CARD_SELECTION_ERROR)
    return matches[0]


def build_card_capability_extraction_request(
    *,
    sources: EnrichmentSources,
    card_id: int,
) -> ExtractionRequest:
    """Build the pinned card capability extraction request from frozen inputs."""
    selected = _selected_card(sources, card_id)
    projection: dict[str, Any] = card_source_projection(selected)
    faces: list[dict[str, Any]] = projection["faces"]
    card = {
        **projection,
        "faces": [{**face, "face_index": index} for index, face in enumerate(faces)],
    }
    user_prompt = _canonical_bytes(
        {
            "contract_version": SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
            "set_code": sources.set_code,
            "card_source_sha256": card_source_sha256(selected),
            "card": card,
        }
    ).decode("utf-8")
    return ExtractionRequest(
        contract_version=SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
        prompt_id=CARD_CAPABILITY_EXTRACTION_PROMPT_ID,
        system_prompt=_CARD_CAPABILITY_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_schema_id=CARD_CAPABILITY_EXTRACTION_RESPONSE_SCHEMA_ID,
        response_schema_name=CARD_CAPABILITY_EXTRACTION_SCHEMA_NAME,
        schema=_card_capability_response_schema(),
    )


def relationship_subject_id(
    *,
    mechanism: str,
    source: CardCapability,
    target: CardCapability,
) -> str:
    """Return the stable identity of one declared enabler-to-payoff relationship."""
    normalized_mechanism = _identifier(mechanism, "mechanism")
    source, target = _validated_participants(source, target)
    return (
        f"relationship:{normalized_mechanism}:{source.card_id}:{source.finding_id}"
        f":{target.card_id}:{target.finding_id}"
    )


def relationship_source_sha256(
    *,
    mechanism: str,
    source: CardCapability,
    target: CardCapability,
) -> str:
    """Hash the exact participant content sent for one declared mechanism."""
    normalized_mechanism = _identifier(mechanism, "mechanism")
    source, target = _validated_participants(source, target)
    payload = {
        "mechanism": normalized_mechanism,
        "source": _capability_prompt_projection(source),
        "target": _capability_prompt_projection(target),
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def build_relationship_validation_request(
    *,
    sources: EnrichmentSources,
    mechanism: str,
    source: CardCapability,
    target: CardCapability,
) -> ExtractionRequest:
    """Build the pinned relationship validation request from frozen inputs."""
    if not isinstance(sources, EnrichmentSources):
        raise SetEnrichmentExtractionError("sources must be an EnrichmentSources record.")
    normalized_mechanism = _identifier(mechanism, "mechanism")
    source, target = _validated_participants(source, target)
    source_card = _selected_card(sources, source.card_id)
    target_card = _selected_card(sources, target.card_id)
    _validated_participant_faces(source, target, source_card=source_card, target_card=target_card)
    user_prompt = _canonical_bytes(
        {
            "contract_version": SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
            "set_code": sources.set_code,
            "mechanism": normalized_mechanism,
            "source": _capability_prompt_projection(source),
            "target": _capability_prompt_projection(target),
        }
    ).decode("utf-8")
    return ExtractionRequest(
        contract_version=SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION,
        prompt_id=RELATIONSHIP_VALIDATION_PROMPT_ID,
        system_prompt=_RELATIONSHIP_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        response_schema_id=RELATIONSHIP_VALIDATION_RESPONSE_SCHEMA_ID,
        response_schema_name=RELATIONSHIP_VALIDATION_SCHEMA_NAME,
        schema=_relationship_response_schema(),
    )


def _response_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Rebuild one response object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _MalformedResponse
        result[key] = value
    return result


def _response_constant(value: str) -> Any:
    """Reject the NaN and Infinity JSON constants."""
    raise _MalformedResponse


def _decode_response_document(content: Any) -> Any:
    """Decode strict JSON without duplicate keys or non-finite numbers."""
    if not isinstance(content, str):
        raise _MalformedResponse
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _MalformedResponse from error
    try:
        return json.loads(
            content,
            object_pairs_hook=_response_object,
            parse_constant=_response_constant,
        )
    except (ValueError, RecursionError) as error:
        raise _MalformedResponse from error


def _require_keys(value: Mapping[str, Any], expected: frozenset[str]) -> None:
    """Require exactly the expected keys on one response object."""
    if set(value) != expected:
        raise _MalformedResponse


def _response_text(value: Any) -> str:
    """Require nonblank UTF-8 response text without rewriting it."""
    if not isinstance(value, str) or not value.strip():
        raise _MalformedResponse
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _MalformedResponse from error
    return value


def _optional_response_text(value: Any) -> None:
    """Require null or nonblank UTF-8 response text."""
    if value is not None:
        _response_text(value)


def _result_keys(value: Mapping[str, Any], expected: frozenset[str], field_name: str) -> None:
    """Require exactly the expected keys on one trusted stored result."""
    if not isinstance(value, Mapping):
        raise SetEnrichmentExtractionError(f"{field_name} must be an object.")
    if set(value) != expected:
        raise SetEnrichmentExtractionError(f"{field_name} has invalid keys.")


def _extraction_outcome(value: Any) -> ExtractionOutcome:
    """Decode one stored outcome string into an exact terminal outcome."""
    if not isinstance(value, str):
        raise SetEnrichmentExtractionError("outcome must be 'success' or 'malformed'.")
    try:
        return ExtractionOutcome(value)
    except ValueError as error:
        raise SetEnrichmentExtractionError("outcome must be 'success' or 'malformed'.") from error


def _nested_records(
    value: Any,
    field_name: str,
    loader: Callable[[Mapping[str, Any]], Any],
) -> tuple[Any, ...]:
    """Decode one stored JSON array of nested records in document order."""
    if not isinstance(value, list):
        raise SetEnrichmentExtractionError(f"{field_name} must be a JSON array.")
    records: list[Any] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise SetEnrichmentExtractionError(f"{field_name} must contain objects.")
        try:
            records.append(loader(entry))
        except SemanticEnrichmentError as error:
            raise SetEnrichmentExtractionError(f"{field_name} must contain valid records.") from error
    return tuple(records)


def _stored_record(
    value: Any,
    field_name: str,
    loader: Callable[[Mapping[str, Any]], Any],
) -> Any:
    """Decode one stored nested record in its canonical storage form."""
    if not isinstance(value, Mapping):
        raise SetEnrichmentExtractionError(f"{field_name} must be an object.")
    try:
        return loader(value)
    except SemanticEnrichmentError as error:
        raise SetEnrichmentExtractionError(f"{field_name} must be a valid record.") from error


def _validated_candidate(item: Any) -> Mapping[str, Any]:
    """Validate one response finding structurally before any record is built."""
    if not isinstance(item, Mapping):
        raise _MalformedResponse
    _require_keys(item, _FINDING_KEYS)
    _response_text(item["finding_id"])
    category = item["category"]
    if not isinstance(category, str) or category not in _GUIDE_CATEGORIES:
        raise _MalformedResponse
    _response_text(item["name"])
    _response_text(item["claim"])
    card_ids = item["card_ids"]
    if not isinstance(card_ids, list):
        raise _MalformedResponse
    seen_ids: set[int] = set()
    for card_id in card_ids:
        if isinstance(card_id, bool) or not isinstance(card_id, int) or card_id <= 0:
            raise _MalformedResponse
        if card_id in seen_ids:
            raise _MalformedResponse
        seen_ids.add(card_id)
    evidence = item["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise _MalformedResponse
    seen_evidence: set[tuple[str, str]] = set()
    for entry in evidence:
        if not isinstance(entry, Mapping):
            raise _MalformedResponse
        _require_keys(entry, _EVIDENCE_KEYS)
        reference = (_response_text(entry["guide_id"]), _response_text(entry["quote"]))
        if reference in seen_evidence:
            raise _MalformedResponse
        seen_evidence.add(reference)
    review = item["review"]
    if not isinstance(review, Mapping):
        raise _MalformedResponse
    _require_keys(review, _REVIEW_KEYS)
    status = review["status"]
    if not isinstance(status, str) or status not in _RESPONSE_STATUSES:
        raise _MalformedResponse
    reason = review["reason"]
    if status == FindingStatus.ACCEPTED.value:
        if reason is not None:
            raise _MalformedResponse
    else:
        _response_text(reason)
    return item


def _response_candidates(document: Any) -> list[Mapping[str, Any]]:
    """Validate the whole response document before retaining any finding."""
    if not isinstance(document, Mapping):
        raise _MalformedResponse
    _require_keys(document, _RESPONSE_KEYS)
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _MalformedResponse
    if version != SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION:
        raise _MalformedResponse
    findings = document["findings"]
    if not isinstance(findings, list):
        raise _MalformedResponse
    candidates: list[Mapping[str, Any]] = []
    identities: set[str] = set()
    for item in findings:
        candidate = _validated_candidate(item)
        finding_id = candidate["finding_id"].strip()
        if finding_id in identities:
            raise _MalformedResponse
        identities.add(finding_id)
        candidates.append(candidate)
    return candidates


def _validated_quantity(value: Any) -> Any:
    """Validate one nullable quantity object structurally before record construction."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _MalformedResponse
    _require_keys(value, _QUANTITY_KEYS)
    amount = value["value"]
    if amount is not None and (
        isinstance(amount, bool) or not isinstance(amount, int) or amount < 1
    ):
        raise _MalformedResponse
    relation = _response_text(value["relation"])
    if relation in _QUANTITY_RELATION_VALUES:
        if relation == QuantityRelation.VARIABLE.value:
            if amount is not None:
                raise _MalformedResponse
        elif amount is None:
            raise _MalformedResponse
    return value


def _validated_oracle_evidence(entry: Any) -> Mapping[str, Any]:
    """Validate one Oracle evidence object structurally before record construction."""
    if not isinstance(entry, Mapping):
        raise _MalformedResponse
    _require_keys(entry, _ORACLE_EVIDENCE_KEYS)
    card_id = entry["card_id"]
    if isinstance(card_id, bool) or not isinstance(card_id, int):
        raise _MalformedResponse
    face_index = entry["face_index"]
    if face_index is not None and (isinstance(face_index, bool) or not isinstance(face_index, int)):
        raise _MalformedResponse
    _response_text(entry["quote"])
    return entry


def _validated_prerequisite(entry: Any) -> Mapping[str, Any]:
    """Validate one capability prerequisite structurally before record construction."""
    if not isinstance(entry, Mapping):
        raise _MalformedResponse
    _require_keys(entry, _PREREQUISITE_KEYS)
    _response_text(entry["kind"])
    _validated_quantity(entry["quantity"])
    _optional_response_text(entry["timing"])
    _optional_response_text(entry["source_zone"])
    _optional_response_text(entry["destination_zone"])
    _validated_oracle_evidence(entry["evidence"])
    return entry


def _validated_capability_candidate(item: Any) -> Mapping[str, Any]:
    """Validate one response capability structurally before any record is built."""
    if not isinstance(item, Mapping):
        raise _MalformedResponse
    _require_keys(item, _CAPABILITY_KEYS)
    _response_text(item["finding_id"])
    card_id = item["card_id"]
    if isinstance(card_id, bool) or not isinstance(card_id, int):
        raise _MalformedResponse
    _response_text(item["card_name"])
    face_index = item["face_index"]
    if face_index is not None and (isinstance(face_index, bool) or not isinstance(face_index, int)):
        raise _MalformedResponse
    _optional_response_text(item["face_name"])
    _response_text(item["role"])
    _validated_quantity(item["quantity"])
    _optional_response_text(item["timing"])
    _optional_response_text(item["source_zone"])
    _optional_response_text(item["destination_zone"])
    prerequisites = item["prerequisites"]
    if not isinstance(prerequisites, list):
        raise _MalformedResponse
    seen_prerequisites: set[bytes] = set()
    for entry in prerequisites:
        reference = _canonical_bytes(_validated_prerequisite(entry))
        if reference in seen_prerequisites:
            raise _MalformedResponse
        seen_prerequisites.add(reference)
    evidence = item["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise _MalformedResponse
    seen_evidence: set[bytes] = set()
    for entry in evidence:
        reference = _canonical_bytes(_validated_oracle_evidence(entry))
        if reference in seen_evidence:
            raise _MalformedResponse
        seen_evidence.add(reference)
    review = item["review"]
    if not isinstance(review, Mapping):
        raise _MalformedResponse
    _require_keys(review, _REVIEW_KEYS)
    status = review["status"]
    if not isinstance(status, str) or status not in _RESPONSE_STATUSES:
        raise _MalformedResponse
    reason = review["reason"]
    if status == FindingStatus.ACCEPTED.value:
        if reason is not None:
            raise _MalformedResponse
    else:
        _response_text(reason)
    return item


def _card_capability_candidates(document: Any) -> list[Mapping[str, Any]]:
    """Validate the whole response document before retaining any capability."""
    if not isinstance(document, Mapping):
        raise _MalformedResponse
    _require_keys(document, _CARD_RESPONSE_KEYS)
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _MalformedResponse
    if version != SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION:
        raise _MalformedResponse
    capabilities = document["capabilities"]
    if not isinstance(capabilities, list):
        raise _MalformedResponse
    candidates: list[Mapping[str, Any]] = []
    identities: set[str] = set()
    for item in capabilities:
        candidate = _validated_capability_candidate(item)
        finding_id = candidate["finding_id"].strip()
        if finding_id in identities:
            raise _MalformedResponse
        identities.add(finding_id)
        candidates.append(candidate)
    return candidates


def _validated_relationship_evidence(entry: Any) -> Mapping[str, Any]:
    """Validate one relationship Oracle evidence object structurally."""
    if not isinstance(entry, Mapping):
        raise _MalformedResponse
    _require_keys(entry, _ORACLE_EVIDENCE_KEYS)
    card_id = entry["card_id"]
    if isinstance(card_id, bool) or not isinstance(card_id, int) or card_id < 1:
        raise _MalformedResponse
    face_index = entry["face_index"]
    if face_index is not None:
        if isinstance(face_index, bool) or not isinstance(face_index, int) or face_index < 0:
            raise _MalformedResponse
    _response_text(entry["quote"])
    return entry


def _relationship_document(document: Any) -> tuple[str, str, str | None, list[Mapping[str, Any]]]:
    """Validate one relationship response document before any verdict is retained."""
    if not isinstance(document, Mapping):
        raise _MalformedResponse
    _require_keys(document, _RELATIONSHIP_RESPONSE_KEYS)
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _MalformedResponse
    if version != SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION:
        raise _MalformedResponse
    verdict = document["verdict"]
    if not isinstance(verdict, str):
        raise _MalformedResponse
    if verdict not in _STATUS_VALUES:
        raise _MalformedResponse
    claim = _response_text(document["claim"])
    reason = document["reason"]
    _optional_response_text(reason)
    evidence = document["evidence"]
    if not isinstance(evidence, list):
        raise _MalformedResponse
    entries = [_validated_relationship_evidence(entry) for entry in evidence]
    return verdict, claim, reason, entries


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
    """Validate source references and drop card references the claim does not name.
    Every retained guide claim is demoted to uncertain for human review.
    """
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
        bound_card_ids = tuple(card_id for card_id in card_ids if card_id in named_card_ids)
        if bound_card_ids != card_ids:
            claim_review = FindingReview(status=FindingStatus.UNCERTAIN, reason=_CARD_NAME_REVIEW_REASON)
        elif status is FindingStatus.ACCEPTED:
            claim_review = FindingReview(status=FindingStatus.UNCERTAIN, reason=_SEMANTIC_REVIEW_REASON)
        else:
            claim_review = FindingReview(status=FindingStatus.UNCERTAIN, reason=reason)
        uncertain.append(
            GuideClaim(
                finding_id=candidate["finding_id"],
                category=candidate["category"],
                name=candidate["name"],
                claim=candidate["claim"],
                card_ids=bound_card_ids,
                evidence=evidence,
                review=claim_review,
                run_id=run_id,
            )
        )
    return uncertain, rejected


def _decoded_enum(value: str, field_name: str, enum_type: type[Enum]) -> Any:
    """Decode one structurally valid string into an exact enum member."""
    try:
        return enum_type(value)
    except ValueError as error:
        raise SemanticEnrichmentError(f"{field_name} uses unsupported vocabulary.") from error


def _decoded_optional_enum(value: str | None, field_name: str, enum_type: type[Enum]) -> Any:
    """Decode nullable structurally valid text into an exact enum member."""
    if value is None:
        return None
    return _decoded_enum(value, field_name, enum_type)


def _decoded_quantity(value: Mapping[str, Any] | None) -> CapabilityQuantity | None:
    """Decode one nullable quantity object into an exact record."""
    if value is None:
        return None
    return CapabilityQuantity(
        value=value["value"],
        relation=_decoded_enum(value["relation"], "relation", QuantityRelation),
    )


def _decoded_evidence(entry: Mapping[str, Any]) -> OracleEvidence:
    """Build one exact Oracle evidence record from a validated entry."""
    return OracleEvidence(
        card_id=entry["card_id"],
        face_index=entry["face_index"],
        quote=entry["quote"],
    )


def _decoded_prerequisite(entry: Mapping[str, Any]) -> CapabilityPrerequisite:
    """Build one typed prerequisite from a source-valid response entry."""
    return CapabilityPrerequisite(
        kind=_decoded_enum(entry["kind"], "kind", PrerequisiteKind),
        quantity=_decoded_quantity(entry["quantity"]),
        timing=entry["timing"],
        source_zone=_decoded_optional_enum(entry["source_zone"], "source_zone", CapabilityZone),
        destination_zone=_decoded_optional_enum(
            entry["destination_zone"], "destination_zone", CapabilityZone
        ),
        evidence=_decoded_evidence(entry["evidence"]),
    )


def _capability_rejected(
    candidate: Mapping[str, Any],
    *,
    reason: str,
    run_id: str,
) -> RejectedFinding:
    """Build one card capability diagnostic that preserves the candidate identity."""
    return RejectedFinding(
        finding_id=candidate["finding_id"],
        source_kind="oracle",
        summary=candidate["role"],
        reason=reason,
        run_id=run_id,
    )


def _capability_record(
    candidate: Mapping[str, Any],
    *,
    face_index: int | None,
    run_id: str,
) -> CardCapability:
    """Build one typed capability from a source-valid response candidate."""
    reason = candidate["review"]["reason"]
    if candidate["review"]["status"] == FindingStatus.ACCEPTED.value:
        reason = _CAPABILITY_SEMANTIC_REVIEW_REASON
    return CardCapability(
        finding_id=candidate["finding_id"],
        card_id=candidate["card_id"],
        card_name=candidate["card_name"],
        face_index=face_index,
        face_name=candidate["face_name"],
        role=_decoded_enum(candidate["role"], "role", Role),
        quantity=_decoded_quantity(candidate["quantity"]),
        timing=candidate["timing"],
        source_zone=_decoded_optional_enum(candidate["source_zone"], "source_zone", CapabilityZone),
        destination_zone=_decoded_optional_enum(
            candidate["destination_zone"], "destination_zone", CapabilityZone
        ),
        prerequisites=tuple(_decoded_prerequisite(entry) for entry in candidate["prerequisites"]),
        evidence=tuple(_decoded_evidence(entry) for entry in candidate["evidence"]),
        review=FindingReview(status=FindingStatus.UNCERTAIN, reason=reason),
        run_id=run_id,
    )


def _capability_evidence(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Collect capability and prerequisite evidence entries in document order."""
    entries: list[Mapping[str, Any]] = list(candidate["evidence"])
    entries.extend(prerequisite["evidence"] for prerequisite in candidate["prerequisites"])
    return entries


def _capability_source_reason(
    candidate: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> tuple[str | None, int | None]:
    """Return the first fixed source reason and the bound canonical face index."""
    if candidate["card_id"] != projection["card_id"]:
        return _CAPABILITY_CARD_ID_REASON, None
    if candidate["card_name"] != projection["name"]:
        return _CAPABILITY_CARD_NAME_REASON, None
    faces = projection["faces"]
    face_index = candidate["face_index"]
    face_name = candidate["face_name"]
    if faces:
        if (
            isinstance(face_index, bool)
            or not isinstance(face_index, int)
            or not 0 <= face_index < len(faces)
        ):
            return _CAPABILITY_FACE_REASON, None
        face = faces[face_index]
        if face_name != face["name"]:
            return _CAPABILITY_FACE_REASON, None
        source_text = face["oracle_text"] or ""
    else:
        if face_index is not None or face_name is not None:
            return _CAPABILITY_FACE_REASON, None
        source_text = projection["oracle_text"] or ""
    evidence = _capability_evidence(candidate)
    if any(
        entry["card_id"] != projection["card_id"] or entry["face_index"] != face_index
        for entry in evidence
    ):
        return _CAPABILITY_EVIDENCE_OWNER_REASON, None
    if any(entry["quote"] not in source_text for entry in evidence):
        return _CAPABILITY_EVIDENCE_QUOTE_REASON, None
    return None, face_index


def _classify_capabilities(
    candidates: list[Mapping[str, Any]],
    *,
    projection: Mapping[str, Any],
    run_id: str,
) -> tuple[list[CardCapability], list[RejectedFinding]]:
    """Bind every capability candidate to the selected canonical card and demote it."""
    uncertain: list[CardCapability] = []
    rejected: list[RejectedFinding] = []
    for candidate in candidates:
        source_reason, face_index = _capability_source_reason(candidate, projection)
        if source_reason is not None:
            rejected.append(_capability_rejected(candidate, reason=source_reason, run_id=run_id))
            continue
        model_reason = candidate["review"]["reason"]
        if candidate["review"]["status"] == FindingStatus.REJECTED.value:
            rejected.append(_capability_rejected(candidate, reason=model_reason, run_id=run_id))
            continue
        try:
            capability = _capability_record(candidate, face_index=face_index, run_id=run_id)
        except SemanticEnrichmentError:
            rejected.append(
                _capability_rejected(candidate, reason=_CAPABILITY_VOCABULARY_REASON, run_id=run_id)
            )
            continue
        uncertain.append(capability)
    return uncertain, rejected


def _participant_oracle_text(capability: CardCapability, card: CardInfo) -> str:
    """Return the exact frozen Oracle text one participant capability binds to."""
    face_index = capability.face_index
    if face_index is None:
        return card.oracle_text or ""
    faces = card.faces
    if 0 <= face_index < len(faces):
        return faces[face_index].oracle_text or ""
    return ""


def _relationship_verdict_reason(
    *,
    status: FindingStatus,
    reason: str | None,
    evidence: tuple[OracleEvidence, ...],
    source: CardCapability,
    target: CardCapability,
    source_card: CardInfo,
    target_card: CardInfo,
) -> str | None:
    """Return the first fixed reason for one verdict that breaks the relationship contract."""
    oracle_text = {
        (source.card_id, source.face_index): _participant_oracle_text(source, source_card),
        (target.card_id, target.face_index): _participant_oracle_text(target, target_card),
    }
    if any((item.card_id, item.face_index) not in oracle_text for item in evidence):
        return _RELATIONSHIP_EVIDENCE_OWNER_REASON
    if any(item.quote not in oracle_text[(item.card_id, item.face_index)] for item in evidence):
        return _RELATIONSHIP_EVIDENCE_QUOTE_REASON
    if status is FindingStatus.ACCEPTED:
        covered = {(item.card_id, item.face_index) for item in evidence}
        if any(participant not in covered for participant in oracle_text):
            return _RELATIONSHIP_EVIDENCE_COVERAGE_REASON
        if reason is not None:
            return _RELATIONSHIP_REASON_FIELD_REASON
    elif reason is None:
        return _RELATIONSHIP_REASON_FIELD_REASON
    return None


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
    except (_MalformedResponse, SemanticEnrichmentError):
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


def _malformed_card_result() -> CardCapabilityExtractionResult:
    """Return the fixed all-or-nothing malformed card outcome."""
    return CardCapabilityExtractionResult(
        outcome=ExtractionOutcome.MALFORMED,
        accepted_capabilities=(),
        uncertain_capabilities=(),
        rejected_capabilities=(),
        malformed_reason=_CARD_MALFORMED_RESPONSE_REASON,
    )


def parse_card_capability_extraction_response(
    *,
    content: str,
    sources: EnrichmentSources,
    card_id: int,
    run_id: str,
) -> CardCapabilityExtractionResult:
    """Parse one untrusted card capability response against frozen sources."""
    selected = _selected_card(sources, card_id)
    normalized_run_id = _identifier(run_id, "run_id")
    projection: Mapping[str, Any] = card_source_projection(selected)
    try:
        candidates = _card_capability_candidates(_decode_response_document(content))
    except (_MalformedResponse, SemanticEnrichmentError):
        return _malformed_card_result()
    try:
        uncertain, rejected = _classify_capabilities(
            candidates,
            projection=projection,
            run_id=normalized_run_id,
        )
    except SemanticEnrichmentError:
        return _malformed_card_result()
    return CardCapabilityExtractionResult(
        outcome=ExtractionOutcome.SUCCESS,
        accepted_capabilities=(),
        uncertain_capabilities=tuple(uncertain),
        rejected_capabilities=tuple(rejected),
        malformed_reason=None,
    )


def _malformed_relationship_result() -> RelationshipValidationResult:
    """Return the fixed all-or-nothing malformed relationship outcome."""
    return RelationshipValidationResult(
        outcome=ExtractionOutcome.MALFORMED,
        relationship=None,
        rejected=None,
        malformed_reason=_RELATIONSHIP_MALFORMED_RESPONSE_REASON,
    )


def parse_relationship_validation_response(
    *,
    content: str,
    sources: EnrichmentSources,
    mechanism: str,
    source: CardCapability,
    target: CardCapability,
    run_id: str,
) -> RelationshipValidationResult:
    """Parse one untrusted relationship validation response against frozen sources."""
    if not isinstance(sources, EnrichmentSources):
        raise SetEnrichmentExtractionError("sources must be an EnrichmentSources record.")
    normalized_mechanism = _identifier(mechanism, "mechanism")
    source, target = _validated_participants(source, target)
    source_card = _selected_card(sources, source.card_id)
    target_card = _selected_card(sources, target.card_id)
    _validated_participant_faces(source, target, source_card=source_card, target_card=target_card)
    normalized_run_id = _identifier(run_id, "run_id")
    finding_id = relationship_subject_id(
        mechanism=normalized_mechanism,
        source=source,
        target=target,
    )
    try:
        verdict, claim, reason, entries = _relationship_document(_decode_response_document(content))
    except (_MalformedResponse, SemanticEnrichmentError):
        return _malformed_relationship_result()
    try:
        evidence = _canonical_evidence(tuple(_decoded_evidence(entry) for entry in entries))
        status = FindingStatus(verdict)
        rejection_reason = _relationship_verdict_reason(
            status=status,
            reason=reason,
            evidence=evidence,
            source=source,
            target=target,
            source_card=source_card,
            target_card=target_card,
        )
        if rejection_reason is not None:
            return RelationshipValidationResult(
                outcome=ExtractionOutcome.SUCCESS,
                relationship=None,
                rejected=RejectedFinding(
                    finding_id=finding_id,
                    source_kind="relationship",
                    summary=claim,
                    reason=rejection_reason,
                    run_id=normalized_run_id,
                ),
                malformed_reason=None,
            )
        if status is FindingStatus.REJECTED:
            return RelationshipValidationResult(
                outcome=ExtractionOutcome.SUCCESS,
                relationship=None,
                rejected=RejectedFinding(
                    finding_id=finding_id,
                    source_kind="relationship",
                    summary=claim,
                    reason=reason,
                    run_id=normalized_run_id,
                ),
                malformed_reason=None,
            )
        return RelationshipValidationResult(
            outcome=ExtractionOutcome.SUCCESS,
            relationship=ValidatedRelationship(
                mechanism=normalized_mechanism,
                source=source,
                target=target,
                claim=claim,
                evidence=evidence,
                review=FindingReview(status=status, reason=reason),
                run_id=normalized_run_id,
            ),
            rejected=None,
            malformed_reason=None,
        )
    except (SemanticEnrichmentError, SetEnrichmentExtractionError):
        return _malformed_relationship_result()


__all__ = [
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
    "RELATIONSHIP_VALIDATION_PROMPT_ID",
    "RELATIONSHIP_VALIDATION_RESPONSE_SCHEMA_ID",
    "RELATIONSHIP_VALIDATION_SCHEMA_NAME",
    "RelationshipValidationResult",
    "SET_ENRICHMENT_EXTRACTION_CONTRACT_VERSION",
    "SetEnrichmentExtractionError",
    "ValidatedRelationship",
    "build_card_capability_extraction_request",
    "build_guide_extraction_request",
    "build_relationship_validation_request",
    "parse_card_capability_extraction_response",
    "parse_guide_extraction_response",
    "parse_relationship_validation_response",
    "relationship_source_sha256",
    "relationship_subject_id",
]
