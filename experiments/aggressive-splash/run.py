#!/usr/bin/env python3
"""Measure how often aggressive and other winning decks splash, and the grades they splash.
Classifies each two-color deck with the same aggressive test the splash code applies to a pool.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from draftomen.card_data_client import CardDataClient  # noqa: E402
from draftomen.config import SPLASH  # noqa: E402
from draftomen.seventeen import GRADE_LABELS  # noqa: E402
from draftomen.splash import _pool_is_aggressive, grade_at_least  # noqa: E402
from scripts.measure_deck_rules import (  # noqa: E402
    WIN_CUTOFFS,
    CardIndex,
    is_land,
    load_ratings,
    pips,
    read_decks,
)

SPEEDS = ("aggressive", "other")


@dataclass(slots=True)
class SpeedGroup:
    """Splash counts for the winning decks of one speed."""

    decks: int = 0
    splash_decks: int = 0
    gate_decks: int = 0
    splash_cards: int = 0
    grades: Counter[str | None] = field(default_factory=Counter)


def percent(*, part: int, whole: int) -> str:
    """Format a share as a whole percentage, or a dash when empty."""

    return f"{100 * part / whole:.0f}%" if whole else "-"


def main() -> int:
    """Run the measurement for one game data dump and print markdown tables."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--set-code", required=True)
    parser.add_argument("--ratings-format", default="PremierDraft")
    parser.add_argument("--app-dir", type=Path, default=None)
    args = parser.parse_args()
    if not args.game_data.is_file():
        parser.error(f"file not found: {args.game_data}")

    set_code = args.set_code.upper()
    database = CardDataClient(app_dir=args.app_dir).load(set_code, allow_network=False)
    card_index = CardIndex.build(cards=list(database.cards.values()))
    ratings = load_ratings(
        set_code=set_code,
        event=args.ratings_format,
        app_dir=args.app_dir,
    )
    event, decks = read_decks(path=args.game_data)
    cutoff = WIN_CUTOFFS[event]

    groups = {speed: SpeedGroup() for speed in SPEEDS}
    unmatched: Counter[str] = Counter()
    winning = 0
    for deck in decks:
        if deck.wins < cutoff:
            continue
        winning += 1
        if len(deck.main_colors) != 2:
            continue
        grp_ids: list[int] = []
        splashed: list[str | None] = []
        for name, copies in deck.cards.items():
            card = card_index.find(name=name)
            if card is None:
                unmatched[name] += copies
                continue
            grp_ids.extend([card.grp_id] * copies)
            if is_land(card=card) or not deck.splash_colors:
                continue
            card_pips = pips(card=card)
            if any(card_pips[color] for color in deck.splash_colors):
                grade = ratings.rating_for(grp_id=card.grp_id).letter_grade
                splashed.extend([grade] * copies)
        aggressive = _pool_is_aggressive(
            pool_grp_ids=tuple(grp_ids),
            card_database=database,
            base_pair=deck.main_colors,
            config=SPLASH,
        )
        group = groups["aggressive" if aggressive else "other"]
        group.decks += 1
        if not splashed:
            continue
        group.splash_decks += 1
        group.splash_cards += len(splashed)
        group.grades.update(splashed)
        group.gate_decks += all(
            grade_at_least(grade=grade, minimum=SPLASH.aggressive_minimum_grade)
            for grade in splashed
        )

    if unmatched:
        print(f"unmatched card names: {unmatched.most_common(8)}", file=sys.stderr)

    print(f"## {set_code} {event}\n")
    print(
        f"{winning} decks with {cutoff}+ wins, "
        f"{sum(group.decks for group in groups.values())} with two main colors.\n"
    )
    print_splash_rate(groups=groups)
    print_grades(groups=groups)
    return 0


def print_splash_rate(*, groups: dict[str, SpeedGroup]) -> None:
    """Print how often each speed splashes and how many splashes pass the A gate."""

    gate = SPLASH.aggressive_minimum_grade
    print("### Splash rate\n")
    print(
        "| Speed | Decks | Splash decks | Splash rate | Splash cards per splash deck "
        f"| Splash decks with only {gate} or better |"
    )
    print("| --- | --- | --- | --- | --- | --- |")
    for speed, group in groups.items():
        per_deck = (
            f"{group.splash_cards / group.splash_decks:.1f}"
            if group.splash_decks
            else "-"
        )
        print(
            f"| {speed} | {group.decks} | {group.splash_decks} | "
            f"{percent(part=group.splash_decks, whole=group.decks)} | {per_deck} | "
            f"{percent(part=group.gate_decks, whole=group.splash_decks)} |"
        )
    print()


def print_grades(*, groups: dict[str, SpeedGroup]) -> None:
    """Print the grade shares of splashed card copies for each speed."""

    print("### Grades of splashed cards\n")
    print(
        "| Grade | "
        + " | ".join(f"{speed} (n={groups[speed].splash_cards})" for speed in SPEEDS)
        + " |"
    )
    print("| --- | " + " | ".join("---" for _ in SPEEDS) + " |")
    for grade in (*reversed(GRADE_LABELS), None):
        if not any(groups[speed].grades[grade] for speed in SPEEDS):
            continue
        print(
            f"| {grade or 'ungraded'} | "
            + " | ".join(
                percent(
                    part=groups[speed].grades[grade],
                    whole=groups[speed].splash_cards,
                )
                for speed in SPEEDS
            )
            + " |"
        )
    print()


if __name__ == "__main__":
    raise SystemExit(main())
