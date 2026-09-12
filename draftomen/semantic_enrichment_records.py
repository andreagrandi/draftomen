"""Strict immutable records for semantic enrichment artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import json
import re
from typing import Any, Self, TypeVar


_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")


class SemanticEnrichmentError(ValueError):
    """Raised when semantic enrichment data violates its contract."""


class FindingStatus(StrEnum):
    """Status retained with a typed finding."""

    ACCEPTED = "accepted"
    UNCERTAIN = "uncertain"
    REJECTED = "rejected"


_ItemT = TypeVar("_ItemT")


def _keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    """Require exactly the expected object keys."""
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        details: list[str] = []
        if unknown:
            details.append("unknown fields: " + ", ".join(sorted(repr(item) for item in unknown)))
        if missing:
            details.append("missing fields: " + ", ".join(sorted(repr(item) for item in missing)))
        raise SemanticEnrichmentError(f"{field_name} has invalid keys ({'; '.join(details)}).")


def require_utf8_text(value: Any, field_name: str) -> str:
    """Reject text that cannot be encoded as UTF-8."""
    if not isinstance(value, str):
        raise SemanticEnrichmentError(f"{field_name} must be a string.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SemanticEnrichmentError(f"{field_name} must be UTF-8 encodable text.") from error
    return value


def _identifier(value: Any, field_name: str) -> str:
    """Validate and strip a nonblank identifier."""
    if not isinstance(value, str) or not value.strip():
        raise SemanticEnrichmentError(f"{field_name} must be a nonblank string.")
    return require_utf8_text(value, field_name).strip()


def _exact_text(value: Any, field_name: str) -> str:
    """Validate nonblank text without changing it."""
    if not isinstance(value, str) or not value.strip():
        raise SemanticEnrichmentError(f"{field_name} must be nonblank text.")
    return require_utf8_text(value, field_name)


def _integer(value: Any, field_name: str, *, positive: bool = False) -> int:
    """Validate an integer with an optional positive lower bound."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticEnrichmentError(f"{field_name} must be an integer.")
    lower_bound = 1 if positive else 0
    if value < lower_bound:
        bound = "positive" if positive else "non-negative"
        raise SemanticEnrichmentError(f"{field_name} must be a {bound} integer.")
    return value


def _optional_integer(value: Any, field_name: str, *, positive: bool = False) -> int | None:
    """Validate a nullable integer."""
    if value is None:
        return None
    return _integer(value, field_name, positive=positive)


def _optional_bool(value: Any, field_name: str) -> bool | None:
    """Validate a nullable strict boolean."""
    if value is not None and type(value) is not bool:
        raise SemanticEnrichmentError(f"{field_name} must be a boolean or null.")
    return value


def _hash(value: Any, field_name: str) -> str:
    """Validate and lowercase a SHA-256 digest."""
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise SemanticEnrichmentError(f"{field_name} must be a SHA-256 digest.")
    return value.lower()


