"""Bridge the pinned Draftmancer protocol into typed Draft Omen events.
Keep simulator transport isolated from the ordinary Draft Omen runtime.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from threading import Condition, RLock
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import socketio

from draftomen.carddb import CardDatabase
from draftomen.events import (
    DraftCompletedEvent,
    DraftEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
)

DRAFTMANCER_REVISION = "df08e5ef647aae54e0b1c569e70b4b2aa5e0016c"
_TRANSPORTS = ["websocket", "polling"]

__all__ = [
    "DRAFTMANCER_REVISION",
    "DraftmancerAdapter",
    "DraftmancerAdapterError",
    "DraftmancerConfig",
    "intersect_supported_set_codes",
]


class DraftmancerAdapterError(RuntimeError):
    """Raised for Draftmancer transport and protocol failures.
Only this error type crosses the adapter's caller-facing boundary.
"""


@dataclass(frozen=True, slots=True)
class DraftmancerConfig:
    """Describe one Draftmancer session and its connection deadline.
Values are validated before an adapter performs any network work.
"""

    server_url: str
    session_id: str
    user_id: str
    user_name: str
    set_code: str
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not isinstance(self.server_url, str):
            raise DraftmancerAdapterError("server_url must be a non-empty HTTP(S) URL.")
        try:
            parsed = urlsplit(self.server_url)
        except ValueError as error:
            raise DraftmancerAdapterError(
                "server_url must be a non-empty HTTP(S) URL."
            ) from error
        if (
            not self.server_url
            or parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.netloc
        ):
            raise DraftmancerAdapterError("server_url must be a non-empty HTTP(S) URL.")
        for field_name in ("session_id", "user_id", "user_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise DraftmancerAdapterError(f"{field_name} must be non-empty.")
        if (
            not isinstance(self.set_code, str)
            or not self.set_code.strip()
            or self.set_code != self.set_code.strip()
        ):
            raise DraftmancerAdapterError("set_code must be a non-empty code.")
        object.__setattr__(self, "set_code", self.set_code.casefold())
        if isinstance(self.timeout_seconds, bool):
            raise DraftmancerAdapterError("timeout_seconds must be finite and positive.")
        try:
            timeout_seconds = float(self.timeout_seconds)
        except (TypeError, ValueError) as error:
            raise DraftmancerAdapterError(
                "timeout_seconds must be finite and positive."
            ) from error
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise DraftmancerAdapterError("timeout_seconds must be finite and positive.")
        object.__setattr__(self, "timeout_seconds", timeout_seconds)


def intersect_supported_set_codes(
    *,
    draftomen_set_codes: Iterable[str],
    draftmancer_set_codes: Iterable[str],
) -> tuple[str, ...]:
    """Return the normalized set-code intersection in lexical order.
Malformed capability entries fail closed with one adapter error.
"""

    draftomen = _normalized_capability_codes(
        codes=draftomen_set_codes,
        source_name="Draft Omen",
    )
    draftmancer = _normalized_capability_codes(
        codes=draftmancer_set_codes,
        source_name="Draftmancer",
    )
    return tuple(sorted(draftomen & draftmancer))


def _validated_card_identity_map(
    *,
    values: Mapping[str, int] | None,
    card_database: CardDatabase,
) -> dict[str, int]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise DraftmancerAdapterError(
            "canonical_grp_ids_by_scryfall_id must be a mapping."
        )
    identities: dict[str, int] = {}
    for card_id, grp_id in values.items():
        if not isinstance(card_id, str) or not card_id.strip():
            raise DraftmancerAdapterError(
                "Draftmancer canonical card identifiers must be non-empty strings."
            )
        if not isinstance(grp_id, int) or isinstance(grp_id, bool):
            raise DraftmancerAdapterError(
                "Draftmancer canonical Arena ids must be integers."
            )
        identities[card_id] = grp_id
    unresolved = card_database.unresolved_grp_ids(grp_ids=identities.values())
    if unresolved:
        raise DraftmancerAdapterError(
            "Draftmancer canonical identity map contains unresolved Arena ids: "
            + ", ".join(str(grp_id) for grp_id in unresolved)
            + "."
        )
    return identities


@dataclass(frozen=True, slots=True)
class _OfferedCard:
    booster_index: int
    arena_id: int


_RawMessage = tuple[str, object]


class _Acknowledgement:
    """Hold the first acknowledgement one Socket.IO callback delivers.
