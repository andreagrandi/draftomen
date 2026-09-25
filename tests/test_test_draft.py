"""Drive the shared test-draft controller through deterministic fake-socket drafts.
Every assertion observes the controller's published Python contract, not plumbing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
import gzip
import json
from pathlib import Path
import tempfile
import threading
import zlib

import pytest
from draftomen.augmented_model_client import AugmentedModelClient
from draftomen.audit import DraftAuditStore, load_draft_audit_records
from draftomen.card_data_client import card_data_cache_path
from draftomen.cardimages import CardImageService
from draftomen.deckbuilder import DeckBuilderError
from draftomen.draftmancer import (
    DraftmancerAdapter,
    DraftmancerAdapterError,
    DraftmancerConfig,
)
from draftomen.events import PackOfferedEvent
from draftomen.paths import app_data_dir
from draftomen.pool import (
    DraftPick,
    DraftState,
    list_draft_states,
    save_draft_state,
)
from draftomen.profile_client import ProfileNetworkPolicy
from draftomen.set_card_data import SetCardData
from draftomen.set_profile import (
    dump_set_profile,
    load_set_profile,
    set_profile_path,
)
from draftomen.seventeen import QUICK_DRAFT_FORMAT
from draftomen.session import (
    ApplicationPhase,
    AugmentationStatus,
    CardImageFetchResult,
    CardImageRequest,
    ChangeAugmentation,
    ChooseRecommendation,
    ContextualEvidenceStatus,
    DataLoadPhase,
    LiveSession,
    LiveSessionCommand,
    LiveSessionSnapshot,
    OperationKind,
    RequestBuild,
    SessionError,
    SnapshotPublisher,
)
from draftomen.test_draft import (
    SIMULATION_DIRECTORY_PREFIX,
    TestDraftController,
    TestDraftError,
    TestDraftInspection,
    TestDraftOfferIdentity,
    TestDraftRunResult,
    TestDraftRuntime,
    TestDraftSet,
    create_test_draft_runtime,
    default_test_draft_bulk_file,
    default_test_draft_checkout_dir,
    run_test_draft_auto,
    supported_test_draft_set_codes,
    supported_test_draft_sets,
    _MissingCardResolver,
    _scan_scryfall_bulk,
)

from tests.augmented_artifacts import fixed_delta_artifact
from tests.test_draftmancer import (
    _CANCELLATION_TEXT,
    _FakeSocket,
    _config,
    _database,
    _query,
    _start_action,
    _start_worker,
    _state,
    _withheld_ack_action,
)

_CONTROLLER_EVENT_NAME = "QuickDraft_HOB_Draftmancer_session-1"
_CONTROLLER_ACCOUNT_ID = "developer-1"
_HELPER_SET_CODE = "hob"
_HELPER_SET_NAME = "Fixture Set"
_HELPER_GRP_IDS = (100, 101, 102, 103)
_HELPER_OFFERS = ((100, 101, 102, 103), (101, 102, 103), (102, 103), (103,))
_HELPER_COORDINATES = ((0, 0), (0, 1), (1, 0), (2, 0))


class _RejectingBuildSession(LiveSession):
    """Reject one deck build while keeping production event ingestion intact.
    The controller's own build stage is proven by overriding dispatch, not by
    monkeypatching the module-level RequestBuild command.
    """

    def dispatch(self, *, command: LiveSessionCommand) -> LiveSessionSnapshot:
        if isinstance(command, RequestBuild):
            raise DeckBuilderError("deck builder refused the drafted pool")
        return super().dispatch(command=command)


class _BuildFailedSnapshotSession(LiveSession):
    """Publish the normal build_failed session error instead of a build result.
    The documented published-error path is proven without breaking the builder.
    """

    def dispatch(self, *, command: LiveSessionCommand) -> LiveSessionSnapshot:
        snapshot = super().dispatch(command=command)
        if not isinstance(command, RequestBuild):
            return snapshot
        return replace(
            snapshot,
            build=None,
            progress=None,
            errors=(
                SessionError(
                    error_id="build",
                    code="build_failed",
                    message=(
                        "Deck build failed: drafted pool is structurally infeasible"
                    ),
                    recoverable=True,
                    operation=OperationKind.BUILD,
                ),
            ),
        )


class _ScriptedSnapshotSession(LiveSession):
    """Publish one crafted snapshot so readiness guards are observed directly.
    Production ingestion still owns the draft; only the immutable snapshot that
    frontends read is replaced, so every refusal is the controller's own.
    """

    def __init__(self, *, app_dir: Path, grp_ids: tuple[int, ...]) -> None:
        super().__init__(
            log_path=None,
            app_dir=app_dir,
            card_database=_database(*grp_ids),
        )
        self.scripted_snapshot: LiveSessionSnapshot | None = None

    @property
    def snapshot(self) -> LiveSessionSnapshot:
        if self.scripted_snapshot is None:
            return super().snapshot
        return self.scripted_snapshot


def _pick_actions(
    *,
    states: tuple[dict[str, object], ...],
) -> list[Callable[[_FakeSocket], object]]:
    """Emit each later draftState from the matching pick ack and end after it."""

    def pick_action(index: int) -> Callable[[_FakeSocket], object]:
        def action(socket: _FakeSocket) -> object:
            if index == len(states) - 1:
                socket.handlers["endDraft"]()
            else:
                socket.server_emit("draftState", states[index + 1])
            return {"code": 0}

        return action

    return [pick_action(index) for index in range(len(states))]


def _scripted_socket(
    *,
    config: DraftmancerConfig,
    states: tuple[dict[str, object], ...],
) -> _FakeSocket:
    """Serve one scripted multi-pack draft from the configured seat."""

    return _FakeSocket(
        start_action=_start_action(config, states[0]),
        pick_actions=_pick_actions(states=states),
    )


def _rejecting_pick_socket(
    *,
    states: tuple[dict[str, object], ...],
) -> _FakeSocket:
    """Accept every pick but the last, whose acknowledgement the simulator rejects."""

    def reject(socket: _FakeSocket) -> object:
        return {
            "code": -1,
            "error": {"title": "Pick rejected", "text": "already picked"},
        }

    return _FakeSocket(
        start_action=_start_action(_config(), states[0]),
        pick_actions=[*_pick_actions(states=states)[:-1], reject],
    )


def _draft_session(*, app_dir: Path, grp_ids: tuple[int, ...]) -> LiveSession:
    """Build the simulated session the controller owns for one draft."""

    return LiveSession(
        log_path=None,
        app_dir=app_dir,
        card_database=_database(*grp_ids),
    )


def _controller(
    *,
    socket: _FakeSocket,
    session: LiveSession,
    grp_ids: tuple[int, ...],
    config: DraftmancerConfig | None = None,
) -> TestDraftController:
    """Wire one adapter's events straight into the controller's session."""

    adapter = DraftmancerAdapter(
        config=_config() if config is None else config,
        card_database=_database(*grp_ids),
        event_sink=lambda event: session.process_events(events=(event,)),
        socket_client=socket,
    )
    return TestDraftController(session=session, adapter=adapter)


def _pick_card_calls(socket: _FakeSocket) -> list[object]:
    """Return every pickCard payload the simulator actually received."""

    return [call.data for call in socket.calls if call.event == "pickCard"]


def _rank_one(inspection: TestDraftInspection) -> int:
    """Return the top production recommendation of one inspected pack."""

    return inspection.snapshot.recommendations.cards[0].card.grp_id


def _print_state(
    *,
    pack_number: int,
    pick_number: int,
    grp_ids: tuple[int, ...],
) -> dict[str, object]:
    """Emit one draftState keyed by Scryfall print ids, as the simulator does."""

    return {
        "boosterNumber": pack_number,
        "pickNumber": pick_number,
        "booster": [
            {
                "uniqueID": pack_number * 100 + pick_number * 10 + index + 1,
                "id": f"print-{grp_id}",
            }
            for index, grp_id in enumerate(grp_ids)
        ],
    }


def _helper_socket(
    *,
    states: tuple[dict[str, object], ...],
    pick_actions: Iterable[Callable[[_FakeSocket], object]] | None = None,
) -> _FakeSocket:
    """Start from the helper's own generated seat, known only to the connect query."""

    def start_action(socket: _FakeSocket) -> object:
        query = _query(socket)
        user_id = query["userID"][0]
        socket.server_emit(
            "startDraft",
            {
                user_id: {
                    "userID": user_id,
                    "userName": query["userName"][0],
                    "isBot": False,
                },
                **{
                    f"bot-{index}": {
                        "userID": f"bot-{index}",
                        "userName": f"Bot {index}",
                        "isBot": True,
                    }
                    for index in range(7)
                },
            },
        )
        socket.server_emit("draftState", states[0])
        return {"code": 0}

    return _FakeSocket(
        start_action=start_action,
        pick_actions=(
            _pick_actions(states=states) if pick_actions is None else pick_actions
        ),
    )


