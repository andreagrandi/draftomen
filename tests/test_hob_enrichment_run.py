"""Behavior tests for reviewed HOB mechanic matching against card Oracle evidence."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from draftomen.carddb import CardInfo
from draftomen.semantic_capability_records import (
    CapabilityAction,
    CapabilityCardType,
    CapabilityQualifier,
    CapabilityTokenRestriction,
    CapabilityZone,
    CardCapability,
)
from draftomen.semantic_enrichment import EnrichmentSources, set_source_sha256
from draftomen.semantic_enrichment_records import (
    ArtifactReview,
    FindingReview,
    FindingStatus,
    OracleEvidence,
    RejectedFinding,
)
from draftomen.semantic_roles import Role
import draftomen.set_enrichment as set_enrichment_module
from draftomen.set_enrichment import (
    EnrichmentAccounting,
    EnrichmentOutcome,
    EnrichmentPhase,
    EnrichmentProgress,
    EnrichmentRunResult,
)
from draftomen.set_enrichment_candidates import (
    CANDIDATE_REASON,
    LOCAL_CONFLICT_REASON_TEMPLATE,
    LOCAL_PROVE_REASON,
    ROLE_COMPATIBILITY_RULES,
    CandidatePackage,
    construct_candidate_packages,
    resolve_candidate_packages,
)
from draftomen.set_enrichment_extraction import (
    RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID,
    CardCapabilityExtractionResult,
    ExtractionOutcome,
    RelationshipValidationResult,
    ValidatedRelationship,
    relationship_subject_id,
)
from draftomen.set_enrichment_work import WorkKind, WorkModelConfig
from scripts.hob_enrichment_run import (
    MAXIMUM_RELATIONSHIP_VALIDATION_BATCHES,
    MINIMUM_LOCAL_RESOLUTION_PERCENT,
    MINIMUM_OVERLAP_CHARS,
    _STOP_BEFORE_RELATIONSHIP_VALIDATION_ENV,
    _build_report,
    _oracle_evidence_matches,
    _relationship_stop_requested,
    _render_markdown,
)


RUN_ID = "run-1"
STORIED_CARD_ID = 103382
STORIED_QUOTE = (
    "Storied (If you control three or more artifacts, legendaries, and/or Sagas, you have an "
    "enduring story for the rest of the game.)"
)
ADVENTURES_CARD_ID = 103397
ADVENTURES_FACE_QUOTE = (
    "Create X 2/2 red Dwarf creature tokens. (Then exile this card. You may cast the "
    "enchantment later from exile.)"
)
REVIEW_REASON = "capability requires semantic review beyond exact-source validation."
UNRESTRICTED_QUALIFIER = CapabilityQualifier(
    card_types=(),
    token_restriction=CapabilityTokenRestriction.UNRESTRICTED,
    subtype=None,
    mana_value=None,
)


def _entry(
    *,
    card_id: int,
    quote: str,
    face_index: int | None = None,
) -> dict[str, object]:
    """Build one reviewed mechanic expectation around exact Oracle evidence."""
    return {
        "name": "Storied",
        "guide_quote": "get you storied since so many of them are legendary.",
        "oracle_card_id": card_id,
        "oracle_face_index": face_index,
        "oracle_quote": quote,
        "required": True,
    }


def _capability(
    *,
    card_id: int,
    quote: str,
    face_index: int | None = None,
    role: Role = Role.MODIFIED,
    status: FindingStatus = FindingStatus.ACCEPTED,
) -> CardCapability:
    """Build one real capability record around a single Oracle evidence quote."""
    return CardCapability(
        finding_id=f"finding-{card_id}-{face_index}",
        card_id=card_id,
        card_name="Fíli the Pathfinder",
        face_index=face_index,
        face_name=None,
        role=role,
        action=CapabilityAction.OTHER,
        zone=CapabilityZone.BATTLEFIELD,
        qualifier=UNRESTRICTED_QUALIFIER,
        quantity=None,
        timing=None,
        source_zone=None,
        destination_zone=None,
        prerequisites=(),
        evidence=(OracleEvidence(card_id=card_id, face_index=face_index, quote=quote),),
        review=FindingReview(
            status=status,
            reason=None if status is FindingStatus.ACCEPTED else REVIEW_REASON,
        ),
        run_id=RUN_ID,
    )


def test_exact_expected_span_on_pinned_card_matches() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    capability = _capability(
        card_id=STORIED_CARD_ID,
        quote=STORIED_QUOTE,
        role=Role.ARTIFACT_PAYOFF,
        status=FindingStatus.UNCERTAIN,
    )

    matches = _oracle_evidence_matches(entry=entry, capabilities=[capability])

    assert len(matches) == 1
    assert matches[0]["rule"] == "card-capability-quote"
    assert matches[0]["card_id"] == STORIED_CARD_ID
    assert matches[0]["finding_id"] == capability.finding_id
    assert matches[0]["role"] == Role.ARTIFACT_PAYOFF.value
    assert matches[0]["status"] == FindingStatus.UNCERTAIN.value


def test_quote_on_another_card_does_not_match() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    capability = _capability(card_id=103397, quote=STORIED_QUOTE)

    assert _oracle_evidence_matches(entry=entry, capabilities=[capability]) == []


def test_pinned_face_index_requires_the_matching_face() -> None:
    entry = _entry(
        card_id=ADVENTURES_CARD_ID,
        face_index=1,
        quote=ADVENTURES_FACE_QUOTE,
    )
    other_face = _capability(
        card_id=ADVENTURES_CARD_ID,
        face_index=0,
        quote=ADVENTURES_FACE_QUOTE,
    )
    pinned_face = _capability(
        card_id=ADVENTURES_CARD_ID,
        face_index=1,
        quote=ADVENTURES_FACE_QUOTE,
    )

    assert _oracle_evidence_matches(entry=entry, capabilities=[other_face]) == []
    assert len(_oracle_evidence_matches(entry=entry, capabilities=[pinned_face])) == 1


def test_larger_ability_quote_containing_the_expected_span_matches() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    capability = _capability(
        card_id=STORIED_CARD_ID,
        quote=f"Fíli the Pathfinder — {STORIED_QUOTE}",
    )

    assert len(_oracle_evidence_matches(entry=entry, capabilities=[capability])) == 1


def test_sub_span_matches_only_above_the_overlap_floor() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    long_sub_span = STORIED_QUOTE[: MINIMUM_OVERLAP_CHARS + 10]
    short_sub_span = STORIED_QUOTE[: MINIMUM_OVERLAP_CHARS - 10]

    assert len(long_sub_span) > MINIMUM_OVERLAP_CHARS
    assert len(short_sub_span) < MINIMUM_OVERLAP_CHARS
    assert (
        len(
            _oracle_evidence_matches(
                entry=entry,
                capabilities=[_capability(card_id=STORIED_CARD_ID, quote=long_sub_span)],
            )
        )
        == 1
    )
    assert (
        _oracle_evidence_matches(
            entry=entry,
            capabilities=[_capability(card_id=STORIED_CARD_ID, quote=short_sub_span)],
        )
        == []
    )


def test_capability_on_the_pinned_card_quoting_another_ability_does_not_match() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    capability = _capability(
        card_id=STORIED_CARD_ID,
        quote="Whenever Fíli or another nontoken Dwarf you control enters, create a 2/2 red Dwarf creature token.",
    )

    assert _oracle_evidence_matches(entry=entry, capabilities=[capability]) == []


# Resolution reporting fixtures: the role-anchored matcher decides every pair the declared role
# rules construct, so a pair that still needs a model validation can only belong to a mechanism no
# declared role rule pairs. No role rule constructs such a pair, so the fixture builds it directly
# on top of the scene's participants.
SET_CODE = "tst"
TOKEN_MAKER_CARD_ID = 401
WIDE_PAYOFF_CARD_ID = 411
NONTOKEN_PAYOFF_CARD_ID = 412
RECURSION_ENABLER_CARD_ID = 501
GRAVEYARD_PAYOFF_CARD_ID = 601

# The only remaining source of model pairs: a mechanism absent from ROLE_COMPATIBILITY_RULES.
UNDECLARED_MECHANISM = "artifact-role-synergy"
UNDECLARED_SOURCE_CARD_ID = 701
UNDECLARED_TARGET_CARD_ID = 991

TOKEN_MAKER_QUOTE = "Create two 1/1 colorless Soldier artifact creature tokens."
WIDE_PAYOFF_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
NONTOKEN_PAYOFF_QUOTE = "Nontoken creatures you control get +1/+1."
RECURSION_QUOTE = "Return target creature card from your graveyard to your hand."
GRAVEYARD_PAYOFF_QUOTE = "This creature gets +1/+1 for each creature card in your graveyard."
UNDECLARED_SOURCE_QUOTE = "Artifacts you control enter the battlefield untapped."
UNDECLARED_TARGET_QUOTE = "Artifacts you control get +1/+0."
TOKEN_RESTRICTION_CONFLICT_REASON = LOCAL_CONFLICT_REASON_TEMPLATE.format(
    field="token_restriction"
)

MODEL_REJECTION_REASON = "the payoff does not reward the created token."
BATCH_BUDGET_MAKER_COUNT = 16
BATCH_BUDGET_PAYOFF_COUNT = 16
BATCH_BUDGET_LOCAL_PAIRS = BATCH_BUDGET_MAKER_COUNT * BATCH_BUDGET_PAYOFF_COUNT
BATCH_BUDGET_RESIDUAL_PAIRS = 61
BATCH_SIZE_RESIDUAL_PAIRS = 21
WIDE_PAYOFF_SPARE_CARD_IDS = (413, 414)


def _role_capability(
    *,
    card_id: int,
    role: Role,
    action: CapabilityAction,
    quote: str,
    qualifier: CapabilityQualifier,
    destination_zone: CapabilityZone | None = None,
) -> CardCapability:
    """Build one structured capability whose stored parameters decide its candidate pairs."""
    return CardCapability(
        finding_id=f"capability-{card_id}",
        card_id=card_id,
        card_name=f"Card {card_id}",
        face_index=None,
        face_name=None,
        role=role,
        action=action,
        zone=CapabilityZone.BATTLEFIELD,
        qualifier=qualifier,
        quantity=None,
        timing=None,
        source_zone=None,
        destination_zone=destination_zone,
        prerequisites=(),
        evidence=(OracleEvidence(card_id=card_id, face_index=None, quote=quote),),
        review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
        run_id=RUN_ID,
    )


def _qualified_qualifier(
    *,
    card_types: tuple[CapabilityCardType, ...] = (CapabilityCardType.CREATURE,),
    token_restriction: CapabilityTokenRestriction = CapabilityTokenRestriction.UNRESTRICTED,
) -> CapabilityQualifier:
    """Build one matcher qualifier around the fixture card type and token restriction."""
    return CapabilityQualifier(
        card_types=card_types,
        token_restriction=token_restriction,
        subtype=None,
        mana_value=None,
    )


def _token_maker(
    card_id: int,
    *,
    action: CapabilityAction = CapabilityAction.CREATE,
    quote: str = TOKEN_MAKER_QUOTE,
) -> CardCapability:
    """Build one token-making enabler the declared token routes decide."""
    return _role_capability(
        card_id=card_id,
        role=Role.TOKEN_MAKER,
        action=action,
        quote=quote,
        destination_zone=CapabilityZone.BATTLEFIELD,
        qualifier=_qualified_qualifier(
            token_restriction=CapabilityTokenRestriction.TOKEN,
        ),
    )


def _go_wide_payoff(
    card_id: int,
    *,
    token_restriction: CapabilityTokenRestriction = CapabilityTokenRestriction.UNRESTRICTED,
    quote: str = WIDE_PAYOFF_QUOTE,
) -> CardCapability:
    """Build one go-wide payoff the structured token signature accepts or conflicts with."""
    return _role_capability(
        card_id=card_id,
        role=Role.GO_WIDE_PAYOFF,
        action=CapabilityAction.COUNT,
        quote=quote,
        qualifier=_qualified_qualifier(token_restriction=token_restriction),
    )


def _recursion_enabler(card_id: int) -> CardCapability:
    """Build one recursion enabler whose declared role proves its graveyard payoff."""
    return _role_capability(
        card_id=card_id,
        role=Role.RECURSION,
        action=CapabilityAction.RETURN,
        quote=RECURSION_QUOTE,
        qualifier=_qualified_qualifier(card_types=()),
    )


def _graveyard_payoff(card_id: int) -> CardCapability:
    """Build one graveyard payoff the recursion enabler pairs with."""
    return _role_capability(
        card_id=card_id,
        role=Role.GRAVEYARD_PAYOFF,
        action=CapabilityAction.COUNT,
        quote=GRAVEYARD_PAYOFF_QUOTE,
        qualifier=_qualified_qualifier(card_types=()),
    )


def _undeclared_source(card_id: int) -> CardCapability:
    """Build one enabler role no declared compatibility rule pairs with any payoff."""
    return _role_capability(
        card_id=card_id,
        role=Role.ARTIFACT_ENABLER,
        action=CapabilityAction.OTHER,
        quote=UNDECLARED_SOURCE_QUOTE,
        qualifier=_qualified_qualifier(card_types=()),
    )


def _undeclared_target(card_id: int) -> CardCapability:
    """Build one payoff role no declared compatibility rule pairs with any enabler."""
    return _role_capability(
        card_id=card_id,
        role=Role.ARTIFACT_PAYOFF,
        action=CapabilityAction.OTHER,
        quote=UNDECLARED_TARGET_QUOTE,
        qualifier=_qualified_qualifier(card_types=()),
    )


def _undeclared_participants(residual_pairs: int) -> tuple[CardCapability, ...]:
    """Return the participants of the undeclared residual pairs one fixture scene needs."""
    if residual_pairs <= 0:
        return ()
    return (
        *(
            _undeclared_source(UNDECLARED_SOURCE_CARD_ID + index)
            for index in range(residual_pairs)
        ),
        _undeclared_target(UNDECLARED_TARGET_CARD_ID),
    )


def _undeclared_packages(residual_pairs: int) -> tuple[CandidatePackage, ...]:
    """Build the pairs of a mechanism no role rule constructs, so only a model settles them."""
    target = _undeclared_target(UNDECLARED_TARGET_CARD_ID)
    return tuple(
        CandidatePackage(
            mechanism=UNDECLARED_MECHANISM,
            source=_undeclared_source(UNDECLARED_SOURCE_CARD_ID + index),
            target=target,
            reason=CANDIDATE_REASON,
        )
        for index in range(residual_pairs)
    )


def _token_scene() -> tuple[CardCapability, ...]:
    """Return one locally accepted token pair and one locally rejected token pair."""
    return (
        _token_maker(TOKEN_MAKER_CARD_ID),
        _go_wide_payoff(WIDE_PAYOFF_CARD_ID),
        _go_wide_payoff(
            NONTOKEN_PAYOFF_CARD_ID,
            token_restriction=CapabilityTokenRestriction.NONTOKEN,
            quote=NONTOKEN_PAYOFF_QUOTE,
        ),
    )


def _mixed_scene() -> tuple[CardCapability, ...]:
    """Return two locally accepted pairs of different mechanisms and one locally rejected pair."""
    return (
        *_token_scene(),
        _recursion_enabler(RECURSION_ENABLER_CARD_ID),
        _graveyard_payoff(GRAVEYARD_PAYOFF_CARD_ID),
    )


def _threshold_scene() -> tuple[CardCapability, ...]:
    """Return four locally decided pairs, so one residual pair lands exactly on the HOB gate."""
    return (
        *_token_scene(),
        *(_go_wide_payoff(card_id) for card_id in WIDE_PAYOFF_SPARE_CARD_IDS),
    )


def _batch_budget_scene() -> tuple[CardCapability, ...]:
    """Return a scene of locally decided pairs that dwarfs the run's residual pairs."""
    return (
        *(_token_maker(1100 + index) for index in range(BATCH_BUDGET_MAKER_COUNT)),
        *(_go_wide_payoff(2100 + index) for index in range(BATCH_BUDGET_PAYOFF_COUNT)),
    )


