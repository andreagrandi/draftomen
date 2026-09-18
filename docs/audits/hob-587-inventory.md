# HOB issue 587 inventory and capability audit (AC1, AC2)

Read-only audit of the paid HOB run `9574d202eef14943`. No provider calls, no
source rewrites, no changes to paid inputs. The ledgers hold the data and the
exact join keys: `docs/audits/hob-587-intended-inventory.json` (AC1) and
`docs/audits/hob-587-capability-ledger.json` (AC2). This file is the narrative.

## Pins

Confirmed artifact `edc7d1666105…` (pre-confirmation `bb00b761b8c1…`), frozen card
database `70ebacdfa4bd…`, frozen guide `bd176ad3d366…` / `537de10ab837…`. Three
distinct digests are pinned for the set input: transport (sha256 of the published
card export gzip `8831da979587…`, three identical local copies), payload (sha256
of its decompressed bytes `c6a8ac73a31f…`), and semantic (`082a17e20d04…`, the
artifact's `set_source_sha256`). The semantic digest is not a byte hash:
`set_source_sha256` in `draftomen/semantic_enrichment.py` hashes the normalized
card projection (set code plus each card's `card_id`, `oracle_id`, set code,
collector number, name, layout, `type_line`, `oracle_text` and faces), so it
matches neither of those two byte digests nor the file digest of
`sources/card-database.json` (`70ebacdfa4bd…`). TraceAudit's rebuilt loader
confirms the match through `card_database_digest_matches_audit` in
`docs/audits/hob-587-trace-out-r2.json`.
MTGJSON HOB `294b1c9e67c6…` (2,633,908 bytes) and HOC
`3bbc711b8de6…` (669,559 bytes) are retained for offline checks at
`.draftomen/corpus-cache/sources/mtgjson/HOB.json` and `HOC.json`. Full digests
are under `source_pins` in the inventory ledger.

## AC1: intended draftable selection

1. **Scryfall bulk** `default-cards-20260828090525` (`ac45637c8d95…`, independent
   authority): 321 HOB printings; the 202 carrying `arena_id` equal the frozen
   202 exactly on arena id, name, oracle id, collector number, keywords, rarity,
   layout, with 0 missing and 0 extra.
2. **MTGJSON HOB play booster** (`294b1c9e67c6…`, independent compiled product
   data, not manufacturer primary): sheets common 62, uncommon 65, rareMythic
   106, wildcard 233, foil 233, nonFoilLand 15, foilLand 15; 248 union printings,
   carrying 198 distinct Arena identities. Those 198 are 180 non-land cards, 8
   nonbasic lands (103565 Elven Passage, 103566 Elvenking's Halls, 103567
   Goblin-town, 103568 Hobbit Hole, 103569 Iron Hills, 103570 Lake-town, 103571
   The Lonely Mountain, 103572 Mirkwood) and 10 basic-land printings
   (103573–103582), split 75 common, 55 uncommon, 53 rare, 15 mythic.
3. **17Lands HOB**, four formats (digests in the ledger): 188 cards per format,
   union 188, equal to the 180 non-land cards plus the 8 nonbasic lands, so the
   observed pool is the booster pool minus its basic-land printings.
4. **Published app export** `website/public/card-data/hob.json.gz`
   (`8831da979587…`): 202 cards in the frozen order. Pipeline output, not an
   independent inventory; it only shows input and publication agree.

Selection is source-grounded, not inferred from `arena_id` presence or rating
coverage. 198 of the 202 Arena identities are obtainable in play boosters, and
the remaining four are the Plains printing identities 107385–107388, which appear
in no play booster; 180 plus 8 gives 188; the 10 booster basics plus those 4
Plains give the 14 ids 17Lands never saw.

Oracle-identity accounting: the frozen 202 arena ids carry 193 distinct oracle
ids and every one of them appears in the play sheets, because the basic-land and
Plains printings share oracle ids with booster printings. The 141 non-Arena
printings in the MTGJSON set (119 in Scryfall terms) map to 78 distinct oracle
ids, all present in the play sheets. Variant printings therefore resolve to
booster-obtainable identities instead of being dropped by arena-id filtering.

Exclusions, each with a reason:

- **119 non-Arena printings** (collector 199–312 and 317–321; the four Plains
  identities at 313–316 are Arena printings, not variants), enumerated in the
  ledger with arena twin, collector number, promo types, and games. All 119
  share an `oracle_id` with an Arena printing (78 distinct twins), so no
  paper-only identity exists and no draftable identity is stranded outside the
  frozen set.
- **hoc, The Hobbit Eternal**: 158 cards, 104 Arena identities, 0 shared with the
  frozen 202, and no booster configuration at all, so it is not a draft product.
  **thob**: 15 token layouts, 0 Arena ids. A further 36 syntax sets contain
  "hobbit" in their names and are unrelated printings.

202 + 119 = 321, so every Scryfall HOB printing is either one of the 202 arena
identities (198 obtainable in play boosters, the four Plains identities excluded
because no HOB product enables drafting them) or one of the 119 enumerated
non-Arena treatments. The frozen input omits nothing.

**Historical 202 vs 201.** `benchmark.json` records 202 cards and 201 eligible
ones; the exclusion rule is `_card_has_source_text` in
`scripts/hob_enrichment_run.py`, and the excluded card is 103513 Ordinary Bear
(null Oracle text, no request in either pass). The audit does not accept that
exclusion as a property of the draft pool: the card is pinned in the artifact and
carries an explicit cause in the ledger.

## AC2: capability evidence inventory

The ledger lists all 583 retained facts grouped by producing run (194 runs, each
serving one card), every row carrying local id, face index, role, and a 16-hex
sha256 of the stored evidence array. The join is exact: 194 run keys, 583 rows,
per-run tuple multisets equal to the artifact row for row. 583 facts = 577 direct
model capabilities + 6 compiler-derived `token_maker` facts listed by full
finding id.

This is an **evidence inventory, not a semantic validation**, and it does not
close AC2 semantically. Three cards carry explicit unsupported dispositions
where the stored role conflicts with the frozen Oracle text:

- 103508 Little Bear: "untap another target creature you control" is stored as
  `disabling_removal`, which the effect is not.
- 103562 Sting, Bilbo's Sword: hone counters are retained as facts under generic
  roles (`counters`, `equipment_payoff`, including a `hone-counter-static` fact),
  yet hone has no role in the closed vocabulary. 103534 Dwalin, Weaponmaster
  keeps its printed hone-counter ability the same way (`counters`,
  `counters_theme`, `equipment_payoff`, `attack_matters`). Hone is therefore a
  representation and relationship gap, not a missing extraction: there is no hone
  representation, and neither card appears in the 685 confirmed relationships.
- 103372 Bilbo's Gambit: the gift mechanic (guide seed and Oracle text) has no
  role at all, and the card also carries no facts.

Downstream contracts must re-verify facts rather than inherit model acceptance.

Candidate accounting: 402 card-capability responses (201 cards across two
contract passes) produced 1369 candidates, where 1369 = 710 from contract
version 1 plus 659 from version 2, and 659 = 577 retained as facts + 61 recorded
as oracle rejections + 21 with no record. The 514 candidates from the superseded
pass are expected debris, not drops.

Rejections, 467 total: 376 relationship (structured capability parameters
conflict), 82 oracle, 9 guide. Oracle reasons: 40 quote not an exact substring,
21 contract mismatch, 6 card-name mismatch, 15 model-declared role-scope
rejections. A rejection reason records the compiler's decision, never a verdict
on the card, so each classification cites the stored reason text.

Cards without facts (8), with cause, candidates, and recorded rejections: 103368
Long-Bodied Grey Dog (role scope, 3/1), 103372 Bilbo's Gambit (role scope, 4/3),
103402 Bilbo, Luckwearer (card-name binding, 13/6), 103471 Dwarven Mauler (no
record, 2/0), 103506 Gigantic Big Bear (quote substring, 4/2), 103513 Ordinary
Bear (no source text, 0/0), 103538 The Great Goblin (mixed, 9/3), 103567
Goblin-town (contract mismatch, 8/4). These 43 candidates produced 19 rejections;
24 records are missing. No card is classified not applicable, and the unknowns
are stated as unknown rather than assumed benign.

Coverage: 194 cards carry facts, 63 cards appear in the 685 confirmed
relationships (token-go-wide-payoff 333, token-sacrifice-outlet 269,
token-death-payoff 37, recursion-graveyard-payoff 27, mill-graveyard-payoff 9,
fodder-sacrifice-outlet 8, fodder-dies-payoff 2), 131 cards carry facts with no
relationship, and the same 8 cards have neither.

## Source differences (separate from classification)

Scryfall and the publication export share 202 identities; divergence is only
normalization, 18 multi-face Oracle text joins and 22 empty-string mana costs
stored as null in the frozen export. 17Lands tracks no basic lands, which fully
explains its 14 missing ids. MTGJSON lists 360 HOB entries against Scryfall's 321
printings; the surplus entries add no arena identities, since all 141 of its
non-Arena printings map to the same 78 oracle ids as Scryfall's 119 non-Arena
printings. The
Arena client snapshot (2025.53.0.5) predates HOB and has no HOB rows, so it
cannot corroborate Arena DraftContent or booster contents. 13 stored
`work/results` entries are stale relative to the current parser (they report
`malformed` while re-parsing succeeds); no ledger claim depends on them.

## Reproduce (offline, seconds)

Six checks sit in `verification_commands` of the capability ledger: exact-tuple
join against `oracle_facts` (194 runs, 583 rows); artifact counts 202 / 583 / 685
/ 685 / 467; rejection reasons 40/21/6/15 plus 376 relationship and 9 guide;
relationship mechanisms and the 63-card participant union; raw accounting 1369 =
710 + 659 and 659 = 577 + 61 + 21; inventory sums (facts 583, relationship column
1370 over 63 cards, 202 arena ids, 119 variant rows). Booster checks read the
retained MTGJSON files under `.draftomen/corpus-cache/sources/mtgjson/`.

The AC1 composition check is the first entry in `verification_commands` of
`docs/audits/hob-587-intended-inventory.json`. Run exactly that command from the
repository root; it reads only the retained
`.draftomen/corpus-cache/sources/mtgjson/HOB.json`, deduplicates the play-sheet
UUID union before looking up card dictionaries, and prints the sheet sizes
followed by `248 198 193 18 10 8 180 [103565, 103566, 103567, 103568, 103569,
103570, 103571, 103572]` — sheet-union printings, Arena identities, distinct
oracle ids, land identities, basic-land identities, nonbasic-land identities,
non-land identities, and the eight nonbasic-land identities. The second command
reads the pinned MTGJSON file and the paid run's frozen card database at
`~/.draftomen/set-enrichment/hob-quickdraft/enrichment-runs/hob/9574d202eef14943/sources/card-database.json`
(digest `70ebacdfa4bd…`); the third reads only this inventory ledger. No
uncommitted probe and no paid call is involved.

## Open items: work that a bounded new child of #559 must carry

- **Candidate adjudication records** (extraction and reconciliation scope): 21 v2
  candidates and 24 candidates across passes have no retention or rejection
  record, bounded by the eight card ids above. This is not #588 record work and
  not shared compiler work; it needs its own child of #559.
- **Role vocabulary and representation gaps**: hone (103534 and 103562, whose
  hone-counter abilities are retained only as generic facts and which carry no
  hone relationship), gift (103372), and the untap effect recorded as removal
  (103508) have no correct closed role. #589 and #590 do not own this, since #590
  covers recruit and amass only; a bounded new child of #559 must decide the
  vocabulary and the hone representation before those cards can be classified
  correctly.
- **Textless cards**: 103513 Ordinary Bear needs a decided contract before any
  coverage statement includes it.
- Nothing in this audit asserts that the 583 retained facts are semantically
  correct; AC2 establishes identity, binding, and accounting, and names the
  known contradictions rather than clearing them.
