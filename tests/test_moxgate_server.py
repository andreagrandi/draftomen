from __future__ import annotations

import http.client
import json
import logging
import queue
import socket
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from draftomen import moxgate_server
from draftomen.card_data_client import CardDataClientError
from draftomen.carddb import CardDatabase, CardDatabaseError, CardInfo
from draftomen.moxgate import MoxgateSnapshot
from draftomen.moxgate_server import (
    MOXGATE_ACCOUNT_ID,
    MOXGATE_REQUEST_HEADER,
    MOXGATE_SNAPSHOT_PATH,
    MoxgateReceiver,
    MoxgateReceiverError,
    MoxgateSessionFeeder,
)
from draftomen.pool import load_draft_state
from draftomen.session import ApplicationPhase, LiveSession
from tests.test_moxgate import (
    _all_grp_ids,
    _database,
    _packs,
    _scryfall_map,
    _snapshots,
)

ORIGIN = "chrome-extension://abcdefghijklmnop"


@pytest.fixture
def receiver() -> Iterator[MoxgateReceiver]:
    with MoxgateReceiver(port=0) as running:
        yield running


def _body(snapshot: MoxgateSnapshot) -> bytes:
    return json.dumps(
        {
            "schema_version": snapshot.schema_version,
            "pick_index": snapshot.pick_index,
            "total_picks": snapshot.total_picks,
            "pack": [
                {"scryfall_id": card.scryfall_id, "name": card.name}
                for card in snapshot.pack
            ],
            "pool": [
                {"scryfall_id": card.scryfall_id, "name": card.name}
                for card in snapshot.pool
            ],
        }
    ).encode("utf-8")