Later deliveries are ignored; the adapter filters terminal states.
"""

    __slots__ = ("_delivered", "_value")

    def __init__(self) -> None:
        self._delivered = False
        self._value: object = None

    @property
    def delivered(self) -> bool:
        return self._delivered

    @property
    def value(self) -> object:
        return self._value

    def deliver(self, *args: object) -> bool:
        """Store the normalized acknowledgement and report whether it was new."""

        if self._delivered:
            return False
        self._delivered = True
        if not args:
            self._value = None
        elif len(args) == 1:
            self._value = args[0]
        else:
            self._value = args
        return True


class DraftmancerAdapter:
    """Drive one Draftmancer draft and publish typed lifecycle events.
Socket callbacks only enqueue messages; caller-thread methods own all state mutation.
"""

    def __init__(
        self,
        *,
        config: DraftmancerConfig,
        card_database: CardDatabase,
        event_sink: Callable[[DraftEvent], None],
        canonical_grp_ids_by_scryfall_id: Mapping[str, int] | None = None,
        socket_client: object | None = None,
    ) -> None:
        if not callable(event_sink):
            raise TypeError("event_sink must be callable.")
        self._config = config
        self._card_database = card_database
        self._canonical_grp_ids_by_scryfall_id = _validated_card_identity_map(
            values=canonical_grp_ids_by_scryfall_id,
            card_database=card_database,
        )
        self._event_sink = event_sink
        self._socket: object | None = socket_client
        self._condition = Condition(RLock())
        self._messages: deque[_RawMessage] = deque()
        self._terminal_error: DraftmancerAdapterError | None = None
        self._closed = False
        self._connected = False
        self._start_acknowledged = False
        self._started = False
        self._completed = False
        self._pick_in_flight = False
        self._active_offer: dict[int, _OfferedCard] = {}
        self._active_coordinates: tuple[int, int] | None = None
        self._expected_coordinates: tuple[int, int] = (0, 0)
        self._accepted_pool: list[int] = []
        self._last_pick_coordinates: tuple[int, int] | None = None
        self._last_offer_key: tuple[tuple[int, int], tuple[int, ...]] | None = None

    @property
    def offered_instance_ids(self) -> tuple[int, ...]:
        """Return active simulator instance identifiers in booster order.
        The tuple is empty while no offer is available to the caller.
        """

        with self._condition:
            return tuple(self._active_offer)

    @property
    def completed(self) -> bool:
        """Return whether a valid Draftmancer completion was published.
A terminal failure never reports completion.
"""

        with self._condition:
            return self._completed

    def connect_and_start(self) -> None:
        """Connect, acknowledge start, and publish the first offer.
The method returns only after start and first draftState are processed.
"""

        self._raise_if_unavailable(operation="connect")
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            if self._started:
                return
        socket = self._ensure_socket()
        self._register_handlers(socket=socket)
        self._connect(socket=socket)
        self._wait_for(
            predicate=lambda: self._connected,
            timeout_message="Timed out connecting to Draftmancer.",
        )
        self._call_acknowledgement(
            socket=socket,
            event="startDraft",
            data=None,
            operation="startDraft",
        )
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            self._start_acknowledged = True
        self._wait_for(
            predicate=lambda: self._started and bool(self._active_offer),
            timeout_message="Timed out waiting for Draftmancer start and first offer.",
        )

    def pick(self, *, unique_card_id: int) -> None:
        """Submit one active simulator instance and publish its next transition.
