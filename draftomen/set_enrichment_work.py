"""Durable content-addressed store for resumable set-enrichment requests.
The caller owns the work directory, and this store never performs network access or mutates
anything outside that directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias

from draftomen.openrouter_client import REASONING_EFFORTS, OpenRouterResponse
from draftomen.set_enrichment_extraction import (
    CardCapabilityExtractionResult,
    ExtractionRequest,
    GuideExtractionResult,
    RelationshipValidationResult,
    SetEnrichmentExtractionError,
)


SET_ENRICHMENT_WORK_SCHEMA_VERSION = 1
WORK_ATTEMPT_DIRECTORY = "attempts"
WORK_RESPONSE_DIRECTORY = "responses"
WORK_RESULT_DIRECTORY = "results"

PathInput: TypeAlias = str | os.PathLike[str]
Clock: TypeAlias = Callable[[], datetime]

_ATTEMPT_STAGE = "attempt"
_RESPONSE_STAGE = "response"
_RESULT_STAGE = "result"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_COST_PATTERN = re.compile(r"\d+(\.\d+)?\Z")
_MODEL_CONFIG_KEYS = frozenset({"model", "reasoning_effort", "max_tokens"})
_IDENTITY_KEYS = frozenset(
    {
        "work_kind",
        "subject_id",
        "contract_version",
        "prompt_id",
        "response_schema_id",
        "response_schema_name",
        "input_sha256",
        "prompt_sha256",
        "response_schema_sha256",
        "model_config",
    }
)
_RESPONSE_FIELDS = (
    "content",
    "model",
    "provider",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cost_usd",
)
_STAGE_KEYS: Mapping[str, frozenset[str]] = {
    _ATTEMPT_STAGE: frozenset({"schema_version", "identity", "attempted_at"}),
    _RESPONSE_STAGE: frozenset({"schema_version", "identity", "response", "responded_at"}),
    _RESULT_STAGE: frozenset({"schema_version", "identity", "result", "completed_at"}),
}
_STAGE_TIMESTAMP_FIELDS: Mapping[str, str] = {
    _ATTEMPT_STAGE: "attempted_at",
    _RESPONSE_STAGE: "responded_at",
    _RESULT_STAGE: "completed_at",
}


class SetEnrichmentWorkError(RuntimeError):
    """Raised when set-enrichment work inputs or stored artifacts violate their contract."""


class SetEnrichmentWorkConflictError(SetEnrichmentWorkError):
    """Raised when durable set-enrichment work already holds different content."""


class _InvalidArtifact(SetEnrichmentWorkError):
    """Report one stored artifact that fails its durable invariants."""


class WorkKind(StrEnum):
    """Logical kind of one durable set-enrichment request."""

    GUIDE = "guide"
    CARD_CAPABILITY = "card-capability"
    RELATIONSHIP = "relationship"


class WorkState(StrEnum):
    """Derived durability state of one work identity."""

    MISSING = "missing"
    INCOMPLETE = "incomplete"
    UNVALIDATED = "unvalidated"
    COMPLETED = "completed"
    CORRUPT = "corrupt"


def _text(value: Any, field_name: str) -> str:
    """Validate and strip one nonblank UTF-8-encodable string."""
    if not isinstance(value, str) or not value.strip():
        raise SetEnrichmentWorkError(f"{field_name} must be a nonblank string.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SetEnrichmentWorkError(f"{field_name} must be UTF-8 encodable.") from error
    return value.strip()


def _sha256(value: Any, field_name: str) -> str:
    """Validate and casefold one SHA-256 digest."""
    if not isinstance(value, str):
        raise SetEnrichmentWorkError(f"{field_name} must be a SHA-256 digest.")
    normalized = value.casefold()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise SetEnrichmentWorkError(f"{field_name} must be a SHA-256 digest.")
    return normalized


def _positive_integer(value: Any, field_name: str) -> int:
    """Validate one positive integer that is not a boolean."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SetEnrichmentWorkError(f"{field_name} must be a positive integer.")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    """Encode one value as canonical compact JSON bytes."""
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return (serialized + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise SetEnrichmentWorkError("Could not canonicalize set-enrichment work data.") from error


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Rebuild one stored JSON object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidArtifact
        result[key] = value
    return result


def _json_constant(value: str) -> Any:
    """Reject the NaN and Infinity JSON constants."""
    raise _InvalidArtifact


def _decode_json(raw: bytes) -> Any:
    """Decode strict JSON without duplicate keys or non-finite numbers."""
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise _InvalidArtifact from error


def _stored_text(value: Any) -> str:
    """Validate one nonblank UTF-8-encodable stored string."""
    if not isinstance(value, str) or not value.strip():
        raise _InvalidArtifact
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _InvalidArtifact from error
    return value


def _stored_count(value: Any) -> int | None:
    """Validate one optional stored non-negative token count."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _InvalidArtifact
    return value


