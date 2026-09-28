from __future__ import annotations

import json
from collections.abc import Iterable

import pytest

from draftomen.draft_format import DraftFormat
from draftomen.events import (
    DraftEvent,
    DraftLogParser,
    PackOfferedEvent,
    QuickDraftDetectedEvent,
    parse_events,
)

DRAFT_ID = "00000000-0000-4000-8000-000000000001"
OTHER_DRAFT_ID = "00000000-0000-4000-8000-000000000002"


def _join_request(*, event_name: str) -> str:
    request = json.dumps({"EventName": event_name, "EntryCurrencyType": "Gem"})
    body = json.dumps({"id": "request-1", "request": request})
    return f"[UnityCrossThreadLogger]==> EventJoin {body}"


def _join_response_lines(*, event_name: str, module: str = "PlayerDraft") -> list[str]:
    course = {
        "CourseId": "course-1",
        "InternalEventName": event_name,
        "CurrentModule": module,
        "ModulePayload": "",
    }
    return [
        "<== EventJoin(request-1)",
        json.dumps({"Course": course}),
    ]


def _notify(
    *,
    pack: int = 1,
    pick: int = 1,
    card_count: int = 14,
    first_grp_id: int = 1000,
    draft_id: str = DRAFT_ID,
) -> str:
    cards = ",".join(str(first_grp_id + index) for index in range(card_count))
    body = json.dumps(
        {"draftId": draft_id, "SelfPick": pick, "SelfPack": pack, "PackCards": cards}
    )
    return f"[UnityCrossThreadLogger]Draft.Notify {body}"


def _pack_events(*, lines: Iterable[str]) -> list[PackOfferedEvent]:
    return [event for event in parse_events(lines) if isinstance(event, PackOfferedEvent)]


@pytest.mark.parametrize(
    ("event_name", "draft_format", "picks_per_pack", "cards_per_pick"),
    [
        ("PremierDraft_TST_20260101", DraftFormat.PREMIER, 14, 1),
        ("TradDraft_TST_20260101", DraftFormat.TRADITIONAL, 14, 1),
        ("PickTwoDraft_TST_20260101", DraftFormat.PICK_TWO, 7, 2),
        ("PickTwoTradDraft_TST_20260101", DraftFormat.PICK_TWO, 7, 2),
    ],
)
def test_notify_produces_zero_based_pack_event_under_detected_rules(
    event_name: str,
    draft_format: DraftFormat,
    picks_per_pack: int,
    cards_per_pick: int,
) -> None:
    events = _pack_events(
        lines=[
            _join_request(event_name=event_name),
            _notify(pack=2, pick=3, card_count=12, first_grp_id=500),
        ]
    )

    assert events == [
        PackOfferedEvent(
            event_name=event_name,
            set_code="TST",
            pack_number=1,
            pick_number=2,
            offered_grp_ids=tuple(range(500, 512)),
            pool_grp_ids=(),
            account_id=None,
            picks_per_pack=picks_per_pack,
            draft_format=draft_format,
            cards_per_pick=cards_per_pick,
        )
    ]


def test_notify_pack_event_carries_the_active_account() -> None:
    account_line = json.dumps({"authenticateResponse": {"clientId": "ACCOUNT-1"}})
    events = _pack_events(
        lines=[
            account_line,
            _join_request(event_name="PremierDraft_TST_20260101"),
            _notify(),
        ]
    )

    assert [event.account_id for event in events] == ["ACCOUNT-1"]


def test_pick_two_accepts_every_logical_pick_and_rejects_a_fourteenth() -> None:
    lines = [_join_request(event_name="PickTwoDraft_TST_20260101")]
    lines.extend(_notify(pick=pick, card_count=16 - 2 * pick) for pick in range(1, 8))
    lines.append(_notify(pick=8, card_count=2))

    events = _pack_events(lines=lines)

    assert [event.pick_number for event in events] == list(range(7))


def test_repeated_notify_record_produces_one_event() -> None:
    notify = _notify(pack=1, pick=2, card_count=13)

    events = _pack_events(
        lines=[
            _join_request(event_name="PremierDraft_TST_20260101"),
            notify,
            notify,
        ]
    )

    assert len(events) == 1


def test_repeated_notify_across_separate_batches_produces_one_event() -> None:
    parser = DraftLogParser()
    notify = _notify(pack=1, pick=2, card_count=13)
    first = list(
        parser.parse_lines(
            lines=[_join_request(event_name="PremierDraft_TST_20260101"), notify]
        )
    )

    second = list(parser.parse_lines(lines=[notify]))

    assert len(first) == 1
    assert second == []


def test_notify_offered_again_after_another_pick_produces_a_second_event() -> None:
    first = _notify(pack=1, pick=2, card_count=13)
    other = _notify(pack=1, pick=3, card_count=12)

    events = _pack_events(
        lines=[
            _join_request(event_name="PremierDraft_TST_20260101"),
            first,
            other,
            first,
        ]
    )

    assert [event.pick_number for event in events] == [1, 2, 1]