The method returns only after a subsequent offer or completion is processed.
"""

        self._raise_if_unavailable(operation="pick")
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            if not self._started:
                raise DraftmancerAdapterError("Draftmancer draft has not started.")
            if self._completed:
                raise DraftmancerAdapterError("Draftmancer draft is already complete.")
            if self._pick_in_flight:
                raise DraftmancerAdapterError("A Draftmancer pick is already in flight.")
            if not isinstance(unique_card_id, int) or isinstance(unique_card_id, bool):
                raise DraftmancerAdapterError("Draftmancer pick instance id must be an integer.")
            selected = self._active_offer.get(unique_card_id)
            if selected is None or self._active_coordinates is None:
                raise DraftmancerAdapterError(
                    f"Draftmancer pick instance {unique_card_id} is not in the active offer."
                )
            coordinates = self._active_coordinates
            self._pick_in_flight = True
        socket = self._socket
        if socket is None:
            message = DraftmancerAdapterError("Draftmancer socket is unavailable.")
            raise self._fail(message)
        self._call_acknowledgement(
            socket=socket,
            event="pickCard",
            data={"pickedCards": [selected.booster_index], "burnedCards": []},
            operation="pick",
        )
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            self._active_offer = {}
            self._active_coordinates = None
            self._pick_in_flight = False
            self._accepted_pool.append(selected.arena_id)
            self._last_pick_coordinates = coordinates
            self._expected_coordinates = (coordinates[0], coordinates[1] + 1)
            event = PickMadeEvent(
                event_name=self._event_name,
                set_code=self._config.set_code.upper(),
                pack_number=coordinates[0],
                pick_number=coordinates[1],
                chosen_grp_id=selected.arena_id,
                account_id=self._config.user_id,
            )
        self._emit(event=event)
        self._wait_for(
            predicate=lambda: self._completed or bool(self._active_offer),
            timeout_message="Timed out waiting for Draftmancer's next offer or completion.",
        )

    def cancel(self) -> None:
        """Cancel in-flight work and wake every waiter without disconnecting.
Cancellation is terminal for this adapter; close() owns transport teardown.
"""

        with self._condition:
            if self._terminal_error is not None or self._closed or self._completed:
                return
            self._terminal_error = DraftmancerAdapterError(
                "Draftmancer operation was cancelled."
            )
            self._messages.clear()
            self._condition.notify_all()

    def close(self) -> None:
        """Disconnect the socket safely and idempotently.
Explicit close wakes any waiting caller without fabricating lifecycle events.
"""

        with self._condition:
            if self._closed:
                return
            self._closed = True
            if self._connected and not self._completed and self._terminal_error is None:
                self._terminal_error = DraftmancerAdapterError(
                    "Draftmancer adapter was closed before completion."
                )
            self._messages.clear()
            self._condition.notify_all()
            socket = self._socket
            self._connected = False
        if socket is not None:
            try:
                socket.disconnect()
            except Exception:
                pass

    @property
    def _event_name(self) -> str:
        return (
            f"QuickDraft_{self._config.set_code.upper()}_Draftmancer_"
            f"{self._config.session_id}"
        )

    def _raise_if_unavailable(self, *, operation: str) -> None:
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise DraftmancerAdapterError("Draftmancer adapter is closed.")
            if operation == "pick" and not self._connected:
                raise DraftmancerAdapterError("Draftmancer adapter is not connected.")

    def _ensure_socket(self) -> object:
        if self._socket is not None:
            return self._socket
        try:
            self._socket = socketio.Client(reconnection=False)
        except Exception as error:
            message = DraftmancerAdapterError(
                f"Could not connect to Draftmancer at "
                f"{self._config.server_url}: {_error_text(error)}"
            )
            raise self._fail(message) from error
        return self._socket

    def _register_handlers(self, *, socket: object) -> None:
        try:
            socket.on(event="startDraft", handler=self._raw_event_handler("startDraft"))
            socket.on(event="draftState", handler=self._raw_event_handler("draftState"))
            socket.on(event="endDraft", handler=self._raw_event_handler("endDraft"))
            socket.on(event="connect", handler=self._connect_handler)
            socket.on(event="disconnect", handler=self._disconnect_handler)
            socket.on(event="connect_error", handler=self._connect_error_handler)
        except Exception as error:
            message = DraftmancerAdapterError(
                f"Could not connect to Draftmancer at "
                f"{self._config.server_url}: {_error_text(error)}"
            )
            raise self._fail(message) from error

    def _connect(self, *, socket: object) -> None:
        """Open the transport without blocking on the namespace handshake.
