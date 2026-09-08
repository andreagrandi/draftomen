# Changelog

- Generate the validated HOB QuickDraft metadata-only profile snapshot with canonical provenance, lifecycle, licensing, and deterministic replay evidence. (#325)

## [Unreleased]
- Explain why the top DO recommendation outranks the runner-up using retained
  scoring evidence, preserve the comparison across display sorting, and record
  it in decision audits without changing evaluation identity. (#398)
- Show the unchanged top-two DO comparison beside confidence in the TUI,
  plain-watch pack footer, and desktop Live Draft header while preserving its
  DO-order meaning across display sorting and buffered publications. (#399)
- Prevent pull-request CI runs for generated-only website card-data and profile
  refreshes while retaining source pull-request and master-push coverage.
- Classify active and historical profile refresh formats at expansion level,
  keeping every supported format in the expansion's lifecycle partition.
- Center and theme the live ratings-refresh confirmation consistently with the
  About and Settings dialogs while preserving keyboard focus and commands.
- Show grades for empirical profile ratings by applying the existing 17Lands
  grading scale to the published positive-sample GIH distribution.
- Fetch hosted card metadata when restoring a completed draft with an empty
  local cache, while keeping active recovered drafts offline.
- Show unchanged concise pick rationale in compact desktop card previews and
  unchanged detailed rationale in focused intel, with wrapped plain text,
  retained scrolling and no pick rationale in build previews. (#389)
- Show each plain-watch pack's unchanged concise top recommendation immediately
  after its ranked rows while preserving event-specific text for buffered packs
  and omitting recommendation output when none is available. (#388)
- Show each focused offered card's unchanged session pick rationale below its
  TUI facts, keep it mouse-wheel accessible in short terminals, and omit pick
  rationale from build context. (#387)
- Add deterministic TUI CI coverage proving hosted profile refresh workers stay
  blocked while keyboard input remains responsive.
- Add immutable per-card pick rationale with exact evidence and score accounting,
  publish concise and detailed explanations through sessions and audits, and
  include detailed rationale in every nonempty replay pack. (#355)

- Add an explicit shared-engine contextual-adjustment mode that can disable
  additive contextual terms while retaining profile-backed base scoring and
  non-contextual evidence. (#380)
- Add the default-enabled live-session and backtest contextual-adjustment
  mode with immutable state, explicit commands, immediate local rescoring,
  stale comparison invalidation, and no toggle-triggered external work; deck
  construction remains independent. (#381)
- Add the persisted desktop Settings → Contextual pick scoring switch,
  defaulting off while retaining the shared API/session default-enabled
  contract; it re-scores current and later packs and backtests, survives a
  fresh-process restart, and remains independent of profile refresh/cache and
  deck construction. (#382)
- Native live startup now defaults to the production hosted profile manifest,
  supports explicit HTTPS overrides and profile-only `--offline-profiles`,
  and leaves static card-data, card-image, and card-metadata policies
  unchanged. Existing ratings refresh controls use the shared hosted-profile
  lifecycle, reporting `updated` or `unchanged` outcomes and retaining the last
  usable cache or deterministic fallback as applicable when refreshes fail, go
  offline, or find no profile, while preserving visible 17Lands attribution.
  (#354)
- Default terminal `watch` profile loading to the production manifest, add
  `--offline-profiles` for profile-only offline mode, and retain cached or
  deterministic-fallback ratings when hosted refreshes fail. Producer,
  publication, and release boundaries remain separate. (#353)
- Report profile-refresh publication as successful after verifying the generated
  commit reached `master`, even when GitHub PR merge metadata is delayed or
  unavailable; keep master verification failures fatal.
- Isolate recoverable ratings errors to the active set at snapshot publication:
  hide inactive-set errors while retaining nondismissed errors for return,
  remove dismissed errors, and make Retry queue the active set's forced
  hosted-profile refresh without disturbing unrelated operation errors.
- Remove direct provider-loader callbacks, cache/progress machinery, and raw
  ratings surfaces from normal terminal and native live factories; TUI,
  plain-watch, CLI `watch`, and Qt now use the shared hosted-profile lifecycle
  while preserving configured manifest overrides. (#370, #371)
- Complete the normal `LiveSession` provider cutover: profile state is the sole
  live ratings authority, cache-first startup and deterministic no-provider
  scoring/build/backtest fallback remain intact, and explicit producer plus
  separate offline/domain cached-provider workflows remain supported. (#373)
- Migrate shared-session regression fixtures to profile-backed inputs or
  deterministic fallback and carry the lifecycle contract through end-to-end
  adapter workflows, including refresh adoption, failure retention, and
  shutdown guards. (#372, #374)
- Propagate explicit hosted-profile refresh requests with `force=True`, bypassing
  only the manifest TTL while preserving offline, authority, validation, and
  non-regression safeguards and usable ratings during refresh. (#367)
- Route explicit ratings-download and recoverable ratings-retry actions through
  that forced hosted-profile lifecycle, coalesce repeated requests, and update
  recommendations in place after validated adoption; TUI and Qt retain existing
  actions and plain-watch adds no command UI. (#368)
- Keep hosted-profile refresh generations scoped to the active lifecycle, rejecting obsolete completions on set/account/draft changes, clear, and stop without restarting on ordinary picks or repeated same-lifecycle detection. (#360)
- Use cached empirical profile ratings immediately for live scoring and keep
  deterministic fallback scoring provider-independent. (#361)
- Adopt hosted refreshes atomically across profile identity, profile-derived
  freshness, current-pack scores/context, and recommendations; retain strongest
  usable authority on outage, invalid, or weaker results and avoid rescoring
  unchanged outcomes. (#361)
- Use hosted card and aggregate pair estimates in recommendation scoring, with exact canonical matching, deterministic legacy fallback, and no provider or lazy scoring requests. (#351)
- Keep profile-refresh publication checks deterministic by evaluating fixture
  ratings against their frozen test clock. (#351)
- Add guarded profile-refresh generation-artifact publication with validated
  full-success automatic merges, immutable partial review snapshots, and
  stale-base, failure, and unchanged-run safeguards. (#345)
- Add the generation-only scheduled and manually dispatched profile-refresh
  workflow with read-only execution, structured failure evidence, and
  successful generated-asset bundles. (#344)
- Use cached ratings or a local fallback for live locked-pair scoring without lazy 17Lands requests, while retaining the current-pack individual win-rate display and visible 17Lands attribution. (#339)
- Add the local `refresh-profile-data` producer command for deterministic set
  selection, cached aggregate 17Lands ratings, validated content-addressed
  profile publication, and bounded partial-failure reporting. (#338)
- Add the initial read-only CI profile-refresh evidence workflow as a
  generation-only, publication-independent baseline. (#303)
- Bundle the immutable HOB/QuickDraft metadata-only baseline in native
  applications with validated offline fallback and hosted refresh
  supersession. (#313)
- Record the production HOB/PremierDraft metadata snapshot audit evidence from the #319 producer run; retain the gzip payload locally for publication. (#319)
- Publish the first production HOB/PremierDraft metadata-only profile snapshot through content-addressed website assets. (#320)
- Complete the production hosting marker add/fetch/remove verification lifecycle. (#320)
- Add deterministic `generate-profile-refresh-batch` generation from staged
  refresh plans with isolated environment outcomes, privacy-safe reports, and
  no-network, no-publication execution. (#307)
- Adopt the master-only Cloudflare website static-asset contract for hosted
  profiles: production and development paths share complete website
  deployments, validated digest-addressed objects, explicit cache and
  retention/legal-erasure boundaries, and a two-merge smoke check. Hosting
  remains independent of stable releases, native releases, PyPI, Homebrew, and
  application startup. (#285)
- Add `execute-profile-refresh` to stage portable, content-addressed profile
  inputs from strict plans with bounded cache reuse, offline replay, and
  privacy-safe execution authorities. (#290)
- Add deterministic profile-generation stage selection from staged ratings and
  public-draft availability with immutable thresholds and privacy-safe records.
  (#301)
- Add deterministic single-environment profile generation from staged inputs with publication eligibility only after existing schema, identity, size, and checksum validation, plus affected-card diagnostics, valid metadata fallback, and bounded failure records without partial artifacts. (#304)

- Tighten agent skill guidance: single full-length waits for delegated
  implementation slices, size-based implementation readiness with board-time
  epic decomposition, and numbered acceptance-criteria issue templates.
- Add format-scoped public-draft acquisition with deterministic row reporting,
  verified cache and offline reuse, stale fallback, and independent three-source
  failure handling for profile-build bundles. (#296)
- Add optional format-scoped 17Lands ratings acquisition with deterministic
  sample reporting, bounded cache reuse, offline support, stale fallback, and
  metadata-only failure recovery. (#295)
- Add set-scoped card-metadata acquisition with deterministic source records,
  bounded cache reuse, stale fallback, and metadata-only profile-build bundles.
  (#292)
- Add bounded, content-aware profile-input caching with strict canonical
  records, verified offline reuse, deterministic retention, and privacy-safe
  outcomes. (#288)
- Add deterministic `plan-profile-refresh` dry runs with explicit Arena
  lifecycle metadata, 17Lands expansion discovery, bounded selections,
  canonical plans, diagnostics, and atomic plan output. (#283)
- Reduce GitHub issue and project API usage with centralized authenticated I/O,
  explicit call budgets, batched ProjectV2 mutations, and single-pass
  verification.
- Expand the GitHub issue workflow with implementation size and orchestration-risk
  classification plus approval-gated epic decomposition and dependency linking.
- Add issue-226's remote set-profile manifest/client workflow with
  offline-first cached loading, explicit live/manual refresh, validation,
  last-good recovery, and compact maturity/outcome status; discovery, scheduling,
  backfill, and publication automation remain owned by #227; hosting is provided
  by #285. (#226)
- Fix `generate-profile --format quickdraft` to accept pinned ratings caches
  with `QuickDraft` metadata in any casing while preserving normalized profile
  identity.
- Add the producer-side `generate-profile` workflow for staged metadata, early,
  and mature set-profile artifacts with pinned inputs, validation, atomic
  publication, deterministic checksums, and privacy-safe aggregate output.
  (#274)
- Add a deterministic, provenance-preserving offline reader for public CSV dumps with checksum verification and privacy-safe reports. (#272)
- Route shared 17Lands draft-row ingestion through the provenance-preserving structure-target builder. (#272)
- Extend bounded whole-package deck ranking with profile-aware semantic and
  empirical package terms, confidence- and maturity-bounded evidence, generic
  no-profile fallback, and frozen HOB draw-second regressions and
  counterfactuals. (#269)

- Add bounded deterministic whole-package beam search and local improvement to
  deck spell selection, while preserving deck, splash, mana, pip, and
  constraint-relaxation invariants. (#268)

- Route validated pre-pick scoring context through live, recovery, accountless, replay, backtest, benchmark, and audit workflows with recommendation/audit evidence parity. (#250)
- Define immutable pre-pick scoring context with deterministic profile provenance and pair-theme annotations. (#251)
- Add bounded contextual pick-score terms for projected role need, late
  urgency, semantic package support, redundancy, unsupported payoffs, and
  fixing, with material explanations and audit evidence. (#252)
- Shrink contextual pair and locked pair-card performance toward neutral/global
  priors using bounded profile and rating sample evidence. (#253)
- Add a development-only reproducible card corpus workflow with pinned Scryfall,
  current and legacy Arena mapping inputs, MTGJSON inputs, deterministic
  normalized artifacts, and coverage reports for offline semantic-analysis work.
- Extend card metadata with normalized face semantics, source-aware augmentation, and schema-4 offline cache migration.
- Add a typed, deterministic Limited semantic-role classifier with multi-valued
  assignments, effective-removal/threshold/mana parameters, canonical keywords
  and textual power support, reviewed overrides, and actionable unknown reports.
- Compile exact-set profiles with shared stable identities, atomic publication,
  deterministic duplicate handling, conservative unknown omission, and
  version-checked resolution; document the workflow. (#220)
- Add a versioned, deeply immutable local set-profile boundary with strict
  sparse empirical sections, semantic-role compatibility, and non-raising
  maturity-aware fallback loading. (#221)
- Add a deterministic immutable pool role ledger with explicit pre-pick
  projection and completed-pool modes, stage urgency, profile targets, and
  normalized interaction/package evidence. (#222)

## [0.3.1] - 2026-08-28

- Fix stable release startup and Linux validation with valid workflow syntax, permissions, and Qt runtime dependencies.
- Package unsigned macOS native bundles as Finder-native compressed DMG images with an Applications shortcut.
- Ignore late TUI session worker updates once Textual shutdown begins.
- Link website download buttons directly to versioned native assets, show the release version, and validate website output in stable releases.

## [0.3.0] - 2026-08-28

- Automatically open Deck Build and build the completed draft after the final pick.
- Add a larger, borderless overlay for the two product screenshots.
- Add a persisted toggle for following system text scaling in the desktop Settings, with effective scale feedback.
- Show desktop display-preference autosave status in the bottom status bar.
- Keep Backtest navigation hidden by default, with a persisted Settings visibility toggle.
- Show the latest successful card-data update time in Settings, with a clear never-updated state.
- Make manual ratings refresh bypass ready/cache short-circuits, expose download progress in Settings, and show the visible ratings refresh time.

- Replace the README and website screenshots with current GUI views for live picks and suggested decks.

- Expand the About dialog with author attribution, MIT License details, and website/GitHub links.

- Expand the wide build bench to show multiple rows while preserving the main deck and selected-card layouts.

- Widen the Live Draft card details and enlarge its focused card preview for
  more readable card imagery and metadata while resizing.

- Improve Settings toggles with high-contrast checked, unchecked, disabled,
  and focus states.

- Replace recent-pick click-modal previews with delayed, bounded hover previews.

- Fetch uncached recommendation card thumbnails in the background as each
  pick is published, without blocking recommendation selection.

- Restyle desktop buttons and dropdown selectors with dimensional Draft Omen
  controls, and move About and Privacy actions into the navigation rail.

- Make `draftomen` launch the live PySide6/QML GUI by default, move the
  terminal workflow to `draftomen-tui`, and keep deterministic mockup launches
  explicit.

- Add a static Draft Omen website with product overview, docs, downloads, and privacy details.

- Add an accessible About dialog with runtime version, project information, and website link.

- Add a Privacy dialog explaining that all user data remains on the user's computer.

- Guide users through enabling Arena Detailed Logs when no draft or readable Player.log is available.

- Renamed the project, Python package, CLI, GUI commands, and release artifacts to Draft Omen and `draftomen`.

- Maintain changelog-backed development updates and stable GitHub release notes.
