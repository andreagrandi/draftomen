"""Measure color and mana stats of winning 17Lands decks from local files.
Run it against a public game data dump; it never opens a network connection.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import re
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from draftomen.card_data_client import CardDataClient, CardDataClientError
from draftomen.carddb import CardInfo
from draftomen.seventeen import GRADE_LABELS, load_cached_17lands_data
from draftomen.splash import land_mana_colors

COLORS = "WUBRG"
WIN_CUTOFFS: dict[str, int] = {"PremierDraft": 7, "PickTwoDraft": 4}
MIN_GAMES = 3
MIN_LANDS = 15
MAX_LANDS = 19
MAX_FIXING_BUCKET = 6
MAX_NONLAND_FIXER_BUCKET = 4
MAX_DUAL_BUCKET = 4
MAX_SPLASH_CARD_BUCKET = 5
PERCENTILES = (10, 25, 50, 75, 90)
KIND_LABELS = ("2 colors", "2 + splash", "3 colors", "other")
FIXER_KINDS = ("land search", "mana creature", "mana rock", "Treasure")
CONFIDENCE_Z = 1.96
MIN_MAIN_SPELLS = 10
GAP_PERCENTILES = (5, 10, 15, 20, 25, 50)
SPLASH_GAP_FLOOR = -0.015

_SYMBOL = re.compile(r"\{([^}]+)\}")
# Two-color or any-color mana, Treasure, and basic land search, which also
# covers basic landcycling text.
FIXER_PATTERN = re.compile(
    r"Add \{[WUBRG]\} or \{[WUBRG]\}"
    r"|Add one mana of any color"
    r"|mana of any color"
    r"|Treasure token"
    r"|Search your library for a basic land",
    re.IGNORECASE,
)
_LAND_SEARCH = re.compile(
    r"basic landcycling|\b(plains|island|swamp|mountain|forest)cycling"
    r"|search your library for (?:a|up to \w+) basic land",
    re.IGNORECASE,
)
_TAP_FOR_MANA = re.compile(r"\{T\}[^:\n]*:\s*Add ([^.\n]*)", re.IGNORECASE)
_TREASURE_MAKER = re.compile(r"\bcreates?\b[^.\n]*\bTreasure tokens?\b", re.IGNORECASE)
_QUOTED_TEXT = re.compile(r'"[^"]*"|“[^”]*”')
_REMINDER_TEXT = re.compile(r"\([^)]*\)")
_PERMANENT_TYPES = ("Artifact", "Creature", "Enchantment", "Planeswalker", "Battle")


class MeasureError(RuntimeError):
    """Raised when an input is missing or unusable and the run must stop."""


@dataclass(slots=True)
class Deck:
    """One draft deck: the latest build plus games and wins of every build."""

    event: str
    build: int
    main_colors: str
    splash_colors: str
    cards: dict[str, int]
    games: int = 0
    wins: int = 0


@dataclass(slots=True)
class DeckRecord:
    """A deck resolved against card data, with its mana facts."""

    deck: Deck
    lands: int
    basics: Counter[str]
    duals: list[tuple[str, ...]]
    sources: dict[str, int]
    color_cards: Counter[str]
    spells: list[CardInfo]
    double_symbol: list[tuple[str, str]]
    splash_cards: list[tuple[CardInfo, int]]
    nonland_fixers: int
    splash_fixers: Counter[str] = field(default_factory=Counter)

    @property
    def kind(self) -> str:
        """Return the deck kind from its main and splash colors."""

        main = len(self.deck.main_colors)
        if main == 3:
            return "3 colors"
        if main == 2 and self.deck.splash_colors:
            return "2 + splash"
        if main == 2:
            return "2 colors"
        return "other"

    @property
    def winning(self) -> bool:
        """Return whether the deck reached the win cutoff of its event."""

        return self.deck.wins >= WIN_CUTOFFS[self.deck.event]

    @property
    def lightest_color(self) -> str:
        """Return the main color with the fewest cards."""

        return min(
            self.deck.main_colors,
            key=lambda color: self.color_cards.get(color, 0),
        )

    @property
    def in_color_duals(self) -> int:
        """Count dual lands that make two or more of the main colors."""

        main = self.deck.main_colors
        return sum(
            1 for land in self.duals if sum(color in main for color in land) >= 2
        )


@dataclass(slots=True)
class CardIndex:
    """Cards of one set found by full name and by front face name."""

    by_name: dict[str, CardInfo] = field(default_factory=dict)

    @classmethod
    def build(cls, *, cards: Sequence[CardInfo]) -> CardIndex:
        """Index each card under its full name and its front face name."""

        index = cls()
        for card in cards:
            index.by_name.setdefault(card.name, card)
            index.by_name.setdefault(card.name.split(" // ")[0], card)
        return index

    def find(self, *, name: str) -> CardInfo | None:
        """Return the card for a deck column name, or None."""

        return self.by_name.get(name)


def event_from_filename(*, path: Path) -> str | None:
    """Return the event in a game_data_public.<SET>.<EVENT>.csv.gz name."""

    parts = path.name.split(".")
    if len(parts) >= 5 and parts[0] == "game_data_public":
        return parts[2]
    return None


def parse_count(*, value: str) -> int:
    """Parse a count that may be empty or stored as a decimal such as 1.0."""

    return int(float(value)) if value else 0


def read_decks(*, path: Path) -> tuple[str, list[Deck]]:
    """Read a game data file into one deck per draft id.
    The row with the highest build index supplies colors and cards.
    """

    decks: dict[str, Deck] = {}
    event: str | None = None
    with gzip.open(path, mode="rt", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise MeasureError(f"{path} is empty.")
        index = {name: position for position, name in enumerate(header)}
        required = ("draft_id", "build_index", "main_colors", "splash_colors", "won")
        missing = [name for name in required if name not in index]
        if missing:
            raise MeasureError(f"{path} has no column {', '.join(missing)}.")
        deck_columns = [
            (position, name[len("deck_") :])
            for position, name in enumerate(header)
            if name.startswith("deck_")
        ]
        for row in reader:
            row_event = (
                row[index["event_type"]]
                if "event_type" in index
                else event_from_filename(path=path)
            )
            if row_event not in WIN_CUTOFFS:
                raise MeasureError(
                    f"No win cutoff is defined for event type {row_event!r}. "
                    f"Supported: {', '.join(WIN_CUTOFFS)}."
                )
            if event is None:
                event = row_event
            elif row_event != event:
                raise MeasureError(
                    f"{path} mixes event types {event!r} and {row_event!r}."
                )
            draft_id = row[index["draft_id"]]
            build = parse_count(value=row[index["build_index"]])
            deck = decks.get(draft_id)
            if deck is None or build > deck.build:
                previous = deck
                deck = Deck(
                    event=row_event,
                    build=build,
                    main_colors=row[index["main_colors"]],
                    splash_colors=row[index["splash_colors"]],
                    cards={
                        name: parse_count(value=row[position])
                        for position, name in deck_columns
                        if parse_count(value=row[position]) != 0
                    },
                    games=0 if previous is None else previous.games,
                    wins=0 if previous is None else previous.wins,
                )
                decks[draft_id] = deck
            deck.games += 1
            deck.wins += row[index["won"]] == "True"
    if event is None:
        raise MeasureError(f"{path} has no game rows.")
    return event, list(decks.values())


def is_land(*, card: CardInfo) -> bool:
    """Return whether the front face is a land."""

    return any("Land" in type_name.split("//")[0] for type_name in card.types)


def is_basic(*, card: CardInfo) -> bool:
    """Return whether the card has the Basic supertype."""

    return any("Basic" in type_name for type_name in card.types)


def pips(*, card: CardInfo) -> Counter[str]:
    """Count mono-color symbols on the front face mana cost.
    Hybrid symbols are flexible and skipped.
    """

    counts: Counter[str] = Counter()
    front_cost = (card.mana_cost or "").split(" // ")[0]
    for symbol in _SYMBOL.findall(front_cost):
        colors = [color for color in COLORS if color in symbol]
        if len(colors) == 1 and "/" not in symbol:
            counts[colors[0]] += 1
    return counts


def is_nonland_fixer(*, card: CardInfo) -> bool:
    """Return whether a spell makes colored mana or finds a basic land."""

    return not is_land(card=card) and bool(FIXER_PATTERN.search(card.oracle_text or ""))


def _sides(*, card: CardInfo) -> list[tuple[str, str]]:
    if card.faces:
        return [(face.type_line or "", face.oracle_text or "") for face in card.faces]
    return [(card.type_line or " ".join(card.types), card.oracle_text or "")]


def fixer_kind(*, card: CardInfo, splash_color: str) -> str | None:
    """Return how a non-land card fixes the splash color, or None.
    The first matching kind wins: land search, mana creature or rock, Treasure.
    """

    if is_land(card=card):
        return None
    sides = _sides(card=card)
    if any(_LAND_SEARCH.search(text) for _, text in sides):
        return "land search"
    for type_line, text in sides:
        if not any(name in type_line for name in _PERMANENT_TYPES):
            continue
        cleaned = _REMINDER_TEXT.sub("", _QUOTED_TEXT.sub("", text))
        for clause in _TAP_FOR_MANA.findall(cleaned):
            if "any color" in clause.lower() or f"{{{splash_color}}}" in clause:
                return "mana creature" if "Creature" in type_line else "mana rock"
    if any(_TREASURE_MAKER.search(text) for _, text in sides):
        return "Treasure"
    return None


def classify_deck(
    *,
    deck: Deck,
    index: CardIndex,
    unmatched: Counter[str] | None = None,
) -> DeckRecord:
    """Resolve deck card names and compute lands, sources and pips.
    Names with no card data are counted in unmatched and skipped.
    """

    basics: Counter[str] = Counter()
    basic_total = 0
    duals: list[tuple[str, ...]] = []
    spells: list[CardInfo] = []
    for name, count in deck.cards.items():
        card = index.find(name=name)
        if card is None:
            if unmatched is not None:
                unmatched[name] += count
            continue
        if is_land(card=card):
            if is_basic(card=card):
                produced = tuple(
                    color for color in card.produced_mana if color in COLORS
                )
                for color in produced:
                    basics[color] += count
                basic_total += count
            else:
                duals.extend([land_mana_colors(card=card)] * count)
        else:
            spells.extend([card] * count)
    lands = basic_total + len(duals)
    sources = {
        color: basics[color] + sum(1 for land in duals if color in land)
        for color in COLORS
    }
    color_cards: Counter[str] = Counter()
    double_symbol: list[tuple[str, str]] = []
    splash_cards: list[tuple[CardInfo, int]] = []
    for card in spells:
        card_pips = pips(card=card)
        for color, count in card_pips.items():
            color_cards[color] += 1
            if count >= 2:
                double_symbol.append((card.name, color))
        for color in deck.splash_colors:
            if card_pips[color]:
                splash_cards.append((card, card_pips[color]))
    splash_fixers: Counter[str] = Counter()
    if len(deck.splash_colors) == 1:
        for card in spells:
            kind = fixer_kind(card=card, splash_color=deck.splash_colors)
            if kind is not None:
                splash_fixers[kind] += 1
    return DeckRecord(
        deck=deck,
        lands=lands,
        basics=basics,
        duals=duals,
        sources=sources,
        color_cards=color_cards,
        spells=spells,
        double_symbol=double_symbol,
        splash_cards=splash_cards,
        nonland_fixers=sum(1 for card in spells if is_nonland_fixer(card=card)),
        splash_fixers=splash_fixers,
    )


def build_records(
    *,
    decks: Sequence[Deck],
    index: CardIndex,
    unmatched: Counter[str] | None = None,
) -> list[DeckRecord]:
    """Classify decks and keep those with enough games and a sane land count."""

    records = [
        classify_deck(deck=deck, index=index, unmatched=unmatched) for deck in decks
    ]
    return [
        record
        for record in records
        if record.deck.games >= MIN_GAMES and MIN_LANDS <= record.lands <= MAX_LANDS
    ]


def win_rate(*, group: Sequence[DeckRecord]) -> float:
    """Return the game win rate of a group as a percentage."""

    games = sum(record.deck.games for record in group)
    if not games:
        return 0.0
    return 100 * sum(record.deck.wins for record in group) / games


def percent(*, part: int, whole: int) -> str:
    """Format a share as a whole percentage, or a dash when empty."""

    return f"{100 * part / whole:.0f}%" if whole else "-"


def distribution(*, values: Sequence[Any], order: Sequence[Any] | None = None) -> str:
    """Format the share of each distinct value."""

    counts = Counter(values)
    total = len(values)
    keys = list(order) if order is not None else sorted(counts)
    return ", ".join(
        f"{key}: {percent(part=counts[key], whole=total)}"
        for key in keys
        if counts[key]
    )


def quantile_line(*, values: Sequence[float]) -> str:
    """Format the p10 to p90 percentiles, or n<10 for small groups."""

    if len(values) < 10:
        return "n<10"
    cuts = statistics.quantiles(values, n=100)
    return " ".join(f"p{p}={cuts[p - 1]:.2f}" for p in PERCENTILES)


def lightest_color_share(*, record: DeckRecord) -> float:
    """Return the lightest main color's share of main-color card marks."""

    counts = [record.color_cards.get(color, 0) for color in record.deck.main_colors]
    return min(counts) / sum(counts) if sum(counts) else 0.0


