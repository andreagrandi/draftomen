from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from draftomen.audit import DraftAuditStore, load_draft_audit_records
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
    LEGACY_CHOSEN_CARD_KEY,
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
    assert all(pick.selected_grp_ids for pick in state.picks)
    assert state.selected_card_count == len(pick_events)

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
        selected_grp_ids=(),
    )
    assert sorted(path.name for path in (tmp_path / "state" / "ACCOUNT-A").iterdir()) == [
        "QuickDraft_ABC_20260703-20260703T120000Z.json",
        "QuickDraft_ABC_20260703.json",
    ]


def test_pack_conflict_draft_id_has_no_characters_windows_rejects(
    tmp_path: Path,
) -> None:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))
    _pick_first_card(store=store, offered_grp_ids=(101, 102))

    state = _offer_first_pack(store=store, offered_grp_ids=(201, 202))

    assert state.draft_id == "QuickDraft_ABC_20260703-20260703T120000Z"
    assert not set('<>:"|?*').intersection(state.draft_id)


def test_pack_conflicts_within_one_second_get_distinct_draft_ids(
    tmp_path: Path,
) -> None:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))
    _pick_first_card(store=store, offered_grp_ids=(101, 102))
    second = _pick_first_card(store=store, offered_grp_ids=(201, 202))

    third = _offer_first_pack(store=store, offered_grp_ids=(301, 302))

    assert second.draft_id == "QuickDraft_ABC_20260703-20260703T120000Z"
    assert third.draft_id == "QuickDraft_ABC_20260703-20260703T120000Z-2"
    assert sorted(path.name for path in (tmp_path / "state" / "ACCOUNT-A").iterdir()) == [
        "QuickDraft_ABC_20260703-20260703T120000Z-2.json",
        "QuickDraft_ABC_20260703-20260703T120000Z.json",
        "QuickDraft_ABC_20260703.json",
    ]


@pytest.mark.parametrize("character", list('<>:"|?*'))
def test_new_draft_with_non_portable_draft_id_raises_and_writes_nothing(
    tmp_path: Path,
    character: str,
) -> None:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)

    with pytest.raises(DraftPoolError, match="draft_id"):
        store.consume(
            event=DraftStartedEvent(
                event_name="QuickDraft_ABC_20260703",
                set_code="ABC",
                course_id=f"course{character}id",
                account_id="ACCOUNT-A",
            )
        )

    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("character", list('<>:"|?*'))
def test_new_draft_with_non_portable_account_id_raises_and_writes_nothing(
    tmp_path: Path,
    character: str,
) -> None:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)

    with pytest.raises(DraftPoolError, match="account_id"):
        store.consume(
            event=PackOfferedEvent(
                event_name="QuickDraft_ABC_20260703",
                set_code="ABC",
                pack_number=0,
                pick_number=0,
                offered_grp_ids=(101, 102),
                pool_grp_ids=(),
                account_id=f"ACCOUNT{character}A",
            )
        )

    assert not (tmp_path / "state").exists()


def test_legacy_draft_id_with_colons_keeps_loading_and_recording(
    tmp_path: Path,
) -> None:
    legacy_id = "QuickDraft_ABC_20260703-2026-07-03T12:00:00+00:00"
    legacy_state = DraftState(
        account_id="ACCOUNT-A",
        draft_id=legacy_id,
        event_name="QuickDraft_ABC_20260703",
        set_code="ABC",
        course_id=None,
        started_at=FIXTURE_NOW.isoformat(),
        updated_at=FIXTURE_NOW.isoformat(),
        completed_at=None,
        completed=False,
        picks=(),
        pool_grp_ids=(),
        account_screen_name=None,
    )
    save_draft_state(state=legacy_state, app_dir=tmp_path)
    audit = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)
    audit.record_draft_started(state=legacy_state)

    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))
    picked = _pick_first_card(store=store, offered_grp_ids=(101, 102))
    audit.record_choice(
        state=picked,
        event=PickMadeEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            selected_grp_ids=(101,),
            account_id=None,
        ),
        ranking_mode="score",
    )

    loaded = load_draft_state(account_id="ACCOUNT-A", draft_id=legacy_id, app_dir=tmp_path)
    records = load_draft_audit_records(
        account_id="ACCOUNT-A",
        draft_id=legacy_id,
        app_dir=tmp_path,
    )
    assert picked.draft_id == legacy_id
    assert loaded.pool_grp_ids == (101,)
    assert [record["record_type"] for record in records] == [
        "draft_started",
        "choice_made",
    ]


