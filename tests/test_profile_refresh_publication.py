from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

from draftomen.carddb import build_card_database_from_bulk_file
from draftomen.profile_manifest import ProfileManifest
from draftomen.semantic_roles import Role
from draftomen.set_profile import SetProfile
from draftomen.seventeen import SeventeenLandsError
import scripts.profile_refresh_publication as publication
import scripts.profile_refresh_workflow as workflow
from tests.test_profile_refresh_workflow_helper import (
    NOW,
    _git_base_commit,
    _manifest,
    _partial_producer_bundle,
    _producer_bundle,
    _source,
    _static,
)


def _git_commit(root: Path, message: str) -> str:
    return _git_commit_paths(root, message, ("website",))


def _git_commit_paths(root: Path, message: str, paths: tuple[str, ...]) -> str:
    subprocess.run(["git", "add", "--", *paths], cwd=root, check=True, stdout=subprocess.PIPE)
    environment = os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _candidate_from_base(source: Path, destination: Path, base_commit: str) -> None:
    shutil.copytree(source, destination)
    subprocess.run(["git", "reset", "--hard", base_commit], cwd=destination, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "clean", "-fdx"], cwd=destination, check=True, stdout=subprocess.PIPE)



def test_prepare_publication_stages_real_producer_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    assert report["status"] == "success"
    base_commit = report["base_commit"]

    returned = publication.prepare_publication(
        bundle_dir=bundle,
        repo_root=candidate,
        expected_base=base_commit,
        master_commit=base_commit,
    )

    assert returned == report
    paths = {asset["path"] for asset in report["generated_assets"]}
    assert paths
    for descriptor in report["generated_assets"]:
        relative = descriptor["path"]
        bundle_bytes = (bundle / "generated" / relative).read_bytes()
        candidate_bytes = (candidate / relative).read_bytes()
        assert candidate_bytes == bundle_bytes
        assert descriptor["bytes"] == len(bundle_bytes)
        assert descriptor["sha256"] == hashlib.sha256(bundle_bytes).hexdigest()
    assert all("batch-reports" not in relative for relative in paths)
    assert not (candidate / "website/public/batch-reports").exists()
    assert (candidate / "website/public/card-data/old.json.gz").is_file()

    manifest = ProfileManifest.from_bytes(
        (candidate / "website/public/profiles/manifest.json").read_bytes()
    )
    artifact = manifest.select(set_code="new", event_format="PremierDraft")
    assert artifact is not None
    object_path = candidate / "website/public/profiles/objects" / f"{artifact.gzip_sha256}.json.gz"
    object_bytes = object_path.read_bytes()
    profile_bytes = gzip.decompress(object_bytes)
    assert artifact.gzip_bytes == len(object_bytes)
    assert artifact.gzip_sha256 == hashlib.sha256(object_bytes).hexdigest()
    assert artifact.profile_bytes == len(profile_bytes)
    assert artifact.profile_sha256 == hashlib.sha256(profile_bytes).hexdigest()
    profile = SetProfile.from_json(json.loads(profile_bytes))
    assert profile.roles_are_compatible
    _, source_bulk = _source(tmp_path)
    metadata = build_card_database_from_bulk_file(path=source_bulk)
    resolution = profile.resolve_roles(metadata.cards[1002])
    assert any(assignment.role is Role.DRAW for assignment in resolution.assignments)
    assert any(rating.gih_win_rate.samples > 0 for rating in profile.card_ratings)
    pair = profile.pair("WU")
    assert pair is not None
    assert pair.performance is not None
    assert pair.performance.samples > 0

    old_artifact = manifest.select(set_code="old", event_format="PremierDraft")
    assert old_artifact is not None
    assert (
        candidate / "website/public/profiles/objects" / f"{old_artifact.gzip_sha256}.json.gz"
    ).is_file()


def test_prepare_rejects_protected_root_staleness_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    old_path = candidate / "website/public/card-data/old.json.gz"
    old_path.write_bytes(b"master changed protected data")
    master_commit = _git_commit(candidate, "change protected generated data")

    with pytest.raises(publication.ProfileRefreshPublicationError, match="stale"):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=master_commit,
        )
    assert not (candidate / "website/public/card-data/new.json.gz").exists()


