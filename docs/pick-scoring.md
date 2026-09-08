# Pick scoring

Draft Omen keeps the resolved GIH win rate visible when available. QuickDraft and Premier rows retain raw 17Lands GIH WR; EARLY and MATURE profile rows show the published, already-shrunk profile value instead. The Textual watch table ranks by DO Score by default after TMT PremierDraft, TMT TradDraft, and SOS PremierDraft trophy benchmarks showed better top-1/top-3/top-5 match rates and average actual-pick rank than raw 17L WR. It also keeps the resolved GIH rate visible and switchable for comparison.

EARLY and MATURE profiles are first for empirical card ratings: the loaded database card must match a profile card by its single canonical producer key exactly. On a match, the profile's published `RateEstimate.value` is the score's card estimate; it is already shrunk, while its GIH sample count and optional ALSA are preserved. Draft Omen computes the grade from the positive-sample published profile-rate distribution; other provider-only metrics remain unavailable and the row source is `Profile`. Unmatched cards use deterministic local fallback—an already-loaded legacy pair rating when locked and applicable, otherwise the local global/format rating (QuickDraft, then Premier fallback), then the neutral prior with ALSA adjustment—without alias or name borrowing. `GENERIC`, `METADATA_ONLY`, and `SEMANTIC_ONLY` profiles do not activate empirical estimates.

Scores are normalized against the unique resolved empirical-profile estimate distribution when an EARLY or MATURE profile contributes card ratings; each canonical profile estimate enters that distribution once even if multiple runtime IDs resolve to it. Without such profile ratings, normalization uses the local rating distribution. Scores are centered so the neutral prior displays as 50 before color logic. The five basic lands that can be added freely during deck building instead receive 0 DO points and rank after draftable cards; drafted nonbasic and special lands keep their normal ratings. Color commitment then multiplies the normalized score. Let `L` be the commitment level from 0 to 1; the factor is `1` for colorless/open/unknown cards or no commitment, `1 + L × (1.15 − 1)` for on-color cards, `1 − L × (1 − 0.75)` for ordinary off-color cards, `1 − L × (1 − 0.95)` for supported splashes, `1 − L × (1 − 0.85)` for speculative splashes, and `1 + L × (1.05 − 1)` for splash fixers. These are the defaults (`on_color_bonus_multiplier = 1.15`, `off_color_penalty_multiplier = 0.75`, and splash ready/speculative/fixer multipliers `0.95`/`0.85`/`1.05`); each factor is bounded below by zero.

Pool color weights come from picked cards. Each colored picked card contributes a quality-weighted amount to each of its colors, so a strong card pulls harder than filler. The highest-weighted two-color pair is the inferred pair once at least two colors have material weight.

During open picks, set/format-specific pair performance is used as a close-pick tiebreaker. If cards' unrounded `raw_score` values are within `3.0` DO points and the hypothetical color-pair weights after taking each card are also close, an empirical-profile pair rate must differ by more than `1pp` before the recommendation prefers the card leading toward the higher-rate pair. Otherwise, the base comparator retains the ordering. For an EARLY or MATURE profile, a published `PairProfile.performance.value` directly supplies the pair tiebreaker rate; that value is already shrunk and receives no second shrinkage. If published performance is absent, the preloaded local aggregate pair rate remains the fallback and keeps the existing evidence/sample shrinkage. Only for that fallback aggregate rate, the tiebreaker uses `p_prior + w × (p − p_prior)`, where `p_prior` is the neutral pair rate and `0 ≤ w ≤ 1`. The influence is the product of profile maturity/confidence, profile total and per-pair sample support, and aggregate pair-game support. Each sample factor is `n / (n + k)` and missing evidence contributes zero, so thin fallback evidence cannot create a material pair-rate margin or overturn base ordering. This is deliberately disabled once the color ramp starts, so pair win rate does not override later commitment signals. Without an empirical profile—including generic, metadata-only, or semantic-only profiles—local rates retain the legacy any-nonzero-rate comparison, so even a sub-`1pp` difference can resolve a close pick. Rationale provenance is narrower than pair-rate availability: only the card that wins an actual pair-rate comparator invocation in this gated open-pick path receives a `tiebreaker` reason. Merely carrying a candidate pair rate or being in a close group is not enough; if the pair-rate winner agrees with the base ordering, the reason is still retained, while a failed gate leaves ordering and rationale unchanged.

