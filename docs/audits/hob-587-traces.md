# HOB 587 traces and run fingerprints

Scope: AC5 (trace from saved evidence through generated representation, pool support and rendered advice for representative failures) and AC6 (original artifact, source and response digests, no provider calls, no changes to paid inputs).

Owner: TraceAudit. Evidence files: `docs/audits/hob-587-run-fingerprint.json`, `docs/audits/hob-587-trace-cases.json`, `docs/audits/hob-587-run-manifest.sha256`, `docs/audits/hob-587-trace-out.json` (durable copy of the final run, sha256 `74244c03...`), `docs/audits/hob-587-trace-out-r2.json` (earlier successful run, sha256 `61bc5f54...`). Probe: `docs/audits/hob_587_trace_probe.py` — the capture used the checked-in revision 3, sha256 `24ac41a8...`, content-identical to the executed copy at `/tmp/hob_587_trace_probe.py` (sha256 `79bb6834...`) except for the portable default paths noted under Reproduction; the current checkout carries revision 4 for #589 (see the Reproduction note).

Status: the final run observed all four stages with zero blockers, `enhancement_origin: artifact_compile`, zero network-denial blocks and unchanged fingerprints. The rendered evidence is a component-level offscreen render of `CardPreview.qml` fed the published `Recommendation` payload; the full application window, live session and user journey were not launched, so nothing here speaks to them. No production code was changed.

## Frozen inputs

The confirmed artifact named by issue #587 is `artifacts/edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84.json` in the paid run `9574d202eef14943`; its file name equals its SHA-256. The run also holds `artifacts/bb00b761...json`, the same content before confirmation (`review.state=pending`).

| Input | Bytes | SHA-256 |
| --- | --- | --- |
| artifact edc7d166...json | 1723163 | `edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84` |
| artifact bb00b761...json (pending review) | 1641435 | `bb00b761b8c1489bb560c6ea233c7d5eb70bb3c6bd52ef484313f12dc0957cae` |
| sources/card-database.json | 261017 | `70ebacdfa4bd8e45f485a6bf7392daf1f754414e0716c6587fcc9e8dc419b9b9` |
| sources/guide.json | 1667858 | `bd176ad3d3666822b98f68cb5f2ae67a0fc7d35c171d7816c266a741a49ac795` |
| repair-backups pre-repair attempt | 700 | `488c162ffdd5c28cecca892eb54291de8bba5baf39ebad1b7b7b49ac53a1e0d1` |
| whole run, 1638 files | 9501470 | manifest `c518992a1735bfbfe358eaa5db9158865740b8f62dd90e11f5a86b69d2c24cb5` |

The frozen guide is `hob-draftsim-guide` from `https://draftsim.com/mtg-hob-limited-set-review/`: its text hashes to `537de10ab83704b308d78efd21a77f9faf93b71ca1e24422de445aee3de30cbf`, which is the artifact's guide pin and the run's guide work-unit `input_sha256`. It is not the repo-local benchmark guide `.draftomen/enrichment-runs/hob/guide.txt` (`21f5d19e...`). The manifest covers 545 attempts and 544 responses; the probe re-derived the 544 results — 402 card-capability, 141 relationship, 1 guide — with 441 success and 103 malformed outcomes (`stages.saved_evidence.work_units`).

Re-verify the whole run with:

```
cd /Users/andrea/.draftomen/set-enrichment/hob-quickdraft/enrichment-runs/hob/9574d202eef14943
find . -type f | LC_ALL=C sort | xargs shasum -a 256 > /tmp/hob-587-manifest-check.sha256
diff /tmp/hob-587-manifest-check.sha256 ~/Projects/draftomen/docs/audits/hob-587-run-manifest.sha256
```

## Final run (revision 3)

