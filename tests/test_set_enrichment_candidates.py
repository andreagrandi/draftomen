"""Behavior tests for bounded candidate construction and structured local resolution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import FrozenInstanceError, dataclass, replace
import json
import time

import pytest

from draftomen.semantic_capability_records import (
    CapabilityAction,
    CapabilityCardType,
    CapabilityPrerequisite,
    CapabilityQualifier,
    CapabilityQuantity,
    CapabilityTokenRestriction,
    CapabilityZone,
    CardCapability,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment_records import FindingReview, FindingStatus, OracleEvidence
from draftomen.semantic_roles import Role
import draftomen.set_enrichment_candidates as candidates_module
from draftomen.set_enrichment_candidates import (
    CANDIDATE_REASON,
    LOCAL_CONFLICT_REASON_TEMPLATE,
    LOCAL_PROVE_REASON,
    LOCAL_UNRESOLVED_REASON,
    MAX_EVALUATED_CANDIDATE_PAIRS,
    MAX_PAIR_WORK_OMITTED_REASON,
    ROLE_COMPATIBILITY_RULES,
    CandidateBounds,
    CandidateOmission,
    CandidateOutcome,
    CandidatePackage,
    CandidatePackageSet,
    CandidateResolution,
    CandidateResolutionBasis,
    CandidateResolutionSet,
    CandidateResolutionVerdict,
    RoleLink,
    SetEnrichmentCandidatesError,
    construct_candidate_packages,
    resolve_candidate_packages,
)
from draftomen.set_enrichment_extraction import (
    CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION,
    CardCapabilityExtractionResult,
    ExtractionOutcome,
)


RUN_ID = "run-1"
OTHER_RUN_ID = "run-2"

TOKEN_ID = 301
SACRIFICE_ID = 302
DEATH_ID = 303
WIDE_ID = 311
DRAW_ID = 312
COUNTER_ID = 313
FODDER_ID = 321
OUTLET_ID = 500
FILLER_ID = 900
SCALING_TOKEN_ID = 1000
SCALING_OUTLET_ID = 2000

UNDECLARED_ID = 700
CAPABILITY_REVIEW_REASON = "capability requires semantic review beyond exact-source validation."
MALFORMED_REASON = (
    "response does not match card capability extraction schema version "
    f"{CARD_CAPABILITY_EXTRACTION_CONTRACT_VERSION}."
)
SAME_CARD_REASON = "source and target must be different cards."

TOKEN_QUOTE = "Create two 1/1 colorless Soldier artifact creature tokens."
WIDE_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
DEATH_QUOTE = "Whenever one or more creatures die, scry 1."
OUTLET_QUOTE = "Sacrifice a creature: Add {C}{C}."
FODDER_QUOTE = "When this creature dies, you gain 1 life."
DRAW_QUOTE = "Draw two cards."
COUNTER_QUOTE = "Counter target spell."
PREREQUISITE_QUOTE = "you may sacrifice a creature."
DISCARD_QUOTE = "Draw a card, then discard a card."
LOOT_QUOTE = "{2}, {T}: Draw a card, then discard a card."
RETURN_QUOTE = "When this creature dies, return it to the battlefield tapped."
RECURSION_QUOTE = "Return target creature card from your graveyard to your hand."
MILL_QUOTE = "Mill four cards."
GRAVEYARD_QUOTE = (
    "This creature gets +1/+1 as long as there are seven or more cards in your graveyard."
)

ENABLER_ID = 401
PAYOFF_ID = 402


def _role_parameters(role: Role) -> tuple[CapabilityAction, CapabilityZone, CapabilityQualifier]:
    """Return the role-accurate v2 action, zone, and qualifier of one capability."""
    if role is Role.TOKEN_MAKER:
        return (
            CapabilityAction.CREATE,
            CapabilityZone.BATTLEFIELD,
            CapabilityQualifier(
                card_types=(CapabilityCardType.CREATURE,),
                token_restriction=CapabilityTokenRestriction.TOKEN,
                subtype=None,
                mana_value=None,
            ),
        )
    if role is Role.GO_WIDE_PAYOFF:
        return (
            CapabilityAction.CONTROL,
            CapabilityZone.BATTLEFIELD,
            CapabilityQualifier(
                card_types=(CapabilityCardType.CREATURE,),
                token_restriction=CapabilityTokenRestriction.UNRESTRICTED,
                subtype=None,
                mana_value=None,
            ),
        )
    return (
        CapabilityAction.OTHER,
        CapabilityZone.BATTLEFIELD,
        CapabilityQualifier(
            card_types=(),
            token_restriction=CapabilityTokenRestriction.UNRESTRICTED,
            subtype=None,
            mana_value=None,
        ),
    )


def _capability(
    *,
    finding_id: str,
    card_id: int,
    card_name: str,
    role: Role,
    action: CapabilityAction | None = None,
    zone: CapabilityZone | None = None,
    qualifier: CapabilityQualifier | None = None,
    face_index: int | None = None,
    face_name: str | None = None,
    quantity: CapabilityQuantity | None = None,
    timing: str | None = None,
    source_zone: CapabilityZone | None = None,
    destination_zone: CapabilityZone | None = None,
    prerequisites: tuple[CapabilityPrerequisite, ...] = (),
    quote: str = "...",
    status: FindingStatus = FindingStatus.ACCEPTED,
    run_id: str = RUN_ID,
) -> CardCapability:
    """Build one real capability record around a single Oracle evidence quote."""
    role_action, role_zone, role_qualifier = _role_parameters(role)
    return CardCapability(
        finding_id=finding_id,
        card_id=card_id,
        card_name=card_name,
        face_index=face_index,
        face_name=face_name,
        role=role,
        action=role_action if action is None else action,
        zone=role_zone if zone is None else zone,
        qualifier=role_qualifier if qualifier is None else qualifier,
        quantity=quantity,
        timing=timing,
        source_zone=source_zone,
        destination_zone=destination_zone,
        prerequisites=prerequisites,
        evidence=(OracleEvidence(card_id=card_id, face_index=face_index, quote=quote),),
        review=FindingReview(
            status=status,
            reason=None if status is FindingStatus.ACCEPTED else CAPABILITY_REVIEW_REASON,
        ),
        run_id=run_id,
    )


def _result(
    *capabilities: CardCapability,
    outcome: ExtractionOutcome = ExtractionOutcome.SUCCESS,
    malformed_reason: str | None = None,
) -> CardCapabilityExtractionResult:
    """Build one terminal extraction result around real capability records."""
    if outcome is ExtractionOutcome.MALFORMED:
        return CardCapabilityExtractionResult(
            outcome=outcome,
            accepted_capabilities=(),
            uncertain_capabilities=(),
            rejected_capabilities=(),
            malformed_reason=(
                malformed_reason if malformed_reason is not None else MALFORMED_REASON
            ),
        )
    return CardCapabilityExtractionResult(
        outcome=outcome,
        accepted_capabilities=tuple(
            capability
            for capability in capabilities
            if capability.review.status is FindingStatus.ACCEPTED
        ),
        uncertain_capabilities=tuple(
            capability
            for capability in capabilities
            if capability.review.status is FindingStatus.UNCERTAIN
        ),
        rejected_capabilities=(),
        malformed_reason=malformed_reason,
    )


def _prerequisite(
    *,
    card_id: int,
    face_index: int | None,
    quote: str,
    quantity: CapabilityQuantity | None = None,
    timing: str | None = None,
    source_zone: CapabilityZone | None = None,
    destination_zone: CapabilityZone | None = None,
) -> CapabilityPrerequisite:
    """Build one cost prerequisite bound to an exact card face."""
    return CapabilityPrerequisite(
        kind=PrerequisiteKind.COST,
        quantity=quantity,
        timing=timing,
        source_zone=source_zone,
        destination_zone=destination_zone,
        evidence=OracleEvidence(card_id=card_id, face_index=face_index, quote=quote),
    )


def _token_enabler(*, run_id: str = RUN_ID) -> CardCapability:
    """Build one token-making capability on its own card."""
    return _capability(
        finding_id="capability-tokens",
        card_id=TOKEN_ID,
        card_name="Token Enabler",
        role=Role.TOKEN_MAKER,
        quote=TOKEN_QUOTE,
        run_id=run_id,
    )


def _wide_payoff(*, run_id: str = RUN_ID) -> CardCapability:
    """Build one go-wide payoff capability on its own card."""
    return _capability(
        finding_id="capability-wide",
        card_id=WIDE_ID,
        card_name="Wide Payoff",
        role=Role.GO_WIDE_PAYOFF,
        quote=WIDE_QUOTE,
        run_id=run_id,
    )


def _budget_fixture() -> tuple[CardCapabilityExtractionResult, ...]:
    """Build the compatible and unrelated capabilities used by the budget test."""
    results = [
        _result(
            _capability(
                finding_id=f"capability-token-{index}",
                card_id=TOKEN_ID + index,
                card_name=f"Token Maker {index}",
                role=Role.TOKEN_MAKER,
                quote=TOKEN_QUOTE,
            )
        )
        for index in range(3)
    ]
    results.extend(
        _result(
            _capability(
                finding_id=f"capability-wide-{index}",
                card_id=WIDE_ID + index,
                card_name=f"Wide Payoff {index}",
                role=Role.GO_WIDE_PAYOFF,
                quote=WIDE_QUOTE,
            )
        )
        for index in range(2)
    )
    results.extend(
        _result(
            _capability(
                finding_id=f"capability-fodder-{index}",
                card_id=FODDER_ID + index,
                card_name=f"Sacrifice Fodder {index}",
                role=Role.SACRIFICE_FODDER,
                quote=FODDER_QUOTE,
            )
        )
        for index in range(3)
    )
    results.extend(
        _result(
            _capability(
                finding_id=f"capability-outlet-{index}",
                card_id=OUTLET_ID + index,
                card_name=f"Sacrifice Outlet {index}",
                role=Role.SACRIFICE_OUTLET,
                quote=OUTLET_QUOTE,
            )
        )
        for index in range(100)
    )
    return tuple(results)


def _scaling_set(filler_count: int) -> tuple[CardCapabilityExtractionResult, ...]:
    """Build one compatible pair plus a caller-chosen number of unrelated cards."""
    results = [_result(_token_enabler()), _result(_wide_payoff())]
    for index in range(filler_count):
        results.append(
            _result(
                _capability(
                    finding_id=f"capability-filler-{index}",
                    card_id=FILLER_ID + index,
                    card_name=f"Filler {index}",
                    role=Role.DRAW,
                    quote=DRAW_QUOTE,
                )
            )
        )
    return tuple(results)


def _compatible_package() -> CandidatePackage:
    """Build one declared-compatible candidate package directly."""
    return CandidatePackage(
        mechanism="token-go-wide-payoff",
        source=_token_enabler(),
        target=_wide_payoff(),
        reason=CANDIDATE_REASON,
    )


def _conflicting_capability_pair() -> CandidatePackageSet:
    """Build two capabilities that share one identity with different content."""
    enabler = _token_enabler()
    conflicting = _capability(
        finding_id="capability-tokens",
        card_id=TOKEN_ID,
        card_name="Token Enabler",
        role=Role.DRAW,
    )
    return construct_candidate_packages((_result(enabler), _result(conflicting)))


def _same_card_package() -> CandidatePackage:
    """Build one package whose two participants share a card."""
    capability = _token_enabler()
    return CandidatePackage(
        mechanism="token-go-wide-payoff",
        source=capability,
        target=capability,
        reason=CANDIDATE_REASON,
    )


def _unaccounted_omissions() -> CandidatePackageSet:
    """Build a package set whose omissions miss one unevaluated candidate pair."""
    return CandidatePackageSet(
        outcome=CandidateOutcome.COMPLETE,
        packages=(),
        omissions=(),
        run_ids=(),
        candidate_pairs=1,
        evaluated_pairs=0,
        malformed_extractions=0,
    )


def test_public_surface_pins_contract_values_and_rules() -> None:
    assert set(candidates_module.__all__) == {
        "CANDIDATE_REASON",
        "LOCAL_CONFLICT_REASON_TEMPLATE",
        "LOCAL_PROVE_REASON",
        "LOCAL_UNRESOLVED_REASON",
        "MAX_EVALUATED_CANDIDATE_PAIRS",
        "MAX_PAIR_WORK_OMITTED_REASON",
        "ROLE_COMPATIBILITY_RULES",
        "CandidateBounds",
        "CandidateOmission",
        "CandidateOutcome",
        "CandidatePackage",
        "CandidatePackageSet",
        "CandidateResolution",
        "CandidateResolutionBasis",
        "CandidateResolutionSet",
        "CandidateResolutionVerdict",
        "RoleLink",
        "SetEnrichmentCandidatesError",
        "construct_candidate_packages",
        "resolve_candidate_packages",
    }
    assert issubclass(SetEnrichmentCandidatesError, ValueError)
    assert MAX_EVALUATED_CANDIDATE_PAIRS == 4096
    assert MAX_PAIR_WORK_OMITTED_REASON == "candidate pair work budget was exhausted"
    assert CANDIDATE_REASON == "declared role compatibility between typed capabilities"
    assert LOCAL_PROVE_REASON == "structured capability parameters prove compatibility"
    assert LOCAL_CONFLICT_REASON_TEMPLATE == (
        "structured capability parameters conflict: {field}"
    )
    assert LOCAL_UNRESOLVED_REASON == (
        "structured capability parameters do not fully determine compatibility"
    )
    assert {member.value for member in CandidateOutcome} == {"complete", "partial"}
    assert {member.value for member in CandidateResolutionBasis} == {"local", "model"}
    assert {member.value for member in CandidateResolutionVerdict} == {
        "accepted",
        "rejected",
        "unresolved",
    }
    assert CandidateBounds().max_evaluated_pairs == MAX_EVALUATED_CANDIDATE_PAIRS

    assert len(ROLE_COMPATIBILITY_RULES) == 11
    rules = [
        (rule.mechanism, rule.enabler, rule.payoff) for rule in ROLE_COMPATIBILITY_RULES
    ]
    assert rules == [
        ("discard-recursion-payoff", Role.DISCARD_ENABLER, Role.RECURSION_PAYOFF),
        ("fodder-dies-payoff", Role.SACRIFICE_FODDER, Role.DEATH_PAYOFF),
        ("fodder-sacrifice-outlet", Role.SACRIFICE_FODDER, Role.SACRIFICE_OUTLET),
        ("hone-equipment-payoff", Role.HONE_COUNTER_SOURCE, Role.HONE_EQUIPMENT_PAYOFF),
        ("loot-recursion-payoff", Role.LOOT, Role.RECURSION_PAYOFF),
        ("mill-graveyard-payoff", Role.SELF_MILL, Role.GRAVEYARD_PAYOFF),
        ("recursion-graveyard-payoff", Role.RECURSION, Role.GRAVEYARD_PAYOFF),
        ("token-death-payoff", Role.TOKEN_MAKER, Role.DEATH_PAYOFF),
        ("token-go-wide-payoff", Role.TOKEN_MAKER, Role.GO_WIDE_PAYOFF),
        ("token-sacrifice-outlet", Role.TOKEN_MAKER, Role.SACRIFICE_OUTLET),
        ("token-source-replacement", Role.TOKEN_MAKER, Role.TOKEN_REPLACEMENT),
    ]
    assert all(isinstance(rule, RoleLink) for rule in ROLE_COMPATIBILITY_RULES)
    mechanisms = [rule.mechanism for rule in ROLE_COMPATIBILITY_RULES]
    assert mechanisms == sorted(mechanisms)
    assert len(set(mechanisms)) == len(mechanisms)
    assert len({(rule.enabler, rule.payoff) for rule in ROLE_COMPATIBILITY_RULES}) == len(
        ROLE_COMPATIBILITY_RULES
    )
    assert ROLE_COMPATIBILITY_RULES[0].to_json() == {
        "mechanism": "discard-recursion-payoff",
        "enabler": "discard_enabler",
        "payoff": "recursion_payoff",
    }

    with pytest.raises(SetEnrichmentCandidatesError) as error:
        RoleLink(
            mechanism="token-go-wide-payoff",
            enabler="token_maker",  # type: ignore[arg-type]
            payoff=Role.GO_WIDE_PAYOFF,
        )
    assert str(error.value) == "enabler must be a Role."


def test_compatible_capabilities_form_the_expected_package() -> None:
    enabler = _token_enabler()
    payoff = _wide_payoff()

    package_set = construct_candidate_packages((_result(enabler), _result(payoff)))

    assert package_set.outcome is CandidateOutcome.COMPLETE
    assert package_set.malformed_extractions == 0
    assert len(package_set.packages) == 1
    package = package_set.packages[0]
    assert package.mechanism == "token-go-wide-payoff"
    assert package.source is enabler
    assert package.target is payoff
    assert package.source.finding_id == "capability-tokens"
    assert package.target.finding_id == "capability-wide"
    assert package.reason == CANDIDATE_REASON
    assert package.identity == (
        "token-go-wide-payoff",
        TOKEN_ID,
        "capability-tokens",
        WIDE_ID,
        "capability-wide",
    )
    assert package_set.candidate_pairs == 1
    assert package_set.evaluated_pairs == 1
    assert package_set.omissions == ()
    assert package_set.run_ids == (RUN_ID,)


def test_incompatible_capabilities_form_no_packages() -> None:
    fodder = _capability(
        finding_id="capability-fodder",
        card_id=SACRIFICE_ID,
        card_name="Sacrifice Fodder",
        role=Role.SACRIFICE_FODDER,
        quote=FODDER_QUOTE,
    )
    draw = _capability(
        finding_id="capability-draw",
        card_id=DRAW_ID,
        card_name="Draw Spell",
        role=Role.DRAW,
        quote=DRAW_QUOTE,
    )
    counterspell = _capability(
        finding_id="capability-counter",
        card_id=COUNTER_ID,
        card_name="Counter Spell",
        role=Role.COUNTERSPELL,
        quote=COUNTER_QUOTE,
    )

    package_set = construct_candidate_packages(
        (_result(_token_enabler(), fodder, draw, counterspell),)
    )

    assert package_set.packages == ()
    assert package_set.candidate_pairs == 0
    assert package_set.evaluated_pairs == 0
    assert package_set.omissions == ()


def test_same_card_capabilities_are_not_participants() -> None:
    token_maker = _capability(
        finding_id="capability-tokens",
        card_id=TOKEN_ID,
        card_name="Token Enabler",
        role=Role.TOKEN_MAKER,
        quote=TOKEN_QUOTE,
    )
    wide_payoff = _capability(
        finding_id="capability-wide",
        card_id=TOKEN_ID,
        card_name="Token Enabler",
        role=Role.GO_WIDE_PAYOFF,
        quote=TOKEN_QUOTE,
    )

    package_set = construct_candidate_packages((_result(token_maker, wide_payoff),))

    assert package_set.packages == ()
    assert package_set.candidate_pairs == 1
    assert package_set.evaluated_pairs == 1
    assert package_set.omissions == ()


def test_token_maker_feeds_a_death_payoff() -> None:
    death_payoff = _capability(
        finding_id="capability-death",
        card_id=DEATH_ID,
        card_name="Death Payoff",
        role=Role.DEATH_PAYOFF,
        quote=DEATH_QUOTE,
    )

    package_set = construct_candidate_packages((_result(_token_enabler(), death_payoff),))

    assert len(package_set.packages) == 1
    package = package_set.packages[0]
    assert package.mechanism == "token-death-payoff"
    assert package.source.role is Role.TOKEN_MAKER
    assert package.target.role is Role.DEATH_PAYOFF

    lone_enabler = construct_candidate_packages((_result(_token_enabler()),))

    assert [item.mechanism for item in lone_enabler.packages] == []


def test_distinct_cards_sharing_one_finding_id_stay_distinct() -> None:
    first = _capability(
        finding_id="capability-tokens",
        card_id=TOKEN_ID,
        card_name="First Token Maker",
        role=Role.TOKEN_MAKER,
        quote=TOKEN_QUOTE,
    )
    second = _capability(
        finding_id="capability-tokens",
        card_id=SACRIFICE_ID,
        card_name="Second Token Maker",
        role=Role.TOKEN_MAKER,
        quote=TOKEN_QUOTE,
    )
    payoff = _wide_payoff()

    package_set = construct_candidate_packages(
        (_result(first), _result(second), _result(payoff))
    )

    assert {package.source.card_id for package in package_set.packages} == {
        TOKEN_ID,
        SACRIFICE_ID,
    }
    assert [package.identity for package in package_set.packages] == [
        ("token-go-wide-payoff", TOKEN_ID, "capability-tokens", WIDE_ID, "capability-wide"),
        ("token-go-wide-payoff", SACRIFICE_ID, "capability-tokens", WIDE_ID, "capability-wide"),
    ]
    assert len(set(package_set.packages)) == 2


def test_package_retains_exact_evidence_prerequisites_zones_and_quantities() -> None:
    prerequisite = _prerequisite(
        card_id=TOKEN_ID,
        face_index=0,
        quote=PREREQUISITE_QUOTE,
        quantity=CapabilityQuantity(value=3, relation=QuantityRelation.AT_MOST),
        timing="before combat",
        source_zone=CapabilityZone.HAND,
        destination_zone=CapabilityZone.BATTLEFIELD,
    )
    enabler = _capability(
        finding_id="capability-tokens",
        card_id=TOKEN_ID,
        card_name="Token Enabler",
        role=Role.TOKEN_MAKER,
        face_index=0,
        face_name="Front",
        quantity=CapabilityQuantity(value=2, relation=QuantityRelation.EXACTLY),
        timing="your upkeep",
        source_zone=CapabilityZone.BATTLEFIELD,
        destination_zone=CapabilityZone.GRAVEYARD,
        prerequisites=(prerequisite,),
        quote=TOKEN_QUOTE,
    )
    payoff = _capability(
        finding_id="capability-wide",
        card_id=WIDE_ID,
        card_name="Wide Payoff",
        role=Role.GO_WIDE_PAYOFF,
        face_index=1,
        face_name="Back",
        quote=WIDE_QUOTE,
    )

    package_set = construct_candidate_packages((_result(enabler), _result(payoff)))
    package = package_set.packages[0]

    assert package.source is enabler
    assert package.target is payoff
    assert package.source.to_json() == enabler.to_json()
    assert package.target.to_json() == payoff.to_json()
    assert (package.source.card_id, package.source.card_name) == (TOKEN_ID, "Token Enabler")
    assert (package.source.face_index, package.source.face_name) == (0, "Front")
    assert (package.target.card_id, package.target.card_name) == (WIDE_ID, "Wide Payoff")
    assert (package.target.face_index, package.target.face_name) == (1, "Back")
    assert package.source.quantity == CapabilityQuantity(
        value=2,
        relation=QuantityRelation.EXACTLY,
    )
    assert package.source.timing == "your upkeep"
    assert package.source.source_zone is CapabilityZone.BATTLEFIELD
    assert package.source.destination_zone is CapabilityZone.GRAVEYARD
    assert package.source.prerequisites == (prerequisite,)
    assert package.source.evidence == enabler.evidence
    assert package.target.evidence == payoff.evidence
    assert package.source.to_json()["prerequisites"] == [
        {
            "kind": "cost",
            "quantity": {"value": 3, "relation": "at_most"},
            "timing": "before combat",
            "source_zone": "hand",
            "destination_zone": "battlefield",
            "evidence": {
                "card_id": TOKEN_ID,
                "face_index": 0,
                "quote": PREREQUISITE_QUOTE,
            },
        }
    ]


def test_package_json_round_trips() -> None:
    enabler = _token_enabler()
    payoff = _wide_payoff()

    package_set = construct_candidate_packages((_result(enabler), _result(payoff)))
    payload = package_set.to_json()

    assert json.loads(json.dumps(payload)) == payload
    assert set(payload) == {
        "outcome",
        "packages",
        "omissions",
        "run_ids",
        "candidate_pairs",
        "evaluated_pairs",
        "malformed_extractions",
    }
    assert payload["outcome"] == "complete"
    assert payload["run_ids"] == [RUN_ID]
    assert payload["omissions"] == []
    assert len(payload["packages"]) == 1
    package_payload = payload["packages"][0]
    assert package_payload["mechanism"] == "token-go-wide-payoff"
    assert package_payload["reason"] == CANDIDATE_REASON
    assert package_payload["source"] == enabler.to_json()
    assert package_payload["target"] == payoff.to_json()

    pristine = json.loads(json.dumps(payload))
    package_payload["source"]["evidence"][0]["quote"] = "mutated"
    payload["run_ids"].append("mutated")
    payload["packages"].append({})
    payload["omissions"].append({})

    assert package_set.to_json() == pristine


def test_reordering_input_does_not_change_identities_or_order() -> None:
    token = _result(_token_enabler())
    wide = _result(_wide_payoff())
    outlet = _result(
        _capability(
            finding_id="capability-outlet",
            card_id=OUTLET_ID,
            card_name="Sacrifice Outlet",
            role=Role.SACRIFICE_OUTLET,
            quote=OUTLET_QUOTE,
        )
    )

    baseline = construct_candidate_packages((token, wide, outlet))
    identities = [package.identity for package in baseline.packages]

    assert identities == [
        ("token-go-wide-payoff", TOKEN_ID, "capability-tokens", WIDE_ID, "capability-wide"),
        ("token-sacrifice-outlet", TOKEN_ID, "capability-tokens", OUTLET_ID, "capability-outlet"),
    ]
    assert identities == sorted(identities)
    assert len(set(identities)) == len(identities)
    assert baseline.candidate_pairs == baseline.evaluated_pairs == 2

    for permutation in (
        (outlet, wide, token),
        (wide, outlet, token),
        (token, token, wide, wide, outlet, outlet),
        (token, wide, outlet, token, wide, outlet),
    ):
        package_set = construct_candidate_packages(permutation)

        assert [package.identity for package in package_set.packages] == identities
        assert package_set.omissions == baseline.omissions
        assert package_set.candidate_pairs == baseline.candidate_pairs
        assert package_set.evaluated_pairs == baseline.evaluated_pairs
        assert package_set.run_ids == baseline.run_ids


def test_duplicate_capabilities_do_not_duplicate_candidates() -> None:
    enabler = _token_enabler()
    payoff = _wide_payoff()
    enabler_result = _result(enabler)
    payoff_result = _result(payoff)

    baseline = construct_candidate_packages((enabler_result, payoff_result))
    repeated = construct_candidate_packages(
        (enabler_result, payoff_result, enabler_result, payoff_result)
    )

    assert baseline.candidate_pairs == baseline.evaluated_pairs == 1
    assert len(baseline.packages) == 1
    assert [package.identity for package in repeated.packages] == [
        package.identity for package in baseline.packages
    ]
    assert repeated.candidate_pairs == repeated.evaluated_pairs == 1

    twin = _token_enabler()
    assert twin == enabler
    assert twin is not enabler

    equivalent = construct_candidate_packages((_result(enabler), _result(twin), payoff_result))

    assert len(equivalent.packages) == 1
    assert equivalent.packages[0].source is enabler

    conflicting = _capability(
        finding_id="capability-tokens",
        card_id=TOKEN_ID,
        card_name="Token Enabler",
        role=Role.DRAW,
        quote=TOKEN_QUOTE,
    )

    with pytest.raises(SetEnrichmentCandidatesError) as error:
        construct_candidate_packages((_result(enabler), _result(conflicting)))
    assert str(error.value) == "capabilities must not share an identity with different content."


def test_exhausted_work_budget_reports_deterministic_omissions() -> None:
    results = _budget_fixture()
    bounded = construct_candidate_packages(
        results,
        bounds=CandidateBounds(max_evaluated_pairs=10),
    )

    # Three fodder cards pair with 100 outlets and three token makers pair with 100 outlets;
    # only the six token-to-go-wide pairs fit the ten evaluated pairs budget.
    assert bounded.candidate_pairs == 606
    assert bounded.evaluated_pairs == 6
    assert len(bounded.packages) == 6
    assert {package.mechanism for package in bounded.packages} == {"token-go-wide-payoff"}
    assert [
        (omission.mechanism, omission.omitted_pairs, omission.reason)
        for omission in bounded.omissions
    ] == [
        ("fodder-sacrifice-outlet", 300, MAX_PAIR_WORK_OMITTED_REASON),
        ("token-sacrifice-outlet", 300, MAX_PAIR_WORK_OMITTED_REASON),
    ]
    assert (
        sum(omission.omitted_pairs for omission in bounded.omissions) + bounded.evaluated_pairs
        == bounded.candidate_pairs
    )

    reversed_bounded = construct_candidate_packages(
        tuple(reversed(results)),
        bounds=CandidateBounds(max_evaluated_pairs=10),
    )

    assert [package.identity for package in reversed_bounded.packages] == [
        package.identity for package in bounded.packages
    ]
    assert reversed_bounded.omissions == bounded.omissions
    assert reversed_bounded.candidate_pairs == bounded.candidate_pairs
    assert reversed_bounded.evaluated_pairs == bounded.evaluated_pairs

    unbounded = construct_candidate_packages(
        results,
        bounds=CandidateBounds(max_evaluated_pairs=MAX_EVALUATED_CANDIDATE_PAIRS),
    )

    assert unbounded.candidate_pairs == unbounded.evaluated_pairs == 606
    assert unbounded.omissions == ()
    assert sum(
        1 for package in unbounded.packages if package.mechanism == "token-sacrifice-outlet"
    ) == 300


def test_unrelated_capabilities_do_not_add_work_or_candidates() -> None:
    small = construct_candidate_packages(_scaling_set(0))
    started = time.monotonic()
    large = construct_candidate_packages(_scaling_set(200))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    for package_set in (small, large):
        assert len(package_set.packages) == 1
        assert package_set.candidate_pairs == package_set.evaluated_pairs == 1
        assert package_set.omissions == ()
    assert large.packages[0].identity == small.packages[0].identity

    results = tuple(
        _result(
            _capability(
                finding_id=f"capability-scale-token-{index}",
                card_id=SCALING_TOKEN_ID + index,
                card_name=f"Scale Token Maker {index}",
                role=Role.TOKEN_MAKER,
                quote=TOKEN_QUOTE,
            )
        )
        for index in range(1000)
    ) + tuple(
        _result(
            _capability(
                finding_id=f"capability-scale-outlet-{index}",
                card_id=SCALING_OUTLET_ID + index,
                card_name=f"Scale Sacrifice Outlet {index}",
                role=Role.SACRIFICE_OUTLET,
                quote=OUTLET_QUOTE,
            )
        )
        for index in range(1000)
    ) + (
        _result(
            _capability(
                finding_id="capability-scale-fodder",
                card_id=SACRIFICE_ID,
                card_name="Scale Sacrifice Fodder",
                role=Role.SACRIFICE_FODDER,
                quote=FODDER_QUOTE,
            )
        ),
    )

    bounds = CandidateBounds()
    bounded = construct_candidate_packages(results, bounds=bounds)

    # The single fodder card pairs with all 1000 outlets inside the budget; the 1000x1000
    # token-to-outlet pairs are omitted whole per mechanism instead of being enumerated.
    assert bounded.evaluated_pairs <= bounds.max_evaluated_pairs
    assert bounded.candidate_pairs == 1000 * 1000 + 1 * 1000
    assert (
        sum(omission.omitted_pairs for omission in bounded.omissions) + bounded.evaluated_pairs
        == bounded.candidate_pairs
    )
    assert [omission.mechanism for omission in bounded.omissions] == ["token-sacrifice-outlet"]
    assert [omission.omitted_pairs for omission in bounded.omissions] == [1000 * 1000]
    assert all(omission.reason == MAX_PAIR_WORK_OMITTED_REASON for omission in bounded.omissions)
    assert bounded.evaluated_pairs == 1 * 1000
    assert len(bounded.packages) == 1 * 1000
    assert {package.mechanism for package in bounded.packages} == {"fodder-sacrifice-outlet"}


def test_partial_extraction_outcome_is_explicit() -> None:
    enabler = _token_enabler()
    payoff = _wide_payoff(run_id=OTHER_RUN_ID)

    partial = construct_candidate_packages(
        (
            _result(enabler),
            _result(payoff),
            _result(outcome=ExtractionOutcome.MALFORMED),
        )
    )

    assert partial.outcome is CandidateOutcome.PARTIAL
    assert partial.malformed_extractions == 1
    assert len(partial.packages) == 1
    assert partial.run_ids == (RUN_ID, OTHER_RUN_ID)

    complete = construct_candidate_packages((_result(enabler), _result(payoff)))

    assert complete.outcome is CandidateOutcome.COMPLETE
    assert complete.malformed_extractions == 0
    assert len(complete.packages) == 1
    assert complete.packages[0].identity == partial.packages[0].identity

    duplicated = construct_candidate_packages(
        (
            _result(enabler),
            _result(payoff),
            _result(outcome=ExtractionOutcome.MALFORMED),
            _result(outcome=ExtractionOutcome.MALFORMED),
        )
    )

    assert duplicated.outcome is CandidateOutcome.PARTIAL
    assert duplicated.malformed_extractions == 1
    assert [package.identity for package in duplicated.packages] == [
        package.identity for package in partial.packages
    ]


@pytest.mark.parametrize(
    ("operation", "expected_message"),
    (
        pytest.param(
            lambda: construct_candidate_packages((object(),)),  # type: ignore[arg-type]
            "results must contain CardCapabilityExtractionResult records.",
            id="non-result-entry",
        ),
        pytest.param(
            lambda: construct_candidate_packages(
                (_result(),),
                bounds=object(),  # type: ignore[arg-type]
            ),
            "bounds must be a CandidateBounds.",
            id="non-bounds",
        ),
        pytest.param(
            lambda: CandidateBounds(max_evaluated_pairs=True),
            "max_evaluated_pairs must be a positive integer.",
            id="boolean-work-bound",
        ),
        pytest.param(
            lambda: CandidateBounds(max_evaluated_pairs=0),
            "max_evaluated_pairs must be a positive integer.",
            id="zero-work-bound",
        ),
        pytest.param(
            _conflicting_capability_pair,
            "capabilities must not share an identity with different content.",
            id="conflicting-duplicate-identity",
        ),
        pytest.param(
            _same_card_package,
            SAME_CARD_REASON,
            id="same-card-participants",
        ),
        pytest.param(
            lambda: CandidateOmission(
                mechanism="token-go-wide-payoff",
                omitted_pairs=0,
                reason=MAX_PAIR_WORK_OMITTED_REASON,
            ),
            "omitted_pairs must be a positive integer.",
            id="zero-omitted-pairs",
        ),
        pytest.param(
            _unaccounted_omissions,
            "omissions must account for every unevaluated candidate pair.",
            id="unaccounted-omissions",
        ),
        pytest.param(
            lambda: CandidatePackageSet(
                outcome=CandidateOutcome.COMPLETE,
                packages=(),
                omissions=(),
                run_ids=(),
                candidate_pairs=0,
                evaluated_pairs=1,
                malformed_extractions=0,
            ),
            "omissions must account for every unevaluated candidate pair.",
            id="evaluated-exceeds-candidate",
        ),
        pytest.param(
            lambda: CandidatePackageSet(
                outcome=CandidateOutcome.PARTIAL,
                packages=(),
                omissions=(),
                run_ids=(),
                candidate_pairs=0,
                evaluated_pairs=0,
                malformed_extractions=2,
            ),
            "malformed_extractions must be 0 or 1.",
            id="repeated-malformed-count",
        ),
    ),
)
def test_construction_rejects_invalid_trusted_input(
    operation: Callable[[], object],
    expected_message: str,
) -> None:
    with pytest.raises(SetEnrichmentCandidatesError) as error:
        operation()
    assert str(error.value) == expected_message


def test_candidate_packages_are_frozen_and_hashable() -> None:
    package = _compatible_package()
    equivalent = _compatible_package()

    assert package == equivalent
    assert package.identity == equivalent.identity
    assert hash(package) == hash(equivalent)

    with pytest.raises(FrozenInstanceError):
        package.mechanism = "fodder-dies-payoff"  # type: ignore[misc]


CREATURE_CARD_TYPES = (CapabilityCardType.CREATURE,)
ARTIFACT_CARD_TYPES = (CapabilityCardType.ARTIFACT,)


def _qualifier(
    *,
    card_types: tuple[CapabilityCardType, ...] = (),
    token_restriction: CapabilityTokenRestriction = CapabilityTokenRestriction.UNRESTRICTED,
    subtype: str | None = None,
    mana_value: CapabilityQuantity | None = None,
) -> CapabilityQualifier:
    """Build one capability qualifier that sets only the caller's fields."""
    return CapabilityQualifier(
        card_types=card_types,
        token_restriction=token_restriction,
        subtype=subtype,
        mana_value=mana_value,
    )


