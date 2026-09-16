"""Durable record of profiles published from confirmed enrichment artifacts.

The record survives every website data refresh and is read by the enrichment
downgrade guard, which must protect a publication even when the retained profile
object is missing.  This module deliberately imports nothing from
``draftomen.profile_publication``: that module imports this one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, TypeAlias


ENRICHMENT_PUBLICATIONS_FILE_NAME = "enrichment-publications.json"
ENRICHMENT_PUBLICATIONS_SCHEMA_VERSION = 1
READ_ERROR = "Could not read the enrichment publication record."
WRITE_ERROR = "Could not publish the enrichment publication record."

PathInput: TypeAlias = str | os.PathLike[str]

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PUBLICATION_KEYS = frozenset(
    {
        "artifact_sha256",
        "event_format",
        "profile_gzip_sha256",
        "published_at",
        "reviewed_at",
        "run_id",
        "set_code",
    }
)
_RECORD_KEYS = frozenset({"publications", "schema_version"})


class EnrichmentPublicationError(RuntimeError):
    """Raised when the durable enrichment publication record is not usable."""


def _required(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise EnrichmentPublicationError(f"Missing required enrichment publication field {key!r}.")
    return value[key]


def _keys(value: Mapping[str, Any], expected: frozenset[str], field_name: str) -> None:
    unknown = set(value) - expected
    if unknown:
        names = ", ".join(sorted(repr(item) for item in unknown))
        raise EnrichmentPublicationError(f"{field_name} contains unsupported fields: {names}.")


def _identifier(value: Any, field_name: str) -> str:
    """Validate one identity string without changing its case."""

    message = f"{field_name} must be a safe identifier."
    if not isinstance(value, str) or not value.strip():
        raise EnrichmentPublicationError(message)
    normalized = value.strip()
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise EnrichmentPublicationError(message)
    return normalized


def _digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise EnrichmentPublicationError(f"{field_name} must be a lowercase SHA-256 digest.")
    return value


def _timestamp(value: Any, field_name: str) -> str:
    message = f"{field_name} must be a timezone-aware ISO-8601 timestamp."
    if not isinstance(value, str):
        raise EnrichmentPublicationError(message)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EnrichmentPublicationError(message) from error
    if parsed.tzinfo is None:
        raise EnrichmentPublicationError(message)
    return value


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _path(*, value: PathInput, field_name: str) -> Path:
    try:
        return Path(value).expanduser()
    except (TypeError, ValueError) as error:
        raise EnrichmentPublicationError(f"{field_name} must be a valid local path.") from error


def _existing_bytes(*, path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write(*, path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class EnrichmentPublication:
    """One published profile produced from a confirmed enrichment artifact.

    The identity key is ``event_format`` rather than ``format`` so a record entry
    stays interchangeable with the ``ProfileManifestArtifact`` it describes.
    """

    set_code: str
    event_format: str
    artifact_sha256: str
    run_id: str
    reviewed_at: str
    published_at: str
    profile_gzip_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "set_code", _identifier(self.set_code, "set_code").casefold()
        )
        object.__setattr__(
            self, "event_format", _identifier(self.event_format, "event_format").casefold()
        )
        _digest(self.artifact_sha256, "artifact_sha256")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        _timestamp(self.reviewed_at, "reviewed_at")
        _timestamp(self.published_at, "published_at")
        _digest(self.profile_gzip_sha256, "profile_gzip_sha256")

    def to_json(self) -> dict[str, object]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "event_format": self.event_format,
            "profile_gzip_sha256": self.profile_gzip_sha256,
            "published_at": self.published_at,
            "reviewed_at": self.reviewed_at,
            "run_id": self.run_id,
            "set_code": self.set_code,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> EnrichmentPublication:
        if not isinstance(value, Mapping):
            raise EnrichmentPublicationError("An enrichment publication must be an object.")
        _keys(value, _PUBLICATION_KEYS, "enrichment publication")
        return cls(
            set_code=_required(value, "set_code"),
            event_format=_required(value, "event_format"),
            artifact_sha256=_required(value, "artifact_sha256"),
            run_id=_required(value, "run_id"),
            reviewed_at=_required(value, "reviewed_at"),
            published_at=_required(value, "published_at"),
            profile_gzip_sha256=_required(value, "profile_gzip_sha256"),
        )


@dataclass(frozen=True, slots=True)
class EnrichmentPublications:
    """Canonical durable record of enrichment-backed profile publications."""

    publications: tuple[EnrichmentPublication, ...] = ()

    def __post_init__(self) -> None:
        publications = tuple(self.publications)
        identities = {(item.set_code, item.event_format) for item in publications}
        if len(identities) != len(publications):
            raise EnrichmentPublicationError(
                "The enrichment publication record contains a duplicate identity."
            )
        object.__setattr__(
            self,
            "publications",
            tuple(sorted(publications, key=lambda item: (item.set_code, item.event_format))),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "publications": [item.to_json() for item in self.publications],
            "schema_version": ENRICHMENT_PUBLICATIONS_SCHEMA_VERSION,
        }

    def to_bytes(self) -> bytes:
        return _json_bytes(self.to_json())

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> EnrichmentPublications:
        if not isinstance(value, Mapping):
            raise EnrichmentPublicationError("The enrichment publication record must be an object.")
        _keys(value, _RECORD_KEYS, "enrichment publication record")
        schema_version = _required(value, "schema_version")
        if schema_version != ENRICHMENT_PUBLICATIONS_SCHEMA_VERSION:
            raise EnrichmentPublicationError(
                f"Unsupported enrichment publication record schema {schema_version}; "
                f"expected {ENRICHMENT_PUBLICATIONS_SCHEMA_VERSION}."
            )
        publications = _required(value, "publications")
        if not isinstance(publications, list):
            raise EnrichmentPublicationError(
                "The enrichment publication record publications must be an array."
            )
        return cls(
            publications=tuple(EnrichmentPublication.from_json(item) for item in publications)
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> EnrichmentPublications:
        try:
            value = json.loads(payload.decode("utf-8"))
            return cls.from_json(value)
        except EnrichmentPublicationError as error:
            raise EnrichmentPublicationError(READ_ERROR) from error
        except (
            AttributeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise EnrichmentPublicationError(READ_ERROR) from error

    def select(self, *, set_code: str, event_format: str) -> EnrichmentPublication | None:
        """Select only an exact normalized set and format identity, if present."""

        normalized_set = _identifier(set_code, "set_code").casefold()
        normalized_format = _identifier(event_format, "event_format").casefold()
        return next(
            (
                item
                for item in self.publications
                if item.set_code == normalized_set and item.event_format == normalized_format
            ),
            None,
        )

    def merged(self, publication: EnrichmentPublication) -> EnrichmentPublications:
        """Replace the publication with the same identity and keep the rest."""

        identity = (publication.set_code, publication.event_format)
        retained = tuple(
            item for item in self.publications if (item.set_code, item.event_format) != identity
        )
        return EnrichmentPublications(publications=(*retained, publication))


def load_enrichment_publications(*, profiles_dir: PathInput) -> EnrichmentPublications:
    """Load the durable publication record, or an empty record when it is absent."""

    try:
        path = _path(value=profiles_dir, field_name="profiles_dir")
        payload = (path / ENRICHMENT_PUBLICATIONS_FILE_NAME).read_bytes()
    except FileNotFoundError:
        return EnrichmentPublications()
    except (OSError, TypeError, ValueError) as error:
        raise EnrichmentPublicationError(READ_ERROR) from error
    return EnrichmentPublications.from_bytes(payload)


def publish_enrichment_publications(
    *, profiles_dir: PathInput, record: EnrichmentPublications
) -> Path:
    """Publish the canonical publication record, reusing identical bytes."""

    if not isinstance(record, EnrichmentPublications):
        raise EnrichmentPublicationError("record must be an EnrichmentPublications.")
    output = _path(value=profiles_dir, field_name="profiles_dir") / ENRICHMENT_PUBLICATIONS_FILE_NAME
    try:
        payload = record.to_bytes()
        if _existing_bytes(path=output) == payload:
            return output
        _atomic_write(path=output, payload=payload)
    except EnrichmentPublicationError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise EnrichmentPublicationError(WRITE_ERROR) from error
    return output


def record_enrichment_publication(
    *, profiles_dir: PathInput, publication: EnrichmentPublication
) -> Path:
    """Merge one publication into the durable record and publish it."""

    record = load_enrichment_publications(profiles_dir=profiles_dir).merged(publication)
    return publish_enrichment_publications(profiles_dir=profiles_dir, record=record)


__all__ = [
    "ENRICHMENT_PUBLICATIONS_FILE_NAME",
    "ENRICHMENT_PUBLICATIONS_SCHEMA_VERSION",
    "READ_ERROR",
    "WRITE_ERROR",
    "EnrichmentPublication",
    "EnrichmentPublicationError",
    "EnrichmentPublications",
    "load_enrichment_publications",
    "publish_enrichment_publications",
    "record_enrichment_publication",
]

