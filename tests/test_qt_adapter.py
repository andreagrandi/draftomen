from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import cast

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import (
    QCoreApplication,
    QMetaObject,
    QObject,
    QTimer,
    QUrl,
    Qt,
    Slot,
)

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.cardimages import CardImageService
from draftomen.mock_session import MockLiveSession
from draftomen.pool_ledger import evaluate_completed_pool_role_ledger
from draftomen.preferences import GuiDisplayPreferences, save_gui_preferences
from draftomen.profile_client import (
    ProfileClient,
    ProfileNetworkPolicy,
    ProfileRefreshOutcome,
    ProfileRefreshResult,
)
from draftomen.profile_manifest import ProfileManifest, ProfileManifestArtifact
from draftomen.qt_adapter import (
    GuiPreferencesAdapter,
    LiveSessionAdapter,
    RecommendationListModel,
    SessionAdapter,
)
from draftomen.session import (
    CardDataState,
    CardImageFetchResult,
    CardImageRequest,
    CardImageState,
    CardView,
    ChangeContextualScoring,
    ChangeRanking,
    ChangeSplashPreference,
    ChooseAccount,
    ChooseRecommendation,
    DataLoadPhase,
    DismissError,
    FocusBuildCard,
    LiveSession,
    LiveSessionCommand,
    LiveSessionSnapshot,
    PoolState,
    ProfileRefreshRequest,
    Recommendation,
    RecommendationState,
    RequestBacktest,
    RequestBuild,
    RequestRatingsDownload,
    RetryError,
    SetProfileState,
    SnapshotPublisher,
)
from draftomen.set_profile import (
    CardRating,
    SET_PROFILE_SCHEMA_VERSION,
    RateEstimate,
    SetProfile,
    dump_set_profile,
    load_set_profile,
)


_PROFILE_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json"
)
_PROFILE_MANIFEST_URL = "https://profiles.example.test/manifest.json"


class _ProfileHttpResponse:
    def __init__(self, *, payload: bytes, url: str) -> None:
        self._payload = payload
        self.url = url

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size >= len(self._payload):
            payload, self._payload = self._payload, b""
            return payload
        payload, self._payload = self._payload[:size], self._payload[size:]
        return payload

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        return None

    def __enter__(self) -> "_ProfileHttpResponse":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _adapter_empirical_profile(
    *,
    profile_version: str,
    generated_at: str,
) -> SetProfile:
    profile = load_set_profile(
        _PROFILE_FIXTURE_PATH,
        expected_set_code="TST",
        expected_format="QuickDraft",
    )
    return replace(
        profile,
        profile_version=profile_version,
        generated_at=generated_at,
        card_ratings=(
            CardRating(
                card_key="grp_id:104894",
                gih_win_rate=RateEstimate(
                    raw_value=0.82,
                    value=0.82,
                    samples=2_000,
                    prior_value=0.5,
                    source="17lands",
                ),
                average_last_seen_at=2.0,
            ),
        ),
    )


def _profile_transport_payload(
    profile: SetProfile,
    *,
    artifact_url: str,
    published_at: str,
) -> tuple[bytes, bytes]:
    profile_bytes = profile.to_bytes()
    gzip_bytes = gzip.compress(profile_bytes, mtime=0)
    artifact = ProfileManifestArtifact(
        set_code=profile.set_code,
        event_format=profile.event_format,
        set_profile_schema_version=SET_PROFILE_SCHEMA_VERSION,
        profile_version=profile.profile_version,
        generated_at=profile.generated_at,
        url=artifact_url,
        gzip_bytes=len(gzip_bytes),
        profile_bytes=len(profile_bytes),
        gzip_sha256=hashlib.sha256(gzip_bytes).hexdigest(),
        profile_sha256=hashlib.sha256(profile_bytes).hexdigest(),
        maturity=profile.maturity,
    )
    manifest = ProfileManifest(
        artifacts=(artifact,),
        published_at=published_at,
    )
    return manifest.to_bytes(), gzip_bytes


class _FakeSession:
    def __init__(self, *, publish: SnapshotPublisher) -> None:
        self._model = MockLiveSession()
        self._publish = publish
        self.snapshot = self._model.snapshot
        self.load_card_data_calls = 0
        self.startup_thread_ids: list[int] = []
        self.poll_thread_ids: list[int] = []
        self.startup_options: list[tuple[bool, bool]] = []
        self.dispatch_thread_ids: list[int] = []
        self.commands: list[LiveSessionCommand] = []

    def scan_startup_files(
        self,
        *,
        include_previous: bool = True,
        include_pre_draft_detection: bool = True,
    ) -> LiveSessionSnapshot:
        self.startup_thread_ids.append(threading.get_ident())
        self.startup_options.append(
            (include_previous, include_pre_draft_detection)
        )
        self._publish(self.snapshot)
        return self.snapshot

    def poll_once(self) -> LiveSessionSnapshot:
        self.poll_thread_ids.append(threading.get_ident())
        self._publish(self.snapshot)
        return self.snapshot

    def dispatch(self, *, command: LiveSessionCommand) -> LiveSessionSnapshot:
        self.dispatch_thread_ids.append(threading.get_ident())
        self.commands.append(command)
        self.snapshot = self._model.dispatch(command=command)
        self._publish(self.snapshot)
        return self.snapshot

    def stop(self) -> LiveSessionSnapshot:
        return self.snapshot

    def load_card_data(self) -> LiveSessionSnapshot:
        self.load_card_data_calls += 1
        raise AssertionError("startup must not load a global card database")


class _ProfileRefreshFakeClient:
    def __init__(self, *, result: object = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, str, bool]] = []
        self.thread_ids: list[int] = []

    def refresh(self, set_code: str, event_format: str, *, force: bool = False) -> object:
        self.calls.append((set_code, event_format, force))
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        self.release.wait(timeout=3.0)
        if self.error is not None:
            raise self.error
        return self.result


class _ProfileRefreshFakeSession(_FakeSession):
    def __init__(
        self,
        *,
        publish: SnapshotPublisher,
        force: bool = False,
    ) -> None:
        super().__init__(publish=publish)
        self.profile_request = ProfileRefreshRequest(
            generation=1,
            set_code="OTJ",
            event_format="QuickDraft",
            force=force,
        )
        self.completed_thread_ids: list[int] = []
        self.failed_thread_ids: list[int] = []
        self.completed_results: list[object] = []
        self.failed_messages: list[str | None] = []

    def profile_refresh_request(self) -> ProfileRefreshRequest | None:
        return self.profile_request

    def complete_profile_refresh(
        self,
        *,
        request: ProfileRefreshRequest,
        result: object,
    ) -> None:
        assert request == self.profile_request
        self.completed_thread_ids.append(threading.get_ident())
        self.completed_results.append(result)
        self.profile_request = None
        self.snapshot = replace(
            self.snapshot,
            set_profile=replace(
                self.snapshot.set_profile,
                refresh_outcome="updated",
                message="Updated OTJ set profile.",
            ),
        )
        self._publish(self.snapshot)

    def fail_profile_refresh(
        self,
        *,
        request: ProfileRefreshRequest,
        error_message: str | None = None,
    ) -> None:
        assert request == self.profile_request
        self.failed_thread_ids.append(threading.get_ident())
        self.failed_messages.append(error_message)
        self.profile_request = None
        self.snapshot = replace(
            self.snapshot,
            set_profile=replace(
                self.snapshot.set_profile,
                phase=DataLoadPhase.FAILED,
                refresh_outcome="remote_failed",
                message="Set profile refresh failed for OTJ; using the last-good profile.",
            ),
        )
        self._publish(self.snapshot)