def _quantity(value: int | None, relation: QuantityRelation) -> CapabilityQuantity:
    """Build one stated capability quantity around its explicit relation."""
    return CapabilityQuantity(value=value, relation=relation)


def _conflict_reason(field: str) -> str:
    """Return the rejection reason one explicitly conflicting structured field produces."""
    return LOCAL_CONFLICT_REASON_TEMPLATE.format(field=field)


@dataclass(frozen=True)
class RouteParticipant:
    """One structured participant of a locally supported mechanism route."""

    card_id: int
    card_name: str
    role: Role
    action: CapabilityAction
    zone: CapabilityZone
    qualifier: CapabilityQualifier
    quote: str
    source_zone: CapabilityZone | None = None
    destination_zone: CapabilityZone | None = None

    def build(self, finding_id: str) -> CardCapability:
        """Build the capability record of this route participant."""
        return _capability(
            finding_id=finding_id,
            card_id=self.card_id,
            card_name=self.card_name,
            role=self.role,
            action=self.action,
            zone=self.zone,
            qualifier=self.qualifier,
            source_zone=self.source_zone,
            destination_zone=self.destination_zone,
            quote=self.quote,
        )


DISCARD_SOURCE = RouteParticipant(
    card_id=ENABLER_ID,
    card_name="Discard Enabler",
    role=Role.DISCARD_ENABLER,
    action=CapabilityAction.DISCARD,
    zone=CapabilityZone.STACK,
    qualifier=_qualifier(card_types=CREATURE_CARD_TYPES),
    quote=DISCARD_QUOTE,
    destination_zone=CapabilityZone.GRAVEYARD,
)
LOOT_SOURCE = replace(
    DISCARD_SOURCE,
    role=Role.LOOT,
    card_name="Loot Enabler",
    quote=LOOT_QUOTE,
)
MILL_SOURCE = RouteParticipant(
    card_id=ENABLER_ID,
    card_name="Mill Enabler",
    role=Role.SELF_MILL,
    action=CapabilityAction.MILL,
    zone=CapabilityZone.BATTLEFIELD,
    qualifier=_qualifier(),
    quote=MILL_QUOTE,
    destination_zone=CapabilityZone.GRAVEYARD,
)
TOKEN_SOURCE = RouteParticipant(
    card_id=ENABLER_ID,
    card_name="Token Enabler",
    role=Role.TOKEN_MAKER,
    action=CapabilityAction.CREATE,
    zone=CapabilityZone.BATTLEFIELD,
    qualifier=_qualifier(
        card_types=CREATURE_CARD_TYPES,
        token_restriction=CapabilityTokenRestriction.TOKEN,
    ),
    quote=TOKEN_QUOTE,
    destination_zone=CapabilityZone.BATTLEFIELD,
)
FODDER_SOURCE = RouteParticipant(
    card_id=ENABLER_ID,
    card_name="Sacrifice Fodder",
    role=Role.SACRIFICE_FODDER,
    action=CapabilityAction.SACRIFICE,
    zone=CapabilityZone.BATTLEFIELD,
    qualifier=_qualifier(card_types=CREATURE_CARD_TYPES),
    quote=FODDER_QUOTE,
    source_zone=CapabilityZone.BATTLEFIELD,
    destination_zone=CapabilityZone.GRAVEYARD,
)
RECURSION_SOURCE = RouteParticipant(
    card_id=ENABLER_ID,
    card_name="Recursion Enabler",
    role=Role.RECURSION,
    action=CapabilityAction.RETURN,
    zone=CapabilityZone.GRAVEYARD,
    qualifier=_qualifier(card_types=CREATURE_CARD_TYPES),
    quote=RETURN_QUOTE,
    source_zone=CapabilityZone.GRAVEYARD,
)
RECURSION_TARGET = RouteParticipant(
    card_id=PAYOFF_ID,
    card_name="Recursion Payoff",
    role=Role.RECURSION_PAYOFF,
    action=CapabilityAction.RETURN,
    zone=CapabilityZone.GRAVEYARD,
    qualifier=_qualifier(card_types=CREATURE_CARD_TYPES),
    quote=RECURSION_QUOTE,
    source_zone=CapabilityZone.GRAVEYARD,
)
GRAVEYARD_TARGET = RouteParticipant(
    card_id=PAYOFF_ID,
    card_name="Graveyard Payoff",
    role=Role.GRAVEYARD_PAYOFF,
    action=CapabilityAction.COUNT,
    zone=CapabilityZone.GRAVEYARD,
    qualifier=_qualifier(),
    quote=GRAVEYARD_QUOTE,
)
DEATH_TARGET = RouteParticipant(
    card_id=DEATH_ID,
    card_name="Death Payoff",
    role=Role.DEATH_PAYOFF,
    action=CapabilityAction.DIE,
    zone=CapabilityZone.GRAVEYARD,
    qualifier=_qualifier(),
    quote=DEATH_QUOTE,
    source_zone=CapabilityZone.BATTLEFIELD,
)
WIDE_TARGET = RouteParticipant(
    card_id=WIDE_ID,
    card_name="Wide Payoff",
    role=Role.GO_WIDE_PAYOFF,
    action=CapabilityAction.CONTROL,
    zone=CapabilityZone.BATTLEFIELD,
    qualifier=_qualifier(card_types=CREATURE_CARD_TYPES),
    quote=WIDE_QUOTE,
)
REPLACEMENT_TARGET = RouteParticipant(
    card_id=PAYOFF_ID,
    card_name="Token Replacement",
    role=Role.TOKEN_REPLACEMENT,
    action=CapabilityAction.REPLACE,
    zone=CapabilityZone.BATTLEFIELD,
    qualifier=_qualifier(),
    quote="If one or more tokens would be created, twice that many are created instead.",
)
OUTLET_TARGET = RouteParticipant(
    card_id=OUTLET_ID,
    card_name="Sacrifice Outlet",
    role=Role.SACRIFICE_OUTLET,
    action=CapabilityAction.SACRIFICE,
    zone=CapabilityZone.GRAVEYARD,
    qualifier=_qualifier(card_types=CREATURE_CARD_TYPES),
    quote=OUTLET_QUOTE,
    source_zone=CapabilityZone.BATTLEFIELD,
    destination_zone=CapabilityZone.GRAVEYARD,
)

