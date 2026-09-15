from __future__ import annotations

import builtins

import json
import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from draftomen.draftmancer import (
    DraftmancerAdapter,
    DraftmancerAdapterError,
    DraftmancerConfig,
    intersect_supported_set_codes,
)

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.events import (
    DraftCompletedEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
)
from draftomen.session import ApplicationPhase, LiveSession
from draftomen.test_draft import (
    TestDraftError,
    _load_canonical_grp_ids_by_scryfall_id,
)


@dataclass(frozen=True)
class _Call:
    event: str
    data: object
    timeout: float


class _FakeSocket:
    def __init__(
        self,
        *,
        start_action: Callable[[_FakeSocket], object] | None = None,
        pick_actions: Iterable[Callable[[_FakeSocket], object]] = (),
        connect_error: Exception | None = None,
    ) -> None:
        self.handlers: dict[str, Callable[[object], None]] = {}
        self.start_action = start_action
        self.pick_actions = list(pick_actions)
        self.connect_error = connect_error
        self.connect_url: str | None = None
        self.connect_kwargs: dict[str, object] = {}
        self.calls: list[_Call] = []
        self.disconnected = False

    def on(self, event: str, handler: Callable[[object], None]) -> None:
        self.handlers[event] = handler

    def connect(self, url: str, **kwargs: object) -> None:
        self.connect_url = url
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error

    def call(self, event: str, data: object, timeout: float) -> object:
        self.calls.append(_Call(event=event, data=data, timeout=timeout))
        if event == "startDraft" and self.start_action is not None:
            return self.start_action(self)
        if event == "pickCard" and self.pick_actions:
            return self.pick_actions.pop(0)(self)
        return {"code": 0}

    def disconnect(self) -> None:
        self.disconnected = True

    def emit(self, event: str, payload: object) -> None:
        handler = self.handlers[event]
        handler(payload)

    def disconnect_from_server(self, reason: str = "server closed") -> None:
        handler = self.handlers.get("disconnect")
        if handler is not None:
            handler(reason)


def _config(*, timeout_seconds: float = 1.0, set_code: str = "hob") -> DraftmancerConfig:
    return DraftmancerConfig(
        server_url="http://127.0.0.1:3000",
        session_id="session-1",
        user_id="developer-1",
        user_name="Developer",
        set_code=set_code,
        timeout_seconds=timeout_seconds,
    )


def _database(*grp_ids: int) -> CardDatabase:
    return CardDatabase(
        cards={
            grp_id: CardInfo(
                grp_id=grp_id,
                name=f"Fixture {grp_id}",
                colors=("W",),
                mana_value=2.0,
                rarity="common",
                types=("Creature",),
                set_code="hob",
            )
            for grp_id in grp_ids
        }
    )


def _seats(config: DraftmancerConfig) -> dict[str, dict[str, object]]:
    return {
        config.user_id: {
            "userID": config.user_id,
            "userName": config.user_name,
            "isBot": False,
        },
        **{
            f"bot-{index}": {
                "userID": f"bot-{index}",
                "userName": f"Bot {index}",
                "isBot": True,
            }
            for index in range(7)
        },
    }


def _state(
    *,
    pack_number: int,
    pick_number: int,
    arena_ids: tuple[int, ...],
    unique_ids: tuple[int, ...] | None = None,
) -> dict[str, object]:
    if unique_ids is None:
        unique_ids = tuple(pack_number * 100 + pick_number * 10 + index + 1 for index in range(len(arena_ids)))
    return {
        "boosterNumber": pack_number,
        "pickNumber": pick_number,
        "booster": [
            {"uniqueID": unique_id, "arena_id": arena_id}
            for unique_id, arena_id in zip(unique_ids, arena_ids, strict=True)
        ],
    }


