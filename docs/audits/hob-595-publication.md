# HOB recovered-profile publication and draft evidence

Issue: #595. Date: 2026-09-19. The recovery and every verification command ran with
`OPENROUTER_API_KEY` and `OPENROUTER_KEY` removed from the environment. No model provider was
constructed or contacted.

## Published artifact

The supported `republish-enrichment` command selected confirmed artifact
`edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84` from run
`9574d202eef14943`, used the local HOB Quick Draft ratings cache, and published an `early`, schema-3
profile. The manifest selects gzip object
`37a4d44603baa6786a75df99b25a4f07baaf3a76227f892891fe838278cbbc5f`; its uncompressed canonical
profile digest is `8274309189935d0739a58925b5a6eed0e4020dfaa5f48e13d170128fd21f6d90`.
The profile retains 708 relationships, 657 of which carry executable qualified projections, and
passes the runtime compatibility check against `website/public/card-data/hob.json.gz`.

The paid run matched all 1,638 lines in `hob-587-run-manifest.sha256` before and after publication.
The confirmed artifact, frozen card database, and frozen guide remained at their audited digests
`edc7d166...`, `70ebacdf...`, and `bd176ad3...`. The inventory and mechanic matrices remain the
source of truth for the complete 193-Oracle-identity / 198-play-booster-printing audit and for the
accepted recruit, amass, landfall, ferocious, adventure, storied, Hone/Equipment, Treasure/Dragon,
and graveyard-direction decisions.

## Real Draftmancer and QML evidence

`scripts/hob_published_profile_smoke.py` uses the pinned Draftmancer revision, the local Scryfall
bulk source, an offline application directory containing the exact profile bytes above, and the
production test-draft controller. It follows a predeclared rank-one selection policy for all three
14-pick packs, records every server-originated offer and pool, builds the completed pool, toggles
AI enhancement off and on against the same live offer, and renders the recovered recommendation
through `CardPreview.qml` before saving an offscreen screenshot under `/tmp`.

The checked-in `hob-595-draft-evidence.json` records 42 offers: 16 had one or more relationship
advice rows and 26 record that no offered recommendation had relationship support from the drafted
pool. The run observed conditional token go-wide, token sacrifice, and token death relationships.
At pack 1 pick 4, `Bolg of the North` and `Thorin's Last Stand` received relationship evidence from
the drafted `Fíli the Pathfinder`; disabling AI enhancement on that same offer removed the
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
smoke used the same prepared application directory and exact recovered profile bytes. Its Auto
journey completed 42 HOB picks and built a 40-card BR deck. Its Manual journey used the real QML
controls for five picks, including a rank-two selection, and observed the pool grow to five cards.
The helper verified that the external pinned Draftmancer server answered before and after both
journeys.

## Reproduction

Start the pinned Draftmancer server as documented in `README.md`, prepare an application directory
with `website/public/card-data/hob.json.gz` and the decompressed published object, then run:

```sh
QT_QPA_PLATFORM=offscreen env -u OPENROUTER_API_KEY -u OPENROUTER_KEY \
  uv run --no-sync python scripts/hob_published_profile_smoke.py \
  --draftmancer-dir ../Draftmancer \
  --scryfall-bulk-file .draftomen/corpus-cache/sources/scryfall-default-cards.jsonl.gz \
  --app-dir "$APP_DIR" \
  --screenshot /tmp/hob-595-published-profile.png \
  --report docs/audits/hob-595-draft-evidence.json
```

The committed evidence JSON has SHA-256
`6fae8b6d14705e78259dc6ccfb93273b2750c0918feb1092c459a33d378bce48`.

## Epic audit

#559 remains open. HOB publication and runtime display are now evidenced, while #578 still requires
the separate LCI regeneration. Closing #595 does not imply that the parent epic is complete.
