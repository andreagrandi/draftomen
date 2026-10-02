"""Offset-persisted Player.log follower.
Emit complete raw lines while tolerating Arena log rotation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Any, BinaryIO, TypeAlias

from draftomen.applog import redact_home
from draftomen.config import POLL_INTERVAL_SECONDS
from draftomen.paths import app_data_dir

logger = logging.getLogger(__name__)

PathInput: TypeAlias = str | PathLike[str]

OFFSET_DIRECTORY_NAME = "logfollow"
OFFSET_SCHEMA_VERSION = 1
STATE_HASH_LENGTH = 16
FINGERPRINT_SAMPLE_BYTES = 4096
# Arena writes the Detailed Logs state within the first kilobyte of each session.
DETAILED_LOGS_HEADER_BYTES = 16384
DETAILED_LOGS_DISABLED_MARKER = b"DETAILED LOGS: DISABLED"


class LogFollowError(RuntimeError):
    """Raised when follower offset state is malformed.
    Callers should surface the message as a watch-mode diagnostic.
    """


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Stable file identity used to detect replacement.
    Device and inode map to stat fields on supported platforms.
    """

    device: int
    inode: int

    def to_json(self) -> dict[str, int]:
        """Convert the identity to persisted JSON.
        Integer fields keep the state portable and inspectable.
        """

        return {"device": self.device, "inode": self.inode}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FileIdentity:
        """Load a file identity from persisted JSON.
        Strict parsing keeps corrupted state from causing silent skips.
        """

        return cls(
            device=_required_int(data.get("device"), field_name="file_identity.device"),
            inode=_required_int(data.get("inode"), field_name="file_identity.inode"),
        )