def single_splash(*, records: Sequence[DeckRecord]) -> list[DeckRecord]:
    """Return splash decks with exactly one splash color."""

    return [
        record
        for record in records
        if record.kind == "2 + splash" and len(record.deck.splash_colors) == 1
    ]


def splash_source_count(*, record: DeckRecord) -> int:
    """Return the number of sources of the single splash color."""

    return record.sources[record.deck.splash_colors]


def has_two_symbol_splash_card(*, record: DeckRecord) -> bool:
    """Return whether any splash card has two or more splash symbols."""

    return any(count >= 2 for _, count in record.splash_cards)


@dataclass(frozen=True, slots=True)
class FixerWeight:
    """Splash land sources one non-land fixer replaces, with a 95% margin."""

    kind: str
    replaced: float
    margin: float
    decks: int


def fixer_weights(*, records: Sequence[DeckRecord]) -> list[FixerWeight] | None:
    """Fit splash land sources on fixer counts by least squares.
    Return None when there are too few decks or the fit is singular.
    """

    one = single_splash(records=records)
    # A kind no deck has would make the fit singular, so the fit leaves it out.
    kinds = [kind for kind in FIXER_KINDS if any(r.splash_fixers[kind] for r in one)]
    columns = len(kinds) + 3
    if len(one) < columns + 1:
        return None
    design = np.array(
        [
            [
                *(record.splash_fixers[kind] for kind in kinds),
                len(record.splash_cards),
                int(has_two_symbol_splash_card(record=record)),
                1,
            ]
            for record in one
        ],
        dtype=float,
    )
    target = np.array([splash_source_count(record=r) for r in one], dtype=float)
    try:
        inverse = np.linalg.inv(design.T @ design)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(inverse)):
        return None
    coefficients = inverse @ design.T @ target
    residuals = target - design @ coefficients
    sigma_squared = float(residuals @ residuals) / (len(one) - columns)
    errors = np.sqrt(np.maximum(np.diag(inverse) * sigma_squared, 0.0))
    return [
        FixerWeight(
            kind=kind,
            replaced=-float(coefficients[position]),
            margin=CONFIDENCE_Z * float(errors[position]),
            decks=sum(1 for r in one if r.splash_fixers[kind]),
        )
        for position, kind in enumerate(kinds)
    ]


