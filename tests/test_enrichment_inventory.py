from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from draftomen.enrichment_inventory import (
    ARTIFACT_ERROR,
    PUBLICATION_ERROR,
    EnrichmentInventoryError,
    EnrichmentPublicationState,
    inventory_enrichment_runs,
    published_enrichment_states,
    select_confirmed_artifact,
)
from draftomen.enrichment_publications import (
    READ_ERROR,
    EnrichmentPublication,
    EnrichmentPublicationError,
    EnrichmentPublications,
    publish_enrichment_publications,
)
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestArtifact,
    dump_profile_manifest,
)
from draftomen.profile_publication import PROFILE_BASE_URL


RUNS_DIRECTORY = "enrichment-runs"
PROFILES_DIRECTORY = "profiles"
OBJECTS_DIRECTORY = "objects"
MANIFEST_FILE_NAME = "manifest.json"
CONFIRMED_RUN_ID = "9574d202eef14943"
PENDING_RUN_ID = "da5336b1f2ceac32"
CREATED_AT = "2026-08-30T09:00:00+00:00"
REVIEWED_AT = "2026-09-01T10:00:00+00:00"
OLDER_REVIEWED_AT = "2026-08-31T10:00:00+00:00"
CONFIRMED_ARTIFACT_SHA256 = "a" * 64
PENDING_ARTIFACT_SHA256 = "b" * 64
OLDER_CONFIRMED_ARTIFACT_SHA256 = "c" * 64
RECORD_ARTIFACT_SHA256 = "d" * 64
ORPHAN_ARTIFACT_SHA256 = "e" * 64
RECORD_PROFILE_GZIP_SHA256 = "1" * 64
ORPHAN_PROFILE_GZIP_SHA256 = "2" * 64
STALE_PROFILE_GZIP_SHA256 = "3" * 64
MISSING_SET_ERROR = "No confirmed enrichment artifact is available for the requested set."
MISSING_ARTIFACT_ERROR = "The requested enrichment artifact is not available or not confirmed."


def _write_artifact(
    *,
    store_dir: Path,
    set_code: str,
    run_id: str,
    sha256: str,
    review_state: str = "pending",
    reviewed_at: str | None = None,
    created_at: str | None = CREATED_AT,
    relationships: int = 0,
    confirmed: int = 0,
) -> Path:
    """Write one synthetic saved artifact into an on-disk store layout."""
    payload = {
        "set_code": set_code,
        "created_at": created_at,
        "review": {"state": review_state, "reviewed_at": reviewed_at},
        "relationships": [{"finding_id": f"finding-{index}"} for index in range(relationships)],
        "confirmed_relationship_ids": [f"finding-{index}" for index in range(confirmed)],
    }
    artifacts_dir = store_dir / RUNS_DIRECTORY / set_code / run_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"{sha256}.json"
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return path


def _record_entry(
    *,
    set_code: str,
    event_format: str,
    artifact_sha256: str,
    profile_gzip_sha256: str,
) -> EnrichmentPublication:
    """Build one durable record entry for a synthetic publication."""
    return EnrichmentPublication(
        set_code=set_code,
        event_format=event_format,
        artifact_sha256=artifact_sha256,
        run_id=CONFIRMED_RUN_ID,
        reviewed_at=REVIEWED_AT,
        published_at=REVIEWED_AT,
        profile_gzip_sha256=profile_gzip_sha256,
    )


def _write_record_publication(
    *,
    profiles_dir: Path,
    set_code: str,
    event_format: str,
    artifact_sha256: str,
    profile_gzip_sha256: str,
) -> None:
    """Record one enriched publication as a committed entry of the durable record."""
    publish_enrichment_publications(
        profiles_dir=profiles_dir,
        record=EnrichmentPublications(
            publications=(
                _record_entry(
                    set_code=set_code,
                    event_format=event_format,
                    artifact_sha256=artifact_sha256,
                    profile_gzip_sha256=profile_gzip_sha256,
                ),
            )
        ),
    )


