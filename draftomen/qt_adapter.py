"""Translate shared live-session state and commands for Qt frontends.
Keep blocking session work in adapter-owned workers and QML values presentation-only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, cast

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QCoreApplication,
    QEvent,
    QMetaObject,
    QModelIndex,
    QObject,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QFontInfo, QGuiApplication

from draftomen.preferences import (
    GuiDisplayPreferences,
    load_gui_preferences,
    save_gui_preferences,
)
from draftomen.profile_client import ProfileClient
from draftomen.ranking import RankingMode
from draftomen.session import (
    CardImageFetchResult,
    CardImageRequest,
    ChangeAiEnhancedSuggestions,
    ChangeContextualScoring,
    ChangeRanking,
    ChangeSplashPreference,
    ChooseAccount,
    ChooseRecommendation,
    DismissError,
    FocusBuildCard,
    LiveSession,
    LiveSessionCommand,
    LiveSessionSnapshot,
    ProfileRefreshRequest,
    RequestBacktest,
    RequestBuild,
    RequestRatingsDownload,
    RetryError,
    SnapshotPublisher,
)
from draftomen.test_draft import (
    DEFAULT_TEST_DRAFT_SERVER_URL,
    DEFAULT_TEST_DRAFT_SET_CODE,
    TestDraftError,
    TestDraftOfferIdentity,
    TestDraftRuntime,
    default_test_draft_bulk_file,
    default_test_draft_checkout_dir,
)

SessionFactory = Callable[[SnapshotPublisher], LiveSession]

TestDraftMode: TypeAlias = Literal["manual", "auto"]
TestDraftSource: TypeAlias = Literal["arena", "test-draft"]
TestDraftPhase: TypeAlias = Literal["idle", "starting", "drafting", "completed", "failed"]

TEST_DRAFT_MODES: tuple[TestDraftMode, ...] = ("manual", "auto")


@dataclass(frozen=True, slots=True)
class TestDraftSessionState:
    """Publish the native Test Draft capability, source, and pick token."""

    enabled: bool = False
    active: bool = False
    phase: TestDraftPhase = "idle"
    mode: TestDraftMode | None = None
    set_code: str | None = None
    supported_set_codes: tuple[str, ...] = ()
    default_set_code: str | None = None
    pending: bool = False
    offer_generation: int = 0
    error: str | None = None


class TestDraftFactory(Protocol):
    """Serve simulated drafts, create their runtimes, and report the supported set codes."""

    def supported_set_codes(self) -> tuple[str, ...]:
        ...

    def ensure_server(self, *, should_stop: Callable[[], bool] | None = None) -> str:
        ...

    def create_runtime(
        self,
        *,
        server_url: str,
        set_code: str,
        publisher: SnapshotPublisher,
        splash_enabled: bool,
        contextual_adjustments_enabled: bool,
        ai_enhanced_suggestions_enabled: bool,
    ) -> TestDraftRuntime:
        ...

    def release_server(self) -> None:
        ...


_ImageRequestKind: TypeAlias = Literal["selected", "recommendation", "recent"]
_OMITTED_SNAPSHOT_FIELDS = frozenset(("current_pack_event", "current_scored_pack"))

_DEFAULT_APPLICATION_FONT_PIXEL_SIZE = 13


_WORKER_ERROR_ID = "qt-worker-error"

_GUI_PREFERENCES_SAVE_LOCK = threading.Lock()


def _to_qml_value(value: Any) -> Any:
    """Convert immutable application values into plain QML-safe values.
    Domain-only and event payloads never cross the frontend boundary.
    """

    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        converted = {
            field.name: _to_qml_value(getattr(value, field.name))
            for field in fields(value)
            if not field.name.startswith("domain_")
            and field.name not in _OMITTED_SNAPSHOT_FIELDS
        }
        image_path = converted.get("image_path")
        if image_path is not None:
            converted["image_path"] = QUrl.fromLocalFile(image_path).toString()
        return converted
    if isinstance(value, tuple):
        return [_to_qml_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _to_qml_value(item)
            for key, item in value.items()
        }
    return value


class RecommendationListModel(QAbstractListModel):
    """Publish complete recommendations with a display-only color projection.
    Filtering never changes the immutable recommendation values or ordering.
    """

    MODEL_DATA_ROLE = Qt.ItemDataRole.UserRole + 1
    ALL_FILTER_MODE = "all"
    ON_COLOR_FILTER_MODE = "on_color"

    filterModeChanged = Signal()
    currentColorsChanged = Signal()

    def __init__(self, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source_rows: list[dict[str, Any]] = []
        self._rows: list[dict[str, Any]] = []
        self._filter_mode = self.ALL_FILTER_MODE
        self._current_colors: tuple[str, ...] = ()

    def roleNames(self) -> dict[int, QByteArray]:
        return {self.MODEL_DATA_ROLE: QByteArray(b"modelData")}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        if role == self.MODEL_DATA_ROLE:
            return self._rows[index.row()]
        return None

    @Property(str, notify=filterModeChanged)
    def filterMode(self) -> str:
        return self._filter_mode

    @Property("QStringList", notify=currentColorsChanged)
    def currentColors(self) -> list[str]:
        return list(self._current_colors)

    @Slot(str)
    def setFilterMode(self, mode: str) -> None:
        if mode not in (self.ALL_FILTER_MODE, self.ON_COLOR_FILTER_MODE):
            raise ValueError(f"Unsupported recommendation filter mode: {mode!r}.")
        if mode == self._filter_mode:
            return
        self._filter_mode = mode
        self._replace_visible_rows()
        self.filterModeChanged.emit()

    def replace(
        self,
        *,
        rows: list[dict[str, Any]],
        current_colors: tuple[str, ...] | list[str] = (),
    ) -> None:
        next_source_rows = list(rows)
        next_current_colors = tuple(
            str(color).upper() for color in current_colors
        )
        colors_changed = next_current_colors != self._current_colors
        source_changed = next_source_rows != self._source_rows
        if not colors_changed and not source_changed:
            return

        self._source_rows = next_source_rows
        self._current_colors = next_current_colors
        self._replace_visible_rows()
        if colors_changed:
            self.currentColorsChanged.emit()

    def _replace_visible_rows(self) -> None:
        visible_rows = self._source_rows
        if self._filter_mode == self.ON_COLOR_FILTER_MODE:
            visible_rows = [
                row
                for row in visible_rows
                if self._row_matches_current_colors(row=row)
            ]

        if visible_rows == self._rows:
            return

        previous_count = len(self._rows)
        next_count = len(visible_rows)
        shared_count = min(previous_count, next_count)
        changed_indices = [
            index
            for index in range(shared_count)
            if self._rows[index] != visible_rows[index]
        ]

        if changed_indices:
            self._rows[:shared_count] = visible_rows[:shared_count]
            first_changed = changed_indices[0]
            last_changed = changed_indices[-1]
            self.dataChanged.emit(
                self.index(first_changed, 0),
                self.index(last_changed, 0),
                [self.MODEL_DATA_ROLE],
            )

        if next_count < previous_count:
            self.beginRemoveRows(
                QModelIndex(),
                next_count,
                previous_count - 1,
            )
            del self._rows[next_count:]
            self.endRemoveRows()
            return

        if next_count > previous_count:
            self.beginInsertRows(
                QModelIndex(),
                previous_count,
                next_count - 1,
            )
            self._rows.extend(visible_rows[previous_count:])
            self.endInsertRows()

    def _row_matches_current_colors(self, *, row: dict[str, Any]) -> bool:
        """Apply pair color semantics without changing recommendation values.
        Colorless cards remain eligible because they require no colored source.
        """
        card = row.get("card")
        if not isinstance(card, dict):
            return False
        colors = card.get("colors")
        if not isinstance(colors, (list, tuple)):
            return False
        card_colors = tuple(str(color).upper() for color in colors)
        if not card_colors:
            return True
        return all(color in self._current_colors for color in card_colors)


class _GuiPreferencesSaveThread(QThread):
    """Serialize immutable GUI preference snapshots on one worker thread.
    At most one snapshot is active; pending work is coalesced to the newest.
    """

    saveFinished = Signal(int, str)

    def __init__(
        self,
        *,
        app_dir: str | PathLike[str] | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_dir = app_dir
        self._condition = threading.Condition()
        self._pending: tuple[int, GuiDisplayPreferences] | None = None
        self._stop_requested = False

    def enqueue(
        self,
        *,
        generation: int,
        preferences: GuiDisplayPreferences,
    ) -> None:
        with self._condition:
            if self._stop_requested:
                return
            self._pending = (generation, preferences)
            self._condition.notify()

    def request_stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._condition.notify()

    def run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stop_requested:
                    self._condition.wait()
                if self._pending is None:
                    return
                generation, preferences = self._pending
                self._pending = None

            try:
                with _GUI_PREFERENCES_SAVE_LOCK:
                    persistence_message = save_gui_preferences(
                        preferences=preferences,
                        app_dir=self._app_dir,
                    )
            except Exception as error:  # pragma: no cover - defensive boundary.
                persistence_message = f"Could not save GUI preferences: {error}"
            self.saveFinished.emit(generation, persistence_message or "Saved")


class GuiPreferencesAdapter(QObject):
    """Expose persisted desktop display choices and contextual-scoring selection through narrow Qt properties.
    Functional changes still reach the live session through explicit commands.
    """

    preferencesChanged = Signal()
    persistenceChanged = Signal()
    applicationFontPixelSizeChanged = Signal()
    contextualAdjustmentsEnabledChanged = Signal(bool)
    mockedDraftEnabledChanged = Signal(bool)
    mockedDraftSourcesChanged = Signal()

    def __init__(
        self,
        *,
        app_dir: str | PathLike[str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._app_dir = app_dir
        self._preferences, self._persistence_message = load_gui_preferences(
            app_dir=app_dir,
        )
        resolved_app_dir = None if app_dir is None else Path(app_dir)
        self._default_mocked_draft_checkout_dir = default_test_draft_checkout_dir(
            app_dir=resolved_app_dir,
        )
        self._default_mocked_draft_bulk_file = default_test_draft_bulk_file(
            app_dir=resolved_app_dir,
        )
        self._save_generation = 0
        self._save_thread: _GuiPreferencesSaveThread | None = None
        self._closing = False

        application = QCoreApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.shutdown)
        if isinstance(application, QGuiApplication):
            application.installEventFilter(self)

    def __del__(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass

    def eventFilter(self, _watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ApplicationFontChange:
            self.applicationFontPixelSizeChanged.emit()
        return False

    @property
    def preferences(self) -> GuiDisplayPreferences:
        """Return the immutable preferences the adapter currently publishes."""

        return self._preferences

    @Property(bool, notify=preferencesChanged)
    def compactDensity(self) -> bool:
        return self._preferences.compact_density

    @Property(bool, notify=preferencesChanged)
    def secondaryStats(self) -> bool:
        return self._preferences.secondary_stats

    @Property(bool, notify=preferencesChanged)
    def cardPreview(self) -> bool:
        return self._preferences.card_preview

    @Property(bool, notify=preferencesChanged)
    def detailedBuildContext(self) -> bool:
        return self._preferences.detailed_build_context

    @Property(bool, notify=preferencesChanged)
    def systemTextScaling(self) -> bool:
        return self._preferences.system_text_scaling

    @Property(bool, notify=preferencesChanged)
    def showBacktest(self) -> bool:
        return self._preferences.show_backtest

    @Property(bool, notify=contextualAdjustmentsEnabledChanged)
    def contextualAdjustmentsEnabled(self) -> bool:
        return self._preferences.contextual_adjustments_enabled

    @Property(bool, notify=mockedDraftEnabledChanged)
    def mockedDraftEnabled(self) -> bool:
        return self._preferences.mocked_draft_enabled

    @Property(str, notify=mockedDraftSourcesChanged)
    def mockedDraftCheckoutDir(self) -> str:
        return (
            self._preferences.mocked_draft_checkout_dir
            or str(self._default_mocked_draft_checkout_dir)
        )

    @Property(str, notify=mockedDraftSourcesChanged)
    def mockedDraftServerUrl(self) -> str:
        return (
            self._preferences.mocked_draft_server_url
            or DEFAULT_TEST_DRAFT_SERVER_URL
        )

    @Property(str, notify=mockedDraftSourcesChanged)
    def mockedDraftScryfallBulkFile(self) -> str:
        return (
            self._preferences.mocked_draft_scryfall_bulk_file
            or str(self._default_mocked_draft_bulk_file)
        )

    @Property(int, notify=applicationFontPixelSizeChanged)
    def applicationFontPixelSize(self) -> int:
        application = QGuiApplication.instance()
        if not isinstance(application, QGuiApplication):
            return _DEFAULT_APPLICATION_FONT_PIXEL_SIZE
        pixel_size = QFontInfo(application.font()).pixelSize()
        return (
            pixel_size
            if pixel_size > 0
            else _DEFAULT_APPLICATION_FONT_PIXEL_SIZE
        )

    @Property(str, notify=persistenceChanged)
    def persistenceMessage(self) -> str:
        return self._persistence_message or "Saved"

    @Slot(bool)
    def setCompactDensity(self, enabled: bool) -> None:
        self._replace_preferences(compact_density=enabled)

    @Slot(bool)
    def setSecondaryStats(self, enabled: bool) -> None:
        self._replace_preferences(secondary_stats=enabled)

    @Slot(bool)
    def setCardPreview(self, enabled: bool) -> None:
        self._replace_preferences(card_preview=enabled)

    @Slot(bool)
    def setDetailedBuildContext(self, enabled: bool) -> None:
        self._replace_preferences(detailed_build_context=enabled)

    @Slot(bool)
    def setSystemTextScaling(self, enabled: bool) -> None:
        self._replace_preferences(system_text_scaling=enabled)

    @Slot(bool)
    def setShowBacktest(self, enabled: bool) -> None:
        self._replace_preferences(show_backtest=enabled)

    @Slot(bool)
    def setContextualAdjustmentsEnabled(self, enabled: bool) -> None:
        self._replace_preferences(contextual_adjustments_enabled=enabled)

    @Slot(bool)
    def setMockedDraftEnabled(self, enabled: bool) -> None:
        self._replace_preferences(mocked_draft_enabled=enabled)

    @Slot(str)
    def setMockedDraftCheckoutDir(self, value: str) -> None:
        self._replace_preferences(mocked_draft_checkout_dir=value.strip())

    @Slot(str)
    def setMockedDraftServerUrl(self, value: str) -> None:
        self._replace_preferences(mocked_draft_server_url=value.strip())

    @Slot(str)
    def setMockedDraftScryfallBulkFile(self, value: str) -> None:
        self._replace_preferences(mocked_draft_scryfall_bulk_file=value.strip())

    @Slot()
    def shutdown(self) -> None:
        self._closing = True
        thread = self._save_thread
        if thread is None:
            return
        thread.request_stop()
        thread.wait()

    def _ensure_save_thread(self) -> _GuiPreferencesSaveThread:
        thread = self._save_thread
        if thread is None:
            thread = _GuiPreferencesSaveThread(
                app_dir=self._app_dir,
                parent=self,
            )
            thread.saveFinished.connect(
                self._apply_save_result,
                Qt.ConnectionType.QueuedConnection,
            )
            self._save_thread = thread
        if not thread.isRunning():
            thread.start()
        return thread

    @Slot(int, str)
    def _apply_save_result(
        self,
        generation: int,
        persistence_message: str,
    ) -> None:
        if self._closing or generation != self._save_generation:
            return
        self._persistence_message = persistence_message
        self.persistenceChanged.emit()

    def _replace_preferences(self, **changes: Any) -> None:
        if self._closing:
            return
        previous = self._preferences
        updated = replace(previous, **changes)
        if updated == previous:
            return
        self._preferences = updated
        self._save_generation += 1
        generation = self._save_generation
        self._persistence_message = "Saving…"
        self.preferencesChanged.emit()
        self.persistenceChanged.emit()
        if (
            updated.contextual_adjustments_enabled
            != previous.contextual_adjustments_enabled
        ):
            self.contextualAdjustmentsEnabledChanged.emit(
                updated.contextual_adjustments_enabled
            )
        if updated.mocked_draft_enabled != previous.mocked_draft_enabled:
            self.mockedDraftEnabledChanged.emit(updated.mocked_draft_enabled)
        if (
            updated.mocked_draft_checkout_dir != previous.mocked_draft_checkout_dir
            or updated.mocked_draft_server_url != previous.mocked_draft_server_url
            or updated.mocked_draft_scryfall_bulk_file
            != previous.mocked_draft_scryfall_bulk_file
        ):
            self.mockedDraftSourcesChanged.emit()
        self._ensure_save_thread().enqueue(
            generation=generation,
            preferences=updated,
        )


class SessionAdapter(QObject):
    """Expose one QML-facing provider contract for mock and live sessions.
    Subclasses choose synchronous mock dispatch or queued live dispatch.
    """

    stateChanged = Signal()
    scenarioChanged = Signal()

    def __init__(
        self,
        *,
        snapshot: LiveSessionSnapshot | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._state: dict[str, Any] = {}
        self._state_before_failure: dict[str, Any] | None = None
        self._test_draft_state: dict[str, Any] = _to_qml_value(
            TestDraftSessionState()
        )
        self._recommendations_model = RecommendationListModel(parent=self)
        self._publish(snapshot=LiveSessionSnapshot() if snapshot is None else snapshot)

    @Property("QVariantMap", notify=stateChanged)
    def state(self) -> dict[str, Any]:
        return self._state

    @Property(QObject, constant=True)
    def recommendationsModel(self) -> RecommendationListModel:
        return self._recommendations_model

    @Property(bool, constant=True)
    def mockMode(self) -> bool:
        return False

    @Property(str, notify=scenarioChanged)
    def scenario(self) -> str:
        return ""

    @Property("QStringList", notify=scenarioChanged)
    def scenarios(self) -> list[str]:
        return []

    @Slot(str)
    def selectScenario(self, scenario: str) -> None:
        del scenario

    @Slot(str)
    def chooseAccount(self, account_id: str) -> None:
        self._dispatch(command=ChooseAccount(account_id=account_id))

    @Slot(int)
    def chooseRecommendation(self, grp_id: int) -> None:
        self._dispatch(command=ChooseRecommendation(grp_id=grp_id))

    @Slot(int)
    def focusBuildCard(self, grp_id: int) -> None:
        self._dispatch(command=FocusBuildCard(grp_id=grp_id))

    @Slot(str)
    def changeRanking(self, ranking_mode: str) -> None:
        self._dispatch(
            command=ChangeRanking(
                ranking_mode=cast(RankingMode, ranking_mode),
            )
        )

    @Slot(bool)
    def setSplashEnabled(self, enabled: bool) -> None:
        self._dispatch(command=ChangeSplashPreference(enabled=enabled))

    @Slot(bool)
    def setContextualScoringEnabled(self, enabled: bool) -> None:
        self._dispatch(command=ChangeContextualScoring(enabled=enabled))

    @Slot(bool)
    def setAiEnhancedSuggestionsEnabled(self, enabled: bool) -> None:
        self._dispatch(command=ChangeAiEnhancedSuggestions(enabled=enabled))

    @Slot()
    def requestRatings(self) -> None:
        ratings = self._state.get("ratings", {})
        set_code = ratings.get("set_code")
        if not set_code:
            return
        self._dispatch(command=RequestRatingsDownload(set_code=set_code))

    @Slot(str)
    def requestBuild(self, pair_override: str) -> None:
        recommendations = self._state.get("recommendations", {})
        self._dispatch(
            command=RequestBuild(
                pair_override=pair_override or None,
                allow_splash=bool(recommendations.get("splash_enabled", True)),
            )
        )

    @Slot()
    def requestBacktest(self) -> None:
        self._dispatch(command=RequestBacktest())

    @Slot(str)
    def dismissError(self, error_id: str) -> None:
        if error_id == _WORKER_ERROR_ID and self._state_before_failure is not None:
            state = self._state_before_failure
            self._state_before_failure = None
            self._replace_state(state=state)
            return
        self._dispatch(command=DismissError(error_id=error_id))

    @Slot(str)
    def retryError(self, error_id: str) -> None:
        self._dispatch(command=RetryError(error_id=error_id))

    @Slot(str, str)
    def startTestDraft(self, mode: str, set_code: str) -> None:
        del mode, set_code

    @Slot(int, int)
    def pickTestDraft(self, grp_id: int, offer_generation: int) -> None:
        del grp_id, offer_generation

    @Slot()
    def leaveTestDraft(self) -> None:
        return

    def setTestDraftFactory(self, test_draft_factory: TestDraftFactory | None) -> None:
        """Ignore a Mocked Draft factory change for frontends without the capability."""

        del test_draft_factory

    def _dispatch(self, *, command: LiveSessionCommand) -> None:
        raise NotImplementedError

    @Slot(object)
    def _apply_snapshot(self, snapshot: LiveSessionSnapshot) -> None:
        self._state_before_failure = None
        self._publish(snapshot=snapshot)

    @Slot(str)
    def _apply_failure(self, message: str) -> None:
        if self._state_before_failure is None:
            self._state_before_failure = self._state
        state = dict(self._state)
        state["status"] = {
            "phase": "error",
            "message": "Draft Omen could not complete background work.",
        }
        state["progress"] = None
        state["errors"] = [
            {
                "error_id": _WORKER_ERROR_ID,
                "code": "qt_worker_error",
                "message": message,
                "recoverable": False,
                "operation": None,
            }
        ]
        self._replace_state(state=state)

    def _publish(self, *, snapshot: LiveSessionSnapshot) -> None:
        state = cast(dict[str, Any], _to_qml_value(snapshot))
        state["test_draft"] = self._test_draft_state_value()
        self._replace_state(state=state)

    def _test_draft_state_value(self) -> dict[str, Any]:
        return self._test_draft_state

    def _replace_state(self, *, state: dict[str, Any]) -> None:
        if state == self._state:
            return
        self._state = state
        recommendations = state.get("recommendations") or {}
        pool = state.get("pool") or {}
        rows = recommendations.get("cards") or []
        self._recommendations_model.replace(
            rows=list(rows),
            current_colors=list(pool.get("current_colors") or []),
        )
        self.stateChanged.emit()


class _CardImageFetchWorker(QObject):
    """Execute one session-owned card image fetch outside the session thread."""

    resultReady = Signal(object, object, str)

    def __init__(self) -> None:
        super().__init__()

    @Slot(object, object)
    def fetch(self, session: LiveSession, request: CardImageRequest) -> None:
        try:
            result = session.fetch_card_image(request=request)
        except Exception as error:  # pragma: no cover - network boundary.
            self.resultReady.emit(request, None, str(error))
        else:
            self.resultReady.emit(request, result, "")


class _ProfileRefreshWorker(QObject):
    """Execute one blocking profile refresh outside the live-session thread."""

    resultReady = Signal(object, object, str)

    def __init__(self, *, profile_client: ProfileClient) -> None:
        super().__init__()
        self._profile_client = profile_client

    @Slot(object)
    def refresh(self, request: object) -> None:
        try:
            if not hasattr(request, "set_code") or not hasattr(request, "event_format"):
                raise TypeError("Profile refresh worker received an invalid request.")
            result = self._profile_client.refresh(
                set_code=request.set_code,
                event_format=request.event_format,
                force=request.force,
            )
        except Exception as error:  # pragma: no cover - network boundary.
            self.resultReady.emit(request, None, str(error))
        else:
            self.resultReady.emit(request, result, "")


class _LiveSessionWorker(QObject):
    _imageFetchRequested = Signal(object, object)
    _imageScheduleRequested = Signal()
    _profileRefreshRequested = Signal(object)
    snapshotReady = Signal(object)
    testDraftStateReady = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        poll_interval_ms: int,
        startup_scan: bool,
        profile_client: ProfileClient | None = None,
        test_draft_factory: TestDraftFactory | None = None,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._poll_interval_ms = poll_interval_ms
        self._startup_scan = startup_scan
        self._profile_client = profile_client
        self._test_draft_factory = test_draft_factory
        self._session: LiveSession | None = None
        self._timer: QTimer | None = None
        self._stop_requested = False
        self._stopped = False
        self._startup_loading = False
        self._startup_snapshot: LiveSessionSnapshot | None = None
        self._image_thread: QThread | None = None
        self._image_worker: _CardImageFetchWorker | None = None
        self._image_request_in_flight: CardImageRequest | None = None
        self._image_request_kind: _ImageRequestKind | None = None
        self._image_session: LiveSession | None = None
        self._image_source_generation = 0
        self._profile_thread: QThread | None = None
        self._profile_worker: _ProfileRefreshWorker | None = None
        self._profile_request_in_flight: ProfileRefreshRequest | None = None
        self._profile_source_generation = 0
        self._test_draft_runtime: TestDraftRuntime | None = None
        self._test_draft_mode: TestDraftMode | None = None
        self._test_draft_set_code: str | None = None
        self._test_draft_offer: TestDraftOfferIdentity | None = None
        self._test_draft_offer_generation = 0
        self._test_draft_pending = False
        self._test_draft_leaving = False
        self._test_draft_phase: TestDraftPhase = "idle"
        self._test_draft_error: str | None = None
        self._test_draft_supported_set_codes: tuple[str, ...] = ()
        self._test_draft_default_set_code: str | None = None
        self._authoritative_source: TestDraftSource = "arena"
        self._source_generation = 0
        self._runtime_generation = 0
        self._splash_enabled = True
        self._contextual_adjustments_enabled = True
        self._ai_enhanced_suggestions_enabled = True
        self._arena_ai_enabled = True

    def _arena_snapshot_publisher(self, snapshot: LiveSessionSnapshot) -> None:
        if self._authoritative_source != "arena":
            return
        self._publish_snapshot(snapshot)

    def _test_draft_snapshot_publisher(
        self,
        *,
        runtime_generation: int,
    ) -> SnapshotPublisher:
        def publish(snapshot: LiveSessionSnapshot) -> None:
            if (
                self._authoritative_source != "test-draft"
                or runtime_generation != self._runtime_generation
            ):
                return
            self._publish_snapshot(snapshot)

        return publish

    def _test_draft_interrupted(self) -> bool:
        """Report a leave or shutdown that already owns the simulated teardown."""
        return self._stop_requested or self._test_draft_leaving

    def _switch_source(self, *, source: TestDraftSource) -> None:
        """Change the authoritative source and drop stale in-flight auxiliary work."""
        self._authoritative_source = source
        self._source_generation += 1
        self._image_request_in_flight = None
        self._image_request_kind = None
        self._image_session = None
        self._profile_request_in_flight = None
        if self._timer is not None:
            if source == "arena":
                self._timer.start()
            else:
                self._timer.stop()

    def _active_session(self) -> LiveSession | None:
        runtime = self._test_draft_runtime
        if self._authoritative_source == "test-draft" and runtime is not None:
            return runtime.session
        return self._session

    def _publish_snapshot(self, snapshot: LiveSessionSnapshot) -> None:
        if self._stop_requested:
            return
        if self._startup_loading:
            self._startup_snapshot = snapshot
            return
        self.snapshotReady.emit(snapshot)

    def _flush_startup_snapshot(self) -> None:
        snapshot = self._startup_snapshot
        self._startup_snapshot = None
        self._startup_loading = False
        if snapshot is not None and not self._stop_requested:
            self.snapshotReady.emit(snapshot)

    def _start_image_worker(self) -> None:
        """Start the dedicated worker used for blocking image retrieval."""

        session = self._session
        if session is None or self._image_thread is not None:
            return
        thread = QThread(parent=self)
        image_worker = _CardImageFetchWorker()
        image_worker.moveToThread(thread)
        self._imageFetchRequested.connect(
            image_worker.fetch,
            Qt.ConnectionType.QueuedConnection,
        )
        # Queue completion so all LiveSession mutation stays on this worker.
        image_worker.resultReady.connect(
            self._image_fetch_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self._imageScheduleRequested.connect(
            self._request_one_card_image,
            Qt.ConnectionType.QueuedConnection,
        )
        thread.finished.connect(image_worker.deleteLater)
        self._image_thread = thread
        self._image_worker = image_worker
        thread.start()

    def _start_profile_worker(self) -> None:
        """Start the dedicated worker used for blocking profile refreshes."""

        profile_client = self._profile_client
        if profile_client is None or self._profile_thread is not None:
            return
        thread = QThread(parent=self)
        profile_worker = _ProfileRefreshWorker(profile_client=profile_client)
        profile_worker.moveToThread(thread)
        self._profileRefreshRequested.connect(
            profile_worker.refresh,
            Qt.ConnectionType.QueuedConnection,
        )
        profile_worker.resultReady.connect(
            self._profile_refresh_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        thread.finished.connect(profile_worker.deleteLater)
        self._profile_thread = thread
        self._profile_worker = profile_worker
        thread.start()


    def request_stop(self) -> None:
        """Request cooperative stop without queuing behind busy session work."""
        self._stop_requested = True
        self.request_test_draft_stop()

    @Slot()
    def start(self) -> None:
        self._startup_loading = True
        self._startup_snapshot = None
        try:
            self._session = self._session_factory(self._arena_snapshot_publisher)
            initial_snapshot = self._session.snapshot
            self._splash_enabled = initial_snapshot.recommendations.splash_enabled
            self._contextual_adjustments_enabled = (
                initial_snapshot.contextual_adjustments_enabled
            )
            self._ai_enhanced_suggestions_enabled = True
            self._arena_ai_enabled = True
            self._start_image_worker()
            self._start_profile_worker()
            self._publish_snapshot(self._session.snapshot)
            if self._stop_requested:
                self.stop()
                return
            if self._startup_scan:
                self._session.scan_startup_files(
                    include_previous=True,
                    include_pre_draft_detection=True,
                )
            if self._stop_requested:
                self.stop()
                return
            initial_poll_succeeded = self._poll()
            if self._stop_requested:
                self.stop()
                return
            if initial_poll_succeeded:
                self._publish_snapshot(self._session.snapshot)
                self._flush_startup_snapshot()
            else:
                self._startup_snapshot = None
                self._startup_loading = False
            self._timer = QTimer(self)
            self._timer.setInterval(self._poll_interval_ms)
            self._timer.timeout.connect(self._poll)
            self._timer.start()
            if self._test_draft_factory is not None and not self._stop_requested:
                self._refresh_test_draft_capability()
                self._publish_test_draft_state()
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            if not self._stop_requested:
                self.failed.emit(str(error))
            self.stop()

    @Slot(object)
    def dispatch(self, command: LiveSessionCommand) -> None:
        session = self._active_session()
        if session is None or self._stop_requested:
            return
        if self._authoritative_source == "test-draft" and isinstance(
            command, (ChooseAccount, RequestBacktest)
        ):
            return
        try:
            session.dispatch(command=command)
            if isinstance(command, ChangeSplashPreference):
                self._splash_enabled = command.enabled
            elif isinstance(command, ChangeContextualScoring):
                self._contextual_adjustments_enabled = command.enabled
            elif isinstance(command, ChangeAiEnhancedSuggestions):
                self._ai_enhanced_suggestions_enabled = command.enabled
            if self._authoritative_source == "arena" and isinstance(
                command, ChangeAiEnhancedSuggestions
            ):
                self._arena_ai_enabled = command.enabled
            if isinstance(command, ChangeContextualScoring):
                return
            self._request_one_card_image()
            if self._profile_client is not None:
                self._request_profile_refresh()
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.failed.emit(str(error))

    @Slot()
    def _poll(self) -> bool:
        if self._authoritative_source != "arena":
            return False
        if self._session is None:
            return False
        if self._stop_requested:
            self.stop()
            return False
        try:
            snapshot = self._session.poll_once()
            self._publish_snapshot(snapshot)
            self._request_one_card_image()
            if self._profile_client is not None:
                self._request_profile_refresh()
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.failed.emit(str(error))
            return False
        finally:
            if self._stop_requested:
                self.stop()
        return True

    def _request_one_card_image(self) -> None:
        """Schedule one image, prioritizing focus before pack thumbnails."""

        session = self._active_session()
        if (
            session is None
            or self._stop_requested
            or self._image_request_in_flight is not None
            or self._image_thread is None
        ):
            return

        request: CardImageRequest | None = None
        request_kind: _ImageRequestKind | None = None
        for kind, method_name in (
            ("selected", "selected_card_image_request"),
            ("recommendation", "recommendation_image_request"),
            ("recent", "recent_pick_image_request"),
        ):
            get_request = getattr(session, method_name, None)
            if not callable(get_request):
                continue
            request = get_request()
            if request is not None:
                request_kind = cast(_ImageRequestKind, kind)
                break
        if request is None or request_kind is None or self._stop_requested:
            return

        self._image_request_in_flight = request
        self._image_request_kind = request_kind
        self._image_session = session
        self._image_source_generation = self._source_generation
        self._imageFetchRequested.emit(session, request)

    def _request_profile_refresh(self) -> None:
        """Schedule the one pending profile refresh without blocking polling."""

        session = self._session
        if (
            self._authoritative_source != "arena"
            or session is None
            or self._profile_client is None
            or self._stop_requested
            or self._profile_request_in_flight is not None
            or self._profile_thread is None
        ):
            return
        get_request = getattr(session, "profile_refresh_request", None)
        if not callable(get_request):
            return
        request = get_request()
        if request is None:
            return
        self._profile_request_in_flight = request
        self._profile_source_generation = self._source_generation
        self._profileRefreshRequested.emit(request)

    @Slot(object, object, str)
    def _image_fetch_finished(
        self,
        request: CardImageRequest,
        result: object,
        error_message: str,
    ) -> None:
        """Apply the result in the session thread and queue the next request."""
        if request != self._image_request_in_flight:
            return
        if self._image_source_generation != self._source_generation:
            return
        request_kind = self._image_request_kind
        self._image_request_in_flight = None
        self._image_request_kind = None
        session = self._image_session
        self._image_session = None
        if session is None or self._stop_requested:
            return

        try:
            if error_message:
                if request_kind == "recommendation":
                    session.fail_recommendation_image_request(
                        request=request,
                        error_message=error_message,
                    )
                elif request_kind == "recent":
                    session.fail_recent_pick_image_request(
                        request=request,
                        error_message=error_message,
                    )
                else:
                    session.fail_card_image_request(
                        request=request,
                        error_message=error_message,
                    )
            else:
                if not isinstance(result, CardImageFetchResult):
                    raise TypeError("Card image worker returned an invalid result.")
                if request_kind == "recommendation":
                    session.complete_recommendation_image_request(
                        request=request,
                        image_path=result.image_path,
                        image_uri=result.image_uri,
                    )
                elif request_kind == "recent":
                    session.complete_recent_pick_image_request(
                        request=request,
                        image_path=result.image_path,
                        image_uri=result.image_uri,
                    )
                else:
                    session.complete_card_image_request(
                        request=request,
                        image_path=result.image_path,
                        image_uri=result.image_uri,
                    )
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.failed.emit(str(error))
        finally:
            self._imageScheduleRequested.emit()

    @Slot(object, object, str)
    def _profile_refresh_finished(
        self,
        request: ProfileRefreshRequest,
        result: object,
        error_message: str,
    ) -> None:
        """Apply refresh results on the live-session thread."""

        if request != self._profile_request_in_flight:
            return
        if self._profile_source_generation != self._source_generation:
            return
        self._profile_request_in_flight = None
        session = self._session
        if session is None or self._stop_requested:
            return

        try:
            if error_message:
                session.fail_profile_refresh(
                    request=request,
                    error_message=error_message,
                )
            else:
                session.complete_profile_refresh(
                    request=request,
                    result=result,
                )
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.failed.emit(str(error))
        finally:
            self._request_profile_refresh()

    def _publish_test_draft_state(self) -> None:
        if self._stop_requested:
            return
        self.testDraftStateReady.emit(
            TestDraftSessionState(
                enabled=self._test_draft_factory is not None,
                active=self._authoritative_source == "test-draft",
                phase=self._test_draft_phase,
                mode=self._test_draft_mode,
                set_code=self._test_draft_set_code,
                supported_set_codes=self._test_draft_supported_set_codes,
                default_set_code=self._test_draft_default_set_code,
                pending=self._test_draft_pending,
                offer_generation=self._test_draft_offer_generation,
                error=self._test_draft_error,
            )
        )

    @Slot(str, str)
    def start_test_draft(self, mode: str, set_code: str) -> None:
        """Create one simulated runtime and make it the authoritative source."""

        if self._test_draft_factory is None or self._stop_requested:
            return
        if self._test_draft_runtime is not None or self._test_draft_pending:
            return
        if self._test_draft_leaving:
            return
        if mode not in TEST_DRAFT_MODES:
            self._test_draft_phase = "failed"
            self._test_draft_error = f"Unsupported Test Draft mode: {mode}"
            self._publish_test_draft_state()
            return
        trimmed_set_code = set_code.strip()
        if not trimmed_set_code:
            self._test_draft_phase = "failed"
            self._test_draft_error = "The Test Draft set code must not be empty."
            self._publish_test_draft_state()
            return

        self._test_draft_pending = True
        self._test_draft_error = None
        self._test_draft_mode = cast(TestDraftMode, mode)
        self._test_draft_set_code = trimmed_set_code
        self._test_draft_phase = "starting"
        self._test_draft_offer = None
        self._test_draft_offer_generation = 0
        self._publish_test_draft_state()
        try:
            self._runtime_generation += 1
            generation = self._runtime_generation
            server_url = self._test_draft_factory.ensure_server(
                should_stop=self._test_draft_interrupted
            )
            if self._test_draft_interrupted():
                return
            runtime = self._test_draft_factory.create_runtime(
                server_url=server_url,
                set_code=trimmed_set_code,
                publisher=self._test_draft_snapshot_publisher(
                    runtime_generation=generation
                ),
                splash_enabled=self._splash_enabled,
                contextual_adjustments_enabled=self._contextual_adjustments_enabled,
                ai_enhanced_suggestions_enabled=self._ai_enhanced_suggestions_enabled,
            )
            if self._stop_requested or self._test_draft_leaving:
                runtime.close()
                self._runtime_generation += 1
                return
            self._test_draft_runtime = runtime
            if mode == "manual":
                inspection = runtime.controller.start()
                self._switch_source(source="test-draft")
                self._test_draft_offer = inspection.offer
                self._test_draft_offer_generation = 1
                self._test_draft_phase = "drafting"
                self._publish_test_draft_state()
                self._publish_snapshot(runtime.session.snapshot)
                self._request_one_card_image()
            else:
                self._switch_source(source="test-draft")
                self._test_draft_phase = "drafting"
                self._publish_test_draft_state()
                result = runtime.controller.run_auto()
                self._test_draft_phase = "completed"
                self._test_draft_offer = None
                self._test_draft_offer_generation = 0
                self._publish_snapshot(result.build)
                self._request_one_card_image()
        except Exception as error:
            if self._test_draft_interrupted():
                return
            startup_failure = (
                self._authoritative_source == "test-draft"
                and isinstance(error, TestDraftError)
                and error.stage == "startup"
            )
            # Fail before releasing: _fail_test_draft keeps the owned runtime while
            # the simulated source is authoritative, and releasing first would
            # restore Arena authority so the release would run a second time.
            self._fail_test_draft(message=str(error))
            if startup_failure:
                self._release_test_draft_runtime()
        finally:
            self._test_draft_pending = False
            self._publish_test_draft_state()

    def _fail_test_draft(self, *, message: str) -> None:
        """Report one Test Draft failure without disturbing an untouched Arena."""
        self._test_draft_phase = "failed"
        self._test_draft_error = message
        self._test_draft_offer = None
        self._test_draft_offer_generation = 0
        if self._authoritative_source == "test-draft":
            return  # runtime stays owned until leave/shutdown
        self._close_test_draft_runtime()

    @Slot(int, int)
    def pick_test_draft(self, grp_id: int, offer_generation: int) -> None:
        """Confirm one inspected simulated offer and publish the next pack."""

        runtime = self._test_draft_runtime
        if (
            self._stop_requested
            or self._test_draft_leaving
            or runtime is None
            or self._authoritative_source != "test-draft"
            or self._test_draft_mode != "manual"
            or self._test_draft_pending
            or self._test_draft_offer is None
            or offer_generation <= 0
            or offer_generation != self._test_draft_offer_generation
        ):
            return
        self._test_draft_pending = True
        self._publish_test_draft_state()
        try:
            step = runtime.controller.confirm(
                grp_id=grp_id,
                expected_offer=self._test_draft_offer,
            )
            self._publish_snapshot(step.after)
            if step.after.draft is not None and step.after.draft.completed:
                self._test_draft_offer = None
                self._test_draft_offer_generation = 0
                self._test_draft_phase = "completed"
            else:
                inspection = runtime.controller.inspect()
                self._test_draft_offer = inspection.offer
                self._test_draft_offer_generation += 1
                self._test_draft_phase = "drafting"
                self._request_one_card_image()
        except Exception as error:
            if self._stop_requested or self._test_draft_leaving:
                return
            self._fail_test_draft(message=str(error))
        finally:
            self._test_draft_pending = False
            self._publish_test_draft_state()

    def request_test_draft_stop(self) -> None:
        """Cancel blocked simulated work without queueing behind it."""
        self._test_draft_leaving = True
        runtime = self._test_draft_runtime
        if runtime is not None:
            runtime.cancel()

    @Slot()
    def leave_test_draft(self) -> None:
        """Return to Arena authority and release the simulated runtime."""

        if self._stop_requested:
            return
        self._release_test_draft_runtime()
        self._reset_test_draft_progress()
        self._publish_test_draft_state()

    @Slot(object)
    def set_test_draft_factory(self, test_draft_factory: TestDraftFactory | None) -> None:
        """Install or clear the developer Mocked Draft capability on the worker thread."""

        if self._stop_requested:
            return
        outgoing = self._test_draft_factory
        self._test_draft_factory = None
        self._release_test_draft_runtime()
        self._close_test_draft_runtime(factory=outgoing)
        self._reset_test_draft_progress()
        self._test_draft_factory = test_draft_factory
        self._refresh_test_draft_capability()
        self._publish_test_draft_state()

    def _release_test_draft_runtime(self) -> None:
        """Close the owned simulated runtime and restore Arena authority."""

        switched = self._authoritative_source == "test-draft"
        self._close_test_draft_runtime()
        if not switched:
            return
        self._switch_source(source="arena")
        try:
            self._restore_arena_preferences()
        except Exception as error:
            self.failed.emit(str(error))
        self._poll()

    def _reset_test_draft_progress(self) -> None:
        """Clear the published simulated-draft mode, offer, phase, and error."""

        self._test_draft_mode = None
        self._test_draft_set_code = None
        self._test_draft_offer = None
        self._test_draft_offer_generation = 0
        self._test_draft_phase = "idle"
        self._test_draft_error = None
        self._test_draft_leaving = False

    def _refresh_test_draft_capability(self) -> None:
        """Publish the installed factory's supported set codes and default set."""

        factory = self._test_draft_factory
        self._test_draft_supported_set_codes = ()
        self._test_draft_default_set_code = None
        if factory is None:
            return
        try:
            supported_codes = factory.supported_set_codes()
        except Exception as error:
            self._test_draft_error = str(error)
            return
        self._test_draft_supported_set_codes = supported_codes
        default_code = DEFAULT_TEST_DRAFT_SET_CODE.casefold()
        if default_code in supported_codes:
            self._test_draft_default_set_code = default_code
        elif supported_codes:
            self._test_draft_default_set_code = supported_codes[0]

    def _close_test_draft_runtime(
        self, *, factory: TestDraftFactory | None = None
    ) -> None:
        release = factory if factory is not None else self._test_draft_factory
        runtime = self._test_draft_runtime
        self._test_draft_runtime = None
        self._test_draft_offer = None
        self._test_draft_offer_generation = 0
        self._runtime_generation += 1
        if runtime is not None:
            try:
                runtime.cancel()
                runtime.close()
            except Exception as error:
                if not self._stop_requested:
                    self.failed.emit(str(error))
        if release is not None:
            try:
                release.release_server()
            except Exception as error:
                if not self._stop_requested:
                    self.failed.emit(str(error))

    def _restore_arena_preferences(self) -> None:
        session = self._session
        if session is None:
            return
        snapshot = session.snapshot
        if snapshot.recommendations.splash_enabled != self._splash_enabled:
            session.dispatch(command=ChangeSplashPreference(enabled=self._splash_enabled))
        if (
            snapshot.contextual_adjustments_enabled
            != self._contextual_adjustments_enabled
        ):
            session.dispatch(
                command=ChangeContextualScoring(
                    enabled=self._contextual_adjustments_enabled
                )
            )
        if self._arena_ai_enabled != self._ai_enhanced_suggestions_enabled:
            session.dispatch(
                command=ChangeAiEnhancedSuggestions(
                    enabled=self._ai_enhanced_suggestions_enabled
                )
            )
            self._arena_ai_enabled = self._ai_enhanced_suggestions_enabled

    @Slot()
    def stop(self) -> None:
        self._stop_requested = True
        self._startup_loading = False
        self._startup_snapshot = None
        if self._stopped:
            return
        if self._timer is not None:
            self._timer.stop()
        self._close_test_draft_runtime()
        if self._session is not None:
            try:
                self._session.stop()
            except Exception as error:  # pragma: no cover - defensive UI boundary.
                self.failed.emit(str(error))
        image_thread = self._image_thread
        if image_thread is not None:
            image_thread.quit()
            image_thread.wait()
            self._image_thread = None
            self._image_worker = None
        profile_thread = self._profile_thread
        if profile_thread is not None:
            profile_thread.quit()
            profile_thread.wait()
            self._profile_thread = None
            self._profile_worker = None
        self._profile_request_in_flight = None
        self._stopped = True
        self.finished.emit()