Commitment is controlled by documented defaults in `config.py`:

- picks 1-5: open, raw scores (`open_pick_count = 5`)
- pick 6: linear ramp begins (`commitment_start_pick = 6`)
- pick 16+: locked to the inferred pair (`locked_pick_index = 16`)
- locked on-color score multiplier: `1.15`
- locked off-color score multiplier: `0.75`
- pool weight baseline/rating scale/min/max: `1.0`, `10.0`, `0.25`, `2.0`
- open-pick pair-win-rate tiebreaker: within `3.0` DO points and `0.25` pair-weight points
- neutral aggregate pair prior: `neutral_pair_win_rate = 0.5` (independent of the `0.55` card prior)

Rows show a `Fit` marker: `On` for cards inside the inferred pair, `Off!` for off-color cards, `Any` for colorless cards, and `Open` before the ramp starts or before a pair is available. Once locked, a matched empirical profile card keeps its profile/global estimate as authoritative. An unmatched card may use only already-materialized legacy pair-card data; scoring makes no provider or lazy pair-card requests. When a legacy pair rating is used, its raw pair rating remains available for `17L WR`, grade, samples, and source metadata; only the score-only base rating is shrunk toward the resolved all-decks card rating. If pair data is absent, scoring uses the deterministic global/neutral fallback. Profile rows expose the published GIH value, sample count, and optional ALSA with source `Profile`, but do not fabricate provider-only metrics or grades. Pair-card influence is bounded by profile maturity/confidence, profile total/per-pair samples, and the pair row's GIH sample count; missing evidence falls back safely toward global evidence rather than inventing certainty.

## Pre-pick scoring context

`build_pick_scoring_context` is the public construction entry point for the
single validated pre-pick context boundary. It accepts the authoritative
pool-before-pick IDs and card database, ratings/config for pair inference, the
exact selected `SetProfile`, and either `pick_index` or the complete stage
coordinates (`pack_number`, `pick_number`, `global_pick_index`,
`estimated_remaining_picks`). Partial coordinates are rejected. An existing
context is authoritative: conflicting coordinates are rejected and the same
context is returned unchanged. `PickEngine.score_pack` resolves stage and
commitment once, then uses the same private validated construction path.
Offline, recovered, accountless, replay, backtest, and benchmark entry points
call `load_scoring_profile` for the active set/format before scoring. They
score from the loaded profile and local ratings snapshot only. Normal live TUI,
plain-watch, CLI `watch`, and Qt live factories use the same profile
authority; they do not pass provider-loader callbacks, provider-cache checks,
synchronous or progress loaders, or raw provider ratings into `LiveSession`.
They load local profile state before any explicitly configured hosted refresh
owned by the adapter worker. Network is never part of score construction. Live
build and completion likewise use the selected profile or deterministic
fallback and make no provider acquisition. Standalone build/backtest commands
may use an already-cached provider snapshot through their separate offline
workflows, but do not fetch provider data. In particular, separate offline/domain
callers supplying already-materialized legacy pair-card data may use it for locked
scoring; normal live locked scoring uses the selected profile or deterministic
fallback and makes no provider or lazy pair-card request.

### Cached profile selection

The flat cache is `<app-data>/set-profiles/<set>-<format>.json`, where
`<app-data>` is the application-data directory (normally `~/.draftomen`).
`ProfileClient.load_cached` reads this path without network access. A valid
non-generic profile is used as-is. If the flat file is missing, corrupt,
future-schema, wrong-target, or generic, valid historical profile locations
are checked and a usable non-generic profile may be migrated to the flat path.
Invalid files are diagnostics, not automatic deletions.

