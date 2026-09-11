"""Pure bounded construction of compatible relationship candidates.
Index validated capabilities by role, pair declared enabler-to-payoff roles, and bound work.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any

from draftomen.semantic_capability_records import CardCapability
from draftomen.semantic_roles import Role
from draftomen.set_enrichment_extraction import CardCapabilityExtractionResult, ExtractionOutcome


class SetEnrichmentCandidatesError(ValueError):
    """Raised when trusted candidate-construction inputs violate their contract."""


class CandidateOutcome(StrEnum):
    """Terminal outcome of one pure bounded candidate construction."""

    COMPLETE = "complete"
    PARTIAL = "partial"


def _identifier(value: Any, field_name: str) -> str:
    """Validate and strip a nonblank identifier."""
    if not isinstance(value, str) or not value.strip():
        raise SetEnrichmentCandidatesError(f"{field_name} must be a nonblank string.")
    return value.strip()


def _canonical_json_bytes(value: Any) -> bytes:
    """Encode a value as canonical compact JSON bytes."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise SetEnrichmentCandidatesError("value cannot be encoded as canonical JSON.") from error


def _record_tuple(
    value: Any,
    *,
    field_name: str,
    expected_type: type,
    key: Callable[[Any], object],
) -> None:
    """Require exact record types in canonical key order."""
    if not isinstance(value, tuple):
        raise SetEnrichmentCandidatesError(f"{field_name} must be a tuple.")
    if any(type(item) is not expected_type for item in value):
        raise SetEnrichmentCandidatesError(
            f"{field_name} must contain {expected_type.__name__} records."
        )
    keys = [key(item) for item in value]
    if len(set(keys)) != len(keys):
        raise SetEnrichmentCandidatesError(f"{field_name} contains duplicate entries.")
    if keys != sorted(keys):
        raise SetEnrichmentCandidatesError(f"{field_name} must be sorted canonically.")


def _count(value: Any, field_name: str) -> int:
    """Validate a non-negative integer count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SetEnrichmentCandidatesError(f"{field_name} must be a non-negative integer.")
    return value


@dataclass(frozen=True, slots=True)
class RoleLink:
    """One declared enabler-to-payoff compatibility rule."""

    mechanism: str
    enabler: Role
    payoff: Role

    def __post_init__(self) -> None:
        object.__setattr__(self, "mechanism", _identifier(self.mechanism, "mechanism"))
        if not isinstance(self.enabler, Role):
            raise SetEnrichmentCandidatesError("enabler must be a Role.")
        if not isinstance(self.payoff, Role):
            raise SetEnrichmentCandidatesError("payoff must be a Role.")

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible role link object."""
        return {
            "mechanism": self.mechanism,
            "enabler": self.enabler.value,
            "payoff": self.payoff.value,
        }


ROLE_COMPATIBILITY_RULES: tuple[RoleLink, ...] = (
    RoleLink(mechanism="discard-recursion-payoff", enabler=Role.DISCARD_ENABLER, payoff=Role.RECURSION_PAYOFF),
    RoleLink(mechanism="fodder-dies-payoff", enabler=Role.SACRIFICE_FODDER, payoff=Role.DEATH_PAYOFF),
    RoleLink(mechanism="fodder-sacrifice-outlet", enabler=Role.SACRIFICE_FODDER, payoff=Role.SACRIFICE_OUTLET),
    RoleLink(mechanism="loot-recursion-payoff", enabler=Role.LOOT, payoff=Role.RECURSION_PAYOFF),
    RoleLink(mechanism="mill-graveyard-payoff", enabler=Role.SELF_MILL, payoff=Role.GRAVEYARD_PAYOFF),
    RoleLink(mechanism="recursion-graveyard-payoff", enabler=Role.RECURSION, payoff=Role.GRAVEYARD_PAYOFF),
    RoleLink(mechanism="token-death-payoff", enabler=Role.TOKEN_MAKER, payoff=Role.DEATH_PAYOFF),
    RoleLink(mechanism="token-go-wide-payoff", enabler=Role.TOKEN_MAKER, payoff=Role.GO_WIDE_PAYOFF),
    RoleLink(mechanism="token-sacrifice-outlet", enabler=Role.TOKEN_MAKER, payoff=Role.SACRIFICE_OUTLET),
)

for _link in ROLE_COMPATIBILITY_RULES:
    if not isinstance(_link, RoleLink):
        raise SetEnrichmentCandidatesError(
            "role compatibility rules must contain RoleLink records."
        )


