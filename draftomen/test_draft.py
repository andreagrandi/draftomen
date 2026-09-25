"""Drive a simulated Draftmancer draft through production recommendations.
The controller is UI-neutral so CLIs and native adapters share one contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
import gzip
import json
from pathlib import Path
import tempfile
from threading import Lock, RLock
from typing import Any, Literal, Self, TypeAlias
import uuid
import zlib

from draftomen.augmented_model_client import AugmentedModelClient
from draftomen.card_data_client import CardDataClient
from draftomen.carddb import (
    CardDatabase,
    CardInfo,
    build_card_database_from_bulk_file,
    build_card_database_from_scryfall_cards,
)
from draftomen.cardimages import CardImageService
from draftomen.corpus import DEFAULT_CACHE_DIR
from draftomen.draftmancer import (
    DraftmancerAdapter,
    DraftmancerAdapterError,
    DraftmancerConfig,
    intersect_supported_set_codes,
)
from draftomen.events import PackOfferedEvent
from draftomen.paths import app_data_dir
from draftomen.profile_client import ProfileClient, ProfileNetworkPolicy
from draftomen.session import (
    ApplicationPhase,
    DataLoadPhase,
    LiveSession,
    LiveSessionSnapshot,
    OperationKind,
    RequestBuild,
    SnapshotPublisher,
)

DRAFTMANCER_TEST_USER_NAME = "Draft Omen test draft"
SIMULATION_DIRECTORY_PREFIX = "draftomen-test-draft-"
SCRYFALL_DEFAULT_CARDS_BULK_FILE_NAME = "scryfall-default-cards.jsonl.gz"
DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE = (
    DEFAULT_CACHE_DIR / "sources" / SCRYFALL_DEFAULT_CARDS_BULK_FILE_NAME
)
DEFAULT_TEST_DRAFT_CHECKOUT_DIRECTORY_NAME = "draftmancer"
DEFAULT_TEST_DRAFT_SERVER_URL = "http://127.0.0.1:3000"
DEFAULT_TEST_DRAFT_SET_CODE = "HOB"
DEFAULT_TEST_DRAFT_TIMEOUT_SECONDS = 10.0
BUILD_FAILED_ERROR_CODE = "build_failed"

TestDraftStage: TypeAlias = Literal["startup", "readiness", "drafting", "build"]


class TestDraftError(RuntimeError):
    """Report one observable test-draft failure with its trustworthy state.
    Callers inspect the stage, accepted steps, and last published snapshot.
    """

    # The Test-prefixed public names are deliberately not pytest test classes.
    __test__ = False

    def __init__(
        self,
        message: str,
        *,
        stage: TestDraftStage,
        steps: tuple[TestDraftStep, ...] = (),
        snapshot: LiveSessionSnapshot | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.steps = steps
        self.snapshot = LiveSessionSnapshot() if snapshot is None else snapshot


@dataclass(frozen=True, slots=True)
class TestDraftOfferIdentity:
    """Identify one offered pack together with the pool that preceded it.
    Equality is the readiness token that guards one confirmed pick.
    """

    __test__ = False

    account_id: str | None
    event_name: str
    set_code: str
    pack_number: int
    pick_number: int
    offered_grp_ids: tuple[int, ...]
    pool_grp_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TestDraftInspection:
    """Pair one ready offered pack with the snapshot that published it.
    The identity is the only token a caller may confirm.
    """

    __test__ = False

    offer: TestDraftOfferIdentity
    snapshot: LiveSessionSnapshot


@dataclass(frozen=True, slots=True)
class TestDraftStep:
    """Record one accepted pick and the state observed around it.
    The before inspection holds the recommendation row that was accepted.
    """

    __test__ = False

    before: TestDraftInspection
    grp_id: int
    unique_card_id: int
    after: LiveSessionSnapshot


@dataclass(frozen=True, slots=True)
class TestDraftSet:
    """Describe one Draftmancer-supported set offered for a simulated draft.
    The cached flag reports whether local card data already exists.
    """

    __test__ = False

    code: str
    name: str
    card_data_cached: bool


@dataclass(frozen=True, slots=True)
class TestDraftRunResult:
    """Expose one automatic run's steps, completion, and build snapshot.
    The build snapshot is the published result of the normal build request.
    """

    __test__ = False

    steps: tuple[TestDraftStep, ...]
    completed: LiveSessionSnapshot
    build: LiveSessionSnapshot


class TestDraftController:
    """Confirm or advance simulated picks from production recommendation state.
    Manual callers confirm inspected offers; automatic callers follow rank one.
    """

    # The Test-prefixed public names are deliberately not pytest test classes.
    __test__ = False

    def __init__(
        self,
        *,
        session: LiveSession,
        adapter: DraftmancerAdapter,
    ) -> None:
        self._session = session
        self._adapter = adapter
        self._lock = RLock()
        self._steps: list[TestDraftStep] = []
        self._consumed_offers: set[TestDraftOfferIdentity] = set()
        self._started = False
        self._closed = False

    def start(self) -> TestDraftInspection:
        """Connect, start the simulated draft, and inspect the first offered pack.
        Selecting or inspecting a card never submits it.
        """

        with self._lock:
            self._raise_if_closed()
            if self._started:
                raise self._error(
                    message="the simulated draft has already started.",
                    stage="readiness",
                )
            try:
                self._adapter.connect_and_start()
            except Exception as error:
                raise self._error(
                    message=f"the simulated draft did not start: {error}",
                    stage="startup",
                ) from error
            self._started = True
            return self._inspect_locked()

    def inspect(self) -> TestDraftInspection:
        """Return the ready offered pack and its snapshot without side effects.
        Unsynchronized, completed, or stale pack state is never returned.
        """

        with self._lock:
            self._raise_if_closed()
            return self._inspect_locked()

    def confirm(
        self,
        *,
        grp_id: int,
        expected_offer: TestDraftOfferIdentity,
    ) -> TestDraftStep:
        """Submit one recommended card for the offer the caller inspected.
        A stale, superseded, or already consumed token never submits a pick.
        """

        with self._lock:
            self._raise_if_closed()
            inspection = self._inspect_locked()
            identity = inspection.offer
            if expected_offer != identity:
                raise self._error(
                    message=(
                        "the confirmed offer token is not the current offered pack; "
                        "inspect the current pack before confirming."
                    ),
                    stage="readiness",
                )
            if expected_offer in self._consumed_offers:
                raise self._error(
                    message="this offered pack was already confirmed.",
                    stage="readiness",
                )
            unique_card_id = self._instance_for_card(identity=identity, grp_id=grp_id)
            self._require_recommended_card(
                inspection=inspection,
                grp_id=grp_id,
            )
            self._consumed_offers.add(expected_offer)
            try:
                self._adapter.pick(unique_card_id=unique_card_id)
            except Exception as error:
                raise self._error(
                    message=f"the simulator did not accept the pick: {error}",
                    stage="drafting",
                ) from error
            after = self._validated_after_locked(identity=identity, grp_id=grp_id)
            step = TestDraftStep(
                before=inspection,
                grp_id=grp_id,
                unique_card_id=unique_card_id,
                after=after,
            )
            self._steps.append(step)
            return step

    def advance_auto(self) -> TestDraftStep:
        """Confirm the first ranked card of a freshly inspected offered pack.
        Rankings are re-read per call so a profile refresh is never stale.
        """

        with self._lock:
            self._raise_if_closed()
            inspection = self._inspect_locked()
            return self.confirm(
                grp_id=inspection.snapshot.recommendations.cards[0].card.grp_id,
                expected_offer=inspection.offer,
            )

    def run_auto(self) -> TestDraftRunResult:
        """Draft every offered pack automatically and build the finished pool.
        The run must be the controller's first interaction.
        """

        with self._lock:
            self._raise_if_closed()
            if self._started:
                raise self._error(
                    message="an automatic run must start from a fresh controller.",
                    stage="readiness",
                )
            self.start()
            while not self._session_completed_locked():
                self.advance_auto()
            completed = self._session.snapshot
            if not self._adapter.completed:
                raise self._error(
                    message=(
                        "the session completed the draft before the simulator "
                        "reported completion."
                    ),
                    stage="drafting",
                )
            try:
                build_snapshot = self._session.dispatch(command=RequestBuild())
            except Exception as error:
                raise self._error(
                    message=f"the deck build failed: {error}",
                    stage="build",
                ) from error
            self._require_build_locked(snapshot=build_snapshot)
            return TestDraftRunResult(
                steps=tuple(self._steps),
                completed=completed,
                build=build_snapshot,
            )

    def close(self) -> None:
        """Close the simulator connection once; later calls are no-ops."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._adapter.close()

    def _inspect_locked(
        self,
        *,
        stage: TestDraftStage = "readiness",
        accepted_picks: int | None = None,
    ) -> TestDraftInspection:
        """Validate the current snapshot as one ready offered pack.
        The accepted-pick count keeps a superseded offer from looking ready.
        """

        self._raise_if_closed()
        if not self._started:
            raise self._error(
                message="the simulated draft has not started.",
                stage=stage,
            )
        snapshot = self._session.snapshot
        if snapshot.status.phase is not ApplicationPhase.DRAFTING:
            raise self._error(
                message=(
                    "the simulated draft is not drafting "
                    f"(phase: {snapshot.status.phase.value})."
                ),
                stage=stage,
            )
        draft = snapshot.draft
        if draft is None or draft.completed:
            raise self._error(
                message="the simulated draft has no active draft identity.",
                stage=stage,
            )
        event = snapshot.current_pack_event
        if event is None or not event.offered_grp_ids:
            raise self._error(
                message="no offered pack is ready to pick from.",
                stage=stage,
            )
        if snapshot.current_scored_pack is None:
            raise self._error(
                message="the offered pack has not been scored yet.",
                stage=stage,
            )
        if not snapshot.recommendations.cards:
            raise self._error(
                message="the offered pack has no recommendations.",
                stage=stage,
            )
        if snapshot.card_data.phase is not DataLoadPhase.READY:
            raise self._error(
                message="card metadata is not ready for the offered pack.",
                stage=stage,
            )
        identity = _offer_identity(event=event)
        if draft.set_code.casefold() != identity.set_code:
            raise self._error(
                message="the offered pack set does not match the active draft.",
                stage=stage,
            )
        if (draft.pack_number, draft.pick_number) != (
            identity.pack_number,
            identity.pick_number,
        ):
            raise self._error(
                message=(
                    "the session is not positioned on pack "
                    f"{identity.pack_number + 1} pick {identity.pick_number + 1}."
                ),
                stage=stage,
            )
        accepted = len(self._steps) if accepted_picks is None else accepted_picks
        if len(identity.pool_grp_ids) != accepted:
            raise self._error(
                message="the offered pack pool does not match the accepted picks.",
                stage=stage,
            )
        if snapshot.pool.total_cards != len(identity.pool_grp_ids):
            raise self._error(
                message="the offered pack pool does not match the published pool.",
                stage=stage,
            )
        if len(self._adapter.offered_instance_ids) != len(identity.offered_grp_ids):
            raise self._error(
                message="the simulator offer does not map to the offered pack.",
                stage=stage,
            )
        return TestDraftInspection(offer=identity, snapshot=snapshot)

    def _instance_for_card(
        self,
        *,
        identity: TestDraftOfferIdentity,
        grp_id: int,
    ) -> int:
        """Resolve one offered card to its first simulator instance.
        Duplicate copies stay deterministic because offered order is stable.
        """

        instance_ids = self._adapter.offered_instance_ids
        if len(instance_ids) != len(identity.offered_grp_ids):
            raise self._error(
                message="the simulator offer no longer maps to the offered pack.",
                stage="readiness",
            )
        for instance_id, offered_grp_id in zip(
            instance_ids,
            identity.offered_grp_ids,
            strict=True,
        ):
            if offered_grp_id == grp_id:
                return instance_id
        raise self._error(
            message=f"card {grp_id} is not in the offered pack.",
            stage="readiness",
        )

    def _require_recommended_card(
        self,
        *,
        inspection: TestDraftInspection,
        grp_id: int,
    ) -> None:
        """Require the confirmed card to have a row in the offered pack.
        Manual confirmation accepts any ranked card, including non-top rows.
        """

        if not any(
            row.card.grp_id == grp_id
            for row in inspection.snapshot.recommendations.cards
        ):
            raise self._error(
                message=f"card {grp_id} has no recommendation for this pack.",
                stage="readiness",
            )

    def _validated_after_locked(
        self,
        *,
        identity: TestDraftOfferIdentity,
        grp_id: int,
    ) -> LiveSessionSnapshot:
        """Require the accepted pick to reach the next pack or completion.
        The measured transition is the adapter's own published state.
        """

        if self._adapter.completed:
            snapshot = self._session.snapshot
            if snapshot.status.phase is not ApplicationPhase.DRAFT_COMPLETE:
                raise self._error(
                    message=(
                        "the simulator completed the draft but the session did "
                        "not publish completion."
                    ),
                    stage="drafting",
                )
            draft = snapshot.draft
            if draft is None or not draft.completed:
                raise self._error(
                    message="the completed draft has no completed draft identity.",
                    stage="drafting",
                )
            if (
                snapshot.current_pack_event is not None
                or snapshot.current_scored_pack is not None
                or snapshot.recommendations.cards
            ):
                raise self._error(
                    message="the completed draft still publishes pack state.",
                    stage="drafting",
                )
            if snapshot.pool.total_cards != len(identity.pool_grp_ids) + 1:
                raise self._error(
                    message="the completed pool does not contain the accepted pick.",
                    stage="drafting",
                )
            return snapshot

        inspection = self._inspect_locked(
            stage="drafting",
            accepted_picks=len(self._steps) + 1,
        )
        next_offer = inspection.offer
        if (next_offer.pack_number, next_offer.pick_number) <= (
            identity.pack_number,
            identity.pick_number,
        ):
            raise self._error(
                message="the simulator did not advance to a new offered pack.",
                stage="drafting",
            )
        if next_offer.pool_grp_ids != identity.pool_grp_ids + (grp_id,):
            raise self._error(
                message=(
                    "the next offered pack does not carry the accepted pick in "
                    "the production pool."
                ),
                stage="drafting",
            )
        return inspection.snapshot

    def _require_build_locked(self, *, snapshot: LiveSessionSnapshot) -> None:
        """Require the build request to return a complete structured deck.
        A failed build keeps its own error instead of a partial deck.
        """

        failure = next(
            (
                error
                for error in snapshot.errors
                if error.code == BUILD_FAILED_ERROR_CODE
                and error.operation is OperationKind.BUILD
            ),
            None,
        )
        if failure is not None:
            raise self._error(
                message=f"the deck build failed: {failure.message}",
                stage="build",
            )
        build = snapshot.build
        if build is None:
            raise self._error(
                message="the deck build produced no result.",
                stage="build",
            )
        if (
            build.domain_pool is None
            or build.domain_selection is None
            or build.domain_spell_selection is None
            or build.domain_mana_base is None
        ):
            raise self._error(
                message="the deck build result is missing structured domain output.",
                stage="build",
            )

    def _session_completed_locked(self) -> bool:
        return self._session.snapshot.status.phase is ApplicationPhase.DRAFT_COMPLETE

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise self._error(
                message="the test-draft controller is closed.",
                stage="readiness",
            )

    def _error(self, *, message: str, stage: TestDraftStage) -> TestDraftError:
        return TestDraftError(
            message,
            stage=stage,
            steps=tuple(self._steps),
            snapshot=self._session.snapshot,
        )


