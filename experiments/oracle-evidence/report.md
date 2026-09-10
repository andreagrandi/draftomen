# Oracle-text local-model feasibility benchmark

## Outcome

Qwen3.5 9B Q5_K_M was the measured winner, but it did not meet the feasibility gates. Qwen3.5 4B produced no structurally valid comparison responses after deterministic wrapper removal. The selected 9B model completed the 301-row ELD snapshot within the time and memory limits, but only 93 responses passed the experiment's schema and evidence validator.

The qualitative review exposed a protocol defect: many rejected responses contained grounded, pool-relevant information but used an unapproved fact kind, used `face_index: 0` for a plain card, or labeled a single-card conditional payoff as an `interaction`. Structural compliance and semantic usefulness therefore need separate metrics in a revision. This does not excuse the seven unsupported claims found in the review sample.

## Protocol provenance

- Issue: #426.
- Source: tracked `website/public/card-data/eld.json.gz`.
- Source SHA-256: `599bbf43f1677eb1be7a313278b48e87d75801ea91db2d3707f5a7641e477182`.
- Source rows / Arena IDs / Oracle IDs: 301 / 301 / 286.
- Eligibility: every frozen exported row, including duplicate Oracle identities, basics, Adventures, and the blank-Oracle card.
- Protocol SHA-256: `82d283603af08b207a22e06876f7052a5f486c5998b6141537e0c0c9e6f870f7`.
- Cases SHA-256: `2be4b3e8d584f06f3a630900d07ee1d32934f21e447b14a8e1c4620ff0c606c7`.
- Frozen corpus SHA-256: `8a1f4baf57fea0cd4568d7104fcdcd69732b3d8d0690da4bb5dc9e9f240721b7`.
- Comparison output SHA-256: `4f36d84a72a2dbec0169532d65d1d9762c85913c54fc3b0772ded31602eabea7`.
- Full-set output SHA-256: `e0ecf4b965f101a54f4c6c928a37fc8ffd9f769b4b484b05f7ab4eb31b762c6a`.
- Comparison selection SHA-256: `4ce0344d34cbdc3a7c449deb9d299891c00833585e6ca93466f2e2700c82adb6`.
- Manual review SHA-256: `08627b5c6c3ffb68ebf48482592a5b8adba008e21d7516a5c08a0aab856d0e50`.
- Comparison judgment digest: `f08991f59d7520bef05484c67e6eeaa3de27eb864edba8e290f492323d412ff5`.

The initially frozen strict parser rejected all 30 responses because the runtime emitted empty `<think>` wrappers; 4B also emitted JSON fences. At the user's direction, protocol version 2 transparently added deterministic removal of only an empty leading think wrapper and an optional whole-response JSON fence. No claims, evidence, JSON fields, or malformed JSON were repaired. Both comparison and full-set inference were rerun under this recorded protocol. Every request records the applied normalizations.

## Runtime and candidates

- llama.cpp: `0.4.0-dev`, build `10888`, commit `72797e891`.
- Runtime archive SHA-256: `9818977ce13d3a4eb8548829868eb25e119155709aa6917862360cd7d88cc2f2`.
- `llama-server` SHA-256: `d707b6db4c1397a7383176fba12d339e5b33c7513669d74c8fbc2a76f6979a72`.
- Qwen3.5 4B Q5_K_M SHA-256: `8814232b85594dcd46c50e5b8b29324a7efe9e746edbe8a3d1df3d3fce7aad39` (3,143,656,608 bytes).
- Qwen3.5 9B Q5_K_M SHA-256: `dc2a39aef291f91a9116ad214058da0d86eb648743a124bd8c333787c4b9c91c` (6,577,841,376 bytes).
- Both GGUFs identify Apache-2.0 upstreams `Qwen/Qwen3.5-4B` and `Qwen/Qwen3.5-9B` respectively.
- Both native templates had SHA-256 `ee1c4923e67f3f382fdf5e6b0f2156b236e2240f13b19731dbc4a2c82ca3d6dc`; no tokenizer-sequence equivalence is claimed.
- Metal was confirmed from the owned process's loaded `libggml-metal` and AGXMetal mappings, while `/props` confirmed the exact model path and alias.

## Five-case comparison

Thirty requests were attempted: three repeats of five cases for each model, with the frozen alternating block order and zero retries.

| Candidate | Valid | TP | FP | FN | Precision | Recall | F1 | Stable cases | Median latency |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Qwen3.5 4B Q5_K_M | 0/15 | 0 | 0 | 81 | 0.000 | 0.000 | 0.000 | 0/5 | 17.382 s |
| Qwen3.5 9B Q5_K_M | 9/15 | 33 | 0 | 48 | 1.000 | 0.407 | 0.579 | 3/5 | 26.859 s |

