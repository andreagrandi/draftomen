"""Behavior tests for UI-neutral resumable set-enrichment orchestration."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from typing import Any

import pytest

from draftomen.carddb import CardFace, CardInfo
from draftomen.openrouter_client import OpenRouterResponse
from draftomen.semantic_capability_records import (
    CapabilityPrerequisite,
    CapabilityQuantity,
    CapabilityZone,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import (
    EnrichmentSources,
    GuideSource,
    card_source_sha256,
    set_source_sha256,
)
from draftomen.semantic_enrichment_records import FindingStatus, OracleEvidence
from draftomen.set_enrichment import (
    Completion,
    EnrichmentOutcome,
    EnrichmentPhase,
    EnrichmentProgress,
    EnrichmentRunResult,
    run_set_enrichment,
)
from draftomen.set_enrichment_extraction import (
    CARD_CAPABILITY_EXTRACTION_PROMPT_ID,
    GUIDE_EXTRACTION_PROMPT_ID,
    RELATIONSHIP_VALIDATION_PROMPT_ID,
    ExtractionOutcome,
    ExtractionRequest,
    build_card_capability_extraction_request,
    build_guide_extraction_request,
    parse_guide_extraction_response,
    relationship_subject_id,
)
from draftomen.set_enrichment_work import (
    SetEnrichmentWorkStore,
    WorkIdentity,
    WorkKind,
    WorkModelConfig,
    WorkState,
    build_work_identity,
)


SET_CODE = "tst"
GUIDE_ID = "guide-1"
GUIDE_URL = "https://guides.example.test/tst-review"
RETRIEVED_AT = "2026-09-01T12:00:00Z"
RUN_ID = "run-1"

GUIDE_FINDING_ID = "claim-token-plan"
GUIDE_SENTENCE = "Token decks in this format win by going wide before the ground stalls."
GUIDE_CLAIM = "Token decks win by going wide."
GUIDE_TEXT = GUIDE_SENTENCE + "\n"

TOKEN_CARD_ID = 301
TOKEN_CARD_NAME = "Token Maker"
TOKEN_QUOTE = "Create two 1/1 colorless Soldier artifact creature tokens."
TOKEN_FINDING_ID = "capability-tokens"

WIDE_CARD_ID = 311
WIDE_CARD_NAME = "Wide Payoff"
WIDE_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
WIDE_FINDING_ID = "capability-wide"
WIDE_TIMING = "combat"
WIDE_PREREQUISITE_TIMING = "upkeep"

UNRELATED_CARD_ID = 321
UNRELATED_CARD_NAME = "Unrelated Sage"
UNRELATED_QUOTE = "Draw two cards."
UNRELATED_FINDING_ID = "capability-draw"

INELIGIBLE_CARD_ID = 331
INELIGIBLE_CARD_NAME = "Blank Slate"

MULTIFACE_CARD_ID = 341
MULTIFACE_CARD_NAME = "Alpha // Beta"
MULTIFACE_FRONT_NAME = "Alpha"
MULTIFACE_FRONT_QUOTE = "At the beginning of your upkeep, create a 1/1 white Soldier token."
MULTIFACE_FRONT_FINDING_ID = "capability-alpha"
MULTIFACE_BACK_NAME = "Beta"
MULTIFACE_BACK_QUOTE = "Creatures you control get +1/+1 until end of turn."
MULTIFACE_BACK_FINDING_ID = "capability-beta"
MULTIFACE_CARD_TEXT = f"{MULTIFACE_FRONT_QUOTE} // {MULTIFACE_BACK_QUOTE}"

ELIGIBLE_CARD_IDS = (TOKEN_CARD_ID, WIDE_CARD_ID, UNRELATED_CARD_ID, MULTIFACE_CARD_ID)
RELATIONSHIP_MECHANISM = "token-go-wide-payoff"
RELATIONSHIP_CLAIM = "Tokens feed the go-wide payoff."

MODEL = "vendor/model"
PROVIDER = "vendor"
REASONING_EFFORT = "high"
MAX_TOKENS = 4096
INPUT_TOKENS = 1200
CACHED_INPUT_TOKENS = 300
OUTPUT_TOKENS = 200
REASONING_TOKENS = 40
CALL_COST_USD = "0.002"
TOTAL_COST_USD = "0.016"
MIDRUN_PROJECTED_COST_USD = "0.01"
WORKED_CALLS = 8
RELATIONSHIP_EVIDENCE_QUOTE_REASON = (
    "relationship Oracle evidence quote is not an exact source substring."
)

RESPOND = "respond"
INTERRUPT = "interrupt"
NO_COST = "no-cost"


class _CompletionFailure(RuntimeError):
    """Represent one scripted acquisition failure in the fake completion."""


def _card(
    *,
    grp_id: int,
    name: str,
    oracle_text: str | None,
    faces: tuple[CardFace, ...] = (),
) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=("W",),
        mana_value=2.0,
        rarity="uncommon",
        types=("Creature",),
        oracle_text=oracle_text,
        set_code=SET_CODE,
        faces=faces,
    )


def _card_for(card_id: int) -> CardInfo:
    return next(card for card in _sources().cards if card.grp_id == card_id)


def _guide_source() -> GuideSource:
    return GuideSource(
        guide_id=GUIDE_ID,
        url=GUIDE_URL,
        text=GUIDE_TEXT,
        retrieved_at=RETRIEVED_AT,
    )


def _sources() -> EnrichmentSources:
    return EnrichmentSources(
        set_code=SET_CODE,
        cards=(
            _card(grp_id=TOKEN_CARD_ID, name=TOKEN_CARD_NAME, oracle_text=TOKEN_QUOTE),
            _card(grp_id=WIDE_CARD_ID, name=WIDE_CARD_NAME, oracle_text=WIDE_QUOTE),
            _card(
                grp_id=UNRELATED_CARD_ID,
                name=UNRELATED_CARD_NAME,
                oracle_text=UNRELATED_QUOTE,
            ),
            _card(grp_id=INELIGIBLE_CARD_ID, name=INELIGIBLE_CARD_NAME, oracle_text=None),
            _card(
                grp_id=MULTIFACE_CARD_ID,
                name=MULTIFACE_CARD_NAME,
                oracle_text=MULTIFACE_CARD_TEXT,
                faces=(
                    CardFace(name=MULTIFACE_FRONT_NAME, oracle_text=MULTIFACE_FRONT_QUOTE),
                    CardFace(name=MULTIFACE_BACK_NAME, oracle_text=MULTIFACE_BACK_QUOTE),
                ),
            ),
        ),
        guides=(_guide_source(),),
    )


def _model_config() -> WorkModelConfig:
    return WorkModelConfig(
        model=MODEL,
        reasoning_effort=REASONING_EFFORT,
        max_tokens=MAX_TOKENS,
    )


def _store(work_root: Path) -> SetEnrichmentWorkStore:
    return SetEnrichmentWorkStore(work_root)


def _guide_identity() -> WorkIdentity:
    return build_work_identity(
        work_kind=WorkKind.GUIDE,
        subject_id=GUIDE_ID,
        input_sha256=_sources().guides[0].text_sha256,
        request=build_guide_extraction_request(sources=_sources(), guide_id=GUIDE_ID),
        model_config=_model_config(),
    )


def _card_identity(card_id: int) -> WorkIdentity:
    return build_work_identity(
        work_kind=WorkKind.CARD_CAPABILITY,
        subject_id=str(card_id),
        input_sha256=card_source_sha256(_card_for(card_id)),
        request=build_card_capability_extraction_request(
            sources=_sources(),
            card_id=card_id,
        ),
        model_config=_model_config(),
    )


def _guide_content() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "findings": [
                {
                    "finding_id": GUIDE_FINDING_ID,
                    "category": "format_finding",
                    "name": "Token plan",
                    "claim": GUIDE_CLAIM,
                    "card_ids": [],
                    "evidence": [{"guide_id": GUIDE_ID, "quote": GUIDE_SENTENCE}],
                    "review": {"status": "accepted", "reason": None},
                }
            ],
        }
    )


def _capability_candidate(
    *,
    finding_id: str,
    card_id: int,
    card_name: str,
    role: str,
    quote: str,
    face_index: int | None = None,
    face_name: str | None = None,
    quantity: dict[str, Any] | None = None,
    timing: str | None = None,
    source_zone: str | None = None,
    destination_zone: str | None = None,
    prerequisites: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one scripted capability entry bound to exact Oracle evidence."""
    return {
        "finding_id": finding_id,
        "card_id": card_id,
        "card_name": card_name,
        "face_index": face_index,
        "face_name": face_name,
        "role": role,
        "quantity": quantity,
        "timing": timing,
        "source_zone": source_zone,
        "destination_zone": destination_zone,
        "prerequisites": prerequisites if prerequisites is not None else [],
        "evidence": [{"card_id": card_id, "face_index": face_index, "quote": quote}],
        "review": {"status": "accepted", "reason": None},
    }