| Observation | Value | Source key |
| --- | --- | --- |
| stages | all four `observed`, zero blockers | `stage_status`, `blockers` |
| enhancement origin | `artifact_compile`, no fallback | `context.enhancement_origin` |
| run tree | 1638 files, `c518992a...` before and after; `run_tree_unchanged` and `run_tree_matches_manifest` true | `run_tree_before/after` |
| installed profile | `c73802bf...` unchanged before and after | `installed_profile_digest_*` |
| provider | client not imported | `provider_client_imported` |
| network | denial installed, zero blocked attempts | `network_denial` |
| artifact checks | filename is digest, digest matches audit, `to_bytes()` reproduces the file, card and guide digests match, guide text matches the pin, no accepted paid relationship payload | `stages.saved_evidence.checks` |

## What the saved evidence contains

The artifact is confirmed and holds 202 cards, 583 capability facts and 685 relationships with zero stored projections (`stages.saved_evidence.artifact`). Two facts decide the trace: every one of the 685 relationships has `run_id local-c7a4f08030a1bfb7`, the deterministic local pair matcher (`draftomen/set_enrichment_workflow.py:629-633`, provider `draftomen`, model `local-pair-matcher-v1`), and the 141 paid relationship validation units accepted nothing (`relationship_units_with_accepted_payload: 0`) — 90 malformed, 51 rejected with "relationship prerequisites contradict their source evidence."

The same aggregation shows what the paid pass produced elsewhere: of the 402 card-capability units, 0 findings were accepted, 172 rejected and 1147 uncertain; the single guide unit rejected 9 and left 2 uncertain. The 583 oracle facts in the artifact therefore come from the local resolver, not from paid acceptance. Mechanism counts: `token-go-wide-payoff` 333, `token-sacrifice-outlet` 269, `token-death-payoff` 37, `recursion-graveyard-payoff` 27, `mill-graveyard-payoff` 9, `fodder-sacrifice-outlet` 8, `fodder-dies-payoff` 2; all seven are declared in `ROLE_COMPATIBILITY_RULES` (`draftomen/set_enrichment_candidates.py:119-132`). The artifact stores 467 rejected findings (376 relationship, 82 oracle, 9 guide) and two `uncertain` guide claims (`mechanic-recruit`, `mechanic-storied`), so the compiled enhancement carries 0 mechanics (`draftomen/profile_enhancement.py:109-113`).

## The four gates

**Unsupported contract (typed projection gate).** `compile_confirmed_relationship_projections` keeps every confirmed relationship and fills `prerequisite_projection` only when `_compile_relationship` can build and re-validate one (`draftomen/profile_relationship_projection.py:223-249`, `337-383`). Observed: 685 relationships compiled, 3 projected; `failure_reasons` `projected` 3, `source_clause_unbound` 678, `target_clause_unbound` 4, with `classifier_matches_compiler` true. No failure carried `*_capability_fact_unusable` or `participant_card_*`, so the 682 silent relationships die at the clause-vocabulary gate, not from data loss. Per mechanism only `mill-graveyard-payoff` (2 of 9) and `recursion-graveyard-payoff` (1 of 27) project. The three ids equal the installed profile's (`generated_at 2026-09-16T19:02:08+00:00`, `enhancement.artifact_sha256` = this artifact), so the machine's profile matches what the compiler produces today. These counts are a code diagnostic, not a semantic verdict: three projected is not three correct. The third projected id, `local-c7a4f08030a1bfb7:relationship:recursion-graveyard-payoff:103442:103442-return-creature-card:103422:103422-0-threshold-graveyard-payoff`, is one of the recursion-family rows the #587 review flags for adjudication (rows [10,20] and [30,20] of `docs/audits/hob-587-relationship-ledger.json`): Gathering of Darkness [103442] returns up to one target creature card from your graveyard to your hand (frozen Oracle, `sources/card-database.json:2543`), a graveyard consumer recorded as a threshold enabler for the seven-card graveyard payoff of Most Decrepit Old Bird // Speak Secrets [103422]. Compiler success does not rescue that classification. Runtime support needs a projection — `_matched_relationship_support` returns None without one (`draftomen/pool_ledger.py:1097-1099`) — which the pool stage confirms: the paid enhancement produced 0 relationship support records while the fixture produced R1 support on two rows and R6 on one. The reviewed R1 pair (`relationship:token-go-wide-payoff:103382:103382-token-maker:103526:103526-go-wide-payoff-1`) is unprojected in the paid artifact and projected only in the calibration fixture, so the committed smoke proves the runtime path on that fixture, not on the paid representation.

