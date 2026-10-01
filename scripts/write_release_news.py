"""Write the website news page source for one released version.
Read the root changelog and store the release notes as a Markdown file.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

# Running `python3 scripts/write_release_news.py` puts scripts/ on sys.path, not the
# repository root, so add the root before importing the sibling module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.extract_changelog import (  # noqa: E402
    CHANGELOG_PATH,
    ChangelogError,
    extract_section,
)

NEWS_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "website" / "src" / "content" / "news"
)
_VERSION_PATTERN = re.compile(pattern=r"[0-9]+\.[0-9]+\.[0-9]+")


def release_date(*, changelog: str, version: str) -> str:
    """Return the ISO date from the exact heading of a released version.
    Raise ChangelogError when the heading has no date.
    """
    heading = re.compile(
        pattern=rf"^## \[{re.escape(version)}\] - ([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})$",
        flags=re.MULTILINE,
    )
    match = heading.search(changelog)
    if match is None:
        raise ChangelogError(f"changelog section {version!r} has no dated heading")
    return match.group(1)


def build_news_page(*, changelog: str, version: str) -> str:
    """Build the Markdown news page for a released version.
    Combine frontmatter with the changelog body, which extract_section validates.
    """
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise ChangelogError(f"invalid version {version!r}; expected X.Y.Z")
    body = extract_section(changelog=changelog, section=version)
    date_text = release_date(changelog=changelog, version=version)
    return f'---\nversion: "{version}"\ndate: {date_text}\n---\n\n{body}'


def build_parser() -> argparse.ArgumentParser:
    """Build the news page argument parser.
    Require the released version to write.
    """
    parser = argparse.ArgumentParser(description="Write a website release news page")
    parser.add_argument("--version", required=True, help="version such as 1.2.3")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Write the news page for the requested version.
    Return a nonzero status and explain failures on stderr.
    """
    args = build_parser().parse_args(args=argv)
    try:
        changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
        page = build_news_page(changelog=changelog, version=args.version)
        NEWS_DIRECTORY.mkdir(parents=True, exist_ok=True)
        output_path = NEWS_DIRECTORY / f"{args.version}.md"
        output_path.write_text(data=page, encoding="utf-8")
    except (ChangelogError, OSError, UnicodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(argv=None))
