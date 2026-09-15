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
    "QtTest",
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
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=command, returncode=0, stdout=stdout, stderr=""
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
    """The manual journey refuses to run without its pinned local inputs."""

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
    """The manual journey probes the pinned server around the compiled run."""

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
    summary = {
        "status": "ok",
        "set_code": "hob",
        "picks": 42,
        "deck_size": 23,
        "selected_pair": "UB",
    }

    def fake_probe(*, server_url: str) -> None:
        assert server_url == bundle_smoke.DEFAULT_SERVER_URL
        events.append("probe")

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int)
        log_path = Path(command[command.index("--log-path") + 1])
        assert log_path.is_file()
        assert log_path.read_bytes() == b""
        events.append("launch")
        commands.append(command)
        timeouts.append(float(timeout))
        stdout = f"{bundle_smoke.TEST_DRAFT_SUMMARY_PREFIX}{json.dumps(summary)}\n"
        return _completed_process(command=command, stdout=stdout)

    monkeypatch.setattr(bundle_smoke, "_probe_draftmancer_server", fake_probe)
    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    assert bundle_smoke.main(journey_arguments) == 0
    assert events == ["probe", "launch", "probe"]
    assert len(commands) == 1
    command = commands[0]
    log_path = Path(command[command.index("--log-path") + 1])
    assert log_path.name == "Player.log"
    assert log_path.parent.name.startswith("draftomen-bundle-smoke-")
    assert command == [
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
        str(log_path),
        "--no-startup-scan",
        "--offline-profiles",
        "--test-draft-server-url",
        bundle_smoke.DEFAULT_SERVER_URL,
        "--test-draft-smoke",
    ]
    assert timeouts[0] > qt_gui.TEST_DRAFT_SMOKE_TIMEOUT_SECONDS
    assert capsys.readouterr().out == (
        json.dumps(
            {
                "status": "ok",
                "bundle": str(bundle_path.resolve()),
                "set_code": "hob",
                "picks": 42,
                "deck_size": 23,
                "selected_pair": "UB",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
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
    """Relative journey inputs reach the bundle absolute from its own directory."""

    monkeypatch.chdir(tmp_path)
    (tmp_path / "Draftomen.exe").write_bytes(b"executable")
    (tmp_path / "Draftmancer").mkdir()
    (tmp_path / "scryfall-default-cards.jsonl.gz").write_bytes(b"bulk")
    (tmp_path / "prepared-app").mkdir()
    commands: list[list[str]] = []
    summary = {
        "status": "ok",
        "set_code": "hob",
        "picks": 42,
        "deck_size": 23,
        "selected_pair": "UB",
    }

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        commands.append(command)
        stdout = f"{bundle_smoke.TEST_DRAFT_SUMMARY_PREFIX}{json.dumps(summary)}\n"
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

    command = commands[0]
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
    """A pinned server that stops answering fails the manual journey."""

    dead_server_url = "http://127.0.0.1:1"
    with pytest.raises(RuntimeError) as dead_probe:
        bundle_smoke._probe_draftmancer_server(server_url=dead_server_url)
    assert dead_server_url in str(dead_probe.value)

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
    probes: list[str] = []
    summary = {
        "status": "ok",
        "set_code": "hob",
        "picks": 42,
        "deck_size": 23,
        "selected_pair": "UB",
    }

    def fake_probe(*, server_url: str) -> None:
        probes.append(server_url)
        if len(probes) > 1:
            raise RuntimeError(f"Draftmancer server probe failed for {server_url}")

    def fake_run(**kwargs: object) -> subprocess.CompletedProcess[str]:
        command = kwargs["args"]
        assert isinstance(command, list)
        stdout = f"{bundle_smoke.TEST_DRAFT_SUMMARY_PREFIX}{json.dumps(summary)}\n"
        return _completed_process(command=command, stdout=stdout)

    monkeypatch.setattr(bundle_smoke, "_probe_draftmancer_server", fake_probe)
    monkeypatch.setattr(bundle_smoke.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as failure:
        bundle_smoke.main(journey_arguments)

    assert probes == [bundle_smoke.DEFAULT_SERVER_URL, bundle_smoke.DEFAULT_SERVER_URL]
    assert bundle_smoke.DEFAULT_SERVER_URL in str(failure.value)


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
    assert project_version == "0.3.1"
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


def test_native_builds_sync_the_locked_draftmancer_extra_and_include_socketio() -> None:
    """Native build inputs install the optional transport the worker imports."""

    workflow_text = (PROJECT_ROOT / ".github/workflows/native-bundles.yml").read_text(
        encoding="utf-8"
    )
    workflow_sections = workflow_text.split("\n  publish-development:", maxsplit=1)
    assert len(workflow_sections) == 2
    build_job_text = workflow_sections[0]
    assert "uv sync --locked --extra draftmancer" in build_job_text

    run_lines = [
        line.strip() for line in build_job_text.splitlines() if "uv run" in line
    ]
    smoke_lines = [line for line in run_lines if "tests/bundle_smoke.py" in line]
    build_lines = [line for line in run_lines if "tests/bundle_smoke.py" not in line]
    assert len(smoke_lines) == 2
    assert all("--extra draftmancer" not in line for line in smoke_lines)
    assert build_lines
    assert all("--extra draftmancer" in line for line in build_lines)

    project_metadata = _read_project_metadata()
    base_dependencies = project_metadata["dependencies"]
    assert isinstance(base_dependencies, list)
    base_dependency_names = {_requirement_name(item) for item in base_dependencies}
    assert not any(
        "socketio" in name or "engineio" in name for name in base_dependency_names
    )
    optional_dependencies = project_metadata["optional-dependencies"]
    assert isinstance(optional_dependencies, dict)
    assert optional_dependencies["draftmancer"] == [
        "python-socketio[client]>=5.16.4,<6"
    ]

    with (PROJECT_ROOT / "uv.lock").open(mode="rb") as lock_file:
        locked_packages = tomllib.load(lock_file)["package"]
    project_package = next(
        package for package in locked_packages if package["name"] == "draftomen"
    )
    locked_extra = project_package["optional-dependencies"]["draftmancer"]
    assert [entry["name"] for entry in locked_extra] == ["python-socketio"]

    for spec_path in SPEC_PATHS.values():
        nuitka_args = shlex.split(_read_spec(path=spec_path)["nuitka"]["extra_args"])
        assert "--include-package=socketio" in nuitka_args
        assert BASELINE_PROFILE_MAPPING in nuitka_args
        assert {"--quiet", "--noinclude-qt-translations"} <= set(nuitka_args)


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


def test_project_metadata_includes_package_sources_logo_and_no_fonts() -> None:
    """The pyside6-project input list covers package sources and assets."""

    with (PROJECT_ROOT / "pyproject.toml").open(mode="rb") as project_file:
        project_files = tomllib.load(project_file)["tool"]["pyside6-project"]["files"]

    declared_python_files = {
        path
        for path in project_files
        if Path(path).parent == Path("draftomen") and Path(path).suffix == ".py"
    }
    actual_python_files = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "draftomen").glob("*.py")
    }
    assert declared_python_files == actual_python_files

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