**Settings (contextual scoring).** The preference defaults to false (`draftomen/preferences.py:186`), reaches the session via `draftomen/qt_gui.py:550`, and empties the contextual breakdown when false (`draftomen/pickengine.py:1157-1164`). Observed: with contextual scoring off, all four rows carry `synergy 0.0`, `aggregate 0.0`, no support records, empty evidence and rationale kinds `[rating, color]`. The user's own preferences file had the flag true on 2026-09-17, so the shipped default is not the local blocker.

**Settings (AI-enhanced suggestions).** The toggle feeds `_enhancement_availability_for_context` (`draftomen/session.py:4581-4617`, `688-750`), and `availability.enabled` becomes `enhanced_relationships_enabled` for scoring (`1889-1891`, `2415-2417`), which strips support records (`draftomen/pickengine.py:974-977`, `498-515`). Observed: with relationships off no row carries support; with the toggle on but the paid enhancement loaded the result is the same — 0 support rows, 0 R1/R6 support — while theme-package synergy still appears (`0.380488` at pick 5, `1.5` at pack 2 pick 13). Availability and representation coverage are separate facts, and no setting recovers the 682.

**Score cap.** `MAX_SYNERGY_TERM` is 1.5 and the aggregate clamps at ±6.0 (`draftomen/pickengine.py:64`, `96-103`, `305-309`). Observed fixture values: pick 4 `0.0` with no support, pick 5 `0.570732` with R1 support, pick 6 `0.205793` with R6 support, pack 2 pick 13 `1.5` with R1 support. On that saturation row the enhancement-removed control also sits at 1.5, `raw_score_delta_vs_enhancement_removed` is 0.0, `cap_binds` is true, and `rationale_mentions_r1_finding` is false — the cap's shape asserted by `scripts/hob_relationship_scoring_smoke.py:327-348`, now observed through the probe. The paid control reaches 1.5 on the same row through the theme package with no support record.

## Rendered surface

`draftomen/pickengine.py:574-708` builds the detailed text, `draftomen/session.py:2514-2540` publishes the payload, `draftomen/qt_adapter.py:141-175` converts it, and `CardPreview.qml:426-437` binds `recommendation.explanation` into `cardPreviewExplanation` when `detailedIntel` is true.

Observed, offscreen, with the label read back from the component tree: the support-free row renders the same generic text under both the fixture and paid enhancements (83 DO points, rating and colour evidence only); the fixture's support-carrying row renders the relationship evidence — "Confirmed relationship support: relationship relationship:token-go-wide-payoff:103382:103382-token-maker:103526:103526-go-wide-payoff-1 (token-go-wide-payoff) for Bard's Company [103526] ... (+0.57 DO points)" — and the paid enhancement has no such row at all (`paid_enhancement.support.row_present: false`). `qml_renderer_matches` is true for all three rendered payloads, and `qml_theme_resolved` is true (font pixel size 11, colour `#c8c2b8`), so the label resolved theme values while the stage ran.

This is component-level evidence: one QML component rendered with a payload the adapter produced. It does not demonstrate the application window, live session behaviour or any interactive journey.

## Qt messages

The probe installs a Qt message handler, which is also why a run can print nothing while messages still exist: they land in `qt_messages_by_phase` instead of the console. Observed phases: `rendered_advice` holds one font-alias cost notice ("Populating font family aliases took 81 ms"), and `teardown` holds `Theme.qml:52: TypeError: Cannot read property 'systemTextScaling' of null`.

That teardown message is the warning seen earlier, now attributed: it fires after the stage, when the `guiPreferences` context property is released while the `Theme` singleton (`draftomen/qml/Theme.qml:52-54`) is still alive. It does not affect the recorded evidence — all label reads happened before it, the component reached `Ready`, and every rendered text matched its renderer counterpart. No message was recorded during the other three stages.

## Limits and open questions

