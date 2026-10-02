"""Choose the certificate authorities that HTTPS downloads trust.
The system store comes first, with the bundled certifi file as the fallback.
"""

from __future__ import annotations

import logging
import os

import certifi

from draftomen.applog import redact_home

logger = logging.getLogger(__name__)


def use_system_trust_store() -> None:
    """Make every later default SSL context verify against the system trust store.
    Fall back to the bundled certifi CA file when the system store cannot be used.
    """

    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as error:
        logger.warning(
            "Could not use the system trust store, %s: %s",
            type(error).__name__,
            error,
        )
        # OpenSSL reads SSL_CERT_FILE when a default context loads its verify paths.
        os.environ["SSL_CERT_FILE"] = certifi.where()
        logger.info(
            "HTTPS verification uses the bundled certifi file %s",
            redact_home(certifi.where()),
        )
        return
    logger.info("HTTPS verification uses the system trust store.")
