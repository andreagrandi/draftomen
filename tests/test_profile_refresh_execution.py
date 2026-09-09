from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_generation import generate_set_profile
from draftomen.profile_generation_execution import generate_staged_environment_profile
from draftomen.profile_generation_stage_policy import select_profile_generation_stage
from draftomen.profile_input_acquisition import (
    CardMetadataAdapter,
    ProfileAggregateCandidate,
    ProfileBuildBundle,
    ProfileInputAcquisitionOutcome,
    ProfileInputAcquisitionResult,
    ProfileInputSourceReport,
    SeventeenLandsPublicDraftAdapter,
    SeventeenLandsRatingsAdapter,
)
from draftomen.profile_input_cache import (
    ProfileInputCache,
    ProfileInputCacheOutcome,
    ProfileInputCachePolicy,
    ProfileInputSource,
)
from draftomen.public_dump import PublicDumpManifest, PublicDumpSource
from draftomen.refresh_plan import LifecycleMetadata, PlannedEnvironment, RefreshPlan
from draftomen.seventeen import load_17lands_format_data
import draftomen.profile_refresh_execution as execution


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "profile-generation"


def _environment(set_code: str = "TST") -> PlannedEnvironment:
    return PlannedEnvironment(
        set_code=set_code,
        event_format="quickdraft",
        lifecycle="active",
        reasons=("manual-selection",),
    )


def _plan(*set_codes: str) -> RefreshPlan:
    selected = set_codes or ("TST",)
    return RefreshPlan(
        selection_mode="history",
        max_environments=len(selected),
        event_format="quickdraft",
        environments=tuple(_environment(code) for code in selected),
        inventory_source_url="https://inventory.example.test/expansions.json",
        inventory_payload_digest="a" * 64,
        lifecycle=LifecycleMetadata(
            provider="fixture",
            source_url="https://lifecycle.example.test/sets.json",
            version="1",
        ),
    )

def _stage_plan() -> RefreshPlan:
    return replace(
        _plan("META", "EARLY", "MATURE"),
        environments=(
            replace(_environment("META"), lifecycle="historical"),
            replace(_environment("EARLY"), lifecycle="active"),
            replace(_environment("MATURE"), lifecycle="mature"),
        ),
    )


def _staged_generation_acquisition(
    environment: PlannedEnvironment, *, tmp_path: Path, draft_name: str
) -> ProfileInputAcquisitionResult:
    acquired = _acquisition(environment, include_evidence=True, draft_name=draft_name)
    assert acquired.bundle is not None

    source_database = CardDatabase.from_json(
        json.loads((FIXTURE_DIR / "card-database.json").read_text())
    )
    card_database = replace(
        source_database,
        cards={
            grp_id: replace(card, set_code=environment.set_code)
            for grp_id, card in source_database.cards.items()
        },
        generated_at=NOW,
    )
    card_report = replace(
        acquired.source,
        card_count=len(card_database),
    )

    draft_path = tmp_path / "stage-inputs" / f"{environment.set_code}-{draft_name}"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_bytes(
        (FIXTURE_DIR / draft_name).read_bytes().replace(
            b"TST", environment.set_code.encode()
        )
    )
    draft_report = _report(
        environment,
        "17lands-public-drafts",
        drafts=True,
        path=draft_path,
    )
    manifest = PublicDumpManifest(
        sources=(
            PublicDumpSource(
                name="17lands-public-drafts",
                path=draft_path,
                sha256=draft_report.sha256,
                retrieved_at=NOW.isoformat(),
                attribution="fixture",
                license="CC0",
            ),
        )
    )
    bundle = replace(
        acquired.bundle,
        card_database=card_database,
        card_metadata=card_report,
        public_drafts=manifest,
        public_draft_source=draft_report,
    )
    return replace(
        acquired,
        source=card_report,
        bundle=bundle,
        public_draft_source=draft_report,
    )


def test_three_environment_plan_loads_and_generates_explicit_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _stage_plan()

    def acquire(**kwargs: object) -> ProfileInputAcquisitionResult:
        environment = kwargs["environment"]
        assert isinstance(environment, PlannedEnvironment)
        if environment.lifecycle == "historical":
            return _acquisition(environment, include_evidence=False)
        return _staged_generation_acquisition(
            environment,
            tmp_path=tmp_path,
            draft_name=(
                "mature-data.csv"
                if environment.lifecycle == "mature"
                else "early-data.csv"
            ),
        )

    monkeypatch.setattr(execution, "acquire_profile_build_bundle", acquire)
    result = execution.execute_profile_refresh_plan(
        plan=plan,
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    assert result.succeeded
    assert [item.environment for item in result.environments] == list(plan.environments)
    assert result.to_json()["counts"] == {
        "failed": 0,
        "metadata_only": 1,
        "planned": 3,
        "staged": 3,
    }
    stages = {"historical": "metadata", "active": "early", "mature": "mature"}
    for item in result.environments:
        bundle = execution.load_staged_profile_build_bundle(
            tmp_path / "output" / "bundles" / item.bundle_id,
            environment=item.environment,
        )
        stage = stages[item.environment.lifecycle or ""]
        generated = generate_set_profile(
            **bundle.generator_inputs(), stage=stage, generated_at=NOW
        )
        repeated = generate_set_profile(
            **bundle.generator_inputs(), stage=stage, generated_at=NOW
        )
        assert generated.profile.maturity.value == (
            "metadata-only" if stage == "metadata" else stage
        )
        assert generated.to_bytes() == repeated.to_bytes()


@pytest.mark.parametrize(
    ("include_ratings", "include_public_drafts", "expected_stage"),
    (
        (False, False, "metadata"),
        (True, False, "early"),
        (True, True, "mature"),
    ),
)
def test_staged_bundle_selector_uses_loaded_role_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_ratings: bool,
    include_public_drafts: bool,
    expected_stage: str,
) -> None:
    environment = _environment()

    def acquire(**kwargs: object) -> ProfileInputAcquisitionResult:
        acquired_environment = kwargs["environment"]
        assert isinstance(acquired_environment, PlannedEnvironment)
        if not include_ratings and not include_public_drafts:
            return _acquisition(acquired_environment, include_evidence=False)
        return _acquisition_with_optional_inputs(
            acquired_environment,
            include_ratings=include_ratings,
            include_public_drafts=include_public_drafts,
        )

    monkeypatch.setattr(execution, "acquire_profile_build_bundle", acquire)
    result = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    assert result.succeeded

    bundle = execution.load_staged_profile_build_bundle(
        tmp_path / "output" / "bundles" / result.environments[0].bundle_id,
        environment=environment,
    )
    selection = select_profile_generation_stage(
        ratings_report=bundle.ratings_source if bundle.ratings is not None else None,
        public_draft_report=(
            bundle.public_draft_source if bundle.public_drafts is not None else None
        ),
    )
    assert selection.stage.value == expected_stage


