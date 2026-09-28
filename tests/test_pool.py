from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from draftomen.events import (
    AccountEvent,
    DraftCompletedEvent,
    DraftEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
    QuickDraftDetectedEvent,
    parse_events,
)
from draftomen.pool import (
    DraftPick,
    DraftPoolError,
    DraftPoolStore,
    DraftState,
    draft_state_path,
    list_account_profiles,
    list_draft_states,
    load_draft_state,
    save_draft_state,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "quick-draft-msh-player.log"
FIXTURE_ACCOUNT_ID = "FIXTURECLIENTID1234567890"
FIXTURE_DRAFT_ID = "00000000-0000-4000-8000-000000000004"
FIXTURE_EVENT_NAME = "QuickDraft_MSH_20260702"
FIXTURE_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


def test_fixture_replay_persists_complete_pool_under_account_directory(
    tmp_path: Path,
) -> None:
    events = _fixture_events()
    pick_events = [event for event in events if isinstance(event, PickMadeEvent)]
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)

    states = store.consume_all(events=events)

    assert len(states) == 1
    state = states[0]
    assert state.account_id == FIXTURE_ACCOUNT_ID
    assert state.account_screen_name == "FixturePlayer"
    assert state.draft_id == FIXTURE_DRAFT_ID
    assert state.event_name == FIXTURE_EVENT_NAME
    assert state.set_code == "MSH"
    assert state.course_id == FIXTURE_DRAFT_ID
    assert state.completed is True
    assert state.completed_at == FIXTURE_NOW.isoformat()
    assert state.chosen_pick_count == len(pick_events)
    assert len(state.pool_grp_ids) == len(pick_events)
    assert state.pool_grp_ids == tuple(
        event.selected_grp_ids[0] for event in pick_events
    )
    assert len(state.picks) == len(pick_events)
    assert all(pick.chosen_grp_id is not None for pick in state.picks)

    path = draft_state_path(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=tmp_path,
    )
    assert path == tmp_path / "state" / FIXTURE_ACCOUNT_ID / f"{FIXTURE_DRAFT_ID}.json"
    assert path.exists()
    assert sorted(path.parent.iterdir()) == [path]
    assert load_draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=tmp_path,
    ) == state

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["account_id"] == FIXTURE_ACCOUNT_ID
    assert payload["account_screen_name"] == "FixturePlayer"
    assert payload["draft_id"] == FIXTURE_DRAFT_ID
    assert len(payload["pool_grp_ids"]) == len(pick_events)


def test_replaying_same_events_over_existing_state_is_idempotent(tmp_path: Path) -> None:
    events = _fixture_events()
    first_store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    first_store.consume_all(events=events)
    before = load_draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=tmp_path,
    )

    second_store = DraftPoolStore(app_dir=tmp_path, clock=_later_clock)
    second_store.consume_all(events=events)

    after = load_draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=tmp_path,
    )
    assert after == before


def test_resuming_from_mid_draft_state_matches_single_continuous_run(
    tmp_path: Path,
) -> None:
    events = _fixture_events()
    split_index = _index_after_pick(events=events, pick_count=17)
    continuous_store = DraftPoolStore(app_dir=tmp_path / "continuous", clock=_fixed_clock)
    resume_store = DraftPoolStore(app_dir=tmp_path / "resume", clock=_fixed_clock)
    restart_store = DraftPoolStore(app_dir=tmp_path / "resume", clock=_fixed_clock)

    continuous_store.consume_all(events=events)
    resume_store.consume_all(events=events[:split_index])
    restart_store.consume_all(events=_without_account_ids(events=events[split_index:]))

    continuous = load_draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=tmp_path / "continuous",
    )
    resumed = load_draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=tmp_path / "resume",
    )
    assert resumed == continuous