def _request(
    receiver: MoxgateReceiver,
    *,
    method: str = "POST",
    path: str = MOXGATE_SNAPSHOT_PATH,
    body: bytes | None = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", receiver.port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def _post(
    receiver: MoxgateReceiver,
    body: bytes,
    *,
    origin: str | None = ORIGIN,
    header: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin

    if header:
        headers[MOXGATE_REQUEST_HEADER] = "1"

    return _request(receiver, body=body, headers=headers)


def _session(tmp_path: Path, packs: list[list[int]]) -> LiveSession:
    return LiveSession(
        log_path=None,
        app_dir=tmp_path / "app",
        card_database=_database(*_all_grp_ids(packs)),
        event_publisher=lambda item: None,
    )


def _feeder(
    receiver: MoxgateReceiver,
    session: LiveSession,
    packs: list[list[int]],
    *,
    draft_id_factory: Callable[[], str] | None = None,
) -> MoxgateSessionFeeder:
    grp_ids = _all_grp_ids(packs)
    extra = {} if draft_id_factory is None else {"draft_id_factory": draft_id_factory}
    return MoxgateSessionFeeder(
        snapshots=receiver.snapshots,
        session=session,
        card_database=_database(*grp_ids),
        canonical_grp_ids_by_scryfall_id=_scryfall_map(*grp_ids),
        **extra,
    )


def test_posted_snapshots_drive_a_session_to_draft_complete(
    receiver: MoxgateReceiver, tmp_path: Path
) -> None:
    packs = _packs(pack_count=3, pack_size=3)
    snapshots = _snapshots(packs)
    session = _session(tmp_path, packs)
    feeder = _feeder(receiver, session, packs)

    status, _, _ = _post(receiver, _body(snapshots[0][0]))
    assert status == 202
    assert feeder.drain() == 1
    offered = session.current_pack_event
    assert offered is not None
    assert offered.offered_grp_ids == tuple(packs[0])

    for snapshot, _ in snapshots[1:]:
        status, _, _ = _post(receiver, _body(snapshot))
        assert status == 202

    assert feeder.drain() == len(snapshots) - 1
    picked = tuple(pick for _, pick in snapshots[:-1])
    assert session.snapshot.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert (
        tuple(pool_card.card.grp_id for pool_card in session.snapshot.pool.cards)
        == picked
    )


def test_complete_draft_is_saved_under_the_moxgate_account(
    receiver: MoxgateReceiver, tmp_path: Path
) -> None:
    packs = _packs(pack_count=3, pack_size=3)
    snapshots = _snapshots(packs)
    session = _session(tmp_path, packs)
    grp_ids = _all_grp_ids(packs)
    feeder = MoxgateSessionFeeder(
        snapshots=receiver.snapshots,
        session=session,
        card_database=_database(*grp_ids),
        canonical_grp_ids_by_scryfall_id=_scryfall_map(*grp_ids),
        account_id=MOXGATE_ACCOUNT_ID,
        draft_id_factory=lambda: "moxgate-draft",
    )

    for snapshot, _ in snapshots:
        assert _post(receiver, _body(snapshot))[0] == 202
    feeder.drain()

    assert session.snapshot.status.phase is ApplicationPhase.DRAFT_COMPLETE
    state = load_draft_state(
        account_id="moxgate",
        draft_id="moxgate-draft",
        app_dir=tmp_path / "app",
    )
    assert state.pool_grp_ids == tuple(pick for _, pick in snapshots[:-1])


def test_second_pack_is_offered_after_the_first_pack_is_picked(
    receiver: MoxgateReceiver, tmp_path: Path
) -> None:
    packs = _packs(pack_count=3, pack_size=3)
    snapshots = _snapshots(packs)
    session = _session(tmp_path, packs)
    feeder = _feeder(receiver, session, packs)

    for snapshot, _ in snapshots[:4]:
        assert _post(receiver, _body(snapshot))[0] == 202
    feeder.drain()

    offered = session.current_pack_event
    assert offered is not None
    assert (offered.pack_number, offered.pick_number) == (1, 0)
    assert offered.offered_grp_ids == tuple(packs[1])


@pytest.mark.parametrize(
    ("origin", "header", "expected"),
    [
        (ORIGIN, False, 400),
        ("https://www.moxgate.com", True, 403),
        (None, True, 403),
    ],
)
def test_untrusted_requests_are_rejected_without_queueing(
    receiver: MoxgateReceiver,
    tmp_path: Path,
    origin: str | None,
    header: bool,
    expected: int,
) -> None:
    packs = _packs(pack_count=1, pack_size=3)
    session = _session(tmp_path, packs)
    feeder = _feeder(receiver, session, packs)

    status, _, body = _post(
        receiver, _body(_snapshots(packs)[0][0]), origin=origin, header=header
    )

    assert status == expected
    assert "error" in json.loads(body)
    assert receiver.snapshots.empty()
    assert feeder.drain() == 0
    assert session.current_pack_event is None
    assert session.snapshot.status.phase is ApplicationPhase.WAITING_FOR_DRAFT


def test_body_over_the_cap_gets_413_without_queueing() -> None:
    with MoxgateReceiver(port=0, max_body_bytes=64) as small:
        status, _, _ = _post(small, b"x" * 65)

        assert status == 413
        assert small.snapshots.empty()


def test_missing_and_invalid_content_length_are_rejected(
    receiver: MoxgateReceiver,
) -> None:
    for length, expected in ((None, 411), ("abc", 400), ("-5", 400)):
        with socket.create_connection(("127.0.0.1", receiver.port), timeout=5) as raw:
            lines = [
                f"POST {MOXGATE_SNAPSHOT_PATH} HTTP/1.1",
                "Host: 127.0.0.1",
                f"Origin: {ORIGIN}",
                f"{MOXGATE_REQUEST_HEADER}: 1",
            ]
            if length is not None:
                lines.append(f"Content-Length: {length}")

            raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            assert raw.recv(64).split(b" ")[1] == str(expected).encode("ascii")

    assert receiver.snapshots.empty()


def test_preflight_from_a_web_origin_is_forbidden(receiver: MoxgateReceiver) -> None:
    status, headers, _ = _request(
        receiver, method="OPTIONS", body=None, headers={"Origin": "https://moxgate.com"}
    )

    assert status == 403
    assert "access-control-allow-origin" not in headers


def test_preflight_from_the_extension_origin_gets_cors_headers(
    receiver: MoxgateReceiver,
) -> None:
    status, headers, _ = _request(
        receiver, method="OPTIONS", body=None, headers={"Origin": ORIGIN}
    )

    assert status == 204
    assert headers["access-control-allow-origin"] == ORIGIN
    assert headers["access-control-allow-methods"] == "POST"
    assert (
        headers["access-control-allow-headers"]
        == f"Content-Type, {MOXGATE_REQUEST_HEADER}"
    )
    assert headers["access-control-max-age"] == "600"
    assert headers["vary"] == "Origin"


def test_accepted_response_echoes_the_extension_origin(
    receiver: MoxgateReceiver,
) -> None:
    packs = _packs(pack_count=1, pack_size=3)

    status, headers, _ = _post(receiver, _body(_snapshots(packs)[0][0]))

    assert status == 202
    assert headers["access-control-allow-origin"] == ORIGIN
    assert headers["vary"] == "Origin"


def test_malformed_and_invalid_snapshots_get_400_and_the_receiver_keeps_working(
    receiver: MoxgateReceiver, caplog: pytest.LogCaptureFixture
) -> None:
    packs = _packs(pack_count=1, pack_size=3)
    invalid = json.dumps({"schema_version": 1, "pick_index": 0}).encode("utf-8")

    with caplog.at_level(logging.WARNING, logger="draftomen.moxgate_server"):
        assert _post(receiver, b"{not json")[0] == 400
        assert _post(receiver, invalid)[0] == 400

    assert "not valid JSON" in caplog.text
    assert "missing total_picks" in caplog.text
    assert receiver.snapshots.empty()
    assert _post(receiver, _body(_snapshots(packs)[0][0]))[0] == 202
    assert receiver.snapshots.qsize() == 1


def test_unknown_path_gets_404(receiver: MoxgateReceiver) -> None:
    status, _, _ = _request(
        receiver,
        path="/other",
        headers={"Origin": ORIGIN, MOXGATE_REQUEST_HEADER: "1"},
    )

    assert status == 404


def test_full_queue_gets_503(receiver: MoxgateReceiver) -> None:
    packs = _packs(pack_count=1, pack_size=3)
    body = _body(_snapshots(packs)[0][0])
    while True:
        try:
            receiver.snapshots.put_nowait(MoxgateSnapshot.from_json(text=body))
        except queue.Full:
            break

    assert _post(receiver, body)[0] == 503


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "localhost-not-ip", ""])
def test_non_loopback_hosts_are_rejected(host: str) -> None:
    with pytest.raises(MoxgateReceiverError, match="loopback"):
        MoxgateReceiver(host=host, port=0)


