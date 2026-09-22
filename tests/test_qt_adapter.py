from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, cast

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

from draftomen.augmented_model_client import (
    AugmentedModelClient,
    AugmentedModelLoad,
    AugmentedModelOutcome,
)
from draftomen.carddb import (
    CardDatabase,
    CardDatabaseError,
    CardInfo,
    build_card_database_from_bulk_file,
)
from draftomen.cardimages import CardImageService
from draftomen.mock_session import MockLiveSession
from draftomen.pool_ledger import evaluate_completed_pool_role_ledger
from draftomen.preferences import (
    GuiDisplayPreferences,
    gui_preferences_path,
    save_gui_preferences,
)
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
    AugmentationState,
    AugmentationStatus,
    CardDataState,
    CardImageFetchResult,
    CardImageRequest,
    CardImageState,
    CardView,
    ChangeAiEnhancedSuggestions,
    ChangeAugmentation,
    ChangeContextualScoring,
    ChangeRanking,
    ChangeSplashPreference,
    ChooseAccount,
    ChooseRecommendation,
    DataLoadPhase,
    DismissError,
    EnhancementAvailabilityState,
    EnhancementAvailabilityStatus,
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
    RateEstimate,
    SetProfile,
    dump_set_profile,
    load_set_profile,
)
from draftomen.test_draft import (
    TestDraftController,
    TestDraftError,
    TestDraftInspection,
    TestDraftOfferIdentity,
    TestDraftRuntime,
    create_test_draft_runtime,
)

from tests.augmented_artifacts import augmented_artifact
from tests.test_draftmancer import _FakeSocket, _withheld_ack_action
from tests.test_test_draft import (
    _HELPER_GRP_IDS,
    _HELPER_OFFERS,
    _HelperSources,
    _arena_states,
    _helper_socket,
    _pick_card_calls,
    _seed_helper_sources,
)

if TYPE_CHECKING:
    # Typing only: importing the protocol at runtime makes pytest try to
    # collect it as a test class and warn on every run.
    from draftomen.qt_adapter import TestDraftFactory


_PROFILE_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "set-profiles" / "mature.json"
)
_AUGMENTED_FIXTURE_LOG_PATH = (
    Path(__file__).parent / "fixtures" / "quick-draft-msh-player.log"
)
_SCRYFALL_BULK_SAMPLE_PATH = (
    Path(__file__).parent / "fixtures" / "scryfall-default-cards-sample.jsonl"
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
        set_profile_schema_version=profile.schema_version,
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


def test_session_adapter_translates_enhancement_availability_to_plain_qml_values() -> None:
    adapter = SessionAdapter(
        snapshot=LiveSessionSnapshot(
            enhancement_availability=EnhancementAvailabilityState(
                status=EnhancementAvailabilityStatus.AVAILABLE,
                set_code="OTJ",
                enabled=True,
                message="AI-enhanced suggestions available for OTJ.",
            )
        )
    )

    availability = adapter.state["enhancement_availability"]
    assert availability == {
        "status": "available",
        "set_code": "OTJ",
        "enabled": True,
        "message": "AI-enhanced suggestions available for OTJ.",
    }
    assert not isinstance(availability, EnhancementAvailabilityState)
    assert not isinstance(availability["status"], EnhancementAvailabilityStatus)


class _RecordingSessionAdapter(SessionAdapter):
    """Record the commands one QML provider dispatches."""

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[LiveSessionCommand] = []

    def _dispatch(self, *, command: LiveSessionCommand) -> None:
        self.commands.append(command)


def test_session_adapter_dispatches_one_augmentation_toggle() -> None:
    adapter = _RecordingSessionAdapter()

    adapter.setAugmentedIntelligenceEnabled(True)

    assert adapter.commands == [ChangeAugmentation(enabled=True)]


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
        adapter.setAiEnhancedSuggestionsEnabled(False)
        adapter.requestRatings()
        adapter.requestBuild("BG")
        build_grp_id = adapter.state["build"]["spells"][0]["card"]["grp_id"]
        adapter.focusBuildCard(build_grp_id)
        adapter.requestBacktest()
        adapter.dismissError("missing-error")
        adapter.retryError("missing-error")
        _process_until(
            application=qcore_application,
            predicate=lambda: len(session.commands) == 12,
            description="all queued live session commands",
        )
        assert [type(command) for command in session.commands] == [
            ChooseAccount,
            ChooseRecommendation,
            ChangeRanking,
            ChangeSplashPreference,
            ChangeContextualScoring,
            ChangeAiEnhancedSuggestions,
            RequestRatingsDownload,
            RequestBuild,
            FocusBuildCard,
            RequestBacktest,
            DismissError,
            RetryError,
        ]
        assert session.commands[5] == ChangeAiEnhancedSuggestions(enabled=False)
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


def test_gui_preferences_adapter_persists_augmented_intelligence_toggle(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    adapter = GuiPreferencesAdapter(app_dir=tmp_path / "app")
    augmented_changes: list[bool] = []
    adapter.augmentedIntelligenceEnabledChanged.connect(augmented_changes.append)
    reloaded: GuiPreferencesAdapter | None = None

    try:
        assert adapter.augmentedIntelligenceEnabled is False
        assert adapter.contextualAdjustmentsEnabled is False
        adapter.setAugmentedIntelligenceEnabled(False)
        assert augmented_changes == []
        adapter.setAugmentedIntelligenceEnabled(True)
        adapter.setAugmentedIntelligenceEnabled(True)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.persistenceMessage == "Saved",
            description="the augmented intelligence preference save",
        )
        reloaded = GuiPreferencesAdapter(app_dir=tmp_path / "app")

        assert adapter.augmentedIntelligenceEnabled is True
        assert augmented_changes == [True]
        assert reloaded.augmentedIntelligenceEnabled is True
        assert reloaded.contextualAdjustmentsEnabled is False
    finally:
        adapter.shutdown()
        if reloaded is not None:
            reloaded.shutdown()


def test_gui_preferences_adapter_persists_mocked_draft_toggle_once(
    qcore_application: QCoreApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_calls: list[GuiDisplayPreferences] = []

    def recording_save(
        *,
        preferences: GuiDisplayPreferences,
        app_dir: str | PathLike[str] | None,
    ) -> str | None:
        save_calls.append(preferences)
        return save_gui_preferences(preferences=preferences, app_dir=app_dir)

    monkeypatch.setattr("draftomen.qt_adapter.save_gui_preferences", recording_save)
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True)
    # A schema v1 file written before the Mocked Draft setting existed.
    gui_preferences_path(app_dir=app_dir).write_text(
        json.dumps({"version": 1, "display": {"secondary_stats": False}}),
        encoding="utf-8",
    )
    adapter = GuiPreferencesAdapter(app_dir=app_dir)
    changes: list[bool] = []
    adapter.mockedDraftEnabledChanged.connect(changes.append)

    try:
        assert adapter.mockedDraftEnabled is False
        assert adapter.secondaryStats is False
        assert adapter.preferences.mocked_draft_checkout_dir == ""
        assert adapter.preferences.mocked_draft_server_url == ""

        adapter.setMockedDraftEnabled(True)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.persistenceMessage == "Saved",
            description="the persisted Mocked Draft setting",
        )
        assert changes == [True]
        assert len(save_calls) == 1
        assert save_calls[0].mocked_draft_enabled is True

        adapter.setMockedDraftEnabled(True)
        repeat_deadline = time.monotonic() + 0.1
        while time.monotonic() < repeat_deadline:
            qcore_application.processEvents()
            time.sleep(0.001)
        assert changes == [True]
        assert len(save_calls) == 1

        reloaded = GuiPreferencesAdapter(app_dir=app_dir)
        try:
            assert reloaded.mockedDraftEnabled is True
        finally:
            reloaded.shutdown()
    finally:
        adapter.shutdown()


def test_gui_preferences_adapter_persists_mocked_draft_sources_and_notifies(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True)
    adapter = GuiPreferencesAdapter(app_dir=app_dir)
    source_changes: list[None] = []
    enablement_changes: list[bool] = []
    adapter.mockedDraftSourcesChanged.connect(lambda: source_changes.append(None))
    adapter.mockedDraftEnabledChanged.connect(enablement_changes.append)

    try:
        from draftomen.test_draft import (
            DEFAULT_TEST_DRAFT_SERVER_URL,
            default_test_draft_bulk_file,
            default_test_draft_checkout_dir,
        )

        assert adapter.mockedDraftCheckoutDir == str(
            default_test_draft_checkout_dir(app_dir=app_dir)
        )
        assert adapter.mockedDraftServerUrl == DEFAULT_TEST_DRAFT_SERVER_URL
        assert adapter.mockedDraftScryfallBulkFile == str(
            default_test_draft_bulk_file(app_dir=app_dir)
        )

        adapter.setMockedDraftCheckoutDir("  /opt/Draftmancer  ")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.persistenceMessage == "Saved",
            description="the persisted Mocked Draft checkout",
        )
        assert adapter.mockedDraftCheckoutDir == "/opt/Draftmancer"
        assert len(source_changes) == 1
        assert enablement_changes == []

        reloaded = GuiPreferencesAdapter(app_dir=app_dir)
        try:
            assert reloaded.mockedDraftCheckoutDir == "/opt/Draftmancer"
            assert reloaded.mockedDraftServerUrl == DEFAULT_TEST_DRAFT_SERVER_URL
            assert reloaded.mockedDraftScryfallBulkFile == str(
                default_test_draft_bulk_file(app_dir=app_dir)
            )
        finally:
            reloaded.shutdown()

        adapter.setMockedDraftCheckoutDir("/opt/Draftmancer")
        repeat_deadline = time.monotonic() + 0.1
        while time.monotonic() < repeat_deadline:
            qcore_application.processEvents()
            time.sleep(0.001)
        assert len(source_changes) == 1
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


