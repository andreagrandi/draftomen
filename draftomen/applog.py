"""Write the rotating Draft Omen application log.
Both the desktop app and the terminal app log to the same file.
"""

from __future__ import annotations

import logging
import os
import platform
import ssl
import sys
import threading
import urllib.error
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
    _log_tls_setup()
    return log_path


def describe_fetch_error(error: BaseException) -> str:
    """Return the cause of a failed hosted fetch for the application log.
    The text names the HTTP status, the inner URLError reason or the exception type.
    """

    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP status {error.code}"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, BaseException):
            return redact_home(f"{type(reason).__name__}: {reason}")
        return redact_home(f"URLError: {reason}")
    return redact_home(f"{type(error).__name__}: {error}")


def redact_home(text: str) -> str:
    """Replace the user's home directory with ~ in text bound for the log."""

    home = str(Path.home())
    if len(home) <= 1:
        return text
    return text.replace(home, "~")


def _running_mode() -> str:
    # Nuitka defines __compiled__ in every module of a pyside6-deploy bundle.
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        return "bundle"
    return "source"


def _log_tls_setup() -> None:
    """Log the run mode and the CA locations OpenSSL will use."""

    verify_paths = ssl.get_default_verify_paths()
    # SSL_CERT_FILE and SSL_CERT_DIR replace the compiled-in paths when set.
    cafile = os.environ.get(verify_paths.openssl_cafile_env) or verify_paths.openssl_cafile
    capath = os.environ.get(verify_paths.openssl_capath_env) or verify_paths.openssl_capath
    cafile_state = "exists" if cafile and Path(cafile).is_file() else "missing"
    logger.info(
        "Running from %s with %s; default cafile %s (%s), capath %s",
        _running_mode(),
        ssl.OPENSSL_VERSION,
        redact_home(cafile or "none"),
        cafile_state,
        redact_home(capath or "none"),
    )


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