def test_port_held_by_another_socket_raises_with_the_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]

        with pytest.raises(MoxgateReceiverError, match=str(port)):
            MoxgateReceiver(port=port).start()


def test_stop_releases_the_port_and_is_idempotent() -> None:
    receiver = MoxgateReceiver(port=0)
    receiver.start()
    port = receiver.port
    thread = receiver._thread
    assert thread is not None and thread.is_alive()

    receiver.stop()
    receiver.stop()

    assert not thread.is_alive()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as rebound:
        rebound.bind(("127.0.0.1", port))
        rebound.listen()


def test_stop_before_start_does_nothing() -> None:
    MoxgateReceiver(port=0).stop()


def test_feeder_logs_a_rejected_snapshot_and_keeps_processing(
    receiver: MoxgateReceiver, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    packs = _packs(pack_count=1, pack_size=4)
    snapshots = _snapshots(packs)
    session = _session(tmp_path, packs)
    feeder = _feeder(receiver, session, packs)
    for index in (0, 2, 1):
        receiver.snapshots.put_nowait(snapshots[index][0])

    with caplog.at_level(logging.WARNING, logger="draftomen.moxgate_server"):
        assert feeder.drain() == 3

    assert "pick_index expected 1 but received 2" in caplog.text
    offered = session.current_pack_event
    assert offered is not None
    assert offered.pick_number == 1


def test_feeder_starts_a_new_draft_on_a_different_first_snapshot(
    receiver: MoxgateReceiver, tmp_path: Path
) -> None:
    first_packs = _packs(pack_count=1, pack_size=3)
    second_packs = [[2000, 2001, 2002]]
    both = [*first_packs, *second_packs]
    session = _session(tmp_path, both)
    counter = iter(range(10))
    feeder = _feeder(
        receiver, session, both, draft_id_factory=lambda: f"draft-{next(counter)}"
    )

    for snapshot, _ in _snapshots(first_packs)[:2]:
        receiver.snapshots.put_nowait(snapshot)
    first_event = _snapshots(second_packs)[0][0]
    receiver.snapshots.put_nowait(first_event)
    receiver.snapshots.put_nowait(first_event)
    feeder.drain()

    offered = session.current_pack_event
    assert offered is not None
    assert offered.offered_grp_ids == tuple(second_packs[0])
    assert offered.event_name.endswith("draft-1")
    assert next(counter) == 2


def test_stalled_body_times_out_without_printing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(moxgate_server._SnapshotHandler, "timeout", 0.2)
    packs = _packs(pack_count=1, pack_size=3)

    with caplog.at_level(logging.DEBUG, logger="draftomen.moxgate_server"):
        with MoxgateReceiver(port=0) as receiver:
            with socket.create_connection(
                ("127.0.0.1", receiver.port), timeout=5
            ) as raw:
                lines = [
                    f"POST {MOXGATE_SNAPSHOT_PATH} HTTP/1.1",
                    "Host: 127.0.0.1",
                    f"Origin: {ORIGIN}",
                    f"{MOXGATE_REQUEST_HEADER}: 1",
                    "Content-Length: 100",
                ]
                raw.sendall(("\r\n".join(lines) + "\r\n\r\n{").encode("ascii"))
                raw.recv(64)

            assert _post(receiver, _body(_snapshots(packs)[0][0]))[0] == 202

    assert "timed out" in caplog.text
    assert capsys.readouterr().err == ""


def test_unexpected_handler_error_is_logged_and_not_printed(
    receiver: MoxgateReceiver,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*, text: str | bytes) -> MoxgateSnapshot:
        raise RuntimeError("boom")

    monkeypatch.setattr(moxgate_server.MoxgateSnapshot, "from_json", fail)

    with caplog.at_level(logging.WARNING, logger="draftomen.moxgate_server"):
        with pytest.raises(http.client.RemoteDisconnected):
            _post(receiver, b"{}")

    assert "failed to handle a request" in caplog.text
    assert "boom" in caplog.text
    assert capsys.readouterr().err == ""


def test_load_moxgate_card_data_maps_arena_id_oracle_fallback_and_unmapped(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "id": "sf-direct",
            "arena_id": 501,
            "oracle_id": "oracle-a",
            "name": "Direct Card",
            "colors": ["W"],
            "cmc": 2,
            "rarity": "common",
            "type_line": "Creature",
        },
        {
            "id": "sf-other-printing",
            "oracle_id": "oracle-a",
            "name": "Direct Card",
        },
        {
            "id": "sf-lower",
            "arena_id": 400,
            "oracle_id": "oracle-b",
            "name": "Lower Card",
            "colors": ["U"],
            "cmc": 3,
            "rarity": "common",
            "type_line": "Creature",
        },
        {
            "id": "sf-lower-reprint",
            "arena_id": 600,
            "oracle_id": "oracle-b",
            "name": "Lower Card",
            "colors": ["U"],
            "cmc": 3,
            "rarity": "common",
            "type_line": "Creature",
        },
        {
            "id": "sf-unmapped",
            "oracle_id": "oracle-c",
            "name": "Paper Only",
        },
    ]
    bulk_path = tmp_path / "bulk.jsonl"
    bulk_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    database, grp_ids = moxgate_server.load_moxgate_card_data(bulk_path=bulk_path)

    assert set(database.cards) == {400, 501, 600}
    assert grp_ids == {
        "sf-direct": 501,
        "sf-other-printing": 501,
        "sf-lower": 400,
        "sf-lower-reprint": 600,
    }