def _adapter(
    *,
    socket: _FakeSocket,
    config: DraftmancerConfig | None = None,
    grp_ids: tuple[int, ...] = (100, 101, 102),
    sink: Callable[[object], None] | None = None,
    canonical_grp_ids_by_scryfall_id: dict[str, int] | None = None,
) -> tuple[DraftmancerAdapter, list[object]]:
    if config is None:
        config = _config()
    published: list[object] = []
    adapter = DraftmancerAdapter(
        config=config,
        card_database=_database(*grp_ids),
        canonical_grp_ids_by_scryfall_id=canonical_grp_ids_by_scryfall_id,
        event_sink=published.append if sink is None else sink,
        socket_client=socket,
    )
    return adapter, published


def _query(socket: _FakeSocket) -> dict[str, list[str]]:
    assert socket.connect_url is not None
    return parse_qs(urlsplit(socket.connect_url).query, strict_parsing=True)


def _start_action(
    config: DraftmancerConfig,
    state: dict[str, object] | None,
    *,
    ack: object = {"code": 0},
) -> Callable[[_FakeSocket], object]:
    def action(socket: _FakeSocket) -> object:
        socket.emit("startDraft", _seats(config))
        if state is not None:
            socket.emit("draftState", state)
        return ack

    return action


def _start_worker(
    *,
    target: Callable[[], None],
    workers: list[threading.Thread],
    errors: list[BaseException],
) -> threading.Event:
    done = threading.Event()

    def run() -> None:
        try:
            target()
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=run)
    workers.append(worker)
    worker.start()
    return done


def _disconnect_action(
    *,
    socket: _FakeSocket,
    reason: str,
    workers: list[threading.Thread],
    errors: list[BaseException],
    after: threading.Event | None = None,
    wait_for_delivery: bool = True,
) -> object:
    def disconnect() -> None:
        if after is not None and not after.wait(timeout=1):
            raise AssertionError("disconnect synchronization was not released")
        socket.disconnect_from_server(reason)

    done = _start_worker(target=disconnect, workers=workers, errors=errors)
    if wait_for_delivery and not done.wait(timeout=1):
        raise AssertionError("disconnect worker did not run")
    return {"code": 0}


def test_intersection_is_casefolded_sorted_unique_and_closed_over_malformed_values() -> None:
    assert intersect_supported_set_codes(
        draftomen_set_codes=("HOB", "tst", "hob", "DO-only"),
        draftmancer_set_codes=("TST", "hob", "sim-only", "HOB"),
    ) == ("hob", "tst")

    for malformed in (None, 42, "", "   "):
        with pytest.raises(DraftmancerAdapterError):
            intersect_supported_set_codes(
                draftomen_set_codes=("HOB", malformed),  # type: ignore[arg-type]
                draftmancer_set_codes=("HOB",),
            )
        with pytest.raises(DraftmancerAdapterError):
            intersect_supported_set_codes(
                draftomen_set_codes=("HOB",),
                draftmancer_set_codes=("HOB", malformed),  # type: ignore[arg-type]
            )


def test_config_validates_transport_identity_set_and_timeout() -> None:
    valid = _config(timeout_seconds=0.25, set_code="HoB")
    assert valid.set_code.casefold() == "hob"
    with pytest.raises((AttributeError, TypeError, ValueError)):
        valid.set_code = "tst"  # type: ignore[misc]

    invalid = (
        {"server_url": "", "session_id": "s", "user_id": "u", "user_name": "n", "set_code": "hob"},
        {"server_url": "ftp://localhost", "session_id": "s", "user_id": "u", "user_name": "n", "set_code": "hob"},
        {"server_url": "http://localhost", "session_id": "", "user_id": "u", "user_name": "n", "set_code": "hob"},
        {"server_url": "http://localhost", "session_id": "s", "user_id": "", "user_name": "n", "set_code": "hob"},
        {"server_url": "http://localhost", "session_id": "s", "user_id": "u", "user_name": "", "set_code": "hob"},
        {"server_url": "http://localhost", "session_id": "s", "user_id": "u", "user_name": "n", "set_code": ""},
    )
    for fields in invalid:
        with pytest.raises(DraftmancerAdapterError):
            DraftmancerConfig(**fields)
    for timeout in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(DraftmancerAdapterError):
            DraftmancerConfig(
                server_url="http://localhost",
                session_id="s",
                user_id="u",
                user_name="n",
                set_code="hob",
                timeout_seconds=timeout,
            )


