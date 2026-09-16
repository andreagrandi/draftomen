"""UI-neutral application boundary for reviewed set-enrichment artifacts.

The workflow freezes caller-selected sources, delegates resumable analysis to the
set-enrichment engine, and crosses the local profile boundary only after confirmation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, TypeAlias

from draftomen.card_data_client import (
    CardDataClient,
    CardDataClientError,
    card_data_cache_path,
)
from draftomen.carddb import CardDatabase, CardDatabaseError, save_card_database
from draftomen.guide_client import GuideClient, GuideClientError, GuideDocument
from draftomen.profile_generation import ProfileGenerationStage
from draftomen.profile_manifest import ProfileManifestError
from draftomen.profile_publication import (
    ProfilePublicationError,
    ProfilePublicationResult,
    generate_local_profile_artifacts,
    publish_profile_publication,
    _GUIDE_SCHEMA_VERSION,
    _guide_freeze_record,
    _strict_json,
)
from draftomen.semantic_capability_records import CardCapability
from draftomen.semantic_enrichment import (
    EnrichmentSources,
    GuideSource,
    SemanticEnrichmentArtifact,
    card_source_sha256,
    set_source_sha256,
)
from draftomen.semantic_enrichment_records import (
    ArtifactReview,
    CardSourcePin,
    FindingReview,
    FindingStatus,
    GuideClaim,
    GuideSourcePin,
    ModelRun,
    OracleFact,
    ReasoningConfig,
    RejectedFinding,
)
from draftomen.semantic_relationship_records import CardRelationship
from draftomen.set_enrichment import (
    Completion,
    EnrichmentAccounting,
    EnrichmentOutcome,
    EnrichmentProgress,
    EnrichmentRunResult,
    openrouter_completion,
    partition_relationship_batches,
    run_set_enrichment,
)
from draftomen.set_enrichment_candidates import (
    LOCAL_PROVE_REASON,
    CandidatePackage,
    CandidateResolution,
    CandidateResolutionBasis,
    CandidateResolutionSet,
    CandidateResolutionVerdict,
)
from draftomen.set_enrichment_extraction import (
    CardCapabilityExtractionResult,
    ExtractionOutcome,
    GuideExtractionResult,
    RelationshipValidationResult,
    build_card_capability_extraction_request,
    build_guide_extraction_request,
    build_relationship_validation_batch_request,
    relationship_batch_source_sha256,
    relationship_batch_subject_id,
    relationship_subject_id,
)
from draftomen.set_enrichment_work import (
    SetEnrichmentWorkError,
    SetEnrichmentWorkStore,
    WorkIdentity,
    WorkKind,
    WorkModelConfig,
    WorkState,
    build_work_identity,
)
from draftomen.seventeen import QUICK_DRAFT_FORMAT


PathInput: TypeAlias = str | os.PathLike[str]


DEFAULT_ENRICHMENT_MODEL_CONFIG = WorkModelConfig(
    model="openai/gpt-5.6-luna",
    reasoning_effort="medium",
    max_tokens=128000,
)


class EnrichmentReviewDecision(StrEnum):
    """The explicit human decision at the profile publication boundary."""

    CONFIRM = "confirm"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class EnrichmentFindingCounts:
    """Counts of original extraction findings before projection."""

    accepted: int
    uncertain: int
    rejected: int
    failed: int


@dataclass(frozen=True, slots=True)
class SetEnrichmentWorkflowResult:
    """Complete source, durable-work, and optional pending-artifact analysis result."""

    set_code: str
    output_dir: Path
    run_dir: Path
    work_dir: Path
    card_database_path: Path
    sources: EnrichmentSources
    run: EnrichmentRunResult
    counts: EnrichmentFindingCounts
    artifact: SemanticEnrichmentArtifact | None
    artifact_path: Path | None


@dataclass(frozen=True, slots=True)
class SetEnrichmentReviewResult:
    """Reviewed artifact and optional local profile publication result."""

    decision: EnrichmentReviewDecision
    artifact: SemanticEnrichmentArtifact
    artifact_path: Path
    publication: ProfilePublicationResult | None
    published_object_path: Path | None = None
    published_manifest_path: Path | None = None


class SetEnrichmentWorkflowError(RuntimeError):
    """Raised when a set-enrichment workflow cannot cross its next boundary."""

    def __init__(
        self,
        message: str,
        *,
        review_result: SetEnrichmentReviewResult | None = None,
    ) -> None:
        self.review_result = review_result
        super().__init__(message)


OUTPUT_DIRECTORY_ERROR = "The selected set-enrichment output directory is not usable."
CONTAINMENT_ERROR = "Set-enrichment output must stay inside the selected output directory."
CARD_CACHE_ERROR = "The set-enrichment card data cache must stay inside the selected output directory."
CARD_DATA_ERROR = "Set-enrichment card data acquisition failed."
GUIDE_ERROR = "Set-enrichment guide acquisition failed."
GUIDE_FREEZE_ERROR = "The frozen set-enrichment guide is inconsistent."
SOURCE_ERROR = "Set-enrichment sources could not be prepared."
ANALYSIS_ERROR = "Set-enrichment analysis failed."
WORK_INCOMPLETE_ERROR = "Set-enrichment durable work is incomplete."
ACCOUNTING_ERROR = "Set-enrichment accounting does not reconcile with its durable work."
ARTIFACT_COLLISION_ERROR = "A different set-enrichment artifact already occupies the content address."
INCOMPLETE_ANALYSIS_ERROR = "Set-enrichment analysis is not complete."
REVIEWER_ERROR = "reviewer_id must be a nonblank string."
REVIEW_DECISION_ERROR = "decision must be an EnrichmentReviewDecision value."
REVIEW_TIMESTAMP_ERROR = "reviewed_at must be a timezone-aware datetime."
REVIEW_ORDER_ERROR = "reviewed_at must not precede the artifact creation time."
REVIEW_PUBLICATION_ERROR = "Set-enrichment review artifact publication failed."
NO_PUBLISHABLE_ERROR = "Set enrichment has no publishable confirmed findings."
PROFILE_PUBLICATION_ERROR = "Set enrichment profile publication failed."

# The structured local matcher is a decision surface of its own, so it publishes one zero-cost run
# instead of borrowing the identity of a paid request that never produced these verdicts.
_LOCAL_MATCHER_PROVIDER = "draftomen"
_LOCAL_MATCHER_MODEL = "local-pair-matcher-v1"
_LOCAL_MATCHER_PROMPT_ID = "draftomen-local-pair-matcher-v1"
_LOCAL_MATCHER_RESPONSE_SCHEMA_ID = "draftomen-local-pair-resolution-v1"
_LOCAL_MATCHER_CONTRACT_VERSION = 1


def _workflow_error(
    message: str,
    error: BaseException | None = None,
    *,
    review_result: SetEnrichmentReviewResult | None = None,
) -> SetEnrichmentWorkflowError:
    """Build one bounded workflow error while retaining its original cause."""
    result = SetEnrichmentWorkflowError(message, review_result=review_result)
    if error is not None:
        result.__cause__ = error
    return result


def _resolved_root(output_dir: PathInput) -> Path:
    """Resolve and create the caller-owned output root."""
    try:
        root = Path(output_dir).expanduser().resolve()
    except (OSError, TypeError, ValueError) as error:
        raise _workflow_error(OUTPUT_DIRECTORY_ERROR, error) from error
    try:
        if root.exists() and not root.is_dir():
            raise _workflow_error(OUTPUT_DIRECTORY_ERROR)
        root.mkdir(parents=True, exist_ok=True)
    except SetEnrichmentWorkflowError:
        raise
    except OSError as error:
        raise _workflow_error(OUTPUT_DIRECTORY_ERROR, error) from error
    return root


def _contained(path: Path, root: Path) -> Path:
    """Validate one path and every existing nested component beneath the root."""
    try:
        candidate = path if path.is_absolute() else root / path
        candidate = Path(candidate)
        root_parts = root.parts
        candidate_parts = candidate.parts
        if len(candidate_parts) < len(root_parts) or candidate_parts[: len(root_parts)] != root_parts:
            raise _workflow_error(CONTAINMENT_ERROR)
        for index in range(len(root_parts) + 1, len(candidate_parts) + 1):
            component = Path(*candidate_parts[:index])
            if component.is_symlink():
                raise _workflow_error(CONTAINMENT_ERROR)
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
        return resolved
    except SetEnrichmentWorkflowError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise _workflow_error(CONTAINMENT_ERROR, error) from error


def _mkdir_contained(path: Path, root: Path) -> None:
    """Create one directory only after checking its complete containment chain."""
    _contained(path, root)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _workflow_error(CONTAINMENT_ERROR, error) from error
    _contained(path, root)


def _scan_no_symlinks(directory: Path, root: Path) -> None:
    """Reject any symlink or special entry inside one workflow-owned directory."""
    _contained(directory, root)
    if not directory.exists():
        return
    pending = [directory]
    try:
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink() or not (
                        entry.is_file(follow_symlinks=False)
                        or entry.is_dir(follow_symlinks=False)
                    ):
                        raise _workflow_error(CONTAINMENT_ERROR)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
    except OSError as error:
        raise _workflow_error(CONTAINMENT_ERROR, error) from error


def _assert_run_layout(sources_dir: Path, work_dir: Path, root: Path) -> None:
    """Validate the frozen source and durable work layout immediately before it is used."""
    for path in (
        sources_dir,
        sources_dir / "guide.json",
        sources_dir / "card-database.json",
        work_dir,
        work_dir / "attempts",
        work_dir / "responses",
        work_dir / "results",
    ):
        _contained(path, root)


def _contained_observer(
    observer: Callable[[EnrichmentProgress], None] | None,
    *,
    sources_dir: Path,
    work_dir: Path,
    root: Path,
) -> Callable[[EnrichmentProgress], None] | None:
    """Re-validate the run layout on every engine progress event before forwarding it."""
    if observer is None:
        return None

    def forward(event: EnrichmentProgress) -> None:
        _assert_run_layout(sources_dir=sources_dir, work_dir=work_dir, root=root)
        observer(event)

    return forward


def _fsync_directory(path: Path) -> None:
    """Flush one directory entry change best-effort."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _install_exclusive(path: Path, payload: bytes, root: Path) -> bool:
    """Install bytes at a content-addressed path without replacing an existing object."""
    _contained(path, root)
    _mkdir_contained(path.parent, root)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        _contained(path, root)
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            return False
        _fsync_directory(path.parent)
        return True
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


