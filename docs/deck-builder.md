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

Conservative splashing is on by default and can be disabled with `--no-splash` or through the TUI configuration opened with `c`. The builder uses the same shared policy as live picks: one third color, at most two `A-` or better cards, and at most one off-color mana pip per card. One card requires three sources and two cards require four. The builder may plan one basic land of the splash color, so the remaining sources must be deterministic drafted fixing lands usable by the primary pair.

Three-color candidates use the same gate as live picks: the set's three-color game share must reach the pick engine threshold. In other sets only pairs are scored. A three-color deck is a candidate when its colors are balanced and the pool can fix them. For balance, each color must appear on at least 20% of the color marks across the deck's top 23 playable spells. For fixing, the pool must hold at least `triad_min_fixing_sources` non-basic cards that produce two or more of the deck's colors. A land counts only when every color it produces is one of the deck's colors, because the mana base plays no other nonbasic lands. A qualifying triad is ranked with the pairs by the same score, using the 17Lands win rate of that three-color deck. A three-color deck never gets a splash.

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
- `splash_max_cards = 2`
- `splash_elite_score_minimum = 70.0`
- `triad_min_fixing_sources = 3` dual lands, tri-lands or multicolor mana sources in the pool for a three-color deck
- shared splash grade floor: `A-`
- shared splash-color limit: `1`
- shared source targets: `3` for one card, `4` for two cards
- planned splash basics: at most `1`
- `main_color_source_floor = 7`
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

Drafted in-pair nonbasic lands are slotted first, up to the land-count target. A nonbasic land is in-pair when its produced mana is known and all produced colors are in the chosen pair. If splash cards are selected, nonbasic lands that produce the one splash color are included and the builder adds at most one basic of that color to meet the shared source target.

Remaining slots are basics. After reserving any required splash basic, the other basics are split proportionally to colored pips in the final spell list, with a 7-source floor for each main color when the available land slots can satisfy it. Ties and leftover basics round toward the color with heavier double-pip requirements.

A three-color deck splits its basics across all three colors by the same rules and plans no splash. The 7-source floor comes first, so when 17 lands cannot give every color seven sources, the basics go to the colors furthest below the floor. Only after that does the pip weighting apply.

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
- Grades of splashed cards: the minimum grade for a splash card.
- Win rate by non-land fixer count: which spells count as fixing.
