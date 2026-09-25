from __future__ import annotations

from dataclasses import FrozenInstanceError
import gzip
import hashlib
import json
from pathlib import Path

import pytest

import draftomen.augmented_manifest as augmented_manifest
import draftomen.augmented_publication as publication
from draftomen.augmented_artifact import (
    AUGMENTED_ARTIFACT_COMPATIBILITY,
    AugmentedArtifact,
)
from draftomen.augmented_manifest import (
    AugmentedManifest,
    AugmentedManifestEntry,
    AugmentedManifestError,
    AugmentedManifestSchemaError,
)
from draftomen.augmented_training import AugmentedTrainingResult
from draftomen.augmented_training_data import AugmentedTrainingSource
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_generation import generate_set_profile
from draftomen.profile_manifest import ProfileManifest, ProfileManifestArtifact
from draftomen.public_dump import PublicDumpManifest
from draftomen.set_card_data import SetCardData
from draftomen.set_profile import SetProfile
from draftomen.sets_manifest import SetsManifest
from tests.augmented_artifacts import augmented_artifact
import tests.test_profile_generation as generation_fixture


_SET_CODE = "tst"
_RETRIEVED_AT = "2026-09-20T12:00:00+00:00"
_SOURCE_URL = (
    "https://17lands-public.s3.amazonaws.com/analysis_data/draft_data/"
    "draft_data_public.TST.PremierDraft.csv.gz"
)


def _card_data_bytes(set_code: str = _SET_CODE) -> bytes:
    card = CardInfo(
        grp_id=1,
        name="Controlled Draft Card",
        colors=("U",),
        mana_value=2.0,
        rarity="common",
        types=("Creature",),
        type_line="Creature — Wizard",
        set_code=set_code.upper(),
        arena_id=1,
        oracle_id="controlled-card-id",
    )
    artifact = SetCardData.from_card_database(
        CardDatabase(cards={1: card}),
        set_code=set_code,
        set_name="Controlled Test Set",
    )
    return artifact.to_gzip_bytes()


def _training_report() -> dict[str, object]:
    return {
        "evaluation": {
            "basic_do": {"top_1": 0.25, "mean_reciprocal_rank": 0.5},
            "basic_plus_augmented": {
                "top_1": 0.5,
                "mean_reciprocal_rank": 0.75,
            },
        }
    }


def _install_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    artifact: AugmentedArtifact | None = None,
    report: dict[str, object] | None = None,
) -> tuple[Path, Path, Path, list[str]]:
    public_dir = tmp_path / "website" / "public"
    card_data_dir = public_dir / "card-data"
    augmented_dir = public_dir / "augmented"
    cache_dir = tmp_path / "private-cache"
    events: list[str] = []
    result_artifact = augmented_artifact(_SET_CODE) if artifact is None else artifact
    result = AugmentedTrainingResult(
        artifact=result_artifact,
        report=_training_report() if report is None else report,
    )

    def acquire(
        *, set_code: str, cache, timeout_seconds: int, adapter
    ) -> AugmentedTrainingSource:
        assert events == ["profile-check"]
        assert set_code == "TST"
        assert timeout_seconds == 19
        assert cache.root == cache_dir
        assert adapter.fetch_public_drafts is publication._fetch_public_drafts_with_progress
        assert adapter.timeout_seconds == 19
        events.append("acquire")
        cache.root.mkdir(parents=True, exist_ok=True)
        private_dump = cache.root / "draft-data.csv.gz"
        csv_bytes = b"expansion,event_type,draft_id\nTST,PremierDraft,controlled-1\n"
        payload = gzip.compress(csv_bytes, mtime=0)
        private_dump.write_bytes(payload)
        return AugmentedTrainingSource(
            path=private_dump,
            url=_SOURCE_URL,
            sha256=hashlib.sha256(payload).hexdigest(),
            retrieved_at=_RETRIEVED_AT,
            attribution="17Lands public datasets",
            license="CC BY 4.0",
            event_type="PremierDraft",
        )

    def resolve(*, set_code: str, output_dir: Path, timeout_seconds: int) -> Path:
        assert events == ["profile-check", "acquire", "profile"]
        assert set_code == "TST"
        assert timeout_seconds == 19
        target = output_dir / f"{set_code.casefold()}.json.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(_card_data_bytes(set_code))
        events.append("card-data")
        return target

    def require_profile(*, set_code: str, profiles_dir: Path) -> None:
        assert events == []
        assert set_code == "TST"
        events.append("profile-check")

    def load_profile(
        *, set_code: str, event_format: str, profiles_dir: Path
    ) -> tuple[SetProfile, str]:
        assert events == ["profile-check", "acquire"]
        assert set_code == "TST"
        assert event_format == "PremierDraft"
        events.append("profile")
        return (
            SetProfile.generic(set_code=set_code, event_format=event_format),
            "published:early",
        )

    def train(*, set_code: str, source, card_database, set_profile):
        assert events == ["profile-check", "acquire", "profile", "card-data"]
        assert set_code == "TST"
        assert source.event_type == "PremierDraft"
        assert set_profile.set_code == _SET_CODE
        assert set_profile.event_format == "premierdraft"
        assert tuple(card_database.cards) == (1,)
        events.append("train")
        return result

    monkeypatch.setattr(publication, "acquire_augmented_training_source", acquire)
    monkeypatch.setattr(publication, "resolve_set_card_data", resolve)
    monkeypatch.setattr(publication, "_require_rated_published_profile", require_profile)
    monkeypatch.setattr(publication, "_load_published_profile", load_profile)
    monkeypatch.setattr(publication, "train_and_gate_augmented_set", train)
    return public_dir, card_data_dir, augmented_dir, events


