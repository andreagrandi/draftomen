"""Fetch-only client for bounded Draftsim guide acquisition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import io
import math
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import urljoin, urlsplit
import urllib.error
import urllib.request


GUIDE_TIMEOUT_SECONDS = 10.0
GUIDE_MAX_BYTES = 4 * 1024 * 1024
GUIDE_ACCEPT = "text/html, text/plain"
GUIDE_ACCEPT_ENCODING = "identity"
GUIDE_USER_AGENT = "draftomen-guide/1 (+https://github.com/andreagrandi/draftomen)"

_ALLOWED_HOSTS = frozenset({"draftsim.com", "www.draftsim.com"})
_MISSING = object()
_MEDIA_TYPES = frozenset({"text/html", "text/plain"})
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_ERROR_MESSAGES = MappingProxyType(
    {
        "invalid_url": "Guide acquisition failed (invalid_url).",
        "unsafe_redirect": "Guide acquisition failed (unsafe_redirect).",
        "timeout": "Guide acquisition failed (timeout).",
        "transport": "Guide acquisition failed (transport).",
        "http": "Guide acquisition failed (http).",
        "unsupported_content": "Guide acquisition failed (unsupported_content).",
        "too_large": "Guide acquisition failed (too_large).",
        "truncated": "Guide acquisition failed (truncated).",
        "malformed_response": "Guide acquisition failed (malformed_response).",
    }
)


@dataclass(frozen=True)
class GuideDocument:
    """Represent one successfully fetched guide source."""

    url: str
    text: str
    sha256: str
    retrieved_at: str


class GuideClientError(RuntimeError):
    """Represent a bounded, classified guide acquisition failure."""

    code: str

    def __init__(self, *, code: str) -> None:
        try:
            message = _ERROR_MESSAGES[code]
        except (KeyError, TypeError):
            raise ValueError("Unknown guide acquisition error code.") from None
        self.code = code
        super().__init__(message)


def _raise(code: str) -> None:
    raise GuideClientError(code=code) from None


def _has_forbidden_characters(value: str) -> bool:
    return "\\" in value or "#" in value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _validate_url(url: Any, *, origin: tuple[str, str, int] | None = None) -> str:
    if (
        not isinstance(url, str)
        or not url
        or url != url.strip()
        or _has_forbidden_characters(url)
    ):
        _raise("invalid_url" if origin is None else "unsafe_redirect")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (ValueError, UnicodeError):
        _raise("invalid_url" if origin is None else "unsafe_redirect")
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or hostname.casefold() not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.netloc.endswith(":")
    ):
        _raise("invalid_url" if origin is None else "unsafe_redirect")
    actual_origin = ("https", hostname.casefold(), 443)
    if origin is not None and actual_origin != origin:
        _raise("unsafe_redirect")
    return url


def _url_origin(url: str) -> tuple[str, str, int]:
    _validate_url(url)
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if hostname is None:
        _raise("invalid_url")
    return "https", hostname.casefold(), 443


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _response_url(response: Any) -> str:
    getter = getattr(response, "geturl", None)
    if not callable(getter):
        _raise("malformed_response")
    try:
        value = getter()
    except Exception:
        _raise("malformed_response")
    if not isinstance(value, str) or not value:
        _raise("malformed_response")
    return value


def _response_status(response: Any) -> int:
    try:
        value = getattr(response, "status", _MISSING)
        if value is _MISSING or value is None:
            getter = getattr(response, "getcode", None)
            if not callable(getter):
                _raise("malformed_response")
            value = getter()
    except GuideClientError:
        raise
    except Exception:
        _raise("malformed_response")
    if isinstance(value, bool) or not isinstance(value, int):
        _raise("malformed_response")
    return value


def _headers(response: Any) -> Any:
    headers = getattr(response, "headers", _MISSING)
    if headers is _MISSING or headers is None:
        getter = getattr(response, "info", None)
        if not callable(getter):
            _raise("malformed_response")
        try:
            headers = getter()
        except Exception:
            _raise("malformed_response")
    if not callable(getattr(headers, "get_all", None)):
        _raise("malformed_response")
    return headers


def _header_values(headers: Any, name: str) -> list[Any] | None:
    try:
        values = headers.get_all(name)
    except Exception:
        _raise("malformed_response")
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        _raise("malformed_response")
    return list(values)


def _split_header_parameters(value: str) -> list[str] | None:
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quoted and character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == ";" and not quoted:
            parts.append(value[start:index])
            start = index + 1
    if quoted or escaped:
        return None
    parts.append(value[start:])
    return parts


def _validate_content_type(headers: Any) -> None:
    values = _header_values(headers, "Content-Type")
    if values is None or len(values) != 1 or not isinstance(values[0], str):
        _raise("unsupported_content" if values is None or len(values or []) == 0 else "malformed_response")
    raw = values[0]
    parts = _split_header_parameters(raw)
    if parts is None or not parts or not isinstance(parts[0], str):
        _raise("malformed_response")
    media_type = parts[0].strip().casefold()
    if (
        not media_type
        or media_type.count("/") != 1
        or any(not _TOKEN_RE.fullmatch(part.strip()) for part in media_type.split("/"))
    ):
        _raise("malformed_response")
    if media_type not in _MEDIA_TYPES:
        _raise("unsupported_content")
    charset: str | None = None
    for parameter in parts[1:]:
        parameter = parameter.strip()
        if not parameter:
            _raise("malformed_response")
        name, separator, value = parameter.partition("=")
        if not separator or not name.strip() or not value.strip():
            _raise("malformed_response")
        if name.strip().casefold() != "charset":
            continue
        if charset is not None:
            _raise("malformed_response")
        value = value.strip()
        if value.startswith('"'):
            if not value.endswith('"') or len(value) < 2:
                _raise("malformed_response")
            value = value[1:-1]
        elif '"' in value:
            _raise("malformed_response")
        if not value or not _TOKEN_RE.fullmatch(value):
            _raise("malformed_response")
        charset = value
    if charset is not None and charset.casefold() != "utf-8":
        _raise("unsupported_content")


def _validate_content_headers(headers: Any, *, max_bytes: int) -> int | None:
    _validate_content_type(headers)
    encoding = _header_values(headers, "Content-Encoding")
    if encoding is not None:
        if len(encoding) != 1 or not isinstance(encoding[0], str):
            _raise("malformed_response")
        if encoding[0].strip().casefold() != "identity":
            _raise("unsupported_content")
    lengths = _header_values(headers, "Content-Length")
    if lengths is None:
        return None
    if len(lengths) != 1 or not isinstance(lengths[0], str):
        _raise("malformed_response")
    value = lengths[0].strip()
    if not value or not value.isdecimal() or any(character not in "0123456789" for character in value):
        _raise("malformed_response")
    normalized = value.lstrip("0") or "0"
    limit = str(max_bytes)
    if len(normalized) > len(limit) or (len(normalized) == len(limit) and normalized > limit):
        _raise("too_large")
    return int(normalized)


def _read_bounded(response: Any, *, max_bytes: int, declared_length: int | None) -> bytes:
    payload = bytearray()
    while True:
        remaining = max_bytes - len(payload)
        try:
            chunk = response.read(min(64 * 1024, remaining + 1))
        except GuideClientError:
            raise
        except http.client.IncompleteRead:
            _raise("truncated")
        except TimeoutError:
            _raise("timeout")
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                _raise("timeout")
            _raise("transport")
        except OSError:
            _raise("transport")
        except Exception:
            _raise("transport")
        if chunk is None or not isinstance(chunk, (bytes, bytearray, memoryview)):
            _raise("malformed_response")
        chunk_bytes = bytes(chunk)
        if not chunk_bytes:
            break
        if len(payload) + len(chunk_bytes) > max_bytes:
            _raise("too_large")
        if declared_length is not None and len(payload) + len(chunk_bytes) > declared_length:
            _raise("malformed_response")
        payload.extend(chunk_bytes)
    if not payload:
        _raise("malformed_response")
    if declared_length is not None and len(payload) < declared_length:
        _raise("truncated")
    return bytes(payload)


def _redirect_target(headers: Any) -> str:
    getter = getattr(headers, "get_all", None)
    if not callable(getter):
        _raise("unsafe_redirect")
    try:
        location = getter("Location")
        if location is None:
            location = getter("URI")
    except Exception:
        _raise("unsafe_redirect")
    if location is None or isinstance(location, str) or not isinstance(location, (list, tuple)):
        _raise("unsafe_redirect")
    if len(location) != 1 or not isinstance(location[0], str) or not location[0]:
        _raise("unsafe_redirect")
    target = location[0]
    if _has_forbidden_characters(target):
        _raise("unsafe_redirect")
    return target


def _validate_redirect(url: str, *, origin: tuple[str, str, int]) -> str:
    try:
        return _validate_url(url, origin=origin)
    except GuideClientError:
        _raise("unsafe_redirect")


class _OriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects outside the initial Draftsim origin."""

    def __init__(self, origin: tuple[str, str, int]) -> None:
        super().__init__()
        self._origin = origin

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        _validate_redirect(newurl, origin=self._origin)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def _guarded_redirect(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        try:
            target = _redirect_target(headers)
            resolved = urljoin(req.full_url, target)
            _validate_redirect(resolved, origin=self._origin)
        except GuideClientError:
            raise
        except Exception:
            _raise("unsafe_redirect")
        finally:
            _close_response(fp)
        with io.BytesIO(b"") as empty_body:
            return super().http_error_302(req, empty_body, code, msg, headers)

    def http_error_301(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self._guarded_redirect(req, fp, code, msg, headers)

    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self._guarded_redirect(req, fp, code, msg, headers)

    def http_error_303(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self._guarded_redirect(req, fp, code, msg, headers)

    def http_error_307(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self._guarded_redirect(req, fp, code, msg, headers)

    def http_error_308(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self._guarded_redirect(req, fp, code, msg, headers)


class GuideClient:
    """Fetch one bounded Draftsim HTML or plain-text guide source."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] | None = None,
        timeout_seconds: float = GUIDE_TIMEOUT_SECONDS,
        max_bytes: int = GUIDE_MAX_BYTES,
    ) -> None:
        if opener is not None and not callable(opener):
            raise TypeError("opener must be callable.")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be positive.")
        try:
            timeout_value = float(timeout_seconds)
        except (OverflowError, ValueError):
            raise ValueError("timeout_seconds must be positive.") from None
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer.")
        self.opener = opener
        self.timeout_seconds = timeout_value
        self.max_bytes = max_bytes

    def fetch(self, *, url: str) -> GuideDocument:
        """Fetch and validate one guide without writing local state."""

        _validate_url(url)
        origin = _url_origin(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": GUIDE_ACCEPT,
                "Accept-Encoding": GUIDE_ACCEPT_ENCODING,
                "User-Agent": GUIDE_USER_AGENT,
            },
            method="GET",
        )
        opener = self.opener
        if opener is None:
            opener = urllib.request.build_opener(_OriginRedirectHandler(origin)).open
        response: Any = None
        try:
            try:
                response = opener(request, timeout=self.timeout_seconds)
            except GuideClientError:
                raise
            except urllib.error.HTTPError as error:
                _close_response(error)
                if isinstance(error.code, int) and 300 <= error.code < 400:
                    _raise("unsafe_redirect")
                _raise("http")
            except TimeoutError:
                _raise("timeout")
            except urllib.error.URLError as error:
                if isinstance(error.reason, TimeoutError):
                    _raise("timeout")
                _raise("transport")
            except OSError:
                _raise("transport")
            except Exception:
                _raise("transport")
            final_url = _response_url(response)
            _validate_redirect(final_url, origin=origin)
            status = _response_status(response)
            if 300 <= status < 400:
                _raise("unsafe_redirect")
            if status != 200:
                _raise("http")
            headers = _headers(response)
            declared_length = _validate_content_headers(headers, max_bytes=self.max_bytes)
            payload = _read_bounded(
                response,
                max_bytes=self.max_bytes,
                declared_length=declared_length,
            )
            try:
                text = payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                _raise("unsupported_content")
            return GuideDocument(
                url=final_url,
                text=text,
                sha256=hashlib.sha256(payload).hexdigest(),
                retrieved_at=datetime.now(timezone.utc).isoformat(),
            )
        except GuideClientError:
            raise
        finally:
            _close_response(response)


__all__ = ["GuideClient", "GuideClientError", "GuideDocument"]