class _StubAugmentedModelClient(AugmentedModelClient):
    """Serve one validated augmentation artifact without cache or network work."""

    def __init__(self, *, app_dir: Path) -> None:
        super().__init__(app_dir=app_dir)
        self.requested_set_codes: list[str] = []
        self.thread_ids: list[int] = []

    def load(self, set_code: str, *, allow_network: bool) -> AugmentedModelLoad:
        assert allow_network is True
        self.requested_set_codes.append(set_code)
        self.thread_ids.append(threading.get_ident())
        return AugmentedModelLoad(
            outcome=AugmentedModelOutcome.DOWNLOADED,
            set_code=set_code,
            artifact=augmented_artifact(set_code="msh"),
        )


def test_live_adapter_loads_the_available_augmentation_model_off_gui_thread(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    """The adapter owns the per-set model load and publishes its availability."""

    gui_thread_id = threading.get_ident()
    fixture_lines = _AUGMENTED_FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    client = _StubAugmentedModelClient(app_dir=tmp_path / "app")
    sessions: list[LiveSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = LiveSession(
            log_path=tmp_path / "Player.log",
            app_dir=tmp_path / "app",
            card_database=build_card_database_from_bulk_file(
                path=_SCRYFALL_BULK_SAMPLE_PATH
            ),
            augmentation_enabled=False,
            augmented_model_client=client,
            poll_interval=0.01,
            snapshot_publisher=publish,
        )
        session.process_lines(lines=fixture_lines[:7])
        sessions.append(session)
        return session

    adapter = LiveSessionAdapter(
        session_factory=factory,
        poll_interval_ms=5,
        startup_scan=False,
        augmented_model_client=client,
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state.get("augmentation", {}).get("status")
            == "available",
            description="the adapter-owned augmentation model load",
        )
        assert client.requested_set_codes == ["MSH"]
        assert all(thread_id != gui_thread_id for thread_id in client.thread_ids)
        assert sessions[0].snapshot.augmentation == AugmentationState(
            status=AugmentationStatus.AVAILABLE,
            set_code="MSH",
            enabled=False,
        )
        assert adapter.state["augmentation"] == {
            "status": "available",
            "set_code": "MSH",
            "enabled": False,
        }
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


# --------------------------------------------------------------------------
# Test Draft worker lifecycle (issue #547). The doubles below stay at module
# level for the manual/auto/leave/shutdown acceptance tests appended next:
#   _RecordingTestDraftFactory         records capability and runtime requests
#   _FakeTestDraftController           scripts start/inspect/confirm/run_auto
#   _FakeTestDraftRuntime              owns one simulated session and controller
#   _FakeTestDraftSession              duck-typed simulated source-less session
#   _BlockedArenaAuxiliaryFakeSession  blocked Arena image and profile work
#   _GuiTickCounter                    counts GUI-thread timer ticks
#   _test_draft_session_snapshot()     simulated snapshot unlike Arena state
#   _test_draft_inspection()           real inspection for the fake controller
#   _test_draft_offer()                real offer token for the fake controller
#   _DISABLED_TEST_DRAFT_STATE         published no-opt-in provider contract
# A factory that needs a fresh runtime per call can pass `runtime_factory`,
# called with the same keywords as `create_runtime`, and read the runtime
# publisher from `publishers` to publish simulated snapshots by hand.
# --------------------------------------------------------------------------


_DISABLED_TEST_DRAFT_STATE: dict[str, object] = {
    "enabled": False,
    "active": False,
    "phase": "idle",
    "mode": None,
    "set_code": None,
    "supported_set_codes": [],
    "default_set_code": None,
    "pending": False,
    "offer_generation": 0,
    "error": None,
    "bulk_file_missing": False,
    "bulk_file_downloading": False,
    "bulk_file_download_percent": None,
}
_SIMULATED_POOL_TOTAL_CARDS = 7


def _test_draft_offer(
    *,
    pack_number: int = 1,
    pick_number: int = 1,
) -> TestDraftOfferIdentity:
    """Build the real offer token one simulated pack is confirmed through."""
    return TestDraftOfferIdentity(
        account_id=None,
        event_name="Test Draft HOB",
        set_code="hob",
        pack_number=pack_number,
        pick_number=pick_number,
        offered_grp_ids=(1, 2, 3),
        pool_grp_ids=(),
    )


def _test_draft_session_snapshot(
    *,
    total_cards: int = _SIMULATED_POOL_TOTAL_CARDS,
) -> LiveSessionSnapshot:
    """Build one simulated draft snapshot that differs from Arena state."""
    ready = MockLiveSession().snapshot
    return replace(
        ready,
        pool=replace(ready.pool, total_cards=total_cards),
    )


def _test_draft_inspection(
    *,
    snapshot: LiveSessionSnapshot | None = None,
    offer: TestDraftOfferIdentity | None = None,
) -> TestDraftInspection:
    """Pair one ready simulated offer with the snapshot that published it."""
    return TestDraftInspection(
        offer=_test_draft_offer() if offer is None else offer,
        snapshot=(
            _test_draft_session_snapshot() if snapshot is None else snapshot
        ),
    )


class _FakeTestDraftSession:
    """Stand in for the source-less session inside one simulated runtime."""

    def __init__(
        self,
        *,
        snapshot: LiveSessionSnapshot | None = None,
    ) -> None:
        self.snapshot = (
            _test_draft_session_snapshot() if snapshot is None else snapshot
        )
        self.commands: list[LiveSessionCommand] = []
        self.dispatch_thread_ids: list[int] = []
        self.image_selection_thread_ids: list[int] = []
        self.stopped = False

    def selected_card_image_request(self) -> CardImageRequest | None:
        self.image_selection_thread_ids.append(threading.get_ident())
        return None

    def dispatch(self, *, command: LiveSessionCommand) -> LiveSessionSnapshot:
        self.dispatch_thread_ids.append(threading.get_ident())
        self.commands.append(command)
        return self.snapshot

    def stop(self) -> LiveSessionSnapshot:
        self.stopped = True
        return self.snapshot


class _FakeTestDraftController:
    """Script the simulated controller calls run on the live worker thread."""

    def __init__(
        self,
        *,
        inspection: TestDraftInspection | None = None,
        inspections: Iterable[TestDraftInspection] = (),
        confirm: Callable[..., object] | None = None,
        run_auto: Callable[[], object] | None = None,
        start_blocks: bool = False,
    ) -> None:
        self.inspection = (
            _test_draft_inspection() if inspection is None else inspection
        )
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self.start_calls = 0
        self.start_thread_ids: list[int] = []
        self.inspect_calls = 0
        self.confirm_calls: list[tuple[int, TestDraftOfferIdentity]] = []
        self.run_auto_calls = 0
        self._inspections = deque(inspections)
        self._confirm = confirm
        self._run_auto = run_auto
        if not start_blocks:
            self.release.set()

    def start(self) -> TestDraftInspection:
        self.start_calls += 1
        self.start_thread_ids.append(threading.get_ident())
        self.started.set()
        self.release.wait(timeout=3.0)
        if self.cancelled.is_set():
            raise TestDraftError(
                "the simulated draft did not start: cancelled.",
                stage="startup",
            )
        return self.inspection

    def inspect(self) -> TestDraftInspection:
        self.inspect_calls += 1
        if self._inspections:
            return self._inspections.popleft()
        return self.inspection

    def confirm(
        self,
        *,
        grp_id: int,
        expected_offer: TestDraftOfferIdentity,
    ) -> object:
        self.confirm_calls.append((grp_id, expected_offer))
        if self._confirm is None:
            raise AssertionError("the simulated confirm call is not scripted.")
        return self._confirm(grp_id=grp_id, expected_offer=expected_offer)

    def run_auto(self) -> object:
        self.run_auto_calls += 1
        if self._run_auto is None:
            raise AssertionError("the simulated run_auto call is not scripted.")
        return self._run_auto()

    def cancel(self) -> None:
        self.cancelled.set()
        self.release.set()


class _FakeTestDraftRuntime:
    """Own one scripted simulated source the worker treats as a runtime."""

    def __init__(
        self,
        *,
        session: _FakeTestDraftSession | None = None,
        controller: _FakeTestDraftController | None = None,
    ) -> None:
        self.session = (
            _FakeTestDraftSession() if session is None else session
        )
        self.controller = (
            _FakeTestDraftController() if controller is None else controller
        )
        self.cancel_calls = 0
        self.close_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.controller.cancel()

    def close(self) -> None:
        self.close_calls += 1


class _RecordingTestDraftFactory:
    """Record the capability, server, runtime, and bulk-download requests."""

    def __init__(
        self,
        *,
        set_codes: tuple[str, ...] = (),
        runtime: object | None = None,
        runtime_factory: Callable[..., object] | None = None,
        create_error: Exception | None = None,
        supported_error: Exception | None = None,
        server_url: str = "http://127.0.0.1:4111",
        ensure_error: Exception | None = None,
        ensure_blocker: threading.Event | None = None,
        bulk_file_missing_value: bool = False,
        download_error: Exception | None = None,
        download_blocker: threading.Event | None = None,
        download_progress: tuple[int, int | None] | None = None,
    ) -> None:
        self.set_codes = set_codes
        self.runtime = runtime
        self.runtime_factory = runtime_factory
        self.create_error = create_error
        self.supported_error = supported_error
        self.server_url = server_url
        self.ensure_error = ensure_error
        self.ensure_blocker = ensure_blocker
        self.bulk_file_missing_value = bulk_file_missing_value
        self.download_error = download_error
        self.download_blocker = download_blocker
        self.download_progress = download_progress
        self.supported_calls = 0
        self.supported_thread_ids: list[int] = []
        self.ensure_calls = 0
        self.ensure_thread_ids: list[int] = []
        self.release_calls = 0
        self.create_calls: list[dict[str, object]] = []
        self.create_thread_ids: list[int] = []
        self.download_calls = 0
        self.download_thread_ids: list[int] = []
        self.publishers: list[SnapshotPublisher] = []

    def supported_set_codes(self) -> tuple[str, ...]:
        self.supported_calls += 1
        self.supported_thread_ids.append(threading.get_ident())
        if self.supported_error is not None:
            raise self.supported_error
        return self.set_codes

    def bulk_file_missing(self) -> bool:
        """Report the configured Scryfall bulk-file availability."""
        return self.bulk_file_missing_value

    def download_bulk_file(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        """Record one bulk download the live worker requested."""
        del should_stop
        self.download_calls += 1
        self.download_thread_ids.append(threading.get_ident())
        if progress is not None and self.download_progress is not None:
            progress(*self.download_progress)
        if self.download_blocker is not None:
            self.download_blocker.wait(timeout=3.0)
        if self.download_error is not None:
            raise self.download_error
        return Path("scryfall-default-cards.jsonl.gz")

    def ensure_server(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> str:
        """Record one server request and answer the configured test URL."""
        del should_stop
        self.ensure_calls += 1
        self.ensure_thread_ids.append(threading.get_ident())
        if self.ensure_blocker is not None:
            self.ensure_blocker.wait(timeout=3.0)
        if self.ensure_error is not None:
            raise self.ensure_error
        return self.server_url

    def release_server(self) -> None:
        """Record one server release the live worker makes."""
        self.release_calls += 1

    def create_runtime(
        self,
        *,
        server_url: str,
        set_code: str,
        publisher: SnapshotPublisher,
        splash_enabled: bool,
        contextual_adjustments_enabled: bool,
        ai_enhanced_suggestions_enabled: bool,
    ) -> object:
        self.create_calls.append(
            {
                "server_url": server_url,
                "set_code": set_code,
                "splash_enabled": splash_enabled,
                "contextual_adjustments_enabled": (
                    contextual_adjustments_enabled
                ),
                "ai_enhanced_suggestions_enabled": (
                    ai_enhanced_suggestions_enabled
                ),
            }
        )
        self.create_thread_ids.append(threading.get_ident())
        self.publishers.append(publisher)
        if self.create_error is not None:
            raise self.create_error
        if self.runtime_factory is not None:
            return self.runtime_factory(
                server_url=server_url,
                set_code=set_code,
                publisher=publisher,
                splash_enabled=splash_enabled,
                contextual_adjustments_enabled=contextual_adjustments_enabled,
                ai_enhanced_suggestions_enabled=(
                    ai_enhanced_suggestions_enabled
                ),
            )
        if self.runtime is None:
            raise AssertionError("the test-draft factory has no runtime.")
        return self.runtime


class _BlockedArenaAuxiliaryFakeSession(_BlockedFocusedImageFakeSession):
    """Hold one Arena image fetch and one Arena profile refresh in flight."""

    def __init__(self, *, publish: SnapshotPublisher) -> None:
        super().__init__(publish=publish)
        self.profile_request = ProfileRefreshRequest(
            generation=1,
            set_code="OTJ",
            event_format="QuickDraft",
            force=False,
        )
        self.fetch_returned = threading.Event()
        self.completed_profile_requests: list[ProfileRefreshRequest] = []
        self.failed_profile_requests: list[ProfileRefreshRequest] = []

    def fetch_card_image(
        self,
        *,
        request: CardImageRequest,
    ) -> CardImageFetchResult:
        result = super().fetch_card_image(request=request)
        self.fetch_returned.set()
        return result

    def profile_refresh_request(self) -> ProfileRefreshRequest | None:
        return self.profile_request

    def complete_profile_refresh(
        self,
        *,
        request: ProfileRefreshRequest,
        result: object,
    ) -> None:
        del result
        self.completed_profile_requests.append(request)
        self.profile_request = None

    def fail_profile_refresh(
        self,
        *,
        request: ProfileRefreshRequest,
        error_message: str | None = None,
    ) -> None:
        del error_message
        self.failed_profile_requests.append(request)
        self.profile_request = None


class _GuiTickCounter(QObject):
    """Count GUI-thread timer ticks while the worker stays busy elsewhere."""

    def __init__(self, *, interval_ms: int = 1) -> None:
        super().__init__()
        self.ticks = 0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @Slot()
    def _tick(self) -> None:
        self.ticks += 1

    def stop(self) -> None:
        self._timer.stop()


def test_live_adapter_without_test_draft_opt_in_ignores_test_draft_commands(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_FakeSession] = []

    def factory(publish: SnapshotPublisher) -> LiveSession:
        session = _FakeSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(session_factory=factory, poll_interval_ms=5)
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions)
            and len(sessions[0].poll_thread_ids) >= 2,
            description="polling without a configured Test Draft",
        )
        session = sessions[0]
        assert adapter.state["test_draft"] == _DISABLED_TEST_DRAFT_STATE
        assert all(
            thread_id != gui_thread_id for thread_id in session.poll_thread_ids
        )

        polls_before_commands = len(session.poll_thread_ids)
        adapter.startTestDraft("manual", "hob")
        adapter.pickTestDraft(1, 1)
        adapter.leaveTestDraft()
        _process_until(
            application=qcore_application,
            predicate=lambda: len(session.poll_thread_ids)
            >= polls_before_commands + 2,
            description="polling after ignored Test Draft commands",
        )
        assert adapter.state["test_draft"] == _DISABLED_TEST_DRAFT_STATE
        assert session.commands == []
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_publishes_test_draft_support_and_default_set(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()

    def build_adapter(
        test_draft_factory: _RecordingTestDraftFactory,
    ) -> tuple[LiveSessionAdapter, list[_FakeSession]]:
        """Start one live adapter over a recording Arena session factory."""
        arenas: list[_FakeSession] = []

        def session_factory(publish: SnapshotPublisher) -> LiveSession:
            arena = _FakeSession(publish=publish)
            arenas.append(arena)
            return cast(LiveSession, arena)

        adapter = LiveSessionAdapter(
            session_factory=session_factory,
            poll_interval_ms=5,
            test_draft_factory=cast("TestDraftFactory", test_draft_factory),
        )
        adapter.start()
        return adapter, arenas

    factory = _RecordingTestDraftFactory(set_codes=("hob", "lci"))
    adapter, arenas = build_adapter(factory)
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["supported_set_codes"]
            == ["hob", "lci"],
            description="the published Test Draft capability",
        )
        assert adapter.state["test_draft"]["enabled"] is True
        assert adapter.state["test_draft"]["default_set_code"] == "hob"
        assert adapter.state["test_draft"]["phase"] == "idle"
        assert adapter.state["test_draft"]["active"] is False
        assert adapter.state["test_draft"]["error"] is None
        assert factory.supported_calls == 1
        assert factory.create_calls == []
        assert arenas[0].poll_thread_ids
        assert all(
            thread_id != gui_thread_id for thread_id in arenas[0].poll_thread_ids
        )
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()

    # The preferred set code wins the default when it is not listed first.
    preferred_factory = _RecordingTestDraftFactory(set_codes=("lci", "hob"))
    preferred_adapter, _ = build_adapter(preferred_factory)
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: preferred_adapter.state["test_draft"][
                "default_set_code"
            ]
            == "hob",
            description="the preferred default Test Draft set",
        )
        assert preferred_adapter.state["test_draft"]["supported_set_codes"] == [
            "lci",
            "hob",
        ]
    finally:
        preferred_adapter.shutdown()
        preferred_adapter.wait_for_shutdown()

    failing_factory = _RecordingTestDraftFactory(
        supported_error=TestDraftError(
            "capability metadata is invalid",
            stage="startup",
        )
    )
    failing_adapter, failing_arenas = build_adapter(failing_factory)
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: failing_adapter.state["test_draft"]["error"]
            == "capability metadata is invalid",
            description="the published Test Draft capability failure",
        )
        failing_arena = failing_arenas[0]
        assert failing_adapter.state["test_draft"]["enabled"] is True
        assert failing_adapter.state["test_draft"]["supported_set_codes"] == []
        assert failing_adapter.state["test_draft"]["default_set_code"] is None
        assert failing_adapter.state["test_draft"]["active"] is False
        polls_before_failure = len(failing_arena.poll_thread_ids)
        _process_until(
            application=qcore_application,
            predicate=lambda: len(failing_arena.poll_thread_ids)
            >= polls_before_failure + 2,
            description="Arena polling despite the capability failure",
        )
        assert (
            failing_adapter.state["test_draft"]["error"]
            == "capability metadata is invalid"
        )
    finally:
        failing_adapter.shutdown()
        failing_adapter.wait_for_shutdown()


