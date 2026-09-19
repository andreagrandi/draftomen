"""Behavior tests for UI-neutral resumable set-enrichment orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
from typing import Any

import pytest

from draftomen.carddb import CardFace, CardInfo
from draftomen.openrouter_client import OpenRouterResponse
from draftomen.semantic_capability_records import (
    CapabilityAction,
    CapabilityCardType,
    CapabilityPrerequisite,
    CapabilityQualifier,
    CapabilityQuantity,
    CapabilityTokenRestriction,
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
import draftomen.set_enrichment as set_enrichment_module
from draftomen.set_enrichment import (
    RELATIONSHIP_BATCH_SIZE,
    Completion,
    EnrichmentOutcome,
    EnrichmentPhase,
    EnrichmentProgress,
    EnrichmentRunResult,
    partition_relationship_batches,
    run_set_enrichment,
)
from draftomen.set_enrichment_candidates import (
    CandidateBounds,
    CandidatePackage,
    CandidatePackageSet,
    CandidateResolutionBasis,
    CandidateResolutionVerdict,
    construct_candidate_packages,
    resolve_candidate_packages,
)
from draftomen.set_enrichment_extraction import (
    CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION,
    CARD_CAPABILITY_EXTRACTION_PROMPT_ID,
    GUIDE_EXTRACTION_PROMPT_ID,
    RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID,
    CardCapabilityExtractionResult,
    ExtractionOutcome,
    ExtractionRequest,
    build_card_capability_extraction_request,
    build_guide_extraction_request,
    build_relationship_validation_batch_request,
    parse_card_capability_extraction_response,
    parse_guide_extraction_response,
    relationship_batch_source_sha256,
    relationship_batch_subject_id,
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

# Fixture cards whose structured capability parameters alone settle the outcome of every pair, so
# the residual-only batches stay observable next to the local decisions they exclude.
LOCAL_TOKEN_CARD_ID = 401
LOCAL_TOKEN_CARD_NAME = "Local Token Maker"
LOCAL_TOKEN_QUOTE = "Create two 1/1 colorless Soldier artifact creature tokens."
LOCAL_TOKEN_FINDING_ID = "capability-local-tokens"
LOCAL_PAYOFF_CARD_ID = 411
LOCAL_PAYOFF_CARD_NAME = "Local Count Payoff"
LOCAL_PAYOFF_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
LOCAL_PAYOFF_FINDING_ID = "capability-local-wide"
LOCAL_NONTOKEN_CARD_ID = 412
LOCAL_NONTOKEN_CARD_NAME = "Nontoken Payoff"
LOCAL_NONTOKEN_QUOTE = "Nontoken creatures you control get +1/+1."
LOCAL_NONTOKEN_FINDING_ID = "capability-local-nontoken"
LOCAL_PROVE_REASON = "structured capability parameters prove compatibility"
LOCAL_TOKEN_RESTRICTION_CONFLICT_REASON = (
    "structured capability parameters conflict: token_restriction"
)

# Fixture whose residual pairs alone exceed one validation batch. Every declared mechanism is
# routed locally, so a residual pair can only come from a mechanism the declared rules do not
# carry: the fixture construction rewrites one declared mechanism's pairs onto that one.
RESIDUAL_MECHANISM = "token-go-wide-payoff"
UNDECLARED_MECHANISM = "undeclared-mechanism"
BATCH_STATED_MAKER_CARD_ID = 501
BATCH_UNSTATED_MAKER_CARD_IDS = (511, 512, 513, 514, 515)
BATCH_MAKER_CARD_IDS = (
    BATCH_STATED_MAKER_CARD_ID,
    *BATCH_UNSTATED_MAKER_CARD_IDS,
)
BATCH_PAYOFF_CARD_IDS = (601, 602, 603, 604, 605)
BATCH_PAYOFF_CARD_ID = BATCH_PAYOFF_CARD_IDS[0]
BATCH_NONTOKEN_PAYOFF_CARD_ID = 611
BATCH_DEATH_PAYOFF_CARD_ID = 621
BATCH_NONTOKEN_DEATH_PAYOFF_CARD_ID = 622
BATCH_TOKEN_QUOTE = "Create two 1/1 colorless Soldier artifact creature tokens."
BATCH_PAYOFF_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
BATCH_NONTOKEN_QUOTE = "Nontoken creatures you control get +1/+1."
BATCH_DEATH_PAYOFF_QUOTE = "Whenever a creature you control dies, each opponent loses 1 life."
BATCH_NONTOKEN_DEATH_PAYOFF_QUOTE = "Whenever a nontoken creature you control dies, draw a card."
BATCH_LOCAL_ACCEPTED_PAIRS = 6
BATCH_LOCAL_REJECTED_PAIRS = 6
BATCH_RESIDUAL_PAIRS = 36
BATCH_LOCAL_PAIRS = BATCH_LOCAL_ACCEPTED_PAIRS + BATCH_LOCAL_REJECTED_PAIRS
BATCH_TOTAL_PAIRS = BATCH_LOCAL_PAIRS + BATCH_RESIDUAL_PAIRS
BATCH_BATCHES = 2
BATCH_CARDS = len(BATCH_MAKER_CARD_IDS) + len(BATCH_PAYOFF_CARD_IDS) + 3
BATCH_WORKED_CALLS = 1 + BATCH_CARDS + BATCH_BATCHES
LEGACY_CARD_CAPABILITY_PROMPT_ID = "draftomen-card-capability-extraction-v1"
LEGACY_CARD_CAPABILITY_RESPONSE_SCHEMA_ID = "draftomen-card-capability-extraction-response-v1"
LEGACY_CARD_CAPABILITY_SCHEMA_NAME = "draftomen_card_capability_extraction_v1"
LEGACY_CARD_CAPABILITY_CONTRACT_VERSION = 1
_CARD_PROMPT_IDS = frozenset(
    {CARD_CAPABILITY_EXTRACTION_PROMPT_ID, LEGACY_CARD_CAPABILITY_PROMPT_ID}
)
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
TOTAL_COST_USD = "0.01"
MIDRUN_PROJECTED_COST_USD = "0.01"
# One guide plus every eligible card: the standard fixture leaves no pair for the model.
WORKED_CALLS = 5
CARD_MALFORMED_REASON = (
    "response does not match card capability extraction schema version "
    f"{CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION}."
)
RELATIONSHIP_EVIDENCE_QUOTE_REASON = (
    "relationship Oracle evidence quote is not an exact source substring."
)
RELATIONSHIP_MALFORMED_REASON = "response does not match relationship validation schema version 2."
RELATIONSHIP_UNCERTAIN_REASON = (
    "Both Oracle texts support the interaction but the timing is ambiguous."
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


def _durable_run_id(card_id: int) -> str:
    """Return the invocation-independent run identifier of one durable card record."""
    return f"durable-{_card_identity(card_id).content_sha256[:16]}"


def _legacy_card_request(*, sources: EnrichmentSources, card_id: int) -> ExtractionRequest:
    """Build the pinned pre-v2 card request whose paid responses the migration resumes from.

    Only the contract version, the three v1 card identities, and the user-prompt contract
    version differ from the current request; the response schema stays the current one.
    """
    current = build_card_capability_extraction_request(sources=sources, card_id=card_id)
    payload = _prompt_payload(current)
    payload["contract_version"] = LEGACY_CARD_CAPABILITY_CONTRACT_VERSION
    return ExtractionRequest(
        contract_version=LEGACY_CARD_CAPABILITY_CONTRACT_VERSION,
        prompt_id=LEGACY_CARD_CAPABILITY_PROMPT_ID,
        system_prompt=current.system_prompt,
        user_prompt=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        response_schema_id=LEGACY_CARD_CAPABILITY_RESPONSE_SCHEMA_ID,
        response_schema_name=LEGACY_CARD_CAPABILITY_SCHEMA_NAME,
        schema=current.response_schema(),
    )


def _legacy_card_identity(card_id: int) -> WorkIdentity:
    """Return the durable identity the pre-v2 card request produces for one fixture card."""
    return build_work_identity(
        work_kind=WorkKind.CARD_CAPABILITY,
        subject_id=str(card_id),
        input_sha256=card_source_sha256(_card_for(card_id)),
        request=_legacy_card_request(sources=_sources(), card_id=card_id),
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


def _qualifier(*, card_types: list[str], token_restriction: str) -> dict[str, Any]:
    """Build one explicit closed qualifier object for a scripted capability."""
    return {
        "card_types": card_types,
        "token_restriction": token_restriction,
        "subtype": None,
        "mana_value": None,
    }


def _role_arguments(role: str) -> tuple[str, str, dict[str, Any]]:
    """Return the role-accurate v2 action, zone, and qualifier of one scripted capability."""
    if role == "token_maker":
        return (
            "create",
            "battlefield",
            _qualifier(card_types=["creature"], token_restriction="token"),
        )
    if role == "go_wide_payoff":
        return (
            "control",
            "battlefield",
            _qualifier(card_types=["creature"], token_restriction="unrestricted"),
        )
    if role == "death_payoff":
        return (
            "die",
            "graveyard",
            _qualifier(card_types=["creature"], token_restriction="unrestricted"),
        )
    if role == "draw":
        return "draw", "hand", _qualifier(card_types=[], token_restriction="unrestricted")
    return "other", "battlefield", _qualifier(card_types=[], token_restriction="unrestricted")


def _capability_candidate(
    *,
    finding_id: str,
    card_id: int,
    card_name: str,
    role: str,
    quote: str,
    face_index: int | None = None,
    face_name: str | None = None,
    qualifier: dict[str, Any] | None = None,
    quantity: dict[str, Any] | None = None,
    timing: str | None = None,
    source_zone: str | None = None,
    destination_zone: str | None = None,
    prerequisites: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one scripted capability entry bound to exact Oracle evidence."""
    action, zone, role_qualifier = _role_arguments(role)
    return {
        "finding_id": finding_id,
        "card_id": card_id,
        "card_name": card_name,
        "face_index": face_index,
        "face_name": face_name,
        "role": role,
        "action": action,
        "zone": zone,
        "qualifier": role_qualifier if qualifier is None else qualifier,
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
            "schema_version": CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION,
            "capabilities": _capability_candidates(card_id),
        }
    )


