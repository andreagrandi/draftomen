from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import email.message
import hashlib
import io
import json
import logging
from pathlib import Path
import ssl
import threading
import time
from typing import Any
import urllib.error

import pytest

from draftomen.card_data_client import (
    CARD_DATA_BASE_URL,
    CARD_DATA_MAX_COMPRESSED_BYTES,
    CARD_DATA_MAX_DECOMPRESSED_BYTES,
    CARD_DATA_TIMEOUT_SECONDS,
    CARD_DATA_USER_AGENT,
    CardDataClient,
    CardDataClientError,
    card_data_cache_path,
    cached_card_data_set_codes,
)
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.set_card_data import SetCardData
from draftomen.sets_manifest import SETS_MANIFEST_URL, CardDataRecord, SetEntry, SetsManifest


class _Response:
    def __init__(self, payload: bytes, *, url: str, status: int = 200) -> None:
        self._stream = io.BytesIO(payload)
        self.url = url
        self.status = status
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True


def _card(*, set_code: str = "tst", arena_id: int = 1, name: str = "Test Card") -> CardInfo:
    return CardInfo(
        grp_id=arena_id,
        name=name,
        colors=("U",),
        mana_value=2.0,
        rarity="common",
        types=("Creature",),
        mana_cost="{1}{U}",
        oracle_text="Draw a card.",
        type_line="Creature — Wizard",
        subtypes=("Wizard",),
        set_code=set_code,
        arena_id=arena_id,
        collector_number=str(arena_id),
        oracle_id=f"oracle-{arena_id}",
    )


def _artifact(*, set_code: str = "tst") -> SetCardData:
    return SetCardData.from_card_database(
        CardDatabase(cards={1: _card(set_code=set_code)}),
        set_code=set_code,
        set_name="Test Set",
    )


def _opener(payload: bytes, calls: list[dict[str, Any]], *, url: str) -> Any:
    def open_url(request: Any, *, timeout: float) -> _Response:
        calls.append({"request": request, "timeout": timeout})
        return _Response(payload, url=url)

    return open_url


def test_cache_path_normalizes_code_and_rejects_unsafe_values(tmp_path: Path) -> None:
    assert card_data_cache_path(set_code="TsT", app_dir=tmp_path) == (
        tmp_path / "card-data" / "tst.json.gz"
    )
    for unsafe in ("", "../tst", "tst/other", "tst\\other", "tst.json.gz", " tst"):
        with pytest.raises(CardDataClientError):
            card_data_cache_path(set_code=unsafe, app_dir=tmp_path)


def test_cached_card_data_set_codes_lists_only_valid_cached_artifacts(tmp_path: Path) -> None:
    assert cached_card_data_set_codes(app_dir=tmp_path) == ()

    directory = tmp_path / "card-data"
    directory.mkdir(parents=True)
    (directory / "hob.json.gz").write_bytes(b"")
    (directory / "lci.json.gz").write_bytes(b"")
    (directory / "Bad.json.gz").write_bytes(b"")
    (directory / "notes.txt").write_bytes(b"")
    (directory / "hob.json").write_bytes(b"")
    (directory / "msh.json.gz").mkdir()

    assert cached_card_data_set_codes(app_dir=tmp_path) == ("hob", "lci")


def test_cold_load_uses_safe_url_headers_timeout_and_atomically_caches(tmp_path: Path) -> None:
    code = "tst"
    url = f"{CARD_DATA_BASE_URL}{code}.json.gz"
    payload = _artifact().to_gzip_bytes()
    calls: list[dict[str, Any]] = []
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=None,
        opener=_opener(payload, calls, url=url),
    )

    database = client.load("TST", allow_network=True)

    assert database.lookup(grp_id=1).name == "Test Card"
    assert len(calls) == 1
    request = calls[0]["request"]
    assert request.full_url == url
    assert request.get_header("Accept") == "application/gzip, application/octet-stream"
    assert request.get_header("User-agent") == CARD_DATA_USER_AGENT
    assert calls[0]["timeout"] == CARD_DATA_TIMEOUT_SECONDS
    assert client.cache_path("TST").read_bytes() == payload
    assert CARD_DATA_MAX_COMPRESSED_BYTES == 16 * 1024 * 1024
    assert CARD_DATA_MAX_DECOMPRESSED_BYTES == 64 * 1024 * 1024


