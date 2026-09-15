"""Launch the Draftomen PySide6 and QML desktop application.
Select the live application provider or deterministic mock provider explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path
from time import monotonic
from typing import Literal, cast

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen import __version__
from draftomen.card_data_client import CardDataClient, cached_card_data_set_codes
from draftomen.carddb import build_card_database_from_bulk_file
from draftomen.cardimages import CardImageService, card_image_cache_dir
from draftomen.mock_session import MOCK_SCENARIOS, MockLiveSession, MockScenario
from draftomen.paths import resolve_player_log_path
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
    DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE,
    DEFAULT_TEST_DRAFT_SERVER_URL,
    DEFAULT_TEST_DRAFT_TIMEOUT_SECONDS,
    TestDraftRuntime,
    create_test_draft_runtime,
    supported_test_draft_set_codes,
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
        help="Enable the developer Test Draft with a pinned Draftmancer checkout.",
    )
    parser.add_argument(
        "--scryfall-bulk-file",
        type=Path,
        default=DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE,
        help="Scryfall JSONL bulk source used to resolve simulated printing identities.",
    )
    parser.add_argument("--test-draft-server-url", default=DEFAULT_TEST_DRAFT_SERVER_URL)
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


def _test_draft_runtime_factory(
    *,
    draftmancer_dir: Path,
    scryfall_bulk_file: Path,
    server_url: str,
    timeout_seconds: float,
    app_dir: Path | None,
    profile_manifest_url: str | None,
    profile_network_policy: ProfileNetworkPolicy,
    simulation_app_dir: Path | None = None,
) -> TestDraftFactory:
    """Create the developer Test Draft factory behind the explicit opt-in."""
    return _GuiTestDraftFactory(
        draftmancer_dir=draftmancer_dir,
        scryfall_bulk_file=scryfall_bulk_file,
        server_url=server_url,
        timeout_seconds=timeout_seconds,
        app_dir=app_dir,
        profile_manifest_url=profile_manifest_url,
        profile_network_policy=profile_network_policy,
        simulation_app_dir=simulation_app_dir,
    )


class _GuiTestDraftFactory:
    """Create simulated draft runtimes from pinned developer sources.
    Supported sets intersect the checkout with locally cached card data.
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
    ) -> None:
        self._draftmancer_dir = draftmancer_dir
        self._scryfall_bulk_file = scryfall_bulk_file
        self._server_url = server_url
        self._timeout_seconds = timeout_seconds
        self._app_dir = app_dir
        self._profile_manifest_url = profile_manifest_url
        self._profile_network_policy = profile_network_policy
        self._simulation_app_dir = simulation_app_dir

    def supported_set_codes(self) -> tuple[str, ...]:
        """List the sets the checkout and the local cache both provide."""
        return supported_test_draft_set_codes(
            draftmancer_dir=self._draftmancer_dir,
            draftomen_set_codes=cached_card_data_set_codes(app_dir=self._app_dir),
        )

    def create_runtime(
        self,
        *,
        set_code: str,
        publisher: SnapshotPublisher,
        splash_enabled: bool,
        contextual_adjustments_enabled: bool,
        ai_enhanced_suggestions_enabled: bool,
    ) -> TestDraftRuntime:
        """Create one isolated simulated draft with the shared preferences."""
        return create_test_draft_runtime(
            draftmancer_dir=self._draftmancer_dir,
            scryfall_bulk_file=self._scryfall_bulk_file,
            server_url=self._server_url,
            set_code=set_code,
            timeout_seconds=self._timeout_seconds,
            source_app_dir=self._app_dir,
            profile_manifest_url=self._profile_manifest_url,
            profile_network_policy=self._profile_network_policy,
            snapshot_publisher=publisher,
            splash_enabled=splash_enabled,
            contextual_adjustments_enabled=contextual_adjustments_enabled,
            ai_enhanced_suggestions_enabled=ai_enhanced_suggestions_enabled,
            simulation_app_dir=self._simulation_app_dir,
            card_image_service=CardImageService(
                cache_dir=card_image_cache_dir(app_dir=self._app_dir),
                timeout_seconds=2.0,
                max_attempts=1,
            ),
        )


def _build_provider(
    *,
    args: argparse.Namespace,
    contextual_adjustments_enabled: bool,
) -> SessionAdapter:
    if args.provider == "mock":
        return MockSessionAdapter(
            session=MockLiveSession(scenario=args.scenario),
        )
    if args.poll_interval <= 0:
        raise ValueError("--poll-interval must be greater than zero.")
    profile_manifest_url = getattr(args, "profile_manifest_url", None)
    profile_network_policy = (
        ProfileNetworkPolicy.OFFLINE
        if getattr(args, "offline_profiles", False)
        else ProfileNetworkPolicy.ALLOWED
    )
    resolved_profile_manifest_url = (
        DEFAULT_PROFILE_MANIFEST_URL
        if profile_manifest_url is None
        else profile_manifest_url
    )
    profile_client = ProfileClient(
        app_dir=args.app_dir,
        manifest_url=resolved_profile_manifest_url,
        network_policy=profile_network_policy,
    )
    test_draft_factory: TestDraftFactory | None = None
    draftmancer_dir = getattr(args, "draftmancer_dir", None)
    if draftmancer_dir is not None:
        test_draft_timeout = getattr(
            args,
            "test_draft_timeout",
            DEFAULT_TEST_DRAFT_TIMEOUT_SECONDS,
        )
        if not 0 < test_draft_timeout < float("inf"):
            raise ValueError("--test-draft-timeout must be finite and positive.")
        test_draft_factory = _test_draft_runtime_factory(
            draftmancer_dir=draftmancer_dir,
            scryfall_bulk_file=getattr(
                args,
                "scryfall_bulk_file",
                DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE,
            ),
            server_url=getattr(
                args,
                "test_draft_server_url",
                DEFAULT_TEST_DRAFT_SERVER_URL,
            ),
            timeout_seconds=test_draft_timeout,
            app_dir=args.app_dir,
            profile_manifest_url=resolved_profile_manifest_url,
            profile_network_policy=profile_network_policy,
        )
    return LiveSessionAdapter(
        session_factory=_live_session_factory(
            log_path=args.log_path,
            app_dir=args.app_dir,
            bulk_file=args.bulk_file,
            poll_interval=args.poll_interval,
            contextual_adjustments_enabled=contextual_adjustments_enabled,
            profile_manifest_url=resolved_profile_manifest_url,
            profile_network_policy=profile_network_policy,
            profile_client=profile_client,
        ),
        profile_client=profile_client,
        poll_interval_ms=max(1, round(args.poll_interval * 1000)),
        startup_scan=args.startup_scan,
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
        status = provider.state.get("status", {})
        if status.get("phase") == "draft_complete":
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

    QQuickStyle.setStyle("Fusion")
    application = QGuiApplication([sys.argv[0]])
    _configure_application_metadata(application=application)

    preferences = GuiPreferencesAdapter(app_dir=args.app_dir, parent=application)
    provider = _build_provider(
        args=args,
        contextual_adjustments_enabled=preferences.contextualAdjustmentsEnabled,
    )
    preferences.contextualAdjustmentsEnabledChanged.connect(
        provider.setContextualScoringEnabled
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

    if args.smoke_test_until_complete:
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

