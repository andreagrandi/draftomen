"""Run the complete HOB set-enrichment analysis and compare it with the reviewed benchmark.
A dry run is free and offline; every live request is bounded by an explicit spending ceiling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from draftomen.carddb import CardInfo
from draftomen.openrouter_client import (
    OpenRouterClient,
    OpenRouterClientError,
    OpenRouterResponse,
)
from draftomen.paths import app_data_dir
from draftomen.semantic_enrichment import (
    EnrichmentSources,
    GuideSource,
    card_source_sha256,
    set_source_sha256,
)
from draftomen.semantic_enrichment_records import FindingStatus
from draftomen.set_card_data import SetCardData, SetCardDataError
from draftomen.set_enrichment import (
    Completion,
    EnrichmentOutcome,
    EnrichmentProgress,
    EnrichmentRunResult,
    openrouter_completion,
    run_set_enrichment,
)
from draftomen.set_enrichment_candidates import ROLE_COMPATIBILITY_RULES
from draftomen.set_enrichment_extraction import (
    CARD_CAPABILITY_EXTRACTION_PROMPT_ID,
    GUIDE_EXTRACTION_PROMPT_ID,
    RELATIONSHIP_VALIDATION_PROMPT_ID,
    ExtractionRequest,
    build_card_capability_extraction_request,
    build_guide_extraction_request,
    build_relationship_validation_request,
    relationship_source_sha256,
    relationship_subject_id,
)
from draftomen.set_enrichment_work import (
    SetEnrichmentWorkStore,
    WorkKind,
    WorkModelConfig,
    build_work_identity,
)

SET_CODE = "hob"
CARD_ARTIFACT_RELATIVE_PATH = Path("website/public/card-data/hob.json.gz")
GUIDE_FILE_NAME = "guide.txt"
BENCHMARK_FILE_NAME = "benchmark.json"
REPORT_JSON_NAME = "report.json"
REPORT_MD_NAME = "report.md"

MODEL = "openai/gpt-5.6-luna"
# Effort is part of the durable-work identity, so changing it discards prior extractions and re-runs
# them. Requested setting for the verification run; measured live at 10.0s and $0.00163 per card
# (1,144 output tokens, 645 of them reasoning) against 31.7s and $0.00326 at `high`.
REASONING_EFFORT = "medium"
# The provider caps completions for this model at 128000 tokens. A guide extraction over the frozen
# guide exceeds 32000 tokens of reasoning plus findings and comes back truncated, which the client
# classifies as a terminal failure, so the full cap is requested.
MAX_TOKENS = 128000

DEFAULT_MAX_USD = "1.00"
DEFAULT_WORK_DIR = Path(".draftomen/enrichment-runs/hob/work")
DEFAULT_RUN_DIR = Path(".draftomen/enrichment-runs/hob")

DRY_RUN_MODEL = "draftomen/hob-dry-run"
DRY_RUN_PROVIDER = "dry-run"
DRY_RUN_COST_USD = "0.002"
DRY_RUN_INPUT_TOKENS = 1200
DRY_RUN_CACHED_INPUT_TOKENS = 300
DRY_RUN_OUTPUT_TOKENS = 200
DRY_RUN_REASONING_TOKENS = 40

MAX_QUOTE_CHARS = 240
DRY_RUN_ROLE_CYCLE = ("token_maker", "go_wide_payoff", "token_maker", "go_wide_payoff")
DRY_RUN_FILLER_ROLE = "draw"

MINIMUM_OVERLAP_CHARS = 20

EXIT_OK = 0
EXIT_ACCEPTANCE_FAILURE = 1
EXIT_UNUSABLE_INPUT = 2
EXIT_SPEND_CEILING = 3

_BENCHMARK_SCHEMA_VERSION = 1
_BENCHMARK_GUIDE_KEYS = ("guide_id", "url", "retrieved_at", "sha256", "chars")
_MECHANIC_KEYS = (
    "name",
    "guide_quote",
    "oracle_card_id",
    "oracle_face_index",
    "oracle_quote",
    "required",
)
_RELATIONSHIP_EXPECTATION_KEYS = (
    "mechanism",
    "source_card_id",
    "target_card_id",
    "source_face_index",
    "target_face_index",
    "source_quote",
    "target_quote",
    "guide_quote",
    "required",
)


class HobEnrichmentRunError(ValueError):
    """Raised when the harness cannot trust its inputs or configuration."""


class BenchmarkError(HobEnrichmentRunError):
    """Raised when the reviewed benchmark cannot be validated against the frozen artifact."""


class SpendCeilingExceeded(RuntimeError):
    """Raised when the accumulated cost reaches the configured spending ceiling."""

    def __init__(self, *, spent: Decimal, ceiling: Decimal, subject: str) -> None:
        self.spent = spent
        self.ceiling = ceiling
        self.subject = subject
        super().__init__(
            f"accumulated spend {_usd_text(spent)} USD reached the ceiling "
            f"{_usd_text(ceiling)} USD while resolving {subject}."
        )


def _usd_text(value: Decimal) -> str:
    """Format one exact decimal cost with normalized trailing digits."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _repository_root() -> Path:
    """Return the repository root that holds the frozen card artifact."""
    return Path(__file__).resolve().parents[1]


def _sha256_bytes(payload: bytes) -> str:
    """Return the lowercase digest of one byte string."""
    return hashlib.sha256(payload).hexdigest()


def _normalized_text(value: str) -> str:
    """Collapse one text to single spaces for passage comparison."""
    return " ".join(value.split())


def _passage_match(left: str, right: str) -> bool:
    """Report whether two quotations cover the same passage."""
    first = _normalized_text(left)
    second = _normalized_text(right)
    if not first or not second:
        return False
    if first == second:
        return True
    if len(first) >= MINIMUM_OVERLAP_CHARS and first in second:
        return True
    return len(second) >= MINIMUM_OVERLAP_CHARS and second in first


def _quote_from(text: str) -> str | None:
    """Return one short nonblank substring copied exactly from supplied text."""
    stripped = text.lstrip()
    if not stripped.strip():
        return None
    return stripped[:MAX_QUOTE_CHARS].rstrip()


def _has_oracle_text(text: str | None) -> bool:
    """Report whether one optional Oracle text carries any nonblank text."""
    return text is not None and bool(text.strip())


def _card_has_source_text(card: CardInfo) -> bool:
    """Report whether one canonical card can carry Oracle evidence."""
    return _has_oracle_text(card.oracle_text) or any(
        _has_oracle_text(face.oracle_text) for face in card.faces
    )


def _eligible_cards(sources: EnrichmentSources) -> tuple[CardInfo, ...]:
    """Return the canonical cards one run may attempt, in ascending card order."""
    ordered = sorted(sources.cards, key=lambda card: card.grp_id)
    return tuple(card for card in ordered if _card_has_source_text(card))


def _card_text(card: CardInfo, face_index: int | None) -> str:
    """Return the exact Oracle text one participant evidence must come from."""
    if face_index is None:
        return card.oracle_text or ""
    return card.faces[face_index].oracle_text or ""


class _SpendGuard:
    """Bound the accumulated cost of every completion one run performs."""

    def __init__(self, *, complete: Completion, ceiling: Decimal) -> None:
        self._complete = complete
        self.ceiling = ceiling
        self.spent = Decimal("0")
        self.requests = 0
        self.unknown_cost_responses = 0

    def __call__(self, request: ExtractionRequest) -> OpenRouterResponse:
        """Submit one request only while its accumulated cost stays under the ceiling."""
        subject = request.prompt_id
        if self.spent >= self.ceiling:
            raise SpendCeilingExceeded(spent=self.spent, ceiling=self.ceiling, subject=subject)
        response = self._complete(request)
        self.requests += 1
        if response.cost_usd is None:
            self.unknown_cost_responses += 1
            print(
                f"spend_unknown request={subject} cost_usd=None"
                f" accumulated_usd={_usd_text(self.spent)} ceiling_usd={_usd_text(self.ceiling)}"
            )
        else:
            self.spent += Decimal(response.cost_usd)
            print(
                f"spend request={subject} cost_usd={response.cost_usd}"
                f" accumulated_usd={_usd_text(self.spent)} ceiling_usd={_usd_text(self.ceiling)}"
            )
            if self.spent > self.ceiling:
                raise SpendCeilingExceeded(spent=self.spent, ceiling=self.ceiling, subject=subject)
        return response


def _dry_run_roles(sources: EnrichmentSources) -> dict[int, tuple[str, ...]]:
    """Assign deterministic dry-run roles that construct at least one candidate pair."""
    roles: dict[int, tuple[str, ...]] = {}
    for index, card in enumerate(_eligible_cards(sources)):
        if index < len(DRY_RUN_ROLE_CYCLE):
            roles[card.grp_id] = (DRY_RUN_ROLE_CYCLE[index],)
        else:
            roles[card.grp_id] = (DRY_RUN_FILLER_ROLE,)
    return roles


