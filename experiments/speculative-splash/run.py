#!/usr/bin/env python3
"""Measure when winning drafters took the cards they splashed, and what happened to off-color picks.
Joins a 17Lands draft data dump with the game data dump of the same set and event.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from draftomen.card_data_client import CardDataClient  # noqa: E402
from draftomen.config import PICK_ENGINE, SPLASH  # noqa: E402
from draftomen.pickengine import _inferred_pair, _pool_color_weights  # noqa: E402
from draftomen.seventeen import GRADE_LABELS  # noqa: E402
from draftomen.splash import card_is_castable_in_pair, splash_requirement  # noqa: E402
from scripts.measure_deck_rules import (  # noqa: E402
    WIN_CUTOFFS,
    CardIndex,
    Deck,
    is_land,
    load_ratings,
    pips,
)

PHASES = ("open", "ramp", "locked")
OUTCOMES = ("splashed", "main color", "sideboard")
GRADE_GROUPS = ("A- or better", "B- to B+", "C+ or lower", "ungraded")


@dataclass(slots=True)
class Pick:
    """One card a drafter took, with its place in the draft."""

    name: str
    pick_index: int
    pack: int


@dataclass(slots=True)
class OffColorPick:
    """A splashable card outside the drafter's pair when it was taken."""

    phase: str
    grade_group: str
    outcome: str


@dataclass(slots=True)
class SplashedPick:
    """A card a winning deck splashed, with when it was taken."""

    pick_index: int
    pack: int
    phase: str
    grade: str | None
    off_pair: bool | None


def phase_of(*, pick_index: int) -> str:
    """Return the pick engine phase for a 1-based card pick index."""

    if pick_index <= PICK_ENGINE.open_pick_count:
        return "open"
    if pick_index < PICK_ENGINE.locked_pick_index:
        return "ramp"
    return "locked"


def grade_group(*, grade: str | None) -> str:
    """Bucket a letter grade into one of the reported grade groups."""

    if grade not in GRADE_LABELS:
        return "ungraded"
    position = GRADE_LABELS.index(grade)
    if position >= GRADE_LABELS.index("A-"):
        return "A- or better"
    if position >= GRADE_LABELS.index("B-"):
        return "B- to B+"
    return "C+ or lower"


