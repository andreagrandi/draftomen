from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from draftomen.draft_format import DraftFormat
from draftomen.pool import DraftPoolStore
from draftomen.events import (
    DraftCompletedEvent,
    DraftEvent,
    DraftLogParser,
    PackOfferedEvent,
    PickMadeEvent,
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


PREMIER = "PremierDraft_TST_20260101"
PICK_TWO = "PickTwoDraft_TST_20260101"
_TIMESTAMP = "[UnityCrossThreadLogger]1/1/2000 12:00:01 PM"


def _pick_request(
    *,
    request_id: str = "pick-1",
    pack: int = 1,
    pick: int = 1,
    cards: Sequence[int] = (5001,),
    draft_id: str = DRAFT_ID,
) -> str:
    request = json.dumps({"DraftId": draft_id, "GrpIds": list(cards), "Pack": pack, "Pick": pick})
    body = json.dumps({"id": request_id, "request": request})
    return f"[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {body}"


def _pick_response(*, request_id: str = "pick-1", **fields: object) -> list[str]:
    body = fields or {"IsPickSuccessful": True}
    return [_TIMESTAMP, f"<== EventPlayerDraftMakePick({request_id})", json.dumps(body)]


def _complete_request(*, event_name: str = PREMIER, is_bot_draft: bool = False) -> str:
    request = json.dumps({"EventName": event_name, "IsBotDraft": is_bot_draft})
    body = json.dumps({"id": "complete-1", "request": request})
    return f"[UnityCrossThreadLogger]==> DraftCompleteDraft {body}"


def _complete_response(*, event_name: str = PREMIER, card_pool: Sequence[int] = ()) -> list[str]:
    body = {
        "CourseId": "course-1",
        "InternalEventName": event_name,
        "CurrentModule": "DeckSelect",
        "ModulePayload": "{}",
        "CardPool": list(card_pool),
    }
    return [_TIMESTAMP, "<== DraftCompleteDraft(complete-1)", json.dumps(body)]


def _picks(*, events: Iterable[DraftEvent]) -> list[PickMadeEvent]:
    return [event for event in events if isinstance(event, PickMadeEvent)]


def _completions(*, events: Iterable[DraftEvent]) -> list[DraftCompletedEvent]:
    return [event for event in events if isinstance(event, DraftCompletedEvent)]


def _pick(*, pack: int, pick: int, cards: tuple[int, ...], event_name: str = PREMIER) -> PickMadeEvent:
    return PickMadeEvent(
        event_name=event_name,
        set_code="TST",
        pack_number=pack,
        pick_number=pick,
        selected_grp_ids=cards,
        account_id=None,
    )


def test_premier_pick_with_success_response_produces_one_zero_based_pick() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(pack=2, pick=3, cards=[5001]),
                *_pick_response(),
            ]
        )
    )

    assert events == [_pick(pack=1, pick=2, cards=(5001,))]


def test_pick_carries_the_active_account() -> None:
    account_line = json.dumps({"authenticateResponse": {"clientId": "ACCOUNT-1"}})

    events = _picks(
        events=parse_events(
            [
                account_line,
                _join_request(event_name=PREMIER),
                _pick_request(),
                *_pick_response(),
            ]
        )
    )

    assert [event.account_id for event in events] == ["ACCOUNT-1"]


def test_pick_two_request_produces_one_pick_holding_both_cards() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PICK_TWO),
                _pick_request(pack=1, pick=7, cards=[5001, 5002]),
                *_pick_response(),
            ]
        )
    )

    assert events == [_pick(pack=0, pick=6, cards=(5001, 5002), event_name=PICK_TWO)]


@pytest.mark.parametrize(
    ("event_name", "cards", "expected_count"),
    [(PREMIER, [5001, 5002], 1), (PICK_TWO, [5001], 2)],
)
def test_card_count_that_disagrees_with_the_format_keeps_the_cards_and_warns(
    event_name: str,
    cards: list[int],
    expected_count: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="draftomen.events"):
        events = _picks(
            events=parse_events(
                [
                    _join_request(event_name=event_name),
                    _pick_request(cards=cards),
                    *_pick_response(),
                ]
            )
        )

    assert events == [_pick(pack=0, pick=0, cards=tuple(cards), event_name=event_name)]
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert event_name in message
    assert f"has {len(cards)} cards, expected {expected_count}" in message