def _offer_first_pack(
    *,
    store: DraftPoolStore,
    offered_grp_ids: tuple[int, ...],
) -> DraftState:
    state = store.consume(
        event=PackOfferedEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            offered_grp_ids=offered_grp_ids,
            pool_grp_ids=(),
            account_id=None,
        )
    )
    assert state is not None
    return state


def _pick_first_card(
    *,
    store: DraftPoolStore,
    offered_grp_ids: tuple[int, ...],
) -> DraftState:
    _offer_first_pack(store=store, offered_grp_ids=offered_grp_ids)
    state = store.consume(
        event=PickMadeEvent(
            event_name="QuickDraft_ABC_20260703",
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            selected_grp_ids=(offered_grp_ids[0],),
            account_id=None,
        )
    )
    assert state is not None
    return state


_REPEAT_EVENT = "QuickDraft_ABC_20260703"


def _saved_draft(
    *,
    tmp_path: Path,
    draft_id: str,
    offered_grp_ids: tuple[int, ...],
    updated_at: str,
    completed: bool = True,
    account_id: str = "ACCOUNT-A",
) -> DraftState:
    state = DraftState(
        account_id=account_id,
        draft_id=draft_id,
        event_name=_REPEAT_EVENT,
        set_code="ABC",
        course_id=draft_id,
        started_at=updated_at,
        updated_at=updated_at,
        completed_at=updated_at if completed else None,
        completed=completed,
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=0,
                offered_grp_ids=offered_grp_ids,
                pool_before_pick=(),
                selected_grp_ids=offered_grp_ids[:1],
            ),
        ),
        pool_grp_ids=offered_grp_ids[:1],
        account_screen_name=None,
    )
    save_draft_state(state=state, app_dir=tmp_path)
    return state


def _repeat_pack_offered(*, offered_grp_ids: tuple[int, ...]) -> PackOfferedEvent:
    return PackOfferedEvent(
        event_name=_REPEAT_EVENT,
        set_code="ABC",
        pack_number=0,
        pick_number=0,
        offered_grp_ids=offered_grp_ids,
        pool_grp_ids=(),
        account_id=None,
    )


def _state_files(*, tmp_path: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in (tmp_path / "state" / "ACCOUNT-A").iterdir()
    }


def test_replayed_first_pack_of_older_completed_draft_resumes_that_draft(
    tmp_path: Path,
) -> None:
    older = _saved_draft(
        tmp_path=tmp_path,
        draft_id="older",
        offered_grp_ids=(101, 102),
        updated_at="2026-07-01T12:00:00+00:00",
    )
    newer = _saved_draft(
        tmp_path=tmp_path,
        draft_id="newer",
        offered_grp_ids=(201, 202),
        updated_at="2026-07-02T12:00:00+00:00",
    )
    before = _state_files(tmp_path=tmp_path)
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name=None))

    state = store.consume(event=_repeat_pack_offered(offered_grp_ids=(101, 102)))

    assert state is not None
    assert state.draft_id == older.draft_id
    assert _state_files(tmp_path=tmp_path) == before
    assert load_draft_state(
        account_id="ACCOUNT-A",
        draft_id=newer.draft_id,
        app_dir=tmp_path,
    ) == newer


