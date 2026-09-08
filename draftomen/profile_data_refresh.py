"""Prepare and publish local 17Lands-backed profile refreshes.

The refresh boundary reads validated static card artifacts, fetches only
aggregate 17Lands format data, and atomically publishes profile objects.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, TypeAlias

from draftomen.profile_generation import (
    DEFAULT_PROFILE_GENERATION_CONFIG,
    ProfileGenerationConfig,
    ProfileGenerationError,
    ProfileGenerationStage,
    generate_set_profile,
)
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestArtifact,
    ProfileManifestError,
    load_profile_manifest,
)
from draftomen.profile_publication import (
    ProfilePublicationError,
    validate_profile_generation,
)
from draftomen.seventeen import (
    HTTP_TIMEOUT_SECONDS,
    SeventeenLandsError,
    _default_fetch_json,
    load_or_refresh_17lands_format_data,
)
from draftomen.set_card_data import SetCardData, SetCardDataError
from draftomen.set_profile import SetProfileError


PathInput: TypeAlias = str | os.PathLike[str]
FetchJson: TypeAlias = Callable[[str, int], Any]
Clock: TypeAlias = Callable[[], datetime]

FILTERS_ENDPOINT = "https://www.17lands.com/data/filters"
PROFILE_BASE_URL = "https://www.draftomen.com/profiles/objects/"
RATINGS_CACHE_TTL = timedelta(days=1)
SUPPORTED_FORMATS = (
    "PremierDraft",
    "TradDraft",
    "QuickDraft",
    "PickTwoDraft",
)
_SET_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FILTER_MAPPING_FIELDS = ("formats_by_expansion", "live_formats_by_expansion")


class ProfileDataRefreshError(ValueError):
    """Raised when refresh selection, inputs, or publication is invalid."""


@dataclass(frozen=True, slots=True)
class Pair:
    """Identify one set, event format, and immutable card-data artifact."""

    set_code: str
    set_name: str
    event_format: str
    static_path: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.set_code, str)
            or _SET_CODE_RE.fullmatch(self.set_code.casefold()) is None
        ):
            raise ProfileDataRefreshError("profile refresh set code is invalid")
        if not isinstance(self.set_name, str) or not self.set_name.strip():
            raise ProfileDataRefreshError("profile refresh set name is invalid")
        if self.event_format not in SUPPORTED_FORMATS:
            raise ProfileDataRefreshError("profile refresh format is unsupported")
        try:
            path = Path(self.static_path)
        except (TypeError, ValueError) as error:
            raise ProfileDataRefreshError("profile refresh card artifact path is invalid") from error
        object.__setattr__(self, "set_code", self.set_code.casefold())
        object.__setattr__(self, "static_path", path)

    @property
    def identity(self) -> tuple[str, str]:
        """Return the normalized set and format identity."""

        return self.set_code, self.event_format.casefold()

    def to_json(self) -> dict[str, str]:
        """Return the path-free pair identity."""

        return {
            "event_format": self.event_format,
            "set_code": self.set_code,
            "set_name": self.set_name,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """Deterministic profile refresh pairs prepared before ratings fetches."""

    pairs: tuple[Pair, ...]
    mode: str = "all"
    selector: str | None = None

    def __post_init__(self) -> None:
        pairs = tuple(self.pairs)
        if any(not isinstance(pair, Pair) for pair in pairs):
            raise ProfileDataRefreshError("profile refresh plan pairs are invalid")
        identities = [pair.identity for pair in pairs]
        if len(identities) != len(set(identities)):
            raise ProfileDataRefreshError("profile refresh plan contains duplicate pairs")
        if self.mode not in {"all", "active", "historical"}:
            raise ProfileDataRefreshError("profile refresh selection mode is invalid")
        selector = None if self.selector is None else self.selector.strip()
        if selector == "":
            raise ProfileDataRefreshError("profile refresh selector is invalid")
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "selector", selector)

    @property
    def count(self) -> int:
        """Return the number of selected set and format pairs."""

        return len(self.pairs)


@dataclass(frozen=True, slots=True)
class Failure:
    """Describe one bounded, path-free pair failure."""

    pair: Pair
    category: str

    def __post_init__(self) -> None:
        if not isinstance(self.pair, Pair):
            raise ProfileDataRefreshError("profile refresh failure pair is invalid")
        if not isinstance(self.category, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", self.category
        ):
            raise ProfileDataRefreshError("profile refresh failure category is invalid")

    @property
    def set_code(self) -> str:
        """Return the failed pair's set code."""

        return self.pair.set_code

    @property
    def event_format(self) -> str:
        """Return the failed pair's event format."""

        return self.pair.event_format

    @property
    def reason(self) -> str:
        """Return the stable failure category."""

        return self.category

    def to_json(self) -> dict[str, str]:
        """Return a path-free failure record."""

        return {
            "category": self.category,
            "event_format": self.pair.event_format,
            "set_code": self.pair.set_code,
        }


