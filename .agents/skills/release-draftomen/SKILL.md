---
name: release-draftomen
description: >-
  Publish a Draft Omen version through the complete version bump, pull request,
  merge, tag, GitHub Actions, and GitHub Release verification workflow. Use
  whenever the user says "release X.Y.Z", "publish version X.Y.Z", "cut a Draft
  Omen release", or otherwise asks to ship a new Draft Omen version.
---

# Release Draft Omen

Normal pushes and merges to `master` do not publish a release. Only pushing a
tag matching `v*` starts `.github/workflows/release.yml`. A release publishes
the native macOS and Windows bundles to a GitHub Release. Draft Omen is no
longer published to PyPI or Homebrew.

An explicit request containing the target version authorizes all release-scoped
mutations: version edit, commit, push, ready PR creation, CI monitoring, PR
merge, annotated tag creation and push, release monitoring, and release
verification. Do not ask for those permissions again or stop after creating the
PR. This authorization does not cover unrelated changes.

If the request omits the exact `X.Y.Z` version, ask for it. Never infer a version.

## Preflight

1. Read `AGENTS.md`, `docs/releasing.md`, and `CHANGELOG.md`.
2. Use `gh` for every GitHub operation and confirm `gh auth status`.
3. Confirm the worktree is clean. Preserve and report unrelated changes.
4. Check the requested version is newer than `uv version --short`.
5. Confirm the remote tag `vX.Y.Z` and GitHub Release `vX.Y.Z` do not exist.

## Prepare and merge the version PR

Start from current `master`:

```bash
git switch master
git pull --ff-only
git switch -c release-X.Y.Z
```

Before bumping the package version, promote the non-empty body under the exact
`## [Unreleased]` heading in `CHANGELOG.md` to:

```text
## [X.Y.Z] - YYYY-MM-DD
```

Use the UTC release date, preserve the entries unchanged, and restore an empty
`## [Unreleased]` heading immediately above the new dated section. Then run:

```bash
uv version X.Y.Z
```

`uv version` updates only `pyproject.toml` and `uv.lock`. Set the same version
in each of these files too:

- `pysidedeploy.macos.spec` and `pysidedeploy.windows.spec`: every
  `--file-version`, `--product-version`, and `--macos-app-version` value.
- `tests/test_desktop_bundle.py`: the expected version in
  `test_native_specs_preserve_project_metadata`.
- `website/package.json` and the two root-package `version` fields in
  `website/package-lock.json`. The release workflow rejects a tag that does not
  match `website/package.json`.

Inspect the diff and run:

```bash
uv run nox -s ci
```

Stage only the version files and `CHANGELOG.md`, run `git diff --cached --check`,
and commit with `Release X.Y.Z`. Push the branch and open a ready PR against
`master` with the mandatory `AGENTS.md` PR template. Use `gh pr checks --watch`,
then merge the green PR with the repository's merge method and delete its remote
branch.

Do not tag the release branch or an unmerged commit.

## Tag and publish

Refresh `master`, confirm it reports `X.Y.Z`, and confirm the tag is still absent:

```bash
git switch master
git pull --ff-only
uv version --short
git tag -a vX.Y.Z -m "Draft Omen X.Y.Z"
git push origin vX.Y.Z
```

Find the exact `Publish release` run for tag `vX.Y.Z` with `gh`, then watch it
through completion. The native builds take about 15 minutes. The `validate`
job checks that the tag matches `pyproject.toml` and `website/package.json`,
builds the website, extracts the non-empty body under the exact
`## [X.Y.Z] - YYYY-MM-DD` section from `CHANGELOG.md`, and runs the full CI
gate. A missing, duplicate, or empty section fails the run; the workflow never
falls back to generated notes. The native bundle jobs build and smoke-test both
macOS DMGs and the Windows executable. After both finish, the
`github-release` job creates or updates the public GitHub Release with the
changelog body, the native assets, and the checksum file.

## Failure handling

Inspect failures with `gh run view --log-failed`. Fix workflow or packaging
failures on a new branch through another green PR.

If the GitHub Release was not published, rerun the failed jobs with
`gh run rerun --failed` when the fix does not need new code on the tag. When it
does, merge the fix, then delete and recreate `vX.Y.Z` on the new `master`
commit. If the GitHub Release for `vX.Y.Z` is already public, do not move the
tag; publish a new patch release instead.

## Verify the release

Inspect `gh release view vX.Y.Z` and confirm:

- the release workflow concluded successfully;
- the release is published, not a draft or prerelease;
- its body contains the promoted dated changelog entries;
- it has `draftomen-vX.Y.Z-unsigned-macos-arm64.dmg`,
  `draftomen-vX.Y.Z-unsigned-macos-x86_64.dmg`,
  `draftomen-vX.Y.Z-unsigned-windows.exe`, and
  `draftomen-vX.Y.Z-unsigned-sha256sums.txt`;
- local `master` is clean and synchronized.

Report the version, tag, workflow URL, and GitHub Release URL.