class _StartupReplaySession(_FakeSession):
    def __init__(self, *, publish: SnapshotPublisher) -> None:
        super().__init__(publish=publish)
        self.poll_count = 0
        self.post_start_ready = threading.Event()

    def scan_startup_files(
        self,
        *,
        include_previous: bool = True,
        include_pre_draft_detection: bool = True,
    ) -> LiveSessionSnapshot:
        self.startup_thread_ids.append(threading.get_ident())
        self.startup_options.append(
            (include_previous, include_pre_draft_detection)
        )
        source_cards = self._model.snapshot.recommendations.cards
        for card_count in (12, 4, 0):
            self.snapshot = replace(
                self.snapshot,
                recommendations=replace(
                    self.snapshot.recommendations,
                    cards=source_cards[:card_count],
                ),
            )
            self._publish(self.snapshot)
        return self.snapshot

    def poll_once(self) -> LiveSessionSnapshot:
        self.poll_thread_ids.append(threading.get_ident())
        self.poll_count += 1
        if self.poll_count == 2:
            self.snapshot = replace(
                self.snapshot,
                recommendations=replace(
                    self.snapshot.recommendations,
                    cards=self._model.snapshot.recommendations.cards[:1],
                ),
            )
            self.post_start_ready.set()
        self._publish(self.snapshot)
        return self.snapshot


class _FailingFirstPollSession(_FakeSession):
    def __init__(self, *, publish: SnapshotPublisher) -> None:
        super().__init__(publish=publish)
        self.poll_count = 0
        self.recovery_allowed = threading.Event()
        self.recovered = threading.Event()

    def poll_once(self) -> LiveSessionSnapshot:
        self.poll_thread_ids.append(threading.get_ident())
        self.poll_count += 1
        if self.poll_count == 1:
            self._publish(self.snapshot)
            raise RuntimeError("initial poll failed")
        self.recovery_allowed.wait(timeout=3.0)
        self.recovered.set()
        return self.snapshot


class _ImageFakeSession(_FakeSession):
    def __init__(self, *, publish: SnapshotPublisher) -> None:
        super().__init__(publish=publish)
        self.request = CardImageRequest(
            generation=1,
            grp_id=1,
            image_uri="https://images.example/fixture.jpg",
        )
        self.fetch_thread_ids: list[int] = []
        self.session_thread_ids: list[int] = []
        self.result_thread_ids: list[int] = []
        self.completions: list[Path] = []
        self._request_pending = True
        self.stopped = False

    def selected_card_image_request(self) -> CardImageRequest | None:
        self.session_thread_ids.append(threading.get_ident())
        return self.request if self._request_pending and not self.stopped else None

    def fetch_card_image(
        self,
        *,
        request: CardImageRequest,
    ) -> CardImageFetchResult:
        assert request == self.request
        self.fetch_thread_ids.append(threading.get_ident())
        return CardImageFetchResult(
            image_path=Path("/tmp/fixture card.jpg"),
            image_uri=request.image_uri or "https://images.example/fixture.jpg",
        )

    def complete_card_image_request(
        self,
        *,
        request: CardImageRequest,
        image_path: Path,
        image_uri: str | None = None,
    ) -> None:
        assert request == self.request
        del image_uri
        self.result_thread_ids.append(threading.get_ident())
        self.completions.append(image_path)
        self._request_pending = False
        self.snapshot = replace(
            self.snapshot,
            card_image=CardImageState(
                grp_id=request.grp_id,
                image_path=str(image_path),
                phase=DataLoadPhase.READY,
                message="Card image ready.",
            ),
        )
        self._publish(self.snapshot)

    def fail_card_image_request(
        self,
        *,
        request: CardImageRequest,
        error_message: str,
    ) -> None:
        raise AssertionError(f"Unexpected image failure: {request} {error_message}")

    def stop(self) -> LiveSessionSnapshot:
        self.stopped = True
        return self.snapshot


class _BlockedFocusedImageFakeSession(_ImageFakeSession):
    def __init__(self, *, publish: SnapshotPublisher) -> None:
        super().__init__(publish=publish)
        self.request = replace(self.request, image_uri=None)
        self.fetch_started = threading.Event()
        self.fetch_release = threading.Event()

    def fetch_card_image(
        self,
        *,
        request: CardImageRequest,
    ) -> CardImageFetchResult:
        self.fetch_thread_ids.append(threading.get_ident())
        self.fetch_started.set()
        self.fetch_release.wait(timeout=3.0)
        return CardImageFetchResult(
            image_path=Path("/tmp/focused metadata.jpg"),
            image_uri="https://images.example/focused-metadata.jpg",
        )

    def dispatch(self, *, command: LiveSessionCommand) -> LiveSessionSnapshot:
        self.dispatch_thread_ids.append(threading.get_ident())
        self.commands.append(command)
        self._publish(self.snapshot)
        return self.snapshot

    def stop(self) -> LiveSessionSnapshot:
        self.fetch_release.set()
        return super().stop()


class _RecommendationImageFakeSession(_FakeSession):
    def __init__(self, *, publish: SnapshotPublisher) -> None:
        super().__init__(publish=publish)
        cards = self.snapshot.recommendations.cards[:2]
        self.requests = [
            CardImageRequest(
                generation=1,
                grp_id=recommendation.card.grp_id,
                image_uri=f"https://images.example/{recommendation.card.grp_id}.jpg",
            )
            for recommendation in cards
        ]
        self.started = threading.Event()
        self.release = threading.Event()
        self.fetch_thread_ids: list[int] = []
        self.session_thread_ids: list[int] = []
        self.result_thread_ids: list[int] = []
        self.completed: list[CardImageRequest] = []
        self.snapshot = replace(
            self.snapshot,
            recommendations=replace(
                self.snapshot.recommendations,
                cards=cards,
                selected_grp_id=cards[0].card.grp_id,
            ),
        )

    def selected_card_image_request(self) -> CardImageRequest | None:
        self.session_thread_ids.append(threading.get_ident())
        return None

    def recommendation_image_request(self) -> CardImageRequest | None:
        self.session_thread_ids.append(threading.get_ident())
        return self.requests[0] if self.requests else None

    def fetch_card_image(
        self,
        *,
        request: CardImageRequest,
    ) -> CardImageFetchResult:
        self.fetch_thread_ids.append(threading.get_ident())
        if not self.started.is_set():
            self.started.set()
            self.release.wait(timeout=3.0)
        return CardImageFetchResult(
            image_path=Path(f"/tmp/{request.grp_id}.jpg"),
            image_uri=request.image_uri
            or f"https://images.example/{request.grp_id}.jpg",
        )

    def complete_recommendation_image_request(
        self,
        *,
        request: CardImageRequest,
        image_path: Path,
        image_uri: str | None = None,
    ) -> None:
        del image_uri
        self.result_thread_ids.append(threading.get_ident())
        self.completed.append(request)
        self.requests.remove(request)
        recommendations = replace(
            self.snapshot.recommendations,
            cards=tuple(
                replace(
                    recommendation,
                    card=replace(
                        recommendation.card,
                        image_path=str(image_path),
                    ),
                )
                if recommendation.card.grp_id == request.grp_id
                else recommendation
                for recommendation in self.snapshot.recommendations.cards
            ),
        )
        self.snapshot = replace(self.snapshot, recommendations=recommendations)
        self._publish(self.snapshot)

    def fail_recommendation_image_request(
        self,
        *,
        request: CardImageRequest,
        error_message: str,
    ) -> None:
        raise AssertionError(f"Unexpected image failure: {request} {error_message}")

    def stop(self) -> LiveSessionSnapshot:
        self.release.set()
        return self.snapshot


class _BusySession(_FakeSession):
    def __init__(self, *, publish: SnapshotPublisher) -> None:
        super().__init__(publish=publish)
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = False

    def scan_startup_files(
        self,
        *,
        include_previous: bool = True,
        include_pre_draft_detection: bool = True,
    ) -> LiveSessionSnapshot:
        self.started.set()
        self.release.wait(timeout=3.0)
        return super().scan_startup_files(
            include_previous=include_previous,
            include_pre_draft_detection=include_pre_draft_detection,
        )

    def stop(self) -> LiveSessionSnapshot:
        self.stopped = True
        self.release.set()
        return self.snapshot


class _FailingStartupSession(_FakeSession):
    def __init__(self, *, publish: SnapshotPublisher) -> None:
        super().__init__(publish=publish)
        self.stopped = False

    def scan_startup_files(
        self,
        *,
        include_previous: bool = True,
        include_pre_draft_detection: bool = True,
    ) -> LiveSessionSnapshot:
        del include_previous, include_pre_draft_detection
        self._publish(self.snapshot)
        raise RuntimeError("startup scan failed")

    def stop(self) -> LiveSessionSnapshot:
        self.stopped = True
        self._publish(self.snapshot)
        return self.snapshot


