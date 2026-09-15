"""Offline-first runtime client for hosted per-set card data artifacts."""

from __future__ import annotations

from collections.abc import Callable
import math
import os
from os import PathLike
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, TypeAlias
from urllib.parse import urlsplit
import urllib.request


from draftomen.carddb import CardDatabase
from draftomen.paths import app_data_dir
from draftomen.set_card_data import SetCardData, SetCardDataError

_DEFAULT_URL_OPENER = urllib.request.urlopen

PathInput: TypeAlias = str | PathLike[str]
UrlOpener: TypeAlias = Callable[..., Any]

CARD_DATA_BASE_URL = "https://www.draftomen.com/card-data/"
CARD_DATA_TIMEOUT_SECONDS = 10.0
CARD_DATA_MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
CARD_DATA_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
CARD_DATA_USER_AGENT = (
    "draftomen-card-data/1 (+https://github.com/andreagrandi/draftomen)"
)
CARD_DATA_ACCEPT = "application/gzip, application/octet-stream"

_CARD_DATA_DIRECTORY = "card-data"
_SET_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class CardDataClientError(RuntimeError):
    """Raised when a card-data cache or hosted artifact cannot be used."""


class _CardDataPathLock:
    """A process-local lock shared by callers targeting one cache path."""

    _guard = threading.Lock()
    _locks: dict[Path, threading.Lock] = {}

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)
        with self._guard:
            self._lock = self._locks.setdefault(self.path, threading.Lock())

    def __enter__(self) -> _CardDataPathLock:
        self._lock.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self._lock.release()


class _OriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects away from the hosted card-data origin."""

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
        _validate_url(newurl, origin=self._origin)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _normalize_set_code(value: Any) -> str:
    """Normalize an external set lookup and reject unsafe path components."""

    if not isinstance(value, str) or not value:
        raise CardDataClientError("set_code must be a non-empty string.")
    normalized = value.casefold()
    if _SET_CODE_RE.fullmatch(normalized) is None:
        raise CardDataClientError(f"Unsafe set_code {value!r}.")
    return normalized


def card_data_cache_path(*, set_code: str, app_dir: PathInput | None = None) -> Path:
    """Return the local cache path for one normalized set artifact."""

    normalized_set_code = _normalize_set_code(set_code)
    root = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
    return root / _CARD_DATA_DIRECTORY / f"{normalized_set_code}.json.gz"


def cached_card_data_set_codes(*, app_dir: PathInput | None = None) -> tuple[str, ...]:
    """List normalized set codes with a locally cached card-data artifact.
    Missing, unreadable, and non-cache entries are skipped.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
    directory = root / _CARD_DATA_DIRECTORY
    if not directory.is_dir():
        return ()
    codes: list[str] = []
    try:
        for entry in directory.iterdir():
            if not entry.name.endswith(".json.gz") or not entry.is_file():
                continue
            set_code = entry.name.removesuffix(".json.gz")
            if _SET_CODE_RE.fullmatch(set_code) is not None:
                codes.append(set_code)
    except OSError:
        return ()
    return tuple(sorted(codes))


