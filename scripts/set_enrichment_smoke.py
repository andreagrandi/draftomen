"""Drive resumable set-enrichment orchestration end to end with fixture responses.
Run it without network access or an API key; every durable artifact stays inside the work directory.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import tempfile
from typing import Any

from draftomen.carddb import CardInfo
from draftomen.openrouter_client import OpenRouterResponse
from draftomen.semantic_enrichment import EnrichmentSources, GuideSource, card_source_sha256
from draftomen.set_enrichment import (
    EnrichmentOutcome,
    EnrichmentProgress,
    EnrichmentRunResult,
    run_set_enrichment,
)
from draftomen.set_enrichment_extraction import (
    CARD_CAPABILITY_EXTRACTION_PROMPT_ID,
    GUIDE_EXTRACTION_PROMPT_ID,
    RELATIONSHIP_VALIDATION_PROMPT_ID,
    ExtractionRequest,
    build_card_capability_extraction_request,
    build_guide_extraction_request,
)
from draftomen.set_enrichment_work import (
    SetEnrichmentWorkStore,
    WorkIdentity,
    WorkKind,
    WorkModelConfig,
    WorkState,
    build_work_identity,
)

SET_CODE = "smk"
GUIDE_ID = "smoke-guide"
GUIDE_URL = "https://guides.example.test/smk-review"
RETRIEVED_AT = "2026-09-01T12:00:00Z"
GUIDE_FINDING_ID = "claim-token-plan"
GUIDE_SENTENCE = "Token strategies in this format win by going wide before the ground stalls."
GUIDE_CLAIM = "Token strategies win by going wide before the ground stalls."
GUIDE_TEXT = GUIDE_SENTENCE + "\n"

TOKEN_CARD_ID = 101
TOKEN_CARD_NAME = "Token Maker"
TOKEN_QUOTE = "Create two 1/1 colorless Soldier artifact creature tokens."
WIDE_CARD_ID = 102
WIDE_CARD_NAME = "Wide Payoff"
WIDE_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
UNRELATED_CARD_ID = 103
UNRELATED_CARD_NAME = "Unrelated Sage"
UNRELATED_QUOTE = "Draw two cards."
INELIGIBLE_CARD_ID = 104
INELIGIBLE_CARD_NAME = "Blank Slate"

RELATIONSHIP_CLAIM = "Tokens feed the go-wide payoff."
MODEL = "smoke/model"
PROVIDER = "smoke"
REASONING_EFFORT = "high"
MAX_TOKENS = 4096
INPUT_TOKENS = 1200
CACHED_INPUT_TOKENS = 300
OUTPUT_TOKENS = 200
REASONING_TOKENS = 40
CALL_COST_USD = "0.002"
INTERRUPT_CALL = 4
FIRST_RUN_ID = "smoke-run-1"
SECOND_RUN_ID = "smoke-run-2"


class _InterruptedError(RuntimeError):
    """Simulate one process interruption during a paid smoke call."""


def _card(*, grp_id: int, name: str, oracle_text: str | None) -> CardInfo:
    """Build one single-faced smoke card bound to the smoke set."""
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=("W",),
        mana_value=2.0,
        rarity="uncommon",
        types=("Creature",),
        oracle_text=oracle_text,
        set_code=SET_CODE,
    )


def _sources() -> EnrichmentSources:
    """Build the frozen fixture with one guide and four canonical cards."""
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
        ),
        guides=(
            GuideSource(
                guide_id=GUIDE_ID,
                url=GUIDE_URL,
                text=GUIDE_TEXT,
                retrieved_at=RETRIEVED_AT,
            ),
        ),
    )


def _model_config() -> WorkModelConfig:
    """Build the frozen acquisition configuration bound into every work identity."""
    return WorkModelConfig(
        model=MODEL,
        reasoning_effort=REASONING_EFFORT,
        max_tokens=MAX_TOKENS,
    )


def _guide_content() -> str:
    """Return the pinned guide response quoting the frozen guide exactly."""
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


def _capability(*, card_id: int, card_name: str, role: str, quote: str) -> dict[str, Any]:
    """Build one scripted capability bound to its exact Oracle quote."""
    return {
        "finding_id": f"capability-{card_id}",
        "card_id": card_id,
        "card_name": card_name,
        "face_index": None,
        "face_name": None,
        "role": role,
        "quantity": None,
        "timing": None,
        "source_zone": None,
        "destination_zone": None,
        "prerequisites": [],
        "evidence": [{"card_id": card_id, "face_index": None, "quote": quote}],
        "review": {"status": "accepted", "reason": None},
    }


def _capability_candidates(card_id: int) -> list[dict[str, Any]]:
    """Build the accepted capabilities one fixture card answers with."""
    if card_id == TOKEN_CARD_ID:
        return [
            _capability(
                card_id=TOKEN_CARD_ID,
                card_name=TOKEN_CARD_NAME,
                role="token_maker",
                quote=TOKEN_QUOTE,
            )
        ]
    if card_id == WIDE_CARD_ID:
        return [
            _capability(
                card_id=WIDE_CARD_ID,
                card_name=WIDE_CARD_NAME,
                role="go_wide_payoff",
                quote=WIDE_QUOTE,
            )
        ]
    if card_id == UNRELATED_CARD_ID:
        return [
            _capability(
                card_id=UNRELATED_CARD_ID,
                card_name=UNRELATED_CARD_NAME,
                role="draw",
                quote=UNRELATED_QUOTE,
            )
        ]
    raise LookupError(f"unexpected card capability request for card {card_id}")


def _capability_content(card_id: int) -> str:
    """Return the pinned capability response for one canonical card."""
    return json.dumps(
        {
            "schema_version": 1,
            "capabilities": _capability_candidates(card_id),
        }
    )


def _relationship_content(prompt: Mapping[str, Any]) -> str:
    """Return one accepted verdict quoting both participants' exact Oracle text."""
    source = prompt["source"]
    target = prompt["target"]
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