@dataclass(frozen=True, slots=True)
class CandidateOmission:
    """Deterministic diagnostic for compatible pairs omitted by a bound."""

    mechanism: str
    omitted_pairs: int
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mechanism", _identifier(self.mechanism, "mechanism"))
        if (
            isinstance(self.omitted_pairs, bool)
            or not isinstance(self.omitted_pairs, int)
            or self.omitted_pairs < 1
        ):
            raise SetEnrichmentCandidatesError("omitted_pairs must be a positive integer.")
        object.__setattr__(self, "reason", _identifier(self.reason, "reason"))

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible candidate omission object."""
        return {
            "mechanism": self.mechanism,
            "omitted_pairs": self.omitted_pairs,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True, eq=False)
class CandidatePackage:
    """One bounded enabler-to-payoff candidate awaiting model validation."""

    mechanism: str
    source: CardCapability
    target: CardCapability
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mechanism", _identifier(self.mechanism, "mechanism"))
        if type(self.source) is not CardCapability:
            raise SetEnrichmentCandidatesError("source must be a CardCapability.")
        if type(self.target) is not CardCapability:
            raise SetEnrichmentCandidatesError("target must be a CardCapability.")
        if self.source.card_id == self.target.card_id:
            raise SetEnrichmentCandidatesError("source and target must be different cards.")
        object.__setattr__(self, "reason", _identifier(self.reason, "reason"))

    @property
    def identity(self) -> tuple[str, int, str, int, str]:
        """Return the stable duplicate identity of this candidate package."""
        # Distinct cards may reuse one finding_id, so both card ids are required
        # for an identity that cannot conflate different participants.
        return (
            self.mechanism,
            self.source.card_id,
            self.source.finding_id,
            self.target.card_id,
            self.target.finding_id,
        )

    def __eq__(self, other: object) -> bool:
        """Compare candidate packages by their stable duplicate identity."""
        if not isinstance(other, CandidatePackage):
            return NotImplemented
        return self.identity == other.identity

    def __hash__(self) -> int:
        """Hash the stable duplicate identity of this candidate package."""
        return hash(self.identity)

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible candidate package object."""
        return {
            "mechanism": self.mechanism,
            "reason": self.reason,
            "source": self.source.to_json(),
            "target": self.target.to_json(),
        }


MAX_EVALUATED_CANDIDATE_PAIRS = 4096
MAX_PAIR_WORK_OMITTED_REASON = "candidate pair work budget was exhausted"
CANDIDATE_REASON = "declared role compatibility between typed capabilities"


@dataclass(frozen=True, slots=True)
class CandidateBounds:
    """Explicit work bound for deterministic bounded candidate construction."""

    max_evaluated_pairs: int = MAX_EVALUATED_CANDIDATE_PAIRS

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_evaluated_pairs, bool)
            or not isinstance(self.max_evaluated_pairs, int)
            or self.max_evaluated_pairs < 1
        ):
            raise SetEnrichmentCandidatesError("max_evaluated_pairs must be a positive integer.")


