"""Generate static card data and local profile artifacts for the website.

This helper deliberately stops at the checked-out website tree.  Publication,
branch management, and deployment belong to a later workflow.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, TypeAlias

from draftomen.card_data_export import (
    PreparedSetDataExport,
    SetDataExportPlan,
    prepare_set_data_export,
    publish_set_data_export,
)
from draftomen.profile_batch_generation import (
    ProfileBatchGenerationError,
    generate_staged_profile_batch,
)
from draftomen.profile_data_refresh import (
    FILTERS_ENDPOINT,
    PROFILE_BASE_URL,
    Pair,
    Plan as ProfilePlan,
    _now,
    _reuse_or_publish_object,
    prepare_profile_data_refresh,
)
from draftomen.profile_input_acquisition import (
    CardMetadataAdapter,
    SeventeenLandsPublicDraftAdapter,
    SeventeenLandsRatingsAdapter,
)
from draftomen.profile_input_cache import ProfileInputCache
from draftomen.profile_manifest import (
    ProfileManifestArtifact,
    ProfileManifestError,
    load_profile_manifest,
)
from draftomen.profile_publication import (
    ProfilePublicationError,
    build_profile_manifest,
    publish_profile_manifest,
)
from draftomen.profile_refresh_execution import (
    DEFAULT_PROFILE_REFRESH_CACHE_POLICY,
    ProfileRefreshExecutionError,
    execute_profile_refresh_plan,
)
from draftomen.refresh_plan import (
    LifecycleMetadata,
    PlannedEnvironment,
    RefreshPlan,
)
from draftomen.seventeen import (
    HTTP_TIMEOUT_SECONDS,
    SEVENTEEN_LANDS_ATTRIBUTION,
    SEVENTEEN_LANDS_EXPANSIONS_ENDPOINT,
    SeventeenLandsExpansionInventory,
    _default_fetch_json,
    fetch_17lands_format_data,
    parse_17lands_expansion_inventory,
)

FetchJson: TypeAlias = Callable[[str, int], Any]
Clock: TypeAlias = Callable[[], datetime]

SELECTION_MODES = ("one", "all", "active", "historical")
_GENERATED_ROOT = Path("website/public")
_CARD_DATA_ROOT = _GENERATED_ROOT / "card-data"
_PROFILES_ROOT = _GENERATED_ROOT / "profiles"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ProfileRefreshWorkflowError(ValueError):
    """Raised for invalid helper inputs."""


class _BundleError(RuntimeError):
    """Raised when a generated delta cannot be collected."""


def _selection(selection_mode: str, selector: str | None) -> tuple[str, str | None, str, str | None]:
    if selection_mode not in SELECTION_MODES:
        raise ProfileRefreshWorkflowError("selection mode is invalid")
    if selection_mode == "one":
        if not isinstance(selector, str) or not selector.strip():
            raise ProfileRefreshWorkflowError("one selection requires --set")
        return selection_mode, selector.strip(), "all", selector.strip()
    if selector is not None:
        raise ProfileRefreshWorkflowError("--set is only valid with one selection")
    return selection_mode, None, selection_mode, None


def _identity_json(identity: Any) -> dict[str, str]:
    return {"set_code": identity.set_code, "set_name": identity.set_name}


def _pair_key(pair: Pair) -> tuple[str, str]:
    return pair.set_code, pair.event_format.casefold()


def _failure(
    *,
    stage: str,
    category: str,
    set_code: str | None = None,
    event_format: str | None = None,
) -> dict[str, str]:
    result: dict[str, str] = {"stage": stage, "category": category}
    if set_code is not None:
        result["set_code"] = set_code
    if event_format is not None:
        result["event_format"] = event_format
    return result


def _static_failure(
    candidate: PreparedSetDataExport,
) -> dict[str, str]:
    return _failure(
        stage="static-write",
        category="static-export-failed",
        set_code=candidate.identity.set_code,
    )


def _sort_failures(failures: list[dict[str, str]]) -> None:
    stage_order = {
        "static-discovery": 0,
        "static-write": 1,
        "profile-planning": 2,
        "profile-execution": 3,
        "bundle": 4,
    }
    failures.sort(
        key=lambda item: (
            stage_order.get(item.get("stage", ""), 99),
            item.get("set_code", ""),
            item.get("event_format", ""),
            item.get("category", ""),
        )
    )


def _safe_summary_text(value: Any) -> str:
    if value is None:
        return "not reported"
    text = str(value)
    if "/" in text or "\\" in text or "://" in text:
        return "not reported"
    text = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in text
    )
    escaped = html.escape(text.strip(), quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>~])", r"\\\1", escaped) or "not reported"


def _pair_label(pair: Mapping[str, Any]) -> str:
    return f"{_safe_summary_text(pair.get('set_code'))} / {_safe_summary_text(pair.get('event_format'))} / {_safe_summary_text(pair.get('set_name'))}"


def render_summary(report: Mapping[str, Any]) -> str:
    """Render a bounded, escaped summary from the generation report."""

    selection = report.get("selection") if isinstance(report.get("selection"), Mapping) else {}
    static = report.get("static") if isinstance(report.get("static"), Mapping) else {}
    profiles = report.get("profiles") if isinstance(report.get("profiles"), Mapping) else {}
    selected_static = static.get("selected") if isinstance(static.get("selected"), list) else []
    static_successful = set(static.get("successful", [])) if isinstance(static.get("successful"), list) else set()
    selected_pairs = profiles.get("selected") if isinstance(profiles.get("selected"), list) else []
    successful_pairs = profiles.get("successful") if isinstance(profiles.get("successful"), list) else []
    successful_pair_keys = {
        (item.get("set_code"), item.get("event_format"))
        for item in successful_pairs
        if isinstance(item, Mapping)
    }
    failures = report.get("failures") if isinstance(report.get("failures"), list) else []

    lines = [
        "# Website generation",
        "",
        f"- Status: {_safe_summary_text(report.get('status'))}",
        f"- Base commit: {_safe_summary_text(report.get('base_commit'))}",
        f"- Selection: {_safe_summary_text(selection.get('mode'))}"
        + (f" ({_safe_summary_text(selection.get('selector'))})" if selection.get("selector") else ""),
        "",
        "## Static card data",
        "",
        f"- Discovery complete: {_safe_summary_text(static.get('discovery_complete'))}",
        f"- Eligible: {_safe_summary_text(static.get('eligible_count'))}",
        f"- Already valid: {_safe_summary_text(static.get('already_valid_count'))}",
        "### Pending sets",
        "",
    ]
    if selected_static:
        for identity in selected_static:
            if isinstance(identity, Mapping):
                code = identity.get("set_code")
                outcome = "successful" if code in static_successful else "failed"
                lines.append(
                    f"- {_pair_label(identity)}: {_safe_summary_text(outcome)}"
                )
    else:
        lines.append("- None")

    lines.extend(
        [
            "## Profiles",
            "",
            f"- Data attribution: {SEVENTEEN_LANDS_ATTRIBUTION}",
            f"- Planning complete: {_safe_summary_text(profiles.get('planning_complete'))}",
            f"- Manifest changed: {_safe_summary_text(profiles.get('manifest_changed'))}",
            "### Selected pairs",
            "",
        ]
    )
    if selected_pairs:
        for pair in selected_pairs:
            if isinstance(pair, Mapping):
                key = (pair.get("set_code"), pair.get("event_format"))
                outcome = "successful" if key in successful_pair_keys else "failed"
                lines.append(f"- {_pair_label(pair)}: {_safe_summary_text(outcome)}")
    else:
        lines.append("- None")

    lines.extend(["", "## Failures", ""])
    if failures:
        for failure in failures:
            if not isinstance(failure, Mapping):
                continue
            details = [
                _safe_summary_text(failure.get("stage")),
                _safe_summary_text(failure.get("category")),
            ]
            if failure.get("set_code") is not None:
                details.append(_safe_summary_text(failure.get("set_code")))
            if failure.get("event_format") is not None:
                details.append(_safe_summary_text(failure.get("event_format")))
            lines.append("- " + ": ".join(details))
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"

def _prepare_planning_view(
    plan: SetDataExportPlan,
    *,
    output_dir: Path,
    planning_dir: Path,
) -> None:
    pending = {candidate.identity.set_code: candidate for candidate in plan.pending}
    # The exporter inventory can lag local card-data.  Keep every existing
    # artifact in the planning view; the profile planner performs its own
    # validation and will retain valid local identities outside this run's
    # static inventory.
    for source in sorted(output_dir.glob("*.json.gz"), key=lambda item: item.name):
        code = source.name[: -len(".json.gz")]
        if code in pending:
            continue
        destination = planning_dir / source.name
        try:
            destination.symlink_to(source)
        except OSError:
            shutil.copyfile(source, destination)
    for identity in plan.sets:
        candidate = pending.get(identity.set_code)
        if candidate is None:
            continue
        (planning_dir / f"{identity.set_code}.json.gz").write_bytes(candidate.gzip_bytes)


def _profile_plan_with_real_paths(plan: ProfilePlan, *, card_data_dir: Path) -> ProfilePlan:
    return ProfilePlan(
        pairs=tuple(
            replace(pair, static_path=card_data_dir / f"{pair.set_code}.json.gz")
            for pair in plan.pairs
        ),
        mode=plan.mode,
        selector=plan.selector,
    )


def _acquire_inventory(
    *,
    inventory_file: Path | None,
    fetch_json: FetchJson | None,
    root: Path,
) -> tuple[SeventeenLandsExpansionInventory, Path, Any]:
    """Read one expansion snapshot and return the path used by static export."""

    temporary: Any = None
    try:
        if inventory_file is None:
            fetcher = _default_fetch_json if fetch_json is None else fetch_json
            payload = fetcher(
                SEVENTEEN_LANDS_EXPANSIONS_ENDPOINT,
                HTTP_TIMEOUT_SECONDS,
            )
        else:
            payload = json.loads(Path(inventory_file).read_bytes().decode("utf-8"))
        inventory = parse_17lands_expansion_inventory(payload)
        if inventory_file is not None:
            return inventory, Path(inventory_file), temporary
        temporary = tempfile.TemporaryDirectory(prefix=".profile-refresh-inventory-", dir=root)
        path = Path(temporary.name) / "expansions.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return inventory, path, temporary
    except Exception:
        if temporary is not None:
            temporary.cleanup()
        raise


def _manual_refresh_plan(
    pair: Pair,
    *,
    inventory: SeventeenLandsExpansionInventory,
) -> RefreshPlan:
    """Bind exactly one already-selected pair to one strict refresh plan."""

    environment = PlannedEnvironment(
        set_code=pair.set_code,
        event_format=pair.event_format.casefold(),
        lifecycle=None,
        reasons=("hosted-selection",),
    )
    return RefreshPlan(
        selection_mode="manual",
        event_format=environment.event_format,
        environments=(environment,),
        inventory_source_url=inventory.source_url,
        inventory_payload_digest=inventory.source_payload_digest,
        lifecycle=LifecycleMetadata(
            provider="17Lands hosted filters",
            source_url=FILTERS_ENDPOINT,
            version="filters-v1",
            classifications=(),
        ),
        selection_set_code=environment.set_code,
    )


def _profile_manifest_artifact(
    *,
    generation: Any,
) -> ProfileManifestArtifact:
    report = generation.report
    profile = generation.profile
    return ProfileManifestArtifact(
        set_code=report.set_code,
        event_format=report.event_format,
        set_profile_schema_version=report.set_profile_schema_version,
        profile_version=profile.profile_version,
        generated_at=report.generated_at,
        url=f"{PROFILE_BASE_URL.rstrip('/')}/{report.gzip_sha256}.json.gz",
        gzip_bytes=report.gzip_bytes,
        profile_bytes=report.profile_bytes,
        gzip_sha256=report.gzip_sha256,
        profile_sha256=report.profile_sha256,
        maturity=profile.maturity,
    )


def _materialize_profiles(
    *,
    profiles_dir: Path,
    command_now: datetime,
    candidates: Sequence[tuple[Pair, Any]],
    failures: list[dict[str, str]],
) -> tuple[list[Pair], bool]:
    """Publish validated objects, then atomically merge one manifest."""

    if not candidates:
        return [], False
    try:
        existing_manifest = load_profile_manifest(profiles_dir / "manifest.json")
    except (OSError, ProfileManifestError):
        for pair, _result in candidates:
            failures.append(
                _failure(
                    stage="profile-execution",
                    category="execution-failed",
                    set_code=pair.set_code,
                    event_format=pair.event_format,
                )
            )
        return [], False

    prepared: list[tuple[Pair, ProfileManifestArtifact, bytes, Path]] = []
    for pair, result in candidates:
        try:
            generation = result.generation
            validated = result.validated
            if generation is None or validated is None:
                raise ValueError("validated profile payload is missing")
            artifact = _profile_manifest_artifact(generation=generation)
            if not isinstance(validated.gzip_bytes, bytes):
                raise TypeError("validated profile gzip is invalid")
            prepared.append(
                (
                    pair,
                    artifact,
                    validated.gzip_bytes,
                    profiles_dir / "objects" / f"{artifact.gzip_sha256}.json.gz",
                )
            )
        except (AttributeError, TypeError, ValueError, ProfileManifestError):
            failures.append(
                _failure(
                    stage="profile-execution",
                    category="profile-validation-failed",
                    set_code=pair.set_code,
                    event_format=pair.event_format,
                )
            )

    published: list[tuple[Pair, ProfileManifestArtifact]] = []
    for pair, artifact, payload, object_path in prepared:
        try:
            # Reuse the existing content-addressed publication primitive.  It
            # preserves prior identities when a conflicting object is found.
            _reuse_or_publish_object(path=object_path, payload=payload)
        except (OSError, ValueError):
            failures.append(
                _failure(
                    stage="profile-execution",
                    category="object-publish-failed",
                    set_code=pair.set_code,
                    event_format=pair.event_format,
                )
            )
            continue
        published.append((pair, artifact))

    if not published:
        return [], False

    existing_artifacts = {
        (artifact.set_code, artifact.event_format): artifact
        for artifact in existing_manifest.artifacts
    }
    replacements = {pair.identity: artifact for pair, artifact in published}
    replacement_changed = any(
        existing_artifacts.get(identity) != artifact
        for identity, artifact in replacements.items()
    )
    if replacement_changed:
        existing_artifacts.update(replacements)
        try:
            merged_manifest = build_profile_manifest(
                tuple(existing_artifacts.values()),
                published_at=command_now,
            )
            publish_profile_manifest(profiles_dir / "manifest.json", merged_manifest)
        except (OSError, ProfileManifestError, ProfilePublicationError, TypeError, ValueError):
            for pair, _artifact in published:
                failures.append(
                    _failure(
                        stage="profile-execution",
                        category="manifest-publish-failed",
                        set_code=pair.set_code,
                        event_format=pair.event_format,
                    )
                )
            return [], False
        return [pair for pair, _artifact in published], True
    return [pair for pair, _artifact in published], False


def _base_bytes(repo_root: Path, base_commit: str, relative_path: str) -> bytes | None:
    shown = subprocess.run(
        ["git", "show", f"{base_commit}:{relative_path}"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if shown.returncode == 0:
        return shown.stdout
    verified = subprocess.run(
        ["git", "rev-parse", "--verify", f"{base_commit}^{{commit}}"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if verified.returncode != 0:
        raise _BundleError("base commit could not be resolved")
    return None


def _asset_descriptor(relative_path: str, payload: bytes) -> dict[str, object]:
    return {
        "path": relative_path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
def _collect_assets(
    *,
    repo_root: Path,
    bundle_dir: Path,
    base_commit: str,
    static_successes: Sequence[str],
    static_already_valid: Sequence[str],
    successful_pairs: Sequence[Pair],
) -> list[dict[str, object]]:
    selected_paths: set[str] = {
        (_CARD_DATA_ROOT / f"{code}.json.gz").as_posix()
        for code in (*static_successes, *static_already_valid)
    }
    if successful_pairs:
        selected_paths.add((_PROFILES_ROOT / "manifest.json").as_posix())
        selected_paths.update(
            (_CARD_DATA_ROOT / f"{pair.set_code}.json.gz").as_posix()
            for pair in successful_pairs
        )
        profiles_dir = repo_root / _PROFILES_ROOT
        try:
            manifest = load_profile_manifest(profiles_dir / "manifest.json")
        except (OSError, ProfileManifestError) as error:
            raise _BundleError("successful profile manifest could not be loaded") from error
        for pair in successful_pairs:
            artifact = manifest.select(set_code=pair.set_code, event_format=pair.event_format)
            if artifact is None or not _SHA256_PATTERN.fullmatch(artifact.gzip_sha256):
                raise _BundleError("successful profile object is not in the manifest")
            selected_paths.add(
                (_PROFILES_ROOT / "objects" / f"{artifact.gzip_sha256}.json.gz").as_posix()
            )

    pending_assets: list[tuple[str, bytes]] = []
    for relative_path in sorted(selected_paths):
        if not (
            relative_path.startswith("website/public/card-data/")
            or relative_path == "website/public/profiles/manifest.json"
            or relative_path.startswith("website/public/profiles/objects/")
        ):
            raise _BundleError("generated asset path is outside the allowlist")
        source = repo_root / relative_path
        try:
            payload = source.read_bytes()
        except OSError as error:
            raise _BundleError("successful generated asset is unavailable") from error
        if _base_bytes(repo_root, base_commit, relative_path) != payload:
            pending_assets.append((relative_path, payload))

    descriptors = [
        _asset_descriptor(relative_path, payload)
        for relative_path, payload in pending_assets
    ]
    try:
        for relative_path, payload in pending_assets:
            destination = bundle_dir / "generated" / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
    except OSError as error:
        raise _BundleError("generated asset could not be copied") from error
    return descriptors


def _empty_static() -> dict[str, Any]:
    return {
        "discovery_complete": False,
        "eligible_count": None,
        "already_valid_count": None,
        "selected": [],
        "successful": [],
    }


def generate_website(
    *,
    base_commit: str,
    selection_mode: str,
    selector: str | None,
    repo_root: Path,
    bundle_dir: Path,
    cache_dir: Path,
    inventory_file: Path | None = None,
    bulk_file: Path | None = None,
    fetch_json: FetchJson | None = None,
    clock: Clock | None = None,
    card_metadata_adapter: CardMetadataAdapter | None = None,
    ratings_adapter: SeventeenLandsRatingsAdapter | None = None,
    public_draft_adapter: SeventeenLandsPublicDraftAdapter | None = None,
) -> dict[str, object]:
    """Generate selected website data and persist a report plus delta bundle."""

    if not isinstance(base_commit, str) or not base_commit.strip():
        raise ProfileRefreshWorkflowError("base commit is required")
    mode, normalized_selector, profile_mode, profile_selector = _selection(
        selection_mode, selector
    )
    root = Path(repo_root).resolve()
    bundle = Path(bundle_dir)
    static_dir = root / _CARD_DATA_ROOT
    profiles_dir = root / _PROFILES_ROOT
    bundle.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(bundle / "generated", ignore_errors=True)
    shutil.rmtree(bundle / "batch-reports", ignore_errors=True)
    for old in (bundle / "result.json", bundle / "summary.md"):
        old.unlink(missing_ok=True)

    report: dict[str, Any] = {
        "schema_version": 1,
        "base_commit": base_commit,
        "selection": {"mode": mode, "selector": normalized_selector},
        "status": "failed",
        "static": _empty_static(),
        "profiles": {
            "planning_complete": False,
            "selected": [],
            "successful": [],
            "manifest_changed": False,
        },
        "failures": [],
        "generated_assets": [],
    }
    failures: list[dict[str, str]] = report["failures"]
    static_plan: SetDataExportPlan | None = None
    static_successes: list[str] = []
    static_already_valid: list[str] = []
    profile_plan: ProfilePlan | None = None
    inventory: SeventeenLandsExpansionInventory | None = None
    inventory_path: Path | None = None
    inventory_temporary: Any = None

    try:
        try:
            inventory, inventory_path, inventory_temporary = _acquire_inventory(
                inventory_file=inventory_file,
                fetch_json=fetch_json,
                root=root,
            )
        except Exception:
            # Do not ask the exporter or planner to invent provenance after an
            # inventory failure; both bounded failures remain visible.
            failures.append(_failure(stage="static-discovery", category="discovery-failed"))
            failures.append(_failure(stage="profile-planning", category="planning-failed"))

        if inventory is not None and inventory_path is not None:
            try:
                static_plan = prepare_set_data_export(
                    selector=None,
                    output_dir=static_dir,
                    inventory_file=inventory_path,
                    bulk_file=bulk_file,
                )
                report["static"] = {
                    "discovery_complete": True,
                    "eligible_count": static_plan.total,
                    "already_valid_count": len(static_plan.already_valid),
                    "selected": [
                        _identity_json(candidate.identity) for candidate in static_plan.pending
                    ],
                    "successful": [],
                }
                static_already_valid = [
                    identity.set_code for identity in static_plan.already_valid
                ]
            except Exception:
                failures.append(_failure(stage="static-discovery", category="discovery-failed"))

        if inventory is not None:
            if static_plan is not None:
                try:
                    for candidate in static_plan.pending:
                        try:
                            publish_set_data_export(candidate=candidate)
                        except Exception:
                            failures.append(_static_failure(candidate))
                        else:
                            static_successes.append(candidate.identity.set_code)
                    report["static"]["successful"] = list(static_successes)
                except Exception:
                    # A failure in the loop itself must not turn a selected
                    # candidate into an unreported success.
                    completed = set(static_successes)
                    for candidate in static_plan.pending:
                        if candidate.identity.set_code not in completed and not any(
                            failure.get("set_code") == candidate.identity.set_code
                            and failure.get("stage") == "static-write"
                            for failure in failures
                        ):
                            failures.append(_static_failure(candidate))
                    report["static"]["successful"] = list(static_successes)

            try:
                if static_plan is None:
                    planning_source = static_dir
                    profile_plan = prepare_profile_data_refresh(
                        profile_selector,
                        card_data_dir=planning_source,
                        mode=profile_mode,
                        fetch_json=fetch_json,
                        filters_url=FILTERS_ENDPOINT,
                    )
                else:
                    with tempfile.TemporaryDirectory(
                        prefix=".profile-refresh-plan-", dir=root
                    ) as temporary:
                        planning_dir = Path(temporary)
                        _prepare_planning_view(
                            static_plan,
                            output_dir=static_dir,
                            planning_dir=planning_dir,
                        )
                        planned = prepare_profile_data_refresh(
                            profile_selector,
                            card_data_dir=planning_dir,
                            mode=profile_mode,
                            fetch_json=fetch_json,
                            filters_url=FILTERS_ENDPOINT,
                        )
                        profile_plan = _profile_plan_with_real_paths(
                            planned,
                            card_data_dir=static_dir,
                        )
                assert profile_plan is not None
                report["profiles"]["planning_complete"] = True
                report["profiles"]["selected"] = [
                    pair.to_json() for pair in profile_plan.pairs
                ]
            except Exception:
                failures.append(_failure(stage="profile-planning", category="planning-failed"))
    finally:
        if inventory_temporary is not None:
            inventory_temporary.cleanup()

    successful_pairs: list[Pair] = []
    if profile_plan is not None:
        candidates: list[tuple[Pair, Any]] = []
        try:
            command_now = _now(clock=clock)
            cache = ProfileInputCache(
                cache_dir,
                policy=DEFAULT_PROFILE_REFRESH_CACHE_POLICY,
                clock=clock,
            )
            effective_ratings_adapter = ratings_adapter
            if effective_ratings_adapter is None:
                def fetch_ratings(
                    *,
                    set_code: str,
                    event_format: str,
                    fetched_at: datetime,
                    timeout_seconds: int,
                ) -> Any:
                    return fetch_17lands_format_data(
                        set_code=set_code,
                        event_format=event_format,
                        fetched_at=fetched_at,
                        fetch_json=fetch_json,
                        timeout_seconds=timeout_seconds,
                    )

                effective_ratings_adapter = SeventeenLandsRatingsAdapter(
                    fetch_ratings=fetch_ratings,
                )
        except Exception:
            for pair in profile_plan.pairs:
                failures.append(
                    _failure(
                        stage="profile-execution",
                        category="execution-failed",
                        set_code=pair.set_code,
                        event_format=pair.event_format,
                    )
                )
        else:
            for pair in profile_plan.pairs:
                try:
                    staged_plan = _manual_refresh_plan(pair, inventory=inventory)
                    with tempfile.TemporaryDirectory(
                        prefix=".profile-refresh-staged-", dir=root
                    ) as staged:
                        staged_dir = Path(staged)
                        execute_profile_refresh_plan(
                            plan=staged_plan,
                            cache=cache,
                            output_dir=staged_dir,
                            offline=False,
                            card_metadata_adapter=card_metadata_adapter,
                            ratings_adapter=effective_ratings_adapter,
                            public_draft_adapter=public_draft_adapter,
                            clock=clock,
                            include_public_drafts=False,
                        )
                        batch = generate_staged_profile_batch(
                            plan=staged_plan,
                            staged_dir=staged_dir,
                            generated_at=command_now,
                        )
                        report_path = bundle / "batch-reports" / f"{batch.plan_sha256}.json"
                        try:
                            report_path.parent.mkdir(parents=True, exist_ok=True)
                            report_path.write_bytes(batch.to_bytes())
                        except OSError:
                            failures.append(
                                _failure(
                                    stage="bundle",
                                    category="report-write-failed",
                                    set_code=pair.set_code,
                                    event_format=pair.event_format,
                                )
                            )

                        result = batch.environments[0]
                        if not result.publication_eligible:
                            reason = result.failure_reason
                            category = (
                                reason.value
                                if hasattr(reason, "value")
                                else reason
                                if isinstance(reason, str) and reason
                                else "execution-failed"
                            )
                            failures.append(
                                _failure(
                                    stage="profile-execution",
                                    category=category,
                                    set_code=pair.set_code,
                                    event_format=pair.event_format,
                                )
                            )
                        elif (
                            result.selection is None
                            or result.selection.stage.value not in {"early", "mature"}
                        ):
                            failures.append(
                                _failure(
                                    stage="profile-execution",
                                    category="empirical-evidence-unavailable",
                                    set_code=pair.set_code,
                                    event_format=pair.event_format,
                                )
                            )
                        elif result.generation is None or result.validated is None:
                            failures.append(
                                _failure(
                                    stage="profile-execution",
                                    category="profile-validation-failed",
                                    set_code=pair.set_code,
                                    event_format=pair.event_format,
                                )
                            )
                        elif not pair.static_path.is_file():
                            failures.append(
                                _failure(
                                    stage="profile-execution",
                                    category="static-artifact-invalid",
                                    set_code=pair.set_code,
                                    event_format=pair.event_format,
                                )
                            )
                        else:
                            candidates.append((pair, result))
                except (OSError, ProfileBatchGenerationError, ProfileRefreshExecutionError):
                    failures.append(
                        _failure(
                            stage="profile-execution",
                            category="execution-failed",
                            set_code=pair.set_code,
                            event_format=pair.event_format,
                        )
                    )

            successful_pairs, manifest_changed = _materialize_profiles(
                profiles_dir=profiles_dir,
                command_now=command_now,
                candidates=candidates,
                failures=failures,
            )
            report["profiles"]["successful"] = [
                pair.to_json() for pair in successful_pairs
            ]
            report["profiles"]["manifest_changed"] = manifest_changed

    try:
        report["generated_assets"] = _collect_assets(
            static_already_valid=static_already_valid,
            repo_root=root,
            bundle_dir=bundle,
            base_commit=base_commit,
            static_successes=static_successes,
            successful_pairs=successful_pairs,
        )
    except Exception:
        shutil.rmtree(bundle / "generated", ignore_errors=True)
        failures.append(_failure(stage="bundle", category="bundle-failed"))
        report["generated_assets"] = []

    _sort_failures(failures)
    report["status"] = "failed" if failures else "success"
    try:
        (bundle / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (bundle / "summary.md").write_text(render_summary(report), encoding="utf-8")
    except OSError:
        failures.append(_failure(stage="bundle", category="report-write-failed"))
        _sort_failures(failures)
        report["status"] = "failed"
        try:
            (bundle / "result.json").write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="profile_refresh_workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--selection-mode", choices=SELECTION_MODES, required=True)
    generate.add_argument("--set")
    generate.add_argument("--base-commit", required=True)
    generate.add_argument("--bundle-dir", type=Path, required=True)
    generate.add_argument("--cache-dir", type=Path, required=True)
    generate.add_argument("--repo-root", type=Path, default=Path.cwd())
    generate.add_argument("--inventory-file", type=Path)
    generate.add_argument("--bulk-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the generation command and return its process status."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command != "generate":
            parser.error("a generation command is required")
        if args.selection_mode == "one" and args.set is None:
            parser.error("--set is required for one selection")
        if args.selection_mode != "one" and args.set is not None:
            parser.error("--set is only valid for one selection")
        report = generate_website(
            base_commit=args.base_commit,
            selection_mode=args.selection_mode,
            selector=args.set,
            repo_root=args.repo_root,
            bundle_dir=args.bundle_dir,
            cache_dir=args.cache_dir,
            inventory_file=args.inventory_file,
            bulk_file=args.bulk_file,
        )
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    except ProfileRefreshWorkflowError:
        return 2
    except Exception:
        return 1
    return 0 if report.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ProfileRefreshWorkflowError", "generate_website", "main", "render_summary"]