def _dry_run_capability(
    *,
    prompt_card: Mapping[str, Any],
    role: str,
    face_index: int | None,
    face_name: str | None,
    quote: str,
) -> dict[str, Any]:
    """Build one schema-valid capability bound to exact prompt Oracle text."""
    return {
        "finding_id": f"dry-run-{role}-{prompt_card['card_id']}",
        "card_id": prompt_card["card_id"],
        "card_name": prompt_card["name"],
        "face_index": face_index,
        "face_name": face_name,
        "role": role,
        "quantity": None,
        "timing": None,
        "source_zone": None,
        "destination_zone": None,
        "prerequisites": [],
        "evidence": [
            {"card_id": prompt_card["card_id"], "face_index": face_index, "quote": quote}
        ],
        "review": {"status": "accepted", "reason": None},
    }


def _dry_run_card_content(
    prompt: Mapping[str, Any],
    *,
    roles: tuple[str, ...],
) -> str:
    """Answer one card capability request with quotes taken from that card's prompt."""
    prompt_card: Mapping[str, Any] = prompt["card"]
    faces: list[Mapping[str, Any]] = list(prompt_card["faces"])
    selected: Mapping[str, Any] | None = None
    face_index: int | None = None
    for index, face in enumerate(faces):
        if face["oracle_text"] is not None and face["oracle_text"].strip():
            selected = face
            face_index = index
            break
    if faces:
        source_text = "" if selected is None else selected["oracle_text"]
        face_name = None if selected is None else selected["name"]
    else:
        source_text = prompt_card["oracle_text"] or ""
        face_name = None
    quote = _quote_from(source_text)
    capabilities = (
        []
        if quote is None
        else [
            _dry_run_capability(
                prompt_card=prompt_card,
                role=role,
                face_index=face_index,
                face_name=face_name,
                quote=quote,
            )
            for role in roles
        ]
    )
    return json.dumps({"schema_version": 1, "capabilities": capabilities})


def _dry_run_guide_content(prompt: Mapping[str, Any]) -> str:
    """Answer one guide request with a finding quoting the frozen guide exactly."""
    guide: Mapping[str, Any] = prompt["guide"]
    quote = _quote_from(guide["text"])
    findings: list[dict[str, Any]] = []
    if quote is not None:
        card_ids = [card["card_id"] for card in prompt["cards"] if card["name"] in quote]
        findings.append(
            {
                "finding_id": "dry-run-mechanic",
                "category": "mechanic",
                "name": "Dry-run mechanic",
                "claim": quote,
                "card_ids": card_ids,
                "evidence": [{"guide_id": guide["guide_id"], "quote": quote}],
                "review": {"status": "accepted", "reason": None},
            }
        )
    return json.dumps({"schema_version": 1, "findings": findings})