def _tree_entries(*, root: Path) -> dict[Path, bytes | None]:
    """Record every path below one root with its exact file bytes."""

    return {
        path.relative_to(root): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _assert_tree_unchanged(*, root: Path, recorded: dict[Path, bytes | None]) -> None:
    """Require every recorded path to keep its exact kind and bytes."""

    for relative, payload in recorded.items():
        path = root / relative
        assert path.exists(), f"{relative} disappeared from {root}"
        if payload is None:
            assert path.is_dir(), f"{relative} is no longer a directory"
            continue
        assert path.read_bytes() == payload, f"{relative} changed bytes in {root}"
    assert _tree_entries(root=root) == recorded, f"{root} gained or lost paths"


@dataclass(frozen=True, slots=True)
class _HelperSources:
    """Collect the isolated directories one headless helper run needs."""

    normal_app_dir: Path
    simulation_dir: Path
    draftmancer_dir: Path
    bulk_path: Path


def _seed_helper_sources(*, tmp_path: Path) -> _HelperSources:
    """Seed card data, Arena history, a checkout, and a bulk file for one run."""

    normal_app_dir = tmp_path / "normal-app"
    artifact = SetCardData.from_card_database(
        _database(*_HELPER_GRP_IDS),
        set_code=_HELPER_SET_CODE,
        set_name=_HELPER_SET_NAME,
    )
    cache_path = card_data_cache_path(
        set_code=_HELPER_SET_CODE,
        app_dir=normal_app_dir,
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(artifact.to_gzip_bytes())
    arena_state = DraftState(
        account_id="arena-account",
        draft_id="arena-draft",
        event_name="QuickDraft_HOB_Arena_arena-draft",
        set_code=_HELPER_SET_CODE,
        course_id=None,
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        completed_at=None,
        completed=False,
        picks=(
            DraftPick(
                pack_number=0,
                pick_number=0,
                offered_grp_ids=(100, 101),
                pool_before_pick=(),
                chosen_grp_id=100,
            ),
        ),
        pool_grp_ids=(100,),
    )
    save_draft_state(state=arena_state, app_dir=normal_app_dir)
    DraftAuditStore(app_dir=normal_app_dir).record_draft_started(state=arena_state)
    draftmancer_dir = tmp_path / "Draftmancer"
    constants_path = draftmancer_dir / "src" / "data" / "constants.json"
    constants_path.parent.mkdir(parents=True, exist_ok=True)
    constants_path.write_text(
        json.dumps({"MTGASets": [_HELPER_SET_CODE.upper()]}),
        encoding="utf-8",
    )
    bulk_path = tmp_path / "scryfall-default-cards.jsonl"
    bulk_path.write_text(
        "".join(
            json.dumps(
                {
                    "set": _HELPER_SET_CODE,
                    "id": f"print-{grp_id}",
                    "oracle_id": f"oracle-{grp_id}",
                    "arena_id": grp_id,
                }
            )
            + "\n"
            for grp_id in _HELPER_GRP_IDS
        ),
        encoding="utf-8",
    )
    return _HelperSources(
        normal_app_dir=normal_app_dir,
        simulation_dir=tmp_path / "simulation",
        draftmancer_dir=draftmancer_dir,
        bulk_path=bulk_path,
    )


def _run_helper(
    *,
    sources: _HelperSources,
    socket: _FakeSocket,
    contextual_adjustments_enabled: bool = True,
) -> TestDraftRunResult:
    """Run one headless automatic draft against the seeded isolated sources."""

    return run_test_draft_auto(
        draftmancer_dir=sources.draftmancer_dir,
        scryfall_bulk_file=sources.bulk_path,
        server_url="http://127.0.0.1:3000",
        set_code=_HELPER_SET_CODE,
        timeout_seconds=1.0,
        source_app_dir=sources.normal_app_dir,
        profile_manifest_url=None,
        profile_network_policy=ProfileNetworkPolicy.OFFLINE,
        contextual_adjustments_enabled=contextual_adjustments_enabled,
        simulation_app_dir=sources.simulation_dir,
        socket_client=socket,
    )


def _create_runtime(
    *,
    sources: _HelperSources,
    socket: _FakeSocket,
    timeout_seconds: float = 1.0,
    snapshot_publisher: SnapshotPublisher | None = None,
    simulation_app_dir: Path | None = None,
    card_image_service: CardImageService | None = None,
) -> TestDraftRuntime:
    """Create one isolated runtime over the seeded card, profile, and bulk sources."""

    return create_test_draft_runtime(
        draftmancer_dir=sources.draftmancer_dir,
        scryfall_bulk_file=sources.bulk_path,
        server_url="http://127.0.0.1:3000",
        set_code=_HELPER_SET_CODE,
        timeout_seconds=timeout_seconds,
        source_app_dir=sources.normal_app_dir,
        profile_manifest_url=None,
        profile_network_policy=ProfileNetworkPolicy.OFFLINE,
        snapshot_publisher=snapshot_publisher,
        simulation_app_dir=simulation_app_dir,
        socket_client=socket,
        card_image_service=card_image_service,
    )


def _draftmancer_checkout(*, root: Path, mtga_sets: object) -> Path:
    """Write one pinned checkout advertising the given raw MTGASets capability."""

    constants_path = root / "src" / "data" / "constants.json"
    constants_path.parent.mkdir(parents=True, exist_ok=True)
    constants_path.write_text(
        json.dumps({"MTGASets": mtga_sets}),
        encoding="utf-8",
    )
    return root


def _arena_states() -> tuple[dict[str, object], ...]:
    """Return the scripted four-pick draft keyed by Arena grpIds."""

    return tuple(
        _state(
            pack_number=pack_number,
            pick_number=pick_number,
            arena_ids=grp_ids,
        )
        for (pack_number, pick_number), grp_ids in zip(
            _HELPER_COORDINATES,
            _HELPER_OFFERS,
            strict=True,
        )
    )


def _print_states() -> tuple[dict[str, object], ...]:
    """Return the scripted four-pick draft keyed by Scryfall print ids."""

    return tuple(
        _print_state(
            pack_number=pack_number,
            pick_number=pick_number,
            grp_ids=grp_ids,
        )
        for (pack_number, pick_number), grp_ids in zip(
            _HELPER_COORDINATES,
            _HELPER_OFFERS,
            strict=True,
        )
    )


def test_start_returns_first_ready_inspection_without_picking(tmp_path: Path) -> None:
    config = _config()
    socket = _scripted_socket(
        config=config,
        states=(_state(pack_number=0, pick_number=0, arena_ids=(100, 101)),),
    )
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    inspection = controller.start()

    assert inspection.offer == TestDraftOfferIdentity(
        account_id=_CONTROLLER_ACCOUNT_ID,
        event_name=_CONTROLLER_EVENT_NAME,
        set_code="hob",
        pack_number=0,
        pick_number=0,
        offered_grp_ids=(100, 101),
        pool_grp_ids=(),
    )
    rows = inspection.snapshot.recommendations.cards
    assert tuple(row.rank for row in rows) == (1, 2)
    assert {row.card.grp_id for row in rows} == {100, 101}
    assert inspection.snapshot.current_pack_event is not None
    assert inspection.snapshot.pool.total_cards == 0
    assert _pick_card_calls(socket) == []
    controller.close()


def test_manual_confirm_accepts_non_top_recommendation(tmp_path: Path) -> None:
    socket = _scripted_socket(
        config=_config(),
        states=(
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 101, 102),
                unique_ids=(11, 12, 13),
            ),
            _state(pack_number=0, pick_number=1, arena_ids=(101,), unique_ids=(14,)),
        ),
    )
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100, 101, 102))
    controller = _controller(
        socket=socket,
        session=session,
        grp_ids=(100, 101, 102),
    )

    inspection = controller.start()
    chosen = inspection.snapshot.recommendations.cards[-1].card.grp_id

    assert chosen != _rank_one(inspection)

    step = controller.confirm(grp_id=chosen, expected_offer=inspection.offer)

    booster_index = (100, 101, 102).index(chosen)
    assert step.grp_id == chosen
    assert step.unique_card_id == (11, 12, 13)[booster_index]
    assert _pick_card_calls(socket) == [
        {"pickedCards": [booster_index], "burnedCards": []}
    ]
    assert step.before.snapshot is inspection.snapshot
    assert step.after.current_pack_event is not None
    assert step.after.current_pack_event.pool_grp_ids == (chosen,)
    controller.close()


def test_auto_confirms_rank_one_over_published_selection(tmp_path: Path) -> None:
    socket = _scripted_socket(
        config=_config(),
        states=(
            _state(pack_number=0, pick_number=0, arena_ids=(100, 101, 102)),
            _state(pack_number=0, pick_number=1, arena_ids=(101,)),
        ),
    )
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100, 101, 102))
    controller = _controller(
        socket=socket,
        session=session,
        grp_ids=(100, 101, 102),
    )

    inspection = controller.start()
    expected = _rank_one(inspection)
    other = inspection.snapshot.recommendations.cards[-1].card.grp_id
    assert other != expected

    selection = session.dispatch(command=ChooseRecommendation(grp_id=other))
    assert selection.recommendations.selected_grp_id == other

    step = controller.advance_auto()

    assert step.grp_id == expected
    assert step.grp_id != selection.recommendations.selected_grp_id
    assert len(_pick_card_calls(socket)) == 1
    controller.close()


