from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import gzip
import hashlib
import io
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from draftomen.profile_client import (
    BUNDLED_PROFILE_BYTES,
    BUNDLED_PROFILE_EVENT_FORMAT,
    BUNDLED_PROFILE_SET_CODE,
    BUNDLED_PROFILE_SHA256,
    ProfileClient,
    ProfileClientError,
    ProfileNetworkPolicy,
    ProfileRefreshOutcome,
)
from draftomen.profile_manifest import ProfileManifest, ProfileManifestArtifact
from draftomen.set_profile import (
    AggregateEvidence,
    CardRating,
    RateEstimate,
    SET_PROFILE_SCHEMA_VERSION,
    ProfileMaturity,
    SetProfile,
    dump_set_profile,
    load_set_profile,
    set_profile_path,
)

BASELINE_PATH = Path(__file__).parents[1] / "draftomen" / "baseline_profiles" / "hob-quickdraft.json"


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "set-profiles"
MANIFEST_URL = "https://profiles.example.test/v1/manifest.json"
ARTIFACT_URL = "https://profiles.example.test/v1/tst-quickdraft.json.gz"
NOW = "2026-08-30T12:00:00+00:00"


class _Response:
    def __init__(self, payload: bytes, url: str | None = None) -> None:
        self._payload = io.BytesIO(payload)
        self.url = url
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)

    def geturl(self) -> str | None:
        return self.url

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _profile(*, version: str = "1.0-semantic", generated_at: str = "2026-08-29T02:00:00+00:00") -> SetProfile:
    value = json.loads((FIXTURE_DIR / "semantic-only.json").read_text(encoding="utf-8"))
    value["profile_version"] = version
    value["generated_at"] = generated_at
    return SetProfile.from_json(value)


def _schema_two_profile(
    *,
    profile_version: str = "2.0-aggregate",
    generated_at: str = "2026-08-30T02:00:00+00:00",
) -> SetProfile:
    early = load_set_profile(FIXTURE_DIR / "early.json")
    authority = AggregateEvidence(
        source_format="quickdraft",
        fallback_reason=None,
        confidence=1.0,
    )
    card_rate = RateEstimate(
        raw_value=0.62,
        value=0.61,
        samples=1_000,
        prior_value=0.5,
        source="17lands:card-ratings",
        aggregate_evidence=authority,
    )
    pair_rate = RateEstimate(
        raw_value=0.58,
        value=0.57,
        samples=1_000,
        prior_value=0.5,
        source="17lands:color-ratings",
        aggregate_evidence=authority,
    )
    pair = replace(early.pairs[0], performance=pair_rate)
    return replace(
        early,
        profile_version=profile_version,
        generated_at=generated_at,
        pairs=(pair,),
        card_ratings=(
            CardRating(card_key="oracle_id:aggregate-card", gih_win_rate=card_rate),
        ),
        schema_version=2,
    )


def _bundled_profile() -> SetProfile:
    return load_set_profile(
        BASELINE_PATH,
        expected_set_code=BUNDLED_PROFILE_SET_CODE,
        expected_format=BUNDLED_PROFILE_EVENT_FORMAT,
    )


def _bundled_variant(*, profile_version: str, generated_at: str) -> SetProfile:
    value = _bundled_profile().to_json()
    value["profile_version"] = profile_version
    value["generated_at"] = generated_at
    value["source"] = {"provider": "test"}
    return SetProfile.from_json(value)


def _artifact(
    profile: SetProfile,
    *,
    generated_at: str | None = None,
    profile_version: str | None = None,
    maturity: ProfileMaturity | str | None = None,
    url: str = ARTIFACT_URL,
    compressed: bytes | None = None,
    profile_bytes: bytes | None = None,
    **changes: Any,
) -> tuple[ProfileManifestArtifact, bytes]:
    raw = profile.to_bytes() if profile_bytes is None else profile_bytes
    packed = gzip.compress(raw, mtime=0) if compressed is None else compressed
    artifact = ProfileManifestArtifact(
        set_code=profile.set_code,
        event_format=profile.event_format,
        set_profile_schema_version=profile.schema_version,
        profile_version=profile.profile_version if profile_version is None else profile_version,
        generated_at=profile.generated_at if generated_at is None else generated_at,
        url=url,
        gzip_bytes=len(packed),
        profile_bytes=len(raw),
        gzip_sha256=hashlib.sha256(packed).hexdigest(),
        profile_sha256=hashlib.sha256(raw).hexdigest(),
        maturity=profile.maturity if maturity is None else maturity,
        **changes,
    )
    return artifact, packed


