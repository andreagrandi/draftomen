"""Offline-first remote set-profile manifest and cache client.

The client stores only canonical, uncompressed :class:`SetProfile` JSON in the
same flat cache used by ``set_profile_path``.  Network failures are deliberately
contained at the refresh boundary: callers always receive the last validated
profile or the generic profile fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, TypeAlias
from urllib.parse import urlsplit
import urllib.request
import zlib

from draftomen.paths import app_data_dir
from draftomen.profile_manifest import (
    PROFILE_MANIFEST_SCHEMA_VERSION,
    ProfileManifest,
    ProfileManifestArtifact,
    ProfileManifestError,
)
from draftomen.set_profile import (
    ProfileMaturity,
    SetProfile,
    SetProfileError,
    SetProfileLoadResult,
    SetProfileSchemaError,
    _safe_component,
    load_set_profile,
    set_profile_path,
)

PathInput: TypeAlias = str | os.PathLike[str]
ProfileOpener: TypeAlias = Callable[..., Any]
ProfileClock: TypeAlias = Callable[[], datetime | float | int]

REMOTE_PROFILE_MANIFEST_SCHEMA_VERSION = PROFILE_MANIFEST_SCHEMA_VERSION
PROFILE_CLIENT_USER_AGENT = "draftomen-set-profile/1"
PROFILE_CLIENT_TIMEOUT_SECONDS = 10.0
PROFILE_MANIFEST_TTL_SECONDS = 24 * 60 * 60
PROFILE_MANIFEST_MAX_BYTES = 1 * 1024 * 1024
PROFILE_ARTIFACT_MAX_GZIP_BYTES = 64 * 1024 * 1024
PROFILE_ARTIFACT_MAX_PROFILE_BYTES = 128 * 1024 * 1024
BUNDLED_PROFILE_SET_CODE = "hob"
BUNDLED_PROFILE_EVENT_FORMAT = "quickdraft"
BUNDLED_PROFILE_BYTES = 270
BUNDLED_PROFILE_SHA256 = "b95f64f7775cf5c20beb83531062a49bceff695fd9d9fb0e1d4132fce4396dd2"
_DEFAULT_BUNDLED_PROFILE_PATH = Path(__file__).resolve().parent / "baseline_profiles" / "hob-quickdraft.json"


class ProfileClientError(RuntimeError):
    """Raised internally for an invalid remote manifest or artifact."""


class ProfileNetworkPolicy(str, Enum):
    """Whether a refresh may access the configured remote manifest."""

    OFFLINE = "offline"
    ALLOWED = "allowed"

class ProfileRefreshOutcome(str, Enum):
    """Structured outcome for one synchronous refresh attempt."""

    OFFLINE = "offline"
    CACHED = "cached"
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    MISSING = "missing"
    STALE_MANIFEST = "stale-manifest"
    MANIFEST_INVALID = "manifest-invalid"
    ARTIFACT_INVALID = "artifact-invalid"
    REMOTE_FAILED = "remote-failed"




@dataclass(frozen=True, slots=True)
class ProfileRefreshResult:
    """Refresh result retaining a usable local profile on every failure."""

    profile: SetProfile
    outcome: ProfileRefreshOutcome | str
    diagnostics: tuple[str, ...] = ()
    manifest: ProfileManifest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile, SetProfile):
            raise TypeError("profile must be a SetProfile")
        try:
            outcome = self.outcome if isinstance(self.outcome, ProfileRefreshOutcome) else ProfileRefreshOutcome(self.outcome)
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported profile refresh outcome") from error
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "diagnostics", tuple(dict.fromkeys(str(item) for item in self.diagnostics)))
        if self.manifest is not None and not isinstance(self.manifest, ProfileManifest):
            raise TypeError("manifest must be a ProfileManifest or None")

    @property
    def status(self) -> str:
        """Return a compact string suitable for session/status presentation."""

        return self.outcome.value

    @property
    def maturity(self) -> ProfileMaturity:
        """Expose the usable profile maturity without unpacking the result."""

        return self.profile.maturity


@dataclass(frozen=True, slots=True)
class _ManifestCache:
    manifest: ProfileManifest
    checked_at: datetime
    manifest_url: str


class _OriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects to an origin other than the configured manifest origin."""

    def __init__(self, origin: tuple[str, str, int]) -> None:
        super().__init__()
        self._origin = origin

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _validate_url(newurl, origin=self._origin)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ProfileLock:
    """A process-local per-cache-key lock."""

    _guard = threading.Lock()
    _locks: dict[Path, threading.Lock] = {}

    def __init__(self, path: Path) -> None:
        self.path = path
        with self._guard:
            self._lock = self._locks.setdefault(path, threading.Lock())

    def __enter__(self) -> _ProfileLock:
        self._lock.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self._lock.release()