def _objects_directory(*, profiles_dir: Path) -> Path:
    """Return the profile objects directory of one profiles tree."""
    objects_dir = profiles_dir / OBJECTS_DIRECTORY
    objects_dir.mkdir(parents=True, exist_ok=True)
    return objects_dir


def _write_object_payload(*, profiles_dir: Path, name: str, payload: bytes) -> Path:
    """Write one raw file into the profile objects directory."""
    path = _objects_directory(profiles_dir=profiles_dir) / name
    path.write_bytes(payload)
    return path


def _write_profile_object(*, profiles_dir: Path, profile_gzip_sha256: str, document: object) -> Path:
    """Write one gzip-compressed published profile object under its content address."""
    return _write_object_payload(
        profiles_dir=profiles_dir,
        name=f"{profile_gzip_sha256}.json.gz",
        payload=gzip.compress(json.dumps(document, sort_keys=True).encode("utf-8")),
    )


def _published_document(
    *,
    set_code: str = "hob",
    event_format: str = "quickdraft",
    artifact_sha256: str = RECORD_ARTIFACT_SHA256,
    enhancement_status: str = "enhanced",
) -> dict[str, object]:
    """Return one minimal published profile document with its enrichment provenance."""
    return {
        "set_code": set_code,
        "format": event_format,
        "enhancement_status": enhancement_status,
        "enhancement": {"artifact_sha256": artifact_sha256},
    }


def _manifest_artifact(
    *, set_code: str, event_format: str, profile_gzip_sha256: str
) -> ProfileManifestArtifact:
    """Build one manifest entry selecting a published object digest."""
    return ProfileManifestArtifact(
        set_code=set_code,
        event_format=event_format,
        set_profile_schema_version=2,
        profile_version="1.0",
        generated_at=REVIEWED_AT,
        url=f"{PROFILE_BASE_URL}{profile_gzip_sha256}.json.gz",
        gzip_bytes=1,
        profile_bytes=1,
        gzip_sha256=profile_gzip_sha256,
        profile_sha256="0" * 64,
        maturity="early",
    )


def _write_manifest(
    *, profiles_dir: Path, artifacts: tuple[ProfileManifestArtifact, ...]
) -> Path:
    """Write one canonical profile manifest selecting the supplied identities."""
    return dump_profile_manifest(
        ProfileManifest(artifacts=artifacts, published_at=REVIEWED_AT),
        profiles_dir / MANIFEST_FILE_NAME,
    )


def test_inventory_lists_runs_and_artifacts_in_canonical_order(tmp_path: Path) -> None:
    pending_path = _write_artifact(
        store_dir=tmp_path,
        set_code="lci",
        run_id=PENDING_RUN_ID,
        sha256=PENDING_ARTIFACT_SHA256,
    )
    older_path = _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id=CONFIRMED_RUN_ID,
        sha256=OLDER_CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=OLDER_REVIEWED_AT,
        relationships=1,
        confirmed=1,
    )
    confirmed_path = _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id=CONFIRMED_RUN_ID,
        sha256=CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=REVIEWED_AT,
        relationships=3,
        confirmed=2,
    )
    (confirmed_path.parent / "notes.txt").write_text("ignored", encoding="utf-8")

    runs = inventory_enrichment_runs(store_dir=tmp_path)

    assert len(runs) == 2
    assert [(run.set_code, run.run_id) for run in runs] == [
        ("hob", CONFIRMED_RUN_ID),
        ("lci", PENDING_RUN_ID),
    ]
    assert [run.path for run in runs] == [
        tmp_path / RUNS_DIRECTORY / "hob" / CONFIRMED_RUN_ID,
        tmp_path / RUNS_DIRECTORY / "lci" / PENDING_RUN_ID,
    ]
    hob, lci = runs
    assert len(hob.artifacts) == 2
    assert [artifact.sha256 for artifact in hob.artifacts] == [
        CONFIRMED_ARTIFACT_SHA256,
        OLDER_CONFIRMED_ARTIFACT_SHA256,
    ]
    confirmed = hob.artifacts[0]
    assert confirmed.set_code == "hob"
    assert confirmed.run_id == CONFIRMED_RUN_ID
    assert confirmed.path == confirmed_path
    assert confirmed.created_at == CREATED_AT
    assert confirmed.reviewed_at == REVIEWED_AT
    assert confirmed.review_state == "confirmed"
    assert confirmed.relationship_count == 3
    assert confirmed.confirmed_relationship_count == 2
    older = hob.artifacts[1]
    assert older.path == older_path
    assert older.reviewed_at == OLDER_REVIEWED_AT
    assert older.review_state == "confirmed"
    assert (older.relationship_count, older.confirmed_relationship_count) == (1, 1)
    pending = lci.artifacts[0]
    assert pending.path == pending_path
    assert pending.created_at == CREATED_AT
    assert pending.reviewed_at is None
    assert pending.review_state == "pending"
    assert (pending.relationship_count, pending.confirmed_relationship_count) == (0, 0)