class _SpendFacts:
    """Expose the spend facts one fixture report records."""

    spent = Decimal("0")
    requests = 0
    unknown_cost_responses = 0


def _source_card(card_id: int, quote: str) -> CardInfo:
    """Build one frozen source card carrying a single fixture ability."""
    return CardInfo(
        grp_id=card_id,
        name=f"Card {card_id}",
        colors=("W",),
        mana_value=2.0,
        rarity="uncommon",
        types=("Creature",),
        oracle_text=quote,
        set_code=SET_CODE,
    )


def _sources(capabilities: Sequence[CardCapability]) -> EnrichmentSources:
    """Build the frozen source context of the fixture capabilities."""
    cards = {
        capability.card_id: _source_card(capability.card_id, capability.evidence[0].quote)
        for capability in capabilities
    }
    return EnrichmentSources(
        set_code=SET_CODE,
        cards=tuple(cards[card_id] for card_id in sorted(cards)),
        guides=(),
    )


def _card_results(
    capabilities: Sequence[CardCapability],
) -> tuple[CardCapabilityExtractionResult, ...]:
    """Group the fixture capabilities into one accepted card extraction per card."""
    grouped: dict[int, list[CardCapability]] = {}
    for capability in capabilities:
        grouped.setdefault(capability.card_id, []).append(capability)
    return tuple(
        CardCapabilityExtractionResult(
            outcome=ExtractionOutcome.SUCCESS,
            accepted_capabilities=tuple(grouped[card_id]),
            uncertain_capabilities=(),
            rejected_capabilities=(),
            malformed_reason=None,
        )
        for card_id in sorted(grouped)
    )


