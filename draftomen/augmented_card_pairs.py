"""Build a Card Pairs table from 17Lands games and test it on held-out games.
The table ships only when better pairs win more games among comparable decks.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
from pathlib import Path

import numpy as np
import polars as pl

from draftomen.augmented_artifact import AUGMENTED_CARD_PAIRS_FORMAT

# A pair seen in n games keeps n / (n + 200) of its raw interaction.
PAIR_SHRINKAGE_GAMES = 200.0
BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_SEED = 20260926
BASIC_LANDS = frozenset(("Plains", "Island", "Swamp", "Mountain", "Forest"))
CARD_PAIRS_GATE_RULE = "same_colours_and_rank_gap_lower_bound_above_zero"


class CardPairsError(RuntimeError):
    """Report game data that cannot build or test a Card Pairs table."""


@dataclass(frozen=True, slots=True)
class CardPairsGate:
    """Hold a Card Pairs table and its held-out win-rate gap with a 95% range.
    The gap compares better and worse pairs in the same colours, rank and strength.
    """

    card_names: tuple[str, ...]
    scores: np.ndarray
    validation_start: str
    training_games: int
    held_out_games: int
    held_out_drafts: int
    gap: float
    gap_low: float
    gap_high: float
    # The game dump's URL and checksum, recorded in the report when set.
    source: dict[str, str] | None = None

    @property
    def passed(self) -> bool:
        return self.gap_low > 0.0

    def report(self) -> dict[str, object]:
        """Return the gate result for the build report, with gaps in points."""

        report: dict[str, object] = {
            "passed": self.passed,
            "rule": CARD_PAIRS_GATE_RULE,
            "gap_pp": round(self.gap * 100.0, 4),
            "gap_low_pp": round(self.gap_low * 100.0, 4),
            "gap_high_pp": round(self.gap_high * 100.0, 4),
            "validation_start": self.validation_start,
            "training_games": self.training_games,
            "held_out_games": self.held_out_games,
            "held_out_drafts": self.held_out_drafts,
        }
        if self.source is not None:
            report["source"] = dict(self.source)
        return report

    def table_json(self) -> dict[str, object]:
        """Return the table as card names and nonzero pairs rounded to 0.01 points.
        Each pair is [first index, second index, score] with first below second.
        """

        first, second = np.triu_indices(len(self.card_names), k=1)
        values = np.round(self.scores[first, second].astype(np.float64), 2)
        kept = np.flatnonzero(values != 0.0)
        return {
            "format": AUGMENTED_CARD_PAIRS_FORMAT,
            "card_names": list(self.card_names),
            "pairs": [
                [int(first[index]), int(second[index]), float(values[index])]
                for index in kept
            ],
            "training_games": self.training_games,
            "trained_before": self.validation_start,
        }


def csv_header(*, path: Path) -> list[str]:
    with path.open(mode="rb") as probe:
        is_gzip = probe.read(2) == b"\x1f\x8b"
    opener = gzip.open if is_gzip else open
    try:
        with opener(path, mode="rt", encoding="utf-8", newline="") as handle:
            return next(csv.reader(handle))
    except (OSError, EOFError, StopIteration, UnicodeError) as error:
        raise CardPairsError("Game data has no readable CSV header.") from error


def game_card_names(*, path: Path) -> tuple[str, ...]:
    """Return the deck card names in a game dump, without basic lands.
    Basic lands would carry deck colours into both the pair and strength measures.
    """

    return tuple(
        column.removeprefix("deck_")
        for column in csv_header(path=path)
        if column.startswith("deck_")
        and column.removeprefix("deck_") not in BASIC_LANDS
    )


def read_decks(
    *, game_path: Path, card_names: tuple[str, ...]
) -> tuple[pl.DataFrame, np.ndarray]:
    """Return each game's draft, time and result, and a games-by-cards deck matrix.
    A matrix cell is 1 when that card is in the game's main deck.
    """

    header = csv_header(path=game_path)
    index_by_name = {name: index for index, name in enumerate(card_names)}
    deck_columns = [
        column
        for column in header
        if column.startswith("deck_") and column.removeprefix("deck_") in index_by_name
    ]
    missing = sorted({"draft_id", "draft_time", "won", "main_colors", "rank"} - set(header))
    if missing:
        raise CardPairsError(f"Game data is missing required columns: {missing}.")
    games = pl.read_csv(
        game_path,
        columns=["draft_id", "draft_time", "won", "main_colors", "rank", *deck_columns],
        schema_overrides={
            "main_colors": pl.Utf8,
            "rank": pl.Utf8,
            # Older dumps such as WOE write deck counts as 0.0.
            **{column: pl.Float32 for column in deck_columns},
        },
        low_memory=False,
    ).with_columns(pl.col("draft_time").str.to_datetime())
    columns = [index_by_name[column.removeprefix("deck_")] for column in deck_columns]
    # Float32 counts stay exact up to 16 million games.
    in_deck = np.zeros((len(games), len(card_names)), dtype=np.float32)
    in_deck[:, columns] = games.select(deck_columns).to_numpy() > 0
    return games.select("draft_id", "draft_time", "won", "main_colors", "rank"), in_deck


def pair_table(*, in_deck: np.ndarray, won: np.ndarray) -> np.ndarray:
    """Return each card pair's shrunk win-rate interaction in percentage points."""

    pair_games = in_deck.T @ in_deck
    pair_wins = in_deck.T @ (in_deck * won[:, None])
    card_games = np.diag(pair_games)
    card_rate = np.diag(pair_wins) / np.maximum(card_games, 1.0)
    pair_rate = pair_wins / np.maximum(pair_games, 1.0)
    overall_rate = won.mean()
    interaction = pair_rate - card_rate[:, None] - card_rate[None, :] + overall_rate
    scores = interaction * pair_games / (pair_games + PAIR_SHRINKAGE_GAMES)
    np.fill_diagonal(scores, 0.0)
    return (scores * 100.0).astype(np.float32)