def _cache(tmp_path: Path) -> ProfileInputCache:
    return ProfileInputCache(
        tmp_path / "cache",
        policy=ProfileInputCachePolicy(
            freshness_ttl=timedelta(hours=1),
            max_entry_bytes=10_000_000,
            max_total_bytes=30_000_000,
            max_records=20,
            max_versions_per_source=3,
        ),
        clock=lambda: NOW,
    )


def _database(set_code: str = "TST") -> CardDatabase:
    return CardDatabase(
        cards={
            1: CardInfo(
                grp_id=1,
                name="Fixture Card",
                colors=("U",),
                mana_value=2,
                rarity="common",
                types=("Creature",),
                set_code=set_code,
            )
        },
        generated_at=NOW,
    )

def _report(
    environment: PlannedEnvironment,
    name: str,
    *,
    card: bool = False,
    ratings: bool = False,
    drafts: bool = False,
    path: Path | None = None,
    event_format: str | None = None,
) -> ProfileInputSourceReport:
    source = ProfileInputSource(
        name=name,
        set_code=environment.set_code,
        event_format=None if card else (event_format or environment.event_format),
    )
    digest = None if path is None else hashlib.sha256(path.read_bytes()).hexdigest()
    return ProfileInputSourceReport(
        source=source,
        outcome=ProfileInputAcquisitionOutcome.ACQUIRED,
        cache_lookup_outcome=ProfileInputCacheOutcome.FRESH,
        cache_store_outcome=ProfileInputCacheOutcome.FRESH,
        source_version="fixture-v1",
        acquired_at=NOW,
        sha256=digest,
        content_bytes=None if path is None else path.stat().st_size,
        card_count=1 if card else 0,
        rating_rows=1 if ratings else None,
        rating_samples=10 if ratings else None,
        draft_rows=1 if drafts else None,
    )


def _acquisition(
    environment: PlannedEnvironment,
    *,
    include_evidence: bool = False,
    draft_name: str = "early-data.csv",
    fallback_candidates: tuple[ProfileAggregateCandidate, ...] = (),
) -> ProfileInputAcquisitionResult:
    card_report = _report(environment, "card-metadata", card=True)
    ratings = load_17lands_format_data(
        set_code="TST", event_format="quickdraft", cache_path=FIXTURE_DIR / "ratings.json"
    )
    ratings = replace(ratings, set_code=environment.set_code, fetched_at=NOW)
    draft_path = FIXTURE_DIR / draft_name
    draft_report = _report(
        environment,
        "17lands-public-drafts",
        drafts=True,
        path=draft_path,
    )
    ratings_report = _report(environment, "17lands-ratings", ratings=True)
    ratings_report = replace(
        ratings_report,
        rating_rows=len(ratings.card_ratings),
        rating_samples=sum(
            row.sample_counts.games_in_hand for row in ratings.card_ratings.values()
        ),
    )
    manifest = PublicDumpManifest(
        sources=(
            PublicDumpSource(
                name="17lands-public-drafts",
                path=draft_path,
                sha256=draft_report.sha256,
                retrieved_at=NOW.isoformat(),
                attribution="fixture",
                license="CC0",
            ),
        )
    )
    bundle = ProfileBuildBundle(
        environment=environment,
        card_database=_database(environment.set_code),
        card_metadata=card_report,
        ratings=ratings if include_evidence else None,
        ratings_source=ratings_report if include_evidence else None,
        fallback_candidates=fallback_candidates,
        public_drafts=manifest if include_evidence else None,
        public_draft_source=draft_report if include_evidence else None,
    )
    return ProfileInputAcquisitionResult(
        environment=environment,
        source=card_report,
        bundle=bundle,
        ratings_source=ratings_report if include_evidence else None,
        fallback_candidates=fallback_candidates,
        public_draft_source=draft_report if include_evidence else None,
    )


def _candidate(
    environment: PlannedEnvironment,
    event_format: str,
    *,
    available: bool = True,
) -> ProfileAggregateCandidate:
    actual_environment = replace(environment, event_format=event_format)
    if not available:
        report = ProfileInputSourceReport(
            source=ProfileInputSource(
                name="17lands-ratings",
                set_code=environment.set_code,
                event_format=event_format,
            ),
            outcome=ProfileInputAcquisitionOutcome.UNAVAILABLE,
            cache_lookup_outcome=None,
            rating_rows=0,
            rating_samples=0,
            diagnostics=("17lands-ratings-unavailable",),
        )
        return ProfileAggregateCandidate(ratings=None, source=report)

    ratings = load_17lands_format_data(
        set_code="TST",
        event_format="quickdraft",
        cache_path=FIXTURE_DIR / "ratings.json",
    )
    ratings = replace(
        ratings,
        set_code=environment.set_code,
        event_format=event_format,
        fetched_at=NOW,
    )
    report = _report(
        actual_environment,
        "17lands-ratings",
        ratings=True,
        event_format=event_format,
    )
    report = replace(
        report,
        rating_rows=len(ratings.card_ratings),
        rating_samples=sum(
            row.sample_counts.games_in_hand for row in ratings.card_ratings.values()
        ),
    )
    return ProfileAggregateCandidate(ratings=ratings, source=report)