# Token-producing parameters held by sources the token routes reject or cannot settle: a nontoken
# restriction, a non-creature card type, and an unclassified action.
NONTOKEN_SOURCE = replace(
    TOKEN_SOURCE,
    qualifier=_qualifier(
        card_types=CREATURE_CARD_TYPES,
        token_restriction=CapabilityTokenRestriction.NONTOKEN,
    ),
)
ARTIFACT_SOURCE = replace(
    TOKEN_SOURCE,
    qualifier=_qualifier(
        card_types=ARTIFACT_CARD_TYPES,
        token_restriction=CapabilityTokenRestriction.TOKEN,
    ),
)
UNCLASSIFIED_SOURCE = replace(TOKEN_SOURCE, action=CapabilityAction.OTHER)

ROUTE_PAIRS: Mapping[str, tuple[RouteParticipant, RouteParticipant]] = {
    "discard-recursion-payoff": (DISCARD_SOURCE, RECURSION_TARGET),
    "fodder-dies-payoff": (FODDER_SOURCE, DEATH_TARGET),
    "fodder-sacrifice-outlet": (FODDER_SOURCE, OUTLET_TARGET),
    "loot-recursion-payoff": (LOOT_SOURCE, RECURSION_TARGET),
    "mill-graveyard-payoff": (MILL_SOURCE, GRAVEYARD_TARGET),
    "token-death-payoff": (TOKEN_SOURCE, DEATH_TARGET),
    "token-go-wide-payoff": (TOKEN_SOURCE, WIDE_TARGET),
    "token-sacrifice-outlet": (TOKEN_SOURCE, OUTLET_TARGET),
    "token-source-replacement": (TOKEN_SOURCE, REPLACEMENT_TARGET),
}