def test_live_adapter_keeps_gui_thread_responsive_during_blocked_test_draft_start(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_FakeSession] = []
    simulated_session = _FakeTestDraftSession()
    controller = _FakeTestDraftController(start_blocks=True)
    runtime = _FakeTestDraftRuntime(
        session=simulated_session,
        controller=controller,
    )
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        session = _FakeSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    ticks = _GuiTickCounter()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(sessions)
            and bool(sessions[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["pending"] is True
            and ticks.ticks > 0,
            description="the pending Test Draft start",
        )
        assert controller.started.is_set()
        assert controller.start_thread_ids
        assert all(
            thread_id != gui_thread_id
            for thread_id in controller.start_thread_ids
        )
        assert adapter.state["test_draft"]["phase"] == "starting"

        ticks_before_release = ticks.ticks
        blocked_deadline = time.monotonic() + 0.2
        while time.monotonic() < blocked_deadline:
            qcore_application.processEvents()
            time.sleep(0.001)
        assert ticks.ticks > ticks_before_release
        assert not controller.release.is_set()
        assert adapter.state["test_draft"]["pending"] is True

        controller.release.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["pending"] is False
            and adapter.state["test_draft"]["phase"] == "drafting",
            description="the released Test Draft start",
        )
        assert controller.start_calls == 1
        assert runtime.close_calls == 0
        assert adapter.state["test_draft"]["active"] is True
        assert adapter.state["test_draft"]["offer_generation"] == 1
        assert adapter.state["test_draft"]["set_code"] == "hob"
        assert adapter.state["pool"]["total_cards"] == _SIMULATED_POOL_TOTAL_CARDS
    finally:
        ticks.stop()
        controller.release.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_ignores_late_arena_results_while_test_draft_is_authoritative(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    arenas: list[_BlockedArenaAuxiliaryFakeSession] = []
    arena_publishers: list[SnapshotPublisher] = []
    client = _ProfileRefreshFakeClient(
        result=ProfileRefreshResult(
            profile=SetProfile.generic(set_code="OTJ", event_format="QuickDraft"),
            outcome=ProfileRefreshOutcome.UPDATED,
            diagnostics=(),
        )
    )
    simulated_session = _FakeTestDraftSession()
    runtime = _FakeTestDraftRuntime(
        session=simulated_session,
        controller=_FakeTestDraftController(),
    )
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _BlockedArenaAuxiliaryFakeSession(publish=publish)
        arenas.append(arena)
        arena_publishers.append(publish)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=60_000,
        profile_client=cast(ProfileClient, client),
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas)
            and arenas[0].fetch_started.is_set()
            and client.started.is_set(),
            description="the blocked Arena image and profile work",
        )
        arena = arenas[0]
        publish_arena_snapshot = arena_publishers[0]
        changed_arena_snapshot = replace(
            arena.snapshot,
            pool=replace(arena.snapshot.pool, total_cards=31),
        )
        publish_arena_snapshot(changed_arena_snapshot)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["pool"]["total_cards"] == 31,
            description="the Arena snapshot published while Arena is authoritative",
        )
        assert client.calls == [("OTJ", "QuickDraft", False)]

        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["active"] is True
            and adapter.state["pool"]["total_cards"]
            == _SIMULATED_POOL_TOTAL_CARDS,
            description="the simulated draft becoming authoritative",
        )
        assert adapter.state["test_draft"]["phase"] == "drafting"
        assert adapter.state["test_draft"]["offer_generation"] == 1
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(
                simulated_session.image_selection_thread_ids
            ),
            description="the simulated session probed for its next card image",
        )
        simulated_state = json.dumps(adapter.state, sort_keys=True)

        arena.fetch_release.set()
        client.release.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: arena.fetch_returned.is_set(),
            description="the released Arena image fetch",
        )
        publish_arena_snapshot(
            replace(
                arena.snapshot,
                pool=replace(arena.snapshot.pool, total_cards=55),
            )
        )
        adapter.setSplashEnabled(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: simulated_session.commands
            == [ChangeSplashPreference(enabled=False)],
            description="the command routed to the simulated session",
        )
        # Both stale completions are released and one further queued command
        # has run, so every late Arena input has since reached the worker.
        settle_deadline = time.monotonic() + 0.3
        while time.monotonic() < settle_deadline:
            qcore_application.processEvents()
            assert json.dumps(adapter.state, sort_keys=True) == simulated_state
            time.sleep(0.001)

        assert json.dumps(adapter.state, sort_keys=True) == simulated_state
        assert adapter.state["pool"]["total_cards"] == _SIMULATED_POOL_TOTAL_CARDS
        assert adapter.state["test_draft"]["active"] is True
        assert arena.completions == []
        assert arena.completed_profile_requests == []
        assert arena.failed_profile_requests == []
        assert len(arena.fetch_thread_ids) == 1
        assert client.calls == [("OTJ", "QuickDraft", False)]
        assert simulated_session.dispatch_thread_ids
        assert all(
            thread_id != gui_thread_id
            for thread_id in simulated_session.dispatch_thread_ids
        )
        assert all(
            thread_id != gui_thread_id
            for thread_id in simulated_session.image_selection_thread_ids
        )
    finally:
        if arenas:
            arenas[0].fetch_release.set()
        client.release.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_start_failure_keeps_arena_authoritative(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    sessions: list[_FakeSession] = []
    factory = _RecordingTestDraftFactory(
        set_codes=("hob",),
        create_error=TestDraftError(
            "the test draft could not start: boom",
            stage="startup",
        ),
    )

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        session = _FakeSession(publish=publish)
        sessions.append(session)
        return cast(LiveSession, session)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["supported_set_codes"]
            == ["hob"],
            description="the published Test Draft capability",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "failed",
            description="the reported Test Draft start failure",
        )
        session = sessions[0]
        assert adapter.state["test_draft"]["active"] is False
        assert (
            adapter.state["test_draft"]["error"]
            == "the test draft could not start: boom"
        )
        assert adapter.state["test_draft"]["pending"] is False
        assert adapter.state["test_draft"]["offer_generation"] == 0
        assert adapter.state["pool"]["total_cards"] == 24
        assert factory.supported_calls == 1
        assert len(factory.create_calls) == 1
        assert factory.create_calls[0]["set_code"] == "hob"
        assert factory.create_thread_ids[0] != gui_thread_id
        polls_before_leave = len(session.poll_thread_ids)
        _process_until(
            application=qcore_application,
            predicate=lambda: len(session.poll_thread_ids)
            >= polls_before_leave + 2,
            description="Arena polling after the Test Draft start failure",
        )

        adapter.leaveTestDraft()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "idle",
            description="the cleared Test Draft failure",
        )
        assert adapter.state["test_draft"]["error"] is None
        assert adapter.state["test_draft"]["active"] is False
        assert adapter.state["test_draft"]["pending"] is False
        assert adapter.state["test_draft"]["offer_generation"] == 0
        assert len(factory.create_calls) == 1
        assert session.commands == []
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_installs_mocked_draft_factory_without_restart(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    factory = _RecordingTestDraftFactory(set_codes=("lci", "hob"))
    adapter = LiveSessionAdapter(session_factory=session_factory, poll_interval_ms=5)
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        assert adapter.state["test_draft"] == _DISABLED_TEST_DRAFT_STATE

        adapter.setTestDraftFactory(cast("TestDraftFactory", factory))
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["enabled"] is True,
            description="the installed Mocked Draft capability",
        )
        assert adapter.state["test_draft"]["supported_set_codes"] == ["lci", "hob"]
        assert adapter.state["test_draft"]["default_set_code"] == "hob"
        assert adapter.state["test_draft"]["phase"] == "idle"
        assert adapter.state["test_draft"]["active"] is False
        assert adapter.state["test_draft"]["error"] is None
        assert factory.supported_calls == 1
        assert factory.supported_thread_ids[0] != gui_thread_id
        assert factory.create_calls == []
        arena = arenas[0]
        assert arena.commands == []
        assert adapter.state["pool"]["total_cards"] == arena.snapshot.pool.total_cards
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_clearing_mocked_draft_factory_restores_arena_authority(
    qcore_application: QCoreApplication,
) -> None:
    runtime = _FakeTestDraftRuntime()
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        # A long interval keeps Arena polls countable: only explicit polls run.
        poll_interval_ms=600_000,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        arena = arenas[0]
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        assert adapter.state["test_draft"]["active"] is True
        assert adapter.state["pool"]["total_cards"] == _SIMULATED_POOL_TOTAL_CARDS
        adapter.setContextualScoringEnabled(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: runtime.session.commands
            == [ChangeContextualScoring(enabled=False)],
            description="the preference change inside the simulated draft",
        )
        assert arena.commands == []
        polls_before_clear = len(arena.poll_thread_ids)

        adapter.setTestDraftFactory(None)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]
            == _DISABLED_TEST_DRAFT_STATE,
            description="the cleared Mocked Draft capability",
        )
        assert len(arena.poll_thread_ids) == polls_before_clear + 1
        assert runtime.cancel_calls == 1
        assert runtime.close_calls == 1
        assert adapter.state["errors"] == []
        assert adapter.state["status"]["phase"] != "error"
        assert adapter.state["pool"]["total_cards"] == arena.snapshot.pool.total_cards
        assert arena.commands == [ChangeContextualScoring(enabled=False)]
        assert adapter.state["contextual_adjustments_enabled"] is False
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_applies_mocked_draft_toggle_after_a_blocked_start(
    qcore_application: QCoreApplication,
) -> None:
    gui_thread_id = threading.get_ident()
    controller = _FakeTestDraftController(start_blocks=True)
    runtime = _FakeTestDraftRuntime(controller=controller)
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    ticks = _GuiTickCounter()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: controller.started.is_set()
            and adapter.state["test_draft"]["phase"] == "starting",
            description="the blocked Test Draft start",
        )
        assert controller.start_thread_ids[0] != gui_thread_id

        adapter.setTestDraftFactory(None)
        ticks_before_release = ticks.ticks
        blocked_deadline = time.monotonic() + 0.2
        while time.monotonic() < blocked_deadline:
            qcore_application.processEvents()
            time.sleep(0.001)
        # The toggle waits behind the blocked start while both threads run on.
        assert ticks.ticks > ticks_before_release
        assert adapter.state["test_draft"]["enabled"] is True
        assert runtime.close_calls == 0

        controller.release.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]
            == _DISABLED_TEST_DRAFT_STATE,
            description="the Mocked Draft toggle applied after the blocked start",
        )
        assert runtime.close_calls == 1
        assert adapter.state["errors"] == []
        arena = arenas[0]
        assert adapter.state["pool"]["total_cards"] == arena.snapshot.pool.total_cards

        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: len(arena.poll_thread_ids) >= 2,
            description="Arena polling after the cleared Mocked Draft capability",
        )
        assert len(factory.create_calls) == 1
    finally:
        ticks.stop()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_ignores_reinstalling_the_same_mocked_draft_factory(
    qcore_application: QCoreApplication,
) -> None:
    controller = _FakeTestDraftController()
    runtime = _FakeTestDraftRuntime(controller=controller)
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        active_state = dict(adapter.state["test_draft"])

        adapter.setTestDraftFactory(cast("TestDraftFactory", factory))
        repeat_deadline = time.monotonic() + 0.1
        while time.monotonic() < repeat_deadline:
            qcore_application.processEvents()
            time.sleep(0.001)
        assert adapter.state["test_draft"] == active_state
        assert adapter.state["test_draft"]["active"] is True
        assert adapter.state["test_draft"]["offer_generation"] == 1
        assert factory.supported_calls == 1
        assert runtime.close_calls == 0
        assert controller.cancelled.is_set() is False
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