class _StateObserver(QObject):
    def __init__(self, *, adapter: LiveSessionAdapter) -> None:
        super().__init__()
        self._adapter = adapter
        self.thread_ids: list[int] = []
        self.states: list[dict[str, object]] = []

    @Slot()
    def observe(self) -> None:
        self.thread_ids.append(threading.get_ident())
        self.states.append(self._adapter.state)


@pytest.fixture
def qcore_application() -> QCoreApplication:
    return cast(QCoreApplication, QCoreApplication.instance() or QCoreApplication([]))


def test_session_adapter_converts_local_image_path_to_file_url(
    tmp_path: Path,
) -> None:
    role_ledger = evaluate_completed_pool_role_ledger(
        final_pool=(1,),
        card_database=CardDatabase(
            cards={
                1: CardInfo(
                    grp_id=1,
                    name="Fixture Ledger Card",
                    colors=(),
                    mana_value=1.0,
                    rarity="common",
                    types=("Creature",),
                )
            }
        ),
    )
    image_path = tmp_path / "card images" / "Fixture Card.jpg"
    snapshot = LiveSessionSnapshot(
        card_image=CardImageState(
            grp_id=1,
            image_path=str(image_path),
            phase=DataLoadPhase.READY,
            message="Card image ready.",
        ),
        recommendations=RecommendationState(
            cards=(
                Recommendation(
                    rank=1,
                    card=CardView(
                        grp_id=1,
                        name="Fixture Card",
                        colors=(),
                        rarity="common",
                        types=("Creature",),
                        mana_cost=None,
                        mana_value=1.0,
                        image_path=str(image_path),
                    ),
                    score=1,
                    win_rate=None,
                    average_last_seen_at=None,
                    source_label="Fixture",
                    color_fit="on_color",
                    no_data=False,
                ),
            ),
            selected_grp_id=1,
        ),
        pool=PoolState(current_colors=("W", "U"), role_ledger=role_ledger),
    )

    adapter = SessionAdapter(snapshot=snapshot)

    assert adapter.state["recommendations"]["cards"][0]["card"]["image_path"] == (
        QUrl.fromLocalFile(str(image_path)).toString()
    )
    assert adapter.state["card_image"]["image_path"] == (
        QUrl.fromLocalFile(str(image_path)).toString()
    )
    assert adapter.state["pool"]["current_colors"] == ["W", "U"]
    assert adapter.state["pool"]["role_ledger"]["mode"] == "completed_pool"
    assert adapter.state["pool"]["role_ledger"]["stage"] is None


def test_session_adapter_exposes_card_data_update_time_in_qvariant_map() -> None:
    timestamp = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    adapter = SessionAdapter(
        snapshot=LiveSessionSnapshot(
            card_data=CardDataState(
                phase=DataLoadPhase.READY,
                message="Card metadata is ready.",
                last_successful_update=timestamp.isoformat(),
            )
        )
    )

    assert adapter.state["card_data"]["last_successful_update"] == (
        timestamp.isoformat()
    )


def test_session_adapter_translates_set_profile_to_plain_qml_values() -> None:
    adapter = SessionAdapter(
        snapshot=LiveSessionSnapshot(
            set_profile=SetProfileState(
                set_code="OTJ",
                event_format="QuickDraft",
                maturity="semantic",
                profile_version="2026.08",
                source="local-semantic",
                phase=DataLoadPhase.READY,
                refresh_outcome="cached",
                message="Using the cached semantic set profile for OTJ.",
            )
        )
    )

    profile_state = adapter.state["set_profile"]
    assert profile_state == {
        "set_code": "OTJ",
        "event_format": "QuickDraft",
        "maturity": "semantic",
        "profile_version": "2026.08",
        "source": "local-semantic",
        "phase": "ready",
        "refresh_outcome": "cached",
        "message": "Using the cached semantic set profile for OTJ.",
    }
    assert not isinstance(profile_state, SetProfileState)


def test_recommendation_model_updates_rows_without_reset_churn() -> None:
    model = RecommendationListModel()
    model_resets: list[object] = []
    data_changes: list[object] = []
    model.modelReset.connect(lambda: model_resets.append(True))
    model.dataChanged.connect(lambda *args: data_changes.append(args))

    def rows(*, image_suffix: str) -> list[dict[str, object]]:
        return [
            {
                "grp_id": grp_id,
                "card": {"image_path": f"file:///card-{grp_id}-{image_suffix}.jpg"},
            }
            for grp_id in range(12)
        ]

    model.replace(rows=rows(image_suffix="first"))
    for image_suffix in ("second", "third", "fourth"):
        model.replace(rows=rows(image_suffix=image_suffix))

    assert model.rowCount() == 12
    assert model.data(
        model.index(0, 0),
        RecommendationListModel.MODEL_DATA_ROLE,
    )["card"]["image_path"] == "file:///card-0-fourth.jpg"
    assert len(data_changes) == 3
    assert model_resets == []

    model.replace(rows=[])
    assert model.rowCount() == 0
    model.replace(rows=rows(image_suffix="restored"))
    assert model.rowCount() == 12


def test_recommendation_model_color_projection_is_reactive_and_non_mutating() -> None:
    model = RecommendationListModel()
    rows = [
        {"rank": 1, "score": 90, "card": {"grp_id": 1, "colors": ["W"]}},
        {"rank": 2, "score": 80, "card": {"grp_id": 2, "colors": ["U"]}},
        {"rank": 3, "score": 70, "card": {"grp_id": 3, "colors": ["W", "G"]}},
        {"rank": 4, "score": 60, "card": {"grp_id": 4, "colors": []}},
    ]
    original_rows = [dict(row, card=dict(row["card"])) for row in rows]
    mode_changes: list[object] = []
    color_changes: list[object] = []
    model.filterModeChanged.connect(lambda: mode_changes.append(True))
    model.currentColorsChanged.connect(lambda: color_changes.append(True))

    model.replace(rows=rows, current_colors=["W"])
    assert model.filterMode == RecommendationListModel.ALL_FILTER_MODE
    assert model.rowCount() == len(rows)

    model.setFilterMode(RecommendationListModel.ON_COLOR_FILTER_MODE)
    assert model.filterMode == RecommendationListModel.ON_COLOR_FILTER_MODE
    assert [
        model.data(model.index(index, 0), RecommendationListModel.MODEL_DATA_ROLE)
        for index in range(model.rowCount())
    ] == [rows[0], rows[3]]
    assert rows == original_rows

    model.replace(rows=rows, current_colors=["U"])
    assert model.currentColors == ["U"]
    assert [
        model.data(model.index(index, 0), RecommendationListModel.MODEL_DATA_ROLE)
        for index in range(model.rowCount())
    ] == [rows[1], rows[3]]
    assert rows == original_rows

    model.replace(rows=rows, current_colors=[])
    assert model.currentColors == []
    assert [
        model.data(model.index(index, 0), RecommendationListModel.MODEL_DATA_ROLE)
        for index in range(model.rowCount())
    ] == [rows[3]]
    assert rows == original_rows

    model.setFilterMode(RecommendationListModel.ALL_FILTER_MODE)
    assert model.rowCount() == len(rows)
    assert [
        model.data(model.index(index, 0), RecommendationListModel.MODEL_DATA_ROLE)
        for index in range(model.rowCount())
    ] == rows
    assert mode_changes == [True, True]
    assert color_changes == [True, True, True]
    assert rows == original_rows


def _process_until(
    *,
    application: QCoreApplication,
    predicate: Callable[[], bool],
    description: str,
) -> None:
    deadline = time.monotonic() + 3.0
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            pytest.fail(f"Timed out waiting for {description}.")
        time.sleep(0.001)
    application.processEvents()


