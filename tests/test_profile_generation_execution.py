from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from draftomen.carddb import CardDatabase
from draftomen.profile_generation import ProfileGenerationResult, ProfileGenerationStage
from draftomen.profile_generation_execution import (
    CLASSIFICATION_ERROR_REASON,
    CLASSIFICATION_GAP_REASON,
    ProfileGenerationEnvironmentOutcome,
    ProfileGenerationExecutionError,
    ProfileGenerationFailurePhase,
    ProfileGenerationFailureReason,
    generate_staged_environment_profile,
)
from draftomen.profile_input_acquisition import (
    ProfileBuildBundle,
    ProfileInputAcquisitionOutcome,
    ProfileInputAcquisitionResult,
    ProfileInputSourceReport,
)
from draftomen.profile_input_cache import (
    ProfileInputCacheOutcome,
    ProfileInputSource,
)
from draftomen.public_dump import PublicDumpManifest, PublicDumpSource
from draftomen.refresh_plan import LifecycleMetadata, PlannedEnvironment, RefreshPlan
from draftomen.set_profile import ProfileMaturity, SetProfile
from draftomen.seventeen import load_17lands_format_data
import draftomen.profile_generation_execution as execution
import draftomen.profile_refresh_execution as refresh


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "profile-generation"


def _environment(set_code: str = "TST") -> PlannedEnvironment:
    return PlannedEnvironment(
        set_code=set_code,
        event_format="quickdraft",
        lifecycle="active",
        reasons=("manual-selection",),
    )


def _report(
    environment: PlannedEnvironment,
    name: str,
    *,
    card: bool = False,
    ratings: bool = False,
    drafts: bool = False,
    path: Path | None = None,
    outcome: ProfileInputAcquisitionOutcome = ProfileInputAcquisitionOutcome.ACQUIRED,
) -> ProfileInputSourceReport:
    source = ProfileInputSource(
        name=name,
        set_code=environment.set_code,
        event_format=None if card else environment.event_format,
    )
    digest = None if path is None else hashlib.sha256(path.read_bytes()).hexdigest()
    return ProfileInputSourceReport(
        source=source,
        outcome=outcome,
        cache_lookup_outcome=ProfileInputCacheOutcome.FRESH,
        cache_store_outcome=ProfileInputCacheOutcome.FRESH,
        source_version="fixture-v1",
        acquired_at=NOW,
        sha256=digest,
        card_count=2 if card else 0,
        rating_rows=1 if ratings else None,
        rating_samples=10 if ratings else None,
        draft_rows=1 if drafts else None,
    )


def _bundle(
    environment: PlannedEnvironment,
    *,
    ratings: bool = False,
    drafts: bool = False,
    ratings_outcome: ProfileInputAcquisitionOutcome = ProfileInputAcquisitionOutcome.ACQUIRED,
    card_database: CardDatabase | None = None,
    tmp_path: Path,
) -> ProfileInputAcquisitionResult:
    cards = card_database or CardDatabase.from_json(
        json.loads((FIXTURE_DIR / "card-database.json").read_text())
    )
    cards = replace(
        cards,
        cards={
            grp_id: replace(card, set_code=environment.set_code)
            for grp_id, card in cards.cards.items()
        },
        generated_at=NOW,
    )
    card_report = replace(
        _report(environment, "card-metadata", card=True),
        card_count=len(cards),
    )

    ratings_value = None
    ratings_report = None
    if ratings:
        ratings_value = load_17lands_format_data(
            set_code="TST", event_format="quickdraft", cache_path=FIXTURE_DIR / "ratings.json"
        )
        ratings_value = replace(ratings_value, set_code=environment.set_code, fetched_at=NOW)
        ratings_report = replace(
            _report(
                environment,
                "17lands-ratings",
                ratings=True,
                outcome=ratings_outcome,
            ),
            rating_rows=len(ratings_value.card_ratings),
            rating_samples=sum(
                row.sample_counts.games_in_hand for row in ratings_value.card_ratings.values()
            ),
        )

    drafts_value = None
    drafts_report = None
    if drafts:
        draft_path = tmp_path / "draft-input.csv"
        draft_path.write_bytes((FIXTURE_DIR / "mature-data.csv").read_bytes())
        drafts_report = _report(
            environment,
            "17lands-public-drafts",
            drafts=True,
            path=draft_path,
        )
        drafts_value = PublicDumpManifest(
            sources=(
                PublicDumpSource(
                    name=drafts_report.source.name,
                    path=draft_path,
                    sha256=drafts_report.sha256,
                    retrieved_at=NOW.isoformat(),
                    attribution="fixture",
                    license="CC0",
                ),
            )
        )

    built = ProfileBuildBundle(
        environment=environment,
        card_database=cards,
        card_metadata=card_report,
        ratings=ratings_value,
        ratings_source=ratings_report,
        public_drafts=drafts_value,
        public_draft_source=drafts_report,
    )
    return ProfileInputAcquisitionResult(
        environment=environment,
        source=card_report,
        bundle=built,
        ratings_source=ratings_report,
        public_draft_source=drafts_report,
    )


