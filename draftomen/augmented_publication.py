"""Build and atomically publish one validated augmented-set model."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import zlib

from draftomen.augmented_artifact import (
    AUGMENTED_ARTIFACT_COMPATIBILITY,
    AugmentedArtifact,
)
from draftomen.augmented_manifest import (
    AugmentedManifest,
    AugmentedManifestEntry,
    dump_augmented_manifest,
)
from draftomen.augmented_public_data import (
    AugmentedPublicDataError,
    _normalized_set_code,
    acquire_augmented_training_source,
)
from draftomen.augmented_training import (
    AugmentedTrainingResult,
    train_and_gate_augmented_set,
)
from draftomen.card_data_export import resolve_set_card_data
from draftomen.carddb import (
    HTTP_TIMEOUT_SECONDS,
    CardDatabase,
    build_card_database_from_scryfall_cards,
    iter_scryfall_default_cards,
)
from draftomen.draftmancer import _unlisted_card_grp_id
from draftomen.paths import app_data_dir
from draftomen.profile_input_cache import ProfileInputCache
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestError,
    load_profile_manifest,
)
from draftomen.profile_refresh_execution import DEFAULT_PROFILE_REFRESH_CACHE_POLICY
from draftomen.set_card_data import SetCardData
from draftomen.set_profile import ProfileMaturity, SetProfile, SetProfileError
from draftomen.test_draft import DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE


@dataclass(frozen=True, slots=True)
class AugmentedBuildResult:
    """Return one set's aggregate gate result and published artifact paths."""

    training: AugmentedTrainingResult
    card_data_path: Path
    profile_source: str
    object_path: Path | None
    manifest_path: Path | None


class AugmentedPublicationError(RuntimeError):
    """Report an augmented publication that cannot be trusted or completed."""


def _dump_card_names(*, path: Path) -> tuple[str, ...]:
    """Return the card names in a draft dump's pack columns.
    Only the header line is read, so a large dump is not decompressed here.
    """

    try:
        with path.open(mode="rb") as probe:
            is_gzip = probe.read(2) == b"\x1f\x8b"
        opener = gzip.open if is_gzip else open
        with opener(path, mode="rt", encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle))
    except (OSError, EOFError, StopIteration, UnicodeError) as error:
        raise AugmentedPublicationError("Draft dump has no readable CSV header.") from error
    return tuple(
        value.removeprefix("pack_card_") for value in header if value.startswith("pack_card_")
    )


def _with_bonus_sheet_cards(
    *,
    card_database: CardDatabase,
    card_names: Iterable[str],
    bulk_file: Path,
) -> CardDatabase:
    """Add dump cards that the set card data does not list, such as DFT Special Guests.
    Their metadata comes from the local Scryfall bulk file, preferring an Arena printing.
    """

    listed: set[str] = set()
    for card in card_database.cards.values():
        listed.add(card.name)
        listed.update(face.name for face in card.faces if face.name)
    missing = set(card_names) - listed
    if not missing:
        return card_database
    if not bulk_file.is_file():
        raise AugmentedPublicationError(
            f"Set card data does not list {sorted(missing)} and the local Scryfall "
            f"bulk file {bulk_file} is missing."
        )
    rows: dict[str, Mapping[str, Any]] = {}
    for row in iter_scryfall_default_cards(bulk_file=bulk_file):
        name = row.get("name")
        if name not in missing:
            continue
        previous = rows.get(name)
        if previous is None or (
            previous.get("arena_id") is None and row.get("arena_id") is not None
        ):
            rows[name] = row
    cards = dict(card_database.cards)
    for _, row in sorted(rows.items()):
        grp_id = row.get("arena_id")
        if not isinstance(grp_id, int) or isinstance(grp_id, bool) or grp_id in cards:
            grp_id = _unlisted_card_grp_id(scryfall_id=str(row.get("id", "")))
        cards.update(
            build_card_database_from_scryfall_cards(
                cards=({**row, "arena_id": grp_id},),
            ).cards
        )
    return CardDatabase(cards=cards, image_uris_by_name=card_database.image_uris_by_name)