def _wide_prerequisite() -> dict[str, Any]:
    return {
        "kind": "threshold",
        "quantity": {"value": 3, "relation": "at_least"},
        "timing": WIDE_PREREQUISITE_TIMING,
        "source_zone": "battlefield",
        "destination_zone": "graveyard",
        "evidence": {"card_id": WIDE_CARD_ID, "face_index": None, "quote": WIDE_QUOTE},
    }


def _capability_candidates(card_id: int) -> list[dict[str, Any]]:
    """Build the scripted capability entries one canonical card answers with."""
    if card_id == TOKEN_CARD_ID:
        return [
            _capability_candidate(
                finding_id=TOKEN_FINDING_ID,
                card_id=TOKEN_CARD_ID,
                card_name=TOKEN_CARD_NAME,
                role="token_maker",
                quote=TOKEN_QUOTE,
            )
        ]
    if card_id == WIDE_CARD_ID:
        return [
            _capability_candidate(
                finding_id=WIDE_FINDING_ID,
                card_id=WIDE_CARD_ID,
                card_name=WIDE_CARD_NAME,
                role="go_wide_payoff",
                quote=WIDE_QUOTE,
                quantity={"value": 2, "relation": "exactly"},
                timing=WIDE_TIMING,
                source_zone="battlefield",
                destination_zone="battlefield",
                prerequisites=[_wide_prerequisite()],
            )
        ]
    if card_id == UNRELATED_CARD_ID:
        return [
            _capability_candidate(
                finding_id=UNRELATED_FINDING_ID,
                card_id=UNRELATED_CARD_ID,
                card_name=UNRELATED_CARD_NAME,
                role="draw",
                quote=UNRELATED_QUOTE,
            )
        ]
    if card_id == MULTIFACE_CARD_ID:
        return [
            _capability_candidate(
                finding_id=MULTIFACE_FRONT_FINDING_ID,
                card_id=MULTIFACE_CARD_ID,
                card_name=MULTIFACE_CARD_NAME,
                role="token_maker",
                quote=MULTIFACE_FRONT_QUOTE,
                face_index=0,
                face_name=MULTIFACE_FRONT_NAME,
            ),
            _capability_candidate(
                finding_id=MULTIFACE_BACK_FINDING_ID,
                card_id=MULTIFACE_CARD_ID,
                card_name=MULTIFACE_CARD_NAME,
                role="go_wide_payoff",
                quote=MULTIFACE_BACK_QUOTE,
                face_index=1,
                face_name=MULTIFACE_BACK_NAME,
            ),
        ]
    raise AssertionError(f"unexpected card capability request for {card_id}.")


