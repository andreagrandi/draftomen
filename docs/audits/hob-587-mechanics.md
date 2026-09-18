# HOB #587 — mechanics and relationship coverage audit

Scope: the frozen run `9574d202eef14943` only. No production edits, no provider calls, no guide
refresh, no source rewriting. The 202 cards, 583 oracle facts and 685 relationships are the
population under audit, not targets. Companion reports: `hob-587-inventory`, `hob-587-trace`.

Owned artifacts: `docs/audits/hob-587-relationship-ledger.json` (machine finding ledger, all 685 saved
relationships), `docs/audits/hob-587-mechanic-matrix.json` (per-mechanic matrix, bounded contract,
downstream gaps), `docs/audits/hob-587-mechanics.md` (this report).

| frozen input | digest / identity |
| --- | --- |
| artifact `edc7d166…be84.json` | sha256 `edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84`, review `confirmed` by `draftomen-tui` 2026-09-14T18:34:09Z |
| `sources/card-database.json` | sha256 `70ebacdfa4bd8e45f485a6bf7392daf1f754414e0716c6587fcc9e8dc419b9b9`, 202 cards |
| `sources/guide.json` (guide the artifact cites) | sha256 `537de10a…`, `hob-draftsim-guide`, `/mtg-hob-limited-set-review/` |
| set source | `draftomen-card-data-v1-hob`, sha256 `082a17e2…` |
| run provenance | provider `draftomen`, model `local-pair-matcher-v1`, `cost_usd: 0` |

## 1. Method
1. Read the frozen artifact directly; nothing was re-generated.
2. Ledger rows = one per saved relationship, grouped by the artifact's own `mechanism` key, resolving
   through a capability catalog (card id, name, face index, role, conditions, mechanics, evidence
   quote index, source/target row counts).
3. Each row carries a semantic class plus a separate compiler-gap code, so a gap is never reported as a
   rejected relationship and no class is inferred from compiler rejection.
4. All 685 rows were then screened against the **full oracle text** of both cards for every
   mechanically falsifiable requirement (typal Elf/Bear/chosen type, Goblin-only, creature versus
   artifact, nontoken-only, creature-death), and the 27 `recursion-graveyard-payoff` rows were
   additionally screened for graveyard **zone direction** (does the cited clause add cards to the
   graveyard, or remove them?). Screen limits are stated in section 8.

## 2. Census
Cards / oracle facts / guide claims: **202 / 583 / 2**. Confirmed relationships / rejected findings:
**685 / 467**. Rows the frozen compiler projects: **3**. Capabilities in the ledger (roled, conditioned,
evidenced): **80**; evidence quotes (all verbatim in the frozen card database): **73**.
Rows per mechanism: `token-go-wide-payoff` 333, `token-sacrifice-outlet` 269, `token-death-payoff` 37,
`recursion-graveyard-payoff` 27, `mill-graveyard-payoff` 9, `fodder-sacrifice-outlet` 8,
`fodder-dies-payoff` 2.

## 3. Classification with source evidence
| class | count | basis |
| --- | --- | --- |
| `uc` useful conditional | 606 | recorded conditions hold |
| `uac` useful conditional, alternate contribution | 17 | The Misty Mountains Cold (13) + graveyard clauses that also fill the graveyard (4) |
| `ucc` useful conditional, resolved from Oracle | 18 | Azog, Moria's Ruin |
| `mrd` record defect, role direction | 20 | Bard, King of Dale |
| `mxg` / `mxe` / `mxd` contradictions | 5 / 3 / 16 | Bolg's Company / Thranduil / graveyard-zone removals + Supper for Spiders |

Compiler gaps (not classes): `sp:fu` 558, `sp:nadj` 53, `sp:af` 36, `sp:opn` 20, `sp:oos` 11, `tp:ou` 4,
projected `-` 3.

