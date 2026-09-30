"""Summarise real Arena logs without printing card IDs or UUIDs.
Run it locally against the private corpus; the logs cannot ship in CI.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from draftomen.draft_format import (
    DRAFT_EVENT_PREFIXES,
    DraftFormat,
    DraftRules,
    detect_draft_format,
    rules_for_format,
)
from draftomen.events import (
    DraftCompletedEvent,
    DraftLogParseError,
    PackOfferedEvent,
    PickMadeEvent,
    parse_events,
)
from draftomen.replay import format_draft_format

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / ".git" / "private" / "player-logs"

# The source manifest names the event for excerpts that lost their EventJoin line.
EVENT_NAME_HINTS: dict[str, str] = {
    "premier-dsk-complete-42-picks.anon.log": "PremierDraft_DSK_20000101",
}

_EVENT_NAME_PATTERN = re.compile(
    pattern="(?:"
    + "|".join(re.escape(prefix) for prefix in DRAFT_EVENT_PREFIXES)
    + ")[A-Za-z0-9_]*"
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.
    Paths default to the private corpus under the repository's .git directory.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Log files or directories to search recursively for *.log.",
    )
    return parser


def collect_logs(*, path: Path) -> list[tuple[Path, str]]:
    """Return log files under a path with their display labels.
    Labels are relative to the path, or to its parent for a single file.
    """
    if path.is_file():
        return [(path, f"{path.parent.name}/{path.name}")]
    return [
        (log, log.relative_to(path).as_posix()) for log in sorted(path.rglob("*.log"))
    ]


def _synthetic_join_line(*, event_name: str) -> str:
    return (
        '[UnityCrossThreadLogger]==> EventJoin {"id":"manifest-join","request":'
        f'"{{\\"EventName\\":\\"{event_name}\\",\\"EntryCurrencyType\\":\\"Gem\\"}}"}}'
    )


def _named_formats(*, text: str) -> dict[str, DraftFormat]:
    named: dict[str, DraftFormat] = {}
    for name in _EVENT_NAME_PATTERN.findall(text):
        found = detect_draft_format(event_name=name)
        if found is not None:
            named[name] = found
    return named


def _sorted_names(*, names: set[str]) -> str:
    return ", ".join(sorted(names))


def _gaps(*, rules: DraftRules, picked: set[tuple[int, int]]) -> str:
    missing = [
        f"pack {pack + 1} pick {pick + 1}"
        for pack in range(rules.pack_count)
        for pick in range(rules.picks_per_pack)
        if (pack, pick) not in picked
    ]
    return ", ".join(missing) if missing else "none"


def summarize_file(*, path: Path, label: str) -> str:
    """Return the summary block for one log file.
    The block holds counts and event names only, never IDs or raw lines.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    hint = EVENT_NAME_HINTS.get(path.name)
    if hint is not None:
        lines.insert(0, _synthetic_join_line(event_name=hint))

    block = [label]
    try:
        events = list(parse_events(lines))
    except DraftLogParseError as error:
        block.append(f"  error: {type(error).__name__}")
        return "\n".join(block)

    offers = [event for event in events if isinstance(event, PackOfferedEvent)]
    picks = [event for event in events if isinstance(event, PickMadeEvent)]
    completions = [event for event in events if isinstance(event, DraftCompletedEvent)]

    rules: DraftRules | None = None
    event_names = {event.event_name for event in (*offers, *picks, *completions)}
    if offers:
        draft_format = offers[0].draft_format
        rules = rules_for_format(draft_format=draft_format)
        format_line = format_draft_format(draft_format=draft_format)
    else:
        named = _named_formats(text=text)
        # Quick Draft reward events show up next to human events, so rank them last.
        human = {
            name: found for name, found in named.items() if found != DraftFormat.QUICK
        }
        named = human or named
        if named:
            first = named[min(named)]
            format_line = (
                f"{format_draft_format(draft_format=first)} (event name only, no draft)"
            )
            event_names = {name for name, found in named.items() if found == first}
        else:
            format_line = "unknown"

    offer_coordinates = {(event.pack_number, event.pick_number) for event in offers}
    pick_coordinates = {(event.pack_number, event.pick_number) for event in picks}
    total = f" of {rules.total_picks}" if rules is not None else ""

    block.append(f"  format: {format_line}")
    if event_names:
        block.append(f"  event: {_sorted_names(names=event_names)}")
    block.append(
        f"  packs: {len(offers)} offers over {len(offer_coordinates)} coordinates"
    )
    block.append(f"  logical picks: {len(pick_coordinates)}{total}")
    block.append(f"  cards: {sum(len(event.selected_grp_ids) for event in picks)}")
    if picks and rules is not None:
        block.append(f"  gaps: {_gaps(rules=rules, picked=pick_coordinates)}")
    completed = "no"
    if completions:
        completed = "yes"
        if any(event.inferred for event in completions):
            completed += " (inferred)"
    block.append(f"  completion: {completed}")
    if hint is not None:
        block.append("  note: event name from manifest")
    return "\n".join(block)


def main(argv: Sequence[str] | None = None) -> int:
    """Print a summary for every log found under the requested paths.
    Return 1 when a path is missing or no logs are found.
    """
    args = build_parser().parse_args(args=argv)
    roots: list[Path] = args.paths or [DEFAULT_ROOT]

    logs: list[tuple[Path, str]] = []
    for root in roots:
        if not root.exists():
            print(f"error: path not found: {root}", file=sys.stderr)
            return 1
        logs.extend(collect_logs(path=root))
    if not logs:
        print("error: no *.log files found", file=sys.stderr)
        return 1

    print("\n\n".join(summarize_file(path=path, label=label) for path, label in logs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(argv=None))
