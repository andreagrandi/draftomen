from __future__ import annotations

import gzip
import hashlib
import io
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from draftomen import __version__
from draftomen.audit import load_draft_audit_records
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.profile_client import ProfileClient, ProfileNetworkPolicy
from draftomen.profile_manifest import ProfileManifest, ProfileManifestArtifact
from draftomen.pool import load_draft_state
from draftomen.qt_gui import (
    APPLICATION_NAME,
    DEFAULT_PROFILE_MANIFEST_URL,
    _build_provider,
    _configure_application_metadata,
    _live_session_factory,
    _parser,
    _preflight_bundled_profile,
    run_gui,
)
from draftomen.qt_adapter import GuiPreferencesAdapter, LiveSessionAdapter
from draftomen.qt_mock import MockSessionAdapter
from draftomen.set_profile import SET_PROFILE_SCHEMA_VERSION, load_set_profile


FIXTURE_ACCOUNT_ID = "FIXTURECLIENTID1234567890"
FIXTURE_DRAFT_ID = "00000000-0000-4000-8000-000000000004"
PROJECT_ROOT = Path(__file__).parent.parent
FIXTURE_LOG_PATH = PROJECT_ROOT / "tests" / "fixtures" / "quick-draft-msh-player.log"
BULK_FILE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "scryfall-default-cards-sample.jsonl"


def _run_qml_probe(
    probe: str,
    *,
    timeout: int = 15,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_environment = os.environ | {"QT_QPA_PLATFORM": "offscreen"}
    if environment is not None:
        process_environment.update(environment)
    return subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        env=process_environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_gui_metadata_uses_canonical_version_and_logo_resource() -> None:
    class RecordingApplication:
        def __init__(self) -> None:
            self.metadata: dict[str, str] = {}

        def setApplicationName(self, value: str) -> None:
            self.metadata["name"] = value

        def setApplicationDisplayName(self, value: str) -> None:
            self.metadata["display_name"] = value

        def setApplicationVersion(self, value: str) -> None:
            self.metadata["version"] = value

        def setOrganizationName(self, value: str) -> None:
            self.metadata["organization"] = value

    application = RecordingApplication()
    _configure_application_metadata(application=application)  # type: ignore[arg-type]

    assert application.metadata == {
        "name": APPLICATION_NAME,
        "display_name": APPLICATION_NAME,
        "version": __version__,
        "organization": APPLICATION_NAME,
    }
    source_logo = PROJECT_ROOT / "docs" / "assets" / "draftomen_logo.png"
    runtime_logo = files("draftomen").joinpath("assets/draftomen_logo.png")
    assert runtime_logo.is_file()
    assert runtime_logo.read_bytes() == source_logo.read_bytes()


def test_live_gui_uses_set_scoped_card_data_client_without_startup_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = CardDatabase(
        cards={
            1: CardInfo(
                grp_id=1,
                name="Test Card",
                colors=(),
                mana_value=1,
                rarity="common",
                types=("Creature",),
                set_code="TST",
            )
        }
    )
    client_instances: list[object] = []

    class RecordingCardDataClient:
        def __init__(self, *, app_dir: Path | None) -> None:
            self.app_dir = app_dir
            self.calls: list[tuple[str, bool]] = []
            client_instances.append(self)

        def load(self, set_code: str, *, allow_network: bool) -> CardDatabase:
            self.calls.append((set_code, allow_network))
            return database

    monkeypatch.setattr("draftomen.qt_gui.CardDataClient", RecordingCardDataClient)
    app_dir = tmp_path / "app"
    preferences = GuiPreferencesAdapter(app_dir=app_dir)
    try:
        contextual_adjustments_enabled = preferences.contextualAdjustmentsEnabled
    finally:
        preferences.shutdown()
    assert contextual_adjustments_enabled is False
    session_factory = _live_session_factory(
        log_path=tmp_path / "Player.log",
        app_dir=app_dir,
        bulk_file=None,
        poll_interval=0.01,
        contextual_adjustments_enabled=contextual_adjustments_enabled,
    )
    session = session_factory(lambda snapshot: None)
    assert session.snapshot.contextual_adjustments_enabled is False

    client = client_instances[0]
    assert client.app_dir == app_dir  # type: ignore[attr-defined]
    assert client.calls == []  # type: ignore[attr-defined]
    assert session.card_database is None
    session._load_card_data_for_set(  # type: ignore[attr-defined]
        set_code="TST",
        allow_network=True,
    )
    assert client.calls == [("TST", True)]  # type: ignore[attr-defined]
    assert session.card_database is database


def test_live_gui_bulk_file_injects_local_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = CardDatabase(cards={})
    bulk_file = tmp_path / "cards.jsonl"
    monkeypatch.setattr(
        "draftomen.qt_gui.build_card_database_from_bulk_file",
        lambda *, path: database,
    )
    session_factory = _live_session_factory(
        log_path=tmp_path / "Player.log",
        app_dir=tmp_path / "app",
        bulk_file=bulk_file,
        poll_interval=0.01,
        contextual_adjustments_enabled=True,
    )
    session = session_factory(lambda snapshot: None)

    assert session.card_database is database

def test_mock_gui_provider_does_not_construct_live_profile_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedProfileClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            raise AssertionError("mock provider must not construct ProfileClient")

    monkeypatch.setattr("draftomen.qt_gui.ProfileClient", UnexpectedProfileClient)
    args = _parser().parse_args(
        [
            "--provider",
            "mock",
            "--scenario",
            "warning",
            "--poll-interval",
            "0",
        ]
    )

    provider = _build_provider(
        args=args,
        contextual_adjustments_enabled=True,
    )

    assert isinstance(provider, MockSessionAdapter)
    assert provider.mockMode is True
    assert provider.scenario == "warning"


def test_live_gui_rejects_nonpositive_poll_interval_before_live_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedProfileClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            raise AssertionError("invalid polling must fail before ProfileClient setup")

    monkeypatch.setattr("draftomen.qt_gui.ProfileClient", UnexpectedProfileClient)
    args = _parser().parse_args(["--poll-interval", "0"])

    with pytest.raises(
        ValueError,
        match=r"^--poll-interval must be greater than zero\.$",
    ):
        _build_provider(args=args, contextual_adjustments_enabled=True)




def test_live_gui_profile_flags_use_cached_provider_state_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_url = "https://profiles.example.test/v1/manifest.json"
    app_dir = tmp_path / "app"
    attempted_requests: list[str] = []

    def guarded_opener(
        _client: ProfileClient,
        request: Any,
        *,
        timeout: float,
    ) -> Any:
        del timeout
        attempted_requests.append(request.full_url)
        raise AssertionError("offline profile mode must not open a request")

    monkeypatch.setattr(ProfileClient, "_default_opener", guarded_opener)
    profile = load_set_profile(
        PROJECT_ROOT / "tests" / "fixtures" / "set-profiles" / "early.json",
        expected_set_code="TST",
        expected_format="QuickDraft",
    )
    cache_client = ProfileClient(
        app_dir=app_dir,
        manifest_url=manifest_url,
        network_policy=ProfileNetworkPolicy.OFFLINE,
    )
    cache_path = cache_client.profile_path("TST", "QuickDraft")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(profile.to_bytes())

    args = _parser().parse_args(
        [
            "--provider",
            "live",
            "--app-dir",
            str(app_dir),
            "--log-path",
            str(tmp_path / "Player.log"),
            "--profile-manifest-url",
            manifest_url,
            "--offline-profiles",
            "--poll-interval",
            "0.01",
            "--no-startup-scan",
        ]
    )
    provider = _build_provider(
        args=args,
        contextual_adjustments_enabled=True,
    )
    assert isinstance(provider, LiveSessionAdapter)

    client = provider._profile_client  # type: ignore[attr-defined]
    assert isinstance(client, ProfileClient)
    assert client.manifest_url == manifest_url
    assert client.network_policy is ProfileNetworkPolicy.OFFLINE
    session = provider._session_factory(lambda snapshot: None)  # type: ignore[attr-defined]
    assert session._profile_client is client  # type: ignore[attr-defined]
    session._set_active_set_code(set_code="TST")  # type: ignore[attr-defined]

    assert session.snapshot.set_profile.set_code == "TST"
    assert session.snapshot.set_profile.maturity == "early"
    assert session.snapshot.set_profile.source == "local-early"
    assert session.snapshot.set_profile.phase.value == "ready"
    assert session.profile_refresh_request() is None
    assert client.refresh("TST", "QuickDraft").outcome.value == "cached"
    assert attempted_requests == []

    fallback_app_dir = tmp_path / "fallback-app"
    fallback_args = _parser().parse_args(
        [
            "--provider",
            "live",
            "--app-dir",
            str(fallback_app_dir),
            "--log-path",
            str(tmp_path / "fallback-Player.log"),
            "--profile-manifest-url",
            manifest_url,
            "--offline-profiles",
            "--poll-interval",
            "0.01",
            "--no-startup-scan",
        ]
    )
    fallback_provider = _build_provider(
        args=fallback_args,
        contextual_adjustments_enabled=True,
    )
    fallback_session = fallback_provider._session_factory(  # type: ignore[attr-defined]
        lambda snapshot: None
    )
    fallback_session._set_active_set_code(set_code="TST")  # type: ignore[attr-defined]
    assert fallback_session.snapshot.set_profile.set_code == "TST"
    assert fallback_session.snapshot.set_profile.maturity == "generic"
    assert fallback_session.snapshot.set_profile.phase.value == "ready"
    assert fallback_session.profile_refresh_request() is None
    assert attempted_requests == []


def test_verify_bundled_profile_flag_is_hidden_and_parsed() -> None:
    parser = _parser()

    args = parser.parse_args(["--verify-bundled-profile"])
    assert args.verify_bundled_profile is True
    assert "--verify-bundled-profile" not in parser.format_help()


def test_bundled_profile_preflight_is_offline_and_cacheless(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"

    assert _preflight_bundled_profile(app_dir=app_dir) is True
    assert not (app_dir / "set-profiles" / "hob-quickdraft.json").exists()


def test_verify_bundled_profile_failure_exits_before_gui_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "draftomen.qt_gui._preflight_bundled_profile",
        lambda *, app_dir: False,
    )

    def unexpected_gui_setup(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("GUI setup should not run after preflight failure")

    class UnexpectedStyle:
        @staticmethod
        def setStyle(style: str) -> None:
            del style
            raise AssertionError("GUI setup should not run after preflight failure")

    monkeypatch.setattr("draftomen.qt_gui.QQuickStyle", UnexpectedStyle)
    monkeypatch.setattr("draftomen.qt_gui.QGuiApplication", unexpected_gui_setup)

    assert run_gui(argv=["--verify-bundled-profile"], forced_provider="mock") == 1

def test_qml_settings_renders_card_and_ratings_update_fallback_and_value() -> None:
    probe = """
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.qt_mock import MockSessionAdapter


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
provider = MockSessionAdapter(session=MockLiveSession(scenario="loading"))
with TemporaryDirectory() as preferences_dir:
    preferences = GuiPreferencesAdapter(app_dir=preferences_dir)
    engine = QQmlApplicationEngine()
    qml_directory = Path.cwd() / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", _fixed_font_family())
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", __version__)
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("initialSurface", "settings")
    context.setContextProperty("initialWindowWidth", 900)
    context.setContextProperty("initialWindowHeight", 760)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    application.processEvents()

    card_message = root.findChild(QObject, "settingsCardDataMessage")
    card_update_label = root.findChild(QObject, "settingsCardDataLastUpdated")
    ratings_update_label = root.findChild(QObject, "settingsRatingsLastUpdated")
    assert card_message is not None
    assert card_update_label is not None
    assert ratings_update_label is not None
    assert card_message.property("text") == "Loading card metadata."
    assert card_update_label.property("text") == "Card metadata updated · Never updated"
    assert ratings_update_label.property("text") == "17Lands ratings updated · Never updated"
    assert not any(
        str(item.property("text")) == "Statistics attribution · 17Lands"
        for item in root.findChildren(QObject)
    )

    provider.selectScenario("ready")
    application.processEvents()
    assert card_message.property("text") == "Card metadata ready."
    assert str(card_update_label.property("text")).startswith("Card metadata updated · ")
    assert "2026" in card_update_label.property("text")
    assert "Never updated" not in card_update_label.property("text")
    assert str(ratings_update_label.property("text")).startswith("17Lands ratings updated · ")
    assert "2026" in ratings_update_label.property("text")
    assert "Never updated" not in ratings_update_label.property("text")

    preferences.shutdown()
    del engine
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr


def test_qml_renders_profile_maturity_and_refresh_outcome_on_compact_surfaces() -> None:
    probe = """
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter, SessionAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.session import DataLoadPhase, SetProfileState


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
base_snapshot = MockLiveSession().snapshot
provider = SessionAdapter(snapshot=base_snapshot)
with TemporaryDirectory() as preferences_dir:
    preferences = GuiPreferencesAdapter(app_dir=preferences_dir)
    engine = QQmlApplicationEngine()
    qml_directory = Path.cwd() / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", _fixed_font_family())
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", __version__)
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("initialSurface", "settings")
    context.setContextProperty("initialWindowWidth", 900)
    context.setContextProperty("initialWindowHeight", 760)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    profile_label = root.findChild(QObject, "statusProfileMessage")
    cache_label = root.findChild(QObject, "settingsProfileCacheStatus")
    assert profile_label is not None
    assert cache_label is not None

    for maturity, outcome, expected_status, expected_cache in (
        ("mature", "unchanged", "Profile · mature · unchanged", "OTJ · mature · unchanged"),
        ("semantic", "updated", "Profile · semantic · updated", "OTJ · semantic · updated"),
        ("generic", None, "Profile · generic", "OTJ · generic"),
    ):
        provider._apply_snapshot(
            replace(
                base_snapshot,
                set_profile=SetProfileState(
                    set_code="OTJ",
                    event_format="QuickDraft",
                    maturity=maturity,
                    profile_version="fixture",
                    source="fixture",
                    phase=DataLoadPhase.READY,
                    refresh_outcome=outcome,
                    message="Profile fixture status.",
                ),
            )
        )
        application.processEvents()
        assert profile_label.property("text") == expected_status
        assert cache_label.property("text") == expected_cache

    preferences.shutdown()
    del engine
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr


def test_qml_renders_full_pre_draft_setup_guidance_offscreen() -> None:
    probe = """
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import LOG_SETUP_GUIDANCE


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
preferences_dir = TemporaryDirectory()
session = MockLiveSession(scenario="empty")
initial_snapshot = session.snapshot
session_state = replace(
    initial_snapshot,
    status=replace(
        initial_snapshot.status,
        message=LOG_SETUP_GUIDANCE,
        setup_guidance=True,
    ),
)
provider = MockSessionAdapter(session=session)
provider._publish(snapshot=session_state)
preferences = GuiPreferencesAdapter(app_dir=preferences_dir.name)
engine = QQmlApplicationEngine()
qml_directory = Path.cwd() / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "live")
context.setContextProperty("initialWindowWidth", 900)
context.setContextProperty("initialWindowHeight", 700)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
assert engine.rootObjects()
root = engine.rootObjects()[0]
application.processEvents()

heading = root.findChild(QObject, "preDraftHeading")
assert heading is not None
assert heading.isVisible()
assert heading.property("text") == "Arena setup needed"
guidance = root.findChild(QObject, "preDraftGuidance")
assert guidance is not None
assert guidance.isVisible()
assert guidance.property("text") == LOG_SETUP_GUIDANCE
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr


def test_production_gui_processes_representative_arena_log_offscreen(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    screenshot = tmp_path / "gui.png"
    environment = os.environ | {"QT_QPA_PLATFORM": "offscreen"}

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "draftomen.qt_gui",
            "--log-path",
            str(FIXTURE_LOG_PATH),
            "--app-dir",
            str(app_dir),
            "--bulk-file",
            str(BULK_FILE_PATH),
            "--poll-interval",
            "0.01",
            "--smoke-test-until-complete",
            "--screenshot",
            str(screenshot),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Monospace" not in completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr
    assert screenshot.is_file()
    assert load_draft_state(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=app_dir,
    ).completed is True
    records = load_draft_audit_records(
        account_id=FIXTURE_ACCOUNT_ID,
        draft_id=FIXTURE_DRAFT_ID,
        app_dir=app_dir,
    )
    assert records[-1]["record_type"] == "draft_completed"


def test_production_startup_scoring_uses_reloaded_contextual_preference_offscreen(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    probe = """
import json
import os
import time
from pathlib import Path

from PySide6.QtGui import QGuiApplication

import draftomen.qt_gui as qt_gui
from draftomen.qt_adapter import GuiPreferencesAdapter, LiveSessionAdapter
from draftomen.session import LiveSession


def wait_until(predicate, description):
    deadline = time.monotonic() + 8
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for " + description)
        time.sleep(0.005)
    application.processEvents()


class RecordingLiveSession(LiveSession):
    def __init__(self, **kwargs):
        self.scored_publications = []
        super().__init__(**kwargs)
        recording_sessions.append(self)

    def _publish(self, snapshot):
        super()._publish(snapshot=snapshot)
        scored_pack = snapshot.current_scored_pack
        if scored_pack is not None and scored_pack.cards and not self.scored_publications:
            self.scored_publications.append(
                {
                    "contextual_adjustments_enabled": (
                        snapshot.contextual_adjustments_enabled
                    ),
                    "scored_pack": scored_pack,
                }
            )


class NoNetworkCardImageService:
    def __init__(self, **kwargs):
        del kwargs

    def resolve_image_uri(self, **kwargs):
        del kwargs
        return None

    def resolve_focused_image_uri(self, **kwargs):
        del kwargs
        return None


recording_sessions = []
qt_gui.LiveSession = RecordingLiveSession
qt_gui.CardImageService = NoNetworkCardImageService

project_root = Path.cwd()
fixture_log_path = project_root / "tests" / "fixtures" / "quick-draft-msh-player.log"
fixture_log_lines = fixture_log_path.read_text(encoding="utf-8").splitlines(keepends=True)
bulk_file = project_root / "tests" / "fixtures" / "scryfall-default-cards-sample.jsonl"


def seed_preferences(*, case_dir, contextual_enabled, legacy):
    seed = GuiPreferencesAdapter(app_dir=case_dir)
    if contextual_enabled:
        seed.setContextualAdjustmentsEnabled(True)
    else:
        seed.setCardPreview(False)
    wait_until(
        lambda: seed.persistenceMessage == "Saved",
        "the seeded preference save",
    )
    seed.shutdown()
    if legacy:
        preferences_path = case_dir / "gui-preferences.json"
        preferences_data = json.loads(preferences_path.read_text(encoding="utf-8"))
        del preferences_data["display"]["contextual_adjustments_enabled"]
        preferences_path.write_text(
            json.dumps(preferences_data, indent=2, sort_keys=True) + "\\n",
            encoding="utf-8",
        )


def startup_case(*, name, contextual_enabled, legacy):
    case_dir = Path(os.environ["DRAFTOMEN_E2E_APP_DIR"]) / name
    case_dir.mkdir(parents=True, exist_ok=True)
    log_path = case_dir / "Player.log"
    log_path.write_text("".join(fixture_log_lines[:7]), encoding="utf-8")
    if legacy:
        seed_preferences(
            case_dir=case_dir,
            contextual_enabled=False,
            legacy=True,
        )
    elif contextual_enabled:
        seed_preferences(
            case_dir=case_dir,
            contextual_enabled=True,
            legacy=False,
        )
    preferences = GuiPreferencesAdapter(app_dir=case_dir)
    provider = None
    try:
        assert preferences.contextualAdjustmentsEnabled is contextual_enabled
        args = qt_gui._parser().parse_args(
            [
                "--provider",
                "live",
                "--app-dir",
                str(case_dir),
                "--log-path",
                str(log_path),
                "--bulk-file",
                str(bulk_file),
                "--poll-interval",
                "0.01",
                "--offline-profiles",
            ]
        )
        assert args.startup_scan is True
        provider = qt_gui._build_provider(
            args=args,
            contextual_adjustments_enabled=preferences.contextualAdjustmentsEnabled,
        )
        assert isinstance(provider, LiveSessionAdapter)
        assert provider._startup_scan is True
        session_index = len(recording_sessions)
        provider.start()
        wait_until(
            lambda: len(recording_sessions) > session_index,
            name + " production-created session",
        )
        recording_session = recording_sessions[session_index]
        wait_until(
            lambda: recording_session.scored_publications,
            name + " first non-empty startup score",
        )
        first_scoring = recording_session.scored_publications[0]
        assert first_scoring["contextual_adjustments_enabled"] is contextual_enabled
        assert first_scoring["scored_pack"].cards
    finally:
        if provider is not None:
            provider.shutdown()
            provider.wait_for_shutdown()
        preferences.shutdown()


application = QGuiApplication([])
startup_case(name="missing", contextual_enabled=False, legacy=False)
startup_case(name="existing-v1-without-field", contextual_enabled=False, legacy=True)
startup_case(name="persisted-true", contextual_enabled=True, legacy=False)
"""
    completed = _run_qml_probe(
        probe,
        environment={"DRAFTOMEN_E2E_APP_DIR": str(app_dir)},
    )

    assert completed.returncode == 0, completed.stderr


def test_production_adapter_renders_build_backtest_and_persisted_preferences_offscreen(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    probe = """
import json
import os
import time
import urllib.parse
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, Qt, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen import __version__
from draftomen.carddb import build_card_database_from_bulk_file
from draftomen.cardimages import CardImageService
from draftomen.pool import load_draft_state
from draftomen.qt_adapter import GuiPreferencesAdapter, LiveSessionAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.session import LiveSession, LiveSessionCommand, RequestBuild


def find_visual_item(item: QQuickItem, object_name: str) -> QQuickItem | None:
    if item.objectName() == object_name:
        return item
    for child in item.childItems():
        found = find_visual_item(child, object_name)
        if found is not None:
            return found
    return None

def selected_recommendation(state):
    recommendations = state["recommendations"]
    selected_grp_id = recommendations["selected_grp_id"]
    return next(
        recommendation
        for recommendation in recommendations["cards"]
        if recommendation["card"]["grp_id"] == selected_grp_id
    )


def image_source(image: QObject) -> str:
    source = image.property("source")
    return source.toString() if isinstance(source, QUrl) else str(source)


def wait_until(predicate, description: str) -> None:
    deadline = time.monotonic() + 8
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for " + description)
        time.sleep(0.005)
    application.processEvents()


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self, maximum: int) -> bytes:
        return self.payload[:maximum]


def metadata_opener(request, timeout):
    del timeout
    query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
    exact_name = query["exact"][0]
    image_uri = (
        "https://images.example/"
        + urllib.parse.quote(exact_name, safe="")
        + ".png"
    )
    return _Response(
        json.dumps({"image_uris": {"normal": image_uri}}).encode("utf-8")
    )


def image_opener(request, timeout):
    del request, timeout
    return _Response(
        (project_root / "draftomen" / "assets" / "draftomen_logo.png").read_bytes()
    )


def load_image_database():
    database = build_card_database_from_bulk_file(path=bulk_file)
    return replace(
        database,
        cards={
            grp_id: replace(card, image_uri=None)
            for grp_id, card in database.cards.items()
        },
        image_uris_by_name={},
    )


project_root = Path.cwd()
app_dir = Path(os.environ["DRAFTOMEN_E2E_APP_DIR"])
fixture_log_path = project_root / "tests" / "fixtures" / "quick-draft-msh-player.log"
fixture_log_lines = fixture_log_path.read_text(encoding="utf-8").splitlines(keepends=True)
log_path = app_dir / "Player.log"
log_path.parent.mkdir(parents=True, exist_ok=True)
log_path.write_text("".join(fixture_log_lines[:22]), encoding="utf-8")
bulk_file = project_root / "tests" / "fixtures" / "scryfall-default-cards-sample.jsonl"


class RecordingLiveSession(LiveSession):
    def __init__(self, **kwargs):
        self.build_requests: list[dict[str, bool | int]] = []
        self.app_dir = kwargs["app_dir"]
        super().__init__(**kwargs)


    def dispatch(self, *, command: LiveSessionCommand):
        if isinstance(command, RequestBuild):
            draft = self.snapshot.draft
            persisted = (
                None
                if draft is None
                else load_draft_state(
                    account_id=draft.account_id,
                    draft_id=draft.draft_id,
                    app_dir=self.app_dir,
                )
            )
            self.build_requests.append(
                {
                    "draft_completed": draft is not None and draft.completed,
                    "pool_total_cards": self.snapshot.pool.total_cards,
                    "persisted_completed": (
                        persisted is not None and persisted.completed
                    ),
                }
            )
        return super().dispatch(command=command)


recording_sessions: list[RecordingLiveSession] = []


def factory(publish):
    session = RecordingLiveSession(
        log_path=log_path,
        card_database=load_image_database(),
        app_dir=app_dir,
        poll_interval=0.01,
        contextual_adjustments_enabled=preferences.contextualAdjustmentsEnabled,
        snapshot_publisher=publish,
        card_image_service=CardImageService(
            cache_dir=app_dir / "card-images",
            opener=image_opener,
            metadata_opener=metadata_opener,
            monotonic_clock=lambda: 0.0,
            sleep=lambda seconds: None,
        ),
    )
    recording_sessions.append(session)
    return session

QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
preferences = GuiPreferencesAdapter(app_dir=app_dir)
preferences.setContextualAdjustmentsEnabled(True)
wait_until(
    lambda: preferences.persistenceMessage == "Saved",
    "the persisted contextual scoring preference",
)
provider = LiveSessionAdapter(
    session_factory=factory,
    poll_interval_ms=10,
    startup_scan=True,
)
preferences.contextualAdjustmentsEnabledChanged.connect(
    provider.setContextualScoringEnabled
)
engine = QQmlApplicationEngine()
qml_directory = project_root / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "live")
context.setContextProperty("initialWindowWidth", 1440)
context.setContextProperty("initialWindowHeight", 900)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
root = engine.rootObjects()[0]
try:
    provider.start()
    wait_until(
        lambda: recording_sessions,
        "the worker-created recording session",
    )
    recording_session = recording_sessions[0]
    wait_until(
        lambda: recording_session.snapshot.draft is not None
        and recording_session.snapshot.draft.pick_number == 5
        and recording_session.snapshot.current_scored_pack is not None,
        "the current startup scoring result",
    )
    first_scored_pack = recording_session.snapshot.current_scored_pack
    assert first_scored_pack is not None
    assert first_scored_pack.cards
    assert recording_session.snapshot.contextual_adjustments_enabled is True
    assert root.property("currentSurface") == "live"
    first_comparison_summary = first_scored_pack.comparison_summary
    assert isinstance(first_comparison_summary, str)
    comparison = root.findChild(QObject, "recommendationComparisonSummary")
    confidence = root.findChild(QObject, "recommendationConfidenceSummary")
    live_view = root.findChild(QObject, "liveDraftView")
    ranking = root.findChild(QObject, "rankingSelector")
    assert (
        comparison is not None
        and confidence is not None
        and live_view is not None
        and ranking is not None
    )
    wait_until(
        lambda: provider.state["recommendations"]["comparison_summary"]
        == first_comparison_summary,
        "the published production recommendation comparison",
    )
    assert comparison.isVisible()
    assert comparison.property("text") == first_comparison_summary

    def assert_comparison_layout(*, width: int, height: int) -> None:
        root.resize(width, height)
        application.processEvents()
        assert comparison.isVisible()
        assert comparison.property("text") == first_comparison_summary
        assert comparison.width() > 0
        assert comparison.height() > 0
        assert float(comparison.property("implicitHeight")) <= (
            comparison.height() + 1
        )
        assert float(comparison.property("paintedHeight")) <= (
            comparison.height() + 1
        )
        comparison_top_left = comparison.mapToScene(QPointF(0, 0))
        comparison_bottom_right = comparison.mapToScene(
            QPointF(comparison.width(), comparison.height())
        )
        live_top_left = live_view.mapToScene(QPointF(0, 0))
        live_bottom_right = live_view.mapToScene(
            QPointF(live_view.width(), live_view.height())
        )
        ranking_top_left = ranking.mapToScene(QPointF(0, 0))
        assert comparison_top_left.x() >= live_top_left.x() - 1
        assert comparison_top_left.y() >= live_top_left.y() - 1
        assert comparison_bottom_right.x() <= live_bottom_right.x() + 1
        assert comparison_bottom_right.y() <= live_bottom_right.y() + 1
        assert comparison_bottom_right.x() <= ranking_top_left.x() + 1
        if confidence.isVisible():
            confidence_bottom_right = confidence.mapToScene(
                QPointF(confidence.width(), confidence.height())
            )
            assert comparison_top_left.y() >= confidence_bottom_right.y() - 1

    for ranking_mode in ("win_rate", "alsa", "mv"):
        provider.changeRanking(ranking_mode)
        wait_until(
            lambda: provider.state["recommendations"]["ranking_mode"]
            == ranking_mode,
            ranking_mode + " production recommendation sort",
        )
        assert provider.state["recommendations"]["comparison_summary"] == (
            first_comparison_summary
        )
        assert comparison.property("text") == first_comparison_summary

    for recommendation in provider.state["recommendations"]["cards"][:2]:
        grp_id = recommendation["card"]["grp_id"]
        provider.chooseRecommendation(grp_id)
        wait_until(
            lambda: provider.state["recommendations"]["selected_grp_id"] == grp_id,
            "the focused production recommendation",
        )
        assert comparison.property("text") == first_comparison_summary
        assert provider.state["recommendations"]["comparison_summary"] == (
            first_comparison_summary
        )
    provider.changeRanking("score")
    wait_until(
        lambda: provider.state["recommendations"]["ranking_mode"] == "score",
        "the restored production recommendation sort",
    )
    assert provider.state["recommendations"]["comparison_summary"] == (
        first_comparison_summary
    )


    assert_comparison_layout(width=1440, height=900)
    assert_comparison_layout(width=760, height=900)
    assert_comparison_layout(width=680, height=640)


    root.resize(760, 900)
    application.processEvents()
    live_preview = root.findChild(QObject, "narrowLiveCardPreview")
    assert live_preview is not None
    live_image = live_preview.findChild(QObject, "cardPreviewImage")
    assert live_image is not None
    live_explanation = live_preview.findChild(QObject, "cardPreviewExplanation")
    assert live_explanation is not None
    wait_until(
        lambda: any(
            recommendation["card"]["grp_id"]
            == provider.state["recommendations"]["selected_grp_id"]
            for recommendation in provider.state["recommendations"]["cards"]
        ),
        "the selected production recommendation",
    )
    first_recommendation = selected_recommendation(provider.state)
    assert first_recommendation["explanation"]
    wait_until(
        lambda: live_explanation.property("text")
            == first_recommendation["explanation"],
        "the visible initial recommendation rationale",
    )
    wait_until(
        lambda: provider.state["card_image"]["phase"] == "ready"
        and live_preview.property("imageCurrent") is True
        and live_image.isVisible(),
        "the visible selected recommendation image",
    )
    first_recommendation_source = image_source(live_image)
    first_recommendation_grp_id = provider.state["card_image"]["grp_id"]
    wait_until(
        lambda: any(
            card["card"]["grp_id"] != first_recommendation_grp_id
            and card["card"]["image_path"] is not None
            for card in provider.state["recommendations"]["cards"]
        ),
        "an automatically fetched recommendation thumbnail",
    )
    next_recommendation_grp_id = next(
        card["card"]["grp_id"]
        for card in provider.state["recommendations"]["cards"]
        if card["card"]["grp_id"] != first_recommendation_grp_id
    )
    next_recommendation = next(
        card
        for card in provider.state["recommendations"]["cards"]
        if card["card"]["grp_id"] == next_recommendation_grp_id
    )
    assert next_recommendation["explanation"]
    provider.chooseRecommendation(next_recommendation_grp_id)
    wait_until(
        lambda: provider.state["card_image"]["grp_id"] == next_recommendation_grp_id
        and provider.state["card_image"]["phase"] == "ready"
        and provider.state["recommendations"]["selected_grp_id"]
            == next_recommendation_grp_id
        and live_preview.property("imageCurrent") is True
        and live_image.isVisible()
        and live_explanation.property("text")
            == next_recommendation["explanation"]
        and image_source(live_image) != first_recommendation_source,
        "the visible changed recommendation image and rationale",
    )
    next_recommendation_source = image_source(live_image)
    assert next_recommendation_source
    assert recording_session.build_requests == []
    provider.requestBuild("")
    wait_until(
        lambda: provider.state.get("build") is not None,
        "the published active-draft build",
    )
    assert len(recording_session.build_requests) == 1


    root.setProperty("currentSurface", "build")
    assert provider.state["build"]["spells"]
    build_spell = find_visual_item(root.contentItem(), "buildSpellButton1")
    assert build_spell is not None and build_spell.isVisible()
    build_spell.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    build_preview = root.findChild(QObject, "narrowBuildCardPreview")
    assert build_preview is not None
    build_image = build_preview.findChild(QObject, "cardPreviewImage")
    build_explanation = build_preview.findChild(QObject, "cardPreviewExplanation")
    assert build_explanation is not None
    assert build_explanation.property("text") == ""
    assert build_image is not None
    first_build_grp_id = provider.state["build"]["spells"][1]["card"]["grp_id"]
    wait_until(
        lambda: provider.state["card_image"]["grp_id"] == first_build_grp_id
        and provider.state["card_image"]["phase"] == "ready"
        and build_preview.property("imageCurrent") is True
        and build_image.isVisible(),
        "the visible focused build image",
    )
    first_build_source = image_source(build_image)
    next_build_spell = find_visual_item(root.contentItem(), "buildSpellButton2")
    assert next_build_spell is not None and next_build_spell.isVisible()
    next_build_spell.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    next_build_grp_id = provider.state["build"]["spells"][2]["card"]["grp_id"]
    wait_until(
        lambda: provider.state["card_image"]["grp_id"] == next_build_grp_id
        and provider.state["card_image"]["phase"] == "ready"
        and build_preview.property("imageCurrent") is True
        and build_image.isVisible()
        and image_source(build_image) != first_build_source,
        "the visible changed build image",
    )
    next_build_source = image_source(build_image)
    assert build_explanation.property("text") == ""
    assert next_build_source

    root.setProperty("currentSurface", "live")
    wait_until(
        lambda: provider.state["card_image"]["grp_id"] == next_recommendation_grp_id
        and provider.state["card_image"]["phase"] == "ready"
        and live_preview.property("imageCurrent") is True
        and live_image.isVisible()
        and image_source(live_image) == next_recommendation_source,
        "the restored visible recommendation image",
    )

    root.setProperty("currentSurface", "build")
    wait_until(
        lambda: provider.state["card_image"]["grp_id"] == next_build_grp_id
        and provider.state["card_image"]["phase"] == "ready"
        and build_preview.property("imageCurrent") is True
        and build_image.isVisible()
        and image_source(build_image) == next_build_source,
        "the restored visible build image",
    )
    root.setProperty("currentSurface", "live")
    application.processEvents()
    assert root.property("currentSurface") == "live"
    with log_path.open(mode="a", encoding="utf-8") as log_file:
        log_file.writelines(fixture_log_lines[22:49])
    wait_until(
        lambda: (provider.state.get("draft") or {}).get("pack_number") == 1,
        "the published subsequent production pack",
    )
    wait_until(
        lambda: (provider.state.get("draft") or {}).get("pack_number") == 1
        and recording_session.snapshot.current_scored_pack is not None,
        "the scored subsequent production pack",
    )
    second_snapshot = recording_session.snapshot
    second_scored_pack = second_snapshot.current_scored_pack
    assert second_scored_pack is not None
    second_comparison_summary = second_scored_pack.comparison_summary
    assert isinstance(second_comparison_summary, str)
    assert second_comparison_summary != first_comparison_summary
    wait_until(
        lambda: provider.state["recommendations"]["comparison_summary"]
        == second_comparison_summary,
        "the subsequent production recommendation comparison",
    )
    assert comparison.isVisible()
    assert comparison.property("text") == second_comparison_summary

    single_card_recommendations = replace(
        second_snapshot.recommendations,
        cards=second_snapshot.recommendations.cards[:1],
        comparison_summary=None,
    )
    recording_session._publish(
        replace(
            second_snapshot,
            recommendations=single_card_recommendations,
        )
    )
    wait_until(
        lambda: provider.state["recommendations"]["comparison_summary"] is None,
        "the cleared single-card production comparison",
    )
    assert comparison.isVisible() is False
    recording_session._publish(second_snapshot)
    wait_until(
        lambda: provider.state["recommendations"]["comparison_summary"]
        == second_comparison_summary,
        "the restored subsequent production comparison",
    )
    assert comparison.isVisible()
    assert comparison.property("text") == second_comparison_summary

    with log_path.open(mode="a", encoding="utf-8") as log_file:
        log_file.writelines(fixture_log_lines[49:])

    build_view = root.findChild(QObject, "buildView")
    assert build_view is not None
    wait_until(
        lambda: (provider.state.get("draft") or {}).get("completed") is True
        and root.property("currentSurface") == "build"
        and provider.state.get("build") is not None
        and len(recording_session.build_requests) == 2,
        "the completed representative draft build",
    )
    assert len(recording_session.build_requests) == 2
    manual_build, completed_build = recording_session.build_requests
    assert manual_build["draft_completed"] is False
    assert manual_build["persisted_completed"] is False
    assert completed_build == {
        "draft_completed": True,
        "pool_total_cards": provider.state["pool"]["total_cards"],
        "persisted_completed": True,
    }
    assert provider.state["pool"]["total_cards"] == 42
    assert build_view.property("hasBuild") is True
    assert build_explanation.property("text") == ""


    root.setProperty("currentSurface", "backtest")
    application.processEvents()
    run_backtest = root.findChild(QObject, "backtestRunButton")
    assert run_backtest is not None and run_backtest.property("visible") is True
    run_backtest.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    wait_until(
        lambda: provider.state.get("backtest") is not None,
        "the published production backtest",
    )
    assert provider.state["backtest"]["rows"]
    backtest_rows = root.findChild(QObject, "backtestRows")
    assert backtest_rows is not None and backtest_rows.property("count") > 0

    root.setProperty("currentSurface", "settings")
    application.processEvents()
    card_preview = root.findChild(QObject, "settingsCardPreviewSwitch")
    assert card_preview is not None and card_preview.property("checked") is True
    card_preview.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert preferences.cardPreview is False
    wait_until(
        lambda: preferences.persistenceMessage == "Saved",
        "the latest display preference save",
    )
    persisted_display_preferences = GuiPreferencesAdapter(app_dir=app_dir)
    try:
        assert persisted_display_preferences.cardPreview is False
    finally:
        persisted_display_preferences.shutdown()