def _manifest(artifact: ProfileManifestArtifact, *, published_at: str = NOW) -> bytes:
    return ProfileManifest(artifacts=(artifact,), published_at=published_at).to_bytes()


def _opener_for(payloads: dict[str, bytes], calls: list[str] | None = None):
    def opener(request: Any, *, timeout: float) -> _Response:
        del timeout
        url = request.full_url
        if calls is not None:
            calls.append(url)
        if url not in payloads:
            raise OSError(f"unexpected URL {url}")
        return _Response(payloads[url], url=url)

    return opener


def test_bundled_baseline_is_selected_offline_without_network_or_cache_write(tmp_path: Path) -> None:
    baseline_before = BASELINE_PATH.read_bytes()
    calls: list[str] = []
    client = ProfileClient(
        tmp_path,
        network_policy=ProfileNetworkPolicy.OFFLINE,
        opener=_opener_for({}, calls),
    )

    loaded = client.load_cached("HOB", "QuickDraft")
    refreshed = client.refresh("HOB", "QuickDraft")

    assert len(baseline_before) == BUNDLED_PROFILE_BYTES
    assert hashlib.sha256(baseline_before).hexdigest() == BUNDLED_PROFILE_SHA256
    assert loaded.profile == _bundled_profile()
    assert loaded.source == "bundled-metadata-only"
    assert refreshed.profile == loaded.profile
    assert refreshed.outcome is ProfileRefreshOutcome.CACHED
    assert calls == []
    assert not client.profile_path("HOB", "QuickDraft").exists()
    assert BASELINE_PATH.read_bytes() == baseline_before


def test_flat_cache_precedes_bundled_baseline(tmp_path: Path) -> None:
    local = _bundled_variant(profile_version="2.0", generated_at="2026-09-03T02:00:00+00:00")
    path = set_profile_path(set_code="HOB", event_format="QuickDraft", app_dir=tmp_path)
    dump_set_profile(local, path)

    result = ProfileClient(tmp_path).load_cached("HOB", "QuickDraft")

    assert result.profile == local
    assert result.source == "local-metadata-only"
    assert result.diagnostics == ()


def test_historical_cache_precedes_bundled_baseline_and_is_migrated(tmp_path: Path) -> None:
    historical = _bundled_variant(profile_version="2.0", generated_at="2026-09-03T02:00:00+00:00")
    legacy = tmp_path / "profiles" / "hob-quickdraft.json"
    dump_set_profile(historical, legacy)

    client = ProfileClient(tmp_path)
    result = client.load_cached("HOB", "QuickDraft")

    assert result.profile == historical
    assert result.source == "legacy-migrated-metadata-only"
    assert client.profile_path("HOB", "QuickDraft").read_bytes() == historical.to_bytes()


def test_nonmatching_identity_does_not_inspect_or_diagnose_bundled_path(tmp_path: Path) -> None:
    bundled_path = tmp_path / "bundle.json"
    result = ProfileClient(tmp_path, bundled_profile_path=bundled_path).load_cached("TST", "QuickDraft")

    assert result.profile.maturity is ProfileMaturity.GENERIC
    assert result.source == "generic"
    assert not any(item.startswith("rejected-bundled:") for item in result.diagnostics)
    assert not bundled_path.exists()


def test_missing_bundled_baseline_is_rejected(tmp_path: Path) -> None:
    bundled_path = tmp_path / "missing.json"

    result = ProfileClient(tmp_path, bundled_profile_path=bundled_path).load_cached("HOB", "QuickDraft")

    assert result.profile.maturity is ProfileMaturity.GENERIC
    assert result.source == "generic"
    assert result.diagnostics == ("rejected-bundled:missing",)


@pytest.mark.parametrize(
    ("payload", "diagnostic"),
    [
        (b"not-json", "rejected-bundled:checksum-or-size"),
        (_profile().to_bytes(), "rejected-bundled:checksum-or-size"),
        (BASELINE_PATH.read_bytes() + b" ", "rejected-bundled:checksum-or-size"),
    ],
    ids=("corrupt", "wrong-target", "digest-mismatched"),
)
def test_invalid_bundled_baseline_is_rejected_without_fallback_leaks(
    tmp_path: Path,
    payload: bytes,
    diagnostic: str,
) -> None:
    bundled_path = tmp_path / "bundle.json"
    bundled_path.write_bytes(payload)

    result = ProfileClient(tmp_path, bundled_profile_path=bundled_path).load_cached("HOB", "QuickDraft")

    assert result.profile.maturity is ProfileMaturity.GENERIC
    assert result.source == "generic"
    assert result.diagnostics == (diagnostic,)