def test_replaying_two_saved_drafts_of_one_event_creates_no_new_state(
    tmp_path: Path,
) -> None:
    _saved_draft(
        tmp_path=tmp_path,
        draft_id="older",
        offered_grp_ids=(101, 102),
        updated_at="2026-07-01T12:00:00+00:00",
    )
    _saved_draft(
        tmp_path=tmp_path,
        draft_id="newer",
        offered_grp_ids=(201, 202),
        updated_at="2026-07-02T12:00:00+00:00",
    )
    before = _state_files(tmp_path=tmp_path)
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name=None))

    first = store.consume(event=_repeat_pack_offered(offered_grp_ids=(101, 102)))
    second = store.consume(event=_repeat_pack_offered(offered_grp_ids=(201, 202)))

    assert first is not None
    assert second is not None
    assert [first.draft_id, second.draft_id] == ["older", "newer"]
    assert _state_files(tmp_path=tmp_path) == before


def test_new_first_pack_with_several_completed_drafts_starts_new_state(
    tmp_path: Path,
) -> None:
    _saved_draft(
        tmp_path=tmp_path,
        draft_id="older",
        offered_grp_ids=(101, 102),
        updated_at="2026-07-01T12:00:00+00:00",
    )
    _saved_draft(
        tmp_path=tmp_path,
        draft_id="newer",
        offered_grp_ids=(201, 202),
        updated_at="2026-07-02T12:00:00+00:00",
    )
    before = _state_files(tmp_path=tmp_path)
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name=None))

    state = store.consume(event=_repeat_pack_offered(offered_grp_ids=(301, 302)))

    assert state is not None
    assert state.draft_id.startswith(f"{_REPEAT_EVENT}-")
    after = _state_files(tmp_path=tmp_path)
    assert set(after) - set(before) == {f"{state.draft_id.replace(':', '_')}.json"}
    assert {name: after[name] for name in before} == before


def test_pick_made_with_several_completed_drafts_resumes_latest_updated(
    tmp_path: Path,
) -> None:
    _saved_draft(
        tmp_path=tmp_path,
        draft_id="a-latest",
        offered_grp_ids=(101, 102),
        updated_at="2026-07-03T12:00:00+00:00",
    )
    _saved_draft(
        tmp_path=tmp_path,
        draft_id="z-oldest",
        offered_grp_ids=(201, 202),
        updated_at="2026-07-01T12:00:00+00:00",
    )
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name=None))

    state = store.consume(
        event=PickMadeEvent(
            event_name=_REPEAT_EVENT,
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            selected_grp_ids=(101,),
            account_id=None,
        )
    )

    assert state is not None
    assert state.draft_id == "a-latest"


def test_several_active_drafts_resume_latest_updated_without_error(
    tmp_path: Path,
) -> None:
    _saved_draft(
        tmp_path=tmp_path,
        draft_id="active-old",
        offered_grp_ids=(101, 102),
        updated_at="2026-07-01T12:00:00+00:00",
        completed=False,
    )
    _saved_draft(
        tmp_path=tmp_path,
        draft_id="active-new",
        offered_grp_ids=(201, 202),
        updated_at="2026-07-02T12:00:00+00:00",
        completed=False,
    )
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name=None))

    state = store.consume(event=_repeat_pack_offered(offered_grp_ids=(301, 302)))

    assert state is not None
    assert state.draft_id.startswith(f"{_REPEAT_EVENT}-")

    other_store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    other_store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name=None))
    resumed = other_store.consume(event=_repeat_pack_offered(offered_grp_ids=(101, 102)))

    assert resumed is not None
    assert resumed.draft_id == "active-old"


def test_account_inference_with_several_drafts_of_one_account_succeeds(
    tmp_path: Path,
) -> None:
    for draft_id, updated_at in (
        ("first", "2026-07-01T12:00:00+00:00"),
        ("second", "2026-07-02T12:00:00+00:00"),
    ):
        _saved_draft(
            tmp_path=tmp_path,
            draft_id=draft_id,
            offered_grp_ids=(101, 102),
            updated_at=updated_at,
        )
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)

    state = store.consume(event=_repeat_pack_offered(offered_grp_ids=(101, 102)))

    assert state is not None
    assert state.account_id == "ACCOUNT-A"
    assert state.draft_id == "second"