def _accepted_result(
    package: CandidatePackage,
    *,
    claim: str,
) -> RelationshipValidationResult:
    """Build one accepted model verdict bound to its residual candidate."""
    return RelationshipValidationResult(
        outcome=ExtractionOutcome.SUCCESS,
        relationship=ValidatedRelationship(
            mechanism=package.mechanism,
            source=package.source,
            target=package.target,
            claim=claim,
            evidence=(package.source.evidence[0], package.target.evidence[0]),
            review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
            run_id=RUN_ID,
        ),
        rejected=None,
        malformed_reason=None,
    )


def _rejected_result(
    package: CandidatePackage,
    *,
    reason: str,
) -> RelationshipValidationResult:
    """Build one rejected model diagnostic bound to its residual candidate."""
    return RelationshipValidationResult(
        outcome=ExtractionOutcome.SUCCESS,
        relationship=None,
        rejected=RejectedFinding(
            finding_id=relationship_subject_id(
                mechanism=package.mechanism,
                source=package.source,
                target=package.target,
            ),
            source_kind="relationship",
            summary=(
                f"{package.source.card_name} does not support {package.target.card_name}"
                f" through {package.mechanism}."
            ),
            reason=reason,
            run_id=RUN_ID,
        ),
        malformed_reason=None,
    )


