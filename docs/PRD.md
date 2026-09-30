# PRD — Draft Omen (GUI + CLI/TUI)

*An unofficial draft assistant for MTG Arena Quick, Premier, Traditional and Pick-Two drafts.*

| | |
|---|---|
| **Name** | Draft Omen — binary/command: `draftomen` |
| **Status** | Draft v1.2 |
| **Date** | 2026-07-03 |
| **Platform** | macOS (primary target); cross-platform capable (Python) — Windows supported best-effort |
| **Interface** | PySide6/QML GUI (live default), `draftomen-tui` terminal interface, and plain CLI output for replay/scripting |
| **Target user** | Single user (personal tool), Limited drafter on MTG Arena; may use multiple Arena accounts on the same computer |

---

## 1. Summary

A desktop application that assists a player during **MTG Arena Quick, Premier, Traditional and Pick-Two drafts**. The live PySide6/QML interface watches Arena's local log file, identifies the cards offered in each pack, and displays a live **score-ranked list** (highest → lowest) of the pack's cards based on **17lands statistics for the draft's format**, progressively biasing scores toward the player's committed colors (target: a two-color deck). The stable `draftomen-tui` terminal command provides the same live workflow and plain CLI modes. When the last pick is made, the **deck builder triggers automatically** and proposes a 40-card deck: chosen color pair, 23 spells balanced between creatures and other spells with a sane mana curve, and a mana base (drafted nonbasic lands + basic land counts) — with deck structure informed by what winning 17lands decks look like.

The tool reads data; it never writes to, injects into, or automates the game client.

### Key architectural decision

The tool does **not** read the screen. All MTGA trackers (Untapped.gg, Arena Tutor, the 17lands client) work by parsing the local `Player.log` file that Arena writes when **Detailed Logs (Plugin Support)** is enabled. Draft events appear in the log as JSON payloads containing numeric Arena card IDs (`grpId`), including pack contents and picks. This makes the tool:

- **Language-independent** — the game client can run in any language (e.g., Italian); card identification is by numeric ID, never by on-screen text.
- **Resolution/UI-independent** — no OCR, no computer vision, no screen-capture permissions.
- **Largely platform-independent** — the log format is identical across OSes; only the log *path* differs, which also enables cross-platform support (§3.6).

OCR is explicitly **out of scope** and would only ever be a fallback if a future Arena patch stopped logging pack contents.

### Multi-account model (clarification)

Two distinct notions of "account," both handled:

1. **OS user account** — `Player.log` lives inside the *current OS user's* home directory. The tool always resolves the log path from the running user's `$HOME`, so two people with separate macOS accounts on the same machine each see only their own logs. No configuration needed.
2. **MTGA account** — two Arena accounts may also be used within the *same* OS session. Arena writes the logged-in account identity into the log at session start. The tool detects the active Arena account from the log stream, tags all draft state (pools, in-progress drafts) with it, and displays the active account in the status bar. Switching Arena accounts never mixes pools.

---

## 2. Goals & Non-Goals

### Goals (v1)