def _stage(
    tmp_path: Path,
    environment: PlannedEnvironment,
    *,
    ratings: bool = False,
    drafts: bool = False,
    ratings_outcome: ProfileInputAcquisitionOutcome = ProfileInputAcquisitionOutcome.ACQUIRED,
    card_database: CardDatabase | None = None,
) -> Path:
    acquired = _bundle(
        environment,
        ratings=ratings,
        drafts=drafts,
        ratings_outcome=ratings_outcome,
        card_database=card_database,
        tmp_path=tmp_path,
    )

    def acquire(**kwargs: object) -> ProfileInputAcquisitionResult:
        assert kwargs["environment"] == environment
        assert acquired.bundle is not None
        return acquired

    old = refresh.acquire_profile_build_bundle
    refresh.acquire_profile_build_bundle = acquire
    try:
        plan = RefreshPlan(
            selection_mode="manual",
            selection_set_code=environment.set_code,
            event_format=environment.event_format,
            environments=(environment,),
            inventory_source_url="https://inventory.example.test/sets.json",
            inventory_payload_digest="a" * 64,
            lifecycle=LifecycleMetadata(
                provider="fixture",
                source_url="https://lifecycle.example.test/sets.json",
                version="1",
            ),
        )
        result = refresh.execute_profile_refresh_plan(
            plan=plan,
            cache=refresh.ProfileInputCache(
                tmp_path / "cache",
                policy=refresh.DEFAULT_PROFILE_REFRESH_CACHE_POLICY,
            ),
            output_dir=tmp_path / "staged",
            offline=True,
            clock=lambda: NOW,
        )
    finally:
        refresh.acquire_profile_build_bundle = old
    assert result.succeeded
    item = result.environments[0]
    return tmp_path / "staged" / "bundles" / item.bundle_id


def test_metadata_bundle_is_publication_eligible_with_validated_payload(tmp_path: Path) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment)

    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )

    assert result.outcome is ProfileGenerationEnvironmentOutcome.PUBLICATION_ELIGIBLE
    assert result.selection is not None
    assert result.selection.stage.value == "metadata"
    assert result.generation is not None
    assert result.validated is not None
    assert result.profile_bytes == result.validated.profile_bytes
    assert result.profile_sha256 == hashlib.sha256(result.profile_bytes).hexdigest()
    assert result.profile_size == len(result.profile_bytes)
    assert result.to_bytes().endswith(b"\n")
    assert "validated" in result.to_json()