class TestDraftRuntime:
    """Own one reusable simulated draft and its isolated runtime resources.
    Callers cancel in-flight work; close retires every owned resource once.
    """

    # The Test-prefixed public name is deliberately not a pytest test class.
    __test__ = False

    def __init__(
        self,
        *,
        session: LiveSession,
        controller: TestDraftController,
        adapter: DraftmancerAdapter,
        profile_client: ProfileClient,
        simulation_app_dir: Path,
        stack: ExitStack,
    ) -> None:
        self._session = session
        self._controller = controller
        self._adapter = adapter
        self._profile_client = profile_client
        self._simulation_app_dir = simulation_app_dir
        self._stack = stack
        self._cleanup_lock = Lock()
        self._closed = False

    @property
    def session(self) -> LiveSession:
        """Return the source-less session that owns simulated draft state."""

        return self._session

    @property
    def controller(self) -> TestDraftController:
        """Return the controller that confirms and advances simulated picks."""

        return self._controller

    @property
    def profile_client(self) -> ProfileClient:
        """Return the hosted profile client that serves the simulated set."""

        return self._profile_client

    @property
    def simulation_app_dir(self) -> Path:
        """Return the isolated directory that holds simulated draft artifacts."""

        return self._simulation_app_dir

    def cancel(self) -> None:
        """Wake blocked draft work without acquiring the controller lock.
        Cancellation is terminal for this runtime; close owns final cleanup.
        """

        self._adapter.cancel()

    def close(self) -> None:
        """Cancel in-flight work and retire every owned resource once.
        Cleanup runs outside the runtime lock in reverse ownership order.
        """

        with self._cleanup_lock:
            if self._closed:
                return
            self._closed = True
        self.cancel()
        self._stack.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()