def grade_of(*, card: CardInfo, ratings: Any) -> str | None:
    """Return the 17Lands letter grade of a card, or None when ungraded."""

    return ratings.rating_for(grp_id=card.grp_id).letter_grade


def grade_share(*, grades: Sequence[str | None], at_least: str) -> str:
    """Return the share of grades at or above a letter grade."""

    floor = GRADE_LABELS.index(at_least)
    good = sum(
        1
        for grade in grades
        if grade in GRADE_LABELS and GRADE_LABELS.index(grade) >= floor
    )
    return percent(part=good, whole=len(grades))


def _print_kinds(*, pool: list[DeckRecord], winning: list[DeckRecord]) -> None:
    print("== Deck kinds: share of all decks | share of winning decks | game win rate")
    for kind in KIND_LABELS:
        everyone = [record for record in pool if record.kind == kind]
        winners = [record for record in winning if record.kind == kind]
        print(
            f"  {kind:11} {percent(part=len(everyone), whole=len(pool)):>4} | "
            f"{percent(part=len(winners), whole=len(winning)):>4} | "
            f"{win_rate(group=everyone):.1f}%"
        )


def _print_three_color(*, label: str, group: list[DeckRecord]) -> None:
    three = [record for record in group if record.kind == "3 colors"]
    print(f"\n== 3-color decks ({label}, n={len(three)})")
    if not three:
        return
    print(
        "  lightest color card share:",
        quantile_line(values=[lightest_color_share(record=r) for r in three]),
    )
    print(
        "  in-color dual lands:",
        distribution(values=[min(r.in_color_duals, MAX_DUAL_BUCKET) for r in three]),
        f"({MAX_DUAL_BUCKET} = {MAX_DUAL_BUCKET}+)",
    )
    print(
        "  sources of the lightest color:",
        distribution(values=[r.sources[r.lightest_color] for r in three]),
    )
    with_double = [r for r in three if r.double_symbol]
    print(
        "  decks with a double-symbol card:",
        percent(part=len(with_double), whole=len(three)),
    )
    print(
        "  sources behind each double-symbol card:",
        distribution(
            values=[r.sources[color] for r in three for _, color in r.double_symbol]
        ),
    )


