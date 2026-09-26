"""Focused checks for native desktop deployment inputs."""

from __future__ import annotations

import configparser
import gzip
import hashlib
import json
import plistlib
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest

from draftomen import qt_gui
from draftomen.set_profile import load_set_profile

from tests import bundle_smoke
from tests.bundle_smoke import _resolve_bundle_executable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATHS = {
    "macos": PROJECT_ROOT / "pysidedeploy.macos.spec",
    "windows": PROJECT_ROOT / "pysidedeploy.windows.spec",
}
EXPECTED_EXCLUDED_QML_PLUGINS = {
    "QtCharts",
    "QtQuick3D",
    "QtSensors",
    "QtWebEngine",
}
EXPECTED_PRODUCT_NAME = "Draft Omen"
EXPECTED_BUNDLE_IDENTIFIER = "io.github.andreagrandi.draftomen"


BASELINE_PROFILE_RESOURCE = "draftomen/baseline_profiles/hob-quickdraft.json"
BASELINE_PROFILE_PATH = PROJECT_ROOT / BASELINE_PROFILE_RESOURCE
BASELINE_PROFILE_MAPPING = f"--include-data-files={BASELINE_PROFILE_RESOURCE}={BASELINE_PROFILE_RESOURCE}"
SELECTED_GZIP_SHA256 = "3ea8e91d02b63a724016ec5dd45b5d1b53ff0aecdad1feda6e495c588d831447"
SELECTED_SOURCE_BYTES = 12997
SELECTED_SOURCE_SHA256 = "362fb91ba62f3d5a324eb21371102a6e9b9835be88553ed69a076a741fcb7302"
SELECTED_GZIP_PATH = (
    PROJECT_ROOT
    / "profile-snapshots"
    / "hob-quickdraft"
    / SELECTED_GZIP_SHA256
    / f"{SELECTED_GZIP_SHA256}.json.gz"
)