def _published_profile_manifest(*, profiles_dir: Path) -> ProfileManifest:
    try:
        return load_profile_manifest(profiles_dir / "manifest.json")
    except ProfileManifestError as error:
        raise AugmentedPublicationError(
            "The published profile manifest could not be read."
        ) from error


def _require_rated_published_profile(*, set_code: str, profiles_dir: Path) -> None:
    """Stop before any download when the set has no published profile with ratings.
    The exact format is checked again once the dump's format is known.
    """

    normalized_set = set_code.casefold()
    manifest = _published_profile_manifest(profiles_dir=profiles_dir)
    if not any(
        artifact.set_code == normalized_set
        and artifact.maturity is not ProfileMaturity.METADATA_ONLY
        for artifact in manifest.artifacts
    ):
        raise AugmentedPublicationError(
            f"No published {set_code.upper()} profile with card ratings. Publish one "
            "before training, because the model is calibrated against its ratings."
        )


def _load_published_profile(
    *,
    set_code: str,
    event_format: str,
    profiles_dir: Path,
) -> tuple[SetProfile, str]:
    """Load the set profile the website publishes, with its checksums verified.
    A missing or ratings-free profile stops the build instead of training on a generic one.
    """

    label = f"{set_code.upper()} {event_format}"
    manifest = _published_profile_manifest(profiles_dir=profiles_dir)
    artifact = manifest.select(set_code=set_code, event_format=event_format)
    if artifact is None:
        raise AugmentedPublicationError(
            f"No published {label} profile. Publish one before training, because "
            "the model is calibrated against its ratings."
        )
    if artifact.maturity is ProfileMaturity.METADATA_ONLY:
        raise AugmentedPublicationError(
            f"The published {label} profile has no card ratings."
        )
    object_path = profiles_dir / "objects" / f"{artifact.gzip_sha256}.json.gz"
    try:
        payload = object_path.read_bytes()
        raw = gzip.decompress(payload)
    except (OSError, EOFError, zlib.error) as error:
        raise AugmentedPublicationError(
            f"The published {label} profile object could not be read."
        ) from error
    if (
        hashlib.sha256(payload).hexdigest() != artifact.gzip_sha256
        or hashlib.sha256(raw).hexdigest() != artifact.profile_sha256
    ):
        raise AugmentedPublicationError(
            f"The published {label} profile object does not match its checksum."
        )
    try:
        profile = SetProfile.from_json(json.loads(raw.decode("utf-8")))
    except (UnicodeError, ValueError, TypeError, SetProfileError) as error:
        raise AugmentedPublicationError(
            f"The published {label} profile object is invalid."
        ) from error
    if profile.set_code != artifact.set_code or profile.event_format != artifact.event_format:
        raise AugmentedPublicationError(
            f"The published {label} profile object names another set or format."
        )
    return profile, f"published:{profile.maturity.value}"


