"""Find out whether a newer Draft Omen release exists.
The website publishes the latest version, and the Microsoft Store build asks the Store instead.
The check runs off the caller's thread.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import sys
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

STORE_CHANNEL = "store"
GITHUB_CHANNEL = "github"
WEBSITE_CHANNEL = "website"
_APPMODEL_ERROR_NO_PACKAGE = 15700
_ERROR_INSUFFICIENT_BUFFER = 122


def newer_version(*, installed: str, published: str) -> str | None:
    """Return the published version when PEP 440 orders it after the installed one.
    Raise InvalidVersion when either string is not a valid version.
    """

    if Version(published) > Version(installed):
        return published
    return None


def is_store_install(*, platform: str = sys.platform, kernel32: object | None = None) -> bool:
    """Return True when the process runs from an MSIX package such as the Microsoft Store build.
    Any failure to ask Windows is logged as a warning and counts as not packaged.
    """

    if platform != "win32":
        return False
    try:
        library = kernel32 if kernel32 is not None else ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        length = ctypes.c_uint32(0)
        code = library.GetCurrentPackageFullName(ctypes.byref(length), None)  # type: ignore[attr-defined]
    except Exception as error:
        logger.warning("Could not tell whether this is a packaged install: %s", error)
        return False
    return code in (0, _ERROR_INSUFFICIENT_BUFFER)


def install_channel(*, platform: str = sys.platform, kernel32: object | None = None) -> str:
    """Return the channel this build was installed from: store, github or website.
    Windows outside a package is the unsigned GitHub exe, and everything else downloads from the website.
    """

    if is_store_install(platform=platform, kernel32=kernel32):
        return STORE_CHANNEL
    if platform == "win32":
        return GITHUB_CHANNEL
    return WEBSITE_CHANNEL


def app_version_from_package_version(*, package_version: str) -> str:
    """Turn a four-part MSIX package version into the app version by dropping the revision.
    Raise ValueError unless the input has exactly four integer parts.
    """

    parts = package_version.split(".")
    if len(parts) != 4 or not all(part.isascii() and part.isdigit() for part in parts):
        raise ValueError(f"Not a four-part package version: {package_version!r}")
    return ".".join(parts[:3])


def query_store_update_version() -> str | None:
    """Ask the Microsoft Store for a pending update of this package and return its four-part version.
    Return None when the Store lists no update for this package. Raise on errors.
    """

    from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
    from winrt.windows.applicationmodel import Package
    from winrt.windows.services.store import StoreContext

    async def query() -> str | None:
        context = StoreContext.get_default()
        updates = await context.get_app_and_optional_store_package_updates_async()
        family_name = Package.current.id.family_name
        for update in updates:
            package_id = update.package.id
            if package_id.family_name == family_name:
                version = package_id.version
                return f"{version.major}.{version.minor}.{version.build}.{version.revision}"
        return None

    # The check runs on its own worker thread, which needs a COM apartment for WinRT calls.
    init_apartment(ApartmentType.MULTI_THREADED)
    try:
        return asyncio.run(query())
    finally:
        uninit_apartment()


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
    store: bool = False,
    query_store: Callable[[], str | None] = query_store_update_version,
) -> str | None:
    """Return the newer published version, or None when there is none.
    With store set, the Microsoft Store answers and the URL is never used. Any failure is logged and returns None.
    """

    if installed == UNKNOWN_VERSION:
        logger.warning("Skipping the update check because the installed version is unknown")
        return None
    if store:
        try:
            package_version = query_store()
            if package_version is None:
                return None
            published = app_version_from_package_version(package_version=package_version)
            return newer_version(installed=installed, published=published)
        except Exception as error:
            logger.warning("Microsoft Store update check failed: %s", error)
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
    store: bool = False,
) -> threading.Thread:
    """Run check_for_update on a daemon thread and pass its result to on_result.
    on_result runs on that thread, so a GUI caller must hand it to its own thread.
    """

    def run() -> None:
        on_result(
            check_for_update(
                url=url,
                installed=installed,
                timeout_seconds=timeout_seconds,
                store=store,
            )
        )

    thread = threading.Thread(target=run, name="draftomen-update-check", daemon=True)
    thread.start()
    return thread