def _timestamp(value: Any, field_name: str) -> tuple[str, datetime]:
    """Parse and normalize a timezone-bearing timestamp."""
    timestamp = _identifier(value, field_name)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise SemanticEnrichmentError(f"{field_name} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise SemanticEnrichmentError(f"{field_name} must include a timezone.")
    normalized = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return normalized, parsed


def _tuple(value: Any, field_name: str) -> tuple[Any, ...]:
    """Require a Python tuple instead of coercing an iterable."""
    if not isinstance(value, tuple):
        raise SemanticEnrichmentError(f"{field_name} must be a tuple.")
    return value


def _json_array(value: Any, field_name: str) -> tuple[Any, ...]:
    """Decode only JSON arrays as tuple-valued fields."""
    if not isinstance(value, list):
        raise SemanticEnrichmentError(f"{field_name} must be a JSON array.")
    return tuple(value)


def _canonical(
    values: tuple[_ItemT, ...],
    *,
    field_name: str,
    key: Callable[[_ItemT], object],
) -> tuple[_ItemT, ...]:
    """Reject duplicate identities and sort a tuple canonically."""
    identities = [key(item) for item in values]
    if len(set(identities)) != len(identities):
        raise SemanticEnrichmentError(f"{field_name} contains duplicate entries.")
    return tuple(sorted(values, key=key))


def _nested_json_array(
    value: Any,
    field_name: str,
    loader: Callable[[Mapping[str, Any]], _ItemT],
) -> tuple[_ItemT, ...]:
    """Decode a JSON array of nested records."""
    return tuple(loader(item) for item in _json_array(value, field_name))


def _enum_status(value: Any, field_name: str) -> FindingStatus:
    """Decode a finding status from its JSON string."""
    if not isinstance(value, str):
        raise SemanticEnrichmentError(f"{field_name} must be a finding status string.")
    try:
        return FindingStatus(value)
    except ValueError as error:
        raise SemanticEnrichmentError(f"{field_name} has an unsupported value.") from error


def _closed_string(value: Any, field_name: str, allowed: set[str]) -> str:
    """Validate a stripped string against a closed value set."""
    normalized = _identifier(value, field_name)
    if normalized not in allowed:
        raise SemanticEnrichmentError(f"{field_name} has an unsupported value.")
    return normalized


@dataclass(frozen=True, slots=True)
class FindingReview:
    """Structural review attached to a finding."""

    status: FindingStatus
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, FindingStatus):
            raise SemanticEnrichmentError("status must be a FindingStatus.")
        if self.reason is not None:
            reason = _exact_text(self.reason, "reason")
            object.__setattr__(self, "reason", reason)
        if self.status in (FindingStatus.UNCERTAIN, FindingStatus.REJECTED) and self.reason is None:
            raise SemanticEnrichmentError("uncertain and rejected findings require a reason.")

    def to_json(self) -> dict[str, object]:
        return {"status": self.status.value, "reason": self.reason}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("finding review must be an object.")
        _keys(value, {"status", "reason"}, "finding review")
        return cls(status=_enum_status(value["status"], "status"), reason=value["reason"])


@dataclass(frozen=True, slots=True)
class OracleEvidence:
    """Exact evidence selected from an Oracle card text."""

    card_id: int
    face_index: int | None
    quote: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_id", _integer(self.card_id, "card_id", positive=True))
        object.__setattr__(self, "face_index", _optional_integer(self.face_index, "face_index"))
        object.__setattr__(self, "quote", _exact_text(self.quote, "quote"))

    def to_json(self) -> dict[str, object]:
        return {"card_id": self.card_id, "face_index": self.face_index, "quote": self.quote}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("Oracle evidence must be an object.")
        _keys(value, {"card_id", "face_index", "quote"}, "Oracle evidence")
        return cls(card_id=value["card_id"], face_index=value["face_index"], quote=value["quote"])


@dataclass(frozen=True, slots=True)
class GuideEvidence:
    """Exact evidence selected from a guide source."""

    guide_id: str
    quote: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "guide_id", _identifier(self.guide_id, "guide_id"))
        object.__setattr__(self, "quote", _exact_text(self.quote, "quote"))

    def to_json(self) -> dict[str, object]:
        return {"guide_id": self.guide_id, "quote": self.quote}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("guide evidence must be an object.")
        _keys(value, {"guide_id", "quote"}, "guide evidence")
        return cls(guide_id=value["guide_id"], quote=value["quote"])


