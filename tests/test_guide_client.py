from __future__ import annotations

import builtins
from collections.abc import Callable
from datetime import datetime
from email.message import Message
import hashlib
import io
import logging
from pathlib import Path
import traceback
from typing import Any
from urllib.error import HTTPError, URLError
import urllib.request

import pytest

from draftomen.guide_client import (
    GUIDE_ACCEPT,
    GUIDE_ACCEPT_ENCODING,
    GUIDE_USER_AGENT,
    GuideClient,
    GuideClientError,
)


_FIXTURE = Path(__file__).parent / "fixtures" / "guides" / "draftsim-guide.html"


class _Response:
    def __init__(
        self,
        payload: bytes | Any,
        *,
        url: str,
        status: int | Any = 200,
        headers: Message | None = None,
        read_error: BaseException | None = None,
        read_chunk: Any = None,
    ) -> None:
        self._stream = io.BytesIO(payload) if isinstance(payload, bytes) else None
        self._payload = payload
        self._read_error = read_error
        self._read_chunk = read_chunk
        self.read_sizes: list[int] = []
        self.url = url
        self.status = status
        self.code = status
        self.reason = "OK"
        self.msg = "OK"
        self.headers = headers if headers is not None else Message()
        self.closed = False

    def read(self, size: int = -1) -> Any:
        self.read_sizes.append(size)
        if self._read_error is not None:
            raise self._read_error
        if self._read_chunk is not None:
            chunk, self._read_chunk = self._read_chunk, None
            return chunk
        if self._stream is None:
            return self._payload
        return self._stream.read(size)

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> Any:
        return self.status

    def info(self) -> Message:
        return self.headers

    def close(self) -> None:
        self.closed = True


class _RecordingBytesIO(io.BytesIO):
    """Record every read attempt so an unread error body stays provable."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        return super().read(size)


def _headers(
    content_type: str | None = "text/html; charset=utf-8",
    *,
    content_encoding: str | None = None,
    content_length: int | str | None = None,
    duplicate_content_type: bool = False,
) -> Message:
    headers = Message()
    if content_type is not None:
        headers["Content-Type"] = content_type
        if duplicate_content_type:
            headers["Content-Type"] = content_type
    if content_encoding is not None:
        headers["Content-Encoding"] = content_encoding
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    return headers


def _opener(response: _Response, calls: list[dict[str, Any]]) -> Callable[..., _Response]:
    def open_url(request: Any, *, timeout: float) -> _Response:
        calls.append({"request": request, "timeout": timeout})
        return response

    return open_url


def _fresh_opener(factory: Callable[[], _Response], calls: list[dict[str, Any]]) -> Any:
    def open_url(request: Any, *, timeout: float) -> _Response:
        calls.append({"request": request, "timeout": timeout})
        return factory()

    return open_url


def _raising_opener(
    failure: BaseException, calls: list[dict[str, Any]]
) -> Callable[..., Any]:
    def open_url(request: Any, *, timeout: float) -> Any:
        calls.append({"request": request, "timeout": timeout})
        raise failure

    return open_url


def _document_response(payload: bytes, *, url: str = "https://draftsim.com/guide") -> _Response:
    return _Response(
        payload,
        url=url,
        headers=_headers(content_length=len(payload)),
    )


def test_url_policy_rejects_invalid_values_before_opening() -> None:
    calls: list[dict[str, Any]] = []
    opener = _opener(_document_response(b"guide"), calls)
    client = GuideClient(opener=opener)

    for candidate in (
        "http://draftsim.com/guide",
        "ftp://draftsim.com/guide",
        "https://draftsim.com.evil.test/guide",
        "https://evil-draftsim.com/guide",
        "https://draftsim.com./guide",
        "https://user:pass@draftsim.com/guide",
        "https://draftsim.com/guide#",
        " https://draftsim.com/guide",
        "https://draftsim.com/guide\n",
        "https://draftsim.com/guide\x7f",
        "https://draftsim.com/guide\\more",
        "https:///guide",
        "https://127.0.0.1/guide",
        "https://draftsim.com:/guide",
        "https://draftsim.com:444/guide",
        "https://draftsim.com:abc/guide",
    ):
        with pytest.raises(GuideClientError) as raised:
            client.fetch(url=candidate)
        assert raised.value.code == "invalid_url"
    assert calls == []


def test_url_policy_accepts_https_hosts_and_preserves_request() -> None:
    calls: list[dict[str, Any]] = []
    client = GuideClient(opener=_opener(_document_response(b"guide"), calls))

    client.fetch(url="HTTPS://DRAFTSIM.COM:443/a?keep=1")
    request = calls[0]["request"]
    assert request.full_url == "HTTPS://DRAFTSIM.COM:443/a?keep=1"
    assert request.get_method() == "GET"
    assert request.get_header("Accept") == GUIDE_ACCEPT
    assert request.get_header("Accept-encoding") == GUIDE_ACCEPT_ENCODING
    assert request.get_header("User-agent") == GUIDE_USER_AGENT
    assert calls[0]["timeout"] == 10.0


def test_constructor_rejects_invalid_configuration() -> None:
    with pytest.raises(TypeError):
        GuideClient(opener=object())  # type: ignore[arg-type]
    for timeout in (True, 0, -1, float("inf"), "10"):
        with pytest.raises(ValueError):
            GuideClient(timeout_seconds=timeout)  # type: ignore[arg-type]
    for maximum in (True, 0, -1, 1.5):
        with pytest.raises(ValueError):
            GuideClient(max_bytes=maximum)  # type: ignore[arg-type]


class _RouteHandler(urllib.request.BaseHandler):
    def __init__(self, routes: dict[str, Any], requests: list[str]) -> None:
        self.routes = routes
        self.requests = requests

    def http_open(self, request: Any) -> Any:
        self.requests.append(request.full_url)
        try:
            route = self.routes[request.full_url]
        except KeyError:
            raise AssertionError(f"unexpected request {request.full_url}") from None
        if isinstance(route, BaseException):
            raise route
        return route

    https_open = http_open


def _redirect_response(target: str, *, url: str) -> _Response:
    headers = Message()
    headers["Location"] = target
    return _Response(b"redirect body", url=url, status=302, headers=headers)


def _production_client(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, Any],
    requests: list[str],
) -> GuideClient:
    """Wrap the client's real redirect handler around a socket-free transport."""

    def build_opener(*handlers: Any) -> Any:
        opener = urllib.request.OpenerDirector()
        opener.add_handler(_RouteHandler(routes, requests))
        for handler in handlers:
            opener.add_handler(handler)
        opener.add_handler(urllib.request.HTTPErrorProcessor())
        opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    return GuideClient()


