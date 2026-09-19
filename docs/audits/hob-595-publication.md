# HOB recovered-profile candidate and draft evidence

Issue: #595. Date: 2026-09-19. The recovery and every verification command ran with
`OPENROUTER_API_KEY` and `OPENROUTER_KEY` removed from the environment. No model provider was
constructed or contacted.

## Candidate artifact

The supported `republish-enrichment` command selected confirmed artifact
`edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84` from run
`9574d202eef14943`, used the local HOB Quick Draft ratings cache, and generated an `early`, schema-3
profile with a current generation/publication timestamp. The candidate manifest selects gzip object
`ba47f76ea308c8278bfdb9176165a220833ab6ca051f509f23a7c9e82966db4c`; its uncompressed canonical
profile digest is `6b188ae9a248905bd5ad678f79008252ffa1889f52da9eb3b3012649d9419341`.
The profile retains 708 relationships, 657 of which carry executable qualified projections, and
passes the runtime compatibility check against `website/public/card-data/hob.json.gz`.

The paid run matched all 1,638 lines in `hob-587-run-manifest.sha256` before and after publication.
The confirmed artifact, frozen card database, and frozen guide remained at their audited digests
`edc7d166...`, `70ebacdf...`, and `bd176ad3...`. The inventory and mechanic matrices remain the
source of truth for the complete 193-Oracle-identity / 198-play-booster-printing audit and for the
accepted recruit, amass, landfall, ferocious, adventure, storied, Hone/Equipment, Treasure/Dragon,
and graveyard-direction decisions.

The production manifest observed on 2026-09-19 still selected gzip object `75132bf4...` with profile
digest `c73802bf...`. That profile reproduced the GUI's `invalid or incompatible` state. The first
recovery attempt incorrectly retained the 2026-09-14 review timestamp, which was older than both
the production manifest and its selected HOB profile; normal clients would therefore reject it as
stale. `republish-enrichment` now defaults new recovered profiles to the current UTC timestamp.

The candidate is only in the local commit. Until it is deployed and fetched by a clean application
installation, supported publication and production availability remain unverified and #595 stays
open.

## Cached-profile Draftmancer and QML evidence

`scripts/hob_published_profile_smoke.py` uses the pinned Draftmancer revision, the local Scryfall
bulk source, a preinstalled offline application directory containing the exact candidate profile
bytes above, and the
production test-draft controller. It follows a predeclared rank-one selection policy for all three
14-pick packs, records every server-originated offer and pool, builds the completed pool, toggles
AI enhancement off and on against the same live offer, and renders the recovered recommendation
through `CardPreview.qml` before saving an offscreen screenshot under `/tmp`. This proves scoring,
the AI off/on behavior, and rendering. It does not prove remote acquisition or deployment.

The checked-in `hob-595-draft-evidence.json` records a full 42-offer candidate run and explicitly
labels profile acquisition as `preinstalled-offline`. At pack 1 pick 2, `Rhovanion Rampager`
received relationship evidence; disabling AI enhancement on that same offer removed the
relationship contributions. Re-enabling it restored the advice, and the QML explanation read back
exactly matched the Python recommendation text.

Two additional ordinary rank-one drafts completed the full 42-pick and build lifecycle through the
headless `draftomen-tui test-draft` command. The trace exposed recovered advice during one run at
pack 2 pick 5 for `Stir Up Trouble` and pack 2 pick 11 for `Gollum the Abandoned`, including the
drafted source cards, conditional qualifications, and effective DO-score contributions. These
ordinary drafts are stochastic; absence of a mechanic family from an offer is a coverage miss, not
evidence that the family is unsupported. Deterministic family coverage remains in the accepted
mechanic matrix and focused compiler, pool-ledger, scoring, session, and QML tests.

## Native verification

A fresh unsigned macOS bundle was built from this checkout with Nuitka 4.1.3. The compiled-bundle
smoke used a prepared application directory. Its Auto
journey completed 42 HOB picks and built a 40-card BR deck. Its Manual journey used the real QML
controls for five picks, including a rank-two selection, and observed the pool grow to five cards.
The helper verified that the external pinned Draftmancer server answered before and after both
journeys. Because its profile was preinstalled, this is native runtime evidence rather than a
served-byte installation test.

## Reproduction

Start the pinned Draftmancer server as documented in `README.md`, prepare an application directory
with `website/public/card-data/hob.json.gz` and the decompressed candidate object, then run:

```sh
QT_QPA_PLATFORM=offscreen env -u OPENROUTER_API_KEY -u OPENROUTER_KEY \
  uv run --no-sync python scripts/hob_published_profile_smoke.py \
  --draftmancer-dir ../Draftmancer \
  --scryfall-bulk-file .draftomen/corpus-cache/sources/scryfall-default-cards.jsonl.gz \
  --app-dir "$APP_DIR" \
  --screenshot /tmp/hob-595-published-profile.png \
  --report docs/audits/hob-595-draft-evidence.json
```

The candidate evidence JSON has SHA-256
`25757e520b83ef320329351ae73f6364239ba45f2a69a253407bffd54267b6f0`. A clean installation must
fetch the deployed manifest and object, record the same profile digest, show AI-enhanced suggestions
as available, and render relationship advice before #595 can close.

## Epic audit

#595 and #559 remain open. Candidate runtime display is evidenced, while supported deployment and
clean-install acquisition remain outstanding for #595 and #578 still requires the separate LCI
regeneration.