def test_start_query_settings_and_acknowledgement_gate_events() -> None:
    config = _config(timeout_seconds=0.5)
    state = _state(pack_number=0, pick_number=0, arena_ids=(100,))
    socket = _FakeSocket(start_action=_start_action(config, state))
    adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))

    adapter.connect_and_start()

    query = _query(socket)
    assert query["userID"] == [config.user_id]
    assert query["userName"] == [config.user_name]
    assert query["sessionID"] == [config.session_id]
    settings = json.loads(query["sessionSettings"][0])
    assert settings == {
        "ownerIsPlayer": True,
        "bots": 7,
        "boostersPerPlayer": 3,
        "setRestriction": ["hob"],
        "pickedCardsPerRound": 1,
        "burnedCardsPerRound": 0,
        "discardRemainingCardsAt": 0,
        "reviewTimer": 0,
        "maxTimer": 0,
        "ignoreCollections": True,
    }
    assert socket.connect_kwargs["transports"] == ["websocket", "polling"]
    assert socket.connect_kwargs["wait"] is True
    assert socket.calls[0].data is None
    assert socket.calls[0].timeout == config.timeout_seconds
    assert [type(event) for event in published] == [DraftStartedEvent, PackOfferedEvent]
    started = published[0]
    assert isinstance(started, DraftStartedEvent)
    assert started == DraftStartedEvent(
        event_name="QuickDraft_HOB_Draftmancer_session-1",
        set_code="HOB",
        course_id="session-1",
        account_id="developer-1",
    )
    assert adapter.offered_instance_ids == (1,)
    assert not adapter.completed


def test_start_requires_one_configured_user_and_seven_bots() -> None:
    config = _config()
    missing_bot = _seats(config)
    missing_bot.pop("bot-6")
    two_humans = _seats(config)
    two_humans.pop("bot-6")
    two_humans["other-human"] = {
        "userID": "other-human",
        "userName": "Other",
        "isBot": False,
    }
    for seats in (missing_bot, two_humans):
        def action(
            socket: _FakeSocket,
            payload: dict[str, dict[str, object]] = seats,
        ) -> object:
            socket.emit("startDraft", payload)
            socket.emit(
                "draftState",
                _state(pack_number=0, pick_number=0, arena_ids=(100,)),
            )
            return {"code": 0}

        socket = _FakeSocket(start_action=action)
        adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))
        with pytest.raises(DraftmancerAdapterError):
            adapter.connect_and_start()
        assert published == []


def test_pick_before_connect_or_outside_offer_is_local_failure() -> None:
    config = _config()
    socket = _FakeSocket()
    adapter, published = _adapter(socket=socket, config=config)
    with pytest.raises(DraftmancerAdapterError):
        adapter.pick(unique_card_id=1)
    assert socket.calls == []
    assert published == []

    state = _state(pack_number=0, pick_number=0, arena_ids=(100,))
    socket = _FakeSocket(start_action=_start_action(config, state))
    adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))
    adapter.connect_and_start()
    with pytest.raises(DraftmancerAdapterError):
        adapter.pick(unique_card_id=999)
    assert len(socket.calls) == 1
    assert len(published) == 2


def test_rejected_start_publishes_nothing_and_enters_terminal_failure() -> None:
    config = _config()
    socket = _FakeSocket(
        start_action=_start_action(
            config,
            _state(pack_number=0, pick_number=0, arena_ids=(100,)),
            ack={"code": 1, "title": "Start rejected", "text": "not enough seats"},
        )
    )
    adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))

    with pytest.raises(DraftmancerAdapterError, match=r"^Draftmancer rejected startDraft:") as error:
        adapter.connect_and_start()
    assert published == []
    with pytest.raises(DraftmancerAdapterError) as repeated:
        adapter.connect_and_start()
    assert str(repeated.value) == str(error.value)
    adapter.close()


