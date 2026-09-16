"""Local inventory of saved set-enrichment runs and their artifacts.

The inventory is a read-only view of one on-disk set-enrichment store.  It
decodes saved artifacts defensively instead of validating them, so a run written
by a different build is still visible, and it selects the confirmed artifact
that offline profile re-publication compiles from.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, TypeAlias


ENRICHMENT_RUNS_DIRECTORY = "enrichment-runs"
ENRICHMENT_ARTIFACTS_DIRECTORY = "artifacts"
ARTIFACT_ERROR = "Could not read a saved enrichment artifact."

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
    "EnrichmentArtifactSummary",
    "EnrichmentInventoryError",
    "EnrichmentRunSummary",
    "inventory_enrichment_runs",
    "select_confirmed_artifact",
]

