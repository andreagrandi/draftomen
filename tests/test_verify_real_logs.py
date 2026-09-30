from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from scripts.verify_real_logs import main

FIXTURES = Path(__file__).parent / "fixtures"


def _run(*, path: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(argv=[str(path)]) == 0
    return capsys.readouterr().out


def _copy_fixture(*, name: str, tmp_path: Path) -> Path:
    target = tmp_path / name
    shutil.copy(src=FIXTURES / name, dst=target)
    return target


@pytest.mark.parametrize(
    ("name", "label", "picks", "cards"),
    [
        ("premier-draft-complete.log", "Premier Draft", 42, 42),
        ("traditional-draft-complete.log", "Traditional Draft", 42, 42),
        ("pick-two-draft-complete.log", "Pick-Two", 21, 42),
    ],
)
def test_complete_fixture_summary(
    name: str,
    label: str,
    picks: int,
    cards: int,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _copy_fixture(name=name, tmp_path=tmp_path)

    output = _run(path=tmp_path, capsys=capsys)

    assert f"  format: {label}\n" in output
    assert f"  logical picks: {picks} of {picks}\n" in output
    assert f"  cards: {cards}\n" in output
    assert "  gaps: none\n" in output
    assert "  completion: yes" in output


def test_missing_pick_request_reports_one_based_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _copy_fixture(name="premier-draft-complete.log", tmp_path=tmp_path)
    kept = [
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if "premier-pick-1-2" not in line
    ]
    source.write_text(data="\n".join(kept) + "\n", encoding="utf-8")

    output = _run(path=tmp_path, capsys=capsys)

    assert "  gaps: pack 1 pick 2\n" in output


def test_event_name_only_log_reports_format_without_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "event.log").write_text(
        data='{"InternalEventName":"TradDraft_MSH_20260930"}\n', encoding="utf-8"
    )

    output = _run(path=tmp_path, capsys=capsys)

    assert "  format: Traditional Draft (event name only, no draft)\n" in output
    assert "  event: TradDraft_MSH_20260930\n" in output
    assert "  gaps:" not in output


def test_manifest_hint_supplies_event_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = [
        line
        for line in (FIXTURES / "premier-draft-complete.log")
        .read_text(encoding="utf-8")
        .splitlines()
        if "EventJoin" not in line and "InternalEventName" not in line
    ]
    excerpt = tmp_path / "premier-dsk-complete-42-picks.anon.log"
    excerpt.write_text(data="\n".join(lines) + "\n", encoding="utf-8")

    output = _run(path=excerpt, capsys=capsys)

    assert "  format: Premier Draft\n" in output
    assert "  event: PremierDraft_DSK_20000101\n" in output
    assert "  packs: 42 offers over 42 coordinates\n" in output
    assert "  note: event name from manifest\n" in output


def test_output_has_no_uuids_or_card_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = _copy_fixture(name="premier-draft-complete.log", tmp_path=tmp_path)
    grp_ids = set(re.findall(pattern=r"\b\d{5,6}\b", string=fixture.read_text()))

    output = _run(path=tmp_path, capsys=capsys)

    assert grp_ids
    assert re.search(pattern=r"[0-9a-f]{8}-[0-9a-f]{4}-", string=output) is None
    assert not grp_ids & set(re.findall(pattern=r"\b\d+\b", string=output))


def test_missing_root_exits_with_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv=[str(tmp_path / "absent")]) == 1
    assert "not found" in capsys.readouterr().err


def test_empty_directory_exits_with_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv=[str(tmp_path)]) == 1
    assert "no *.log files" in capsys.readouterr().err