def test_inventory_returns_nothing_without_an_enrichment_runs_directory(tmp_path: Path) -> None:
    (tmp_path / "some-other-directory").mkdir()

    assert inventory_enrichment_runs(store_dir=tmp_path) == ()
    assert inventory_enrichment_runs(store_dir=tmp_path / "absent") == ()


def test_inventory_raises_when_the_runs_directory_cannot_be_read(tmp_path: Path) -> None:
    runs_dir = tmp_path / RUNS_DIRECTORY
    runs_dir.mkdir()
    runs_dir.chmod(0o000)
    try:
        with pytest.raises(EnrichmentInventoryError) as raised:
            inventory_enrichment_runs(store_dir=tmp_path)
    finally:
        runs_dir.chmod(0o700)

    assert str(raised.value) == ARTIFACT_ERROR


def test_inventory_keeps_runs_that_saved_no_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / RUNS_DIRECTORY / "hob" / CONFIRMED_RUN_ID
    run_dir.mkdir(parents=True)
    (tmp_path / RUNS_DIRECTORY / "stray-file").write_text("ignored", encoding="utf-8")
    (tmp_path / RUNS_DIRECTORY / "hob" / "stray-file").write_text("ignored", encoding="utf-8")

    runs = inventory_enrichment_runs(store_dir=tmp_path)

    assert len(runs) == 1
    assert (runs[0].set_code, runs[0].run_id) == ("hob", CONFIRMED_RUN_ID)
    assert runs[0].path == run_dir
    assert runs[0].artifacts == ()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"{not json", id="invalid-json"),
        pytest.param(b"\x80\x81", id="invalid-utf-8"),
        pytest.param(b"[]", id="not-an-object"),
    ],
)
def test_inventory_raises_for_unreadable_artifact_files(tmp_path: Path, payload: bytes) -> None:
    artifacts_dir = tmp_path / RUNS_DIRECTORY / "hob" / CONFIRMED_RUN_ID / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / f"{CONFIRMED_ARTIFACT_SHA256}.json").write_bytes(payload)

    with pytest.raises(EnrichmentInventoryError) as raised:
        inventory_enrichment_runs(store_dir=tmp_path)

    assert str(raised.value) == ARTIFACT_ERROR


def test_inventory_raises_for_a_missing_artifact_file(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / RUNS_DIRECTORY / "hob" / CONFIRMED_RUN_ID / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / f"{CONFIRMED_ARTIFACT_SHA256}.json").symlink_to(artifacts_dir / "absent.json")

    with pytest.raises(EnrichmentInventoryError) as raised:
        inventory_enrichment_runs(store_dir=tmp_path)

    assert str(raised.value) == ARTIFACT_ERROR


def test_select_confirmed_artifact_returns_the_newest_review(tmp_path: Path) -> None:
    _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id="z-run",
        sha256=OLDER_CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=OLDER_REVIEWED_AT,
    )
    newer_path = _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id="a-run",
        sha256=CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=REVIEWED_AT,
    )
    _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id="b-run",
        sha256=PENDING_ARTIFACT_SHA256,
    )

    selected = select_confirmed_artifact(store_dir=tmp_path, set_code="HOB")

    assert selected.path == newer_path
    assert selected.sha256 == CONFIRMED_ARTIFACT_SHA256