def test_hosted_card_data_with_unknown_fields_loads_and_caches(tmp_path: Path) -> None:
    code = "tst"
    url = f"{CARD_DATA_BASE_URL}{code}.json.gz"
    value = _artifact().to_json()
    value["future_field"] = True
    value["cards"] = [{**card, "future_card_field": 1} for card in value["cards"]]
    payload = gzip.compress(json.dumps(value).encode())
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=None,
        opener=_opener(payload, [], url=url),
    )

    database = client.load("TST", allow_network=True)

    assert database.lookup(grp_id=1).name == "Test Card"
    assert client.cache_path("TST").read_bytes() == payload

    def fail_opener(*_: Any, **__: Any) -> Any:
        raise AssertionError("network must not be opened for a valid cache")

    cached = CardDataClient(app_dir=tmp_path, opener=fail_opener).load(
        "TST", allow_network=False
    )
    assert cached.lookup(grp_id=1).name == "Test Card"


def test_valid_cache_hit_never_opens_network(tmp_path: Path) -> None:
    destination = card_data_cache_path(set_code="tst", app_dir=tmp_path)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(_artifact().to_gzip_bytes())

    def fail_opener(*_: Any, **__: Any) -> Any:
        raise AssertionError("network must not be opened for a valid cache")

    loaded = CardDataClient(app_dir=tmp_path, opener=fail_opener).load(
        "TST", allow_network=False
    )
    assert loaded.lookup(grp_id=1).name == "Test Card"


def test_missing_and_invalid_cache_fail_offline(tmp_path: Path) -> None:
    client = CardDataClient(app_dir=tmp_path)
    with pytest.raises(CardDataClientError, match="network access is disabled"):
        client.load("tst", allow_network=False)

    destination = client.cache_path("tst")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"not gzip")
    with pytest.raises(CardDataClientError, match="network access is disabled"):
        client.load("tst", allow_network=False)


def test_invalid_cache_is_replaced_after_valid_network_refresh(tmp_path: Path) -> None:
    destination = card_data_cache_path(set_code="tst", app_dir=tmp_path)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"not gzip")
    payload = _artifact().to_gzip_bytes()
    url = f"{CARD_DATA_BASE_URL}tst.json.gz"
    calls: list[dict[str, Any]] = []

    loaded = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=None,
        opener=_opener(payload, calls, url=url),
    ).load("TST", allow_network=True)

    assert loaded.lookup(grp_id=1).name == "Test Card"
    assert len(calls) == 1
    assert destination.read_bytes() == payload


def test_malformed_gzip_and_wrong_top_level_identity_are_rejected(tmp_path: Path) -> None:
    url = f"{CARD_DATA_BASE_URL}tst.json.gz"
    malformed = CardDataClient(
        app_dir=tmp_path / "malformed",
        opener=_opener(b"not gzip", [], url=url),
    )
    with pytest.raises(CardDataClientError, match="invalid"):
        malformed.load("tst", allow_network=True)

    wrong_set = CardDataClient(
        app_dir=tmp_path / "wrong-set",
        opener=_opener(_artifact(set_code="oth").to_gzip_bytes(), [], url=url),
    )
    with pytest.raises(CardDataClientError, match="invalid"):
        wrong_set.load("tst", allow_network=True)



def test_cross_origin_redirect_is_rejected(tmp_path: Path) -> None:
    url = f"{CARD_DATA_BASE_URL}tst.json.gz"
    calls: list[dict[str, Any]] = []
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=None,
        opener=_opener(_artifact().to_gzip_bytes(), calls, url="https://evil.example/tst.json.gz"),
    )
    with pytest.raises(CardDataClientError, match="origin"):
        client.load("tst", allow_network=True)
    assert len(calls) == 1
    assert calls[0]["request"].full_url == url
    assert not client.cache_path("tst").exists()


