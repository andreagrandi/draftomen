"""Canonical per-set augmentation artifact for framework-free runtime scoring."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import gzip
import hashlib
import io
import json
import math
import re
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from draftomen.augmented_training_data import FEATURE_NAMES, card_feature_membership
from draftomen.carddb import CardInfo

AUGMENTED_ARTIFACT_SCHEMA_VERSION = 1
AUGMENTED_ARTIFACT_COMPATIBILITY = "coarse-context-model-c/v1"
AUGMENTED_ARTIFACT_MODEL_NAME = "coarse_context_model_c"
AUGMENTED_ARTIFACT_ARCHITECTURE = "bias_plus_tanh_low_rank_pool_context"
AUGMENTED_ARTIFACT_INPUT_TRANSFORM = "log1p_counts"
AUGMENTED_ARTIFACT_HIDDEN_ACTIVATION = "tanh"
AUGMENTED_ARTIFACT_CALIBRATION_METHOD = "offered_mean_centered_logit_clip"
AUGMENTED_MAXIMUM_DELTA = 8.0
AUGMENTED_MAXIMUM_MULTIPLIER = 16.0
AUGMENTED_ARTIFACT_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
_SET_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "compatibility",
        "set_code",
        "model",
        "calibration",
        "source",
        "evaluation",
    }
)
_OPTIONAL_TOP_KEYS = frozenset({"training"})
_MODEL_KEYS = frozenset(
    {
        "name",
        "architecture",
        "feature_schema",
        "candidate_output_ids",
        "runtime_parameters",
    }
)
_RUNTIME_PARAMETER_KEYS = frozenset(
    {
        "input_transform",
        "hidden_activation",
        "bias",
        "input_weights",
        "output_weights",
    }
)
_SOURCE_KEYS = frozenset(
    {
        "attribution",
        "event_type",
        "license",
        "retrieved_at",
        "sha256",
        "url",
    }
)
_EVALUATION_KEYS = frozenset({"basic_do", "basic_plus_augmented"})
_METRIC_OBJECT_KEYS = frozenset({"picks", "top_1", "mean_reciprocal_rank"})
_METRIC_SET_KEYS = frozenset({"top_1", "mean_reciprocal_rank"})
_METRIC_SUMMARY_KEYS = frozenset({"picks", "basic_do", "basic_plus_augmented"})
_CALIBRATION_REQUIRED_KEYS = frozenset(
    {"method", "multiplier", "minimum_delta", "maximum_delta"}
)
_CALIBRATION_KEYS = frozenset(
    {
        "method",
        "multiplier",
        "minimum_delta",
        "maximum_delta",
        "selection_partition",
        "selection_metric",
        "candidates",
    }
)
_CANDIDATE_KEYS = frozenset({"multiplier", "top_1", "mean_reciprocal_rank"})


class AugmentedArtifactError(ValueError):
    """Report an artifact that cannot be trusted for runtime scoring."""


class AugmentedArtifactIncompatibleError(AugmentedArtifactError):
    """Report an artifact this Draft Omen build cannot interpret."""


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        raise AugmentedArtifactError(f"{label} keys must be strings.")
    if keys != expected:
        missing = sorted(expected - keys)
        unknown = sorted(keys - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise AugmentedArtifactError(f"{label} has invalid keys ({'; '.join(details)}).")


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AugmentedArtifactError(f"{field_name} must be a non-empty string.")
    return value


def _stripped_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AugmentedArtifactError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AugmentedArtifactError(f"{field_name} must be a number.")
    try:
        result = float(value)
    except OverflowError as error:
        raise AugmentedArtifactError(f"{field_name} must be finite.") from error
    if not math.isfinite(result):
        raise AugmentedArtifactError(f"{field_name} must be finite.")
    return result


def _positive_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AugmentedArtifactError(f"{field_name} must be a positive integer.")
    return value


def _metric_value(value: Any, field_name: str) -> float:
    result = _finite_number(value, field_name)
    if not 0.0 <= result <= 1.0:
        raise AugmentedArtifactError(f"{field_name} must be within [0.0, 1.0].")
    return result


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return (serialized + "\n").encode("utf-8")
    except (TypeError, UnicodeError, ValueError, OverflowError) as error:
        raise AugmentedArtifactError(
            "Augmented artifact could not be serialized canonically."
        ) from error


def _duplicate_checking_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AugmentedArtifactError(f"Duplicate JSON object key {key!r}.")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise AugmentedArtifactError(f"JSON constant {value!r} is not allowed.")


def _decode_gzip(payload: bytes, *, max_decompressed_bytes: int) -> bytes:
    if isinstance(max_decompressed_bytes, bool) or not isinstance(
        max_decompressed_bytes, int
    ):
        raise AugmentedArtifactError(
            "max_decompressed_bytes must be a positive integer."
        )
    if max_decompressed_bytes <= 0:
        raise AugmentedArtifactError(
            "max_decompressed_bytes must be a positive integer."
        )
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        raw = decompressor.decompress(payload, max_decompressed_bytes + 1)
        if len(raw) > max_decompressed_bytes:
            raise AugmentedArtifactError(
                "Augmented artifact gzip payload exceeds decompressed size limit."
            )
        if not decompressor.eof:
            raw += decompressor.flush(max_decompressed_bytes + 1 - len(raw))
        if len(raw) > max_decompressed_bytes:
            raise AugmentedArtifactError(
                "Augmented artifact gzip payload exceeds decompressed size limit."
            )
        if not decompressor.eof:
            raise AugmentedArtifactError(
                "Augmented artifact gzip payload is incomplete."
            )
        if decompressor.unused_data or decompressor.unconsumed_tail:
            raise AugmentedArtifactError(
                "Augmented artifact gzip payload has trailing data."
            )
        return raw
    except AugmentedArtifactError:
        raise
    except (EOFError, OSError, zlib.error) as error:
        raise AugmentedArtifactError(
            "Augmented artifact gzip payload is invalid."
        ) from error


def _deterministic_gzip(payload: bytes) -> bytes:
    output = io.BytesIO()
    try:
        with gzip.GzipFile(
            fileobj=output,
            mode="wb",
            filename="",
            mtime=0,
            compresslevel=9,
        ) as stream:
            stream.write(payload)
    except (OSError, TypeError, ValueError) as error:
        raise AugmentedArtifactError(
            "Augmented artifact gzip payload could not be serialized."
        ) from error
    return output.getvalue()


def _set_code(value: Any, *, field_name: str = "set_code") -> str:
    value = _required_string(value, field_name)
    if _SET_CODE_RE.fullmatch(value) is None:
        raise AugmentedArtifactError(
            f"{field_name} must be a lowercase path-safe set code."
        )
    return value


def _expected_set_code(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise AugmentedArtifactError("expected set code must be a non-empty string.")
    return _set_code(value.casefold(), field_name="expected set code")


def _timestamp(value: Any, field_name: str) -> str:
    timestamp = _stripped_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise AugmentedArtifactError(
            f"{field_name} must be an ISO-8601 timestamp."
        ) from error
    if parsed.tzinfo is None:
        raise AugmentedArtifactError(f"{field_name} must include a timezone.")
    return timestamp


def _hash(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AugmentedArtifactError(f"{field_name} must be a SHA-256 digest.")
    return value.lower()


def _https_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise AugmentedArtifactError(
            "url must be an absolute HTTPS URL without whitespace or control characters."
        )
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise AugmentedArtifactError("url must be an absolute HTTPS URL.") from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "#" in value
    ):
        raise AugmentedArtifactError("url must be an absolute HTTPS URL.")
    if port not in (None, 443) or parsed.netloc.endswith(":"):
        raise AugmentedArtifactError("url must use port 443 when a port is specified.")
    return value


def _candidate_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AugmentedArtifactError(f"{field_name} must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise AugmentedArtifactError(f"{field_name} must be a UUID string.") from error


def _float_vector(
    value: Any, *, field_name: str, length: int | None = None
) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        raise AugmentedArtifactError(f"{field_name} must be an array of numbers.")
    result = tuple(
        _finite_number(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )
    if length is not None and len(result) != length:
        raise AugmentedArtifactError(
            f"{field_name} must contain exactly {length} numbers."
        )
    return result


@dataclass(frozen=True, slots=True)
class AugmentedSource:
    """Provenance for one published training dump."""

    attribution: str
    event_type: str
    license: str
    retrieved_at: str
    sha256: str
    url: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "attribution", _stripped_string(self.attribution, "source.attribution")
        )
        object.__setattr__(
            self, "event_type", _stripped_string(self.event_type, "source.event_type")
        )
        object.__setattr__(
            self, "license", _stripped_string(self.license, "source.license")
        )
        object.__setattr__(
            self, "retrieved_at", _timestamp(self.retrieved_at, "source.retrieved_at")
        )
        object.__setattr__(self, "sha256", _hash(self.sha256, "source.sha256"))
        object.__setattr__(self, "url", _https_url(self.url))

    def to_json(self) -> dict[str, object]:
        return {
            "attribution": self.attribution,
            "event_type": self.event_type,
            "license": self.license,
            "retrieved_at": self.retrieved_at,
            "sha256": self.sha256,
            "url": self.url,
        }

    @classmethod
    def from_json(cls, value: Any) -> AugmentedSource:
        if not isinstance(value, Mapping):
            raise AugmentedArtifactError("source must be an object.")
        _exact_keys(value, _SOURCE_KEYS, "Augmented artifact source")
        return cls(
            attribution=value["attribution"],
            event_type=value["event_type"],
            license=value["license"],
            retrieved_at=value["retrieved_at"],
            sha256=value["sha256"],
            url=value["url"],
        )


@dataclass(frozen=True, slots=True)
class AugmentedMetricSet:
    """One pair of rank metrics without a pick count."""

    top_1: float
    mean_reciprocal_rank: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "top_1", _metric_value(self.top_1, "top_1"))
        object.__setattr__(
            self,
            "mean_reciprocal_rank",
            _metric_value(self.mean_reciprocal_rank, "mean_reciprocal_rank"),
        )

    def to_json(self) -> dict[str, object]:
        return {"mean_reciprocal_rank": self.mean_reciprocal_rank, "top_1": self.top_1}

    @classmethod
    def from_json(cls, value: Any, *, field_name: str) -> AugmentedMetricSet:
        if not isinstance(value, Mapping):
            raise AugmentedArtifactError(f"{field_name} must be an object.")
        _exact_keys(value, _METRIC_SET_KEYS, f"Augmented artifact {field_name}")
        return cls(
            top_1=value["top_1"], mean_reciprocal_rank=value["mean_reciprocal_rank"]
        )


@dataclass(frozen=True, slots=True)
class AugmentedMetricSummary:
    """Held-out metrics for Basic DO with and without augmentation."""

    picks: int
    basic_do: AugmentedMetricSet
    basic_plus_augmented: AugmentedMetricSet

    def __post_init__(self) -> None:
        object.__setattr__(self, "picks", _positive_integer(self.picks, "picks"))
        if not isinstance(self.basic_do, AugmentedMetricSet):
            raise AugmentedArtifactError("basic_do must be an AugmentedMetricSet.")
        if not isinstance(self.basic_plus_augmented, AugmentedMetricSet):
            raise AugmentedArtifactError(
                "basic_plus_augmented must be an AugmentedMetricSet."
            )

    def to_json(self) -> dict[str, object]:
        return {
            "basic_do": self.basic_do.to_json(),
            "basic_plus_augmented": self.basic_plus_augmented.to_json(),
            "picks": self.picks,
        }

    @classmethod
    def from_json(cls, value: Any) -> AugmentedMetricSummary:
        if not isinstance(value, Mapping):
            raise AugmentedArtifactError("metrics must be an object.")
        _exact_keys(value, _METRIC_SUMMARY_KEYS, "Augmented artifact metrics")
        return cls(
            picks=_positive_integer(value["picks"], "metrics.picks"),
            basic_do=AugmentedMetricSet.from_json(
                value["basic_do"], field_name="metrics.basic_do"
            ),
            basic_plus_augmented=AugmentedMetricSet.from_json(
                value["basic_plus_augmented"],
                field_name="metrics.basic_plus_augmented",
            ),
        )


def _evaluation_from_json(value: Any) -> AugmentedMetricSummary:
    if not isinstance(value, Mapping):
        raise AugmentedArtifactError("evaluation must be an object.")
    _exact_keys(value, _EVALUATION_KEYS, "Augmented artifact evaluation")
    summaries: dict[str, AugmentedMetricSet] = {}
    picks: int | None = None
    for side in ("basic_do", "basic_plus_augmented"):
        item = value[side]
        if not isinstance(item, Mapping):
            raise AugmentedArtifactError(f"evaluation.{side} must be an object.")
        _exact_keys(item, _METRIC_OBJECT_KEYS, f"Augmented artifact evaluation.{side}")
        side_picks = _positive_integer(item["picks"], f"evaluation.{side}.picks")
        if picks is None:
            picks = side_picks
        elif picks != side_picks:
            raise AugmentedArtifactError("evaluation picks must match.")
        summaries[side] = AugmentedMetricSet(
            top_1=_metric_value(item["top_1"], f"evaluation.{side}.top_1"),
            mean_reciprocal_rank=_metric_value(
                item["mean_reciprocal_rank"],
                f"evaluation.{side}.mean_reciprocal_rank",
            ),
        )
    assert picks is not None
    return AugmentedMetricSummary(
        picks=picks,
        basic_do=summaries["basic_do"],
        basic_plus_augmented=summaries["basic_plus_augmented"],
    )


def _evaluation_to_json(summary: AugmentedMetricSummary) -> dict[str, object]:
    return {
        "basic_do": {
            "mean_reciprocal_rank": summary.basic_do.mean_reciprocal_rank,
            "picks": summary.picks,
            "top_1": summary.basic_do.top_1,
        },
        "basic_plus_augmented": {
            "mean_reciprocal_rank": summary.basic_plus_augmented.mean_reciprocal_rank,
            "picks": summary.picks,
            "top_1": summary.basic_plus_augmented.top_1,
        },
    }


@dataclass(frozen=True, slots=True)
class AugmentedCalibration:
    """Bounded offered-mean calibration for one artifact."""

    method: str = AUGMENTED_ARTIFACT_CALIBRATION_METHOD
    multiplier: float = 0.0
    minimum_delta: float = -AUGMENTED_MAXIMUM_DELTA
    maximum_delta: float = AUGMENTED_MAXIMUM_DELTA
    selection_partition: str | None = None
    selection_metric: str | None = None
    candidates: tuple[Mapping[str, float], ...] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.method, str)
            or self.method != AUGMENTED_ARTIFACT_CALIBRATION_METHOD
        ):
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported calibration method {self.method!r}."
            )
        multiplier = _finite_number(self.multiplier, "calibration.multiplier")
        if not 0.0 <= multiplier <= AUGMENTED_MAXIMUM_MULTIPLIER:
            raise AugmentedArtifactError(
                "calibration.multiplier must be within [0.0, 16.0]."
            )
        maximum_delta = _finite_number(self.maximum_delta, "calibration.maximum_delta")
        if not 0.0 < maximum_delta <= AUGMENTED_MAXIMUM_DELTA:
            raise AugmentedArtifactError(
                "calibration.maximum_delta must be within (0.0, 8.0]."
            )
        minimum_delta = _finite_number(self.minimum_delta, "calibration.minimum_delta")
        if not -AUGMENTED_MAXIMUM_DELTA <= minimum_delta <= 0.0:
            raise AugmentedArtifactError(
                "calibration.minimum_delta must be within [-8.0, 0.0]."
            )
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "maximum_delta", maximum_delta)
        object.__setattr__(self, "minimum_delta", minimum_delta)
        if self.selection_partition is not None:
            object.__setattr__(
                self,
                "selection_partition",
                _stripped_string(
                    self.selection_partition, "calibration.selection_partition"
                ),
            )
        if self.selection_metric is not None:
            object.__setattr__(
                self,
                "selection_metric",
                _stripped_string(self.selection_metric, "calibration.selection_metric"),
            )
        if self.candidates is not None:
            if not isinstance(self.candidates, (list, tuple)):
                raise AugmentedArtifactError("calibration.candidates must be an array.")
            validated: list[dict[str, float]] = []
            for index, item in enumerate(self.candidates):
                if not isinstance(item, Mapping):
                    raise AugmentedArtifactError(
                        f"calibration.candidates[{index}] must be an object."
                    )
                _exact_keys(
                    item,
                    _CANDIDATE_KEYS,
                    f"Augmented artifact calibration.candidates[{index}]",
                )
                multiplier_value = _finite_number(
                    item["multiplier"],
                    f"calibration.candidates[{index}].multiplier",
                )
                if multiplier_value < 0.0:
                    raise AugmentedArtifactError(
                        f"calibration.candidates[{index}].multiplier must be >= 0.0."
                    )
                validated.append(
                    {
                        "multiplier": multiplier_value,
                        "top_1": _metric_value(
                            item["top_1"],
                            f"calibration.candidates[{index}].top_1",
                        ),
                        "mean_reciprocal_rank": _metric_value(
                            item["mean_reciprocal_rank"],
                            f"calibration.candidates[{index}].mean_reciprocal_rank",
                        ),
                    }
                )
            object.__setattr__(self, "candidates", tuple(validated))

    def to_json(self) -> dict[str, object]:
        value: dict[str, object] = {
            "maximum_delta": self.maximum_delta,
            "method": self.method,
            "minimum_delta": self.minimum_delta,
            "multiplier": self.multiplier,
        }
        if self.candidates is not None:
            value["candidates"] = [
                {
                    "mean_reciprocal_rank": item["mean_reciprocal_rank"],
                    "multiplier": item["multiplier"],
                    "top_1": item["top_1"],
                }
                for item in self.candidates
            ]
        if self.selection_metric is not None:
            value["selection_metric"] = self.selection_metric
        if self.selection_partition is not None:
            value["selection_partition"] = self.selection_partition
        return value

    @classmethod
    def from_json(cls, value: Any) -> AugmentedCalibration:
        if not isinstance(value, Mapping):
            raise AugmentedArtifactError("calibration must be an object.")
        keys = set(value)
        if any(not isinstance(key, str) for key in keys):
            raise AugmentedArtifactError("calibration keys must be strings.")
        unknown = keys - _CALIBRATION_KEYS
        if unknown:
            names = ", ".join(sorted(repr(item) for item in unknown))
            raise AugmentedArtifactError(
                f"Augmented artifact calibration contains unsupported fields: {names}."
            )
        missing = sorted(_CALIBRATION_REQUIRED_KEYS - keys)
        if missing:
            raise AugmentedArtifactError(
                f"calibration is missing required fields: {', '.join(missing)}."
            )
        return cls(
            method=value["method"],
            multiplier=value["multiplier"],
            minimum_delta=value["minimum_delta"],
            maximum_delta=value["maximum_delta"],
            selection_partition=value.get("selection_partition"),
            selection_metric=value.get("selection_metric"),
            candidates=value.get("candidates"),
        )


@dataclass(frozen=True, slots=True)
class AugmentedArtifact:
    """One validated per-set coarse-context augmentation artifact."""

    set_code: str
    candidate_ids: tuple[str, ...]
    input_weights: tuple[tuple[float, ...], ...]
    output_weights: tuple[tuple[float, ...], ...]
    bias: tuple[float, ...]
    calibration: AugmentedCalibration
    source: AugmentedSource
    evaluation: AugmentedMetricSummary
    schema_version: int = AUGMENTED_ARTIFACT_SCHEMA_VERSION
    compatibility: str = AUGMENTED_ARTIFACT_COMPATIBILITY
    model_name: str = AUGMENTED_ARTIFACT_MODEL_NAME
    architecture: str = AUGMENTED_ARTIFACT_ARCHITECTURE
    input_transform: str = AUGMENTED_ARTIFACT_INPUT_TRANSFORM
    hidden_activation: str = AUGMENTED_ARTIFACT_HIDDEN_ACTIVATION
    feature_schema: tuple[str, ...] = FEATURE_NAMES
    training: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise AugmentedArtifactIncompatibleError(
                "schema_version must be an integer."
            )
        if self.schema_version != AUGMENTED_ARTIFACT_SCHEMA_VERSION:
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported augmented artifact schema {self.schema_version}; "
                f"expected {AUGMENTED_ARTIFACT_SCHEMA_VERSION}."
            )
        if (
            not isinstance(self.compatibility, str)
            or self.compatibility != AUGMENTED_ARTIFACT_COMPATIBILITY
        ):
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported augmented artifact compatibility {self.compatibility!r}."
            )
        object.__setattr__(self, "set_code", _set_code(self.set_code))
        if (
            not isinstance(self.model_name, str)
            or self.model_name != AUGMENTED_ARTIFACT_MODEL_NAME
        ):
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported augmented model name {self.model_name!r}."
            )
        if (
            not isinstance(self.architecture, str)
            or self.architecture != AUGMENTED_ARTIFACT_ARCHITECTURE
        ):
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported augmented model architecture {self.architecture!r}."
            )
        if (
            not isinstance(self.input_transform, str)
            or self.input_transform != AUGMENTED_ARTIFACT_INPUT_TRANSFORM
        ):
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported augmented input transform {self.input_transform!r}."
            )
        if (
            not isinstance(self.hidden_activation, str)
            or self.hidden_activation != AUGMENTED_ARTIFACT_HIDDEN_ACTIVATION
        ):
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported augmented hidden activation {self.hidden_activation!r}."
            )
        if not isinstance(self.feature_schema, (list, tuple)) or tuple(
            self.feature_schema
        ) != tuple(FEATURE_NAMES):
            raise AugmentedArtifactIncompatibleError(
                "Unsupported augmented feature schema."
            )
        object.__setattr__(self, "feature_schema", tuple(self.feature_schema))
        if not isinstance(self.candidate_ids, (list, tuple)) or not self.candidate_ids:
            raise AugmentedArtifactError(
                "candidate_output_ids must contain at least one id."
            )
        normalized_ids = tuple(
            _candidate_id(item, f"candidate_output_ids[{index}]")
            for index, item in enumerate(self.candidate_ids)
        )
        if len(set(normalized_ids)) != len(normalized_ids):
            raise AugmentedArtifactError(
                "candidate_output_ids must not contain duplicates."
            )
        object.__setattr__(self, "candidate_ids", normalized_ids)
        feature_count = len(FEATURE_NAMES)
        if not isinstance(self.input_weights, (list, tuple)):
            raise AugmentedArtifactError("input_weights must be an array of arrays.")
        if len(self.input_weights) != feature_count:
            raise AugmentedArtifactError(
                f"input_weights must contain exactly {feature_count} rows."
            )
        rows: list[tuple[float, ...]] = []
        for index, row in enumerate(self.input_weights):
            if not isinstance(row, (list, tuple)):
                raise AugmentedArtifactError(
                    f"input_weights[{index}] must be an array of numbers."
                )
            rows.append(_float_vector(row, field_name=f"input_weights[{index}]"))
        hidden_size = len(rows[0])
        if hidden_size < 1:
            raise AugmentedArtifactError("input_weights rows must not be empty.")
        if any(len(row) != hidden_size for row in rows):
            raise AugmentedArtifactError(
                "input_weights rows must share one hidden size."
            )
        object.__setattr__(self, "input_weights", tuple(rows))
        if not isinstance(self.output_weights, (list, tuple)):
            raise AugmentedArtifactError("output_weights must be an array of arrays.")
        if len(self.output_weights) != hidden_size:
            raise AugmentedArtifactError(
                f"output_weights must contain exactly {hidden_size} rows."
            )
        outputs: list[tuple[float, ...]] = []
        for index, row in enumerate(self.output_weights):
            if not isinstance(row, (list, tuple)):
                raise AugmentedArtifactError(
                    f"output_weights[{index}] must be an array of numbers."
                )
            outputs.append(
                _float_vector(
                    row,
                    field_name=f"output_weights[{index}]",
                    length=len(normalized_ids),
                )
            )
        object.__setattr__(self, "output_weights", tuple(outputs))
        object.__setattr__(
            self,
            "bias",
            _float_vector(self.bias, field_name="bias", length=len(normalized_ids)),
        )
        if not isinstance(self.calibration, AugmentedCalibration):
            raise AugmentedArtifactError("calibration must be an AugmentedCalibration.")
        if not isinstance(self.source, AugmentedSource):
            raise AugmentedArtifactError("source must be an AugmentedSource.")
        if not isinstance(self.evaluation, AugmentedMetricSummary):
            raise AugmentedArtifactError("evaluation must be an AugmentedMetricSummary.")
        if self.training is not None and not isinstance(self.training, Mapping):
            raise AugmentedArtifactError("training must be an object.")

    def to_json(self) -> dict[str, object]:
        value: dict[str, object] = {
            "calibration": self.calibration.to_json(),
            "compatibility": self.compatibility,
            "evaluation": _evaluation_to_json(self.evaluation),
            "model": {
                "architecture": self.architecture,
                "candidate_output_ids": list(self.candidate_ids),
                "feature_schema": list(self.feature_schema),
                "name": self.model_name,
                "runtime_parameters": {
                    "bias": list(self.bias),
                    "hidden_activation": self.hidden_activation,
                    "input_transform": self.input_transform,
                    "input_weights": [list(row) for row in self.input_weights],
                    "output_weights": [list(row) for row in self.output_weights],
                },
            },
            "schema_version": self.schema_version,
            "set_code": self.set_code,
            "source": self.source.to_json(),
        }
        if self.training is not None:
            value["training"] = dict(self.training)
        return value

    def to_bytes(self) -> bytes:
        """Serialize this artifact as canonical UTF-8 JSON bytes."""

        return _canonical_json_bytes(self.to_json())

    def to_gzip_bytes(self) -> bytes:
        """Serialize canonical JSON bytes in a deterministic gzip container."""

        return _deterministic_gzip(self.to_bytes())

    @classmethod
    def from_json(
        cls,
        value: Mapping[str, Any],
        *,
        expected_set_code: str | None = None,
        expected_compatibility: str | None = None,
    ) -> AugmentedArtifact:
        """Parse one artifact object with strict schema and value checks."""

        if not isinstance(value, Mapping):
            raise AugmentedArtifactError("Augmented artifact must be a JSON object.")
        keys = set(value)
        if any(not isinstance(key, str) for key in keys):
            raise AugmentedArtifactError("Augmented artifact keys must be strings.")
        unknown = keys - _TOP_LEVEL_KEYS - _OPTIONAL_TOP_KEYS
        if unknown:
            names = ", ".join(sorted(repr(item) for item in unknown))
            raise AugmentedArtifactError(
                f"Augmented artifact contains unsupported fields: {names}."
            )
        missing = sorted(_TOP_LEVEL_KEYS - keys)
        if missing:
            raise AugmentedArtifactError(
                f"Augmented artifact is missing required fields: {', '.join(missing)}."
            )
        model = value["model"]
        if not isinstance(model, Mapping):
            raise AugmentedArtifactError("model must be an object.")
        _exact_keys(model, _MODEL_KEYS, "Augmented artifact model")
        runtime = model["runtime_parameters"]
        if not isinstance(runtime, Mapping):
            raise AugmentedArtifactError("runtime_parameters must be an object.")
        _exact_keys(
            runtime, _RUNTIME_PARAMETER_KEYS, "Augmented artifact runtime_parameters"
        )
        result = cls(
            set_code=value["set_code"],
            candidate_ids=model["candidate_output_ids"],
            input_weights=runtime["input_weights"],
            output_weights=runtime["output_weights"],
            bias=runtime["bias"],
            calibration=AugmentedCalibration.from_json(value["calibration"]),
            source=AugmentedSource.from_json(value["source"]),
            evaluation=_evaluation_from_json(value["evaluation"]),
            schema_version=value["schema_version"],
            compatibility=value["compatibility"],
            model_name=model["name"],
            architecture=model["architecture"],
            input_transform=runtime["input_transform"],
            hidden_activation=runtime["hidden_activation"],
            feature_schema=model["feature_schema"],
            training=_training_from_json(value.get("training")),
        )
        if expected_set_code is not None:
            expected = _expected_set_code(expected_set_code)
            if result.set_code != expected:
                raise AugmentedArtifactError(
                    f"Augmented artifact belongs to set {result.set_code!r}, "
                    f"expected {expected!r}."
                )
        if (
            expected_compatibility is not None
            and result.compatibility != expected_compatibility
        ):
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported augmented artifact compatibility {result.compatibility!r}."
            )
        return result

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        expected_set_code: str | None = None,
        expected_compatibility: str | None = None,
    ) -> AugmentedArtifact:
        """Parse raw bytes and reject malformed or non-canonical JSON."""

        if not isinstance(payload, bytes):
            raise AugmentedArtifactError("Augmented artifact bytes must be bytes.")
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_duplicate_checking_object,
                parse_constant=_reject_json_constant,
            )
        except AugmentedArtifactError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise AugmentedArtifactError(
                "Augmented artifact JSON could not be parsed."
            ) from error
        result = cls.from_json(
            value,
            expected_set_code=expected_set_code,
            expected_compatibility=expected_compatibility,
        )
        if payload != result.to_bytes():
            raise AugmentedArtifactError("Augmented artifact JSON is not canonical.")
        return result

    @classmethod
    def from_gzip_bytes(
        cls,
        payload: bytes,
        *,
        expected_set_code: str | None = None,
        expected_compatibility: str | None = None,
        expected_sha256: str | None = None,
        expected_byte_size: int | None = None,
        max_decompressed_bytes: int = AUGMENTED_ARTIFACT_MAX_DECOMPRESSED_BYTES,
    ) -> AugmentedArtifact:
        """Parse a bounded gzip artifact and reject non-canonical containers."""

        if not isinstance(payload, bytes):
            raise AugmentedArtifactError("Augmented artifact gzip bytes must be bytes.")
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str):
                raise AugmentedArtifactError("expected_sha256 must be a string.")
            if hashlib.sha256(payload).hexdigest() != expected_sha256.lower():
                raise AugmentedArtifactError(
                    "Augmented artifact checksum does not match the expected digest."
                )
        raw = _decode_gzip(payload, max_decompressed_bytes=max_decompressed_bytes)
        result = cls.from_bytes(raw)
        if payload != result.to_gzip_bytes():
            raise AugmentedArtifactError(
                "Augmented artifact gzip bytes are not canonical."
            )
        if expected_set_code is not None:
            expected = _expected_set_code(expected_set_code)
            if result.set_code != expected:
                raise AugmentedArtifactError(
                    f"Augmented artifact belongs to set {result.set_code!r}, "
                    f"expected {expected!r}."
                )
        if (
            expected_compatibility is not None
            and result.compatibility != expected_compatibility
        ):
            raise AugmentedArtifactIncompatibleError(
                f"Unsupported augmented artifact compatibility {result.compatibility!r}."
            )
        if expected_byte_size is not None:
            if isinstance(expected_byte_size, bool) or not isinstance(
                expected_byte_size, int
            ):
                raise AugmentedArtifactError("expected_byte_size must be an integer.")
            if len(raw) != expected_byte_size:
                raise AugmentedArtifactError(
                    "Augmented artifact size does not match the expected byte count."
                )
        return result

    def candidate_deltas(
        self, *, pool_features: Sequence[int], candidate_ids: Sequence[str | None]
    ) -> tuple[float, ...]:
        """Return one bounded delta per offered candidate id."""

        if (
            not isinstance(pool_features, (list, tuple))
            or len(pool_features) != len(FEATURE_NAMES)
            or any(
                isinstance(item, bool)
                or not isinstance(item, int)
                or item < 0
                for item in pool_features
            )
        ):
            raise AugmentedArtifactError(
                f"pool_features must be {len(FEATURE_NAMES)} non-negative integers."
            )
        if not isinstance(candidate_ids, (list, tuple)):
            raise AugmentedArtifactError("candidate_ids must be a sequence.")
        transformed = [math.log1p(item) for item in pool_features]
        hidden = [
            math.tanh(
                sum(
                    transformed[j] * self.input_weights[j][h]
                    for j in range(len(FEATURE_NAMES))
                )
            )
            for h in range(len(self.output_weights))
        ]
        logits = [
            self.bias[i]
            + sum(hidden[h] * self.output_weights[h][i] for h in range(len(hidden)))
            for i in range(len(self.candidate_ids))
        ]
        index_by_id = {item: index for index, item in enumerate(self.candidate_ids)}
        known_logits: list[float] = []
        positions: list[int | None] = []
        for offered in candidate_ids:
            normalized: str | None = None
            if isinstance(offered, str) and offered:
                try:
                    normalized = str(uuid.UUID(offered))
                except (ValueError, AttributeError, TypeError):
                    normalized = None
            position = index_by_id.get(normalized) if normalized is not None else None
            positions.append(position)
            if position is not None:
                known_logits.append(logits[position])
        if not known_logits:
            return (0.0,) * len(candidate_ids)
        mean = sum(known_logits) / len(known_logits)
        deltas: list[float] = []
        for position in positions:
            if position is None:
                deltas.append(0.0)
                continue
            raw = (logits[position] - mean) * self.calibration.multiplier
            clipped = min(
                max(raw, self.calibration.minimum_delta), self.calibration.maximum_delta
            )
            deltas.append(
                min(max(clipped, -AUGMENTED_MAXIMUM_DELTA), AUGMENTED_MAXIMUM_DELTA)
            )
        return tuple(deltas)


def _training_from_json(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AugmentedArtifactError("training must be an object.")
    return dict(value)


def pool_feature_counts(*, pool_cards: Iterable[CardInfo]) -> tuple[int, ...]:
    """Sum fixed-schema feature membership over cards already picked."""

    totals = [0] * len(FEATURE_NAMES)
    for card in pool_cards:
        membership = card_feature_membership(card=card)
        for index, included in enumerate(membership):
            totals[index] += included
    return tuple(totals)


__all__ = [
    "AUGMENTED_ARTIFACT_ARCHITECTURE",
    "AUGMENTED_ARTIFACT_CALIBRATION_METHOD",
    "AUGMENTED_ARTIFACT_COMPATIBILITY",
    "AUGMENTED_ARTIFACT_HIDDEN_ACTIVATION",
    "AUGMENTED_ARTIFACT_INPUT_TRANSFORM",
    "AUGMENTED_ARTIFACT_MAX_DECOMPRESSED_BYTES",
    "AUGMENTED_ARTIFACT_MODEL_NAME",
    "AUGMENTED_ARTIFACT_SCHEMA_VERSION",
    "AUGMENTED_MAXIMUM_DELTA",
    "AUGMENTED_MAXIMUM_MULTIPLIER",
    "AugmentedArtifact",
    "AugmentedArtifactError",
    "AugmentedArtifactIncompatibleError",
    "AugmentedCalibration",
    "AugmentedMetricSet",
    "AugmentedMetricSummary",
    "AugmentedSource",
    "pool_feature_counts",
]