class LiveSessionAdapter(SessionAdapter):
    """Run the production live session on one adapter-owned QThread.
    Immutable snapshots return through queued Qt signals.
    """

    _commandRequested = Signal(object)
    _testDraftStartRequested = Signal(str, str)
    _testDraftPickRequested = Signal(int, int)
    _testDraftLeaveRequested = Signal()
    _testDraftFactoryChanged = Signal(object)

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        poll_interval_ms: int,
        startup_scan: bool = True,
        profile_client: ProfileClient | None = None,
        test_draft_factory: TestDraftFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be greater than zero.")
        super().__init__(parent=parent)
        self._session_factory = session_factory
        self._poll_interval_ms = poll_interval_ms
        self._startup_scan = startup_scan
        self._profile_client = profile_client
        self._test_draft_factory = test_draft_factory
        self.thread: QThread | None = None
        self._worker: _LiveSessionWorker | None = None
        self._test_draft_state: dict[str, Any] = _to_qml_value(
            TestDraftSessionState(enabled=test_draft_factory is not None)
        )
        self._replace_state(state=self._state | {"test_draft": self._test_draft_state})

    @Slot()
    def start(self) -> None:
        if self.thread is not None and self.thread.isRunning():
            return
        thread = QThread(parent=self)
        worker = _LiveSessionWorker(
            session_factory=self._session_factory,
            poll_interval_ms=self._poll_interval_ms,
            startup_scan=self._startup_scan,
            profile_client=self._profile_client,
            test_draft_factory=self._test_draft_factory,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        self._commandRequested.connect(worker.dispatch, Qt.ConnectionType.QueuedConnection)
        self._testDraftStartRequested.connect(
            worker.start_test_draft,
            Qt.ConnectionType.QueuedConnection,
        )
        self._testDraftPickRequested.connect(
            worker.pick_test_draft,
            Qt.ConnectionType.QueuedConnection,
        )
        self._testDraftLeaveRequested.connect(
            worker.leave_test_draft,
            Qt.ConnectionType.QueuedConnection,
        )
        self._testDraftFactoryChanged.connect(
            worker.set_test_draft_factory,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.snapshotReady.connect(
            self._apply_snapshot,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.testDraftStateReady.connect(
            self._apply_test_draft_state,
            Qt.ConnectionType.QueuedConnection,
        )
        worker.failed.connect(self._apply_failure, Qt.ConnectionType.QueuedConnection)
        thread.finished.connect(worker.deleteLater)
        worker.finished.connect(
            thread.quit,
            Qt.ConnectionType.DirectConnection,
        )
        self.thread = thread
        self._worker = worker
        thread.start()

    @Slot()
    def shutdown(self) -> None:
        thread = self.thread
        worker = self._worker
        if thread is None or worker is None or not thread.isRunning():
            return
        worker.request_stop()
        thread.requestInterruption()
        QMetaObject.invokeMethod(
            worker,
            "stop",
            Qt.ConnectionType.QueuedConnection,
        )
        thread.wait(100)

    def wait_for_shutdown(self) -> None:
        """Wait for the owned worker thread before the adapter is destroyed."""
        thread = self.thread
        if thread is not None and thread.isRunning():
            thread.wait()

    @Slot(str, str)
    def startTestDraft(self, mode: str, set_code: str) -> None:
        if self._test_draft_state.get("enabled") is not True or self._worker is None:
            return
        self._testDraftStartRequested.emit(mode, set_code)

    @Slot(int, int)
    def pickTestDraft(self, grp_id: int, offer_generation: int) -> None:
        if self._worker is None:
            return
        self._testDraftPickRequested.emit(grp_id, offer_generation)

    @Slot()
    def leaveTestDraft(self) -> None:
        worker = self._worker
        if worker is None:
            return
        # Unblock a blocked start or pick before the queued leave slot runs.
        worker.request_test_draft_stop()
        self._testDraftLeaveRequested.emit()

    def setTestDraftFactory(self, test_draft_factory: TestDraftFactory | None) -> None:
        """Install or clear the developer Mocked Draft capability at runtime."""

        if self._test_draft_factory is test_draft_factory:
            return
        self._test_draft_factory = test_draft_factory
        worker = self._worker
        if worker is not None:
            self._testDraftFactoryChanged.emit(test_draft_factory)
            return
        # A toggle between engine.load and provider.start(): the worker publishes
        # the authoritative capability as soon as it starts.
        self._test_draft_state = _to_qml_value(
            TestDraftSessionState(enabled=test_draft_factory is not None)
        )
        self._replace_state(state=self._state | {"test_draft": self._test_draft_state})

    @Slot(object)
    def _apply_test_draft_state(self, state: TestDraftSessionState) -> None:
        value = cast(dict[str, Any], _to_qml_value(state))
        if value == self._test_draft_state:
            return
        self._test_draft_state = value
        self._replace_state(state=self._state | {"test_draft": value})

    def _test_draft_state_value(self) -> dict[str, Any]:
        return self._test_draft_state

    def _dispatch(self, *, command: LiveSessionCommand) -> None:
        self._commandRequested.emit(command)
