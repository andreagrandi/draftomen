from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
from typing import Any
import pytest

from draftomen.augmented_artifact import AugmentedArtifact
from draftomen.augmented_model_client import (
    AUGMENTED_ARTIFACT_ACCEPT,
    AUGMENTED_MANIFEST_ACCEPT,
    AUGMENTED_MANIFEST_URL,
    AUGMENTED_OBJECTS_BASE_URL,
    AUGMENTED_TIMEOUT_SECONDS,
    AUGMENTED_USER_AGENT,
    AugmentedModelClient,
    AugmentedModelClientError,
    AugmentedModelOutcome,
    augmented_cache_path,
)
from draftomen.profile_data_refresh import SUPPORTED_FORMATS
from tests.augmented_artifacts import (
    augmented_artifact,
    augmented_manifest_entry_json,
    augmented_manifest_json,
    canonical_bytes,
)


class _Response:
    def __init__(self, payload: bytes, *, url: str, status: int = 200) -> None:
        self._stream = io.BytesIO(payload)
        self.url = url
        self.status = status
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True


def _manifest_payload(manifest: dict) -> bytes:
    return canonical_bytes(manifest)


def _client(
    tmp_path: Path,
    *,
    objects: dict[str, bytes],
    manifest: dict | None = None,
    manifest_status: int = 200,
    manifest_url: str | None = None,
    error_urls: tuple[str, ...] = (),
    calls: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> tuple[AugmentedModelClient, list[dict[str, Any]]]:
    recorded: list[dict[str, Any]] = calls if calls is not None else []
    manifest_value = augmented_manifest_json() if manifest is None else manifest

    def opener(request: Any, *, timeout: float) -> _Response:
        recorded.append({"request": request, "timeout": timeout})
        url = request.full_url
        if url in error_urls:
            raise OSError("network down")
        if url == AUGMENTED_MANIFEST_URL:
            return _Response(
                _manifest_payload(manifest_value), url=url, status=manifest_status
            )
        if url.startswith(AUGMENTED_OBJECTS_BASE_URL) and url in objects:
            return _Response(objects[url], url=url)
        raise AssertionError(f"unexpected request {url}")

    client = AugmentedModelClient(
        app_dir=tmp_path, manifest_url=manifest_url or AUGMENTED_MANIFEST_URL, opener=opener, **kwargs
    )
    return client, recorded


def _served(*, set_code: str = "tst") -> tuple[bytes, str, int, dict]:
    artifact = augmented_artifact(set_code)
    payload = artifact.to_gzip_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    manifest = augmented_manifest_json(
        sets={
            set_code: augmented_manifest_entry_json(
                artifact_bytes=len(artifact.to_bytes()),
                artifact_sha256=digest,
            )
        }
    )
    return payload, digest, len(artifact.to_bytes()), manifest


def test_cold_load_downloads_manifest_then_object_and_caches(tmp_path: Path) -> None:
    payload, digest, size, manifest = _served()
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    client, calls = _client(tmp_path, objects={object_url: payload}, manifest=manifest)

    loaded = client.load("TST", allow_network=True)

    assert loaded.outcome is AugmentedModelOutcome.DOWNLOADED
    assert loaded.available is True
    assert loaded.status == "downloaded"
    assert loaded.artifact is not None
    assert loaded.message == (
        "Augmented model for set 'tst' was downloaded and cached."
    )
    assert len(calls) == 2
    manifest_request = calls[0]["request"]
    object_request = calls[1]["request"]
    assert manifest_request.full_url == AUGMENTED_MANIFEST_URL
    assert manifest_request.get_header("Accept") == AUGMENTED_MANIFEST_ACCEPT
    assert manifest_request.get_header("User-agent") == AUGMENTED_USER_AGENT
    assert calls[0]["timeout"] == AUGMENTED_TIMEOUT_SECONDS
    assert object_request.full_url == object_url
    assert object_request.get_header("Accept") == AUGMENTED_ARTIFACT_ACCEPT
    assert object_request.get_header("User-agent") == AUGMENTED_USER_AGENT
    assert client.cache_path("TST").read_bytes() == payload
    assert not tuple(client.cache_path("tst").parent.glob(".tst.json.gz.*"))

    second, _ = client.load("tst", allow_network=True), None
    assert second.outcome is AugmentedModelOutcome.CACHED
    assert [item["request"].full_url for item in calls].count(object_url) == 1


def test_valid_cache_hit_never_opens_network(tmp_path: Path) -> None:
    destination = augmented_cache_path(set_code="tst", app_dir=tmp_path)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(augmented_artifact().to_gzip_bytes())

    def fail_opener(*_: Any, **__: Any) -> Any:
        raise AssertionError("network must not be opened for a valid cache")

    loaded = AugmentedModelClient(app_dir=tmp_path, opener=fail_opener).load(
        "TST", allow_network=False
    )
    assert loaded.outcome is AugmentedModelOutcome.CACHED
    assert loaded.available is True
    assert loaded.artifact is not None


def test_replace_failure_returns_downloaded_and_preserves_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, digest, _, manifest = _served()
    previous = augmented_artifact(multiplier=0.0).to_gzip_bytes()
    assert previous != payload
    destination = augmented_cache_path(set_code="tst", app_dir=tmp_path)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(previous)
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"

    def fail_replace(*_: Any, **__: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("draftomen.augmented_model_client.os.replace", fail_replace)
    client, _ = _client(tmp_path, objects={object_url: payload}, manifest=manifest)
    loaded = client.load("tst", allow_network=True)

    assert loaded.outcome is AugmentedModelOutcome.DOWNLOADED
    assert loaded.available is True
    assert "could not be written" in loaded.message
    assert destination.read_bytes() == previous
    assert not tuple(destination.parent.glob(f".{destination.name}.*"))


def test_manifest_without_set_returns_missing(tmp_path: Path) -> None:
    payload, digest, size, _ = _served(set_code="hob")
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    manifest = augmented_manifest_json(
        sets={
            "hob": augmented_manifest_entry_json(
                artifact_bytes=size, artifact_sha256=digest
            )
        }
    )
    client, calls = _client(tmp_path, objects={object_url: payload}, manifest=manifest)
    loaded = client.load("lci", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.MISSING
    assert loaded.available is False
    assert "no entry" in loaded.message
    assert not client.cache_path("lci").exists()


def test_future_compatibility_entry_returns_incompatible(tmp_path: Path) -> None:
    payload, digest, size, _ = _served()
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    manifest = augmented_manifest_json(
        sets={
            "tst": augmented_manifest_entry_json(
                artifact_bytes=size,
                artifact_sha256=digest,
                compatibility="coarse-context-model-c/v2",
            )
        }
    )
    client, calls = _client(tmp_path, objects={object_url: payload}, manifest=manifest)
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INCOMPATIBLE
    assert loaded.available is False
    assert [item["request"].full_url for item in calls] == [AUGMENTED_MANIFEST_URL]


def test_future_schema_version_returns_incompatible(tmp_path: Path) -> None:
    payload, digest, size, _ = _served()
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    manifest = augmented_manifest_json(
        sets={
            "tst": augmented_manifest_entry_json(
                artifact_bytes=size,
                artifact_sha256=digest,
                artifact_schema_version=2,
            )
        }
    )
    client, _ = _client(tmp_path, objects={object_url: payload}, manifest=manifest)
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INCOMPATIBLE
    assert loaded.available is False


def test_non_gzip_payload_returns_invalid(tmp_path: Path) -> None:
    digest = "c" * 64
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    manifest = augmented_manifest_json(
        sets={
            "tst": augmented_manifest_entry_json(
                artifact_bytes=10, artifact_sha256=digest
            )
        }
    )
    client, _ = _client(
        tmp_path, objects={object_url: b"not gzip"}, manifest=manifest
    )
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False
    assert not client.cache_path("tst").exists()


def test_checksum_mismatch_returns_invalid(tmp_path: Path) -> None:
    payload = augmented_artifact().to_gzip_bytes()
    digest = "d" * 64
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    manifest = augmented_manifest_json(
        sets={
            "tst": augmented_manifest_entry_json(
                artifact_bytes=len(augmented_artifact().to_bytes()),
                artifact_sha256=digest,
            )
        }
    )
    client, _ = _client(tmp_path, objects={object_url: payload}, manifest=manifest)
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False
    assert not client.cache_path("tst").exists()


def test_wrong_set_payload_returns_invalid(tmp_path: Path) -> None:
    other = augmented_artifact("oth").to_gzip_bytes()
    digest = hashlib.sha256(other).hexdigest()
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    artifact = augmented_artifact("tst")
    manifest = augmented_manifest_json(
        sets={
            "tst": augmented_manifest_entry_json(
                artifact_bytes=len(AugmentedArtifact.from_gzip_bytes(other).to_bytes()),
                artifact_sha256=digest,
            )
        }
    )
    assert artifact.set_code == "tst"
    client, _ = _client(tmp_path, objects={object_url: other}, manifest=manifest)
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False
    assert not client.cache_path("tst").exists()


def test_byte_size_mismatch_returns_invalid(tmp_path: Path) -> None:
    payload, digest, size, manifest = _served()
    wrong = dict(manifest["sets"])
    entry = dict(wrong["tst"])
    entry["artifact_bytes"] = size + 1
    wrong["tst"] = entry
    manifest = dict(manifest)
    manifest["sets"] = wrong
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    client, _ = _client(tmp_path, objects={object_url: payload}, manifest=manifest)
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False
    assert not client.cache_path("tst").exists()


def test_non_canonical_container_returns_invalid(tmp_path: Path) -> None:
    raw = augmented_artifact().to_bytes()
    stream = io.BytesIO()
    with gzip.GzipFile(
        fileobj=stream, mode="wb", filename="custom", mtime=7, compresslevel=6
    ) as writer:
        writer.write(raw)
    payload = stream.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    manifest = augmented_manifest_json(
        sets={
            "tst": augmented_manifest_entry_json(
                artifact_bytes=len(raw), artifact_sha256=digest
            )
        }
    )
    client, _ = _client(tmp_path, objects={object_url: payload}, manifest=manifest)
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False
    assert not client.cache_path("tst").exists()


def test_fetch_failures_return_unreachable(tmp_path: Path) -> None:
    manifest_client, _ = _client(
        tmp_path, objects={}, error_urls=(AUGMENTED_MANIFEST_URL,)
    )
    manifest_failed = manifest_client.load("tst", allow_network=True)
    assert manifest_failed.outcome is AugmentedModelOutcome.UNREACHABLE
    assert manifest_failed.available is False

    payload, digest, size, manifest = _served()
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    object_client, _ = _client(
        tmp_path / "objects",
        objects={},
        manifest=manifest,
        error_urls=(object_url,),
    )
    object_failed = object_client.load("tst", allow_network=True)
    assert object_failed.outcome is AugmentedModelOutcome.UNREACHABLE
    assert object_failed.available is False


def test_http_503_returns_unreachable(tmp_path: Path) -> None:
    payload, digest, size, manifest = _served()
    client, _ = _client(
        tmp_path, objects={}, manifest=manifest, manifest_status=503
    )
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.UNREACHABLE
    assert loaded.available is False


def test_cross_origin_redirect_returns_invalid(tmp_path: Path) -> None:
    payload, digest, size, manifest = _served()
    client, calls = _client(tmp_path, objects={}, manifest=manifest)

    def evil_opener(request: Any, *, timeout: float) -> _Response:
        calls.append({"request": request, "timeout": timeout})
        if request.full_url == AUGMENTED_MANIFEST_URL:
            return _Response(canonical_bytes(manifest), url=request.full_url)
        return _Response(payload, url="https://evil.example/tst.json.gz")

    evil = AugmentedModelClient(app_dir=tmp_path, opener=evil_opener)
    loaded = evil.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False
    assert not evil.cache_path("tst").exists()


def test_compressed_limit_returns_invalid(tmp_path: Path) -> None:
    payload, digest, size, manifest = _served()
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    client, _ = _client(
        tmp_path,
        objects={object_url: payload},
        manifest=manifest,
        max_compressed_bytes=len(payload) - 1,
    )
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False


def test_decompressed_limit_skips_object_request(tmp_path: Path) -> None:
    payload, digest, size, manifest = _served()
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    client, calls = _client(
        tmp_path,
        objects={object_url: payload},
        manifest=manifest,
        max_decompressed_bytes=size - 1,
    )
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False
    assert [item["request"].full_url for item in calls] == [AUGMENTED_MANIFEST_URL]


def test_offline_missing_cache_is_unreachable(tmp_path: Path) -> None:
    loaded = AugmentedModelClient(app_dir=tmp_path).load("tst", allow_network=False)
    assert loaded.outcome is AugmentedModelOutcome.UNREACHABLE
    assert loaded.available is False
    assert "network access is disabled" in loaded.message


def test_unsafe_set_code_raises(tmp_path: Path) -> None:
    with pytest.raises(AugmentedModelClientError):
        AugmentedModelClient(app_dir=tmp_path).load("../tst", allow_network=False)
    with pytest.raises(AugmentedModelClientError):
        augmented_cache_path(set_code="", app_dir=tmp_path)


@pytest.mark.parametrize("format_name", list(SUPPORTED_FORMATS))
def test_lookup_ignores_draft_format(tmp_path: Path, format_name: str) -> None:
    assert format_name in ("PremierDraft", "TradDraft", "QuickDraft", "PickTwoDraft")
    payload, digest, size, _ = _served(set_code="hob")
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    manifest = augmented_manifest_json(
        sets={
            "hob": augmented_manifest_entry_json(
                artifact_bytes=size,
                artifact_sha256=digest,
                event_type="QuickDraft",
            )
        }
    )
    client, _ = _client(
        tmp_path / format_name, objects={object_url: payload}, manifest=manifest
    )
    loaded = client.load("hob", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.DOWNLOADED
    assert loaded.available is True
    assert loaded.artifact is not None
    assert hashlib.sha256(payload).hexdigest() == digest
    assert client.cache_path("HOB") == client.cache_path("hob")
    assert client.cache_path("hob").read_bytes() == payload


def test_quickdraft_entry_selected_for_same_set(tmp_path: Path) -> None:
    payload, digest, size, _ = _served(set_code="hob")
    object_url = f"{AUGMENTED_OBJECTS_BASE_URL}{digest}.json.gz"
    manifest = augmented_manifest_json(
        sets={
            "hob": augmented_manifest_entry_json(
                artifact_bytes=size,
                artifact_sha256=digest,
                event_type="QuickDraft",
            )
        }
    )
    client, _ = _client(tmp_path, objects={object_url: payload}, manifest=manifest)
    loaded = client.load("HOB", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.DOWNLOADED
    assert loaded.available is True


def test_non_canonical_manifest_returns_invalid(tmp_path: Path) -> None:
    raw = canonical_bytes(augmented_manifest_json()).decode("utf-8")
    parsed = json.loads(raw)
    parsed["published_at"] = "not-a-timestamp"
    client, _ = _client(tmp_path, objects={}, manifest=parsed)
    loaded = client.load("tst", allow_network=True)
    assert loaded.outcome is AugmentedModelOutcome.INVALID
    assert loaded.available is False