def test_prepare_rejects_descriptor_hash_mismatch_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    descriptor = result["generated_assets"][0]
    descriptor["sha256"] = hashlib.sha256(b"not the payload").hexdigest()
    (bundle / "result.json").write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(publication.ProfileRefreshPublicationError, match="descriptor"):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )
    assert not (candidate / "website/public/card-data/new.json.gz").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 1.0),
        ("status", []),
        ("selection", {"mode": [], "selector": None}),
        ("failures", {}),
        ("generated_assets", {}),
    ],
)
def test_prepare_rejects_malformed_report_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    result[field] = value
    (bundle / "result.json").write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(publication.ProfileRefreshPublicationError):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )


def test_prepare_rejects_traversal_and_undeclared_generated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    result["generated_assets"][0]["path"] = "website/public/card-data/../escape.json.gz"
    (bundle / "result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(publication.ProfileRefreshPublicationError):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )

    # Restore the report and add a file that is not named by any descriptor.
    (bundle / "result.json").write_text(json.dumps(report), encoding="utf-8")
    unexpected = bundle / "generated/website/public/card-data/unlisted.json.gz"
    unexpected.write_bytes(b"unexpected")
    with pytest.raises(publication.ProfileRefreshPublicationError, match="undeclared"):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )


def test_prepare_rejects_generated_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    static_path = bundle / "generated/website/public/card-data/new.json.gz"
    payload = static_path.read_bytes()
    static_path.unlink()
    target = tmp_path / "outside.json.gz"
    target.write_bytes(payload)
    static_path.symlink_to(target)
    with pytest.raises(publication.ProfileRefreshPublicationError):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )


def test_prepare_accepts_empty_success_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    generator = tmp_path / "generator"
    _static(generator / "website/public/card-data", set_code="old", set_name="Old Set")
    inventory, bulk = _source(tmp_path)
    inventory.write_text(json.dumps(["OLD"]), encoding="utf-8")
    _manifest(generator)
    base_commit = _git_base_commit(generator)
    bundle = tmp_path / "bundle"
    report = workflow.generate_website(
        base_commit=base_commit,
        selection_mode="active",
        selector=None,
        repo_root=generator,
        bundle_dir=bundle,
        cache_dir=tmp_path / "cache",
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=lambda _url, _timeout: {
            "formats_by_expansion": {},
            "live_formats_by_expansion": {},
        },
        clock=lambda: NOW,
    )
    candidate = tmp_path / "candidate"
    _candidate_from_base(generator, candidate, base_commit)

    assert report["status"] == "success"
    assert report["static"]["selected"] == []
    assert report["static"]["successful"] == []
    assert report["profiles"]["selected"] == []
    assert report["profiles"]["successful"] == []
    assert report["failures"] == []
    assert report["generated_assets"] == []
    assert publication.prepare_publication(
        bundle_dir=bundle,
        repo_root=candidate,
        expected_base=base_commit,
        master_commit=base_commit,
    ) == report


def _write_bundle_manifest(bundle: Path, result: dict[str, Any], value: dict[str, Any]) -> None:
    payload = ProfileManifest.from_json(value).to_bytes()
    manifest = bundle / "generated/website/public/profiles/manifest.json"
    manifest.write_bytes(payload)
    for descriptor in result["generated_assets"]:
        if descriptor["path"] == "website/public/profiles/manifest.json":
            descriptor["bytes"] = len(payload)
            descriptor["sha256"] = hashlib.sha256(payload).hexdigest()
            break
    else:
        raise AssertionError("producer bundle did not declare its manifest")
    (bundle / "result.json").write_text(json.dumps(result), encoding="utf-8")


def test_prepare_rejects_manifest_dropping_unrelated_prior_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    manifest_path = bundle / "generated/website/public/profiles/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert any(item["set_code"] == "old" for item in manifest["artifacts"])
    manifest["artifacts"] = [item for item in manifest["artifacts"] if item["set_code"] != "old"]
    _write_bundle_manifest(bundle, result, manifest)

    with pytest.raises(publication.ProfileRefreshPublicationError, match="drops"):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )
    assert not (candidate / "website/public/card-data/new.json.gz").exists()