When candidate loading has multiple valid profiles, maturity precedence is
`mature`, `early`, `semantic-only`, `metadata-only`, then a matching
`last_valid_profile`, and finally generic. `load_scoring_profile` converts
that generic result to `None`, so the existing rating/color path remains
unchanged when no usable profile is available. If a live manifest URL is
explicitly configured, an adapter-owned refresh can atomically install a
validated newer profile and rescore the active pack; stale, invalid, or
unavailable remote data leaves the profile already selected for scoring in
place. Without that opt-in URL, live scoring remains offline.
Normal live hosted refresh remains explicit here; the default manifest URL work
owned by issue #353 and the native default-URL and ratings-presentation work
owned by issue #354 remain outside this change.

`PickScoringContext` is an immutable value with exactly two fields:

- `set_profile: SetProfile`
- `role_ledger: PoolRoleLedger`

Construction validates that both fields have their declared types, the ledger
uses `PRE_PICK_PROJECTION`, its stage is present, and its
`profile_source` is exactly `profile:{set_profile.maturity.value}`. The
validated ledger stage is exposed through the context's read-only `stage`
property.

`ScoredPack.scoring_context` retains the supplied or constructed context
exactly, and `ScoredPack.role_ledger` retains its ledger.

### Contextual-adjustment mode

The shared engine and module-level `score_pack` wrapper expose
`contextual_adjustments_enabled`, which defaults to `True` for compatibility.
Set it to `False` when the caller needs profile-backed base scoring without
the six additive contextual terms. This mode still loads the selected rich
profile for card ratings and normalization, color inference, splash assessment,
and pair tiebreaking, and an explicitly supplied scoring context does not
override the setting.

The setting controls only those additive contextual terms; it does not disable
profile-backed scoring. While disabled, the selected profile can still supply
card estimates and normalization, color inference, splash assessment, and pair
tiebreaking. Profile download/refresh and profile-cache selection are
independent of this mode: disabling contextual adjustments neither prevents
profile loading or use of an existing cache nor changes the adapter-owned
profile refresh policy. Changing the mode itself makes no profile, ratings,
metadata, or card-image request.

With contextual adjustments disabled, scores use only the existing
base-score/color calculation and its `0–100` clamp. The serialized breakdown
contains zero for every contextual term and contextual evidence is empty, so
recommendations and audit payloads do not claim that contextual adjustments
were applied. The default enabled mode retains the bounded terms,
aggregate clamp, ordering, and fallback behavior described below. Enabling the
mode permits contextual terms; it does not guarantee a numeric score or
recommendation change. A changed score requires a validated pre-pick context
and usable, material profile evidence. With no usable profile/context, generic
rating/color scoring remains in effect and the contextual evidence stays empty.

#### Live session and backtest behavior

`LiveSession(..., contextual_adjustments_enabled=True)` is enabled by default,
as is shared backtest generation. This shared API/session default is separate
from the desktop's persisted policy. The published
`LiveSessionSnapshot.contextual_adjustments_enabled` value is authoritative and
immutable; callers change it by dispatching the frozen
`ChangeContextualScoring(enabled: bool)` command.

When a current pack exists, the command immediately re-scores that pack locally
from already-loaded card, profile, and pool state, then publishes the
replacement scored pack and recommendations. The visible scores and
explanations are replaced immediately; when the required profile evidence is
not usable or material, the replacement can retain the same numeric ordering
while correctly omitting contextual evidence. Toggling the mode makes no
metadata, profile, ratings, or card-image request, does not queue a delayed
recommendation-image request, and does not cancel unrelated work. The selected
mode is used for later packs and retained through profile adoption and other
session lifecycle transitions, including recovery.

Session-requested backtests use the same current mode, while the shared
`generate_backtest_report` entry point accepts the mode explicitly and defaults
it to enabled. Changing the mode retires any published backtest comparison and
invalidates an in-flight one, so a stale completion cannot republish results
scored under the previous mode. Deck construction is independent of this
toggle: build requests do not use contextual terms, and changing the mode
neither rebuilds nor changes an existing deck result.

#### Desktop policy and fresh-process persistence

