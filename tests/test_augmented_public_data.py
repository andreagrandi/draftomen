from __future__ import annotations

import gzip
import hashlib
import json
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import draftomen.augmented_public_data as public_data
from draftomen.augmented_public_data import (
    AugmentedPublicDataError,
    PublicDraftDataset,
    acquire_augmented_training_source,
    discover_public_draft_datasets,
    fetch_public_draft_listing,
)
from draftomen.profile_input_acquisition import (
    PUBLIC_DRAFT_ATTRIBUTION,
    PUBLIC_DRAFT_LICENSE,
    ProfileInputAcquisitionError,
    SeventeenLandsPublicDraftAdapter,
)
from draftomen.profile_input_cache import (
    ProfileInputCache,
    ProfileInputCachePolicy,
)
from draftomen.public_dump import PublicDumpSource
from draftomen.refresh_plan import PlannedEnvironment
from draftomen.seventeen import public_draft_data_url

_NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("event_format", "expected_format"),
    [
        ("PremierDraft", "PremierDraft"),
        ("TradDraft", "TradDraft"),
        ("QuickDraft", "QuickDraft"),
    ],
)
def test_public_draft_url_canonicalizes_planned_event_formats(
    event_format: str, expected_format: str
) -> None:
    environment = PlannedEnvironment(
        set_code="HOB",
        event_format=event_format,
        lifecycle=None,
        reasons=("augmented-training",),
    )
    expected_url = (
        "https://17lands-public.s3.amazonaws.com/analysis_data/draft_data/"
        f"draft_data_public.HOB.{expected_format}.csv.gz"
    )

    assert environment.event_format == event_format.casefold()
    assert public_draft_data_url(
        set_code=environment.set_code,
        event_format=environment.event_format,
    ) == expected_url
    assert public_draft_data_url(
        set_code=environment.set_code,
        event_format=event_format,
    ) == expected_url


def _web_link(url: str) -> dict[str, Any]:
    return {
        "type": "hyperlink",
        "data": {"link_type": "Web", "url": url},
    }


def _row(
    *,
    set_code: str = "HOB",
    event_format: str = "PremierDraft",
    url: str | None = None,
    spans: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    draft_url = (
        public_draft_data_url(set_code=set_code, event_format=event_format)
        if url is None
        else url
    )
    return {
        "expansion": [{"text": set_code}],
        "format": [{"text": event_format}],
        "draft_data": [
            {
                "text": "Draft Data",
                "spans": [_web_link(draft_url)] if spans is None else spans,
            }
        ],
        "game_data": [
            {"spans": [_web_link("https://example.invalid/game.csv.gz")]}
        ],
        "replay_data": [
            {"spans": [_web_link("https://example.invalid/replay.csv.gz")]}
        ],
    }


def _listing(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"datasets": list(rows)}


def _cache(tmp_path: Path) -> ProfileInputCache:
    return ProfileInputCache(
        tmp_path / "profile-input-cache",
        policy=ProfileInputCachePolicy(
            freshness_ttl=timedelta(hours=1),
            max_entry_bytes=100_000,
            max_total_bytes=300_000,
            max_records=10,
            max_versions_per_source=3,
        ),
        clock=lambda: _NOW,
    )


def _draft_csv(*, set_code: str = "HOB", event_format: str = "PremierDraft") -> bytes:
    text = (
        "draft_id,expansion,event_type,event_match_wins,pick,pick_maindeck_rate\n"
        f"draft-one,{set_code},{event_format},7,Requested Card,1.0\n"
    )
    return gzip.compress(text.encode("utf-8"), mtime=0)


class _DraftFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, int]] = []
        self.error: Exception | None = None

    def __call__(
        self,
        *,
        set_code: str,
        event_format: str,
        path: Path,
        timeout_seconds: int,
    ) -> None:
        self.calls.append((set_code, event_format, timeout_seconds))
        if self.error is not None:
            raise self.error
        path.write_bytes(self.payload)


def _adapter(fetcher: _DraftFetcher) -> SeventeenLandsPublicDraftAdapter:
    return SeventeenLandsPublicDraftAdapter(
        fetch_public_drafts=fetcher,
        timeout_seconds=23,
    )


def _assert_no_website_public_write(tmp_path: Path) -> None:
    assert not (tmp_path / "website" / "public").exists()


