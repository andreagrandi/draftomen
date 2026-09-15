"""Smoke-test a compiled Draftomen bundle with mock, live, and Test Draft runs.
The default mode launches the bundle twice in deterministic mock and default live
configurations; --test-draft drives one manual Test Draft journey against a pinned
Draftmancer server. This helper intentionally imports only the standard library so
the target bundle is what supplies the application and PySide6 runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

DEFAULT_PROCESS_TIMEOUT_SECONDS = 60
DEFAULT_TEST_DRAFT_PROCESS_TIMEOUT_SECONDS = 1200
DEFAULT_SERVER_URL = "http://127.0.0.1:3000"
TEST_DRAFT_SUMMARY_PREFIX = "Test Draft smoke: "


def build_parser() -> argparse.ArgumentParser:
    """Build the native bundle smoke-test argument parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Launch a compiled Draftomen bundle in deterministic mock mode and then "
            "as a default live start, or run the opt-in manual Test Draft journey."
        ),
    )
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "Maximum seconds to wait for each compiled launch "
            f"(defaults to {DEFAULT_PROCESS_TIMEOUT_SECONDS} seconds, or "
            f"{DEFAULT_TEST_DRAFT_PROCESS_TIMEOUT_SECONDS} seconds with --test-draft)."
        ),
    )
    parser.add_argument(
        "--test-draft",
        action="store_true",
        help=(
            "Run the manual real-server Test Draft journey against a pinned "
            "Draftmancer checkout and its running server; never part of CI."
        ),
    )
    parser.add_argument(
        "--draftmancer-dir",
        type=Path,
        default=None,
        help="Pinned Draftmancer checkout enabling the developer Test Draft.",
    )
    parser.add_argument(
        "--scryfall-bulk-file",
        type=Path,
        default=None,
        help="Scryfall JSONL bulk file the Test Draft journey resolves printings from.",
    )
    parser.add_argument(
        "--app-dir",
        type=Path,
        default=None,
        help="Prepared application directory the Test Draft journey may mutate.",
    )
    parser.add_argument(
        "--server-url",
        default=DEFAULT_SERVER_URL,
        help="Draftmancer server the manual Test Draft journey connects to.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested bundle smoke tests and return a process-style exit code."""

    args = build_parser().parse_args(args=argv)
    journey_only_flags = [
        flag
        for flag, value in (
            ("--draftmancer-dir", args.draftmancer_dir),
            ("--scryfall-bulk-file", args.scryfall_bulk_file),
            ("--app-dir", args.app_dir),
        )
        if value is not None
    ]
    if journey_only_flags and not args.test_draft:
        raise RuntimeError(
            "Invalid arguments: --test-draft is required for "
            f"{', '.join(journey_only_flags)}."
        )
    bundle_path = args.bundle.resolve()
    executable = _resolve_bundle_executable(bundle_path=bundle_path)
    timeout = (
        args.timeout
        if args.timeout is not None
        else (
            DEFAULT_TEST_DRAFT_PROCESS_TIMEOUT_SECONDS
            if args.test_draft
            else DEFAULT_PROCESS_TIMEOUT_SECONDS
        )
    )

    if args.test_draft:
        return _run_test_draft_smoke(
            executable=executable,
            draftmancer_dir=args.draftmancer_dir,
            scryfall_bulk_file=args.scryfall_bulk_file,
            app_dir=args.app_dir,
            server_url=args.server_url,
            timeout=timeout,
        )

    environment = _clean_environment(environment=os.environ)
    with TemporaryDirectory(prefix="draftomen-bundle-smoke-") as temporary_dir:
        directory = Path(temporary_dir)
        _run_mock_launch(
            executable=executable,
            directory=directory,
            timeout=timeout,
            environment=environment,
        )
        _run_live_launch(
            executable=executable,
            directory=directory,
            timeout=timeout,
            environment=environment,
        )

    return 0


def _run_mock_launch(
    *,
    executable: Path,
    directory: Path,
    timeout: int,
    environment: Mapping[str, str],
) -> None:
    """Launch the bundle in deterministic mock mode with the bundled profile."""

    mock_directory = directory / "mock"
    mock_directory.mkdir(parents=True, exist_ok=True)
    app_directory = mock_directory / "app"
    command = [
        str(executable),
        "--provider",
        "mock",
        "--smoke-test",
        "--verify-bundled-profile",
        "--app-dir",
        str(app_directory),
    ]
    subprocess.run(
        args=command,
        check=True,
        cwd=executable.parent,
        env=environment,
        timeout=timeout,
    )
    profile_cache_path = app_directory / "set-profiles" / "hob-quickdraft.json"
    if profile_cache_path.exists() or profile_cache_path.is_symlink():
        raise RuntimeError(
            "Bundled profile smoke test mutated the flat profile cache: "
            f"{profile_cache_path}"
        )


def _run_live_launch(
    *,
    executable: Path,
    directory: Path,
    timeout: int,
    environment: Mapping[str, str],
) -> None:
    """Launch the bundle in default live mode and require a rendered window."""

    live_directory = directory / "live"
    live_directory.mkdir(parents=True, exist_ok=True)
    log_path = live_directory / "Player.log"
    log_path.touch()
    app_directory = live_directory / "app"
    screenshot_path = live_directory / "live-smoke.png"
    command = [
        str(executable),
        "--provider",
        "live",
        "--offline-profiles",
        "--no-startup-scan",
        "--smoke-test",
        "--log-path",
        str(log_path),
        "--app-dir",
        str(app_directory),
        "--screenshot",
        str(screenshot_path),
    ]
    result = subprocess.run(
        args=command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        cwd=executable.parent,
        env=environment,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Live smoke test exited with code {result.returncode}; "
            f"last stdout lines: {result.stdout.splitlines()[-10:]}; "
            f"last stderr lines: {result.stderr.splitlines()[-10:]}"
        )
    if log_path.stat().st_size != 0:
        raise RuntimeError(
            f"Live smoke test wrote to the isolated player log: {log_path}"
        )
    profile_cache_path = app_directory / "set-profiles" / "hob-quickdraft.json"
    if profile_cache_path.exists() or profile_cache_path.is_symlink():
        raise RuntimeError(
            "Live smoke test mutated the flat profile cache: "
            f"{profile_cache_path}"
        )
    if not screenshot_path.is_file() or screenshot_path.stat().st_size == 0:
        raise RuntimeError(
            f"Live smoke test did not render a window screenshot: {screenshot_path}"
        )


def _run_test_draft_smoke(
    *,
    executable: Path,
    draftmancer_dir: Path | None,
    scryfall_bulk_file: Path | None,
    app_dir: Path | None,
    server_url: str,
    timeout: int,
) -> int:
    """Drive one manual Test Draft journey against a pinned Draftmancer server."""

    problems: list[str] = []
    if draftmancer_dir is None:
        problems.append("--draftmancer-dir is required")
    elif not draftmancer_dir.is_dir():
        problems.append(
            f"--draftmancer-dir must be an existing directory: {draftmancer_dir}"
        )
    else:
        # The bundle runs with its own executable directory as the working
        # directory, so every validated input must reach it absolute.
        draftmancer_dir = draftmancer_dir.resolve()
    if scryfall_bulk_file is None:
        problems.append("--scryfall-bulk-file is required")
    elif not scryfall_bulk_file.is_file():
        problems.append(
            f"--scryfall-bulk-file must be an existing file: {scryfall_bulk_file}"
        )
    else:
        scryfall_bulk_file = scryfall_bulk_file.resolve()
    if app_dir is None:
        problems.append("--app-dir is required")
    elif not app_dir.is_dir():
        problems.append(f"--app-dir must be an existing directory: {app_dir}")
    else:
        app_dir = app_dir.resolve()
    if problems:
        raise RuntimeError(f"Invalid --test-draft arguments: {'; '.join(problems)}.")

    _probe_draftmancer_server(server_url=server_url)

    with TemporaryDirectory(prefix="draftomen-bundle-smoke-") as temporary_dir:
        log_path = Path(temporary_dir) / "Player.log"
        log_path.touch()
        command = [
            str(executable),
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
            server_url,
            "--test-draft-smoke",
        ]
        result = subprocess.run(
            args=command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            cwd=executable.parent,
            env=_clean_environment(environment=os.environ),
            timeout=timeout,
        )

    process_output = (
        f"exit code {result.returncode}; "
        f"last stdout lines: {result.stdout.splitlines()[-10:]}; "
        f"last stderr lines: {result.stderr.splitlines()[-10:]}"
    )
    if result.returncode != 0:
        raise RuntimeError(f"Test Draft smoke test failed with {process_output}")

    summary_line = next(
        (
            line
            for line in result.stdout.splitlines()
            if line.startswith(TEST_DRAFT_SUMMARY_PREFIX)
        ),
        None,
    )
    if summary_line is None:
        raise RuntimeError(
            f"Test Draft smoke test printed no summary line; {process_output}"
        )

    try:
        summary = json.loads(summary_line.removeprefix(TEST_DRAFT_SUMMARY_PREFIX))
    except ValueError as error:
        raise RuntimeError(
            f"Test Draft smoke test printed an invalid summary line "
            f"{summary_line!r}; {process_output}"
        ) from error

    if not isinstance(summary, dict):
        raise RuntimeError(
            f"Test Draft smoke test printed a non-object summary line "
            f"{summary_line!r}; {process_output}"
        )

    set_code = summary.get("set_code")
    picks = summary.get("picks")
    deck_size = summary.get("deck_size")
    if (
        summary.get("status") != "ok"
        or not isinstance(set_code, str)
        or not set_code
        or not isinstance(picks, int)
        or picks <= 0
        or not isinstance(deck_size, int)
        or deck_size <= 0
    ):
        raise RuntimeError(
            f"Test Draft smoke test reported an unusable summary line "
            f"{summary_line!r}; {process_output}"
        )

    _probe_draftmancer_server(server_url=server_url)

    print(
        json.dumps(
            {
                "status": "ok",
                "bundle": str(executable),
                "set_code": set_code,
                "picks": picks,
                "deck_size": deck_size,
                "selected_pair": summary.get("selected_pair"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _probe_draftmancer_server(*, server_url: str) -> None:
    """Confirm the pinned Draftmancer server answers a Socket.IO handshake."""

    probe_url = f"{server_url.rstrip('/')}/socket.io/?EIO=4&transport=polling"
    try:
        with urllib.request.urlopen(probe_url, timeout=5.0) as response:
            status = getattr(response, "status", None) or response.getcode()
    except Exception as error:
        raise RuntimeError(
            f"Draftmancer server probe failed for {probe_url}: {error}"
        ) from error
    if status != 200:
        raise RuntimeError(
            f"Draftmancer server probe for {probe_url} answered HTTP {status} "
            "instead of 200."
        )


def _resolve_bundle_executable(*, bundle_path: Path) -> Path:
    """Resolve the executable named by a macOS app or a Windows bundle."""

    if bundle_path.suffix.lower() == ".app" and bundle_path.is_dir():
        contents_directory = bundle_path / "Contents"
        plist_path = contents_directory / "Info.plist"
        if not plist_path.is_file():
            raise RuntimeError(f"Missing macOS bundle metadata: {plist_path}")

        try:
            with plist_path.open(mode="rb") as plist_file:
                metadata = plistlib.load(plist_file)
        except (OSError, plistlib.InvalidFileException, TypeError, ValueError) as error:
            raise RuntimeError(
                f"Could not read macOS bundle metadata from {plist_path}: {error}"
            ) from error

        if not isinstance(metadata, dict):
            raise RuntimeError(
                f"Invalid macOS bundle metadata in {plist_path}: "
                "expected a dictionary."
            )

        executable_name = metadata.get("CFBundleExecutable")
        if not isinstance(executable_name, str) or not executable_name.strip():
            raise RuntimeError(
                f"Invalid macOS bundle metadata in {plist_path}: "
                "CFBundleExecutable must be a nonempty string."
            )

        executable_path = contents_directory / "MacOS" / executable_name
        if not executable_path.is_file():
            raise RuntimeError(
                f"macOS bundle executable does not exist as a regular file: "
                f"{executable_path}"
            )
        return executable_path

    if bundle_path.suffix.lower() == ".exe" and bundle_path.is_file():
        return bundle_path

    raise RuntimeError(
        f"Expected a macOS .app directory or Windows .exe file: {bundle_path}"
    )


def _clean_environment(*, environment: Mapping[str, str]) -> dict[str, str]:
    """Remove development-environment overrides from the bundle process."""

    clean_environment = dict(environment)
    for variable in (
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
    ):
        clean_environment.pop(variable, None)
    return clean_environment


if __name__ == "__main__":
    raise SystemExit(main(argv=sys.argv[1:]))
