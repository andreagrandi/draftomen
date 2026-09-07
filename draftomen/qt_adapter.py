"""Translate shared live-session state and commands for Qt frontends.
Keep blocking session work in adapter-owned workers and QML values presentation-only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from os import PathLike
from typing import Any, Literal, TypeAlias, cast

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

SessionFactory = Callable[[SnapshotPublisher], LiveSession]
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
        return super().eventFilter(_watched, event)

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

    def _replace_preferences(self, **changes: bool) -> None:
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
        self._replace_state(state=cast(dict[str, Any], _to_qml_value(snapshot)))

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

    def __init__(self, *, session: LiveSession) -> None:
        super().__init__()
        self._session = session

    @Slot(object)
    def fetch(self, request: CardImageRequest) -> None:
        try:
            result = self._session.fetch_card_image(request=request)
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
    _imageFetchRequested = Signal(object)
    _imageScheduleRequested = Signal()
    _profileRefreshRequested = Signal(object)
    snapshotReady = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        poll_interval_ms: int,
        startup_scan: bool,
        profile_client: ProfileClient | None = None,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._poll_interval_ms = poll_interval_ms
        self._startup_scan = startup_scan
        self._profile_client = profile_client
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
        self._profile_thread: QThread | None = None
        self._profile_worker: _ProfileRefreshWorker | None = None
        self._profile_request_in_flight: ProfileRefreshRequest | None = None

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
        image_worker = _CardImageFetchWorker(session=session)
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

    @Slot()
    def start(self) -> None:
        self._startup_loading = True
        self._startup_snapshot = None
        try:
            self._session = self._session_factory(self._publish_snapshot)
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
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            if not self._stop_requested:
                self.failed.emit(str(error))
            self.stop()

    @Slot(object)
    def dispatch(self, command: LiveSessionCommand) -> None:
        if self._session is None or self._stop_requested:
            return
        try:
            self._session.dispatch(command=command)
            if isinstance(command, ChangeContextualScoring):
                return
            self._request_one_card_image()
            if self._profile_client is not None:
                self._request_profile_refresh()
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.failed.emit(str(error))

    @Slot()
    def _poll(self) -> bool:
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

        session = self._session
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
        self._imageFetchRequested.emit(request)

    def _request_profile_refresh(self) -> None:
        """Schedule the one pending profile refresh without blocking polling."""

        session = self._session
        if (
            session is None
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
        request_kind = self._image_request_kind
        self._image_request_in_flight = None
        self._image_request_kind = None
        session = self._session
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

    @Slot()
    def stop(self) -> None:
        self._stop_requested = True
        self._startup_loading = False
        self._startup_snapshot = None
        if self._stopped:
            return
        if self._timer is not None:
            self._timer.stop()
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

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        poll_interval_ms: int,
        startup_scan: bool = True,
        profile_client: ProfileClient | None = None,
        parent: QObject | None = None,
    ) -> None:
        if poll_interval_ms <= 0:
            raise ValueError("poll_interval_ms must be greater than zero.")
        super().__init__(parent=parent)
        self._session_factory = session_factory
        self._poll_interval_ms = poll_interval_ms
        self._startup_scan = startup_scan
        self._profile_client = profile_client
        self.thread: QThread | None = None
        self._worker: _LiveSessionWorker | None = None

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
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        self._commandRequested.connect(worker.dispatch, Qt.ConnectionType.QueuedConnection)
        worker.snapshotReady.connect(
            self._apply_snapshot,
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

    def _dispatch(self, *, command: LiveSessionCommand) -> None:
        self._commandRequested.emit(command)
