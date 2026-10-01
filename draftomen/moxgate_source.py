"""Run the Moxgate draft source without any frontend framework.
A runtime owns the loopback receiver, an isolated live session and the snapshot feeder.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from draftomen.augmented_model_client import AugmentedModelClient
from draftomen.carddb import CardDatabase
from draftomen.cardimages import CardImageService
from draftomen.config import SPLASH
from draftomen.moxgate_server import (
    MOXGATE_ACCOUNT_ID,
    MOXGATE_DEFAULT_PORT,
    MOXGATE_SNAPSHOT_PATH,
    MoxgateReceiver,
    MoxgateSessionFeeder,
)
from draftomen.paths import PathInput
from draftomen.profile_client import ProfileClient
from draftomen.session import LiveSession, SnapshotPublisher

MoxgatePhase = Literal[
    "stopped", "starting", "waiting", "receiving", "port_in_use", "failed"
]


@dataclass(frozen=True, slots=True)
class MoxgateSourceState:
    """Describe where the Moxgate source is in its lifecycle.
    The endpoint is the URL the browser extension posts to, once known.
    """

    phase: MoxgatePhase = "stopped"
    port: int = MOXGATE_DEFAULT_PORT
    endpoint: str | None = None
    error: str | None = None


class MoxgateRuntime:
    """Own a started Moxgate receiver, its isolated session and its feeder.
    Call drain on the session thread and close once to free the port.
    """

    def __init__(
        self,
        *,
        receiver: MoxgateReceiver,
        session: LiveSession,
        feeder: MoxgateSessionFeeder,
        host: str,
    ) -> None:
        self._receiver = receiver
        self._session = session
        self._feeder = feeder
        host_label = f"[{host}]" if ":" in host else host
        self._state = MoxgateSourceState(
            phase="waiting",
            port=receiver.port,
            endpoint=f"http://{host_label}:{receiver.port}{MOXGATE_SNAPSHOT_PATH}",
        )
        self._closed = False

    @property
    def session(self) -> LiveSession:
        """Return the isolated session that the snapshots drive.
        It never reads an Arena log.
        """

        return self._session

    @property
    def state(self) -> MoxgateSourceState:
        """Return the current source state.
        It is waiting until a drain takes a snapshot, then receiving.
        """

        return self._state

    def drain(self) -> int:
        """Feed every queued snapshot to the session.
        Returns how many snapshots were taken and moves the phase to receiving.
        """

        taken = self._feeder.drain()
        if taken and self._state.phase == "waiting":
            self._state = replace(self._state, phase="receiving")

        return taken

    def close(self) -> None:
        """Stop the receiver and the session.
        The port is freed even if stopping the session fails. A second call does nothing.
        """

        if self._closed:
            return

        self._closed = True
        try:
            self._receiver.stop()
        finally:
            self._session.stop()
            self._state = replace(self._state, phase="stopped")


def create_moxgate_runtime(
    *,
    card_database: CardDatabase,
    canonical_grp_ids_by_scryfall_id: Mapping[str, int],
    snapshot_publisher: SnapshotPublisher | None,
    app_dir: PathInput | None,
    port: int = MOXGATE_DEFAULT_PORT,
    host: str = "127.0.0.1",
    profile_client: ProfileClient | None = None,
    card_image_service: CardImageService | None = None,
    augmented_model_client: AugmentedModelClient | None = None,
    splash_enabled: bool = SPLASH.enabled_by_default,
    contextual_adjustments_enabled: bool = True,
) -> MoxgateRuntime:
    """Bind the receiver, then build the session and feeder around it.
    A bind failure raises MoxgatePortInUseError and leaves nothing running.
    """

    receiver = MoxgateReceiver(host=host, port=port)
    # Bind first so a port in use leaves no session behind.
    receiver.start()
    try:
        session = LiveSession(
            log_path=None,
            app_dir=app_dir,
            card_database=card_database,
            profile_client=profile_client,
            snapshot_publisher=snapshot_publisher,
            card_image_service=card_image_service,
            augmented_model_client=augmented_model_client,
            splash_enabled=splash_enabled,
            contextual_adjustments_enabled=contextual_adjustments_enabled,
        )
        try:
            feeder = MoxgateSessionFeeder(
                snapshots=receiver.snapshots,
                session=session,
                card_database=card_database,
                canonical_grp_ids_by_scryfall_id=canonical_grp_ids_by_scryfall_id,
                account_id=MOXGATE_ACCOUNT_ID,
            )
            return MoxgateRuntime(
                receiver=receiver, session=session, feeder=feeder, host=host
            )
        except BaseException:
            session.stop()
            raise
    except BaseException:
        receiver.stop()
        raise