def test_prepare_rejects_manifest_dropping_failed_prior_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, candidate, bundle, published_base, report = _partial_producer_bundle(tmp_path, monkeypatch)
    result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    manifest_path = bundle / "generated/website/public/profiles/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed_artifact = next(
        item
        for item in manifest["artifacts"]
        if item["set_code"] == "new" and item["format"] == "tradDraft".casefold()
    )
    manifest["artifacts"].remove(failed_artifact)
    _write_bundle_manifest(bundle, result, manifest)

    with pytest.raises(publication.ProfileRefreshPublicationError, match="drops"):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=published_base,
            master_commit=published_base,
        )
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=candidate,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        == ""
    )


def test_prepare_rejects_missing_referenced_profile_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    manifest_path = bundle / "generated/website/public/profiles/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        item
        for item in manifest["artifacts"]
        if item["set_code"] == "new" and item["format"] == "premierDraft".casefold()
    )
    missing_digest = "0" * 64
    artifact["gzip_sha256"] = missing_digest
    artifact["url"] = artifact["url"].rsplit("/", 1)[0] + f"/{missing_digest}.json.gz"
    _write_bundle_manifest(bundle, result, manifest)

    with pytest.raises(publication.ProfileRefreshPublicationError, match="candidate .* is missing"):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )
    assert not (candidate / "website/public/card-data/new.json.gz").exists()


def test_prepare_rejects_missing_declared_manifest_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    manifest = bundle / "generated/website/public/profiles/manifest.json"
    manifest.unlink()

    with pytest.raises(publication.ProfileRefreshPublicationError):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )
def _configure_local_origin(root: Path, base_commit: str, origin: Path) -> None:
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=root, check=True, stdout=subprocess.PIPE)
    subprocess.run(
        ["git", "push", "origin", f"{base_commit}:refs/heads/master"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_publish_success_creates_snapshot_pr_and_master_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    unrelated = candidate / "unrelated-source.txt"
    unrelated.write_text("retained source change\n", encoding="utf-8")
    unrelated_commit = _git_commit_paths(candidate, "advance unrelated master source", ("unrelated-source.txt",))
    subprocess.run(
        ["git", "push", "origin", f"{unrelated_commit}:refs/heads/master"],
        cwd=candidate,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    branch = "automation/profile-refresh-123-456"
    state = {"created": False, "views": 0}
    url = "https://github.com/example/repo/pull/7"

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"]:
            if not state["created"]:
                return []
            head = subprocess.run(
                ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.split()[0]
            return [
                {
                    "number": 7,
                    "url": url,
                    "state": "OPEN",
                    "headRefOid": head,
                    "mergedAt": None,
                }
            ]
        state["views"] += 1
        head = subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]
        return {
            "url": url,
            "state": "OPEN" if state["views"] == 1 else "MERGED",
            "mergedAt": None if state["views"] == 1 else NOW.isoformat(),
            "headRefOid": head,
        }

    def fake_gh(_root: Path, _arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["created"] = True
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    summary = tmp_path / "summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="123",
        artifact_id="456",
        run_url="https://github.com/example/repo/actions/runs/123",
        summary_file=summary,
    ) == 0
    master = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    branch_head = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", f"refs/heads/{branch}"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert master == branch_head
    parent = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", f"{branch_head}^"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert parent == unrelated_commit
    assert (
        subprocess.run(
            ["git", "--git-dir", str(origin), "show", f"{branch_head}:unrelated-source.txt"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        == "retained source change\n"
    )
    assert "Validated generated data was merged" in summary.read_text(encoding="utf-8")


def test_publish_partial_keeps_master_unchanged_and_lists_every_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, candidate, bundle, published_base, report = _partial_producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, published_base, origin)
    branch = "automation/profile-refresh-124-457"
    url = "https://github.com/example/repo/pull/8"
    state: dict[str, Any] = {"created": False, "creates": 0, "body": ""}
    branch_pushes: list[str] = []

    def branch_head() -> str:
        return subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        assert arguments[:2] == ["pr", "list"]
        if not state["created"]:
            return []
        return [
            {
                "number": 8,
                "url": url,
                "state": "OPEN",
                "headRefOid": branch_head(),
                "mergedAt": None,
            }
        ]

    def fake_gh(_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["created"] = True
        state["creates"] += 1
        body_index = arguments.index("--body-file") + 1
        state["body"] = Path(arguments[body_index]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    real_push_branch = publication._push_branch

    def track_push_branch(root: Path, branch_name: str, head: str) -> None:
        branch_pushes.append(head)
        real_push_branch(root, branch_name, head)

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    monkeypatch.setattr(publication, "_push_branch", track_push_branch)
    summary = tmp_path / "summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=published_base,
        repository="example/repo",
        run_id="124",
        artifact_id="457",
        run_url="https://github.com/example/repo/actions/runs/124",
        summary_file=summary,
    ) == 1
    assert state["creates"] == 1
    assert workflow.render_summary(report) in state["body"]
    assert all(section in state["body"] for section in ("## Summary", "## Changes", "## Scope notes", "## Verification"))
    assert "Card data from 17Lands (17lands.com)" in state["body"]
    snapshot_head = branch_head()
    assert branch_pushes == [snapshot_head]
    subprocess.run(
        ["git", "push", "origin", f"{snapshot_head}:refs/pull/8/head"],
        cwd=candidate,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second_summary = tmp_path / "second-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=published_base,
        repository="example/repo",
        run_id="124",
        artifact_id="457",
        run_url="https://github.com/example/repo/actions/runs/124",
        summary_file=second_summary,
    ) == 1
    assert state["creates"] == 1
    assert branch_pushes == [snapshot_head]
    assert "Partial generated data was published for review" in second_summary.read_text(encoding="utf-8")
    master = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert master == published_base
    base_manifest = ProfileManifest.from_bytes(
        (candidate / "website/public/profiles/manifest.json").read_bytes()
    )
    branch_manifest = ProfileManifest.from_bytes(
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(origin),
                "show",
                "refs/heads/automation/profile-refresh-124-457:website/public/profiles/manifest.json",
            ],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
    )
    base_failed = base_manifest.select(set_code="new", event_format="TradDraft")
    branch_failed = branch_manifest.select(set_code="new", event_format="TradDraft")
    assert base_failed is not None
    assert branch_failed is not None
    assert branch_failed.to_json() == base_failed.to_json()
    object_path = candidate / "website/public/profiles/objects" / f"{base_failed.gzip_sha256}.json.gz"
    branch_object = subprocess.run(
        [
            "git",
            "--git-dir",
            str(origin),
            "show",
            f"refs/heads/automation/profile-refresh-124-457:website/public/profiles/objects/{base_failed.gzip_sha256}.json.gz",
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    assert branch_object == object_path.read_bytes()
    summary_text = summary.read_text(encoding="utf-8")
    rendered_failures = {
        line[2:].replace("\\-", "-")
        for line in summary_text.splitlines()
        if line.startswith("- ")
    }
    assert "profile-execution: empirical-evidence-unavailable: new: TradDraft" in rendered_failures


    maintained_path = candidate / "website/public/card-data/maintainer.json.gz"
    maintained_path.write_bytes(b"maintainer changed snapshot")
    maintained_head = _git_commit(candidate, "maintainer changes publication snapshot")
    for ref in ("refs/heads/automation/profile-refresh-124-457", "refs/pull/8/head"):
        subprocess.run(
            ["git", "push", "--force", "origin", f"{maintained_head}:{ref}"],
            cwd=candidate,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    conflict_summary = tmp_path / "conflict-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=published_base,
        repository="example/repo",
        run_id="124",
        artifact_id="457",
        run_url="https://github.com/example/repo/actions/runs/124",
        summary_file=conflict_summary,
    ) == 1
    assert state["creates"] == 1
    assert branch_pushes == [snapshot_head]
    assert "snapshot tree does not match verified evidence" in conflict_summary.read_text(encoding="utf-8")


def test_publish_recovers_orphan_snapshot_without_replacing_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    branch = "automation/profile-refresh-132-465"
    url = "https://github.com/example/repo/pull/12"
    state = {"attempts": 0, "created": False, "views": 0}
    branch_pushes: list[str] = []

    def branch_head() -> str:
        return subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"]:
            if not state["created"]:
                return []
            return [
                {
                    "number": 12,
                    "url": url,
                    "state": "OPEN",
                    "headRefOid": branch_head(),
                    "mergedAt": None,
                }
            ]
        state["views"] += 1
        return {
            "url": url,
            "state": "OPEN" if state["views"] == 1 else "MERGED",
            "mergedAt": None if state["views"] == 1 else NOW.isoformat(),
            "headRefOid": branch_head(),
        }

    def fake_gh(_root: Path, _arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["attempts"] += 1
        if state["attempts"] == 1:
            return subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="temporary API failure")
        state["created"] = True
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    real_push_branch = publication._push_branch

    def track_push_branch(root: Path, branch_name: str, head: str) -> None:
        branch_pushes.append(head)
        real_push_branch(root, branch_name, head)

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    monkeypatch.setattr(publication, "_push_branch", track_push_branch)
    first_summary = tmp_path / "orphan-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="132",
        artifact_id="465",
        run_url="https://github.com/example/repo/actions/runs/132",
        summary_file=first_summary,
    ) == 1
    orphan_head = branch_head()
    assert state["created"] is False
    second_summary = tmp_path / "recovered-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="132",
        artifact_id="465",
        run_url="https://github.com/example/repo/actions/runs/132",
        summary_file=second_summary,
    ) == 0
    assert branch_pushes == [orphan_head]
    recovered_master = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert recovered_master == orphan_head
    assert "Validated generated data was merged" in second_summary.read_text(encoding="utf-8")


def test_publish_post_cas_github_failure_reports_master_updated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    branch = "automation/profile-refresh-133-466"
    url = "https://github.com/example/repo/pull/13"
    state = {"created": False, "views": 0}

    def branch_head() -> str:
        return subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"]:
            if not state["created"]:
                return []
            return [
                {
                    "number": 13,
                    "url": url,
                    "state": "OPEN",
                    "headRefOid": branch_head(),
                    "mergedAt": None,
                }
            ]
        state["views"] += 1
        if state["views"] == 2:
            publication._fail("GitHub API outage after CAS")
        head = branch_head()
        return {
            "url": url,
            "state": "OPEN",
            "mergedAt": None,
            "headRefOid": head,
        }

    def fake_gh(_root: Path, _arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["created"] = True
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    summary = tmp_path / "post-cas-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="133",
        artifact_id="466",
        run_url="https://github.com/example/repo/actions/runs/133",
        summary_file=summary,
    ) == 0
    master = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    branch_head_value = branch_head()
    assert master == branch_head_value
    subprocess.run(
        ["git", "--git-dir", str(origin), "merge-base", "--is-ancestor", branch_head_value, master],
        check=True,
        stdout=subprocess.PIPE,
    )
    summary_text = summary.read_text(encoding="utf-8")
    assert "master updated; merge confirmation unavailable" in summary_text
    assert url in summary_text
    assert "Publication failed" not in summary_text



def test_publish_stale_open_metadata_after_cas_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    branch = "automation/profile-refresh-134-467"
    url = "https://github.com/example/repo/pull/14"
    state = {"created": False}

    def branch_head() -> str:
        return subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"] and not state["created"]:
            return []
        head = branch_head()
        pull = {
            "number": 14,
            "url": url,
            "state": "OPEN",
            "headRefOid": head,
            "mergedAt": None,
        }
        if arguments[:2] == ["pr", "list"]:
            return [pull]
        return {key: value for key, value in pull.items() if key != "number"}

    def fake_gh(_root: Path, _arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["created"] = True
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    summary = tmp_path / "stale-open-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="134",
        artifact_id="467",
        run_url="https://github.com/example/repo/actions/runs/134",
        summary_file=summary,
    ) == 0
    master = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    branch_head_value = branch_head()
    assert master == branch_head_value
    subprocess.run(
        ["git", "--git-dir", str(origin), "merge-base", "--is-ancestor", branch_head_value, master],
        check=True,
        stdout=subprocess.PIPE,
    )
    summary_text = summary.read_text(encoding="utf-8")
    assert "master updated; merge confirmation unavailable" in summary_text
    assert "Validated generated data was merged into master." not in summary_text


def test_publish_post_cas_master_fetch_failure_remains_hard_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    branch = "automation/profile-refresh-135-468"
    url = "https://github.com/example/repo/pull/15"
    state = {"created": False, "fetches": 0}

    def branch_head() -> str:
        return subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"] and not state["created"]:
            return []
        head = branch_head()
        pull = {
            "number": 15,
            "url": url,
            "state": "OPEN",
            "headRefOid": head,
            "mergedAt": None,
        }
        if arguments[:2] == ["pr", "list"]:
            return [pull]
        return {key: value for key, value in pull.items() if key != "number"}

    def fake_gh(_root: Path, _arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["created"] = True
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    real_fetch_master = publication._fetch_master

    def fail_after_cas(root: Path) -> str:
        state["fetches"] += 1
        if state["fetches"] == 2:
            publication._fail("post-CAS master fetch failed")
        return real_fetch_master(root)

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    monkeypatch.setattr(publication, "_fetch_master", fail_after_cas)
    summary = tmp_path / "fetch-failure-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="135",
        artifact_id="468",
        run_url="https://github.com/example/repo/actions/runs/135",
        summary_file=summary,
    ) == 1
    master = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    branch_head_value = branch_head()
    assert master == branch_head_value
    assert "post-CAS master fetch failed" in summary.read_text(encoding="utf-8")
    assert "master updated; merge confirmation unavailable" not in summary.read_text(encoding="utf-8")


def test_publish_post_cas_master_ancestry_failure_remains_hard_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    branch = "automation/profile-refresh-136-469"
    url = "https://github.com/example/repo/pull/16"
    state = {"created": False}

    def branch_head() -> str:
        return subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"] and not state["created"]:
            return []
        head = branch_head()
        pull = {
            "number": 16,
            "url": url,
            "state": "OPEN",
            "headRefOid": head,
            "mergedAt": None,
        }
        if arguments[:2] == ["pr", "list"]:
            return [pull]
        return {key: value for key, value in pull.items() if key != "number"}

    def fake_gh(_root: Path, _arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["created"] = True
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    real_is_ancestor = publication._is_ancestor

    def fail_after_cas(root: Path, ancestor: str, descendant: str) -> bool:
        assert real_is_ancestor(root, ancestor, descendant)
        return False

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    monkeypatch.setattr(publication, "_is_ancestor", fail_after_cas)
    summary = tmp_path / "ancestry-failure-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="136",
        artifact_id="469",
        run_url="https://github.com/example/repo/actions/runs/136",
        summary_file=summary,
    ) == 1
    master = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert master == branch_head()
    assert "master publication could not be verified" in summary.read_text(encoding="utf-8")
    assert "master updated; merge confirmation unavailable" not in summary.read_text(encoding="utf-8")


def test_publish_genuine_no_work_does_not_query_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    generator = tmp_path / "generator"
    _static(generator / "website/public/card-data", set_code="old", set_name="Old Set")
    inventory, bulk = _source(tmp_path)
    inventory.write_text(json.dumps(["OLD"]), encoding="utf-8")
    _manifest(generator)
    base_commit = _git_base_commit(generator)
    bundle = tmp_path / "bundle"
    report = workflow.generate_website(
        base_commit=base_commit,
        selection_mode="active",
        selector=None,
        repo_root=generator,
        bundle_dir=bundle,
        cache_dir=tmp_path / "cache",
        inventory_file=inventory,
        bulk_file=bulk,
        fetch_json=lambda _url, _timeout: {
            "formats_by_expansion": {},
            "live_formats_by_expansion": {},
        },
        clock=lambda: NOW,
    )
    assert report["status"] == "success"
    monkeypatch.setattr(
        publication,
        "_gh_json",
        lambda *_args, **_kwargs: pytest.fail("no-work publication queried GitHub"),
    )
    summary = tmp_path / "summary.md"
    candidate = tmp_path / "candidate"
    _candidate_from_base(generator, candidate, base_commit)
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=base_commit,
        repository="example/repo",
        run_id="125",
        artifact_id="458",
        run_url="https://github.com/example/repo/actions/runs/125",
        summary_file=summary,
    ) == 0
    assert "no branch" in summary.read_text(encoding="utf-8")


def test_publish_rerun_reuses_open_snapshot_when_merge_confirmation_is_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    branch = "automation/profile-refresh-126-459"
    state = {"created": False, "creates": 0, "merged": False, "closed": False}
    url = "https://github.com/example/repo/pull/9"

    def branch_head() -> str:
        return subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"]:
            if not state["created"]:
                return []
            return [
                {
                    "number": 9,
                    "url": url,
                    "state": "CLOSED" if state["closed"] else ("MERGED" if state["merged"] else "OPEN"),
                    "headRefOid": branch_head(),
                    "mergedAt": NOW.isoformat() if state["merged"] else None,
                }
            ]
        return {
            "url": url,
            "state": "OPEN",
            "mergedAt": None,
            "headRefOid": branch_head(),
        }
    def fake_gh(_root: Path, _arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["created"] = True
        state["creates"] += 1
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    first_summary = tmp_path / "first-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="126",
        artifact_id="459",
        run_url="https://github.com/example/repo/actions/runs/126",
        summary_file=first_summary,
    ) == 0
    snapshot_head = branch_head()
    subprocess.run(
        ["git", "push", "origin", f"{snapshot_head}:refs/pull/9/head"],
        cwd=candidate,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second_summary = tmp_path / "second-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="126",
        artifact_id="459",
        run_url="https://github.com/example/repo/actions/runs/126",
        summary_file=second_summary,
    ) == 1
    assert state["creates"] == 1
    assert "master updated; merge confirmation unavailable" in second_summary.read_text(encoding="utf-8")
    state["closed"] = True
    closed_summary = tmp_path / "closed-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="126",
        artifact_id="459",
        run_url="https://github.com/example/repo/actions/runs/126",
        summary_file=closed_summary,
    ) == 1
    assert "closed or merged" in closed_summary.read_text(encoding="utf-8")
    state["closed"] = False
    state["merged"] = True
    merged_summary = tmp_path / "merged-summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="126",
        artifact_id="459",
        run_url="https://github.com/example/repo/actions/runs/126",
        summary_file=merged_summary,
    ) == 0




def test_publish_all_failed_creates_no_snapshot_or_pull_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import draftomen.card_data_export as card_export

    monkeypatch.setattr(card_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)
    root = tmp_path / "generator"
    _static(root / "website/public/card-data", set_code="old", set_name="Old Set")
    _manifest(root)
    base_commit = _git_base_commit(root)
    missing_inventory = tmp_path / "missing-inventory.json"
    missing_bulk = tmp_path / "missing-cards.jsonl"
    bundle = tmp_path / "bundle"
    report = workflow.generate_website(
        base_commit=base_commit,
        selection_mode="all",
        selector=None,
        repo_root=root,
        bundle_dir=bundle,
        cache_dir=tmp_path / "cache",
        inventory_file=missing_inventory,
        bulk_file=missing_bulk,
        fetch_json=lambda _url, _timeout: (_ for _ in ()).throw(
            SeventeenLandsError("injected filters outage")
        ),
        clock=lambda: NOW,
    )
    assert report["status"] == "failed"
    assert report["static"]["successful"] == []
    assert report["profiles"]["successful"] == []
    assert report["generated_assets"] == []
    assert {failure["stage"] for failure in report["failures"]} == {
        "static-discovery",
        "profile-planning",
    }
    candidate = tmp_path / "candidate"
    _candidate_from_base(root, candidate, base_commit)
    monkeypatch.setattr(
        publication,
        "_gh_json",
        lambda *_args, **_kwargs: pytest.fail("all-failed publication queried GitHub"),
    )
    summary = tmp_path / "summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=base_commit,
        repository="example/repo",
        run_id="127",
        artifact_id="460",
        run_url="https://github.com/example/repo/actions/runs/127",
        summary_file=summary,
    ) == 1
    summary_text = summary.read_text(encoding="utf-8")
    rendered_failures = {
        line[2:].replace("\\-", "-")
        for line in summary_text.splitlines()
        if line.startswith("- ")
    }
    assert {
        "static-discovery: discovery-failed",
        "profile-planning: planning-failed",
    }.issubset(rendered_failures)