@pytest.mark.parametrize(
    ("ratings_outcome", "fallback_state"),
    [
        pytest.param(ProfileInputAcquisitionOutcome.ACQUIRED, "none", id="live"),
        pytest.param(ProfileInputAcquisitionOutcome.CACHED, "none", id="fresh-cache"),
        pytest.param(ProfileInputAcquisitionOutcome.STALE, "verified-stale-cache", id="stale-fallback"),
        pytest.param(
            ProfileInputAcquisitionOutcome.OFFLINE_REUSED,
            "verified-offline-cache",
            id="offline-fallback",
        ),
    ],
)
def test_generation_json_preserves_exact_input_provenance(
    tmp_path: Path,
    ratings_outcome: ProfileInputAcquisitionOutcome,
    fallback_state: str,
) -> None:
    environment = _environment()
    bundle_path = _stage(
        tmp_path,
        environment,
        ratings=True,
        drafts=True,
        ratings_outcome=ratings_outcome,
    )
    generated_at = NOW + timedelta(days=1)
    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=generated_at,
    )

    assert result.publication_eligible
    assert result.generation is not None
    assert result.generation.report.generated_at == generated_at.isoformat()
    authority = json.loads((bundle_path / "bundle.json").read_text())
    expected = [
        {
            "role": "card_database",
            "name": "card-metadata",
            "sha256": authority["sources"]["card_database"]["sha256"],
            "source_version": "fixture-v1",
            "requested_set": "TST",
            "requested_format": "quickdraft",
            "source_format": "",
            "acquired_at": NOW.isoformat(),
            "outcome": "acquired",
            "fallback_state": "none",
        },
        {
            "role": "seventeen_lands_ratings",
            "name": "17lands-ratings",
            "sha256": authority["sources"]["ratings"]["sha256"],
            "source_version": "fixture-v1",
            "requested_set": "TST",
            "requested_format": "quickdraft",
            "source_format": "quickdraft",
            "acquired_at": NOW.isoformat(),
            "outcome": ratings_outcome.value,
            "fallback_state": fallback_state,
        },
        {
            "role": "seventeen_lands_public_drafts",
            "name": "17lands-public-drafts",
            "sha256": authority["sources"]["public_drafts"]["sha256"],
            "source_version": "fixture-v1",
            "requested_set": "TST",
            "requested_format": "quickdraft",
            "source_format": "quickdraft",
            "acquired_at": NOW.isoformat(),
            "outcome": "acquired",
            "fallback_state": "none",
        },
    ]
    assert result.to_json()["input_sources"] == expected


def test_unpinned_unavailable_ratings_provenance_is_retained(tmp_path: Path) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment)
    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW + timedelta(days=1),
    )

    assert result.publication_eligible
    ratings = next(
        source for source in result.to_json()["input_sources"] if source["role"] == "seventeen_lands_ratings"
    )
    assert ratings == {
        "role": "seventeen_lands_ratings",
        "name": "17lands-ratings",
        "sha256": "",
        "source_version": "",
        "requested_set": "TST",
        "requested_format": "quickdraft",
        "source_format": "quickdraft",
        "acquired_at": "",
        "outcome": "unavailable",
        "fallback_state": "none",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("name", "https://unsafe.example/source", id="url"),
        pytest.param("source_version", "/tmp/secret", id="path"),
        pytest.param("requested_set", "TST\nsecret", id="control"),
        pytest.param("unknown", "value", id="unknown-key"),
    ],
)
def test_generation_json_rejects_unsafe_or_unknown_input_source_fields(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment)
    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )
    source = dict(result.input_sources[0])
    source[field] = value

    with pytest.raises(ProfileGenerationExecutionError):
        replace(result, input_sources=(source,))


def test_generated_identity_is_validated_against_requested_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment)
    original_generate = execution.generate_set_profile

    def generate_different_identity(**kwargs: object) -> ProfileGenerationResult:
        different = dict(kwargs)
        different["set_code"] = "OTHER"
        different["event_format"] = "sealed"
        return original_generate(**different)

    monkeypatch.setattr(execution, "generate_set_profile", generate_different_identity)
    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )

    assert result.outcome is ProfileGenerationEnvironmentOutcome.FAILED
    assert result.failure_phase is ProfileGenerationFailurePhase.VALIDATION
    assert result.failure_reason is ProfileGenerationFailureReason.VALIDATION_FAILED
    assert result.generation is None and result.validated is None