def _acquisition_with_optional_inputs(
    environment: PlannedEnvironment,
    *,
    include_ratings: bool,
    include_public_drafts: bool,
) -> ProfileInputAcquisitionResult:
    acquired = _acquisition(environment, include_evidence=True)
    assert acquired.bundle is not None
    bundle = replace(
        acquired.bundle,
        ratings=acquired.bundle.ratings if include_ratings else None,
        ratings_source=acquired.bundle.ratings_source if include_ratings else None,
        public_drafts=(
            acquired.bundle.public_drafts if include_public_drafts else None
        ),
        public_draft_source=(
            acquired.bundle.public_draft_source if include_public_drafts else None
        ),
    )
    return replace(
        acquired,
        bundle=bundle,
        ratings_source=acquired.ratings_source if include_ratings else None,
        public_draft_source=(
            acquired.public_draft_source if include_public_drafts else None
        ),
    )


def test_default_policy_is_bounded() -> None:
    policy = execution.DEFAULT_PROFILE_REFRESH_CACHE_POLICY
    assert policy.freshness_ttl > timedelta(0)
    assert policy.max_entry_bytes <= policy.max_total_bytes
def test_bundle_layout_inputs_and_move_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=True),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(), cache=_cache(tmp_path), output_dir=tmp_path / "output", offline=True, clock=lambda: NOW
    )
    item = result.environments[0]
    assert item.staged
    bundle_dir = tmp_path / "output" / "bundles" / item.bundle_id
    assert {path.name for path in bundle_dir.iterdir()} == {"bundle.json", "objects"}
    authority = json.loads((bundle_dir / "bundle.json").read_bytes())
    assert set(authority) == {
        "bundle_id",
        "environment",
        "executor_version",
        "fallback_candidates",
        "inputs",
        "mode",
        "outcome",
        "plan_sha256",
        "schema_version",
        "skip_reasons",
        "sources",
    }
    assert authority["fallback_candidates"] == []
    assert {
        role: set(authority["inputs"][role])
        for role in ("card_database", "ratings", "public_drafts")
    } == {
        "card_database": {"source_name", "sha256", "content_bytes"},
        "ratings": {"source_name", "sha256", "content_bytes"},
        "public_drafts": {
            "source_name",
            "sha256",
            "content_bytes",
            "attribution",
            "license",
        },
    }
    assert all(
        authority["inputs"][role]["source_name"]
        == authority["sources"][role]["source"]["name"]
        for role in ("card_database", "ratings", "public_drafts")
    )
    assert set(authority["sources"]) == {"card_database", "ratings", "public_drafts"}
    assert all(
        set(report) == {
            "acquired_at",
            "acquisition_outcome",
            "cache_lookup_outcome",
            "cache_store_outcome",
            "content_bytes",
            "diagnostics",
            "sample_availability",
            "sha256",
            "source",
            "source_version",
        }
        for report in authority["sources"].values()
    )
    assert authority["bundle_id"] == item.bundle_id
    loaded_location = tmp_path / "moved" / item.bundle_id
    loaded_location.parent.mkdir()
    shutil.move(str(bundle_dir), loaded_location)
    loaded = execution.load_staged_profile_build_bundle(loaded_location)
    assert loaded.environment == item.environment
    assert loaded.ratings is not None
    assert loaded.public_drafts is not None
    assert (
        Path(loaded.public_drafts.sources[0].path)
        == loaded_location / "objects" / f"{authority['inputs']['public_drafts']['sha256']}.bin"
    )


def test_same_output_rerun_replaces_changed_card_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment()
    first_acquisition = _acquisition(environment)
    assert first_acquisition.bundle is not None
    second_bundle = replace(
        first_acquisition.bundle,
        card_database=replace(
            first_acquisition.bundle.card_database,
            cards={
                1: replace(
                    first_acquisition.bundle.card_database.cards[1],
                    name="Updated Fixture Card",
                )
            },
        ),
    )
    second_acquisition = replace(first_acquisition, bundle=second_bundle)
    acquisitions = iter((first_acquisition, second_acquisition))
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: next(acquisitions),
    )

    output_dir = tmp_path / "output"
    first = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=output_dir,
        offline=True,
        clock=lambda: NOW,
    )
    bundle_dir = output_dir / "bundles" / first.environments[0].bundle_id
    first_authority = json.loads((bundle_dir / "bundle.json").read_bytes())
    first_card = first_authority["inputs"]["card_database"]
    first_object = bundle_dir / "objects" / f"{first_card['sha256']}.bin"
    first_object_bytes = first_object.read_bytes()

    second = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=output_dir,
        offline=True,
        clock=lambda: NOW,
    )
    assert second.succeeded
    second_authority = json.loads((bundle_dir / "bundle.json").read_bytes())
    second_card = second_authority["inputs"]["card_database"]
    second_object = bundle_dir / "objects" / f"{second_card['sha256']}.bin"
    assert second_card["sha256"] != first_card["sha256"]
    assert second_card["content_bytes"] != first_card["content_bytes"]
    assert second_object.read_bytes() != first_object_bytes
    assert not first_object.exists()
    loaded = execution.load_staged_profile_build_bundle(bundle_dir)
    assert loaded.card_database.cards[1].name == "Updated Fixture Card"