- The rendered claim is component-level, as stated above; the full window and session were never launched in this audit.
- Representation is measured, not meaning: how many of the 682 unprojected relationships are semantically sound but unrepresentable in the closed clause grammar is not settled by a compiler histogram, and compiler rejection alone is not semantic invalidity — just as projection is not semantic certification, one of the three projected rows is itself under ledger adjudication (typed projection gate above).
- The published remote profile object served by `~/.draftomen/set-profiles/v1/manifest.json` was not compared with the locally installed profile.
- Why the paid capability units accepted nothing while the local resolver produced all 583 oracle facts, and why the paid guide unit rejected all nine of its findings, belongs to the inventory and mechanic work.

## Reproduction

Note: #589 changed the projection gates after this capture with qualified prerequisite emission, zone-supply rejection, and per-finding conversion outcomes, so a rerun of the checked-in revision-4 probe classifies more rows as projected/qualified and no longer reproduces the committed `failure_reasons`, projected counts, or the revision-3 probe sha256.

```
shasum -a 256 docs/audits/hob_587_trace_probe.py   # expect 829a85df505890582b689d0e3a546acb384c94d142078ae4618365b8d95a145c (revision 4; the capture above used revision 3, 24ac41a8...)
cd /Users/andrea/Projects/draftomen
QT_QPA_PLATFORM=offscreen uv run --no-sync python docs/audits/hob_587_trace_probe.py --out /tmp/hob-587-trace-out.json
jq '.stage_status, .context.enhancement_origin, .blockers, .qt_messages_by_phase' /tmp/hob-587-trace-out.json
jq '.stages.generated_representation.failure_reasons, .stages.generated_representation.per_mechanism' /tmp/hob-587-trace-out.json
jq '.stages.pool_support.focused_checks' /tmp/hob-587-trace-out.json
jq '.stages.rendered_advice.qml_rendered_texts, .stages.rendered_advice.qml_renderer_matches, .stages.rendered_advice.qml_rendered_details' /tmp/hob-587-trace-out.json
jq '.run_tree_unchanged, .run_tree_matches_manifest, .network_denial.blocked_attempts' /tmp/hob-587-trace-out.json
```

The capture ran the executed revision 3 with two portable default expressions (`REPO_ROOT` derived from the utility's own checked-in location, `RUN_DIR` under the home directory; the `DRAFTOMEN_REPO` and `HOB587_RUN_DIR` overrides are unchanged) plus the docstring run path; its diff against the executed `/tmp` copy is those lines only. The checkout now carries revision 4 for #589, so a rerun from the checkout no longer reproduces the committed final output beyond the timing-dependent Qt font-alias message and the phase list it appears in — compare with those keys dropped only to confirm determinism of the new gates, not to reproduce this capture:

```
diff <(jq 'del(.qt_messages_by_phase, .qt_phases_seen)' /tmp/hob-587-trace-out.json) \
     <(jq 'del(.qt_messages_by_phase, .qt_phases_seen)' docs/audits/hob-587-trace-out.json)   # expect no other difference
jq '.qt_messages_by_phase, .qt_phases_seen' /tmp/hob-587-trace-out.json
```

The final output is preserved at `docs/audits/hob-587-trace-out.json` (sha256 `74244c03b89d29505851b6be022498005058eef21caa6e81479621ffe73a80e6`); the earlier successful run is kept at `docs/audits/hob-587-trace-out-r2.json` (sha256 `61bc5f54...`). Every quoted value can be re-read from those files without rerunning anything, for example:

```
jq '.stages.pool_support.focused_checks.saturation_row' docs/audits/hob-587-trace-out.json
jq '.stages.rendered_advice.qml_rendered_texts["fixture_enhancement.support"]' docs/audits/hob-587-trace-out.json
```

Environment: repository virtualenv (Python 3.12.9 with PySide6), repository root as the working directory, no network and no API key. The utility derives its default repository root from its own checked-in location and its default paid-run and installed-profile paths from the home directory; `DRAFTOMEN_REPO`, `HOB587_RUN_DIR` and `HOB587_INSTALLED_PROFILE` override them. The probe reads the paid run read-only, imports no provider client, denies its own network access, and writes only its output JSON.