def supported_test_draft_set_codes(
    *,
    draftmancer_dir: Path,
    draftomen_set_codes: Iterable[str],
) -> tuple[str, ...]:
    """Intersect requested Draft Omen set codes with the pinned simulator's.
    Malformed capability metadata fails closed before any network work.
    """

    draftmancer_codes = _load_supported_set_codes(draftmancer_dir=draftmancer_dir)
    try:
        return intersect_supported_set_codes(
            draftomen_set_codes=draftomen_set_codes,
            draftmancer_set_codes=draftmancer_codes,
        )
    except DraftmancerAdapterError as error:
        raise TestDraftError(
            f"Draftmancer set capability metadata is invalid: {error}",
            stage="startup",
        ) from error


def supported_test_draft_sets(
    *,
    draftmancer_dir: Path,
    cached_set_codes: Iterable[str],
) -> tuple[TestDraftSet, ...]:
    """List every set the pinned simulator supports, ordered by full name.
    Sets missing from Draftmancer's set metadata fall back to their code.
    """

    draftmancer_codes = _load_supported_set_codes(draftmancer_dir=draftmancer_dir)
    codes = supported_test_draft_set_codes(
        draftmancer_dir=draftmancer_dir,
        draftomen_set_codes=draftmancer_codes,
    )
    names = _load_set_names(draftmancer_dir=draftmancer_dir)
    cached = {code.casefold() for code in cached_set_codes}
    sets = (
        TestDraftSet(
            code=code,
            name=names.get(code, code.upper()),
            card_data_cached=code in cached,
        )
        for code in codes
    )
    return tuple(sorted(sets, key=lambda item: (item.name.casefold(), item.code)))