def _build(
    *,
    card_data_dir: Path,
    augmented_dir: Path,
    cache_dir: Path,
) -> publication.AugmentedBuildResult:
    return publication.build_augmented_set(
        set_code="TST",
        card_data_dir=card_data_dir,
        augmented_dir=augmented_dir,
        cache_dir=cache_dir,
        timeout_seconds=19,
    )


def _entry_for(artifact: AugmentedArtifact, payload: bytes) -> AugmentedManifestEntry:
    return AugmentedManifestEntry(
        set_code=artifact.set_code,
        artifact_bytes=len(artifact.to_bytes()),
        artifact_schema_version=artifact.schema_version,
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
        compatibility=artifact.compatibility,
        source=artifact.source,
        metrics=artifact.evaluation,
    )


def _seed_previous_publications(augmented_dir: Path) -> tuple[dict[Path, bytes], AugmentedManifestEntry]:
    objects_dir = augmented_dir / "objects"
    objects_dir.mkdir(parents=True)
    prior_entries: list[AugmentedManifestEntry] = []
    prior_files: dict[Path, bytes] = {}
    for code, multiplier in (("oth", 1.0), (_SET_CODE, 0.25)):
        artifact = augmented_artifact(code, multiplier=multiplier)
        payload = artifact.to_gzip_bytes()
        entry = _entry_for(artifact, payload)
        object_path = objects_dir / f"{entry.artifact_sha256}.json.gz"
        object_path.write_bytes(payload)
        prior_entries.append(entry)
        prior_files[object_path] = payload

    manifest = AugmentedManifest(
        entries=tuple(prior_entries),
        published_at="2026-09-21T00:00:00+00:00",
    )
    manifest_path = augmented_dir / "manifest.json"
    # Existing manifests need not use the writer's canonical whitespace. Keep this
    # exact representation to exercise no-churn preservation on repeated builds.
    manifest_bytes = b"\n" + json.dumps(
        manifest.to_json(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    prior_files[manifest_path] = manifest_bytes
    return prior_files, next(item for item in prior_entries if item.set_code == "oth")


def _assert_prior_bytes_unchanged(prior_files: dict[Path, bytes]) -> None:
    for path, payload in prior_files.items():
        assert path.read_bytes() == payload


def _assert_no_temporary_files(root: Path) -> None:
    assert not [
        path
        for path in root.rglob("*")
        if path.is_file() and path.name.startswith(".")
    ]


def test_publishes_client_readable_object_and_preserves_other_set_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir, card_data_dir, augmented_dir, events = _install_workflow(
        monkeypatch,
        tmp_path,
    )
    prior_files, other_entry = _seed_previous_publications(augmented_dir)
    artifact = augmented_artifact(_SET_CODE)
    expected_gzip = artifact.to_gzip_bytes()
    expected_digest = hashlib.sha256(expected_gzip).hexdigest()

    result = _build(
        card_data_dir=card_data_dir,
        augmented_dir=augmented_dir,
        cache_dir=tmp_path / "private-cache",
    )

    assert events == ["profile-check", "acquire", "profile", "card-data", "train"]
    assert result.training.report["evaluation"] == _training_report()["evaluation"]
    assert result.profile_source == "published:early"
    assert result.card_data_path == card_data_dir / f"{_SET_CODE}.json.gz"
    assert result.object_path == augmented_dir / "objects" / f"{expected_digest}.json.gz"
    assert result.manifest_path == augmented_dir / "manifest.json"
    assert result.object_path.read_bytes() == expected_gzip
    assert result.card_data_path.read_bytes() == _card_data_bytes()
    assert result.card_data_path.is_relative_to(public_dir)

    manifest_bytes = result.manifest_path.read_bytes()
    manifest = AugmentedManifest.from_bytes(manifest_bytes)
    published_entry = manifest.select(set_code=_SET_CODE)
    assert published_entry is not None
    assert published_entry.artifact_sha256 == expected_digest
    assert published_entry.artifact_bytes == len(artifact.to_bytes())
    assert manifest.select(set_code="oth") == other_entry
    assert manifest.select(set_code="tst") == _entry_for(artifact, expected_gzip)
    validated = AugmentedArtifact.from_gzip_bytes(
        result.object_path.read_bytes(),
        expected_set_code=_SET_CODE,
        expected_compatibility=AUGMENTED_ARTIFACT_COMPATIBILITY,
        expected_sha256=published_entry.artifact_sha256,
        expected_byte_size=published_entry.artifact_bytes,
    )
    assert validated == artifact

    sets_manifest = SetsManifest.from_bytes((public_dir / "sets" / "manifest.json").read_bytes())
    published_set = sets_manifest.select(set_code=_SET_CODE)
    assert published_set is not None
    assert published_set.augmented == {"metrics": artifact.evaluation.to_json()}

    prior_object = augmented_dir / "objects" / f"{other_entry.artifact_sha256}.json.gz"
    assert prior_object.read_bytes() == prior_files[prior_object]
    assert (tmp_path / "private-cache" / "draft-data.csv.gz").exists()
    public_files = {
        path.relative_to(public_dir).as_posix()
        for path in public_dir.rglob("*")
        if path.is_file()
    }
    assert public_files == {
        f"card-data/{_SET_CODE}.json.gz",
        "sets/manifest.json",
        "augmented/manifest.json",
        f"augmented/objects/{expected_digest}.json.gz",
        *(f"augmented/objects/{path.name}" for path in prior_files if path.parent.name == "objects"),
    }

    manifest_inode = result.manifest_path.stat().st_ino
    object_inode = result.object_path.stat().st_ino
    events.clear()
    repeated = _build(
        card_data_dir=card_data_dir,
        augmented_dir=augmented_dir,
        cache_dir=tmp_path / "private-cache",
    )
    assert repeated.object_path == result.object_path
    assert repeated.manifest_path == result.manifest_path
    assert result.manifest_path.read_bytes() == manifest_bytes
    assert result.manifest_path.stat().st_ino == manifest_inode
    assert result.object_path.stat().st_ino == object_inode
    assert AugmentedManifest.from_bytes(result.manifest_path.read_bytes()).published_at == manifest.published_at
    with pytest.raises(FrozenInstanceError):
        result.object_path = None  # type: ignore[misc]
    _assert_no_temporary_files(public_dir)


def test_failed_gate_returns_no_publication_paths_and_preserves_prior_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failed = AugmentedTrainingResult(artifact=None, report=_training_report())
    public_dir, card_data_dir, augmented_dir, _ = _install_workflow(
        monkeypatch,
        tmp_path,
        artifact=None,
    )
    # The helper's default is passing; replace only the trainer response for this case.
    monkeypatch.setattr(
        publication,
        "train_and_gate_augmented_set",
        lambda **_kwargs: failed,
    )
    prior_files, _ = _seed_previous_publications(augmented_dir)

    result = _build(
        card_data_dir=card_data_dir,
        augmented_dir=augmented_dir,
        cache_dir=tmp_path / "private-cache",
    )

    assert result.training is failed
    assert result.object_path is None
    assert result.manifest_path is None
    _assert_prior_bytes_unchanged(prior_files)
    assert not (augmented_dir / "objects" / f"{hashlib.sha256(augmented_artifact(_SET_CODE).to_gzip_bytes()).hexdigest()}.json.gz").exists()
    assert (card_data_dir / f"{_SET_CODE}.json.gz").exists()
    _assert_no_temporary_files(public_dir)


def test_acquisition_failure_precedes_any_public_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "website" / "public"
    cache_dir = tmp_path / "private-cache"

    def fail_acquisition(**_kwargs):
        raise ValueError("controlled source unavailable")

    def unexpected_card_data(**_kwargs):
        raise AssertionError("card data must not be resolved before source acquisition")

    monkeypatch.setattr(
        publication, "_require_rated_published_profile", lambda **_kwargs: None
    )
    monkeypatch.setattr(publication, "acquire_augmented_training_source", fail_acquisition)
    monkeypatch.setattr(publication, "resolve_set_card_data", unexpected_card_data)

    with pytest.raises(ValueError, match="controlled source unavailable"):
        publication.build_augmented_set(
            set_code="TST",
            card_data_dir=public_dir / "card-data",
            augmented_dir=public_dir / "augmented",
            cache_dir=cache_dir,
            timeout_seconds=19,
        )

    assert not public_dir.exists()


def test_corrupt_existing_manifest_is_not_replaced_or_extended(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir, card_data_dir, augmented_dir, _ = _install_workflow(
        monkeypatch,
        tmp_path,
    )
    prior_files, _ = _seed_previous_publications(augmented_dir)
    manifest_path = augmented_dir / "manifest.json"
    malformed = b"{not valid JSON"
    manifest_path.write_bytes(malformed)
    prior_files[manifest_path] = malformed

    with pytest.raises(AugmentedManifestSchemaError):
        _build(
            card_data_dir=card_data_dir,
            augmented_dir=augmented_dir,
            cache_dir=tmp_path / "private-cache",
        )

    _assert_prior_bytes_unchanged(prior_files)
    expected_digest = hashlib.sha256(augmented_artifact(_SET_CODE).to_gzip_bytes()).hexdigest()
    assert not (augmented_dir / "objects" / f"{expected_digest}.json.gz").exists()
    _assert_no_temporary_files(public_dir)


def test_conflicting_digest_name_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir, card_data_dir, augmented_dir, _ = _install_workflow(
        monkeypatch,
        tmp_path,
    )
    prior_files, _ = _seed_previous_publications(augmented_dir)
    artifact = augmented_artifact(_SET_CODE)
    digest = hashlib.sha256(artifact.to_gzip_bytes()).hexdigest()
    conflict_path = augmented_dir / "objects" / f"{digest}.json.gz"
    conflict_bytes = b"different bytes at a content-addressed name"
    conflict_path.write_bytes(conflict_bytes)

    with pytest.raises(publication.AugmentedPublicationError, match="different bytes"):
        _build(
            card_data_dir=card_data_dir,
            augmented_dir=augmented_dir,
            cache_dir=tmp_path / "private-cache",
        )

    _assert_prior_bytes_unchanged(prior_files)
    assert conflict_path.read_bytes() == conflict_bytes
    _assert_no_temporary_files(public_dir)


def test_object_installation_failure_preserves_all_manifest_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir, card_data_dir, augmented_dir, _ = _install_workflow(
        monkeypatch,
        tmp_path,
    )
    prior_files, _ = _seed_previous_publications(augmented_dir)

    def fail_link(*_args, **_kwargs):
        raise OSError("controlled hard-link failure")

    monkeypatch.setattr(publication.os, "link", fail_link)
    with pytest.raises(OSError, match="controlled hard-link failure"):
        _build(
            card_data_dir=card_data_dir,
            augmented_dir=augmented_dir,
            cache_dir=tmp_path / "private-cache",
        )

    _assert_prior_bytes_unchanged(prior_files)
    expected_digest = hashlib.sha256(augmented_artifact(_SET_CODE).to_gzip_bytes()).hexdigest()
    assert not (augmented_dir / "objects" / f"{expected_digest}.json.gz").exists()
    _assert_no_temporary_files(public_dir)


def test_manifest_replace_failure_keeps_prior_references_and_cleans_temporary_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir, card_data_dir, augmented_dir, _ = _install_workflow(
        monkeypatch,
        tmp_path,
    )
    prior_files, _ = _seed_previous_publications(augmented_dir)
    artifact = augmented_artifact(_SET_CODE)
    payload = artifact.to_gzip_bytes()
    digest = hashlib.sha256(payload).hexdigest()

    def fail_replace(*_args, **_kwargs):
        raise OSError("controlled manifest replace failure")

    monkeypatch.setattr(augmented_manifest.os, "replace", fail_replace)
    with pytest.raises(AugmentedManifestError, match="Could not write augmented manifest"):
        _build(
            card_data_dir=card_data_dir,
            augmented_dir=augmented_dir,
            cache_dir=tmp_path / "private-cache",
        )

    _assert_prior_bytes_unchanged(prior_files)
    assert (augmented_dir / "objects" / f"{digest}.json.gz").read_bytes() == payload
    _assert_no_temporary_files(public_dir)


def _publish_profile(
    profiles_dir: Path, *, stage: str = "early"
) -> tuple[SetProfile, ProfileManifestArtifact]:
    generation = generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage=stage,
        card_database=generation_fixture._database(),
        source_manifest=PublicDumpManifest(
            sources=(generation_fixture._source("no-data.csv"),)
        ),
        generated_at=generation_fixture.GENERATED_AT,
        ratings=generation_fixture._ratings() if stage != "metadata" else None,
        config=generation_fixture._config(),
    )
    report = generation.report
    artifact = ProfileManifestArtifact(
        set_code=report.set_code,
        event_format=report.event_format,
        set_profile_schema_version=report.set_profile_schema_version,
        profile_version=generation.profile.profile_version,
        generated_at=report.generated_at,
        url=f"https://www.draftomen.com/profiles/objects/{report.gzip_sha256}.json.gz",
        gzip_bytes=report.gzip_bytes,
        profile_bytes=report.profile_bytes,
        gzip_sha256=report.gzip_sha256,
        profile_sha256=report.profile_sha256,
        maturity=generation.profile.maturity,
    )
    objects = profiles_dir / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    (objects / f"{report.gzip_sha256}.json.gz").write_bytes(generation.gzip_bytes)
    (profiles_dir / "manifest.json").write_bytes(
        ProfileManifest(artifacts=(artifact,), published_at=_RETRIEVED_AT).to_bytes()
    )
    return generation.profile, artifact


def test_published_profile_is_loaded_with_its_maturity_as_the_source(
    tmp_path: Path,
) -> None:
    profiles_dir = tmp_path / "profiles"
    expected, _ = _publish_profile(profiles_dir)

    profile, source = publication._load_published_profile(
        set_code="TST", event_format="QuickDraft", profiles_dir=profiles_dir
    )

    assert profile.to_bytes() == expected.to_bytes()
    assert source == "published:early"


def test_missing_published_format_stops_with_a_named_error(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    _publish_profile(profiles_dir)

    with pytest.raises(publication.AugmentedPublicationError, match="No published TST TradDraft"):
        publication._load_published_profile(
            set_code="TST", event_format="TradDraft", profiles_dir=profiles_dir
        )


def test_metadata_only_profile_is_rejected_before_any_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profiles_dir = tmp_path / "profiles"
    _publish_profile(profiles_dir, stage="metadata")

    def unexpected_acquisition(**_kwargs):
        raise AssertionError("the dump must not be downloaded without rated profiles")

    monkeypatch.setattr(
        publication, "acquire_augmented_training_source", unexpected_acquisition
    )

    with pytest.raises(publication.AugmentedPublicationError, match="card ratings"):
        publication.build_augmented_set(
            set_code="TST",
            card_data_dir=tmp_path / "card-data",
            augmented_dir=tmp_path / "augmented",
            cache_dir=tmp_path / "cache",
            profiles_dir=profiles_dir,
        )
    with pytest.raises(publication.AugmentedPublicationError, match="card ratings"):
        publication._load_published_profile(
            set_code="TST", event_format="QuickDraft", profiles_dir=profiles_dir
        )


def test_set_without_published_profiles_stops_before_any_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profiles_dir = tmp_path / "profiles"
    _publish_profile(profiles_dir)

    def unexpected_acquisition(**_kwargs):
        raise AssertionError("the dump must not be downloaded without a profile")

    monkeypatch.setattr(
        publication, "acquire_augmented_training_source", unexpected_acquisition
    )

    with pytest.raises(publication.AugmentedPublicationError, match="No published OTH"):
        publication.build_augmented_set(
            set_code="OTH",
            card_data_dir=tmp_path / "card-data",
            augmented_dir=tmp_path / "augmented",
            cache_dir=tmp_path / "cache",
            profiles_dir=profiles_dir,
        )


def test_published_profile_with_a_wrong_checksum_is_rejected(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    _, artifact = _publish_profile(profiles_dir)
    object_path = profiles_dir / "objects" / f"{artifact.gzip_sha256}.json.gz"
    object_path.write_bytes(gzip.compress(b"{}", mtime=0))

    with pytest.raises(publication.AugmentedPublicationError, match="checksum"):
        publication._load_published_profile(
            set_code="TST", event_format="QuickDraft", profiles_dir=profiles_dir
        )
