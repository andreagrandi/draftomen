# HOB enrichment benchmark (frozen)

Reviewed expectations for a later paid Draft Omen enrichment run over *The Hobbit* (set code `hob`). Every card id, Oracle quote and guide quote below is copied verbatim from the frozen sources in this directory; nothing here was guessed.

## Frozen sources

- Card source: `website/public/card-data/hob.json.gz` (sha256 `8831da97958741163a6d4b93d5068c28a44d8146ed476c9a76618cc91d0ae7b7`), 202 canonical cards.
- Normalized set identity `set_source_sha256`: `082a17e20d0406350d0c1e04f3a7d4b04454132eece0c87ac929ad13653f279a`.
- Guide: `draftsim-hob-draft-guide` <https://draftsim.com/mtg-hob-draft-guide/> retrieved `2026-09-11T12:16:46.844329+00:00` UTC. The guide text is `guide.txt`: 36256 characters, sha256 `21f5d19ee59790fdfe9b1f33c98840b18da0db5ea6d7e4d07a089c234a37ddae`.
- The guide text is the page's readable text, derived deterministically from the byte-exact fetched page `raw.html` (sha256 `fb1c113ece0e044ff5ae6083061d7b8b90fb5e92762e1db35a80ccbf02a89cef`, 1761983 characters, same URL, same retrieval time) by `scripts/hob_guide_freeze.py`, which drops script, style, noscript, svg, iframe, template and comment content entirely, decodes HTML entities and strips remaining tags. Every guide quote below is an exact span of that text and contains no markup, script or CSS.
- Eligibility: a card is eligible when the card or one of its faces has nonblank Oracle text.

## Card eligibility

- Eligible canonical cards: **201**.
- Ineligible canonical cards: **1** — `103513` Ordinary Bear (no Oracle text, therefore never a valid enrichment subject).

## Required mechanics

The guide's *Mechanics Revisited* section is the authority for these names.

1. **Storied** (required) — guide quote:

   > get you storied since so many of them are legendary.

2. **Recruit** (required) — guide quote:

   > Recruit fills this role in white decks

3. **Adventures** (required) — guide quote:

   > The common adventure cycle is woefully unbalanced.

4. **Amass** (required) — guide quote:

   > get ready for a lot of amass wars

5. **Ferocious** (required) — guide quote:

   > ferocious is running, which makes cards like Duskwatch Hunter more useful than you’d expect.

6. **Landfall** (required) — guide quote:

   > Landfall has kind of been a bust.

   The following sentence names the payoff cards; the shorter span above is the mechanic's own verdict.

7. **Hone Counters** (required) — guide quote:

   > and repeatedly buff several equipment.

   Every expectation quote is a short, distinctive span of the guide's own prose. Expectations are
   compared by containment against a run's evidence quotes, so a span the guide does not literally
   contain — or one longer than what the model copies — could never match, and the failure would be
   an artefact of the benchmark rather than of the pipeline. The heading alone (13 characters) is
   below the harness's 20-character containment floor, so the Hone Counters quote uses the section's
   prose.

## Relationships

Each expectation names a `ROLE_COMPATIBILITY_RULES` mechanism, the two participants, the exact Oracle line that proves each side of the interaction and the guide sentence that motivates expecting it. `source_face_index` / `target_face_index` are `null` when the quote comes from card-level Oracle text.

### R1 — `token-go-wide-payoff` (required)

- Source enabler: `103382` **Fíli the Pathfinder** (face index None)
  - `Whenever Fíli or another nontoken Dwarf you control enters, create a 2/2 red Dwarf creature token.`
- Target payoff: `103526` **Bard's Company** (face index None)
  - `Other creatures you control get +1/+1.`
- Guide sentence that motivates it:
  - `However, white has the best rares of any color by a substantial degree. An Unexpected Party, Fíli, Kíli, The Eagles Are Coming!, The Queen of Dale, Bard's Company, Dáin's Company… need I go on?`

### R2 — `token-go-wide-payoff` (required)

- Source enabler: `103503` **Dancing from Dark to Dawn** (face index None)
  - `Landfall — Whenever a land you control enters, create a 2/2 green Bear creature token.`
- Target payoff: `103381` **Esgaroth Garrison** (face index None)
  - `Esgaroth Garrison's power is equal to the number of creatures you control.`
- Guide sentence that motivates it:
  - `Landfall has kind of been a bust. There’s no common reason to care about this mechanic at all, so if you’re doing anything spicy with landfall, it’ll be with one of these cards:`

### R3 — `token-sacrifice-outlet` (required)

- Source enabler: `103478` **Goblin-town Flunkies** (face index None)
  - `When this creature enters, amass Goblins 1. (Put a +1/+1 counter on an Army you control. It's also a Goblin. If you don't control an Army, create a 0/0 black Goblin Army creature token first.)`
- Target payoff: `103550` **Tom, Bert, and William** (face index None)
  - `{1}, Sacrifice another creature: Draw cards equal to the sacrificed creature's power, then discard a card.`
- Guide sentence that motivates it:
  - `One underrated aspect of this mechanic is using 1/1 Goblin Army tokens as sacrifice fodder, which will come up often with Tidings of War.`

### R4 — `fodder-sacrifice-outlet` (required)