def test_select_confirmed_artifact_honours_the_artifact_pin(tmp_path: Path) -> None:
    _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id=CONFIRMED_RUN_ID,
        sha256=CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=REVIEWED_AT,
    )
    older_path = _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id=CONFIRMED_RUN_ID,
        sha256=OLDER_CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=OLDER_REVIEWED_AT,
    )

    selected = select_confirmed_artifact(
        store_dir=tmp_path,
        set_code="hob",
        artifact_sha256=OLDER_CONFIRMED_ARTIFACT_SHA256,
    )

    assert selected.path == older_path
    assert selected.reviewed_at == OLDER_REVIEWED_AT


def test_select_confirmed_artifact_filters_by_run_identity(tmp_path: Path) -> None:
    older_path = _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id="a-run",
        sha256=OLDER_CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=OLDER_REVIEWED_AT,
    )
    newer_path = _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id="z-run",
        sha256=CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=REVIEWED_AT,
    )

    from_older_run = select_confirmed_artifact(store_dir=tmp_path, set_code="hob", run_id="a-run")
    from_newer_run = select_confirmed_artifact(store_dir=tmp_path, set_code="hob", run_id="z-run")

    assert from_older_run.path == older_path
    assert from_newer_run.path == newer_path

    with pytest.raises(EnrichmentInventoryError) as missing_run:
        select_confirmed_artifact(store_dir=tmp_path, set_code="hob", run_id="absent-run")
    assert str(missing_run.value) == MISSING_SET_ERROR

    with pytest.raises(EnrichmentInventoryError) as outside_run:
        select_confirmed_artifact(
            store_dir=tmp_path,
            set_code="hob",
            artifact_sha256=CONFIRMED_ARTIFACT_SHA256,
            run_id="a-run",
        )
    assert str(outside_run.value) == MISSING_ARTIFACT_ERROR


def test_select_confirmed_artifact_reports_missing_confirmed_candidates(tmp_path: Path) -> None:
    _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id=CONFIRMED_RUN_ID,
        sha256=CONFIRMED_ARTIFACT_SHA256,
        review_state="confirmed",
        reviewed_at=REVIEWED_AT,
    )
    _write_artifact(
        store_dir=tmp_path,
        set_code="hob",
        run_id=PENDING_RUN_ID,
        sha256=PENDING_ARTIFACT_SHA256,
    )

    with pytest.raises(EnrichmentInventoryError) as unknown_set:
        select_confirmed_artifact(store_dir=tmp_path, set_code="lci")
    assert str(unknown_set.value) == MISSING_SET_ERROR

    with pytest.raises(EnrichmentInventoryError) as unknown_artifact:
        select_confirmed_artifact(store_dir=tmp_path, set_code="hob", artifact_sha256="f" * 64)
    assert str(unknown_artifact.value) == MISSING_ARTIFACT_ERROR

    with pytest.raises(EnrichmentInventoryError) as pending_artifact:
        select_confirmed_artifact(
            store_dir=tmp_path,
            set_code="hob",
            artifact_sha256=PENDING_ARTIFACT_SHA256,
        )
    assert str(pending_artifact.value) == MISSING_ARTIFACT_ERROR


