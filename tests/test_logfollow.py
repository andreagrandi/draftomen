from __future__ import annotations

import json
from pathlib import Path

import pytest

import draftomen.logfollow as logfollow_module
from draftomen.logfollow import LogFollower, detailed_logs_disabled, log_offset_path


@pytest.mark.parametrize(
    ("header", "expected"),
    (
        ("Initialize engine version\nDETAILED LOGS: DISABLED\n", True),
        ("Initialize engine version\nDETAILED LOGS: ENABLED\n", False),
        ("Initialize engine version\n", False),
    ),
)
def test_detailed_logs_disabled_reads_arena_header(
    tmp_path: Path,
    header: str,
    expected: bool,
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.write_text(header + "later line\n", encoding="utf-8")

    assert detailed_logs_disabled(path=log_path) is expected


def test_detailed_logs_disabled_ignores_marker_past_the_header(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "Player.log"
    padding = "x" * logfollow_module.DETAILED_LOGS_HEADER_BYTES
    log_path.write_text(padding + "\nDETAILED LOGS: DISABLED\n", encoding="utf-8")

    assert detailed_logs_disabled(path=log_path) is False


def test_detailed_logs_disabled_treats_missing_log_as_unknown(
    tmp_path: Path,
) -> None:
    assert detailed_logs_disabled(path=tmp_path / "Player.log") is False


def test_partial_lines_are_buffered_and_offset_resumes_across_restarts(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    log_path.write_text("first\nsecond", encoding="utf-8")
    follower = LogFollower(
        log_path=log_path,
        app_dir=app_dir,
        poll_interval=0.25,
    )

    assert follower.poll_interval == 0.25
    assert follower.poll() == ("first",)

    offset_path = log_offset_path(log_path=log_path, app_dir=app_dir)
    assert offset_path.parent == app_dir / "logfollow"
    assert offset_path.exists()
    state = json.loads(offset_path.read_text(encoding="utf-8"))
    assert state["offset"] == len("first\n")

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("\nthird\n")

    restarted = LogFollower(log_path=log_path, app_dir=app_dir)

    assert restarted.poll() == ("second", "third")
    assert restarted.poll() == ()


def test_truncation_resets_offset_without_replaying_old_lines(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    log_path.write_text("old one\nold two\n", encoding="utf-8")
    follower = LogFollower(log_path=log_path, app_dir=app_dir)

    assert follower.poll() == ("old one", "old two")

    log_path.write_text("new\n", encoding="utf-8")

    assert follower.poll() == ("new",)
    assert follower.poll() == ()


def test_recreated_log_resets_offset_without_replaying_old_lines(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    log_path.write_text("old\n", encoding="utf-8")
    follower = LogFollower(log_path=log_path, app_dir=app_dir)

    assert follower.poll() == ("old",)

    log_path.unlink()
    log_path.write_text("new\n", encoding="utf-8")

    assert follower.poll() == ("new",)
    assert follower.poll() == ()


def test_rewritten_same_size_log_resets_offset(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    log_path.write_text("old\n", encoding="utf-8")
    follower = LogFollower(log_path=log_path, app_dir=app_dir)

    assert follower.poll() == ("old",)

    with log_path.open("r+b") as log_file:
        log_file.write(b"new\n")

    assert follower.poll() == ("new",)
    assert follower.poll() == ()


def test_rotation_to_player_prev_recovers_unread_tail_once(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    log_path.write_text("one\n", encoding="utf-8")
    follower = LogFollower(log_path=log_path, app_dir=app_dir)

    assert follower.poll() == ("one",)

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("two\n")
    log_path.rename(previous_log_path)
    log_path.write_text("three\n", encoding="utf-8")

    assert follower.poll() == ("two", "three")
    assert follower.poll() == ()


def test_startup_scan_reads_player_prev_then_current_and_advances_offset(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    previous_log_path.write_text("prev one\nprev two\n", encoding="utf-8")
    log_path.write_text("current one\ncurrent two", encoding="utf-8")
    follower = LogFollower(log_path=log_path, app_dir=app_dir)

    assert follower.scan_startup_files() == ("prev one", "prev two", "current one")

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("\ncurrent three\n")
    assert follower.poll() == ("current two", "current three")
    assert follower.poll() == ()


def test_poll_preserves_rotated_tail_when_previous_log_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    log_path.write_text("one\n", encoding="utf-8")
    follower = LogFollower(log_path=log_path, app_dir=app_dir)

    assert follower.poll() == ("one",)
    offset_path = log_offset_path(log_path=log_path, app_dir=app_dir)
    saved_state = offset_path.read_bytes()

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("two\n")
    log_path.rename(previous_log_path)
    log_path.write_text("three\n", encoding="utf-8")

    original_open = logfollow_module.Path.open

    def fail_previous_open(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> object:
        if path == previous_log_path:
            raise OSError("temporary previous-log read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(logfollow_module.Path, "open", fail_previous_open)
    with pytest.raises(OSError, match="temporary previous-log read failure"):
        follower.poll()

    assert offset_path.read_bytes() == saved_state

    monkeypatch.setattr(logfollow_module.Path, "open", original_open)
    assert follower.poll() == ("two", "three")


def test_open_log_propagates_non_missing_open_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.touch()
    original_open = logfollow_module.Path.open

    def fail_open(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> object:
        raise OSError("temporary log-open failure")

    monkeypatch.setattr(logfollow_module.Path, "open", fail_open)
    with pytest.raises(OSError, match="temporary log-open failure"):
        logfollow_module._open_log(path=log_path)

    monkeypatch.setattr(logfollow_module.Path, "open", original_open)


def test_open_log_propagates_non_missing_stat_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log_path = tmp_path / "Player.log"
    log_path.touch()

    def fail_fstat(file_descriptor: int) -> object:
        raise OSError("temporary log-stat failure")

    monkeypatch.setattr(logfollow_module.os, "fstat", fail_fstat)
    with pytest.raises(OSError, match="temporary log-stat failure"):
        logfollow_module._open_log(path=log_path)


def test_startup_scan_skips_unreadable_current_log_but_keeps_previous_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    previous_log_path.write_text("previous line\n", encoding="utf-8")
    log_path.write_text("current line\n", encoding="utf-8")
    follower = LogFollower(log_path=log_path, app_dir=app_dir)
    original_open = logfollow_module.Path.open

    def fail_current_open(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> object:
        if path == log_path:
            raise OSError("temporary current-log read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(logfollow_module.Path, "open", fail_current_open)
    assert follower.scan_startup_files() == ("previous line",)

    assert not log_offset_path(log_path=log_path, app_dir=app_dir).exists()


def test_startup_scan_does_not_suppress_unreadable_previous_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_dir = tmp_path / "app"
    log_path = tmp_path / "Player.log"
    previous_log_path = tmp_path / "Player-prev.log"
    previous_log_path.write_text("previous line\n", encoding="utf-8")
    log_path.write_text("current line\n", encoding="utf-8")
    follower = LogFollower(log_path=log_path, app_dir=app_dir)
    original_open = logfollow_module.Path.open

    def fail_previous_open(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> object:
        if path == previous_log_path:
            raise OSError("temporary previous-log read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(logfollow_module.Path, "open", fail_previous_open)
    with pytest.raises(OSError, match="temporary previous-log read failure"):
        follower.scan_startup_files()

