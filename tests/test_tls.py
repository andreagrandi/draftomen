"""Test that HTTPS downloads trust the system store and fall back to certifi."""

from __future__ import annotations

import logging
import os
import ssl
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace

import certifi
import pytest
import truststore

from draftomen import cli, qt_gui, tls

# The autouse fixture replaces the helper, so keep the real one for its own test.
real_is_intel_macos = tls._is_intel_macos


@pytest.fixture(autouse=True)
def not_intel_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the truststore path whatever the host, so the tests do not depend on it."""

    monkeypatch.setattr(tls, "_is_intel_macos", lambda: False)


@pytest.fixture
def no_ssl_cert_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start with SSL_CERT_FILE unset and restore it after the test."""

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


def _assert_certifi_fallback(*, caplog: pytest.LogCaptureFixture, cause: str) -> None:
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert any(cause in message for message in warnings)
    assert os.environ["SSL_CERT_FILE"] == certifi.where()
    assert ssl.get_default_verify_paths().cafile == certifi.where()


def test_use_system_trust_store_injects_truststore(
    no_ssl_cert_file: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="draftomen.tls")

    tls.use_system_trust_store()

    assert ssl.SSLContext is truststore.SSLContext
    assert "SSL_CERT_FILE" not in os.environ
    assert "system trust store" in caplog.text


def test_use_system_trust_store_falls_back_to_certifi_when_injection_fails(
    monkeypatch: pytest.MonkeyPatch,
    no_ssl_cert_file: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing_injection() -> None:
        raise RuntimeError("injection marker")

    monkeypatch.setattr(truststore, "inject_into_ssl", failing_injection)

    tls.use_system_trust_store()

    assert ssl.SSLContext is not truststore.SSLContext
    _assert_certifi_fallback(caplog=caplog, cause="RuntimeError: injection marker")


def test_use_system_trust_store_falls_back_to_certifi_when_truststore_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    no_ssl_cert_file: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setitem(sys.modules, "truststore", None)

    tls.use_system_trust_store()

    _assert_certifi_fallback(caplog=caplog, cause="ModuleNotFoundError")


def test_use_system_trust_store_skips_truststore_on_intel_macos(
    monkeypatch: pytest.MonkeyPatch,
    no_ssl_cert_file: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class TouchedTruststore:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"truststore.{name} was used on Intel macOS")

    monkeypatch.setattr(tls, "_is_intel_macos", lambda: True)
    monkeypatch.setitem(sys.modules, "truststore", TouchedTruststore())
    caplog.set_level(logging.INFO, logger="draftomen.tls")

    tls.use_system_trust_store()

    assert os.environ["SSL_CERT_FILE"] == certifi.where()
    assert ssl.get_default_verify_paths().cafile == certifi.where()
    assert "HTTPS verification uses the bundled certifi file" in caplog.text
    assert "Could not use the system trust store" not in caplog.text


@pytest.mark.parametrize(
    ("platform_name", "machine", "expected"),
    [
        ("darwin", "x86_64", True),
        ("darwin", "arm64", False),
        ("linux", "x86_64", False),
        ("win32", "AMD64", False),
    ],
)
def test_is_intel_macos_matches_only_x86_64_macos(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    machine: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(tls.sys, "platform", platform_name)
    monkeypatch.setattr(tls.platform, "machine", lambda: machine)

    assert real_is_intel_macos() is expected


def test_cli_main_uses_the_system_trust_store(no_ssl_cert_file: None) -> None:
    assert cli.main(argv=["--version"]) == 0

    assert ssl.SSLContext is truststore.SSLContext


def test_gui_main_uses_the_system_trust_store(
    monkeypatch: pytest.MonkeyPatch,
    no_ssl_cert_file: None,
) -> None:
    monkeypatch.setattr(qt_gui, "run_gui", lambda: 0)
    monkeypatch.setattr(qt_gui, "_install_qt_message_handler", lambda: None)

    assert qt_gui.main() == 0

    assert ssl.SSLContext is truststore.SSLContext


def test_https_check_reports_status_and_size(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def read(self) -> bytes:
            return b"12345"

    requested: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(qt_gui.urllib.request, "urlopen", fake_urlopen)

    exit_code = qt_gui.run_gui(argv=["--https-check", "https://example.test/a"])

    assert exit_code == 0
    assert requested == ["https://example.test/a"]
    assert "status 200, 5 bytes, ssl.SSLContext" in capsys.readouterr().out


def test_https_check_reports_the_cause_of_a_failed_download(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_urlopen(request: urllib.request.Request, timeout: float) -> SimpleNamespace:
        raise urllib.error.URLError("certificate verify failed marker")

    monkeypatch.setattr(qt_gui.urllib.request, "urlopen", failing_urlopen)

    exit_code = qt_gui.run_gui(argv=["--https-check", "https://example.test/a"])

    assert exit_code == 1
    assert "certificate verify failed marker" in capsys.readouterr().err