def test_discovery_prefers_premier_and_reads_only_requested_draft_data_links() -> None:
    rows = _listing(
        _row(event_format="QuickDraft"),
        _row(set_code="OTH", event_format="PremierDraft"),
        _row(event_format="TradDraft"),
        _row(event_format="PremierDraft"),
    )

    datasets = discover_public_draft_datasets(set_code="hob", listing=rows)

    assert datasets == (
        PublicDraftDataset(
            event_format="PremierDraft",
            url=public_draft_data_url(set_code="HOB", event_format="PremierDraft"),
        ),
        PublicDraftDataset(
            event_format="TradDraft",
            url=public_draft_data_url(set_code="HOB", event_format="TradDraft"),
        ),
        PublicDraftDataset(
            event_format="QuickDraft",
            url=public_draft_data_url(set_code="HOB", event_format="QuickDraft"),
        ),
    )


def test_discovery_rejects_picktwo_noncanonical_and_malformed_draft_links() -> None:
    rows = _listing(
        _row(set_code="OTH", event_format="PremierDraft"),
        _row(event_format="PickTwoDraft"),
        _row(
            event_format="PremierDraft",
            url="https://other.example/draft.csv.gz",
        ),
        _row(event_format="PremierDraft", spans=[]),
        _row(
            event_format="PremierDraft",
            spans=[
                _web_link(
                    public_draft_data_url(set_code="HOB", event_format="PremierDraft")
                ),
                _web_link("https://example.invalid/second.csv.gz"),
            ],
        ),
        _row(
            event_format="PremierDraft",
            spans=[
                {
                    "type": "hyperlink",
                    "data": {
                        "link_type": "Document",
                        "url": public_draft_data_url(
                            set_code="HOB", event_format="PremierDraft"
                        ),
                    },
                }
            ],
        ),
    )

    assert discover_public_draft_datasets(set_code="HOB", listing=rows) == ()


def test_absent_premier_uses_first_listed_supported_format(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[str, str, str | None, tuple[str, ...]]] = []
    path = tmp_path / "cached-dump.csv.gz"
    path.write_bytes(_draft_csv(event_format="TradDraft"))
    source = PublicDumpSource(
        name="17lands-public-drafts",
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        retrieved_at=_NOW.isoformat(),
        attribution=PUBLIC_DRAFT_ATTRIBUTION,
        license=PUBLIC_DRAFT_LICENSE,
    )

    def acquire(*, environment: Any, cache: Any, adapter: Any) -> PublicDumpSource:
        del cache
        calls.append(
            (
                environment.event_format,
                environment.set_code,
                environment.lifecycle,
                environment.reasons,
            )
        )
        assert isinstance(adapter, SeventeenLandsPublicDraftAdapter)
        return source

    monkeypatch.setattr(public_data, "acquire_public_draft_source", acquire)
    result = acquire_augmented_training_source(
        set_code="HOB",
        cache=_cache(tmp_path),
        timeout_seconds=17,
        listing=_listing(
            _row(event_format="TradDraft"),
            _row(event_format="QuickDraft"),
        ),
    )

    assert calls == [("traddraft", "HOB", None, ("augmented-training",))]
    assert result.event_type == "TradDraft"
    assert result.url == public_draft_data_url(set_code="HOB", event_format="TradDraft")
    assert result.attribution == PUBLIC_DRAFT_ATTRIBUTION
    assert result.license == PUBLIC_DRAFT_LICENSE
    _assert_no_website_public_write(tmp_path)


def test_unusable_premier_falls_back_to_listed_trad_before_quick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []
    path = tmp_path / "cached-trad.csv.gz"
    path.write_bytes(_draft_csv(event_format="TradDraft"))
    source = PublicDumpSource(
        name="17lands-public-drafts",
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        retrieved_at=_NOW.isoformat(),
        attribution=PUBLIC_DRAFT_ATTRIBUTION,
        license=PUBLIC_DRAFT_LICENSE,
    )

    def acquire(*, environment: Any, cache: Any, adapter: Any) -> PublicDumpSource:
        del cache, adapter
        calls.append(environment.event_format)
        if environment.event_format == "premierdraft":
            raise ProfileInputAcquisitionError("source is unavailable")
        return source

    monkeypatch.setattr(public_data, "acquire_public_draft_source", acquire)
    result = acquire_augmented_training_source(
        set_code="HOB",
        cache=_cache(tmp_path),
        timeout_seconds=17,
        listing=_listing(
            _row(event_format="QuickDraft"),
            _row(event_format="TradDraft"),
            _row(event_format="PremierDraft"),
        ),
    )

    assert calls == ["premierdraft", "traddraft"]
    assert result.event_type == "TradDraft"
    _assert_no_website_public_write(tmp_path)