def test_pick_without_a_human_draft_context_produces_nothing() -> None:
    lines = [_pick_request(), *_pick_response()]

    assert list(parse_events(lines)) == []
    quick_events = list(
        parse_events([_join_request(event_name="QuickDraft_TST_20260101"), *lines])
    )
    assert [type(event) for event in quick_events] == [QuickDraftDetectedEvent]


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"draft_id": ""},
        {"pack": 0},
        {"pack": 4},
        {"pick": 0},
        {"pick": 15},
        {"cards": []},
        {"cards": [0]},
        {"cards": [-4]},
    ],
)
def test_pick_request_outside_the_rules_is_dropped(request_kwargs: dict[str, object]) -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(request_id="bad", **request_kwargs),  # type: ignore[arg-type]
        *_pick_response(request_id="bad"),
    ]

    assert list(parse_events(lines)) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"DraftId": DRAFT_ID, "GrpIds": [1], "Pack": "1", "Pick": 1},
        {"DraftId": DRAFT_ID, "GrpIds": [1], "Pack": True, "Pick": 1},
        {"DraftId": DRAFT_ID, "GrpIds": [1], "Pack": 1, "Pick": 1.5},
        {"DraftId": DRAFT_ID, "GrpIds": [True], "Pack": 1, "Pick": 1},
        {"DraftId": DRAFT_ID, "GrpIds": ["1"], "Pack": 1, "Pick": 1},
        {"DraftId": DRAFT_ID, "GrpIds": 1, "Pack": 1, "Pick": 1},
        {"DraftId": 7, "GrpIds": [1], "Pack": 1, "Pick": 1},
        {"GrpIds": [1], "Pack": 1, "Pick": 1},
    ],
)
def test_pick_request_with_a_wrongly_typed_field_is_dropped(payload: object) -> None:
    body = json.dumps({"id": "bad", "request": json.dumps(payload)})
    line = f"[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {body}"

    assert list(parse_events([_join_request(event_name=PREMIER), line, *_pick_response(request_id="bad")])) == []


def test_missing_submission_leaves_a_gap_that_completion_does_not_fill() -> None:
    lines = [
        _join_request(event_name=PICK_TWO),
        _pick_request(request_id="a", pick=3, cards=[1, 2]),
        *_pick_response(request_id="a"),
        _pick_request(request_id="b", pick=5, cards=[3, 4]),
        *_pick_response(request_id="b"),
        _complete_request(event_name=PICK_TWO),
        *_complete_response(event_name=PICK_TWO, card_pool=[1, 2, 9, 9, 3, 4]),
    ]

    events = list(parse_events(lines))

    assert [(event.pack_number, event.pick_number) for event in _picks(events=events)] == [
        (0, 2),
        (0, 4),
    ]
    completed = _completions(events=events)
    assert len(completed) == 1
    assert completed[0].picked_grp_ids == (1, 2, 3, 4)


def test_completion_from_the_final_pick_response() -> None:
    lines = [
        _join_request(event_name=PICK_TWO),
        _pick_request(request_id="a", pack=3, pick=7, cards=[7, 8]),
        *_pick_response(request_id="a", IsPickingCompleted=True, IsPickSuccessful=True),
    ]

    events = list(parse_events(lines))

    assert events == [
        _pick(pack=2, pick=6, cards=(7, 8), event_name=PICK_TWO),
        DraftCompletedEvent(
            event_name=PICK_TWO,
            set_code="TST",
            pack_number=2,
            pick_number=6,
            picked_grp_ids=(7, 8),
            inferred=False,
            account_id=None,
        ),
    ]


def test_completion_from_the_complete_draft_request_alone() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(cards=[11]),
                *_pick_response(),
                _pick_request(request_id="b", pick=2, cards=[12]),
                _complete_request(),
            ]
        )
    )

    assert [type(event) for event in events] == [PickMadeEvent, PickMadeEvent, DraftCompletedEvent]
    assert events[2].picked_grp_ids == (11, 12)  # type: ignore[union-attr]
    assert (events[2].pack_number, events[2].pick_number) == (2, 13)  # type: ignore[union-attr]


def test_completion_from_the_complete_draft_response_alone() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(cards=[11]),
                *_pick_response(),
                *_complete_response(card_pool=[11]),
            ]
        )
    )

    assert [type(event) for event in events] == [PickMadeEvent, DraftCompletedEvent]


