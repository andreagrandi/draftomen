from __future__ import annotations

import csv
import gzip
import io
from pathlib import Path

import numpy as np
import pytest

from draftomen.augmented_artifact import AugmentedCardPairs
from draftomen.augmented_card_pairs import (
    CardPairsError,
    gate_card_pairs,
    pair_table,
)

_CARDS = ("Alpha", "Beta", "Gamma", "Delta")
_VALIDATION_START = "2026-08-01T00:00:00.000000+00:00"


def _game_row(*, draft: str, time: str, deck: tuple[str, ...], won: bool) -> dict[str, str]:
    row = {
        "expansion": "TST",
        "event_type": "PremierDraft",
        "draft_id": draft,
        "draft_time": time,
        "rank": "gold",
        "main_colors": "WU",
        "won": "True" if won else "False",
        # Basic lands stay out of the table.
        "deck_Plains": "8",
    }
    for card in _CARDS:
        row[f"deck_{card}"] = "1" if card in deck else "0"
    return row


def _training_rows(*, count: int) -> list[dict[str, str]]:
    """Return games before the cut-off where Alpha and Beta win together."""

    rows = []
    for index in range(count):
        time = f"2026-07-{1 + index % 28:02d} 12:00:00"
        rows.extend(
            [
                _game_row(draft=f"t{index}a", time=time, deck=("Alpha", "Beta"), won=True),
                _game_row(draft=f"t{index}b", time=time, deck=("Alpha", "Gamma"), won=False),
                _game_row(draft=f"t{index}c", time=time, deck=("Beta", "Delta"), won=False),
                _game_row(draft=f"t{index}d", time=time, deck=("Gamma", "Delta"), won=True),
            ]
        )
    return rows


def _held_out_rows(*, count: int, pairs_win: bool) -> list[dict[str, str]]:
    # Games drafted exactly at the validation start are held out.
    rows = [
        _game_row(
            draft="edge-a",
            time="2026-08-01 00:00:00",
            deck=("Alpha", "Beta"),
            won=pairs_win,
        ),
        _game_row(
            draft="edge-b",
            time="2026-08-01 00:00:00",
            deck=("Alpha", "Gamma"),
            won=not pairs_win,
        ),
    ]
    for index in range(count):
        time = f"2026-08-{2 + index % 20:02d} 12:00:00"
        rows.extend(
            [
                _game_row(
                    draft=f"h{index}a", time=time, deck=("Alpha", "Beta"), won=pairs_win
                ),
                _game_row(
                    draft=f"h{index}b",
                    time=time,
                    deck=("Alpha", "Gamma"),
                    won=not pairs_win,
                ),
            ]
        )
    return rows


def _write_dump(*, path: Path, rows: list[dict[str, str]]) -> Path:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    path.write_bytes(gzip.compress(buffer.getvalue().encode("utf-8"), mtime=0))
    return path


def test_table_uses_only_games_drafted_before_the_validation_start(tmp_path: Path) -> None:
    training = _training_rows(count=30)
    # Held-out games where Alpha and Beta lose would pull the pair down if they leaked in.
    held_out = _held_out_rows(count=20, pairs_win=False)
    dump = _write_dump(path=tmp_path / "games.bin", rows=[*held_out, *training])

    gate = gate_card_pairs(game_path=dump, validation_start=_VALIDATION_START)

    assert gate.card_names == _CARDS
    assert gate.training_games == len(training)
    assert gate.held_out_games == len(held_out)
    assert gate.held_out_drafts == len(held_out)
    in_deck = np.asarray(
        [[float(row[f"deck_{card}"]) for card in _CARDS] for row in training],
        dtype=np.float32,
    )
    won = np.asarray([row["won"] == "True" for row in training], dtype=np.float32)
    np.testing.assert_allclose(gate.scores, pair_table(in_deck=in_deck, won=won))
    # 30 games of +50 points, shrunk by 30 / (30 + 200).
    assert gate.scores[0, 1] == pytest.approx(50.0 * 30 / 230, rel=1e-5)


def test_gate_passes_when_better_pairs_win_more_held_out_games(tmp_path: Path) -> None:
    rows = [*_training_rows(count=30), *_held_out_rows(count=20, pairs_win=True)]
    dump = _write_dump(path=tmp_path / "games.bin", rows=rows)

    gate = gate_card_pairs(game_path=dump, validation_start=_VALIDATION_START)

    assert gate.passed is True
    assert gate.gap_low > 0.0
    report = gate.report()
    assert report["passed"] is True
    assert report["gap_pp"] == pytest.approx(100.0)
    assert report["validation_start"] == _VALIDATION_START


def test_gate_fails_when_better_pairs_lose_held_out_games(tmp_path: Path) -> None:
    rows = [*_training_rows(count=30), *_held_out_rows(count=20, pairs_win=False)]
    dump = _write_dump(path=tmp_path / "games.bin", rows=rows)

    gate = gate_card_pairs(game_path=dump, validation_start=_VALIDATION_START)

    assert gate.passed is False
    assert gate.report()["gap_high_pp"] < 0.0


def test_table_json_parses_as_the_artifact_card_pairs_table(tmp_path: Path) -> None:
    rows = [*_training_rows(count=30), *_held_out_rows(count=20, pairs_win=True)]
    dump = _write_dump(path=tmp_path / "games.bin", rows=rows)
    gate = gate_card_pairs(game_path=dump, validation_start=_VALIDATION_START)

    table = gate.table_json()
    parsed = AugmentedCardPairs.from_json(table)

    assert table["trained_before"] == _VALIDATION_START
    assert table["training_games"] == 120
    assert parsed.card_names == _CARDS
    assert parsed.score(first="Beta", second="Alpha") == pytest.approx(
        round(float(gate.scores[0, 1]), 2)
    )
    assert all(first < second for first, second, _ in table["pairs"])


def test_game_data_without_games_after_the_cut_off_is_rejected(tmp_path: Path) -> None:
    dump = _write_dump(path=tmp_path / "games.bin", rows=_training_rows(count=3))

    with pytest.raises(CardPairsError, match="before and after"):
        gate_card_pairs(game_path=dump, validation_start=_VALIDATION_START)


def test_unparseable_validation_start_is_rejected(tmp_path: Path) -> None:
    dump = _write_dump(path=tmp_path / "games.bin", rows=_training_rows(count=3))

    with pytest.raises(CardPairsError, match="ISO"):
        gate_card_pairs(game_path=dump, validation_start="last Tuesday")

