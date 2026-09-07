# Draft audit logging

Draft Omen writes an independent audit trail while processing live Quick Drafts.
The log is designed for later algorithm investigations without recalculating old
decisions using newer ratings or configuration.

## Location and format

Each draft has one append-only JSON Lines file:

```text
~/.draftomen/audit/drafts/<account-id>/<draft-id>.jsonl
```

An explicit `--app-dir` replaces `~/.draftomen` in that path. Every line is a
complete JSON object with `schema_version`, `record_id`, `record_type`,
`recorded_at`, application version, account, draft, event, and set identifiers.
The writer appends a complete encoded line and flushes it to durable storage
before returning.

Records are never updated in place. Their stable IDs make repeated Arena log
events idempotent within one process and allow downstream analysis to de-duplicate
records safely if two Draft Omen processes watch the same account concurrently.
A malformed existing audit file fails loudly instead of accepting new records
after corrupted evidence.

## Record types

`draft_started`

- Course identity and original draft start timestamp.

`decision_evaluated`

- Zero-based pack and pick coordinates plus the absolute pick index.
- Complete offered pack and pool before the pick.
- Pick-engine configuration, optional-feature state, and application version.
- Ratings dataset formats, fetch timestamps, and aggregate pair records.
- Score-normalization bounds and inferred color commitment.
- Every candidate's card metadata, resolved rating, sample counts, fallback
  source, base score, color adjustment, pair tiebreaker, final score, and rank.
- The active splash color, picked splash-card count, fixing sources, mana-source
  target, grade and score gates, classification, and exact decision reasons.
- Exact card order for DO Score, 17Lands win rate, ALSA, and mana-value views.

### Rationale and published recommendation fields

Each `decision_evaluated.recommendation` object and every object in
`decision_evaluated.candidates` carries three additive fields at that object's
top level:

- `rationale`: the immutable `PickRationale.to_json()` shape:
  `{"reasons": [...], "unattributed_contribution": number}`. Each reason has
  `kind`, `contribution`, `phrase`, and `evidence`; tuple evidence is encoded
  as a JSON list. The allowed reason kinds and materiality rules are documented
  in [pick-scoring.md](pick-scoring.md).
- `concise_explanation`: the short drafter-facing output of
  `render_pick_rationale_concise`.
- `explanation`: the detailed output of
  `render_pick_rationale_detailed`, including retained evidence and score
  accounting when applicable.

The fields are projected from the same scored card as the candidate's existing
rating, color, contextual, splash, and score fields. They are additive to audit
schema version 1; they do not replace the existing `contextual_breakdown` or
`contextual_evidence` fields.

`evaluation_id` and the evaluation `record_id` remain stable when these fields
are added or changed: evaluation identity deliberately excludes `rationale`,
`concise_explanation`, and `explanation` from both the recommendation and
candidate payloads. Historical schema-version-1 records may omit the new
fields, and the loader continues to read them without inventing replacements;
restarting or rescanning such a record remains idempotent.

Offline replay does not append audit records, but its `Recommendation:` line
uses `render_pick_rationale_detailed`, so every nonempty offered pack includes
the detailed rationale for its recommendation.


A pending pick may have more than one evaluation when ratings finish loading or
the scoring inputs genuinely change. Each distinct evaluation gets its own
`evaluation_id`. Once its choice is recorded, startup rescans cannot add newer
evaluations to that historical pick.

`choice_made`

- The Arena card actually chosen.
- The TUI ranking mode visible at the time, or DO Score in plain watch mode.
- The recommendation at the top of that ranking.
- Whether the user followed the recommendation.
- The `decision_id` and latest `evaluation_id` available at choice time.

A choice remains useful even if Draft Omen started after the offered pack and
therefore has no evaluation to link. In that case, the evaluation and
recommendation fields are `null`.

`draft_completed`

- Final pool, card count, completion coordinates, and whether completion was
  explicit or inferred from the final Arena payload.

## Scope

The live Textual interface and `watch --plain` write audit data. Offline `replay`,
deck building, benchmarking, and backtesting do not, because recomputed
recommendations are not evidence of what was shown during the original draft.

Audit logs remain local. Draft Omen does not upload them, automatically delete
them, or combine them with mutable resumable state under `state/`.