def _read_spec(*, path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def _read_project_metadata() -> dict[str, object]:
    with (PROJECT_ROOT / "pyproject.toml").open(mode="rb") as project_file:
        return tomllib.load(project_file)["project"]


def _requirement_name(requirement: str) -> str:
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
    assert match is not None, requirement
    return match.group(0).lower().replace("_", "-").replace(".", "-")


def _completed_process(
    *,
    command: list[str],
    stdout: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=command, returncode=returncode, stdout=stdout, stderr=""
    )


def _forbid_process_launch(**kwargs: object) -> None:
    raise AssertionError(f"unexpected process launch: {kwargs}")


def _forbid_server_probe(*, server_url: str) -> None:
    raise AssertionError(f"unexpected server probe: {server_url}")


def _create_macos_bundle(
    *,
    root: Path,
    metadata: object,
    executable_name: str | None = "qt_gui",
) -> Path:
    bundle_path = root / "Draftomen.app"
    executable_directory = bundle_path / "Contents" / "MacOS"
    executable_directory.mkdir(parents=True)
    if executable_name is not None:
        (executable_directory / executable_name).write_bytes(b"executable")
    with (bundle_path / "Contents" / "Info.plist").open(mode="wb") as plist_file:
        plistlib.dump(metadata, plist_file)
    return bundle_path


def _prepare_test_draft_journey(
    *,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Create the pinned Test Draft inputs and stub the server probe."""

    bundle_path = root / "Draftomen.exe"
    bundle_path.write_bytes(b"executable")
    draftmancer_dir = root / "Draftmancer"
    draftmancer_dir.mkdir()
    scryfall_bulk_file = root / "scryfall-default-cards.jsonl.gz"
    scryfall_bulk_file.write_bytes(b"bulk")
    app_dir = root / "prepared-app"
    app_dir.mkdir()

    def fake_probe(*, server_url: str) -> None:
        assert server_url == bundle_smoke.DEFAULT_SERVER_URL

    monkeypatch.setattr(bundle_smoke, "_probe_draftmancer_server", fake_probe)
    return [
        str(bundle_path),
        "--test-draft",
        "--draftmancer-dir",
        str(draftmancer_dir),
        "--scryfall-bulk-file",
        str(scryfall_bulk_file),
        "--app-dir",
        str(app_dir),
    ]


def test_bundle_smoke_main_configures_launch_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The smoke command uses isolated app directories and strict timeouts."""

    bundle_path = tmp_path / "Draftomen.exe"
    bundle_path.write_bytes(b"executable")
    calls: list[dict[str, object]] = []
    providers: list[str] = []
    environments: list[dict[str, str]] = []

    monkeypatch.setenv("PYTHONHOME", "/sentinel")
    monkeypatch.setenv("PYTHONPATH", "/sentinel")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/sentinel")
    monkeypatch.setenv("VIRTUAL_ENV", "/sentinel")

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str] | None:
        command = kwargs["args"]
        assert isinstance(command, list)
        provider = command[command.index("--provider") + 1]
        app_directory = Path(command[command.index("--app-dir") + 1])
        assert app_directory.parent.is_dir()
        assert not (app_directory / "set-profiles" / "hob-quickdraft.json").exists()
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        for variable in (
            "PYTHONHOME",
            "PYTHONPATH",
            "UV_PROJECT_ENVIRONMENT",
            "VIRTUAL_ENV",
        ):
            assert variable not in environment
        providers.append(provider)
        environments.append(environment)
        calls.append(kwargs)
        if provider == "live":
            Path(command[command.index("--screenshot") + 1]).write_bytes(b"png")
            return _completed_process(command=command)
        return None

    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    assert bundle_smoke.main([str(bundle_path)]) == 0
    assert bundle_smoke.main([str(bundle_path), "--timeout", "300"]) == 0
    assert [call["timeout"] for call in calls] == [60, 60, 300, 300]
    assert providers == ["mock", "live", "mock", "live"]
    assert environments[0] == environments[1]
    assert environments[2] == environments[3]


def test_bundle_smoke_main_runs_mock_then_default_live_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default smoke run proves the bundled profile and a default live start."""

    bundle_path = tmp_path / "Draftomen.exe"
    bundle_path.write_bytes(b"executable")
    commands: list[list[str]] = []
    live_defect: list[str | None] = [None]

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str] | None:
        command = kwargs["args"]
        assert isinstance(command, list)
        commands.append(command)
        provider = command[command.index("--provider") + 1]
        app_directory = Path(command[command.index("--app-dir") + 1])
        profile_cache_path = app_directory / "set-profiles" / "hob-quickdraft.json"
        assert app_directory.parent.is_dir()
        assert not profile_cache_path.exists()
        if provider == "mock":
            return None
        log_path = Path(command[command.index("--log-path") + 1])
        screenshot_path = Path(command[command.index("--screenshot") + 1])
        assert log_path.read_bytes() == b""
        if live_defect[0] == "player-log":
            log_path.write_bytes(b"bundle wrote to the player log")
        if live_defect[0] != "screenshot":
            screenshot_path.write_bytes(b"png")
        if live_defect[0] == "profile-cache":
            profile_cache_path.parent.mkdir(parents=True)
            profile_cache_path.write_bytes(b"unexpected cache entry")
        return _completed_process(command=command)

    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    assert bundle_smoke.main([str(bundle_path)]) == 0
    mock_command, live_command = commands
    mock_app_directory = Path(mock_command[mock_command.index("--app-dir") + 1])
    live_app_directory = Path(live_command[live_command.index("--app-dir") + 1])
    assert mock_command == [
        str(bundle_path.resolve()),
        "--provider",
        "mock",
        "--smoke-test",
        "--verify-bundled-profile",
        "--app-dir",
        str(mock_app_directory),
    ]
    assert live_command == [
        str(bundle_path.resolve()),
        "--provider",
        "live",
        "--offline-profiles",
        "--no-startup-scan",
        "--smoke-test",
        "--log-path",
        str(live_app_directory.parent / "Player.log"),
        "--app-dir",
        str(live_app_directory),
        "--screenshot",
        str(live_app_directory.parent / "live-smoke.png"),
    ]
    assert mock_app_directory != live_app_directory
    assert mock_app_directory.parent.name == "mock"
    assert live_app_directory.parent.name == "live"
    assert mock_app_directory.parent.parent == live_app_directory.parent.parent
    assert mock_app_directory.parent.parent.name.startswith("draftomen-bundle-smoke-")
    for command in commands:
        assert "--draftmancer-dir" not in command
        assert "--test-draft-smoke" not in command
        assert "--test-draft-server-url" not in command

    assert (
        bundle_smoke.TEST_DRAFT_SUMMARY_PREFIX == qt_gui.TEST_DRAFT_SMOKE_SUMMARY_PREFIX
    )

    for defect, error_fragment in (
        ("player-log", "wrote to the isolated player log"),
        ("profile-cache", "mutated the flat profile cache"),
        ("screenshot", "did not render a window screenshot"),
    ):
        live_defect[0] = defect
        with pytest.raises(RuntimeError, match=error_fragment):
            bundle_smoke.main([str(bundle_path)])


@pytest.mark.parametrize("provider", ["mock", "live"])
def test_bundle_smoke_main_rejects_flat_profile_cache_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """A successful bundle launch must not populate the flat profile cache."""

    bundle_path = tmp_path / "Draftomen.exe"
    bundle_path.write_bytes(b"executable")

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str] | None:
        command = kwargs["args"]
        assert isinstance(command, list)
        if command[command.index("--provider") + 1] != provider:
            return _completed_process(command=command)
        app_directory = Path(command[command.index("--app-dir") + 1])
        cache_path = app_directory / "set-profiles" / "hob-quickdraft.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_bytes(b"unexpected cache entry")
        if provider == "live":
            Path(command[command.index("--screenshot") + 1]).write_bytes(b"png")
        return _completed_process(command=command)

    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="mutated the flat profile cache"):
        bundle_smoke.main([str(bundle_path)])


@pytest.mark.parametrize(
    "flags",
    [
        ("--draftmancer-dir",),
        ("--scryfall-bulk-file",),
        ("--app-dir",),
        ("--draftmancer-dir", "--scryfall-bulk-file", "--app-dir"),
    ],
    ids=["draftmancer-dir", "scryfall-bulk-file", "app-dir", "every-journey-flag"],
)
def test_bundle_smoke_rejects_journey_flags_without_test_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
) -> None:
    """Journey-only flags are refused instead of running the default launches."""

    bundle_path = tmp_path / "Draftomen.exe"
    bundle_path.write_bytes(b"executable")
    command = [str(bundle_path)]
    for flag in flags:
        command.extend([flag, str(tmp_path / "journey-input")])

    monkeypatch.setattr(bundle_smoke.subprocess, "run", _forbid_process_launch)
    monkeypatch.setattr(bundle_smoke, "_probe_draftmancer_server", _forbid_server_probe)

    with pytest.raises(RuntimeError) as error:
        bundle_smoke.main(command)

    message = str(error.value)
    for flag in flags:
        assert flag in message
    assert message == (
        f"Invalid arguments: --test-draft is required for {', '.join(flags)}."
    )


@pytest.mark.parametrize(
    ("arguments", "error_fragment"),
    [
        (
            {"--scryfall-bulk-file": "scryfall", "--app-dir": "app"},
            "--draftmancer-dir",
        ),
        (
            {"--draftmancer-dir": "draftmancer", "--app-dir": "app"},
            "--scryfall-bulk-file",
        ),
        (
            {"--draftmancer-dir": "draftmancer", "--scryfall-bulk-file": "scryfall"},
            "--app-dir",
        ),
        (
            {
                "--draftmancer-dir": "draftmancer",
                "--scryfall-bulk-file": "scryfall",
                "--app-dir": "missing-app",
            },
            "--app-dir must be an existing directory",
        ),
    ],
    ids=[
        "without-draftmancer-dir",
        "without-scryfall-bulk-file",
        "without-app-dir",
        "missing-app-dir",
    ],
)
def test_bundle_smoke_test_draft_mode_requires_pinned_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, str],
    error_fragment: str,
) -> None:
    """Both Test Draft journeys refuse to run without their pinned local inputs."""

    bundle_path = tmp_path / "Draftomen.exe"
    bundle_path.write_bytes(b"executable")
    sources = {
        "draftmancer": tmp_path / "Draftmancer",
        "scryfall": tmp_path / "scryfall-default-cards.jsonl.gz",
        "app": tmp_path / "prepared-app",
        "missing-app": tmp_path / "missing-app",
    }
    sources["draftmancer"].mkdir()
    sources["scryfall"].write_bytes(b"bulk")
    sources["app"].mkdir()
    command = [str(bundle_path), "--test-draft"]
    for flag, source in arguments.items():
        command.extend([flag, str(sources[source])])

    monkeypatch.setattr(bundle_smoke.subprocess, "run", _forbid_process_launch)
    monkeypatch.setattr(bundle_smoke, "_probe_draftmancer_server", _forbid_server_probe)

    with pytest.raises(RuntimeError, match=error_fragment):
        bundle_smoke.main(command)


def test_bundle_smoke_test_draft_mode_probes_the_server_and_requires_the_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both Test Draft journeys probe the pinned server around the compiled runs."""

    bundle_path = tmp_path / "Draftomen.exe"
    bundle_path.write_bytes(b"executable")
    draftmancer_dir = tmp_path / "Draftmancer"
    draftmancer_dir.mkdir()
    scryfall_bulk_file = tmp_path / "scryfall-default-cards.jsonl.gz"
    scryfall_bulk_file.write_bytes(b"bulk")
    app_dir = tmp_path / "prepared-app"
    app_dir.mkdir()
    journey_arguments = [
        str(bundle_path),
        "--test-draft",
        "--draftmancer-dir",
        str(draftmancer_dir),
        "--scryfall-bulk-file",
        str(scryfall_bulk_file),
        "--app-dir",
        str(app_dir),
    ]
    events: list[str] = []
    commands: list[list[str]] = []
    timeouts: list[float] = []
    summaries: dict[str, dict[str, object]] = {
        "auto": {
            "status": "ok",
            "mode": "auto",
            "set_code": "hob",
            "picks": 42,
            "deck_size": 23,
            "selected_pair": "UB",
        },
        "manual": {
            "status": "ok",
            "mode": "manual",
            "set_code": "hob",
            "picks": 5,
            "pool_total": 5,
            "non_top_rank": 2,
        },
    }

    def fake_probe(*, server_url: str) -> None:
        assert server_url == bundle_smoke.DEFAULT_SERVER_URL
        events.append("probe")

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int)
        assert command[-2] == "--test-draft-smoke"
        log_path = Path(command[command.index("--log-path") + 1])
        assert log_path.is_file()
        assert log_path.read_bytes() == b""
        events.append("launch")
        commands.append(command)
        timeouts.append(float(timeout))
        stdout = (
            f"{bundle_smoke.TEST_DRAFT_SUMMARY_PREFIX}"
            f"{json.dumps(summaries[command[-1]])}\n"
        )
        return _completed_process(command=command, stdout=stdout)

    monkeypatch.setattr(bundle_smoke, "_probe_draftmancer_server", fake_probe)
    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    assert bundle_smoke.main(journey_arguments) == 0
    assert events == ["probe", "launch", "launch", "probe"]
    assert [command[-2:] for command in commands] == [
        ["--test-draft-smoke", "auto"],
        ["--test-draft-smoke", "manual"],
    ]
    assert len(timeouts) == 2
    assert all(
        timeout > qt_gui.TEST_DRAFT_SMOKE_TIMEOUT_SECONDS for timeout in timeouts
    )

    def without_journey_log_path(command: list[str]) -> list[str]:
        """Hide the per-journey log path so both vectors can be compared."""
        position = command.index("--log-path")
        return command[: position + 1] + command[position + 2 : -2]

    assert without_journey_log_path(commands[0]) == without_journey_log_path(commands[1])
    assert without_journey_log_path(commands[0]) == [
        str(bundle_path.resolve()),
        "--provider",
        "live",
        "--draftmancer-dir",
        str(draftmancer_dir),
        "--scryfall-bulk-file",
        str(scryfall_bulk_file),
        "--app-dir",
        str(app_dir),
        "--log-path",
        "--no-startup-scan",
        "--offline-profiles",
        "--test-draft-server-url",
        bundle_smoke.DEFAULT_SERVER_URL,
    ]
    log_paths = [
        Path(command[command.index("--log-path") + 1]) for command in commands
    ]
    assert [log_path.name for log_path in log_paths] == ["Player.log", "Player.log"]
    assert log_paths[0].parent != log_paths[1].parent
    assert log_paths[0].parent.parent == log_paths[1].parent.parent
    assert log_paths[0].parent.parent.name.startswith("draftomen-bundle-smoke-")

    output = capsys.readouterr().out
    assert output.count("\n") == 2
    assert output.endswith("\n")
    auto_line, manual_line = output.splitlines()
    auto_summary = json.loads(auto_line)
    manual_summary = json.loads(manual_line)
    assert set(auto_summary) == {
        "bundle",
        "journey",
        "status",
        "mode",
        "set_code",
        "picks",
        "deck_size",
        "selected_pair",
    }
    assert set(manual_summary) == {
        "bundle",
        "journey",
        "status",
        "mode",
        "set_code",
        "picks",
        "pool_total",
        "non_top_rank",
    }
    assert auto_summary == {
        **summaries["auto"],
        "bundle": str(bundle_path.resolve()),
        "journey": "auto",
    }
    assert manual_summary == {
        **summaries["manual"],
        "bundle": str(bundle_path.resolve()),
        "journey": "manual",
    }
    assert auto_line == json.dumps(
        auto_summary, separators=(",", ":"), sort_keys=True
    )
    assert manual_line == json.dumps(
        manual_summary, separators=(",", ":"), sort_keys=True
    )

    def fake_run_without_summary(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        return _completed_process(
            command=command, stdout="no Test Draft summary here\n"
        )

    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run_without_summary)

    with pytest.raises(RuntimeError, match=r"printed no summary line; exit code 0"):
        bundle_smoke.main(journey_arguments)


