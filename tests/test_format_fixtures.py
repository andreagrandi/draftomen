from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path

import pytest

from draftomen.carddb import CardDatabase, CardInfo, build_card_database_from_bulk_file
from draftomen.events import (
    DraftCompletedEvent,
    DraftEvent,
    DraftLogParseError,
    PackOfferedEvent,
    PickMadeEvent,
    parse_events,
)
from draftomen.pool import DraftPoolStore, DraftState
from draftomen.replay import replay_log_file
from draftomen.session import (
    ApplicationPhase,
    LiveSession,
    LiveSessionSnapshot,
)
from draftomen.watch import PlainLogWatcher, run_plain_watch

FIXTURES_PATH = Path(__file__).parent / "fixtures"
SCRYFALL_BULK_SAMPLE_PATH = FIXTURES_PATH / "scryfall-default-cards-sample.jsonl"
TOTAL_CARDS = 42
PICK_COORDINATE = (1, 3)
THREAD_PREFIX = "[4675] "


@dataclass(frozen=True)
class FormatCase:
    """Describe one complete synthetic fixture and what it must produce.
    Counts follow the format rules: one or two cards for each logical pick.
    """

    path: Path
    format_label: str
    logical_picks: int
    cards_per_pick: int
    picks_per_pack: int


PREMIER_CASE = FormatCase(
    path=FIXTURES_PATH / "premier-draft-complete.log",
    format_label="Premier Draft",
    logical_picks=42,
    cards_per_pick=1,
    picks_per_pack=14,
)
TRADITIONAL_CASE = FormatCase(
    path=FIXTURES_PATH / "traditional-draft-complete.log",
    format_label="Traditional Draft",
    logical_picks=42,
    cards_per_pick=1,
    picks_per_pack=14,
)
PICK_TWO_CASE = FormatCase(
    path=FIXTURES_PATH / "pick-two-draft-complete.log",
    format_label="Pick-Two",
    logical_picks=21,
    cards_per_pick=2,
    picks_per_pack=7,
)
ALL_CASES = [
    pytest.param(PREMIER_CASE, id="premier"),
    pytest.param(TRADITIONAL_CASE, id="traditional"),
    pytest.param(PICK_TWO_CASE, id="pick-two"),
]


def _fixture_lines(*, case: FormatCase) -> list[str]:
    return case.path.read_text(encoding="utf-8").splitlines()


def _fixture_card_database() -> CardDatabase:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)
    cards = {
        grp_id: replace(card, set_code="msh")
        for grp_id, card in database.cards.items()
    }
    cards[9001] = CardInfo(
        grp_id=9001,
        name="Plains",
        colors=("W",),
        mana_value=0.0,
        rarity="basic",
        types=("Basic Land — Plains",),
        mana_cost=None,
        set_code="msh",
    )
    return replace(database, cards=cards)


def _pool_state(*, lines: Sequence[str], tmp_path: Path) -> DraftState:
    store = DraftPoolStore(app_dir=tmp_path / "pool")
    store.set_active_account(account_id="acct")
    states = store.consume_all(events=parse_events(lines))
    assert len(states) == 1
    return states[0]


def _replay_output(*, lines: Sequence[str], tmp_path: Path) -> str:
    log_path = tmp_path / "replay-input.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return replay_log_file(logfile=log_path, card_database=_fixture_card_database())


def _watch_output(*, lines: Sequence[str], tmp_path: Path) -> str:
    watcher = PlainLogWatcher(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "watch-app",
        card_database=_fixture_card_database(),
        poll_interval=0.01,
    )
    try:
        return watcher.process_lines(lines=lines)
    finally:
        watcher.close()


def _session_snapshot(*, lines: Sequence[str], tmp_path: Path) -> LiveSessionSnapshot:
    session = LiveSession(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "session-app",
        card_database=_fixture_card_database(),
    )
    return session.process_lines(lines=lines)


def _chosen_lines(*, output: str) -> list[str]:
    return [
        line
        for line in output.splitlines()
        if line.startswith(("Chosen card:", "Chosen cards:"))
    ]


def _events_of(*, lines: Sequence[str], kind: type[DraftEvent]) -> list[DraftEvent]:
    return [event for event in parse_events(lines) if isinstance(event, kind)]


