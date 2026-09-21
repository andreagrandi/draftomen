"""Offline-first runtime client for hosted per-set augmentation artifacts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import hashlib
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

from draftomen.augmented_artifact import (
    AUGMENTED_ARTIFACT_COMPATIBILITY,
    AUGMENTED_ARTIFACT_SCHEMA_VERSION,
    AugmentedArtifact,
    AugmentedArtifactError,
    AugmentedArtifactIncompatibleError,
)
from draftomen.augmented_manifest import (
    AugmentedManifest,
    AugmentedManifestError,
)
from draftomen.paths import app_data_dir

_DEFAULT_URL_OPENER = urllib.request.urlopen

PathInput: TypeAlias = str | PathLike[str]
UrlOpener: TypeAlias = Callable[..., Any]

AUGMENTED_MANIFEST_URL = "https://www.draftomen.com/augmented/manifest.json"
AUGMENTED_OBJECTS_BASE_URL = "https://www.draftomen.com/augmented/objects/"
AUGMENTED_TIMEOUT_SECONDS = 10.0
AUGMENTED_MANIFEST_MAX_BYTES = 1 * 1024 * 1024
AUGMENTED_ARTIFACT_MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
AUGMENTED_ARTIFACT_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
AUGMENTED_USER_AGENT = (
    "draftomen-augmented-model/1 (+https://github.com/andreagrandi/draftomen)"
)
AUGMENTED_MANIFEST_ACCEPT = "application/json"
AUGMENTED_ARTIFACT_ACCEPT = "application/gzip, application/octet-stream"

_AUGMENTED_DIRECTORY = "augmented"
_SET_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class AugmentedModelClientError(RuntimeError):
    """Report an unusable cache path, configuration, or network failure."""


class _AugmentedOriginError(AugmentedModelClientError):
    """Report a redirect or final URL outside the hosted origin."""


class _AugmentedLimitError(AugmentedModelClientError):
    """Report a response that exceeds its trusted byte limit."""


class AugmentedModelOutcome(StrEnum):
    CACHED = "cached"
    DOWNLOADED = "downloaded"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"
    INVALID = "invalid"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class AugmentedModelLoad:
    outcome: AugmentedModelOutcome
    set_code: str
    artifact: AugmentedArtifact | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, AugmentedModelOutcome):
            raise AugmentedModelClientError("outcome must be an AugmentedModelOutcome.")
        if not isinstance(self.set_code, str) or not self.set_code:
            raise AugmentedModelClientError("set_code must be a non-empty string.")
        if not isinstance(self.message, str):
            raise AugmentedModelClientError("message must be a string.")
        if self.outcome in (
            AugmentedModelOutcome.CACHED,
            AugmentedModelOutcome.DOWNLOADED,
        ):
            if not isinstance(self.artifact, AugmentedArtifact):
                raise AugmentedModelClientError(
                    "artifact is required for cached and downloaded outcomes."
                )
        elif self.artifact is not None:
            raise AugmentedModelClientError(
                "artifact must be absent for unavailable outcomes."
            )

    @property
    def available(self) -> bool:
        return self.outcome in (
            AugmentedModelOutcome.CACHED,
            AugmentedModelOutcome.DOWNLOADED,
        )

    @property
    def status(self) -> str:
        return self.outcome.value


class _AugmentedPathLock:
    """A process-local lock shared by callers targeting one cache path."""

    _guard = threading.Lock()
    _locks: dict[Path, threading.Lock] = {}

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)
        with self._guard:
            self._lock = self._locks.setdefault(self.path, threading.Lock())

    def __enter__(self) -> _AugmentedPathLock:
        self._lock.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self._lock.release()


class _OriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects away from the hosted augmented-model origin."""

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
        raise AugmentedModelClientError("set_code must be a non-empty string.")
    normalized = value.casefold()
    if _SET_CODE_RE.fullmatch(normalized) is None:
        raise AugmentedModelClientError(f"Unsafe set_code {value!r}.")
    return normalized