def test_required_failure_isolated_and_ordered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def acquire(**kwargs: object) -> ProfileInputAcquisitionResult:
        environment = kwargs["environment"]
        assert isinstance(environment, PlannedEnvironment)
        if environment.set_code == "AAA":
            return replace(_acquisition(environment), bundle=None)
        return _acquisition(environment)

    monkeypatch.setattr(execution, "acquire_profile_build_bundle", acquire)
    result = execution.execute_profile_refresh_plan(
        plan=_plan("ZZZ", "AAA", "TST"),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    assert [item.environment.set_code for item in result.environments] == ["AAA", "TST", "ZZZ"]
    assert [item.outcome.value for item in result.environments] == ["failed", "staged", "staged"]
    report = json.loads((tmp_path / "output" / "execution.json").read_bytes())
    assert report["counts"] == {
        "failed": 1,
        "metadata_only": 2,
        "planned": 3,
        "staged": 2,
    }
    assert len(report["environments"]) == 3
    assert "required-source-failed" in result.environments[0].diagnostics


def test_failed_authority_write_is_isolated_and_privacy_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def acquire(**kwargs: object) -> ProfileInputAcquisitionResult:
        environment = kwargs["environment"]
        assert isinstance(environment, PlannedEnvironment)
        if environment.set_code == "AAA":
            return replace(_acquisition(environment), bundle=None)
        return _acquisition(environment)

    monkeypatch.setattr(execution, "acquire_profile_build_bundle", acquire)
    original_write = execution._atomic_write_bytes
    failed_once = False

    def fail_first_authority(path: Path, payload: bytes) -> None:
        nonlocal failed_once
        if path.name == "bundle.json" and not failed_once:
            failed_once = True
            raise OSError(f"simulated authority write failure at {tmp_path}")
        original_write(path, payload)

    monkeypatch.setattr(execution, "_atomic_write_bytes", fail_first_authority)
    result = execution.execute_profile_refresh_plan(
        plan=_plan("AAA", "BBB"),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )

    assert failed_once
    assert [item.environment.set_code for item in result.environments] == ["AAA", "BBB"]
    assert [item.outcome.value for item in result.environments] == ["failed", "staged"]
    assert "failure-authority-write-failed" in result.environments[0].diagnostics
    report_path = tmp_path / "output" / "execution.json"
    report = json.loads(report_path.read_bytes())
    assert report["counts"] == {
        "failed": 1,
        "metadata_only": 1,
        "planned": 2,
        "staged": 1,
    }
    assert len(report["environments"]) == 2
    assert report["environments"][0]["outcome"] == "failed"
    assert report["environments"][1]["outcome"] == "staged"
    second_bundle_path = (
        tmp_path
        / "output"
        / "bundles"
        / result.environments[1].bundle_id
        / "bundle.json"
    )
    serialized = report_path.read_bytes() + second_bundle_path.read_bytes() + result.to_bytes()
    assert b"simulated authority write failure" not in serialized
    assert str(tmp_path).encode() not in serialized


@pytest.mark.parametrize(
    ("include_ratings", "include_public_drafts", "missing_role"),
    [
        (True, False, "public_drafts"),
        (False, True, "ratings"),
    ],
)
def test_optional_inputs_fail_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_ratings: bool,
    include_public_drafts: bool,
    missing_role: str,
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition_with_optional_inputs(
            kwargs["environment"],
            include_ratings=include_ratings,
            include_public_drafts=include_public_drafts,
        ),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    item = result.environments[0]
    assert item.staged
    expected_skip = (
        "17lands-ratings-unavailable"
        if missing_role == "ratings"
        else "17lands-public-drafts-unavailable"
    )
    assert expected_skip in item.skip_reasons
    authority = json.loads(
        (tmp_path / "output" / "bundles" / item.bundle_id / "bundle.json").read_bytes()
    )
    assert authority["inputs"][missing_role] is None
    loaded = execution.load_staged_profile_build_bundle(
        tmp_path / "output" / "bundles" / item.bundle_id
    )
    assert (loaded.ratings is not None) is include_ratings
    assert (loaded.public_drafts is not None) is include_public_drafts

def test_partial_optional_staging_failure_preserves_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(
            kwargs["environment"], include_evidence=True
        ),
    )
    original_write_stream = execution._write_stream_object
    failed_once = False

    def fail_one_public_object(objects: Path, source: object) -> tuple[str, int]:
        nonlocal failed_once
        if isinstance(source, Path) and source.name == "early-data.csv" and not failed_once:
            failed_once = True
            raise OSError("simulated public-draft object failure")
        return original_write_stream(objects, source)

    monkeypatch.setattr(execution, "_write_stream_object", fail_one_public_object)
    result = execution.execute_profile_refresh_plan(
        plan=_plan("AAA", "BBB"),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    assert result.succeeded
    first, second = result.environments
    first_authority = json.loads(
        (tmp_path / "output" / "bundles" / first.bundle_id / "bundle.json").read_bytes()
    )
    second_authority = json.loads(
        (tmp_path / "output" / "bundles" / second.bundle_id / "bundle.json").read_bytes()
    )
    assert first_authority["inputs"]["ratings"] is not None
    assert first_authority["inputs"]["public_drafts"] is None
    assert second_authority["inputs"]["ratings"] is not None
    assert second_authority["inputs"]["public_drafts"] is not None
    assert "17lands-public-drafts-staging-failed" in first.skip_reasons
    assert execution.load_staged_profile_build_bundle(
        tmp_path / "output" / "bundles" / first.bundle_id
    ).ratings is not None
    assert execution.load_staged_profile_build_bundle(
        tmp_path / "output" / "bundles" / second.bundle_id
    ).public_drafts is not None
    generated = generate_staged_environment_profile(
        bundle_path=tmp_path / "output" / "bundles" / first.bundle_id,
        environment=first.environment,
        generated_at=NOW,
    )
    assert generated.publication_eligible
    assert generated.selection is not None
    assert generated.selection.stage.value == "early"
    unavailable_drafts = next(
        source
        for source in generated.input_sources
        if source["role"] == "seventeen_lands_public_drafts"
    )
    assert unavailable_drafts["outcome"] == "unavailable"
    assert unavailable_drafts["sha256"] == ""
    assert unavailable_drafts["source_version"] == ""
    assert unavailable_drafts["acquired_at"] == ""


def test_objects_precede_bundle_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=True),
    )
    original_write = execution._atomic_write_bytes
    observed = False

    def write_authority(path: Path, payload: bytes) -> None:
        nonlocal observed
        if path.name == "bundle.json":
            assert (path.parent / "objects").is_dir()
            assert any((path.parent / "objects").iterdir())
            observed = True
        original_write(path, payload)

    monkeypatch.setattr(execution, "_atomic_write_bytes", write_authority)
    execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    assert observed


