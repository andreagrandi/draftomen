"""Behavior tests for the durable set-enrichment work store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

import pytest

from draftomen.carddb import CardFace, CardInfo
from draftomen.openrouter_client import OpenRouterResponse
from draftomen.semantic_enrichment import EnrichmentSources, GuideSource, card_source_sha256
from draftomen.semantic_enrichment_records import (
    FindingReview,
    FindingStatus,
    GuideClaim,
    GuideEvidence,
    RejectedFinding,
)
from draftomen.set_enrichment_extraction import (
    CardCapabilityExtractionResult,
    ExtractionOutcome,
    ExtractionRequest,
    GuideExtractionResult,
    build_card_capability_extraction_request,
    build_guide_extraction_request,
    parse_card_capability_extraction_response,
    parse_guide_extraction_response,
)
from draftomen.set_enrichment_work import (
    WORK_ATTEMPT_DIRECTORY,
    WORK_RESPONSE_DIRECTORY,
    WORK_RESULT_DIRECTORY,
    SetEnrichmentWorkConflictError,
    SetEnrichmentWorkError,
    SetEnrichmentWorkStore,
    WorkIdentity,
    WorkKind,
    WorkModelConfig,
    WorkRecord,
    WorkState,
    build_work_identity,
)


SET_CODE = "tst"
GUIDE_ID = "guide-1"
GUIDE_URL = "https://guides.example.test/tst-review"
RETRIEVED_AT = "2026-09-01T12:00:00Z"
RUN_ID = "run-1"

ALPHA_ID = 101
BETA_ID = 102
GAMMA_ID = 103

PLAIN_CARD_ID = 201
PLAIN_CARD_NAME = "Solo Sentinel"
PLAIN_CARD_TYPE_LINE = "Creature — Bird"
PLAIN_CARD_TEXT = "Flying. When this creature enters, draw a card."

TWO_FACE_CARD_ID = 202
TWO_FACE_CARD_NAME = "Alpha // Beta"
TWO_FACE_CARD_TYPE_LINE = "Creature — Soldier // Enchantment — Aura"
FRONT_FACE_NAME = "Alpha"
FRONT_FACE_TYPE_LINE = "Creature — Soldier"
FRONT_FACE_TEXT = "Flying. Whenever this creature attacks, draw a card."
BACK_FACE_NAME = "Beta"
BACK_FACE_TYPE_LINE = "Enchantment — Aura"
BACK_FACE_TEXT = "At the beginning of your upkeep, each opponent mills two cards."
TWO_FACE_CARD_TEXT = f"{FRONT_FACE_TEXT} // {BACK_FACE_TEXT}"
BACK_MILL_QUOTE = "each opponent mills two cards"

CARD_ORACLE_TEXT = "Flying. When this creature enters, draw a card."

FORMAT_SENTENCE = "Midrange decks in this format lean on cheap interaction and resilient threats."
FORMAT_CLAIM = "Midrange is the format's default fair deck."
GUIDE_TEXT = FORMAT_SENTENCE + "\n"

MODEL = "vendor/model"
REASONING_EFFORT = "high"
MAX_TOKENS = 4096

MALFORMED_RESPONSE_CONTENT = "{"

STAGES = ("attempt", "response", "result")

STAGE_TIMESTAMP_FIELDS = {
    "attempt": "attempted_at",
    "response": "responded_at",
    "result": "completed_at",
}

IDENTITY_FIELDS = (
    "work_kind",
    "subject_id",
    "contract_version",
    "prompt_id",
    "response_schema_id",
    "response_schema_name",
    "input_sha256",
    "prompt_sha256",
    "response_schema_sha256",
    "model_config",
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


class FrozenClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_store(root: Path, clock: FrozenClock | None = None) -> SetEnrichmentWorkStore:
    return SetEnrichmentWorkStore(root, clock=clock or FrozenClock())


def _card(grp_id: int, name: str) -> CardInfo:
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=("U", "B"),
        mana_value=3.0,
        rarity="uncommon",
        types=("Creature",),
        oracle_text=CARD_ORACLE_TEXT,
        set_code=SET_CODE,
    )


def _guide() -> GuideSource:
    return GuideSource(
        guide_id=GUIDE_ID,
        url=GUIDE_URL,
        text=GUIDE_TEXT,
        retrieved_at=RETRIEVED_AT,
    )


def _sources() -> EnrichmentSources:
    return EnrichmentSources(
        set_code=SET_CODE,
        cards=(_card(ALPHA_ID, "Alpha"), _card(BETA_ID, "Beta"), _card(GAMMA_ID, "Gamma")),
        guides=(_guide(),),
    )


def _plain_card() -> CardInfo:
    return CardInfo(
        grp_id=PLAIN_CARD_ID,
        name=PLAIN_CARD_NAME,
        colors=("W",),
        mana_value=2.0,
        rarity="common",
        types=("Creature",),
        oracle_text=PLAIN_CARD_TEXT,
        type_line=PLAIN_CARD_TYPE_LINE,
        set_code=SET_CODE,
    )


def _two_face_card() -> CardInfo:
    return CardInfo(
        grp_id=TWO_FACE_CARD_ID,
        name=TWO_FACE_CARD_NAME,
        colors=("U", "B"),
        mana_value=4.0,
        rarity="rare",
        types=("Creature",),
        oracle_text=TWO_FACE_CARD_TEXT,
        type_line=TWO_FACE_CARD_TYPE_LINE,
        layout="transform",
        faces=(
            CardFace(
                name=FRONT_FACE_NAME,
                type_line=FRONT_FACE_TYPE_LINE,
                oracle_text=FRONT_FACE_TEXT,
            ),
            CardFace(
                name=BACK_FACE_NAME,
                type_line=BACK_FACE_TYPE_LINE,
                oracle_text=BACK_FACE_TEXT,
            ),
        ),
        set_code=SET_CODE,
    )


def _capability_sources() -> EnrichmentSources:
    return EnrichmentSources(
        set_code=SET_CODE,
        cards=(_plain_card(), _two_face_card()),
        guides=(_guide(),),
    )


def _guide_content() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "findings": [
                {
                    "finding_id": "claim-format",
                    "category": "format_finding",
                    "name": "Midrange core",
                    "claim": FORMAT_CLAIM,
                    "card_ids": [],
                    "evidence": [{"guide_id": GUIDE_ID, "quote": FORMAT_SENTENCE}],
                    "review": {"status": "accepted", "reason": None},
                }
            ],
        }
    )


def _capability_content() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "capabilities": [
                {
                    "finding_id": "capability-draw",
                    "card_id": TWO_FACE_CARD_ID,
                    "card_name": TWO_FACE_CARD_NAME,
                    "face_index": 1,
                    "face_name": BACK_FACE_NAME,
                    "role": "draw",
                    "quantity": None,
                    "timing": None,
                    "source_zone": None,
                    "destination_zone": None,
                    "prerequisites": [],
                    "evidence": [
                        {
                            "card_id": TWO_FACE_CARD_ID,
                            "face_index": 1,
                            "quote": BACK_MILL_QUOTE,
                        }
                    ],
                    "review": {"status": "accepted", "reason": None},
                }
            ],
        }
    )


def _guide_request() -> ExtractionRequest:
    return build_guide_extraction_request(sources=_sources(), guide_id=GUIDE_ID)


def _card_request() -> ExtractionRequest:
    return build_card_capability_extraction_request(
        sources=_capability_sources(),
        card_id=TWO_FACE_CARD_ID,
    )


def _guide_result() -> GuideExtractionResult:
    return parse_guide_extraction_response(
        content=_guide_content(),
        sources=_sources(),
        guide_id=GUIDE_ID,
        run_id=RUN_ID,
    )


def _card_result() -> CardCapabilityExtractionResult:
    return parse_card_capability_extraction_response(
        content=_capability_content(),
        sources=_capability_sources(),
        card_id=TWO_FACE_CARD_ID,
        run_id=RUN_ID,
    )


def _populated_guide_result() -> GuideExtractionResult:
    return GuideExtractionResult(
        outcome=ExtractionOutcome.SUCCESS,
        accepted_findings=(
            GuideClaim(
                finding_id="claim-accepted",
                category="format_finding",
                name="Accepted claim",
                claim=FORMAT_CLAIM,
                card_ids=(),
                evidence=(GuideEvidence(guide_id=GUIDE_ID, quote=FORMAT_SENTENCE),),
                review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
                run_id=RUN_ID,
            ),
        ),
        uncertain_findings=(
            GuideClaim(
                finding_id="claim-uncertain",
                category="strategy",
                name="Uncertain claim",
                claim=FORMAT_CLAIM,
                card_ids=(ALPHA_ID, BETA_ID),
                evidence=(GuideEvidence(guide_id=GUIDE_ID, quote=FORMAT_SENTENCE),),
                review=FindingReview(
                    status=FindingStatus.UNCERTAIN,
                    reason="The quote is exact but the reading needs review.",
                ),
                run_id=RUN_ID,
            ),
        ),
        rejected_findings=(
            RejectedFinding(
                finding_id="claim-rejected",
                source_kind="guide",
                summary=FORMAT_CLAIM,
                reason="The guide does not support this interaction.",
                run_id=RUN_ID,
            ),
        ),
        malformed_reason=None,
    )


def _guide_response(
    *, content: str | None = None, output_tokens: int | None = 340
) -> OpenRouterResponse:
    return OpenRouterResponse(
        content=_guide_content() if content is None else content,
        model=MODEL,
        provider="vendor-andrea",
        input_tokens=1200,
        cached_input_tokens=800,
        output_tokens=output_tokens,
        reasoning_tokens=64,
        cost_usd="0.012345",
    )


def _card_response(*, content: str | None = None) -> OpenRouterResponse:
    return OpenRouterResponse(
        content=_capability_content() if content is None else content,
        model=MODEL,
        provider="vendor-andrea",
        input_tokens=900,
        cached_input_tokens=400,
        output_tokens=210,
        reasoning_tokens=32,
        cost_usd="0.004321",
    )


def _model_config() -> WorkModelConfig:
    return WorkModelConfig(model=MODEL, reasoning_effort=REASONING_EFFORT, max_tokens=MAX_TOKENS)


def _guide_identity() -> WorkIdentity:
    return build_work_identity(
        work_kind=WorkKind.GUIDE,
        subject_id=GUIDE_ID,
        input_sha256=_guide().text_sha256,
        request=_guide_request(),
        model_config=_model_config(),
    )


def _card_identity() -> WorkIdentity:
    return build_work_identity(
        work_kind=WorkKind.CARD_CAPABILITY,
        subject_id=str(TWO_FACE_CARD_ID),
        input_sha256=card_source_sha256(_two_face_card()),
        request=_card_request(),
        model_config=_model_config(),
    )


def _identity(**overrides: object) -> WorkIdentity:
    base = _guide_identity()
    values: dict[str, object] = {name: getattr(base, name) for name in IDENTITY_FIELDS}
    values.update(overrides)
    return WorkIdentity(**values)  # type: ignore[arg-type]


def _complete(
    store: SetEnrichmentWorkStore,
    identity: WorkIdentity,
    *,
    response: OpenRouterResponse,
    result: GuideExtractionResult | CardCapabilityExtractionResult,
) -> WorkRecord:
    store.record_attempt(identity=identity)
    store.record_response(identity=identity, response=response)
    return store.record_result(identity=identity, result=result)


def _stage_directory(store: SetEnrichmentWorkStore, stage: str) -> Path:
    directories = {
        "attempt": store.attempts,
        "response": store.responses,
        "result": store.results,
    }
    return directories[stage]


def _stage_path(store: SetEnrichmentWorkStore, stage: str, identity: WorkIdentity) -> Path:
    return _stage_directory(store, stage) / f"{identity.content_sha256}.json"


def _isolated_stage(
    source: SetEnrichmentWorkStore,
    root: Path,
    stage: str,
    identity: WorkIdentity,
) -> Path:
    """Copy one durable artifact alone into a fresh work root."""
    directory = _stage_directory(make_store(root), stage)
    directory.mkdir(parents=True)
    path = directory / f"{identity.content_sha256}.json"
    path.write_bytes(_stage_path(source, stage, identity).read_bytes())
    return path


def _layout(root: Path) -> tuple[str, ...]:
    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries.append(f"{relative} -> {os.readlink(path)}")
        elif path.is_dir():
            entries.append(f"{relative}/")
        else:
            entries.append(relative)
    return tuple(entries)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _compact_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def test_identity_is_content_addressed_from_pinned_request_and_model_config() -> None:
    request = _guide_request()
    identity = _guide_identity()

    assert identity == _guide_identity()
    assert identity.content_sha256 == _guide_identity().content_sha256
    assert identity.work_kind is WorkKind.GUIDE
    assert identity.subject_id == GUIDE_ID
    assert identity.contract_version == request.contract_version
    assert identity.prompt_id == request.prompt_id
    assert identity.response_schema_id == request.response_schema_id
    assert identity.response_schema_name == request.response_schema_name
    assert identity.prompt_sha256 == request.prompt_sha256
    assert identity.response_schema_sha256 == request.response_schema_sha256
    assert identity.input_sha256 == _guide().text_sha256
    assert identity.model_config == _model_config()

    variants = {
        "contract-version": _identity(contract_version=2),
        "prompt-id": _identity(prompt_id="draftomen-guide-extraction-v2"),
        "response-schema-id": _identity(
            response_schema_id="draftomen-guide-extraction-response-v2"
        ),
        "response-schema-name": _identity(response_schema_name="draftomen_guide_extraction_v2"),
        "input-sha256": _identity(input_sha256="1" * 64),
        "prompt-sha256": _identity(prompt_sha256="2" * 64),
        "response-schema-sha256": _identity(response_schema_sha256="3" * 64),
        "subject-id": _identity(subject_id="guide-2"),
        "model": _identity(
            model_config=WorkModelConfig(
                model="other/model",
                reasoning_effort=REASONING_EFFORT,
                max_tokens=MAX_TOKENS,
            )
        ),
        "reasoning-effort": _identity(
            model_config=WorkModelConfig(model=MODEL, reasoning_effort="low", max_tokens=MAX_TOKENS)
        ),
        "max-tokens": _identity(
            model_config=WorkModelConfig(
                model=MODEL,
                reasoning_effort=REASONING_EFFORT,
                max_tokens=MAX_TOKENS + 1,
            )
        ),
    }
    digests = {identity.content_sha256}
    for label, variant in variants.items():
        assert variant != identity, label
        assert variant.content_sha256 not in digests, label
        digests.add(variant.content_sha256)

    card_request = _card_request()
    card_identity = _card_identity()
    assert card_identity.work_kind is WorkKind.CARD_CAPABILITY
    assert card_identity.subject_id == str(TWO_FACE_CARD_ID)
    assert card_identity.input_sha256 == card_source_sha256(_two_face_card())
    assert card_identity.prompt_sha256 == card_request.prompt_sha256
    assert card_identity.response_schema_sha256 == card_request.response_schema_sha256
    assert card_identity.content_sha256 != identity.content_sha256

    assert _identity(input_sha256="A" * 64).input_sha256 == "a" * 64

    with pytest.raises(SetEnrichmentWorkError):
        build_work_identity(
            work_kind="guide",  # type: ignore[arg-type]
            subject_id=GUIDE_ID,
            input_sha256=_guide().text_sha256,
            request=request,
            model_config=_model_config(),
        )
    with pytest.raises(SetEnrichmentWorkError):
        build_work_identity(
            work_kind=WorkKind.GUIDE,
            subject_id=GUIDE_ID,
            input_sha256=_guide().text_sha256,
            request="not-a-request",  # type: ignore[arg-type]
            model_config=_model_config(),
        )
    invalid_identities: tuple[dict[str, object], ...] = (
        {"work_kind": "guide"},
        {"contract_version": True},
        {"contract_version": 0},
        {"subject_id": "   "},
        {"input_sha256": "not-a-digest"},
        {"prompt_sha256": "f" * 63},
        {"model_config": "model"},
    )
    for values in invalid_identities:
        with pytest.raises(SetEnrichmentWorkError):
            _identity(**values)
    invalid_configs: tuple[dict[str, object], ...] = (
        {"model": "", "reasoning_effort": REASONING_EFFORT, "max_tokens": MAX_TOKENS},
        {"model": "bad model", "reasoning_effort": REASONING_EFFORT, "max_tokens": MAX_TOKENS},
        {"model": "modèle", "reasoning_effort": REASONING_EFFORT, "max_tokens": MAX_TOKENS},
        {"model": MODEL, "reasoning_effort": "extreme", "max_tokens": MAX_TOKENS},
        {"model": MODEL, "reasoning_effort": REASONING_EFFORT, "max_tokens": 0},
        {"model": MODEL, "reasoning_effort": REASONING_EFFORT, "max_tokens": True},
    )
    for values in invalid_configs:
        with pytest.raises(SetEnrichmentWorkError):
            WorkModelConfig(**values)  # type: ignore[arg-type]


def test_stored_successful_response_and_result_are_recovered_without_a_new_request(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "work")
    identity = _card_identity()
    response = _card_response()
    result = _card_result()
    assert result.outcome is ExtractionOutcome.SUCCESS

    assert store.lookup(identity=identity).state is WorkState.MISSING
    assert store.record_attempt(identity=identity).state is WorkState.INCOMPLETE
    stored_response = store.record_response(identity=identity, response=response)
    assert stored_response.state is WorkState.UNVALIDATED
    assert store.record_result(identity=identity, result=result).state is WorkState.COMPLETED

    recovered = make_store(tmp_path / "work").lookup(identity=identity)
    assert recovered.state is WorkState.COMPLETED
    assert recovered.identity == identity
    assert recovered.result is not None
    assert recovered.result == result
    assert recovered.result.outcome is ExtractionOutcome.SUCCESS
    assert recovered.response is not None
    assert recovered.response == response
    assert recovered.response.content == _capability_content()
    assert recovered.diagnostics == ()
    assert recovered.attempted_at == NOW
    assert recovered.responded_at == NOW
    assert recovered.completed_at == NOW

    other = make_store(tmp_path / "work").lookup(identity=_guide_identity())
    assert other.state is WorkState.MISSING
    assert other.response is None
    assert other.result is None
    assert other.attempted_at is None
    assert other.responded_at is None
    assert other.completed_at is None


def test_changed_input_model_prompt_or_schema_prevents_stale_reuse(tmp_path: Path) -> None:
    root = tmp_path / "work"
    store = make_store(root)
    identity = _guide_identity()
    _complete(store, identity, response=_guide_response(), result=_guide_result())
    before = _tree(root)

    other_inputs = (
        _identity(input_sha256="f" * 64),
        _identity(
            model_config=WorkModelConfig(
                model="other/model", reasoning_effort="low", max_tokens=512
            )
        ),
        _identity(prompt_sha256="e" * 64),
        _identity(response_schema_sha256="d" * 64),
    )
    for other in other_inputs:
        record = store.lookup(identity=other)
        assert record.state is WorkState.MISSING
        assert record.response is None
        assert record.result is None
        assert record.diagnostics == ()

    kept = store.lookup(identity=identity)
    assert kept.state is WorkState.COMPLETED
    assert kept.result == _guide_result()
    assert kept.response == _guide_response()
    assert _tree(root) == before


def test_response_durable_without_validation_is_revalidatable(tmp_path: Path) -> None:
    root = tmp_path / "work"
    store = make_store(root)
    identity = _guide_identity()
    response = _guide_response()

    assert store.record_attempt(identity=identity).state is WorkState.INCOMPLETE
    pending = store.record_response(identity=identity, response=response)

    assert pending.state is WorkState.UNVALIDATED
    assert pending.responded_at == NOW
    assert pending.completed_at is None
    assert pending.result is None
    assert pending.response is not None
    assert pending.response.content == _guide_content()
    assert pending.response.input_tokens == 1200
    assert pending.response.cached_input_tokens == 800
    assert pending.response.output_tokens == 340
    assert pending.response.reasoning_tokens == 64
    assert pending.response.cost_usd == "0.012345"

    stored = make_store(root).lookup(identity=identity)
    assert stored.state is WorkState.UNVALIDATED
    assert stored.response is not None
    reparsed = parse_guide_extraction_response(
        content=stored.response.content,
        sources=_sources(),
        guide_id=GUIDE_ID,
        run_id=RUN_ID,
    )
    assert reparsed.outcome is ExtractionOutcome.SUCCESS
    assert reparsed == _guide_result()

    completed = store.record_result(identity=identity, result=reparsed)
    assert completed.state is WorkState.COMPLETED
    assert completed.result == reparsed
    assert completed.attempted_at == NOW
    assert completed.responded_at == NOW
    assert completed.completed_at == NOW

    unstarted = _identity(subject_id="guide-2")
    rejected = make_store(root).record_response(
        identity=unstarted,
        response=_guide_response(content=MALFORMED_RESPONSE_CONTENT),
    )
    assert rejected.state is WorkState.UNVALIDATED
    assert rejected.attempted_at is None
    malformed = parse_guide_extraction_response(
        content=MALFORMED_RESPONSE_CONTENT,
        sources=_sources(),
        guide_id=GUIDE_ID,
        run_id=RUN_ID,
    )
    assert malformed.outcome is ExtractionOutcome.MALFORMED
    stored_malformed = store.record_result(identity=unstarted, result=malformed)
    assert stored_malformed.state is WorkState.COMPLETED
    assert stored_malformed.result == malformed
    assert stored_malformed.result is not None
    assert stored_malformed.result.outcome is ExtractionOutcome.MALFORMED
    assert make_store(root).lookup(identity=unstarted).result == malformed


def test_truncated_noncanonical_and_mismatched_artifacts_are_corrupt(tmp_path: Path) -> None:
    def truncated(path: Path) -> None:
        path.write_bytes(path.read_bytes()[:16])

    def pretty_printed(path: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        path.write_bytes(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))

    def reordered(path: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        descending = {key: value[key] for key in sorted(value, reverse=True)}
        body = json.dumps(descending, ensure_ascii=False, separators=(",", ":"))
        path.write_bytes((body + "\n").encode("utf-8"))

    def unsupported_version(path: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["schema_version"] = 2
        path.write_bytes(_compact_bytes(value))

    def tampered_identity(path: Path) -> None:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["identity"]["subject_id"] = "other-subject"
        path.write_bytes(_compact_bytes(value))

    def symlinked(path: Path) -> None:
        target = path.with_name(path.name + ".target")
        path.rename(target)
        path.symlink_to(target)

    def completed(root: Path) -> tuple[SetEnrichmentWorkStore, WorkIdentity]:
        store = make_store(root)
        identity = _card_identity()
        _complete(store, identity, response=_card_response(), result=_card_result())
        return store, identity

    mutations: tuple[tuple[str, Callable[[Path], None]], ...] = (
        ("truncated", truncated),
        ("pretty-printed", pretty_printed),
        ("reordered", reordered),
        ("unsupported-version", unsupported_version),
        ("tampered-identity", tampered_identity),
        ("symlinked", symlinked),
    )
    for stage in STAGES:
        for label, mutate in mutations:
            store, identity = completed(tmp_path / f"{stage}-{label}")
            path = _stage_path(store, stage, identity)
            assert path.is_file(), (stage, label)
            mutate(path)
            record = store.lookup(identity=identity)
            assert record.state is WorkState.CORRUPT, (stage, label)
            assert f"{stage}-invalid" in record.diagnostics, (stage, label)
            assert record.result is None, (stage, label)
            if stage == "response":
                assert record.response is None, (stage, label)
                assert "result-without-response" in record.diagnostics, (stage, label)
            else:
                assert record.response is not None, (stage, label)

    store, identity = completed(tmp_path / "result-without-response")
    _stage_path(store, "response", identity).unlink()
    orphaned = store.lookup(identity=identity)
    assert orphaned.state is WorkState.CORRUPT
    assert orphaned.diagnostics == ("result-without-response",)
    assert orphaned.result is None
    assert orphaned.response is None


def test_conflicting_artifacts_cannot_replace_durable_work(tmp_path: Path) -> None:
    clock = FrozenClock()
    store = make_store(tmp_path / "work", clock)
    identity = _guide_identity()
    response = _guide_response()
    result = _guide_result()
    _complete(store, identity, response=response, result=result)
    paths = {stage: _stage_path(store, stage, identity) for stage in STAGES}
    before = {stage: path.read_bytes() for stage, path in paths.items()}

    clock.value = NOW + timedelta(minutes=5)
    rival = _guide_response(content=MALFORMED_RESPONSE_CONTENT, output_tokens=9997)
    with pytest.raises(
        SetEnrichmentWorkConflictError,
        match="already recorded with different content",
    ):
        store.record_response(identity=identity, response=rival)
    malformed = parse_guide_extraction_response(
        content=MALFORMED_RESPONSE_CONTENT,
        sources=_sources(),
        guide_id=GUIDE_ID,
        run_id=RUN_ID,
    )
    assert malformed.outcome is ExtractionOutcome.MALFORMED
    with pytest.raises(SetEnrichmentWorkConflictError):
        store.record_result(identity=identity, result=malformed)
    assert {stage: path.read_bytes() for stage, path in paths.items()} == before

    kept = store.lookup(identity=identity)
    assert kept.state is WorkState.COMPLETED
    assert kept.result == result
    assert kept.response == response
    assert kept.attempted_at == NOW
    assert kept.responded_at == NOW
    assert kept.completed_at == NOW

    assert store.record_response(identity=identity, response=response).state is WorkState.COMPLETED
    assert store.record_result(identity=identity, result=result).state is WorkState.COMPLETED
    assert {stage: path.read_bytes() for stage, path in paths.items()} == before
    assert store.lookup(identity=identity).responded_at == NOW
    assert store.lookup(identity=identity).completed_at == NOW

    unbacked = _card_identity()
    empty = make_store(tmp_path / "empty", clock)
    assert empty.record_attempt(identity=unbacked).state is WorkState.INCOMPLETE
    with pytest.raises(SetEnrichmentWorkError):
        empty.record_result(identity=unbacked, result=_card_result())
    assert not (empty.results / f"{unbacked.content_sha256}.json").exists()


def test_interruption_never_exposes_completion_and_prior_records_survive(tmp_path: Path) -> None:
    store = make_store(tmp_path / "work")
    completed = _guide_identity()
    _complete(store, completed, response=_guide_response(), result=_guide_result())
    interrupted = _identity(subject_id="guide-2")

    assert store.record_attempt(identity=interrupted).state is WorkState.INCOMPLETE
    assert not (store.results / f"{interrupted.content_sha256}.json").exists()
    prior = store.lookup(identity=completed)
    assert prior.state is WorkState.COMPLETED
    assert prior.result == _guide_result()
    assert prior.response == _guide_response()

    assert (
        store.record_response(
            identity=interrupted,
            response=_guide_response(content=MALFORMED_RESPONSE_CONTENT),
        ).state
        is WorkState.UNVALIDATED
    )
    assert store.lookup(identity=completed).state is WorkState.COMPLETED

    assert (
        store.record_result(identity=interrupted, result=_guide_result()).state
        is WorkState.COMPLETED
    )
    torn_path = store.results / f"{interrupted.content_sha256}.json"
    torn_path.write_bytes(torn_path.read_bytes()[:24])

    torn = store.lookup(identity=interrupted)
    assert torn.state is WorkState.CORRUPT
    assert torn.diagnostics == ("result-invalid",)
    assert torn.result is None
    assert torn.response is not None
    assert torn.completed_at is None

    survivor = store.lookup(identity=completed)
    assert survivor.state is WorkState.COMPLETED
    assert survivor.result == _guide_result()
    assert survivor.response == _guide_response()


def test_store_writes_stay_inside_the_supplied_work_directory(tmp_path: Path) -> None:
    root = tmp_path / "work"
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("keep", encoding="utf-8")

    store = make_store(root)
    identity = _card_identity()
    _complete(store, identity, response=_card_response(), result=_card_result())

    assert (WORK_ATTEMPT_DIRECTORY, WORK_RESPONSE_DIRECTORY, WORK_RESULT_DIRECTORY) == (
        "attempts",
        "responses",
        "results",
    )
    assert store.root == root
    assert store.attempts == root / WORK_ATTEMPT_DIRECTORY
    assert store.responses == root / WORK_RESPONSE_DIRECTORY
    assert store.results == root / WORK_RESULT_DIRECTORY
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == [
        WORK_ATTEMPT_DIRECTORY,
        f"{WORK_ATTEMPT_DIRECTORY}/{identity.content_sha256}.json",
        WORK_RESPONSE_DIRECTORY,
        f"{WORK_RESPONSE_DIRECTORY}/{identity.content_sha256}.json",
        WORK_RESULT_DIRECTORY,
        f"{WORK_RESULT_DIRECTORY}/{identity.content_sha256}.json",
    ]
    assert sorted(path.name for path in sentinel.iterdir()) == ["keep.txt"]
    assert (sentinel / "keep.txt").read_text(encoding="utf-8") == "keep"

    external = tmp_path / "external"
    external.mkdir()
    symlinked_root = tmp_path / "symlinked"
    symlinked_root.mkdir()
    (symlinked_root / WORK_RESPONSE_DIRECTORY).symlink_to(external)
    blocked = make_store(symlinked_root)
    with pytest.raises(SetEnrichmentWorkError):
        blocked.record_response(identity=identity, response=_card_response())
    assert tuple(external.iterdir()) == ()

    absent_root = tmp_path / "absent"
    missing = make_store(absent_root).lookup(identity=identity)
    assert missing.state is WorkState.MISSING
    assert not absent_root.exists()


def test_accounting_and_timestamps_survive_resumption(tmp_path: Path) -> None:
    root = tmp_path / "work"
    clock = FrozenClock()
    store = make_store(root, clock)
    identity = _card_identity()
    response = OpenRouterResponse(
        content=_capability_content(),
        model="vendor/model-2",
        provider="vendor-name",
        input_tokens=1200,
        cached_input_tokens=800,
        output_tokens=340,
        reasoning_tokens=64,
        cost_usd="0.012345",
    )

    assert store.record_attempt(identity=identity).attempted_at == NOW
    clock.value = NOW + timedelta(minutes=1)
    assert store.record_response(identity=identity, response=response).responded_at == clock.value
    clock.value = NOW + timedelta(minutes=2)
    assert store.record_result(identity=identity, result=_card_result()).completed_at == clock.value

    recovered = make_store(root).lookup(identity=identity)
    assert recovered.state is WorkState.COMPLETED
    assert recovered.attempted_at == NOW
    assert recovered.responded_at == NOW + timedelta(minutes=1)
    assert recovered.completed_at == NOW + timedelta(minutes=2)
    stored = recovered.response
    assert stored is not None
    assert stored.content == _capability_content()
    assert stored.model == "vendor/model-2"
    assert stored.provider == "vendor-name"
    assert stored.input_tokens == 1200
    assert stored.cached_input_tokens == 800
    assert stored.output_tokens == 340
    assert stored.reasoning_tokens == 64
    assert stored.cost_usd == "0.012345"
    assert recovered.result == _card_result()

    bare = _identity(subject_id="guide-2")
    bare_response = OpenRouterResponse(
        content=_guide_content(),
        model=MODEL,
        provider=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        reasoning_tokens=None,
        cost_usd=None,
    )
    assert store.record_attempt(identity=bare).state is WorkState.INCOMPLETE
    assert (
        store.record_response(identity=bare, response=bare_response).state
        is WorkState.UNVALIDATED
    )

    bare_record = make_store(root).lookup(identity=bare)
    assert bare_record.responded_at == NOW + timedelta(minutes=2)
    assert bare_record.response is not None
    assert bare_record.response.provider is None
    assert bare_record.response.input_tokens is None
    assert bare_record.response.cached_input_tokens is None
    assert bare_record.response.output_tokens is None
    assert bare_record.response.reasoning_tokens is None
    assert bare_record.response.cost_usd is None


def test_work_kind_binds_stored_results(tmp_path: Path) -> None:
    store = make_store(tmp_path / "work")
    card_identity = _card_identity()
    guide_identity = _guide_identity()

    store.record_attempt(identity=card_identity)
    store.record_response(identity=card_identity, response=_card_response())
    with pytest.raises(SetEnrichmentWorkError):
        store.record_result(identity=card_identity, result=_guide_result())
    assert not (store.results / f"{card_identity.content_sha256}.json").exists()

    store.record_attempt(identity=guide_identity)
    store.record_response(identity=guide_identity, response=_guide_response())
    with pytest.raises(SetEnrichmentWorkError):
        store.record_result(identity=guide_identity, result=_card_result())
    assert not (store.results / f"{guide_identity.content_sha256}.json").exists()

    assert (
        store.record_result(identity=guide_identity, result=_guide_result()).result
        == _guide_result()
    )
    assert (
        store.record_result(identity=card_identity, result=_card_result()).state
        is WorkState.COMPLETED
    )

    stored_path = store.results / f"{card_identity.content_sha256}.json"
    value: dict[str, Any] = json.loads(stored_path.read_text(encoding="utf-8"))
    value["identity"]["work_kind"] = "guide"
    stored_path.write_bytes(_compact_bytes(value))

    record = store.lookup(identity=card_identity)
    assert record.state is WorkState.CORRUPT
    assert record.diagnostics == ("result-invalid",)
    assert record.result is None
    assert record.response is not None
    assert record.response == _card_response()


def test_symlinked_stage_directory_rejects_before_creating_siblings(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / WORK_RESPONSE_DIRECTORY).symlink_to(external)
    store = make_store(root)
    identity = _guide_identity()
    before = _layout(root)
    assert before == (f"{WORK_RESPONSE_DIRECTORY} -> {external}",)

    with pytest.raises(SetEnrichmentWorkError):
        store.record_response(identity=identity, response=_guide_response())

    assert _layout(root) == before
    assert not (root / WORK_ATTEMPT_DIRECTORY).exists()
    assert not (root / WORK_RESULT_DIRECTORY).exists()
    assert tuple(external.iterdir()) == ()


def test_conflicting_response_rejects_without_creating_sibling_directories(tmp_path: Path) -> None:
    installed = make_store(tmp_path / "installed")
    identity = _guide_identity()
    installed.record_response(identity=identity, response=_guide_response())
    root = tmp_path / "work"
    path = _isolated_stage(installed, root, "response", identity)
    before = _layout(root)
    assert before == (
        f"{WORK_RESPONSE_DIRECTORY}/",
        f"{WORK_RESPONSE_DIRECTORY}/{identity.content_sha256}.json",
    )
    stored = path.read_bytes()

    rival = _guide_response(content=MALFORMED_RESPONSE_CONTENT, output_tokens=9997)
    with pytest.raises(SetEnrichmentWorkConflictError):
        make_store(root).record_response(identity=identity, response=rival)

    assert _layout(root) == before
    assert path.read_bytes() == stored
    assert not (root / WORK_ATTEMPT_DIRECTORY).exists()
    assert not (root / WORK_RESULT_DIRECTORY).exists()

    record = make_store(root).lookup(identity=identity)
    assert record.state is WorkState.UNVALIDATED
    assert record.response == _guide_response()
    assert record.result is None
    assert record.diagnostics == ()
    assert _layout(root) == before


def test_identical_response_is_a_no_op_without_sibling_directories(tmp_path: Path) -> None:
    installed = make_store(tmp_path / "installed")
    identity = _guide_identity()
    installed.record_response(identity=identity, response=_guide_response())
    root = tmp_path / "work"
    path = _isolated_stage(installed, root, "response", identity)
    clock = FrozenClock()
    store = make_store(root, clock)
    before = _layout(root)
    stored = path.read_bytes()

    clock.value = NOW + timedelta(hours=6)
    record = store.record_response(identity=identity, response=_guide_response())

    assert record.state is WorkState.UNVALIDATED
    assert record.responded_at == NOW
    assert record.response == _guide_response()
    assert _layout(root) == before
    assert path.read_bytes() == stored
    assert not (root / WORK_ATTEMPT_DIRECTORY).exists()
    assert not (root / WORK_RESULT_DIRECTORY).exists()


def test_unusable_and_non_file_occupied_artifacts_reject_without_mutating_the_tree(
    tmp_path: Path,
) -> None:
    def truncated(path: Path) -> None:
        path.write_bytes(path.read_bytes()[:16])

    def replaced_by_directory(path: Path) -> None:
        path.unlink()
        path.mkdir()
        (path / "keep.txt").write_text("keep", encoding="utf-8")

    rival = _guide_response(content=MALFORMED_RESPONSE_CONTENT, output_tokens=9997)
    malformed = parse_guide_extraction_response(
        content=MALFORMED_RESPONSE_CONTENT,
        sources=_sources(),
        guide_id=GUIDE_ID,
        run_id=RUN_ID,
    )
    occupiers: tuple[tuple[str, Callable[[Path], None]], ...] = (
        ("truncated", truncated),
        ("directory", replaced_by_directory),
    )
    for label, occupy in occupiers:
        for stage in STAGES:
            root = tmp_path / f"{label}-{stage}"
            store = make_store(root)
            identity = _guide_identity()
            _complete(store, identity, response=_guide_response(), result=_guide_result())
            path = _stage_path(store, stage, identity)
            occupy(path)
            before = _layout(root)
            stored = _tree(root)

            with pytest.raises(SetEnrichmentWorkConflictError):
                if stage == "attempt":
                    store.record_attempt(identity=identity)
                elif stage == "response":
                    store.record_response(identity=identity, response=rival)
                else:
                    store.record_result(identity=identity, result=malformed)

            assert _layout(root) == before, (label, stage)
            assert _tree(root) == stored, (label, stage)


def test_publication_failure_preserves_prior_work_and_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "work"
    store = make_store(root)
    completed = _guide_identity()
    _complete(store, completed, response=_guide_response(), result=_guide_result())
    completed_paths = {stage: _stage_path(store, stage, completed) for stage in STAGES}
    completed_bytes = {stage: path.read_bytes() for stage, path in completed_paths.items()}

    pending = _identity(subject_id="guide-2")
    key = pending.content_sha256
    store.record_attempt(identity=pending)
    store.record_response(identity=pending, response=_guide_response())
    result_path = store.results / f"{key}.json"

    real_replace = os.replace

    def fail_replace(source_path: str, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("draftomen.set_enrichment_work.os.replace", fail_replace)
    with pytest.raises(
        SetEnrichmentWorkError,
        match="Could not publish set-enrichment work artifact.",
    ):
        store.record_result(identity=pending, result=_guide_result())
    monkeypatch.setattr("draftomen.set_enrichment_work.os.replace", real_replace)

    assert not result_path.exists()
    assert tuple(store.results.glob(f".{key}.json.*")) == ()
    pending_record = store.lookup(identity=pending)
    assert pending_record.state is WorkState.UNVALIDATED
    assert pending_record.response == _guide_response()
    assert pending_record.result is None
    assert pending_record.completed_at is None
    prior = store.lookup(identity=completed)
    assert prior.state is WorkState.COMPLETED
    assert prior.result == _guide_result()
    assert prior.response == _guide_response()
    assert {stage: path.read_bytes() for stage, path in completed_paths.items()} == completed_bytes

    stale = store.results / f".{key}.json.stale"
    stale.write_bytes(b"stale temporary bytes")
    assert store.lookup(identity=pending).state is WorkState.UNVALIDATED
    assert stale.read_bytes() == b"stale temporary bytes"

    assert (
        store.record_result(identity=pending, result=_guide_result()).state is WorkState.COMPLETED
    )
    assert result_path.exists()
    assert stale.read_bytes() == b"stale temporary bytes"
    recovered = store.lookup(identity=pending)
    assert recovered.state is WorkState.COMPLETED
    assert recovered.result == _guide_result()
    assert {stage: path.read_bytes() for stage, path in completed_paths.items()} == completed_bytes


def test_non_utc_aware_clock_is_persisted_as_utc(tmp_path: Path) -> None:
    offset_now = datetime(2026, 8, 31, 12, tzinfo=timezone(timedelta(hours=2)))
    instant = offset_now.astimezone(UTC)
    assert instant == datetime(2026, 8, 31, 10, tzinfo=UTC)
    assert instant != NOW
    root = tmp_path / "work"
    store = make_store(root, FrozenClock(offset_now))
    identity = _guide_identity()

    assert store.record_attempt(identity=identity).attempted_at == instant
    stored_response = store.record_response(identity=identity, response=_guide_response())
    assert stored_response.responded_at == instant
    assert store.record_result(identity=identity, result=_guide_result()).completed_at == instant

    for stage in STAGES:
        raw = _stage_path(store, stage, identity).read_bytes()
        assert b"+00:00" in raw, stage
        assert b"+02:00" not in raw, stage
    assert b"2026-08-31T10:00:00+00:00" in _stage_path(store, "attempt", identity).read_bytes()

    recovered = make_store(root).lookup(identity=identity)
    assert recovered.state is WorkState.COMPLETED
    assert recovered.attempted_at == instant
    assert recovered.responded_at == instant
    assert recovered.completed_at == instant
    for stamp in (recovered.attempted_at, recovered.responded_at, recovered.completed_at):
        assert stamp is not None
        assert stamp.utcoffset() == timedelta(0)


def test_record_result_requires_a_usable_durable_response(tmp_path: Path) -> None:
    root = tmp_path / "work"
    store = make_store(root)
    identity = _guide_identity()
    store.record_attempt(identity=identity)
    store.record_response(identity=identity, response=_guide_response())
    response_path = _stage_path(store, "response", identity)
    response_path.write_bytes(response_path.read_bytes()[:16])
    before = _layout(root)

    with pytest.raises(SetEnrichmentWorkError):
        store.record_result(identity=identity, result=_guide_result())

    assert _layout(root) == before
    assert tuple(store.results.iterdir()) == ()
    record = store.lookup(identity=identity)
    assert record.state is WorkState.CORRUPT
    assert record.diagnostics == ("response-invalid",)
    assert record.result is None
    assert record.response is None
    assert record.completed_at is None


def test_unreadable_artifact_translates_to_a_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "work"
    store = make_store(root)
    identity = _guide_identity()
    _complete(store, identity, response=_guide_response(), result=_guide_result())

    real_read_bytes = Path.read_bytes

    def fail_read_bytes(path: Path) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    with pytest.raises(
        SetEnrichmentWorkError,
        match="Could not read set-enrichment work artifact.",
    ):
        store.lookup(identity=identity)
    monkeypatch.setattr(Path, "read_bytes", real_read_bytes)

    recovered = store.lookup(identity=identity)
    assert recovered.state is WorkState.COMPLETED
    assert recovered.diagnostics == ()
    assert recovered.result == _guide_result()


def test_malformed_stored_payloads_are_corrupt(tmp_path: Path) -> None:
    def stored_value(path: Path) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return value

    def duplicate_key(path: Path) -> None:
        body = path.read_bytes().rstrip(b"\n")
        assert body.endswith(b"}")
        path.write_bytes(body[:-1] + b',"schema_version":1}\n')

    def nan_tokens(path: Path) -> None:
        value = stored_value(path)
        value["response"]["output_tokens"] = float("nan")
        path.write_bytes(_compact_bytes(value))

    def infinite_tokens(path: Path) -> None:
        value = stored_value(path)
        value["response"]["cached_input_tokens"] = float("inf")
        path.write_bytes(_compact_bytes(value))

    def negative_tokens(path: Path) -> None:
        value = stored_value(path)
        value["response"]["output_tokens"] = -1
        path.write_bytes(_compact_bytes(value))

    def boolean_tokens(path: Path) -> None:
        value = stored_value(path)
        value["response"]["input_tokens"] = True
        path.write_bytes(_compact_bytes(value))

    def non_numeric_cost(path: Path) -> None:
        value = stored_value(path)
        value["response"]["cost_usd"] = "free"
        path.write_bytes(_compact_bytes(value))

    def non_string_provider(path: Path) -> None:
        value = stored_value(path)
        value["response"]["provider"] = 7
        path.write_bytes(_compact_bytes(value))

    def unsupported_outcome(path: Path) -> None:
        value = stored_value(path)
        value["result"]["outcome"] = "partly-successful"
        path.write_bytes(_compact_bytes(value))

    def replaced_timestamp(stage: str, replacement: object) -> Callable[[Path], None]:
        def mutate(path: Path) -> None:
            value = stored_value(path)
            value[STAGE_TIMESTAMP_FIELDS[stage]] = replacement
            path.write_bytes(_compact_bytes(value))

        return mutate

    cases: list[tuple[str, str, Callable[[Path], None]]] = []
    for stage in STAGES:
        cases.append((stage, "duplicate-key", duplicate_key))
        cases.append((stage, "naive-timestamp", replaced_timestamp(stage, "2026-08-31T12:00:00")))
        cases.append((stage, "non-string-timestamp", replaced_timestamp(stage, 20260831)))
    for label, mutate in (
        ("nan-tokens", nan_tokens),
        ("infinite-tokens", infinite_tokens),
        ("negative-tokens", negative_tokens),
        ("boolean-tokens", boolean_tokens),
        ("non-numeric-cost", non_numeric_cost),
        ("non-string-provider", non_string_provider),
    ):
        cases.append(("response", label, mutate))
    cases.append(("result", "unsupported-outcome", unsupported_outcome))

    for stage, label, mutate in cases:
        store = make_store(tmp_path / f"{stage}-{label}")
        identity = _guide_identity()
        _complete(store, identity, response=_guide_response(), result=_guide_result())
        mutate(_stage_path(store, stage, identity))

        record = store.lookup(identity=identity)
        assert record.state is WorkState.CORRUPT, (stage, label)
        assert f"{stage}-invalid" in record.diagnostics, (stage, label)
        assert record.result is None, (stage, label)
        if stage == "response":
            assert record.response is None, (stage, label)
            assert "result-without-response" in record.diagnostics, (stage, label)
        else:
            assert record.response is not None, (stage, label)


def test_repeated_attempt_is_an_idempotent_no_op(tmp_path: Path) -> None:
    root = tmp_path / "work"
    clock = FrozenClock()
    store = make_store(root, clock)
    identity = _guide_identity()

    first = store.record_attempt(identity=identity)
    assert first.state is WorkState.INCOMPLETE
    assert first.attempted_at == NOW
    before_layout = _layout(root)
    before_tree = _tree(root)

    clock.value = NOW + timedelta(hours=3)
    again = store.record_attempt(identity=identity)
    assert again.state is WorkState.INCOMPLETE
    assert again.attempted_at == NOW
    assert _layout(root) == before_layout
    assert _tree(root) == before_tree


def test_populated_guide_result_round_trips_through_storage(tmp_path: Path) -> None:
    root = tmp_path / "work"
    store = make_store(root)
    identity = _guide_identity()
    populated = _populated_guide_result()
    assert populated.accepted_findings != ()
    assert populated.uncertain_findings != ()
    assert populated.rejected_findings != ()

    _complete(store, identity, response=_guide_response(), result=populated)

    recovered = make_store(root).lookup(identity=identity)
    assert recovered.state is WorkState.COMPLETED
    stored = recovered.result
    assert isinstance(stored, GuideExtractionResult)
    assert stored == populated
    assert stored.outcome is ExtractionOutcome.SUCCESS
    assert tuple(claim.finding_id for claim in stored.accepted_findings) == ("claim-accepted",)
    assert tuple(claim.finding_id for claim in stored.uncertain_findings) == ("claim-uncertain",)
    assert tuple(finding.finding_id for finding in stored.rejected_findings) == ("claim-rejected",)
    assert stored.uncertain_findings[0].card_ids == (ALPHA_ID, BETA_ID)


def test_malformed_card_result_round_trips_as_completed(tmp_path: Path) -> None:
    root = tmp_path / "work"
    store = make_store(root)
    identity = _card_identity()
    malformed = parse_card_capability_extraction_response(
        content=MALFORMED_RESPONSE_CONTENT,
        sources=_capability_sources(),
        card_id=TWO_FACE_CARD_ID,
        run_id=RUN_ID,
    )
    assert malformed.outcome is ExtractionOutcome.MALFORMED

    store.record_attempt(identity=identity)
    store.record_response(
        identity=identity,
        response=_card_response(content=MALFORMED_RESPONSE_CONTENT),
    )
    assert store.record_result(identity=identity, result=malformed).state is WorkState.COMPLETED

    recovered = make_store(root).lookup(identity=identity)
    assert recovered.state is WorkState.COMPLETED
    assert recovered.result == malformed
    assert recovered.result is not None
    assert recovered.result.outcome is ExtractionOutcome.MALFORMED
    assert recovered.result.malformed_reason is not None
    assert recovered.response is not None
    assert recovered.response.content == MALFORMED_RESPONSE_CONTENT