class ProfileClient:
    """Validate, install, and read remote profiles without blocking readers."""

    def __init__(
        self,
        app_dir: PathInput | None = None,
        *,
        manifest_url: str | None = None,
        network_policy: ProfileNetworkPolicy | str = ProfileNetworkPolicy.ALLOWED,
        opener: ProfileOpener | None = None,
        clock: ProfileClock | None = None,
        timeout_seconds: float = PROFILE_CLIENT_TIMEOUT_SECONDS,
        manifest_ttl_seconds: float = PROFILE_MANIFEST_TTL_SECONDS,
        manifest_max_bytes: int = PROFILE_MANIFEST_MAX_BYTES,
        max_gzip_bytes: int = PROFILE_ARTIFACT_MAX_GZIP_BYTES,
        max_profile_bytes: int = PROFILE_ARTIFACT_MAX_PROFILE_BYTES,
        bundled_profile_path: PathInput | None = None,
    ) -> None:
        self.app_dir = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
        self.manifest_url = None if manifest_url is None else _validate_url(manifest_url)
        self.network_policy = _network_policy(network_policy)
        self.opener = opener or self._default_opener
        if not callable(self.opener):
            raise TypeError("opener must be callable")
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        if not callable(self.clock):
            raise TypeError("clock must be callable")
        self.timeout_seconds = _positive_float(timeout_seconds, "timeout_seconds")
        self.manifest_ttl_seconds = _non_negative_float(manifest_ttl_seconds, "manifest_ttl_seconds")
        self.manifest_max_bytes = _positive_int(manifest_max_bytes, "manifest_max_bytes")
        self.max_gzip_bytes = _positive_int(max_gzip_bytes, "max_gzip_bytes")
        self.max_profile_bytes = _positive_int(max_profile_bytes, "max_profile_bytes")
        self.bundled_profile_path = Path(
            _DEFAULT_BUNDLED_PROFILE_PATH if bundled_profile_path is None else bundled_profile_path
        ).expanduser()

    def profile_path(self, set_code: str, event_format: str) -> Path:
        """Return the existing flat authoritative set-profile cache path."""

        return set_profile_path(set_code=set_code, event_format=event_format, app_dir=self.app_dir)

    def manifest_path(self) -> Path:
        """Return the local, non-authoritative manifest validation cache path."""

        return self.app_dir / "set-profiles" / "v1" / "manifest.json"

    def load_cached(
        self,
        set_code: str,
        event_format: str,
        *,
        last_valid_profile: SetProfile | None = None,
    ) -> SetProfileLoadResult:
        """Load only local files, adopting a validated historical cache if present.

        This method never constructs a request and never invokes ``opener``.
        """

        normalized_set = _safe_component(set_code, "set_code")
        normalized_format = _safe_component(event_format, "format")
        destination = self.profile_path(normalized_set, normalized_format)
        diagnostics: list[str] = []

        profile = _load_local_profile(destination, normalized_set, normalized_format)
        if profile is not None and profile.maturity is not ProfileMaturity.GENERIC:
            return SetProfileLoadResult(
                profile=profile,
                source=f"local-{profile.maturity.value}",
                diagnostics=tuple(diagnostics),
            )
        if destination.exists():
            diagnostics.append("rejected-cache:generic" if profile is not None else "rejected-cache")

        # Historical releases used a versioned or top-level profiles directory.
        # Read those candidates locally and migrate only after a lock-protected
        # recheck of the destination.  Writes remain at set_profile_path's flat
        # location, so there is one authoritative cache for future readers.
        for legacy in _historical_profile_paths(self.app_dir, normalized_set, normalized_format, destination):
            profile = _load_local_profile(legacy, normalized_set, normalized_format)
            if profile is None or profile.maturity is ProfileMaturity.GENERIC:
                if legacy.exists():
                    diagnostics.append("rejected-legacy:generic" if profile is not None else "rejected-legacy")
                continue
            try:
                with _ProfileLock(destination):
                    current = _load_local_profile(destination, normalized_set, normalized_format)
                    if current is not None and current.maturity is not ProfileMaturity.GENERIC:
                        return SetProfileLoadResult(
                            profile=current,
                            source=f"local-{current.maturity.value}",
                            diagnostics=tuple(diagnostics),
                        )
                    _atomic_write_bytes(destination, profile.to_bytes())
            except OSError as error:
                diagnostics.append(f"legacy-migration:{type(error).__name__}")
                return SetProfileLoadResult(
                    profile=profile,
                    source=f"legacy-{profile.maturity.value}",
                    diagnostics=tuple(diagnostics),
                )
            return SetProfileLoadResult(
                profile=profile,
                source=f"legacy-migrated-{profile.maturity.value}",
                diagnostics=tuple(diagnostics),
            )
        if normalized_set == BUNDLED_PROFILE_SET_CODE and normalized_format == BUNDLED_PROFILE_EVENT_FORMAT:
            profile, diagnostic = _load_bundled_profile(self.bundled_profile_path)
            if profile is not None:
                return SetProfileLoadResult(
                    profile=profile,
                    source="bundled-metadata-only",
                    diagnostics=tuple(diagnostics),
                )
            if diagnostic is not None:
                diagnostics.append(diagnostic)

        if last_valid_profile is not None:
            try:
                if (
                    not isinstance(last_valid_profile, SetProfile)
                    or last_valid_profile.set_code != normalized_set
                    or last_valid_profile.event_format != normalized_format
                ):
                    raise SetProfileError("last_valid_profile does not match the requested set and format")
            except (SetProfileError, TypeError, ValueError) as error:
                diagnostics.append(f"rejected-last-valid:{type(error).__name__}")
            else:
                return SetProfileLoadResult(
                    profile=last_valid_profile,
                    source="last-valid",
                    diagnostics=tuple(diagnostics),
                )

        fallback = SetProfile.generic(set_code=normalized_set, event_format=normalized_format)
        return SetProfileLoadResult(profile=fallback, source="generic", diagnostics=tuple(diagnostics))

    def refresh(
        self,
        set_code: str,
        event_format: str,
        *,
        force: bool = False,
        network_policy: ProfileNetworkPolicy | str | None = None,
    ) -> ProfileRefreshResult:
        """Synchronously refresh one profile while retaining the last good cache."""

        normalized_set = _safe_component(set_code, "set_code")
        normalized_format = _safe_component(event_format, "format")
        cached_result = self.load_cached(normalized_set, normalized_format)
        cached_profile = cached_result.profile
        diagnostics = list(cached_result.diagnostics)
        selected_policy = self.network_policy if network_policy is None else network_policy
        selected_policy = _network_policy(selected_policy)

        if self.manifest_url is None or selected_policy is ProfileNetworkPolicy.OFFLINE:
            diagnostics.append("offline:no-network" if selected_policy is ProfileNetworkPolicy.OFFLINE else "offline:no-manifest-url")
            return ProfileRefreshResult(cached_profile, _offline_outcome(cached_profile), tuple(diagnostics))

        manifest: ProfileManifest | None = None
        try:
            now = self._now()
            cached_manifest = self._read_manifest_cache(diagnostics)
            if (
                not force
                and cached_manifest is not None
                and (now - cached_manifest.checked_at).total_seconds() < self.manifest_ttl_seconds
            ):
                manifest = cached_manifest.manifest
            else:
                fetched = self._fetch_manifest()
                if cached_manifest is not None:
                    fetched_published_at = _aware_datetime(fetched.published_at, "manifest published_at")
                    cached_published_at = _aware_datetime(cached_manifest.manifest.published_at, "cached manifest published_at")
                    if fetched_published_at < cached_published_at:
                        diagnostics.append("manifest:stale")
                        return ProfileRefreshResult(
                            cached_profile,
                            ProfileRefreshOutcome.STALE_MANIFEST,
                            tuple(diagnostics),
                            cached_manifest.manifest,
                        )
                    if (
                        fetched_published_at == cached_published_at
                        and fetched.to_bytes() != cached_manifest.manifest.to_bytes()
                    ):
                        diagnostics.append("manifest:conflicting-published-at")
                        return ProfileRefreshResult(
                            cached_profile,
                            ProfileRefreshOutcome.STALE_MANIFEST,
                            tuple(diagnostics),
                            cached_manifest.manifest,
                        )
                manifest = fetched
                if not self._write_manifest_cache(
                    _ManifestCache(manifest=manifest, checked_at=now, manifest_url=self.manifest_url),
                    diagnostics,
                ):
                    diagnostics.append("manifest-cache:write-failed")

            if manifest is None:  # pragma: no cover - guarded by the branches above
                raise ProfileClientError("manifest-unavailable")
            artifact = manifest.select(set_code=normalized_set, event_format=normalized_format)
            if artifact is None:
                diagnostics.append("manifest:no-requested-artifact")
                return ProfileRefreshResult(cached_profile, ProfileRefreshOutcome.MISSING, tuple(diagnostics), manifest)
            try:
                _validate_url(artifact.url, origin=_url_origin(self.manifest_url))
            except ProfileClientError as error:
                diagnostics.append(f"artifact:{_error_code(error)}")
                return ProfileRefreshResult(
                    cached_profile,
                    ProfileRefreshOutcome.ARTIFACT_INVALID,
                    tuple(diagnostics),
                    manifest,
                )
            if _same_profile_identity(cached_profile, artifact):
                return ProfileRefreshResult(cached_profile, ProfileRefreshOutcome.UNCHANGED, tuple(diagnostics), manifest)
            relation = _artifact_relation(cached_profile, artifact)
            if relation != "newer":
                diagnostics.append(f"artifact:{relation}")
                return ProfileRefreshResult(cached_profile, ProfileRefreshOutcome.STALE_MANIFEST, tuple(diagnostics), manifest)
        except ProfileClientError as error:
            diagnostics.append(f"manifest:{_error_code(error)}")
            return ProfileRefreshResult(
                cached_profile,
                _manifest_failure_outcome(error),
                tuple(diagnostics),
                manifest,
            )
        except Exception as error:
            diagnostics.append(f"manifest:{type(error).__name__}")
            return ProfileRefreshResult(
                cached_profile,
                ProfileRefreshOutcome.REMOTE_FAILED,
                tuple(diagnostics),
                manifest,
            )

        staged_path: Path | None = None
        try:
            staged_path, staged_profile = self._stage_artifact(artifact, destination=self.profile_path(normalized_set, normalized_format))
        except ProfileClientError as error:
            diagnostics.append(f"artifact:{_error_code(error)}")
            return ProfileRefreshResult(
                cached_profile,
                _artifact_failure_outcome(error),
                tuple(diagnostics),
                manifest,
            )
        except Exception as error:
            diagnostics.append(f"artifact:{type(error).__name__}")
            return ProfileRefreshResult(
                cached_profile,
                ProfileRefreshOutcome.REMOTE_FAILED,
                tuple(diagnostics),
                manifest,
            )

        destination = self.profile_path(normalized_set, normalized_format)
        try:
            with _ProfileLock(destination):
                # A concurrent updater may have committed while this artifact was
                # downloading.  Always compare the fresh destination under lock.
                current = _load_local_profile(destination, normalized_set, normalized_format) or cached_profile
                relation = _artifact_relation(current, artifact)
                if relation == "unchanged":
                    return ProfileRefreshResult(current, ProfileRefreshOutcome.UNCHANGED, tuple(diagnostics), manifest)
                if relation != "newer":
                    diagnostics.append(f"commit:{relation}")
                    return ProfileRefreshResult(
                        current,
                        ProfileRefreshOutcome.STALE_MANIFEST,
                        tuple(diagnostics),
                        manifest,
                    )
                os.replace(staged_path, destination)
                staged_path = None
                _fsync_directory(destination.parent)
                return ProfileRefreshResult(staged_profile, ProfileRefreshOutcome.UPDATED, tuple(diagnostics), manifest)
        except OSError as error:
            diagnostics.append(f"commit:{type(error).__name__}")
            current = _load_local_profile(destination, normalized_set, normalized_format) or cached_profile
            return ProfileRefreshResult(
                current,
                ProfileRefreshOutcome.REMOTE_FAILED,
                tuple(diagnostics),
                manifest,
            )
        finally:
            _unlink(staged_path)

    def _now(self) -> datetime:
        value = self.clock()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = datetime.fromtimestamp(value, tz=UTC)
        return _aware_datetime(value, "client clock")

    def _default_opener(self, request: urllib.request.Request, *, timeout: float) -> Any:
        origin = _url_origin(self.manifest_url or request.full_url)
        return urllib.request.build_opener(_OriginRedirectHandler(origin)).open(request, timeout=timeout)

    def _open(self, url: str, *, accept: str) -> Any:
        origin = _url_origin(self.manifest_url or url)
        _validate_url(url, origin=origin)
        request = urllib.request.Request(
            url,
            headers={"Accept": accept, "User-Agent": PROFILE_CLIENT_USER_AGENT},
        )
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
        except ProfileClientError:
            raise
        except Exception as error:
            raise ProfileClientError(f"network-{type(error).__name__}") from error
        final_url = _response_url(response) or url
        try:
            _validate_url(final_url, origin=origin)
        except ProfileClientError:
            _close_response(response)
            raise
        return response

    def _fetch_manifest(self) -> ProfileManifest:
        response = self._open(self.manifest_url or "", accept="application/json")
        try:
            payload = _read_response(response, limit=self.manifest_max_bytes)
        except ProfileClientError:
            raise
        except Exception as error:
            raise ProfileClientError(f"manifest-read-{type(error).__name__}") from error
        finally:
            _close_response(response)
        try:
            return ProfileManifest.from_bytes(payload)
        except Exception as error:
            if isinstance(error, ProfileClientError):
                raise
            raise ProfileClientError(f"manifest-parse-{type(error).__name__}") from error

    def _stage_artifact(self, artifact: ProfileManifestArtifact, *, destination: Path) -> tuple[Path, SetProfile]:
        if artifact.gzip_bytes > self.max_gzip_bytes:
            raise ProfileClientError("compressed-size-limit")
        if artifact.profile_bytes > self.max_profile_bytes:
            raise ProfileClientError("profile-size-limit")
        origin = _url_origin(self.manifest_url or artifact.url)
        _validate_url(artifact.url, origin=origin)
        destination.parent.mkdir(parents=True, exist_ok=True)
        compressed_path: Path | None = None
        raw_path: Path | None = None
        successful = False
        try:
            response = self._open(artifact.url, accept="application/gzip, application/octet-stream")
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".gz",
                    delete=False,
                ) as compressed:
                    compressed_path = Path(compressed.name)
                    digest = hashlib.sha256()
                    compressed_size = _stream_to_file(
                        response,
                        compressed,
                        digest=digest,
                        limit=min(self.max_gzip_bytes, artifact.gzip_bytes),
                        declared=artifact.gzip_bytes,
                    )
                    compressed.flush()
                    os.fsync(compressed.fileno())
                if compressed_size != artifact.gzip_bytes or digest.hexdigest() != artifact.gzip_sha256:
                    raise ProfileClientError("gzip-checksum-or-size")
            finally:
                _close_response(response)

            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".json",
                delete=False,
            ) as raw:
                raw_path = Path(raw.name)
                profile_digest = hashlib.sha256()
                profile_size = _decompress_to_file(
                    compressed_path,
                    raw,
                    digest=profile_digest,
                    limit=min(self.max_profile_bytes, artifact.profile_bytes),
                    declared=artifact.profile_bytes,
                )
                raw.flush()
                os.fsync(raw.fileno())
            if profile_size != artifact.profile_bytes or profile_digest.hexdigest() != artifact.profile_sha256:
                raise ProfileClientError("profile-checksum-or-size")
            raw_bytes = raw_path.read_bytes()
            try:
                value = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(value, Mapping):
                    raise SetProfileSchemaError("profile JSON must be an object")
                profile = SetProfile.from_json(value)
            except (UnicodeError, json.JSONDecodeError, RecursionError, SetProfileError, TypeError, ValueError) as error:
                raise ProfileClientError("profile-invalid") from error
            if profile.to_bytes() != raw_bytes:
                raise ProfileClientError("profile-not-canonical")
            _validate_profile_metadata(profile, artifact)
            _unlink(compressed_path)
            compressed_path = None
            if raw_path is None:  # pragma: no cover - NamedTemporaryFile always names itself
                raise ProfileClientError("artifact-staging-failed")
            successful = True
            return raw_path, profile
        except ProfileClientError:
            raise
        except (OSError, EOFError, zlib.error) as error:
            raise ProfileClientError(f"artifact-{type(error).__name__}") from error
        finally:
            _unlink(compressed_path)
            if not successful:
                _unlink(raw_path)

    def _read_manifest_cache(self, diagnostics: list[str]) -> _ManifestCache | None:
        path = self.manifest_path()
        try:
            payload = path.read_bytes()
            if len(payload) > self.manifest_max_bytes:
                raise ProfileClientError("cache-too-large")
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, Mapping):
                raise ProfileClientError("cache-not-object")
            manifest_value = value.get("manifest")
            checked_at_value = value.get("checked_at")
            manifest_url_value = value.get("manifest_url")
            if (
                not isinstance(manifest_value, Mapping)
                or not isinstance(checked_at_value, str)
                or not isinstance(manifest_url_value, str)
                or manifest_url_value != self.manifest_url
            ):
                raise ProfileClientError("cache-source")
            return _ManifestCache(
                manifest=ProfileManifest.from_json(manifest_value),
                checked_at=_aware_datetime(checked_at_value, "cached checked_at"),
                manifest_url=manifest_url_value,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ProfileClientError, ProfileManifestError, TypeError, ValueError) as error:
            if path.exists():
                diagnostics.append(f"manifest-cache:{_error_code(error)}")
            return None

    def _write_manifest_cache(self, cache: _ManifestCache, diagnostics: list[str]) -> bool:
        value = {
            "checked_at": cache.checked_at.isoformat(),
            "manifest": cache.manifest.to_json(),
            "manifest_url": cache.manifest_url,
        }
        try:
            _atomic_write_bytes(self.manifest_path(), _canonical_json_bytes(value))
            return True
        except OSError as error:
            diagnostics.append(f"manifest-cache:{type(error).__name__}")
            return False




