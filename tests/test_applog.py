from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from draftomen import __version__, applog


def _read_log(*, path: Path | None) -> str:
    assert path is not None
    return path.read_text(encoding="utf-8")


def test_configure_logging_creates_the_log_file_with_a_startup_line(
    tmp_path: Path,
) -> None:
    logs_dir = tmp_path / "logs"

    log_path = applog.configure_logging(logs_dir=logs_dir)

    assert log_path == logs_dir / applog.LOG_FILE_NAME
    contents = _read_log(path=log_path)
    assert f"Draft Omen {__version__} starting on " in contents
    assert " INFO " in contents


def test_configure_logging_uses_the_default_logs_dir_without_an_argument(
    app_logs_dir: Path,
) -> None:
    log_path = applog.configure_logging()

    assert log_path == app_logs_dir / applog.LOG_FILE_NAME
    assert log_path.is_file()


def test_default_logs_dir_sits_inside_the_app_data_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert applog.default_logs_dir() == tmp_path / ".draftomen" / "logs"


def test_default_logs_dir_uses_the_given_app_dir(tmp_path: Path) -> None:
    assert applog.default_logs_dir(app_dir=tmp_path) == tmp_path / "logs"


def test_configure_logging_twice_keeps_a_single_file_handler(tmp_path: Path) -> None:
    applog.configure_logging(logs_dir=tmp_path / "first")
    applog.configure_logging(logs_dir=tmp_path / "second")

    file_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == tmp_path / "second" / applog.LOG_FILE_NAME


def test_log_file_rotates_and_keeps_only_the_configured_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(applog, "LOG_MAX_BYTES", 300)
    monkeypatch.setattr(applog, "LOG_BACKUP_COUNT", 2)
    logs_dir = tmp_path / "logs"
    applog.configure_logging(logs_dir=logs_dir)
    logger = logging.getLogger("draftomen.test-rotation")

    for index in range(100):
        logger.info("filler line number %d with some padding text", index)

    names = sorted(path.name for path in logs_dir.iterdir())
    assert names == [
        applog.LOG_FILE_NAME,
        f"{applog.LOG_FILE_NAME}.1",
        f"{applog.LOG_FILE_NAME}.2",
    ]


def test_main_thread_exception_is_logged_with_its_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chained: list[BaseException] = []

    def previous_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        chained.append(exc_value)

    monkeypatch.setattr(sys, "excepthook", previous_hook)
    log_path = applog.configure_logging(logs_dir=tmp_path / "logs")

    try:
        raise ValueError("main thread failure marker")
    except ValueError as error:
        sys.excepthook(type(error), error, error.__traceback__)

    contents = _read_log(path=log_path)
    assert "CRITICAL" in contents
    assert "Traceback (most recent call last)" in contents
    assert "ValueError: main thread failure marker" in contents
    assert len(chained) == 1
    assert str(chained[0]) == "main thread failure marker"


def test_worker_thread_exception_is_logged_with_its_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chained: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", chained.append)
    log_path = applog.configure_logging(logs_dir=tmp_path / "logs")

    def fail() -> None:
        raise RuntimeError("worker thread failure marker")

    worker = threading.Thread(target=fail, name="applog-test-worker")
    worker.start()
    worker.join()

    contents = _read_log(path=log_path)
    assert "Uncaught exception in thread applog-test-worker" in contents
    assert "Traceback (most recent call last)" in contents
    assert "RuntimeError: worker thread failure marker" in contents
    assert len(chained) == 1
    assert chained[0].exc_type is RuntimeError


def test_worker_thread_exit_is_not_logged_but_still_chained(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chained: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", chained.append)
    log_path = applog.configure_logging(logs_dir=tmp_path / "logs")

    def leave() -> None:
        raise SystemExit(0)

    worker = threading.Thread(target=leave)
    worker.start()
    worker.join()

    assert "Uncaught exception" not in _read_log(path=log_path)
    assert len(chained) == 1


def test_configure_logging_twice_logs_an_uncaught_error_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "excepthook", lambda *_args: None)
    applog.configure_logging(logs_dir=tmp_path / "logs")
    log_path = applog.configure_logging(logs_dir=tmp_path / "logs")

    try:
        raise ValueError("logged once marker")
    except ValueError as error:
        sys.excepthook(type(error), error, error.__traceback__)

    assert _read_log(path=log_path).count("ValueError: logged once marker") == 1


def test_unwritable_logs_dir_reports_to_stderr_and_returns_none(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "logs"
    blocked.write_text("a file where the folder should be", encoding="utf-8")

    log_path = applog.configure_logging(logs_dir=blocked)

    assert log_path is None
    captured = capsys.readouterr()
    assert "could not write its log" in captured.err
    assert str(blocked) in captured.err
    assert not any(
        isinstance(handler, RotatingFileHandler)
        for handler in logging.getLogger().handlers
    )