def test_published_states_include_record_publications(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    _write_record_publication(
        profiles_dir=profiles_dir,
        set_code="hob",
        event_format="quickdraft",
        artifact_sha256=RECORD_ARTIFACT_SHA256,
        profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
    )
    _write_manifest(
        profiles_dir=profiles_dir,
        artifacts=(
            _manifest_artifact(
                set_code="hob",
                event_format="quickdraft",
                profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            ),
        ),
    )

    states = published_enrichment_states(profiles_dir=profiles_dir)

    assert states == (
        EnrichmentPublicationState(
            set_code="hob",
            event_format="quickdraft",
            artifact_sha256=RECORD_ARTIFACT_SHA256,
            profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            referenced=True,
            source="record",
        ),
    )


def test_published_states_ignore_candidates(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    publish_enrichment_publications(
        profiles_dir=profiles_dir,
        record=EnrichmentPublications(
            publications=(
                _record_entry(
                    set_code="hob",
                    event_format="quickdraft",
                    artifact_sha256=RECORD_ARTIFACT_SHA256,
                    profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
                ),
            ),
            candidates=(
                _record_entry(
                    set_code="hob",
                    event_format="quickdraft",
                    artifact_sha256=RECORD_ARTIFACT_SHA256,
                    profile_gzip_sha256=STALE_PROFILE_GZIP_SHA256,
                ),
                _record_entry(
                    set_code="lci",
                    event_format="quickdraft",
                    artifact_sha256=ORPHAN_ARTIFACT_SHA256,
                    profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
                ),
            ),
        ),
    )
    _write_manifest(
        profiles_dir=profiles_dir,
        artifacts=(
            _manifest_artifact(
                set_code="hob",
                event_format="quickdraft",
                profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            ),
        ),
    )

    states = published_enrichment_states(profiles_dir=profiles_dir)

    assert states == (
        EnrichmentPublicationState(
            set_code="hob",
            event_format="quickdraft",
            artifact_sha256=RECORD_ARTIFACT_SHA256,
            profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            referenced=True,
            source="record",
        ),
    )


def test_published_states_resolve_object_publications_against_the_manifest(
    tmp_path: Path,
) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
        document=_published_document(artifact_sha256=RECORD_ARTIFACT_SHA256),
    )
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
        document=_published_document(
            set_code="lci",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
        ),
    )
    _write_manifest(
        profiles_dir=profiles_dir,
        artifacts=(
            _manifest_artifact(
                set_code="hob",
                event_format="quickdraft",
                profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            ),
            # The lci entry no longer selects the object its artifact produced.
            _manifest_artifact(
                set_code="lci",
                event_format="quickdraft",
                profile_gzip_sha256=STALE_PROFILE_GZIP_SHA256,
            ),
        ),
    )

    states = published_enrichment_states(profiles_dir=profiles_dir)

    assert states == (
        EnrichmentPublicationState(
            set_code="hob",
            event_format="quickdraft",
            artifact_sha256=RECORD_ARTIFACT_SHA256,
            profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            referenced=True,
            source="object",
        ),
        EnrichmentPublicationState(
            set_code="lci",
            event_format="quickdraft",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
            profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
            referenced=False,
            source="object",
        ),
    )


def test_published_states_merge_the_record_and_object_sources(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    _write_record_publication(
        profiles_dir=profiles_dir,
        set_code="hob",
        event_format="quickdraft",
        artifact_sha256=RECORD_ARTIFACT_SHA256,
        profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
    )
    # The object of the recorded publication repeats it under its own identity.
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
        document=_published_document(
            set_code="HOB",
            event_format="QuickDraft",
            artifact_sha256=RECORD_ARTIFACT_SHA256,
        ),
    )
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
        document=_published_document(
            set_code="lci",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
        ),
    )
    _write_manifest(
        profiles_dir=profiles_dir,
        artifacts=(
            _manifest_artifact(
                set_code="hob",
                event_format="quickdraft",
                profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            ),
        ),
    )

    states = published_enrichment_states(profiles_dir=profiles_dir)

    assert states == (
        EnrichmentPublicationState(
            set_code="hob",
            event_format="quickdraft",
            artifact_sha256=RECORD_ARTIFACT_SHA256,
            profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            referenced=True,
            source="record",
        ),
        EnrichmentPublicationState(
            set_code="lci",
            event_format="quickdraft",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
            profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
            referenced=False,
            source="object",
        ),
    )


def test_published_states_are_unreferenced_without_a_manifest(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
        document=_published_document(
            set_code="lci",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
        ),
    )

    states = published_enrichment_states(profiles_dir=profiles_dir)

    assert states == (
        EnrichmentPublicationState(
            set_code="lci",
            event_format="quickdraft",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
            profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
            referenced=False,
            source="object",
        ),
    )