finally:
    preferences.shutdown()
    provider.shutdown()
    provider.wait_for_shutdown()
    del engine
"""
    completed = _run_qml_probe(
        probe,
        timeout=20,
        environment={"DRAFTOMEN_E2E_APP_DIR": str(app_dir)},
    )

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr


def test_qml_card_preview_explanation_modes_and_refresh_offscreen(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    probe = """
import json
import os
import time
import urllib.parse
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QUrl, Qt
from PySide6.QtGui import QFontMetricsF, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen.carddb import build_card_database_from_bulk_file
from draftomen.cardimages import CardImageService
from draftomen.qt_adapter import GuiPreferencesAdapter, LiveSessionAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.session import LiveSession


def wait_until(predicate, description):
    deadline = time.monotonic() + 8
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for " + description)
        time.sleep(0.005)
    application.processEvents()


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self, maximum):
        return self.payload[:maximum]


def metadata_opener(request, timeout):
    del timeout
    query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
    exact_name = query["exact"][0]
    image_uri = (
        "https://images.example/"
        + urllib.parse.quote(exact_name, safe="")
        + ".png"
    )
    return _Response(
        json.dumps({"image_uris": {"normal": image_uri}}).encode("utf-8")
    )


def image_opener(request, timeout):
    del request, timeout
    return _Response(
        (project_root / "draftomen" / "assets" / "draftomen_logo.png").read_bytes()
    )


def load_image_database():
    database = build_card_database_from_bulk_file(path=bulk_file)
    return replace(
        database,
        cards={
            grp_id: replace(card, image_uri=None)
            for grp_id, card in database.cards.items()
        },
        image_uris_by_name={},
    )


def find_visual_item(item, object_name):
    if item.objectName() == object_name:
        return item
    for child in item.childItems():
        found = find_visual_item(child, object_name)
        if found is not None:
            return found
    return None


def selected_recommendation(state):
    recommendations = state["recommendations"]
    selected_grp_id = recommendations["selected_grp_id"]
    return next(
        recommendation
        for recommendation in recommendations["cards"]
        if recommendation["card"]["grp_id"] == selected_grp_id
    )


def rect_in(item, ancestor):
    top_left = item.mapToItem(ancestor, QPointF(0, 0))
    bottom_right = item.mapToItem(
        ancestor,
        QPointF(item.width(), item.height()),
    )
    return top_left, bottom_right


def assert_contained(preview, details, explanation):
    details_top_left, details_bottom_right = rect_in(details, preview)
    label_top_left, label_bottom_right = rect_in(explanation, preview)
    bounds = (
        preview.width(),
        preview.height(),
        details_top_left,
        details_bottom_right,
        label_top_left,
        label_bottom_right,
    )
    assert details_top_left.x() >= -1, bounds
    assert details_top_left.y() >= -1, bounds
    assert details_bottom_right.x() <= preview.width() + 1, bounds
    assert details_bottom_right.y() <= preview.height() + 1, bounds
    assert label_top_left.x() >= -1, bounds
    assert label_bottom_right.x() <= preview.width() + 1, bounds
    assert (
        float(explanation.property("paintedWidth")) <= explanation.width() + 1
    ), bounds


project_root = Path.cwd()
app_dir = Path(os.environ["DRAFTOMEN_E2E_APP_DIR"])
fixture_log_path = project_root / "tests" / "fixtures" / "quick-draft-msh-player.log"
fixture_log_lines = fixture_log_path.read_text(encoding="utf-8").splitlines(
    keepends=True
)
log_path = app_dir / "Player.log"
log_path.parent.mkdir(parents=True, exist_ok=True)
log_path.write_text("".join(fixture_log_lines[:7]), encoding="utf-8")
bulk_file = project_root / "tests" / "fixtures" / "scryfall-default-cards-sample.jsonl"