# --------------------------------------------------------------------------
# Test Draft acceptance over real simulated runtimes (issue #547). The tests
# above script the simulated source; these drive the production runtime over
# the fixture fake transport, so published state, picks, and the build are the
# real card-data and controller results:
#   _CountingTestDraftRuntime      counts one real runtime's close() calls
#   _RealTestDraftRuntimeFactory   builds real runtimes for the live worker
#   _start_real_test_draft_adapter boots one adapter over a seeded tmp tree
# `_helper_socket` is the fixture socket a real runtime can start from: it
# answers with the seats of the runtime's generated identity, which only the
# connect query knows.
# --------------------------------------------------------------------------


class _CountingTestDraftRuntime:
    """Count one real runtime's closes while delegating every owned resource."""

    def __init__(self, *, runtime: TestDraftRuntime) -> None:
        self.runtime = runtime
        self.close_calls = 0

    @property
    def session(self) -> LiveSession:
        return self.runtime.session

    @property
    def controller(self) -> TestDraftController:
        return self.runtime.controller

    def cancel(self) -> None:
        self.runtime.cancel()

    def close(self) -> None:
        self.close_calls += 1
        self.runtime.close()


class _RealTestDraftRuntimeFactory:
    """Create real simulated runtimes over one seeded fake transport."""

    def __init__(
        self,
        *,
        sources: _HelperSources,
        socket: _FakeSocket,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._sources = sources
        self._socket = socket
        self._timeout_seconds = timeout_seconds
        self.started: list[_CountingTestDraftRuntime] = []
        self.publication_thread_ids: list[int] = []

    def __call__(
        self,
        *,
        server_url: str,
        set_code: str,
        publisher: SnapshotPublisher,
        splash_enabled: bool,
        contextual_adjustments_enabled: bool,
        ai_enhanced_suggestions_enabled: bool,
    ) -> _CountingTestDraftRuntime:
        """Build one runtime that publishes through the worker's publisher."""

        publication_thread_ids = self.publication_thread_ids

        def recording_publisher(snapshot: LiveSessionSnapshot) -> None:
            publication_thread_ids.append(threading.get_ident())
            publisher(snapshot)

        simulation_app_dir = self._sources.simulation_dir / f"run-{len(self.started)}"
        simulation_app_dir.mkdir(parents=True, exist_ok=True)
        runtime = create_test_draft_runtime(
            draftmancer_dir=self._sources.draftmancer_dir,
            scryfall_bulk_file=self._sources.bulk_path,
            server_url=server_url,
            set_code=set_code,
            timeout_seconds=self._timeout_seconds,
            source_app_dir=self._sources.normal_app_dir,
            profile_manifest_url=None,
            profile_network_policy=ProfileNetworkPolicy.OFFLINE,
            snapshot_publisher=recording_publisher,
            splash_enabled=splash_enabled,
            contextual_adjustments_enabled=contextual_adjustments_enabled,
            ai_enhanced_suggestions_enabled=ai_enhanced_suggestions_enabled,
            simulation_app_dir=simulation_app_dir,
            socket_client=self._socket,
        )
        wrapper = _CountingTestDraftRuntime(runtime=runtime)
        self.started.append(wrapper)
        return wrapper

    @property
    def runtime(self) -> TestDraftRuntime:
        """Return the one runtime the worker asked this factory to build."""
        assert len(self.started) == 1, "the worker built more than one runtime."
        return self.started[0].runtime

    @property
    def close_calls(self) -> int:
        return sum(wrapper.close_calls for wrapper in self.started)


def _start_real_test_draft_adapter(
    *,
    application: QCoreApplication,
    tmp_path: Path,
    socket: _FakeSocket,
) -> tuple[
    LiveSessionAdapter,
    _FakeSession,
    _RecordingTestDraftFactory,
    _RealTestDraftRuntimeFactory,
]:
    """Start one live adapter whose Test Draft factory builds real runtimes."""

    sources = _seed_helper_sources(tmp_path=tmp_path)
    source = _RealTestDraftRuntimeFactory(sources=sources, socket=socket)
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime_factory=source)
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        # A long interval keeps Arena polls countable: only explicit polls run.
        poll_interval_ms=600_000,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    _process_until(
        application=application,
        predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
        description="the initial Arena poll",
    )
    assert len(arenas) == 1
    return adapter, arenas[0], factory, source


