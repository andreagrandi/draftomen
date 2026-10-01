from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.draft_format import DraftFormat, detect_draft_format
from draftomen.events import (
    DraftCompletedEvent,
    DraftEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
)
from draftomen.session import ApplicationPhase, LiveSession
from draftomen.moxgate import (
    MOXGATE_SCHEMA_VERSION,
    MoxgateAdapter,
    MoxgateSnapshot,
    MoxgateSnapshotCard,
    MoxgateSnapshotError,
)

DRAFT_ID = "draft-1"
ACCOUNT_ID = "account-1"


def _database(*grp_ids: int, set_codes: dict[int, str] | None = None) -> CardDatabase:
    overrides = set_codes or {}
    return CardDatabase(
        cards={
            grp_id: CardInfo(
                grp_id=grp_id,
                name=f"Fixture {grp_id}",
                colors=("W",),
                mana_value=2.0,
                rarity="common",
                types=("Creature",),
                set_code=overrides.get(grp_id, "hob"),
            )
            for grp_id in grp_ids
        }
    )


def _card(grp_id: int) -> MoxgateSnapshotCard:
    return MoxgateSnapshotCard(
        scryfall_id=f"scryfall-{grp_id}", name=f"Fixture {grp_id}"
    )


def _scryfall_map(*grp_ids: int) -> dict[str, int]:
    return {f"scryfall-{grp_id}": grp_id for grp_id in grp_ids}


def _packs(*, pack_count: int, pack_size: int) -> list[list[int]]:
    return [
        [1000 + pack * 100 + slot for slot in range(pack_size)]
        for pack in range(pack_count)
    ]


def _all_grp_ids(packs: list[list[int]]) -> tuple[int, ...]:
    return tuple(sorted({grp_id for pack in packs for grp_id in pack}))


def _adapter(
    packs: list[list[int]],
    *,
    set_codes: dict[int, str] | None = None,
) -> MoxgateAdapter:
    grp_ids = _all_grp_ids(packs)
    return MoxgateAdapter(
        card_database=_database(*grp_ids, set_codes=set_codes),
        canonical_grp_ids_by_scryfall_id=_scryfall_map(*grp_ids),
        draft_id=DRAFT_ID,
        account_id=ACCOUNT_ID,
    )