def create_test_draft_runtime(
    *,
    draftmancer_dir: Path,
    scryfall_bulk_file: Path,
    server_url: str,
    set_code: str,
    timeout_seconds: float,
    source_app_dir: Path | None,
    profile_manifest_url: str | None,
    profile_network_policy: ProfileNetworkPolicy,
    snapshot_publisher: SnapshotPublisher | None = None,
    splash_enabled: bool = True,
    contextual_adjustments_enabled: bool = True,
    simulation_app_dir: Path | None = None,
    socket_client: object | None = None,
    card_image_service: CardImageService | None = None,
    augmented_model_client: AugmentedModelClient | None = None,
) -> TestDraftRuntime:
    """Create one isolated simulated draft runtime from validated sources.
    The returned runtime owns its session, adapter, and temporary directory.
    Every construction failure unwinds the resources built before it.
    """

    normalized_set_code = _normalized_set_code(set_code=set_code)
    supported_codes = supported_test_draft_set_codes(
        draftmancer_dir=draftmancer_dir,
        draftomen_set_codes=(normalized_set_code,),
    )
    if normalized_set_code not in supported_codes:
        raise TestDraftError(
            f"requested set {normalized_set_code!r} is not supported by Draftmancer",
            stage="startup",
        )
    try:
        card_database = CardDataClient(app_dir=source_app_dir).load(
            normalized_set_code,
            allow_network=True,
        )
    except Exception as error:
        raise TestDraftError(
            f"card data for set {normalized_set_code!r} is unavailable: {error}",
            stage="startup",
        ) from error
    bulk_scan = _scan_scryfall_bulk(
        bulk_path=scryfall_bulk_file,
        set_code=normalized_set_code,
        card_database=card_database,
    )
    try:
        profile_client = ProfileClient(
            app_dir=source_app_dir,
            manifest_url=profile_manifest_url,
            network_policy=profile_network_policy,
        )
    except Exception as error:
        raise TestDraftError(
            f"set profile source configuration is invalid: {error}",
            stage="startup",
        ) from error
    resolved_source_dir = _resolved_app_dir(app_dir=source_app_dir)
    if simulation_app_dir is not None:
        try:
            resolved_simulation_dir = _resolved_app_dir(app_dir=simulation_app_dir)
        except Exception as error:
            raise TestDraftError(
                f"the test draft could not start: {error}",
                stage="startup",
            ) from error
        if resolved_simulation_dir == resolved_source_dir:
            raise TestDraftError(
                "the simulation directory must differ from the card and profile "
                "source directory",
                stage="startup",
            )
    run_id = uuid.uuid4().hex
    try:
        config = DraftmancerConfig(
            server_url=server_url,
            session_id=f"draftomen-test-draft-{run_id}",
            user_id=f"draftomen-test-draft-user-{run_id}",
            user_name=DRAFTMANCER_TEST_USER_NAME,
            set_code=normalized_set_code,
            timeout_seconds=timeout_seconds,
        )
    except Exception as error:
        raise TestDraftError(
            f"Draftmancer configuration is invalid: {error}",
            stage="startup",
        ) from error

    stack = ExitStack()
    try:
        try:
            if simulation_app_dir is None:
                simulation_dir = Path(
                    stack.enter_context(
                        tempfile.TemporaryDirectory(prefix=SIMULATION_DIRECTORY_PREFIX)
                    )
                )
            else:
                simulation_dir = Path(simulation_app_dir).expanduser()
            session = LiveSession(
                log_path=None,
                app_dir=simulation_dir,
                card_database=card_database,
                profile_client=profile_client,
                snapshot_publisher=snapshot_publisher,
                card_image_service=card_image_service,
                augmented_model_client=augmented_model_client,
                splash_enabled=splash_enabled,
                contextual_adjustments_enabled=contextual_adjustments_enabled,
            )
            stack.callback(session.stop)
            adapter = DraftmancerAdapter(
                config=config,
                card_database=card_database,
                canonical_grp_ids_by_scryfall_id=(
                    bulk_scan.canonical_grp_ids_by_scryfall_id
                ),
                missing_card_resolver=_MissingCardResolver(
                    bulk_path=scryfall_bulk_file,
                    set_rows_by_scryfall_id=bulk_scan.set_rows_by_scryfall_id,
                ),
                event_sink=lambda event: session.process_events(events=(event,)),
                socket_client=socket_client,
            )
        except Exception as error:
            raise TestDraftError(
                f"the test draft could not start: {error}",
                stage="startup",
            ) from error
        try:
            controller = TestDraftController(session=session, adapter=adapter)
        except Exception as error:
            adapter.close()
            raise TestDraftError(
                f"the test draft could not start: {error}",
                stage="startup",
            ) from error
        stack.callback(controller.close)
    except BaseException:
        stack.close()
        raise
    return TestDraftRuntime(
        session=session,
        controller=controller,
        adapter=adapter,
        profile_client=profile_client,
        simulation_app_dir=simulation_dir,
        stack=stack,
    )