def test_account_inference_across_accounts_reports_missing_account(
    tmp_path: Path,
) -> None:
    for account_id in ("ACCOUNT-A", "ACCOUNT-B"):
        _saved_draft(
            tmp_path=tmp_path,
            draft_id=f"draft-{account_id}",
            offered_grp_ids=(101, 102),
            updated_at="2026-07-01T12:00:00+00:00",
            account_id=account_id,
        )
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)

    with pytest.raises(DraftPoolError, match="missing an MTGA account id"):
        store.consume(event=_repeat_pack_offered(offered_grp_ids=(101, 102)))


def test_two_card_pick_adds_both_cards_to_pool_at_one_coordinate(
    tmp_path: Path,
) -> None:
    store = _pick_two_store(tmp_path=tmp_path)

    state = store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))

    assert state is not None
    assert state.pool_grp_ids == (101, 102)
    assert state.picks == (
        DraftPick(
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(101, 102, 103),
            pool_before_pick=(),
            selected_grp_ids=(101, 102),
        ),
    )
    assert state.chosen_pick_count == 1
    assert state.selected_card_count == 2
    assert _load_pick_two_state(tmp_path=tmp_path) == state


def test_duplicate_identical_multi_card_pick_is_a_no_op(tmp_path: Path) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))
    path = store.path_for(account_id="ACCOUNT-A", draft_id=PICK_TWO_EVENT_NAME)
    saved_payload = path.read_text(encoding="utf-8")

    replayed = store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))

    assert replayed is not None
    assert replayed.pool_grp_ids == (101, 102)
    assert replayed.selected_card_count == 2
    assert path.read_text(encoding="utf-8") == saved_payload


def test_different_selection_at_same_coordinate_raises_conflict(
    tmp_path: Path,
) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))
    before = _load_pick_two_state(tmp_path=tmp_path)

    with pytest.raises(DraftPoolError, match="selected_grp_ids changed from"):
        store.consume(event=_pick_two_pick(selected_grp_ids=(101, 103)))

    assert _load_pick_two_state(tmp_path=tmp_path) == before


def test_multi_card_pick_with_unoffered_card_raises(tmp_path: Path) -> None:
    store = _pick_two_store(tmp_path=tmp_path)

    with pytest.raises(DraftPoolError, match="which were not all offered"):
        store.consume(event=_pick_two_pick(selected_grp_ids=(101, 101)))


def test_multi_card_pick_survives_next_pack_and_completion(tmp_path: Path) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))
    store.consume(
        event=PackOfferedEvent(
            event_name=PICK_TWO_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=1,
            offered_grp_ids=(201, 202),
            pool_grp_ids=(102, 101),
            account_id=None,
        )
    )
    pending = _load_pick_two_state(tmp_path=tmp_path)
    assert pending.pool_grp_ids == (101, 102)
    assert pending.pick_for(pack_number=0, pick_number=1) == DraftPick(
        pack_number=0,
        pick_number=1,
        offered_grp_ids=(201, 202),
        pool_before_pick=(102, 101),
    )

    store.consume(
        event=_pick_two_pick(selected_grp_ids=(201, 202), pick_number=1)
    )
    completed = store.consume(
        event=DraftCompletedEvent(
            event_name=PICK_TWO_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=1,
            picked_grp_ids=(202, 101, 201, 102),
            inferred=False,
            account_id=None,
        )
    )

    assert completed is not None
    assert completed.completed is True
    assert completed.pool_grp_ids == (101, 102, 201, 202)
    assert completed.chosen_pick_count == 2
    assert completed.selected_card_count == 4