@pytest.mark.parametrize("force", [False, True])
def test_live_adapter_refreshes_profiles_off_worker_without_blocking_polling(
    qcore_application: QCoreApplication,
    force: bool,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_ProfileRefreshFakeSession] = []
    client = _ProfileRefreshFakeClient(
        result=ProfileRefreshResult(
            profile=SetProfile.generic(set_code="OTJ", event_format="QuickDraft"),
            outcome=ProfileRefreshOutcome.UPDATED,
            diagnostics=(),
        )
    )

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _ProfileRefreshFakeSession(publish=publish, force=force)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=5,
        profile_client=cast(ProfileClient, client),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions)
            and client.started.is_set()
            and len(sessions[0].poll_thread_ids) >= 2,
            description="profile refresh and continued live polling",
        )
        session = sessions[0]
        assert client.calls == [("OTJ", "QuickDraft", force)]
        assert client.thread_ids
        assert all(thread_id != gui_thread_id for thread_id in client.thread_ids)
        assert len(session.poll_thread_ids) >= 2
        client.release.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(session.completed_thread_ids),
            description="profile refresh completion",
        )
        assert session.completed_results
        assert session.completed_thread_ids[0] != gui_thread_id
        assert session.completed_thread_ids[0] != client.thread_ids[0]
        assert client.calls == [("OTJ", "QuickDraft", force)]
    finally:
        client.release.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_routes_profile_refresh_failure_to_session_worker(
    qcore_application: QCoreApplication,
) -> None:
    sessions: list[_ProfileRefreshFakeSession] = []
    client = _ProfileRefreshFakeClient(error=RuntimeError("profile network failed"))
    client.release.set()

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _ProfileRefreshFakeSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
        profile_client=cast(ProfileClient, client),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions) and bool(sessions[0].failed_thread_ids),
            description="profile refresh failure",
        )
        session = sessions[0]
        assert client.calls == [("OTJ", "QuickDraft", False)]
        assert session.failed_messages == ["profile network failed"]
        assert session.failed_thread_ids[0] != threading.get_ident()
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_shuts_down_cleanly_with_profile_refresh_in_flight(
    qcore_application: QCoreApplication,
) -> None:
    sessions: list[_ProfileRefreshFakeSession] = []
    client = _ProfileRefreshFakeClient(
        result=ProfileRefreshResult(
            profile=SetProfile.generic(set_code="OTJ", event_format="QuickDraft"),
            outcome=ProfileRefreshOutcome.UNCHANGED,
            diagnostics=(),
        )
    )

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _ProfileRefreshFakeSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
        profile_client=cast(ProfileClient, client),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=client.started.is_set,
            description="profile refresh before shutdown",
        )
        adapter.shutdown()
        assert adapter.thread is not None and adapter.thread.isRunning()
        client.release.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.thread is not None
            and not adapter.thread.isRunning(),
            description="profile refresh worker shutdown",
        )
    finally:
        client.release.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_hosted_profile_failure_retains_cached_authority(
    qcore_application: QCoreApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached_profile = _adapter_empirical_profile(
        profile_version="cached-1.0",
        generated_at="2026-08-29T00:00:00+00:00",
    )
    app_dir = tmp_path / "app"
    provider_calls: list[object] = []

    def guarded_provider_opener(
        request: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        provider_calls.append(request)
        raise AssertionError("hosted profile flow must not query direct providers")

    monkeypatch.setattr(
        "draftomen.carddb.urllib.request.urlopen",
        guarded_provider_opener,
    )
    monkeypatch.setattr(
        "draftomen.seventeen.urllib.request.urlopen",
        guarded_provider_opener,
    )
    attempted_urls: list[str] = []

    def unavailable_opener(request: object, *, timeout: float) -> object:
        del timeout
        attempted_urls.append(str(getattr(request, "full_url")))
        raise OSError("profile provider unavailable")

    client = ProfileClient(
        app_dir=app_dir,
        manifest_url=_PROFILE_MANIFEST_URL,
        network_policy=ProfileNetworkPolicy.ALLOWED,
        opener=unavailable_opener,
    )
    dump_set_profile(cached_profile, client.profile_path("TST", "QuickDraft"))
    sessions: list[LiveSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = LiveSession(
            log_path=tmp_path / "Player.log",
            app_dir=app_dir,
            card_database=CardDatabase(cards={}),
            profile_client=client,
            snapshot_publisher=publish,
        )
        sessions.append(session)
        session._set_active_set_code(set_code="TST")
        return session

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
        profile_client=client,
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: attempted_urls == [_PROFILE_MANIFEST_URL]
            and adapter.state.get("set_profile", {}).get("refresh_outcome")
            == ProfileRefreshOutcome.REMOTE_FAILED.value,
            description="the initial hosted profile failure",
        )
        state = adapter.state
        assert state["set_profile"]["profile_version"] == cached_profile.profile_version
        assert state["set_profile"]["source"] == "local-mature"
        assert state["set_profile"]["maturity"] == "mature"
        assert state["set_profile"]["phase"] == DataLoadPhase.FAILED.value
        assert state["ratings"]["phase"] == DataLoadPhase.READY.value
        assert state["recommendations"]["cards"] == []
        assert state["errors"] == []
        assert provider_calls == []

        adapter.requestRatings()
        _process_until(
            application=qcore_application,
            predicate=lambda: attempted_urls == [
                _PROFILE_MANIFEST_URL,
                _PROFILE_MANIFEST_URL,
            ]
            and bool(adapter.state.get("errors")),
            description="the forced hosted profile failure",
        )
        state = adapter.state
        assert state["set_profile"]["profile_version"] == cached_profile.profile_version
        assert state["set_profile"]["source"] == "local-mature"
        assert state["set_profile"]["phase"] == DataLoadPhase.FAILED.value
        assert state["set_profile"]["refresh_outcome"] == (
            ProfileRefreshOutcome.REMOTE_FAILED.value
        )
        assert state["ratings"]["phase"] == DataLoadPhase.READY.value
        assert state["errors"][0]["code"] == "ratings_unavailable"
        assert state["recommendations"]["cards"] == []
        assert provider_calls == []
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_ignores_stale_hosted_profile_completion_identity(
    qcore_application: QCoreApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached_profile = _adapter_empirical_profile(
        profile_version="cached-1.0",
        generated_at="2026-08-29T00:00:00+00:00",
    )
    stale_profile = _adapter_empirical_profile(
        profile_version="stale-2.0",
        generated_at="2026-08-30T00:00:00+00:00",
    )
    current_profile = _adapter_empirical_profile(
        profile_version="current-3.0",
        generated_at="2026-08-31T00:00:00+00:00",
    )
    stale_artifact_url = "https://profiles.example.test/stale.json.gz"
    current_artifact_url = "https://profiles.example.test/current.json.gz"
    stale_manifest, stale_artifact = _profile_transport_payload(
        stale_profile,
        artifact_url=stale_artifact_url,
        published_at="2026-08-30T01:00:00+00:00",
    )
    current_manifest, current_artifact = _profile_transport_payload(
        current_profile,
        artifact_url=current_artifact_url,
        published_at="2026-08-31T01:00:00+00:00",
    )
    first_refresh_started = threading.Event()
    release_first_refresh = threading.Event()
    second_refresh_started = threading.Event()
    release_second_refresh = threading.Event()
    provider_calls: list[object] = []

    def guarded_provider_opener(
        request: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        provider_calls.append(request)
        raise AssertionError("hosted profile flow must not query direct providers")

    monkeypatch.setattr(
        "draftomen.carddb.urllib.request.urlopen",
        guarded_provider_opener,
    )
    monkeypatch.setattr(
        "draftomen.seventeen.urllib.request.urlopen",
        guarded_provider_opener,
    )
    attempted_urls: list[str] = []

    def opener(request: object, *, timeout: float) -> _ProfileHttpResponse:
        del timeout
        url = str(getattr(request, "full_url"))
        attempted_urls.append(url)
        if url == _PROFILE_MANIFEST_URL:
            manifest_number = attempted_urls.count(_PROFILE_MANIFEST_URL)
            if manifest_number == 1:
                first_refresh_started.set()
                release_first_refresh.wait(timeout=3.0)
                return _ProfileHttpResponse(
                    payload=stale_manifest,
                    url=url,
                )
            second_refresh_started.set()
            release_second_refresh.wait(timeout=3.0)
            return _ProfileHttpResponse(
                payload=current_manifest,
                url=url,
            )
        if url == stale_artifact_url:
            return _ProfileHttpResponse(payload=stale_artifact, url=url)
        if url == current_artifact_url:
            return _ProfileHttpResponse(payload=current_artifact, url=url)
        raise AssertionError(f"unexpected profile provider URL: {url}")

    app_dir = tmp_path / "app"
    client = ProfileClient(
        app_dir=app_dir,
        manifest_url=_PROFILE_MANIFEST_URL,
        network_policy=ProfileNetworkPolicy.ALLOWED,
        opener=opener,
        manifest_ttl_seconds=0,
    )
    dump_set_profile(cached_profile, client.profile_path("TST", "QuickDraft"))
    sessions: list[LiveSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = LiveSession(
            log_path=tmp_path / "Player.log",
            app_dir=app_dir,
            card_database=CardDatabase(cards={}),
            profile_client=client,
            snapshot_publisher=publish,
        )
        sessions.append(session)
        session._set_active_set_code(set_code="TST")
        return session

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
        profile_client=client,
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=first_refresh_started.is_set,
            description="the first hosted profile refresh",
        )
        assert provider_calls == []
        session = sessions[0]
        adapter.requestRatings()
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                (request := session.profile_refresh_request()) is not None
                and request.force
            ),
            description="the forced profile refresh identity",
        )

        release_first_refresh.set()
        _process_until(
            application=qcore_application,
            predicate=second_refresh_started.is_set,
            description="the replacement hosted profile refresh",
        )
        state = adapter.state
        assert state["set_profile"]["profile_version"] == cached_profile.profile_version
        assert state["ratings"]["phase"] == DataLoadPhase.READY.value
        assert state["errors"] == []

        release_second_refresh.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state.get("set_profile", {}).get(
                "profile_version"
            )
            == current_profile.profile_version,
            description="the current hosted profile completion",
        )
        state = adapter.state
        assert state["set_profile"]["source"] == "remote"
        assert state["set_profile"]["refresh_outcome"] == (
            ProfileRefreshOutcome.UPDATED.value
        )
        assert provider_calls == []
        assert state["ratings"]["phase"] == DataLoadPhase.READY.value
        assert state["errors"] == []
        assert attempted_urls == [
            _PROFILE_MANIFEST_URL,
            stale_artifact_url,
            _PROFILE_MANIFEST_URL,
            current_artifact_url,
        ]
        assert provider_calls == []
    finally:
        release_first_refresh.set()
        release_second_refresh.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_runs_session_work_on_worker_and_queues_plain_snapshots(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_FakeSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _FakeSession(publish=publish)
        session.factory_thread_id = threading.get_ident()
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )
    assert adapter.thread is None
    observer = _StateObserver(adapter=adapter)
    adapter.stateChanged.connect(observer.observe)

    try:
        assert adapter.mockMode is False
        assert isinstance(adapter.scenario, str)
        assert isinstance(adapter.scenarios, list)

        adapter.start()
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions)
            and bool(sessions[0].startup_thread_ids)
            and bool(sessions[0].poll_thread_ids)
            and bool(adapter.state),
            description="the live session startup snapshot",
        )
        assert adapter.thread is not None
        assert adapter.thread.isRunning()

        session = sessions[0]
        assert session.factory_thread_id != gui_thread_id
        assert all(thread_id != gui_thread_id for thread_id in session.startup_thread_ids)
        assert all(thread_id != gui_thread_id for thread_id in session.poll_thread_ids)
        assert session.startup_options == [(True, True)]
        assert session.load_card_data_calls == 0
        assert observer.thread_ids
        assert set(observer.thread_ids) == {gui_thread_id}
        assert observer.states[-1] == adapter.state
        assert "domain_pool" not in adapter.state["build"]
        assert "domain_selection" not in adapter.state["build"]
    finally:
        adapter.shutdown()

    assert adapter.thread is not None
    assert not adapter.thread.isRunning()


