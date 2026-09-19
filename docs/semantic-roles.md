# Semantic roles

`draftomen.semantic_roles` is the reusable semantic boundary for Limited card
roles. It consumes a `CardInfo`, `CardFace`, or one normalized mapping emitted by
`draftomen.corpus.normalize_card`; it does not alter card metadata and does not
score picks or decks.

## Classify cards

```python
from draftomen.semantic_roles import RoleClassifier

classifier = RoleClassifier()
result = classifier.classify(normalized_card)
for assignment in result.assignments:
    print(assignment.role.value, assignment.confidence, assignment.evidence)

if result.unknown_reports:
    for report in result.unknown_reports:
        print(report.card_key, report.mechanic, report.reason)
```

The classifier is deterministic. Roles are stable-deduplicated and sorted by
role name; reports and diagnostics have stable ordering. `result.to_bytes()` is a
canonical JSON representation, including `classifier_version`,
`role_schema_version`, per-assignment provenance/confidence, and whole-result
provenance. Repeated classification of the same normalized mapping therefore
produces byte-identical output. Canonical `keywords` are read from both the card
and each face: known values participate in inference, while an unsupported value
produces an actionable unknown report. Canonical `power` and `toughness` values
are textual; only a plain integer power is used for numeric roles, while `*`,
`X`, and compound values remain nonnumeric.

A card with unsafe or incomplete source metadata, an unsupported layout, malformed
canonical fields, or an explicit unknown mechanic receives no inferred roles and
an actionable `UnknownMechanicReport`. Faces must be objects with correctly typed
fields. Face text is classified independently before assignments are unioned, so
conditions on one face cannot alter another. Oracle prose is not scanned for
arbitrary capitalized words, so an ordinary word cannot become a mechanic.

The role vocabulary covers interaction (including typed effective-removal
characteristics), card advantage/selection, creature and typal identity,
tokens, sacrifice/death, graveyard, permanent types/equipment/counters,
friendly untap support with typed target restrictions, token-creation replacement effects with
their separate-source dependency, land/mana (including typed produced resources), and numeric
power/permanent and other state thresholds. One card can have any number of assignments.

Gift is retained as an optional promise to an opponent with a typed gift object and the specific
effect branch that the promise qualifies. Opponent benefits and unrelated optional text do not
become Gift roles without the complete printed instruction.

Token-replacement relationships point from a distinct, explicit token-creation instruction to the
replacement payoff. The replacement card never supplies a token itself, and Army growth or an
opponent-controlled creation branch does not become an independent source.

Hone sources retain whether they affect each Equipment or only the source itself, their printed
entry or attack timing, and their stated quantity basis. Hone Equipment payoffs retain the exact
per-counter +1/+0 benefit, while ordinary counters and non-Equipment reminder text remain outside
the typed relationship.

## Reviewed overrides

Exceptional corrections are data, not card-name conditionals. A reviewed
`OverrideSet` is keyed only by a stable `oracle_id`, numeric `arena_id`/`grp_id`,
or exact set and collector identity. Display-name keys (`name:...`) are rejected
and are never used for override lookup:

```json
{
  "schema_version": 1,
  "overrides": [
    {
      "key": "oracle_id:example",
      "add": [{"role": "card_selection", "confidence": 0.95}],
      "remove": [],
      "rationale": "Reviewed correction and reason."
    }
  ]
}
```

Load it with `load_role_overrides()` and pass it to `classify_card()` or
`RoleClassifier`. The bundled set is `BUNDLED_REVIEWED_OVERRIDES`; override
entries are validated, sorted by key, and never silently combined when malformed.
An override can resolve an explicit unsupported mechanic report, but cannot
override malformed, unsafe, incomplete, or unknown-source metadata. Applied
assignments retain `reviewed_override` provenance. Add a correction only when a
reusable generic rule cannot represent an exception.

## Compiled set profiles and precedence

A profile contains compiled assignments for one exact set and records all three
versions: `profile_schema_version`, `classifier_version`, and
`role_schema_version`. Build one from deterministic local results:

```python
from draftomen.semantic_roles import compile_role_profile, dump_role_profile

profile = compile_role_profile(set_code="hbl", results=classifier.classify_many(rows))
dump_role_profile(profile, ".draftomen/roles/HBL/profile.json")
```

Profile keys prefer identities shared by normalized corpus rows and `CardInfo`:
numeric Arena/group identity first, then exact set-plus-collector identity, then
an Oracle identity. A display name is only a last-resort local classification
key and cannot identify a reviewed override. Thus a profile compiled from a
normalized row can authoritatively resolve its equivalent `CardInfo`.

`load_role_profile()` rejects malformed JSON and unsupported profile schemas.
`dump_role_profile()` writes through a flushed, fsynced sibling temporary file
and atomically replaces the destination, preserving an existing artifact if
writing fails. `RoleClassifier.resolve(card, profile=profile)` (or
`resolve_card_roles`) uses this precedence:

1. an exact-set profile with compatible classifier and role-schema versions is
   authoritative for the card assignment;
2. a missing profile, wrong set, missing card entry, or incompatible version falls
   back wholly to the local classifier plus bundled reviewed overrides;
3. assignments from incompatible sources are never merged.
When a novel mechanic maps to an existing role, add only a conservative metadata
pattern and, if needed, a recognized explicit value in `SUPPORTED_MECHANICS`.
When it introduces a genuinely reusable concept that the vocabulary cannot
express:

1. add a typed `Role` value and parameter type (immutable, JSON validated);
2. add the generic metadata classifier mapping in `_infer_assignments`;
3. add deterministic fixture assertions under `tests/fixtures/semantic-roles.json`
   and `tests/test_semantic_roles.py`;
4. increment `ROLE_SCHEMA_VERSION` when the serialized role contract changes,
   and increment `CLASSIFIER_VERSION` when classification semantics change;
5. rebuild affected profiles offline with `rebuild_role_profile`, which uses
   `load_normalized_rows`, `RoleClassifier.classify_many()`, and
   `dump_role_profile()`:

   ```bash
   python -c 'from draftomen.semantic_roles import rebuild_role_profile; rebuild_role_profile(normalized_path=".draftomen/corpus-artifacts/normalized.jsonl", set_code="hbl", output_path=".draftomen/roles/HBL/profile.json")'
   ```

   The helper filters to the requested set and emits stable JSON; it never
   touches the live card database.
6. inspect unknown reports and keep the generated profile's version fields
   compatible before publishing it.

Unknown or unsafe results are omitted from compiled profiles rather than written
as empty authoritative entries. Rebuilds coalesce duplicate stable identities
only when their assignments are equal and fail clearly on conflicts. Raw corpus
acquisition remains the responsibility of `draftomen.corpus`; a rebuild consumes
its normalized JSONL output and does not modify the live card database.
