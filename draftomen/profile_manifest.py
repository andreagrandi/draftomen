"""Canonical remote manifest contract for published set-profile artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, TypeAlias
from urllib.parse import urlsplit

from draftomen.set_profile import (
    ProfileMaturity,
    SUPPORTED_SET_PROFILE_SCHEMA_VERSIONS,
)


PROFILE_MANIFEST_SCHEMA_VERSION = 1
PathInput: TypeAlias = str | os.PathLike[str]
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")


class ProfileManifestError(ValueError):
    """Raised when a profile manifest is malformed or cannot be persisted."""


class ProfileManifestSchemaError(ProfileManifestError):
    """Raised when a profile manifest does not match the supported schema."""


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise ProfileManifestSchemaError(f"Missing required profile manifest field {key!r}.")
    return value[key]


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileManifestSchemaError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _component(value: Any, field_name: str) -> str:
    normalized = _string(value, field_name).casefold()
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ProfileManifestSchemaError(f"{field_name} must be a safe path component.")
    return normalized


def _integer(value: Any, field_name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileManifestSchemaError(f"{field_name} must be an integer.")
    if value < (1 if positive else 0):
        bound = "positive" if positive else "non-negative"
        raise ProfileManifestSchemaError(f"{field_name} must be a {bound} integer.")
    return value


def _timestamp(value: Any, field_name: str) -> str:
    timestamp = _string(value, field_name)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProfileManifestSchemaError(f"{field_name} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise ProfileManifestSchemaError(f"{field_name} must include a timezone.")
    return timestamp


def _hash(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ProfileManifestSchemaError(f"{field_name} must be a SHA-256 digest.")
    return value.lower()


def _https_url(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise ProfileManifestSchemaError(
            "url must be an absolute HTTPS URL without whitespace or control characters."
        )
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing .port is intentional: urllib.parse defers malformed-port
        # errors until this property is read.
        port = parsed.port
    except ValueError as error:
        raise ProfileManifestSchemaError("url must be an absolute HTTPS URL.") from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "#" in value
    ):
        raise ProfileManifestSchemaError("url must be an absolute HTTPS URL.")
    if port not in (None, 443) or parsed.netloc.endswith(":"):
        raise ProfileManifestSchemaError("url must use port 443 when a port is specified.")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    unknown = set(value) - expected
    if unknown:
        names = ", ".join(sorted(repr(item) for item in unknown))
        raise ProfileManifestSchemaError(f"{field_name} contains unsupported fields: {names}.")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")

@dataclass(frozen=True, slots=True)
class ProfileManifestArtifact:
    """One immutable, remotely downloadable set-profile artifact record."""

    set_code: str
    event_format: str
    set_profile_schema_version: int
    profile_version: str
    generated_at: str
    url: str
    gzip_bytes: int
    profile_bytes: int
    gzip_sha256: str
    profile_sha256: str
    maturity: ProfileMaturity | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "set_code", _component(self.set_code, "set_code"))
        object.__setattr__(self, "event_format", _component(self.event_format, "format"))
        schema_version = _integer(
            self.set_profile_schema_version,
            "set_profile_schema_version",
            positive=True,
        )
        if schema_version not in SUPPORTED_SET_PROFILE_SCHEMA_VERSIONS:
            raise ProfileManifestSchemaError(
                f"Unsupported set-profile schema {schema_version}; "
                f"supported versions are {SUPPORTED_SET_PROFILE_SCHEMA_VERSIONS}."
            )
        object.__setattr__(self, "set_profile_schema_version", schema_version)
        object.__setattr__(self, "profile_version", _string(self.profile_version, "profile_version"))
        object.__setattr__(self, "generated_at", _timestamp(self.generated_at, "generated_at"))
        object.__setattr__(self, "url", _https_url(self.url))
        object.__setattr__(self, "gzip_bytes", _integer(self.gzip_bytes, "gzip_bytes", positive=True))
        object.__setattr__(self, "profile_bytes", _integer(self.profile_bytes, "profile_bytes", positive=True))
        object.__setattr__(self, "gzip_sha256", _hash(self.gzip_sha256, "gzip_sha256"))
        object.__setattr__(self, "profile_sha256", _hash(self.profile_sha256, "profile_sha256"))
        try:
            maturity = self.maturity.value if isinstance(self.maturity, ProfileMaturity) else _string(self.maturity, "maturity")
            maturity = ProfileMaturity(maturity)
        except (TypeError, ValueError, ProfileManifestSchemaError) as error:
            raise ProfileManifestSchemaError(f"Unsupported profile maturity {self.maturity!r}.") from error
        if maturity is ProfileMaturity.GENERIC:
            raise ProfileManifestSchemaError("Profile manifest artifacts cannot have generic maturity.")
        object.__setattr__(self, "maturity", maturity)

    @property
    def format(self) -> str:
        """Return the normalized event format under its manifest name."""

        return self.event_format

    def to_json(self) -> dict[str, object]:
        return {
            "format": self.event_format,
            "generated_at": self.generated_at,
            "gzip_bytes": self.gzip_bytes,
            "gzip_sha256": self.gzip_sha256,
            "maturity": self.maturity.value,
            "profile_bytes": self.profile_bytes,
            "profile_sha256": self.profile_sha256,
            "profile_version": self.profile_version,
            "set_code": self.set_code,
            "set_profile_schema_version": self.set_profile_schema_version,
            "url": self.url,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ProfileManifestArtifact:
        if not isinstance(value, Mapping):
            raise ProfileManifestSchemaError("Profile manifest artifact must be an object.")
        _keys(
            value,
            {
                "format",
                "generated_at",
                "gzip_bytes",
                "gzip_sha256",
                "maturity",
                "profile_bytes",
                "profile_sha256",
                "profile_version",
                "set_code",
                "set_profile_schema_version",
                "url",
            },
            "profile manifest artifact",
        )
        return cls(
            set_code=_required(value, "set_code"),
            event_format=_required(value, "format"),
            set_profile_schema_version=_required(value, "set_profile_schema_version"),
            profile_version=_required(value, "profile_version"),
            generated_at=_required(value, "generated_at"),
            url=_required(value, "url"),
            gzip_bytes=_required(value, "gzip_bytes"),
            profile_bytes=_required(value, "profile_bytes"),
            gzip_sha256=_required(value, "gzip_sha256"),
            profile_sha256=_required(value, "profile_sha256"),
            maturity=_required(value, "maturity"),
        )


@dataclass(frozen=True, slots=True)
class ProfileManifest:
    """Canonical manifest containing exactly one artifact per set and format."""

    artifacts: tuple[ProfileManifestArtifact, ...]
    published_at: str
    schema_version: int = PROFILE_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        schema_version = _integer(self.schema_version, "schema_version", positive=True)
        if schema_version != PROFILE_MANIFEST_SCHEMA_VERSION:
            raise ProfileManifestSchemaError(
                f"Unsupported profile manifest schema {schema_version}; expected {PROFILE_MANIFEST_SCHEMA_VERSION}."
            )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "published_at", _timestamp(self.published_at, "published_at"))
        artifacts = tuple(self.artifacts)
        if not artifacts or any(not isinstance(item, ProfileManifestArtifact) for item in artifacts):
            raise ProfileManifestSchemaError(
                "Profile manifest artifacts must contain at least one ProfileManifestArtifact."
            )
        identities = {(item.set_code, item.event_format) for item in artifacts}
        if len(identities) != len(artifacts):
            raise ProfileManifestSchemaError(
                "Profile manifest contains duplicate set and format artifacts."
            )
        object.__setattr__(self, "artifacts", tuple(sorted(artifacts, key=lambda item: (item.set_code, item.event_format))))

    def to_json(self) -> dict[str, object]:
        return {
            "artifacts": [item.to_json() for item in self.artifacts],
            "published_at": self.published_at,
            "schema_version": self.schema_version,
        }

    def to_bytes(self) -> bytes:
        return _json_bytes(self.to_json())

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ProfileManifest:
        if not isinstance(value, Mapping):
            raise ProfileManifestSchemaError("Profile manifest must be an object.")
        _keys(value, {"artifacts", "published_at", "schema_version"}, "profile manifest")
        schema_version = _required(value, "schema_version")
        if schema_version != PROFILE_MANIFEST_SCHEMA_VERSION:
            raise ProfileManifestSchemaError(
                f"Unsupported profile manifest schema {schema_version}; expected {PROFILE_MANIFEST_SCHEMA_VERSION}."
            )
        artifacts = _required(value, "artifacts")
        if not isinstance(artifacts, list):
            raise ProfileManifestSchemaError("Profile manifest artifacts must be an array.")
        return cls(
            artifacts=tuple(ProfileManifestArtifact.from_json(item) for item in artifacts),
            published_at=_required(value, "published_at"),
            schema_version=schema_version,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> ProfileManifest:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise ProfileManifestSchemaError(f"Could not parse profile manifest: {error}.") from error
        return cls.from_json(value)

    def select(self, *, set_code: str, event_format: str) -> ProfileManifestArtifact | None:
        """Select only an exact normalized set/format identity, if present."""

        normalized_set = _component(set_code, "set_code")
        normalized_format = _component(event_format, "format")
        return next(
            (
                item
                for item in self.artifacts
                if item.set_code == normalized_set and item.event_format == normalized_format
            ),
            None,
        )


def load_profile_manifest(path: PathInput) -> ProfileManifest:
    """Load and strictly validate one profile manifest."""

    try:
        with Path(path).open(mode="rb") as manifest_file:
            return ProfileManifest.from_bytes(manifest_file.read())
    except ProfileManifestError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError) as error:
        raise ProfileManifestError(f"Could not read profile manifest {path}: {error}.") from error


def dump_profile_manifest(manifest: ProfileManifest, path: PathInput) -> Path:
    """Write canonical profile manifest bytes atomically."""

    if not isinstance(manifest, ProfileManifest):
        raise TypeError("manifest must be a ProfileManifest.")
    try:
        output = Path(path)
    except (TypeError, ValueError) as error:
        raise ProfileManifestError("Profile manifest path must be a valid local path.") from error
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
    except OSError as error:
        raise ProfileManifestError(f"Could not write profile manifest {output}: {error}.") from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
    return output


__all__ = [
    "PROFILE_MANIFEST_SCHEMA_VERSION",
    "ProfileManifest",
    "ProfileManifestArtifact",
    "ProfileManifestError",
    "ProfileManifestSchemaError",
    "dump_profile_manifest",
    "load_profile_manifest",
]