The handshake is awaited on the adapter's condition, so cancellation and the timeout both apply to it.
"""
        query = {
            "userID": self._config.user_id,
            "userName": self._config.user_name,
            "sessionID": self._config.session_id,
            "sessionSettings": json.dumps(
                {
                    "ownerIsPlayer": True,
                    "bots": 7,
                    "boostersPerPlayer": 3,
                    "setRestriction": [self._config.set_code],
                    "pickedCardsPerRound": 1,
                    "burnedCardsPerRound": 0,
                    "discardRemainingCardsAt": 0,
                    "reviewTimer": 0,
                    "maxTimer": 0,
                    "ignoreCollections": True,
                },
                separators=(",", ":"),
            ),
        }
        parsed = urlsplit(self._config.server_url)
        existing = parse_qsl(parsed.query, keep_blank_values=True)
        connection_url = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(existing + list(query.items())),
                parsed.fragment,
            )
        )
        try:
            socket.connect(
                url=connection_url,
                transports=list(_TRANSPORTS),
                wait=False,
            )
        except Exception as error:
            message = DraftmancerAdapterError(
                f"Could not connect to Draftmancer at "
                f"{self._config.server_url}: {_error_text(error)}"
            )
            raise self._fail(message) from error

    def _call_acknowledgement(
        self,
        *,
        socket: object,
        event: str,
        data: object,
        operation: str,
    ) -> object:
        started = monotonic()
        pending = _Acknowledgement()
        try:
            socket.emit(
                event=event,
                data=data,
                callback=self._acknowledgement_handler(pending=pending),
            )
        except Exception as error:
            failure = self._queued_failure()
            if failure is None:
                failure = self._ack_error(
                    error=error,
                    elapsed=monotonic() - started,
                    operation=operation,
                )
            raise self._fail(failure) from error
        acknowledgement = self._await_acknowledgement(
            pending=pending,
            operation=operation,
            deadline=started + self._config.timeout_seconds,
        )
        if monotonic() - started >= self._config.timeout_seconds:
            raise self._fail(
                DraftmancerAdapterError(
                    f"Draftmancer rejected {operation}: acknowledgement timed out."
                )
            )
        try:
            _require_success_ack(acknowledgement=acknowledgement, operation=operation)
        except DraftmancerAdapterError as error:
            failure = DraftmancerAdapterError(f"Draftmancer rejected {operation}: {error}")
            raise self._fail(failure) from error
        return acknowledgement

    def _acknowledgement_handler(
        self,
        *,
        pending: _Acknowledgement,
    ) -> Callable[..., None]:
        """Return the callback that stores one acknowledgement for its waiter.
Late callbacks after a terminal outcome are ignored, never processed.
"""

        def handler(*args: object) -> None:
            with self._condition:
                if self._terminal_error is not None or self._closed or self._completed:
                    return
                if not pending.deliver(*args):
                    return
                self._condition.notify_all()

        return handler

    def _await_acknowledgement(
        self,
        *,
        pending: _Acknowledgement,
        operation: str,
        deadline: float,
    ) -> object:
        """Wait for one acknowledgement or fail on terminal state or deadline.
