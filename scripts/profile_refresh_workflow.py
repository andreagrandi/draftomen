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
from draftomen.profile_data_refresh import (
    FILTERS_ENDPOINT,
    Pair,
    Plan as ProfilePlan,
    Result as ProfileResult,
    execute_profile_data_refresh,
    prepare_profile_data_refresh,
)
from draftomen.seventeen import SEVENTEEN_LANDS_ATTRIBUTION
from draftomen.profile_manifest import ProfileManifestError, load_profile_manifest


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
    profile_result: ProfileResult | None,
) -> list[dict[str, object]]:
    selected_paths: set[str] = {
        (_CARD_DATA_ROOT / f"{code}.json.gz").as_posix()
        for code in (*static_successes, *static_already_valid)
    }
    if profile_result is not None and profile_result.successful_pairs:
        selected_paths.add((_PROFILES_ROOT / "manifest.json").as_posix())
        selected_paths.update(
            (_CARD_DATA_ROOT / f"{pair.set_code}.json.gz").as_posix()
            for pair in profile_result.successful_pairs
        )
        profiles_dir = repo_root / _PROFILES_ROOT
        try:
            manifest = load_profile_manifest(profiles_dir / "manifest.json")
        except (OSError, ProfileManifestError) as error:
            raise _BundleError("successful profile manifest could not be loaded") from error
        for pair in profile_result.successful_pairs:
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
    profile_result: ProfileResult | None = None

    try:
        static_plan = prepare_set_data_export(
            selector=None,
            output_dir=static_dir,
            inventory_file=inventory_file,
            bulk_file=bulk_file,
        )
        report["static"] = {
            "discovery_complete": True,
            "eligible_count": static_plan.total,
            "already_valid_count": len(static_plan.already_valid),
            "selected": [_identity_json(candidate.identity) for candidate in static_plan.pending],
            "successful": [],
        }
        static_already_valid = [
            identity.set_code for identity in static_plan.already_valid
        ]
    except Exception:
        failures.append(_failure(stage="static-discovery", category="discovery-failed"))

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
            # A failure in the loop itself must not turn a selected candidate into
            # an unreported success.
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
            with tempfile.TemporaryDirectory(prefix=".profile-refresh-plan-", dir=root) as temporary:
                planning_dir = Path(temporary)
                _prepare_planning_view(static_plan, output_dir=static_dir, planning_dir=planning_dir)
                planned = prepare_profile_data_refresh(
                    profile_selector,
                    card_data_dir=planning_dir,
                    mode=profile_mode,
                    fetch_json=fetch_json,
                    filters_url=FILTERS_ENDPOINT,
                )
                profile_plan = _profile_plan_with_real_paths(planned, card_data_dir=static_dir)
        assert profile_plan is not None
        report["profiles"]["planning_complete"] = True
        report["profiles"]["selected"] = [pair.to_json() for pair in profile_plan.pairs]
    except Exception:
        failures.append(_failure(stage="profile-planning", category="planning-failed"))

    if profile_plan is not None:
        try:
            profile_result = execute_profile_data_refresh(
                profile_plan,
                profiles_dir=profiles_dir,
                cache_dir=cache_dir,
                fetch_json=fetch_json,
                clock=clock,
            )
            report["profiles"]["successful"] = [
                pair.to_json() for pair in profile_result.successful_pairs
            ]
            report["profiles"]["manifest_changed"] = profile_result.manifest_changed
            failures.extend(
                _failure(
                    stage="profile-execution",
                    category=failure.category,
                    set_code=failure.set_code,
                    event_format=failure.event_format,
                )
                for failure in profile_result.failures
            )
        except Exception:
            if not profile_plan.pairs:
                failures.append(
                    _failure(stage="profile-execution", category="execution-failed")
                )
            resolved: set[tuple[str, str]] = set()
            for failure in failures:
                if failure.get("stage") == "profile-execution":
                    code = failure.get("set_code")
                    event_format = failure.get("event_format")
                    if code is not None and event_format is not None:
                        resolved.add((code, event_format.casefold()))
            for pair in profile_plan.pairs:
                if _pair_key(pair) not in resolved:
                    failures.append(
                        _failure(
                            stage="profile-execution",
                            category="execution-failed",
                            set_code=pair.set_code,
                            event_format=pair.event_format,
                        )
                    )

    try:
        report["generated_assets"] = _collect_assets(
            static_already_valid=static_already_valid,
            repo_root=root,
            bundle_dir=bundle,
            base_commit=base_commit,
            static_successes=static_successes,
            profile_result=profile_result,
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
