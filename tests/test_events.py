from __future__ import annotations

import json
from pathlib import Path

import pytest

from draftomen.events import (
    AccountEvent,
    DraftCompletedEvent,
    DraftLogParseError,
    DraftLogParser,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
    QuickDraftDetectedEvent,
    parse_events,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "quick-draft-msh-player.log"
EXPECTED_PICK_COORDINATES = [
    (pack_number, pick_number)
    for pack_number in range(3)
    for pick_number in range(14)
]
EXPECTED_CHOSEN_GRP_IDS = [
    105097,
    105134,
    105003,
    105037,
    105117,
    105014,
    105034,
    104997,
    105030,
    104989,
    105054,
    105070,
    105054,
    105084,
    105037,
    104998,
    105005,
    104983,
    105017,
    105003,
    105047,
    104996,
    105006,
    105033,
    105013,
    105003,
    105049,
    105032,
    105000,
    104986,
    105164,
    105005,
    105117,
    105053,
    104989,
    105002,
    105031,
    104995,
    105004,
    104995,
    104911,
    105182,
]
EXPECTED_FIRST_PACK = (
    104894,
    104976,
    105080,
    104995,
    105027,
    105030,
    105170,
    104932,
    104893,
    105091,
    104969,
    105097,
    104979,
    105164,
)


def test_parse_fixture_yields_account_start_all_picks_and_completion() -> None:
    fixture_lines = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()

    events = list(parse_events(lines=fixture_lines))

    expected_sequence: list[type[object]] = [
        AccountEvent,
        QuickDraftDetectedEvent,
        DraftStartedEvent,
    ]
    for _ in range(42):
        expected_sequence.extend([PackOfferedEvent, PickMadeEvent])
    expected_sequence.append(DraftCompletedEvent)
    assert [type(event) for event in events] == expected_sequence

    account = events[0]
    assert isinstance(account, AccountEvent)
    assert account.client_id == "FIXTURECLIENTID1234567890"
    assert account.screen_name == "FixturePlayer"
    assert account.previous_client_id is None

    detected = events[1]
    assert isinstance(detected, QuickDraftDetectedEvent)
    assert detected.event_name == "QuickDraft_MSH_20260702"
    assert detected.set_code == "MSH"
    assert detected.account_id == account.client_id

    draft_started = events[2]
    assert isinstance(draft_started, DraftStartedEvent)
    assert draft_started.event_name == "QuickDraft_MSH_20260702"
    assert draft_started.set_code == "MSH"
    assert draft_started.course_id == "00000000-0000-4000-8000-000000000004"
    assert draft_started.account_id == account.client_id

    pack_events = [event for event in events if isinstance(event, PackOfferedEvent)]
    pick_events = [event for event in events if isinstance(event, PickMadeEvent)]
    assert len(pack_events) == 42
    assert len(pick_events) == 42
    assert [
        (event.pack_number, event.pick_number) for event in pack_events
    ] == EXPECTED_PICK_COORDINATES
    assert [
        (event.pack_number, event.pick_number) for event in pick_events
    ] == EXPECTED_PICK_COORDINATES
    assert [
        event.selected_grp_ids[0] for event in pick_events
    ] == EXPECTED_CHOSEN_GRP_IDS
    assert [len(event.offered_grp_ids) for event in pack_events] == list(
        range(14, 0, -1)
    ) * 3
    assert pack_events[0].offered_grp_ids == EXPECTED_FIRST_PACK
    assert pick_events[0].selected_grp_ids == (105097,)
    assert pack_events[-1].offered_grp_ids == (105182,)
    assert pick_events[-1].selected_grp_ids == (105182,)

    completion = events[-1]
    assert isinstance(completion, DraftCompletedEvent)
    assert completion.event_name == "QuickDraft_MSH_20260702"
    assert completion.set_code == "MSH"
    assert completion.pack_number == 2
    assert completion.pick_number == 13
    assert len(completion.picked_grp_ids) == 42
    assert completion.inferred is False
    assert completion.account_id == account.client_id


def test_parser_uses_login_display_name_when_auth_screen_name_is_missing() -> None:
    auth_line = json.dumps(
        {
            "authenticateResponse": {
                "clientId": "ACCOUNTID123",
            },
        }
    )

    events = list(
        parse_events(
            lines=[
                "[Accounts - Login] Logged in successfully. "
                "Display Name: ReadablePlayer#12345",
                auth_line,
            ]
        )
    )

    assert events == [
        AccountEvent(
            client_id="ACCOUNTID123",
            screen_name="ReadablePlayer#12345",
            previous_client_id=None,
        )
    ]


def test_parser_accepts_a_decorated_login_display_name_line() -> None:
    auth_line = json.dumps(
        {
            "authenticateResponse": {
                "clientId": "ACCOUNTID123",
            },
        }
    )

    events = list(
        parse_events(
            lines=[
                "[UnityCrossThreadLogger] [Accounts - Login] "
                "Logged in successfully. Display Name: ReadablePlayer#12345",
                auth_line,
            ]
        )
    )

    assert events == [
        AccountEvent(
            client_id="ACCOUNTID123",
            screen_name="ReadablePlayer#12345",
            previous_client_id=None,
        )
    ]


def test_parser_records_quick_draft_course_ids_from_course_snapshots() -> None:
    parser = DraftLogParser()
    snapshot_line = json.dumps(
        {
            "Courses": [
                {
                    "CourseId": "quick-draft-course",
                    "InternalEventName": "QuickDraft_MSH_20260702",
                    "CurrentModule": "CreateMatch",
                },
                {
                    "CourseId": "other-course",
                    "InternalEventName": "PremierDraft_MSH_20260702",
                    "CurrentModule": "CreateMatch",
                },
            ]
        }
    )

    assert list(parser.parse_lines(lines=[snapshot_line])) == []
    assert parser.observed_quick_draft_course_ids == frozenset({"quick-draft-course"})


def test_parser_clears_prior_account_context_at_a_new_login_boundary() -> None:
    parser = DraftLogParser()
    first_auth_line = json.dumps(
        {
            "authenticateResponse": {
                "clientId": "FIRSTACCOUNT",
                "screenName": "FirstPlayer",
            },
        }
    )
    previous_course_snapshot = json.dumps(
        {
            "Courses": [
                {
                    "CourseId": "previous-course",
                    "InternalEventName": "QuickDraft_MSH_20260702",
                }
            ]
        }
    )
    next_course_line = json.dumps(
        {
            "Course": {
                "CourseId": "next-course",
                "InternalEventName": "QuickDraft_MSH_20260702",
                "CurrentModule": "BotDraft",
            }
        }
    )

    assert list(
        parser.parse_lines(lines=[first_auth_line, previous_course_snapshot])
    ) == [
        AccountEvent(
            client_id="FIRSTACCOUNT",
            screen_name="FirstPlayer",
            previous_client_id=None,
        )
    ]
    events = list(
        parser.parse_lines(
            lines=[
                "[Accounts - Login] Logged in successfully. "
                "Display Name: SecondPlayer#12345",
                next_course_line,
            ]
        )
    )

    assert events == [
        DraftStartedEvent(
            event_name="QuickDraft_MSH_20260702",
            set_code="MSH",
            course_id="next-course",
            account_id=None,
        )
    ]
    assert parser.observed_quick_draft_course_ids == frozenset({"next-course"})


def test_parser_prefers_login_display_name_when_auth_screen_name_is_client_id() -> None:
    auth_line = json.dumps(
        {
            "authenticateResponse": {
                "clientId": "ACCOUNTID123",
                "screenName": "ACCOUNTID123",
            },
        }
    )

    events = list(
        parse_events(
            lines=[
                "[Accounts - Login] Logged in successfully. "
                "Display Name: ReadablePlayer#12345",
                auth_line,
            ]
        )
    )

    assert events == [
        AccountEvent(
            client_id="ACCOUNTID123",
            screen_name="ReadablePlayer#12345",
            previous_client_id=None,
        )
    ]


def test_parser_reuses_known_account_name_for_repeated_auth_without_screen_name() -> None:
    first_auth_line = json.dumps(
        {
            "authenticateResponse": {
                "clientId": "ACCOUNTID123",
                "screenName": "ReadablePlayer",
            },
        }
    )
    second_auth_line = json.dumps(
        {
            "authenticateResponse": {
                "clientId": "ACCOUNTID123",
            },
        }
    )

    events = list(parse_events(lines=[first_auth_line, second_auth_line]))

    assert events == [
        AccountEvent(
            client_id="ACCOUNTID123",
            screen_name="ReadablePlayer",
            previous_client_id=None,
        ),
        AccountEvent(
            client_id="ACCOUNTID123",
            screen_name="ReadablePlayer",
            previous_client_id=None,
        ),
    ]


def test_parser_emits_account_change_when_authenticate_response_changes() -> None:
    first_auth_line = json.dumps(
        {
            "authenticateResponse": {
                "clientId": "FIRSTACCOUNT",
                "screenName": "FirstPlayer",
            },
        }
    )
    second_auth_line = json.dumps(
        {
            "authenticateResponse": {
                "clientId": "SECONDACCOUNT",
                "screenName": "SecondPlayer",
            },
        }
    )

    events = list(parse_events(lines=[first_auth_line, second_auth_line]))

    assert events == [
        AccountEvent(
            client_id="FIRSTACCOUNT",
            screen_name="FirstPlayer",
            previous_client_id=None,
        ),
        AccountEvent(
            client_id="SECONDACCOUNT",
            screen_name="SecondPlayer",
            previous_client_id="FIRSTACCOUNT",
        ),
    ]


def test_parser_ignores_draft_stack_trace_lines() -> None:
    lines = [
        "Wotc.Mtga.Network.ServiceWrappers.AwsEventServiceWrapper:GetBotDraftStatus(String)",
        "Wotc.Mtga.Wrapper.Draft.<GetDraftStatus>d__60:MoveNext()",
    ]

    assert list(parse_events(lines=lines)) == []


def test_parser_infers_completion_from_final_empty_pack_without_completed_status() -> None:
    picked_cards = [str(grp_id) for grp_id in range(1, 43)]
    payload = {
        "Result": "Success",
        "EventName": "QuickDraft_ABC_20260702",
        "DraftStatus": "PickNext",
        "PackNumber": 2,
        "PickNumber": 13,
        "NumCardsToPick": 1,
        "DraftPack": [],
        "PickedCards": picked_cards,
    }
    line = json.dumps({"CurrentModule": "BotDraft", "Payload": json.dumps(payload)})

    events = list(parse_events(lines=[line]))

    assert events == [
        DraftCompletedEvent(
            event_name="QuickDraft_ABC_20260702",
            set_code="ABC",
            pack_number=2,
            pick_number=13,
            picked_grp_ids=tuple(range(1, 43)),
            inferred=True,
            account_id=None,
        )
    ]


def test_parser_ignores_quick_draft_course_snapshot_outside_botdraft() -> None:
    line = json.dumps(
        {
            "Course": {
                "CourseId": "00000000-0000-4000-8000-000000000078",
                "InternalEventName": "QuickDraft_ABC_20260702",
                "CurrentModule": "DeckSelect",
                "ModulePayload": "",
                "CourseDeckSummary": {"Attributes": []},
                "CardPool": [],
                "CardStyles": [],
            },
        }
    )

    assert list(parse_events(lines=[line])) == []


@pytest.mark.parametrize(
    "raw_line",
    [
        json.dumps({"CurrentModule": "BotDraft", "Payload": "not-json"}),
        json.dumps({"CurrentModule": "BotDraft"}),
        json.dumps(
            {
                "CurrentModule": "BotDraft",
                "Payload": json.dumps(
                    {
                        "Result": "Success",
                        "EventName": "QuickDraft_ABC_20260702",
                        "DraftStatus": "PatchDrift",
                        "PackNumber": 0,
                        "PickNumber": 0,
                        "DraftPack": ["1"],
                        "PickedCards": [],
                    }
                ),
            }
        ),
    ],
)
def test_malformed_or_unknown_draft_lines_raise_diagnostic_with_raw_line(
    raw_line: str,
) -> None:
    with pytest.raises(DraftLogParseError) as error:
        list(parse_events(lines=[raw_line]))

    assert error.value.raw_line == raw_line
    assert raw_line in str(error.value)


def test_pick_made_event_rejects_empty_selected_grp_ids() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        PickMadeEvent(
            event_name="QuickDraft_ABC_20260702",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            selected_grp_ids=(),
            account_id=None,
        )
