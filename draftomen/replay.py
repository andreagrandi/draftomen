"""Deterministic plain-text replay rendering.
Completion events append the same build sheet used by live plain watch.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import TypeAlias

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.deckbuilder import BuildPool, build_deck_from_pool, format_build_result
from draftomen.events import (
    EXPECTED_PICKS_PER_PACK,
    EXPECTED_TOTAL_PICKS,
    AccountEvent,
    DraftCompletedEvent,
    DraftEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
    parse_events,
)
from draftomen.pickengine import (
    PickEngine,
    ScoredCard,
    ScoredPack,
    render_pick_rationale_detailed,
)
from draftomen.pool import DraftPoolStore
from draftomen.set_profile import SetProfile
from draftomen.seventeen import SEVENTEEN_LANDS_ATTRIBUTION, SeventeenLandsData

PathInput: TypeAlias = str | PathLike[str]
RatingsLoader: TypeAlias = Callable[[str], SeventeenLandsData]
ProfileLoader: TypeAlias = Callable[[str], SetProfile | None]


class ReplayError(RuntimeError):
    """Raised when a log cannot be replayed.
    Callers should surface the message as a concise CLI diagnostic.
    """


@dataclass(frozen=True, slots=True)
class _ReplayHeader:
    """Summary fields printed before replayed picks.
    Missing fields are rendered explicitly so output stays deterministic.
    """

    account_id: str | None
    screen_name: str | None
    event_name: str | None
    set_code: str | None
    draft_id: str | None


def replay_log_file(
    *,
    logfile: PathInput,
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    ratings_loader: RatingsLoader | None = None,
    profile_loader: ProfileLoader | None = None,
    splash_enabled: bool = True,
    set_profile: SetProfile | None = None,
) -> str:
    """Replay one captured Player.log file into deterministic text.
    Ratings and profiles are caller-supplied or loaded once from parsed set code.
    """

    path = Path(logfile)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ReplayError(f"Could not read replay log {path}: {error}.") from error

    events = tuple(parse_events(lines=lines))
    return render_replay_events(
        events=events,
        card_database=card_database,
        ratings_data=ratings_data,
        ratings_loader=ratings_loader,
        profile_loader=profile_loader,
        splash_enabled=splash_enabled,
        set_profile=set_profile,
    )


def render_replay_events(
    *,
    events: Iterable[DraftEvent],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    ratings_loader: RatingsLoader | None = None,
    profile_loader: ProfileLoader | None = None,
    splash_enabled: bool = True,
    set_profile: SetProfile | None = None,
) -> str:
    """Render parsed events to stable plain-text replay output.
    Pool validation is run first so conflicting streams fail before printing.
    """

    event_tuple = tuple(events)
    if not event_tuple:
        raise ReplayError("No Quick Draft events found in log file.")

    _validate_events_with_pool(events=event_tuple)

    header = _header_from_events(events=event_tuple)
    loaded_ratings = _ratings_data_for_replay(
        header=header,
        ratings_data=ratings_data,
        ratings_loader=ratings_loader,
    )
    loaded_profile = _set_profile_for_replay(
        header=header,
        set_profile=set_profile,
        profile_loader=profile_loader,
    )
    pick_engine = PickEngine(
        ratings_data=loaded_ratings,
        splash_enabled=splash_enabled,
        set_profile=loaded_profile,
    )
    lines = _format_header(header=header)
    lines.append("")

    for event in event_tuple:
        if isinstance(event, PackOfferedEvent):
            lines.extend(
                format_pack_offered_event(
                    event=event,
                    card_database=card_database,
                    pick_engine=pick_engine,
                )
            )
        elif isinstance(event, PickMadeEvent):
            lines.extend(
                format_pick_made_event(
                    event=event,
                    card_database=card_database,
                )
            )
            lines.append("")
        elif isinstance(event, DraftCompletedEvent):
            lines.extend(format_draft_completed_event(event=event))
            lines.append("")
            lines.extend(
                _format_completed_build_sheet(
                    event=event,
                    header=header,
                    card_database=card_database,
                    ratings_data=loaded_ratings,
                    splash_enabled=splash_enabled,
                    set_profile=loaded_profile,
                )
            )

    return "\n".join(lines).rstrip() + "\n"


def _validate_events_with_pool(*, events: tuple[DraftEvent, ...]) -> None:
    with tempfile.TemporaryDirectory(prefix="draftomen-replay-") as temporary_dir:
        store = DraftPoolStore(app_dir=temporary_dir)
        store.consume_all(events=events)


def _ratings_data_for_replay(
    *,
    header: _ReplayHeader,
    ratings_data: SeventeenLandsData | None,
    ratings_loader: RatingsLoader | None,
) -> SeventeenLandsData | None:
    if ratings_data is not None or ratings_loader is None:
        return ratings_data

    if header.set_code is None:
        return None

    return ratings_loader(header.set_code)


def _set_profile_for_replay(
    *,
    header: _ReplayHeader,
    set_profile: SetProfile | None,
    profile_loader: ProfileLoader | None,
) -> SetProfile | None:
    if set_profile is not None or profile_loader is None:
        return set_profile

    if header.set_code is None:
        return None

    return profile_loader(header.set_code)


def _format_completed_build_sheet(
    *,
    event: DraftCompletedEvent,
    header: _ReplayHeader,
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None,
    splash_enabled: bool,
    set_profile: SetProfile | None,
) -> list[str]:
    pool = BuildPool(
        set_code=event.set_code,
        pool_grp_ids=event.picked_grp_ids,
        source_label=f"replay {header.draft_id or event.event_name}",
        account_id=header.account_id,
        draft_id=header.draft_id,
    )
    selection, build_sheet = build_deck_from_pool(
        pool=pool,
        card_database=card_database,
        ratings_data=ratings_data,
        allow_splash=splash_enabled,
        set_profile=set_profile,
    )
    return format_build_result(
        pool=pool,
        selection=selection,
        spell_selection=build_sheet.spell_selection,
        mana_base=build_sheet.mana_base,
    ).rstrip("\n").splitlines()


def _header_from_events(*, events: tuple[DraftEvent, ...]) -> _ReplayHeader:
    account_names: dict[str, str | None] = {}
    active_account_id: str | None = None
    event_name: str | None = None
    set_code: str | None = None
    draft_id: str | None = None

    for event in events:
        if isinstance(event, AccountEvent):
            active_account_id = event.client_id
            account_names[event.client_id] = event.screen_name
            continue

        if isinstance(event, DraftStartedEvent):
            active_account_id = event.account_id or active_account_id
            event_name = event.event_name
            set_code = event.set_code
            draft_id = event.course_id
            break

        if isinstance(event, (PackOfferedEvent, PickMadeEvent, DraftCompletedEvent)):
            active_account_id = event.account_id or active_account_id
            event_name = event.event_name
            set_code = event.set_code
            draft_id = event.event_name
            break

    screen_name = None
    if active_account_id is not None:
        screen_name = account_names.get(active_account_id)

    return _ReplayHeader(
        account_id=active_account_id,
        screen_name=screen_name,
        event_name=event_name,
        set_code=set_code,
        draft_id=draft_id,
    )


def format_pack_offered_event(
    *,
    event: PackOfferedEvent,
    card_database: CardDatabase,
    pick_engine: PickEngine | None = None,
    scored_pack: ScoredPack | None = None,
) -> list[str]:
    """Format a pack offer with the same plain text replay uses."""

    if scored_pack is None:
        engine = pick_engine if pick_engine is not None else PickEngine()
        pick_index = _draft_pick_index(event=event)
        scored_pack = engine.score_pack(
            offered_grp_ids=event.offered_grp_ids,
            card_database=card_database,
            pool_grp_ids=event.pool_grp_ids,
            pick_index=pick_index,
            pack_number=event.pack_number,
            pick_number=event.pick_number,
            global_pick_index=pick_index,
            estimated_remaining_picks=max(0, EXPECTED_TOTAL_PICKS - pick_index),
        )
    lines = format_ranked_pack(
        event=event,
        card_database=card_database,
        scored_pack=scored_pack,
    )
    if scored_pack.cards:
        lines.append(
            "Recommendation: "
            + render_pick_rationale_detailed(
                scored_card=scored_pack.cards[0],
            )
        )
    return lines


def format_ranked_pack(
    *,
    event: PackOfferedEvent,
    card_database: CardDatabase,
    scored_pack: ScoredPack,
    recommendation_line: str | None = None,
    confidence_summary: str | None = None,
    comparison_summary: str | None = None,
) -> list[str]:
    """Format a scored pack's ranked cards for plain text output."""

    lines = [
        f"Pack {event.pack_number + 1} Pick {event.pick_number + 1}",
        _format_pack_status(scored_pack=scored_pack),
        f"Data source: {scored_pack.source_summary}",
    ]
    unresolved_count = len(
        card_database.unresolved_grp_ids(
            grp_ids=(*event.offered_grp_ids, *event.pool_grp_ids),
        )
    )
    if unresolved_count > 0:
        lines.append(f"Warning: {unresolved_count} unresolved card metadata")

    lines.append("Offered cards:")
    lines.extend(_format_scored_cards(cards=scored_pack.cards))
    if recommendation_line is not None:
        lines.append(recommendation_line)
    if confidence_summary is not None:
        lines.append(f"  Confidence: {confidence_summary}")
    if comparison_summary is not None:
        lines.append(f"  {comparison_summary}")
    if any(card.no_data for card in scored_pack.cards):
        lines.append("  * Prior uses neutral prior adjusted by ALSA when available.")
    return lines