def test_live_adapter_publishes_manual_test_draft_snapshots_off_gui_thread(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    gui_thread_id = threading.get_ident()
    socket = _helper_socket(states=_arena_states())
    adapter, arena, factory, source = _start_real_test_draft_adapter(
        application=qcore_application,
        tmp_path=tmp_path,
        socket=socket,
    )
    try:
        assert adapter.state["pool"]["total_cards"] == arena.snapshot.pool.total_cards
        assert arena.snapshot.build is not None

        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        simulated = source.runtime.session.snapshot
        test_draft = adapter.state["test_draft"]
        assert test_draft["active"] is True
        assert test_draft["phase"] == "drafting"
        assert test_draft["mode"] == "manual"
        assert test_draft["set_code"] == "hob"
        assert test_draft["pending"] is False
        assert test_draft["error"] is None
        assert factory.create_calls[0]["set_code"] == "hob"
        assert factory.create_thread_ids[0] != gui_thread_id
        assert source.publication_thread_ids
        assert all(
            thread_id != gui_thread_id
            for thread_id in source.publication_thread_ids
        )
        # The published state is the simulated session's state: a fresh pool,
        # the simulated draft identity, and no Arena build.
        assert adapter.state["pool"]["total_cards"] == simulated.pool.total_cards == 0
        assert (
            adapter.state["draft"]["draft_id"]
            == simulated.draft.draft_id
            != arena.snapshot.draft.draft_id
        )
        assert (
            adapter.state["draft"]["set_code"]
            == simulated.draft.set_code
            == "HOB"
        )
        assert [
            row["card"]["grp_id"]
            for row in adapter.state["recommendations"]["cards"]
        ] == [
            row.card.grp_id for row in simulated.recommendations.cards
        ] == list(_HELPER_GRP_IDS)
        assert adapter.state["build"] is None
        # No image work is configured for this runtime, so the published state
        # must stay free of any implied image failure.
        assert adapter.state["errors"] == []

        # A poll tick the Arena timer queued before the switch cannot publish
        # Arena state: the worker must keep the simulated source authoritative.
        polls_before_tick = len(arena.poll_thread_ids)
        worker = adapter._worker
        assert worker is not None
        QMetaObject.invokeMethod(
            worker,
            "_poll",
            Qt.ConnectionType.QueuedConnection,
        )
        # A simulated-session command queued behind the tick proves the tick
        # was processed before this state is observed.
        adapter.setSplashEnabled(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["recommendations"]["splash_enabled"]
            is False,
            description="the queued Arena poll tick",
        )
        assert len(arena.poll_thread_ids) == polls_before_tick
        assert adapter.state["pool"]["total_cards"] == 0
        assert adapter.state["draft"]["draft_id"] == simulated.draft.draft_id
        assert adapter.state["test_draft"]["offer_generation"] == 1
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_runs_auto_test_draft_to_completion_and_build(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    socket = _helper_socket(states=_arena_states())
    adapter, arena, factory, source = _start_real_test_draft_adapter(
        application=qcore_application,
        tmp_path=tmp_path,
        socket=socket,
    )
    try:
        adapter.startTestDraft("auto", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "completed",
            description="the completed automatic Test Draft",
        )
        state = adapter.state
        assert state["test_draft"]["active"] is True
        assert state["test_draft"]["mode"] == "auto"
        assert state["test_draft"]["offer_generation"] == 0
        assert state["test_draft"]["pending"] is False
        assert state["test_draft"]["error"] is None
        assert state["draft"]["completed"] is True
        assert state["pool"]["total_cards"] == len(_HELPER_OFFERS)
        assert state["draft"]["draft_id"] != arena.snapshot.draft.draft_id

        simulated_build = source.runtime.session.snapshot.build
        build = state["build"]
        assert simulated_build is not None
        assert build is not None
        assert build["selected_pair"] == simulated_build.selected_pair
        assert build["spells"]
        assert build["lands"]
        assert [
            row["card"]["grp_id"] for row in build["spells"]
        ] == [spell.card.grp_id for spell in simulated_build.spells]
        assert [row["name"] for row in build["lands"]] == [
            land.name for land in simulated_build.lands
        ]
        assert all(row["quantity"] >= 1 for row in build["lands"])

        picks = _pick_card_calls(socket)
        assert len(picks) == len(_HELPER_OFFERS) == len(_HELPER_GRP_IDS)
        for payload, offered_grp_ids in zip(
            picks, _HELPER_OFFERS, strict=True
        ):
            assert payload["burnedCards"] == []
            (chosen_index,) = payload["pickedCards"]
            assert 0 <= chosen_index < len(offered_grp_ids)
        assert state["errors"] == []
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_rejects_stale_and_duplicate_test_draft_generations(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    socket = _helper_socket(states=_arena_states())
    adapter, _, _, source = _start_real_test_draft_adapter(
        application=qcore_application,
        tmp_path=tmp_path,
        socket=socket,
    )
    try:
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        rank_one_grp_id = adapter.state["recommendations"]["cards"][0]["card"]["grp_id"]

        # The stale generation, the future generation, and the duplicate of the
        # current generation are queued back to back; only the one call holding
        # the current generation may reach the simulator.
        adapter.pickTestDraft(rank_one_grp_id, 0)
        adapter.pickTestDraft(rank_one_grp_id, 2)
        adapter.pickTestDraft(rank_one_grp_id, 1)
        adapter.pickTestDraft(rank_one_grp_id, 1)
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 2,
            description="the one accepted manual pick",
        )
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                source.runtime.session.snapshot.pool.total_cards == 1
            ),
            description="the confirmed simulated pick",
        )
        assert len(_pick_card_calls(socket)) == 1
        assert adapter.state["pool"]["total_cards"] == 1
        assert [
            row["card"]["grp_id"]
            for row in adapter.state["recommendations"]["cards"]
        ] == list(_HELPER_OFFERS[1])
        assert rank_one_grp_id not in {
            row["card"]["grp_id"]
            for row in adapter.state["recommendations"]["cards"]
        }
        assert adapter.state["test_draft"]["phase"] == "drafting"
        assert adapter.state["test_draft"]["pending"] is False
        assert adapter.state["test_draft"]["active"] is True
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_leaves_test_draft_and_restores_arena_state(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    socket = _helper_socket(states=_arena_states())
    adapter, arena, factory, source = _start_real_test_draft_adapter(
        application=qcore_application,
        tmp_path=tmp_path,
        socket=socket,
    )
    try:
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        assert adapter.state["pool"]["total_cards"] == 0
        assert arena.snapshot.pool.total_cards != 0
        polls_before_leave = len(arena.poll_thread_ids)

        adapter.leaveTestDraft()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "idle"
            and adapter.state["test_draft"]["active"] is False,
            description="the restored Arena authority",
        )
        assert len(arena.poll_thread_ids) == polls_before_leave + 1
        assert adapter.state["test_draft"] == {
            "enabled": True,
            "active": False,
            "phase": "idle",
            "mode": None,
            "set_code": None,
            "supported_set_codes": ["hob"],
            "default_set_code": "hob",
            "pending": False,
            "offer_generation": 0,
            "error": None,
            "bulk_file_missing": False,
            "bulk_file_downloading": False,
            "bulk_file_download_percent": None,
        }
        # The published state is Arena state again, not the simulated draft.
        assert adapter.state["pool"]["total_cards"] == arena.snapshot.pool.total_cards
        assert adapter.state["draft"]["draft_id"] == arena.snapshot.draft.draft_id
        assert [
            row["card"]["grp_id"]
            for row in adapter.state["recommendations"]["cards"]
        ] == [
            row.card.grp_id for row in arena.snapshot.recommendations.cards
        ]
        assert len(factory.create_calls) == 1
        assert source.close_calls == 1
        assert socket.disconnect_count == 1
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_leave_cancels_blocked_test_draft_and_closes_once(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    socket = _FakeSocket(start_action=_withheld_ack_action(entered=entered))
    adapter, arena, factory, source = _start_real_test_draft_adapter(
        application=qcore_application,
        tmp_path=tmp_path,
        socket=socket,
    )
    try:
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: entered.is_set()
            and adapter.state["test_draft"]["pending"] is True,
            description="the blocked Test Draft start",
        )
        assert adapter.state["test_draft"]["active"] is False
        assert adapter.state["test_draft"]["phase"] == "starting"

        adapter.leaveTestDraft()
        # The runtime timeout is five seconds, so only a cancelled start can
        # return to Arena state inside this bounded pump.
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "idle"
            and adapter.state["test_draft"]["pending"] is False,
            description="the cancelled Test Draft start",
        )
        assert adapter.state["test_draft"]["active"] is False
        assert adapter.state["test_draft"]["error"] is None
        assert adapter.state["test_draft"]["mode"] is None
        assert adapter.state["status"]["phase"] != "error"
        assert adapter.state["errors"] == []
        assert adapter.state["pool"]["total_cards"] == arena.snapshot.pool.total_cards
        assert len(factory.create_calls) == 1
        assert source.close_calls == 1
        assert socket.disconnect_count == 1
        assert _pick_card_calls(socket) == []
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_shutdown_closes_blocked_test_draft_once(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    socket = _FakeSocket(start_action=_withheld_ack_action(entered=entered))
    adapter, arena, factory, source = _start_real_test_draft_adapter(
        application=qcore_application,
        tmp_path=tmp_path,
        socket=socket,
    )
    adapter.startTestDraft("manual", "hob")
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: entered.is_set()
            and adapter.state["test_draft"]["pending"] is True,
            description="the blocked Test Draft start",
        )
        blocked_state = json.dumps(adapter.state, sort_keys=True)

        adapter.shutdown()
        adapter.wait_for_shutdown()

        thread = adapter.thread
        assert thread is not None
        assert thread.isRunning() is False
        assert len(factory.create_calls) == 1
        assert source.close_calls == 1
        assert socket.disconnect_count == 1
        assert _pick_card_calls(socket) == []
        assert adapter.state["errors"] == []
        assert adapter.state["test_draft"]["active"] is False
        assert (
            adapter.state["pool"]["total_cards"]
            == arena.snapshot.pool.total_cards
        )
        # Shutdown publishes nothing: the last visible state survives it.
        assert json.dumps(adapter.state, sort_keys=True) == blocked_state
        qcore_application.processEvents()
        assert json.dumps(adapter.state, sort_keys=True) == blocked_state
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_carries_preferences_across_test_draft_sources(
    qcore_application: QCoreApplication,
    tmp_path: Path,
) -> None:
    socket = _helper_socket(states=_arena_states())
    adapter, arena, factory, source = _start_real_test_draft_adapter(
        application=qcore_application,
        tmp_path=tmp_path,
        socket=socket,
    )
    try:
        adapter.setSplashEnabled(False)
        adapter.setContextualScoringEnabled(False)
        adapter.setAiEnhancedSuggestionsEnabled(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: arena.commands
            == [
                ChangeSplashPreference(enabled=False),
                ChangeContextualScoring(enabled=False),
                ChangeAiEnhancedSuggestions(enabled=False),
            ],
            description="the Arena preference commands",
        )
        assert arena.snapshot.recommendations.splash_enabled is False
        assert arena.snapshot.contextual_adjustments_enabled is False

        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        assert factory.create_calls[0] == {
            "server_url": factory.server_url,
            "set_code": "hob",
            "splash_enabled": False,
            "contextual_adjustments_enabled": False,
            "ai_enhanced_suggestions_enabled": False,
        }
        assert adapter.state["recommendations"]["splash_enabled"] is False
        assert adapter.state["contextual_adjustments_enabled"] is False

        adapter.setSplashEnabled(True)
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                source.runtime.session.snapshot.recommendations.splash_enabled
                is True
            ),
            description="the splash toggle inside the simulated draft",
        )
        assert adapter.state["recommendations"]["splash_enabled"] is True

        adapter.leaveTestDraft()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "idle",
            description="the restored Arena authority",
        )
        assert adapter.state["recommendations"]["splash_enabled"] is True
        assert [
            command
            for command in arena.commands
            if isinstance(command, ChangeSplashPreference)
        ] == [
            ChangeSplashPreference(enabled=False),
            ChangeSplashPreference(enabled=True),
        ]
        # Only the changed preference is replayed; the two unchanged choices
        # were already applied to the retained Arena session.
        assert [
            command
            for command in arena.commands
            if isinstance(command, ChangeContextualScoring)
        ] == [ChangeContextualScoring(enabled=False)]
        assert [
            command
            for command in arena.commands
            if isinstance(command, ChangeAiEnhancedSuggestions)
        ] == [ChangeAiEnhancedSuggestions(enabled=False)]
        assert source.close_calls == 1
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


