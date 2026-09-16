from __future__ import annotations

import json
from pathlib import Path

import pytest

from draftomen.enrichment_inventory import (
    ARTIFACT_ERROR,
    EnrichmentInventoryError,
    inventory_enrichment_runs,
    select_confirmed_artifact,
)


RUNS_DIRECTORY = "enrichment-runs"
CONFIRMED_RUN_ID = "9574d202eef14943"
PENDING_RUN_ID = "da5336b1f2ceac32"
CREATED_AT = "2026-08-30T09:00:00+00:00"
REVIEWED_AT = "2026-09-01T10:00:00+00:00"
OLDER_REVIEWED_AT = "2026-08-31T10:00:00+00:00"
CONFIRMED_ARTIFACT_SHA256 = "a" * 64
PENDING_ARTIFACT_SHA256 = "b" * 64
OLDER_CONFIRMED_ARTIFACT_SHA256 = "c" * 64
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