def _print_splash(*, label: str, group: list[DeckRecord]) -> None:
    one = single_splash(records=group)
    print(f"\n== 2-color + 1 splash decks ({label}, n={len(one)})")
    if not one:
        return
    print(
        "  splash cards per deck:",
        distribution(
            values=[min(len(r.splash_cards), MAX_SPLASH_CARD_BUCKET) for r in one]
        ),
        f"({MAX_SPLASH_CARD_BUCKET} = {MAX_SPLASH_CARD_BUCKET}+)",
    )
    single = [r for r in one if not has_two_symbol_splash_card(record=r)]
    double = [r for r in one if has_two_symbol_splash_card(record=r)]
    print(
        f"  splash sources, only single-symbol splash cards (n={len(single)}):",
        distribution(values=[splash_source_count(record=r) for r in single]),
    )
    print(
        f"  splash sources, with a two-symbol splash card (n={len(double)}):",
        distribution(values=[splash_source_count(record=r) for r in double]),
    )
    print(
        "  splash basics:",
        distribution(values=[r.basics[r.deck.splash_colors] for r in one]),
    )


def _print_fixer_weights(*, label: str, group: list[DeckRecord]) -> None:
    one = single_splash(records=group)
    print(
        "\n== Splash land sources replaced by each non-land fixer "
        f"({label}, n={len(one)})"
    )
    weights = fixer_weights(records=group)
    if weights is None:
        print("  too few decks to fit")
        return
    for weight in weights:
        print(
            f"  {weight.kind:13} {weight.replaced:+.2f} +/- {weight.margin:.2f}"
            f" (decks with it: {weight.decks})"
        )