1. Live pick recommendations during **Quick, Premier, Traditional and Pick-Two drafts**, from P1P1 through the final pick. The format comes from the event name, and each format's pack rules set the pick count and the cards taken per pick (§3.1).
2. Per-pick display: every offered card with a **single, comparable score**, sorted highest → lowest, showing card name and color(s).
3. Scores driven by 17lands card data for the draft's own format, with automatic fallback to PremierDraft data when that format's samples are unavailable or too thin (early in a set's run, or for rarer formats such as Traditional and Pick-Two).
4. Color-aware scoring that starts open and converges on the best **two-color pair** as the pool grows.
5. **Deck builder auto-triggered when no picks remain** (also runnable on demand): pair selection, 23-spell list with creature/spell balance and curve constraints, land count including drafted duals, basics split by pip count. Output is a build sheet the player replicates in the Arena client. Deck structure targets anchored to winning-deck patterns from 17lands (see FR-5.6).
6. **Multi-account correctness**: per-OS-user log resolution and per-MTGA-account state separation.
7. A **pleasant TUI**: live-updating pack panel, pool/color sidebar, status bar; plain-text mode for replay and scripting.
8. Offline replay: every feature testable against captured log fixtures without spending gems.
9. Compliance with 17lands usage guidelines: fetch the per-set ratings JSON at most ~once/day, cache locally, display clear "Data from 17Lands" attribution.

### Non-Goals (v1)

- Sealed, Cube, and constructed formats such as Standard.
- Screen reading / OCR.
- Match tracking, collection tracking, win-rate dashboards.
- Automatic deck import into Arena (the client does not support Limited decklist import; the build sheet is applied manually).
- Any form of gameplay automation (also a Terms-of-Service requirement, not just a scope choice).
- Unrestricted three-color decks or more than one splash color.
- Official Linux support (Arena doesn't run natively on Linux; Wine/Proton log paths are undocumented — "works if you point `--log-path` at it," nothing more).

---

## 3. Background research (findings)

### 3.1 How trackers get draft data

- Arena writes `Player.log`; requires the in-game setting **Detailed Logs (Plugin Support)** (Settings → Account), then a client restart.
- The log is **reset on every game restart**; the previous session is moved to `Player-prev.log`.
- Quick Draft (bot draft) is the *easy* case: pack contents and picks are logged pick-by-pick, **including P1P1**. Human drafts (Premier, Traditional and Pick-Two) log each pack as a `Draft.Notify` line and each pick as an `EventPlayerDraftMakePick` request, with 1-based coordinates. Arena can skip a notify, including the P1P1 one, or a pick request, so the parser keeps gaps rather than guessing. [log-format.md](log-format.md) records the protocol.
- Premier and Traditional take one card per pick from 14-card packs. Traditional is the best-of-three variant and drafts the same way. Pick-Two takes two cards per pick, so each pack gives 7 logical picks and the draft ends with 42 cards from 21 picks.
- Historical event markers for bot drafts in reference implementations: `BotDraft_DraftStatus` / `BotDraft_DraftPick` carrying JSON with pack number, pick number, and card IDs. **Exact current tokens must be confirmed empirically** (see M0) — event names drift across Arena patches.
- Session-start log lines carry the logged-in Arena account identity (screen name / user id); exact token to be confirmed at M0 alongside the draft events.
- As a cross-check, Arena also logs the event card pool when entering the deck-building screen.

### 3.2 17lands data access

- 17lands exposes the card statistics shown on its site via JSON endpoints (e.g., card ratings filtered by `expansion` and `format`). **QuickDraft exists as a distinct format** in their data, alongside PremierDraft, TradDraft, etc.
- Their usage guidelines: automated scraping is discouraged and rate-limited, API stability is not guaranteed, attribution is required, and public data dumps (CC BY 4.0) are the preferred bulk channel.
- 17lands also publishes **winning-deck information**: trophy decks (7-win decks) per set/format on the site, and full deck compositions in the public data dumps. This is the source for "make the proposed deck resemble winning decks" (FR-5.6).
- **Decision:** fetch the small per-set/per-format ratings JSON, cache locally, refresh at most daily. Do not scrape. Bulk dumps reserved for the structural-targets analysis (M6).
- Cards with fewer than ~500 samples have **no win-rate data** on the endpoint; the pick engine needs a fallback for these.

### 3.3 Card identity mapping

- `grpId` → card metadata via **Scryfall bulk data** ("default cards"), filtering entries that carry an `arena_id`. Provides name, colors, mana value, rarity, type line.
- Alternative for day-one coverage of brand-new sets: Arena's own local SQLite card database inside the app bundle. Noted as a fallback; v1 uses Scryfall.
- Display names default to English regardless of client language; localized display names are possible later via Scryfall localized prints.

### 3.4 Prior art & code reuse policy

- `bstaple1/MTGA_Draft_17Lands` (archived June 2023) and actively maintained forks implement substantially this tool (multi-format, GUI).
- **Policy:** these repos are *reference material* — for log token names, log-rotation handling, and the "Auto" deck-filter heuristic (all-decks ratings for ~15 picks, then pool-matched color filter). Algorithms and ideas are reimplemented in our own codebase; no verbatim code is copied without checking license compatibility.

### 3.5 Fair play / ToS

- Log-reading draft assistants operate openly and are widely tolerated (17lands' own client included). The tool must never automate input or reveal hidden information. Re-check Wizards of the Coast's current third-party application policy before any public distribution.

### 3.6 Cross-platform notes

The codebase is pure Python and OS-agnostic; platform surface is confined to log-path resolution:

| OS | Default `Player.log` location | Support level |
|---|---|---|
| macOS | `~/Library/Logs/Wizards Of The Coast/MTGA/Player.log` | Primary — developed & tested here |
| Windows | `%USERPROFILE%\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log` | Best-effort — path resolution implemented, untested until tried |
| Linux (Wine/Proton) | inside the Wine prefix (varies) | Unsupported — works via `--log-path` override only |

Both defaults derive from the running OS user's home directory, which is what guarantees per-OS-user correctness (Goal 6) for free.

---

## 4. User stories

1. *As a drafter*, when a draft pack is offered, I see within ~1 second every card in the pack with a **score**, ordered highest to lowest, with card name and color(s), so I can pick confidently at a glance.
2. *As a drafter*, as my pool grows, scores increasingly favor my strongest two colors, so I end up with a coherent bi-color deck.
3. *As a drafter*, at any point I can see my current pool, the tool's inferred color pair, and which Arena account the session belongs to.
4. *As a drafter with two Arena accounts on this computer*, the tool always reads the current OS user's logs and keeps each Arena account's drafts and pools separate, so recommendations never mix accounts.
5. *As a drafter*, **when there are no more picks to make, the deck builder triggers automatically** and proposes a 40-card deck — ideally resembling the structure of winning (trophy) decks from 17lands data for my color pair.
6. *As a drafter*, I can re-run the builder later (Quick Draft allows building any time before playing) and force a different color pair if I disagree.
7. *As a drafter*, I can see the 3–5 nearest cuts ("bench") with one-line reasons, so overriding the tool is easy.
8. *As a drafter*, the interface is a clean, live-updating terminal UI — not a scrolling wall of text.
9. *As a developer*, I can replay a captured `Player.log` and get deterministic plain-text output, so parser and algorithm changes are regression-tested without spending gems.

---

## 5. Functional requirements

### FR-1 Log follower & platform resolution

- FR-1.1 Resolve the log path from the **current OS user's home directory**, per-platform defaults as in §3.6; `--log-path` overrides.
- FR-1.2 Tail `Player.log` by polling (~1 s interval) from a persisted byte offset.
- FR-1.3 Detect truncation/recreation (inode change or size shrink) and reset the offset; on startup, optionally scan `Player-prev.log` + current log to recover an in-progress draft.
- FR-1.4 Tolerate partial trailing lines (buffer until newline).

### FR-2 Draft event parser & account awareness

- FR-2.1 Detect the **active MTGA account** from session-start log events; expose it to the UI and state layer. If the account changes mid-stream (relog), close out the previous account's context cleanly.
- FR-2.2 Detect draft entry and format from event metadata before the first pack: the live `EventJoin` request, the course state, or for human drafts the table draft queue line. Never infer the format from pack contents. Historical startup scans must not present an old entry as the upcoming draft.
- FR-2.3 For each pick: extract pack number, pick number, offered card `grpId`s.
- FR-2.4 Extract the player's chosen cards for each pick, one or two depending on the format; maintain the pool.
- FR-2.5 Detect **draft completion** (explicit event if present; otherwise inferred at final pick with full pool count) — this is the auto-trigger for the deck builder.
- FR-2.6 Persist draft state (pool, picks, set, timestamps) to disk keyed by **(MTGA account id, draft/event id)**.
- FR-2.7 Parser is built and tested against **captured fixture logs** checked into the repo, never against assumed formats.
- FR-2.8 Unknown/changed event formats fail loudly with a clear diagnostic (never silently skip a pick).

### FR-3 Static data layer

- FR-3.1 Scryfall bulk download → local `grpId → {name, colors, mana_value, rarity, types}` map; cached; manual `refresh-data` command.
- FR-3.2 17lands ratings fetch per (set, format) and all-time period, where format is the draft's own 17lands format: GIH WR, OH WR, ALSA, IWD, sample counts; cached with timestamp. The all-time period preserves historical samples when a set returns to draft. The TUI asks before the first download for a set, shows request progress, replaces legacy date-range caches, and auto-refreshes an existing current cache if it is > 24 h old.
- FR-3.3 Fallback chain: the draft's own format → PremierDraft data (flagged in UI) → neutral prior. Quick uses `QuickDraft`, Premier uses `PremierDraft`, Traditional uses `TradDraft` and Pick-Two uses `PickTwoDraft`. [pick-scoring.md](pick-scoring.md#draft-formats-and-ratings-fallback) holds the full table.
- FR-3.4 Color-pair win rates for the set (10 two-color pairs) fetched and cached on the same cadence.
- FR-3.5 Attribution line "Card data from 17Lands (17lands.com)" visible in the TUI footer and on build sheets.
- FR-3.6 All caches and state live under a single app data directory (e.g., `~/.draftomen/`), with per-account subdirectories for draft state.
- FR-3.7 Before P1P1, show exactly one set-level 0–100 reliability value derived from aggregate own-format coverage, Premier fallback coverage, and sample depth. Hide it when the first pack appears. This value is presentation-only and must never enter card scoring, ranking, fallback resolution, color commitment, backtests, benchmarks, or deck building.

### FR-4 Pick engine & pick display

- FR-4.1 Base card rating = GIH WR; when absent (< 500 samples), fall back to a neutral prior adjusted by ALSA.
- FR-4.2 **Displayed score**: a single 0–100 value per card = base rating normalized against the set's distribution, multiplied by the color-commitment factor (FR-4.4). One number the drafter can compare at a glance; recomputed per pick, so the same card can score differently as commitment shifts.
- FR-4.3 Pool color weights: each picked card contributes its own quality to its colors (a bomb pulls harder than filler).
- FR-4.4 Commitment ramp by pick index: picks 1–5 ≈ raw ratings; picks 6–15 growing on-color bonus; pick ≥ ~16 effectively locked to the best pair; off-color cards still displayed, penalized, and marked.
- FR-4.5 Where 17lands provides pair-filtered card ratings with adequate samples, use them once a pair is locked; otherwise all-decks ratings.
- FR-4.6 **Per-pack display (primary requirement)**: cards sorted by score, highest → lowest. Columns: rank, **score**, **card name**, **color(s)** (rendered with mana-color styling in the TUI). Secondary columns (grade, GIH WR, ALSA, MV, on-color marker) available but visually subordinate. Status line: active account, inferred pair, pick counter, pool size, data source in use (Quick / Premier fallback).

### FR-5 Deck builder

- FR-5.1 **Trigger: automatic**, immediately when FR-2.5 fires (no more picks to make — before any match is played and regardless of event completion). Also manual via `build` subcommand against any persisted pool (`--account` to disambiguate if needed).
- FR-5.2 **Stage 1 — pair selection.** Score all 10 pairs: sum of top ~23 playable card scores (in-pair + colorless + castable artifacts), blended with the pair's set-level 17lands win rate. Report chosen pair, runner-up, and score gap. `--pair XY` flag forces a pair.
- FR-5.3 **Stage 2 — 23 spells.** Greedy fill by score under structural constraints:
  - creature floor: 14–17 creatures (configurable defaults);
  - curve quotas: minimum ~5–6 cards at MV 2, soft cap ~2–3 at MV ≥ 6;
  - when a creature and non-creature are near-equal in score and the creature floor is unmet, prefer the creature;
  - splash rule on by default and optional through TUI configuration or `--no-splash`: one additional color, ≤ 2 `A-` or better single-pip cards, with three sources for one card or four for two (including at most one planned basic).
- FR-5.4 **Stage 3 — mana base.** Default 17 lands; 16 if aggressive (low avg MV + 2-drop quota filled), 18 if top-heavy. Drafted in-color nonbasics slot in first (documented caveat: prefer basics over taplands in 16-land aggressive builds). Remaining basics split proportionally to colored pips in the final 23, with a per-main-color floor (~7 sources), rounding toward the double-pip-heavy color.
- FR-5.5 **Build sheet output:** pair + its 17lands WR; spells sorted by curve, creatures and non-creatures separated; land section (nonbasics first, then basic counts); bench of 3–5 nearest cuts with one-line reasons (e.g., "cut: 5th 4-drop, curve full"). Exactly 40 cards, always.
- FR-5.6 **Winning-deck alignment.** Two tiers:
  - *v1 (required):* the structural constraint defaults in FR-5.3/5.4 are set from established Limited consensus, which itself reflects winning-deck norms; the build sheet shows the chosen pair's 17lands win rate as context.
  - *v1 bonus / M6 (stretch):* derive per-pair structural targets empirically from 17lands winning decks (trophy decks and/or public data dumps): average creature count, curve shape, land count for successful decks of that pair in this set. The builder then (a) uses those as its constraint defaults and (b) prints a short **similarity report** — e.g., "trophy WU decks in this set: avg 16.1 creatures / 16.9 lands; your build: 15 / 17."
- FR-5.7 Deterministic: same pool + same cached data ⇒ same deck (stable tie-breaking).

### FR-6 Interface (TUI + plain CLI)

- FR-6.1 **Live mode is a TUI** (full-screen terminal app): pack panel (score-sorted table, FR-4.6), sidebar with pool summary (color distribution bar, mana curve sparkline, last picks), footer status bar (account, pair, pick counter, data source, 17lands attribution).
- FR-6.2 On draft completion, the TUI switches to the **build view** (build sheet + bench), with a keybind to force a rebuild with another pair.
- FR-6.3 Minimal keybindings: quit, toggle secondary stat columns, cycle ranking (17L WR / DO score / ALSA / MV), open build view, rebuild with pair override, and reopen the missing-ratings download offer.
- FR-6.4 `replay <logfile>` and `build --pool <file>` run in **plain-text mode** (deterministic, pipe-friendly, no TUI) for testing and scripting. `watch --plain` also available for minimal environments.
- FR-6.5 Graceful degradation on narrow terminals (hide secondary columns first).

### FR-7 Replay & tooling

- FR-7.1 `replay <logfile>` runs the full pipeline offline over a fixture and prints everything the live mode would (plain mode).
- FR-7.2 Fixture drafts are the regression suite; CI (or a make target) replays them and diffs output.

---

## 6. Non-functional requirements

| # | Requirement |
|---|---|
| NFR-1 | New-pack-to-display latency ≤ 1.5 s on a typical machine. |
| NFR-2 | Read-only with respect to the game: no injection, no input automation, no network interaction with Arena. |
| NFR-3 | Network calls only to Scryfall and 17lands; graceful offline degradation using caches. |
| NFR-4 | ≤ 1 ratings fetch per set/format per day (17lands guidelines). |
| NFR-5 | No elevated permissions on any platform (log file is user-readable; no screen-recording or accessibility permissions). |
| NFR-6 | Parser failures are loud and diagnosable (raw offending log line printed / saved). |
| NFR-7 | All tunables (creature floor, curve quotas, land thresholds, commitment ramp, score normalization) in one config module with documented defaults. |
| NFR-8 | Pure-Python, OS-specific code confined to path resolution; must run on macOS and Windows (best-effort) without code changes. |
| NFR-9 | TUI remains responsive during network fetches (fetches off the render loop). |

---

## 7. Technical design

### 7.1 Stack

- **Python 3.12+**.
- **Textual** for the TUI (same ecosystem as `rich`, which it uses under the hood); `rich` alone for plain-text mode tables.
- `httpx` (or `requests`) for HTTP; stdlib elsewhere.
- Rationale: reference implementations are Python (easy cross-reading); fastest iteration for log/JSON work; Textual gives a genuinely nice terminal UI with little code and runs on macOS/Windows/Linux terminals alike. A Swift/Go port remains a possible later step once the parser has stabilized — not before.

### 7.2 Modules

```
draftomen/
  paths.py        # FR-1.1 / NFR-8: per-OS log path & app-dir resolution
  logfollow.py    # FR-1: offset-persisted poller, rotation handling
  events.py       # FR-2: account detection, bot and human draft events, completion detection
  carddb.py       # FR-3: Scryfall bulk → grpId map (cache)
  seventeen.py    # FR-3: 17lands ratings + pair WRs (cache, fallback chain, attribution)
  pickengine.py   # FR-4: ratings → normalized 0–100 score, color commitment
  deckbuilder.py  # FR-5: pair selection, constrained fill, mana base, similarity report
  pool.py         # per-(account, draft) state & persistence
  tui.py          # FR-6: Textual app (pack view, build view)
  cli.py          # `draftomen-tui watch | replay | build | refresh-data`
  config.py       # NFR-7: tunables
tests/fixtures/   # captured Player.log files (M0 deliverable)
```

### 7.3 Data flow

```
Player.log ──tail──▶ events.py ──account/picks/packs──▶ pool.py (per account)
                                        │
              carddb.py (grpId→card)    │    seventeen.py (ratings, pair WRs)
                                        ▼
                     pickengine.py ──scored, sorted pack──▶ tui.py (live)
                                        │
                       (no picks left → auto-trigger)
                                        ▼
                     deckbuilder.py ──build sheet (+similarity)──▶ tui.py / stdout
```

### 7.4 Localization note

Client language is irrelevant to correctness (numeric IDs end-to-end). Output names are English via Scryfall; localized display names are a possible future flag.

---

## 8. Milestones

| # | Milestone | Deliverable | Exit criterion |
|---|---|---|---|
| M0 | **Capture** | Fixture `Player.log` from one real Quick Draft (detailed logs on); document observed event tokens incl. account identity & completion signal | Fixture committed; tokens confirmed against reference repos |
| M1 | **Offline replay** | Parser + Scryfall mapping over the fixture: prints every pack/pick with names; account id detected | `replay` reproduces the full draft deterministically |
| M2 | **Live tail** | Same output streaming during a real draft (plain mode); rotation/truncation handled; per-account state dirs | Survives Arena restart mid-session |
| M3 | **Ratings & score** | 17lands fetch/cache/fallback; 0–100 score; score-sorted plain table with name + colors; attribution | Tool already usable for real drafting |
| M4 | **Color logic** | Pool weights, commitment ramp, inferred-pair status | Tuned by replaying captured drafts |
| M5 | **Deck builder** | Auto-trigger on last pick; pair selection; constrained 23-spell fill; mana base; build sheet + bench; `build` re-run with `--pair` | Sensible 40-card builds on all fixture pools |
| M6 | **TUI** | Textual app: pack view, sidebar, status bar, build view, keybinds | Full draft ergonomically usable end-to-end in the TUI |
| M7 | **Stretch — winning-deck alignment** | Per-pair structural targets from 17lands trophy/winning-deck data; similarity report; optional conservative splashing | Similarity line on build sheets for sets with data |

M0–M5 are each sized as a weekend-or-less chunk; the TUI (M6) is deliberately scheduled after the logic is proven in plain mode, so UI polish never blocks core function. M7 is the "bonus points" item and the only piece requiring 17lands deck-level data.

---

## 9. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Arena patch changes log event names/format | High (recurring) | Tool breaks until patched | Fixture-driven parser; loud failures (FR-2.8); maintained community forks as canary; parser isolated in one module |
| Account-identity token in logs is unclear/absent | Low–medium | Per-account separation degraded | Confirm at M0; fallback: hash of whatever stable session identity exists; worst case, `--account` manual flag |
| Own-format data absent/thin early in a set or for Traditional and Pick-Two | Certain, cyclically | Weaker recommendations | Automatic PremierDraft fallback, clearly flagged (FR-3.3) |
| Cards under 500 samples have no WR | Certain for some rares | Scoring gaps | ALSA-adjusted neutral prior (FR-4.1) |
| GIH WR bias (deck/player quality confounds) | Inherent | Occasional bad advice | Score not gospel; bench output makes overrides easy; documented limitation |
| 17lands endpoint shape changes (no stability guarantee) | Medium | Data layer breaks | Thin fetch module, cached last-good data, daily cadence keeps us low-profile |
| Trophy/winning-deck data access differs from ratings endpoint | Medium | M7 slips | M7 is stretch by design; v1 ships with consensus defaults |
| New set, day-one: no 17lands data at all | Certain, cyclically | No scores for ~1–2 weeks | Tool still shows pack contents; user drafts on judgment; (future: manual tier-list import) |
| ToS drift on third-party tools | Low | Tool must change/stop | Read-only design; no automation; re-check policy before any distribution |
| Scryfall lag on new-set `arena_id`s | Low–medium | Unknown card names day one | Acceptable for v1; Arena local SQLite DB documented as fallback |
| Windows path/rotation quirks differ | Medium (untested) | Best-effort target broken | Path logic isolated in `paths.py`; fix on first report |

---

## 10. Open questions

1. Confirm exact current Quick Draft event tokens from the M0 fixture (expected in the `BotDraft_*` family).
2. Confirm the account-identity token at session start and its stability across patches (M0).
3. Does the current log include a distinct, reliable "no more picks" event for Quick Draft, or is completion inferred at final pick + full pool count? (Both checked at M0; inference is the fallback and is sufficient for the auto-trigger.)
4. Pair-filtered card ratings: verify per-pair sample sizes are usable for QuickDraft or whether pair filtering should apply to Premier data only.
5. M7 data source: trophy-deck endpoint vs. public data dumps — pick whichever is lighter and within usage guidelines when M7 starts.
6. Score presentation: single 0–100 integer confirmed; decide at M3 whether ties show one decimal.

## 11. Future work (explicitly deferred)

- M7 hardening: per-set/per-pair empirically fitted structure targets from 17lands public data dumps (CC BY 4.0), refreshed per set.
- Broader three-color archetype support beyond the conservative one-color splash policy.
- Native UI (menu-bar / floating panel) on top of the stabilized core.
- Localized card names in output.
- Manual tier-list import for day-one new sets.
- Packaged distribution (signed/notarized binary) if ever shared beyond personal use.

---

## 12. Naming & branding

- **Name:** Draft Omen. Command/binary, package, and app-dir all use `draftomen`. Chosen for memorability, terminal-friendliness, and distance from existing tools in the space (Untapped.gg, Arena Tutor, 17Lands, Draftsim, Draftmancer, MTGA Assistant, etc.). "SnapPick" was considered and rejected (existing iOS app and Chrome extension).
- **Trademark constraint:** per WotC's Fan Content Policy, Wizards trademarks (including "Magic: The Gathering," "MTG," "Magic," and Arena branding) must not be incorporated into the project name or logo. They may be used descriptively in prose (e.g., the tagline "an unofficial Quick Draft assistant for MTG Arena"), which is nominative use.
- **No confusion:** nothing in the branding may imply endorsement by, or affiliation with, Wizards of the Coast or 17Lands.
- **Required disclaimer** (README, `--version` output, and any published page):

  > Draft Omen is unofficial Fan Content permitted under the Fan Content Policy. Not approved/endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC. Card data from 17Lands (17lands.com); 17Lands does not endorse this tool.

- **Before first publication:** re-verify name availability on GitHub/PyPI and register the PyPI name early.

---

*Draft Omen is unofficial Fan Content permitted under the Fan Content Policy. Not approved/endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC. Card data from 17Lands (17lands.com); this tool is unaffiliated with and not endorsed by 17Lands.*
