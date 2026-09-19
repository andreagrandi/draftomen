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
from draftomen.semantic_capability_records import (
    CapabilityQuantity,
    CapabilityZone,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import (
    SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
    card_source_sha256,
)
from draftomen.semantic_enrichment_records import (
    ArtifactReview,
    CardSourcePin,
    FindingReview,
    FindingStatus,
    ModelRun,
    OracleEvidence,
    ReasoningConfig,
)
from draftomen.semantic_relationship_records import (
    CardRelationship,
    RelationshipParticipant,
    RelationshipPrerequisite,
    RelationshipPrerequisiteProjection,
    RelationshipTiming,
    RelationshipZone,
)
from draftomen.semantic_roles import (
    CompiledRoleProfile,
    ProfileCard,
    Role,
    RoleAssignment,
)
from draftomen.set_profile import (
    EnhancementCardData,
    PairProfile,
    ProfileMaturity,
    SampleSummary,
    SetProfile,
    SetProfileEnhancement,
    SourceMetadata,
    dump_set_profile,
    load_scoring_profile,
)

ACCOUNT_ID = "account-a"
DRAFT_ID = "draft-a"
EVENT_NAME = "QuickDraft_ABC_20260727"
SET_CODE = "ABC"

RELATIONSHIP_SET_CODE = "tst"
RELATIONSHIP_SOURCE_ID = 901
RELATIONSHIP_TARGET_ID = 902
RELATIONSHIP_MECHANISM = "token-go-wide-payoff"
RELATIONSHIP_RUN_ID = "run-audit-relationship"
RELATIONSHIP_FINDING_ID = (
    "relationship:token-go-wide-payoff:901:capability-audit-enabler"
    ":902:capability-audit-payoff"
)
RELATIONSHIP_SOURCE_NAME = "Audit Enabler"
RELATIONSHIP_TARGET_NAME = "Audit Payoff"
RELATIONSHIP_SOURCE_TEXT = "Create two 1/1 white Soldier creature tokens."
RELATIONSHIP_TARGET_TEXT = "Creatures you control get +1/+1."
RELATIONSHIP_SOURCE_PREREQUISITE = (
    "source:condition/create/token;types=all_of:creature;token=token;subtype=soldier;"
    "color=exact:W;controller=you;qty=exactly/2;zones=none->battlefield/you"
)
RELATIONSHIP_TARGET_PREREQUISITE = (
    "target:condition/control/permanent;types=all_of:creature;controller=you"
)
SENTINEL_CLAIM = "RELATIONSHIP-CLAIM-SENTINEL"
SENTINEL_LEGACY_PREREQUISITE = "RELATIONSHIP-LEGACY-PREREQUISITE-SENTINEL"
SENTINEL_MODEL_RUN = "RELATIONSHIP-MODEL-RUN-SENTINEL"
SENTINEL_ORACLE_QUOTE = "RELATIONSHIP-ORACLE-QUOTE-SENTINEL is not part of the quoted ability."


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


def test_audit_persists_bounded_relationship_score_provenance(
    tmp_path: Path,
) -> None:
    profile = _relationship_profile(tmp_path)
    enhancement = profile.enhancement
    assert enhancement is not None
    relationship = enhancement.relationships[0]
    assert relationship.claim == SENTINEL_CLAIM
    assert relationship.prerequisites == (SENTINEL_LEGACY_PREREQUISITE,)
    assert enhancement.runs[0].provider == SENTINEL_MODEL_RUN
    database = _relationship_card_database()
    engine = PickEngine(set_profile=profile)
    scored_pack = engine.score_pack(
        offered_grp_ids=(RELATIONSHIP_TARGET_ID,),
        card_database=database,
        pool_grp_ids=(RELATIONSHIP_SOURCE_ID,),
        pick_index=1,
    )
    scored_target = next(
        card
        for card in scored_pack.cards
        if card.card.grp_id == RELATIONSHIP_TARGET_ID
    )
    assert scored_target.contextual_breakdown.synergy > 0.0
    assert len(scored_target.relationship_contributions) == 1
    contribution = scored_target.relationship_contributions[0]
    assert contribution.support.finding_id == RELATIONSHIP_FINDING_ID
    assert contribution.raw_contribution > 0.0
    assert contribution.effective_contribution == 0.0

    # The same offered target keeps no relationship support once its drafted
    # source is absent from the pre-pick pool.
    control_pack = engine.score_pack(
        offered_grp_ids=(RELATIONSHIP_TARGET_ID,),
        card_database=database,
        pick_index=1,
    )
    assert control_pack.role_ledger is not None
    assert control_pack.role_ledger.relationship_support == ()
    assert all(
        not item.startswith("relationship ")
        for item in control_pack.cards[0].contextual_evidence
    )

    store = DraftAuditStore(app_dir=tmp_path, clock=_fixed_clock)
    store.record_decision(
        state=_draft_state(),
        event=_pack_event(
            offered_grp_ids=(RELATIONSHIP_TARGET_ID,),
            pool_grp_ids=(RELATIONSHIP_SOURCE_ID,),
        ),
        scored_pack=scored_pack,
        config=engine.config,
        ratings_data=engine.ratings_data,
    )

    records = load_draft_audit_records(
        account_id=ACCOUNT_ID,
        draft_id=DRAFT_ID,
        app_dir=tmp_path,
    )
    assert [record["record_type"] for record in records] == ["decision_evaluated"]
    decision = records[0]
    assert decision["pool_before_pick"] == [RELATIONSHIP_SOURCE_ID]
    assert scored_pack.role_ledger is not None
    support = scored_pack.role_ledger.relationship_support[0]
    assert decision["role_ledger"]["relationship_support"] == [support.to_json()]
    assert support.outcome.value == "supported"
    assert support.profile_fingerprint == profile.fingerprint
    assert support.source_prerequisites
    assert support.target_prerequisites

    candidate = next(
        candidate
        for candidate in decision["candidates"]
        if candidate["grp_id"] == RELATIONSHIP_TARGET_ID
    )
    assert candidate["scoring"]["contextual_evidence"] == list(
        scored_target.contextual_evidence
    )
    assert candidate["scoring"]["relationship_contributions"] == [
        contribution.to_json()
    ]
    assert candidate["scoring"]["contextual_breakdown"]["synergy"] == (
        scored_target.contextual_breakdown.synergy
    )
    assert candidate["concise_explanation"] == render_pick_rationale_concise(
        scored_card=scored_target
    )
    assert candidate["explanation"] == render_pick_rationale_detailed(
        scored_card=scored_target
    )
    assert "Confirmed relationship support" not in candidate["explanation"]

    recommendation = decision["recommendation"]
    assert recommendation["grp_id"] == RELATIONSHIP_TARGET_ID
    assert recommendation["contextual_evidence"] == (
        candidate["scoring"]["contextual_evidence"]
    )
    assert recommendation["relationship_contributions"] == [
        contribution.to_json()
    ]
    assert recommendation["concise_explanation"] == candidate["concise_explanation"]
    assert recommendation["explanation"] == candidate["explanation"]

    # Raw claim, legacy prerequisite, and model-run prose never reach the record.
    serialized = json.dumps(decision)
    for sentinel in (SENTINEL_CLAIM, SENTINEL_LEGACY_PREREQUISITE, SENTINEL_MODEL_RUN):
        assert sentinel not in serialized

    # The offered card's Oracle text stays in candidate metadata and out of
    # every relationship-derived ledger, evidence, rationale, or explanation field.
    assert SENTINEL_ORACLE_QUOTE in candidate["metadata"]["oracle_text"]
    relationship_fields = (
        decision["role_ledger"]["relationship_support"],
        candidate["scoring"]["contextual_evidence"],
        candidate["scoring"]["relationship_contributions"],
        candidate["rationale"],
        candidate["concise_explanation"],
        candidate["explanation"],
        recommendation["contextual_evidence"],
        recommendation["relationship_contributions"],
        recommendation["rationale"],
        recommendation["concise_explanation"],
        recommendation["explanation"],
    )
    for field in relationship_fields:
        assert SENTINEL_ORACLE_QUOTE not in json.dumps(field)


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


def _pack_event(
    *,
    offered_grp_ids: tuple[int, ...] = (101, 102),
    pool_grp_ids: tuple[int, ...] = (),
) -> PackOfferedEvent:
    return PackOfferedEvent(
        event_name=EVENT_NAME,
        set_code=SET_CODE,
        pack_number=0,
        pick_number=0,
        offered_grp_ids=offered_grp_ids,
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


def _relationship_card_database() -> CardDatabase:
    """Return the exact frozen cards of the confirmed relationship fixture."""
    return CardDatabase(
        cards={
            RELATIONSHIP_SOURCE_ID: CardInfo(
                grp_id=RELATIONSHIP_SOURCE_ID,
                name=RELATIONSHIP_SOURCE_NAME,
                colors=("W",),
                mana_value=2.0,
                rarity="uncommon",
                types=("Creature",),
                oracle_text=RELATIONSHIP_SOURCE_TEXT,
                set_code=RELATIONSHIP_SET_CODE,
            ),
            RELATIONSHIP_TARGET_ID: CardInfo(
                grp_id=RELATIONSHIP_TARGET_ID,
                name=RELATIONSHIP_TARGET_NAME,
                colors=("W",),
                mana_value=4.0,
                rarity="rare",
                types=("Creature",),
                oracle_text=f"{RELATIONSHIP_TARGET_TEXT}\n{SENTINEL_ORACLE_QUOTE}",
                set_code=RELATIONSHIP_SET_CODE,
            ),
        }
    )


def _relationship_token_clause() -> RelationshipPrerequisite:
    """Build the enabler's typed creature-token output clause."""
    return RelationshipPrerequisite(
        kind=PrerequisiteKind.CONDITION,
        subject="output",
        operation="create",
        object_kind="token",
        card_types=("creature",),
        type_operator="all_of",
        token_restriction="token",
        exclusion="none",
        subtype="soldier",
        color_operator="exact",
        colors=("W",),
        controller="you",
        owner="not_applicable",
        quantity=CapabilityQuantity(value=2, relation=QuantityRelation.EXACTLY),
        source_zone=None,
        destination_zone=RelationshipZone(
            zone=CapabilityZone.BATTLEFIELD,
            player="you",
        ),
        timing=RelationshipTiming(window="unrestricted", turn="any", max_per_turn=None),
        required_card_id=None,
        evidence=OracleEvidence(
            card_id=RELATIONSHIP_SOURCE_ID,
            face_index=None,
            quote=RELATIONSHIP_SOURCE_TEXT,
        ),
        operation_quote="Create",
        operation_occurrence=0,
        object_quote="two 1/1 white Soldier creature tokens",
        object_occurrence=0,
        capability_prerequisite_indices=(),
    )


def _relationship_wide_payoff_clause() -> RelationshipPrerequisite:
    """Build the payoff's typed creature-control clause."""
    return RelationshipPrerequisite(
        kind=PrerequisiteKind.CONDITION,
        subject="participant",
        operation="control",
        object_kind="permanent",
        card_types=("creature",),
        type_operator="all_of",
        token_restriction="unrestricted",
        exclusion="none",
        subtype=None,
        color_operator="unrestricted",
        colors=(),
        controller="you",
        owner="not_applicable",
        quantity=None,
        source_zone=None,
        destination_zone=None,
        timing=RelationshipTiming(window="unrestricted", turn="any", max_per_turn=None),
        required_card_id=None,
        evidence=OracleEvidence(
            card_id=RELATIONSHIP_TARGET_ID,
            face_index=None,
            quote=RELATIONSHIP_TARGET_TEXT,
        ),
        operation_quote="control",
        operation_occurrence=0,
        object_quote="Creatures you control",
        object_occurrence=0,
        capability_prerequisite_indices=(),
    )


def _relationship_record() -> CardRelationship:
    """Build the accepted directional relationship of the fixture pair."""
    source, target = _relationship_participants()
    return CardRelationship(
        finding_id=RELATIONSHIP_FINDING_ID,
        mechanism=RELATIONSHIP_MECHANISM,
        participants=(RELATIONSHIP_SOURCE_ID, RELATIONSHIP_TARGET_ID),
        claim=SENTINEL_CLAIM,
        prerequisites=(SENTINEL_LEGACY_PREREQUISITE,),
        oracle_evidence=(
            OracleEvidence(
                card_id=RELATIONSHIP_SOURCE_ID,
                face_index=None,
                quote=RELATIONSHIP_SOURCE_TEXT,
            ),
            OracleEvidence(
                card_id=RELATIONSHIP_TARGET_ID,
                face_index=None,
                quote=RELATIONSHIP_TARGET_TEXT,
            ),
        ),
        guide_evidence=(),
        review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
        run_id=RELATIONSHIP_RUN_ID,
        prerequisite_projection=RelationshipPrerequisiteProjection(
            source=source,
            target=target,
        ),
    )


def _relationship_participants() -> tuple[RelationshipParticipant, RelationshipParticipant]:
    """Build both pinned participants of the confirmed relationship."""
    database = _relationship_card_database()
    source_card = database.cards[RELATIONSHIP_SOURCE_ID]
    target_card = database.cards[RELATIONSHIP_TARGET_ID]
    source = RelationshipParticipant(
        card_id=RELATIONSHIP_SOURCE_ID,
        capability_id="capability-audit-enabler",
        card_name=RELATIONSHIP_SOURCE_NAME,
        face_index=None,
        face_name=None,
        card_source_sha256=card_source_sha256(source_card),
        role=Role.TOKEN_MAKER,
        capability_prerequisites=(),
        prerequisites=(_relationship_token_clause(),),
    )
    target = RelationshipParticipant(
        card_id=RELATIONSHIP_TARGET_ID,
        capability_id="capability-audit-payoff",
        card_name=RELATIONSHIP_TARGET_NAME,
        face_index=None,
        face_name=None,
        card_source_sha256=card_source_sha256(target_card),
        role=Role.GO_WIDE_PAYOFF,
        capability_prerequisites=(),
        prerequisites=(_relationship_wide_payoff_clause(),),
    )
    return source, target


def _relationship_set_profile() -> SetProfile:
    """Build one confirmed schema-three profile carrying the typed relationship."""
    database = _relationship_card_database()
    return SetProfile(
        set_code=RELATIONSHIP_SET_CODE,
        event_format="quickdraft",
        profile_version="relationship-audit-test",
        generated_at="1970-01-01T00:00:00+00:00",
        source=SourceMetadata(provider="test"),
        maturity=ProfileMaturity.MATURE,
        samples=SampleSummary(total=1, by_pair=(("WU", 1),)),
        confidence=1.0,
        pairs=(PairProfile(pair="WU"),),
        role_profile=CompiledRoleProfile(
            set_code=RELATIONSHIP_SET_CODE,
            cards=(
                ProfileCard(
                    key=f"arena_id:{RELATIONSHIP_SOURCE_ID}",
                    card_name=RELATIONSHIP_SOURCE_NAME,
                    assignments=(RoleAssignment(Role.TOKEN_MAKER, confidence=0.9),),
                ),
                ProfileCard(
                    key=f"arena_id:{RELATIONSHIP_TARGET_ID}",
                    card_name=RELATIONSHIP_TARGET_NAME,
                    assignments=(
                        RoleAssignment(Role.GO_WIDE_PAYOFF, confidence=0.8),
                    ),
                ),
            ),
        ),
        schema_version=3,
        enhancement=SetProfileEnhancement(
            artifact_schema_version=SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
            artifact_sha256="a" * 64,
            set_code=RELATIONSHIP_SET_CODE,
            set_source_id="audit-relationship-source",
            set_source_sha256="b" * 64,
            created_at="2026-07-27T09:00:00+00:00",
            card_data=EnhancementCardData(
                source="audit-relationship-source",
                sha256="b" * 64,
                card_count=2,
            ),
            cards=tuple(
                CardSourcePin(
                    card_id=card.grp_id,
                    oracle_id=None,
                    collector_number=None,
                    sha256=card_source_sha256(card),
                )
                for card in database.cards.values()
            ),
            guides=(),
            runs=(
                ModelRun(
                    run_id=RELATIONSHIP_RUN_ID,
                    provider=SENTINEL_MODEL_RUN,
                    model="audit-model",
                    reasoning=ReasoningConfig(
                        enabled=None,
                        effort=None,
                        max_tokens=None,
                        exclude=None,
                    ),
                    prompt_id="prompt-audit-relationship",
                    prompt_sha256="c" * 64,
                    response_schema_id="schema-audit-relationship",
                    response_schema_sha256="d" * 64,
                    started_at="2026-07-27T08:00:00+00:00",
                    completed_at="2026-07-27T08:05:00+00:00",
                    input_tokens=None,
                    output_tokens=None,
                    reasoning_tokens=None,
                    cost_usd=None,
                ),
            ),
            mechanics=(),
            relationships=(_relationship_record(),),
            review=ArtifactReview(
                state="confirmed",
                reviewer_id="reviewer-a",
                reviewed_at="2026-07-27T09:30:00+00:00",
            ),
            confidence=0.9,
        ),
    )


def _relationship_profile(tmp_path: Path) -> SetProfile:
    """Dump the fixture profile and reload it through the public loader."""
    path = dump_set_profile(
        _relationship_set_profile(),
        tmp_path / f"{RELATIONSHIP_SET_CODE}-quickdraft.json",
    )
    profile = load_scoring_profile(
        RELATIONSHIP_SET_CODE,
        "quickdraft",
        profile_path=path,
    )
    assert profile is not None
    return profile