Queued transport failures end the wait before the acknowledgement timeout.
"""

        while True:
            with self._condition:
                if self._terminal_error is not None:
                    raise self._terminal_error
                if self._closed:
                    raise self._fail(
                        DraftmancerAdapterError("Draftmancer adapter is closed.")
                    )
                failure = self._queued_failure()
                if failure is not None:
                    raise self._fail(failure)
                if pending.delivered:
                    return pending.value
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise self._fail(
                        DraftmancerAdapterError(
                            f"Draftmancer rejected {operation}: acknowledgement timed out."
                        )
                    )
                self._condition.wait(timeout=remaining)

    def _queued_failure(self) -> DraftmancerAdapterError | None:
        with self._condition:
            if self._terminal_error is not None:
                return self._terminal_error
            if self._closed:
                return DraftmancerAdapterError("Draftmancer adapter is closed.")
            for kind, payload in self._messages:
                if kind == "error":
                    return (
                        payload
                        if isinstance(payload, DraftmancerAdapterError)
                        else DraftmancerAdapterError(str(payload))
                    )
                if kind == "connect_error":
                    return DraftmancerAdapterError(
                        f"Could not connect to Draftmancer at "
                        f"{self._config.server_url}: {_error_text(payload)}"
                    )
                if kind == "disconnect":
                    phase = "completion" if self._started else "start"
                    return DraftmancerAdapterError(
                        f"Draftmancer disconnected before {phase}: {_error_text(payload)}"
                    )
        return None

    def _ack_error(
        self,
        *,
        error: Exception,
        elapsed: float,
        operation: str,
    ) -> DraftmancerAdapterError:
        if elapsed >= self._config.timeout_seconds or isinstance(error, TimeoutError):
            return DraftmancerAdapterError(
                f"Draftmancer rejected {operation}: acknowledgement timed out."
            )
        return DraftmancerAdapterError(f"Draftmancer rejected {operation}: {_error_text(error)}")


    def _raw_event_handler(self, event_name: str) -> Callable[..., None]:
        def handler(*args: object) -> None:
            if len(args) > 1:
                self._enqueue(
                    kind="error",
                    payload=DraftmancerAdapterError(
                        f"Malformed {event_name} event envelope."
                    ),
                )
                return
            self._enqueue(kind=event_name, payload=args[0] if args else None)

        return handler

    def _disconnect_handler(self, *args: object) -> None:
        if len(args) > 1:
            self._enqueue(
                kind="error",
                payload=DraftmancerAdapterError("Malformed disconnect event envelope."),
            )
            return
        self._enqueue(kind="disconnect", payload=args[0] if args else "unknown reason")

    def _connect_handler(self, *args: object) -> None:
        if len(args) > 1:
            self._enqueue(
                kind="error",
                payload=DraftmancerAdapterError("Malformed connect event envelope."),
            )
            return
        self._enqueue(kind="connect", payload=None)

    def _connect_error_handler(self, *args: object) -> None:
        if len(args) > 1:
            self._enqueue(
                kind="connect_error",
                payload="malformed connection error envelope",
            )
            return
        self._enqueue(kind="connect_error", payload=args[0] if args else "unknown reason")

    def _enqueue(self, *, kind: str, payload: object) -> None:
        with self._condition:
            if self._closed or self._completed or self._terminal_error is not None:
                return
            self._messages.append((kind, payload))
            self._condition.notify_all()

    def _wait_for(self, *, predicate: Callable[[], bool], timeout_message: str) -> None:
        deadline = monotonic() + self._config.timeout_seconds
        while True:
            with self._condition:
                if self._terminal_error is not None:
                    raise self._terminal_error
                if self._closed:
                    message = DraftmancerAdapterError("Draftmancer adapter is closed.")
                    raise self._fail(message)
                if predicate():
                    return
                remaining = deadline - monotonic()
                if remaining <= 0:
                    message = DraftmancerAdapterError(timeout_message)
                    raise self._fail(message)
                if self._messages:
                    message = self._messages.popleft()
                else:
                    self._condition.wait(timeout=remaining)
                    continue
            self._process_message(message=message)

    def _process_message(self, *, message: _RawMessage) -> None:
        kind, payload = message
        if kind == "error":
            error = payload if isinstance(payload, DraftmancerAdapterError) else DraftmancerAdapterError(str(payload))
            raise self._fail(error)
        if kind == "connect_error":
            error = DraftmancerAdapterError(
                f"Could not connect to Draftmancer at "
                f"{self._config.server_url}: {_error_text(payload)}"
            )
            raise self._fail(error)
        if kind == "disconnect":
            with self._condition:
                if self._completed:
                    return
                phase = "completion" if self._started else "start"
            error = DraftmancerAdapterError(
                f"Draftmancer disconnected before {phase}: {_error_text(payload)}"
            )
            raise self._fail(error)
        if kind == "connect":
            self._process_connect()
            return
        if kind == "startDraft":
            self._process_start(payload=payload)
            return
        if kind == "draftState":
            self._process_state(payload=payload)
            return
        if kind == "endDraft":
            self._process_end(payload=payload)
            return
        error = DraftmancerAdapterError(f"Unknown Draftmancer event {kind!r}.")
        raise self._fail(error)

    def _process_connect(self) -> None:
        """Record the namespace handshake that releases the connection wait.