def test_new_draft_with_same_first_pack_is_not_treated_as_a_repeat() -> None:
    events = _pack_events(
        lines=[
            _join_request(event_name="PremierDraft_TST_20260101"),
            _notify(draft_id=DRAFT_ID),
            _notify(draft_id=OTHER_DRAFT_ID),
        ]
    )

    assert len(events) == 2


def test_draft_whose_first_notify_is_pack_one_pick_two_starts_cleanly() -> None:
    events = _pack_events(
        lines=[
            _join_request(event_name="PremierDraft_TST_20260101"),
            _notify(pack=1, pick=2, card_count=13),
            _notify(pack=1, pick=3, card_count=12),
        ]
    )

    assert [(event.pack_number, event.pick_number) for event in events] == [(0, 1), (0, 2)]
    assert {event.set_code for event in events} == {"TST"}


def test_join_response_sets_the_event_when_the_request_was_not_logged() -> None:
    lines = _join_response_lines(event_name="PickTwoDraft_TST_20260101")
    lines.append(_notify(card_count=14))

    events = _pack_events(lines=lines)

    assert [event.draft_format for event in events] == [DraftFormat.PICK_TWO]
    assert events[0].event_name == "PickTwoDraft_TST_20260101"


def test_join_response_from_another_module_does_not_set_the_event() -> None:
    lines = _join_response_lines(
        event_name="PremierDraft_TST_20260101",
        module="DeckSelect",
    )
    lines.append(_notify())

    assert _pack_events(lines=lines) == []


def test_flat_course_snapshot_sets_the_event() -> None:
    snapshot = json.dumps(
        {
            "CourseId": "course-1",
            "InternalEventName": "TradDraft_TST_20260101",
            "CurrentModule": "PlayerDraft",
        }
    )

    events = _pack_events(lines=[snapshot, _notify()])

    assert [event.draft_format for event in events] == [DraftFormat.TRADITIONAL]


def test_notify_without_a_joined_event_produces_nothing() -> None:
    assert _pack_events(lines=[_notify()]) == []


@pytest.mark.parametrize(
    "event_name",
    [
        "Constructed_BestOf3",
        "PickTwoQuickDraft_TST_20260101",
        "Sealed_TST_20260101",
    ],
)
def test_unknown_event_prefix_leaves_the_active_draft_unchanged(event_name: str) -> None:
    events = _pack_events(
        lines=[
            _join_request(event_name="PremierDraft_TST_20260101"),
            _notify(pick=1),
            _join_request(event_name=event_name),
            *_join_response_lines(event_name=event_name),
            _notify(pick=2, card_count=13),
        ]
    )

    assert [event.event_name for event in events] == ["PremierDraft_TST_20260101"] * 2
    assert [event.draft_format for event in events] == [DraftFormat.PREMIER] * 2


def test_unknown_event_prefix_alone_never_starts_a_draft() -> None:
    events = _pack_events(
        lines=[_join_request(event_name="Constructed_BestOf3"), _notify()]
    )

    assert events == []


@pytest.mark.parametrize(
    "event_name",
    ["PremierDraft", "PremierDraft_", "PremierDraft__20260101"],
)
def test_human_event_name_without_a_set_code_is_ignored(event_name: str) -> None:
    events = _pack_events(lines=[_join_request(event_name=event_name), _notify()])

    assert events == []


def test_unreadable_join_request_for_a_human_draft_is_ignored() -> None:
    lines = [
        '[UnityCrossThreadLogger]==> EventJoin {"id":"1","request":"PremierDraft_TST {"}',
        '[UnityCrossThreadLogger]==> EventJoin {"id":"1","request":"[\\"PremierDraft_TST\\"]"}',
        _notify(),
    ]

    assert _pack_events(lines=lines) == []


@pytest.mark.parametrize(
    "malformed_line",
    [
        "[UnityCrossThreadLogger]Draft.Notify {not json}",
        "[UnityCrossThreadLogger]Draft.Notify {}",
        "[UnityCrossThreadLogger]Draft.Notify {\"draftId\":\"d\"}",
        _notify(pack=0),
        _notify(pack=4),
        _notify(pick=0),
        _notify(pick=15),
        _notify(draft_id=""),
    ],
)
def test_malformed_notify_leaves_the_active_draft_unchanged(malformed_line: str) -> None:
    valid = _notify(pack=1, pick=2, card_count=13)

    events = _pack_events(
        lines=[
            _join_request(event_name="PremierDraft_TST_20260101"),
            malformed_line,
            valid,
        ]
    )

    assert [(event.pack_number, event.pick_number) for event in events] == [(0, 1)]