def test_failed_authority_replaces_prior_staged_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=True),
    )
    first = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    bundle_dir = tmp_path / "output" / "bundles" / first.environments[0].bundle_id
    authority_path = bundle_dir / "bundle.json"
    previous = authority_path.read_bytes()

    def fail_required(**kwargs: object) -> ProfileInputAcquisitionResult:
        environment = kwargs["environment"]
        assert isinstance(environment, PlannedEnvironment)
        return replace(_acquisition(environment), bundle=None)

    monkeypatch.setattr(execution, "acquire_profile_build_bundle", fail_required)
    second = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    assert not second.succeeded
    authority = json.loads(authority_path.read_bytes())
    assert authority["outcome"] == "failed"
    assert authority["inputs"] == {
        "card_database": None,
        "ratings": None,
        "public_drafts": None,
    }
    assert authority_path.read_bytes() != previous


def test_failed_authority_preserves_prior_marker_when_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=True),
    )
    first = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    bundle_dir = tmp_path / "output" / "bundles" / first.environments[0].bundle_id
    authority_path = bundle_dir / "bundle.json"
    previous = authority_path.read_bytes()
    original_write = execution._atomic_write_bytes

    def fail_bundle_authority(path: Path, payload: bytes) -> None:
        if path == authority_path:
            raise OSError("simulated authority replacement failure")
        original_write(path, payload)

    monkeypatch.setattr(execution, "_atomic_write_bytes", fail_bundle_authority)

    def fail_required(**kwargs: object) -> ProfileInputAcquisitionResult:
        environment = kwargs["environment"]
        assert isinstance(environment, PlannedEnvironment)
        return replace(_acquisition(environment), bundle=None)

    monkeypatch.setattr(execution, "acquire_profile_build_bundle", fail_required)
    result = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    assert not result.succeeded
    assert authority_path.read_bytes() == previous

