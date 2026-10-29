# Speculative splash picks in winning FRA drafts (#1028)

Run on 2026-10-29 with `run.py`. It joins the 17Lands public draft data dump
with the game data dump of the same event. The Premier draft data was
retrieved on 2026-10-26, the Pick Two draft data and both game data dumps on
2026-10-29.

```
uv run python experiments/speculative-splash/run.py \
  --draft-data draft_data_public.FRA.PremierDraft.csv.gz \
  --game-data game_data_public.FRA.PremierDraft.csv.gz \
  --set-code FRA --event PremierDraft
```

## Method

- Winning drafts reached 7 wins in Premier and 4 wins in Pick Two. Each draft
  is joined to its latest deck build in the game data.
- A splashed card is a spell with a symbol of the splash color in a deck with
  two main colors and one splash color, as 17Lands labels the deck.
- Picks are numbered by card, 1 to 42. Pick Two takes two cards per pack, so
  each pack still covers 14 cards.
- Phases follow the pick engine: picks 1 to 5 are open, 6 to 15 ramp up the
  color commitment, and 16 onward are locked.
- The pair at pick time is the engine's heaviest pair from the pool weights
  before the pick. Triads are ignored. Open picks have no pair.
- An off-color pick is a spell outside that pair which the splash code would
  accept as a splash: one outside color with at most 2 symbols.
- Outcomes: splashed if the deck splashes its color, main color if the deck
  plays its color as a main color, sideboard if the card is not in the deck.
- Grades come from the cached Premier ratings in both events, since Pick Two
  ratings for FRA are not cached.

## Sample

| Event | Winning drafts | With a deck | Two colors plus one splash | Splashed cards |
| --- | --- | --- | --- | --- |
| Premier | 5,614 | 5,599 | 1,685 | 3,103 |
| Pick Two | 4,562 | 4,554 | 1,213 | 2,399 |

## 1. When splashed cards were taken

The median splashed card was pick 18 in Premier and pick 19 in Pick Two.

| Taken in | Premier | Pick Two | Off the pair at pick time, Premier | Pick Two |
| --- | --- | --- | --- | --- |
| pack 1 | 31% | 31% | 86% | 86% |
| pack 2 | 36% | 34% | 93% | 91% |
| pack 3 | 33% | 35% | 98% | 97% |
| picks 1 to 5 | 22% | 20% | - | - |
| picks 6 to 15 | 15% | 16% | 89% | 87% |
| picks 16 to 42 | 63% | 64% | 96% | 95% |

Most splashed cards were taken after pick 15, when the drafter's pair was
already set. Almost all of them were outside that pair when taken, so winning
drafters do take splash cards before they know they will splash.

## 2. Grades of splashed cards

| Grade | Premier | Pick Two |
| --- | --- | --- |
| A+ | 4% | 3% |
| A | 0% | 0% |
| A- | 13% | 11% |
| B+ | 10% | 7% |
| B | 18% | 20% |
| B- | 17% | 13% |
| C+ | 19% | 21% |
| C | 13% | 15% |
| C- | 3% | 4% |
| D+ | 2% | 4% |
| D | 1% | 2% |

The share of A- or better cards is the same in every phase, 17 to 19% in
Premier and 15% in Pick Two. Early splash picks are not of higher grade than
late ones.

## 3. What happened to off-color picks

Each row counts splashable picks outside the drafter's pair at pick time.

### Premier

| Phase | Grade | Picks | Splashed | Main color | Sideboard |
| --- | --- | --- | --- | --- | --- |
| picks 6 to 15 | A- or better | 447 | 26% | 62% | 12% |
| picks 6 to 15 | B- to B+ | 2,212 | 11% | 51% | 38% |
| picks 6 to 15 | C+ or lower | 9,477 | 2% | 24% | 74% |
| picks 6 to 15 | all | 12,411 | 4% | 29% | 66% |
| picks 16 to 42 | A- or better | 1,289 | 36% | 49% | 16% |
| picks 16 to 42 | B- to B+ | 6,007 | 18% | 42% | 39% |
| picks 16 to 42 | C+ or lower | 18,779 | 5% | 19% | 76% |
| picks 16 to 42 | all | 26,779 | 9% | 25% | 65% |

### Pick Two

| Phase | Grade | Picks | Splashed | Main color | Sideboard |
| --- | --- | --- | --- | --- | --- |
| picks 6 to 15 | A- or better | 379 | 19% | 65% | 16% |
| picks 6 to 15 | B- to B+ | 2,237 | 8% | 48% | 44% |
| picks 6 to 15 | C+ or lower | 9,966 | 2% | 29% | 68% |
| picks 6 to 15 | all | 12,776 | 4% | 33% | 63% |
| picks 16 to 42 | A- or better | 1,107 | 27% | 44% | 29% |
| picks 16 to 42 | B- to B+ | 6,233 | 13% | 40% | 46% |
| picks 16 to 42 | C+ or lower | 20,712 | 4% | 25% | 71% |
| picks 16 to 42 | all | 28,603 | 7% | 28% | 64% |

The "all" rows also hold a few hundred ungraded picks, of which 1% or fewer
made the deck. Twenty-two picks went into a deck that labels their color
neither main nor splash. They count in the picks column but in none of the
outcome columns.

An off-color pick more often ends up in a main color than in a splash, even
after pick 15. Drafters change colors, so the engine's "locked" phase is not a
lock for them. A- or better off-color cards made the deck 84 to 88% of the
time when taken in picks 6 to 15. B grades made it about 60% of the time and
C+ or lower about 30%, mostly as a main color.

## The current rule

The rule this issue questions no longer runs. Since #1036, the deck plays
basics for the splash, so `assess_splash_card` in `draftomen/splash.py`
always counts enough sources. Every graded card that passes the other splash
checks is now `splash-ready`, whatever its grade. The `splash-speculative`
branch needs an A grade and a pool that is not aggressive, and those cards are
already `splash-ready`, so it cannot be reached.

`speculative_minimum_grade` now does one thing: an aggressive pool only marks
an off-color card splash-ready when it is graded A or better. In FRA, 0% of
splashed cards were graded A and 3 to 4% A+, so aggressive pools almost never
splash.

## Proposal

Keep the current pick rule. Do not add a grade floor for off-color picks.

In plain words: after pick 5, an off-color card that is better than every
on-color card in the pack keeps 95% of its score at full commitment, at any
grade. The comparison with the best on-color card already stops weak
off-color cards from winning the pick.

The data supports this:

- Winning drafters take splash cards at every grade and in every phase.
  83 to 86% of splashed cards were below A-. Of those taken after pick 5,
  93 to 95% were off the pair when taken.
- A grade floor would block the B and C+ cards that make up most splashes.
- High-grade off-color picks are rarely wasted. Those graded A- or better
  made the deck 84 to 88% of the time from picks 6 to 15.

Two follow-ups, for approval before any code change:

1. Remove the unreachable `splash-speculative` classification, its 0.85
   multiplier and its labels in the TUI and replay code.
2. Measure the aggressive pool gate separately. This run does not split decks
   by speed, so it cannot tell whether the A grade gate is right.

## Limits

- Win rates are not compared. Winning drafters are better players, so the
  shares describe what good drafters do, not what wins.
- The pick-time pair uses the engine's weights and ignores triads, so some
  picks counted as off-color were in a third main color.
- Both events use Premier grades.
- A card picked more than once is matched to deck copies in pick order.
