# Changelog

- Generate the validated HOB QuickDraft metadata-only profile snapshot with canonical provenance, lifecycle, licensing, and deterministic replay evidence. (#325)

## [Unreleased]
- Publish the first HOB PremierDraft augmented model after it beats Basic DO
  on held-out Top-1 agreement and mean reciprocal rank. Its manifest records
  the source and points to the compressed model by checksum. (#624)
- Publish one validated augmented model with `build-augmented-set SET` only
  when both held-out metrics beat Basic DO. Canonicalize public dump URL format
  casing, read compressed cache objects by gzip signature, keep draft dumps in
  the private cache, and update the manifest after installing the object.
  (#639)
- Train one explicit set from pinned draft data and return a validated runtime
  artifact only when held-out Top-1 and MRR both beat Basic DO. Keep the HOB
  pilot command and reject mismatched set, card, profile, and source data. (#644)
- Package the HOB compact trainer for import without changing its pilot command
  or strict held-out gate. Add a deterministic passing compact regression. (#643)
- Reuse valid published per-set card data without fetching sources or changing
  its bytes. Generate only a missing requested set and reject invalid existing
  files or unsupported set codes without replacing artifacts. (#637)
- Select a listed public 17Lands Draft Data dump for one set, preferring
  PremierDraft before TradDraft and QuickDraft. Reuse validated cached bytes
  and keep the source URL, checksum, retrieval time, and license for later
  augmented training. (#636)
- Explain the augmented DO arithmetic in the desktop card detail. The
  selected-card detail shows Basic DO, the signed adjustment, and the total
  DO Score when augmentation is on, and keeps the plain DO presentation
  otherwise. (#633)
- Show Augmented Intelligence availability and control in the desktop. Settings
  gains a persisted Augmented Intelligence switch that stays off and disabled
  until the active set has a validated model, the live status strip reports
  unavailable, available, or on, and the adapter owns the per-set model load
  off the session thread, failing closed to Basic DO. (#632)
- Apply the validated per-set augmentation delta to live scoring behind one
  default-off session flag. Live totals clamp the bounded artifact adjustment
  onto Basic DO and order on the float total, fail closed to Basic DO when no
  model is usable, and expose per-set availability plus an adapter-owned load
  request with per-row basic and adjustment scores. (#621)

- Add the per-set augmentation artifact contract, set-keyed manifest, and
  offline-first runtime client with content-addressed downloads, atomic caching,
  and framework-free bounded inference. (#620)

- Train the HOB coarse-context Model C with fixed-seed parameters, bounded
  additive calibration, and held-out Basic DO comparisons. Write a local model
  artifact only when both Top-1 agreement and mean reciprocal rank improve.
  (#619)

- Prepare reusable per-set training rows from pinned public draft data with the
  tested 22 pool-summary values, whole-draft chronological splits, source
  provenance, Oracle card IDs, and leakage-safe reports. Resolve full card and
  face names through set metadata so HOB remains the first checked set without
  hard-coding it in the loader. (#618)

- Disable legacy AI-enhanced relationship scoring in production recommendations,
  backtests, live sessions, and the desktop settings control while retaining the
  schema-3 data and explicit offline calibration path. (#617)

- Separate drafted-card relationship advice from ordinary scoring rationale and remove
  confidence and mana-production filler from focused card explanations. Mark cards that have
  advice, show an explicit per-card no-match result, score either endpoint when its partner is
  drafted, preserve all Oracle-derived capability roles in the published profile, and use
  validated draw, exact shared-subtype, Landfall, Ferocious, and Storied evidence in live advice.
  Restrict token-maker classification to creature tokens so Treasure creation cannot produce false
  creature-token synergy claims. (#595)

- Give recovered enrichment publications a current timestamp so normal clients can replace an
  older cached profile instead of rejecting the recovered artifact as stale. (#595)

- Republish the recovered HOB Quick Draft enrichment and expose relationship advice in the
  headless real-Draftmancer pick trace. (#595)

- Show readable drafted-card relationship advice in focused card intel, including useful
  interactions whose effective score increment is zero, and explain the independent contextual
  scoring and AI-enhancement settings in the desktop UI. (#594)
- Score supported and conditional pool relationships within existing synergy bounds, preserve
  zero-effective relationship provenance in audit and backtest output, and avoid generic synergy
  double counting. (#611)
- Classify drafted-pool relationship evidence as supported, conditional, incompatible, or
  unsupported while retaining exact clause, qualification, card, and profile provenance. (#606)
- Recover typed Hone counter sources and Equipment payoffs, including Dwalin-to-Equipment relationships and Sting's qualified self interaction, from saved HOB evidence without provider calls (#607).
- Recover Bilbo's Gambit's complete Gift instruction with its optional opponent promise, Treasure
  object, and qualified spell-lock branch in offline role profiles. (#608)
- Derive qualified source-to-replacement relationships from distinct explicit HOB token creators,
  excluding Army growth, opponent-controlled creation, and duplicate printed instructions. (#612)
- Represent token-creation replacement effects as source-dependent modifiers so they cannot be
  counted as independent token supply during offline relationship conversion. (#610)
- Recover friendly untap effects as typed support instead of disabling removal, including exact
  target, controller, and source-exclusion restrictions in regenerated role profiles. (#609)
- Preserve Adventure component selection and printed exile-then-cast reminders as face-bound
  relationship qualifications while every face shares its parent drafted-card identity. (#592)
- Reconcile saved HOB capability responses during offline resume. The workflow reparses retained
  paid response bytes with the current parser, recovers candidates that older successful results
  silently omitted, preserves stable provenance, and leaves every paid artifact unchanged. (#596)
- Compile source-bound landfall, ferocious, and storied condition maps offline. The
  condition compiler derives helper and payoff capabilities from pinned Oracle text
  with exact-quote evidence, links draft-potential helper edges with thresholds and
  timing preserved, and publishes the map beside reviewed relationships without
  touching finding, run, or confidence math; the offline probe recovers the saved
  #587 family rows through the real `generate-profile` consumer with zero network
  calls. (#591)
- Recover qualified recruit and amass relationships offline. The projection
  compiler binds exact multiline Oracle evidence, retains conditional recruit
  discard, amass mode, Azog controller, and Misty Mountains threshold
  qualifications, recovers qualified payoff participants with source-proven
  cost, choice, timing, and conversion statements, and rejects the four
  subtype-negative pairs as `token_subtype_contradiction`, so all 414 scoped
  HOB findings account through the real `generate-profile` consumer with
  410 qualified rows and four explicit contradictions. (#590)
- Recover qualified relationships through the offline profile pipeline. The
  projection compiler now emits qualified rows: a prerequisite that cannot bind a
  typed clause is retained verbatim as an exact-quote qualification inside the
  participant's own frozen evidence paragraph, so a relationship with one
  untypable statement is projected instead of dropped, and every stored
  relationship gets exactly one deterministic conversion outcome with a
  source-linked reason, one of `decoded`, `qualified`, `missing_evidence`,
  `contradiction`, or `unsupported`, recorded by `generate-profile` in the
  generation report under `relationship_conversions`. The compiler also rejects
  an enabler that consumes a zone its payoff counts, reporting
  `zone_supply_contradiction:<zone>`, unless another capability of the enabler's
  own card face refills that zone from the library or hand. (#589)
- Retain source-bound conditional draft synergies as qualified relationship projections.
  A projection participant may declare the stated cost, choice, condition, mode, party,
  quantity, and timing requirements the clause grammar cannot certify as exact
  `selector`/`occurrence` qualifications inside its own frozen Oracle paragraph;
  declared gaps close `incomplete` verdicts while contradictions still fail, the
  projection reports `decoded` only when nothing is declared and `qualified` otherwise,
  and the optional key keeps legacy artifact and profile bytes unchanged. (#588)
- Document the HOB enrichment coverage audit, source-bound capability and
  relationship ledgers, conditional draft-synergy contract, and offline runtime
  traces. Record missing recovery work without changing production behavior or
  rerunning paid enrichment. (#587)
- Download the Mocked Draft Scryfall bulk source from the dialog. When the resolved
  `scryfall-default-cards.jsonl.gz` is missing the dialog says so and offers **Download
  Scryfall data**, and the click streams Scryfall's `default_cards` bulk JSONL into the
  resolved path through a sibling temporary file with `os.replace`, so progress is
  published as an indeterminate or percentage bar on the existing Mocked Draft state
  channel, a failed or cancelled download never replaces an existing source or installs a
  partial file, the capability re-resolves when the download lands so the dialog is ready
  without a restart, and Start is not gated on the missing file, so it still reports the
  real missing-file error until the download lands.
  (#583)
- Edit and persist the three Mocked Draft sources from the DEVELOPER settings
  section: the Draftmancer checkout directory, the server URL, and the Scryfall
  bulk file each prefill with the effective location, the capability rebuilds on
  the same path as the toggle, and the launch flags stay per-launch overrides.
  (#582)
- Start and stop the pinned Draftmancer server from the Mocked Draft capability.
  `draftomen/draftmancer_server.py` owns the checkout process: it adopts a server
  that already answers the configured location, otherwise starts the pinned checkout
  on a picked loopback port with persistence disabled, waits for a Socket.IO
  handshake, and stops only the process it started on leave, at exit, and after a
  failed start, with every setup failure surfaced as actionable dialog text. (#558)
- Persist Mocked Draft as an off-by-default desktop developer setting with the
  Draftmancer checkout location and server URL. The settings surface exposes it as a
  DEVELOPER toggle that installs or clears the simulated-draft capability live, the
  app-bar action and dialog read "Mocked Draft", `--draftmancer-dir` stays a
  per-launch override, and the in-app path resolves the checkout, card data, and
  Scryfall bulk source from configured or application-data locations instead of the
  process working directory. (#557)
- Serve the confirmed HOB enrichment as a role-bearing profile. Recompiling the
  confirmed artifact `edc7d166…` at the `early` stage publishes
  `objects/75132bf4…json.gz` (schema 3, `enhancement_status=enhanced`, a 138-card
  role profile and 685 relationships) and selects it for `hob/quickdraft`,
  superseding the metadata-only `fd236b38…` object, which stays committed. The
  publication record names the artifact, run, and profile digest, and the
  automated profile refresh reports a retained-enrichment conflict for that
  identity instead of downgrading it, while unrelated identities still refresh.
  The LCI identity is unchanged: its confirmed artifact stores pre-v2 capability
  facts, so no relationship can be projected for it yet. (#573)
- Recover role-bearing enrichment offline. `republish-enrichment` takes the explicit
  generation `--stage` plus the same optional `--ratings-file`, `--source-manifest`, and
  `--draft-source-name` inputs as `generate-profile`, so a confirmed artifact recompiles at
  `early` with its empirical evidence into a schema-3 profile that carries both the enhancement
  and a compiled role profile. The default `--stage metadata` keeps its metadata-only purpose,
  and a role-bearing recovery with missing or unusable inputs exits `1` before any object,
  manifest entry, or publication record is written. (#571)
- Fail `list-enrichment` instead of printing a smaller inventory when a published
  profile object under `<profiles>/objects` exists but cannot be read: the
  per-object read is separated from gzip/JSON decoding in
  `draftomen/enrichment_inventory.py`, so a permission or I/O error exits 1 with
  `list-enrichment failed: Could not read the published enrichment profiles.`,
  while malformed gzip, invalid text or JSON, and non-profile payloads remain
  skippable. (#572)
- Keep enrichment publication provenance across a failed or interrupted publication. The
  durable record in `draftomen/enrichment_publications.py` is written as schema 2 with one
  committed entry per `(set, format)` identity plus at most one pending candidate:
  `publish_profile_publication` records the candidate, writes the manifest entry that names
  the new profile, and only then promotes the candidate, so a failed manifest write or a
  killed process no longer overwrites the provenance of the profile the manifest still
  serves, `filter_enriched_profile_downgrades` protects the retained digest through either
  entry, and a fresh process resolves the leftover candidate against the manifest it finds,
  promoting it when that manifest selects its digest and dropping it when it does not. Plain
  publications never read or write the record, and a schema-1 record still loads. (#570)
- List local enrichment work and where it was published. `list-enrichment` reports
  every run and saved artifact under a store directory with its set, run identity,
  created and reviewed timestamps, review state, relationship and confirmed counts,
  and artifact SHA-256, plus the profiles each artifact was published as, marking an
  identity `orphaned` when the manifest no longer selects the published object, so a
  replaced publication is visible instead of silent. It reads local files only, makes
  no network request and no model call, and defaults to the shared
  `<app data directory>/set-enrichment` store. (#567)
- Recover already-paid enrichment work offline. `republish-enrichment` recompiles
  and publishes one metadata-stage profile from a saved confirmed artifact without
  freezing a guide and without any model call, selecting the newest confirmed
  artifact for the set unless an exact `--artifact` digest or `--run` identity is
  given, and `draftomen/enrichment_publications.py` records `artifact_sha256`,
  `run_id`, `reviewed_at`, `published_at`, and `profile_gzip_sha256` per
  `(set, format)` in `website/public/profiles/enrichment-publications.json`, which no
  website data refresh rewrites or deletes. `filter_enriched_profile_downgrades` now
  reads that record, so an identity whose recorded publication matches its retained
  manifest entry is protected from a plain regeneration even when its retained object
  is missing or unreadable, a record entry that does not describe the retained entry
  never blocks a legitimate replacement, and an unreadable record fails closed. (#566)
- Retain published enriched profiles when an automated website data refresh would
  replace them with plain ones. `draftomen/profile_publication.py` adds
  `filter_enriched_profile_downgrades`, which splits generated replacements into
  accepted artifacts and `EnrichmentDowngradeConflict` records by reading the
  published profile object bytes, and both plain-profile producers —
  `execute_profile_data_refresh` and the refresh workflow's `_materialize_profiles` —
  route every replacement through it before merging, so a downgraded identity keeps
  its manifest entry and object bytes, its object is never rewritten, and the run
  still succeeds; `refresh-profile-data` prints one retained line per conflict,
  `generate_website` records them under `profiles.enrichment_conflicts` and in a
  `### Retained enriched profiles` summary block, and the bundle validator accepts and
  re-validates the new records. Enriched-to-enriched replacement and replacement of a
  non-enriched entry are unchanged. (#561)
- Generate usable profiles from saved confirmed enrichment without new model
  calls. `generate-profile --enrichment PATH` validates the artifact digest,
  frozen guide and card sources, then produces schema-3 profiles with compiled
  roles at the selected evidence-backed stage. Generation recovers eligible
  typed projections from retained local capability facts, preserves the source
  artifact digest and review, and keeps unsupported relationships unchanged.
  Unrepresented conditions fail closed. Clause validation now separates a
  following instruction or activated ability effect from the current clause's
  zones without dropping same-instruction restrictions. Generator version 3
  records the changed deterministic output. (#560)
- Add the simulator-backed native Manual Test Draft acceptance journey (#553):
  `draftomen/qt_gui.py` turns the hidden `--test-draft-smoke` flag into a journey
  selector whose bare form still selects the existing Auto journey while `manual`
  selects the new Manual journey, adds `TEST_DRAFT_MANUAL_PICK_COUNT`, and adds the
  `_TestDraftManualSmokeDriver` behind the narrow `_SmokeControls` port with the
  `_QmlSmokeControls` implementation that locates a control by object name inside the
  running window and activates it with a synthesized Space key, so the driver stays
  testable without a display; the journey drives the real controls in order —
  `testDraftButton`, `testDraftManualModeButton`, and `testDraftStartButton` to start
  a manual draft, `testDraftCloseButton`, a real `wideRecommendationRow2` (or
  `narrowRecommendationRow2`) recommendation row for the first pick,
  `testDraftPickButton` for each of the five picks, then `testDraftButton` and
  `testDraftLeaveButton` to leave — requires the first confirmed pick to be a non-top
  recommendation, and requires every pick to grow the published pool by exactly one
  and to re-render the drafting heading, exits 1 with a named reason on any failed
  requirement or missing capability, and prints one `Test Draft smoke: {...}` manual
  summary line whose `mode`, `set_code`, `picks`, `pool_total`, `non_top_rank`, and
  `status` keys report `manual`, `hob`, `5`, `5`, `2`, and `ok`;
  `tests/bundle_smoke.py` runs both journeys in one invocation behind
  `REQUIRED_MANUAL_PICKS` and validates each journey's summary before printing its own
  compact result; `tests/test_qt_gui.py` covers the selector, the activation order,
  the summary line, the non-top requirement, and the named failures while
  `tests/test_desktop_bundle.py` covers the two-launch argument vectors and the
  summary validation; and `README.md` and `docs/desktop-bundles.md` document the
  developer runbook for the headless, compiled, and interactive runs. (#553)
- Add the developer-only native Test Draft controls to the desktop GUI:
  `draftomen/qml/TestDraftDialog.qml` is the new modal dialog that lists the capability's
  supported set codes with the published default preselected, offers Manual or Auto,
  reports pending and failure text, and dispatches `startTestDraft` and `leaveTestDraft`;
  `draftomen/qml/AppBar.qml` gains a Test Draft action that is visible only while the
  capability is enabled and accented while a simulated draft is active;
  `draftomen/qml/LiveDraftView.qml` gains the Test Draft status indicator, the visible
  failure label, and the Manual Pick button that submits the selected recommendation with
  its published offer generation, so the button stays absent during ordinary Arena
  drafting; `draftomen/qml/Main.qml` mounts the dialog and opens it from the app-bar
  action with focus restoration; the new component is registered in
  `draftomen/qml/qmldir`, `pyproject.toml`, `pysidedeploy.macos.spec`, and
  `pysidedeploy.windows.spec`; and `tests/test_qt_gui.py` gains offscreen QML interaction
  tests covering the opt-in absence, the set and mode dialog dispatch, the Manual pick
  dispatch, the pending and failure states, and leaving the run. (#552)
- Package and smoke-test the native Test Draft (#548):
  `.github/workflows/native-bundles.yml` now syncs the locked `draftmancer` extra with
  `uv sync --locked --extra draftmancer` and keeps `--extra draftmancer` on every build-path
  `uv run` in the `native-bundle` job, so every command there requests the environment —
  the base dependencies plus the optional transport — that the sync step installs before
  Nuitka packages it, while the two
  `tests/bundle_smoke.py` smoke steps stay extra-free so they prove the artifact in an
  environment that does not provide the transport; both `pysidedeploy.macos.spec` and
  `pysidedeploy.windows.spec` declare `--include-package=socketio`, so the bundle carries the
  Socket.IO transport that `draftomen/draftmancer.py` imports lazily for the developer Test
  Draft, while wheel, Homebrew, and source startup keep `python-socketio` optional;
  `tests/bundle_smoke.py` now runs two ordered compiled launches inside one temporary
  directory — the existing mock launch with `--provider mock --smoke-test
  --verify-bundled-profile` and its isolated `--app-dir`, then a default live start with
  `--provider live --offline-profiles --no-startup-scan --smoke-test`, its own app directory,
  an isolated empty `Player.log`, and a screenshot that must be non-empty — and gains an
  opt-in `--test-draft` manual journey mode that rejects those three journey flags unless
  `--test-draft` is given, validates its pinned `--draftmancer-dir`, `--scryfall-bulk-file`,
  and explicit `--app-dir` inputs, probes the Draftmancer endpoint before and after the run
  without ever starting, stopping, or configuring the service, drives the compiled GUI
  through the new hidden `--test-draft-smoke` flag, and requires the cross-process
  `Test Draft smoke: {...}` summary before printing its own compact result, decoding
  captured bundle output as UTF-8 so it cannot fail locale decoding;
  `draftomen/qt_gui.py` gains the hidden `--test-draft-smoke` flag and the
  `_TestDraftSmokeDriver`, which starts the capability's published default set code, reports
  the completed draft and build, leaves the simulated session, and bounds the journey at
  900 seconds, reporting a capability error immediately instead of waiting out that bound,
  so the app gives the reason before the helper's own bound; and
  `docs/desktop-bundles.md` documents the extra-aware build path, the two-launch helper, and
  the manual native journey.
- Add the developer Test Draft opt-in to the native GUI and the serialized worker
  lifecycle behind it (#547): `draftomen/qt_gui.py` accepts `--draftmancer-dir` to pin a
  Draftmancer checkout beside `--scryfall-bulk-file`, `--test-draft-server-url`, and
  `--test-draft-timeout`, builds the runtime factory only behind that explicit opt-in,
  and leaves the mock provider and every run without the flag on the unchanged
  Arena-only contract; `draftomen/qt_adapter.py` publishes the immutable
  `TestDraftSessionState` (capability, authoritative source, phase, mode, set code,
  supported and default set codes, pending flag, opaque `offer_generation`, and error)
  with the `TestDraftFactory` boundary and the `startTestDraft`, `pickTestDraft`, and
  `leaveTestDraft` provider intentions, and runs that lifecycle on the existing worker
  thread, so the Arena `LiveSession` stays retained while a separate simulated source is
  authoritative, Arena snapshots, image completions, and profile refreshes are dropped
  by their source generation while simulated snapshots are dropped by their runtime
  generation, manual picks submit at most once per published offer generation, leaving
  restores the retained Arena session with exactly one poll, and the splash,
  contextual-scoring, and AI-enhanced preference values are shared with the simulated
  session and replayed onto Arena on leave; `draftomen/card_data_client.py` gains
  `cached_card_data_set_codes(...)` and `draftomen/test_draft.py` now exports the shared
  `DEFAULT_TEST_DRAFT_*` constants and accepts an injected `CardImageService`, so the
  offered sets intersect the pinned checkout with locally cached card data.
- Make the test-draft runtime cancellable and reusable (#546):
  `draftomen/test_draft.py` exposes `create_test_draft_runtime(...)`, which performs the
  supported-set, card-data, Scryfall-identity, and profile-source preflight and returns a
  `TestDraftRuntime` owning the isolated source-less `LiveSession`, the Draftmancer
  adapter, the `TestDraftController`, and the implicit simulation directory;
  `TestDraftRuntime.cancel()` wakes a blocked connection, start, or pick operation
  without acquiring the controller lock, without waiting for in-flight session
  publication, and without disconnecting the client, while `TestDraftRuntime.close()`
  retires the controller, session, and temporary directory exactly once in reverse
  ownership order, unwinds partial construction, and reports every construction failure,
  including the implicit simulation directory, as a startup `TestDraftError`;
  `supported_test_draft_set_codes(...)` reuses the existing Draft Omen/Draftmancer
  capability intersection, so HOB is offered only when both sides advertise it;
  `DraftmancerAdapter` opens the transport without blocking on the namespace handshake
  and waits for that handshake, `startDraft`, and `pickCard` on its own condition with a
  public terminal `cancel()`, emits through a Socket.IO callback instead of the
  transport's blocking `call()`, ignores late acknowledgements after a terminal outcome,
  records completion before publishing it, and keeps `close()` as the only
  client-transport teardown, so a blocked operation returns immediately on cancellation;
  and `run_test_draft_auto(...)` keeps its signature, ordered picks, build result, and
  isolated persistence.
- Add the UI-neutral test-draft controller and headless `draftomen-tui test-draft`
  command (#538): `draftomen/test_draft.py` exposes `TestDraftController` for explicit
  Manual confirmation of one inspected offer and recommendation-driven Auto advancement
  over `LiveSession` snapshots, with `TestDraftError` preserving the failure stage, the
  accepted steps, and the last trustworthy snapshot; `run_test_draft_auto(...)` drafts a
  full event through production scoring and the normal `RequestBuild`, keeps simulated
  draft state and audit records in an isolated simulation directory while card-data and
  profile sources stay in the normal application directory, and the command prints one
  `Pack N pick M: <card> (grpId <id>)` line per accepted pick before the unchanged
  deck-builder report.
- Add the pinned Draftmancer typed-event adapter and deterministic protocol
  smoke helper for local three-pack development drafts. (#537) The helper
  resolves Draftmancer printing identities through a locally downloaded
  Scryfall default-cards bulk file without per-card API calls; the required HOB
  smoke completes all 42 picks across three packs and verifies the typed event
  stream, final pool, and persisted source-less `LiveSession`.
- Accept typed `DraftEvent` ingestion directly on `LiveSession` (#536):
  `LiveSession(log_path=None, ...)` builds a deliberate source-less session
  with no `LogFollower`, keeps the incremental parser so `process_lines`
  stays the single Arena-line path, and returns the current snapshot from
  `poll_once`, `scan_startup_files`, and setup-status refresh without
  filesystem work; the existing per-event orchestration (store persistence,
  scoring, profile and card-data activation, audit records, pool evolution,
  ordered event publication, completion) moved unchanged into the new public
  `LiveSession.process_events(events=...)` entry point, which `process_lines`
  now calls after per-line parsing and parser login recovery, so parsed Arena
  lines and direct typed producers share one ingestion boundary.
- Publish the confirmed enriched HOB QuickDraft profile snapshot and manifest entry generated by `enrich-set`.
- Publish the confirmed `enrich-set` result into the production profile tree instead of
  only its local output directory: Confirm now installs the metadata-only QuickDraft
  object at `website/public/profiles/objects/<gzip_sha256>.json.gz`, merges its entry
  into the existing `website/public/profiles/manifest.json` through the shared
  `publish_profile_object` and `merge_profile_manifest_artifacts` publication
  primitives, and reports `published_profile_object`, `profile_sha256`,
  `gzip_sha256` and `profile_manifest`; the content-addressed object is installed
  before the manifest so the manifest stays authoritative, an identical
  republication rewrites neither the object nor the manifest, an absent or
  malformed repository manifest fails closed with `PROFILE_PUBLICATION_ERROR`
  rather than synthesizing one, and Cancel still publishes no profile. (#532)
- Resolve relationship candidates with a role-anchored local matcher (#527):
  the Oracle-text regex pruning is gone, and each constructed candidate now
  carries one deterministic verdict over the v2 capability `action`, `zone`,
  and `qualifier` fields — every declared mechanism
  (`token-go-wide-payoff`, `token-death-payoff`,
  `token-sacrifice-outlet`, `discard-recursion-payoff`,
  `loot-recursion-payoff`, and `mill-graveyard-payoff`) decides locally,
  an explicit structured-field conflict vetoes the pair, and a field the
  parameters do not settle leaves it for the model; only residual pairs
  reach the 20-pair validation batches, so the HOB corpus pays for zero
  model validation batches while every pair keeps a truthful `local` or
  `model` basis and the published artifact records the zero-cost
  `local-pair-matcher-v1` run beside the paid work it did not replace.
- Tolerate per-item card extraction violations and recover them locally
  (#527): a card response that violates one item of the capability contract
  now keeps its other items instead of discarding the whole response, and a
  stored all-or-nothing malformed card result is re-parsed from the paid
  response it retains under an invocation-independent run identifier, so a
  resume repairs the record with no additional model call, no store write,
  and byte-identical provenance across reruns.
- Report pair resolution and validation budget in the HOB harness (#527):
  the report gains `candidates.resolution` with the local and residual pair
  counts, the local share, and the planned relationship validation batch
  count, and a complete unlimited full-source run now fails acceptance
  below 80% local resolution or above 60 relationship validation batches.
- Cut relationship-validation enrichment cost: candidate validation now
  sends bounded batches of 20 pairs per model call instead of one call per
  pair, a local mechanism-compatibility filter prunes pairs whose oracle
  texts cannot express the mechanism before any paid call, and per-batch
  work identities keep runs resumable at batch granularity. (#432)
- Carry matcher-ready structured parameters on card capabilities (#526):
  extracted card capabilities now require a closed primary `action`, a
  `zone`, and a `qualifier` object (card types, token restriction,
  subtype, mana value) alongside the existing fields; card extraction
  requests move to the dedicated `draftomen-card-capability-extraction-v2`
  prompt and response schema while guide extraction stays on its v1
  contract, so previously paid card responses are re-acquired under the
  new identities while guide work is reused, and stored relationship
  results that predate the card v2 fields are revalidated from their
  already-paid responses without a new request.
- Skip end-of-pack handshake picks in `backtest` (#432 follow-up): Arena
  sends a one-card `BotDraftDraftPick` request with `PickNumber` 14 after
  the last pick of each pack, and persisted drafts carrying those records
  crashed backtest with a ledger stage error; such picks are now reported
  as skipped rows with an explicit reason instead of aborting the report,
  and the skipped-picks footnote names the new reason. (#432)
- Expose enhancement availability and control in the desktop interface
  (#519): the shared `enhancement_availability` snapshot state reaches QML
  through the existing plain-value translation, Settings gains an
  `AI-enhanced suggestions` guidance row whose switch dispatches the
  existing `ChangeAiEnhancedSuggestions` command only when availability
  permits it, and the status strip gains a compact enhancement label with
  the same offline/no-live-model copy in its tooltip and accessible
  description. (#519)
- Render the shared enhancement availability in `watch --plain` (#518):
  plain-text output gains a `Status:` enhancement line beside the profile
  status that announces `AI enhancement: On for {set}` with the
  profile-backed offline copy when the active set can use enhanced
  suggestions and republishes the session's own set-specific message
  verbatim for `not-enhanced`, `incompatible`, `unavailable` and `disabled`
  states; unchanged statuses are not repeated across polls and the
  announcement resets when the active set disappears. (#518)
- Render the shared enhancement availability in the Textual interface (#517):
  the status bar gains an `AI enhancement` label that shows the exact
  `On`/`Off` profile-backed offline copy for the `available` and `disabled`
  states and republishes the session's own set-specific message verbatim for
  `not-enhanced`, `incompatible` and `unavailable` sets, while a new `e`
  binding named `AI enhance` dispatches the existing
  `ChangeAiEnhancedSuggestions` preference command only when the shared
  availability permits it — the footer control stays visibly disabled and
  non-dispatching for the other three states through Textual's
  `check_action` gate, refreshed on every session publication. (#517)
- Expose AI-enhanced relationship suggestions as an explicit, user-controlled
  session capability (#440): a new `enhancement_availability` snapshot field
  classifies the active set as `available`, `not-enhanced`, `incompatible`,
  `unavailable` or `disabled` from the set profile's confirmed enhancement,
  its role compatibility, participant resolvability against the loaded card
  database and the user preference, so frontends can render why enhanced
  suggestions are or are not in effect; a new `ChangeAiEnhancedSuggestions`
  command toggles the preference, re-scores the current pack, invalidates the
  backtest result and republishes the invariant-owned availability state;
  `PickEngine`, `project_pool_role_ledger` and `generate_backtest_report` gain
  an `enhanced_relationships_enabled` gate that removes the typed relationship
  synergy term and empties ledger `relationship_support` when the capability
  is off, while the new `relationship_enhancement_is_compatible` predicate
  fail-closes on profiles without a compiled role profile or with
  relationships whose participants cannot resolve; and the mock provider adds
  `not_enhanced` and `enhancement_incompatible` scenarios plus preference
  handling for deterministic frontend development. (#440)
- Calibrate the typed relationship scoring against an offline HOB backtest
  (#510): the nine pre-calibration `0.5` support factors are settled as
  code-owned constants — the two HOB-observed token mechanisms keep their
  deterministic same-card factor and the other seven retain the conservative
  uniform value because no outcome labels support mechanism-specific strengths —
  and a new tracked `hob-relationship-scoring` fixture pair plus
  `scripts/hob_relationship_scoring_smoke.py` prove the effect end to end
  without network access: one persisted pre-pick draft is scored under the
  compiled enhanced profile, the otherwise identical enhancement-removed
  profile, and the all-context-disabled control, exposing exact same-card
  raw-score deltas of `0.190244` (token-go-wide-payoff) and `0.205793`
  (token-sacrifice-outlet), a neutral unsupported row, a saturation row
  whose recommended card is itself the relationship target while the
  generic synergy term already fills `MAX_SYNERGY_TERM`, so the `0.75`
  relationship increment is genuinely clipped, byte-identical determinism
  across repeated controls, unchanged persisted state bytes, and a
  privacy-safe canonical report that carries only allowlisted evidence. The
  token-go-wide-payoff target clause is disclosed as a proxy through the
  report's `relationship_projection_notes`: the closed prerequisite
  vocabulary cannot express the payoff's other-creatures anthem, so the
  flash-condition requirement is encoded without the stated Human subtype.
  (#510)
- Apply bounded, source-bound typed relationship support to pre-pick draft
  recommendations: a confirmed schema-3 relationship whose typed prerequisite
  projection is satisfied by a drafted card that survives the likely-deck
  projection may now add one bounded synergy increment to its exact offered
  target card. `draftomen.pool_ledger` gains an immutable `RelationshipSupport`
  record, a `relationship_support` ledger field serialized for audit, and a
  fail-closed matcher that dispatches the nine approved mechanisms over the
  typed clauses only (exact card identities, compiled role profiles, canonical
  prerequisite strings, no claim/quote/guide/run text). `draftomen.pickengine`
  folds the matched signal into the existing synergy term under the current
  `MAX_SYNERGY_TERM` cap with one uniform pre-calibration factor of `0.5` per
  mechanism, renders "Confirmed relationship support" evidence in the detailed
  rationale, and selects a single maximum instead of stacking findings, copies
  or quantities. Completed-pool evaluation stays relationship-neutral and
  historical replay/calibration remains #510 work. (#509)
- Establish one shared typed relationship prerequisite contract in the new
  `draftomen.semantic_relationship_records` module: an accepted relationship may carry an
  optional directional `prerequisite_projection` whose atomic clauses bind each controlling
  cost, trigger, condition, threshold and supplied output fact to one exact frozen Oracle
  ability paragraph, a closed color/type/zone/controller/quantity/timing vocabulary and the
  complete indices of the capability prerequisites it discharges, with shared validators that
  decline to project incomplete, ambiguous or unsupported prerequisites instead of inventing
  constraints, while a prerequisite that contradicts its own source evidence becomes a
  rejected diagnostic rather than an advisory relationship; `CardRelationship` moves to that
  module and keeps every legacy descriptive
  field, artifacts and profiles without a projection stay loadable and serialize without the
  new key, and relationship duplicate identity now carries the projected direction so
  reversed and distinct-capability relationships no longer collapse. Relationship validation moves
  to the pinned v2 request and response contract while guide and card extraction stay on their
  existing version, so retained v1 relationship results stay readable but are never reused for a
  v2 request. (#508)
- Add the interactive terminal `enrich-set` command that turns the UI-neutral
  set-enrichment workflow into one operator journey on the packaged `draftomen-tui`
  executable: `draftomen-tui enrich-set SET --guide-url URL --output-dir PATH` streams one
  flushed stderr line per guide, capability, candidate and relationship progress event with
  completed/total counts, percentages, tokens, live and projected cost, and executed/reused
  work; prints a plain-text review whose sections separate guide mechanics, Oracle-validated
  card mechanic support grouped by capability role, format/archetype/uncategorised strategy
  claims, named strategy claims with their resolved card names, projected inferred synergies,
  aggregated uncertain reasons, rejected and malformed diagnostics with deterministic subject
  labels, source, guide, model and prompt provenance, the full run, work, card-data and
  pending artifact paths, and the reconciled final accounting; defaults every ambiguous
  decision point — blank input, `Cancel`, end of input, an interrupt — to Cancel, accepts only
  an exact `Confirm`, and reports one-shot invalid input as a cancellation; delegates every
  reviewed-artifact and profile transition to `finalize_set_enrichment` and never generates a
  profile itself, so Cancel prints `profile=not-published` while a Confirm reports the profile
  path, the generation report, and the matching profile and gzip checksums; returns 130 with
  the resumable-work location when analysis is cancelled or interrupted while keeping the
  durable prefix, and reduces every workflow failure to one bounded, path-free
  `enrich-set failed:` line with exit code 1 — retaining the reviewed artifact path when
  confirmation failed after publication of the review. (#504)
- Serve the reviewed LCI QuickDraft enrichment from the production profile tree:
  `website/public/profiles/objects/` gains the schema-3 enriched object and
  `website/public/profiles/manifest.json` now lists it for `lci`/`quickdraft`, so
  clients fetch the enriched profile from the default manifest URL they already
  use instead of an opt-in development tree. The published object is
  byte-identical to the reviewed artifact and keeps its enrichment provenance
  digest.
- Run one set-enrichment analysis as a resumable, UI-neutral workflow through
  `analyze_set_enrichment`, and publish its reviewed outcome through
  `finalize_set_enrichment`, instead of leaving the extraction engine, the
  durable work store and the profile publisher to a future interactive surface:
  the module resolves one output root and confines every cache, run, source, work
  and artifact path to it, re-validating the layout at each phase boundary and
  immediately before publishing and rejecting any nested symlink in the source or
  profile tree, acquires the pinned card data and freezes the guide under
  `enrichment-runs/<set>/<guide-key>/` without clobbering an existing freeze —
  re-validating a reused freeze against the acquisition path's URL rules and a
  strict integer schema version — and replays the card-to-card
  analysis over durable content-addressed work identities, so a compatible rerun
  reuses the completed prefix without a replacement paid request. Analysis
  reports the guide, capability, candidate and relationship phases; ordered
  progress events; running token and cost accounting — including cached input
  tokens — reconciled against exactly one durable model run per request, with a
  null response cost disabling the projected-final-cost claim; globally namespaced
  findings; and the accepted, uncertain, rejected and failed counts, keeping
  candidate omissions out of the failed count. Cancellation before each paid
  request and an interrupted analysis preserve the durable prefix, and a
  provider failure raises a bounded, path-free error with no artifact. A
  completed analysis publishes a pending artifact under the guide-keyed
  artifacts directory and leaves it in place for review; a decision records a
  distinct content-addressed reviewed artifact, installing it without clobbering
  an identical existing object, and rejects an incomplete or already reviewed
  analysis, a blank reviewer ID, a naive timestamp, a timestamp preceding the
  artifact, and a decision that is not an `EnrichmentReviewDecision` value; any
  failure after that review publication carries the persisted reviewed artifact
  on the error. It generates the metadata-only QuickDraft schema-3 profile plus
  its `generation.json` — with matching profile and gzip checksums — only for an
  explicit operator Confirm. Cancel records a cancelled artifact and publishes
  nothing; confirming nothing publishable fails closed with `NO_PUBLISHABLE_ERROR`;
  and a publication failure keeps the confirmed review artifact and leaves any
  existing generation marker authoritative. (#503)
- Compile a confirmed semantic-enrichment artifact into local profile
  generation instead of leaving schema 3 unreachable: `generate_set_profile(...)`
  and `generate_local_profile_artifacts(...)` accept one optional keyword-only
  `enrichment` artifact, `compile_profile_enhancement` is the only schema-3
  writer, and the generation report records the compiled block's privacy-safe
  provenance in an `enhancement` object rather than any guide text, oracle text,
  reviewer identity, or local path. Compilation fails closed before any profile
  bytes exist — an unconfirmed or cancelled review, an artifact `set_code` that
  does not match the requested set, card data whose semantic set-source digest
  differs from the generation card database (a limited-card artifact is rejected
  by design), a card-data identity that cannot be a profile identity, a
  published identity (guide, run, provider, or model) that looks like a local
  filesystem path (absolute, home-relative, or `./`/`../`-relative), or no
  accepted mechanic claim and no confirmed relationship — and each rejection is
  a bounded, path-free message that publication re-raises unchanged. Validation
  also reconciles the report's provenance against the published block. An
  unenhanced generation keeps schema 1 or 2 with byte-identical report bytes and
  no `enhancement` key, a rejected artifact leaves no output directory, and an
  identical replay writes no new object. (#437)
- Carry reviewed model-assisted set enhancement in an explicit schema-3 profile
  block instead of leaving it indistinguishable from empirical evidence: an
  `enhancement_status` of `enhanced` or `not-enhanced` plus a self-contained
  `enhancement` object built from the merged semantic-enrichment vocabulary —
  the artifact and set-source digests, the pinned card data, the pinned cards,
  guides and model runs, the accepted mechanic and relationship findings, and
  the artifact review with its own confidence. The block must cover exactly the
  declared card data, carry at least one accepted finding, hold globally unique
  finding IDs, resolve every finding's run, guide and card reference, and carry
  a confirmed review; an unconfirmed, mismatched or incompatible block is
  rejected rather than partially read, and a schema-1 or schema-2 payload that
  declares enhancement data is rejected rather than silently dropped. Schema-1
  and schema-2 artifacts keep their canonical bytes and report themselves as not
  enhanced, model-assisted claims never enter the empirical `synergy` field, and
  the enhancement compiler is the only schema-3 writer. (#436)
- Decide a `token-death-payoff` relationship from its two frozen participants
  instead of the model's sampling draw: an enabler that creates a creature token
  and a payoff that rewards creatures dying is valid without the enabler's text
  showing the token dying, while a payoff whose quoted reward is restricted to
  nontoken creatures is not declared. Prompt guidance cannot make identical
  evidence receive one verdict — fresh draws of the same pairs arrived 2
  accepted / 2 rejected and 0 accepted / 4 rejected — so the rule is read in
  `parse_relationship_validation_response` before the model's verdict, keyed on
  the declared mechanism, the two participants' roles and the payoff's quoted
  evidence, with the relationship prompt and every work identity left
  byte-identical. The re-derived HOB run answers R5 as an accepted relationship,
  changes no verdict of any other mechanism, and passes acceptance for the first
  time. (#496) (#472)
- Record `token_maker` for a quoted ability that creates a creature token, even
  when the same ability already carries the role of its trigger condition, so a
  landfall payoff or an attack trigger that makes a creature token still pairs
  with a token payoff instead of depending on one sampling draw. An artifact
  creature token qualifies, while a Treasure, Food or Clue token, or a
  quotation that only mentions a creature token, adds nothing. The recorded
  re-run over the frozen HOB sources constructs and accepts
  `token-go-wide-payoff` (`103503` → `103381`) and `token-sacrifice-outlet`
  (`103492` → `103491`), the two expectations the run before it could not
  construct; `acceptance.passed` stays `false` on `token-death-payoff`
  (`103531` → `103448`) alone. (#494) (#495) (#472)
- Match a reviewed HOB mechanic against exact card Oracle evidence as well as
  the guide quotation, so a complete run can evidence `Adventures`, `Hone
  Counters` and `Amass` from the canonical card artifact instead of the one
  guide sentence the extraction happens to quote, and re-author the
  `103531` → `103448` expectation onto `token-death-payoff`, the declared rule
  a token maker and a death payoff can reach. The first complete run under the
  new expectations matched all seven reviewed mechanics — `Adventures`, `Hone
  Counters` and `Amass` only through card Oracle evidence — and three of seven
  relationships, and it failed acceptance on `token-go-wide-payoff`
  (`103503` → `103381`) and `token-sacrifice-outlet` (`103492` → `103491`),
  absent because neither enabler is assigned `token_maker`, and on
  `token-death-payoff` (`103531` → `103448`), whose constructed pair the
  validator rejected. (#472)
- Keep a static ability's card-capability role when the same Oracle text also
  states a keyword ability, so an anthem or a creature-count payoff is a
  `go_wide_payoff` on a card that also recruits and can pair with a token
  maker instead of being replaced by the keyword's role. (#489)
- Define every card-capability role in the set-independent vocabulary and carry
  that glossary, together with the card's declared mana value, power, colors and
  produced mana, inside the pinned card request, so the model assigns a role only
  when a quoted ability satisfies its definition, `sacrifice_fodder` explicitly
  excludes token producers, and the new `token-death-payoff` mechanism pairs
  creature tokens with death payoffs. (#488)
- Re-author two reviewed benchmark relationships that named a mechanism the role
  vocabulary cannot reach: `103492` → `103491` and `103531` → `103458` now expect
  `token-sacrifice-outlet`, the interaction their card texts actually produce and
  the mechanism the paid run already accepted for both pairs, and the benchmark
  records the unassigned `sacrifice_fodder` role, the remaining unreachable
  expectations and the extraction misses behind them. (#472)
- Decide a triggered ability's card-capability role from its trigger event before
  its effect, so an ability that triggers when one or more creatures die is a
  death payoff and can pair with sacrifice fodder. (#485)
- Retain guide findings whose referenced card IDs are not all named in the claim
  for review with the unverifiable references dropped, instead of discarding the
  whole finding and its exact guide evidence. (#472)
- Verify a complete set analysis against reviewed expectations: a committed
  HOB benchmark pairs a frozen Draftsim guide with required mechanics and
  relationship expectations bound to exact Oracle evidence, and a run harness
  enforces a hard USD ceiling, reuses durable work across interruption,
  reports per-card outcomes, token and cost accounting, omission reasons and
  benchmark matches, and proves set profiles stay untouched. (#472)
- Accept the guide and card extraction response schemas against strict
  structured-output validation, which rejects `uniqueItems`, while the
  response validators keep enforcing array uniqueness and non-empty evidence,
  so live set enrichment can run against the provider. (#472)
- Run a complete set analysis through a UI-neutral resumable service that
  coordinates guide, card, candidate and relationship phases over frozen
  sources and a caller-owned work store, reuses matching durable work, exposes
  immutable progress and cost accounting, supports cooperative cancellation
  and interruption-safe resumption, and returns a pending-review result
  without writing set profiles. (#481)
- Validate constructed relationship candidates through a pinned prompt and
  response schema that bind each verdict to the candidate identity and its
  exact Oracle evidence, persist validated relationships under a durable
  relationship work kind, and never turn fabricated, foreign or incomplete
  evidence into an accepted relationship. (#480)
- Persist resumable set-enrichment work through a caller-owned
  content-addressed store that matches input, model configuration, prompt and
  schema hashes before reusing work, keeps raw responses with trusted
  accounting and validated results across interruption, and never reports
  incomplete, corrupt or conflicting artifacts as completed successful work.
  (#470)
- Construct bounded compatible relationship candidates from validated card
  capabilities by indexing typed roles, emitting stable enabler-to-payoff candidate
  packages that retain exact participant identities, evidence, prerequisites,
  timing, zones and quantities, and enforcing an explicit evaluated-pair budget with
  deterministic per-mechanism omission diagnostics instead of enumerating all
  possible card pairs. (#469)
- Extract typed source-bound capabilities from one complete canonical card and
  all of its faces, binding exact Oracle evidence, semantic role, quantity,
  timing, zones and structured prerequisites to the selected card and face
  while retaining unsupported, foreign or fabricated candidates as diagnostics
  and keeping every source-valid interpretation uncertain for semantic review.
  (#474)
- Build deterministic guide extraction requests from frozen guide and card
  inputs, and parse untrusted responses into source-bound format findings,
  mechanics, archetypes and named interaction claims with exact evidence,
  explicit malformed, successful-empty, uncertain and rejected outcomes, and
  conservative acceptance that keeps an exact quotation as an uncertain guide
  claim rather than an Oracle-validated relationship. (#473)
- Make CI signals deterministic by pinning the uv version in every GitHub
  Actions workflow and waiting for the desktop card-preview explanation to
  settle its scroll position before asserting it.
- Submit strict structured-output requests to OpenRouter through a bounded,
  standard-library client that reads its credential only from
  `OPENROUTER_API_KEY`, pins the model, JSON schema and reasoning effort at
  construction, requires provider parameter support, and returns generated
  content separately from trusted API token, cache, provider and billed-cost
  metadata without parsing findings, writing profiles, or mutating production
  artifacts. (#447)
- Acquire user-provided Draftsim guides through a bounded, standard-library
  HTTPS client that enforces allowed hosts, same-origin redirects, response
  size, timeout and UTF-8 content rules while returning verbatim source,
  SHA-256, final URL and retrieval time without writing profiles or
  production artifacts. (#446)
- Define a versioned, strictly validated semantic-enrichment artifact that
  preserves Oracle-derived facts and advisory guide claims separately, records
  card and package relationships with explicit prerequisites, keeps uncertain
  and rejected findings out of the confirmed collection, and pins card, guide,
  prompt, schema, reasoning, usage, and cost provenance in canonical JSON.
  (#433)
- Benchmark pinned local Qwen3.5 4B and 9B Oracle-text extraction across a
  frozen ELD snapshot, retaining evidence, telemetry, and a stop decision
  without changing production scoring or publication. (#426)
- Publish contextual-evidence availability and exact or fallback source formats
  from immutable live-session snapshots, with an accessible desktop status that
  stays coherent across profile and scoring-mode transitions. (#413)
- Repair GitHub-hosted profile refreshes through the official 17Lands API hostname and canonical hosted event format names.
- Render detailed pick rationales as base rating plus material drafter-facing
  deck-fit reasons, omitting rounded-zero and internal bookkeeping while
  retaining structured scoring evidence. (#424)
- Publish QuickDraft profiles from eligible same-set Premier/Traditional
  aggregate evidence when exact coverage is incomplete, preserving actual
  source provenance and replacing fallback sections on later exact refreshes.
  (#421)
- Compose explicitly supplied same-set aggregate ratings independently by card
  and color pair, with versioned source provenance and conservative cross-format
  confidence for QuickDraft fallbacks. (#420)
- Generate hosted profiles through aggregate-only staged acquisition, retain
  prior empirical profiles when evidence is unavailable, and publish canonical
  batch provenance without exposing diagnostic inputs. (#416)
- Add explicit aggregate-only profile acquisition for Oracle metadata and
  17Lands card/color evidence, with verified-cache fallback and privacy-safe
  provenance preserved through staged generation and batch reports. (#415)
- Compile deterministic semantic card roles into EARLY empirical profiles so
  contextual scoring can use Oracle metadata before public draft data exists.
  (#410)
- Show each ranked plain-watch card's unchanged concise pick rationale directly
  below its row while preserving buffered publication identity and replay output.
  (#405)
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
