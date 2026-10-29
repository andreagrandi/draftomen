# Deck builder

The `build` subcommand runs the v1 deck-builder stages that are available in plain CLI mode:

1. Score all two-color pairs from the drafted pool. In sets where three-color decks are common, also score the ten three-color decks next to the pairs.
2. Select deck spells for the chosen pair under cached empirical targets or Limited fallback defaults.
3. Add a mana base and print a 40-card build sheet with bench cards.

`draftomen-tui replay` and `draftomen-tui watch --plain` also run these stages automatically when a draft completion event is parsed. Running `draftomen-tui` with no subcommand starts watch mode and scans startup logs by default, so a just-finished draft should open directly into the build view.

## Eligible spells

The spell selector always considers cards that are castable in the chosen pair:

- cards whose colors are all in the chosen pair;
- colorless nonland spells, including colorless artifacts;
- in-pair artifacts and other in-pair spells.

Conservative splashing is on by default and can be disabled with `--no-splash` or through the TUI configuration opened with `c`. The builder uses the same shared policy as live picks: one third color and at most three cards, each with one or two symbols of the splash color and no symbol of another outside color. A splash with only one-symbol cards needs 3 land sources of the splash color, and a splash with a two-symbol card needs 4. Basics of the splash color count fully, so the builder adds as many as the drafted fixing lands leave short. `scripts/measure_deck_rules.py` found on the 17Lands FRA PremierDraft dump retrieved 2026-10-29 that one splash color was played in 87 to 89% of winning splash decks, nearly all of them splashed 1 to 3 cards, and most had 3 or 4 splash sources. 14 to 22% had no dual land for the splash and used 2 or 3 basics. Two-symbol cards were 14% of splashed cards, and 70% of decks splashing one had 4 or more splash sources.

A splash card has no minimum grade, but it needs a 17Lands rating. Of the cards that winning decks splashed in the same dump, only 17% (Premier) and 14% (Pick Two) were `A-` or better, 40% and 36% were `B` or better, and the most common grade was `C+` at 26%. A splash card goes in only when it beats the card it replaces, so the optimizer's score ranking decides, and a third-color card sets the splash color only when its 17Lands win rate beats the weakest of the pool's best 23 on-pair spells.

Three-color candidates use the same gate as live picks: the set's three-color game share must reach the pick engine threshold. In other sets only pairs are scored. A three-color deck is a candidate when each of its colors appears on at least 4 of the deck's top 23 playable spells (`triad_min_color_cards = 4`). A color with 1 to 3 cards is a splash, so splash rules apply instead. 17Lands labels a third color with 1 to 3 cards a splash and 4 or more a main color, and in 98% of three-color decks the lightest color has 4 or more cards. A pool with no fixing still gets its balanced three-color decks scored, and the fixing adjustment below decides how much fixing helps. A qualifying triad is ranked with the pairs by the same score, using the 17Lands win rate of that three-color deck. The builder moves that win rate by the measured gap between FRA PremierDraft three-color decks with the same number of fixing pieces and all three-color decks. The gaps are -18.5 points for 0 pieces, -7.5 for 1, -4.9 for 2, -2.3 for 3, -1.6 for 4, -0.3 for 5 and +2.0 for 6 or more. `scripts/measure_deck_rules.py` measured them on the 17Lands FRA PremierDraft dump retrieved 2026-10-29. The script counts the fixing pieces in the deck, while the builder counts them in the pool. Two-color decks get no adjustment. The build sheet prints the fixing piece count and the adjustment next to each three-color deck. A three-color deck never gets a splash.

The builder sorts each pool card into one of three fixing groups for a deck's colors:

- Land source: a nonbasic land that makes two or more of the deck's colors and no other color, because the mana base plays no other nonbasic lands.
- Non-land fixer: a card with basic landcycling, a permanent that taps for one mana of any color, or a card that creates a Treasure token.
- Not fixing: every other card. This includes basic lands and a spell such as Soul Tether, the prepared spell of Konstrari Improviser, whose token taps for two colors only after a second spell is cast.

Source minimums count land sources only. Fixing counts for three-color decks count both land sources and non-land fixers.

## Structural defaults

All tunables live in `draftomen/config.py` under `DeckBuilderConfig`:

