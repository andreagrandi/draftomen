from __future__ import annotations

import difflib
from pathlib import Path
from tempfile import TemporaryDirectory

import nox

PROJECT_ROOT = Path(__file__).parent
FIXTURES_DIRECTORY = PROJECT_ROOT / "tests" / "fixtures"
GOLDEN_DIRECTORY = PROJECT_ROOT / "tests" / "golden"
BULK_FILE_PATH = FIXTURES_DIRECTORY / "scryfall-default-cards-sample.jsonl"
QML_DIRECTORY = PROJECT_ROOT / "draftomen" / "qml"


@nox.session(python=False)
def gui(session: nox.Session) -> None:
    session.run("uv", "sync", "--locked", external=True)
    session.run(
        "uv",
        "run",
        "pytest",
        "tests/test_session.py",
        "tests/test_mock_session.py",
        "tests/test_qt_adapter.py",
        "tests/test_qt_mock.py",
        "tests/test_qt_gui.py",
        "tests/test_preferences.py",
    )
    session.run(
        "uv",
        "run",
        "pyside6-qmllint",
        "-I",
        str(QML_DIRECTORY),
        *(str(path) for path in sorted(QML_DIRECTORY.glob("*.qml"))),
        external=True,
    )
    session.run(
        "uv",
        "run",
        "draftomen",
        "--provider",
        "mock",
        "--smoke-test",
        external=True,
    )


@nox.session(python=False)
def ci(session: nox.Session) -> None:
    session.run("uv", "run", "ruff", "check", ".", external=True)
    session.run("uv", "run", "pytest", external=True)
    _run_replay_regressions(session=session)
    session.run("uv", "run", "draftomen-tui", "--version", external=True)
    session.run("git", "diff", "--check", external=True)


def _run_replay_regressions(*, session: nox.Session) -> None:
    logfiles = sorted(FIXTURES_DIRECTORY.glob("*-player.log"))
    if not logfiles:
        session.error(f"No replay fixtures found in {FIXTURES_DIRECTORY}.")

    if not BULK_FILE_PATH.is_file():
        session.error(f"Replay bulk fixture is missing: {BULK_FILE_PATH}.")

    for logfile in logfiles:
        golden_path = GOLDEN_DIRECTORY / f"{logfile.stem}.replay.txt"
        if not golden_path.is_file():
            session.error(f"Replay golden file is missing: {golden_path}.")

        with TemporaryDirectory(prefix="draftomen-ci-") as app_dir:
            actual_output = session.run(
                "uv",
                "run",
                "draftomen-tui",
                "replay",
                str(logfile),
                "--bulk-file",
                str(BULK_FILE_PATH),
                "--app-dir",
                app_dir,
                external=True,
                silent=True,
            )
        expected_output = golden_path.read_text(encoding="utf-8")

        if actual_output != expected_output:
            session.error(
                _replay_mismatch_message(
                    actual_output=actual_output,
                    expected_output=expected_output,
                    golden_path=golden_path,
                    logfile=logfile,
                )
            )


def _replay_mismatch_message(
    *,
    actual_output: str,
    expected_output: str,
    golden_path: Path,
    logfile: Path,
) -> str:
    diff = "".join(
        difflib.unified_diff(
            expected_output.splitlines(keepends=True),
            actual_output.splitlines(keepends=True),
            fromfile=str(golden_path),
            tofile=f"{logfile} replay output",
        )
    )
    return f"Replay output does not match {golden_path}:\n{diff}"