def test_schema_1_state_file_migrates_chosen_card_on_load(tmp_path: Path) -> None:
    path = draft_state_path(
        account_id="ACCOUNT-A",
        draft_id="draft-v1",
        app_dir=tmp_path,
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "account_id": "ACCOUNT-A",
                "account_screen_name": None,
                "draft_id": "draft-v1",
                "event_name": "QuickDraft_ABC_20260703",
                "set_code": "ABC",
                "course_id": "draft-v1",
                "started_at": "2026-07-03T12:00:00+00:00",
                "updated_at": "2026-07-03T12:00:00+00:00",
                "completed_at": None,
                "completed": False,
                "picks": [
                    {
                        "pack_number": 0,
                        "pick_number": 0,
                        "offered_grp_ids": [101, 102],
                        "pool_before_pick": [],
                        LEGACY_CHOSEN_CARD_KEY: 101,
                    },
                    {
                        "pack_number": 0,
                        "pick_number": 1,
                        "offered_grp_ids": [201, 202],
                        "pool_before_pick": [101],
                        LEGACY_CHOSEN_CARD_KEY: None,
                    },
                ],
                "pool_grp_ids": [101],
            }
        ),
        encoding="utf-8",
    )

    state = load_draft_state(account_id="ACCOUNT-A", draft_id="draft-v1", app_dir=tmp_path)

    assert state.picks == (
        DraftPick(
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(101, 102),
            pool_before_pick=(),
            selected_grp_ids=(101,),
        ),
        DraftPick(
            pack_number=0,
            pick_number=1,
            offered_grp_ids=(201, 202),
            pool_before_pick=(101,),
            selected_grp_ids=(),
        ),
    )
    assert state.chosen_pick_count == 1
    assert state.selected_card_count == 1

    save_draft_state(state=state, app_dir=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert [pick["selected_grp_ids"] for pick in payload["picks"]] == [[101], []]
    assert all(LEGACY_CHOSEN_CARD_KEY not in pick for pick in payload["picks"])


def test_schema_1_state_file_with_invalid_chosen_card_raises() -> None:
    with pytest.raises(DraftPoolError, match=rf"picks\[0\]\.{LEGACY_CHOSEN_CARD_KEY}"):
        DraftState.from_json(
            data={
                "schema_version": 1,
                "picks": [
                    {"pack_number": 0, "pick_number": 0, LEGACY_CHOSEN_CARD_KEY: "x"}
                ],
            }
        )


def test_schema_2_state_file_round_trips_multi_card_picks(tmp_path: Path) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    state = store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))
    assert state is not None

    payload = json.loads(
        store.path_for(account_id="ACCOUNT-A", draft_id=PICK_TWO_EVENT_NAME).read_text(
            encoding="utf-8"
        )
    )

    assert payload["schema_version"] == 2
    assert payload["picks"][0]["selected_grp_ids"] == [101, 102]
    assert DraftState.from_json(data=payload) == state


def test_unknown_state_schema_raises() -> None:
    with pytest.raises(DraftPoolError, match="Unsupported draft state schema 3"):
        DraftState.from_json(data={"schema_version": 3, "picks": []})


def test_superset_card_pool_becomes_the_pool_without_filling_the_gap(
    tmp_path: Path,
) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))

    completed = store.consume(
        event=_pick_two_completion(card_pool_grp_ids=(101, 901, 102, 902))
    )

    assert completed is not None
    assert completed.completed is True
    assert completed.pool_grp_ids == (101, 901, 102, 902)
    assert completed.selected_card_count == 2
    assert completed.pick_for(pack_number=0, pick_number=1) is None
    assert _load_pick_two_state(tmp_path=tmp_path) == completed


def test_late_card_pool_updates_the_pool_of_a_completed_draft(
    tmp_path: Path,
) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))
    first = store.consume(event=_pick_two_completion())
    assert first is not None
    later_store = DraftPoolStore(app_dir=tmp_path, clock=_later_clock)
    later_store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))

    updated = later_store.consume(
        event=_pick_two_completion(card_pool_grp_ids=(101, 102, 901, 902))
    )

    assert updated is not None
    assert updated.completed is True
    assert updated.completed_at == first.completed_at
    assert updated.updated_at == _later_clock().isoformat()
    assert updated.pool_grp_ids == (101, 102, 901, 902)
    assert updated.selected_card_count == 2
    assert _load_pick_two_state(tmp_path=tmp_path) == updated