def _dry_run_relationship_content(prompt: Mapping[str, Any]) -> str:
    """Answer one relationship request with a verdict quoting both participants exactly."""
    source: Mapping[str, Any] = prompt["source"]
    target: Mapping[str, Any] = prompt["target"]
    return json.dumps(
        {
            "schema_version": 1,
            "verdict": "accepted",
            "claim": (
                f"{source['card_name']} enables {target['card_name']} "
                f"through {prompt['mechanism']}."
            ),
            "reason": None,
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


class _DryRunCompletion:
    """Answer every pinned request with deterministic content taken from that request."""

    def __init__(self, *, sources: EnrichmentSources) -> None:
        self._roles = _dry_run_roles(sources)
        self.calls: list[str] = []

    def __call__(self, request: ExtractionRequest) -> OpenRouterResponse:
        """Answer one pinned request without any network access."""
        self.calls.append(request.prompt_id)
        return OpenRouterResponse(
            content=self._content(request),
            model=DRY_RUN_MODEL,
            provider=DRY_RUN_PROVIDER,
            input_tokens=DRY_RUN_INPUT_TOKENS,
            cached_input_tokens=DRY_RUN_CACHED_INPUT_TOKENS,
            output_tokens=DRY_RUN_OUTPUT_TOKENS,
            reasoning_tokens=DRY_RUN_REASONING_TOKENS,
            cost_usd=DRY_RUN_COST_USD,
        )

    def _content(self, request: ExtractionRequest) -> str:
        """Dispatch one request to the deterministic content of its contract."""
        prompt = json.loads(request.user_prompt)
        if request.prompt_id == GUIDE_EXTRACTION_PROMPT_ID:
            return _dry_run_guide_content(prompt)
        if request.prompt_id == CARD_CAPABILITY_EXTRACTION_PROMPT_ID:
            card_id = prompt["card"]["card_id"]
            return _dry_run_card_content(prompt, roles=self._roles[card_id])
        if request.prompt_id == RELATIONSHIP_VALIDATION_PROMPT_ID:
            return _dry_run_relationship_content(prompt)
        raise HobEnrichmentRunError(f"unexpected pinned request {request.prompt_id!r}.")


def _print_progress(progress: EnrichmentProgress) -> None:
    """Print one immutable progress event as a single run line."""
    accounting = progress.accounting
    projected = (
        "none"
        if accounting.projected_final_cost_usd is None
        else accounting.projected_final_cost_usd
    )
    print(
        f"progress phase={progress.phase.value}"
        f" guides={progress.guides_completed}/{progress.guides_total}"
        f" ({progress.guides_percent:.1f}%)"
        f" cards={progress.cards_completed}/{progress.cards_total}"
        f" ({progress.cards_percent:.1f}%)"
        f" relationships={progress.relationships_completed}/{progress.relationships_total}"
        f" ({progress.relationships_percent:.1f}%)"
        f" valid={progress.valid_count} uncertain={progress.uncertain_count}"
        f" rejected={progress.rejected_count}"
        f" input_tokens={accounting.input_tokens}"
        f" cached_input_tokens={accounting.cached_input_tokens}"
        f" output_tokens={accounting.output_tokens}"
        f" reasoning_tokens={accounting.reasoning_tokens}"
        f" running_cost_usd={accounting.running_cost_usd}"
        f" projected_final_cost_usd={projected}"
    )


def _load_card_artifact() -> tuple[SetCardData, bytes]:
    """Load and validate the frozen HOB canonical card artifact."""
    path = _repository_root() / CARD_ARTIFACT_RELATIVE_PATH
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise HobEnrichmentRunError(f"card artifact {path} could not be read.") from error
    try:
        data = SetCardData.from_gzip_bytes(payload, expected_set_code=SET_CODE)
    except SetCardDataError as error:
        raise HobEnrichmentRunError(f"card artifact {path} is not a valid {SET_CODE} artifact.") from error
    return data, payload


def _benchmark_guide_identity(benchmark: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Return the frozen guide identity one benchmark records, when it is usable."""
    if benchmark is None:
        return None
    guide = benchmark.get("guide")
    if not isinstance(guide, Mapping):
        return None
    return guide


def _load_guides(
    *,
    run_dir: Path,
    benchmark: Mapping[str, Any] | None,
) -> tuple[tuple[GuideSource, ...], dict[str, Any]]:
    """Load the frozen HOB guide, or run without guide sources when it is absent."""
    path = run_dir / GUIDE_FILE_NAME
    facts: dict[str, Any] = {"path": str(path), "present": False}
    if not path.is_file():
        print(f"warning=guide file {path} is missing; running without guide sources")
        return (), facts
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise HobEnrichmentRunError(f"guide file {path} is not readable UTF-8 text.") from error
    digest = _sha256_bytes(payload)
    facts.update({"present": True, "sha256": digest, "chars": len(text)})
    identity = _benchmark_guide_identity(benchmark)
    if identity is not None:
        guide = GuideSource(
            guide_id=identity["guide_id"],
            url=identity["url"],
            text=text,
            retrieved_at=identity["retrieved_at"],
        )
        facts["identity_source"] = "benchmark"
    else:
        retrieved_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        guide = GuideSource(
            guide_id="hob-guide",
            url=path.resolve().as_uri(),
            text=text,
            retrieved_at=retrieved_at,
        )
        facts["identity_source"] = "harness"
    facts["guide_id"] = guide.guide_id
    facts["text_sha256"] = guide.text_sha256
    return (guide,), facts


def _build_sources(
    *,
    data: SetCardData,
    guides: tuple[GuideSource, ...],
) -> EnrichmentSources:
    """Build the frozen source context one analysis runs against."""
    try:
        return EnrichmentSources(set_code=data.set_code, cards=data.cards, guides=guides)
    except ValueError as error:
        raise HobEnrichmentRunError("frozen HOB sources are inconsistent with the card artifact.") from error


def _limited_sources(
    sources: EnrichmentSources,
    *,
    limit: int | None,
) -> tuple[EnrichmentSources, dict[str, Any]]:
    """Cap the eligible cards one cheap smoke run processes."""
    eligible = _eligible_cards(sources)
    facts: dict[str, Any] = {"eligible": len(eligible), "limited_to": limit, "applied": False}
    if limit is None or limit >= len(eligible):
        return sources, facts
    kept = {card.grp_id for card in eligible[:limit]}
    cards = tuple(
        card for card in sources.cards if card.grp_id in kept or not _card_has_source_text(card)
    )
    limited = EnrichmentSources(set_code=sources.set_code, cards=cards, guides=sources.guides)
    facts["applied"] = True
    facts["eligible"] = len(_eligible_cards(limited))
    return limited, facts


def _validate_acquisition_config(sources: EnrichmentSources) -> None:
    """Prove the pinned acquisition configuration is accepted before any paid request."""
    request = _representative_request(sources)
    if request is None:
        raise HobEnrichmentRunError("no pinned request is available for the pinned configuration.")
    try:
        OpenRouterClient(
            model=MODEL,
            schema_name=request.response_schema_name,
            schema=request.response_schema(),
            reasoning_effort=REASONING_EFFORT,
            max_tokens=MAX_TOKENS,
        )
    except OpenRouterClientError as error:
        raise HobEnrichmentRunError(f"pinned model configuration is rejected ({error.code}).") from error
    print(
        f"model_config model={MODEL} reasoning_effort={REASONING_EFFORT}"
        f" max_tokens={MAX_TOKENS} accepted=true"
    )


def _representative_request(sources: EnrichmentSources) -> ExtractionRequest | None:
    """Return the first pinned request one run would submit."""
    if sources.guides:
        return build_guide_extraction_request(
            sources=sources,
            guide_id=sources.guides[0].guide_id,
        )
    eligible = _eligible_cards(sources)
    if not eligible:
        return None
    return build_card_capability_extraction_request(sources=sources, card_id=eligible[0].grp_id)


def _profile_directories(*, work_dir: Path, run_dir: Path) -> tuple[Path, ...]:
    """Return every set-profile directory this run could touch."""
    candidates = (
        Path(app_data_dir()) / "set-profiles",
        _repository_root() / "set-profiles",
        run_dir / "set-profiles",
        work_dir / "set-profiles",
    )
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def _profile_snapshot(directories: Sequence[Path]) -> dict[str, Any]:
    """Hash every file under the supplied set-profile directories."""
    entries: list[dict[str, Any]] = []
    for directory in directories:
        files: dict[str, str] = {}
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    try:
                        files[str(path.relative_to(directory))] = _sha256_bytes(path.read_bytes())
                    except OSError as error:
                        raise HobEnrichmentRunError(
                            f"set-profile file {path} could not be read."
                        ) from error
        digest = _sha256_bytes(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        entries.append(
            {
                "path": str(directory),
                "exists": directory.is_dir(),
                "file_count": len(files),
                "sha256": digest,
                "files": files,
            }
        )
    return {"directories": entries, "digest": _sha256_bytes(
        json.dumps([entry["sha256"] for entry in entries], separators=(",", ":")).encode("utf-8")
    )}


def _profile_comparison(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two set-profile snapshots without rewriting either."""
    changed: list[dict[str, Any]] = []
    for previous, current in zip(before["directories"], after["directories"]):
        if previous["sha256"] != current["sha256"] or previous["file_count"] != current["file_count"]:
            changed.append(
                {
                    "path": current["path"],
                    "before_sha256": previous["sha256"],
                    "after_sha256": current["sha256"],
                    "before_file_count": previous["file_count"],
                    "after_file_count": current["file_count"],
                }
            )
    return {
        "directories": [entry["path"] for entry in after["directories"]],
        "file_counts": {entry["path"]: entry["file_count"] for entry in after["directories"]},
        "before_sha256": before["digest"],
        "after_sha256": after["digest"],
        "changed": changed,
        "unchanged": not changed and before["digest"] == after["digest"],
    }


def _card_outcome_row(card_id: int, result: Any) -> dict[str, Any]:
    """Summarize one attempted canonical card and every reason it was rejected."""
    return {
        "card_id": card_id,
        "outcome": result.outcome.value,
        "capabilities": len(result.capabilities),
        "accepted": len(result.accepted_capabilities),
        "uncertain": len(result.uncertain_capabilities),
        "roles": sorted({capability.role.value for capability in result.capabilities}),
        "rejected": [
            {
                "finding_id": finding.finding_id,
                "summary": finding.summary,
                "reason": finding.reason,
            }
            for finding in result.rejected_capabilities
        ],
        "malformed_reason": result.malformed_reason,
    }


def _guide_rows(result: EnrichmentRunResult) -> list[dict[str, Any]]:
    """Summarize every retained guide result with its findings and reasons."""
    rows: list[dict[str, Any]] = []
    for guide_id, guide_result in zip(result.guide_ids, result.guide_results):
        rows.append(
            {
                "guide_id": guide_id,
                "outcome": guide_result.outcome.value,
                "accepted": len(guide_result.accepted_findings),
                "uncertain": len(guide_result.uncertain_findings),
                "claims": [
                    {
                        "finding_id": claim.finding_id,
                        "category": claim.category,
                        "name": claim.name,
                        "claim": claim.claim,
                        "status": claim.review.status.value,
                        "reason": claim.review.reason,
                        "quotes": [evidence.quote for evidence in claim.evidence],
                    }
                    for claim in guide_result.guide_claims
                ],
                "rejected": [
                    {
                        "finding_id": finding.finding_id,
                        "summary": finding.summary,
                        "reason": finding.reason,
                    }
                    for finding in guide_result.rejected_findings
                ],
                "malformed_reason": guide_result.malformed_reason,
            }
        )
    return rows


def _relationship_row(
    *,
    package: Any | None,
    entry: Any,
) -> dict[str, Any]:
    """Summarize one constructed candidate and the verdict it received."""
    relationship = entry.relationship
    rejected = entry.rejected
    if relationship is not None:
        verdict = relationship.review.status.value
        claim = relationship.claim
        reason = relationship.review.reason
        evidence = [item.to_json() for item in relationship.evidence]
    elif rejected is not None:
        verdict = FindingStatus.REJECTED.value
        claim = rejected.summary
        reason = rejected.reason
        evidence = []
    else:
        verdict = "malformed"
        claim = None
        reason = entry.malformed_reason
        evidence = []
    return {
        "subject_id": (
            relationship_subject_id(
                mechanism=package.mechanism,
                source=package.source,
                target=package.target,
            )
            if package is not None
            else None
        ),
        "mechanism": None if package is None else package.mechanism,
        "participants": (
            None
            if package is None
            else {
                "source": package.source.to_json(),
                "target": package.target.to_json(),
            }
        ),
        "outcome": entry.outcome.value,
        "verdict": verdict,
        "claim": claim,
        "reason": reason,
        "evidence": evidence,
    }


def _relationship_rows(result: EnrichmentRunResult) -> list[dict[str, Any]]:
    """Summarize every relationship result against its constructed candidate."""
    packages = () if result.candidate_packages is None else result.candidate_packages.packages
    rows: list[dict[str, Any]] = []
    for index, entry in enumerate(result.relationship_results):
        package = packages[index] if index < len(packages) else None
        rows.append(_relationship_row(package=package, entry=entry))
    return rows


def _work_kind_rows(
    *,
    result: EnrichmentRunResult,
    sources: EnrichmentSources,
    model_config: WorkModelConfig,
) -> list[dict[str, Any]]:
    """Summarize the pinned request identities of every resolved work item."""
    cards = {card.grp_id: card for card in sources.cards}
    guides = {guide.guide_id: guide for guide in sources.guides}
    items: list[tuple[WorkKind, str, ExtractionRequest, str]] = []
    for guide_id in result.guide_ids:
        items.append(
            (
                WorkKind.GUIDE,
                guide_id,
                build_guide_extraction_request(sources=sources, guide_id=guide_id),
                guides[guide_id].text_sha256,
            )
        )
    for card_id in result.card_ids:
        items.append(
            (
                WorkKind.CARD_CAPABILITY,
                str(card_id),
                build_card_capability_extraction_request(sources=sources, card_id=card_id),
                card_source_sha256(cards[card_id]),
            )
        )
    if result.candidate_packages is not None:
        for package in result.candidate_packages.packages:
            items.append(
                (
                    WorkKind.RELATIONSHIP,
                    relationship_subject_id(
                        mechanism=package.mechanism,
                        source=package.source,
                        target=package.target,
                    ),
                    build_relationship_validation_request(
                        sources=sources,
                        mechanism=package.mechanism,
                        source=package.source,
                        target=package.target,
                    ),
                    relationship_source_sha256(
                        mechanism=package.mechanism,
                        source=package.source,
                        target=package.target,
                    ),
                )
            )
    rows: list[dict[str, Any]] = []
    for kind in WorkKind:
        identities = [
            build_work_identity(
                work_kind=kind,
                subject_id=subject_id,
                input_sha256=input_sha256,
                request=request,
                model_config=model_config,
            )
            for work_kind, subject_id, request, input_sha256 in items
            if work_kind is kind
        ]
        rows.append(
            {
                "work_kind": kind.value,
                "items": len(identities),
                "contract_version": (
                    identities[0].contract_version if identities else None
                ),
                "prompt_id": identities[0].prompt_id if identities else None,
                "response_schema_id": (
                    identities[0].response_schema_id if identities else None
                ),
                "response_schema_name": (
                    identities[0].response_schema_name if identities else None
                ),
                "prompt_sha256": sorted({identity.prompt_sha256 for identity in identities}),
                "response_schema_sha256": sorted(
                    {identity.response_schema_sha256 for identity in identities}
                ),
                "identity_sha256": sorted(identity.content_sha256 for identity in identities),
            }
        )
    return rows


def _benchmark_text(value: Any, field_name: str, *, failures: list[str]) -> str | None:
    """Validate one required benchmark string."""
    if not isinstance(value, str) or not value.strip():
        failures.append(f"{field_name} must be a nonblank string.")
        return None
    return value


def _benchmark_bool(value: Any, field_name: str, *, failures: list[str]) -> bool:
    """Validate one required benchmark boolean."""
    if type(value) is not bool:
        failures.append(f"{field_name} must be a boolean.")
        return False
    return value


def _benchmark_card_id(value: Any, field_name: str, *, failures: list[str]) -> int | None:
    """Validate one required benchmark card identifier."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        failures.append(f"{field_name} must be a positive card id.")
        return None
    return value


def _benchmark_face_index(
    value: Any,
    field_name: str,
    *,
    card: CardInfo | None,
    failures: list[str],
) -> int | None:
    """Validate one optional benchmark face index against its card."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        failures.append(f"{field_name} must be null or a non-negative face index.")
        return None
    if card is not None and value >= len(card.faces):
        failures.append(f"{field_name} {value} is not a face of card {card.grp_id}.")
        return None
    return value


def _load_benchmark(path: Path) -> Mapping[str, Any] | None:
    """Read the reviewed benchmark object when the file is present."""
    if not path.is_file():
        return None
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise BenchmarkError(f"benchmark {path} could not be read.") from error
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise BenchmarkError(f"benchmark {path} is not valid JSON.") from error
    if not isinstance(value, Mapping):
        raise BenchmarkError(f"benchmark {path} must be a JSON object.")
    return value


def _validate_benchmark(
    *,
    benchmark: Mapping[str, Any],
    path: Path,
    sources: EnrichmentSources,
    card_payload: bytes,
    guide_text: str | None,
    guide_facts: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the reviewed benchmark against the frozen artifact before any paid work."""
    failures: list[str] = []

    def fail(message: str) -> None:
        failures.append(message)

    if benchmark.get("schema_version") != _BENCHMARK_SCHEMA_VERSION:
        fail(f"schema_version must be {_BENCHMARK_SCHEMA_VERSION}.")
    if benchmark.get("set_code") != sources.set_code:
        fail(f"set_code must be {sources.set_code!r}.")
    if benchmark.get("card_source_path") != str(CARD_ARTIFACT_RELATIVE_PATH):
        fail(f"card_source_path must be {str(CARD_ARTIFACT_RELATIVE_PATH)!r}.")
    if benchmark.get("card_source_sha256") != _sha256_bytes(card_payload):
        fail("card_source_sha256 does not match the frozen card artifact.")
    expected_set_source = set_source_sha256(sources)
    if benchmark.get("set_source_sha256") != expected_set_source:
        fail("set_source_sha256 does not match the frozen source projection.")
    cards = {card.grp_id: card for card in sources.cards}
    eligible = _eligible_cards(sources)
    eligible_ids = [card.grp_id for card in eligible]
    ineligible_ids = sorted(
        card.grp_id for card in sources.cards if card.grp_id not in set(eligible_ids)
    )
    if benchmark.get("card_count") != len(sources.cards):
        fail(f"card_count must be {len(sources.cards)}.")
    if benchmark.get("eligible_card_count") != len(eligible_ids):
        fail(f"eligible_card_count must be {len(eligible_ids)}.")
    if benchmark.get("ineligible_card_count") != len(ineligible_ids):
        fail(f"ineligible_card_count must be {len(ineligible_ids)}.")
    if benchmark.get("eligible_card_ids") != eligible_ids:
        fail("eligible_card_ids do not match the frozen artifact.")
    if benchmark.get("ineligible_card_ids") != ineligible_ids:
        fail("ineligible_card_ids do not match the frozen artifact.")

    guide_record = benchmark.get("guide")
    mechanics: list[Any] = []
    relationships: list[Any] = []
    if not isinstance(guide_record, Mapping):
        fail("guide must be an object with the frozen guide identity.")
    else:
        for key in _BENCHMARK_GUIDE_KEYS:
            if key not in guide_record:
                fail(f"guide.{key} is required.")
        _benchmark_text(guide_record.get("guide_id"), "guide.guide_id", failures=failures)
        _benchmark_text(guide_record.get("url"), "guide.url", failures=failures)
        _benchmark_text(guide_record.get("retrieved_at"), "guide.retrieved_at", failures=failures)
        if guide_text is None:
            fail("guide.txt is required to validate the benchmark guide quotations.")
        else:
            if guide_record.get("sha256") != guide_facts.get("sha256"):
                fail("guide.sha256 does not match the frozen guide text.")
            if guide_record.get("chars") != len(guide_text):
                fail("guide.chars does not match the frozen guide text.")

    value = benchmark.get("mechanics")
    if not isinstance(value, list):
        fail("mechanics must be an array.")
    else:
        mechanics = value
        for index, entry in enumerate(mechanics):
            if not isinstance(entry, Mapping):
                fail(f"mechanics[{index}] must be an object.")
                continue
            for key in _MECHANIC_KEYS:
                if key not in entry:
                    fail(f"mechanics[{index}].{key} is required.")
            _benchmark_text(entry.get("name"), f"mechanics[{index}].name", failures=failures)
            quote = _benchmark_text(
                entry.get("guide_quote"), f"mechanics[{index}].guide_quote", failures=failures
            )
            _benchmark_bool(entry.get("required"), f"mechanics[{index}].required", failures=failures)
            if quote is not None and guide_text is not None and quote not in guide_text:
                fail(f"mechanics[{index}].guide_quote is not an exact guide substring.")
            oracle_card_id = _benchmark_card_id(
                entry.get("oracle_card_id"),
                f"mechanics[{index}].oracle_card_id",
                failures=failures,
            )
            if oracle_card_id is not None and oracle_card_id not in eligible_ids:
                fail(f"mechanics[{index}].oracle_card_id is not an eligible frozen card.")
            oracle_card = cards.get(oracle_card_id) if oracle_card_id is not None else None
            oracle_face = _benchmark_face_index(
                entry.get("oracle_face_index"),
                f"mechanics[{index}].oracle_face_index",
                card=oracle_card,
                failures=failures,
            )
            oracle_quote = _benchmark_text(
                entry.get("oracle_quote"),
                f"mechanics[{index}].oracle_quote",
                failures=failures,
            )
            if oracle_quote is not None and oracle_card is not None:
                if oracle_quote not in _card_text(oracle_card, oracle_face):
                    fail(
                        f"mechanics[{index}].oracle_quote is not an exact Oracle substring."
                    )

    value = benchmark.get("relationships")
    if not isinstance(value, list):
        fail("relationships must be an array.")
    else:
        relationships = value
        mechanisms = {link.mechanism for link in ROLE_COMPATIBILITY_RULES}
        for index, entry in enumerate(relationships):
            if not isinstance(entry, Mapping):
                fail(f"relationships[{index}] must be an object.")
                continue
            for key in _RELATIONSHIP_EXPECTATION_KEYS:
                if key not in entry:
                    fail(f"relationships[{index}].{key} is required.")
            mechanism = _benchmark_text(
                entry.get("mechanism"), f"relationships[{index}].mechanism", failures=failures
            )
            if mechanism is not None and mechanism not in mechanisms:
                fail(f"relationships[{index}].mechanism {mechanism!r} is not a declared rule.")
            source_card_id = _benchmark_card_id(
                entry.get("source_card_id"),
                f"relationships[{index}].source_card_id",
                failures=failures,
            )
            target_card_id = _benchmark_card_id(
                entry.get("target_card_id"),
                f"relationships[{index}].target_card_id",
                failures=failures,
            )
            if source_card_id is not None and source_card_id not in cards:
                fail(f"relationships[{index}].source_card_id is not a frozen card.")
            if target_card_id is not None and target_card_id not in cards:
                fail(f"relationships[{index}].target_card_id is not a frozen card.")
            if source_card_id is not None and source_card_id == target_card_id:
                fail(f"relationships[{index}] must reference two different cards.")
            source_card = cards.get(source_card_id) if source_card_id is not None else None
            target_card = cards.get(target_card_id) if target_card_id is not None else None
            source_face = _benchmark_face_index(
                entry.get("source_face_index"),
                f"relationships[{index}].source_face_index",
                card=source_card,
                failures=failures,
            )
            target_face = _benchmark_face_index(
                entry.get("target_face_index"),
                f"relationships[{index}].target_face_index",
                card=target_card,
                failures=failures,
            )
            _benchmark_bool(
                entry.get("required"), f"relationships[{index}].required", failures=failures
            )
            for side, card, face, quote_key in (
                ("source", source_card, source_face, "source_quote"),
                ("target", target_card, target_face, "target_quote"),
            ):
                quote = _benchmark_text(
                    entry.get(quote_key),
                    f"relationships[{index}].{quote_key}",
                    failures=failures,
                )
                if quote is not None and card is not None:
                    text = _card_text(card, face)
                    if quote not in text:
                        fail(
                            f"relationships[{index}].{quote_key} is not an exact "
                            f"{side} Oracle substring."
                        )
            guide_quote = _benchmark_text(
                entry.get("guide_quote"),
                f"relationships[{index}].guide_quote",
                failures=failures,
            )
            if guide_quote is not None and guide_text is not None and guide_quote not in guide_text:
                fail(f"relationships[{index}].guide_quote is not an exact guide substring.")

    if failures:
        raise BenchmarkError(
            f"benchmark {path} cannot be trusted:\n  - " + "\n  - ".join(failures)
        )
    return {
        "path": str(path),
        "trusted": True,
        "schema_version": _BENCHMARK_SCHEMA_VERSION,
        "set_source_sha256": expected_set_source,
        "card_source_sha256": benchmark["card_source_sha256"],
        "card_count": len(sources.cards),
        "eligible_card_count": len(eligible_ids),
        "ineligible_card_ids": ineligible_ids,
        "guide_sha256": None if guide_text is None else guide_facts.get("sha256"),
        "mechanics": len(mechanics),
        "relationships": len(relationships),
        "required_mechanics": sum(
            1 for entry in mechanics if isinstance(entry, Mapping) and entry.get("required") is True
        ),
        "required_relationships": sum(
            1
            for entry in relationships
            if isinstance(entry, Mapping) and entry.get("required") is True
        ),
    }


def _oracle_evidence_matches(
    *,
    entry: Mapping[str, Any],
    capabilities: Sequence[Any],
) -> list[dict[str, Any]]:
    """Report which retained capabilities quote one mechanic's reviewed Oracle evidence."""
    card_id = int(entry["oracle_card_id"])
    face_index = entry["oracle_face_index"]
    quote = str(entry["oracle_quote"])
    matches: list[dict[str, Any]] = []
    for capability in capabilities:
        if capability.card_id != card_id:
            continue
        if face_index is not None and capability.face_index != face_index:
            continue
        if not any(
            _passage_match(quote, evidence.quote) for evidence in capability.evidence
        ):
            continue
        matches.append(
            {
                "rule": "card-capability-quote",
                "card_id": capability.card_id,
                "face_index": capability.face_index,
                "finding_id": capability.finding_id,
                "role": capability.role.value,
                "status": capability.review.status.value,
            }
        )
    return matches


def _mechanic_expectation(
    *,
    entry: Mapping[str, Any],
    result: EnrichmentRunResult,
    relationship_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Decide whether one reviewed mechanic appears in the run's retained evidence."""
    name = str(entry["name"])
    key = name.casefold()
    quote = str(entry["guide_quote"])
    matches: list[dict[str, Any]] = []
    related: list[dict[str, Any]] = []
    claims = [
        claim
        for guide_result in result.guide_results
        for claim in guide_result.guide_claims
    ]
    for claim in claims:
        quote_match = any(_passage_match(quote, item.quote) for item in claim.evidence)
        if claim.name.casefold() == key:
            matches.append(
                {
                    "rule": "guide-claim-name",
                    "finding_id": claim.finding_id,
                    "status": claim.review.status.value,
                    "quote_match": quote_match,
                }
            )
        elif quote_match:
            matches.append(
                {
                    "rule": "guide-quote-overlap",
                    "finding_id": claim.finding_id,
                    "status": claim.review.status.value,
                    "quote_match": True,
                }
            )
        elif key in claim.claim.casefold() or key in claim.name.casefold():
            related.append(
                {
                    "kind": "guide-claim",
                    "finding_id": claim.finding_id,
                    "status": claim.review.status.value,
                    "reason": claim.review.reason,
                }
            )
    for row in relationship_rows:
        mechanism = row["mechanism"]
        if mechanism is None or row["verdict"] == "malformed":
            continue
        lowered = mechanism.casefold()
        if lowered == key:
            matches.append(
                {"rule": "relationship-mechanism", "finding_id": row["subject_id"], "status": row["verdict"]}
            )
        elif len(key) >= 4 and key in lowered:
            matches.append(
                {
                    "rule": "relationship-mechanism-substring",
                    "finding_id": row["subject_id"],
                    "status": row["verdict"],
                }
            )
    capabilities = [
        capability
        for card_result in result.card_results
        for capability in card_result.capabilities
    ]
    card_matches = _oracle_evidence_matches(entry=entry, capabilities=capabilities)
    matches.extend(card_matches)
    rejections = [
        {
            "kind": "guide-rejection",
            "finding_id": finding.finding_id,
            "reason": finding.reason,
        }
        for guide_result in result.guide_results
        for finding in guide_result.rejected_findings
        if key in finding.summary.casefold()
    ]
    omissions = [] if result.candidate_packages is None else list(result.candidate_packages.omissions)
    omission = next(
        (
            {"mechanism": item.mechanism, "omitted_pairs": item.omitted_pairs, "reason": item.reason}
            for item in omissions
            if key in item.mechanism.casefold() or item.mechanism.casefold() in key
        ),
        None,
    )
    accepted = [item for item in matches if item["status"] == FindingStatus.ACCEPTED.value]
    if matches:
        classification = "matched"
        detail = (
            f"matched through {matches[0]['rule']} ({matches[0]['status']})"
            + ("" if accepted else " without an accepted match")
        )
    elif rejections:
        classification = "rejected"
        detail = "the run recorded a rejected diagnostic for this mechanic"
    elif related:
        classification = "uncertain"
        detail = "the run retained an uncertain claim naming this mechanic without matching quotations"
    elif omission is not None:
        classification = "omission"
        detail = "candidate construction omitted compatible pairs for this mechanic"
    else:
        classification = "absent"
        detail = "the run retained no claim or relationship for this mechanic"
    return {
        "kind": "mechanic",
        "expectation": name,
        "required": bool(entry["required"]),
        "matched": bool(matches),
        "classification": classification,
        "detail": detail,
        "matches": matches,
        "card_evidence": card_matches,
        "related": related,
        "rejections": rejections,
        "omission": omission,
    }


def _quotes_present(
    expected: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> bool:
    """Report whether every expected quotation appears in the retained evidence."""
    observed = [_normalized_text(str(item["quote"])) for item in evidence]
    for key in keys:
        wanted = _normalized_text(str(expected[key]))
        if not any(wanted in item or _passage_match(wanted, item) for item in observed):
            return False
    return True


def _relationship_expectation(
    *,
    entry: Mapping[str, Any],
    result: EnrichmentRunResult,
) -> dict[str, Any]:
    """Decide whether one reviewed relationship appears in the run's retained evidence."""
    mechanism = str(entry["mechanism"])
    source_card_id = int(entry["source_card_id"])
    target_card_id = int(entry["target_card_id"])
    source_face = entry["source_face_index"]
    target_face = entry["target_face_index"]
    packages = () if result.candidate_packages is None else result.candidate_packages.packages
    constructed: list[dict[str, Any]] = []
    for index, package in enumerate(packages):
        if package.mechanism != mechanism:
            continue
        if package.source.card_id != source_card_id or package.target.card_id != target_card_id:
            continue
        if source_face is not None and package.source.face_index != source_face:
            continue
        if target_face is not None and package.target.face_index != target_face:
            continue
        entry_result = (
            result.relationship_results[index]
            if index < len(result.relationship_results)
            else None
        )
        constructed.append(
            _relationship_row(package=package, entry=entry_result)
            if entry_result is not None
            else {
                "subject_id": relationship_subject_id(
                    mechanism=package.mechanism,
                    source=package.source,
                    target=package.target,
                ),
                "mechanism": package.mechanism,
                "participants": {
                    "source": package.source.to_json(),
                    "target": package.target.to_json(),
                },
                "outcome": "unresolved",
                "verdict": "unresolved",
                "claim": None,
                "reason": None,
                "evidence": [],
            }
        )
    accepted = [row for row in constructed if row["verdict"] == FindingStatus.ACCEPTED.value]
    accepted_row = accepted[0] if accepted else None
    quote_match = (
        None
        if accepted_row is None
        else _quotes_present(entry, accepted_row["evidence"], ("source_quote", "target_quote"))
    )
    omissions = () if result.candidate_packages is None else result.candidate_packages.omissions
    omission = next(
        (
            {"mechanism": item.mechanism, "omitted_pairs": item.omitted_pairs, "reason": item.reason}
            for item in omissions
            if item.mechanism == mechanism
        ),
        None,
    )
    capabilities = [
        capability
        for card_result in result.card_results
        for capability in card_result.capabilities
        if capability.card_id in (source_card_id, target_card_id)
    ]
    if accepted_row is not None:
        classification = "matched"
        detail = f"matched accepted relationship {accepted_row['subject_id']}"
    elif any(row["verdict"] == FindingStatus.UNCERTAIN.value for row in constructed):
        classification = "uncertain"
        row = next(row for row in constructed if row["verdict"] == FindingStatus.UNCERTAIN.value)
        detail = f"uncertain verdict for {row['subject_id']}: {row['reason']}"
    elif any(row["verdict"] == FindingStatus.REJECTED.value for row in constructed):
        classification = "rejected"
        row = next(row for row in constructed if row["verdict"] == FindingStatus.REJECTED.value)
        detail = f"rejected verdict for {row['subject_id']}: {row['reason']}"
    elif any(row["verdict"] == "malformed" for row in constructed):
        row = next(row for row in constructed if row["verdict"] == "malformed")
        classification = "malformed"
        detail = f"malformed verdict for {row['subject_id']}: {row['reason']}"
    elif omission is not None:
        classification = "omission"
        detail = f"candidate construction omitted {omission['omitted_pairs']} pairs ({omission['reason']})"
    else:
        classification = "absent"
        detail = (
            "no constructed candidate pair for this mechanism and these cards "
            f"(participant capabilities recorded: {len(capabilities)})"
        )
    return {
        "kind": "relationship",
        "expectation": (
            f"{mechanism}:{source_card_id}:{source_face}:{target_card_id}:{target_face}"
        ),
        "required": bool(entry["required"]),
        "matched": accepted_row is not None,
        "classification": classification,
        "detail": detail,
        "evidence_quote_match": quote_match,
        "constructed": constructed,
        "omission": omission,
        "participant_capabilities": [
            {
                "finding_id": capability.finding_id,
                "card_id": capability.card_id,
                "face_index": capability.face_index,
                "role": capability.role.value,
            }
            for capability in capabilities
        ],
    }


def _benchmark_comparison(
    *,
    benchmark: Mapping[str, Any],
    validation: Mapping[str, Any],
    result: EnrichmentRunResult,
    limit_facts: Mapping[str, Any],
) -> dict[str, Any]:
    relationship_rows = _relationship_rows(result)
    mechanics = [
        _mechanic_expectation(
            entry=entry,
            result=result,
            relationship_rows=relationship_rows,
        )
        for entry in benchmark["mechanics"]
    ]
    relationships = [
        _relationship_expectation(entry=entry, result=result)
        for entry in benchmark["relationships"]
    ]
    source_identity_match = result.set_source_sha256 == benchmark["set_source_sha256"]
    required_unmatched = [
        item["expectation"]
        for item in (*mechanics, *relationships)
        if item["required"] and not item["matched"]
    ]
    return {
        "present": True,
        "trusted": True,
        "path": validation["path"],
        "validation": dict(validation),
        "run_set_source_sha256": result.set_source_sha256,
        "reviewed_set_source_sha256": benchmark["set_source_sha256"],
        "source_identity_match": source_identity_match,
        "limited_run": bool(limit_facts["applied"]),
        "matching_rules": (
            "casefolded mechanic-name equality, overlapping exact quotation "
            f"(>= {MINIMUM_OVERLAP_CHARS} characters) in a retained guide claim or in a retained "
            "card capability for the expected Oracle card and face, or an accepted/uncertain "
            "relationship with the reviewed mechanism; relationships match on mechanism, both "
            "card ids and any pinned participant face index, and their exact Oracle quotations "
            "are re-checked"
        ),
        "mechanics": mechanics,
        "relationships": relationships,
        "required_unmatched": required_unmatched,
    }


def _absent_benchmark(path: Path) -> dict[str, Any]:
    """Record that no reviewed benchmark was available for comparison."""
    return {
        "present": False,
        "path": str(path),
        "note": (
            "no reviewed benchmark file is present; observed results are recorded without "
            "comparison and no acceptance failure is derived from benchmark expectations"
        ),
    }


def _build_report(
    *,
    args: argparse.Namespace,
    result: EnrichmentRunResult,
    sources: EnrichmentSources,
    full_sources: EnrichmentSources,
    card_facts: Mapping[str, Any],
    guide_facts: Mapping[str, Any],
    limit_facts: Mapping[str, Any],
    model_config: WorkModelConfig,
    guard: _SpendGuard,
    ceiling: Decimal,
    profiles: Mapping[str, Any],
    benchmark: Mapping[str, Any] | None,
    benchmark_validation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the complete report object for one finished analysis."""
    card_rows = [
        _card_outcome_row(card_id, card_result)
        for card_id, card_result in zip(result.card_ids, result.card_results)
    ]
    relationship_rows = _relationship_rows(result)
    accounting = result.progress.accounting
    candidate_facts: dict[str, Any] | None = None
    if result.candidate_packages is not None:
        packages = result.candidate_packages
        candidate_facts = {
            "outcome": packages.outcome.value,
            "candidate_pairs": packages.candidate_pairs,
            "evaluated_pairs": packages.evaluated_pairs,
            "malformed_extractions": packages.malformed_extractions,
            "packages": len(packages.packages),
            "run_ids": list(packages.run_ids),
            "omissions": [
                {
                    "mechanism": item.mechanism,
                    "omitted_pairs": item.omitted_pairs,
                    "reason": item.reason,
                }
                for item in packages.omissions
            ],
        }
    report: dict[str, Any] = {
        "run": {
            "run_id": result.run_id,
            "mode": "dry-run" if args.dry_run else "live",
            "dry_run": bool(args.dry_run),
            "work_dir": str(args.work_dir),
            "run_dir": str(args.run_dir),
            "ceiling_usd": _usd_text(ceiling),
            "spent_usd": _usd_text(guard.spent),
            "requests": guard.requests,
            "unknown_cost_responses": guard.unknown_cost_responses,
        },
        "outcome": result.outcome.value,
        "complete": result.complete,
        "review": {
            "state": result.review.state,
            "reviewer_id": result.review.reviewer_id,
            "reviewed_at": result.review.reviewed_at,
        },
        "sources": {
            "set_code": result.set_code,
            "set_source_sha256": result.set_source_sha256,
            "full_set_source_sha256": set_source_sha256(full_sources),
            "card_artifact": str(CARD_ARTIFACT_RELATIVE_PATH),
            "card_artifact_sha256": card_facts["sha256"],
            "card_count": card_facts["card_count"],
            "eligible_card_count": limit_facts["eligible"],
            "attempted_card_count": len(result.card_ids),
            "ineligible_card_ids": list(result.ineligible_card_ids),
            "limited_to": limit_facts["limited_to"],
            "limit_applied": limit_facts["applied"],
        },
        "guide": dict(guide_facts),
        "model": {
            **model_config.to_json(),
            "live_model_config": {
                "model": MODEL,
                "reasoning_effort": REASONING_EFFORT,
                "max_tokens": MAX_TOKENS,
            },
        },
        "work": {
            "model_config": model_config.to_json(),
            "kinds": _work_kind_rows(
                result=result,
                sources=sources,
                model_config=model_config,
            ),
        },
        "progress": {
            "final_phase": result.progress.phase.value,
            "guides": {
                "completed": result.progress.guides_completed,
                "total": result.progress.guides_total,
                "percent": result.progress.guides_percent,
            },
            "cards": {
                "completed": result.progress.cards_completed,
                "total": result.progress.cards_total,
                "percent": result.progress.cards_percent,
            },
            "relationships": {
                "completed": result.progress.relationships_completed,
                "total": result.progress.relationships_total,
                "percent": result.progress.relationships_percent,
            },
            "valid_count": result.progress.valid_count,
            "uncertain_count": result.progress.uncertain_count,
            "rejected_count": result.progress.rejected_count,
        },
        "accounting": {
            "executed_work": accounting.executed_work,
            "reused_work": accounting.reused_work,
            "work_without_cost": accounting.work_without_cost,
            "input_tokens": accounting.input_tokens,
            "cached_input_tokens": accounting.cached_input_tokens,
            "output_tokens": accounting.output_tokens,
            "reasoning_tokens": accounting.reasoning_tokens,
            "running_cost_usd": accounting.running_cost_usd,
            "projected_final_cost_usd": accounting.projected_final_cost_usd,
        },
        "guides": {"items": _guide_rows(result)},
        "cards": {
            "items": card_rows,
            "outcomes": _reason_counts(row["outcome"] for row in card_rows),
            "rejection_reasons": _reason_counts(
                finding["reason"] for row in card_rows for finding in row["rejected"]
            ),
            "malformed_reasons": _reason_counts(
                row["malformed_reason"] for row in card_rows if row["malformed_reason"]
            ),
        },
        "candidates": candidate_facts,
        "relationships": {
            "items": relationship_rows,
            "verdicts": _reason_counts(row["verdict"] for row in relationship_rows),
            "rejection_reasons": _reason_counts(
                row["reason"]
                for row in relationship_rows
                if row["verdict"] in {"rejected", "malformed"} and row["reason"]
            ),
            "accepted": [
                row for row in relationship_rows if row["verdict"] == FindingStatus.ACCEPTED.value
            ],
            "uncertain": [
                row for row in relationship_rows if row["verdict"] == FindingStatus.UNCERTAIN.value
            ],
            "rejected": [
                row
                for row in relationship_rows
                if row["verdict"] in {FindingStatus.REJECTED.value, "malformed"}
            ],
        },
        "profiles": dict(profiles),
        "benchmark": (
            _absent_benchmark(Path(args.benchmark))
            if benchmark is None
            else _benchmark_comparison(
                benchmark=benchmark,
                validation=benchmark_validation,
                result=result,
                limit_facts=limit_facts,
            )
        ),
    }
    report["acceptance"] = _acceptance(
        report=report,
        benchmark=benchmark,
        result=result,
    )
    return report


def _reason_counts(values: Any) -> dict[str, int]:
    """Count occurrences of each reported reason or outcome."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _acceptance(
    *,
    report: Mapping[str, Any],
    benchmark: Mapping[str, Any] | None,
    result: EnrichmentRunResult,
) -> dict[str, Any]:
    """Decide whether this run satisfies the acceptance rules of the analysis."""
    failures: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        if not passed:
            failures.append({"kind": name, "detail": detail})

    check(
        "outcome",
        result.outcome is EnrichmentOutcome.COMPLETE,
        f"run outcome is {result.outcome.value}",
    )
    check(
        "review",
        result.review.state == "pending",
        f"review state is {result.review.state}",
    )
    check(
        "profiles",
        bool(report["profiles"]["unchanged"]),
        f"set profiles changed: {report['profiles']['changed']}",
    )
    comparison = report["benchmark"]
    if benchmark is None:
        return {
            "checks": ["outcome", "review", "profiles"],
            "failures": failures,
            "passed": not failures,
            "note": comparison["note"],
        }
    check(
        "source-identity",
        bool(comparison["source_identity_match"]),
        "the run did not analyse the reviewed full source set "
        f"(limited_to={report['sources']['limited_to']})",
    )
    for item in (*comparison["mechanics"], *comparison["relationships"]):
        if item["required"] and not item["matched"]:
            failures.append(
                {
                    "kind": f"required-{item['kind']}",
                    "expectation": item["expectation"],
                    "classification": item["classification"],
                    "detail": item["detail"],
                }
            )
    return {
        "checks": [
            "outcome",
            "review",
            "profiles",
            "source-identity",
            "required-mechanics",
            "required-relationships",
        ],
        "failures": failures,
        "passed": not failures,
    }


def _write_report(report: Mapping[str, Any], *, run_dir: Path) -> tuple[Path, Path]:
    """Write the run report as JSON and Markdown inside the run directory."""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise HobEnrichmentRunError(f"run directory {run_dir} could not be created.") from error
    json_path = run_dir / REPORT_JSON_NAME
    md_path = run_dir / REPORT_MD_NAME
    try:
        json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        md_path.write_text(_render_markdown(report), encoding="utf-8")
    except OSError as error:
        raise HobEnrichmentRunError(f"report could not be written under {run_dir}.") from error
    return json_path, md_path


def _render_markdown(report: Mapping[str, Any]) -> str:
    """Render the report object as a reviewable Markdown document."""
    run = report["run"]
    lines: list[str] = [
        "# HOB set-enrichment run report",
        "",
        f"- run id: `{run['run_id']}`",
        f"- mode: {run['mode']} (dry run: {run['dry_run']})",
        f"- outcome: {report['outcome']} (complete: {report['complete']})",
        f"- review state: {report['review']['state']}",
        f"- work directory: `{run['work_dir']}`",
        f"- run directory: `{run['run_dir']}`",
        f"- spending ceiling: {run['ceiling_usd']} USD (spent {run['spent_usd']} USD over "
        f"{run['requests']} requests, {run['unknown_cost_responses']} without cost)",
        "",
        "## Sources",
        "",
        f"- set code: {report['sources']['set_code']}",
        f"- set source sha256: `{report['sources']['set_source_sha256']}`",
        f"- full set source sha256: `{report['sources']['full_set_source_sha256']}`",
        f"- card artifact: `{report['sources']['card_artifact']}` "
        f"(sha256 `{report['sources']['card_artifact_sha256']}`, "
        f"{report['sources']['card_count']} cards)",
        f"- eligible cards analysed: {len(report['cards']['items'])} "
        f"of {report['sources']['eligible_card_count']}",
        f"- ineligible card ids: {report['sources']['ineligible_card_ids']}",
        f"- limited run: {report['sources']['limit_applied']} "
        f"(limited_to={report['sources']['limited_to']})",
        f"- guide: {report['guide']}",
        "",
        "## Model and pinned work identities",
        "",
        f"- configured model configuration: {report['model']}",
    ]
    for row in report["work"]["kinds"]:
        lines.append(
            f"- {row['work_kind']}: {row['items']} items, prompt `{row['prompt_id']}`, "
            f"schema `{row['response_schema_id']}` (`{row['response_schema_name']}`), "
            f"contract {row['contract_version']}, "
            f"distinct prompt sha256: {len(row['prompt_sha256'])}, "
            f"distinct schema sha256: {len(row['response_schema_sha256'])} "
            "(full digests in report.json)"
        )
    lines.extend(
        [
            "",
            "## Progress and accounting",
            "",
            f"- final phase: {report['progress']['final_phase']}",
            f"- guides: {report['progress']['guides']['completed']}/"
            f"{report['progress']['guides']['total']} "
            f"({report['progress']['guides']['percent']}%)",
            f"- cards: {report['progress']['cards']['completed']}/"
            f"{report['progress']['cards']['total']} "
            f"({report['progress']['cards']['percent']}%)",
            f"- relationships: {report['progress']['relationships']['completed']}/"
            f"{report['progress']['relationships']['total']} "
            f"({report['progress']['relationships']['percent']}%)",
            f"- valid/uncertain/rejected: {report['progress']['valid_count']}/"
            f"{report['progress']['uncertain_count']}/{report['progress']['rejected_count']}",
            f"- accounting: {report['accounting']}",
            "",
            "## Cards",
            "",
            f"- outcomes: {report['cards']['outcomes']}",
            f"- rejection reasons: {report['cards']['rejection_reasons']}",
            f"- malformed reasons: {report['cards']['malformed_reasons']}",
        ]
    )
    for row in report["cards"]["items"]:
        if row["rejected"] or row["malformed_reason"]:
            lines.append(
                f"- card {row['card_id']}: outcome {row['outcome']}, "
                f"capabilities {row['capabilities']}, roles {row['roles']}, "
                f"rejected {row['rejected']}, malformed {row['malformed_reason']}"
            )
    lines.extend(["", "## Guide findings", ""])
    for row in report["guides"]["items"]:
        lines.append(
            f"- guide `{row['guide_id']}`: outcome {row['outcome']}, "
            f"accepted {row['accepted']}, uncertain {row['uncertain']}, "
            f"malformed {row['malformed_reason']}"
        )
        for claim in row["claims"]:
            lines.append(
                f"  - claim `{claim['finding_id']}` ({claim['category']}, {claim['status']}): "
                f"{claim['name']} - {claim['claim']!r} reasons={claim['reason']} "
                f"quotes={claim['quotes']}"
            )
        for finding in row["rejected"]:
            lines.append(
                f"  - rejected `{finding['finding_id']}`: {finding['reason']}"
            )
    lines.extend(["", "## Candidate construction", "", f"- {report['candidates']}", ""])
    lines.extend(
        [
            "## Relationships",
            "",
            f"- verdicts: {report['relationships']['verdicts']}",
            f"- rejection reasons: {report['relationships']['rejection_reasons']}",
        ]
    )
    for row in report["relationships"]["items"]:
        lines.append(
            f"- `{row['subject_id']}` mechanism {row['mechanism']} "
            f"outcome {row['outcome']} verdict {row['verdict']} reason {row['reason']}"
        )
        if row["participants"] is not None:
            lines.append(f"  - source: {row['participants']['source']}")
            lines.append(f"  - target: {row['participants']['target']}")
        if row["evidence"]:
            lines.append(f"  - evidence: {row['evidence']}")
    lines.extend(["", "## Set profiles", "", f"- {report['profiles']}", ""])
    lines.extend(["", "## Benchmark comparison", ""])
    comparison = report["benchmark"]
    if not comparison["present"]:
        lines.append(f"- {comparison['note']} (path `{comparison['path']}`)")
    else:
        lines.append(f"- benchmark: `{comparison['path']}` (trusted {comparison['trusted']})")
        lines.append(f"- validation: {comparison['validation']}")
        lines.append(
            f"- source identity match: {comparison['source_identity_match']} "
            f"(run `{comparison['run_set_source_sha256']}`, "
            f"reviewed `{comparison['reviewed_set_source_sha256']}`)"
        )
        lines.append(f"- matching rules: {comparison['matching_rules']}")
        for kind in ("mechanics", "relationships"):
            for item in comparison[kind]:
                requirement = "required" if item["required"] else "optional"
                lines.append(
                    f"- [{kind}] {requirement} {item['expectation']}: "
                    f"matched={item['matched']} classification={item['classification']} - "
                    f"{item['detail']}"
                )
                for match in item.get("matches", ()):
                    lines.append(f"  - match: {match}")
                for rejection in item.get("rejections", ()):
                    lines.append(f"  - rejected: {rejection}")
                if item.get("omission") is not None:
                    lines.append(f"  - omission: {item['omission']}")
                if item.get("participant_capabilities"):
                    lines.append(f"  - participant capabilities: {item['participant_capabilities']}")
        lines.append(f"- required expectations unmatched: {comparison['required_unmatched']}")
    lines.extend(["", "## Acceptance", ""])
    for failure in report["acceptance"]["failures"]:
        lines.append(f"- failure: {failure}")
    lines.append(f"- passed: {report['acceptance']['passed']}")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    """Build the HOB enrichment run argument parser."""
    parser = argparse.ArgumentParser(description="Run and report the complete HOB set enrichment")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIR,
        help=f"durable work artifact directory (default: {DEFAULT_WORK_DIR})",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=DEFAULT_RUN_DIR,
        help=(
            "directory holding the guide, benchmark and written report "
            f"(default: {DEFAULT_RUN_DIR})"
        ),
    )
    parser.add_argument(
        "--max-usd",
        default=DEFAULT_MAX_USD,
        help=f"hard spending ceiling in USD (default: {DEFAULT_MAX_USD})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="answer every pinned request with deterministic offline content",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="cap the eligible cards processed for a cheap smoke run",
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        help=f"reviewed benchmark path (default: <run-dir>/{BENCHMARK_FILE_NAME})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the HOB enrichment analysis and return the process status."""
    args = build_parser().parse_args(args=argv)
    try:
        ceiling = Decimal(args.max_usd)
    except (InvalidOperation, TypeError) as error:
        raise SystemExit(f"--max-usd must be a decimal amount: {error}")
    if not ceiling.is_finite() or ceiling <= 0:
        raise SystemExit("--max-usd must be a positive finite decimal amount.")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be a positive integer.")
    run_dir = Path(args.run_dir)
    benchmark_path = Path(args.benchmark) if args.benchmark else run_dir / BENCHMARK_FILE_NAME
    args.benchmark = benchmark_path
    try:
        return _run(
            args=args,
            run_dir=run_dir,
            ceiling=ceiling,
            benchmark_path=benchmark_path,
        )
    except HobEnrichmentRunError as error:
        print(f"error={error}")
        return EXIT_UNUSABLE_INPUT


def _run(
    *,
    args: argparse.Namespace,
    run_dir: Path,
    ceiling: Decimal,
    benchmark_path: Path,
) -> int:
    """Execute one HOB analysis run, write its report and return the process status."""
    work_dir = Path(args.work_dir)
    print(
        f"mode={'dry-run' if args.dry_run else 'live'} work_dir={work_dir}"
        f" run_dir={run_dir} ceiling_usd={_usd_text(ceiling)}"
    )
    data, card_payload = _load_card_artifact()
    benchmark = _load_benchmark(benchmark_path)
    guides, guide_facts = _load_guides(run_dir=run_dir, benchmark=benchmark)
    full_sources = _build_sources(data=data, guides=guides)
    benchmark_validation: Mapping[str, Any] | None = None
    if benchmark is not None:
        benchmark_validation = _validate_benchmark(
            benchmark=benchmark,
            path=benchmark_path,
            sources=full_sources,
            card_payload=card_payload,
            guide_text=None if not guides else guides[0].text,
            guide_facts=guide_facts,
        )
    sources, limit_facts = _limited_sources(full_sources, limit=args.limit)
    card_facts = {
        "sha256": _sha256_bytes(card_payload),
        "card_count": len(data.cards),
    }
    _validate_acquisition_config(sources)
    model_config = (
        WorkModelConfig(
            model=DRY_RUN_MODEL,
            reasoning_effort=REASONING_EFFORT,
            max_tokens=MAX_TOKENS,
        )
        if args.dry_run
        else WorkModelConfig(
            model=MODEL,
            reasoning_effort=REASONING_EFFORT,
            max_tokens=MAX_TOKENS,
        )
    )
    profile_directories = _profile_directories(work_dir=work_dir, run_dir=run_dir)
    profiles_before = _profile_snapshot(profile_directories)
    dry_run_completion: _DryRunCompletion | None = None
    if args.dry_run:
        dry_run_completion = _DryRunCompletion(sources=sources)
        complete: Completion = dry_run_completion
    else:
        complete = openrouter_completion(model_config=model_config)
    guard = _SpendGuard(complete=complete, ceiling=ceiling)
    store = SetEnrichmentWorkStore(work_dir)
    try:
        result = run_set_enrichment(
            sources=sources,
            complete=guard,
            work_store=store,
            model_config=model_config,
            observer=_print_progress,
        )
    except SpendCeilingExceeded as error:
        profiles_after = _profile_snapshot(profile_directories)
        comparison = _profile_comparison(profiles_before, profiles_after)
        print(
            f"spend_ceiling_stop={error} accumulated_usd={_usd_text(guard.spent)}"
            f" ceiling_usd={_usd_text(guard.ceiling)} requests={guard.requests}"
        )
        print(f"profiles_unchanged={comparison['unchanged']}")
        return EXIT_SPEND_CEILING
    profiles_after = _profile_snapshot(profile_directories)
    report = _build_report(
        args=args,
        result=result,
        sources=sources,
        full_sources=full_sources,
        card_facts=card_facts,
        guide_facts=guide_facts,
        limit_facts=limit_facts,
        model_config=model_config,
        guard=guard,
        ceiling=ceiling,
        profiles=_profile_comparison(profiles_before, profiles_after),
        benchmark=benchmark,
        benchmark_validation=benchmark_validation,
    )
    json_path, md_path = _write_report(report, run_dir=run_dir)
    accounting = result.progress.accounting
    if dry_run_completion is not None:
        print(f"dry_run_calls={len(dry_run_completion.calls)} network=unused")
    print(
        f"summary outcome={result.outcome.value} review={result.review.state}"
        f" run_id={result.run_id} cards={len(result.card_ids)}/{limit_facts['eligible']}"
        f" relationships={len(result.relationship_results)}"
        f" reused_work={accounting.reused_work} executed_work={accounting.executed_work}"
        f" running_cost_usd={accounting.running_cost_usd}"
        f" projected_final_cost_usd={accounting.projected_final_cost_usd}"
    )
    print(f"report_json={json_path} report_md={md_path}")
    for failure in report["acceptance"]["failures"]:
        print(f"acceptance_failure kind={failure['kind']} detail={failure['detail']}")
    print(f"acceptance_passed={report['acceptance']['passed']}")
    return EXIT_OK if report["acceptance"]["passed"] else EXIT_ACCEPTANCE_FAILURE


if __name__ == "__main__":
    raise SystemExit(main(argv=None))