def test_pinned_bundled_baseline_rejects_strict_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def return_generic(*_args: object, **_kwargs: object) -> SetProfile:
        return SetProfile.generic(set_code=BUNDLED_PROFILE_SET_CODE, event_format=BUNDLED_PROFILE_EVENT_FORMAT)

    monkeypatch.setattr("draftomen.profile_client.load_set_profile", return_generic)
    result = ProfileClient(tmp_path).load_cached("HOB", "QuickDraft")

    assert result.profile.maturity is ProfileMaturity.GENERIC
    assert result.source == "generic"
    assert result.diagnostics == ("rejected-bundled:invalid",)


def test_matching_last_valid_profile_follows_rejected_bundled_baseline(tmp_path: Path) -> None:
    bundled_path = tmp_path / "bundle.json"
    bundled_path.write_bytes(b"not-json")
    last_valid = _bundled_variant(profile_version="2.0", generated_at="2026-09-03T02:00:00+00:00")

    result = ProfileClient(tmp_path, bundled_profile_path=bundled_path).load_cached(
        "HOB",
        "QuickDraft",
        last_valid_profile=last_valid,
    )

    assert result.profile == last_valid
    assert result.source == "last-valid"
    assert result.diagnostics == ("rejected-bundled:checksum-or-size",)


def test_hosted_refresh_supersedes_bundled_baseline_without_mutating_resource(tmp_path: Path) -> None:
    bundled_path = tmp_path / "bundle.json"
    bundled_before = BASELINE_PATH.read_bytes()
    bundled_path.write_bytes(bundled_before)
    refreshed = _bundled_variant(profile_version="2.0", generated_at="2026-09-03T02:00:00+00:00")
    artifact, packed = _artifact(refreshed)
    calls: list[str] = []
    client = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: _manifest(artifact), ARTIFACT_URL: packed}, calls),
        bundled_profile_path=bundled_path,
        clock=_FrozenClock(datetime.fromisoformat("2026-09-03T12:00:00+00:00")),
        manifest_ttl_seconds=0,
    )

    result = client.refresh("HOB", "QuickDraft", force=True)

    assert result.outcome is ProfileRefreshOutcome.UPDATED
    assert result.profile == refreshed
    assert client.profile_path("HOB", "QuickDraft").read_bytes() == refreshed.to_bytes()
    assert bundled_path.read_bytes() == bundled_before
    assert calls == [MANIFEST_URL, ARTIFACT_URL]


def test_refresh_installs_and_loads_schema_two_aggregate_profile(tmp_path: Path) -> None:
    profile = _schema_two_profile()
    artifact, packed = _artifact(profile)
    client = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: _manifest(artifact), ARTIFACT_URL: packed}),
    )

    refreshed = client.refresh("TST", "QuickDraft", force=True)
    loaded = client.load_cached("TST", "QuickDraft")

    assert refreshed.outcome is ProfileRefreshOutcome.UPDATED
    assert refreshed.profile == profile
    assert loaded.profile == profile
    assert loaded.profile.schema_version == 2
    card_rate = loaded.profile.card_ratings[0].gih_win_rate
    assert card_rate.aggregate_evidence == AggregateEvidence("quickdraft", None, 1.0)
    pair = loaded.profile.pair("WU")
    assert pair is not None
    assert pair.performance is not None
    assert pair.performance.aggregate_evidence == AggregateEvidence("quickdraft", None, 1.0)