def format_pick_made_event(
    *,
    event: PickMadeEvent,
    card_database: CardDatabase,
) -> list[str]:
    """Format a chosen-card event with replay-compatible text.
    The caller decides whether to add a separating blank line.
    """

    return [
        "Chosen card: "
        f"{format_card_info(card_database.lookup(grp_id=event.chosen_grp_id))}"
    ]


def format_draft_completed_event(*, event: DraftCompletedEvent) -> list[str]:
    """Format draft completion with replay-compatible text.
    Completion type records whether Arena emitted an explicit status.
    """

    completion_type = "inferred" if event.inferred else "explicit"
    return [
        "Draft complete: "
        f"{len(event.picked_grp_ids)} cards ({completion_type} completion)"
    ]


def format_card_info(card: CardInfo) -> str:
    """Format one card for plain CLI output.
    Unknown cards are displayed explicitly instead of failing lookups.
    """

    return _format_card(card)


def _format_header(*, header: _ReplayHeader) -> list[str]:
    return [
        "Draft Omen replay",
        f"Account: {_format_account(header=header)}",
        f"Set: {header.set_code or 'unknown'}",
        f"Event: {header.event_name or 'unknown'}",
        f"Draft: {header.draft_id or 'unknown'}",
        f"Attribution: {SEVENTEEN_LANDS_ATTRIBUTION}",
    ]