@pytest.mark.parametrize(
    "tamper", ["authority", "object", "missing", "symlink", "noncanonical", "source-name"]
)
def test_loader_rejects_tampering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=True),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(), cache=_cache(tmp_path), output_dir=tmp_path / "output", offline=True, clock=lambda: NOW
    )
    bundle_dir = tmp_path / "output" / "bundles" / result.environments[0].bundle_id
    authority_path = bundle_dir / "bundle.json"
    if tamper == "authority":
        data = json.loads(authority_path.read_bytes())
        data["inputs"]["card_database"]["content_bytes"] += 1
        authority_path.write_bytes((json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode())
    elif tamper == "noncanonical":
        authority_path.write_bytes(b'{"schema_version":1}\n')
    elif tamper == "missing":
        data = json.loads(authority_path.read_bytes())
        digest = data["inputs"]["card_database"]["sha256"]
        (bundle_dir / "objects" / f"{digest}.bin").unlink()
    elif tamper == "object":
        data = json.loads(authority_path.read_bytes())
        digest = data["inputs"]["card_database"]["sha256"]
        (bundle_dir / "objects" / f"{digest}.bin").write_bytes(b"tampered")
    elif tamper == "source-name":
        data = json.loads(authority_path.read_bytes())
        data["inputs"]["ratings"]["source_name"] = "mismatched-source"
        authority_path.write_bytes(
            (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
    else:
        data = json.loads(authority_path.read_bytes())
        digest = data["inputs"]["card_database"]["sha256"]
        target = tmp_path / "outside.bin"
        target.write_bytes((bundle_dir / "objects" / f"{digest}.bin").read_bytes())
        (bundle_dir / "objects" / f"{digest}.bin").unlink()
        (bundle_dir / "objects" / f"{digest}.bin").symlink_to(target)
    with pytest.raises(execution.ProfileRefreshExecutionError):
        execution.load_staged_profile_build_bundle(bundle_dir)

def test_loader_ignores_unreferenced_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=True),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(), cache=_cache(tmp_path), output_dir=tmp_path / "output", offline=True, clock=lambda: NOW
    )
    bundle_dir = tmp_path / "output" / "bundles" / result.environments[0].bundle_id
    before = execution.load_staged_profile_build_bundle(bundle_dir)

    objects = bundle_dir / "objects"
    (objects / "unreferenced.bin").write_bytes(b"not a referenced input")
    target = tmp_path / "unreferenced-target.bin"
    target.write_bytes(b"not a referenced input either")
    (objects / "unreferenced-link.bin").symlink_to(target)

    after = execution.load_staged_profile_build_bundle(bundle_dir)
    assert after == before


def test_loader_accepts_semantic_model_numeric_canonicality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=False),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    bundle_dir = tmp_path / "output" / "bundles" / result.environments[0].bundle_id
    authority_path = bundle_dir / "bundle.json"
    authority = json.loads(authority_path.read_bytes())
    card_input = authority["inputs"]["card_database"]
    old_digest = card_input["sha256"]
    card_path = bundle_dir / "objects" / f"{old_digest}.bin"
    card_value = json.loads(card_path.read_bytes())
    card_value["cards"]["1"]["mana_value"] = 2.0
    card_payload = (
        json.dumps(card_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    new_digest = hashlib.sha256(card_payload).hexdigest()
    card_path.unlink()
    (bundle_dir / "objects" / f"{new_digest}.bin").write_bytes(card_payload)
    card_input.update({"content_bytes": len(card_payload), "sha256": new_digest})
    card_source = authority["sources"]["card_database"]
    card_source.update({"content_bytes": len(card_payload), "sha256": new_digest})
    authority_path.write_bytes(
        (
            json.dumps(authority, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
    )
    loaded = execution.load_staged_profile_build_bundle(bundle_dir)
    assert loaded.card_database.cards[1].mana_value == 2.0


def test_authorities_and_results_are_privacy_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=True),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        clock=lambda: NOW,
    )
    execution_path = tmp_path / "output" / "execution.json"
    bundle_path = next((tmp_path / "output" / "bundles").iterdir()) / "bundle.json"
    execution_json = execution_path.read_text()
    bundle_json = bundle_path.read_text()
    assert set(json.loads(execution_json)) == {
        "schema_version",
        "executor_version",
        "plan_sha256",
        "mode",
        "counts",
        "environments",
    }
    serialized = execution_json + bundle_json + result.to_bytes().decode()
    for sentinel in (
        "Fixture Card",
        "early-data.csv",
        "alpha",
        "https://",
        "Authorization",
        "Bearer",
        "secret-token",
        "profile_version",
        "generated_at",
    ):
        assert sentinel not in serialized
    assert result.to_bytes() == execution_path.read_bytes()


def test_real_acquisition_online_then_offline_reuses_cache(tmp_path: Path) -> None:
    card_calls: list[str] = []
    rating_calls: list[str] = []
    draft_calls: list[str] = []

    def card_fetch(*, set_code: str, timeout_seconds: int) -> CardDatabase:
        del timeout_seconds
        card_calls.append(set_code)
        source = CardDatabase.from_json(json.loads((FIXTURE_DIR / "card-database.json").read_text()))
        return replace(source, cards={key: replace(card, set_code=set_code) for key, card in source.cards.items()})

    def ratings_fetch(*, set_code: str, event_format: str, fetched_at: datetime, timeout_seconds: int):
        del timeout_seconds
        rating_calls.append(set_code)
        source = load_17lands_format_data(set_code="TST", event_format="quickdraft", cache_path=FIXTURE_DIR / "ratings.json")
        return replace(source, set_code=set_code, event_format=event_format, fetched_at=fetched_at)

    def draft_fetch(*, set_code: str, event_format: str, path: Path, timeout_seconds: int) -> None:
        del timeout_seconds
        draft_calls.append(set_code)
        payload = (FIXTURE_DIR / "early-data.csv").read_bytes().replace(b"TST", set_code.encode())
        payload = payload.replace(b"QuickDraft", event_format.encode())
        path.write_bytes(payload)

    adapters = {
        "card_metadata_adapter": CardMetadataAdapter(fetch_database=card_fetch),
        "ratings_adapter": SeventeenLandsRatingsAdapter(fetch_ratings=ratings_fetch),
        "public_draft_adapter": SeventeenLandsPublicDraftAdapter(fetch_public_drafts=draft_fetch),
    }
    first = execution.execute_profile_refresh_plan(
        plan=_plan(), cache=_cache(tmp_path), output_dir=tmp_path / "online", offline=False, clock=lambda: NOW, **adapters
    )
    assert first.succeeded
    first_bundle = first.environments[0].bundle_id
    first_authority = json.loads(
        (tmp_path / "online" / "bundles" / first_bundle / "bundle.json").read_bytes()
    )
    card_calls_before, rating_calls_before, draft_calls_before = len(card_calls), len(rating_calls), len(draft_calls)
    first_loaded = execution.load_staged_profile_build_bundle(
        tmp_path / "online" / "bundles" / first_bundle
    )
    first_profile = generate_set_profile(
        **first_loaded.generator_inputs(), stage="early", generated_at=NOW
    )

    def fail_fetch(**_: object):
        raise AssertionError("offline acquisition attempted a fetch")
    offline_adapters = {
        "card_metadata_adapter": CardMetadataAdapter(fetch_database=fail_fetch),
        "ratings_adapter": SeventeenLandsRatingsAdapter(fetch_ratings=fail_fetch),
        "public_draft_adapter": SeventeenLandsPublicDraftAdapter(fetch_public_drafts=fail_fetch),
    }
    second = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "online",
        offline=True,
        clock=lambda: NOW,
        **offline_adapters,
    )
    second_authority = json.loads(
        (tmp_path / "online" / "bundles" / first_bundle / "bundle.json").read_bytes()
    )
    assert first_authority["mode"] == "online"
    assert second_authority["mode"] == "offline"
    assert all(
        report["cache_lookup_outcome"] == "offline-reused"
        and report["cache_store_outcome"] is None
        for report in second_authority["sources"].values()
    )
    assert second_authority["inputs"] == first_authority["inputs"]
    assert len(card_calls) == card_calls_before
    assert len(rating_calls) == rating_calls_before
    assert len(draft_calls) == draft_calls_before
    second_loaded = execution.load_staged_profile_build_bundle(
        tmp_path / "online" / "bundles" / first_bundle
    )
    second_profile = generate_set_profile(
        **second_loaded.generator_inputs(), stage="early", generated_at=NOW
    )
    assert first_profile.to_bytes() == second_profile.to_bytes()


def test_loaded_bundle_keeps_explicit_generator_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=True),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(), cache=_cache(tmp_path), output_dir=tmp_path / "output", offline=True, clock=lambda: NOW
    )
    bundle = execution.load_staged_profile_build_bundle(
        tmp_path / "output" / "bundles" / result.environments[0].bundle_id
    )
    metadata = generate_set_profile(**bundle.generator_inputs(), stage="metadata", generated_at=NOW)
    assert metadata.profile.maturity.value == "metadata-only"
    assert bundle.ratings is not None

def test_metadata_only_bundle_has_null_optional_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(kwargs["environment"], include_evidence=False),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(), cache=_cache(tmp_path), output_dir=tmp_path / "output", offline=True, clock=lambda: NOW
    )
    bundle_dir = tmp_path / "output" / "bundles" / result.environments[0].bundle_id
    authority = json.loads((bundle_dir / "bundle.json").read_bytes())
    assert authority["inputs"]["card_database"] is not None
    assert authority["inputs"]["ratings"] is None
    assert authority["inputs"]["public_drafts"] is None
    loaded = execution.load_staged_profile_build_bundle(bundle_dir)
    assert loaded.ratings is None
    assert loaded.public_drafts is None
    assert {
        "17lands-ratings-unavailable",
        "17lands-public-drafts-unavailable",
    } <= set(result.environments[0].skip_reasons)