The production desktop GUI applies a separate, persisted policy: **Settings →
Contextual pick scoring** is an accessible keyboard/mouse switch whose default
is off. Its value is stored as the additive
`display.contextual_adjustments_enabled` field in GUI preferences schema
version 1. Existing version-1 preferences remain compatible: a missing or
non-boolean field falls back to off while the other display preferences remain
usable. Startup loads this preference before provider/session construction and
before `LiveSessionAdapter.start()`. A real accepted switch change dispatches
`ChangeContextualScoring`; the desktop saves changes through its coalesced
atomic writer and drains pending writes during shutdown.

Switching the setting in the desktop immediately updates the current pack's
scores and recommendation explanations through the live-session command, uses
the selected mode for later packs and backtests, and retains the profile
loading/download/cache and deck-independence rules above. A fresh desktop
process reads the saved value before its first provider/session score, so the
choice survives a normal close and restart.

For a reproducible production restart check, use the same isolated application
directory, log fixture, local card-data file, and cached profile for both
processes. Do not use the mockup:

1. Prepare an isolated `APP_DIR`, a readable `PLAYER_LOG`, a local Scryfall
   JSONL `CARD_DATA` file, and a validated non-generic profile cache below
   `APP_DIR/set-profiles/` for the set/format under test.
2. Start the production GUI with those exact inputs, for example:

   ```bash
   uv run draftomen --provider live \
     --app-dir "$APP_DIR" \
     --log-path "$PLAYER_LOG" \
     --bulk-file "$CARD_DATA" \
     --offline-profiles
   ```

3. In **Settings**, activate **Contextual pick scoring** with the mouse, then use
   Space to disable it and the mouse to enable it again. With a current pack
   visible, record the displayed scores and recommendation explanation, then
   close the application normally so the coalesced writer can drain.
4. After the first process has exited, launch the exact same command as a new
   OS process. Confirm that the switch is still on before the first scored pack
   appears, then inspect a later pack and a session backtest. Turn the switch off,
   close normally, and launch a third process to confirm the off state before its
   first scored pack. Both on and off directions are required for acceptance
   verification.

##### Recorded #382 verification

This production workflow used the controlled fixture identity **TST /
QuickDraft**, profile version `contextual-1.0` (source `local-mature`, maturity
`mature`), event `QuickDraft_TST_20260829`, and user-facing pack 1, pick 7
(stored coordinates 0/6). The card was grpId 104894, **Fixture Card 104894**,
with inferred pair **WU**. These controlled fixture identities are verification
data, not user configuration.

- In the initial fresh process, no preference was loaded and the first score
  mode was **OFF** (the default). The real Settings switch enabled scoring with
  the mouse, Space disabled it, and the mouse enabled it again. The card's
  visible score was **72 → 73 → 72 → 73** (OFF → ON → OFF → final ON). OFF had
  no contextual evidence; ON had aggregate **+0.637871**, `fills draw deficit
  (0/3)`, and `emerging role urgency 0.15`.
  The off/on explanations differed, and normal shutdown persisted ON.
- A second fresh OS process loaded **ON** before startup; its first score was
  ON, the switch was visibly ON, and the card remained **73** with the same
  material WU evidence. Mouse-disabling the switch changed the visible score to
  **72** and changed the explanation; normal shutdown persisted **OFF**.
- A third fresh OS process loaded **OFF** before startup; its first score was
  OFF, the switch was visibly OFF, the card was **72**, and contextual evidence
  was empty. OFF remained persisted after normal shutdown.

All three driver invocations exited successfully.

To observe a score delta rather than only the persisted mode and explanation,
use a profile and pool that produce a validated pre-pick context with material
evidence. A metadata-only, unusable, or absent profile is still a valid
persistence test, but enabling the switch need not change its numeric scores.

When a validated profile-backed pre-pick context is available, the engine scores
with six small additive contextual terms from that validated pre-pick state:
role need (0–2.5), late urgency (0–3.0), semantic package support (0–1.5),
redundancy (−2.0–0), unsupported payoff (−2.0–0), and fixing need (0–1.5).
Each term is finite and clamped to its documented range. The contextual
contribution is the sum of those six terms clamped to −6.0–+6.0 DO points,
and the raw score is:

