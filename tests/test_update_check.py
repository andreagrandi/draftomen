"""Test the update check against a local HTTP server.
Failures must return None with a warning, and the check must not block its caller.
"""

from __future__ import annotations

import http.server
import logging
import sys
import threading
import types
import time
from collections.abc import Awaitable, Iterator
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
def test_check_returns_none_when_published_is_not_newer(
    site: tuple[FakeSite, str], installed: str
) -> None:
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
    assert _warnings(caplog) == [
        "Skipping the update check because the installed version is unknown"
    ]


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
    [
        b"not json",
        b'["0.7.0"]',
        b'{"version": 7}',
        b"{}",
        b'{"version": "not a version"}',
    ],
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


def test_start_update_check_runs_off_the_callers_thread(
    site: tuple[FakeSite, str],
) -> None:
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


class FakeKernel32:
    """Stands in for kernel32 and returns a fixed GetCurrentPackageFullName code."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.calls = 0

    def GetCurrentPackageFullName(self, length: object, buffer: object) -> int:
        self.calls += 1
        return self.code


@pytest.mark.parametrize(("code", "expected"), [(0, True), (122, True), (15700, False), (1, False)])
def test_is_store_install_maps_the_package_full_name_result(code: int, expected: bool) -> None:
    kernel32 = FakeKernel32(code=code)

    assert update_check.is_store_install(platform="win32", kernel32=kernel32) is expected
    assert kernel32.calls == 1


def test_is_store_install_is_false_off_windows_without_touching_kernel32() -> None:
    kernel32 = FakeKernel32(code=0)

    assert update_check.is_store_install(platform="darwin", kernel32=kernel32) is False
    assert kernel32.calls == 0


def test_is_store_install_warns_and_returns_false_when_the_call_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    assert update_check.is_store_install(platform="win32", kernel32=object()) is False
    assert len(_warnings(caplog)) == 1


def test_install_channel_names_store_github_and_website_builds() -> None:
    assert update_check.install_channel(platform="win32", kernel32=FakeKernel32(code=0)) == "store"
    assert (
        update_check.install_channel(platform="win32", kernel32=FakeKernel32(code=15700))
        == "github"
    )
    assert (
        update_check.install_channel(platform="darwin", kernel32=FakeKernel32(code=0)) == "website"
    )
    assert update_check.STORE_CHANNEL == "store"
    assert update_check.GITHUB_CHANNEL == "github"
    assert update_check.WEBSITE_CHANNEL == "website"


def test_app_version_from_package_version_drops_the_revision() -> None:
    assert update_check.app_version_from_package_version(package_version="0.6.5.0") == "0.6.5"
    assert update_check.app_version_from_package_version(package_version="10.20.30.0") == "10.20.30"


@pytest.mark.parametrize("package_version", ["0.6.5", "a.b.c.d", "0.6.5.0.1", "", "0.6.5."])
def test_app_version_from_package_version_rejects_invalid_input(
    package_version: str,
) -> None:
    with pytest.raises(ValueError):
        update_check.app_version_from_package_version(package_version=package_version)


def test_store_check_never_requests_the_url_and_announces_the_store_version(
    site: tuple[FakeSite, str],
) -> None:
    fake, url = site
    fake.status = 500

    result = update_check.check_for_update(
        url=url,
        installed="0.6.4",
        store=True,
        query_store=lambda: "0.7.0.0",
    )

    assert result == "0.7.0"


def test_store_check_does_not_touch_the_url(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    result = update_check.check_for_update(
        url="http://127.0.0.1:1/version.json",
        installed="0.6.4",
        store=True,
        query_store=lambda: "0.7.0.0",
    )

    assert result == "0.7.0"
    assert _warnings(caplog) == []


def test_store_check_returns_none_when_the_store_has_no_update() -> None:
    assert (
        update_check.check_for_update(installed="0.6.4", store=True, query_store=lambda: None)
        is None
    )


def test_store_check_returns_none_when_the_store_version_is_not_newer() -> None:
    assert (
        update_check.check_for_update(installed="0.7.0", store=True, query_store=lambda: "0.7.0.0")
        is None
    )


def test_store_check_warns_and_returns_none_when_the_query_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    def query_store() -> str | None:
        raise RuntimeError("store unavailable")

    assert (
        update_check.check_for_update(installed="0.6.4", store=True, query_store=query_store)
        is None
    )
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "Microsoft Store update check failed" in warnings[0]
    assert "store unavailable" in warnings[0]


def test_store_check_warns_and_returns_none_on_a_malformed_store_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    assert (
        update_check.check_for_update(installed="0.6.4", store=True, query_store=lambda: "0.7.0")
        is None
    )
    assert len(_warnings(caplog)) == 1


def test_store_check_skips_unknown_installed_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    calls: list[str] = []

    def query_store() -> str | None:
        calls.append("queried")
        return "0.7.0.0"

    result = update_check.check_for_update(
        installed=update_check.UNKNOWN_VERSION, store=True, query_store=query_store
    )

    assert result is None
    assert calls == []
    assert len(_warnings(caplog)) == 1


def test_start_update_check_passes_the_store_flag_through() -> None:
    results: list[str | None] = []
    done = threading.Event()

    def on_result(version: str | None) -> None:
        results.append(version)
        done.set()

    original = update_check.check_for_update
    seen: list[bool] = []

    def fake_check(**kwargs: object) -> str | None:
        seen.append(bool(kwargs["store"]))
        return "9.9.9"

    update_check.check_for_update = fake_check  # type: ignore[assignment]
    try:
        update_check.start_update_check(on_result=on_result, installed="0.6.4", store=True).join(
            timeout=5
        )
    finally:
        update_check.check_for_update = original

    assert done.is_set()
    assert seen == [True]
    assert results == ["9.9.9"]


class _FakeVersion:
    def __init__(self, *, major: int, minor: int, build: int, revision: int) -> None:
        self.major = major
        self.minor = minor
        self.build = build
        self.revision = revision


def _fake_update(*, family_name: str, version: _FakeVersion) -> object:
    package_id = types.SimpleNamespace(family_name=family_name, version=version)
    return types.SimpleNamespace(package=types.SimpleNamespace(id=package_id))


def _install_fake_winrt(monkeypatch: pytest.MonkeyPatch, *, updates: list[object]) -> list[str]:
    class FakeContext:
        def get_app_and_optional_store_package_updates_async(
            self,
        ) -> Awaitable[list[object]]:
            async def result() -> list[object]:
                return updates

            return result()

    class FakeStoreContext:
        @staticmethod
        def get_default() -> FakeContext:
            return FakeContext()

    current = types.SimpleNamespace(id=types.SimpleNamespace(family_name="Draftomen_abc"))
    store_module = types.ModuleType("winrt.windows.services.store")
    store_module.StoreContext = FakeStoreContext  # type: ignore[attr-defined]
    model_module = types.ModuleType("winrt.windows.applicationmodel")
    model_module.Package = types.SimpleNamespace(current=current)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winrt.windows.services.store", store_module)
    monkeypatch.setitem(sys.modules, "winrt.windows.applicationmodel", model_module)
    apartment_calls: list[str] = []
    runtime_module = types.ModuleType("winrt.runtime")
    runtime_module.ApartmentType = types.SimpleNamespace(MULTI_THREADED="mta")  # type: ignore[attr-defined]
    runtime_module.init_apartment = lambda kind: apartment_calls.append(f"init {kind}")  # type: ignore[attr-defined]
    runtime_module.uninit_apartment = lambda: apartment_calls.append("uninit")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winrt.runtime", runtime_module)
    return apartment_calls


def test_query_store_update_version_returns_the_matching_package_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = [
        _fake_update(
            family_name="Other_xyz",
            version=_FakeVersion(major=9, minor=9, build=9, revision=0),
        ),
        _fake_update(
            family_name="Draftomen_abc",
            version=_FakeVersion(major=0, minor=6, build=5, revision=0),
        ),
    ]
    apartment_calls = _install_fake_winrt(monkeypatch, updates=updates)

    assert update_check.query_store_update_version() == "0.6.5.0"
    assert apartment_calls == ["init mta", "uninit"]


def test_query_store_update_version_ignores_other_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = [
        _fake_update(
            family_name="Other_xyz",
            version=_FakeVersion(major=9, minor=9, build=9, revision=0),
        ),
    ]
    _install_fake_winrt(monkeypatch, updates=updates)

    assert update_check.query_store_update_version() is None


def test_query_store_update_version_returns_none_for_no_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_winrt(monkeypatch, updates=[])

    assert update_check.query_store_update_version() is None
