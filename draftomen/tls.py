"""Choose the certificate authorities that HTTPS downloads trust.
The system store comes first, with the bundled certifi file as the fallback.
"""

from __future__ import annotations

import logging
import os
import platform
import sys

import certifi

from draftomen.applog import redact_home

logger = logging.getLogger(__name__)


def use_system_trust_store() -> None:
    """Make every later default SSL context verify against the system trust store.
    Fall back to the bundled certifi CA file when the system store cannot be used.
    """

    # truststore imports ctypes, whose libffi closure allocation hangs under the
    # hardened runtime on Intel macOS 26 instead of raising, so skip it there.
    if _is_intel_macos():
        _use_certifi()
        return
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as error:
        logger.warning(
            "Could not use the system trust store, %s: %s",
            type(error).__name__,
            error,
        )
        _use_certifi()
        return
    logger.info("HTTPS verification uses the system trust store.")


def _is_intel_macos() -> bool:
    """Return True when this process runs as x86_64 on macOS."""

    return sys.platform == "darwin" and platform.machine() == "x86_64"


def _use_certifi() -> None:
    """Point OpenSSL at the bundled certifi CA file and log the choice."""

    # OpenSSL reads SSL_CERT_FILE when a default context loads its verify paths.
    os.environ["SSL_CERT_FILE"] = certifi.where()
    logger.info(
        "HTTPS verification uses the bundled certifi file %s",
        redact_home(certifi.where()),
    )
