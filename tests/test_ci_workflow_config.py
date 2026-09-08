from __future__ import annotations

from pathlib import Path
import re


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
GENERATED_ROOTS = (
    "website/public/card-data/**",
    "website/public/profiles/**",
)


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


def test_pull_request_ignores_only_generated_website_roots() -> None:
    pull_request = _event_block(_text(), "pull_request")

    assert _list_value(pull_request, "branches") == ("master",)
    assert _list_value(pull_request, "paths-ignore") == GENERATED_ROOTS


def test_source_pull_requests_and_master_pushes_remain_configured() -> None:
    text = _text()
    pull_request = _event_block(text, "pull_request")
    push = _event_block(text, "push")

    assert _list_value(pull_request, "branches") == ("master",)
    assert _list_value(push, "branches") == ("master",)
    assert "paths-ignore:" not in push
