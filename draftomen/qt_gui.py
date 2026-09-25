"""Launch the Draftomen PySide6 and QML desktop application.
Select the live application provider or deterministic mock provider explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol, cast

from PySide6.QtCore import QCoreApplication, QObject, QTimer, QUrl, Qt
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen import __version__
from draftomen.augmented_model_client import AugmentedModelClient
from draftomen.card_data_client import CardDataClient, cached_card_data_set_codes
from draftomen.carddb import (
    build_card_database_from_bulk_file,
    download_scryfall_default_cards_bulk_file,
)
from draftomen.cardimages import CardImageService, card_image_cache_dir
from draftomen.draftmancer_server import MockedDraftServer
from draftomen.mock_session import MOCK_SCENARIOS, MockLiveSession, MockScenario
from draftomen.paths import resolve_player_log_path
from draftomen.preferences import GuiDisplayPreferences, load_gui_preferences
from draftomen.qt_adapter import (
    GuiPreferencesAdapter,
    LiveSessionAdapter,
    SessionAdapter,
    SessionFactory,
    TestDraftFactory,
)
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import LiveSession, SnapshotPublisher
from draftomen.test_draft import (
    DEFAULT_TEST_DRAFT_SERVER_URL,
    DEFAULT_TEST_DRAFT_TIMEOUT_SECONDS,
    TestDraftRuntime,
    TestDraftSet,
    create_test_draft_runtime,
    default_test_draft_bulk_file,
    default_test_draft_checkout_dir,
    supported_test_draft_sets,
)
from draftomen.profile_client import (
    BUNDLED_PROFILE_BYTES,
    BUNDLED_PROFILE_EVENT_FORMAT,
    BUNDLED_PROFILE_SET_CODE,
    BUNDLED_PROFILE_SHA256,
    ProfileClient,
    ProfileNetworkPolicy,
)


SURFACES = ("live", "build", "backtest", "settings")
ProviderName = Literal["live", "mock"]
APPLICATION_NAME = "Draft Omen"
DEFAULT_PROFILE_MANIFEST_URL = "https://www.draftomen.com/profiles/manifest.json"
TEST_DRAFT_SMOKE_SUMMARY_PREFIX = "Test Draft smoke: "
TEST_DRAFT_SMOKE_TIMEOUT_SECONDS = 900.0
TEST_DRAFT_MANUAL_PICK_COUNT = 5


def _configure_application_metadata(*, application: QGuiApplication) -> None:
    """Set the Qt application metadata.
    Use the canonical package name and version.
    """
    application.setApplicationName(APPLICATION_NAME)
    application.setApplicationDisplayName(APPLICATION_NAME)
    application.setApplicationVersion(__version__)
    application.setOrganizationName(APPLICATION_NAME)


def _qml_directory(*, executable_path: Path | None = None) -> Path:
    """Resolve QML beside the source package or compiled bundle executable."""
    source_directory = Path(__file__).with_name("qml")
    if source_directory.is_dir():
        return source_directory

    bundle_executable = (
        Path(sys.argv[0]) if executable_path is None else executable_path
    )
    return bundle_executable.resolve().parent / "qml"


def _fixed_font_family() -> str:
    if QGuiApplication.instance() is None:
        raise RuntimeError(
            "QGuiApplication must exist before resolving the fixed font."
        )
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def _parser(*, forced_provider: ProviderName | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch the Draft Omen desktop application.",
    )
    parser.add_argument(
        "--provider",
        choices=("live", "mock"),
        default="live" if forced_provider is None else forced_provider,
        help="Use production services or deterministic visual-development data.",
    )
    parser.add_argument(
        "--scenario",
        choices=MOCK_SCENARIOS,
        default="ready",
        help="Initial representative state in mock mode.",
    )
    parser.add_argument(
        "--surface",
        choices=SURFACES,
        default="live",
        help="Initial application surface.",
    )
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--log-path", type=Path, default=None)
    parser.add_argument("--app-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--verify-bundled-profile",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--bulk-file",
        type=Path,
        default=None,
        help="Use a local Scryfall JSONL card database in live mode.",
    )
    parser.add_argument(
        "--profile-manifest-url",
        default=None,
        help=(
            "Override the hosted set-profile manifest URL. "
            f"Defaults to {DEFAULT_PROFILE_MANIFEST_URL}."
        ),
    )
    parser.add_argument(
        "--offline-profiles",
        action="store_true",
        help="Use only cached set profiles; do not access the hosted manifest.",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.set_defaults(startup_scan=True)
    parser.add_argument(
        "--no-startup-scan",
        dest="startup_scan",
        action="store_false",
        help="Skip live startup log recovery.",
    )
    parser.add_argument(
        "--draftmancer-dir",
        type=Path,
        default=None,
        help=(
            "Enable the developer Mocked Draft with a pinned Draftmancer checkout; "
            "overrides the persisted setting."
        ),
    )
    parser.add_argument(
        "--scryfall-bulk-file",
        type=Path,
        default=None,
        help=(
            "Scryfall JSONL bulk source used to resolve simulated printing identities "
            "(default: the application data directory)."
        ),
    )
    parser.add_argument(
        "--test-draft-server-url",
        default=None,
        help=f"Draftmancer server URL (default: {DEFAULT_TEST_DRAFT_SERVER_URL}).",
    )
    parser.add_argument(
        "--test-draft-timeout",
        type=float,
        default=DEFAULT_TEST_DRAFT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Render the window and exit automatically.",
    )
    parser.add_argument(
        "--smoke-test-until-complete",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--test-draft-smoke",
        nargs="?",
        choices=("auto", "manual"),
        const="auto",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        help="Save the rendered window before exiting.",
    )
    return parser


def _preflight_bundled_profile(*, app_dir: Path | None) -> bool:
    """Verify the bundled baseline through the offline client.
    The selected flat cache path must remain absent throughout verification.
    """
    cache_path: Path | None = None
    try:
        client = ProfileClient(
            app_dir=app_dir,
            network_policy=ProfileNetworkPolicy.OFFLINE,
        )
        cache_path = client.profile_path(
            BUNDLED_PROFILE_SET_CODE,
            BUNDLED_PROFILE_EVENT_FORMAT,
        )
        if cache_path.exists() or cache_path.is_symlink():
            raise RuntimeError("the flat bundled-profile cache already exists")

        bundled_bytes = client.bundled_profile_path.read_bytes()
        if len(bundled_bytes) != BUNDLED_PROFILE_BYTES:
            raise RuntimeError("bundled profile byte count does not match")
        bundled_digest = hashlib.sha256(bundled_bytes).hexdigest()
        if bundled_digest != BUNDLED_PROFILE_SHA256:
            raise RuntimeError("bundled profile digest does not match")

        loaded = client.load_cached(
            BUNDLED_PROFILE_SET_CODE,
            BUNDLED_PROFILE_EVENT_FORMAT,
        )
        if cache_path.exists() or cache_path.is_symlink():
            raise RuntimeError("offline profile load created the flat cache")
        if loaded.source != "bundled-metadata-only":
            raise RuntimeError("offline profile load did not use bundled metadata")
        profile = loaded.profile
        if (
            profile.set_code != BUNDLED_PROFILE_SET_CODE
            or profile.event_format != BUNDLED_PROFILE_EVENT_FORMAT
            or profile.maturity.value != "metadata-only"
        ):
            raise RuntimeError("bundled profile identity does not match")
        if profile.to_bytes() != bundled_bytes:
            raise RuntimeError("loaded bundled profile bytes do not match")
        if profile.fingerprint != BUNDLED_PROFILE_SHA256:
            raise RuntimeError("loaded bundled profile digest does not match")
        return True
    except Exception as error:  # noqa: BLE001 - preflight must fail closed.
        print(f"Bundled profile verification failed: {error}", file=sys.stderr)
        return False


def _live_session_factory(
    *,
    log_path: Path | None,
    app_dir: Path | None,
    bulk_file: Path | None,
    poll_interval: float,
    contextual_adjustments_enabled: bool,
    augmentation_enabled: bool = False,
    augmented_model_client: AugmentedModelClient | None = None,
    profile_manifest_url: str | None = None,
    profile_network_policy: ProfileNetworkPolicy = ProfileNetworkPolicy.ALLOWED,
    profile_client: ProfileClient | None = None,
) -> SessionFactory:
    if profile_client is None:
        profile_client = ProfileClient(
            app_dir=app_dir,
            manifest_url=(
                DEFAULT_PROFILE_MANIFEST_URL
                if profile_manifest_url is None
                else profile_manifest_url
            ),
            network_policy=profile_network_policy,
        )
    card_data_client = (
        None if bulk_file is not None else CardDataClient(app_dir=app_dir)
    )

    def factory(publish: SnapshotPublisher) -> LiveSession:
        common_kwargs = {
            "log_path": resolve_player_log_path(log_path=log_path),
            "app_dir": app_dir,
            "profile_client": profile_client,
            "poll_interval": poll_interval,
            "snapshot_publisher": publish,
            "augmentation_enabled": augmentation_enabled,
            "augmented_model_client": augmented_model_client,
            "card_image_service": CardImageService(
                cache_dir=card_image_cache_dir(app_dir=app_dir),
                timeout_seconds=2.0,
                max_attempts=1,
            ),
        }
        if bulk_file is not None:
            return LiveSession(
                **common_kwargs,
                contextual_adjustments_enabled=contextual_adjustments_enabled,
                card_database=build_card_database_from_bulk_file(path=bulk_file),
            )
        assert card_data_client is not None
        return LiveSession(
            **common_kwargs,
            contextual_adjustments_enabled=contextual_adjustments_enabled,
            set_card_data_loader=card_data_client.load,
        )

    return factory


def _resolved_profile_access(
    *,
    args: argparse.Namespace,
) -> tuple[str, ProfileNetworkPolicy]:
    """Resolve the hosted profile manifest URL and the network policy the GUI uses."""

    manifest_url = getattr(args, "profile_manifest_url", None)
    network_policy = (
        ProfileNetworkPolicy.OFFLINE
        if getattr(args, "offline_profiles", False)
        else ProfileNetworkPolicy.ALLOWED
    )
    return (
        DEFAULT_PROFILE_MANIFEST_URL if manifest_url is None else manifest_url,
        network_policy,
    )


@dataclass(frozen=True, slots=True)
class _MockedDraftSources:
    """Resolve the developer Mocked Draft inputs outside the process working directory."""

    checkout_dir: Path
    scryfall_bulk_file: Path
    server_url: str


def _mocked_draft_sources(
    *,
    args: argparse.Namespace,
    preferences: GuiDisplayPreferences,
) -> _MockedDraftSources:
    """Resolve Mocked Draft inputs from explicit flags, persisted settings, or application data."""

    # Per-launch input precedence: explicit flag, then persisted setting, then application-data
    # default. The simulated runtime state stays in its own temporary directory.
    checkout_value = args.draftmancer_dir or preferences.mocked_draft_checkout_dir
    checkout_dir = (
        Path(checkout_value).expanduser()
        if checkout_value
        else default_test_draft_checkout_dir(app_dir=args.app_dir)
    )
    bulk_file_value = (
        args.scryfall_bulk_file or preferences.mocked_draft_scryfall_bulk_file
    )
    scryfall_bulk_file = (
        Path(bulk_file_value).expanduser()
        if bulk_file_value
        else default_test_draft_bulk_file(app_dir=args.app_dir)
    )
    server_url = (
        args.test_draft_server_url
        or preferences.mocked_draft_server_url
        or DEFAULT_TEST_DRAFT_SERVER_URL
    )
    return _MockedDraftSources(
        checkout_dir=checkout_dir,
        scryfall_bulk_file=scryfall_bulk_file,
        server_url=server_url,
    )


def _mocked_draft_factory(
    *,
    args: argparse.Namespace,
    preferences: GuiDisplayPreferences,
    augmented_model_client: AugmentedModelClient | None = None,
) -> TestDraftFactory:
    """Build the developer Mocked Draft factory from resolved sources."""

    sources = _mocked_draft_sources(args=args, preferences=preferences)
    timeout_seconds = args.test_draft_timeout
    if not 0 < timeout_seconds < float("inf"):
        raise ValueError("--test-draft-timeout must be finite and positive.")
    manifest_url, network_policy = _resolved_profile_access(args=args)
    return _GuiTestDraftFactory(
        draftmancer_dir=sources.checkout_dir,
        scryfall_bulk_file=sources.scryfall_bulk_file,
        server_url=sources.server_url,
        timeout_seconds=timeout_seconds,
        app_dir=args.app_dir,
        profile_manifest_url=manifest_url,
        profile_network_policy=network_policy,
        simulation_app_dir=None,
        augmented_model_client=augmented_model_client,
    )


def _sync_mocked_draft_capability(
    *,
    provider: SessionAdapter,
    args: argparse.Namespace,
    preferences: GuiDisplayPreferences,
) -> None:
    """Install or clear the Mocked Draft capability from the current preferences."""

    # The user's live choice governs; a launch flag is only consulted for the sources.
    if not preferences.mocked_draft_enabled:
        provider.setTestDraftFactory(None)
        return
    try:
        augmented_model_client = getattr(provider, "_augmented_model_client", None)
        if not isinstance(augmented_model_client, AugmentedModelClient):
            augmented_model_client = None
        factory = _mocked_draft_factory(
            args=args,
            preferences=preferences,
            augmented_model_client=augmented_model_client,
        )
    except ValueError as error:
        print(f"Mocked Draft could not be enabled: {error}", file=sys.stderr)
        provider.setTestDraftFactory(None)
        return
    provider.setTestDraftFactory(factory)


class _GuiTestDraftFactory:
    """Create simulated draft runtimes from pinned developer sources.
    Supported sets come from the checkout and download card data on request.
    """

    def __init__(
        self,
        *,
        draftmancer_dir: Path,
        scryfall_bulk_file: Path,
        server_url: str,
        timeout_seconds: float,
        app_dir: Path | None,
        profile_manifest_url: str | None,
        profile_network_policy: ProfileNetworkPolicy,
        simulation_app_dir: Path | None,
        augmented_model_client: AugmentedModelClient | None = None,
    ) -> None:
        self._draftmancer_dir = draftmancer_dir
        self._scryfall_bulk_file = scryfall_bulk_file
        self._server = MockedDraftServer(
            configured_url=server_url,
            checkout_dir=draftmancer_dir,
        )
        self._timeout_seconds = timeout_seconds
        self._app_dir = app_dir
        self._profile_manifest_url = profile_manifest_url
        self._profile_network_policy = profile_network_policy
        self._simulation_app_dir = simulation_app_dir
        self._augmented_model_client = (
            AugmentedModelClient(app_dir=app_dir)
            if augmented_model_client is None
            else augmented_model_client
        )

    def supported_sets(self) -> tuple[TestDraftSet, ...]:
        """List the checkout's sets and whether each has cached card data."""
        return supported_test_draft_sets(
            draftmancer_dir=self._draftmancer_dir,
            cached_set_codes=cached_card_data_set_codes(app_dir=self._app_dir),
        )

    def download_card_data(self, *, set_code: str) -> None:
        """Download and validate one set's hosted card-data artifact into the cache."""

        CardDataClient(app_dir=self._app_dir).load(set_code, allow_network=True)

    def bulk_file_missing(self) -> bool:
        """Report whether the resolved Scryfall bulk source still needs a download."""

        return not self._scryfall_bulk_file.is_file()

    def download_bulk_file(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        """Download the Scryfall default-cards bulk source into its resolved path."""

        return download_scryfall_default_cards_bulk_file(
            destination=self._scryfall_bulk_file,
            should_stop=should_stop,
            progress=progress,
        )

    def ensure_server(self, *, should_stop: Callable[[], bool] | None = None) -> str:
        """Serve the pinned checkout when nothing answers the configured location."""
        return self._server.ensure_server(should_stop=should_stop)

    def release_server(self) -> None:
        """Stop the checkout this factory started; an adopted server is untouched."""
        self._server.release()

    def create_runtime(
        self,
        *,
        server_url: str,
        set_code: str,
        publisher: SnapshotPublisher,
        splash_enabled: bool,
        contextual_adjustments_enabled: bool,
    ) -> TestDraftRuntime:
        """Create one isolated simulated draft with the shared preferences."""
        return create_test_draft_runtime(
            draftmancer_dir=self._draftmancer_dir,
            scryfall_bulk_file=self._scryfall_bulk_file,
            server_url=server_url,
            set_code=set_code,
            timeout_seconds=self._timeout_seconds,
            source_app_dir=self._app_dir,
            profile_manifest_url=self._profile_manifest_url,
            profile_network_policy=self._profile_network_policy,
            snapshot_publisher=publisher,
            splash_enabled=splash_enabled,
            contextual_adjustments_enabled=contextual_adjustments_enabled,
            simulation_app_dir=self._simulation_app_dir,
            augmented_model_client=self._augmented_model_client,
            card_image_service=CardImageService(
                cache_dir=card_image_cache_dir(app_dir=self._app_dir),
                timeout_seconds=2.0,
                max_attempts=1,
            ),
        )


def _build_provider(
    *,
    args: argparse.Namespace,
    preferences: GuiDisplayPreferences,
) -> SessionAdapter:
    if args.provider == "mock":
        return MockSessionAdapter(
            session=MockLiveSession(scenario=args.scenario),
        )
    if args.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than zero.")
    resolved_profile_manifest_url, profile_network_policy = _resolved_profile_access(
        args=args
    )
    profile_client = ProfileClient(
        app_dir=args.app_dir,
        manifest_url=resolved_profile_manifest_url,
        network_policy=profile_network_policy,
    )
    augmented_model_client = AugmentedModelClient(app_dir=args.app_dir)
    # Startup enablement: the launch flag or the persisted Mocked Draft setting. A later
    # settings toggle replaces this choice with the user's live selection.
    test_draft_factory: TestDraftFactory | None = None
    if args.draftmancer_dir is not None or preferences.mocked_draft_enabled:
        test_draft_factory = _mocked_draft_factory(
            args=args,
            preferences=preferences,
            augmented_model_client=augmented_model_client,
        )
    return LiveSessionAdapter(
        session_factory=_live_session_factory(
            log_path=args.log_path,
            app_dir=args.app_dir,
            bulk_file=args.bulk_file,
            poll_interval=args.poll_interval,
            contextual_adjustments_enabled=preferences.contextual_adjustments_enabled,
            augmentation_enabled=preferences.augmented_intelligence_enabled,
            augmented_model_client=augmented_model_client,
            profile_manifest_url=resolved_profile_manifest_url,
            profile_network_policy=profile_network_policy,
            profile_client=profile_client,
        ),
        profile_client=profile_client,
        augmented_model_client=augmented_model_client,
        poll_interval_ms=max(1, round(args.poll_interval * 1000)),
        startup_scan=args.startup_scan,
        augmentation_enabled=preferences.augmented_intelligence_enabled,
        test_draft_factory=test_draft_factory,
    )


def _finish_smoke_test(
    *,
    engine: QQmlApplicationEngine,
    application: QGuiApplication,
    screenshot: Path | None,
) -> None:
    root_objects = engine.rootObjects()
    if not root_objects:
        application.exit(1)
        return
    if screenshot is not None:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        image = root_objects[0].grabWindow()
        if image.isNull() or not image.save(str(screenshot)):
            application.exit(1)
            return
        print(f"Saved GUI screenshot to {screenshot}")
    application.quit()


def _draft_complete_capture_ready(
    *,
    provider: SessionAdapter,
    deadline_passed: bool,
) -> bool:
    """Return whether the until-complete smoke test can capture the window.
    A fast replay completes before the hosted profile downloads, so capture waits for that refresh until the deadline.
    """

    if provider.state.get("status", {}).get("phase") != "draft_complete":
        return False
    pending = getattr(provider, "profile_refresh_pending", None)
    return deadline_passed or not (callable(pending) and pending())


def _finish_smoke_test_when_draft_completes(
    *,
    engine: QQmlApplicationEngine,
    application: QGuiApplication,
    provider: SessionAdapter,
    screenshot: Path | None,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = monotonic() + timeout_seconds
    timer = QTimer(application)

    def stop_waiting() -> None:
        timer.stop()
        timer.timeout.disconnect(finish_when_ready)
        timer.deleteLater()


    def finish_when_ready() -> None:
        if _draft_complete_capture_ready(
            provider=provider,
            deadline_passed=monotonic() >= deadline,
        ):
            stop_waiting()
            QTimer.singleShot(
                0,
                lambda: _finish_smoke_test(
                    engine=engine,
                    application=application,
                    screenshot=screenshot,
                ),
            )
        elif monotonic() >= deadline:
            stop_waiting()
            QTimer.singleShot(0, lambda: application.exit(1))

    timer.setInterval(20)
    timer.timeout.connect(finish_when_ready)
    timer.start()


class _TestDraftSmokeDriver:
    """Drive one unattended Test Draft journey through the live adapter.
    Report the run summary on stdout and leave the simulated draft first.
    """

    def __init__(
        self,
        *,
        provider: SessionAdapter,
        timeout_seconds: float = TEST_DRAFT_SMOKE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._started = False
        self._leaving = False
        self._deadline = clock() + timeout_seconds

    def advance(self) -> int | None:
        """Advance the journey once and return an exit code when it ends.
        Return None while the simulated draft is still running.
        """
        test_draft = self._provider.state.get("test_draft", {})
        phase = test_draft.get("phase")
        if phase == "failed":
            print(
                f"Test Draft smoke failed: {test_draft.get('error')}",
                file=sys.stderr,
            )
            return 1
        # A capability with no set code (missing card data) never starts; report why.
        if not self._started and test_draft.get("error"):
            print(
                f"Test Draft smoke failed: {test_draft['error']}",
                file=sys.stderr,
            )
            return 1
        if test_draft.get("enabled") is not True:
            print(
                "Test Draft smoke requires the --draftmancer-dir opt-in.",
                file=sys.stderr,
            )
            return 1
        if not self._started:
            set_code = test_draft.get("default_set_code")
            if set_code:
                self._provider.startTestDraft("auto", set_code)
                self._started = True
        if (
            self._started
            and phase == "completed"
            and self._provider.state.get("build") is not None
            and not self._leaving
        ):
            build = self._provider.state["build"]
            summary = {
                "status": "ok",
                "mode": test_draft.get("mode"),
                "set_code": test_draft.get("set_code"),
                "picks": self._provider.state["pool"]["total_cards"],
                "deck_size": build["deck_size"],
                "selected_pair": build["selected_pair"],
            }
            print(
                f"{TEST_DRAFT_SMOKE_SUMMARY_PREFIX}"
                f"{json.dumps(summary, separators=(',', ':'), sort_keys=True)}"
            )
            self._provider.leaveTestDraft()
            self._leaving = True
        if self._leaving and phase == "idle" and test_draft.get("active") is False:
            return 0
        if self._clock() >= self._deadline:
            print(
                f"Test Draft smoke timed out after {self._timeout_seconds:g} seconds.",
                file=sys.stderr,
            )
            return 1
        return None


class _SmokeControls(Protocol):
    """Locate and activate the controls of one running application window."""

    def find(self, name: str) -> QObject | None:
        ...

    def activate(self, name: str) -> None:
        ...


def _find_visual_item(item: QQuickItem, object_name: str) -> QQuickItem | None:
    """Find one descendant item by object name.
    List delegates are only reachable through the visual child tree.
    """
    if item.objectName() == object_name:
        return item
    for child in item.childItems():
        found = _find_visual_item(child, object_name)
        if found is not None:
            return found
    return None


class _QmlSmokeControls:
    """Locate and activate one QML control inside the running application window."""

    def __init__(self, *, window: QObject) -> None:
        self._window = window

    def find(self, name: str) -> QObject | None:
        """Return the named control of the window, or None when it is absent."""
        found = self._window.findChild(QObject, name)
        if found is not None:
            return found
        content_item = self._window.property("contentItem")
        if not isinstance(content_item, QQuickItem):
            return None
        return _find_visual_item(content_item, name)

    def activate(self, name: str) -> None:
        """Focus the named control, press Space, and process the queued events."""
        item = self.find(name)
        if item is None:
            raise RuntimeError(f"the {name} control is not available")
        if item.property("visible") is not True:
            raise RuntimeError(f"the {name} control is not visible")
        cast(QQuickItem, item).forceActiveFocus()
        QTest.keyClick(self._window, Qt.Key_Space)
        QCoreApplication.processEvents()


class _TestDraftManualSmokeDriver:
    """Drive one Manual Test Draft journey through the real QML controls.
    Confirm consecutive picks and require published pool and pack state each time.
    """

    _STEP_OPEN_DIALOG = 0
    _STEP_DIALOG_VISIBLE = 1
    _STEP_MODE = 2
    _STEP_START = 3
    _STEP_DRAFTING = 4
    _STEP_DIALOG_CLOSED = 6
    _STEP_SELECT_ROW = 7
    _STEP_ROW_SELECTED = 8
    _STEP_PICK = 9
    _STEP_PICK_CONFIRMED = 10
    _STEP_REDRAWN = 11
    _STEP_LEAVE_DIALOG = 12
    _STEP_LEAVE_VISIBLE = 13
    _STEP_LEAVE = 14
    _STEP_LEFT = 15
    _STEP_DONE = 16

    def __init__(
        self,
        *,
        provider: SessionAdapter,
        controls: _SmokeControls,
        timeout_seconds: float = TEST_DRAFT_SMOKE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._provider = provider
        self._controls = controls
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._deadline = clock() + timeout_seconds
        self._step = self._STEP_OPEN_DIALOG
        self._started = False
        self._picks = 0
        self._non_top_rank = 0
        self._generation = 0
        self._pool_total = 0
        self._set_code: str | None = None
        self._heading = ""

    def advance(self) -> int | None:
        """Advance the journey once and return an exit code when it ends.
        Stay on each step until its control or state requirement holds.
        """
        if self._step == self._STEP_DONE:
            return 0
        if self._test_draft().get("enabled") is not True:
            print(
                "Test Draft smoke requires the --draftmancer-dir opt-in.",
                file=sys.stderr,
            )
            return 1
        try:
            reason = self._capability_failure()
            if reason is None:
                reason = self._run_step()
        except RuntimeError as error:
            reason = str(error)
        if reason is not None:
            return self._fail(reason)
        if self._step == self._STEP_DONE:
            return 0
        if self._clock() >= self._deadline:
            print(
                f"Test Draft smoke timed out after {self._timeout_seconds:g} seconds.",
                file=sys.stderr,
            )
            return 1
        return None

    def _run_step(self) -> str | None:
        """Run the current journey step and report the requirement it failed."""
        if self._step == self._STEP_OPEN_DIALOG:
            self._controls.activate("testDraftButton")
            self._step = self._STEP_DIALOG_VISIBLE
        elif self._step == self._STEP_DIALOG_VISIBLE:
            if self._control_visible("testDraftDialog"):
                self._step = self._STEP_MODE
        elif self._step == self._STEP_MODE:
            self._controls.activate("testDraftManualModeButton")
            self._step = self._STEP_START
        elif self._step == self._STEP_START:
            missing = self._missing_control("testDraftStartButton")
            if missing is not None:
                return missing
            if not self._control_ready("testDraftStartButton", enabled=True):
                return None
            self._started = True
            self._controls.activate("testDraftStartButton")
            self._step = self._STEP_DRAFTING
        elif self._step == self._STEP_DRAFTING:
            if self._offer_generation() == 1 and self._offer_ready(minimum_cards=1):
                self._pool_total = self._published_pool_total() or 0
                self._set_code = self._draft_set_code()
                self._heading = self._draft_heading() or ""
                self._step = self._STEP_DIALOG_CLOSED
        elif self._step == self._STEP_DIALOG_CLOSED:
            # The dialog closes itself once the draft it started is under way.
            if not self._control_visible("testDraftDialog"):
                self._step = self._STEP_SELECT_ROW
        elif self._step == self._STEP_SELECT_ROW:
            if self._offer_ready(minimum_cards=2):
                row = self._row_name()
                if row is None:
                    return None
                self._controls.activate(row)
                self._step = self._STEP_ROW_SELECTED
        elif self._step == self._STEP_ROW_SELECTED:
            top_grp_id = self._top_grp_id()
            if top_grp_id is not None and self._selected_grp_id() != top_grp_id:
                self._non_top_rank = 2
                self._step = self._STEP_PICK
        elif self._step == self._STEP_PICK:
            if self._offer_ready(minimum_cards=1):
                missing = self._missing_control("testDraftPickButton")
                if missing is not None:
                    return missing
                if not self._control_ready("testDraftPickButton", enabled=True):
                    return None
                self._generation = self._offer_generation()
                self._controls.activate("testDraftPickButton")
                self._step = self._STEP_PICK_CONFIRMED
        elif self._step == self._STEP_PICK_CONFIRMED:
            if self._offer_generation() == self._generation + 1:
                pool_total = self._published_pool_total()
                if pool_total is None:
                    return "the published pool total is missing"
                if pool_total != self._pool_total + 1:
                    return (
                        f"the pool grew to {pool_total} cards instead of "
                        f"{self._pool_total + 1}"
                    )
                self._pool_total = pool_total
                self._step = self._STEP_REDRAWN
        elif self._step == self._STEP_REDRAWN:
            heading = self._draft_heading()
            if heading is not None and heading != self._heading:
                self._heading = heading
                self._picks += 1
                self._step = (
                    self._STEP_LEAVE_DIALOG
                    if self._picks == TEST_DRAFT_MANUAL_PICK_COUNT
                    else self._STEP_PICK
                )
        elif self._step == self._STEP_LEAVE_DIALOG:
            self._controls.activate("testDraftButton")
            self._step = self._STEP_LEAVE_VISIBLE
        elif self._step == self._STEP_LEAVE_VISIBLE:
            if self._control_visible("testDraftDialog"):
                self._step = self._STEP_LEAVE
        elif self._step == self._STEP_LEAVE:
            missing = self._missing_control("testDraftLeaveButton")
            if missing is not None:
                return missing
            if not self._control_ready("testDraftLeaveButton", enabled=True):
                return None
            self._controls.activate("testDraftLeaveButton")
            self._step = self._STEP_LEFT
        elif self._step == self._STEP_LEFT:
            test_draft = self._test_draft()
            if (
                test_draft.get("phase") == "idle"
                and test_draft.get("active") is False
            ):
                self._publish_summary()
                self._step = self._STEP_DONE
        return None

    def _capability_failure(self) -> str | None:
        """Report the published capability state that stops the journey."""
        test_draft = self._test_draft()
        phase = test_draft.get("phase")
        if phase == "failed":
            return str(test_draft.get("error") or "the simulated draft failed")
        if not self._started and test_draft.get("error"):
            return str(test_draft["error"])
        if phase == "completed" and self._picks < TEST_DRAFT_MANUAL_PICK_COUNT:
            return f"the simulated draft completed after {self._picks} picks"
        return None

    def _publish_summary(self) -> None:
        """Print the journey summary the bundle helper validates.
        Leaving clears the capability, so the journey reports what it observed.
        """
        summary = {
            "status": "ok",
            "mode": "manual",
            "set_code": self._set_code,
            "picks": self._picks,
            "pool_total": self._pool_total,
            "non_top_rank": self._non_top_rank,
        }
        print(
            f"{TEST_DRAFT_SMOKE_SUMMARY_PREFIX}"
            f"{json.dumps(summary, separators=(',', ':'), sort_keys=True)}"
        )

    def _fail(self, reason: str) -> int:
        """Report one failed journey requirement and its exit code."""
        print(f"Test Draft smoke failed: {reason}", file=sys.stderr)
        return 1

    def _test_draft(self) -> dict[str, Any]:
        """Return the published Test Draft capability, empty when absent."""
        value = self._provider.state.get("test_draft")
        return value if isinstance(value, dict) else {}

    def _published_pool_total(self) -> int | None:
        """Return the published pool size, or None when it is absent."""
        pool = self._provider.state.get("pool")
        if not isinstance(pool, dict):
            return None
        total = pool.get("total_cards")
        return total if isinstance(total, int) else None

    def _offer_generation(self) -> int:
        """Return the published offer generation, zero when it is absent."""
        generation = self._test_draft().get("offer_generation")
        return generation if isinstance(generation, int) else 0

    def _draft_set_code(self) -> str | None:
        """Return the simulated set code the capability published."""
        set_code = self._test_draft().get("set_code")
        return set_code if isinstance(set_code, str) and set_code else None

    def _recommendation_cards(self) -> list[Any]:
        """Return the published offer rows, empty when there are none."""
        recommendations = self._provider.state.get("recommendations")
        if not isinstance(recommendations, dict):
            return []
        cards = recommendations.get("cards")
        return cards if isinstance(cards, list) else []

    def _offer_ready(self, *, minimum_cards: int) -> bool:
        """Report whether one settled offer carries enough published cards."""
        test_draft = self._test_draft()
        if test_draft.get("pending") is True or test_draft.get("phase") != "drafting":
            return False
        if self._offer_generation() <= 0:
            return False
        return len(self._recommendation_cards()) >= minimum_cards

    def _top_grp_id(self) -> int | None:
        """Return the top-ranked card identity of the published offer."""
        cards = self._recommendation_cards()
        if not cards or not isinstance(cards[0], dict):
            return None
        card = cards[0].get("card")
        if not isinstance(card, dict):
            return None
        grp_id = card.get("grp_id")
        return grp_id if isinstance(grp_id, int) else None

    def _selected_grp_id(self) -> int | None:
        """Return the selected recommendation identity, None when absent."""
        recommendations = self._provider.state.get("recommendations")
        if not isinstance(recommendations, dict):
            return None
        selected = recommendations.get("selected_grp_id")
        return selected if isinstance(selected, int) else None

    def _draft_heading(self) -> str | None:
        """Return the heading the live drafting view currently renders."""
        view = self._controls.find("liveDraftView")
        if view is None:
            return None
        heading = view.property("draftHeading")
        if not isinstance(heading, str) or not heading:
            return None
        return heading

    def _row_name(self) -> str | None:
        """Return the rank-two recommendation row the journey selects."""
        for name in ("wideRecommendationRow2", "narrowRecommendationRow2"):
            if self._control_ready(name):
                return name
        return None

    def _missing_control(self, name: str) -> str | None:
        """Report the required control the window does not carry at all."""
        if self._controls.find(name) is None:
            return f"the {name} control is not available"
        return None

    def _control_visible(self, name: str) -> bool:
        """Report whether the named control is on screen right now."""
        item = self._controls.find(name)
        return item is not None and item.property("visible") is True

    def _control_ready(self, name: str, *, enabled: bool = False) -> bool:
        """Report whether the named control is visible and usable right now."""
        item = self._controls.find(name)
        if item is None or item.property("visible") is not True:
            return False
        return not enabled or item.property("enabled") is True


def _test_draft_smoke_driver(
    *,
    journey: str,
    provider: SessionAdapter,
    window: QObject,
) -> _TestDraftSmokeDriver | _TestDraftManualSmokeDriver:
    """Select the Test Draft smoke journey the hidden flag asked for."""
    if journey == "manual":
        return _TestDraftManualSmokeDriver(
            provider=provider,
            controls=_QmlSmokeControls(window=window),
        )
    return _TestDraftSmokeDriver(provider=provider)


def run_gui(
    *,
    argv: Sequence[str] | None = None,
    forced_provider: ProviderName | None = None,
) -> int:
    args = _parser(forced_provider=forced_provider).parse_args(argv)
    if forced_provider is not None:
        args.provider = forced_provider

    if args.verify_bundled_profile and not _preflight_bundled_profile(
        app_dir=args.app_dir,
    ):
        return 1

    if args.test_draft_smoke and (
        args.provider != "live"
        or (
            args.draftmancer_dir is None
            and not load_gui_preferences(app_dir=args.app_dir)[0].mocked_draft_enabled
        )
    ):
        print(
            "--test-draft-smoke requires --draftmancer-dir or an enabled Mocked Draft "
            "setting with the live provider.",
            file=sys.stderr,
        )
        return 1

    QQuickStyle.setStyle("Fusion")
    application = QGuiApplication([sys.argv[0]])
    _configure_application_metadata(application=application)

    preferences = GuiPreferencesAdapter(app_dir=args.app_dir, parent=application)
    provider = _build_provider(args=args, preferences=preferences.preferences)

    def apply_mocked_draft_enabled(_enabled: bool) -> None:
        """Apply the Mocked Draft setting the user just toggled."""

        _sync_mocked_draft_capability(
            provider=provider,
            args=args,
            preferences=preferences.preferences,
        )

    def apply_mocked_draft_sources() -> None:
        """Rebuild the installed Mocked Draft capability after a source edit."""

        # A source edit is not an enablement choice: with the setting off, a capability
        # installed from a launch flag stays exactly as the user left it.
        if not preferences.preferences.mocked_draft_enabled:
            return
        _sync_mocked_draft_capability(
            provider=provider,
            args=args,
            preferences=preferences.preferences,
        )

    preferences.mockedDraftEnabledChanged.connect(apply_mocked_draft_enabled)
    preferences.mockedDraftSourcesChanged.connect(apply_mocked_draft_sources)
    preferences.contextualAdjustmentsEnabledChanged.connect(
        provider.setContextualScoringEnabled
    )
    preferences.augmentedIntelligenceEnabledChanged.connect(
        provider.setAugmentedIntelligenceEnabled
    )
    engine = QQmlApplicationEngine()
    qml_directory = _qml_directory()
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("applicationTitle", APPLICATION_NAME)
    context.setContextProperty("fixedFontFamily", _fixed_font_family())
    context.setContextProperty("applicationVersion", __version__)
    context.setContextProperty("initialSurface", args.surface)
    context.setContextProperty("initialWindowWidth", args.width)
    context.setContextProperty("initialWindowHeight", args.height)
    engine.setInitialProperties({"provider": provider})

    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    if not engine.rootObjects():
        return 1

    if isinstance(provider, LiveSessionAdapter):
        application.aboutToQuit.connect(provider.shutdown)
        provider.start()

    if args.test_draft_smoke:
        driver = _test_draft_smoke_driver(
            journey=args.test_draft_smoke,
            provider=provider,
            window=engine.rootObjects()[0],
        )
        test_draft_timer = QTimer(application)

        def advance_test_draft_smoke() -> None:
            code = driver.advance()
            if code is None:
                return
            test_draft_timer.stop()
            test_draft_timer.timeout.disconnect(advance_test_draft_smoke)
            application.exit(code)

        test_draft_timer.setInterval(20)
        test_draft_timer.timeout.connect(advance_test_draft_smoke)
        test_draft_timer.start()
    elif args.smoke_test_until_complete:
        _finish_smoke_test_when_draft_completes(
            engine=engine,
            application=application,
            provider=provider,
            screenshot=args.screenshot,
        )
    elif args.smoke_test or args.screenshot is not None:
        QTimer.singleShot(
            800,
            lambda: _finish_smoke_test(
                engine=engine,
                application=application,
                screenshot=args.screenshot,
            ),
        )
    exit_code = application.exec()
    preferences.shutdown()
    if isinstance(provider, LiveSessionAdapter):
        provider.shutdown()
        provider.wait_for_shutdown()
    del engine
    return exit_code


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())

