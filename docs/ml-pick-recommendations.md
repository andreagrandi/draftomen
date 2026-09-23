# ML-based pick recommendations

This document evaluates a future machine-learning recommendation track for
Draft Omen. It is a research and architecture decision, not a claim that the
current heuristic should be replaced. DO Score remains the production default
and the existing trophy-draft benchmark remains the baseline.

## Decision

The first experiment should be an offline, set-and-format-specific
learning-to-rank model trained from 17Lands public draft dumps. Each pick is one
ranking group, each offered card is one candidate, and the human-selected card
is the relevance label. A LightGBM LambdaRank model is the recommended first
prototype because the data is tabular, packs have variable sizes, training can
group candidates by pick, and prediction produces one score per candidate.

The model must begin as an offline comparison only. It may advance to a separate
runtime-integration issue only after it beats DO Score on held-out data under the
evaluation gate below. Even then, Draft Omen should add a model-neutral
recommendation layer and retain the heuristic as an automatic fallback. The ML
model should augment the current engine before it is considered as a
replacement.

Deep card-text or sequence models are not the first experiment. They add feature
and distribution complexity before a tabular ranker has established whether
historical pick data improves on the current baseline.

## Historical data sources

### Primary source: 17Lands public draft data

The [17Lands Public Data page](https://www.17lands.com/public_datasets) publishes
per-set and per-format draft dumps. A current `TMT` `PremierDraft` header was
checked while preparing this design. It contains:

- draft metadata: `expansion`, `event_type`, `draft_id`, `draft_time`, and
  `rank`;
- position: `pack_number` and `pick_number`;
- offered-card counts in `pack_card_<card name>` columns;
- the pool before the pick in `pool_<card name>` columns;
- selected cards in `pick` and, where applicable, `pick_2`;
- downstream fields such as `pick_maindeck_rate`,
  `pick_sideboard_in_rate`, `event_match_wins`, and `event_match_losses`;
- coarse player-history fields such as `user_n_games_bucket` and
  `user_game_win_rate_bucket`.

This is sufficient for a pick-ranking dataset without scraping 17Lands APIs.
The first experiment should exclude rows with a non-empty `pick_2` so every
ranking group has exactly one positive candidate. It should report how many
rows this removes. The `pool_*` columns should be cross-checked against the
sequence reconstructed from earlier `pick` values; disagreements should be
skipped and counted rather than silently repaired.

### Optional outcome source: 17Lands public game data

The public game dump can be joined to draft rows by `draft_id`. A current TMT
header includes `game_time`, build, match and game numbers, ranks, deck and hand
card counts, turn count, and `won`. It is feasible for secondary analyses of
whether model agreement varies with downstream results.

Game results must not become features for the pick being predicted. They may be
used only as evaluation strata, an auxiliary target, or an explicitly separate
sample-weighting experiment. Because only the outcome of the cards actually
picked is observed, game data cannot establish what would have happened after
an unchosen recommendation.

### Card metadata

The existing Scryfall-backed `CardDatabase` already supplies Arena card ID,
name, colors, mana value, rarity, types, mana cost, and produced mana. The first
model should reuse those fields and should not add a second card-data pipeline.
Oracle text, power/toughness, images, and embeddings are outside the initial
experiment. That limitation makes the first model set-specific rather than a
day-one model for unseen cards.

### Sources not selected for the first experiment

- 17Lands replay data is much larger and describes game actions rather than the
  decision context needed for pick ranking. Revisit it only for a later
  gameplay-aware research question.
- The card-ratings and color-ratings endpoints already used by Draft Omen
  remain inputs to the heuristic baseline. They should not be scraped into a
  training corpus: 17Lands discourages automated API scraping and does not
  guarantee stable endpoint shapes.
- Local Draft Omen histories are suitable for private, user-controlled
  backtests, but they are too small and potentially identifiable for a shared
  training corpus. They must not be uploaded or collected automatically.

## Usage, licensing, and privacy constraints

The [17Lands usage guidelines](https://www.17lands.com/usage_guidelines) say
public datasets are typically released under CC BY 4.0 and recommend public
dumps instead of API scraping. They also describe an expected publication
schedule of roughly two weeks for draft data, three weeks for game data, and six
weeks for replay data. Consequently, a set-specific model cannot be a day-one
fallback for a new set.

Before using a dump, record the exact source URL, retrieval date, and license
shown for that file. If a dump does not carry an explicit compatible license,
do not train or redistribute an artifact from it until the terms are clarified.
Model manifests and any published results must attribute 17Lands without
implying endorsement. The site's
[Terms of Service](https://www.17lands.com/terms_of_service) continue to govern
site and API access separately from a dataset-specific license.

The [17Lands privacy policy](https://www.17lands.com/privacy) says published data
is anonymized but cannot be guaranteed impossible to trace to an individual.
Treat `draft_id` as a pseudonymous grouping key: hash or remap it in intermediate
artifacts, never use it as a feature, and never commit raw rows, trained data
matrices, or row-level predictions. Only aggregate metrics and model artifacts
whose license permits distribution may leave the local research environment.

Scryfall-derived metadata should remain subject to the project's existing
attribution and Wizards Fan Content notices. This research does not redistribute
Scryfall bulk data or card images.

## Training examples and features

Create one candidate row for each positive `pack_card_*` count. All candidate
rows from the same draft pick share a ranking-group ID derived from the remapped
draft ID, pack number, and pick number.

The primary relevance label is:

- `1` for the card named by `pick`;
- `0` for every other offered card.

This is an imitation-learning label: it means "the historical drafter chose
this card," not "this was objectively the best card." The baseline model gives
all eligible pick groups equal weight. A separate experiment may weight drafts
by match wins or compare trophy-only rows, but those results must be reported
separately and cannot replace the all-draft result.

Allowed features are available at recommendation time:

- candidate identity within the exact set and candidate colors, rarity, mana
  value, types, mana cost, and produced mana;
- counts and summary features for every card in the current pack;
- the pre-pick pool, including color counts, quality-weighted color fit, mana
  curve, card-type counts, and candidate similarity to the pool;
- zero-based pack and pick numbers plus the derived overall pick index;
- set code and event format.

Forbidden features either leak future information or encode user identity and
skill:

- `pick`, `pick_2`, `pick_maindeck_rate`, and `pick_sideboard_in_rate`;
- match wins/losses, game rows, final deck contents, and later picks;
- `draft_id`, timestamps as unique values, rank, opponent rank, and player
  history buckets;
- 17Lands aggregate statistics calculated using validation or test dates.

Aggregate card statistics may be tested only if they are recomputed strictly
from the training window. The primary experiment should omit them so the
comparison cannot benefit from post-cutoff information. DO Score should use one
frozen ratings snapshot across the comparison, matching the existing
retrospective benchmark; the report must identify that snapshot and note that
this is calibration rather than a time-causal simulation.

## Model and artifact design

Use LightGBM's
[`LGBMRanker`](https://lightgbm.readthedocs.io/en/stable/pythonapi/lightgbm.LGBMRanker.html)
with the `lambdarank` objective. Pass pack sizes as the ranking groups and
evaluate NDCG at ranks 1, 3, and 5 during training. Use a fixed seed, early
stopping on the validation window, and record all parameters. LightGBM is a
research-only dependency at this stage and must not be added to Draft Omen's
runtime dependency group.

Each future model artifact needs a manifest containing:

- artifact, feature-schema, and training-code versions;
- exact set and event format;
- training, validation, and test time ranges;
- source URLs, retrieval dates, and licenses;
- feature names and preprocessing rules;
- model parameters, random seed, and evaluation metrics;
- model checksum and the minimum compatible Draft Omen version.

Artifacts should be local and deterministic. Runtime inference must not call a
hosted model or upload draft state.

## Offline evaluation

Sort drafts by their earliest `draft_time`, then assign complete drafts to the
first 70% training, next 15% validation, and final 15% test windows. No
`draft_id` may appear in more than one split. Run the same procedure for at least
two held-out set/format datasets. Report whole-set holdouts separately when
testing whether one model generalizes to unseen cards.

Score the exact same eligible test picks with:

1. raw 17Lands win-rate ranking;
2. the current DO Score `PickEngine`;
3. the ML ranker.

Report top-1, top-3, and top-5 historical-pick agreement, mean actual-pick rank,
mean reciprocal rank, and NDCG at 1, 3, and 5. Break every result down by set,
format, and the existing `open`, `building`, and `locked` phases. Also report
all-draft, trophy-only, rank-bucket, and available outcome strata without
presenting those correlations as causal.

Use paired bootstrap resampling clustered by `draft_id` so all picks from a
draft stay together. Publish the point estimate and 95% confidence interval for
the ML-minus-DO difference.

The ML model may advance to runtime-integration work only when:

- its test-set mean reciprocal rank improvement over DO Score has a paired 95%
  confidence interval excluding zero on at least two set/format datasets;
- no `open`, `building`, or `locked` phase loses more than one percentage point
  of top-3 agreement;
- skipped and unresolved-row rates are reported and are comparable between
  models;
- local prediction for one pack fits within Draft Omen's 1.5-second display
  latency budget.

Failing the gate is a useful result. It means DO Score remains the default and
the research report should identify the largest error strata before proposing a
more complex model.

## Biases and interpretation risks

- **Human-pick bias:** the label reproduces historical choices, including
  mistakes and conventional wisdom.
- **Survivor and trophy bias:** successful drafts are not a random sample, and
  filtering to them discards losing strategies and weaker players.
- **Player-skill bias:** experienced users may draft and play better; rank and
  history stratification can diagnose this but must not personalize live picks.
- **Outcome noise:** a draft result reflects deck construction, games, draws,
  opponents, and piloting, not only one pick.
- **No counterfactual outcome:** the data never observes the result of taking a
  different card from the same pack.
- **Metagame drift:** card understanding, bot behavior, and archetype popularity
  change over a set's lifetime, which is why chronological splits matter.
- **Format mismatch:** Premier, Traditional, and Quick Draft have different
  incentives and pack dynamics. A model or result is valid only for its named
  format.
- **New-set cold start:** public-data release timing and exact card identities
  prevent the first model from replacing the heuristic early in a format.

These limitations mean model output is a ranked historical-choice prediction,
not proof of the best pick. UI copy must preserve that distinction.

## Future integration boundary

Do not change `pickengine.py` for this research issue. If a model passes the
gate, introduce a model-neutral boundary rather than making an ML implementation
populate heuristic-specific `ScoredCard` fields.

The future contract should contain:

- `PickContext`: offered card IDs, pool card IDs, pack number, pick number, set
  code, event format, and access to card metadata;
- `PickRecommendation`: card ID, rank score, provenance, optional confidence,
  and optional explanation metadata;
- `RecommendationPack`: ordered recommendations, source summary, and an
  optional fallback reason;
- `PickRecommender`: a protocol that ranks one `PickContext`.

A heuristic adapter should wrap the current `PickEngine`; an ML adapter should
load only an exact set/format and feature-schema match; and a hybrid adapter may
blend or select between them only after separate evaluation. Missing artifacts,
schema mismatches, load failures, or prediction errors must fall back to DO
Score without interrupting live drafting. The active source and fallback must
remain visible, and 17Lands attribution must remain present.

Suggested follow-up work is deliberately split into separate changes: build the
offline dataset and reproducible trainer, add artifact validation and the
recommendation protocol, then add source/confidence UI and shadow-mode
comparison. Model download or distribution policy should be decided only after
the first artifact's size and license are known.

## Build and review one augmented set

Before training, review the [17Lands Public Data listing](https://www.17lands.com/public_datasets)
and [usage guidelines](https://www.17lands.com/usage_guidelines). Check the
exact URLs and dataset-specific licenses for HOB datasets in the listing, since
the command may fall back to a later format. Do not run it until every dataset it
could select has terms compatible with model training and distribution.

Run the command from the development environment for one set:

```sh
uv run draftomen-tui build-augmented-set HOB
```

The command reads the public Draft Data listing and checks the requested set's
`PremierDraft`, `TradDraft`, then `QuickDraft` dataset in that preference order.
It uses the first listed dump it can acquire and validate. It reuses a valid
canonical card-data artifact for that set. If none exists, it generates only
that set's artifact under `website/public/card-data/`; an invalid existing
artifact causes the command to fail. The downloaded dump stays in the per-user
`.draftomen/profile-input-cache/`, not in `website/public/`.

The held-out evaluation compares Basic DO with Basic DO plus the augmented model.
The command prints the profile source used for Basic DO and both `top_1` and
`mean_reciprocal_rank` values for each comparison. It publishes a model only
when both augmented metrics strictly exceed Basic DO. A matching set-and-format
profile is used when available; otherwise the command reports and uses the
generic scoring profile.

On a passing gate, the command writes the compressed model to
`website/public/augmented/objects/<compressed-sha256>.json.gz` and updates
`website/public/augmented/manifest.json`. The filename digest is calculated
from the compressed object bytes. A failed gate exits with status 1 and does not
publish a model object or manifest entry; a newly generated card-data artifact
may remain. Validation, acquisition, training, or publishing errors also exit
with status 1. Ctrl-C exits with status 130.

Before opening a pull request:

1. Compare the selected dataset's source record in the model and manifest with
   the 17Lands listing. Record the exact source URL, retrieval date, and
   dataset-specific license. The [17Lands usage guidelines](https://www.17lands.com/usage_guidelines)
   explain the public-data terms. Do not distribute the model if its license is
   missing or does not permit it. The site's
   [Terms of Service](https://www.17lands.com/terms_of_service) also apply to
   access.
2. Inspect the generated model and manifest, any newly generated card-data
   artifact, and both aggregate metric pairs. Confirm the set and format match
   the reviewed source.
3. Inspect the `website/public/` changes. This workflow should add only the
   compressed model object and manifest update, plus canonical card data if the
   command generated it. Do not add the public dump, training matrices, or
   row-level predictions.
4. Open a normal pull request manually after review. This command does not run
   Git, push changes, or create a pull request.

## Issue acceptance mapping

| Issue #37 criterion | Design decision |
| --- | --- |
| Historical sources and constraints | Use public draft dumps, optionally join public game data, reuse existing card metadata, and enforce the licensing and privacy rules above. |
| Candidate model inputs | Use only current pack, pre-pick pool, position, set/format, and available card metadata; exclude identifiers and future information. |
| Labels and risks | Use the human pick as an imitation label, keep outcomes secondary, and report the listed human, survivor, skill, outcome, drift, and format biases. |
| Offline comparison | Compare raw 17L WR, DO Score, and ML on identical chronological draft-level holdouts with clustered confidence intervals and the explicit promotion gate. |
| Future architecture | Add a model-neutral recommender boundary only after the gate, augment first, and retain DO Score as the automatic fallback. |
