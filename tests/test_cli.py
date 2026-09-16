from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from draftomen import __version__
from draftomen import cli
from draftomen import config
from draftomen.audit import load_draft_audit_records
from draftomen.carddb import CardDatabase, build_card_database_from_bulk_file
from draftomen.cli import build_parser, main
from draftomen.deckbuilder import BuildPool, build_deck_from_pool, format_build_result
from draftomen.enrichment_inventory import ARTIFACT_ERROR, PUBLICATION_ERROR
from draftomen.pool import DraftState, load_draft_state, save_draft_state
from draftomen.pool_ledger import relationship_enhancement_is_compatible
from draftomen.profile_generation import generate_set_profile
from draftomen.profile_input_acquisition import (
    CardMetadataAdapter,
    SeventeenLandsPublicDraftAdapter,
    SeventeenLandsRatingsAdapter,
)
from draftomen.profile_input_cache import ProfileInputCache
from draftomen.profile_manifest import ProfileManifest, ProfileManifestArtifact
from draftomen.profile_refresh_execution import load_staged_profile_build_bundle
from draftomen.refresh_plan import LifecycleMetadata, PlannedEnvironment, RefreshPlan, write_refresh_plan
from draftomen.semantic_enrichment import (
    EnrichmentSources,
    GuideSource,
    SemanticEnrichmentArtifact,
    card_source_sha256,
    set_source_sha256,
)
from draftomen.semantic_enrichment_records import (
    ArtifactReview,
    CardSourcePin,
    FindingStatus,
    GuideEvidence,
    GuideSourcePin,
)
from draftomen.semantic_roles import Role, resolve_card_roles
from draftomen.session import (
    BuildResult,
    CardView,
    LiveSessionSnapshot,
    Recommendation,
    RecommendationState,
)
from draftomen.set_enrichment import (
    EnrichmentAccounting,
    EnrichmentOutcome,
    EnrichmentPhase,
    EnrichmentProgress,
)
from draftomen.set_enrichment_workflow import ANALYSIS_ERROR, NO_PUBLISHABLE_ERROR
from draftomen.set_profile import (
    SetProfile,
    dump_set_profile,
    load_set_profile,
    set_profile_path,
)
from draftomen.seventeen import (
    QUICK_DRAFT_FORMAT,
    load_17lands_format_data,
    seventeen_lands_structure_targets_cache_path,
)
from draftomen.test_draft import (
    TestDraftError,
    TestDraftInspection,
    TestDraftOfferIdentity,
    TestDraftRunResult,
    TestDraftStep,
)
from draftomen.tui import DraftomenTuiApp
from draftomen.watch import PlainLogWatcher

from tests.test_profile_generation import (
    TYPED_SOURCE_CARD_ID,
    TYPED_TARGET_CARD_ID,
    _enrichment_run,
    _ratings,
    _typed_database,
    _typed_relationship,
)


SCRYFALL_BULK_SAMPLE_PATH = (
    Path(__file__).parent / "fixtures" / "scryfall-default-cards-sample.jsonl"
)
QUICK_DRAFT_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "quick-draft-msh-player.log"
)
FIXTURE_ACCOUNT_ID = "FIXTURECLIENTID1234567890"
FIXTURE_DRAFT_ID = "00000000-0000-4000-8000-000000000004"

PROFILE_GENERATION_FIXTURE_DIR = (
    Path(__file__).parent / "fixtures" / "profile-generation"
)
CLI_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

REFRESH_PLAN_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "refresh-plan"
PROFILE_GENERATION_AT = "2026-08-30T12:00:00+00:00"
CLI_REFRESH_TIMESTAMP = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def test_package_version_matches_installed_distribution_metadata() -> None:
    assert __version__ == version(distribution_name="draftomen")