@pytest.mark.parametrize(
    "records",
    (
        (
            {"set": "hob", "id": "print-1", "oracle_id": "oracle-1", "arena_id": 100},
            {"set": "hob", "id": "print-1", "oracle_id": "oracle-2", "arena_id": 101},
        ),
        (
            {"set": "hob", "id": "print-1", "oracle_id": "oracle-2", "arena_id": 101},
            {"set": "hob", "id": "print-1", "oracle_id": "oracle-1", "arena_id": 100},
        ),
    ),
)
def test_scryfall_identity_mapping_rejects_conflicting_duplicate_prints(
    tmp_path: Path,
    records: tuple[dict[str, object], ...],
) -> None:
    bulk_path = tmp_path / "default-cards.jsonl"
    bulk_path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    with pytest.raises(
        TestDraftError,
        match=r"^Scryfall card print-1 has conflicting bulk records$",
    ) as error:
        _load_canonical_grp_ids_by_scryfall_id(
            bulk_path=bulk_path,
            set_code="hob",
            card_database=_database(100, 101),
        )
    assert error.value.stage == "startup"


def test_scryfall_identity_mapping_uses_canonical_arena_id_for_each_instance() -> None:
    config = _config()
    state = {
        "boosterNumber": 0,
        "pickNumber": 0,
        "booster": [
            {"uniqueID": 11, "id": "bonus-print", "arena_id": 999},
            {"uniqueID": 12, "id": "bonus-print"},
        ],
    }
    socket = _FakeSocket(start_action=_start_action(config, state))
    adapter, published = _adapter(
        socket=socket,
        config=config,
        grp_ids=(100,),
        canonical_grp_ids_by_scryfall_id={"bonus-print": 100},
    )

    adapter.connect_and_start()

    assert adapter.offered_instance_ids == (11, 12)
    assert published[1].offered_grp_ids == (100, 100)  # type: ignore[union-attr]
    adapter.close()


def test_duplicate_arena_ids_use_unique_instance_index_for_pick() -> None:
    config = _config()
    first = _state(
        pack_number=0,
        pick_number=0,
        arena_ids=(100, 100),
        unique_ids=(11, 12),
    )
    second = _state(pack_number=0, pick_number=1, arena_ids=(101,), unique_ids=(13,))
    socket = _FakeSocket(
        start_action=_start_action(config, first),
        pick_actions=[
            lambda fake: (fake.emit("draftState", second), {"code": 0})[1],
        ],
    )
    adapter, published = _adapter(socket=socket, config=config, grp_ids=(100, 101))

    adapter.connect_and_start()
    adapter.pick(unique_card_id=12)

    assert socket.calls[1].event == "pickCard"
    assert socket.calls[1].data == {"pickedCards": [1], "burnedCards": []}
    assert published[1] == PackOfferedEvent(
        event_name="QuickDraft_HOB_Draftmancer_session-1",
        set_code="HOB",
        pack_number=0,
        pick_number=0,
        offered_grp_ids=(100, 100),
        pool_grp_ids=(),
        account_id="developer-1",
    )
    assert published[2] == PickMadeEvent(
        event_name="QuickDraft_HOB_Draftmancer_session-1",
        set_code="HOB",
        pack_number=0,
        pick_number=0,
        chosen_grp_id=100,
        account_id="developer-1",
    )
    assert published[3].offered_grp_ids == (101,)  # type: ignore[union-attr]
    assert published[3].pool_grp_ids == (100,)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "booster",
    (
        [{"uniqueID": 1}],
        [{"uniqueID": 1, "arena_id": "100"}],
        [{"uniqueID": 1, "arena_id": 999}],
        [{"uniqueID": 1, "arena_id": 100}, {"uniqueID": 1, "arena_id": 100}],
    ),
)
def test_invalid_booster_identity_fails_before_pack_event(
    booster: list[dict[str, object]],
) -> None:
    config = _config()
    state = {"boosterNumber": 0, "pickNumber": 0, "booster": booster}
    socket = _FakeSocket(start_action=_start_action(config, state))
    adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))

    with pytest.raises(DraftmancerAdapterError):
        adapter.connect_and_start()
    assert not any(isinstance(event, PackOfferedEvent) for event in published)