def _stored_cost(value: Any) -> str | None:
    """Validate one optional stored normalized decimal cost."""
    if value is None:
        return None
    if not isinstance(value, str) or _COST_PATTERN.fullmatch(value) is None:
        raise _InvalidArtifact
    return value


def _stored_timestamp(value: Any) -> datetime:
    """Decode one stored timezone-aware timestamp."""
    if not isinstance(value, str):
        raise _InvalidArtifact
    try:
        decoded = datetime.fromisoformat(value)
    except ValueError as error:
        raise _InvalidArtifact from error
    if decoded.tzinfo is None or decoded.utcoffset() is None:
        raise _InvalidArtifact
    return decoded


def _stored_response(value: Any) -> OpenRouterResponse:
    """Decode one stored trusted response record."""
    if not isinstance(value, Mapping) or set(value) != set(_RESPONSE_FIELDS):
        raise _InvalidArtifact
    content = value["content"]
    if not isinstance(content, str):
        raise _InvalidArtifact
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _InvalidArtifact from error
    provider = value["provider"]
    return OpenRouterResponse(
        content=content,
        model=_stored_text(value["model"]),
        provider=None if provider is None else _stored_text(provider),
        input_tokens=_stored_count(value["input_tokens"]),
        cached_input_tokens=_stored_count(value["cached_input_tokens"]),
        output_tokens=_stored_count(value["output_tokens"]),
        reasoning_tokens=_stored_count(value["reasoning_tokens"]),
        cost_usd=_stored_cost(value["cost_usd"]),
    )


def _stored_result(
    value: Any,
    work_kind: WorkKind,
) -> GuideExtractionResult | CardCapabilityExtractionResult | RelationshipValidationResult:
    """Decode one stored validated extraction result for its work kind."""
    if not isinstance(value, Mapping):
        raise _InvalidArtifact
    try:
        if work_kind is WorkKind.GUIDE:
            return GuideExtractionResult.from_json(value)
        if work_kind is WorkKind.CARD_CAPABILITY:
            return CardCapabilityExtractionResult.from_json(value)
        if work_kind is WorkKind.RELATIONSHIP:
            return RelationshipValidationResult.from_json(value)
        raise _InvalidArtifact
    except SetEnrichmentExtractionError as error:
        raise _InvalidArtifact from error