def test_live_adapter_coalesces_historical_startup_snapshots(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_StartupReplaySession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _StartupReplaySession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=5,
    )
    observer = _StateObserver(adapter=adapter)
    adapter.stateChanged.connect(observer.observe)

    try:
        adapter.start()
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions)
            and sessions[0].post_start_ready.is_set(),
            description="the post-start live snapshot",
        )

        observed_card_counts = [
            len(cast(dict[str, object], state["recommendations"])["cards"])
            for state in observer.states
        ]
        assert observed_card_counts == [0, 1]
        assert observer.thread_ids
        assert set(observer.thread_ids) == {gui_thread_id}
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()

    assert adapter.thread is not None
    assert not adapter.thread.isRunning()


def test_live_adapter_retains_initial_poll_failure_until_recovery(
    qcore_application: QCoreApplication,
) -> None:
    sessions: list[_FailingFirstPollSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _FailingFirstPollSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=5,
    )

    try:
        adapter.start()
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(adapter.state.get("errors")),
            description="the initial poll error",
        )
        session = sessions[0]
        assert session.poll_count >= 1
        assert adapter.state["errors"][0]["message"] == "initial poll failed"
        assert adapter.state["status"]["phase"] == "error"

        session.recovery_allowed.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: session.recovered.is_set()
            and not adapter.state.get("errors"),
            description="a recovered polling snapshot",
        )
        assert adapter.state["status"]["phase"] != "error"
    finally:
        session = sessions[0] if sessions else None
        if session is not None:
            session.recovery_allowed.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()

    assert adapter.thread is not None
    assert not adapter.thread.isRunning()


def test_live_adapter_queues_explicit_commands_and_shutdown_is_safe(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_FakeSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _FakeSession(publish=publish)
        session.factory_thread_id = threading.get_ident()
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )

    assert adapter.thread is None
    adapter.shutdown()
    assert adapter.thread is None
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions) and bool(adapter.state),
            description="the live session initial state",
        )
        session = sessions[0]

        account_id = adapter.state["accounts"][0]["account_id"]

        adapter.chooseAccount(account_id)
        selected_grp_id = adapter.state["recommendations"]["cards"][1]["card"]["grp_id"]

        adapter.chooseRecommendation(selected_grp_id)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["recommendations"].get("selected_grp_id")
            == selected_grp_id,
            description="the selected recommendation snapshot",
        )

        adapter.changeRanking("win_rate")
        adapter.setSplashEnabled(False)
        adapter.setContextualScoringEnabled(False)
        adapter.requestRatings()
        adapter.requestBuild("BG")
        build_grp_id = adapter.state["build"]["spells"][0]["card"]["grp_id"]
        adapter.focusBuildCard(build_grp_id)
        adapter.requestBacktest()
        adapter.dismissError("missing-error")
        adapter.retryError("missing-error")
        _process_until(
            application=qcore_application,
            predicate=lambda: len(session.commands) == 11,
            description="all queued live session commands",
        )

        assert [type(command) for command in session.commands] == [
            ChooseAccount,
            ChooseRecommendation,
            ChangeRanking,
            ChangeSplashPreference,
            ChangeContextualScoring,
            RequestRatingsDownload,
            RequestBuild,
            FocusBuildCard,
            RequestBacktest,
            DismissError,
            RetryError,
        ]
        assert session.dispatch_thread_ids
        assert all(thread_id != gui_thread_id for thread_id in session.dispatch_thread_ids)
    finally:
        adapter.shutdown()

    assert adapter.thread is not None
    assert not adapter.thread.isRunning()


def test_live_adapter_fetches_card_images_on_worker_and_completes_on_session_worker(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_ImageFakeSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _ImageFakeSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )
    observer = _StateObserver(adapter=adapter)
    adapter.stateChanged.connect(observer.observe)
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions)
            and bool(sessions[0].completions)
            and adapter.state["card_image"]["phase"] == "ready",
            description="the worker-fetched card image snapshot",
        )
        session = sessions[0]
        assert adapter.state["card_image"]["image_path"] == (
            QUrl.fromLocalFile(str(session.completions[-1])).toString()
        )
        assert session.fetch_thread_ids
        assert session.result_thread_ids
        assert session.session_thread_ids
        assert set(session.result_thread_ids) <= set(session.session_thread_ids)
        assert set(session.fetch_thread_ids).isdisjoint(session.result_thread_ids)
        assert all(thread_id != gui_thread_id for thread_id in session.fetch_thread_ids)
        assert all(thread_id != gui_thread_id for thread_id in session.result_thread_ids)
        assert observer.thread_ids
        assert set(observer.thread_ids) == {gui_thread_id}
        assert observer.states[-1]["card_image"]["phase"] == "ready"
    finally:
        adapter.shutdown()

    assert sessions[0].stopped is True
    assert adapter.thread is not None
    assert not adapter.thread.isRunning()