def test_bundle_smoke_test_draft_mode_resolves_journey_inputs_for_the_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative journey inputs reach both launches absolute from its own directory."""

    monkeypatch.chdir(tmp_path)
    (tmp_path / "Draftomen.exe").write_bytes(b"executable")
    (tmp_path / "Draftmancer").mkdir()
    (tmp_path / "scryfall-default-cards.jsonl.gz").write_bytes(b"bulk")
    (tmp_path / "prepared-app").mkdir()
    commands: list[list[str]] = []
    summaries: dict[str, dict[str, object]] = {
        "auto": {
            "status": "ok",
            "mode": "auto",
            "set_code": "hob",
            "picks": 42,
            "deck_size": 23,
            "selected_pair": "UB",
        },
        "manual": {
            "status": "ok",
            "mode": "manual",
            "set_code": "hob",
            "picks": 5,
            "pool_total": 5,
            "non_top_rank": 2,
        },
    }

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        commands.append(command)
        stdout = (
            f"{bundle_smoke.TEST_DRAFT_SUMMARY_PREFIX}"
            f"{json.dumps(summaries[command[-1]])}\n"
        )
        return _completed_process(command=command, stdout=stdout)

    def fake_probe(*, server_url: str) -> None:
        assert server_url == bundle_smoke.DEFAULT_SERVER_URL

    monkeypatch.setattr(bundle_smoke, "_probe_draftmancer_server", fake_probe)
    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    assert (
        bundle_smoke.main(
            [
                "Draftomen.exe",
                "--test-draft",
                "--draftmancer-dir",
                "Draftmancer",
                "--scryfall-bulk-file",
                "scryfall-default-cards.jsonl.gz",
                "--app-dir",
                "prepared-app",
            ]
        )
        == 0
    )

    assert [command[-1] for command in commands] == ["auto", "manual"]
    for command in commands:
        assert command[command.index("--draftmancer-dir") + 1] == str(
            (tmp_path / "Draftmancer").resolve()
        )
        assert command[command.index("--scryfall-bulk-file") + 1] == str(
            (tmp_path / "scryfall-default-cards.jsonl.gz").resolve()
        )
        assert command[command.index("--app-dir") + 1] == str(
            (tmp_path / "prepared-app").resolve()
        )


def test_bundle_smoke_test_draft_mode_rejects_a_dead_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned server that stops answering fails after both journeys."""

    dead_server_url = "http://127.0.0.1:1"
    with pytest.raises(RuntimeError) as dead_probe:
        bundle_smoke._probe_draftmancer_server(server_url=dead_server_url)
    assert dead_server_url in str(dead_probe.value)

    journey_arguments = _prepare_test_draft_journey(
        root=tmp_path, monkeypatch=monkeypatch
    )
    probes: list[str] = []
    launches: list[str] = []
    summaries: dict[str, dict[str, object]] = {
        "auto": {
            "status": "ok",
            "mode": "auto",
            "set_code": "hob",
            "picks": 42,
            "deck_size": 23,
            "selected_pair": "UB",
        },
        "manual": {
            "status": "ok",
            "mode": "manual",
            "set_code": "hob",
            "picks": 5,
            "pool_total": 5,
            "non_top_rank": 2,
        },
    }

    def fake_probe(*, server_url: str) -> None:
        probes.append(server_url)
        if len(probes) > 1:
            raise RuntimeError(f"Draftmancer server probe failed for {server_url}")

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        launches.append(command[-1])
        stdout = (
            f"{bundle_smoke.TEST_DRAFT_SUMMARY_PREFIX}"
            f"{json.dumps(summaries[command[-1]])}\n"
        )
        return _completed_process(command=command, stdout=stdout)

    monkeypatch.setattr(bundle_smoke, "_probe_draftmancer_server", fake_probe)
    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as failure:
        bundle_smoke.main(journey_arguments)

    assert probes == [bundle_smoke.DEFAULT_SERVER_URL, bundle_smoke.DEFAULT_SERVER_URL]
    assert launches == ["auto", "manual"]
    assert bundle_smoke.DEFAULT_SERVER_URL in str(failure.value)


