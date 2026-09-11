"""Behavior tests for bounded compatible candidate construction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
import json
import time

import pytest

from draftomen.semantic_capability_records import (
    CapabilityPrerequisite,
    CapabilityQuantity,
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
    MAX_EVALUATED_CANDIDATE_PAIRS,
    MAX_PAIR_WORK_OMITTED_REASON,
    ROLE_COMPATIBILITY_RULES,
    CandidateBounds,
    CandidateOmission,
    CandidateOutcome,
    CandidatePackage,
    CandidatePackageSet,
    RoleLink,
    SetEnrichmentCandidatesError,
    construct_candidate_packages,
)
from draftomen.set_enrichment_extraction import CardCapabilityExtractionResult, ExtractionOutcome


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

CAPABILITY_REVIEW_REASON = "capability requires semantic review beyond exact-source validation."
MALFORMED_REASON = "response does not match card capability extraction schema version 1."
SAME_CARD_REASON = "source and target must be different cards."

TOKEN_QUOTE = "Create two 1/1 colorless Soldier artifact creature tokens."
WIDE_QUOTE = "Creatures you control get +1/+0 for each other creature you control."
DEATH_QUOTE = "Whenever one or more creatures die, scry 1."
OUTLET_QUOTE = "Sacrifice a creature: Add {C}{C}."
FODDER_QUOTE = "When this creature dies, you gain 1 life."
DRAW_QUOTE = "Draw two cards."
COUNTER_QUOTE = "Counter target spell."
PREREQUISITE_QUOTE = "you may sacrifice a creature."


def _capability(
    *,
    finding_id: str,
    card_id: int,
    card_name: str,
    role: Role,
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
    return CardCapability(
        finding_id=finding_id,
        card_id=card_id,
        card_name=card_name,
        face_index=face_index,
        face_name=face_name,
        role=role,
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
        "MAX_EVALUATED_CANDIDATE_PAIRS",
        "MAX_PAIR_WORK_OMITTED_REASON",
        "ROLE_COMPATIBILITY_RULES",
        "CandidateBounds",
        "CandidateOmission",
        "CandidateOutcome",
        "CandidatePackage",
        "CandidatePackageSet",
        "RoleLink",
        "SetEnrichmentCandidatesError",
        "construct_candidate_packages",
    }
    assert issubclass(SetEnrichmentCandidatesError, ValueError)
    assert MAX_EVALUATED_CANDIDATE_PAIRS == 4096
    assert MAX_PAIR_WORK_OMITTED_REASON == "candidate pair work budget was exhausted"
    assert CANDIDATE_REASON == "declared role compatibility between typed capabilities"
    assert {member.value for member in CandidateOutcome} == {"complete", "partial"}
    assert CandidateBounds().max_evaluated_pairs == MAX_EVALUATED_CANDIDATE_PAIRS

    assert len(ROLE_COMPATIBILITY_RULES) == 9
    rules = [
        (rule.mechanism, rule.enabler, rule.payoff) for rule in ROLE_COMPATIBILITY_RULES
    ]
    assert rules == [
        ("discard-recursion-payoff", Role.DISCARD_ENABLER, Role.RECURSION_PAYOFF),
        ("fodder-dies-payoff", Role.SACRIFICE_FODDER, Role.DEATH_PAYOFF),
        ("fodder-sacrifice-outlet", Role.SACRIFICE_FODDER, Role.SACRIFICE_OUTLET),
        ("loot-recursion-payoff", Role.LOOT, Role.RECURSION_PAYOFF),
        ("mill-graveyard-payoff", Role.SELF_MILL, Role.GRAVEYARD_PAYOFF),
        ("recursion-graveyard-payoff", Role.RECURSION, Role.GRAVEYARD_PAYOFF),
        ("token-death-payoff", Role.TOKEN_MAKER, Role.DEATH_PAYOFF),
        ("token-go-wide-payoff", Role.TOKEN_MAKER, Role.GO_WIDE_PAYOFF),
        ("token-sacrifice-outlet", Role.TOKEN_MAKER, Role.SACRIFICE_OUTLET),
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
