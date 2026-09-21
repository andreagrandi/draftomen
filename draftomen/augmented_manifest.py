"""Canonical remote manifest contract for per-set augmentation artifacts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import tempfile
from os import PathLike
from typing import Any, Mapping, TypeAlias

from draftomen.augmented_artifact import (
    AugmentedMetricSummary,
    AugmentedSource,
)

AUGMENTED_MANIFEST_SCHEMA_VERSION = 1
PathInput: TypeAlias = str | PathLike[str]
_SET_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_TOP_LEVEL_KEYS = {"schema_version", "published_at", "sets"}
_ENTRY_KEYS = {
    "artifact_bytes",
    "artifact_schema_version",
    "artifact_sha256",
    "compatibility",
    "metrics",
    "source",
}


class AugmentedManifestError(ValueError):
    """Report a manifest that cannot be trusted for artifact selection."""


class AugmentedManifestSchemaError(AugmentedManifestError):
    """Report a manifest that does not match the supported schema."""


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise AugmentedManifestSchemaError(
            f"Missing required augmented manifest field {key!r}."
        )
    return value[key]


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AugmentedManifestSchemaError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _integer(value: Any, field_name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AugmentedManifestSchemaError(f"{field_name} must be an integer.")
    if value < (1 if positive else 0):
        bound = "positive" if positive else "non-negative"
        raise AugmentedManifestSchemaError(f"{field_name} must be a {bound} integer.")
    return value


def _timestamp(value: Any, field_name: str) -> str:
    timestamp = _string(value, field_name)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise AugmentedManifestSchemaError(
            f"{field_name} must be an ISO-8601 timestamp."
        ) from error
    if parsed.tzinfo is None:
        raise AugmentedManifestSchemaError(f"{field_name} must include a timezone.")
    return timestamp


def _hash(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AugmentedManifestSchemaError(f"{field_name} must be a SHA-256 digest.")
    return value.lower()


def _set_code(value: Any, *, field_name: str = "set_code") -> str:
    if not isinstance(value, str) or not value:
        raise AugmentedManifestSchemaError(f"{field_name} must be a non-empty string.")
    if _SET_CODE_RE.fullmatch(value) is None:
        raise AugmentedManifestSchemaError(
            f"{field_name} must be a lowercase path-safe set code."
        )
    return value


def _normalize_lookup(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise AugmentedManifestSchemaError("set_code must be a non-empty string.")
    normalized = value.casefold()
    if _SET_CODE_RE.fullmatch(normalized) is None:
        raise AugmentedManifestSchemaError(
            "set_code must be a lowercase path-safe set code."
        )
    return normalized


def _keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    unknown = set(value) - expected
    if unknown:
        names = ", ".join(sorted(repr(item) for item in unknown))
        raise AugmentedManifestSchemaError(
            f"{field_name} contains unsupported fields: {names}."
        )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _duplicate_checking_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AugmentedManifestSchemaError(f"Duplicate JSON object key {key!r}.")
        value[key] = item
    return value


def _fsync_directory(path: Path) -> None:
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


def _source_from_json(value: Any) -> AugmentedSource:
    if not isinstance(value, Mapping):
        raise AugmentedManifestSchemaError("source must be an object.")
    try:
        return AugmentedSource.from_json(value)
    except ValueError as error:
        raise AugmentedManifestSchemaError(str(error)) from error


def _metrics_from_json(value: Any) -> AugmentedMetricSummary:
    if not isinstance(value, Mapping):
        raise AugmentedManifestSchemaError("metrics must be an object.")
    try:
        return AugmentedMetricSummary.from_json(value)
    except ValueError as error:
        raise AugmentedManifestSchemaError(str(error)) from error


@dataclass(frozen=True, slots=True)
class AugmentedManifestEntry:
    """One immutable, remotely downloadable augmentation artifact record."""

    set_code: str
    artifact_bytes: int
    artifact_schema_version: int
    artifact_sha256: str
    compatibility: str
    source: AugmentedSource
    metrics: AugmentedMetricSummary

    def __post_init__(self) -> None:
        object.__setattr__(self, "set_code", _set_code(self.set_code))
        object.__setattr__(
            self,
            "artifact_bytes",
            _integer(self.artifact_bytes, "artifact_bytes", positive=True),
        )
        object.__setattr__(
            self,
            "artifact_schema_version",
            _integer(
                self.artifact_schema_version, "artifact_schema_version", positive=True
            ),
        )
        object.__setattr__(
            self, "artifact_sha256", _hash(self.artifact_sha256, "artifact_sha256")
        )
        object.__setattr__(
            self, "compatibility", _string(self.compatibility, "compatibility")
        )
        if not isinstance(self.source, AugmentedSource):
            raise AugmentedManifestSchemaError("source must be an AugmentedSource.")
        if not isinstance(self.metrics, AugmentedMetricSummary):
            raise AugmentedManifestSchemaError(
                "metrics must be an AugmentedMetricSummary."
            )

    def to_json(self) -> dict[str, object]:
        return {
            "artifact_bytes": self.artifact_bytes,
            "artifact_schema_version": self.artifact_schema_version,
            "artifact_sha256": self.artifact_sha256,
            "compatibility": self.compatibility,
            "metrics": self.metrics.to_json(),
            "source": self.source.to_json(),
        }

    @classmethod
    def from_json(
        cls, value: Mapping[str, Any], *, set_code: str
    ) -> AugmentedManifestEntry:
        if not isinstance(value, Mapping):
            raise AugmentedManifestSchemaError(
                "Augmented manifest entry must be an object."
            )
        _keys(value, _ENTRY_KEYS, "augmented manifest entry")
        return cls(
            set_code=set_code,
            artifact_bytes=_required(value, "artifact_bytes"),
            artifact_schema_version=_required(value, "artifact_schema_version"),
            artifact_sha256=_required(value, "artifact_sha256"),
            compatibility=_required(value, "compatibility"),
            source=_source_from_json(_required(value, "source")),
            metrics=_metrics_from_json(_required(value, "metrics")),
        )


@dataclass(frozen=True, slots=True)
class AugmentedManifest:
    """Canonical manifest containing exactly one artifact record per set."""

    entries: tuple[AugmentedManifestEntry, ...]
    published_at: str
    schema_version: int = AUGMENTED_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        schema_version = _integer(self.schema_version, "schema_version", positive=True)
        if schema_version != AUGMENTED_MANIFEST_SCHEMA_VERSION:
            raise AugmentedManifestSchemaError(
                f"Unsupported augmented manifest schema {schema_version}; "
                f"expected {AUGMENTED_MANIFEST_SCHEMA_VERSION}."
            )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(
            self, "published_at", _timestamp(self.published_at, "published_at")
        )
        entries = tuple(self.entries)
        if not entries or any(
            not isinstance(item, AugmentedManifestEntry) for item in entries
        ):
            raise AugmentedManifestSchemaError(
                "Augmented manifest must contain at least one set entry."
            )
        codes = [item.set_code for item in entries]
        if len(set(codes)) != len(codes):
            raise AugmentedManifestSchemaError(
                "Augmented manifest contains duplicate set entries."
            )
        object.__setattr__(
            self, "entries", tuple(sorted(entries, key=lambda item: item.set_code))
        )

    def to_json(self) -> dict[str, object]:
        return {
            "published_at": self.published_at,
            "schema_version": self.schema_version,
            "sets": {item.set_code: item.to_json() for item in self.entries},
        }

    def to_bytes(self) -> bytes:
        return _json_bytes(self.to_json())

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> AugmentedManifest:
        if not isinstance(value, Mapping):
            raise AugmentedManifestSchemaError("Augmented manifest must be an object.")
        _keys(value, _TOP_LEVEL_KEYS, "augmented manifest")
        schema_version = _required(value, "schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != AUGMENTED_MANIFEST_SCHEMA_VERSION
        ):
            raise AugmentedManifestSchemaError(
                f"Unsupported augmented manifest schema {schema_version}; expected 1."
            )
        sets = _required(value, "sets")
        if not isinstance(sets, dict):
            raise AugmentedManifestSchemaError(
                "Augmented manifest sets must be an object."
            )
        if not sets:
            raise AugmentedManifestSchemaError(
                "Augmented manifest must contain at least one set entry."
            )
        entries: list[AugmentedManifestEntry] = []
        for key, item in sets.items():
            _set_code(key)
            entries.append(AugmentedManifestEntry.from_json(item, set_code=key))
        return cls(
            entries=tuple(entries),
            published_at=_required(value, "published_at"),
            schema_version=schema_version,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> AugmentedManifest:
        if not isinstance(payload, bytes):
            raise AugmentedManifestSchemaError(
                "Augmented manifest bytes must be bytes."
            )
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_duplicate_checking_object,
            )
        except AugmentedManifestSchemaError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise AugmentedManifestSchemaError(
                f"Could not parse augmented manifest: {error}."
            ) from error
        return cls.from_json(value)

    def select(self, *, set_code: str) -> AugmentedManifestEntry | None:
        """Return the entry for one normalized set code, if present."""

        normalized = _normalize_lookup(set_code)
        return next(
            (item for item in self.entries if item.set_code == normalized),
            None,
        )

    def entry_set_codes(self) -> tuple[str, ...]:
        return tuple(item.set_code for item in self.entries)


def load_augmented_manifest(path: PathInput) -> AugmentedManifest:
    """Load and strictly validate one augmented manifest."""

    try:
        with Path(path).open(mode="rb") as manifest_file:
            return AugmentedManifest.from_bytes(manifest_file.read())
    except AugmentedManifestError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError) as error:
        raise AugmentedManifestError(
            f"Could not read augmented manifest {path}: {error}."
        ) from error


def dump_augmented_manifest(manifest: AugmentedManifest, path: PathInput) -> Path:
    """Write canonical augmented manifest bytes atomically."""

    if not isinstance(manifest, AugmentedManifest):
        raise TypeError("manifest must be an AugmentedManifest.")
    try:
        output = Path(path)
    except (TypeError, ValueError) as error:
        raise AugmentedManifestError(
            "Augmented manifest path must be a valid local path."
        ) from error
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(manifest.to_bytes())
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
        _fsync_directory(output.parent)
    except OSError as error:
        raise AugmentedManifestError(
            f"Could not write augmented manifest {output}: {error}."
        ) from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
    return output


__all__ = [
    "AUGMENTED_MANIFEST_SCHEMA_VERSION",
    "AugmentedManifest",
    "AugmentedManifestEntry",
    "AugmentedManifestError",
    "AugmentedManifestSchemaError",
    "dump_augmented_manifest",
    "load_augmented_manifest",
]
