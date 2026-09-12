# Set profiles

A set profile is an immutable, versioned artifact containing evidence for one
set and one Limited event format. The profile boundary is in
`draftomen.set_profile`. It is separate from the card database and 17Lands
cache formats; those existing cache files are not migrated or rewritten by
profile loading.

The producer workflow is explicit and reproducible: it reads caller-selected
local inputs, writes a validated compressed artifact plus a generation marker,
and can turn those outputs into a remote manifest record. The profile client
workflow is local-first and network-optional. Native and terminal live commands
use `https://www.draftomen.com/profiles/manifest.json` by default;
`--profile-manifest-url` selects an explicit HTTPS override, and
`--offline-profiles` selects `ProfileNetworkPolicy.OFFLINE` for profile
networking only. Native applications may bundle one validated baseline profile
snapshot as a read-only local fallback, but Draft Omen does not bundle a hosted
profile host or manifest. Live runtime sessions do not load ratings directly
from 17Lands. Producer generation, website publication, and package/native
release workflows remain independent of runtime profile consumption.

For the complete local producer path, run `draftomen-tui refresh-profile-data`.
With no selector it discovers every supported set/format pair represented by a
validated `website/public/card-data/<lowercase-set-code>.json.gz` artifact; pass
an exact case-insensitive set code or full set name to select one set, or use
`--active`/`--historical` to select the corresponding pairs from 17Lands' first-
party `/data/filters` format availability. The supported formats are
`PremierDraft`, `TradDraft`, `QuickDraft`, and `PickTwoDraft`; other 17Lands
formats are ignored. A set selector cannot be combined with either lifecycle
flag. The command prints the deterministic pair list
before fetching ratings, then uses the existing 24-hour cache for one aggregate
full-set card-ratings request and one aggregate color-ratings request per pair.
It never downloads or regenerates Scryfall card data and never makes per-card or
pair-filtered ratings requests.

Each successful pair is generated as an early profile, validated, and published
to `website/public/profiles/objects/<gzip_sha256>.json.gz`; the production
manifest is merged only after object publication, preserving failed or
unrelated entries and all old objects. Identical output leaves website bytes
and modification times unchanged. Every failure is printed with a bounded
category; partial and all-failed runs return status `1`, while a run with no
failures returns `0`. Generated profiles retain the visible attribution
`Card data from 17Lands (17lands.com)`. This command does not provide a
dashboard, regenerate static card data, alter runtime/session networking, run
scheduled jobs, or publish through Cloudflare or GitHub.

## Static card-data artifacts
Set card metadata is a separate schema-versioned (schema version `1`) artifact
from set profiles and from 17Lands statistics. The exporter writes canonical
gzip JSON to
`website/public/card-data/<lowercase-set-code>.json.gz`; the hosted base URL is
`https://www.draftomen.com/card-data/`. Each artifact contains only the
declared set's printed Scryfall card fields, identities, and image data. It
does not contain 17Lands ratings or statistics.

Run `draftomen-tui export-set-data SET` for one exact case-insensitive set code
or full name, or omit `SET` to discover all eligible sets. No-argument
discovery requires at least 200 distinct Arena card identities per set and
excludes paper-only, Cube, Chaos, and Remix sets. Discovery makes one
`/data/expansions` request to 17Lands and acquires Scryfall default-cards data
once. Supplying `--inventory-file PATH` and `--bulk-file PATH` uses local
copies instead. Before publication, all pending candidates are validated; the
all-set command publishes in set-code order and reruns skip only strict valid
siblings, so an interrupted run resumes at the first pending set.

Live mode detects the set from the draft before loading card data. Its selected
artifact is cached at the application-data
`card-data/<lowercase-set-code>.json.gz` path (normally
`~/.draftomen/card-data/<set-code>.json.gz`). A valid cache is reused in
cache-only operation; otherwise the runtime fetches the selected artifact from
the default hosted URL. No card-data network request occurs after
`DraftStartedEvent`.
`--offline-profiles` applies only to hosted set-profile networking. It does not
disable Scryfall card metadata, card images, or static card-data networking;
those sources retain their own cache and network policies.

In the TUI, press `r` to retry a recoverable card-data error. Network repair is
available only before draft start; after `DraftStartedEvent`, retries use the
local cache only.


## Bundled native baseline

Every native application bundle carries the same pinned HOB QuickDraft
metadata-only profile:

| Field | Pinned value |
| --- | --- |
| `set_code` | `hob` |
| `format` | `quickdraft` |
| `profile_version` | `1.0` |
| `maturity` | `metadata-only` |
| `schema_version` | `1` |
| `generated_at` | `2026-09-02T06:18:05.694266+00:00` |
| module resource | `draftomen/baseline_profiles/hob-quickdraft.json` |
| profile bytes | `270` |
| profile SHA-256 | `b95f64f7775cf5c20beb83531062a49bceff695fd9d9fb0e1d4132fce4396dd2` |

The runtime exports these pins as `BUNDLED_PROFILE_SET_CODE="hob"`,
`BUNDLED_PROFILE_EVENT_FORMAT="quickdraft"`, `BUNDLED_PROFILE_BYTES=270`, and
`BUNDLED_PROFILE_SHA256="b95f64f7775cf5c20beb83531062a49bceff695fd9d9fb0e1d4132fce4396dd2"`.
The checked-in payload is canonical UTF-8 JSON with its final newline and
contains `artifact=set-profile`, `provider=draftomen-profile-generator`, and
`revision=1`. It is validated producer output for issue #325, not hand-written
or synthesized at native build time. Producer provenance remains authoritative
for the source URL or path, retrieval timestamp, input and artifact digests,
generator/version, attribution, and licensing; raw rows and local source files
are not bundled.

At runtime the resource resolves relative to the installed `draftomen` module,
never to the current working directory or application-data directory. For an
exact `hob`/`quickdraft` request, loading requires:

1. exactly `270` bytes and the pinned lowercase SHA-256 digest;
2. strict schema-version `1`, identity, metadata, and `metadata-only`
   validation;
3. byte-for-byte canonical re-serialization, with a non-generic result.

The only bundled-resource rejection diagnostics are
`rejected-bundled:missing`, `rejected-bundled:checksum-or-size`, and
`rejected-bundled:invalid`. `load_cached()` orders candidates as valid
non-generic flat cache, valid historical cache (migrated under existing rules),
the matching bundled baseline, a matching `last_valid_profile`, and finally
the generic profile. A corrupt or otherwise rejected baseline therefore cannot
be selected accidentally.

The bundled file is read-only evidence: loading it performs no network access,
never copies it into application data, and never writes, renames, deletes, or
mutates the module resource. A fresh offline install can use it without a
profile-cache entry. The terminal watch default can later fetch a valid newer
profile from the production manifest, while an explicitly configured hosted
manifest can do the same for other clients. Such a profile installs the flat
cache and supersedes the baseline on later loads under the existing
anti-regression rules. Equal identity is `unchanged`; older maturity or
timestamp, and same-timestamp conflicts, are `stale-manifest`. Invalid
manifests or artifacts, network failures, and failed commits retain the usable
profile and never mutate the bundle. The baseline resource itself introduces
no default URL or implicit network activity.

### Baseline ownership and update procedure

Update this baseline only from a validated producer run: preserve provenance,
canonicalize and validate the target identity, record the exact byte count and
SHA-256, and update the identity and digest constants with the resource. Native
packaging must keep both platform mappings pointed at that path and verify final
artifacts. Website hosting and hosted refresh remain a separate boundary.

## Generate producer artifacts

`generate-profile` is the producer-side workflow for generating a profile from
local inputs. It never downloads a card database, ratings, or draft dump. Run
it through the installed terminal entry point:

```sh
uv run draftomen-tui generate-profile \
  --set-code TST \
  --format quickdraft \
  --stage metadata \
  --generated-at 2026-08-30T12:00:00+00:00 \
  --card-database-file "$PWD/.draftomen/inputs/card-database.json" \
  --output-dir "$PWD/.draftomen/set-profiles"
```

The command has these required options:

- `--set-code SET_CODE`: the exact target set code.
- `--format FORMAT`: the exact target event format (for example,
  `quickdraft`).
- `--stage {metadata,early,mature}`: the lifecycle stage to generate. The
  command does not infer or promote a stage from the amount of input data.
- `--generated-at ISO8601`: a timezone-aware ISO-8601 timestamp, such as
  `2026-08-30T12:00:00+00:00`.
- `--card-database-file PATH`: an existing local card-database cache file.
- `--output-dir PATH`: the local directory under which the publication
  directory is created.

The optional input and metadata options are:

- `--ratings-file PATH`: an existing local 17Lands ratings cache file for the
  requested set and format. It is not fetched by this command.
- `--source-manifest PATH`: a local public-dump manifest describing pinned
  local draft data. Its sources must have a local `path` and a SHA-256
  `sha256` pin; a URL-only source cannot be consumed by this workflow.
- `--draft-source-name NAME`: the exact source name to select from the
  manifest. It is required when the manifest contains more than one source
  and is not needed when it contains exactly one.
- `--profile-version VERSION`: the non-empty profile artifact version. It
  defaults to `1.0`.

Set and format inputs are stripped and case-folded for profile, report, and
publication-path identity. A pinned ratings cache must identify the requested
set and format case-insensitively; its stored metadata is retained unchanged.
The set code, format, and every input path are explicit. Keep the card database
and ratings files unchanged when reproducibility matters. A source manifest is
the pin for a draft dump: its `sha256` is the digest of the exact local bytes,
and a relative source `path` is resolved relative to the manifest file. For
example:

```json
{
  "schema_version": 1,
  "sources": [
    {
      "name": "17lands-public-drafts",
      "path": "inputs/tst-drafts.csv.gz",
      "sha256": "REPLACE_WITH_THE_64_HEX_DIGIT_SHA256",
      "retrieved_at": "2026-08-30T11:00:00+00:00",
      "attribution": "Public draft data: 17Lands",
      "license": "Record the terms that apply to this source"
    }
  ]
}
```

Compute the digest over the exact file before replacing the placeholder (on
macOS, `shasum -a 256 "$DUMP_FILE"` prints it). Keep the attribution and
license entries accurate for the source you use; the generator records them
but does not determine or grant rights.

### Lifecycle stages

Use the same explicit set, format, timestamp, card database, and output
directory for each stage. The following commands show the complete progression
for one local input set:

```sh
# 1. No empirical or semantic evidence: metadata-only.
uv run draftomen-tui generate-profile \
  --set-code TST \
  --format quickdraft \
  --stage metadata \
  --generated-at 2026-08-30T12:00:00+00:00 \
  --card-database-file "$PWD/.draftomen/inputs/card-database.json" \
  --output-dir "$PWD/.draftomen/set-profiles"

# 2. Early evidence: use the local ratings cache and one pinned draft source.
uv run draftomen-tui generate-profile \
  --set-code TST \
  --format quickdraft \
  --stage early \
  --generated-at 2026-08-30T12:00:00+00:00 \
  --card-database-file "$PWD/.draftomen/inputs/card-database.json" \
  --ratings-file "$PWD/.draftomen/inputs/tst-quickdraft-ratings.json" \
  --source-manifest "$PWD/.draftomen/inputs/source-manifest.json" \
  --draft-source-name 17lands-public-drafts \
  --output-dir "$PWD/.draftomen/set-profiles"

# 3. Mature evidence: the pinned source must contain accepted decks and
#    Stage C structure targets for every accepted color pair.
uv run draftomen-tui generate-profile \
  --set-code TST \
  --format quickdraft \
  --stage mature \
  --generated-at 2026-08-30T12:00:00+00:00 \
  --card-database-file "$PWD/.draftomen/inputs/card-database.json" \
  --ratings-file "$PWD/.draftomen/inputs/tst-quickdraft-ratings.json" \
  --source-manifest "$PWD/.draftomen/inputs/source-manifest.json" \
  --draft-source-name 17lands-public-drafts \
  --output-dir "$PWD/.draftomen/set-profiles"
```

`metadata` produces a `metadata-only` profile and does not use ratings or
draft rows as profile evidence. `early` produces an `early` profile from
available empirical inputs; the resulting profile must contain empirical
evidence, so generation fails if the supplied inputs cannot provide any.
`mature` requires accepted deck evidence and Stage C structure targets for
every accepted color pair. It fails rather than silently publishing a weaker
profile when those requirements are not met. `semantic-only` is a separate
profile maturity for local loading and is not a `--stage` choice here.

### Publication, validation, and repeatability

For `--set-code TST`, `--format quickdraft`, and the output directory above,
the command writes:

```text
.draftomen/set-profiles/tst-quickdraft/
├── artifacts/
│   └── <gzip_sha256>.json.gz
└── generation.json
```

The exact paths are:

```text
<output>/<set>-<format>/artifacts/<gzip_sha256>.json.gz
<output>/<set>-<format>/generation.json
```

The artifact is the canonical profile JSON compressed with deterministic gzip.
Its filename is the SHA-256 of the compressed bytes, so it is a
content-addressed object. `generation.json` is the authoritative generation
marker: it records the selected set, format, stage, normalized UTC timestamp,
input checksums, source provenance, aggregate counts, profile and gzip
checksums, and sizes. It is the only operation that makes a newly generated
artifact the current generation.

Before publication, the workflow loads only the caller-selected local inputs,
generates the profile, decompresses and parses the gzip artifact, validates the
profile schema and requested set/format, checks canonical serialization, and
reconciles the artifact and report checksums and sizes. It then writes the
content-addressed artifact through a flushed, synchronized temporary file
before atomically replacing the generation marker. An existing artifact is
reused only when its bytes are identical; different bytes at the same
content-addressed path fail.

To regenerate deterministically, repeat the command with identical bytes for
every local input, the same set and format, the same stage and profile version,
and the same explicit timezone-aware `--generated-at` value. Compare the
resulting `gzip_sha256` and `profile_sha256` in `generation.json`; the gzip
checksum must also match the digest in the artifact filename (for example,
`shasum -a 256 <path-from-generation.json>`). The canonical artifact and
generation marker bytes should be identical. If an input or timestamp changes,
the checksums can legitimately change and the prior content-addressed object
is not rewritten.

Successful stdout is aggregate and privacy-safe only: it reports maturity,
input/sample/skip/error counts, `validation=passed`, the artifact path, and
the generation manifest path. It does not print raw rows, source names, card
names, or user identifiers. A workflow failure writes an actionable message
prefixed with `generate-profile:` to stderr and exits with status `1`; it does
not print those data values. Validation failures occur before the marker is
replaced, so the last valid generation remains authoritative. A failed
publication can leave an unreferenced new content object, but it cannot make
an invalid generation the current one.

The successful output uses aggregate key/value lines:

```text
maturity=<metadata-only|early|mature>
input_count=<count>
sample_count=<count>
skip_count=<count>
error_count=<count>
validation=passed
artifact=<artifact-path>
generation_manifest=<generation-json-path>
```

The count values are aggregates from the generation report; the paths point to
the two local publication outputs described above.

### Producer API for a remote manifest

The producer APIs are explicit about the handoff from local generation to
remote publication:

1. `generate_local_profile_artifacts(...)` returns a validated
   `ProfilePublicationResult` containing the compressed artifact, generation
   report, and aggregate counts.
2. `profile_manifest_artifact_from_publication(result, artifact_url)` validates
   that result and converts it to one `ProfileManifestArtifact`. The supplied
   URL is the location where the producer will serve that exact compressed
   artifact.
3. `build_profile_manifest(artifacts, published_at=...)` builds the canonical
   `ProfileManifest` for one or more set/format artifacts.
4. `publish_profile_manifest(path, manifest)` writes the canonical manifest
   atomically. `load_profile_manifest(path)` and `dump_profile_manifest(...)`
   provide strict local manifest I/O.

### Hosted publication boundary (current)

Profile hosting is a static-asset operation owned by repository maintainers,
not profile generation or client startup. Producer and client code do not
upload, schedule, discover, or backfill profile data. Production and
development assets are directories in one Astro/Cloudflare website snapshot:

| Environment | Repository source | Public base | Public manifest |
| --- | --- | --- | --- |
| Production | `website/public/profiles/` | `https://www.draftomen.com/profiles/` | `https://www.draftomen.com/profiles/manifest.json` |
| Development | `website/public/profiles-dev/` | `https://www.draftomen.com/profiles-dev/` | `https://www.draftomen.com/profiles-dev/manifest.json` |

The source/output mapping is mechanical:

```text
website/public/profiles/manifest.json
  -> website/dist/profiles/manifest.json
website/public/profiles/objects/<sha256>.json.gz
  -> website/dist/profiles/objects/<sha256>.json.gz
website/public/profiles-dev/manifest.json
  -> website/dist/profiles-dev/manifest.json
website/public/profiles-dev/objects/<sha256>.json.gz
  -> website/dist/profiles-dev/objects/<sha256>.json.gz
```

Copy only genuine output from the validated local producer workflow, never a
hand-written fixture. Stage generated `artifacts/<gzip_sha256>.json.gz` files
outside `website/public/` until gzip integrity, profile schema, set/format
identity, canonical JSON, and generation metadata pass. Hosted paths contain
no raw rows, source manifests, local paths, draft dumps, or generation
reports. No payload is committed until publication is ready.

#### Master-only atomic deployment

The existing Cloudflare Git integration runs the ordinary Astro static build
only for a commit merged to `master`; that merge is the sole website trigger.
Cloudflare publishes the resulting `website/dist/` through the existing
Wrangler assets configuration as one complete snapshot. It does not upload
objects independently or deploy a partial profile directory, and production
and development cannot have separate publication timing.

There is no profile hook, manual data deployment, branch/preview deployment, or
alternate publication timing. A failed build leaves the last successful
snapshot. Publishing a hosted snapshot does not trigger or gate Python, PyPI,
Homebrew, native packaging, application startup, or release workflows; terminal
runtime consumption of the production manifest is a separate client setting.
Issue #227 discovery, scheduling, backfill, and publication automation remain
excluded; baseline application bundling remains owned by #313.

#### Stage, validate, and publish one transition