@dataclass(frozen=True, slots=True)
class Result:
    """Summarize successful profile refreshes and every pair failure."""

    plan: Plan
    successful_pairs: tuple[Pair, ...] = ()
    failures: tuple[Failure, ...] = ()
    manifest_changed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.plan, Plan):
            raise ProfileDataRefreshError("profile refresh result plan is invalid")
        successes = tuple(self.successful_pairs)
        failures = tuple(self.failures)
        if any(not isinstance(pair, Pair) for pair in successes):
            raise ProfileDataRefreshError("profile refresh result successes are invalid")
        if any(not isinstance(failure, Failure) for failure in failures):
            raise ProfileDataRefreshError("profile refresh result failures are invalid")
        object.__setattr__(self, "successful_pairs", successes)
        object.__setattr__(self, "failures", failures)

    @property
    def succeeded(self) -> bool:
        """Return whether every planned pair completed successfully."""

        return not self.failures

    def to_json(self) -> dict[str, Any]:
        """Return a path-free result summary."""

        return {
            "failed": [failure.to_json() for failure in self.failures],
            "manifest_changed": self.manifest_changed,
            "planned": self.plan.count,
            "published": [pair.to_json() for pair in self.successful_pairs],
        }


def prepare_profile_data_refresh(
    selector: str | None = None,
    *,
    card_data_dir: PathInput = Path("website/public/card-data"),
    mode: str = "all",
    active: bool = False,
    historical: bool = False,
    fetch_json: FetchJson | None = None,
    filters_url: str = FILTERS_ENDPOINT,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> Plan:
    """Prepare deterministic pairs from local cards and 17Lands filters."""

    normalized_mode = _selection_mode(mode=mode, active=active, historical=historical)
    if not isinstance(filters_url, str) or not filters_url.strip():
        raise ProfileDataRefreshError("profile refresh filters URL is invalid")
    fetcher = fetch_json or _default_fetch_json
    if not callable(fetcher):
        raise ProfileDataRefreshError("filters JSON fetcher is invalid")
    try:
        output = Path(card_data_dir)
    except (TypeError, ValueError) as error:
        raise ProfileDataRefreshError("card-data directory is invalid") from error
    identities = _load_static_identities(output=output)
    try:
        payload = fetcher(filters_url, timeout_seconds)
    except Exception as error:
        raise ProfileDataRefreshError("could not fetch 17Lands filters") from error
    available, live = _parse_filters(payload=payload)

    selector_identity = _select_static_identity(identities=identities, selector=selector)
    selected: list[Pair] = []
    for identity in identities:
        if selector_identity is not None and identity[:2] != selector_identity[:2]:
            continue
        available_formats = frozenset(
            event_format
            for event_format in SUPPORTED_FORMATS
            if event_format in available.get(identity[0], frozenset())
        )
        live_formats = frozenset(
            event_format
            for event_format in SUPPORTED_FORMATS
            if event_format in live.get(identity[0], frozenset())
        )
        expansion_is_live = bool(available_formats.intersection(live_formats))
        if normalized_mode == "active":
            formats = available_formats if expansion_is_live else frozenset()
        elif normalized_mode == "historical":
            formats = available_formats if not expansion_is_live else frozenset()
        else:
            formats = available_formats
        for event_format in SUPPORTED_FORMATS:
            if event_format in formats:
                selected.append(
                    Pair(
                        set_code=identity[0],
                        set_name=identity[1],
                        event_format=event_format,
                        static_path=identity[2],
                    )
                )
    selected.sort(key=lambda pair: (pair.set_code, SUPPORTED_FORMATS.index(pair.event_format)))
    return Plan(
        pairs=tuple(selected),
        mode=normalized_mode,
        selector=selector,
    )


def execute_profile_data_refresh(
    plan: Plan,
    *,
    profiles_dir: PathInput = Path("website/public/profiles"),
    cache_dir: PathInput | None = None,
    fetch_json: FetchJson | None = None,
    clock: Clock | None = None,
    config: ProfileGenerationConfig = DEFAULT_PROFILE_GENERATION_CONFIG,
    profile_version: str = "1.0",
    profile_base_url: str = PROFILE_BASE_URL,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> Result:
    """Generate and atomically publish every selected profile pair."""

    if not isinstance(plan, Plan):
        raise ProfileDataRefreshError("profile refresh plan is invalid")
    try:
        profiles = Path(profiles_dir)
    except (TypeError, ValueError) as error:
        raise ProfileDataRefreshError("profiles directory is invalid") from error
    manifest_path = profiles / "manifest.json"
    try:
        existing_manifest = load_profile_manifest(manifest_path)
        existing_manifest_bytes = manifest_path.read_bytes()
    except (OSError, ProfileManifestError) as error:
        raise ProfileDataRefreshError("profile manifest could not be loaded") from error

    command_now = _now(clock=clock)
    failures: list[Failure] = []
    prepared: list[tuple[Pair, bytes, ProfileManifestArtifact, Path]] = []
    for pair in plan.pairs:
        try:
            card_data = SetCardData.from_gzip_bytes(
                pair.static_path.read_bytes(),
                expected_set_code=pair.set_code,
                expected_set_name=pair.set_name,
            )
            card_database = card_data.to_card_database()
        except (OSError, SetCardDataError):
            failures.append(Failure(pair=pair, category="static-artifact-invalid"))
            continue
        try:
            ratings = load_or_refresh_17lands_format_data(
                set_code=pair.set_code,
                event_format=pair.event_format,
                app_dir=cache_dir,
                refresh=False,
                clock=clock,
                fetch_json=fetch_json,
                timeout_seconds=timeout_seconds,
                cache_ttl=RATINGS_CACHE_TTL,
            )
        except (OSError, RuntimeError, SeventeenLandsError, UnicodeError):
            failures.append(Failure(pair=pair, category="ratings-unavailable"))
            continue
        try:
            fetched_at = ratings.fetched_at
            stale = (
                not isinstance(fetched_at, datetime)
                or fetched_at.tzinfo is None
                or command_now.astimezone(UTC) - fetched_at.astimezone(UTC)
                >= RATINGS_CACHE_TTL
            )
        except (AttributeError, OverflowError, TypeError, ValueError):
            failures.append(Failure(pair=pair, category="ratings-unavailable"))
            continue
        if stale:
            failures.append(Failure(pair=pair, category="ratings-unavailable"))
            continue
        try:
            generation = generate_set_profile(
                set_code=pair.set_code,
                event_format=pair.event_format,
                stage=ProfileGenerationStage.EARLY,
                card_database=card_database,
                generated_at=ratings.fetched_at,
                profile_version=profile_version,
                ratings=ratings,
                config=config,
            )
        except (ProfileGenerationError, SetProfileError, RuntimeError):
            failures.append(Failure(pair=pair, category="profile-generation-failed"))
            continue
        try:
            validated = validate_profile_generation(
                generation=generation,
                set_code=pair.set_code,
                event_format=pair.event_format.casefold(),
                stage=ProfileGenerationStage.EARLY.value,
            )
            report = generation.report
            artifact = ProfileManifestArtifact(
                set_code=report.set_code,
                event_format=report.event_format,
                set_profile_schema_version=report.set_profile_schema_version,
                profile_version=generation.profile.profile_version,
                generated_at=report.generated_at,
                url=_profile_url(base_url=profile_base_url, digest=report.gzip_sha256),
                gzip_bytes=report.gzip_bytes,
                profile_bytes=report.profile_bytes,
                gzip_sha256=report.gzip_sha256,
                profile_sha256=report.profile_sha256,
                maturity=generation.profile.maturity,
            )
        except (ProfileDataRefreshError, ProfileManifestError, ProfilePublicationError):
            failures.append(Failure(pair=pair, category="profile-validation-failed"))
            continue
        object_path = profiles / "objects" / f"{report.gzip_sha256}.json.gz"
        prepared.append((pair, validated.gzip_bytes, artifact, object_path))

    published_pairs: list[Pair] = []
    replacements: dict[tuple[str, str], ProfileManifestArtifact] = {}
    for pair, gzip_bytes, artifact, object_path in prepared:
        try:
            _reuse_or_publish_object(path=object_path, payload=gzip_bytes)
        except (OSError, ProfileDataRefreshError):
            failures.append(Failure(pair=pair, category="object-publish-failed"))
            continue
        published_pairs.append(pair)
        replacements[pair.identity] = artifact

    manifest_changed = False
    if replacements:
        existing_artifacts = {
            (artifact.set_code, artifact.event_format): artifact
            for artifact in existing_manifest.artifacts
        }
        replacement_changed = any(
            existing_artifacts.get(identity) != artifact
            for identity, artifact in replacements.items()
        )
        if replacement_changed:
            existing_artifacts.update(replacements)
            published_at = _now(clock=clock)
            merged_manifest = ProfileManifest(
                artifacts=tuple(existing_artifacts.values()),
                published_at=published_at.isoformat(),
            )
            merged_bytes = merged_manifest.to_bytes()
            if merged_bytes != existing_manifest_bytes:
                try:
                    _atomic_write(path=manifest_path, payload=merged_bytes)
                except OSError:
                    for pair in published_pairs:
                        failures.append(Failure(pair=pair, category="manifest-publish-failed"))
                    published_pairs.clear()
                    replacements.clear()
                else:
                    manifest_changed = True

    failures.sort(key=lambda failure: (failure.pair.set_code, SUPPORTED_FORMATS.index(failure.pair.event_format)))
    published_pairs.sort(key=lambda pair: (pair.set_code, SUPPORTED_FORMATS.index(pair.event_format)))
    return Result(
        plan=plan,
        successful_pairs=tuple(published_pairs),
        failures=tuple(failures),
        manifest_changed=manifest_changed,
    )


def _selection_mode(*, mode: str, active: bool, historical: bool) -> str:
    if active and historical:
        raise ProfileDataRefreshError("active and historical selections are exclusive")
    if not isinstance(mode, str):
        raise ProfileDataRefreshError("profile refresh selection mode is invalid")
    stripped = mode.strip()
    normalized = stripped.casefold()
    aliases = {
        "all": "all",
        "active": "active",
        "history": "historical",
        "historical": "historical",
    }
    if normalized not in aliases or (not (active or historical) and stripped != normalized):
        raise ProfileDataRefreshError("profile refresh selection mode is invalid")
    return "active" if active else "historical" if historical else aliases[normalized]


def _load_static_identities(*, output: Path) -> tuple[tuple[str, str, Path], ...]:
    if not output.is_dir():
        raise ProfileDataRefreshError("card-data directory could not be loaded")
    identities: list[tuple[str, str, Path]] = []
    seen_codes: set[str] = set()
    for path in sorted(output.glob("*.json.gz"), key=lambda item: item.name):
        filename = path.name
        code = filename[: -len(".json.gz")]
        if _SET_CODE_RE.fullmatch(code) is None:
            continue
        if code in seen_codes:
            raise ProfileDataRefreshError("card-data directory contains duplicate set artifacts")
        try:
            artifact = SetCardData.from_gzip_bytes(
                path.read_bytes(),
                expected_set_code=code,
            )
        except (OSError, SetCardDataError):
            continue
        if artifact.set_code in seen_codes:
            raise ProfileDataRefreshError("card-data directory contains duplicate set artifacts")
        seen_codes.add(artifact.set_code)
        identities.append((artifact.set_code, artifact.set_name, path))
    return tuple(sorted(identities, key=lambda item: item[0]))


def _select_static_identity(
    *,
    identities: tuple[tuple[str, str, Path], ...],
    selector: str | None,
) -> tuple[str, str, Path] | None:
    if selector is None:
        return None
    if not isinstance(selector, str) or not selector.strip():
        raise ProfileDataRefreshError("profile refresh selector is invalid")
    normalized = selector.strip().casefold()
    matches = [
        item
        for item in identities
        if item[0] == normalized or item[1].strip().casefold() == normalized
    ]
    if not matches:
        raise ProfileDataRefreshError("profile refresh selector does not match a set")
    if len(matches) != 1:
        raise ProfileDataRefreshError("profile refresh selector is ambiguous")
    return matches[0]


def _parse_filters(*, payload: Any) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    if not isinstance(payload, Mapping):
        raise ProfileDataRefreshError("17Lands filters response is malformed")
    try:
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        raise ProfileDataRefreshError("17Lands filters response is malformed") from error
    parsed: list[dict[str, frozenset[str]]] = []
    for field_name in _FILTER_MAPPING_FIELDS:
        value = payload.get(field_name)
        if not isinstance(value, Mapping):
            raise ProfileDataRefreshError("17Lands filters response is malformed")
        mapping: dict[str, frozenset[str]] = {}
        for raw_code, raw_formats in value.items():
            if not isinstance(raw_code, str):
                raise ProfileDataRefreshError("17Lands filters response is malformed")
            code = raw_code.strip().casefold()
            if code in mapping:
                raise ProfileDataRefreshError("17Lands filters response is malformed")
            if raw_formats is None and field_name == "live_formats_by_expansion":
                formats = frozenset()
            elif isinstance(raw_formats, list) and all(
                isinstance(item, str) and item.strip() for item in raw_formats
            ):
                normalized = [item.strip() for item in raw_formats]
                formats = frozenset(item for item in normalized if item in SUPPORTED_FORMATS)
            else:
                raise ProfileDataRefreshError("17Lands filters response is malformed")
            mapping[code] = formats
        parsed.append(mapping)
    return parsed[0], parsed[1]


def _profile_url(*, base_url: str, digest: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ProfileDataRefreshError("profile base URL is invalid")
    return f"{base_url.rstrip('/')}/{digest}.json.gz"


def _reuse_or_publish_object(*, path: Path, payload: bytes) -> None:
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        _atomic_write(path=path, payload=payload)
        return
    if existing != payload:
        raise ProfileDataRefreshError("content-addressed profile object conflicts")


def _atomic_write(*, path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _now(*, clock: Clock | None) -> datetime:
    value = datetime.now(tz=UTC) if clock is None else clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProfileDataRefreshError("refresh clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


__all__ = [
    "FILTERS_ENDPOINT",
    "PROFILE_BASE_URL",
    "RATINGS_CACHE_TTL",
    "SUPPORTED_FORMATS",
    "Failure",
    "Pair",
    "Plan",
    "ProfileDataRefreshError",
    "Result",
    "execute_profile_data_refresh",
    "prepare_profile_data_refresh",
]