def test_publish_master_lease_rejects_late_advance_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    branch = "automation/profile-refresh-128-461"
    url = "https://github.com/example/repo/pull/10"
    state = {"created": False}

    def branch_head() -> str:
        return subprocess.run(
            ["git", "ls-remote", str(origin), f"refs/heads/{branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"] and not state["created"]:
            return []
        pull = {
            "number": 10,
            "url": url,
            "state": "OPEN",
            "headRefOid": branch_head(),
            "mergedAt": None,
        }
        if arguments[:2] == ["pr", "list"]:
            return [pull]
        return {
            "url": url,
            "state": "OPEN",
            "headRefOid": branch_head(),
            "mergedAt": None,
        }

    def fake_gh(_root: Path, _arguments: list[str]) -> subprocess.CompletedProcess[str]:
        state["created"] = True
        return subprocess.CompletedProcess(["gh"], 0, stdout=url + "\n", stderr="")

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_gh", fake_gh)
    real_push_master = publication._push_master

    def race(root: Path, master: str, head: str) -> None:
        unrelated = root / "unrelated-source.txt"
        unrelated.write_text("late master advance\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", unrelated.name], cwd=root, check=True, stdout=subprocess.PIPE)
        environment = os.environ | {
            "GIT_AUTHOR_NAME": "Race",
            "GIT_AUTHOR_EMAIL": "race@example.invalid",
            "GIT_COMMITTER_NAME": "Race",
            "GIT_COMMITTER_EMAIL": "race@example.invalid",
        }
        subprocess.run(
            ["git", "commit", "-m", "unrelated master change"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:refs/heads/master"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        real_push_master(root, master, head)

    monkeypatch.setattr(publication, "_push_master", race)
    summary = tmp_path / "summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="128",
        artifact_id="461",
        run_url="https://github.com/example/repo/actions/runs/128",
        summary_file=summary,
    ) == 1
    final_master = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert final_master != report["base_commit"]
    assert "master changed before publication CAS" in summary.read_text(encoding="utf-8")


def test_publish_rejects_mutated_pull_head_before_snapshot_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    url = "https://github.com/example/repo/pull/11"
    reported_head = "1" * 40

    def fake_gh_json(_root: Path, arguments: list[str]) -> Any:
        assert arguments[:2] == ["pr", "list"]
        return [
            {
                "number": 11,
                "url": url,
                "state": "OPEN",
                "headRefOid": reported_head,
                "mergedAt": None,
            }
        ]

    monkeypatch.setattr(publication, "_gh_json", fake_gh_json)
    monkeypatch.setattr(publication, "_fetch_pull_head", lambda *_args, **_kwargs: report["base_commit"])
    summary = tmp_path / "summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="129",
        artifact_id="462",
        run_url="https://github.com/example/repo/actions/runs/129",
        summary_file=summary,
    ) == 1
    assert "pull request head changed" in summary.read_text(encoding="utf-8")
    assert (
        subprocess.run(
            ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        == report["base_commit"]
    )


def test_publish_github_api_failure_leaves_master_and_branch_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    monkeypatch.setattr(
        publication,
        "_gh_json",
        lambda *_args, **_kwargs: publication._fail("GitHub API outage"),
    )
    summary = tmp_path / "summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="130",
        artifact_id="463",
        run_url="https://github.com/example/repo/actions/runs/130",
        summary_file=summary,
    ) == 1
    assert (
        subprocess.run(
            ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/master"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        == report["base_commit"]
    )
    assert "GitHub API outage" in summary.read_text(encoding="utf-8")


def test_publish_rejects_master_protected_root_change_before_pr_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    old_static = candidate / "website/public/card-data/old.json.gz"
    old_static.write_bytes(b"master changed protected static bytes")
    stale_master = _git_commit(candidate, "change protected static data")
    subprocess.run(
        ["git", "push", "origin", f"{stale_master}:refs/heads/master"],
        cwd=candidate,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    monkeypatch.setattr(publication, "_gh_json", lambda *_args, **_kwargs: [])
    summary = tmp_path / "summary.md"
    assert publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=report["base_commit"],
        repository="example/repo",
        run_id="131",
        artifact_id="464",
        run_url="https://github.com/example/repo/actions/runs/131",
        summary_file=summary,
    ) == 1
    assert "protected website data changed" in summary.read_text(encoding="utf-8")