def test_all_three_completion_signals_emit_one_completion() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(pack=3, pick=14, cards=[11]),
                *_pick_response(IsPickingCompleted=True, IsPickSuccessful=True),
                _complete_request(),
                *_complete_response(card_pool=[11]),
            ]
        )
    )

    assert len(_completions(events=events)) == 1
    assert len(_picks(events=events)) == 1


def test_complete_draft_request_for_a_bot_draft_or_other_event_does_not_complete() -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(cards=[11]),
        *_pick_response(),
        _complete_request(is_bot_draft=True),
        _complete_request(event_name="PremierDraft_OTH_20260101"),
        *_complete_response(event_name="PremierDraft_OTH_20260101", card_pool=[11]),
    ]

    assert _completions(events=parse_events(lines)) == []


def test_completion_with_no_recorded_pick_uses_the_card_pool() -> None:
    events = _completions(
        events=parse_events(
            [_join_request(event_name=PREMIER), *_complete_response(card_pool=[3, 1, 2])]
        )
    )

    assert [event.picked_grp_ids for event in events] == [(3, 1, 2)]


def test_completion_with_no_pick_and_no_card_pool_emits_nothing() -> None:
    lines = [_join_request(event_name=PREMIER), _complete_request(), *_complete_response()]

    assert list(parse_events(lines)) == []


def test_card_pool_mismatch_warns_and_keeps_the_recorded_cards(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(cards=[11]),
        *_pick_response(),
        *_complete_response(card_pool=[11, 12]),
    ]

    with caplog.at_level(logging.WARNING, logger="draftomen.events"):
        events = list(parse_events(lines))

    assert _completions(events=events)[0].picked_grp_ids == (11,)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "2 cards but 1 were recorded from 1 of 42 picks" in message


def test_matching_card_pool_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(cards=[11]),
        *_pick_response(),
        _pick_request(request_id="b", pick=2, cards=[12]),
        *_pick_response(request_id="b"),
        *_complete_response(card_pool=[12, 11]),
    ]

    with caplog.at_level(logging.WARNING, logger="draftomen.events"):
        list(parse_events(lines))

    assert caplog.records == []


def test_late_card_pool_still_reconciles_after_an_earlier_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(cards=[11]),
        *_pick_response(IsPickingCompleted=True, IsPickSuccessful=True),
        *_complete_response(card_pool=[99]),
    ]

    with caplog.at_level(logging.WARNING, logger="draftomen.events"):
        events = list(parse_events(lines))

    assert len(_completions(events=events)) == 1
    assert len(caplog.records) == 1


def test_rejected_pick_produces_nothing_and_the_retry_is_recorded() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(request_id="first", cards=[11]),
                *_pick_response(request_id="first", IsPickSuccessful=False),
                _pick_request(request_id="retry", cards=[12]),
                *_pick_response(request_id="retry"),
            ]
        )
    )

    assert events == [_pick(pack=0, pick=0, cards=(12,))]


def test_rejected_pick_with_no_retry_is_never_emitted() -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(request_id="first", cards=[11]),
        *_pick_response(request_id="first", IsPickSuccessful=False),
        _pick_request(request_id="next", pick=2, cards=[12]),
        *_pick_response(request_id="next"),
    ]

    assert _picks(events=parse_events(lines)) == [_pick(pack=0, pick=1, cards=(12,))]


def test_rejection_of_an_already_recorded_pick_warns(caplog: pytest.LogCaptureFixture) -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(request_id="first", cards=[11]),
        _pick_request(request_id="next", pick=2, cards=[12]),
        *_pick_response(request_id="first", IsPickSuccessful=False),
    ]

    with caplog.at_level(logging.WARNING, logger="draftomen.events"):
        events = _picks(events=parse_events(lines))

    assert len(events) == 2
    assert len(caplog.records) == 1
    assert "pack 0 pick 0" in caplog.records[0].getMessage()


def test_repeated_request_and_repeated_response_count_once() -> None:
    request = _pick_request(cards=[11])
    lines = [
        _join_request(event_name=PREMIER),
        request,
        request,
        *_pick_response(),
        *_pick_response(),
        request,
        _pick_request(request_id="other-id", cards=[11]),
    ]

    assert _picks(events=parse_events(lines)) == [_pick(pack=0, pick=0, cards=(11,))]