def _capability_content(card_id: int) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "capabilities": _capability_candidates(card_id),
        }
    )


def _relationship_content(prompt: dict[str, Any], *, foreign_quote: str | None) -> str:
    """Build one accepted verdict for the single candidate a request describes."""
    source = prompt["source"]
    target = prompt["target"]
    source_quote = source["evidence"][0]["quote"] if foreign_quote is None else foreign_quote
    return json.dumps(
        {
            "schema_version": 1,
            "verdict": "accepted",
            "claim": RELATIONSHIP_CLAIM,
            "reason": None,
            "evidence": [
                {
                    "card_id": source["card_id"],
                    "face_index": source["face_index"],
                    "quote": source_quote,
                },
                {
                    "card_id": target["card_id"],
                    "face_index": target["face_index"],
                    "quote": target["evidence"][0]["quote"],
                },
            ],
        }
    )


def _prompt_payload(request: ExtractionRequest) -> dict[str, Any]:
    return json.loads(request.user_prompt)


def _prompt_subject(request: ExtractionRequest) -> str:
    """Return the durable work subject one pinned request describes."""
    payload = _prompt_payload(request)
    if request.prompt_id == GUIDE_EXTRACTION_PROMPT_ID:
        return payload["guide"]["guide_id"]
    if request.prompt_id == CARD_CAPABILITY_EXTRACTION_PROMPT_ID:
        return str(payload["card"]["card_id"])
    source = payload["source"]
    target = payload["target"]
    return (
        f"relationship:{payload['mechanism']}:{source['card_id']}:{source['finding_id']}"
        f":{target['card_id']}:{target['finding_id']}"
    )