@dataclass(frozen=True, slots=True)
class CandidatePackageSet:
    """Terminal result of one pure bounded candidate construction.
    malformed_extractions reports presence (0 or 1), never a repeated count.
    """

    outcome: CandidateOutcome
    packages: tuple[CandidatePackage, ...]
    omissions: tuple[CandidateOmission, ...]
    run_ids: tuple[str, ...]
    candidate_pairs: int
    evaluated_pairs: int
    malformed_extractions: int

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, CandidateOutcome):
            raise SetEnrichmentCandidatesError("outcome must be a CandidateOutcome.")
        _record_tuple(
            self.packages,
            field_name="packages",
            expected_type=CandidatePackage,
            key=lambda item: item.identity,
        )
        _record_tuple(
            self.omissions,
            field_name="omissions",
            expected_type=CandidateOmission,
            key=lambda item: item.mechanism,
        )
        if not isinstance(self.run_ids, tuple):
            raise SetEnrichmentCandidatesError("run_ids must be a tuple.")
        if any(not isinstance(item, str) or not item.strip() for item in self.run_ids):
            raise SetEnrichmentCandidatesError("run_ids must contain nonblank strings.")
        if list(self.run_ids) != sorted(set(self.run_ids)):
            raise SetEnrichmentCandidatesError("run_ids must be unique and ascending.")
        candidate_pairs = _count(self.candidate_pairs, "candidate_pairs")
        evaluated_pairs = _count(self.evaluated_pairs, "evaluated_pairs")
        malformed_extractions = _count(self.malformed_extractions, "malformed_extractions")
        if malformed_extractions > 1:
            raise SetEnrichmentCandidatesError("malformed_extractions must be 0 or 1.")
        if evaluated_pairs > candidate_pairs or sum(
            item.omitted_pairs for item in self.omissions
        ) != candidate_pairs - evaluated_pairs:
            raise SetEnrichmentCandidatesError(
                "omissions must account for every unevaluated candidate pair."
            )
        if self.outcome is CandidateOutcome.PARTIAL:
            if malformed_extractions == 0:
                raise SetEnrichmentCandidatesError(
                    "partial outcomes must report malformed extractions."
                )
        elif malformed_extractions > 0:
            raise SetEnrichmentCandidatesError(
                "complete outcomes must not report malformed extractions."
            )

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible candidate package set object."""
        return {
            "outcome": self.outcome.value,
            "packages": [item.to_json() for item in self.packages],
            "omissions": [item.to_json() for item in self.omissions],
            "run_ids": list(self.run_ids),
            "candidate_pairs": self.candidate_pairs,
            "evaluated_pairs": self.evaluated_pairs,
            "malformed_extractions": self.malformed_extractions,
        }


def construct_candidate_packages(
    results: Iterable[CardCapabilityExtractionResult],
    *,
    bounds: CandidateBounds = CandidateBounds(),
) -> CandidatePackageSet:
    """Construct bounded compatible relationship candidates from validated extraction results."""
    if not isinstance(bounds, CandidateBounds):
        raise SetEnrichmentCandidatesError("bounds must be a CandidateBounds.")
    entries = tuple(results)
    malformed_extractions = 0
    capabilities: list[CardCapability] = []
    for entry in entries:
        if type(entry) is not CardCapabilityExtractionResult:
            raise SetEnrichmentCandidatesError(
                "results must contain CardCapabilityExtractionResult records."
            )
        if entry.outcome is ExtractionOutcome.MALFORMED:
            # A malformed result retains no card identity, so repeated and distinct
            # malformed inputs are indistinguishable: report presence, not a count.
            malformed_extractions = 1
            continue
        capabilities.extend(entry.capabilities)
    index: dict[tuple[int, str], CardCapability] = {}
    for capability in capabilities:
        key = (capability.card_id, capability.finding_id)
        existing = index.get(key)
        if existing is None:
            index[key] = capability
        elif _canonical_json_bytes(existing.to_json()) != _canonical_json_bytes(
            capability.to_json()
        ):
            raise SetEnrichmentCandidatesError(
                "capabilities must not share an identity with different content."
            )
    buckets: dict[Role, list[CardCapability]] = {}
    for capability in index.values():
        buckets.setdefault(capability.role, []).append(capability)
    ordered_buckets = {
        role: tuple(sorted(items, key=lambda item: (item.finding_id, item.card_id)))
        for role, items in buckets.items()
    }
    candidate_pairs = 0
    evaluated_pairs = 0
    packages: list[CandidatePackage] = []
    omissions: list[CandidateOmission] = []
    for link in ROLE_COMPATIBILITY_RULES:
        sources = ordered_buckets.get(link.enabler, ())
        targets = ordered_buckets.get(link.payoff, ())
        if not sources or not targets:
            continue
        pairs = len(sources) * len(targets)
        candidate_pairs += pairs
        if evaluated_pairs + pairs > bounds.max_evaluated_pairs:
            omissions.append(
                CandidateOmission(
                    mechanism=link.mechanism,
                    omitted_pairs=pairs,
                    reason=MAX_PAIR_WORK_OMITTED_REASON,
                )
            )
            continue
        evaluated_pairs += pairs
        for source in sources:
            for target in targets:
                if source.card_id == target.card_id:
                    continue
                packages.append(
                    CandidatePackage(
                        mechanism=link.mechanism,
                        source=source,
                        target=target,
                        reason=CANDIDATE_REASON,
                    )
                )
    packages.sort(key=lambda item: item.identity)
    run_ids = tuple(sorted({capability.run_id for capability in index.values()}))
    return CandidatePackageSet(
        outcome=(
            CandidateOutcome.PARTIAL if malformed_extractions else CandidateOutcome.COMPLETE
        ),
        packages=tuple(packages),
        omissions=tuple(omissions),
        run_ids=run_ids,
        candidate_pairs=candidate_pairs,
        evaluated_pairs=evaluated_pairs,
        malformed_extractions=malformed_extractions,
    )


__all__ = [
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
]