@pytest.mark.parametrize(
    "payload",
    [
        {"draftId": DRAFT_ID, "SelfPick": "1", "SelfPack": 1, "PackCards": "1,2"},
        {"draftId": DRAFT_ID, "SelfPick": 1, "SelfPack": True, "PackCards": "1,2"},
        {"draftId": DRAFT_ID, "SelfPick": 1.5, "SelfPack": 1, "PackCards": "1,2"},
        {"draftId": DRAFT_ID, "SelfPick": 1, "SelfPack": 1, "PackCards": ""},
        {"draftId": DRAFT_ID, "SelfPick": 1, "SelfPack": 1, "PackCards": "1,,2"},
        {"draftId": DRAFT_ID, "SelfPick": 1, "SelfPack": 1, "PackCards": "1,x"},
        {"draftId": DRAFT_ID, "SelfPick": 1, "SelfPack": 1, "PackCards": "1,-2"},
        {"draftId": DRAFT_ID, "SelfPick": 1, "SelfPack": 1, "PackCards": [1, 2]},
        {"draftId": DRAFT_ID, "SelfPick": 1, "SelfPack": 1},
        {"draftId": 7, "SelfPick": 1, "SelfPack": 1, "PackCards": "1,2"},
    ],
)
def test_notify_with_invalid_field_is_dropped_without_raising(payload: object) -> None:
    line = f"[UnityCrossThreadLogger]Draft.Notify {json.dumps(payload)}"

    assert _pack_events(
        lines=[_join_request(event_name="PremierDraft_TST_20260101"), line]
    ) == []


def test_notify_line_without_a_json_object_is_not_a_draft_shaped_error() -> None:
    lines = [
        _join_request(event_name="PremierDraft_TST_20260101"),
        "[UnityCrossThreadLogger]Draft.Notify",
        "[UnityCrossThreadLogger]Draft.Notify []",
    ]

    assert _pack_events(lines=lines) == []


@pytest.mark.parametrize("prefix", ["[1234] ", "[212014]  ", "[Thread 7] "])
def test_thread_prefix_parses_the_same_as_the_unprefixed_line(prefix: str) -> None:
    lines = [
        _join_request(event_name="PremierDraft_TST_20260101"),
        *_join_response_lines(event_name="PremierDraft_TST_20260101"),
        _notify(pack=1, pick=2, card_count=13),
    ]
    prefixed = [f"{prefix}{line}" for line in lines]

    assert _pack_events(lines=prefixed) == _pack_events(lines=lines)
    assert len(_pack_events(lines=prefixed)) == 1


def test_thread_prefix_parses_quick_draft_join_the_same_as_the_unprefixed_line() -> None:
    line = _join_request(event_name="QuickDraft_TST_20260101")

    plain = list(parse_events([line]))
    prefixed = list(parse_events([f"[4675] {line}"]))

    assert plain == prefixed
    assert isinstance(plain[0], QuickDraftDetectedEvent)


def test_thread_prefix_leaves_login_lines_working() -> None:
    login = "[Accounts - Login] Logged in successfully. Display Name: Tester#12345"
    parser = DraftLogParser()

    list(parser.parse_lines(lines=[f"[99] {login}"]))

    assert parser.pending_login_screen_name == "Tester#12345"
    assert parser.login_generation == 1


def test_thread_prefix_does_not_strip_other_bracket_tags() -> None:
    events = list(parse_events(["[Accounts - Login] Logged in successfully."]))

    assert events == []


def test_login_boundary_forgets_the_joined_event() -> None:
    login = "[Accounts - Login] Logged in successfully. Display Name: Tester#12345"

    events = _pack_events(
        lines=[
            _join_request(event_name="PremierDraft_TST_20260101"),
            login,
            _notify(),
        ]
    )

    assert events == []


def test_quick_join_replaces_a_human_event() -> None:
    lines: list[str] = [
        _join_request(event_name="PremierDraft_TST_20260101"),
        _join_request(event_name="QuickDraft_TST_Draftmancer_20260101"),
        _notify(),
    ]

    events = _pack_events(lines=lines)

    assert [(event.draft_format, event.picks_per_pack) for event in events] == [
        (DraftFormat.QUICK, 14)
    ]


def test_payload_pack_events_keep_quick_defaults() -> None:
    payload = json.dumps(
        {
            "Result": "Success",
            "EventName": "QuickDraft_TST_Draftmancer_20260101",
            "DraftStatus": "PickNext",
            "PackNumber": 0,
            "PickNumber": 0,
            "DraftPack": ["1", "2", "3"],
            "PickedCards": [],
        }
    )
    line = json.dumps({"CurrentModule": "BotDraft", "Payload": payload})

    events: list[DraftEvent] = list(parse_events([line]))

    assert isinstance(events[0], PackOfferedEvent)
    assert events[0].draft_format is DraftFormat.QUICK
    assert events[0].cards_per_pick == 1