def test_request_without_a_response_is_emitted_at_the_next_request() -> None:
    parser = DraftLogParser()

    first = list(
        parser.parse_lines(
            lines=[_join_request(event_name=PREMIER), _pick_request(request_id="a", cards=[11])]
        )
    )
    second = list(
        parser.parse_lines(lines=[_pick_request(request_id="b", pick=2, cards=[12])])
    )

    assert first == []
    assert second == [_pick(pack=0, pick=0, cards=(11,))]


def test_request_without_a_response_is_emitted_by_the_parse_events_flush() -> None:
    events = list(parse_events([_join_request(event_name=PREMIER), _pick_request(cards=[11])]))

    assert events == [_pick(pack=0, pick=0, cards=(11,))]


def test_flush_returns_the_pending_pick_once() -> None:
    parser = DraftLogParser()
    list(parser.parse_lines(lines=[_join_request(event_name=PREMIER), _pick_request()]))

    assert parser.flush() == (_pick(pack=0, pick=0, cards=(5001,)),)
    assert parser.flush() == ()


def test_pending_pick_is_emitted_before_the_next_pack_event() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(cards=[11]),
                _notify(pick=2, card_count=13),
            ]
        )
    )

    assert [type(event) for event in events] == [PickMadeEvent, PackOfferedEvent]


def test_pending_pick_is_emitted_before_a_login_boundary_clears_state() -> None:
    login = "[Accounts - Login] Logged in successfully. Display Name: Tester#12345"

    events = list(
        parse_events(
            [
                json.dumps({"authenticateResponse": {"clientId": "ACCOUNT-1"}}),
                _join_request(event_name=PREMIER),
                _pick_request(cards=[11]),
                login,
                _pick_request(request_id="later", pick=2, cards=[12]),
            ]
        )
    )

    assert [(type(event), event.account_id) for event in events[1:]] == [
        (PickMadeEvent, "ACCOUNT-1")
    ]
    assert len(events) == 2


def test_pending_pick_is_emitted_under_the_old_event_when_the_context_changes() -> None:
    events = _picks(
        events=parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(cards=[11]),
                _join_request(event_name=PICK_TWO),
                _pick_request(request_id="b", cards=[21, 22]),
            ]
        )
    )

    assert [(event.event_name, event.selected_grp_ids) for event in events] == [
        (PREMIER, (11,)),
        (PICK_TWO, (21, 22)),
    ]


def test_request_and_response_in_separate_batches_produce_one_pick() -> None:
    parser = DraftLogParser()
    first = list(
        parser.parse_lines(lines=[_join_request(event_name=PREMIER), _pick_request(cards=[11])])
    )
    second = list(parser.parse_lines(lines=_pick_response()))
    third = list(parser.parse_lines(lines=[_pick_request(request_id="b", pick=2, cards=[12])]))

    assert first == []
    assert second == [_pick(pack=0, pick=0, cards=(11,))]
    assert third == []


def test_response_body_split_across_batches_is_still_read() -> None:
    parser = DraftLogParser()
    list(parser.parse_lines(lines=[_join_request(event_name=PREMIER), _pick_request()]))
    list(parser.parse_lines(lines=["<== EventPlayerDraftMakePick(pick-1)"]))

    events = list(parser.parse_lines(lines=["", json.dumps({"IsPickSuccessful": False})]))

    assert events == []
    assert parser.flush() == ()


def test_new_draft_id_starts_a_new_record() -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(request_id="a", cards=[11]),
        *_pick_response(request_id="a", IsPickingCompleted=True, IsPickSuccessful=True),
        _pick_request(request_id="b", cards=[21], draft_id=OTHER_DRAFT_ID),
        *_pick_response(request_id="b", IsPickingCompleted=True, IsPickSuccessful=True),
    ]

    events = list(parse_events(lines))

    assert [c.picked_grp_ids for c in _completions(events=events)] == [(11,), (21,)]


def test_pick_for_a_completed_draft_is_ignored() -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(request_id="a", cards=[11]),
        *_pick_response(request_id="a", IsPickingCompleted=True, IsPickSuccessful=True),
        _pick_request(request_id="b", pick=2, cards=[12]),
        *_pick_response(request_id="b"),
    ]

    assert len(_picks(events=parse_events(lines))) == 1