def _completion_content(request: ExtractionRequest, *, foreign_quote: str | None) -> str:
    """Build the scripted fixture content one pinned request answers with."""
    payload = _prompt_payload(request)
    if request.prompt_id == GUIDE_EXTRACTION_PROMPT_ID:
        return _guide_content()
    if request.prompt_id == CARD_CAPABILITY_EXTRACTION_PROMPT_ID:
        return _capability_content(payload["card"]["card_id"])
    return _relationship_content(payload, foreign_quote=foreign_quote)


class _FakeCompletion:
    """Answer pinned requests deterministically and record every call."""

    def __init__(
        self,
        *,
        behaviours: Sequence[str] = (),
        fail_after: int | None = None,
        foreign_relationship_quote: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.requests: list[ExtractionRequest] = []
        self.behaviours = behaviours
        self.fail_after = fail_after
        self.foreign_relationship_quote = foreign_relationship_quote

    def __call__(self, request: ExtractionRequest) -> OpenRouterResponse:
        """Answer one pinned request, raising once the scripted failure count is reached."""
        self.calls.append((request.prompt_id, _prompt_subject(request)))
        self.requests.append(request)
        index = len(self.calls) - 1
        if self.fail_after is not None and index >= self.fail_after:
            raise _CompletionFailure("scripted acquisition failure.")
        behaviour = self.behaviours[index] if index < len(self.behaviours) else RESPOND
        if behaviour == INTERRUPT:
            raise KeyboardInterrupt
        content = _completion_content(request, foreign_quote=self.foreign_relationship_quote)
        if behaviour == NO_COST:
            return OpenRouterResponse(
                content=content,
                model=MODEL,
                provider=None,
                input_tokens=None,
                cached_input_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
                cost_usd=None,
            )
        return OpenRouterResponse(
            content=content,
            model=MODEL,
            provider=PROVIDER,
            input_tokens=INPUT_TOKENS,
            cached_input_tokens=CACHED_INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            reasoning_tokens=REASONING_TOKENS,
            cost_usd=CALL_COST_USD,
        )

    def calls_for(self, prompt_id: str) -> list[tuple[str, str]]:
        """Return every recorded call made for one pinned prompt."""
        return [call for call in self.calls if call[0] == prompt_id]


def _run(
    *,
    work_root: Path,
    completion: Completion,
    observer: Callable[[EnrichmentProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> EnrichmentRunResult:
    """Run one analysis against a caller-owned work root."""
    return run_set_enrichment(
        sources=_sources(),
        complete=completion,
        work_store=_store(work_root),
        model_config=_model_config(),
        run_id=RUN_ID,
        observer=observer,
        is_cancelled=is_cancelled,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    """Map every file under one root to its exact bytes."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_full_run_executes_every_phase_and_reports_pending_review(tmp_path: Path) -> None:
    events: list[EnrichmentProgress] = []
    completion = _FakeCompletion()
    result = _run(work_root=tmp_path / "work", completion=completion, observer=events.append)

    assert result.outcome is EnrichmentOutcome.COMPLETE
    assert result.complete is True
    assert result.review.state == "pending"
    assert result.review.reviewer_id is None
    assert result.review.reviewed_at is None
    assert result.run_id == RUN_ID
    assert result.set_code == SET_CODE
    assert result.set_source_sha256 == set_source_sha256(_sources())

    entries = [
        event
        for index, event in enumerate(events)
        if index == 0 or event.phase is not events[index - 1].phase
    ]
    assert [event.phase for event in entries] == [
        EnrichmentPhase.GUIDES,
        EnrichmentPhase.CARD_CAPABILITIES,
        EnrichmentPhase.CANDIDATES,
        EnrichmentPhase.RELATIONSHIPS,
    ]
    assert [event.guides_completed for event in entries] == [0, 1, 1, 1]
    assert [event.cards_completed for event in entries] == [0, 0, 4, 4]
    assert [event.relationships_total for event in entries] == [0, 0, 0, 3]
    assert [event.relationships_completed for event in entries] == [0, 0, 0, 0]

    guide_events = [event for event in events if event.phase is EnrichmentPhase.GUIDES]
    card_events = [event for event in events if event.phase is EnrichmentPhase.CARD_CAPABILITIES]
    relationship_events = [
        event for event in events if event.phase is EnrichmentPhase.RELATIONSHIPS
    ]
    assert [event.guides_completed for event in guide_events] == [0, 1]
    assert [event.cards_completed for event in card_events] == [0, 1, 2, 3, 4]
    assert [event.relationships_completed for event in relationship_events] == [0, 1, 2, 3, 3]
    assert len(events) == 14
    assert events[-1] == result.progress
    assert result.progress.guides_completed == 1
    assert result.progress.cards_completed == 4
    assert result.progress.relationships_completed == 3


def test_failed_acquisition_propagates_without_completing_the_run(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    completion = _FakeCompletion(fail_after=1)
    with pytest.raises(_CompletionFailure):
        _run(work_root=work_root, completion=completion)

    store = _store(work_root)
    assert store.lookup(identity=_guide_identity()).state is WorkState.COMPLETED
    assert store.lookup(identity=_card_identity(TOKEN_CARD_ID)).state is WorkState.INCOMPLETE

    resumed = _run(work_root=work_root, completion=_FakeCompletion())
    assert resumed.outcome is EnrichmentOutcome.COMPLETE


def test_uninterrupted_run_attempts_each_eligible_card_once(tmp_path: Path) -> None:
    completion = _FakeCompletion()
    result = _run(work_root=tmp_path / "work", completion=completion)

    assert [subject for _, subject in completion.calls_for(GUIDE_EXTRACTION_PROMPT_ID)] == [
        GUIDE_ID
    ]
    assert [subject for _, subject in completion.calls_for(CARD_CAPABILITY_EXTRACTION_PROMPT_ID)] == [
        str(card_id) for card_id in ELIGIBLE_CARD_IDS
    ]
    assert result.card_ids == ELIGIBLE_CARD_IDS
    assert result.ineligible_card_ids == (INELIGIBLE_CARD_ID,)
    assert all(call[1] != str(INELIGIBLE_CARD_ID) for call in completion.calls)

    multiface = result.card_results[-1]
    assert multiface.outcome is ExtractionOutcome.SUCCESS
    assert [capability.face_index for capability in multiface.capabilities] == [0, 1]
    assert len(completion.calls_for(CARD_CAPABILITY_EXTRACTION_PROMPT_ID)) == len(
        ELIGIBLE_CARD_IDS
    )
    assert len(completion.calls) == WORKED_CALLS


def test_resumed_run_reuses_durable_work_without_replacement_calls(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    first = _run(work_root=work_root, completion=_FakeCompletion())
    completion = _FakeCompletion()
    second = _run(work_root=work_root, completion=completion)

    assert completion.calls == []
    assert second.outcome is EnrichmentOutcome.COMPLETE
    assert second.guide_results == first.guide_results
    assert second.card_results == first.card_results
    assert second.relationship_results == first.relationship_results
    assert second.progress.accounting.reused_work == WORKED_CALLS
    assert second.progress.accounting.executed_work == 0


def test_unvalidated_durable_response_is_reparsed_without_a_request(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    store = _store(work_root)
    identity = _guide_identity()
    store.record_attempt(identity=identity)
    store.record_response(
        identity=identity,
        response=OpenRouterResponse(
            content=_guide_content(),
            model=MODEL,
            provider=PROVIDER,
            input_tokens=INPUT_TOKENS,
            cached_input_tokens=CACHED_INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            reasoning_tokens=REASONING_TOKENS,
            cost_usd=CALL_COST_USD,
        ),
    )
    assert store.lookup(identity=identity).state is WorkState.UNVALIDATED

    completion = _FakeCompletion()
    result = _run(work_root=work_root, completion=completion)

    assert result.outcome is EnrichmentOutcome.COMPLETE
    assert completion.calls_for(GUIDE_EXTRACTION_PROMPT_ID) == []
    assert result.guide_results[0] == parse_guide_extraction_response(
        content=_guide_content(),
        sources=_sources(),
        guide_id=GUIDE_ID,
        run_id=RUN_ID,
    )
    assert store.lookup(identity=identity).state is WorkState.COMPLETED
    assert result.progress.accounting.reused_work == 1


def test_relationship_calls_cover_every_constructed_candidate(tmp_path: Path) -> None:
    completion = _FakeCompletion()
    result = _run(work_root=tmp_path / "work", completion=completion)

    assert result.candidate_packages is not None
    packages = result.candidate_packages.packages
    assert len(packages) == 3
    expected_subjects = [
        relationship_subject_id(
            mechanism=package.mechanism,
            source=package.source,
            target=package.target,
        )
        for package in packages
    ]
    assert [
        subject for _, subject in completion.calls_for(RELATIONSHIP_VALIDATION_PROMPT_ID)
    ] == expected_subjects

    relationship_requests = [
        request
        for request in completion.requests
        if request.prompt_id == RELATIONSHIP_VALIDATION_PROMPT_ID
    ]
    assert len(relationship_requests) == len(packages)
    for request, package in zip(relationship_requests, packages):
        prompt = _prompt_payload(request)
        assert prompt["mechanism"] == package.mechanism
        assert prompt["source"]["card_id"] == package.source.card_id
        assert prompt["source"]["finding_id"] == package.source.finding_id
        assert prompt["target"]["card_id"] == package.target.card_id
        assert prompt["target"]["finding_id"] == package.target.finding_id


def test_accepted_relationship_preserves_candidate_capability_content(tmp_path: Path) -> None:
    result = _run(work_root=tmp_path / "work", completion=_FakeCompletion())

    assert result.candidate_packages is not None
    packages = result.candidate_packages.packages
    assert len(result.relationship_results) == len(packages)
    for item, package in zip(result.relationship_results, packages):
        relationship = item.relationship
        assert relationship is not None
        assert item.rejected is None
        assert relationship.identity == package.identity
        assert relationship.mechanism == package.mechanism
        assert relationship.source == package.source
        assert relationship.target == package.target
        assert relationship.review.status is FindingStatus.ACCEPTED
        assert relationship.review.reason is None
        assert relationship.evidence == tuple(
            sorted(
                (
                    OracleEvidence(
                        card_id=package.source.card_id,
                        face_index=package.source.face_index,
                        quote=package.source.evidence[0].quote,
                    ),
                    OracleEvidence(
                        card_id=package.target.card_id,
                        face_index=package.target.face_index,
                        quote=package.target.evidence[0].quote,
                    ),
                ),
                key=lambda item: (
                    item.card_id,
                    -1 if item.face_index is None else item.face_index,
                    item.quote,
                ),
            )
        )

    wide_relationships = [
        item.relationship
        for item in result.relationship_results
        if item.relationship is not None and item.relationship.target.card_id == WIDE_CARD_ID
    ]
    assert len(wide_relationships) == 2
    for relationship in wide_relationships:
        assert relationship is not None
        assert relationship.target.quantity == CapabilityQuantity(
            value=2,
            relation=QuantityRelation.EXACTLY,
        )
        assert relationship.target.timing == WIDE_TIMING
        assert relationship.target.source_zone is CapabilityZone.BATTLEFIELD
        assert relationship.target.destination_zone is CapabilityZone.BATTLEFIELD
        assert relationship.target.prerequisites == (
            CapabilityPrerequisite(
                kind=PrerequisiteKind.THRESHOLD,
                quantity=CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST),
                timing=WIDE_PREREQUISITE_TIMING,
                source_zone=CapabilityZone.BATTLEFIELD,
                destination_zone=CapabilityZone.GRAVEYARD,
                evidence=OracleEvidence(card_id=WIDE_CARD_ID, face_index=None, quote=WIDE_QUOTE),
            ),
        )
        assert relationship.target.evidence == (
            OracleEvidence(card_id=WIDE_CARD_ID, face_index=None, quote=WIDE_QUOTE),
        )


def test_foreign_relationship_evidence_becomes_a_rejected_diagnostic(tmp_path: Path) -> None:
    completion = _FakeCompletion(foreign_relationship_quote=UNRELATED_QUOTE)
    result = _run(work_root=tmp_path / "work", completion=completion)

    assert result.outcome is EnrichmentOutcome.COMPLETE
    assert len(result.relationship_results) == 3
    assert all(item.relationship is None for item in result.relationship_results)
    assert {item.rejected.reason for item in result.relationship_results if item.rejected} == {
        RELATIONSHIP_EVIDENCE_QUOTE_REASON
    }
    assert result.progress.valid_count == 0
    assert result.progress.rejected_count == 3


def test_progress_events_expose_counters_percentages_and_costs(tmp_path: Path) -> None:
    events: list[EnrichmentProgress] = []
    result = _run(
        work_root=tmp_path / "work",
        completion=_FakeCompletion(),
        observer=events.append,
    )

    card_events = [event for event in events if event.phase is EnrichmentPhase.CARD_CAPABILITIES]
    relationship_events = [
        event for event in events if event.phase is EnrichmentPhase.RELATIONSHIPS
    ]
    assert [event.cards_percent for event in card_events] == [0.0, 25.0, 50.0, 75.0, 100.0]
    assert [event.guides_percent for event in card_events] == [100.0] * 5
    assert [event.relationships_percent for event in relationship_events] == [
        0.0,
        33.3,
        66.7,
        100.0,
        100.0,
    ]
    assert card_events[1].accounting.projected_final_cost_usd == MIDRUN_PROJECTED_COST_USD

    accounting = result.progress.accounting
    assert accounting.executed_work == WORKED_CALLS
    assert accounting.reused_work == 0
    assert accounting.work_without_cost == 0
    assert accounting.input_tokens == INPUT_TOKENS * WORKED_CALLS
    assert accounting.cached_input_tokens == CACHED_INPUT_TOKENS * WORKED_CALLS
    assert accounting.output_tokens == OUTPUT_TOKENS * WORKED_CALLS
    assert accounting.reasoning_tokens == REASONING_TOKENS * WORKED_CALLS
    assert accounting.running_cost_usd == TOTAL_COST_USD
    assert accounting.projected_final_cost_usd == TOTAL_COST_USD

    assert result.progress.valid_count == 3
    assert result.progress.uncertain_count == 6
    assert result.progress.rejected_count == 0
    assert events[-1].guides_percent == 100.0
    assert events[-1].cards_percent == 100.0
    assert events[-1].relationships_percent == 100.0

    with pytest.raises(FrozenInstanceError):
        events[0].cards_completed = 1  # type: ignore[misc]


def test_response_without_cost_keeps_running_cost_and_disables_projection(
    tmp_path: Path,
) -> None:
    completion = _FakeCompletion(behaviours=(NO_COST,))
    result = _run(work_root=tmp_path / "work", completion=completion)

    accounting = result.progress.accounting
    assert accounting.executed_work == WORKED_CALLS
    assert accounting.work_without_cost == 1
    assert accounting.input_tokens == INPUT_TOKENS * (WORKED_CALLS - 1)
    assert accounting.running_cost_usd == "0.014"
    assert accounting.projected_final_cost_usd is None


def test_resumed_run_charges_reused_work_once(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    first = _run(work_root=work_root, completion=_FakeCompletion())
    completion = _FakeCompletion()
    second = _run(work_root=work_root, completion=completion)

    assert first.progress.accounting.running_cost_usd == TOTAL_COST_USD
    assert second.progress.accounting.running_cost_usd == TOTAL_COST_USD
    assert second.progress.accounting.projected_final_cost_usd == TOTAL_COST_USD
    assert second.progress.accounting.reused_work == WORKED_CALLS
    assert second.progress.accounting.executed_work == 0
    assert completion.calls == []


def test_cancellation_after_first_card_preserves_resumable_work(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    state = {"cancel": False}

    def observer(event: EnrichmentProgress) -> None:
        if event.phase is EnrichmentPhase.CARD_CAPABILITIES and event.cards_completed == 1:
            state["cancel"] = True

    result = _run(
        work_root=work_root,
        completion=_FakeCompletion(),
        observer=observer,
        is_cancelled=lambda: state["cancel"],
    )

    assert result.outcome is EnrichmentOutcome.CANCELLED
    assert result.complete is False
    assert result.candidate_packages is None
    assert result.relationship_results == ()
    assert len(result.guide_results) == 1
    assert len(result.card_results) == 1
    assert result.progress.relationships_total == 0

    store = _store(work_root)
    assert store.lookup(identity=_guide_identity()).state is WorkState.COMPLETED
    assert store.lookup(identity=_card_identity(TOKEN_CARD_ID)).state is WorkState.COMPLETED

    resumed = _run(work_root=work_root, completion=_FakeCompletion())
    assert resumed.outcome is EnrichmentOutcome.COMPLETE
    assert resumed.progress.cards_completed == len(ELIGIBLE_CARD_IDS)


def test_keyboard_interrupt_keeps_interrupted_work_resumable(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    completion = _FakeCompletion(behaviours=(RESPOND, INTERRUPT))
    with pytest.raises(KeyboardInterrupt):
        _run(work_root=work_root, completion=completion)

    store = _store(work_root)
    assert store.lookup(identity=_guide_identity()).state is WorkState.COMPLETED
    assert store.lookup(identity=_card_identity(TOKEN_CARD_ID)).state is WorkState.INCOMPLETE

    resumed_completion = _FakeCompletion()
    resumed = _run(work_root=work_root, completion=resumed_completion)
    assert resumed.outcome is EnrichmentOutcome.COMPLETE
    assert resumed.progress.accounting.reused_work == 1
    assert resumed.progress.accounting.executed_work == WORKED_CALLS - 1
    assert resumed_completion.calls[0][0] == CARD_CAPABILITY_EXTRACTION_PROMPT_ID
    assert all(subject != GUIDE_ID for _, subject in resumed_completion.calls)


def test_run_without_compatible_candidates_completes_with_zero_percentages(
    tmp_path: Path,
) -> None:
    sources = EnrichmentSources(
        set_code=SET_CODE,
        cards=(
            _card(
                grp_id=UNRELATED_CARD_ID,
                name=UNRELATED_CARD_NAME,
                oracle_text=UNRELATED_QUOTE,
            ),
        ),
        guides=(_guide_source(),),
    )
    completion = _FakeCompletion()
    result = run_set_enrichment(
        sources=sources,
        complete=completion,
        work_store=_store(tmp_path / "work"),
        model_config=_model_config(),
        run_id=RUN_ID,
    )

    assert result.outcome is EnrichmentOutcome.COMPLETE
    assert result.candidate_packages is not None
    assert result.candidate_packages.packages == ()
    assert result.relationship_results == ()
    assert result.progress.relationships_total == 0
    assert result.progress.relationships_percent == 0.0
    assert result.progress.guides_percent == 100.0
    assert len(completion.calls) == 2


def test_run_writes_only_work_artifacts_and_leaves_profiles_unchanged(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    profile = tmp_path / "profiles" / "tst-profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_bytes(b'{"set_code":"tst"}\n')

    before = _snapshot(tmp_path)
    result = _run(work_root=work_root, completion=_FakeCompletion())
    after = _snapshot(tmp_path)

    assert result.outcome is EnrichmentOutcome.COMPLETE
    assert after["profiles/tst-profile.json"] == before["profiles/tst-profile.json"]
    assert set(before) - set(after) == set()
    changed = {path for path, content in after.items() if before.get(path) != content}
    assert changed
    assert all(Path(path).parts[0] == "work" for path in changed)