class RecordingLiveSession(LiveSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        recording_sessions.append(self)


recording_sessions = []


def factory(publish):
    return RecordingLiveSession(
        log_path=log_path,
        card_database=load_image_database(),
        app_dir=app_dir,
        poll_interval=0.01,
        contextual_adjustments_enabled=True,
        snapshot_publisher=publish,
        card_image_service=CardImageService(
            cache_dir=app_dir / "card-images",
            opener=image_opener,
            metadata_opener=metadata_opener,
            monotonic_clock=lambda: 0.0,
            sleep=lambda seconds: None,
        ),
    )


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
preferences = GuiPreferencesAdapter(app_dir=app_dir)
provider = LiveSessionAdapter(
    session_factory=factory,
    poll_interval_ms=10,
    startup_scan=True,
)

qml = b'''
import QtQuick 2.15
import QtQuick.Controls 2.15

ApplicationWindow {
    id: host
    property bool useOverride: false
    property var overrideRecommendation: null
    readonly property var selectedRecommendation: {
        const recommendationState = sessionProvider.state.recommendations
        if (!recommendationState || !recommendationState.cards)
            return null
        for (let index = 0; index < recommendationState.cards.length; index++) {
            const recommendation = recommendationState.cards[index]
            if (recommendation.card.grp_id
                    === recommendationState.selected_grp_id)
                return recommendation
        }
        return recommendationState.cards.length > 0
            ? recommendationState.cards[0] : null
    }
    width: 550
    height: 600
    visible: true

    CardPreview {
        id: preview
        objectName: "cardPreviewProbe"
        anchors.fill: parent
        recommendation: host.useOverride
            ? host.overrideRecommendation : host.selectedRecommendation
        imageState: sessionProvider.state.card_image
    }
}
'''

engine = QQmlApplicationEngine()
qml_directory = project_root / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("guiPreferences", preferences)
component = QQmlComponent(engine)
component.setData(
    qml,
    QUrl.fromLocalFile(str(qml_directory / "CardPreviewProbe.qml")),
)
assert component.status() == QQmlComponent.Status.Ready, [
    error.toString() for error in component.errors()
]
host = component.create()
assert host is not None
host.show()
preview = host.findChild(QObject, "cardPreviewProbe")
assert preview is not None
details = preview.findChild(QObject, "cardPreviewDetails")
explanation = preview.findChild(QObject, "cardPreviewExplanation")
assert details is not None
assert explanation is not None


def check_mode(*, detailed, width, height, expected, stress):
    host.resize(width, height)
    preview.forceActiveFocus()
    preview.setProperty("detailedIntel", detailed)
    wait_until(
        lambda: explanation.property("text") == expected
        and (
            not stress
            or details.property("contentHeight") > details.height() + 1
        ),
        "the preview mode layout",
    )
    assert explanation.property("text") == expected, (detailed, width, height)
    assert_contained(preview, details, explanation)
    if not detailed:
        assert details.property("activeFocusOnTab") is False
    if not stress:
        return
    assert details.property("contentHeight") > details.height() + 1, (
        detailed,
        width,
        height,
        details.property("contentHeight"),
        details.height(),
    )
    details.forceActiveFocus()
    QTest.keyClick(host, Qt.Key_End)
    application.processEvents()
    maximum = max(0, details.property("contentHeight") - details.height())
    assert abs(details.property("contentY") - maximum) <= 1, (
        detailed,
        width,
        height,
        details.property("contentY"),
        maximum,
    )
    label_bottom = explanation.mapToItem(
        details,
        QPointF(0, explanation.height()),
    ).y()
    assert -1 <= label_bottom <= details.height() + 1, (
        detailed,
        width,
        height,
        label_bottom,
        details.height(),
    )
    QTest.keyClick(host, Qt.Key_Home)
    application.processEvents()
    assert details.property("contentY") <= 1


try:
    provider.start()
    wait_until(
        lambda: recording_sessions,
        "the worker-created production session",
    )
    recording_session = recording_sessions[0]
    wait_until(
        lambda: recording_session.snapshot.recommendations.cards
        and provider.state["recommendations"]["cards"]
        and provider.state["recommendations"]["selected_grp_id"] is not None,
        "the first production recommendation publication",
    )
    initial_snapshot = recording_session.snapshot
    initial_recommendation = selected_recommendation(provider.state)
    initial_immutable = next(
        recommendation
        for recommendation in initial_snapshot.recommendations.cards
        if recommendation.card.grp_id
            == initial_recommendation["card"]["grp_id"]
    )
    initial_concise = initial_immutable.concise_explanation
    initial_detailed = initial_immutable.explanation
    assert initial_concise
    assert initial_detailed
    assert initial_concise != initial_detailed
    wait_until(
        lambda: explanation.property("text") == initial_concise,
        "the compact production explanation",
    )
    check_mode(
        detailed=False,
        width=360,
        height=600,
        expected=initial_concise,
        stress=False,
    )
    check_mode(
        detailed=True,
        width=360,
        height=600,
        expected=initial_detailed,
        stress=False,
    )
    check_mode(
        detailed=False,
        width=550,
        height=600,
        expected=initial_concise,
        stress=False,
    )
    check_mode(
        detailed=True,
        width=550,
        height=600,
        expected=initial_detailed,
        stress=False,
    )
    check_mode(
        detailed=True,
        width=550,
        height=430,
        expected=initial_detailed,
        stress=False,
    )

    replacement_concise = "Published concise replacement."
    replacement_detailed = "Published detailed replacement."
    replacement = replace(
        initial_immutable,
        concise_explanation=replacement_concise,
        explanation=replacement_detailed,
    )
    replacement_snapshot = replace(
        recording_session.snapshot,
        recommendations=replace(
            recording_session.snapshot.recommendations,
            cards=tuple(
                replacement
                if recommendation.card.grp_id == replacement.card.grp_id
                else recommendation
                for recommendation in recording_session.snapshot.recommendations.cards
            ),
            selected_grp_id=replacement.card.grp_id,
        ),
    )
    recording_session._publish(replacement_snapshot)
    wait_until(
        lambda: provider.state["recommendations"]["selected_grp_id"]
            == replacement.card.grp_id
        and explanation.property("text") == replacement_detailed,
        "the retained detailed replacement explanation",
    )
    preview.setProperty("detailedIntel", False)
    application.processEvents()
    assert explanation.property("text") == replacement_concise, (
        explanation.property("text"),
        replacement_concise,
    )
    preview.setProperty("detailedIntel", True)
    application.processEvents()
    assert explanation.property("text") == replacement_detailed, (
        explanation.property("text"),
        replacement_detailed,
    )

    other_recommendation = next(
        recommendation
        for recommendation in recording_session.snapshot.recommendations.cards
        if recommendation.card.grp_id != replacement.card.grp_id
    )
    provider.chooseRecommendation(other_recommendation.card.grp_id)
    wait_until(
        lambda: provider.state["recommendations"]["selected_grp_id"]
            == other_recommendation.card.grp_id
        and preview.property("recommendation")["card"]["grp_id"]
            == other_recommendation.card.grp_id
        and explanation.property("text")
            == other_recommendation.explanation,
        "the selected recommendation identity and detailed explanation",
    )
    preview.setProperty("detailedIntel", False)
    application.processEvents()
    assert explanation.property("text") == other_recommendation.concise_explanation

    literal_text = "<b>literal</b> & punctuation"
    literal_recommendation = replace(
        initial_immutable,
        concise_explanation=literal_text,
        explanation=literal_text,
    )
    literal_snapshot = replace(
        recording_session.snapshot,
        recommendations=replace(
            recording_session.snapshot.recommendations,
            cards=tuple(
                literal_recommendation
                if recommendation.card.grp_id == literal_recommendation.card.grp_id
                else recommendation
                for recommendation in recording_session.snapshot.recommendations.cards
            ),
            selected_grp_id=literal_recommendation.card.grp_id,
        ),
    )
    recording_session._publish(literal_snapshot)
    wait_until(
        lambda: explanation.property("text") == literal_text,
        "the literal compact explanation",
    )
    host.resize(550, 600)
    application.processEvents()
    literal_width = QFontMetricsF(
        explanation.property("font")
    ).horizontalAdvance(literal_text)
    assert abs(explanation.property("paintedWidth") - literal_width) <= 1

    compact_long = (
        "Compact paragraph one uses ordinary words and enough content to wrap "
        "across several lines. It also includes literal <b>literal</b> & "
        "punctuation. " * 14
        + "\\n\\nCompact paragraph two keeps the explanation genuinely long "
        "so the existing Flickable must scroll rather than truncate it. "
        * 14
        + "\\n\\nCompact final sentence."
    )
    detailed_long = (
        "Detailed paragraph one uses ordinary words and enough content to wrap "
        "across several lines. It also includes literal <b>literal</b> & "
        "punctuation. " * 24
        + "\\n\\nDetailed paragraph two keeps the explanation genuinely long "
        "so the existing Flickable must scroll rather than truncate it. "
        * 24
        + "\\n\\nDetailed final sentence."
    )
    stress_recommendation = replace(
        initial_immutable,
        concise_explanation=compact_long,
        explanation=detailed_long,
    )
    stress_snapshot = replace(
        recording_session.snapshot,
        recommendations=replace(
            recording_session.snapshot.recommendations,
            cards=tuple(
                stress_recommendation
                if recommendation.card.grp_id == stress_recommendation.card.grp_id
                else recommendation
                for recommendation in recording_session.snapshot.recommendations.cards
            ),
            selected_grp_id=stress_recommendation.card.grp_id,
        ),
    )
    recording_session._publish(stress_snapshot)
    wait_until(
        lambda: explanation.property("text") == compact_long,
        "the retained compact stress explanation",
    )
    for detailed, width, height, expected in (
        (False, 360, 600, compact_long),
        (True, 360, 600, detailed_long),
        (False, 550, 600, compact_long),
        (True, 550, 600, detailed_long),
        (True, 550, 430, detailed_long),
    ):
        check_mode(
            detailed=detailed,
            width=width,
            height=height,
            expected=expected,
            stress=True,
        )

    host.setProperty("useOverride", True)
    host.setProperty("overrideRecommendation", None)
    preview.forceActiveFocus()
    preview.setProperty("detailedIntel", False)
    application.processEvents()
    assert preview.property("recommendation") is None
    assert explanation.property("text") == ""
    with log_path.open(mode="a", encoding="utf-8") as log_file:
        log_file.writelines(fixture_log_lines[7:])
    wait_until(
        lambda: (provider.state.get("draft") or {}).get("completed") is True,
        "the completed fixture draft",
    )
    provider.requestBuild("")
    wait_until(
        lambda: provider.state.get("build") is not None
        and provider.state["build"]["spells"],
        "the completed fixture build",
    )
    host.resize(550, 250)
    application.processEvents()
    build_card = provider.state["build"]["spells"][0]
    build_grp_id = build_card["card"]["grp_id"]
    host.setProperty("overrideRecommendation", build_card)
    provider.focusBuildCard(build_grp_id)
    build_name = preview.findChild(QObject, "cardPreviewName")
    build_facts = preview.findChild(QObject, "cardPreviewFacts")
    build_image = preview.findChild(QObject, "cardPreviewImage")
    assert build_name is not None
    assert build_facts is not None
    assert build_image is not None
    wait_until(
        lambda: explanation.property("text") == ""
        and build_name.property("text") == build_card["card"]["name"]
        and preview.property("imageCurrent") is True
        and provider.state["card_image"]["phase"] == "ready"
        and build_image.isVisible(),
        "the build-card preview image and empty rationale",
    )
    assert build_facts.property("text")
    assert "MV" in build_facts.property("text")
    assert details.height() > 0
    assert build_name.isVisible()
    assert build_facts.isVisible()
finally:
    preferences.shutdown()
    provider.shutdown()
    provider.wait_for_shutdown()
    host.close()
    del engine
"""
    completed = _run_qml_probe(
        probe,
        timeout=25,
        environment={"DRAFTOMEN_E2E_APP_DIR": str(app_dir)},
    )

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr


def test_completed_draft_automatically_builds_and_survives_unrelated_snapshots() -> None:
    probe = """
from dataclasses import replace
from pathlib import Path
import time
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import ApplicationPhase


def wait_until(predicate, description):
    deadline = time.monotonic() + 5
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for " + description)
        time.sleep(0.005)
    application.processEvents()


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
session = MockLiveSession(scenario="ready")
session._snapshot = replace(
    session.snapshot,
    status=replace(
        session.snapshot.status,
        phase=ApplicationPhase.DRAFT_COMPLETE,
        message="Draft complete.",
    ),
    draft=replace(session.snapshot.draft, completed=True),
    pool=replace(session.snapshot.pool, total_cards=42),
    build=None,
)
provider = MockSessionAdapter(session=session)
with TemporaryDirectory() as preferences_dir:
    preferences = GuiPreferencesAdapter(
        app_dir=preferences_dir,
        parent=application,
    )
    engine = QQmlApplicationEngine()
    qml_directory = Path.cwd() / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", "monospace")
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", "0.0")
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("initialSurface", "build")
    context.setContextProperty("initialWindowWidth", 1440)
    context.setContextProperty("initialWindowHeight", 900)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    root = engine.rootObjects()[0]
    build_view = root.findChild(QObject, "buildView")
    assert build_view is not None
    wait_until(
        lambda: provider.state.get("build") is not None,
        "the automatic completed-draft build",
    )
    assert provider.state["draft"]["completed"] is True
    assert provider.state["pool"]["total_cards"] == 42
    assert build_view.property("hasBuild") is True
    build_identity = build_view.property("buildIdentity")
    assert build_identity

    for image_phase in ("loading", "ready"):
        unrelated_state = dict(provider.state)
        unrelated_state["status"] = {
            "phase": "draft_complete",
            "message": "Draft complete; image " + image_phase + ".",
        }
        unrelated_state["card_image"] = {
            "grp_id": 104983,
            "image_path": "file:///tmp/unrelated-card.jpg",
            "phase": image_phase,
            "message": "Card image " + image_phase + ".",
        }
        provider._replace_state(state=unrelated_state)
        application.processEvents()
        assert build_view.property("hasBuild") is True
        assert build_view.property("buildIdentity") == build_identity
"""
    completed = _run_qml_probe(probe, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert "TypeError" not in completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr


def test_completed_draft_transition_requires_matching_identity_offscreen() -> None:
    probe = """
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import ApplicationPhase, LiveSessionCommand, RequestBuild


class RecordingMockSession(MockLiveSession):
    def __init__(self, *, scenario):
        super().__init__(scenario=scenario)
        self.build_request_states: list[tuple[str | None, str | None, bool]] = []

    def dispatch(self, *, command: LiveSessionCommand):
        if isinstance(command, RequestBuild):
            draft = self.snapshot.draft
            self.build_request_states.append(
                (
                    None if draft is None else draft.account_id,
                    None if draft is None else draft.draft_id,
                    draft is not None and draft.completed,
                )
            )
        return super().dispatch(command=command)


def publish(snapshot):
    session._snapshot = snapshot
    provider._publish(snapshot=snapshot)
    application.processEvents()


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
session = RecordingMockSession(scenario="ready")
ready_snapshot = session.snapshot
empty_snapshot = replace(
    ready_snapshot,
    status=replace(
        ready_snapshot.status,
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message="Waiting for a Quick Draft.",
    ),
    draft=None,
    pool=replace(ready_snapshot.pool, total_cards=0),
    build=None,
)
session._snapshot = empty_snapshot
provider = MockSessionAdapter(session=session)
with TemporaryDirectory() as preferences_dir:
    preferences = GuiPreferencesAdapter(
        app_dir=preferences_dir,
        parent=application,
    )
    engine = QQmlApplicationEngine()
    qml_directory = Path.cwd() / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", "monospace")
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", "0.0")
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("initialSurface", "live")
    context.setContextProperty("initialWindowWidth", 1440)
    context.setContextProperty("initialWindowHeight", 900)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    root = engine.rootObjects()[0]
    application.processEvents()
    assert root.property("currentSurface") == "live"
    assert session.build_request_states == []

    recovered_completed_snapshot = replace(
        empty_snapshot,
        status=replace(
            empty_snapshot.status,
            phase=ApplicationPhase.DRAFT_COMPLETE,
            message="Recovered completed draft.",
        ),
        draft=replace(
            ready_snapshot.draft,
            account_id="account-b",
            draft_id="draft-b",
            completed=True,
        ),
        pool=replace(ready_snapshot.pool, total_cards=42),
    )
    publish(recovered_completed_snapshot)
    assert root.property("currentSurface") == "live"
    assert session.build_request_states == []

    drafting_a_snapshot = replace(
        recovered_completed_snapshot,
        status=replace(
            recovered_completed_snapshot.status,
            phase=ApplicationPhase.DRAFTING,
            message="Draft A in progress.",
        ),
        draft=replace(
            ready_snapshot.draft,
            account_id="account-a",
            draft_id="draft-a",
            completed=False,
        ),
        pool=replace(ready_snapshot.pool, total_cards=41),
        build=None,
    )
    publish(drafting_a_snapshot)
    assert root.property("currentSurface") == "live"
    assert session.build_request_states == []

    completed_b_snapshot = replace(
        drafting_a_snapshot,
        status=replace(
            drafting_a_snapshot.status,
            phase=ApplicationPhase.DRAFT_COMPLETE,
            message="Draft B complete.",
        ),
        draft=replace(
            drafting_a_snapshot.draft,
            account_id="account-b",
            draft_id="draft-b",
            completed=True,
        ),
        pool=replace(ready_snapshot.pool, total_cards=42),
    )
    publish(completed_b_snapshot)
    assert root.property("currentSurface") == "live"
    assert session.build_request_states == []
    del root
    del engine
"""
    completed = _run_qml_probe(probe)
    assert completed.returncode == 0, completed.stderr
    assert "TypeError" not in completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr


def test_completed_draft_transition_rebuilds_stale_build_once_offscreen() -> None:
    probe = """
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import ApplicationPhase, LiveSessionCommand, RequestBuild


class RecordingMockSession(MockLiveSession):
    def __init__(self, *, scenario):
        super().__init__(scenario=scenario)
        self.build_request_states: list[tuple[bool, int]] = []

    def dispatch(self, *, command: LiveSessionCommand):
        if isinstance(command, RequestBuild):
            draft = self.snapshot.draft
            self.build_request_states.append(
                (
                    draft is not None and draft.completed,
                    self.snapshot.pool.total_cards,
                )
            )
        return super().dispatch(command=command)


def wait_until(predicate, description):
    deadline = time.monotonic() + 5
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for " + description)
        time.sleep(0.005)
    application.processEvents()


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
session = RecordingMockSession(scenario="ready")
drafting_snapshot = replace(
    session.snapshot,
    status=replace(
        session.snapshot.status,
        phase=ApplicationPhase.DRAFTING,
        message="Draft in progress.",
    ),
    draft=replace(session.snapshot.draft, completed=False),
    pool=replace(session.snapshot.pool, total_cards=41),
)
session._snapshot = drafting_snapshot
provider = MockSessionAdapter(session=session)
with TemporaryDirectory() as preferences_dir:
    preferences = GuiPreferencesAdapter(
        app_dir=preferences_dir,
        parent=application,
    )
    engine = QQmlApplicationEngine()
    qml_directory = Path.cwd() / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", "monospace")
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", "0.0")
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("initialSurface", "live")
    context.setContextProperty("initialWindowWidth", 1440)
    context.setContextProperty("initialWindowHeight", 900)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    root = engine.rootObjects()[0]
    build_view = root.findChild(QObject, "buildView")
    assert build_view is not None
    application.processEvents()
    assert root.property("currentSurface") == "live"
    assert session.build_request_states == []

    completed_snapshot = replace(
        drafting_snapshot,
        status=replace(
            drafting_snapshot.status,
            phase=ApplicationPhase.DRAFT_COMPLETE,
            message="Draft complete.",
        ),
        draft=replace(drafting_snapshot.draft, completed=True),
        pool=replace(drafting_snapshot.pool, total_cards=42),
    )
    session._snapshot = completed_snapshot
    provider._publish(snapshot=completed_snapshot)
    wait_until(
        lambda: root.property("currentSurface") == "build"
        and provider.state["draft"]["completed"] is True
        and provider.state["pool"]["total_cards"] == 42
        and provider.state.get("build") is not None
        and len(session.build_request_states) == 1,
        "the completed draft build",
    )
    assert session.build_request_states == [(True, 42)]
    assert build_view.property("hasBuild") is True

    unrelated_state = dict(provider.state)
    unrelated_state["status"] = dict(unrelated_state["status"])
    unrelated_state["status"]["message"] = "Deck remains available."
    provider._replace_state(state=unrelated_state)
    application.processEvents()
    assert root.property("currentSurface") == "build"
    assert len(session.build_request_states) == 1
    del root
    del engine
"""
    completed = _run_qml_probe(probe)
    assert completed.returncode == 0, completed.stderr
    assert "TypeError" not in completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr


def test_completed_draft_build_error_clear_recovers_once() -> None:
    probe = """
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import ApplicationPhase, LiveSessionCommand, RequestBuild


class RecordingMockSession(MockLiveSession):
    def __init__(self, *, scenario):
        super().__init__(scenario=scenario)
        self.commands = []

    def dispatch(self, *, command: LiveSessionCommand):
        self.commands.append(command)
        return super().dispatch(command=command)


def wait_until(predicate, description):
    deadline = time.monotonic() + 5
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for " + description)
        time.sleep(0.005)
    application.processEvents()


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
session = RecordingMockSession(scenario="build_error")
session._snapshot = replace(
    session.snapshot,
    status=replace(
        session.snapshot.status,
        phase=ApplicationPhase.DRAFT_COMPLETE,
    ),
    draft=replace(session.snapshot.draft, completed=True),
    pool=replace(session.snapshot.pool, total_cards=42),
)
provider = MockSessionAdapter(session=session)
with TemporaryDirectory() as preferences_dir:
    preferences = GuiPreferencesAdapter(
        app_dir=preferences_dir,
        parent=application,
    )
    engine = QQmlApplicationEngine()
    qml_directory = Path.cwd() / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", "monospace")
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", "0.0")
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("initialSurface", "build")
    context.setContextProperty("initialWindowWidth", 1440)
    context.setContextProperty("initialWindowHeight", 900)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    root = engine.rootObjects()[0]
    application.processEvents()
    assert provider.state["errors"][0]["operation"] == "build"
    assert root.property("automaticBuildContext") == ""
    assert session.commands == []
    unrelated_error_state = dict(provider.state)
    unrelated_error_state["status"] = dict(unrelated_error_state["status"])
    unrelated_error_state["status"]["message"] = "Build error remains visible."
    provider._replace_state(state=unrelated_error_state)
    application.processEvents()
    assert provider.state["errors"][0]["operation"] == "build"
    assert session.commands == []

    build_progress_state = dict(provider.state)
    build_progress_state["errors"] = []
    build_progress_state["build"] = None
    build_progress_state["progress"] = {
        "operation": "build",
        "message": "Building deck",
    }
    provider._replace_state(state=build_progress_state)
    application.processEvents()
    assert provider.state["progress"]["operation"] == "build"
    assert session.commands == []

    completed_state = dict(provider.state)
    completed_state["progress"] = None
    provider._replace_state(state=completed_state)
    wait_until(
        lambda: sum(isinstance(command, RequestBuild) for command in session.commands)
        == 1,
        "the deferred build request",
    )
    build_commands = [
        command for command in session.commands if isinstance(command, RequestBuild)
    ]
    assert len(build_commands) == 1
    assert root.property("automaticBuildContext") == "mock-account:mock-otj-draft"
    unrelated_state = dict(provider.state)
    unrelated_state["status"] = dict(unrelated_state["status"])
    unrelated_state["status"]["message"] = "Build remains available."
    provider._replace_state(state=unrelated_state)
    application.processEvents()
    assert sum(isinstance(command, RequestBuild) for command in session.commands) == 1
    del root
    del engine
"""
    completed = _run_qml_probe(probe)
    assert completed.returncode == 0, completed.stderr
    assert "TypeError" not in completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr


def test_qml_keyboard_controls_dispatch_account_and_ratings_commands_offscreen() -> None:
    probe = """
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import ChooseAccount, RequestRatingsDownload


class RecordingProvider(MockSessionAdapter):
    def __init__(self) -> None:
        self.commands = []
        super().__init__(session=MockLiveSession(scenario="warning"))

    def _dispatch(self, *, command) -> None:
        self.commands.append(command)
        super()._dispatch(command=command)


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
provider = RecordingProvider()
preference_dir = TemporaryDirectory()
preferences = GuiPreferencesAdapter(app_dir=preference_dir.name)
engine = QQmlApplicationEngine()
qml_directory = Path.cwd() / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "live")
context.setContextProperty("initialWindowWidth", 1440)
context.setContextProperty("initialWindowHeight", 900)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
root = engine.rootObjects()[0]
application.processEvents()

account_selector = root.findChild(QObject, "accountSelector")
assert account_selector is not None
account_selector.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
QTest.keyClick(root, Qt.Key_Return)
application.processEvents()
assert any(isinstance(command, ChooseAccount) for command in provider.commands)
assert provider.state["ratings"]["phase"] == "missing"
unavailable_state = dict(provider.state)
unavailable_state["ratings"] = dict(unavailable_state["ratings"])
unavailable_state["ratings"]["phase"] = "unavailable"
provider._replace_state(state=unavailable_state)
application.processEvents()
assert provider.state["ratings"]["phase"] == "unavailable"


download = root.findChild(QObject, "ratingsDownloadButton")
assert download is not None
assert download.property("visible") is True
download.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
dialog = root.findChild(QObject, "ratingsDownloadDialog")
background = root.findChild(QObject, "ratingsDownloadDialogBackground")
header = root.findChild(QObject, "ratingsDownloadDialogHeader")
title = root.findChild(QObject, "ratingsDownloadDialogTitle")
message = root.findChild(QObject, "ratingsDownloadDialogMessage")
footer = root.findChild(QObject, "ratingsDownloadDialogFooter")
footer_background = root.findChild(QObject, "ratingsDownloadDialogFooterBackground")
confirm = root.findChild(QObject, "ratingsDownloadConfirmButton")
assert dialog is not None
assert dialog.property("visible") is True
assert dialog.property("modal") is True
assert all(
    item is not None
    for item in (background, header, title, message, footer, footer_background, confirm)
)
window_width = float(root.property("width"))
window_height = float(root.property("height"))
dialog_width = float(dialog.property("width"))
dialog_height = float(dialog.property("height"))
assert dialog_width <= window_width - 32
assert dialog_height <= window_height - 32
assert abs(float(dialog.property("x")) - (window_width - dialog_width) / 2) <= 1
assert abs(float(dialog.property("y")) - (window_height - dialog_height) / 2) <= 1
assert QColor(background.property("color")) == QColor("#11142a")
assert int(background.property("radius")) == 4
assert float(header.property("implicitHeight")) == 52
assert QColor(header.property("color")) == QColor("#191d3b")
assert QColor(title.property("color")) == QColor("#f5f1e8")
assert footer.property("alignment") != 0
assert QColor(footer_background.property("color")) == QColor("#191d3b")
QTest.keyClick(root, Qt.Key_Escape)
application.processEvents()
assert dialog.property("visible") is False
assert download.property("activeFocus") is True

download.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert dialog.property("visible") is True
confirm.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert dialog.property("visible") is False
assert any(isinstance(command, RequestRatingsDownload) for command in provider.commands)
assert provider.state["ratings"]["phase"] == "loading"
provider._replace_state(state=unavailable_state)
application.processEvents()
settings = root.findChild(QObject, "settingsButton")
assert settings is not None
settings.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
settings_download = root.findChild(QObject, "settingsRatingsDownloadButton")
assert settings_download is not None
settings_download.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
settings_dialog = root.findChild(QObject, "settingsRatingsDownloadDialog")
assert settings_dialog is not None
assert settings_dialog.property("visible") is True
settings_cancel = root.findChild(QObject, "settingsRatingsDownloadCancelButton")
assert settings_cancel is not None
settings_cancel.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert settings_dialog.property("visible") is False

settings_download.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert settings_dialog.property("visible") is True
QTest.keyClick(root, Qt.Key_Escape)
application.processEvents()
assert settings_dialog.property("visible") is False

settings_download.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert settings_dialog.property("visible") is True
settings_confirm = root.findChild(QObject, "settingsRatingsDownloadConfirmButton")
assert settings_confirm is not None
settings_confirm.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert any(
    isinstance(command, RequestRatingsDownload) for command in provider.commands
)
assert provider.state["ratings"]["phase"] == "loading"
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr


