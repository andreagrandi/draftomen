from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

import scripts.profile_refresh_workflow as workflow
from draftomen.card_data_export import build_card_database_from_scryfall_cards
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_generation import generate_set_profile
from draftomen.profile_refresh_execution import DEFAULT_PROFILE_REFRESH_CACHE_POLICY
from draftomen.profile_input_acquisition import (
    CardMetadataAdapter,
    SeventeenLandsPublicDraftAdapter,
    SeventeenLandsRatingsAdapter,
)
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestArtifact,
    load_profile_manifest,
)
from draftomen.set_profile import SetProfile
from draftomen.set_card_data import SetCardData
from draftomen.seventeen import (
    CARD_RATINGS_ENDPOINT,
    COLOR_RATINGS_ENDPOINT,
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsError,
    SeventeenLandsFormatData,
    card_ratings_url,
    color_ratings_url,
    fetch_17lands_format_data,
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
            "arena_id": 1002,
            "oracle_id": "00000000-0000-4000-8000-000000001002",
            "name": "Fixture Thin Quick Card",
            "colors": ["U"],
            "cmc": 2,
            "rarity": "uncommon",
            "type_line": "Creature — Wizard",
            "oracle_text": "When this creature enters the battlefield, draw a card.",
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


def _fixture_adapters(
    *,
    fail_formats: frozenset[str] = frozenset(),
    change_formats: frozenset[str] = frozenset(),
) -> tuple[CardMetadataAdapter, SeventeenLandsRatingsAdapter, SeventeenLandsPublicDraftAdapter, list[str], list[str]]:
    """Build deterministic adapters backed by the recorded 17Lands payloads."""

    ratings_requests: list[str] = []
    public_draft_calls: list[str] = []
    failed = frozenset(value.casefold() for value in fail_formats)
    changed = frozenset(value.casefold() for value in change_formats)
    card = {
        "arena_id": 1002,
        "oracle_id": "00000000-0000-4000-8000-000000001002",
        "name": "Fixture Thin Quick Card",
        "colors": ["U"],
        "cmc": 2,
        "rarity": "uncommon",
        "type_line": "Creature — Wizard",
        "oracle_text": "When this creature enters the battlefield, draw a card.",
        "set": "new",
        "set_name": "New Set",
        "collector_number": "1",
    }

    def fetch_database(*, set_code: str, timeout_seconds: int) -> CardDatabase:
        del timeout_seconds
        assert set_code.casefold() == "new"
        return build_card_database_from_scryfall_cards(cards=(card,))

    card_adapter = CardMetadataAdapter(fetch_database=fetch_database)
    card_payload = json.loads(
        (Path(__file__).parent / "fixtures/17lands-card-ratings-premier.json").read_text(
            encoding="utf-8"
        )
    )
    color_payload = json.loads(
        (Path(__file__).parent / "fixtures/17lands-color-ratings.json").read_text(
            encoding="utf-8"
        )
    )

    def fetch_ratings(
        *,
        set_code: str,
        event_format: str,
        fetched_at: datetime,
        timeout_seconds: int,
    ) -> SeventeenLandsFormatData:
        normalized_format = event_format.casefold()
        if normalized_format in failed:
            raise SeventeenLandsError("fixture ratings outage")

        def fetch_json(url: str, timeout: int) -> Any:
            del timeout
            ratings_requests.append(url)
            if url == card_ratings_url(set_code=set_code, event_format=event_format):
                return card_payload
            if url == color_ratings_url(set_code=set_code, event_format=event_format):
                return color_payload
            pytest.fail(f"unexpected ratings URL: {url}")

        ratings = fetch_17lands_format_data(
            set_code=set_code,
            event_format=event_format,
            fetched_at=fetched_at,
            fetch_json=fetch_json,
            timeout_seconds=timeout_seconds,
        )
        if normalized_format not in changed:
            return ratings
        card_ratings = dict(ratings.card_ratings)
        grp_id = next(iter(card_ratings))
        card_ratings[grp_id] = replace(
            card_ratings[grp_id],
            gih_win_rate=0.61,
        )
        return replace(ratings, card_ratings=card_ratings)

    ratings_adapter = SeventeenLandsRatingsAdapter(fetch_ratings=fetch_ratings)

    def fetch_public_drafts(**kwargs: Any) -> None:
        public_draft_calls.append("fetch")
        pytest.fail("hosted producer must not fetch public drafts")

    class FailOnPublicDraftSource(SeventeenLandsPublicDraftAdapter):
        def source_for(self, *, environment: Any) -> Any:
            public_draft_calls.append("source")
            pytest.fail("hosted producer must not inspect public-draft source")

    public_adapter = FailOnPublicDraftSource(fetch_public_drafts=fetch_public_drafts)
    return card_adapter, ratings_adapter, public_adapter, ratings_requests, public_draft_calls


def _candidate_from_base(source: Path, destination: Path, base_commit: str) -> None:
    shutil.copytree(source, destination)
    subprocess.run(
        ["git", "reset", "--hard", base_commit],
        cwd=destination,
        check=True,
        stdout=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "clean", "-fdx"],
        cwd=destination,
        check=True,
        stdout=subprocess.PIPE,
    )