# Variant transforms. Each one takes the base fixture lines and returns new lines.


def _with_thread_prefix(*, lines: Sequence[str]) -> list[str]:
    return [f"{THREAD_PREFIX}{line}" for line in lines]


def _notify_index(*, lines: Sequence[str], pack: int, pick: int) -> int:
    marker = f'"SelfPick":{pick},"SelfPack":{pack},'
    return next(
        index
        for index, line in enumerate(lines)
        if "Draft.Notify" in line and marker in line
    )


def _request_index(*, lines: Sequence[str], pack: int, pick: int) -> int:
    marker = f'\\"Pack\\":{pack},\\"Pick\\":{pick}}}'
    return next(
        index
        for index, line in enumerate(lines)
        if "EventPlayerDraftMakePick {" in line and marker in line
    )


def _without_first_notify(*, lines: Sequence[str]) -> list[str]:
    index = _notify_index(lines=lines, pack=1, pick=1)
    return [*lines[:index], *lines[index + 1 :]]


def _without_submission(*, lines: Sequence[str], pack: int, pick: int) -> list[str]:
    index = _request_index(lines=lines, pack=pack, pick=pick)
    return [*lines[:index], *lines[index + 3 :]]


def _with_failed_response_and_retry(
    *,
    lines: Sequence[str],
    pack: int,
    pick: int,
) -> list[str]:
    index = _request_index(lines=lines, pack=pack, pick=pick)
    request, response_header, response_body = lines[index : index + 3]
    request_id = re.search(r'"id":"([^"]+)"', request)
    assert request_id is not None
    retry_id = f"{request_id.group(1)}-retry"
    return [
        *lines[:index],
        request,
        response_header,
        json.dumps({"IsPickSuccessful": False}),
        request.replace(f'"id":"{request_id.group(1)}"', f'"id":"{retry_id}"'),
        response_header.replace(f"({request_id.group(1)})", f"({retry_id})"),
        response_body,
        *lines[index + 3 :],
    ]


def _with_repeated_notify(*, lines: Sequence[str], pack: int, pick: int) -> list[str]:
    index = _notify_index(lines=lines, pack=pack, pick=pick)
    return [*lines[: index + 1], lines[index], *lines[index + 1 :]]