TOKEN_MECHANISMS = (
    "token-death-payoff",
    "token-go-wide-payoff",
    "token-sacrifice-outlet",
)

FODDER_MECHANISMS = (
    "fodder-dies-payoff",
    "fodder-sacrifice-outlet",
)


def _route_package(
    mechanism: str,
    source: RouteParticipant,
    target: RouteParticipant,
) -> CandidatePackage:
    """Build one candidate package around two structured route participants."""
    return CandidatePackage(
        mechanism=mechanism,
        source=source.build(finding_id="capability-source"),
        target=target.build(finding_id="capability-target"),
        reason=CANDIDATE_REASON,
    )


def _only_resolution(package: CandidatePackage) -> CandidateResolution:
    """Return the single resolution one call resolves for one package."""
    resolution_set = resolve_candidate_packages((package,))

    assert len(resolution_set.resolutions) == 1
    return resolution_set.resolutions[0]


def _route_outcome(
    mechanism: str,
    *,
    source: RouteParticipant | None = None,
    target: RouteParticipant | None = None,
) -> CandidateResolution:
    """Resolve one route pair under the caller's participant replacements."""
    default_source, default_target = ROUTE_PAIRS[mechanism]
    return _only_resolution(
        _route_package(
            mechanism,
            default_source if source is None else source,
            default_target if target is None else target,
        )
    )


def _structured_results() -> tuple[CardCapabilityExtractionResult, ...]:
    """Build capabilities whose structured parameters route through every token mechanism."""
    return (
        _result(TOKEN_SOURCE.build(finding_id="capability-tokens")),
        _result(DEATH_TARGET.build(finding_id="capability-death")),
        _result(WIDE_TARGET.build(finding_id="capability-wide")),
        _result(OUTLET_TARGET.build(finding_id="capability-outlet")),
    )


def _mixed_outcome_packages() -> tuple[CandidatePackage, ...]:
    """Build token-to-wide candidates whose parameters resolve three different ways."""
    return construct_candidate_packages(
        (
            _result(
                replace(ARTIFACT_SOURCE, card_id=TOKEN_ID).build(
                    finding_id="capability-excluded"
                )
            ),
            _result(
                replace(UNCLASSIFIED_SOURCE, card_id=SACRIFICE_ID).build(
                    finding_id="capability-unclassified"
                )
            ),
            _result(TOKEN_SOURCE.build(finding_id="capability-tokens")),
            _result(WIDE_TARGET.build(finding_id="capability-wide")),
        )
    ).packages