def test_same_origin_redirect_does_not_read_intermediate_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    redirect = _redirect_response("/final", url="https://draftsim.com/start")
    redirect._read_error = AssertionError("redirect body must not be consumed")
    final = _document_response(b"final", url="https://draftsim.com/final")
    client = _production_client(
        monkeypatch,
        {
            "https://draftsim.com/start": redirect,
            "https://draftsim.com/final": final,
        },
        requests,
    )

    document = client.fetch(url="https://draftsim.com/start")

    assert document.url == "https://draftsim.com/final"
    assert document.text == "final"
    assert requests == ["https://draftsim.com/start", "https://draftsim.com/final"]
    assert redirect.closed
    assert redirect.read_sizes == []
    assert final.closed


def test_redirect_closes_response_when_destination_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []
    redirect = _redirect_response("/final", url="https://draftsim.com/start")
    client = _production_client(
        monkeypatch,
        {
            "https://draftsim.com/start": redirect,
            "https://draftsim.com/final": URLError("destination secret"),
        },
        requests,
    )

    with pytest.raises(GuideClientError) as raised:
        client.fetch(url="https://draftsim.com/start")
    assert raised.value.code == "transport"
    assert str(raised.value) == "Guide acquisition failed (transport)."
    assert redirect.closed
    assert requests == ["https://draftsim.com/start", "https://draftsim.com/final"]