def test_two_account_stream_persists_separated_states_without_pool_leakage(
    tmp_path: Path,
) -> None:
    events: list[DraftEvent] = [
        AccountEvent(client_id="ACCOUNT-A", screen_name="First"),
        DraftStartedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            course_id="draft-a",
            account_id=None,
        ),
        PackOfferedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(101, 102),
            pool_grp_ids=(),
            account_id=None,
        ),
        PickMadeEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            selected_grp_ids=(101,),
            account_id=None,
        ),
        AccountEvent(
            client_id="ACCOUNT-B",
            screen_name="Second",
            previous_client_id="ACCOUNT-A",
        ),
        DraftStartedEvent(
            event_name="QuickDraft_DEF_20260703",
            set_code="DEF",
            course_id="draft-b",
            account_id=None,
        ),
        PackOfferedEvent(
            event_name="QuickDraft_DEF_20260703",
            set_code="DEF",
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(201, 202),
            pool_grp_ids=(),
            account_id=None,
        ),
        PickMadeEvent(
            event_name="QuickDraft_DEF_20260703",
            set_code="DEF",
            pack_number=0,
            pick_number=0,
            selected_grp_ids=(201,),
            account_id=None,
        ),
        DraftCompletedEvent(
            event_name="QuickDraft_DEF_20260703",
            set_code="DEF",
            pack_number=0,
            pick_number=0,
            picked_grp_ids=(201,),
            inferred=False,
            account_id=None,
        ),
        AccountEvent(
            client_id="ACCOUNT-A",
            screen_name="First",
            previous_client_id="ACCOUNT-B",
        ),
        PackOfferedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=1,
            offered_grp_ids=(102, 103),
            pool_grp_ids=(101,),
            account_id=None,
        ),
        PickMadeEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=1,
            selected_grp_ids=(102,),
            account_id=None,
        ),
        DraftCompletedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=1,
            picked_grp_ids=(101, 102),
            inferred=False,
            account_id=None,
        ),
    ]
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)

    store.consume_all(events=events)

    first_state = load_draft_state(
        account_id="ACCOUNT-A",
        draft_id="draft-a",
        app_dir=tmp_path,
    )
    second_state = load_draft_state(
        account_id="ACCOUNT-B",
        draft_id="draft-b",
        app_dir=tmp_path,
    )
    assert first_state.account_screen_name == "First"
    assert second_state.account_screen_name == "Second"
    assert first_state.pool_grp_ids == (101, 102)
    assert second_state.pool_grp_ids == (201,)
    assert first_state.completed is True
    assert second_state.completed is True
    assert sorted(path.name for path in (tmp_path / "state").iterdir()) == [
        "ACCOUNT-A",
        "ACCOUNT-B",
    ]
    assert sorted(path.name for path in (tmp_path / "state" / "ACCOUNT-A").iterdir()) == [
        "draft-a.json",
    ]
    assert sorted(path.name for path in (tmp_path / "state" / "ACCOUNT-B").iterdir()) == [
        "draft-b.json",
    ]


def test_accountless_draft_start_recovers_account_from_persisted_course_id(
    tmp_path: Path,
) -> None:
    first_store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    first_store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))
    first_store.consume(
        event=DraftStartedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            course_id="draft-a",
            account_id=None,
        )
    )

    restart_store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    recovered = restart_store.consume(
        event=DraftStartedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            course_id="draft-a",
            account_id=None,
        )
    )

    assert recovered is not None
    assert recovered.account_id == "ACCOUNT-A"
    assert recovered.account_screen_name == "First"


def test_account_profile_labels_legacy_drafts_after_a_restart(tmp_path: Path) -> None:
    legacy_state = DraftState(
        account_id="ACCOUNT-A",
        draft_id="legacy-draft",
        event_name="QuickDraft_ABC_20260703",
        set_code="ABC",
        course_id="legacy-draft",
        started_at=FIXTURE_NOW.isoformat(),
        updated_at=FIXTURE_NOW.isoformat(),
        completed_at=None,
        completed=False,
        picks=(),
        pool_grp_ids=(),
        account_screen_name=None,
    )
    save_draft_state(state=legacy_state, app_dir=tmp_path)

    first_store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    first_store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))

    loaded = load_draft_state(
        account_id="ACCOUNT-A",
        draft_id="legacy-draft",
        app_dir=tmp_path,
    )
    listed = list_draft_states(app_dir=tmp_path)

    assert loaded.account_screen_name == "First"
    assert listed == (replace(legacy_state, account_screen_name="First"),)
    assert (tmp_path / "accounts" / "ACCOUNT-A.json").exists()