def test_inspect_refuses_completed_draft_without_picking_again(tmp_path: Path) -> None:
    # An offered pack always scores at least one recommendation, so the
    # deterministic readiness refusal without pack state is post-completion.
    socket = _scripted_socket(
        config=_config(),
        states=(_state(pack_number=0, pick_number=0, arena_ids=(100,)),),
    )
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100,))
    controller = _controller(socket=socket, session=session, grp_ids=(100,))

    inspection = controller.start()
    step = controller.confirm(
        grp_id=_rank_one(inspection),
        expected_offer=inspection.offer,
    )

    assert step.after.status.phase is ApplicationPhase.DRAFT_COMPLETE

    with pytest.raises(TestDraftError, match="not drafting") as error:
        controller.inspect()

    assert error.value.stage == "readiness"
    assert error.value.steps == (step,)
    assert error.value.snapshot.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert len(_pick_card_calls(socket)) == 1
    controller.close()


def test_inspect_refuses_unready_pack_state_without_submitting(tmp_path: Path) -> None:
    socket = _scripted_socket(
        config=_config(),
        states=(
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 101),
                unique_ids=(11, 12),
            ),
            _state(pack_number=0, pick_number=1, arena_ids=(101,), unique_ids=(13,)),
        ),
    )
    session = _ScriptedSnapshotSession(app_dir=tmp_path / "app", grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    inspection = controller.start()
    offered = inspection.snapshot.current_pack_event
    assert offered is not None

    # A drafting pack without scored recommendations pins the
    # "the offered pack has no recommendations" readiness guard.
    session.scripted_snapshot = replace(
        inspection.snapshot,
        recommendations=replace(inspection.snapshot.recommendations, cards=()),
    )

    with pytest.raises(TestDraftError, match="no recommendations") as unscored:
        controller.inspect()

    assert unscored.value.stage == "readiness"
    assert unscored.value.steps == ()

    # One extra offered id without a matching simulator instance pins the
    # "the simulator offer does not map to the offered pack" readiness guard.
    session.scripted_snapshot = replace(
        inspection.snapshot,
        current_pack_event=replace(
            offered,
            offered_grp_ids=offered.offered_grp_ids + (102,),
        ),
    )

    with pytest.raises(TestDraftError, match="does not map") as unmatched:
        controller.inspect()

    assert unmatched.value.stage == "readiness"
    assert unmatched.value.steps == ()
    assert _pick_card_calls(socket) == []

    # The same first-pack snapshot after one accepted pick pins the
    # "the offered pack pool does not match the accepted picks" guard, so a
    # superseded pack can never look ready again.
    session.scripted_snapshot = None
    step = controller.confirm(
        grp_id=_rank_one(inspection),
        expected_offer=inspection.offer,
    )
    session.scripted_snapshot = inspection.snapshot

    with pytest.raises(TestDraftError, match="accepted picks") as superseded:
        controller.inspect()

    assert superseded.value.stage == "readiness"
    assert superseded.value.steps == (step,)
    assert len(_pick_card_calls(socket)) == 1
    controller.close()


def test_confirm_refuses_forged_and_superseded_tokens_without_submitting(
    tmp_path: Path,
) -> None:
    socket = _scripted_socket(
        config=_config(),
        states=(
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 101),
                unique_ids=(11, 12),
            ),
            _state(pack_number=0, pick_number=1, arena_ids=(101,), unique_ids=(13,)),
        ),
    )
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    inspection = controller.start()
    for forged in (
        replace(inspection.offer, pick_number=inspection.offer.pick_number + 1),
        replace(inspection.offer, pool_grp_ids=(999,)),
    ):
        with pytest.raises(
            TestDraftError,
            match="not the current offered pack",
        ) as forged_error:
            controller.confirm(grp_id=_rank_one(inspection), expected_offer=forged)
        assert forged_error.value.stage == "readiness"
        assert forged_error.value.steps == ()
    assert _pick_card_calls(socket) == []

    step = controller.confirm(
        grp_id=_rank_one(inspection),
        expected_offer=inspection.offer,
    )

    with pytest.raises(
        TestDraftError,
        match="not the current offered pack",
    ) as stale_error:
        controller.confirm(grp_id=step.grp_id, expected_offer=inspection.offer)

    assert stale_error.value.stage == "readiness"
    assert stale_error.value.steps == (step,)
    assert len(_pick_card_calls(socket)) == 1
    controller.close()


def test_repeated_confirmation_submits_and_records_one_pick(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    socket = _scripted_socket(
        config=_config(),
        states=(
            _state(pack_number=0, pick_number=0, arena_ids=(100, 101)),
            _state(pack_number=0, pick_number=1, arena_ids=(101,)),
        ),
    )
    session = _draft_session(app_dir=app_dir, grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    inspection = controller.start()
    chosen = _rank_one(inspection)
    step = controller.confirm(grp_id=chosen, expected_offer=inspection.offer)

    with pytest.raises(TestDraftError) as repeat:
        controller.confirm(grp_id=chosen, expected_offer=inspection.offer)

    assert repeat.value.stage == "readiness"
    assert repeat.value.steps == (step,)
    assert _pick_card_calls(socket) == [
        {"pickedCards": [(100, 101).index(chosen)], "burnedCards": []}
    ]

    states = list_draft_states(app_dir=app_dir)
    assert len(states) == 1
    assert states[0].chosen_pick_count == 1
    assert states[0].pool_grp_ids == (chosen,)
    assert states[0].completed is False
    choices = tuple(
        record
        for record in load_draft_audit_records(
            account_id=states[0].account_id,
            draft_id=states[0].draft_id,
            app_dir=app_dir,
        )
        if record["record_type"] == "choice_made"
    )
    assert [record["chosen_grp_id"] for record in choices] == [chosen]
    controller.close()


def test_confirm_refuses_repeat_after_ambiguous_pick_without_resubmitting(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    socket = _rejecting_pick_socket(
        states=(
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 101),
                unique_ids=(11, 12),
            ),
        ),
    )
    session = _draft_session(app_dir=app_dir, grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    inspection = controller.start()
    chosen = _rank_one(inspection)

    # A rejected acknowledgement is reported as a drafting failure, and the
    # offer identity stays consumed so the caller cannot retry it blindly.
    with pytest.raises(TestDraftError, match="did not accept the pick") as rejected:
        controller.confirm(grp_id=chosen, expected_offer=inspection.offer)

    assert rejected.value.stage == "drafting"
    assert rejected.value.steps == ()
    assert isinstance(rejected.value.__cause__, DraftmancerAdapterError)

    with pytest.raises(TestDraftError, match="already confirmed") as repeated:
        controller.confirm(grp_id=chosen, expected_offer=inspection.offer)

    assert repeated.value.stage == "readiness"
    assert repeated.value.steps == ()
    assert _pick_card_calls(socket) == [
        {"pickedCards": [(100, 101).index(chosen)], "burnedCards": []}
    ]

    states = list_draft_states(app_dir=app_dir)
    assert [state.chosen_pick_count for state in states] == [0]
    choices = tuple(
        record
        for record in load_draft_audit_records(
            account_id=states[0].account_id,
            draft_id=states[0].draft_id,
            app_dir=app_dir,
        )
        if record["record_type"] == "choice_made"
    )
    assert choices == ()
    controller.close()


def test_advance_auto_carries_accepted_picks_into_the_production_pool(
    tmp_path: Path,
) -> None:
    socket = _scripted_socket(
        config=_config(),
        states=(
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 101),
                unique_ids=(11, 12),
            ),
            _state(
                pack_number=0,
                pick_number=1,
                arena_ids=(101, 102),
                unique_ids=(13, 14),
            ),
            _state(pack_number=1, pick_number=0, arena_ids=(200,), unique_ids=(15,)),
            _state(pack_number=2, pick_number=0, arena_ids=(300,), unique_ids=(16,)),
        ),
    )
    session = _draft_session(
        app_dir=tmp_path / "app",
        grp_ids=(100, 101, 102, 200, 300),
    )
    controller = _controller(
        socket=socket,
        session=session,
        grp_ids=(100, 101, 102, 200, 300),
    )

    controller.start()
    steps = tuple(controller.advance_auto() for _ in range(4))

    for index, step in enumerate(steps):
        assert step.after.pool.total_cards == index + 1

    for step in steps[:-1]:
        next_pack = step.after.current_pack_event
        assert step.after.status.phase is ApplicationPhase.DRAFTING
        assert next_pack is not None
        assert next_pack.pool_grp_ids == step.before.offer.pool_grp_ids + (step.grp_id,)

    completed = steps[-1].after
    assert completed.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert completed.current_pack_event is None
    assert completed.pool.total_cards == 4
    drafted = Counter(
        card.card.grp_id
        for card in completed.pool.cards
        for _ in range(card.quantity)
    )
    assert drafted == Counter(step.grp_id for step in steps)
    controller.close()