# --------------------------------------------------------------------------
# Test Draft review evidence (issue #547 findings F1-F4). Each test pins one
# behavior the review could only infer from the surrounding mechanism:
#   F2  a processed Leave owns the worker, so a queued Start is dropped
#   F1  a superseded runtime's publisher cannot change published state
#   F3  contextual and AI choices made during a Test Draft replay onto Arena
#   F4  the GUI thread keeps ticking while a queued pick is blocked
# --------------------------------------------------------------------------


class _ScriptedTestDraftStep:
    """Carry the one snapshot a scripted simulated pick publishes."""

    def __init__(self, *, after: LiveSessionSnapshot) -> None:
        self.after = after


def test_live_adapter_drops_a_start_that_a_pending_leave_owns(
    qcore_application: QCoreApplication,
) -> None:
    arenas: list[_BusySession] = []
    controller = _FakeTestDraftController()
    runtime = _FakeTestDraftRuntime(
        session=_FakeTestDraftSession(),
        controller=controller,
    )
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _BusySession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas),
            description="the created Arena session",
        )
        assert len(arenas) == 1
        arena = arenas[0]
        _process_until(
            application=qcore_application,
            predicate=lambda: arena.started.is_set(),
            description="the blocked Arena startup scan",
        )
        # Start and Leave are queued while the worker is still inside the
        # startup scan; the leave request marks the worker directly, so the
        # Start the worker has not reached yet must be dropped.
        adapter.startTestDraft("manual", "hob")
        adapter.leaveTestDraft()
        arena.release.set()

        # One further intention queued behind both proves they were processed.
        adapter.setSplashEnabled(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: arena.commands
            == [ChangeSplashPreference(enabled=False)],
            description="the intention queued behind the start and the leave",
        )
        assert factory.create_calls == []
        assert factory.supported_calls == 1
        assert controller.start_calls == 0
        assert runtime.close_calls == 0
        assert adapter.state["test_draft"] == {
            "enabled": True,
            "active": False,
            "phase": "idle",
            "mode": None,
            "set_code": None,
            "supported_set_codes": ["hob"],
            "default_set_code": "hob",
            "pending": False,
            "offer_generation": 0,
            "error": None,
            "bulk_file_missing": False,
            "bulk_file_downloading": False,
            "bulk_file_download_percent": None,
        }
        assert adapter.state["errors"] == []
        assert adapter.state["pool"]["total_cards"] == arena.snapshot.pool.total_cards
        polls_before = len(arena.poll_thread_ids)
        _process_until(
            application=qcore_application,
            predicate=lambda: len(arena.poll_thread_ids) >= polls_before + 2,
            description="Arena polling after the dropped Test Draft start",
        )
    finally:
        if arenas:
            arenas[0].release.set()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_ignores_publications_from_a_superseded_test_draft_runtime(
    qcore_application: QCoreApplication,
) -> None:
    arenas: list[_FakeSession] = []
    runtimes: list[_FakeTestDraftRuntime] = []

    def runtime_factory(**_: object) -> object:
        runtime = _FakeTestDraftRuntime()
        runtimes.append(runtime)
        return runtime

    factory = _RecordingTestDraftFactory(
        set_codes=("hob",),
        runtime_factory=runtime_factory,
    )

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=60_000,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the first manual Test Draft offer",
        )
        superseded_publisher = factory.publishers[0]
        assert len(runtimes) == 1
        assert adapter.state["pool"]["total_cards"] == _SIMULATED_POOL_TOTAL_CARDS

        adapter.leaveTestDraft()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "idle"
            and adapter.state["test_draft"]["active"] is False,
            description="the restored Arena authority",
        )
        assert runtimes[0].close_calls == 1

        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: len(factory.publishers) == 2
            and adapter.state["test_draft"]["offer_generation"] == 1,
            description="the second manual Test Draft offer",
        )
        assert superseded_publisher is not factory.publishers[1]
        superseded_state = json.dumps(adapter.state, sort_keys=True)
        simulated = _test_draft_session_snapshot()

        # The retired runtime still holds its publisher. The snapshot it would
        # publish is the terminal STOPPED state a closing simulated session
        # emits, carried here with a pool the live draft never showed.
        superseded_publisher(
            replace(
                simulated,
                status=replace(simulated.status, phase="stopped"),
                pool=replace(simulated.pool, total_cards=41),
            )
        )
        settle_deadline = time.monotonic() + 0.2
        while time.monotonic() < settle_deadline:
            qcore_application.processEvents()
            assert json.dumps(adapter.state, sort_keys=True) == superseded_state
            time.sleep(0.001)
        assert json.dumps(adapter.state, sort_keys=True) == superseded_state

        # The publisher of the runtime that owns the current generation still
        # publishes, so the guard drops stale publications only.
        factory.publishers[1](
            replace(simulated, pool=replace(simulated.pool, total_cards=63))
        )
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["pool"]["total_cards"] == 63,
            description="the current Test Draft runtime publication",
        )
        assert adapter.state["status"]["phase"] != "stopped"
        assert adapter.state["test_draft"]["active"] is True
        assert adapter.state["test_draft"]["offer_generation"] == 1
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_replays_contextual_and_ai_preferences_on_leave(
    qcore_application: QCoreApplication,
) -> None:
    arenas: list[_FakeSession] = []
    simulated_session = _FakeTestDraftSession()
    runtime = _FakeTestDraftRuntime(
        session=simulated_session,
        controller=_FakeTestDraftController(),
    )
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=60_000,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        arena = arenas[0]
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        assert arena.commands == []
        assert adapter.state["contextual_adjustments_enabled"] is True

        adapter.setContextualScoringEnabled(False)
        adapter.setAiEnhancedSuggestionsEnabled(False)
        _process_until(
            application=qcore_application,
            predicate=lambda: simulated_session.commands
            == [
                ChangeContextualScoring(enabled=False),
                ChangeAiEnhancedSuggestions(enabled=False),
            ],
            description="the preference commands inside the simulated draft",
        )
        assert arena.commands == []

        adapter.leaveTestDraft()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "idle"
            and arena.commands
            == [
                ChangeContextualScoring(enabled=False),
                ChangeAiEnhancedSuggestions(enabled=False),
            ],
            description="the replayed Arena preferences",
        )
        assert adapter.state["test_draft"]["active"] is False
        assert adapter.state["contextual_adjustments_enabled"] is False
        assert not any(
            isinstance(command, ChangeSplashPreference)
            for command in arena.commands
        )
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_keeps_gui_thread_responsive_during_blocked_test_draft_pick(
    qcore_application: QCoreApplication,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    simulated = _test_draft_session_snapshot()
    confirmed_snapshot = replace(
        simulated,
        pool=replace(simulated.pool, total_cards=13),
    )

    def confirm(*, grp_id: int, expected_offer: TestDraftOfferIdentity) -> object:
        del grp_id, expected_offer
        entered.set()
        release.wait(timeout=3.0)
        return _ScriptedTestDraftStep(after=confirmed_snapshot)

    arenas: list[_FakeSession] = []
    controller = _FakeTestDraftController(confirm=confirm)
    runtime = _FakeTestDraftRuntime(
        session=_FakeTestDraftSession(),
        controller=controller,
    )
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    ticks = _GuiTickCounter()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        rank_one_grp_id = adapter.state["recommendations"]["cards"][0]["card"][
            "grp_id"
        ]

        adapter.pickTestDraft(rank_one_grp_id, 1)
        _process_until(
            application=qcore_application,
            predicate=lambda: entered.is_set()
            and adapter.state["test_draft"]["pending"] is True,
            description="the blocked Test Draft pick",
        )
        ticks_before_release = ticks.ticks
        blocked_deadline = time.monotonic() + 0.2
        while time.monotonic() < blocked_deadline:
            qcore_application.processEvents()
            time.sleep(0.001)
        assert ticks.ticks > ticks_before_release
        assert not release.is_set()
        assert adapter.state["test_draft"]["pending"] is True

        release.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["pending"] is False
            and adapter.state["test_draft"]["offer_generation"] == 2,
            description="the confirmed Test Draft pick",
        )
        assert controller.confirm_calls == [(rank_one_grp_id, _test_draft_offer())]
        assert controller.inspect_calls == 1
        assert adapter.state["pool"]["total_cards"] == 13
        assert adapter.state["test_draft"]["phase"] == "drafting"
    finally:
        release.set()
        ticks.stop()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_serves_and_releases_the_mocked_draft_server(
    qcore_application: QCoreApplication,
) -> None:
    """A manual start serves the URL and leave releases the server."""
    gui_thread_id = threading.get_ident()
    runtime = _FakeTestDraftRuntime()
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        assert factory.ensure_calls == 1
        assert all(
            thread_id != gui_thread_id for thread_id in factory.ensure_thread_ids
        )
        assert factory.create_calls[0]["server_url"] == factory.server_url
        assert adapter.state["test_draft"]["active"] is True

        adapter.leaveTestDraft()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "idle"
            and adapter.state["test_draft"]["active"] is False,
            description="the released Mocked Draft server",
        )
        assert factory.release_calls == 1
        assert adapter.state["pool"]["total_cards"] == arenas[0].snapshot.pool.total_cards
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_releases_the_mocked_draft_server_when_the_draft_start_fails(
    qcore_application: QCoreApplication,
) -> None:
    """A runtime failure tears down the server and publishes the error."""
    factory = _RecordingTestDraftFactory(
        set_codes=("hob",),
        create_error=TestDraftError(
            "card data for set 'hob' is unavailable", stage="startup"
        ),
    )
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "failed",
            description="the failed Mocked Draft start",
        )
        assert factory.ensure_calls == 1
        assert factory.release_calls == 1
        assert adapter.state["test_draft"]["error"] == (
            "card data for set 'hob' is unavailable"
        )
        assert adapter.state["test_draft"]["active"] is False
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_releases_the_mocked_draft_server_when_auto_startup_fails(
    qcore_application: QCoreApplication,
) -> None:
    """A failed automatic connect releases the served checkout and returns to Arena."""

    def run_auto() -> object:
        raise TestDraftError(
            "the simulated draft did not start: boom",
            stage="startup",
        )

    runtime = _FakeTestDraftRuntime(
        controller=_FakeTestDraftController(run_auto=run_auto)
    )
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("auto", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "failed"
            and adapter.state["test_draft"]["pending"] is False,
            description="the failed automatic Mocked Draft start",
        )
        state = adapter.state["test_draft"]
        assert state["phase"] == "failed"
        assert state["error"] == "the simulated draft did not start: boom"
        assert state["active"] is False
        assert state["pending"] is False
        assert factory.ensure_calls == 1
        assert len(factory.create_calls) == 1
        assert runtime.close_calls == 1
        assert factory.release_calls == 1
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_publishes_the_mocked_draft_server_error(
    qcore_application: QCoreApplication,
) -> None:
    """A server failure reaches the dialog with no runtime and a release."""
    from draftomen.draftmancer_server import MockedDraftServerError
    from draftomen.draftmancer_server import NODE_MISSING_MESSAGE

    factory = _RecordingTestDraftFactory(
        set_codes=("hob",),
        runtime=_FakeTestDraftRuntime(),
        ensure_error=MockedDraftServerError(NODE_MISSING_MESSAGE),
    )
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "failed",
            description="the published Mocked Draft server error",
        )
        assert factory.create_calls == []
        assert factory.release_calls == 1
        assert adapter.state["test_draft"]["error"] == NODE_MISSING_MESSAGE
        assert adapter.state["test_draft"]["phase"] == "failed"
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_releases_the_mocked_draft_server_when_the_capability_is_cleared(
    qcore_application: QCoreApplication,
) -> None:
    """Clearing the factory releases the outgoing server and keeps the new one."""
    runtime = _FakeTestDraftRuntime()
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)
    replacement = _RecordingTestDraftFactory(
        set_codes=("hob",), runtime=_FakeTestDraftRuntime()
    )
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        adapter.setTestDraftFactory(cast("TestDraftFactory", replacement))
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["active"] is False
            and adapter.state["test_draft"]["phase"] == "idle",
            description="the replacement Mocked Draft capability",
        )
        assert factory.release_calls == 1
        assert replacement.release_calls == 0
        assert adapter.state["test_draft"]["active"] is False
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_releases_the_mocked_draft_server_at_shutdown(
    qcore_application: QCoreApplication,
) -> None:
    """Shutdown releases the served checkout after a manual start."""
    runtime = _FakeTestDraftRuntime()
    factory = _RecordingTestDraftFactory(set_codes=("hob",), runtime=runtime)
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["offer_generation"] == 1,
            description="the manual Test Draft offer",
        )
        adapter.shutdown()
        adapter.wait_for_shutdown()
        assert factory.release_calls == 1
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_keeps_gui_thread_responsive_while_the_mocked_draft_server_starts(
    qcore_application: QCoreApplication,
) -> None:
    """The GUI thread ticks while ensure_server blocks on the worker thread."""
    gui_thread_id = threading.get_ident()
    blocker = threading.Event()
    runtime = _FakeTestDraftRuntime()
    factory = _RecordingTestDraftFactory(
        set_codes=("hob",), runtime=runtime, ensure_blocker=blocker
    )
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    ticks = _GuiTickCounter()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.startTestDraft("manual", "hob")
        _process_until(
            application=qcore_application,
            predicate=lambda: factory.ensure_calls == 1 and ticks.ticks > 0,
            description="the blocked Mocked Draft server start",
        )
        assert adapter.state["test_draft"]["phase"] == "starting"
        ticks_before_release = ticks.ticks
        blocked_deadline = time.monotonic() + 0.2
        while time.monotonic() < blocked_deadline:
            qcore_application.processEvents()
            time.sleep(0.001)
        assert ticks.ticks > ticks_before_release
        assert all(
            thread_id != gui_thread_id for thread_id in factory.ensure_thread_ids
        )
        assert adapter.state["test_draft"]["pending"] is True

        blocker.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "drafting",
            description="the served Mocked Draft start",
        )
        assert factory.create_calls[0]["server_url"] == factory.server_url
    finally:
        blocker.set()
        ticks.stop()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_downloads_the_missing_test_draft_bulk_file_off_the_gui_thread(
    qcore_application: QCoreApplication,
) -> None:
    """The worker downloads the Mocked Draft bulk file off the GUI thread."""
    gui_thread_id = threading.get_ident()
    blocker = threading.Event()
    factory = _RecordingTestDraftFactory(
        set_codes=("hob",),
        bulk_file_missing_value=True,
        download_blocker=blocker,
        download_progress=(50, 100),
    )
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    ticks = _GuiTickCounter()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                adapter.state["test_draft"]["bulk_file_missing"] is True
            ),
            description="the missing Mocked Draft bulk file",
        )
        adapter.downloadTestDraftBulkFile()
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                adapter.state["test_draft"]["bulk_file_downloading"] is True
            ),
            description="the reported Mocked Draft bulk download",
        )
        assert adapter.state["test_draft"]["bulk_file_download_percent"] == 50
        assert adapter.state["test_draft"]["pending"] is True
        assert adapter.state["test_draft"]["bulk_file_missing"] is True
        ticks_before_release = ticks.ticks
        blocked_deadline = time.monotonic() + 0.2
        while time.monotonic() < blocked_deadline:
            qcore_application.processEvents()
            time.sleep(0.001)
        assert ticks.ticks > ticks_before_release
        assert factory.download_calls == 1
        assert all(
            thread_id != gui_thread_id for thread_id in factory.download_thread_ids
        )

        factory.set_codes = ("hob", "msh")
        factory.bulk_file_missing_value = False
        blocker.set()
        _process_until(
            application=qcore_application,
            predicate=lambda: (
                adapter.state["test_draft"]["bulk_file_downloading"] is False
            ),
            description="the completed Mocked Draft bulk download",
        )
        assert adapter.state["test_draft"] == {
            "enabled": True,
            "active": False,
            "phase": "idle",
            "mode": None,
            "set_code": None,
            "supported_set_codes": ["hob", "msh"],
            "default_set_code": "hob",
            "pending": False,
            "offer_generation": 0,
            "error": None,
            "bulk_file_missing": False,
            "bulk_file_downloading": False,
            "bulk_file_download_percent": None,
        }
        assert factory.create_calls == []
    finally:
        blocker.set()
        ticks.stop()
        adapter.shutdown()
        adapter.wait_for_shutdown()


