"""Validate, stage, and publish profile-refresh generation evidence.

The local preparation boundary validates every uploaded byte before the
orchestrator creates an immutable review snapshot or performs the master CAS.
All GitHub and network Git calls remain in this module's small wrappers.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any

import scripts.profile_refresh_workflow as profile_refresh_workflow

from draftomen.profile_client import (
    PROFILE_ARTIFACT_MAX_GZIP_BYTES,
    PROFILE_ARTIFACT_MAX_PROFILE_BYTES,
    _decompress_to_file,
    _validate_profile_metadata,
)
from draftomen.profile_data_refresh import PROFILE_BASE_URL, SUPPORTED_FORMATS
from draftomen.profile_manifest import ProfileManifest, ProfileManifestArtifact, ProfileManifestError
from draftomen.set_card_data import SetCardData, SetCardDataError
from draftomen.set_profile import SetProfile, SetProfileError


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_SAFE_FIELD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_GENERATED_ROOT = Path("website/public")
_CARD_ROOT = _GENERATED_ROOT / "card-data"
_PROFILE_ROOT = _GENERATED_ROOT / "profiles"
_MANIFEST_RELATIVE = (_PROFILE_ROOT / "manifest.json").as_posix()


class ProfileRefreshPublicationError(ValueError):
    """Raised when generation evidence cannot be safely staged."""


def _fail(message: str, error: BaseException | None = None) -> None:
    if error is None:
        raise ProfileRefreshPublicationError(message)
    raise ProfileRefreshPublicationError(message) from error


def _duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    _fail(f"JSON constant {value!r} is not allowed")


def _load_json(path: Path, *, label: str) -> Any:
    try:
        payload = path.read_bytes()
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_json_constant,
        )
    except ProfileRefreshPublicationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError) as error:
        _fail(f"{label} is not valid JSON", error)
    raise AssertionError("unreachable")


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != expected:
        _fail(f"{label} has invalid fields")


def _nonempty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a non-empty string")
    return value


def _safe_code(value: Any, *, label: str) -> str:
    value = _nonempty_string(value, label=label)
    if _SAFE_COMPONENT.fullmatch(value) is None or value != value.casefold():
        _fail(f"{label} is not a safe lowercase component")
    return value


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{label} must be a positive integer")
    return value


def _nonnegative_int_or_none(value: Any, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{label} must be a non-negative integer or null")
    return value


def _validate_identity(value: Any, *, label: str) -> tuple[str, str]:
    item = _mapping(value, label=label)
    _exact_keys(item, {"set_code", "set_name"}, label=label)
    code = _safe_code(item["set_code"], label=f"{label}.set_code")
    name = _nonempty_string(item["set_name"], label=f"{label}.set_name")
    return code, name


def _validate_pair(value: Any, *, label: str) -> tuple[str, str, str]:
    item = _mapping(value, label=label)
    _exact_keys(item, {"event_format", "set_code", "set_name"}, label=label)
    code, name = _validate_identity(
        {"set_code": item["set_code"], "set_name": item["set_name"]},
        label=label,
    )
    event_format = _nonempty_string(item["event_format"], label=f"{label}.event_format")
    if event_format not in SUPPORTED_FORMATS:
        _fail(f"{label}.event_format is unsupported")
    return code, event_format.casefold(), name


def _validate_report(report: Any, *, expected_base: str) -> dict[str, Any]:
    report = _mapping(report, label="result.json")
    _exact_keys(
        report,
        {"schema_version", "base_commit", "selection", "status", "static", "profiles", "failures", "generated_assets"},
        label="result.json",
    )
    if isinstance(report["schema_version"], bool) or not isinstance(report["schema_version"], int) or report["schema_version"] != 1:
        _fail("unsupported report schema")
    if not isinstance(expected_base, str) or _HEX40.fullmatch(expected_base) is None:
        _fail("expected base commit must be a full lowercase SHA")
    if report["base_commit"] != expected_base:
        _fail("report base commit does not match trusted expected base")
    status = report["status"]
    if not isinstance(status, str) or status not in {"success", "failed"}:
        _fail("report status is invalid")

    selection = _mapping(report["selection"], label="selection")
    _exact_keys(selection, {"mode", "selector"}, label="selection")
    mode = selection["mode"]
    if not isinstance(mode, str) or mode not in {"one", "all", "active", "historical"}:
        _fail("selection mode is invalid")
    selector = selection["selector"]
    if selector is not None:
        _nonempty_string(selector, label="selection.selector")
    if (mode == "one") != (selector is not None):
        _fail("selection selector does not match selection mode")

    static = _mapping(report["static"], label="static")
    _exact_keys(
        static,
        {"discovery_complete", "eligible_count", "already_valid_count", "selected", "successful"},
        label="static",
    )
    if not isinstance(static["discovery_complete"], bool):
        _fail("static.discovery_complete must be boolean")
    _nonnegative_int_or_none(static["eligible_count"], label="static.eligible_count")
    _nonnegative_int_or_none(static["already_valid_count"], label="static.already_valid_count")
    if not isinstance(static["selected"], list) or not isinstance(static["successful"], list):
        _fail("static selected and successful fields must be arrays")
    static_identities: dict[str, str] = {}
    for index, identity in enumerate(static["selected"]):
        code, name = _validate_identity(identity, label=f"static.selected[{index}]")
        if code in static_identities:
            _fail("static.selected contains duplicate identities")
        static_identities[code] = name
    static_successes: set[str] = set()
    for index, code_value in enumerate(static["successful"]):
        code = _safe_code(code_value, label=f"static.successful[{index}]")
        if code in static_successes or code not in static_identities:
            _fail("static.successful contains an unselected or duplicate identity")
        static_successes.add(code)

    profiles = _mapping(report["profiles"], label="profiles")
    _exact_keys(
        profiles,
        {"planning_complete", "selected", "successful", "manifest_changed"},
        label="profiles",
    )
    if not isinstance(profiles["planning_complete"], bool) or not isinstance(profiles["manifest_changed"], bool):
        _fail("profile completion fields must be boolean")
    if not isinstance(profiles["selected"], list) or not isinstance(profiles["successful"], list):
        _fail("profile selected and successful fields must be arrays")
    selected_pairs: dict[tuple[str, str], str] = {}
    for index, pair in enumerate(profiles["selected"]):
        code, event_format, name = _validate_pair(pair, label=f"profiles.selected[{index}]")
        identity = (code, event_format)
        if identity in selected_pairs:
            _fail("profiles.selected contains duplicate identities")
        selected_pairs[identity] = name
        if code in static_identities and static_identities[code] != name:
            _fail("profile and static identities disagree")
    successful_pairs: set[tuple[str, str]] = set()
    for index, pair in enumerate(profiles["successful"]):
        code, event_format, name = _validate_pair(pair, label=f"profiles.successful[{index}]")
        identity = (code, event_format)
        if identity in successful_pairs or identity not in selected_pairs or selected_pairs[identity] != name:
            _fail("profiles.successful contains an unselected or duplicate identity")
        successful_pairs.add(identity)

    failures = report["failures"]
    if not isinstance(failures, list):
        _fail("failures must be an array")
    failure_static: set[str] = set()
    failure_pairs: set[tuple[str, str]] = set()
    for index, failure_value in enumerate(failures):
        failure = _mapping(failure_value, label=f"failures[{index}]")
        keys = set(failure)
        if not keys.issubset({"stage", "category", "set_code", "event_format"}) or not {"stage", "category"}.issubset(keys):
            _fail("failure record has invalid fields")
        stage = _nonempty_string(failure["stage"], label=f"failures[{index}].stage")
        category = _nonempty_string(failure["category"], label=f"failures[{index}].category")
        if _SAFE_FIELD.fullmatch(stage) is None or _SAFE_FIELD.fullmatch(category) is None:
            _fail("failure record fields are not safe identifiers")
        code_value = failure.get("set_code")
        format_value = failure.get("event_format")
        if format_value is not None and code_value is None:
            _fail("failure event_format requires set_code")
        if code_value is None:
            continue
        code = _safe_code(code_value, label=f"failures[{index}].set_code")
        if format_value is None:
            if code not in static_identities:
                _fail("failure refers to an unselected static identity")
            if code in failure_static or code in static_successes:
                _fail("static identity is both successful and failed")
            failure_static.add(code)
            continue
        event_format = _nonempty_string(format_value, label=f"failures[{index}].event_format")
        if event_format not in SUPPORTED_FORMATS:
            _fail("failure event_format is unsupported")
        identity = (code, event_format.casefold())
        if identity not in selected_pairs:
            _fail("failure refers to an unselected profile identity")
        if identity in failure_pairs or identity in successful_pairs:
            _fail("profile identity is both successful and failed")
        failure_pairs.add(identity)

    if (status == "success") != (not failures):
        _fail("report status does not match failure evidence")
    if not isinstance(report["generated_assets"], list):
        _fail("generated_assets must be an array")

    return {
        "static_identities": static_identities,
        "static_successes": static_successes,
        "selected_pairs": selected_pairs,
        "successful_pairs": successful_pairs,
        "failure_static": failure_static,
        "failure_pairs": failure_pairs,
    }


def _descriptor_kind(relative: Any) -> tuple[str, str | None]:
    if not isinstance(relative, str) or not relative or relative.startswith("/") or "\\" in relative:
        _fail("generated asset path is not canonical")
    pieces = relative.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces):
        _fail("generated asset path contains an unsafe component")
    if len(pieces) == 4 and pieces[:3] == ["website", "public", "card-data"]:
        code = pieces[3]
        if not code.endswith(".json.gz") or _SAFE_COMPONENT.fullmatch(code[:-8]) is None:
            _fail("generated static path is invalid")
        return "static", code[:-8]
    if relative == _MANIFEST_RELATIVE:
        return "manifest", None
    if len(pieces) == 5 and pieces[:4] == ["website", "public", "profiles", "objects"]:
        digest_name = pieces[4]
        if not digest_name.endswith(".json.gz") or _HEX64.fullmatch(digest_name[:-8]) is None:
            _fail("generated profile object path is invalid")
        return "object", digest_name[:-8]
    _fail("generated asset path is outside the allowlist")
    raise AssertionError("unreachable")


def _assert_no_symlink_components(path: Path, *, stop: Path) -> None:
    try:
        relative = path.relative_to(stop)
    except ValueError:
        _fail("path escapes its containment root")
    current = stop
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            _fail("symlink path components are not allowed")


def _regular_file(path: Path, *, stop: Path, label: str) -> None:
    _assert_no_symlink_components(path, stop=stop)
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        _fail(f"{label} is missing", error)
    if not stat.S_ISREG(mode):
        _fail(f"{label} is not a regular file")


def _scan_generated(generated: Path) -> set[str]:
    try:
        root_mode = generated.lstat().st_mode
    except OSError:
        return set()
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        _fail("generated directory must be a real directory")
    found: set[str] = set()
    for directory, dirnames, filenames in os.walk(generated, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in list(dirnames):
            child = directory_path / name
            try:
                mode = child.lstat().st_mode
            except OSError as error:
                _fail("generated directory entry disappeared", error)
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                _fail("generated path contains a non-directory entry")
        for name in filenames:
            child = directory_path / name
            try:
                mode = child.lstat().st_mode
            except OSError as error:
                _fail("generated file disappeared", error)
            if not stat.S_ISREG(mode):
                _fail("generated entry is not a regular file")
            relative = child.relative_to(generated).as_posix()
            found.add(relative)
    return found


def _git(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        _fail("git validation could not run", error)
    raise AssertionError("unreachable")


def _resolve_commit(root: Path, value: str, *, label: str) -> None:
    if not isinstance(value, str) or _HEX40.fullmatch(value) is None:
        _fail(f"{label} must be a full lowercase commit SHA")
    result = _git(root, ["rev-parse", "--verify", "--quiet", f"{value}^{{commit}}"])
    if result.returncode != 0 or result.stdout.strip() != value:
        _fail(f"{label} does not resolve to a commit")


def _git_status_paths(root: Path) -> tuple[set[str], bool]:
    result = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if result.returncode != 0:
        _fail("git status failed")
    paths: set[str] = set()
    clean = True
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        clean = False
        if len(line) < 4:
            _fail("git status returned an invalid record")
        status = line[:2]
        path = line[3:]
        if "->" in path or status[0] in {"D", "R", "C"} or status[1] in {"D", "R", "C"}:
            _fail("candidate contains a deletion or rename")
        paths.add(path)
    return paths, clean


def _validate_git_base(*, repo_root: Path, expected_base: str, master_commit: str) -> None:
    _resolve_commit(repo_root, expected_base, label="expected base commit")
    _resolve_commit(repo_root, master_commit, label="master commit")
    head = _git(repo_root, ["rev-parse", "--verify", "--quiet", "HEAD"])
    if head.returncode != 0 or head.stdout.strip() != master_commit:
        _fail("publication worktree is not checked out at master commit")
    ancestor = _git(repo_root, ["merge-base", "--is-ancestor", expected_base, master_commit])
    if ancestor.returncode == 1:
        _fail("generation base is stale: it is not an ancestor of master")
    if ancestor.returncode != 0:
        _fail("could not compare generation base with master")
    protected = _git(
        repo_root,
        [
            "diff",
            "--quiet",
            expected_base,
            master_commit,
            "--",
            "website/public/card-data",
            "website/public/profiles",
        ],
    )
    if protected.returncode == 1:
        _fail("generation base is stale: protected website data changed")
    if protected.returncode != 0:
        _fail("could not compare protected website data")


def _read_candidate_file(*, repo_root: Path, relative: str, descriptor_payloads: Mapping[str, bytes]) -> bytes:
    if relative in descriptor_payloads:
        return descriptor_payloads[relative]
    path = repo_root / relative
    _regular_file(path, stop=repo_root, label=f"candidate {relative}")
    try:
        return path.read_bytes()
    except OSError as error:
        _fail(f"candidate {relative} could not be read", error)
    raise AssertionError("unreachable")


def _manifest_from_bytes(payload: bytes, *, label: str) -> ProfileManifest:
    try:
        manifest = ProfileManifest.from_bytes(payload=payload)
    except (ProfileManifestError, TypeError, ValueError) as error:
        _fail(f"{label} is invalid", error)
    if manifest.to_bytes() != payload:
        _fail(f"{label} is not canonical")
    return manifest


def _validate_profile_object(
    *,
    payload: bytes,
    artifact: ProfileManifestArtifact,
    label: str,
) -> None:
    if len(payload) != artifact.gzip_bytes or len(payload) > PROFILE_ARTIFACT_MAX_GZIP_BYTES:
        _fail(f"{label} compressed size does not match manifest")
    if hashlib.sha256(payload).hexdigest() != artifact.gzip_sha256:
        _fail(f"{label} compressed digest does not match manifest")
    with tempfile.TemporaryDirectory(prefix=".profile-refresh-profile-") as temporary:
        source = Path(temporary) / "profile.json.gz"
        raw = Path(temporary) / "profile.json"
        source.write_bytes(payload)
        digest = hashlib.sha256()
        try:
            with raw.open("wb") as output:
                raw_size = _decompress_to_file(
                    path=source,
                    output=output,
                    digest=digest,
                    limit=PROFILE_ARTIFACT_MAX_PROFILE_BYTES,
                    declared=artifact.profile_bytes,
                )
        except Exception as error:
            _fail(f"{label} cannot be safely decompressed", error)
        if raw_size != artifact.profile_bytes or digest.hexdigest() != artifact.profile_sha256:
            _fail(f"{label} raw size or digest does not match manifest")
        raw_bytes = raw.read_bytes()
        try:
            value = json.loads(
                raw_bytes.decode("utf-8"),
                object_pairs_hook=_duplicate_object,
                parse_constant=_reject_json_constant,
            )
            profile = SetProfile.from_json(value=value)
        except ProfileRefreshPublicationError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, SetProfileError, TypeError, ValueError) as error:
            _fail(f"{label} contains invalid profile data", error)
        if profile.to_bytes() != raw_bytes:
            _fail(f"{label} profile JSON is not canonical")
        try:
            _validate_profile_metadata(profile=profile, artifact=artifact)
        except Exception as error:
            _fail(f"{label} metadata does not match manifest", error)




def _validate_and_stage(
    *,
    bundle_dir: Path,
    repo_root: Path,
    master_commit: str,
    evidence: dict[str, Any],
    report: dict[str, Any],
) -> None:
    descriptors = report["generated_assets"]
    descriptor_paths: set[str] = set()
    descriptor_payloads: dict[str, bytes] = {}
    descriptor_kinds: dict[str, tuple[str, str | None]] = {}
    source_generated = bundle_dir / "generated"
    generated_files = _scan_generated(source_generated)
    for index, descriptor_value in enumerate(descriptors):
        descriptor = _mapping(descriptor_value, label=f"generated_assets[{index}]")
        _exact_keys(descriptor, {"path", "sha256", "bytes"}, label=f"generated_assets[{index}]")
        relative = descriptor["path"]
        kind, identity = _descriptor_kind(relative)
        if relative in descriptor_paths:
            _fail("generated_assets contains duplicate paths")
        descriptor_paths.add(relative)
        digest = descriptor["sha256"]
        if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
            _fail("generated asset digest must be lowercase SHA-256")
        size = _positive_int(descriptor["bytes"], label="generated asset bytes")
        source = source_generated / relative
        _regular_file(source, stop=source_generated, label=f"generated asset {relative}")
        try:
            payload = source.read_bytes()
        except OSError as error:
            _fail(f"generated asset {relative} could not be read", error)
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            _fail(f"generated asset {relative} does not match its descriptor")
        descriptor_payloads[relative] = payload
        descriptor_kinds[relative] = (kind, identity)

    if generated_files != descriptor_paths:
        _fail("generated directory contains an undeclared or missing descriptor file")

    static_identities: dict[str, str] = evidence["static_identities"]
    descriptor_static_identities = dict(static_identities)
    for code, event_format in evidence["successful_pairs"]:
        name = evidence["selected_pairs"][(code, event_format)]
        previous_name = descriptor_static_identities.get(code)
        if previous_name is not None and previous_name != name:
            _fail("profile and static identities disagree")
        descriptor_static_identities[code] = name
    for relative, (kind, identity) in descriptor_kinds.items():
        if kind == "static":
            assert identity is not None
            if identity not in descriptor_static_identities or identity in evidence["failure_static"]:
                _fail("generated static data is not a successful or already-valid selection")
            try:
                SetCardData.from_gzip_bytes(
                    payload=descriptor_payloads[relative],
                    expected_set_code=identity,
                    expected_set_name=descriptor_static_identities[identity],
                )
            except (SetCardDataError, TypeError, ValueError) as error:
                _fail(f"generated static data {relative} is invalid", error)
        elif kind == "object" and not evidence["successful_pairs"]:
            _fail("profile object evidence has no successful profile identity")
        elif kind == "manifest" and not evidence["successful_pairs"]:
            _fail("profile manifest evidence has no successful profile identity")

    # Validate the repository's existing tree before any writes.  A clean
    # worktree is part of the disposable-worktree contract.
    status_paths, clean = _git_status_paths(repo_root)
    if not clean or status_paths:
        _fail("publication worktree must be clean before staging")

    expected_changes: set[str] = set()
    for relative, payload in descriptor_payloads.items():
        destination = repo_root / relative
        _assert_no_symlink_components(destination.parent, stop=repo_root)
        try:
            destination_mode = destination.lstat().st_mode
        except FileNotFoundError:
            destination_mode = None
        except OSError as error:
            _fail(f"candidate destination {relative} could not be inspected", error)
        if destination_mode is not None:
            if stat.S_ISLNK(destination_mode):
                _fail("symlink path components are not allowed")
            if not stat.S_ISREG(destination_mode):
                _fail(f"candidate destination {relative} is not a regular file")
            try:
                existing = destination.read_bytes()
            except OSError as error:
                _fail(f"candidate destination {relative} could not be read", error)
            if existing != payload:
                expected_changes.add(relative)
            kind, identity = descriptor_kinds[relative]
            if kind == "object" and existing != payload:
                _fail("content-addressed profile object conflicts with existing bytes")
            if kind == "static" and existing != payload:
                assert identity is not None
                try:
                    SetCardData.from_gzip_bytes(
                        payload=existing,
                        expected_set_code=identity,
                        expected_set_name=descriptor_static_identities[identity],
                    )
                except (SetCardDataError, TypeError, ValueError):
                    pass
                else:
                    _fail("existing valid static data cannot be replaced")
        else:
            expected_changes.add(relative)

    manifest_path = _PROFILE_ROOT / "manifest.json"
    base_manifest: ProfileManifest | None = None
    current_manifest_path = repo_root / manifest_path
    candidate_manifest: ProfileManifest | None = base_manifest
    try:
        current_manifest_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        _fail("base profile manifest could not be inspected", error)
    else:
        _regular_file(current_manifest_path, stop=repo_root, label="base profile manifest")
        try:
            base_manifest = _manifest_from_bytes(current_manifest_path.read_bytes(), label="base profile manifest")
        except OSError as error:
            _fail("base profile manifest could not be read", error)
    if manifest_path.as_posix() in descriptor_payloads:
        candidate_manifest = _manifest_from_bytes(
            descriptor_payloads[manifest_path.as_posix()],
            label="candidate profile manifest",
        )
        if base_manifest is not None:
            prior = {(item.set_code, item.format): item.to_json() for item in base_manifest.artifacts}
            current = {(item.set_code, item.format): item.to_json() for item in candidate_manifest.artifacts}
            for identity, artifact in prior.items():
                if identity not in evidence["successful_pairs"] and current.get(identity) != artifact:
                    _fail("candidate manifest drops or changes an unsuccessful prior identity")
            for identity in current:
                if identity not in prior and identity not in evidence["successful_pairs"]:
                    _fail("candidate manifest introduces an unreported profile identity")
        elif any(
            (item.set_code, item.format) not in evidence["successful_pairs"]
            for item in candidate_manifest.artifacts
        ):
            _fail("candidate manifest introduces an unreported profile identity")
    elif base_manifest is not None:
        candidate_manifest = base_manifest

    if candidate_manifest is None and evidence["successful_pairs"]:
        _fail("successful profiles require a candidate manifest")
    candidate_artifacts = (
        {} if candidate_manifest is None else {(item.set_code, item.format): item for item in candidate_manifest.artifacts}
    )
    successful_artifact_digests: set[str] = set()
    for identity in evidence["successful_pairs"]:
        artifact = candidate_artifacts.get(identity)
        if artifact is None:
            _fail("successful profile identity is missing from candidate manifest")
        if artifact.url != f"{PROFILE_BASE_URL}{artifact.gzip_sha256}.json.gz":
            _fail("successful profile URL does not match its content digest")
        successful_artifact_digests.add(artifact.gzip_sha256)
        object_relative = (_PROFILE_ROOT / "objects" / f"{artifact.gzip_sha256}.json.gz").as_posix()
        object_payload = _read_candidate_file(
            repo_root=repo_root,
            relative=object_relative,
            descriptor_payloads=descriptor_payloads,
        )
        _validate_profile_object(payload=object_payload, artifact=artifact, label=f"profile object {object_relative}")
        static_relative = (_CARD_ROOT / f"{identity[0]}.json.gz").as_posix()
        static_payload = _read_candidate_file(
            repo_root=repo_root,
            relative=static_relative,
            descriptor_payloads=descriptor_payloads,
        )
        try:
            SetCardData.from_gzip_bytes(
                payload=static_payload,
                expected_set_code=identity[0],
                expected_set_name=evidence["selected_pairs"][identity],
            )
        except (SetCardDataError, TypeError, ValueError) as error:
            _fail(f"successful profile static data {static_relative} is invalid", error)

    for relative, (kind, identity) in descriptor_kinds.items():
        if kind == "object":
            assert identity is not None
            if identity not in successful_artifact_digests:
                _fail("new profile object is not referenced by a successful manifest entry")

    # Successful static records must remain valid even when the producer's
    # delta happened to be empty for that path.
    for code in evidence["static_successes"]:
        relative = (_CARD_ROOT / f"{code}.json.gz").as_posix()
        payload = _read_candidate_file(repo_root=repo_root, relative=relative, descriptor_payloads=descriptor_payloads)
        try:
            SetCardData.from_gzip_bytes(
                payload=payload,
                expected_set_code=code,
                expected_set_name=static_identities[code],
            )
        except (SetCardDataError, TypeError, ValueError) as error:
            _fail(f"successful static data {relative} is invalid", error)

    if descriptor_paths and not evidence["static_successes"] and not evidence["successful_pairs"]:
        _fail("generated assets exist without successful work")

    # Only after every descriptor, manifest, profile, and static invariant has
    # passed do we touch the disposable worktree.
    for relative, payload in descriptor_payloads.items():
        destination = repo_root / relative
        _assert_no_symlink_components(destination.parent, stop=repo_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() == payload:
            continue
        try:
            destination.write_bytes(payload)
        except OSError as error:
            _fail(f"could not stage candidate {relative}", error)
        if destination.stat().st_mode & 0o111:
            _fail(f"candidate data file is executable: {relative}")

    changed_paths, clean_after = _git_status_paths(repo_root)
    if changed_paths != expected_changes or clean_after != (not expected_changes):
        _fail("candidate worktree diff does not match declared descriptors")
    for relative in changed_paths:
        path = repo_root / relative
        _regular_file(path, stop=repo_root, label=f"candidate changed file {relative}")
        if path.stat().st_mode & 0o111:
            _fail(f"candidate changed file is executable: {relative}")


def prepare_publication(
    *,
    bundle_dir: Path,
    repo_root: Path,
    expected_base: str,
    master_commit: str,
) -> dict[str, Any]:
    """Validate and stage a complete generation bundle in a disposable tree."""

    bundle = Path(bundle_dir)
    root = Path(repo_root).resolve()
    if not bundle.is_dir() or bundle.is_symlink():
        _fail("bundle directory is unavailable")
    result_path = bundle / "result.json"
    _regular_file(result_path, stop=bundle, label="result.json")
    report = _load_json(result_path, label="result.json")
    evidence = _validate_report(report, expected_base=expected_base)
    _validate_git_base(repo_root=root, expected_base=expected_base, master_commit=master_commit)
    status_paths, clean = _git_status_paths(root)
    if not clean or status_paths:
        _fail("publication worktree must be clean before staging")
    _validate_and_stage(
        bundle_dir=bundle,
        repo_root=root,
        master_commit=master_commit,
        evidence=evidence,
        report=report,
    )
    return report


def _git_network(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a network Git operation without persisting credentials."""
    try:
        return subprocess.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "-C",
                str(root),
                *arguments,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
    except OSError as error:
        _fail("git publication command could not run", error)
    raise AssertionError("unreachable")


