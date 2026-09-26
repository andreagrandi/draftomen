# Native desktop bundles

Draft Omen's native desktop bundles are built in two automated contexts: a
manual `workflow_dispatch` run for temporary development testing, or as part
of the tagged `v*` release workflow. Ordinary pushes, merges, and pull requests
do not trigger native builds automatically. macOS has two artifacts, one for
Apple Silicon (`arm64`) and one for Intel (`x86_64`). Each is a compressed,
read-only DMG for Finder-native distribution with the app at the volume root
and an `Applications` symlink to `/Applications`. Each DMG runs only on its
own architecture.

Development builds are unsigned. Their DMG contains
`Draftomen-unsigned-macos.app`, which has only Nuitka's ad-hoc signature and is
not notarized. Tag releases sign the macOS app with Developer ID, and they sign,
notarize and staple the DMG, which contains `Draft Omen.app`. The
[signed macOS release path](#signed-macos-release-path) section describes it.
The Windows artifact is unsigned in both contexts.

## Tool choice

The repository uses [`pyside6-deploy`](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html), the deployment wrapper maintained with PySide6. It delegates the platform packaging step to Nuitka and has the inputs this application needs: an existing Python entry point, a project file, QML files, Qt modules and plugins, platform icons, and a macOS application bundle.

Briefcase was considered but rejected for this change. Its
[CI packaging workflow](https://briefcase.beeware.org/en/latest/how-to/building/ci/)
uses a stronger native-template and application-layout model and documents
unsigned uploaded artifacts, which would require migrating this existing
PySide6 entry point and launcher conventions. `pyside6-deploy` keeps the
current `draftomen.qt_gui:main` entry point and QML adapter intact while
still producing a `.app` or `.exe`; that smaller migration is the reason for
the choice.

The checked-in platform specs are:

- `pysidedeploy.macos.spec`: `dist-native/macos-unsigned/Draftomen-unsigned-macos.app`
- `pysidedeploy.windows.spec`: `dist-native/windows-unsigned/Draftomen-unsigned-windows.exe`

Both specs pin `Nuitka==4.1.3` in their `[python] packages` field, and the
workflow also installs that exact deployment dependency explicitly before
invoking `pyside6-deploy`. That explicit install is load-bearing:
`pyside6-deploy` fails when the pinned Nuitka is not importable, because its own
`python -m pip install Nuitka` fallback cannot run in a uv-managed environment
that ships no `pip`. An `uv run` never removes an already-installed package, so
only the workflow's own `uv sync --locked` step can prune it.
The existing `uv.lock` continues to select the PySide6 version. Run deployment
commands from the repository root because the specs use repository-relative paths.

## Explicit bundle inputs

The `[tool.pyside6-project]` table in `pyproject.toml` enumerates the Python
sources, every QML file, `qml/qmldir`, the logo, and both platform icons. Each
spec repeats the QML list in its supported `[qt] qml_files` key and declares the
Qt inputs used by the adapter:

- modules: `Core`, `Gui`, `Qml`, `Quick`, and `QuickControls2`;
- plugins: `imageformats`, `platforms`, `platformthemes`, and `styles`;
- QML: `QtQuick`, `QtQuick.Controls`, and `QtQuick.Layouts` imports through the
  `draftomen/qml` module;
- resources: `draftomen/assets/draftomen_logo.png` plus the `.icns` and `.ico`
  icons generated from that checked-in logo. These native icon files are
  checked-in source inputs, not CI build products; regenerate them from the
  logo with deterministic fixed-size PNG variants when branding changes.
- bundled profile: the immutable `draftomen/baseline_profiles/hob-quickdraft.json`
  resource, kept at its module-relative destination for `ProfileClient`.

Both specs keep PySide6's default unused QML plugin exclusions explicit:
`QtCharts`, `QtQuick3D`, `QtSensors`, and `QtWebEngine`. `QtTest` is not
excluded because the native smoke driver in `draftomen/qt_gui.py` imports
`PySide6.QtTest`. `pyside6-deploy` turns each exclusion into a Nuitka
`--noinclude-dlls` pattern, and on Windows that pattern removes
`Qt6Test.dll`, so the executable fails to start. On macOS the pattern matches
nothing because Qt ships as frameworks.

Both specs additionally declare `--include-package=socketio`. The Socket.IO
transport (`python-socketio[client]`) is a required base dependency, so it is
always importable: source startup and both native builds install it, and the developer Test Draft is always available. The specs still
hand Nuitka the transport as an explicit packaging input — the developer Test
Draft reaches it through `draftomen/draftmancer.py`, and the pinned
`--include-package=socketio` keeps the bundled package declared instead of
depending on what Nuitka's import analysis discovers.

### Baseline profile data-file mapping

Both platform specs include this identical, explicit Nuitka mapping:

```text
--include-data-files=draftomen/baseline_profiles/hob-quickdraft.json=draftomen/baseline_profiles/hob-quickdraft.json
```

The byte-for-byte source/destination preserves the module-relative lookup in
installed bundles. It is a read-only native input and does not create a default
hosted URL or alter the checked-in baseline.

`NavigationRail.qml` resolves the logo relative to the bundled QML directory.
The pyside6-deploy QML data-directory input preserves the QML and assets
siblings, and `draftomen.qt_gui._qml_directory()` also falls back to a
`qml` directory beside a compiled executable when the source package path is
not present. Card preview images are cache data and are intentionally not
bundle resources.

No application fonts are bundled. The GUI deliberately uses Qt's system fixed
font, so a bundle does not capture machine-specific font files. Application
metadata remains canonical (`Draft Omen` and the package version) through
`qt_gui._configure_application_metadata`; the platform spec title and output
directory mark each result as an unsigned development artifact. On macOS,
“unsigned” means no developer/distribution identity or notarization despite
Nuitka's required ad-hoc signature.

## Local build and inspection

Install the locked project dependencies and the pinned deployment dependency:

```bash
uv sync --locked
uv pip install "Nuitka==4.1.3"
```

Build the platform matching the host:

```bash
# macOS
mkdir -p dist-native/macos-unsigned
uv run pyside6-deploy --config-file pysidedeploy.macos.spec --force

# Windows PowerShell
New-Item -ItemType Directory -Force dist-native/windows-unsigned | Out-Null
uv run pyside6-deploy --config-file pysidedeploy.windows.spec --force
```

The deployment output directory must exist before `pyside6-deploy` finalizes
the bundle. The native workflow creates the matrix platform's directory
explicitly; the local commands above do the same.

Every `uv run` in the build path uses the environment `uv sync --locked`
installs, and the base dependencies already include the transport, so no
command needs an extra flag. The transport must be importable when the build
runs, and only an explicit `uv sync` prunes an environment, so the pinned
Nuitka installed just above also survives every following `uv run`.

On macOS, package the generated app as a Finder-native compressed DMG. Nuitka
builds for the host architecture, so `uname -m` names the DMG after the
architecture it contains:

```bash
staging="$(mktemp -d)"
cleanup() { rm -rf "$staging"; }
trap cleanup EXIT
ditto \
  dist-native/macos-unsigned/Draftomen-unsigned-macos.app \
  "$staging/Draftomen-unsigned-macos.app"
ln -s /Applications "$staging/Applications"
hdiutil create -volname "Draft Omen" -srcfolder "$staging" -ov -format UDZO \
  "dist-native/macos-unsigned/Draftomen-unsigned-macos-$(uname -m).dmg"
```

The resulting image is read-only and compressed with `UDZO`; mounting it
exposes the app at the volume root alongside the Applications shortcut.

Standalone Windows builds require Dependency Walker. The Windows workflow
downloads the official x64 2.2 archive over HTTPS, verifies its pinned SHA-256
(`35db68a613874a2e8c1422eb0ea7861f825fc71717d46dabf1f249ce9634b4f1`),
and extracts it into Nuitka's downloads cache before packaging. A changed
archive fails the build instead of executing unverified bytes. A first local
Windows build may still prompt before populating the same user cache.

The expected deployment output is the platform-specific path listed above.
For a non-building input inspection, add `--dry-run`; pyside6-deploy should
report the checked QML list and a generated Nuitka command containing the
PySide6 plugin, the QML/assets data directories, the platform icon option, and
the declared Qt module/plugin inputs. `--dry-run` does not produce a runnable
bundle.

The deterministic smoke helper launches the actual compiled executable twice
inside one temporary directory, in this order: the mock bundled-profile launch
first, then a default live launch. It does not import or run the source GUI, and
it removes developer Python path and virtual-environment overrides before
launching the bundle:

```bash
# macOS
uv run python tests/bundle_smoke.py \
  dist-native/macos-unsigned/Draftomen-unsigned-macos.app

# Windows PowerShell
uv run python tests/bundle_smoke.py `
  --timeout 300 `
  dist-native/windows-unsigned/Draftomen-unsigned-windows.exe
```

The helper reads `Contents/Info.plist` and resolves the macOS executable named
by `CFBundleExecutable`, so neighboring binaries and libraries do not affect
selection. It uses the Windows `.exe` directly.

The first launch passes `--provider mock --smoke-test --verify-bundled-profile`
and an isolated temporary `--app-dir`. It proves the deterministic mock path and
the bundled-profile preflight in one run: a successful launch exits with code 0
after the existing deterministic 800 ms GUI smoke behavior, and the flat profile
cache under the smoke app directory must still be absent afterwards, so the
preflight validated the pinned resource without mutating the cache.

The second launch starts the default live provider with `--provider live
--offline-profiles --no-startup-scan --smoke-test`, its own temporary
`--app-dir`, a `Player.log` the helper creates empty before launch, and
`--screenshot`. The helper requires a zero exit code, a log that is still empty
after the run, no profile-cache entry under that app directory, and a non-empty
screenshot. This launch proves a live start renders and shuts down with no Arena
log to follow, no profile network access, and no simulator or other service.

Mock mode avoids network, Arena logs, card downloads, and machine-specific
runtime caches, so the bundle can be visually inspected without live services.
The helper defaults to a 60-second process timeout, which applies to each
launch. The Windows CI smoke run passes 300 seconds because the first launch
must unpack the compressed one-file runtime before the application's smoke
timer starts; the bounded timeout still fails a bundle that does not exit.

### Bundled-baseline smoke

The smoke helper launches the actual compiled executable with the hidden
`--verify-bundled-profile` flag. That preflight validates the pinned resource's
canonical bytes, size, digest, schema, and HOB/QuickDraft identity offline, while
asserting that no profile-cache entry is written. The native workflow runs this
check against the final mounted macOS app and Windows executable.

### Native Test Draft smoke (manual)

The opt-in `--test-draft` mode of `tests/bundle_smoke.py` is the only check that
exercises the compiled bundle's Socket.IO path end to end. It launches the
bundled GUI twice through the hidden `--test-draft-smoke` flag against a
developer-managed Draftmancer server, so an opted-in developer proves that the
packaged transport publishes the Test Draft capability and that the native Test
Draft controls drive it, while the external server keeps running. The mode is
never part of CI: the workflow never runs it and never requires an ambient
Draftmancer service. The source-tree application, unlike this helper, starts the
pinned checkout itself when the Mocked Draft setting is enabled.

The helper runs both journeys in one invocation:

- **Auto** starts the capability's published default set code, completes a full
  simulated draft, reaches the ordinary build result, reports the deck size and
  the selected pair, then leaves the simulated session.
- **Manual** starts a manual HOB draft through the real Test Draft dialog
  **Start** control, closes the dialog, then confirms five consecutive picks
  through the real recommendation rows and the real **Pick** control, one of
  them a non-top recommendation (`non_top_rank` is 2). Every confirmation must
  grow the published pool and advance the pack and pick counters, and the run
  leaves through the dialog's **Leave test draft** control.

Prerequisites:

- a pinned Draftmancer checkout at
  `df08e5ef647aae54e0b1c569e70b4b2aa5e0016c`, started in another terminal with
  `DISABLE_PERSISTENCE=TRUE npm start`;
- the Scryfall complete default-cards bulk source the identity mapping uses,
  for example the development corpus cache at
  `.draftomen/corpus-cache/sources/scryfall-default-cards.jsonl.gz`;
- a prepared app directory containing `card-data/<set>.json.gz`, normally
  copied from the application's own `card-data/` directory, plus the optional
  cached set profile under `set-profiles/`.

Run the helper against the compiled bundle:

```bash
uv run python tests/bundle_smoke.py \
  --test-draft \
  --draftmancer-dir ../Draftmancer \
  --scryfall-bulk-file .draftomen/corpus-cache/sources/scryfall-default-cards.jsonl.gz \
  --app-dir <prepared-dir> \
  dist-native/macos-unsigned/Draftomen-unsigned-macos.app
```

Each journey prints its own summary line on stdout once it has left the
simulated session:

```text
Test Draft smoke: {"deck_size":..,"mode":"auto","picks":..,"selected_pair":"..","set_code":"..","status":"ok"}
Test Draft smoke: {"mode":"manual","non_top_rank":2,"picks":5,"pool_total":5,"set_code":"hob","status":"ok"}
```

The helper requires each journey's line, with a `status` of `ok`, a `mode` that
matches the journey, and a non-empty `set_code`; the Auto journey additionally
requires at least one pick and a non-empty deck, and the Manual journey at least
five picks, a `non_top_rank` of 2, and a positive `pool_total`. It then prints
its own compact per-journey summary and exits 0. The app gives up after 900
seconds and the helper after 1200 seconds by default, so the app reports the
reason first; `--timeout` overrides the helper's own bound. `--server-url`
selects a Draftmancer endpoint other than `http://127.0.0.1:3000`.

The helper only probes that endpoint before and after the two launches: it
never starts, stops, or configures Draftmancer, and the post-run probe proves
the external server is still alive once the app has exited. It requires an
explicit `--app-dir`, so the developer's real application data is never
mutated, and it runs each launch with `--offline-profiles` and
`--no-startup-scan` so the journey follows no Arena log and performs no profile
network request.

## GitHub Actions artifacts and tagged releases

`.github/workflows/native-bundles.yml` has two entry points:

- `workflow_dispatch` lets a developer start a temporary development build
  from the Actions page. Its outputs remain GitHub Actions run artifacts.
- `workflow_call` lets the tagged release workflow invoke the same build and
  smoke-test implementation. There is no automatic `pull_request` trigger,
  and ordinary pushes or merges do not trigger native builds.

The workflow runs three jobs: macOS `arm64` on `macos-latest`, macOS `x86_64`
on `macos-15-intel`, and Windows on `windows-latest`. Both macOS jobs use
`pysidedeploy.macos.spec`. Nuitka compiles for the host architecture and the
uv-managed Python is single-arch, so each macOS bundle comes from a runner of
its own architecture. A universal build would need a universal2 interpreter
that uv does not provide and would double the download size. Intel support
lasts as long as GitHub offers an Intel macOS runner. Each job syncs the locked
project environment, installs Nuitka 4.1.3, builds with the platform spec, and
smoke-tests the same payload shape that it uploads:

- macOS: after the build, the job reads `CFBundleExecutable` from the app's
  `Info.plist` and runs `lipo -archs` on that executable. The job fails when
  the result differs from the matrix architecture, so a runner image that
  changes architecture cannot publish a mislabeled DMG. The completed
  `Draftomen-unsigned-macos.app` is staged with an `Applications` symlink and
  packaged as the compressed `Draftomen-unsigned-macos-arm64.dmg` or
  `Draftomen-unsigned-macos-x86_64.dmg` image. The workflow attaches the image
  read-only and without Finder browsing at a temporary mountpoint, runs
  `tests/bundle_smoke.py` against the mounted app, and detaches the image even
  when the smoke test fails before uploading the DMG.
- Windows: the workflow runs `tests/bundle_smoke.py` directly against
  `Draftomen-unsigned-windows.exe` and uploads that `.exe` directly.

### Manual development artifacts

The uploaded artifact names are:

- `draftomen-macos-arm64-unsigned-development`;
- `draftomen-macos-x86_64-unsigned-development`;
- `draftomen-windows-unsigned-development`.

Download these from the **Actions** page: open the manual workflow run and
download its artifacts from the run summary. They are GitHub Actions run
artifacts, not GitHub Release assets; they are retained only for the
repository's configured Actions artifact-retention period and may expire.

Each macOS artifact download is a GitHub Actions artifact archive containing
exactly one file, `Draftomen-unsigned-macos-<arch>.dmg`; it is not the `.app`
directory directly. Pick the archive that matches the Mac, extract it, then
attach the DMG read-only and without Finder browsing at a temporary mountpoint.
The image contains the app at its volume root and an `Applications` shortcut:

```bash
arch="$(uname -m)"
unzip "draftomen-macos-${arch}-unsigned-development.zip" -d macos-download
macos_mount="$(mktemp -d)"
cleanup() {
  hdiutil detach "$macos_mount" >/dev/null 2>&1 || true
  rmdir "$macos_mount" || true
}
trap cleanup EXIT
hdiutil attach -readonly -nobrowse \
  -mountpoint "$macos_mount" \
  "macos-download/Draftomen-unsigned-macos-${arch}.dmg"
test -d "$macos_mount/Draftomen-unsigned-macos.app"
test -L "$macos_mount/Applications"
uv run python tests/bundle_smoke.py \
  "$macos_mount/Draftomen-unsigned-macos.app"
```

The `EXIT` trap detaches the image after a successful smoke test or a failure.
Alternatively, open the DMG in Finder and drag `Draftomen-unsigned-macos.app`
onto the `Applications` shortcut, then eject the image. The DMG is unsigned,
and the app has only Nuitka's required ad-hoc signature (no developer or
distribution identity or notarization). Anyone redistributing the app must
arrange platform-appropriate signing and notarization independently after
copying it from the mounted image.

### Signed macOS release path

`release.yml` calls the native workflow with `sign_macos: true`. The input
defaults to `false`, and `workflow_dispatch` runs cannot set it, so development
builds stay unsigned. With the input set, the two macOS jobs run in the
`macos-release` environment, which holds the Apple credentials and admits only
`v*` tags. The Windows job never enters that environment. `docs/releasing.md`
describes the credentials and how to rotate them.

Each signed macOS job runs these steps:

1. Check that every secret and variable is set, decode the `.p12` into a
   temporary keychain under `$RUNNER_TEMP`, and add that keychain to the user
   search list, because Nuitka calls `codesign` without `--keychain`. The job
   reads the SHA-1 of the `Developer ID Application` identity for
   `APPLE_TEAM_ID` from the keychain. This runs before the build, so bad
   credentials fail in seconds.
2. Copy `pysidedeploy.macos.spec` to an untracked
   `pysidedeploy.macos.signed.spec` with
   `--macos-sign-identity=<SHA-1> --macos-sign-notarization` added to
   `extra_args`. Nuitka then signs every binary and data file it places in the
   bundle with Hardened Runtime and a secure timestamp.
3. Run `pyside6-deploy --keep-deployment-files`, check Nuitka's own output
   `draftomen/deployment/qt_gui.app` with
   `codesign --verify --deep --strict`, copy it with `ditto` to
   `dist-native/macos-signed/Draft Omen.app`, and check the copy the same way.
4. Check the executable architecture with `lipo -archs`, then build
   `Draftomen-signed-macos-<arch>.dmg` from a staging folder filled with
   `ditto`.
5. Sign the DMG with the same identity and a timestamp. Submit it with
   `xcrun notarytool submit --wait`, using the App Store Connect API key. Any
   status other than `Accepted` prints Apple's log and fails the job.
6. Staple the ticket, then run `xcrun stapler validate` and
   `spctl --assess --type open --context context:primary-signature` on the
   DMG.
7. Mount the stapled DMG, run `spctl --assess --type execute` on the app inside
   it, and run `tests/bundle_smoke.py` against that app.
8. Upload that DMG as `draftomen-macos-<arch>-signed-release`.

A last step runs with `always()`. It deletes the temporary keychain, which also
removes it from the search list, and deletes the decoded `.p12`, the `.p8` and
the notarization result, even when signing or notarization failed.

#### Why the build copies with ditto

Nuitka signs the roughly 2,500 QML, `qmldir` and other data files in
`Contents/MacOS` as nested code. `codesign` stores their signatures in
`com.apple.cs.*` extended attributes. pyside6-deploy finishes by copying
Nuitka's app to `exec_directory` with `shutil.copytree`, which drops extended
attributes on macOS. That copy fails strict verification with
`code object is not signed at all` on a `.qml` file. `ditto` and
`hdiutil create -srcfolder` keep the attributes, so the signed path uses them
and moves only the DMG between jobs. `actions/upload-artifact`, `zip` and other
copies that ignore extended attributes break the seal in the same way.

The unsigned development bundles have the same broken seal, because they come
from pyside6-deploy's copy. Nothing checks ad-hoc signatures strictly, so it
has no visible effect there.

#### Entitlements

The signed app has no entitlements. `tests/bundle_smoke.py` passes on the
Hardened Runtime build without any, including the QML engine. Add an
entitlement only when a Hardened Runtime build fails without it, and list it
here with the failure that required it. Nuitka has no general entitlements
option, so adding one means re-signing the main executable after the build.

### Tagged release assets

For a tag such as `v1.2.3`, `release.yml` invokes the reusable native workflow
with `workflow_call` and `sign_macos: true`. The GitHub Release publication job runs only after the
`validate` job has passed the version, website, changelog, and CI checks and
all three native bundle jobs have built and passed their smoke tests.
It checks out the tagged repository, extracts the non-empty body under the exact
`## [1.2.3] - YYYY-MM-DD` section in `CHANGELOG.md`, and uses that body as the
GitHub Release notes. A missing, duplicate, or empty section fails the job
before release publication. The job downloads the three native Actions artifacts
from that release run, renames their payloads, generates SHA-256 checksums, and
creates or updates the GitHub Release with those notes.

Release publication is recoverable: rerunning the job reuses an existing draft
or release, replaces the four assets and changelog notes, and publishes any
draft left by an earlier interrupted attempt. Native Actions artifacts are
likewise overwritten when their build jobs are rerun.

The persistent public assets attached to the `v1.2.3` GitHub Release are:

- `draftomen-v1.2.3-macos-arm64.dmg` and
  `draftomen-v1.2.3-macos-x86_64.dmg`, signed, notarized and stapled
  compressed read-only images each containing the signed `Draft Omen.app`
  bundle for that architecture and an `Applications` symlink;
- `draftomen-v1.2.3-unsigned-windows.exe`, containing the Windows
  executable; and
- `draftomen-v1.2.3-sha256sums.txt`, containing SHA-256 entries for those
  three binaries.

The release filenames include the tag, and the macOS names include the
architecture. The Windows name keeps `unsigned` until Windows signing lands.
Releases up to 0.4.0 published unsigned macOS DMGs named
`draftomen-v<version>-unsigned-macos-<arch>.dmg` and a checksum file named
`draftomen-v<version>-unsigned-sha256sums.txt`. Releases before 0.4.0
published a single `unsigned-macos.dmg` that ran only on Apple Silicon. Mounting a macOS asset in
Finder or with `hdiutil attach -readonly -nobrowse` shows the app and
Applications shortcut. The Windows executable has no distribution signature.

The native assets are the only published distribution. Releases after 0.4.0
do not publish to PyPI or update the Homebrew tap.

## Independence boundaries

Bundled-baseline embedding is a native packaging concern only: it does not
publish profiles, change website hosting, or add a default URL or startup
network request. Hosted refresh remains explicitly configured and independent.

Baseline updates do not gate release timing, and the native workflow jobs
remain separate.