def _response_content(request: ExtractionRequest) -> str:
    """Return the fixture content one pinned request answers with."""
    if request.prompt_id == GUIDE_EXTRACTION_PROMPT_ID:
        return _guide_content()
    payload = json.loads(request.user_prompt)
    if request.prompt_id == CARD_CAPABILITY_EXTRACTION_PROMPT_ID:
        return _capability_content(payload["card"]["card_id"])
    if request.prompt_id == RELATIONSHIP_VALIDATION_PROMPT_ID:
        return _relationship_content(payload)
    raise LookupError(f"unexpected prompt id {request.prompt_id}")


class _FakeCompletion:
    """Answer pinned smoke requests deterministically and count every call.

    Raising at a fixed call index leaves the durable store exactly as a killed process would.
    """

    def __init__(self, *, interrupt_at: int | None = None) -> None:
        self.interrupt_at = interrupt_at
        self.calls: list[str] = []

    def __call__(self, request: ExtractionRequest) -> OpenRouterResponse:
        """Answer one pinned request after recording its prompt identity."""
        self.calls.append(request.prompt_id)
        if self.interrupt_at is not None and len(self.calls) == self.interrupt_at:
            raise _InterruptedError(
                f"simulated interruption on call {self.interrupt_at} ({request.prompt_id})"
            )
        return OpenRouterResponse(
            content=_response_content(request),
            model=MODEL,
            provider=PROVIDER,
            input_tokens=INPUT_TOKENS,
            cached_input_tokens=CACHED_INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            reasoning_tokens=REASONING_TOKENS,
            cost_usd=CALL_COST_USD,
        )


def _work_identities(
    *,
    sources: EnrichmentSources,
    model_config: WorkModelConfig,
) -> tuple[WorkIdentity, ...]:
    """Build every durable identity the frozen smoke fixture can request."""
    guide = sources.guides[0]
    identities = [
        build_work_identity(
            work_kind=WorkKind.GUIDE,
            subject_id=guide.guide_id,
            input_sha256=guide.text_sha256,
            request=build_guide_extraction_request(sources=sources, guide_id=guide.guide_id),
            model_config=model_config,
        )
    ]
    for card in sources.cards:
        identities.append(
            build_work_identity(
                work_kind=WorkKind.CARD_CAPABILITY,
                subject_id=str(card.grp_id),
                input_sha256=card_source_sha256(card),
                request=build_card_capability_extraction_request(
                    sources=sources,
                    card_id=card.grp_id,
                ),
                model_config=model_config,
            )
        )
    return tuple(identities)