def _relationship_entry(
    *,
    mechanism: str,
    source_card_id: int,
    target_card_id: int,
    source_quote: str,
    target_quote: str,
    source_face_index: int | None = None,
    target_face_index: int | None = None,
    required: bool = True,
) -> dict[str, object]:
    """Build one reviewed relationship expectation around exact participant Oracle evidence."""
    return {
        "mechanism": mechanism,
        "source_card_id": source_card_id,
        "target_card_id": target_card_id,
        "source_face_index": source_face_index,
        "target_face_index": target_face_index,
        "source_quote": source_quote,
        "target_quote": target_quote,
        "required": required,
    }


def _report(
    capabilities: Sequence[CardCapability],
    *,
    residual_pairs: int = 0,
    dry_run: bool = False,
    limit_applied: bool = False,
    full_capabilities: Sequence[CardCapability] | None = None,
    benchmark_relationships: Sequence[Mapping[str, object]] | None = None,
    benchmark_source_identity: bool = True,
    rejected_residuals: Sequence[int] = (),
) -> dict[str, Any]:
    """Build one complete HOB report over fixture capabilities and scripted model verdicts.

    `residual_pairs` adds pairs of a mechanism no declared role rule constructs, which are the only
    pairs a relationship validation still decides; `rejected_residuals` names the residual pairs
    whose scripted verdict rejects them, by residual index.
    """
    scene = (*capabilities, *_undeclared_participants(residual_pairs))
    sources = _sources(scene)
    card_results = _card_results(scene)
    package_set = construct_candidate_packages(card_results)
    resolutions = resolve_candidate_packages(
        tuple(
            sorted(
                (*package_set.packages, *_undeclared_packages(residual_pairs)),
                key=lambda package: package.identity,
            )
        )
    )
    rejected = set(rejected_residuals)
    relationship_results = tuple(
        _rejected_result(package, reason=MODEL_REJECTION_REASON)
        if index in rejected
        else _accepted_result(package, claim=f"Model claim {index}.")
        for index, package in enumerate(resolutions.model_packages)
    )
    result = EnrichmentRunResult(
        outcome=EnrichmentOutcome.COMPLETE,
        review=ArtifactReview(state="pending", reviewer_id=None, reviewed_at=None),
        run_id=RUN_ID,
        set_code=SET_CODE,
        set_source_sha256=set_source_sha256(sources),
        guide_ids=(),
        guide_results=(),
        card_ids=tuple(sorted({capability.card_id for capability in scene})),
        card_results=card_results,
        ineligible_card_ids=(),
        candidate_packages=package_set,
        candidate_resolutions=resolutions,
        relationship_results=relationship_results,
        progress=EnrichmentProgress(
            phase=EnrichmentPhase.RELATIONSHIPS,
            guides_completed=0,
            guides_total=0,
            cards_completed=len(card_results),
            cards_total=len(card_results),
            relationships_completed=len(resolutions.resolutions),
            relationships_total=len(resolutions.resolutions),
            valid_count=len(resolutions.local_accepted)
            + sum(1 for item in relationship_results if item.relationship is not None),
            uncertain_count=0,
            rejected_count=len(resolutions.local_rejected)
            + sum(1 for item in relationship_results if item.rejected is not None),
            accounting=EnrichmentAccounting(
                executed_work=len(card_results),
                reused_work=0,
                work_without_cost=0,
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                running_cost_usd="0",
                projected_final_cost_usd=None,
            ),
        ),
    )
    benchmark: dict[str, Any] | None = None
    if benchmark_relationships is not None:
        benchmark = {
            "set_source_sha256": (
                set_source_sha256(sources) if benchmark_source_identity else "f" * 64
            ),
            "mechanics": [],
            "relationships": [dict(entry) for entry in benchmark_relationships],
        }
    return _build_report(
        args=argparse.Namespace(
            dry_run=dry_run,
            work_dir=Path("work"),
            run_dir=Path("run"),
            benchmark=Path("benchmark.json"),
        ),
        result=result,
        sources=sources,
        full_sources=(
            sources if full_capabilities is None else _sources(full_capabilities)
        ),
        card_facts={"sha256": "0" * 64, "card_count": len(card_results)},
        guide_facts={"present": False, "path": "guide.txt"},
        limit_facts={
            "applied": limit_applied,
            "limited_to": len(card_results) if limit_applied else None,
            "eligible": len(card_results),
        },
        model_config=WorkModelConfig(model="vendor/model", reasoning_effort="high", max_tokens=4096),
        guard=_SpendFacts(),
        ceiling=Decimal("2.00"),
        profiles={
            "directories": [],
            "file_counts": {},
            "before_sha256": "0" * 64,
            "after_sha256": "0" * 64,
            "changed": [],
            "unchanged": True,
        },
        benchmark=benchmark,
        benchmark_validation=(
            None if benchmark is None else {"path": "benchmark.json", "failures": []}
        ),
    )