def test_wrong_status_and_compressed_size_are_rejected(tmp_path: Path) -> None:
    payload = _artifact().to_gzip_bytes()
    url = f"{CARD_DATA_BASE_URL}tst.json.gz"

    def bad_status(request: Any, *, timeout: float) -> _Response:
        del timeout
        return _Response(payload, url=request.full_url, status=503)

    with pytest.raises(CardDataClientError, match="status 503"):
        CardDataClient(app_dir=tmp_path, opener=bad_status).load(
            "tst", allow_network=True
        )

    calls: list[dict[str, Any]] = []
    with pytest.raises(CardDataClientError, match="compressed size"):
        CardDataClient(
            app_dir=tmp_path / "size",
            opener=_opener(payload, calls, url=url),
            max_compressed_bytes=len(payload) - 1,
        ).load("tst", allow_network=True)


def test_bad_per_card_identity_is_rejected_and_does_not_install(tmp_path: Path) -> None:
    value = _artifact().to_json()
    value["cards"][0]["set_code"] = "oth"  # type: ignore[index]
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        stream.write(raw)
    payload = output.getvalue()
    url = f"{CARD_DATA_BASE_URL}tst.json.gz"
    with pytest.raises(CardDataClientError, match="invalid"):
        CardDataClient(app_dir=tmp_path, opener=_opener(payload, [], url=url)).load(
            "tst", allow_network=True
        )
    assert not card_data_cache_path(set_code="tst", app_dir=tmp_path).exists()


def test_replace_failure_preserves_existing_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = card_data_cache_path(set_code="tst", app_dir=tmp_path)
    destination.parent.mkdir(parents=True)
    previous = b"previous invalid artifact"
    destination.write_bytes(previous)
    payload = _artifact().to_gzip_bytes()
    url = f"{CARD_DATA_BASE_URL}tst.json.gz"

    def fail_replace(*_: Any, **__: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("draftomen.card_data_client.os.replace", fail_replace)
    with pytest.raises(CardDataClientError, match="install"):
        CardDataClient(app_dir=tmp_path, opener=_opener(payload, [], url=url)).load(
            "tst", allow_network=True
        )
    assert destination.read_bytes() == previous
    assert not tuple(destination.parent.glob(f".{destination.name}.*"))


def test_same_key_concurrency_fetches_once_and_installs_one_cache(tmp_path: Path) -> None:
    payload = _artifact().to_gzip_bytes()
    url = f"{CARD_DATA_BASE_URL}tst.json.gz"
    calls: list[dict[str, Any]] = []
    gate = threading.Event()

    def opener(request: Any, *, timeout: float) -> _Response:
        calls.append({"request": request, "timeout": timeout})
        gate.set()
        time.sleep(0.03)
        return _Response(payload, url=request.full_url)

    client = CardDataClient(app_dir=tmp_path, sets_manifest_url=None, opener=opener)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.load, "TST", allow_network=True)
        assert gate.wait(timeout=1)
        second = pool.submit(client.load, "tst", allow_network=True)
        assert first.result().lookup(grp_id=1).name == "Test Card"
        assert second.result().lookup(grp_id=1).name == "Test Card"
    assert len(calls) == 1
    assert calls[0]["request"].full_url == url


def _manifest_bytes(*payloads_by_set: tuple[str, bytes]) -> bytes:
    return SetsManifest(
        entries=tuple(
            SetEntry(
                set_code=set_code,
                name="Test Set",
                card_data=CardDataRecord(
                    url=f"{CARD_DATA_BASE_URL}{set_code}.json.gz",
                    bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                ),
                profiles={},
                augmented=None,
            )
            for set_code, payload in payloads_by_set
        )
    ).to_bytes()


def _routing_opener(
    routes: dict[str, bytes | Exception], requested: list[str]
) -> Any:
    def open_url(request: Any, *, timeout: float) -> _Response:
        del timeout
        requested.append(request.full_url)
        route = routes[request.full_url]
        if isinstance(route, Exception):
            raise route
        return _Response(route, url=request.full_url)

    return open_url


