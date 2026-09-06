from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "profile-refresh.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _run_block(text: str, step_name: str) -> str:
    marker = f"      - name: {step_name}\n"
    start = text.index(marker)
    run_start = text.index("        run: |\n", start) + len("        run: |\n")
    step_end = text.find("\n      - name:", run_start)
    job_match = re.search(r"\n  [A-Za-z0-9_-]+:\n", text[run_start:])
    job_end = -1 if job_match is None else run_start + job_match.start()
    end_candidates = [end for end in (step_end, job_end) if end >= 0]
    end = min(end_candidates, default=len(text))
    lines = text[run_start:end].splitlines()
    return "\n".join(line[10:] for line in lines)


def _job_block(text: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    start = text.index(marker)
    remainder = text[start + len(marker) :]
    next_job = re.search(r"\n  [A-Za-z0-9_-]+:\n", remainder)
    end = len(text) if next_job is None else start + len(marker) + next_job.start()
    return text[start:end]


def _run_shell(
    script: str,
    *,
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    shell_env = os.environ.copy()
    shell_env.update(env)
    return subprocess.run(
        ["bash"],
        cwd=cwd,
        env=shell_env,
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "uv-args"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$ARGS_FILE"\n'
        'exit "${UV_STATUS:-0}"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return bin_dir, args_file


def _generation_env(
    tmp_path: Path,
    *,
    event_name: str,
    schedule: str = "",
    mode: str = "",
    selector: str = "",
    uv_status: str = "0",
) -> tuple[dict[str, str], Path]:
    bin_dir, args_file = _fake_uv(tmp_path)
    work_dir = tmp_path / "work"
    repo_root = tmp_path / "checkout"
    repo_root.mkdir()
    return (
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ARGS_FILE": str(args_file),
            "UV_STATUS": uv_status,
            "EVENT_NAME": event_name,
            "EVENT_SCHEDULE": schedule,
            "INPUT_SELECTION_MODE": mode,
            "INPUT_SET": selector,
            "BASE_COMMIT": "0123456789abcdef",
            "REPO_ROOT": str(repo_root),
            "WORK_DIR": str(work_dir),
        },
        work_dir,
    )


def test_dispatch_and_schedule_policy_is_explicit() -> None:
    text = _text()
    trigger = text[text.index("on:\n") : text.index("permissions:\n")]
    assert re.findall(r'^    - cron: "([^"]+)"$', trigger, flags=re.MULTILINE) == [
        "17 6 * * *",
        "47 6 * * 0",
    ]
    inputs = set(re.findall(r"^      ([a-z_]+):$", trigger, flags=re.MULTILINE))
    assert inputs == {"selection_mode", "set"}
    assert re.findall(
        r"^          - (one|all|active|historical)$",
        trigger,
        flags=re.MULTILINE,
    ) == ["one", "all", "active", "historical"]
    assert re.search(
        r"selection_mode:\n(?:        [^\n]+\n)+        default: all",
        trigger,
    )
    assert re.search(r"set:\n(?:        [^\n]+\n)+        required: false", trigger)


def test_generation_shell_maps_events_and_quotes_helper_arguments(tmp_path: Path) -> None:
    script = _run_block(_text(), "Generate website data")
    env, work_dir = _generation_env(
        tmp_path,
        event_name="workflow_dispatch",
        mode="one",
        selector='Set "with spaces" $HOME',
    )
    result = _run_shell(script, env=env, cwd=tmp_path)
    assert result.returncode == 0
    args = Path(env["ARGS_FILE"]).read_text(encoding="utf-8").splitlines()
    assert args[:2] == ["run", "python"]
    assert "--selection-mode" in args
    assert args[args.index("--selection-mode") + 1] == "one"
    assert args[args.index("--set") + 1] == 'Set "with spaces" $HOME'
    assert args[args.index("--base-commit") + 1] == env["BASE_COMMIT"]
    assert args[args.index("--bundle-dir") + 1] == str(work_dir / "bundle")
    assert args[args.index("--cache-dir") + 1] == str(work_dir / "cache")


def test_generation_shell_maps_both_schedules_and_rejects_unknown_schedule(
    tmp_path: Path,
) -> None:
    script = _run_block(_text(), "Generate website data")
    for schedule, expected in (
        ("17 6 * * *", "active"),
        ("47 6 * * 0", "historical"),
    ):
        case_dir = tmp_path / expected
        case_dir.mkdir()
        env, _ = _generation_env(
            case_dir,
            event_name="schedule",
            schedule=schedule,
        )
        result = _run_shell(script, env=env, cwd=case_dir)
        assert result.returncode == 0
        args = Path(env["ARGS_FILE"]).read_text(encoding="utf-8").splitlines()
        assert args[args.index("--selection-mode") + 1] == expected
        assert "--set" not in args

    unknown_dir = tmp_path / "unknown"
    unknown_dir.mkdir()
    env, work_dir = _generation_env(
        unknown_dir,
        event_name="schedule",
        schedule="0 0 * * *",
    )
    result = _run_shell(script, env=env, cwd=unknown_dir)
    assert result.returncode == 2
    assert not Path(env["ARGS_FILE"]).exists()


def test_generation_shell_rejects_invalid_set_combinations(tmp_path: Path) -> None:
    script = _run_block(_text(), "Generate website data")
    for mode, selector in (("one", ""), ("all", "TST")):
        case_dir = tmp_path / mode
        case_dir.mkdir()
        env, work_dir = _generation_env(
            case_dir,
            event_name="workflow_dispatch",
            mode=mode,
            selector=selector,
        )
        result = _run_shell(script, env=env, cwd=case_dir)
        assert result.returncode == 2
        assert not Path(env["ARGS_FILE"]).exists()


def test_workflow_permissions_and_ephemeral_bundle_policy() -> None:
    text = _text()
    generate = _job_block(text, "generate")
    publish = _job_block(text, "publish")
    assert "permissions:\n  contents: read\n" in text
    assert not re.search(r"(?m)^\s+(?:actions|contents|pull-requests): write$", generate)
    assert "permissions:" not in generate
    assert re.search(
        r"permissions:\n"
        r"      actions: read\n"
        r"      contents: write\n"
        r"      pull-requests: write\n",
        publish,
    )
    assert generate.count("persist-credentials: false") == 1
    assert publish.count("persist-credentials: false") == 1
    assert generate.count("enable-cache: false") == 1
    assert publish.count("enable-cache: false") == 1
    assert "actions/cache" not in text
    assert "retention-days: 7" in text
    assert "if-no-files-found: error" in text
    assert re.search(
        r"path: \$\{\{ runner\.temp \}\}/draftomen-profile-refresh-.*?/bundle",
        generate,
    )
    assert "cloudflare" not in text.lower()
    assert "wrangler" not in text.lower()
    assert "secrets." not in text.lower()
    assert "GH_TOKEN: ${{ github.token }}" in publish
    assert "GH_TOKEN" not in generate


def test_publish_job_is_master_only_noncancelled_and_serialized() -> None:
    text = _text()
    generate = _job_block(text, "generate")
    publish = _job_block(text, "publish")
    assert "outputs:\n      artifact-id: ${{ steps.upload.outputs.artifact-id }}" in generate
    assert "needs: generate" in publish
    assert re.search(
        r"if: >-\n"
        r"      \$\{\{ !cancelled\(\) &&\n"
        r"          github\.ref == 'refs/heads/master' &&\n"
        r"          needs\.generate\.outputs\.artifact-id != '' \}\}",
        publish,
    )
    assert re.search(
        r"concurrency:\n"
        r"      group: profile-refresh-publish\n"
        r"      cancel-in-progress: false\n",
        publish,
    )
    assert "artifact-ids: ${{ needs.generate.outputs.artifact-id }}" in publish
    assert "merge-multiple: true" in publish
    assert "name: profile-refresh-generation-" not in publish


def test_publish_checkout_and_cli_use_trusted_ids_and_quoted_arguments(
    tmp_path: Path,
) -> None:
    text = _text()
    publish = _job_block(text, "publish")
    checkout = publish[publish.index("- name: Check out repository") :]
    assert "ref: ${{ github.sha }}" in checkout
    assert "fetch-depth: 0" in checkout
    script = _run_block(text, "Publish generated website data")
    bin_dir, args_file = _fake_uv(tmp_path)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ARGS_FILE": str(args_file),
        "UV_STATUS": "0",
        "BASE_COMMIT": "a" * 40,
        "REPOSITORY": "owner/repo",
        "RUN_ID": "123",
        "ARTIFACT_ID": "456",
        "RUN_URL": "https://github.com/owner/repo/actions/runs/123?x=a b&y=$HOME",
        "REPO_ROOT": str(tmp_path / "checkout with spaces"),
        "BUNDLE_DIR": str(tmp_path / "bundle;printf unsafe"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary $HOME.md"),
    }
    result = _run_shell(script, env=env, cwd=tmp_path)
    assert result.returncode == 0
    assert Path(env["ARGS_FILE"]).read_text(encoding="utf-8").splitlines() == [
        "run",
        "python",
        "-m",
        "scripts.profile_refresh_publication",
        "--repo-root",
        env["REPO_ROOT"],
        "--bundle-dir",
        env["BUNDLE_DIR"],
        "--base-commit",
        env["BASE_COMMIT"],
        "--repository",
        env["REPOSITORY"],
        "--run-id",
        env["RUN_ID"],
        "--artifact-id",
        env["ARTIFACT_ID"],
        "--run-url",
        env["RUN_URL"],
        "--summary-file",
        env["GITHUB_STEP_SUMMARY"],
    ]
    env["UV_STATUS"] = "9"
    assert _run_shell(script, env=env, cwd=tmp_path).returncode == 9


def test_failure_evidence_steps_are_always_attempted_and_status_is_propagated(
    tmp_path: Path,
) -> None:
    text = _text()
    generation = _run_block(text, "Generate website data")
    summary = _run_block(text, "Append generation summary")
    final = _run_block(text, "Propagate generation status")
    assert "publish:" not in final
    assert "continue-on-error: true" in text[text.index("- name: Generate website data") :]
    assert text.count("if: ${{ always() }}") >= 3
    env, work_dir = _generation_env(tmp_path, event_name="workflow_dispatch", mode="all", uv_status="7")
    result = _run_shell(generation, env=env, cwd=tmp_path)
    assert result.returncode == 7
    bundle = work_dir / "bundle"
    (bundle / "summary.md").write_text("# summary\n", encoding="utf-8")
    summary_env = {
        "WORK_DIR": str(work_dir),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "step-summary"),
    }
    result = _run_shell(summary, env=summary_env, cwd=tmp_path)
    assert result.returncode == 0
    assert (tmp_path / "step-summary").read_text(encoding="utf-8") == "# summary\n"

    final_env = {
        "GENERATION_OUTCOME": "failure",
        "SUMMARY_OUTCOME": "success",
        "UPLOAD_OUTCOME": "success",
    }
    assert _run_shell(final, env=final_env, cwd=tmp_path).returncode == 1
    final_env["GENERATION_OUTCOME"] = "success"
    assert _run_shell(final, env=final_env, cwd=tmp_path).returncode == 0