def test_duplicate_copies_confirm_the_first_matching_instance(tmp_path: Path) -> None:
    socket = _scripted_socket(
        config=_config(),
        states=(
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 100),
                unique_ids=(11, 12),
            ),
            _state(pack_number=0, pick_number=1, arena_ids=(101,), unique_ids=(13,)),
        ),
    )
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    inspection = controller.start()
    assert inspection.offer.offered_grp_ids == (100, 100)

    step = controller.confirm(grp_id=100, expected_offer=inspection.offer)

    assert step.unique_card_id == 11
    assert _pick_card_calls(socket) == [{"pickedCards": [0], "burnedCards": []}]
    assert step.after.current_pack_event is not None
    assert step.after.current_pack_event.pool_grp_ids == (100,)
    controller.close()


def test_auto_run_exposes_ordered_inspections_and_the_normal_build(
    tmp_path: Path,
) -> None:
    socket = _scripted_socket(config=_config(), states=_arena_states())
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=_HELPER_GRP_IDS)
    controller = _controller(
        socket=socket,
        session=session,
        grp_ids=_HELPER_GRP_IDS,
    )

    result = controller.run_auto()

    assert isinstance(result, TestDraftRunResult)
    assert result.completed.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert len(result.steps) == len(_HELPER_OFFERS)

    accepted: tuple[int, ...] = ()
    for (pack_number, pick_number), offered_grp_ids, step in zip(
        _HELPER_COORDINATES,
        _HELPER_OFFERS,
        result.steps,
        strict=True,
    ):
        before = step.before.snapshot
        assert before.current_pack_event == PackOfferedEvent(
            event_name=_CONTROLLER_EVENT_NAME,
            set_code="HOB",
            pack_number=pack_number,
            pick_number=pick_number,
            offered_grp_ids=offered_grp_ids,
            pool_grp_ids=accepted,
            account_id=_CONTROLLER_ACCOUNT_ID,
            picks_per_pack=len(offered_grp_ids) + pick_number,
        )
        rows = before.recommendations.cards
        assert tuple(row.rank for row in rows) == tuple(range(1, len(rows) + 1))
        assert {row.card.grp_id for row in rows} == set(offered_grp_ids)
        assert rows[0].card.grp_id == step.grp_id
        assert before.pool.total_cards == len(accepted)
        accepted += (step.grp_id,)
        if len(accepted) < len(_HELPER_OFFERS):
            assert step.after.status.phase is ApplicationPhase.DRAFTING
            assert step.after.current_pack_event is not None
            assert step.after.current_pack_event.pool_grp_ids == accepted
        else:
            assert step.after.status.phase is ApplicationPhase.DRAFT_COMPLETE

    build = result.build.build
    assert build is not None
    assert build.domain_pool is not None
    assert build.domain_pool.pool_grp_ids == tuple(step.grp_id for step in result.steps)
    assert build.domain_pool.account_id == _CONTROLLER_ACCOUNT_ID
    assert build.domain_selection is not None
    assert build.domain_spell_selection is not None
    assert build.domain_mana_base is not None
    assert len(_pick_card_calls(socket)) == len(result.steps)
    controller.close()


@pytest.mark.parametrize("pack_size", (13, 15))
def test_auto_run_completes_a_full_draft_with_non_arena_pack_sizes(
    tmp_path: Path,
    pack_size: int,
) -> None:
    grp_ids = tuple(range(1000, 1000 + pack_size))
    states = tuple(
        _state(
            pack_number=pack_number,
            pick_number=pick_number,
            arena_ids=grp_ids[pick_number:],
        )
        for pack_number in range(3)
        for pick_number in range(pack_size)
    )
    socket = _scripted_socket(config=_config(), states=states)
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=grp_ids)
    controller = _controller(socket=socket, session=session, grp_ids=grp_ids)

    result = controller.run_auto()

    assert len(result.steps) == 3 * pack_size
    assert result.completed.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert {
        step.before.snapshot.current_pack_event.picks_per_pack  # type: ignore[union-attr]
        for step in result.steps
    } == {pack_size}
    last_event = result.steps[-1].before.snapshot.current_pack_event
    assert last_event is not None
    assert (last_event.pack_number, last_event.pick_number) == (2, pack_size - 1)
    build = result.build.build
    assert build is not None
    assert build.domain_pool is not None
    assert len(build.domain_pool.pool_grp_ids) == 3 * pack_size
    controller.close()


def test_start_failure_keeps_stage_and_original_cause(tmp_path: Path) -> None:
    socket = _FakeSocket(connect_error=OSError("connection refused"))
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100,))
    controller = _controller(socket=socket, session=session, grp_ids=(100,))

    with pytest.raises(TestDraftError) as error:
        controller.start()

    assert error.value.stage == "startup"
    assert error.value.steps == ()
    assert error.value.snapshot.status.phase is ApplicationPhase.WAITING_FOR_DRAFT
    assert isinstance(error.value.__cause__, DraftmancerAdapterError)
    assert _pick_card_calls(socket) == []
    controller.close()


def test_drafting_failure_keeps_accepted_steps_and_original_cause(
    tmp_path: Path,
) -> None:
    socket = _FakeSocket(
        start_action=_start_action(
            _config(),
            _state(pack_number=0, pick_number=0, arena_ids=(100, 101)),
        ),
        pick_actions=[
            lambda fake: {
                "code": -1,
                "error": {"title": "Pick rejected", "text": "already picked"},
            }
        ],
    )
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    inspection = controller.start()

    with pytest.raises(TestDraftError, match="did not accept the pick") as error:
        controller.confirm(
            grp_id=_rank_one(inspection),
            expected_offer=inspection.offer,
        )

    assert error.value.stage == "drafting"
    assert error.value.steps == ()
    assert error.value.snapshot.status.phase is ApplicationPhase.DRAFTING
    assert isinstance(error.value.__cause__, DraftmancerAdapterError)
    assert len(_pick_card_calls(socket)) == 1
    controller.close()


def test_drafting_failure_preserves_earlier_accepted_steps(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    socket = _rejecting_pick_socket(
        states=(
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 101),
                unique_ids=(11, 12),
            ),
            _state(pack_number=0, pick_number=1, arena_ids=(101,), unique_ids=(13,)),
        ),
    )
    session = _draft_session(app_dir=app_dir, grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    controller.start()
    first = controller.advance_auto()

    # The rejected second pick is neither submitted twice nor recorded, and the
    # failure keeps the accepted step and the pool that the pick produced.
    with pytest.raises(TestDraftError, match="did not accept the pick") as error:
        controller.advance_auto()

    assert error.value.stage == "drafting"
    assert [step.grp_id for step in error.value.steps] == [first.grp_id]
    assert error.value.snapshot.pool.total_cards == 1
    assert len(_pick_card_calls(socket)) == 2

    states = list_draft_states(app_dir=app_dir)
    assert [state.chosen_pick_count for state in states] == [1]
    assert states[0].pool_grp_ids == (first.grp_id,)
    controller.close()


def test_unchanged_next_offer_coordinates_fail_the_ordering_check(
    tmp_path: Path,
) -> None:
    def repeat_picked_pack(socket: _FakeSocket) -> object:
        # The simulator re-sends the picked pack's coordinates in a fresh
        # envelope, which the adapter rejects as an out-of-order draftState.
        socket.server_emit(
            "draftState",
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 101),
                unique_ids=(21, 22),
            ),
        )
        return {"code": 0}

    socket = _FakeSocket(
        start_action=_start_action(
            _config(),
            _state(
                pack_number=0,
                pick_number=0,
                arena_ids=(100, 101),
                unique_ids=(11, 12),
            ),
        ),
        pick_actions=[repeat_picked_pack],
    )
    session = _draft_session(app_dir=tmp_path / "app", grp_ids=(100, 101))
    controller = _controller(socket=socket, session=session, grp_ids=(100, 101))

    inspection = controller.start()

    with pytest.raises(TestDraftError, match="did not accept the pick") as error:
        controller.confirm(
            grp_id=_rank_one(inspection),
            expected_offer=inspection.offer,
        )

    assert error.value.stage == "drafting"
    assert error.value.steps == ()
    assert isinstance(error.value.__cause__, DraftmancerAdapterError)
    # The submitted pick reached the session, but the repeated pack never became
    # a new offer, so no step is recorded and no second pick is submitted.
    assert (
        error.value.snapshot.current_pack_event
        == inspection.snapshot.current_pack_event
    )
    assert error.value.snapshot.pool.total_cards == 1
    assert len(_pick_card_calls(socket)) == 1
    controller.close()