def test_refresh_schema_mismatch_preserves_last_good_schema_two_profile(tmp_path: Path) -> None:
    installed_profile = _schema_two_profile()
    installed_artifact, installed_packed = _artifact(installed_profile)
    payloads = {
        MANIFEST_URL: _manifest(installed_artifact),
        ARTIFACT_URL: installed_packed,
    }
    client = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for(payloads),
        manifest_ttl_seconds=0,
    )

    installed = client.refresh("TST", "QuickDraft", force=True)
    assert installed.outcome is ProfileRefreshOutcome.UPDATED

    newer_profile = _schema_two_profile(
        profile_version="2.1-aggregate",
        generated_at="2026-08-31T02:00:00+00:00",
    )
    newer_artifact, newer_packed = _artifact(newer_profile)
    mismatched_artifact = replace(newer_artifact, set_profile_schema_version=1)
    payloads.update(
        {
            MANIFEST_URL: _manifest(
                mismatched_artifact,
                published_at="2026-08-31T12:00:00+00:00",
            ),
            ARTIFACT_URL: newer_packed,
        }
    )

    rejected = client.refresh("TST", "QuickDraft", force=True)
    loaded = client.load_cached("TST", "QuickDraft")

    assert rejected.outcome is ProfileRefreshOutcome.ARTIFACT_INVALID
    assert rejected.profile == installed_profile
    assert loaded.profile == installed_profile
    assert client.profile_path("TST", "QuickDraft").read_bytes() == installed_profile.to_bytes()


def test_load_cached_is_local_only_and_offline_zero_network(tmp_path: Path) -> None:
    profile = _profile()
    dump_set_profile(profile, set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path))
    calls: list[str] = []
    client = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        network_policy=ProfileNetworkPolicy.OFFLINE,
        opener=_opener_for({}, calls),
    )

    cached = client.load_cached("TST", "QuickDraft")
    result = client.refresh("TST", "QuickDraft")

    assert cached.profile == profile
    assert cached.source == "local-semantic-only"
    assert result.profile == profile
    assert result.outcome is ProfileRefreshOutcome.CACHED
    assert calls == []


@pytest.mark.parametrize("cache_kind", ["missing", "corrupt", "future"])
def test_missing_corrupt_and_future_cache_fall_back_to_generic(tmp_path: Path, cache_kind: str) -> None:
    path = set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path)
    if cache_kind == "corrupt":
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not-json")
    elif cache_kind == "future":
        value = _profile().to_json()
        value["schema_version"] = SET_PROFILE_SCHEMA_VERSION + 1
        path.parent.mkdir(parents=True)
        path.write_bytes(json.dumps(value).encode("utf-8"))

    result = ProfileClient(tmp_path).load_cached("TST", "QuickDraft")

    assert result.source == "generic"
    assert result.profile.maturity is ProfileMaturity.GENERIC


def test_existing_historical_cache_is_adopted_without_network(tmp_path: Path) -> None:
    profile = _profile()
    legacy = tmp_path / "profiles" / "tst-quickdraft.json"
    dump_set_profile(profile, legacy)
    client = ProfileClient(tmp_path, opener=lambda *_args, **_kwargs: pytest.fail("network used"))

    result = client.load_cached("TST", "QuickDraft")

    assert result.profile == profile
    assert result.source == "legacy-migrated-semantic-only"
    assert client.profile_path("TST", "QuickDraft").read_bytes() == profile.to_bytes()


def test_refresh_installs_newer_then_unchanged_without_artifact_download(tmp_path: Path) -> None:
    old = _profile()
    new = _profile(version="1.1-semantic", generated_at="2026-08-30T02:00:00+00:00")
    dump_set_profile(old, set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path))
    artifact, packed = _artifact(new)
    calls: list[str] = []
    client = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: _manifest(artifact), ARTIFACT_URL: packed}, calls),
        clock=_FrozenClock(datetime.fromisoformat(NOW)),
        manifest_ttl_seconds=0,
    )

    updated = client.refresh("TST", "QuickDraft", force=True)
    calls_after_update = list(calls)
    unchanged = client.refresh("TST", "QuickDraft", force=True)

    assert updated.outcome is ProfileRefreshOutcome.UPDATED
    assert updated.profile.profile_version == "1.1-semantic"
    assert unchanged.outcome is ProfileRefreshOutcome.UNCHANGED
    assert calls_after_update.count(ARTIFACT_URL) == 1
    assert calls.count(ARTIFACT_URL) == 1