@pytest.mark.parametrize(
    ("ratings", "drafts", "expected_stage", "expected_maturity"),
    [
        pytest.param(True, False, "early", ProfileMaturity.EARLY, id="early"),
        pytest.param(True, True, "mature", ProfileMaturity.MATURE, id="mature"),
    ],
)
def test_empirical_staged_bundles_are_publication_eligible_after_full_validation(
    tmp_path: Path,
    ratings: bool,
    drafts: bool,
    expected_stage: str,
    expected_maturity: ProfileMaturity,
) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment, ratings=ratings, drafts=drafts)

    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )

    assert result.selection is not None
    assert result.generation is not None
    assert result.validated is not None
    generation = result.generation
    validated = result.validated
    profile_bytes = validated.profile_bytes
    gzip_bytes = validated.gzip_bytes
    report_bytes = validated.report_bytes
    profile = SetProfile.from_json(json.loads(profile_bytes))
    report = generation.report

    assert profile == generation.profile
    assert profile.maturity is expected_maturity
    assert profile.set_code == environment.set_code.casefold()
    assert profile.event_format == environment.event_format.casefold()
    assert result.selection.stage.value == expected_stage
    assert report.stage == expected_stage
    assert report.set_code == profile.set_code
    assert report.event_format == profile.event_format

    assert gzip.decompress(gzip_bytes) == profile_bytes
    assert validated.profile_bytes == generation.profile.to_bytes()
    assert validated.gzip_bytes == generation.gzip_bytes
    assert validated.report_bytes == report.to_bytes()
    assert report.profile_bytes == len(profile_bytes)
    assert report.profile_sha256 == hashlib.sha256(profile_bytes).hexdigest()
    assert report.gzip_bytes == len(gzip_bytes)
    assert report.gzip_sha256 == hashlib.sha256(gzip_bytes).hexdigest()
    assert result.profile_size == len(validated.profile_bytes)
    assert result.profile_sha256 == hashlib.sha256(validated.profile_bytes).hexdigest()
    assert result.gzip_size == len(validated.gzip_bytes)
    assert result.gzip_sha256 == hashlib.sha256(validated.gzip_bytes).hexdigest()
    assert result.report_size == len(validated.report_bytes)
    assert result.report_sha256 == hashlib.sha256(validated.report_bytes).hexdigest()
    assert result.outcome is ProfileGenerationEnvironmentOutcome.PUBLICATION_ELIGIBLE


def test_publication_eligible_result_rejects_unrelated_payload_and_mismatches(
    tmp_path: Path,
) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment)
    first = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )
    second = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW + timedelta(seconds=1),
    )
    assert first.publication_eligible
    assert first.selection is not None
    assert first.validated is not None
    assert second.validated is not None

    with pytest.raises(ProfileGenerationExecutionError):
        replace(first, validated=second.validated)
    with pytest.raises(ProfileGenerationExecutionError):
        replace(first, environment=_environment("OTHER"))
    with pytest.raises(ProfileGenerationExecutionError):
        replace(
            first,
            selection=replace(first.selection, stage=ProfileGenerationStage.EARLY),
        )
    with pytest.raises(ProfileGenerationExecutionError):
        replace(first, skip_count=first.skip_count + 1)
    with pytest.raises(ProfileGenerationExecutionError):
        replace(first, error_count=first.error_count + 1)


def test_classification_diagnostics_do_not_block_metadata_fallback(tmp_path: Path) -> None:
    source = CardDatabase.from_json(json.loads((FIXTURE_DIR / "card-database.json").read_text()))
    unsupported_base = replace(
        source.cards[1],
        name="Unsupported Mechanic",
        keywords=("Futurecraft",),
        oracle_text="This card has an unsupported mechanic.",
    )
    gap = replace(
        source.cards[2],
        oracle_id="classification-gap",
        name="Ordinary Prose",
        oracle_text="The archivist smiles at the sunset.",
    )
    cards = {
        index: replace(
            unsupported_base,
            grp_id=index,
            arena_id=index,
            oracle_id=f"unsupported-{index}",
        )
        for index in range(1, 71)
    }
    cards[71] = replace(gap, grp_id=71, arena_id=71)
    environment = _environment()
    bundle_path = _stage(
        tmp_path,
        environment,
        card_database=replace(source, cards=cards),
    )

    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )

    assert result.publication_eligible
    assert result.selection is not None and result.selection.stage.value == "metadata"
    assert result.diagnostic_total == 71
    assert result.diagnostics_omitted == 7
    assert len(result.diagnostics) == 64
    diagnostic_sort_keys = [
        (
            item.card_key.casefold(),
            "" if item.card_name is None else item.card_name.casefold(),
            "" if item.mechanic is None else item.mechanic.casefold(),
            item.reason.casefold(),
        )
        for item in result.diagnostics
    ]
    assert diagnostic_sort_keys == sorted(diagnostic_sort_keys)
    assert any(item.mechanic == "Futurecraft" for item in result.diagnostics)
    assert all(item.mechanic == "Futurecraft" for item in result.diagnostics)
    assert all(len(item.card_key) <= 128 for item in result.diagnostics)
    assert any(
        item.reason == "No semantic mapping exists for mechanic 'Futurecraft'; add a reusable primitive or reviewed override."
        for item in result.diagnostics
    )