def run_test_draft_auto(
    *,
    draftmancer_dir: Path,
    scryfall_bulk_file: Path,
    server_url: str,
    set_code: str,
    timeout_seconds: float,
    source_app_dir: Path | None,
    profile_manifest_url: str | None,
    profile_network_policy: ProfileNetworkPolicy,
    splash_enabled: bool = True,
    contextual_adjustments_enabled: bool = True,
    simulation_app_dir: Path | None = None,
    socket_client: object | None = None,
) -> TestDraftRunResult:
    """Run one headless automatic draft against a pinned Draftmancer server.
    Card and profile sources stay in the normal app directory; simulated draft
    state and audit records stay in an isolated simulation directory.
    """

    runtime = create_test_draft_runtime(
        draftmancer_dir=draftmancer_dir,
        scryfall_bulk_file=scryfall_bulk_file,
        server_url=server_url,
        set_code=set_code,
        timeout_seconds=timeout_seconds,
        source_app_dir=source_app_dir,
        profile_manifest_url=profile_manifest_url,
        profile_network_policy=profile_network_policy,
        splash_enabled=splash_enabled,
        contextual_adjustments_enabled=contextual_adjustments_enabled,
        simulation_app_dir=simulation_app_dir,
        socket_client=socket_client,
    )
    try:
        return runtime.controller.run_auto()
    finally:
        runtime.close()


