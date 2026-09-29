from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from draftomen import applog
from draftomen.card_data_client import CardDataClient


@pytest.fixture(autouse=True)
def _no_hosted_sets_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop card-data clients built without an explicit manifest URL from reaching the website.
    Tests that exercise manifest checks pass sets_manifest_url themselves.
    """

    monkeypatch.setitem(CardDataClient.__init__.__kwdefaults__, "sets_manifest_url", None)


@pytest.fixture
def app_logs_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the folder that stands in for ~/.draftomen/logs during one test."""

    return tmp_path_factory.mktemp("app-logs")


@pytest.fixture(autouse=True)
def _isolated_application_log(
    monkeypatch: pytest.MonkeyPatch,
    app_logs_dir: Path,
) -> Iterator[None]:
    """Keep every test out of the real application log.
    Restore the exception hooks and root logger that configure_logging changes.
    """

    real_default_logs_dir = applog.default_logs_dir

    def isolated_default_logs_dir(*, app_dir: str | os.PathLike[str] | None = None) -> Path:
        if app_dir is None:
            return app_logs_dir
        return real_default_logs_dir(app_dir=app_dir)

    monkeypatch.setattr(applog, "default_logs_dir", isolated_default_logs_dir)
    # Tests that start the CLI or the app in a subprocess inherit this home folder,
    # so those runs do not write to the real ~/.draftomen/logs either.
    monkeypatch.setenv("HOME", str(app_logs_dir.parent / f"{app_logs_dir.name}-home"))
    monkeypatch.setenv("USERPROFILE", os.environ["HOME"])
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    excepthook = sys.excepthook
    threading_excepthook = threading.excepthook
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    sys.excepthook = excepthook
    threading.excepthook = threading_excepthook
    applog._handler = None
