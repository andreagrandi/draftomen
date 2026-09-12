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

   Oracle evidence: `103382` Fíli the Pathfinder — `Storied (If you control three or more artifacts, legendaries, and/or Sagas, you have an enduring story for the rest of the game.)`

2. **Recruit** (required) — guide quote:

   > Recruit fills this role in white decks

   Oracle evidence: `103386` Lake-town Lookout — `When this creature dies, recruit. (Draw a card, then discard a card. If you discarded a nonland card, create a 1/1 white Human Soldier creature token.)`

3. **Adventures** (required) — guide quote:

   > The common adventure cycle is woefully unbalanced.

   Oracle evidence: `103397` An Unexpected Party // At the Door, face 1 (`At the Door`) — `Create X 2/2 red Dwarf creature tokens. (Then exile this card. You may cast the enchantment later from exile.)`

4. **Amass** (required) — guide quote:

   > get ready for a lot of amass wars

   Oracle evidence: `103494` Tidings of War — `Amass Goblins 1. If this spell was cast from a graveyard, amass Goblins 3 instead. (To amass Goblins X, put X +1/+1 counters on an Army you control. It's also a Goblin. If you don't control an Army, create a 0/0 black Goblin Army creature token first.)`

5. **Ferocious** (required) — guide quote:

   > ferocious is running, which makes cards like Duskwatch Hunter more useful than you’d expect.

   Oracle evidence: `103510` Nasty Little Rabbit — `Ferocious — At the beginning of combat on your turn, if you control a creature with power 4 or greater, put a +1/+1 counter on this creature.`

6. **Landfall** (required) — guide quote:

   > Landfall has kind of been a bust.

   Oracle evidence: `103500` Beorn's Hospitality — `Landfall — Whenever a land you control enters, put a +1/+1 counter on target creature you control.`

   The following sentence names the payoff cards; the shorter span above is the mechanic's own verdict.

7. **Hone Counters** (required) — guide quote:

   > and repeatedly buff several equipment.

   Oracle evidence: `103534` Dwalin, Weaponmaster — `Whenever Dwalin enters or attacks, put a hone counter on each Equipment you control. (Each hone counter on an Equipment grants +1/+0 to equipped creature.)`

   Every expectation quote is a short, distinctive span of the guide's own prose. Expectations are
   compared by containment against a run's evidence quotes, so a span the guide does not literally
   contain — or one longer than what the model copies — could never match, and the failure would be
   an artefact of the benchmark rather than of the pipeline. The heading alone (13 characters) is
   below the harness's 20-character containment floor, so the Hone Counters quote uses the section's
   prose.

   The Oracle evidence above is the second, independent way to match the same mechanic, so a guide
   extraction that never quotes the guide's one sentence for a mechanic cannot make the mechanic
   unmatchable on its own. A mechanic is matched by a retained guide claim whose evidence quotes the
   guide span above **or** by a retained card capability for that Oracle card and face whose evidence
   quotes the Oracle span above. `oracle_card_id`, `oracle_face_index` and `oracle_quote` in
   `benchmark.json` carry those reviewed spans; the harness validates each one as an exact substring
   of the frozen card artifact before it performs any paid work, and refuses a run whose benchmark
   quotes a span the frozen card does not literally contain.

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

### R4 — `token-sacrifice-outlet` (required)

- Source enabler: `103492` **Stone-Giant of High Pass** (face index None)
  - `Whenever this creature enters or attacks, create a 3/1 colorless Wall artifact creature token with defender named Stone Boulder.`
- Target payoff: `103491` **Snowslope Hunter** (face index None)
  - `Sacrifice another creature or artifact: Exile the top card of your library. You may play it until the end of your next turn. Activate only during your turn and only once each turn.`
- Guide sentence that motivates it:
  - `The good news though is a 6/6 + 4 mana later (or fodder for cards like Snowslope Hunter) puts in insane work.`

### R5 — `token-death-payoff` (required)

