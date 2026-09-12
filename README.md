<p align="center">
  <img src="https://raw.githubusercontent.com/andreagrandi/draftomen/master/docs/assets/draftomen_banner.png" alt="Draft Omen banner" width="100%">
</p>

# Draft Omen

Draft Omen is an unofficial desktop draft assistant for MTG Arena Quick Drafts. Its PySide6/QML application reads Arena's local `Player.log`, recognizes each pack and pick, and ranks the available cards while you draft. When the draft is complete, it also suggests a 40-card deck from your pool. A terminal interface with the same draft behavior is available as `draftomen-tui`.

Draft Omen is read-only: it does not write to, inject into, or automate MTG Arena. Card details come from [Scryfall](https://scryfall.com/) and draft statistics come from [17Lands](https://www.17lands.com/).

## Screenshots

### Live Draft GUI — pick recommendations

The Live Draft view shows Pack 1, Pick 3 with 12 cards available and Glóin the Mighty selected. Its DO Score, 17Lands win rate, grade, and color fit appear alongside focused-card details and a pool summary.

![Draft Omen Live Draft GUI showing Pack 1, Pick 3 with 12 cards available, Glóin the Mighty selected, ranked card rows, DO Score, 17Lands win rate, grade, color fit, focused-card details, and pool summary](https://raw.githubusercontent.com/andreagrandi/draftomen/master/docs/assets/draft-pick-recommendations.png)

### Suggested deck GUI

After the draft, the Suggested deck view shows an automatic UR pair and a 40-card build with 23 spells, 17 lands, a mana base and curve, grouped main-deck spells, and selected-card details.

![Draft Omen Suggested deck GUI with an automatic UR pair, 40-card build with 23 spells and 17 lands, mana base, mana curve, grouped main-deck spells, and Tidings of War selected](https://raw.githubusercontent.com/andreagrandi/draftomen/master/docs/assets/suggested-deck-build.png)

## How recommendations work

Draft Omen keeps the data behind every recommendation visible instead of presenting a black-box pick order:

- **17Lands ratings:** each card shows its Games-in-Hand win rate (`17L WR`) and a 17Lands-style grade. Quick Draft data is preferred; Premier Draft data is used as a fallback when Quick Draft samples are missing or too small.
- **DO Score:** the default 0-100 ranking normalizes 17Lands win rates across the set. A card without a reliable sample starts from a neutral score of 50, adjusted by its Average Last Seen At (`ALSA`) when available. The five freely available basic lands instead score 0 and rank after draftable cards.
- **Color fit:** early picks stay open. From pick 6 onward, scores gradually favor the colors supported by the drafted pool; from pick 16, on-color cards receive the full bonus and off-color cards the full penalty. Strong picks influence the inferred color pair more than filler.
- **Close early picks:** when two cards have similar scores, set- and format-specific 17Lands color-pair win rates can break the tie without forcing an early commitment.
- **Conservative splashing:** an `A`-range, single-pip card can be marked as a visible third-color splash when it is materially better than the on-color choices and the pool can support its mana. Draft Omen limits this to one extra color and two cards.
- **Transparent alternatives:** press `s` to compare rankings by DO Score, raw 17Lands win rate, ALSA, or mana value. The interface also identifies neutral-prior and Premier-fallback rows.
- **Deck suggestion:** the builder evaluates two-color pairs using card quality and 17Lands pair performance, then chooses spells and lands while considering creatures, curve, colored mana requirements, and cached 17Lands deck-structure targets when available.

DO Score is the default because it performed better than raw 17Lands win rate in Draft Omen's offline pick benchmarks. It is still guidance rather than a perfect pick order: public statistics reflect the decks, players, and contexts in which cards were played.

For the complete methodology, see [pick scoring](docs/pick-scoring.md), [benchmarking](docs/benchmarking.md), and the [deck builder](docs/deck-builder.md).

## How to use it

### 1. Install

On macOS, install Draft Omen with Homebrew:

```bash
brew install andreagrandi/tap/draftomen
```

On other platforms, [install uv](https://docs.astral.sh/uv/getting-started/installation/) and run:

```bash
uv tool install draftomen
```

### 2. Enable Arena logs

In MTG Arena, open **Settings → Account**, enable **Detailed Logs (Plugin Support)**, and restart Arena.

### 3. Start Draft Omen

Start Draft Omen before entering a Quick Draft:

```bash
draftomen
```

The PySide6/QML desktop application loads card metadata when needed, watches
Arena's standard log location, detects the set, and follows the draft
automatically. Native and terminal live sessions use the shared set-profile
lifecycle as their sole ratings authority. They load a validated local profile
cache first, so a warm valid profile is active immediately while an allowed
hosted-profile refresh proceeds. If the production manifest (or an explicit
override) is absent, inaccessible, invalid, or has no profile for the set, the
cached profile is retained; with no usable cache, deterministic fallback scoring
remains active. Live sessions do not load ratings directly from 17Lands.

Native and terminal live commands use
`https://www.draftomen.com/profiles/manifest.json` by default. For the desktop
command, use `--profile-manifest-url URL` to select an explicit HTTPS manifest,
or `--offline-profiles` to select profile-only offline mode:

```bash
draftomen --profile-manifest-url "$PROFILE_MANIFEST_URL"
draftomen --offline-profiles
```

`--offline-profiles` selects `ProfileNetworkPolicy.OFFLINE` and takes
precedence over profile networking only. It does not disable Scryfall card
metadata, card images, or static card-data networking.

The TUI `d` action and the native ratings refresh control use the existing
shared profile-refresh command boundary. A newer validated profile reports an
`updated` outcome and changes recommendations in place; an equal result reports
`unchanged` and leaves the current ratings active. Failed, offline, or missing
refreshes retain the last usable cache when ratings exist; with no usable
profile, deterministic fallback scoring remains active. Repeated requests
coalesce, and `watch --plain` has no command UI.

### Terminal interface

For a terminal workflow, use the stable `draftomen-tui` command. It preserves
the watch, replay, build, backtest, benchmark, data-refresh,
`export-set-data`, local `generate-profile`, `refresh-profile`, and interactive
`enrich-set` subcommands:

```bash
draftomen-tui
draftomen-tui watch
draftomen-tui watch --plain
draftomen-tui watch --offline-profiles
draftomen-tui watch --profile-manifest-url "$PROFILE_MANIFEST_URL"
```

Terminal `watch` and `watch --plain` use the production hosted manifest by
default. `--profile-manifest-url` selects an alternate HTTPS manifest, and
`--offline-profiles` disables only profile networking; Scryfall card data,
images, and other static data sources retain their own network/cache behavior.

To generate a deterministic set profile from pinned input files, use
`generate-profile` with explicit set, format, stage, timezone-aware timestamp,
card-database, and output paths. The producer and cache workflow, including
remote manifest fields, validation, refresh, recovery, and explicit provider
ingestion, is documented in [set profiles](docs/set-profiles.md).

To enrich one set from a published draft guide, use the interactive
`enrich-set` command. It freezes the given guide URL and the pinned card data
under the selected output directory, streams per-phase progress with running
token and cost accounting, prints a review of accepted, uncertain, rejected,
and failed findings with source, model, prompt, and path provenance, and
publishes the metadata-only QuickDraft profile only for an explicit `Confirm`
at the prompt. Every other input, including an empty line, cancels, publishes
nothing, and keeps the resumable work for a later run:

```bash
draftomen-tui enrich-set LCI \
  --guide-url https://draftsim.com/mtg-lci-draft-guide/ \
  --output-dir "$HOME/.draftomen/set-enrichment"
```

### Static set card data

The live card lookup uses one schema-versioned (schema version `1`), canonical
Scryfall artifact per Arena set. Artifacts are published at
`website/public/card-data/<lowercase-set-code>.json.gz` and hosted at
`https://www.draftomen.com/card-data/<lowercase-set-code>.json.gz`.
Generate one set by exact code or full name, or discover every eligible set:

```bash
draftomen-tui export-set-data SET
draftomen-tui export-set-data
```

Discovery makes one `17Lands /data/expansions` request and one Scryfall
default-cards bulk acquisition. No-argument discovery requires at least 200
distinct Arena card identities per set; paper-only, Cube, Chaos, and Remix
sets are excluded. `--inventory-file PATH` and `--bulk-file PATH` replace
those network sources for reproducible or offline runs. The all-set command
validates existing artifacts, publishes pending sets in code order, and can be
rerun to resume from the first pending set.

In live mode, Draft Omen detects the set before loading its selected artifact.
The default cache path is the application-data `card-data/<set-code>.json.gz`
file (normally `~/.draftomen/card-data/<set-code>.json.gz`); a valid cache is
reused without network access. Card metadata is automatically loaded at most
once per pre-draft detection lifecycle for a selected set; no card-data network
request is made after `DraftStartedEvent`.
Printed card fields and images come from Scryfall; 17Lands ratings and
statistics remain a separate data source and are not embedded in card-data
artifacts.

In the TUI, press `r` to retry a recoverable card-data error. Network repair is
available only before draft start; after `DraftStartedEvent`, retries use the
local cache only.

### Hosted set profiles and profile-only offline mode

Native and terminal set-profile loading is local-first and hosted by default. A
validated flat cache is used before any network refresh, so a warm valid cache
remains active while the production manifest is checked. The default manifest
for both live commands is:

```text
https://www.draftomen.com/profiles/manifest.json
```

Override it for a different HTTPS manifest:

```bash
draftomen --profile-manifest-url "$PROFILE_MANIFEST_URL"
draftomen-tui watch --profile-manifest-url "$PROFILE_MANIFEST_URL"
draftomen-tui watch --plain --profile-manifest-url "$PROFILE_MANIFEST_URL"
```

Use `--offline-profiles` for profile-only offline operation:

```bash
draftomen --offline-profiles
draftomen-tui watch --offline-profiles
draftomen-tui watch --plain --offline-profiles
```

This flag selects `ProfileNetworkPolicy.OFFLINE` for set profiles only and
takes precedence over profile networking. It does not disable Scryfall card
metadata, card images, or static card-data networking; those sources retain
their own cache and network policies.

When a hosted manifest or artifact is missing, unreachable, invalid, or weaker
than the cached profile, the last-good cache remains authoritative. If there
is no usable cache, deterministic fallback scoring remains active; live
sessions do not load ratings directly from 17Lands. The TUI `d` action and the
native ratings refresh control request the same forced hosted-profile refresh.
A newer validated profile reports an `updated` outcome and changes
recommendations in place; an equal result reports `unchanged` and leaves
ratings active. Failed, offline, or missing refreshes retain the last usable
cache when ratings exist; with no usable profile, deterministic fallback
scoring remains active. Repeated requests coalesce, and `watch --plain` has
no command UI.

Manual `refresh-profile` remains an explicit producer/client operation:

```bash
draftomen-tui refresh-profile --set-code SET --format FORMAT \
  --manifest-url "$PROFILE_MANIFEST_URL"
```

Draft Omen does not bundle a hosted manifest. Producer generation, website
publication, and Python/native release workflows remain independent of runtime
profile consumption. Native applications may still carry a validated baseline
profile resource for local fallback; hosted refresh never mutates that
resource.

For development-only semantic analysis, use the reproducible [card corpus
workflow](docs/corpus.md). It keeps pinned source bytes and locks outside
tracked data and emits deterministic artifacts for offline consumers.

Use the arrow keys or `j`/`k` to browse cards, `s` to change the ranking,
`b` to open the current build, `c` to configure the view and optional splash
recommendations, `d` to refresh the hosted profile in the TUI, and `q` to quit.

### Visual development

For deterministic visual development, launch the explicit forced-mock entry
point:

```bash
draftomen-gui-mockup
```

For an automated, non-interactive GUI smoke check, use the default command
with its mock provider and bounded smoke flag:

```bash
QT_QPA_PLATFORM=offscreen draftomen --provider mock --smoke-test
```

See the [desktop GUI guide](docs/gui-mockup.md) for live and mock launch
options, selectable states, and responsive review targets. For reproducible
unsigned development bundles, see the [desktop bundle guide](docs/desktop-bundles.md).

Live recommendations currently support Quick Draft. Windows support is best-effort.

## Local draft audit data

Live sessions started with `draftomen-tui`, including `watch --plain`, keep an
independent, append-only JSONL audit log for every draft under
`~/.draftomen/audit/drafts/<account-id>/<draft-id>.jsonl`. Each record includes
the offered pack, pool snapshot, ratings source, scoring configuration, complete
candidate calculations, splash eligibility and mana-source reasoning, all
supported rankings, the recommendation visible when the pick was made, and the
card actually chosen. Stable record IDs prevent
startup scans and log rotation from rewriting prior evidence.

Offline `replay` and backtest commands do not write audit records. For the schema
and retention details, see [draft audit logging](docs/draft-audit-log.md).

## Branding and compliance

Draft Omen is not affiliated with, sponsored by, approved by, or endorsed by Wizards of the Coast, Scryfall, or 17Lands.

Draft Omen is unofficial Fan Content permitted under the Fan Content Policy. Not approved/endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC. Card data from Scryfall and 17Lands; neither service endorses this tool.