def test_replace_bundle_directory_preserves_backup_when_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "bundle"
    destination.mkdir()
    (destination / "bundle.json").write_text("prior", encoding="utf-8")
    temporary = tmp_path / ".bundle.new"
    temporary.mkdir()
    (temporary / "bundle.json").write_text("next", encoding="utf-8")

    real_replace = execution.os.replace
    calls: list[tuple[object, object]] = []

    def failing_replace(source: object, target: object) -> None:
        calls.append((source, target))
        if len(calls) == 1:
            real_replace(source, target)
            return
        raise OSError("simulated replace failure")

    monkeypatch.setattr(execution.os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        execution._replace_bundle_directory(temporary, destination)

    backups = [path for path in tmp_path.iterdir() if path.name.startswith(".bundle.old-")]
    assert len(backups) == 1
    assert (backups[0] / "bundle.json").read_text(encoding="utf-8") == "prior"


def test_environment_reasons_round_trip_verbatim() -> None:
    environment = PlannedEnvironment(
        set_code="NEW",
        event_format="PremierDraft",
        lifecycle="active",
        reasons=("requested by weekly operator review",),
    )

    value = execution._environment_json(environment)

    assert value["reasons"] == ["requested by weekly operator review"]
    assert execution._parse_environment(value) == environment


def test_aggregate_only_staged_authority_uses_null_draft_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition_with_optional_inputs(
            kwargs["environment"], include_ratings=True, include_public_drafts=False
        ),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        include_public_drafts=False,
        clock=lambda: NOW,
    )
    item = result.environments[0]
    assert item.sources[2] is None
    assert not any("public-drafts" in reason for reason in item.skip_reasons)
    bundle_dir = tmp_path / "output" / "bundles" / item.bundle_id
    authority = json.loads((bundle_dir / "bundle.json").read_bytes())
    assert authority["sources"]["public_drafts"] is None
    assert authority["inputs"]["public_drafts"] is None
    loaded = execution.load_staged_profile_build_bundle(bundle_dir)
    assert loaded.public_drafts is None
    assert loaded.public_draft_source is None


