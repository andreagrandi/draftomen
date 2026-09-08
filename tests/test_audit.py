from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import draftomen.audit as audit_module
from draftomen.audit import (
    DraftAuditError,
    DraftAuditStore,
    draft_audit_path,
    load_draft_audit_records,
)
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import PICK_ENGINE
from draftomen.events import (
    DraftCompletedEvent,
    PackOfferedEvent,
    PickMadeEvent,
)
from draftomen.pickengine import (
    PickEngine,
    render_pick_rationale_concise,
    render_pick_rationale_detailed,
)
from draftomen.pool import DraftState
from draftomen.set_profile import (
    PairProfile,
    ProfileMaturity,
    SampleSummary,
    SetProfile,
    SourceMetadata,
)

ACCOUNT_ID = "account-a"
DRAFT_ID = "draft-a"
EVENT_NAME = "QuickDraft_ABC_20260727"
SET_CODE = "ABC"


def test_audit_records_complete_decision_and_choice_without_duplicates(
    tmp_path: Path,
) -> None:
    state = _draft_state()
    offer = _pack_event()
    engine = PickEngine()
    scored_pack = engine.score_pack(
        offered_grp_ids=offer.offered_grp_ids,
        card_database=_card_database(),
        pool_grp_ids=offer.pool_grp_ids,
        pick_index=1,
    )
    store = DraftAuditStore(
        app_dir=tmp_path,
        clock=_fixed_clock,
        app_version="1.2.3",
    )

    store.record_draft_started(state=state)
    store.record_draft_started(state=state)
    store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=engine.ratings_data,
    )
    store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=engine.ratings_data,
    )
    store.record_choice(
        state=state,
        event=_pick_event(),
        ranking_mode="mv",
    )
    store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=engine.ratings_data,
    )
    store.record_draft_completed(
        state=_completed_state(),
        event=_completed_event(),
    )
    store.record_draft_completed(
        state=_completed_state(),
        event=_completed_event(),
    )

    records = load_draft_audit_records(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )

    assert [record["record_type"] for record in records] == [
        "draft_started",
        "decision_evaluated",
        "choice_made",
        "draft_completed",
    ]
    decision = records[1]
    assert decision["schema_version"] == 1
    assert decision["app_version"] == "1.2.3"
    assert decision["recorded_at"] == "2026-07-27T10:30:00+00:00"
    assert decision["offered_grp_ids"] == [101, 102]
    assert decision["pool_before_pick"] == []
    assert decision["recommended_grp_id"] == 101
    assert decision["rankings"]["score"] == [101, 102]
    assert decision["rankings"]["mv"] == [102, 101]
    assert decision["algorithm"]["config"]["locked_pick_index"] == (
        PICK_ENGINE.locked_pick_index
    )
    assert decision["algorithm"]["features"]["splash_enabled"] is True
    assert decision["ratings_snapshot"] is None
    assert decision["commitment"] == {
        "color_weights": {"B": 0.0, "G": 0.0, "R": 0.0, "U": 0.0, "W": 0.0},
        "inferred_pair": None,
        "level": 0.0,
        "locked": False,
        "phase": "open",
        "pick_index": 1,
        "pool_size": 0,
    }
    assert decision["splash_state"] == {
        "active_color": None,
        "aggressive": False,
        "base_pair": None,
        "enabled": True,
        "fixing_sources": [
            ["W", 0],
            ["U", 0],
            ["B", 0],
            ["R", 0],
            ["G", 0],
        ],
        "picked_card_count": 0,
    }
    scored_cards = {card.card.grp_id: card for card in scored_pack.cards}
    for candidate in decision["candidates"]:
        scored_card = scored_cards[candidate["grp_id"]]
        assert candidate["rationale"] == scored_card.rationale.to_json()
        assert candidate["concise_explanation"] == render_pick_rationale_concise(
            scored_card=scored_card,
        )
        assert candidate["explanation"] == render_pick_rationale_detailed(
            scored_card=scored_card,
        )
    assert len(decision["candidates"]) == 2
    assert decision["candidates"][0]["scoring"]["source_label"] == "Prior*"
    assert decision["candidates"][0]["rating"]["sample_counts"]["games_in_hand"] == 0
    assert decision["candidates"][0]["splash"]["classification"] == "open"
    assert decision["candidates"][0]["splash"]["reasons"] == [
        "primary colors are still open"
    ]
    assert decision["context_provenance"] is None
    assert decision["recommendation"]["grp_id"] == decision["recommended_grp_id"]
    recommended_scored_card = scored_cards[decision["recommended_grp_id"]]
    assert decision["recommendation"]["rationale"] == (
        recommended_scored_card.rationale.to_json()
    )
    assert decision["recommendation"]["concise_explanation"] == (
        render_pick_rationale_concise(scored_card=recommended_scored_card)
    )
    assert decision["recommendation"]["explanation"] == (
        render_pick_rationale_detailed(scored_card=recommended_scored_card)
    )
    assert decision["recommendation"]["contextual_evidence"] == (
        decision["candidates"][0]["scoring"]["contextual_evidence"]
    )

    choice = records[2]
    assert choice["evaluation_id"] == decision["evaluation_id"]
    assert choice["chosen_grp_id"] == 102
    assert choice["ranking_mode"] == "mv"
    assert choice["recommended_grp_id"] == 102
    assert choice["recommendation_followed"] is True

    completion = records[3]
    assert completion["picked_grp_ids"] == [102]
    assert completion["pick_count"] == 1
    assert completion["inferred"] is False