def _rejection_packages() -> tuple[CandidatePackage, ...]:
    """Build candidates whose rejections group by mechanism and by conflicting field."""
    return construct_candidate_packages(
        (
            _result(
                replace(NONTOKEN_SOURCE, card_id=TOKEN_ID).build(
                    finding_id="capability-nontoken-one"
                )
            ),
            _result(
                replace(NONTOKEN_SOURCE, card_id=SACRIFICE_ID).build(
                    finding_id="capability-nontoken-two"
                )
            ),
            _result(ARTIFACT_SOURCE.build(finding_id="capability-artifact")),
            _result(WIDE_TARGET.build(finding_id="capability-wide")),
            _result(DEATH_TARGET.build(finding_id="capability-death")),
        )
    ).packages


ROUTE_CONFLICT_CASES = (
    pytest.param(
        "discard-recursion-payoff",
        replace(DISCARD_SOURCE, action=CapabilityAction.DRAW),
        RECURSION_TARGET,
        "action",
        id="discard-source-action",
    ),
    pytest.param(
        "discard-recursion-payoff",
        replace(DISCARD_SOURCE, destination_zone=CapabilityZone.EXILE),
        RECURSION_TARGET,
        "zone",
        id="discard-source-zone",
    ),
    pytest.param(
        "discard-recursion-payoff",
        replace(DISCARD_SOURCE, qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES)),
        RECURSION_TARGET,
        "card_types",
        id="discard-source-card-types",
    ),
    pytest.param(
        "discard-recursion-payoff",
        DISCARD_SOURCE,
        replace(RECURSION_TARGET, action=CapabilityAction.DRAW),
        "action",
        id="discard-target-action",
    ),
    pytest.param(
        "discard-recursion-payoff",
        DISCARD_SOURCE,
        replace(RECURSION_TARGET, source_zone=CapabilityZone.HAND),
        "zone",
        id="discard-target-zone",
    ),
    pytest.param(
        "fodder-dies-payoff",
        replace(FODDER_SOURCE, action=CapabilityAction.CREATE),
        DEATH_TARGET,
        "action",
        id="fodder-dies-source-action",
    ),
    pytest.param(
        "fodder-dies-payoff",
        replace(FODDER_SOURCE, destination_zone=CapabilityZone.EXILE),
        DEATH_TARGET,
        "zone",
        id="fodder-dies-source-zone",
    ),
    pytest.param(
        "fodder-dies-payoff",
        replace(FODDER_SOURCE, source_zone=CapabilityZone.LIBRARY),
        DEATH_TARGET,
        "zone",
        id="fodder-dies-source-origin-zone",
    ),
    pytest.param(
        "fodder-dies-payoff",
        FODDER_SOURCE,
        replace(DEATH_TARGET, source_zone=CapabilityZone.GRAVEYARD),
        "zone",
        id="fodder-dies-target-origin-zone",
    ),
    pytest.param(
        "fodder-dies-payoff",
        replace(FODDER_SOURCE, qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES)),
        DEATH_TARGET,
        "card_types",
        id="fodder-dies-source-card-types",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        replace(FODDER_SOURCE, action=CapabilityAction.CREATE),
        OUTLET_TARGET,
        "action",
        id="fodder-sacrifice-source-action",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        replace(FODDER_SOURCE, destination_zone=CapabilityZone.EXILE),
        OUTLET_TARGET,
        "zone",
        id="fodder-sacrifice-source-zone",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        replace(FODDER_SOURCE, source_zone=CapabilityZone.HAND),
        OUTLET_TARGET,
        "zone",
        id="fodder-sacrifice-source-origin-zone",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        FODDER_SOURCE,
        replace(OUTLET_TARGET, destination_zone=CapabilityZone.EXILE),
        "zone",
        id="fodder-sacrifice-target-zone",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        FODDER_SOURCE,
        replace(OUTLET_TARGET, source_zone=CapabilityZone.HAND),
        "zone",
        id="fodder-sacrifice-target-origin-zone",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        FODDER_SOURCE,
        replace(OUTLET_TARGET, qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES)),
        "card_types",
        id="fodder-sacrifice-card-types",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        replace(FODDER_SOURCE, qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES)),
        OUTLET_TARGET,
        "card_types",
        id="fodder-sacrifice-source-card-types",
    ),
    pytest.param(
        "loot-recursion-payoff",
        replace(LOOT_SOURCE, action=CapabilityAction.DRAW),
        RECURSION_TARGET,
        "action",
        id="loot-source-action",
    ),
    pytest.param(
        "loot-recursion-payoff",
        LOOT_SOURCE,
        replace(RECURSION_TARGET, action=CapabilityAction.DRAW),
        "action",
        id="loot-target-action",
    ),
    pytest.param(
        "mill-graveyard-payoff",
        replace(MILL_SOURCE, action=CapabilityAction.DRAW),
        GRAVEYARD_TARGET,
        "action",
        id="mill-source-action",
    ),
    pytest.param(
        "mill-graveyard-payoff",
        replace(MILL_SOURCE, qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES)),
        replace(GRAVEYARD_TARGET, qualifier=_qualifier(card_types=CREATURE_CARD_TYPES)),
        "card_types",
        id="mill-card-types",
    ),
    pytest.param(
        "token-death-payoff",
        replace(TOKEN_SOURCE, action=CapabilityAction.DRAW),
        DEATH_TARGET,
        "action",
        id="token-death-source-action",
    ),
    pytest.param(
        "token-death-payoff",
        replace(TOKEN_SOURCE, destination_zone=CapabilityZone.GRAVEYARD),
        DEATH_TARGET,
        "zone",
        id="token-death-source-zone",
    ),
    pytest.param(
        "token-death-payoff",
        TOKEN_SOURCE,
        replace(DEATH_TARGET, action=CapabilityAction.DRAW),
        "action",
        id="token-death-target-action",
    ),
    pytest.param(
        "token-death-payoff",
        TOKEN_SOURCE,
        replace(DEATH_TARGET, source_zone=CapabilityZone.GRAVEYARD),
        "zone",
        id="token-death-target-zone",
    ),
    pytest.param(
        "token-go-wide-payoff",
        replace(TOKEN_SOURCE, action=CapabilityAction.DRAW),
        WIDE_TARGET,
        "action",
        id="token-go-wide-source-action",
    ),
    pytest.param(
        "token-go-wide-payoff",
        TOKEN_SOURCE,
        replace(WIDE_TARGET, action=CapabilityAction.DRAW),
        "action",
        id="token-go-wide-target-action",
    ),
    pytest.param(
        "token-go-wide-payoff",
        TOKEN_SOURCE,
        replace(WIDE_TARGET, zone=CapabilityZone.GRAVEYARD),
        "zone",
        id="token-go-wide-target-zone",
    ),
    pytest.param(
        "token-sacrifice-outlet",
        replace(TOKEN_SOURCE, action=CapabilityAction.DRAW),
        OUTLET_TARGET,
        "action",
        id="token-sacrifice-source-action",
    ),
    pytest.param(
        "token-sacrifice-outlet",
        TOKEN_SOURCE,
        replace(OUTLET_TARGET, action=CapabilityAction.DRAW),
        "action",
        id="token-sacrifice-target-action",
    ),
    pytest.param(
        "token-sacrifice-outlet",
        TOKEN_SOURCE,
        replace(OUTLET_TARGET, source_zone=CapabilityZone.HAND),
        "zone",
        id="token-sacrifice-target-zone",
    ),
)

ROLE_ANCHORED_CASES = (
    pytest.param(
        "discard-recursion-payoff",
        DISCARD_SOURCE,
        replace(RECURSION_TARGET, action=CapabilityAction.OTHER),
        id="discard-target-action",
    ),
    pytest.param(
        "discard-recursion-payoff",
        DISCARD_SOURCE,
        replace(RECURSION_TARGET, source_zone=None),
        id="discard-target-zone",
    ),
    pytest.param(
        "fodder-dies-payoff",
        FODDER_SOURCE,
        replace(DEATH_TARGET, action=CapabilityAction.OTHER),
        id="fodder-dies-target-action",
    ),
    pytest.param(
        "fodder-dies-payoff",
        FODDER_SOURCE,
        replace(DEATH_TARGET, source_zone=None),
        id="fodder-dies-target-origin-zone",
    ),
    pytest.param(
        "fodder-dies-payoff",
        replace(FODDER_SOURCE, source_zone=None),
        DEATH_TARGET,
        id="fodder-dies-source-origin-zone",
    ),
    pytest.param(
        "fodder-dies-payoff",
        replace(FODDER_SOURCE, qualifier=_qualifier()),
        DEATH_TARGET,
        id="fodder-dies-source-card-types",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        replace(FODDER_SOURCE, source_zone=None),
        OUTLET_TARGET,
        id="fodder-sacrifice-source-origin-zone",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        replace(FODDER_SOURCE, qualifier=_qualifier()),
        OUTLET_TARGET,
        id="fodder-sacrifice-source-card-types",
    ),
    pytest.param(
        "fodder-sacrifice-outlet",
        FODDER_SOURCE,
        replace(OUTLET_TARGET, source_zone=None),
        id="fodder-sacrifice-target-origin-zone",
    ),
    pytest.param(
        "loot-recursion-payoff",
        LOOT_SOURCE,
        replace(RECURSION_TARGET, action=CapabilityAction.OTHER),
        id="loot-target-action",
    ),
    pytest.param(
        "loot-recursion-payoff",
        LOOT_SOURCE,
        replace(RECURSION_TARGET, source_zone=None),
        id="loot-target-zone",
    ),
    pytest.param(
        "token-death-payoff",
        TOKEN_SOURCE,
        replace(DEATH_TARGET, action=CapabilityAction.OTHER),
        id="token-death-target-action",
    ),
    pytest.param(
        "token-death-payoff",
        TOKEN_SOURCE,
        replace(DEATH_TARGET, source_zone=None),
        id="token-death-target-zone",
    ),
    pytest.param(
        "token-sacrifice-outlet",
        TOKEN_SOURCE,
        replace(OUTLET_TARGET, action=CapabilityAction.OTHER),
        id="token-sacrifice-target-action",
    ),
    pytest.param(
        "token-sacrifice-outlet",
        TOKEN_SOURCE,
        replace(OUTLET_TARGET, source_zone=None),
        id="token-sacrifice-target-zone",
    ),
)

UNROUTABLE_MECHANISM_CASES = (
    pytest.param(
        "undeclared-mechanism",
        TOKEN_SOURCE,
        DEATH_TARGET,
        id="undeclared-mechanism-token-parameters",
    ),
    pytest.param(
        "undeclared-mechanism",
        DISCARD_SOURCE,
        RECURSION_TARGET,
        id="undeclared-mechanism-discard-parameters",
    ),
)


@pytest.mark.parametrize("mechanism", ROUTE_PAIRS)
def test_supported_routes_accept_compatible_pairs(mechanism: str) -> None:
    source, target = ROUTE_PAIRS[mechanism]
    package = _route_package(mechanism, source, target)

    resolution = _only_resolution(package)

    assert resolution.package is package
    assert resolution.identity == package.identity
    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize(
    ("mechanism", "source", "target", "field"),
    ROUTE_CONFLICT_CASES,
)
def test_supported_routes_reject_conflicting_structured_fields(
    mechanism: str,
    source: RouteParticipant,
    target: RouteParticipant,
    field: str,
) -> None:
    resolution = _route_outcome(mechanism, source=source, target=target)

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason(field)


