# HOB recovered-profile publication and draft evidence

Issue: #595. Date: 2026-09-19. The recovery and every verification command ran with
`OPENROUTER_API_KEY` and `OPENROUTER_KEY` removed from the environment. No model provider was
constructed or contacted.

## Published artifact

The supported `republish-enrichment` command selected confirmed artifact
`edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84` from run
`9574d202eef14943`, used the local HOB Quick Draft ratings cache, and generated an `early`, schema-3
profile with a current generation/publication timestamp. The production manifest selects gzip object
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

The previous production manifest selected gzip object `75132bf4...` with profile digest
`c73802bf...`. That profile reproduced the GUI's `invalid or incompatible` state. The first
recovery attempt incorrectly retained the 2026-09-14 review timestamp, which was older than both
the production manifest and its selected HOB profile; normal clients would therefore reject it as
stale. `republish-enrichment` now defaults new recovered profiles to the current UTC timestamp.

After deployment, the live production manifest had SHA-256 `9cf0fac2...` and selected the exact
gzip object `ba47f76e...`; downloading that object from its production URL reproduced the same gzip
digest. A clean application directory refreshed through the default production manifest with
outcome `updated` and cached canonical profile SHA-256 `6b188ae9...`.

## Production acquisition and visible advice

The production-acquired cache completed a real 42-pick headless Draftmancer draft and built its
deck. The trace displayed AI-enhanced drafted-card relationship advice at pack 1 picks 6 and 7 and
pack 3 pick 2. A separate full 42-pick production-cache run recorded 18 offers with relationship
advice, removed contributions when AI enhancement was disabled on the same offer, restored them
when re-enabled, and rendered the full explanation for `Stir Up Trouble` through `CardPreview.qml`.

The native app then started a manual HOB Mocked Draft with contextual scoring enabled. Its real
footer displayed `Contextual · semantic + QuickDraft evidence`, `Relationship advice active for
HOB`, and `Profile ratings are ready for HOB`; the focused recommendation applied contextual
support from the drafted pool.

## Cached-profile Draftmancer and QML evidence

`scripts/hob_published_profile_smoke.py` uses the pinned Draftmancer revision, the local Scryfall
bulk source, a preinstalled offline application directory containing the exact candidate profile
bytes above, and the
production test-draft controller. It follows a predeclared rank-one selection policy for all three
14-pick packs, records every server-originated offer and pool, builds the completed pool, toggles
AI enhancement off and on against the same live offer, and renders the recovered recommendation
through the complete `Main.qml` Live Draft surface before saving an offscreen screenshot under
`/tmp`. The smoke requires a visible `AI-ENHANCED RELATIONSHIP ADVICE` block, exact agreement with
the recommendation data, and absence of the rejected confidence and mana-production copy. This
proves scoring, the AI off/on behavior, and rendering. It does not prove remote acquisition or
deployment.

The checked-in `hob-595-draft-evidence.json` records a full 42-offer candidate run and explicitly
labels profile acquisition as `preinstalled-offline`. At pack 1 pick 2, `Rhovanion Rampager`
received relationship evidence; disabling AI enhancement on that same offer removed the
relationship contributions. Re-enabling it restored the advice, and the QML advice block read back
exactly matched the Python recommendation text.

Two additional ordinary rank-one drafts completed the full 42-pick and build lifecycle through the
headless `draftomen-tui test-draft` command. The trace exposed recovered advice during one run at
pack 2 pick 5 for `Stir Up Trouble` and pack 2 pick 11 for `Gollum the Abandoned`, including the
drafted source cards, conditional qualifications, and effective DO-score contributions. These
ordinary drafts are stochastic; absence of a mechanic family from an offer is a coverage miss, not
evidence that the family is unsupported. Deterministic family coverage remains in the accepted
mechanic matrix and focused compiler, pool-ledger, scoring, session, and QML tests.

## Native verification

A fresh unsigned macOS bundle was built from this checkout with Nuitka 4.1.3. After production
acquisition, the compiled-bundle smoke used the exact cached profile. Its Auto
journey completed 42 HOB picks and built a 40-card BR deck. Its Manual journey used the real QML
controls for five picks, including a rank-two selection, and observed the pool grow to five cards.
The helper verified that the external pinned Draftmancer server answered before and after both
journeys. A subsequent native manual draft confirmed contextual evidence and relationship advice
were active together on the real surface.

The final full-app QML run used production profile SHA-256
`6b188ae9a248905bd5ad678f79008252ffa1889f52da9eb3b3012649d9419341`. At pack 1 pick 5,
`Great Fierce Bee` visibly showed the separate relationship-advice heading and its conditional
interaction with drafted `Bothersome Noisemaker`; disabling AI enhancement removed that same-offer
relationship. The `/tmp` screenshot SHA-256 was
`165a196b3daed7e86e0deda6597f982a23b10267d4334f3889cd5e76ca48bfe0`.

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
`25757e520b83ef320329351ae73f6364239ba45f2a69a253407bffd54267b6f0`. The clean production-cache
run is recorded separately; its evidence file has SHA-256
`c2b3d758b956f419350481e1693137fee4867f74ea6b2bf9cacbbb18e9883204`.

## Epic audit

#595 is ready to close after this final evidence update is committed and merged. #559 remains open
because #578 still requires the separate LCI regeneration.