def test_live_adapter_reports_a_test_draft_bulk_download_failure(
    qcore_application: QCoreApplication,
) -> None:
    """A failed bulk download publishes the actionable error and stays retryable."""
    factory = _RecordingTestDraftFactory(
        set_codes=("hob",),
        bulk_file_missing_value=True,
        download_error=CardDatabaseError(
            "Failed to download Scryfall default-cards bulk data: connection reset"
        ),
    )
    arenas: list[_FakeSession] = []

    def session_factory(publish: SnapshotPublisher) -> LiveSession:
        arena = _FakeSession(publish=publish)
        arenas.append(arena)
        return cast(LiveSession, arena)

    adapter = LiveSessionAdapter(
        session_factory=session_factory,
        poll_interval_ms=5,
        test_draft_factory=cast("TestDraftFactory", factory),
    )
    adapter.start()
    try:
        _process_until(
            application=qcore_application,
            predicate=lambda: bool(arenas) and bool(arenas[0].poll_thread_ids),
            description="the initial Arena poll",
        )
        adapter.downloadTestDraftBulkFile()
        _process_until(
            application=qcore_application,
            predicate=lambda: adapter.state["test_draft"]["phase"] == "failed",
            description="the reported Mocked Draft bulk download failure",
        )
        assert adapter.state["test_draft"]["error"] == (
            "Failed to download Scryfall default-cards bulk data: connection reset"
        )
        assert adapter.state["test_draft"]["bulk_file_missing"] is True
        assert adapter.state["test_draft"]["bulk_file_downloading"] is False
        assert adapter.state["test_draft"]["bulk_file_download_percent"] is None
        assert adapter.state["test_draft"]["pending"] is False

        polls_before = len(arenas[0].poll_thread_ids)
        _process_until(
            application=qcore_application,
            predicate=lambda: len(arenas[0].poll_thread_ids) >= polls_before + 2,
            description="Arena polling after the failed Mocked Draft bulk download",
        )

        # The worker kept its capability, so the dialog can offer the retry.
        adapter.downloadTestDraftBulkFile()
        _process_until(
            application=qcore_application,
            predicate=lambda: factory.download_calls == 2,
            description="the retried Mocked Draft bulk download",
        )
        assert adapter.state["test_draft"]["phase"] == "failed"
        assert factory.create_calls == []
    finally:
        adapter.shutdown()
        adapter.wait_for_shutdown()
