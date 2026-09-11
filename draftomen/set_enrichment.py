"""UI-neutral resumable orchestration of frozen set-enrichment analysis.
Every paid completion comes from the caller, and all durable state stays in the caller's store.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from functools import partial
import re
from typing import Any, TypeAlias, TypeVar, cast
from uuid import uuid4

from draftomen.carddb import CardInfo
from draftomen.openrouter_client import (
    OPENROUTER_TIMEOUT_SECONDS,
    OpenRouterClient,
    OpenRouterResponse,
)
from draftomen.semantic_enrichment import EnrichmentSources, card_source_sha256, set_source_sha256
from draftomen.semantic_enrichment_records import ArtifactReview, FindingStatus
from draftomen.set_enrichment_candidates import (
    CandidateBounds,
    CandidatePackageSet,
    construct_candidate_packages,
)
from draftomen.set_enrichment_extraction import (
    CardCapabilityExtractionResult,
    ExtractionRequest,
    GuideExtractionResult,
    RelationshipValidationResult,
    build_card_capability_extraction_request,
    build_guide_extraction_request,
    build_relationship_validation_request,
    parse_card_capability_extraction_response,
    parse_guide_extraction_response,
    parse_relationship_validation_response,
    relationship_source_sha256,
    relationship_subject_id,
)
from draftomen.set_enrichment_work import (
    SetEnrichmentWorkStore,
    WorkIdentity,
    WorkKind,
    WorkModelConfig,
    WorkRecord,
    WorkState,
    build_work_identity,
)


class SetEnrichmentError(ValueError):
    """Raised when resumable set-enrichment inputs violate their contract."""


class EnrichmentPhase(StrEnum):
    """Ordered phase of one resumable set-enrichment run."""

    GUIDES = "guides"
    CARD_CAPABILITIES = "card-capabilities"
    CANDIDATES = "candidates"
    RELATIONSHIPS = "relationships"


class EnrichmentOutcome(StrEnum):
    """Terminal outcome of one resumable set-enrichment run."""

    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class EnrichmentAccounting:
    """Immutable token, cost and reuse accounting of one run phase."""

    executed_work: int
    reused_work: int
    work_without_cost: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    running_cost_usd: str
    projected_final_cost_usd: str | None


@dataclass(frozen=True, slots=True)
class EnrichmentProgress:
    """Immutable progress event emitted synchronously by one running analysis."""

    phase: EnrichmentPhase
    guides_completed: int
    guides_total: int
    cards_completed: int
    cards_total: int
    relationships_completed: int
    relationships_total: int
    valid_count: int
    uncertain_count: int
    rejected_count: int
    accounting: EnrichmentAccounting

    @property
    def guides_percent(self) -> float:
        """Return the rounded guide completion percentage."""
        return _percentage(self.guides_completed, self.guides_total)

    @property
    def cards_percent(self) -> float:
        """Return the rounded eligible-card completion percentage."""
        return _percentage(self.cards_completed, self.cards_total)

    @property
    def relationships_percent(self) -> float:
        """Return the rounded constructed-candidate completion percentage."""
        return _percentage(self.relationships_completed, self.relationships_total)


@dataclass(frozen=True, slots=True)
class EnrichmentRunResult:
    """Source-bound result of one resumable set-enrichment run.
    A cancelled result keeps every durably resolved item without claiming the analysis finished.
    """

    outcome: EnrichmentOutcome
    review: ArtifactReview
    run_id: str
    set_code: str
    set_source_sha256: str
    guide_ids: tuple[str, ...]
    guide_results: tuple[GuideExtractionResult, ...]
    card_ids: tuple[int, ...]
    card_results: tuple[CardCapabilityExtractionResult, ...]
    ineligible_card_ids: tuple[int, ...]
    candidate_packages: CandidatePackageSet | None
    relationship_results: tuple[RelationshipValidationResult, ...]
    progress: EnrichmentProgress

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, EnrichmentOutcome):
            raise SetEnrichmentError("outcome must be an EnrichmentOutcome.")
        if type(self.review) is not ArtifactReview:
            raise SetEnrichmentError("review must be an ArtifactReview.")
        if not isinstance(self.progress, EnrichmentProgress):
            raise SetEnrichmentError("progress must be an EnrichmentProgress.")
        if self.candidate_packages is not None and not isinstance(
            self.candidate_packages, CandidatePackageSet
        ):
            raise SetEnrichmentError("candidate_packages must be a CandidatePackageSet or None.")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        object.__setattr__(self, "set_code", _identifier(self.set_code, "set_code").casefold())
        object.__setattr__(
            self,
            "set_source_sha256",
            _sha256(self.set_source_sha256, "set_source_sha256"),
        )
        object.__setattr__(self, "guide_ids", _guide_id_tuple(self.guide_ids))
        object.__setattr__(self, "card_ids", _card_id_tuple(self.card_ids, "card_ids"))
        object.__setattr__(
            self,
            "ineligible_card_ids",
            _card_id_tuple(self.ineligible_card_ids, "ineligible_card_ids"),
        )
        if set(self.card_ids) & set(self.ineligible_card_ids):
            raise SetEnrichmentError("card_ids must not repeat ineligible_card_ids.")
        if len(self.guide_ids) != len(self.guide_results):
            raise SetEnrichmentError("guide_ids must pair with guide_results.")
        if len(self.card_ids) != len(self.card_results):
            raise SetEnrichmentError("card_ids must pair with card_results.")
        if type(self.guide_results) is not tuple or any(
            type(item) is not GuideExtractionResult for item in self.guide_results
        ):
            raise SetEnrichmentError("guide_results must contain GuideExtractionResult records.")
        if type(self.card_results) is not tuple or any(
            type(item) is not CardCapabilityExtractionResult for item in self.card_results
        ):
            raise SetEnrichmentError(
                "card_results must contain CardCapabilityExtractionResult records."
            )
        if type(self.relationship_results) is not tuple or any(
            type(item) is not RelationshipValidationResult for item in self.relationship_results
        ):
            raise SetEnrichmentError(
                "relationship_results must contain RelationshipValidationResult records."
            )
        self._require_relationship_prefix()
        if self.outcome is EnrichmentOutcome.COMPLETE:
            if self.candidate_packages is None:
                raise SetEnrichmentError("complete runs must construct candidate packages.")
            if len(self.relationship_results) != len(self.candidate_packages.packages):
                raise SetEnrichmentError("complete runs must resolve every constructed candidate.")

    @property
    def complete(self) -> bool:
        """Report whether this run finished every required phase."""
        return self.outcome is EnrichmentOutcome.COMPLETE

    def _require_relationship_prefix(self) -> None:
        """Require every retained relationship result to follow the constructed candidates."""
        if self.candidate_packages is None:
            if self.relationship_results:
                raise SetEnrichmentError(
                    "relationship results require constructed candidate packages."
                )
            return
        packages = self.candidate_packages.packages
        if len(self.relationship_results) > len(packages):
            raise SetEnrichmentError("relationship results must follow constructed candidates.")
        for item, package in zip(self.relationship_results, packages):
            expected = relationship_subject_id(
                mechanism=package.mechanism,
                source=package.source,
                target=package.target,
            )
            identity = _relationship_finding_id(item)
            if identity is not None and identity != expected:
                raise SetEnrichmentError(
                    "relationship results must match their constructed candidate."
                )


Completion: TypeAlias = Callable[[ExtractionRequest], OpenRouterResponse]

_ResultT = TypeVar("_ResultT")

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _identifier(value: Any, field_name: str) -> str:
    """Validate and strip one nonblank UTF-8 identifier."""
    if not isinstance(value, str) or not value.strip():
        raise SetEnrichmentError(f"{field_name} must be a nonblank string.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SetEnrichmentError(f"{field_name} must be UTF-8 encodable text.") from error
    return value.strip()


def _sha256(value: Any, field_name: str) -> str:
    """Validate one lowercase SHA-256 digest."""
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise SetEnrichmentError(f"{field_name} must be a SHA-256 digest.")
    return value


def _card_id_tuple(value: Any, field_name: str) -> tuple[int, ...]:
    """Require ascending unique canonical card identifiers."""
    if not isinstance(value, tuple) or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value
    ):
        raise SetEnrichmentError(f"{field_name} must contain positive card ids.")
    if list(value) != sorted(set(value)):
        raise SetEnrichmentError(f"{field_name} must be ascending and unique.")
    return value


def _guide_id_tuple(value: Any) -> tuple[str, ...]:
    """Require ascending unique frozen guide identifiers."""
    if not isinstance(value, tuple):
        raise SetEnrichmentError("guide_ids must be a tuple of guide identifiers.")
    normalized = tuple(_identifier(item, "guide_ids") for item in value)
    if list(normalized) != sorted(set(normalized)):
        raise SetEnrichmentError("guide_ids must be ascending and unique.")
    return normalized


def _run_id(value: Any) -> str:
    """Validate one optional caller run identifier."""
    if value is None:
        return f"enrichment-{uuid4().hex}"
    return _identifier(value, "run_id")


def _percentage(completed: int, total: int) -> float:
    """Return one rounded percentage that never divides by zero."""
    return 0.0 if total == 0 else round(completed * 100.0 / total, 1)


def _decimal_text(value: Decimal) -> str:
    """Format one exact decimal the way stored costs are normalized."""
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _has_oracle_text(text: str | None) -> bool:
    """Report whether one optional Oracle text carries any nonblank text."""
    return text is not None and bool(text.strip())


def _eligible_cards(sources: EnrichmentSources) -> tuple[CardInfo, ...]:
    """Return the canonical cards this run may attempt, in ascending card order."""
    ordered = sorted(sources.cards, key=lambda card: card.grp_id)
    return tuple(
        card
        for card in ordered
        if _has_oracle_text(card.oracle_text)
        or any(_has_oracle_text(face.oracle_text) for face in card.faces)
    )


def _relationship_finding_id(result: RelationshipValidationResult) -> str | None:
    """Return the candidate identity one parsed relationship result binds to."""
    if result.relationship is not None:
        return result.relationship.finding_id
    if result.rejected is not None:
        return result.rejected.finding_id
    return None


def _valid_count(relationships: Sequence[RelationshipValidationResult]) -> int:
    """Count accepted relationships."""
    return sum(
        1
        for item in relationships
        if item.relationship is not None
        and item.relationship.review.status is FindingStatus.ACCEPTED
    )


def _uncertain_count(
    guides: Sequence[GuideExtractionResult],
    cards: Sequence[CardCapabilityExtractionResult],
    relationships: Sequence[RelationshipValidationResult],
) -> int:
    """Count uncertain guide claims, capabilities and relationships."""
    return (
        sum(len(result.uncertain_findings) for result in guides)
        + sum(len(result.uncertain_capabilities) for result in cards)
        + sum(
            1
            for item in relationships
            if item.relationship is not None
            and item.relationship.review.status is FindingStatus.UNCERTAIN
        )
    )


def _rejected_count(
    guides: Sequence[GuideExtractionResult],
    cards: Sequence[CardCapabilityExtractionResult],
    relationships: Sequence[RelationshipValidationResult],
) -> int:
    """Count rejected diagnostics across every retained result."""
    return (
        sum(len(result.rejected_findings) for result in guides)
        + sum(len(result.rejected_capabilities) for result in cards)
        + sum(1 for item in relationships if item.rejected is not None)
    )


class _RunAccounting:
    """Track the unique responses one run resolved from durable state or a paid call."""

    def __init__(self) -> None:
        self._responses: dict[str, OpenRouterResponse] = {}
        self.executed_work = 0
        self.reused_work = 0

    def record(
        self,
        *,
        identity: WorkIdentity,
        response: OpenRouterResponse,
        reused: bool,
    ) -> None:
        """Account one work identity at most once per run."""
        if identity.content_sha256 in self._responses:
            return
        self._responses[identity.content_sha256] = response
        if reused:
            self.reused_work += 1
        else:
            self.executed_work += 1

    def snapshot(self, *, planned_work: int) -> EnrichmentAccounting:
        """Return immutable accounting for the work resolved so far.
        The projected final cost extrapolates over the work known at emission time.
        """
        input_tokens = 0
        cached_input_tokens = 0
        output_tokens = 0
        reasoning_tokens = 0
        work_without_cost = 0
        running = Decimal(0)
        for response in self._responses.values():
            input_tokens += response.input_tokens or 0
            cached_input_tokens += response.cached_input_tokens or 0
            output_tokens += response.output_tokens or 0
            reasoning_tokens += response.reasoning_tokens or 0
            if response.cost_usd is None:
                work_without_cost += 1
                continue
            running += Decimal(response.cost_usd)
        accounted_work = len(self._responses)
        projected: str | None = None
        if accounted_work and planned_work and not work_without_cost:
            projected = _decimal_text(
                (running * planned_work / accounted_work).quantize(
                    Decimal("0.000001"),
                    rounding=ROUND_HALF_UP,
                )
            )
        return EnrichmentAccounting(
            executed_work=self.executed_work,
            reused_work=self.reused_work,
            work_without_cost=work_without_cost,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            running_cost_usd=_decimal_text(running),
            projected_final_cost_usd=projected,
        )


def _durable_response(record: WorkRecord) -> OpenRouterResponse:
    """Require the durable response retained by one reusable work record."""
    if record.response is None:
        raise SetEnrichmentError("durable set-enrichment work is missing its response.")
    return record.response


def _resolve_work(
    *,
    store: SetEnrichmentWorkStore,
    identity: WorkIdentity,
    request: ExtractionRequest,
    complete: Completion,
    parse: Callable[..., _ResultT],
    run_id: str,
) -> tuple[_ResultT, OpenRouterResponse, bool]:
    """Resolve one work identity from durable state or one paid completion call.
    Completed and unvalidated records are reused without a request; a missing or incomplete
    record records its durable attempt before the paid call, so a failure stays resumable.
    """
    record = store.lookup(identity=identity)
    if record.state is WorkState.CORRUPT:
        raise SetEnrichmentError(
            f"durable set-enrichment work for {identity.subject_id} is corrupt."
        )
    if record.state is WorkState.COMPLETED:
        # The store decodes every durable result under its own work kind, so a completed
        # record already holds the exact type the kind-specific parser produces.
        result = record.result
        if result is None:
            raise SetEnrichmentError("durable set-enrichment work is missing its result.")
        return cast(_ResultT, result), _durable_response(record), True
    if record.state is WorkState.UNVALIDATED:
        response = _durable_response(record)
        result = parse(content=response.content, run_id=run_id)
        store.record_result(identity=identity, result=result)
        return result, response, True
    store.record_attempt(identity=identity)
    response = complete(request)
    store.record_response(identity=identity, response=response)
    result = parse(content=response.content, run_id=run_id)
    store.record_result(identity=identity, result=result)
    return result, response, False


def run_set_enrichment(
    *,
    sources: EnrichmentSources,
    complete: Completion,
    work_store: SetEnrichmentWorkStore,
    model_config: WorkModelConfig,
    run_id: str | None = None,
    bounds: CandidateBounds = CandidateBounds(),
    observer: Callable[[EnrichmentProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> EnrichmentRunResult:
    """Run every resumable set-enrichment phase over frozen sources.
    Durable successful work is reused, cancellation stops at a phase or work boundary, and any
    failed acquisition propagates so the caller can resume from the work already recorded.
    """
    if not isinstance(sources, EnrichmentSources):
        raise SetEnrichmentError("sources must be an EnrichmentSources record.")
    if not callable(complete):
        raise SetEnrichmentError("complete must be callable.")
    if not isinstance(work_store, SetEnrichmentWorkStore):
        raise SetEnrichmentError("work_store must be a SetEnrichmentWorkStore.")
    if not isinstance(model_config, WorkModelConfig):
        raise SetEnrichmentError("model_config must be a WorkModelConfig.")
    if not isinstance(bounds, CandidateBounds):
        raise SetEnrichmentError("bounds must be a CandidateBounds.")
    if observer is not None and not callable(observer):
        raise SetEnrichmentError("observer must be callable or None.")
    if is_cancelled is not None and not callable(is_cancelled):
        raise SetEnrichmentError("is_cancelled must be callable or None.")
    normalized_run_id = _run_id(run_id)
    eligible_cards = _eligible_cards(sources)
    eligible_ids = {card.grp_id for card in eligible_cards}
    ineligible_card_ids = tuple(
        sorted(card.grp_id for card in sources.cards if card.grp_id not in eligible_ids)
    )
    guide_ids: list[str] = []
    guide_results: list[GuideExtractionResult] = []
    card_ids: list[int] = []
    card_results: list[CardCapabilityExtractionResult] = []
    relationship_results: list[RelationshipValidationResult] = []
    packages: CandidatePackageSet | None = None
    ledger = _RunAccounting()
    phase = EnrichmentPhase.GUIDES

    def cancel_requested() -> bool:
        """Report whether the caller asked this run to stop."""
        return is_cancelled is not None and bool(is_cancelled())

    def relationships_total() -> int:
        """Return the number of constructed candidates known so far."""
        return 0 if packages is None else len(packages.packages)

    def snapshot(current: EnrichmentPhase) -> EnrichmentProgress:
        """Build one immutable progress event from the work resolved so far."""
        return EnrichmentProgress(
            phase=current,
            guides_completed=len(guide_results),
            guides_total=len(sources.guides),
            cards_completed=len(card_results),
            cards_total=len(eligible_cards),
            relationships_completed=len(relationship_results),
            relationships_total=relationships_total(),
            valid_count=_valid_count(relationship_results),
            uncertain_count=_uncertain_count(guide_results, card_results, relationship_results),
            rejected_count=_rejected_count(guide_results, card_results, relationship_results),
            accounting=ledger.snapshot(
                planned_work=len(sources.guides) + len(eligible_cards) + relationships_total()
            ),
        )

    def emit(current: EnrichmentPhase) -> None:
        """Publish one progress event synchronously to the observer."""
        if observer is not None:
            observer(snapshot(current))

    def finish(outcome: EnrichmentOutcome) -> EnrichmentRunResult:
        """Publish the final progress event and build the source-bound run result."""
        progress = snapshot(phase)
        if observer is not None:
            observer(progress)
        return EnrichmentRunResult(
            outcome=outcome,
            review=ArtifactReview(state="pending", reviewer_id=None, reviewed_at=None),
            run_id=normalized_run_id,
            set_code=sources.set_code,
            set_source_sha256=set_source_sha256(sources),
            guide_ids=tuple(guide_ids),
            guide_results=tuple(guide_results),
            card_ids=tuple(card_ids),
            card_results=tuple(card_results),
            ineligible_card_ids=ineligible_card_ids,
            candidate_packages=packages,
            relationship_results=tuple(relationship_results),
            progress=progress,
        )

    emit(phase)
    if cancel_requested():
        return finish(EnrichmentOutcome.CANCELLED)
    for guide in sources.guides:
        if cancel_requested():
            return finish(EnrichmentOutcome.CANCELLED)
        request = build_guide_extraction_request(sources=sources, guide_id=guide.guide_id)
        identity = build_work_identity(
            work_kind=WorkKind.GUIDE,
            subject_id=guide.guide_id,
            input_sha256=guide.text_sha256,
            request=request,
            model_config=model_config,
        )
        result, response, reused = _resolve_work(
            store=work_store,
            identity=identity,
            request=request,
            complete=complete,
            parse=partial(
                parse_guide_extraction_response,
                sources=sources,
                guide_id=guide.guide_id,
            ),
            run_id=normalized_run_id,
        )
        ledger.record(identity=identity, response=response, reused=reused)
        guide_ids.append(guide.guide_id)
        guide_results.append(result)
        emit(phase)

    phase = EnrichmentPhase.CARD_CAPABILITIES
    emit(phase)
    if cancel_requested():
        return finish(EnrichmentOutcome.CANCELLED)
    for card in eligible_cards:
        if cancel_requested():
            return finish(EnrichmentOutcome.CANCELLED)
        request = build_card_capability_extraction_request(sources=sources, card_id=card.grp_id)
        identity = build_work_identity(
            work_kind=WorkKind.CARD_CAPABILITY,
            subject_id=str(card.grp_id),
            input_sha256=card_source_sha256(card),
            request=request,
            model_config=model_config,
        )
        result, response, reused = _resolve_work(
            store=work_store,
            identity=identity,
            request=request,
            complete=complete,
            parse=partial(
                parse_card_capability_extraction_response,
                sources=sources,
                card_id=card.grp_id,
            ),
            run_id=normalized_run_id,
        )
        ledger.record(identity=identity, response=response, reused=reused)
        card_ids.append(card.grp_id)
        card_results.append(result)
        emit(phase)

    phase = EnrichmentPhase.CANDIDATES
    emit(phase)
    if cancel_requested():
        return finish(EnrichmentOutcome.CANCELLED)
    packages = construct_candidate_packages(card_results, bounds=bounds)
    emit(phase)

    phase = EnrichmentPhase.RELATIONSHIPS
    emit(phase)
    if cancel_requested():
        return finish(EnrichmentOutcome.CANCELLED)
    for package in packages.packages:
        if cancel_requested():
            return finish(EnrichmentOutcome.CANCELLED)
        request = build_relationship_validation_request(
            sources=sources,
            mechanism=package.mechanism,
            source=package.source,
            target=package.target,
        )
        identity = build_work_identity(
            work_kind=WorkKind.RELATIONSHIP,
            subject_id=relationship_subject_id(
                mechanism=package.mechanism,
                source=package.source,
                target=package.target,
            ),
            input_sha256=relationship_source_sha256(
                mechanism=package.mechanism,
                source=package.source,
                target=package.target,
            ),
            request=request,
            model_config=model_config,
        )
        result, response, reused = _resolve_work(
            store=work_store,
            identity=identity,
            request=request,
            complete=complete,
            parse=partial(
                parse_relationship_validation_response,
                sources=sources,
                mechanism=package.mechanism,
                source=package.source,
                target=package.target,
            ),
            run_id=normalized_run_id,
        )
        ledger.record(identity=identity, response=response, reused=reused)
        relationship_results.append(result)
        emit(phase)
    return finish(EnrichmentOutcome.COMPLETE)


def openrouter_completion(
    *,
    model_config: WorkModelConfig,
    opener: Callable[..., Any] | None = None,
    timeout_seconds: float = OPENROUTER_TIMEOUT_SECONDS,
) -> Completion:
    """Return a completion callable that submits every pinned request to OpenRouter."""
    if not isinstance(model_config, WorkModelConfig):
        raise SetEnrichmentError("model_config must be a WorkModelConfig.")

    def complete(request: ExtractionRequest) -> OpenRouterResponse:
        """Submit one pinned request through a client configured for its schema."""
        client = OpenRouterClient(
            model=model_config.model,
            schema_name=request.response_schema_name,
            schema=request.response_schema(),
            reasoning_effort=model_config.reasoning_effort,
            max_tokens=model_config.max_tokens,
            opener=opener,
            timeout_seconds=timeout_seconds,
        )
        return client.complete(
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
        )

    return complete


__all__ = [
    "Completion",
    "EnrichmentAccounting",
    "EnrichmentOutcome",
    "EnrichmentPhase",
    "EnrichmentProgress",
    "EnrichmentRunResult",
    "SetEnrichmentError",
    "openrouter_completion",
    "run_set_enrichment",
]
