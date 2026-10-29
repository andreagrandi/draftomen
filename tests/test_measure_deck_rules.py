from __future__ import annotations

import csv
import gzip
import socket
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from draftomen.carddb import CardInfo
from scripts import measure_deck_rules
from scripts.measure_deck_rules import (
    CardIndex,
    MeasureError,
    build_records,
    classify_deck,
    main,
    read_decks,
)

CARD_NAMES = [
    "Plains",
    "Island",
    "Swamp",
    "Azorius Gate",
    "Bolt Spell",
    "White Twin",
    "Blue Bear",
    "Fixer Elf",
    "Black Splash",
]


def _card(
    *,
    grp_id: int,
    name: str,
    types: tuple[str, ...],
    mana_cost: str | None = None,
    produced: tuple[str, ...] = (),
    oracle_text: str | None = None,
) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=(),
        mana_value=None,
        rarity="common",
        types=types,
        mana_cost=mana_cost,
        produced_mana=produced,
        oracle_text=oracle_text,
    )


def _cards() -> list[CardInfo]:
    return [
        _card(grp_id=1, name="Plains", types=("Basic", "Land"), produced=("W",)),
        _card(grp_id=2, name="Island", types=("Basic", "Land"), produced=("U",)),
        _card(grp_id=3, name="Swamp", types=("Basic", "Land"), produced=("B",)),
        _card(grp_id=4, name="Azorius Gate", types=("Land",), produced=("W", "U")),
        _card(grp_id=5, name="Bolt Spell", types=("Instant",), mana_cost="{R}"),
        _card(grp_id=6, name="White Twin", types=("Creature",), mana_cost="{1}{W}{W}"),
        _card(grp_id=7, name="Blue Bear", types=("Creature",), mana_cost="{1}{U}"),
        _card(
            grp_id=8,
            name="Fixer Elf",
            types=("Creature",),
            mana_cost="{1}{G}",
            oracle_text="{T}: Add one mana of any color.",
        ),
        _card(
            grp_id=9,
            name="Black Splash // Back Side",
            types=("Creature",),
            mana_cost="{B}{B} // {1}",
        ),
    ]


def _index() -> CardIndex:
    return CardIndex.build(cards=_cards())


def _write_game_data(
    *,
    path: Path,
    rows: list[dict[str, Any]],
) -> Path:
    header = [
        "expansion",
        "event_type",
        "draft_id",
        "build_index",
        "main_colors",
        "splash_colors",
        "won",
    ] + [f"deck_{name}" for name in CARD_NAMES]
    with gzip.open(path, mode="wt", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            cards = row.get("cards", {})
            writer.writerow(
                [
                    "TST",
                    row.get("event", "PremierDraft"),
                    row["draft"],
                    row.get("build", 0),
                    row.get("main", "WU"),
                    row.get("splash", ""),
                    "True" if row.get("won") else "False",
                ]
                + [cards.get(name, 0) for name in CARD_NAMES]
            )
    return path


def _two_color_cards(*, lands: int = 17) -> dict[str, int]:
    return {
        "Plains": lands - 4,
        "Island": 3,
        "Azorius Gate": 1,
        "White Twin": 2,
        "Blue Bear": 3,
    }


def _games(*, draft: str, wins: int, losses: int, **extra: Any) -> list[dict[str, Any]]:
    rows = [dict(draft=draft, won=True, **extra) for _ in range(wins)]
    rows += [dict(draft=draft, won=False, **extra) for _ in range(losses)]
    return rows


def test_read_decks_keeps_latest_build_and_sums_games_across_builds(
    tmp_path: Path,
) -> None:
    rows = [
        dict(draft="a", build=0, won=True, main="WU", cards={"Plains": 17}),
        dict(draft="a", build=1, won=False, main="WB", cards={"Swamp": 17}),
        dict(draft="a", build=1, won=True, main="WB", cards={"Swamp": 17}),
        dict(draft="b", build=0, won=False, main="WU", cards={"Plains": 16}),
    ]
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=rows)

    event, decks = read_decks(path=path)

    assert event == "PremierDraft"
    by_colors = {deck.main_colors: deck for deck in decks}
    assert len(decks) == 2
    assert by_colors["WB"].cards == {"Swamp": 17}
    assert by_colors["WB"].games == 3
    assert by_colors["WB"].wins == 2
    assert by_colors["WU"].games == 1


