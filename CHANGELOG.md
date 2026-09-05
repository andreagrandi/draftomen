# Changelog

- Generate the validated HOB QuickDraft metadata-only profile snapshot with canonical provenance, lifecycle, licensing, and deterministic replay evidence. (#325)
## [Unreleased]
- Use cached ratings or a local fallback for live locked-pair scoring without lazy 17Lands requests, while retaining the current-pack individual win-rate display and visible 17Lands attribution. (#339)
- Add the local `refresh-profile-data` producer command for deterministic set
  selection, cached aggregate 17Lands ratings, validated content-addressed
  profile publication, and bounded partial-failure reporting. (#338)
- Add deterministic per-set Scryfall card-data export, static hosting headers,
  and offline-first hosted artifact loading for live drafts. (#334)
- Add the read-only, bounded CI profile-refresh workflow with explicit
  manual/history dispatch validation, ephemeral cache policy, report-only
  evidence, and privacy-safe summaries; active selection remains local-only
  and no generated profile payload is published. (#303)
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