- Source enabler: `103531` **Chief Warg's Company** (face index None)
  - `At the beginning of your upkeep, create a 2/2 green Wolf creature token.`
- Target payoff: `103448` **Great Fierce Bee** (face index None)
  - `Whenever one or more other creatures die, scry 1. (Look at the top card of your library. You may put that card on the bottom.)`
- Guide sentence that motivates it:
  - `You can sometimes ambush “dies” stuff like Rhovanion Rampager with this and make your opponent cry.`

### R6 — `token-sacrifice-outlet` (required)

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

## Re-authoring record (2026-09-11)

Two expectations named a mechanism the role vocabulary cannot reach, and the paid run's retained evidence showed how to correct them. R4 (`103492` → `103491`) and R6 (`103531` → `103458`) were written as `fodder-sacrifice-outlet`, which requires the enabler to carry `sacrifice_fodder`. In the paid `medium` run that role is assigned to **zero of 444** capabilities, so no such pair is ever constructed and neither expectation could be met by any run; both enablers in fact carry `token_maker`, and both interactions are real (`103492` creates a sacrificeable 3/1 Wall artifact token, `103531` creates a 2/2 Wolf each upkeep, and `103458` and `103491` both consume creatures). The vocabulary expresses that interaction as `token-sacrifice-outlet` (`TOKEN_MAKER` → `SACRIFICE_OUTLET`), and the paid run already retained accepted relationships for exactly these participants:

- `relationship:token-sacrifice-outlet:103492:103492-token-maker-1:103491:103491-sacrifice-outlet-1` — accepted.
- `relationship:token-sacrifice-outlet:103531:103531-upkeep-wolf-token:103458:103458-sacrifice-outlet` — accepted.

Of the run's 285 `token-sacrifice-outlet` relationships, 211 were accepted. R3 keeps the same mechanism with different participants, so the required set now covers this interaction three times and covers two mechanisms in total.

A second re-authoring, made with the Oracle-evidence benchmark of the same date, moves R5 from `fodder-dies-payoff` onto `token-death-payoff`. #488 defined `sacrifice_fodder` to exclude a card that only creates creature tokens — such a card is a `token_maker` instead — so `fodder-dies-payoff` (`SACRIFICE_FODDER` → `DEATH_PAYOFF`) is unreachable for `103531`, whose relevant ability creates a Wolf token and nothing else. `token-death-payoff` (`TOKEN_MAKER` → `DEATH_PAYOFF`) is the declared rule for exactly that interaction, so R5's mechanism changed while its two participants (`103531` → `103448`), both Oracle quotes, its motivating guide sentence and its `required` flag stayed identical. A token producer feeding a die-trigger is the interaction the guide's `“dies” stuff` sentence describes, so the expectation now names the rule a run can actually reach.

The remaining required expectations stay required. They are unmet for two recorded reasons that are extraction or vocabulary limitations rather than expectation defects:

