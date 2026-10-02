"""Resolve, download, and cache card images for any frontend.
Network and filesystem behavior stay behind a UI-neutral Python service.
"""

from __future__ import annotations

import http.client
import hashlib
import json
import logging
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from threading import Lock, RLock
from typing import Any, TypeAlias

from draftomen.carddb import SCRYFALL_USER_AGENT, CardDatabase, CardInfo
from draftomen.paths import app_data_dir

logger = logging.getLogger(__name__)

PathInput: TypeAlias = str | PathLike[str]
ImageUrlOpener: TypeAlias = Callable[..., Any]
MetadataUrlOpener: TypeAlias = Callable[..., Any]
MonotonicClock: TypeAlias = Callable[[], float]
SleepFunction: TypeAlias = Callable[[float], None]

CARD_IMAGE_CACHE_DIR_NAME = "card-images"
CARD_IMAGE_MAX_BYTES = 8 * 1024 * 1024
CARD_IMAGE_TIMEOUT_SECONDS = 10.0
CARD_IMAGE_MAX_ATTEMPTS = 2
CARD_IMAGE_FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SCRYFALL_NAMED_CARD_URL = "https://api.scryfall.com/cards/named"
SCRYFALL_NAMED_CARD_MAX_BYTES = 1024 * 1024
SCRYFALL_NAMED_CARD_MIN_INTERVAL_SECONDS = 0.5


class CardImageError(RuntimeError):
    """Raised when a card image cannot be resolved or cached.
    Frontends decide how and where to present the failure.
    """