def test_build_failure_keeps_completed_steps_and_original_cause(
    tmp_path: Path,
) -> None:
    socket = _scripted_socket(
        config=_config(),
        states=(_state(pack_number=0, pick_number=0, arena_ids=(100,)),),
    )
    session = _RejectingBuildSession(
        log_path=None,
        app_dir=tmp_path / "app",
        card_database=_database(100),
    )
    # The build stage is forced by a session subclass whose dispatch rejects
    # RequestBuild; the production RequestBuild command is never monkeypatched.
    controller = _controller(socket=socket, session=session, grp_ids=(100,))

    with pytest.raises(TestDraftError, match="deck build failed") as error:
        controller.run_auto()

    assert error.value.stage == "build"
    assert len(error.value.steps) == 1
    assert error.value.steps[0].grp_id == 100
    assert error.value.snapshot.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert error.value.snapshot.pool.total_cards == 1
    assert isinstance(error.value.__cause__, DeckBuilderError)
    controller.close()


def test_build_failure_reports_the_published_session_error(
    tmp_path: Path,
) -> None:
    socket = _scripted_socket(
        config=_config(),
        states=(_state(pack_number=0, pick_number=0, arena_ids=(100,)),),
    )
    session = _BuildFailedSnapshotSession(
        log_path=None,
        app_dir=tmp_path / "app",
        card_database=_database(100),
    )
    controller = _controller(socket=socket, session=session, grp_ids=(100,))

    with pytest.raises(TestDraftError, match="structurally infeasible") as error:
        controller.run_auto()

    # The published error path raises without a wrapped cause, unlike the
    # raised-dispatch case, and still keeps every accepted step.
    assert error.value.stage == "build"
    assert error.value.__cause__ is None
    assert [step.grp_id for step in error.value.steps] == [100]
    controller.close()


