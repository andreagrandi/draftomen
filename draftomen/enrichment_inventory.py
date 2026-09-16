"""Local inventory of saved set-enrichment runs and their artifacts.

The inventory is a read-only view of one on-disk set-enrichment store.  It
decodes saved artifacts defensively instead of validating them, so a run written
by a different build is still visible, and it selects the confirmed artifact
that offline profile re-publication compiles from.  It also resolves where each
confirmed artifact was published by reading the durable publication record and
the published profile objects of a repository profiles tree.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import gzip
import json
import os
from pathlib import Path
from typing import Any, TypeAlias
import zlib

from draftomen.enrichment_publications import load_enrichment_publications
from draftomen.profile_manifest import ProfileManifestError, load_profile_manifest


ENRICHMENT_RUNS_DIRECTORY = "enrichment-runs"
ENRICHMENT_ARTIFACTS_DIRECTORY = "artifacts"
ARTIFACT_ERROR = "Could not read a saved enrichment artifact."
PROFILE_OBJECTS_DIRECTORY = "objects"
PROFILE_MANIFEST_FILE_NAME = "manifest.json"
PROFILE_OBJECT_SUFFIX = ".json.gz"
PUBLICATION_ERROR = "Could not read the published enrichment profiles."

PathInput: TypeAlias = str | os.PathLike[str]


class EnrichmentInventoryError(RuntimeError):
    """Raised when a local enrichment store cannot be read."""


@dataclass(frozen=True, slots=True)
class EnrichmentArtifactSummary:
    """One saved enrichment artifact with its run identity and review state."""

    set_code: str
    run_id: str
    sha256: str
    path: Path
    created_at: str | None
    reviewed_at: str | None
    review_state: str
    relationship_count: int
    confirmed_relationship_count: int


@dataclass(frozen=True, slots=True)
class EnrichmentRunSummary:
    """One local enrichment run directory and its saved artifacts."""

    set_code: str
    run_id: str
    path: Path
    artifacts: tuple[EnrichmentArtifactSummary, ...]


@dataclass(frozen=True, slots=True)
class EnrichmentPublicationState:
    """One identity in which an enrichment artifact was published."""

    set_code: str
    event_format: str
    artifact_sha256: str
    profile_gzip_sha256: str
    referenced: bool
    source: str  # "record" or "object"


def inventory_enrichment_runs(*, store_dir: PathInput) -> tuple[EnrichmentRunSummary, ...]:
    """Return every local enrichment run and artifact under one store directory."""
    runs_dir = Path(store_dir).expanduser() / ENRICHMENT_RUNS_DIRECTORY
    try:
        runs_dir.stat()
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise EnrichmentInventoryError(ARTIFACT_ERROR) from error
    runs = [
        _run_summary(run_dir=run_dir, set_code=set_dir.name)
        for set_dir in _subdirectories(directory=runs_dir)
        for run_dir in _subdirectories(directory=set_dir)
    ]
    return tuple(sorted(runs, key=lambda run: (run.set_code, run.run_id)))


def select_confirmed_artifact(
    *,
    store_dir: PathInput,
    set_code: str,
    artifact_sha256: str | None = None,
    run_id: str | None = None,
) -> EnrichmentArtifactSummary:
    """Select the confirmed artifact to re-publish from one local store."""
    expected_set_code = set_code.casefold()
    candidates = [
        artifact
        for run in inventory_enrichment_runs(store_dir=store_dir)
        if run_id is None or run.run_id == run_id
        for artifact in run.artifacts
        if artifact.review_state == "confirmed"
        and artifact.set_code.casefold() == expected_set_code
    ]
    if artifact_sha256 is not None:
        for artifact in candidates:
            if artifact.sha256 == artifact_sha256:
                return artifact
        raise EnrichmentInventoryError(
            "The requested enrichment artifact is not available or not confirmed."
        )
    if not candidates:
        raise EnrichmentInventoryError(
            "No confirmed enrichment artifact is available for the requested set."
        )
    return max(candidates, key=lambda artifact: (artifact.reviewed_at or "", artifact.sha256))


def published_enrichment_states(
    *, profiles_dir: PathInput
) -> tuple[EnrichmentPublicationState, ...]:
    """Return every publication of an enrichment-backed profile and its manifest reference.

    A publication is described twice on disk: by the durable record written when
    a confirmed artifact was published, and by the content-addressed profile
    objects themselves, which are the only trace of a publication that predates
    the record.  A publication both sources describe is reported once, from the
    record, because the record names the artifact behind the published object.
    """
    directory = Path(profiles_dir).expanduser()
    manifest_digests = _manifest_digests(profiles_dir=directory)
    publications: dict[tuple[str, str, str], EnrichmentPublicationState] = {}
    for state in (
        *_record_states(profiles_dir=directory, manifest_digests=manifest_digests),
        *_object_states(profiles_dir=directory, manifest_digests=manifest_digests),
    ):
        publications.setdefault(_state_key(state), state)
    return tuple(sorted(publications.values(), key=_state_key))


def _manifest_digests(*, profiles_dir: Path) -> dict[tuple[str, str], str]:
    """Return the gzip digest the manifest currently selects per identity.

    A missing manifest references nothing; a path that exists but cannot be
    read or parsed is a failure of the profiles tree.
    """
    manifest_path = profiles_dir / PROFILE_MANIFEST_FILE_NAME
    try:
        manifest_path.stat()
        manifest = load_profile_manifest(manifest_path)
    except FileNotFoundError:
        return {}
    except (OSError, ProfileManifestError) as error:
        raise EnrichmentInventoryError(PUBLICATION_ERROR) from error
    return {
        (artifact.set_code, artifact.event_format): artifact.gzip_sha256
        for artifact in manifest.artifacts
    }


def _record_states(
    *,
    profiles_dir: Path,
    manifest_digests: Mapping[tuple[str, str], str],
) -> tuple[EnrichmentPublicationState, ...]:
    """Return one state per entry of the durable publication record."""
    record = load_enrichment_publications(profiles_dir=profiles_dir)
    return tuple(
        EnrichmentPublicationState(
            set_code=item.set_code,
            event_format=item.event_format,
            artifact_sha256=item.artifact_sha256,
            profile_gzip_sha256=item.profile_gzip_sha256,
            referenced=_referenced(
                manifest_digests=manifest_digests,
                set_code=item.set_code,
                event_format=item.event_format,
                profile_gzip_sha256=item.profile_gzip_sha256,
            ),
            source="record",
        )
        for item in record.publications
    )


def _object_states(
    *,
    profiles_dir: Path,
    manifest_digests: Mapping[tuple[str, str], str],
) -> tuple[EnrichmentPublicationState, ...]:
    """Return one state per enhanced profile object held under its content address."""
    states: list[EnrichmentPublicationState] = []
    for path in _object_files(directory=profiles_dir / PROFILE_OBJECTS_DIRECTORY):
        document = _published_profile(path=path)
        if document is None or document.get("enhancement_status") != "enhanced":
            continue
        enhancement = document.get("enhancement")
        if not isinstance(enhancement, Mapping):
            continue
        set_code = _text(document.get("set_code"))
        event_format = _text(document.get("format"))
        artifact_sha256 = _text(enhancement.get("artifact_sha256"))
        if not set_code or not event_format or not artifact_sha256:
            continue
        profile_gzip_sha256 = path.name.removesuffix(PROFILE_OBJECT_SUFFIX)
        normalized_set = set_code.casefold()
        normalized_format = event_format.casefold()
        states.append(
            EnrichmentPublicationState(
                set_code=normalized_set,
                event_format=normalized_format,
                artifact_sha256=artifact_sha256,
                profile_gzip_sha256=profile_gzip_sha256,
                referenced=_referenced(
                    manifest_digests=manifest_digests,
                    set_code=normalized_set,
                    event_format=normalized_format,
                    profile_gzip_sha256=profile_gzip_sha256,
                ),
                source="object",
            )
        )
    return tuple(states)


def _referenced(
    *,
    manifest_digests: Mapping[tuple[str, str], str],
    set_code: str,
    event_format: str,
    profile_gzip_sha256: str,
) -> bool:
    """Return True when the manifest selects this publication of one identity."""
    return manifest_digests.get((set_code, event_format)) == profile_gzip_sha256


def _state_key(state: EnrichmentPublicationState) -> tuple[str, str, str]:
    """Return the publication identity one resolved state is deduplicated by."""
    return (state.set_code, state.event_format, state.profile_gzip_sha256)


def _object_files(*, directory: Path) -> tuple[Path, ...]:
    """Return the profile object files of one objects directory in name order.

    A missing objects directory holds no objects; one that exists but cannot be
    listed is a failure of the profiles tree, not an empty history.
    """
    try:
        entries = tuple(
            entry
            for entry in directory.iterdir()
            if entry.name.endswith(PROFILE_OBJECT_SUFFIX) and not entry.is_dir()
        )
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise EnrichmentInventoryError(PUBLICATION_ERROR) from error
    return tuple(sorted(entries, key=lambda entry: entry.name))


def _published_profile(*, path: Path) -> Mapping[str, Any] | None:
    """Decode one published gzip profile object, or None when its content is unusable.

    An object that cannot be read is a failure of the profiles tree, not an
    absent publication; malformed gzip, text, or JSON is a legacy payload and
    is skipped.
    """
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise EnrichmentInventoryError(PUBLICATION_ERROR) from error
    try:
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
    except (EOFError, OSError, TypeError, UnicodeDecodeError, ValueError, zlib.error):
        return None
    return document if isinstance(document, Mapping) else None


def _subdirectories(*, directory: Path) -> tuple[Path, ...]:
    """Return the real subdirectories of one directory in name order."""
    try:
        entries = tuple(entry for entry in directory.iterdir() if entry.is_dir())
    except OSError as error:
        raise EnrichmentInventoryError(ARTIFACT_ERROR) from error
    return tuple(sorted(entries, key=lambda entry: entry.name))


def _run_summary(*, run_dir: Path, set_code: str) -> EnrichmentRunSummary:
    """Summarize one run directory and the artifacts it saved."""
    artifacts: tuple[EnrichmentArtifactSummary, ...] = ()
    artifacts_dir = run_dir / ENRICHMENT_ARTIFACTS_DIRECTORY
    if artifacts_dir.is_dir():
        try:
            paths = tuple(
                entry
                for entry in artifacts_dir.iterdir()
                if entry.suffix == ".json" and not entry.is_dir()
            )
        except OSError as error:
            raise EnrichmentInventoryError(ARTIFACT_ERROR) from error
        artifacts = tuple(
            sorted(
                (
                    _artifact_summary(path=path, run_id=run_dir.name, fallback_set_code=set_code)
                    for path in paths
                ),
                key=lambda artifact: artifact.sha256,
            )
        )
    return EnrichmentRunSummary(
        set_code=set_code,
        run_id=run_dir.name,
        path=run_dir,
        artifacts=artifacts,
    )


def _artifact_summary(
    *,
    path: Path,
    run_id: str,
    fallback_set_code: str,
) -> EnrichmentArtifactSummary:
    """Decode one saved artifact file defensively into a summary record."""
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnrichmentInventoryError(ARTIFACT_ERROR) from error
    if not isinstance(payload, dict):
        raise EnrichmentInventoryError(ARTIFACT_ERROR)
    review = payload.get("review")
    if not isinstance(review, dict):
        review = {}
    return EnrichmentArtifactSummary(
        # The artifact names its own set; the scan directory is only a fallback.
        set_code=_text(payload.get("set_code")) or fallback_set_code,
        run_id=run_id,
        sha256=path.stem,
        path=path,
        created_at=_text(payload.get("created_at")),
        reviewed_at=_text(review.get("reviewed_at")),
        review_state=_text(review.get("state")) or "unknown",
        relationship_count=_array_length(payload.get("relationships")),
        confirmed_relationship_count=_array_length(payload.get("confirmed_relationship_ids")),
    )


def _text(value: Any) -> str | None:
    """Return one optional JSON text field, or None when it is not text."""
    return value if isinstance(value, str) else None


def _array_length(value: Any) -> int:
    """Return the length of one optional JSON array field."""
    return len(value) if isinstance(value, list) else 0


__all__ = [
    "ARTIFACT_ERROR",
    "ENRICHMENT_ARTIFACTS_DIRECTORY",
    "ENRICHMENT_RUNS_DIRECTORY",
    "PROFILE_MANIFEST_FILE_NAME",
    "PROFILE_OBJECTS_DIRECTORY",
    "PROFILE_OBJECT_SUFFIX",
    "PUBLICATION_ERROR",
    "EnrichmentArtifactSummary",
    "EnrichmentInventoryError",
    "EnrichmentPublicationState",
    "EnrichmentRunSummary",
    "inventory_enrichment_runs",
    "published_enrichment_states",
    "select_confirmed_artifact",
]

