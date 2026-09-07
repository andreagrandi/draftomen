from __future__ import annotations

import json
from pathlib import Path

import pytest

import draftomen.preferences as preferences_module
from draftomen.preferences import (
    GuiDisplayPreferences,
    TuiVisibilityPreferences,
    gui_preferences_path,
    load_gui_preferences,
    load_tui_preferences,
    save_gui_preferences,
    save_tui_preferences,
    tui_preferences_path,
)


def test_load_tui_preferences_uses_defaults_when_file_is_missing(tmp_path: Path) -> None:
    preferences, warning = load_tui_preferences(app_dir=tmp_path / "app")

    assert preferences == TuiVisibilityPreferences()
    assert warning is None


def test_tui_preferences_round_trip_atomically_to_explicit_app_directory(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    expected = TuiVisibilityPreferences(
        secondary_columns=False,
        build_details=True,
        card_image_preview="hide",
        attribution=False,
    )

    warning = save_tui_preferences(preferences=expected, app_dir=app_dir)
    actual, load_warning = load_tui_preferences(app_dir=app_dir)
    path = tui_preferences_path(app_dir=app_dir)

    assert warning is None
    assert actual == expected
    assert load_warning is None
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "visibility": {
            "account_identifier": True,
            "attribution": False,
            "build_details": True,
            "card_image_preview": "hide",
            "draft_identifier": True,
            "focused_card_details": True,
            "mana_pips_and_sources": True,
            "pool_color_distribution": True,
            "pool_mana_curve": True,
            "pool_metadata": True,
                "secondary_columns": False,
                "splash_enabled": True,
            },
        }
    assert not tuple(path.parent.glob("tmp*"))


def test_load_tui_preferences_merges_partial_files_and_ignores_unknown_fields(
    tmp_path: Path,
) -> None:
    path = tui_preferences_path(app_dir=tmp_path / "app")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "visibility": {
                    "secondary_columns": False,
                    "card_image_preview": "show",
                    "future_setting": "ignored",
                },
            }
        ),
        encoding="utf-8",
    )

    preferences, warning = load_tui_preferences(app_dir=path.parent)

    assert preferences.secondary_columns is False
    assert preferences.card_image_preview == "show"
    assert preferences.build_details is False
    assert warning is None


def test_load_tui_preferences_uses_defaults_for_invalid_fields(tmp_path: Path) -> None:
    path = tui_preferences_path(app_dir=tmp_path / "app")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "visibility": {
                    "secondary_columns": "false",
                    "build_details": True,
                    "card_image_preview": ["hide"],
                },
            }
        ),
        encoding="utf-8",
    )

    preferences, warning = load_tui_preferences(app_dir=path.parent)

    assert preferences.secondary_columns is True
    assert preferences.build_details is True
    assert preferences.card_image_preview == "auto"
    assert warning is not None
    assert "secondary_columns" in warning
    assert "card_image_preview" in warning


def test_load_tui_preferences_recovers_from_malformed_and_unsupported_files(
    tmp_path: Path,
) -> None:
    path = tui_preferences_path(app_dir=tmp_path / "app")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    malformed_preferences, malformed_warning = load_tui_preferences(app_dir=path.parent)

    path.write_text(
        json.dumps({"version": 99, "visibility": {}}),
        encoding="utf-8",
    )
    unsupported_preferences, unsupported_warning = load_tui_preferences(
        app_dir=path.parent,
    )

    assert malformed_preferences == TuiVisibilityPreferences()
    assert malformed_warning is not None
    assert unsupported_preferences == TuiVisibilityPreferences()
    assert unsupported_warning is not None
    assert "unsupported settings version" in unsupported_warning


