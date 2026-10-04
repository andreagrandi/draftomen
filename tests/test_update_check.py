"""Test the update check against a local HTTP server.
Failures must return None with a warning, and the check must not block its caller.
"""

from __future__ import annotations

import http.server
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from draftomen import update_check


@dataclass
class FakeSite:
    """What the local server answers for /version.json."""

    status: int = 200
    body: bytes = b'{"version": "0.7.0"}'
    delay_seconds: float = 0.0
    release: threading.Event | None = None


@pytest.fixture
def site() -> Iterator[tuple[FakeSite, str]]:
    fake = FakeSite()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if fake.release is not None:
                fake.release.wait(timeout=5)
            time.sleep(fake.delay_seconds)
            self.send_response(fake.status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(fake.body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}/version.json"
    finally:
        if fake.release is not None:
            fake.release.set()
        server.shutdown()
        server.server_close()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


def test_check_returns_published_version_when_newer(site: tuple[FakeSite, str]) -> None:
    _, url = site

    assert update_check.check_for_update(url=url, installed="0.6.3") == "0.7.0"


@pytest.mark.parametrize("installed", ["0.7.0", "0.7.1", "0.8.0.dev3"])
def test_check_returns_none_when_published_is_not_newer(site: tuple[FakeSite, str], installed: str) -> None:
    _, url = site

    assert update_check.check_for_update(url=url, installed=installed) is None


def test_check_uses_pep_440_ordering() -> None:
    assert update_check.newer_version(installed="0.9.0", published="0.10.0") == "0.10.0"
    assert update_check.newer_version(installed="0.7.0rc1", published="0.7.0") == "0.7.0"
    assert update_check.newer_version(installed="0.7.0", published="0.7.0rc1") is None


def test_check_skips_unknown_installed_version(
    site: tuple[FakeSite, str], caplog: pytest.LogCaptureFixture
) -> None:
    _, url = site

    with caplog.at_level(logging.WARNING, logger=update_check.__name__):
        result = update_check.check_for_update(url=url, installed=update_check.UNKNOWN_VERSION)

    assert result is None
    assert _warnings(caplog) == ["Skipping the update check because the installed version is unknown"]


def test_check_returns_none_and_warns_on_timeout(
    site: tuple[FakeSite, str], caplog: pytest.LogCaptureFixture
) -> None:
    fake, url = site
    fake.delay_seconds = 1.0

    with caplog.at_level(logging.WARNING, logger=update_check.__name__):
        result = update_check.check_for_update(url=url, installed="0.6.3", timeout_seconds=0.2)

    assert result is None
    [warning] = _warnings(caplog)
    assert warning.startswith(f"Update check against {url} failed:")
    assert "timed out" in warning


def test_check_returns_none_and_warns_on_http_error(
    site: tuple[FakeSite, str], caplog: pytest.LogCaptureFixture
) -> None:
    fake, url = site
    fake.status = 503

    with caplog.at_level(logging.WARNING, logger=update_check.__name__):
        result = update_check.check_for_update(url=url, installed="0.6.3")

    assert result is None
    [warning] = _warnings(caplog)
    assert "HTTP Error 503" in warning


@pytest.mark.parametrize(
    "body",
    [b"not json", b'["0.7.0"]', b'{"version": 7}', b"{}", b'{"version": "not a version"}'],
)
def test_check_returns_none_and_warns_on_malformed_json(
    site: tuple[FakeSite, str], caplog: pytest.LogCaptureFixture, body: bytes
) -> None:
    fake, url = site
    fake.body = body

    with caplog.at_level(logging.WARNING, logger=update_check.__name__):
        result = update_check.check_for_update(url=url, installed="0.6.3")

    assert result is None
    [warning] = _warnings(caplog)
    assert warning.startswith(f"Update check against {url} failed:")


def test_start_update_check_runs_off_the_callers_thread(site: tuple[FakeSite, str]) -> None:
    fake, url = site
    fake.release = threading.Event()
    results: list[tuple[str | None, int]] = []
    done = threading.Event()

    def on_result(version: str | None) -> None:
        results.append((version, threading.get_ident()))
        done.set()

    started = time.monotonic()
    thread = update_check.start_update_check(on_result=on_result, url=url, installed="0.6.3")
    returned_after = time.monotonic() - started

    # The server holds the response, so the caller is free while the check waits.
    assert returned_after < 0.5
    assert thread.daemon
    assert results == []

    fake.release.set()
    assert done.wait(timeout=5)
    assert results == [("0.7.0", thread.ident)]
    assert thread.ident != threading.get_ident()