def _two_card_artifact() -> SetCardData:
    return SetCardData.from_card_database(
        CardDatabase(
            cards={
                1: _card(),
                2: _card(arena_id=2, name="Second Card"),
            }
        ),
        set_code="tst",
        set_name="Test Set",
    )


_CARD_URL = f"{CARD_DATA_BASE_URL}tst.json.gz"


def test_cold_load_verifies_the_download_against_the_sets_manifest(tmp_path: Path) -> None:
    payload = _artifact().to_gzip_bytes()
    requested: list[str] = []
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener(
            {SETS_MANIFEST_URL: _manifest_bytes(("tst", payload)), _CARD_URL: payload},
            requested,
        ),
    )

    database = client.load("TST", allow_network=True)

    assert database.lookup(grp_id=1).name == "Test Card"
    assert requested == [SETS_MANIFEST_URL, _CARD_URL]
    assert client.cache_path("tst").read_bytes() == payload


def test_checksum_mismatch_rejects_the_download_and_keeps_the_cached_file(
    tmp_path: Path,
) -> None:
    cached = _artifact().to_gzip_bytes()
    published = _two_card_artifact().to_gzip_bytes()
    tampered = _two_card_artifact().to_gzip_bytes() + b"\x00"
    destination = card_data_cache_path(set_code="tst", app_dir=tmp_path)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(cached)
    requested: list[str] = []
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener(
            {SETS_MANIFEST_URL: _manifest_bytes(("tst", published)), _CARD_URL: tampered},
            requested,
        ),
    )

    database = client.load("tst", allow_network=True)

    assert requested == [SETS_MANIFEST_URL, _CARD_URL]
    assert tuple(database.cards) == (1,)
    assert destination.read_bytes() == cached


def test_checksum_mismatch_without_a_cache_raises(tmp_path: Path) -> None:
    payload = _artifact().to_gzip_bytes()
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener(
            {
                SETS_MANIFEST_URL: _manifest_bytes(("tst", payload + b"x")),
                _CARD_URL: payload,
            },
            [],
        ),
    )

    with pytest.raises(CardDataClientError, match="checksum"):
        client.load("tst", allow_network=True)
    assert not client.cache_path("tst").exists()


def test_changed_checksum_downloads_once_and_unchanged_checksum_downloads_nothing(
    tmp_path: Path,
) -> None:
    old = _artifact().to_gzip_bytes()
    new = _two_card_artifact().to_gzip_bytes()
    destination = card_data_cache_path(set_code="tst", app_dir=tmp_path)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(old)
    requested: list[str] = []
    routes: dict[str, bytes | Exception] = {
        SETS_MANIFEST_URL: _manifest_bytes(("tst", new)),
        _CARD_URL: new,
    }

    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener(routes, requested),
    )
    first = client.load("tst", allow_network=True)
    second = client.load("tst", allow_network=True)

    assert tuple(first.cards) == (1, 2)
    assert tuple(second.cards) == (1, 2)
    assert requested == [SETS_MANIFEST_URL, _CARD_URL]
    assert destination.read_bytes() == new

    requested.clear()
    fresh_client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener(routes, requested),
    )
    assert tuple(fresh_client.load("tst", allow_network=True).cards) == (1, 2)
    assert requested == [SETS_MANIFEST_URL]


def test_cached_set_loads_when_the_network_is_unavailable(tmp_path: Path) -> None:
    payload = _artifact().to_gzip_bytes()
    destination = card_data_cache_path(set_code="tst", app_dir=tmp_path)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    requested: list[str] = []
    offline = OSError("network is unreachable")
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener({SETS_MANIFEST_URL: offline, _CARD_URL: offline}, requested),
    )

    database = client.load("tst", allow_network=True)

    assert database.lookup(grp_id=1).name == "Test Card"
    assert requested == [SETS_MANIFEST_URL]
    assert destination.read_bytes() == payload