@pytest.mark.parametrize(
    ("comparison_summary",),
    ((None,), ("DO recommendation: Alpha leads Beta by 10 DO points.",)),
)
def test_audit_decision_serializes_comparison_summary(
    tmp_path: Path,
    comparison_summary: str | None,
) -> None:
    state = _draft_state()
    offer = _pack_event()
    engine = PickEngine()
    scored_pack = replace(
        engine.score_pack(
            offered_grp_ids=offer.offered_grp_ids,
            card_database=_card_database(),
            pool_grp_ids=offer.pool_grp_ids,
            pick_index=1,
        ),
        comparison_summary=comparison_summary,
    )
    store = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)

    store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=engine.ratings_data,
    )

    records = load_draft_audit_records(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )
    assert records[0]["comparison_summary"] == comparison_summary
    assert all(
        "comparison_summary" not in candidate
        for candidate in records[0]["candidates"]
    )


def test_audit_comparison_summary_is_excluded_from_evaluation_identity() -> None:
    state = _draft_state()
    offer = _pack_event()
    engine = PickEngine()
    scored_pack = engine.score_pack(
        offered_grp_ids=offer.offered_grp_ids,
        card_database=_card_database(),
        pool_grp_ids=offer.pool_grp_ids,
        pick_index=1,
    )
    evaluation = audit_module._decision_payload(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=engine.ratings_data,
        app_version="test",
        decision_id="decision-a",
    )

    missing = dict(evaluation)
    missing.pop("comparison_summary", None)
    null = dict(evaluation)
    null["comparison_summary"] = None
    text = dict(evaluation)
    text["comparison_summary"] = "DO recommendation: explanatory text."

    identities = {
        json.dumps(
            audit_module._evaluation_identity_payload(evaluation=variant),
            sort_keys=True,
        )
        for variant in (missing, null, text)
    }
    assert len(identities) == 1

    changed_scoring = dict(evaluation)
    candidates = list(evaluation["candidates"])
    first_candidate = dict(candidates[0])
    first_scoring = dict(first_candidate["scoring"])
    first_scoring["score"] += 1
    first_candidate["scoring"] = first_scoring
    candidates[0] = first_candidate
    changed_scoring["candidates"] = candidates
    assert audit_module._evaluation_identity_payload(
        evaluation=changed_scoring,
    ) != audit_module._evaluation_identity_payload(evaluation=evaluation)


def test_audit_comparison_summary_changes_do_not_duplicate_decisions(
    tmp_path: Path,
) -> None:
    state = _draft_state()
    offer = _pack_event()
    engine = PickEngine()
    scored_pack = engine.score_pack(
        offered_grp_ids=offer.offered_grp_ids,
        card_database=_card_database(),
        pool_grp_ids=offer.pool_grp_ids,
        pick_index=1,
    )
    store = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)

    store.record_decision(
        state=state,
        event=offer,
        scored_pack=replace(scored_pack, comparison_summary=None),
        config=engine.config,
        ratings_data=engine.ratings_data,
    )
    store.record_decision(
        state=state,
        event=offer,
        scored_pack=replace(
            scored_pack,
            comparison_summary="DO recommendation: explanatory text.",
        ),
        config=engine.config,
        ratings_data=engine.ratings_data,
    )

    records = load_draft_audit_records(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )
    assert len(records) == 1
    assert records[0]["comparison_summary"] is None


