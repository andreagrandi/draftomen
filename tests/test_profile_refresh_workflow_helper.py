from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

import draftomen.profile_data_refresh as profile_refresh_module
import scripts.profile_refresh_workflow as workflow
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_generation import generate_set_profile
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestArtifact,
    load_profile_manifest,
)
from draftomen.set_card_data import SetCardData
from draftomen.seventeen import (
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsError,
    SeventeenLandsFormatData,
    save_17lands_format_data,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _database(*, set_code: str, set_name: str) -> CardDatabase:
    return CardDatabase(
        cards={
            1: CardInfo(
                grp_id=1,
                name=f"{set_name} Card",
                colors=("W", "U"),
                mana_value=2,
                rarity="common",
                types=("Creature",),
                type_line="Creature — Wizard",
                set_code=set_code,
                arena_id=1,
            )
        }
    )


def _static(directory: Path, *, set_code: str, set_name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{set_code}.json.gz"
    path.write_bytes(
        SetCardData.from_card_database(
            _database(set_code=set_code, set_name=set_name),
            set_code=set_code,
            set_name=set_name,
        ).to_gzip_bytes()
    )
    return path


def _source(root: Path) -> tuple[Path, Path]:
    inventory = root / "inventory.json"
    bulk = root / "default-cards.jsonl"
    inventory.write_text(json.dumps(["OLD", "NEW"]), encoding="utf-8")
    cards = [
        {
            "arena_id": 1,
            "name": "Old Card",
            "colors": ["W", "U"],
            "cmc": 2,
            "rarity": "common",
            "type_line": "Creature — Wizard",
            "set": "old",
            "set_name": "Old Set",
            "collector_number": "1",
        },
        {
            "arena_id": 1,
            "name": "New Card",
            "colors": ["W", "U"],
            "cmc": 2,
            "rarity": "common",
            "type_line": "Creature — Wizard",
            "set": "new",
            "set_name": "New Set",
            "collector_number": "1",
        },
    ]
    bulk.write_text("".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8")
    return inventory, bulk


def _ratings(*, event_format: str = "PremierDraft") -> SeventeenLandsFormatData:
    return SeventeenLandsFormatData(
        set_code="NEW",
        event_format=event_format,
        fetched_at=NOW,
        card_ratings={
            1: SeventeenCardStats(
                grp_id=1,
                name="New Card",
                color="WU",
                rarity="common",
                average_last_seen_at=3.0,
                gih_win_rate=0.60,
                opening_hand_win_rate=0.55,
                drawn_improvement_win_rate=0.58,
                sample_counts=RatingSampleCounts(100, 80, 60, 40, 30),
            )
        },
        pair_win_rates={
            "WU": ColorPairWinRate(pair="WU", wins=60, games=100, win_rate=0.60)
        },
    )


def _manifest(root: Path) -> None:
    generation = generate_set_profile(
        set_code="old",
        event_format="PremierDraft",
        stage="metadata",
        card_database=_database(set_code="old", set_name="Old Set"),
        generated_at=NOW,
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
    profiles = root / "website/public/profiles"
    profiles.mkdir(parents=True)
    (profiles / "manifest.json").write_bytes(
        ProfileManifest(artifacts=(artifact,), published_at=NOW.isoformat()).to_bytes()
    )
    objects = profiles / "objects"
    objects.mkdir()
    (objects / f"{report.gzip_sha256}.json.gz").write_bytes(generation.gzip_bytes)


def test_generate_uses_real_producers_and_preserves_valid_static(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    monkeypatch.setattr(workflow, "_base_bytes", lambda *args, **kwargs: None)
    root = tmp_path / "checkout"
    card_dir = root / "website/public/card-data"
    old = _static(card_dir, set_code="old", set_name="Old Set")
    old_bytes = old.read_bytes()
    old_mtime = old.stat().st_mtime_ns
    inventory, bulk = _source(tmp_path)
    _manifest(root)
    cache = tmp_path / "cache"
    save_17lands_format_data(_ratings(), app_dir=cache)
    calls: list[str] = []

    def fetch_json(url: str, timeout: int) -> dict[str, Any]:
        calls.append(url)
        return {
            "formats_by_expansion": {"NEW": ["PremierDraft"]},
            "live_formats_by_expansion": {},
        }

    bundle = tmp_path / "bundle"
    report = workflow.generate_website(
        base_commit="base",
        selection_mode="one",
        selector="new",
        repo_root=root,
        bundle_dir=bundle,
        cache_dir=cache,
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=fetch_json,
        clock=lambda: NOW,
    )

    assert report["status"] == "success"
    assert report["selection"] == {"mode": "one", "selector": "new"}
    assert report["static"]["selected"] == [{"set_code": "new", "set_name": "New Set"}]
    assert report["profiles"]["selected"][0]["set_code"] == "new"
    assert report["profiles"]["successful"][0]["event_format"] == "PremierDraft"
    assert old.read_bytes() == old_bytes
    assert old.stat().st_mtime_ns == old_mtime
    assert (bundle / "generated/website/public/card-data/new.json.gz").is_file()
    assert (bundle / "generated/website/public/profiles/manifest.json").is_file()
    assert calls == ["https://www.17lands.com/data/filters"]
    assert json.loads((bundle / "result.json").read_text(encoding="utf-8"))["schema_version"] == 1


def test_failed_static_write_keeps_profile_pair_selected_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    monkeypatch.setattr(workflow, "_base_bytes", lambda *args, **kwargs: None)
    root = tmp_path / "checkout"
    _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    inventory, bulk = _source(tmp_path)
    _manifest(root)

    def fail_new(*, candidate: Any) -> Path:
        if candidate.identity.set_code == "new":
            raise OSError("injected replace failure")
        return candidate.target_path

    monkeypatch.setattr(workflow, "publish_set_data_export", fail_new)
    report = workflow.generate_website(
        base_commit="base",
        selection_mode="all",
        selector=None,
        repo_root=root,
        bundle_dir=tmp_path / "bundle",
        cache_dir=tmp_path / "cache",
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=lambda _url, _timeout: {
            "formats_by_expansion": {"NEW": ["PremierDraft"]},
            "live_formats_by_expansion": {},
        },
        clock=lambda: NOW,
    )

    assert report["status"] == "failed"
    assert report["profiles"]["selected"][0]["set_code"] == "new"
    assert any(
        failure["stage"] == "static-write" and failure["set_code"] == "new"
        for failure in report["failures"]
    )
    assert any(
        failure["category"] == "static-artifact-invalid"
        and failure["set_code"] == "new"
        for failure in report["failures"]
    )
    assert (tmp_path / "bundle/result.json").is_file()
    assert (tmp_path / "bundle/summary.md").is_file()


def test_summary_escapes_report_metadata_and_lists_selected_work() -> None:
    summary = workflow.render_summary(
        {
            "status": "failed",
            "base_commit": "<secret>&",
            "selection": {"mode": "one", "selector": "<set>"},
            "static": {
                "discovery_complete": True,
                "eligible_count": 1,
                "already_valid_count": 0,
                "selected": [{"set_code": "new", "set_name": "<New>"}],
                "successful": [],
            },
            "profiles": {
                "planning_complete": True,
                "selected": [
                    {"set_code": "new", "set_name": "<New>", "event_format": "PremierDraft"}
                ],
                "successful": [],
                "manifest_changed": False,
            },
            "failures": [
                {"stage": "profile-execution", "category": "<unsafe>", "set_code": "new"}
            ],
        }
    )
    assert "&lt;secret&gt;&amp;" in summary
    assert "&lt;New&gt;" in summary
    assert "<unsafe>" not in summary
    assert "Card data from 17Lands" in summary
    assert "new / PremierDraft /" in summary


def test_cli_rejects_invalid_selector_combinations(tmp_path: Path) -> None:
    assert workflow.main(
        [
            "generate",
            "--selection-mode",
            "one",
            "--base-commit",
            "base",
            "--bundle-dir",
            str(tmp_path / "bundle"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    ) == 2
    assert workflow.main(
        [
            "generate",
            "--selection-mode",
            "active",
            "--set",
            "new",
            "--base-commit",
            "base",
            "--bundle-dir",
            str(tmp_path / "bundle"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    ) == 2


def test_mixed_profile_failures_merge_manifest_and_preserve_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    monkeypatch.setattr(workflow, "_base_bytes", lambda *args, **kwargs: None)
    root = tmp_path / "checkout"
    _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    inventory, bulk = _source(tmp_path)
    _manifest(root)
    cache = tmp_path / "cache"
    save_17lands_format_data(_ratings(), app_dir=cache)
    original_loader = profile_refresh_module.load_or_refresh_17lands_format_data

    def load_ratings(**kwargs: Any) -> Any:
        if kwargs["event_format"] == "TradDraft":
            raise SeventeenLandsError("injected ratings outage")
        return original_loader(**kwargs)

    monkeypatch.setattr(
        profile_refresh_module,
        "load_or_refresh_17lands_format_data",
        load_ratings,
    )
    report = workflow.generate_website(
        base_commit="base",
        selection_mode="all",
        selector=None,
        repo_root=root,
        bundle_dir=tmp_path / "bundle",
        cache_dir=cache,
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=lambda _url, _timeout: {
            "formats_by_expansion": {"NEW": ["PremierDraft", "TradDraft"]},
            "live_formats_by_expansion": {},
        },
        clock=lambda: NOW,
    )

    assert report["status"] == "failed"
    assert {pair["event_format"] for pair in report["profiles"]["successful"]} == {"PremierDraft"}
    assert any(
        failure["category"] == "ratings-unavailable"
        and failure["event_format"] == "TradDraft"
        for failure in report["failures"]
    )
    manifest = load_profile_manifest(root / "website/public/profiles/manifest.json")
    assert manifest.select(set_code="old", event_format="PremierDraft") is not None
    new_artifact = manifest.select(set_code="new", event_format="PremierDraft")
    assert new_artifact is not None
    assert (tmp_path / "bundle/result.json").is_file()
    object_paths = list((tmp_path / "bundle/generated/website/public/profiles/objects").glob("*"))
    assert [path.name for path in object_paths] == [f"{new_artifact.gzip_sha256}.json.gz"]


def test_source_and_filter_outages_are_explicit_in_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "checkout"
    _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    monkeypatch.setattr(
        workflow,
        "prepare_set_data_export",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("source outage")),
    )
    report = workflow.generate_website(
        base_commit="base",
        selection_mode="all",
        selector=None,
        repo_root=root,
        bundle_dir=tmp_path / "bundle",
        cache_dir=tmp_path / "cache",
        fetch_json=lambda _url, _timeout: (_ for _ in ()).throw(RuntimeError("filters outage")),
        clock=lambda: NOW,
    )

    assert report["status"] == "failed"
    assert report["static"]["discovery_complete"] is False
    assert report["static"]["eligible_count"] is None
    assert {failure["stage"] for failure in report["failures"]} == {
        "static-discovery",
        "profile-planning",
    }
    assert (tmp_path / "bundle/result.json").is_file()
    assert (tmp_path / "bundle/summary.md").is_file()


def test_active_historical_modes_reuse_fresh_aggregate_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    monkeypatch.setattr(workflow, "_base_bytes", lambda *args, **kwargs: None)
    root = tmp_path / "checkout"
    _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    inventory, bulk = _source(tmp_path)
    _manifest(root)
    cache = tmp_path / "cache"
    save_17lands_format_data(_ratings(event_format="PremierDraft"), app_dir=cache)
    save_17lands_format_data(_ratings(event_format="TradDraft"), app_dir=cache)
    requests: list[str] = []

    def fetch_json(url: str, _timeout: int) -> dict[str, Any]:
        requests.append(url)
        return {
            "formats_by_expansion": {"NEW": ["PremierDraft", "TradDraft"]},
            "live_formats_by_expansion": {"NEW": ["PremierDraft"]},
        }

    common = dict(
        base_commit="base",
        selector=None,
        repo_root=root,
        bundle_dir=tmp_path / "bundle",
        cache_dir=cache,
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=fetch_json,
        clock=lambda: NOW,
    )
    active = workflow.generate_website(selection_mode="active", **common)
    historical = workflow.generate_website(selection_mode="historical", **common)

    assert [pair["event_format"] for pair in active["profiles"]["selected"]] == ["PremierDraft"]
    assert [pair["event_format"] for pair in historical["profiles"]["selected"]] == ["TradDraft"]
    assert active["status"] == historical["status"] == "success"
    assert requests == ["https://www.17lands.com/data/filters"] * 2


def _git_base_commit(root: Path) -> str:
    subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "add", "website"], cwd=root, check=True, stdout=subprocess.PIPE)
    tree = subprocess.run(
        ["git", "write-tree"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    environment = os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    return subprocess.run(
        ["git", "commit-tree", tree, "-m", "base"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        env=environment,
    ).stdout.strip()


def test_delta_bundle_compares_generated_assets_to_base_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    root = tmp_path / "checkout"
    old = _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    old_bytes = old.read_bytes()
    old_mtime = old.stat().st_mtime_ns
    inventory, bulk = _source(tmp_path)
    _manifest(root)
    base_commit = _git_base_commit(root)
    cache = tmp_path / "cache"
    bundle = tmp_path / "bundle"
    save_17lands_format_data(_ratings(), app_dir=cache)
    requests: list[str] = []

    def fetch_json(url: str, _timeout: int) -> dict[str, Any]:
        requests.append(url)
        return {
            "formats_by_expansion": {"NEW": ["PremierDraft"]},
            "live_formats_by_expansion": {},
        }

    common = dict(
        base_commit=base_commit,
        selection_mode="all",
        selector=None,
        repo_root=root,
        bundle_dir=bundle,
        cache_dir=cache,
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=fetch_json,
        clock=lambda: NOW,
    )
    first = workflow.generate_website(**common)

    assert first["status"] == "success"
    first_paths = {asset["path"] for asset in first["generated_assets"]}
    new_artifact = load_profile_manifest(
        root / "website/public/profiles/manifest.json"
    ).select(set_code="new", event_format="PremierDraft")
    assert new_artifact is not None
    expected_paths = {
        "website/public/card-data/new.json.gz",
        "website/public/profiles/manifest.json",
        f"website/public/profiles/objects/{new_artifact.gzip_sha256}.json.gz",
    }
    assert first_paths == expected_paths
    first_descriptors = first["generated_assets"]
    first_bundle_assets = {
        path: (bundle / "generated" / path).read_bytes()
        for path in first_paths
    }
    tracked_paths = tuple(expected_paths)
    first_state = {
        path: (
            (root / path).read_bytes(),
            (root / path).stat().st_mtime_ns,
        )
        for path in tracked_paths
    }

    second = workflow.generate_website(**common)

    assert second["status"] == "success"
    assert second["generated_assets"] == first_descriptors
    second_paths = {asset["path"] for asset in second["generated_assets"]}
    assert second_paths == expected_paths
    second_bundle_assets = {
        path: (bundle / "generated" / path).read_bytes()
        for path in second_paths
    }
    assert second_bundle_assets == first_bundle_assets
    for path, (payload, mtime) in first_state.items():
        assert (root / path).read_bytes() == payload
        assert (root / path).stat().st_mtime_ns == mtime
    assert old.read_bytes() == old_bytes
    assert old.stat().st_mtime_ns == old_mtime
    assert requests == ["https://www.17lands.com/data/filters"] * 2