def test_three_pack_lifecycle_orders_pick_before_queued_offer_and_completion(
    tmp_path: Any,
) -> None:
    config = _config(timeout_seconds=0.5)
    states = (
        _state(pack_number=0, pick_number=0, arena_ids=(100,)),
        _state(pack_number=0, pick_number=1, arena_ids=(101,)),
        _state(pack_number=1, pick_number=0, arena_ids=(200,)),
        _state(pack_number=2, pick_number=0, arena_ids=(300,)),
    )
    pick_published = [threading.Event() for _ in states]
    worker_errors: list[BaseException] = []
    workers: list[threading.Thread] = []

    def delayed(
        *,
        index: int,
        payloads: tuple[dict[str, object], ...],
        socket: _FakeSocket,
    ) -> None:
        if not pick_published[index].wait(timeout=1):
            raise AssertionError("next-offer synchronization was not released")
        for payload in payloads:
            socket.emit("draftState", payload)
        if index == 2:
            socket.handlers["endDraft"]()

    def pick_action(index: int) -> Callable[[_FakeSocket], object]:
        def action(socket: _FakeSocket) -> object:
            if index == 0:
                socket.emit("draftState", {
                    "boosterNumber": 0,
                    "pickNumber": 1,
                    "boosterCount": 0,
                })
                socket.emit("draftState", states[1])
            elif index in (1, 2):
                payloads = (states[2], states[2]) if index == 1 else (states[3],)
                _start_worker(
                    target=lambda: delayed(index=index, payloads=payloads, socket=socket),
                    workers=workers,
                    errors=worker_errors,
                )
            else:
                socket.handlers["endDraft"]()
            return {"code": 0}

        return action

    socket = _FakeSocket(
        start_action=_start_action(config, states[0]),
        pick_actions=[pick_action(index) for index in range(len(states))],
    )
    published: list[object] = []
    sink_thread_ids: list[int] = []
    pick_count = 0
    caller_thread_id = threading.get_ident()
    session = LiveSession(
        log_path=None,
        app_dir=tmp_path / "app",
        card_database=_database(100, 101, 200, 300),
        event_publisher=lambda item: None,
    )

    def sink(event: object) -> None:
        nonlocal pick_count
        sink_thread_ids.append(threading.get_ident())
        published.append(event)
        session.process_events(events=(event,))
        if isinstance(event, PickMadeEvent):
            pick_published[pick_count].set()
            pick_count += 1

    adapter, _ = _adapter(
        socket=socket,
        config=config,
        grp_ids=(100, 101, 200, 300),
        sink=sink,
    )
    adapter.connect_and_start()
    for state in states:
        booster = state["booster"]
        assert isinstance(booster, list)
        instance = booster[0]["uniqueID"]
        assert isinstance(instance, int)
        adapter.pick(unique_card_id=instance)
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()

    assert worker_errors == []
    assert sink_thread_ids == [caller_thread_id] * len(published)
    assert [type(event) for event in published] == [
        DraftStartedEvent,
        PackOfferedEvent,
        PickMadeEvent,
        PackOfferedEvent,
        PickMadeEvent,
        PackOfferedEvent,
        PickMadeEvent,
        PackOfferedEvent,
        PickMadeEvent,
        DraftCompletedEvent,
    ]
    offers = tuple(event for event in published if isinstance(event, PackOfferedEvent))
    picks = tuple(event for event in published if isinstance(event, PickMadeEvent))
    coordinates = ((0, 0), (0, 1), (1, 0), (2, 0))
    assert tuple((event.pack_number, event.pick_number) for event in offers) == coordinates
    assert tuple((event.pack_number, event.pick_number) for event in picks) == coordinates
    assert tuple(event.chosen_grp_id for event in picks) == (100, 101, 200, 300)
    assert tuple(event.offered_grp_ids for event in offers) == ((100,), (101,), (200,), (300,))
    completed = published[-1]
    assert isinstance(completed, DraftCompletedEvent)
    assert (completed.pack_number, completed.pick_number) == (2, 0)
    assert completed.picked_grp_ids == (100, 101, 200, 300)
    assert completed.inferred is False
    assert adapter.completed
    assert adapter.offered_instance_ids == ()
    assert session.snapshot.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert session.snapshot.pool.total_cards == 4
    assert tuple(pool_card.card.grp_id for pool_card in session.snapshot.pool.cards) == (
        100,
        101,
        200,
        300,
    )