`clamp(base score × color factor + contextual contribution, 0, 100)`.

Terms scale with draft stage, profile maturity/confidence, target confidence,
assignment confidence, and existing package evidence, so early picks remain
quality-first. Missing targets are saturated at their preferred minimum; in
particular, late role urgency disappears once that target is met. Empirical
card-pair synergy is not used.

Scored rows and session recommendations retain the immutable term breakdown,
aggregate, and material evidence strings. Recommendation explanations name
the inferred pair and optional theme, profile maturity/confidence, and
material contextual terms without claiming that weak semantic evidence
guarantees an outcome.

`PairProfile.theme` is optional annotation-only metadata. When present, it is
trimmed while preserving the supplied case; it labels a pair theme and does
not independently affect scoring. Contextual scoring uses only the supplied
pre-pick ledger and never projects against offered or future cards. Without a
validated profile-backed context, scores and ordering remain the generic
rating/color results.

All live, recovered, and accountless session paths, replay, backtest, and
benchmark route pre-pick scoring through `PickEngine.score_pack` and use the
`ScoredPack` handoff. Audit serialization consumes that same pack rather than
reconstructing context; watch and TUI adapters consume shared session state
and do not build context.

Session recommendations, replay explanations, and backtest results use the
scored-card contextual evidence. Audit records serialize matching
recommendation/candidate breakdown and evidence, pair/theme/profile metadata,
and context stage/profile provenance, preserving recommendation/audit parity.

Missing, corrupt, incompatible, or generic profiles normalize to no context:
`build_pick_scoring_context` returns `None` and
`ScoredPack.scoring_context` remains `None`; generic rating/color results stay
unchanged, although a stage-aware generic role ledger may still be retained.

## Immutable pick rationale

Every engine-produced `ScoredCard` has a frozen `PickRationale` after final
ordering. It is an ordered tuple of frozen `PickReason` values plus an explicit
`unattributed_contribution` remainder. Each reason has exactly `kind`,
`contribution`, `phrase`, and `evidence` fields. The allowed kinds are `rating`,
`color`, `role`, `urgency`, `synergy`, `redundancy`, `unsupported_payoff`,
`fixing`, `splash`, and `tiebreaker`. Reasons are ordered deterministically:
rating baseline, color adjustment, material contextual terms, retained splash
assessment, then actual tiebreaker provenance.

`evidence` is either absent, one retained string, or a tuple of retained
strings (serialized as a JSON list). Material contextual terms are strictly
those with `abs(value) > 0.01`; each retains its exact contribution value and
the corresponding original evidence string. Splash assessments retain their
complete reasons, while a tiebreaker retains the pair-rate comparison that
actually selected the winner. Rating, splash, and tiebreaker reasons are
nonadditive: the rating reason describes the normalized `base_score` baseline,
which is counted once, and the latter two preserve decision evidence without
inventing score points. The color reason contributes exactly
`base_score × (color_factor − 1)`.

The scoring arithmetic remains:

`raw_score = clamp(base_score × color_factor + contextual_breakdown.aggregate, 0, 100)`.

`raw_score` is already clamped before it is stored. Rationale accounting
reconciles the existing score without changing it:

`base_score + attributed_contribution + unattributed_contribution = raw_score`.

`attributed_contribution` sums only reasons with a numeric contribution. The
remainder is the exact difference and can account for omitted sub-materiality
terms, the contextual aggregate cap and six-decimal rounding, and the final
0–100 clamp. It is accounting for existing arithmetic, not a fictional
allocation, an additional score term, or a scoring change.

Arithmetic values remain floating point. The displayed DO score is a
nonnegative whole-number rendering of the clamped raw score (nearest integer,
with `.5` rounded upward). Concise signed contribution labels use nearest whole
DO-point rounding with halves away from zero (`+1.5` becomes `+2`, `−1.5`
becomes `−2`); detailed rationale retains contributions to two decimal places.
Display rounding therefore must not be used to reconstruct the score equation.