def _positive_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be positive.")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be positive.") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} must be positive.")
    return result


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _url_origin(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or url != url.strip() or any(
        character.isspace() or ord(character) < 32 for character in url
    ):
        raise CardDataClientError("Invalid card-data URL.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        # Accessing .port is intentional: malformed ports are deferred by
        # urllib.parse until this property is read.
        port = parsed.port
    except ValueError as error:
        raise CardDataClientError("Invalid card-data URL.") from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "#" in url
    ):
        raise CardDataClientError("Invalid card-data URL.")
    if port not in (None, 443) or parsed.netloc.endswith(":"):
        raise CardDataClientError("Invalid card-data URL port.")
    return "https", hostname.casefold(), 443


def _validate_url(url: str, *, origin: tuple[str, str, int] | None = None) -> str:
    actual = _url_origin(url)
    if origin is not None and actual != origin:
        raise CardDataClientError("Card-data redirect changed origin.")
    return url


def _response_url(response: Any) -> str | None:
    getter = getattr(response, "geturl", None)
    if callable(getter):
        value = getter()
        if isinstance(value, str) and value:
            return value
    value = getattr(response, "url", None)
    return value if isinstance(value, str) and value else None


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _response_status(response: Any) -> int | None:
    value = getattr(response, "status", None)
    if value is None:
        getter = getattr(response, "getcode", None)
        if callable(getter):
            value = getter()
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CardDataClientError("Card-data response status is invalid.")
    return value


def _read_bounded(response: Any, *, limit: int) -> bytes:
    payload = bytearray()
    while True:
        chunk = response.read(min(64 * 1024, limit - len(payload) + 1))
        if chunk in (b"", None):
            if chunk is None:
                raise CardDataClientError("Card-data response returned no byte chunk.")
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise CardDataClientError("Card-data response returned a non-byte chunk.")
        payload.extend(chunk)
        if len(payload) > limit:
            raise CardDataClientError(
                f"Card-data artifact exceeds compressed size limit ({limit} bytes)."
            )
    return bytes(payload)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _atomic_install(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
        _fsync_directory(destination.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


class CardDataClient:
    """Load one hosted set artifact with local-first, atomic caching."""

    def __init__(
        self,
        *,
        app_dir: PathInput | None = None,
        base_url: str = CARD_DATA_BASE_URL,
        opener: UrlOpener = urllib.request.urlopen,
        timeout_seconds: float = CARD_DATA_TIMEOUT_SECONDS,
        max_compressed_bytes: int = CARD_DATA_MAX_COMPRESSED_BYTES,
        max_decompressed_bytes: int = CARD_DATA_MAX_DECOMPRESSED_BYTES,
    ) -> None:
        self.app_dir = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
        if not callable(opener):
            raise TypeError("opener must be callable.")
        self.opener = opener
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self.max_compressed_bytes = _positive_int(
            max_compressed_bytes, "max_compressed_bytes"
        )
        self.max_decompressed_bytes = _positive_int(
            max_decompressed_bytes, "max_decompressed_bytes"
        )
        self.base_url = self._normalize_base_url(base_url)
        self._origin = _url_origin(self.base_url)

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url:
            raise CardDataClientError("base_url must be a non-empty string.")
        candidate = base_url if base_url.endswith("/") else f"{base_url}/"
        _validate_url(candidate)
        parsed = urlsplit(candidate)
        if parsed.query or parsed.fragment:
            raise CardDataClientError("base_url must not contain a query or fragment.")
        return candidate

    def cache_path(self, set_code: str) -> Path:
        """Return the local path used for one set's cached artifact."""

        return card_data_cache_path(set_code=set_code, app_dir=self.app_dir)

    def load(self, set_code: str, *, allow_network: bool) -> CardDatabase:
        """Load a validated set database, optionally downloading one artifact."""

        normalized_set_code = _normalize_set_code(set_code)
        destination = card_data_cache_path(
            set_code=normalized_set_code,
            app_dir=self.app_dir,
        )
        with _CardDataPathLock(destination):
            cached = self._load_cached(destination, normalized_set_code)
            if cached is not None:
                return cached.to_card_database()
            if not allow_network:
                raise CardDataClientError(
                    f"Card-data cache missing or invalid for set {normalized_set_code!r}; "
                    "network access is disabled."
                )
            try:
                payload = self._fetch(normalized_set_code)
                card_data = SetCardData.from_gzip_bytes(
                    payload,
                    max_decompressed_bytes=self.max_decompressed_bytes,
                    expected_set_code=normalized_set_code,
                )
            except CardDataClientError:
                raise
            except (OSError, SetCardDataError, TypeError, ValueError) as error:
                raise CardDataClientError(
                    f"Hosted card-data artifact for set {normalized_set_code!r} is invalid."
                ) from error
            try:
                _atomic_install(destination, payload)
            except OSError as error:
                raise CardDataClientError(
                    f"Could not install card-data cache for set {normalized_set_code!r}."
                ) from error
            return card_data.to_card_database()

    def _load_cached(self, destination: Path, set_code: str) -> SetCardData | None:
        try:
            payload = destination.read_bytes()
            return SetCardData.from_gzip_bytes(
                payload,
                max_decompressed_bytes=self.max_decompressed_bytes,
                expected_set_code=set_code,
            )
        except (OSError, SetCardDataError, TypeError, ValueError):
            return None

    def _fetch(self, set_code: str) -> bytes:
        url = f"{self.base_url}{set_code}.json.gz"
        _validate_url(url, origin=self._origin)
        request = urllib.request.Request(
            url,
            headers={"Accept": CARD_DATA_ACCEPT, "User-Agent": CARD_DATA_USER_AGENT},
            method="GET",
        )
        try:
            opener = self.opener
            if opener is _DEFAULT_URL_OPENER:
                # Respect a test/application monkeypatch of urlopen while
                # retaining redirect protection for the real default opener.
                if urllib.request.urlopen is not _DEFAULT_URL_OPENER:
                    opener = urllib.request.urlopen
                else:
                    opener = urllib.request.build_opener(
                        _OriginRedirectHandler(self._origin)
                    ).open
            response = opener(request, timeout=self.timeout_seconds)
        except CardDataClientError:
            raise
        except Exception as error:
            raise CardDataClientError(
                f"Could not fetch card-data artifact for set {set_code!r}."
            ) from error
        try:
            final_url = _response_url(response) or url
            _validate_url(final_url, origin=self._origin)
            status = _response_status(response)
            if status is not None and status != 200:
                raise CardDataClientError(
                    f"Hosted card-data request returned HTTP status {status}."
                )
            return _read_bounded(response, limit=self.max_compressed_bytes)
        except CardDataClientError:
            raise
        except Exception as error:
            raise CardDataClientError(
                f"Could not read card-data artifact for set {set_code!r}."
            ) from error
        finally:
            _close_response(response)


__all__ = [
    "CARD_DATA_ACCEPT",
    "CARD_DATA_BASE_URL",
    "CARD_DATA_MAX_COMPRESSED_BYTES",
    "CARD_DATA_MAX_DECOMPRESSED_BYTES",
    "CARD_DATA_TIMEOUT_SECONDS",
    "CARD_DATA_USER_AGENT",
    "CardDataClient",
    "CardDataClientError",
    "card_data_cache_path",
    "cached_card_data_set_codes",
]
