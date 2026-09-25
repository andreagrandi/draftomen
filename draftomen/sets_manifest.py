"""Build and read the published manifest that lists each set's hosted data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from draftomen.augmented_manifest import AugmentedManifest
from draftomen.profile_manifest import ProfileManifest
from draftomen.set_card_data import SetCardData

SETS_MANIFEST_SCHEMA_VERSION = 1
SETS_MANIFEST_URL = "https://www.draftomen.com/sets/manifest.json"
SITE_BASE_URL = "https://www.draftomen.com/"
SETS_MANIFEST_RELATIVE_PATH = Path("sets") / "manifest.json"

_SET_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CARD_DATA_SUFFIX = ".json.gz"


class SetsManifestError(ValueError):
    """Report a sets manifest or input that cannot be trusted."""


@dataclass(frozen=True, slots=True)
class CardDataRecord:
    """Locate one set's card-data file and pin its exact bytes."""

    url: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith("https://"):
            raise SetsManifestError("card_data url must be an https URL.")
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes <= 0:
            raise SetsManifestError("card_data bytes must be a positive integer.")
        if not isinstance(self.sha256, str) or _HEX64_RE.fullmatch(self.sha256) is None:
            raise SetsManifestError("card_data sha256 must be lowercase SHA-256.")

    def to_json(self) -> dict[str, object]:
        return {"bytes": self.bytes, "sha256": self.sha256, "url": self.url}


@dataclass(frozen=True, slots=True)
class SetEntry:
    """Record the card data, profile formats and augmented model of one set."""

    set_code: str
    name: str
    card_data: CardDataRecord
    profiles: Mapping[str, str]
    augmented: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        if not isinstance(self.set_code, str) or _SET_CODE_RE.fullmatch(self.set_code) is None:
            raise SetsManifestError(f"Invalid set code {self.set_code!r}.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise SetsManifestError(f"Set {self.set_code} has no name.")
        if not isinstance(self.card_data, CardDataRecord):
            raise SetsManifestError(f"Set {self.set_code} has invalid card data.")
        if not isinstance(self.profiles, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.profiles.items()
        ):
            raise SetsManifestError(f"Set {self.set_code} has invalid profiles.")
        object.__setattr__(self, "profiles", dict(sorted(self.profiles.items())))
        if self.augmented is not None and not isinstance(self.augmented, Mapping):
            raise SetsManifestError(f"Set {self.set_code} has an invalid augmented entry.")

    def to_json(self) -> dict[str, object]:
        return {
            "augmented": None if self.augmented is None else dict(self.augmented),
            "card_data": self.card_data.to_json(),
            "name": self.name,
            "profiles": {
                event_format: {"maturity": maturity}
                for event_format, maturity in self.profiles.items()
            },
        }


@dataclass(frozen=True, slots=True)
class SetsManifest:
    """List every published set in set-code order."""

    entries: tuple[SetEntry, ...]
    schema_version: int = SETS_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SETS_MANIFEST_SCHEMA_VERSION:
            raise SetsManifestError(
                f"Unsupported sets manifest schema {self.schema_version}; "
                f"expected {SETS_MANIFEST_SCHEMA_VERSION}."
            )
        entries = tuple(sorted(self.entries, key=lambda entry: entry.set_code))
        codes = [entry.set_code for entry in entries]
        if len(set(codes)) != len(codes):
            raise SetsManifestError("Sets manifest contains duplicate sets.")
        object.__setattr__(self, "entries", entries)

    def select(self, *, set_code: str) -> SetEntry | None:
        """Return one set's entry, matching the code case-insensitively."""

        normalized = set_code.strip().casefold()
        return next((entry for entry in self.entries if entry.set_code == normalized), None)

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sets": {entry.set_code: entry.to_json() for entry in self.entries},
        }

    def to_bytes(self) -> bytes:
        return (
            json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, payload: bytes) -> SetsManifest:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise SetsManifestError(f"Could not parse sets manifest: {error}.") from error
        if not isinstance(value, dict) or not isinstance(value.get("sets"), dict):
            raise SetsManifestError("Sets manifest must be an object with a sets object.")
        entries: list[SetEntry] = []
        try:
            for set_code, item in value["sets"].items():
                card_data = item["card_data"]
                entries.append(
                    SetEntry(
                        set_code=set_code,
                        name=item["name"],
                        card_data=CardDataRecord(
                            url=card_data["url"],
                            bytes=card_data["bytes"],
                            sha256=card_data["sha256"],
                        ),
                        profiles={
                            event_format: profile["maturity"]
                            for event_format, profile in item["profiles"].items()
                        },
                        augmented=item["augmented"],
                    )
                )
        except (KeyError, TypeError, AttributeError) as error:
            raise SetsManifestError(f"Sets manifest entry is malformed: {error!r}.") from error
        return cls(entries=tuple(entries), schema_version=value.get("schema_version"))


def build_sets_manifest(*, public_dir: Path, base_url: str = SITE_BASE_URL) -> SetsManifest:
    """Build the sets manifest from the card data, profile and augmented files under public_dir.
    Every set with a card-data file gets an entry; the output depends only on file contents.
    """

    card_data_dir = public_dir / "card-data"
    profiles = _load_optional(
        path=public_dir / "profiles" / "manifest.json",
        parse=ProfileManifest.from_bytes,
    )
    augmented = _load_optional(
        path=public_dir / "augmented" / "manifest.json",
        parse=AugmentedManifest.from_bytes,
    )
    entries: list[SetEntry] = []
    for path in sorted(card_data_dir.glob(f"*{_CARD_DATA_SUFFIX}")):
        set_code = path.name.removesuffix(_CARD_DATA_SUFFIX)
        payload = path.read_bytes()
        try:
            card_data = SetCardData.from_gzip_bytes(payload, expected_set_code=set_code)
        except ValueError as error:
            raise SetsManifestError(f"Card data {path.name} is invalid: {error}") from error
        profile_formats = (
            {}
            if profiles is None
            else {
                artifact.event_format: artifact.maturity.value
                for artifact in profiles.artifacts
                if artifact.set_code == set_code
            }
        )
        augmented_entry = None if augmented is None else augmented.select(set_code=set_code)
        entries.append(
            SetEntry(
                set_code=set_code,
                name=card_data.set_name,
                card_data=CardDataRecord(
                    url=f"{base_url}card-data/{path.name}",
                    bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                ),
                profiles=profile_formats,
                augmented=(
                    None
                    if augmented_entry is None
                    else {"metrics": augmented_entry.metrics.to_json()}
                ),
            )
        )
    return SetsManifest(entries=tuple(entries))


def write_sets_manifest(*, public_dir: Path) -> Path:
    """Regenerate public_dir/sets/manifest.json and replace it only when its bytes change."""

    payload = build_sets_manifest(public_dir=public_dir).to_bytes()
    path = public_dir / SETS_MANIFEST_RELATIVE_PATH
    try:
        if path.read_bytes() == payload:
            return path
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(payload)
    os.chmod(temporary.name, 0o644)
    os.replace(temporary.name, path)
    return path


def _load_optional(*, path: Path, parse: Any) -> Any:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return parse(payload)
    except ValueError as error:
        raise SetsManifestError(f"{path} could not be read: {error}") from error


__all__ = [
    "CardDataRecord",
    "SETS_MANIFEST_RELATIVE_PATH",
    "SETS_MANIFEST_URL",
    "SetEntry",
    "SetsManifest",
    "SetsManifestError",
    "build_sets_manifest",
    "write_sets_manifest",
]