`render_pick_rationale_concise` always shows the rating sentence for a
draftable card and adds at most two non-rating reasons. Those reasons are
selected by descending absolute numeric magnitude, with stable original order
as the tie-breaker; nonadditive splash and tiebreaker evidence remains eligible.
Concise output uses stable kind-specific wording and signed whole-point labels,
but does not include raw evidence strings. The rating sentence identifies the
available source or neutral-prior uncertainty; when a retained profile supplies
maturity and confidence, it shows both, and unavailable confidence is rendered
as unknown rather than inferred. Weak semantic evidence is described as
supporting evidence, never as a guaranteed outcome.

`render_pick_rationale_detailed` starts with the card's score, rating baseline,
and exact color contribution, then lists every retained material contextual,
splash, and actual tiebreaker reason with decimal contributions where
applicable and the retained evidence in brackets. It adds the context pair and
optional theme only when material contextual terms exist, and reports a
nonzero score-accounting remainder. The five freely available basic lands keep
the exact legacy explanation: they receive 0 DO points and rank after draftable
cards.

Without a usable validated scoring context, contextual terms and evidence are
absent and generic rating/color scoring remains unchanged. Disabling contextual
adjustments likewise emits zero contextual terms and empty contextual
evidence; it does not suppress the rating/color reasons or independently
available splash and actual tiebreaker reasons. A `Recommendation` publishes
the same card rationale through `rationale`, `concise_explanation`, and
`explanation`; the latter two are rendered directly by
`render_pick_rationale_concise` and `render_pick_rationale_detailed`. Replay
uses the detailed renderer for the recommendation on every nonempty pack.

## Top-two recommendation comparison

After final DO recommendation ordering, each `ScoredPack` may include one
`comparison_summary` for the first two draftable, non-basic cards. It is
`None` when fewer than two such cards exist. The engine composes this text once
from the ordered scored cards; `RecommendationState` publishes it unchanged
regardless of the active display sort, and decision audit records serialize the
same nullable value. Because it is explanatory presentation rather than a
scoring input, it does not participate in evaluation identity.

The comparison follows ranking evidence in this order:

1. When the winner has retained open-pick tiebreaker evidence, the engine
   directly reruns the existing eligibility and material-margin check for the
   top two cards. A qualifying result names the preferred color pair and its
   percentage-point rate advantage; it never reuses prose that may describe a
   different card.
2. If deterministic close-pick ordering retained an order that a direct
   top-two base-key comparison does not explain, the text describes that
   ordering without inventing a score gap.
3. Equal displayed DO scores name the actual next base-key discriminator:
   unrounded score, base rating, or original offered-pack order.
4. Otherwise, the comparison reports the difference between the two displayed
   whole-number DO scores.

For a score advantage, retained arithmetic is grouped into rating, color, each
contextual reason kind, and the unattributed accounting remainder. Only
positive winner-minus-runner-up deltas support the explanation. Factors are
ordered by their unrounded positive contribution, with rationale-kind order as
the deterministic tie-breaker, and the shortest prefix exceeding half of all
positive support is named. The remainder is called `score limits and small
adjustments` because it can contain clamps, caps, rounding, and omitted tiny
terms. Splash and tiebreaker reasons are nonadditive and are never counted as
point factors.

Displayed point gaps remain whole numbers and named factor contributions are
not separately rounded or claimed to sum to that gap. The existing confidence
classification supplies only compact uncertainty wording: early/open picks
ask the drafter to stay flexible, close picks remain explicitly close, and no
new threshold or confidence model is introduced.

The same engine text appears beside confidence in three live locations: the
TUI focused offered-card panel, the plain-watch footer after each eligible
pack's ranked rows, and the desktop Live Draft header beside the preview. Each
surface omits an absent comparison. Plain watch uses the publication's own
snapshot so buffered packs keep their original explanation. The TUI and
desktop retain the comparison when focus or the active display sort changes
and replace or clear it only when the session publishes a new pack value.
Therefore a comparison always describes DO recommendation order, even while
the visible rows are sorted by 17L WR, ALSA, or mana value.