@dataclass(frozen=True, slots=True)
class CardImageService:
    """Provide bounded card-image resolution, download, and caching.
    Failed downloads can be retried without retaining frontend state.
    """

    cache_dir: Path
    max_bytes: int = CARD_IMAGE_MAX_BYTES
    timeout_seconds: float = CARD_IMAGE_TIMEOUT_SECONDS
    max_attempts: int = CARD_IMAGE_MAX_ATTEMPTS
    opener: ImageUrlOpener = field(
        default=urllib.request.urlopen,
        repr=False,
        compare=False,
    )
    metadata_opener: MetadataUrlOpener = field(
        default=urllib.request.urlopen,
        repr=False,
        compare=False,
    )
    metadata_max_bytes: int = SCRYFALL_NAMED_CARD_MAX_BYTES
    monotonic_clock: MonotonicClock = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )
    sleep: SleepFunction = field(default=time.sleep, repr=False, compare=False)
    _metadata_uris_by_name: dict[str, str] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _metadata_rate_state: dict[str, float] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _metadata_rate_lock: RLock = field(
        default_factory=RLock,
        init=False,
        repr=False,
        compare=False,
    )

    _failure_lock: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _warned_hosts: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
        compare=False,
    )
    _suppressed_failures: dict[str, int] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("Card-image byte limit must be positive.")
        if self.timeout_seconds <= 0:
            raise ValueError("Card-image timeout must be positive.")
        if self.max_attempts <= 0:
            raise ValueError("Card-image attempt limit must be positive.")
        if self.metadata_max_bytes <= 0:
            raise ValueError("Card metadata byte limit must be positive.")

    def resolve_image_uri(
        self,
        *,
        card: CardInfo,
        card_database: CardDatabase,
    ) -> str | None:
        """Resolve a card's direct or name-indexed Scryfall image URL.
        Unknown cards remain unresolved rather than triggering network work.
        """

        if card.image_uri is not None:
            return card.image_uri
        if card.unknown:
            return None

        return card_database.image_uri_for_name(name=card.name)

    def resolve_focused_image_uri(
        self,
        *,
        card: CardInfo,
        card_database: CardDatabase,
    ) -> str | None:
        """Resolve a selected known card, using one exact Scryfall lookup if needed.

        Projection callers must use :meth:`resolve_image_uri`, which deliberately
        stays local-only. Successful named lookups are retained in memory.
        """

        image_uri = self.resolve_image_uri(card=card, card_database=card_database)
        if image_uri is not None or card.unknown:
            return image_uri

        name_key = _normalized_card_name(name=card.name)
        cached_uri = self._metadata_uris_by_name.get(name_key)
        if cached_uri is not None:
            return cached_uri

        request = urllib.request.Request(
            f"{SCRYFALL_NAMED_CARD_URL}?exact="
            f"{urllib.parse.quote(card.name, safe='')}",
            headers={
                "Accept": "application/json;q=0.9,*/*;q=0.8",
                "User-Agent": SCRYFALL_USER_AGENT,
            },
        )
        try:
            try:
                self._wait_for_metadata_request_slot()
                with self.metadata_opener(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    payload = response.read(self.metadata_max_bytes + 1)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                raise CardImageError(f"Card metadata lookup failed: {error}") from error
            except (http.client.HTTPException, OSError, urllib.error.URLError) as error:
                raise CardImageError(f"Card metadata lookup failed: {error}") from error

            if not isinstance(payload, bytes):
                raise CardImageError("Card metadata lookup returned malformed response.")
            if len(payload) > self.metadata_max_bytes:
                raise CardImageError("Card metadata lookup failed: response too large.")
            try:
                card_object = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CardImageError(
                    f"Card metadata lookup returned malformed JSON: {error}"
                ) from error
            if not isinstance(card_object, dict):
                raise CardImageError("Card metadata lookup returned malformed JSON object.")
        except CardImageError as error:
            self._record_failure(url=request.full_url, cause=error)
            raise

        image_uri = _scryfall_card_image_uri(card=card_object)
        if image_uri is not None:
            self._metadata_uris_by_name[name_key] = image_uri
        self._record_success(url=request.full_url)
        return image_uri

    def _wait_for_metadata_request_slot(self) -> None:
        """Respect Scryfall's two-requests-per-second named-endpoint limit."""

        with self._metadata_rate_lock:
            now = self.monotonic_clock()
            last_request_at = self._metadata_rate_state.get("last_request_at")
            if last_request_at is not None:
                next_request_at = (
                    last_request_at + SCRYFALL_NAMED_CARD_MIN_INTERVAL_SECONDS
                )
                if now < next_request_at:
                    self.sleep(next_request_at - now)
                    now = max(self.monotonic_clock(), next_request_at)
            self._metadata_rate_state["last_request_at"] = now

    def cached_path(self, *, image_uri: str) -> Path:
        """Return the deterministic cache path for one image URL.
        Existing and future downloads share the same collision-resistant key.
        """

        digest = hashlib.sha256(image_uri.encode("utf-8")).hexdigest()
        extension = _card_image_extension(image_uri=image_uri)
        return self.cache_dir / f"{digest}{extension}"

    def fetch(self, *, image_uri: str) -> Path:
        """Return a cached image or download it with bounded retries.
        Successful writes are atomic and oversized responses are rejected.
        """

        image_path = self.cached_path(image_uri=image_uri)
        if image_path.is_file():
            return image_path

        request = urllib.request.Request(
            image_uri,
            headers={
                "Accept": "image/*,*/*;q=0.8",
                "User-Agent": SCRYFALL_USER_AGENT,
            },
        )
        last_error: OSError | urllib.error.URLError | None = None
        for _ in range(self.max_attempts):
            try:
                with self.opener(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    image_data = response.read(self.max_bytes + 1)
            except (OSError, urllib.error.URLError) as error:
                last_error = error
                continue

            if len(image_data) > self.max_bytes:
                failure = CardImageError("Image fetch failed: response too large.")
                self._record_failure(url=image_uri, cause=failure)
                raise failure

            self._record_success(url=image_uri)
            return self._write_image(image_path=image_path, image_data=image_data)

        detail = "unknown network error" if last_error is None else str(last_error)
        failure = CardImageError(
            f"Image fetch failed after {self.max_attempts} attempts: {detail}"
        )
        self._record_failure(url=image_uri, cause=failure)
        raise failure from last_error

    def log_failure_summary(self) -> None:
        """Log suppressed failure counts for every host and reset them.
        Call this when image work ends so late failures are not lost.
        """

        with self._failure_lock:
            counts = {
                host: count
                for host, count in self._suppressed_failures.items()
                if count > 0
            }
            self._suppressed_failures.clear()
        for host, count in counts.items():
            logger.warning("%d more card image downloads from %s failed", count, host)

    def _record_failure(self, *, url: str, cause: Exception) -> None:
        host = urllib.parse.urlsplit(url).hostname or "unknown host"
        with self._failure_lock:
            first_failure = host not in self._warned_hosts
            if first_failure:
                self._warned_hosts.add(host)
            else:
                self._suppressed_failures[host] = (
                    self._suppressed_failures.get(host, 0) + 1
                )
        if first_failure:
            logger.warning("Card image download from %s failed: %s", host, cause)

    def _record_success(self, *, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname or "unknown host"
        with self._failure_lock:
            count = self._suppressed_failures.pop(host, 0)
        if count > 0:
            logger.warning("%d more card image downloads from %s failed", count, host)

    def _write_image(self, *, image_path: Path, image_data: bytes) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.cache_dir,
                delete=False,
            ) as temporary_file:
                temporary_file.write(image_data)
                temporary_path = Path(temporary_file.name)

            temporary_path.replace(image_path)
        except OSError as error:
            raise CardImageError(f"Image cache write failed: {error}") from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

        return image_path


def card_image_cache_dir(*, app_dir: PathInput | None = None) -> Path:
    """Return the shared card-image cache directory.
    An explicit application directory keeps tests and portable runs isolated.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir)
    return root / CARD_IMAGE_CACHE_DIR_NAME


def _card_image_extension(*, image_uri: str) -> str:
    parsed_uri = urllib.parse.urlparse(image_uri)
    extension = Path(parsed_uri.path).suffix.lower()
    if extension in CARD_IMAGE_FILE_EXTENSIONS:
        return extension

    return ".jpg"


def _normalized_card_name(*, name: str) -> str:
    return " ".join(name.casefold().replace("’", "'").split())


def _scryfall_card_image_uri(*, card: Mapping[str, Any]) -> str | None:
    image_uri = _scryfall_image_uri(value=card.get("image_uris"))
    if image_uri is not None:
        return image_uri

    faces = card.get("card_faces")
    if not isinstance(faces, list):
        return None
    for face in faces:
        if isinstance(face, Mapping):
            image_uri = _scryfall_image_uri(value=face.get("image_uris"))
            if image_uri is not None:
                return image_uri

    return None


def _scryfall_image_uri(*, value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("normal", "large", "small", "png", "border_crop", "art_crop"):
        image_uri = value.get(key)
        if isinstance(image_uri, str) and image_uri:
            return image_uri

    return None

