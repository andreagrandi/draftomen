from __future__ import annotations

from pathlib import Path
import re


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _event_block(text: str, event_name: str) -> str:
    marker = f"  {event_name}:\n"
    start = text.index(marker)
    remainder = text[start + len(marker) :]
    next_event = re.search(r"\n  [A-Za-z0-9_-]+:\n", remainder)
    end = len(text) if next_event is None else start + len(marker) + next_event.start()
    return text[start:end]


def _list_value(block: str, key: str) -> tuple[str, ...]:
    match = re.search(
        rf"^    {re.escape(key)}:\n((?:^      - .*\n?)+)",
        block,
        flags=re.MULTILINE,
    )
    if match is None:
        return ()
    return tuple(
        line.removeprefix("      - ").rstrip()
        for line in match.group(1).splitlines()
    )


def test_pull_requests_run_ci_for_every_changed_path() -> None:
    pull_request = _event_block(_text(), "pull_request")

    assert _list_value(pull_request, "branches") == ("master",)
    assert "paths-ignore:" not in pull_request


def test_source_pull_requests_and_master_pushes_remain_configured() -> None:
    text = _text()
    pull_request = _event_block(text, "pull_request")
    push = _event_block(text, "push")

    assert _list_value(pull_request, "branches") == ("master",)
    assert _list_value(push, "branches") == ("master",)
    assert "paths-ignore:" not in push


def _job_block(text: str, job_name: str) -> str:
    jobs = text[text.index("\njobs:\n") :]
    marker = f"\n  {job_name}:\n"
    start = jobs.index(marker) + len(marker)
    next_job = re.search(r"\n  [A-Za-z0-9_-]+:\n", jobs[start:])
    return jobs[start:] if next_job is None else jobs[start : start + next_job.start()]


def test_website_job_builds_from_the_locked_dependencies() -> None:
    website = _job_block(text=_text(), job_name="website")

    assert "npm ci" in website
    assert "npm run build" in website
    assert "working-directory: website" in website


def test_dependabot_merge_waits_for_every_check_job() -> None:
    merge = _job_block(text=_text(), job_name="dependabot-merge")

    assert _list_value(merge, "needs") == ("tests", "gui", "extension", "website")
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in merge
    assert "continue-on-error" not in merge
    assert "always()" not in merge


def test_dependabot_merge_leaves_major_and_unknown_updates_open() -> None:
    merge = _job_block(text=_text(), job_name="dependabot-merge")

    assert "version-update:semver-patch" in merge
    assert "version-update:semver-minor" in merge
    assert "semver-major" not in merge
    assert "!=" not in merge


def test_dependabot_merge_pins_the_tested_commit_and_runs_ci_on_master() -> None:
    merge = _job_block(text=_text(), job_name="dependabot-merge")

    assert '--match-head-commit "$HEAD_SHA"' in merge
    assert "--auto" not in merge
    assert "gh workflow run ci.yml" in merge
    assert "--ref master" in merge