@pytest.mark.parametrize(
    "target",
    (
        "https://www.draftsim.com/final",
        "https://evil.example/final",
        "http://draftsim.com/final",
        "/final#",
        "/final#fragment",
        "/final\n",
    ),
)
def test_unsafe_redirect_is_rejected_before_destination(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    requests: list[str] = []
    redirect = _redirect_response(target, url="https://draftsim.com/start")
    redirect._read_error = AssertionError("redirect body must not be consumed")
    client = _production_client(monkeypatch, {"https://draftsim.com/start": redirect}, requests)

    with pytest.raises(GuideClientError) as raised:
        client.fetch(url="https://draftsim.com/start")
    assert raised.value.code == "unsafe_redirect"
    assert redirect.closed
    assert redirect.read_sizes == []
    assert requests == ["https://draftsim.com/start"]


def test_redirect_duplicates_and_loops_fail_boundedly(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []
    duplicate_headers = Message()
    duplicate_headers["Location"] = "/final"
    duplicate_headers["Location"] = "/other"
    duplicate = _Response(
        b"redirect body",
        url="https://draftsim.com/start",
        status=302,
        headers=duplicate_headers,
    )
    client = _production_client(monkeypatch, {"https://draftsim.com/start": duplicate}, requests)
    with pytest.raises(GuideClientError) as raised:
        client.fetch(url="https://draftsim.com/start")
    assert raised.value.code == "unsafe_redirect"
    assert duplicate.closed

    requests.clear()
    first = _redirect_response("/second", url="https://draftsim.com/first")
    second = _redirect_response("/first", url="https://draftsim.com/second")
    client = _production_client(
        monkeypatch,
        {
            "https://draftsim.com/first": first,
            "https://draftsim.com/second": second,
        },
        requests,
    )
    with pytest.raises(GuideClientError) as raised:
        client.fetch(url="https://draftsim.com/first")
    assert raised.value.code == "unsafe_redirect"
    assert len(requests) <= 11
    assert first.closed and second.closed


def test_injected_final_url_cannot_bypass_origin_policy() -> None:
    calls: list[dict[str, Any]] = []
    response = _document_response(b"guide", url="https://evil.example/guide")
    client = GuideClient(opener=_opener(response, calls))

    with pytest.raises(GuideClientError) as raised:
        client.fetch(url="https://draftsim.com/guide")
    assert raised.value.code == "unsafe_redirect"
    assert response.closed


def test_bounds_accept_limit_and_reject_overflow_without_content_length() -> None:
    exact = _Response(
        b"1234",
        url="https://draftsim.com/exact",
        headers=_headers(content_length=4),
    )
    result = GuideClient(opener=_opener(exact, []), max_bytes=4).fetch(
        url="https://draftsim.com/exact"
    )
    assert result.text == "1234"
    assert exact.read_sizes == [5, 1]

    overflow = _Response(
        b"12345",
        url="https://draftsim.com/overflow",
        headers=_headers(content_type="text/plain"),
    )
    with pytest.raises(GuideClientError) as raised:
        GuideClient(opener=_opener(overflow, []), max_bytes=4).fetch(
            url="https://draftsim.com/overflow"
        )
    assert raised.value.code == "too_large"
    assert overflow.closed


def test_bounds_reject_declared_overflow_short_body_and_bad_chunks() -> None:
    oversized = _Response(
        b"never read",
        url="https://draftsim.com/oversized",
        headers=_headers(content_length=5),
    )
    with pytest.raises(GuideClientError) as raised:
        GuideClient(opener=_opener(oversized, []), max_bytes=4).fetch(
            url="https://draftsim.com/oversized"
        )
    assert raised.value.code == "too_large"
    assert oversized.read_sizes == []
    assert oversized.closed

    short = _Response(
        b"abc",
        url="https://draftsim.com/short",
        headers=_headers(content_length=4),
    )
    with pytest.raises(GuideClientError) as raised:
        GuideClient(opener=_opener(short, []), max_bytes=8).fetch(
            url="https://draftsim.com/short"
        )
    assert raised.value.code == "truncated"
    assert short.closed

    bad_chunk = _Response(
        b"unused",
        url="https://draftsim.com/chunk",
        headers=_headers(content_length=None),
        read_chunk="not bytes",
    )
    with pytest.raises(GuideClientError) as raised:
        GuideClient(opener=_opener(bad_chunk, []), max_bytes=8).fetch(
            url="https://draftsim.com/chunk"
        )
    assert raised.value.code == "malformed_response"
    assert bad_chunk.closed

    invalid_headers = _Response(
        b"guide",
        url="https://draftsim.com/headers",
        headers=_headers(duplicate_content_type=True),
    )
    with pytest.raises(GuideClientError) as raised:
        GuideClient(opener=_opener(invalid_headers, []), max_bytes=8).fetch(
            url="https://draftsim.com/headers"
        )
    assert raised.value.code == "malformed_response"
    assert invalid_headers.closed


def test_declared_content_length_accepts_padding_and_rejects_overflow() -> None:
    huge = _Response(
        b"never read",
        url="https://draftsim.com/huge",
        headers=_headers(content_length="9" * 5000),
    )
    with pytest.raises(GuideClientError) as raised:
        GuideClient(opener=_opener(huge, []), max_bytes=4).fetch(
            url="https://draftsim.com/huge"
        )
    assert raised.value.code == "too_large"
    assert huge.read_sizes == []
    assert huge.closed

    padded = _Response(
        b"1234",
        url="https://draftsim.com/padded",
        headers=_headers(content_length="0004"),
    )
    document = GuideClient(opener=_opener(padded, []), max_bytes=4).fetch(
        url="https://draftsim.com/padded"
    )
    assert document.text == "1234"
    assert padded.closed


def test_timeout_transport_and_http_failures_are_classified_without_leaks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    for failure, code in (
        (TimeoutError("secret timeout"), "timeout"),
        (URLError(TimeoutError("secret wrapped timeout")), "timeout"),
        (URLError("secret network detail"), "transport"),
        (OSError("secret os detail"), "transport"),
    ):
        calls: list[dict[str, Any]] = []
        client = GuideClient(
            opener=_raising_opener(failure, calls),
            timeout_seconds=3.5,
        )
        with pytest.raises(GuideClientError) as raised:
            client.fetch(url="https://draftsim.com/guide")
        assert raised.value.code == code
        assert calls[0]["timeout"] == 3.5
        assert "secret" not in str(raised.value)
        assert "secret" not in "".join(traceback.format_exception(raised.value))

    error = HTTPError(
        "https://draftsim.com/guide",
        503,
        "secret HTTP detail",
        _headers(content_type="text/plain"),
        _RecordingBytesIO(b"secret response body"),
    )
    error_calls: list[dict[str, Any]] = []
    client = GuideClient(opener=_raising_opener(error, error_calls), timeout_seconds=3.5)
    with pytest.raises(GuideClientError) as raised:
        client.fetch(url="https://draftsim.com/guide")
    assert raised.value.code == "http"
    assert error_calls[0]["timeout"] == 3.5
    assert isinstance(error.fp, _RecordingBytesIO)
    assert error.fp.reads == []
    assert error.fp.closed
    assert "secret" not in "".join(traceback.format_exception(raised.value))

    read_timeout = _Response(
        b"unused",
        url="https://draftsim.com/guide",
        headers=_headers(),
    )
    read_calls: list[dict[str, Any]] = []

    def read_opener(request: Any, *, timeout: float) -> _Response:
        read_calls.append({"request": request, "timeout": timeout})
        if timeout == 2.5:
            read_timeout._read_error = TimeoutError("secret read timeout")
        return read_timeout

    client = GuideClient(opener=read_opener, timeout_seconds=2.5)
    with pytest.raises(GuideClientError) as raised:
        client.fetch(url="https://draftsim.com/guide")
    assert raised.value.code == "timeout"
    assert read_calls[0]["timeout"] == 2.5
    assert read_timeout.closed
    assert "secret" not in caplog.text


def test_content_fidelity_hash_and_timestamp() -> None:
    fixture = _FIXTURE.read_bytes()
    calls: list[dict[str, Any]] = []
    client = GuideClient(
        opener=_fresh_opener(
            lambda: _document_response(
                fixture,
                url="https://draftsim.com/fixture-guide/",
            ),
            calls,
        )
    )

    first = client.fetch(url="https://draftsim.com/fixture-guide/")
    second = client.fetch(url="https://draftsim.com/fixture-guide/")

    assert first.text == fixture.decode("utf-8")
    assert first.sha256 == hashlib.sha256(fixture).hexdigest()
    assert second.sha256 == first.sha256
    assert first.url == "https://draftsim.com/fixture-guide/"
    timestamp = datetime.fromisoformat(first.retrieved_at)
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() is not None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "headers,payload",
    (
        (_headers(content_type="application/json"), b"{}"),
        (_headers(content_encoding="gzip"), b"guide"),
        (_headers(content_type="text/html; charset=iso-8859-1"), b"guide"),
        (_headers(content_type="text/html; charset="), b"guide"),
        (_headers(), b"\xff"),
        (_headers(), b""),
    ),
)
def test_unsupported_and_malformed_content_fails(
    headers: Message,
    payload: bytes,
) -> None:
    response = _Response(payload, url="https://draftsim.com/guide", headers=headers)
    with pytest.raises(GuideClientError) as raised:
        GuideClient(opener=_opener(response, [])).fetch(url="https://draftsim.com/guide")
    assert raised.value.code in {"unsupported_content", "malformed_response"}
    assert response.closed


def test_public_fetch_has_no_filesystem_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile.json"
    artifact = tmp_path / "production-artifact.json"
    profile.write_bytes(b"profile sentinel")
    artifact.write_bytes(b"artifact sentinel")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    fixture = _FIXTURE.read_bytes()
    responses = iter(
        (
            _document_response(fixture),
            _Response(
                fixture,
                url="https://draftsim.com/guide",
                headers=_headers(content_type="application/json"),
            ),
        )
    )
    real_open = io.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError("guide acquisition attempted a filesystem write")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(builtins, "open", guarded_open)
    client = GuideClient(opener=lambda *_args, **_kwargs: next(responses))
    assert client.fetch(url="https://draftsim.com/guide").text == fixture.decode("utf-8")
    with pytest.raises(GuideClientError):
        client.fetch(url="https://draftsim.com/guide")

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