def test_retained_classification_diagnostics_report_cards_and_reasons(
    tmp_path: Path,
) -> None:
    source = CardDatabase.from_json(json.loads((FIXTURE_DIR / "card-database.json").read_text()))
    unsupported = replace(
        source.cards[1],
        grp_id=1,
        arena_id=1,
        oracle_id="unsupported-mechanic",
        name="Unsupported Mechanic",
        keywords=("Futurecraft",),
        oracle_text="This card has an unsupported mechanic.",
    )
    gap = replace(
        source.cards[2],
        grp_id=2,
        arena_id=2,
        oracle_id="classification-gap",
        name="Classification Gap",
        oracle_text="The archivist smiles at the sunset.",
    )
    environment = _environment()
    bundle_path = _stage(
        tmp_path,
        environment,
        card_database=replace(source, cards={1: unsupported, 2: gap}),
    )

    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )

    assert result.publication_eligible
    assert result.selection is not None and result.selection.stage.value == "metadata"
    assert result.diagnostic_total == 2
    assert result.diagnostics_omitted == 0
    assert len(result.diagnostics) == 2
    diagnostic_sort_keys = [
        (
            item.card_key.casefold(),
            "" if item.card_name is None else item.card_name.casefold(),
            "" if item.mechanic is None else item.mechanic.casefold(),
            item.reason.casefold(),
        )
        for item in result.diagnostics
    ]
    assert diagnostic_sort_keys == sorted(diagnostic_sort_keys)

    unsupported_diagnostic = next(
        item for item in result.diagnostics if item.card_name == "Unsupported Mechanic"
    )
    assert unsupported_diagnostic.card_key == "arena_id:1"
    assert unsupported_diagnostic.mechanic == "Futurecraft"
    assert unsupported_diagnostic.reason == (
        "No semantic mapping exists for mechanic 'Futurecraft'; "
        "add a reusable primitive or reviewed override."
    )

    gap_diagnostic = next(item for item in result.diagnostics if item.card_name == "Classification Gap")
    assert gap_diagnostic.card_key == "arena_id:2"
    assert gap_diagnostic.mechanic is None
    assert gap_diagnostic.reason == CLASSIFICATION_GAP_REASON


def test_classifier_errors_use_fixed_safe_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment)

    def fail_classification(self: object, card: object) -> object:
        raise ValueError(f"secret classifier details {tmp_path}")

    monkeypatch.setattr(execution.RoleClassifier, "classify", fail_classification)
    result = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )

    assert result.publication_eligible
    assert result.diagnostic_total == 2
    assert all(item.reason == CLASSIFICATION_ERROR_REASON for item in result.diagnostics)
    assert str(tmp_path) not in result.to_bytes().decode()