@pytest.mark.parametrize(
    "bad_line",
    [
        "[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {not json}",
        '[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {"id":"x"}',
        '[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {"id":"x","request":"[1]"}',
        '[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {"id":"x","request":"{"}',
        '[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {"id":"x","request":5}',
        '[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {"request":"{}"}',
        '[UnityCrossThreadLogger]==> EventPlayerDraftMakePick [1]',
        '[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {"id":"x","request":"{\\"Draf',
        "[UnityCrossThreadLogger]==> DraftCompleteDraft {not json}",
        '[UnityCrossThreadLogger]==> DraftCompleteDraft {"id":"x","request":"[]"}',
        '[UnityCrossThreadLogger]==> EventPlayerDraftSomethingNew {"id":"x","request":"{}"}',
        "<== EventPlayerDraftSomethingNew(x)",
        "<== EventPlayerDraftMakePick(",
        '{"IsPickSuccessful": true',
        "[]",
    ],
)
def test_malformed_human_draft_lines_do_not_raise_or_change_the_draft(bad_line: str) -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(request_id="a", cards=[11]),
        bad_line,
        '{"IsPickSuccessful": true',
        _pick_request(request_id="b", pick=2, cards=[12]),
        *_pick_response(request_id="b"),
    ]

    events = list(parse_events(lines))

    assert events == [
        _pick(pack=0, pick=0, cards=(11,)),
        _pick(pack=0, pick=1, cards=(12,)),
    ]


@pytest.mark.parametrize(
    "body",
    ["not json", "[1, 2]", "{}", '{"CardPool": ', '{"Other": 1}'],
)
def test_unusable_response_bodies_are_ignored(body: str) -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(cards=[11]),
        "<== EventPlayerDraftMakePick(pick-1)",
        body,
        "<== DraftCompleteDraft(complete-1)",
        body,
    ]

    events = list(parse_events(lines))

    assert events == [_pick(pack=0, pick=0, cards=(11,))]


def test_thread_prefixed_pick_lines_parse_like_unprefixed_ones() -> None:
    lines = [
        _join_request(event_name=PREMIER),
        _pick_request(cards=[11]),
        *_pick_response(IsPickingCompleted=True, IsPickSuccessful=True),
        _complete_request(),
        *_complete_response(card_pool=[11]),
    ]
    prefixed = [f"[4675] {line}" for line in lines]

    assert list(parse_events(prefixed)) == list(parse_events(lines))
    assert len(list(parse_events(prefixed))) == 2


def test_quick_draft_pick_lines_keep_their_existing_parsing() -> None:
    request = json.dumps(
        {
            "PickInfo": {
                "EventName": "QuickDraft_TST_20260101",
                "CardIds": ["7"],
                "PackNumber": 0,
                "PickNumber": 1,
            }
        }
    )
    line = "[UnityCrossThreadLogger]==> BotDraftDraftPick " + json.dumps(
        {"id": "quick-1", "request": request}
    )

    events = list(parse_events([line]))

    assert events == [
        PickMadeEvent(
            event_name="QuickDraft_TST_20260101",
            set_code="TST",
            pack_number=0,
            pick_number=1,
            selected_grp_ids=(7,),
            account_id=None,
        )
    ]


def _pack_at(*, events: Iterable[DraftEvent], pack: int, pick: int) -> PackOfferedEvent:
    matches = [
        event
        for event in events
        if isinstance(event, PackOfferedEvent)
        and (event.pack_number, event.pick_number) == (pack, pick)
    ]
    assert len(matches) == 1
    return matches[0]


def test_first_pack_of_a_draft_carries_an_empty_pool() -> None:
    events = list(parse_events([_join_request(event_name=PREMIER), _notify()]))

    assert _pack_at(events=events, pack=0, pick=0).pool_grp_ids == ()


def test_pack_after_a_recorded_pick_carries_that_pick_as_its_pool() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PICK_TWO),
                _notify(card_count=14),
                _pick_request(cards=[7, 8]),
                *_pick_response(),
                _notify(pick=2, card_count=12),
            ]
        )
    )

    assert [type(event) for event in events] == [PackOfferedEvent, PickMadeEvent, PackOfferedEvent]
    assert _pack_at(events=events, pack=0, pick=1).pool_grp_ids == (7, 8)