def test_helper_run_keeps_normal_arena_state_and_audit_untouched(
    tmp_path: Path,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    recorded = _tree_entries(root=sources.normal_app_dir)
    arena_history = {
        relative: payload
        for relative, payload in recorded.items()
        if relative.parts[0] in {"state", "audit"} and payload is not None
    }
    assert set(arena_history) == {
        Path("audit/drafts/arena-account/arena-draft.jsonl"),
        Path("state/arena-account/arena-draft.json"),
    }
    assert Path("card-data/hob.json.gz") in recorded
    socket = _helper_socket(states=_print_states())

    result = _run_helper(sources=sources, socket=socket)

    _assert_tree_unchanged(root=sources.normal_app_dir, recorded=recorded)

    simulated = _tree_entries(root=sources.simulation_dir)
    simulated_files = {
        relative: payload
        for relative, payload in simulated.items()
        if payload is not None
    }
    assert simulated_files
    assert {relative.parts[0] for relative in simulated} == {"state", "audit"}
    user_id = _query(socket)["userID"][0]
    assert all(user_id in relative.parts for relative in simulated_files)
    persisted = list_draft_states(app_dir=sources.simulation_dir)
    assert len(persisted) == 1
    assert persisted[0].account_id == user_id
    assert persisted[0].completed is True
    assert persisted[0].pool_grp_ids == tuple(step.grp_id for step in result.steps)
    audit_records = load_draft_audit_records(
        account_id=persisted[0].account_id,
        draft_id=persisted[0].draft_id,
        app_dir=sources.simulation_dir,
    )
    assert {record["record_type"] for record in audit_records} == {
        "draft_started",
        "decision_evaluated",
        "choice_made",
        "draft_completed",
    }
    assert result.build.card_data.phase is DataLoadPhase.READY
    assert result.completed.contextual_adjustments_enabled is True
    assert result.completed.set_profile.set_code == "HOB"
    assert result.completed.set_profile.maturity == "metadata-only"
    assert result.completed.set_profile.source == "bundled-metadata-only"
    assert result.completed.set_profile.phase is DataLoadPhase.READY
    assert (
        result.completed.contextual_evidence.status
        is ContextualEvidenceStatus.UNAVAILABLE
    )
    # The headless runner closes through the reusable runtime, so the simulator
    # client transport is torn down exactly once.
    assert socket.disconnect_count == 1


def test_helper_run_publishes_configured_feature_flags(tmp_path: Path) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    socket = _helper_socket(states=_print_states())

    result = _run_helper(
        sources=sources,
        socket=socket,
        contextual_adjustments_enabled=False,
    )

    assert result.completed.contextual_adjustments_enabled is False
    assert (
        result.completed.contextual_evidence.status
        is ContextualEvidenceStatus.DISABLED
    )
    assert {
        relative.parts[0]
        for relative in _tree_entries(root=sources.simulation_dir)
    } == {"state", "audit"}


def test_helper_run_reports_a_truncated_bulk_source_as_a_startup_failure(
    tmp_path: Path,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    payload = gzip.compress(sources.bulk_path.read_bytes())
    truncated = tmp_path / "scryfall-default-cards.jsonl.gz"
    truncated.write_bytes(payload[: len(payload) // 2])

    # A truncated gzip bulk source must fail the documented startup boundary
    # with the decoder error as its cause, before any simulated session or
    # isolated state directory exists.
    with pytest.raises(TestDraftError) as error:
        _run_helper(
            sources=replace(sources, bulk_path=truncated),
            socket=_FakeSocket(),
        )

    assert error.value.stage == "startup"
    assert error.value.steps == ()
    assert isinstance(error.value.__cause__, (EOFError, OSError, zlib.error))
    assert not sources.simulation_dir.exists()


def test_helper_run_reports_the_installed_local_profile(tmp_path: Path) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    profile = load_set_profile(
        Path(__file__).parent / "fixtures" / "hob-relationship-scoring-profile.json",
        expected_set_code="HOB",
        expected_format=QUICK_DRAFT_FORMAT,
    )
    dump_set_profile(
        profile,
        set_profile_path(
            set_code="HOB",
            event_format=QUICK_DRAFT_FORMAT,
            app_dir=sources.normal_app_dir,
        ),
    )
    socket = _helper_socket(states=_print_states())

    result = _run_helper(sources=sources, socket=socket)

    # An empirical profile installed in the configured source directory is
    # retained, so the run never falls back to the bundled metadata-only one.
    assert result.completed.set_profile.source == "local-mature"
    assert result.completed.set_profile.maturity == "mature"
    assert result.completed.set_profile.profile_version == profile.profile_version
    assert result.completed.set_profile.phase is DataLoadPhase.READY
    assert result.completed.set_profile.set_code == "HOB"


def test_supported_test_draft_set_codes_includes_hob_only_when_both_sources_advertise_it(
    tmp_path: Path,
) -> None:
    advertise_hob = _draftmancer_checkout(
        root=tmp_path / "with-hob",
        mtga_sets=["HOB", "TST"],
    )
    assert supported_test_draft_set_codes(
        draftmancer_dir=advertise_hob,
        draftomen_set_codes=("hob", "TST", "omen-only"),
    ) == ("hob", "tst")

    # The same request against a checkout without HOB proves there is no HOB
    # special case: only advertised intersections survive, case-folded.
    without_hob = _draftmancer_checkout(
        root=tmp_path / "tst-only",
        mtga_sets=["TST"],
    )
    assert supported_test_draft_set_codes(
        draftmancer_dir=without_hob,
        draftomen_set_codes=("hob", "TST", "omen-only"),
    ) == ("tst",)

    not_a_list = _draftmancer_checkout(root=tmp_path / "not-a-list", mtga_sets="HOB")
    with pytest.raises(TestDraftError) as unlisted:
        supported_test_draft_set_codes(
            draftmancer_dir=not_a_list,
            draftomen_set_codes=("hob",),
        )

    assert unlisted.value.stage == "startup"

    blank_entry = _draftmancer_checkout(
        root=tmp_path / "blank-entry",
        mtga_sets=["HOB", ""],
    )
    with pytest.raises(
        TestDraftError,
        match="Draftmancer set capability metadata is invalid:",
    ) as malformed:
        supported_test_draft_set_codes(
            draftmancer_dir=blank_entry,
            draftomen_set_codes=("hob",),
        )

    assert malformed.value.stage == "startup"
    assert isinstance(malformed.value.__cause__, DraftmancerAdapterError)


def test_supported_test_draft_sets_list_every_checkout_set_by_full_name(
    tmp_path: Path,
) -> None:
    checkout = _draftmancer_checkout(root=tmp_path, mtga_sets=["WOE", "hob", "LCI", "TST"])
    (checkout / "src" / "data" / "SetsInfos.json").write_text(
        json.dumps(
            {
                "woe": {"fullName": "Wilds of Eldraine"},
                "hob": {"fullName": "The Hobbit"},
                "lci": {"fullName": "The Lost Caverns of Ixalan"},
                "tst": {"fullName": "  "},
                "unused": "not an object",
            }
        ),
        encoding="utf-8",
    )

    # Uncached sets are listed too, and a set without a usable name keeps its code.
    assert supported_test_draft_sets(
        draftmancer_dir=checkout,
        cached_set_codes=("HOB", "lci", "omen-only"),
    ) == (
        TestDraftSet(code="hob", name="The Hobbit", card_data_cached=True),
        TestDraftSet(code="lci", name="The Lost Caverns of Ixalan", card_data_cached=True),
        TestDraftSet(code="tst", name="TST", card_data_cached=False),
        TestDraftSet(code="woe", name="Wilds of Eldraine", card_data_cached=False),
    )


def test_supported_test_draft_sets_fail_closed_on_missing_or_malformed_metadata(
    tmp_path: Path,
) -> None:
    missing = _draftmancer_checkout(root=tmp_path / "missing", mtga_sets=["HOB"])
    with pytest.raises(TestDraftError, match="missing Draftmancer set metadata file"):
        supported_test_draft_sets(draftmancer_dir=missing, cached_set_codes=())

    malformed = _draftmancer_checkout(root=tmp_path / "malformed", mtga_sets=["HOB"])
    (malformed / "src" / "data" / "SetsInfos.json").write_text("[]", encoding="utf-8")
    with pytest.raises(TestDraftError, match="SetsInfos.json must contain an object") as error:
        supported_test_draft_sets(draftmancer_dir=malformed, cached_set_codes=())

    assert error.value.stage == "startup"


def test_reusable_test_draft_runtime_matches_headless_auto_contract(
    tmp_path: Path,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    published: list[LiveSessionSnapshot] = []
    socket = _helper_socket(states=_print_states())
    runtime = _create_runtime(
        sources=sources,
        socket=socket,
        snapshot_publisher=published.append,
        simulation_app_dir=sources.simulation_dir,
    )

    assert runtime.session.log_path is None
    assert runtime.simulation_app_dir == sources.simulation_dir
    assert runtime.simulation_app_dir != sources.normal_app_dir
    assert runtime.profile_client.profile_path(
        "HOB", "QuickDraft"
    ).is_relative_to(sources.normal_app_dir)

    result = runtime.controller.run_auto()

    assert result.completed.status.phase is ApplicationPhase.DRAFT_COMPLETE
    assert result.completed.pool.total_cards == len(_HELPER_OFFERS)
    assert len(result.steps) == len(_HELPER_OFFERS)
    accepted: tuple[int, ...] = ()
    for offered_grp_ids, step in zip(_HELPER_OFFERS, result.steps, strict=True):
        before = step.before.snapshot
        assert before.current_pack_event is not None
        assert before.current_pack_event.offered_grp_ids == offered_grp_ids
        assert before.pool.total_cards == len(accepted)
        rows = before.recommendations.cards
        assert tuple(row.rank for row in rows) == tuple(range(1, len(rows) + 1))
        assert {row.card.grp_id for row in rows} == set(offered_grp_ids)
        assert rows[0].card.grp_id == step.grp_id
        accepted += (step.grp_id,)

    build = result.build.build
    assert build is not None
    assert build.domain_pool is not None
    assert build.domain_pool.pool_grp_ids == tuple(step.grp_id for step in result.steps)
    assert build.domain_selection is not None
    assert build.domain_spell_selection is not None
    assert build.domain_mana_base is not None

    states = list_draft_states(app_dir=runtime.simulation_app_dir)
    assert len(states) == 1
    assert states[0].completed is True
    assert states[0].pool_grp_ids == accepted
    records = load_draft_audit_records(
        account_id=states[0].account_id,
        draft_id=states[0].draft_id,
        app_dir=runtime.simulation_app_dir,
    )
    assert "draft_completed" in {record["record_type"] for record in records}

    runtime.close()

    assert socket.disconnect_count == 1


def test_create_test_draft_runtime_accepts_a_card_image_service(
    tmp_path: Path,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    image_path = tmp_path / "card-images" / "fixture.jpg"
    fetched_uris: list[str] = []

    class RecordingCardImageService:
        """Record every image fetch and answer with one local placeholder."""

        def fetch(self, *, image_uri: str) -> Path:
            fetched_uris.append(image_uri)
            return image_path

    socket = _FakeSocket()
    with _create_runtime(
        sources=sources,
        socket=socket,
        simulation_app_dir=sources.simulation_dir,
        card_image_service=RecordingCardImageService(),
    ) as runtime:
        request = CardImageRequest(
            generation=1,
            grp_id=_HELPER_GRP_IDS[0],
            image_uri="https://images.example/x.jpg",
        )

        result = runtime.session.fetch_card_image(request=request)

        assert isinstance(result, CardImageFetchResult)
        assert result.image_path == image_path
        assert result.image_uri == request.image_uri
        assert fetched_uris == [request.image_uri]

    assert socket.disconnect_count == 1


def test_create_test_draft_runtime_loads_and_rescores_hob_augmentation(
    tmp_path: Path,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    oracle_ids = {
        grp_id: f"00000000-0000-0000-0000-{grp_id:012d}"
        for grp_id in _HELPER_GRP_IDS
    }
    database = _database(*_HELPER_GRP_IDS)
    augmented_database = replace(
        database,
        cards={
            grp_id: replace(card, oracle_id=oracle_ids[grp_id])
            for grp_id, card in database.cards.items()
        },
    )
    cache_path = card_data_cache_path(
        set_code=_HELPER_SET_CODE,
        app_dir=sources.normal_app_dir,
    )
    cache_path.write_bytes(
        SetCardData.from_card_database(
            augmented_database,
            set_code=_HELPER_SET_CODE,
            set_name=_HELPER_SET_NAME,
        ).to_gzip_bytes()
    )
    client = AugmentedModelClient(app_dir=tmp_path / "augmented-model")
    socket = _helper_socket(states=_print_states())

    with create_test_draft_runtime(
        draftmancer_dir=sources.draftmancer_dir,
        scryfall_bulk_file=sources.bulk_path,
        server_url="http://127.0.0.1:3000",
        set_code=_HELPER_SET_CODE,
        timeout_seconds=1.0,
        source_app_dir=sources.normal_app_dir,
        profile_manifest_url=None,
        profile_network_policy=ProfileNetworkPolicy.OFFLINE,
        simulation_app_dir=sources.simulation_dir,
        socket_client=socket,
        augmented_model_client=client,
    ) as runtime:
        inspection = runtime.controller.start()
        basic_rows = tuple(
            (
                row.card.grp_id,
                row.score,
                row.basic_score,
                row.augmentation_delta,
            )
            for row in inspection.snapshot.recommendations.cards
        )
        assert inspection.snapshot.current_pack_event is not None
        assert inspection.snapshot.current_pack_event.set_code == "HOB"

        request = runtime.session.augmented_model_request()
        assert request is not None
        assert request.set_code == "HOB"

        artifact = fixed_delta_artifact(
            set_code=_HELPER_SET_CODE,
            candidate_ids=tuple(oracle_ids[grp_id] for grp_id in _HELPER_GRP_IDS),
            deltas=(-2.0, -2.0, -2.0, 6.0),
        )
        model_cache_path = client.cache_path(_HELPER_SET_CODE)
        model_cache_path.parent.mkdir(parents=True, exist_ok=True)
        model_cache_path.write_bytes(artifact.to_gzip_bytes())
        load = client.load(request.set_code, allow_network=False)
        assert load.available
        assert load.artifact is not None

        runtime.session.complete_augmented_model(request=request, load=load)
        available = runtime.session.snapshot
        assert available.augmentation.status is AugmentationStatus.AVAILABLE
        assert available.augmentation.set_code == "HOB"
        assert runtime.session.augmented_model_request() is None

        augmented = runtime.session.dispatch(
            command=ChangeAugmentation(enabled=True)
        )
        augmented_rows = tuple(
            (
                row.card.grp_id,
                row.score,
                row.basic_score,
                row.augmentation_delta,
            )
            for row in augmented.recommendations.cards
        )
        assert augmented.augmentation.status is AugmentationStatus.AVAILABLE
        assert augmented.augmentation.enabled
        assert augmented_rows != basic_rows
        assert augmented_rows[0][0] == _HELPER_GRP_IDS[-1]
        assert augmented_rows[0][0] != basic_rows[0][0]
        assert augmented_rows[0][2] is not None
        assert augmented_rows[0][3] is not None

        restored = runtime.session.dispatch(
            command=ChangeAugmentation(enabled=False)
        )
        assert tuple(
            (
                row.card.grp_id,
                row.score,
                row.basic_score,
                row.augmentation_delta,
            )
            for row in restored.recommendations.cards
        ) == basic_rows

    assert socket.disconnect_count == 1


def test_reusable_test_draft_runtime_removes_its_implicit_simulation_directory(
    tmp_path: Path,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    published: list[LiveSessionSnapshot] = []
    socket = _FakeSocket()
    runtime = _create_runtime(
        sources=sources,
        socket=socket,
        snapshot_publisher=published.append,
    )

    simulation_dir = runtime.simulation_app_dir
    assert simulation_dir.exists()
    assert not simulation_dir.is_relative_to(sources.normal_app_dir)

    runtime.close()

    assert not simulation_dir.exists()
    assert [snapshot.status.phase for snapshot in published] == [
        ApplicationPhase.STOPPED
    ]
    assert socket.disconnect_count == 1

    runtime.cancel()
    runtime.close()

    assert [snapshot.status.phase for snapshot in published] == [
        ApplicationPhase.STOPPED
    ]
    assert socket.disconnect_count == 1


def test_reusable_test_draft_runtime_cleans_up_partial_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    recorded = _tree_entries(root=sources.normal_app_dir)
    temporary_root = tmp_path / "temp-root"
    temporary_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary_root))
    published: list[LiveSessionSnapshot] = []
    created_directories: list[Path] = []
    socket = _FakeSocket()

    def refuse(*, session: LiveSession, adapter: DraftmancerAdapter) -> object:
        del session, adapter
        created_directories.extend(temporary_root.iterdir())
        raise RuntimeError("controller construction refused")

    # Only the module boundary is replaced, so the session and adapter are
    # real and the runtime factory owns cleaning them up again.
    monkeypatch.setattr("draftomen.test_draft.TestDraftController", refuse)

    with pytest.raises(TestDraftError) as error:
        _create_runtime(
            sources=sources,
            socket=socket,
            snapshot_publisher=published.append,
        )

    assert error.value.stage == "startup"
    assert "the test draft could not start:" in str(error.value)
    assert isinstance(error.value.__cause__, RuntimeError)
    assert socket.disconnect_count == 1
    assert [snapshot.status.phase for snapshot in published] == [
        ApplicationPhase.STOPPED
    ]
    # The implicit simulation directory lived under the patched temporary root
    # while the controller was being built, and close removed it again.
    assert len(created_directories) == 1
    assert not created_directories[0].exists()
    assert list(temporary_root.iterdir()) == []
    _assert_tree_unchanged(root=sources.normal_app_dir, recorded=recorded)


def test_reusable_test_draft_runtime_reports_implicit_directory_failure_as_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    recorded = _tree_entries(root=sources.normal_app_dir)
    temporary_root = tmp_path / "temp-root"
    temporary_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary_root))
    create_directory = tempfile.TemporaryDirectory

    def refuse_simulation_directory(
        *args: object,
        **kwargs: object,
    ) -> tempfile.TemporaryDirectory[str]:
        # Only the implicit simulation directory fails; every unrelated
        # temporary directory in the call path still delegates to the real one.
        if kwargs.get("prefix") == SIMULATION_DIRECTORY_PREFIX:
            raise OSError("no space left on device")
        return create_directory(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", refuse_simulation_directory)
    socket = _FakeSocket()

    with pytest.raises(TestDraftError) as error:
        _create_runtime(sources=sources, socket=socket)

    assert error.value.stage == "startup"
    assert str(error.value).startswith("the test draft could not start:")
    assert "no space left on device" in str(error.value)
    assert isinstance(error.value.__cause__, OSError)
    assert list(temporary_root.iterdir()) == []
    assert socket.disconnect_count == 0
    _assert_tree_unchanged(root=sources.normal_app_dir, recorded=recorded)

    # The CLI handler prints its own diagnostic only for these two error types,
    # so the same failure must not reach it as a bare OSError.
    with pytest.raises(TestDraftError) as helper_error:
        run_test_draft_auto(
            draftmancer_dir=sources.draftmancer_dir,
            scryfall_bulk_file=sources.bulk_path,
            server_url="http://127.0.0.1:3000",
            set_code=_HELPER_SET_CODE,
            timeout_seconds=1.0,
            source_app_dir=sources.normal_app_dir,
            profile_manifest_url=None,
            profile_network_policy=ProfileNetworkPolicy.OFFLINE,
            socket_client=socket,
        )

    assert helper_error.value.stage == "startup"
    assert not isinstance(helper_error.value, OSError)
    assert isinstance(helper_error.value.__cause__, OSError)
    assert list(temporary_root.iterdir()) == []
    assert socket.disconnect_count == 0
    _assert_tree_unchanged(root=sources.normal_app_dir, recorded=recorded)


def test_reusable_test_draft_runtime_reports_explicit_directory_failure_as_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    recorded = _tree_entries(root=sources.normal_app_dir)
    simulation_app_dir = tmp_path / "unresolvable-simulation"
    expanduser = Path.expanduser

    def refuse_unresolvable_simulation_directory(self: Path) -> Path:
        # Only the unresolvable simulation directory fails; every other
        # expansion in the call path still delegates to the real one.
        if self == simulation_app_dir:
            raise OSError("could not determine the simulation directory home")
        return expanduser(self)

    monkeypatch.setattr(Path, "expanduser", refuse_unresolvable_simulation_directory)
    socket = _FakeSocket()

    with pytest.raises(TestDraftError) as error:
        _create_runtime(
            sources=sources,
            socket=socket,
            simulation_app_dir=simulation_app_dir,
        )

    assert error.value.stage == "startup"
    assert str(error.value).startswith("the test draft could not start:")
    assert "could not determine the simulation directory home" in str(error.value)
    assert isinstance(error.value.__cause__, OSError)
    assert not simulation_app_dir.exists()
    assert socket.disconnect_count == 0
    _assert_tree_unchanged(root=sources.normal_app_dir, recorded=recorded)


@pytest.mark.parametrize(
    ("boundary", "expected_disconnects", "expected_stopped"),
    [
        ("draftomen.test_draft.LiveSession", 0, 0),
        ("draftomen.test_draft.DraftmancerAdapter", 0, 1),
    ],
    ids=("session", "adapter"),
)
def test_reusable_test_draft_runtime_cleans_up_every_partial_construction_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    expected_disconnects: int,
    expected_stopped: int,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    recorded = _tree_entries(root=sources.normal_app_dir)
    temporary_root = tmp_path / "temp-root"
    temporary_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary_root))
    published: list[LiveSessionSnapshot] = []
    created_directories: list[Path] = []
    socket = _FakeSocket()

    def refuse(*args: object, **kwargs: object) -> object:
        del args, kwargs
        created_directories.extend(temporary_root.iterdir())
        raise RuntimeError("construction refused")

    # Only the module boundary under test is replaced, so every collaborator
    # built before it stays real and the runtime factory must unwind it.
    monkeypatch.setattr(boundary, refuse)

    with pytest.raises(TestDraftError) as error:
        _create_runtime(
            sources=sources,
            socket=socket,
            snapshot_publisher=published.append,
        )

    assert error.value.stage == "startup"
    assert str(error.value).startswith("the test draft could not start:")
    assert isinstance(error.value.__cause__, RuntimeError)
    assert socket.disconnect_count == expected_disconnects
    assert [
        snapshot.status.phase for snapshot in published
    ].count(ApplicationPhase.STOPPED) == expected_stopped
    assert len(created_directories) == 1
    assert not created_directories[0].exists()
    assert list(temporary_root.iterdir()) == []
    _assert_tree_unchanged(root=sources.normal_app_dir, recorded=recorded)


def test_runtime_cancel_wakes_blocked_start(tmp_path: Path) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    entered = threading.Event()
    socket = _FakeSocket(start_action=_withheld_ack_action(entered=entered))
    runtime = _create_runtime(
        sources=sources,
        socket=socket,
        timeout_seconds=5.0,
        simulation_app_dir=sources.simulation_dir,
    )
    workers: list[threading.Thread] = []
    errors: list[BaseException] = []
    done = _start_worker(
        target=runtime.controller.start,
        workers=workers,
        errors=errors,
    )

    assert entered.wait(timeout=1)
    runtime.cancel()

    assert done.wait(timeout=1)
    for worker in workers:
        worker.join()

    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, TestDraftError)
    assert error.stage == "startup"
    assert _CANCELLATION_TEXT in str(error)
    assert isinstance(error.__cause__, DraftmancerAdapterError)
    assert error.steps == ()
    assert error.snapshot.status.phase is ApplicationPhase.WAITING_FOR_DRAFT
    assert error.snapshot.pool.total_cards == 0

    runtime.close()

    assert socket.disconnect_count == 1

    runtime.cancel()
    runtime.close()

    assert socket.disconnect_count == 1


def test_runtime_cancel_wakes_blocked_pick(tmp_path: Path) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    states = _print_states()
    entered = threading.Event()
    socket = _helper_socket(
        states=states,
        pick_actions=[
            _pick_actions(states=states)[0],
            _withheld_ack_action(entered=entered),
        ],
    )
    runtime = _create_runtime(
        sources=sources,
        socket=socket,
        timeout_seconds=5.0,
        simulation_app_dir=sources.simulation_dir,
    )
    workers: list[threading.Thread] = []
    errors: list[BaseException] = []

    runtime.controller.start()
    first_step = runtime.controller.advance_auto()
    inspection = runtime.controller.inspect()
    done = _start_worker(
        target=lambda: runtime.controller.confirm(
            grp_id=_rank_one(inspection),
            expected_offer=inspection.offer,
        ),
        workers=workers,
        errors=errors,
    )

    assert entered.wait(timeout=1)
    runtime.cancel()

    assert done.wait(timeout=1)
    for worker in workers:
        worker.join()

    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, TestDraftError)
    assert error.stage == "drafting"
    assert _CANCELLATION_TEXT in str(error)
    assert isinstance(error.__cause__, DraftmancerAdapterError)
    assert error.steps == (first_step,)
    assert error.snapshot.status.phase is ApplicationPhase.DRAFTING
    assert error.snapshot.pool.total_cards == 1

    persisted = list_draft_states(app_dir=runtime.simulation_app_dir)
    assert [state.chosen_pick_count for state in persisted] == [1]
    assert persisted[0].completed is False
    assert persisted[0].pool_grp_ids == (first_step.grp_id,)

    runtime.close()

    assert socket.disconnect_count == 1

    runtime.cancel()
    runtime.close()

    assert socket.disconnect_count == 1


def test_runtime_close_during_blocked_start_retires_every_resource_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    recorded = _tree_entries(root=sources.normal_app_dir)
    temporary_root = tmp_path / "temp-root"
    temporary_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary_root))
    published: list[LiveSessionSnapshot] = []
    entered = threading.Event()
    socket = _FakeSocket(start_action=_withheld_ack_action(entered=entered))
    runtime = _create_runtime(
        sources=sources,
        socket=socket,
        timeout_seconds=5.0,
        snapshot_publisher=published.append,
    )
    simulation_dir = runtime.simulation_app_dir
    assert simulation_dir.parent == temporary_root
    workers: list[threading.Thread] = []
    start_errors: list[BaseException] = []
    start_done = _start_worker(
        target=runtime.controller.start,
        workers=workers,
        errors=start_errors,
    )

    assert entered.wait(timeout=1)
    close_errors: list[BaseException] = []
    close_done = _start_worker(
        target=runtime.close,
        workers=workers,
        errors=close_errors,
    )

    # Close cancels the blocked start instead of waiting out its deadline.
    assert close_done.wait(timeout=2)

    assert start_done.wait(timeout=2)
    for worker in workers:
        worker.join()

    assert close_errors == []
    assert len(start_errors) == 1
    error = start_errors[0]
    assert isinstance(error, TestDraftError)
    assert error.stage == "startup"
    assert _CANCELLATION_TEXT in str(error)
    assert isinstance(error.__cause__, DraftmancerAdapterError)
    assert [snapshot.status.phase for snapshot in published] == [
        ApplicationPhase.STOPPED
    ]
    assert socket.disconnect_count == 1
    assert not simulation_dir.exists()
    assert list(temporary_root.iterdir()) == []
    _assert_tree_unchanged(root=sources.normal_app_dir, recorded=recorded)

    runtime.cancel()
    runtime.close()

    assert socket.disconnect_count == 1