- R5 `token-death-payoff` (`103531` → `103448`) is now the declared rule for the interaction the guide motivates: `103531` **Chief Warg's Company** creates a 2/2 Wolf token every upkeep and `103448` **Great Fierce Bee** triggers whenever one or more other creatures die, which the accepted `TOKEN_MAKER` → `DEATH_PAYOFF` rule expresses. Its target additionally needs the trigger-before-effect fix verified in #485: a bounded paid probe over `103448` assigned `death_payoff` after that prompt guidance, where the earlier paid run assigned only `card_selection`.
- R1 and R2 `token-go-wide-payoff` are unmet because the extraction does not read static anthem and scaling text as a go-wide payoff. Measured: `103526` **Bard's Company** (`Other creatures you control get +1/+1. Whenever this creature enters or attacks, recruit.`) was tagged `loot` and `token_maker` at `medium`, and `103381` **Esgaroth Garrison** (`Esgaroth Garrison's power is equal to the number of creatures you control. When this creature enters, recruit.`) was tagged `loot` and `token_maker` at `medium` and `rummage` and `token_maker` at `high`, while `103382` **Fíli the Pathfinder** carries the equivalent anthem sentence and *is* tagged `go_wide_payoff` at both `medium` and `high`. The miss is therefore inconsistent role assignment around keyword lines such as `recruit`, not a reasoning-effort shortfall: no stored record for `103526` above `medium` exists, and `103381` misses the role at `high` too.

Every count above is derived from the run's own retained artifacts under `.draftomen/enrichment-runs/hob/work/`, so the record can be re-derived without a new paid request.

## Verification record (2026-09-12)

One complete live run under these expectations:

```
uv run python scripts/hob_enrichment_run.py --max-usd 2.00
```

Exit code **1**: an acceptance failure, not a crashed or incomplete run. Every number below is read from `.draftomen/enrichment-runs/hob/report.json`, which this run rewrote.

- Run id `enrichment-6c582412e48d4808aec71da9d9214b4c`, `mode=live`, `dry_run=false`, ceiling `2` USD, 201/201 eligible cards attempted (`limit_applied` false), 1060 executed and 4 reused work identities, `unknown_cost_responses` 0.
- Model `openai/gpt-5.6-luna`, `reasoning_effort` `medium`, `max_tokens` 128000.
- Work kinds, each pinned by prompt and response-schema identity: `draftomen-guide-extraction-v1` / `draftomen-guide-extraction-response-v1` (1 item, prompt sha256 `d1096035e1c0f582c230dab3289ca76ed83a835af66eb26e15c51a88a3608af8`); `draftomen-card-capability-extraction-v1` / `draftomen-card-capability-extraction-response-v1` (201 items, one prompt sha256 per card, first `0239f877a55ccfad6134f75c54a53c91d9707d54161a7e7401c553b2cc09c39a`); `draftomen-relationship-validation-v1` / `draftomen-relationship-validation-response-v1` (862 items, first prompt sha256 `000c8b295d36e565c19db32d3c880d3978f242ddf31db30468046c8ebef2bd01`).
- Accounting: input 1,085,193 tokens (227,457 cached), output 523,123, reasoning 279,975, `running_cost_usd` `0.81889859`, `run.spent_usd` `0.80056372`, work without a recorded cost 0 — inside the $2.00 ceiling.
- Retention: 21 guide claims, all uncertain and none rejected; 550 card capabilities with 125 rejected diagnostics and 0 accepted, because the extraction prompts reserve acceptance for review and the run's `review.state` is `pending`; 873 candidate pairs evaluated into 862 packages with no malformed extraction and no omission.
- Relationship verdicts: 584 accepted, 8 uncertain, 270 rejected.

Reviewed mechanics, as matched by the run:

| Mechanic | Matched | Matching rules | Evidence status |
|---|---|---|---|
| Storied | yes | `card-capability-quote`, `guide-quote-overlap` | uncertain |
| Recruit | yes | `card-capability-quote`, `guide-quote-overlap` | uncertain |
| Adventures | yes | `card-capability-quote` | uncertain |
| Amass | yes | `card-capability-quote` | uncertain |
| Ferocious | yes | `card-capability-quote`, `guide-quote-overlap` | uncertain |
| Landfall | yes | `card-capability-quote`, `guide-quote-overlap` | uncertain |
| Hone Counters | yes | `card-capability-quote` | uncertain |

All seven reviewed mechanics matched, and `Adventures`, `Hone Counters` and `Amass` match **only** through the card Oracle evidence added here — the guide extraction retained 21 claims and never quoted the guide sentence of those three, so before this re-authoring they were unmatchable by construction.

Reviewed relationships, as matched by the run:

| Expectation | Matched | Classification |
|---|---|---|
| R1 `token-go-wide-payoff:103382 → 103526` | yes | accepted relationship, both Oracle quotes re-checked |
| R2 `token-go-wide-payoff:103503 → 103381` | no | absent — no constructed candidate pair |
| R3 `token-sacrifice-outlet:103478 → 103550` | yes | accepted relationship, both Oracle quotes re-checked |
| R4 `token-sacrifice-outlet:103492 → 103491` | no | absent — no constructed candidate pair |
| R5 `token-death-payoff:103531 → 103448` | no | rejected verdict |
| R6 `token-sacrifice-outlet:103531 → 103458` | yes | accepted relationship, both Oracle quotes re-checked |
| R7 `loot-recursion-payoff:103563 → 103550` (optional) | no | rejected verdict |

The reviewed expectations were therefore **not** all matched: three required relationships are unmatched, so `acceptance.passed` is `false`. The failure detail is the run's own retained evidence, not a benchmark artefact:

- **R2** — `103503` **Dancing from Dark to Dawn** carries `counters`, `counters_theme` and `landfall_payoff` and **no** `token_maker`, so `token-go-wide-payoff` never pairs it with `103381` **Esgaroth Garrison**, which does carry `go_wide_payoff`. The card's Oracle text creates a 2/2 green Bear token on landfall, so the missing role is an extraction miss.
- **R4** — `103492` **Stone-Giant of High Pass** carries `attack_matters`, `damage_removal` and `sacrifice_outlet` and **no** `token_maker`, while `103491` **Snowslope Hunter** carries `sacrifice_outlet`. The card's Oracle text creates a 3/1 Wall artifact creature token, so this is the same missing-role miss as R2 on a token the prompt's artifact-token exclusion may have discouraged.
- **R5** — the pair was constructed (`103531` `token_maker` → `103448` `death_payoff`) and the relationship validator rejected it: *"The supplied text does not establish that the created token dies, so it does not support the declared token-death-payoff interaction."* The rejection is a verdict limitation, and the expectation is left exactly as reviewed rather than relaxed to force a pass.

These three failures describe the run above. The re-run recorded below resolves R2 and R4 through the token-maker rule and leaves R5 rejected.

`profiles.unchanged` is `true` (profile sha256 `35584d51fdd72b5a406382222efde6725422a84b279ec10a9c155139b2e0f0e8` before and after) and `review.state` is `pending`, so the run wrote no set profile and changed no reviewed artifact.

### Re-run after the token-maker rule (#494, #495)

A second complete live run over the same frozen sources, with the card-capability parse deriving `token_maker` for a quoted ability that creates a creature token even when that ability already carries its trigger-derived role:

```
uv run python scripts/hob_enrichment_run.py --max-usd 2.00
```

Exit code **1**: the run completes, and its only acceptance failure is R5. The run reused the stored responses of the run above for every identity the change does not alter — 119 paid completions and 1064 reused work identities, `run.spent_usd` `0.0534924` — so the numbers below are read from `.draftomen/enrichment-runs/hob/report.json`, which this run rewrote.

- Run id `enrichment-f643d9bbbf264af2971be0efc8180567`, `mode=live`, `dry_run=false`, ceiling `2` USD, 201/201 eligible cards attempted (`limit_applied` false), `unknown_cost_responses` 0.
- Retention: 21 guide claims; 555 retained card capabilities with 125 rejected diagnostics and 0 accepted, up from 550; `token_maker` is retained on 37 cards, up from 32; 993 candidate pairs evaluated into 981 packages with no malformed extraction and no omission, up from 873 into 862.
- Relationship verdicts: 673 accepted, 9 uncertain, 299 rejected.
- `profiles.unchanged` is `true` (profile sha256 `35584d51fdd72b5a406382222efde6725422a84b279ec10a9c155139b2e0f0e8` before and after) and `review.state` is `pending`.

| Expectation | Matched | Classification |
|---|---|---|
| R1 `token-go-wide-payoff:103382 → 103526` | yes | accepted relationship, both Oracle quotes re-checked |
| R2 `token-go-wide-payoff:103503 → 103381` | yes | accepted relationship, both Oracle quotes re-checked |
| R3 `token-sacrifice-outlet:103478 → 103550` | yes | accepted relationship, both Oracle quotes re-checked |
| R4 `token-sacrifice-outlet:103492 → 103491` | yes | accepted relationship, both Oracle quotes re-checked |
| R5 `token-death-payoff:103531 → 103448` | no | rejected verdict |
| R6 `token-sacrifice-outlet:103531 → 103458` | yes | accepted relationship, both Oracle quotes re-checked |
| R7 `loot-recursion-payoff:103563 → 103550` (optional) | no | rejected verdict |

R2 and R4 are now constructed and accepted, so the derived role resolves the two missing-role failures recorded above with the same reviewed expectations: R2's accepted evidence quotes `Landfall — Whenever a land you control enters, create a 2/2 green Bear creature token.` beside `Esgaroth Garrison's power is equal to the number of creatures you control.`, and R4's quotes `Whenever this creature enters or attacks, create a 3/1 colorless Wall artifact creature token with defender named Stone Boulder.` beside `Sacrifice another creature or artifact: Exile the top card of your library. You may play it until the end of your next turn. Activate only during your turn and only once each turn.`