def test_qml_settings_hosted_ratings_refresh_dialog_offscreen() -> None:
    probe = """
import gzip
import hashlib
import io
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from PySide6.QtCore import QObject, QUrl, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen import __version__
from draftomen.carddb import CardDatabase
from draftomen.profile_client import ProfileClient, ProfileNetworkPolicy
from draftomen.profile_manifest import ProfileManifest, ProfileManifestArtifact
from draftomen.qt_adapter import GuiPreferencesAdapter, LiveSessionAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.session import LiveSession
from draftomen.set_profile import SET_PROFILE_SCHEMA_VERSION, load_set_profile


def wait_until(predicate, description):
    deadline = time.monotonic() + 8
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for " + description)
        time.sleep(0.005)
    application.processEvents()


class Response:
    def __init__(self, payload: bytes, url: str):
        self._payload = io.BytesIO(payload)
        self._url = url

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        pass


project_root = Path.cwd()
app_dir = TemporaryDirectory()
app_path = Path(app_dir.name)
manifest_url = "https://profiles.example.test/v1/manifest.json"
artifact_url = "https://profiles.example.test/v1/tst-quickdraft.json.gz"
cached_profile = load_set_profile(
    project_root / "tests" / "fixtures" / "set-profiles" / "early.json",
    expected_set_code="TST",
    expected_format="QuickDraft",
)
updated_profile = replace(
    cached_profile,
    profile_version="e2e-updated",
    generated_at="2026-09-02T00:00:00+00:00",
)
cached_client = ProfileClient(
    app_dir=app_path,
    manifest_url=manifest_url,
    network_policy=ProfileNetworkPolicy.OFFLINE,
)
cache_path = cached_client.profile_path("TST", "QuickDraft")
cache_path.parent.mkdir(parents=True, exist_ok=True)
cache_path.write_bytes(cached_profile.to_bytes())
artifact_bytes = gzip.compress(updated_profile.to_bytes(), mtime=0)
artifact = ProfileManifestArtifact(
    set_code=updated_profile.set_code,
    event_format=updated_profile.event_format,
    set_profile_schema_version=SET_PROFILE_SCHEMA_VERSION,
    profile_version=updated_profile.profile_version,
    generated_at=updated_profile.generated_at,
    url=artifact_url,
    gzip_bytes=len(artifact_bytes),
    profile_bytes=len(updated_profile.to_bytes()),
    gzip_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
    profile_sha256=hashlib.sha256(updated_profile.to_bytes()).hexdigest(),
    maturity=updated_profile.maturity,
)
payloads = {
    manifest_url: ProfileManifest(
        artifacts=(artifact,),
        published_at="2026-09-02T00:00:00+00:00",
    ).to_bytes(),
    artifact_url: artifact_bytes,
}
requests = []


def opener(request, *, timeout):
    del timeout
    requests.append(request.full_url)
    return Response(payloads[request.full_url], request.full_url)


client = ProfileClient(
    app_dir=app_path,
    manifest_url=manifest_url,
    network_policy=ProfileNetworkPolicy.OFFLINE,
    opener=opener,
)


def factory(publish):
    session = LiveSession(
        log_path=app_path / "Player.log",
        card_database=CardDatabase(cards={}),
        app_dir=app_path,
        poll_interval=0.01,
        snapshot_publisher=publish,
        profile_client=client,
    )
    session._set_active_set_code(set_code="TST")
    return session


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
provider = LiveSessionAdapter(
    session_factory=factory,
    poll_interval_ms=10,
    startup_scan=False,
    profile_client=client,
)
preferences = GuiPreferencesAdapter(app_dir=app_path)
engine = QQmlApplicationEngine()
qml_directory = project_root / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "settings")
context.setContextProperty("initialWindowWidth", 900)
context.setContextProperty("initialWindowHeight", 700)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
root = engine.rootObjects()[0]
download = root.findChild(QObject, "settingsRatingsDownloadButton")
dialog = root.findChild(QObject, "settingsRatingsDownloadDialog")
confirm = root.findChild(QObject, "settingsRatingsDownloadConfirmButton")
status = root.findChild(QObject, "settingsProfileCacheStatus")
assert download is not None
assert dialog is not None
assert confirm is not None
assert status is not None

provider.start()
try:
    wait_until(
        lambda: download.property("enabled") is True
        and provider.state["set_profile"]["source"] == "local-early",
        "cached hosted profile state in the settings control",
    )
    assert provider.state["set_profile"]["refresh_outcome"] == "cached"
    assert requests == []

    download.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    wait_until(lambda: dialog.property("visible") is True, "ratings refresh dialog")
    QTest.keyClick(root, Qt.Key_Escape)
    wait_until(lambda: dialog.property("visible") is False, "ratings refresh cancellation")
    assert download.property("activeFocus") is True

    client.network_policy = ProfileNetworkPolicy.ALLOWED
    download.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    wait_until(lambda: dialog.property("visible") is True, "ratings refresh confirmation dialog")
    confirm.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    wait_until(lambda: dialog.property("visible") is False, "ratings refresh dialog close")
    wait_until(
        lambda: provider.state["set_profile"]["refresh_outcome"]
        in ("updated", "unchanged"),
        "shared hosted profile refresh outcome",
    )
    outcome = provider.state["set_profile"]["refresh_outcome"]
    assert requests == [manifest_url, artifact_url]
    assert status.property("text") == "TST · early · " + outcome
finally:
    preferences.shutdown()
    provider.shutdown()
    provider.wait_for_shutdown()
    del engine
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr


def test_qml_state_banner_renders_generic_progress_and_excludes_ratings_loader() -> None:
    probe = """
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter, SessionAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.session import (
    DataLoadPhase,
    OperationKind,
    ProgressState,
    RatingsState,
    SetProfileState,
)


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
base_snapshot = MockLiveSession().snapshot
provider = SessionAdapter(snapshot=base_snapshot)
preferences_dir = TemporaryDirectory()
preferences = GuiPreferencesAdapter(app_dir=preferences_dir.name)
engine = QQmlApplicationEngine()
qml_directory = Path.cwd() / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "live")
context.setContextProperty("initialWindowWidth", 900)
context.setContextProperty("initialWindowHeight", 700)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
root = engine.rootObjects()[0]
banner = root.findChild(QObject, "liveStateBanner")
progress_bar = root.findChild(QObject, "stateProgressBar")
assert banner is not None
assert progress_bar is not None

ready_snapshot = replace(
    base_snapshot,
    set_profile=SetProfileState(
        set_code="TST",
        event_format="QuickDraft",
        maturity="mature",
        profile_version="test",
        source="test",
        phase=DataLoadPhase.READY,
        refresh_outcome="unchanged",
        message="Set profile ready.",
    ),
    ratings=RatingsState(
        set_code="TST",
        phase=DataLoadPhase.UNAVAILABLE,
        message="Ratings are not loaded.",
    ),
)

build_snapshot = replace(
    ready_snapshot,
    progress=ProgressState(
        operation=OperationKind.BUILD,
        message="Building deck",
        completed=2,
        total=4,
    ),
)
root.setProperty("currentSurface", "live")
provider._apply_snapshot(build_snapshot)
application.processEvents()
assert banner.property("visible") is True
assert banner.property("bannerTitle") == "Working"
assert banner.property("bannerMessage") == "Building deck"
assert progress_bar.property("visible") is True
assert progress_bar.property("indeterminate") is False
assert progress_bar.property("value") == 2

backtest_snapshot = replace(
    ready_snapshot,
    progress=ProgressState(
        operation=OperationKind.BACKTEST,
        message="Running backtest",
    ),
)
root.setProperty("currentSurface", "live")
provider._apply_snapshot(backtest_snapshot)
application.processEvents()
assert banner.property("bannerMessage") == "Running backtest"
assert progress_bar.property("visible") is True
assert progress_bar.property("indeterminate") is True

ratings_snapshot = replace(
    ready_snapshot,
    progress=ProgressState(
        operation=OperationKind.RATINGS,
        message="Downloading ratings",
    ),
)
root.setProperty("currentSurface", "live")
provider._apply_snapshot(ratings_snapshot)
application.processEvents()
assert banner.property("bannerTitle") == "Ratings unavailable"
assert banner.property("bannerMessage") == "Ratings are not loaded."
assert progress_bar.property("visible") is False

preferences.shutdown()
del root
del engine
preferences_dir.cleanup()
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr


def test_qml_recent_pick_preview_uses_delayed_bounded_hover_offscreen() -> None:
    probe = """
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen import __version__
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.qt_mock import MockSessionAdapter
from draftomen.mock_session import MockLiveSession


def find_visual_item(item: QQuickItem, object_name: str) -> QQuickItem | None:
    if item.objectName() == object_name:
        return item
    for child in item.childItems():
        found = find_visual_item(child, object_name)
        if found is not None:
            return found
    return None


def visible_item_named(name: str) -> QQuickItem | None:
    return next(
        (
            item
            for item in root.findChildren(QObject, name)
            if isinstance(item, QQuickItem) and item.isVisible()
        ),
        None,
    )


def mouse_center(item: QQuickItem) -> QPoint:
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    return QPoint(round(point.x()), round(point.y()))


def item_bounds(item: QQuickItem) -> tuple[float, float, float, float]:
    top_left = item.mapToItem(root.contentItem(), QPointF(0, 0))
    bottom_right = item.mapToItem(
        root.contentItem(), QPointF(item.width(), item.height())
    )
    return top_left.x(), top_left.y(), bottom_right.x(), bottom_right.y()


def item_center(item: QQuickItem) -> QPointF:
    return item.mapToItem(
        root.contentItem(), QPointF(item.width() / 2, item.height() / 2)
    )


def center_is_inside(item: QQuickItem, container: QQuickItem) -> bool:
    center = item_center(item)
    left, top, right, bottom = item_bounds(container)
    return left <= center.x() <= right and top <= center.y() <= bottom
def assert_preview_does_not_overlap_gallery(
    preview: QQuickItem, gallery: QQuickItem
) -> None:
    preview_left, preview_top, preview_right, preview_bottom = item_bounds(preview)
    gallery_left, gallery_top, gallery_right, gallery_bottom = item_bounds(gallery)
    assert (
        preview_right <= gallery_left
        or preview_left >= gallery_right
        or preview_bottom <= gallery_top
        or preview_top >= gallery_bottom
    )


def preview_point_without_thumbnail(
    preview: QQuickItem, thumbnails: list[QQuickItem]
) -> QPoint:
    candidates = (
        (16, 16),
        (preview.width() - 16, 16),
        (16, preview.height() - 16),
        (preview.width() - 16, preview.height() - 16),
        (preview.width() / 2, preview.height() / 2),
    )
    for x, y in candidates:
        point = preview.mapToItem(root.contentItem(), QPointF(x, y))
        if not any(
            item_bounds(thumbnail)[0] <= point.x() <= item_bounds(thumbnail)[2]
            and item_bounds(thumbnail)[1] <= point.y() <= item_bounds(thumbnail)[3]
            for thumbnail in thumbnails
        ):
            scene_point = preview.mapToScene(QPointF(x, y))
            return QPoint(round(scene_point.x()), round(scene_point.y()))
    raise AssertionError("preview has no deterministic point outside thumbnails")


def assert_preview_inside_window(preview: QQuickItem) -> None:
    left, top, right, bottom = item_bounds(preview)
    content = root.contentItem()
    assert left >= 0
    assert top >= 0
    assert right <= content.width()
    assert bottom <= content.height()


def pump(milliseconds: int) -> None:
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.005)
    application.processEvents()


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
preferences_dir = TemporaryDirectory()
provider = MockSessionAdapter(session=MockLiveSession(scenario="ready"))
preferences = GuiPreferencesAdapter(app_dir=preferences_dir.name)
engine = QQmlApplicationEngine()
qml_directory = Path.cwd() / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "live")
context.setContextProperty("initialWindowWidth", 680)
context.setContextProperty("initialWindowHeight", 640)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
assert engine.rootObjects()
root = engine.rootObjects()[0]
root.resize(680, 1200)
application.processEvents()
detail_tabs = root.findChild(QObject, "liveDetailTabs")
assert detail_tabs is not None
detail_tabs.setProperty("currentIndex", 1)
application.processEvents()
pool_flickable = visible_item_named("poolSummaryFlickable")
gallery = visible_item_named("recentPicksGallery")
assert pool_flickable is not None and gallery is not None
assert gallery.isVisible()
gallery_top_left = gallery.mapToItem(pool_flickable, QPointF(0, 0))
current_content_y = float(pool_flickable.property("contentY"))
pool_flickable.setProperty(
    "contentY", max(0.0, current_content_y + gallery_top_left.y() - 8.0)
)
application.processEvents()
thumbnails = [
    find_visual_item(gallery, f"recentPickThumbnail{index}")
    for index in range(12)
]
assert all(thumbnail is not None for thumbnail in thumbnails)
thumbnails = [thumbnail for thumbnail in thumbnails if thumbnail is not None]
first_thumbnail = thumbnails[0]
hover_timers = root.findChildren(QObject, "recentPickHoverTimer")
all_previews = root.findChildren(QObject, "recentPickPreview")
previews = gallery.findChildren(QObject, "recentPickPreview")
assert first_thumbnail.isVisible()
assert hover_timers and all(timer.property("interval") > 500 for timer in hover_timers)
assert len(previews) == 1
preview = previews[0]
assert preview.property("visible") is False

accessible_thumbnail = QAccessible.queryAccessibleInterface(first_thumbnail)
assert accessible_thumbnail is not None
assert accessible_thumbnail.role() == QAccessible.Role.Graphic
assert accessible_thumbnail.text(QAccessible.Text.Name) == (
    provider.state["pool"]["recent_picks"][0]["card"]["name"]
)
assert "Hover for a card preview." in accessible_thumbnail.text(
    QAccessible.Text.Description
)
assert first_thumbnail.property("activeFocusOnTab") in (None, False)

QTest.mouseMove(root, mouse_center(first_thumbnail))
pump(250)
assert preview.property("visible") is False
QTest.mouseMove(root, QPoint(2, 2))
pump(130)
assert preview.property("visible") is False

QTest.mouseMove(root, mouse_center(first_thumbnail))
pump(250)
assert preview.property("visible") is False
pump(300)
assert preview.property("visible") is True
assert preview.property("modal") is False
assert preview.property("focus") is False
first_grp_id = provider.state["pool"]["recent_picks"][0]["card"]["grp_id"]
previewed = gallery.property("previewedPick")
assert previewed["card"]["grp_id"] == first_grp_id
assert_preview_inside_window(preview)
assert_preview_does_not_overlap_gallery(preview, gallery)
assert preview.width() > first_thumbnail.width()
assert root.findChild(QObject, "recentPickPreviewDialog") is None

preview_point = preview_point_without_thumbnail(preview, thumbnails)
QTest.mouseMove(root, preview_point)
pump(30)
assert gallery.property("previewHovered") is True
assert preview.property("visible") is True
assert gallery.property("previewedPick")["card"]["grp_id"] == first_grp_id
QTest.mouseMove(root, mouse_center(first_thumbnail))
pump(50)
assert preview.property("visible") is True
assert gallery.property("previewedPick")["card"]["grp_id"] == first_grp_id
assert all(timer.property("running") is False for timer in hover_timers)
pump(250)
assert preview.property("visible") is True
assert gallery.property("previewedPick")["card"]["grp_id"] == first_grp_id

next_thumbnail = next(
    (
        thumbnail
        for thumbnail in thumbnails[1:]
        if thumbnail.isVisible() and center_is_inside(thumbnail, pool_flickable)
    ),
    None,
)
assert next_thumbnail is not None
next_index = int(next_thumbnail.objectName().removeprefix("recentPickThumbnail"))
next_grp_id = provider.state["pool"]["recent_picks"][next_index]["card"]["grp_id"]
QTest.mouseMove(root, mouse_center(next_thumbnail))
pump(30)
assert preview.property("visible") is False
assert gallery.property("previewedPick") is None
pump(300)
assert preview.property("visible") is False
assert gallery.property("previewedPick") is None
pump(260)
assert preview.property("visible") is True
assert gallery.property("previewedPick")["card"]["grp_id"] == next_grp_id
assert sum(item.property("visible") for item in all_previews) == 1
assert_preview_inside_window(preview)
assert_preview_does_not_overlap_gallery(preview, gallery)
assert preview.width() > next_thumbnail.width()

root.resize(680, 640)
pump(100)
assert root.width() == 680
assert root.height() == 640
assert preview.property("visible") is True
assert gallery.property("previewedPick")["card"]["grp_id"] == next_grp_id
assert_preview_inside_window(preview)
assert_preview_does_not_overlap_gallery(preview, gallery)
assert preview.width() > next_thumbnail.width()

QTest.mouseClick(
    root,
    Qt.LeftButton,
    Qt.NoModifier,
    mouse_center(next_thumbnail),
)
application.processEvents()
assert preview.property("visible") is True
assert gallery.property("previewedPick")["card"]["grp_id"] == next_grp_id
assert root.findChild(QObject, "recentPickPreviewDialog") is None
QTest.mouseMove(root, QPoint(2, 2))
pump(130)
assert preview.property("visible") is False
"""
    completed = _run_qml_probe(probe)
    assert completed.returncode == 0, completed.stderr


