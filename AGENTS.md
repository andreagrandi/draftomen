## Session Start Workflow

Before making code or documentation changes in this repo:

1. Switch back to `master`.
2. Pull the latest changes with `git pull --ff-only`.
3. Create a new branch with a short, descriptive name related to the feature being added or the bug being fixed.
4. Make the requested changes on that branch.

Do not start work from an old feature branch unless the user explicitly asks to continue that branch.

## Ticket and Pull Request Scope

Before planning or editing work for an issue or pull request, read and follow
`.agents/skills/pr-scope-guidance/SKILL.md`. Complete its ticket-size assessment
before writing the implementation plan. Any decomposition warning requires an
explicit split-or-combine decision; do not treat a request to implement the
ticket as approval to waive that decision.

Even when the user approves combined ticket or pull request scope, divide
delegated implementation into bounded, file-owned execution slices and apply
the skill's inspection and focused-test gate between dependency waves.

## Verification Requirement

Before claiming any implementation is done, perform end-to-end verification of the actual user-facing workflow, not just unit tests or isolated helpers. If true end-to-end verification is impossible in the current environment, say so explicitly, document what was verified instead, and do not present the work as complete.

## Mandatory Change-Scoped End-to-End Check

Every ticket that changes a user-facing surface MUST prove the changed path on the real surface before the work is presented as done:

1. Run the actual surface (GUI, TUI, or CLI), not just unit tests, probes, or helpers.
2. Exercise exactly what changed: a new button MUST be seen, clicked, and observed to do its job; a new setting MUST be seen, edited, and observed to take effect. Do not re-verify unrelated flows.
3. NEVER claim a control, setting, field, or path exists in the UI without having seen it there. A preference/dataclass field with no bound UI control is not a setting.
4. If the real surface cannot run here (compiled bundle, missing hardware/service), say so explicitly, document what was verified instead, and do not present the work as complete.

## Epic Completion Workflow

When working on the last remaining open ticket belonging to an epic, do not close either the ticket or the epic until the ticket's implementation has been merged. Once the implementation is merged and the final ticket is closed, audit the full epic against the merged implementation and its acceptance criteria. Do not assume that the epic is complete merely because all of its planned child tickets have been completed.

- If any work required by the epic remains, create an additional child ticket covering the missing work and leave the epic open.
- If the whole epic has been implemented and verified, close the epic only after the final child ticket has been closed.

## Changelog Requirement

Before committing, pushing, or opening or updating a pull request, ensure the changeset includes an appropriate `CHANGELOG.md` entry under `## [Unreleased]`. For a release, move those entries to `## [X.Y.Z] - YYYY-MM-DD`, then restore `## [Unreleased]`.