@pytest.mark.parametrize(("mechanism", "source", "target"), ROLE_ANCHORED_CASES)
def test_supported_routes_accept_fields_the_roles_do_not_contradict(
    mechanism: str,
    source: RouteParticipant,
    target: RouteParticipant,
) -> None:
    resolution = _route_outcome(mechanism, source=source, target=target)

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize("mechanism", ROUTE_PAIRS)
def test_unclassified_source_actions_are_role_anchored(mechanism: str) -> None:
    resolution = _route_outcome(
        mechanism,
        source=replace(ROUTE_PAIRS[mechanism][0], action=CapabilityAction.OTHER),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize("mechanism", ROUTE_PAIRS)
def test_missing_source_destination_zones_are_role_anchored(mechanism: str) -> None:
    resolution = _route_outcome(
        mechanism,
        source=replace(ROUTE_PAIRS[mechanism][0], destination_zone=None),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize("restriction", tuple(CapabilityTokenRestriction))
def test_unrestricted_payoffs_accept_any_produced_token_restriction(
    restriction: CapabilityTokenRestriction,
) -> None:
    resolution = _route_outcome(
        "discard-recursion-payoff",
        source=replace(
            DISCARD_SOURCE,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                token_restriction=restriction,
            ),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize(
    "restriction",
    (CapabilityTokenRestriction.TOKEN, CapabilityTokenRestriction.NONTOKEN),
)
def test_unstated_sources_accept_restricted_payoffs(
    restriction: CapabilityTokenRestriction,
) -> None:
    resolution = _route_outcome(
        "discard-recursion-payoff",
        target=replace(
            RECURSION_TARGET,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                token_restriction=restriction,
            ),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize("mechanism", TOKEN_MECHANISMS)
def test_token_routes_reject_nontoken_sources(mechanism: str) -> None:
    resolution = _route_outcome(mechanism, source=NONTOKEN_SOURCE)

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason("token_restriction")


@pytest.mark.parametrize("mechanism", TOKEN_MECHANISMS)
def test_token_routes_reject_payoffs_that_exclude_tokens(mechanism: str) -> None:
    _, target = ROUTE_PAIRS[mechanism]
    excluded = replace(
        target,
        qualifier=_qualifier(
            card_types=target.qualifier.card_types,
            token_restriction=CapabilityTokenRestriction.NONTOKEN,
        ),
    )

    resolution = _route_outcome(mechanism, target=excluded)

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason("token_restriction")


@pytest.mark.parametrize("mechanism", TOKEN_MECHANISMS)
def test_token_sources_must_produce_creature_tokens(mechanism: str) -> None:
    source, target = ROUTE_PAIRS[mechanism]

    unstated = _route_outcome(
        mechanism,
        source=replace(
            source,
            qualifier=_qualifier(token_restriction=CapabilityTokenRestriction.TOKEN),
        ),
        target=target,
    )
    conflicting = _route_outcome(
        mechanism,
        source=ARTIFACT_SOURCE,
        target=target,
    )

    assert unstated.basis is CandidateResolutionBasis.LOCAL
    assert unstated.verdict is CandidateResolutionVerdict.ACCEPTED
    assert unstated.reason == LOCAL_PROVE_REASON
    assert conflicting.basis is CandidateResolutionBasis.LOCAL
    assert conflicting.verdict is CandidateResolutionVerdict.REJECTED
    assert conflicting.reason == _conflict_reason("card_types")


@pytest.mark.parametrize(
    "mechanism",
    ("token-go-wide-payoff", "token-sacrifice-outlet"),
)
def test_creature_token_payoffs_must_select_creatures(mechanism: str) -> None:
    source, target = ROUTE_PAIRS[mechanism]

    unstated = _route_outcome(
        mechanism,
        source=source,
        target=replace(target, qualifier=_qualifier()),
    )
    conflicting = _route_outcome(
        mechanism,
        source=source,
        target=replace(target, qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES)),
    )

    assert unstated.basis is CandidateResolutionBasis.LOCAL
    assert unstated.verdict is CandidateResolutionVerdict.ACCEPTED
    assert unstated.reason == LOCAL_PROVE_REASON
    assert conflicting.basis is CandidateResolutionBasis.LOCAL
    assert conflicting.verdict is CandidateResolutionVerdict.REJECTED
    assert conflicting.reason == _conflict_reason("card_types")


def test_go_wide_other_payoffs_are_role_anchored() -> None:
    """The closed action vocabulary has no enhancement verb, so `OTHER` leaves the payoff to the
    declared role, while an explicit non-creature selection still contradicts the mechanism.
    """
    selected = _route_outcome(
        "token-go-wide-payoff",
        target=replace(WIDE_TARGET, action=CapabilityAction.OTHER),
    )
    unselected = _route_outcome(
        "token-go-wide-payoff",
        target=replace(
            WIDE_TARGET,
            action=CapabilityAction.OTHER,
            qualifier=_qualifier(),
        ),
    )
    non_creature = _route_outcome(
        "token-go-wide-payoff",
        target=replace(
            WIDE_TARGET,
            action=CapabilityAction.OTHER,
            qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES),
        ),
    )

    assert selected.basis is CandidateResolutionBasis.LOCAL
    assert selected.verdict is CandidateResolutionVerdict.ACCEPTED
    assert selected.reason == LOCAL_PROVE_REASON
    assert unselected.basis is CandidateResolutionBasis.LOCAL
    assert unselected.verdict is CandidateResolutionVerdict.ACCEPTED
    assert unselected.reason == LOCAL_PROVE_REASON
    assert non_creature.basis is CandidateResolutionBasis.LOCAL
    assert non_creature.verdict is CandidateResolutionVerdict.REJECTED
    assert non_creature.reason == _conflict_reason("card_types")


def test_go_wide_other_payoffs_that_exclude_tokens_reject() -> None:
    resolution = _route_outcome(
        "token-go-wide-payoff",
        target=replace(
            WIDE_TARGET,
            action=CapabilityAction.OTHER,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                token_restriction=CapabilityTokenRestriction.NONTOKEN,
            ),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason("token_restriction")


def test_fodder_outlets_accept_the_sacrificed_objects_they_allow() -> None:
    """An outlet that names the fodder's card type, or names no card type at all, can be fed by
    the creatures the fodder sacrifices.
    """
    artifact_or_creature = _route_outcome(
        "fodder-sacrifice-outlet",
        target=replace(
            OUTLET_TARGET,
            qualifier=_qualifier(
                card_types=(CapabilityCardType.ARTIFACT, CapabilityCardType.CREATURE)
            ),
        ),
    )
    unrestricted = _route_outcome(
        "fodder-sacrifice-outlet",
        target=replace(OUTLET_TARGET, qualifier=_qualifier()),
    )

    assert artifact_or_creature.basis is CandidateResolutionBasis.LOCAL
    assert artifact_or_creature.verdict is CandidateResolutionVerdict.ACCEPTED
    assert artifact_or_creature.reason == LOCAL_PROVE_REASON
    assert unrestricted.basis is CandidateResolutionBasis.LOCAL
    assert unrestricted.verdict is CandidateResolutionVerdict.ACCEPTED
    assert unrestricted.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize("mechanism", FODDER_MECHANISMS)
def test_fodder_routes_accept_a_stated_nontoken_body_for_a_nontoken_payoff(
    mechanism: str,
) -> None:
    """A fodder that states itself nontoken is exactly the body a payoff rewarding nontokens needs,
    so the pair is compatible instead of rejected for the payoff's own restriction.
    """
    resolution = _route_outcome(
        mechanism,
        source=replace(
            FODDER_SOURCE,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                token_restriction=CapabilityTokenRestriction.NONTOKEN,
            ),
        ),
        target=replace(
            ROUTE_PAIRS[mechanism][1],
            qualifier=_qualifier(token_restriction=CapabilityTokenRestriction.NONTOKEN),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize("mechanism", FODDER_MECHANISMS)
def test_fodder_routes_accept_an_unstated_body_kind_on_the_declared_role_pair(
    mechanism: str,
) -> None:
    """The fodder role states a creature body, never that the body is a nontoken, so a payoff that
    rewards nontokens only is never contradicted by the absent statement: the declared role pair
    carries the pair locally.
    """
    resolution = _route_outcome(
        mechanism,
        target=replace(
            ROUTE_PAIRS[mechanism][1],
            qualifier=_qualifier(token_restriction=CapabilityTokenRestriction.NONTOKEN),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize("mechanism", FODDER_MECHANISMS)
@pytest.mark.parametrize(
    ("source_restriction", "target_restriction"),
    (
        (CapabilityTokenRestriction.TOKEN, CapabilityTokenRestriction.NONTOKEN),
        (CapabilityTokenRestriction.NONTOKEN, CapabilityTokenRestriction.TOKEN),
    ),
)
def test_fodder_routes_reject_stated_body_kinds_a_payoff_contradicts(
    mechanism: str,
    source_restriction: CapabilityTokenRestriction,
    target_restriction: CapabilityTokenRestriction,
) -> None:
    """Two explicit restrictions that differ contradict the pair in both directions. When the payoff
    rewards nontokens only, that explicit conflict shares its field with the missing-evidence signal
    an unstated fodder body reports, and the explicit conflict outranks it.
    """
    resolution = _route_outcome(
        mechanism,
        source=replace(
            FODDER_SOURCE,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                token_restriction=source_restriction,
            ),
        ),
        target=replace(
            ROUTE_PAIRS[mechanism][1],
            qualifier=_qualifier(token_restriction=target_restriction),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason("token_restriction")


@pytest.mark.parametrize(
    "mechanism",
    ("fodder-dies-payoff", "fodder-sacrifice-outlet"),
)
def test_fodder_routes_accept_payoffs_that_name_only_the_object_they_supply(
    mechanism: str,
) -> None:
    resolution = _route_outcome(
        mechanism,
        target=replace(
            ROUTE_PAIRS[mechanism][1],
            qualifier=_qualifier(token_restriction=CapabilityTokenRestriction.TOKEN),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


def test_fodder_qualifier_conflicts_follow_their_fixed_field_order() -> None:
    resolution = _route_outcome(
        "fodder-dies-payoff",
        source=replace(FODDER_SOURCE, qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES)),
        target=replace(
            DEATH_TARGET,
            qualifier=_qualifier(token_restriction=CapabilityTokenRestriction.NONTOKEN),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason("card_types")


@pytest.mark.parametrize(
    "mechanism",
    (
        "discard-recursion-payoff",
        "loot-recursion-payoff",
        "mill-graveyard-payoff",
        "token-death-payoff",
    ),
)
def test_unrestricted_payoff_card_types_accept_any_produced_types(mechanism: str) -> None:
    resolution = _route_outcome(
        mechanism,
        target=replace(ROUTE_PAIRS[mechanism][1], qualifier=_qualifier()),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize(
    "mechanism",
    ("discard-recursion-payoff", "loot-recursion-payoff", "mill-graveyard-payoff"),
)
def test_empty_produced_card_types_are_role_backed_supplies(
    mechanism: str,
) -> None:
    resolution = _route_outcome(
        mechanism,
        source=replace(ROUTE_PAIRS[mechanism][0], qualifier=_qualifier()),
        target=replace(
            ROUTE_PAIRS[mechanism][1],
            qualifier=_qualifier(card_types=CREATURE_CARD_TYPES),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize(
    ("mechanism", "source_qualifier", "target_qualifier"),
    (
        pytest.param(
            "discard-recursion-payoff",
            _qualifier(card_types=ARTIFACT_CARD_TYPES),
            _qualifier(card_types=CREATURE_CARD_TYPES),
            id="discard-recursion-payoff",
        ),
        pytest.param(
            "loot-recursion-payoff",
            _qualifier(card_types=ARTIFACT_CARD_TYPES),
            _qualifier(card_types=CREATURE_CARD_TYPES),
            id="loot-recursion-payoff",
        ),
        pytest.param(
            "mill-graveyard-payoff",
            _qualifier(card_types=ARTIFACT_CARD_TYPES),
            _qualifier(card_types=CREATURE_CARD_TYPES),
            id="mill-graveyard-payoff",
        ),
        pytest.param(
            "token-death-payoff",
            _qualifier(
                card_types=CREATURE_CARD_TYPES,
                token_restriction=CapabilityTokenRestriction.TOKEN,
            ),
            _qualifier(card_types=ARTIFACT_CARD_TYPES),
            id="token-death-payoff",
        ),
    ),
)
def test_disjoint_explicit_card_types_reject(
    mechanism: str,
    source_qualifier: CapabilityQualifier,
    target_qualifier: CapabilityQualifier,
) -> None:
    resolution = _route_outcome(
        mechanism,
        source=replace(ROUTE_PAIRS[mechanism][0], qualifier=source_qualifier),
        target=replace(ROUTE_PAIRS[mechanism][1], qualifier=target_qualifier),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason("card_types")


def test_null_payoff_subtypes_accept_any_produced_subtype() -> None:
    resolution = _route_outcome(
        "discard-recursion-payoff",
        source=replace(
            DISCARD_SOURCE,
            qualifier=_qualifier(card_types=CREATURE_CARD_TYPES, subtype="Goblin"),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize(
    ("source_subtype", "target_subtype", "verdict", "reason"),
    (
        pytest.param(
            "Goblin",
            "goblin",
            CandidateResolutionVerdict.ACCEPTED,
            LOCAL_PROVE_REASON,
            id="equal-subtypes",
        ),
        pytest.param(
            "Goblin",
            "Elf",
            CandidateResolutionVerdict.REJECTED,
            _conflict_reason("subtype"),
            id="unequal-subtypes",
        ),
    ),
)
def test_explicit_subtypes_compare_produced_against_selected(
    source_subtype: str,
    target_subtype: str,
    verdict: CandidateResolutionVerdict,
    reason: str,
) -> None:
    resolution = _route_outcome(
        "discard-recursion-payoff",
        source=replace(
            DISCARD_SOURCE,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                subtype=source_subtype,
            ),
        ),
        target=replace(
            RECURSION_TARGET,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                subtype=target_subtype,
            ),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is verdict
    assert resolution.reason == reason


def test_unstated_supplied_subtypes_never_contradict_a_subtype_constraint() -> None:
    resolution = _route_outcome(
        "discard-recursion-payoff",
        target=replace(
            RECURSION_TARGET,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                subtype="Goblin",
            ),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution.reason == LOCAL_PROVE_REASON


@pytest.mark.parametrize(
    ("source_mana_value", "target_mana_value", "verdict"),
    (
        pytest.param(
            _quantity(2, QuantityRelation.EXACTLY),
            None,
            CandidateResolutionVerdict.ACCEPTED,
            id="unconstrained-payoff",
        ),
        pytest.param(
            _quantity(3, QuantityRelation.EXACTLY),
            _quantity(2, QuantityRelation.AT_LEAST),
            CandidateResolutionVerdict.ACCEPTED,
            id="exactly-inside-at-least",
        ),
        pytest.param(
            _quantity(1, QuantityRelation.EXACTLY),
            _quantity(3, QuantityRelation.AT_MOST),
            CandidateResolutionVerdict.ACCEPTED,
            id="exactly-inside-at-most",
        ),
        pytest.param(
            _quantity(3, QuantityRelation.AT_LEAST),
            _quantity(2, QuantityRelation.AT_LEAST),
            CandidateResolutionVerdict.ACCEPTED,
            id="at-least-inside-at-least",
        ),
        pytest.param(
            _quantity(1, QuantityRelation.EXACTLY),
            _quantity(3, QuantityRelation.AT_LEAST),
            CandidateResolutionVerdict.REJECTED,
            id="exactly-below-at-least",
        ),
        pytest.param(
            _quantity(4, QuantityRelation.EXACTLY),
            _quantity(3, QuantityRelation.AT_MOST),
            CandidateResolutionVerdict.REJECTED,
            id="exactly-above-at-most",
        ),
        pytest.param(
            _quantity(2, QuantityRelation.AT_LEAST),
            _quantity(5, QuantityRelation.AT_MOST),
            CandidateResolutionVerdict.ACCEPTED,
            id="partial-overlap",
        ),
        pytest.param(
            _quantity(3, QuantityRelation.AT_MOST),
            _quantity(1, QuantityRelation.EXACTLY),
            CandidateResolutionVerdict.ACCEPTED,
            id="at-most-wider-than-exactly",
        ),
        pytest.param(
            _quantity(None, QuantityRelation.VARIABLE),
            _quantity(2, QuantityRelation.AT_LEAST),
            CandidateResolutionVerdict.ACCEPTED,
            id="variable-produced",
        ),
        pytest.param(
            _quantity(2, QuantityRelation.EXACTLY),
            _quantity(None, QuantityRelation.VARIABLE),
            CandidateResolutionVerdict.ACCEPTED,
            id="variable-constraint",
        ),
    ),
)
def test_mana_value_intervals_compare_produced_against_selected(
    source_mana_value: CapabilityQuantity | None,
    target_mana_value: CapabilityQuantity | None,
    verdict: CandidateResolutionVerdict,
) -> None:
    resolution = _route_outcome(
        "discard-recursion-payoff",
        source=replace(
            DISCARD_SOURCE,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                mana_value=source_mana_value,
            ),
        ),
        target=replace(
            RECURSION_TARGET,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                mana_value=target_mana_value,
            ),
        ),
    )

    expected_basis = (
        CandidateResolutionBasis.MODEL
        if verdict is CandidateResolutionVerdict.UNRESOLVED
        else CandidateResolutionBasis.LOCAL
    )
    expected_reason = (
        LOCAL_UNRESOLVED_REASON if expected_basis is CandidateResolutionBasis.MODEL else None
    )
    if verdict is CandidateResolutionVerdict.REJECTED:
        expected_reason = _conflict_reason("mana_value")
    elif verdict is CandidateResolutionVerdict.ACCEPTED:
        expected_reason = LOCAL_PROVE_REASON

    assert resolution.basis is expected_basis
    assert resolution.verdict is verdict
    assert resolution.reason == expected_reason


def test_earlier_route_conflicts_win_over_later_route_and_qualifier_fields() -> None:
    undetermined_zone = _route_outcome(
        "discard-recursion-payoff",
        source=replace(DISCARD_SOURCE, action=CapabilityAction.DRAW),
        target=replace(RECURSION_TARGET, source_zone=None),
    )
    conflicting_types = _route_outcome(
        "discard-recursion-payoff",
        source=replace(
            DISCARD_SOURCE,
            action=CapabilityAction.DRAW,
            qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES),
        ),
    )

    assert undetermined_zone.verdict is CandidateResolutionVerdict.REJECTED
    assert undetermined_zone.reason == _conflict_reason("action")
    assert conflicting_types.verdict is CandidateResolutionVerdict.REJECTED
    assert conflicting_types.reason == _conflict_reason("action")


def test_role_anchored_route_fields_do_not_block_later_conflicts() -> None:
    resolution = _route_outcome(
        "discard-recursion-payoff",
        source=replace(DISCARD_SOURCE, destination_zone=None),
        target=replace(RECURSION_TARGET, action=CapabilityAction.DRAW),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason("action")


def test_conflicts_of_both_participants_follow_one_global_field_order() -> None:
    """A target action conflict outranks the source's later zone conflict, and a source card type
    conflict outranks the target's later token restriction conflict, whichever participant a route
    compares first.
    """
    target_action = _route_outcome(
        "discard-recursion-payoff",
        source=replace(DISCARD_SOURCE, destination_zone=CapabilityZone.EXILE),
        target=replace(RECURSION_TARGET, action=CapabilityAction.DRAW),
    )
    source_card_types = _route_outcome(
        "token-go-wide-payoff",
        source=ARTIFACT_SOURCE,
        target=replace(
            WIDE_TARGET,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                token_restriction=CapabilityTokenRestriction.NONTOKEN,
            ),
        ),
    )

    assert target_action.basis is CandidateResolutionBasis.LOCAL
    assert target_action.verdict is CandidateResolutionVerdict.REJECTED
    assert target_action.reason == _conflict_reason("action")
    assert source_card_types.basis is CandidateResolutionBasis.LOCAL
    assert source_card_types.verdict is CandidateResolutionVerdict.REJECTED
    assert source_card_types.reason == _conflict_reason("card_types")


def test_unresolved_earlier_fields_block_later_conflicts() -> None:
    """The decision walker settles an earlier field before a later field is examined: a comparison
    an earlier field cannot settle leaves the pair unresolved even when a later field conflicts,
    and an explicit conflict on an earlier field outranks a later missing-evidence signal.
    """
    unresolved = (CandidateResolutionVerdict.UNRESOLVED, LOCAL_UNRESOLVED_REASON)
    subtype_conflict = (
        CandidateResolutionVerdict.REJECTED,
        LOCAL_CONFLICT_REASON_TEMPLATE.format(field="subtype"),
    )
    mana_value_conflict = (
        CandidateResolutionVerdict.REJECTED,
        LOCAL_CONFLICT_REASON_TEMPLATE.format(field="mana_value"),
    )

    assert candidates_module._decide((("subtype", unresolved), ("mana_value", mana_value_conflict))) == unresolved
    assert candidates_module._decide((("subtype", subtype_conflict), ("mana_value", unresolved))) == subtype_conflict


def test_earlier_conflicts_outrank_later_missing_evidence() -> None:
    """A source card type contradiction is settled before the payoff's nontoken-only selection is
    read, so the reported reason is the earlier field's conflict.
    """
    resolution = _route_outcome(
        "fodder-dies-payoff",
        source=replace(FODDER_SOURCE, qualifier=_qualifier(card_types=ARTIFACT_CARD_TYPES)),
        target=replace(
            DEATH_TARGET,
            qualifier=_qualifier(token_restriction=CapabilityTokenRestriction.NONTOKEN),
        ),
    )

    assert resolution.basis is CandidateResolutionBasis.LOCAL
    assert resolution.verdict is CandidateResolutionVerdict.REJECTED
    assert resolution.reason == _conflict_reason("card_types")


def test_qualifier_checks_follow_their_fixed_field_order() -> None:
    conflicting = _route_outcome(
        "discard-recursion-payoff",
        source=replace(
            DISCARD_SOURCE,
            qualifier=_qualifier(
                card_types=ARTIFACT_CARD_TYPES,
                subtype="Goblin",
                mana_value=_quantity(1, QuantityRelation.EXACTLY),
            ),
        ),
        target=replace(
            RECURSION_TARGET,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                subtype="Elf",
                mana_value=_quantity(3, QuantityRelation.AT_LEAST),
            ),
        ),
    )
    conflicting_subtype = _route_outcome(
        "discard-recursion-payoff",
        source=replace(
            DISCARD_SOURCE,
            qualifier=_qualifier(subtype="Goblin"),
        ),
        target=replace(
            RECURSION_TARGET,
            qualifier=_qualifier(
                card_types=CREATURE_CARD_TYPES,
                subtype="Elf",
            ),
        ),
    )

    assert conflicting.verdict is CandidateResolutionVerdict.REJECTED
    assert conflicting.reason == _conflict_reason("card_types")
    assert conflicting_subtype.verdict is CandidateResolutionVerdict.REJECTED
    assert conflicting_subtype.reason == _conflict_reason("subtype")


def test_constructed_packages_resolve_one_to_one_in_canonical_order() -> None:
    package_set = construct_candidate_packages(_structured_results())
    resolution_set = resolve_candidate_packages(package_set.packages)

    assert [package.mechanism for package in package_set.packages] == [
        "token-death-payoff",
        "token-go-wide-payoff",
        "token-sacrifice-outlet",
    ]
    assert [resolution.identity for resolution in resolution_set.resolutions] == [
        package.identity for package in package_set.packages
    ]
    assert [resolution.package for resolution in resolution_set.resolutions] == list(
        package_set.packages
    )
    assert {resolution.basis for resolution in resolution_set.resolutions} == {
        CandidateResolutionBasis.LOCAL
    }
    assert {resolution.verdict for resolution in resolution_set.resolutions} == {
        CandidateResolutionVerdict.ACCEPTED
    }
    assert resolution_set.local_accepted == package_set.packages
    assert resolution_set.local_rejected == ()
    assert resolution_set.model_packages == ()
    assert resolution_set.omissions == ()


def test_resolution_set_splits_local_decisions_from_undeclared_mechanisms() -> None:
    packages = (
        *_mixed_outcome_packages(),
        _route_package(
            "undeclared-mechanism",
            replace(TOKEN_SOURCE, card_id=UNDECLARED_ID),
            replace(DEATH_TARGET, card_id=UNDECLARED_ID + 1),
        ),
    )
    resolution_set = resolve_candidate_packages(packages)
    by_source = {
        resolution.package.source.card_id: resolution
        for resolution in resolution_set.resolutions
    }

    assert len(packages) == 4
    assert by_source[TOKEN_ID].verdict is CandidateResolutionVerdict.REJECTED
    assert by_source[SACRIFICE_ID].verdict is CandidateResolutionVerdict.ACCEPTED
    assert by_source[ENABLER_ID].verdict is CandidateResolutionVerdict.ACCEPTED
    assert resolution_set.local_rejected == (by_source[TOKEN_ID].package,)
    assert resolution_set.local_accepted == (
        by_source[SACRIFICE_ID].package,
        by_source[ENABLER_ID].package,
    )
    assert resolution_set.model_packages == (packages[3],)
    assert resolution_set.omissions == ()


def test_rejections_expose_the_mechanism_and_reason_a_reporter_groups_by() -> None:
    resolution_set = resolve_candidate_packages(_rejection_packages())

    assert resolution_set.omissions == ()
    assert len(resolution_set.resolutions) == 6
    assert sorted(
        (resolution.package.mechanism, resolution.reason)
        for resolution in resolution_set.resolutions
    ) == [
        ("token-death-payoff", _conflict_reason("card_types")),
        ("token-death-payoff", _conflict_reason("token_restriction")),
        ("token-death-payoff", _conflict_reason("token_restriction")),
        ("token-go-wide-payoff", _conflict_reason("card_types")),
        ("token-go-wide-payoff", _conflict_reason("token_restriction")),
        ("token-go-wide-payoff", _conflict_reason("token_restriction")),
    ]


def test_repeated_resolution_is_deterministic() -> None:
    packages = _mixed_outcome_packages()

    first = resolve_candidate_packages(packages)
    second = resolve_candidate_packages(packages)

    assert first == second
    assert first.to_json() == second.to_json()

    payload = first.to_json()

    assert json.loads(json.dumps(payload)) == payload
    assert set(payload) == {"resolutions", "omissions"}
    assert payload["omissions"] == []
    assert payload["resolutions"] == [
        resolution.to_json() for resolution in first.resolutions
    ]
    assert all(
        set(item) == {"package", "basis", "verdict", "reason"}
        for item in payload["resolutions"]
    )
    assert {item["basis"] for item in payload["resolutions"]} == {"local"}
    assert {item["verdict"] for item in payload["resolutions"]} == {
        "accepted",
        "rejected",
    }


@pytest.mark.parametrize(("mechanism", "source", "target"), UNROUTABLE_MECHANISM_CASES)
def test_mechanisms_without_a_route_never_resolve_locally(
    mechanism: str,
    source: RouteParticipant,
    target: RouteParticipant,
) -> None:
    resolution = _only_resolution(_route_package(mechanism, source, target))

    assert resolution.basis is CandidateResolutionBasis.MODEL
    assert resolution.verdict is CandidateResolutionVerdict.UNRESOLVED
    assert resolution.reason == LOCAL_UNRESOLVED_REASON


def test_declared_mechanisms_resolve_the_packages_that_carry_their_declared_roles() -> None:
    """A declared mechanism resolves locally exactly when its participants carry the roles its
    rule links, and that link's route then reads their structured parameters.
    """
    accepted = _only_resolution(_route_package("fodder-dies-payoff", FODDER_SOURCE, DEATH_TARGET))
    rejected = _only_resolution(
        _route_package(
            "fodder-dies-payoff",
            replace(FODDER_SOURCE, action=CapabilityAction.CREATE),
            DEATH_TARGET,
        )
    )

    assert accepted.basis is CandidateResolutionBasis.LOCAL
    assert accepted.verdict is CandidateResolutionVerdict.ACCEPTED
    assert accepted.reason == LOCAL_PROVE_REASON
    assert rejected.basis is CandidateResolutionBasis.LOCAL
    assert rejected.verdict is CandidateResolutionVerdict.REJECTED
    assert rejected.reason == _conflict_reason("action")


@pytest.mark.parametrize(
    ("mechanism", "source", "target", "expected_message"),
    (
        pytest.param(
            "fodder-dies-payoff",
            TOKEN_SOURCE,
            DEATH_TARGET,
            "fodder-dies-payoff requires sacrifice_fodder as its source role "
            "and death_payoff as its target role.",
            id="unrelated-source-role",
        ),
        pytest.param(
            "fodder-dies-payoff",
            FODDER_SOURCE,
            WIDE_TARGET,
            "fodder-dies-payoff requires sacrifice_fodder as its source role "
            "and death_payoff as its target role.",
            id="unrelated-target-role",
        ),
        pytest.param(
            "token-go-wide-payoff",
            DEATH_TARGET,
            WIDE_TARGET,
            "token-go-wide-payoff requires token_maker as its source role "
            "and go_wide_payoff as its target role.",
            id="swapped-role-pair",
        ),
    ),
)
def test_declared_mechanisms_reject_packages_with_unrelated_roles(
    mechanism: str,
    source: RouteParticipant,
    target: RouteParticipant,
    expected_message: str,
) -> None:
    """A declared mechanism proves nothing when its participants carry other roles, so a package
    built that way is invalid candidate input rather than an accepted pair.
    """
    with pytest.raises(SetEnrichmentCandidatesError) as error:
        resolve_candidate_packages((_route_package(mechanism, source, target),))

    assert str(error.value) == expected_message


def test_undeclared_mechanisms_carry_any_roles_to_the_model() -> None:
    """A mechanism no rule declares is the supported residual path, so the resolver never requires
    a declared role pair of it: its packaged roles are irrelevant to the model verdict.
    """
    declared_roles = _only_resolution(
        _route_package("undeclared-mechanism", FODDER_SOURCE, DEATH_TARGET)
    )
    unrelated_roles = _only_resolution(
        _route_package("undeclared-mechanism", WIDE_TARGET, FODDER_SOURCE)
    )

    assert declared_roles.basis is CandidateResolutionBasis.MODEL
    assert declared_roles.verdict is CandidateResolutionVerdict.UNRESOLVED
    assert declared_roles.reason == LOCAL_UNRESOLVED_REASON
    assert unrelated_roles.basis is CandidateResolutionBasis.MODEL
    assert unrelated_roles.verdict is CandidateResolutionVerdict.UNRESOLVED
    assert unrelated_roles.reason == LOCAL_UNRESOLVED_REASON


def _resolution_set_with_two_packages() -> CandidateResolutionSet:
    """Build one canonically ordered resolution set holding two distinct resolutions."""
    packages = _mixed_outcome_packages()
    source_resolution = resolve_candidate_packages((packages[0],))
    second_resolution = resolve_candidate_packages(packages[1:])
    return CandidateResolutionSet(
        resolutions=source_resolution.resolutions + second_resolution.resolutions,
        omissions=(),
    )


@pytest.mark.parametrize(
    ("operation", "expected_message"),
    (
        pytest.param(
            lambda: CandidateResolution(
                package="capability-tokens",  # type: ignore[arg-type]
                basis=CandidateResolutionBasis.LOCAL,
                verdict=CandidateResolutionVerdict.ACCEPTED,
                reason=LOCAL_PROVE_REASON,
            ),
            "package must be a CandidatePackage.",
            id="non-package",
        ),
        pytest.param(
            lambda: CandidateResolution(
                package=_compatible_package(),
                basis="local",  # type: ignore[arg-type]
                verdict=CandidateResolutionVerdict.ACCEPTED,
                reason=LOCAL_PROVE_REASON,
            ),
            "basis must be a CandidateResolutionBasis.",
            id="raw-basis",
        ),
        pytest.param(
            lambda: CandidateResolution(
                package=_compatible_package(),
                basis=CandidateResolutionBasis.LOCAL,
                verdict="accepted",  # type: ignore[arg-type]
                reason=LOCAL_PROVE_REASON,
            ),
            "verdict must be a CandidateResolutionVerdict.",
            id="raw-verdict",
        ),
        pytest.param(
            lambda: CandidateResolution(
                package=_compatible_package(),
                basis=CandidateResolutionBasis.LOCAL,
                verdict=CandidateResolutionVerdict.UNRESOLVED,
                reason=LOCAL_UNRESOLVED_REASON,
            ),
            "local resolutions must be accepted or rejected.",
            id="local-unresolved",
        ),
        pytest.param(
            lambda: CandidateResolution(
                package=_compatible_package(),
                basis=CandidateResolutionBasis.MODEL,
                verdict=CandidateResolutionVerdict.ACCEPTED,
                reason=LOCAL_PROVE_REASON,
            ),
            "model resolutions must be unresolved.",
            id="model-accepted",
        ),
        pytest.param(
            lambda: CandidateResolution(
                package=_compatible_package(),
                basis=CandidateResolutionBasis.MODEL,
                verdict=CandidateResolutionVerdict.REJECTED,
                reason=_conflict_reason("action"),
            ),
            "model resolutions must be unresolved.",
            id="model-rejected",
        ),
        pytest.param(
            lambda: CandidateResolution(
                package=_compatible_package(),
                basis=CandidateResolutionBasis.LOCAL,
                verdict=CandidateResolutionVerdict.ACCEPTED,
                reason="   ",
            ),
            "reason must be a nonblank string.",
            id="blank-reason",
        ),
        pytest.param(
            lambda: CandidateResolutionSet(resolutions=[], omissions=()),  # type: ignore[arg-type]
            "resolutions must be a tuple.",
            id="list-resolutions",
        ),
        pytest.param(
            lambda: CandidateResolutionSet(resolutions=(object(),), omissions=()),  # type: ignore[arg-type]
            "resolutions must contain CandidateResolution records.",
            id="non-resolution-entry",
        ),
        pytest.param(
            lambda: CandidateResolutionSet(
                resolutions=(_resolution_set_with_two_packages().resolutions[0],) * 2,
                omissions=(),
            ),
            "resolutions contains duplicate entries.",
            id="duplicate-resolutions",
        ),
        pytest.param(
            lambda: CandidateResolutionSet(
                resolutions=tuple(
                    reversed(_resolution_set_with_two_packages().resolutions)
                ),
                omissions=(),
            ),
            "resolutions must be sorted canonically.",
            id="unsorted-resolutions",
        ),
        pytest.param(
            lambda: CandidateResolutionSet(
                resolutions=_resolution_set_with_two_packages().resolutions,
                omissions=[],  # type: ignore[arg-type]
            ),
            "omissions must be a tuple.",
            id="list-omissions",
        ),
        pytest.param(
            lambda: CandidateResolutionSet(
                resolutions=_resolution_set_with_two_packages().resolutions,
                omissions=(object(),),  # type: ignore[arg-type]
            ),
            "omissions must contain CandidateOmission records.",
            id="non-omission-entry",
        ),
        pytest.param(
            lambda: resolve_candidate_packages(["capability-tokens"]),  # type: ignore[arg-type]
            "packages must be a tuple.",
            id="list-packages",
        ),
        pytest.param(
            lambda: resolve_candidate_packages((object(),)),  # type: ignore[arg-type]
            "packages must contain CandidatePackage records.",
            id="non-package-entry",
        ),
        pytest.param(
            lambda: resolve_candidate_packages(
                (_compatible_package(), _compatible_package())
            ),
            "packages contains duplicate entries.",
            id="duplicate-packages",
        ),
        pytest.param(
            lambda: resolve_candidate_packages(
                tuple(
                    reversed(
                        construct_candidate_packages(_structured_results()).packages
                    )
                )
            ),
            "packages must be sorted canonically.",
            id="unsorted-packages",
        ),
    ),
)
def test_resolution_rejects_invalid_trusted_input(
    operation: Callable[[], object],
    expected_message: str,
) -> None:
    with pytest.raises(SetEnrichmentCandidatesError) as error:
        operation()
    assert str(error.value) == expected_message
