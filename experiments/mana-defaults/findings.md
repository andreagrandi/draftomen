# Land counts, main color sources and splash symbols in winning FRA decks

Run on 2026-10-29 with `run.py` on the 17Lands public game data dumps for FRA
Premier and Pick Two, both retrieved on 2026-10-29.

```
uv run python experiments/mana-defaults/run.py \
  --game-data game_data_public.FRA.PremierDraft.csv.gz \
  --set-code FRA
```

## Method

- Decks come from `build_records` in `scripts/measure_deck_rules.py`: the
  latest build of each draft, 3 or more games and 15 to 19 lands. A winning
  deck reached 7 wins in Premier and 4 wins in Pick Two.
- Land counts and curves use decks with two main colors, with or without a
  splash.
- The curve follows `_curve_land_count` in `draftomen/deckbuilder.py` as it
  was before this change. A deck is top-heavy when its spells average a mana
  value of 3.4 or more or it plays more than 3 spells of mana value 6 or more.
  It is aggressive when its spells average 2.7 or less and it plays 5 or more
  two-drops. Every other deck is default.
- A land source is a land that makes the color.
- Splash symbols count the symbols of the splash color on each splashed card,
  one per copy, in decks with two main colors and one splash color.

## Sample

| Event | Winning decks | Two main colors | Three main colors | Single-splash decks |
| --- | --- | --- | --- | --- |
| Premier | 4,093 | 3,273 | 737 | 1,270 |
| Pick Two | 4,444 | 3,159 | 1,182 | 1,221 |

## 1. Land count by curve

### Premier

| Curve | Decks | 15 lands | 16 lands | 17 lands | 18 lands |
| --- | --- | --- | --- | --- | --- |
| aggressive | 305 | 4% | 30% | 66% | 0% |
| default | 2,362 | 1% | 22% | 77% | 0% |
| top-heavy | 606 | 2% | 19% | 78% | 1% |

### Pick Two

| Curve | Decks | 15 lands | 16 lands | 17 lands | 18 lands |
| --- | --- | --- | --- | --- | --- |
| aggressive | 286 | 0% | 33% | 66% | 0% |
| default | 2,282 | 1% | 18% | 80% | 1% |
| top-heavy | 591 | 1% | 12% | 85% | 2% |

No deck played 19 lands. The median is 17 lands for every curve.

By average mana value, the share of 16-land decks falls from 38 to 41% at 2.5
or less to 12 to 18% at 3.4 or more. The share of 18-land decks stays at 2% or
less in every bucket.

## 2. Land sources of the lightest main color

| Decks | Event | p10 | p25 | p50 | p75 | p90 | 7 or more |
| --- | --- | --- | --- | --- | --- | --- | --- |
| two main colors | Premier | 7 | 8 | 8 | 9 | 9 | 96% |
| two main colors | Pick Two | 7 | 7 | 8 | 8 | 9 | 93% |
| three main colors | Premier | 3 | 4 | 5 | 6 | 6 | 10% |
| three main colors | Pick Two | 4 | 4 | 5 | 6 | 6 | 7% |

## 3. Splash symbols per splashed card

| Symbols of the splash color | Premier (n=2,334) | Pick Two (n=2,412) |
| --- | --- | --- |
| 1 | 86% | 86% |
| 2 | 14% | 14% |
| 3 or more | 0% | 0% |

3 of 1,270 Premier and 2 of 1,221 Pick Two single-splash decks played a card
with 3 or more splash symbols.

## Decisions

- Land count: every deck plays 17 lands. Most winning decks play 17 at every
  curve, and 18 lands is almost never played. Aggressive decks play 16 more
  often than others, but still play 17 two times in three. The 2.7 and 3.4
  mana value bounds stay, since the spell optimizer uses them to score curve
  shape.
- `main_color_source_floor = 7` stays. 93 to 96% of winning two-color decks
  have 7 or more sources of their lighter main color. In three-color decks the
  floor is a preference and `triad_min_color_land_sources = 4` is the minimum.
- The splash limit of 2 symbols per card stays. Winning decks almost never
  splash a card with 3.

The aggressive pool test in `SplashConfig` decides nothing since #1043. The
splash assessment only records it.

## Limits

- Win rates are not compared. The shares describe what winning decks played.
- Decks with fewer than 15 or more than 19 lands are left out by
  `build_records`.
- A card with a land only on its back face counts as a spell, so a deck's
  land count can understate its mana sources.
- Only FRA was measured.