def _print_winning_grades(*, winning: list[DeckRecord], ratings: Any) -> None:
    one = single_splash(records=winning)
    grades = [
        grade_of(card=card, ratings=ratings)
        for record in one
        for card, _ in record.splash_cards
    ]
    order = [*reversed(GRADE_LABELS), None]
    print(f"\n== Grades of splashed cards (winning decks, n={len(grades)})")
    print("  ", distribution(values=grades, order=order))
    print(
        f"  A- or better: {grade_share(grades=grades, at_least='A-')}"
        f"  B or better: {grade_share(grades=grades, at_least='B')}"
    )


def _main_spell_win_rates(*, record: DeckRecord, ratings: Any) -> list[float]:
    splash_color = record.deck.splash_colors
    rates = []
    for card in record.spells:
        if pips(card=card)[splash_color] != 0:
            continue
        rate = ratings.rating_for(grp_id=card.grp_id).gih_win_rate
        if rate is not None:
            rates.append(rate)
    return rates


def splash_quality_gaps(
    *, records: Sequence[DeckRecord], ratings: Any, weakest: bool = False
) -> list[float]:
    """Return each splashed card's GIH win rate minus its deck's main-colour baseline.
    The baseline is the median main-colour spell, or the weakest one when asked.
    """

    gaps: list[float] = []
    for record in records:
        main_rates = _main_spell_win_rates(record=record, ratings=ratings)
        if len(main_rates) < MIN_MAIN_SPELLS:
            continue
        baseline = min(main_rates) if weakest else statistics.median(main_rates)
        for card, _ in record.splash_cards:
            rate = ratings.rating_for(grp_id=card.grp_id).gih_win_rate
            if rate is not None:
                gaps.append(rate - baseline)
    return gaps