def test_audit_context_provenance_omits_pool_but_preserves_profile_and_evidence(
    tmp_path: Path,
) -> None:
    state = _draft_state()
    offer = _pack_event(pool_grp_ids=(101,))
    profile = _set_profile()
    engine = PickEngine(set_profile=profile)
    scored_pack = engine.score_pack(
        offered_grp_ids=offer.offered_grp_ids,
        card_database=_card_database(),
        pool_grp_ids=offer.pool_grp_ids,
        pack_number=offer.pack_number,
        pick_number=offer.pick_number,
        global_pick_index=1,
        estimated_remaining_picks=41,
    )
    store = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)

    store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=engine.ratings_data,
    )

    records = load_draft_audit_records(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )
    decision = records[0]
    provenance = decision["context_provenance"]
    assert decision["pool_before_pick"] == [101]
    assert provenance == {
        "stage": {
            "pack_number": 0,
            "pick_number": 0,
            "global_pick_index": 1,
            "estimated_remaining_picks": 41,
        },
        "profile": {
            "set_code": "abc",
            "format": "quickdraft",
            "profile_version": "audit-context-test",
            "maturity": "mature",
            "confidence": 1.0,
            "fingerprint": profile.fingerprint,
            "source": {"provider": "test"},
        },
    }
    recommendation = decision["recommendation"]
    assert recommendation is not None
    assert recommendation["grp_id"] == decision["recommended_grp_id"]
    candidate = next(
        candidate
        for candidate in decision["candidates"]
        if candidate["grp_id"] == recommendation["grp_id"]
    )
    for candidate_payload, scored_card in zip(
        decision["candidates"],
        scored_pack.cards,
    ):
        assert candidate_payload["scoring"]["contextual_evidence"] == list(
            scored_card.contextual_evidence
        )
    for field in (
        "contextual_breakdown",
        "contextual_evidence",
        "contextual_pair",
        "contextual_theme",
        "contextual_profile_maturity",
        "contextual_profile_confidence",
    ):
        expected = (
            list(candidate["scoring"][field])
            if field == "contextual_evidence"
            else candidate["scoring"][field]
        )
        assert recommendation[field] == expected


def test_restart_does_not_re_evaluate_a_pick_with_a_recorded_choice(
    tmp_path: Path,
) -> None:
    state = _draft_state()
    offer = _pack_event()
    engine = PickEngine()
    scored_pack = engine.score_pack(
        offered_grp_ids=offer.offered_grp_ids,
        card_database=_card_database(),
        pool_grp_ids=offer.pool_grp_ids,
        pick_index=1,
    )
    first_store = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)
    first_store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=None,
    )
    first_store.record_choice(
        state=state,
        event=_pick_event(),
        ranking_mode="score",
    )

    restarted_store = DraftAuditStore(app_dir=tmp_path, clock=_later_clock)
    restarted_store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=None,
    )
    restarted_store.record_choice(
        state=state,
        event=_pick_event(),
        ranking_mode="score",
    )

    records = load_draft_audit_records(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )
    assert [record["record_type"] for record in records] == [
        "decision_evaluated",
        "choice_made",
    ]
    assert {record["recorded_at"] for record in records} == {
        "2026-07-27T10:30:00+00:00"
    }