def test_pool_lists_recorded_cards_in_coordinate_order() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(request_id="b", pick=2, cards=[12]),
                *_pick_response(request_id="b"),
                _pick_request(request_id="a", pick=1, cards=[11]),
                *_pick_response(request_id="a"),
                _notify(pick=3, card_count=12),
            ]
        )
    )

    assert _pack_at(events=events, pack=0, pick=2).pool_grp_ids == (11, 12)


def test_pack_from_a_different_draft_carries_an_empty_pool() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(cards=[11]),
                *_pick_response(),
                _notify(draft_id=OTHER_DRAFT_ID),
            ]
        )
    )

    assert _pack_at(events=events, pack=0, pick=0).pool_grp_ids == ()


def test_pack_arriving_while_a_pick_is_pending_follows_that_pick() -> None:
    events = list(
        parse_events(
            [
                _join_request(event_name=PREMIER),
                _pick_request(cards=[11]),
                _notify(pick=2, card_count=13),
            ]
        )
    )

    assert events[0] == _pick(pack=0, pick=0, cards=(11,))
    assert isinstance(events[1], PackOfferedEvent)
    assert events[1].pool_grp_ids == (11,)


def test_repeated_notify_does_not_flush_or_reorder_a_pending_pick() -> None:
    notify = _notify(pick=2, card_count=13)
    parser = DraftLogParser()
    first = list(
        parser.parse_lines(
            lines=[_join_request(event_name=PREMIER), _notify(), _pick_request(cards=[11]), _notify()]
        )
    )
    second = list(parser.parse_lines(lines=[*_pick_response(), notify, notify]))

    assert [type(event) for event in first] == [PackOfferedEvent]
    assert [type(event) for event in second] == [PickMadeEvent, PackOfferedEvent]


def _pick_two_log() -> tuple[list[str], list[int]]:
    lines = [_join_request(event_name=PICK_TWO)]
    recorded: list[int] = []
    for pack in range(1, 4):
        for pick in range(1, 8):
            first_card = pack * 1000 + pick * 10
            lines.append(_notify(pack=pack, pick=pick, card_count=16 - 2 * pick, first_grp_id=first_card))
            if (pack, pick) == (1, 4):
                continue

            cards = [first_card, first_card + 1]
            recorded.extend(cards)
            request_id = f"pick-{pack}-{pick}"
            lines.append(_pick_request(request_id=request_id, pack=pack, pick=pick, cards=cards))
            lines.extend(_pick_response(request_id=request_id))

    card_pool = [*recorded, 9001, 9002]
    lines.append(_complete_request(event_name=PICK_TWO))
    lines.extend(_complete_response(event_name=PICK_TWO, card_pool=card_pool))
    return lines, recorded


def test_pick_two_log_with_a_missing_submission_feeds_the_pool_store(tmp_path: Path) -> None:
    lines, recorded = _pick_two_log()
    store = DraftPoolStore(app_dir=tmp_path)
    store.set_active_account(account_id="acct")

    states = store.consume_all(events=parse_events(lines))

    assert len(states) == 1
    state = states[0]
    missing = state.pick_for(pack_number=0, pick_number=3)
    assert missing is not None
    assert missing.selected_grp_ids == ()
    assert state.completed
    assert state.chosen_pick_count == 20
    assert sorted(state.pool_grp_ids) == sorted(recorded)
    assert len(state.pool_grp_ids) == 40


def test_premier_log_feeds_the_pool_store(tmp_path: Path) -> None:
    lines = [_join_request(event_name=PREMIER)]
    for pick in range(1, 4):
        lines.append(_notify(pick=pick, card_count=15 - pick, first_grp_id=100 * pick))
        lines.append(_pick_request(request_id=f"p{pick}", pick=pick, cards=[100 * pick]))
        lines.extend(_pick_response(request_id=f"p{pick}"))

    lines.append(_complete_request())
    lines.extend(_complete_response(card_pool=[100, 200, 300]))
    store = DraftPoolStore(app_dir=tmp_path)
    store.set_active_account(account_id="acct")

    states = store.consume_all(events=parse_events(lines))

    assert len(states) == 1
    assert states[0].completed
    assert states[0].pool_grp_ids == (100, 200, 300)
    assert states[0].chosen_pick_count == 3
