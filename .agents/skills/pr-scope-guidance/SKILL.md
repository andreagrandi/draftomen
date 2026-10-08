---
name: pr-scope-guidance
description: >-
  Assesses and decomposes Draft Omen tickets before implementation planning,
  then constrains PR and subagent scope. Use when starting or continuing issue
  work, planning implementation, deciding PR scope, or updating a pull request.
---

# Plan Reviewable Ticket, PR, and Agent Scope

Apply this with `AGENTS.md` and workflow-specific skills. Higher reasoning effort
never authorizes broader scope, adjacent cleanup, or speculative features.

## Assess Ticket Size Before Planning

Complete this assessment before writing a detailed plan or editing files:

1. Inspect the issue, relevant code, dependencies, and comparable merged work.
   If your tool provides a scoping subagent, use it for the code reading and
   verify the files it names. Data or network checks it cannot run stay with
   you and must not be skipped.
2. Map every acceptance criterion to its likely subsystem, production files,
   test files, and user-facing verification boundary.
3. Identify independently reversible outcomes and any new architectural
   boundary, dependency, process, migration, or user interface.
4. Estimate changed files and meaningful changed lines, including tests,
   fixtures, documentation, and migrations; exclude generated artifacts only.
5. State which decomposition warnings apply.

A decomposition warning applies when the ticket:

- is sized `L` or `XL` on the project board (see the `gh-issue` skill's size
  definitions and implementation-readiness rule);
- spans more than one major subsystem;
- combines independently reversible behaviors;
- combines a reusable backend capability with substantial UI delivery;
- contains several independent security, lifecycle, or integration concerns;
- is expected to change more than roughly 10 files;
- is expected to exceed roughly 500 meaningful changed lines; or
- cannot be understood and reviewed as one coherent outcome.

Any warning requires a decomposition decision before planning. Propose smaller,
independently mergeable tickets with each outcome, dependency, expected files,
and verification. Wait for approval to split or explicitly combine; a request
to implement the issue does not waive the warning.

## Preserve Native Parent-Child Relationships

Every issue created by splitting an existing ticket must be attached as a
native GitHub sub-issue of the ticket being split. Body text such as
`Parent issue: #123`, project-board grouping, and blocked-by relationships do
not replace the native parent-child relationship.

Use `POST /repos/{owner}/{repo}/issues/{parent}/sub_issues` with the created
issue's database `id`, then verify it through the parent's `sub_issues`
endpoint before implementation begins. When a child ticket is split again,
attach its new tickets to that immediate child, not only to the root epic.


## Plan the Selected Slice

After the ticket passes assessment or the user approves combined scope, state:

1. the single outcome the PR or selected child ticket delivers;
2. the principal files and subsystems expected to change;
3. explicit exclusions;
4. proportional verification; and
5. the completed decomposition decision.

Implement only the selected acceptance criteria and strictly required changes.

## Bound Agent Execution Separately

User approval for a combined ticket or PR does not justify one exhaustive agent
assignment. Before delegation:

- divide work into file-owned slices with one coherent subsystem or contract;
- define shared types, inputs, outputs, and invariants before spawning agents;
- implement foundational contracts first, then parallelize independent consumers;
- keep tests with the slice whose observable behavior they defend; and
- never assign “implement the complete ticket across every integration” when
  stable narrower ownership exists.

Writing agents must perform targeted reads and start the owned change promptly.
Wait for each delegated slice with one full-length wait and judge the returned
result; never poll in short windows, evaluate interim progress, or steer or
stop an agent that is still working. Re-slice only after a slice returns
failed, cancelled, or expired. After each dependency wave, the parent inspects
the diff and runs focused tests before starting consumers. After two basic
regression loops in one slice, reassign the narrow broken contract instead of
continuing broad patches.

## Stop at Early Scope Drift

If exploration, implementation, or review reveals an unplanned architectural
boundary, dependency, process, migration, UI, or independently mergeable
outcome, stop before implementing it. Reassess scope and request a decomposition
decision when a warning now applies. Do not wait for the final diff.

## Keep PR Boundaries Coherent

Each PR has one primary outcome and one independently reversible reason to
change. Include required tests; exclude opportunistic refactors, unrelated
cleanup, speculative frameworks, sibling issues, and deferred product features.

Prefer stable boundaries when useful: core models and invariants; domain or
application services; offline or external integrations; frontend adapters and
UI; final cross-feature integration and end-to-end journeys.

## Verify Proportionally

- Core/domain slices need deterministic unit and focused integration tests.
- State, security, concurrency, and migration slices need explicit boundary and
  failure coverage.
- Adapter or UI slices need verification on the actual user-facing surface.
- Full cold-start, restart, packaging, and complete journeys belong to the final
  integration slice or the PR that changes those behaviors.

Do not weaken verification to shrink a slice, and do not repeat the complete
journey for preparatory slices that do not own it.