def _failure_kinds(report: Mapping[str, Any]) -> list[str]:
    """Return the acceptance failure kinds one report records."""
    return [failure["kind"] for failure in report["acceptance"]["failures"]]


def _resolution(report: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the candidate resolution facts of one fixture report."""
    return report["candidates"]["resolution"]


def test_residual_pairs_only_come_from_a_mechanism_no_role_rule_declares() -> None:
    assert UNDECLARED_MECHANISM not in {link.mechanism for link in ROLE_COMPATIBILITY_RULES}
    scene = (
        _token_maker(TOKEN_MAKER_CARD_ID),
        _undeclared_source(UNDECLARED_SOURCE_CARD_ID),
        _undeclared_target(UNDECLARED_TARGET_CARD_ID),
    )

    assert construct_candidate_packages(_card_results(scene)).packages == ()
    # The same construction over declared role pairs still builds pairs, so the empty result above
    # reports the undeclared mechanism rather than a scene no rule can ever match.
    assert len(construct_candidate_packages(_card_results(_mixed_scene())).packages) == 3
    assert [
        (item.basis.value, item.verdict.value)
        for item in resolve_candidate_packages(_undeclared_packages(2)).resolutions
    ] == [("model", "unresolved"), ("model", "unresolved")]


def test_relationship_rows_render_every_resolution_with_its_basis() -> None:
    maker = _token_maker(TOKEN_MAKER_CARD_ID)
    payoff = _go_wide_payoff(WIDE_PAYOFF_CARD_ID)
    nontoken = _go_wide_payoff(
        NONTOKEN_PAYOFF_CARD_ID,
        token_restriction=CapabilityTokenRestriction.NONTOKEN,
        quote=NONTOKEN_PAYOFF_QUOTE,
    )
    residual_sources = tuple(
        _undeclared_source(UNDECLARED_SOURCE_CARD_ID + index) for index in range(2)
    )
    residual_target = _undeclared_target(UNDECLARED_TARGET_CARD_ID)
    report = _report((maker, payoff, nontoken), residual_pairs=2, rejected_residuals=(1,))
    rows = report["relationships"]["items"]

    assert [row["subject_id"] for row in rows] == [
        relationship_subject_id(
            mechanism=UNDECLARED_MECHANISM, source=residual_sources[0], target=residual_target
        ),
        relationship_subject_id(
            mechanism=UNDECLARED_MECHANISM, source=residual_sources[1], target=residual_target
        ),
        relationship_subject_id(mechanism="token-go-wide-payoff", source=maker, target=payoff),
        relationship_subject_id(mechanism="token-go-wide-payoff", source=maker, target=nontoken),
    ]
    assert [(row["resolution_basis"], row["verdict"]) for row in rows] == [
        ("model", "accepted"),
        ("model", "rejected"),
        ("local", "accepted"),
        ("local", "rejected"),
    ]
    assert [row["reason"] for row in rows] == [
        None,
        MODEL_REJECTION_REASON,
        LOCAL_PROVE_REASON,
        TOKEN_RESTRICTION_CONFLICT_REASON,
    ]
    assert rows[0]["claim"] == "Model claim 0."
    assert rows[1]["claim"] == (
        f"Card {UNDECLARED_SOURCE_CARD_ID + 1} does not support"
        f" Card {UNDECLARED_TARGET_CARD_ID} through {UNDECLARED_MECHANISM}."
    )
    assert rows[2]["claim"] == (
        f"Card {TOKEN_MAKER_CARD_ID} supports Card {WIDE_PAYOFF_CARD_ID}"
        " through token-go-wide-payoff."
    )
    assert [item["quote"] for item in rows[2]["evidence"]] == [
        TOKEN_MAKER_QUOTE,
        WIDE_PAYOFF_QUOTE,
    ]
    assert rows[3]["claim"] == (
        f"Card {TOKEN_MAKER_CARD_ID} supports Card {NONTOKEN_PAYOFF_CARD_ID}"
        " through token-go-wide-payoff."
    )
    assert [row["resolution_basis"] for row in report["relationships"]["accepted"]] == [
        "model",
        "local",
    ]
    assert [row["resolution_basis"] for row in report["relationships"]["rejected"]] == [
        "model",
        "local",
    ]


def test_local_resolution_metrics_round_the_local_share_to_one_decimal() -> None:
    report = _report(_token_scene(), residual_pairs=1)
    resolution = _resolution(report)

    assert resolution["total_pairs"] == 3
    assert (resolution["local_pairs"], resolution["local_accepted"]) == (2, 1)
    assert resolution["local_rejected"] == 1
    assert resolution["model_pairs"] == 1
    assert resolution["local_percent"] == 66.7
    assert resolution["relationship_validation_batches"] == 1
    assert resolution["rejections"] == [
        {
            "mechanism": "token-go-wide-payoff",
            "omitted_pairs": 1,
            "reason": TOKEN_RESTRICTION_CONFLICT_REASON,
        }
    ]


def test_local_resolution_metrics_of_a_fully_local_run() -> None:
    report = _report(_mixed_scene())
    resolution = _resolution(report)
    rows = report["relationships"]["items"]

    assert resolution["total_pairs"] == 3
    assert (resolution["local_pairs"], resolution["local_accepted"]) == (3, 2)
    assert resolution["local_rejected"] == 1
    assert resolution["model_pairs"] == 0
    assert resolution["local_percent"] == 100.0
    assert resolution["relationship_validation_batches"] == 0
    assert resolution["rejections"] == [
        {
            "mechanism": "token-go-wide-payoff",
            "omitted_pairs": 1,
            "reason": TOKEN_RESTRICTION_CONFLICT_REASON,
        }
    ]
    assert [row["resolution_basis"] for row in rows] == ["local"] * resolution["total_pairs"]
    assert report["relationships"]["verdicts"] == {"accepted": 2, "rejected": 1}


def test_local_resolution_metrics_report_zero_percent_without_candidate_pairs() -> None:
    report = _report((_go_wide_payoff(WIDE_PAYOFF_CARD_ID),))
    resolution = _resolution(report)

    assert report["candidates"]["packages"] == 0
    assert resolution["total_pairs"] == 0
    assert resolution["local_percent"] == 0.0
    assert resolution["local_pairs"] == 0
    assert resolution["model_pairs"] == 0
    assert resolution["relationship_validation_batches"] == 0
    assert resolution["rejections"] == []


def test_work_kind_rows_report_no_residual_relationship_work() -> None:
    report = _report(_mixed_scene())
    kinds = {row["work_kind"]: row for row in report["work"]["kinds"]}
    relationship_kind = kinds[WorkKind.RELATIONSHIP.value]

    assert relationship_kind["items"] == 0
    assert relationship_kind["contract_version"] is None
    assert relationship_kind["prompt_id"] is None
    assert relationship_kind["response_schema_id"] is None
    assert relationship_kind["response_schema_name"] is None
    assert relationship_kind["prompt_sha256"] == []
    assert relationship_kind["response_schema_sha256"] == []
    assert relationship_kind["identity_sha256"] == []
    markdown = _render_markdown(report)
    assert f"- {WorkKind.RELATIONSHIP.value}: no resolved work items" in markdown


def test_planned_validation_batches_follow_the_residual_batch_size() -> None:
    report = _report(_mixed_scene(), residual_pairs=BATCH_SIZE_RESIDUAL_PAIRS)
    resolution = _resolution(report)
    relationship_kind = next(
        row for row in report["work"]["kinds"] if row["work_kind"] == WorkKind.RELATIONSHIP.value
    )

    assert (resolution["model_pairs"], resolution["total_pairs"]) == (
        BATCH_SIZE_RESIDUAL_PAIRS,
        3 + BATCH_SIZE_RESIDUAL_PAIRS,
    )
    assert resolution["relationship_validation_batches"] == 2
    assert relationship_kind["items"] == 2
    assert relationship_kind["prompt_id"] == RELATIONSHIP_BATCH_VALIDATION_PROMPT_ID
    assert len(relationship_kind["response_schema_sha256"]) == 1
    assert len(relationship_kind["identity_sha256"]) == 2
    assert [row["work_kind"] for row in report["work"]["kinds"]] == [
        WorkKind.GUIDE.value,
        WorkKind.CARD_CAPABILITY.value,
        WorkKind.RELATIONSHIP.value,
    ]


def test_acceptance_gates_fail_a_complete_run_below_the_local_threshold() -> None:
    report = _report(_mixed_scene(), residual_pairs=2, benchmark_relationships=())
    resolution = _resolution(report)

    assert report["complete"] is True
    assert report["benchmark"]["source_identity_match"] is True
    assert resolution["local_percent"] == 60.0
    assert resolution["local_percent"] < MINIMUM_LOCAL_RESOLUTION_PERCENT
    assert resolution["relationship_validation_batches"] == 1
    assert "local-resolution" in report["acceptance"]["checks"]
    assert "relationship-validation-calls" in report["acceptance"]["checks"]
    assert _failure_kinds(report) == ["local-resolution"]
    assert report["acceptance"]["passed"] is False


def test_acceptance_gates_pass_at_the_local_resolution_threshold() -> None:
    report = _report(_threshold_scene(), residual_pairs=1, benchmark_relationships=())
    resolution = _resolution(report)

    assert (resolution["local_pairs"], resolution["total_pairs"]) == (4, 5)
    assert resolution["local_percent"] == MINIMUM_LOCAL_RESOLUTION_PERCENT
    assert resolution["relationship_validation_batches"] == 1
    assert (
        resolution["relationship_validation_batches"]
        <= MAXIMUM_RELATIONSHIP_VALIDATION_BATCHES
    )
    assert report["acceptance"]["failures"] == []
    assert report["acceptance"]["passed"] is True


def test_acceptance_gates_pass_a_full_source_run_that_needs_no_validation() -> None:
    report = _report(_mixed_scene(), benchmark_relationships=())
    resolution = _resolution(report)

    assert resolution["local_percent"] == 100.0
    assert resolution["relationship_validation_batches"] == 0
    assert report["acceptance"]["failures"] == []
    assert report["acceptance"]["passed"] is True


def test_acceptance_gates_fail_a_run_above_the_validation_batch_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The HOB ceiling of 60 batches is far above any fixture the free suite can afford, so the
    # planned count is measured at a batch size of one over 61 residual pairs.
    monkeypatch.setattr(set_enrichment_module, "RELATIONSHIP_BATCH_SIZE", 1)
    report = _report(_batch_budget_scene(), residual_pairs=BATCH_BUDGET_RESIDUAL_PAIRS)
    resolution = _resolution(report)

    assert (resolution["local_pairs"], resolution["model_pairs"]) == (
        BATCH_BUDGET_LOCAL_PAIRS,
        BATCH_BUDGET_RESIDUAL_PAIRS,
    )
    assert resolution["local_percent"] >= MINIMUM_LOCAL_RESOLUTION_PERCENT
    assert resolution["relationship_validation_batches"] == BATCH_BUDGET_RESIDUAL_PAIRS
    assert _failure_kinds(report) == ["relationship-validation-calls"]
    assert report["acceptance"]["passed"] is False


def test_hob_gates_are_reported_but_not_applied_to_dry_and_limited_runs() -> None:
    dry_run = _report(_mixed_scene(), residual_pairs=2, dry_run=True)
    limited = _report(_mixed_scene(), residual_pairs=2, limit_applied=True)

    for report in (dry_run, limited):
        resolution = _resolution(report)
        assert resolution["total_pairs"] == 5
        assert resolution["local_percent"] < MINIMUM_LOCAL_RESOLUTION_PERCENT
        assert "local-resolution" not in report["acceptance"]["checks"]
        assert "relationship-validation-calls" not in report["acceptance"]["checks"]
        assert report["acceptance"]["failures"] == []
    assert dry_run["run"]["dry_run"] is True
    assert limited["sources"]["limit_applied"] is True


def test_hob_gates_require_a_complete_unlimited_full_source_live_run() -> None:
    scene = _mixed_scene()
    partial = _report(scene, full_capabilities=(*scene, _token_maker(900)))

    assert "local-resolution" not in partial["acceptance"]["checks"]
    assert "relationship-validation-calls" not in partial["acceptance"]["checks"]
    assert partial["acceptance"]["failures"] == []
    assert partial["sources"]["limit_applied"] is False
    assert (
        partial["sources"]["set_source_sha256"] != partial["sources"]["full_set_source_sha256"]
    )
    complete = _report(scene, benchmark_relationships=())
    assert "local-resolution" in complete["acceptance"]["checks"]
    assert "relationship-validation-calls" in complete["acceptance"]["checks"]


def test_benchmark_relationship_expectation_is_satisfied_by_a_locally_accepted_pair() -> None:
    report = _report(
        _mixed_scene(),
        benchmark_relationships=(
            _relationship_entry(
                mechanism="token-go-wide-payoff",
                source_card_id=TOKEN_MAKER_CARD_ID,
                target_card_id=WIDE_PAYOFF_CARD_ID,
                source_quote=TOKEN_MAKER_QUOTE,
                target_quote=WIDE_PAYOFF_QUOTE,
            ),
        ),
    )
    expectation = report["benchmark"]["relationships"][0]

    assert expectation["matched"] is True
    assert expectation["classification"] == "matched"
    assert expectation["evidence_quote_match"] is True
    assert [row["resolution_basis"] for row in expectation["constructed"]] == ["local"]
    assert report["benchmark"]["required_unmatched"] == []
    assert "required-relationship" not in _failure_kinds(report)


def test_benchmark_relationship_expectation_reports_a_locally_rejected_pair() -> None:
    report = _report(
        _mixed_scene(),
        benchmark_relationships=(
            _relationship_entry(
                mechanism="token-go-wide-payoff",
                source_card_id=TOKEN_MAKER_CARD_ID,
                target_card_id=NONTOKEN_PAYOFF_CARD_ID,
                source_quote=TOKEN_MAKER_QUOTE,
                target_quote=NONTOKEN_PAYOFF_QUOTE,
            ),
        ),
    )
    expectation = report["benchmark"]["relationships"][0]

    assert expectation["matched"] is False
    assert expectation["classification"] == "rejected"
    assert expectation["detail"].endswith(TOKEN_RESTRICTION_CONFLICT_REASON)
    assert report["benchmark"]["required_unmatched"] == [
        f"token-go-wide-payoff:{TOKEN_MAKER_CARD_ID}:None:{NONTOKEN_PAYOFF_CARD_ID}:None"
    ]
    assert "required-relationship" in _failure_kinds(report)


def test_benchmark_relationship_expectation_is_satisfied_by_a_model_validated_pair() -> None:
    report = _report(
        _token_scene(),
        residual_pairs=1,
        benchmark_relationships=(
            _relationship_entry(
                mechanism=UNDECLARED_MECHANISM,
                source_card_id=UNDECLARED_SOURCE_CARD_ID,
                target_card_id=UNDECLARED_TARGET_CARD_ID,
                source_quote=UNDECLARED_SOURCE_QUOTE,
                target_quote=UNDECLARED_TARGET_QUOTE,
            ),
        ),
    )
    expectation = report["benchmark"]["relationships"][0]

    assert expectation["matched"] is True
    assert expectation["classification"] == "matched"
    assert [row["resolution_basis"] for row in expectation["constructed"]] == ["model"]
    assert "required-relationship" not in _failure_kinds(report)


def test_markdown_renders_the_resolution_metrics() -> None:
    markdown = _render_markdown(_report(_mixed_scene(), residual_pairs=1))

    assert (
        "- resolution: 3/4 pairs decided locally (75.0%), accepted 2, rejected 1, "
        "awaiting validation 1" in markdown
    )
    assert "- planned relationship validation batches: 1" in markdown
    assert (
        "- local rejection: mechanism token-go-wide-payoff 1 pairs, "
        f"reason {TOKEN_RESTRICTION_CONFLICT_REASON}" in markdown
    )


def _boundary_progress(*, local_pairs: int, residual_pairs: int) -> EnrichmentProgress:
    """Build the progress event one run observes at its relationship boundary."""
    return EnrichmentProgress(
        phase=EnrichmentPhase.RELATIONSHIPS,
        guides_completed=0,
        guides_total=0,
        cards_completed=1,
        cards_total=1,
        relationships_completed=local_pairs,
        relationships_total=local_pairs + residual_pairs,
        valid_count=local_pairs,
        uncertain_count=0,
        rejected_count=0,
        accounting=EnrichmentAccounting(
            executed_work=1,
            reused_work=0,
            work_without_cost=0,
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            running_cost_usd="0",
            projected_final_cost_usd=None,
        ),
    )


def test_stop_guard_is_inactive_unless_the_environment_variable_is_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove mere membership in the environment never cancels a run."""
    boundary = _boundary_progress(local_pairs=0, residual_pairs=2)

    monkeypatch.delenv(_STOP_BEFORE_RELATIONSHIP_VALIDATION_ENV, raising=False)
    assert _relationship_stop_requested(boundary) is False
    monkeypatch.setenv(_STOP_BEFORE_RELATIONSHIP_VALIDATION_ENV, "0")
    assert _relationship_stop_requested(boundary) is False
    monkeypatch.setenv(_STOP_BEFORE_RELATIONSHIP_VALIDATION_ENV, "")
    assert _relationship_stop_requested(boundary) is False
    monkeypatch.setenv(_STOP_BEFORE_RELATIONSHIP_VALIDATION_ENV, "1")
    assert _relationship_stop_requested(boundary) is True


def test_stop_guard_leaves_a_fully_local_run_to_finish_its_acceptance_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove a run with no residual validation work is never cancelled at the boundary."""
    monkeypatch.setenv(_STOP_BEFORE_RELATIONSHIP_VALIDATION_ENV, "1")

    report = _report(_mixed_scene())

    assert _resolution(report)["model_pairs"] == 0
    assert _relationship_stop_requested(_boundary_progress(local_pairs=3, residual_pairs=0)) is False
    assert report["acceptance"]["passed"] is True


def test_stop_guard_cancels_a_residual_run_before_any_validation_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the guard still stops the paid boundary the user required it for."""
    monkeypatch.setenv(_STOP_BEFORE_RELATIONSHIP_VALIDATION_ENV, "1")

    report = _report(_mixed_scene(), residual_pairs=2)
    boundary = _boundary_progress(local_pairs=3, residual_pairs=2)

    assert _resolution(report)["model_pairs"] == 2
    assert _relationship_stop_requested(boundary) is True
    # The engine consults the caller's cancellation callback at the boundary it publishes before
    # the first batch, so an active guard cancels the run while zero validation calls are made.
    assert _relationship_stop_requested(None) is False
    assert _relationship_stop_requested(_boundary_progress(local_pairs=3, residual_pairs=0)) is False