def _local_sources() -> EnrichmentSources:
    """Build the fixture whose pairs the structured parameters settle on their own."""
    cards = [
        _card(
            grp_id=LOCAL_TOKEN_CARD_ID,
            name=LOCAL_TOKEN_CARD_NAME,
            oracle_text=LOCAL_TOKEN_QUOTE,
        ),
        _card(
            grp_id=LOCAL_PAYOFF_CARD_ID,
            name=LOCAL_PAYOFF_CARD_NAME,
            oracle_text=LOCAL_PAYOFF_QUOTE,
        ),
        _card(
            grp_id=LOCAL_NONTOKEN_CARD_ID,
            name=LOCAL_NONTOKEN_CARD_NAME,
            oracle_text=LOCAL_NONTOKEN_QUOTE,
        ),
    ]
    return EnrichmentSources(set_code=SET_CODE, cards=tuple(cards), guides=(_guide_source(),))


def _local_capability_candidates(card_id: int) -> list[dict[str, Any]]:
    """Build the scripted capability entries one local-resolution fixture card answers with."""
    if card_id == LOCAL_TOKEN_CARD_ID:
        return [
            _capability_candidate(
                finding_id=LOCAL_TOKEN_FINDING_ID,
                card_id=LOCAL_TOKEN_CARD_ID,
                card_name=LOCAL_TOKEN_CARD_NAME,
                role="token_maker",
                quote=LOCAL_TOKEN_QUOTE,
                destination_zone="battlefield",
            )
        ]
    if card_id == LOCAL_PAYOFF_CARD_ID:
        return [
            _capability_candidate(
                finding_id=LOCAL_PAYOFF_FINDING_ID,
                card_id=LOCAL_PAYOFF_CARD_ID,
                card_name=LOCAL_PAYOFF_CARD_NAME,
                role="go_wide_payoff",
                quote=LOCAL_PAYOFF_QUOTE,
            )
        ]
    if card_id == LOCAL_NONTOKEN_CARD_ID:
        return [
            _capability_candidate(
                finding_id=LOCAL_NONTOKEN_FINDING_ID,
                card_id=LOCAL_NONTOKEN_CARD_ID,
                card_name=LOCAL_NONTOKEN_CARD_NAME,
                role="go_wide_payoff",
                quote=LOCAL_NONTOKEN_QUOTE,
                qualifier=_qualifier(card_types=["creature"], token_restriction="nontoken"),
            )
        ]
    raise AssertionError(f"unexpected local card capability request for {card_id}.")


def _local_capability_content(card_id: int) -> str:
    return json.dumps(
        {
            "schema_version": CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION,
            "capabilities": _local_capability_candidates(card_id),
        }
    )


def _batch_sources() -> EnrichmentSources:
    """Build the fixture whose residual pairs alone need more than one validation batch."""
    cards = [
        _card(
            grp_id=card_id,
            name=f"Batch Maker {card_id}",
            oracle_text=BATCH_TOKEN_QUOTE,
        )
        for card_id in BATCH_MAKER_CARD_IDS
    ]
    cards.extend(
        _card(grp_id=card_id, name=f"Batch Payoff {card_id}", oracle_text=BATCH_PAYOFF_QUOTE)
        for card_id in BATCH_PAYOFF_CARD_IDS
    )
    cards.extend(
        (
            _card(
                grp_id=BATCH_NONTOKEN_PAYOFF_CARD_ID,
                name="Batch Nontoken Payoff",
                oracle_text=BATCH_NONTOKEN_QUOTE,
            ),
            _card(
                grp_id=BATCH_DEATH_PAYOFF_CARD_ID,
                name="Batch Death Payoff",
                oracle_text=BATCH_DEATH_PAYOFF_QUOTE,
            ),
            _card(
                grp_id=BATCH_NONTOKEN_DEATH_PAYOFF_CARD_ID,
                name="Batch Nontoken Death Payoff",
                oracle_text=BATCH_NONTOKEN_DEATH_PAYOFF_QUOTE,
            ),
        )
    )
    return EnrichmentSources(set_code=SET_CODE, cards=tuple(cards), guides=(_guide_source(),))


def _batch_payoff_payload() -> dict[str, Any]:
    """Return the full v2 payload of the batch fixture's first unrestricted payoff."""
    return {
        "quantity": {"value": 2, "relation": "exactly"},
        "timing": WIDE_TIMING,
        "source_zone": "battlefield",
        "destination_zone": "battlefield",
        "prerequisites": [
            {
                "kind": "threshold",
                "quantity": {"value": 3, "relation": "at_least"},
                "timing": WIDE_PREREQUISITE_TIMING,
                "source_zone": "battlefield",
                "destination_zone": "graveyard",
                "evidence": {
                    "card_id": BATCH_PAYOFF_CARD_ID,
                    "face_index": None,
                    "quote": BATCH_PAYOFF_QUOTE,
                },
            }
        ],
    }


