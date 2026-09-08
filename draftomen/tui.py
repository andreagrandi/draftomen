"""Textual live interface for Draftomen watch mode.
Render ranked packs and status updates without blocking fetches.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import replace
from os import PathLike
from pathlib import Path
from threading import Lock, get_ident
from typing import TypeAlias

from rich.align import Align
from rich.console import Group
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    ProgressBar,
    Select,
    Static,
    Switch,
)
from textual.worker import get_current_worker

try:  # pragma: no cover - import availability depends on optional terminal extras.
    from textual_image.renderable.tgp import Image as TgpImage
except Exception:  # pragma: no cover - graceful fallback when unavailable.
    TgpImage = None

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.cardimages import CardImageService, card_image_cache_dir
from draftomen.config import COLOR_PAIRS, POLL_INTERVAL_SECONDS
from draftomen.deckbuilder import BuildPool, ManaBase, PairSelection, SpellSelection
from draftomen.events import (
    DraftCompletedEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
    QuickDraftDetectedEvent,
)
from draftomen.pickengine import (
    ScoredCard,
    ScoredPack,
    recommendation_confidence_summary,
)
from draftomen.preferences import (
    TuiVisibilityPreferences,
    load_tui_preferences,
    save_tui_preferences,
)
from draftomen.profile_client import ProfileClient, ProfileRefreshResult
from draftomen.ranking import (
    DEFAULT_RANKING_MODE,
    RANKING_LABELS,
    RANKING_MODES,
    rank_scored_cards,
    ranking_label,
)
from draftomen.session import (
    ApplicationPhase,
    BacktestPickResult,
    BacktestResult,
    BuildResult,
    CardView,
    ChangeRanking,
    ChangeSplashPreference,
    ChooseAccount,
    DataLoadPhase,
    LiveSession,
    LiveSessionCommand,
    LiveSessionEvent,
    LiveSessionSnapshot,
    OperationKind,
    PoolState,
    Recommendation,
    ProfileRefreshRequest,
    RequestBacktest,
    RequestBuild,
    RequestRatingsDownload,
    RetryError,
    SessionError,
    SetCardDataLoader,
)
from draftomen.setinfo import format_set_label
from draftomen.seventeen import (
    SEVENTEEN_LANDS_ATTRIBUTION,
)

_EMPTY_CARD_DATABASE = CardDatabase(cards={})

PathInput: TypeAlias = str | PathLike[str]
TuiCardQuantityKey: TypeAlias = tuple[str, str]
TuiCardQuantityGroup: TypeAlias = tuple[ScoredCard, int]


class _SessionSnapshotMessage(Message):
    """Publish one immutable session snapshot on the Textual message loop."""

    def __init__(self, snapshot: LiveSessionSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot


PRIMARY_COLUMN_KEYS = ("rank", "win_rate", "grade", "score", "card", "colors")
SECONDARY_COLUMN_KEYS = ("fit", "alsa", "mv", "source")
SORT_MODES = RANKING_MODES
BUILD_SPELL_SORT_MODES = ("curve", "score", "name")
SECONDARY_COLUMN_MIN_WIDTH = 88
SIDEBAR_MIN_WIDTH = 56
COLOR_ORDER = ("W", "U", "B", "R", "G")
COLOR_NAMES = {
    "W": "White",
    "U": "Blue",
    "B": "Black",
    "R": "Red",
    "G": "Green",
}
BASIC_LAND_NAMES = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}
COLORLESS_KEY = "C"
UNKNOWN_COLOR_KEY = "?"
CURVE_BUCKET_LABELS = ("0", "1", "2", "3", "4", "5", "6+")
SPARKLINE_GLYPHS = "▁▂▃▄▅▆▇█"
CARD_IMAGE_PREVIEW_ENV = "DRAFTOMEN_CARD_IMAGES"
MANA_ICON_GLYPHS = {
    "W": "\ue600",
    "U": "\ue601",
    "B": "\ue602",
    "R": "\ue603",
    "G": "\ue604",
    "C": "\ue904",
}
MANA_CARD_TYPE_GLYPHS = {
    "Artifact": "\ue61e",
    "Creature": "\ue61f",
    "Enchantment": "\ue620",
    "Instant": "\ue621",
    "Land": "\ue622",
    "Planeswalker": "\ue623",
    "Sorcery": "\ue624",
}

COLOR_STYLES = {
    "W": "bold bright_white",
    "U": "bold dodger_blue1",
    "B": "bold grey50",
    "R": "bold red3",
    "G": "bold green3",
}

COLUMN_LABELS = {
    "rank": "#",
    "win_rate": "17L WR",
    "grade": "17L Grade",
    "score": "DO",
    "card": "Card",
    "colors": "Colors",
    "fit": "Fit",
    "gih": "GIH WR",
    "alsa": "ALSA",
    "mv": "MV",
    "source": "Source",
}

COLUMN_WIDTHS = {
    "rank": 3,
    "win_rate": 8,
    "grade": 9,
    "score": 5,
    "card": None,
    "colors": 10,
    "fit": 10,
    "gih": 8,
    "alsa": 7,
    "mv": 5,
    "source": 9,
}

SORT_LABELS = RANKING_LABELS


class CardDetailsPanel(Static, can_focus=False):
    """Sidebar panel for the highlighted card.
    Keep focus styling ready for future actions, but do not enter it with Tab yet.
    """


_VISIBILITY_BOOLEAN_OPTIONS = (
    (
        "splash_enabled",
        "Splash recommendations — consider one supported third color for "
        "exceptionally strong cards.",
    ),
    (
        "secondary_columns",
        "Secondary pack columns — show Fit, ALSA, mana value, and source on wide "
        "screens.",
    ),
    (
        "build_details",
        "Build details — show the picked pool, pair reasoning, checks, and bench "
        "cuts.",
    ),
    (
        "pool_metadata",
        "Pool metadata — show set, event, pool size, pairs, and metadata status.",
    ),
    (
        "pool_color_distribution",
        "Pool color distribution — show the sidebar color bar.",
    ),
    (
        "pool_mana_curve",
        "Mana curve — show pool and detailed-build mana curves.",
    ),
    (
        "account_identifier",
        "Account identifier — show the active account and detailed-build account ID.",
    ),
    (
        "draft_identifier",
        "Draft identifier — show draft IDs in pool and detailed-build metadata.",
    ),
    (
        "mana_pips_and_sources",
        "Mana pips and sources — show detailed-build mana requirements and sources.",
    ),
    (
        "attribution",
        "17Lands attribution — show the data-source attribution in the status bar.",
    ),
    (
        "focused_card_details",
        "Focused card details — show statistics for the highlighted card in the "
        "sidebar.",
    ),
)


class TuiVisibilityScreen(ModalScreen[TuiVisibilityPreferences | None]):
    """Modal editor for persisted optional TUI elements.
    Saving returns the selected preferences to the calling application.
    """

    CSS = """
    TuiVisibilityScreen {
        align: center middle;
    }

    #visibility-dialog {
        width: 76;
        height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #visibility-options {
        height: 1fr;
    }

    .visibility-label {
        height: auto;
        margin-top: 1;
    }

    #visibility-actions {
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, *, preferences: TuiVisibilityPreferences) -> None:
        super().__init__()
        self.preferences = preferences

    def compose(self) -> ComposeResult:
        """Compose descriptive controls for every persisted visibility preference.
        The scrollable form fits short terminals without hiding any option.
        """

        with Vertical(id="visibility-dialog"):
            yield Static("TUI config", classes="visibility-label")
            yield Static(
                "Choose optional elements. Responsive terminal width can still hide "
                "enabled sections.",
                classes="visibility-label",
            )
            with VerticalScroll(id="visibility-options"):
                for field_name, label in _VISIBILITY_BOOLEAN_OPTIONS:
                    yield Static(label, classes="visibility-label")
                    yield Switch(
                        value=getattr(self.preferences, field_name),
                        id=_visibility_control_id(field_name=field_name),
                    )
                yield Static(
                    "Card image preview — Auto follows terminal detection; Show "
                    "requests previews; Hide prevents image loading.",
                    classes="visibility-label",
                )
                yield Select(
                    (
                        ("Auto", "auto"),
                        ("Show", "show"),
                        ("Hide", "hide"),
                    ),
                    value=self.preferences.card_image_preview,
                    allow_blank=False,
                    id=_visibility_control_id(field_name="card_image_preview"),
                )
            with Horizontal(id="visibility-actions"):
                yield Button("Save", id="save-visibility", variant="primary")
                yield Button("Cancel", id="cancel-visibility")
                yield Button("Reset defaults", id="reset-visibility")

    def action_cancel(self) -> None:
        """Dismiss the dialog without changing active or persisted preferences.
        Escape follows the explicit Cancel action.
        """

        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Save, cancel, or reset the form according to its selected action.
        Reset changes the form only until the user confirms Save.
        """

        if event.button.id == "save-visibility":
            self.dismiss(self._selected_preferences())
        elif event.button.id == "cancel-visibility":
            self.dismiss(None)
        elif event.button.id == "reset-visibility":
            self._restore_defaults()

    def _selected_preferences(self) -> TuiVisibilityPreferences:
        values = {
            field_name: self.query_one(
                f"#{_visibility_control_id(field_name=field_name)}",
                Switch,
            ).value
            for field_name, _ in _VISIBILITY_BOOLEAN_OPTIONS
        }
        card_image_preview = self.query_one(
            f"#{_visibility_control_id(field_name='card_image_preview')}",
            Select,
        ).value
        return TuiVisibilityPreferences(
            **values,
            card_image_preview=str(card_image_preview),
        )

    def _restore_defaults(self) -> None:
        defaults = TuiVisibilityPreferences()
        for field_name, _ in _VISIBILITY_BOOLEAN_OPTIONS:
            self.query_one(
                f"#{_visibility_control_id(field_name=field_name)}",
                Switch,
            ).value = getattr(defaults, field_name)
        self.query_one(
            f"#{_visibility_control_id(field_name='card_image_preview')}",
            Select,
        ).value = defaults.card_image_preview


def _visibility_control_id(*, field_name: str) -> str:
    return f"visibility-{field_name.replace('_', '-')}"


class MissingRatingsScreen(ModalScreen[bool]):
    """Ask before checking or refreshing the hosted set profile.
    Missing-data prompts explain deterministic fallback scores; refresh prompts
    explain that active cached ratings remain available.
    """

    CSS = """
    MissingRatingsScreen {
        align: center middle;
    }

    #missing-ratings-dialog {
        width: 68;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }

    #missing-ratings-title {
        height: auto;
        text-style: bold;
        margin-bottom: 1;
    }

    #missing-ratings-message {
        height: auto;
        margin-bottom: 1;
    }

    #missing-ratings-actions {
        height: auto;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Not now", show=False)]

    def __init__(self, *, set_code: str, refresh: bool = False) -> None:
        super().__init__()
        self.set_code = set_code.upper()
        self.is_refresh = refresh

    def compose(self) -> ComposeResult:
        """Compose the hosted profile refresh prompt for missing or ready data."""
        if self.is_refresh:
            title = f"Refresh hosted profile for {self.set_code}"
            message = (
                "A hosted set profile is active for this draft. Refresh it now? "
                "Progress will be shown, then the current pack will be rescored "
                "automatically."
            )
            action_label = "Refresh profile"
        else:
            title = f"No empirical profile for {self.set_code}"
            message = (
                "Draft Omen is using deterministic fallback scores. Check the "
                "hosted set profile now? Progress will be shown, then the current "
                "pack will be rescored automatically."
            )
            action_label = "Check hosted profile"

        with Vertical(id="missing-ratings-dialog"):
            yield Static(title, id="missing-ratings-title")
            yield Static(message, id="missing-ratings-message")
            with Horizontal(id="missing-ratings-actions"):
                yield Button(
                    action_label,
                    id="download-ratings",
                    variant="primary",
                )
                yield Button("Not now", id="cancel-ratings-download")

    def action_cancel(self) -> None:
        """Dismiss without starting a network request.
        The app preserves the current ratings state and notice.
        """

        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return whether the user explicitly approved the download.
        The app owns the worker and progress state after this screen closes.
        """

        self.dismiss(event.button.id == "download-ratings")


class DraftomenTuiApp(App[None]):
    """Textual app for live Quick Draft recommendations.
    The app can tail a real log or accept fixture lines in tests.
    """

    TITLE = "Draft Omen"

    CSS = """
    Screen {
        layout: vertical;
    }

    #main {
        height: 1fr;
    }

    #pack-panel {
        width: 3fr;
        min-width: 0;
        padding: 0 1;
    }

    #sidebar {
        width: 1fr;
        min-width: 24;
        padding: 0 1;
        border-left: solid $accent;
    }

    #sidebar-scroll {
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
    }

    #pack-title {
        height: 1;
        text-style: bold;
    }

    #pre-draft-readiness {
        height: 1fr;
        content-align: center middle;
        text-align: center;
        text-style: bold;
    }

    #pack-table,
    #build-scroll,
    #focused-card {
        border: blank $surface;
    }

    #pack-table:focus,
    #build-scroll:focus,
    #focused-card:focus {
        border: solid $accent;
    }

    #pack-table {
        height: 1fr;
    }

    #build-scroll {
        height: 1fr;
        overflow-y: auto;
    }

    #build-view {
        height: auto;
    }

    #pool-summary,
    #focused-card,
    #card-image-preview {
        height: auto;
        margin-bottom: 1;
    }

    #card-image-preview {
        content-align: center top;
        margin-top: 1;
        text-align: center;
    }

    #status-bar {
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }

    #ratings-download-panel {
        height: auto;
        display: none;
        background: $panel;
        padding: 0 1;
    }

    #ratings-download-label {
        height: 1;
    }

    #ratings-download-progress {
        height: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("c", "open_config", "Config", show=True),
        Binding("s", "cycle_sort", "Rank", show=True),
        Binding("b", "open_build_view", "Build", show=True),
        Binding("t", "open_backtest_report", "Backtest", show=True),
        Binding("a", "cycle_account", "Account", show=True),
        Binding("p", "rebuild_with_pair_override", "Pair", show=True),
        Binding("m", "toggle_mana_icons", "Mana", show=True),
        Binding("d", "download_ratings", "Data", show=True),
        Binding("r", "retry_card_data", "Retry card data", show=True),
        Binding("up", "navigate_previous_card", "Previous", show=False, priority=True),
        Binding("left", "navigate_previous_card", "Previous", show=False, priority=True),
        Binding("k", "navigate_previous_card", "Previous", show=False, priority=True),
        Binding("down", "navigate_next_card", "Next", show=False, priority=True),
        Binding("right", "navigate_next_card", "Next", show=False, priority=True),
        Binding("j", "navigate_next_card", "Next", show=False, priority=True),
        Binding("pageup", "navigate_page_up", "Page", show=False, priority=True),
        Binding("pagedown", "navigate_page_down", "Page", show=False, priority=True),
        Binding("home", "navigate_home", "Home", show=False, priority=True),
        Binding("end", "navigate_end", "End", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        log_path: PathInput,
        card_database: CardDatabase | None = None,
        set_card_data_loader: SetCardDataLoader | None = None,
        app_dir: PathInput | None = None,
        profile_client: ProfileClient | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        previous_log_path: PathInput | None = None,
        startup_scan: bool = False,
        once: bool = False,
        poll_enabled: bool = True,
        image_preview_enabled: bool | None = None,
        mana_icons_enabled: bool = False,
        card_image_service: CardImageService | None = None,
        visibility_preferences: TuiVisibilityPreferences | None = None,
        splash_enabled: bool | None = None,
    ) -> None:
        super().__init__()
        if card_database is None and set_card_data_loader is None:
            raise ValueError("card_database or set_card_data_loader is required.")
        if card_database is not None and set_card_data_loader is not None:
            raise ValueError(
                "card_database and set_card_data_loader are mutually exclusive."
            )
        self._session_ingestion_may_block = set_card_data_loader is not None

        self.log_path = Path(log_path).expanduser().resolve(strict=False)
        self._preferences_app_dir = app_dir
        if visibility_preferences is None:
            (
                self.visibility_preferences,
                self._preferences_load_warning,
            ) = load_tui_preferences(app_dir=app_dir)
        else:
            self.visibility_preferences = visibility_preferences
            self._preferences_load_warning = None
        if splash_enabled is not None:
            self.visibility_preferences = replace(
                self.visibility_preferences,
                splash_enabled=splash_enabled,
            )
        self._preferences_save_warning: str | None = None
        self._card_database_error: str | None = None
        self._log_processing_started = False
        self._ingestion_lock = Lock()
        self._textual_thread_id: int | None = None
        self._profile_refresh_in_flight: ProfileRefreshRequest | None = None
        self._profile_client = profile_client
        self.session = LiveSession(
            log_path=self.log_path,
            card_database=card_database,
            set_card_data_loader=set_card_data_loader,
            app_dir=app_dir,
            poll_interval=poll_interval,
            previous_log_path=previous_log_path,
            snapshot_publisher=self._publish_session_snapshot,
            event_publisher=self._publish_session_event,
            splash_enabled=self.visibility_preferences.splash_enabled,
            profile_client=profile_client,
        )
        self.startup_scan = startup_scan
        self.once = once
        self.poll_enabled = poll_enabled
        self.poll_interval = poll_interval
        self._card_image_service = card_image_service or CardImageService(
            cache_dir=card_image_cache_dir(app_dir=app_dir),
        )
        self._card_image_preview_enabled = (
            _card_image_preview_enabled(env=os.environ)
            if image_preview_enabled is None
            else image_preview_enabled
        )
        self.mana_icons_enabled = mana_icons_enabled
        self._card_image_paths_by_uri: dict[str, Path] = {}
        self._card_image_failures_by_uri: dict[str, str] = {}
        self._loading_card_image_uris: set[str] = set()
        self._card_image_uris_by_grp_id: dict[int, str] = {}

        self.show_secondary_columns = self.visibility_preferences.secondary_columns
        self.sort_mode = DEFAULT_RANKING_MODE
        self._view_mode = "pack"
        self._visible_column_keys: tuple[str, ...] = ()
        self._active_account_id: str | None = None
        self._active_account_label = "unknown"
        self._event_name: str | None = None
        self._set_code: str | None = None
        self._draft_id: str | None = None
        self._pick_label = "—"
        self._pool_size = 0
        self._pool_grp_ids: tuple[int, ...] = ()
        self._pair_label = "open"
        self._commitment_label = "0% open"
        self._data_source = "unknown"
        self._last_error: str | None = None
        self._session_error: str | None = None
        self._forced_pair: str | None = None
        self._build_pair_label = "—"
        self._build_text = "Build view: no picked cards yet."
        self._build_error: str | None = None
        self._build_action_status: str | None = None
        self._pending_build_success_message: str | None = None
        self._build_spell_sort_mode = "curve"
        self._build_show_details = self.visibility_preferences.build_details
        self._build_focus_cards: tuple[TuiCardQuantityGroup, ...] = ()
        self._build_focused_card_index = 0
        self._build_result: BuildResult | None = None
        self._backtest_text = "Backtest view: complete a draft, then press t."
        self._backtest_error: str | None = None
        self._backtest_action_status: str | None = None
        self._pending_backtest_success_message: str | None = None
        self._open_backtest_when_ready = False
        self._current_pack_event: PackOfferedEvent | None = None
        self._current_pack: ScoredPack | None = None
        self._recommendations_by_grp_id: dict[int, Recommendation] = {}
        self._rating_prompted_sets: set[str] = set()
        self._rating_prompt_open_sets: set[str] = set()
        self._rating_notices_by_set: dict[str, str] = {}
        self._rating_download_requested_sets: set[str] = set()

    @property
    def card_database(self) -> CardDatabase:
        """Return the session's current database for presentation.

        Before selected card metadata is ready, presentation uses one stable
        empty database so the shell and resize handlers remain safe. Once the
        session publishes a database, every lookup uses that current object.
        """

        database = self.session.card_database
        if database is None:
            return _EMPTY_CARD_DATABASE
        return database

    @property
    def card_database_loading(self) -> bool:
        """Return whether selected card metadata is currently loading."""

        return self.session.snapshot.card_data.phase == DataLoadPhase.LOADING

    @property
    def visible_column_keys(self) -> tuple[str, ...]:
        """Return current pack-table columns for tests.
        The value reflects width-based degradation and user toggles.
        """

        return self._visible_column_keys

    @property
    def profile_refresh_in_flight(self) -> ProfileRefreshRequest | None:
        """Return the adapter-owned profile refresh currently running."""

        return self._profile_refresh_in_flight


    @property
    def build_view_text(self) -> str:
        """Return the rendered build view text for pilot tests.
        This mirrors the Static widget content after a build refresh.
        """

        return self._build_text

    @property
    def backtest_view_text(self) -> str:
        """Return the rendered backtest report text for pilot tests.
        This mirrors the Static widget content after a report refresh.
        """

        return self._backtest_text

    def compose(self) -> ComposeResult:
        """Compose the pack table, sidebar, status bar, and key footer.
        Textual's Footer automatically renders the declared keybindings.
        """

        yield Header(show_clock=False)
        with Horizontal(id="main"):
            with Vertical(id="pack-panel"):
                yield Static("Waiting for a Quick Draft pack…", id="pack-title")
                yield Static(
                    "Quick Draft set not detected yet.",
                    id="pre-draft-readiness",
                )
                yield DataTable(id="pack-table")
                with VerticalScroll(id="build-scroll", can_focus=True):
                    yield Static("Build view: no picked cards yet.", id="build-view")
            with Vertical(id="sidebar"):
                with VerticalScroll(id="sidebar-scroll", can_focus=False):
                    yield Static("Pool: no draft yet", id="pool-summary")
                    yield Static("", id="card-image-preview")
                    yield CardDetailsPanel("Focused card: none", id="focused-card")
        with Vertical(id="ratings-download-panel"):
            yield Static("", id="ratings-download-label")
            yield ProgressBar(
                total=4,
                show_eta=False,
                id="ratings-download-progress",
            )
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        """Render the shell, then start log ingestion in its existing worker."""

        self._textual_thread_id = get_ident()
        table = self.query_one("#pack-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.focus()
        self._render_all()

        self._start_log_processing()

    def _start_log_processing(self) -> None:
        """Start polling after the shell is mounted.
        Selected card metadata loads when detection is ingested by this worker.
        """

        if not self.poll_enabled or self._log_processing_started:
            return

        self._log_processing_started = True
        if self.startup_scan:
            self._scan_startup_files_worker(exit_after=self.once)
            return

        self._start_polling()

    def _start_polling(self) -> None:
        """Begin polling only after optional startup recovery has finished.
        The first poll catches lines appended while startup recovery was running.
        """

        self._poll_log_worker(exit_after=self.once)
        if not self.once:
            self.set_interval(self.poll_interval, self._poll_log_worker)

    def on_resize(self, event: events.Resize) -> None:
        """Rebuild the table when width crosses a degradation threshold.
        Secondary columns are hidden first on narrow terminals.
        """

        self._render_all()

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        """Refresh card details when keyboard navigation changes rows.
        Periodic log polls should not be needed before details update.
        """

        if event.data_table.id == "pack-table":
            self._render_focused_card_details()

    def action_open_config(self) -> None:
        """Open the in-app editor for persisted optional TUI elements.
        The callback applies only settings that the user explicitly saves.
        """

        self.push_screen(
            TuiVisibilityScreen(preferences=self.visibility_preferences),
            self._apply_visibility_preferences,
        )

    def _apply_visibility_preferences(
        self,
        preferences: TuiVisibilityPreferences | None,
    ) -> None:
        if preferences is None:
            return

        splash_changed = (
            self.visibility_preferences.splash_enabled != preferences.splash_enabled
        )
        had_build_result = self._build_result is not None
        self.visibility_preferences = preferences
        self.show_secondary_columns = preferences.secondary_columns
        self._build_show_details = preferences.build_details
        self._save_visibility_preferences()
        self.session.dispatch(
            command=ChangeSplashPreference(enabled=preferences.splash_enabled),
        )
        if had_build_result and splash_changed:
            self._request_build_view(success_message=None)
        elif self._build_result is not None:
            self._refresh_build_text_from_result()
        self._render_all()

    def _save_visibility_preferences(self) -> None:
        self._preferences_save_warning = save_tui_preferences(
            preferences=self.visibility_preferences,
            app_dir=self._preferences_app_dir,
        )

    def action_toggle_mana_icons(self) -> None:
        """Toggle opt-in Mana font icon rendering.
        Terminals need Andrew Gioia's Mana font installed before enabling it.
        """

        self.mana_icons_enabled = not self.mana_icons_enabled
        if self._view_mode == "build":
            self._refresh_build_text_from_result()

        self._render_all()

    def action_download_ratings(self) -> None:
        """Offer or retry a hosted profile refresh for the active draft set."""

        set_code = self._set_code
        if set_code is None:
            self._last_error = (
                "No active draft set is available for a hosted profile refresh."
            )
            self._render_all()
            return

        ratings = self.session.snapshot.ratings
        if ratings.phase == DataLoadPhase.LOADING:
            return

        self._show_missing_ratings_prompt(
            set_code=set_code,
            force=True,
            refresh=ratings.phase == DataLoadPhase.READY,
        )

    def action_retry_card_data(self) -> None:
        """Retry the current recoverable card-data error in the session worker.
        The shared session decides whether network access remains allowed.
        """

        error = next(
            (
                candidate
                for candidate in self.session.snapshot.errors
                if candidate.operation == OperationKind.CARD_DATA
                and candidate.recoverable
            ),
            None,
        )
        if error is None:
            self._last_error = "No recoverable card-data error to retry."
            self._render_all()
            return

        self._last_error = None
        self._dispatch_session_command_worker(
            command=RetryError(error_id=error.error_id)
        )

    def action_cycle_sort(self) -> None:
        """Cycle pack ranking or build spell grouping.
        Backtest reports are rebuilt with the selected recommendation ranking.
        """

        if self._view_mode == "build":
            index = BUILD_SPELL_SORT_MODES.index(self._build_spell_sort_mode)
            self._build_spell_sort_mode = BUILD_SPELL_SORT_MODES[
                (index + 1) % len(BUILD_SPELL_SORT_MODES)
            ]
            self._refresh_build_text_from_result()
        else:
            index = SORT_MODES.index(self.sort_mode)
            self.sort_mode = SORT_MODES[(index + 1) % len(SORT_MODES)]
            self.session.dispatch(
                command=ChangeRanking(ranking_mode=self.sort_mode),
            )
            if self._view_mode == "backtest":
                self._request_backtest_view(
                    success_message=(
                        f"rebuilt {ranking_label(ranking_mode=self.sort_mode)} "
                        "recommendation comparison"
                    ),
                    open_when_ready=False,
                )

        self._render_all()

    def action_open_build_view(self) -> None:
        """Request a build for the current pool and show the shared result."""

        current_build = self.session.snapshot.build
        if (
            self._view_mode == "build"
            and self._build_error is None
            and current_build is not None
            and current_build.pair_override == self._forced_pair
        ):
            self.query_one("#build-scroll", VerticalScroll).scroll_home(animate=False)
            self._last_error = None
            self._build_action_status = "no build needed — current pool already shown"
            self._render_all()
            return

        self._request_build_view(success_message="rebuilt current pool")
        self._render_all()

    def action_open_backtest_report(self) -> None:
        """Request the shared persisted-pick recommendation comparison."""

        self._build_action_status = None
        self._request_backtest_view(
            success_message=(
                f"rebuilt {ranking_label(ranking_mode=self.sort_mode)} "
                "recommendation comparison"
            ),
            open_when_ready=True,
        )
        self._render_all()

    def action_navigate_previous_card(self) -> None:
        """Move to the previous card in the active card list.
        Left, Up, and k share this action for predictable keyboard browsing.
        """

        if self._view_mode == "build":
            self._move_build_card_cursor(delta=-1)
            return

        if self._view_mode == "backtest":
            self.query_one("#build-scroll", VerticalScroll).scroll_page_up(animate=False)
            return

        self._move_pack_cursor(delta=-1)

    def action_navigate_next_card(self) -> None:
        """Move to the next card in the active card list.
        Right, Down, and j share this action for predictable keyboard browsing.
        """

        if self._view_mode == "build":
            self._move_build_card_cursor(delta=1)
            return

        if self._view_mode == "backtest":
            self.query_one("#build-scroll", VerticalScroll).scroll_page_down(animate=False)
            return

        self._move_pack_cursor(delta=1)

    def action_navigate_page_up(self) -> None:
        """Page up in the current keyboard-navigation context.
        Pack view moves the card cursor; build view scrolls the deck sheet.
        """

        if self._view_mode in {"build", "backtest"}:
            self.query_one("#build-scroll", VerticalScroll).scroll_page_up(animate=False)
            return

        self._move_pack_cursor(delta=-self._pack_cursor_page_size())

    def action_navigate_page_down(self) -> None:
        """Page down in the current keyboard-navigation context.
        Pack view moves the card cursor; build view scrolls the deck sheet.
        """

        if self._view_mode in {"build", "backtest"}:
            self.query_one("#build-scroll", VerticalScroll).scroll_page_down(animate=False)
            return

        self._move_pack_cursor(delta=self._pack_cursor_page_size())

    def action_navigate_home(self) -> None:
        """Jump to the first pack card or the top of the build view.
        This keeps Home useful in both major TUI modes.
        """

        if self._view_mode in {"build", "backtest"}:
            self.query_one("#build-scroll", VerticalScroll).scroll_home(animate=False)
            return

        self._move_pack_cursor_to(row=0)

    def action_navigate_end(self) -> None:
        """Jump to the last pack card or the bottom of the build view.
        This keeps End useful in both major TUI modes.
        """

        if self._view_mode in {"build", "backtest"}:
            self.query_one("#build-scroll", VerticalScroll).scroll_end(animate=False)
            return

        table = self.query_one("#pack-table", DataTable)
        self._move_pack_cursor_to(row=table.row_count - 1)

    def action_cycle_account(self) -> None:
        """Cycle known accounts and use the latest recovered draft when available.
        Multiple saved drafts for one account must not create duplicate stops.
        """

        accounts = self.session.known_accounts()
        if not accounts:
            self._record_error("no known accounts to switch to")
            return

        ordered_account_ids = tuple(account.account_id for account in accounts)
        if self._active_account_id in ordered_account_ids:
            index = (ordered_account_ids.index(self._active_account_id) + 1) % len(
                ordered_account_ids
            )
        else:
            index = 0

        account_id = ordered_account_ids[index]
        command = ChooseAccount(account_id=account_id)
        self._dispatch_session_command_worker(command)

    def action_rebuild_with_pair_override(self) -> None:
        """Request a shared build after cycling the TUI pair override."""

        self._forced_pair = self._next_forced_pair()
        self._request_build_view(
            success_message=f"rebuilt with forced pair {self._forced_pair}",
        )
        self._render_all()

    def process_lines(
        self,
        *,
        lines: Iterable[str],
        include_pre_draft_detection: bool = True,
    ) -> None:
        """Process complete Player.log lines and refresh the TUI.
        Tests call this directly to simulate a live fixture stream.
        """

        if self.card_database_loading or self._card_database_error is not None:
            return

        line_batch = tuple(lines)
        if (
            self.is_running
            and line_batch
            and self._session_ingestion_may_block
        ):
            self._process_lines_worker(
                lines=line_batch,
                include_pre_draft_detection=include_pre_draft_detection,
            )
            return

        try:
            with self._ingestion_lock:
                self.session.process_lines(
                    lines=line_batch,
                    include_pre_draft_detection=include_pre_draft_detection,
                )
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self._record_error(str(error))
            return

        self._render_all()

    @work(thread=True, exclusive=True, group="session-ingestion")
    def _process_lines_worker(
        self,
        *,
        lines: tuple[str, ...],
        include_pre_draft_detection: bool,
    ) -> None:
        """Run shared ingestion and ratings work away from Textual's thread.
        Session publications marshal immutable state back to the adapter.
        """

        worker = get_current_worker()
        try:
            with self._ingestion_lock:
                if worker.is_cancelled:
                    return
                self.session.process_lines(
                    lines=lines,
                    include_pre_draft_detection=include_pre_draft_detection,
                )
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.call_from_thread(self._record_error, str(error))

    @work(thread=True, exclusive=True, group="session-ingestion")
    def _poll_log_worker(self, *, exit_after: bool = False) -> None:
        """Schedule one shared-session polling cycle in a Textual worker.
        Session publications marshal state changes onto the Textual thread.
        """

        worker = get_current_worker()
        try:
            with self._ingestion_lock:
                if worker.is_cancelled:
                    return
                self.session.poll_once()
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.call_from_thread(self._record_error, str(error))
            if exit_after:
                self.call_from_thread(self.exit)
            return

        if exit_after:
            self.call_from_thread(self.exit)

    @work(thread=True, exclusive=True, group="session-ingestion")
    def _scan_startup_files_worker(self, *, exit_after: bool = False) -> None:
        """Schedule shared startup recovery in a Textual worker.
        Recovery detection is retained so selected card data loads in this worker.
        """

        worker = get_current_worker()
        try:
            with self._ingestion_lock:
                if worker.is_cancelled:
                    return
                self.session.scan_startup_files(
                    include_previous=True,
                    include_pre_draft_detection=True,
                )
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.call_from_thread(self._record_error, str(error))
            if exit_after:
                self.call_from_thread(self.exit)
            return

        if exit_after:
            self.call_from_thread(self.exit)
            return

        self.call_from_thread(self._start_polling)

    @work(thread=True, group="session-commands")
    def _dispatch_session_command_worker(
        self,
        command: LiveSessionCommand,
    ) -> None:
        """Dispatch a potentially blocking shared command in a worker.
        Session progress and results return through immutable publications.
        """

        worker = get_current_worker()
        if worker.is_cancelled:
            return

        try:
            self.session.dispatch(command=command)
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            self.call_from_thread(self._record_error, str(error))
            if isinstance(command, RequestRatingsDownload):
                try:
                    self.call_from_thread(
                        self._schedule_or_finish_ratings_download_request,
                        command.set_code,
                    )
                except Exception:  # pragma: no cover - app may be shutting down.
                    pass
            return
        if isinstance(command, RequestRatingsDownload):
            try:
                self.call_from_thread(
                    self._schedule_or_finish_ratings_download_request,
                    command.set_code,
                )
            except Exception:  # pragma: no cover - app may be shutting down.
                pass
            return
        try:
            self.call_from_thread(self._schedule_profile_refresh)
        except Exception:  # pragma: no cover - app may be shutting down.
            pass

    def _schedule_or_finish_ratings_download_request(self, set_code: str) -> None:
        """Schedule an accepted ratings request or finish a shared rejection."""

        request = self.session.profile_refresh_request()
        in_flight = self._profile_refresh_in_flight
        if request is None:
            if in_flight is None or in_flight.set_code != set_code:
                self._finish_ratings_download_request(set_code)
            return
        if request.set_code != set_code:
            self._finish_ratings_download_request(set_code)
            if in_flight is not None:
                return
        if in_flight is None:
            self._schedule_profile_refresh()

    def _finish_ratings_download_request(self, set_code: str) -> None:
        """Finish a rejected ratings command without leaving stale progress."""

        if set_code not in self._rating_download_requested_sets:
            return

        self._rating_download_requested_sets.discard(set_code)
        snapshot = self.session.snapshot
        self._rating_notices_by_set[set_code] = (
            self._ratings_refresh_unavailable_notice(
                set_code=set_code,
                snapshot=snapshot,
            )
        )
        self._render_all()

    def _schedule_profile_refresh(self) -> None:
        """Start the one pending profile refresh without blocking Textual."""

        if self._profile_client is None or not self.is_running:
            return
        request = self.session.profile_refresh_request()
        if request is None or self._profile_refresh_in_flight is not None:
            return

        self._profile_refresh_in_flight = request
        self._rating_notices_by_set.setdefault(
            request.set_code,
            f"Refreshing hosted profile for {request.set_code}…",
        )
        self._render_all()
        self._refresh_profile_worker(request=request)

    @work(thread=True, group="profile-refresh")
    def _refresh_profile_worker(self, *, request: ProfileRefreshRequest) -> None:
        """Fetch one profile in a worker and publish it through LiveSession."""

        worker = get_current_worker()
        try:
            if worker.is_cancelled or self._profile_client is None:
                return
            if self.session.profile_refresh_request() != request:
                return
            result = self._profile_client.refresh(
                set_code=request.set_code,
                event_format=request.event_format,
                force=request.force,
            )
            if worker.is_cancelled:
                return
            if not isinstance(result, ProfileRefreshResult):
                raise TypeError("ProfileClient.refresh returned an invalid result.")
            self.session.complete_profile_refresh(request=request, result=result)
        except Exception as error:  # pragma: no cover - defensive UI boundary.
            if not worker.is_cancelled:
                self.session.fail_profile_refresh(
                    request=request,
                    error_message=str(error),
                )
        finally:
            if self.is_running:
                try:
                    self.call_from_thread(self._profile_refresh_finished, request)
                except Exception:  # pragma: no cover - app may be shutting down.
                    self._profile_refresh_in_flight = None
            else:
                self._profile_refresh_in_flight = None

    def _profile_refresh_finished(self, request: ProfileRefreshRequest) -> None:
        """Release the in-flight guard and pick up a newer active-set request."""

        if self._profile_refresh_in_flight != request:
            return
        self._profile_refresh_in_flight = None
        self._apply_session_snapshot(self.session.snapshot)
        self._schedule_profile_refresh()

    def _session_publication_is_allowed(self) -> bool:
        """Allow presentation updates only while Textual is running."""
        return self.is_running

    def _publish_session_snapshot(self, snapshot: LiveSessionSnapshot) -> None:
        """Marshal one immutable shared snapshot onto Textual's thread.
        Pack projections travel with the snapshot so account state stays coherent.
        """

        if not self._session_publication_is_allowed():
            return
        if (
            self.is_running
            and self._textual_thread_id is not None
            and get_ident() != self._textual_thread_id
        ):
            self.post_message(_SessionSnapshotMessage(snapshot))
            return

        self._apply_session_snapshot(snapshot)

    def _publish_session_event(self, published: LiveSessionEvent) -> None:
        """Marshal an ordered domain event onto Textual's presentation thread.
        Events drive view transitions without re-owning application state.
        """

        if not self._session_publication_is_allowed():
            return
        if (
            self.is_running
            and self._textual_thread_id is not None
            and get_ident() != self._textual_thread_id
        ):
            self.call_from_thread(self._apply_session_event, published)
            return

        self._apply_session_event(published)

    def on__session_snapshot_message(
        self,
        message: _SessionSnapshotMessage,
    ) -> None:
        """Apply a worker-published snapshot on Textual's thread."""
        if message.snapshot is not self.session.snapshot:
            return
        self._apply_session_snapshot(message.snapshot)

    def _apply_session_snapshot(
        self,
        snapshot: LiveSessionSnapshot,
    ) -> None:
        if not self._session_publication_is_allowed():
            return
        previous_identity = (self._active_account_id, self._draft_id)
        account = snapshot.active_account
        draft = snapshot.draft
        self._active_account_id = None if account is None else account.account_id
        self._active_account_label = self._account_label(
            account_id=self._active_account_id,
            snapshot=snapshot,
        )
        self._event_name = None if draft is None else draft.event_name
        self._set_code = snapshot.ratings.set_code
        if self._set_code is None and draft is not None:
            self._set_code = draft.set_code
        self._draft_id = None if draft is None else draft.draft_id
        self._current_pack_event = snapshot.current_pack_event
        self._current_pack = snapshot.current_scored_pack
        self._recommendations_by_grp_id = {
            item.card.grp_id: item for item in snapshot.recommendations.cards
        }
        if snapshot.status.phase == ApplicationPhase.DRAFT_COMPLETE:
            self._pick_label = "complete"
        elif snapshot.current_pack_event is not None:
            self._pick_label = (
                f"P{snapshot.current_pack_event.pack_number + 1}"
                f"P{snapshot.current_pack_event.pick_number + 1}"
            )
        elif draft is not None:
            self._pick_label = "recovered"
        elif snapshot.ratings.set_code is not None:
            self._pick_label = "preparing"
        else:
            self._pick_label = "—"
        self._pool_grp_ids = self._snapshot_pool_grp_ids(snapshot=snapshot)
        self._pool_size = snapshot.pool.total_cards
        self._pair_label = snapshot.pool.inferred_pair or "open"
        self._commitment_label = _commitment_label(
            commitment=snapshot.pool.commitment,
        )
        self.sort_mode = snapshot.recommendations.ranking_mode
        self._data_source = self._session_data_source(snapshot=snapshot)
        self._apply_card_data_state(snapshot=snapshot)
        self._apply_ratings_state(snapshot=snapshot)
        general_errors = tuple(
            error
            for error in snapshot.errors
            if error.operation not in {OperationKind.BUILD, OperationKind.BACKTEST}
        )
        self._session_error = (
            None if not general_errors else general_errors[-1].message
        )

        identity = (self._active_account_id, self._draft_id)
        if identity != previous_identity:
            self._adopt_changed_session_identity(snapshot=snapshot)
        self._apply_secondary_results(snapshot=snapshot)
        if snapshot.card_data.phase == DataLoadPhase.READY:
            self._start_log_processing()

        self._schedule_profile_refresh()
        self._render_all()

    def _apply_session_event(self, published: LiveSessionEvent) -> None:
        if not self._session_publication_is_allowed():
            return
        event = published.event
        if isinstance(
            event,
            (
                QuickDraftDetectedEvent,
                DraftStartedEvent,
                PackOfferedEvent,
                PickMadeEvent,
                DraftCompletedEvent,
            ),
        ):
            self._event_name = event.event_name
            self._set_code = event.set_code

        if isinstance(event, (QuickDraftDetectedEvent, DraftStartedEvent)):
            self._reset_secondary_view_state()
            self._view_mode = "pack"
            self._pick_label = (
                "preparing"
                if isinstance(event, QuickDraftDetectedEvent)
                else "waiting"
            )
        elif isinstance(event, PackOfferedEvent):
            self._view_mode = "pack"
            self._pick_label = f"P{event.pack_number + 1}P{event.pick_number + 1}"
            self._build_action_status = None
            self._backtest_action_status = None
            self._build_result = None
            self._backtest_error = None
            self._clear_build_render_state()
        elif isinstance(event, PickMadeEvent):
            self._pick_label = (
                f"P{event.pack_number + 1}P{event.pick_number + 1} picked"
            )
            self._build_action_status = None
            self._backtest_action_status = None
            self._build_result = None
            self._backtest_error = None
            self._clear_build_render_state()
        elif isinstance(event, DraftCompletedEvent):
            self._pick_label = "complete"
            self._build_action_status = None
            self._backtest_action_status = None
            self._request_build_view(success_message=None)

        self._render_all()

    def _apply_secondary_results(self, *, snapshot: LiveSessionSnapshot) -> None:
        progress = snapshot.progress
        build_in_progress = (
            progress is not None and progress.operation == OperationKind.BUILD
        )
        backtest_in_progress = (
            progress is not None and progress.operation == OperationKind.BACKTEST
        )

        if not build_in_progress:
            build_error = _operation_error(
                snapshot=snapshot,
                operation=OperationKind.BUILD,
            )
            if build_error is not None:
                message = build_error.message.removeprefix("Deck build failed: ")
                self._adopt_build_error(snapshot=snapshot, message=message)
            elif snapshot.build is not None:
                self._adopt_build_result(result=snapshot.build)

        if not backtest_in_progress:
            backtest_error = _operation_error(
                snapshot=snapshot,
                operation=OperationKind.BACKTEST,
            )
            if backtest_error is not None:
                message = backtest_error.message.removeprefix("Backtest failed: ")
                self._backtest_error = message
                self._backtest_text = f"Backtest view unavailable: {message}"
                self._backtest_action_status = f"cannot backtest — {message}"
                self._pending_backtest_success_message = None
                self._open_backtest_when_ready = False
            elif snapshot.backtest is not None:
                self._adopt_backtest_result(result=snapshot.backtest)

    def _apply_card_data_state(self, *, snapshot: LiveSessionSnapshot) -> None:
        if snapshot.card_data.phase != DataLoadPhase.FAILED:
            self._card_database_error = None
            return

        error = next(
            (
                candidate
                for candidate in snapshot.errors
                if candidate.operation == OperationKind.CARD_DATA
            ),
            None,
        )
        detail = snapshot.card_data.message if error is None else error.message
        detail = detail.removeprefix("Card metadata failed to load: ").removesuffix(
            "."
        )
        self._card_database_error = (
            f"{detail}. Press r to retry card metadata. Network repair is "
            "available only before draft start; after draft start, retries "
            "are local-only."
        )

    def _apply_ratings_state(self, *, snapshot: LiveSessionSnapshot) -> None:
        ratings = snapshot.ratings
        set_code = ratings.set_code
        if set_code is None:
            return

        progress = snapshot.progress
        profile = snapshot.set_profile
        if profile.phase == DataLoadPhase.LOADING or ratings.phase == DataLoadPhase.LOADING:
            if progress is not None and progress.operation == OperationKind.RATINGS:
                completed = 0 if progress.completed is None else progress.completed
                total = "?" if progress.total is None else str(progress.total)
                self._rating_notices_by_set[set_code] = (
                    f"Refreshing hosted profile for {set_code} ({completed}/{total})"
                )
            else:
                self._rating_notices_by_set[set_code] = (
                    f"Refreshing hosted profile for {set_code}…"
                )
            return
        if profile.phase == DataLoadPhase.FAILED:
            if ratings.phase == DataLoadPhase.READY:
                self._rating_notices_by_set[set_code] = (
                    f"Hosted profile refresh failed for {set_code}; cached ratings "
                    "remain active. Press d to retry."
                )
            else:
                self._rating_notices_by_set[set_code] = (
                    f"Hosted profile refresh failed for {set_code}; deterministic "
                    "fallback scores remain active. Press d to retry."
                )
            return

        if ratings.phase in {DataLoadPhase.MISSING, DataLoadPhase.UNAVAILABLE}:
            self._rating_notices_by_set[set_code] = (
                f"No empirical profile for {set_code}; deterministic fallback scores "
                "are active. Choose Check hosted profile, or press d later."
            )
            if ratings.phase == DataLoadPhase.MISSING:
                self._show_missing_ratings_prompt(set_code=set_code)
            return

        if ratings.phase == DataLoadPhase.FAILED:
            self._rating_notices_by_set[set_code] = (
                f"Ratings unavailable for {set_code}; deterministic fallback scores "
                "are active. Press d to retry."
            )
            return

        if ratings.phase != DataLoadPhase.READY:
            return

        if set_code in self._rating_download_requested_sets:
            self._rating_notices_by_set[set_code] = self._ratings_ready_notice(
                snapshot=snapshot,
            )
            return

        self._rating_notices_by_set.pop(set_code, None)

    def _ratings_ready_notice(self, *, snapshot: LiveSessionSnapshot) -> str:
        ratings = snapshot.ratings
        set_code = ratings.set_code or "unknown set"
        profile = snapshot.set_profile
        if profile.phase == DataLoadPhase.FAILED:
            return (
                f"Hosted profile refresh failed for {set_code}; cached ratings "
                "remain active. Press d to retry."
            )

        if profile.refresh_outcome == "updated":
            prefix = f"Hosted profile updated for {set_code}; scores recalculated."
        elif profile.refresh_outcome == "unchanged":
            prefix = (
                f"Hosted profile unchanged for {set_code}; cached ratings remain "
                "active."
            )
        elif profile.refresh_outcome == "cached":
            prefix = f"Hosted profile loaded from cache for {set_code}."
        else:
            prefix = f"Hosted profile ready for {set_code}; scores recalculated."

        total_cards = ratings.total_cards
        rated_cards = ratings.rated_cards
        if total_cards is None or rated_cards is None:
            return f"{prefix} Future scores will use it."
        if rated_cards == total_cards:
            return f"{prefix} All {total_cards} offered cards have usable ratings."

        return (
            f"{prefix} {rated_cards}/{total_cards} offered cards have usable "
            "ratings; deterministic fallback scores remain where profile evidence "
            "is unavailable or thin."
        )

    def _adopt_changed_session_identity(
        self,
        *,
        snapshot: LiveSessionSnapshot,
    ) -> None:
        self._reset_secondary_view_state()
        if self._current_pack_event is not None:
            self._view_mode = "pack"
            return
        if snapshot.draft is None or snapshot.pool.total_cards == 0:
            self._view_mode = "pack"
            return

        self._request_build_view(success_message=None)

    def _reset_secondary_view_state(self) -> None:
        self._forced_pair = None
        self._build_pair_label = "—"
        self._build_text = "Build view: no picked cards yet."
        self._build_error = None
        self._build_action_status = None
        self._pending_build_success_message = None
        self._build_spell_sort_mode = "curve"
        self._build_show_details = self.visibility_preferences.build_details
        self._build_result = None
        self._backtest_text = "Backtest view: complete a draft, then press t."
        self._backtest_error = None
        self._backtest_action_status = None
        self._pending_backtest_success_message = None
        self._open_backtest_when_ready = False
        self._clear_build_render_state()

    def _snapshot_pool_grp_ids(
        self,
        *,
        snapshot: LiveSessionSnapshot,
    ) -> tuple[int, ...]:
        return tuple(
            pool_card.card.grp_id
            for pool_card in snapshot.pool.cards
            for _ in range(pool_card.quantity)
        )

    def _session_data_source(self, *, snapshot: LiveSessionSnapshot) -> str:
        source = snapshot.recommendations.source_summary
        if source is None:
            return "unknown"
        profile = snapshot.set_profile
        ratings = snapshot.ratings
        if (
            profile.phase == DataLoadPhase.LOADING
            or ratings.phase == DataLoadPhase.LOADING
        ):
            return f"{source} (refreshing hosted profile)"
        if profile.phase == DataLoadPhase.FAILED:
            if ratings.phase == DataLoadPhase.READY:
                return f"{source} (hosted refresh failed; cached ratings active)"
            return f"{source} (hosted refresh failed; deterministic fallback)"
        if ratings.phase == DataLoadPhase.FAILED:
            return f"{source} (ratings unavailable; deterministic fallback)"
        if ratings.phase in {DataLoadPhase.MISSING, DataLoadPhase.UNAVAILABLE}:
            return f"{source} (no empirical profile; deterministic fallback)"
        if (
            ratings.phase == DataLoadPhase.READY
            and ratings.total_cards
            and ratings.rated_cards == 0
        ):
            return (
                f"{source} (no empirical ratings for offered cards; "
                "deterministic fallback)"
            )

        return source
    def _show_missing_ratings_prompt(
        self,
        *,
        set_code: str,
        force: bool = False,
        refresh: bool = False,
    ) -> None:
        if not self.is_running or set_code in self._rating_prompt_open_sets:
            return

        if not force and set_code in self._rating_prompted_sets:
            return

        self._rating_prompted_sets.add(set_code)
        self._rating_prompt_open_sets.add(set_code)
        self._render_all()
        self.push_screen(
            MissingRatingsScreen(set_code=set_code, refresh=refresh),
            lambda approved: self._handle_ratings_download_choice(
                set_code=set_code,
                approved=approved,
                refresh=refresh,
            ),
        )

    def _handle_ratings_download_choice(
        self,
        *,
        set_code: str,
        approved: bool,
        refresh: bool = False,
    ) -> None:
        self._rating_prompt_open_sets.discard(set_code)
        if approved:
            self._start_ratings_load(set_code=set_code)
            return

        snapshot = self.session.snapshot
        if refresh and snapshot.ratings.phase == DataLoadPhase.READY:
            if snapshot.set_profile.phase == DataLoadPhase.FAILED:
                self._rating_notices_by_set[set_code] = (
                    f"Hosted profile refresh failed for {set_code}; refresh "
                    "cancelled. Cached ratings remain active. Press d to retry."
                )
            else:
                self._rating_notices_by_set[set_code] = (
                    self._ratings_refresh_unavailable_notice(
                        set_code=set_code,
                        snapshot=snapshot,
                        cancelled=True,
                    )
                )
        else:
            self._rating_notices_by_set[set_code] = (
                f"No empirical profile for {set_code}; deterministic fallback scores "
                "remain active. Press d to check the hosted profile."
            )
        self._render_all()

    def _ratings_refresh_unavailable_notice(
        self,
        *,
        set_code: str,
        snapshot: LiveSessionSnapshot,
        cancelled: bool = False,
    ) -> str:
        action = "cancelled" if cancelled else "unavailable"
        if snapshot.ratings.phase == DataLoadPhase.READY:
            return (
                f"Hosted profile refresh {action} for {set_code}; existing cached "
                "ratings remain active. Press d to refresh."
            )
        return (
            f"Hosted profile refresh {action} for {set_code}; deterministic fallback "
            "scores remain active. Press d to check the hosted profile."
        )

    def _start_ratings_load(self, *, set_code: str) -> None:
        if self.session.snapshot.ratings.phase == DataLoadPhase.LOADING:
            return

        self._rating_download_requested_sets.add(set_code)
        self._rating_notices_by_set[set_code] = (
            f"Refreshing hosted profile for {set_code}…"
        )
        self._render_all()
        self._dispatch_session_command_worker(
            RequestRatingsDownload(set_code=set_code),
        )


    def _render_all(self) -> None:
        if not self.is_mounted:
            return
        try:
            self.query_one("#sidebar", Vertical)
        except NoMatches:
            return

        self._update_responsive_visibility()
        self._render_pack_title()
        self._render_pre_draft_readiness()
        self._render_pack_table()
        self._render_sidebar()
        self._ensure_visible_focus()
        self._render_ratings_download_status()
        self._render_status_bar()

    def _render_ratings_download_status(self) -> None:
        panel = self.query_one("#ratings-download-panel", Vertical)
        label = self.query_one("#ratings-download-label", Static)
        progress_bar = self.query_one("#ratings-download-progress", ProgressBar)
        set_code = self._set_code
        notice = (
            None
            if set_code is None
            else self._rating_notices_by_set.get(set_code)
        )
        panel.display = notice is not None
        if notice is None:
            return

        label.update(notice)
        snapshot = self.session.snapshot
        pending_request = self.session.profile_refresh_request()
        in_flight = self._profile_refresh_in_flight
        downloading = (
            set_code is not None
            and snapshot.ratings.set_code == set_code
            and (
                snapshot.ratings.phase == DataLoadPhase.LOADING
                or snapshot.set_profile.phase == DataLoadPhase.LOADING
                or (
                    pending_request is not None
                    and pending_request.set_code == set_code
                )
                or (
                    in_flight is not None
                    and in_flight.set_code == set_code
                )
            )
        )
        progress_bar.display = downloading
        if not downloading or set_code is None:
            return

        progress = snapshot.progress
        if progress is None or progress.operation != OperationKind.RATINGS:
            return
        progress_bar.update(
            total=1 if progress.total is None else progress.total,
            progress=0 if progress.completed is None else progress.completed,
        )

    def _update_responsive_visibility(self) -> None:
        sidebar = self.query_one("#sidebar", Vertical)
        sidebar.display = (
            self.size.width >= SIDEBAR_MIN_WIDTH
            and self._sidebar_has_enabled_content()
        )

    def _sidebar_has_enabled_content(self) -> bool:
        if self._view_mode not in {"build", "backtest"} and any(
            (
                self.visibility_preferences.pool_metadata,
                self.visibility_preferences.pool_color_distribution,
                self.visibility_preferences.pool_mana_curve,
            )
        ):
            return True

        return (
            self.visibility_preferences.focused_card_details
            or self._card_image_preview_is_enabled()
        )

    def _card_image_preview_is_enabled(self) -> bool:
        mode = self.visibility_preferences.card_image_preview
        if mode == "hide":
            return False
        if mode == "show":
            return TgpImage is not None

        return self._card_image_preview_enabled

    def _render_pack_title(self) -> None:
        title = self.query_one("#pack-title", Static)
        if self.card_database_loading:
            title.update("Loading card metadata…")
            return

        if self._card_database_error is not None:
            title.update("Card metadata unavailable — check the status bar")
            return

        if self._view_mode == "build":
            if self._build_error is not None and self._build_pair_label == "—":
                title.update("Build view unavailable — metadata or playable count issue")
                return

            build_pair_label = _format_pair_label(
                pair=self._build_pair_label,
                mana_icons_enabled=self.mana_icons_enabled,
            )
            override = "automatic"
            if self._forced_pair is not None:
                forced_pair = _format_pair_label(
                    pair=self._forced_pair,
                    mana_icons_enabled=self.mana_icons_enabled,
                )
                override = f"forced {forced_pair}"

            detail = "details" if self._build_show_details else "compact"
            title.update(
                f"Build view — pair {build_pair_label} ({override}); "
                f"spells {self._build_spell_sort_mode}; {detail}; "
                "scroll ↑/↓ PgUp/PgDn"
            )
            return

        if self._view_mode == "backtest":
            if self._backtest_error is not None:
                title.update("Backtest view unavailable — saved draft state issue")
            else:
                title.update(
                    "Backtest view — "
                    f"{ranking_label(ranking_mode=self.sort_mode)} "
                    "recommendations vs actual picks; scroll ↑/↓ PgUp/PgDn"
                )
            return

        if self._current_pack_event is None:
            if self._pick_label == "complete":
                title.update("Draft complete")
            elif self._set_code is not None:
                title.update(
                    "Quick Draft detected — "
                    f"{format_set_label(set_code=self._set_code)}; "
                    "waiting for the first pack…"
                )
            else:
                title.update("Waiting for a Quick Draft pack…")
            return

        event = self._current_pack_event
        title.update(
            "Available cards — Pack "
            f"{event.pack_number + 1} "
            "Pick "
            f"{event.pick_number + 1} "
            f"— ranked by {SORT_LABELS[self.sort_mode]}"
        )

    def _render_pre_draft_readiness(self) -> None:
        readiness = self.query_one("#pre-draft-readiness", Static)
        readiness.display = self._show_pre_draft_readiness()
        if not readiness.display:
            return
        status = self.session.snapshot.status
        if status.setup_guidance:
            readiness.update(status.message)
            return

        set_code = self._set_code
        if set_code is None:
            readiness.update(
                "Quick Draft set not detected yet.\n"
                "Waiting for Arena to report a Quick Draft entry."
            )
            return

        set_label = format_set_label(set_code=set_code)
        prefix = f"Hosted profile for {set_label}:"
        snapshot = self.session.snapshot
        ratings = snapshot.ratings
        profile = snapshot.set_profile
        if (
            profile.phase == DataLoadPhase.LOADING
            or ratings.phase == DataLoadPhase.LOADING
        ):
            readiness.update(f"{prefix} Refreshing…")
            return

        if profile.phase == DataLoadPhase.FAILED:
            if ratings.phase == DataLoadPhase.READY:
                readiness.update(
                    f"{prefix} Refresh failed — cached ratings active; "
                    "press d to retry"
                )
            else:
                readiness.update(
                    f"{prefix} Refresh failed — deterministic fallback active; "
                    "press d to retry"
                )
            return

        if ratings.phase in {DataLoadPhase.MISSING, DataLoadPhase.UNAVAILABLE}:
            readiness.update(
                f"{prefix} No empirical profile — deterministic fallback active; "
                "press d to check"
            )
            return

        if ratings.phase == DataLoadPhase.READY:
            outcome = profile.refresh_outcome
            if outcome == "updated":
                readiness.update(f"{prefix} Updated")
            elif outcome == "unchanged":
                readiness.update(f"{prefix} Current")
            else:
                readiness.update(f"{prefix} Ready")
            return

        readiness.update(f"{prefix} Not checked")

    def _show_pre_draft_readiness(self) -> bool:
        return (
            self._view_mode == "pack"
            and self._current_pack_event is None
            and self._pick_label != "complete"
        )

    def _render_pack_table(self) -> None:
        table = self.query_one("#pack-table", DataTable)
        build_scroll = self.query_one("#build-scroll", VerticalScroll)
        build_view = self.query_one("#build-view", Static)
        table.loading = self.card_database_loading
        table.display = (
            self._view_mode == "pack" and not self._show_pre_draft_readiness()
        )
        build_scroll.display = self._view_mode in {"build", "backtest"}
        build_view.update(self._active_text_view())
        if self._view_mode in {"build", "backtest"}:
            self._visible_column_keys = ()
            return

        previous_row_key, previous_row_index = self._capture_table_cursor(table=table)
        table.clear(columns=True)
        column_keys = self._column_keys_for_width()
        self._visible_column_keys = column_keys
        for column_key in column_keys:
            table.add_column(
                COLUMN_LABELS[column_key],
                width=COLUMN_WIDTHS[column_key],
                key=column_key,
            )

        if self._current_pack is None:
            return

        for rank, scored_card in enumerate(self._sorted_cards(), start=1):
            row = _row_cells(
                rank=rank,
                scored_card=scored_card,
                column_keys=column_keys,
                mana_icons_enabled=self.mana_icons_enabled,
            )
            table.add_row(
                *row,
                key=f"{rank}-{scored_card.card.grp_id}-{scored_card.original_index}",
            )

        self._restore_table_cursor(
            table=table,
            row_key=previous_row_key,
            row_index=previous_row_index,
        )

    def _capture_table_cursor(self, *, table: DataTable) -> tuple[str | None, int]:
        if table.row_count == 0:
            return None, 0

        row_index = max(0, table.cursor_coordinate.row)
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:  # pragma: no cover - defensive Textual boundary.
            return None, row_index

        return str(cell_key.row_key.value), row_index

    def _restore_table_cursor(
        self,
        *,
        table: DataTable,
        row_key: str | None,
        row_index: int,
    ) -> None:
        if table.row_count == 0:
            return

        target_row = min(max(row_index, 0), table.row_count - 1)
        if row_key is not None:
            try:
                target_row = table.get_row_index(row_key)
            except Exception:  # pragma: no cover - defensive Textual boundary.
                pass

        table.move_cursor(row=target_row, column=0, animate=False)

    def _move_pack_cursor(self, *, delta: int) -> None:
        table = self.query_one("#pack-table", DataTable)
        self._move_pack_cursor_to(row=table.cursor_coordinate.row + delta)

    def _move_pack_cursor_to(self, *, row: int) -> None:
        if self._view_mode != "pack":
            return

        table = self.query_one("#pack-table", DataTable)
        if table.row_count == 0:
            return

        target_row = min(max(row, 0), table.row_count - 1)
        table.focus()
        table.move_cursor(row=target_row, column=0, animate=False)
        self._render_focused_card_details()

    def _move_build_card_cursor(self, *, delta: int) -> None:
        if not self._build_focus_cards:
            return

        target_index = self._build_focused_card_index + delta
        self._build_focused_card_index = min(
            max(target_index, 0),
            len(self._build_focus_cards) - 1,
        )
        self._refresh_build_text_from_result()
        self.query_one("#build-scroll", VerticalScroll).focus()
        self.query_one("#build-view", Static).update(self._build_text)
        self._render_focused_card_details()

    def _pack_cursor_page_size(self) -> int:
        table = self.query_one("#pack-table", DataTable)
        return max(1, table.size.height - 3)

    def _render_sidebar(self) -> None:
        pool_summary = self.query_one("#pool-summary", Static)
        show_pool_summary = self._view_mode not in {"build", "backtest"} and any(
            (
                self.visibility_preferences.pool_metadata,
                self.visibility_preferences.pool_color_distribution,
                self.visibility_preferences.pool_mana_curve,
            )
        )
        pool_summary.display = show_pool_summary and self.query_one(
            "#sidebar",
            Vertical,
        ).display
        if self.card_database_loading:
            if pool_summary.display:
                pool_summary.update("Card metadata\nLoading local cache and Scryfall data…")
            self._render_focused_card_details()
            return

        if self._card_database_error is not None:
            if pool_summary.display:
                pool_summary.update(
                    "Card metadata\nLoad failed — check the status bar for next steps."
                )
            self._render_focused_card_details()
            return

        if pool_summary.display:
            lines = ["Pool summary"]
            if self.visibility_preferences.pool_metadata:
                event_text = self._event_name or "unknown event"
                override_label = self._build_override_label()
                inferred_pair = _format_pair_label(
                    pair=self._pair_label,
                    mana_icons_enabled=self.mana_icons_enabled,
                )
                build_pair = _format_pair_label(
                    pair=self._build_pair_label,
                    mana_icons_enabled=self.mana_icons_enabled,
                )
                override = _format_pair_label(
                    pair=override_label,
                    mana_icons_enabled=self.mana_icons_enabled,
                )
                lines.extend(
                    (
                        f"Set: {format_set_label(set_code=self._set_code)}",
                        f"Event: {event_text}",
                    )
                )
                if self.visibility_preferences.draft_identifier:
                    lines.append(f"Draft: {self._draft_id or 'unknown draft'}")
                lines.extend(
                    (
                        f"Pool size: {self._pool_size}",
                        f"Inferred pair: {inferred_pair}",
                        f"Build pair: {build_pair}",
                        f"Override: {override}",
                        f"Build action: {self._build_action_label()}",
                        self._metadata_status_text(),
                    )
                )
            if self.visibility_preferences.pool_color_distribution:
                lines.append(
                    _pool_color_distribution_bar(
                        pool_grp_ids=self._pool_grp_ids,
                        card_database=self.card_database,
                        mana_icons_enabled=self.mana_icons_enabled,
                    )
                )
            if self.visibility_preferences.pool_mana_curve:
                lines.append(
                    _pool_curve_sparkline(
                        pool_grp_ids=self._pool_grp_ids,
                        card_database=self.card_database,
                    )
                )
            pool_summary.update("\n".join(lines))

        self._render_focused_card_details()

    def _render_focused_card_details(self) -> None:
        try:
            focused_card = self.query_one("#focused-card", Static)
        except NoMatches:
            return

        sidebar = self.query_one("#sidebar", Vertical)
        focused_card.display = (
            self.visibility_preferences.focused_card_details and sidebar.display
        )
        selected = self._focused_card_details()
        if selected is None:
            self._render_card_image_preview(card=None)
            if focused_card.display:
                focused_card.update(
                    "Focused card details\n"
                    "Use ↑/↓/←/→ in the card list to browse card details here."
                )
            return

        section, rank, total_count, scored_card, quantity = selected
        card = scored_card.card
        self._render_card_image_preview(card=card)
        if not focused_card.display:
            return

        type_line = _format_card_types(
            card=card,
            mana_icons_enabled=self.mana_icons_enabled,
        )
        quantity_line = f"Quantity: {quantity}\n" if quantity > 1 else ""
        color_label = _format_card_colors(
            card=card,
            mana_icons_enabled=self.mana_icons_enabled,
            long_colorless=True,
        )
        facts = (
            "Focused card details\n"
            f"{section} {rank}/{total_count}\n"
            f"{_format_card_name(card=card)}\n"
            f"{quantity_line}"
            f"Colors: {color_label}\n"
            f"Mana value: {_format_mana_value(card=card)}\n"
            f"Type: {type_line}\n"
            f"17L WR: {_format_win_rate(scored_card=scored_card)}\n"
            f"17L Grade: {_format_letter_grade(scored_card=scored_card)}\n"
            f"DO Score: {scored_card.score}\n"
            f"Color fit: {_format_color_fit(scored_card=scored_card)}\n"
            f"{_format_splash_details(scored_card=scored_card)}"
            f"ALSA (avg last seen): {_format_alsa(scored_card=scored_card)}\n"
            f"Data source: {_format_tui_source_label(scored_card=scored_card)}"
        )
        details = Text.from_markup(facts)
        if self._view_mode == "pack":
            recommendation = self._recommendations_by_grp_id.get(card.grp_id)
            if recommendation is not None and recommendation.concise_explanation:
                details.append("\n\nWhy this score:\n")
                details.append(recommendation.concise_explanation)
        focused_card.update(details)

    def _render_card_image_preview(self, *, card: CardInfo | None) -> None:
        image_panel = self.query_one("#card-image-preview", Static)
        if not self._card_image_preview_is_enabled():
            image_panel.display = False
            return

        sidebar = self.query_one("#sidebar", Vertical)
        if not sidebar.display:
            image_panel.display = False
            return

        if TgpImage is None:
            image_panel.display = True
            image_panel.update(
                "Image preview unavailable\n"
                "Install the textual-image package to render Kitty images."
            )
            return

        if card is None:
            image_panel.display = False
            return

        image_panel.display = True
        image_uri = self._card_image_uri_for_card(card=card)
        if image_uri is None:
            if card.unknown:
                image_panel.display = False
                return

            image_panel.update(
                "Image preview unavailable\n"
                f"{_format_card_name(card=card)}\n"
                "Image URL is not in the local Scryfall cache. Run refresh-data."
            )
            return
        image_path = self._card_image_paths_by_uri.get(image_uri)
        if image_path is not None and image_path.exists():
            self._show_card_image(
                image_panel=image_panel,
                image_path=image_path,
                card=card,
                image_uri=image_uri,
            )
            return

        cached_path = self._card_image_service.cached_path(
            image_uri=image_uri,
        )
        if cached_path.exists():
            self._card_image_paths_by_uri[image_uri] = cached_path
            self._show_card_image(
                image_panel=image_panel,
                image_path=cached_path,
                card=card,
                image_uri=image_uri,
            )
            return

        failure = self._card_image_failures_by_uri.get(image_uri)
        if failure is not None:
            image_panel.update(
                "Image preview unavailable\n"
                f"{_format_card_name(card=card)}\n"
                f"{failure}"
            )
            return

        image_panel.update(
            "Loading image preview…\n"
            f"{_format_card_name(card=card)}"
        )
        if image_uri not in self._loading_card_image_uris:
            self._loading_card_image_uris.add(image_uri)
            self._fetch_card_image_worker(image_uri)

    def _card_image_uri_for_card(self, *, card: CardInfo) -> str | None:
        image_uri = self._card_image_uris_by_grp_id.get(card.grp_id)
        if image_uri is None:
            image_uri = self._card_image_service.resolve_image_uri(
                card=card,
                card_database=self.card_database,
            )
        if image_uri is not None:
            self._card_image_uris_by_grp_id[card.grp_id] = image_uri

        return image_uri

    def _show_card_image(
        self,
        *,
        image_panel: Static,
        image_path: Path,
        card: CardInfo,
        image_uri: str,
    ) -> None:
        if TgpImage is None:
            return

        width = max(20, min(30, image_panel.size.width or 28))
        try:
            preview = TgpImage(
                str(image_path),
                width=width,
                height="auto",
            )
        except Exception as error:  # pragma: no cover - defensive renderer boundary.
            self._card_image_failures_by_uri[image_uri] = str(error)
            image_panel.update(
                "Image preview unavailable\n"
                f"{_format_card_name(card=card)}\n"
                f"{error}"
            )
            return

        image_panel.update(
            Group(
                Align.center(Text(f"Preview: {_format_card_name(card=card)}")),
                "",
                Align.center(preview),
            )
        )

    @work(thread=True, group="card-images")
    def _fetch_card_image_worker(self, image_uri: str) -> None:
        worker = get_current_worker()
        if worker.is_cancelled:
            return

        image_path = None
        error_message = None
        try:
            image_path = self._card_image_service.fetch(image_uri=image_uri)
        except Exception as error:  # pragma: no cover - defensive network boundary.
            error_message = str(error)

        if worker.is_cancelled:
            return

        self.call_from_thread(
            self._finish_card_image_load,
            image_uri,
            image_path,
            error_message,
        )

    def _finish_card_image_load(
        self,
        image_uri: str,
        image_path: Path | None,
        error_message: str | None,
    ) -> None:
        self._loading_card_image_uris.discard(image_uri)
        if image_path is None:
            self._card_image_failures_by_uri[image_uri] = error_message or "fetch failed"
        elif not image_path.exists():
            self._card_image_failures_by_uri[image_uri] = "fetch failed"
        else:
            self._card_image_paths_by_uri[image_uri] = image_path
            self._card_image_failures_by_uri.pop(image_uri, None)

        self._render_focused_card_details()

    def _focused_card_details(
        self,
    ) -> tuple[str, int, int, ScoredCard, int] | None:
        if self._view_mode == "build":
            return self._focused_build_card()

        selected = self._focused_pack_card()
        if selected is None:
            return None

        rank, scored_card = selected
        return "Available card", rank, len(self._sorted_cards()), scored_card, 1

    def _focused_build_card(self) -> tuple[str, int, int, ScoredCard, int] | None:
        if self._view_mode != "build" or not self._build_focus_cards:
            return None

        self._build_focused_card_index = min(
            max(self._build_focused_card_index, 0),
            len(self._build_focus_cards) - 1,
        )
        card, quantity = self._build_focus_cards[self._build_focused_card_index]
        return (
            "Selected card",
            self._build_focused_card_index + 1,
            len(self._build_focus_cards),
            card,
            quantity,
        )

    def _focused_pack_card(self) -> tuple[int, ScoredCard] | None:
        if self._view_mode != "pack" or self._current_pack is None:
            return None

        table = self.query_one("#pack-table", DataTable)
        row_index = table.cursor_coordinate.row
        cards = self._sorted_cards()
        if row_index < 0 or row_index >= len(cards):
            return None

        return row_index + 1, cards[row_index]

    def _ensure_visible_focus(self) -> None:
        focused_id = None if self.focused is None else self.focused.id
        if focused_id is None:
            self._focus_primary_card_section()
            return

        if focused_id == "pack-table" and self._view_mode != "pack":
            self._focus_primary_card_section()
            return

        if focused_id == "build-scroll" and self._view_mode not in {"build", "backtest"}:
            self._focus_primary_card_section()

    def _focus_primary_card_section(self) -> None:
        if self._view_mode in {"build", "backtest"}:
            self.query_one("#build-scroll", VerticalScroll).focus()
            return

        self.query_one("#pack-table", DataTable).focus()

    def _build_override_label(self) -> str:
        if self._forced_pair is not None:
            return self._forced_pair

        if self._build_error is not None and self._build_pair_label == "—":
            return "unavailable"

        return "automatic"

    def _build_action_label(self) -> str:
        if self._build_action_status is None:
            return "not requested"

        return self._build_action_status

    def _render_status_bar(self) -> None:
        status = self.query_one("#status-bar", Static)
        if self.card_database_loading:
            status.update("Loading card metadata… the watch UI remains available.")
            return

        if self._card_database_error is not None:
            status.update(f"Card metadata failed to load: {self._card_database_error}")
            return

        pair_label = self._pair_label
        if self._view_mode == "build" and self._build_pair_label != "—":
            pair_label = self._build_pair_label
        pair_label = _format_pair_label(
            pair=pair_label,
            mana_icons_enabled=self.mana_icons_enabled,
        )

        if self._view_mode == "build":
            sort_label = f"Build sort: {self._build_spell_sort_mode}"
        elif self._view_mode == "backtest":
            sort_label = f"Backtest ranking: {SORT_LABELS[self.sort_mode]}"
        else:
            sort_label = f"Ranking: {SORT_LABELS[self.sort_mode]}"

        segments: list[str] = []
        if self.visibility_preferences.account_identifier:
            segments.append(f"Account: {self._active_account_label}")
        profile_state = self.session.snapshot.set_profile
        profile_label = (
            "Profile: unavailable"
            if profile_state.maturity is None
            else f"Profile: {profile_state.maturity}"
        )
        if profile_state.refresh_outcome is not None:
            profile_label += f" ({profile_state.refresh_outcome})"
        segments.extend(
            (
                f"View: {self._view_mode}",
                f"Pair: {pair_label} ({self._commitment_label})",
                f"Pick: {self._pick_label}",
                f"Pool: {self._pool_size}",
                f"Data: {self._data_source}",
                profile_label,
                sort_label,
            )
        )
        confidence_label = self._recommendation_confidence_label()
        if confidence_label is not None:
            segments.append(f"Confidence: {confidence_label}")
        segments.append(self._splash_status_label())
        icon_label = "on" if self.mana_icons_enabled else "off"
        segments.append(f"Mana icons: {icon_label}")
        if self.visibility_preferences.attribution:
            segments.append(SEVENTEEN_LANDS_ATTRIBUTION)
        if self._forced_pair is not None:
            override = _format_pair_label(
                pair=self._forced_pair,
                mana_icons_enabled=self.mana_icons_enabled,
            )
            segments.insert(0, f"Override: {override}")

        unresolved_count = self._unresolved_metadata_count()
        if unresolved_count > 0:
            segments.insert(0, f"Warning: {unresolved_count} unresolved card metadata")
        if self._build_error is not None:
            segments.insert(0, f"Build: {self._build_error}")
        if self._build_action_status is not None:
            segments.insert(0, f"Build action: {self._build_action_status}")
        if self._backtest_action_status is not None:
            segments.insert(0, f"Backtest action: {self._backtest_action_status}")
        if self._backtest_error is not None and self._view_mode == "backtest":
            segments.insert(0, f"Backtest: {self._backtest_error}")
        if self._last_error is not None:
            segments.insert(0, f"Error: {self._last_error}")
        if self._session_error is not None:
            segments.insert(0, f"Error: {self._session_error}")
        if self._preferences_save_warning is not None:
            segments.insert(0, self._preferences_save_warning)
        if self._preferences_load_warning is not None:
            segments.insert(0, self._preferences_load_warning)

        status.update(" | ".join(segments))

    def _splash_status_label(self) -> str:
        enabled_label = "On" if self.visibility_preferences.splash_enabled else "Off"
        return f"Splash: {enabled_label}"

    def _column_keys_for_width(self) -> tuple[str, ...]:
        show_secondary = (
            self.show_secondary_columns and self.size.width >= SECONDARY_COLUMN_MIN_WIDTH
        )
        if show_secondary:
            return PRIMARY_COLUMN_KEYS + SECONDARY_COLUMN_KEYS

        return PRIMARY_COLUMN_KEYS

    def _sorted_cards(self) -> tuple[ScoredCard, ...]:
        if self._current_pack is None:
            return ()

        return rank_scored_cards(
            cards=self._current_pack.cards,
            ranking_mode=self.sort_mode,
        )

    def _recommendation_confidence_label(self) -> str | None:
        if self._view_mode != "pack" or self._current_pack is None:
            return None

        return recommendation_confidence_summary(
            cards=self._sorted_cards(),
            ranking_mode=self.sort_mode,
            phase=self._current_pack.commitment.phase,
        )


    def _metadata_status_text(self) -> str:
        visible_count = len(self._visible_metadata_grp_ids())
        if visible_count == 0:
            return "Metadata: waiting"

        unresolved_count = self._unresolved_metadata_count()
        if unresolved_count == 0:
            return "Metadata: complete"

        return f"Metadata warning: {unresolved_count} unresolved card metadata"

    def _unresolved_metadata_count(self) -> int:
        unresolved_grp_ids = self.card_database.unresolved_grp_ids(
            grp_ids=self._visible_metadata_grp_ids(),
        )
        return len(unresolved_grp_ids)

    def _visible_metadata_grp_ids(self) -> tuple[int, ...]:
        grp_ids = list(self._pool_grp_ids)
        if self._current_pack_event is not None:
            grp_ids.extend(self._current_pack_event.offered_grp_ids)

        return tuple(grp_ids)

    def _active_text_view(self) -> str:
        if self._view_mode == "backtest":
            return self._backtest_text

        return self._build_text

    def _request_build_view(self, *, success_message: str | None) -> None:
        if self.session.snapshot.pool.total_cards == 0 or self._set_code is None:
            self._build_error = "no picked cards yet"
            self._build_text = "Build view: no picked cards yet."
            self._build_pair_label = "—"
            self._build_result = None
            self._clear_build_render_state()
            if success_message is not None:
                self._build_action_status = "cannot build — no picked cards yet"
            return

        self._pending_build_success_message = success_message
        if success_message is not None:
            self._build_action_status = None
        self._build_error = None
        self._view_mode = "build"
        self._dispatch_session_command_worker(
            RequestBuild(
                pair_override=self._forced_pair,
                allow_splash=self.visibility_preferences.splash_enabled,
            )
        )

    def _request_backtest_view(
        self,
        *,
        success_message: str,
        open_when_ready: bool,
    ) -> None:
        self._pending_backtest_success_message = success_message
        self._backtest_action_status = None
        self._backtest_error = None
        self._open_backtest_when_ready = open_when_ready
        self._dispatch_session_command_worker(
            RequestBacktest(
                account_id=self._active_account_id,
                draft_id=self._draft_id,
            )
        )

    def _adopt_build_result(self, *, result: BuildResult) -> None:
        if (
            result.domain_pool is None
            or result.domain_selection is None
            or result.domain_spell_selection is None
            or result.domain_mana_base is None
        ):
            self._adopt_build_error(
                snapshot=self.session.snapshot,
                message="Shared build result is missing detailed build data.",
            )
            return

        self._build_result = result
        self._build_error = None
        self._last_error = None
        self._build_pair_label = result.selected_pair
        if self._pending_build_success_message is not None:
            self._build_action_status = self._pending_build_success_message
        self._pending_build_success_message = None
        self._refresh_build_text_from_result()

    def _adopt_build_error(
        self,
        *,
        snapshot: LiveSessionSnapshot,
        message: str,
    ) -> None:
        self._build_error = message
        self._build_text = _format_tui_build_error(
            pool=snapshot.pool,
            error=message,
            width=self._build_text_width(),
            mana_icons_enabled=self.mana_icons_enabled,
        )
        self._build_pair_label = "—"
        self._build_result = None
        self._clear_build_render_state()
        self._pending_build_success_message = None
        self._build_action_status = f"cannot build — {message}"

    def _adopt_backtest_result(self, *, result: BacktestResult) -> None:
        self._backtest_error = None
        self._backtest_text = _format_tui_backtest_result(result=result).rstrip("\n")
        if self._pending_backtest_success_message is not None:
            self._backtest_action_status = self._pending_backtest_success_message
        self._pending_backtest_success_message = None
        if self._open_backtest_when_ready:
            self._view_mode = "backtest"
            self.query_one("#build-scroll", VerticalScroll).scroll_home(animate=False)
            self._last_error = None
        self._open_backtest_when_ready = False

    def _refresh_build_text_from_result(self) -> None:
        result = self._build_result
        if (
            result is None
            or result.domain_pool is None
            or result.domain_selection is None
            or result.domain_spell_selection is None
            or result.domain_mana_base is None
        ):
            return

        spell_selection = result.domain_spell_selection
        self._build_focus_cards = _tui_selected_spell_groups(
            spell_selection=spell_selection,
            spell_sort_mode=self._build_spell_sort_mode,
        )
        if not self._build_focus_cards:
            self._build_focused_card_index = 0
        else:
            self._build_focused_card_index = min(
                max(self._build_focused_card_index, 0),
                len(self._build_focus_cards) - 1,
            )

        self._build_text = _format_tui_build_result(
            pool=result.domain_pool,
            selection=result.domain_selection,
            spell_selection=spell_selection,
            mana_base=result.domain_mana_base,
            card_database=self.card_database,
            spell_sort_mode=self._build_spell_sort_mode,
            show_details=self._build_show_details,
            focused_card_index=self._build_focused_card_index,
            width=self._build_text_width(),
            visibility_preferences=self.visibility_preferences,
            mana_icons_enabled=self.mana_icons_enabled,
        )

    def _clear_build_render_state(self) -> None:
        self._build_focus_cards = ()
        self._build_focused_card_index = 0

    def _build_text_width(self) -> int:
        sidebar_width = max(0, self.query_one("#sidebar", Vertical).size.width)
        return max(60, self.size.width - sidebar_width - 4)

    def _next_forced_pair(self) -> str:
        current_pair = self._forced_pair
        if current_pair is None and self._build_pair_label in COLOR_PAIRS:
            current_pair = self._build_pair_label

        if current_pair not in COLOR_PAIRS:
            return COLOR_PAIRS[0]

        index = COLOR_PAIRS.index(current_pair)
        return COLOR_PAIRS[(index + 1) % len(COLOR_PAIRS)]

    def _account_label(
        self,
        *,
        account_id: str | None,
        snapshot: LiveSessionSnapshot | None = None,
    ) -> str:
        client_id = account_id or self._active_account_id
        if client_id is None:
            return "unknown"

        current = self.session.snapshot if snapshot is None else snapshot
        identity = next(
            (
                account
                for account in current.accounts
                if account.account_id == client_id
            ),
            None,
        )
        if identity is None or identity.screen_name is None:
            return client_id

        return identity.screen_name

    def _record_error(self, message: str) -> None:
        self._last_error = message
        self._render_all()


def _card_image_preview_enabled(*, env: Mapping[str, str]) -> bool:
    if TgpImage is None:
        return False

    override = env.get(CARD_IMAGE_PREVIEW_ENV)
    if override is not None:
        return override.strip().casefold() in {"1", "true", "yes", "on"}

    term_program = env.get("TERM_PROGRAM", "").casefold()
    if term_program in {"ghostty", "kitty", "wezterm"}:
        return True

    term = env.get("TERM", "").casefold()
    if "kitty" in term or "ghostty" in term:
        return True

    return bool(
        env.get("KITTY_WINDOW_ID")
        or env.get("GHOSTTY_RESOURCES_DIR")
        or env.get("WEZTERM_EXECUTABLE")
    )

def _operation_error(
    *,
    snapshot: LiveSessionSnapshot,
    operation: OperationKind,
) -> SessionError | None:
    return next(
        (
            error
            for error in reversed(snapshot.errors)
            if error.operation == operation
        ),
        None,
    )


def _format_tui_backtest_result(*, result: BacktestResult) -> str:
    account = result.account_id or "unknown"
    if result.account_screen_name is not None:
        account = f"{result.account_screen_name} ({account})"
    completed = (
        "unknown" if result.completed is None else _format_tui_yes_no(result.completed)
    )
    chosen_pick_count = (
        "unknown"
        if result.chosen_pick_count is None
        else str(result.chosen_pick_count)
    )
    data_sources = "none" if not result.data_sources else "; ".join(result.data_sources)
    lines = [
        "Draft Omen backtest",
        f"Account: {account}",
        f"Set: {result.set_code or 'unknown'}",
        f"Event: {result.event_name or 'unknown'}",
        f"Draft: {result.draft_id or 'unknown'}",
        f"Completed: {completed}",
        f"Ranking: {ranking_label(ranking_mode=result.ranking_mode)}",
        (
            f"Picks: {chosen_pick_count} chosen, "
            f"{result.compared_count} compared, {result.skipped_count} skipped"
        ),
        f"Data sources: {data_sources}",
        "",
    ]
    lines.extend(_format_tui_backtest_rows(result=result))
    lines.append("")
    lines.extend(_format_tui_backtest_summary(result=result))
    return "\n".join(lines).rstrip() + "\n"


def _format_tui_backtest_rows(*, result: BacktestResult) -> list[str]:
    if not result.rows:
        return ["No saved picks were found for this draft."]

    recommended_values = tuple(
        _format_tui_backtest_recommended(row=row) for row in result.rows
    )
    actual_values = tuple(_format_tui_backtest_actual(row=row) for row in result.rows)
    recommended_width = max(
        len("Recommended"),
        *(len(value) for value in recommended_values),
    )
    actual_width = max(len("Actual"), *(len(value) for value in actual_values))
    lines = [
        "Pack  Pick  Pool  17L WR  DO   "
        f"{'Recommended':<{recommended_width}}  "
        f"{'Actual':<{actual_width}}  "
        "Match"
    ]
    for row, recommended, actual in zip(
        result.rows,
        recommended_values,
        actual_values,
        strict=True,
    ):
        lines.append(
            f"{row.pack_number + 1:>4}  "
            f"{row.pick_number + 1:>4}  "
            f"{_format_tui_optional_int(row.pool_size):>4}  "
            f"{_format_tui_backtest_win_rate(row=row):>6}  "
            f"{_format_tui_backtest_score(row=row):>3}  "
            f"{recommended:<{recommended_width}}  "
            f"{actual:<{actual_width}}  "
            f"{_format_tui_backtest_match(row=row)}"
        )

    return lines


def _format_tui_backtest_summary(*, result: BacktestResult) -> list[str]:
    if result.compared_count == 0:
        lines = [
            f"Summary: no comparable picks; {result.skipped_count} skipped."
        ]
    else:
        match_rate = result.match_count / result.compared_count
        lines = [
            "Summary: "
            f"{result.match_count}/{result.compared_count} recommendations matched "
            f"actual picks ({match_rate:.1%})."
        ]

    if result.skipped_count:
        lines.append(
            "Skipped picks were not scored when saved offered-card or "
            "pool-before-pick history was missing."
        )

    return lines


def _format_tui_backtest_recommended(*, row: BacktestPickResult) -> str:
    if row.recommended is not None:
        return _format_tui_card_view(card=row.recommended)

    reason = row.skipped_reason or "not scored"
    return f"skipped: {reason}"


def _format_tui_backtest_actual(*, row: BacktestPickResult) -> str:
    if row.actual is not None:
        return _format_tui_card_view(card=row.actual)

    return row.skipped_reason or "missing actual selected card"


def _format_tui_backtest_win_rate(*, row: BacktestPickResult) -> str:
    if row.recommended is None or row.recommended_win_rate is None:
        return "—"

    return f"{row.recommended_win_rate:.1%}"


def _format_tui_backtest_score(*, row: BacktestPickResult) -> str:
    if row.recommended is None or row.recommended_score is None:
        return "—"

    return str(row.recommended_score)


def _format_tui_backtest_match(*, row: BacktestPickResult) -> str:
    if row.match is None:
        return "skipped"

    return _format_tui_yes_no(row.match)


def _format_tui_card_view(*, card: CardView) -> str:
    colors = "Unknown" if _card_view_is_unknown(card=card) else (
        "".join(card.colors) if card.colors else "Colorless"
    )
    return f"{card.name} [{colors}] (grpId {card.grp_id})"


def _format_tui_optional_int(value: int | None) -> str:
    return "—" if value is None else str(value)


def _format_tui_yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _card_info_from_view(*, card: CardView) -> CardInfo:
    return CardInfo(
        grp_id=card.grp_id,
        name=card.name,
        colors=card.colors,
        mana_value=card.mana_value,
        rarity=card.rarity,
        types=card.types,
        mana_cost=card.mana_cost,
        produced_mana=card.produced_mana,
        unknown=card.unknown,
        oracle_text=card.oracle_text,
        keywords=card.keywords,
        type_line=card.type_line,
        subtypes=card.subtypes,
        layout=card.layout,
        faces=card.faces,
        set_code=card.set_code,
        collector_number=card.collector_number,
        arena_id=card.arena_id,
        source_provenance=card.source_provenance,
    )


def _card_view_is_unknown(*, card: CardView) -> bool:
    return card.unknown



_BUILD_COLUMN_MIN_WIDTH = 34
_BUILD_COLUMN_MAX_WIDTH = 80


def _format_tui_build_result(
    *,
    pool: BuildPool,
    selection: PairSelection,
    spell_selection: SpellSelection,
    mana_base: ManaBase,
    card_database: CardDatabase,
    spell_sort_mode: str,
    show_details: bool,
    focused_card_index: int,
    width: int,
    visibility_preferences: TuiVisibilityPreferences,
    mana_icons_enabled: bool = False,
) -> str:
    chosen_label = "forced" if selection.forced_pair is not None else "automatic"
    color_pair = _format_pair_label(
        pair=selection.chosen.pair,
        mana_icons_enabled=mana_icons_enabled,
    )
    counts = spell_selection.counts
    lines = [
        "[bold]Suggested deck[/bold]",
        "",
        f"Set: {format_set_label(set_code=pool.set_code)}",
        f"Color pair: {color_pair} ({chosen_label})",
        (
            f"Deck: {mana_base.deck_size} cards — "
            f"{mana_base.spell_count} spells, {mana_base.land_count} lands"
        ),
        f"Average mana value: {mana_base.average_mana_value:.2f}",
        (
            f"Creatures: {counts.creatures}; "
            f"Noncreatures: {counts.noncreatures}; Lands: {mana_base.land_count}"
        ),
        (
            "Ratings: rows show 17Lands WR and 17Lands-style grade from "
            "each source format."
        ),
        (
            "Keys: b checks build status; ↑/↓/←/→ or j/k browse cards; "
            "PgUp/PgDn page; s changes spell sort; c opens config; "
            "p changes pair; m toggles Mana icons"
        ),
        "",
    ]
    lines.extend(
        _format_tui_selected_spells(
            spell_selection=spell_selection,
            spell_sort_mode=spell_sort_mode,
            focused_card_index=focused_card_index,
            width=width,
            mana_icons_enabled=mana_icons_enabled,
        )
    )
    lines.append("")
    lines.extend(
        _format_tui_lands(
            mana_base=mana_base,
            mana_icons_enabled=mana_icons_enabled,
        )
    )
    if show_details:
        lines.append("")
        lines.extend(
            _format_tui_build_context(
                pool=pool,
                selection=selection,
                spell_selection=spell_selection,
                mana_base=mana_base,
                visibility_preferences=visibility_preferences,
                mana_icons_enabled=mana_icons_enabled,
            )
        )
        lines.append("")
        lines.extend(
            _format_tui_spell_counts(
                spell_selection=spell_selection,
                mana_icons_enabled=mana_icons_enabled,
            )
        )
        lines.append("")
        lines.extend(
            _format_tui_picked_pool(
                pool=pool,
                card_database=card_database,
                width=width,
                mana_icons_enabled=mana_icons_enabled,
            )
        )
        lines.append("")
        lines.extend(
            _format_tui_pair_scores(
                selection=selection,
                mana_icons_enabled=mana_icons_enabled,
            )
        )
        lines.append("")
        lines.extend(
            _format_tui_bench(
                selection=spell_selection,
                width=width,
                mana_icons_enabled=mana_icons_enabled,
            )
        )
    else:
        lines.append("")
        lines.append(
            "Details hidden: open config with c to show build context, picked pool, "
            "color-pair reasoning, structure checks, and bench cuts."
        )

    return "\n".join(lines) + "\n"


def _format_tui_build_context(
    *,
    pool: BuildPool,
    selection: PairSelection,
    spell_selection: SpellSelection,
    mana_base: ManaBase,
    visibility_preferences: TuiVisibilityPreferences,
    mana_icons_enabled: bool = False,
) -> list[str]:
    lines = ["Build context"]
    if visibility_preferences.pool_mana_curve:
        lines.append(_format_tui_curve_summary(spells=spell_selection.spells))
    if visibility_preferences.pool_metadata:
        lines.extend(
            (
                f"Pool: {pool.source_label}",
                f"Pool size: {selection.pool_size} cards",
            )
        )
    if visibility_preferences.account_identifier and pool.account_id is not None:
        lines.append(f"Account: {pool.account_id}")
    if visibility_preferences.draft_identifier and pool.draft_id is not None:
        lines.append(f"Draft: {pool.draft_id}")
    if visibility_preferences.mana_pips_and_sources:
        mana_pips = _format_plain_color_counts(
            mana_base.pip_counts,
            mana_icons_enabled=mana_icons_enabled,
        )
        mana_sources = _format_plain_color_counts(
            mana_base.source_counts,
            mana_icons_enabled=mana_icons_enabled,
        )
        lines.extend([
            f"Mana pips: {mana_pips}",
            f"Mana sources: {mana_sources}",
        ])
    return lines


def _format_tui_build_error(
    *,
    pool: PoolState,
    error: str,
    width: int,
    mana_icons_enabled: bool = False,
) -> str:
    lines = [
        f"Build view unavailable: {error}",
        "",
    ]
    lines.extend(
        _format_tui_snapshot_pool(
            pool=pool,
            width=width,
            mana_icons_enabled=mana_icons_enabled,
        )
    )
    return "\n".join(lines) + "\n"


def _format_tui_snapshot_pool(
    *,
    pool: PoolState,
    width: int,
    mana_icons_enabled: bool = False,
) -> list[str]:
    lines = [f"Picked pool ({pool.total_cards})"]
    if not pool.cards:
        return lines + ["- none"]

    index = 0
    for pool_card in pool.cards:
        card = _card_info_from_view(card=pool_card.card)
        for _ in range(pool_card.quantity):
            index += 1
            lines.append(
                _clip(
                    text=_format_tui_picked_card(
                        index=index,
                        card=card,
                        mana_icons_enabled=mana_icons_enabled,
                    ),
                    width=width,
                )
            )

    return lines


def _format_tui_picked_pool(
    *,
    pool: BuildPool,
    card_database: CardDatabase,
    width: int,
    mana_icons_enabled: bool = False,
) -> list[str]:
    lines = [f"Picked pool ({len(pool.pool_grp_ids)})"]
    if not pool.pool_grp_ids:
        return lines + ["- none"]

    for index, grp_id in enumerate(pool.pool_grp_ids, start=1):
        card = card_database.lookup(grp_id=grp_id)
        lines.append(
            _clip(
                text=_format_tui_picked_card(
                    index=index,
                    card=card,
                    mana_icons_enabled=mana_icons_enabled,
                ),
                width=width,
            )
        )

    return lines


def _format_tui_picked_card(
    *,
    index: int,
    card: CardInfo,
    mana_icons_enabled: bool = False,
) -> str:
    marker = "[unresolved] " if card.unknown else ""
    color_label = _format_card_colors(
        card=card,
        mana_icons_enabled=mana_icons_enabled,
        long_colorless=True,
    )
    mana_value = _format_mana_value(card=card)
    card_name = _format_card_name(card=card)
    return f"{index:02d}. {marker}{card_name} | Colors {color_label} | MV {mana_value}"


def _format_tui_curve_summary(*, spells: tuple[ScoredCard, ...]) -> str:
    counts = [0 for _ in CURVE_BUCKET_LABELS]
    unknown_count = 0
    for card in spells:
        mana_value = card.card.mana_value
        if mana_value is None:
            unknown_count += 1
            continue

        counts[_curve_bucket(mana_value=mana_value)] += 1

    parts = [
        f"{label}: {counts[index]}"
        for index, label in enumerate(CURVE_BUCKET_LABELS)
    ]
    if unknown_count > 0:
        parts.append(f"?: {unknown_count}")

    return "Mana curve: " + " | ".join(parts)


def _format_tui_selected_spells(
    *,
    spell_selection: SpellSelection,
    spell_sort_mode: str,
    focused_card_index: int,
    width: int,
    mana_icons_enabled: bool = False,
) -> list[str]:
    groups = _tui_selected_spell_groups(
        spell_selection=spell_selection,
        spell_sort_mode=spell_sort_mode,
    )
    if spell_sort_mode == "score":
        return _format_tui_spell_columns(
            title="Selected spells by score",
            groups=groups,
            total_count=len(spell_selection.spells),
            focused_card_index=focused_card_index,
            width=width,
            mana_icons_enabled=mana_icons_enabled,
        )

    if spell_sort_mode == "name":
        return _format_tui_spell_columns(
            title="Selected spells by name",
            groups=groups,
            total_count=len(spell_selection.spells),
            focused_card_index=focused_card_index,
            width=width,
            mana_icons_enabled=mana_icons_enabled,
        )

    return _format_tui_spell_curve(
        groups=groups,
        total_count=len(spell_selection.spells),
        focused_card_index=focused_card_index,
        width=width,
        mana_icons_enabled=mana_icons_enabled,
    )


def _tui_selected_spell_groups(
    *,
    spell_selection: SpellSelection,
    spell_sort_mode: str,
) -> tuple[TuiCardQuantityGroup, ...]:
    if spell_sort_mode == "score":
        return _group_tui_spell_cards(
            cards=tuple(sorted(
                spell_selection.spells,
                key=lambda card: (
                    -card.score,
                    _format_mana_value(card=card.card),
                    card.card.name,
                ),
            )),
        )

    if spell_sort_mode == "name":
        return _group_tui_spell_cards(
            cards=tuple(sorted(
                spell_selection.spells,
                key=lambda card: card.card.name,
            )),
        )

    return _group_tui_spell_cards(
        cards=tuple(sorted(spell_selection.spells, key=_tui_spell_curve_sort_key)),
    )


def _format_tui_spell_curve(
    *,
    groups: tuple[TuiCardQuantityGroup, ...],
    total_count: int,
    focused_card_index: int,
    width: int,
    mana_icons_enabled: bool = False,
) -> list[str]:
    groups_by_bucket: dict[str, list[tuple[int, TuiCardQuantityGroup]]] = {}
    for index, group in enumerate(groups):
        groups_by_bucket.setdefault(_mana_value_bucket(card=group[0]), []).append(
            (index, group),
        )

    blocks: list[list[str]] = []
    for bucket in _ordered_mana_buckets(groups={
        key: [card for _, (card, _) in values]
        for key, values in groups_by_bucket.items()
    }):
        indexed_groups = groups_by_bucket[bucket]
        bucket_count = sum(quantity for _, (_, quantity) in indexed_groups)
        block = [f"MV {bucket} ({bucket_count})"]
        block.extend(
            _format_tui_spell_card(
                card=card,
                quantity=quantity,
                focused=index == focused_card_index,
                show_focus_marker=True,
                mana_icons_enabled=mana_icons_enabled,
            )
            for index, (card, quantity) in indexed_groups
        )
        blocks.append(block)

    return [f"Selected spells by mana value ({total_count})"] + _columnize_blocks(
        blocks=blocks,
        width=width,
    )


def _format_tui_spell_columns(
    *,
    title: str,
    groups: tuple[TuiCardQuantityGroup, ...],
    total_count: int,
    focused_card_index: int,
    width: int,
    mana_icons_enabled: bool = False,
) -> list[str]:
    blocks = [
        [
            _format_tui_spell_card(
                card=card,
                quantity=quantity,
                focused=index == focused_card_index,
                show_focus_marker=True,
                mana_icons_enabled=mana_icons_enabled,
            )
        ]
        for index, (card, quantity) in enumerate(groups)
    ]
    return [f"{title} ({total_count})"] + _columnize_blocks(blocks=blocks, width=width)


def _columnize_blocks(*, blocks: list[list[str]], width: int) -> list[str]:
    if not blocks:
        return ["- none"]

    column_count = max(1, min(2, width // _BUILD_COLUMN_MIN_WIDTH))
    column_width = min(
        _BUILD_COLUMN_MAX_WIDTH,
        max(_BUILD_COLUMN_MIN_WIDTH, width // column_count),
    )
    columns: list[list[str]] = [[] for _ in range(column_count)]
    heights = [0 for _ in range(column_count)]
    for block in blocks:
        column_index = min(range(column_count), key=lambda index: heights[index])
        if columns[column_index]:
            columns[column_index].append("")
            heights[column_index] += 1

        columns[column_index].extend(block)
        heights[column_index] += len(block)

    max_height = max(len(column) for column in columns)
    lines: list[str] = []
    for row_index in range(max_height):
        parts = []
        for column in columns:
            text = column[row_index] if row_index < len(column) else ""
            parts.append(_clip(text=text, width=column_width - 2).ljust(column_width))

        lines.append("".join(parts).rstrip())

    return lines


def _format_tui_lands(
    *,
    mana_base: ManaBase,
    mana_icons_enabled: bool = False,
) -> list[str]:
    lines = [f"Lands: {mana_base.land_count} ({mana_base.reason})"]
    basics = ", ".join(
        f"{basic.count} {basic.name}" for basic in mana_base.basic_lands
    )
    if basics:
        lines.append(f"Basics: {basics}")
    else:
        lines.append("Basics: none")

    if mana_base.nonbasic_lands:
        nonbasic_parts = []
        for land in mana_base.nonbasic_lands:
            source_label = _format_color_label(
                colors=land.source_colors,
                mana_icons_enabled=mana_icons_enabled,
                long_colorless=False,
            )
            nonbasic_parts.append(f"{land.card.name} ({source_label} source)")

        lines.append(f"Nonbasics: {'; '.join(nonbasic_parts)}")
    else:
        lines.append("Nonbasics: none")

    return lines


def _format_tui_spell_counts(
    *,
    spell_selection: SpellSelection,
    mana_icons_enabled: bool = False,
) -> list[str]:
    counts = spell_selection.counts
    constraints = spell_selection.constraints
    pair = _format_pair_label(
        pair=spell_selection.pair,
        mana_icons_enabled=mana_icons_enabled,
    )
    return [
        "Structure checks",
        f"Eligible spells for {pair}: {spell_selection.eligible_count}",
        f"Selected spells: {counts.total}/{spell_selection.requested_spell_count}",
        (
            f"Creatures: {counts.creatures} "
            f"(target {constraints.creature_floor}-{constraints.creature_ceiling})"
        ),
        f"Two-drops: {counts.two_drops} (minimum {constraints.minimum_two_drops})",
        f"Expensive spells: {counts.expensive} (soft cap {constraints.maximum_expensive_spells})",
        f"Applied relaxations: {_format_tui_relaxations(spell_selection.applied_relaxations)}",
    ]


def _format_tui_pair_scores(
    *,
    selection: PairSelection,
    mana_icons_enabled: bool = False,
) -> list[str]:
    lines = [
        "Color-pair reasoning",
        (
            "This diagnostic compares playable cards with 17Lands color-pair "
            "context; it is not another decklist."
        ),
    ]
    for score in selection.ranked_scores:
        pair = _format_pair_label(
            pair=score.pair,
            mana_icons_enabled=mana_icons_enabled,
        )
        lines.append(
            f"- {pair}: {score.playable_count} playable cards; "
            f"pair strength {score.blended_score:.2f}; "
            f"17Lands WR {_format_tui_win_rate(score.pair_win_rate)}; "
            f"top {selection.target_spell_count} sum {score.playable_score_sum:.2f}"
        )

    return lines


def _format_tui_bench(
    *,
    selection: SpellSelection,
    width: int,
    mana_icons_enabled: bool = False,
) -> list[str]:
    lines = ["Bench"]
    if not selection.bench:
        return lines + ["- none"]

    for card, quantity in _group_tui_spell_cards(cards=selection.bench):
        lines.append(
            _clip(
                text=_format_tui_spell_card(
                    card=card,
                    quantity=quantity,
                    mana_icons_enabled=mana_icons_enabled,
                ),
                width=width,
            )
        )

    return lines


def _format_tui_spell_card(
    *,
    card: ScoredCard,
    quantity: int = 1,
    focused: bool = False,
    show_focus_marker: bool = False,
    mana_icons_enabled: bool = False,
) -> str:
    quantity_suffix = _format_tui_quantity_suffix(quantity=quantity)
    focus_marker = ""
    if show_focus_marker:
        focus_marker = "▶ " if focused else "  "

    color_label = _format_card_colors(
        card=card.card,
        mana_icons_enabled=mana_icons_enabled,
        long_colorless=False,
    )
    return (
        f"{focus_marker}"
        f"{_format_win_rate(scored_card=card):>6} "
        f"{_format_letter_grade(scored_card=card):>2} "
        f"{card.score:>2} "
        f"{card.card.name} ({color_label})"
        f"{quantity_suffix}"
    )


def _group_tui_spell_cards(
    *,
    cards: Iterable[ScoredCard],
) -> tuple[TuiCardQuantityGroup, ...]:
    grouped_cards: dict[TuiCardQuantityKey, TuiCardQuantityGroup] = {}
    for card in cards:
        quantity_key = _tui_card_quantity_key(card=card.card)
        existing_group = grouped_cards.get(quantity_key)
        if existing_group is None:
            grouped_cards[quantity_key] = (card, 1)
            continue

        representative, quantity = existing_group
        grouped_cards[quantity_key] = (representative, quantity + 1)

    return tuple(grouped_cards.values())


def _tui_card_quantity_key(*, card: CardInfo) -> TuiCardQuantityKey:
    if card.unknown:
        return ("unknown", str(card.grp_id))

    return ("name", " ".join(card.name.casefold().split()))


def _format_tui_quantity_suffix(*, quantity: int) -> str:
    if quantity <= 1:
        return ""

    return f" x{quantity}"


def _tui_spell_curve_sort_key(card: ScoredCard) -> tuple[float, int, str, int]:
    mana_value = 99.0 if card.card.mana_value is None else card.card.mana_value
    return (mana_value, -card.score, card.card.name, card.original_index)


def _mana_value_bucket(*, card: ScoredCard) -> str:
    mana_value = card.card.mana_value
    if mana_value is None:
        return "?"

    if mana_value >= 6:
        return "6+"

    return _format_mana_value(card=card.card)


def _ordered_mana_buckets(*, groups: dict[str, list[ScoredCard]]) -> tuple[str, ...]:
    ordered = tuple(label for label in ("0", "1", "2", "3", "4", "5", "6+") if label in groups)
    if "?" in groups:
        return ordered + ("?",)

    return ordered


def _format_plain_color_counts(
    counts: tuple[tuple[str, int], ...],
    *,
    mana_icons_enabled: bool = False,
) -> str:
    if not counts:
        return "none"

    parts = []
    for color, count in counts:
        label = _format_color_count_label(
            color=color,
            mana_icons_enabled=mana_icons_enabled,
        )
        parts.append(f"{label} {count}")

    return ", ".join(parts)


def _format_plain_colors(colors: tuple[str, ...]) -> str:
    return _format_color_label(colors=colors, mana_icons_enabled=False)


def _format_card_colors(
    *,
    card: CardInfo,
    mana_icons_enabled: bool = False,
    long_colorless: bool = True,
) -> str:
    if card.unknown:
        return "Unknown"

    return _format_color_label(
        colors=card.colors,
        mana_icons_enabled=mana_icons_enabled,
        long_colorless=long_colorless,
    )


def _format_color_label(
    *,
    colors: tuple[str, ...],
    mana_icons_enabled: bool = False,
    long_colorless: bool = True,
) -> str:
    if not colors:
        return _format_colorless_label(
            mana_icons_enabled=mana_icons_enabled,
            long_colorless=long_colorless,
        )

    if not mana_icons_enabled:
        return "".join(colors)

    return "".join(
        _format_mana_symbol(symbol=color, mana_icons_enabled=mana_icons_enabled)
        for color in colors
    )


def _format_pair_label(*, pair: str, mana_icons_enabled: bool = False) -> str:
    if not mana_icons_enabled:
        return pair

    if not pair or any(symbol not in MANA_ICON_GLYPHS for symbol in pair):
        return pair

    return "".join(
        _format_mana_symbol(symbol=symbol, mana_icons_enabled=mana_icons_enabled)
        for symbol in pair
    )


def _format_color_count_label(*, color: str, mana_icons_enabled: bool = False) -> str:
    if color == UNKNOWN_COLOR_KEY:
        return UNKNOWN_COLOR_KEY

    if color == COLORLESS_KEY:
        return _format_colorless_label(
            mana_icons_enabled=mana_icons_enabled,
            long_colorless=False,
        )

    return _format_pair_label(pair=color, mana_icons_enabled=mana_icons_enabled)


def _format_colorless_label(
    *,
    mana_icons_enabled: bool,
    long_colorless: bool,
) -> str:
    if not mana_icons_enabled:
        return "Colorless"

    fallback = "Colorless" if long_colorless else COLORLESS_KEY
    return f"{MANA_ICON_GLYPHS[COLORLESS_KEY]} {fallback}"


def _format_mana_symbol(*, symbol: str, mana_icons_enabled: bool) -> str:
    if not mana_icons_enabled:
        return symbol

    return MANA_ICON_GLYPHS.get(symbol, symbol)


def _format_card_types(
    *,
    card: CardInfo,
    mana_icons_enabled: bool = False,
) -> str:
    type_line = " ".join(card.types) if card.types else "Unknown"
    if card.unknown or not mana_icons_enabled:
        return type_line

    icons = [
        glyph
        for card_type, glyph in MANA_CARD_TYPE_GLYPHS.items()
        if any(card_type in type_part for type_part in card.types)
    ]
    if not icons:
        return type_line

    return f"{''.join(icons)} {type_line}"


def _format_tui_relaxations(relaxations: tuple[str, ...]) -> str:
    return ", ".join(relaxations) if relaxations else "none"


def _format_tui_win_rate(value: float | None) -> str:
    if value is None:
        return "unknown"

    return f"{value:.1%}"


def _clip(*, text: str, width: int) -> str:
    if len(text) <= width:
        return text

    if width <= 1:
        return "…"

    protected_suffix = _clip_protected_suffix(text=text)
    if protected_suffix and width > len(protected_suffix) + 1:
        prefix_width = width - len(protected_suffix) - 1
        return text[:prefix_width] + "…" + protected_suffix

    return text[: width - 1] + "…"


def _clip_protected_suffix(*, text: str) -> str:
    quantity_index = text.rfind(" x")
    if quantity_index == -1 or not text[quantity_index + 2 :].isdigit():
        return ""

    color_close_index = quantity_index - 1
    if color_close_index < 0 or text[color_close_index] != ")":
        return ""

    color_open_index = text.rfind("(", 0, color_close_index)
    if color_open_index == -1:
        return ""

    if color_open_index > 0 and text[color_open_index - 1] == " ":
        return text[color_open_index - 1 :]

    return text[color_open_index:]


_ERROR_EXIT_CODE = 1


def run_tui_watch(
    *,
    log_path: PathInput,
    card_database: CardDatabase | None = None,
    set_card_data_loader: SetCardDataLoader | None = None,
    app_dir: PathInput | None = None,
    profile_client: ProfileClient | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    once: bool = False,
    startup_scan: bool = False,
    mana_icons_enabled: bool = False,
    splash_enabled: bool | None = None,
) -> int:
    """Run Textual watch mode and return a process-style exit code.
    Metadata may load in a worker after the initial shell has rendered.
    """

    app = DraftomenTuiApp(
        log_path=log_path,
        card_database=card_database,
        set_card_data_loader=set_card_data_loader,
        app_dir=app_dir,
        profile_client=profile_client,
        poll_interval=poll_interval,
        startup_scan=startup_scan,
        once=once,
        mana_icons_enabled=mana_icons_enabled,
        splash_enabled=splash_enabled,
    )
    try:
        app.run(headless=once)
    except KeyboardInterrupt:
        return 130
    except Exception:
        return _ERROR_EXIT_CODE

    return 0




def _row_cells(
    *,
    rank: int,
    scored_card: ScoredCard,
    column_keys: tuple[str, ...],
    mana_icons_enabled: bool = False,
) -> tuple[object, ...]:
    values = {
        "rank": f"{rank:02d}",
        "win_rate": _format_win_rate(scored_card=scored_card),
        "grade": _format_letter_grade(scored_card=scored_card),
        "score": str(scored_card.score),
        "card": _format_pick_card_name(scored_card=scored_card),
        "colors": _styled_colors(
            card=scored_card.card,
            mana_icons_enabled=mana_icons_enabled,
        ),
        "fit": _format_color_fit(scored_card=scored_card),
        "gih": _format_win_rate(scored_card=scored_card),
        "alsa": _format_alsa(scored_card=scored_card),
        "mv": _format_mana_value(card=scored_card.card),
        "source": scored_card.source_label,
    }
    return tuple(values[column_key] for column_key in column_keys)




def _commitment_label(*, commitment: float) -> str:
    if commitment <= 0.0:
        phase = "open"
    elif commitment >= 1.0:
        phase = "locked"
    else:
        phase = "building"

    return f"{int(round(commitment * 100))}% {phase}"


def _format_card_name(*, card: CardInfo) -> str:
    if card.unknown:
        return f"{card.name} (grpId {card.grp_id})"

    return card.name


def _format_pick_card_name(*, scored_card: ScoredCard) -> str:
    card_name = _format_card_name(card=scored_card.card)
    splash_color = scored_card.splash.splash_color
    if splash_color is None:
        return card_name

    color_name = COLOR_NAMES.get(splash_color, splash_color).upper()
    if scored_card.color_fit == "splash-ready":
        return f"SPLASH {color_name} — {card_name}"
    if scored_card.color_fit == "splash-speculative":
        return f"POSSIBLE SPLASH {color_name} — {card_name}"
    if scored_card.color_fit == "splash-fixer":
        return f"{color_name} SPLASH MANA — {card_name}"

    return card_name


def _styled_colors(*, card: CardInfo, mana_icons_enabled: bool = False) -> Text:
    if card.unknown:
        return Text("Unknown", style="bold yellow")

    if not card.colors:
        colorless = _format_colorless_label(
            mana_icons_enabled=mana_icons_enabled,
            long_colorless=False,
        )
        return Text(colorless, style="grey50")

    text = Text()
    for color in card.colors:
        symbol = _format_mana_symbol(
            symbol=color,
            mana_icons_enabled=mana_icons_enabled,
        )
        text.append(symbol, style=COLOR_STYLES.get(color, "bold"))
    return text


def _format_color_fit(*, scored_card: ScoredCard) -> str:
    if scored_card.color_fit == "on-color":
        return "On"

    if scored_card.color_fit == "off-color":
        return "Off!"

    if scored_card.color_fit == "colorless":
        return "Any"

    if scored_card.color_fit == "unknown":
        return "?"

    if scored_card.color_fit == "splash-ready":
        return f"Splash {scored_card.splash.splash_color}"

    if scored_card.color_fit == "splash-speculative":
        return f"Splash? {scored_card.splash.splash_color}"

    if scored_card.color_fit == "splash-fixer":
        return f"Fix {scored_card.splash.splash_color}"

    return "Open"


def _format_splash_details(*, scored_card: ScoredCard) -> str:
    splash = scored_card.splash
    if splash.splash_color is None:
        return ""

    color_name = COLOR_NAMES.get(splash.splash_color, splash.splash_color)
    classification = {
        "splash-ready": "Recommended — mana is supported",
        "splash-speculative": "Speculative — more fixing is needed",
        "splash-fixer": "Fixing pick for the active splash",
        "off-color": "Not recommended as a splash",
    }.get(splash.classification, splash.classification.replace("-", " ").title())
    source_line = ""
    if splash.required_sources > 0:
        source_parts = [f"{splash.fixing_sources} fixing lands"]
        if splash.planned_basic_sources:
            basic_name = BASIC_LAND_NAMES.get(
                splash.splash_color,
                "basic land",
            )
            source_parts.append(
                f"{splash.planned_basic_sources} planned {basic_name}"
            )
        source_line = (
            f"Mana support: {splash.available_sources} of "
            f"{splash.required_sources} sources "
            f"({', '.join(source_parts)})\n"
        )
    reason = "; ".join(splash.reasons)
    return (
        f"Splash recommendation: {color_name}\n"
        f"Splash assessment: {classification}\n"
        f"{source_line}"
        f"Reason: {reason}\n"
    )


def _format_win_rate(*, scored_card: ScoredCard) -> str:
    if scored_card.rating.gih_win_rate is None:
        return "—"

    return f"{scored_card.rating.gih_win_rate:.1%}"


def _format_letter_grade(*, scored_card: ScoredCard) -> str:
    return scored_card.rating.letter_grade or "—"


def _format_alsa(*, scored_card: ScoredCard) -> str:
    if scored_card.rating.average_last_seen_at is None:
        return "—"

    return f"{scored_card.rating.average_last_seen_at:.2f}"


def _format_tui_source_label(*, scored_card: ScoredCard) -> str:
    labels = {
        "Quick": "Quick Draft",
        "Premier": "Premier Draft fallback",
        "Prior": "neutral prior",
    }
    return labels.get(scored_card.source_label, scored_card.source_label)


def _format_mana_value(*, card: CardInfo) -> str:
    if card.mana_value is None:
        return "—"

    if card.mana_value.is_integer():
        return str(int(card.mana_value))

    return f"{card.mana_value:.1f}"


def _pool_color_distribution_bar(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    mana_icons_enabled: bool = False,
) -> str:
    if not pool_grp_ids:
        return "Colors: none"

    counts = _pool_color_counts(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
    )
    max_count = max(counts.values(), default=0)
    keys = COLOR_ORDER + (COLORLESS_KEY,)
    parts = []
    for color in keys:
        bar = _scaled_bar(count=counts[color], max_count=max_count)
        label = _format_color_count_label(
            color=color,
            mana_icons_enabled=mana_icons_enabled,
        )
        parts.append(f"{label} {bar} {counts[color]}")
    if counts[UNKNOWN_COLOR_KEY] > 0:
        parts.append(
            f"? {_scaled_bar(count=counts[UNKNOWN_COLOR_KEY], max_count=max_count)} "
            f"{counts[UNKNOWN_COLOR_KEY]}"
        )

    return "Colors: " + " | ".join(parts)


def _pool_color_counts(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
) -> Counter[str]:
    counts: Counter[str] = Counter({color: 0 for color in COLOR_ORDER})
    counts[COLORLESS_KEY] = 0
    counts[UNKNOWN_COLOR_KEY] = 0
    for grp_id in pool_grp_ids:
        card = card_database.lookup(grp_id=grp_id)
        if card.unknown:
            counts[UNKNOWN_COLOR_KEY] += 1
            continue

        if not card.colors:
            counts[COLORLESS_KEY] += 1
            continue

        for color in card.colors:
            if color in COLOR_ORDER:
                counts[color] += 1
            else:
                counts[UNKNOWN_COLOR_KEY] += 1

    return counts


def _pool_curve_sparkline(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
) -> str:
    if not pool_grp_ids:
        return "Curve: none"

    counts = _mana_curve_counts(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
    )
    max_count = max(counts, default=0)
    parts = []
    for index, label in enumerate(CURVE_BUCKET_LABELS):
        glyph = _sparkline_glyph(count=counts[index], max_count=max_count)
        parts.append(f"{label}{glyph}{counts[index]}")
    return "Curve: " + " ".join(parts)


def _mana_curve_counts(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
) -> list[int]:
    counts = [0 for _ in CURVE_BUCKET_LABELS]
    for grp_id in pool_grp_ids:
        card = card_database.lookup(grp_id=grp_id)
        if card.unknown or card.mana_value is None or _is_land_card(card=card):
            continue

        bucket = _curve_bucket(mana_value=card.mana_value)
        counts[bucket] += 1

    return counts


def _curve_bucket(*, mana_value: float) -> int:
    rounded = max(0, int(mana_value))
    return min(rounded, len(CURVE_BUCKET_LABELS) - 1)


def _is_land_card(*, card: CardInfo) -> bool:
    return any("Land" in type_line for type_line in card.types)


def _is_creature_card(*, card: CardInfo) -> bool:
    return any("Creature" in type_line for type_line in card.types)


def _scaled_bar(*, count: int, max_count: int, width: int = 5) -> str:
    if count <= 0 or max_count <= 0:
        return "·"

    filled = max(1, round((count / max_count) * width))
    return "█" * filled


def _sparkline_glyph(*, count: int, max_count: int) -> str:
    if count <= 0 or max_count <= 0:
        return "·"

    index = max(0, round((count / max_count) * (len(SPARKLINE_GLYPHS) - 1)))
    return SPARKLINE_GLYPHS[index]