`acceptance.passed` stays `false` on one remaining required expectation: **R5** `token-death-payoff:103531 → 103448`, which the validator still rejects — *"The supplied text does not establish that the created token dies, so it does not support the declared token-death-payoff interaction."* The expectation is left exactly as reviewed, the rejection is unchanged from the run above, and the verdict rule it depends on is tracked as #496.

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
- Matching a mechanic means the run retained a guide claim whose evidence quotes the guide span above, **or** a card capability for the expectation's `oracle_card_id` and `oracle_face_index` whose evidence quotes the `oracle_quote` span above, **or** an accepted or uncertain relationship carrying the reviewed mechanism as its name. Mechanic names compare casefolded, and quotation evidence matches by containment of at least 20 characters in either direction, so an extraction that quotes the whole ability line matches a shorter reviewed span.
- `guide.txt` is the page's readable text, not its markup: the raw page (`raw.html`) with script, style, noscript, svg, iframe, template and comment content dropped, entities decoded and tags stripped by `scripts/hob_guide_freeze.py`. `guide.sha256` and `guide.chars` pin that text, `guide.raw_source` records the preserved raw page, and every guide quote above is an exact substring of it, so an enrichment run can match guide evidence against frozen text instead of raw HTML.
- Every card cited here has card-level Oracle text, so every `*_face_index` is `null` except `103397` **An Unexpected Party // At the Door**, whose `Adventures` expectation pins face 1 (`At the Door`). No ineligible card (`103513 Ordinary Bear`) is cited anywhere.

