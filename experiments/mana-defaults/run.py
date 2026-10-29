#!/usr/bin/env python3
"""Measure the deck builder's land count, main color source and splash symbol defaults.
Reads one 17Lands game data dump and reports what winning decks played.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from draftomen.config import DECK_BUILDER  # noqa: E402
from scripts.measure_deck_rules import (  # noqa: E402
    PERCENTILES,
    DeckRecord,
    build_records,
    load_card_index,
    percent,
    read_decks,
    single_splash,
)

CURVES = ("aggressive", "default", "top-heavy")
LAND_COUNTS = (15, 16, 17, 18, 19)
MANA_VALUE_BUCKETS = (
    ("2.5 or less", 0.0, 2.55),
    ("2.6 to 2.7", 2.55, 2.75),
    ("2.8 to 2.9", 2.75, 2.95),
    ("3.0 to 3.1", 2.95, 3.15),
    ("3.2 to 3.3", 3.15, 3.35),
    ("3.4 or more", 3.35, 99.0),
)


def average_mana_value(*, record: DeckRecord) -> float:
    """Return the average mana value of the deck's spells."""

    values = [card.mana_value for card in record.spells if card.mana_value is not None]
    return sum(values) / len(values) if values else 0.0


def curve_of(*, record: DeckRecord) -> str:
    """Classify a deck's curve with the deck builder's land count rule."""

    values = [card.mana_value for card in record.spells if card.mana_value is not None]
    average = average_mana_value(record=record)
    expensive = sum(
        value >= DECK_BUILDER.expensive_spell_mana_value for value in values
    )
    two_drops = sum(value == DECK_BUILDER.two_drop_mana_value for value in values)
    if (
        average >= DECK_BUILDER.top_heavy_average_mana_value_min
        or expensive > DECK_BUILDER.maximum_expensive_spells
    ):
        return "top-heavy"
    if (
        average <= DECK_BUILDER.aggressive_average_mana_value_max
        and two_drops >= DECK_BUILDER.minimum_two_drops
    ):
        return "aggressive"
    return "default"


def percentiles(*, values: list[float]) -> str:
    """Format the reported percentiles of a list of values."""

    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return " | ".join(f"{cuts[point - 1]:.0f}" for point in PERCENTILES)


def land_row(*, label: str, group: list[DeckRecord]) -> str:
    """Format one land count distribution row."""

    counts = Counter(record.lands for record in group)
    median = statistics.median(record.lands for record in group) if group else 0
    return (
        f"| {label} | {len(group)} | {median:.0f} | "
        + " | ".join(percent(part=counts[lands], whole=len(group)) for lands in LAND_COUNTS)
        + " |"
    )


def main() -> int:
    """Run the measurement for one game data dump and print markdown tables."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--set-code", required=True)
    parser.add_argument("--app-dir", type=Path, default=None)
    args = parser.parse_args()
    if not args.game_data.is_file():
        parser.error(f"file not found: {args.game_data}")

    event, decks = read_decks(path=args.game_data)
    index = load_card_index(set_code=args.set_code.upper(), app_dir=args.app_dir)
    unmatched: Counter[str] = Counter()
    records = build_records(decks=decks, index=index, unmatched=unmatched)
    if unmatched:
        print(f"unmatched card names: {unmatched.most_common(8)}", file=sys.stderr)
    winning = [record for record in records if record.winning]
    two_color = [
        record for record in winning if record.kind in ("2 colors", "2 + splash")
    ]
    three_color = [record for record in winning if record.kind == "3 colors"]

    print(f"## {args.set_code.upper()} {event}\n")
    print(
        f"{len(winning)} winning decks, {len(two_color)} with two main colors, "
        f"{len(three_color)} with three.\n"
    )

    print("### Land count by the deck builder's curve rule\n")
    header = " | ".join(f"{lands} lands" for lands in LAND_COUNTS)
    print(f"| Curve | Decks | Median | {header} |")
    print("| --- | --- | --- | " + " | ".join("---" for _ in LAND_COUNTS) + " |")
    for curve in CURVES:
        print(
            land_row(
                label=curve,
                group=[record for record in two_color if curve_of(record=record) == curve],
            )
        )
    print()

    print("### Land count by average mana value\n")
    print(f"| Average MV | Decks | Median | {header} |")
    print("| --- | --- | --- | " + " | ".join("---" for _ in LAND_COUNTS) + " |")
    for label, low, high in MANA_VALUE_BUCKETS:
        print(
            land_row(
                label=label,
                group=[
                    record
                    for record in two_color
                    if low <= average_mana_value(record=record) < high
                ],
            )
        )
    print()

    floor = DECK_BUILDER.main_color_source_floor
    print("### Land sources of the lightest main color\n")
    points = " | ".join(f"p{point}" for point in PERCENTILES)
    print(f"| Decks | n | {points} | {floor} or more |")
    print("| --- | --- | " + " | ".join("---" for _ in PERCENTILES) + " | --- |")
    for label, group in (("two main colors", two_color), ("three main colors", three_color)):
        lightest = [
            min(record.sources[color] for color in record.deck.main_colors)
            for record in group
        ]
        print(
            f"| {label} | {len(group)} | {percentiles(values=lightest)} | "
            f"{percent(part=sum(value >= floor for value in lightest), whole=len(lightest))} |"
        )
    print()

    splash_decks = single_splash(records=winning)
    symbols = Counter(min(count, 3) for record in splash_decks for _, count in record.splash_cards)
    total = sum(symbols.values())
    over_limit = sum(
        any(count > 2 for _, count in record.splash_cards) for record in splash_decks
    )
    print(f"### Splash symbols per splashed card (n={total})\n")
    print("| Symbols of the splash color | Share |")
    print("| --- | --- |")
    for count, label in ((1, "1"), (2, "2"), (3, "3 or more")):
        print(f"| {label} | {percent(part=symbols[count], whole=total)} |")
    print(
        f"\n{over_limit} of {len(splash_decks)} single-splash decks played a card "
        "with 3 or more splash symbols.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