def test_qml_tab_and_shift_tab_traversal_stays_on_surfaces_and_dialogs_offscreen() -> None:
    probe = """
from pathlib import Path
import time
from tempfile import TemporaryDirectory

from PySide6.QtCore import QMetaObject, QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.qt_mock import MockSessionAdapter


def find_visual_item(item: QQuickItem, object_name: str) -> QQuickItem | None:
    if item.objectName() == object_name:
        return item
    for child in item.childItems():
        found = find_visual_item(child, object_name)
        if found is not None:
            return found
    return None


def mouse_center(item: QQuickItem) -> QPoint:
    point = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    return QPoint(round(point.x()), round(point.y()))


def assert_visual_item_inside(parent: QQuickItem, child: QQuickItem) -> None:
    parent_top_left = parent.mapToScene(QPointF(0, 0))
    parent_bottom_right = parent.mapToScene(
        QPointF(parent.width(), parent.height())
    )
    child_top_left = child.mapToScene(QPointF(0, 0))
    child_bottom_right = child.mapToScene(
        QPointF(child.width(), child.height())
    )
    assert child_top_left.x() >= parent_top_left.x()
    assert child_top_left.y() >= parent_top_left.y()
    assert child_bottom_right.x() <= parent_bottom_right.x()
    assert child_bottom_right.y() <= parent_bottom_right.y()

def wait_until(predicate, description: str) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for " + description)
        time.sleep(0.005)
    application.processEvents()


def image_source(image: QObject) -> str:
    source = image.property("source")
    return source.toString() if isinstance(source, QUrl) else str(source)


def visible_texts(item: QQuickItem) -> list[str]:
    values = []
    text = item.property("text")
    if item.isVisible() and isinstance(text, str) and text:
        values.append(text)
    for child in item.childItems():
        values.extend(visible_texts(child))
    return values


def has_focus(item: QObject) -> bool:
    return bool(item.property("activeFocus"))


def tab_to(root: QObject, item: QObject, *, maximum: int = 40) -> None:
    for _ in range(maximum):
        QTest.keyClick(root, Qt.Key_Tab)
        application.processEvents()
        if has_focus(item):
            return
    raise AssertionError("keyboard traversal did not reach " + item.objectName())


def assert_surface_round_trip(root: QObject, item: QObject) -> None:
    tab_to(root, item)
    QTest.keyClick(root, Qt.Key_Tab)
    application.processEvents()
    assert not has_focus(item)
    QTest.keyClick(root, Qt.Key_Tab, Qt.ShiftModifier)
    application.processEvents()
    assert has_focus(item)


def assert_modal_cycle(
    root: QObject,
    opener: QObject,
    dialog_name: str,
    cancel_name: str,
    confirm_name: str,
) -> None:
    tab_to(root, opener)
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    dialog = root.findChild(QObject, dialog_name)
    cancel = root.findChild(QObject, cancel_name)
    confirm = root.findChild(QObject, confirm_name)
    assert dialog is not None and dialog.property("visible") is True
    assert cancel is not None and confirm is not None
    tab_to(root, cancel, maximum=4)
    QTest.keyClick(root, Qt.Key_Tab)
    application.processEvents()
    assert has_focus(confirm)
    QTest.keyClick(root, Qt.Key_Tab)
    application.processEvents()
    assert has_focus(cancel)
    QTest.keyClick(root, Qt.Key_Tab, Qt.ShiftModifier)
    application.processEvents()
    assert has_focus(confirm)
    QTest.keyClick(root, Qt.Key_Escape)
    application.processEvents()
    assert dialog.property("visible") is False
    assert has_focus(opener)


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
preferences_dir = TemporaryDirectory()
provider = MockSessionAdapter(session=MockLiveSession(scenario="warning"))
preferences = GuiPreferencesAdapter(app_dir=preferences_dir.name)
engine = QQmlApplicationEngine()
qml_directory = Path.cwd() / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "live")
context.setContextProperty("initialWindowWidth", 1440)
context.setContextProperty("initialWindowHeight", 900)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
root = engine.rootObjects()[0]
application.processEvents()

live_view = root.findChild(QObject, "liveDraftView")
assert live_view is not None
wide_card_details_width = int(
    live_view.property("wideCardDetailsWidth")
)
assert wide_card_details_width == 550
wide_recommendation_list_width = int(
    live_view.property("wideRecommendationsMinimumListWidth")
)
assert wide_recommendation_list_width == 662
assert int(live_view.property("wideRecommendationsMinimumWidth")) == (
    wide_recommendation_list_width + wide_card_details_width + 12
)

ranking = root.findChild(QObject, "rankingSelector")
assert ranking is not None
assert_surface_round_trip(root, ranking)
wide_preview = find_visual_item(root.contentItem(), "wideLiveCardPreview")
wide_pool = find_visual_item(root.contentItem(), "wideLivePoolDetails")
wide_row = find_visual_item(root.contentItem(), "wideRecommendationRow1")
second_row = find_visual_item(root.contentItem(), "wideRecommendationRow2")
third_row = find_visual_item(root.contentItem(), "wideRecommendationRow3")
assert wide_preview is not None and wide_preview.isVisible()
assert wide_pool is not None and wide_pool.isVisible()
assert wide_row is not None and wide_row.isVisible()
assert second_row is not None and second_row.isVisible()
assert third_row is not None and third_row.isVisible()

wide_on_color_filter = root.findChild(QObject, "wideRecommendationOnColorFilter")
wide_all_filter = root.findChild(QObject, "wideRecommendationAllFilter")
assert wide_on_color_filter is not None and wide_all_filter is not None
assert wide_on_color_filter.property("text") == "On Color"
assert wide_all_filter.property("text") == "All"
assert wide_all_filter.property("checked") is True
recommendation_model = provider.recommendationsModel
source_recommendation_ids = [
    recommendation["card"]["grp_id"]
    for recommendation in provider.state["recommendations"]["cards"]
]
on_color_ids = [
    recommendation["card"]["grp_id"]
    for recommendation in provider.state["recommendations"]["cards"]
    if not recommendation["card"]["colors"]
    or all(
        color in provider.state["pool"]["current_colors"]
        for color in recommendation["card"]["colors"]
    )
]
QTest.mouseClick(
    root,
    Qt.LeftButton,
    Qt.NoModifier,
    mouse_center(wide_on_color_filter),
)
application.processEvents()
assert wide_on_color_filter.property("checked") is True
assert recommendation_model.rowCount() == len(on_color_ids)
assert [
    recommendation_model.data(
        recommendation_model.index(index, 0),
        recommendation_model.MODEL_DATA_ROLE,
    )["card"]["grp_id"]
    for index in range(recommendation_model.rowCount())
] == on_color_ids
assert [
    recommendation["card"]["grp_id"]
    for recommendation in provider.state["recommendations"]["cards"]
] == source_recommendation_ids
QTest.mouseClick(
    root,
    Qt.LeftButton,
    Qt.NoModifier,
    mouse_center(wide_all_filter),
)
application.processEvents()
assert wide_all_filter.property("checked") is True
assert recommendation_model.rowCount() == len(source_recommendation_ids)

second_row.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Return)
application.processEvents()
assert provider.state["recommendations"]["selected_grp_id"] == (
    provider.state["recommendations"]["cards"][1]["card"]["grp_id"]
)
third_row.forceActiveFocus()
application.processEvents()
assert wide_row.property("stateText") == "Recommended"
assert second_row.property("stateText") == "Selected"
assert third_row.property("stateText") == "Keyboard focused"

image_path = Path.cwd() / "draftomen" / "assets" / "draftomen_logo.png"
image_url = QUrl.fromLocalFile(str(image_path)).toString()
missing_image_url = QUrl.fromLocalFile(
    str(Path(preferences_dir.name) / "missing-card-image.png")
).toString()
long_name = "A very long recommendation name that must remain readable within its row"
recommendations_state = dict(provider.state["recommendations"])
recommendation_cards = []
for index, recommendation in enumerate(recommendations_state["cards"]):
    updated_recommendation = dict(recommendation)
    updated_card = dict(recommendation["card"])
    updated_card["image_path"] = None
    if index == 0:
        updated_card["image_path"] = image_url
        updated_card["name"] = long_name
    elif index == 2:
        updated_card["image_path"] = missing_image_url
    updated_recommendation["card"] = updated_card
    if index == 2:
        updated_recommendation["win_rate"] = None
        updated_recommendation["letter_grade"] = None
        updated_recommendation["color_fit"] = None
        updated_recommendation["average_last_seen_at"] = None
        updated_recommendation["source_label"] = None
    recommendation_cards.append(updated_recommendation)
recommendations_state["cards"] = recommendation_cards
state_with_thumbnails = dict(provider.state)
state_with_thumbnails["recommendations"] = recommendations_state
provider._replace_state(state=state_with_thumbnails)
application.processEvents()
wide_row = find_visual_item(root.contentItem(), "wideRecommendationRow1")
second_row = find_visual_item(root.contentItem(), "wideRecommendationRow2")
third_row = find_visual_item(root.contentItem(), "wideRecommendationRow3")
assert wide_row is not None and second_row is not None and third_row is not None
wide_header_rank = find_visual_item(root.contentItem(), "recommendationHeaderRank")
wide_header_card = find_visual_item(root.contentItem(), "recommendationHeaderCard")
wide_rank = find_visual_item(wide_row, "recommendationRank")
wide_card = find_visual_item(wide_row, "recommendationCardCell")
assert wide_header_rank is not None and wide_header_card is not None
assert wide_rank is not None and wide_card is not None
assert wide_rank.mapToScene(QPointF(0, 0)).x() < \
    wide_card.mapToScene(QPointF(0, 0)).x()
assert abs(
    wide_header_rank.mapToScene(QPointF(0, 0)).x()
    - wide_rank.mapToScene(QPointF(0, 0)).x()
) <= 2
assert abs(
    wide_header_card.mapToScene(QPointF(0, 0)).x()
    - wide_card.mapToScene(QPointF(0, 0)).x()
) <= 2

wide_thumbnail_frame = find_visual_item(
    wide_row, "recommendationThumbnailFrame"
)
wide_thumbnail_image = find_visual_item(
    wide_row, "recommendationThumbnailImage"
)
wide_thumbnail_fallback = find_visual_item(
    wide_row, "recommendationThumbnailFallback"
)
wide_thumbnail_label = find_visual_item(
    wide_row, "recommendationThumbnailFallbackLabel"
)
assert wide_thumbnail_frame is not None and wide_thumbnail_frame.isVisible()
assert wide_thumbnail_image is not None
assert wide_thumbnail_fallback is not None and wide_thumbnail_label is not None
assert wide_thumbnail_frame.width() == 50
assert wide_thumbnail_frame.height() > 0
assert_visual_item_inside(wide_card, wide_thumbnail_frame)
assert wide_rank.mapToScene(QPointF(0, 0)).x() < \
    wide_thumbnail_frame.mapToScene(QPointF(0, 0)).x()
wide_thumbnail_top_left = wide_thumbnail_frame.mapToItem(
    wide_row, QPointF(0, 0)
)
wide_thumbnail_bottom_right = wide_thumbnail_frame.mapToItem(
    wide_row, QPointF(wide_thumbnail_frame.width(), wide_thumbnail_frame.height())
)
assert wide_thumbnail_top_left.x() >= -1
assert wide_thumbnail_top_left.y() >= -1
assert wide_thumbnail_bottom_right.x() <= wide_row.width() + 1
assert wide_thumbnail_bottom_right.y() <= wide_row.height() + 1
wait_until(
    lambda: wide_thumbnail_image.isVisible(),
    "the visible locally published recommendation thumbnail",
)
assert image_source(wide_thumbnail_image) == image_url
assert wide_thumbnail_fallback.isVisible() is False

wide_missing_fallback = find_visual_item(
    second_row, "recommendationThumbnailFallback"
)
wide_missing_label = find_visual_item(
    second_row, "recommendationThumbnailFallbackLabel"
)
assert wide_missing_fallback is not None and wide_missing_fallback.isVisible()
assert wide_missing_label is not None
assert wide_missing_label.property("text") == "No image available"

wide_error_image = find_visual_item(third_row, "recommendationThumbnailImage")
wide_error_fallback = find_visual_item(
    third_row, "recommendationThumbnailFallback"
)
wide_error_label = find_visual_item(
    third_row, "recommendationThumbnailFallbackLabel"
)
assert wide_error_image is not None
assert wide_error_fallback is not None and wide_error_label is not None
wait_until(
    lambda: wide_error_fallback.isVisible()
    and wide_error_label.property("text") == "Image failed to load",
    "the labelled failed recommendation thumbnail fallback",
)
assert wide_error_image.isVisible() is False
assert wide_preview.mapToItem(root.contentItem(), QPointF(0, wide_preview.height())).y() \
    <= wide_pool.mapToItem(root.contentItem(), QPointF(0, 0)).y()
assert wide_preview.property("imageFrameHeight") > 0
assert wide_preview.findChild(QObject, "cardPreviewFacts") is not None
assert wide_preview.findChild(QObject, "cardPreviewScores") is not None
assert wide_preview.findChild(QObject, "cardPreviewExplanation") is not None
wide_frame = find_visual_item(wide_preview, "cardPreviewImageFrame")
wide_details = find_visual_item(wide_preview, "cardPreviewDetails")
assert wide_frame is not None and wide_frame.isVisible()
assert wide_details is not None and wide_details.isVisible()
assert wide_preview.width() == wide_card_details_width
assert wide_row.width() > wide_preview.width()
assert wide_frame.width() == 250
assert wide_frame.height() == 350
assert wide_frame.height() / 280 == 1.25
assert wide_details.width() >= 180
assert wide_details.width() < wide_preview.width() / 2, (
    wide_preview.width(),
    wide_preview.height(),
    wide_frame.width(),
    wide_frame.height(),
    wide_details.width(),
    wide_details.height(),
)
assert_visual_item_inside(wide_preview, wide_frame)
assert_visual_item_inside(wide_preview, wide_details)
wide_frame_right = wide_frame.mapToItem(
    wide_preview, QPointF(wide_frame.width(), 0)
).x()
wide_details_left = wide_details.mapToItem(
    wide_preview, QPointF(0, 0)
).x()
assert wide_frame_right <= wide_details_left
wide_row_right = wide_row.mapToItem(
    root.contentItem(), QPointF(wide_row.width(), 0)
).x()
wide_preview_left = wide_preview.mapToItem(
    root.contentItem(), QPointF(0, 0)
).x()
assert wide_row_right <= wide_preview_left
confidence = root.findChild(QObject, "recommendationConfidenceSummary")
assert confidence is not None
state_without_confidence = dict(provider.state)
recommendations_without_confidence = dict(state_without_confidence["recommendations"])
recommendations_without_confidence["confidence_summary"] = None
state_without_confidence["recommendations"] = recommendations_without_confidence
provider._replace_state(state=state_without_confidence)
application.processEvents()
assert confidence.isVisible() is False
state_with_confidence = dict(provider.state)
recommendations_with_confidence = dict(state_with_confidence["recommendations"])
recommendations_with_confidence["confidence_summary"] = "Current pool confidence: 64%"
state_with_confidence["recommendations"] = recommendations_with_confidence
provider._replace_state(state=state_with_confidence)
application.processEvents()
assert confidence.isVisible()
assert confidence.property("text") == "Current pool confidence: 64%"
wide_row = find_visual_item(root.contentItem(), "wideRecommendationRow1")
second_row = find_visual_item(root.contentItem(), "wideRecommendationRow2")
third_row = find_visual_item(root.contentItem(), "wideRecommendationRow3")
assert wide_row is not None and second_row is not None and third_row is not None
assert wide_pool.findChild(QObject, "poolCount") is not None
pool_count = wide_pool.findChild(QObject, "poolCount")
assert pool_count is not None
pool_state = provider.state["pool"]
assert pool_count.property("text") == (
    f'{pool_state["total_cards"]} / {pool_state["target_cards"]} cards'
)
pool_average = wide_pool.findChild(QObject, "poolManaCurveAverage")
assert pool_average is not None
assert pool_average.property("text") == "Average mana value: 2.43"
unavailable_pool_state = dict(provider.state)
unavailable_pool = dict(unavailable_pool_state["pool"])
unavailable_pool["average_mana_value"] = None
unavailable_pool_state["pool"] = unavailable_pool
provider._replace_state(state=unavailable_pool_state)
application.processEvents()
assert pool_average.property("text") == "Average mana value: —"
provider.selectScenario("warning")
application.processEvents()
restored_state = dict(provider.state)
restored_state["recommendations"] = recommendations_state
provider._replace_state(state=restored_state)
application.processEvents()
wide_row = find_visual_item(root.contentItem(), "wideRecommendationRow1")
second_row = find_visual_item(root.contentItem(), "wideRecommendationRow2")
third_row = find_visual_item(root.contentItem(), "wideRecommendationRow3")
assert wide_row is not None and second_row is not None and third_row is not None
pool_flickable = wide_pool.findChild(QObject, "poolSummaryFlickable")
pool_scrollbar = wide_pool.findChild(QObject, "poolSummaryScrollBar")
assert pool_flickable is not None and pool_flickable.property("activeFocusOnTab") is True
assert pool_scrollbar is not None and pool_scrollbar.isVisible()
accessible_pool = QAccessible.queryAccessibleInterface(pool_flickable)
assert accessible_pool is not None
assert "Page Up" in accessible_pool.text(QAccessible.Text.Description)
pool_flickable.forceActiveFocus()
assert pool_flickable.property("activeFocus") is True
max_pool_scroll = max(
    0.0,
    float(pool_flickable.property("contentHeight"))
    - float(pool_flickable.property("height")),
)
assert max_pool_scroll > 0
QTest.keyClick(root, Qt.Key_Down)
application.processEvents()
assert pool_flickable.property("contentY") > 0
QTest.keyClick(root, Qt.Key_Up)
application.processEvents()
assert pool_flickable.property("contentY") == 0
QTest.keyClick(root, Qt.Key_PageDown)
application.processEvents()
assert pool_flickable.property("contentY") > 0
QTest.keyClick(root, Qt.Key_PageUp)
application.processEvents()
assert pool_flickable.property("contentY") == 0
QTest.keyClick(root, Qt.Key_End)
application.processEvents()
assert abs(float(pool_flickable.property("contentY")) - max_pool_scroll) < 1
QTest.keyClick(root, Qt.Key_Home)
application.processEvents()
assert pool_flickable.property("contentY") == 0
name = wide_row.findChild(QObject, "recommendationName")
assert name is not None
assert name.property("text") == provider.state["recommendations"]["cards"][0]["card"]["name"]
assert name.property("truncated") is False
assert name.property("paintedWidth") <= name.property("width") + 1
assert name.property("paintedHeight") <= name.property("height") + 1
assert_visual_item_inside(wide_card, name)
metadata = wide_row.findChild(QObject, "recommendationMetadata")
assert metadata is not None
assert_visual_item_inside(wide_card, metadata)
first_card = provider.state["recommendations"]["cards"][0]
wide_metrics = {
    "recommendationColors": " · ".join(first_card["card"]["colors"]) or "Colorless",
    "recommendationScore": str(first_card["score"]),
    "recommendationWinRate": f'{first_card["win_rate"] * 100:.1f}%',
    "recommendationGrade": first_card["letter_grade"],
    "recommendationFit": first_card["color_fit"],
}
for metric_name, metric_text in wide_metrics.items():
    metric = wide_row.findChild(QObject, metric_name)
    assert metric is not None and metric.isVisible()
    assert metric.property("text") == metric_text
    assert_visual_item_inside(wide_row, metric)
assert name.property("text") == long_name
assert wide_row.height() >= 84
assert wide_row.width() <= wide_row.parentItem().width() + 1
live_view = root.findChild(QObject, "liveDraftView")
assert live_view is not None
wide_content_threshold = int(
    live_view.property("wideRecommendationsMinimumWidth")
)
assert wide_content_threshold == (
    wide_recommendation_list_width + wide_card_details_width + 12
)
window_threshold_width = root.width() + wide_content_threshold - live_view.width()
root.resize(window_threshold_width, 900)
application.processEvents()
assert live_view.property("wideRecommendations") is True
assert find_visual_item(root.contentItem(), "wideRecommendationRow1").isVisible()
root.resize(window_threshold_width - 1, 900)
application.processEvents()
assert live_view.property("wideRecommendations") is False
boundary_narrow_row = find_visual_item(
    root.contentItem(), "narrowRecommendationRow1"
)
assert boundary_narrow_row is not None and boundary_narrow_row.isVisible()
assert boundary_narrow_row.height() == 112
assert boundary_narrow_row.property("recommendation")["card"]["name"] == long_name
boundary_narrow_details = find_visual_item(
    boundary_narrow_row, "recommendationNarrowCardDetails"
)
assert boundary_narrow_details is not None and boundary_narrow_details.isVisible()
boundary_narrow_name = boundary_narrow_details.findChild(
    QObject, "recommendationName"
)
assert boundary_narrow_name is not None
assert boundary_narrow_name.property("text") == long_name
root.resize(1440, 900)
application.processEvents()
assert live_view.property("wideRecommendations") is True

root.resize(1440, 738)
application.processEvents()
assert live_view.property("wideRecommendations") is False
fallback_wide_preview = find_visual_item(
    root.contentItem(), "wideLiveCardPreview"
)
fallback_list = find_visual_item(
    root.contentItem(), "narrowRecommendationList"
)
fallback_tabs = find_visual_item(root.contentItem(), "liveDetailTabs")
fallback_preview = find_visual_item(
    root.contentItem(), "narrowLiveCardPreview"
)
fallback_banner = find_visual_item(
    root.contentItem(), "liveStateBanner"
)
assert fallback_wide_preview is not None
assert fallback_wide_preview.isVisible() is False
assert fallback_list is not None and fallback_list.isVisible()
assert fallback_tabs is not None and fallback_tabs.isVisible()
assert fallback_preview is not None and fallback_preview.isVisible()
fallback_stack = fallback_preview.parentItem()
assert fallback_banner is not None and fallback_banner.isVisible()
assert fallback_stack is not None and fallback_stack.isVisible()
assert_visual_item_inside(live_view, fallback_banner)
assert_visual_item_inside(live_view, fallback_stack)
assert_visual_item_inside(live_view, fallback_list)
assert_visual_item_inside(live_view, fallback_tabs)
assert_visual_item_inside(live_view, fallback_preview)
fallback_list_bottom = fallback_list.mapToItem(
    live_view, QPointF(0, fallback_list.height())
).y()
fallback_banner_bottom = fallback_banner.mapToItem(
    live_view, QPointF(0, fallback_banner.height())
).y()
fallback_list_top = fallback_list.mapToItem(
    live_view, QPointF(0, 0)
).y()

narrow_controls = find_visual_item(
    root.contentItem(), "narrowRecommendationControls"
)
narrow_on_color_filter = root.findChild(QObject, "narrowRecommendationOnColorFilter")
narrow_all_filter = root.findChild(QObject, "narrowRecommendationAllFilter")
assert narrow_controls is not None and narrow_controls.isVisible()
assert narrow_on_color_filter is not None and narrow_all_filter is not None
assert_visual_item_inside(live_view, narrow_controls)
assert_visual_item_inside(narrow_controls, narrow_on_color_filter)
assert_visual_item_inside(narrow_controls, narrow_all_filter)
assert narrow_all_filter.property("checked") is True
QTest.mouseClick(
    root,
    Qt.LeftButton,
    Qt.NoModifier,
    mouse_center(narrow_on_color_filter),
)
application.processEvents()
assert narrow_on_color_filter.property("checked") is True
assert recommendation_model.rowCount() == len(on_color_ids)
QTest.mouseClick(
    root,
    Qt.LeftButton,
    Qt.NoModifier,
    mouse_center(narrow_all_filter),
)
application.processEvents()
assert narrow_all_filter.property("checked") is True
assert recommendation_model.rowCount() == len(source_recommendation_ids)
fallback_tabs_top = fallback_tabs.mapToItem(
    live_view, QPointF(0, 0)
).y()
fallback_tabs_bottom = fallback_tabs.mapToItem(
    live_view, QPointF(0, fallback_tabs.height())
).y()
fallback_preview_top = fallback_preview.mapToItem(
    live_view, QPointF(0, 0)
).y()
fallback_controls_top = narrow_controls.mapToItem(
    live_view, QPointF(0, 0)
).y()
fallback_controls_bottom = narrow_controls.mapToItem(
    live_view, QPointF(0, narrow_controls.height())
).y()
assert fallback_list_bottom <= fallback_controls_top
assert fallback_controls_bottom <= fallback_preview_top
assert fallback_list_bottom <= fallback_tabs_top
assert fallback_tabs_bottom <= fallback_preview_top
assert fallback_banner_bottom <= fallback_list_top
fallback_frame = find_visual_item(
    fallback_preview, "cardPreviewImageFrame"
)
fallback_details = find_visual_item(
    fallback_preview, "cardPreviewDetails"
)
assert fallback_frame is not None and fallback_frame.isVisible()
assert fallback_details is not None and fallback_details.isVisible()
assert_visual_item_inside(fallback_preview, fallback_frame)
assert_visual_item_inside(fallback_preview, fallback_details)
fallback_frame_right = fallback_frame.mapToItem(
    fallback_preview, QPointF(fallback_frame.width(), 0)
).x()
fallback_details_left = fallback_details.mapToItem(
    fallback_preview, QPointF(0, 0)
).x()
assert fallback_frame_right <= fallback_details_left
root.resize(1440, 900)
application.processEvents()
assert live_view.property("wideRecommendations") is True
assert find_visual_item(
    root.contentItem(), "wideLiveCardPreview"
).isVisible()

root.resize(760, 900)
application.processEvents()
narrow_row = find_visual_item(root.contentItem(), "narrowRecommendationRow1")
narrow_second_row = find_visual_item(root.contentItem(), "narrowRecommendationRow2")
assert narrow_row is not None and narrow_row.isVisible()
assert narrow_second_row is not None and narrow_second_row.isVisible()

narrow_preview = find_visual_item(root.contentItem(), "narrowLiveCardPreview")
assert narrow_preview is not None and narrow_preview.isVisible()
narrow_frame = find_visual_item(narrow_preview, "cardPreviewImageFrame")
narrow_details = find_visual_item(narrow_preview, "cardPreviewDetails")
assert narrow_frame is not None and narrow_frame.isVisible()
assert narrow_details is not None and narrow_details.isVisible()
assert narrow_frame.width() >= 195
assert_visual_item_inside(narrow_preview, narrow_frame)
assert_visual_item_inside(narrow_preview, narrow_details)
narrow_frame_right = narrow_frame.mapToItem(
    narrow_preview, QPointF(narrow_frame.width(), 0)
).x()
narrow_details_left = narrow_details.mapToItem(
    narrow_preview, QPointF(0, 0)
).x()
assert narrow_frame_right <= narrow_details_left
narrow_thumbnail_frame = find_visual_item(
    narrow_row, "recommendationThumbnailFrame"
)
narrow_thumbnail_image = find_visual_item(
    narrow_row, "recommendationThumbnailImage"
)
narrow_thumbnail_fallback = find_visual_item(
    narrow_row, "recommendationThumbnailFallback"
)
assert narrow_thumbnail_frame is not None and narrow_thumbnail_frame.isVisible()
assert narrow_thumbnail_image is not None and narrow_thumbnail_fallback is not None
assert narrow_thumbnail_frame.width() == 50
assert narrow_thumbnail_frame.height() > 0
assert narrow_row.height() == 112
assert narrow_row.width() <= narrow_row.parentItem().width() + 1
narrow_thumbnail_top_left = narrow_thumbnail_frame.mapToItem(
    narrow_row, QPointF(0, 0)
)
narrow_thumbnail_bottom_right = narrow_thumbnail_frame.mapToItem(
    narrow_row,
    QPointF(narrow_thumbnail_frame.width(), narrow_thumbnail_frame.height()),
)
assert narrow_thumbnail_top_left.x() >= -1
assert narrow_thumbnail_top_left.y() >= -1
assert narrow_thumbnail_bottom_right.x() <= narrow_row.width() + 1
assert narrow_thumbnail_bottom_right.y() <= narrow_row.height() + 1
wait_until(
    lambda: narrow_thumbnail_image.isVisible(),
    "the visible narrow recommendation thumbnail",
)
assert image_source(narrow_thumbnail_image) == image_url
assert narrow_thumbnail_fallback.isVisible() is False

narrow_missing_fallback = find_visual_item(
    narrow_second_row, "recommendationThumbnailFallback"
)
narrow_missing_label = find_visual_item(
    narrow_second_row, "recommendationThumbnailFallbackLabel"
)
assert narrow_missing_fallback is not None and narrow_missing_fallback.isVisible()
assert narrow_missing_label is not None
assert narrow_missing_label.property("text") == "No image available"
narrow_recommendation_list = root.findChild(
    QObject, "narrowRecommendationList"
)
assert live_view.property("narrowRecommendationPreferredHeight") == 220
assert narrow_recommendation_list.height() >= 220
narrow_recommendation_list.setProperty("currentIndex", 2)
assert narrow_recommendation_list.property("currentIndex") == 2
assert QMetaObject.invokeMethod(narrow_recommendation_list, "positionViewAtEnd")
wait_until(
    lambda: float(narrow_recommendation_list.property("contentY")) > 0,
    "the narrow recommendation list to scroll to its end",
)
wait_until(
    lambda: find_visual_item(root.contentItem(), "narrowRecommendationRow3")
    is not None,
    "the third narrow recommendation row",
)
narrow_third_row = find_visual_item(root.contentItem(), "narrowRecommendationRow3")
assert narrow_third_row is not None and narrow_third_row.isVisible()
narrow_third_texts = visible_texts(narrow_third_row)
assert "17L —" in narrow_third_texts
assert "Grade —" in narrow_third_texts
assert "Open" in narrow_third_texts

provider.selectScenario("warning")
application.processEvents()
root.resize(680, 640)
application.processEvents()
assert live_view.property("wideRecommendations") is False
minimum_list = find_visual_item(
    root.contentItem(), "narrowRecommendationList"
)
minimum_preview = find_visual_item(root.contentItem(), "narrowLiveCardPreview")
minimum_controls = find_visual_item(
    root.contentItem(), "narrowRecommendationControls"
)
minimum_filter = find_visual_item(
    root.contentItem(), "narrowRecommendationFilter"
)
assert minimum_list is not None and minimum_list.isVisible()
minimum_banner = find_visual_item(
    root.contentItem(), "liveStateBanner"
)
minimum_tabs = find_visual_item(root.contentItem(), "liveDetailTabs")
assert minimum_banner is not None and minimum_banner.isVisible()
assert minimum_controls is not None and minimum_controls.isVisible()
assert minimum_tabs is not None and minimum_tabs.isVisible()
assert minimum_filter is not None and minimum_filter.isVisible()
assert_visual_item_inside(root.contentItem(), minimum_banner)
assert_visual_item_inside(root.contentItem(), minimum_list)
assert_visual_item_inside(root.contentItem(), minimum_controls)
assert_visual_item_inside(minimum_controls, minimum_filter)
assert_visual_item_inside(root.contentItem(), minimum_tabs)
assert minimum_list.height() < 120
assert minimum_preview is not None and minimum_preview.isVisible()
minimum_stack = minimum_preview.parentItem()
assert minimum_stack is not None
assert minimum_stack.height() >= 276
minimum_frame = find_visual_item(minimum_preview, "cardPreviewImageFrame")
minimum_details = find_visual_item(minimum_preview, "cardPreviewDetails")
assert minimum_frame is not None and minimum_frame.isVisible()
assert minimum_details is not None and minimum_details.isVisible()
assert_visual_item_inside(root.contentItem(), minimum_preview)
assert_visual_item_inside(minimum_preview, minimum_frame)
assert_visual_item_inside(minimum_preview, minimum_details)
minimum_frame_right = minimum_frame.mapToItem(
    minimum_preview, QPointF(minimum_frame.width(), 0)
).x()
minimum_details_left = minimum_details.mapToItem(
    minimum_preview, QPointF(0, 0)
).x()
assert minimum_frame_right <= minimum_details_left
minimum_list_bottom = minimum_list.mapToItem(
    root.contentItem(), QPointF(0, minimum_list.height())
).y()
minimum_preview_top = minimum_preview.mapToItem(
    root.contentItem(), QPointF(0, 0)
).y()
assert minimum_list_bottom <= minimum_preview_top
minimum_banner_bottom = minimum_banner.mapToItem(
    root.contentItem(), QPointF(0, minimum_banner.height())
).y()
minimum_list_top = minimum_list.mapToItem(
    root.contentItem(), QPointF(0, 0)
).y()
minimum_tabs_top = minimum_tabs.mapToItem(
    root.contentItem(), QPointF(0, 0)
).y()
minimum_tabs_bottom = minimum_tabs.mapToItem(
    root.contentItem(), QPointF(0, minimum_tabs.height())
).y()
minimum_controls_top = minimum_controls.mapToItem(
    root.contentItem(), QPointF(0, 0)
).y()
minimum_controls_bottom = minimum_controls.mapToItem(
    root.contentItem(), QPointF(0, minimum_controls.height())
).y()
assert minimum_list_bottom <= minimum_controls_top
assert minimum_controls_bottom <= minimum_preview_top
assert minimum_banner_bottom <= minimum_list_top
assert minimum_list_bottom <= minimum_tabs_top
assert minimum_tabs_bottom <= minimum_preview_top
assert_visual_item_inside(root.contentItem(), minimum_stack)
minimum_image = find_visual_item(minimum_preview, "cardPreviewImage")
assert minimum_image is not None
assert_visual_item_inside(minimum_frame, minimum_image)
minimum_name = find_visual_item(minimum_details, "cardPreviewName")
minimum_facts = find_visual_item(minimum_details, "cardPreviewFacts")
assert minimum_name is not None and minimum_facts is not None
for metadata in (minimum_name, minimum_facts):
    assert metadata.property("paintedWidth") <= metadata.width() + 1
    assert metadata.property("paintedHeight") <= metadata.height() + 1
    assert metadata.property("implicitHeight") <= metadata.height() + 1
root.resize(1440, 900)
application.processEvents()
provider.selectScenario("empty")
application.processEvents()
assert confidence.isVisible() is False
provider.selectScenario("ready")
application.processEvents()
assert confidence.isVisible() is False
assert len(provider.state["pool"]["mana_curve"]) == 7
assert pool_state["color_distribution"]
provider.selectScenario("warning")
application.processEvents()
download = root.findChild(QObject, "ratingsDownloadButton")
assert download is not None
assert_modal_cycle(
    root,
    download,
    "ratingsDownloadDialog",
    "ratingsDownloadCancelButton",
    "ratingsDownloadConfirmButton",
)

root.setProperty("currentSurface", "build")
application.processEvents()
pair_selector = root.findChild(QObject, "buildPairSelector")
assert pair_selector is not None
assert_surface_round_trip(root, pair_selector)

root.setProperty("currentSurface", "backtest")
application.processEvents()
run_backtest = root.findChild(QObject, "backtestRunButton")
assert run_backtest is not None
assert_surface_round_trip(root, run_backtest)

root.setProperty("currentSurface", "settings")
application.processEvents()
preview_switch = root.findChild(QObject, "settingsCardPreviewSwitch")
assert preview_switch is not None
assert_surface_round_trip(root, preview_switch)
settings_download = root.findChild(QObject, "settingsRatingsDownloadButton")
assert settings_download is not None
assert_modal_cycle(
    root,
    settings_download,
    "settingsRatingsDownloadDialog",
    "settingsRatingsDownloadCancelButton",
    "settingsRatingsDownloadConfirmButton",
)
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr


def test_qml_about_dialog_is_accessible_and_preserves_provider_state_offscreen() -> None:
    probe = """
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QUrl, Qt, Slot
from PySide6.QtGui import QAccessible, QDesktopServices, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.qt_mock import MockSessionAdapter


