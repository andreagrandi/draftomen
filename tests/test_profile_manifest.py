from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from draftomen.profile_manifest import (
    PROFILE_MANIFEST_SCHEMA_VERSION,
    ProfileManifest,
    ProfileManifestArtifact,
    ProfileManifestError,
    ProfileManifestSchemaError,
    dump_profile_manifest,
    load_profile_manifest,
)
from draftomen.set_profile import ProfileMaturity, SET_PROFILE_SCHEMA_VERSION


PUBLISHED_AT = "2026-08-30T12:00:00+00:00"
GENERATED_AT = "2026-08-29T12:00:00+00:00"


def _artifact(
    set_code: str,
    event_format: str,
    *,
    url: str | None = None,
    schema_version: int = SET_PROFILE_SCHEMA_VERSION,
) -> ProfileManifestArtifact:
    return ProfileManifestArtifact(
        set_code=set_code,
        event_format=event_format,
        set_profile_schema_version=schema_version,
        profile_version="release-candidate",
        generated_at=GENERATED_AT,
        url=(
            f"https://cdn.example.test/profiles/{set_code.strip()}-{event_format.strip()}.json.gz"
            if url is None
            else url
        ),
        gzip_bytes=200,
        profile_bytes=100,
        gzip_sha256="a" * 64,
        profile_sha256="b" * 64,
        maturity=ProfileMaturity.EARLY,
    )


def test_manifest_round_trip_is_canonical_and_sorted(tmp_path: Path) -> None:
    manifest = ProfileManifest(
        artifacts=(_artifact("ZZZ", "PremierDraft"), _artifact("TST", "QuickDraft")),
        published_at=PUBLISHED_AT,
    )

    assert [(item.set_code, item.event_format) for item in manifest.artifacts] == [
        ("tst", "quickdraft"),
        ("zzz", "premierdraft"),
    ]
    payload = manifest.to_bytes()
    assert payload == manifest.to_bytes()
    assert ProfileManifest.from_bytes(payload) == manifest

    path = dump_profile_manifest(manifest, tmp_path / "profile-manifest.json")
    assert load_profile_manifest(path) == manifest
    assert path.read_bytes() == payload


def test_manifest_accepts_each_supported_set_profile_schema_version() -> None:
    for schema_version in (1, 2):
        artifact = _artifact("TST", "QuickDraft", schema_version=schema_version)
        restored = ProfileManifestArtifact.from_json(artifact.to_json())
        assert restored.set_profile_schema_version == schema_version

def test_selection_requires_exact_normalized_set_and_format() -> None:
    artifact = _artifact("TST", "QuickDraft")
    manifest = ProfileManifest(artifacts=(artifact,), published_at=PUBLISHED_AT)

    assert manifest.select(set_code=" tst ", event_format="QUICKDRAFT") == artifact
    assert manifest.select(set_code="TST", event_format="PremierDraft") is None


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example.test/profile.json.gz",
        "https:///profile.json.gz",
        "https://user:pass@cdn.example.test/profile.json.gz",
        "https://cdn.example.test:444/profile.json.gz",
        "https://cdn.example.test:/profile.json.gz",
        "https://cdn.example.test:not-a-port/profile.json.gz",
        "https://cdn.example.test/profile.json.gz#fragment",
        "https://cdn.example.test/profile.json.gz#",
        "https://cdn.example.test/profile.json.gz with-space",
        "https://cdn.example.test/profile.json.gz\x00",
        " https://cdn.example.test/profile.json.gz",
        "https://[::1",
    ],
)
def test_artifact_urls_match_runtime_https_policy(url: str) -> None:
    with pytest.raises(ProfileManifestSchemaError):
        _artifact("TST", "QuickDraft", url=url)


def test_artifact_urls_allow_explicit_https_443() -> None:
    artifact = _artifact(
        "TST",
        "QuickDraft",
        url="https://cdn.example.test:443/profile.json.gz",
    )

    assert artifact.url.endswith(":443/profile.json.gz")


def test_duplicate_normalized_identities_are_rejected() -> None:
    with pytest.raises(ProfileManifestError, match="duplicate"):
        ProfileManifest(
            artifacts=(_artifact("TST", "QuickDraft"), _artifact(" tst ", "quickdraft")),
            published_at=PUBLISHED_AT,
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("manifest_schema", "Unsupported profile manifest schema"),
        ("profile_schema", "Unsupported set-profile schema"),
        ("missing", "Missing required profile manifest field"),
        ("extra", "unsupported fields"),
        ("http", "absolute HTTPS URL"),
        ("generic", "cannot have generic maturity"),
        ("hash", "SHA-256"),
    ],
)
def test_invalid_and_future_manifest_fields_fail_closed(change: str, message: str) -> None:
    value = ProfileManifest(
        artifacts=(_artifact("TST", "QuickDraft"),),
        published_at=PUBLISHED_AT,
    ).to_json()
    artifact = value["artifacts"][0]
    assert isinstance(artifact, dict)
    if change == "manifest_schema":
        value["schema_version"] = PROFILE_MANIFEST_SCHEMA_VERSION + 1
    elif change == "profile_schema":
        artifact["set_profile_schema_version"] = SET_PROFILE_SCHEMA_VERSION + 1
    elif change == "missing":
        del artifact["url"]
    elif change == "extra":
        artifact["unexpected"] = True
    elif change == "http":
        artifact["url"] = "http://cdn.example.test/profile.json.gz"
    elif change == "generic":
        artifact["maturity"] = ProfileMaturity.GENERIC.value
    else:
        artifact["profile_sha256"] = "not-a-digest"

    with pytest.raises(ProfileManifestSchemaError, match=message):
        ProfileManifest.from_json(value)


def test_required_types_and_timestamp_values_are_strict() -> None:
    artifact = _artifact("TST", "QuickDraft").to_json()
    artifact["gzip_bytes"] = True
    with pytest.raises(ProfileManifestError, match="gzip_bytes"):
        ProfileManifestArtifact.from_json(artifact)

    value = ProfileManifest(artifacts=(_artifact("TST", "QuickDraft"),), published_at=PUBLISHED_AT).to_json()
    value["published_at"] = datetime(2026, 8, 30, tzinfo=UTC).isoformat()
    value["artifacts"] = tuple(value["artifacts"])
    with pytest.raises(ProfileManifestError, match="artifacts must be an array"):
        ProfileManifest.from_json(value)


def test_json_object_with_noncanonical_input_is_normalized() -> None:
    manifest = ProfileManifest(artifacts=(_artifact("TST", "QuickDraft"),), published_at=PUBLISHED_AT)
    value = json.loads(manifest.to_bytes())
    value["artifacts"][0]["gzip_sha256"] = "A" * 64

    rebuilt = ProfileManifest.from_json(value)
    assert rebuilt.artifacts[0].gzip_sha256 == "a" * 64
    assert rebuilt.to_bytes() == manifest.to_bytes()