- `uac` (17): 13 rows are The Misty Mountains Cold — its Treasure tokens are artifacts, but chapters I–IV
  each "create a 6/6 red Dragon creature token with flying" when you control four or more Treasures and
  sacrifice the Saga, so creature-count, pump, death and creature-only-sacrifice uses follow from the
  first chapter whose count reaches four (never when the check fails); 4 rows are graveyard rows whose
  cited clause removes a card from the graveyard while another clause of the same card adds one
  (103390 The Mountain-king's Return chapter I; 103543 Silvan Reveler). Useful conditional.
- `ucc` (18): Azog, Moria's Ruin reads "When Azog enters, destroy up to one other target creature. Its
  controller amasses Goblins X, where X is that creature's power. If you controlled that creature, draw
  a card." The pairing is useful only when Azog destroys a creature you control; the saved quote carried
  only the amass reminder, and the fact is resolved from Oracle text, not missing.
- `mrd` (20): Bard, King of Dale reads "If one or more tokens would be created under your control, twice
  that many of those tokens are created instead." It creates no token itself, yet the saved rows put it
  on the provider side: a role-direction defect in the payoff model (Bard as modifier of another
  source's tokens), not an Oracle gap.
- `mxg` (5) / `mxe` (3) / `mxd` (16): Bolg's Company sacrifices *another Goblin* and no paired source
  produces a Goblin anywhere in its full text (Troop of Ponies, Lake-town Lookout's Human Soldier, Head
  of the Hunt's Wolf, Down in the Valley's Elf, Bard's Company's Human Soldier); Thranduil's "Other
  Elves you control get +1/+1" cannot apply to the source tokens and he has no conversion clause. The
  16 `mxd` rows are the zone-direction cases: 15 are `recursion-graveyard-payoff` rows whose cited
  clause *removes* a card from the graveyard (e.g. 103454) and therefore consumes the very seven-card
  threshold the row was projected against, and 1 is Supper for Spiders, whose "target opponent" return
  clause is recorded against the same kind of payoff but loads the opponent's graveyard.
- Full-text screen result: four candidate missed contradictions (non-Bear token makers vs Beorn the
  Fierce's "Other Bears you control get +2/+2") all resolved in favour of the saved class, because
  Beorn's combat trigger makes a target creature a Bear "in addition to its other types". Nothing in
  the saved data records amass itself as a contradiction. The zone screen moved 16 rows to `mxd`, 4 to
  `uac` and left 7 `recursion` rows `uc`.

## 4. Per-family accounting
| family | rows | claim |
| --- | --- | --- |
| `token-go-wide-payoff` | 333 | token sources (amass-tagged 117, recruit-tagged 89, landfall-tagged 29, adventure-tagged 23, equipment-tagged 18, mill/flashback-tagged 17, Treasure-tagged 9, storied-tagged 7, plain rooms) vs anthems, creature-count, typal |
| `token-sacrifice-outlet` | 269 | outlets differ by what they accept (another creature; artifact or creature; another Goblin; self) and by cost (mana, tap, once per turn, sorcery speed); the class data locates 24 contradictions here (16 `mxd`, 5 `mxg`, 3 `mxe`) |
| `token-death-payoff` | 37 | needs a *creature* token that dies; every recorded source is one, and The Misty Mountains Cold appears only through its Dragon token |
| `recursion-graveyard-payoff` | 27 | payoff 103422 needs seven or more cards in your graveyard, so self-mill and flashback are the enabling half. All 27 rows adjudicated for zone direction: 16 `mxd` (the cited clause removes a card from the graveyard), 4 `uac` (another clause of the same card adds one), 7 `uc` |
| `mill-graveyard-payoff` | 9 | contains 2 of the 3 rows the compiler projects (the third is the recursion row 103442 → 103422) |
| `fodder-sacrifice-outlet` | 8 | Troop of Ponies is the only recorded sacrifice-fodder capability |
| `fodder-dies-payoff` | 2 | Great Fierce Bee ("one or more other creatures die") and Part in Friendship ("nontoken creature you control dies") |

The nontoken distinction holds in the data: Great Fierce Bee is the target of all 37
`token-death-payoff` rows; Part in Friendship (nontoken-only) is the target of none.

## 5. Bounded contract
A pair is evaluable only if it states provided capability, payoff requirement, costs, optionality,
quantities, alternative modes, timing and party/typal scope. Frozen evidence supports: **recruit**
("draw a card, then discard a card. If you discarded a nonland card, create a 1/1 white Human Soldier
creature token" — the draw and the discard are mandatory when the ability resolves, the token is
conditional on the card actually discarded being a nonland; an empty hand does not exclude the token
because the draw happens first, and neither does a starting hand of lands, since the drawn card decides
the discard; the parent ability decides whether recruit happens at all, and no recruit card
prints an optional parent; 13 capabilities); **amass** ("N +1/+1 counters on an Army you control; it's
also a Goblin; if you don't control an Army, create a 0/0 black Goblin Army token first" — 18
capabilities, N = 1/2/3/4 or X; repeat amass grows one creature, so it is not a go-wide engine, and a
creature-only outlet always accepts it); **landfall** (one trigger per land entry; payloads differ per
card — Bear/Elf tokens, +1/+1 counters or pump, tap/untap, base power/toughness, and Silvan Reveler's
{1}{G}{U} graveyard return; plus a saga granting the clause; 7 capabilities on 4 cards, no rate model
and none required by #587); **ferocious** (six cards, each with its own payoff, all gated on controlling
a creature with power 4 or greater; 1 recorded capability — the Wilderland Scrounger go-wide counters;
Armies or the 6/6 Dragon could satisfy the gate, nothing links them — consumer opportunity);
**adventures** (second face cast separately; 13 capabilities on 6 adventure-layout cards, `face_index`
part of identity); **storied** ("If you control three or more artifacts, legendaries, and/or Sagas, you
have an enduring story for the rest of the game." — nine cards print the keyword, 2 ledger capabilities
carry the tag, and the attained story persists even if the qualifying permanents are lost); **hone /
equipment** (hone: the two cards' hone facts are retained, only the dedicated representation is missing,
section 8; equipment: 5 capabilities of the set's 22 Equipment cards, including Goblin Plate Mail "amass
Goblins 1, then attach this Equipment to the amassed Army", Orcrist's per-Equipment cost reduction plus
Treasure per creature of a chosen type, and Sting's hone counters); **Treasure** (artifact tokens;
creature-only outlets stay separate from artifact-accepting ones, and The Misty Mountains Cold's Dragon
is the creature half of that card).

## 6. Guide comparison (frozen vs supplied example, no substitution)
| | frozen guide (evidence for these rows) | supplied example guide |
| --- | --- | --- |
| id / url | `hob-draftsim-guide`, `/mtg-hob-limited-set-review/` | `draftsim-hob-draft-guide`, `/mtg-hob-draft-guide/` |
| sha256 | `537de10a…` | `21f5d19e…` (= `.draftomen/enrichment-runs/hob/guide.txt`) |
| mentions: recruit / amass / landfall / ferocious / adventure / storied / hone / equipment / Treasure / tribal | 24 / 17 / 17 / 12 / 25 / 14 / 5 (46 raw) / 25 / 21 / 0 | 7 / 10 / 8 / 5 / 3 / 8 / 7 / 8 / 6 / 0 |

The frozen guide is the deeper document on adventures (25 mentions vs 3) and carries Set Mechanics
sections for Adventures, Amass, Recruit and Storied plus its own archetype and card-note discussion; its
hone coverage is a single review sentence, not a mechanic section — the figure `5 (46 raw)` counts
word-level hone matches against raw substring matches, 41 of which sit inside "phone"/"iPhone". Identity
caveat: the reader-mode fetch Main read as `artifact://10` is not proven byte-identical to `guide.txt`;
the supplement's identity rests on the benchmark metadata (`draftsim-hob-draft-guide`, sha `21f5d19e…`).
The benchmark uses the example guide and the website export (`website/public/card-data/hob.json.gz`,
202 cards, 201 eligible, ineligible `[103513]`); it is not the source of the 685 rows.

## 7. Army growth vs repeated go-wide
First amass with no Army creates a real body; every later amass adds counters to that same creature. A
6-power Army is one attacker, one sacrifice body, one death trigger — not six bodies. All 5 `mxg` rows
are Bolg's Company ("{T}, Sacrifice another Goblin: Add {B}{R}") paired with sources producing no
Goblin at all; amass itself is never contradicted. Goblin-town Flunkies creates the Army and Goblin
Plate Mail both amasses and attaches to it; The Misty Mountains Cold's Dragon token *is* a creature, but
it arrives only when the saga's own check succeeds — chapters I–IV each "create a 6/6 red Dragon creature
token with flying" if you control four or more Treasures and sacrifice the Saga — so the creature payoffs
follow from the first chapter whose count reaches four and never when the check fails.

## 8. Representation gap scope, unresolved work, blockers
Representation gap (bounded, no implementation here): hone is **not** missing extraction. The frozen
card database (sha `70ebacdf…`) prints hone on 2 cards, and the frozen artifact retains 9 hone oracle
facts for them — Dwalin, Weaponmaster 103534 (`103534-attack-matters-1`, `103534-counters-1`,
`103534-counters-theme-1`, `103534-equipment-payoff-1`, quoting "put a hone counter on each Equipment you
control. (Each hone counter on an Equipment grants +1/+0 to equipped creature.)") and Sting, Bilbo's Sword
103562 (`103562-enter-counters`, `103562-enter-counters-theme`, `103562-equip-ability`,
`103562-equipment-payoff`, `103562-hone-counter-static`). What is missing is the dedicated
representation: no hone role or mechanic exists in the frozen capability catalog, so the counters are
recorded only as counters/counters_theme/equipment_payoff, and neither card has any saved relationship.
The bounded fix is a representation extension plus relationship recovery from those frozen sources — no
re-extraction, no new run, no paid call.
Unresolved matrix work: (1) 20 `mrd` rows — correct the payoff model so Bard's doubling is a modifier of
another source's token creation (no new evidence needed); (2) the hone representation above; (3) 606 `uc`
rows — the screen covers requirement classes and graveyard zone direction only, so treat them as
conditionally useful, never as validated synergies; (4) compiler coverage stays 3 of 685 projected rows,
recorded for #589.
Blockers: the example guide is a different document (section 6); no rate or availability model is
required by #587; the 3 live rows are what the frozen compiler emits, not a verified subset; every family
contract in section 5 is bound to a frozen quote rather than to a keyword name or to compiler
acceptance.

## 9. Reproducible verification for Main
```bash
cd /Users/andrea/Projects/draftomen
python3 - <<'PY'
import json,hashlib
run="/Users/andrea/.draftomen/set-enrichment/hob-quickdraft/enrichment-runs/hob/9574d202eef14943"
L=json.load(open("docs/audits/hob-587-relationship-ledger.json")); A=json.load(open(run+"/artifacts/edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84.json"))
caps=L["capabilities"]
got=[L["finding_id"]["prefix"]+m+":"+str(caps[r[0]]["card_id"])+":"+caps[r[0]]["id"]+":"+str(caps[r[1]]["card_id"])+":"+caps[r[1]]["id"] for m,v in L["findings"].items() for r in v]
ids=[r["finding_id"] for r in A["relationships"]]
print(len(caps),len(L["quote_registry"]),sum(len(v) for v in L["findings"].values()))  # 80 73 685
print(set(got)==set(ids), len(got)==len(set(got)))                                     # True True
print(hashlib.sha256("\n".join(got).encode()).hexdigest())          # b0bfff83… order
print(hashlib.sha256("\n".join(sorted(got)).encode()).hexdigest())  # 50268b6f… set
PY
```
Expected `80 73 685`, then `True True`, then the ledger's `finding_id.order_sha256` (`b0bfff83…`) and
`finding_id.set_sha256` (`50268b6f…`); both matched at audit time, as did the artifact's
`confirmed_relationship_ids: 685`. Class census after the corrections: `uc` 606, `uac` 17, `ucc` 18,
`mrd` 20, `mxg` 5, `mxe` 3, `mxd` 16, summing to 685. Guide sentences must be read from the frozen
`sources/guide.json`.

## 10. Downstream gaps
**#588** ids survive: run, artifact digest, card id/face and quotes are preserved for re-keying.
**#589** 682 of 685 rows never project; `sp:fu` 558 shows the grammar must accept keyword reminder text
and conditional frames. **#590** recruit's draw and discard are mandatory while its token is
discard-conditional (on a nonland), and the parent ability decides whether recruit happens at all; amass
re-uses or creates exactly one Army. **#591** landfall, ferocious and storied are recorded as printed
(storied at the three-permanent threshold with a persistent attained state); rate modelling is not
required. **#592** adventure faces must bind as a separate cast (`face_index` preserved). **#593** the
pool population is the full 202 cards / 583 facts, not the 80 participants; the 24 contradictions are
hard negatives, the 17 `uac` rows stay conditional, and the 20 `mrd` rows need the corrected model.
**#594** render class and gap separately; `mrd` is a record defect, not a synergy; 3 live rows. **#595**
the two digests prove accounting (every saved id, in order and as a set) — not semantic completeness,
bounded by 606 + 17 + 18 + 20 + 24 and the 3-of-685 projection. **#597** hone representation: the two
hone cards' facts are retained but the catalog has no hone role/mechanic and neither card has a saved
relationship, so hone-counter text has no recorded home or item links.

## 11. Explicit non-claims
Rows were classified from the artifact's own recorded facts, conditions and quotes plus the frozen
guide, then screened against full oracle text for the requirement classes and, for the 27 recursion
rows, for graveyard zone direction. `uc`, `uac` and `ucc` rows are conditionally useful: they hold only
while their recorded conditions (timing, optionality, typal, controller scope, the four-or-more-Treasure
Dragon check, the three-permanent enduring-story threshold) are met. No claim is made about the example
guide as evidence, and none that the benchmark's 201-eligible population equals the frozen 202-card
population.