def _git_commit(root: Path, message: str) -> str:
    subprocess.run(
        ["git", "add", "--", "website"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    environment = os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _producer_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    generator = tmp_path / "generator"
    _static(generator / "website/public/card-data", set_code="old", set_name="Old Set")
    inventory, bulk = _source(tmp_path)
    _manifest(generator)
    base_commit = _git_base_commit(generator)
    cache = tmp_path / "cache"
    card_adapter, ratings_adapter, public_adapter, _, first_public_calls = _fixture_adapters()
    bundle = tmp_path / "bundle"

    def fetch_json(url: str, timeout: int) -> Any:
        del timeout
        if url == "https://www.17lands.com/data/filters":
            return {
                "formats_by_expansion": {"NEW": ["PremierDraft"]},
                "live_formats_by_expansion": {},
            }
        pytest.fail(f"unexpected workflow URL: {url}")

    report = workflow.generate_website(
        base_commit=base_commit,
        selection_mode="one",
        selector="new",
        repo_root=generator,
        bundle_dir=bundle,
        cache_dir=cache,
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=fetch_json,
        clock=lambda: NOW,
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )
    assert first_public_calls == []
    candidate = tmp_path / "candidate"
    _candidate_from_base(generator, candidate, base_commit)
    return generator, candidate, bundle, report


def _partial_producer_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, str, dict[str, Any]]:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    root = tmp_path / "generator"
    _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    inventory, bulk = _source(tmp_path)
    _manifest(root)
    base_commit = _git_base_commit(root)
    base_cache = tmp_path / "cache"
    card_adapter, ratings_adapter, public_adapter, _, first_public_calls = _fixture_adapters()
    filters = {
        "formats_by_expansion": {"NEW": ["PremierDraft", "TradDraft"]},
        "live_formats_by_expansion": {},
    }

    def fetch_json(url: str, timeout: int) -> Any:
        del timeout
        if url == "https://www.17lands.com/data/filters":
            return filters
        pytest.fail(f"unexpected workflow URL: {url}")

    first = workflow.generate_website(
        base_commit=base_commit,
        selection_mode="all",
        selector=None,
        repo_root=root,
        bundle_dir=tmp_path / "first-bundle",
        cache_dir=base_cache,
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=fetch_json,
        clock=lambda: NOW,
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )
    assert first_public_calls == []
    assert first["status"] == "success"
    assert {pair["event_format"] for pair in first["profiles"]["successful"]} == {
        "PremierDraft",
        "TradDraft",
    }
    published_base = _git_commit(root, "publish initial profile pair")
    second_cache = tmp_path / "second-cache"
    (
        card_adapter,
        ratings_adapter,
        public_adapter,
        _,
        second_public_calls,
    ) = _fixture_adapters(
        fail_formats=frozenset({"TradDraft"}),
        change_formats=frozenset({"PremierDraft"}),
    )
    report = workflow.generate_website(
        base_commit=published_base,
        selection_mode="all",
        selector=None,
        repo_root=root,
        bundle_dir=tmp_path / "bundle",
        cache_dir=second_cache,
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=fetch_json,
        clock=lambda: NOW,
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )
    assert second_public_calls == []
    candidate = tmp_path / "candidate"
    _candidate_from_base(root, candidate, published_base)
    return root, candidate, tmp_path / "bundle", published_base, report


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
    card_adapter, ratings_adapter, public_adapter, _, public_calls = _fixture_adapters()
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
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )

    assert report["status"] == "success"
    assert report["selection"] == {"mode": "one", "selector": "new"}
    assert report["static"]["selected"] == [{"set_code": "new", "set_name": "New Set"}]
    assert report["profiles"]["selected"][0]["set_code"] == "new"
    assert report["profiles"]["successful"][0]["event_format"] == "PremierDraft"
    assert old.read_bytes() == old_bytes
    assert old.stat().st_mtime_ns == old_mtime
    assert public_calls == []
    assert (bundle / "generated/website/public/card-data/new.json.gz").is_file()
    assert (bundle / "generated/website/public/profiles/manifest.json").is_file()
    assert calls == ["https://www.17lands.com/data/filters"]
    assert json.loads((bundle / "result.json").read_text(encoding="utf-8"))["schema_version"] == 1


