"""Add Arena ids from 17Lands card ratings to Scryfall default-cards data.
Use it for a set Scryfall has no Arena ids for, then pass the output to export-set-data.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from draftomen.carddb import iter_scryfall_default_cards


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.
    The output is gzip JSONL, which export-set-data accepts as --bulk-file.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("set_code", metavar="SET", help="Set code, case-insensitive.")
    parser.add_argument(
        "--ratings-file",
        type=Path,
        required=True,
        help="17Lands card ratings JSON: a list of objects with name and mtga_id.",
    )
    parser.add_argument(
        "--bulk-file",
        type=Path,
        default=None,
        help="Scryfall default-cards JSONL or .jsonl.gz. Downloaded when omitted.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Patched default-cards file to write, ending in .gz.",
    )
    return parser


def load_ratings(*, path: Path) -> dict[str, int]:
    """Return the Arena id for each card name in a 17Lands ratings file.
    Raises ValueError when the file is not a list of objects with name and mtga_id.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot read ratings file {path}: {error}") from error

    if not isinstance(payload, list):
        raise ValueError(f"Ratings file {path} must hold a JSON list.")

    ids: dict[str, int] = {}
    for index, entry in enumerate(payload):
        name = entry.get("name") if isinstance(entry, dict) else None
        arena_id = entry.get("mtga_id") if isinstance(entry, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or isinstance(arena_id, bool)
            or not isinstance(arena_id, int)
            or arena_id <= 0
        ):
            raise ValueError(
                f"Ratings entry {index} needs a name and a positive integer mtga_id."
            )
        ids[_front_name(name=name)] = arena_id
    return ids


def _front_name(*, name: str) -> str:
    return name.split(" // ")[0]


def _collector_rank(*, card: Mapping[str, Any]) -> tuple[int, int, str]:
    number = str(card.get("collector_number", ""))
    match = re.fullmatch(r"(\d+)(.*)", number)
    if match:
        return (0, int(match.group(1)), match.group(2))
    return (1, 0, number)


def patch_rows(
    *,
    rows: list[dict[str, Any]],
    set_code: str,
    ids: Mapping[str, int],
) -> tuple[int, int, list[str]]:
    """Set arena_id on the lowest collector number print of each rated card.
    Returns the matched count, the already-set count, and the unmatched names.
    """
    wanted = set_code.casefold()
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in rows:
        if str(card.get("set", "")).casefold() == wanted:
            by_name[_front_name(name=str(card.get("name", "")))].append(card)

    matched = 0
    already_set = 0
    unmatched: list[str] = []
    for name, arena_id in ids.items():
        prints = by_name.get(name)
        if not prints:
            unmatched.append(name)
        elif any(card.get("arena_id") is not None for card in prints):
            already_set += 1
        else:
            min(prints, key=lambda card: _collector_rank(card=card))["arena_id"] = (
                arena_id
            )
            matched += 1
    return matched, already_set, unmatched


def main(argv: Sequence[str] | None = None) -> int:
    """Patch the Scryfall data and print a short report.
    Returns a non-zero status when the ratings are invalid or nothing matched.
    """
    args = build_parser().parse_args(args=argv)
    if args.output.suffix != ".gz":
        print("--output must end in .gz so export-set-data reads it.", file=sys.stderr)
        return 2
    try:
        ids = load_ratings(path=args.ratings_file)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    rows = [
        dict(card) for card in iter_scryfall_default_cards(bulk_file=args.bulk_file)
    ]
    matched, already_set, unmatched = patch_rows(
        rows=rows, set_code=args.set_code, ids=ids
    )
    print(f"Matched: {matched}")
    print(f"Already set: {already_set}")
    print(f"Unmatched: {len(unmatched)}")
    for name in unmatched:
        print(f"  {name}")

    if matched + already_set == 0:
        print(f"No rating matched a {args.set_code} card.", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, mode="wt", encoding="utf-8") as handle:
        for card in rows:
            handle.write(json.dumps(card, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
