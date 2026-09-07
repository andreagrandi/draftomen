"""Persisted user settings for Draftomen interfaces.
Keep optional behavior and layout choices stable across app restarts.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from os import PathLike
from pathlib import Path
from typing import Literal, TypeAlias

from draftomen.paths import app_data_dir

PathInput: TypeAlias = str | PathLike[str]
CardImagePreviewMode: TypeAlias = Literal["auto", "show", "hide"]

TUI_PREFERENCES_FILE_NAME = "tui-preferences.json"
TUI_PREFERENCES_SCHEMA_VERSION = 1
CARD_IMAGE_PREVIEW_MODES = frozenset({"auto", "show", "hide"})


@dataclass(frozen=True)
class TuiVisibilityPreferences:
    """User-controlled Textual behavior and optional interface elements.
    Missing settings use safe defaults so existing files remain compatible.
    """

    secondary_columns: bool = True
    build_details: bool = False
    splash_enabled: bool = True
    pool_metadata: bool = True
    pool_color_distribution: bool = True
    pool_mana_curve: bool = True
    account_identifier: bool = True
    draft_identifier: bool = True
    mana_pips_and_sources: bool = True
    attribution: bool = True
    focused_card_details: bool = True
    card_image_preview: CardImagePreviewMode = "auto"


def tui_preferences_path(*, app_dir: PathInput | None = None) -> Path:
    """Return the path used for persisted TUI visibility preferences.
    The file lives directly in Draftomen's per-user application directory.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
    return root / TUI_PREFERENCES_FILE_NAME


def load_tui_preferences(
    *,
    app_dir: PathInput | None = None,
) -> tuple[TuiVisibilityPreferences, str | None]:
    """Load persisted visibility preferences, falling back safely to defaults.
    A warning describes malformed or unsupported configuration without blocking startup.
    """

    try:
        path = tui_preferences_path(app_dir=app_dir)
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return TuiVisibilityPreferences(), None
    except (OSError, UnicodeDecodeError) as error:
        return TuiVisibilityPreferences(), f"Could not load TUI preferences: {error}"

    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as error:
        return TuiVisibilityPreferences(), f"Could not load TUI preferences: {error}"

    if not isinstance(raw_data, dict):
        return (
            TuiVisibilityPreferences(),
            "Could not load TUI preferences: expected an object.",
        )

    version = raw_data.get("version")
    if version != TUI_PREFERENCES_SCHEMA_VERSION:
        return (
            TuiVisibilityPreferences(),
            "Could not load TUI preferences: unsupported settings version.",
        )

    visibility = raw_data.get("visibility")
    if not isinstance(visibility, dict):
        return (
            TuiVisibilityPreferences(),
            "Could not load TUI preferences: expected a visibility object.",
        )

    defaults = TuiVisibilityPreferences()
    values = asdict(defaults)
    invalid_fields: list[str] = []
    for field_name, default_value in values.items():
        value = visibility.get(field_name, default_value)
        if field_name == "card_image_preview":
            if not isinstance(value, str) or value not in CARD_IMAGE_PREVIEW_MODES:
                invalid_fields.append(field_name)
                continue
        elif type(value) is not bool:
            invalid_fields.append(field_name)
            continue

        values[field_name] = value

    preferences = TuiVisibilityPreferences(**values)
    if not invalid_fields:
        return preferences, None

    fields = ", ".join(invalid_fields)
    return preferences, f"TUI preferences used defaults for invalid fields: {fields}."


def save_tui_preferences(
    *,
    preferences: TuiVisibilityPreferences,
    app_dir: PathInput | None = None,
) -> str | None:
    """Atomically save visibility preferences and return a non-fatal error message.
    The active TUI can continue using preferences when persistence fails.
    """

    path = tui_preferences_path(app_dir=app_dir)
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            json.dump(
                {
                    "version": TUI_PREFERENCES_SCHEMA_VERSION,
                    "visibility": asdict(preferences),
                },
                temporary_file,
                indent=2,
                sort_keys=True,
            )
            temporary_file.write("\n")
            temporary_path = Path(temporary_file.name)

        temporary_path.replace(path)
    except OSError as error:
        return f"Could not save TUI preferences: {error}"
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    return None


GUI_PREFERENCES_FILE_NAME = "gui-preferences.json"
GUI_PREFERENCES_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GuiDisplayPreferences:
    """User-controlled desktop display choices and contextual-scoring selection are persisted.
    Functional changes still reach the live session through explicit commands.
    """

    compact_density: bool = False
    secondary_stats: bool = True
    card_preview: bool = True
    detailed_build_context: bool = True
    system_text_scaling: bool = True
    show_backtest: bool = False
    contextual_adjustments_enabled: bool = False


def gui_preferences_path(*, app_dir: PathInput | None = None) -> Path:
    """Return the path used for persisted desktop display preferences.
    The file lives directly in Draftomen's per-user application directory.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
    return root / GUI_PREFERENCES_FILE_NAME


def load_gui_preferences(
    *,
    app_dir: PathInput | None = None,
) -> tuple[GuiDisplayPreferences, str | None]:
    """Load desktop display preferences, falling back safely to defaults.
    A warning describes malformed or unsupported configuration without blocking startup.
    """

    try:
        path = gui_preferences_path(app_dir=app_dir)
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return GuiDisplayPreferences(), None
    except (OSError, UnicodeDecodeError) as error:
        return GuiDisplayPreferences(), f"Could not load GUI preferences: {error}"

    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as error:
        return GuiDisplayPreferences(), f"Could not load GUI preferences: {error}"

    if not isinstance(raw_data, dict):
        return GuiDisplayPreferences(), "Could not load GUI preferences: expected an object."
    if raw_data.get("version") != GUI_PREFERENCES_SCHEMA_VERSION:
        return (
            GuiDisplayPreferences(),
            "Could not load GUI preferences: unsupported settings version.",
        )

    display = raw_data.get("display")
    if not isinstance(display, dict):
        return (
            GuiDisplayPreferences(),
            "Could not load GUI preferences: expected a display object.",
        )

    defaults = GuiDisplayPreferences()
    values = asdict(defaults)
    invalid_fields: list[str] = []
    for field_name, default_value in values.items():
        value = display.get(field_name, default_value)
        if type(value) is not bool:
            invalid_fields.append(field_name)
            continue
        values[field_name] = value

    preferences = GuiDisplayPreferences(**values)
    if not invalid_fields:
        return preferences, None
    return (
        preferences,
        "GUI preferences used defaults for invalid fields: "
        + ", ".join(invalid_fields)
        + ".",
    )


def save_gui_preferences(
    *,
    preferences: GuiDisplayPreferences,
    app_dir: PathInput | None = None,
) -> str | None:
    """Atomically save desktop display preferences and return a non-fatal error.
    The active GUI can continue using a changed preference when persistence fails.
    """

    path = gui_preferences_path(app_dir=app_dir)
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            json.dump(
                {
                    "version": GUI_PREFERENCES_SCHEMA_VERSION,
                    "display": asdict(preferences),
                },
                temporary_file,
                indent=2,
                sort_keys=True,
            )
            temporary_file.write("\n")
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(path)
    except OSError as error:
        return f"Could not save GUI preferences: {error}"
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    return None