def deciles(*, values: np.ndarray) -> np.ndarray:
    edges = np.quantile(values, np.linspace(0.0, 1.0, 11)[1:-1])
    return np.searchsorted(edges, values, side="right")


def bootstrap_counts(*, draft_count: int) -> np.ndarray:
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    return np.stack(
        [
            np.bincount(
                rng.integers(0, draft_count, size=draft_count),
                minlength=draft_count,
            ).astype(np.float32)
            for _ in range(BOOTSTRAP_SAMPLES)
        ]
    )


def split_gap(
    *,
    strata: np.ndarray,
    split_by: np.ndarray,
    won: np.ndarray,
    draft_codes: np.ndarray,
    counts: np.ndarray,
) -> tuple[float, float, float]:
    """Return the win-rate gap between the halves of split_by within each stratum.
    Each stratum's gap is weighted by n1 * n0 / (n1 + n0), resampling whole drafts.
    """

    _, strata = np.unique(strata, return_inverse=True)
    stratum_count = int(strata.max()) + 1
    high = np.zeros(len(won), dtype=np.int64)
    for stratum in range(stratum_count):
        members = strata == stratum
        high[members] = split_by[members] > np.median(split_by[members])
    cells = strata * 2 + high
    wins = np.zeros((int(draft_codes.max()) + 1, stratum_count * 2))
    games = np.zeros_like(wins)
    np.add.at(wins, (draft_codes, cells), won)
    np.add.at(games, (draft_codes, cells), 1.0)

    def gap(*, cell_wins: np.ndarray, cell_games: np.ndarray) -> np.ndarray:
        cell_wins = cell_wins.reshape(-1, stratum_count, 2)
        cell_games = cell_games.reshape(-1, stratum_count, 2)
        rates = cell_wins / np.maximum(cell_games, 1.0)
        n0, n1 = cell_games[..., 0], cell_games[..., 1]
        weight = np.where((n0 > 0) & (n1 > 0), n1 * n0 / np.maximum(n1 + n0, 1.0), 0.0)
        return np.sum((rates[..., 1] - rates[..., 0]) * weight, axis=-1) / np.maximum(
            np.sum(weight, axis=-1), 1e-12
        )

    point = gap(cell_wins=wins.sum(axis=0), cell_games=games.sum(axis=0))[0]
    samples = gap(cell_wins=counts @ wins, cell_games=counts @ games)
    low, top = np.percentile(samples, [2.5, 97.5])
    return float(point), float(low), float(top)


