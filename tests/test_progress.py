from __future__ import annotations

import pytest

from draftomen.progress import ProgressReporter


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_slow_progress_prints_once_per_ten_percent_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    reporter = ProgressReporter(label="Download", total=1000, unit="picks", clock=clock)

    for done in range(1, 1001):
        clock.now += 0.1
        reporter.update(done=done)

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 10
    assert lines[0] == "Download: 10% (100 picks of 1,000 picks) after 10s"
    assert lines[2] == "Download: 30% (300 picks of 1,000 picks) after 30s"
    assert lines[-1] == "Download: 100% (1,000 picks of 1,000 picks) after 100s"


def test_fast_progress_waits_five_seconds_between_lines_but_always_prints_completion(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    reporter = ProgressReporter(label="Download", total=1000, clock=clock)

    for done in range(1, 1001):
        clock.now = done / 125
        reporter.update(done=done)

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "Download: 62% (625 of 1,000) after 5s",
        "Download: 100% (1,000 of 1,000) after 8s",
    ]


def test_large_jumps_print_one_line_instead_of_one_per_skipped_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    reporter = ProgressReporter(label="Scoring", total=100, step=0.25, clock=clock)

    clock.now = 10.0
    reporter.update(done=60)
    clock.now = 20.0
    reporter.update(done=70)
    reporter.update(done=100)

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "Scoring: 60% (60 of 100) after 10s",
        "Scoring: 100% (100 of 100) after 20s",
    ]


def test_unknown_total_prints_at_most_every_five_seconds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    reporter = ProgressReporter(label="Download", total=None, unit="bytes", clock=clock)

    for second in range(1, 21):
        clock.now = float(second)
        reporter.update(done=second * 1_000_000)

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "Download: 5.0 MB after 5s",
        "Download: 10.0 MB after 10s",
        "Download: 15.0 MB after 15s",
        "Download: 20.0 MB after 20s",
    ]