def _durable_counts(
    *,
    store: SetEnrichmentWorkStore,
    identities: Sequence[WorkIdentity],
) -> str:
    """Count the durable work states of every identity read back through the store."""
    counts = {state: 0 for state in WorkState}
    for identity in identities:
        counts[store.lookup(identity=identity).state] += 1
    return " ".join(f"{state.value}={counts[state]}" for state in WorkState)


def _print_progress(progress: EnrichmentProgress) -> None:
    """Print one immutable progress event as a single smoke line."""
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


def _summary_line(result: EnrichmentRunResult) -> str:
    """Render the run outcome, review state, relationship identity and reuse counts."""
    accepted = next(
        (
            item.relationship
            for item in result.relationship_results
            if item.relationship is not None
        ),
        None,
    )
    relationship = "none" if accepted is None else accepted.finding_id
    ineligible = ",".join(str(card_id) for card_id in result.ineligible_card_ids) or "none"
    accounting = result.progress.accounting
    return (
        f"summary outcome={result.outcome.value} review={result.review.state}"
        f" relationship={relationship} ineligible_card_ids={ineligible}"
        f" reused_work={accounting.reused_work} executed_work={accounting.executed_work}"
    )


def _mismatches(result: EnrichmentRunResult) -> list[str]:
    """List every observable smoke check one resumed run failed."""
    mismatches: list[str] = []
    if result.outcome is not EnrichmentOutcome.COMPLETE:
        mismatches.append(f"outcome is {result.outcome.value}, expected complete")
    if result.review.state != "pending":
        mismatches.append(f"review state is {result.review.state}, expected pending")
    accepted = [
        item.relationship
        for item in result.relationship_results
        if item.relationship is not None
    ]
    packages = () if result.candidate_packages is None else result.candidate_packages.packages
    if len(accepted) != 1:
        mismatches.append(f"accepted relationships are {len(accepted)}, expected 1")
    elif len(packages) != 1 or accepted[0].identity != packages[0].identity:
        mismatches.append("the accepted relationship does not match the constructed candidate")
    accounting = result.progress.accounting
    if accounting.reused_work <= 0:
        mismatches.append(f"reused work is {accounting.reused_work}, expected more than 0")
    if accounting.projected_final_cost_usd != accounting.running_cost_usd:
        mismatches.append(
            "projected final cost "
            f"{accounting.projected_final_cost_usd} does not equal running cost "
            f"{accounting.running_cost_usd}"
        )
    return mismatches


def build_parser() -> argparse.ArgumentParser:
    """Build the smoke-script argument parser."""
    parser = argparse.ArgumentParser(description="Smoke-test resumable set enrichment")
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="directory for durable work artifacts (default: a fresh temporary directory)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the interrupted and resumed smoke analyses and return the process status."""
    args = build_parser().parse_args(args=argv)
    work_dir = (
        Path(tempfile.mkdtemp(prefix="set-enrichment-smoke-"))
        if args.work_dir is None
        else args.work_dir
    )
    print(f"work_dir={work_dir}")
    sources = _sources()
    model_config = _model_config()
    store = SetEnrichmentWorkStore(work_dir)
    identities = _work_identities(sources=sources, model_config=model_config)
    try:
        run_set_enrichment(
            sources=sources,
            complete=_FakeCompletion(interrupt_at=INTERRUPT_CALL),
            work_store=store,
            model_config=model_config,
            run_id=FIRST_RUN_ID,
        )
    except _InterruptedError as error:
        print(f"interruption={error}")
    else:
        print("mismatch=the interrupted run finished without raising")
        return 1
    print(f"durable_work {_durable_counts(store=store, identities=identities)}")
    result = run_set_enrichment(
        sources=sources,
        complete=_FakeCompletion(),
        work_store=store,
        model_config=model_config,
        run_id=SECOND_RUN_ID,
        observer=_print_progress,
    )
    print(_summary_line(result))
    mismatches = _mismatches(result)
    for mismatch in mismatches:
        print(f"mismatch={mismatch}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main(argv=None))