class UrlHandler(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    @Slot(QUrl)
    def openUrl(self, url: QUrl) -> None:
        self.urls.append(url.toString())


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
provider = MockSessionAdapter(session=MockLiveSession(scenario="warning"))
preferences_dir = TemporaryDirectory()
preferences = GuiPreferencesAdapter(app_dir=preferences_dir.name)
engine = QQmlApplicationEngine()
qml_directory = Path.cwd() / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "live")
context.setContextProperty("initialWindowWidth", 680)
context.setContextProperty("initialWindowHeight", 640)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
assert engine.rootObjects()
root = engine.rootObjects()[0]
root.resize(680, 640)
application.processEvents()

about_link = root.findChild(QObject, "aboutLink")
status_strip = root.findChild(QObject, "statusStrip")
assert about_link is not None and status_strip is not None
assert about_link.isVisible()
assert about_link.property("activeFocusOnTab") is True
accessible_about_link = QAccessible.queryAccessibleInterface(about_link)
assert accessible_about_link is not None
assert accessible_about_link.text(QAccessible.Text.Name) == "Open About dialog"
assert root.property("narrow") is True
assert about_link.width() > 0
assert status_strip.width() <= root.width()
before_state = provider.state.copy()

url_handler = UrlHandler()
QDesktopServices.setUrlHandler("https", url_handler, "openUrl")
try:
    about_link.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()

    dialog = root.findChild(QObject, "aboutDialog")
    title = root.findChild(QObject, "aboutDialogTitle")
    logo = root.findChild(QObject, "aboutDialogLogo")
    version = root.findChild(QObject, "aboutDialogVersion")
    author = root.findChild(QObject, "aboutDialogAuthor")
    license_label = root.findChild(QObject, "aboutDialogLicense")
    website = root.findChild(QObject, "aboutDialogWebsite")
    repository = root.findChild(QObject, "aboutDialogRepository")
    close = root.findChild(QObject, "aboutDialogCloseButton")
    assert dialog is not None and dialog.property("visible") is True
    assert dialog.property("modal") is True
    window_width = float(root.property("width"))
    window_height = float(root.property("height"))
    dialog_x = float(dialog.property("x"))
    dialog_y = float(dialog.property("y"))
    dialog_width = float(dialog.property("width"))
    dialog_height = float(dialog.property("height"))
    dialog_margin = 16.0
    assert dialog_width > 0 and dialog_height > 0
    assert dialog_width <= window_width - 2 * dialog_margin
    assert dialog_height <= window_height - 2 * dialog_margin
    assert dialog_margin <= dialog_x <= window_width - dialog_width - dialog_margin
    assert dialog_margin <= dialog_y <= window_height - dialog_height - dialog_margin
    assert abs(dialog_x - (window_width - dialog_width) / 2) <= 1.0
    assert abs(dialog_y - (window_height - dialog_height) / 2) <= 1.0
    assert title is not None and title.property("text") == "Draft Omen"
    assert logo is not None and logo.isVisible() and logo.width() >= 180
    assert version is not None and version.property("text") == "Version " + __version__
    assert author is not None and author.property("text") == "Made with ❤️ by Andrea Grandi"
    assert license_label is not None
    assert license_label.property("text") == "Draft Omen is licensed under the MIT License"
    assert website is not None and website.isVisible()
    assert website.property("text") == "Project website"
    assert repository is not None and repository.isVisible()
    assert repository.property("text") == "GitHub repository"
    assert close is not None and close.property("text") == "Close"
    assert dialog.property("projectWebsite") == "https://www.draftomen.com"
    assert dialog.property("projectRepository") == "https://github.com/andreagrandi/draftomen"
    assert website.property("activeFocusOnTab") is True
    assert repository.property("activeFocusOnTab") is True
    accessible_author = QAccessible.queryAccessibleInterface(author)
    assert accessible_author is not None
    assert accessible_author.text(QAccessible.Text.Name) == "Made with ❤️ by Andrea Grandi"
    accessible_license = QAccessible.queryAccessibleInterface(license_label)
    assert accessible_license is not None
    assert accessible_license.text(QAccessible.Text.Name) == (
        "Draft Omen is licensed under the MIT License"
    )
    accessible_website = QAccessible.queryAccessibleInterface(website)
    assert accessible_website is not None
    assert accessible_website.text(QAccessible.Text.Name) == "Open Draft Omen project website"
    assert accessible_website.text(QAccessible.Text.Description) == "https://www.draftomen.com"
    accessible_repository = QAccessible.queryAccessibleInterface(repository)
    assert accessible_repository is not None
    assert accessible_repository.text(QAccessible.Text.Name) == "Open Draft Omen GitHub repository"
    assert accessible_repository.text(QAccessible.Text.Description) == (
        "https://github.com/andreagrandi/draftomen"
    )
    logo_source = logo.property("source")
    assert isinstance(logo_source, QUrl)
    logo_context = QQmlEngine.contextForObject(logo)
    assert logo_context is not None
    resolved_logo_source = logo_context.baseUrl().resolved(logo_source)
    assert resolved_logo_source.path().endswith("/assets/draftomen_logo.png")
    if resolved_logo_source.isLocalFile():
        assert Path(resolved_logo_source.toLocalFile()).resolve() == (
            Path.cwd() / "draftomen" / "assets" / "draftomen_logo.png"
        ).resolve()
    assert provider.state == before_state

    website.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert url_handler.urls == ["https://www.draftomen.com"]
    assert provider.state == before_state

    repository.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert url_handler.urls == [
        "https://www.draftomen.com",
        "https://github.com/andreagrandi/draftomen",
    ]
    assert provider.state == before_state

    close.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert dialog.property("visible") is False
    assert about_link.property("activeFocus") is True
    assert provider.state == before_state

    about_link.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert dialog.property("visible") is True
    QTest.keyClick(root, Qt.Key_Escape)
    application.processEvents()
    assert dialog.property("visible") is False
    assert about_link.property("activeFocus") is True
    assert provider.state == before_state
finally:
    QDesktopServices.unsetUrlHandler("https")
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr


def test_qml_privacy_dialog_is_accessible_modal_and_preserves_provider_state_offscreen() -> None:
    probe = """
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QPoint, QPointF, QUrl, Qt
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.qt_mock import MockSessionAdapter


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
provider = MockSessionAdapter(session=MockLiveSession(scenario="warning"))
preferences_dir = TemporaryDirectory()
preferences = GuiPreferencesAdapter(app_dir=preferences_dir.name)
engine = QQmlApplicationEngine()
qml_directory = Path.cwd() / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("initialSurface", "live")
context.setContextProperty("initialWindowWidth", 680)
context.setContextProperty("initialWindowHeight", 640)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
assert engine.rootObjects()
root = engine.rootObjects()[0]
root.resize(680, 640)
application.processEvents()

privacy_link = root.findChild(QObject, "privacyLink")
about_link = root.findChild(QObject, "aboutLink")
navigation_rail = root.findChild(QObject, "navigationRail")
assert privacy_link is not None and about_link is not None and navigation_rail is not None
assert privacy_link.property("text") == "Privacy"
assert privacy_link.isVisible()
assert privacy_link.property("activeFocusOnTab") is True
accessible_privacy_link = QAccessible.queryAccessibleInterface(privacy_link)
assert accessible_privacy_link is not None
assert accessible_privacy_link.text(QAccessible.Text.Name) == "Open Privacy dialog"
assert root.property("narrow") is True
assert privacy_link.width() > 0
assert navigation_rail.width() <= root.width()

window_width = float(root.property("width"))
window_height = float(root.property("height"))
privacy_top_left = privacy_link.mapToScene(QPointF(0, 0))
privacy_bottom_right = privacy_link.mapToScene(
    QPointF(privacy_link.width(), privacy_link.height())
)
navigation_top_left = navigation_rail.mapToScene(QPointF(0, 0))
navigation_bottom_right = navigation_rail.mapToScene(
    QPointF(navigation_rail.width(), navigation_rail.height())
)
assert privacy_top_left.x() >= navigation_top_left.x()
assert privacy_top_left.y() >= navigation_top_left.y()
assert privacy_bottom_right.x() <= navigation_bottom_right.x()
assert privacy_bottom_right.y() <= navigation_bottom_right.y()
assert privacy_top_left.x() >= 0
assert privacy_top_left.y() >= 0
assert privacy_bottom_right.x() <= window_width
assert privacy_bottom_right.y() <= window_height

before_state = provider.state.copy()
initial_surface = root.property("currentSurface")

def tab_to(item: QObject, *, maximum: int = 40) -> None:
    for _ in range(maximum):
        QTest.keyClick(root, Qt.Key_Tab)
        application.processEvents()
        if item.property("activeFocus") is True:
            return
    raise AssertionError("keyboard traversal did not reach " + item.objectName())


tab_to(privacy_link)
assert privacy_link.property("activeFocus") is True
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()

dialog = root.findChild(QObject, "privacyDialog")
title = root.findChild(QObject, "privacyDialogTitle")
disclosure = root.findChild(QObject, "privacyDialogDisclosure")
close = root.findChild(QObject, "privacyDialogCloseButton")
about_dialog = root.findChild(QObject, "aboutDialog")
assert dialog is not None and dialog.property("visible") is True
assert dialog.property("modal") is True
assert root.property("currentSurface") == initial_surface
assert dialog.property("title") == "Privacy"
assert title is not None and title.property("text") == "Privacy"
assert disclosure is not None
assert disclosure.property("text") == (
    "All user data remains on the user's computer. "
    "Draft Omen does not send your personal data to us."
)
accessible_disclosure = QAccessible.queryAccessibleInterface(disclosure)
assert accessible_disclosure is not None
assert accessible_disclosure.text(QAccessible.Text.Name) == disclosure.property("text")
assert close is not None and close.property("text") == "Close"
assert close.property("activeFocusOnTab") is True
accessible_close = QAccessible.queryAccessibleInterface(close)
assert accessible_close is not None
assert accessible_close.text(QAccessible.Text.Name) == "Close Privacy dialog"

dialog_x = float(dialog.property("x"))
dialog_y = float(dialog.property("y"))
dialog_width = float(dialog.property("width"))
dialog_height = float(dialog.property("height"))
dialog_margin = 16.0
assert dialog_width > 0 and dialog_height > 0
assert dialog_width <= window_width - 2 * dialog_margin
assert dialog_height <= window_height - 2 * dialog_margin
assert dialog_margin <= dialog_x <= window_width - dialog_width - dialog_margin
assert dialog_margin <= dialog_y <= window_height - dialog_height - dialog_margin
assert abs(dialog_x - (window_width - dialog_width) / 2) <= 1.0
assert abs(dialog_y - (window_height - dialog_height) / 2) <= 1.0
assert provider.state == before_state

tab_to(close, maximum=4)
assert close.property("activeFocus") is True
QTest.keyClick(root, Qt.Key_Tab)
application.processEvents()
assert dialog.property("visible") is True
assert close.property("activeFocus") is True
assert privacy_link.property("activeFocus") is False
assert about_link.property("activeFocus") is False
QTest.keyClick(root, Qt.Key_Tab, Qt.ShiftModifier)
application.processEvents()
assert dialog.property("visible") is True
assert close.property("activeFocus") is True
assert privacy_link.property("activeFocus") is False
assert about_link.property("activeFocus") is False
assert root.property("currentSurface") == initial_surface

about_position = about_link.mapToItem(
    root.contentItem(), QPointF(about_link.width() / 2, about_link.height() / 2)
)
QTest.mouseClick(
    root,
    Qt.LeftButton,
    Qt.NoModifier,
    QPoint(round(about_position.x()), round(about_position.y())),
)
application.processEvents()
assert dialog.property("visible") is True
assert about_dialog is not None and about_dialog.property("visible") is False
assert root.property("currentSurface") == initial_surface
assert provider.state == before_state

QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert dialog.property("visible") is False
assert privacy_link.property("activeFocus") is True
assert root.property("currentSurface") == initial_surface
assert provider.state == before_state

tab_to(privacy_link)
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert dialog.property("visible") is True
assert root.property("currentSurface") == initial_surface
QTest.keyClick(root, Qt.Key_Escape)
application.processEvents()
assert dialog.property("visible") is False
assert privacy_link.property("activeFocus") is True
assert root.property("currentSurface") == initial_surface
assert provider.state == before_state
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr


def test_qml_preferences_build_backtest_and_responsive_states_offscreen() -> None:
    probe = """
from dataclasses import replace
from tempfile import TemporaryDirectory
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, Qt, QUrl
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen import __version__
from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_gui import _fixed_font_family
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import FocusBuildCard, RequestBacktest, RequestBuild

def find_visual_item(item: QQuickItem, object_name: str) -> QQuickItem | None:
    if item.objectName() == object_name:
        return item
    for child in item.childItems():
        found = find_visual_item(child, object_name)
        if found is not None:
            return found
    return None


def assert_visible_active_focus(item: QQuickItem) -> None:
    assert item.property("activeFocus") is True
    assert item.isVisible()
    top_left = item.mapToScene(QPointF(0, 0))
    bottom_right = item.mapToScene(QPointF(item.width(), item.height()))
    assert top_left.y() >= 0
    assert bottom_right.y() <= root.height()


def assert_visual_item_inside(parent: QQuickItem, child: QQuickItem) -> None:
    parent_top_left = parent.mapToScene(QPointF(0, 0))
    parent_bottom_right = parent.mapToScene(QPointF(parent.width(), parent.height()))
    child_top_left = child.mapToScene(QPointF(0, 0))
    child_bottom_right = child.mapToScene(QPointF(child.width(), child.height()))
    assert child_top_left.x() >= parent_top_left.x()
    assert child_top_left.y() >= parent_top_left.y()
    assert child_bottom_right.x() <= parent_bottom_right.x()
    assert child_bottom_right.y() <= parent_bottom_right.y()


def assert_visual_item_precedes(first: QQuickItem, second: QQuickItem) -> None:
    first_bottom = first.mapToScene(QPointF(0, first.height())).y()
    second_top = second.mapToScene(QPointF(0, 0)).y()
    assert first_bottom <= second_top


def scene_geometry(item: QQuickItem) -> tuple[float, float, float, float]:
    top_left = item.mapToScene(QPointF(0, 0))
    return (top_left.x(), top_left.y(), item.width(), item.height())


def assert_geometry_close(
    item: QQuickItem,
    expected: tuple[float, float, float, float],
) -> None:
    actual = scene_geometry(item)
    assert all(
        abs(value - expected_value) <= 0.1
        for value, expected_value in zip(actual, expected)
    )


def trigger_and_wait_for_layout(item, action, predicate) -> None:
    height_change_spy = QSignalSpy(item.heightChanged)
    action()
    while not predicate(item.height()):
        assert height_change_spy.wait(1000)
    application.processEvents()


def wait_for_saved(preferences: GuiPreferencesAdapter) -> None:
    for _ in range(100):
        application.processEvents()
        if preferences.persistenceMessage == "Saved":
            return
        QTest.qWait(10)
    assert preferences.persistenceMessage == "Saved"


class RecordingProvider(MockSessionAdapter):
    def __init__(self):
        self.commands = []
        super().__init__(session=MockLiveSession(scenario="ready"))

    def _dispatch(self, *, command):
        self.commands.append(command)
        super()._dispatch(command=command)


preferences_dir = TemporaryDirectory()
QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
provider = RecordingProvider()
preferences = GuiPreferencesAdapter(app_dir=preferences_dir.name)
preferences.setCardPreview(False)
engine = QQmlApplicationEngine()
qml_directory = Path.cwd() / "draftomen" / "qml"
engine.addImportPath(str(qml_directory))
context = engine.rootContext()
context.setContextProperty("fixedFontFamily", _fixed_font_family())
context.setContextProperty("sessionProvider", provider)
context.setContextProperty("guiPreferences", preferences)
context.setContextProperty("applicationTitle", "Draft Omen")
context.setContextProperty("applicationVersion", __version__)
context.setContextProperty("initialSurface", "settings")
context.setContextProperty("initialWindowWidth", 1440)
context.setContextProperty("initialWindowHeight", 900)
engine.setInitialProperties({"provider": provider})
engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
root = engine.rootObjects()[0]
application.processEvents()
assert root.property("desktopApplicationVersion") == __version__
version_label = root.findChild(QObject, "applicationVersionLabel")
assert version_label is not None and version_label.property("text") == "v" + __version__
logo = find_visual_item(root.contentItem(), "draftomenLogo")
app_bar_title = root.findChild(QObject, "appBarBrandTitle")
assert logo is not None and logo.isVisible()
assert app_bar_title is not None and app_bar_title.isVisible() is False
accessible_logo = QAccessible.queryAccessibleInterface(logo)
assert accessible_logo is not None
assert accessible_logo.text(QAccessible.Text.Name) == "Draft Omen logo"

preview_switch = root.findChild(QObject, "settingsCardPreviewSwitch")
assert preview_switch is not None and preview_switch.property("checked") is False
preview_switch.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert preferences.cardPreview is True
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert preferences.cardPreview is False
wait_for_saved(preferences)
assert GuiPreferencesAdapter(app_dir=preferences_dir.name).cardPreview is False

root.resize(1080, 900)
root.setProperty("currentSurface", "live")
application.processEvents()
responsive_live_view = root.findChild(QObject, "liveDraftView")
assert responsive_live_view is not None
assert responsive_live_view.property("wideRecommendations") is False
responsive_narrow_row = find_visual_item(
    root.contentItem(), "narrowRecommendationRow1"
)
responsive_narrow_preview = find_visual_item(
    root.contentItem(), "narrowLiveCardPreview"
)
assert responsive_narrow_row is not None and responsive_narrow_row.isVisible()
assert responsive_narrow_row.height() == 112
assert responsive_narrow_preview is not None and responsive_narrow_preview.isVisible()
assert responsive_narrow_row.property("recommendation")["card"]["name"] == (
    provider.state["recommendations"]["cards"][0]["card"]["name"]
)

root.resize(760, 900)
root.setProperty("currentSurface", "live")
application.processEvents()
live_tabs = root.findChild(QObject, "liveDetailTabs")
narrow_controls = find_visual_item(
    root.contentItem(), "narrowRecommendationControls"
)
narrow_filter = find_visual_item(
    root.contentItem(), "narrowRecommendationFilter"
)
narrow_live_preview = find_visual_item(root.contentItem(), "narrowLiveCardPreview")
narrow_live_pool = find_visual_item(root.contentItem(), "narrowLivePoolDetails")
narrow_row = find_visual_item(root.contentItem(), "narrowRecommendationRow1")
assert live_tabs is not None and live_tabs.property("currentIndex") == 0
assert narrow_controls is not None and narrow_controls.isVisible()
assert narrow_filter is not None and narrow_filter.isVisible()
assert narrow_live_preview is not None and narrow_live_preview.isVisible()
assert narrow_live_pool is not None and narrow_live_pool.isVisible() is False
assert narrow_row is not None and narrow_row.height() >= 100
comparison = root.findChild(QObject, "recommendationComparisonSummary")
assert comparison is not None
state_before_markup = dict(provider.state)
recommendations_with_markup = dict(state_before_markup["recommendations"])
markup_summary = (
    "DO recommendation: <b>Alpha & Beta</b> ranks ahead of <i>Gamma</i> "
    "because this deliberately long plain-text comparison wraps without markup."
)
recommendations_with_markup["comparison_summary"] = markup_summary
state_with_markup = dict(state_before_markup)
state_with_markup["recommendations"] = recommendations_with_markup
provider._replace_state(state=state_with_markup)
application.processEvents()
assert comparison.isVisible()
assert comparison.property("text") == markup_summary
assert comparison.width() > 0
assert float(comparison.property("paintedHeight")) <= comparison.height() + 1
provider._replace_state(state=state_before_markup)
application.processEvents()

