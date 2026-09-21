from __future__ import annotations

from pathlib import Path

import pytest

from draftomen.augmented_manifest import (
    AugmentedManifest,
    AugmentedManifestSchemaError,
    dump_augmented_manifest,
    load_augmented_manifest,
)
from tests.augmented_artifacts import (
    augmented_manifest_entry_json,
    augmented_manifest_json,
    canonical_bytes,
)


def _manifest_bytes(value: dict) -> bytes:
    return canonical_bytes(value)


def test_manifest_parses_hob_entry() -> None:
    entry = augmented_manifest_entry_json()
    manifest = AugmentedManifest.from_bytes(_manifest_bytes(augmented_manifest_json()))
    selected = manifest.select(set_code="hob")
    assert selected is not None
    assert selected.artifact_sha256 == entry["artifact_sha256"]
    assert selected.artifact_schema_version == entry["artifact_schema_version"]
    assert selected.compatibility == entry["compatibility"]
    assert selected.source.to_json() == entry["source"]
    assert selected.metrics.to_json() == entry["metrics"]
    assert manifest.entry_set_codes() == ("hob",)


def test_select_normalizes_case_and_misses_unknown() -> None:
    manifest = AugmentedManifest.from_bytes(_manifest_bytes(augmented_manifest_json()))
    assert manifest.select(set_code="HOB") is not None
    assert manifest.select(set_code="lci") is None


def test_select_ignores_event_type() -> None:
    manifest = AugmentedManifest.from_bytes(
        _manifest_bytes(
            augmented_manifest_json(
                sets={"hob": augmented_manifest_entry_json(event_type="QuickDraft")}
            )
        )
    )
    assert manifest.select(set_code="hob") is not None


def test_to_bytes_is_canonical_and_round_trips() -> None:
    manifest = AugmentedManifest.from_bytes(_manifest_bytes(augmented_manifest_json()))
    raw = manifest.to_bytes()
    assert raw.endswith(b"\n")
    assert b"\n" not in raw[:-1]
    assert raw == manifest.to_bytes()
    assert AugmentedManifest.from_bytes(raw) == manifest


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda value: value.update(schema_version=2), "Unsupported augmented manifest schema"),
        (lambda value: value.update(extra=1), "unsupported fields"),
        (lambda value: value["sets"]["hob"].update(extra=1), "unsupported fields"),
        (lambda value: value.update(sets={}), "at least one set entry"),
        (lambda value: value.update(sets={"HOB": value["sets"]["hob"]}), "lowercase path-safe"),
        (
            lambda value: value["sets"]["hob"].update(artifact_sha256="zzz"),
            "SHA-256",
        ),
        (lambda value: value["sets"]["hob"]["metrics"].update(picks=0), "positive integer"),
        (
            lambda value: value["sets"]["hob"]["metrics"]["basic_do"].update(top_1=2.0),
            "within \\[0.0, 1.0\\]",
        ),
        (
            lambda value: value["sets"]["hob"]["source"].update(url="http://example.com/x"),
            "HTTPS",
        ),
        (
            lambda value: value.update(published_at="2026-09-21T00:00:00"),
            "timezone",
        ),
    ],
)
def test_manifest_rejections(mutate, match: str) -> None:
    value = augmented_manifest_json()
    mutate(value)
    with pytest.raises(AugmentedManifestSchemaError, match=match):
        AugmentedManifest.from_bytes(_manifest_bytes(value))


def test_duplicate_json_keys_rejected() -> None:
    raw = _manifest_bytes(augmented_manifest_json()).decode("utf-8")
    corrupted = raw.replace('"schema_version":1', '"schema_version":1,"schema_version":1', 1)
    with pytest.raises(AugmentedManifestSchemaError, match="Duplicate"):
        AugmentedManifest.from_bytes(corrupted.encode("utf-8"))


def test_dump_and_load_round_trip(tmp_path: Path) -> None:
    manifest = AugmentedManifest.from_bytes(_manifest_bytes(augmented_manifest_json()))
    target = tmp_path / "manifest.json"
    assert dump_augmented_manifest(manifest, target) == target
    assert load_augmented_manifest(target) == manifest


def test_dump_leaves_no_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = AugmentedManifest.from_bytes(_manifest_bytes(augmented_manifest_json()))
    target = tmp_path / "manifest.json"

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("draftomen.augmented_manifest.os.replace", _boom)
    with pytest.raises(Exception):
        dump_augmented_manifest(manifest, target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