def _batch_capability_candidates(card_id: int) -> list[dict[str, Any]]:
    """Build the scripted capability entries one batch fixture card answers with."""
    if card_id == BATCH_STATED_MAKER_CARD_ID:
        return [
            _capability_candidate(
                finding_id=f"capability-{card_id}",
                card_id=card_id,
                card_name=f"Batch Maker {card_id}",
                role="token_maker",
                quote=BATCH_TOKEN_QUOTE,
                destination_zone="battlefield",
            )
        ]
    if card_id in BATCH_UNSTATED_MAKER_CARD_IDS:
        return [
            _capability_candidate(
                finding_id=f"capability-{card_id}",
                card_id=card_id,
                card_name=f"Batch Maker {card_id}",
                role="token_maker",
                quote=BATCH_TOKEN_QUOTE,
            )
        ]
    if card_id in BATCH_PAYOFF_CARD_IDS:
        return [
            _capability_candidate(
                finding_id=f"capability-{card_id}",
                card_id=card_id,
                card_name=f"Batch Payoff {card_id}",
                role="go_wide_payoff",
                quote=BATCH_PAYOFF_QUOTE,
                **(_batch_payoff_payload() if card_id == BATCH_PAYOFF_CARD_ID else {}),
            )
        ]
    if card_id == BATCH_NONTOKEN_PAYOFF_CARD_ID:
        return [
            _capability_candidate(
                finding_id=f"capability-{card_id}",
                card_id=card_id,
                card_name="Batch Nontoken Payoff",
                role="go_wide_payoff",
                quote=BATCH_NONTOKEN_QUOTE,
                qualifier=_qualifier(card_types=["creature"], token_restriction="nontoken"),
            )
        ]
    if card_id == BATCH_DEATH_PAYOFF_CARD_ID:
        return [
            _capability_candidate(
                finding_id=f"capability-{card_id}",
                card_id=card_id,
                card_name="Batch Death Payoff",
                role="death_payoff",
                quote=BATCH_DEATH_PAYOFF_QUOTE,
                source_zone="battlefield",
            )
        ]
    if card_id == BATCH_NONTOKEN_DEATH_PAYOFF_CARD_ID:
        return [
            _capability_candidate(
                finding_id=f"capability-{card_id}",
                card_id=card_id,
                card_name="Batch Nontoken Death Payoff",
                role="death_payoff",
                quote=BATCH_NONTOKEN_DEATH_PAYOFF_QUOTE,
                source_zone="battlefield",
                qualifier=_qualifier(card_types=["creature"], token_restriction="nontoken"),
            )
        ]
    raise AssertionError(f"unexpected batch card capability request for {card_id}.")


def _batch_capability_content(card_id: int) -> str:
    return json.dumps(
        {
            "schema_version": CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION,
            "capabilities": _batch_capability_candidates(card_id),
        }
    )


def _undeclared_construction(mechanism: str) -> Callable[..., CandidatePackageSet]:
    """Return a constructor that leaves one declared mechanism's pairs unresolved.

    Every declared mechanism is routed locally, so a pair reaches the model only when its
    mechanism is one the declared rules do not carry. This seam keeps that residual path
    observable by rewriting the pairs of one declared mechanism onto an undeclared one.
    """

    def construct(
        results: Iterable[CardCapabilityExtractionResult],
        *,
        bounds: CandidateBounds = CandidateBounds(),
    ) -> CandidatePackageSet:
        """Construct the fixture pairs and leave one mechanism's pairs for the model."""
        constructed = construct_candidate_packages(results, bounds=bounds)
        return replace(
            constructed,
            packages=tuple(
                sorted(
                    (
                        replace(package, mechanism=UNDECLARED_MECHANISM)
                        if package.mechanism == mechanism
                        else package
                        for package in constructed.packages
                    ),
                    key=lambda item: item.identity,
                )
            ),
        )

    return construct


def _batch_card_results() -> tuple[CardCapabilityExtractionResult, ...]:
    """Return every parsed card extraction the batch fixture produces, in engine order."""
    return tuple(
        parse_card_capability_extraction_response(
            content=_batch_capability_content(card_id),
            sources=_batch_sources(),
            card_id=card_id,
            run_id=RUN_ID,
        )
        for card_id in sorted(card.grp_id for card in _batch_sources().cards)
    )


def _residual_package_set() -> CandidatePackageSet:
    """Return the batch fixture's constructed pairs with one mechanism left unresolved."""
    return _undeclared_construction(RESIDUAL_MECHANISM)(_batch_card_results())


def _residual_batch(batch_index: int) -> tuple[CandidatePackage, ...]:
    """Return one residual validation batch of the batch fixture, in engine order."""
    package_set = _residual_package_set()
    return partition_relationship_batches(
        resolve_candidate_packages(package_set.packages).model_packages
    )[batch_index]


def _residual_batch_identity(batch_index: int) -> WorkIdentity:
    """Return the durable identity of one residual validation batch of the batch fixture."""
    batch = _residual_batch(batch_index)
    request = build_relationship_validation_batch_request(sources=_batch_sources(), packages=batch)
    return build_work_identity(
        work_kind=WorkKind.RELATIONSHIP,
        subject_id=relationship_batch_subject_id(batch_index),
        input_sha256=relationship_batch_source_sha256(request=request),
        request=request,
        model_config=_model_config(),
    )


def _legacyize_batch_result(work_root: Path, *, batch_index: int) -> None:
    """Rewrite one stored residual batch result into its canonical pre-v2 bytes.

    Only the v2 capability fields leave each accepted verdict's participants, so the artifact
    keeps its envelope, identity, and completion timestamp and stays byte-canonical.
    """
    path = work_root / "results" / f"{_residual_batch_identity(batch_index).content_sha256}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for verdict in payload["result"]["verdicts"]:
        relationship = verdict["relationship"]
        if relationship is None:
            continue
        for participant in (relationship["source"], relationship["target"]):
            for field_name in ("action", "zone", "qualifier"):
                del participant[field_name]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    path.write_bytes((encoded + "\n").encode("utf-8"))


def _contract_violating_card_content(card_id: int) -> str:
    """Build one v2 card response whose last capability carries the paid-store violation.

    The paid HOB responses state an accepted capability with a non-null reason, which the strict
    capability validator rejects item by item.
    """
    capabilities = _capability_candidates(card_id)
    return json.dumps(
        {
            "schema_version": CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION,
            "capabilities": [
                *capabilities[:-1],
                {**capabilities[-1], "review": {"status": "accepted", "reason": "Looks correct."}},
            ],
        }
    )


def _duplicate_finding_id_card_content(card_id: int) -> str:
    """Build one v2 card response whose two capabilities repeat one finding_id."""
    capability = _capability_candidates(card_id)[0]
    return json.dumps(
        {
            "schema_version": CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION,
            "capabilities": [capability, capability],
        }
    )


def _run_with_card_content(
    work_root: Path,
    *,
    card_id: int,
    content: str,
) -> EnrichmentRunResult:
    """Run one analysis in which the named card answers with the scripted content."""
    return _run(
        work_root=work_root,
        completion=_FakeCompletion(
            card_content=lambda requested: (
                content if requested == card_id else _capability_content(requested)
            )
        ),
    )