def test_audit_restart_preserves_identity_for_historical_records_without_rationale(
    tmp_path: Path,
) -> None:
    state = _draft_state()
    offer = _pack_event()
    engine = PickEngine()
    scored_pack = engine.score_pack(
        offered_grp_ids=offer.offered_grp_ids,
        card_database=_card_database(),
        pool_grp_ids=offer.pool_grp_ids,
        pick_index=1,
    )
    store = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)
    store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=None,
    )
    path = draft_audit_path(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )
    historical = json.loads(path.read_text(encoding="utf-8"))
    for field in ("rationale", "concise_explanation", "explanation"):
        historical["recommendation"].pop(field, None)
        for candidate in historical["candidates"]:
            candidate.pop(field, None)
    path.write_text(
        json.dumps(historical, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    restarted_store = DraftAuditStore(app_dir=tmp_path, clock=_later_clock)
    restarted_store.record_decision(
        state=state,
        event=offer,
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=None,
    )

    records = load_draft_audit_records(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )
    assert len(records) == 1
    assert "rationale" not in records[0]["recommendation"]

def test_audit_loader_rejects_a_malformed_json_line(tmp_path: Path) -> None:
    path = draft_audit_path(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": 1, "record_id": "valid"}) + "\nnot-json\n",
        encoding="utf-8",
    )

    with pytest.raises(DraftAuditError) as error:
        load_draft_audit_records(
            account_id=ACCOUNT_ID,
            draft_id=DRAFT_ID,
            app_dir=tmp_path,
        )

    assert "at line 2" in str(error.value)


def test_audit_append_failure_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)

    def fail_open(*args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(audit_module.os, "open", fail_open)

    with pytest.raises(DraftAuditError) as error:
        store.record_draft_started(state=_draft_state())

    assert "disk full" in str(error.value)
    assert not draft_audit_path(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    ).exists()


def test_audit_path_rejects_unsafe_account_and_draft_ids(tmp_path: Path) -> None:
    with pytest.raises(DraftAuditError):
        draft_audit_path(
            account_id="../account",
            draft_id=DRAFT_ID,
            app_dir=tmp_path,
        )

    with pytest.raises(DraftAuditError):
        draft_audit_path(
            account_id=ACCOUNT_ID,
            draft_id="draft/name",
            app_dir=tmp_path,
        )


def test_audit_file_is_compact_jsonl_with_one_object_per_record(tmp_path: Path) -> None:
    store = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)
    path = store.record_draft_started(state=_draft_state())
    store.record_draft_completed(
        state=_completed_state(),
        event=_completed_event(),
    )

    lines = path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert all("\n" not in line for line in lines)


def _draft_state() -> DraftState:
    started_at = "2026-07-27T10:00:00+00:00"
    return DraftState(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        event_name=EVENT_NAME,
        set_code=SET_CODE,
        course_id=DRAFT_ID,
        started_at=started_at,
        updated_at=started_at,
        completed_at=None,
        completed=False,
        picks=(),
        pool_grp_ids=(),
    )


def _set_profile() -> SetProfile:
    return SetProfile(
        set_code="ABC",
        event_format="quickdraft",
        profile_version="audit-context-test",
        generated_at="1970-01-01T00:00:00+00:00",
        source=SourceMetadata(provider="test"),
        maturity=ProfileMaturity.MATURE,
        samples=SampleSummary(total=1, by_pair=(("WU", 1),)),
        confidence=1.0,
        pairs=(PairProfile(pair="WU"),),
    )


def _completed_state() -> DraftState:
    state = _draft_state()
    return DraftState(
        account_id=state.account_id,
        draft_id=state.draft_id,
        event_name=state.event_name,
        set_code=state.set_code,
        course_id=state.course_id,
        started_at=state.started_at,
        updated_at="2026-07-27T10:45:00+00:00",
        completed_at="2026-07-27T10:45:00+00:00",
        completed=True,
        picks=state.picks,
        pool_grp_ids=(102,),
    )


def _pack_event(*, pool_grp_ids: tuple[int, ...] = ()) -> PackOfferedEvent:
    return PackOfferedEvent(
        event_name=EVENT_NAME,
        set_code=SET_CODE,
        pack_number=0,
        pick_number=0,
        offered_grp_ids=(101, 102),
        pool_grp_ids=pool_grp_ids,
        account_id=ACCOUNT_ID,
    )


def _pick_event() -> PickMadeEvent:
    return PickMadeEvent(
        event_name=EVENT_NAME,
        set_code=SET_CODE,
        pack_number=0,
        pick_number=0,
        chosen_grp_id=102,
        account_id=ACCOUNT_ID,
    )


def _completed_event() -> DraftCompletedEvent:
    return DraftCompletedEvent(
        event_name=EVENT_NAME,
        set_code=SET_CODE,
        pack_number=2,
        pick_number=13,
        picked_grp_ids=(102,),
        inferred=False,
        account_id=ACCOUNT_ID,
    )


def _card_database() -> CardDatabase:
    return CardDatabase(
        cards={
            101: CardInfo(
                grp_id=101,
                name="Large Green Card",
                colors=("G",),
                mana_value=5.0,
                rarity="common",
                types=("Creature",),
            ),
            102: CardInfo(
                grp_id=102,
                name="Small Black Card",
                colors=("B",),
                mana_value=2.0,
                rarity="common",
                types=("Creature",),
            ),
        }
    )


def _fixed_clock() -> datetime:
    return datetime(2026, 7, 27, 10, 30, tzinfo=UTC)


def _later_clock() -> datetime:
    return datetime(2026, 7, 27, 11, 30, tzinfo=UTC)
