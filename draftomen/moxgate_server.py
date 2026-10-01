"""Receive Moxgate draft snapshots from the browser extension on a loopback port.
The server only queues snapshots; a feeder on the session thread turns them into draft events.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import queue
import socket
import socketserver
import sys
import threading
import uuid
import zlib
from collections.abc import Callable, Iterator, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any

from draftomen.carddb import (
    CardDatabase,
    CardDatabaseError,
    _iter_jsonl_objects,
    _open_text_bulk_file,
    build_card_database_from_scryfall_cards,
)
from draftomen.moxgate import MoxgateAdapter, MoxgateSnapshot, MoxgateSnapshotError
from draftomen.session import LiveSession

logger = logging.getLogger(__name__)

MOXGATE_DEFAULT_PORT = 47326
MOXGATE_SNAPSHOT_PATH = "/moxgate/snapshot"
MOXGATE_REQUEST_HEADER = "X-Draftomen-Moxgate"
MOXGATE_MAX_BODY_BYTES = 256 * 1024
ALLOWED_ORIGIN_PREFIX = "chrome-extension://"
MOXGATE_QUEUE_SIZE = 256
MOXGATE_REQUEST_TIMEOUT_SECONDS = 5
# serve_forever checks for shutdown this often, so stop() waits at most this long.
MOXGATE_SHUTDOWN_POLL_SECONDS = 0.05


def load_moxgate_card_data(
    *, bulk_path: Path
) -> tuple[CardDatabase, dict[str, int]]:
    """Read a Scryfall bulk file once into a card database and a Scryfall id map.
    Each id maps to its own arena_id, else the lowest grpId sharing its oracle id.
    """

    rows: list[tuple[str, str | None, int | None]] = []

    def _cards() -> Iterator[Mapping[str, Any]]:
        with _open_text_bulk_file(path=bulk_path) as bulk_file:
            for card in _iter_jsonl_objects(lines=bulk_file, source=str(bulk_path)):
                scryfall_id = card.get("id")
                if isinstance(scryfall_id, str) and scryfall_id:
                    arena_id = card.get("arena_id")
                    rows.append(
                        (
                            scryfall_id,
                            _oracle_id(card=card),
                            arena_id if isinstance(arena_id, int) else None,
                        )
                    )

                yield card

    try:
        database = build_card_database_from_scryfall_cards(cards=_cards())
    except (OSError, EOFError, zlib.error) as error:
        raise CardDatabaseError(
            f"Failed to read Scryfall bulk file {bulk_path}: {error}"
        ) from error

    grp_ids_by_oracle_id: dict[str, int] = {}
    for grp_id, info in database.cards.items():
        if info.oracle_id is not None:
            known = grp_ids_by_oracle_id.get(info.oracle_id)
            if known is None or grp_id < known:
                grp_ids_by_oracle_id[info.oracle_id] = grp_id

    grp_ids_by_scryfall_id: dict[str, int] = {}
    for scryfall_id, oracle_id, arena_id in rows:
        if arena_id is not None and arena_id in database.cards:
            grp_ids_by_scryfall_id[scryfall_id] = arena_id
        elif oracle_id is not None and oracle_id in grp_ids_by_oracle_id:
            grp_ids_by_scryfall_id[scryfall_id] = grp_ids_by_oracle_id[oracle_id]

    return database, grp_ids_by_scryfall_id


def _oracle_id(*, card: Mapping[str, Any]) -> str | None:
    oracle_id = card.get("oracle_id")
    if isinstance(oracle_id, str) and oracle_id:
        return oracle_id

    faces = card.get("card_faces")
    if isinstance(faces, list) and faces and isinstance(faces[0], Mapping):
        face_oracle_id = faces[0].get("oracle_id")
        if isinstance(face_oracle_id, str) and face_oracle_id:
            return face_oracle_id

    return None


class MoxgateReceiverError(RuntimeError):
    """Raised when the Moxgate receiver cannot start.
    The message names the host and port or the rejected host.
    """


class _ReceiverServer(HTTPServer):
    # On Windows SO_REUSEADDR lets a second socket take over a port in use.
    allow_reuse_address = sys.platform != "win32"

    def __init__(
        self,
        address: tuple[str, int],
        *,
        snapshots: queue.Queue[MoxgateSnapshot],
        max_body_bytes: int,
    ) -> None:
        self.snapshots = snapshots
        self.max_body_bytes = max_body_bytes
        # IPv6 loopback needs an IPv6 socket.
        if ":" in address[0]:
            self.address_family = socket.AF_INET6

        super().__init__(address, _SnapshotHandler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves the host name, which can stall on DNS.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback to stderr, which would break plain watch output.
        logger.warning(
            "Moxgate receiver failed to handle a request from %s",
            client_address,
            exc_info=True,
        )


class _SnapshotHandler(BaseHTTPRequestHandler):
    server: _ReceiverServer
    timeout = MOXGATE_REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("Moxgate receiver: " + format, *args)

    def do_OPTIONS(self) -> None:
        if self.path != MOXGATE_SNAPSHOT_PATH:
            self._reply_error(status=HTTPStatus.NOT_FOUND, reason="Not found")
            return

        origin = self._allowed_origin()
        if origin is None:
            self._reply_error(status=HTTPStatus.FORBIDDEN, reason="Origin not allowed")
            return

        self._send(
            status=HTTPStatus.NO_CONTENT,
            origin=origin,
            headers={
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": f"Content-Type, {MOXGATE_REQUEST_HEADER}",
                "Access-Control-Max-Age": "600",
            },
        )

    def do_POST(self) -> None:
        if self.path != MOXGATE_SNAPSHOT_PATH:
            self._reply_error(status=HTTPStatus.NOT_FOUND, reason="Not found")
            return

        origin = self._allowed_origin()
        if origin is None:
            self._reply_error(status=HTTPStatus.FORBIDDEN, reason="Origin not allowed")
            return

        if not self.headers.get(MOXGATE_REQUEST_HEADER):
            self._reply_error(
                status=HTTPStatus.BAD_REQUEST,
                reason=f"Missing {MOXGATE_REQUEST_HEADER} header",
                origin=origin,
            )
            return

        length_text = self.headers.get("Content-Length")
        if length_text is None:
            self._reply_error(
                status=HTTPStatus.LENGTH_REQUIRED,
                reason="Content-Length is required",
                origin=origin,
            )
            return

        length = _parse_length(text=length_text)
        if length is None:
            self._reply_error(
                status=HTTPStatus.BAD_REQUEST,
                reason="Content-Length must be a non-negative integer",
                origin=origin,
            )
            return

        if length > self.server.max_body_bytes:
            self._reply_error(
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                reason=f"Body is larger than {self.server.max_body_bytes} bytes",
                origin=origin,
            )
            return

        body = self.rfile.read(length)
        if len(body) != length:
            self._reply_error(
                status=HTTPStatus.BAD_REQUEST,
                reason="Body is shorter than Content-Length",
                origin=origin,
            )
            return

        try:
            snapshot = MoxgateSnapshot.from_json(text=body)
        except MoxgateSnapshotError as error:
            logger.warning("Rejected Moxgate snapshot: %s", error)
            self._reply_error(
                status=HTTPStatus.BAD_REQUEST, reason=str(error), origin=origin
            )
            return

        try:
            self.server.snapshots.put_nowait(snapshot)
        except queue.Full:
            logger.warning("Moxgate snapshot queue is full; dropped a snapshot")
            self._reply_error(
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                reason="Snapshot queue is full",
                origin=origin,
            )
            return

        self._send(status=HTTPStatus.ACCEPTED, origin=origin)

    def _allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        if origin is not None and origin.startswith(ALLOWED_ORIGIN_PREFIX):
            return origin

        return None

    def _reply_error(
        self, *, status: HTTPStatus, reason: str, origin: str | None = None
    ) -> None:
        self._send(
            status=status,
            origin=origin,
            body=json.dumps({"error": reason}).encode("utf-8"),
        )

    def _send(
        self,
        *,
        status: HTTPStatus,
        origin: str | None = None,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.send_response(status)
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

        for name, value in (headers or {}).items():
            self.send_header(name, value)

        if body:
            self.send_header("Content-Type", "application/json")

        self.send_header("Content-Length", str(len(body)))
        # An unread request body must not be parsed as the next request.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        if body:
            self.wfile.write(body)


def _parse_length(*, text: str) -> int | None:
    if not text.isascii() or not text.isdigit():
        return None

    return int(text)


class MoxgateReceiver:
    """Listen on a loopback port and queue the Moxgate snapshots posted to it.
    Only loopback hosts are accepted so the page data never leaves the machine.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = MOXGATE_DEFAULT_PORT,
        max_body_bytes: int = MOXGATE_MAX_BODY_BYTES,
    ) -> None:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False

        if not is_loopback:
            raise MoxgateReceiverError(
                f"Moxgate receiver host must be a loopback IP address, got {host!r}"
            )

        self._host = host
        self._port = port
        self._max_body_bytes = max_body_bytes
        self._snapshots: queue.Queue[MoxgateSnapshot] = queue.Queue(
            maxsize=MOXGATE_QUEUE_SIZE
        )
        self._server: _ReceiverServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Return the bound port once started.
        Before start it returns the requested port.
        """

        if self._server is not None:
            return int(self._server.server_address[1])

        return self._port

    @property
    def snapshots(self) -> queue.Queue[MoxgateSnapshot]:
        """Return the queue that holds accepted snapshots.
        The session thread drains it through a MoxgateSessionFeeder.
        """

        return self._snapshots

    def start(self) -> None:
        """Bind the port and serve requests on a daemon thread.
        Raises MoxgateReceiverError when the port cannot be bound.
        """

        if self._server is not None:
            return

        try:
            server = _ReceiverServer(
                (self._host, self._port),
                snapshots=self._snapshots,
                max_body_bytes=self._max_body_bytes,
            )
        except OSError as error:
            raise MoxgateReceiverError(
                f"Moxgate receiver could not listen on {self._host}:{self._port}: "
                f"{error}. Is another Draft Omen watch running?"
            ) from error

        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": MOXGATE_SHUTDOWN_POLL_SECONDS},
            name="draftomen-moxgate-receiver",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()
        logger.info("Moxgate receiver listening on %s:%s", self._host, self.port)

    def stop(self) -> None:
        """Stop serving and release the port.
        Calling it twice, or before start, does nothing.
        """

        server, thread = self._server, self._thread
        if server is None:
            return

        self._server = None
        self._thread = None
        self._port = int(server.server_address[1])
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join()

    def __enter__(self) -> MoxgateReceiver:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


class MoxgateSessionFeeder:
    """Turn queued Moxgate snapshots into draft events for a live session.
    Call drain on the session thread; it starts a new adapter for each new draft.
    """

    def __init__(
        self,
        *,
        snapshots: queue.Queue[MoxgateSnapshot],
        session: LiveSession,
        card_database: CardDatabase,
        canonical_grp_ids_by_scryfall_id: Mapping[str, int],
        account_id: str | None = None,
        draft_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._snapshots = snapshots
        self._session = session
        self._card_database = card_database
        self._grp_ids_by_scryfall_id = canonical_grp_ids_by_scryfall_id
        self._account_id = account_id
        self._draft_id_factory = draft_id_factory
        self._adapter: MoxgateAdapter | None = None
        self._last_accepted: MoxgateSnapshot | None = None

    def drain(self) -> int:
        """Process every queued snapshot without blocking.
        Returns how many snapshots were taken from the queue.
        """

        taken = 0
        while True:
            try:
                snapshot = self._snapshots.get_nowait()
            except queue.Empty:
                return taken

            taken += 1
            self._process(snapshot=snapshot)

    def _process(self, *, snapshot: MoxgateSnapshot) -> None:
        adapter = self._adapter
        is_new_draft = (
            adapter is None
            or snapshot.pick_index == 0
            and snapshot != self._last_accepted
        )
        if is_new_draft:
            adapter = MoxgateAdapter(
                card_database=self._card_database,
                canonical_grp_ids_by_scryfall_id=self._grp_ids_by_scryfall_id,
                draft_id=self._draft_id_factory(),
                account_id=self._account_id,
            )

        assert adapter is not None
        try:
            events = adapter.process(snapshot=snapshot)
        except MoxgateSnapshotError as error:
            logger.warning("Ignored Moxgate snapshot: %s", error)
            if is_new_draft:
                self._adapter = None

            return

        self._adapter = adapter
        self._last_accepted = snapshot
        if events:
            self._session.process_events(events=events)