def _response_payload(value: Any) -> dict[str, Any]:
    """Validate one trusted response record into its stored payload."""
    if not isinstance(value, OpenRouterResponse):
        raise SetEnrichmentWorkError("response must be an OpenRouterResponse.")
    payload = {name: getattr(value, name) for name in _RESPONSE_FIELDS}
    try:
        _stored_response(payload)
    except _InvalidArtifact as error:
        raise SetEnrichmentWorkError("response fields are not a storable record.") from error
    return payload


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkModelConfig:
    """Exact acquisition model configuration bound into one work identity."""

    model: str
    reasoning_effort: str
    max_tokens: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model, str)
            or not self.model
            or not all(33 <= ord(character) <= 126 for character in self.model)
        ):
            raise SetEnrichmentWorkError("model must be a printable ASCII string.")
        if (
            not isinstance(self.reasoning_effort, str)
            or self.reasoning_effort not in REASONING_EFFORTS
        ):
            raise SetEnrichmentWorkError("reasoning_effort must be a supported reasoning effort.")
        object.__setattr__(self, "max_tokens", _positive_integer(self.max_tokens, "max_tokens"))

    def to_json(self) -> dict[str, object]:
        """Return fresh JSON-compatible stored bytes for this model configuration."""
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> WorkModelConfig:
        """Decode validated stored bytes into one exact model configuration."""
        if not isinstance(value, Mapping):
            raise SetEnrichmentWorkError("model_config must be an object.")
        if set(value) != _MODEL_CONFIG_KEYS:
            raise SetEnrichmentWorkError("model_config has invalid keys.")
        return cls(
            model=value["model"],
            reasoning_effort=value["reasoning_effort"],
            max_tokens=value["max_tokens"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkIdentity:
    """Content-addressed identity of one durable set-enrichment request."""

    work_kind: WorkKind
    subject_id: str
    contract_version: int
    prompt_id: str
    response_schema_id: str
    response_schema_name: str
    input_sha256: str
    prompt_sha256: str
    response_schema_sha256: str
    model_config: WorkModelConfig

    def __post_init__(self) -> None:
        if not isinstance(self.work_kind, WorkKind):
            raise SetEnrichmentWorkError("work_kind must be a WorkKind.")
        for field_name in ("subject_id", "prompt_id", "response_schema_id", "response_schema_name"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        object.__setattr__(
            self,
            "contract_version",
            _positive_integer(self.contract_version, "contract_version"),
        )
        for field_name in ("input_sha256", "prompt_sha256", "response_schema_sha256"):
            object.__setattr__(self, field_name, _sha256(getattr(self, field_name), field_name))
        if not isinstance(self.model_config, WorkModelConfig):
            raise SetEnrichmentWorkError("model_config must be a WorkModelConfig.")

    @property
    def content_sha256(self) -> str:
        """Return the content address of this identity."""
        return hashlib.sha256(_canonical_json_bytes(self.to_json())).hexdigest()

    def to_json(self) -> dict[str, object]:
        """Return fresh JSON-compatible stored bytes for this identity."""
        return {
            "work_kind": self.work_kind.value,
            "subject_id": self.subject_id,
            "contract_version": self.contract_version,
            "prompt_id": self.prompt_id,
            "response_schema_id": self.response_schema_id,
            "response_schema_name": self.response_schema_name,
            "input_sha256": self.input_sha256,
            "prompt_sha256": self.prompt_sha256,
            "response_schema_sha256": self.response_schema_sha256,
            "model_config": self.model_config.to_json(),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> WorkIdentity:
        """Decode validated stored bytes into one exact work identity."""
        if not isinstance(value, Mapping):
            raise SetEnrichmentWorkError("identity must be an object.")
        if set(value) != _IDENTITY_KEYS:
            raise SetEnrichmentWorkError("identity has invalid keys.")
        work_kind = value["work_kind"]
        if not isinstance(work_kind, str):
            raise SetEnrichmentWorkError("work_kind must be a WorkKind.")
        try:
            decoded_kind = WorkKind(work_kind)
        except ValueError as error:
            raise SetEnrichmentWorkError("work_kind must be a WorkKind.") from error
        return cls(
            work_kind=decoded_kind,
            subject_id=value["subject_id"],
            contract_version=value["contract_version"],
            prompt_id=value["prompt_id"],
            response_schema_id=value["response_schema_id"],
            response_schema_name=value["response_schema_name"],
            input_sha256=value["input_sha256"],
            prompt_sha256=value["prompt_sha256"],
            response_schema_sha256=value["response_schema_sha256"],
            model_config=WorkModelConfig.from_json(value["model_config"]),
        )


@dataclass(frozen=True, slots=True)
class WorkRecord:
    """Derived durable work state for one identity.

    A completed record means validation finished and its artifacts are durable: a stored malformed
    outcome is completed work, and callers must read `result.outcome` instead of re-requesting it.
    """

    identity: WorkIdentity
    state: WorkState
    attempted_at: datetime | None
    responded_at: datetime | None
    completed_at: datetime | None
    response: OpenRouterResponse | None
    result: GuideExtractionResult | CardCapabilityExtractionResult | RelationshipValidationResult | None
    diagnostics: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, WorkState):
            raise SetEnrichmentWorkError("state must be a WorkState.")
        if (self.state is WorkState.COMPLETED) is not (self.result is not None):
            raise SetEnrichmentWorkError("only completed work may carry its validated result.")
        if (self.state is WorkState.CORRUPT) is not bool(self.diagnostics):
            raise SetEnrichmentWorkError("corrupt work must state its diagnostics.")
        object.__setattr__(self, "diagnostics", tuple(dict.fromkeys(self.diagnostics)))


@dataclass(frozen=True, slots=True)
class _Stage:
    """Report one stage artifact exactly as verified."""

    present: bool
    payload: Mapping[str, Any] | None


def _verified_payload(
    raw: bytes,
    *,
    identity: WorkIdentity,
    stage: str,
) -> Mapping[str, Any] | None:
    """Return one decoded artifact payload only when every durable invariant holds."""
    try:
        parsed = _decode_json(raw)
        if not isinstance(parsed, dict) or set(parsed) != _STAGE_KEYS[stage]:
            raise _InvalidArtifact
        version = parsed["schema_version"]
        if isinstance(version, bool) or version != SET_ENRICHMENT_WORK_SCHEMA_VERSION:
            raise _InvalidArtifact
        if WorkIdentity.from_json(parsed["identity"]) != identity:
            raise _InvalidArtifact
        if stage == _ATTEMPT_STAGE:
            _stored_timestamp(parsed["attempted_at"])
        elif stage == _RESPONSE_STAGE:
            _stored_response(parsed["response"])
            _stored_timestamp(parsed["responded_at"])
        else:
            _stored_result(parsed["result"], identity.work_kind)
            _stored_timestamp(parsed["completed_at"])
        if raw != _canonical_json_bytes(parsed):
            raise _InvalidArtifact
    except (_InvalidArtifact, SetEnrichmentWorkError):
        return None
    return parsed


def _read_stage(path: Path, *, identity: WorkIdentity, stage: str) -> _Stage:
    """Read one stage artifact, returning a payload only when it is fully usable."""
    try:
        if path.is_symlink():
            return _Stage(present=True, payload=None)
        if not path.exists():
            return _Stage(present=False, payload=None)
        if not path.is_file():
            return _Stage(present=True, payload=None)
        raw = path.read_bytes()
    except OSError as error:
        raise SetEnrichmentWorkError("Could not read set-enrichment work artifact.") from error
    return _Stage(present=True, payload=_verified_payload(raw, identity=identity, stage=stage))


def _same_stage_content(
    *,
    identity: WorkIdentity,
    stage: str,
    existing: Mapping[str, Any],
    new: Mapping[str, Any],
) -> bool:
    """Compare the durable stage content of one artifact with newly recorded content."""
    if stage == _ATTEMPT_STAGE:
        return True
    if stage == _RESPONSE_STAGE:
        return _stored_response(existing["response"]) == _stored_response(new["response"])
    return _stored_result(existing["result"], identity.work_kind) == _stored_result(
        new["result"], identity.work_kind
    )


def _payload_timestamp(payload: Mapping[str, Any] | None, field_name: str) -> datetime | None:
    """Return one verified artifact timestamp when that artifact is usable."""
    return None if payload is None else _stored_timestamp(payload[field_name])


def _payload_response(payload: Mapping[str, Any] | None) -> OpenRouterResponse | None:
    """Return one verified durable response when that artifact is usable."""
    return None if payload is None else _stored_response(payload["response"])


def _payload_result(
    payload: Mapping[str, Any] | None,
    work_kind: WorkKind,
) -> GuideExtractionResult | CardCapabilityExtractionResult | RelationshipValidationResult | None:
    """Return one verified durable result when that artifact is usable."""
    return None if payload is None else _stored_result(payload["result"], work_kind)


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    """Publish one artifact atomically inside its stage directory."""
    try:
        if destination.parent.is_symlink():
            raise SetEnrichmentWorkError("Set-enrichment work directory cannot be a symbolic link.")
        if destination.parent.exists() and not destination.parent.is_dir():
            raise SetEnrichmentWorkError("Set-enrichment work directory is not a directory.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.is_symlink():
            raise SetEnrichmentWorkError("Set-enrichment work directory cannot be a symbolic link.")
    except OSError as error:
        raise SetEnrichmentWorkError("Could not create set-enrichment work directory.") from error
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination.name}.", dir=destination.parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        _fsync_directory(destination.parent)
    except OSError as error:
        raise SetEnrichmentWorkError("Could not publish set-enrichment work artifact.") from error
    finally:
        _unlink(Path(temporary_name) if temporary_name is not None else None)


def _fsync_directory(path: Path) -> None:
    """Flush one directory entry change best-effort."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _unlink(path: Path | None) -> None:
    """Remove one temporary path best-effort."""
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


class SetEnrichmentWorkStore:
    """Store durable resumable set-enrichment work in one caller-owned root."""

    def __init__(self, root: PathInput, *, clock: Clock | None = None) -> None:
        if isinstance(root, str):
            if not root.strip():
                raise SetEnrichmentWorkError("root must be a nonblank path.")
        elif not isinstance(root, os.PathLike):
            raise SetEnrichmentWorkError("root must be a path.")
        try:
            self.root = Path(root)
        except (TypeError, ValueError) as error:
            raise SetEnrichmentWorkError("root must be a path.") from error
        if clock is None:
            clock = _utc_now
        if not callable(clock):
            raise SetEnrichmentWorkError("clock must be callable.")
        self.clock: Clock = clock
        self.attempts = self.root / WORK_ATTEMPT_DIRECTORY
        self.responses = self.root / WORK_RESPONSE_DIRECTORY
        self.results = self.root / WORK_RESULT_DIRECTORY

    def record_attempt(self, *, identity: WorkIdentity) -> WorkRecord:
        """Record one durable attempt marker for the identity."""
        self._require_identity(identity)
        payload = {
            "schema_version": SET_ENRICHMENT_WORK_SCHEMA_VERSION,
            "identity": identity.to_json(),
            "attempted_at": self._now(),
        }
        return self._publish(identity=identity, stage=_ATTEMPT_STAGE, payload=payload)

    def record_response(
        self,
        *,
        identity: WorkIdentity,
        response: OpenRouterResponse,
    ) -> WorkRecord:
        """Record one durable trusted response for the identity."""
        self._require_identity(identity)
        payload = {
            "schema_version": SET_ENRICHMENT_WORK_SCHEMA_VERSION,
            "identity": identity.to_json(),
            "response": _response_payload(response),
            "responded_at": self._now(),
        }
        return self._publish(identity=identity, stage=_RESPONSE_STAGE, payload=payload)

    def record_result(
        self,
        *,
        identity: WorkIdentity,
        result: (
            GuideExtractionResult | CardCapabilityExtractionResult | RelationshipValidationResult
        ),
    ) -> WorkRecord:
        """Record one durable validated result for the identity."""
        self._require_identity(identity)
        expected = {
            WorkKind.GUIDE: GuideExtractionResult,
            WorkKind.CARD_CAPABILITY: CardCapabilityExtractionResult,
            WorkKind.RELATIONSHIP: RelationshipValidationResult,
        }[identity.work_kind]
        if not isinstance(result, expected):
            raise SetEnrichmentWorkError(
                f"result must be a {expected.__name__} for this work kind."
            )
        self._require_durable_response(identity=identity)
        payload = {
            "schema_version": SET_ENRICHMENT_WORK_SCHEMA_VERSION,
            "identity": identity.to_json(),
            "result": result.to_json(),
            "completed_at": self._now(),
        }
        return self._publish(identity=identity, stage=_RESULT_STAGE, payload=payload)

    def lookup(self, *, identity: WorkIdentity) -> WorkRecord:
        """Return the durable work record for one identity without writing anything."""
        self._require_identity(identity)
        self._ensure_owned_directories()
        key = identity.content_sha256
        attempt = _read_stage(
            self.attempts / f"{key}.json",
            identity=identity,
            stage=_ATTEMPT_STAGE,
        )
        response = _read_stage(
            self.responses / f"{key}.json",
            identity=identity,
            stage=_RESPONSE_STAGE,
        )
        result = _read_stage(
            self.results / f"{key}.json",
            identity=identity,
            stage=_RESULT_STAGE,
        )
        diagnostics: list[str] = []
        if attempt.present and attempt.payload is None:
            diagnostics.append("attempt-invalid")
        if response.present and response.payload is None:
            diagnostics.append("response-invalid")
        if result.present and result.payload is None:
            diagnostics.append("result-invalid")
        if result.payload is not None and response.payload is None:
            diagnostics.append("result-without-response")
        if diagnostics:
            state = WorkState.CORRUPT
        elif result.payload is not None:
            state = WorkState.COMPLETED
        elif response.payload is not None:
            state = WorkState.UNVALIDATED
        elif attempt.payload is not None:
            state = WorkState.INCOMPLETE
        else:
            state = WorkState.MISSING
        return WorkRecord(
            identity=identity,
            state=state,
            attempted_at=_payload_timestamp(
                attempt.payload,
                _STAGE_TIMESTAMP_FIELDS[_ATTEMPT_STAGE],
            ),
            responded_at=_payload_timestamp(
                response.payload,
                _STAGE_TIMESTAMP_FIELDS[_RESPONSE_STAGE],
            ),
            completed_at=_payload_timestamp(
                result.payload,
                _STAGE_TIMESTAMP_FIELDS[_RESULT_STAGE],
            ),
            response=_payload_response(response.payload),
            result=(
                _payload_result(result.payload, identity.work_kind)
                if state is WorkState.COMPLETED
                else None
            ),
            diagnostics=tuple(diagnostics),
        )

    def _publish(
        self,
        *,
        identity: WorkIdentity,
        stage: str,
        payload: Mapping[str, Any],
    ) -> WorkRecord:
        """Publish one stage artifact atomically unless identical content is durable."""
        self._ensure_owned_directories()
        path = self._stage_directory(stage) / f"{identity.content_sha256}.json"
        existing = _read_stage(path, identity=identity, stage=stage)
        if existing.present:
            if existing.payload is None or not _same_stage_content(
                identity=identity,
                stage=stage,
                existing=existing.payload,
                new=payload,
            ):
                raise SetEnrichmentWorkConflictError(
                    "set-enrichment work artifact is already recorded with different content."
                )
            return self.lookup(identity=identity)
        self._ensure_owned_directories(create=True)
        _atomic_write_bytes(path, _canonical_json_bytes(payload))
        return self.lookup(identity=identity)

    def _require_durable_response(self, *, identity: WorkIdentity) -> None:
        """Require one usable durable response artifact before recording a result."""
        self._ensure_owned_directories()
        stage = _read_stage(
            self.responses / f"{identity.content_sha256}.json",
            identity=identity,
            stage=_RESPONSE_STAGE,
        )
        if stage.payload is None:
            raise SetEnrichmentWorkError("a durable response is required before its result.")

    def _ensure_owned_directories(self, *, create: bool = False) -> None:
        """Reject internal directory links before following or mutating them."""
        if create:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise SetEnrichmentWorkError("Could not create set-enrichment work root.") from error
        for path in (self.attempts, self.responses, self.results):
            self._check_owned_directory(path)
            if create and not path.exists():
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    raise SetEnrichmentWorkError(
                        "Could not create set-enrichment work directory."
                    ) from error
            self._check_owned_directory(path)

    @staticmethod
    def _check_owned_directory(path: Path) -> None:
        """Reject one internal stage directory that is linked or not a directory."""
        try:
            if path.is_symlink():
                raise SetEnrichmentWorkError(
                    "Set-enrichment work directory cannot be a symbolic link."
                )
            if path.exists() and not path.is_dir():
                raise SetEnrichmentWorkError("Set-enrichment work directory is not a directory.")
        except OSError as error:
            raise SetEnrichmentWorkError("Could not inspect set-enrichment work directory.") from error

    def _stage_directory(self, stage: str) -> Path:
        """Return the owned directory of one work stage."""
        if stage == _ATTEMPT_STAGE:
            return self.attempts
        if stage == _RESPONSE_STAGE:
            return self.responses
        return self.results

    def _now(self) -> str:
        """Return the current stored timestamp from the configured clock."""
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise SetEnrichmentWorkError("clock must return a timezone-aware datetime.")
        return now.astimezone(UTC).isoformat()

    @staticmethod
    def _require_identity(identity: Any) -> None:
        """Require one exact work identity instance."""
        if not isinstance(identity, WorkIdentity):
            raise SetEnrichmentWorkError("identity must be a WorkIdentity.")


def build_work_identity(
    *,
    work_kind: WorkKind,
    subject_id: str,
    input_sha256: str,
    request: ExtractionRequest,
    model_config: WorkModelConfig,
) -> WorkIdentity:
    """Build one work identity that cannot drift from its pinned request.
    Callers pass card_source_sha256(card) for card work and GuideSource.text_sha256 for guide work.
    """
    if not isinstance(request, ExtractionRequest):
        raise SetEnrichmentWorkError("request must be an ExtractionRequest.")
    return WorkIdentity(
        work_kind=work_kind,
        subject_id=subject_id,
        contract_version=request.contract_version,
        prompt_id=request.prompt_id,
        response_schema_id=request.response_schema_id,
        response_schema_name=request.response_schema_name,
        input_sha256=input_sha256,
        prompt_sha256=request.prompt_sha256,
        response_schema_sha256=request.response_schema_sha256,
        model_config=model_config,
    )


__all__ = [
    "SET_ENRICHMENT_WORK_SCHEMA_VERSION",
    "WORK_ATTEMPT_DIRECTORY",
    "WORK_RESPONSE_DIRECTORY",
    "WORK_RESULT_DIRECTORY",
    "SetEnrichmentWorkConflictError",
    "SetEnrichmentWorkError",
    "SetEnrichmentWorkStore",
    "WorkIdentity",
    "WorkKind",
    "WorkModelConfig",
    "WorkRecord",
    "WorkState",
    "build_work_identity",
]