def _freeze_guide(
    *,
    guide_path: Path,
    root: Path,
    guide_url: str,
    normalized_set: str,
    guide_client: GuideClient,
) -> GuideSource:
    """Load a valid frozen guide or fetch and atomically freeze it once."""
    _contained(guide_path, root)
    try:
        present = guide_path.exists()
    except OSError as error:
        raise _workflow_error(GUIDE_FREEZE_ERROR, error) from error
    if present:
        try:
            value = _strict_json(guide_path.read_bytes())
            return _guide_freeze_record(
                value=value, guide_url=guide_url, normalized_set=normalized_set
            )
        except ProfilePublicationError as error:
            raise _workflow_error(GUIDE_FREEZE_ERROR, error) from error
        except (OSError, UnicodeDecodeError, TypeError, ValueError, RecursionError) as error:
            raise _workflow_error(GUIDE_FREEZE_ERROR, error) from error

    try:
        document = guide_client.fetch(url=guide_url)
    except (GuideClientError, OSError) as error:
        raise _workflow_error(GUIDE_ERROR, error) from error
    if not isinstance(document, GuideDocument):
        raise _workflow_error(SOURCE_ERROR)
    try:
        computed_sha256 = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
    except (AttributeError, TypeError, UnicodeError) as error:
        raise _workflow_error(GUIDE_FREEZE_ERROR, error) from error
    if document.sha256 != computed_sha256:
        raise _workflow_error(GUIDE_FREEZE_ERROR)
    guide_id = f"{normalized_set}-draftsim-guide"
    try:
        guide = GuideSource(
            guide_id=guide_id,
            url=document.url,
            text=document.text,
            retrieved_at=document.retrieved_at,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise _workflow_error(GUIDE_FREEZE_ERROR, error) from error
    record = {
        "schema_version": _GUIDE_SCHEMA_VERSION,
        "requested_url": guide_url,
        "guide_id": guide.guide_id,
        "url": guide.url,
        "text": guide.text,
        "sha256": guide.text_sha256,
        "retrieved_at": guide.retrieved_at,
    }
    try:
        payload = (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise _workflow_error(GUIDE_FREEZE_ERROR, error) from error
    try:
        if _install_exclusive(guide_path, payload, root):
            return guide
        value = _strict_json(guide_path.read_bytes())
        return _guide_freeze_record(value=value, guide_url=guide_url, normalized_set=normalized_set)
    except ProfilePublicationError as error:
        raise _workflow_error(GUIDE_FREEZE_ERROR, error) from error
    except (OSError, TypeError, ValueError, UnicodeError) as error:
        raise _workflow_error(GUIDE_FREEZE_ERROR, error) from error


def _build_work_identities(
    *,
    sources: EnrichmentSources,
    run: EnrichmentRunResult,
    model_config: WorkModelConfig,
) -> list[tuple[WorkIdentity, Any]]:
    """Rebuild every request identity in the engine's exact phase order."""
    identities: list[tuple[WorkIdentity, Any]] = []
    guides_by_id = {guide.guide_id: guide for guide in sources.guides}
    cards_by_id = {card.grp_id: card for card in sources.cards}
    for guide_id, result in zip(run.guide_ids, run.guide_results):
        guide = guides_by_id[guide_id]
        request = build_guide_extraction_request(sources=sources, guide_id=guide_id)
        identity = build_work_identity(
            work_kind=WorkKind.GUIDE,
            subject_id=guide_id,
            input_sha256=guide.text_sha256,
            request=request,
            model_config=model_config,
        )
        identities.append((identity, result))
    for card_id, result in zip(run.card_ids, run.card_results):
        card = cards_by_id[card_id]
        request = build_card_capability_extraction_request(sources=sources, card_id=card_id)
        identity = build_work_identity(
            work_kind=WorkKind.CARD_CAPABILITY,
            subject_id=str(card_id),
            input_sha256=card_source_sha256(card),
            request=request,
            model_config=model_config,
        )
        identities.append((identity, result))
    resolutions = run.candidate_resolutions
    if resolutions is None:
        if run.relationship_results:
            raise _workflow_error(WORK_INCOMPLETE_ERROR)
        if run.candidate_packages is not None:
            # A complete run resolves every constructed package before any relationship work.
            raise _workflow_error(WORK_INCOMPLETE_ERROR)
        return identities
    packages = resolutions.model_packages
    if len(run.relationship_results) != len(packages):
        raise _workflow_error(WORK_INCOMPLETE_ERROR)
    batches = partition_relationship_batches(packages)
    resolved = 0
    for batch_index, batch in enumerate(batches):
        request = build_relationship_validation_batch_request(sources=sources, packages=batch)
        identity = build_work_identity(
            work_kind=WorkKind.RELATIONSHIP,
            subject_id=relationship_batch_subject_id(batch_index),
            input_sha256=relationship_batch_source_sha256(request=request),
            request=request,
            model_config=model_config,
        )
        verdicts = run.relationship_results[resolved : resolved + len(batch)]
        resolved += len(batch)
        identities.append((identity, tuple(zip(batch, verdicts))))
    return identities


def _aware(value: datetime | None) -> datetime:
    """Require one timezone-aware datetime and normalize it to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _workflow_error(WORK_INCOMPLETE_ERROR)
    return value.astimezone(UTC)


def _model_runs(
    *,
    store: SetEnrichmentWorkStore,
    identities: list[tuple[WorkIdentity, Any]],
    model_config: WorkModelConfig,
) -> tuple[tuple[ModelRun, ...], dict[str, Any]]:
    """Read completed durable response provenance and build model runs."""
    model_runs: list[ModelRun] = []
    records: dict[str, Any] = {}
    seen: set[str] = set()
    for identity, context in identities:
        key = identity.content_sha256
        if key in seen:
            continue
        seen.add(key)
        try:
            record = store.lookup(identity=identity)
        except Exception as error:
            raise _workflow_error(WORK_INCOMPLETE_ERROR, error) from error
        if (
            record.state is not WorkState.COMPLETED
            or record.response is None
            or record.completed_at is None
        ):
            raise _workflow_error(WORK_INCOMPLETE_ERROR)
        completed_at = _aware(record.completed_at)
        started_at = _aware(record.attempted_at if record.attempted_at is not None else record.responded_at)
        response = record.response
        try:
            model_run = ModelRun(
                run_id=f"work-{key}",
                provider="openrouter" if response.provider is None else response.provider,
                model=response.model,
                reasoning=ReasoningConfig(
                    enabled=None,
                    effort=identity.model_config.reasoning_effort,
                    max_tokens=None,
                    exclude=None,
                ),
                prompt_id=identity.prompt_id,
                prompt_sha256=identity.prompt_sha256,
                response_schema_id=identity.response_schema_id,
                response_schema_sha256=identity.response_schema_sha256,
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                reasoning_tokens=response.reasoning_tokens,
                cost_usd=response.cost_usd,
            )
        except (TypeError, ValueError, UnicodeError) as error:
            raise _workflow_error(WORK_INCOMPLETE_ERROR, error) from error
        model_runs.append(model_run)
        records[key] = (record, context)
    return tuple(model_runs), records


def _namespaced(identity: WorkIdentity, finding_id: str) -> str:
    """Namespace one extraction finding under its durable request identity."""
    return f"work-{identity.content_sha256}:{finding_id}"


def _canonical_payload(value: Any) -> bytes:
    """Encode one provenance payload as its compact canonical JSON bytes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _local_claim(package: CandidatePackage) -> str:
    """Return the deterministic claim one locally decided pair publishes."""
    return (
        f"{package.source.card_name} supports {package.target.card_name}"
        f" through {package.mechanism}."
    )


def _local_namespaced(run_id: str, finding_id: str) -> str:
    """Namespace one local matcher finding under the local run that decided it."""
    return f"{run_id}:{finding_id}"


def _local_matcher_run(
    *,
    resolutions: CandidateResolutionSet | None,
    created_at: str,
) -> ModelRun | None:
    """Build the one zero-cost run that records this artifact's local decisions, or None.

    The run's identifiers pin the exact matcher contract, the ordered input packages, and the
    ordered resolutions, so the same decisions always publish the same provenance.
    """
    if not isinstance(resolutions, CandidateResolutionSet):
        raise _workflow_error(ANALYSIS_ERROR)
    if not resolutions.local_accepted and not resolutions.local_rejected:
        return None
    packages = [resolution.package.to_json() for resolution in resolutions.resolutions]
    outcomes = [resolution.to_json() for resolution in resolutions.resolutions]
    prompt_sha256 = hashlib.sha256(
        _canonical_payload(
            {"contract_version": _LOCAL_MATCHER_CONTRACT_VERSION, "packages": packages}
        )
    ).hexdigest()
    response_schema_sha256 = hashlib.sha256(
        _canonical_payload(
            {"contract_version": _LOCAL_MATCHER_CONTRACT_VERSION, "resolutions": outcomes}
        )
    ).hexdigest()
    decision_sha256 = hashlib.sha256(
        _canonical_payload(
            {
                "contract_version": _LOCAL_MATCHER_CONTRACT_VERSION,
                "packages": packages,
                "resolutions": outcomes,
            }
        )
    ).hexdigest()
    try:
        return ModelRun(
            run_id=f"local-{decision_sha256[:16]}",
            provider=_LOCAL_MATCHER_PROVIDER,
            model=_LOCAL_MATCHER_MODEL,
            reasoning=ReasoningConfig(enabled=None, effort=None, max_tokens=None, exclude=None),
            prompt_id=_LOCAL_MATCHER_PROMPT_ID,
            prompt_sha256=prompt_sha256,
            response_schema_id=_LOCAL_MATCHER_RESPONSE_SCHEMA_ID,
            response_schema_sha256=response_schema_sha256,
            started_at=created_at,
            completed_at=created_at,
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            cost_usd="0",
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise _workflow_error(ANALYSIS_ERROR, error) from error


def _capability_claim(capability: CardCapability) -> str:
    """Serialize only the validated structured fields of one capability."""
    value = capability.to_json()
    selected = {
        key: value[key]
        for key in (
            "card_name",
            "face_index",
            "face_name",
            "role",
            "action",
            "zone",
            "qualifier",
            "quantity",
            "timing",
            "source_zone",
            "destination_zone",
            "prerequisites",
        )
    }
    return json.dumps(
        selected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _mapped_findings(
    *,
    identities: list[tuple[WorkIdentity, Any]],
    resolutions: CandidateResolutionSet | None,
    local_run_id: str | None,
) -> tuple[tuple[OracleFact, ...], tuple[GuideClaim, ...], tuple[CardRelationship, ...], tuple[RejectedFinding, ...]]:
    """Map extraction findings into source-bound semantic records.

    Local and residual model outcomes merge in canonical candidate order, so every pair keeps the
    provenance of the run that actually decided it.
    """
    facts: list[OracleFact] = []
    claims: list[GuideClaim] = []
    candidates: list[CardRelationship] = []
    rejected: list[RejectedFinding] = []
    verdicts: dict[tuple[str, int, str, int, str], tuple[WorkIdentity, RelationshipValidationResult]] = {}
    for identity, context in identities:
        if identity.work_kind is WorkKind.GUIDE:
            result = context
            if not isinstance(result, GuideExtractionResult):
                raise _workflow_error(ANALYSIS_ERROR)
            for claim in (*result.accepted_findings, *result.uncertain_findings):
                claims.append(
                    GuideClaim(
                        finding_id=_namespaced(identity, claim.finding_id),
                        category=claim.category,
                        name=claim.name,
                        claim=claim.claim,
                        card_ids=claim.card_ids,
                        evidence=claim.evidence,
                        review=claim.review,
                        run_id=f"work-{identity.content_sha256}",
                    )
                )
            for finding in result.rejected_findings:
                rejected.append(
                    RejectedFinding(
                        finding_id=_namespaced(identity, finding.finding_id),
                        source_kind=finding.source_kind,
                        summary=finding.summary,
                        reason=finding.reason,
                        run_id=f"work-{identity.content_sha256}",
                    )
                )
        elif identity.work_kind is WorkKind.CARD_CAPABILITY:
            result = context
            if not isinstance(result, CardCapabilityExtractionResult):
                raise _workflow_error(ANALYSIS_ERROR)
            for capability in (*result.accepted_capabilities, *result.uncertain_capabilities):
                facts.append(
                    OracleFact(
                        finding_id=_namespaced(identity, capability.finding_id),
                        card_id=capability.card_id,
                        kind=capability.role.value,
                        claim=_capability_claim(capability),
                        evidence=capability.evidence,
                        review=capability.review,
                        run_id=f"work-{identity.content_sha256}",
                    )
                )
            for finding in result.rejected_capabilities:
                rejected.append(
                    RejectedFinding(
                        finding_id=_namespaced(identity, finding.finding_id),
                        source_kind=finding.source_kind,
                        summary=finding.summary,
                        reason=finding.reason,
                        run_id=f"work-{identity.content_sha256}",
                    )
                )
        else:
            # A relationship identity resolves one whole batch, so its verdicts stay grouped here
            # and are re-expanded per pair once the canonical resolutions bind them to their pair.
            pairs = context
            if not isinstance(pairs, tuple) or not pairs:
                raise _workflow_error(ANALYSIS_ERROR)
            for package, result in pairs:
                if not isinstance(package, CandidatePackage) or not isinstance(
                    result, RelationshipValidationResult
                ):
                    raise _workflow_error(ANALYSIS_ERROR)
                verdicts[package.identity] = (identity, result)
    if resolutions is None:
        if verdicts:
            raise _workflow_error(ANALYSIS_ERROR)
        return tuple(facts), tuple(claims), (), tuple(rejected)
    if not isinstance(resolutions, CandidateResolutionSet):
        raise _workflow_error(ANALYSIS_ERROR)
    if len(verdicts) != len(resolutions.model_packages):
        raise _workflow_error(ANALYSIS_ERROR)
    if local_run_id is not None and not isinstance(local_run_id, str):
        raise _workflow_error(ANALYSIS_ERROR)
    for resolution in resolutions.resolutions:
        if not isinstance(resolution, CandidateResolution):
            raise _workflow_error(ANALYSIS_ERROR)
        package = resolution.package
        subject_id = relationship_subject_id(
            mechanism=package.mechanism,
            source=package.source,
            target=package.target,
        )
        if resolution.basis is CandidateResolutionBasis.LOCAL:
            if local_run_id is None:
                raise _workflow_error(ANALYSIS_ERROR)
            claim = _local_claim(package)
            finding_id = _local_namespaced(local_run_id, subject_id)
            if resolution.verdict is CandidateResolutionVerdict.ACCEPTED:
                candidates.append(
                    CardRelationship(
                        finding_id=finding_id,
                        mechanism=package.mechanism,
                        participants=(package.source.card_id, package.target.card_id),
                        claim=claim,
                        prerequisites=(package.reason, LOCAL_PROVE_REASON),
                        oracle_evidence=package.source.evidence + package.target.evidence,
                        guide_evidence=(),
                        review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
                        run_id=local_run_id,
                    )
                )
            else:
                rejected.append(
                    RejectedFinding(
                        finding_id=finding_id,
                        source_kind="relationship",
                        summary=claim,
                        reason=resolution.reason,
                        run_id=local_run_id,
                    )
                )
            continue
        entry = verdicts.get(resolution.identity)
        if entry is None:
            raise _workflow_error(ANALYSIS_ERROR)
        identity, result = entry
        if result.relationship is not None:
            relationship = result.relationship
            candidates.append(
                CardRelationship(
                    finding_id=_namespaced(identity, relationship.finding_id),
                    mechanism=relationship.mechanism,
                    participants=(relationship.source.card_id, relationship.target.card_id),
                    claim=relationship.claim,
                    prerequisites=(package.reason,),
                    oracle_evidence=relationship.evidence,
                    guide_evidence=(),
                    review=relationship.review,
                    run_id=f"work-{identity.content_sha256}",
                    prerequisite_projection=relationship.prerequisite_projection,
                )
            )
        if result.rejected is not None:
            finding = result.rejected
            rejected.append(
                RejectedFinding(
                    finding_id=_namespaced(identity, finding.finding_id),
                    source_kind=finding.source_kind,
                    summary=finding.summary,
                    reason=finding.reason,
                    run_id=f"work-{identity.content_sha256}",
                )
            )
    return (
        tuple(facts),
        tuple(claims),
        tuple(candidates),
        tuple(rejected),
    )


def _project_relationships(candidates: tuple[CardRelationship, ...]) -> tuple[CardRelationship, ...]:
    """Retain one accepted or uncertain candidate for each semantic identity."""
    grouped: dict[tuple[str, tuple[int, ...], tuple[int | str, ...]], list[CardRelationship]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.identity, []).append(candidate)
    selected: list[CardRelationship] = []
    for values in grouped.values():
        accepted = [item for item in values if item.review.status is FindingStatus.ACCEPTED]
        pool = accepted if accepted else [item for item in values if item.review.status is FindingStatus.UNCERTAIN]
        if pool:
            selected.append(min(pool, key=lambda item: item.finding_id))
    return tuple(selected)


def _counts(run: EnrichmentRunResult) -> EnrichmentFindingCounts:
    """Count original extraction results without counting candidate omissions."""
    resolutions = run.candidate_resolutions
    accepted = sum(len(item.accepted_findings) for item in run.guide_results)
    accepted += sum(len(item.accepted_capabilities) for item in run.card_results)
    accepted += sum(
        1
        for item in run.relationship_results
        if item.relationship is not None and item.relationship.review.status is FindingStatus.ACCEPTED
    )
    if resolutions is not None:
        accepted += len(resolutions.local_accepted)
    uncertain = sum(len(item.uncertain_findings) for item in run.guide_results)
    uncertain += sum(len(item.uncertain_capabilities) for item in run.card_results)
    uncertain += sum(
        1
        for item in run.relationship_results
        if item.relationship is not None and item.relationship.review.status is FindingStatus.UNCERTAIN
    )
    rejected = sum(len(item.rejected_findings) for item in run.guide_results)
    rejected += sum(len(item.rejected_capabilities) for item in run.card_results)
    rejected += sum(1 for item in run.relationship_results if item.rejected is not None)
    if resolutions is not None:
        rejected += len(resolutions.local_rejected)
    failed = sum(
        1 for item in (*run.guide_results, *run.card_results, *run.relationship_results)
        if item.outcome is ExtractionOutcome.MALFORMED
    )
    return EnrichmentFindingCounts(
        accepted=accepted,
        uncertain=uncertain,
        rejected=rejected,
        failed=failed,
    )


def _reconcile(
    *,
    accounting: EnrichmentAccounting,
    model_runs: tuple[ModelRun, ...],
    cached_input_tokens: int,
) -> None:
    """Reconcile durable model-run provenance with the engine's final ledger."""
    if len(model_runs) != accounting.executed_work + accounting.reused_work:
        raise _workflow_error(ACCOUNTING_ERROR)
    if sum(run.input_tokens or 0 for run in model_runs) != accounting.input_tokens:
        raise _workflow_error(ACCOUNTING_ERROR)
    if sum(run.output_tokens or 0 for run in model_runs) != accounting.output_tokens:
        raise _workflow_error(ACCOUNTING_ERROR)
    if cached_input_tokens != accounting.cached_input_tokens:
        raise _workflow_error(ACCOUNTING_ERROR)
    if sum(run.reasoning_tokens or 0 for run in model_runs) != accounting.reasoning_tokens:
        raise _workflow_error(ACCOUNTING_ERROR)
    missing = sum(run.cost_usd is None for run in model_runs)
    if missing != accounting.work_without_cost:
        raise _workflow_error(ACCOUNTING_ERROR)
    try:
        running = sum((Decimal(run.cost_usd) for run in model_runs if run.cost_usd is not None), Decimal(0))
        accounted = Decimal(accounting.running_cost_usd)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _workflow_error(ACCOUNTING_ERROR, error) from error
    if running != accounted:
        raise _workflow_error(ACCOUNTING_ERROR)


def _publish_artifact(*, artifact: SemanticEnrichmentArtifact, artifacts_dir: Path, root: Path) -> Path:
    """Publish or reuse one canonical content-addressed semantic artifact."""
    try:
        payload = artifact.to_bytes()
    except Exception as error:
        raise _workflow_error(ANALYSIS_ERROR, error) from error
    path = artifacts_dir / f"{hashlib.sha256(payload).hexdigest()}.json"
    _contained(path, root)
    try:
        if _install_exclusive(path, payload, root):
            return path
        if path.read_bytes() == payload:
            return path
        raise _workflow_error(ARTIFACT_COLLISION_ERROR)
    except SetEnrichmentWorkflowError:
        raise
    except OSError as error:
        raise _workflow_error(ANALYSIS_ERROR, error) from error


def _artifact_from_parts(
    *,
    sources: EnrichmentSources,
    created_at: str,
    runs: tuple[ModelRun, ...],
    facts: tuple[OracleFact, ...],
    claims: tuple[GuideClaim, ...],
    relationships: tuple[CardRelationship, ...],
    rejected: tuple[RejectedFinding, ...],
    review: ArtifactReview,
    confirmed_relationship_ids: tuple[str, ...],
) -> SemanticEnrichmentArtifact:
    """Build one immutable source-bound semantic artifact."""
    cards = tuple(
        CardSourcePin(
            card_id=card.grp_id,
            oracle_id=card.oracle_id,
            collector_number=card.collector_number,
            sha256=card_source_sha256(card),
        )
        for card in sources.cards
    )
    guides = tuple(
        GuideSourcePin(
            guide_id=guide.guide_id,
            url=guide.url,
            sha256=guide.text_sha256,
            retrieved_at=guide.retrieved_at,
        )
        for guide in sources.guides
    )
    return SemanticEnrichmentArtifact(
        set_code=sources.set_code,
        set_source_id=f"draftomen-card-data-v1-{sources.set_code}",
        set_source_sha256=set_source_sha256(sources),
        created_at=created_at,
        cards=cards,
        guides=guides,
        runs=runs,
        oracle_facts=facts,
        guide_claims=claims,
        relationships=relationships,
        rejected_findings=rejected,
        review=review,
        confirmed_relationship_ids=confirmed_relationship_ids,
        sources=sources,
    )


def analyze_set_enrichment(
    *,
    set_code: str,
    guide_url: str,
    output_dir: PathInput,
    model_config: WorkModelConfig = DEFAULT_ENRICHMENT_MODEL_CONFIG,
    completion: Completion | None = None,
    observer: Callable[[EnrichmentProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    card_data_client: CardDataClient | None = None,
    guide_client: GuideClient | None = None,
    run_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SetEnrichmentWorkflowResult:
    """Acquire frozen sources, run resumable analysis, and publish a pending artifact."""
    root = _resolved_root(output_dir)
    runs_dir = root / "enrichment-runs"
    _mkdir_contained(runs_dir, root)
    try:
        expected_cache = card_data_cache_path(set_code=set_code, app_dir=runs_dir)
        normalized_set = expected_cache.name.removesuffix(".json.gz")
    except (CardDataClientError, OSError, TypeError, ValueError) as error:
        raise _workflow_error(CARD_DATA_ERROR, error) from error
    _contained(expected_cache, root)
    client = card_data_client if card_data_client is not None else CardDataClient(app_dir=runs_dir)
    try:
        actual_cache = Path(client.cache_path(set_code))
        if actual_cache.expanduser().resolve() != expected_cache.resolve():
            raise _workflow_error(CARD_CACHE_ERROR)
    except SetEnrichmentWorkflowError:
        raise
    except (CardDataClientError, OSError, TypeError, ValueError) as error:
        raise _workflow_error(CARD_CACHE_ERROR, error) from error
    # An injected client may reach the same destination through a root whose ancestors are
    # symbolic links, so containment is enforced on the prescribed path the run relies on.
    _contained(expected_cache, root)
    try:
        card_database = client.load(set_code=normalized_set, allow_network=True)
        if not isinstance(card_database, CardDatabase):
            raise TypeError("card client did not return CardDatabase")
    except (CardDataClientError, CardDatabaseError, OSError, TypeError, ValueError) as error:
        raise _workflow_error(CARD_DATA_ERROR, error) from error

    try:
        guide_key = hashlib.sha256(guide_url.encode("utf-8")).hexdigest()[:16]
    except (AttributeError, UnicodeError) as error:
        raise _workflow_error(GUIDE_ERROR, error) from error
    run_dir = runs_dir / normalized_set / guide_key
    sources_dir = run_dir / "sources"
    guide_path = sources_dir / "guide.json"
    card_database_path = sources_dir / "card-database.json"
    work_dir = run_dir / "work"
    artifacts_dir = run_dir / "artifacts"
    for path in (run_dir, sources_dir, guide_path, card_database_path, work_dir, artifacts_dir):
        _contained(path, root)
    _mkdir_contained(sources_dir, root)
    try:
        save_card_database(card_database, cache_path=card_database_path)
    except (CardDatabaseError, OSError, TypeError, ValueError, UnicodeError) as error:
        raise _workflow_error(CARD_DATA_ERROR, error) from error
    _contained(card_database_path, root)
    frozen_guide = _freeze_guide(
        guide_path=guide_path,
        root=root,
        guide_url=guide_url,
        normalized_set=normalized_set,
        guide_client=guide_client if guide_client is not None else GuideClient(),
    )
    try:
        sources = EnrichmentSources(
            set_code=normalized_set,
            cards=tuple(card_database.cards.values()),
            guides=(frozen_guide,),
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise _workflow_error(SOURCE_ERROR, error) from error
    _mkdir_contained(work_dir, root)
    _contained(artifacts_dir, root)
    _assert_run_layout(sources_dir=sources_dir, work_dir=work_dir, root=root)
    try:
        store = SetEnrichmentWorkStore(work_dir, clock=clock)
        complete = completion if completion is not None else openrouter_completion(model_config=model_config)
        run = run_set_enrichment(
            sources=sources,
            complete=complete,
            work_store=store,
            model_config=model_config,
            run_id=run_id,
            observer=_contained_observer(
                observer,
                sources_dir=sources_dir,
                work_dir=work_dir,
                root=root,
            ),
            is_cancelled=is_cancelled,
        )
    except KeyboardInterrupt:
        raise
    except Exception as error:
        raise _workflow_error(ANALYSIS_ERROR, error) from error
    finding_counts = _counts(run)
    if run.outcome is EnrichmentOutcome.CANCELLED:
        return SetEnrichmentWorkflowResult(
            set_code=normalized_set,
            output_dir=root,
            run_dir=run_dir,
            work_dir=work_dir,
            card_database_path=card_database_path,
            sources=sources,
            run=run,
            counts=finding_counts,
            artifact=None,
            artifact_path=None,
        )
    if run.outcome is not EnrichmentOutcome.COMPLETE:
        raise _workflow_error(ANALYSIS_ERROR)
    try:
        identities = _build_work_identities(
            sources=sources,
            run=run,
            model_config=model_config,
        )
        _assert_run_layout(sources_dir=sources_dir, work_dir=work_dir, root=root)
        model_runs, records = _model_runs(
            store=store,
            identities=identities,
            model_config=model_config,
        )
        completions = [
            datetime.fromisoformat(model_run.completed_at.replace("Z", "+00:00"))
            for model_run in model_runs
        ]
        if not completions or any(
            value.tzinfo is None or value.utcoffset() is None for value in completions
        ):
            raise _workflow_error(WORK_INCOMPLETE_ERROR)
        created_at = max(completions).astimezone(UTC).isoformat()
        local_run = _local_matcher_run(
            resolutions=run.candidate_resolutions,
            created_at=created_at,
        )
        facts, claims, candidate_relationships, rejected = _mapped_findings(
            identities=identities,
            resolutions=run.candidate_resolutions,
            local_run_id=None if local_run is None else local_run.run_id,
        )
        relationships = _project_relationships(candidate_relationships)
        artifact = _artifact_from_parts(
            sources=sources,
            created_at=created_at,
            runs=model_runs if local_run is None else (*model_runs, local_run),
            facts=facts,
            claims=claims,
            relationships=relationships,
            rejected=rejected,
            review=ArtifactReview(state="pending", reviewer_id=None, reviewed_at=None),
            confirmed_relationship_ids=(),
        )
        try:
            cached_input_tokens = sum(
                record.response.cached_input_tokens
                if type(record.response.cached_input_tokens) is int
                else 0
                for record, _context in records.values()
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise _workflow_error(ACCOUNTING_ERROR, error) from error
        _reconcile(
            accounting=run.progress.accounting,
            model_runs=model_runs,
            cached_input_tokens=cached_input_tokens,
        )
        _assert_run_layout(sources_dir=sources_dir, work_dir=work_dir, root=root)
        artifact_path = _publish_artifact(artifact=artifact, artifacts_dir=artifacts_dir, root=root)
    except SetEnrichmentWorkflowError:
        raise
    except (SetEnrichmentWorkError, TypeError, ValueError, UnicodeError, OSError) as error:
        raise _workflow_error(ANALYSIS_ERROR, error) from error
    return SetEnrichmentWorkflowResult(
        set_code=normalized_set,
        output_dir=root,
        run_dir=run_dir,
        work_dir=work_dir,
        card_database_path=card_database_path,
        sources=sources,
        run=run,
        counts=finding_counts,
        artifact=artifact,
        artifact_path=artifact_path,
    )


def _parse_created_at(value: str) -> datetime:
    """Parse one already-validated artifact creation timestamp."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise _workflow_error(INCOMPLETE_ANALYSIS_ERROR, error) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _workflow_error(INCOMPLETE_ANALYSIS_ERROR)
    return parsed.astimezone(UTC)


def _reviewed_artifact(
    *,
    artifact: SemanticEnrichmentArtifact,
    sources: EnrichmentSources,
    review: ArtifactReview,
    confirmed_relationship_ids: tuple[str, ...],
) -> SemanticEnrichmentArtifact:
    """Rebuild an immutable artifact with its completed review metadata."""
    return SemanticEnrichmentArtifact(
        schema_version=artifact.schema_version,
        set_code=artifact.set_code,
        set_source_id=artifact.set_source_id,
        set_source_sha256=artifact.set_source_sha256,
        created_at=artifact.created_at,
        cards=artifact.cards,
        guides=artifact.guides,
        runs=artifact.runs,
        oracle_facts=artifact.oracle_facts,
        guide_claims=artifact.guide_claims,
        relationships=artifact.relationships,
        rejected_findings=artifact.rejected_findings,
        review=review,
        confirmed_relationship_ids=confirmed_relationship_ids,
        sources=sources,
    )


def finalize_set_enrichment(
    *,
    analysis: SetEnrichmentWorkflowResult,
    decision: EnrichmentReviewDecision,
    reviewer_id: str,
    reviewed_at: datetime,
    profiles_dir: PathInput = Path("website/public/profiles"),
) -> SetEnrichmentReviewResult:
    """Publish a review decision and optionally generate and publish the confirmed profile.

    ``profiles_dir`` is resolved from the process current directory only after the
    reviewed artifact is durable and only for Confirm, so Cancel never inspects or
    creates the repository profile tree.
    """
    if (
        not isinstance(analysis, SetEnrichmentWorkflowResult)
        or analysis.run.outcome is not EnrichmentOutcome.COMPLETE
        or analysis.artifact is None
        or analysis.artifact_path is None
        or analysis.artifact.review.state != "pending"
    ):
        raise _workflow_error(INCOMPLETE_ANALYSIS_ERROR)
    if not isinstance(decision, EnrichmentReviewDecision):
        raise _workflow_error(REVIEW_DECISION_ERROR)
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise _workflow_error(REVIEWER_ERROR)
    if not isinstance(reviewed_at, datetime) or reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise _workflow_error(REVIEW_TIMESTAMP_ERROR)
    normalized_reviewed_at = reviewed_at.astimezone(UTC)
    created_at = _parse_created_at(analysis.artifact.created_at)
    if normalized_reviewed_at < created_at:
        raise _workflow_error(REVIEW_ORDER_ERROR)
    normalized_reviewer = reviewer_id.strip()
    reviewed = _reviewed_artifact(
        artifact=analysis.artifact,
        sources=analysis.sources,
        review=ArtifactReview(
            state="confirmed" if decision is EnrichmentReviewDecision.CONFIRM else "cancelled",
            reviewer_id=normalized_reviewer,
            reviewed_at=normalized_reviewed_at.isoformat(),
        ),
        confirmed_relationship_ids=(
            tuple(
                relationship.finding_id
                for relationship in analysis.artifact.relationships
                if relationship.review.status is FindingStatus.ACCEPTED
            )
            if decision is EnrichmentReviewDecision.CONFIRM
            else ()
        ),
    )
    _scan_no_symlinks(analysis.run_dir / "sources", root=analysis.output_dir)
    _contained(analysis.card_database_path, analysis.output_dir)
    artifacts_dir = analysis.run_dir / "artifacts"
    _contained(artifacts_dir, analysis.output_dir)
    try:
        artifact_path = _publish_artifact(
            artifact=reviewed,
            artifacts_dir=artifacts_dir,
            root=analysis.output_dir,
        )
    except SetEnrichmentWorkflowError as error:
        if str(error) == CONTAINMENT_ERROR:
            raise
        raise _workflow_error(REVIEW_PUBLICATION_ERROR, error) from error
    except Exception as error:
        raise _workflow_error(REVIEW_PUBLICATION_ERROR, error) from error
    review_result = SetEnrichmentReviewResult(
        decision=decision,
        artifact=reviewed,
        artifact_path=artifact_path,
        publication=None,
    )
    if decision is EnrichmentReviewDecision.CANCEL:
        return review_result
    try:
        publishable = any(
            claim.category == "mechanic" and claim.review.status is FindingStatus.ACCEPTED
            for claim in reviewed.guide_claims
        ) or bool(reviewed.confirmed_relationships)
        if not publishable:
            raise SetEnrichmentWorkflowError(
                NO_PUBLISHABLE_ERROR,
                review_result=review_result,
            )
        profile_root = analysis.output_dir / f"{analysis.set_code}-{QUICK_DRAFT_FORMAT.casefold()}"
        _contained(profile_root, analysis.output_dir)
        _contained(profile_root / "artifacts", analysis.output_dir)
        _contained(profile_root / "generation.json", analysis.output_dir)
        _scan_no_symlinks(profile_root, root=analysis.output_dir)
        publication = generate_local_profile_artifacts(
            set_code=analysis.set_code,
            event_format=QUICK_DRAFT_FORMAT,
            stage=ProfileGenerationStage.METADATA,
            generated_at=normalized_reviewed_at,
            card_database_path=analysis.card_database_path,
            output_dir=analysis.output_dir,
            enrichment=reviewed,
        )
        _contained(publication.artifact_path, analysis.output_dir)
        if publication.manifest_path != profile_root / "generation.json":
            raise SetEnrichmentWorkflowError(
                PROFILE_PUBLICATION_ERROR,
                review_result=review_result,
            )
    except SetEnrichmentWorkflowError as error:
        if error.review_result is not None:
            raise
        raise SetEnrichmentWorkflowError(
            str(error),
            review_result=review_result,
        ) from error
    except (ProfilePublicationError, OSError, TypeError, ValueError) as error:
        raise SetEnrichmentWorkflowError(
            PROFILE_PUBLICATION_ERROR,
            review_result=review_result,
        ) from error
    return _publish_confirmed_profile(
        publication=publication,
        profiles_dir=profiles_dir,
        published_at=normalized_reviewed_at,
        run_id=analysis.run_dir.name,
        review_result=SetEnrichmentReviewResult(
            decision=decision,
            artifact=reviewed,
            artifact_path=artifact_path,
            publication=publication,
        ),
    )


def _publish_confirmed_profile(
    *,
    publication: ProfilePublicationResult,
    profiles_dir: PathInput,
    published_at: datetime,
    run_id: str,
    review_result: SetEnrichmentReviewResult,
) -> SetEnrichmentReviewResult:
    """Install one confirmed profile in the repository and keep the review result intact.

    Every repository write belongs to ``publish_profile_publication``, which loads
    the manifest before writing anything, installs the immutable object before the
    provenance record, and the record before the manifest entry that names it.
    This wrapper only attaches the installation paths to the confirmed review
    result and maps any boundary failure onto the workflow error taxonomy.
    """
    try:
        installed = publish_profile_publication(
            publication=publication,
            profiles_dir=profiles_dir,
            published_at=published_at,
            run_id=run_id,
        )
    except (
        OSError,
        ProfileManifestError,
        ProfilePublicationError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:
        raise _workflow_error(
            PROFILE_PUBLICATION_ERROR,
            error,
            review_result=review_result,
        ) from error
    return SetEnrichmentReviewResult(
        decision=review_result.decision,
        artifact=review_result.artifact,
        artifact_path=review_result.artifact_path,
        publication=publication,
        published_object_path=installed.object_path,
        published_manifest_path=installed.manifest_path,
    )


__all__ = [
    "ACCOUNTING_ERROR",
    "ANALYSIS_ERROR",
    "ARTIFACT_COLLISION_ERROR",
    "CARD_CACHE_ERROR",
    "CARD_DATA_ERROR",
    "CONTAINMENT_ERROR",
    "DEFAULT_ENRICHMENT_MODEL_CONFIG",
    "EnrichmentFindingCounts",
    "EnrichmentReviewDecision",
    "GUIDE_ERROR",
    "GUIDE_FREEZE_ERROR",
    "INCOMPLETE_ANALYSIS_ERROR",
    "NO_PUBLISHABLE_ERROR",
    "OUTPUT_DIRECTORY_ERROR",
    "PROFILE_PUBLICATION_ERROR",
    "REVIEWER_ERROR",
    "REVIEW_DECISION_ERROR",
    "REVIEW_ORDER_ERROR",
    "REVIEW_PUBLICATION_ERROR",
    "REVIEW_TIMESTAMP_ERROR",
    "SOURCE_ERROR",
    "SetEnrichmentReviewResult",
    "SetEnrichmentWorkflowError",
    "SetEnrichmentWorkflowResult",
    "WORK_INCOMPLETE_ERROR",
    "analyze_set_enrichment",
    "finalize_set_enrichment",
]