def _format_account(*, header: _ReplayHeader) -> str:
    if header.account_id is None:
        return "unknown"

    if header.screen_name is None:
        return header.account_id

    return f"{header.screen_name} ({header.account_id})"


def _draft_pick_index(*, event: PackOfferedEvent) -> int:
    return (event.pack_number * EXPECTED_PICKS_PER_PACK) + event.pick_number + 1


def _format_pack_status(*, scored_pack: ScoredPack) -> str:
    commitment = scored_pack.commitment
    pair = commitment.inferred_pair if commitment.inferred_pair is not None else "open"
    percent = int(round(commitment.level * 100))
    status = (
        "Status: "
        f"inferred pair {pair}, "
        f"commitment {percent}% ({commitment.phase}), "
        f"pool {commitment.pool_size}"
    )
    splash_status = _format_splash_status(scored_pack=scored_pack)
    if splash_status is None:
        return status

    return f"{status}, {splash_status}"


def _format_splash_status(*, scored_pack: ScoredPack) -> str | None:
    state = scored_pack.splash_state
    if not state.enabled:
        return "splash disabled"
    if state.active_color is None:
        return None

    fixing_sources = state.fixing_for(color=state.active_color)
    return (
        f"splash {state.active_color} "
        f"{state.picked_card_count}/2 cards, "
        f"{fixing_sources} drafted fixing"
    )


def _format_scored_cards(*, cards: tuple[ScoredCard, ...]) -> list[str]:
    if not cards:
        return []

    card_width = max(len(_format_scored_card_name(card)) for card in cards)
    fit_width = (
        9
        if any(card.color_fit.startswith("splash-") for card in cards)
        else 5
    )
    lines = [
        "  #   Score  "
        f"{'Card':<{card_width}}  "
        f"Colors     {'Fit':<{fit_width}}  GIH WR   ALSA    MV  Source"
    ]
    for rank, scored_card in enumerate(cards, start=1):
        lines.append(
            "  "
            f"{rank:02d}  "
            f"{scored_card.score:>5}  "
            f"{_format_scored_card_name(scored_card):<{card_width}}  "
            f"{_format_card_colors(scored_card.card):<9}  "
            f"{_format_color_fit(scored_card):<{fit_width}}  "
            f"{_format_win_rate(scored_card):>6}  "
            f"{_format_alsa(scored_card):>5}  "
            f"{_format_mana_value(scored_card.card):>4}  "
            f"{scored_card.source_label}"
        )

    return lines


def _format_scored_card_name(card: ScoredCard) -> str:
    return f"{card.card.name} (grpId {card.card.grp_id})"


def _format_win_rate(card: ScoredCard) -> str:
    if card.rating.gih_win_rate is None:
        return "—"

    return f"{card.rating.gih_win_rate:.1%}"


def _format_alsa(card: ScoredCard) -> str:
    if card.rating.average_last_seen_at is None:
        return "—"

    return f"{card.rating.average_last_seen_at:.2f}"


def _format_color_fit(card: ScoredCard) -> str:
    if card.color_fit == "on-color":
        return "On"

    if card.color_fit == "off-color":
        return "Off!"

    if card.color_fit == "colorless":
        return "Any"

    if card.color_fit == "unknown":
        return "?"

    if card.color_fit == "splash-ready":
        return f"Splash {card.splash.splash_color}"

    if card.color_fit == "splash-speculative":
        return f"Splash?{card.splash.splash_color}"

    if card.color_fit == "splash-fixer":
        return f"Fix {card.splash.splash_color}"

    return "Open"


def _format_mana_value(card: CardInfo) -> str:
    if card.mana_value is None:
        return "—"

    if card.mana_value.is_integer():
        return str(int(card.mana_value))

    return f"{card.mana_value:.1f}"


def _format_card_colors(card: CardInfo) -> str:
    if card.unknown:
        return "Unknown"

    return "".join(card.colors) if card.colors else "Colorless"


def _format_card(card: CardInfo) -> str:
    if card.unknown:
        colors = "Unknown"
    else:
        colors = "".join(card.colors) if card.colors else "Colorless"

    return f"{card.name} [{colors}] (grpId {card.grp_id})"
