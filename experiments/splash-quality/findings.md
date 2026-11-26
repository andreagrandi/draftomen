# Splash card quality and base-color sources in winning FRA decks

Issue #1094. Run on 2026-11-26 with `scripts/measure_deck_rules.py` on the
17Lands public game data dumps for FRA Premier and Pick Two, retrieved on
2026-11-26. 17Lands published them on 2026-10-17 at
`https://data.17landscapes.com/analysis_data/game_data/`, which the 17Lands
public datasets API links to.

```
uv run python scripts/measure_deck_rules.py \
  --game-data game_data_public.FRA.PremierDraft.csv.gz \
  --set-code FRA

uv run python scripts/measure_deck_rules.py \
  --game-data game_data_public.FRA.PickTwoDraft.csv.gz \
  --set-code FRA \
  --ratings-format PremierDraft
```

TRK has no public game dump yet, so FRA stands in for it. Both runs grade
cards with the cached FRA Premier ratings.

All figures come from winning decks with two main colors and one splash
color. A winning deck has 7 wins in Premier and 4 in Pick Two.

## 1. Splash card quality

For each splashed card, the script subtracts the median GIH win rate of the
deck's main-color spells from the card's GIH win rate. Decks with fewer than
10 rated main-color spells are skipped.

| Event | Splashed cards | p5 | p10 | p15 | p20 | p25 | p50 | At least -0.015 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Premier | 3,281 | -0.026 | -0.015 | -0.008 | -0.004 | -0.001 | +0.016 | 90% |
| Pick Two | 4,123 | -0.032 | -0.018 | -0.010 | -0.005 | -0.001 | +0.016 | 88% |

The same gap against the deck's weakest main-color spell:

| Event | p5 | p10 | p15 | p20 | p25 | p50 |
| --- | --- | --- | --- | --- | --- | --- |
| Premier | +0.013 | +0.023 | +0.030 | +0.034 | +0.039 | +0.060 |
| Pick Two | +0.011 | +0.023 | +0.031 | +0.036 | +0.041 | +0.063 |

Winning decks rarely splash a card much weaker than their typical main-color
spell. Half of the splashed cards beat the median main-color spell by 1.6
points or more.

## 2. Base-color sources

The main color with more cards is the primary color, and the other is the
secondary color. The table counts land sources of each.

| Event | Decks | Primary at 8 or more | Secondary at 7 or more |
| --- | --- | --- | --- |
| Premier | 1,766 | 92% | 92% |
| Pick Two | 2,052 | 84% | 88% |

Source distributions:

- Premier primary: 7 in 8%, 8 in 46%, 9 in 35%, 10 in 10%.
- Premier secondary: 6 in 6%, 7 in 29%, 8 in 45%, 9 in 16%.
- Pick Two primary: 7 in 16%, 8 in 53%, 9 in 25%, 10 in 5%.
- Pick Two secondary: 6 in 10%, 7 in 36%, 8 in 41%, 9 in 9%.

Decks split by whether any land makes the splash color:

| Event | Group | Decks | Share | Primary 8+ and secondary 7+ |
| --- | --- | --- | --- | --- |
| Premier | no splash dual | 269 | 15% | 60% |
| Premier | 1+ splash dual | 1,497 | 85% | 89% |
| Pick Two | no splash dual | 437 | 21% | 47% |
| Pick Two | 1+ splash dual | 1,615 | 79% | 81% |

The most common land sources in decks with no splash dual, as primary,
secondary and splash:

- Premier: 8/7/2 in 15%, 7/7/3 in 12%, 8/8/2 in 10%, 8/7/3 in 9%, 8/8/1 in 8%.
- Pick Two: 7/7/3 in 15%, 8/7/2 in 12%, 8/8/2 in 9%, 8/7/3 in 8%, 8/8/1 in 7%.

With 17 lands and no splash dual, the deck cannot keep 8 and 7 base sources
and also reach the 3 or 4 splash sources the builder requires. Winning decks
without a splash dual split between giving the splash fewer sources and
giving the primary color fewer. The approved rule keeps the base minimums and
cuts the splash instead.

## Approved rules

- A splash card needs a GIH win rate no lower than 1.5 points below the
  median of the pool's best 23 on-pair spells.
- When a splash is added, the main color with more cards keeps at least 8
  land sources and the other main color keeps at least 7.
- When the lands left after those minimums cannot give the splash its 3 or 4
  sources, the builder cuts the splash cards and builds without them.