def test_unreachable_manifest_still_allows_a_cold_download(tmp_path: Path) -> None:
    payload = _artifact().to_gzip_bytes()
    requested: list[str] = []
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener(
            {SETS_MANIFEST_URL: OSError("manifest unavailable"), _CARD_URL: payload},
            requested,
        ),
    )

    assert client.load("tst", allow_network=True).lookup(grp_id=1).name == "Test Card"
    assert requested == [SETS_MANIFEST_URL, _CARD_URL]


def test_published_set_codes_lists_manifest_sets_and_fetches_the_manifest_once(
    tmp_path: Path,
) -> None:
    payload = _artifact().to_gzip_bytes()
    requested: list[str] = []
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener(
            {
                SETS_MANIFEST_URL: _manifest_bytes(("zzz", payload), ("tst", payload)),
                _CARD_URL: payload,
            },
            requested,
        ),
    )

    assert client.published_set_codes() == ("tst", "zzz")
    assert client.load("tst", allow_network=True).lookup(grp_id=1).name == "Test Card"
    assert client.published_set_codes() == ("tst", "zzz")
    assert requested == [SETS_MANIFEST_URL, _CARD_URL]


def test_published_set_codes_is_none_when_the_manifest_is_disabled(tmp_path: Path) -> None:
    requested: list[str] = []
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=None,
        opener=_routing_opener({}, requested),
    )

    assert client.published_set_codes() is None
    assert requested == []


def test_published_set_codes_is_none_when_the_manifest_cannot_be_fetched(
    tmp_path: Path,
) -> None:
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener({SETS_MANIFEST_URL: OSError("offline")}, []),
    )

    assert client.published_set_codes() is None


def test_published_set_codes_is_none_when_the_manifest_is_invalid(tmp_path: Path) -> None:
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener({SETS_MANIFEST_URL: b"not json"}, []),
    )

    assert client.published_set_codes() is None


def _card_data_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "draftomen.card_data_client"
        and record.levelno == logging.WARNING
    ]


def test_ssl_failure_is_logged_with_url_and_cause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    error = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=None,
        opener=_routing_opener({_CARD_URL: error}, []),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(CardDataClientError, match="Could not fetch card-data artifact"):
            client.load("tst", allow_network=True)

    warnings = _card_data_warnings(caplog)
    assert len(warnings) == 1
    assert _CARD_URL in warnings[0].getMessage()
    assert "SSLCertVerificationError" in warnings[0].getMessage()


def test_http_error_status_is_logged_with_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    error = urllib.error.HTTPError(_CARD_URL, 403, "Forbidden", email.message.Message(), None)
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=None,
        opener=_routing_opener({_CARD_URL: error}, []),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(CardDataClientError):
            client.load("tst", allow_network=True)

    warnings = _card_data_warnings(caplog)
    assert len(warnings) == 1
    assert _CARD_URL in warnings[0].getMessage()
    assert "403" in warnings[0].getMessage()


def test_unreachable_sets_manifest_is_logged_with_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener({SETS_MANIFEST_URL: OSError("offline")}, []),
    )

    with caplog.at_level(logging.WARNING):
        assert client.published_set_codes() is None

    warnings = _card_data_warnings(caplog)
    assert len(warnings) == 1
    assert SETS_MANIFEST_URL in warnings[0].getMessage()
    assert "offline" in warnings[0].getMessage()


def test_unparsable_sets_manifest_is_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener({SETS_MANIFEST_URL: b"not json"}, []),
    )

    with caplog.at_level(logging.WARNING):
        assert client.published_set_codes() is None

    warnings = _card_data_warnings(caplog)
    assert len(warnings) == 1
    assert SETS_MANIFEST_URL in warnings[0].getMessage()


def test_successful_load_logs_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    payload = _artifact().to_gzip_bytes()
    client = CardDataClient(
        app_dir=tmp_path,
        sets_manifest_url=SETS_MANIFEST_URL,
        opener=_routing_opener(
            {SETS_MANIFEST_URL: _manifest_bytes(("tst", payload)), _CARD_URL: payload},
            [],
        ),
    )

    with caplog.at_level(logging.DEBUG):
        client.load("tst", allow_network=True)

    assert _card_data_warnings(caplog) == []