def test_load_moxgate_card_data_reports_malformed_bulk_file(tmp_path: Path) -> None:
    bulk_path = tmp_path / "bulk.jsonl"
    bulk_path.write_text("not json\n", encoding="utf-8")

    with pytest.raises(CardDatabaseError):
        moxgate_server.load_moxgate_card_data(bulk_path=bulk_path)


class _FakeCardDataClient:
    def __init__(
        self,
        *,
        app_dir: Path,
        codes: tuple[str, ...] | None,
        databases: dict[str, CardDatabase | Exception],
    ) -> None:
        self.app_dir = app_dir
        self._codes = codes
        self._databases = databases
        self.loaded: list[tuple[str, bool]] = []

    def published_set_codes(self) -> tuple[str, ...] | None:
        return self._codes

    def load(self, set_code: str, *, allow_network: bool) -> CardDatabase:
        self.loaded.append((set_code, allow_network))
        result = self._databases[set_code]
        if isinstance(result, Exception):
            raise result
        return result


def _hosted_database(
    *, grp_id: int, name: str, set_code: str, scryfall_id: str | None
) -> CardDatabase:
    image_uri = (
        None
        if scryfall_id is None
        else f"https://cards.scryfall.io/normal/front/{scryfall_id[0]}/"
        f"{scryfall_id[1]}/{scryfall_id}.jpg?1789"
    )
    return CardDatabase(
        cards={
            grp_id: CardInfo(
                grp_id=grp_id,
                name=name,
                colors=("W",),
                mana_value=2.0,
                rarity="common",
                types=("Creature",),
                set_code=set_code,
                image_uri=image_uri,
            )
        },
        image_uris_by_name={name.casefold(): image_uri} if image_uri else {},
    )


_SF_ONE = "79dd5c54-5ea5-47b5-8f9b-50ed57a5ea45"
_SF_TWO = "1b2c3d4e-0000-4000-8000-0123456789ab"