@pytest.mark.parametrize(
    ("alternate_manifest_url", "alternate_artifact_url"),
    [
        (
            "https://profiles.example.test/v2/manifest.json",
            "https://profiles.example.test/v2/tst-quickdraft.json.gz",
        ),
        (
            "https://other.example.test/v1/manifest.json",
            "https://other.example.test/v1/tst-quickdraft.json.gz",
        ),
    ],
)
def test_manifest_cache_is_bound_to_exact_source_within_ttl(
    tmp_path: Path,
    alternate_manifest_url: str,
    alternate_artifact_url: str,
) -> None:
    old = _profile()
    first = _profile(version="1.1-semantic", generated_at="2026-08-30T02:00:00+00:00")
    second = _profile(version="1.2-semantic", generated_at="2026-08-31T02:00:00+00:00")
    dump_set_profile(old, set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path))
    first_artifact, first_packed = _artifact(first)
    second_artifact, second_packed = _artifact(second, url=alternate_artifact_url)
    calls: list[str] = []
    payloads = {
        MANIFEST_URL: _manifest(first_artifact),
        ARTIFACT_URL: first_packed,
        alternate_manifest_url: _manifest(second_artifact),
        alternate_artifact_url: second_packed,
    }
    clock = _FrozenClock(datetime.fromisoformat(NOW))

    initial = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for(payloads, calls),
        clock=clock,
        manifest_ttl_seconds=24 * 60 * 60,
    )
    assert initial.refresh("TST", "QuickDraft", force=True).outcome is ProfileRefreshOutcome.UPDATED

    switched = ProfileClient(
        tmp_path,
        manifest_url=alternate_manifest_url,
        opener=_opener_for(payloads, calls),
        clock=clock,
        manifest_ttl_seconds=24 * 60 * 60,
    )
    result = switched.refresh("TST", "QuickDraft")

    assert result.outcome is ProfileRefreshOutcome.UPDATED
    assert result.profile == second
    assert calls.count(MANIFEST_URL) == 1
    assert calls.count(alternate_manifest_url) == 1
    assert json.loads(switched.manifest_path().read_bytes())["manifest_url"] == alternate_manifest_url


def test_manifest_cache_without_source_is_refetched(tmp_path: Path) -> None:
    current = _profile()
    candidate = _profile(version="1.1-semantic", generated_at="2026-08-30T02:00:00+00:00")
    dump_set_profile(current, set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path))
    artifact, packed = _artifact(candidate)
    cache_path = tmp_path / "set-profiles" / "v1" / "manifest.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(
        json.dumps(
            {
                "checked_at": NOW,
                "manifest": json.loads(_manifest(artifact)),
            }
        ).encode("utf-8")
    )
    calls: list[str] = []
    client = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: _manifest(artifact), ARTIFACT_URL: packed}, calls),
        clock=_FrozenClock(datetime.fromisoformat(NOW)),
        manifest_ttl_seconds=24 * 60 * 60,
    )

    result = client.refresh("TST", "QuickDraft")

    assert result.outcome is ProfileRefreshOutcome.UPDATED
    assert calls.count(MANIFEST_URL) == 1

@pytest.mark.parametrize(
    ("version", "generated_at", "maturity", "expected"),
    [
        ("0.9-semantic", "2026-08-28T02:00:00+00:00", ProfileMaturity.SEMANTIC_ONLY, "stale"),
        ("2.0-metadata", "2026-08-31T02:00:00+00:00", ProfileMaturity.METADATA_ONLY, "stale"),
        ("conflicting", "2026-08-29T02:00:00+00:00", ProfileMaturity.SEMANTIC_ONLY, "conflict"),
    ],
)
def test_refresh_rejects_older_conflicting_and_maturity_downgrade(
    tmp_path: Path,
    version: str,
    generated_at: str,
    maturity: ProfileMaturity,
    expected: str,
) -> None:
    current = _profile()
    dump_set_profile(current, set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path))
    candidate = _profile(version=version, generated_at=generated_at)
    artifact, packed = _artifact(candidate, maturity=maturity)
    calls: list[str] = []
    client = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: _manifest(artifact), ARTIFACT_URL: packed}, calls),
        clock=_FrozenClock(datetime.fromisoformat(NOW)),
        manifest_ttl_seconds=0,
    )

    result = client.refresh("TST", "QuickDraft", force=True)

    assert result.profile == current
    assert result.outcome is ProfileRefreshOutcome.STALE_MANIFEST
    assert f"artifact:{expected}" in result.diagnostics
    assert ARTIFACT_URL not in calls


def test_refresh_failure_preserves_last_good_cache_and_manifest(tmp_path: Path) -> None:
    current = _profile()
    dump_set_profile(current, set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path))
    artifact, _ = _artifact(_profile(version="1.1-semantic", generated_at="2026-08-30T02:00:00+00:00"))
    before = set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path).read_bytes()
    manifest_payload = _manifest(artifact)
    client = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: manifest_payload, ARTIFACT_URL: b"broken-gzip"}),
        clock=_FrozenClock(datetime.fromisoformat(NOW)),
        manifest_ttl_seconds=0,
    )

    result = client.refresh("TST", "QuickDraft", force=True)

