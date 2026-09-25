"""Path resolution for Draftomen.
Keep OS-specific Player.log defaults isolated from the rest of the package.
"""

from __future__ import annotations

import platform
from os import PathLike
from pathlib import Path, PureWindowsPath
from typing import TypeAlias

PathInput: TypeAlias = str | PathLike[str]
ResolvedPath: TypeAlias = Path | PureWindowsPath

MACOS_PLAYER_LOG_RELATIVE_PATH = Path(
    "Library",
    "Logs",
    "Wizards Of The Coast",
    "MTGA",
    "Player.log",
)
WINDOWS_PLAYER_LOG_RELATIVE_PATH = PureWindowsPath(
    "AppData",
    "LocalLow",
    "Wizards Of The Coast",
    "MTGA",
    "Player.log",
)
APP_DATA_DIRECTORY_NAME = ".draftomen"
# Profile and augmented-model pipelines cache downloads here, relative to the
# working directory, so the user data directory holds only what the app uses.
DEVELOPER_CACHE_DIR = Path(".draftomen") / "corpus-cache"


class UnsupportedPlatformError(RuntimeError):
    """Raised when no default Player.log path exists for the OS.
    Pass an explicit path to support unsupported environments.
    """


def app_data_dir(
    home: PathInput | None = None,
    *,
    system: str | None = None,
) -> ResolvedPath:
    """Return Draftomen's per-user app data directory.
    The directory lives under the current OS user's home path.
    """

    current_system = platform.system() if system is None else system
    if current_system == "Windows":
        return _windows_home(home=home) / APP_DATA_DIRECTORY_NAME

    return _posix_home(home=home) / APP_DATA_DIRECTORY_NAME


def default_player_log_path(
    *,
    home: PathInput | None = None,
    system: str | None = None,
) -> ResolvedPath:
    """Return the default MTG Arena Player.log path for the OS.
    macOS is primary, Windows is best-effort, and other OSes require override.
    """

    current_system = platform.system() if system is None else system
    if current_system == "Darwin":
        return _posix_home(home=home) / MACOS_PLAYER_LOG_RELATIVE_PATH

    if current_system == "Windows":
        return _windows_home(home=home) / WINDOWS_PLAYER_LOG_RELATIVE_PATH

    raise UnsupportedPlatformError(
        "No default MTG Arena Player.log path is known for "
        f"{current_system}. Pass --log-path to use an explicit file."
    )


def resolve_player_log_path(
    log_path: PathInput | None = None,
    *,
    home: PathInput | None = None,
    system: str | None = None,
) -> ResolvedPath:
    """Resolve Player.log, honoring an explicit override first.
    Without an override, return the platform default for the current user.
    """

    current_system = platform.system() if system is None else system
    if log_path is not None:
        if current_system == "Windows":
            return PureWindowsPath(log_path)

        return Path(log_path).expanduser()

    return default_player_log_path(home=home, system=current_system)


def resolve_log_path(
    log_path: PathInput | None = None,
    *,
    home: PathInput | None = None,
    system: str | None = None,
) -> ResolvedPath:
    """Compatibility wrapper for Player.log resolution.
    Prefer resolve_player_log_path in new code.
    """

    return resolve_player_log_path(log_path=log_path, home=home, system=system)


def _posix_home(home: PathInput | None = None) -> Path:
    if home is None:
        return Path.home()

    return Path(home).expanduser()


def _windows_home(home: PathInput | None = None) -> PureWindowsPath:
    if home is None:
        return PureWindowsPath(Path.home())

    return PureWindowsPath(home)