def augmented_cache_path(
    *, set_code: str, app_dir: PathInput | None = None
) -> Path:
    """Return the local cache path for one normalized set artifact."""

    normalized_set_code = _normalize_set_code(set_code)
    root = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
    return root / _AUGMENTED_DIRECTORY / f"{normalized_set_code}.json.gz"


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
        raise AugmentedModelClientError("Invalid augmented model URL.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise AugmentedModelClientError("Invalid augmented model URL.") from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "#" in url
    ):
        raise AugmentedModelClientError("Invalid augmented model URL.")
    if port not in (None, 443) or parsed.netloc.endswith(":"):
        raise AugmentedModelClientError("Invalid augmented model URL port.")
    return "https", hostname.casefold(), 443


def _validate_url(url: str, *, origin: tuple[str, str, int] | None = None) -> str:
    actual = _url_origin(url)
    if origin is not None and actual != origin:
        raise _AugmentedOriginError("Augmented model redirect changed origin.")
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
        raise AugmentedModelClientError("Augmented model response status is invalid.")
    return value


def _read_bounded(response: Any, *, limit: int) -> bytes:
    payload = bytearray()
    while True:
        chunk = response.read(min(64 * 1024, limit - len(payload) + 1))
        if chunk in (b"", None):
            if chunk is None:
                raise AugmentedModelClientError(
                    "Augmented model response returned no byte chunk."
                )
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise AugmentedModelClientError(
                "Augmented model response returned a non-byte chunk."
            )
        payload.extend(chunk)
        if len(payload) > limit:
            raise _AugmentedLimitError(
                f"Augmented model artifact exceeds compressed size limit ({limit} bytes)."
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


def _unavailable(
    *, code: str, outcome: AugmentedModelOutcome, reason: str
) -> AugmentedModelLoad:
    return AugmentedModelLoad(
        outcome=outcome,
        set_code=code,
        message=f"Augmented model for set {code!r} is unavailable: {reason}.",
    )


class AugmentedModelClient:
    """Load one hosted augmentation artifact with local-first, atomic caching."""

    def __init__(
        self,
        *,
        app_dir: PathInput | None = None,
        manifest_url: str = AUGMENTED_MANIFEST_URL,
        objects_base_url: str = AUGMENTED_OBJECTS_BASE_URL,
        opener: UrlOpener = urllib.request.urlopen,
        timeout_seconds: float = AUGMENTED_TIMEOUT_SECONDS,
        max_manifest_bytes: int = AUGMENTED_MANIFEST_MAX_BYTES,
        max_compressed_bytes: int = AUGMENTED_ARTIFACT_MAX_COMPRESSED_BYTES,
        max_decompressed_bytes: int = AUGMENTED_ARTIFACT_MAX_DECOMPRESSED_BYTES,
    ) -> None:
        self.app_dir = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
        if not callable(opener):
            raise TypeError("opener must be callable.")
        self.opener = opener
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self.max_manifest_bytes = _positive_int(
            max_manifest_bytes, "max_manifest_bytes"
        )
        self.max_compressed_bytes = _positive_int(
            max_compressed_bytes, "max_compressed_bytes"
        )
        self.max_decompressed_bytes = _positive_int(
            max_decompressed_bytes, "max_decompressed_bytes"
        )
        self.manifest_url = self._normalize_manifest_url(manifest_url)
        self.objects_base_url = self._normalize_objects_base_url(objects_base_url)
        manifest_origin = _url_origin(self.manifest_url)
        objects_origin = _url_origin(self.objects_base_url)
        if manifest_origin != objects_origin:
            raise AugmentedModelClientError(
                "Augmented model manifest and objects must share one origin."
            )
        self._origin = manifest_origin

    @staticmethod
    def _normalize_manifest_url(manifest_url: str) -> str:
        if not isinstance(manifest_url, str) or not manifest_url:
            raise AugmentedModelClientError("manifest_url must be a non-empty string.")
        _validate_url(manifest_url)
        parsed = urlsplit(manifest_url)
        if parsed.query or parsed.fragment:
            raise AugmentedModelClientError(
                "manifest_url must not contain a query or fragment."
            )
        return manifest_url

    @staticmethod
    def _normalize_objects_base_url(objects_base_url: str) -> str:
        if not isinstance(objects_base_url, str) or not objects_base_url:
            raise AugmentedModelClientError(
                "objects_base_url must be a non-empty string."
            )
        candidate = (
            objects_base_url
            if objects_base_url.endswith("/")
            else f"{objects_base_url}/"
        )
        _validate_url(candidate)
        parsed = urlsplit(candidate)
        if parsed.query or parsed.fragment:
            raise AugmentedModelClientError(
                "objects_base_url must not contain a query or fragment."
            )
        return candidate

    def cache_path(self, set_code: str) -> Path:
        """Return the local path used for one set's cached artifact."""

        return augmented_cache_path(set_code=set_code, app_dir=self.app_dir)

    def load(self, set_code: str, *, allow_network: bool) -> AugmentedModelLoad:
        """Load a validated augmentation artifact, optionally downloading it."""

        code = _normalize_set_code(set_code)
        destination = augmented_cache_path(set_code=code, app_dir=self.app_dir)
        with _AugmentedPathLock(destination):
            cached, cached_error = self._load_cached(destination, code)
            if not allow_network:
                if cached is not None:
                    return AugmentedModelLoad(
                        outcome=AugmentedModelOutcome.CACHED,
                        set_code=code,
                        artifact=cached,
                        message=(
                            f"Augmented model for set {code!r} "
                            "is available from the local cache."
                        ),
                    )
                if cached_error is not None:
                    if isinstance(cached_error, AugmentedArtifactIncompatibleError):
                        return _unavailable(
                            code=code,
                            outcome=AugmentedModelOutcome.INCOMPATIBLE,
                            reason="the cached artifact is incompatible with this build",
                        )
                    return _unavailable(
                        code=code,
                        outcome=AugmentedModelOutcome.INVALID,
                        reason="the cached artifact is invalid",
                    )
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.UNREACHABLE,
                    reason=(
                        "network access is disabled and no cached artifact exists"
                    ),
                )
            try:
                manifest_bytes = self._fetch(self.manifest_url, AUGMENTED_MANIFEST_ACCEPT)
            except AugmentedModelClientError as error:
                if cached is not None:
                    return AugmentedModelLoad(
                        outcome=AugmentedModelOutcome.CACHED,
                        set_code=code,
                        artifact=cached,
                        message=(
                            f"Augmented model for set {code!r} "
                            "is available from the local cache."
                        ),
                    )
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.UNREACHABLE,
                    reason=f"the manifest could not be fetched ({error})",
                )
            try:
                manifest = AugmentedManifest.from_bytes(manifest_bytes)
            except AugmentedManifestError as error:
                if cached is not None:
                    return AugmentedModelLoad(
                        outcome=AugmentedModelOutcome.CACHED,
                        set_code=code,
                        artifact=cached,
                        message=(
                            f"Augmented model for set {code!r} "
                            "is available from the local cache."
                        ),
                    )
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.INVALID,
                    reason=f"the published manifest is invalid ({error})",
                )
            entry = manifest.select(set_code=code)
            if entry is None:
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.MISSING,
                    reason="the published manifest has no entry for set "
                    f"{code!r}",
                )
            if (
                entry.artifact_schema_version != AUGMENTED_ARTIFACT_SCHEMA_VERSION
                or entry.compatibility != AUGMENTED_ARTIFACT_COMPATIBILITY
            ):
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.INCOMPATIBLE,
                    reason="the published artifact is incompatible with this build",
                )
            if entry.artifact_bytes > self.max_decompressed_bytes:
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.INVALID,
                    reason="declared artifact size exceeds the decompressed limit",
                )
            if cached is not None and self._cached_checksum(destination) == entry.artifact_sha256:
                return AugmentedModelLoad(
                    outcome=AugmentedModelOutcome.CACHED,
                    set_code=code,
                    artifact=cached,
                    message=(
                        f"Augmented model for set {code!r} "
                        "is available from the local cache."
                    ),
                )
            object_url = f"{self.objects_base_url}{entry.artifact_sha256}.json.gz"
            try:
                payload = self._fetch(object_url, AUGMENTED_ARTIFACT_ACCEPT)
            except (_AugmentedOriginError, _AugmentedLimitError) as error:
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.INVALID,
                    reason=f"the artifact could not be fetched ({error})",
                )
            except AugmentedModelClientError as error:
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.UNREACHABLE,
                    reason=f"the artifact could not be fetched ({error})",
                )
            try:
                artifact = AugmentedArtifact.from_gzip_bytes(
                    payload,
                    expected_set_code=code,
                    expected_compatibility=AUGMENTED_ARTIFACT_COMPATIBILITY,
                    expected_sha256=entry.artifact_sha256,
                    expected_byte_size=entry.artifact_bytes,
                    max_decompressed_bytes=self.max_decompressed_bytes,
                )
            except AugmentedArtifactIncompatibleError:
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.INCOMPATIBLE,
                    reason="the published artifact is incompatible with this build",
                )
            except (AugmentedArtifactError, TypeError, ValueError):
                return _unavailable(
                    code=code,
                    outcome=AugmentedModelOutcome.INVALID,
                    reason="the published artifact failed validation",
                )
            try:
                _atomic_install(destination, payload)
            except OSError:
                return AugmentedModelLoad(
                    outcome=AugmentedModelOutcome.DOWNLOADED,
                    set_code=code,
                    artifact=artifact,
                    message=(
                        f"Augmented model for set {code!r} was downloaded "
                        "but could not be written to the local cache."
                    ),
                )
            return AugmentedModelLoad(
                outcome=AugmentedModelOutcome.DOWNLOADED,
                set_code=code,
                artifact=artifact,
                message=(
                    f"Augmented model for set {code!r} was downloaded and cached."
                ),
            )

    def _load_cached(
        self, destination: Path, code: str
    ) -> tuple[AugmentedArtifact | None, Exception | None]:
        try:
            payload = destination.read_bytes()
        except OSError:
            return None, None
        try:
            return (
                AugmentedArtifact.from_gzip_bytes(
                    payload,
                    expected_set_code=code,
                    expected_compatibility=AUGMENTED_ARTIFACT_COMPATIBILITY,
                ),
                None,
            )
        except (AugmentedArtifactError, TypeError, ValueError) as error:
            return None, error

    def _cached_checksum(self, destination: Path) -> str | None:
        try:
            return hashlib.sha256(destination.read_bytes()).hexdigest()
        except OSError:
            return None

    def _fetch(self, url: str, accept: str) -> bytes:
        _validate_url(url, origin=self._origin)
        request = urllib.request.Request(
            url,
            headers={"Accept": accept, "User-Agent": AUGMENTED_USER_AGENT},
            method="GET",
        )
        try:
            opener = self.opener
            if opener is _DEFAULT_URL_OPENER:
                if urllib.request.urlopen is not _DEFAULT_URL_OPENER:
                    opener = urllib.request.urlopen
                else:
                    opener = urllib.request.build_opener(
                        _OriginRedirectHandler(self._origin)
                    ).open
            response = opener(request, timeout=self.timeout_seconds)
        except AugmentedModelClientError:
            raise
        except Exception as error:
            raise AugmentedModelClientError(
                "Could not fetch augmented model artifact."
            ) from error
        try:
            final_url = _response_url(response) or url
            try:
                _validate_url(final_url, origin=self._origin)
            except _AugmentedOriginError:
                raise
            except AugmentedModelClientError as error:
                raise _AugmentedOriginError(
                    "Augmented model redirect changed origin."
                ) from error
            status = _response_status(response)
            if status is not None and status != 200:
                raise AugmentedModelClientError(
                    f"Hosted augmented model request returned HTTP status {status}."
                )
            limit = (
                self.max_manifest_bytes
                if url == self.manifest_url
                else self.max_compressed_bytes
            )
            return _read_bounded(response, limit=limit)
        except AugmentedModelClientError:
            raise
        except Exception as error:
            raise AugmentedModelClientError(
                "Could not read augmented model artifact."
            ) from error
        finally:
            _close_response(response)


__all__ = [
    "AUGMENTED_ARTIFACT_ACCEPT",
    "AUGMENTED_ARTIFACT_MAX_COMPRESSED_BYTES",
    "AUGMENTED_ARTIFACT_MAX_DECOMPRESSED_BYTES",
    "AUGMENTED_MANIFEST_ACCEPT",
    "AUGMENTED_MANIFEST_MAX_BYTES",
    "AUGMENTED_MANIFEST_URL",
    "AUGMENTED_OBJECTS_BASE_URL",
    "AUGMENTED_TIMEOUT_SECONDS",
    "AUGMENTED_USER_AGENT",
    "AugmentedModelClient",
    "AugmentedModelClientError",
    "AugmentedModelLoad",
    "AugmentedModelOutcome",
    "augmented_cache_path",
]