def test_manifest_and_artifact_urls_reject_credentials_fragments_ports_and_cross_origin(tmp_path: Path) -> None:
    invalid = (
        "http://profiles.example.test/manifest.json",
        "https://user:pass@profiles.example.test/manifest.json",
        "https://profiles.example.test:444/manifest.json",
        "https://profiles.example.test:/manifest.json",
        "https://profiles.example.test:not-a-port/manifest.json",
        "https://profiles.example.test/manifest.json#fragment",
        "https://profiles.example.test/manifest.json#",
        "https://profiles.example.test/manifest.json with-space",
        "https://profiles.example.test/manifest.json\x00",
        "https:///manifest.json",
        " https://profiles.example.test/manifest.json",
    )
    for url in invalid:
        with pytest.raises(ProfileClientError):
            ProfileClient(tmp_path, manifest_url=url)

    with pytest.raises(ProfileClientError):
        ProfileClient(tmp_path, manifest_url="https://[::1")


def test_manifest_url_allows_explicit_https_443(tmp_path: Path) -> None:
    url = "https://profiles.example.test:443/manifest.json"

    client = ProfileClient(tmp_path, manifest_url=url)

    assert client.manifest_url == url


def test_redirect_final_origin_is_rejected_without_touching_cache(tmp_path: Path) -> None:
    current = _profile()
    path = set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path)
    dump_set_profile(current, path)
    artifact, packed = _artifact(_profile(version="1.1-semantic", generated_at="2026-08-30T02:00:00+00:00"))

    def opener(request: Any, *, timeout: float) -> _Response:
        del timeout
        if request.full_url == MANIFEST_URL:
            return _Response(_manifest(artifact), url=MANIFEST_URL)
        return _Response(packed, url="https://other.example.test/profile.gz")

    before = path.read_bytes()
    result = ProfileClient(tmp_path, manifest_url=MANIFEST_URL, opener=opener).refresh("TST", "QuickDraft", force=True)

    assert result.profile == current
    assert result.outcome is ProfileRefreshOutcome.ARTIFACT_INVALID
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["compressed-size", "profile-size", "gzip-hash", "profile-hash", "gzip-trailing", "gzip-incomplete"])
def test_artifact_bounds_decompression_and_hash_failures_preserve_cache(tmp_path: Path, failure: str) -> None:
    current = _profile()
    path = set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path)
    dump_set_profile(current, path)
    newer = _profile(version="1.1-semantic", generated_at="2026-08-30T02:00:00+00:00")
    artifact, packed = _artifact(newer)
    if failure == "compressed-size":
        client = ProfileClient(tmp_path, manifest_url=MANIFEST_URL, max_gzip_bytes=1)
    elif failure == "profile-size":
        client = ProfileClient(tmp_path, manifest_url=MANIFEST_URL, max_profile_bytes=1)
    else:
        client = ProfileClient(tmp_path, manifest_url=MANIFEST_URL)
    if failure == "gzip-hash":
        artifact = replace(artifact, gzip_sha256="0" * 64)
    elif failure == "profile-hash":
        artifact = replace(artifact, profile_sha256="0" * 64)
    elif failure == "gzip-trailing":
        packed += b"trailing"
        artifact = replace(
            artifact,
            gzip_bytes=len(packed),
            gzip_sha256=hashlib.sha256(packed).hexdigest(),
        )
    elif failure == "gzip-incomplete":
        packed = packed[:-4]
        artifact = replace(
            artifact,
            gzip_bytes=len(packed),
            gzip_sha256=hashlib.sha256(packed).hexdigest(),
        )
    payloads = {MANIFEST_URL: _manifest(artifact), ARTIFACT_URL: packed}
    client.opener = _opener_for(payloads)

    result = client.refresh("TST", "QuickDraft", force=True)

    assert result.profile == current
    assert result.outcome is ProfileRefreshOutcome.ARTIFACT_INVALID
    assert path.read_bytes() == current.to_bytes()