def test_picktwo_or_missing_datasets_never_reach_the_acquisition_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    def acquire(*, environment: Any, cache: Any, adapter: Any) -> PublicDumpSource:
        del cache, adapter
        calls.append(environment.event_format)
        raise AssertionError("no candidate should be attempted")

    monkeypatch.setattr(public_data, "acquire_public_draft_source", acquire)
    for listing in (_listing(_row(event_format="PickTwoDraft")), {"datasets": []}):
        with pytest.raises(
            AugmentedPublicDataError,
            match="No supported public Draft Data",
        ):
            acquire_augmented_training_source(
                set_code="HOB",
                cache=_cache(tmp_path),
                timeout_seconds=17,
                listing=listing,
            )

    assert calls == []
    _assert_no_website_public_write(tmp_path)


def test_cached_public_draft_is_reused_with_pinned_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = _cache(tmp_path)
    fetcher = _DraftFetcher(_draft_csv())
    adapter = _adapter(fetcher)
    listing = _listing(_row(event_format="PremierDraft"))

    first = acquire_augmented_training_source(
        set_code="HOB",
        cache=cache,
        timeout_seconds=17,
        listing=listing,
        adapter=adapter,
    )
    second = acquire_augmented_training_source(
        set_code="HOB",
        cache=cache,
        timeout_seconds=17,
        listing=listing,
        adapter=adapter,
    )

    expected_payload = _draft_csv()
    assert fetcher.calls == [("HOB", "premierdraft", 23)]
    assert first.event_type == second.event_type == "PremierDraft"
    assert first.url == second.url == public_draft_data_url(
        set_code="HOB", event_format="PremierDraft"
    )
    assert first.path == second.path
    assert first.sha256 == second.sha256 == hashlib.sha256(
        expected_payload
    ).hexdigest()
    assert first.retrieved_at == second.retrieved_at
    assert datetime.fromisoformat(first.retrieved_at).tzinfo is not None
    assert Path(first.path).read_bytes() == expected_payload
    assert first.attribution == PUBLIC_DRAFT_ATTRIBUTION
    assert first.license == PUBLIC_DRAFT_LICENSE
    _assert_no_website_public_write(tmp_path)


def test_invalid_listed_dumps_fall_back_to_valid_quickdraft_through_real_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[str, str]] = []

    def fetch_public_drafts(
        *,
        set_code: str,
        event_format: str,
        path: Path,
        timeout_seconds: int,
    ) -> None:
        del timeout_seconds
        calls.append((set_code, event_format))
        if event_format == "premierdraft":
            payload = _draft_csv(event_format="TradDraft")
        elif event_format == "traddraft":
            payload = _draft_csv(set_code="OTH", event_format="TradDraft")
        else:
            payload = _draft_csv(event_format="QuickDraft")
        path.write_bytes(payload)

    adapter = SeventeenLandsPublicDraftAdapter(
        fetch_public_drafts=fetch_public_drafts,
        timeout_seconds=23,
    )
    listing = _listing(
        _row(event_format="QuickDraft"),
        _row(event_format="TradDraft"),
        _row(event_format="PremierDraft"),
    )

    result = acquire_augmented_training_source(
        set_code="HOB",
        cache=_cache(tmp_path),
        timeout_seconds=17,
        listing=listing,
        adapter=adapter,
    )

    expected_payload = _draft_csv(event_format="QuickDraft")
    assert calls == [
        ("HOB", "premierdraft"),
        ("HOB", "traddraft"),
        ("HOB", "quickdraft"),
    ]
    assert result.event_type == "QuickDraft"
    assert result.url == public_draft_data_url(
        set_code="HOB",
        event_format="QuickDraft",
    )
    assert result.sha256 == hashlib.sha256(expected_payload).hexdigest()
    assert Path(result.path).read_bytes() == expected_payload
    _assert_no_website_public_write(tmp_path)


def test_invalid_set_code_is_rejected_before_catalog_or_cache_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    def fetch(*, timeout_seconds: int) -> dict[str, Any]:
        del timeout_seconds
        raise AssertionError("catalog must not be fetched for an invalid set")

    monkeypatch.setattr(public_data, "fetch_public_draft_listing", fetch)
    cache = _cache(tmp_path)
    with pytest.raises(AugmentedPublicDataError, match="Set code"):
        acquire_augmented_training_source(
            set_code="HÖB",
            cache=cache,
            timeout_seconds=17,
        )

    assert not cache.root.exists()
    _assert_no_website_public_write(tmp_path)


