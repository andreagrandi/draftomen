from __future__ import annotations

from pathlib import Path

import pytest

from scripts import write_release_news
from scripts.write_release_news import main

CHANGELOG = """# Changelog

## [Unreleased]

- Add something new.

## [1.2.3] - 2026-08-25

- Fix release packaging with `uv`
  and wrapped text. (#123)
- Second note.

## [1.2.2]

- Missing date.
"""


@pytest.fixture
def news_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    changelog_path = tmp_path / "CHANGELOG.md"
    directory = tmp_path / "news"
    changelog_path.write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(write_release_news, "CHANGELOG_PATH", changelog_path)
    monkeypatch.setattr(write_release_news, "NEWS_DIRECTORY", directory)
    return directory


def test_main_writes_frontmatter_and_changelog_body(news_directory: Path) -> None:
    exit_code = main(argv=["--version", "1.2.3"])

    assert exit_code == 0
    assert (news_directory / "1.2.3.md").read_text(encoding="utf-8") == (
        '---\nversion: "1.2.3"\ndate: 2026-08-25\n---\n\n'
        "- Fix release packaging with `uv`\n"
        "  and wrapped text. (#123)\n"
        "- Second note.\n"
    )


def test_main_overwrites_an_existing_page(news_directory: Path) -> None:
    news_directory.mkdir()
    (news_directory / "1.2.3.md").write_text("stale", encoding="utf-8")

    assert main(argv=["--version", "1.2.3"]) == 0

    assert "stale" not in (news_directory / "1.2.3.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("version", "expected_message"),
    [
        ("Unreleased", "invalid version"),
        ("2.0.0", "not found"),
        ("1.2", "invalid version"),
        ("v1.2.3", "invalid version"),
        ("1.2.2", "valid ISO calendar date"),
    ],
)
def test_main_rejects_unusable_versions(
    version: str,
    expected_message: str,
    news_directory: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(argv=["--version", version])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert expected_message in captured.err
    assert captured.out == ""
    assert not news_directory.exists()