def test_runtime_cancel_returns_while_snapshot_publication_is_held(
    tmp_path: Path,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    published: list[LiveSessionSnapshot] = []
    entered = threading.Event()
    released = threading.Event()

    def publish(snapshot: LiveSessionSnapshot) -> None:
        published.append(snapshot)
        # The start event's own snapshot is the first DRAFTING publication, so
        # holding it stalls the start path before its first pack is scored.
        if snapshot.status.phase is ApplicationPhase.DRAFTING and not entered.is_set():
            entered.set()
            assert released.wait(timeout=5)

    socket = _helper_socket(states=_print_states())
    runtime = _create_runtime(
        sources=sources,
        socket=socket,
        timeout_seconds=5.0,
        snapshot_publisher=publish,
    )
    workers: list[threading.Thread] = []
    start_errors: list[BaseException] = []
    start_done = _start_worker(
        target=runtime.controller.start,
        workers=workers,
        errors=start_errors,
    )

    assert entered.wait(timeout=1)
    cancel_errors: list[BaseException] = []
    cancel_done = _start_worker(
        target=runtime.cancel,
        workers=workers,
        errors=cancel_errors,
    )

    assert cancel_done.wait(timeout=1)
    assert not released.is_set()

    released.set()

    assert start_done.wait(timeout=2)
    for worker in workers:
        worker.join()

    assert cancel_errors == []
    assert len(start_errors) == 1
    error = start_errors[0]
    assert isinstance(error, TestDraftError)
    assert error.stage == "startup"
    assert _CANCELLATION_TEXT in str(error)
    assert isinstance(error.__cause__, DraftmancerAdapterError)
    assert _pick_card_calls(socket) == []

    runtime.close()

    assert [snapshot.status.phase for snapshot in published] == [
        ApplicationPhase.DRAFTING,
        ApplicationPhase.STOPPED,
    ]
    assert socket.disconnect_count == 1


def test_runtime_concurrent_close_retires_resources_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _seed_helper_sources(tmp_path=tmp_path)
    recorded = _tree_entries(root=sources.normal_app_dir)
    temporary_root = tmp_path / "temp-root"
    temporary_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary_root))
    published: list[LiveSessionSnapshot] = []
    socket = _FakeSocket()
    runtime = _create_runtime(
        sources=sources,
        socket=socket,
        snapshot_publisher=published.append,
    )
    simulation_dir = runtime.simulation_app_dir
    workers: list[threading.Thread] = []
    errors: list[BaseException] = []
    closers = [
        _start_worker(target=runtime.close, workers=workers, errors=errors)
        for _ in range(2)
    ]

    for closer in closers:
        assert closer.wait(timeout=2)
    for worker in workers:
        worker.join()

    assert errors == []
    assert socket.disconnect_count == 1
    assert [snapshot.status.phase for snapshot in published] == [
        ApplicationPhase.STOPPED
    ]
    assert not simulation_dir.exists()
    assert list(temporary_root.iterdir()) == []
    _assert_tree_unchanged(root=sources.normal_app_dir, recorded=recorded)