def test_tui_version_output_includes_required_disclaimer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(argv=["--version"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"draftomen-tui {__version__}" in captured.out
    assert (
        "Draft Omen is unofficial Fan Content permitted under the Fan Content Policy."
        in captured.out
    )
    assert "17Lands does not endorse this tool." in captured.out


def test_tui_parser_uses_tui_command_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(args=["--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert "usage: draftomen-tui" in captured.out
    assert "Unofficial Quick Draft assistant for MTG Arena (TUI)." in captured.out


@pytest.mark.parametrize(
    ("command", "expected_help"),
    [
        ("watch", "Live"),
        ("replay", "Deterministic"),
        ("build", "Select"),
        ("test-draft", "headless"),
        ("backtest", "Dry-run"),
        ("benchmark-picks", "Offline benchmark"),
        ("refresh-data", "Scryfall"),
        ("refresh-structure-targets", "17Lands"),
        ("refresh-profile-data", "Refresh"),
        ("generate-profile", "Generate"),
        ("execute-profile-refresh", "Acquire and stage"),
        ("generate-profile-refresh-batch", "Generate profiles"),
        ("export-set-data", "Export"),
        ("enrich-set", "Freeze"),
        ("republish-enrichment", "Recompile"),
        ("list-enrichment", "Report every local enrichment run"),
    ],
)
def test_subcommands_are_registered_with_help_text(
    command: str,
    expected_help: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as error:
        parser.parse_args(args=[command, "--help"])

    captured = capsys.readouterr()

    assert error.value.code == 0
    assert command in captured.out
    assert expected_help in captured.out

def test_export_set_data_parser_defaults_and_options() -> None:
    parser = build_parser()

    defaults = parser.parse_args(args=["export-set-data"])
    assert defaults.set is None
    assert defaults.inventory_file is None
    assert defaults.bulk_file is None
    assert defaults.output_dir == Path("website/public/card-data")
    assert defaults.timeout == cli.HTTP_TIMEOUT_SECONDS

    args = parser.parse_args(
        args=[
            "export-set-data",
            "tst",
            "--inventory-file",
            "inventory.json",
            "--bulk-file",
            "bulk.jsonl.gz",
            "--output-dir",
            "out",
            "--timeout",
            "17",
        ]
    )
    assert args.set == "tst"
    assert args.inventory_file == Path("inventory.json")
    assert args.bulk_file == Path("bulk.jsonl.gz")
    assert args.output_dir == Path("out")
    assert args.timeout == 17


def test_refresh_profile_data_parser_supports_selector_modes_and_app_dir() -> None:
    parser = build_parser()

    defaults = parser.parse_args(args=["refresh-profile-data"])
    assert defaults.set is None
    assert defaults.active is False
    assert defaults.historical is False
    assert defaults.app_dir is None

    selected = parser.parse_args(
        args=["refresh-profile-data", "HOB", "--app-dir", "cache"]
    )
    assert selected.set == "HOB"
    assert selected.app_dir == Path("cache")

    active = parser.parse_args(args=["refresh-profile-data", "--active"])
    historical = parser.parse_args(args=["refresh-profile-data", "--historical"])
    assert active.active is True
    assert historical.historical is True

    with pytest.raises(SystemExit) as error:
        parser.parse_args(
            args=["refresh-profile-data", "--active", "--historical"]
        )
    assert error.value.code == 2


def test_refresh_profile_data_rejects_selector_with_lifecycle_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(argv=["refresh-profile-data", "HOB", "--active"])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        "refresh-profile-data: SET cannot be combined with --active or --historical\n"
    )


def test_refresh_profile_data_passes_app_dir_only_to_ratings_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute_kwargs: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "prepare_profile_data_refresh",
        lambda **kwargs: SimpleNamespace(count=0, pairs=()),
    )

    def fake_execute(plan: object, **kwargs: object) -> object:
        execute_kwargs.update(kwargs)
        return SimpleNamespace(enrichment_conflicts=(), failures=(), succeeded=True)

    monkeypatch.setattr(cli, "execute_profile_data_refresh", fake_execute)

    assert main(argv=["refresh-profile-data", "--app-dir", "cache"]) == 0
    assert execute_kwargs == {"cache_dir": Path("cache")}


def test_refresh_profile_data_prints_plan_before_execution_and_flushes(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    pairs = (
        SimpleNamespace(set_code="aaa", set_name="Alpha Set", event_format="PremierDraft"),
        SimpleNamespace(set_code="bbb", set_name="Beta Set", event_format="QuickDraft"),
    )

    def fake_prepare(**kwargs: object) -> object:
        calls.append("prepare")
        return SimpleNamespace(count=2, pairs=pairs)

    def fake_execute(plan: object, **kwargs: object) -> object:
        calls.append(capsys.readouterr().out)
        return SimpleNamespace(enrichment_conflicts=(), failures=(), succeeded=True)

    monkeypatch.setattr(cli, "prepare_profile_data_refresh", fake_prepare)
    monkeypatch.setattr(cli, "execute_profile_data_refresh", fake_execute)

    assert main(argv=["refresh-profile-data"]) == 0

    assert calls == [
        "prepare",
        "selected 2 profile pairs\nAAA - Alpha Set / PremierDraft\n"
        "BBB - Beta Set / QuickDraft\n",
    ]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("failure_count", [1, 2])
def test_refresh_profile_data_prints_all_failures_and_returns_nonzero(
    failure_count: int,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = tuple(
        SimpleNamespace(
            set_code=code,
            set_name=name,
            event_format="QuickDraft",
        )
        for code, name in (("aaa", "Alpha Set"), ("bbb", "Beta Set"))
    )
    failures = tuple(
        SimpleNamespace(pair=pairs[index], category="ratings-unavailable")
        for index in range(failure_count)
    )
    monkeypatch.setattr(
        cli,
        "prepare_profile_data_refresh",
        lambda **kwargs: SimpleNamespace(count=2, pairs=pairs),
    )
    monkeypatch.setattr(
        cli,
        "execute_profile_data_refresh",
        lambda plan, **kwargs: SimpleNamespace(
            enrichment_conflicts=(),
            failures=failures,
            succeeded=False,
        ),
    )

    assert main(argv=["refresh-profile-data"]) == 1

    captured = capsys.readouterr()
    assert captured.out == (
        "selected 2 profile pairs\nAAA - Alpha Set / QuickDraft\n"
        "BBB - Beta Set / QuickDraft\n"
    )
    assert captured.err == "".join(
        "refresh-profile-data failed: "
        f"{pairs[index].set_code.upper()} - {pairs[index].set_name} / QuickDraft: "
        "ratings-unavailable\n"
        for index in range(failure_count)
    )


@pytest.mark.parametrize("stage", ["prepare", "execute"])
def test_refresh_profile_data_top_level_errors_are_path_free(
    stage: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if stage == "prepare":
        monkeypatch.setattr(
            cli,
            "prepare_profile_data_refresh",
            lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("/private/user/path should not be printed")
            ),
        )
    else:
        monkeypatch.setattr(
            cli,
            "prepare_profile_data_refresh",
            lambda **kwargs: SimpleNamespace(count=0, pairs=()),
        )
        monkeypatch.setattr(
            cli,
            "execute_profile_data_refresh",
            lambda plan, **kwargs: (_ for _ in ()).throw(
                RuntimeError("/private/user/path should not be printed")
            ),
        )

    assert main(argv=["refresh-profile-data"]) == 1

    captured = capsys.readouterr()
    assert "/private/user/path" not in captured.err
    assert captured.err in {
        "refresh-profile-data: unable to prepare profile refresh plan\n",
        "refresh-profile-data: unable to execute profile refresh\n",
    }


@pytest.mark.parametrize("timeout", ["0", "-1", "not-an-int"])
def test_export_set_data_parser_rejects_invalid_timeout(timeout: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(args=["export-set-data", "--timeout", timeout])

    assert error.value.code == 2


@pytest.mark.parametrize("selector", ["TST", "test set"])
def test_export_set_data_single_mode_resolves_selector_and_publishes(
    selector: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = SimpleNamespace(set_code="tst", set_name="Test Set")
    candidate = SimpleNamespace(
        identity=identity,
        target_path=tmp_path / "tst.json.gz",
        gzip_bytes=b"canonical",
    )
    calls: list[dict[str, object]] = []
    published: list[object] = []

    def fake_prepare(**kwargs: object) -> object:
        calls.append(kwargs)
        return SimpleNamespace(total=1, already_valid=(), pending=(candidate,))

    def fake_publish(*, candidate: object) -> Path:
        published.append(candidate)
        return tmp_path / "tst.json.gz"

    monkeypatch.setattr(cli, "prepare_set_data_export", fake_prepare)
    monkeypatch.setattr(cli, "publish_set_data_export", fake_publish)

    assert (
        main(
            argv=[
                "export-set-data",
                selector,
                "--output-dir",
                str(tmp_path),
            ]
        )
        == 0
    )

    assert calls == [
        {
            "selector": selector,
            "output_dir": tmp_path,
            "inventory_file": None,
            "bulk_file": None,
            "timeout_seconds": cli.HTTP_TIMEOUT_SECONDS,
        }
    ]
    assert published == [candidate]
    assert capsys.readouterr().out == (
        "wrote TST - Test Set -> " + str(tmp_path / "tst.json.gz") + "\n"
    )


def test_export_set_data_all_mode_lists_every_pending_set_before_publishing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = tuple(
        SimpleNamespace(
            identity=SimpleNamespace(set_code=code, set_name=name),
            target_path=tmp_path / f"{code}.json.gz",
            gzip_bytes=b"canonical",
        )
        for code, name in (("aaa", "Alpha"), ("bbb", "Beta"))
    )
    published: list[object] = []
    output_seen_before_publish: list[str] = []

    prepare_calls: list[dict[str, object]] = []

    def fake_prepare(**kwargs: object) -> object:
        prepare_calls.append(kwargs)
        return SimpleNamespace(total=2, already_valid=(), pending=candidates)

    monkeypatch.setattr(cli, "prepare_set_data_export", fake_prepare)

    def fake_publish(*, candidate: object) -> Path:
        output_seen_before_publish.append(capsys.readouterr().out)
        published.append(candidate)
        return candidate.target_path  # type: ignore[union-attr]

    monkeypatch.setattr(cli, "publish_set_data_export", fake_publish)

    assert main(argv=["export-set-data"]) == 0

    assert published == list(candidates)
    assert prepare_calls[0]["output_dir"] == Path("website/public/card-data")
    assert output_seen_before_publish[0] == (
        "total=2 already-valid=0 pending=2\nAAA - Alpha\nBBB - Beta\n"
    )
    assert output_seen_before_publish[1] == (
        "wrote AAA - Alpha -> " + str(tmp_path / "aaa.json.gz") + "\n"
    )
    assert capsys.readouterr().out == (
        "wrote BBB - Beta -> " + str(tmp_path / "bbb.json.gz") + "\n"
    )


def test_export_set_data_all_mode_valid_rerun_reports_no_pending_without_publishing(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = False

    monkeypatch.setattr(
        cli,
        "prepare_set_data_export",
        lambda **kwargs: SimpleNamespace(
            total=2,
            already_valid=(
                SimpleNamespace(set_code="aaa", set_name="Alpha"),
                SimpleNamespace(set_code="bbb", set_name="Beta"),
            ),
            pending=(),
        ),
    )

    def fake_publish(*, candidate: object) -> Path:
        nonlocal published
        published = True
        raise AssertionError("no-pending plans must not publish")

    monkeypatch.setattr(cli, "publish_set_data_export", fake_publish)

    assert main(argv=["export-set-data"]) == 0
    assert published is False
    assert capsys.readouterr().out == "total=2 already-valid=2 pending=0\n"


def test_export_set_data_failure_is_concise_and_returns_one(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_prepare(**kwargs: object) -> object:
        raise cli.SetDataExportError("source data unavailable")

    monkeypatch.setattr(cli, "prepare_set_data_export", fail_prepare)

    assert main(argv=["export-set-data"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "export-set-data failed: source data unavailable\n"


def test_export_set_data_interrupt_preserves_earlier_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = tuple(
        SimpleNamespace(
            identity=SimpleNamespace(set_code=code, set_name=name),
            target_path=tmp_path / f"{code}.json.gz",
            gzip_bytes=b"canonical",
        )
        for code, name in (("aaa", "Alpha"), ("bbb", "Beta"))
    )
    published: list[object] = []

    monkeypatch.setattr(
        cli,
        "prepare_set_data_export",
        lambda **kwargs: SimpleNamespace(
            total=2,
            already_valid=(),
            pending=candidates,
        ),
    )

    def fake_publish(*, candidate: object) -> Path:
        published.append(candidate)
        if len(published) == 2:
            raise KeyboardInterrupt
        return candidate.target_path  # type: ignore[union-attr]

    monkeypatch.setattr(cli, "publish_set_data_export", fake_publish)

    assert main(argv=["export-set-data"]) == 130
    assert published == list(candidates)


def test_generate_profile_refresh_batch_parser_registers_exact_options() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "generate-profile-refresh-batch",
            "--plan",
            "plan.json",
            "--staged-dir",
            "staged",
            "--generated-at",
            PROFILE_GENERATION_AT,
        ]
    )

    assert args.plan == Path("plan.json")
    assert args.staged_dir == Path("staged")
    assert args.generated_at == datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    assert args.profile_version == "1.0"

    base = [
        "generate-profile-refresh-batch",
        "--plan",
        "plan.json",
        "--staged-dir",
        "staged",
        "--generated-at",
        PROFILE_GENERATION_AT,
    ]
    for option in ("--plan", "--staged-dir", "--generated-at"):
        missing = list(base)
        position = missing.index(option)
        del missing[position : position + 2]
        with pytest.raises(SystemExit):
            parser.parse_args(missing)


def test_execute_profile_refresh_parser_registers_exact_options() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "execute-profile-refresh",
            "--plan",
            "plan.json",
            "--cache-dir",
            "cache",
            "--output-dir",
            "output",
            "--offline",
        ]
    )

    assert args.plan == Path("plan.json")
    assert args.cache_dir == Path("cache")
    assert args.output_dir == Path("output")
    assert args.offline is True

    base = [
        "execute-profile-refresh",
        "--plan",
        "plan.json",
        "--cache-dir",
        "cache",
        "--output-dir",
        "output",
    ]
    for option in ("--plan", "--cache-dir", "--output-dir"):
        missing = list(base)
        position = missing.index(option)
        del missing[position : position + 2]
        with pytest.raises(SystemExit):
            parser.parse_args(missing)


def test_generate_profile_refresh_batch_handler_emits_canonical_bytes_and_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    expected = b'{"counts":{"failed":0,"planned":1,"publication_eligible":1}}\n'
    plan = object()
    calls: dict[str, object] = {}

    class Result:
        succeeded = True

        def to_bytes(self) -> bytes:
            calls["to_bytes"] = calls.get("to_bytes", 0) + 1
            return expected

    def load(path: Path) -> object:
        calls["plan_path"] = path
        return plan

    def generate(**kwargs: object) -> Result:
        calls["generate"] = kwargs
        return Result()

    monkeypatch.setattr(cli, "load_refresh_plan", load)
    monkeypatch.setattr(cli, "generate_staged_profile_batch", generate)

    exit_code = main(
        argv=[
            "generate-profile-refresh-batch",
            "--plan",
            str(tmp_path / "private-plan.json"),
            "--staged-dir",
            str(tmp_path / "private-staged"),
            "--generated-at",
            PROFILE_GENERATION_AT,
            "--profile-version",
            "2.0",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.encode() == expected
    assert captured.err == ""
    assert calls["plan_path"] == tmp_path / "private-plan.json"
    assert calls["generate"] == {
        "plan": plan,
        "staged_dir": tmp_path / "private-staged",
        "generated_at": datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        "profile_version": "2.0",
    }
    assert calls["to_bytes"] == 1


def test_generate_profile_refresh_batch_handler_emits_report_before_failed_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = b'{"counts":{"failed":1,"planned":1,"publication_eligible":0}}\n'

    class Result:
        succeeded = False

        def to_bytes(self) -> bytes:
            return expected

    monkeypatch.setattr(cli, "load_refresh_plan", lambda path: object())
    monkeypatch.setattr(cli, "generate_staged_profile_batch", lambda **kwargs: Result())

    exit_code = main(
        argv=[
            "generate-profile-refresh-batch",
            "--plan",
            "plan.json",
            "--staged-dir",
            "staged",
            "--generated-at",
            PROFILE_GENERATION_AT,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out.encode() == expected
    assert captured.err == ""


@pytest.mark.parametrize("stage", ["plan", "batch"])
def test_generate_profile_refresh_batch_errors_are_generic_and_path_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    stage: str,
) -> None:
    sentinel = "PRIVATE-EXCEPTION-DETAIL"
    private_plan = tmp_path / "private-plan-sentinel.json"
    private_staged = tmp_path / "private-staged-sentinel"
    if stage == "plan":
        monkeypatch.setattr(
            cli,
            "load_refresh_plan",
            lambda path: (_ for _ in ()).throw(ValueError(sentinel)),
        )
        monkeypatch.setattr(cli, "generate_staged_profile_batch", lambda **kwargs: None)
        expected_error = "generate-profile-refresh-batch: invalid plan\n"
    else:
        monkeypatch.setattr(cli, "load_refresh_plan", lambda path: object())
        monkeypatch.setattr(
            cli,
            "generate_staged_profile_batch",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError(sentinel)),
        )
        expected_error = "generate-profile-refresh-batch: batch generation error\n"

    exit_code = main(
        argv=[
            "generate-profile-refresh-batch",
            "--plan",
            str(private_plan),
            "--staged-dir",
            str(private_staged),
            "--generated-at",
            PROFILE_GENERATION_AT,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == expected_error
    assert str(tmp_path) not in captured.err
    assert sentinel not in captured.err


@pytest.mark.parametrize("selection_mode", ["manual", "active", "history"])
def test_generate_profile_refresh_batch_cli_accepts_each_selection_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    selection_mode: str,
) -> None:
    base_plan = _cli_refresh_plan()
    if selection_mode == "manual":
        plan = replace(
            base_plan,
            selection_mode="manual",
            selection_set_code="EARLY",
            max_environments=None,
            environments=(next(item for item in base_plan.environments if item.set_code == "EARLY"),),
        )
    elif selection_mode == "active":
        plan = replace(
            base_plan,
            selection_mode="active",
            selection_set_code=None,
            max_environments=None,
            environments=tuple(
                item for item in base_plan.environments if item.lifecycle == "active"
            ),
        )
    else:
        plan = base_plan
    plan_path = tmp_path / f"{selection_mode}-plan.json"
    write_refresh_plan(plan_path, plan)
    expected = f'{{"selection_mode":"{selection_mode}"}}\n'.encode()
    calls: list[RefreshPlan] = []

    class Result:
        succeeded = True

        def to_bytes(self) -> bytes:
            return expected

    def generate(**kwargs: object) -> Result:
        candidate = kwargs["plan"]
        assert isinstance(candidate, RefreshPlan)
        calls.append(candidate)
        assert kwargs["staged_dir"] == tmp_path / "staged"
        assert kwargs["generated_at"] == CLI_REFRESH_TIMESTAMP
        return Result()

    monkeypatch.setattr(cli, "generate_staged_profile_batch", generate)
    exit_code = main(
        argv=[
            "generate-profile-refresh-batch",
            "--plan",
            str(plan_path),
            "--staged-dir",
            str(tmp_path / "staged"),
            "--generated-at",
            CLI_REFRESH_TIMESTAMP.isoformat(),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.encode() == expected
    assert captured.err == ""
    assert [item.selection_mode for item in calls] == [selection_mode]

def test_execute_profile_refresh_handler_emits_canonical_bytes_and_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    plan = object()
    cache = object()
    expected = b'{"counts":{"failed":0}}\n'
    calls: dict[str, object] = {}

    class Result:
        succeeded = True

        def to_bytes(self) -> bytes:
            calls["to_bytes"] = calls.get("to_bytes", 0) + 1
            return expected

    def load(path: Path) -> object:
        calls["plan"] = path
        return plan

    def make_cache(root: Path, *, policy: object) -> object:
        calls["cache_root"] = root
        calls["policy"] = policy
        return cache

    def execute(**kwargs: object) -> Result:
        calls["execute"] = kwargs
        return Result()

    monkeypatch.setattr(cli, "load_refresh_plan", load)
    monkeypatch.setattr(cli, "ProfileInputCache", make_cache)
    monkeypatch.setattr(cli, "execute_profile_refresh_plan", execute)

    exit_code = cli.handle_execute_profile_refresh(
        SimpleNamespace(
            plan=tmp_path / "private-plan.json",
            cache_dir=tmp_path / "private-cache",
            output_dir=tmp_path / "private-output",
            offline=True,
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.encode() == expected
    assert captured.err == ""
    assert calls["plan"] == tmp_path / "private-plan.json"
    assert calls["cache_root"] == tmp_path / "private-cache"
    assert calls["policy"] is cli.DEFAULT_PROFILE_REFRESH_CACHE_POLICY
    assert calls["execute"] == {
        "plan": plan,
        "cache": cache,
        "output_dir": tmp_path / "private-output",
        "offline": True,
    }
    assert calls["to_bytes"] == 1


def test_execute_profile_refresh_handler_emits_result_before_failed_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = b'{"counts":{"failed":1,"planned":1}}\n'

    class Result:
        succeeded = False

        def to_bytes(self) -> bytes:
            return expected

    monkeypatch.setattr(cli, "load_refresh_plan", lambda path: object())
    monkeypatch.setattr(cli, "ProfileInputCache", lambda root, *, policy: object())
    monkeypatch.setattr(cli, "execute_profile_refresh_plan", lambda **kwargs: Result())

    exit_code = cli.handle_execute_profile_refresh(
        SimpleNamespace(
            plan=Path("plan.json"),
            cache_dir=Path("cache"),
            output_dir=Path("output"),
            offline=False,
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out.encode() == expected
    assert captured.err == ""


@pytest.mark.parametrize(
    ("stage", "message"),
    [
        ("plan", "execute-profile-refresh: invalid plan\n"),
        ("cache", "execute-profile-refresh: cache error\n"),
        ("execution", "execute-profile-refresh: execution error\n"),
    ],
)
def test_execute_profile_refresh_handler_errors_are_generic_and_path_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    stage: str,
    message: str,
) -> None:
    sentinel = "PRIVATE-EXCEPTION-DETAIL"
    private_path = tmp_path / "private-plan-sentinel.json"
    if stage == "plan":
        monkeypatch.setattr(
            cli,
            "load_refresh_plan",
            lambda path: (_ for _ in ()).throw(ValueError(sentinel)),
        )
    else:
        monkeypatch.setattr(cli, "load_refresh_plan", lambda path: object())
    if stage == "cache":
        monkeypatch.setattr(
            cli,
            "ProfileInputCache",
            lambda root, *, policy: (_ for _ in ()).throw(RuntimeError(sentinel)),
        )
    else:
        monkeypatch.setattr(cli, "ProfileInputCache", lambda root, *, policy: object())
    if stage == "execution":
        monkeypatch.setattr(
            cli,
            "execute_profile_refresh_plan",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError(sentinel)),
        )

    exit_code = cli.handle_execute_profile_refresh(
        SimpleNamespace(
            plan=private_path,
            cache_dir=tmp_path / "private-cache",
            output_dir=tmp_path / "private-output",
            offline=False,
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == message
    assert str(tmp_path) not in captured.err
    assert sentinel not in captured.err

def test_watch_profile_manifest_url_override_and_replay_rejection() -> None:
    parser = build_parser()
    watch = parser.parse_args(
        args=["watch", "--profile-manifest-url", "https://profiles.example.test/m.json"]
    )
    assert watch.profile_manifest_url == "https://profiles.example.test/m.json"

    with pytest.raises(SystemExit):
        parser.parse_args(
            args=[
                "replay",
                str(QUICK_DRAFT_FIXTURE_PATH),
                "--profile-manifest-url",
                "https://profiles.example.test/m.json",
            ]
        )


def test_refresh_profile_parser_requires_named_refresh_inputs() -> None:
    parser = build_parser()
    args = parser.parse_args(
        args=[
            "refresh-profile",
            "--set-code",
            "TST",
            "--format",
            QUICK_DRAFT_FORMAT,
            "--manifest-url",
            "https://profiles.example.test/m.json",
            "--app-dir",
            "/tmp/draftomen",
        ]
    )
    assert args.set_code == "TST"
    assert args.format == QUICK_DRAFT_FORMAT
    assert args.manifest_url == "https://profiles.example.test/m.json"
    assert args.app_dir == Path("/tmp/draftomen")


def test_refresh_profile_prints_compact_outcome_and_uses_cached_profile_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeProfileClient:
        def __init__(self, *, app_dir, manifest_url, network_policy) -> None:
            assert app_dir == tmp_path / "app"
            assert manifest_url == "https://profiles.example.test/m.json"
            assert network_policy.value == "allowed"

        def refresh(self, set_code, event_format, *, network_policy):
            calls.append((set_code, event_format))
            return SimpleNamespace(
                profile=SimpleNamespace(
                    set_code="TST",
                    event_format=event_format.casefold(),
                ),
                maturity=SimpleNamespace(value="mature"),
                status="remote-failed",
            )

        def profile_path(self, set_code, event_format):
            return tmp_path / "app" / f"{set_code.casefold()}-{event_format.casefold()}.json"

    monkeypatch.setattr(cli, "ProfileClient", FakeProfileClient)
    args = build_parser().parse_args(
        args=[
            "refresh-profile",
            "--set-code",
            "TST",
            "--format",
            QUICK_DRAFT_FORMAT,
            "--manifest-url",
            "https://profiles.example.test/m.json",
            "--app-dir",
            str(tmp_path / "app"),
        ]
    )

    assert args.handler(args) == 0
    assert calls == [("TST", QUICK_DRAFT_FORMAT)]
    output = capsys.readouterr()
    assert output.err == ""
    assert (
        "refresh-profile: set_code=TST format=quickdraft "
        "maturity=mature outcome=remote-failed "
        f"cache_path={tmp_path / 'app' / 'tst-quickdraft.json'}"
    ) in output.out

def test_corpus_build_cli_local_then_offline_is_byte_stable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture_dir = Path(__file__).parent / "fixtures"
    source_spec = tmp_path / "sources.json"
    source_spec.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sources": [
                    {
                        "name": "scryfall",
                        "kind": "scryfall",
                        "path": str(fixture_dir / "corpus-scryfall.jsonl"),
                    },
                    {
                        "name": "arena-cards",
                        "kind": "arena",
                        "path": str(fixture_dir / "corpus-arena-cards.json"),
                    },
                    {
                        "name": "arena-localization",
                        "kind": "arena",
                        "path": str(fixture_dir / "corpus-arena-localization.json"),
                    },
                    {
                        "name": "mtgjson",
                        "kind": "mtgjson",
                        "path": str(fixture_dir / "corpus-mtgjson.json"),
                    },
                ],
                "selection": {"mode": "explicit", "sets": ["hbl", "dsk"]},
            }
        ),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "cache"
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    common = [
        "corpus-build",
        "--source-spec",
        str(source_spec),
        "--cache-dir",
        str(cache_dir),
    ]

    assert main([*common, "--output-dir", str(first_dir)]) == 0
    first_output = capsys.readouterr()
    assert "built" in first_output.out
    assert first_output.err == ""

    assert main([*common, "--output-dir", str(second_dir), "--offline"]) == 0
    second_output = capsys.readouterr()
    assert "built" in second_output.out
    assert second_output.err == ""
    assert (first_dir / "normalized.jsonl").read_bytes() == (
        second_dir / "normalized.jsonl"
    ).read_bytes()
    assert (first_dir / "coverage.json").read_bytes() == (
        second_dir / "coverage.json"
    ).read_bytes()


def test_splash_is_default_on_and_each_user_flow_can_disable_it() -> None:
    parser = build_parser()

    assert parser.parse_args(args=["watch"]).splash_enabled is None
    assert parser.parse_args(args=["watch", "--splash"]).splash_enabled is True
    assert parser.parse_args(args=["watch", "--no-splash"]).splash_enabled is False
    assert parser.parse_args(args=["replay", "draft.log"]).splash_enabled is True
    assert (
        parser.parse_args(args=["replay", "draft.log", "--no-splash"]).splash_enabled
        is False
    )
    assert parser.parse_args(args=["build"]).allow_splash is True
    assert parser.parse_args(args=["build", "--no-splash"]).allow_splash is False
    assert parser.parse_args(args=["backtest"]).splash_enabled is True
    assert parser.parse_args(args=["backtest", "--no-splash"]).splash_enabled is False


def test_watch_plain_once_honors_log_path_override(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")

    exit_code = main(
        argv=[
            "watch",
            "--log-path",
            str(log_path),
            "--plain",
            "--bulk-file",
            str(SCRYFALL_BULK_SAMPLE_PATH),
            "--app-dir",
            str(tmp_path / "app"),
            "--once",
            "--offline-profiles",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert str(log_path) in captured.out
    assert "Mode: plain-text" in captured.out
    assert captured.err == ""


def test_watch_plain_once_ignores_quick_draft_course_snapshot_outside_botdraft(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        json.dumps(
            {
                "Course": {
                    "CourseId": "00000000-0000-4000-8000-000000000078",
                    "InternalEventName": "QuickDraft_ABC_20260702",
                    "CurrentModule": "DeckSelect",
                    "ModulePayload": "",
                    "CourseDeckSummary": {"Attributes": []},
                    "CardPool": [],
                    "CardStyles": [],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = main(
        argv=[
            "watch",
            "--log-path",
            str(log_path),
            "--plain",
            "--bulk-file",
            str(SCRYFALL_BULK_SAMPLE_PATH),
            "--app-dir",
            str(tmp_path / "app"),
            "--once",
            "--offline-profiles",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Mode: plain-text" in captured.out
    assert captured.err == ""


def test_watch_plain_actual_entrypoint_processes_complete_fixture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        QUICK_DRAFT_FIXTURE_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    app_dir = tmp_path / "app"

    exit_code = main(
        argv=[
            "watch",
            "--log-path",
            str(log_path),
            "--plain",
            "--bulk-file",
            str(SCRYFALL_BULK_SAMPLE_PATH),
            "--app-dir",
            str(app_dir),
            "--once",
            "--offline-profiles",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.count("Pack ") == 42
    assert captured.out.count("Chosen card:") == 42
    assert "Draft complete: 42 cards (explicit completion)" in captured.out
    assert "Suggested deck" in captured.out
    assert (
        "Pool: watch FIXTURECLIENTID1234567890/"
        "00000000-0000-4000-8000-000000000004"
    ) in captured.out
    assert captured.err == ""

    state = load_draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=app_dir,
    )
    assert state.completed is True
    assert state.chosen_pick_count == 42
    assert len(state.pool_grp_ids) == 42
    audit_records = load_draft_audit_records(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=app_dir,
    )
    assert len(audit_records) == 86
    assert audit_records[-1]["record_type"] == "draft_completed"


def test_watch_tui_once_is_default_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")

    exit_code = main(
        argv=[
            "watch",
            "--log-path",
            str(log_path),
            "--bulk-file",
            str(SCRYFALL_BULK_SAMPLE_PATH),
            "--app-dir",
            str(tmp_path / "app"),
            "--once",
            "--offline-profiles",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_watch_mana_icons_flag_is_explicit_tui_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_tui_watch(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_tui_watch", fake_run_tui_watch)

    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")

    exit_code = main(
        argv=[
            "watch",
            "--log-path",
            str(log_path),
            "--mana-icons",
            "--bulk-file",
            str(SCRYFALL_BULK_SAMPLE_PATH),
            "--once",
        ]
    )

    default_args = build_parser().parse_args(args=["watch"])

    assert exit_code == 0
    assert default_args.mana_icons is False
    assert isinstance(captured["card_database"], CardDatabase)
    assert captured["set_card_data_loader"] is None
    assert captured["mana_icons_enabled"] is True





def test_build_pool_file_selects_pair_offline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bulk_file = _write_build_bulk_file(directory=tmp_path)
    pool_file = tmp_path / "pool.json"
    pool_file.write_text(
        json.dumps({"set_code": "TST", "pool_grp_ids": [1, 2, 3, 4, 5]}),
        encoding="utf-8",
    )

    exit_code = main(
        argv=[
            "build",
            "--pool",
            str(pool_file),
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(tmp_path / "app"),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.startswith("Suggested deck\n")
    assert "Color pair: WU (automatic" in captured.out
    assert "Average mana value:" in captured.out
    assert "Mana curve: 0:" in captured.out
    assert "Chosen pair: WU (automatic" in captured.out
    assert "Runner-up:" in captured.out
    assert "Strength gap:" in captured.out
    assert "Structure checks:" in captured.out
    assert "Selected spells: 4/23" in captured.out
    assert "Card data from 17Lands" in captured.out
    assert captured.err == ""


def test_build_cli_loads_local_profile_and_passes_it_to_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_dir = tmp_path / "app"
    profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json",
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )
    dump_set_profile(
        profile,
        set_profile_path(
            set_code="TST",
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=app_dir,
        ),
    )
    bulk_file = _write_build_bulk_file(directory=tmp_path)
    pool_file = tmp_path / "pool.json"
    pool_file.write_text(
        json.dumps({"set_code": "TST", "pool_grp_ids": [1, 2, 3, 4, 5]}),
        encoding="utf-8",
    )

    loaded_profiles: list[SetProfile | None] = []
    real_load = cli.load_scoring_profile

    def record_load(
        set_code: str,
        event_format: str,
        *,
        app_dir: Path | None = None,
        **kwargs: object,
    ) -> SetProfile | None:
        loaded = real_load(
            set_code=set_code,
            event_format=event_format,
            app_dir=app_dir,
            **kwargs,
        )
        loaded_profiles.append(loaded)
        return loaded

    builder_kwargs: dict[str, object] = {}

    def record_build(**kwargs: object) -> tuple[object, SimpleNamespace]:
        builder_kwargs.update(kwargs)
        return object(), SimpleNamespace(
            spell_selection=object(),
            mana_base=object(),
        )

    monkeypatch.setattr(cli, "load_scoring_profile", record_load)
    monkeypatch.setattr(cli, "build_deck_from_pool", record_build)
    monkeypatch.setattr(cli, "format_build_result", lambda **kwargs: "stub build\n")

    exit_code = main(
        argv=[
            "build",
            "--pool",
            str(pool_file),
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(app_dir),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert loaded_profiles == [profile]
    assert builder_kwargs["set_profile"] is loaded_profiles[0]
    assert captured.out == "stub build\n"
    assert captured.err == ""


def test_build_defaults_to_splash_but_requires_an_eligible_card(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bulk_file = _write_build_bulk_file(directory=tmp_path)
    pool_file = tmp_path / "pool.json"
    pool_file.write_text(
        json.dumps({"set_code": "TST", "pool_grp_ids": [1, 2, 3, 4, 5]}),
        encoding="utf-8",
    )

    exit_code = main(
        argv=[
            "build",
            "--pool",
            str(pool_file),
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(tmp_path / "app"),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Splash: enabled; no eligible A- or better" in captured.out
    assert "Eligible spells for WU: 4" in captured.out
    assert captured.err == ""



def test_refresh_structure_targets_command_writes_cache(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_dir = tmp_path / "app"
    bulk_file = _write_build_bulk_file(directory=tmp_path)
    draft_data_file = _write_structure_draft_data_file(directory=tmp_path)

    exit_code = main(
        argv=[
            "refresh-structure-targets",
            "--set-code",
            "TST",
            "--draft-data-file",
            str(draft_data_file),
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(app_dir),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "pair structure targets" in captured.out
    assert seventeen_lands_structure_targets_cache_path(
        set_code="TST",
        event_format="QuickDraft",
        app_dir=app_dir,
    ).exists()
    assert captured.err == ""



def test_build_pair_flag_forces_requested_pair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bulk_file = _write_build_bulk_file(directory=tmp_path)
    pool_file = tmp_path / "pool.json"
    pool_file.write_text(
        json.dumps({"set_code": "TST", "pool_grp_ids": [1, 2, 3, 4, 5]}),
        encoding="utf-8",
    )

    exit_code = main(
        argv=[
            "build",
            "--pool",
            str(pool_file),
            "--pair",
            "BR",
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(tmp_path / "app"),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Chosen pair: BR (forced" in captured.out
    assert "Best automatic pair: WU" in captured.out
    assert captured.err == ""



def test_build_uses_persisted_pool_with_account_and_draft_filters(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_dir = tmp_path / "app"
    bulk_file = _write_build_bulk_file(directory=tmp_path)
    save_draft_state(
        state=_build_draft_state(account_id="acct", draft_id="draft"),
        app_dir=app_dir,
    )

    exit_code = main(
        argv=[
            "build",
            "--account",
            "acct",
            "--draft-id",
            "draft",
            "--bulk-file",
            str(bulk_file),
            "--app-dir",
            str(app_dir),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Pool: persisted acct/draft" in captured.out
    assert "Chosen pair: WU (automatic" in captured.out
    assert captured.err == ""



def test_build_rejects_invalid_pair(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(argv=["build", "--pair", "ZZ"])

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "invalid choice" in captured.err


def test_test_draft_parser_defaults_and_options() -> None:
    parser = build_parser()

    defaults = parser.parse_args(
        args=["test-draft", "--draftmancer-dir", "../Draftmancer"]
    )

    assert defaults.draftmancer_dir == Path("../Draftmancer")
    assert defaults.scryfall_bulk_file == cli.DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE
    assert defaults.scryfall_bulk_file == Path(
        ".draftomen/corpus-cache/sources/scryfall-default-cards.jsonl.gz"
    )
    assert defaults.server_url == "http://127.0.0.1:3000"
    assert defaults.set_code == "HOB"
    assert defaults.timeout == 10.0
    assert defaults.app_dir is None
    assert defaults.profile_manifest_url is None
    assert defaults.offline_profiles is False
    assert defaults.splash_enabled is True

    configured = parser.parse_args(
        args=[
            "test-draft",
            "--draftmancer-dir",
            "../Draftmancer",
            "--scryfall-bulk-file",
            "bulk.jsonl.gz",
            "--server-url",
            "http://127.0.0.1:9999",
            "--set-code",
            "dsk",
            "--timeout",
            "2.5",
            "--app-dir",
            "/tmp/app",
            "--profile-manifest-url",
            "https://profiles.example/manifest.json",
            "--offline-profiles",
            "--no-splash",
        ]
    )

    assert configured.scryfall_bulk_file == Path("bulk.jsonl.gz")
    assert configured.server_url == "http://127.0.0.1:9999"
    assert configured.set_code == "dsk"
    assert configured.timeout == 2.5
    assert configured.app_dir == Path("/tmp/app")
    assert configured.profile_manifest_url == "https://profiles.example/manifest.json"
    assert configured.offline_profiles is True
    assert configured.splash_enabled is False

    with pytest.raises(SystemExit) as error:
        parser.parse_args(args=["test-draft"])

    assert error.value.code == 2


def test_test_draft_prints_ordered_trace_and_build_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build_snapshot, expected_build = _test_draft_build_snapshot(directory=tmp_path)
    result = TestDraftRunResult(
        steps=(
            _test_draft_step(
                pack_number=0,
                pick_number=0,
                offered=((7, "First Pick"), (9, "Runner Up")),
                accepted_grp_id=7,
            ),
            _test_draft_step(
                pack_number=0,
                pick_number=2,
                offered=((11, "Second Rank"), (13, "Accepted Second Pick")),
                accepted_grp_id=13,
            ),
            _test_draft_step(
                pack_number=2,
                pick_number=0,
                offered=((17, "Deep Pick"),),
                accepted_grp_id=17,
            ),
        ),
        completed=LiveSessionSnapshot(),
        build=build_snapshot,
    )

    monkeypatch.setattr(cli, "run_test_draft_auto", lambda **kwargs: result)

    exit_code = main(
        argv=[
            "test-draft",
            "--draftmancer-dir",
            str(tmp_path / "Draftmancer"),
            "--scryfall-bulk-file",
            str(tmp_path / "bulk.jsonl.gz"),
            "--app-dir",
            str(tmp_path / "app"),
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == (
        "Pack 1 pick 1: First Pick (grpId 7)\n"
        "Pack 1 pick 3: Accepted Second Pick (grpId 13)\n"
        "Pack 3 pick 1: Deep Pick (grpId 17)\n"
        f"{expected_build}"
    )


@pytest.mark.parametrize("stage", ["startup", "drafting", "build"])
def test_test_draft_reports_failures_without_deck_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    def fail(**kwargs: object) -> TestDraftRunResult:
        raise TestDraftError(f"{stage} failed", stage=stage)

    monkeypatch.setattr(cli, "run_test_draft_auto", fail)

    exit_code = main(
        argv=["test-draft", "--draftmancer-dir", str(tmp_path / "Draftmancer")]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == f"test-draft failed: {stage} failed\n"


@pytest.mark.parametrize("timeout", ["0", "inf", "nan"])
def test_test_draft_rejects_invalid_timeout_without_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    timeout: str,
) -> None:
    def unexpected(**kwargs: object) -> TestDraftRunResult:
        raise AssertionError("invalid configuration must not run the draft")

    monkeypatch.setattr(cli, "run_test_draft_auto", unexpected)

    exit_code = main(
        argv=["test-draft", "--draftmancer-dir", "Draftmancer", "--timeout", timeout]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "test-draft failed: --timeout must be finite and positive.\n"


def test_config_exposes_documented_tunables() -> None:
    assert config.DECK_BUILDER.deck_size == 40
    assert config.DECK_BUILDER.target_spell_count == 23
    assert config.DECK_BUILDER.pair_score_card_weight == 0.85
    assert config.DECK_BUILDER.pair_score_win_rate_weight == 0.15
    assert config.DECK_BUILDER.default_land_count == 17
    assert config.DECK_BUILDER.aggressive_land_count == 16
    assert config.DECK_BUILDER.top_heavy_land_count == 18
    assert config.DECK_BUILDER.creature_floor == 14
    assert config.DECK_BUILDER.creature_ceiling == 17
    assert config.DECK_BUILDER.minimum_two_drops == 5
    assert config.DECK_BUILDER.maximum_expensive_spells == 3
    assert config.DECK_BUILDER.two_drop_mana_value == 2.0
    assert config.DECK_BUILDER.expensive_spell_mana_value == 6.0
    assert config.DECK_BUILDER.near_tie_creature_preference_points == 2.0
    assert config.DECK_BUILDER.splash_max_cards == 2
    assert config.DECK_BUILDER.splash_elite_score_minimum == 70.0
    assert config.DECK_BUILDER.maximum_unresolved_metadata_ratio == 0.25
    assert config.DECK_BUILDER.main_color_source_floor == 7
    assert config.DECK_BUILDER.structure_maindeck_rate_threshold == 0.5
    assert "minimum two-drop quota" in config.DECK_BUILDER.relaxation_order
    assert config.PICK_ENGINE.thin_sample_minimum == 500
    assert config.PICK_ENGINE.neutral_prior_win_rate == 0.55
    assert config.PICK_ENGINE.score_decimal_places == 0
    assert config.POLL_INTERVAL_SECONDS == 1.0
    assert "WU" in config.COLOR_PAIRS


def test_no_subcommand_defaults_to_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def fake_watch(args: object) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(cli, "handle_watch", fake_watch)

    exit_code = main(argv=[])

    assert exit_code == 0
    assert len(calls) == 1
    assert getattr(calls[0], "command") == "watch"
    assert getattr(calls[0], "startup_scan") is True


def test_watch_defaults_to_set_card_data_client_without_startup_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path | None, str, bool]] = []
    captured: dict[str, object] = {}

    class FakeCardDataClient:
        def __init__(self, *, app_dir: Path | None = None) -> None:
            calls.append((app_dir, "init", False))

        def load(self, set_code: str, *, allow_network: bool) -> CardDatabase:
            calls.append((None, set_code, allow_network))
            return CardDatabase(cards={})

    def fake_run_tui_watch(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "CardDataClient", FakeCardDataClient)
    monkeypatch.setattr(cli, "run_tui_watch", fake_run_tui_watch)
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")
    app_dir = tmp_path / "app"

    assert (
        main(
            argv=[
                "watch",
                "--log-path",
                str(log_path),
                "--app-dir",
                str(app_dir),
                "--once",
            ]
        )
        == 0
    )

    assert calls == [(app_dir, "init", False)]
    assert captured["card_database"] is None
    loader = captured["set_card_data_loader"]
    assert callable(loader)
    assert loader("TST", allow_network=True).cards == {}  # type: ignore[operator]
    assert calls[-1] == (None, "TST", True)


@pytest.mark.parametrize("plain", [False, True], ids=["tui", "plain"])
@pytest.mark.parametrize(
    ("profile_args", "expected_manifest_url", "network_expected"),
    [
        (
            (),
            cli.DEFAULT_PROFILE_MANIFEST_URL,
            True,
        ),
        (
            (
                "--profile-manifest-url",
                "https://profiles.example.test/m.json",
            ),
            "https://profiles.example.test/m.json",
            True,
        ),
        (
            (
                "--profile-manifest-url",
                "https://profiles.example.test/m.json",
                "--offline-profiles",
            ),
            "https://profiles.example.test/m.json",
            False,
        ),
    ],
    ids=["production-default", "manifest-override", "override-offline"],
)
def test_watch_uses_profile_source_through_each_terminal_mode(
    plain: bool,
    profile_args: tuple[str, ...],
    expected_manifest_url: str,
    network_expected: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json",
        expected_set_code="TST",
        expected_format=QUICK_DRAFT_FORMAT,
    )
    profile_bytes = profile.to_bytes()
    artifact_bytes = gzip.compress(profile_bytes, mtime=0)
    artifact_url = (
        expected_manifest_url.rsplit("/", maxsplit=1)[0]
        + "/tst-quickdraft.json.gz"
    )
    artifact = ProfileManifestArtifact(
        set_code=profile.set_code,
        event_format=profile.event_format,
        set_profile_schema_version=profile.schema_version,
        profile_version=profile.profile_version,
        generated_at=profile.generated_at,
        url=artifact_url,
        gzip_bytes=len(artifact_bytes),
        profile_bytes=len(profile_bytes),
        gzip_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
        maturity=profile.maturity,
    )
    responses = {
        expected_manifest_url: ProfileManifest(
            artifacts=(artifact,),
            published_at="2026-09-01T00:00:00+00:00",
        ).to_bytes(),
        artifact_url: artifact_bytes,
    }
    requested_urls: list[str] = []

    class Response:
        def __init__(self, payload: bytes, url: str) -> None:
            self._stream = BytesIO(payload)
            self.url = url

        def read(self, size: int = -1) -> bytes:
            return self._stream.read(size)

        def close(self) -> None:
            self._stream.close()

    def deterministic_opener(
        _client: object,
        request: Any,
        *,
        timeout: float,
    ) -> Response:
        del timeout
        url = request.full_url
        requested_urls.append(url)
        if url not in responses:
            raise AssertionError(f"unexpected profile request: {url}")
        return Response(responses[url], url)

    monkeypatch.setattr(cli.ProfileClient, "_default_opener", deterministic_opener)

    fixture_lines = tuple(
        line.replace("MSH", "TST")
        for line in QUICK_DRAFT_FIXTURE_PATH.read_text(encoding="utf-8").splitlines()[:7]
    )
    observations: list[object] = []

    def fake_plain_watch(**kwargs: object) -> int:
        watcher = PlainLogWatcher(
            log_path=kwargs["log_path"],
            card_database=kwargs["card_database"],
            set_card_data_loader=kwargs["set_card_data_loader"],
            app_dir=kwargs["app_dir"],
            profile_client=kwargs["profile_client"],
            poll_interval=0.01,
            splash_enabled=kwargs["splash_enabled"],
        )
        try:
            watcher.process_lines(lines=fixture_lines)
            for _ in range(300):
                if watcher.profile_refresh_in_flight is None:
                    break
                time.sleep(0.01)
            assert watcher.profile_refresh_in_flight is None
            observations.append(watcher.session.snapshot)
        finally:
            watcher.close()
        return 0

    async def drive_tui_watch(**kwargs: object) -> None:
        app = DraftomenTuiApp(
            log_path=kwargs["log_path"],
            card_database=kwargs["card_database"],
            set_card_data_loader=kwargs["set_card_data_loader"],
            app_dir=kwargs["app_dir"],
            profile_client=kwargs["profile_client"],
            poll_interval=0.01,
            poll_enabled=False,
            mana_icons_enabled=kwargs["mana_icons_enabled"],
            splash_enabled=kwargs["splash_enabled"],
        )
        async with app.run_test(size=(120, 24)) as pilot:
            app.process_lines(lines=fixture_lines)
            for _ in range(300):
                snapshot = app.session.snapshot
                if (
                    (
                        snapshot.set_profile.maturity == "mature"
                        and app.profile_refresh_in_flight is None
                    )
                    or (
                        not network_expected
                        and app.session.profile_refresh_request() is None
                        and app.profile_refresh_in_flight is None
                    )
                ):
                    break
                await pilot.pause(0.01)
            assert app.profile_refresh_in_flight is None
            observations.append(app.session.snapshot)

    runner_name = "run_plain_watch" if plain else "run_tui_watch"
    if plain:
        monkeypatch.setattr(cli, runner_name, fake_plain_watch)
    else:
        monkeypatch.setattr(
            cli,
            runner_name,
            lambda **kwargs: (asyncio.run(drive_tui_watch(**kwargs)) or 0),
        )

    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")
    app_dir = tmp_path / "app"
    args = [
        "watch",
        "--log-path",
        str(log_path),
        "--bulk-file",
        str(SCRYFALL_BULK_SAMPLE_PATH),
        "--app-dir",
        str(app_dir),
        "--once",
        *profile_args,
    ]
    if plain:
        args.insert(1, "--plain")
    assert main(argv=args) == 0
    snapshot, = observations
    assert (
        requested_urls == [expected_manifest_url, artifact_url]
        if network_expected
        else requested_urls == []
    )
    if network_expected:
        assert snapshot.set_profile.maturity == "mature"
        assert snapshot.current_scored_pack is not None
        assert snapshot.current_scored_pack.scoring_context is not None
    else:
        assert snapshot.set_profile.maturity == "generic"
        assert snapshot.set_profile.source == "generic"
        assert snapshot.current_scored_pack is not None
        assert snapshot.current_scored_pack.scoring_context is None


def test_watch_bulk_file_uses_direct_database_without_hosted_card_data_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = CardDatabase(cards={})
    captured: dict[str, object] = {}

    def fail_client(**kwargs: object) -> NoReturn:
        raise AssertionError("hosted card client must not be constructed for --bulk-file")

    def fake_build(*, path: Path) -> CardDatabase:
        assert path == SCRYFALL_BULK_SAMPLE_PATH
        return database

    def fake_run_plain_watch(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "CardDataClient", fail_client)
    monkeypatch.setattr(cli, "build_card_database_from_bulk_file", fake_build)
    monkeypatch.setattr(cli, "run_plain_watch", fake_run_plain_watch)
    log_path = tmp_path / "Player.log"
    log_path.write_text("", encoding="utf-8")

    assert (
        main(
            argv=[
                "watch",
                "--plain",
                "--log-path",
                str(log_path),
                "--bulk-file",
                str(SCRYFALL_BULK_SAMPLE_PATH),
            ]
        )
        == 0
    )

    assert captured["card_database"] is database
    assert captured["set_card_data_loader"] is None


def _write_build_bulk_file(*, directory: Path) -> Path:
    path = directory / "build-bulk.jsonl"
    rows = [
        _scryfall_row(grp_id=1, name="White Fixture", colors=["W"]),
        _scryfall_row(grp_id=2, name="Blue Fixture", colors=["U"]),
        _scryfall_row(grp_id=3, name="Second White Fixture", colors=["W"]),
        _scryfall_row(grp_id=4, name="Second Blue Fixture", colors=["U"]),
        _scryfall_row(grp_id=5, name="Red Fixture", colors=["R"]),
    ]
    path.write_text(
        "".join(f"{json.dumps(row)}\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _test_draft_recommendation(*, rank: int, grp_id: int, name: str) -> Recommendation:
    return Recommendation(
        rank=rank,
        card=CardView(
            grp_id=grp_id,
            name=name,
            colors=("W",),
            rarity="common",
            types=("Creature",),
            mana_cost="{2}",
            mana_value=2.0,
            image_path=None,
        ),
        score=100 - rank,
        win_rate=0.55,
        average_last_seen_at=6000.0,
        source_label="17Lands",
        color_fit="on-color",
        no_data=False,
    )


def _test_draft_step(
    *,
    pack_number: int,
    pick_number: int,
    offered: tuple[tuple[int, str], ...],
    accepted_grp_id: int,
) -> TestDraftStep:
    rows = tuple(
        _test_draft_recommendation(rank=rank, grp_id=grp_id, name=name)
        for rank, (grp_id, name) in enumerate(offered, start=1)
    )
    accepted = next(row for row in rows if row.card.grp_id == accepted_grp_id)
    return TestDraftStep(
        before=TestDraftInspection(
            offer=TestDraftOfferIdentity(
                account_id=FIXTURE_ACCOUNT_ID,
                event_name="QuickDraft",
                set_code="TST",
                pack_number=pack_number,
                pick_number=pick_number,
                offered_grp_ids=tuple(grp_id for grp_id, _ in offered),
                pool_grp_ids=(),
            ),
            snapshot=LiveSessionSnapshot(
                recommendations=RecommendationState(cards=rows)
            ),
        ),
        grp_id=accepted.card.grp_id,
        unique_card_id=accepted.card.grp_id + 1000,
        after=LiveSessionSnapshot(),
    )


def _test_draft_build_snapshot(*, directory: Path) -> tuple[LiveSessionSnapshot, str]:
    bulk_file = _write_build_bulk_file(directory=directory)
    database = build_card_database_from_bulk_file(path=bulk_file)
    pool = BuildPool(
        set_code="TST",
        pool_grp_ids=(1, 2, 3, 4, 5),
        source_label="test-draft pool",
    )
    selection, build_sheet = build_deck_from_pool(pool=pool, card_database=database)
    return (
        LiveSessionSnapshot(
            build=BuildResult(
                selected_pair=selection.chosen.pair,
                pair_options=(),
                spells=(),
                lands=(),
                bench=(),
                deck_size=build_sheet.mana_base.total_cards,
                domain_pool=pool,
                domain_selection=selection,
                domain_spell_selection=build_sheet.spell_selection,
                domain_mana_base=build_sheet.mana_base,
            )
        ),
        format_build_result(
            pool=pool,
            selection=selection,
            spell_selection=build_sheet.spell_selection,
            mana_base=build_sheet.mana_base,
        ),
    )


def _write_structure_draft_data_file(*, directory: Path) -> Path:
    path = directory / "draft-data.csv"
    rows = [
        "draft_id,expansion,event_type,event_match_wins,pick,pick_maindeck_rate",
    ]
    names = ["White Fixture", "Blue Fixture", "Second White Fixture", "Second Blue Fixture"]
    for index in range(23):
        rows.append(
            "draft,"
            "TST,"
            "QuickDraft,"
            "7,"
            f"{names[index % len(names)]},"
            "1.0"
        )

    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path



def _scryfall_row(*, grp_id: int, name: str, colors: list[str]) -> dict[str, object]:
    return {
        "arena_id": grp_id,
        "name": name,
        "colors": colors,
        "cmc": 2,
        "rarity": "common",
        "type_line": "Creature — Fixture",
    }



def _build_draft_state(*, account_id: str, draft_id: str) -> DraftState:
    now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC).isoformat()
    return DraftState(
        account_id=account_id,
        draft_id=draft_id,
        event_name="QuickDraft_TST_20260703",
        set_code="TST",
        course_id=draft_id,
        started_at=now,
        updated_at=now,
        completed_at=None,
        completed=False,
        picks=(),
        pool_grp_ids=(1, 2, 3, 4, 5),
    )


def _run_generate_profile_cli(
    *,
    stage: str,
    output_dir: Path,
    card_database_path: Path = PROFILE_GENERATION_FIXTURE_DIR / "card-database.json",
    ratings_path: Path | None = None,
    source_manifest_path: Path | None = None,
    enrichment_path: Path | None = None,
    generated_at: str = PROFILE_GENERATION_AT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "draftomen.cli",
        "generate-profile",
        "--set-code",
        "TST",
        "--format",
        "quickdraft",
        "--stage",
        stage,
        "--generated-at",
        generated_at,
        "--card-database-file",
        str(card_database_path),
        "--output-dir",
        str(output_dir),
    ]
    if ratings_path is not None:
        command.extend(["--ratings-file", str(ratings_path)])
    if source_manifest_path is not None:
        command.extend(["--source-manifest", str(source_manifest_path)])
    if enrichment_path is not None:
        command.extend(["--enrichment", str(enrichment_path)])
    return subprocess.run(
        command,
        cwd=CLI_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )


def _assert_profile_cli_success(
    *,
    completed: subprocess.CompletedProcess[str],
    expected_input_count: int,
    expected_stage: str,
) -> tuple[Path, Path, bytes, bytes]:
    assert completed.returncode == 0
    assert completed.stderr == ""

    report_lines = completed.stdout.splitlines()
    assert len(report_lines) == 8
    reported = dict(line.split("=", maxsplit=1) for line in report_lines)
    assert set(reported) == {
        "maturity",
        "input_count",
        "sample_count",
        "skip_count",
        "error_count",
        "validation",
        "artifact",
        "generation_manifest",
    }

    artifact_path = Path(reported["artifact"])
    manifest_path = Path(reported["generation_manifest"])
    artifact_bytes = artifact_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    report = json.loads(manifest_bytes)
    profile_bytes = gzip.decompress(artifact_bytes)
    profile = SetProfile.from_json(json.loads(profile_bytes))

    assert profile.set_code == "tst"
    assert profile.event_format == "quickdraft"
    assert profile.maturity.value == reported["maturity"]
    assert report["stage"] == expected_stage
    assert report["set_code"] == "tst"
    assert report["event_format"] == "quickdraft"
    assert report["profile_sha256"] == hashlib.sha256(profile_bytes).hexdigest()
    assert report["gzip_sha256"] == hashlib.sha256(artifact_bytes).hexdigest()
    assert artifact_path.name == f"{report['gzip_sha256']}.json.gz"
    assert manifest_bytes == (
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    expected_reported = {
        "maturity": profile.maturity.value,
        "input_count": str(expected_input_count),
        "sample_count": str(report["samples"]["total"]),
        "skip_count": str(sum(report["skip_reasons"].values())),
        "error_count": str(sum(report["error_reasons"].values())),
        "validation": "passed",
        "artifact": str(artifact_path),
        "generation_manifest": str(manifest_path),
    }
    assert reported == expected_reported
    assert completed.stdout == "".join(
        f"{key}={expected_reported[key]}\n"
        for key in (
            "maturity",
            "input_count",
            "sample_count",
            "skip_count",
            "error_count",
            "validation",
            "artifact",
            "generation_manifest",
        )
    )
    assert not any(
        secret in completed.stdout
        for secret in ("alpha", "beta", "Support Creature", "Removal Spell", "FIXTURECLIENTID")
    )
    return artifact_path, manifest_path, artifact_bytes, manifest_bytes


@pytest.mark.parametrize(
    ("stage", "expected_input_count", "source_manifest_name", "with_ratings"),
    [
        ("metadata", 2, "manifest-no-data.json", False),
        ("early", 3, "manifest-early-data.json", True),
        ("mature", 3, "manifest-mature-data.json", True),
    ],
)
def test_generate_profile_cli_processes_all_stages_deterministically(
    stage: str,
    expected_input_count: int,
    source_manifest_name: str,
    with_ratings: bool,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "published"
    source_manifest_path = PROFILE_GENERATION_FIXTURE_DIR / source_manifest_name
    ratings_path = (
        PROFILE_GENERATION_FIXTURE_DIR / "ratings.json" if with_ratings else None
    )

    first = _run_generate_profile_cli(
        stage=stage,
        output_dir=output_dir,
        ratings_path=ratings_path,
        source_manifest_path=source_manifest_path,
    )
    first_artifact, first_manifest, first_artifact_bytes, first_manifest_bytes = (
        _assert_profile_cli_success(
            completed=first,
            expected_input_count=expected_input_count,
            expected_stage=stage,
        )
    )

    second = _run_generate_profile_cli(
        stage=stage,
        output_dir=output_dir,
        ratings_path=ratings_path,
        source_manifest_path=source_manifest_path,
    )
    second_artifact, second_manifest, second_artifact_bytes, second_manifest_bytes = (
        _assert_profile_cli_success(
            completed=second,
            expected_input_count=expected_input_count,
            expected_stage=stage,
        )
    )

    assert second.stdout == first.stdout
    assert second_artifact == first_artifact
    assert second_manifest == first_manifest
    assert second_artifact_bytes == first_artifact_bytes
    assert second_manifest_bytes == first_manifest_bytes
    assert hashlib.sha256(second_artifact_bytes).hexdigest() == Path(
        second_artifact
    ).name.removesuffix(".json.gz")


def test_generate_profile_cli_failure_preserves_last_valid_publication(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "published"
    first = _run_generate_profile_cli(
        stage="early",
        output_dir=output_dir,
        ratings_path=PROFILE_GENERATION_FIXTURE_DIR / "ratings.json",
        source_manifest_path=PROFILE_GENERATION_FIXTURE_DIR / "manifest-early-data.json",
    )
    artifact_path, manifest_path, artifact_bytes, manifest_bytes = (
        _assert_profile_cli_success(
            completed=first,
            expected_input_count=3,
            expected_stage="early",
        )
    )

    failed = _run_generate_profile_cli(
        stage="early",
        output_dir=output_dir,
        card_database_path=tmp_path / "missing-card-database.json",
        ratings_path=PROFILE_GENERATION_FIXTURE_DIR / "ratings.json",
        source_manifest_path=PROFILE_GENERATION_FIXTURE_DIR / "manifest-early-data.json",
    )
    assert failed.returncode == 1
    assert failed.stdout == ""
    assert failed.stderr == (
        "generate-profile: Could not load the card database input.\n"
    )
    assert "Support Creature" not in failed.stderr
    assert "alpha" not in failed.stderr
    assert artifact_path.read_bytes() == artifact_bytes
    assert manifest_path.read_bytes() == manifest_bytes


def test_generate_profile_cli_uses_a_saved_enrichment_artifact_without_network_access(
    tmp_path: Path,
) -> None:
    database = _typed_database()
    guide = GuideSource(
        guide_id="tst-draftsim-guide",
        url="https://draftsim.com/tst-limited-set-review/",
        text="TST rewards going wide with support creatures.",
        retrieved_at="2026-09-01T12:00:00Z",
    )
    sources = EnrichmentSources(
        set_code="TST",
        cards=tuple(database.cards.values()),
        guides=(guide,),
    )
    relationship = replace(
        _typed_relationship(),
        guide_evidence=(
            GuideEvidence(guide_id=guide.guide_id, quote="rewards going wide"),
        ),
    )
    artifact = SemanticEnrichmentArtifact(
        set_code="TST",
        set_source_id="tst-card-data-v1",
        set_source_sha256=set_source_sha256(sources),
        created_at="2026-09-01T12:02:00Z",
        cards=tuple(
            CardSourcePin(
                card_id=card.grp_id,
                oracle_id=card.oracle_id,
                collector_number=card.collector_number,
                sha256=card_source_sha256(card),
            )
            for card in sources.cards
        ),
        guides=(
            GuideSourcePin(
                guide_id=guide.guide_id,
                url=guide.url,
                sha256=guide.text_sha256,
                retrieved_at=guide.retrieved_at,
            ),
        ),
        runs=(_enrichment_run(),),
        oracle_facts=(),
        guide_claims=(),
        relationships=(relationship,),
        rejected_findings=(),
        review=ArtifactReview(
            state="confirmed",
            reviewer_id="local-review",
            reviewed_at="2026-09-01T14:00:00Z",
        ),
        confirmed_relationship_ids=(relationship.finding_id,),
        sources=sources,
    )

    artifact_bytes = artifact.to_bytes()
    artifact_path = (
        tmp_path
        / "run"
        / "artifacts"
        / f"{hashlib.sha256(artifact_bytes).hexdigest()}.json"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    guide_path = tmp_path / "run" / "sources" / "guide.json"
    guide_path.parent.mkdir(parents=True)
    guide_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requested_url": "https://draftsim.com/tst-limited-set-review",
                "guide_id": guide.guide_id,
                "url": guide.url,
                "text": guide.text,
                "sha256": guide.text_sha256,
                "retrieved_at": guide.retrieved_at,
            }
        ),
        encoding="utf-8",
    )
    card_database_path = tmp_path / "cards.json"
    card_database_path.write_text(json.dumps(database.to_json()), encoding="utf-8")

    netblock = tmp_path / "netblock"
    netblock.mkdir()
    (netblock / "sitecustomize.py").write_text(
        "import socket\n"
        "\n"
        "\n"
        "def _fail(*args, **kwargs):\n"
        '    raise RuntimeError("network access is forbidden in this test")\n'
        "\n"
        "\n"
        "socket.socket.connect = _fail\n"
        "socket.socket.connect_ex = _fail\n"
        "socket.create_connection = _fail\n",
        encoding="utf-8",
    )

    completed = _run_generate_profile_cli(
        stage="early",
        output_dir=tmp_path / "published",
        card_database_path=card_database_path,
        ratings_path=PROFILE_GENERATION_FIXTURE_DIR / "ratings.json",
        enrichment_path=artifact_path,
        env={**os.environ, "PYTHONPATH": str(netblock)},
    )
    _artifact_path, _manifest_path, published_bytes, _manifest_bytes = (
        _assert_profile_cli_success(
            completed=completed,
            expected_input_count=3,
            expected_stage="early",
        )
    )

    profile = SetProfile.from_json(json.loads(gzip.decompress(published_bytes)))
    assert profile.schema_version == 3
    assert profile.role_profile is not None
    assert profile.enhancement is not None
    assert (
        profile.enhancement.artifact_sha256
        == hashlib.sha256(artifact.to_bytes()).hexdigest()
    )
    for card_id, role in (
        (TYPED_SOURCE_CARD_ID, Role.TOKEN_MAKER),
        (TYPED_TARGET_CARD_ID, Role.GO_WIDE_PAYOFF),
    ):
        resolved = resolve_card_roles(
            database.cards[card_id],
            profile=profile.role_profile,
        )
        assert resolved.source == "compiled_profile"
        assert role in {assignment.role for assignment in resolved.assignments}


@pytest.mark.parametrize(
    ("generated_at", "expected_error"),
    [
        (
            "2026-08-30T12:00:00",
            "--generated-at must include a timezone offset.",
        ),
        (
            "0001-01-01T00:00:00+14:00",
            "--generated-at must be representable after UTC normalization.",
        ),
        (
            "not-an-iso-timestamp",
            "--generated-at must be a valid ISO-8601 timestamp.",
        ),
    ],
)
def test_generate_profile_cli_rejects_invalid_timestamp_as_argparse_error(
    generated_at: str,
    expected_error: str,
) -> None:
    completed = _run_generate_profile_cli(
        stage="metadata",
        output_dir=Path("/tmp/draftomen-profile-cli-invalid"),
        generated_at=generated_at,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert (
        "draftomen-tui generate-profile: error: argument --generated-at: "
        f"{expected_error}"
    ) in completed.stderr.splitlines()
    assert expected_error in completed.stderr
    assert not completed.stderr.startswith("generate-profile:")


def test_plan_profile_refresh_cli_dry_run_prints_canonical_plan_without_profiles(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "draftomen.cli",
            "plan-profile-refresh",
            "--set-code",
            "new",
            "--event-format",
            "PremierDraft",
            "--inventory-file",
            str(REFRESH_PLAN_FIXTURE_DIR / "expansions.json"),
            "--lifecycle-file",
            str(REFRESH_PLAN_FIXTURE_DIR / "lifecycle.json"),
            "--dry-run",
        ],
        cwd=CLI_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    parsed = json.loads(completed.stdout)
    assert parsed["selection"] == {"mode": "manual", "set_code": "NEW"}
    assert parsed["event_format"] == "premierdraft"
    assert parsed["environments"] == [
        {
            "event_format": "premierdraft",
            "lifecycle": "active",
            "reasons": ["manual"],
            "set_code": "NEW",
        }
    ]
    assert completed.stdout.endswith("\n")
    assert parsed["inventory"]["source_url"] == "https://www.17lands.com/data/expansions"
    assert str(REFRESH_PLAN_FIXTURE_DIR) not in completed.stdout
    assert "file://" not in completed.stdout
    assert parsed["lifecycle"]["source_url"] == "https://schedule.example.test/arena.json"
    assert (
        "inventory:duplicate-entry:entry=NEW:normalized expansion code already present"
        in parsed["diagnostics"]
    )
    assert "inventory:malformed-entry:entry=:expected a non-empty string" in parsed["diagnostics"]
    assert not list(tmp_path.glob("**/*profile*"))


def test_plan_profile_refresh_cli_output_plan_writes_without_printing(
    tmp_path: Path,
) -> None:
    output_plan = tmp_path / "refresh-plan.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "draftomen.cli",
            "plan-profile-refresh",
            "--set-code",
            "new",
            "--event-format",
            "PremierDraft",
            "--inventory-file",
            str(REFRESH_PLAN_FIXTURE_DIR / "expansions.json"),
            "--lifecycle-file",
            str(REFRESH_PLAN_FIXTURE_DIR / "lifecycle.json"),
            "--output-plan",
            str(output_plan),
        ],
        cwd=CLI_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert json.loads(output_plan.read_text(encoding="utf-8"))["selection"] == {
        "mode": "manual",
        "set_code": "NEW",
    }
    assert output_plan.read_text(encoding="utf-8").endswith("\n")
    assert not list(tmp_path.glob("**/*profile*"))


def test_plan_profile_refresh_cli_requires_exactly_one_output_mode() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "plan-profile-refresh",
                "--set-code",
                "TST",
                "--event-format",
                "QuickDraft",
            ]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "plan-profile-refresh",
                "--set-code",
                "TST",
                "--event-format",
                "QuickDraft",
                "--dry-run",
                "--output-plan",
                "plan.json",
            ]
        )


def test_plan_profile_refresh_cli_rejects_empty_automatic_selection(tmp_path: Path) -> None:
    lifecycle_file = tmp_path / "lifecycle.json"
    lifecycle_file.write_text(
        json.dumps(
            {
                "provider": "Arena schedule",
                "source_url": "https://schedule.example.test/arena.json",
                "version": "2026-08-30",
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "draftomen.cli",
            "plan-profile-refresh",
            "--active",
            "--event-format",
            "QuickDraft",
            "--inventory-file",
            str(REFRESH_PLAN_FIXTURE_DIR / "expansions.json"),
            "--lifecycle-file",
            str(lifecycle_file),
            "--dry-run",
        ],
        cwd=CLI_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "active selection matched no environments" in completed.stderr



def test_plan_profile_refresh_cli_rejects_unknown_manual_selection() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "draftomen.cli",
            "plan-profile-refresh",
            "--set-code",
            "NOT-IN-17LANDS",
            "--event-format",
            "QuickDraft",
            "--inventory-file",
            str(REFRESH_PLAN_FIXTURE_DIR / "expansions.json"),
            "--lifecycle-file",
            str(REFRESH_PLAN_FIXTURE_DIR / "lifecycle.json"),
            "--dry-run",
        ],
        cwd=CLI_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "NOT-IN-17LANDS" in completed.stderr
    assert "not present in the 17Lands inventory" in completed.stderr


def test_plan_profile_refresh_cli_requires_one_selection_and_event_format() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["plan-profile-refresh", "--event-format", "QuickDraft", "--dry-run"])
    with pytest.raises(SystemExit):
        parser.parse_args(["plan-profile-refresh", "--active", "--dry-run"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["plan-profile-refresh", "--active", "--event-format", "QuickDraft", "--set-code", "TST", "--dry-run"]
        )



@pytest.mark.parametrize("lifecycle_url", ["", "not-a-url"])
def test_plan_profile_refresh_cli_rejects_malformed_lifecycle_url_without_traceback(
    lifecycle_url: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "draftomen.cli",
            "plan-profile-refresh",
            "--set-code",
            "NEW",
            "--event-format",
            "PremierDraft",
            "--inventory-file",
            str(REFRESH_PLAN_FIXTURE_DIR / "expansions.json"),
            "--lifecycle-url",
            lifecycle_url,
            "--dry-run",
        ],
        cwd=CLI_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "Traceback" not in completed.stderr
    assert completed.stderr.strip() == (
        "plan-profile-refresh: lifecycle URL must be an absolute URL"
    )


def _cli_refresh_plan() -> RefreshPlan:
    environments = (
        PlannedEnvironment(
            set_code="META",
            event_format="quickdraft",
            lifecycle="historical",
            reasons=("fixture-metadata-only",),
        ),
        PlannedEnvironment(
            set_code="EARLY",
            event_format="quickdraft",
            lifecycle="active",
            reasons=("fixture-early",),
        ),
        PlannedEnvironment(
            set_code="MATURE",
            event_format="quickdraft",
            lifecycle="mature",
            reasons=("fixture-mature",),
        ),
    )
    return RefreshPlan(
        selection_mode="history",
        max_environments=len(environments),
        event_format="quickdraft",
        environments=environments,
        inventory_source_url="https://inventory.example.test/expansions.json",
        inventory_payload_digest="a" * 64,
        lifecycle=LifecycleMetadata(
            provider="fixture",
            source_url="https://lifecycle.example.test/sets.json",
            version="fixture-v1",
        ),
    )


def _seed_cli_cache(
    *,
    cache: ProfileInputCache,
    environment: PlannedEnvironment,
    temporary_dir: Path,
    include_ratings: bool,
    include_public_drafts: bool,
) -> None:
    fixture_dir = PROFILE_GENERATION_FIXTURE_DIR
    source_database = CardDatabase.from_json(
        json.loads((fixture_dir / "card-database.json").read_text(encoding="utf-8"))
    )

    def fetch_database(*, set_code: str, timeout_seconds: int) -> CardDatabase:
        del timeout_seconds
        return replace(
            source_database,
            cards={
                grp_id: replace(card, set_code=set_code)
                for grp_id, card in source_database.cards.items()
            },
            generated_at=CLI_REFRESH_TIMESTAMP,
        )

    card_adapter = CardMetadataAdapter(fetch_database=fetch_database)
    card_snapshot = card_adapter.acquire(
        environment=environment,
        acquired_at=CLI_REFRESH_TIMESTAMP,
    )
    cache.store(
        source=card_adapter.source_for(environment=environment),
        source_version=card_snapshot.source_version,
        input_stream=BytesIO(card_snapshot.payload),
        expected_sha256=card_snapshot.sha256,
        acquired_at=card_snapshot.acquired_at,
    )

    if include_ratings:
        ratings_template = load_17lands_format_data(
            set_code="TST",
            event_format="quickdraft",
            cache_path=fixture_dir / "ratings.json",
        )

        def fetch_ratings(
            *,
            set_code: str,
            event_format: str,
            fetched_at: datetime,
            timeout_seconds: int,
        ):
            del timeout_seconds
            return replace(
                ratings_template,
                set_code=set_code,
                event_format=event_format,
                fetched_at=fetched_at,
            )

        ratings_adapter = SeventeenLandsRatingsAdapter(fetch_ratings=fetch_ratings)
        ratings_snapshot = ratings_adapter.acquire(
            environment=environment,
            acquired_at=CLI_REFRESH_TIMESTAMP,
        )
        cache.store(
            source=ratings_adapter.source_for(environment=environment),
            source_version=ratings_snapshot.source_version,
            input_stream=BytesIO(ratings_snapshot.payload),
            expected_sha256=ratings_snapshot.sha256,
            acquired_at=ratings_snapshot.acquired_at,
        )

    if include_public_drafts:
        draft_fixture = fixture_dir / "mature-data.csv"

        def fetch_public_drafts(
            *,
            set_code: str,
            event_format: str,
            path: Path,
            timeout_seconds: int,
        ) -> None:
            del event_format, timeout_seconds
            path.write_bytes(draft_fixture.read_bytes().replace(b"TST", set_code.encode()))

        public_draft_adapter = SeventeenLandsPublicDraftAdapter(
            fetch_public_drafts=fetch_public_drafts
        )
        draft_path = temporary_dir / f"{environment.set_code}-public-drafts.csv"
        draft_snapshot = public_draft_adapter.acquire(
            environment=environment,
            acquired_at=CLI_REFRESH_TIMESTAMP,
            path=draft_path,
        )
        with draft_path.open("rb") as input_stream:
            cache.store(
                source=public_draft_adapter.source_for(environment=environment),
                source_version=draft_snapshot.source_version,
                input_stream=input_stream,
                expected_sha256=draft_snapshot.sha256,
                acquired_at=draft_snapshot.acquired_at,
            )


def _run_execute_profile_refresh_cli(
    *,
    plan_path: Path,
    cache_dir: Path,
    output_dir: Path,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "draftomen.cli",
            "execute-profile-refresh",
            "--plan",
            str(plan_path),
            "--cache-dir",
            str(cache_dir),
            "--output-dir",
            str(output_dir),
            "--offline",
        ],
        cwd=CLI_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
    )


def _run_generate_profile_refresh_batch_cli(
    *,
    plan_path: Path,
    staged_dir: Path,
    generated_at: datetime = CLI_REFRESH_TIMESTAMP,
    profile_version: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        sys.executable,
        "-m",
        "draftomen.cli",
        "generate-profile-refresh-batch",
        "--plan",
        str(plan_path),
        "--staged-dir",
        str(staged_dir),
        "--generated-at",
        generated_at.isoformat(),
    ]
    if profile_version is not None:
        command.extend(["--profile-version", profile_version])
    return subprocess.run(
        command,
        cwd=CLI_REPOSITORY_ROOT,
        capture_output=True,
        check=False,
    )


def test_execute_profile_refresh_cli_offline_is_byte_stable_and_path_free(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "private-refresh-plan.json"
    cache_dir = tmp_path / "private-cache"
    output_dir = tmp_path / "private-output"
    plan = _cli_refresh_plan()
    write_refresh_plan(plan_path, plan)

    cache = ProfileInputCache(
        cache_dir,
        policy=cli.DEFAULT_PROFILE_REFRESH_CACHE_POLICY,
        clock=lambda: CLI_REFRESH_TIMESTAMP,
    )
    for environment in plan.environments:
        _seed_cli_cache(
            cache=cache,
            environment=environment,
            temporary_dir=tmp_path,
            include_ratings=environment.set_code == "MATURE"
            or environment.set_code == "EARLY",
            include_public_drafts=environment.set_code == "MATURE",
        )

    first = _run_execute_profile_refresh_cli(
        plan_path=plan_path,
        cache_dir=cache_dir,
        output_dir=output_dir,
    )
    assert first.returncode == 0
    assert first.stderr == b""
    execution_bytes = (output_dir / "execution.json").read_bytes()
    assert first.stdout == execution_bytes

    execution = json.loads(execution_bytes)
    assert execution["counts"] == {
        "failed": 0,
        "metadata_only": 1,
        "planned": 3,
        "staged": 3,
    }
    assert [item["environment"]["set_code"] for item in execution["environments"]] == [
        "EARLY",
        "MATURE",
        "META",
    ]
    stages = {"META": "metadata", "EARLY": "early", "MATURE": "mature"}
    generator_bytes: dict[str, bytes] = {}
    for item in execution["environments"]:
        bundle_dir = output_dir / "bundles" / item["bundle_id"]
        authority_path = bundle_dir / "bundle.json"
        assert authority_path.is_file()
        authority_bytes = authority_path.read_bytes()
        authority = json.loads(authority_bytes)
        assert authority["outcome"] == "staged"
        loaded = load_staged_profile_build_bundle(bundle_dir)
        stage = stages[item["environment"]["set_code"]]
        generated = generate_set_profile(
            **loaded.generator_inputs(),
            stage=stage,
            generated_at=CLI_REFRESH_TIMESTAMP,
        )
        repeated = generate_set_profile(
            **loaded.generator_inputs(),
            stage=stage,
            generated_at=CLI_REFRESH_TIMESTAMP,
        )
        assert generated.to_bytes() == repeated.to_bytes()
        generator_bytes[item["environment"]["set_code"]] = generated.to_bytes()
        if stage == "metadata":
            assert authority["inputs"]["ratings"] is None
            assert authority["inputs"]["public_drafts"] is None
            assert "17lands-ratings-cache-missing" in authority["skip_reasons"]
            assert "17lands-public-drafts-cache-missing" in authority["skip_reasons"]
        elif stage == "early":
            assert authority["inputs"]["ratings"] is not None
            assert authority["inputs"]["public_drafts"] is None
            assert "17lands-public-drafts-cache-missing" in authority["skip_reasons"]
        else:
            assert authority["inputs"]["ratings"] is not None
            assert authority["inputs"]["public_drafts"] is not None

        for payload in (first.stdout, first.stderr, authority_bytes, execution_bytes):
            assert str(plan_path).encode() not in payload
            assert str(cache_dir).encode() not in payload
            assert str(output_dir).encode() not in payload
            assert b"Support Creature" not in payload
            assert b"alpha" not in payload
            assert b"https://" not in payload

    second = _run_execute_profile_refresh_cli(
        plan_path=plan_path,
        cache_dir=cache_dir,
        output_dir=output_dir,
    )
    assert second.returncode == 0
    assert second.stderr == b""
    assert second.stdout == execution_bytes
    assert generator_bytes
    second_execution = json.loads(second.stdout)
    for item in second_execution["environments"]:
        set_code = item["environment"]["set_code"]
        loaded = load_staged_profile_build_bundle(
            output_dir / "bundles" / item["bundle_id"]
        )
        generated = generate_set_profile(
            **loaded.generator_inputs(),
            stage=stages[set_code],
            generated_at=CLI_REFRESH_TIMESTAMP,
        )
        assert generated.to_bytes() == generator_bytes[set_code]


def test_execute_profile_refresh_cli_failed_required_source_preserves_results(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "private-refresh-plan.json"
    output_dir = tmp_path / "private-output"
    write_refresh_plan(plan_path, _cli_refresh_plan())

    completed = _run_execute_profile_refresh_cli(
        plan_path=plan_path,
        cache_dir=tmp_path / "empty-cache",
        output_dir=output_dir,
    )

    assert completed.returncode == 1
    assert completed.stderr == b""
    execution_bytes = (output_dir / "execution.json").read_bytes()
    assert completed.stdout == execution_bytes
    execution = json.loads(execution_bytes)
    assert execution["counts"] == {
        "failed": 3,
        "metadata_only": 0,
        "planned": 3,
        "staged": 0,
    }
    assert len(execution["environments"]) == 3
    assert all(item["outcome"] == "failed" for item in execution["environments"])
    assert str(tmp_path).encode() not in completed.stdout
    assert b"Traceback" not in completed.stderr


@pytest.mark.parametrize("selection_mode", ["manual", "active", "history"])
def test_generate_profile_refresh_batch_cli_generates_each_selection_mode(
    tmp_path: Path,
    selection_mode: str,
) -> None:
    base_plan = _cli_refresh_plan()
    by_code = {item.set_code: item for item in base_plan.environments}
    if selection_mode == "manual":
        plan = replace(
            base_plan,
            selection_mode="manual",
            selection_set_code="EARLY",
            max_environments=None,
            environments=(by_code["EARLY"],),
        )
    elif selection_mode == "active":
        plan = replace(
            base_plan,
            selection_mode="active",
            selection_set_code=None,
            max_environments=None,
            environments=(by_code["EARLY"],),
        )
    else:
        plan = replace(
            base_plan,
            selection_mode="history",
            selection_set_code=None,
            max_environments=2,
            environments=(by_code["META"], by_code["MATURE"]),
        )

    plan_path = tmp_path / f"{selection_mode}-plan.json"
    cache_dir = tmp_path / f"{selection_mode}-cache"
    staged_dir = tmp_path / f"{selection_mode}-staged"
    write_refresh_plan(plan_path, plan)
    cache = ProfileInputCache(
        cache_dir,
        policy=cli.DEFAULT_PROFILE_REFRESH_CACHE_POLICY,
        clock=lambda: CLI_REFRESH_TIMESTAMP,
    )
    for environment in plan.environments:
        _seed_cli_cache(
            cache=cache,
            environment=environment,
            temporary_dir=tmp_path,
            include_ratings=environment.lifecycle in {"active", "mature"},
            include_public_drafts=environment.lifecycle == "mature",
        )

    staged = _run_execute_profile_refresh_cli(
        plan_path=plan_path,
        cache_dir=cache_dir,
        output_dir=staged_dir,
    )
    assert staged.returncode == 0
    assert staged.stderr == b""

    generated = _run_generate_profile_refresh_batch_cli(
        plan_path=plan_path,
        staged_dir=staged_dir,
    )
    assert generated.returncode == 0
    assert generated.stderr == b""
    report = json.loads(generated.stdout)
    assert report["selection_mode"] == selection_mode
    assert report["counts"] == {
        "failed": 0,
        "planned": len(plan.environments),
        "publication_eligible": len(plan.environments),
    }
    assert [item["outcome"] for item in report["environments"]] == [
        "publication-eligible"
    ] * len(plan.environments)


def test_generate_profile_refresh_batch_cli_is_deterministic_and_preserves_partial_results(
    tmp_path: Path,
) -> None:
    base_plan = _cli_refresh_plan()
    failed = PlannedEnvironment(
        set_code="FAIL",
        event_format="quickdraft",
        lifecycle="active",
        reasons=("fixture-failure",),
    )
    plan = replace(
        base_plan,
        max_environments=4,
        environments=(*base_plan.environments, failed),
    )
    plan_path = tmp_path / "private-refresh-plan.json"
    cache_dir = tmp_path / "private-cache"
    staged_dir = tmp_path / "private-staged"
    write_refresh_plan(plan_path, plan)
    cache = ProfileInputCache(
        cache_dir,
        policy=cli.DEFAULT_PROFILE_REFRESH_CACHE_POLICY,
        clock=lambda: CLI_REFRESH_TIMESTAMP,
    )
    for environment in plan.environments:
        if environment.set_code == "FAIL":
            continue
        _seed_cli_cache(
            cache=cache,
            environment=environment,
            temporary_dir=tmp_path,
            include_ratings=environment.lifecycle in {"active", "mature"},
            include_public_drafts=environment.lifecycle == "mature",
        )

    staged = _run_execute_profile_refresh_cli(
        plan_path=plan_path,
        cache_dir=cache_dir,
        output_dir=staged_dir,
    )
    assert staged.returncode == 1
    assert staged.stderr == b""

    first = _run_generate_profile_refresh_batch_cli(
        plan_path=plan_path,
        staged_dir=staged_dir,
    )
    second = _run_generate_profile_refresh_batch_cli(
        plan_path=plan_path,
        staged_dir=staged_dir,
    )

    assert first.returncode == 1
    assert second.returncode == 1
    assert first.stderr == b""
    assert second.stderr == b""
    assert first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert report["counts"] == {"failed": 1, "planned": 4, "publication_eligible": 3}
    outcomes = {
        item["environment"]["set_code"]: item["outcome"]
        for item in report["environments"]
    }
    assert outcomes == {
        "EARLY": "publication-eligible",
        "FAIL": "failed",
        "MATURE": "publication-eligible",
        "META": "publication-eligible",
    }
    for payload in (first.stdout, first.stderr, second.stdout, second.stderr):
        assert str(plan_path).encode() not in payload
        assert str(cache_dir).encode() not in payload
        assert str(staged_dir).encode() not in payload
        assert b"Support Creature" not in payload
        assert b"fixture-secret" not in payload
        assert b"https://" not in payload


def _enrichment_accounting(**overrides: object) -> EnrichmentAccounting:
    defaults: dict[str, object] = {
        "executed_work": 3,
        "reused_work": 0,
        "work_without_cost": 0,
        "input_tokens": 1200,
        "cached_input_tokens": 300,
        "output_tokens": 200,
        "reasoning_tokens": 40,
        "running_cost_usd": "0.006",
        "projected_final_cost_usd": "0.012",
    }
    defaults.update(overrides)
    return EnrichmentAccounting(**defaults)


def _enrichment_progress(
    phase: EnrichmentPhase,
    *,
    accounting: EnrichmentAccounting | None = None,
    **counters: int,
) -> EnrichmentProgress:
    defaults = {
        "guides_completed": 0,
        "guides_total": 1,
        "cards_completed": 0,
        "cards_total": 296,
        "relationships_completed": 0,
        "relationships_total": 4,
        "valid_count": 0,
        "uncertain_count": 0,
        "rejected_count": 0,
    }
    defaults.update(counters)
    return EnrichmentProgress(
        phase=phase,
        accounting=_enrichment_accounting() if accounting is None else accounting,
        **defaults,
    )


def _enrichment_review_analysis(tmp_path: Path) -> SimpleNamespace:
    """Build one complete pending analysis covering every review report category.
    Claim text stays name-neutral so resolved card names appear exactly once.
    """

    same_reason = "Needs more context."
    cards = tuple(
        SimpleNamespace(grp_id=card_id, name=name)
        for card_id, name in (
            (1, "Alpha"),
            (2, "Beta"),
            (3, "Gamma"),
            (4, "Delta"),
            (5, "Epsilon"),
            (6, "Zeta"),
        )
    )

    def review(status: FindingStatus, reason: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(status=status, reason=reason)

    guide_claims = (
        SimpleNamespace(
            category="mechanic",
            name="Token creation",
            claim="Creates creature tokens from a spell.",
            card_ids=(),
            review=review(FindingStatus.ACCEPTED),
        ),
        SimpleNamespace(
            category="format_finding",
            name="Limited",
            claim="The format rewards efficient early plays.",
            card_ids=(),
            review=review(FindingStatus.ACCEPTED),
        ),
        SimpleNamespace(
            category="archetype",
            name="Tokens",
            claim="Token decks can go wide.",
            card_ids=(),
            review=review(FindingStatus.UNCERTAIN, same_reason),
        ),
        SimpleNamespace(
            category="strategy",
            name="Go wide",
            claim="Build a broad battlefield.",
            card_ids=(),
            review=review(FindingStatus.ACCEPTED),
        ),
        SimpleNamespace(
            category="strategy",
            name="single-card plan",
            claim="Use the first payoff.",
            card_ids=(4,),
            review=review(FindingStatus.UNCERTAIN, same_reason),
        ),
        SimpleNamespace(
            category="strategy",
            name="paired plan",
            claim="Pair these cards for a closing line.",
            card_ids=(5, 6),
            review=review(FindingStatus.UNCERTAIN, same_reason),
        ),
    )
    oracle_facts = (
        SimpleNamespace(
            kind="token_maker",
            claim='{"card_name":"Token Maker","role":"token_maker"}',
            review=review(FindingStatus.ACCEPTED),
        ),
        SimpleNamespace(
            kind="token_maker",
            claim="PRIVATE-MODEL-CONTENT",
            review=review(FindingStatus.UNCERTAIN, same_reason),
        ),
        SimpleNamespace(
            kind="go_wide_payoff",
            claim="Supports a wide battlefield.",
            review=review(FindingStatus.ACCEPTED),
        ),
        SimpleNamespace(
            kind="combat_trick",
            claim="A combat trick.",
            review=review(FindingStatus.UNCERTAIN, None),
        ),
    )
    relationships = (
        SimpleNamespace(
            participants=(1, 2),
            mechanism="token-death-payoff",
            claim="The enabler supports the token payoff.",
            review=review(FindingStatus.ACCEPTED),
        ),
        SimpleNamespace(
            participants=(2, 3),
            mechanism="draw-payoff",
            claim="The pair generates cards.",
            review=review(FindingStatus.UNCERTAIN, "Evidence is incomplete."),
        ),
    )
    accounting = _enrichment_accounting()
    run = SimpleNamespace(
        outcome=EnrichmentOutcome.COMPLETE,
        set_source_sha256="b" * 64,
        guide_ids=("guide-1",),
        guide_results=(SimpleNamespace(malformed_reason="guide response malformed."),),
        card_ids=(1, 2),
        card_results=(
            SimpleNamespace(malformed_reason="card response malformed."),
            SimpleNamespace(malformed_reason=None),
        ),
        relationship_results=(
            SimpleNamespace(malformed_reason="relationship response malformed."),
        ),
        progress=SimpleNamespace(accounting=accounting),
    )
    artifact = SimpleNamespace(
        set_code="tst",
        review=SimpleNamespace(state="pending"),
        guides=(
            SimpleNamespace(
                url="https://guide.example.test/tst",
                sha256="c" * 64,
            ),
        ),
        runs=(
            SimpleNamespace(
                provider="openrouter",
                model="model-a",
                reasoning=SimpleNamespace(effort="medium"),
                prompt_id="prompt-a",
                response_schema_id="schema-a",
            ),
            SimpleNamespace(
                provider="openrouter",
                model="model-a",
                reasoning=SimpleNamespace(effort=None),
                prompt_id="prompt-a",
                response_schema_id="schema-a",
            ),
            SimpleNamespace(
                provider="other",
                model="model-b",
                reasoning=SimpleNamespace(effort="low"),
                prompt_id="prompt-b",
                response_schema_id="schema-b",
            ),
        ),
        oracle_facts=oracle_facts,
        guide_claims=guide_claims,
        relationships=relationships,
        rejected_findings=(
            SimpleNamespace(
                source_kind="guide",
                summary="unsupported claim",
                reason="schema rejected.",
            ),
        ),
    )
    return SimpleNamespace(
        set_code="tst",
        output_dir=tmp_path,
        run_dir=tmp_path / "enrichment-runs" / "tst" / "run-1",
        work_dir=tmp_path / "enrichment-work" / "tst",
        card_database_path=tmp_path / "card-data" / "tst.json",
        sources=SimpleNamespace(cards=cards),
        run=run,
        counts=SimpleNamespace(accepted=7, uncertain=4, rejected=2, failed=1),
        artifact=artifact,
        artifact_path=tmp_path / "artifacts" / "pending.json",
    )


def _enrichment_handler_argv(output_dir: Path) -> list[str]:
    return [
        "enrich-set",
        "TST",
        "--guide-url",
        "https://guide.example.test/tst",
        "--output-dir",
        str(output_dir),
    ]


def _complete_enrichment_analysis(output_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        output_dir=output_dir,
        run=SimpleNamespace(outcome=EnrichmentOutcome.COMPLETE),
    )


def test_enrich_set_parser_registers_arguments_and_requires_each_input() -> None:
    parser = build_parser()

    args = parser.parse_args(
        args=[
            "enrich-set",
            "LCI",
            "--guide-url",
            "https://x/y",
            "--output-dir",
            "/tmp/enrichment",
        ]
    )

    assert args.set == "LCI"
    assert args.guide_url == "https://x/y"
    assert args.output_dir == Path("/tmp/enrichment")
    assert args.handler is cli.handle_enrich_set

    missing_arguments = (
        ["enrich-set", "--guide-url", "https://x/y", "--output-dir", "/tmp/enrichment"],
        ["enrich-set", "LCI", "--output-dir", "/tmp/enrichment"],
        ["enrich-set", "LCI", "--guide-url", "https://x/y"],
    )
    for missing in missing_arguments:
        with pytest.raises(SystemExit) as error:
            parser.parse_args(args=missing)
        assert error.value.code == 2

    complete = [
        "enrich-set",
        "LCI",
        "--guide-url",
        "https://x/y",
        "--output-dir",
        "/tmp/enrichment",
    ]
    for unsupported in (["--profiles-dir", "/tmp/profiles"], ["--repo-root", "/tmp/repo"]):
        with pytest.raises(SystemExit) as error:
            parser.parse_args(args=[*complete, *unsupported])
        assert error.value.code == 2


def test_enrich_set_help_does_not_require_credentials_or_invoke_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def unexpected_analysis(**kwargs: object) -> NoReturn:
        raise AssertionError("analysis must not run for --help")

    monkeypatch.setattr(cli, "analyze_set_enrichment", unexpected_analysis)

    with pytest.raises(SystemExit) as error:
        main(argv=["enrich-set", "--help"])

    assert error.value.code == 0


@pytest.mark.parametrize(
    ("phase", "counters", "projected", "expected"),
    [
        (
            EnrichmentPhase.GUIDES,
            {"guides_completed": 1, "guides_total": 2},
            "0.012",
            "enrich-set progress: guide analysis 1/2 (50.0%) input_tokens=1200 cached_input_tokens=300 output_tokens=200 reasoning_tokens=40 executed_work=3 reused_work=0 work_without_cost=0 actual_cost_usd=0.006 projected_final_cost_usd=0.012\n",
        ),
        (
            EnrichmentPhase.CARD_CAPABILITIES,
            {"cards_completed": 296, "cards_total": 296},
            "0.012",
            "enrich-set progress: card analysis 296/296 (100.0%) input_tokens=1200 cached_input_tokens=300 output_tokens=200 reasoning_tokens=40 executed_work=3 reused_work=0 work_without_cost=0 actual_cost_usd=0.006 projected_final_cost_usd=0.012\n",
        ),
        (
            EnrichmentPhase.CANDIDATES,
            {"relationships_completed": 2, "relationships_total": 4},
            None,
            "enrich-set progress: candidate validation 2/4 (50.0%) input_tokens=1200 cached_input_tokens=300 output_tokens=200 reasoning_tokens=40 executed_work=3 reused_work=0 work_without_cost=0 actual_cost_usd=0.006 projected_final_cost_usd=unknown\n",
        ),
        (
            EnrichmentPhase.RELATIONSHIPS,
            {"relationships_completed": 3, "relationships_total": 4},
            "0.012",
            "enrich-set progress: candidate validation 3/4 (75.0%) input_tokens=1200 cached_input_tokens=300 output_tokens=200 reasoning_tokens=40 executed_work=3 reused_work=0 work_without_cost=0 actual_cost_usd=0.006 projected_final_cost_usd=0.012\n",
        ),
    ],
)
def test_print_enrichment_progress_maps_phases_and_preserves_accounting(
    phase: EnrichmentPhase,
    counters: dict[str, int],
    projected: str | None,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    accounting = _enrichment_accounting(projected_final_cost_usd=projected)

    cli._print_enrichment_progress(
        _enrichment_progress(phase, accounting=accounting, **counters)
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected


def test_print_enrichment_progress_reports_reused_work_without_executed_work(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli._print_enrichment_progress(
        _enrichment_progress(
            EnrichmentPhase.CARD_CAPABILITIES,
            accounting=_enrichment_accounting(executed_work=0, reused_work=7),
            cards_completed=296,
            cards_total=296,
        )
    )

    captured = capsys.readouterr()
    assert captured.err == (
        "enrich-set progress: card analysis 296/296 (100.0%) "
        "input_tokens=1200 cached_input_tokens=300 output_tokens=200 reasoning_tokens=40 "
        "executed_work=0 reused_work=7 work_without_cost=0 actual_cost_usd=0.006 "
        "projected_final_cost_usd=0.012\n"
    )


def test_format_enrichment_review_renders_complete_typed_report_without_raw_payload(
    tmp_path: Path,
) -> None:
    analysis = _enrichment_review_analysis(tmp_path)

    report = cli._format_enrichment_review(analysis)

    expected = f"""Draft Omen set enrichment review
Set: TST
Findings: accepted=7 uncertain=4 rejected=2 failed=1
Mechanics
- [accepted] Token creation: Creates creature tokens from a spell.
Card mechanic support
- combat_trick: accepted=0 uncertain=1
- go_wide_payoff: accepted=1 uncertain=0
- token_maker: accepted=1 uncertain=1
Format, colors, and archetypes
- [accepted] format_finding Limited: The format rewards efficient early plays.
- [uncertain] archetype Tokens: Token decks can go wide.
- [accepted] strategy Go wide: Build a broad battlefield.
Named synergies
- [uncertain] Delta (single-card plan): Use the first payoff.
- [uncertain] Epsilon+Zeta (paired plan): Pair these cards for a closing line.
Inferred synergies
- [accepted] Alpha+Beta (token-death-payoff): The enabler supports the token payoff.
- [uncertain] Beta+Gamma (draw-payoff): The pair generates cards.
Uncertainty
- 1x Evidence is incomplete.
- 4x Needs more context.
Validation failures
- guide unsupported claim: schema rejected.
- guide guide-1: guide response malformed.
- card 1: card response malformed.
- relationship 1: relationship response malformed.
Provenance
Source: set=tst sha256={"b" * 64}
Guide: https://guide.example.test/tst sha256={"c" * 64}
Model: provider=openrouter model=model-a reasoning=medium
Model: provider=openrouter model=model-a reasoning=none
Model: provider=other model=model-b reasoning=low
Prompt: prompt-a schema=schema-a
Prompt: prompt-b schema=schema-b
Paths
Run: {analysis.run_dir}
Work: {analysis.work_dir}
Card data: {analysis.card_database_path}
Pending enrichment: {analysis.artifact_path}
Final accounting: input_tokens=1200 cached_input_tokens=300 output_tokens=200 reasoning_tokens=40 executed_work=3 reused_work=0 work_without_cost=0 actual_cost_usd=0.006 projected_final_cost_usd=0.012 final_cost_usd=0.006
"""

    assert report == expected
    assert '{"card_name":"Token Maker","role":"token_maker"}' not in report
    assert "PRIVATE-MODEL-CONTENT" not in report
    assert report.count("Delta") == 1
    assert report.count("Epsilon") == 1
    assert report.count("Zeta") == 1


def test_format_enrichment_review_renders_none_for_empty_inferred_section(
    tmp_path: Path,
) -> None:
    analysis = _enrichment_review_analysis(tmp_path)
    analysis.artifact.relationships = ()

    report = cli._format_enrichment_review(analysis)

    assert "Inferred synergies\nnone\nUncertainty\n" in report


def test_handle_enrich_set_confirm_reports_publication_and_reviewer_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    analysis = _complete_enrichment_analysis(tmp_path)
    finalize_calls: dict[str, object] = {}
    publication = SimpleNamespace(
        artifact_path=tmp_path / "profile.json.gz",
        manifest_path=tmp_path / "generation.json",
        generation=SimpleNamespace(
            report=SimpleNamespace(
                profile_sha256="profile-sha",
                gzip_sha256="gzip-sha",
            )
        ),
    )
    published_object = tmp_path / "website/public/profiles/objects/gzip-sha.json.gz"
    published_manifest = tmp_path / "website/public/profiles/manifest.json"

    monkeypatch.setattr(cli, "analyze_set_enrichment", lambda **kwargs: analysis)
    monkeypatch.setattr(cli, "_format_enrichment_review", lambda value: "")
    monkeypatch.setattr("builtins.input", lambda prompt: "Confirm")

    def finalize(**kwargs: object) -> SimpleNamespace:
        finalize_calls.update(kwargs)
        return SimpleNamespace(
            decision=cli.EnrichmentReviewDecision.CONFIRM,
            artifact=SimpleNamespace(),
            artifact_path=tmp_path / "confirmed.json",
            publication=publication,
            published_object_path=published_object,
            published_manifest_path=published_manifest,
        )

    monkeypatch.setattr(cli, "finalize_set_enrichment", finalize)

    exit_code = main(argv=_enrichment_handler_argv(tmp_path))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == (
        "decision=Confirm\n"
        f"enrichment_artifact={tmp_path / 'confirmed.json'}\n"
        f"published_profile_object={published_object}\n"
        "profile_sha256=profile-sha\n"
        "gzip_sha256=gzip-sha\n"
        f"profile_manifest={published_manifest}\n"
    )
    assert captured.err == ""
    assert finalize_calls["reviewer_id"] == "draftomen-tui"
    assert finalize_calls["decision"] is cli.EnrichmentReviewDecision.CONFIRM
    assert isinstance(finalize_calls["reviewed_at"], datetime)
    assert finalize_calls["reviewed_at"].tzinfo is not None


@pytest.mark.parametrize(
    ("input_value", "invalid", "leading_newline"),
    [
        ("Cancel", False, False),
        ("", False, False),
        ("   ", False, False),
        ("confirm", True, False),
        ("yes", True, False),
        ("Reject", True, False),
        ("reject", True, False),
        (EOFError(), False, True),
        (KeyboardInterrupt(), False, True),
    ],
    ids=[
        "explicit-cancel",
        "blank",
        "whitespace",
        "lowercase-confirm",
        "yes",
        "literal-reject",
        "lowercase-reject",
        "eof",
        "prompt-ctrl-c",
    ],
)
def test_handle_enrich_set_defaults_every_non_confirm_decision_to_cancel(
    input_value: str | BaseException,
    invalid: bool,
    leading_newline: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    analysis = _complete_enrichment_analysis(tmp_path)
    decisions: list[object] = []

    monkeypatch.setattr(cli, "analyze_set_enrichment", lambda **kwargs: analysis)
    monkeypatch.setattr(cli, "_format_enrichment_review", lambda value: "")

    def input_value_or_raise(prompt: str) -> str:
        if isinstance(input_value, BaseException):
            raise input_value
        return input_value

    monkeypatch.setattr("builtins.input", input_value_or_raise)
    monkeypatch.setattr(
        cli,
        "finalize_set_enrichment",
        lambda **kwargs: (
            decisions.append(kwargs["decision"])
            or SimpleNamespace(
                decision=cli.EnrichmentReviewDecision.CANCEL,
                artifact=SimpleNamespace(),
                artifact_path=tmp_path / "cancelled.json",
                publication=None,
            )
        ),
    )

    exit_code = main(argv=_enrichment_handler_argv(tmp_path))

    captured = capsys.readouterr()
    prefix = "\n" if leading_newline else ""
    assert exit_code == 0
    assert captured.out == (
        prefix
        + "decision=Cancel\n"
        + f"enrichment_artifact={tmp_path / 'cancelled.json'}\n"
        + "profile=not-published\n"
    )
    assert captured.err == (
        "Invalid decision; cancelling.\n" if invalid else ""
    )
    assert decisions == [cli.EnrichmentReviewDecision.CANCEL]


@pytest.mark.parametrize("analysis_mode", ["cancelled", "keyboard-interrupt"])
def test_handle_enrich_set_analysis_cancellation_preserves_work_and_skips_finalize(
    analysis_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    finalize_called = False

    if analysis_mode == "cancelled":
        monkeypatch.setattr(
            cli,
            "analyze_set_enrichment",
            lambda **kwargs: SimpleNamespace(
                output_dir=tmp_path,
                run=SimpleNamespace(outcome=EnrichmentOutcome.CANCELLED),
            ),
        )
    else:
        def interrupted_analysis(**kwargs: object) -> NoReturn:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "analyze_set_enrichment", interrupted_analysis)

    def unexpected_finalize(**kwargs: object) -> NoReturn:
        nonlocal finalize_called
        finalize_called = True
        raise AssertionError("finalization must not run")

    monkeypatch.setattr(cli, "finalize_set_enrichment", unexpected_finalize)

    exit_code = main(argv=_enrichment_handler_argv(tmp_path))

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == (
        f"enrich-set cancelled: resumable work preserved under {tmp_path}\n"
    )
    assert finalize_called is False


def test_handle_enrich_set_analysis_errors_are_bounded_and_path_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    sentinel = "PRIVATE-ANALYSIS-CAUSE"

    def fail_analysis(**kwargs: object) -> NoReturn:
        try:
            raise RuntimeError(sentinel)
        except RuntimeError as cause:
            raise cli.SetEnrichmentWorkflowError(ANALYSIS_ERROR) from cause

    monkeypatch.setattr(cli, "analyze_set_enrichment", fail_analysis)

    exit_code = main(argv=_enrichment_handler_argv(tmp_path))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "enrich-set failed: Set-enrichment analysis failed.\n"
    assert sentinel not in captured.err


@pytest.mark.parametrize(
    ("message", "with_review_result"),
    [
        (NO_PUBLISHABLE_ERROR, True),
        (cli.PROFILE_PUBLICATION_ERROR, True),
        (cli.INCOMPLETE_ANALYSIS_ERROR, False),
    ],
    ids=["no-publishable-retains-review", "publication-retains-review", "without-review"],
)
def test_handle_enrich_set_finalization_failures_report_only_retained_review(
    message: str,
    with_review_result: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    analysis = _complete_enrichment_analysis(tmp_path)
    retained_path = tmp_path / "confirmed.json"

    monkeypatch.setattr(cli, "analyze_set_enrichment", lambda **kwargs: analysis)
    monkeypatch.setattr(cli, "_format_enrichment_review", lambda value: "")
    monkeypatch.setattr("builtins.input", lambda prompt: "Confirm")

    def fail_finalize(**kwargs: object) -> NoReturn:
        review_result = (
            SimpleNamespace(artifact_path=retained_path)
            if with_review_result
            else None
        )
        raise cli.SetEnrichmentWorkflowError(message, review_result=review_result)

    monkeypatch.setattr(cli, "finalize_set_enrichment", fail_finalize)

    exit_code = main(argv=_enrichment_handler_argv(tmp_path))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == f"enrich-set failed: {message}\n"
    if with_review_result:
        assert captured.out == (
            "decision=Confirm\n"
            f"enrichment_artifact={retained_path}\n"
            "profile=not-published\n"
        )
    else:
        assert captured.out == ""


@pytest.mark.parametrize("missing", ["published_object_path", "published_manifest_path"])
def test_handle_enrich_set_reports_not_published_for_incomplete_repository_publication(
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    analysis = _complete_enrichment_analysis(tmp_path)
    retained_path = tmp_path / "confirmed.json"
    publication = SimpleNamespace(generation=SimpleNamespace(report=SimpleNamespace()))

    monkeypatch.setattr(cli, "analyze_set_enrichment", lambda **kwargs: analysis)
    monkeypatch.setattr(cli, "_format_enrichment_review", lambda value: "")
    monkeypatch.setattr("builtins.input", lambda prompt: "Confirm")
    monkeypatch.setattr(
        cli,
        "finalize_set_enrichment",
        lambda **kwargs: SimpleNamespace(
            decision=cli.EnrichmentReviewDecision.CONFIRM,
            artifact=SimpleNamespace(),
            artifact_path=retained_path,
            publication=publication,
            published_object_path=None if missing == "published_object_path" else tmp_path / "object.json.gz",
            published_manifest_path=None if missing == "published_manifest_path" else tmp_path / "manifest.json",
        ),
    )

    exit_code = main(argv=_enrichment_handler_argv(tmp_path))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == (
        "decision=Confirm\n"
        f"enrichment_artifact={retained_path}\n"
        "profile=not-published\n"
    )
    assert captured.err == f"enrich-set failed: {cli.PROFILE_PUBLICATION_ERROR}\n"


REPUBLISH_RUN_ID = "9574d202eef14943"
REPUBLISH_REVIEWED_AT = "2026-09-01T14:00:00Z"
REPUBLISH_GUIDE_TEXT = "TST rewards going wide with support creatures."
REPUBLISH_MISSING_CARD_DATA_ERROR = (
    "The selected enrichment run is missing its frozen card data."
)
REPUBLISH_MISSING_SET_ERROR = (
    "No confirmed enrichment artifact is available for the requested set."
)
REPUBLISH_MISSING_ARTIFACT_ERROR = (
    "The requested enrichment artifact is not available or not confirmed."
)


def _republish_guide() -> GuideSource:
    """Build the frozen guide shared by the saved artifact and its run directory."""

    return GuideSource(
        guide_id="tst-draftsim-guide",
        url="https://draftsim.com/tst-limited-set-review/",
        text=REPUBLISH_GUIDE_TEXT,
        retrieved_at="2026-09-01T12:00:00Z",
    )


def _republish_enrichment_artifact(
    *,
    database: CardDatabase,
    guide: GuideSource,
) -> SemanticEnrichmentArtifact:
    """Build one confirmed artifact that its frozen run sources can revalidate."""

    sources = EnrichmentSources(
        set_code="TST",
        cards=tuple(database.cards.values()),
        guides=(guide,),
    )
    relationship = replace(
        _typed_relationship(),
        run_id=REPUBLISH_RUN_ID,
        guide_evidence=(
            GuideEvidence(guide_id=guide.guide_id, quote="rewards going wide"),
        ),
    )
    return SemanticEnrichmentArtifact(
        set_code="TST",
        set_source_id="tst-card-data-v1",
        set_source_sha256=set_source_sha256(sources),
        created_at="2026-09-01T12:02:00Z",
        cards=tuple(
            CardSourcePin(
                card_id=card.grp_id,
                oracle_id=card.oracle_id,
                collector_number=card.collector_number,
                sha256=card_source_sha256(card),
            )
            for card in sources.cards
        ),
        guides=(
            GuideSourcePin(
                guide_id=guide.guide_id,
                url=guide.url,
                sha256=guide.text_sha256,
                retrieved_at=guide.retrieved_at,
            ),
        ),
        runs=(_enrichment_run(run_id=REPUBLISH_RUN_ID),),
        oracle_facts=(),
        guide_claims=(),
        relationships=(relationship,),
        rejected_findings=(),
        review=ArtifactReview(
            state="confirmed",
            reviewer_id="local-review",
            reviewed_at=REPUBLISH_REVIEWED_AT,
        ),
        confirmed_relationship_ids=(relationship.finding_id,),
        sources=sources,
    )


def _republish_card_database() -> CardDatabase:
    """Build the store's frozen card data with production's lowercase set code."""

    return CardDatabase(
        cards={
            card_id: replace(card, set_code="tst")
            for card_id, card in _typed_database().cards.items()
        }
    )


def _write_republish_store(*, store_dir: Path) -> Path:
    """Write one confirmed artifact and its frozen run sources into a local store."""

    database = _republish_card_database()
    guide = _republish_guide()
    artifact_bytes = _republish_enrichment_artifact(
        database=database,
        guide=guide,
    ).to_bytes()
    run_dir = store_dir / "enrichment-runs" / "tst" / REPUBLISH_RUN_ID
    artifact_path = (
        run_dir / "artifacts" / f"{hashlib.sha256(artifact_bytes).hexdigest()}.json"
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_bytes)
    sources_dir = run_dir / "sources"
    sources_dir.mkdir()
    (sources_dir / "card-database.json").write_text(
        json.dumps(database.to_json()),
        encoding="utf-8",
    )
    (sources_dir / "guide.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requested_url": "https://draftsim.com/tst-limited-set-review",
                "guide_id": guide.guide_id,
                "url": guide.url,
                "text": guide.text,
                "sha256": guide.text_sha256,
                "retrieved_at": guide.retrieved_at,
            }
        ),
        encoding="utf-8",
    )
    return artifact_path


def _write_republish_ratings(*, path: Path) -> Path:
    """Write the TST QuickDraft ratings cache the recovery command reads."""

    path.write_text(json.dumps(_ratings().to_json()), encoding="utf-8")
    return path


def _write_republish_profiles(*, profiles_dir: Path) -> None:
    """Write a published profiles tree whose manifest already holds another set."""

    manifest = ProfileManifest(
        artifacts=(
            ProfileManifestArtifact(
                set_code="oth",
                event_format="QuickDraft",
                set_profile_schema_version=3,
                profile_version="1.0",
                generated_at="2026-08-01T00:00:00+00:00",
                url="https://www.draftomen.com/profiles/objects/oth.json.gz",
                gzip_bytes=1,
                profile_bytes=1,
                gzip_sha256="a" * 64,
                profile_sha256="b" * 64,
                maturity="metadata-only",
            ),
        ),
        published_at="2026-08-01T00:00:00+00:00",
    )
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "manifest.json").write_bytes(manifest.to_bytes())


def test_republish_enrichment_publishes_a_saved_confirmed_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    artifact_path = _write_republish_store(store_dir=store_dir)
    profiles_dir = tmp_path / "profiles"
    _write_republish_profiles(profiles_dir=profiles_dir)

    exit_code = main(
        argv=[
            "republish-enrichment",
            "tst",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
        ]
    )

    captured = capsys.readouterr()
    printed = dict(line.split("=", 1) for line in captured.out.splitlines())
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    gzip_sha256 = printed["gzip_sha256"]

    assert exit_code == 0
    assert captured.err == ""
    assert printed["set_code"] == "tst"
    assert printed["format"] == "quickdraft"
    assert printed["artifact"] == str(artifact_path)
    assert printed["artifact_sha256"] == artifact_sha256
    assert printed["run_id"] == REPUBLISH_RUN_ID
    assert printed["maturity"] == "metadata-only"
    assert printed["object"] == str(profiles_dir / "objects" / f"{gzip_sha256}.json.gz")
    assert printed["manifest"] == str(profiles_dir / "manifest.json")
    assert printed["manifest_changed"] == "True"
    assert printed["publications"] == str(
        profiles_dir / "enrichment-publications.json"
    )
    assert (
        store_dir
        / "tst-quickdraft"
        / "tst-quickdraft"
        / "artifacts"
        / f"{gzip_sha256}.json.gz"
    ).is_file()

    record = json.loads(
        (profiles_dir / "enrichment-publications.json").read_text(encoding="utf-8")
    )
    assert record == {
        "candidates": [],
        "publications": [
            {
                "artifact_sha256": artifact_sha256,
                "event_format": "quickdraft",
                "profile_gzip_sha256": gzip_sha256,
                "published_at": "2026-09-01T14:00:00+00:00",
                "reviewed_at": REPUBLISH_REVIEWED_AT,
                "run_id": REPUBLISH_RUN_ID,
                "set_code": "tst",
            }
        ],
        "schema_version": 2,
    }

    manifest = ProfileManifest.from_bytes((profiles_dir / "manifest.json").read_bytes())
    entry = manifest.select(set_code="tst", event_format="quickdraft")
    assert entry is not None
    assert entry.gzip_sha256 == gzip_sha256
    assert manifest.select(set_code="oth", event_format="quickdraft") is not None

    published_profile = json.loads(
        gzip.decompress(
            (profiles_dir / "objects" / f"{gzip_sha256}.json.gz").read_bytes()
        )
    )
    assert published_profile["enhancement_status"] == "enhanced"
    assert published_profile["enhancement"]["artifact_sha256"] == artifact_sha256


def test_republish_enrichment_reports_an_unknown_set_without_touching_profiles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    _write_republish_store(store_dir=store_dir)
    profiles_dir = tmp_path / "profiles"

    exit_code = main(
        argv=[
            "republish-enrichment",
            "zzz",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        f"republish-enrichment failed: {REPUBLISH_MISSING_SET_ERROR}\n"
    )
    assert not profiles_dir.exists()


def test_republish_enrichment_reports_an_unknown_artifact_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    _write_republish_store(store_dir=store_dir)
    profiles_dir = tmp_path / "profiles"

    exit_code = main(
        argv=[
            "republish-enrichment",
            "tst",
            "--artifact",
            "f" * 64,
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        f"republish-enrichment failed: {REPUBLISH_MISSING_ARTIFACT_ERROR}\n"
    )
    assert not profiles_dir.exists()


def test_republish_enrichment_requires_the_frozen_card_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    artifact_path = _write_republish_store(store_dir=store_dir)
    (artifact_path.parent.parent / "sources" / "card-database.json").unlink()
    profiles_dir = tmp_path / "profiles"

    exit_code = main(
        argv=[
            "republish-enrichment",
            "tst",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        f"republish-enrichment failed: {REPUBLISH_MISSING_CARD_DATA_ERROR}\n"
    )
    assert not profiles_dir.exists()


def test_republish_enrichment_recovers_a_role_bearing_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    artifact_path = _write_republish_store(store_dir=store_dir)
    ratings_path = _write_republish_ratings(
        path=tmp_path / "tst-quickdraft-ratings.json"
    )
    profiles_dir = tmp_path / "profiles"
    _write_republish_profiles(profiles_dir=profiles_dir)

    exit_code = main(
        argv=[
            "republish-enrichment",
            "tst",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
            "--stage",
            "early",
            "--ratings-file",
            str(ratings_path),
        ]
    )

    captured = capsys.readouterr()
    printed = dict(line.split("=", 1) for line in captured.out.splitlines())
    artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    assert exit_code == 0
    assert captured.err == ""
    assert printed["maturity"] == "early"
    assert printed["artifact_sha256"] == artifact_sha256

    profile_bytes = (
        profiles_dir / "objects" / f"{printed['gzip_sha256']}.json.gz"
    ).read_bytes()
    published_profile = json.loads(gzip.decompress(profile_bytes))

    assert published_profile["schema_version"] == 3
    assert published_profile["enhancement_status"] == "enhanced"
    assert published_profile["enhancement"]["artifact_sha256"] == artifact_sha256
    assert published_profile["role_profile"]["cards"]

    loaded = SetProfile.from_json(published_profile)
    assert (
        relationship_enhancement_is_compatible(
            set_profile=loaded,
            card_database=_republish_card_database(),
        )
        is True
    )


def test_republish_enrichment_requires_empirical_inputs_for_a_role_bearing_stage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    _write_republish_store(store_dir=store_dir)
    profiles_dir = tmp_path / "profiles"
    _write_republish_profiles(profiles_dir=profiles_dir)

    exit_code = main(
        argv=[
            "republish-enrichment",
            "tst",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
            "--stage",
            "early",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        "republish-enrichment failed: Early profile generation requires "
        "empirical ratings or accepted draft evidence.\n"
    )
    assert not (store_dir / "tst-quickdraft").exists()
    assert not (profiles_dir / "objects").exists()
    assert not (profiles_dir / "enrichment-publications.json").exists()
    manifest = ProfileManifest.from_bytes((profiles_dir / "manifest.json").read_bytes())
    assert manifest.select(set_code="tst", event_format="quickdraft") is None
    assert manifest.select(set_code="oth", event_format="quickdraft") is not None


def test_republish_enrichment_rejects_an_unusable_ratings_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    _write_republish_store(store_dir=store_dir)
    profiles_dir = tmp_path / "profiles"
    _write_republish_profiles(profiles_dir=profiles_dir)
    ratings_path = _write_republish_ratings(
        path=tmp_path / "tst-quickdraft-ratings.json"
    )
    payload = json.loads(ratings_path.read_text(encoding="utf-8"))
    payload["set_code"] = "OTH"
    ratings_path.write_text(json.dumps(payload), encoding="utf-8")

    for ratings_input in (ratings_path, tmp_path / "missing-ratings.json"):
        exit_code = main(
            argv=[
                "republish-enrichment",
                "tst",
                "--store-dir",
                str(store_dir),
                "--profiles-dir",
                str(profiles_dir),
                "--stage",
                "early",
                "--ratings-file",
                str(ratings_input),
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert captured.out == ""
        assert captured.err == (
            "republish-enrichment failed: Could not load the ratings input.\n"
        )
        assert not (store_dir / "tst-quickdraft").exists()
        assert not (profiles_dir / "objects").exists()
        assert not (profiles_dir / "enrichment-publications.json").exists()
        manifest = ProfileManifest.from_bytes(
            (profiles_dir / "manifest.json").read_bytes()
        )
        assert manifest.select(set_code="tst", event_format="quickdraft") is None
        assert manifest.select(set_code="oth", event_format="quickdraft") is not None


LIST_ENRICHMENT_CONFIRMED_SET = "hob"
LIST_ENRICHMENT_CONFIRMED_RUN_ID = "9574d202eef14943"
LIST_ENRICHMENT_PENDING_SET = "lci"
LIST_ENRICHMENT_PENDING_RUN_ID = "da5336b1f2ceac32"
LIST_ENRICHMENT_CREATED_AT = "2026-08-30T09:00:00+00:00"
LIST_ENRICHMENT_REVIEWED_AT = "2026-09-01T10:00:00+00:00"
LIST_ENRICHMENT_PROFILE_GZIP_SHA256 = "f" * 64


def _write_list_enrichment_artifact(
    *,
    store_dir: Path,
    set_code: str,
    run_id: str,
    review_state: str,
    reviewed_at: str | None,
    created_at: str | None,
    relationships: int,
    confirmed: int,
) -> Path:
    """Write one saved enrichment artifact into a local store layout."""

    payload = {
        "set_code": set_code,
        "created_at": created_at,
        "review": {"state": review_state, "reviewed_at": reviewed_at},
        "relationships": [
            {"finding_id": f"finding-{index}"} for index in range(relationships)
        ],
        "confirmed_relationship_ids": [
            f"finding-{index}" for index in range(confirmed)
        ],
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    artifacts_dir = store_dir / "enrichment-runs" / set_code / run_id / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"{hashlib.sha256(payload_bytes).hexdigest()}.json"
    path.write_bytes(payload_bytes)
    return path


def _write_list_enrichment_store(*, store_dir: Path) -> tuple[Path, Path]:
    """Write one confirmed HOB artifact and one pending LCI artifact into a store."""

    confirmed_path = _write_list_enrichment_artifact(
        store_dir=store_dir,
        set_code=LIST_ENRICHMENT_CONFIRMED_SET,
        run_id=LIST_ENRICHMENT_CONFIRMED_RUN_ID,
        review_state="confirmed",
        reviewed_at=LIST_ENRICHMENT_REVIEWED_AT,
        created_at=LIST_ENRICHMENT_CREATED_AT,
        relationships=3,
        confirmed=2,
    )
    pending_path = _write_list_enrichment_artifact(
        store_dir=store_dir,
        set_code=LIST_ENRICHMENT_PENDING_SET,
        run_id=LIST_ENRICHMENT_PENDING_RUN_ID,
        review_state="pending",
        reviewed_at=None,
        created_at=None,
        relationships=0,
        confirmed=0,
    )
    return confirmed_path, pending_path


def _write_list_enrichment_profiles(*, profiles_dir: Path, artifact_sha256: str) -> None:
    """Write an orphaned enriched HOB object beside a manifest holding another set
    and legacy non-profile payloads."""

    _write_republish_profiles(profiles_dir=profiles_dir)
    objects_dir = profiles_dir / "objects"
    objects_dir.mkdir()
    (objects_dir / f"{LIST_ENRICHMENT_PROFILE_GZIP_SHA256}.json.gz").write_bytes(
        gzip.compress(
            json.dumps(
                {
                    "set_code": LIST_ENRICHMENT_CONFIRMED_SET,
                    "format": "quickdraft",
                    "enhancement_status": "enhanced",
                    "enhancement": {"artifact_sha256": artifact_sha256},
                },
                sort_keys=True,
            ).encode("utf-8")
        )
    )
    # Legacy non-profile payloads under a content address stay skippable.
    (objects_dir / "not-gzip.json.gz").write_bytes(b"{not a gzip stream")
    (objects_dir / "not-utf8.json.gz").write_bytes(gzip.compress(b"\x80\x81"))
    (objects_dir / "not-json.json.gz").write_bytes(gzip.compress(b"{not json"))
    (objects_dir / "not-a-profile.json.gz").write_bytes(gzip.compress(b"[1, 2, 3]"))


def test_list_enrichment_prints_runs_artifacts_and_publication_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    confirmed_path, pending_path = _write_list_enrichment_store(store_dir=store_dir)
    confirmed_sha256 = hashlib.sha256(confirmed_path.read_bytes()).hexdigest()
    pending_sha256 = hashlib.sha256(pending_path.read_bytes()).hexdigest()
    profiles_dir = tmp_path / "profiles"
    _write_list_enrichment_profiles(
        profiles_dir=profiles_dir,
        artifact_sha256=confirmed_sha256,
    )
    manifest_bytes = (profiles_dir / "manifest.json").read_bytes()

    exit_code = main(
        argv=[
            "list-enrichment",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == (
        f"run {LIST_ENRICHMENT_CONFIRMED_SET} {LIST_ENRICHMENT_CONFIRMED_RUN_ID}"
        " artifacts=1\n"
        f"  artifact {LIST_ENRICHMENT_CONFIRMED_SET} {LIST_ENRICHMENT_CONFIRMED_RUN_ID}"
        f" {confirmed_sha256} created={LIST_ENRICHMENT_CREATED_AT}"
        f" reviewed={LIST_ENRICHMENT_REVIEWED_AT}"
        " state=confirmed relationships=3 confirmed=2"
        " published=hob/quickdraft:orphaned\n"
        f"run {LIST_ENRICHMENT_PENDING_SET} {LIST_ENRICHMENT_PENDING_RUN_ID}"
        " artifacts=1\n"
        f"  artifact {LIST_ENRICHMENT_PENDING_SET} {LIST_ENRICHMENT_PENDING_RUN_ID}"
        f" {pending_sha256} created=unknown reviewed=unknown state=pending"
        " relationships=0 confirmed=0 published=none\n"
        "list-enrichment: runs=2 artifacts=2 confirmed=1 published=1 orphaned=1\n"
    )
    assert (profiles_dir / "manifest.json").read_bytes() == manifest_bytes
    assert not (profiles_dir / "enrichment-publications.json").exists()


def test_list_enrichment_filters_runs_by_set_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    confirmed_path, pending_path = _write_list_enrichment_store(store_dir=store_dir)
    profiles_dir = tmp_path / "profiles"
    _write_list_enrichment_profiles(
        profiles_dir=profiles_dir,
        artifact_sha256=hashlib.sha256(confirmed_path.read_bytes()).hexdigest(),
    )

    exit_code = main(
        argv=[
            "list-enrichment",
            "--set",
            "LCI",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    # The filter selects one set code for the printed runs and for every count.
    assert captured.out == (
        f"run {LIST_ENRICHMENT_PENDING_SET} {LIST_ENRICHMENT_PENDING_RUN_ID}"
        " artifacts=1\n"
        f"  artifact {LIST_ENRICHMENT_PENDING_SET} {LIST_ENRICHMENT_PENDING_RUN_ID}"
        f" {hashlib.sha256(pending_path.read_bytes()).hexdigest()}"
        " created=unknown reviewed=unknown state=pending relationships=0 confirmed=0"
        " published=none\n"
        "list-enrichment: runs=1 artifacts=1 confirmed=0 published=0 orphaned=0\n"
    )


def test_list_enrichment_reports_an_unreadable_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    _write_list_enrichment_store(store_dir=store_dir)
    runs_dir = store_dir / "enrichment-runs"
    runs_dir.chmod(0o000)
    profiles_dir = tmp_path / "profiles"

    try:
        exit_code = main(
            argv=[
                "list-enrichment",
                "--store-dir",
                str(store_dir),
                "--profiles-dir",
                str(profiles_dir),
            ]
        )
    finally:
        runs_dir.chmod(0o700)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == f"list-enrichment failed: {ARTIFACT_ERROR}\n"


def test_list_enrichment_reports_an_unreadable_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "set-enrichment"
    _write_list_enrichment_store(store_dir=store_dir)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "manifest.json").write_bytes(b"{not json")

    exit_code = main(
        argv=[
            "list-enrichment",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == f"list-enrichment failed: {PUBLICATION_ERROR}\n"


def test_list_enrichment_reports_an_unreadable_profile_object(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_dir = tmp_path / "set-enrichment"
    confirmed_path, _ = _write_list_enrichment_store(store_dir=store_dir)
    profiles_dir = tmp_path / "profiles"
    _write_list_enrichment_profiles(
        profiles_dir=profiles_dir,
        artifact_sha256=hashlib.sha256(confirmed_path.read_bytes()).hexdigest(),
    )
    object_path = (
        profiles_dir / "objects" / f"{LIST_ENRICHMENT_PROFILE_GZIP_SHA256}.json.gz"
    )

    real_read_bytes = Path.read_bytes

    def fail_read_bytes(path: Path) -> bytes:
        if path == object_path:
            raise PermissionError("read denied")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    exit_code = main(
        argv=[
            "list-enrichment",
            "--store-dir",
            str(store_dir),
            "--profiles-dir",
            str(profiles_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == f"list-enrichment failed: {PUBLICATION_ERROR}\n"