def _snapshots(
    packs: list[list[int]],
    *,
    choose: int = 0,
) -> tuple[tuple[MoxgateSnapshot, int], ...]:
    """Return each snapshot with the grpId picked from its pack.
    The last entry is the final snapshot with an empty pack, paired with 0.
    """

    pack_size = len(packs[0])
    total_picks = len(packs) * pack_size
    pool: list[int] = []
    result: list[tuple[MoxgateSnapshot, int]] = []
    current: list[int] = []
    for index in range(total_picks):
        if index % pack_size == 0:
            current = list(packs[index // pack_size])

        pick = current[min(choose, len(current) - 1)]
        snapshot = MoxgateSnapshot(
            schema_version=MOXGATE_SCHEMA_VERSION,
            pick_index=index,
            total_picks=total_picks,
            pack=tuple(_card(grp_id) for grp_id in current),
            pool=tuple(_card(grp_id) for grp_id in pool),
        )
        result.append((snapshot, pick))
        pool.append(pick)
        current.remove(pick)

    final = MoxgateSnapshot(
        schema_version=MOXGATE_SCHEMA_VERSION,
        pick_index=total_picks,
        total_picks=total_picks,
        pack=(),
        pool=tuple(_card(grp_id) for grp_id in pool),
    )
    result.append((final, 0))
    return tuple(result)


def _run(
    adapter: MoxgateAdapter, snapshots: tuple[tuple[MoxgateSnapshot, int], ...]
) -> list[DraftEvent]:
    events: list[DraftEvent] = []
    for snapshot, _ in snapshots:
        events.extend(adapter.process(snapshot=snapshot))
    return events


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "pick_index": 0,
        "total_picks": 2,
        "pack": [{"scryfall_id": "scryfall-1", "name": "Fixture 1"}],
        "pool": [],
    }
    payload.update(overrides)
    return payload


def test_full_draft_emits_started_offers_picks_and_completed_in_order() -> None:
    packs = _packs(pack_count=3, pack_size=14)
    snapshots = _snapshots(packs)
    adapter = _adapter(packs)

    events = _run(adapter, snapshots)

    assert isinstance(events[0], DraftStartedEvent)
    assert events[0].event_name == f"QuickDraft_HOB_Moxgate_{DRAFT_ID}"
    assert events[0].set_code == "HOB"
    assert events[0].course_id == DRAFT_ID
    assert events[0].account_id == ACCOUNT_ID
    offers = [event for event in events if isinstance(event, PackOfferedEvent)]
    picks = [event for event in events if isinstance(event, PickMadeEvent)]
    completed = [event for event in events if isinstance(event, DraftCompletedEvent)]
    assert len([event for event in events if isinstance(event, DraftStartedEvent)]) == 1
    assert len(offers) == 42
    assert len(picks) == 42
    assert len(completed) == 1
    assert isinstance(events[-1], DraftCompletedEvent)
    # Each pick comes before the offer that follows it, so the stream alternates.
    expected_order = [DraftStartedEvent]
    for _ in range(42):
        expected_order += [PackOfferedEvent, PickMadeEvent]
    expected_order.append(DraftCompletedEvent)
    assert [type(event) for event in events] == expected_order
    assert (offers[0].pack_number, offers[0].pick_number) == (0, 0)
    assert (offers[14].pack_number, offers[14].pick_number) == (1, 0)
    assert (offers[-1].pack_number, offers[-1].pick_number) == (2, 13)
    assert (picks[14].pack_number, picks[14].pick_number) == (1, 0)
    assert (picks[-1].pack_number, picks[-1].pick_number) == (2, 13)
    assert all(event.picks_per_pack == 14 for event in offers)
    assert all(event.draft_format is DraftFormat.QUICK for event in offers)
    assert all(event.cards_per_pick == 1 for event in offers)
    assert detect_draft_format(event_name=events[0].event_name) is DraftFormat.QUICK
    pick_ids = tuple(event.selected_grp_ids[0] for event in picks)
    assert completed[0].picked_grp_ids == pick_ids
    assert completed[0].pool_grp_ids == pick_ids
    assert (completed[0].pack_number, completed[0].pick_number) == (2, 13)
    assert completed[0].inferred is False
    assert adapter.completed is True


def test_fifteen_card_packs_derive_fifteen_picks_per_pack() -> None:
    packs = _packs(pack_count=3, pack_size=15)
    adapter = _adapter(packs)

    events = _run(adapter, _snapshots(packs))

    offers = [event for event in events if isinstance(event, PackOfferedEvent)]
    assert len(offers) == 45
    assert (offers[15].pack_number, offers[15].pick_number) == (1, 0)
    assert (offers[44].pack_number, offers[44].pick_number) == (2, 14)
    assert offers[15].picks_per_pack == 15
    assert len(offers[15].offered_grp_ids) == 15


def test_selected_grp_id_is_the_card_added_to_the_pool() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    adapter = _adapter(packs)

    events = _run(adapter, _snapshots(packs, choose=2))

    picks = [event for event in events if isinstance(event, PickMadeEvent)]
    assert picks[0].selected_grp_ids == (packs[0][2],)
    offers = [event for event in events if isinstance(event, PackOfferedEvent)]
    assert offers[1].pool_grp_ids == (packs[0][2],)
    assert offers[1].offered_grp_ids == (packs[0][0], packs[0][1], packs[0][3])


def test_duplicate_card_in_pack_produces_one_selected_grp_id() -> None:
    packs = [[1, 2, 2, 3]]
    adapter = _adapter(packs)
    first = MoxgateSnapshot(
        schema_version=1,
        pick_index=0,
        total_picks=4,
        pack=tuple(_card(grp_id) for grp_id in (1, 2, 2, 3)),
        pool=(),
    )
    second = MoxgateSnapshot(
        schema_version=1,
        pick_index=1,
        total_picks=4,
        pack=tuple(_card(grp_id) for grp_id in (1, 2, 3)),
        pool=(_card(2),),
    )

    adapter.process(snapshot=first)
    events = adapter.process(snapshot=second)

    assert isinstance(events[0], PickMadeEvent)
    assert events[0].selected_grp_ids == (2,)
    assert isinstance(events[1], PackOfferedEvent)
    assert events[1].offered_grp_ids == (1, 2, 3)
    assert events[1].pool_grp_ids == (2,)


def test_repeated_snapshot_mid_draft_emits_nothing() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    snapshots = _snapshots(packs)
    adapter = _adapter(packs)

    adapter.process(snapshot=snapshots[0][0])
    adapter.process(snapshot=snapshots[1][0])

    assert adapter.process(snapshot=snapshots[1][0]) == ()
    assert len(adapter.process(snapshot=snapshots[2][0])) == 2


def test_repeated_snapshot_after_completion_emits_nothing() -> None:
    packs = _packs(pack_count=1, pack_size=3)
    snapshots = _snapshots(packs)
    adapter = _adapter(packs)
    _run(adapter, snapshots)

    assert adapter.process(snapshot=snapshots[-1][0]) == ()
    assert adapter.completed is True


def test_different_snapshot_after_completion_raises() -> None:
    packs = _packs(pack_count=1, pack_size=3)
    snapshots = _snapshots(packs)
    adapter = _adapter(packs)
    _run(adapter, snapshots)

    with pytest.raises(MoxgateSnapshotError, match="already complete"):
        adapter.process(snapshot=snapshots[0][0])


def test_unknown_schema_version_names_received_and_supported_versions() -> None:
    with pytest.raises(
        MoxgateSnapshotError, match=r"schema_version 2.*supported version is 1"
    ):
        MoxgateSnapshot.from_mapping(value=_payload(schema_version=2))


@pytest.mark.parametrize(
    ("field_name", "match"),
    [
        ("schema_version", "missing schema_version"),
        ("pick_index", "missing pick_index"),
        ("total_picks", "missing total_picks"),
        ("pack", "missing pack"),
        ("pool", "missing pool"),
    ],
)
def test_missing_field_names_the_field(field_name: str, match: str) -> None:
    payload = _payload()
    del payload[field_name]

    with pytest.raises(MoxgateSnapshotError, match=match):
        MoxgateSnapshot.from_mapping(value=payload)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"pick_index": True}, "pick_index must be an integer"),
        ({"pick_index": -1}, "pick_index must not be negative"),
        ({"total_picks": 0}, "total_picks must be at least 1"),
        ({"pick_index": 3}, "pick_index 3 exceeds total_picks 2"),
        ({"pack": [{"scryfall_id": "", "name": "A"}]}, r"pack\[0\].scryfall_id"),
        ({"pack": [{"scryfall_id": "x"}]}, r"pack\[0\].name"),
        ({"pool": [{"scryfall_id": "x", "name": "A"}]}, "pool has 1 cards"),
        ({"pack": []}, "pack is empty"),
    ],
)
def test_invalid_field_raises_with_field_name(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(MoxgateSnapshotError, match=match):
        MoxgateSnapshot.from_mapping(value=_payload(**overrides))


def test_non_object_and_invalid_json_raise() -> None:
    with pytest.raises(MoxgateSnapshotError, match="not valid JSON"):
        MoxgateSnapshot.from_json(text="{")

    with pytest.raises(MoxgateSnapshotError, match="must be a JSON object"):
        MoxgateSnapshot.from_json(text="[]")


def test_final_snapshot_with_cards_in_pack_raises() -> None:
    payload = _payload(
        pick_index=2,
        pool=[
            {"scryfall_id": "scryfall-1", "name": "Fixture 1"},
            {"scryfall_id": "scryfall-2", "name": "Fixture 2"},
        ],
    )

    with pytest.raises(MoxgateSnapshotError, match="pack must be empty"):
        MoxgateSnapshot.from_mapping(value=payload)


def test_from_json_round_trip_of_valid_payload() -> None:
    text = json.dumps(_payload())

    for source in (text, text.encode()):
        snapshot = MoxgateSnapshot.from_json(text=source)

        assert snapshot == MoxgateSnapshot(
            schema_version=1,
            pick_index=0,
            total_picks=2,
            pack=(MoxgateSnapshotCard(scryfall_id="scryfall-1", name="Fixture 1"),),
            pool=(),
        )


def test_unresolvable_scryfall_id_names_the_card() -> None:
    packs = _packs(pack_count=1, pack_size=3)
    adapter = _adapter(packs)
    snapshot = replace(
        _snapshots(packs)[0][0],
        pack=(
            MoxgateSnapshotCard(scryfall_id="scryfall-missing", name="Lost Card"),
            _card(packs[0][1]),
            _card(packs[0][2]),
        ),
    )

    with pytest.raises(MoxgateSnapshotError, match=r"'Lost Card'.*'scryfall-missing'"):
        adapter.process(snapshot=snapshot)


def test_grp_id_missing_from_card_database_names_the_card() -> None:
    packs = _packs(pack_count=1, pack_size=3)
    grp_ids = _all_grp_ids(packs)
    adapter = MoxgateAdapter(
        card_database=_database(*grp_ids[:-1]),
        canonical_grp_ids_by_scryfall_id=_scryfall_map(*grp_ids),
        draft_id=DRAFT_ID,
        account_id=None,
    )

    with pytest.raises(
        MoxgateSnapshotError, match=rf"'Fixture {grp_ids[-1]}'.*cannot resolve"
    ):
        adapter.process(snapshot=_snapshots(packs)[0][0])


def test_empty_draft_id_raises() -> None:
    with pytest.raises(MoxgateSnapshotError, match="draft_id"):
        MoxgateAdapter(
            card_database=_database(),
            canonical_grp_ids_by_scryfall_id={},
            draft_id="",
            account_id=None,
        )


def test_first_snapshot_after_first_pick_raises() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    adapter = _adapter(packs)

    with pytest.raises(
        MoxgateSnapshotError, match="after its first pick is not supported"
    ):
        adapter.process(snapshot=_snapshots(packs)[1][0])


def test_total_picks_not_a_multiple_of_pack_size_raises() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    adapter = _adapter(packs)
    snapshot = replace(_snapshots(packs)[0][0], total_picks=6)

    with pytest.raises(MoxgateSnapshotError, match="not a multiple"):
        adapter.process(snapshot=snapshot)


def test_pick_counter_going_back_raises_and_adapter_recovers() -> None:
    packs = _packs(pack_count=1, pack_size=5)
    snapshots = _snapshots(packs)
    adapter = _adapter(packs)
    for index in range(3):
        adapter.process(snapshot=snapshots[index][0])

    with pytest.raises(MoxgateSnapshotError, match="expected 3 but received 1"):
        adapter.process(snapshot=snapshots[1][0])

    events = adapter.process(snapshot=snapshots[3][0])
    assert isinstance(events[0], PickMadeEvent)
    assert (events[0].pack_number, events[0].pick_number) == (0, 2)


def test_pick_counter_skipping_raises_and_adapter_recovers() -> None:
    packs = _packs(pack_count=1, pack_size=5)
    snapshots = _snapshots(packs)
    adapter = _adapter(packs)
    adapter.process(snapshot=snapshots[0][0])

    with pytest.raises(MoxgateSnapshotError, match="expected 1 but received 2"):
        adapter.process(snapshot=snapshots[2][0])

    events = adapter.process(snapshot=snapshots[1][0])
    assert isinstance(events[0], PickMadeEvent)
    assert (events[0].pack_number, events[0].pick_number) == (0, 0)


def test_set_code_is_the_majority_of_the_first_pack() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    adapter = _adapter(packs, set_codes={packs[0][0]: "abc"})

    events = adapter.process(snapshot=_snapshots(packs)[0][0])

    assert isinstance(events[0], DraftStartedEvent)
    assert events[0].set_code == "HOB"
    assert events[0].event_name == f"QuickDraft_HOB_Moxgate_{DRAFT_ID}"


def test_set_code_tie_breaks_on_alphabetically_first_code() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    adapter = _adapter(packs, set_codes={packs[0][0]: "abc", packs[0][1]: "abc"})

    events = adapter.process(snapshot=_snapshots(packs)[0][0])

    assert isinstance(events[0], DraftStartedEvent)
    assert events[0].set_code == "ABC"


def test_pool_gaining_a_card_not_in_previous_pack_raises() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    snapshots = _snapshots(packs)
    grp_ids = _all_grp_ids(packs)
    adapter = MoxgateAdapter(
        card_database=_database(*grp_ids, 9999),
        canonical_grp_ids_by_scryfall_id=_scryfall_map(*grp_ids, 9999),
        draft_id=DRAFT_ID,
        account_id=None,
    )
    adapter.process(snapshot=snapshots[0][0])
    bad = replace(snapshots[1][0], pool=(_card(9999),))

    with pytest.raises(MoxgateSnapshotError, match="not in the previous pack"):
        adapter.process(snapshot=bad)

    assert len(adapter.process(snapshot=snapshots[1][0])) == 2


def test_pool_that_drops_a_previous_card_raises() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    snapshots = _snapshots(packs)
    adapter = _adapter(packs)
    for index in range(2):
        adapter.process(snapshot=snapshots[index][0])
    bad = replace(snapshots[2][0], pool=(_card(packs[0][1]), _card(packs[0][2])))

    with pytest.raises(
        MoxgateSnapshotError, match="previous pool plus exactly one new card"
    ):
        adapter.process(snapshot=bad)


def test_pack_with_wrong_size_raises() -> None:
    packs = _packs(pack_count=1, pack_size=4)
    snapshots = _snapshots(packs)
    adapter = _adapter(packs)
    adapter.process(snapshot=snapshots[0][0])
    bad = replace(snapshots[1][0], pack=snapshots[1][0].pack[:-1])

    with pytest.raises(MoxgateSnapshotError, match="expected 3 cards but received 2"):
        adapter.process(snapshot=bad)


def test_live_session_completes_a_moxgate_draft_with_the_picked_pool(tmp_path: Path) -> None:
    packs = _packs(pack_count=3, pack_size=14)
    adapter = _adapter(packs)
    session = LiveSession(
        log_path=None,
        app_dir=tmp_path / "app",
        card_database=_database(*_all_grp_ids(packs)),
        event_publisher=lambda item: None,
    )

    for snapshot, _ in _snapshots(packs):
        session.process_events(events=adapter.process(snapshot=snapshot))

    picked = tuple(pick for _, pick in _snapshots(packs)[:-1])
    assert session.snapshot.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert tuple(pool_card.card.grp_id for pool_card in session.snapshot.pool.cards) == picked