def _malform_stored_card_result(work_root: Path, *, card_id: int) -> None:
    """Rewrite one stored card result into the all-or-nothing malformed artifact.

    The strict parser wrote exactly this artifact for a response that violates the capability
    contract in one item, and the paid response stored beside it is what a resume recovers.
    """
    path = work_root / "results" / f"{_card_identity(card_id).content_sha256}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"] = CardCapabilityExtractionResult(
        outcome=ExtractionOutcome.MALFORMED,
        accepted_capabilities=(),
        uncertain_capabilities=(),
        rejected_capabilities=(),
        malformed_reason=CARD_MALFORMED_REASON,
    ).to_json()
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    path.write_bytes((encoded + "\n").encode("utf-8"))


def _drop_stored_card_finding(work_root: Path, *, card_id: int, finding_id: str) -> None:
    """Rewrite one historical result as if its parser silently omitted a valid candidate."""
    path = work_root / "results" / f"{_card_identity(card_id).content_sha256}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = payload["result"]
    result["uncertain_capabilities"] = [
        item
        for item in result["uncertain_capabilities"]
        if item["finding_id"] != finding_id
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    path.write_bytes((encoded + "\n").encode("utf-8"))


def _relationship_verdict(pair: dict[str, Any], *, foreign_quote: str | None) -> dict[str, Any]:
    """Build one accepted v2 verdict for a single pair of a batch request.
    The typed prerequisites stay advisory, so no projection is fabricated.
    """
    source = pair["source"]
    target = pair["target"]
    source_quote = source["evidence"][0]["quote"] if foreign_quote is None else foreign_quote
    return {
        "index": pair["index"],
        "schema_version": 2,
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
        "prerequisite_status": "uncertain",
        "source_prerequisites": [],
        "target_prerequisites": [],
    }


def _relationship_content(prompt: dict[str, Any], *, foreign_quote: str | None) -> str:
    """Build one accepted verdict per pair the batch request lists."""
    return json.dumps(
        {
            "verdicts": [
                _relationship_verdict(pair, foreign_quote=foreign_quote)
                for pair in prompt["pairs"]
            ]
        }
    )


def _evidence_less_relationship_content(size: int) -> str:
    """Build one batch response whose verdicts omit the participant evidence a record requires."""
    return json.dumps(
        {
            "verdicts": [
                {
                    "index": index,
                    "schema_version": 2,
                    "verdict": "uncertain",
                    "claim": RELATIONSHIP_CLAIM,
                    "reason": RELATIONSHIP_UNCERTAIN_REASON,
                    "evidence": [],
                    "prerequisite_status": "uncertain",
                    "source_prerequisites": [],
                    "target_prerequisites": [],
                }
                for index in range(size)
            ]
        }
    )


def _prompt_payload(request: ExtractionRequest) -> dict[str, Any]:
    return json.loads(request.user_prompt)


def _prompt_subject(request: ExtractionRequest) -> str:
    """Return the durable work subject one guide or card request describes."""
    payload = _prompt_payload(request)
    if request.prompt_id == GUIDE_EXTRACTION_PROMPT_ID:
        return payload["guide"]["guide_id"]
    if request.prompt_id in _CARD_PROMPT_IDS:
        return str(payload["card"]["card_id"])
    raise AssertionError(f"unexpected prompt {request.prompt_id}.")


def _completion_content(
    request: ExtractionRequest,
    *,
    foreign_quote: str | None,
    card_content: Callable[[int], str] | None = None,
) -> str:
    """Build the scripted fixture content one pinned request answers with."""
    payload = _prompt_payload(request)
    if request.prompt_id == GUIDE_EXTRACTION_PROMPT_ID:
        return _guide_content()
    if request.prompt_id in _CARD_PROMPT_IDS:
        card_id = payload["card"]["card_id"]
        return _capability_content(card_id) if card_content is None else card_content(card_id)
    return _relationship_content(payload, foreign_quote=foreign_quote)


class _FakeCompletion:
    """Answer pinned requests deterministically and record every call."""

    def __init__(
        self,
        *,
        behaviours: Sequence[str] = (),
        fail_after: int | None = None,
        foreign_relationship_quote: str | None = None,
        card_content: Callable[[int], str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.requests: list[ExtractionRequest] = []
        self.behaviours = behaviours
        self.fail_after = fail_after
        self.foreign_relationship_quote = foreign_relationship_quote
        self.card_content = card_content

    def __call__(self, request: ExtractionRequest) -> OpenRouterResponse:
        """Answer one pinned request, raising once the scripted failure count is reached."""
        if request.prompt_id == RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID:
            # Only the caller knows a batch's index, so the fixture labels the batch each run
            # requests in order, exactly like the engine's durable subject.
            subject = relationship_batch_subject_id(
                len(self.calls_for(RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID))
            )
        else:
            subject = _prompt_subject(request)
        self.calls.append((request.prompt_id, subject))
        self.requests.append(request)
        index = len(self.calls) - 1
        if self.fail_after is not None and index >= self.fail_after:
            raise _CompletionFailure("scripted acquisition failure.")
        behaviour = self.behaviours[index] if index < len(self.behaviours) else RESPOND
        if behaviour == INTERRUPT:
            raise KeyboardInterrupt
        content = _completion_content(
            request,
            foreign_quote=self.foreign_relationship_quote,
            card_content=self.card_content,
        )
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
    sources: EnrichmentSources | None = None,
    observer: Callable[[EnrichmentProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    run_id: str | None = RUN_ID,
) -> EnrichmentRunResult:
    """Run one analysis against a caller-owned work root."""
    return run_set_enrichment(
        sources=_sources() if sources is None else sources,
        complete=completion,
        work_store=_store(work_root),
        model_config=_model_config(),
        run_id=run_id,
        observer=observer,
        is_cancelled=is_cancelled,
    )


def _batch_completion(
    *,
    behaviours: Sequence[str] = (),
    foreign_relationship_quote: str | None = None,
) -> _FakeCompletion:
    """Return a completion that answers the batch fixture's own card capabilities."""
    return _FakeCompletion(
        behaviours=behaviours,
        foreign_relationship_quote=foreign_relationship_quote,
        card_content=_batch_capability_content,
    )


def _run_residual(
    monkeypatch: pytest.MonkeyPatch,
    *,
    work_root: Path,
    completion: Completion | None = None,
    observer: Callable[[EnrichmentProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> EnrichmentRunResult:
    """Run the batch fixture, whose residual pairs only the undeclared-mechanism rewrite creates."""
    monkeypatch.setattr(
        set_enrichment_module,
        "construct_candidate_packages",
        _undeclared_construction(RESIDUAL_MECHANISM),
    )
    return _run(
        work_root=work_root,
        completion=_batch_completion() if completion is None else completion,
        sources=_batch_sources(),
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
    # Every constructed pair is terminal before the last phase starts, so it never waits for a
    # validation batch and the relationship phase reports it already completed.
    assert [event.relationships_completed for event in entries] == [0, 0, 0, 3]

    guide_events = [event for event in events if event.phase is EnrichmentPhase.GUIDES]
    card_events = [event for event in events if event.phase is EnrichmentPhase.CARD_CAPABILITIES]
    candidate_events = [event for event in events if event.phase is EnrichmentPhase.CANDIDATES]
    relationship_events = [
        event for event in events if event.phase is EnrichmentPhase.RELATIONSHIPS
    ]
    assert [event.guides_completed for event in guide_events] == [0, 1]
    assert [event.cards_completed for event in card_events] == [0, 1, 2, 3, 4]
    assert [
        (event.relationships_total, event.relationships_completed) for event in candidate_events
    ] == [(0, 0), (3, 3)]
    assert [event.relationships_completed for event in relationship_events] == [3, 3]
    assert len(events) == 11
    assert events[-1] == result.progress
    assert result.progress.guides_completed == 1
    assert result.progress.cards_completed == 4
    assert result.progress.relationships_completed == 3
    assert result.relationship_results == ()


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
    assert completion.calls_for(RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID) == []
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


def test_card_v2_resume_reexecutes_cards_and_reuses_guide_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the v2 migration re-pays only cards."""
    work_root = tmp_path / "work"
    store = _store(work_root)
    monkeypatch.setattr(
        set_enrichment_module,
        "build_card_capability_extraction_request",
        _legacy_card_request,
    )
    legacy_completion = _FakeCompletion()
    first = _run(work_root=work_root, completion=legacy_completion)

    assert first.outcome is EnrichmentOutcome.COMPLETE
    assert legacy_completion.calls_for(CARD_CAPABILITY_EXTRACTION_PROMPT_ID) == []
    assert [
        subject for _, subject in legacy_completion.calls_for(LEGACY_CARD_CAPABILITY_PROMPT_ID)
    ] == [str(card_id) for card_id in ELIGIBLE_CARD_IDS]
    legacy_requests = [
        request
        for request in legacy_completion.requests
        if request.prompt_id == LEGACY_CARD_CAPABILITY_PROMPT_ID
    ]
    assert len(legacy_requests) == len(ELIGIBLE_CARD_IDS)
    assert all(
        request.contract_version == LEGACY_CARD_CAPABILITY_CONTRACT_VERSION
        and request.response_schema_id == LEGACY_CARD_CAPABILITY_RESPONSE_SCHEMA_ID
        and request.response_schema_name == LEGACY_CARD_CAPABILITY_SCHEMA_NAME
        and _prompt_payload(request)["contract_version"] == LEGACY_CARD_CAPABILITY_CONTRACT_VERSION
        for request in legacy_requests
    )
    for card_id in ELIGIBLE_CARD_IDS:
        assert _legacy_card_identity(card_id) != _card_identity(card_id)
        assert store.lookup(identity=_legacy_card_identity(card_id)).state is WorkState.COMPLETED

    monkeypatch.setattr(
        set_enrichment_module,
        "build_card_capability_extraction_request",
        build_card_capability_extraction_request,
    )
    resumed_completion = _FakeCompletion()
    resumed = _run(work_root=work_root, completion=resumed_completion)

    assert resumed.outcome is EnrichmentOutcome.COMPLETE
    assert resumed.card_results == first.card_results
    assert resumed.candidate_packages == first.candidate_packages
    assert resumed_completion.calls_for(GUIDE_EXTRACTION_PROMPT_ID) == []
    assert resumed_completion.calls_for(RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID) == []
    assert [
        subject for _, subject in resumed_completion.calls_for(CARD_CAPABILITY_EXTRACTION_PROMPT_ID)
    ] == [str(card_id) for card_id in ELIGIBLE_CARD_IDS]
    assert len(resumed_completion.calls) == len(ELIGIBLE_CARD_IDS)
    assert resumed.progress.accounting.executed_work == len(ELIGIBLE_CARD_IDS)
    assert resumed.progress.accounting.reused_work == WORKED_CALLS - len(ELIGIBLE_CARD_IDS)
    for card_id in ELIGIBLE_CARD_IDS:
        assert store.lookup(identity=_card_identity(card_id)).state is WorkState.COMPLETED


def test_completed_malformed_card_result_reparses_its_paid_response_without_a_request(
    tmp_path: Path,
) -> None:
    """Prove a stored all-or-nothing card result recovers offline on resume."""
    work_root = tmp_path / "work"
    content = _contract_violating_card_content(MULTIFACE_CARD_ID)
    _run_with_card_content(work_root, card_id=MULTIFACE_CARD_ID, content=content)
    _malform_stored_card_result(work_root, card_id=MULTIFACE_CARD_ID)
    store = _store(work_root)
    stored = store.lookup(identity=_card_identity(MULTIFACE_CARD_ID))
    assert stored.state is WorkState.COMPLETED
    assert stored.result is not None
    assert stored.result.outcome is ExtractionOutcome.MALFORMED
    before = _snapshot(work_root)

    completion = _FakeCompletion()
    resumed = _run(work_root=work_root, completion=completion)

    assert resumed.outcome is EnrichmentOutcome.COMPLETE
    assert completion.calls == []
    assert resumed.progress.accounting.reused_work == WORKED_CALLS
    assert resumed.progress.accounting.executed_work == 0
    cards = dict(zip(resumed.card_ids, resumed.card_results, strict=True))
    recovered = cards[MULTIFACE_CARD_ID]
    assert recovered == parse_card_capability_extraction_response(
        content=content,
        sources=_sources(),
        card_id=MULTIFACE_CARD_ID,
        run_id=_durable_run_id(MULTIFACE_CARD_ID),
    )
    assert recovered.outcome is ExtractionOutcome.SUCCESS
    assert recovered.malformed_reason is None
    assert [capability.finding_id for capability in recovered.uncertain_capabilities] == [
        MULTIFACE_FRONT_FINDING_ID
    ]
    assert [finding.finding_id for finding in recovered.rejected_capabilities] == [
        f"{MULTIFACE_CARD_ID}-rejected-1"
    ]
    # The recovery costs no durable byte: the store keeps the paid response and the artifact the
    # strict parser wrote, and every resume recomputes the same recovery from them.
    assert _snapshot(work_root) == before


def test_completed_card_result_recovers_a_silently_omitted_candidate_without_a_request(
    tmp_path: Path,
) -> None:
    """Prove a successful historical result cannot hide a candidate in its paid response."""
    work_root = tmp_path / "work"
    content = _capability_content(MULTIFACE_CARD_ID)
    original = _run_with_card_content(
        work_root,
        card_id=MULTIFACE_CARD_ID,
        content=content,
    )
    original_card = dict(zip(original.card_ids, original.card_results, strict=True))[
        MULTIFACE_CARD_ID
    ]
    omitted_id = original_card.uncertain_capabilities[-1].finding_id
    _drop_stored_card_finding(
        work_root,
        card_id=MULTIFACE_CARD_ID,
        finding_id=omitted_id,
    )
    before = _snapshot(work_root)

    completion = _FakeCompletion()
    resumed = _run(work_root=work_root, completion=completion)

    assert completion.calls == []
    recovered = dict(zip(resumed.card_ids, resumed.card_results, strict=True))[
        MULTIFACE_CARD_ID
    ]
    assert [item.finding_id for item in recovered.uncertain_capabilities] == [
        item.finding_id for item in original_card.uncertain_capabilities
    ]
    assert {item.run_id for item in recovered.uncertain_capabilities} == {
        _durable_run_id(MULTIFACE_CARD_ID)
    }
    assert resumed.progress.accounting.reused_work == WORKED_CALLS
    assert resumed.progress.accounting.executed_work == 0
    assert _snapshot(work_root) == before


def test_completed_card_result_keeps_stored_records_when_reparse_only_changes_run_ids(
    tmp_path: Path,
) -> None:
    """Prove ordinary resumes preserve the original capability provenance."""
    work_root = tmp_path / "work"
    original = _run(work_root=work_root, completion=_FakeCompletion())
    before = _snapshot(work_root)

    resumed = _run(
        work_root=work_root,
        completion=_FakeCompletion(),
        run_id="later-invocation",
    )

    assert resumed.card_results == original.card_results
    assert _snapshot(work_root) == before


def test_recovered_malformed_card_result_ignores_the_invoking_run_id(tmp_path: Path) -> None:
    """Prove local recovery of a paid response reproduces the same records on every resume.

    A default resume mints a fresh invocation run id, so a recovery that stamped it would publish
    different capability records for the same durable bytes and change every downstream artifact
    derived from them.
    """
    work_root = tmp_path / "work"
    content = _contract_violating_card_content(MULTIFACE_CARD_ID)
    _run_with_card_content(work_root, card_id=MULTIFACE_CARD_ID, content=content)
    _malform_stored_card_result(work_root, card_id=MULTIFACE_CARD_ID)
    before = _snapshot(work_root)

    resumed_runs = [
        _run(work_root=work_root, completion=_FakeCompletion(), run_id=run_id)
        for run_id in (None, "resumed-invocation")
    ]

    recovered = [
        dict(zip(result.card_ids, result.card_results, strict=True))[MULTIFACE_CARD_ID]
        for result in resumed_runs
    ]
    assert all(item.outcome is ExtractionOutcome.SUCCESS for item in recovered)
    assert recovered[0].to_json() == recovered[1].to_json()
    assert recovered[0] == recovered[1]
    assert recovered[0] == parse_card_capability_extraction_response(
        content=content,
        sources=_sources(),
        card_id=MULTIFACE_CARD_ID,
        run_id=_durable_run_id(MULTIFACE_CARD_ID),
    )
    assert {
        capability.run_id
        for capability in (*recovered[0].uncertain_capabilities, *recovered[0].accepted_capabilities)
    } == {_durable_run_id(MULTIFACE_CARD_ID)}
    assert {finding.run_id for finding in recovered[0].rejected_capabilities} == {
        _durable_run_id(MULTIFACE_CARD_ID)
    }
    assert all(
        result.progress.accounting.reused_work == WORKED_CALLS for result in resumed_runs
    )
    assert all(result.progress.accounting.executed_work == 0 for result in resumed_runs)
    # The recovery is a pure function of the durable store, so it never rewrites it.
    assert _snapshot(work_root) == before


def test_completed_malformed_card_result_stays_malformed_without_a_request(
    tmp_path: Path,
) -> None:
    """Prove a response the tolerant parser still rejects keeps its stored result."""
    work_root = tmp_path / "work"
    content = _duplicate_finding_id_card_content(MULTIFACE_CARD_ID)
    _run_with_card_content(work_root, card_id=MULTIFACE_CARD_ID, content=content)
    _malform_stored_card_result(work_root, card_id=MULTIFACE_CARD_ID)
    store = _store(work_root)
    stored = store.lookup(identity=_card_identity(MULTIFACE_CARD_ID))
    assert stored.state is WorkState.COMPLETED
    assert stored.result is not None
    assert stored.result.outcome is ExtractionOutcome.MALFORMED
    before = _snapshot(work_root)

    completion = _FakeCompletion()
    resumed = _run(work_root=work_root, completion=completion)

    assert resumed.outcome is EnrichmentOutcome.COMPLETE
    assert completion.calls == []
    assert resumed.progress.accounting.reused_work == WORKED_CALLS
    assert resumed.progress.accounting.executed_work == 0
    cards = dict(zip(resumed.card_ids, resumed.card_results, strict=True))
    kept = cards[MULTIFACE_CARD_ID]
    assert kept == stored.result
    assert kept.outcome is ExtractionOutcome.MALFORMED
    assert kept.malformed_reason == CARD_MALFORMED_REASON
    assert _snapshot(work_root) == before


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


def test_unvalidated_relationship_response_resumes_to_a_malformed_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove a durable batch response without participant evidence resumes as malformed."""
    work_root = tmp_path / "work"
    store = _store(work_root)
    identity = _residual_batch_identity(0)
    store.record_attempt(identity=identity)
    store.record_response(
        identity=identity,
        response=OpenRouterResponse(
            content=_evidence_less_relationship_content(len(_residual_batch(0))),
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

    completion = _batch_completion()
    result = _run_residual(monkeypatch, work_root=work_root, completion=completion)

    assert result.outcome is EnrichmentOutcome.COMPLETE
    assert store.lookup(identity=identity).state is WorkState.COMPLETED
    # The fake labels batches by position, so the one remaining request is identified by its pairs.
    assert len(completion.calls_for(RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID)) == 1
    (remaining_request,) = [
        request
        for request in completion.requests
        if request.prompt_id == RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID
    ]
    assert [
        (pair["mechanism"], pair["source"]["card_id"], pair["target"]["card_id"])
        for pair in _prompt_payload(remaining_request)["pairs"]
    ] == [
        (package.mechanism, package.source.card_id, package.target.card_id)
        for package in _residual_batch(1)
    ]
    assert len(_prompt_payload(remaining_request)["pairs"]) == len(_residual_batch(1))
    resolutions = result.candidate_resolutions
    assert resolutions is not None
    malformed = result.relationship_results[:RELATIONSHIP_BATCH_SIZE]
    assert len(malformed) == RELATIONSHIP_BATCH_SIZE
    assert all(item.outcome is ExtractionOutcome.MALFORMED for item in malformed)
    assert {item.malformed_reason for item in malformed} == {RELATIONSHIP_MALFORMED_REASON}
    assert all(item.relationship is None and item.rejected is None for item in malformed)
    assert [
        item.relationship.identity
        for item in result.relationship_results[RELATIONSHIP_BATCH_SIZE:]
        if item.relationship is not None
    ] == [
        package.identity
        for package in resolutions.model_packages[RELATIONSHIP_BATCH_SIZE:]
    ]


def test_relationship_calls_cover_every_residual_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = _batch_completion()
    result = _run_residual(monkeypatch, work_root=tmp_path / "work", completion=completion)

    resolutions = result.candidate_resolutions
    assert resolutions is not None
    batches = partition_relationship_batches(resolutions.model_packages)
    assert len(batches) == BATCH_BATCHES
    assert [
        subject for _, subject in completion.calls_for(RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID)
    ] == [relationship_batch_subject_id(index) for index in range(len(batches))]

    relationship_requests = [
        request
        for request in completion.requests
        if request.prompt_id == RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID
    ]
    assert len(relationship_requests) == len(batches)
    for request, batch in zip(relationship_requests, batches):
        prompt = _prompt_payload(request)
        assert [pair["index"] for pair in prompt["pairs"]] == list(range(len(batch)))
        for pair, package in zip(prompt["pairs"], batch):
            assert pair["mechanism"] == package.mechanism == UNDECLARED_MECHANISM
            assert pair["source"]["card_id"] == package.source.card_id
            assert pair["source"]["finding_id"] == package.source.finding_id
            assert pair["target"]["card_id"] == package.target.card_id
            assert pair["target"]["finding_id"] == package.target.finding_id


def test_accepted_relationship_preserves_candidate_capability_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_residual(monkeypatch, work_root=tmp_path / "work", completion=_batch_completion())

    resolutions = result.candidate_resolutions
    assert resolutions is not None
    packages = resolutions.model_packages
    assert len(result.relationship_results) == len(packages) == BATCH_RESIDUAL_PAIRS
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
        assert relationship.prerequisite_projection is None
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

    token_relationships = [
        item.relationship
        for item in result.relationship_results
        if item.relationship is not None
        and item.relationship.source.card_id == BATCH_STATED_MAKER_CARD_ID
    ]
    assert len(token_relationships) == len(BATCH_PAYOFF_CARD_IDS) + 1
    for relationship in token_relationships:
        assert relationship is not None
        assert relationship.source.action is CapabilityAction.CREATE
        assert relationship.source.zone is CapabilityZone.BATTLEFIELD
        assert relationship.source.destination_zone is CapabilityZone.BATTLEFIELD
        assert relationship.source.qualifier == CapabilityQualifier(
            card_types=(CapabilityCardType.CREATURE,),
            token_restriction=CapabilityTokenRestriction.TOKEN,
            subtype=None,
            mana_value=None,
        )

    nontoken_relationships = [
        item.relationship
        for item in result.relationship_results
        if item.relationship is not None
        and item.relationship.target.card_id == BATCH_NONTOKEN_PAYOFF_CARD_ID
    ]
    assert len(nontoken_relationships) == len(BATCH_MAKER_CARD_IDS)
    for relationship in nontoken_relationships:
        assert relationship is not None
        assert relationship.target.qualifier.card_types == (CapabilityCardType.CREATURE,)
        assert (
            relationship.target.qualifier.token_restriction
            is CapabilityTokenRestriction.NONTOKEN
        )

    full_payoff = next(
        item.relationship
        for item in result.relationship_results
        if item.relationship is not None
        and item.relationship.target.card_id == BATCH_PAYOFF_CARD_ID
    )
    assert full_payoff is not None
    assert full_payoff.target.quantity == CapabilityQuantity(
        value=2,
        relation=QuantityRelation.EXACTLY,
    )
    assert full_payoff.target.timing == WIDE_TIMING
    assert full_payoff.target.source_zone is CapabilityZone.BATTLEFIELD
    assert full_payoff.target.destination_zone is CapabilityZone.BATTLEFIELD
    assert full_payoff.target.prerequisites == (
        CapabilityPrerequisite(
            kind=PrerequisiteKind.THRESHOLD,
            quantity=CapabilityQuantity(value=3, relation=QuantityRelation.AT_LEAST),
            timing=WIDE_PREREQUISITE_TIMING,
            source_zone=CapabilityZone.BATTLEFIELD,
            destination_zone=CapabilityZone.GRAVEYARD,
            evidence=OracleEvidence(
                card_id=BATCH_PAYOFF_CARD_ID,
                face_index=None,
                quote=BATCH_PAYOFF_QUOTE,
            ),
        ),
    )
    assert full_payoff.target.evidence == (
        OracleEvidence(card_id=BATCH_PAYOFF_CARD_ID, face_index=None, quote=BATCH_PAYOFF_QUOTE),
    )


def test_foreign_relationship_evidence_becomes_a_rejected_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = _batch_completion(foreign_relationship_quote=UNRELATED_QUOTE)
    result = _run_residual(monkeypatch, work_root=tmp_path / "work", completion=completion)

    assert result.outcome is EnrichmentOutcome.COMPLETE
    assert len(result.relationship_results) == BATCH_RESIDUAL_PAIRS
    assert all(item.relationship is None for item in result.relationship_results)
    assert {item.rejected.reason for item in result.relationship_results if item.rejected} == {
        RELATIONSHIP_EVIDENCE_QUOTE_REASON
    }
    assert result.progress.valid_count == BATCH_LOCAL_ACCEPTED_PAIRS
    assert result.progress.rejected_count == BATCH_LOCAL_REJECTED_PAIRS + BATCH_RESIDUAL_PAIRS


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
    # The single candidate phase already resolved every pair, so the relationship phase never
    # reports a partially validated run.
    assert [event.relationships_percent for event in relationship_events] == [100.0, 100.0]
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
    assert accounting.running_cost_usd == "0.008"
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


def test_local_resolution_leaves_only_residual_pairs_to_validation_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[EnrichmentProgress] = []
    completion = _batch_completion()
    result = _run_residual(
        monkeypatch,
        work_root=tmp_path / "work",
        completion=completion,
        observer=events.append,
    )

    assert result.outcome is EnrichmentOutcome.COMPLETE
    assert result.candidate_packages is not None
    resolutions = result.candidate_resolutions
    assert resolutions is not None
    assert [item.identity for item in resolutions.resolutions] == [
        package.identity for package in result.candidate_packages.packages
    ]
    local = tuple(
        item for item in resolutions.resolutions if item.basis is CandidateResolutionBasis.LOCAL
    )
    assert len(local) == BATCH_LOCAL_PAIRS
    assert len(resolutions.local_accepted) == BATCH_LOCAL_ACCEPTED_PAIRS
    assert len(resolutions.local_rejected) == BATCH_LOCAL_REJECTED_PAIRS
    assert len(resolutions.model_packages) == BATCH_RESIDUAL_PAIRS
    assert {
        item.reason
        for item in local
        if item.verdict is CandidateResolutionVerdict.ACCEPTED
    } == {LOCAL_PROVE_REASON}
    assert {
        item.reason
        for item in local
        if item.verdict is CandidateResolutionVerdict.REJECTED
    } == {LOCAL_TOKEN_RESTRICTION_CONFLICT_REASON}

    # Only the residual pairs reach the completion callable: every validation request quotes the
    # residual packages alone, in their canonical order.
    local_identities = {item.identity for item in local}
    relationship_requests = [
        request
        for request in completion.requests
        if request.prompt_id == RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID
    ]
    assert len(relationship_requests) == BATCH_BATCHES
    prompted = [
        (
            pair["mechanism"],
            pair["source"]["card_id"],
            pair["source"]["finding_id"],
            pair["target"]["card_id"],
            pair["target"]["finding_id"],
        )
        for request in relationship_requests
        for pair in _prompt_payload(request)["pairs"]
    ]
    assert prompted == [package.identity for package in resolutions.model_packages]
    assert not local_identities & set(prompted)

    assert [
        item.relationship.identity
        for item in result.relationship_results
        if item.relationship is not None
    ] == [package.identity for package in resolutions.model_packages]

    candidate_events = [event for event in events if event.phase is EnrichmentPhase.CANDIDATES]
    assert [
        (event.relationships_total, event.relationships_completed) for event in candidate_events
    ] == [(0, 0), (BATCH_TOTAL_PAIRS, BATCH_LOCAL_PAIRS)]
    relationship_events = [
        event for event in events if event.phase is EnrichmentPhase.RELATIONSHIPS
    ]
    assert [event.relationships_completed for event in relationship_events] == [
        BATCH_LOCAL_PAIRS,
        BATCH_LOCAL_PAIRS + RELATIONSHIP_BATCH_SIZE,
        BATCH_TOTAL_PAIRS,
        BATCH_TOTAL_PAIRS,
    ]
    assert result.progress.relationships_total == BATCH_TOTAL_PAIRS
    assert result.progress.relationships_completed == BATCH_TOTAL_PAIRS
    # Every local acceptance plus every accepted residual verdict of the fake completion.
    assert result.progress.valid_count == BATCH_LOCAL_ACCEPTED_PAIRS + BATCH_RESIDUAL_PAIRS
    assert result.progress.rejected_count == BATCH_LOCAL_REJECTED_PAIRS

    # A local decision is not work: only the guide, the cards and the residual batches count.
    accounting = result.progress.accounting
    assert len(completion.calls) == BATCH_WORKED_CALLS
    assert accounting.executed_work == BATCH_WORKED_CALLS
    assert accounting.reused_work == 0
    assert accounting.projected_final_cost_usd == accounting.running_cost_usd


def test_fully_local_resolution_requests_no_relationship_batch(tmp_path: Path) -> None:
    completion = _FakeCompletion(card_content=_local_capability_content)
    result = _run(
        work_root=tmp_path / "work",
        completion=completion,
        sources=_local_sources(),
    )

    assert result.outcome is EnrichmentOutcome.COMPLETE
    resolutions = result.candidate_resolutions
    assert resolutions is not None
    assert len(resolutions.resolutions) == 2
    assert len(resolutions.local_accepted) == 1
    assert len(resolutions.local_rejected) == 1
    assert resolutions.model_packages == ()
    assert result.relationship_results == ()
    assert completion.calls_for(RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID) == []
    assert len(completion.calls) == 4
    assert result.progress.relationships_total == 2
    assert result.progress.relationships_completed == 2
    assert result.progress.relationships_percent == 100.0
    assert result.progress.valid_count == 1
    assert result.progress.rejected_count == 1
    assert result.progress.accounting.executed_work == 4


def test_residual_batches_keep_their_exact_size_and_canonical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = _batch_completion()
    result = _run_residual(monkeypatch, work_root=tmp_path / "work", completion=completion)

    assert result.outcome is EnrichmentOutcome.COMPLETE
    resolutions = result.candidate_resolutions
    assert resolutions is not None
    assert len(resolutions.resolutions) == BATCH_TOTAL_PAIRS
    assert len(resolutions.model_packages) == BATCH_RESIDUAL_PAIRS

    batches = partition_relationship_batches(resolutions.model_packages)
    assert len(batches) == BATCH_BATCHES
    assert [len(batch) for batch in batches] == [
        RELATIONSHIP_BATCH_SIZE,
        BATCH_RESIDUAL_PAIRS - RELATIONSHIP_BATCH_SIZE,
    ]
    assert [package.identity for batch in batches for package in batch] == [
        package.identity for package in resolutions.model_packages
    ]
    assert [
        subject for _, subject in completion.calls_for(RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID)
    ] == [relationship_batch_subject_id(index) for index in range(len(batches))]

    relationship_requests = [
        request
        for request in completion.requests
        if request.prompt_id == RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID
    ]
    assert len(relationship_requests) == len(batches)
    for request, batch in zip(relationship_requests, batches):
        prompt = _prompt_payload(request)
        assert [pair["index"] for pair in prompt["pairs"]] == list(range(len(batch)))
        assert [
            (
                pair["mechanism"],
                pair["source"]["card_id"],
                pair["source"]["finding_id"],
                pair["target"]["card_id"],
                pair["target"]["finding_id"],
            )
            for pair in prompt["pairs"]
        ] == [package.identity for package in batch]

    assert len(result.relationship_results) == BATCH_RESIDUAL_PAIRS
    assert [
        item.relationship.identity
        for item in result.relationship_results
        if item.relationship is not None
    ] == [package.identity for package in resolutions.model_packages]


def test_residual_batches_resume_from_durable_results_without_replacement_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove a resumed run reuses paid residual batches and revalidates pre-v2 verdict bytes."""
    work_root = tmp_path / "work"
    store = _store(work_root)
    first = _run_residual(monkeypatch, work_root=work_root, completion=_batch_completion())

    assert first.outcome is EnrichmentOutcome.COMPLETE
    assert len(first.relationship_results) == BATCH_RESIDUAL_PAIRS
    paid = store.lookup(identity=_residual_batch_identity(1))
    assert paid.state is WorkState.COMPLETED
    assert paid.legacy_result is False

    _legacyize_batch_result(work_root, batch_index=1)
    legacy_record = store.lookup(identity=_residual_batch_identity(1))
    assert legacy_record.state is WorkState.UNVALIDATED
    assert legacy_record.legacy_result is True

    completion = _batch_completion()
    resumed = _run_residual(monkeypatch, work_root=work_root, completion=completion)

    assert resumed.outcome is EnrichmentOutcome.COMPLETE
    assert completion.calls == []
    assert resumed.relationship_results == first.relationship_results
    assert resumed.progress.accounting.reused_work == BATCH_WORKED_CALLS
    assert resumed.progress.accounting.executed_work == 0
    record = store.lookup(identity=_residual_batch_identity(1))
    assert record.state is WorkState.COMPLETED
    assert record.legacy_result is False
    assert record.result is not None
    assert record.result.verdicts == resumed.relationship_results[RELATIONSHIP_BATCH_SIZE:]


def test_cancellation_at_the_relationship_boundary_keeps_local_resolutions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = tmp_path / "work"
    state = {"cancel": False}

    def observer(event: EnrichmentProgress) -> None:
        if (
            event.phase is EnrichmentPhase.RELATIONSHIPS
            and event.relationships_completed == BATCH_LOCAL_PAIRS + RELATIONSHIP_BATCH_SIZE
        ):
            state["cancel"] = True

    cancelled = _run_residual(
        monkeypatch,
        work_root=work_root,
        completion=_batch_completion(),
        observer=observer,
        is_cancelled=lambda: state["cancel"],
    )

    assert cancelled.outcome is EnrichmentOutcome.CANCELLED
    assert cancelled.complete is False
    resolutions = cancelled.candidate_resolutions
    assert resolutions is not None
    assert cancelled.candidate_packages is not None
    assert [item.identity for item in resolutions.resolutions] == [
        package.identity for package in cancelled.candidate_packages.packages
    ]
    assert len(resolutions.local_accepted) == BATCH_LOCAL_ACCEPTED_PAIRS
    assert len(resolutions.local_rejected) == BATCH_LOCAL_REJECTED_PAIRS
    assert [
        item.relationship.identity
        for item in cancelled.relationship_results
        if item.relationship is not None
    ] == [
        package.identity
        for package in resolutions.model_packages[:RELATIONSHIP_BATCH_SIZE]
    ]
    assert cancelled.progress.relationships_total == BATCH_TOTAL_PAIRS
    assert (
        cancelled.progress.relationships_completed
        == BATCH_LOCAL_PAIRS + RELATIONSHIP_BATCH_SIZE
    )
    assert cancelled.progress.valid_count == BATCH_LOCAL_ACCEPTED_PAIRS + RELATIONSHIP_BATCH_SIZE
    assert cancelled.progress.rejected_count == BATCH_LOCAL_REJECTED_PAIRS

    completion = _batch_completion()
    resumed = _run_residual(monkeypatch, work_root=work_root, completion=completion)

    assert resumed.outcome is EnrichmentOutcome.COMPLETE
    assert resumed.candidate_resolutions is not None
    assert resumed.candidate_resolutions.to_json() == resolutions.to_json()
    assert len(resumed.relationship_results) == BATCH_RESIDUAL_PAIRS
    # The fake labels batches by position, so the resumed request is identified by its pairs.
    assert len(completion.calls) == 1
    (resumed_request,) = [
        request
        for request in completion.requests
        if request.prompt_id == RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID
    ]
    assert [
        (pair["mechanism"], pair["source"]["card_id"], pair["target"]["card_id"])
        for pair in _prompt_payload(resumed_request)["pairs"]
    ] == [
        (package.mechanism, package.source.card_id, package.target.card_id)
        for package in _residual_batch(1)
    ]
    assert (
        _store(work_root).lookup(identity=_residual_batch_identity(1)).state
        is WorkState.COMPLETED
    )
    assert resumed.progress.accounting.reused_work == BATCH_WORKED_CALLS - 1
    assert resumed.progress.accounting.executed_work == 1