def test_live_adapter_keeps_commands_responsive_during_focused_metadata_lookup(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_BlockedFocusedImageFakeSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _BlockedFocusedImageFakeSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions) and sessions[0].fetch_started.is_set(),
            description="the focused metadata image fetch",
        )
        session = sessions[0]
        adapter.chooseRecommendation(session.request.grp_id)
        _process_until(
            application=qcore_application,
            predicate=lambda: len(session.commands) == 1,
            description="a queued command during focused metadata lookup",
        )
        assert isinstance(session.commands[0], ChooseRecommendation)
        assert session.dispatch_thread_ids[0] != gui_thread_id
        assert session.fetch_thread_ids[0] != gui_thread_id
        assert session.dispatch_thread_ids[0] != session.fetch_thread_ids[0]
    finally:
        if sessions:
            sessions[0].fetch_release.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_surfaces_and_dismisses_worker_initialization_errors(
    qcore_application: QCoreApplication,
) -> None:
    def factory(publish: SnapshotPublisher) -> LiveSession:
        del publish
        raise RuntimeError("Live provider failed.")

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(adapter.state["errors"]),
            description="the live provider error",
        )

        assert adapter.state["errors"][0]["message"] == "Live provider failed."
        assert adapter.state["errors"][0]["recoverable"] is False

        adapter.dismissError("qt-worker-error")

        assert adapter.state["errors"] == []
    finally:
        adapter.shutdown()


def test_live_adapter_retains_startup_failure_when_stop_publishes(
    qcore_application: QCoreApplication,
) -> None:
    sessions: list[_FailingStartupSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _FailingStartupSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(adapter.state.get("errors"))
            and adapter.thread is not None
            and not adapter.thread.isRunning(),
            description="the startup scan failure and worker shutdown",
        )

        assert sessions[0].stopped is True
        assert adapter.state["errors"][0]["message"] == "startup scan failed"
        assert adapter.state["status"]["phase"] == "error"
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_gui_preferences_adapter_persists_display_choices_independently(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    adapter = GuiPreferencesAdapter(app_dir=tmp_path / "app")
    changes: list[bool] = []
    contextual_changes: list[bool] = []
    adapter.preferencesChanged.connect(lambda: changes.append(True))
    adapter.contextualAdjustmentsEnabledChanged.connect(
        contextual_changes.append
    )

    try:
        assert adapter.showBacktest is False
        assert adapter.contextualAdjustmentsEnabled is False
        adapter.setCompactDensity(False)
        adapter.setShowBacktest(False)
        adapter.setContextualAdjustmentsEnabled(False)
        assert changes == []
        assert contextual_changes == []
        adapter.setCompactDensity(True)
        adapter.setSecondaryStats(False)
        adapter.setCardPreview(False)
        adapter.setDetailedBuildContext(False)
        adapter.setSystemTextScaling(False)
        adapter.setShowBacktest(True)
        adapter.setContextualAdjustmentsEnabled(True)
        adapter.setContextualAdjustmentsEnabled(True)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.persistenceMessage == "Saved",
            description="the latest GUI preferences save",
        )
        reloaded = GuiPreferencesAdapter(app_dir=tmp_path / "app")

        assert changes == [True, True, True, True, True, True, True]
        assert contextual_changes == [True]
        assert adapter.persistenceMessage == "Saved"
        assert reloaded.compactDensity is True
        assert reloaded.secondaryStats is False
        assert reloaded.cardPreview is False
        assert reloaded.detailedBuildContext is False
        assert reloaded.systemTextScaling is False
        assert reloaded.showBacktest is True
        assert reloaded.contextualAdjustmentsEnabled is True
    finally:
        adapter.shutdown()

def test_gui_preferences_adapter_exposes_saving_and_ignores_stale_completion(
    qcore_application: QCoreApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_calls: list[GuiDisplayPreferences] = []
    first_save_started = threading.Event()
    release_first_save = threading.Event()

    def delayed_save(
        *,
        preferences: GuiDisplayPreferences,
        app_dir: str | PathLike[str] | None,
    ) -> str | None:
        del app_dir
        save_calls.append(preferences)
        if len(save_calls) == 1:
            first_save_started.set()
            assert release_first_save.wait(timeout=3.0)
            return "stale save failed"
        if len(save_calls) == 3:
            return "current save failed"
        return None

    monkeypatch.setattr("draftomen.qt_adapter.save_gui_preferences", delayed_save)
    adapter = GuiPreferencesAdapter(app_dir=tmp_path / "app")
    observed_messages: list[str] = []
    adapter.persistenceChanged.connect(
        lambda: observed_messages.append(adapter.persistenceMessage)
    )

    try:
        adapter.setCompactDensity(True)
        _process_until(
            application=qcore_application,
            predicate=first_save_started.is_set,
            description="the first preference save to start",
        )
        assert adapter.persistenceMessage == "Saving…"

        adapter.setSecondaryStats(False)
        assert adapter.persistenceMessage == "Saving…"
        release_first_save.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: len(save_calls) == 2
            and adapter.persistenceMessage == "Saved",
            description="the newest preference save to complete",
        )
        assert len(save_calls) == 2
        latest_preferences = save_calls[-1]
        assert latest_preferences.compact_density is True
        assert latest_preferences.secondary_stats is False
        assert "stale save failed" not in observed_messages
        adapter.setCardPreview(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.persistenceMessage == "current save failed",
            description="the current preference save failure",
        )
        assert adapter.persistenceMessage == "current save failed"
    finally:
        release_first_save.set()
        adapter.shutdown()


def test_gui_preferences_adapter_shutdown_drains_coalesced_contextual_save(
    qcore_application: QCoreApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_calls: list[GuiDisplayPreferences] = []
    first_save_started = threading.Event()
    release_first_save = threading.Event()

    def blocked_save(
        *,
        preferences: GuiDisplayPreferences,
        app_dir: str | PathLike[str] | None,
    ) -> str | None:
        save_calls.append(preferences)
        if len(save_calls) == 1:
            first_save_started.set()
            assert release_first_save.wait(timeout=3.0)
        return save_gui_preferences(preferences=preferences, app_dir=app_dir)

    monkeypatch.setattr("draftomen.qt_adapter.save_gui_preferences", blocked_save)
    adapter = GuiPreferencesAdapter(app_dir=tmp_path / "app")

    try:
        adapter.setContextualAdjustmentsEnabled(True)
        assert first_save_started.wait(timeout=3.0)
        adapter.setCompactDensity(True)
        adapter.setContextualAdjustmentsEnabled(False)
        assert adapter.contextualAdjustmentsEnabled is False
        release_first_save.set()
        adapter.shutdown()

        assert len(save_calls) == 2
        assert save_calls[-1].contextual_adjustments_enabled is False
        reloaded = GuiPreferencesAdapter(app_dir=tmp_path / "app")
        assert reloaded.contextualAdjustmentsEnabled is False
        assert reloaded.compactDensity is True
        reloaded.shutdown()
    finally:
        release_first_save.set()
        adapter.shutdown()


def test_live_adapter_keeps_selection_responsive_during_thumbnail_fetches(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_RecommendationImageFakeSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _RecommendationImageFakeSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions) and sessions[0].started.is_set(),
            description="the first recommendation image fetch",
        )
        session = sessions[0]
        selected_grp_id = session.requests[1].grp_id
        adapter.chooseRecommendation(selected_grp_id)
        _process_until(
            application=qcore_application,
            predicate=lambda: len(session.commands) == 1,
            description="a selection while an image fetch is blocked",
        )
        assert isinstance(session.commands[0], ChooseRecommendation)

        session.release.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: len(session.completed) == 2,
            description="all queued recommendation image fetches",
        )
        assert session.fetch_thread_ids
        assert session.result_thread_ids
        assert len(set(session.fetch_thread_ids)) == 1
        assert len(set(session.result_thread_ids)) == 1
        assert all(thread_id != gui_thread_id for thread_id in session.fetch_thread_ids)
        assert all(thread_id != gui_thread_id for thread_id in session.result_thread_ids)
        assert set(session.fetch_thread_ids).isdisjoint(session.result_thread_ids)
    finally:
        if sessions:
            sessions[0].release.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_shutdown_returns_while_startup_work_is_busy(
    qcore_application: QCoreApplication,
) -> None:
    sessions: list[_BusySession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _BusySession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions) and sessions[0].started.is_set(),
            description="busy startup work",
        )
        started_at = time.monotonic()
        adapter.shutdown()
        assert time.monotonic() - started_at < 0.5
        assert adapter.thread is not None and adapter.thread.isRunning()

        sessions[0].release.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.thread is not None
            and not adapter.thread.isRunning(),
            description="cooperative worker shutdown",
        )
        assert sessions[0].stopped is True
    finally:
        sessions[0].release.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_application_quit_releases_busy_worker_before_adapter_teardown(
    qcore_application: QCoreApplication,
) -> None:
    sessions: list[_BusySession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _BusySession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
    )
    adapter.start()
    _process_until(
        application=qcore_application,
        predicate=lambda: bool(sessions) and sessions[0].started.is_set(),
        description="busy startup work before application quit",
    )

    quit_requested = threading.Event()

    def release_after_quit() -> None:
        quit_requested.wait(timeout=1.0)
        sessions[0].release.set()

    def quit_application() -> None:
        quit_requested.set()
        qcore_application.quit()

    release_thread = threading.Thread(target=release_after_quit)
    release_thread.start()
    try:
        QTimer.singleShot(0, quit_application)
        started_at = time.monotonic()
        qcore_application.exec()
        adapter.shutdown()
        adapter.wait_for_shutdown()
        elapsed = time.monotonic() - started_at

        assert elapsed < 1.0
    finally:
        quit_requested.set()
        sessions[0].release.set()
        release_thread.join(timeout=1.0)
        adapter.shutdown()
        adapter.wait_for_shutdown()