def test_network_inventory_is_fetched_once_and_batch_provenance_is_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    monkeypatch.setattr(workflow, "_base_bytes", lambda *args, **kwargs: None)
    root = tmp_path / "checkout"
    _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    _inventory_file, bulk = _source(tmp_path)
    _manifest(root)
    card_adapter, ratings_adapter, public_adapter, rating_calls, public_calls = _fixture_adapters()
    requests: list[str] = []

    def fetch_json(url: str, _timeout: int) -> Any:
        requests.append(url)
        if url == "https://www.17lands.com/data/expansions":
            return ["OLD", "NEW"]
        if url == "https://www.17lands.com/data/filters":
            return {
                "formats_by_expansion": {"NEW": ["PremierDraft"]},
                "live_formats_by_expansion": {},
            }
        pytest.fail(f"unexpected workflow URL: {url}")

    report = workflow.generate_website(
        base_commit="base",
        selection_mode="one",
        selector="new",
        repo_root=root,
        bundle_dir=tmp_path / "bundle",
        cache_dir=tmp_path / "cache",
        bulk_file=bulk,
        fetch_json=fetch_json,
        clock=lambda: NOW,
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )
    assert report["status"] == "success"
    assert requests == [
        "https://www.17lands.com/data/expansions",
        "https://www.17lands.com/data/filters",
    ]
    assert len(rating_calls) == 2
    assert public_calls == []
    batch_reports = list((tmp_path / "bundle/batch-reports").glob("*.json"))
    assert len(batch_reports) == 1
    batch_path = batch_reports[0]
    batch_text = batch_path.read_text(encoding="utf-8")
    batch = json.loads(batch_text)
    assert batch_path.name == f'{batch["plan_sha256"]}.json'
    environment = batch["environments"][0]
    assert environment["environment"] == {
        "event_format": "premierdraft",
        "lifecycle": None,
        "set_code": "NEW",
    }
    assert environment["selection"]["stage"] == "early"
    ratings_source = next(
        source
        for source in environment["sources"]
        if source["role"] == "seventeen_lands_ratings"
    )
    assert set(ratings_source) == {
        "acquired_at",
        "attribution",
        "fallback_state",
        "license",
        "name",
        "outcome",
        "requested_format",
        "requested_set",
        "role",
        "sha256",
        "source_format",
        "source_version",
    }
    assert ratings_source["name"] == "17lands-ratings"
    assert ratings_source["requested_set"] == "NEW"
    assert ratings_source["requested_format"] == "premierdraft"
    assert ratings_source["source_format"] == "premierdraft"
    assert ratings_source["outcome"] == "acquired"
    assert ratings_source["fallback_state"] == "none"
    assert ratings_source["acquired_at"] == NOW.isoformat()
    assert ratings_source["source_version"].endswith(ratings_source["sha256"])
    assert len(ratings_source["sha256"]) == 64
    assert "http" not in batch_text
    assert str(tmp_path) not in batch_text