def test_replaying_both_completions_after_the_card_pool_changes_nothing(
    tmp_path: Path,
) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))
    store.consume(event=_pick_two_completion())
    store.consume(event=_pick_two_completion(card_pool_grp_ids=(101, 102, 901, 902)))
    path = store.path_for(account_id="ACCOUNT-A", draft_id=PICK_TWO_EVENT_NAME)
    saved_payload = path.read_text(encoding="utf-8")

    store.consume(event=_pick_two_completion())
    replayed = store.consume(
        event=_pick_two_completion(card_pool_grp_ids=(101, 102, 901, 902))
    )

    assert replayed is not None
    assert replayed.pool_grp_ids == (101, 102, 901, 902)
    assert path.read_text(encoding="utf-8") == saved_payload


@pytest.mark.parametrize("completed_first", [False, True])
def test_card_pool_missing_a_recorded_card_keeps_the_pool_and_warns(
    completed_first: bool,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))
    if completed_first:
        store.consume(event=_pick_two_completion())

    with caplog.at_level(logging.WARNING, logger="draftomen.pool"):
        state = store.consume(
            event=_pick_two_completion(card_pool_grp_ids=(101, 901, 902))
        )

    assert state is not None
    assert state.completed is True
    assert state.pool_grp_ids == (101, 102)
    assert _load_pick_two_state(tmp_path=tmp_path).pool_grp_ids == (101, 102)
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert "lacks recorded cards" in caplog.records[0].getMessage()


def test_completion_that_conflicts_with_recorded_picks_still_raises(
    tmp_path: Path,
) -> None:
    store = _pick_two_store(tmp_path=tmp_path)
    store.consume(event=_pick_two_pick(selected_grp_ids=(101, 102)))
    store.consume(event=_pick_two_completion(card_pool_grp_ids=(101, 102, 901)))

    with pytest.raises(DraftPoolError, match="conflicts with saved pool"):
        store.consume(
            event=replace(_pick_two_completion(), picked_grp_ids=(101, 103))
        )


PICK_TWO_EVENT_NAME = "PickTwoDraft_ABC_20260703"


def _pick_two_completion(
    *,
    card_pool_grp_ids: tuple[int, ...] | None = None,
) -> DraftCompletedEvent:
    return DraftCompletedEvent(
        event_name=PICK_TWO_EVENT_NAME,
        set_code="ABC",
        pack_number=0,
        pick_number=0,
        picked_grp_ids=(101, 102),
        inferred=False,
        account_id=None,
        card_pool_grp_ids=card_pool_grp_ids,
    )


def _pick_two_store(*, tmp_path: Path) -> DraftPoolStore:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))
    store.consume(
        event=PackOfferedEvent(
            event_name=PICK_TWO_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=0,
            offered_grp_ids=(101, 102, 103),
            pool_grp_ids=(),
            account_id=None,
        )
    )
    return store


def _pick_two_pick(
    *,
    selected_grp_ids: tuple[int, ...],
    pick_number: int = 0,
) -> PickMadeEvent:
    return PickMadeEvent(
        event_name=PICK_TWO_EVENT_NAME,
        set_code="ABC",
        pack_number=0,
        pick_number=pick_number,
        selected_grp_ids=selected_grp_ids,
        account_id=None,
    )


def _load_pick_two_state(*, tmp_path: Path) -> DraftState:
    return load_draft_state(
        account_id="ACCOUNT-A",
        draft_id=PICK_TWO_EVENT_NAME,
        app_dir=tmp_path,
    )


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


RESUME_EVENT_NAME = "PremierDraft_ABC_20260703"