def _network_policy(value: ProfileNetworkPolicy | str) -> ProfileNetworkPolicy:
    try:
        return value if isinstance(value, ProfileNetworkPolicy) else ProfileNetworkPolicy(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"unsupported profile network policy {value!r}") from error


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _positive_float(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be positive") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result


def _non_negative_float(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be non-negative") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _aware_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProfileClientError(f"{field_name} is not ISO-8601") from error
    else:
        raise ProfileClientError(f"{field_name} is not ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProfileClientError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _url_origin(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url or url != url.strip() or any(
        character.isspace() or ord(character) < 32 for character in url
    ):
        raise ProfileClientError("url-policy")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        # Accessing .port is intentional: urllib.parse defers malformed-port
        # errors until this property is read.
        port = parsed.port
    except ValueError as error:
        raise ProfileClientError("url-policy") from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or "#" in url
    ):
        raise ProfileClientError("url-policy")
    if port not in (None, 443) or parsed.netloc.endswith(":"):
        raise ProfileClientError("url-port")
    return "https", hostname.casefold(), 443


def _validate_url(url: str, *, origin: tuple[str, str, int] | None = None) -> str:
    actual = _url_origin(url)
    if origin is not None and actual != origin:
        raise ProfileClientError("url-origin")
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
        except OSError:
            pass


def _read_response(response: Any, *, limit: int) -> bytes:
    payload = bytearray()
    while True:
        chunk = response.read(min(64 * 1024, limit - len(payload) + 1))
        if chunk in (b"", None):
            if chunk is None:
                raise ProfileClientError("response-read-type")
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ProfileClientError("response-read-type")
        payload.extend(chunk)
        if len(payload) > limit:
            raise ProfileClientError("response-too-large")
    return bytes(payload)


def _stream_to_file(response: Any, output: Any, *, digest: Any, limit: int, declared: int) -> int:
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if chunk in (b"", None):
            if chunk is None:
                raise ProfileClientError("response-read-type")
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ProfileClientError("response-read-type")
        chunk = bytes(chunk)
        total += len(chunk)
        if total > limit or total > declared:
            raise ProfileClientError("compressed-size-limit")
        output.write(chunk)
        digest.update(chunk)
    return total


def _decompress_to_file(path: Path, output: Any, *, digest: Any, limit: int, declared: int) -> int:
    total = 0
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                remaining = limit - total
                if remaining <= 0:
                    raise ProfileClientError("profile-size-limit")
                raw = decompressor.decompress(chunk, remaining + 1)
                if decompressor.unused_data:
                    raise ProfileClientError("gzip-trailing-data")
                total += len(raw)
                if total > limit or total > declared:
                    raise ProfileClientError("profile-size-limit")
                output.write(raw)
                digest.update(raw)
                if decompressor.unconsumed_tail:
                    raise ProfileClientError("profile-size-limit")
        remaining = limit - total
        if remaining < 0:
            raise ProfileClientError("profile-size-limit")
        raw = decompressor.flush(remaining + 1)
        total += len(raw)
        if total > limit or total > declared:
            raise ProfileClientError("profile-size-limit")
        output.write(raw)
        digest.update(raw)
        if not decompressor.eof:
            raise ProfileClientError("gzip-incomplete")
        return total
    except zlib.error:
        raise


def _validate_profile_metadata(profile: SetProfile, artifact: ProfileManifestArtifact) -> None:
    if profile.set_code != artifact.set_code or profile.event_format != artifact.format:
        raise ProfileClientError("profile-identity-mismatch")
    if profile.profile_version != artifact.profile_version:
        raise ProfileClientError("profile-version-mismatch")
    if profile.maturity is not artifact.maturity:
        raise ProfileClientError("profile-maturity-mismatch")
    if profile.generated_at != artifact.generated_at and _aware_datetime(profile.generated_at, "profile generated_at") != _aware_datetime(artifact.generated_at, "artifact generated_at"):
        raise ProfileClientError("profile-generated-at-mismatch")
    if profile.schema_version != artifact.set_profile_schema_version:
        raise ProfileClientError("profile-schema-mismatch")


def _profile_time(profile: SetProfile) -> datetime:
    try:
        return _aware_datetime(profile.generated_at, "profile generated_at")
    except ProfileClientError:
        return datetime.min.replace(tzinfo=UTC)


def _maturity_rank(maturity: ProfileMaturity) -> int:
    return {
        ProfileMaturity.MATURE: 0,
        ProfileMaturity.EARLY: 1,
        ProfileMaturity.SEMANTIC_ONLY: 2,
        ProfileMaturity.METADATA_ONLY: 3,
        ProfileMaturity.GENERIC: 4,
    }[maturity]


def _same_profile_identity(profile: SetProfile, artifact: ProfileManifestArtifact) -> bool:
    if profile.maturity is ProfileMaturity.GENERIC:
        return False
    return (
        profile.set_code == artifact.set_code
        and profile.event_format == artifact.format
        and profile.profile_version == artifact.profile_version
        and _profile_time(profile) == _aware_datetime(artifact.generated_at, "artifact generated_at")
        and profile.maturity is artifact.maturity
        and len(profile.to_bytes()) == artifact.profile_bytes
        and hashlib.sha256(profile.to_bytes()).hexdigest() == artifact.profile_sha256
    )


def _artifact_relation(profile: SetProfile, artifact: ProfileManifestArtifact) -> str:
    if _same_profile_identity(profile, artifact):
        return "unchanged"
    if profile.maturity is not ProfileMaturity.GENERIC:
        artifact_time = _aware_datetime(artifact.generated_at, "artifact generated_at")
        profile_time = _profile_time(profile)
        if _maturity_rank(artifact.maturity) > _maturity_rank(profile.maturity):
            return "stale"
        if _maturity_rank(artifact.maturity) == _maturity_rank(profile.maturity):
            if artifact_time < profile_time:
                return "stale"
            if artifact_time == profile_time:
                return "conflict"
    return "newer"


def _offline_outcome(profile: SetProfile) -> ProfileRefreshOutcome:
    return ProfileRefreshOutcome.CACHED if profile.maturity is not ProfileMaturity.GENERIC else ProfileRefreshOutcome.OFFLINE


def _manifest_failure_outcome(error: ProfileClientError) -> ProfileRefreshOutcome:
    code = _error_code(error)
    return (
        ProfileRefreshOutcome.REMOTE_FAILED
        if code.startswith("network-")
        else ProfileRefreshOutcome.MANIFEST_INVALID
    )


def _artifact_failure_outcome(error: ProfileClientError) -> ProfileRefreshOutcome:
    code = _error_code(error)
    return (
        ProfileRefreshOutcome.REMOTE_FAILED
        if code.startswith("network-")
        else ProfileRefreshOutcome.ARTIFACT_INVALID
    )


def _load_bundled_profile(path: Path) -> tuple[SetProfile | None, str | None]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None, "rejected-bundled:missing"
    except OSError:
        return None, "rejected-bundled:invalid"

    if len(payload) != BUNDLED_PROFILE_BYTES or hashlib.sha256(payload).hexdigest() != BUNDLED_PROFILE_SHA256:
        return None, "rejected-bundled:checksum-or-size"
    try:
        profile = load_set_profile(
            path,
            expected_set_code=BUNDLED_PROFILE_SET_CODE,
            expected_format=BUNDLED_PROFILE_EVENT_FORMAT,
        )
        if profile.to_bytes() != payload or profile.maturity is ProfileMaturity.GENERIC:
            return None, "rejected-bundled:invalid"
    except (OSError, TypeError, ValueError):
        return None, "rejected-bundled:invalid"
    return profile, None


def _load_local_profile(path: Path, set_code: str, event_format: str) -> SetProfile | None:
    try:
        profile = load_set_profile(path, expected_set_code=set_code, expected_format=event_format)
    except (OSError, SetProfileError, TypeError, ValueError):
        return None
    return profile if profile.maturity is not ProfileMaturity.GENERIC else profile


def _historical_profile_paths(app_dir: Path, set_code: str, event_format: str, destination: Path) -> tuple[Path, ...]:
    filename = f"{set_code}-{event_format}.json"
    candidates = (
        app_dir / "profiles" / filename,
        app_dir / "set-profiles" / "v1" / "profiles" / filename,
        app_dir / "set-profiles" / "v1" / filename,
    )
    return tuple(path for path in candidates if path != destination)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{destination.name}.", dir=destination.parent, delete=False) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        _fsync_directory(destination.parent)
    finally:
        _unlink(Path(temporary_name) if temporary_name is not None else None)


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


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _error_code(error: BaseException) -> str:
    return str(error.args[0]) if error.args and str(error.args[0]) else type(error).__name__


__all__ = [
    "BUNDLED_PROFILE_BYTES",
    "BUNDLED_PROFILE_EVENT_FORMAT",
    "BUNDLED_PROFILE_SET_CODE",
    "BUNDLED_PROFILE_SHA256",
    "PROFILE_ARTIFACT_MAX_GZIP_BYTES",
    "PROFILE_ARTIFACT_MAX_PROFILE_BYTES",
    "PROFILE_CLIENT_TIMEOUT_SECONDS",
    "PROFILE_MANIFEST_MAX_BYTES",
    "PROFILE_MANIFEST_TTL_SECONDS",
    "ProfileClient",
    "ProfileClientError",
    "ProfileNetworkPolicy",
    "ProfileRefreshOutcome",
    "ProfileRefreshResult",
]