def test_schema_identity_and_metadata_failures_are_structured(tmp_path: Path) -> None:
    current = _profile()
    path = set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path)
    dump_set_profile(current, path)
    malformed = json.loads(current.to_bytes())
    malformed["schema_version"] = SET_PROFILE_SCHEMA_VERSION + 1
    malformed["profile_version"] = "1.1-semantic"
    malformed["generated_at"] = "2026-08-30T02:00:00+00:00"
    raw = (json.dumps(malformed, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    artifact, packed = _artifact(
        current,
        generated_at="2026-08-30T02:00:00+00:00",
        profile_version="1.1-semantic",
        profile_bytes=raw,
        compressed=gzip.compress(raw, mtime=0),
    )
    result = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: _manifest(artifact), ARTIFACT_URL: packed}),
    ).refresh("TST", "QuickDraft", force=True)

    assert result.profile == current
    assert result.outcome is ProfileRefreshOutcome.ARTIFACT_INVALID
    assert any(item.startswith("artifact:") for item in result.diagnostics)


def test_refresh_outcomes_distinguish_offline_missing_manifest_and_remote_failures(tmp_path: Path) -> None:
    offline = ProfileClient(tmp_path, network_policy=ProfileNetworkPolicy.OFFLINE)
    assert offline.refresh("TST", "QuickDraft").outcome is ProfileRefreshOutcome.OFFLINE

    malformed = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: b"not-json"}),
    )
    assert malformed.refresh("TST", "QuickDraft", force=True).outcome is ProfileRefreshOutcome.MANIFEST_INVALID

    def unavailable(_request: Any, *, timeout: float) -> _Response:
        del timeout
        raise OSError("network unavailable")

    remote_failed = ProfileClient(tmp_path, manifest_url=MANIFEST_URL, opener=unavailable)
    assert remote_failed.refresh("TST", "QuickDraft", force=True).outcome is ProfileRefreshOutcome.REMOTE_FAILED

    current = _profile()
    missing_artifact = _artifact(current)[0]
    manifest_without_requested_artifact = ProfileManifest(
        artifacts=(
            replace(
                missing_artifact,
                set_code="other",
            ),
        ),
        published_at=NOW,
    ).to_bytes()
    missing = ProfileClient(
        tmp_path,
        manifest_url=MANIFEST_URL,
        opener=_opener_for({MANIFEST_URL: manifest_without_requested_artifact}),
    )
    assert missing.refresh("TST", "QuickDraft", force=True).outcome is ProfileRefreshOutcome.MISSING


def test_timeout_is_positive_and_passed_to_opener(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ProfileClient(tmp_path, timeout_seconds=0)
    with pytest.raises(ValueError):
        ProfileClient(tmp_path, timeout_seconds=float("inf"))

    seen: list[float] = []
    profile = _profile(version="1.1-semantic", generated_at="2026-08-30T02:00:00+00:00")
    artifact, packed = _artifact(profile)

    def opener(request: Any, *, timeout: float) -> _Response:
        seen.append(timeout)
        return _Response(_manifest(artifact) if request.full_url == MANIFEST_URL else packed, request.full_url)

    ProfileClient(tmp_path, manifest_url=MANIFEST_URL, opener=opener, timeout_seconds=3.5).refresh(
        "TST", "QuickDraft", force=True
    )
    assert seen == [3.5, 3.5]


def test_concurrent_refreshes_expose_only_whole_profiles(tmp_path: Path) -> None:
    old = _profile()
    new = _profile(version="1.1-semantic", generated_at="2026-08-30T02:00:00+00:00")
    path = set_profile_path(set_code="TST", event_format="QuickDraft", app_dir=tmp_path)
    dump_set_profile(old, path)
    artifact, packed = _artifact(new)
    payloads = {MANIFEST_URL: _manifest(artifact), ARTIFACT_URL: packed}

    def run() -> ProfileRefreshOutcome:
        client = ProfileClient(tmp_path, manifest_url=MANIFEST_URL, opener=_opener_for(payloads), manifest_ttl_seconds=0)
        return client.refresh("TST", "QuickDraft", force=True).outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: run(), range(2)))

    loaded = load_set_profile(path, expected_set_code="tst", expected_format="quickdraft")
    assert all(outcome in {ProfileRefreshOutcome.UPDATED, ProfileRefreshOutcome.UNCHANGED} for outcome in outcomes)
    assert loaded.to_bytes() in {old.to_bytes(), new.to_bytes()}
    assert path.read_bytes() in {old.to_bytes(), new.to_bytes()}
