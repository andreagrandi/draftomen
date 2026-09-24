from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from draftomen.mock_session import MOCK_SCENARIOS, MockLiveSession
from draftomen.qt_adapter import _to_qml_value
from draftomen.qt_mock import MockSessionAdapter
from draftomen.session import (
    LiveSessionSnapshot,
    OperationKind,
    RequestBacktest,
    RequestBuild,
)


def test_qt_translation_publishes_plain_values_without_domain_payloads() -> None:
    snapshot = MockLiveSession().snapshot
    values = _to_qml_value(snapshot)
    build = snapshot.build

    assert build is not None
    expected_average = sum(
        entry.quantity * entry.card.mana_value
        for entry in build.spells
        if entry.card.mana_value is not None
    ) / sum(entry.quantity for entry in build.spells)
    assert values["status"]["phase"] == "drafting"
    recommendation_rows = values["recommendations"]["cards"]
    assert recommendation_rows[0]["card"]["name"]
    assert "enhancement_availability" not in values
    assert "enhancement_advice_message" not in values
    for recommendation in recommendation_rows:
        assert "relationship_contributions" not in recommendation
        assert "relationship_advice" not in recommendation
        assert "relationship_advice_enabled" not in recommendation
    recommendation = recommendation_rows[0]
    assert values["contextual_adjustments_enabled"] is True
    assert values["contextual_evidence"]["status"] == "exact"
    assert values["contextual_evidence"]["message"] == (
        "Contextual · semantic + QuickDraft evidence"
    )
    assert "contextual_breakdown" in recommendation
    assert "contextual_evidence" in recommendation
    assert "basic_score" in recommendation
    assert "augmentation_delta" in recommendation
    assert values["augmentation"] == {
        "status": "unavailable",
        "set_code": None,
        "enabled": False,
    }
    assert values["augmentation_message"] == "Augmented Intelligence is unavailable."
    assert values["build"]["average_mana_value"] == pytest.approx(expected_average)
    assert values["set_profile"]["maturity"] == "mature"
    assert values["set_profile"]["refresh_outcome"] == "unchanged"
    assert values["pool"]["average_mana_value"] == pytest.approx(17 / 7)
    recent_picks = values["pool"]["recent_picks"]
    assert isinstance(recent_picks, list)
    assert isinstance(recent_picks[0], dict)
    assert isinstance(recent_picks[0]["card"], dict)
    assert isinstance(recent_picks[0]["image"], dict)
    assert recent_picks[0]["image"]["phase"] == "unavailable"
    assert recent_picks[0]["image"]["image_path"] is None
    assert "domain_pool" not in values["build"]
    assert "domain_selection" not in values["build"]


def test_qt_mock_adapter_exposes_shared_provider_contract() -> None:
    session = MockLiveSession()
    adapter = MockSessionAdapter(session=session)

    assert adapter.mockMode is True
    assert adapter.scenario == "ready"
    assert adapter.scenarios == list(MOCK_SCENARIOS)
    assert adapter.recommendationsModel.rowCount() == len(
        session.snapshot.recommendations.cards
    )

    adapter.selectScenario("warning")
    assert adapter.scenario == "warning"
    assert adapter.state["ratings"]["phase"] == "missing"

    selected_grp_id = session.snapshot.recommendations.cards[1].card.grp_id
    adapter.chooseRecommendation(selected_grp_id)
    assert adapter.state["recommendations"]["selected_grp_id"] == selected_grp_id

    adapter.changeRanking("win_rate")
    assert adapter.state["recommendations"]["ranking_mode"] == "win_rate"

    adapter.setSplashEnabled(False)
    assert adapter.state["recommendations"]["splash_enabled"] is False

    initial_average = adapter.state["build"]["average_mana_value"]
    adapter.requestBuild("BG")
    assert adapter.state["build"]["pair_override"] == "BG"
    assert adapter.state["build"]["average_mana_value"] == initial_average

    adapter.requestBacktest()
    assert adapter.state["backtest"]["rows"]


def test_qt_adapter_ignores_unknown_visual_scenario() -> None:
    adapter = MockSessionAdapter(session=MockLiveSession())

    adapter.selectScenario("unknown")

    assert adapter.scenario == "ready"
    assert isinstance(adapter.state, dict)
    assert isinstance(_to_qml_value(LiveSessionSnapshot()), dict)


def test_qt_mock_exposes_build_and_backtest_failure_variants() -> None:
    adapter = MockSessionAdapter(session=MockLiveSession())

    adapter.selectScenario("build_error")
    assert adapter.state["build"] is None
    assert adapter.state["errors"][0]["operation"] == "build"
    adapter.selectScenario("backtest_missing")
    assert adapter.state["backtest"]["skipped_count"] == 1
    assert adapter.state["backtest"]["rows"][0]["skipped_reason"]
    adapter.selectScenario("backtest_error")
    assert adapter.state["backtest"] is None
    assert adapter.state["errors"][0]["operation"] == "backtest"


def test_qt_mock_success_clears_only_its_operation_error() -> None:
    session = MockLiveSession(scenario="backtest_error")

    build = session.dispatch(command=RequestBuild())

    assert build.build is not None
    assert [error.operation for error in build.errors] == [OperationKind.BACKTEST]

    session = MockLiveSession(scenario="build_error")

    backtest = session.dispatch(command=RequestBacktest())

    assert backtest.backtest is not None
    assert [error.operation for error in backtest.errors] == [OperationKind.BUILD]