def test_premier_cutoff_is_seven_wins(tmp_path: Path) -> None:
    rows = _games(draft="six", wins=6, losses=0, cards=_two_color_cards())
    rows += _games(draft="seven", wins=7, losses=0, cards=_two_color_cards())
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=rows)
    event, decks = read_decks(path=path)

    records = build_records(decks=decks, index=_index())

    assert event == "PremierDraft"
    assert sorted(r.deck.wins for r in records if r.winning) == [7]
    assert len(records) == 2


def test_pick_two_cutoff_is_four_wins(tmp_path: Path) -> None:
    rows = _games(
        draft="three", wins=3, losses=0, event="PickTwoDraft", cards=_two_color_cards()
    )
    rows += _games(
        draft="four", wins=4, losses=0, event="PickTwoDraft", cards=_two_color_cards()
    )
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=rows)
    event, decks = read_decks(path=path)

    records = build_records(decks=decks, index=_index())

    assert event == "PickTwoDraft"
    assert [r.deck.wins for r in records if r.winning] == [4]


def test_decks_with_few_games_or_odd_land_counts_are_filtered_out(
    tmp_path: Path,
) -> None:
    rows = _games(draft="short", wins=2, losses=0, cards=_two_color_cards())
    rows += _games(
        draft="few-lands", wins=3, losses=0, cards=_two_color_cards(lands=14)
    )
    rows += _games(
        draft="many-lands", wins=3, losses=0, cards=_two_color_cards(lands=20)
    )
    rows += _games(draft="ok", wins=3, losses=0, cards=_two_color_cards(lands=15))
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=rows)
    _, decks = read_decks(path=path)

    records = build_records(decks=decks, index=_index())

    assert [r.lands for r in records] == [15]


def test_sources_count_basics_and_dual_lands() -> None:
    deck = measure_deck_rules.Deck(
        event="PremierDraft",
        build=0,
        main_colors="WU",
        splash_colors="",
        cards=_two_color_cards(),
        games=3,
        wins=3,
    )

    record = classify_deck(deck=deck, index=_index())

    assert record.lands == 17
    assert record.sources["W"] == 14
    assert record.sources["U"] == 4
    assert record.sources["B"] == 0
    assert record.in_color_duals == 1
    assert record.color_cards["W"] == 2
    assert record.double_symbol == [("White Twin", "W"), ("White Twin", "W")]
    assert record.kind == "2 colors"


def test_splash_deck_counts_splash_cards_and_unmatched_names() -> None:
    deck = measure_deck_rules.Deck(
        event="PremierDraft",
        build=0,
        main_colors="WU",
        splash_colors="B",
        cards={**_two_color_cards(), "Black Splash": 1, "Fixer Elf": 1, "Mystery": 2},
        games=3,
        wins=3,
    )
    unmatched: Counter[str] = Counter()

    record = classify_deck(deck=deck, index=_index(), unmatched=unmatched)

    assert record.kind == "2 + splash"
    assert [(c.name, n) for c, n in record.splash_cards] == [
        ("Black Splash // Back Side", 2)
    ]
    assert record.nonland_fixers == 1
    assert unmatched == Counter({"Mystery": 2})


def test_unknown_event_type_fails(tmp_path: Path) -> None:
    rows = _games(draft="x", wins=3, losses=0, event="TradDraft", cards={"Plains": 17})
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=rows)

    with pytest.raises(MeasureError, match="TradDraft"):
        read_decks(path=path)