@pytest.mark.parametrize(
    ("manual_summary", "error_fragment"),
    [
        (
            {
                "status": "ok",
                "mode": "auto",
                "set_code": "hob",
                "picks": bundle_smoke.REQUIRED_MANUAL_PICKS,
                "pool_total": bundle_smoke.REQUIRED_MANUAL_PICKS,
                "non_top_rank": 2,
            },
            "reported mode 'auto' for the manual journey",
        ),
        (None, "printed no summary line"),
        (
            {
                "status": "ok",
                "mode": "manual",
                "set_code": "hob",
                "picks": bundle_smoke.REQUIRED_MANUAL_PICKS - 1,
                "pool_total": bundle_smoke.REQUIRED_MANUAL_PICKS - 1,
                "non_top_rank": 2,
            },
            "unusable manual summary line",
        ),
        (
            {
                "status": "ok",
                "mode": "manual",
                "set_code": "hob",
                "picks": bundle_smoke.REQUIRED_MANUAL_PICKS,
                "pool_total": bundle_smoke.REQUIRED_MANUAL_PICKS,
                "non_top_rank": 1,
            },
            "unusable manual summary line",
        ),
    ],
    ids=[
        "wrong-journey-mode",
        "without-summary-line",
        "below-required-picks",
        "without-non-top-rank",
    ],
)
def test_bundle_smoke_test_draft_mode_rejects_unusable_manual_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manual_summary: dict[str, object] | None,
    error_fragment: str,
) -> None:
    """The Manual journey rejects every summary its requirements do not accept."""

    journey_arguments = _prepare_test_draft_journey(
        root=tmp_path, monkeypatch=monkeypatch
    )
    launches: list[str] = []
    auto_summary = {
        "status": "ok",
        "mode": "auto",
        "set_code": "hob",
        "picks": 42,
        "deck_size": 23,
        "selected_pair": "UB",
    }

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        launches.append(command[-1])
        summary = auto_summary if command[-1] == "auto" else manual_summary
        stdout = (
            ""
            if summary is None
            else f"{bundle_smoke.TEST_DRAFT_SUMMARY_PREFIX}{json.dumps(summary)}\n"
        )
        return _completed_process(command=command, stdout=stdout)

    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=re.escape(error_fragment)):
        bundle_smoke.main(journey_arguments)

    assert launches == ["auto", "manual"]