The 9B model was stable on draw-second, quantity/threshold, and the negative pair. Food and timing cases failed evidence validation in all three repeats. The negative pair invented no Redcap/Alliance relationship. The 9B model won by F1; its F1 remained below the predeclared 0.70 stop boundary.

## Full ELD snapshot

| Measure | Result | Criterion |
|---|---:|---:|
| Eligible / attempted / completed | 301 / 301 / 301 | complete |
| Cold-start-to-final durable write | 6,861.608 s (114m 21.6s) | at most 7,200 s |
| Valid responses | 93/301 (30.90%) | at least 99% |
| Invalid evidence | 205 | — |
| Invalid schema | 3 | — |
| Valid uncertain responses | 0/93 (0%) | at most 20% |
| Facts in valid responses | 255 | — |
| Retries | 0 | 0 |
| Uncompleted Arena IDs | 0 | 0 |
| Teardown | 0.954 s | reported separately |

### Memory telemetry

- Peak sampled process-tree RSS: 7,367,770,112 bytes.
- Kernel maximum RSS: 7,367,294,976 bytes.
- Kernel peak footprint: 9,487,954,520 bytes.
- Operational proxy, defined as their maximum rather than sum: 9,487,954,520 bytes (8.84 GiB).
- Limit: 17,179,869,184 bytes (16 GiB).
- Sampling: 24,012 samples at 250 ms, zero sampling gaps, zero memory aborts.
- Host physical memory: 34,359,738,368 bytes.
- Baseline and final swap and `vm_stat` snapshots are retained with the external telemetry. RSS and footprint are overlapping Apple unified-memory accounting, not separate allocations or a backend VRAM trace.

## Manual review

The deterministic sample contained 15/15 completed adjudications: five lowest-ID valid outputs, five suspicious outputs, fill rows, an Adventure card, and the blank-Oracle card. The model-uncertain-valid stratum was empty.

- Validator-valid rows captured their material facts: 6/6 (100%).
- Rows judged to capture their material facts regardless of taxonomy: 13/15 (86.67%).
- Rows with at least one unsupported claim: 3/15.
- Unsupported claims: 7.
- Model-uncertain valid rows: none.
- Reviewer-uncertain rows: none.

### Grounded, useful output rejected by the contract

**Knights' Charge (70461)** correctly exposed both pool-relevant Knight payoffs: attacking Knights drain each opponent and gain life, while the activated ability returns all Knight creature cards from the controller's graveyard. It failed because the model used unapproved `cost` and `condition` kinds and labeled a single-card Knight payoff as `interaction`.

**Belle of the Brawl (70225)** correctly exposed menace and the attack payoff giving other controlled Knights +1/+0 until end of turn. It failed because the model addressed a plain card as `face_index: 0`.

**Trail of Crumbs (70326)** correctly exposed Food creation, the Food sacrifice ability, the sacrifice trigger, payment, and permanent selection. It failed structural checks and also weakened a mandatory post-payment look into an optional action; that timing/modal change is unsupported.

These examples show that schema adherence cannot stand in for semantic utility. Pool-relevant tribal, artifact, enchantment, Food, graveyard, and threshold facts were often present even when the entire response was rejected.

### Material semantic errors

**Fae of Wishes // Granted (70191)** invented activation-only-in-play wording, called Granted an activated ability, and invented a same-turn cross-face dependency involving another mana source and unspecified timing. It also omitted flying and the activation's blue component.

**Giant Killer // Chop Down (70161)** reversed the tap-symbol rule, called Chop Down an activated ability, and copied Giant Killer's `{1}{W}, {T}` cost onto the Adventure face. This is unsafe for scoring without review.

## Limitations

- Five frozen cases and fifteen reviewed rows are bounded feasibility evidence, not a population accuracy estimate.
- The user-directed wrapper normalization is a documented protocol revision after the original strict run failed; results are not presented as if it had been predeclared initially.
- The narrow type enum and two-card `interaction` evidence rule are poorly matched to one-card full-set requests and understate semantic utility.
- Conversely, accepting all semantically plausible text would hide real rules, timing, face-assignment, and modality errors.
- ELD's 301-row snapshot made the operational pass unnecessarily long; a future iteration should use a smaller set or a predeclared staged stop gate.
- No production scoring, profile, website artifact, workflow, runtime dependency, or application behavior changed.

## Smallest justified next ticket

Revise the extraction/evaluation contract before any production integration: separate grounded semantic content from taxonomy compliance; represent pool-relevant categories and payoffs without requiring a second supplied card; validate plain versus faceful evidence addresses; and add explicit activated-cost, Adventure, modality, and timing handling. Rerun a frozen held-out benchmark on a smaller set before considering contextual-score integration.

## Decision

The selected model completed inside the operational limits, but comparison F1 was 0.579, structural validity was 60% in comparison and 30.90% across ELD, and the manual sample contained seven unsupported claims. The measured schema mismatch is revisable, but the current candidate/protocol combination is not safe for production scoring.

**stop**
