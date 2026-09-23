"""Discover and cache pinned public Draft Data for augmented-set training."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from draftomen.augmented_training_data import (
    AugmentedTrainingDataError,
    AugmentedTrainingSource,
)
from draftomen.profile_input_acquisition import (
    PUBLIC_DRAFT_ATTRIBUTION,
    PUBLIC_DRAFT_LICENSE,
    ProfileInputAcquisitionError,
    SeventeenLandsPublicDraftAdapter,
    acquire_public_draft_source,
)
from draftomen.profile_input_cache import ProfileInputCache
from draftomen.public_dump import PublicDumpSource
from draftomen.refresh_plan import PlannedEnvironment
from draftomen.seventeen import (
    SEVENTEEN_LANDS_USER_AGENT,
    public_draft_data_url,
)

_PRISMIC_API_ROOT = "https://17lands.cdn.prismic.io/api/v2"
_MAX_CATALOG_RESPONSE_BYTES = 4 * 1024 * 1024
_SUPPORTED_FORMATS = ("PremierDraft", "TradDraft", "QuickDraft")
_SUPPORTED_FORMAT_SET = frozenset(_SUPPORTED_FORMATS)
_SET_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{2,8}\Z")


class AugmentedPublicDataError(ValueError):
    """Raised when public Draft Data cannot be selected or acquired safely."""


@dataclass(frozen=True, slots=True)
class PublicDraftDataset:
    """One catalog-listed and canonical public Draft Data download."""

    event_format: str
    url: str


def fetch_public_draft_listing(*, timeout_seconds: int) -> Mapping[str, Any]:
    """Fetch the single public-data document from the public Prismic catalog."""

    timeout = _validated_timeout(timeout_seconds)
    repository = _fetch_json(url=_PRISMIC_API_ROOT, timeout_seconds=timeout)
    refs = repository.get("refs")
    if not isinstance(refs, list):
        raise AugmentedPublicDataError("The public Draft Data catalog has no refs list.")
    master_refs = [
        ref
        for ref in refs
        if isinstance(ref, Mapping)
        and (ref.get("id") == "master" or ref.get("isMasterRef") is True)
    ]
    if len(master_refs) != 1:
        raise AugmentedPublicDataError(
            "The public Draft Data catalog has no unique master ref."
        )
    master_ref = master_refs[0].get("ref")
    if not isinstance(master_ref, str) or not master_ref.strip():
        raise AugmentedPublicDataError(
            "The public Draft Data catalog master ref is malformed."
        )

    query = urllib.parse.urlencode(
        {
            "q": '[[at(document.type, "public-data")]]',
            "pageSize": "1",
            "ref": master_ref,
        }
    )
    response = _fetch_json(
        url=f"{_PRISMIC_API_ROOT}/documents/search?{query}",
        timeout_seconds=timeout,
    )
    results = response.get("results")
    total_results_size = response.get("total_results_size")
    if (
        not isinstance(results, list)
        or len(results) != 1
        or isinstance(total_results_size, bool)
        or not isinstance(total_results_size, int)
        or total_results_size != 1
    ):
        raise AugmentedPublicDataError(
            "The public Draft Data catalog must contain exactly one public-data document."
        )
    document = results[0]
    if (
        not isinstance(document, Mapping)
        or document.get("type") != "public-data"
    ):
        raise AugmentedPublicDataError(
            "The public Draft Data catalog document is malformed."
        )
    data = document.get("data")
    if not isinstance(data, Mapping):
        raise AugmentedPublicDataError(
            "The public Draft Data catalog data is malformed."
        )
    return data


def discover_public_draft_datasets(
    *, set_code: str, listing: Mapping[str, Any]
) -> tuple[PublicDraftDataset, ...]:
    """Select supported canonical Draft Data links for one set, in preference order."""

    normalized_set = _normalized_set_code(set_code)
    if not isinstance(listing, Mapping):
        raise AugmentedPublicDataError(
            "The public Draft Data catalog must be an object."
        )
    rows = listing.get("datasets")
    if not isinstance(rows, list):
        raise AugmentedPublicDataError(
            "The public Draft Data catalog has no datasets list."
        )

    selected: dict[str, PublicDraftDataset] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        expansion = _first_field_text(row.get("expansion"))
        event_format = _first_field_text(row.get("format"))
        if (
            expansion != normalized_set
            or event_format is None
            or event_format not in _SUPPORTED_FORMAT_SET
        ):
            continue
        url = _draft_data_url(row=row)
        if url is None or url != public_draft_data_url(
            set_code=normalized_set,
            event_format=event_format,
        ):
            continue
        selected.setdefault(
            event_format,
            PublicDraftDataset(event_format=event_format, url=url),
        )

    return tuple(
        selected[event_format]
        for event_format in _SUPPORTED_FORMATS
        if event_format in selected
    )


def acquire_augmented_training_source(
    *,
    set_code: str,
    cache: ProfileInputCache,
    timeout_seconds: int,
    listing: Mapping[str, Any] | None = None,
    adapter: SeventeenLandsPublicDraftAdapter | None = None,
) -> AugmentedTrainingSource:
    """Acquire the first usable listed source through the shared validated cache."""

    normalized_set = _normalized_set_code(set_code)
    timeout = _validated_timeout(timeout_seconds)
    catalog = (
        fetch_public_draft_listing(timeout_seconds=timeout)
        if listing is None
        else listing
    )
    candidates = discover_public_draft_datasets(
        set_code=normalized_set,
        listing=catalog,
    )
    if not candidates:
        raise AugmentedPublicDataError(
            f"No supported public Draft Data dump is listed for set {normalized_set}."
        )

    public_adapter = (
        SeventeenLandsPublicDraftAdapter(timeout_seconds=timeout)
        if adapter is None
        else adapter
    )
    for candidate in candidates:
        environment = PlannedEnvironment(
            set_code=normalized_set,
            event_format=candidate.event_format,
            lifecycle=None,
            reasons=("augmented-training",),
        )
        try:
            source = acquire_public_draft_source(
                environment=environment,
                cache=cache,
                adapter=public_adapter,
            )
        except ProfileInputAcquisitionError:
            continue
        try:
            return _training_source(source=source, dataset=candidate)
        except (AugmentedTrainingDataError, TypeError, ValueError):
            continue

    raise AugmentedPublicDataError(
        f"No listed public Draft Data dump is available and valid for set {normalized_set}."
    )


def _fetch_json(*, url: str, timeout_seconds: int) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": SEVENTEEN_LANDS_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(_MAX_CATALOG_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError) as error:
        raise AugmentedPublicDataError(
            "Could not fetch the public Draft Data catalog."
        ) from error
    if len(payload) > _MAX_CATALOG_RESPONSE_BYTES:
        raise AugmentedPublicDataError(
            "The public Draft Data catalog response is too large."
        )
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AugmentedPublicDataError(
            "The public Draft Data catalog returned malformed JSON."
        ) from error
    if not isinstance(document, Mapping):
        raise AugmentedPublicDataError(
            "The public Draft Data catalog response is malformed."
        )
    return document


def _validated_timeout(timeout_seconds: int) -> int:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise AugmentedPublicDataError("timeout_seconds must be a positive integer.")
    return timeout_seconds


def _normalized_set_code(value: str) -> str:
    if not isinstance(value, str):
        raise AugmentedPublicDataError(
            "Set code must contain 2 to 8 ASCII letters or digits."
        )
    normalized = value.strip()
    if _SET_CODE_PATTERN.fullmatch(normalized) is None:
        raise AugmentedPublicDataError(
            "Set code must contain 2 to 8 ASCII letters or digits."
        )
    return normalized.upper()


def _first_field_text(value: Any) -> str | None:
    if (
        not isinstance(value, list)
        or not value
        or not isinstance(value[0], Mapping)
    ):
        return None
    text = value[0].get("text")
    return text if isinstance(text, str) else None


def _draft_data_url(*, row: Mapping[str, Any]) -> str | None:
    draft_data = row.get("draft_data")
    if (
        not isinstance(draft_data, list)
        or not draft_data
        or not isinstance(draft_data[0], Mapping)
    ):
        return None
    spans = draft_data[0].get("spans")
    if not isinstance(spans, list):
        return None
    hyperlinks = [
        span
        for span in spans
        if isinstance(span, Mapping) and span.get("type") == "hyperlink"
    ]
    if len(hyperlinks) != 1:
        return None
    span = hyperlinks[0]
    data = span.get("data")
    if not isinstance(data, Mapping) or data.get("link_type") != "Web":
        return None
    url = data.get("url")
    return url if isinstance(url, str) else None


def _training_source(
    *, source: PublicDumpSource, dataset: PublicDraftDataset
) -> AugmentedTrainingSource:
    if (
        not isinstance(source, PublicDumpSource)
        or source.path is None
        or source.sha256 is None
        or source.retrieved_at is None
    ):
        raise AugmentedPublicDataError(
            "The validated public-draft source is incomplete."
        )
    return AugmentedTrainingSource(
        path=source.path,
        url=dataset.url,
        sha256=source.sha256,
        retrieved_at=source.retrieved_at,
        attribution=PUBLIC_DRAFT_ATTRIBUTION,
        license=PUBLIC_DRAFT_LICENSE,
        event_type=dataset.event_format,
    )


__all__ = [
    "AugmentedPublicDataError",
    "PublicDraftDataset",
    "acquire_augmented_training_source",
    "discover_public_draft_datasets",
    "fetch_public_draft_listing",
]
