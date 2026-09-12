"""Focused behavioral tests for the UI-neutral set-enrichment workflow."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import Decimal
from datetime import UTC, datetime, timedelta, timezone
from email.message import Message
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any
import gzip

import pytest

import draftomen.set_enrichment_workflow as workflow
from draftomen.card_data_client import CardDataClient, card_data_cache_path
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.guide_client import GuideClient
from draftomen.openrouter_client import OpenRouterResponse
from draftomen.profile_publication import ProfilePublicationError
from draftomen.semantic_enrichment import SemanticEnrichmentArtifact
from draftomen.semantic_enrichment_records import FindingStatus
from draftomen.set_card_data import SetCardData
from draftomen.set_enrichment import EnrichmentOutcome, EnrichmentPhase, EnrichmentProgress
from draftomen.set_enrichment_candidates import CandidateBounds, MAX_PAIR_WORK_OMITTED_REASON
from draftomen.set_enrichment_extraction import (
    CARD_CAPABILITY_EXTRACTION_PROMPT_ID,
    GUIDE_EXTRACTION_PROMPT_ID,
    RELATIONSHIP_VALIDATION_PROMPT_ID,
    ExtractionRequest,
)
from draftomen.set_enrichment_work import WorkIdentity, WorkKind, WorkModelConfig
from draftomen.set_profile import SetProfile


SET_CODE = "TST"
GUIDE_URL = "https://draftsim.com/guides/tst"
SECOND_GUIDE_URL = "https://draftsim.com/guides/tst-second"
GUIDE_TEXT = "Token Maker and Wide Payoff reward going wide."
GUIDE_QUOTE = "reward going wide"
MODEL = "vendor/model"
PROVIDER = "vendor"
RUN_ID = "workflow-test-run"
INPUT_TOKENS = 1200
CACHED_INPUT_TOKENS = 300
OUTPUT_TOKENS = 200
REASONING_TOKENS = 40
CALL_COST = "0.002"
MODEL_CONFIG = WorkModelConfig(model=MODEL, reasoning_effort="high", max_tokens=4096)

TOKEN_ID = 1
TOKEN_NAME = "Token Maker"
TOKEN_QUOTE = "Create two 1/1 white Soldier creature tokens."
WIDE_ID = 2
WIDE_NAME = "Wide Payoff"
WIDE_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
DRAW_ID = 3
DRAW_NAME = "Card Draw"
DRAW_QUOTE = "When this enters the battlefield, draw a card."


class _Response:
    """Small socket-free response used by both injected HTTP clients."""

    def __init__(self, payload: bytes, *, url: str, content_type: str) -> None:
        self._stream = io.BytesIO(payload)
        self.url = url
        self.status = 200
        self.code = 200
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(payload))
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True


def _card(
    *,
    card_id: int,
    name: str,
    oracle_text: str,
) -> CardInfo:
    return CardInfo(
        grp_id=card_id,
        name=name,
        colors=("W",),
        mana_value=2.0,
        rarity="common",
        types=("Creature",),
        mana_cost="{1}{W}",
        oracle_text=oracle_text,
        type_line="Creature — Soldier",
        set_code=SET_CODE.casefold(),
        collector_number=str(card_id),
        arena_id=card_id,
        oracle_id=f"oracle-{card_id}",
        power="2",
        toughness="2",
    )


def _cards() -> tuple[CardInfo, ...]:
    return (
        _card(card_id=TOKEN_ID, name=TOKEN_NAME, oracle_text=TOKEN_QUOTE),
        _card(card_id=WIDE_ID, name=WIDE_NAME, oracle_text=WIDE_QUOTE),
        _card(card_id=DRAW_ID, name=DRAW_NAME, oracle_text=DRAW_QUOTE),
    )


def _database(cards: Sequence[CardInfo] | None = None) -> CardDatabase:
    selected = _cards() if cards is None else tuple(cards)
    return CardDatabase(cards={card.grp_id: card for card in selected})


def _card_payload(cards: Sequence[CardInfo]) -> bytes:
    artifact = SetCardData.from_card_database(
        _database(cards),
        set_code=SET_CODE,
        set_name="Test Set",
    )
    return artifact.to_gzip_bytes()


def _clients(
    output_dir: Path,
    *,
    cards: Sequence[CardInfo],
    card_calls: list[dict[str, Any]] | None = None,
    guide_calls: list[dict[str, Any]] | None = None,
    card_app_dir: Path | None = None,
    guide_failure: BaseException | None = None,
    guide_url: str = GUIDE_URL,
) -> tuple[CardDataClient, GuideClient]:
    payload = _card_payload(cards)
    recorded_card_calls = card_calls if card_calls is not None else []
    recorded_guide_calls = guide_calls if guide_calls is not None else []

    def card_opener(request: Any, *, timeout: float) -> _Response:
        recorded_card_calls.append({"request": request, "timeout": timeout})
        return _Response(
            payload,
            url="https://www.draftomen.com/card-data/tst.json.gz",
            content_type="application/gzip",
        )

    def guide_opener(request: Any, *, timeout: float) -> _Response:
        recorded_guide_calls.append({"request": request, "timeout": timeout})
        if guide_failure is not None:
            raise guide_failure
        return _Response(
            GUIDE_TEXT.encode("utf-8"),
            url=guide_url,
            content_type="text/plain; charset=utf-8",
        )

    return (
        CardDataClient(
            app_dir=(output_dir / "enrichment-runs") if card_app_dir is None else card_app_dir,
            opener=card_opener,
        ),
        GuideClient(opener=guide_opener),
    )


def _guide_candidate(
    *,
    finding_id: str,
    category: str,
    status: str,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "category": category,
        "name": "Going wide",
        "claim": f"{TOKEN_NAME} and {WIDE_NAME} reward going wide.",
        "card_ids": [TOKEN_ID, WIDE_ID],
        "evidence": [{"guide_id": f"{SET_CODE.casefold()}-draftsim-guide", "quote": GUIDE_QUOTE}],
        "review": {"status": status, "reason": reason},
    }


def _guide_content(*, category: str = "mechanic", counts: bool = False) -> str:
    findings = [
        _guide_candidate(
            finding_id="guide-mechanic",
            category=category,
            status="accepted",
            reason=None,
        )
    ]
    if counts:
        findings.extend(
            (
                _guide_candidate(
                    finding_id="guide-uncertain",
                    category="strategy",
                    status="uncertain",
                    reason="Needs review.",
                ),
                _guide_candidate(
                    finding_id="guide-rejected",
                    category="format_finding",
                    status="rejected",
                    reason="Not supported by the guide.",
                ),
            )
        )
    return json.dumps({"schema_version": 1, "findings": findings})

def _capability_candidate(
    *,
    finding_id: str,
    card_id: int,
    card_name: str,
    role: str,
    quote: str,
    status: str = "accepted",
    reason: str | None = None,
    quantity: dict[str, Any] | None = None,
    timing: str | None = None,
    source_zone: str | None = None,
    destination_zone: str | None = None,
    evidence_quotes: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "card_id": card_id,
        "card_name": card_name,
        "face_index": None,
        "face_name": None,
        "role": role,
        "quantity": quantity,
        "timing": timing,
        "source_zone": source_zone,
        "destination_zone": destination_zone,
        "prerequisites": [],
        "evidence": [
            {"card_id": card_id, "face_index": None, "quote": value}
            for value in (quote, *evidence_quotes)
        ],
        "review": {"status": status, "reason": reason},
    }


def _capability_content(
    card_id: int,
    *,
    duplicate: bool = False,
    counts: bool = False,
    fidelity: bool = False,
) -> str:
    if card_id == TOKEN_ID:
        if fidelity:
            candidates = [
                _capability_candidate(
                    finding_id="token-capability",
                    card_id=TOKEN_ID,
                    card_name=TOKEN_NAME,
                    role="token_maker",
                    quote=TOKEN_QUOTE,
                    quantity={"value": 2, "relation": "exactly"},
                    timing="on resolution",
                    source_zone="library",
                    destination_zone="battlefield",
                    evidence_quotes=("two 1/1 white Soldier creature tokens",),
                )
            ]
        elif duplicate:
            candidates = [
                _capability_candidate(
                    finding_id="token-a",
                    card_id=TOKEN_ID,
                    card_name=TOKEN_NAME,
                    role="token_maker",
                    quote=TOKEN_QUOTE,
                ),
                _capability_candidate(
                    finding_id="token-b",
                    card_id=TOKEN_ID,
                    card_name=TOKEN_NAME,
                    role="token_maker",
                    quote=TOKEN_QUOTE,
                ),
            ]
        elif counts:
            candidates = [
                _capability_candidate(
                    finding_id="token-accepted",
                    card_id=TOKEN_ID,
                    card_name=TOKEN_NAME,
                    role="token_maker",
                    quote=TOKEN_QUOTE,
                ),
                _capability_candidate(
                    finding_id="token-uncertain",
                    card_id=TOKEN_ID,
                    card_name=TOKEN_NAME,
                    role="token_maker",
                    quote=TOKEN_QUOTE,
                    status="uncertain",
                    reason="Needs review.",
                ),
                _capability_candidate(
                    finding_id="token-rejected",
                    card_id=TOKEN_ID,
                    card_name=TOKEN_NAME,
                    role="token_maker",
                    quote=TOKEN_QUOTE,
                    status="rejected",
                    reason="Not supported.",
                ),
            ]
        else:
            candidates = [
                _capability_candidate(
                    finding_id="token-capability",
                    card_id=TOKEN_ID,
                    card_name=TOKEN_NAME,
                    role="token_maker",
                    quote=TOKEN_QUOTE,
                )
            ]
    elif card_id == WIDE_ID:
        candidates = [
            _capability_candidate(
                finding_id="wide-capability" if not duplicate else "wide-a",
                card_id=WIDE_ID,
                card_name=WIDE_NAME,
                role="go_wide_payoff",
                quote=WIDE_QUOTE,
            )
        ]
        if duplicate:
            candidates.append(
                _capability_candidate(
                    finding_id="wide-b",
                    card_id=WIDE_ID,
                    card_name=WIDE_NAME,
                    role="go_wide_payoff",
                    quote=WIDE_QUOTE,
                )
            )
    elif card_id == DRAW_ID:
        candidates = [
            _capability_candidate(
                finding_id="draw-capability",
                card_id=DRAW_ID,
                card_name=DRAW_NAME,
                role="draw",
                quote=DRAW_QUOTE,
            )
        ]
    else:
        raise AssertionError(f"unexpected card id {card_id}")
    return json.dumps({"schema_version": 1, "capabilities": candidates})


def _relationship_content(request: dict[str, Any], *, status: str) -> str:
    source = request["source"]
    target = request["target"]
    if status == "malformed":
        return "{malformed"
    reason = None if status == "accepted" else "The interaction needs review."
    return json.dumps(
        {
            "schema_version": 1,
            "verdict": status,
            "claim": "The token engine feeds the wide payoff.",
            "reason": reason,
            "evidence": [
                {
                    "card_id": source["card_id"],
                    "face_index": source["face_index"],
                    "quote": source["evidence"][0]["quote"],
                },
                {
                    "card_id": target["card_id"],
                    "face_index": target["face_index"],
                    "quote": target["evidence"][0]["quote"],
                },
            ],
        }
    )


class _ProviderFailure(RuntimeError):
    """Represent one scripted provider failure in the fake completion."""


class _Completion:
    """Deterministic completion callable with controllable review outcomes."""

    def __init__(
        self,
        *,
        duplicate: bool = False,
        counts: bool = False,
        fidelity: bool = False,
        malformed_card_ids: tuple[int, ...] = (),
        guide_category: str = "mechanic",
        relationship_mode: str = "accepted",
        relationship_statuses: tuple[str, ...] = (),
        accepted_pairs: tuple[tuple[str, str], ...] = (),
        null_cost_first: bool = False,
        interrupt_after: int | None = None,
        fail_after: int | None = None,
    ) -> None:
        self.duplicate = duplicate
        self.counts = counts
        self.fidelity = fidelity
        self.malformed_card_ids = malformed_card_ids
        self.guide_category = guide_category
        self.relationship_mode = relationship_mode
        self.relationship_statuses = relationship_statuses
        self.accepted_pairs = accepted_pairs
        self.null_cost_first = null_cost_first
        self.interrupt_after = interrupt_after
        self.fail_after = fail_after
        self.calls: list[ExtractionRequest] = []

    def __call__(self, request: ExtractionRequest) -> OpenRouterResponse:
        index = len(self.calls)
        self.calls.append(request)
        if self.interrupt_after is not None and index >= self.interrupt_after:
            raise KeyboardInterrupt
        if self.fail_after is not None and index >= self.fail_after:
            raise _ProviderFailure("simulated provider failure")
        payload = json.loads(request.user_prompt)
        if request.prompt_id == GUIDE_EXTRACTION_PROMPT_ID:
            content = _guide_content(category=self.guide_category, counts=self.counts)
        elif request.prompt_id == CARD_CAPABILITY_EXTRACTION_PROMPT_ID:
            if payload["card"]["card_id"] in self.malformed_card_ids:
                content = "{malformed"
            else:
                content = _capability_content(
                    payload["card"]["card_id"],
                    duplicate=self.duplicate,
                    counts=self.counts,
                    fidelity=self.fidelity,
                )
        elif request.prompt_id == RELATIONSHIP_VALIDATION_PROMPT_ID:
            relationship_index = sum(
                call.prompt_id == RELATIONSHIP_VALIDATION_PROMPT_ID for call in self.calls
            ) - 1
            if self.relationship_statuses:
                status = self.relationship_statuses[relationship_index]
            elif self.relationship_mode == "accepted-pair":
                source_id = payload["source"]["finding_id"]
                target_id = payload["target"]["finding_id"]
                status = (
                    "accepted"
                    if (source_id, target_id) in self.accepted_pairs
                    else "uncertain"
                )
            elif self.relationship_mode == "mixed":
                status = "accepted" if len(self.calls) == 5 else "malformed"
            else:
                status = self.relationship_mode
            content = _relationship_content(payload, status=status)
        else:
            raise AssertionError(f"unexpected prompt {request.prompt_id}")
        if self.null_cost_first and index == 0:
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
            cost_usd=CALL_COST,
        )


def _run(
    tmp_path: Path,
    *,
    output_dir: Path | None = None,
    cards: Sequence[CardInfo] | None = None,
    completion: _Completion | None = None,
    card_client: CardDataClient | None = None,
    guide_client: GuideClient | None = None,
    card_calls: list[dict[str, Any]] | None = None,
    guide_calls: list[dict[str, Any]] | None = None,
    observer: Callable[[EnrichmentProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    clock: Callable[[], datetime] | None = None,
    guide_url: str = GUIDE_URL,
) -> workflow.SetEnrichmentWorkflowResult:
    output = tmp_path / "output" if output_dir is None else output_dir
    selected_cards = _cards() if cards is None else tuple(cards)
    complete = _Completion() if completion is None else completion
    if card_client is None or guide_client is None:
        built_card_client, built_guide_client = _clients(
            output,
            cards=selected_cards,
            card_calls=card_calls,
            guide_calls=guide_calls,
            guide_url=guide_url,
        )
        card_client = built_card_client if card_client is None else card_client
        guide_client = built_guide_client if guide_client is None else guide_client
    return workflow.analyze_set_enrichment(
        set_code=SET_CODE,
        guide_url=guide_url,
        output_dir=output,
        model_config=MODEL_CONFIG,
        completion=complete,
        observer=observer,
        is_cancelled=is_cancelled,
        card_data_client=card_client,
        guide_client=guide_client,
        run_id=RUN_ID,
        clock=clock,
    )


def _message(name: str) -> str:
    value = getattr(workflow, name, None)
    if value is None:
        value = getattr(workflow, f"_{name}")
    return value


def _profile_marker(output_dir: Path) -> Path:
    return output_dir / "tst-quickdraft" / "generation.json"


def _profile_snapshot(output_dir: Path) -> dict[str, bytes]:
    profile_root = output_dir / "tst-quickdraft"
    return {
        str(path.relative_to(profile_root)): path.read_bytes()
        for path in sorted(profile_root.rglob("*"))
        if path.is_file()
    }


def _create_metadata_profile(tmp_path: Path, *, output_dir: Path) -> Path:
    result = _run(tmp_path, output_dir=output_dir, completion=_Completion())
    assert result.artifact_path is not None
    pending_path = result.artifact_path
    assert pending_path.exists()
    review = workflow.finalize_set_enrichment(
        analysis=result,
        decision=workflow.EnrichmentReviewDecision.CONFIRM,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )
    assert review.publication is not None
    assert _profile_marker(output_dir).exists()
    return output_dir


def _second_work_dir(output_dir: Path) -> Path:
    guide_key = hashlib.sha256(SECOND_GUIDE_URL.encode("utf-8")).hexdigest()[:16]
    return output_dir / "enrichment-runs" / "tst" / guide_key / "work"


def _assert_under(root: Path, path: Path) -> None:
    path.resolve().relative_to(root.resolve())


def test_cold_acquisition_writes_contained_sources_work_and_card_cache(tmp_path: Path) -> None:
    card_calls: list[dict[str, Any]] = []
    guide_calls: list[dict[str, Any]] = []
    result = _run(tmp_path, card_calls=card_calls, guide_calls=guide_calls)
    root = result.output_dir

    assert len(card_calls) == 1
    assert len(guide_calls) == 1
    assert result.card_database_path.exists()
    assert result.run_dir.exists()
    assert result.work_dir.exists()
    assert result.artifact_path is not None and result.artifact_path.exists()
    cache_path = card_data_cache_path(
        set_code=SET_CODE,
        app_dir=root / "enrichment-runs",
    )
    assert cache_path.exists()
    guide_path = result.run_dir / "sources" / "guide.json"
    assert guide_path.exists()
    assert set(json.loads(guide_path.read_bytes())) == {
        "schema_version",
        "requested_url",
        "guide_id",
        "url",
        "text",
        "sha256",
        "retrieved_at",
    }
    for path in (cache_path, guide_path, result.card_database_path, result.work_dir):
        _assert_under(root, path)
    for path in root.rglob("*"):
        if path.is_file() or path.is_dir():
            _assert_under(root, path)
    for name in ("attempts", "responses", "results"):
        assert (result.work_dir / name).is_dir()


def test_injected_card_client_cache_mismatch_fails_before_network(tmp_path: Path) -> None:
    output = tmp_path / "output"
    card_calls: list[dict[str, Any]] = []
    guide_calls: list[dict[str, Any]] = []
    card_client, guide_client = _clients(
        output,
        cards=_cards(),
        card_calls=card_calls,
        guide_calls=guide_calls,
        card_app_dir=tmp_path / "outside-cache",
    )
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(
            tmp_path,
            output_dir=output,
            card_client=card_client,
            guide_client=guide_client,
            completion=_Completion(),
        )
    assert str(raised.value) == _message("CARD_CACHE_ERROR")
    assert card_calls == []
    assert guide_calls == []


def test_nested_source_work_and_profile_symlinks_fail_without_external_mutation(
    tmp_path: Path,
) -> None:
    for kind in ("source", "work", "profile"):
        case_root = tmp_path / kind
        output = case_root / "output"
        external = case_root / "external"
        external.mkdir(parents=True)
        sentinel = external / "sentinel"
        sentinel.write_bytes(b"authoritative")
        before_external = {
            path.relative_to(external): path.read_bytes()
            for path in external.rglob("*")
            if path.is_file()
        }
        if kind == "source":
            (output / "enrichment-runs").mkdir(parents=True)
            os.symlink(external, output / "enrichment-runs" / "card-data")
        elif kind == "work":
            guide_key = hashlib.sha256(GUIDE_URL.encode("utf-8")).hexdigest()[:16]
            run_dir = output / "enrichment-runs" / "tst" / guide_key
            run_dir.mkdir(parents=True)
            os.symlink(external, run_dir / "work")
        else:
            output.mkdir(parents=True)
            os.symlink(external, output / "tst-quickdraft")
        if kind == "profile":
            result = _run(output, output_dir=output, completion=_Completion())
            with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
                workflow.finalize_set_enrichment(
                    analysis=result,
                    decision=workflow.EnrichmentReviewDecision.CONFIRM,
                    reviewer_id="reviewer",
                    reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
                )
        else:
            with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
                _run(output, output_dir=output, completion=_Completion())
        assert str(raised.value) == _message("CONTAINMENT_ERROR")
        after_external = {
            path.relative_to(external): path.read_bytes()
            for path in external.rglob("*")
            if path.is_file()
        }
        assert after_external == before_external
        assert sentinel.read_bytes() == b"authoritative"


def test_reused_frozen_guide_never_refetches(tmp_path: Path) -> None:
    first_guide_calls: list[dict[str, Any]] = []
    first = _run(tmp_path, guide_calls=first_guide_calls)
    second_guide_calls: list[dict[str, Any]] = []
    second = _run(tmp_path, guide_calls=second_guide_calls, completion=_Completion())

    assert len(first_guide_calls) == 1
    assert second_guide_calls == []
    assert first.artifact is not None and second.artifact is not None


def test_complete_run_round_trips_pending_artifact_and_maps_durable_requests(tmp_path: Path) -> None:
    events: list[EnrichmentProgress] = []
    result = _run(tmp_path, completion=_Completion(), observer=events.append)
    assert list(dict.fromkeys(event.phase for event in events)) == [
        EnrichmentPhase.GUIDES,
        EnrichmentPhase.CARD_CAPABILITIES,
        EnrichmentPhase.CANDIDATES,
        EnrichmentPhase.RELATIONSHIPS,
    ]
    assert result.run.outcome is EnrichmentOutcome.COMPLETE
    assert result.artifact is not None
    assert result.artifact.review.state == "pending"
    assert result.artifact_path is not None
    decoded = SemanticEnrichmentArtifact.from_bytes(
        result.artifact_path.read_bytes(),
        sources=result.sources,
    )
    assert decoded == result.artifact
    findings = (
        *result.artifact.guide_claims,
        *result.artifact.oracle_facts,
        *result.artifact.relationships,
        *result.artifact.rejected_findings,
    )
    assert findings
    assert all(item.finding_id.startswith("work-") and ":" in item.finding_id for item in findings)
    assert len(result.artifact.runs) == len(result.run.guide_results) + len(result.run.card_results) + len(
        result.run.relationship_results
    )
    assert all(run.run_id.startswith("work-") for run in result.artifact.runs)
    assert b'"content"' not in result.artifact.to_bytes()


def test_finding_counts_distinguish_statuses_and_malformed_results_from_omissions(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        completion=_Completion(counts=True, relationship_mode="mixed"),
    )

    assert result.counts == workflow.EnrichmentFindingCounts(
        accepted=1,
        uncertain=6,
        rejected=2,
        failed=1,
    )
    assert result.run.candidate_packages is not None
    assert result.run.candidate_packages.omissions == ()
    assert [
        item.outcome.value for item in result.run.relationship_results
    ] == ["success", "malformed"]


def test_finding_counts_are_exact_for_known_verdicts(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        completion=_Completion(
            counts=True,
            malformed_card_ids=(DRAW_ID,),
            relationship_statuses=("accepted", "rejected"),
        ),
    )
    assert result.artifact is not None

    counts = result.counts
    assert counts.accepted == 1
    assert counts.uncertain == 5
    assert counts.rejected == 3
    assert counts.failed == 1

    guide_result = result.run.guide_results[0]
    assert guide_result.accepted_findings == ()
    assert {
        item.finding_id for item in guide_result.uncertain_findings
    } == {"guide-mechanic", "guide-uncertain"}
    assert {item.finding_id for item in guide_result.rejected_findings} == {"guide-rejected"}

    card_results = dict(zip(result.run.card_ids, result.run.card_results))
    assert card_results[TOKEN_ID].accepted_capabilities == ()
    assert {
        item.finding_id for item in card_results[TOKEN_ID].uncertain_capabilities
    } == {"token-accepted", "token-uncertain"}
    assert {item.finding_id for item in card_results[TOKEN_ID].rejected_capabilities} == {
        "token-rejected"
    }
    assert card_results[DRAW_ID].outcome.value == "malformed"

    assert len(result.run.relationship_results) == 2
    assert sum(item.relationship is not None for item in result.run.relationship_results) == 1
    assert sum(item.rejected is not None for item in result.run.relationship_results) == 1
    guide_claim_ids = {item.finding_id.rsplit(":", 1)[-1] for item in result.artifact.guide_claims}
    assert guide_claim_ids == {"guide-mechanic", "guide-uncertain"}
    oracle_fact_ids = {item.finding_id.rsplit(":", 1)[-1] for item in result.artifact.oracle_facts}
    assert oracle_fact_ids == {"token-accepted", "token-uncertain", "wide-capability"}
    rejected_ids = {item.finding_id.rsplit(":", 1)[-1] for item in result.artifact.rejected_findings}
    assert {"guide-rejected", "token-rejected"} <= rejected_ids


def test_budget_omissions_are_not_counted_as_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from draftomen.set_enrichment import run_set_enrichment

    def bounded_run(**kwargs: Any) -> Any:
        return run_set_enrichment(**kwargs, bounds=CandidateBounds(max_evaluated_pairs=1))

    monkeypatch.setattr(workflow, "run_set_enrichment", bounded_run)
    result = _run(tmp_path, completion=_Completion(duplicate=True))

    assert result.run.candidate_packages is not None
    omissions = result.run.candidate_packages.omissions
    assert omissions
    assert [omission.reason for omission in omissions] == [MAX_PAIR_WORK_OMITTED_REASON]
    assert [omission.mechanism for omission in omissions] == [
        "token-go-wide-payoff",
    ]
    assert [omission.omitted_pairs for omission in omissions] == [4]
    malformed_count = sum(
        item.outcome.value == "malformed"
        for item in (*result.run.guide_results, *result.run.card_results, *result.run.relationship_results)
    )
    assert result.counts.failed == malformed_count
    assert result.counts.failed == 0


def test_provenance_keeps_every_populated_capability_field(tmp_path: Path) -> None:
    result = _run(tmp_path, completion=_Completion(fidelity=True))
    assert result.artifact is not None
    assert result.artifact_path is not None

    persisted = SemanticEnrichmentArtifact.from_bytes(
        result.artifact_path.read_bytes(),
        sources=result.sources,
    )
    fact = next(
        item
        for item in persisted.oracle_facts
        if item.finding_id.endswith(":token-capability")
    )
    assert json.loads(fact.claim) == {
        "card_name": TOKEN_NAME,
        "face_index": None,
        "face_name": None,
        "role": "token_maker",
        "quantity": {"value": 2, "relation": "exactly"},
        "timing": "on resolution",
        "source_zone": "library",
        "destination_zone": "battlefield",
        "prerequisites": [],
    }
    assert {item.quote for item in fact.evidence} == {
        TOKEN_QUOTE,
        "two 1/1 white Soldier creature tokens",
    }


def test_known_costs_reconcile_as_exact_decimals(tmp_path: Path) -> None:
    completion = _Completion()
    result = _run(tmp_path, completion=completion)
    assert result.artifact is not None
    assert len(completion.calls) == 5
    assert len(result.artifact.runs) == 5

    expected = Decimal("0.010")
    artifact_total = sum(
        (Decimal(run.cost_usd) for run in result.artifact.runs if run.cost_usd is not None),
        Decimal(0),
    )
    accounting_total = Decimal(result.run.progress.accounting.running_cost_usd)
    assert artifact_total == expected
    assert accounting_total == expected
    assert result.run.progress.accounting.work_without_cost == 0


def test_colliding_capabilities_prefer_accepted_relationship_and_rerun_is_identical(
    tmp_path: Path,
) -> None:
    completion = _Completion(
        duplicate=True,
        relationship_mode="accepted-pair",
        accepted_pairs=(("token-b", "wide-b"),),
    )
    first = _run(tmp_path, completion=completion)
    assert first.artifact is not None
    assert len(first.run.relationship_results) == 4
    statuses = {
        item.relationship.review.status
        for item in first.run.relationship_results
        if item.relationship is not None
    }
    assert statuses == {FindingStatus.ACCEPTED, FindingStatus.UNCERTAIN}
    assert len(first.artifact.relationships) == 1
    selected = first.artifact.relationships[0]
    assert selected.review.status is FindingStatus.ACCEPTED
    assert selected.identity == ("token-go-wide-payoff", (TOKEN_ID, WIDE_ID))
    assert selected.finding_id.endswith(":relationship:token-go-wide-payoff:1:token-b:2:wide-b")
    assert first.artifact_path is not None
    first_bytes = first.artifact_path.read_bytes()

    second_completion = _Completion(
        duplicate=True,
        relationship_mode="accepted-pair",
        accepted_pairs=(("token-b", "wide-b"),),
    )
    second = _run(tmp_path, completion=second_completion)
    assert second.artifact_path is not None
    assert second.artifact_path.read_bytes() == first_bytes
    assert second_completion.calls == []


def test_reasoning_provenance_timestamps_tokens_and_unknown_cost_reconcile(
    tmp_path: Path,
) -> None:
    fixed = datetime(2026, 9, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    result = _run(
        tmp_path,
        completion=_Completion(null_cost_first=True),
        clock=lambda: fixed,
    )
    assert result.artifact is not None
    runs = result.artifact.runs
    assert all(
        run.reasoning.enabled is None
        and run.reasoning.effort == MODEL_CONFIG.reasoning_effort
        and run.reasoning.max_tokens is None
        and run.reasoning.exclude is None
        for run in runs
    )
    assert any(run.provider == "openrouter" and run.cost_usd is None for run in runs)
    assert all(
        datetime.fromisoformat(run.started_at.replace("Z", "+00:00")).utcoffset() == timedelta(0)
        and datetime.fromisoformat(run.completed_at.replace("Z", "+00:00")).utcoffset()
        == timedelta(0)
        for run in runs
    )
    assert (
        datetime.fromisoformat(result.artifact.created_at.replace("Z", "+00:00")).utcoffset()
        == timedelta(0)
    )
    accounting = result.run.progress.accounting
    assert sum(run.input_tokens or 0 for run in runs) == accounting.input_tokens
    assert sum(run.output_tokens or 0 for run in runs) == accounting.output_tokens
    assert sum(run.reasoning_tokens or 0 for run in runs) == accounting.reasoning_tokens
    assert accounting.work_without_cost == 1
    assert accounting.projected_final_cost_usd is None

    identities = _durable_identities(result.work_dir)
    assert set(identities) == {run.run_id for run in runs}
    assert {run.model for run in runs} == {MODEL}
    assert {identity.model_config.max_tokens for identity in identities.values()} == {
        MODEL_CONFIG.max_tokens
    }
    assert {identity.model_config.reasoning_effort for identity in identities.values()} == {
        MODEL_CONFIG.reasoning_effort
    }
    assert {run.prompt_id for run in runs} == {
        GUIDE_EXTRACTION_PROMPT_ID,
        CARD_CAPABILITY_EXTRACTION_PROMPT_ID,
        RELATIONSHIP_VALIDATION_PROMPT_ID,
    }
    assert len(
        {
            run.prompt_sha256
            for run in runs
            if run.prompt_id == CARD_CAPABILITY_EXTRACTION_PROMPT_ID
        }
    ) == 3
    for prompt_id in {run.prompt_id for run in runs}:
        family = [run for run in runs if run.prompt_id == prompt_id]
        assert len({run.response_schema_id for run in family}) == 1
        assert len({run.response_schema_sha256 for run in family}) == 1
    for run in runs:
        identity = identities[run.run_id]
        assert run.prompt_id == identity.prompt_id
        assert run.prompt_sha256 == identity.prompt_sha256
        assert run.response_schema_id == identity.response_schema_id
        assert run.response_schema_sha256 == identity.response_schema_sha256


def test_compatible_rerun_reuses_all_work_with_zero_completion_calls(tmp_path: Path) -> None:
    first = _run(tmp_path, completion=_Completion())
    second_completion = _Completion(interrupt_after=0)
    second = _run(tmp_path, completion=second_completion)

    assert first.artifact_path is not None and second.artifact_path is not None
    assert second.artifact_path.read_bytes() == first.artifact_path.read_bytes()
    assert second_completion.calls == []
    assert second.run.progress.accounting.executed_work == 0
    assert second.run.progress.accounting.reused_work > 0


def test_cancellation_and_keyboard_interrupt_preserve_durable_prefix_without_publication(
    tmp_path: Path,
) -> None:
    cancel_state = {"requested": False}

    def observe(event: EnrichmentProgress) -> None:
        if event.phase is EnrichmentPhase.CARD_CAPABILITIES and event.cards_completed == 1:
            cancel_state["requested"] = True

    cancelled = _run(
        tmp_path,
        output_dir=tmp_path / "cancelled",
        completion=_Completion(),
        observer=observe,
        is_cancelled=lambda: cancel_state["requested"],
    )
    assert cancelled.run.outcome is EnrichmentOutcome.CANCELLED
    assert cancelled.artifact is None
    assert cancelled.artifact_path is None
    assert tuple(cancelled.work_dir.joinpath("results").glob("*.json"))
    assert not _profile_marker(cancelled.output_dir).exists()

    interrupted_output = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt):
        _run(
            tmp_path,
            output_dir=interrupted_output,
            completion=_Completion(interrupt_after=1),
        )
    work_root = interrupted_output / "enrichment-runs" / "tst" / (
        hashlib.sha256(GUIDE_URL.encode("utf-8")).hexdigest()[:16]
    ) / "work"
    assert tuple(work_root.joinpath("results").glob("*.json"))
    assert not _profile_marker(interrupted_output).exists()
    assert not tuple((work_root.parent / "artifacts").glob("*.json"))


def test_cancel_publishes_only_cancelled_review_artifact(tmp_path: Path) -> None:
    result = _run(tmp_path, completion=_Completion())
    assert result.artifact_path is not None
    pending_path = result.artifact_path
    review = workflow.finalize_set_enrichment(
        analysis=result,
        decision=workflow.EnrichmentReviewDecision.CANCEL,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )

    assert review.artifact.review.state == "cancelled"
    assert review.artifact.confirmed_relationship_ids == ()
    assert review.publication is None
    assert review.artifact_path.exists()
    assert review.artifact_path != pending_path
    assert not _profile_marker(result.output_dir).exists()
    assert pending_path.exists()


def test_confirm_selects_all_accepted_relationships_and_publishes_metadata_profile(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, completion=_Completion())
    assert result.artifact is not None
    accepted_ids = tuple(
        relationship.finding_id
        for relationship in result.artifact.relationships
        if relationship.review.status is FindingStatus.ACCEPTED
    )
    review = workflow.finalize_set_enrichment(
        analysis=result,
        decision=workflow.EnrichmentReviewDecision.CONFIRM,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )

    assert review.artifact.review.state == "confirmed"
    assert review.artifact.confirmed_relationship_ids == accepted_ids
    assert review.publication is not None
    publication = review.publication
    assert publication.artifact_path.exists()
    assert publication.manifest_path.exists()
    compressed = publication.artifact_path.read_bytes()
    profile_bytes = gzip.decompress(compressed)
    profile = SetProfile.from_json(json.loads(profile_bytes))
    report = publication.generation.report
    assert profile.schema_version == 3
    assert report.stage == "metadata"
    assert report.profile_sha256 == hashlib.sha256(profile_bytes).hexdigest()
    assert report.gzip_sha256 == hashlib.sha256(compressed).hexdigest()
    assert report.gzip_bytes == len(compressed)
    assert publication.manifest_path == _profile_marker(result.output_dir)


def test_no_publishable_confirmation_keeps_review_artifact_and_marker_bytes(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        completion=_Completion(guide_category="strategy", relationship_mode="uncertain"),
    )
    marker = _profile_marker(result.output_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"authoritative-generation\n")
    marker_before = marker.read_bytes()

    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        workflow.finalize_set_enrichment(
            analysis=result,
            decision=workflow.EnrichmentReviewDecision.CONFIRM,
            reviewer_id="operator",
            reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
        )
    assert str(raised.value) == workflow.NO_PUBLISHABLE_ERROR
    assert raised.value.review_result is not None
    assert raised.value.review_result.artifact.review.state == "confirmed"
    assert raised.value.review_result.artifact_path.exists()
    assert raised.value.review_result.artifact_path.parent == result.run_dir / "artifacts"
    assert marker.read_bytes() == marker_before


def test_profile_publication_failure_keeps_confirmed_review_and_marker_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(tmp_path, completion=_Completion())
    marker = _profile_marker(result.output_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"authoritative-generation\n")
    marker_before = marker.read_bytes()

    def fail_publication(**_: object) -> object:
        raise ProfilePublicationError("injected publication failure")

    monkeypatch.setattr(workflow, "generate_local_profile_artifacts", fail_publication)
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        workflow.finalize_set_enrichment(
            analysis=result,
            decision=workflow.EnrichmentReviewDecision.CONFIRM,
            reviewer_id="operator",
            reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
        )
    assert str(raised.value) == workflow.PROFILE_PUBLICATION_ERROR
    assert raised.value.review_result is not None
    assert raised.value.review_result.artifact.review.state == "confirmed"
    assert raised.value.review_result.artifact_path.exists()
    assert raised.value.review_result.artifact_path.parent == result.run_dir / "artifacts"
    assert marker.read_bytes() == marker_before


def _durable_identities(work_dir: Path) -> dict[str, WorkIdentity]:
    """Return the durable request identity behind each recorded work run id."""
    identities: dict[str, WorkIdentity] = {}
    for path in (work_dir / "results").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        identity = WorkIdentity.from_json(payload["identity"])
        identities[f"work-{identity.content_sha256}"] = identity
    return identities


def _namespaced_relationship_ids(work_dir: Path) -> set[str]:
    """Return every namespaced relationship id the durable work would produce."""
    return {
        f"{run_id}:{identity.subject_id}"
        for run_id, identity in _durable_identities(work_dir).items()
        if identity.work_kind is WorkKind.RELATIONSHIP
    }


def test_colliding_accepted_relationships_retain_the_lowest_namespaced_identifier(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        completion=_Completion(
            duplicate=True,
            relationship_mode="accepted-pair",
            accepted_pairs=(("token-a", "wide-b"), ("token-b", "wide-b")),
        ),
    )

    accepted = [
        item.relationship
        for item in result.run.relationship_results
        if item.relationship is not None
        and item.relationship.review.status is FindingStatus.ACCEPTED
    ]
    assert len(accepted) == 2
    assert {
        (item.mechanism, tuple(sorted((item.source.card_id, item.target.card_id))))
        for item in accepted
    } == {("token-go-wide-payoff", (TOKEN_ID, WIDE_ID))}

    colliding = {
        identifier
        for identifier in _namespaced_relationship_ids(result.work_dir)
        if identifier.endswith((":token-a:2:wide-b", ":token-b:2:wide-b"))
    }
    assert len(colliding) == 2
    assert result.artifact is not None
    retained = result.artifact.relationships
    assert len(retained) == 1
    assert retained[0].finding_id == min(colliding)
    assert retained[0].run_id == retained[0].finding_id.split(":", 1)[0]
    assert retained[0].review.status is FindingStatus.ACCEPTED


def test_provider_failure_preserves_durable_prefix_without_publication(tmp_path: Path) -> None:
    result_root = tmp_path / "output"
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(tmp_path, output_dir=result_root, completion=_Completion(fail_after=1))

    assert str(raised.value) == _message("ANALYSIS_ERROR")
    assert raised.value.review_result is None
    guide_key = hashlib.sha256(GUIDE_URL.encode("utf-8")).hexdigest()[:16]
    run_dir = result_root / "enrichment-runs" / "tst" / guide_key
    assert len(tuple((run_dir / "work" / "results").glob("*.json"))) == 1
    assert tuple((run_dir / "artifacts").glob("*.json")) == ()
    assert not _profile_marker(result_root).exists()


def test_profile_tree_is_preserved_after_explicit_cancel(tmp_path: Path) -> None:
    output_dir = _create_metadata_profile(tmp_path, output_dir=tmp_path / "output")
    profile_before = _profile_snapshot(output_dir)
    result = _run(
        tmp_path,
        output_dir=output_dir,
        completion=_Completion(),
        guide_url=SECOND_GUIDE_URL,
    )
    assert result.artifact_path is not None
    pending_path = result.artifact_path
    assert pending_path.exists()
    review = workflow.finalize_set_enrichment(
        analysis=result,
        decision=workflow.EnrichmentReviewDecision.CANCEL,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )

    assert review.artifact.review.state == "cancelled"
    assert review.publication is None
    assert review.artifact_path != pending_path
    assert pending_path.exists()
    assert review.artifact_path.exists()
    assert tuple((_second_work_dir(output_dir) / "results").glob("*.json"))
    assert _profile_snapshot(output_dir) == profile_before


def test_profile_tree_is_preserved_after_cooperative_analysis_cancellation(
    tmp_path: Path,
) -> None:
    output_dir = _create_metadata_profile(tmp_path, output_dir=tmp_path / "output")
    profile_before = _profile_snapshot(output_dir)
    cancellation = {"requested": False}

    def observe(event: EnrichmentProgress) -> None:
        if event.phase is EnrichmentPhase.CARD_CAPABILITIES and event.cards_completed == 1:
            cancellation["requested"] = True

    result = _run(
        tmp_path,
        output_dir=output_dir,
        completion=_Completion(),
        observer=observe,
        is_cancelled=lambda: cancellation["requested"],
        guide_url=SECOND_GUIDE_URL,
    )

    assert result.run.outcome is EnrichmentOutcome.CANCELLED
    assert result.artifact is None
    assert tuple((_second_work_dir(output_dir) / "results").glob("*.json"))
    assert _profile_snapshot(output_dir) == profile_before


def test_profile_tree_is_preserved_after_keyboard_interrupt(tmp_path: Path) -> None:
    output_dir = _create_metadata_profile(tmp_path, output_dir=tmp_path / "output")
    profile_before = _profile_snapshot(output_dir)
    with pytest.raises(KeyboardInterrupt):
        _run(
            tmp_path,
            output_dir=output_dir,
            completion=_Completion(interrupt_after=1),
            guide_url=SECOND_GUIDE_URL,
        )

    assert tuple((_second_work_dir(output_dir) / "results").glob("*.json"))
    assert _profile_snapshot(output_dir) == profile_before


def test_profile_tree_is_preserved_after_provider_failure(tmp_path: Path) -> None:
    output_dir = _create_metadata_profile(tmp_path, output_dir=tmp_path / "output")
    profile_before = _profile_snapshot(output_dir)
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(
            tmp_path,
            output_dir=output_dir,
            completion=_Completion(fail_after=1),
            guide_url=SECOND_GUIDE_URL,
        )

    assert str(raised.value) == workflow.ANALYSIS_ERROR
    assert tuple((_second_work_dir(output_dir) / "results").glob("*.json"))
    assert _profile_snapshot(output_dir) == profile_before


def test_non_publishable_confirm_preserves_profile_tree_and_review_artifact(
    tmp_path: Path,
) -> None:
    output_dir = _create_metadata_profile(tmp_path, output_dir=tmp_path / "output")
    profile_before = _profile_snapshot(output_dir)
    result = _run(
        tmp_path,
        output_dir=output_dir,
        completion=_Completion(guide_category="strategy", relationship_mode="uncertain"),
        guide_url=SECOND_GUIDE_URL,
    )

    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        workflow.finalize_set_enrichment(
            analysis=result,
            decision=workflow.EnrichmentReviewDecision.CONFIRM,
            reviewer_id="operator",
            reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
        )

    assert str(raised.value) == workflow.NO_PUBLISHABLE_ERROR
    assert raised.value.review_result is not None
    review = raised.value.review_result
    assert review.artifact.review.state == "confirmed"
    assert review.artifact_path.exists()
    assert _profile_snapshot(output_dir) == profile_before


def test_review_guards_reject_an_incomplete_analysis_and_invalid_review_input(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, completion=_Completion())
    assert result.artifact_path is not None
    reviewed_at = datetime.now(tz=UTC) + timedelta(minutes=1)
    cases: list[tuple[dict[str, Any], str]] = [
        (
            {
                "analysis": replace(result, artifact=None, artifact_path=None),
                "decision": workflow.EnrichmentReviewDecision.CONFIRM,
                "reviewer_id": "operator",
                "reviewed_at": reviewed_at,
            },
            _message("INCOMPLETE_ANALYSIS_ERROR"),
        ),
        (
            {
                "analysis": result,
                "decision": workflow.EnrichmentReviewDecision.CONFIRM,
                "reviewer_id": "   ",
                "reviewed_at": reviewed_at,
            },
            _message("REVIEWER_ERROR"),
        ),
        (
            {
                "analysis": result,
                "decision": workflow.EnrichmentReviewDecision.CONFIRM,
                "reviewer_id": "operator",
                "reviewed_at": datetime.now(),
            },
            _message("REVIEW_TIMESTAMP_ERROR"),
        ),
        (
            {
                "analysis": result,
                "decision": workflow.EnrichmentReviewDecision.CONFIRM,
                "reviewer_id": "operator",
                "reviewed_at": datetime(2000, 1, 1, tzinfo=UTC),
            },
            _message("REVIEW_ORDER_ERROR"),
        ),
        (
            {
                "analysis": replace(result, artifact_path=None),
                "decision": workflow.EnrichmentReviewDecision.CONFIRM,
                "reviewer_id": "operator",
                "reviewed_at": reviewed_at,
            },
            _message("INCOMPLETE_ANALYSIS_ERROR"),
        ),
    ]

    for arguments, message in cases:
        with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
            workflow.finalize_set_enrichment(**arguments)
        assert str(raised.value) == message
        assert raised.value.review_result is None

    assert result.artifact_path.exists()
    assert tuple(result.artifact_path.parent.glob("*.json")) == (result.artifact_path,)
    assert not _profile_marker(result.output_dir).exists()


def test_output_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"not a directory\n")

    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(tmp_path, output_dir=blocked, completion=_Completion())

    assert str(raised.value) == _message("OUTPUT_DIRECTORY_ERROR")
    assert blocked.read_bytes() == b"not a directory\n"


def test_swapped_source_symlink_is_rejected_before_finalization(tmp_path: Path) -> None:
    result = _run(tmp_path, completion=_Completion())
    source = result.run_dir / "sources" / "card-database.json"
    preserved = source.with_name("card-database-preserved.json")
    source.rename(preserved)
    external_dir = tmp_path.parent / f"{tmp_path.name}-source-external"
    external_dir.mkdir()
    sentinel = external_dir / "sentinel"
    sentinel.write_bytes(b"authoritative")

    os.symlink(sentinel, source)

    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        workflow.finalize_set_enrichment(
            analysis=result,
            decision=workflow.EnrichmentReviewDecision.CONFIRM,
            reviewer_id="operator",
            reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
        )

    error = raised.value
    assert workflow.CONTAINMENT_ERROR in str(error)
    assert error.review_result is None
    assert sentinel.read_bytes() == b"authoritative"
    assert not _profile_marker(result.output_dir).exists()


def test_preplanted_profile_artifact_symlink_blocks_publication_and_reports_review(
    tmp_path: Path,
) -> None:
    first = _run(tmp_path, completion=_Completion())
    workflow.finalize_set_enrichment(
        analysis=first,
        decision=workflow.EnrichmentReviewDecision.CONFIRM,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )
    profile_artifacts = first.output_dir / "tst-quickdraft" / "artifacts"
    published = next(profile_artifacts.glob("*.json.gz"))
    external_dir = tmp_path.parent / f"{tmp_path.name}-profile-external"
    external_dir.mkdir()
    sentinel = external_dir / "sentinel"
    sentinel.write_bytes(b"authoritative")
    marker = _profile_marker(first.output_dir)
    marker_before = marker.read_bytes()

    second = _run(tmp_path, completion=_Completion())
    published.unlink()
    os.symlink(sentinel, published)

    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        workflow.finalize_set_enrichment(
            analysis=second,
            decision=workflow.EnrichmentReviewDecision.CONFIRM,
            reviewer_id="operator",
            reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
        )

    error = raised.value
    assert workflow.CONTAINMENT_ERROR in str(error)
    assert error.review_result is not None
    assert error.review_result.artifact.review.state == "confirmed"
    reviewed_path = error.review_result.artifact_path
    assert reviewed_path.exists()
    assert reviewed_path.parent.name == "artifacts"
    assert reviewed_path.is_relative_to(second.output_dir / "enrichment-runs" / "tst")
    assert sentinel.read_bytes() == b"authoritative"
    assert marker.read_bytes() == marker_before


def test_artifact_collision_with_different_bytes_fails_closed(tmp_path: Path) -> None:
    first = _run(tmp_path, completion=_Completion())
    assert first.artifact_path is not None
    collision = b"tampered artifact bytes"
    first.artifact_path.write_bytes(collision)

    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(tmp_path, completion=_Completion())

    assert workflow.ARTIFACT_COLLISION_ERROR in str(raised.value)
    assert first.artifact_path.read_bytes() == collision


def test_invalid_frozen_guide_is_rejected(tmp_path: Path) -> None:
    first = _run(tmp_path, completion=_Completion())
    guide_path = first.run_dir / "sources" / "guide.json"
    frozen = json.loads(guide_path.read_text(encoding="utf-8"))

    invalid_schema = dict(frozen)
    invalid_schema["schema_version"] = 1.0
    guide_path.write_text(json.dumps(invalid_schema), encoding="utf-8")
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(tmp_path, completion=_Completion())
    assert workflow.GUIDE_FREEZE_ERROR in str(raised.value)

    invalid_url = dict(frozen)
    invalid_url["url"] = "not-a-guide-url"
    guide_path.write_text(json.dumps(invalid_url), encoding="utf-8")
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(tmp_path, completion=_Completion())
    assert workflow.GUIDE_FREEZE_ERROR in str(raised.value)


def test_unusable_guide_freeze_destination_reports_a_workflow_error(tmp_path: Path) -> None:
    first = _run(tmp_path, completion=_Completion())
    guide_path = first.run_dir / "sources" / "guide.json"
    guide_path.unlink()
    guide_path.mkdir()

    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(tmp_path, completion=_Completion())

    assert workflow.GUIDE_FREEZE_ERROR in str(raised.value)


def test_confirming_an_already_reviewed_artifact_fails_closed(tmp_path: Path) -> None:
    result = _run(tmp_path, completion=_Completion())
    first_review = workflow.finalize_set_enrichment(
        analysis=result,
        decision=workflow.EnrichmentReviewDecision.CONFIRM,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )
    assert first_review.artifact.review.state == "confirmed"
    confirmed_path = first_review.artifact_path
    assert confirmed_path.exists()
    confirmed_bytes = confirmed_path.read_bytes()

    reviewed_analysis = replace(
        result,
        artifact=first_review.artifact,
        artifact_path=confirmed_path,
    )
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        workflow.finalize_set_enrichment(
            analysis=reviewed_analysis,
            decision=workflow.EnrichmentReviewDecision.CONFIRM,
            reviewer_id="operator",
            reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=2),
        )

    assert workflow.INCOMPLETE_ANALYSIS_ERROR in str(raised.value)
    assert confirmed_path.read_bytes() == confirmed_bytes


def test_non_enum_decision_is_rejected_without_publishing_a_review(tmp_path: Path) -> None:
    result = _run(tmp_path, completion=_Completion())
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        workflow.finalize_set_enrichment(
            analysis=result,
            decision="confirm",
            reviewer_id="operator",
            reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
        )

    assert workflow.REVIEW_DECISION_ERROR in str(raised.value)
    for path in (result.run_dir / "artifacts").glob("*.json"):
        state = json.loads(path.read_text(encoding="utf-8"))["review"]["state"]
        assert state not in {"confirmed", "cancelled"}


def test_cached_input_token_drift_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_model_runs = workflow._model_runs

    def drift_model_runs(**kwargs: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
        model_runs, records = original_model_runs(**kwargs)
        key, (record, context) = next(iter(records.items()))
        assert record.response is not None
        assert isinstance(record.response.cached_input_tokens, int)
        drifted_response = replace(
            record.response,
            cached_input_tokens=record.response.cached_input_tokens + 1,
        )
        drifted_record = replace(record, response=drifted_response)
        drifted_records = dict(records)
        drifted_records[key] = (drifted_record, context)
        return model_runs, drifted_records

    monkeypatch.setattr(workflow, "_model_runs", drift_model_runs)
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        _run(tmp_path, completion=_Completion())

    assert workflow.ACCOUNTING_ERROR in str(raised.value)


def test_real_publication_failure_preserves_the_existing_generation_marker(
    tmp_path: Path,
) -> None:
    first = _run(tmp_path, output_dir=tmp_path / "output", completion=_Completion())
    first_review = workflow.finalize_set_enrichment(
        analysis=first,
        decision=workflow.EnrichmentReviewDecision.CONFIRM,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )
    assert first_review.publication is not None
    marker = _profile_marker(first.output_dir)
    marker_before = marker.read_bytes()

    second = _run(
        tmp_path,
        output_dir=first.output_dir,
        guide_url=f"{GUIDE_URL}?fresh-publication",
        completion=_Completion(),
    )
    assert second.artifact_path is not None
    assert second.artifact_path != first.artifact_path
    profile_root = second.output_dir / "tst-quickdraft"
    original_mode = profile_root.stat().st_mode
    publication_error: workflow.SetEnrichmentWorkflowError | None = None
    try:
        profile_root.chmod(original_mode & ~0o222)
        try:
            workflow.finalize_set_enrichment(
                analysis=second,
                decision=workflow.EnrichmentReviewDecision.CONFIRM,
                reviewer_id="operator",
                reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
            )
        except workflow.SetEnrichmentWorkflowError as error:
            publication_error = error
        else:
            pytest.skip("platform permits the read-only profile-root publication")
    finally:
        profile_root.chmod(original_mode)

    assert publication_error is not None
    assert str(publication_error) == workflow.PROFILE_PUBLICATION_ERROR
    assert publication_error.review_result is not None
    assert publication_error.review_result.artifact_path.exists()
    assert marker.read_bytes() == marker_before


def test_completed_prefix_is_reused_after_a_failed_run(tmp_path: Path) -> None:
    result_root = tmp_path / "output"
    failed_completion = _Completion(fail_after=1)
    with pytest.raises(workflow.SetEnrichmentWorkflowError):
        _run(tmp_path, output_dir=result_root, completion=failed_completion)

    durable_results = tuple(
        (result_root / "enrichment-runs" / "tst" / hashlib.sha256(GUIDE_URL.encode("utf-8")).hexdigest()[:16])
        .joinpath("work", "results")
        .glob("*.json")
    )
    assert len(durable_results) == 1
    expected_completion = _Completion()
    _run(
        tmp_path,
        output_dir=tmp_path / "expected",
        completion=expected_completion,
    )
    rerun_completion = _Completion()
    rerun = _run(tmp_path, output_dir=result_root, completion=rerun_completion)

    assert rerun.run.progress.accounting.reused_work > 0
    assert len(rerun_completion.calls) == len(expected_completion.calls) - len(durable_results)
    completed_request = failed_completion.calls[0]
    assert all(
        (call.prompt_id, call.user_prompt) != (completed_request.prompt_id, completed_request.user_prompt)
        for call in rerun_completion.calls
    )
    assert not _profile_marker(result_root).exists()


def test_confirm_selection_matrix_covers_mechanic_only_relationship_only_and_uncertain(
    tmp_path: Path,
) -> None:
    uncertain_guide = _run(
        tmp_path,
        output_dir=tmp_path / "uncertain-guide",
        completion=_Completion(guide_category="strategy", relationship_mode="uncertain"),
    )
    with pytest.raises(workflow.SetEnrichmentWorkflowError) as raised:
        workflow.finalize_set_enrichment(
            analysis=uncertain_guide,
            decision=workflow.EnrichmentReviewDecision.CONFIRM,
            reviewer_id="operator",
            reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
        )
    assert str(raised.value) == workflow.NO_PUBLISHABLE_ERROR
    assert raised.value.review_result is not None
    assert raised.value.review_result.artifact.review.state == "confirmed"
    assert raised.value.review_result.artifact_path.exists()
    assert not _profile_marker(uncertain_guide.output_dir).exists()
    assert not (uncertain_guide.output_dir / "tst-quickdraft").exists()

    relationship_only = _run(
        tmp_path,
        output_dir=tmp_path / "relationship-only",
        completion=_Completion(guide_category="strategy"),
    )
    assert relationship_only.artifact is not None
    assert not any(
        claim.category == "mechanic" and claim.review.status is FindingStatus.ACCEPTED
        for claim in relationship_only.artifact.guide_claims
    )
    relationship_review = workflow.finalize_set_enrichment(
        analysis=relationship_only,
        decision=workflow.EnrichmentReviewDecision.CONFIRM,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )
    assert relationship_review.publication is not None
    assert relationship_review.artifact.confirmed_relationship_ids

    mixed = _run(
        tmp_path,
        output_dir=tmp_path / "mixed",
        completion=_Completion(
            duplicate=True,
            relationship_statuses=("accepted", "uncertain", "uncertain", "uncertain"),
        ),
    )
    assert mixed.artifact is not None
    accepted_relationships = [
        item.relationship
        for item in mixed.run.relationship_results
        if item.relationship is not None and item.relationship.review.status is FindingStatus.ACCEPTED
    ]
    uncertain_relationships = [
        item.relationship
        for item in mixed.run.relationship_results
        if item.relationship is not None and item.relationship.review.status is FindingStatus.UNCERTAIN
    ]
    assert len(accepted_relationships) == 1
    assert uncertain_relationships
    accepted_relationship = accepted_relationships[0]
    uncertain_ids = tuple(item.finding_id for item in uncertain_relationships)
    accepted_identity = (
        accepted_relationship.mechanism,
        tuple(sorted((accepted_relationship.source.card_id, accepted_relationship.target.card_id))),
    )
    assert tuple(
        (relationship.mechanism, relationship.participants)
        for relationship in mixed.artifact.relationships
    ) == (accepted_identity,)
    accepted_ids = (mixed.artifact.relationships[0].finding_id,)
    mixed_review = workflow.finalize_set_enrichment(
        analysis=mixed,
        decision=workflow.EnrichmentReviewDecision.CONFIRM,
        reviewer_id="operator",
        reviewed_at=datetime.now(tz=UTC) + timedelta(minutes=1),
    )
    assert mixed_review.publication is not None
    assert mixed_review.artifact.confirmed_relationship_ids == accepted_ids
    assert all(identifier not in mixed_review.artifact.confirmed_relationship_ids for identifier in uncertain_ids)
    profile_bytes = gzip.decompress(mixed_review.publication.artifact_path.read_bytes())
    profile = SetProfile.from_json(json.loads(profile_bytes))
    assert tuple(
        (relationship.mechanism, relationship.participants)
        for relationship in profile.enhancement.relationships
    ) == (accepted_identity,)