def test_bundle_smoke_test_draft_mode_stops_after_a_failed_auto_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed first journey aborts the run before the Manual launch."""

    journey_arguments = _prepare_test_draft_journey(
        root=tmp_path, monkeypatch=monkeypatch
    )
    launches: list[str] = []

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        launches.append(command[-1])
        return _completed_process(command=command, returncode=1)

    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="failed with exit code 1"):
        bundle_smoke.main(journey_arguments)

    assert launches == ["auto"]


def test_macos_bundle_resolution_uses_plist_executable(tmp_path: Path) -> None:
    """The plist target wins when an app contains neighboring binaries."""

    bundle_path = _create_macos_bundle(
        root=tmp_path,
        metadata={"CFBundleExecutable": "qt_gui"},
    )
    executable_directory = bundle_path / "Contents" / "MacOS"
    (executable_directory / "libpython3.12.dylib").write_bytes(b"library")
    (executable_directory / "helper").write_bytes(b"neighbor")

    assert _resolve_bundle_executable(bundle_path=bundle_path) == (
        executable_directory / "qt_gui"
    )


@pytest.mark.parametrize(
    ("metadata", "error_fragment"),
    [
        ({}, "CFBundleExecutable"),
        ({"CFBundleExecutable": ""}, "CFBundleExecutable"),
        ({"CFBundleExecutable": "   "}, "CFBundleExecutable"),
        ({"CFBundleExecutable": 42}, "CFBundleExecutable"),
        ([], "expected a dictionary"),
    ],
)
def test_macos_bundle_resolution_rejects_invalid_metadata(
    tmp_path: Path,
    metadata: object,
    error_fragment: str,
) -> None:
    """Malformed executable metadata fails instead of guessing a file."""

    bundle_path = _create_macos_bundle(root=tmp_path, metadata=metadata)

    with pytest.raises(RuntimeError, match=error_fragment):
        _resolve_bundle_executable(bundle_path=bundle_path)


def test_macos_bundle_resolution_rejects_missing_metadata(tmp_path: Path) -> None:
    """An app without Info.plist cannot be resolved."""

    bundle_path = tmp_path / "Draftomen.app"
    (bundle_path / "Contents" / "MacOS").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="Missing macOS bundle metadata"):
        _resolve_bundle_executable(bundle_path=bundle_path)


def test_macos_bundle_resolution_rejects_missing_executable(tmp_path: Path) -> None:
    """A valid plist target must exist as a regular file."""

    bundle_path = _create_macos_bundle(
        root=tmp_path,
        metadata={"CFBundleExecutable": "qt_gui"},
        executable_name=None,
    )

    with pytest.raises(RuntimeError, match="does not exist as a regular file"):
        _resolve_bundle_executable(bundle_path=bundle_path)


def test_macos_bundle_resolution_rejects_malformed_plist(tmp_path: Path) -> None:
    """An unreadable Info.plist cannot supply an executable name."""

    bundle_path = tmp_path / "Draftomen.app"
    contents_directory = bundle_path / "Contents"
    (contents_directory / "MacOS").mkdir(parents=True)
    (contents_directory / "Info.plist").write_bytes(b"not a plist")

    with pytest.raises(RuntimeError, match="Could not read macOS bundle metadata"):
        _resolve_bundle_executable(bundle_path=bundle_path)


def test_windows_bundle_resolution_uses_exe_directly(tmp_path: Path) -> None:
    """Windows bundles continue to resolve their direct executable path."""

    executable = tmp_path / "Draftomen.EXE"
    executable.write_bytes(b"executable")
    (tmp_path / "Draftomen.dll").write_bytes(b"library")

    assert _resolve_bundle_executable(bundle_path=executable) == executable


def test_native_specs_preserve_project_metadata() -> None:
    """Nuitka metadata stays aligned with the package version and branding."""

    project = _read_project_metadata()
    project_version = project["version"]
    assert project_version == "0.4.1"
    expected_common_args = {
        "--company-name=Draft Omen",
        f"--product-name={EXPECTED_PRODUCT_NAME}",
        f"--file-version={project_version}",
        f"--product-version={project_version}",
    }

    for platform, spec_path in SPEC_PATHS.items():
        spec = _read_spec(path=spec_path)
        nuitka_args = set(shlex.split(spec["nuitka"]["extra_args"]))
        assert expected_common_args <= nuitka_args
        if platform == "macos":
            assert {
                f"--macos-app-name={EXPECTED_PRODUCT_NAME}",
                f"--macos-app-version={project_version}",
                f"--macos-signed-app-name={EXPECTED_BUNDLE_IDENTIFIER}",
            } <= nuitka_args
        else:
            assert "--assume-yes-for-downloads" not in nuitka_args
            assert (
                "--file-description=An unofficial Quick Draft assistant for MTG Arena"
                in nuitka_args
            )


def test_native_builds_sync_the_locked_draftmancer_transport_and_socketio() -> None:
    """Native build inputs carry the required transport the worker imports."""

    workflow_text = (PROJECT_ROOT / ".github/workflows/native-bundles.yml").read_text(
        encoding="utf-8"
    )
    workflow_sections = workflow_text.split("\n  publish-development:", maxsplit=1)
    assert len(workflow_sections) == 2
    build_job_text = workflow_sections[0]
    assert "--extra" not in build_job_text
    sync_lines = [
        line.strip()
        for line in build_job_text.splitlines()
        if line.strip().startswith("run: uv sync")
    ]
    assert sync_lines == ["run: uv sync --locked"]

    run_lines = [
        line.strip() for line in build_job_text.splitlines() if "uv run" in line
    ]
    smoke_lines = [line for line in run_lines if "tests/bundle_smoke.py" in line]
    build_lines = [line for line in run_lines if "tests/bundle_smoke.py" not in line]
    assert len(smoke_lines) == 2
    assert build_lines

    project_metadata = _read_project_metadata()
    base_dependencies = project_metadata["dependencies"]
    assert isinstance(base_dependencies, list)
    base_dependency_names = {_requirement_name(item) for item in base_dependencies}
    assert "python-socketio" in base_dependency_names
    optional_dependencies = project_metadata.get("optional-dependencies", {})
    assert isinstance(optional_dependencies, dict)
    assert "draftmancer" not in optional_dependencies

    with (PROJECT_ROOT / "uv.lock").open(mode="rb") as lock_file:
        locked_packages = tomllib.load(lock_file)["package"]
    project_package = next(
        package for package in locked_packages if package["name"] == "draftomen"
    )
    locked_base_dependencies = {
        entry["name"] for entry in project_package["dependencies"]
    }
    assert "python-socketio" in locked_base_dependencies
    assert "optional-dependencies" not in project_package

    for spec_path in SPEC_PATHS.values():
        nuitka_args = shlex.split(_read_spec(path=spec_path)["nuitka"]["extra_args"])
        assert "--include-package=socketio" in nuitka_args
        assert BASELINE_PROFILE_MAPPING in nuitka_args
        assert {"--quiet", "--noinclude-qt-translations"} <= set(nuitka_args)


def _read_native_bundle_matrix() -> list[dict[str, str]]:
    workflow_text = (PROJECT_ROOT / ".github/workflows/native-bundles.yml").read_text(
        encoding="utf-8"
    )
    matrix_text = workflow_text.split("        include:\n", maxsplit=1)[1]
    matrix_text = matrix_text.split("\n    steps:", maxsplit=1)[0]
    entries: list[dict[str, str]] = []
    for line in matrix_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            entries.append({})
            stripped = stripped[2:]
        key, value = stripped.split(":", maxsplit=1)
        entries[-1][key.strip()] = value.strip()
    return entries


def test_native_bundle_matrix_builds_both_macos_architectures() -> None:
    """Each macOS architecture builds from the shared spec on a matching runner
    and publishes a DMG named after its architecture.
    """

    macos_entries = {
        entry["arch"]: entry
        for entry in _read_native_bundle_matrix()
        if entry["platform"] == "macos"
    }

    assert set(macos_entries) == {"arm64", "x86_64"}
    assert macos_entries["arm64"]["os"] == "macos-latest"
    assert macos_entries["x86_64"]["os"] == "macos-15-intel"
    for arch, entry in macos_entries.items():
        assert entry["config"] == "pysidedeploy.macos.spec"
        assert entry["bundle"] == "Draftomen-unsigned-macos.app"
        assert entry["artifact"] == f"Draftomen-unsigned-macos-{arch}.dmg"
        assert entry["artifact_name"] == f"draftomen-macos-{arch}-unsigned-development"


def test_native_bundle_workflow_checks_macos_executable_architecture() -> None:
    """The macOS build fails when lipo reports an architecture other than the
    matrix entry's architecture.
    """

    workflow_text = (PROJECT_ROOT / ".github/workflows/native-bundles.yml").read_text(
        encoding="utf-8"
    )
    check_step = workflow_text.split("- name: Verify macOS bundle architecture", maxsplit=1)[1]
    check_step = check_step.split("\n      - name:", maxsplit=1)[0]

    assert "if: matrix.platform == 'macos'" in check_step
    assert "Print :CFBundleExecutable" in check_step
    assert 'lipo -archs "$bundle/Contents/MacOS/$executable"' in check_step
    assert '[[ "$archs" != "${{ matrix.arch }}" ]]' in check_step
    assert "exit 1" in check_step
    assert workflow_text.index("Verify macOS bundle architecture") < workflow_text.index(
        "Create macOS DMG"
    )


@pytest.mark.parametrize(
    ("workflow_name", "name_variable", "macos_artifact_suffix", "signed"),
    [
        ("native-bundles.yml", "BUILD_IDENTIFIER", "unsigned-development", False),
        ("release.yml", "RELEASE_TAG", "signed-release", True),
    ],
)
def test_release_workflows_publish_both_macos_dmgs(
    workflow_name: str, name_variable: str, macos_artifact_suffix: str, signed: bool
) -> None:
    """Development and tagged releases upload both DMGs, the Windows executable,
    and a checksum file that lists all three binaries. Only tag releases drop
    `unsigned` from the macOS and checksum names.
    """

    workflow_text = (PROJECT_ROOT / ".github/workflows" / workflow_name).read_text(
        encoding="utf-8"
    )
    prefix = f"draftomen-${{{name_variable}}}"
    signed_prefix = prefix if signed else f"{prefix}-unsigned"

    for arch in ("arm64", "x86_64"):
        assert f"name: draftomen-macos-{arch}-{macos_artifact_suffix}" in workflow_text
        assert f"{signed_prefix}-macos-{arch}.dmg" in workflow_text
    assert f"{prefix}-unsigned-windows.exe" in workflow_text
    assert f"{signed_prefix}-sha256sums.txt" in workflow_text
    assert "unsigned-macos.dmg" not in workflow_text
    if signed:
        assert "-unsigned-macos-" not in workflow_text
        assert "-unsigned-sha256sums" not in workflow_text
    assert (
        'sha256sum "${macos_arm64_name}" "${macos_x86_64_name}" "${windows_name}"'
        in workflow_text
    )

    upload_command = workflow_text.split("gh release upload", maxsplit=1)[1]
    upload_command = upload_command.split("\n\n", maxsplit=1)[0]
    uploaded_assets = re.findall(r'"release-assets/published/([^"]+)"', upload_command)
    assert len(uploaded_assets) == 4


def _workflow_step(workflow_text: str, name: str) -> str:
    step = workflow_text.split(f"- name: {name}\n", maxsplit=1)[1]
    return step.split("\n      - name:", maxsplit=1)[0]


def test_only_release_macos_jobs_use_signing_environment() -> None:
    """The tag release asks for signing, and only macOS jobs with that input
    enter the macos-release environment.
    """

    release_text = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    native_text = (PROJECT_ROOT / ".github/workflows/native-bundles.yml").read_text(
        encoding="utf-8"
    )
    signed_condition = "inputs.sign_macos && matrix.platform == 'macos'"

    assert "    with:\n      sign_macos: true\n" in release_text
    assert "    secrets: inherit\n" in release_text
    assert "default: false" in native_text.split("  workflow_dispatch:", maxsplit=1)[0]
    assert (
        f"environment: ${{{{ {signed_condition} && 'macos-release' || '' }}}}"
        in native_text
    )
    assert f"MACOS_SIGNED: ${{{{ {signed_condition} }}}}" in native_text
    publish_job = native_text.split("\n  publish-development:", maxsplit=1)[1]
    assert "secrets." not in publish_job
    assert "vars." not in publish_job


def test_signed_macos_build_keeps_data_file_signatures() -> None:
    """The signed build verifies Nuitka's app strictly and copies it with ditto
    instead of using pyside6-deploy's copy, which drops extended attributes.
    """

    workflow_text = (PROJECT_ROOT / ".github/workflows/native-bundles.yml").read_text(
        encoding="utf-8"
    )
    build_step = _workflow_step(workflow_text=workflow_text, name="Build signed macOS bundle")

    assert "if: env.MACOS_SIGNED == 'true'" in build_step
    assert "--macos-sign-identity=$MACOS_SIGNING_IDENTITY --macos-sign-notarization" in build_step
    assert "--keep-deployment-files" in build_step
    verify = 'codesign --verify --deep --strict --verbose=2 "$nuitka_app"'
    copy = 'ditto "$nuitka_app" "$BUNDLE_DIRECTORY/$BUNDLE"'
    assert build_step.index(verify) < build_step.index(copy)

    dmg_step = _workflow_step(workflow_text=workflow_text, name="Create macOS DMG")
    assert 'ditto "$BUNDLE_DIRECTORY/$BUNDLE" "$staging/$BUNDLE"' in dmg_step
    assert "mv " not in dmg_step


def test_signed_macos_dmg_is_notarized_before_smoke_test_and_upload() -> None:
    """The DMG is signed, notarized and stapled before the mounted smoke test,
    and the upload publishes that same file.
    """

    workflow_text = (PROJECT_ROOT / ".github/workflows/native-bundles.yml").read_text(
        encoding="utf-8"
    )
    notarize_step = _workflow_step(
        workflow_text=workflow_text, name="Sign, notarize and staple macOS DMG"
    )
    smoke_step = _workflow_step(
        workflow_text=workflow_text,
        name="Smoke-test mounted macOS DMG with deterministic mock data",
    )
    upload_step = _workflow_step(workflow_text=workflow_text, name="Upload bundle artifact")

    for command in (
        "xcrun notarytool submit",
        'xcrun stapler staple "$dmg"',
        'xcrun stapler validate "$dmg"',
        "spctl --assess --type open --context context:primary-signature",
    ):
        assert command in notarize_step
    assert '"$status" != "Accepted"' in notarize_step
    assert 'spctl --assess --type execute --verbose=2 "$mountpoint/$BUNDLE"' in smoke_step
    assert '"$BUNDLE_DIRECTORY/$ARTIFACT"' in smoke_step
    assert "path: ${{ env.BUNDLE_DIRECTORY }}/${{ env.ARTIFACT }}" in upload_step
    assert (
        workflow_text.index("Sign, notarize and staple macOS DMG")
        < workflow_text.index("Smoke-test mounted macOS DMG")
        < workflow_text.index("Upload bundle artifact")
    )


def test_signing_credentials_are_removed_even_after_failure() -> None:
    """An always-run step deletes the temporary keychain and every decoded
    credential file.
    """

    workflow_text = (PROJECT_ROOT / ".github/workflows/native-bundles.yml").read_text(
        encoding="utf-8"
    )
    cleanup_step = _workflow_step(
        workflow_text=workflow_text,
        name="Remove temporary signing keychain and credentials",
    )

    assert "if: always() && env.MACOS_SIGNED == 'true'" in cleanup_step
    assert 'security delete-keychain "$keychain"' in cleanup_step
    for credential_file in ("draftomen-signing.p12", "draftomen-notary.p8"):
        assert credential_file in cleanup_step
        assert credential_file in workflow_text.split(cleanup_step, maxsplit=1)[0]
    assert workflow_text.index(cleanup_step) > workflow_text.index("Upload bundle artifact")


def test_native_specs_enumerate_runtime_inputs() -> None:
    """Both platform specs describe the same app inputs and unsigned outputs."""

    with (PROJECT_ROOT / "pyproject.toml").open(mode="rb") as project_file:
        project_files = tomllib.load(project_file)["tool"]["pyside6-project"]["files"]
    expected_qml_files = {
        path for path in project_files if path.startswith("draftomen/qml/")
    }

    for platform, spec_path in SPEC_PATHS.items():
        spec = _read_spec(path=spec_path)
        app = spec["app"]
        python = spec["python"]
        qt = spec["qt"]
        nuitka = spec["nuitka"]

        assert app["title"] == f"Draftomen-unsigned-{platform}"
        assert "unsigned" in app["exec_directory"]
        assert (PROJECT_ROOT / app["icon"]).is_file()
        assert app["project_file"] == "pyproject.toml"
        assert python["packages"] == "Nuitka==4.1.3"
        # Interpreter selection stays with the invoking uv environment; a build must
        # never commit a machine-specific path that a --force run would rewrite.
        assert python["python_path"] == ""
        assert set(qt["qml_files"].split(",")) == expected_qml_files
        assert qt["modules"].split(",") == [
            "Core",
            "Gui",
            "Qml",
            "Quick",
            "QuickControls2",
        ]
        assert set(qt["excluded_qml_plugins"].split(",")) == EXPECTED_EXCLUDED_QML_PLUGINS
        assert set(qt["plugins"].split(",")) == {
            "imageformats",
            "platforms",
            "platformthemes",
            "styles",
        }
        assert nuitka["mode"] == "onefile"


def test_project_metadata_includes_logo_and_no_fonts() -> None:
    """The pyside6-project input list names existing assets."""

    with (PROJECT_ROOT / "pyproject.toml").open(mode="rb") as project_file:
        project_files = tomllib.load(project_file)["tool"]["pyside6-project"]["files"]

    assert "draftomen/assets/draftomen_logo.png" in project_files
    assert not any("font" in path.lower() for path in project_files)
    for path in project_files:
        assert (PROJECT_ROOT / path).is_file(), path

    assert (PROJECT_ROOT / "draftomen/assets/draftomen.icns").read_bytes()[:4] == b"icns"
    assert (PROJECT_ROOT / "draftomen/assets/draftomen.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_bundled_profile_provenance_and_packaging_are_deterministic(tmp_path: Path) -> None:
    """The selected baseline retains deterministic provenance and packaging
    identity across source metadata and native deployment inputs.
    """

    generation_path = SELECTED_GZIP_PATH.parent / "generation.json"
    source_path = SELECTED_GZIP_PATH.parent / "source.json"
    generation_bytes = generation_path.read_bytes()
    generation = json.loads(generation_bytes)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    assert len(source_bytes) == SELECTED_SOURCE_BYTES
    assert hashlib.sha256(source_bytes).hexdigest() == SELECTED_SOURCE_SHA256
    generation_checksums = generation["checksums"]
    source_artifacts = source["artifacts"]
    source_gzip = source_artifacts["gzip"]
    source_profile = source_artifacts["profile"]
    source_generation = source_artifacts["generation"]
    source_environment = source["batch"]["document"]["environments"][0]

    assert SELECTED_GZIP_PATH.is_file()
    assert SELECTED_GZIP_PATH.parent.parent.name == "hob-quickdraft"
    assert SELECTED_GZIP_PATH.parent.name == SELECTED_GZIP_SHA256
    assert SELECTED_GZIP_PATH.name == f"{SELECTED_GZIP_SHA256}.json.gz"
    assert len(generation_bytes) == source["generation_bytes"] == source_generation["bytes"]
    assert hashlib.sha256(generation_bytes).hexdigest() == source["generation_sha256"] == source_generation["sha256"]

    gzip_bytes = SELECTED_GZIP_PATH.read_bytes()
    assert SELECTED_GZIP_PATH.stat().st_size == generation["gzip_bytes"] == source_gzip["bytes"]
    assert hashlib.sha256(gzip_bytes).hexdigest() == SELECTED_GZIP_SHA256
    assert generation["gzip_sha256"] == generation_checksums["gzip"] == source_gzip["sha256"]
    assert source_environment["artifacts"]["gzip"] == source_gzip

    profile_bytes = gzip.decompress(gzip_bytes)
    profile_document = json.loads(profile_bytes)
    assert len(profile_bytes) == generation["profile_bytes"] == source_profile["bytes"]
    assert (
        hashlib.sha256(profile_bytes).hexdigest()
        == generation["profile_sha256"]
        == generation_checksums["profile"]
        == source_profile["sha256"]
    )
    assert source_environment["artifacts"]["profile"] == source_profile
    assert profile_bytes == BASELINE_PROFILE_PATH.read_bytes()

    assert profile_document["set_code"] == generation["set_code"] == "hob"
    assert profile_document["format"] == generation["event_format"] == "quickdraft"
    assert profile_document["generated_at"] == generation["generated_at"]
    assert profile_document["maturity"] == "metadata-only"
    identity = source["identity"]
    assert identity["canonical_set_code"] == profile_document["set_code"]
    assert identity["canonical_event_format"] == profile_document["format"]
    assert identity["external_set_code"] == "HOB"
    assert identity["external_event_format"] == "QuickDraft"

    raw_profile_path = tmp_path / "hob-quickdraft.json"
    raw_profile_path.write_bytes(profile_bytes)
    profile = load_set_profile(
        raw_profile_path,
        expected_set_code="hob",
        expected_format="quickdraft",
    )
    assert profile.set_code == "hob"
    assert profile.event_format == "quickdraft"
    assert profile.to_bytes() == profile_bytes

    with (PROJECT_ROOT / "pyproject.toml").open(mode="rb") as project_file:
        project_config = tomllib.load(project_file)
    project_tools = project_config["tool"]
    pyside_files = project_tools["pyside6-project"]["files"]
    assert BASELINE_PROFILE_RESOURCE in pyside_files
    package_data_entries = [
        entry
        for entries in project_tools["setuptools"]["package-data"].values()
        for entry in entries
    ]
    assert not any("baseline_profiles" in entry for entry in package_data_entries)

    spec_mappings = {
        platform: tuple(
            argument
            for argument in shlex.split(_read_spec(path=spec_path)["nuitka"]["extra_args"])
            if argument.startswith("--include-data-files=")
        )
        for platform, spec_path in SPEC_PATHS.items()
    }
    assert spec_mappings == {
        "macos": (BASELINE_PROFILE_MAPPING,),
        "windows": (BASELINE_PROFILE_MAPPING,),
    }