def test_pick_rejection_and_malformed_ack_publish_no_pick_or_completion() -> None:
    config = _config()
    state = _state(pack_number=0, pick_number=0, arena_ids=(100,))
    for ack in (
        {
            "code": -1,
            "error": {"title": "Pick rejected", "text": "already picked"},
        },
        None,
    ):
        socket = _FakeSocket(
            start_action=_start_action(config, state),
            pick_actions=[lambda fake, result=ack: result],
        )
        adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))
        adapter.connect_and_start()
        with pytest.raises(
            DraftmancerAdapterError,
            match=r"^Draftmancer rejected pick:",
        ) as error:
            adapter.pick(unique_card_id=1)
        if isinstance(ack, dict):
            assert str(error.value).startswith("Draftmancer rejected pick:")
            assert "Pick rejected" in str(error.value)
            assert "already picked" in str(error.value)
        assert not any(isinstance(event, PickMadeEvent) for event in published)
        assert not any(isinstance(event, DraftCompletedEvent) for event in published)
        with pytest.raises(DraftmancerAdapterError):
            adapter.pick(unique_card_id=1)


def test_default_socket_client_reports_missing_optional_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "socketio":
            raise ModuleNotFoundError("No module named socketio")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    config = _config()
    adapter = DraftmancerAdapter(
        config=config,
        card_database=_database(100),
        event_sink=lambda event: None,
    )
    with pytest.raises(
        DraftmancerAdapterError,
        match=r"^Install Draft Omen with the 'draftmancer' extra\.$",
    ):
        adapter.connect_and_start()


def test_unavailable_service_is_transport_failure_and_terminal() -> None:
    socket = _FakeSocket(connect_error=OSError("connection refused"))
    adapter, published = _adapter(socket=socket)

    with pytest.raises(DraftmancerAdapterError, match=r"^Could not connect to Draftmancer at") as error:
        adapter.connect_and_start()
    assert published == []
    with pytest.raises(DraftmancerAdapterError) as repeated:
        adapter.connect_and_start()
    assert str(repeated.value) == str(error.value)
    adapter.close()


def test_start_and_first_offer_timeout_are_terminal() -> None:
    config = _config(timeout_seconds=0.01)
    def start_without_offer(socket: _FakeSocket) -> object:
        socket.emit("startDraft", _seats(config))
        return {"code": 0}

    socket = _FakeSocket(start_action=start_without_offer)
    adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))

    with pytest.raises(DraftmancerAdapterError, match=r"(?i)(timed out|timeout)"):
        adapter.connect_and_start()
    assert [type(event) for event in published] == [DraftStartedEvent]
    with pytest.raises(DraftmancerAdapterError):
        adapter.connect_and_start()


def test_pick_ack_timeout_has_no_fabricated_pick_or_completion() -> None:
    config = _config(timeout_seconds=0.01)
    state = _state(pack_number=0, pick_number=0, arena_ids=(100,))

    def slow_pick(socket: _FakeSocket) -> object:
        time.sleep(0.03)
        return {"code": 0}

    socket = _FakeSocket(start_action=_start_action(config, state), pick_actions=[slow_pick])
    adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))
    adapter.connect_and_start()

    with pytest.raises(DraftmancerAdapterError, match=r"(?i)(timed out|timeout)"):
        adapter.pick(unique_card_id=1)
    assert not any(isinstance(event, PickMadeEvent) for event in published)
    assert not any(isinstance(event, DraftCompletedEvent) for event in published)