def build_augmented_set(
    *,
    set_code: str,
    card_data_dir: Path = Path("website/public/card-data"),
    augmented_dir: Path = Path("website/public/augmented"),
    cache_dir: Path | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    scryfall_bulk_file: Path = DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE,
    profiles_dir: Path = Path("website/public/profiles"),
) -> AugmentedBuildResult:
    """Acquire, gate, and publish one set's compressed augmented artifact."""

    # The dump download can take many minutes, so check the inputs first.
    try:
        _normalized_set_code(set_code)
    except AugmentedPublicDataError as error:
        raise AugmentedPublicationError(str(error)) from error
    _require_rated_published_profile(set_code=set_code, profiles_dir=profiles_dir)
    cache_root = (
        cache_dir
        if cache_dir is not None
        else app_data_dir() / "profile-input-cache"
    )
    source = acquire_augmented_training_source(
        set_code=set_code,
        cache=ProfileInputCache(
            cache_root,
            policy=DEFAULT_PROFILE_REFRESH_CACHE_POLICY,
        ),
        timeout_seconds=timeout_seconds,
    )
    # Training calibrates corrections to Basic DO, so it must use the ratings
    # users score with. Stop here, before the long training run, without them.
    set_profile, profile_source = _load_published_profile(
        set_code=set_code,
        event_format=source.event_type,
        profiles_dir=profiles_dir,
    )

    card_data_path = resolve_set_card_data(
        set_code=set_code,
        output_dir=card_data_dir,
        timeout_seconds=timeout_seconds,
    )
    card_data = SetCardData.from_gzip_bytes(
        card_data_path.read_bytes(),
        expected_set_code=set_code.casefold(),
    )
    card_database = _with_bonus_sheet_cards(
        card_database=card_data.to_card_database(),
        card_names=_dump_card_names(path=Path(source.path)),
        bulk_file=scryfall_bulk_file,
    )
    training = train_and_gate_augmented_set(
        set_code=set_code,
        source=source,
        card_database=card_database,
        set_profile=set_profile,
    )
    if training.artifact is None:
        return AugmentedBuildResult(
            training=training,
            card_data_path=card_data_path,
            profile_source=profile_source,
            object_path=None,
            manifest_path=None,
        )

    normalized_set = set_code.casefold()
    raw_bytes = training.artifact.to_bytes()
    gzip_bytes = training.artifact.to_gzip_bytes()
    gzip_digest = hashlib.sha256(gzip_bytes).hexdigest()
    artifact = AugmentedArtifact.from_gzip_bytes(
        gzip_bytes,
        expected_set_code=normalized_set,
        expected_compatibility=AUGMENTED_ARTIFACT_COMPATIBILITY,
        expected_sha256=gzip_digest,
        expected_byte_size=len(raw_bytes),
    )
    entry = AugmentedManifestEntry(
        set_code=normalized_set,
        artifact_bytes=len(raw_bytes),
        artifact_schema_version=artifact.schema_version,
        artifact_sha256=gzip_digest,
        compatibility=artifact.compatibility,
        source=artifact.source,
        metrics=artifact.evaluation,
    )

    output_dir = Path(augmented_dir)
    manifest_path = output_dir / "manifest.json"
    try:
        existing_bytes = manifest_path.read_bytes()
    except FileNotFoundError:
        existing_manifest = None
        existing_entry = None
    else:
        existing_manifest = AugmentedManifest.from_bytes(existing_bytes)
        existing_entry = existing_manifest.select(set_code=normalized_set)

    entries = (
        ()
        if existing_manifest is None
        else tuple(
            item
            for item in existing_manifest.entries
            if item.set_code != normalized_set
        )
    )
    manifest = AugmentedManifest(
        entries=(*entries, entry),
        published_at=datetime.now(UTC).isoformat(),
    )
    manifest = AugmentedManifest.from_bytes(manifest.to_bytes())

    object_path = output_dir / "objects" / f"{gzip_digest}.json.gz"
    _install_immutable_object(path=object_path, payload=gzip_bytes)

    if existing_entry != entry:
        dump_augmented_manifest(manifest, manifest_path)

    return AugmentedBuildResult(
        training=training,
        card_data_path=card_data_path,
        profile_source=profile_source,
        object_path=object_path,
        manifest_path=manifest_path,
    )


def _install_immutable_object(*, path: Path, payload: bytes) -> None:
    """Install a same-directory temporary object without replacing a name."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise AugmentedPublicationError(
                    "The content-addressed augmented object exists with different bytes."
                )
            return
        _fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry change where the platform supports it."""

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


__all__ = [
    "AugmentedBuildResult",
    "AugmentedPublicationError",
    "build_augmented_set",
]
