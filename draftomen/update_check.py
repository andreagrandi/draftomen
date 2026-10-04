"""Find out whether a newer Draft Omen release exists.
The website publishes the latest version, and the check runs off the caller's thread.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from collections.abc import Callable

from packaging.version import Version

from draftomen import __version__

logger = logging.getLogger(__name__)

DEFAULT_VERSION_URL = "https://www.draftomen.com/version.json"
UNKNOWN_VERSION = "0.0.0+unknown"
_TIMEOUT_SECONDS = 5.0
_MAX_RESPONSE_BYTES = 64 * 1024


def newer_version(*, installed: str, published: str) -> str | None:
    """Return the published version when PEP 440 orders it after the installed one.
    Raise InvalidVersion when either string is not a valid version.
    """

    if Version(published) > Version(installed):
        return published
    return None


def fetch_published_version(*, url: str, timeout_seconds: float = _TIMEOUT_SECONDS) -> str:
    """Download version.json and return its version string.
    Raise on network, HTTP and format errors.
    """

    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"draftomen/{__version__}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read(_MAX_RESPONSE_BYTES))
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str):
        raise ValueError(f"version.json has no version string: {payload!r}")
    return version


def check_for_update(
    *,
    url: str = DEFAULT_VERSION_URL,
    installed: str = __version__,
    timeout_seconds: float = _TIMEOUT_SECONDS,
) -> str | None:
    """Return the newer published version, or None when there is none.
    Any failure is logged as a warning and returns None.
    """

    if installed == UNKNOWN_VERSION:
        logger.warning("Skipping the update check because the installed version is unknown")
        return None
    try:
        published = fetch_published_version(url=url, timeout_seconds=timeout_seconds)
        return newer_version(installed=installed, published=published)
    except Exception as error:
        logger.warning("Update check against %s failed: %s", url, error)
        return None


def start_update_check(
    *,
    on_result: Callable[[str | None], None],
    url: str = DEFAULT_VERSION_URL,
    installed: str = __version__,
    timeout_seconds: float = _TIMEOUT_SECONDS,
) -> threading.Thread:
    """Run check_for_update on a daemon thread and pass its result to on_result.
    on_result runs on that thread, so a GUI caller must hand it to its own thread.
    """

    def run() -> None:
        on_result(check_for_update(url=url, installed=installed, timeout_seconds=timeout_seconds))

    thread = threading.Thread(target=run, name="draftomen-update-check", daemon=True)
    thread.start()
    return thread