Use the explicit producer inputs and commands in
[Generate producer artifacts](#generate-producer-artifacts), staging outside
the public directories. Before merging a publication:

1. Validate every genuine artifact's gzip integrity, profile schema,
   set/format identity, canonical serialization, and generation report.
2. Hash the exact compressed bytes with SHA-256. The lowercase digest must
   equal the object filename and `generation.json`'s `gzip_sha256`; recorded
   `gzip_bytes` and `profile_bytes` must equal the corresponding bytes.
3. Compare complete bytes when a digest path already exists. Reuse identical
   bytes; reject different bytes at that path as a collision and never
   overwrite it.
4. Copy only validated objects to `objects/<sha256>.json.gz`. Build and
   validate each environment's `manifest.json` from those objects in the same
   commit. Every URL must be absolute HTTPS, same-origin with its environment,
   and exactly `<public-base>objects/<gzip_sha256>.json.gz`; URL digest,
   filename, all digest/size fields, and fetched bytes must agree.
5. Prune objects not referenced by the manifest being published. Never prune
   a referenced object or treat a manifest or `profile-smoke/` marker as an
   object.

Objects, both manifests, and ordinary website changes must land in one
master-bound commit: never merge a manifest without its objects or an object
without a manifest entry. A failed build/validation leaves the prior valid
generation authoritative. Roll back by reverting the website change through
the normal review process, not by editing a live file.

#### Focused build and HTTP verification

Validation is temporary and command-driven; no profile-specific test suite
remains. For each publication, stage runtime-created, genuine valid
manifest/object pairs in an isolated website copy, not committed fixtures:

```sh
tmp="$(mktemp -d)"
cp -a website "$tmp/website"
# Stage producer-generated valid pairs in both public/profiles/ and
# public/profiles-dev/ under "$tmp/website".
npm --prefix "$tmp/website" run build
cmp "$tmp/website/public/profiles/manifest.json" \
    "$tmp/website/dist/profiles/manifest.json"
cmp "$tmp/website/public/profiles-dev/manifest.json" \
    "$tmp/website/dist/profiles-dev/manifest.json"
```

For every object named by either manifest, byte-compare its source and
`dist/<environment>/objects/<sha256>.json.gz` files. Validate both manifests
with the existing Python loader:

```sh
uv run python -c '
import sys
from draftomen.profile_manifest import load_profile_manifest
for path in sys.argv[1:]:
    load_profile_manifest(path)
' "$tmp/website/public/profiles/manifest.json" \
  "$tmp/website/public/profiles-dev/manifest.json"
```

Serve the built copy with the existing Wrangler configuration
(`npx wrangler dev --config wrangler.jsonc --local` from that copy), fetch both
manifests and one object per environment, and compare response bytes with the
built files. Check all four cache rules:

| Path | Required `Cache-Control` |
| --- | --- |
| `/profiles/manifest.json` | `public, max-age=0, must-revalidate` |
| `/profiles/objects/<sha256>.json.gz` | `public, max-age=31556952, immutable` |
| `/profiles-dev/manifest.json` | `public, max-age=0, must-revalidate` |
| `/profiles-dev/objects/<sha256>.json.gz` | `public, max-age=31556952, immutable` |

Fetch each public URL (replacing `<sha256>` with a digest named by its
manifest), compare its response body with the built file, and inspect headers:

```sh
for url in \
  https://www.draftomen.com/profiles/manifest.json \
  https://www.draftomen.com/profiles/objects/<sha256>.json.gz \
  https://www.draftomen.com/profiles-dev/manifest.json \
  https://www.draftomen.com/profiles-dev/objects/<sha256>.json.gz
do
  curl --fail --silent --show-error --dump-header - \
    --output /dev/null "$url"
done
```

Manifests revalidate; objects are immutable compressed-byte digests. A changed
object gets a new digest and URL, never a repurposed URL. The smoke namespace
is disjoint and is not a cache-policy test.

#### Retention and legal erasure

The live tree contains only current manifests and referenced objects. Git and
Cloudflare deployment history retain prior snapshots under existing retention
settings for rollback/audit, but archival never overrides erasure. For an
erasure request, remove the object and every reference, merge the clean
snapshot to `master`, remove retained Git history where legally required, and
request purge of Cloudflare edge/deployment copies through existing controls.
Record completion without erased bytes in an issue, log, or new commit. Raw
inputs were never uploaded and remain in local provenance systems.

#### Credentials, roles, and shutdown

Repository maintainers own the existing Cloudflare account, website project,
route, Git integration, access review, incident response, and legal/AUP
decisions. Operators stage assets in a reviewable pull request; reviewers
verify mapping, digest/size/cache invariants, limits, and minimization. No new
Draft Omen hosting secret is needed or permitted.

Public evidence for the existing `cloudflare-workers-and-pages` GitHub App
verifies that its provider-defined manifest currently requests
`administration: write`, `checks: write`, `contents: write`,
`deployments: write`, `pull_requests: write`, and `metadata: read`. This is
not read-only or least privilege for static hosting. The least-practical
repository-access control is selecting the installation only for this
repository; maintainers must verify that selection before publication and stop
publication or correct the access if it is broader. Maintainers review those
actual provider-defined scopes, repository selection, terms, and the
master-only setting at each access review; they must not claim Contents and
Metadata read-only access.

The integration owner rotates or reconnects the existing grant when
authorization expires or ownership changes. Verify repository selection,
provider scopes, master-only trigger, route, and a harmless build, then revoke
or disconnect the prior grant. Revocation uninstalls/disconnects the
integration, removes its repository grant, and disables automatic builds. For
emergency shutdown, pause builds, disable the affected route, and
revoke/disconnect the grant; retain the last good snapshot only when legally
and operationally appropriate. Never print or commit credentials.

Maintainers monitor every master build/deployment, reachability and checksums,
cache headers, file count/largest asset, build minutes/concurrency/timeouts,
route health, retention/erasure work, and legal/AUP reports. A failing check
blocks the website merge, never a package, native release, or startup.

#### Service, size, cost, and data boundary

On the current Cloudflare plan, static requests are free and unlimited. There
is no separate incremental storage charge within 20,000 files per website
version and 25 MiB per asset, so incremental hosting cost is `$0` within those
bounds. Build capacity is 3,000 minutes/month, one concurrent build, and a
20-minute timeout. Maintainers recheck plan terms when the account/provider
changes; these are not availability guarantees.

Keep the root manifest below the client's 1 MiB limit. Objects are expected to
be tens to hundreds of KiB compressed; a complete 2018-onward backfill is
estimated at 5–50 MiB. Expected use is read-only: one small manifest and only
selected immutable objects, with no uploads or raw-corpus traffic. Estimates
are below the host's 25 MiB asset limit and client's 64 MiB compressed-object
and 128 MiB decompressed-profile limits. Raw corpora remain local and are not
in website file-count/cost estimates. Revisit sizes and request volume from
observed data, without raising client limits as a hosting workaround. Cloudflare
terms, AUP, abuse controls, privacy commitments, and availability constraints
apply.

Hosted payloads are compact derived profile JSON and canonical manifests only:
no raw rows, local paths, dumps, account names, or user identifiers. Preserve
source attribution, license, retrieval details, and redistribution permission;
provenance is evidence of review, not a license grant. Do not publish until
terms permit redistribution. The canonical attribution and non-endorsement
rules are in [README branding and compliance](../README.md#branding-and-compliance).
Investigate complaints promptly, honor legal erasure, and keep all hosted paths
free of raw inputs and identifiers.

#### Master-only smoke check

Run this harmless check with two ordinary merges to `master`, touching no
manifest or object path:

1. In the first merge, add only
   `website/public/profile-smoke/<run-id>.txt`, exactly
   `draftomen hosting smoke <run-id>\n` (one final newline). After the complete
   production deployment, fetch
   `https://www.draftomen.com/profile-smoke/<run-id>.txt` and compare bytes.
2. In the next master merge, remove that marker and make no profile asset
   changes. After deployment, retry its URL with bounded delays until it
   returns `404 Not Found`, and record eventual absence.

The smoke check never creates, rewrites, prunes, or validates profile assets,
accepts arbitrary paths or credentials, or exposes credentials. If cleanup or
absence fails, remove only the known marker in the next master merge, redeploy
the complete snapshot, and repeat the bounded absence check before other work.

### Provenance and legal responsibility

The source manifest is part of the reproducible input record. The generation
report retains each selected source's logical name, SHA-256, retrieval
timestamp when supplied, attribution, and license metadata, while omitting
local paths and raw source rows. It also records canonical checksums for the
card database and ratings data. Keep the source manifest with the inputs used
for regeneration.

The operator or producer remains responsible for obtaining source data lawfully,
preserving required attribution, and recording the applicable license or terms
without inventing them. A report's provenance fields are documentation, not a
license grant. The workflow emits compact aggregate/profile artifacts; it does
not publish the source dump or redistribute raw rows.

Historical-pick modeling, benchmark calibration, promotion gates, and other
runtime-integration signals proposed in #88 remain outside this profile
contract.

## Profile input cache foundation

The library-only `draftomen.profile_input_cache` boundary stores immutable
profile-input bytes without downloading or interpreting them. It is deliberately
separate from the set-profile artifact cache. The execute workflow supplies
source adapters, network policy, and CLI defaults around this boundary; the
cache itself has no source adapters, refresh workflow, migration, or
publication behavior. A caller supplies a logical `ProfileInputSource`, an
explicit `source_version`, and a binary stream:

```python
from datetime import timedelta
from draftomen.profile_input_cache import (
    ProfileInputCache,
    ProfileInputCachePolicy,
    ProfileInputSource,
)

cache = ProfileInputCache(
    ".draftomen/profile-input-cache",
    policy=ProfileInputCachePolicy(
        freshness_ttl=timedelta(hours=24),
        max_entry_bytes=64 * 1024 * 1024,
        max_total_bytes=256 * 1024 * 1024,
        max_records=32,
        max_versions_per_source=2,
    ),
)
```

`ProfileInputSource.name` is a normalized safe logical name. Optional
`set_code` is normalized to uppercase and `event_format` is case-folded.
Source identities intentionally cannot carry URLs, local paths, headers, raw
rows, or other free-form metadata. Records contain only that identity, the
source version, UTC acquisition time, SHA-256, and byte count. The object is
stored at `objects/<sha256>.bin`; its canonical sidecar is stored at
`records/<sha256-of-source-and-version>.json`. SHA-256 verifies integrity and
pinning, but is not a signature or authenticity mechanism.

The policy fields are all explicit and positive: `freshness_ttl` controls the
online fresh/stale boundary (`age < freshness_ttl` is fresh and
`age >= freshness_ttl` is stale); `max_entry_bytes` rejects an oversized
stream while it is staged; `max_total_bytes` counts each shared content object
once; `max_records` limits sidecars; and `max_versions_per_source` limits
retained versions for one source. A new entry is rejected if those bounds
cannot be met while retaining one verified entry for every source. Staging is
streamed and temporary, so a transient peak can be one candidate plus existing
objects; failed staging or publication does not replace the prior valid
sidecar. Victim cleanup is recoverable: if cleanup fails after the candidate is
published, the candidate remains within visible bounds and the next cache
operation reconciles the pending cleanup before returning an existing same-version
entry.

`lookup(..., source_version=...)` is exact. Without a version it selects the
newest verified record deterministically by acquisition time, version, and
digest. Online reads return `fresh` or `stale`; `offline=True` returns a
verified entry as `offline-reused`. Missing identities return `missing`, while
malformed metadata, mismatched identity, truncation, checksum failure, and
pin failure return `corrupt`. An offline latest lookup may skip a corrupt
newer candidate and reuse the newest older verified entry. Results and
diagnostics contain no operational paths or input bytes.

`prune()` removes invalid records, orphaned objects, temporary staging files,
and deterministic oldest superseded entries. `invalidate()` can target one
version or all versions for a source; it refuses to remove that source's last
verified offline copy unless `allow_offline_loss=True`. Both return only
deleted-record and deleted-byte counts. Cache mutation methods require a
single writer process. Atomic replacement protects readers, but this boundary
does not add inter-process locks, leases, retries, or stale-lock recovery.

## Card metadata profile inputs

`draftomen.profile_input_acquisition` turns one `PlannedEnvironment` into the
required card-metadata portion of a `ProfileBuildBundle`. The default
`CardMetadataAdapter` downloads Scryfall's default-card bulk data, retains only
cards whose set code matches the planned environment, normalizes them through
the existing `CardDatabase` schema, and stores canonical bytes through
`ProfileInputCache`. Card metadata is keyed by set rather than event format, so
one verified set snapshot can serve Quick Draft and Premier Draft profile
builds.

```python
from draftomen.profile_input_acquisition import acquire_card_metadata_bundle

result = acquire_card_metadata_bundle(
    environment=planned_environment,
    cache=cache,
    offline=False,
)
if result.bundle is not None:
    generator_inputs = result.bundle.generator_inputs()
```

The bundle keeps the normalized `CardDatabase` in memory for the existing
profile generator. Its canonical acquisition report contains only the logical
source identity, source version, UTC acquisition timestamp, SHA-256, byte and
card counts, cache lookup/store outcomes, stable diagnostics, and skip reasons.
It never serializes card rows, image URLs, cache paths, credentials, exception
text, or secrets.

Fresh verified metadata is reused without a network request. `offline=True`
uses the newest verified cache entry and never invokes the adapter. A stale but
verified entry remains usable when an online refresh fails, with explicit
`stale` and `card-metadata-refresh-failed` reporting. Missing offline metadata,
corrupt content, an unavailable source, or a cache failure returns a result
without a bundle and with a bounded outcome; freshly fetched bytes never bypass
the cache when publication fails.

`acquire_card_metadata_bundle` supplies only the metadata needed for a
metadata-only profile build. It does not acquire empirical evidence, select a
generation stage, execute a refresh plan, publish artifacts, add a CLI, or
change the runtime card database.

## Optional 17Lands ratings profile inputs

`acquire_profile_build_bundle` composes the required metadata acquisition with
an optional format-scoped `SeventeenLandsRatingsAdapter`. The ratings adapter
uses the existing normalized `SeventeenLandsFormatData` contract, validates it
for the planned set and event format, and stores canonical JSON through the
same bounded profile-input cache.

```python
from draftomen.profile_input_acquisition import acquire_profile_build_bundle

result = acquire_profile_build_bundle(
    environment=planned_environment,
    cache=cache,
    offline=False,
)
if result.bundle is not None:
    generator_inputs = result.bundle.generator_inputs()
```

Ratings cache identities include both set and event format. Source reports
record the normalized rating-row count and the sum of games-in-hand samples,
along with source version, UTC acquisition timestamp, digest, byte count, cache
outcomes, diagnostics, and skip reasons. They never retain rating rows, card
names, local paths, credentials, exception text, or secrets.

Fresh ratings are reused without network access. `offline=True` reuses the
newest verified entry, and a verified stale entry remains usable if its online
refresh fails. Missing, corrupt, unavailable, or uncacheable ratings produce a
bounded source outcome while preserving the valid metadata-only bundle.

## Optional 17Lands public-draft inputs

The same `acquire_profile_build_bundle` call also invokes a format-scoped
`SeventeenLandsPublicDraftAdapter`. The default adapter downloads the preferred
17Lands public draft dump for the requested set and event format, verifies that
every yielded row uses the supported generator schema and requested
environment, and streams the exact dump bytes through `ProfileInputCache`.
The resulting bundle supplies a pinned one-source `PublicDumpManifest` to the
existing profile generator; the source path points only to the verified cache
object and is never serialized in the acquisition report.

Public-draft reports record the logical source, adapter version, acquisition
timestamp, digest, byte count, cache lookup/store outcomes, stable diagnostics,
and available draft-row count. They do not contain URLs, cache or staging
paths, raw rows, draft identifiers, card names, credentials, exception text, or
secrets. The reader recognizes gzip content in the cache's content-addressed
`.bin` objects, so the same verified bytes can be consumed by profile
generation without renaming or copying them.

Metadata remains required, while ratings and public drafts have independent
bounded outcomes. A ratings failure can therefore retain public-draft evidence,
and a public-draft failure can retain ratings. If neither empirical source is
available, the valid metadata-only bundle remains usable. `offline=True`
reuses each source's newest verified cache entry without invoking its adapter;
a verified stale public dump remains usable when refresh fails, while missing,
corrupt, mismatched, unavailable, or uncacheable dumps produce source-specific
skip reasons without suppressing other valid inputs.

Acquisition records evidence availability but does not infer an early or mature
stage, run generation, publish artifacts, or retain source rows in memory after
validation. Callers make lifecycle decisions from the deterministic rating and
draft-row availability reports.

## Plan profile refreshes

`plan-profile-refresh` is a dry-run producer command. It selects environment
identities (`set_code` plus the explicitly supplied `--event-format`) from the
current 17Lands expansion inventory, but it never generates or publishes a
profile. The inventory is the sole eligibility source; there is no application
set whitelist.

The command uses the network-backed
`https://www.17lands.com/data/expansions` inventory by default. For reproducible
offline runs, `--inventory-file` accepts that endpoint's exact JSON list shape:

```json
["TST", "NEW", "OLD"]
```

Lifecycle is a separate, explicit operator input. 17Lands does not publish
Arena rotation windows, so Draft Omen does not infer dates, order, or
availability from the inventory. Operators must derive lifecycle assignments
from an authoritative Arena schedule and record the provider, source URL, and
version in a local or URL-supplied document:

```json
{
  "provider": "Arena schedule",
  "source_url": "https://schedule.example.test/arena.json",
  "version": "2026-08-30",
  "active": ["NEW"],
  "mature": ["TST"],
  "historical": ["OLD"]
}
```

The stage lists may instead be represented by `environments` (or `records`)
objects with `set_code` and `lifecycle` fields. Missing, malformed, duplicate,
conflicting, or inventory-unknown lifecycle entries are retained as stable
diagnostics. They do not discard valid 17Lands inventory entries.

Select exactly one planning mode and always provide an event format:

```sh
# One known inventory environment (manual selection).
uv run draftomen-tui plan-profile-refresh \
  --set-code NEW --event-format PremierDraft \
  --inventory-file "$PWD/inputs/expansions.json" \
  --lifecycle-file "$PWD/inputs/arena-lifecycle.json" \
  --output-plan "$PWD/refresh-plan-manual.json"

# Every environment explicitly classified active.
uv run draftomen-tui plan-profile-refresh \
  --active --event-format PremierDraft \
  --inventory-file "$PWD/inputs/expansions.json" \
  --lifecycle-file "$PWD/inputs/arena-lifecycle.json" \
  --output-plan "$PWD/refresh-plan-active.json"

# At most two explicitly historical environments.
uv run draftomen-tui plan-profile-refresh \
  --history --max-environments 2 --event-format PremierDraft \
  --inventory-file "$PWD/inputs/expansions.json" \
  --lifecycle-file "$PWD/inputs/arena-lifecycle.json" \
  --output-plan "$PWD/refresh-plan-history.json"
```

With `--dry-run`, the command prints canonical compact JSON with sorted keys,
deterministic environment order and reasons, and one trailing newline.
`--output-plan` writes the same bytes atomically.
A history selection includes only entries classified `historical` and applies
the explicit bound; an active selection includes only entries classified
`active`. A manual selection requires the set code to be in the 17Lands
inventory, while its lifecycle classification remains explicit metadata (and
may be reported as unknown). No mode infers a lifecycle stage.

## Execute a profile refresh

`execute-profile-refresh` consumes one canonical plan produced by
`plan-profile-refresh`; it does not rediscover inventory, interpret lifecycle
dates, generate a profile, or publish a profile manifest. Save the bytes from
`plan-profile-refresh --output-plan` (or the canonical JSON printed by
`--dry-run`) and hand that file to the executor:

```sh
uv run draftomen-tui plan-profile-refresh \
  --set-code TST --event-format PremierDraft \
  --inventory-file "$PWD/inputs/expansions.json" \
  --lifecycle-file "$PWD/inputs/arena-lifecycle.json" \
  --output-plan "$PWD/inputs/refresh-plan.json"

uv run draftomen-tui execute-profile-refresh \
  --plan "$PWD/inputs/refresh-plan.json" \
  --cache-dir "$PWD/.draftomen/profile-input-cache" \
  --output-dir "$PWD/.draftomen/profile-refresh"

# Strictly no-network replay from verified cache entries:
uv run draftomen-tui execute-profile-refresh \
  --plan "$PWD/inputs/refresh-plan.json" \
  --cache-dir "$PWD/.draftomen/profile-input-cache" \
  --output-dir "$PWD/.draftomen/profile-refresh-offline" \
  --offline
```

The executor strictly loads the plan with `load_refresh_plan` (including its
canonical schema, ordering, identity, and size checks), constructs
`ProfileInputCache(cache_dir, policy=DEFAULT_PROFILE_REFRESH_CACHE_POLICY)`,
and processes environments sequentially in plan order. The default cache policy
is exactly `freshness_ttl=7 days`, `max_entry_bytes=128 * 1024 * 1024`
(`134,217,728` bytes), `max_total_bytes=512 * 1024 * 1024`
(`536,870,912` bytes), `max_records=256`, and
`max_versions_per_source=3`.

The output layout is:

```text
<output-dir>/
├── bundles/
│   └── <bundle_id>/
│       ├── objects/
│       │   └── <sha256>.bin
│       └── bundle.json
└── execution.json
```

Each role object is at the exact relative path
`bundles/<bundle_id>/objects/<sha256>.bin`; `bundle.json` and
`execution.json` are the bundle and run authorities for those objects.

Every planned environment contributes exactly one result in plan order.
`bundle.json` is canonical JSON (sorted keys, compact separators, one final
newline) with exactly these top-level fields:

`schema_version`, `executor_version`, `bundle_id`, `plan_sha256`, `mode`,
`environment`, `outcome`, `inputs`, `sources`, and `skip_reasons`.
`environment` contains exactly `event_format`, `lifecycle`, `reasons`, and
`set_code`. `inputs` and `sources` each contain exactly the roles
`card_database`, `ratings`, and `public_drafts`. An input is null when that
role is unavailable; otherwise card metadata and ratings contain exactly
`source_name`, `sha256`, and `content_bytes`, while public drafts contain
exactly `source_name`, `sha256`, `content_bytes`, `attribution`, and `license`.
`source_name` is the bounded logical `ProfileInputSource.name`, never a path or
URL, and must equal the corresponding parsed source report. Each source report
contains exactly `acquired_at`, `acquisition_outcome`, `cache_lookup_outcome`,
`cache_store_outcome`, `content_bytes`, `diagnostics`, `sample_availability`,
`sha256`, `source`, and `source_version`. `source` contains exactly
`event_format`, `name`, and `set_code`; sample availability is exactly
`card_count`, `rating_rows` plus `rating_samples`, or `draft_rows`
according to the role. Digests, sizes, source versions, UTC acquisition
timestamps, cache outcomes, bounded diagnostics, and stable skip reasons are
recorded only when permitted by those schemas.

`execution.json` and `ProfileRefreshExecutionResult.to_bytes()` have identical
canonical bytes. Their exact top-level fields are `schema_version`,
`executor_version`, `plan_sha256`, `mode`, `counts`, and `environments`.
`counts` contains exactly `failed`, `metadata_only`, `planned`, and `staged`.
Each environment entry contains exactly `available_input_roles`, `bundle_id`,
`environment`, `outcome`, and `skip_reasons`; the aggregate counts are
reconciled from those entries. The command writes these bytes to stdout and
also atomically writes them to `<output-dir>/execution.json`.

Staging writes and verifies each content-addressed object before writing
`bundle.json`; a temporary prepared bundle is atomically installed only after
its authority is complete. Successful staging removes unreferenced temporary
objects before commit. The loader verifies every object referenced by `inputs`
and ignores unreferenced object entries, so extra bytes cannot affect the
reconstructed bundle; safe cleanup belongs to successful staging and
post-commit handling. Use one executor writer for a given cache and output
directory: cache mutations require a single writer process, and no
inter-process lock, lease, retry, or stale-lock recovery is provided. A
prepared successful bundle atomically replaces any current bundle only after
it is complete, including online-to-offline reruns. Content-addressed object
bytes are immutable, but bundle authority is current-run state. A required
failure can replace the current authority with a failed marker; if that
replacement fails, the prior authority remains. A failed authority is not
loadable. Cache object retention and orphan cleanup are governed separately by
`prune()` and the invalidation rules above.

Use `load_staged_profile_build_bundle(<bundle-directory>)` to verify object
digests, sizes, canonical models, role pins, and the environment, then pass
`bundle.generator_inputs()` to the unchanged explicit generator:

```python
from draftomen.profile_generation import generate_set_profile
from draftomen.profile_refresh_execution import load_staged_profile_build_bundle

bundle = load_staged_profile_build_bundle(bundle_directory)
generated = generate_set_profile(
    **bundle.generator_inputs(),
    stage="metadata",       # choose "early" or "mature" explicitly when eligible
    generated_at=fixed_timestamp,
)
```

Execution never chooses or falls back to a generation stage and never calls
generation. Metadata-only bundles are valid. Missing, corrupt, unavailable,
or independently failed ratings and public-draft inputs become null optional
roles with source outcomes and stable skip reasons; a valid required card
object still stages and sibling environments continue. If required card
acquisition or staging fails, that environment receives a failed authority and
the remaining environments still run. Exit status is `0` only when every
environment is staged (including metadata-only and optional degradation), and
`1` for any failed environment or bounded plan, cache, execution, or authority
error. Argparse syntax errors retain exit status `2`. Per-environment failures
still emit the complete execution result; bounded errors never expose paths or
raw exception text.

Without `--offline`, fresh verified cache entries are reused; stale entries
receive at most one adapter attempt, and a verified stale entry remains usable
when that attempt fails. Missing or corrupt entries are refreshed online when
possible; otherwise the role is unavailable. With `--offline`, no adapter is
invoked and no network is accessed: only verified entries, including stale
ones, may be reused. Missing or corrupt required metadata fails that
environment; missing or corrupt optional roles are skipped. Repeating an
online run with the same canonical plan and cache offline therefore makes no
fetch calls and preserves the verified input object digests and, for the same
explicit generator stage and fixed `generated_at`, generator bytes. The
offline authority records `mode=offline`, so authority metadata need not be
byte-identical.

Each source is attempted at most once per environment and execution is
sequential; adapters perform no retries or backoff. The default adapters pass
the positive 60-second request timeout. A complete cache-miss environment
uses at most two Scryfall requests for card metadata, two 17Lands requests for
ratings, and one 17Lands public-draft download; cache hits reduce that bound.
Operators must observe provider rate limits and terms. The cache bounds input
storage to one shared object per digest, at most 128 MiB per object, 512 MiB
total, 256 records, and three versions per source; staged output adds one
verified object per available role and environment.

The authorities, execution output, and diagnostics are privacy-safe: they
contain no local or relative paths, URLs or headers, credentials or secrets,
exception text, raw source rows or row values, draft identifiers, or card
names. Within the staged bundle, public-draft row bytes appear only in the
pinned input object needed by the generator; the loader reconstructs its
runtime path from the bundle directory and never reads a serialized path.
Keep that object and the profile-input cache as local raw evidence: the
executor does not publish either one. Public-draft authority records the
required attribution and license (the default adapter uses `Public draft data:
17Lands` and `CC BY 4.0`). These fields are provenance, not a rights grant:
operators must verify applicable 17Lands terms, preserve attribution and
license notices, and obtain any permissions required for redistribution. The
executor is strictly no-network in offline mode and explicitly excludes
profile generation and remote publication.

## Run a local profile-refresh batch

The operator workflow has three separate commands: plan the environments,
execute the plan to stage verified input bundles, then generate the batch from
that staged execution. The plan is the authority for manual, active, or
bounded-history selection; execution and batch generation do not rediscover
inventory or lifecycle metadata. Keep the inventory, lifecycle document,
plans, cache, and staged output on local storage. Supplying
`--inventory-file` and `--lifecycle-file` keeps planning local as well; without
those options, planning may use its network-backed inventory or lifecycle URL
inputs.

The three local plan modes can be prepared as separate canonical plan files:

```sh
PLAN_DIR="$PWD/.draftomen/profile-plans"
CACHE_DIR="$PWD/.draftomen/profile-input-cache"
STAGED_DIR="$PWD/.draftomen/profile-refresh"
GENERATED_AT="2026-08-30T12:00:00+00:00"

mkdir -p "$PLAN_DIR" "$CACHE_DIR"

# Manual: one known inventory environment.
uv run draftomen-tui plan-profile-refresh \
  --set-code NEW --event-format PremierDraft \
  --inventory-file "$PWD/inputs/expansions.json" \
  --lifecycle-file "$PWD/inputs/arena-lifecycle.json" \
  --output-plan "$PLAN_DIR/manual.json"

# Active: every inventory environment explicitly classified active.
uv run draftomen-tui plan-profile-refresh \
  --active --event-format PremierDraft \
  --inventory-file "$PWD/inputs/expansions.json" \
  --lifecycle-file "$PWD/inputs/arena-lifecycle.json" \
  --output-plan "$PLAN_DIR/active.json"

# Bounded history: at most two explicitly historical environments.
uv run draftomen-tui plan-profile-refresh \
  --history --max-environments 2 --event-format PremierDraft \
  --inventory-file "$PWD/inputs/expansions.json" \
  --lifecycle-file "$PWD/inputs/arena-lifecycle.json" \
  --output-plan "$PLAN_DIR/history.json"
```

`--dry-run` prints the same canonical plan bytes instead of writing
`--output-plan`; use the written form when handing the plan to the next
command. The local lifecycle document must classify entries used by active or
history selection explicitly. Manual selection still requires its set code to
be in the inventory; its lifecycle classification may be unknown. Active
selection includes only entries classified `active`, and history selection
includes only entries classified `historical` up to the positive
`--max-environments` bound.

Execute each plan into its own staged directory. The optional `--offline`
switch below is an execution switch, not a batch switch: it requires verified
cache entries and prevents acquisition or network access. Omit it when the
executor is allowed to acquire missing inputs, then retain the resulting cache
and staged authority for later local replays.

```sh
# The first run must omit --offline so the executor can acquire and cache the
# inputs; add --offline only for later cache-only replays.
for mode in manual active history; do
  uv run draftomen-tui execute-profile-refresh \
    --plan "$PLAN_DIR/$mode.json" \
    --cache-dir "$CACHE_DIR" \
    --output-dir "$STAGED_DIR/$mode"
done
```

Finally, run the read-only batch generation command once for each staged
execution:

```sh
for mode in manual active history; do
  uv run draftomen-tui generate-profile-refresh-batch \
    --plan "$PLAN_DIR/$mode.json" \
    --staged-dir "$STAGED_DIR/$mode" \
    --generated-at "$GENERATED_AT"
done
```

`--generated-at` is required and must be timezone-aware ISO-8601. Batch
generation uses the existing fixed profile artifact version default, `1.0`.
Batch generation reads only the canonical plan and the existing staged
execution authority and bundles. It
does not acquire inputs, access the profile-input cache, invoke adapters, or
access the network, and it has no `--offline` option. It also does not run
GitHub Actions, publish a profile or manifest, or write artifacts or reports.
Eligible validated profile, gzip, and generation-report payloads remain in
process memory for later in-process callers; the staged directory is unchanged.

Stage selection is performed independently for each staged environment from
its available ratings and public-draft evidence. A batch may therefore contain
metadata, early, and mature profiles together; lifecycle metadata never
promotes a generation stage.

The batch command writes one canonical, compact JSON report to stdout, with
sorted keys and one trailing newline. Its top-level report contains the batch
`schema_version`, the plan SHA-256, the plan `selection_mode`, the staging
run's `execution_mode` (`online` or `offline`), the aggregate `counts`,
ordered per-environment results, and a `versions` object holding
`generator_version`, `profile_generation_schema_version`,
`profile_generation_execution_schema_version`, `set_profile_schema_version`,
`public_dump_manifest_schema_version`, and `statistics_version`. Each
publication-eligible environment includes its selected stage and bounded
observed samples, safe logical source metadata for every input that produced
the profile (name and digest for the card-database and ratings inputs, plus
attribution and license for public-draft sources), card-game and pair-game
counts, skip/error totals and reason counts, and profile, gzip, and
generation-report SHA-256 and byte-size pairs. Failed environments retain
their safe identity and finite outcome, phase, and reason (and any bounded
stage selection); environments the refresh executor never staged are labeled
`refresh-execution` / `refresh-execution-failed` and carry the executor's
recorded skip-reason counts. One failure does not discard eligible sibling
results. Results remain in canonical plan order.

The report is privacy-safe: it contains no raw datasets or payload bytes,
secrets, local paths, source URLs or retrieval metadata, exception text,
diagnostics, card names, generated timestamp, or profile version. Raw input
objects remain local evidence in the cache and staged bundles; batch generation
does not publish or copy them. A complete report is emitted for per-environment
failures and the command exits `0` only when every planned environment is
`publication-eligible`; any failed environment exits `1`. Fatal plan,
authority, or batch-input errors emit only generic, path-free stderr and exit
`1` (argument syntax errors retain `2`).

For deterministic repeats, keep the canonical plan bytes, staged input bytes,
generator configuration, stage thresholds, and timezone-aware `--generated-at`
unchanged. Repeating the batch then produces byte-identical report bytes and
identical eligible payload bytes; the report itself does not include
`generated_at` or `profile_version`.


## Run the CI profile-refresh workflow

`.github/workflows/profile-refresh.yml` has two deliberately separate stages:
read-only generation and guarded publication. Generation checks out the requested
commit, discovers missing or invalid static card-data artifacts, refreshes the
selected profile pairs, and uploads one run-evidence bundle. It never commits,
pushes, creates a branch, creates a pull request, or deploys a website. The
publication job runs only for a non-cancelled run on `master`, and consumes the
exact artifact ID uploaded by that generation attempt; it does not download a
latest-name match or rerun generation.

Generation has only `contents: read` permission and checkout credential
persistence is disabled. Publication uses the built-in `GITHUB_TOKEN` only, with
job-scoped `actions: read`, `contents: write`, and `pull-requests: write`
permissions. The workflow defaults remain read-only. No personal access token,
GitHub App credential, deployment credential, or stored login is used. The
publisher validates the bundle in a disposable worktree based on the current
master commit, then uses an exact master compare-and-swap (CAS); it does not
bypass branch protections.

### Publication outcomes

The report and the complete generated delta are validated before any branch,
pull-request, or master write.
A full-success report has no failures. Any changed successful static card data,
including a static-only change, is eligible for the same automatic fast-forward
as changed successful profile data. The publisher creates a ready snapshot PR
whose body uses the required `## Summary`, `## Changes`, `## Scope notes`, and
`## Verification` template sections; the generated producer summary may add
subsections. It includes the workflow URL, generation and checked-master SHAs,
validated asset paths, selected and successful work, provider attribution, and
every failure. For a full success, it fast-forwards `master` to that PR's one
validated commit. GitHub then records the PR as
merged when the head becomes reachable from `master`; the publisher does not
invoke `gh pr merge`.


| Generation result | Snapshot PR | `master` | Exit |
| --- | --- | --- | --- |
| Full success with changed static and/or profile data | Ready PR, then automatic fast-forward and indirect merge | Updated once by the exact CAS | `0` |
| Partial success, including static success with failed profile work or profile success with failed static work | One immutable ready PR left open for review | Unchanged | `1` |
| Valid success with no changed assets, including zero selected work and already-valid static data | None | Unchanged | `0` |
| All-failed result: status `failed` with no successful static code or profile pair | None | Unchanged | `1` |
| Invalid evidence or stale protected data | No branch, PR, or master write; any existing reviewed PR is left untouched | Unchanged | `1` |
| Publication error or a rejected CAS | A snapshot branch or PR may already exist; no master write unless a prior CAS succeeded | Unchanged unless a prior CAS already succeeded | `1` |

An empty delta does not by itself mean all-failed: a valid `success` report with
an empty delta is unchanged, while a `failed` report with an empty delta exits
`1` without a PR. Reserve all-failed for status `failed` with no successful
static code or profile pair. Nonempty assets without corresponding successful
work are inconsistent evidence and are rejected. Every failure is retained in
the report, the generated PR body, and the Actions summary; a partial PR is
never silently promoted to a full success.

### Schedules and dispatch

GitHub Actions evaluates both schedules in UTC:

| Schedule | Selection |
| --- | --- |
| `17 6 * * *` | `active` every day |
| `47 6 * * 0` | `historical` on Sundays |

Both schedules perform missing-only static discovery before profile generation.
The workflow never infers a schedule selection for a manual run. Manual
dispatch exposes these inputs:

| Input | Values |
| --- | --- |
| `selection_mode` | `one`, `all`, `active`, or `historical`; defaults to `all` |
| `set` | Optional exact set code or full name; required only when the mode is `one` |

`set` is rejected for `all`, `active`, and `historical`. A one-set profile
selection does not narrow static discovery: discovery always uses the complete
inventory so every missing or invalid card-data artifact can be reported and
generated without rewriting an already-valid sibling.

The profile selection is derived from the current 17Lands `/data/filters`
availability. `all` selects every supported available pair, `active` selects
available and live pairs, and `historical` selects available pairs that are not
live. The supported formats remain `PremierDraft`, `TradDraft`, `QuickDraft`,
and `PickTwoDraft`; other formats are ignored. The helper receives the full
checked-out `github.sha` as `base_commit`, which identifies the source used for
generation.

For a manual run from the command line, use the same four-mode contract as the
workflow form:

```sh
gh workflow run profile-refresh.yml \
  --ref master \
  -f selection_mode=one \
  -f set=TLA
```

Omit `-f set=...` for `all`, `active`, or `historical`. The scheduled event
mapping is fixed by the cron expression; an unknown schedule value fails rather
than silently selecting a different mode. `TLA` is an existing set code; use
another exact code or full name when needed.

### Publication safety and immutable snapshots

The protected generated roots are exactly:

```text
website/public/card-data
website/public/profiles
```

Before staging, the publisher resolves the trusted generation `base_commit`,
requires it to be an ancestor of the checked master, and compares the *whole*
protected roots, not merely paths listed in the artifact:

```sh
git diff --quiet EXPECTED_BASE MASTER_COMMIT -- \
  website/public/card-data website/public/profiles
```

Any changed, added, deleted, or type-changed path in either root is stale and
aborts publication. Unrelated master changes are allowed and remain in the
candidate because the candidate starts from that current master tree. A
successful publication commit contains only the fully validated descriptor
paths, is regular non-executable data, has exactly one parent, and has that
captured master commit as its parent.

Each publication branch is immutable and unique to its source evidence:

```text
automation/profile-refresh-<run-id>-<artifact-id>
```

The artifact ID comes from the generation upload, not from report metadata.
The publisher queries all PRs for the exact branch and `master` base. A
same-artifact rerun reuses and verifies its existing snapshot rather than
replacing the branch or creating a rolling branch. A concurrent branch
creation is a collision, not permission to update an existing ref. Maintainer-
changed branches and closed-unmerged PRs are conflicts; a maintainer-closed PR
is never reopened.

The final full-success write is an exact remote lease using the validated commit
SHA, not a movable branch:

```sh
git push \
  --force-with-lease=refs/heads/master:MASTER_COMMIT \
  origin VALIDATED_HEAD_SHA:refs/heads/master
```

If `master` advances, even with an unrelated change, the lease rejects and the
publisher does not retry against a new base. After a successful lease, it
fetches master and checks reachability, then reads the PR state. If GitHub has
not yet confirmed the indirect merge, the factual outcome is
`master updated; merge confirmation unavailable` with the PR URL. No second
merge operation is attempted. The uploaded artifact and its evidence are
retained for seven days.

### Re-runs, stale data, and source outages

Use the exact run and artifact as follows:

* When generation succeeded but publication failed, `gh run rerun RUN_ID
  --failed` retries the failed publication with the same artifact ID and
  immutable snapshot. It does not repeat the merge. If that snapshot is
  already reachable from `master`, the retry only records the merged state or
  reports `master updated; merge confirmation unavailable`; it never creates a
  duplicate PR.
* A complete all-job rerun, or a fresh workflow dispatch, generates a new
  artifact ID and may create a new partial snapshot. If generation itself
  failed, a failed-job rerun regenerates it; use the fresh evidence rather than
  treating the old bundle as authoritative.
* An expired or missing seven-day artifact requires a new full dispatch. Do not
  substitute a latest artifact or rerun with guessed metadata.
* After a Scryfall or 17Lands outage, rerun after the source recovers. An
  incomplete report is not zero work, and an outage must not be reinterpreted
  as an unchanged success.
* A stale-base or failed master CAS requires a new run from the current
  `master`, not a publication-only retry of the old base. Review or close any
  superseded partial snapshot manually; retain its successful data evidence and
  all recorded failures. The publisher never overwrites a reviewed partial PR.

To inspect a run and its evidence:

```sh
gh run view RUN_ID --json url,status,conclusion,jobs
gh run download RUN_ID \
  --name "profile-refresh-generation-RUN_ID-RUN_ATTEMPT" \
  --dir "$PWD/.draftomen/profile-refresh-evidence"
```

The artifact layout is:

```text
result.json
summary.md
generated/website/public/card-data/<set>.json.gz
generated/website/public/profiles/manifest.json
generated/website/public/profiles/objects/<sha256>.json.gz
```

Only successful changed assets are copied into `generated/`; caches, planning
directories, old unrelated objects, and orphan objects are excluded. The
publisher uses the producer's rendered summary and includes every selected
item, successful item, attribution, validated path, checked-master SHA,
generation SHA, workflow URL, and failure in the PR and Actions summary. The
uploaded Markdown is display evidence only and cannot authorize publication.

### Provider, cache, and attribution policy

Static discovery uses the existing Scryfall card-data exporter and its
configured inventory/bulk sources. A valid existing
`website/public/card-data/<set>.json.gz` is not rewritten. Discovery may still
read and validate the complete source before classifying candidates; this is
why a run can report a source outage even when no static file needs changing.

Profile generation uses the existing aggregate 17Lands provider and its
runner-temporary cache. A cache entry is fresh for 24 hours. For each cache
miss, the producer makes one aggregate full-set card-ratings request and one
aggregate color-ratings request per selected pair; it does not make per-card or
pair-filtered requests. Requests are sequential, use positive 60-second
timeouts and an identifying user agent, and do not add retries, backoff, or an
invented numeric throttle. A fresh cache hit avoids those ratings requests.
The cache is discarded with the runner and is never uploaded or reused through
`actions/cache`.

Generated profiles retain the visible attribution `Card data from 17Lands
(17lands.com)`. Provider outages, stale or unusable cache data, timeout
failures, and malformed filter responses remain categorized failures in the
report; they are not converted into successful empty profiles.

### Required Actions setting and rollback

Before enabling publication, an owner must enable **Settings → Actions →
General → Allow GitHub Actions to create and approve pull requests**. Keep the
default workflow token permissions read-only; the workflow grants write
permissions only to the publication job. The publisher creates ready PRs but
never approves them and does not need the repository `allow_auto_merge` setting:
the validated single-parent commit is fast-forwarded with the master CAS.

To roll back generated data, revert the generated PR's single commit through
the normal reviewed PR process. Revert the manifest and its associated static
card-data and profile-object changes together; never edit hosted files directly
or remove only one content-addressed object.

PR checks created with `GITHUB_TOKEN` may await maintainer approval, and pushes
made with that token do not trigger another Actions run. This automation does
not wait for those checks, bypass branch protections, or claim that an existing
Cloudflare integration has deployed the website. Deployment and external-host
recovery are outside this procedure.

### Re-run the helper locally

Run the generation helper from a dedicated checkout, such as a disposable
worktree with no unrelated edits. This exercises read-only generation only; it
does not create publication branches or PRs:

```sh
REPO_ROOT="$(git rev-parse --show-toplevel)"
BASE_COMMIT="$(git rev-parse HEAD)"
TEMP_DIR="${TMPDIR:-/tmp}/draftomen-profile-refresh-local"
EVIDENCE_DIR="$TEMP_DIR/evidence"
CACHE_DIR="$TEMP_DIR/cache"
BUNDLE_DIR="$EVIDENCE_DIR/bundle"
mkdir -p "$EVIDENCE_DIR" "$CACHE_DIR"
cd "$REPO_ROOT"

generation_status=0
if uv run python "$REPO_ROOT/scripts/profile_refresh_workflow.py" generate \
  --selection-mode one \
  --set TLA \
  --base-commit "$BASE_COMMIT" \
  --bundle-dir "$BUNDLE_DIR" \
  --cache-dir "$CACHE_DIR" \
  --repo-root "$REPO_ROOT"
then
  :
else
  generation_status=$?
fi

printf 'helper exit status: %s\n' "$generation_status"
cat "$BUNDLE_DIR/result.json"
cat "$BUNDLE_DIR/summary.md"
exit "$generation_status"
```

The helper writes `result.json` and `summary.md` before returning a failure
status whenever it can persist evidence. Re-running this local helper with the
same declared base and dedicated bundle replaces that local evidence; it is
not a publication retry and it does not make an uploaded artifact current.

## Select a profile-generation stage

After staging, load the bundle and select a stage from its role-keyed source
reports. The selector consumes only `bundle.ratings_source` and
`bundle.public_draft_source`; it does not read raw rows or local paths.

The batch CLI performs this selection and the validated generation for every
staged environment; the following is the equivalent lower-level library path
for callers that need to inspect one bundle.

```python
from draftomen.profile_generation_stage_policy import select_profile_generation_stage
from draftomen.profile_refresh_execution import load_staged_profile_build_bundle

bundle = load_staged_profile_build_bundle(bundle_directory)
selection = select_profile_generation_stage(
    ratings_report=bundle.ratings_source,
    public_draft_report=bundle.public_draft_source,
)
stage = selection.stage.value
```

The positive defaults are one rating row, one games-in-hand rating sample, and
one validated public-draft row. In exact terms, `early` is selected only when
the pinned ratings report has `rating_rows >= early_rating_rows` **and**
`rating_samples >= early_rating_samples`. `mature` is selected only when that
early predicate is met **and** the pinned public-draft report has
`draft_rows >= mature_draft_rows`. Mature is checked before early. A complete
report that does not meet the early predicate selects `metadata`; lifecycle
metadata does not promote a stage.

Thresholds are configurable through the immutable
`ProfileGenerationStageThresholds` value passed as `thresholds=`. Its
`early_rating_rows`, `early_rating_samples`, and `mature_draft_rows` fields
must each be positive integers. The default value is
`DEFAULT_PROFILE_GENERATION_STAGE_THRESHOLDS`; callers should retain the
thresholds used for each selection.

`selection.to_json()` is a deterministic record with exactly these top-level
keys: `stage`, `thresholds`, `observed_availability`, and `rationale`.
`thresholds` records the early rating-row/sample predicates and the mature
rating-row/sample plus draft-row predicates. `observed_availability` records
only `ratings_available`, `rating_rows`, `rating_samples`,
`public_drafts_available`, and `draft_rows`. The record contains bounded
counts and rationale only: it omits source names, digests, URLs, local paths,
raw rows, and row values.

For optional evidence, `None` means that role is unavailable; with both role
reports `None`, selection is explicitly `metadata` with the
`no-empirical-evidence` rationale. Complete zero counts do not satisfy the
positive thresholds and never upgrade a stage. Partial or ambiguous supplied
reports (including incomplete pins, missing paired rating counts, or counts
for the wrong role) raise a bounded `ProfileGenerationStagePolicyError`
instead of falling back to another stage.

Selection is a decision record only. It does not generate or publish a
profile, and it does not certify that the generator will accept the selected
stage; callers must invoke the unchanged explicit generator and its validation
workflow separately.

## Set-profile schema versions 1, 2, and 3

A profile is a JSON object with these required fields:

- `schema_version`: supported values are `1`, `2`, and `3`. Other values are
  rejected rather than guessed. Historical schema-1 and schema-2 artifacts
  retain their canonical bytes; newly generated empirical profiles remain
  schema 1 or schema 2, and schema 3 currently means an enhanced profile.
- `profile_version`: the producer's non-empty artifact version.
- `set_code` and `format`: the exact target set and event format.
- `generated_at`: an ISO-8601 timestamp.
- `maturity`: `mature`, `early`, `metadata-only`, or `semantic-only` for local
  artifacts. The in-memory generic fallback is marked `generic` and is never
  written as an evidence artifact.
- `confidence`: a finite number from `0` through `1`.
- `source`: an object with a non-empty `provider` and optional artifact metadata.

Empirical sections are sparse and optional:

- `samples`, when present, contains a non-negative `total` and an optional
  `by_pair` object. The map contains only observed configured pairs; an omitted
  map means that per-pair counts are unavailable. `SampleSummary.count_for()`
  returns `None` for an unavailable pair.
- `pair_profiles`, when present, is an array containing only configured pairs
  for which a context is available. Each pair is listed at most once, in
  canonical `config.COLOR_PAIRS` order. A missing context is exposed by
  `SetProfile.pair()` returning `None`, not by an empty fabricated context.
  A context can contain optional empirical `structural_targets`, `role_targets`,
  `removal_targets`, `synergy`, and `scarcity` arrays. Empty arrays mean no
  evidence was supplied and are omitted during canonical serialization.

Schema-2 positive-sample card GIH estimates and pair-performance estimates
also contain `aggregate_evidence`. This object has exactly `source_format`,
`fallback_reason`, and `confidence`. Exact-format evidence uses the requested
format and a null reason. Cross-format evidence is valid only when the requested
format is QuickDraft, the source is PremierDraft or TradDraft, and the reason is
`missing-exact-evidence`, `thin-exact-evidence`, or
`invalid-exact-evidence`. Zero-sample priors have no aggregate authority.
Schema-1 profiles reject this field so older artifacts cannot silently claim
the new provenance contract.

`generate_set_profile(..., ratings=..., fallback_ratings=...)` keeps `ratings`
as the exact requested-format dataset. `fallback_ratings` accepts only
already-loaded same-set PremierDraft and TradDraft candidates. QuickDraft
selects supported observations independently for each canonical card and color
pair in exact, PremierDraft, then TradDraft order; other requested formats
remain exact-only. This API does not acquire or stage fallback data.

Pair-profile semantic annotations are separate from empirical evidence:

- `theme`, when present, is a trimmed, non-empty descriptive label for a pair
  context. It is semantic annotation metadata, not evidence, and may be used
  in semantic-only profiles. It annotates the canonical pair for explanations
  and is never a whitelist: a theme does not make other canonical pairs
  ineligible.

Mature and early profiles must contain empirical evidence. Metadata-only
profiles must contain neither empirical nor semantic evidence. Semantic-only
profiles must carry the compiled `role_profile`, may carry semantic pair
annotations such as `theme`, and must not contain empirical evidence. Thus
metadata-only artifacts can omit `samples` and `pair_profiles`, while
semantic-only artifacts omit empirical sections but retain `role_profile` and
may retain theme annotations. The generic fallback contains neither empirical
section.

### Enhancement block (schema 3)

`enhancement_status` is present exactly when `schema_version` is `3`: `enhanced`
when the `enhancement` object is present, otherwise `not-enhanced`. Schema-1 and
schema-2 payloads must not declare either key; declaring one is rejected rather
than ignored, so an artifact can never silently drop a model-assisted claim.

The `enhancement` object has `artifact_schema_version`, `artifact_sha256`,
`set_code`, `set_source_id`, `set_source_sha256`, `created_at`, `card_data`,
`cards`, `guides`, `runs`, `mechanics`, `relationships`, `review`, and
`confidence`. `artifact_schema_version` must equal
`SEMANTIC_ENRICHMENT_SCHEMA_VERSION`; an artifact written against a newer
semantic-enrichment schema is rejected as incompatible instead of being
partially read.

A block must satisfy all of the following; failing any condition rejects it:

- `card_data` pins the reviewed card source (`source`, `sha256`, `card_count`)
  and `cards` pins the reviewed cards, which must cover `card_count` exactly.
  `guides` pins guide sources and may be empty.
- `runs` records the model runs that produced the findings and must not be
  empty. `mechanics` and `relationships` hold the included findings: every
  mechanics entry must be a `mechanic`-category claim, every entry in either
  list must be accepted, and at least one of the two lists must be non-empty.
- Finding IDs must be globally unique, `set_code` must match the profile,
  every run, guide, and card a finding references must resolve against the
  block's own `runs`, `guides`, and `cards`, and every recorded run must be
  referenced by an included finding. A relationship's `oracle_evidence` must
  cover its `participants` exactly.
- `review` must have `state` `confirmed`. `confidence` is a bounded number
  separate from the profile's own `confidence`.

Enhancement is orthogonal to empirical maturity: a schema-3 profile has two
independent axes, maturity and enhancement status. The block carries
model-assisted semantic claims only, and `PairProfile.synergy` remains the
empirical 17Lands-derived field, never a place for enhancement content.

Only enhanced profiles are schema 3 today: `generate-profile` still emits
schema 1 for the metadata stage and schema 2 for early and mature stages, and
the enhancement compiler is the only schema-3 writer. The generic fallback
never carries enhancement data; the profile fingerprint, the SHA-256 of the
canonical bytes, covers the block's content and provenance.

The block reuses the semantic-enrichment record types (`CardSourcePin`,
`GuideSourcePin`, `ModelRun`, `GuideClaim`, `CardRelationship`,
`ArtifactReview`) verbatim instead of defining parallel profile-side records.

The optional `role_profile` object carries compiled per-card semantic roles. It
uses the existing semantic-role vocabulary and assignment types, and declares
`schema_version`, `role_schema_version`, `classifier_version`, and `cards`.
Unknown optional fields are ignored so producers can add fields without making
older readers unsafe. Required fields, field types, finite numeric values,
color-pair coverage where supplied, role assignments, maturity evidence
invariants, and all version values are validated. Serialization sorts keys and
records, so equivalent profiles have stable bytes. The domain graph consists of
frozen dataclasses and tuples; parsed JSON is not retained as mutable
dictionaries or lists.

## Remote manifest schema version 1

The producer manifest is a canonical JSON object with exactly these top-level
fields:

- `schema_version`: exactly `1`.
- `published_at`: a timezone-aware ISO-8601 publication timestamp.
- `artifacts`: a non-empty array with at most one artifact for each normalized
  `set_code` and `format` pair.

Each `artifacts` item has exactly these fields:

- `set_code` and `format`: non-empty, case-folded safe path components.
- `set_profile_schema_version`: the artifact's actual supported set-profile
  schema (`1`, `2`, or `3`). It must match the downloaded profile.
- `profile_version`: a non-empty producer version.
- `generated_at`: a timezone-aware ISO-8601 timestamp.
- `url`: an absolute HTTPS URL for the compressed profile artifact.
- `gzip_bytes` and `profile_bytes`: positive integer compressed and
  decompressed sizes.
- `gzip_sha256` and `profile_sha256`: 64-hex-character SHA-256 digests of
  the exact compressed and decompressed bytes.
- `maturity`: `mature`, `early`, `semantic-only`, or `metadata-only`;
  `generic` is never a downloadable manifest artifact.

Unknown fields, duplicate set/format identities, future schema versions,
invalid timestamps, unsafe path components, bad sizes/digests, and non-HTTPS
URLs are rejected. Manifest and profile serialization is canonical (sorted
keys and records with a final newline), so equivalent values have stable
bytes. `ProfileManifest.from_json()` and `load_profile_manifest()` perform
strict validation; `ProfileManifest.select(set_code=..., event_format=...)`
only returns an exact normalized identity.

## Lifecycle, cache, and refresh

Profile use is local-first and network-optional. The authoritative profile
cache is always the flat path:

```text
<app-data>/set-profiles/<set-code>-<format>.json
```

Here `<app-data>` means the directory returned by `app_data_dir()` (normally
`~/.draftomen`); `set_profile_path()` does not append `.draftomen` a second
time. The client keeps a separate validated manifest envelope at
`<app-data>/set-profiles/v1/manifest.json`; that file is only a manifest
validation/TTL cache, not a profile source. Downloaded profiles are stored as
canonical, uncompressed JSON at the flat path.

### Normal live session boundary

Normal live construction through the CLI `watch` path, Textual TUI,
`watch --plain`, and the Qt live factory uses the set-profile boundary as its
sole ratings authority. These paths construct `LiveSession` without direct
provider callbacks, provider-cache checks, synchronous or progress loaders,
raw provider-rating accessors, or direct 17Lands provider objects. Startup,
pack completion, and live build requests never acquire 17Lands data directly;
they use the selected profile or deterministic local/generic fallback.
Standalone build and backtest commands remain usable without provider data;
their separate offline workflows may still consume an explicitly available
cached provider snapshot, but do not perform live provider acquisition.

The explicit producer commands and separate domain/offline workflows retain
their supported provider behavior. Their cached or fresh provider inputs are
producer/offline concerns and are not a second ratings authority inside a
normal live session.
`LiveSession` publishes immutable profile and rating state and exposes only the
guarded refresh request/result handoff needed by adapters; it does not expose
mutable provider data.
The Qt adapter passes one `ProfileClient` instance to both the live session and
its background refresh worker. QML renders the published `set_profile` and
ratings state and emits intents; it performs no networking, scoring, caching,
or lifecycle decisions.

### Live command defaults

Terminal `watch`, `watch --plain`, CLI `watch`, and the native desktop live
command configure the production manifest by default:

```text
https://www.draftomen.com/profiles/manifest.json
```

`--profile-manifest-url URL` selects a different explicit HTTPS manifest for
either command. Set-profile activation is cache-first: a warm valid local
profile is usable immediately while the allowed hosted refresh runs. If the
hosted manifest or requested artifact is absent, unreachable, invalid, or
weaker than the cached profile, the last-good cache remains authoritative. With
no usable cache, deterministic fallback scoring remains active; live sessions
do not load ratings directly from 17Lands.

The native command accepts the same profile policy flags:

```sh
draftomen --profile-manifest-url "$PROFILE_MANIFEST_URL"
draftomen --offline-profiles
```

`--offline-profiles` selects `ProfileNetworkPolicy.OFFLINE`, takes precedence
over profile networking, and prevents manifest or artifact requests. It does
not disable Scryfall card metadata, card images, or static card-data
networking; those sources retain their own cache and network policies. This is
profile-only offline mode, not full application offline mode.

In the Textual TUI, `d` and the native ratings refresh control request the same
forced hosted-profile refresh through the shared live-session lifecycle. Force
bypasses the manifest TTL only. A newer validated result reports `updated`,
adopts atomically, and updates recommendations in place; an equal result
reports `unchanged` and leaves the current ratings active. Failed, offline,
missing, or non-adoptable refreshes retain the last usable cache when ratings
exist; with no usable empirical profile, deterministic fallback scoring remains
active. Repeated requests coalesce, and `watch --plain` has no command UI.

The default and override are client configuration only: they do not move
producer generation or website publication into the runtime and do not make
either one a package, native, or release dependency.

`ProfileClient(app_dir=..., manifest_url=..., network_policy=...)` is the
public client boundary. `ProfileNetworkPolicy.OFFLINE` forbids network access;
`ProfileNetworkPolicy.ALLOWED` permits it only when a manifest URL is
configured. `load_cached(set_code, event_format)` performs local I/O only and
never constructs a request. `refresh(set_code, event_format, force=False,
network_policy=...)` returns a `ProfileRefreshResult` containing the usable
profile, optional diagnostics/manifest, and a compact `status`.
Live session activation is cache-first: it loads and validates the local profile
before queuing any explicitly permitted refresh. A cached EARLY or MATURE profile
with empirical card ratings is immediately usable for scoring and recommendations;
the pending refresh does not block the current pack. For such a profile, its
published card ratings are the live authority, rather than a separately loaded
provider-ratings snapshot. An EARLY or MATURE profile is empirical only when
provider-backed evidence was available during generation; without valid provider
evidence, scoring uses deterministic fallback and never fabricates an empirical
profile.

Explicit ratings-download requests and retries of recoverable ratings errors
validate the active set and format, then queue a forced hosted-profile refresh
through the shared live-session lifecycle. A forced request bypasses only the
manifest TTL; it does not invoke the direct provider ratings loader. Offline
policy, a missing manifest or client, an injected authoritative profile, HTTPS
and origin security checks, checksum and schema validation, and non-regression
checks still apply. A usable cached or in-memory profile remains the scoring
authority while refresh is pending and when an accepted refresh fails, so
existing ratings and recommendations remain active. A validated newer result is
adopted atomically with the existing rescoring flow, so current recommendations
update without restarting.

At each snapshot publication, `LiveSession` projects recoverable ratings errors
from a per-set cache onto the active set only. An error for an inactive set, or
any ratings error when no set is active, is hidden; a nondismissed error is
projected again when that set becomes active. Dismissing an error removes its
per-set cache entry, so switching away and back cannot resurrect it. This
projection leaves unrelated operation errors untouched in the snapshot, while
their own lifecycle still applies (for example, an intentional account reset
may clear them). `Retry` acts only on the currently published active-set
ratings error and queues that set's forced hosted-profile refresh.
Repeated forced requests coalesce for the same active lifecycle. TUI and Qt emit
their existing actions; plain-watch adds no command UI.

For ordinary candidate loading, `safe_load_set_profile(...)` never raises for
missing, corrupt, future-schema, malformed, or wrong-target candidates. Its
deterministic fallback hierarchy is mature, then early, semantic-only, and
metadata-only, then a supplied `last_valid_profile` whose set and format match,
and finally a zero-confidence generic profile for exactly the requested target.
The fallback hierarchy's generic result is never exposed to scoring:
`load_scoring_profile()` returns `None`.

`ProfileClient.load_cached()` first uses a valid non-generic flat profile. If
that destination is missing, corrupt, future-schema, wrong-target, or generic,
it checks historical locations from earlier versions:

```text
<app-data>/profiles/<set>-<format>.json
<app-data>/set-profiles/v1/profiles/<set>-<format>.json
<app-data>/set-profiles/v1/<set>-<format>.json
```
After those flat and historical candidates, `ProfileClient.load_cached()`
considers a matching bundled baseline where applicable, then a matching
`last_valid_profile`, and finally generic.

A valid non-generic historical profile is reused offline and, when the flat
destination is writable, migrated there under the per-profile lock; the
historical file remains untouched. This preserves profiles across upgrades
without requiring a remote manifest. Corrupt or future-schema files are
reported as diagnostics and are not automatically deleted. A refresh failure
never deletes or replaces the last-good flat profile (or generic fallback); a
newly fetched valid manifest may still be cached independently before an
artifact failure is reported.

For empirical profile ratings, freshness comes from the profile's validated
`generated_at` generation timestamp. Adoption projects that timestamp into the
live ratings state; provider fetch or refresh-completion time is not a substitute.

Activation and rescoring remain local-only: they use the already validated cache
or in-memory authority, perform no network I/O, and do not reload the profile
cache for every score.

An adoptable hosted result is one atomic authority publication: profile
identity, `generated_at`-derived ratings freshness, current-pack scores and
scoring context, and recommendations advance together. A remote outage, invalid
artifact, or weaker result retains the strongest usable in-memory authority
and its freshness. Equal or unchanged results update refresh status without
replacing authority or rescoring. A newer cached profile, including one
installed concurrently, wins the same non-regression comparison and cannot be
overwritten by an older completion. Refresh completion consumes only its
supplied result and in-memory state; it performs no network or profile-cache
load.

When networking is allowed, the client reuses a validated manifest for its
default 24-hour TTL. A forced request (`force=True`) bypasses only this TTL,
then selects the exact normalized set/format artifact. It accepts only a newer
maturity or timestamp; an identical artifact is `unchanged`, while an older,
lower-maturity, or same-timestamp conflicting artifact is `stale-manifest`. A
missing target artifact is `missing`. Manifest and artifact failures never
replace the current profile.

Every remote URL must be absolute HTTPS with no credentials, fragment,
whitespace, or non-default port. Artifact URLs and redirects must remain on
the manifest URL's HTTPS origin. The client applies a positive timeout (10
seconds by default), bounds the manifest to 1 MiB, compressed artifacts to
64 MiB, and decompressed profiles to 128 MiB. It streams the gzip bytes,
checks the declared compressed size and SHA-256, rejects incomplete or
trailing gzip data, checks decompressed size and SHA-256, then validates
canonical JSON, set-profile schema, set/format identity, profile version,
maturity, generated timestamp, and schema version before installation.

Staging files are created in the destination directory. A successful commit
flushes and atomically replaces the flat cache with `os.replace`, then
flushes the directory; a failed download, decompression, checksum, schema, or
metadata check cannot become current. Concurrent refreshes for the same cache
key use a process-local lock and re-read the destination under that lock, so a
later commit cannot overwrite a newer valid profile with an older one.

Refresh outcomes are compact and stable:

```text
offline | cached | unchanged | updated | missing | stale-manifest |
manifest-invalid | artifact-invalid | remote-failed
```

`offline` means no usable cached profile was available when networking was
disabled; `cached` means a non-generic local profile was retained without
networking; `updated` means a newer artifact was committed. The remaining
outcomes describe why a remote refresh did not replace the cache. In every
case the result's `profile` is the usable last-good or generic fallback.
The manual command prints this as compact maturity/outcome status:

```sh
uv run draftomen-tui refresh-profile \
  --set-code TST \
  --format quickdraft \
  --manifest-url "$PROFILE_MANIFEST_URL"

# Optional explicit application-data directory:
uv run draftomen-tui refresh-profile \
  --set-code TST \
  --format quickdraft \
  --manifest-url "$PROFILE_MANIFEST_URL" \
  --app-dir "$HOME/.draftomen"
```

Output is one privacy-safe line such as
`refresh-profile: set_code=tst format=quickdraft maturity=mature
outcome=updated cache_path=...`. It does not print profile rows or source
data. A non-generic usable result exits successfully; a generic result or
setup failure exits non-zero.


The refresh generation is the lifecycle authority for live hosted-profile
refreshes. Set, account, or draft identity changes, along with clear or stop,
retire the request so an obsolete completion cannot be accepted as current,
publish profile/status/error state, or trigger rescoring. A worker that has
already entered `ProfileClient.refresh()` may still finish its network or
cache work; its result is ignored by the generation guard. Ordinary picks and
repeated detection of the same lifecycle do not restart the refresh. On
shutdown, adapters retire the session lifecycle and tear down their owned
refresh workers before releasing them, so an in-flight result cannot publish
late state. This is logical stale-result rejection only; it does not promise
to cancel worker threads, network requests, or cache writes already underway.

## Semantic-role compatibility

Compiled role data is authoritative only when its declared role schema and
classifier version match the installed `ROLE_SCHEMA_VERSION` and
`CLASSIFIER_VERSION`. `SetProfile.resolve_roles()` passes the compiled object
unchanged to `RoleClassifier.resolve`, which performs the compatibility check
and falls back to local classification. An incompatible profile therefore falls
back wholly to the local classifier; roles are never merged across versions.
Missing empirical sections do not remove a valid semantic role profile.

## Deliberate exclusions

The generator and client do not change pick scoring, deck-building heuristics,
card-database schemas, or 17Lands cache schemas. The generator reads only
explicit local inputs; the producer manifest APIs validate and write local
publication files, while the client downloads only when configured by its
caller. Native and terminal live commands supply the production manifest by
default; `--profile-manifest-url` overrides it and `--offline-profiles` disables
profile networking only.

Explicit producer commands retain their supported provider ingestion from
caller-selected or first-party 17Lands inputs. Producer generation, static
website publication, and client consumption are separate boundaries: producer
and client code do not upload, schedule, discover, or backfill profile data.
The current hosting boundary above is limited to the master-only static assets
under `website/public/profiles/` and `website/public/profiles-dev/`, deployed
in the ordinary complete Astro/Cloudflare website snapshot.

The broader issue #227 discovery, scheduling, backfill, and publication
automation remains excluded and does not own hosting. Native issue #354
integration uses the same shared profile lifecycle: startup applies the
production default, explicit override, or profile-only offline policy, while
existing ratings controls present shared refresh outcomes without direct
runtime 17Lands loading.
Website hosting and profile publication do not trigger or gate native
packaging, releases, startup, PyPI, or Homebrew. Producers choose maturity and
hand off the checksummed artifacts described by their manifest; the loader only
validates, orders, and exposes profiles.