- `deck_size = 40`
- `target_spell_count = 23`
- `default_land_count = 17`
- `aggressive_land_count = 16` when average mana value is low and the 2-drop quota is filled
- `top_heavy_land_count = 18` when average mana value is high
- `creature_floor = 14`
- `creature_ceiling = 17`
- `minimum_two_drops = 5` at mana value 2
- `maximum_expensive_spells = 3` at mana value 6 or greater
- `near_tie_creature_preference_points = 2.0`
- `maximum_unresolved_metadata_ratio = 0.25`
- `splash_max_cards = 3`
- `triad_fixing_win_rate_gaps` moves a three-color deck's win rate by its fixing piece count, from -18.5 points at 0 pieces to +2.0 points at 6 or more
- shared splash grade floor: none, a splash card only has to beat the card it replaces
- shared splash-color limit: `1`
- shared splash symbol limit: `2` symbols of the splash color per card
- shared source targets: `3` for one-symbol cards, `4` when a card has two symbols
- splash basics: as many as the drafted fixing lands leave short of the source target
- `main_color_source_floor = 7`
- `triad_min_color_land_sources = 4`
- `double_symbol_min_color_land_sources = 6`
- `structure_maindeck_rate_threshold = 0.5`

These are the fallback FR-5.6 structural defaults: established Limited consensus values used when no empirical 17Lands target cache exists for the selected set and pair.

Empirical targets can be computed from 17Lands public draft-data dumps with `refresh-structure-targets`. The command groups trophy drafts by deck, computes average creature count, curve shape, and land count per two-color pair, and caches the small aggregate under the normal 17Lands cache directory. The builder loads those targets automatically in offline `build`, `replay`, and watch flows when the cache is present.

The spell selector uses a bounded whole-deck optimizer rather than a greedy walk. It first scores the filtered, quantity-limited candidate tuple, then keeps the best `optimizer_beam_width` partial decks at each selection depth. Complete feasible decks are ranked by one deterministic objective (higher is better):

- individual card quality (`optimizer_quality_weight = 1.0`);
- curve shape, including the two-drop quota, expensive-spell cap, and average mana value (`optimizer_curve_weight = 0.12`);
- creature-count structure (`optimizer_creature_structure_weight = 0.12`).

When the existing `SetProfile` and canonical pair from pair selection provide usable evidence, complete-package ranking adds bounded terms for:

- profile role-target fit and effective-removal coverage;
- enabler/payoff balance, including draw-second packages;
- semantic package synergy, evaluated from selected package membership rather than the entire drafted pool;
- empirical pair/card context;
- redundancy and unsupported-payoff penalties; and
- mana strain.

Semantic package evidence is separate from empirical pair evidence. Pair synergy and scarcity values are multiplied by a deterministic bounded sample-strength factor: an entry's `samples` value when present, otherwise the canonical pair sample count. Missing or zero samples contribute no empirical effect; the pair sample count itself is not a standalone ranking signal. Signed pair synergy is retained, so negative evidence lowers a package's bounded objective contribution. Profile confidence and maturity still bound the overall influence as confidence × maturity. They never replace the hard feasibility checks below.

After the beam completes, deterministic local improvement tries up to `optimizer_local_improvement_rounds = 2` rounds, considering at most `optimizer_local_improvement_candidates = 8` replacement candidates per round. `optimizer_max_search_nodes = 32768` caps expanded search nodes and `optimizer_max_evaluations = 4096` caps complete-package objective evaluations. Together with the beam width of `24`, these explicit limits bound ordinary 40–50-card pools: the selector does not enumerate every deck.

The frozen HOB draw-second regression compares the optimized package with the prior greedy baseline. Multiple `Master's Councillor` copies must bring at least one baseline-omitted `Patient Instructor` into the package. Removing the draw-second payoffs removes that advantage; adding another weak enabler does not force every related copy into the deck. A no-profile replay must preserve the generic fallback.

Candidate order is the existing score order. Beam states, replacements, and final ties preserve stable candidate order and card identity, so repeated runs with the same pool, ratings, and configuration return the same package.

The optimizer changes only package ranking. It still enforces pool quantity limits, the requested spell count, creature floor and ceiling, two-drop minimum, expensive-spell cap, and splash eligibility/limits. If a pool is infeasible, the existing relaxation order remains: expensive-spell cap, minimum two-drop quota, creature ceiling, creature floor, then eligible-card shortage.
When the curve calls for 16 or 18 lands, the builder reselects the spell count to keep the final deck exactly 40 cards.

Before pair selection, the builder validates that enough picked cards have card metadata to identify playable spells reliably. If too much metadata is missing, or unresolved cards make the playable count fall below the spell target, the builder refuses to print a deck and asks the user to refresh card data or pass a current bulk file.