def test_live_adapter_contextual_toggle_stays_local_with_production_session(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    image_calls: list[object] = []
    profile_calls: list[object] = []
    event_name = "QuickDraft_TST_20260829"
    pool_before_pick = (104976, 105080, 104995, 105027, 105030, 105170)

    def payload_line(*, payload: dict[str, object]) -> str:
        return json.dumps(
            {"CurrentModule": "BotDraft", "Payload": json.dumps(payload)}
        )

    def pack_line(
        *,
        pack_number: int,
        pick_number: int,
        draft_pack: tuple[int, ...],
        picked_cards: tuple[int, ...],
    ) -> str:
        return payload_line(
            payload={
                "Result": "Success",
                "EventName": event_name,
                "DraftStatus": "PickNext",
                "PackNumber": pack_number,
                "PickNumber": pick_number,
                "NumCardsToPick": 1,
                "DraftPack": [str(grp_id) for grp_id in draft_pack],
                "PickedCards": [str(grp_id) for grp_id in picked_cards],
            }
        )

    def pick_line(
        *,
        request_id: str,
        card_id: int,
        pack_number: int,
        pick_number: int,
    ) -> str:
        request = {
            "EventName": event_name,
            "PickInfo": {
                "EventName": event_name,
                "CardIds": [str(card_id)],
                "PackNumber": pack_number,
                "PickNumber": pick_number,
            },
        }
        envelope = {"id": request_id, "request": json.dumps(request)}
        return (
            "[UnityCrossThreadLogger]==> BotDraftDraftPick "
            f"{json.dumps(envelope)}"
        )

    fixture_lines = [
        json.dumps(
            {
                "authenticateResponse": {
                    "clientId": "FIXTURECLIENTID1234567890",
                    "screenName": "FixturePlayer",
                }
            }
        ),
        json.dumps(
            {
                "Course": {
                    "CourseId": "00000000-0000-4000-8000-000000000004",
                    "InternalEventName": event_name,
                    "CurrentModule": "BotDraft",
                }
            }
        ),
    ]
    for pick_index, picked_card in enumerate(pool_before_pick):
        pack_number, pick_number = divmod(pick_index, 14)
        fixture_lines.extend(
            (
                pack_line(
                    pack_number=pack_number,
                    pick_number=pick_number,
                    draft_pack=(picked_card,),
                    picked_cards=pool_before_pick[:pick_index],
                ),
                pick_line(
                    request_id=f"profiled-pick-{pick_index}",
                    card_id=picked_card,
                    pack_number=pack_number,
                    pick_number=pick_number,
                ),
            )
        )
    fixture_lines.append(
        payload_line(
            payload={
                "Result": "Success",
                "EventName": event_name,
                "DraftStatus": "PickNext",
                "PackNumber": 0,
                "PickNumber": len(pool_before_pick),
                "NumCardsToPick": 1,
                "DraftPack": [
                    "104894",
                    "104976",
                    "105080",
                    "104995",
                    "105027",
                    "105030",
                    "105170",
                    "104932",
                    "104893",
                    "105091",
                    "104969",
                    "105097",
                    "104979",
                    "105164",
                ],
                "PickedCards": [str(grp_id) for grp_id in pool_before_pick],
            }
        )
    )
    fixture_lines.append(
        pick_line(
            request_id="backtest-pick",
            card_id=104894,
            pack_number=0,
            pick_number=len(pool_before_pick),
        )
    )

    def fail_image_opener(request: object, timeout: float) -> object:
        del timeout
        image_calls.append(request)
        raise AssertionError("contextual scoring must not fetch card images")

    def fail_profile_opener(request: object, timeout: float) -> object:
        del timeout
        profile_calls.append(request)
        raise AssertionError("contextual scoring must not refresh profiles")

    card_database = CardDatabase(
        cards={
            grp_id: CardInfo(
                grp_id=grp_id,
                name=f"Fixture Card {grp_id}",
                colors=("W", "U"),
                mana_value=3.0,
                rarity="common",
                types=("Creature",),
                image_uri=f"https://images.example/{grp_id}.jpg",
                set_code="tst",
            )
            for grp_id in (
                104894,
                104976,
                105080,
                104995,
                105027,
                105030,
                105170,
                104932,
                104893,
                105091,
                104969,
                105097,
                104979,
                105164,
            )
        }
    )
    profile_client = ProfileClient(
        app_dir=tmp_path / "profiles",
        manifest_url=_PROFILE_MANIFEST_URL,
        opener=fail_profile_opener,
    )
    profile = _adapter_empirical_profile(
        profile_version="contextual-1.0",
        generated_at="2026-08-29T00:00:00+00:00",
    )
    profile = replace(
        profile,
        card_ratings=(
            replace(
                profile.card_ratings[0],
                gih_win_rate=replace(
                    profile.card_ratings[0].gih_win_rate,
                    raw_value=0.57,
                    value=0.57,
                ),
            ),
        ),
        role_profile=replace(
            profile.role_profile,
            cards=(
                replace(profile.role_profile.cards[0], key="grp_id:104894"),
            ),
        ),
    )
    dump_set_profile(
        profile,
        profile_client.profile_path("TST", "QuickDraft"),
    )
    image_service = CardImageService(
        cache_dir=tmp_path / "images",
        opener=fail_image_opener,
    )

    class _ControlledLiveSession(LiveSession):
        poll_count = 0
        contextual_toggle_count = 0
        external_work_ready = threading.Event()

        def dispatch(
            self,
            *,
            command: LiveSessionCommand,
        ) -> LiveSessionSnapshot:
            if isinstance(command, ChangeContextualScoring):
                self.contextual_toggle_count += 1
                if self.contextual_toggle_count == 3:
                    assert self._current_pack_event is not None
                    self._prepare_recommendation_image_requests(
                        pack=self._current_pack_event,
                        recommendations=self.snapshot.recommendations,
                    )
                    with self._state_lock:
                        self._queue_profile_refresh_locked(force=True)
                    self.external_work_ready.set()
            return super().dispatch(command=command)

        def poll_once(self) -> LiveSessionSnapshot:
            self.poll_count += 1
            if self.poll_count == 2:
                self.process_lines(
                    lines=(
                        pack_line(
                            pack_number=0,
                            pick_number=7,
                            draft_pack=(104894, 104976),
                            picked_cards=pool_before_pick + (104894,),
                        ),
                    )
                )
            return super().poll_once()

    sessions: list[_ControlledLiveSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _ControlledLiveSession(
            log_path=tmp_path / "Player.log",
            app_dir=tmp_path / "app",
            card_database=card_database,
            card_image_service=image_service,
            profile_client=profile_client,
            snapshot_publisher=publish,
        )
        original_network_policy = profile_client.network_policy
        profile_client.network_policy = ProfileNetworkPolicy.OFFLINE
        session._card_image_service = None
        try:
            session.process_lines(lines=fixture_lines)
        finally:
            session._card_image_service = image_service
            profile_client.network_policy = original_network_policy
        sessions.append(session)
        return session

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=60_000,
        startup_scan=False,
        profile_client=profile_client,
    )
    adapter.start()

    def recommendation_model_row(*, grp_id: int) -> dict[str, object]:
        model = adapter.recommendationsModel
        for index in range(model.rowCount()):
            row = model.data(
                model.index(index, 0),
                RecommendationListModel.MODEL_DATA_ROLE,
            )
            if not isinstance(row, dict):
                continue
            card = row.get("card")
            if isinstance(card, dict) and card.get("grp_id") == grp_id:
                return row
        raise AssertionError(f"Recommendation model is missing grp_id {grp_id}.")

    def assert_rationale_parity(
        *,
        expected: Recommendation,
    ) -> tuple[dict[str, object], dict[str, object]]:
        state_row = next(
            card
            for card in adapter.state["recommendations"]["cards"]
            if card["card"]["grp_id"] == expected.card.grp_id
        )
        model_row = recommendation_model_row(grp_id=expected.card.grp_id)
        for row in (state_row, model_row):
            assert row["concise_explanation"] == expected.concise_explanation
            assert row["explanation"] == expected.explanation
        return state_row, model_row

    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions)
            and bool(adapter.state["recommendations"]["cards"]),
            description="the production live-session recommendations",
        )
        initial_snapshot = sessions[0].snapshot
        initial_recommendation = next(
            recommendation
            for recommendation in initial_snapshot.recommendations.cards
            if recommendation.card.grp_id == 104894
        )
        assert initial_snapshot.contextual_adjustments_enabled is True
        assert initial_recommendation.contextual_pair == "WU"
        assert initial_recommendation.contextual_evidence
        assert initial_recommendation.contextual_breakdown.aggregate > 0
        initial_state_recommendation, initial_model_recommendation = (
            assert_rationale_parity(expected=initial_recommendation)
        )
        initial_state_score = initial_state_recommendation["score"]

        adapter.requestBacktest()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state.get("backtest") is not None,
            description="the enabled production backtest",
        )
        enabled_backtest = adapter.state["backtest"]
        assert enabled_backtest is not None
        enabled_backtest_row = next(
            row
            for row in enabled_backtest["rows"]
            if row["recommended"]["grp_id"] == 104894
        )
        assert enabled_backtest_row["recommended_score"] is not None
        assert enabled_backtest_row["contextual_evidence"]
        image_calls_after_enabled_backtest = len(image_calls)
        profile_calls_after_enabled_backtest = len(profile_calls)

        adapter.setContextualScoringEnabled(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                bool(sessions)
                and sessions[0].snapshot.contextual_adjustments_enabled is False
                and adapter.state.get("backtest") is None
            ),
            description="the queued contextual-scoring disable",
        )
        for _ in range(5):
            qcore_application.processEvents()
        disabled_recommendation = next(
            recommendation
            for recommendation in sessions[0].snapshot.recommendations.cards
            if recommendation.card.grp_id == 104894
        )
        disabled_state_recommendation, disabled_model_recommendation = (
            assert_rationale_parity(expected=disabled_recommendation)
        )
        assert disabled_recommendation.contextual_evidence == ()
        assert disabled_recommendation.contextual_breakdown.aggregate == 0
        assert disabled_recommendation.score < initial_recommendation.score
        assert disabled_state_recommendation["score"] < initial_state_score
        assert (
            disabled_recommendation.concise_explanation
            != initial_recommendation.concise_explanation
        )
        assert disabled_recommendation.explanation != initial_recommendation.explanation
        assert (
            disabled_model_recommendation["concise_explanation"]
            != initial_model_recommendation["concise_explanation"]
        )
        assert (
            disabled_model_recommendation["explanation"]
            != initial_model_recommendation["explanation"]
        )
        assert len(image_calls) == image_calls_after_enabled_backtest
        assert len(profile_calls) == profile_calls_after_enabled_backtest

        adapter.requestBacktest()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state.get("backtest") is not None,
            description="the disabled production backtest",
        )
        disabled_backtest = adapter.state["backtest"]
        assert disabled_backtest is not None
        disabled_backtest_row = next(
            row
            for row in disabled_backtest["rows"]
            if row["recommended"]["grp_id"] == 104894
        )
        assert disabled_backtest_row["recommended_score"] is not None
        assert (
            disabled_backtest_row["recommended_score"]
            < enabled_backtest_row["recommended_score"]
        )
        assert disabled_backtest_row["contextual_evidence"] == []
        image_calls_after_disabled_backtest = len(image_calls)
        profile_calls_after_disabled_backtest = len(profile_calls)

        adapter.setContextualScoringEnabled(True)
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                bool(sessions)
                and sessions[0].snapshot.contextual_adjustments_enabled is True
                and adapter.state.get("backtest") is None
            ),
            description="the queued contextual-scoring enable",
        )
        enabled_recommendation = next(
            recommendation
            for recommendation in sessions[0].snapshot.recommendations.cards
            if recommendation.card.grp_id == 104894
        )
        enabled_state_recommendation, enabled_model_recommendation = (
            assert_rationale_parity(expected=enabled_recommendation)
        )
        assert enabled_recommendation.contextual_evidence
        assert enabled_recommendation.contextual_breakdown.aggregate > 0
        assert enabled_recommendation.score > disabled_recommendation.score
        assert (
            enabled_state_recommendation["score"]
            > disabled_state_recommendation["score"]
        )
        assert (
            enabled_recommendation.concise_explanation
            != disabled_recommendation.concise_explanation
        )
        assert enabled_recommendation.explanation != disabled_recommendation.explanation
        assert (
            enabled_model_recommendation["concise_explanation"]
            != disabled_model_recommendation["concise_explanation"]
        )
        assert (
            enabled_model_recommendation["explanation"]
            != disabled_model_recommendation["explanation"]
        )
        image_calls_after_enable = len(image_calls)
        profile_calls_after_enable = len(profile_calls)
        assert len(image_calls) == image_calls_after_disabled_backtest
        assert len(profile_calls) == profile_calls_after_disabled_backtest

        adapter.setContextualScoringEnabled(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                bool(sessions)
                and sessions[0].snapshot.contextual_adjustments_enabled is False
                and adapter.state.get("backtest") is None
                and sessions[0].external_work_ready.is_set()
                and sessions[0].recommendation_image_request() is not None
                and sessions[0].profile_refresh_request() is not None
            ),
            description="the queued contextual-scoring disable",
        )
        assert len(image_calls) == image_calls_after_enable
        assert len(profile_calls) == profile_calls_after_enable
        worker = adapter._worker
        assert worker is not None
        image_calls_before_later_poll = len(image_calls)

        QMetaObject.invokeMethod(
            worker,
            "_poll",
            Qt.ConnectionType.QueuedConnection,
        )
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                bool(sessions)
                and sessions[0].snapshot.current_pack_event is not None
                and sessions[0].snapshot.current_pack_event.pack_number == 0
                and sessions[0].snapshot.current_pack_event.pick_number == 7
                and len(image_calls) > image_calls_before_later_poll
            ),
            description="the controlled later production poll result",
        )
        assert sessions[0].snapshot.contextual_adjustments_enabled is False
        later_recommendation = next(
            recommendation
            for recommendation in sessions[0].snapshot.recommendations.cards
            if recommendation.card.grp_id == 104894
        )
        assert later_recommendation.contextual_evidence == ()
        assert later_recommendation.contextual_breakdown.aggregate == 0
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()
