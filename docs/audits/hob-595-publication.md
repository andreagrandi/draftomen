# HOB recovered-profile publication and draft evidence

Issue: #595. Date: 2026-09-19. The recovery and every verification command ran with
`OPENROUTER_API_KEY` and `OPENROUTER_KEY` removed from the environment. No model provider was
constructed or contacted.

## Candidate artifact

The supported `republish-enrichment` command selected confirmed artifact
`edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84` from run
`9574d202eef14943`, used the local HOB Quick Draft ratings cache, and generated an `early`, schema-3
profile with a current generation/publication timestamp. The checked-in manifest selects gzip object
`ad2c9840b189e709f7419a1b7db95c7cd96e1d4e4f3cade7ec56d5b8ea6ff5b4`; its uncompressed canonical
profile digest is `57d8abdb65f695f1caef65d14e8dca25e6012f179d20b41ff2442be0a220248a`.
The profile retains 708 relationships, 657 of which carry executable qualified projections, and
passes the runtime compatibility check against `website/public/card-data/hob.json.gz`.
Those projections cover 60 unique cards: 43 source cards and 25 payoff cards overlap by eight.
Five more cards occur only in unprojected relationships. Pairwise projections are no longer the
only runtime input: publication now compiles all 583 unambiguous Oracle-derived capability facts
into the role profile, producing 198 card entries and 769 assignments, including 278 assignments
with `semantic-enrichment` provenance. Runtime advice also consumes the existing condition map's
273 capabilities and 1,317 interactions. This covers ordinary card draw for second-card payoffs,
typed creature families, and the published Landfall, Ferocious, and Storied requirements without
making a new model call. Advice generation deliberately excludes broad package inferences for
go-wide, sacrifice, graveyard, artifact, enchantment, Equipment, and generic threshold roles.
Token-maker classification now requires creature-token creation, so Treasure creation such as
Dori's cannot support a creature-token claim.

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

The previous deployed production manifest had SHA-256 `9cf0fac2...` and selected the exact
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

The now-retired `scripts/hob_published_profile_smoke.py` used the pinned Draftmancer revision, the
local Scryfall bulk source, a preinstalled offline application directory containing the exact
candidate profile bytes above, and the production test-draft controller. It followed a predeclared
rank-one selection policy for all three 14-pick packs, recorded every server-originated offer and
pool, built the completed pool, toggled AI enhancement off and on against the same live offer, and
rendered the recovered recommendation through the complete `Main.qml` Live Draft surface before
saving an offscreen screenshot under `/tmp`. The smoke required a visible `AI-ENHANCED RELATIONSHIP
ADVICE` block, exact agreement with the recommendation data, and absence of the rejected confidence
and mana-production copy. This proved scoring, the AI off/on behavior, and rendering at the time; it
did not prove remote acquisition or deployment. The script is no longer runnable after retirement
of the shared-session relationship fields and QML advice block.

Live scoring treats either endpoint as the offered card when the other endpoint is already in the
pool. A post-fix ordinary draft produced advice in 25 of 42 offers, covering 45 offered rows and
108 relationship contributions. Of those contributions, 42 scored an offered source card against
a drafted payoff; the former one-way implementation could never expose those interactions. The
first reverse-direction example was `Goblin Plate Mail` offered at pack 2 pick 1 with drafted
`Esgaroth Garrison` as its go-wide payoff.

The checked-in `hob-595-draft-evidence.json` records a full 42-offer candidate run and explicitly
labels profile acquisition as `preinstalled-offline`. It uses the exact candidate digest above,
records every semantic or projected advice row, and verifies that disabling AI enhancement on the
same server offer removes the combined advice. Re-enabling it restores the advice, and the QML
advice block read back exactly matches the Python recommendation text.

The real Draftmancer run exposed Oracle-derived condition advice on the actual surface, including
Storied advice that named the drafted qualifying permanent and the offered payoff. A prior run of
the same candidate capabilities also showed `Bilbo, Luckwearer // Burglar's Plot` as a draw source
for `Lakeshore Apothecary`'s second-card payoff and showed `Bombur, Gentle Dreamer` receiving
Storied advice. Focused scoring tests cover the other direction, the AI-off gate, and the exact
Bilbo/Bombur families. Self-pairs are rejected, and semantic advice is capped at three concise
items per card.

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

The final strict-advice full-app run completed all 42 picks. At pack 1 pick 2 it visibly showed
`Óin the Brave` with drafted `Gollum, Silent Slinker // Meager Meal` named as a permanent that
counts toward Óin's three-artifact, legendary-permanent, or Saga Storied requirement. It also
rendered the explicit no-match state. The positive and no-match screenshot SHA-256 digests were
`8481956dd0f87b70c48962e6a0497bbf3d4a6e7e6cc1f732a8740088babd64f2` and
`99a2df870a6111a56c0769d8424c3a149ea76c98ab4a57b4c0012df754ab25f6`.

## Evidence artifact hashes

The candidate evidence JSON has SHA-256
`3b11d4a2497d282dfa20c35cdc1bf9a7c7f397cb1049e22a60f85b920c70bf8a`. The clean
production-cache run is recorded separately; its evidence file has SHA-256
`c2b3d758b956f419350481e1693137fee4867f74ea6b2bf9cacbbb18e9883204`.

## Epic audit

#595 remains open while its advice quality is evaluated and refined. #559 also remains open because
#578 still requires the separate LCI regeneration.