def gap_percentile_line(*, gaps: Sequence[float]) -> str:
    """Format the gap percentiles in win rate points, or n<10 for small groups."""

    if len(gaps) < 10:
        return "n<10"
    cuts = statistics.quantiles(gaps, n=100)
    return " ".join(f"p{p}={cuts[p - 1]:+.3f}" for p in GAP_PERCENTILES)


def main_color_sources(*, record: DeckRecord) -> tuple[int, int]:
    """Return land sources of the primary and secondary main colour.
    The colour with more cards is primary, and a tie keeps the pair order.
    """

    first, second = record.deck.main_colors
    cards = record.color_cards
    if cards.get(second, 0) > cards.get(first, 0):
        first, second = second, first
    return record.sources[first], record.sources[second]


def _print_splash_quality(*, winning: list[DeckRecord], ratings: Any) -> None:
    one = single_splash(records=winning)
    gaps = splash_quality_gaps(records=one, ratings=ratings)
    print(f"\n== Splash card quality (winning decks, n={len(gaps)} splashed cards)")
    print("  GIH minus median main-colour spell:", gap_percentile_line(gaps=gaps))
    print(
        f"  at least {SPLASH_GAP_FLOOR:+.3f}:",
        percent(part=sum(gap >= SPLASH_GAP_FLOOR for gap in gaps), whole=len(gaps)),
    )
    print(
        "  GIH minus weakest main-colour spell:",
        gap_percentile_line(
            gaps=splash_quality_gaps(records=one, ratings=ratings, weakest=True)
        ),
    )


def split_by_splash_dual(
    *, records: Sequence[DeckRecord]
) -> tuple[list[DeckRecord], list[DeckRecord]]:
    """Split one-splash decks into those with no splash dual and those with one or more.
    A splash dual is a land in the deck that makes the splash color.
    """

    without: list[DeckRecord] = []
    with_dual: list[DeckRecord] = []
    for record in records:
        has_dual = any(record.deck.splash_colors in land for land in record.duals)
        (with_dual if has_dual else without).append(record)
    return without, with_dual


def _print_splash_dual_group(
    *, label: str, group: list[DeckRecord], total: int
) -> None:
    combos = [
        (*main_color_sources(record=r), splash_source_count(record=r)) for r in group
    ]
    both = sum(p >= 8 and s >= 7 for p, s, _ in combos)
    print(
        f"  {label}: n={len(group)} ({percent(part=len(group), whole=total)} of decks),"
        f" primary >= 8 and secondary >= 7: {percent(part=both, whole=len(group))}"
    )
    for (primary, secondary, splash), count in Counter(combos).most_common(6):
        print(
            f"    primary {primary}, secondary {secondary}, splash {splash}:"
            f" {percent(part=count, whole=len(group))}"
        )


def _print_main_color_sources(*, winning: list[DeckRecord]) -> None:
    decks = [r for r in single_splash(records=winning) if len(r.deck.main_colors) == 2]
    pairs = [main_color_sources(record=r) for r in decks]
    print(f"\n== Base-colour sources in one-splash decks (winning, n={len(pairs)})")
    print("  primary:", distribution(values=[p for p, _ in pairs]))
    print("  secondary:", distribution(values=[s for _, s in pairs]))
    print(
        "  primary >= 8:",
        percent(part=sum(p >= 8 for p, _ in pairs), whole=len(pairs)),
        " secondary >= 7:",
        percent(part=sum(s >= 7 for _, s in pairs), whole=len(pairs)),
    )
    without, with_dual = split_by_splash_dual(records=decks)
    print("  by splash dual lands:")
    _print_splash_dual_group(label="no splash dual", group=without, total=len(decks))
    _print_splash_dual_group(label="1+ splash dual", group=with_dual, total=len(decks))