## Expectations deliberately not required

- `mill-graveyard-payoff`: the set mills only through `103502 Cantankerous Keepers`, `103546 Silvan Rally` (face 1), `103422 Speak Secrets` (face 1) and `103556 Gleam of Death` (face 1), and no guide sentence states a mill payoff, so any threshold pairing would be speculative rather than supported.
- `fodder-dies-payoff` with `103514 Part in Friendship`: its trigger requires a *nontoken* creature, so token fodder pairs would be wrong even though the role pairing looks compatible.
- `fodder-dies-payoff` for `103531` **Chief Warg's Company** → `103448` **Great Fierce Bee**: #488 defines `sacrifice_fodder` as excluding a card that only creates creature tokens, so this pair can never be constructed and the same interaction is reviewed as R5 under `token-death-payoff` instead.
- `token-go-wide-payoff` with `103524 Bard, King of Dale`: its text doubles tokens that would be created instead of rewarding a wide board, so that card is not a go-wide payoff here.
- `discard-recursion-payoff` and `recursion-graveyard-payoff`: the set's recursion cards return themselves or a target creature card, and no guide sentence states a graveyard payoff, so requiring those pairs would test the model's imagination rather than the format.
- Additional `token-sacrifice-outlet` pairs beyond R3, R4 and R6: the run constructs 285 of them and accepted 211, so they are real interactions, but no further guide sentence singles out a specific token maker and sacrifice outlet for review.
- Any `Hone Counters` relationship: the guide names the mechanic but states no enabler to payoff pair for it, so it stays a required mechanic with no required relationship.