def test_missing_next_offer_timeout_does_not_fabricate_completion() -> None:
    config = _config(timeout_seconds=0.01)
    state = _state(pack_number=0, pick_number=0, arena_ids=(100,))
    socket = _FakeSocket(
        start_action=_start_action(config, state),
        pick_actions=[lambda fake: {"code": 0}],
    )
    adapter, published = _adapter(socket=socket, config=config, grp_ids=(100,))
    adapter.connect_and_start()

    with pytest.raises(DraftmancerAdapterError, match=r"(?i)(timed out|timeout)"):
        adapter.pick(unique_card_id=1)
    assert any(isinstance(event, PickMadeEvent) for event in published)
    assert not any(isinstance(event, DraftCompletedEvent) for event in published)


@pytest.mark.parametrize("wait_stage", ("start_ack", "first_offer", "pick_ack", "next_offer"))
def test_disconnect_during_each_wait_is_terminal(
    wait_stage: str,
) -> None:
    config = _config(timeout_seconds=0.2)
    first = _state(pack_number=0, pick_number=0, arena_ids=(100,))
    workers: list[threading.Thread] = []
    worker_errors: list[BaseException] = []
    pick_published = threading.Event()

    if wait_stage == "start_ack":
        def start_action(socket: _FakeSocket) -> object:
            _disconnect_action(
                socket=socket,
                reason="before start acknowledgement",
                workers=workers,
                errors=worker_errors,
            )
            return {"code": 0}

        socket = _FakeSocket(start_action=start_action)
    elif wait_stage == "first_offer":
        def start_action(socket: _FakeSocket) -> object:
            socket.emit("startDraft", _seats(config))
            _disconnect_action(
                socket=socket,
                reason="before first offer",
                workers=workers,
                errors=worker_errors,
            )
            return {"code": 0}

        socket = _FakeSocket(start_action=start_action)
    elif wait_stage == "pick_ack":
        def pick_action(socket: _FakeSocket) -> object:
            _disconnect_action(
                socket=socket,
                reason="during pick acknowledgement",
                workers=workers,
                errors=worker_errors,
            )
            return {"code": 0}

        socket = _FakeSocket(
            start_action=_start_action(config, first),
            pick_actions=[pick_action],
        )
    else:
        def pick_action(socket: _FakeSocket) -> object:
            _disconnect_action(
                socket=socket,
                reason="before next offer",
                workers=workers,
                errors=worker_errors,
                after=pick_published,
                wait_for_delivery=False,
            )
            return {"code": 0}

        socket = _FakeSocket(
            start_action=_start_action(config, first),
            pick_actions=[pick_action],
        )

    published: list[object] = []

    def sink(event: object) -> None:
        published.append(event)
        if isinstance(event, PickMadeEvent):
            pick_published.set()

    adapter, _ = _adapter(
        socket=socket,
        config=config,
        grp_ids=(100,),
        sink=sink,
    )
    if wait_stage in {"start_ack", "first_offer"}:
        operation = adapter.connect_and_start
    else:
        adapter.connect_and_start()
        operation = lambda: adapter.pick(unique_card_id=1)

    with pytest.raises(
        DraftmancerAdapterError,
        match=r"^Draftmancer disconnected before",
    ) as error:
        operation()
    for worker in workers:
        worker.join(timeout=1)
        assert not worker.is_alive()
    expected_reason = {
        "start_ack": "before start acknowledgement",
        "first_offer": "before first offer",
        "pick_ack": "during pick acknowledgement",
        "next_offer": "before next offer",
    }[wait_stage]
    assert expected_reason in str(error.value)
    assert worker_errors == []
    assert "Draftmancer disconnected before" in str(error.value)
    assert not any(isinstance(event, DraftCompletedEvent) for event in published)
    if wait_stage == "pick_ack":
        assert not any(isinstance(event, PickMadeEvent) for event in published)
    if wait_stage == "next_offer":
        assert sum(isinstance(event, PickMadeEvent) for event in published) == 1
    with pytest.raises(DraftmancerAdapterError):
        adapter.connect_and_start()
    adapter.close()
