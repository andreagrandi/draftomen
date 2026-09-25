from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from draftomen.augmented_manifest import AugmentedManifest, AugmentedManifestEntry
from draftomen.profile_manifest import ProfileManifest, ProfileManifestArtifact
from draftomen.set_profile import ProfileMaturity
from draftomen.sets_manifest import (
    SetsManifest,
    SetsManifestError,
    build_sets_manifest,
    write_sets_manifest,
)
from tests.augmented_artifacts import augmented_artifact
from tests.test_augmented_publication import _card_data_bytes

_REPOSITORY_PUBLIC_DIR = Path(__file__).resolve().parents[1] / "website" / "public"


def _profile_artifact(*, set_code: str, event_format: str, maturity: ProfileMaturity) -> ProfileManifestArtifact:
    digest = hashlib.sha256(f"{set_code}-{event_format}".encode()).hexdigest()
    return ProfileManifestArtifact(
        set_code=set_code,
        event_format=event_format,
        set_profile_schema_version=4,
        profile_version="1.0",
        generated_at="2026-09-20T12:00:00+00:00",
        url=f"https://www.draftomen.com/profiles/objects/{digest}.json.gz",
        gzip_bytes=100,
        profile_bytes=400,
        gzip_sha256=digest,
        profile_sha256=digest,
        maturity=maturity,
    )


def _public_dir(tmp_path: Path) -> Path:
    public_dir = tmp_path / "public"
    card_data_dir = public_dir / "card-data"
    card_data_dir.mkdir(parents=True)
    (card_data_dir / "tst.json.gz").write_bytes(_card_data_bytes("tst"))
    (card_data_dir / "oth.json.gz").write_bytes(_card_data_bytes("oth"))
    profiles_dir = public_dir / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "manifest.json").write_bytes(
        ProfileManifest(
            artifacts=(
                _profile_artifact(
                    set_code="tst",
                    event_format="QuickDraft",
                    maturity=ProfileMaturity.EARLY,
                ),
                _profile_artifact(
                    set_code="tst",
                    event_format="PremierDraft",
                    maturity=ProfileMaturity.METADATA_ONLY,
                ),
            ),
            published_at="2026-09-20T12:00:00+00:00",
        ).to_bytes()
    )
    artifact = augmented_artifact("tst")
    payload = artifact.to_gzip_bytes()
    augmented_dir = public_dir / "augmented"
    augmented_dir.mkdir()
    (augmented_dir / "manifest.json").write_bytes(
        AugmentedManifest(
            entries=(
                AugmentedManifestEntry(
                    set_code="tst",
                    artifact_bytes=len(artifact.to_bytes()),
                    artifact_schema_version=artifact.schema_version,
                    artifact_sha256=hashlib.sha256(payload).hexdigest(),
                    compatibility=artifact.compatibility,
                    source=artifact.source,
                    metrics=artifact.evaluation,
                ),
            ),
            published_at="2026-09-20T12:00:00+00:00",
        ).to_bytes()
    )
    return public_dir


def test_sets_manifest_records_card_data_profiles_and_augmented_model_for_a_set(
    tmp_path: Path,
) -> None:
    public_dir = _public_dir(tmp_path)

    manifest = build_sets_manifest(public_dir=public_dir)

    card_data = (public_dir / "card-data" / "tst.json.gz").read_bytes()
    assert manifest.to_json()["sets"]["tst"] == {
        "name": "Controlled Test Set",
        "card_data": {
            "url": "https://www.draftomen.com/card-data/tst.json.gz",
            "bytes": len(card_data),
            "sha256": hashlib.sha256(card_data).hexdigest(),
        },
        "profiles": {
            "premierdraft": {"maturity": "metadata-only"},
            "quickdraft": {"maturity": "early"},
        },
        "augmented": {"metrics": augmented_artifact("tst").evaluation.to_json()},
    }


def test_set_without_profiles_or_augmented_model_is_listed_with_empty_entries(
    tmp_path: Path,
) -> None:
    manifest = build_sets_manifest(public_dir=_public_dir(tmp_path))

    other = manifest.select(set_code="OTH")
    assert other is not None
    assert other.profiles == {}
    assert other.augmented is None


def test_recorded_checksums_match_every_published_card_data_file(tmp_path: Path) -> None:
    public_dir = _public_dir(tmp_path)

    manifest = build_sets_manifest(public_dir=public_dir)

    for entry in manifest.entries:
        payload = (public_dir / "card-data" / f"{entry.set_code}.json.gz").read_bytes()
        assert entry.card_data.sha256 == hashlib.sha256(payload).hexdigest()
        assert entry.card_data.bytes == len(payload)


def test_generating_twice_from_the_same_inputs_produces_identical_bytes(
    tmp_path: Path,
) -> None:
    public_dir = _public_dir(tmp_path)

    path = write_sets_manifest(public_dir=public_dir)
    first = path.read_bytes()
    first_inode = path.stat().st_ino
    write_sets_manifest(public_dir=public_dir)

    assert path.read_bytes() == first
    assert path.stat().st_ino == first_inode
    assert build_sets_manifest(public_dir=public_dir).to_bytes() == first
    assert SetsManifest.from_bytes(first).to_bytes() == first


def test_card_data_named_for_another_set_is_rejected(tmp_path: Path) -> None:
    public_dir = _public_dir(tmp_path)
    (public_dir / "card-data" / "bad.json.gz").write_bytes(_card_data_bytes("tst"))

    with pytest.raises(SetsManifestError, match="bad.json.gz"):
        build_sets_manifest(public_dir=public_dir)


def test_malformed_manifest_bytes_are_rejected() -> None:
    with pytest.raises(SetsManifestError):
        SetsManifest.from_bytes(b'{"schema_version": 1, "sets": {"tst": {}}}')
    with pytest.raises(SetsManifestError):
        SetsManifest.from_bytes(b'{"schema_version": 2, "sets": {}}')


def test_published_sets_manifest_matches_the_published_website_data() -> None:
    published = (_REPOSITORY_PUBLIC_DIR / "sets" / "manifest.json").read_bytes()

    manifest = build_sets_manifest(public_dir=_REPOSITORY_PUBLIC_DIR)

    assert published == manifest.to_bytes()
    assert {entry.set_code for entry in manifest.entries} == {
        path.name.removesuffix(".json.gz")
        for path in (_REPOSITORY_PUBLIC_DIR / "card-data").glob("*.json.gz")
    }