def test_resumed_pack_uses_saved_pool_as_pool_before_pick(tmp_path: Path) -> None:
    store = _saved_resume_store(tmp_path=tmp_path)

    state = store.consume(event=_resumed_pack(pick_number=2, pool_grp_ids=()))

    pick = state.pick_for(pack_number=0, pick_number=2)
    assert pick is not None
    assert pick.pool_before_pick == (101, 102)
    assert state.pool_grp_ids == (101, 102)


def test_resumed_pack_replay_keeps_saved_pool(tmp_path: Path) -> None:
    store = _saved_resume_store(tmp_path=tmp_path)
    store.consume(event=_resumed_pack(pick_number=2, pool_grp_ids=()))
    store.consume(
        event=PickMadeEvent(
            event_name=RESUME_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=2,
            selected_grp_ids=(103,),
            account_id="ACCOUNT-A",
        )
    )

    state = store.consume(event=_resumed_pack(pick_number=2, pool_grp_ids=(103,)))

    pick = state.pick_for(pack_number=0, pick_number=2)
    assert pick is not None
    assert pick.pool_before_pick == (101, 102)
    assert state.pool_grp_ids == (101, 102, 103)


def test_resumed_pack_adds_missing_seed_cards_to_saved_pool(tmp_path: Path) -> None:
    store = _saved_resume_store(tmp_path=tmp_path)

    state = store.consume(event=_resumed_pack(pick_number=2, pool_grp_ids=(999,)))

    pick = state.pick_for(pack_number=0, pick_number=2)
    assert pick is not None
    assert Counter(state.pool_grp_ids) == Counter((101, 102, 999))
    assert pick.pool_before_pick == state.pool_grp_ids


def test_resumed_pack_with_seed_and_saved_picks_completes_to_card_pool(
    tmp_path: Path,
) -> None:
    store = _saved_resume_store(tmp_path=tmp_path)
    store.consume(event=_resumed_pack(pick_number=2, pool_grp_ids=(900,)))
    store.consume(
        event=PickMadeEvent(
            event_name=RESUME_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=2,
            selected_grp_ids=(103,),
            account_id="ACCOUNT-A",
        )
    )
    store.consume(event=_resumed_pack(pick_number=3, pool_grp_ids=(900, 103)))

    state = store.consume(
        event=_resumed_completion(
            picked_grp_ids=(900, 103),
            card_pool_grp_ids=(101, 102, 103, 900),
        )
    )

    assert state.completed is True
    assert Counter(state.pool_grp_ids) == Counter((101, 102, 103, 900))


def test_non_resumed_pack_with_partial_pool_still_raises(tmp_path: Path) -> None:
    store = _saved_resume_store(tmp_path=tmp_path)

    with pytest.raises(DraftPoolError, match="accumulated pool"):
        store.consume(
            event=replace(
                _resumed_pack(pick_number=2, pool_grp_ids=()),
                resumed=False,
            )
        )


def test_resumed_completion_merges_card_pool_and_replays(tmp_path: Path) -> None:
    store = _saved_resume_store(tmp_path=tmp_path)
    store.consume(event=_resumed_pack(pick_number=2, pool_grp_ids=()))
    store.consume(
        event=PickMadeEvent(
            event_name=RESUME_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=2,
            selected_grp_ids=(103,),
            account_id="ACCOUNT-A",
        )
    )
    completion = _resumed_completion(
        picked_grp_ids=(103,),
        card_pool_grp_ids=(101, 102, 103),
    )

    state = store.consume(event=completion)
    replayed = store.consume(event=completion)

    assert state.completed is True
    assert Counter(state.pool_grp_ids) == Counter((101, 102, 103))
    assert replayed.pool_grp_ids == state.pool_grp_ids