@dataclass(frozen=True, slots=True)
class OffsetState:
    """Persisted byte offset for one followed log path.
    The offset always points to the last emitted newline boundary.
    """

    log_path: str
    offset: int
    file_identity: FileIdentity
    content_fingerprint: str | None

    def to_json(self) -> dict[str, object]:
        """Convert the state to persisted JSON.
        The log path guards against accidental hash collisions.
        """

        return {
            "schema_version": OFFSET_SCHEMA_VERSION,
            "log_path": self.log_path,
            "offset": self.offset,
            "file_identity": self.file_identity.to_json(),
            "content_fingerprint": self.content_fingerprint,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> OffsetState:
        """Load offset state from JSON.
        Invalid schemas fail before any log bytes are skipped.
        """

        schema_version = _required_int(
            data.get("schema_version"),
            field_name="schema_version",
        )
        if schema_version != OFFSET_SCHEMA_VERSION:
            raise LogFollowError(
                "Unsupported log follower schema "
                f"{schema_version}; expected {OFFSET_SCHEMA_VERSION}."
            )

        identity_value = data.get("file_identity")
        if not isinstance(identity_value, Mapping):
            raise LogFollowError("Missing or invalid file_identity; expected object.")

        offset = _required_int(data.get("offset"), field_name="offset")
        if offset < 0:
            raise LogFollowError("Invalid offset; expected a non-negative integer.")

        return cls(
            log_path=_required_str(data.get("log_path"), field_name="log_path"),
            offset=offset,
            file_identity=FileIdentity.from_json(data=identity_value),
            content_fingerprint=_optional_str(
                data.get("content_fingerprint"),
                field_name="content_fingerprint",
            ),
        )


@dataclass(frozen=True, slots=True)
class _OpenedLog:
    """Open binary file plus stat-derived metadata.
    Keeping these together avoids stat/open race surprises.
    """

    handle: BinaryIO
    identity: FileIdentity
    size: int


@dataclass(frozen=True, slots=True)
class _ReadResult:
    """Complete lines read from a byte offset.
    The next offset remains unchanged when the tail is partial.
    """

    lines: tuple[str, ...]
    next_offset: int


class LogFollower:
    """Poll Player.log and emit only complete raw lines.
    Offsets are persisted under Draftomen's app data directory.
    """

    def __init__(
        self,
        *,
        log_path: PathInput,
        app_dir: PathInput | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        previous_log_path: PathInput | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero.")

        self.log_path = Path(log_path).expanduser().resolve(strict=False)
        self.app_dir = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
        self.poll_interval = poll_interval
        self.previous_log_path = _default_previous_log_path(
            log_path=self.log_path,
            previous_log_path=previous_log_path,
        )
        self.offset_path = log_offset_path(log_path=self.log_path, app_dir=self.app_dir)
        if self.previous_log_path is None:
            logger.info("Following Arena log %s", redact_home(str(self.log_path)))
        else:
            logger.info(
                "Following Arena log %s (previous %s)",
                redact_home(str(self.log_path)),
                redact_home(str(self.previous_log_path)),
            )

    def poll(self) -> tuple[str, ...]:
        """Read complete lines appended since the persisted offset.
        Rotation recovery reads any matching Player-prev.log tail first.
        """

        state = self._load_state()
        opened = _open_log(path=self.log_path)
        if opened is None:
            return ()

        try:
            if state is None:
                return self._read_current(opened=opened, offset=0)

            self._ensure_state_matches_path(state=state)
            if opened.identity != state.file_identity:
                rotated_lines = self._read_rotated_tail(state=state)
                current_lines = self._read_current(opened=opened, offset=0)
                return rotated_lines + current_lines

            if opened.size < state.offset:
                return self._read_current(opened=opened, offset=0)

            if not _emitted_prefix_matches(handle=opened.handle, state=state):
                return self._read_current(opened=opened, offset=0)

            return self._read_current(opened=opened, offset=state.offset)
        finally:
            opened.handle.close()

    def follow(self) -> Iterator[str]:
        """Yield complete lines forever using the configured poll interval.
        The iterator is intentionally raw and performs no log parsing.
        """

        while True:
            yield from self.poll()
            time.sleep(self.poll_interval)

    def scan_startup_files(self, *, include_previous: bool = True) -> tuple[str, ...]:
        """Scan existing rotated and current logs from the beginning.
        The current log offset is advanced so later polling does not repeat it.
        """

        lines: list[str] = []
        if include_previous and self.previous_log_path is not None:
            previous_opened = _open_log(path=self.previous_log_path)
            if previous_opened is not None:
                try:
                    previous_result = _read_complete_lines(
                        handle=previous_opened.handle,
                        offset=0,
                    )
                    lines.extend(previous_result.lines)
                finally:
                    previous_opened.handle.close()

        try:
            current_opened = _open_log(path=self.log_path)
        except OSError:
            return tuple(lines)
        if current_opened is None:
            return tuple(lines)

        try:
            current_result = _read_complete_lines(handle=current_opened.handle, offset=0)
            lines.extend(current_result.lines)
            self._save_state(
                OffsetState(
                    log_path=str(self.log_path),
                    offset=current_result.next_offset,
                    file_identity=current_opened.identity,
                    content_fingerprint=_content_fingerprint(
                        handle=current_opened.handle,
                        offset=current_result.next_offset,
                    ),
                )
            )
        finally:
            current_opened.handle.close()

        return tuple(lines)

    def _read_current(self, *, opened: _OpenedLog, offset: int) -> tuple[str, ...]:
        result = _read_complete_lines(handle=opened.handle, offset=offset)
        self._save_state(
            OffsetState(
                log_path=str(self.log_path),
                offset=result.next_offset,
                file_identity=opened.identity,
                content_fingerprint=_content_fingerprint(
                    handle=opened.handle,
                    offset=result.next_offset,
                ),
            )
        )
        return result.lines

    def _read_rotated_tail(self, *, state: OffsetState) -> tuple[str, ...]:
        if self.previous_log_path is None:
            return ()

        previous_opened = _open_log(path=self.previous_log_path)
        if previous_opened is None:
            return ()

        try:
            if previous_opened.identity != state.file_identity:
                return ()

            if previous_opened.size < state.offset:
                return ()

            return _read_complete_lines(
                handle=previous_opened.handle,
                offset=state.offset,
            ).lines
        finally:
            previous_opened.handle.close()

    def _load_state(self) -> OffsetState | None:
        if not self.offset_path.exists():
            return None

        try:
            data = json.loads(self.offset_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise LogFollowError(
                f"Malformed log follower state {self.offset_path}: {error}."
            ) from error

        if not isinstance(data, Mapping):
            raise LogFollowError(
                f"Malformed log follower state {self.offset_path}: expected object."
            )

        return OffsetState.from_json(data=data)

    def _save_state(self, state: OffsetState) -> None:
        self.offset_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_json(), indent=2, sort_keys=True)
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=self.offset_path.parent,
            encoding="utf-8",
        ) as temporary_file:
            temporary_file.write(payload)
            temporary_file.write("\n")
            temporary_path = Path(temporary_file.name)

        temporary_path.replace(self.offset_path)

    def _ensure_state_matches_path(self, *, state: OffsetState) -> None:
        if state.log_path != str(self.log_path):
            raise LogFollowError(
                "Log follower state path does not match followed log: "
                f"{state.log_path!r} != {str(self.log_path)!r}."
            )


def log_offset_path(*, log_path: PathInput, app_dir: PathInput | None = None) -> Path:
    """Return the persisted offset path for a followed log.
    State files are keyed by a stable digest of the expanded log path.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
    resolved_log_path = Path(log_path).expanduser().resolve(strict=False)
    digest = hashlib.sha256(str(resolved_log_path).encode("utf-8")).hexdigest()
    return root / OFFSET_DIRECTORY_NAME / f"{digest[:STATE_HASH_LENGTH]}.json"


def _open_log(*, path: Path) -> _OpenedLog | None:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None

    try:
        stat_result = os.fstat(handle.fileno())
    except OSError:
        handle.close()
        raise

    return _OpenedLog(
        handle=handle,
        identity=FileIdentity(
            device=int(stat_result.st_dev),
            inode=int(stat_result.st_ino),
        ),
        size=int(stat_result.st_size),
    )


def is_log_readable(*, path: PathInput) -> bool:
    """Return whether a Player.log path can currently be opened for reading.
    Filesystem errors are reported as unavailable instead of escaping.
    """

    try:
        with Path(path).expanduser().resolve(strict=False).open("rb"):
            return True
    except OSError:
        return False


def detailed_logs_disabled(*, path: PathInput) -> bool:
    """Return whether Arena's log header says Detailed Logs are turned off.
    A missing header or unreadable file is unknown and returns False.
    """

    try:
        with Path(path).expanduser().resolve(strict=False).open("rb") as handle:
            header = handle.read(DETAILED_LOGS_HEADER_BYTES)
    except OSError:
        return False
    return DETAILED_LOGS_DISABLED_MARKER in header


def _read_complete_lines(*, handle: BinaryIO, offset: int) -> _ReadResult:
    handle.seek(offset)
    data = handle.read()
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return _ReadResult(lines=(), next_offset=offset)

    complete_end = last_newline + 1
    text = data[:complete_end].decode("utf-8", errors="replace")
    return _ReadResult(
        lines=tuple(text.splitlines()),
        next_offset=offset + complete_end,
    )


def _emitted_prefix_matches(*, handle: BinaryIO, state: OffsetState) -> bool:
    if state.content_fingerprint is None:
        return True

    return (
        _content_fingerprint(handle=handle, offset=state.offset)
        == state.content_fingerprint
    )


def _content_fingerprint(*, handle: BinaryIO, offset: int) -> str:
    hasher = hashlib.sha256()
    hasher.update(str(offset).encode("ascii"))
    hasher.update(b"\0")
    if offset <= FINGERPRINT_SAMPLE_BYTES * 2:
        handle.seek(0)
        hasher.update(handle.read(offset))
        return hasher.hexdigest()

    handle.seek(0)
    hasher.update(handle.read(FINGERPRINT_SAMPLE_BYTES))
    handle.seek(offset - FINGERPRINT_SAMPLE_BYTES)
    hasher.update(handle.read(FINGERPRINT_SAMPLE_BYTES))
    return hasher.hexdigest()


def _default_previous_log_path(
    *,
    log_path: Path,
    previous_log_path: PathInput | None,
) -> Path | None:
    if previous_log_path is not None:
        return Path(previous_log_path).expanduser().resolve(strict=False)

    if log_path.name != "Player.log":
        return None

    return log_path.with_name("Player-prev.log")


def _required_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise LogFollowError(f"Missing or invalid {field_name}; expected non-empty string.")

    return value


def _optional_str(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None

    return _required_str(value, field_name=field_name)


def _required_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise LogFollowError(f"Missing or invalid {field_name}; expected integer.")

    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise LogFollowError(f"Missing or invalid {field_name}; expected integer.") from error