A connect envelope after cancellation or close is dropped by _enqueue and never sets the flag; close() owns transport teardown.
"""

        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            self._connected = True

    def _process_start(self, *, payload: object) -> None:
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            if not self._start_acknowledged:
                error = DraftmancerAdapterError("Draftmancer sent startDraft before acknowledgement.")
                raise self._fail(error)
            if self._started:
                error = DraftmancerAdapterError("Draftmancer sent duplicate startDraft.")
                raise self._fail(error)
        try:
            seats = _seat_records(payload=payload)
        except DraftmancerAdapterError as error:
            raise self._fail(error)
        if len(seats) != 8:
            error = DraftmancerAdapterError("Draftmancer startDraft did not provide eight seats.")
            raise self._fail(error)
        non_bots = []
        bot_count = 0
        for seat in seats:
            if not isinstance(seat, Mapping):
                error = DraftmancerAdapterError("Draftmancer startDraft contains an invalid seat.")
                raise self._fail(error)
            is_bot = seat.get("isBot")
            if not isinstance(is_bot, bool):
                error = DraftmancerAdapterError("Draftmancer seat is missing boolean isBot.")
                raise self._fail(error)
            if is_bot:
                bot_count += 1
            else:
                non_bots.append(seat)
        if bot_count != 7 or len(non_bots) != 1 or non_bots[0].get("userID") != self._config.user_id:
            error = DraftmancerAdapterError(
                "Draftmancer startDraft seats did not contain the configured user and seven bots."
            )
            raise self._fail(error)
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            self._started = True
            event = DraftStartedEvent(
                event_name=self._event_name,
                set_code=self._config.set_code.upper(),
                course_id=self._config.session_id,
                account_id=self._config.user_id,
            )
        self._emit(event=event)

    def _process_state(self, *, payload: object) -> None:
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            if not self._start_acknowledged or not self._started:
                error = DraftmancerAdapterError("Draftmancer sent draftState before startDraft.")
                raise self._fail(error)
            if self._active_offer:
                error = DraftmancerAdapterError("Draftmancer sent draftState before the active pick.")
                raise self._fail(error)
        coordinates, offers = self._decode_state(payload=payload)
        if not offers:
            # The simulator also emits booster-less sync states while the
            # table cycles; they carry no offer and never advance the draft.
            return
        offer_key = (coordinates, tuple(offers))
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            if offer_key == self._last_offer_key:
                # The pinned simulator re-sends the identical draftState once
                # per pack transition; a repeated envelope is not a new offer.
                return
            expected = self._expected_coordinates
            if coordinates != expected and coordinates != (expected[0] + 1, 0):
                error = DraftmancerAdapterError(
                    "Draftmancer sent out-of-order draftState coordinates: "
                    f"expected {expected}, got {coordinates}."
                )
                raise self._fail(error)
            self._last_offer_key = offer_key
            self._active_coordinates = coordinates
            self._active_offer = offers
            event = PackOfferedEvent(
                event_name=self._event_name,
                set_code=self._config.set_code.upper(),
                pack_number=coordinates[0],
                pick_number=coordinates[1],
                offered_grp_ids=tuple(card.arena_id for card in offers.values()),
                pool_grp_ids=tuple(self._accepted_pool),
                account_id=self._config.user_id,
                # Each pick takes one card from the pack, so the offered count
                # plus the pick number recovers the set's booster size.
                picks_per_pack=len(offers) + coordinates[1],
            )
        self._emit(event=event)

    def _decode_state(
        self,
        *,
        payload: object,
    ) -> tuple[tuple[int, int], dict[int, _OfferedCard]]:
        try:
            if not isinstance(payload, Mapping):
                raise DraftmancerAdapterError("Draftmancer draftState must be an object.")
            booster_number = _non_negative_int(
                payload.get("boosterNumber"),
                field_name="boosterNumber",
            )
            pick_number = _non_negative_int(
                payload.get("pickNumber"),
                field_name="pickNumber",
            )
            booster = payload.get("booster")
            if booster is None:
                booster_count = payload.get("boosterCount")
                if booster_count != 0:
                    raise DraftmancerAdapterError(
                        "Draftmancer draftState without a booster must report boosterCount 0."
                    )
                booster = []
            if not isinstance(booster, list):
                raise DraftmancerAdapterError(
                    "Draftmancer draftState booster must be a list."
                )
            offers: dict[int, _OfferedCard] = {}
            labels_by_grp_id: dict[int, str] = {}
            for index, entry in enumerate(booster):
                if not isinstance(entry, Mapping):
                    raise DraftmancerAdapterError(
                        "Draftmancer booster contains an invalid card object."
                    )
                unique_id = entry.get("uniqueID")
                if not isinstance(unique_id, int) or isinstance(unique_id, bool):
                    raise DraftmancerAdapterError(
                        "Draftmancer booster uniqueID must be an integer."
                    )
                if unique_id in offers:
                    raise DraftmancerAdapterError(
                        "Draftmancer booster uniqueID values must be distinct."
                    )
                scryfall_id = entry.get("id")
                canonical_grp_id = (
                    self._canonical_grp_ids_by_scryfall_id.get(scryfall_id)
                    if isinstance(scryfall_id, str)
                    else None
                )
                if canonical_grp_id is None:
                    arena_id = entry.get("arena_id")
                    if not isinstance(arena_id, int) or isinstance(arena_id, bool):
                        raise DraftmancerAdapterError(
                            f"Draftmancer booster card {_booster_card_label(entry=entry)} "
                            "must have a mapped Scryfall id or an integer arena_id."
                        )
                    canonical_grp_id = arena_id
                offers[unique_id] = _OfferedCard(
                    booster_index=index,
                    arena_id=canonical_grp_id,
                )
                labels_by_grp_id.setdefault(
                    canonical_grp_id,
                    _booster_card_label(entry=entry),
                )
            unresolved = self._card_database.unresolved_grp_ids(grp_ids=labels_by_grp_id)
            if unresolved:
                raise DraftmancerAdapterError(
                    "Draftmancer booster contains unresolved Arena ids: "
                    + ", ".join(
                        f"{grp_id} {labels_by_grp_id[grp_id]}" for grp_id in unresolved
                    )
                    + "."
                )
            return (booster_number, pick_number), offers
        except DraftmancerAdapterError as error:
            raise self._fail(error)
        except Exception as error:
            failure = DraftmancerAdapterError(
                f"Draftmancer draftState validation failed: {_error_text(error)}"
            )
            raise self._fail(failure) from error

    def _process_end(self, *, payload: object) -> None:
        del payload
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
            if not self._start_acknowledged or not self._started:
                error = DraftmancerAdapterError("Draftmancer sent endDraft before startDraft.")
                raise self._fail(error)
            if self._pick_in_flight:
                error = DraftmancerAdapterError("Draftmancer sent endDraft while a pick was unresolved.")
                raise self._fail(error)
            if not self._accepted_pool or self._last_pick_coordinates is None:
                error = DraftmancerAdapterError("Draftmancer sent endDraft before an acknowledged pick.")
                raise self._fail(error)
            if self._active_offer:
                error = DraftmancerAdapterError("Draftmancer sent endDraft before the active pick.")
                raise self._fail(error)
            coordinates = self._last_pick_coordinates
            self._completed = True
            event = DraftCompletedEvent(
                event_name=self._event_name,
                set_code=self._config.set_code.upper(),
                pack_number=coordinates[0],
                pick_number=coordinates[1],
                picked_grp_ids=tuple(self._accepted_pool),
                inferred=False,
                account_id=self._config.user_id,
            )
        self._emit(event=event)
        with self._condition:
            self._messages.clear()

    def _emit(self, *, event: DraftEvent) -> None:
        """Publish one event to the sink without holding the adapter condition.