def _stub_ratings() -> SimpleNamespace:
    return SimpleNamespace(
        primary=SimpleNamespace(card_ratings={1: object()}),
        rating_for=lambda *, grp_id: SimpleNamespace(
            letter_grade={9: "A", 6: "C", 7: "B"}.get(grp_id)
        ),
    )


def _patch_loaders(*, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        measure_deck_rules,
        "load_card_index",
        lambda *, set_code, app_dir: _index(),
    )
    monkeypatch.setattr(
        measure_deck_rules,
        "load_ratings",
        lambda *, set_code, event, app_dir: _stub_ratings(),
    )


def _full_rows() -> list[dict[str, Any]]:
    splash_cards = {**_two_color_cards(), "Black Splash": 1}
    rows = _games(draft="splash", wins=7, losses=1, splash="B", cards=splash_cards)
    rows += _games(draft="plain", wins=2, losses=2, cards=_two_color_cards())
    return rows


def test_main_prints_every_stat_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_loaders(monkeypatch=monkeypatch)
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=_full_rows())

    code = main(argv=["--game-data", str(path), "--set-code", "tst"])

    output = capsys.readouterr().out
    assert code == 0
    assert "PremierDraft: 2 decks, 1 winning decks" in output
    assert "== Deck kinds" in output
    assert "== 3-color decks (winning" in output
    assert "== 2-color + 1 splash decks (winning, n=1)" in output
    assert "splash sources, only single-symbol splash cards" in output
    assert "splash sources, with a two-symbol splash card (n=1)" in output
    assert "splash basics" in output
    assert "A- or better: 100%  B or better: 100%" in output
    assert "== Win rate by fixing pieces" in output
    assert "== Win rate by non-land fixer count" in output


def test_main_fails_when_ratings_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        measure_deck_rules,
        "load_card_index",
        lambda *, set_code, app_dir: _index(),
    )
    monkeypatch.setattr(
        measure_deck_rules,
        "load_cached_17lands_data",
        lambda **_: SimpleNamespace(primary=SimpleNamespace(card_ratings={})),
    )
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=_full_rows())

    code = main(argv=["--game-data", str(path), "--set-code", "TST"])

    assert code == 1
    assert "No cached 17Lands ratings for TST PremierDraft" in capsys.readouterr().err


def test_main_fails_for_unknown_event_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_loaders(monkeypatch=monkeypatch)
    rows = _games(draft="x", wins=3, losses=0, event="Sealed", cards={"Plains": 17})
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=rows)

    code = main(argv=["--game-data", str(path), "--set-code", "TST"])

    assert code == 1
    assert "Sealed" in capsys.readouterr().err


def test_main_opens_no_network_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    _patch_loaders(monkeypatch=monkeypatch)
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=_full_rows())

    assert main(argv=["--game-data", str(path), "--set-code", "TST"]) == 0


def test_real_loaders_open_no_network_connection_when_caches_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=_full_rows())
    argv = ["--game-data", str(path), "--set-code", "TST"]

    code = main(argv=[*argv, "--app-dir", str(tmp_path / "app")])

    assert code == 1
    assert "No cached card data for TST" in capsys.readouterr().err


def test_ratings_format_overrides_the_event_of_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def load_ratings(*, set_code: str, event: str, app_dir: Path | None) -> Any:
        requested.append(event)
        return _stub_ratings()

    _patch_loaders(monkeypatch=monkeypatch)
    monkeypatch.setattr(measure_deck_rules, "load_ratings", load_ratings)
    path = _write_game_data(path=tmp_path / "data.csv.gz", rows=_full_rows())
    argv = ["--game-data", str(path), "--set-code", "TST"]

    assert main(argv=argv) == 0
    assert main(argv=[*argv, "--ratings-format", "QuickDraft"]) == 0
    assert requested == ["PremierDraft", "QuickDraft"]