- Source enabler: `103492` **Stone-Giant of High Pass** (face index None)
  - `Whenever this creature enters or attacks, create a 3/1 colorless Wall artifact creature token with defender named Stone Boulder.`
- Target payoff: `103491` **Snowslope Hunter** (face index None)
  - `Sacrifice another creature or artifact: Exile the top card of your library. You may play it until the end of your next turn. Activate only during your turn and only once each turn.`
- Guide sentence that motivates it:
  - `The good news though is a 6/6 + 4 mana later (or fodder for cards like Snowslope Hunter) puts in insane work.`

### R5 — `fodder-dies-payoff` (required)

- Source enabler: `103531` **Chief Warg's Company** (face index None)
  - `At the beginning of your upkeep, create a 2/2 green Wolf creature token.`
- Target payoff: `103448` **Great Fierce Bee** (face index None)
  - `Whenever one or more other creatures die, scry 1. (Look at the top card of your library. You may put that card on the bottom.)`
- Guide sentence that motivates it:
  - `You can sometimes ambush “dies” stuff like Rhovanion Rampager with this and make your opponent cry.`

### R6 — `fodder-sacrifice-outlet` (required)

- Source enabler: `103531` **Chief Warg's Company** (face index None)
  - `At the beginning of your upkeep, create a 2/2 green Wolf creature token.`
- Target payoff: `103458` **Rhovanion Rampager** (face index None)
  - `Whenever this creature attacks, you may sacrifice another creature. If you do, put a number of +1/+1 counters on this creature equal to the sacrificed creature's power.`
- Guide sentence that motivates it:
  - `Sacrificing to Rhovanion Rampager is okay with amass or “dies” stuff, but otherwise I tend to just trade it off asap for guaranteed value.`

### R7 — `loot-recursion-payoff` (optional)

- Source enabler: `103563` **Thrór's Map** (face index None)
  - `{2}, {T}: Draw a card, then discard a card.`
- Target payoff: `103550` **Tom, Bert, and William** (face index None)
  - `When Tom, Bert, and William die, if they were a creature, return them to the battlefield. They're an artifact. (They're no longer a creature.)`
- Guide sentence that motivates it:
  - `Best in decks with really good rare creatures to get back, or recruit to set it up consistently.`

## Reproducing the frozen guide

`guide.txt` is deliberately not versioned, so a reviewer regenerates it from the pinned page:

1. Fetch <https://draftsim.com/mtg-hob-draft-guide/> byte-exactly into
   `.draftomen/enrichment-runs/hob/raw.html`; expect 1761983 characters and sha256
   `fb1c113ece0e044ff5ae6083061d7b8b90fb5e92762e1db35a80ccbf02a89cef`.
2. Run `uv run python scripts/hob_guide_freeze.py`, which derives `guide.txt` and prints its
   length and sha256: expect 36256 characters and sha256
   `21f5d19ee59790fdfe9b1f33c98840b18da0db5ea6d7e4d07a089c234a37ddae`.

The harness refuses a run whose guide text does not match that pin (`guide.sha256` or
`guide.chars`), so a drifted page fails loudly instead of silently shifting every quoted span.

## How to read these expectations

- `required: true` means an accepted enrichment run must match the expectation: an unmatched required expectation is an acceptance failure, not a completed run (issue #472 AC4). `required: false` entries are checked but reported as optional.
- Matching a relationship means the run produced an accepted relationship whose mechanism and two participants are the ones below and whose Oracle evidence covers both participants; the quote text above is the strongest exact evidence available in the frozen card data.
- `guide.txt` is the page's readable text, not its markup: the raw page (`raw.html`) with script, style, noscript, svg, iframe, template and comment content dropped, entities decoded and tags stripped by `scripts/hob_guide_freeze.py`. `guide.sha256` and `guide.chars` pin that text, `guide.raw_source` records the preserved raw page, and every guide quote above is an exact substring of it, so an enrichment run can match guide evidence against frozen text instead of raw HTML.
- Every card cited here has card-level Oracle text, so all `*_face_index` values are `null`. No ineligible card (`103513 Ordinary Bear`) is cited anywhere.

## Expectations deliberately not required

- `mill-graveyard-payoff`: the set mills only through `103502 Cantankerous Keepers`, `103546 Silvan Rally` (face 1), `103422 Speak Secrets` (face 1) and `103556 Gleam of Death` (face 1), and no guide sentence states a mill payoff, so any threshold pairing would be speculative rather than supported.
- `fodder-dies-payoff` with `103514 Part in Friendship`: its trigger requires a *nontoken* creature, so token fodder pairs would be wrong even though the role pairing looks compatible.
- `token-go-wide-payoff` with `103524 Bard, King of Dale`: its text doubles tokens that would be created instead of rewarding a wide board, so that card is not a go-wide payoff here.
- `discard-recursion-payoff`, `recursion-graveyard-payoff` and `token-sacrifice-outlet` beyond `103550 Tom, Bert, and William` / `103458 Rhovanion Rampager`: the set's recursion cards return themselves or a target creature card, and no guide sentence states a graveyard payoff, so requiring those pairs would test the model's imagination rather than the format.
- Any `Hone Counters` relationship: the guide names the mechanic but states no enabler to payoff pair for it, so it stays a required mechanic with no required relationship.