def test_published_states_skip_object_files_that_are_not_enriched_profiles(
    tmp_path: Path,
) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
        document=_published_document(
            set_code="lci",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
        ),
    )
    _write_object_payload(
        profiles_dir=profiles_dir,
        name="not-gzip.json.gz",
        payload=b"{not a gzip stream",
    )
    _write_object_payload(
        profiles_dir=profiles_dir,
        name=f"{STALE_PROFILE_GZIP_SHA256}.json.gz",
        payload=gzip.compress(b"\x80\x81"),
    )
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256="4" * 64,
        document=[{"set_code": "hob"}],
    )
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256="5" * 64,
        document=_published_document(enhancement_status="plain"),
    )
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256="6" * 64,
        document={"set_code": "hob", "format": "quickdraft", "enhancement_status": "enhanced"},
    )
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256="7" * 64,
        document={
            "set_code": "hob",
            "format": "quickdraft",
            "enhancement_status": "enhanced",
            "enhancement": "artifact-sha256",
        },
    )
    _write_object_payload(
        profiles_dir=profiles_dir,
        name="notes.txt",
        payload=b"ignored",
    )

    states = published_enrichment_states(profiles_dir=profiles_dir)

    assert states == (
        EnrichmentPublicationState(
            set_code="lci",
            event_format="quickdraft",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
            profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
            referenced=False,
            source="object",
        ),
    )


def test_published_states_ignore_a_missing_objects_directory(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    _write_record_publication(
        profiles_dir=profiles_dir,
        set_code="hob",
        event_format="quickdraft",
        artifact_sha256=RECORD_ARTIFACT_SHA256,
        profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
    )

    states = published_enrichment_states(profiles_dir=profiles_dir)

    assert states == (
        EnrichmentPublicationState(
            set_code="hob",
            event_format="quickdraft",
            artifact_sha256=RECORD_ARTIFACT_SHA256,
            profile_gzip_sha256=RECORD_PROFILE_GZIP_SHA256,
            referenced=False,
            source="record",
        ),
    )


def test_published_states_raise_for_an_unreadable_objects_directory(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    objects_dir = _objects_directory(profiles_dir=profiles_dir)
    _write_profile_object(
        profiles_dir=profiles_dir,
        profile_gzip_sha256=ORPHAN_PROFILE_GZIP_SHA256,
        document=_published_document(
            set_code="lci",
            artifact_sha256=ORPHAN_ARTIFACT_SHA256,
        ),
    )
    objects_dir.chmod(0o000)
    try:
        with pytest.raises(EnrichmentInventoryError) as raised:
            published_enrichment_states(profiles_dir=profiles_dir)
    finally:
        objects_dir.chmod(0o700)

    assert str(raised.value) == PUBLICATION_ERROR


def test_published_states_raise_for_a_malformed_manifest(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    _objects_directory(profiles_dir=profiles_dir)
    (profiles_dir / MANIFEST_FILE_NAME).write_bytes(b"{not json")

    with pytest.raises(EnrichmentInventoryError) as raised:
        published_enrichment_states(profiles_dir=profiles_dir)

    assert str(raised.value) == PUBLICATION_ERROR


def test_published_states_raise_for_a_directory_named_manifest(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    _objects_directory(profiles_dir=profiles_dir)
    (profiles_dir / MANIFEST_FILE_NAME).mkdir()

    with pytest.raises(EnrichmentInventoryError) as raised:
        published_enrichment_states(profiles_dir=profiles_dir)

    assert str(raised.value) == PUBLICATION_ERROR


def test_published_states_raise_for_a_malformed_record(tmp_path: Path) -> None:
    profiles_dir = tmp_path / PROFILES_DIRECTORY
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "enrichment-publications.json").write_bytes(b"{not json")

    with pytest.raises(EnrichmentPublicationError) as raised:
        published_enrichment_states(profiles_dir=profiles_dir)

    assert str(raised.value) == READ_ERROR

