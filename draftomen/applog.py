"""Write the rotating Draft Omen application log.
Both the desktop app and the terminal app log to the same file.
"""

from __future__ import annotations

import logging
import platform
import sys
import threading
from logging.handlers import RotatingFileHandler
from os import PathLike
from pathlib import Path
from types import TracebackType

from draftomen import __version__
from draftomen import paths

LOG_FILE_NAME = "draftomen.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logger = logging.getLogger(__name__)

_handler: RotatingFileHandler | None = None
_previous_excepthook = sys.excepthook
_previous_threading_excepthook = threading.excepthook


def default_logs_dir(*, app_dir: str | PathLike[str] | None = None) -> Path:
    """Return the folder that holds the application log.
    It sits inside the per-user app data directory, or inside app_dir when given.
    """

    root = Path(paths.app_data_dir() if app_dir is None else app_dir).expanduser()
    return root / "logs"


def configure_logging(*, logs_dir: Path | None = None) -> Path | None:
    """Attach the rotating log file to the root logger and hook uncaught errors.
    Return the log path, or None after a stderr message when the file cannot open.
    """

    global _handler

    directory = default_logs_dir() if logs_dir is None else logs_dir
    log_path = directory / LOG_FILE_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            filename=log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as error:
        print(f"Draft Omen could not write its log to {log_path}: {error}", file=sys.stderr)
        return None

    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler.close()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _handler = handler

    _install_excepthooks()
    logger.info(
        "Draft Omen %s starting on %s with Python %s",
        __version__,
        platform.platform(),
        platform.python_version(),
    )
    return log_path


def _install_excepthooks() -> None:
    """Route uncaught main-thread and worker-thread errors into the log.
    Each wrapper still calls the hook it replaced.
    """

    global _previous_excepthook, _previous_threading_excepthook

    if sys.excepthook is not _log_uncaught_exception:
        _previous_excepthook = sys.excepthook
        sys.excepthook = _log_uncaught_exception
    if threading.excepthook is not _log_uncaught_thread_exception:
        _previous_threading_excepthook = threading.excepthook
        threading.excepthook = _log_uncaught_thread_exception


def _log_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    """Log an uncaught main-thread exception, then run the previous hook."""
    logger.critical(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )
    _previous_excepthook(exc_type, exc_value, exc_traceback)


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    """Log an uncaught worker-thread exception, then run the previous hook."""
    if args.exc_type is not SystemExit:
        thread_name = "unknown" if args.thread is None else args.thread.name
        logger.error(
            "Uncaught exception in thread %s",
            thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    _previous_threading_excepthook(args)