def test_default_test_draft_checkout_dir_composes_under_the_application_data_directory(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app-data"

    assert default_test_draft_checkout_dir(app_dir=app_dir) == app_dir / "draftmancer"
    assert default_test_draft_checkout_dir() == app_data_dir() / "draftmancer"


def test_default_test_draft_bulk_file_composes_under_the_application_data_directory(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app-data"

    assert default_test_draft_bulk_file(app_dir=app_dir) == (
        app_dir / "corpus-cache" / "sources" / "scryfall-default-cards.jsonl.gz"
    )
    assert default_test_draft_bulk_file() == (
        app_data_dir() / "corpus-cache" / "sources" / "scryfall-default-cards.jsonl.gz"
    )


def _resolver_bulk(*, tmp_path: Path) -> Path:
    bulk_path = tmp_path / "scryfall-default-cards.jsonl"
    rows = (
        {
            "set": "ktk",
            "id": "ktk-106",
            "oracle_id": "claws-oracle",
            "name": "Crater's Claws",
            "cmc": 1,
            "rarity": "rare",
            "type_line": "Sorcery",
            "colors": ["R"],
        },
        {
            "set": "spg",
            "id": "spg-19",
            "oracle_id": "prison-oracle",
            "arena_id": 88912,
            "name": "Ghostly Prison",
            "cmc": 3,
            "rarity": "uncommon",
            "type_line": "Enchantment",
            "colors": ["W"],
        },
    )
    bulk_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    return bulk_path


def test_missing_card_resolver_describes_arena_printings_from_the_bulk_file(
    tmp_path: Path,
) -> None:
    bulk_path = _resolver_bulk(tmp_path=tmp_path)
    scan = _scan_scryfall_bulk(
        bulk_path=bulk_path,
        set_code="ktk",
        card_database=_database(100),
    )
    resolver = _MissingCardResolver(
        bulk_path=bulk_path,
        set_rows_by_scryfall_id=scan.set_rows_by_scryfall_id,
    )

    card = resolver(grp_id=88912, scryfall_id="spg-19")

    assert card is not None
    assert (card.grp_id, card.name, card.set_code) == (88912, "Ghostly Prison", "spg")


def test_missing_card_resolver_uses_the_set_row_for_a_draftmancer_only_arena_id(
    tmp_path: Path,
) -> None:
    bulk_path = _resolver_bulk(tmp_path=tmp_path)
    scan = _scan_scryfall_bulk(
        bulk_path=bulk_path,
        set_code="ktk",
        card_database=_database(100),
    )
    resolver = _MissingCardResolver(
        bulk_path=bulk_path,
        set_rows_by_scryfall_id=scan.set_rows_by_scryfall_id,
    )

    card = resolver(grp_id=58149, scryfall_id="ktk-106")
    unknown = resolver(grp_id=58150, scryfall_id="not-in-set")

    assert card is not None
    assert (card.grp_id, card.name, card.colors) == (58149, "Crater's Claws", ("R",))
    assert unknown is None
