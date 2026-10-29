# Splashes in aggressive winning FRA decks (#1040)

Run on 2026-10-29 with `run.py` on the 17Lands public game data dumps for FRA
Premier and Pick Two, both retrieved on 2026-10-29.

```
uv run python experiments/aggressive-splash/run.py \
  --game-data game_data_public.FRA.PremierDraft.csv.gz \
  --set-code FRA
```

## Method

- A winning deck reached 7 wins in Premier and 4 wins in Pick Two, counted
  from its games in the game data. Each draft uses its latest deck build.
- Only decks with two main colors are counted, since the app tests speed
  against a base pair.
- A deck is aggressive when `_pool_is_aggressive` in `draftomen/splash.py`
  says so for the deck's cards and its main colors: at least 8 spells
  castable in the pair, an average mana value of 2.7 or less, and at least 30%
  two-drops among them. Copies count once each.
- A splash deck has a splash color, as 17Lands labels it, and at least one
  spell with a symbol of that color. Those spells are the splashed cards, one
  per copy.
- Grades come from the cached Premier ratings in both events, since Pick Two
  ratings for FRA are not cached.

## Sample

| Event | Winning decks | Two main colors | Aggressive | Other |
| --- | --- | --- | --- | --- |
| Premier | 4,098 | 3,277 | 355 | 2,922 |
| Pick Two | 4,450 | 3,162 | 319 | 2,843 |

## 1. Splash rate

| Event | Speed | Splash rate | Splash cards per splash deck | Splash decks with only `A` or better |
| --- | --- | --- | --- | --- |
| Premier | aggressive | 33% (118 of 355) | 1.9 | 1% |
| Premier | other | 43% (1,248 of 2,922) | 2.1 | 3% |
| Pick Two | aggressive | 37% (118 of 319) | 2.2 | 1% |
| Pick Two | other | 44% (1,251 of 2,843) | 2.2 | 2% |

Aggressive winning decks splash a little less often than other decks, but a
third of them still splash. They splash about as many cards as other decks.

The last column counts splash decks whose splashed cards are all graded `A`
or better, which is what the current gate allows. It would permit 1% of the
splashes that aggressive winning decks played.

## 2. Grades of splashed cards

| Grade | Premier aggressive (n=223) | Premier other (n=2,595) | Pick Two aggressive (n=262) | Pick Two other (n=2,799) |
| --- | --- | --- | --- | --- |
| A+ | 2% | 5% | 5% | 3% |
| A | 0% | 0% | 0% | 0% |
| A- | 13% | 14% | 8% | 11% |
| B+ | 9% | 9% | 8% | 7% |
| B | 17% | 18% | 21% | 19% |
| B- | 20% | 16% | 18% | 14% |
| C+ | 22% | 19% | 20% | 21% |
| C | 10% | 13% | 12% | 14% |
| C- | 3% | 3% | 4% | 4% |
| D+ | 1% | 2% | 3% | 3% |
| D | 2% | 1% | 2% | 2% |
| D- | 0% | 0% | 1% | 0% |

Aggressive decks splash cards of the same grades as other decks. `A` or
better makes up 2 to 5% of splashed cards in both groups. `C+` and `B` are
the most common grades in both.

## Proposal

Drop the `A` gate for aggressive pools. An aggressive pool should use the
same splash rule as any other pool: a splash card goes in when it beats the
card it replaces, with no minimum grade.

The data supports this:

- A third of aggressive winning decks splash, against 43 to 44% of other
  decks. Speed lowers the splash rate a little but does not stop splashing.
- The current gate would allow 1% of the splashes aggressive winning decks
  played.
- The grades of splashed cards in aggressive decks match those of other
  decks, so a different grade rule for aggressive pools has no support.

The code change is a separate ticket, for approval. It would remove
`aggressive_minimum_grade` from `SplashConfig` and the aggressive branch in
`assess_splash_card`. Whether `SplashState.aggressive` still has another use
should be checked then.

## Limits

- Win rates are not compared. The shares describe what winning decks did,
  not whether a splash made an aggressive deck win more.
- The app tests the drafted pool during the draft. This run tests the final
  deck, which leaves out sideboard cards, so the two can differ.
- Winning decks count wins in the game data, which holds fewer drafts than the
  draft data, so the counts differ from #1028.
- The aggressive groups are small, 118 splash decks in each event.
- Both events use Premier grades.