Only the caller thread emits, so ordering holds, and the lock never spans persistence or snapshot publication.
"""

        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._closed:
                raise self._fail(DraftmancerAdapterError("Draftmancer adapter is closed."))
        try:
            self._event_sink(event)
        except Exception as error:
            message = DraftmancerAdapterError(f"Draftmancer event sink failed: {_error_text(error)}")
            raise self._fail(message) from error
        with self._condition:
            if self._terminal_error is not None:
                raise self._terminal_error


    def _fail(self, error: DraftmancerAdapterError) -> DraftmancerAdapterError:
        with self._condition:
            if self._terminal_error is None:
                self._terminal_error = error
                self._messages.clear()
                self._condition.notify_all()
            return self._terminal_error


def _normalized_capability_codes(
    *,
    codes: Iterable[str],
    source_name: str,
) -> set[str]:
    if isinstance(codes, (str, bytes)):
        raise DraftmancerAdapterError(
            f"{source_name} set capabilities must be iterable strings."
        )
    try:
        values = iter(codes)
    except TypeError as error:
        raise DraftmancerAdapterError(
            f"{source_name} set capabilities must be iterable strings."
        ) from error
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise DraftmancerAdapterError(
                f"{source_name} set capabilities contain a malformed code."
            )
        normalized.add(value.casefold())
    return normalized


def _seat_records(*, payload: object) -> tuple[object, ...]:
    if not isinstance(payload, Mapping):
        raise DraftmancerAdapterError(
            "Draftmancer startDraft seats must be a user-ID-to-seat mapping."
        )
    for user_id, seat in payload.items():
        if not isinstance(user_id, str) or not isinstance(seat, Mapping):
            raise DraftmancerAdapterError(
                "Draftmancer startDraft seats must be a user-ID-to-seat mapping."
            )
    return tuple(payload.values())


def _booster_card_label(*, entry: Mapping[object, object]) -> str:
    """Describe a booster card by name, set and collector number for error messages.
    Missing fields fall back to the Scryfall id so the card stays identifiable.
    """

    name = entry.get("name")
    set_code = entry.get("set")
    collector_number = entry.get("collector_number")
    scryfall_id = entry.get("id")
    label = name if isinstance(name, str) and name else "unnamed card"
    if isinstance(set_code, str) and set_code and isinstance(collector_number, str):
        return f"{label} ({set_code.upper()} #{collector_number})"
    if isinstance(scryfall_id, str) and scryfall_id:
        return f"{label} (Scryfall {scryfall_id})"
    return label


def _non_negative_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DraftmancerAdapterError(
            f"Draftmancer draftState {field_name} must be a non-negative integer."
        )
    return value


def _require_success_ack(*, acknowledgement: object, operation: str) -> None:
    if not isinstance(acknowledgement, Mapping):
        raise DraftmancerAdapterError("malformed acknowledgement")
    code = acknowledgement.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise DraftmancerAdapterError("malformed acknowledgement")
    if code != 0:
        raise DraftmancerAdapterError(_ack_description(acknowledgement=acknowledgement, operation=operation))


def _ack_description(*, acknowledgement: Mapping[str, object], operation: str) -> str:
    nested = acknowledgement.get("error")
    if isinstance(nested, Mapping):
        return _error_text(nested)
    return f"server returned a nonzero {operation} code"


def _error_text(error: object) -> str:
    if isinstance(error, Mapping):
        title = error.get("title")
        text = error.get("text")
        if isinstance(title, str) and isinstance(text, str) and title and text:
            return f"{title}: {text}"
        for key in ("message", "reason", "error", "text", "title"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(dict(error), separators=(",", ":"), sort_keys=True)
    if isinstance(error, BaseException):
        return str(error) or error.__class__.__name__
    return str(error) or "unknown reason"