def _offer_identity(*, event: PackOfferedEvent) -> TestDraftOfferIdentity:
    """Copy one offered pack event into the controller's readiness token."""

    return TestDraftOfferIdentity(
        account_id=event.account_id,
        event_name=event.event_name,
        set_code=event.set_code.strip().casefold(),
        pack_number=event.pack_number,
        pick_number=event.pick_number,
        offered_grp_ids=event.offered_grp_ids,
        pool_grp_ids=event.pool_grp_ids,
    )


def _normalized_set_code(*, set_code: str) -> str:
    """Normalize an external set code and reject an empty value."""

    normalized = set_code.strip().casefold()
    if not normalized:
        raise TestDraftError("set code must not be empty", stage="startup")
    return normalized


def _resolved_app_dir(*, app_dir: Path | None) -> Path:
    """Resolve one app directory the same way the shared clients do."""

    root = app_data_dir() if app_dir is None else app_dir
    return Path(root).expanduser().resolve(strict=False)


def default_test_draft_checkout_dir(*, app_dir: Path | None = None) -> Path:
    """Return the pinned Draftmancer checkout inside the application data directory."""

    return _resolved_app_dir(app_dir=app_dir) / DEFAULT_TEST_DRAFT_CHECKOUT_DIRECTORY_NAME


def default_test_draft_bulk_file(*, app_dir: Path | None = None) -> Path:
    """Return the Scryfall bulk source inside the application data directory.
    The layout mirrors the developer corpus cache without its working-directory prefix.
    """

    return (
        _resolved_app_dir(app_dir=app_dir)
        / "corpus-cache"
        / "sources"
        / SCRYFALL_DEFAULT_CARDS_BULK_FILE_NAME
    )


