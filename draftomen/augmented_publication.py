"""Build and atomically publish one validated augmented-set model."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any

from draftomen.augmented_artifact import (
    AUGMENTED_ARTIFACT_COMPATIBILITY,
    AugmentedArtifact,
)
from draftomen.augmented_manifest import (
    AugmentedManifest,
    AugmentedManifestEntry,
    dump_augmented_manifest,
)
from draftomen.augmented_public_data import acquire_augmented_training_source
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
from draftomen.profile_refresh_execution import DEFAULT_PROFILE_REFRESH_CACHE_POLICY
from draftomen.set_card_data import SetCardData
from draftomen.set_profile import safe_load_set_profile
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


def build_augmented_set(
    *,
    set_code: str,
    card_data_dir: Path = Path("website/public/card-data"),
    augmented_dir: Path = Path("website/public/augmented"),
    cache_dir: Path | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    scryfall_bulk_file: Path = DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE,
) -> AugmentedBuildResult:
    """Acquire, gate, and publish one set's compressed augmented artifact."""

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

    card_data_path = resolve_set_card_data(
        set_code=set_code,
        output_dir=card_data_dir,
        timeout_seconds=timeout_seconds,
    )
    card_data = SetCardData.from_gzip_bytes(
        card_data_path.read_bytes(),
        expected_set_code=set_code.casefold(),
    )
    profile_result = safe_load_set_profile(
        set_code=set_code,
        event_format=source.event_type,
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
        set_profile=profile_result.profile,
    )
    if training.artifact is None:
        return AugmentedBuildResult(
            training=training,
            card_data_path=card_data_path,
            profile_source=profile_result.source,
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
        profile_source=profile_result.source,
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