def test_resumed_completion_replay_after_card_pool_replaced_pool(tmp_path: Path) -> None:
    store = _saved_resume_store(tmp_path=tmp_path)
    store.consume(
        event=PickMadeEvent(
            event_name=RESUME_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=2,
            selected_grp_ids=(103,),
            account_id="ACCOUNT-A",
        )
    )
    store.consume(
        event=_resumed_completion(
            picked_grp_ids=(103,),
            card_pool_grp_ids=(101, 102, 103, 104),
        )
    )

    state = store.consume(
        event=_resumed_completion(
            picked_grp_ids=(103,),
            card_pool_grp_ids=(101, 102, 103, 104),
        )
    )

    assert Counter(state.pool_grp_ids) == Counter((101, 102, 103, 104))


def test_resumed_completion_with_pick_missing_from_saved_pool_raises(
    tmp_path: Path,
) -> None:
    store = _saved_resume_store(tmp_path=tmp_path)

    with pytest.raises(DraftPoolError, match="does not match accumulated pool"):
        store.consume(
            event=_resumed_completion(
                picked_grp_ids=(999,),
                card_pool_grp_ids=None,
            )
        )


def test_resumed_draft_without_saved_state_completes_to_card_pool(
    tmp_path: Path,
) -> None:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))

    store.consume(
        event=PickMadeEvent(
            event_name=RESUME_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=2,
            selected_grp_ids=(103,),
            account_id="ACCOUNT-A",
        )
    )
    store.consume(event=_resumed_pack(pick_number=3, pool_grp_ids=(103,)))
    store.consume(
        event=PickMadeEvent(
            event_name=RESUME_EVENT_NAME,
            set_code="ABC",
            pack_number=0,
            pick_number=3,
            selected_grp_ids=(104,),
            account_id="ACCOUNT-A",
        )
    )
    state = store.consume(
        event=_resumed_completion(
            picked_grp_ids=(103, 104),
            card_pool_grp_ids=(101, 102, 103, 104),
        )
    )

    assert state.completed is True
    assert Counter(state.pool_grp_ids) == Counter((101, 102, 103, 104))


def _saved_resume_store(*, tmp_path: Path) -> DraftPoolStore:
    store = DraftPoolStore(app_dir=tmp_path, clock=_fixed_clock)
    store.consume(event=AccountEvent(client_id="ACCOUNT-A", screen_name="First"))
    store.consume(
        event=DraftStartedEvent(
            event_name=RESUME_EVENT_NAME,
            set_code="ABC",
            course_id="resume-course",
            account_id="ACCOUNT-A",
        )
    )
    for pick_number, card, pool in ((0, 101, ()), (1, 102, (101,))):
        store.consume(
            event=PackOfferedEvent(
                event_name=RESUME_EVENT_NAME,
                set_code="ABC",
                pack_number=0,
                pick_number=pick_number,
                offered_grp_ids=(card, 103),
                pool_grp_ids=pool,
                account_id="ACCOUNT-A",
            )
        )
        store.consume(
            event=PickMadeEvent(
                event_name=RESUME_EVENT_NAME,
                set_code="ABC",
                pack_number=0,
                pick_number=pick_number,
                selected_grp_ids=(card,),
                account_id="ACCOUNT-A",
            )
        )
    return store


def _resumed_pack(
    *,
    pick_number: int,
    pool_grp_ids: tuple[int, ...],
) -> PackOfferedEvent:
    return PackOfferedEvent(
        event_name=RESUME_EVENT_NAME,
        set_code="ABC",
        pack_number=0,
        pick_number=pick_number,
        offered_grp_ids=(103, 104),
        pool_grp_ids=pool_grp_ids,
        account_id="ACCOUNT-A",
        resumed=True,
    )


def _resumed_completion(
    *,
    picked_grp_ids: tuple[int, ...],
    card_pool_grp_ids: tuple[int, ...] | None,
) -> DraftCompletedEvent:
    return DraftCompletedEvent(
        event_name=RESUME_EVENT_NAME,
        set_code="ABC",
        pack_number=0,
        pick_number=3,
        picked_grp_ids=picked_grp_ids,
        inferred=False,
        account_id="ACCOUNT-A",
        card_pool_grp_ids=card_pool_grp_ids,
        resumed=True,
    )