class _JsonResponse:
    def __init__(self, payload: bytes) -> None:
        self._stream = BytesIO(payload)

    def __enter__(self) -> _JsonResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self._stream.close()

    def read(self, size: int) -> bytes:
        return self._stream.read(size)


def _response(value: object) -> _JsonResponse:
    return _JsonResponse(json.dumps(value).encode("utf-8"))


def _install_urlopen(
    monkeypatch: pytest.MonkeyPatch, responses: list[object]
) -> list[tuple[str, int]]:
    pending = iter(responses)
    calls: list[tuple[str, int]] = []

    def urlopen(request: Any, *, timeout: int) -> _JsonResponse:
        calls.append((request.full_url, timeout))
        value = next(pending)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return _JsonResponse(value)
        return _response(value)

    monkeypatch.setattr(public_data.urllib.request, "urlopen", urlopen)
    return calls


def test_catalog_fetch_uses_master_ref_query_and_returns_document_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_urlopen(
        monkeypatch,
        [
            {
                "refs": [
                    {"id": "old", "ref": "old-ref"},
                    {
                        "id": "master",
                        "ref": "current-ref",
                        "isMasterRef": True,
                    },
                ]
            },
            {
                "total_results_size": 1,
                "results": [{"type": "public-data", "data": {"datasets": []}}],
            },
        ],
    )

    assert fetch_public_draft_listing(timeout_seconds=11) == {"datasets": []}
    assert len(calls) == 2
    assert calls[0] == ("https://17lands.cdn.prismic.io/api/v2", 11)
    endpoint, timeout = calls[1]
    parsed = urllib.parse.urlsplit(endpoint)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/api/v2/documents/search"
    assert query == {
        "q": ['[[at(document.type, "public-data")]]'],
        "pageSize": ["1"],
        "ref": ["current-ref"],
    }
    assert timeout == 11


@pytest.mark.parametrize(
    ("repository", "search"),
    [
        (b"not-json", None),
        ({"refs": []}, None),
        (
            {"refs": [{"id": "master", "ref": "ref"}]},
            {"total_results_size": 0, "results": []},
        ),
        (
            {"refs": [{"id": "master", "ref": "ref"}]},
            {
                "total_results_size": 2,
                "results": [{"type": "public-data", "data": {}}],
            },
        ),
        (
            {"refs": [{"id": "master", "ref": "ref"}]},
            {
                "total_results_size": 1,
                "results": [{"type": "wrong-type", "data": {}}],
            },
        ),
    ],
)
def test_catalog_rejects_malformed_or_ambiguous_responses(
    monkeypatch: pytest.MonkeyPatch,
    repository: object,
    search: object,
) -> None:
    responses = [repository] if search is None else [repository, search]
    _install_urlopen(monkeypatch, responses)

    with pytest.raises(AugmentedPublicDataError):
        fetch_public_draft_listing(timeout_seconds=5)


def test_catalog_unavailability_is_reported_as_a_bounded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_urlopen(monkeypatch, [urllib.error.URLError("temporary offline")])

    with pytest.raises(AugmentedPublicDataError, match="Could not fetch") as error:
        fetch_public_draft_listing(timeout_seconds=5)

    assert "temporary offline" not in str(error.value)


def test_corrupt_cached_dump_does_not_become_a_training_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    cache = _cache(tmp_path)
    fetcher = _DraftFetcher(_draft_csv())
    adapter = _adapter(fetcher)
    listing = _listing(_row(event_format="PremierDraft"))
    acquired = acquire_augmented_training_source(
        set_code="HOB",
        cache=cache,
        timeout_seconds=17,
        listing=listing,
        adapter=adapter,
    )
    Path(acquired.path).write_bytes(b"corrupt cache bytes")
    fetcher.error = RuntimeError("offline")

    with pytest.raises(AugmentedPublicDataError, match="No listed public Draft Data"):
        acquire_augmented_training_source(
            set_code="HOB",
            cache=cache,
            timeout_seconds=17,
            listing=listing,
            adapter=adapter,
        )

    assert len(fetcher.calls) == 2
    _assert_no_website_public_write(tmp_path)