def read_picks(*, path: Path, cutoff: int) -> dict[str, list[Pick]]:
    """Read the picks of drafts that reached the win cutoff, in pick order.
    Pick Two rows hold two cards; each card gets its own pick index.
    """

    rows: dict[str, list[tuple[int, int, str, str]]] = {}
    with gzip.open(path, mode="rt", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        index = {name: position for position, name in enumerate(header)}
        second = index.get("pick_2")
        for row in reader:
            if int(row[index["event_match_wins"]] or 0) < cutoff:
                continue
            rows.setdefault(row[index["draft_id"]], []).append(
                (
                    int(row[index["pack_number"]]),
                    int(row[index["pick_number"]]),
                    row[index["pick"]],
                    row[second] if second is not None else "",
                )
            )
    picks: dict[str, list[Pick]] = {}
    for draft_id, draft_rows in rows.items():
        ordered: list[Pick] = []
        for pack, _, first, extra in sorted(draft_rows):
            for name in (first, extra):
                if name:
                    ordered.append(
                        Pick(name=name, pick_index=len(ordered) + 1, pack=pack + 1)
                    )
        picks[draft_id] = ordered
    return picks


def read_decks_by_id(*, path: Path) -> dict[str, Deck]:
    """Read the latest deck build of each draft in a game data dump.
    Wins come from the draft data, so games and wins stay at zero here.
    """

    decks: dict[str, Deck] = {}
    with gzip.open(path, mode="rt", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        index = {name: position for position, name in enumerate(header)}
        deck_columns = [
            (position, name[len("deck_") :])
            for position, name in enumerate(header)
            if name.startswith("deck_")
        ]
        for row in reader:
            draft_id = row[index["draft_id"]]
            build = int(row[index["build_index"]] or 0)
            deck = decks.get(draft_id)
            if deck is not None and deck.build >= build:
                continue
            decks[draft_id] = Deck(
                event=row[index["event_type"]],
                build=build,
                main_colors=row[index["main_colors"]],
                splash_colors=row[index["splash_colors"]],
                cards={
                    name: int(row[position])
                    for position, name in deck_columns
                    if row[position] not in ("", "0")
                },
            )
    if not decks:
        raise SystemExit(f"error: {path} has no game rows")
    return decks


def percent(*, part: int, whole: int) -> str:
    """Format a share as a whole percentage, or a dash when empty."""

    return f"{100 * part / whole:.0f}%" if whole else "-"


def main() -> int:
    """Run the measurement for one event and print markdown tables."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft-data", type=Path, required=True)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--set-code", required=True)
    parser.add_argument("--event", choices=tuple(WIN_CUTOFFS), required=True)
    parser.add_argument("--ratings-format", default="PremierDraft")
    parser.add_argument("--app-dir", type=Path, default=None)
    args = parser.parse_args()
    for path in (args.draft_data, args.game_data):
        if not path.is_file():
            parser.error(f"file not found: {path}")

    set_code = args.set_code.upper()
    cutoff = WIN_CUTOFFS[args.event]
    database = CardDataClient(app_dir=args.app_dir).load(set_code, allow_network=False)
    card_index = CardIndex.build(cards=list(database.cards.values()))
    ratings = load_ratings(
        set_code=set_code,
        event=args.ratings_format,
        app_dir=args.app_dir,
    )
    event = args.event
    decks_by_id = read_decks_by_id(path=args.game_data)
    picks_by_draft = read_picks(path=args.draft_data, cutoff=cutoff)

    unmatched: Counter[str] = Counter()
    splashed: list[SplashedPick] = []
    off_color: list[OffColorPick] = []
    joined = 0
    splash_decks = 0
    for draft_id, picks in picks_by_draft.items():
        deck = decks_by_id.get(draft_id)
        if deck is None:
            continue
        joined += 1
        single_splash = len(deck.main_colors) == 2 and len(deck.splash_colors) == 1
        splash_decks += single_splash
        remaining = Counter(deck.cards)
        pool: list[int] = []
        for pick in picks:
            card = card_index.find(name=pick.name)
            in_deck = remaining[pick.name] > 0
            if in_deck:
                remaining[pick.name] -= 1
            if card is None:
                unmatched[pick.name] += 1
                continue
            phase = phase_of(pick_index=pick.pick_index)
            pair = None
            if phase != "open":
                weights = _pool_color_weights(
                    pool_grp_ids=tuple(pool),
                    card_database=database,
                    ratings_data=ratings,
                    config=PICK_ENGINE,
                )
                pair = _inferred_pair(weights=weights, config=PICK_ENGINE)
            pool.append(card.grp_id)
            if is_land(card=card) or not card.colors:
                continue
            grade = ratings.rating_for(grp_id=card.grp_id).letter_grade
            off_pair = (
                None
                if pair is None
                else not card_is_castable_in_pair(card=card, base_pair=pair)
            )
            card_pips = pips(card=card)
            if (
                single_splash
                and in_deck
                and card_pips[deck.splash_colors]
            ):
                splashed.append(
                    SplashedPick(
                        pick_index=pick.pick_index,
                        pack=pick.pack,
                        phase=phase,
                        grade=grade,
                        off_pair=off_pair,
                    )
                )
            if not off_pair or pair is None:
                continue
            splash_color, splash_pips = splash_requirement(card=card, base_pair=pair)
            if splash_color is None or splash_pips > SPLASH.maximum_off_color_pips:
                continue
            if not in_deck:
                outcome = "sideboard"
            elif splash_color in deck.main_colors:
                outcome = "main color"
            elif splash_color in deck.splash_colors:
                outcome = "splashed"
            else:
                outcome = "other"
            off_color.append(
                OffColorPick(
                    phase=phase,
                    grade_group=grade_group(grade=grade),
                    outcome=outcome,
                )
            )

    if not joined:
        parser.error("no winning draft in the draft data has a deck in the game data")
    if unmatched:
        print(f"unmatched card names: {unmatched.most_common(8)}", file=sys.stderr)

    print(f"## {set_code} {event}\n")
    print(
        f"{len(picks_by_draft)} drafts with {cutoff}+ wins, {joined} with a deck in the "
        f"game data, {splash_decks} of them two colors plus one splash color.\n"
    )
    print_splashed(splashed=splashed)
    print_off_color(off_color=off_color)
    return 0


def print_splashed(*, splashed: list[SplashedPick]) -> None:
    """Print when splashed cards were taken and their grades."""

    total = len(splashed)
    print(f"### Splashed cards (n={total})\n")
    print(
        f"Median pick: {statistics.median(p.pick_index for p in splashed):.0f} of 42.\n"
    )
    print("| Taken in | Share | Off the pair at pick time | A- or better |")
    print("| --- | --- | --- | --- |")
    for label, group in (
        *(
            (f"pack {pack}", [p for p in splashed if p.pack == pack])
            for pack in (1, 2, 3)
        ),
        *(
            (f"{phase} picks", [p for p in splashed if p.phase == phase])
            for phase in PHASES
        ),
    ):
        judged = [p for p in group if p.off_pair is not None]
        print(
            f"| {label} | {percent(part=len(group), whole=total)} | "
            f"{percent(part=sum(p.off_pair for p in judged), whole=len(judged))} | "
            f"{percent(part=sum(grade_group(grade=p.grade) == 'A- or better' for p in group), whole=len(group))} |"
        )
    print()
    grades = Counter(p.grade for p in splashed)
    order = [*reversed(GRADE_LABELS), None]
    print("| Grade | Share |")
    print("| --- | --- |")
    for grade in order:
        if grades[grade]:
            print(
                f"| {grade or 'ungraded'} | {percent(part=grades[grade], whole=total)} |"
            )
    print()


def print_off_color(*, off_color: list[OffColorPick]) -> None:
    """Print what happened to splashable picks outside the pair at pick time."""

    print(f"### Splashable picks outside the pair at pick time (n={len(off_color)})\n")
    print("| Phase | Grade | Picks | Splashed | Main color | Sideboard |")
    print("| --- | --- | --- | --- | --- | --- |")
    for phase in ("ramp", "locked"):
        for group in (*GRADE_GROUPS, "all"):
            rows = [
                p
                for p in off_color
                if p.phase == phase and (group == "all" or p.grade_group == group)
            ]
            if not rows:
                continue
            counts = Counter(p.outcome for p in rows)
            print(
                f"| {phase} | {group} | {len(rows)} | "
                + " | ".join(
                    percent(part=counts[outcome], whole=len(rows))
                    for outcome in OUTCOMES
                )
                + " |"
            )
    others = sum(p.outcome == "other" for p in off_color)
    if others:
        print(f"\n{others} picks went into a deck that labels their color neither main nor splash.")
    print()


if __name__ == "__main__":
    raise SystemExit(main())

