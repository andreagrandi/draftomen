# Releasing Draft Omen

Stable Draft Omen releases create a public GitHub Release with the version's
native bundle assets and changelog body. The native bundles are the only
distribution channel. Releases no longer publish to PyPI or update the
Homebrew tap. Versions up to 0.4.0 stay on PyPI and in
`andreagrandi/homebrew-tap`, but they get no further updates. Development
releases are a separate GitHub prerelease path.

## Hosted profile data operations

Profile assets are static files in the same Astro/Cloudflare website snapshot
as the rest of the site. A commit merged to `master` is the only deployment
trigger: the existing Cloudflare Git integration builds and publishes the
complete `website/dist/` output atomically, including
`website/public/profiles/` at
<https://www.draftomen.com/profiles/> and `website/public/profiles-dev/` at
<https://www.draftomen.com/profiles-dev/>. Profile assets never trigger or
gate a native bundle release or application startup.

Terminal `watch`, `watch --plain`, CLI `watch`, and the native live command use
`https://www.draftomen.com/profiles/manifest.json` by default.
`--profile-manifest-url` overrides the manifest for either client, while
`--offline-profiles` selects `ProfileNetworkPolicy.OFFLINE` for profile
networking only. It does not disable Scryfall card metadata, card images, or
static card-data networking.
A missing or failed hosted profile leaves the local cache or deterministic
fallback in use; live runtime sessions never load ratings directly from 17Lands.
Native applications retain their validated bundled baseline as a local fallback,
and the existing native ratings control reports the shared refresh's `updated` or
`unchanged` outcome. Failed, offline, or missing refreshes retain the last usable
cache when ratings exist; with no usable profile, deterministic fallback scoring
remains active.

These are runtime client configurations, not package or release inputs. The
manifest is not bundled: producer generation, website publication, and
native bundle releases remain independent.

Follow [`docs/set-profiles.md`](set-profiles.md) for validated object staging,
manifest construction, pruning, cache headers, retention and legal erasure,
Cloudflare integration ownership and shutdown, current service limits, data
minimization, and the two-merge master-only smoke check. Baseline application
bundling remains owned by #313; this hosting boundary does not absorb that
work.

## Release trigger

Normal pushes and merges to `master` run CI but do not publish a stable release.
A stable release starts only when a tag matching `v*` is pushed, and the
workflow rejects tags that do not match the version in `pyproject.toml`.

For a manually requested native development build, ask exactly `make a new dev
release`. The repository's development-release skill dispatches
`.github/workflows/native-bundles.yml` on `master` with a unique UTC request ID,
watches that exact run, verifies the rolling prerelease's changelog content and
assets, and reports the workflow and release URLs.

## Changelog contract

`CHANGELOG.md` always has an exact `## [Unreleased]` heading. Released entries
use an exact heading in this form:

```text
## [X.Y.Z] - YYYY-MM-DD
```

The shared helper command
`python3 scripts/extract_changelog.py --section SECTION --output PATH` reads the
root changelog and writes the non-empty body under the exact section through
the next `## ` heading, with one trailing newline. Missing, duplicate, or empty
sections fail the command.

Before preparing a stable version PR, use the UTC release date to promote the
Unreleased entries unchanged into `## [X.Y.Z] - YYYY-MM-DD`, then restore an
empty `## [Unreleased]` heading immediately above the dated section. Include
that changelog promotion with the version change before merging and tagging.

## Development releases

Development releases are native-only builds for manual testing. The workflow creates or updates the GitHub prerelease with the mutable tag `development`; it does not create a version tag or invoke the stable release workflow. The rolling prerelease is always available at the stable URL:

<https://github.com/andreagrandi/draftomen/releases/tag/development>

Each build uses this identifier:

```text
v<VERSION>-dev.<YYYYMMDD>.<RUN_NUMBER>
```

`VERSION` comes from `pyproject.toml`, `YYYYMMDD` is the UTC build date, and
`RUN_NUMBER` is `github.run_number`. The run number is scoped to the workflow,
progresses monotonically, and does not change when that run is rerun. The
release title and notes identify the same version, UTC date, and progressive
run number. For example, run 7 for version `0.2.0` on 2026-08-25 uses
`v0.2.0-dev.20260825.7` and the title `Draft Omen v0.2.0 development build
2026-08-25 #7`.