def test_load_hosted_moxgate_card_data_maps_image_ids_across_sets(tmp_path: Path) -> None:
    client = _FakeCardDataClient(
        app_dir=tmp_path,
        codes=("aaa", "bbb"),
        databases={
            "aaa": _hosted_database(
                grp_id=7, name="Card // Back", set_code="aaa", scryfall_id=_SF_ONE
            ),
            "bbb": _hosted_database(
                grp_id=3, name="Card // Back", set_code="bbb", scryfall_id=_SF_TWO
            ),
        },
    )

    data = moxgate_server.load_hosted_moxgate_card_data(card_data_client=client)  # type: ignore[arg-type]

    assert data.grp_ids_by_scryfall_id == {_SF_ONE: 7, _SF_TWO: 3}
    assert data.grp_ids_by_name == {"card // back": (3, 7)}
    assert set(data.card_database.cards) == {3, 7}
    assert client.loaded == [("aaa", True), ("bbb", True)]


def test_load_hosted_moxgate_card_data_keeps_lowest_grp_id_for_a_shared_scryfall_id(
    tmp_path: Path,
) -> None:
    client = _FakeCardDataClient(
        app_dir=tmp_path,
        codes=("aaa", "bbb"),
        databases={
            "aaa": _hosted_database(grp_id=9, name="A", set_code="aaa", scryfall_id=_SF_ONE),
            "bbb": _hosted_database(grp_id=4, name="A", set_code="bbb", scryfall_id=_SF_ONE),
        },
    )

    data = moxgate_server.load_hosted_moxgate_card_data(card_data_client=client)  # type: ignore[arg-type]

    assert data.grp_ids_by_scryfall_id == {_SF_ONE: 4}


def test_load_hosted_moxgate_card_data_skips_a_failing_set_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _FakeCardDataClient(
        app_dir=tmp_path,
        codes=("aaa", "bad"),
        databases={
            "aaa": _hosted_database(grp_id=7, name="A", set_code="aaa", scryfall_id=_SF_ONE),
            "bad": CardDataClientError("download broke"),
        },
    )

    with caplog.at_level(logging.WARNING, logger="draftomen.moxgate_server"):
        data = moxgate_server.load_hosted_moxgate_card_data(card_data_client=client)  # type: ignore[arg-type]

    assert data.grp_ids_by_scryfall_id == {_SF_ONE: 7}
    assert "bad" in caplog.text
    assert "download broke" in caplog.text


def test_load_hosted_moxgate_card_data_falls_back_to_cached_set_codes(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "card-data"
    cache_dir.mkdir()
    (cache_dir / "ccc.json.gz").write_bytes(b"cached")
    client = _FakeCardDataClient(
        app_dir=tmp_path,
        codes=None,
        databases={
            "ccc": _hosted_database(grp_id=8, name="C", set_code="ccc", scryfall_id=_SF_TWO)
        },
    )

    data = moxgate_server.load_hosted_moxgate_card_data(card_data_client=client)  # type: ignore[arg-type]

    assert data.grp_ids_by_scryfall_id == {_SF_TWO: 8}


def test_load_hosted_moxgate_card_data_raises_when_nothing_loads(tmp_path: Path) -> None:
    failing = _FakeCardDataClient(
        app_dir=tmp_path,
        codes=("aaa",),
        databases={"aaa": CardDataClientError("down")},
    )
    empty = _FakeCardDataClient(app_dir=tmp_path, codes=None, databases={})

    for client in (failing, empty):
        with pytest.raises(CardDatabaseError, match="Hosted card data could not be loaded"):
            moxgate_server.load_hosted_moxgate_card_data(card_data_client=client)  # type: ignore[arg-type]


def test_feeder_resolves_unknown_scryfall_ids_by_name_when_given_a_name_map(
    receiver: MoxgateReceiver, tmp_path: Path
) -> None:
    packs = _packs(pack_count=1, pack_size=3)
    grp_ids = _all_grp_ids(packs)
    session = _session(tmp_path, packs)
    partial_map = _scryfall_map(*grp_ids[:2])
    feeder = MoxgateSessionFeeder(
        snapshots=receiver.snapshots,
        session=session,
        card_database=_database(*grp_ids),
        canonical_grp_ids_by_scryfall_id=partial_map,
        grp_ids_by_name={f"fixture {grp_ids[2]}": (grp_ids[2],)},
    )
    receiver.snapshots.put(_snapshots(packs)[0][0])

    feeder.drain()

    assert session.snapshot.status.phase is ApplicationPhase.DRAFTING