def _print_win_rates(*, pool: list[DeckRecord]) -> None:
    three = [r for r in pool if r.kind == "3 colors"]
    print(
        "\n== Win rate by fixing pieces (3-color decks, in-color duals + non-land fixers)"
    )
    for pieces in range(MAX_FIXING_BUCKET + 1):
        bucket = [
            r
            for r in three
            if min(r.in_color_duals + r.nonland_fixers, MAX_FIXING_BUCKET) == pieces
        ]
        suffix = "+" if pieces == MAX_FIXING_BUCKET else " "
        print(
            f"  {pieces}{suffix} pieces: n={len(bucket):5} WR {win_rate(group=bucket):.1f}%"
        )
    print("\n== Win rate by non-land fixer count")
    for kind in KIND_LABELS:
        kind_group = [r for r in pool if r.kind == kind]
        print(f"  {kind} (n={len(kind_group)})")
        for count in range(MAX_NONLAND_FIXER_BUCKET + 1):
            bucket = [
                r
                for r in kind_group
                if min(r.nonland_fixers, MAX_NONLAND_FIXER_BUCKET) == count
            ]
            suffix = "+" if count == MAX_NONLAND_FIXER_BUCKET else " "
            print(
                f"    {count}{suffix} fixers: n={len(bucket):5} WR {win_rate(group=bucket):.1f}%"
            )


def print_report(*, event: str, records: list[DeckRecord], ratings: Any) -> None:
    """Print every stat for one event."""

    winning = [record for record in records if record.winning]
    print(f"######## {event}: {len(records)} decks, {len(winning)} winning decks")
    _print_kinds(pool=records, winning=winning)
    for label, group in (("winning", winning), ("all", records)):
        _print_three_color(label=label, group=group)
        _print_splash(label=label, group=group)
    for label, group in (("winning", winning), ("all", records)):
        _print_fixer_weights(label=label, group=group)
    _print_winning_grades(winning=winning, ratings=ratings)
    _print_splash_quality(winning=winning, ratings=ratings)
    _print_main_color_sources(winning=winning)
    _print_win_rates(pool=records)


def load_card_index(*, set_code: str, app_dir: Path | None) -> CardIndex:
    """Load the cached card data for a set without network access.
    A missing cache stops the run with a clear message.
    """

    try:
        database = CardDataClient(app_dir=app_dir).load(set_code, allow_network=False)
    except CardDataClientError as error:
        raise MeasureError(
            f"No cached card data for {set_code}. Open the set in Draftomen once "
            f"to cache it. ({error})"
        ) from error
    return CardIndex.build(cards=list(database.cards.values()))


def load_ratings(*, set_code: str, event: str, app_dir: Path | None) -> Any:
    """Load cached 17Lands ratings for the set and event format.
    Missing or empty ratings stop the run instead of grading from a neutral prior.
    """

    ratings = load_cached_17lands_data(
        set_code=set_code,
        event_format=event,
        app_dir=app_dir,
    )
    if not ratings.primary.card_ratings:
        raise MeasureError(
            f"No cached 17Lands ratings for {set_code} {event}. "
            "Refresh the ratings in Draftomen first."
        )
    return ratings


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--game-data",
        required=True,
        type=Path,
        help="Path to game_data_public.<SET>.<EVENT>.csv.gz.",
    )
    parser.add_argument("--set-code", required=True, help="Set code, such as FRA.")
    parser.add_argument(
        "--app-dir",
        type=Path,
        default=None,
        help="Draftomen data directory. Defaults to ~/.draftomen.",
    )
    parser.add_argument(
        "--ratings-format",
        default=None,
        help=(
            "17Lands format whose cached ratings grade the splash cards, such as "
            "PremierDraft. Defaults to the event of the game data file."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the measurement and print the stats."""

    args = build_parser().parse_args(argv)
    set_code = args.set_code.upper()
    try:
        event, decks = read_decks(path=args.game_data)
        index = load_card_index(set_code=set_code, app_dir=args.app_dir)
        ratings = load_ratings(
            set_code=set_code,
            event=args.ratings_format or event,
            app_dir=args.app_dir,
        )
    except MeasureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    unmatched: Counter[str] = Counter()
    records = build_records(decks=decks, index=index, unmatched=unmatched)
    if unmatched:
        print(
            f"unmatched card names: {unmatched.most_common(8)}",
            file=sys.stderr,
        )
    print_report(event=event, records=records, ratings=ratings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