## Mana base

Drafted in-pair nonbasic lands are slotted first, up to the land-count target. A nonbasic land is in-pair when its produced mana is known and all produced colors are in the chosen pair. If splash cards are selected, nonbasic lands that produce the one splash color are included and the builder adds basics of that color until the deck meets the shared source target of 3 or 4.

Remaining slots are basics. After reserving the splash basics, the other basics are split proportionally to colored pips in the final spell list, with a 7-source floor for each main color when the available land slots can satisfy it. Ties and leftover basics round toward the color with heavier double-pip requirements.

A three-color deck splits its basics across all three colors by the same rules and plans no splash. Every color first gets at least 4 land sources (`triad_min_color_land_sources = 4`). A dual land counts toward each color it makes, and non-land fixers do not count. `scripts/measure_deck_rules.py` found that the lightest color of a winning three-color deck had 4 or more land sources in about 90% of decks, on the 17Lands FRA PremierDraft dump retrieved 2026-10-29. Two-color decks have no such minimum. The 7-source floor comes next, so when 17 lands cannot give every color seven sources, the basics go to the colors furthest below the floor. Only after that does the pip weighting apply.

A spell with two or more symbols of one main color, such as `{1}{B}{B}`, needs at least 6 land sources of that color (`double_symbol_min_color_land_sources = 6`). Hybrid symbols do not count. The basic split meets this floor after the three-color minimum of 4 and before the 7-source floor. When the floor changes the split, the build sheet adds a mana note that names the card and says the builder added basics of its color. When no split can reach 6, the builder cuts the card, selects spells again without it, and adds a mana note that names the cut card. The same rule applies to two-color decks, where the 7-source floor almost always covers it already. `scripts/measure_deck_rules.py` found that about 85% of cards with two symbols of one color in winning three-color decks had 6 or more land sources of that color, and 8% had 5. The figure comes from the 17Lands FRA PremierDraft dump retrieved 2026-10-29.

For 16-land aggressive builds, the output prints the caveat that basics can be preferable to slow taplands when curve pressure matters.

## Build sheet

Plain output includes:

- chosen pair and its 17Lands win rate context;
- exact deck size, spell count, and land count;
- a similarity line when empirical 17Lands structure targets exist;
- spells sorted by curve with creatures and non-creatures separated;
- lands with nonbasics first, then basic counts;
- a bench of the nearest cuts with one-line reasons.

## Relaxation order

When the pool cannot satisfy every default and still produce the target spell count, constraints are relaxed in this order:

1. expensive-spell cap;
2. minimum two-drop quota;
3. creature ceiling;
4. creature floor;
5. eligible-card shortage.

The build sheet prints both the relaxation order and the relaxations that were actually applied.

## Measuring winning decks

`scripts/measure_deck_rules.py` measures how winning 17Lands decks were built. It reads local files only and never opens a network connection.

```
uv run python scripts/measure_deck_rules.py --game-data game_data_public.FRA.PremierDraft.csv.gz --set-code FRA
```

The game data file is a 17Lands public dump for PremierDraft or PickTwoDraft. The script needs the set's cached card data and cached 17Lands ratings, and stops with an error if either is missing. The ratings come from the event of the game data file. Pass `--ratings-format PremierDraft` or `--ratings-format QuickDraft` to grade with another format, for example when no PickTwoDraft ratings are cached. Use `--app-dir` to point at a data directory other than `~/.draftomen`.

A winning deck has 7 or more wins in PremierDraft and 4 or more in PickTwoDraft. Decks need at least 3 games and 15 to 19 lands. A source is a land that makes the color. A non-land fixer is a spell that makes two-color or any-color mana, makes a Treasure, or searches for a basic land, which includes basic landcycling.

Each stat feeds these rules:

- Share of two-color, splash and three-color decks, with win rates: how often the builder should pick a three-color deck or a splash.
- Lightest color card share percentiles: the share of cards each color of a three-color deck needs.
- In-color dual lands and win rate by fixing pieces: the three-color score by fixing pieces, and three-color decks without dual lands.
- Sources of the lightest color: the land minimum for each color of a three-color deck.
- Sources behind each double-symbol card: the source count for cards with two symbols of one color.
- Splash cards per deck: the card count that separates a splash from a third main color.
- Splash sources and splash basics: how many sources a splash needs and whether basics can supply them.
- Grades of splashed cards: why a splash card needs no minimum grade.
- Win rate by non-land fixer count: which spells count as fixing.