def deck_measures(
    *,
    in_deck: np.ndarray,
    won: np.ndarray,
    train: np.ndarray,
    held: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return the training table and each held-out deck's card strength and pair score.
    Card strength is the average card win rate above the overall training rate.
    """

    table = pair_table(in_deck=in_deck[train], won=won[train])
    card_games = in_deck[train].sum(axis=0)
    card_rate = (won[train] @ in_deck[train]) / np.maximum(card_games, 1.0)
    # Cards seen in fewer than 200 training games count as average strength.
    card_edge = np.where(
        card_games >= PAIR_SHRINKAGE_GAMES, card_rate - won[train].mean(), 0.0
    )
    decks = in_deck[held]
    sizes = np.maximum(decks.sum(axis=1), 2.0)
    strength = (decks @ card_edge) / sizes
    pairs = np.sum((decks @ (table / 100.0)) * decks, axis=1) / (sizes * (sizes - 1.0))
    return table, strength, pairs


def colours_and_rank(*, games: pl.DataFrame) -> np.ndarray:
    """Return a code per game for its main colours and player rank."""

    labels = (
        games.select(
            pl.concat_str(
                [
                    pl.col("main_colors").fill_null(""),
                    pl.col("rank").fill_null("unknown"),
                ],
                separator="|",
            )
        )
        .to_series()
        .to_numpy()
    )
    _, codes = np.unique(labels, return_inverse=True)
    return codes


def gate_card_pairs(*, game_path: Path, validation_start: str) -> CardPairsGate:
    """Build the table from games drafted before Model C's validation period.
    The later games test whether better pairs win more in equal colours, rank and strength.
    """

    try:
        cutoff = datetime.fromisoformat(validation_start)
    except (TypeError, ValueError) as error:
        raise CardPairsError(
            f"Validation start {validation_start!r} is not an ISO date and time."
        ) from error
    # 17Lands writes naive UTC times, and Model C stores them as UTC.
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone(UTC).replace(tzinfo=None)
    card_names = game_card_names(path=game_path)
    if not card_names:
        raise CardPairsError("Game data has no deck card columns.")
    games, in_deck = read_decks(game_path=game_path, card_names=card_names)
    train = (games["draft_time"] < cutoff).to_numpy()
    held = ~train
    if not train.any() or not held.any():
        raise CardPairsError(
            f"Game data needs games both before and after {validation_start}."
        )
    won = games["won"].cast(pl.Float32).to_numpy()
    table, strength, pairs = deck_measures(
        in_deck=in_deck, won=won, train=train, held=held
    )
    held_games = games.filter(pl.Series(held))
    _, draft_codes = np.unique(held_games["draft_id"].to_numpy(), return_inverse=True)
    draft_count = int(draft_codes.max()) + 1
    # Decks in the same colours and player rank, so the pair score cannot
    # stand in for archetype strength or skill.
    gap, low, high = split_gap(
        strata=colours_and_rank(games=held_games) * 10 + deciles(values=strength),
        split_by=pairs,
        won=won[held].astype(np.float64),
        draft_codes=draft_codes,
        counts=bootstrap_counts(draft_count=draft_count),
    )
    return CardPairsGate(
        card_names=card_names,
        scores=table,
        validation_start=validation_start,
        training_games=int(train.sum()),
        held_out_games=int(held.sum()),
        held_out_drafts=draft_count,
        gap=gap,
        gap_low=low,
        gap_high=high,
    )


__all__ = [
    "CARD_PAIRS_GATE_RULE",
    "CardPairsError",
    "CardPairsGate",
    "gate_card_pairs",
]