Plain `watch` prints exactly one indented `Recommendation:` line after the
ranked rows for each pack whose event snapshot has recommendations. The line
uses that snapshot's top recommendation's unchanged concise explanation, so
buffered packs retain their own rationale. Packs without recommendations emit
no recommendation line. Replay continues to use the detailed explanation.

In the live TUI, the focused offered-card details show the applied session
recommendation's unchanged concise explanation below the existing card facts
under `Why this score:`. The focused-details visibility preference controls the
whole panel, and its contents remain reachable with mouse-wheel scrolling in
short terminals. Build context never shows pick rationale, including in the
focused sidebar.

The desktop `CardPreview` renders a recommendation's unchanged concise
explanation in compact mode and its unchanged detailed explanation in
focused-intel mode. Both current Live Draft preview layouts use focused intel;
compact recommendation rendering remains a component capability rather than a
separate Live Draft option. Explanations wrap as plain text and remain reachable
through the existing details scrolling. Build previews receive build-card
values and show no pick rationale.



## Splash recommendations

Splash recommendations are enabled by default. Open the TUI configuration with `c` to disable them persistently, or start a session with `draftomen-tui watch --no-splash`. `draftomen-tui replay` and `draftomen-tui backtest` also accept `--no-splash`.

The splash policy is deliberately narrower than general three-color drafting:

- Keep exactly one inferred two-color primary pair and consider at most one additional color.
- Take at most two splash cards.
- Require each splash card to have only one mana pip of the splash color and no other color outside the primary pair.
- Require at least an `A-` 17Lands grade and a base DO Score advantage of `5.0` over the best offered on-color card.
- Require three sources for one splash card and four sources for two. At most one source may be a planned basic; the rest must be deterministic drafted fixing lands that are castable in the primary pair.
- Before color lock, an unsupported `A` or `A+` card may be marked `Splash?` as a speculative pick. Speculative splashes are disabled after lock and in aggressive pools.
- Aggressive pools require a supported `A` or better splash.

The `Fit` column uses `Splash X` for a supported splash, `Splash? X` for a speculative one, and `Fix X` when a fixing land directly supports the active splash color. Focused card details show the source count and the exact acceptance or rejection reason. Once a splash color has been established, cards of any other third color remain ordinary off-color cards.

The TUI pack table shows `17L WR` and `17L Grade` as primary columns. For QuickDraft and Premier rows, `17L WR` is the raw Games-in-Hand win rate from the resolved 17Lands source. `17L Grade` follows the methodology published on the 17Lands Card Data page for the Grades view: grades are centered at `C` on the selected win-rate metric distribution, and each grade step is a deterministic `0.33` standard-deviation band. Draft Omen computes those grades from the cached 17Lands GIH distribution for the same source and format/filter context the row uses (QuickDraft for Quick Drafts, PremierDraft when the row is a Premier fallback, or pair-filtered data when used). Profile rows show the published `RateEstimate.value` (already shrunk), preserve its GIH sample count and optional ALSA, and compute `17L Grade` from the positive-sample published profile-rate distribution. Cards without positive-sample profile evidence or a resolved GIH win rate show `—`.

Displayed `DO` scores are whole-number integers. We do not show one decimal for ties because the plain draft table should stay easy to scan; after the open-pick pair-win-rate tiebreaker, remaining DO ties are resolved deterministically by raw score, base rating, and original pack order.

The TUI is explicit about the active ranking in the title and status bar. Press `s` to cycle between DO Score, 17Lands WR, ALSA, and mana value. When the current recommendation is an early/open pick or the top two cards are very close in the active ranking, the status bar shows confidence copy such as `early/open pick — stay flexible` or `close pick` rather than overclaiming certainty. Backtest reports also print the ranking used before listing recommended-vs-actual picks.

See [benchmarking.md](benchmarking.md) for the offline 17Lands public-data workflow and current calibration evidence. The known non-ML follow-up is reviewing building/locked DO Score misses where trophy drafters still took off-color cards, which may indicate set-specific color-ramp or off-color-penalty tuning.

Rows marked `Prior*` did not have a strong GIH sample. The `Source` column shows whether a row used `Profile`, `Quick Draft`, `Premier Draft fallback`, `Prior*`, or `Basic`.
