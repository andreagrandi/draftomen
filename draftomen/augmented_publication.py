"""Build and atomically publish one validated augmented-set model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import tempfile

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
from draftomen.carddb import HTTP_TIMEOUT_SECONDS
from draftomen.paths import app_data_dir
from draftomen.profile_input_cache import ProfileInputCache
from draftomen.profile_refresh_execution import DEFAULT_PROFILE_REFRESH_CACHE_POLICY
from draftomen.set_card_data import SetCardData
from draftomen.set_profile import safe_load_set_profile


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


def build_augmented_set(
    *,
    set_code: str,
    card_data_dir: Path = Path("website/public/card-data"),
    augmented_dir: Path = Path("website/public/augmented"),
    cache_dir: Path | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
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
    training = train_and_gate_augmented_set(
        set_code=set_code,
        source=source,
        card_database=card_data.to_card_database(),
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
