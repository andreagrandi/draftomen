"""Freeze the fetched HOB draft guide page as deterministic readable text.
Keep the raw page byte-exact and derive the guide text the benchmark quotes.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parents[1] / ".draftomen" / "enrichment-runs" / "hob"
RAW_FILE_NAME = "raw.html"
GUIDE_FILE_NAME = "guide.txt"

DROPPED_ELEMENTS = ("script", "style", "noscript", "svg", "iframe", "template")
_BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "option",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_BREAK = "\x00"


class HobGuideFreezeError(ValueError):
    """Describe a raw page that cannot be turned into frozen guide text.
    Keep freeze failures clear for both callers and the command line.
    """


class _ReadableTextParser(HTMLParser):
    """Collect one page's readable text while dropping markup and script blocks.
    Block boundaries become paragraph breaks, so no source markup can leak into the text.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._dropped_depth = 0
        self._pieces: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open a dropped or block-level element without emitting its markup."""
        if tag in DROPPED_ELEMENTS:
            self._dropped_depth += 1
            return
        if self._dropped_depth:
            return
        if tag in _BLOCK_ELEMENTS:
            self._pieces.append(_BREAK)

    def handle_endtag(self, tag: str) -> None:
        """Close a dropped or block-level element without emitting its markup."""
        if tag in DROPPED_ELEMENTS:
            self._dropped_depth = max(self._dropped_depth - 1, 0)
            return
        if self._dropped_depth:
            return
        if tag in _BLOCK_ELEMENTS:
            self._pieces.append(_BREAK)

    def handle_data(self, data: str) -> None:
        """Keep decoded text outside every dropped element."""
        if not self._dropped_depth:
            self._pieces.append(data)

    def text(self) -> str:
        """Return the collected text with paragraph breaks at block boundaries."""
        return "".join(self._pieces)


def readable_text(page: str) -> str:
    """Return the readable text of one HTML page as normalised paragraphs.
    Drop script, style, noscript, svg, iframe, template and comment content entirely.
    """
    parser = _ReadableTextParser()
    parser.feed(page)
    parser.close()
    paragraphs = [
        " ".join(block.split()) for block in parser.text().split(_BREAK)
    ]
    body = "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
    if not body:
        raise HobGuideFreezeError("the raw page contains no readable text.")
    return f"{body}\n"


def build_parser() -> argparse.ArgumentParser:
    """Build the guide freeze argument parser.
    Default to the frozen HOB run directory and allow explicit paths.
    """
    parser = argparse.ArgumentParser(description="Freeze a fetched guide page as readable text")
    parser.add_argument(
        "--raw",
        type=Path,
        default=RUN_DIR / RAW_FILE_NAME,
        help="raw fetched HTML page",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=RUN_DIR / GUIDE_FILE_NAME,
        help="frozen guide text path",
    )
    return parser


def _describe(label: str, path: Path, text: str, digest: str) -> str:
    """Describe one frozen file for the command line."""
    return f"{label} {path}: {len(text)} chars sha256 {digest}"


def main(argv: Sequence[str] | None = None) -> int:
    """Derive the frozen guide text from the raw page.
    Return a nonzero status and explain unreadable input on stderr.
    """
    args = build_parser().parse_args(args=argv)
    try:
        payload = args.raw.read_bytes()
        page = payload.decode("utf-8")
        text = readable_text(page)
        data = text.encode("utf-8")
        args.output.write_bytes(data)
    except (HobGuideFreezeError, OSError, UnicodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(_describe("raw", args.raw, page, hashlib.sha256(payload).hexdigest()))
    print(_describe("guide", args.output, text, hashlib.sha256(data).hexdigest()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(argv=None))