def test_load_tui_preferences_recovers_from_invalid_utf8_and_path_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tui_preferences_path(app_dir=tmp_path / "app")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x80")

    invalid_utf8_preferences, invalid_utf8_warning = load_tui_preferences(
        app_dir=path.parent,
    )

    def inaccessible_path(*, app_dir: object = None) -> Path:
        raise PermissionError("preferences directory is inaccessible")

    monkeypatch.setattr(preferences_module, "tui_preferences_path", inaccessible_path)
    inaccessible_preferences, inaccessible_warning = load_tui_preferences()

    assert invalid_utf8_preferences == TuiVisibilityPreferences()
    assert invalid_utf8_warning is not None
    assert inaccessible_preferences == TuiVisibilityPreferences()
    assert inaccessible_warning is not None
    assert "inaccessible" in inaccessible_warning


def test_tui_preferences_path_uses_default_application_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preferences_module, "app_data_dir", lambda: tmp_path / "app")

    path = tui_preferences_path()

    assert path == tmp_path / "app" / "tui-preferences.json"


def test_load_gui_preferences_uses_defaults_when_file_is_missing(tmp_path: Path) -> None:
    preferences, warning = load_gui_preferences(app_dir=tmp_path / "app")

    assert preferences == GuiDisplayPreferences()
    assert preferences.contextual_adjustments_enabled is False
    assert warning is None


def test_load_gui_preferences_defaults_contextual_adjustments_for_existing_v1_file(
    tmp_path: Path,
) -> None:
    path = gui_preferences_path(app_dir=tmp_path / "app")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "display": {
                    "secondary_stats": False,
                },
            },
        ),
        encoding="utf-8",
    )

    preferences, warning = load_gui_preferences(app_dir=path.parent)

    assert preferences == GuiDisplayPreferences(secondary_stats=False)
    assert preferences.contextual_adjustments_enabled is False
    assert warning is None


@pytest.mark.parametrize("enabled", [False, True])
def test_gui_preferences_round_trip_contextual_adjustments(
    tmp_path: Path,
    enabled: bool,
) -> None:
    expected = GuiDisplayPreferences(contextual_adjustments_enabled=enabled)

    assert save_gui_preferences(preferences=expected, app_dir=tmp_path / "app") is None
    actual, warning = load_gui_preferences(app_dir=tmp_path / "app")

    assert actual == expected
    assert warning is None


def test_gui_preferences_round_trip_and_isolate_display_choices(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    expected = GuiDisplayPreferences(
        compact_density=True,
        secondary_stats=False,
        card_preview=False,
        detailed_build_context=False,
        system_text_scaling=False,
        show_backtest=True,
    )

    assert save_gui_preferences(preferences=expected, app_dir=app_dir) is None
    actual, warning = load_gui_preferences(app_dir=app_dir)

    assert actual == expected
    assert warning is None
    assert json.loads(gui_preferences_path(app_dir=app_dir).read_text()) == {
        "display": {
            "card_preview": False,
            "compact_density": True,
            "contextual_adjustments_enabled": False,
            "detailed_build_context": False,
            "secondary_stats": False,
            "show_backtest": True,
            "system_text_scaling": False,
        },
        "version": 1,
    }


def test_gui_preferences_recover_from_invalid_schema_and_fields(
    tmp_path: Path,
) -> None:
    path = gui_preferences_path(app_dir=tmp_path / "app")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "display": {
                    "compact_density": "yes",
                    "contextual_adjustments_enabled": "yes",
                    "secondary_stats": False,
                    "show_backtest": "yes",
                },
            },
        ),
        encoding="utf-8",
    )

    preferences, warning = load_gui_preferences(app_dir=path.parent)

    assert preferences == GuiDisplayPreferences(secondary_stats=False)
    assert warning is not None
    assert "compact_density" in warning
    assert "contextual_adjustments_enabled" in warning
    assert "show_backtest" in warning
    path.write_text(json.dumps({"version": 2, "display": {}}), encoding="utf-8")
    _, unsupported_warning = load_gui_preferences(app_dir=path.parent)
    assert unsupported_warning is not None
    assert "unsupported settings version" in unsupported_warning