def test_list_account_profiles_includes_accounts_without_drafts(
    tmp_path: Path,
) -> None:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.set_active_account(account_id="ACCOUNT-B", screen_name="Second")
    store.set_active_account(account_id="ACCOUNT-A", screen_name="First")

    profiles = list_account_profiles(app_dir=tmp_path)

    assert tuple(
        (profile.account_id, profile.screen_name) for profile in profiles
    ) == (
        ("ACCOUNT-A", "First"),
        ("ACCOUNT-B", "Second"),
    )


def test_conflicting_first_pack_starts_new_synthetic_draft_state(
    tmp_path: Path,
) -> None:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))
    first_state = store.consume(
        event=PackOfferedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(101, 102),
            pool_grp_ids=(),
            account_id=None,
        )
    )
    assert first_state is not None
    store.consume(
        event=PickMadeEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            selected_grp_ids=(101,),
            account_id=None,
        )
    )

    second_state = store.consume(
        event=PackOfferedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(201, 202),
            pool_grp_ids=(),
            account_id=None,
        )
    )

    assert second_state is not None
    assert second_state.draft_id != first_state.draft_id
    assert second_state.draft_id.startswith("QuickDraft_ABC_20260703-")
    assert second_state.pick_for(pack_number=0, pick_number=0) == DraftPick(
        pack_number=0,
        pick_number=0,
        offered_grp_ids=(201, 202),
        pool_before_pick=(),
        chosen_grp_id=None,
    )
    assert sorted(path.name for path in (tmp_path / "state" / "ACCOUNT-A").iterdir()) == [
        "QuickDraft_ABC_20260703-2026-07-03T12_00_00+00_00.json",
        "QuickDraft_ABC_20260703.json",
    ]


def test_multi_card_pick_made_event_raises_and_leaves_state_unchanged(
    tmp_path: Path,
) -> None:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))
    store.consume(
        event=PackOfferedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(101, 102),
            pool_grp_ids=(),
            account_id=None,
        )
    )
    before = load_draft_state(
        account_id="ACCOUNT-A",
        draft_id="QuickDraft_ABC_20260703",
        app_dir=tmp_path,
    )

    with pytest.raises(DraftPoolError, match="multi-card picks are not supported"):
        store.consume(
            event=PickMadeEvent(
                event_name="QuickDraft_ABC_20260703",
                set_code="ABC",
                pack_number=0,
                pick_number=0,
                selected_grp_ids=(101, 102),
                account_id=None,
            )
        )

    after = load_draft_state(
        account_id="ACCOUNT-A",
        draft_id="QuickDraft_ABC_20260703",
        app_dir=tmp_path,
    )
    assert after == before


def _fixture_events() -> list[DraftEvent]:
    fixture_lines = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
    return list(parse_events(lines=fixture_lines))


def _without_account_ids(*, events: list[DraftEvent]) -> list[DraftEvent]:
    stripped_events: list[DraftEvent] = []
    for event in events:
        if isinstance(
            event,
            (
                QuickDraftDetectedEvent,
                DraftStartedEvent,
                PackOfferedEvent,
                PickMadeEvent,
                DraftCompletedEvent,
            ),
        ):
            stripped_events.append(replace(event, account_id=None))
        else:
            stripped_events.append(event)

    return stripped_events


def _fixed_clock() -> datetime:
    return FIXTURE_NOW


def _later_clock() -> datetime:
    return datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _index_after_pick(*, events: list[DraftEvent], pick_count: int) -> int:
    seen = 0
    for index, event in enumerate(events, start=1):
        if isinstance(event, PickMadeEvent):
            seen += 1
            if seen == pick_count:
                return index

    raise AssertionError(f"Fixture does not contain {pick_count} picks.")
