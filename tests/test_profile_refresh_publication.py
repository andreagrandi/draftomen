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

from draftomen.profile_manifest import ProfileManifest
from draftomen.set_profile import SetProfile
from draftomen.sets_manifest import SetsManifest
from draftomen.seventeen import SeventeenLandsError
import scripts.profile_refresh_publication as publication
import scripts.profile_refresh_workflow as workflow
from tests.test_profile_refresh_workflow_helper import (
    NOW,
    _OLD_CARD_AS_BONUS,
    _THIN_CARD_AS_BONUS,
    _NEW_PREMIER,
    _committed_card_data_workspace,
    _filters,
    _generate_rebuild,
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
    profile_json = json.loads(profile_bytes)
    profile = SetProfile.from_json(profile_json)
    assert profile.schema_version == 4
    assert {"role_profile", "enhancement", "enhancement_status"}.isdisjoint(profile_json)
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


def test_prepare_publication_sets_manifest_checksum_matches_rebuilt_card_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _committed_card_data_workspace(tmp_path, monkeypatch)
    generator, _inventory, _bulk, base_commit = workspace
    relative = "website/public/card-data/new.json.gz"
    committed = (generator / relative).read_bytes()
    report = _generate_rebuild(
        tmp_path,
        workspace,
        selection_mode="active",
        filters=_filters(formats=_NEW_PREMIER, live=_NEW_PREMIER),
        fetch_rated_cards=lambda *, set_code: _OLD_CARD_AS_BONUS,
    )
    assert report["status"] == "success"
    assert relative in {asset["path"] for asset in report["generated_assets"]}
    candidate = tmp_path / "candidate"
    _candidate_from_base(generator, candidate, base_commit)
    assert (candidate / relative).read_bytes() == committed

    publication.prepare_publication(
        bundle_dir=tmp_path / "bundle",
        repo_root=candidate,
        expected_base=base_commit,
        master_commit=base_commit,
    )

    staged = (candidate / relative).read_bytes()
    assert staged != committed
    entry = SetsManifest.from_bytes(
        (candidate / "website/public/sets/manifest.json").read_bytes()
    ).select(set_code="new")
    assert entry is not None
    assert entry.card_data.sha256 == hashlib.sha256(staged).hexdigest()
    assert entry.card_data.bytes == len(staged)


def test_prepare_publication_accepts_rebuild_failure_next_to_a_rebuilt_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _committed_card_data_workspace(tmp_path, monkeypatch)
    generator, _inventory, _bulk, base_commit = workspace

    def fetch(*, set_code: str) -> Any:
        if set_code == "new":
            raise SeventeenLandsError("fixture ratings outage")
        return _THIN_CARD_AS_BONUS

    report = _generate_rebuild(
        tmp_path,
        workspace,
        selection_mode="active",
        filters=_filters(
            formats={**_NEW_PREMIER, "OLD": ["PremierDraft"]},
            live={**_NEW_PREMIER, "OLD": ["PremierDraft"]},
        ),
        fetch_rated_cards=fetch,
    )
    assert report["static"]["rebuilt"] == ["old"]
    candidate = tmp_path / "candidate"
    _candidate_from_base(generator, candidate, base_commit)

    publication.prepare_publication(
        bundle_dir=tmp_path / "bundle",
        repo_root=candidate,
        expected_base=base_commit,
        master_commit=base_commit,
    )

    assert (candidate / "website/public/card-data/old.json.gz").read_bytes() == (
        generator / "website/public/card-data/old.json.gz"
    ).read_bytes()


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


@pytest.mark.parametrize(
    "card_ratings",
    [
        None,
        [{"set_code": "new", "event_format": "PremierDraft", "fetched": 2, "accepted": 1}],
        [
            {
                "set_code": "old",
                "event_format": "PremierDraft",
                "fetched": 2,
                "accepted": 1,
                "from_other_formats": 0,
                "rejected": {},
            }
        ],
        [
            {
                "set_code": "new",
                "event_format": "PremierDraft",
                "fetched": -1,
                "accepted": 1,
                "from_other_formats": 0,
                "rejected": {},
            }
        ],
        [
            {
                "set_code": "new",
                "event_format": "PremierDraft",
                "fetched": 2,
                "accepted": 1,
                "from_other_formats": 0,
                "rejected": {"/tmp/raw": 1},
            }
        ],
    ],
)
def test_prepare_rejects_malformed_card_rating_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    card_ratings: Any,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    assert result["profiles"]["card_ratings"] == [
        {
            "set_code": "new",
            "event_format": "PremierDraft",
            "fetched": 2,
            "accepted": 1,
            "from_other_formats": 0,
            "rejected": {"card_rating_unmatched_metadata": 1},
        }
    ]
    result["profiles"]["card_ratings"] = card_ratings
    (bundle / "result.json").write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(publication.ProfileRefreshPublicationError, match="card_ratings"):
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
        if item["set_code"] == "new" and item["format"] == "premierdraft"
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

    dispatches: list[list[str]] = []

    def fake_gh(_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ["workflow", "run"]:
            assert not state["created"]
            dispatches.append(arguments)
            return subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")
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
    assert dispatches == [
        ["workflow", "run", "ci.yml", "--repo", "example/repo", "--ref", branch],
    ]
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
    assert len(dispatches) == 1
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
    base_failed = base_manifest.select(set_code="new", event_format="PremierDraft")
    branch_failed = branch_manifest.select(set_code="new", event_format="PremierDraft")
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
    assert "profile-execution: empirical-evidence-unavailable: new: PremierDraft" in rendered_failures


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


@pytest.mark.parametrize(
    "fallbacks",
    [None, "new", ["NEW"], [1], ["../new"]],
)
def test_prepare_rejects_malformed_card_metadata_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fallbacks: Any,
) -> None:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
    assert result["profiles"]["card_metadata_fallbacks"] == []
    result["profiles"]["card_metadata_fallbacks"] = fallbacks
    (bundle / "result.json").write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(publication.ProfileRefreshPublicationError):
        publication.prepare_publication(
            bundle_dir=bundle,
            repo_root=candidate,
            expected_base=report["base_commit"],
            master_commit=report["base_commit"],
        )


class _FakeGitHub:
    """Stand-in for the gh calls the publisher makes, backed by a local origin."""

    def __init__(self, *, origin: Path, branch: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.origin = origin
        self.branch = branch
        self.events: list[str] = []
        self.pr: dict[str, Any] | None = None
        self.pr_creates = 0
        self.pr_body = ""
        self.runs: list[dict[str, Any]] = []
        self.ci_conclusion = "success"
        self.pending_polls = 0
        self.dispatch_ok = True
        self.run_list_value: Any = None
        self.view_failures: list[bool] = []
        self.sleeps: list[float] = []
        self._now = 0.0
        self._pending: dict[str, Any] | None = None
        monkeypatch.setattr(publication, "_gh_json", self.gh_json)
        monkeypatch.setattr(publication, "_gh", self.gh)
        monkeypatch.setattr(publication, "_ci_sleep", self._sleep)
        monkeypatch.setattr(publication, "_ci_clock", lambda: self._now)
        real_push_master = publication._push_master

        def tracked_push_master(root: Path, master: str, head: str) -> None:
            self.events.append("push-master")
            real_push_master(root, master, head)

        monkeypatch.setattr(publication, "_push_master", tracked_push_master)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds

    def branch_head(self) -> str:
        return subprocess.run(
            ["git", "ls-remote", str(self.origin), f"refs/heads/{self.branch}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.split()[0]

    def branch_exists(self) -> bool:
        return bool(
            subprocess.run(
                ["git", "ls-remote", str(self.origin), f"refs/heads/{self.branch}"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
        )

    def master(self) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", "refs/heads/master"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def open_pr(self, *, root: Path, number: int) -> None:
        """Attach an open PR to the branch as it stands now."""
        head = self.branch_head()
        subprocess.run(
            ["git", "push", "origin", f"{head}:refs/pull/{number}/head"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.pr = {
            "number": number,
            "url": f"https://github.com/example/repo/pull/{number}",
            "state": "OPEN",
            "mergedAt": None,
        }

    def gh_json(self, _root: Path, arguments: list[str]) -> Any:
        if arguments[:2] == ["pr", "list"]:
            if self.pr is None:
                return []
            return [{**self.pr, "headRefOid": self.branch_head()}]
        if arguments[:2] == ["pr", "view"]:
            assert self.pr is not None
            if self.view_failures and self.view_failures.pop(0):
                publication._fail("GitHub API outage")
            pull = {key: value for key, value in self.pr.items() if key != "number"}
            return {**pull, "headRefOid": self.branch_head()}
        assert arguments[:2] == ["run", "list"]
        assert arguments[arguments.index("--workflow") + 1] == "ci.yml"
        if self.run_list_value is not None:
            return self.run_list_value
        if self._pending is not None:
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return [*self.runs, {"databaseId": self._pending["databaseId"], "status": "in_progress", "conclusion": ""}]
            self.runs.append(self._pending)
            self._pending = None
        if any(run["status"] == "completed" and run["conclusion"] == "success" for run in self.runs):
            self.events.append("ci-success-seen")
        return list(self.runs)

    def gh(self, _root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ["workflow", "run"]:
            self.events.append("dispatch")
            assert arguments == ["workflow", "run", "ci.yml", "--repo", "example/repo", "--ref", self.branch]
            if not self.dispatch_ok:
                return subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="dispatch refused")
            self._pending = {
                "databaseId": 900 + len(self.runs),
                "status": "completed",
                "conclusion": self.ci_conclusion,
            }
            return subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")
        assert arguments[:2] == ["pr", "create"]
        self.events.append("pr-create")
        self.pr_creates += 1
        self.pr_body = Path(arguments[arguments.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.pr = {
            "number": 50,
            "url": "https://github.com/example/repo/pull/50",
            "state": "OPEN",
            "mergedAt": None,
        }
        return subprocess.CompletedProcess(["gh"], 0, stdout=self.pr["url"] + "\n", stderr="")


def _publish(
    *,
    candidate: Path,
    bundle: Path,
    base: str,
    summary: Path,
    run_id: str = "123",
    artifact_id: str = "456",
) -> int:
    return publication.publish_website(
        repo_root=candidate,
        bundle_dir=bundle,
        expected_base=base,
        repository="example/repo",
        run_id=run_id,
        artifact_id=artifact_id,
        run_url=f"https://github.com/example/repo/actions/runs/{run_id}",
        summary_file=summary,
    )


def _setup_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, dict[str, Any], Path, _FakeGitHub]:
    _generator, candidate, bundle, report = _producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, report["base_commit"], origin)
    github = _FakeGitHub(origin=origin, branch="automation/profile-refresh-123-456", monkeypatch=monkeypatch)
    return candidate, bundle, report, origin, github


def test_publish_success_pushes_master_after_ci_without_pull_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, origin, github = _setup_success(tmp_path, monkeypatch)
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
    github.pending_polls = 2
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 0
    assert github.pr_creates == 0
    assert github.events == ["dispatch", "ci-success-seen", "push-master"]
    assert github.sleeps == [20, 20]
    master = github.master()
    parent = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", f"{master}^"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    assert parent == unrelated_commit
    assert (
        subprocess.run(
            ["git", "--git-dir", str(origin), "show", f"{master}:unrelated-source.txt"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        == "retained source change\n"
    )
    sets_manifest = SetsManifest.from_bytes(
        subprocess.run(
            ["git", "--git-dir", str(origin), "show", f"{master}:website/public/sets/manifest.json"],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
    )
    new_set = sets_manifest.select(set_code="new")
    assert new_set is not None
    assert new_set.profiles == {"premierdraft": "early"}
    assert not github.branch_exists()
    assert "pushed to master after CI passed" in summary.read_text(encoding="utf-8")


def test_publish_success_skips_dispatch_when_ci_already_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    github.runs = [
        {"databaseId": 7, "status": "completed", "conclusion": "failure"},
        {"databaseId": 8, "status": "completed", "conclusion": "success"},
    ]
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 0
    assert "dispatch" not in github.events
    assert github.master() != report["base_commit"]


def test_publish_ci_failure_keeps_master_untouched_and_branch_for_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    github.ci_conclusion = "failure"
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert github.events == ["dispatch"]
    assert github.master() == report["base_commit"]
    assert github.branch_exists()
    assert github.pr_creates == 0
    text = summary.read_text(encoding="utf-8")
    assert "CI finished with conclusion failure" in text


def test_publish_ci_timeout_keeps_master_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    github.pending_polls = 10**6
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert sum(github.sleeps) == 20 * 60
    assert "push-master" not in github.events
    assert github.master() == report["base_commit"]
    assert github.branch_exists()
    assert "timed out waiting for CI" in summary.read_text(encoding="utf-8")


def test_publish_dispatch_failure_on_success_blocks_master_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    github.dispatch_ok = False
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert github.master() == report["base_commit"]
    assert "could not dispatch CI" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "value",
    [
        {"databaseId": 1},
        "not a list",
        ["not an object"],
        [{"databaseId": "1", "status": "completed", "conclusion": "success"}],
        [{"databaseId": True, "status": "completed", "conclusion": "success"}],
        [{"databaseId": 0, "status": "completed", "conclusion": "success"}],
        [{"databaseId": 1, "status": "", "conclusion": "success"}],
        [{"databaseId": 1, "status": "completed", "conclusion": 5}],
        [{"status": "completed", "conclusion": "success"}],
    ],
)
def test_publish_malformed_ci_run_list_is_not_treated_as_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    github.run_list_value = value
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert "push-master" not in github.events
    assert github.master() == report["base_commit"]
    assert "Publication failed" in summary.read_text(encoding="utf-8")


def test_publish_master_advancing_during_ci_wait_is_rejected_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    github.pending_polls = 1
    real_sleep = github._sleep

    def advance_master_while_waiting(seconds: float) -> None:
        real_sleep(seconds)
        unrelated = candidate / "unrelated-source.txt"
        unrelated.write_text("master moved during CI\n", encoding="utf-8")
        moved = _git_commit_paths(candidate, "unrelated master change", ("unrelated-source.txt",))
        subprocess.run(
            ["git", "push", "origin", f"{moved}:refs/heads/master"],
            cwd=candidate,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    monkeypatch.setattr(publication, "_ci_sleep", advance_master_while_waiting)
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert github.events.count("push-master") == 1
    assert github.master() != github.branch_head()
    assert github.master() != report["base_commit"]
    assert github.branch_exists()
    assert "master changed before publication CAS" in summary.read_text(encoding="utf-8")


def test_publish_branch_deletion_failure_keeps_success_and_notes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    monkeypatch.setattr(publication, "_delete_branch", lambda *_args, **_kwargs: publication._fail("delete refused"))
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 0
    assert github.master() == github.branch_head()
    text = summary.read_text(encoding="utf-8")
    assert "pushed to master after CI passed" in text
    assert "could not be deleted" in text


def test_publish_rerun_after_failed_branch_deletion_notes_published_and_retries_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    real_delete = publication._delete_branch
    monkeypatch.setattr(publication, "_delete_branch", lambda *_args, **_kwargs: publication._fail("delete refused"))
    first = tmp_path / "first.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=first) == 0
    published = github.master()
    github.events.clear()
    monkeypatch.setattr(publication, "_delete_branch", real_delete)
    second = tmp_path / "second.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=second) == 0
    assert github.events == []
    assert github.master() == published
    assert not github.branch_exists()
    assert "already published to master" in second.read_text(encoding="utf-8")


def test_publish_orphan_branch_after_ci_failure_dispatches_fresh_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    github.ci_conclusion = "failure"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=tmp_path / "a.md") == 1
    orphan_head = github.branch_head()
    branch_pushes: list[str] = []
    real_push_branch = publication._push_branch

    def track_push_branch(root: Path, branch_name: str, head: str) -> None:
        branch_pushes.append(head)
        real_push_branch(root, branch_name, head)

    monkeypatch.setattr(publication, "_push_branch", track_push_branch)
    github.ci_conclusion = "success"
    github.events.clear()
    second = tmp_path / "b.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=second) == 0
    assert branch_pushes == []
    assert github.events == ["dispatch", "ci-success-seen", "push-master"]
    assert github.master() == orphan_head
    assert not github.branch_exists()


def test_publish_partial_dispatch_failure_still_opens_pull_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, candidate, bundle, published_base, _report = _partial_producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, published_base, origin)
    github = _FakeGitHub(origin=origin, branch="automation/profile-refresh-123-456", monkeypatch=monkeypatch)
    github.dispatch_ok = False
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=published_base, summary=summary) == 1
    assert github.events == ["dispatch", "pr-create"]
    assert github.master() == published_base
    text = summary.read_text(encoding="utf-8")
    assert "CI dispatch failed" in text
    assert "Partial generated data was published for review" in text


def test_publish_partial_dispatches_ci_before_opening_pull_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, candidate, bundle, published_base, _report = _partial_producer_bundle(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    _configure_local_origin(candidate, published_base, origin)
    github = _FakeGitHub(origin=origin, branch="automation/profile-refresh-123-456", monkeypatch=monkeypatch)
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=published_base, summary=summary) == 1
    assert github.events == ["dispatch", "pr-create"]
    assert github.sleeps == []
    assert github.master() == published_base
    assert github.branch_exists()


def _existing_pr_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    number: int,
) -> tuple[Path, Path, dict[str, Any], _FakeGitHub]:
    """Leave a validated branch behind a failed CI run, then open a PR on it."""
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    github.ci_conclusion = "failure"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=tmp_path / "seed.md") == 1
    github.open_pr(root=candidate, number=number)
    github.ci_conclusion = "success"
    github.events.clear()
    return candidate, bundle, report, github


def test_publish_existing_open_pr_waits_for_ci_before_master_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, github = _existing_pr_setup(tmp_path, monkeypatch, number=14)
    github.view_failures = [False, True]
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 0
    assert github.pr_creates == 0
    assert github.events == ["dispatch", "ci-success-seen", "push-master"]
    assert github.master() == github.branch_head()
    text = summary.read_text(encoding="utf-8")
    assert "master updated; merge confirmation unavailable" in text
    assert "https://github.com/example/repo/pull/14" in text


def test_publish_existing_open_pr_reports_merged_when_github_confirms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, github = _existing_pr_setup(tmp_path, monkeypatch, number=15)
    real_view = github.gh_json
    views = {"count": 0}

    def merged_after_push(root: Path, arguments: list[str]) -> Any:
        value = real_view(root, arguments)
        if arguments[:2] == ["pr", "view"]:
            views["count"] += 1
            if views["count"] == 2:
                value = {**value, "state": "MERGED", "mergedAt": NOW.isoformat()}
        return value

    monkeypatch.setattr(publication, "_gh_json", merged_after_push)
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 0
    assert "Validated generated data was merged into master." in summary.read_text(encoding="utf-8")


def test_publish_existing_open_pr_with_failed_ci_does_not_push_master(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, github = _existing_pr_setup(tmp_path, monkeypatch, number=16)
    github.ci_conclusion = "cancelled"
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert "push-master" not in github.events
    assert github.master() == report["base_commit"]
    assert "CI finished with conclusion cancelled" in summary.read_text(encoding="utf-8")


def test_publish_post_master_fetch_failure_remains_hard_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    real_fetch_master = publication._fetch_master
    fetches = {"count": 0}

    def fail_after_push(root: Path) -> str:
        fetches["count"] += 1
        if fetches["count"] == 2:
            publication._fail("post-CAS master fetch failed")
        return real_fetch_master(root)

    monkeypatch.setattr(publication, "_fetch_master", fail_after_push)
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert github.master() == github.branch_head()
    assert "post-CAS master fetch failed" in summary.read_text(encoding="utf-8")


def test_publish_post_master_ancestry_failure_remains_hard_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    real_is_ancestor = publication._is_ancestor

    def fail_after_push(root: Path, ancestor: str, descendant: str) -> bool:
        assert real_is_ancestor(root, ancestor, descendant)
        return False

    monkeypatch.setattr(publication, "_is_ancestor", fail_after_push)
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert github.master() == github.branch_head()
    assert "master publication could not be verified" in summary.read_text(encoding="utf-8")
    assert github.branch_exists()


def test_publish_master_lease_rejects_late_advance_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, bundle, report, _origin, github = _setup_success(tmp_path, monkeypatch)
    real_push_master = publication._push_master

    def race(root: Path, master: str, head: str) -> None:
        unrelated = root / "unrelated-source.txt"
        unrelated.write_text("late master advance\n", encoding="utf-8")
        advanced = _git_commit_paths(root, "unrelated master change", ("unrelated-source.txt",))
        subprocess.run(
            ["git", "push", "origin", f"{advanced}:refs/heads/master"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        real_push_master(root, master, head)

    monkeypatch.setattr(publication, "_push_master", race)
    summary = tmp_path / "summary.md"
    assert _publish(candidate=candidate, bundle=bundle, base=report["base_commit"], summary=summary) == 1
    assert github.master() != report["base_commit"]
    assert github.master() != github.branch_head()
    assert github.branch_exists()
    assert "master changed before publication CAS" in summary.read_text(encoding="utf-8")
