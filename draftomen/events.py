"""Parse Quick, Premier, Traditional and Pick-Two draft log lines into typed events.
Keep Arena log knowledge isolated in a pure line-consumer layer.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn, TypeAlias

from draftomen.draft_format import (
    DRAFT_EVENT_PREFIXES,
    QUICK_DRAFT_PREFIX,
    QUICK_RULES,
    DraftFormat,
    DraftRules,
    detect_draft_format,
    rules_for_format,
)

logger = logging.getLogger(__name__)

EXPECTED_PACK_COUNT = QUICK_RULES.pack_count
EXPECTED_PICKS_PER_PACK = QUICK_RULES.picks_per_pack
EXPECTED_TOTAL_PICKS = QUICK_RULES.total_picks
FINAL_PACK_NUMBER = EXPECTED_PACK_COUNT - 1
FINAL_PICK_NUMBER = EXPECTED_PICKS_PER_PACK - 1

_REQUEST_LINE = re.compile(r"^\[UnityCrossThreadLogger\]==>\s+(?P<token>\S+)\s+(?P<body>\{.*\})$")
_NOTIFY_LINE = re.compile(r"^\[UnityCrossThreadLogger\]Draft\.Notify\s+(?P<body>\{.*\})$")
# Rotated UTC_Log files start each line with "[<thread>] ". The lookahead keeps
# tags such as "[Accounts - Login] Logged in" intact.
_THREAD_PREFIX = re.compile(
    r"^\[(?!UnityCrossThreadLogger\])[^\[\]]+\]\s+(?=\[UnityCrossThreadLogger\]|<==|==>|\{)"
)
_TABLE_DRAFT_QUEUE = re.compile(
    r"Entering table draft queue: (?P<set_code>[A-Za-z0-9]+)_(?P<kind>[A-Za-z0-9]+)_Draft\b"
)
_QUEUE_KIND_PREFIXES = {
    "Premier": "PremierDraft",
    "PickTwo": "PickTwoDraft",
    "Trad": "TradDraft",
}
_RESPONSE_MARKER = re.compile(r"^<==\s+(?P<token>[^()]+)\(")
_RESPONSE_ID = re.compile(r"^<==\s+(?P<token>[^()\s]+)\((?P<id>[^()]*)\)")
_MAKE_PICK_TOKEN = "EventPlayerDraftMakePick"
_COMPLETE_DRAFT_TOKEN = "DraftCompleteDraft"
_LOGIN_DISPLAY_NAME = re.compile(
    r"\[Accounts - Login\]\s+Logged in successfully\.\s+"
    r"Display Name:\s+(?P<screen_name>.+?)\s*$"
)


class DraftLogParseError(ValueError):
    """Raised when a draft-shaped log line cannot be parsed.
    The raw offending line is retained for loud diagnostics.
    """

    def __init__(self, message: str, *, raw_line: str) -> None:
        self.raw_line = raw_line
        super().__init__(f"{message}\nRaw line: {raw_line}")


@dataclass(frozen=True, slots=True)
class AccountEvent:
    """Active MTGA account detected from the log stream.
    A previous account id marks a mid-stream account change.
    """

    client_id: str
    screen_name: str | None
    previous_client_id: str | None = None


@dataclass(frozen=True, slots=True)
class QuickDraftDetectedEvent:
    """Quick Draft entry detected before Arena creates its draft course.
    This informational event lets the UI prepare set-level data before P1P1.
    """

    event_name: str
    set_code: str
    account_id: str | None


@dataclass(frozen=True, slots=True)
class DraftStartedEvent:
    """Quick Draft course start detected from Arena course state.
    Event name identifies the draft event and set code identifies the set.
    """

    event_name: str
    set_code: str
    course_id: str
    account_id: str | None


@dataclass(frozen=True, slots=True)
class PackOfferedEvent:
    """Pack contents offered for a draft pick.
    Card identifiers are normalized Arena grpIds and coordinates are 0-based.
    """

    event_name: str
    set_code: str
    pack_number: int
    pick_number: int
    offered_grp_ids: tuple[int, ...]
    pool_grp_ids: tuple[int, ...]
    account_id: str | None
    # Arena packs hold 14 picks; Mocked Draft packs from Draftmancer can hold
    # 13 or 15 depending on the set's booster layout.
    picks_per_pack: int = EXPECTED_PICKS_PER_PACK
    draft_format: DraftFormat = DraftFormat.QUICK
    # Pick-Two takes two cards for each logical pick.
    cards_per_pick: int = QUICK_RULES.cards_per_pick


@dataclass(frozen=True, slots=True)
class PickMadeEvent:
    """Cards selected at one pack and pick coordinate, as Arena grpIds.
    Quick Draft selects one card; Pick-Two selects two.
    """

    event_name: str
    set_code: str
    pack_number: int
    pick_number: int
    selected_grp_ids: tuple[int, ...]
    account_id: str | None

    def __post_init__(self) -> None:
        if not self.selected_grp_ids:
            raise ValueError("PickMadeEvent.selected_grp_ids must not be empty.")


@dataclass(frozen=True, slots=True)
class DraftCompletedEvent:
    """Quick Draft completion detected from payload or final-pick inference.
    The picked card list is the final pool snapshot from Arena.
    """

    event_name: str
    set_code: str
    pack_number: int
    pick_number: int
    picked_grp_ids: tuple[int, ...]
    inferred: bool
    account_id: str | None


DraftEvent: TypeAlias = (
    AccountEvent
    | QuickDraftDetectedEvent
    | DraftStartedEvent
    | PackOfferedEvent
    | PickMadeEvent
    | DraftCompletedEvent
)


@dataclass(frozen=True, slots=True)
class _DraftContext:
    event_name: str
    set_code: str
    rules: DraftRules
    # A queue-marker context has no dated event name, so EventJoin may still replace it.
    from_queue: bool = False


@dataclass(frozen=True, slots=True)
class _PendingPick:
    request_id: str
    draft_id: str
    coordinate: tuple[int, int]
    cards: tuple[int, ...]
    context: _DraftContext
    account_id: str | None


@dataclass(slots=True)
class _HumanDraftRecord:
    # draft_id is None when the draft was only seen through its completion.
    draft_id: str | None
    picks: dict[tuple[int, int], tuple[int, ...]] = field(default_factory=dict)
    completed: bool = False


@dataclass(slots=True)
class _ParserState:
    account_id: str | None = None
    pending_login_screen_name: str | None = None
    screen_names_by_client_id: dict[str, str] = field(default_factory=dict)
    observed_quick_draft_course_ids: set[str] = field(default_factory=set)
    login_generation: int = 0
    # Draft.Notify carries no event name, so packs use the last event Arena joined.
    draft_context: _DraftContext | None = None
    last_notify: tuple[str, int, int, tuple[int, ...]] | None = None
    # A human-draft pick waits for its response, and the response body arrives on
    # the line after its "<== token(id)" marker.
    pending_pick: _PendingPick | None = None
    expected_body: tuple[str, str] | None = None
    human_draft: _HumanDraftRecord | None = None
    seen_pick_request_ids: set[str] = field(default_factory=set)
    emitted_pick_requests: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Events produced while a line changes state, yielded before that line's own events.
    outbox: list[DraftEvent] = field(default_factory=list)


class DraftLogParser:
    """Incrementally parse Quick Draft log lines.
    Parser state preserves the active account across live polling batches.
    """

    def __init__(self) -> None:
        self._state = _ParserState()

    def parse_lines(self, *, lines: Iterable[str]) -> Iterator[DraftEvent]:
        """Yield typed Quick Draft events from complete log lines.
        The function consumes strings only and performs no I/O.
        """

        for raw_line in lines:
            line = raw_line.rstrip("\r\n")
            yield from _parse_line(line=line, state=self._state)

    def flush(self) -> tuple[DraftEvent, ...]:
        """Return the human-draft pick still waiting for a response.
        Call it after the last line of a finite log so that pick is not lost.
        """

        return _flush_pending_pick(state=self._state)

    @property
    def pending_login_screen_name(self) -> str | None:
        """Return the latest login name that lacks an authenticated account id.
        Callers may use it only when recovery leaves one unambiguous account.
        """

        return self._state.pending_login_screen_name

    @property
    def observed_quick_draft_course_ids(self) -> frozenset[str]:
        """Return Quick Draft course ids seen in current course snapshots.
        Snapshots associate an otherwise unbound login name with saved drafts.
        """

        return frozenset(self._state.observed_quick_draft_course_ids)

    @property
    def login_generation(self) -> int:
        """Return the count of login boundaries observed in the log stream.
        Consumers use it to discard account context from the prior login.
        """

        return self._state.login_generation


def parse_events(lines: Iterable[str]) -> Iterator[DraftEvent]:
    """Yield typed Quick Draft events from log lines.
    The function consumes strings only and performs no I/O.
    """

    parser = DraftLogParser()
    yield from parser.parse_lines(lines=lines)
    yield from parser.flush()


def _parse_line(line: str, state: _ParserState) -> tuple[DraftEvent, ...]:
    stripped = _THREAD_PREFIX.sub("", line.strip(), count=1)
    if not stripped:
        return ()

    human_events = _parse_human_draft_line(stripped=stripped, state=state)
    if human_events is not None:
        return human_events

    events = _parse_general_line(line=line, stripped=stripped, state=state)
    if events:
        state.outbox.extend(_flush_pending_pick(state=state))

    queued = tuple(state.outbox)
    state.outbox.clear()
    return (*queued, *events)


def _parse_general_line(
    *,
    line: str,
    stripped: str,
    state: _ParserState,
) -> tuple[DraftEvent, ...]:
    if "BotDraft_Draft" in stripped:
        _raise(
            "Unsupported Quick Draft token; expected current BotDraftDraft* format",
            raw_line=line,
        )

    login_match = _LOGIN_DISPLAY_NAME.search(stripped)
    if login_match is not None:
        state.outbox.extend(_flush_pending_pick(state=state))
        state.human_draft = None
        state.seen_pick_request_ids.clear()
        state.emitted_pick_requests.clear()
        state.account_id = None
        state.pending_login_screen_name = login_match.group("screen_name").strip()
        state.screen_names_by_client_id.clear()
        state.observed_quick_draft_course_ids.clear()
        state.draft_context = None
        state.last_notify = None
        state.login_generation += 1
        return ()

    queue_match = _TABLE_DRAFT_QUEUE.search(stripped)
    if queue_match is not None:
        prefix = _QUEUE_KIND_PREFIXES.get(queue_match.group("kind"))
        if prefix is not None:
            _remember_draft_event(
                event_name=f"{prefix}_{queue_match.group('set_code')}",
                state=state,
                from_queue=True,
            )
        return ()

    notify_match = _NOTIFY_LINE.match(stripped)
    if notify_match is not None:
        return _parse_draft_notify(body=notify_match.group("body"), state=state)

    request_match = _REQUEST_LINE.match(stripped)
    if request_match is not None:
        return _parse_request_line(
            token=request_match.group("token"),
            body=request_match.group("body"),
            raw_line=line,
            state=state,
        )

    response_match = _RESPONSE_MARKER.match(stripped)
    if response_match is not None:
        token = response_match.group("token")
        if token in {"BotDraftDraftStatus", "BotDraftDraftPick"}:
            return ()

        if "BotDraft" in token:
            _raise(f"Unsupported BotDraft response token {token!r}", raw_line=line)

        return ()

    if stripped.startswith("{"):
        if not _json_line_may_contain_events(stripped):
            return ()
        return _parse_json_line(text=stripped, raw_line=line, state=state)

    if _contains_unparsed_draft_shape(stripped):
        _raise("Unknown draft-shaped log line", raw_line=line)

    return ()


def _parse_request_line(
    *,
    token: str,
    body: str,
    raw_line: str,
    state: _ParserState,
) -> tuple[DraftEvent, ...]:
    if token == "EventJoin":
        return _parse_event_join_request(
            body=body,
            raw_line=raw_line,
            state=state,
        )

    if token == "BotDraftDraftStatus":
        request = _request_payload(body=body, raw_line=raw_line)
        event_name = _required_str(
            request.get("EventName"),
            field_name="request.EventName",
            raw_line=raw_line,
        )
        _set_code(event_name=event_name, raw_line=raw_line)
        return ()

    if token == "BotDraftDraftPick":
        request = _request_payload(body=body, raw_line=raw_line)
        pick_info = _required_mapping(
            request.get("PickInfo"),
            field_name="request.PickInfo",
            raw_line=raw_line,
        )
        event_name = _required_str(
            pick_info.get("EventName", request.get("EventName")),
            field_name="request.PickInfo.EventName",
            raw_line=raw_line,
        )
        set_code = _set_code(event_name=event_name, raw_line=raw_line)
        card_ids = _int_tuple(
            pick_info.get("CardIds"),
            field_name="request.PickInfo.CardIds",
            raw_line=raw_line,
        )
        if len(card_ids) != 1:
            _raise(
                "Quick Draft pick request must contain exactly one CardIds entry",
                raw_line=raw_line,
            )

        pack_number = _required_int(
            pick_info.get("PackNumber"),
            field_name="request.PickInfo.PackNumber",
            raw_line=raw_line,
        )
        pick_number = _required_int(
            pick_info.get("PickNumber"),
            field_name="request.PickInfo.PickNumber",
            raw_line=raw_line,
        )
        return (
            PickMadeEvent(
                event_name=event_name,
                set_code=set_code,
                pack_number=pack_number,
                pick_number=pick_number,
                selected_grp_ids=(card_ids[0],),
                account_id=state.account_id,
            ),
        )

    if "BotDraft" in token:
        _raise(f"Unsupported BotDraft request token {token!r}", raw_line=raw_line)

    return ()


def _parse_event_join_request(
    *,
    body: str,
    raw_line: str,
    state: _ParserState,
) -> tuple[DraftEvent, ...]:
    if QUICK_DRAFT_PREFIX not in raw_line:
        _remember_join_request_event(body=body, raw_line=raw_line, state=state)
        return ()

    request = _request_payload(body=body, raw_line=raw_line)
    event_name = _required_str(
        request.get("EventName"),
        field_name="request.EventName",
        raw_line=raw_line,
    )
    if not event_name.startswith(QUICK_DRAFT_PREFIX):
        # The substring gate above also matches names such as PickTwoQuickDraft_.
        return ()

    set_code = _set_code(event_name=event_name, raw_line=raw_line)
    _remember_draft_event(event_name=event_name, state=state)
    return (
        QuickDraftDetectedEvent(
            event_name=event_name,
            set_code=set_code,
            account_id=state.account_id,
        ),
    )


def _remember_join_request_event(
    *,
    body: str,
    raw_line: str,
    state: _ParserState,
) -> None:
    if not any(prefix in raw_line for prefix in DRAFT_EVENT_PREFIXES):
        return

    # Human drafts share the EventJoin token with every other event, so a request
    # this parser cannot read must not fail the live loop.
    try:
        request = _request_payload(body=body, raw_line=raw_line)
    except DraftLogParseError:
        return

    event_name = request.get("EventName")
    if isinstance(event_name, str):
        _remember_draft_event(event_name=event_name, state=state)


def _remember_draft_event(
    *,
    event_name: str,
    state: _ParserState,
    from_queue: bool = False,
) -> None:
    draft_format = detect_draft_format(event_name=event_name)
    if draft_format is None:
        return

    parts = event_name.split("_")
    if len(parts) < 2 or parts[1] == "":
        return

    context = state.draft_context
    if context is not None and context.event_name == event_name:
        return

    if context is not None and _same_draft(
        context=context,
        draft_format=draft_format,
        set_code=parts[1],
    ):
        # The pool store rejects a renamed event, so one draft keeps one name.
        if from_queue:
            return
        if context.from_queue:
            if state.last_notify is None:
                state.draft_context = _DraftContext(
                    event_name=event_name,
                    set_code=context.set_code,
                    rules=context.rules,
                )
            return

    state.outbox.extend(_flush_pending_pick(state=state))
    state.human_draft = None
    state.draft_context = _DraftContext(
        event_name=event_name,
        set_code=parts[1],
        rules=rules_for_format(draft_format=draft_format),
        from_queue=from_queue,
    )
    state.last_notify = None


def _same_draft(*, context: _DraftContext, draft_format: DraftFormat, set_code: str) -> bool:
    return (
        context.set_code == set_code
        and detect_draft_format(event_name=context.event_name) == draft_format
    )


def _parse_draft_notify(*, body: str, state: _ParserState) -> tuple[DraftEvent, ...]:
    # Notify lines come from untrusted logs and Arena logs each one twice, so any
    # record that does not fit the active draft is dropped without raising.
    context = state.draft_context
    if context is None:
        return ()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ()

    if not isinstance(payload, dict):
        return ()

    draft_id = payload.get("draftId")
    pack = payload.get("SelfPack")
    pick = payload.get("SelfPick")
    offered_grp_ids = _notify_card_ids(payload.get("PackCards"))
    rules = context.rules
    if (
        not isinstance(draft_id, str)
        or draft_id == ""
        or not _is_plain_int(pack)
        or not 1 <= pack <= rules.pack_count
        or not _is_plain_int(pick)
        or not 1 <= pick <= rules.picks_per_pack
        or offered_grp_ids is None
    ):
        return ()

    notify_key = (draft_id, pack, pick, offered_grp_ids)
    if notify_key == state.last_notify:
        return ()

    state.last_notify = notify_key
    # The pool store checks each pack's pool against the picks it has seen, so the
    # pick for the previous coordinate must be recorded and yielded first.
    pick_events = _flush_pending_pick(state=state)
    record = state.human_draft
    pool_grp_ids = _recorded_cards(
        record=record if record is not None and record.draft_id == draft_id else None
    )
    return (
        *pick_events,
        PackOfferedEvent(
            event_name=context.event_name,
            set_code=context.set_code,
            pack_number=pack - 1,
            pick_number=pick - 1,
            offered_grp_ids=offered_grp_ids,
            pool_grp_ids=pool_grp_ids,
            account_id=state.account_id,
            picks_per_pack=rules.picks_per_pack,
            draft_format=rules.draft_format,
            cards_per_pick=rules.cards_per_pick,
        ),
    )


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _notify_card_ids(value: Any) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None

    parts = [part.strip() for part in value.split(",")]
    if not all(part.isascii() and part.isdigit() for part in parts):
        return None

    return tuple(int(part) for part in parts)


def _parse_human_draft_line(
    *,
    stripped: str,
    state: _ParserState,
) -> tuple[DraftEvent, ...] | None:
    # Human-draft lines come from untrusted logs, so nothing here raises. None
    # means the line is not a human-draft line and the general parser takes it.
    expected = state.expected_body
    state.expected_body = None
    if expected is not None:
        body_events = _parse_response_body(expected=expected, text=stripped, state=state)
        if body_events is not None:
            return body_events

    request_match = _REQUEST_LINE.match(stripped)
    if request_match is not None:
        token = request_match.group("token")
        body = request_match.group("body")
        if token == _MAKE_PICK_TOKEN:
            return _parse_pick_request(body=body, state=state)

        if token == _COMPLETE_DRAFT_TOKEN:
            return _parse_complete_request(body=body, state=state)

        return None

    marker_match = _RESPONSE_ID.match(stripped)
    if marker_match is not None and marker_match.group("token") in {
        _MAKE_PICK_TOKEN,
        _COMPLETE_DRAFT_TOKEN,
    }:
        state.expected_body = (marker_match.group("token"), marker_match.group("id"))
        return ()

    return None


def _human_context(*, state: _ParserState) -> _DraftContext | None:
    context = state.draft_context
    if context is None or context.rules.draft_format is DraftFormat.QUICK:
        return None

    return context


def _human_request(*, body: str) -> tuple[str, dict[str, Any]] | None:
    try:
        envelope = json.loads(body)
        request_id = envelope["id"]
        payload = json.loads(envelope["request"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None

    if not isinstance(request_id, str) or request_id == "" or not isinstance(payload, dict):
        return None

    return request_id, payload


def _parse_pick_request(*, body: str, state: _ParserState) -> tuple[DraftEvent, ...]:
    context = _human_context(state=state)
    request = _human_request(body=body)
    if context is None or request is None:
        return ()

    request_id, payload = request
    draft_id = payload.get("DraftId")
    pack = payload.get("Pack")
    pick = payload.get("Pick")
    cards = payload.get("GrpIds")
    rules = context.rules
    if (
        not isinstance(draft_id, str)
        or draft_id == ""
        or not _is_plain_int(pack)
        or not 1 <= pack <= rules.pack_count
        or not _is_plain_int(pick)
        or not 1 <= pick <= rules.picks_per_pack
        or not isinstance(cards, list)
        or not cards
        or not all(_is_plain_int(card) and card > 0 for card in cards)
    ):
        return ()

    coordinate = (pack - 1, pick - 1)
    pending = state.pending_pick
    record = state.human_draft
    if request_id in state.seen_pick_request_ids:
        return ()

    if pending is not None and (pending.draft_id, pending.coordinate) == (draft_id, coordinate):
        return ()

    if record is not None and record.draft_id == draft_id:
        if record.completed or coordinate in record.picks:
            return ()

    events = _flush_pending_pick(state=state)
    state.seen_pick_request_ids.add(request_id)
    state.pending_pick = _PendingPick(
        request_id=request_id,
        draft_id=draft_id,
        coordinate=coordinate,
        cards=tuple(cards),
        context=context,
        account_id=state.account_id,
    )
    return events


def _flush_pending_pick(*, state: _ParserState) -> tuple[DraftEvent, ...]:
    pending = state.pending_pick
    if pending is None:
        return ()

    state.pending_pick = None
    record = state.human_draft
    if record is None or record.draft_id != pending.draft_id:
        record = _HumanDraftRecord(draft_id=pending.draft_id)
        state.human_draft = record

    if record.completed or pending.coordinate in record.picks:
        return ()

    rules = pending.context.rules
    pack_number, pick_number = pending.coordinate
    if len(pending.cards) != rules.cards_per_pick:
        # The format decides the pick shape; the cards stay exactly as logged.
        logger.warning(
            "Pick in %s at pack %d pick %d has %d cards, expected %d for %s",
            pending.context.event_name,
            pack_number,
            pick_number,
            len(pending.cards),
            rules.cards_per_pick,
            rules.draft_format.value,
        )

    record.picks[pending.coordinate] = pending.cards
    state.emitted_pick_requests[pending.request_id] = pending.coordinate
    return (
        PickMadeEvent(
            event_name=pending.context.event_name,
            set_code=pending.context.set_code,
            pack_number=pack_number,
            pick_number=pick_number,
            selected_grp_ids=pending.cards,
            account_id=pending.account_id,
        ),
    )


def _parse_complete_request(*, body: str, state: _ParserState) -> tuple[DraftEvent, ...]:
    context = _human_context(state=state)
    request = _human_request(body=body)
    if context is None or request is None:
        return ()

    _, payload = request
    events = _flush_pending_pick(state=state)
    if payload.get("EventName") != context.event_name or payload.get("IsBotDraft") is not False:
        return events

    return (*events, *_complete_human_draft(state=state, card_pool=None, draft_id=None))


def _parse_response_body(
    *,
    expected: tuple[str, str],
    text: str,
    state: _ParserState,
) -> tuple[DraftEvent, ...] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    token, response_id = expected
    if token == _MAKE_PICK_TOKEN:
        if "IsPickSuccessful" not in data and "IsPickingCompleted" not in data:
            return None

        return _parse_pick_response(response_id=response_id, data=data, state=state)

    if "InternalEventName" not in data and "CardPool" not in data:
        return None

    return _parse_complete_response(data=data, state=state)


def _parse_pick_response(
    *,
    response_id: str,
    data: dict[str, Any],
    state: _ParserState,
) -> tuple[DraftEvent, ...]:
    context = _human_context(state=state)
    if context is None:
        return ()

    failed = data.get("IsPickSuccessful") is False
    pending = state.pending_pick
    events: tuple[DraftEvent, ...] = ()
    if pending is not None and pending.request_id == response_id:
        if failed:
            state.pending_pick = None
            return ()

        events = _flush_pending_pick(state=state)
    elif failed and response_id in state.emitted_pick_requests:
        # The pick already left the parser, so it cannot be taken back.
        pack_number, pick_number = state.emitted_pick_requests[response_id]
        logger.warning(
            "Arena rejected a pick in %s at pack %d pick %d after it was recorded",
            context.event_name,
            pack_number,
            pick_number,
        )

    if data.get("IsPickingCompleted") is True and not failed:
        events = (*events, *_complete_human_draft(state=state, card_pool=None, draft_id=None))

    return events


def _parse_complete_response(
    *,
    data: dict[str, Any],
    state: _ParserState,
) -> tuple[DraftEvent, ...]:
    context = _human_context(state=state)
    if context is None or data.get("InternalEventName") != context.event_name:
        return ()

    pool = data.get("CardPool")
    card_pool: tuple[int, ...] | None = None
    if isinstance(pool, list) and all(_is_plain_int(card) for card in pool):
        card_pool = tuple(pool)

    draft_id = data.get("DraftId")
    return _complete_human_draft(
        state=state,
        card_pool=card_pool,
        draft_id=draft_id if isinstance(draft_id, str) and draft_id != "" else None,
    )


def _recorded_cards(*, record: _HumanDraftRecord | None) -> tuple[int, ...]:
    if record is None:
        return ()

    return tuple(card for _, cards in sorted(record.picks.items()) for card in cards)


def _complete_human_draft(
    *,
    state: _ParserState,
    card_pool: tuple[int, ...] | None,
    draft_id: str | None,
) -> tuple[DraftEvent, ...]:
    context = _human_context(state=state)
    if context is None:
        return ()

    events = _flush_pending_pick(state=state)
    rules = context.rules
    record = state.human_draft
    if record is None:
        record = _HumanDraftRecord(draft_id=draft_id)
        state.human_draft = record

    recorded_cards = _recorded_cards(record=record)
    if not record.completed:
        # A gap in the picks stays a gap; the card pool only stands in when no
        # pick was recorded at all.
        picked_grp_ids = recorded_cards or card_pool or ()
        if picked_grp_ids:
            record.completed = True
            events = (
                *events,
                DraftCompletedEvent(
                    event_name=context.event_name,
                    set_code=context.set_code,
                    pack_number=rules.pack_count - 1,
                    pick_number=rules.picks_per_pack - 1,
                    picked_grp_ids=picked_grp_ids,
                    inferred=False,
                    account_id=state.account_id,
                ),
            )

    if card_pool is not None and record.picks and Counter(card_pool) != Counter(recorded_cards):
        logger.warning(
            "Card pool of %s has %d cards but %d were recorded from %d of %d picks",
            context.event_name,
            len(card_pool),
            len(recorded_cards),
            len(record.picks),
            rules.total_picks,
        )

    return events


def _request_payload(*, body: str, raw_line: str) -> dict[str, Any]:
    envelope = _json_object(text=body, raw_line=raw_line, context="request envelope")
    request_text = _required_str(
        envelope.get("request"),
        field_name="request envelope.request",
        raw_line=raw_line,
    )
    return _json_object(text=request_text, raw_line=raw_line, context="request payload")


def _parse_json_line(
    *,
    text: str,
    raw_line: str,
    state: _ParserState,
) -> tuple[DraftEvent, ...]:
    data = _json_object(text=text, raw_line=raw_line, context="JSON log line")
    _remember_quick_draft_course_ids(data=data, state=state)
    _remember_player_draft_course(data=data, state=state)

    if "authenticateResponse" in data:
        account = _parse_account(data=data, raw_line=raw_line, state=state)
        return (account,)

    if "Course" in data:
        started = _parse_course(data=data, raw_line=raw_line, state=state)
        if started is not None:
            return (started,)
        return ()

    if "CurrentModule" in data and "Payload" in data:
        if not _payload_line_is_draft_shaped(data=data):
            return ()
        return _parse_module_payload(data=data, raw_line=raw_line, state=state)

    if _mapping_contains_draft_shape(data):
        _raise("Unknown draft-shaped JSON log line", raw_line=raw_line)

    return ()


def _remember_quick_draft_course_ids(
    *,
    data: dict[str, Any],
    state: _ParserState,
) -> None:
    course_values: list[Any] = [data.get("Course")]
    courses = data.get("Courses")
    if isinstance(courses, list):
        course_values.extend(courses)

    for course in course_values:
        if not isinstance(course, dict):
            continue

        event_name = course.get("InternalEventName")
        course_id = course.get("CourseId")
        if (
            isinstance(event_name, str)
            and event_name.startswith(QUICK_DRAFT_PREFIX)
            and isinstance(course_id, str)
            and course_id != ""
        ):
            state.observed_quick_draft_course_ids.add(course_id)


def _remember_player_draft_course(*, data: dict[str, Any], state: _ParserState) -> None:
    # The EventJoin response wraps the course, while resume snapshots are flat.
    course = data.get("Course")
    if not isinstance(course, dict):
        course = data

    event_name = course.get("InternalEventName")
    if (
        course.get("CurrentModule") == "PlayerDraft"
        and isinstance(course.get("CourseId"), str)
        and isinstance(event_name, str)
    ):
        _remember_draft_event(event_name=event_name, state=state)


def _parse_account(
    *,
    data: dict[str, Any],
    raw_line: str,
    state: _ParserState,
) -> AccountEvent:
    response = _required_mapping(
        data.get("authenticateResponse"),
        field_name="authenticateResponse",
        raw_line=raw_line,
    )
    client_id = _required_str(
        response.get("clientId"),
        field_name="authenticateResponse.clientId",
        raw_line=raw_line,
    )
    screen_name = _screen_name_for_account(
        response=response,
        client_id=client_id,
        raw_line=raw_line,
        state=state,
    )

    previous_client_id = state.account_id if state.account_id != client_id else None
    state.account_id = client_id
    state.pending_login_screen_name = None
    if screen_name is not None:
        state.screen_names_by_client_id[client_id] = screen_name
    return AccountEvent(
        client_id=client_id,
        screen_name=screen_name,
        previous_client_id=previous_client_id,
    )


def _screen_name_for_account(
    *,
    response: dict[str, Any],
    client_id: str,
    raw_line: str,
    state: _ParserState,
) -> str | None:
    screen_name_value = response.get("screenName")
    if screen_name_value is not None:
        screen_name = _required_str(
            screen_name_value,
            field_name="authenticateResponse.screenName",
            raw_line=raw_line,
        )
        if screen_name != client_id:
            return screen_name

    if state.pending_login_screen_name is not None:
        return state.pending_login_screen_name

    return state.screen_names_by_client_id.get(client_id)


def _parse_course(
    *,
    data: dict[str, Any],
    raw_line: str,
    state: _ParserState,
) -> DraftStartedEvent | None:
    course = _required_mapping(
        data.get("Course"),
        field_name="Course",
        raw_line=raw_line,
    )
    current_module = course.get("CurrentModule")
    event_name_value = course.get("InternalEventName")
    if current_module != "BotDraft":
        return None

    event_name = _required_str(
        event_name_value,
        field_name="Course.InternalEventName",
        raw_line=raw_line,
    )
    set_code = _set_code(event_name=event_name, raw_line=raw_line)
    course_id = _required_str(
        course.get("CourseId"),
        field_name="Course.CourseId",
        raw_line=raw_line,
    )
    return DraftStartedEvent(
        event_name=event_name,
        set_code=set_code,
        course_id=course_id,
        account_id=state.account_id,
    )


def _parse_module_payload(
    *,
    data: dict[str, Any],
    raw_line: str,
    state: _ParserState,
) -> tuple[DraftEvent, ...]:
    module = _required_str(
        data.get("CurrentModule"),
        field_name="CurrentModule",
        raw_line=raw_line,
    )
    if module not in {"BotDraft", "DeckSelect"}:
        _raise(f"Unsupported draft CurrentModule {module!r}", raw_line=raw_line)

    payload_text = _required_str(
        data.get("Payload"),
        field_name="Payload",
        raw_line=raw_line,
    )
    payload = _json_object(
        text=payload_text,
        raw_line=raw_line,
        context="module Payload",
    )
    result = _required_str(
        payload.get("Result"),
        field_name="Payload.Result",
        raw_line=raw_line,
    )
    if result != "Success":
        _raise(f"Draft payload result was {result!r}, not 'Success'", raw_line=raw_line)

    event_name = _required_str(
        payload.get("EventName"),
        field_name="Payload.EventName",
        raw_line=raw_line,
    )
    set_code = _set_code(event_name=event_name, raw_line=raw_line)
    pack_number = _required_int(
        payload.get("PackNumber"),
        field_name="Payload.PackNumber",
        raw_line=raw_line,
    )
    pick_number = _required_int(
        payload.get("PickNumber"),
        field_name="Payload.PickNumber",
        raw_line=raw_line,
    )
    offered_grp_ids = _int_tuple(
        payload.get("DraftPack"),
        field_name="Payload.DraftPack",
        raw_line=raw_line,
    )
    picked_grp_ids = _int_tuple(
        payload.get("PickedCards"),
        field_name="Payload.PickedCards",
        raw_line=raw_line,
    )
    status_value = payload.get("DraftStatus")
    if status_value is None:
        status = None
    else:
        status = _required_str(
            status_value,
            field_name="Payload.DraftStatus",
            raw_line=raw_line,
        )

    if status == "Completed":
        if offered_grp_ids:
            _raise(
                "Completed draft payload must not include offered cards",
                raw_line=raw_line,
            )
        return (
            DraftCompletedEvent(
                event_name=event_name,
                set_code=set_code,
                pack_number=pack_number,
                pick_number=pick_number,
                picked_grp_ids=picked_grp_ids,
                inferred=False,
                account_id=state.account_id,
            ),
        )

    if _is_completion_shape(
        pack_number=pack_number,
        pick_number=pick_number,
        offered_grp_ids=offered_grp_ids,
        picked_grp_ids=picked_grp_ids,
    ):
        return (
            DraftCompletedEvent(
                event_name=event_name,
                set_code=set_code,
                pack_number=pack_number,
                pick_number=pick_number,
                picked_grp_ids=picked_grp_ids,
                inferred=True,
                account_id=state.account_id,
            ),
        )

    if status is None:
        _raise(
            "Missing Payload.DraftStatus outside final completion shape",
            raw_line=raw_line,
        )

    if status != "PickNext":
        _raise(f"Unsupported draft status {status!r}", raw_line=raw_line)

    if module != "BotDraft":
        _raise("PickNext payload must use CurrentModule BotDraft", raw_line=raw_line)

    if not offered_grp_ids:
        _raise("PickNext payload must include offered DraftPack cards", raw_line=raw_line)

    return (
        PackOfferedEvent(
            event_name=event_name,
            set_code=set_code,
            pack_number=pack_number,
            pick_number=pick_number,
            offered_grp_ids=offered_grp_ids,
            pool_grp_ids=picked_grp_ids,
            account_id=state.account_id,
        ),
    )


def _is_completion_shape(
    *,
    pack_number: int,
    pick_number: int,
    offered_grp_ids: tuple[int, ...],
    picked_grp_ids: tuple[int, ...],
) -> bool:
    return (
        pack_number == FINAL_PACK_NUMBER
        and pick_number == FINAL_PICK_NUMBER
        and offered_grp_ids == ()
        and len(picked_grp_ids) == EXPECTED_TOTAL_PICKS
    )


def _json_object(*, text: str, raw_line: str, context: str) -> dict[str, Any]:
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        _raise(f"Malformed {context}: {error.msg}", raw_line=raw_line)

    if not isinstance(decoded, dict):
        _raise(f"Malformed {context}: expected JSON object", raw_line=raw_line)

    return decoded


def _required_mapping(value: Any, *, field_name: str, raw_line: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _raise(f"Missing or invalid {field_name}; expected object", raw_line=raw_line)

    return value


def _required_str(value: Any, *, field_name: str, raw_line: str) -> str:
    if not isinstance(value, str) or value == "":
        _raise(
            f"Missing or invalid {field_name}; expected non-empty string",
            raw_line=raw_line,
        )

    return value


def _required_int(value: Any, *, field_name: str, raw_line: str) -> int:
    if isinstance(value, bool):
        _raise(f"Missing or invalid {field_name}; expected integer", raw_line=raw_line)

    try:
        return int(value)
    except (TypeError, ValueError):
        _raise(f"Missing or invalid {field_name}; expected integer", raw_line=raw_line)


def _int_tuple(value: Any, *, field_name: str, raw_line: str) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _raise(
            f"Missing or invalid {field_name}; expected list of grpIds",
            raw_line=raw_line,
        )

    return tuple(
        _required_int(item, field_name=f"{field_name}[]", raw_line=raw_line)
        for item in value
    )


def _set_code(*, event_name: str, raw_line: str) -> str:
    if not event_name.startswith(QUICK_DRAFT_PREFIX):
        _raise(
            f"Unsupported draft event {event_name!r}; expected QuickDraft",
            raw_line=raw_line,
        )

    parts = event_name.split("_")
    if len(parts) < 2 or parts[1] == "":
        _raise("Quick Draft event name does not include a set code", raw_line=raw_line)

    return parts[1]


def _json_line_may_contain_events(line: str) -> bool:
    return any(
        token in line
        for token in (
            "authenticateResponse",
            "Course",
            "CurrentModule",
            "Payload",
            QUICK_DRAFT_PREFIX,
            "BotDraft",
            "DraftPack",
            "PickInfo",
            "DraftStatus",
        )
    )


def _payload_line_is_draft_shaped(*, data: dict[str, Any]) -> bool:
    module = data.get("CurrentModule")
    payload = data.get("Payload")
    if module == "BotDraft":
        return True

    if isinstance(payload, str):
        return any(
            token in payload
            for token in (QUICK_DRAFT_PREFIX, "DraftStatus", "DraftPack", "PickedCards")
        )

    return False


def _mapping_contains_draft_shape(data: dict[str, Any]) -> bool:
    if data.get("CurrentModule") == "BotDraft":
        return True

    return any(
        key in data
        for key in (
            "DraftStatus",
            "DraftPack",
            "PickedCards",
            "PickInfo",
            "BotDraft",
        )
    )


def _contains_unparsed_draft_shape(line: str) -> bool:
    if "BotDraft_Draft" in line:
        return True

    return any(
        token in line
        for token in (
            "\"BotDraftDraft",
            "\"DraftPack\"",
            "\"PickInfo\"",
            "\"DraftStatus\"",
        )
    )


def _raise(message: str, *, raw_line: str) -> NoReturn:
    raise DraftLogParseError(message, raw_line=raw_line)