@dataclass(frozen=True, slots=True)
class OracleFact:
    """A card-scoped fact with exact Oracle evidence."""

    finding_id: str
    card_id: int
    kind: str
    claim: str
    evidence: tuple[OracleEvidence, ...]
    review: FindingReview
    run_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", _identifier(self.finding_id, "finding_id"))
        object.__setattr__(self, "card_id", _integer(self.card_id, "card_id", positive=True))
        object.__setattr__(self, "kind", _identifier(self.kind, "kind"))
        object.__setattr__(self, "claim", _exact_text(self.claim, "claim"))
        evidence = _tuple(self.evidence, "evidence")
        if not evidence:
            raise SemanticEnrichmentError("evidence must not be empty.")
        if any(not isinstance(item, OracleEvidence) for item in evidence):
            raise SemanticEnrichmentError("evidence must contain OracleEvidence records.")
        if any(item.card_id != self.card_id for item in evidence):
            raise SemanticEnrichmentError("Oracle fact evidence must belong to its card.")
        canonical = _canonical(
            evidence,
            field_name="evidence",
            key=lambda item: (item.card_id, -1 if item.face_index is None else item.face_index, item.quote),
        )
        object.__setattr__(self, "evidence", canonical)
        if not isinstance(self.review, FindingReview):
            raise SemanticEnrichmentError("review must be a FindingReview.")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))

    def to_json(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "card_id": self.card_id,
            "kind": self.kind,
            "claim": self.claim,
            "evidence": [item.to_json() for item in self.evidence],
            "review": self.review.to_json(),
            "run_id": self.run_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("Oracle fact must be an object.")
        _keys(value, {"finding_id", "card_id", "kind", "claim", "evidence", "review", "run_id"}, "Oracle fact")
        return cls(
            finding_id=value["finding_id"],
            card_id=value["card_id"],
            kind=value["kind"],
            claim=value["claim"],
            evidence=_nested_json_array(value["evidence"], "evidence", OracleEvidence.from_json),
            review=FindingReview.from_json(value["review"]),
            run_id=value["run_id"],
        )


@dataclass(frozen=True, slots=True)
class GuideClaim:
    """An advisory guide claim with exact guide evidence."""

    finding_id: str
    category: str
    name: str
    claim: str
    card_ids: tuple[int, ...]
    evidence: tuple[GuideEvidence, ...]
    review: FindingReview
    run_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "finding_id",
            _identifier(self.finding_id, "finding_id"),
        )
        object.__setattr__(
            self,
            "category",
            _closed_string(
                self.category,
                "category",
                {"format_finding", "mechanic", "archetype", "strategy"},
            ),
        )
        object.__setattr__(self, "name", _identifier(self.name, "name"))
        object.__setattr__(self, "claim", _exact_text(self.claim, "claim"))
        card_ids = _tuple(self.card_ids, "card_ids")
        if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in card_ids):
            raise SemanticEnrichmentError("card_ids must contain positive integers.")
        object.__setattr__(self, "card_ids", _canonical(card_ids, field_name="card_ids", key=lambda item: item))
        evidence = _tuple(self.evidence, "evidence")
        if not evidence:
            raise SemanticEnrichmentError("evidence must not be empty.")
        if any(not isinstance(item, GuideEvidence) for item in evidence):
            raise SemanticEnrichmentError("evidence must contain GuideEvidence records.")
        object.__setattr__(
            self,
            "evidence",
            _canonical(evidence, field_name="evidence", key=lambda item: (item.guide_id, item.quote)),
        )
        if not isinstance(self.review, FindingReview):
            raise SemanticEnrichmentError("review must be a FindingReview.")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))

    def to_json(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "name": self.name,
            "claim": self.claim,
            "card_ids": list(self.card_ids),
            "evidence": [item.to_json() for item in self.evidence],
            "review": self.review.to_json(),
            "run_id": self.run_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("guide claim must be an object.")
        _keys(
            value,
            {"finding_id", "category", "name", "claim", "card_ids", "evidence", "review", "run_id"},
            "guide claim",
        )
        return cls(
            finding_id=value["finding_id"],
            category=value["category"],
            name=value["name"],
            claim=value["claim"],
            card_ids=_json_array(value["card_ids"], "card_ids"),
            evidence=_nested_json_array(value["evidence"], "evidence", GuideEvidence.from_json),
            review=FindingReview.from_json(value["review"]),
            run_id=value["run_id"],
        )


@dataclass(frozen=True, slots=True)
class RejectedFinding:
    """Diagnostic for a finding rejected before typed storage."""

    finding_id: str
    source_kind: str
    summary: str
    reason: str
    run_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "finding_id", _identifier(self.finding_id, "finding_id"))
        object.__setattr__(
            self,
            "source_kind",
            _closed_string(self.source_kind, "source_kind", {"oracle", "guide", "relationship"}),
        )
        object.__setattr__(self, "summary", _exact_text(self.summary, "summary"))
        object.__setattr__(self, "reason", _exact_text(self.reason, "reason"))
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))

    def to_json(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "source_kind": self.source_kind,
            "summary": self.summary,
            "reason": self.reason,
            "run_id": self.run_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("rejected finding must be an object.")
        _keys(value, {"finding_id", "source_kind", "summary", "reason", "run_id"}, "rejected finding")
        return cls(
            finding_id=value["finding_id"],
            source_kind=value["source_kind"],
            summary=value["summary"],
            reason=value["reason"],
            run_id=value["run_id"],
        )


@dataclass(frozen=True, slots=True)
class CardSourcePin:
    """Provenance pin for a card source."""

    card_id: int
    oracle_id: str | None
    collector_number: str | None
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_id", _integer(self.card_id, "card_id", positive=True))
        if self.oracle_id is not None:
            object.__setattr__(self, "oracle_id", _identifier(self.oracle_id, "oracle_id"))
        if self.collector_number is not None:
            object.__setattr__(self, "collector_number", _identifier(self.collector_number, "collector_number"))
        object.__setattr__(self, "sha256", _hash(self.sha256, "sha256"))

    def to_json(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "oracle_id": self.oracle_id,
            "collector_number": self.collector_number,
            "sha256": self.sha256,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("card source pin must be an object.")
        _keys(value, {"card_id", "oracle_id", "collector_number", "sha256"}, "card source pin")
        return cls(
            card_id=value["card_id"],
            oracle_id=value["oracle_id"],
            collector_number=value["collector_number"],
            sha256=value["sha256"],
        )


@dataclass(frozen=True, slots=True)
class GuideSourcePin:
    """Provenance pin for a guide source."""

    guide_id: str
    url: str
    sha256: str
    retrieved_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "guide_id", _identifier(self.guide_id, "guide_id"))
        object.__setattr__(self, "url", _identifier(self.url, "url"))
        object.__setattr__(self, "sha256", _hash(self.sha256, "sha256"))
        normalized, _ = _timestamp(self.retrieved_at, "retrieved_at")
        object.__setattr__(self, "retrieved_at", normalized)

    def to_json(self) -> dict[str, object]:
        return {
            "guide_id": self.guide_id,
            "url": self.url,
            "sha256": self.sha256,
            "retrieved_at": self.retrieved_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("guide source pin must be an object.")
        _keys(value, {"guide_id", "url", "sha256", "retrieved_at"}, "guide source pin")
        return cls(
            guide_id=value["guide_id"],
            url=value["url"],
            sha256=value["sha256"],
            retrieved_at=value["retrieved_at"],
        )


@dataclass(frozen=True, slots=True)
class ReasoningConfig:
    """Provider reasoning configuration preserved with a model run."""

    enabled: bool | None
    effort: str | None
    max_tokens: int | None
    exclude: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _optional_bool(self.enabled, "enabled"))
        if self.effort is not None:
            object.__setattr__(self, "effort", _identifier(self.effort, "effort"))
        object.__setattr__(self, "max_tokens", _optional_integer(self.max_tokens, "max_tokens", positive=True))
        object.__setattr__(self, "exclude", _optional_bool(self.exclude, "exclude"))

    def to_json(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "exclude": self.exclude,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("reasoning config must be an object.")
        _keys(value, {"enabled", "effort", "max_tokens", "exclude"}, "reasoning config")
        return cls(
            enabled=value["enabled"],
            effort=value["effort"],
            max_tokens=value["max_tokens"],
            exclude=value["exclude"],
        )


def _cost(value: Any, field_name: str) -> str | None:
    """Validate and normalize an optional decimal cost string."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SemanticEnrichmentError(f"{field_name} must be a decimal string or null.")
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise SemanticEnrichmentError(f"{field_name} must be a decimal string or null.") from error
    if not decimal_value.is_finite() or decimal_value < 0:
        raise SemanticEnrichmentError(f"{field_name} must be a finite non-negative decimal.")
    if decimal_value == 0:
        return "0"
    text = format(decimal_value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


@dataclass(frozen=True, slots=True)
class ModelRun:
    """Request accounting and provenance for one model run."""

    run_id: str
    provider: str
    model: str
    reasoning: ReasoningConfig
    prompt_id: str
    prompt_sha256: str
    response_schema_id: str
    response_schema_sha256: str
    started_at: str
    completed_at: str
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: str | None

    def __post_init__(self) -> None:
        for field_name in ("run_id", "provider", "model", "prompt_id", "response_schema_id"):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        if not isinstance(self.reasoning, ReasoningConfig):
            raise SemanticEnrichmentError("reasoning must be a ReasoningConfig.")
        object.__setattr__(self, "prompt_sha256", _hash(self.prompt_sha256, "prompt_sha256"))
        object.__setattr__(self, "response_schema_sha256", _hash(self.response_schema_sha256, "response_schema_sha256"))
        started, started_parsed = _timestamp(self.started_at, "started_at")
        completed, completed_parsed = _timestamp(self.completed_at, "completed_at")
        if completed_parsed < started_parsed:
            raise SemanticEnrichmentError("completed_at must not precede started_at.")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "completed_at", completed)
        for field_name in ("input_tokens", "output_tokens", "reasoning_tokens"):
            object.__setattr__(self, field_name, _optional_integer(getattr(self, field_name), field_name))
        object.__setattr__(self, "cost_usd", _cost(self.cost_usd, "cost_usd"))

    def to_json(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "reasoning": self.reasoning.to_json(),
            "prompt_id": self.prompt_id,
            "prompt_sha256": self.prompt_sha256,
            "response_schema_id": self.response_schema_id,
            "response_schema_sha256": self.response_schema_sha256,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("model run must be an object.")
        _keys(
            value,
            {
                "run_id",
                "provider",
                "model",
                "reasoning",
                "prompt_id",
                "prompt_sha256",
                "response_schema_id",
                "response_schema_sha256",
                "started_at",
                "completed_at",
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cost_usd",
            },
            "model run",
        )
        return cls(
            run_id=value["run_id"],
            provider=value["provider"],
            model=value["model"],
            reasoning=ReasoningConfig.from_json(value["reasoning"]),
            prompt_id=value["prompt_id"],
            prompt_sha256=value["prompt_sha256"],
            response_schema_id=value["response_schema_id"],
            response_schema_sha256=value["response_schema_sha256"],
            started_at=value["started_at"],
            completed_at=value["completed_at"],
            input_tokens=value["input_tokens"],
            output_tokens=value["output_tokens"],
            reasoning_tokens=value["reasoning_tokens"],
            cost_usd=value["cost_usd"],
        )


@dataclass(frozen=True, slots=True)
class ArtifactReview:
    """Human review state for an enrichment artifact."""

    state: str
    reviewer_id: str | None
    reviewed_at: str | None

    def __post_init__(self) -> None:
        state = _closed_string(self.state, "state", {"pending", "confirmed", "cancelled"})
        object.__setattr__(self, "state", state)
        if state == "pending":
            if self.reviewer_id is not None or self.reviewed_at is not None:
                raise SemanticEnrichmentError("pending review requires null reviewer metadata.")
            return
        if self.reviewer_id is None:
            raise SemanticEnrichmentError("completed review requires a reviewer_id.")
        object.__setattr__(self, "reviewer_id", _identifier(self.reviewer_id, "reviewer_id"))
        if self.reviewed_at is None:
            raise SemanticEnrichmentError("completed review requires reviewed_at.")
        normalized, _ = _timestamp(self.reviewed_at, "reviewed_at")
        object.__setattr__(self, "reviewed_at", normalized)

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reviewer_id": self.reviewer_id,
            "reviewed_at": self.reviewed_at,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise SemanticEnrichmentError("artifact review must be an object.")
        _keys(value, {"state", "reviewer_id", "reviewed_at"}, "artifact review")
        return cls(
            state=value["state"],
            reviewer_id=value["reviewer_id"],
            reviewed_at=value["reviewed_at"],
        )


__all__ = [
    "ArtifactReview",
    "CardSourcePin",
    "FindingReview",
    "FindingStatus",
    "GuideClaim",
    "GuideEvidence",
    "GuideSourcePin",
    "ModelRun",
    "OracleEvidence",
    "OracleFact",
    "ReasoningConfig",
    "RejectedFinding",
    "SemanticEnrichmentError",
    "require_utf8_text",
]