def test_stale_verified_cache_remains_empirically_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow, "_base_bytes", lambda *args, **kwargs: None)
    generator, _candidate, first_bundle, _first = _producer_bundle(tmp_path, monkeypatch)
    first_batch = json.loads(
        next((first_bundle / "batch-reports").glob("*.json")).read_text(encoding="utf-8")
    )
    first_source = next(
        source
        for source in first_batch["environments"][0]["sources"]
        if source["role"] == "seventeen_lands_ratings"
    )
    first_manifest = load_profile_manifest(
        generator / "website/public/profiles/manifest.json"
    )
    first_artifact = first_manifest.select(
        set_code="new",
        event_format="PremierDraft",
    )
    assert first_artifact is not None
    first_profile = SetProfile.from_json(
        json.loads(
            gzip.decompress(
                (
                    generator
                    / "website/public/profiles/objects"
                    / f"{first_artifact.gzip_sha256}.json.gz"
                ).read_bytes()
            )
        )
    )
    inventory, bulk = _source(tmp_path)
    card_adapter, ratings_adapter, public_adapter, _, public_calls = _fixture_adapters(
        fail_formats=frozenset({"PremierDraft"})
    )
    report = workflow.generate_website(
        base_commit="base",
        selection_mode="one",
        selector="new",
        repo_root=generator,
        bundle_dir=tmp_path / "stale-bundle",
        cache_dir=tmp_path / "cache",
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=lambda _url, _timeout: {
            "formats_by_expansion": {"NEW": ["PremierDraft"]},
            "live_formats_by_expansion": {},
        },
        clock=lambda: NOW + DEFAULT_PROFILE_REFRESH_CACHE_POLICY.freshness_ttl + timedelta(seconds=1),
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )
    assert report["status"] == "success"
    assert report["profiles"]["successful"][0]["event_format"] == "PremierDraft"
    batch = json.loads(
        next((tmp_path / "stale-bundle/batch-reports").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    ratings_source = next(
        source
        for source in batch["environments"][0]["sources"]
        if source["role"] == "seventeen_lands_ratings"
    )
    assert ratings_source["outcome"] == "stale"
    assert ratings_source["fallback_state"] == "verified-stale-cache"
    assert ratings_source["sha256"] == first_source["sha256"]
    assert ratings_source["acquired_at"] == first_source["acquired_at"]
    stale_manifest = load_profile_manifest(
        generator / "website/public/profiles/manifest.json"
    )
    stale_artifact = stale_manifest.select(
        set_code="new",
        event_format="PremierDraft",
    )
    assert stale_artifact is not None
    stale_profile = SetProfile.from_json(
        json.loads(
            gzip.decompress(
                (
                    generator
                    / "website/public/profiles/objects"
                    / f"{stale_artifact.gzip_sha256}.json.gz"
                ).read_bytes()
            )
        )
    )
    assert stale_profile.card_ratings == first_profile.card_ratings
    assert public_calls == []

@pytest.mark.parametrize("tamper_cache", [False, True])
def test_absent_or_tampered_verified_cache_retains_prior_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_cache: bool,
) -> None:
    monkeypatch.setattr(workflow, "_base_bytes", lambda *args, **kwargs: None)
    generator, _candidate, _bundle, _first = _producer_bundle(tmp_path, monkeypatch)
    inventory, bulk = _source(tmp_path)
    manifest_path = generator / "website/public/profiles/manifest.json"
    prior_manifest = manifest_path.read_bytes()
    prior_objects = {
        path.name: path.read_bytes()
        for path in (generator / "website/public/profiles/objects").glob("*")
    }
    cache_dir = tmp_path / ("cache" if tamper_cache else "missing-cache")
    if tamper_cache:
        records = list((cache_dir / "records").glob("*.json"))
        rating_record = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in records
            if json.loads(path.read_text(encoding="utf-8"))["source"]["event_format"]
            == "premierdraft"
        )
        (cache_dir / "objects" / f'{rating_record["sha256"]}.bin').write_bytes(b"tampered")

    card_adapter, ratings_adapter, public_adapter, _, public_calls = _fixture_adapters(
        fail_formats=frozenset({"PremierDraft"})
    )
    report = workflow.generate_website(
        base_commit="base",
        selection_mode="one",
        selector="new",
        repo_root=generator,
        bundle_dir=tmp_path / "unavailable-bundle",
        cache_dir=cache_dir,
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=lambda _url, _timeout: {
            "formats_by_expansion": {"NEW": ["PremierDraft"]},
            "live_formats_by_expansion": {},
        },
        clock=lambda: NOW,
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )
    assert report["status"] == "failed"
    assert report["profiles"]["successful"] == []
    assert any(
        failure["category"] == "empirical-evidence-unavailable"
        for failure in report["failures"]
    )
    batch = json.loads(
        next((tmp_path / "unavailable-bundle/batch-reports").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    environment = batch["environments"][0]
    assert environment["outcome"] == "publication-eligible"
    assert environment["selection"]["stage"] == "metadata"
    assert manifest_path.read_bytes() == prior_manifest
    assert {
        path.name: path.read_bytes()
        for path in (generator / "website/public/profiles/objects").glob("*")
    } == prior_objects
    assert public_calls == []

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
    card_adapter, ratings_adapter, public_adapter, _, public_calls = _fixture_adapters()

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
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )
    assert report["profiles"]["successful"] == []
    assert public_calls == []

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
    assert not any(failure["category"] == "bundle-failed" for failure in report["failures"])
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
    root, _candidate, bundle, _published_base, report = _partial_producer_bundle(
        tmp_path,
        monkeypatch,
    )
    assert report["status"] == "failed"
    assert {pair["event_format"] for pair in report["profiles"]["successful"]} == {
        "PremierDraft"
    }
    assert any(
        failure["category"] == "empirical-evidence-unavailable"
        and failure["event_format"] == "TradDraft"
        for failure in report["failures"]
    )
    manifest = load_profile_manifest(root / "website/public/profiles/manifest.json")
    assert manifest.select(set_code="old", event_format="PremierDraft") is not None
    assert manifest.select(set_code="new", event_format="PremierDraft") is not None
    assert (bundle / "result.json").is_file()


def test_source_and_filter_outages_are_explicit_in_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "checkout"
    _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    inventory, bulk = _source(tmp_path)
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
        inventory_file=inventory,
        bulk_file=bulk,
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
    card_adapter, ratings_adapter, public_adapter, _, public_calls = _fixture_adapters()
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
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
    )
    active = workflow.generate_website(selection_mode="active", **common)
    historical = workflow.generate_website(selection_mode="historical", **common)

    assert [pair["event_format"] for pair in active["profiles"]["selected"]] == [
        "PremierDraft",
        "TradDraft",
    ]
    assert historical["profiles"]["selected"] == []
    assert active["status"] == historical["status"] == "success"
    assert requests == ["https://www.17lands.com/data/filters"] * 2
    assert public_calls == []


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
    card_adapter, ratings_adapter, public_adapter, _, public_calls = _fixture_adapters()
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
        card_metadata_adapter=card_adapter,
        ratings_adapter=ratings_adapter,
        public_draft_adapter=public_adapter,
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
    assert public_calls == []
    assert old.read_bytes() == old_bytes
    assert old.stat().st_mtime_ns == old_mtime
    assert requests == ["https://www.17lands.com/data/filters"] * 2