def _with_truncated_last_line(*, lines: Sequence[str]) -> list[str]:
    last = lines[-1]
    return [*lines[:-1], last[: len(last) // 2]]


def _without_complete_draft(*, lines: Sequence[str]) -> list[str]:
    index = next(
        index for index, line in enumerate(lines) if "==> DraftCompleteDraft" in line
    )
    return list(lines[:index])


# Base fixtures, from ingestion to every output surface.


@pytest.mark.parametrize("case", ALL_CASES)
def test_fixture_ends_with_expected_logical_picks_and_cards(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    state = _pool_state(lines=_fixture_lines(case=case), tmp_path=tmp_path)

    assert state.completed
    assert state.chosen_pick_count == case.logical_picks
    assert state.selected_card_count == TOTAL_CARDS
    assert len(state.pool_grp_ids) == TOTAL_CARDS


@pytest.mark.parametrize("case", ALL_CASES)
def test_fixture_events_offer_every_pick_and_choose_from_the_offered_pack(
    case: FormatCase,
) -> None:
    events = list(parse_events(_fixture_lines(case=case)))
    packs = [event for event in events if isinstance(event, PackOfferedEvent)]
    picks = [event for event in events if isinstance(event, PickMadeEvent)]
    completions = [event for event in events if isinstance(event, DraftCompletedEvent)]

    assert len(packs) == case.logical_picks
    assert len(picks) == case.logical_picks
    for pack, pick in zip(packs, picks, strict=True):
        assert (pack.pack_number, pack.pick_number) == (
            pick.pack_number,
            pick.pick_number,
        )
        assert set(pick.selected_grp_ids) <= set(pack.offered_grp_ids)
        assert len(pick.selected_grp_ids) == case.cards_per_pick
    assert len(completions) == 1


@pytest.mark.parametrize("case", ALL_CASES)
def test_fixture_reaches_draft_complete_in_the_live_session(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    snapshot = _session_snapshot(lines=_fixture_lines(case=case), tmp_path=tmp_path)

    assert snapshot.status.phase == ApplicationPhase.DRAFT_COMPLETE
    assert snapshot.draft is not None
    assert snapshot.draft.completed is True
    assert snapshot.draft.pack_number == 2
    assert snapshot.draft.pick_number == case.picks_per_pack - 1
    assert snapshot.pool.total_cards == TOTAL_CARDS


@pytest.mark.parametrize("case", ALL_CASES)
def test_fixture_replay_output_names_format_and_lists_every_pick(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    output = _replay_output(lines=_fixture_lines(case=case), tmp_path=tmp_path)

    assert f"Format: {case.format_label}" in output
    assert "Quick" not in output
    headings = [line for line in output.splitlines() if line.startswith("Pack ")]
    assert len(headings) == case.logical_picks
    assert headings[0] == "Pack 1 Pick 1"
    assert headings[-1] == f"Pack 3 Pick {case.picks_per_pack}"
    assert len(_chosen_lines(output=output)) == case.logical_picks
    assert f"Draft complete: {TOTAL_CARDS} cards" in output


@pytest.mark.parametrize("case", ALL_CASES)
def test_fixture_plain_watch_output_names_format_and_lists_every_pick(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    output = _watch_output(lines=_fixture_lines(case=case), tmp_path=tmp_path)

    assert f"format {case.format_label}, pick P1P1" in output
    assert "Quick" not in output
    take = ", take 2 cards" if case.cards_per_pick == 2 else ""
    headings = [line for line in output.splitlines() if line.startswith("Pack ")]
    assert len(headings) == case.logical_picks
    assert headings[0] == f"Pack 1 of 3, Pick 1 of {case.picks_per_pack}{take}"
    assert headings[-1] == (
        f"Pack 3 of 3, Pick {case.picks_per_pack} of {case.picks_per_pack}{take}"
    )
    assert len(_chosen_lines(output=output)) == case.logical_picks


def test_pick_two_fixture_lists_both_cards_on_one_chosen_line(tmp_path: Path) -> None:
    lines = _fixture_lines(case=PICK_TWO_CASE)

    cards = "Fixture Spider [G] (grpId 105097), Fixture Blue Card [U] (grpId 105134)"
    assert f"Chosen card: {cards}" in _replay_output(lines=lines, tmp_path=tmp_path)
    assert f"Chosen cards: {cards}" in _watch_output(lines=lines, tmp_path=tmp_path)


# Variants. Each one derives from a base fixture and asserts the safe outcome.


@pytest.mark.parametrize("case", ALL_CASES)
def test_thread_prefix_gives_the_same_replay_and_pool_as_unprefixed_lines(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    lines = _fixture_lines(case=case)
    prefixed = _with_thread_prefix(lines=lines)

    assert _replay_output(lines=prefixed, tmp_path=tmp_path) == _replay_output(
        lines=lines,
        tmp_path=tmp_path,
    )
    assert list(parse_events(prefixed)) == list(parse_events(lines))
    plain_state = _pool_state(lines=lines, tmp_path=tmp_path / "plain")
    prefixed_state = _pool_state(lines=prefixed, tmp_path=tmp_path / "prefixed")
    assert prefixed_state.pool_grp_ids == plain_state.pool_grp_ids
    assert prefixed_state.chosen_pick_count == case.logical_picks
    assert prefixed_state.completed


@pytest.mark.parametrize("case", ALL_CASES)
def test_missing_first_notify_still_records_the_first_pick_and_completes(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    lines = _without_first_notify(lines=_fixture_lines(case=case))

    packs = _events_of(lines=lines, kind=PackOfferedEvent)
    state = _pool_state(lines=lines, tmp_path=tmp_path)

    assert len(packs) == case.logical_picks - 1
    first_pick = state.pick_for(pack_number=0, pick_number=0)
    assert first_pick is not None
    assert len(first_pick.selected_grp_ids) == case.cards_per_pick
    assert state.completed
    assert state.chosen_pick_count == case.logical_picks
    assert len(state.pool_grp_ids) == TOTAL_CARDS


@pytest.mark.parametrize("case", ALL_CASES)
def test_missing_first_notify_still_reaches_draft_complete_in_the_session(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    lines = _without_first_notify(lines=_fixture_lines(case=case))

    snapshot = _session_snapshot(lines=lines, tmp_path=tmp_path)

    assert snapshot.status.phase == ApplicationPhase.DRAFT_COMPLETE
    assert snapshot.pool.total_cards == TOTAL_CARDS


@pytest.mark.parametrize("case", ALL_CASES)
def test_missing_submission_leaves_a_gap_and_completion_does_not_fill_it(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    pack, pick = PICK_COORDINATE
    lines = _without_submission(
        lines=_fixture_lines(case=case),
        pack=pack,
        pick=pick,
    )

    picks = _events_of(lines=lines, kind=PickMadeEvent)
    state = _pool_state(lines=lines, tmp_path=tmp_path)

    assert len(picks) == case.logical_picks - 1
    gap = state.pick_for(pack_number=pack - 1, pick_number=pick - 1)
    assert gap is not None
    assert gap.selected_grp_ids == ()
    assert state.completed
    assert state.chosen_pick_count == case.logical_picks - 1
    assert len(state.pool_grp_ids) == TOTAL_CARDS - case.cards_per_pick


@pytest.mark.parametrize("case", ALL_CASES)
def test_missing_submission_shows_the_pack_but_no_chosen_line_for_the_gap(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    pack, pick = PICK_COORDINATE
    lines = _without_submission(
        lines=_fixture_lines(case=case),
        pack=pack,
        pick=pick,
    )

    replay = _replay_output(lines=lines, tmp_path=tmp_path)
    watch = _watch_output(lines=lines, tmp_path=tmp_path)
    snapshot = _session_snapshot(lines=lines, tmp_path=tmp_path)

    assert f"Pack {pack} Pick {pick}" in replay.splitlines()
    assert len(_chosen_lines(output=replay)) == case.logical_picks - 1
    assert len(_chosen_lines(output=watch)) == case.logical_picks - 1
    assert snapshot.pool.total_cards == TOTAL_CARDS - case.cards_per_pick


@pytest.mark.parametrize("case", ALL_CASES)
def test_failed_pick_response_then_retry_counts_only_the_retry(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    pack, pick = PICK_COORDINATE
    base_lines = _fixture_lines(case=case)
    lines = _with_failed_response_and_retry(lines=base_lines, pack=pack, pick=pick)

    picks = _events_of(lines=lines, kind=PickMadeEvent)
    state = _pool_state(lines=lines, tmp_path=tmp_path / "retry")
    base_state = _pool_state(lines=base_lines, tmp_path=tmp_path / "base")

    assert len(picks) == case.logical_picks
    assert state.completed
    assert state.chosen_pick_count == case.logical_picks
    assert state.pool_grp_ids == base_state.pool_grp_ids
    assert state.pick_for(pack_number=pack - 1, pick_number=pick - 1) == (
        base_state.pick_for(pack_number=pack - 1, pick_number=pick - 1)
    )


@pytest.mark.parametrize("case", ALL_CASES)
def test_failed_pick_response_without_retry_records_no_pick(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    pack, pick = PICK_COORDINATE
    lines = _with_failed_response_and_retry(
        lines=_fixture_lines(case=case),
        pack=pack,
        pick=pick,
    )
    index = _request_index(lines=lines, pack=pack, pick=pick)
    without_retry = [*lines[: index + 3], *lines[index + 6 :]]

    picks = _events_of(lines=without_retry, kind=PickMadeEvent)
    state = _pool_state(lines=without_retry, tmp_path=tmp_path)

    assert len(picks) == case.logical_picks - 1
    assert state.chosen_pick_count == case.logical_picks - 1
    assert len(state.pool_grp_ids) == TOTAL_CARDS - case.cards_per_pick


@pytest.mark.parametrize("case", ALL_CASES)
def test_repeated_notify_yields_one_pack_event_and_one_heading(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    pack, pick = PICK_COORDINATE
    lines = _with_repeated_notify(
        lines=_fixture_lines(case=case),
        pack=pack,
        pick=pick,
    )

    packs = _events_of(lines=lines, kind=PackOfferedEvent)
    replay = _replay_output(lines=lines, tmp_path=tmp_path)
    watch = _watch_output(lines=lines, tmp_path=tmp_path)

    assert len(packs) == case.logical_picks
    assert replay.splitlines().count(f"Pack {pack} Pick {pick}") == 1
    watch_heading = f"Pack {pack} of 3, Pick {pick} of {case.picks_per_pack}"
    assert sum(line.startswith(watch_heading) for line in watch.splitlines()) == 1
    assert len(_chosen_lines(output=replay)) == case.logical_picks
    assert len(_chosen_lines(output=watch)) == case.logical_picks


@pytest.mark.parametrize("case", ALL_CASES)
def test_truncated_last_line_keeps_recorded_picks_and_invents_no_card(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "Player.log"
    lines = _with_truncated_last_line(lines=_fixture_lines(case=case))
    # A log that ends mid-line has no final newline, as when Arena is still writing.
    log_path.write_text("\n".join(lines), encoding="utf-8")
    session = LiveSession(
        log_path=log_path,
        app_dir=tmp_path / "session-app",
        card_database=_fixture_card_database(),
    )
    output = StringIO()

    snapshot = session.poll_once()
    exit_code = run_plain_watch(
        log_path=log_path,
        app_dir=tmp_path / "watch-app",
        card_database=_fixture_card_database(),
        output=output,
        poll_interval=0.01,
        once=True,
    )

    # The follower holds back the partial DraftCompleteDraft response, so no
    # error surfaces. The final pick response already carries
    # IsPickingCompleted, so the draft completes from that signal with the 42
    # recorded cards and nothing invented from the missing CardPool.
    assert exit_code == 0
    assert snapshot.errors == ()
    assert snapshot.status.phase == ApplicationPhase.DRAFT_COMPLETE
    assert snapshot.pool.total_cards == TOTAL_CARDS
    assert len(_chosen_lines(output=output.getvalue())) == case.logical_picks


@pytest.mark.parametrize("case", ALL_CASES)
def test_parser_and_replay_reject_a_truncated_draft_line_with_a_parse_error(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    lines = _with_truncated_last_line(lines=_fixture_lines(case=case))

    # Strict parsing has no partial-line buffer, so it raises DraftLogParseError
    # after it has already emitted every earlier event.
    with pytest.raises(DraftLogParseError):
        _pool_state(lines=lines, tmp_path=tmp_path / "pool")
    with pytest.raises(DraftLogParseError):
        _replay_output(lines=lines, tmp_path=tmp_path)


@pytest.mark.parametrize("case", ALL_CASES)
def test_unanswered_final_pick_request_is_kept_but_does_not_complete_the_draft(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    lines = _without_complete_draft(lines=_fixture_lines(case=case))
    lines = _with_truncated_last_line(lines=lines)

    state = _pool_state(lines=lines, tmp_path=tmp_path)

    # The truncated final response is not JSON, so nothing confirms or completes
    # the draft. The parser still flushes the last request at end of input.
    assert not state.completed
    assert state.chosen_pick_count == case.logical_picks
    assert len(state.pool_grp_ids) == TOTAL_CARDS


@pytest.mark.parametrize("case", ALL_CASES)
def test_completion_without_complete_draft_uses_the_last_pick_response(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    lines = _without_complete_draft(lines=_fixture_lines(case=case))

    completions = _events_of(lines=lines, kind=DraftCompletedEvent)
    state = _pool_state(lines=lines, tmp_path=tmp_path)
    snapshot = _session_snapshot(lines=lines, tmp_path=tmp_path)

    assert len(completions) == 1
    assert state.completed
    assert state.chosen_pick_count == case.logical_picks
    assert len(state.pool_grp_ids) == TOTAL_CARDS
    assert snapshot.status.phase == ApplicationPhase.DRAFT_COMPLETE


@pytest.mark.parametrize("case", ALL_CASES)
def test_completion_with_complete_draft_completes_once(
    case: FormatCase,
    tmp_path: Path,
) -> None:
    lines = _fixture_lines(case=case)

    completions = _events_of(lines=lines, kind=DraftCompletedEvent)
    state = _pool_state(lines=lines, tmp_path=tmp_path)

    assert len(completions) == 1
    assert state.completed
    assert len(state.pool_grp_ids) == TOTAL_CARDS