@pytest.mark.parametrize("tamper", ("draft-input-with-null-report", "null-ratings-report"))
def test_loader_rejects_invalid_null_source_authority_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition_with_optional_inputs(
            kwargs["environment"], include_ratings=True, include_public_drafts=False
        ),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        include_public_drafts=False,
        clock=lambda: NOW,
    )
    bundle_dir = (
        tmp_path / "output" / "bundles" / result.environments[0].bundle_id
    )
    authority_path = bundle_dir / "bundle.json"
    authority = json.loads(authority_path.read_bytes())
    if tamper == "draft-input-with-null-report":
        authority["inputs"]["public_drafts"] = {
            "attribution": "fixture",
            "content_bytes": 1,
            "license": "CC0",
            "sha256": "b" * 64,
            "source_name": "17lands-public-drafts",
        }
    else:
        authority["sources"]["ratings"] = None
    authority_path.write_bytes(
        (
            json.dumps(
                authority,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )

    with pytest.raises(execution.ProfileRefreshExecutionError):
        execution.load_staged_profile_build_bundle(bundle_dir)


def test_refresh_include_public_drafts_must_be_bool_before_execution(
    tmp_path: Path,
) -> None:
    with pytest.raises(execution.ProfileRefreshExecutionError, match="include_public_drafts"):
        execution.execute_profile_refresh_plan(
            plan=_plan(),
            cache=_cache(tmp_path),
            output_dir=tmp_path / "output",
            offline=True,
            include_public_drafts=1,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
    assert not (tmp_path / "output").exists()


def _stage_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidates: tuple[ProfileAggregateCandidate, ...],
) -> tuple[execution.ProfileRefreshEnvironmentResult, Path]:
    monkeypatch.setattr(
        execution,
        "acquire_profile_build_bundle",
        lambda **kwargs: _acquisition(
            kwargs["environment"],
            fallback_candidates=candidates,
        ),
    )
    result = execution.execute_profile_refresh_plan(
        plan=_plan(),
        cache=_cache(tmp_path),
        output_dir=tmp_path / "output",
        offline=True,
        include_public_drafts=False,
        clock=lambda: NOW,
    )
    item = result.environments[0]
    return item, tmp_path / "output" / "bundles" / item.bundle_id


def _rewrite_authority(path: Path, authority: dict[str, object]) -> None:
    path.write_bytes(
        (
            json.dumps(authority, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
    )


def _rewrite_candidate_payload(
    bundle_dir: Path,
    authority: dict[str, object],
    mutate: object,
) -> None:
    rows = authority["fallback_candidates"]
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    input_value = row["input"]
    assert isinstance(input_value, dict)
    old_digest = input_value["sha256"]
    assert isinstance(old_digest, str)
    old_path = bundle_dir / "objects" / f"{old_digest}.bin"
    payload = json.loads(old_path.read_bytes())
    assert isinstance(payload, dict)
    assert callable(mutate)
    mutate(payload)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    (bundle_dir / "objects" / f"{digest}.bin").write_bytes(encoded)
    if digest != old_digest:
        old_path.unlink()
    input_value["sha256"] = digest
    input_value["content_bytes"] = len(encoded)
    source = row["source"]
    assert isinstance(source, dict)
    source["sha256"] = digest
    source["content_bytes"] = len(encoded)
    _rewrite_authority(bundle_dir / "bundle.json", authority)


def test_fallback_candidates_round_trip_with_order_and_portable_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment()
    candidates = (_candidate(environment, "PremierDraft"), _candidate(environment, "TradDraft"))
    item, bundle_dir = _stage_fallback(tmp_path, monkeypatch, candidates)
    authority_path = bundle_dir / "bundle.json"
    authority = json.loads(authority_path.read_bytes())
    rows = authority["fallback_candidates"]
    assert [row["source"]["source"]["event_format"] for row in rows] == [
        "premierdraft",
        "traddraft",
    ]
    assert all(row["input"] is not None for row in rows)
    assert rows[0]["input"]["sha256"] != rows[1]["input"]["sha256"]
    assert str(tmp_path).encode() not in authority_path.read_bytes()
    assert [report.source.event_format for report in item.fallback_sources] == [
        "premierdraft",
        "traddraft",
    ]
    moved_root = tmp_path / "moved"
    shutil.move(str(tmp_path / "output"), moved_root)
    loaded = execution.load_staged_profile_build_bundle(
        moved_root / "bundles" / item.bundle_id
    )
    assert [candidate.source.source.event_format for candidate in loaded.fallback_candidates] == [
        "premierdraft",
        "traddraft",
    ]
    assert all(candidate.ratings is not None for candidate in loaded.fallback_candidates)


def test_unavailable_fallback_has_null_input_and_empty_array_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment()
    unavailable = (_candidate(environment, "PremierDraft", available=False),)
    item, bundle_dir = _stage_fallback(tmp_path, monkeypatch, unavailable)
    authority = json.loads((bundle_dir / "bundle.json").read_bytes())
    assert authority["fallback_candidates"][0]["input"] is None
    assert authority["fallback_candidates"][0]["source"]["sha256"] is None
    loaded = execution.load_staged_profile_build_bundle(bundle_dir)
    assert loaded.fallback_candidates[0].ratings is None
    assert item.available_input_roles == ("card_database",)
    assert item.metadata_only
    assert item.to_json()["fallback_sources"][0]["sha256"] is None

    empty_item, empty_dir = _stage_fallback(tmp_path, monkeypatch, ())
    empty_authority_path = empty_dir / "bundle.json"
    empty_authority = json.loads(empty_authority_path.read_bytes())
    assert empty_authority["fallback_candidates"] == []
    assert execution.load_staged_profile_build_bundle(empty_dir).fallback_candidates == ()
    empty_authority.pop("fallback_candidates")
    _rewrite_authority(empty_authority_path, empty_authority)
    with pytest.raises(execution.ProfileRefreshExecutionError):
        execution.load_staged_profile_build_bundle(empty_dir)
    assert empty_item.fallback_sources == ()


def test_fallback_ratings_count_once_as_available_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment()
    item, bundle_dir = _stage_fallback(
        tmp_path, monkeypatch, (_candidate(environment, "PremierDraft"),)
    )
    assert item.available_input_roles == ("card_database", "ratings")
    assert not item.metadata_only
    assert item.to_json()["available_input_roles"].count("ratings") == 1
    loaded = execution.load_staged_profile_build_bundle(bundle_dir)
    assert loaded.ratings is None
    assert loaded.fallback_candidates[0].ratings is not None


@pytest.mark.parametrize(
    "tamper",
    (
        "missing",
        "digest",
        "size",
        "object",
        "wrong-set",
        "unsupported-format",
        "duplicate",
        "reversed",
        "object-format",
        "timestamp",
        "source-version",
        "old-schema",
    ),
)
def test_loader_rejects_strict_fallback_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    environment = _environment()
    candidates = (_candidate(environment, "PremierDraft"), _candidate(environment, "TradDraft"))
    _, bundle_dir = _stage_fallback(tmp_path, monkeypatch, candidates)
    authority_path = bundle_dir / "bundle.json"
    authority = json.loads(authority_path.read_bytes())
    rows = authority["fallback_candidates"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    first_input = first["input"]
    assert isinstance(first_input, dict)
    digest = first_input["sha256"]
    assert isinstance(digest, str)
    object_path = bundle_dir / "objects" / f"{digest}.bin"

    if tamper == "missing":
        object_path.unlink()
    elif tamper == "digest":
        first_input["sha256"] = "0" * 64
        _rewrite_authority(authority_path, authority)
    elif tamper == "size":
        first_input["content_bytes"] += 1
        _rewrite_authority(authority_path, authority)
    elif tamper == "object":
        object_path.write_bytes(b"corrupt")
    elif tamper == "wrong-set":
        _rewrite_candidate_payload(
            bundle_dir, authority, lambda payload: payload.update({"set_code": "BAD"})
        )
    elif tamper == "unsupported-format":
        first["source"]["source"]["event_format"] = "sealed"
        _rewrite_authority(authority_path, authority)
    elif tamper == "duplicate":
        duplicate = json.loads(json.dumps(rows[0]))
        rows[1] = duplicate
        _rewrite_authority(authority_path, authority)
    elif tamper == "reversed":
        authority["fallback_candidates"] = list(reversed(rows))
        _rewrite_authority(authority_path, authority)
    elif tamper == "object-format":
        _rewrite_candidate_payload(
            bundle_dir,
            authority,
            lambda payload: payload.update({"event_format": "TradDraft"}),
        )
    elif tamper == "timestamp":
        _rewrite_candidate_payload(
            bundle_dir,
            authority,
            lambda payload: payload.update({"fetched_at": "2026-08-31T13:00:00+00:00"}),
        )
    elif tamper == "source-version":
        first["source"]["source_version"] = None
        _rewrite_authority(authority_path, authority)
    else:
        authority["schema_version"] = 1
        _rewrite_authority(authority_path, authority)

    with pytest.raises(execution.ProfileRefreshExecutionError):
        execution.load_staged_profile_build_bundle(bundle_dir)


def test_candidate_staging_failure_is_optional_and_drops_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _environment()
    candidate = _candidate(environment, "PremierDraft")
    calls = 0
    original = execution._write_bytes_object

    def fail_candidate(objects: Path, payload: bytes) -> tuple[str, int]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("candidate object unavailable")
        return original(objects, payload)

    monkeypatch.setattr(execution, "_write_bytes_object", fail_candidate)
    item, bundle_dir = _stage_fallback(tmp_path, monkeypatch, (candidate,))
    authority = json.loads((bundle_dir / "bundle.json").read_bytes())
    row = authority["fallback_candidates"][0]
    assert row["input"] is None
    assert row["source"]["sha256"] is None
    assert row["source"]["content_bytes"] is None
    assert "17lands-premierdraft-staging-failed" in item.skip_reasons
    assert item.available_input_roles == ("card_database",)
    loaded = execution.load_staged_profile_build_bundle(bundle_dir)
    assert loaded.fallback_candidates[0].ratings is None