def _gh(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one GitHub CLI operation with the workflow's ephemeral token."""
    try:
        return subprocess.run(
            ["gh", *arguments],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
    except OSError as error:
        _fail("GitHub CLI could not run", error)
    raise AssertionError("unreachable")


def _gh_json(root: Path, arguments: Sequence[str]) -> Any:
    result = _gh(root, arguments)
    if result.returncode != 0:
        _fail("GitHub CLI request failed")
    try:
        return json.loads(
            result.stdout,
            object_pairs_hook=_duplicate_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, TypeError, RecursionError) as error:
        _fail("GitHub CLI returned invalid JSON", error)
    raise AssertionError("unreachable")


def _summary_append(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(text)
            if text and not text.endswith("\n"):
                output.write("\n")
    except OSError as error:
        _fail("could not update Actions summary", error)


def _publication_note(summary_file: Path, message: str, *, url: str | None = None) -> None:
    suffix = f"\n- Review PR: {url}" if url else ""
    _summary_append(summary_file, f"\n## Profile publication\n\n- {message}{suffix}\n")


def _commit_sha(root: Path, revision: str) -> str:
    result = _git(root, ["rev-parse", "--verify", f"{revision}^{{commit}}"])
    if result.returncode != 0:
        _fail("Git revision could not be resolved")
    value = result.stdout.strip()
    if _HEX40.fullmatch(value) is None:
        _fail("Git returned an invalid commit SHA")
    return value


def _tree_sha(root: Path, revision: str) -> str:
    result = _git(root, ["rev-parse", "--verify", f"{revision}^{{tree}}"])
    if result.returncode != 0:
        _fail("Git tree could not be resolved")
    value = result.stdout.strip()
    if _HEX40.fullmatch(value) is None:
        _fail("Git returned an invalid tree SHA")
    return value


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = _git(root, ["merge-base", "--is-ancestor", ancestor, descendant])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    _fail("could not determine Git ancestry")
    raise AssertionError("unreachable")


def _commit_parent(root: Path, commit: str) -> str:
    result = _git(root, ["rev-list", "--parents", "-n", "1", commit])
    if result.returncode != 0:
        _fail("publication commit could not be inspected")
    parts = result.stdout.strip().split()
    if len(parts) != 2 or _HEX40.fullmatch(parts[0]) is None or _HEX40.fullmatch(parts[1]) is None:
        _fail("publication snapshot must have exactly one parent")
    if parts[0] != commit:
        _fail("publication snapshot commit identity changed")
    return parts[1]


def _commit_changed_paths(root: Path, parent: str, commit: str) -> set[str]:
    result = _git(root, ["diff", "--name-status", "--no-renames", parent, commit, "--"])
    if result.returncode != 0:
        _fail("publication snapshot diff could not be inspected")
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] not in {"A", "M"}:
            _fail("publication snapshot contains a deletion, rename, or mode change")
        relative = fields[1]
        _descriptor_kind(relative)
        if relative in paths:
            _fail("publication snapshot contains duplicate paths")
        paths.add(relative)
    return paths


def _worktree_add(root: Path, revision: str) -> tuple[Path, Path]:
    parent = Path(tempfile.mkdtemp(prefix=".profile-refresh-worktree-", dir=root.parent))
    worktree = parent / "tree"
    result = _git(root, ["worktree", "add", "--detach", "--force", str(worktree), revision])
    if result.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        _fail("could not create disposable publication worktree")
    return parent, worktree

def _worktree_remove(root: Path, holder: Path, worktree: Path) -> None:
    _git(root, ["worktree", "remove", "--force", str(worktree)])
    shutil.rmtree(holder, ignore_errors=True)


def _validated_snapshot(
    *,
    repo_root: Path,
    bundle_dir: Path,
    expected_base: str,
    head: str,
) -> tuple[str, set[str]]:
    """Validate an existing one-commit snapshot before stale-data checks."""
    parent = _commit_parent(repo_root, head)
    snapshot_paths = _commit_changed_paths(repo_root, parent, head)
    holder, worktree = _worktree_add(repo_root, parent)
    try:
        prepare_publication(
            bundle_dir=bundle_dir,
            repo_root=worktree,
            expected_base=expected_base,
            master_commit=parent,
        )
        candidate_paths, clean = _git_status_paths(worktree)
        if clean or not candidate_paths:
            _fail("existing publication snapshot has no verified changes")
        if candidate_paths != snapshot_paths:
            _fail("existing publication snapshot tree does not match verified evidence")
        add = _git(worktree, ["add", "--", *sorted(candidate_paths)])
        if add.returncode != 0:
            _fail("could not inspect existing publication snapshot")
        candidate_tree = _git(worktree, ["write-tree"])
        if candidate_tree.returncode != 0:
            _fail("could not inspect existing publication snapshot")
        if candidate_tree.stdout.strip() != _tree_sha(repo_root, head):
            _fail("existing publication snapshot tree does not match verified evidence")
    finally:
        _worktree_remove(repo_root, holder, worktree)
    return parent, snapshot_paths


def _fetch_master(repo_root: Path) -> str:
    result = _git_network(
        repo_root,
        ["fetch", "origin", "+refs/heads/master:refs/remotes/origin/master"],
    )
    if result.returncode != 0:
        _fail("could not fetch current master")
    return _commit_sha(repo_root, "refs/remotes/origin/master")


def _fetch_generation_commit(repo_root: Path, expected_base: str) -> None:
    result = _git_network(repo_root, ["fetch", "origin", expected_base])
    if result.returncode != 0:
        _fail("could not fetch generation commit")


def _remote_branch_exists(repo_root: Path, branch: str) -> bool:
    result = _git_network(repo_root, ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"])
    if result.returncode != 0:
        _fail("could not inspect publication branch")
    return any(line.split("\t")[-1] == f"refs/heads/{branch}" for line in result.stdout.splitlines())


def _fetch_branch(repo_root: Path, branch: str) -> str:
    local_ref = f"refs/remotes/origin/{branch}"
    result = _git_network(repo_root, ["fetch", "origin", f"+refs/heads/{branch}:{local_ref}"])
    if result.returncode != 0:
        _fail("could not fetch publication branch")
    return _commit_sha(repo_root, local_ref)


def _fetch_pull_head(repo_root: Path, number: str) -> str:
    local_ref = f"refs/remotes/origin/profile-refresh-pr-{number}"
    result = _git_network(
        repo_root,
        ["fetch", "origin", f"+refs/pull/{number}/head:{local_ref}"],
    )
    if result.returncode != 0:
        _fail("could not fetch pull request head")
    return _commit_sha(repo_root, local_ref)


def _repo_identifier(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
        _fail("repository must be OWNER/REPO")
    return value


def _numeric_id(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.isdigit() or int(value) <= 0:
        _fail(f"{label} must be a positive numeric identifier")
    return value


def _report_has_work(report: Mapping[str, Any]) -> bool:
    static = report["static"]
    profiles = report["profiles"]
    if report["status"] == "success":
        return bool(report["generated_assets"])
    return bool(report["generated_assets"] or static["successful"] or profiles["successful"])


def _commit_candidate(root: Path, *, paths: set[str]) -> str:
    if not paths:
        _fail("cannot commit an empty publication")
    add = _git(root, ["add", "--", *sorted(paths)])
    if add.returncode != 0:
        _fail("could not stage verified publication files")
    cached = _git(root, ["diff", "--cached", "--name-status", "--no-renames", "--"])
    if cached.returncode != 0:
        _fail("could not inspect staged publication files")
    staged: set[str] = set()
    for line in cached.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] not in {"A", "M"}:
            _fail("publication commit contains an invalid file change")
        relative = fields[1]
        _descriptor_kind(relative)
        staged.add(relative)
        path = root / relative
        _regular_file(path, stop=root, label=f"staged file {relative}")
        if path.stat().st_mode & 0o111:
            _fail("publication file is executable")
    if staged != paths:
        _fail("staged publication differs from verified evidence")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "github-actions[bot]",
            "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "github-actions[bot]",
            "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        }
    )
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "Refresh generated website data"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    except OSError as error:
        _fail("could not create publication commit", error)
    if result.returncode != 0:
        _fail("could not create publication commit")
    head = _commit_sha(root, "HEAD")
    _commit_parent(root, head)
    return head


def _push_branch(repo_root: Path, branch: str, head: str) -> None:
    result = _git_network(
        repo_root,
        [
            "push",
            f"--force-with-lease=refs/heads/{branch}:",
            "origin",
            f"{head}:refs/heads/{branch}",
        ],
    )
    if result.returncode != 0:
        _fail("publication branch creation collided or was rejected")


def _push_master(repo_root: Path, master: str, head: str) -> None:
    result = _git_network(
        repo_root,
        [
            "push",
            f"--force-with-lease=refs/heads/master:{master}",
            "origin",
            f"{head}:refs/heads/master",
        ],
    )
    if result.returncode != 0:
        _fail("master changed before publication CAS")


def _pr_list(repo_root: Path, repository: str, branch: str) -> list[dict[str, Any]]:
    value = _gh_json(
        repo_root,
        [
            "pr",
            "list",
            "--repo",
            repository,
            "--state",
            "all",
            "--head",
            branch,
            "--base",
            "master",
            "--json",
            "number,url,state,headRefOid,mergedAt",
        ],
    )
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _fail("GitHub returned an invalid pull request list")
    return value


def _pr_view(repo_root: Path, repository: str, number: str) -> dict[str, Any]:
    value = _gh_json(
        repo_root,
        [
            "pr",
            "view",
            number,
            "--repo",
            repository,
            "--json",
            "url,state,mergedAt,headRefOid",
        ],
    )
    if not isinstance(value, dict):
        _fail("GitHub returned an invalid pull request")
    return value


def _pr_create(
    repo_root: Path,
    repository: str,
    branch: str,
    title: str,
    body_file: Path,
) -> str:
    result = _gh(
        repo_root,
        [
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            "master",
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            str(body_file),
            "--no-maintainer-edit",
        ],
    )
    if result.returncode != 0:
        _fail("could not create publication pull request")
    url = result.stdout.strip()
    if not url or "\n" in url or "\r" in url:
        _fail("GitHub returned an invalid pull request URL")
    return url


def _publication_body(
    *,
    report: Mapping[str, Any],
    run_url: str,
    expected_base: str,
    master_commit: str,
    paths: set[str],
    partial: bool,
) -> str:
    title_note = (
        "Automatic merge is eligible after the final master compare-and-swap."
        if not partial
        else "This partial update remains open for review; failed work is listed below."
    )
    summary = profile_refresh_workflow.render_summary(report)
    return (
        "## Summary\n\n"
        "Refresh generated website data from the profile-refresh workflow.\n\n"
        f"- Workflow run: {run_url}\n"
        f"- Generation base: `{expected_base}`\n"
        f"- Checked master: `{master_commit}`\n"
        f"- {title_note}\n\n"
        "## Changes\n\n"
        + "\n".join(f"- `{path}`" for path in sorted(paths))
        + "\n\n"
        "Selected and successful static/profile work, attribution, and every failure "
        "are included in the validated generation summary below.\n\n"
        "## Scope notes\n\n"
        "Only validated generated website data under the protected roots is included.\n"
        "Profile data attribution and every generation failure follow below.\n\n"
        "## Verification\n\n"
        "The uploaded generation report and every descriptor were validated locally "
        "before publication.\n\n"
        + summary
    )


def publish_website(
    *,
    repo_root: Path,
    bundle_dir: Path,
    expected_base: str,
    repository: str,
    run_id: str,
    artifact_id: str,
    run_url: str,
    summary_file: Path,
) -> int:
    """Validate one generation artifact and safely publish its immutable snapshot."""
    summary_path = Path(summary_file)
    try:
        root = Path(repo_root).resolve()
        bundle = Path(bundle_dir)
        repository = _repo_identifier(repository)
        run_id = _numeric_id(run_id, label="run ID")
        artifact_id = _numeric_id(artifact_id, label="artifact ID")
        if not isinstance(run_url, str) or not run_url.strip() or "\n" in run_url or "\r" in run_url:
            _fail("workflow URL is invalid")
        if not bundle.is_dir() or bundle.is_symlink():
            _fail("bundle directory is unavailable")
        result_path = bundle / "result.json"
        _regular_file(result_path, stop=bundle, label="result.json")
        report = _load_json(result_path, label="result.json")
        _validate_report(report, expected_base=expected_base)
        _summary_append(summary_path, "\n## Profile refresh generation\n\n" + profile_refresh_workflow.render_summary(report))
    except ProfileRefreshPublicationError as error:
        try:
            _publication_note(summary_path, f"Invalid generation bundle: {str(error)[:300]}")
        except ProfileRefreshPublicationError:
            pass
        return 1

    branch = f"automation/profile-refresh-{run_id}-{artifact_id}"
    status_code = 0 if report["status"] == "success" else 1
    if not _report_has_work(report):
        try:
            prepare_publication(
                bundle_dir=Path(bundle_dir),
                repo_root=Path(repo_root),
                expected_base=expected_base,
                master_commit=expected_base,
            )
        except ProfileRefreshPublicationError as error:
            _publication_note(summary_path, f"Publication validation failed: {str(error)[:300]}")
            return 1
        _publication_note(
            summary_path,
            "No changed generated data was validated; no branch, commit, or pull request was created.",
        )
        return status_code

    pr_url: str | None = None
    try:
        _fetch_generation_commit(root, expected_base)
        master_commit = _fetch_master(root)
        matches = _pr_list(root, repository, branch)
        if len(matches) > 1:
            _fail("more than one matching publication pull request exists")
        existing = matches[0] if matches else None
        head: str | None = None
        parent: str | None = None
        snapshot_paths: set[str] | None = None
        merged = False
        state = ""
        if existing is not None:
            number = existing.get("number")
            pr_url_value = existing.get("url")
            reported_head = existing.get("headRefOid")
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                _fail("matching pull request has an invalid number")
            if not isinstance(pr_url_value, str) or not pr_url_value:
                _fail("matching pull request has no URL")
            if not isinstance(reported_head, str) or _HEX40.fullmatch(reported_head) is None:
                _fail("matching pull request has an invalid head SHA")
            pr_url = pr_url_value
            head = _fetch_pull_head(root, str(number))
            if head != reported_head:
                _fail("pull request head changed during publication")
            state = str(existing.get("state", "")).upper()
            merged = state == "MERGED" or bool(existing.get("mergedAt"))
            parent, snapshot_paths = _validated_snapshot(
                repo_root=root,
                bundle_dir=bundle,
                expected_base=expected_base,
                head=head,
            )
            if _is_ancestor(root, head, master_commit):
                if merged:
                    _publication_note(summary_path, "The validated snapshot is already merged.", url=pr_url)
                    return status_code
                if state != "OPEN":
                    _fail("closed or merged publication snapshot is not reachable from master")
                _publication_note(
                    summary_path,
                    "master updated; merge confirmation unavailable",
                    url=pr_url,
                )
                return 1
            if merged or state != "OPEN":
                _fail("closed or merged publication snapshot is not reachable from master")
            if parent != master_commit:
                _fail("existing publication snapshot is stale; dispatch fresh generation")
        else:
            if _remote_branch_exists(root, branch):
                head = _fetch_branch(root, branch)
                parent, snapshot_paths = _validated_snapshot(
                    repo_root=root,
                    bundle_dir=bundle,
                    expected_base=expected_base,
                    head=head,
                )
                if _is_ancestor(root, head, master_commit):
                    _fail("orphan publication branch is already reachable from master")
                if parent != master_commit:
                    _fail("orphan publication snapshot is stale; dispatch fresh generation")

        if head is None:
            holder, candidate = _worktree_add(root, master_commit)
            try:
                prepared = prepare_publication(
                    bundle_dir=bundle,
                    repo_root=candidate,
                    expected_base=expected_base,
                    master_commit=master_commit,
                )
                candidate_paths, clean = _git_status_paths(candidate)
                if clean or not candidate_paths:
                    _publication_note(
                        summary_path,
                        "Validated generation output is unchanged; no branch, commit, or pull request was created.",
                    )
                    return 0 if prepared["status"] == "success" else 1
                head = _commit_candidate(candidate, paths=candidate_paths)
                snapshot_paths = candidate_paths
            finally:
                _worktree_remove(root, holder, candidate)
            _push_branch(root, branch, head)
        else:
            assert parent is not None and snapshot_paths is not None
            if parent != master_commit:
                _fail("publication snapshot parent does not equal checked master")

        assert head is not None and snapshot_paths is not None
        partial = report["status"] == "failed"
        title = "Refresh generated website data (partial)" if partial else "Refresh generated website data"
        if existing is None:
            body = _publication_body(
                report=report,
                run_url=run_url,
                expected_base=expected_base,
                master_commit=master_commit,
                paths=snapshot_paths,
                partial=partial,
            )
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".profile-refresh-pr-",
                suffix=".md",
                dir=root,
                delete=False,
            ) as body_handle:
                body_handle.write(body)
                body_path = Path(body_handle.name)
            try:
                pr_url = _pr_create(root, repository, branch, title, body_path)
            finally:
                body_path.unlink(missing_ok=True)
        if partial:
            _publication_note(
                summary_path,
                "Partial generated data was published for review; automatic merge is not eligible.",
                url=pr_url,
            )
            return 1

        number: str | None = None
        if existing is not None:
            number = str(existing["number"])
        else:
            listed = _pr_list(root, repository, branch)
            if len(listed) != 1:
                _fail("created publication pull request could not be confirmed")
            number_value = listed[0].get("number")
            if isinstance(number_value, bool) or not isinstance(number_value, int) or number_value <= 0:
                _fail("created publication pull request has an invalid number")
            number = str(number_value)
            pr_url = listed[0].get("url")
        assert number is not None
        current_pr = _pr_view(root, repository, number)
        current_state = str(current_pr.get("state", "")).upper()
        current_head = current_pr.get("headRefOid")
        if current_state == "MERGED" and _is_ancestor(root, head, master_commit):
            _publication_note(summary_path, "The validated snapshot is already merged.", url=pr_url)
            return 0
        if current_state != "OPEN" or current_head != head:
            _fail("publication pull request changed before master CAS")
        if _commit_parent(root, head) != master_commit:
            _fail("publication commit parent does not equal checked master")
        _push_master(root, master_commit, head)
        verified_master = _fetch_master(root)
        if not _is_ancestor(root, head, verified_master):
            _fail("master publication could not be verified")
        try:
            final_pr = _pr_view(root, repository, number)
        except ProfileRefreshPublicationError:
            _publication_note(
                summary_path,
                "master updated; merge confirmation unavailable",
                url=pr_url,
            )
            return 0
        final_state = str(final_pr.get("state", "")).upper()
        pr_url = final_pr.get("url") or pr_url
        if final_state != "MERGED" and not final_pr.get("mergedAt"):
            _publication_note(
                summary_path,
                "master updated; merge confirmation unavailable",
                url=pr_url,
            )
            return 0
        _publication_note(summary_path, "Validated generated data was merged into master.", url=pr_url)
        return 0
    except ProfileRefreshPublicationError as error:
        _publication_note(summary_path, f"Publication failed: {str(error)[:300]}", url=pr_url)
        return 1


def _positive_numeric_argument(value: str) -> str:
    if not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("must be a positive numeric identifier")
    return value


def _repository_argument(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
        raise argparse.ArgumentTypeError("must be OWNER/REPO")
    return value


def _sha_argument(value: str) -> str:
    if _HEX40.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("must be a full lowercase commit SHA")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profile_refresh_publication")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--base-commit", type=_sha_argument, required=True)
    parser.add_argument("--repository", type=_repository_argument, required=True)
    parser.add_argument("--run-id", type=_positive_numeric_argument, required=True)
    parser.add_argument("--artifact-id", type=_positive_numeric_argument, required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        return publish_website(
            repo_root=args.repo_root,
            bundle_dir=args.bundle_dir,
            expected_base=args.base_commit,
            repository=args.repository,
            run_id=args.run_id,
            artifact_id=args.artifact_id,
            run_url=args.run_url,
            summary_file=args.summary_file,
        )
    except ProfileRefreshPublicationError:
        return 1


__all__ = [
    "ProfileRefreshPublicationError",
    "main",
    "prepare_publication",
    "publish_website",
]


if __name__ == "__main__":
    raise SystemExit(main())