assert narrow_row.width() == narrow_controls.width()
assert narrow_live_preview.width() == narrow_controls.width()
assert_visual_item_inside(narrow_controls, narrow_filter)
assert_visual_item_inside(narrow_controls, live_tabs)
narrow_filter_right = narrow_filter.mapToItem(
    narrow_controls, QPointF(narrow_filter.width(), 0)
).x()
narrow_tabs_left = live_tabs.mapToItem(narrow_controls, QPointF(0, 0)).x()
narrow_tabs_right = live_tabs.mapToItem(
    narrow_controls, QPointF(live_tabs.width(), 0)
).x()
assert narrow_filter_right <= narrow_tabs_left
assert narrow_tabs_right <= narrow_controls.width()
narrow_frame = find_visual_item(narrow_live_preview, "cardPreviewImageFrame")
assert narrow_frame is not None and narrow_frame.isVisible()
assert narrow_frame.width() >= 195
assert_visual_item_inside(narrow_live_preview, narrow_frame)
assert narrow_row.width() > root.width() / 2
assert logo.isVisible() is False
assert app_bar_title.isVisible()

live_tabs.setProperty("currentIndex", 1)
application.processEvents()
assert narrow_live_preview.isVisible() is False
assert narrow_live_pool.isVisible()

live_tabs.setProperty("currentIndex", 0)
application.processEvents()
assert narrow_live_preview.isVisible()
preferences.setCardPreview(True)
application.processEvents()

root.setProperty("currentSurface", "build")
application.processEvents()
build_view = root.findChild(QObject, "buildView")
narrow_type_summary = find_visual_item(root.contentItem(), "narrowBuildTypeSummary")
assert narrow_type_summary is not None and narrow_type_summary.isVisible()
assert narrow_type_summary.property("text") == "7 creatures · 1 instant"
changed_state = dict(provider.state)
changed_build = dict(changed_state["build"])
changed_build["creature_count"] = 5
changed_build["instant_count"] = 2
changed_state["build"] = changed_build
provider._replace_state(state=changed_state)
application.processEvents()
assert narrow_type_summary.property("text") == "5 creatures · 2 instants"
provider.selectScenario("ready")
application.processEvents()
assert narrow_type_summary.property("text") == "7 creatures · 1 instant"
assert build_view is not None
initial_focus_key = build_view.property("publishedBuildFocusKey")
assert initial_focus_key
focus_command_count = sum(
    isinstance(command, FocusBuildCard) for command in provider.commands
)
provider.selectScenario("empty")
application.processEvents()
assert build_view.property("publishedBuildFocusKey") == ""
provider.selectScenario("ready")
application.processEvents()
assert build_view.property("publishedBuildFocusKey") == initial_focus_key
assert sum(
    isinstance(command, FocusBuildCard) for command in provider.commands
) == focus_command_count + 1

narrow_curve = find_visual_item(root.contentItem(), "narrowBuildManaCurve")
assert narrow_curve is not None and narrow_curve.isVisible()
narrow_average = find_visual_item(narrow_curve, "manaCurveAverage")
assert narrow_average is not None and narrow_average.isVisible()
assert narrow_average.width() >= narrow_average.property("implicitWidth")
formatted_state = dict(provider.state)
formatted_build = dict(formatted_state["build"])
formatted_build["average_mana_value"] = 2.956
formatted_state["build"] = formatted_build
provider._replace_state(state=formatted_state)
application.processEvents()
assert narrow_average.property("text") == "Average mana value: 2.96"
unavailable_state = dict(provider.state)
unavailable_build = dict(unavailable_state["build"])
unavailable_build["average_mana_value"] = None
unavailable_state["build"] = unavailable_build
provider._replace_state(state=unavailable_state)
application.processEvents()
assert narrow_average.property("text") == "Average mana value: —"
provider.selectScenario("ready")
application.processEvents()
assert narrow_average.property("text") == "Average mana value: 2.70"

pair_selector = root.findChild(QObject, "buildPairSelector")
rebuild = root.findChild(QObject, "buildRebuildButton")
assert pair_selector is not None and rebuild is not None
pair_selector.setProperty("currentIndex", 1)
rebuild.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert any(isinstance(command, RequestBuild) and command.pair_override == "BG" for command in provider.commands)
assert provider.state["build"]["selected_pair"] == "BG"
assert narrow_type_summary.property("text") == "7 creatures · 1 instant"
spell_button = find_visual_item(root.contentItem(), "buildSpellButton1")
assert build_view is not None and spell_button is not None
spell_button.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
QTest.qWait(20)
card_details_toggle = find_visual_item(root.contentItem(), "buildCardDetailsToggle")
assert card_details_toggle is not None
assert_visible_active_focus(card_details_toggle)
assert build_view.property("focusedCard")["card"]["name"] == provider.state["build"]["spells"][1]["card"]["name"]
assert spell_button.property("selected") is True
accessible_spell = QAccessible.queryAccessibleInterface(spell_button)
assert accessible_spell is not None
assert accessible_spell.object() is spell_button
assert spell_button.property("accessibilitySelectable") is True
assert spell_button.property("accessibilitySelected") is True
narrow_preview = find_visual_item(root.contentItem(), "narrowBuildCardPreview")
assert narrow_preview is not None and narrow_preview.isVisible()
assert narrow_preview.property("recommendation")["card"]["name"] == provider.state["build"]["spells"][1]["card"]["name"]
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert_visible_active_focus(card_details_toggle)
bench_toggle = root.findChild(QObject, "buildBenchToggle")
assert bench_toggle is not None
assert bench_toggle.property("text").endswith("bench · 2")
bench_toggle.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
bench_button = find_visual_item(root.contentItem(), "buildBenchButton0")
assert bench_button is not None
bench_button.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert narrow_preview.property("recommendation")["card"]["name"] == provider.state["build"]["bench"][0]["card"]["name"]
assert any(
    isinstance(command, FocusBuildCard)
    and command.grp_id == provider.state["build"]["bench"][0]["card"]["grp_id"]
    for command in provider.commands
)
assert narrow_preview.property("imageState")["grp_id"] == provider.state["build"]["bench"][0]["card"]["grp_id"]
QTest.qWait(20)
assert_visible_active_focus(card_details_toggle)

build_view.setProperty("contextExpanded", False)
root.resize(1059, 900)
application.processEvents()
assert root.property("narrow") is False
assert build_view.property("compactPresentation") is True
assert build_view.property("contextExpanded") is False
assert pair_selector.isVisible()
assert rebuild.isVisible()
compact_spell = find_visual_item(root.contentItem(), "buildSpellButton1")
assert compact_spell is not None and compact_spell.isVisible()
narrow_context = find_visual_item(root.contentItem(), "narrowBuildContext")
narrow_context_toggle = find_visual_item(root.contentItem(), "narrowBuildContextToggle")
narrow_context_details = find_visual_item(root.contentItem(), "narrowBuildContextDetails")
assert narrow_context is not None and narrow_context.isVisible()
assert narrow_context_toggle is not None and narrow_context_toggle.isVisible()
assert narrow_context_details is not None and narrow_context_details.isVisible() is False
assert narrow_context_toggle.property("text") == "Show why this pair"
collapsed_narrow_context_height = narrow_context.height()
accessible_narrow_context = QAccessible.queryAccessibleInterface(narrow_context_toggle)
assert accessible_narrow_context is not None
assert accessible_narrow_context.text(QAccessible.Text.Name) == "Show why this pair"
assert accessible_narrow_context.text(QAccessible.Text.Description) == (
    "The pair rationale is currently collapsed. Activating this button expands it."
)
narrow_context_toggle.forceActiveFocus()
trigger_and_wait_for_layout(
    narrow_context,
    lambda: QTest.keyClick(root, Qt.Key_Space),
    lambda height: height > collapsed_narrow_context_height,
)
assert build_view.property("contextExpanded") is True
assert narrow_context_toggle.property("text") == "Hide why this pair"
assert accessible_narrow_context.text(QAccessible.Text.Name) == "Hide why this pair"
assert accessible_narrow_context.text(QAccessible.Text.Description) == (
    "The pair rationale is currently expanded. Activating this button collapses it."
)
assert narrow_context_details.isVisible()
narrow_reason = find_visual_item(root.contentItem(), "narrowPairOptionWG")
assert narrow_reason is not None and narrow_reason.isVisible()
assert narrow_context.height() > collapsed_narrow_context_height
trigger_and_wait_for_layout(
    narrow_context,
    lambda: QTest.keyClick(root, Qt.Key_Space),
    lambda height: height == collapsed_narrow_context_height,
)
assert build_view.property("contextExpanded") is False
assert narrow_context_toggle.property("text") == "Show why this pair"
assert narrow_context_details.isVisible() is False
assert narrow_reason.isVisible() is False
assert narrow_context.height() == collapsed_narrow_context_height

root.resize(1060, 900)
application.processEvents()
assert build_view.property("compactPresentation") is False
wide_creature_count = find_visual_item(root.contentItem(), "wideBuildCreatureCount")
wide_instant_count = find_visual_item(root.contentItem(), "wideBuildInstantCount")
assert wide_creature_count is not None and wide_creature_count.isVisible()
assert wide_instant_count is not None and wide_instant_count.isVisible()
assert wide_creature_count.property("text") == "7"
assert wide_instant_count.property("text") == "1"
assert pair_selector.isVisible()
assert rebuild.isVisible()

wide_curve = find_visual_item(root.contentItem(), "wideBuildManaCurve")
assert wide_curve is not None and wide_curve.isVisible()
wide_mana_base = find_visual_item(root.contentItem(), "wideBuildManaBase")
wide_warnings = find_visual_item(root.contentItem(), "wideBuildWarnings")
assert wide_mana_base is not None and wide_mana_base.isVisible()
assert wide_warnings is not None and wide_warnings.isVisible()
assert_visual_item_precedes(wide_mana_base, wide_curve)
assert_visual_item_precedes(wide_curve, wide_warnings)
wide_title = find_visual_item(wide_curve, "manaCurveTitle")
assert wide_title is not None and wide_title.isVisible()
assert wide_title.width() <= wide_curve.width()
assert wide_title.width() >= wide_title.property("implicitWidth")
assert_visual_item_inside(wide_curve, wide_title)
wide_average = find_visual_item(wide_curve, "manaCurveAverage")
assert wide_average is not None and wide_average.isVisible()
assert wide_average.property("text") == "Average mana value: 2.70"
assert wide_average.width() <= wide_curve.width()
assert wide_average.width() >= wide_average.property("implicitWidth")
assert_visual_item_inside(wide_curve, wide_average)
assert_visual_item_precedes(wide_title, wide_average)
wide_spells = find_visual_item(root.contentItem(), "buildSpellGroups")
assert wide_spells is not None and wide_spells.isVisible()
assert wide_spells.x() >= 0
assert wide_spells.x() + wide_spells.width() <= build_view.width()
wide_context = find_visual_item(root.contentItem(), "wideBuildContext")
wide_context_toggle = find_visual_item(root.contentItem(), "wideBuildContextToggle")
wide_context_details = find_visual_item(root.contentItem(), "wideBuildContextDetails")
assert wide_context is not None and wide_context.isVisible()
assert wide_context_toggle is not None and wide_context_toggle.isVisible()
assert wide_context_details is not None and wide_context_details.isVisible() is False
assert wide_context_toggle.property("text") == "Show why this pair"
collapsed_wide_context_height = wide_context.height()
accessible_wide_context = QAccessible.queryAccessibleInterface(wide_context_toggle)
assert accessible_wide_context is not None
assert accessible_wide_context.text(QAccessible.Text.Name) == "Show why this pair"
assert accessible_wide_context.text(QAccessible.Text.Description) == (
    "The pair rationale is currently collapsed. Activating this button expands it."
)
wide_context_toggle.forceActiveFocus()
trigger_and_wait_for_layout(
    wide_context,
    lambda: QTest.keyClick(root, Qt.Key_Space),
    lambda height: height > collapsed_wide_context_height,
)
assert build_view.property("contextExpanded") is True
assert wide_context_toggle.property("text") == "Hide why this pair"
assert accessible_wide_context.text(QAccessible.Text.Name) == "Hide why this pair"
assert accessible_wide_context.text(QAccessible.Text.Description) == (
    "The pair rationale is currently expanded. Activating this button collapses it."
)
assert wide_context_details.isVisible()
wide_reason = find_visual_item(root.contentItem(), "widePairOptionWG")
assert wide_reason is not None and wide_reason.isVisible()
assert "score 82.4" in wide_reason.property("text")
assert "25 playables" in wide_reason.property("text")
assert wide_context.height() > collapsed_wide_context_height
trigger_and_wait_for_layout(
    wide_context,
    lambda: QTest.keyClick(root, Qt.Key_Space),
    lambda height: height == collapsed_wide_context_height,
)
assert build_view.property("contextExpanded") is False
assert wide_context_toggle.property("text") == "Show why this pair"
assert wide_context_details.isVisible() is False
assert wide_reason.isVisible() is False
assert wide_context.height() == collapsed_wide_context_height

current_snapshot = provider._session.snapshot
build = current_snapshot.build
assert build is not None
second_bench = build.spells[0]
assert build.bench[0].card.grp_id != second_bench.card.grp_id
updated_snapshot = replace(
    current_snapshot,
    build=replace(
        build,
        bench=(*build.bench, second_bench),
    ),
)
provider._session._snapshot = updated_snapshot
provider._publish(snapshot=updated_snapshot)
application.processEvents()
build_view.setProperty("benchExpanded", False)
application.processEvents()
wide_bench_toggle = find_visual_item(root.contentItem(), "buildBenchToggle")
assert wide_bench_toggle is not None and wide_bench_toggle.isVisible()
assert_visual_item_inside(build_view, wide_bench_toggle)
assert_visual_item_inside(root.contentItem(), wide_bench_toggle)
assert wide_bench_toggle.property("text").endswith("bench · 3")
wide_bench_toggle.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert_visual_item_inside(build_view, wide_bench_toggle)
assert_visual_item_inside(root.contentItem(), wide_bench_toggle)
wide_bench = find_visual_item(root.contentItem(), "buildBenchButton0")
assert wide_bench is not None and wide_bench.isVisible()
wide_bench.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert wide_bench.property("activeFocus") is True

root.resize(1440, 640)
application.processEvents()
build_view.setProperty("benchExpanded", False)
application.processEvents()
low_build_scroll = find_visual_item(root.contentItem(), "narrowBuildScroll")
low_bench_toggle = find_visual_item(root.contentItem(), "buildBenchToggle")
low_bench_panel = find_visual_item(root.contentItem(), "narrowBuildBench")
assert build_view.property("compactPresentation") is True
assert preferences.cardPreview is True
assert low_build_scroll is not None and low_build_scroll.isVisible()
assert low_bench_toggle is not None and low_bench_toggle.isVisible()
assert low_bench_panel is not None and low_bench_panel.isVisible()
assert_visual_item_inside(build_view, low_build_scroll)
build_view.setProperty("benchExpanded", True)
application.processEvents()
assert build_view.property("benchExpanded") is True
assert low_build_scroll.isVisible()
assert low_bench_panel.isVisible()
assert_visual_item_inside(build_view, low_build_scroll)
low_bench_row = find_visual_item(root.contentItem(), "buildBenchButton0")
assert low_bench_row is not None and low_bench_row.isVisible()
build_view.setProperty("benchExpanded", False)
application.processEvents()
assert build_view.property("benchExpanded") is False
assert low_build_scroll.isVisible()
assert low_bench_panel.isVisible()
assert low_bench_row.isVisible() is False
assert_visual_item_inside(build_view, low_build_scroll)

root.resize(1440, 900)
application.processEvents()
build_view.setProperty("benchExpanded", False)
application.processEvents()
wide_bench_toggle = find_visual_item(root.contentItem(), "buildBenchToggle")
wide_bench_panel = find_visual_item(root.contentItem(), "wideBuildBench")
wide_bench_scroll = find_visual_item(root.contentItem(), "wideBuildBenchScroll")
assert wide_bench_toggle is not None and wide_bench_toggle.isVisible()
assert wide_bench_panel is not None and wide_bench_panel.isVisible()
assert wide_bench_scroll is not None
assert_visual_item_inside(build_view, wide_bench_toggle)
assert_visual_item_inside(root.contentItem(), wide_bench_toggle)
wide_preview = find_visual_item(root.contentItem(), "wideBuildCardPreview")
wide_curve = find_visual_item(root.contentItem(), "wideBuildManaCurve")
assert wide_preview is not None and wide_preview.isVisible()
assert wide_curve is not None and wide_curve.isVisible()
assert_visual_item_inside(build_view, wide_preview)
collapsed_bench_geometry = scene_geometry(wide_bench_panel)
collapsed_spells_geometry = scene_geometry(wide_spells)
collapsed_preview_geometry = scene_geometry(wide_preview)
wide_bench_toggle.forceActiveFocus()
expanded_bench_geometry = None
expanded_spells_geometry = None
expanded_preview_geometry = None
for cycle in range(2):
    trigger_and_wait_for_layout(
        wide_bench_panel,
        lambda: QTest.keyClick(root, Qt.Key_Space),
        lambda height: height > collapsed_bench_geometry[3],
    )
    assert build_view.property("benchExpanded") is True
    bench_row0 = find_visual_item(root.contentItem(), "buildBenchButton0")
    bench_row1 = find_visual_item(root.contentItem(), "buildBenchButton1")
    assert bench_row0 is not None and bench_row0.isVisible()
    assert bench_row1 is not None and bench_row1.isVisible()
    for bench_row in (bench_row0, bench_row1):
        assert bench_row.height() >= 40
        assert_visual_item_inside(wide_bench_scroll, bench_row)
    current_bench_geometry = scene_geometry(wide_bench_panel)
    current_spells_geometry = scene_geometry(wide_spells)
    current_preview_geometry = scene_geometry(wide_preview)
    if cycle == 0:
        expanded_bench_geometry = current_bench_geometry
        expanded_spells_geometry = current_spells_geometry
        expanded_preview_geometry = current_preview_geometry
    else:
        assert_geometry_close(wide_bench_panel, expanded_bench_geometry)
        assert_geometry_close(wide_spells, expanded_spells_geometry)
        assert_geometry_close(wide_preview, expanded_preview_geometry)
    assert current_bench_geometry[3] > collapsed_bench_geometry[3]
    assert current_bench_geometry[1] < collapsed_bench_geometry[1]
    assert current_spells_geometry[3] < collapsed_spells_geometry[3]
    assert current_preview_geometry[3] < collapsed_preview_geometry[3]

    wide_bench_toggle.forceActiveFocus()
    trigger_and_wait_for_layout(
        wide_bench_panel,
        lambda: QTest.keyClick(root, Qt.Key_Space),
        lambda height: abs(height - collapsed_bench_geometry[3]) <= 0.1,
    )
    assert build_view.property("benchExpanded") is False
    assert_geometry_close(wide_bench_panel, collapsed_bench_geometry)
    assert_geometry_close(wide_spells, collapsed_spells_geometry)
    assert_geometry_close(wide_preview, collapsed_preview_geometry)

wide_heading = find_visual_item(wide_preview, "cardPreviewHeading")
wide_frame = find_visual_item(wide_preview, "cardPreviewImageFrame")
wide_fallback = find_visual_item(wide_preview, "cardPreviewFallback")
wide_details = find_visual_item(wide_preview, "cardPreviewDetails")
assert wide_heading is not None and wide_heading.isVisible()
assert wide_frame is not None and wide_frame.isVisible()
assert abs(wide_frame.height() - wide_frame.width() * 1.4) <= 0.1
assert wide_details is not None and wide_details.isVisible()
assert wide_frame.width() > 240
assert wide_preview.width() - wide_frame.width() >= 40
assert abs(wide_details.width() - wide_frame.width()) <= 1
assert abs(wide_details.x() - wide_frame.x()) <= 1
assert wide_details.y() - (wide_frame.y() + wide_frame.height()) >= 18
assert wide_fallback is not None and wide_fallback.isVisible()
for item in (wide_heading, wide_frame, wide_fallback, wide_details):
    assert_visual_item_inside(wide_preview, item)
assert_visual_item_inside(build_view, wide_curve)
accessible_preview = QAccessible.queryAccessibleInterface(wide_preview)
assert accessible_preview is not None
assert accessible_preview.text(QAccessible.Text.Name) == (
    "Selected card, " + provider.state["build"]["bench"][0]["card"]["name"]
)


provider.selectScenario("build_error")
application.processEvents()
build_request = root.findChild(QObject, "buildRequestButton")
assert build_request is not None
assert provider.state["errors"][0]["operation"] == "build"
build_request.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert any(
    isinstance(command, RequestBuild) and command.pair_override is None
    for command in provider.commands
)

root.setProperty("currentSurface", "backtest")
application.processEvents()
backtest_view = find_visual_item(root.contentItem(), "backtestView")
assert backtest_view is not None
run_backtest = root.findChild(QObject, "backtestRunButton")
assert run_backtest is not None
run_backtest.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert any(isinstance(command, RequestBacktest) for command in provider.commands)
subtitle = root.findChild(QObject, "backtestSubtitle")
assert subtitle is not None
assert "MagoAnubiTest (mock-account)" in subtitle.property("text")
assert "Draft mock-otj-draft" in subtitle.property("text")
assert "OTJ" in subtitle.property("text")
assert "Quick Draft" in subtitle.property("text")
root.resize(1440, 900)
application.processEvents()
win_rate_header = find_visual_item(root.contentItem(), "backtestWinRateHeader")
win_rate = find_visual_item(root.contentItem(), "backtestWinRate0")
assert win_rate_header is not None and win_rate_header.isVisible()
assert win_rate is not None and win_rate.isVisible() and win_rate.property("text") == "63.6%"

root.setProperty("currentSurface", "settings")
application.processEvents()
secondary_stats = root.findChild(QObject, "settingsSecondaryStatsSwitch")
assert secondary_stats is not None and secondary_stats.property("checked") is True
secondary_stats.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert preferences.secondaryStats is False

root.setProperty("currentSurface", "backtest")
application.processEvents()
assert win_rate_header.isVisible() is False
assert win_rate.isVisible() is False
source_header = find_visual_item(root.contentItem(), "backtestSourceHeader")
source = find_visual_item(root.contentItem(), "backtestSource0")
assert source_header is not None and source_header.isVisible() is False
assert source is not None and source.isVisible() is False
for name in ("backtestRecommended0", "backtestActual0", "backtestResult0", "backtestScore0"):
    primary = find_visual_item(root.contentItem(), name)
    assert primary is not None and primary.isVisible()
root.resize(760, 900)
application.processEvents()
narrow_secondary = find_visual_item(root.contentItem(), "backtestNarrowSecondary0")
assert narrow_secondary is not None and narrow_secondary.isVisible() is False
narrow_score = find_visual_item(root.contentItem(), "backtestNarrowScore0")
assert narrow_score is not None and narrow_score.isVisible()


provider.selectScenario("backtest_missing")
application.processEvents()
rows = root.findChild(QObject, "backtestRows")
assert rows is not None
assert rows.property("count") == 1
assert provider.state["backtest"]["skipped_count"] == 1
subtitle = root.findChild(QObject, "backtestSubtitle")
assert subtitle is not None
assert "recorded picks" in subtitle.property("text")


root.resize(760, 900)
application.processEvents()
assert root.property("narrow") is True
assert root.width() == 760
assert rows.width() <= root.width()
root.resize(1440, 900)
application.processEvents()
assert root.property("narrow") is False
assert root.width() == 1440

provider.selectScenario("backtest_error")
application.processEvents()
subtitle = root.findChild(QObject, "backtestSubtitle")
assert subtitle is not None
assert subtitle.property("text") == "Compare persisted picks with the active ranking"
dismiss = find_visual_item(backtest_view, "sessionErrorDismissButton")
assert dismiss is not None and dismiss.property("visible") is True
dismiss.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert provider.state["errors"] == []

provider.selectScenario("backtest_error")
application.processEvents()
retry = find_visual_item(backtest_view, "sessionErrorRetryButton")
assert retry is not None and retry.property("visible") is True
retry.forceActiveFocus()
QTest.keyClick(root, Qt.Key_Space)
application.processEvents()
assert provider.state["errors"] == []

preferences.shutdown()
del engine

"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr


def test_qml_dimensional_controls_have_distinct_states_and_fit_narrow_window() -> None:
    probe = """
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QPointF, QObject, Qt, QUrl
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_mock import MockSessionAdapter


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
session = MockLiveSession(scenario="ready")
provider = MockSessionAdapter(session=session)

with TemporaryDirectory() as preferences_dir:
    preferences = GuiPreferencesAdapter(app_dir=preferences_dir)
    engine = QQmlApplicationEngine()
    qml_directory = Path.cwd() / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", "monospace")
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", "0.0")
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("initialSurface", "live")
    context.setContextProperty("initialWindowWidth", 680)
    context.setContextProperty("initialWindowHeight", 640)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    root.resize(680, 640)
    application.processEvents()

    navigation = root.findChild(QObject, "navigationRail")
    about = root.findChild(QObject, "aboutLink")
    privacy = root.findChild(QObject, "privacyLink")
    settings = root.findChild(QObject, "settingsButton")
    ranking = root.findChild(QObject, "rankingSelector")
    status_strip = root.findChild(QObject, "statusStrip")
    assert navigation is not None
    assert about is not None and privacy is not None
    assert settings is not None and ranking is not None and status_strip is not None
    assert about.isVisible() and privacy.isVisible()
    for control in (about, privacy):
        assert control.property("height") >= 42
        position = control.mapToItem(navigation, QPointF(0, 0))
        assert position.x() >= 0
        assert position.y() >= 0
        assert position.y() + control.property("height") <= navigation.property("height")
        accessible = QAccessible.queryAccessibleInterface(control)
        assert accessible is not None
        assert accessible.text(QAccessible.Text.Name)

    for control in (settings, ranking):
        assert control.property("height") >= 42
        position = control.mapToItem(root.contentItem(), QPointF(0, 0))
        assert position.x() >= 0
        assert position.y() >= 0
        assert position.x() + control.property("width") <= root.width()
        accessible = QAccessible.queryAccessibleInterface(control)
        assert accessible is not None
        assert accessible.text(QAccessible.Text.Name)
    surface = settings.findChild(QObject, "dimensionalSurface")
    assert surface is not None
    assert surface.property("stateEnabled") is True
    assert surface.property("statePressed") is False
    assert surface.property("stateSelected") is False
    resting_fill = surface.property("topFillColor")
    assert surface.property("faceY") == 0
    surface.setProperty("stateHovered", True)
    application.processEvents()
    assert surface.property("faceY") == 0
    assert surface.property("topFillColor") != resting_fill
    surface.setProperty("stateHovered", False)
    application.processEvents()
    settings.forceActiveFocus()
    application.processEvents()
    assert surface.property("stateFocused") is True
    assert surface.property("outlineWidth") >= 2
    settings.setProperty("checked", True)
    application.processEvents()
    assert surface.property("stateSelected") is True
    settings.setProperty("enabled", False)
    application.processEvents()
    assert surface.property("stateEnabled") is False
    settings.setProperty("enabled", True)

    ranking.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    popup = ranking.findChild(QObject, "dimensionalComboPopup")
    combo_list = ranking.findChild(QObject, "dimensionalComboList")
    assert popup is not None and popup.property("visible") is True
    assert combo_list is not None
    delegate = combo_list.property("currentItem")
    assert delegate is not None and delegate.property("height") >= 42
    delegate_surface = delegate.findChild(QObject, "dimensionalSurface")
    selected_indicator = delegate.findChild(
        QObject, "dimensionalComboSelectedIndicator"
    )
    assert delegate_surface is not None
    assert delegate_surface.property("stateExpanded") is True
    assert selected_indicator is not None
    assert selected_indicator.property("visible") is True
    ranking.setProperty("currentIndex", 1)
    application.processEvents()
    assert ranking.property("currentIndex") == 1
    QTest.keyClick(root, Qt.Key_Escape)
    application.processEvents()
    assert popup.property("visible") is False

    about.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    about_dialog = root.findChild(QObject, "aboutDialog")
    close = root.findChild(QObject, "aboutDialogCloseButton")
    assert about_dialog is not None and about_dialog.property("visible") is True
    assert close is not None and close.property("height") >= 42
    close.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert about_dialog.property("visible") is False
    assert about.property("activeFocus") is True

    del engine
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr


def test_qml_settings_switches_expose_contrast_states_and_keyboard_toggle() -> None:
    probe = """
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QPoint, QObject, QPointF, Qt, QUrl
from PySide6.QtGui import QAccessible, QColor, QFontInfo, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from draftomen.mock_session import MockLiveSession
from draftomen.qt_adapter import GuiPreferencesAdapter
from draftomen.qt_mock import MockSessionAdapter


def relative_luminance(value) -> float:
    color = QColor(value)
    channels = [color.redF(), color.greenF(), color.blueF()]
    linear = [
        channel / 12.92 if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(first, second) -> float:
    first_luminance = relative_luminance(first)
    second_luminance = relative_luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def expected_scaled_pixel_size(resolved_pixel_size: int) -> int:
    return int(11 * resolved_pixel_size / 13 + 0.5)

def expected_system_scale_percent(resolved_pixel_size: int) -> int:
    return int(100 * resolved_pixel_size / 13 + 0.5)


def assert_accessible_status(item, expected: str) -> None:
    accessible = QAccessible.queryAccessibleInterface(item)
    assert accessible is not None
    assert accessible.text(QAccessible.Text.Name) == expected
    assert accessible.text(QAccessible.Text.Description) == expected


def wait_for_saved(preferences: GuiPreferencesAdapter) -> None:
    for _ in range(100):
        application.processEvents()
        if preferences.persistenceMessage == "Saved":
            return
        QTest.qWait(10)
    assert preferences.persistenceMessage == "Saved"


QQuickStyle.setStyle("Fusion")
application = QGuiApplication([])
application_font = application.font()
application_font.setPixelSize(26)
application.setFont(application_font)
assert application_font.pixelSize() == 26
assert QFontInfo(application_font).pixelSize() == 26
provider = MockSessionAdapter(session=MockLiveSession(scenario="ready"))

with TemporaryDirectory() as preferences_dir:
    preferences = GuiPreferencesAdapter(app_dir=preferences_dir)
    engine = QQmlApplicationEngine()
    qml_directory = Path.cwd() / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", "monospace")
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", "0.0")
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("initialSurface", "settings")
    context.setContextProperty("initialWindowWidth", 900)
    context.setContextProperty("initialWindowHeight", 760)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    assert engine.rootObjects()
    root = engine.rootObjects()[0]
    application.processEvents()
    navigation = root.findChild(QObject, "navigationRail")
    assert navigation is not None
    status_strip = root.findChild(QObject, "statusStrip")
    assert status_strip is not None
    persistence_message = root.findChild(QObject, "statusPersistenceMessage")
    assert persistence_message is not None
    assert persistence_message.property("text") == "Saved"
    assert QColor(persistence_message.property("color")) == QColor("#a78bfa")
    show_backtest = root.findChild(QObject, "settingsShowBacktestSwitch")
    assert show_backtest is not None
    assert show_backtest.property("checked") is False
    assert preferences.showBacktest is False
    assert navigation.property("backtestNavigationVisible") is False
    show_backtest.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert show_backtest.property("checked") is True
    assert preferences.showBacktest is True
    assert navigation.property("backtestNavigationVisible") is True
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert show_backtest.property("checked") is False
    assert preferences.showBacktest is False
    contextual_switch = root.findChild(QObject, "settingsContextualScoringSwitch")
    assert contextual_switch is not None
    assert contextual_switch.property("checked") is False
    accessible_contextual = QAccessible.queryAccessibleInterface(contextual_switch)
    assert accessible_contextual is not None
    assert accessible_contextual.text(QAccessible.Text.Name) == "Contextual pick scoring"
    assert accessible_contextual.text(QAccessible.Text.Description) == (
        "Enable contextual score adjustments and evidence in recommendations and backtests. "
        "Saved for this desktop application."
    )
    contextual_center = contextual_switch.mapToItem(
        root.contentItem(),
        QPointF(contextual_switch.width() / 2, contextual_switch.height() / 2),
    )
    QTest.mouseClick(
        root,
        Qt.LeftButton,
        Qt.NoModifier,
        QPoint(round(contextual_center.x()), round(contextual_center.y())),
    )
    application.processEvents()
    assert contextual_switch.property("checked") is True
    assert preferences.contextualAdjustmentsEnabled is True
    contextual_switch.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert contextual_switch.property("checked") is False
    assert preferences.contextualAdjustmentsEnabled is False
    QTest.mouseClick(
        root,
        Qt.LeftButton,
        Qt.NoModifier,
        QPoint(round(contextual_center.x()), round(contextual_center.y())),
    )
    application.processEvents()
    assert contextual_switch.property("checked") is True
    assert preferences.contextualAdjustmentsEnabled is True
    wait_for_saved(preferences)
    persisted_contextual_preferences = GuiPreferencesAdapter(
        app_dir=preferences_dir
    )
    try:
        assert persisted_contextual_preferences.contextualAdjustmentsEnabled is True
    finally:
        persisted_contextual_preferences.shutdown()
    assert root.findChild(QObject, "settingsPersistenceMessage") is None
    accessible_persistence = QAccessible.queryAccessibleInterface(persistence_message)
    assert accessible_persistence is not None
    assert accessible_persistence.text(QAccessible.Text.Name) == "Saved"
    assert accessible_persistence.text(QAccessible.Text.Description)
    assert persistence_message.width() <= status_strip.width()
    names = (
        "settingsSplashSwitch",
        "settingsShowBacktestSwitch",
        "settingsContextualScoringSwitch",
        "settingsCompactDensitySwitch",
        "settingsSecondaryStatsSwitch",
        "settingsCardPreviewSwitch",
        "settingsDetailedBuildContextSwitch",
        "settingsSystemTextScalingSwitch",
    )
    switches = [root.findChild(QObject, name) for name in names]
    assert all(switch is not None for switch in switches)
    assert all(switch.property("height") >= 42 for switch in switches)
    assert all(switch.property("width") >= 40 for switch in switches)

    checked_switches = [switch for switch in switches if switch.property("checked")]
    unchecked_switches = [
        switch for switch in switches if not switch.property("checked")
    ]
    assert len(checked_switches) == 6
    assert len(unchecked_switches) == 2

    for switch in switches:
        is_checked = switch.property("checked") is True
        assert switch.property("visualChecked") is is_checked
        assert switch.property("visualUnchecked") is (not is_checked)
        assert switch.property("visualDisabled") is False
        assert switch.property("visualFocused") == switch.property("activeFocus")
        assert switch.property("visualState") == (
            "checked" if is_checked else "unchecked"
        )
        assert switch.findChild(QObject, "settingsSwitchTrack") is not None
        assert switch.findChild(QObject, "settingsSwitchThumb") is not None
        assert switch.findChild(QObject, "settingsSwitchStateText") is not None
        assert switch.findChild(QObject, "settingsSwitchFocusRing") is not None

    checked_track = QColor(checked_switches[0].property("visualTrackColor"))
    unchecked_track = QColor(unchecked_switches[0].property("visualTrackColor"))
    checked_thumb = QColor(checked_switches[0].property("visualThumbColor"))
    unchecked_thumb = QColor(unchecked_switches[0].property("visualThumbColor"))
    assert all(
        QColor(switch.property("visualTrackColor")) == checked_track
        for switch in checked_switches
    )
    assert all(
        QColor(switch.property("visualTrackColor")) == unchecked_track
        for switch in unchecked_switches
    )
    assert all(
        QColor(switch.property("visualThumbColor")) == checked_thumb
        for switch in checked_switches
    )
    assert all(
        QColor(switch.property("visualThumbColor")) == unchecked_thumb
        for switch in unchecked_switches
    )
    assert checked_track != unchecked_track
    assert checked_thumb != unchecked_thumb
    assert relative_luminance(checked_track) < relative_luminance(unchecked_track)
    assert contrast_ratio(
        checked_track, checked_switches[0].property("visualContentColor")
    ) >= 4.5
    assert contrast_ratio(
        unchecked_track, unchecked_switches[0].property("visualContentColor")
    ) >= 4.5
    assert contrast_ratio(
        checked_thumb, checked_switches[0].property("visualThumbContentColor")
    ) >= 4.5
    assert contrast_ratio(
        unchecked_thumb, unchecked_switches[0].property("visualThumbContentColor")
    ) >= 4.5

    disabled_switch = checked_switches[0]
    disabled_switch.setProperty("enabled", False)
    application.processEvents()
    assert disabled_switch.property("visualDisabled") is True
    assert disabled_switch.property("visualState") == "disabled"
    assert QColor(disabled_switch.property("visualTrackColor")) == QColor(
        disabled_switch.property("disabledTrackColor")
    )
    assert QColor(disabled_switch.property("visualThumbColor")) == QColor(
        disabled_switch.property("disabledThumbColor")
    )
    assert contrast_ratio(
        disabled_switch.property("visualTrackColor"),
        disabled_switch.property("visualContentColor"),
    ) >= 4.5
    assert contrast_ratio(
        disabled_switch.property("visualThumbColor"),
        disabled_switch.property("visualThumbContentColor"),
    ) >= 4.5
    focus_ring = disabled_switch.findChild(QObject, "settingsSwitchFocusRing")
    assert focus_ring is not None and focus_ring.property("visible") is False
    disabled_switch.setProperty("enabled", True)

    system_switch = root.findChild(QObject, "settingsSystemTextScalingSwitch")
    assert system_switch is not None and system_switch.property("checked") is True
    accessible_system = QAccessible.queryAccessibleInterface(system_switch)
    assert accessible_system is not None
    assert accessible_system.text(QAccessible.Text.Name) == "Follow system text size"
    assert accessible_system.text(QAccessible.Text.Description) == (
        "Use the resolved system text size instead of Draft Omen's 100% baseline."
    )
    system_message = root.findChild(QObject, "settingsSystemTextScalingMessage")
    assert system_message is not None
    resolved_pixel_size = QFontInfo(application.font()).pixelSize()
    assert resolved_pixel_size == 26
    resolved_scale_percent = expected_system_scale_percent(resolved_pixel_size)
    enabled_different_message = (
        f"Following system text size at the detected {resolved_scale_percent}% scale."
    )
    assert system_message.property("text") == enabled_different_message
    assert_accessible_status(system_message, enabled_different_message)

    persistence_message = root.findChild(QObject, "statusPersistenceMessage")
    assert persistence_message is not None
    assert root.findChild(QObject, "settingsPersistenceMessage") is None
    scaled_font = persistence_message.property("font")
    resolved_pixel_size = QFontInfo(application.font()).pixelSize()
    assert scaled_font.pixelSize() == expected_scaled_pixel_size(resolved_pixel_size)
    inherited_font_control = root.findChild(
        QObject, "settingsRatingsDownloadButton"
    )
    assert inherited_font_control is not None
    inherited_font = inherited_font_control.property("font")
    assert inherited_font.pixelSize() == resolved_pixel_size
    inherited_font_signature = (
        inherited_font.family(),
        inherited_font.styleName(),
        inherited_font.weight(),
        inherited_font.italic(),
    )
    system_switch.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert system_switch.property("checked") is False
    assert preferences.systemTextScaling is False
    unscaled_font = persistence_message.property("font")
    assert unscaled_font.pixelSize() == 11
    assert inherited_font_control.property("font").pixelSize() == 13
    assert (
        inherited_font_control.property("font").family(),
        inherited_font_control.property("font").styleName(),
        inherited_font_control.property("font").weight(),
        inherited_font_control.property("font").italic(),
    ) == inherited_font_signature
    disabled_different_message = (
        f"Using Draft Omen's 100% baseline. The detected system scale is "
        f"{resolved_scale_percent}%."
    )
    assert system_message.property("text") == disabled_different_message
    assert_accessible_status(system_message, disabled_different_message)

    wait_for_saved(preferences)
    assert persistence_message.property("text") == "Saved"
    assert QColor(persistence_message.property("color")) == QColor("#a78bfa")
    assert_accessible_status(persistence_message, "Saved")
    persisted_system_preferences = GuiPreferencesAdapter(app_dir=preferences_dir)
    try:
        assert persisted_system_preferences.systemTextScaling is False
    finally:
        persisted_system_preferences.shutdown()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert system_switch.property("checked") is True
    assert persistence_message.property("font").pixelSize() == scaled_font.pixelSize()
    assert inherited_font_control.property("font").pixelSize() == resolved_pixel_size
    assert (
        inherited_font_control.property("font").family(),
        inherited_font_control.property("font").styleName(),
        inherited_font_control.property("font").weight(),
        inherited_font_control.property("font").italic(),
    ) == inherited_font_signature
    assert system_message.property("text") == enabled_different_message
    assert_accessible_status(system_message, enabled_different_message)

    point_font = application.font()
    point_font.setPointSize(16)
    application.setFont(point_font)
    application.processEvents()
    assert point_font.pixelSize() == -1
    resolved_point_size = QFontInfo(point_font).pixelSize()
    assert resolved_point_size > 13
    point_scaled_font = persistence_message.property("font")
    assert point_scaled_font.pixelSize() == expected_scaled_pixel_size(resolved_point_size)
    assert inherited_font_control.property("font").pixelSize() == resolved_point_size
    resolved_point_scale_percent = expected_system_scale_percent(resolved_point_size)
    enabled_point_message = (
        f"Following system text size at the detected "
        f"{resolved_point_scale_percent}% scale."
    )
    assert system_message.property("text") == enabled_point_message
    assert_accessible_status(system_message, enabled_point_message)

    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert system_switch.property("checked") is False
    assert persistence_message.property("font").pixelSize() == 11
    assert inherited_font_control.property("font").pixelSize() == 13
    disabled_point_message = (
        f"Using Draft Omen's 100% baseline. The detected system scale is "
        f"{resolved_point_scale_percent}%."
    )
    assert system_message.property("text") == disabled_point_message
    assert_accessible_status(system_message, disabled_point_message)

    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert system_switch.property("checked") is True
    assert persistence_message.property("font").pixelSize() == point_scaled_font.pixelSize()
    assert inherited_font_control.property("font").pixelSize() == resolved_point_size
    assert system_message.property("text") == enabled_point_message
    assert_accessible_status(system_message, enabled_point_message)

    persisted_preferences = GuiPreferencesAdapter(app_dir=preferences_dir)
    persisted_preferences.setSystemTextScaling(False)
    persisted_preferences.setShowBacktest(True)
    persisted_preferences.setContextualAdjustmentsEnabled(True)
    assert persisted_preferences.systemTextScaling is False
    assert persisted_preferences.showBacktest is True
    assert persisted_preferences.contextualAdjustmentsEnabled is True
    wait_for_saved(persisted_preferences)
    preferences.shutdown()
    del root
    del engine

    reload_engine = QQmlApplicationEngine()
    reload_engine.addImportPath(str(qml_directory))
    reload_context = reload_engine.rootContext()
    reload_context.setContextProperty("fixedFontFamily", "monospace")
    reload_context.setContextProperty("sessionProvider", provider)
    reload_context.setContextProperty("applicationTitle", "Draft Omen")
    reload_context.setContextProperty("applicationVersion", "0.0")
    reload_context.setContextProperty("guiPreferences", persisted_preferences)
    reload_context.setContextProperty("initialSurface", "settings")
    reload_context.setContextProperty("initialWindowWidth", 900)
    reload_context.setContextProperty("initialWindowHeight", 760)
    reload_engine.setInitialProperties({"provider": provider})
    reload_engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    assert reload_engine.rootObjects()
    root = reload_engine.rootObjects()[0]
    application.processEvents()
    reloaded_navigation = root.findChild(QObject, "navigationRail")
    assert reloaded_navigation is not None
    assert reloaded_navigation.property("backtestNavigationVisible") is True
    reloaded_show_backtest = root.findChild(
        QObject, "settingsShowBacktestSwitch"
    )
    assert reloaded_show_backtest is not None
    assert reloaded_show_backtest.property("checked") is True
    reloaded_system_switch = root.findChild(
        QObject, "settingsSystemTextScalingSwitch"
    )
    assert reloaded_system_switch is not None
    assert reloaded_system_switch.property("checked") is False
    reloaded_contextual_switch = root.findChild(
        QObject, "settingsContextualScoringSwitch"
    )
    assert reloaded_contextual_switch is not None
    assert reloaded_contextual_switch.property("checked") is True
    reloaded_inherited_font_control = root.findChild(
        QObject, "settingsRatingsDownloadButton"
    )
    assert reloaded_inherited_font_control is not None
    assert reloaded_inherited_font_control.property("font").pixelSize() == 13
    reloaded_persistence_message = root.findChild(
        QObject, "statusPersistenceMessage"
    )
    assert reloaded_persistence_message is not None
    assert root.findChild(QObject, "settingsPersistenceMessage") is None
    assert reloaded_persistence_message.property("font").pixelSize() == 11
    reloaded_system_message = root.findChild(
        QObject, "settingsSystemTextScalingMessage"
    )
    assert reloaded_system_message is not None
    assert reloaded_system_message.property("text") == disabled_point_message
    assert_accessible_status(reloaded_system_message, disabled_point_message)

    assert (
        reloaded_inherited_font_control.property("font").family(),
        reloaded_inherited_font_control.property("font").styleName(),
        reloaded_inherited_font_control.property("font").weight(),
        reloaded_inherited_font_control.property("font").italic(),
    ) == inherited_font_signature
    reloaded_system_switch.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert reloaded_system_switch.property("checked") is True
    assert reloaded_inherited_font_control.property("font").pixelSize() == (
        resolved_point_size
    )
    assert reloaded_persistence_message.property("font").pixelSize() == (
        expected_scaled_pixel_size(resolved_point_size)
    )
    assert reloaded_system_message.property("text") == enabled_point_message
    assert_accessible_status(reloaded_system_message, enabled_point_message)
    equal_font = application.font()
    equal_font.setPixelSize(13)
    application.setFont(equal_font)
    application.processEvents()
    resolved_equal_size = QFontInfo(application.font()).pixelSize()
    assert resolved_equal_size == 13
    assert reloaded_inherited_font_control.property("font").pixelSize() == 13
    assert reloaded_persistence_message.property("font").pixelSize() == 11
    enabled_equal_message = (
        "Following system text size. The detected 100% scale matches "
        "Draft Omen's default, so no visible size change is expected."
    )
    assert reloaded_system_message.property("text") == enabled_equal_message
    assert_accessible_status(reloaded_system_message, enabled_equal_message)

    reloaded_system_switch.forceActiveFocus()
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert reloaded_system_switch.property("checked") is False
    disabled_equal_message = (
        "Using Draft Omen's 100% baseline. The detected system scale is 100%."
    )
    assert reloaded_system_message.property("text") == disabled_equal_message
    assert_accessible_status(reloaded_system_message, disabled_equal_message)
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert reloaded_system_switch.property("checked") is True
    assert reloaded_system_message.property("text") == enabled_equal_message
    assert_accessible_status(reloaded_system_message, enabled_equal_message)

    card_preview = root.findChild(QObject, "settingsCardPreviewSwitch")
    assert card_preview is not None
    accessible = QAccessible.queryAccessibleInterface(card_preview)
    assert accessible is not None
    assert accessible.text(QAccessible.Text.Name) == "Card image preview"
    assert accessible.text(QAccessible.Text.Description) == (
        "Saved desktop display preference."
    )
    card_preview.forceActiveFocus()
    application.processEvents()
    assert card_preview.property("visualFocused") is True
    focus_ring = card_preview.findChild(QObject, "settingsSwitchFocusRing")
    assert focus_ring is not None and focus_ring.property("visible") is True
    was_checked = card_preview.property("checked")
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert card_preview.property("checked") is (not was_checked)
    QTest.keyClick(root, Qt.Key_Space)
    application.processEvents()
    assert card_preview.property("checked") is was_checked

    persisted_preferences.shutdown()
    del reload_engine
"""
    completed = _run_qml_probe(probe)

    assert completed.returncode == 0, completed.stderr
    assert "Binding loop detected" not in completed.stderr
    assert "Unable to assign" not in completed.stderr
    assert "TypeError" not in completed.stderr
