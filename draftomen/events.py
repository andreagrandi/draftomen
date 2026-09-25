"""Parse Quick Draft log lines into typed events.
Keep Arena log knowledge isolated in a pure line-consumer layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn, TypeAlias

QUICK_DRAFT_PREFIX = "QuickDraft_"
EXPECTED_PACK_COUNT = 3
EXPECTED_PICKS_PER_PACK = 14
EXPECTED_TOTAL_PICKS = EXPECTED_PACK_COUNT * EXPECTED_PICKS_PER_PACK
FINAL_PACK_NUMBER = EXPECTED_PACK_COUNT - 1
FINAL_PICK_NUMBER = EXPECTED_PICKS_PER_PACK - 1

_REQUEST_LINE = re.compile(r"^\[UnityCrossThreadLogger\]==>\s+(?P<token>\S+)\s+(?P<body>\{.*\})$")
_RESPONSE_MARKER = re.compile(r"^<==\s+(?P<token>[^()]+)\(")
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
    """Pack contents offered for a Quick Draft pick.
    Card identifiers are normalized Arena grpIds.
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


@dataclass(frozen=True, slots=True)
class PickMadeEvent:
    """Chosen card for a Quick Draft pick.
    Quick Draft picks one card, exposed as an Arena grpId.
    """

    event_name: str
    set_code: str
    pack_number: int
    pick_number: int
    chosen_grp_id: int
    account_id: str | None


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


@dataclass(slots=True)
class _ParserState:
    account_id: str | None = None
    pending_login_screen_name: str | None = None
    screen_names_by_client_id: dict[str, str] = field(default_factory=dict)
    observed_quick_draft_course_ids: set[str] = field(default_factory=set)
    login_generation: int = 0


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


def _parse_line(line: str, state: _ParserState) -> tuple[DraftEvent, ...]:
    stripped = line.strip()
    if not stripped:
        return ()

    if "BotDraft_Draft" in stripped:
        _raise(
            "Unsupported Quick Draft token; expected current BotDraftDraft* format",
            raw_line=line,
        )

    login_match = _LOGIN_DISPLAY_NAME.search(stripped)
    if login_match is not None:
        state.account_id = None
        state.pending_login_screen_name = login_match.group("screen_name").strip()
        state.screen_names_by_client_id.clear()
        state.observed_quick_draft_course_ids.clear()
        state.login_generation += 1
        return ()

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
                chosen_grp_id=card_ids[0],
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
        return ()

    request = _request_payload(body=body, raw_line=raw_line)
    event_name = _required_str(
        request.get("EventName"),
        field_name="request.EventName",
        raw_line=raw_line,
    )
    set_code = _set_code(event_name=event_name, raw_line=raw_line)
    return (
        QuickDraftDetectedEvent(
            event_name=event_name,
            set_code=set_code,
            account_id=state.account_id,
        ),
    )


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