def test_generator_and_validation_failures_are_bounded_and_leave_staged_inputs_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment)
    before = {
        path.relative_to(bundle_path): path.read_bytes()
        for path in bundle_path.rglob("*")
        if path.is_file()
    }
    output_entries = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    def fail_generator(**_: object) -> ProfileGenerationResult:
        raise RuntimeError(f"secret path {tmp_path}")

    monkeypatch.setattr(execution, "generate_set_profile", fail_generator)
    generated = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )
    assert generated.outcome is ProfileGenerationEnvironmentOutcome.FAILED
    assert generated.failure_reason is ProfileGenerationFailureReason.GENERATION_FAILED
    assert generated.failure_phase is ProfileGenerationFailurePhase.GENERATION
    assert generated.generation is None and generated.validated is None
    assert str(tmp_path).encode() not in generated.to_bytes()

    monkeypatch.undo()
    monkeypatch.setattr(
        execution,
        "validate_profile_generation",
        lambda **_: (_ for _ in ()).throw(RuntimeError(f"secret path {tmp_path}")),
    )
    validated = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )
    assert validated.outcome is ProfileGenerationEnvironmentOutcome.FAILED
    assert validated.failure_reason is ProfileGenerationFailureReason.VALIDATION_FAILED
    assert validated.failure_phase is ProfileGenerationFailurePhase.VALIDATION
    assert validated.generation is None and validated.validated is None
    assert str(tmp_path).encode() not in validated.to_bytes()
    assert output_entries == {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert before == {
        path.relative_to(bundle_path): path.read_bytes()
        for path in bundle_path.rglob("*")
        if path.is_file()
    }


def test_identical_staged_inputs_produce_byte_identical_result_and_payloads(tmp_path: Path) -> None:
    environment = _environment()
    bundle_path = _stage(tmp_path, environment, ratings=True)
    first = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )
    second = generate_staged_environment_profile(
        bundle_path=bundle_path,
        environment=environment,
        generated_at=NOW,
    )

    assert first.to_bytes() == second.to_bytes()
    assert first.diagnostics == second.diagnostics
    assert first.profile_bytes == second.profile_bytes
    assert first.gzip_bytes == second.gzip_bytes
    assert first.report_bytes == second.report_bytes
    assert first.profile_sha256 == second.profile_sha256
    assert first.gzip_sha256 == second.gzip_sha256
    assert first.report_sha256 == second.report_sha256


def test_diagnostic_serialization_is_hash_seed_independent() -> None:
    script = """
import types

import draftomen.profile_generation_execution as execution
from draftomen.profile_generation_execution import (
    ProfileGenerationEnvironmentOutcome,
    ProfileGenerationEnvironmentResult,
)
from draftomen.refresh_plan import PlannedEnvironment


class Classifier:
    def classify(self, card):
        report = types.SimpleNamespace(
            card_key=f"{card.variant}-{card.index:03d}",
            card_name=f"{card.variant} Name",
            mechanic=f"{card.variant} Mechanic",
            reason=f"{card.variant} reason",
        )
        return types.SimpleNamespace(
            unknown_reports=(report,),
            assignments=(),
            card_key=report.card_key,
            card_name=report.card_name,
        )


execution.RoleClassifier = Classifier
cards = {
    index * 2 + variant_index: types.SimpleNamespace(
        index=index,
        variant=variant,
        unknown=False,
        set_code="TST",
        oracle_id=f"{variant}-{index:03d}",
        grp_id=index * 2 + variant_index,
    )
    for index in range(40)
    for variant_index, variant in enumerate(("Case", "case"))
}
diagnostics, total, omitted = execution._classify_cards(
    types.SimpleNamespace(cards=cards),
    "TST",
)
environment = PlannedEnvironment(
    set_code="TST",
    event_format="quickdraft",
    lifecycle="active",
    reasons=("manual-selection",),
)
result = ProfileGenerationEnvironmentResult(
    environment=environment,
    outcome=ProfileGenerationEnvironmentOutcome.FAILED,
    diagnostics=diagnostics,
    diagnostic_total=total,
    diagnostics_omitted=omitted,
    failure_phase="generation",
    failure_reason="generation-failed",
)
print(result.to_bytes().decode("utf-8"), end="")
"""
    root = Path(__file__).parents[1]

    def serialize(seed: str) -> bytes:
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        return subprocess.check_output(
            [sys.executable, "-c", script],
            cwd=root,
            env=environment,
        )

    assert serialize("1") == serialize("2")