The rolling prerelease notes retain this build metadata and then include the
exact heading `## Changes since previous release` followed by the non-empty
body extracted from `## [Unreleased]` in `CHANGELOG.md`. A missing, duplicate,
or empty Unreleased section fails the development workflow before publishing.
The development-release verification must confirm both the metadata and the
extracted changelog content.

The rolling prerelease contains exactly four unsigned assets for the current
build:

- `draftomen-<build-id>-unsigned-macos-arm64.dmg`
- `draftomen-<build-id>-unsigned-macos-x86_64.dmg`
- `draftomen-<build-id>-unsigned-windows.exe`
- `draftomen-<build-id>-unsigned-sha256sums.txt`

The checksum file contains SHA-256 checksums for the two DMGs and the Windows
executable. The `arm64` DMG is built on `macos-latest` and runs on Apple
Silicon. The `x86_64` DMG is built on `macos-15-intel` and runs on Intel Macs.
Intel builds continue only while GitHub offers an Intel macOS runner. Each
build job checks the executable with `lipo -archs` and fails when the
architecture differs from the one in the asset name. Before this change the
single macOS asset was named `draftomen-<build-id>-unsigned-macos.dmg`.
Each macOS asset is a compressed, read-only DMG with a `Draft Omen` volume
containing the app at its root and an `Applications` symlink to `/Applications`.
For manual testing, download the Actions artifact ZIP, extract the DMG, attach
it read-only and without Finder browsing with `hdiutil attach -readonly
-nobrowse -mountpoint <temporary-mount> <file>.dmg`, and run
`tests/bundle_smoke.py` against the mounted app. Detach the image afterward even
when the smoke test fails (an `EXIT` trap is recommended); alternatively open
the DMG in Finder and drag the app onto its `Applications` shortcut. The DMG is
unsigned, and the app has only Nuitka's required ad-hoc signature (no developer
or distribution identity or notarization). Arrange platform-appropriate signing
and notarization before redistributing the copied app.

Development releases do not replace a stable release.

## Publish a release

1. On a feature branch, choose the UTC release date and promote the non-empty
   body under `## [Unreleased]` unchanged into:

   ```text
   ## [<version>] - <YYYY-MM-DD>
   ```

   Restore an empty `## [Unreleased]` heading immediately above that dated
   section, then bump the package version:

   ```bash
   uv version <version>
   uv run nox -s ci
   ```

   `uv version` updates only `pyproject.toml` and `uv.lock`. Set the same
   version in `pysidedeploy.macos.spec`, `pysidedeploy.windows.spec`, the
   expected version in `tests/test_desktop_bundle.py`, and
   `website/package.json` with its lock file.

2. Merge the version and changelog promotion through the normal pull request
   workflow.
3. From the updated `master` branch, create and push the matching tag:

   ```bash
   git tag v<version>
   git push origin v<version>
   ```

The stable workflow checks out the tagged repository and runs
`python3 scripts/extract_changelog.py --section <version> --output PATH`.
Missing, duplicate, or empty sections fail the workflow instead of publishing
empty or generated notes. It rejects tags that do not match the versions in
`pyproject.toml` and `website/package.json`, builds the website, and runs the
full CI gate. It builds the native bundles in parallel, and creates or updates
the public GitHub Release with the dated changelog body and native bundle
assets once both finish.

## Verify the published release

The public GitHub Release for `v<version>` must be published with the promoted
dated changelog body and the three native bundle assets plus checksum file. The
macOS release assets are `draftomen-v<version>-unsigned-macos-arm64.dmg` and
`draftomen-v<version>-unsigned-macos-x86_64.dmg`. Download the asset that
matches the Mac's architecture, open it
in Finder or attach it read-only and without Finder browsing with
`hdiutil attach -readonly -nobrowse -mountpoint <temporary-mount> <file>.dmg`,
and run `tests/bundle_smoke.py` against the mounted app. Use an `EXIT` trap to
detach the image even when smoke testing fails. To distribute it, drag the app
onto the DMG's `Applications` shortcut in Finder, then eject the image; arrange
platform-appropriate signing and notarization before redistributing the copied
app.