def _load_supported_set_codes(*, draftmancer_dir: Path) -> tuple[str, ...]:
    """Read the pinned simulator's MTGASets capability list.
    Malformed checkout metadata fails before any network connection.
    """

    constants_path = draftmancer_dir / "src" / "data" / "constants.json"
    if not constants_path.is_file():
        raise TestDraftError(
            f"missing Draftmancer constants file: {constants_path}",
            stage="startup",
        )
    try:
        payload = json.loads(constants_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TestDraftError(
            f"could not parse Draftmancer constants file {constants_path}: {error}",
            stage="startup",
        ) from error
    if not isinstance(payload, dict):
        raise TestDraftError(
            "Draftmancer constants.json must contain an object",
            stage="startup",
        )
    draftmancer_codes = payload.get("MTGASets")
    if not isinstance(draftmancer_codes, list):
        raise TestDraftError(
            "Draftmancer constants.json MTGASets must contain an array",
            stage="startup",
        )
    return tuple(draftmancer_codes)


def _load_set_names(*, draftmancer_dir: Path) -> dict[str, str]:
    """Read full set names from the pinned simulator's SetsInfos metadata.
    Entries without a usable name are skipped so callers can fall back.
    """

    sets_infos_path = draftmancer_dir / "src" / "data" / "SetsInfos.json"
    if not sets_infos_path.is_file():
        raise TestDraftError(
            f"missing Draftmancer set metadata file: {sets_infos_path}",
            stage="startup",
        )
    try:
        payload = json.loads(sets_infos_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TestDraftError(
            f"could not parse Draftmancer set metadata file {sets_infos_path}: {error}",
            stage="startup",
        ) from error
    if not isinstance(payload, dict):
        raise TestDraftError(
            "Draftmancer SetsInfos.json must contain an object",
            stage="startup",
        )
    names: dict[str, str] = {}
    for code, info in payload.items():
        if not isinstance(code, str) or not isinstance(info, dict):
            continue
        name = info.get("fullName")
        if isinstance(name, str) and name.strip():
            names[code.casefold()] = name.strip()
    return names


def _load_canonical_grp_ids_by_scryfall_id(
    *,
    bulk_path: Path,
    set_code: str,
    card_database: CardDatabase,
) -> dict[str, int]:
    """Resolve every Scryfall printing that Draft Omen can identify to an Arena grpId.
    The complete Scryfall bulk file is read locally and never queried per card.
    """

    return _scan_scryfall_bulk(
        bulk_path=bulk_path,
        set_code=set_code,
        card_database=card_database,
    ).canonical_grp_ids_by_scryfall_id


@dataclass(frozen=True)
class _ScryfallBulkScan:
    """Identity map and the draft set's own rows from one pass over the bulk file.
    The rows let the Mocked Draft describe set cards that Scryfall has no Arena id for.
    """

    canonical_grp_ids_by_scryfall_id: dict[str, int]
    set_rows_by_scryfall_id: dict[str, dict[str, Any]]


def _scan_scryfall_bulk(
    *,
    bulk_path: Path,
    set_code: str,
    card_database: CardDatabase,
) -> _ScryfallBulkScan:
    """Read the bulk file once for the identity map and the set's own printings.
    The complete Scryfall bulk file is read locally and never queried per card.
    """

    if not bulk_path.is_file():
        raise TestDraftError(
            f"missing local Scryfall bulk file: {bulk_path}",
            stage="startup",
        )
    # Draftmancer boosters also carry printings from other sets, such as
    # reprints in a bonus slot, so every set's printings are indexed.
    records_by_card_id: dict[str, tuple[str, int | None]] = {}
    set_rows_by_scryfall_id: dict[str, dict[str, Any]] = {}
    open_bulk = gzip.open if bulk_path.suffix == ".gz" else Path.open
    try:
        with open_bulk(bulk_path, mode="rt", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TestDraftError(
                        f"Scryfall bulk line {line_number} must be an object",
                        stage="startup",
                    )
                in_set = value.get("set") == set_code
                card_id = value.get("id")
                oracle_id = _scryfall_oracle_id(card=value)
                if not isinstance(card_id, str) or not card_id:
                    if not in_set:
                        continue
                    raise TestDraftError(
                        f"Scryfall {set_code} card on line {line_number} has no id",
                        stage="startup",
                    )
                if oracle_id is None:
                    if not in_set:
                        continue
                    raise TestDraftError(
                        f"Scryfall {set_code} card {card_id} has no oracle_id",
                        stage="startup",
                    )
                if in_set:
                    set_rows_by_scryfall_id[card_id] = value
                arena_id = value.get("arena_id")
                if not isinstance(arena_id, int) or isinstance(arena_id, bool):
                    arena_id = None
                record = (oracle_id, arena_id)
                previous = records_by_card_id.get(card_id)
                if previous is not None and previous != record:
                    raise TestDraftError(
                        f"Scryfall card {card_id} has conflicting bulk records",
                        stage="startup",
                    )
                records_by_card_id[card_id] = record
    except TestDraftError:
        raise
    except (
        OSError,
        EOFError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        zlib.error,
    ) as error:
        raise TestDraftError(
            f"could not parse local Scryfall bulk file {bulk_path}: {error}",
            stage="startup",
        ) from error
    if not set_rows_by_scryfall_id:
        raise TestDraftError(
            f"local Scryfall bulk file {bulk_path} contains no {set_code} cards",
            stage="startup",
        )

    canonical_ids = set(card_database.cards)
    set_arena_ids_by_oracle: dict[str, set[int]] = {}
    any_arena_ids_by_oracle: dict[str, set[int]] = {}
    for oracle_id, arena_id in records_by_card_id.values():
        if arena_id is None:
            continue
        any_arena_ids_by_oracle.setdefault(oracle_id, set()).add(arena_id)
        if arena_id in canonical_ids:
            set_arena_ids_by_oracle.setdefault(oracle_id, set()).add(arena_id)

    # Arena printings of one card, such as alternate-art basics, play
    # identically, so the lowest grpId keeps each choice deterministic. The
    # set's own printings win, so its ratings apply; other Arena printings
    # cover bonus-sheet cards that the set card data does not list.
    identities: dict[str, int] = {}
    for card_id, (oracle_id, arena_id) in records_by_card_id.items():
        if arena_id is not None and arena_id in canonical_ids:
            identities[card_id] = arena_id
        elif oracle_id in set_arena_ids_by_oracle:
            identities[card_id] = min(set_arena_ids_by_oracle[oracle_id])
        elif arena_id is not None:
            identities[card_id] = arena_id
        elif oracle_id in any_arena_ids_by_oracle:
            identities[card_id] = min(any_arena_ids_by_oracle[oracle_id])
    return _ScryfallBulkScan(
        canonical_grp_ids_by_scryfall_id=identities,
        set_rows_by_scryfall_id=set_rows_by_scryfall_id,
    )


class _MissingCardResolver:
    """Describe Mocked Draft booster cards that the set card data does not list.
    Cards come from the local Scryfall bulk file and carry no ratings.
    """

    def __init__(
        self,
        *,
        bulk_path: Path,
        set_rows_by_scryfall_id: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._bulk_path = bulk_path
        self._set_rows_by_scryfall_id = set_rows_by_scryfall_id
        self._arena_cards: CardDatabase | None = None
        self._lock = Lock()

    def __call__(self, *, grp_id: int, scryfall_id: str | None) -> CardInfo | None:
        """Return card metadata for an Arena grpId the set card data lacks.
        Scryfall Arena printings come first, then the set's own row for Draftmancer-only ids.
        """

        card = self._arena_card_database().cards.get(grp_id)
        if card is not None:
            return card
        row = (
            None
            if scryfall_id is None
            else self._set_rows_by_scryfall_id.get(scryfall_id)
        )
        if row is None:
            return None
        # Scryfall has no arena_id for some set printings, such as KTK, so
        # Draftmancer's Arena id stands in for it.
        database = build_card_database_from_scryfall_cards(
            cards=({**row, "arena_id": grp_id},),
        )
        return database.cards.get(grp_id)

    def _arena_card_database(self) -> CardDatabase:
        # Building every Arena card takes a few seconds, so it waits for the
        # first missing card and most drafts never pay for it.
        with self._lock:
            if self._arena_cards is None:
                self._arena_cards = build_card_database_from_bulk_file(
                    path=self._bulk_path,
                )
            return self._arena_cards


def _scryfall_oracle_id(*, card: Mapping[str, Any]) -> str | None:
    """Return the card's oracle id, reading the first face when the top level has none.
    Reversible layouts keep oracle_id on each face instead of the card object.
    """

    oracle_id = card.get("oracle_id")
    if isinstance(oracle_id, str) and oracle_id:
        return oracle_id
    faces = card.get("card_faces")
    if isinstance(faces, list) and faces and isinstance(faces[0], Mapping):
        face_oracle_id = faces[0].get("oracle_id")
        if isinstance(face_oracle_id, str) and face_oracle_id:
            return face_oracle_id
    return None
