"""Pure bounded construction and role-anchored local resolution of relationship candidates.
Index validated capabilities by role, pair declared enabler-to-payoff roles, and bound work. Resolve
every constructed pair locally: the declared role pair proves the mechanism, the structured action,
zone, and qualifier parameters decide the pair they can settle, and only a pair those parameters
leave open reaches paid validation. A package whose mechanism is declared in
`ROLE_COMPATIBILITY_RULES` must carry exactly that rule's declared role pair, and the resolver
raises `SetEnrichmentCandidatesError` for any other declared-mechanism package, because a declared
mechanism with unrelated participants proves nothing. No Oracle-text pattern is matched anywhere in
this module.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, TypeAlias

from draftomen.semantic_capability_records import (
    CapabilityAction,
    CapabilityCardType,
    CapabilityQualifier,
    CapabilityQuantity,
    CapabilityTokenRestriction,
    CapabilityZone,
    CardCapability,
    QuantityRelation,
)
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
    # `token-death-payoff` declares that a creature token dying satisfies a payoff that rewards
    # creatures dying: the enabler's own text need not show the token dying, and a payoff that
    # restricts its reward to nontoken creatures is not declared.
    RoleLink(mechanism="token-death-payoff", enabler=Role.TOKEN_MAKER, payoff=Role.DEATH_PAYOFF),
    RoleLink(mechanism="token-go-wide-payoff", enabler=Role.TOKEN_MAKER, payoff=Role.GO_WIDE_PAYOFF),
    RoleLink(mechanism="token-sacrifice-outlet", enabler=Role.TOKEN_MAKER, payoff=Role.SACRIFICE_OUTLET),
)

for _link in ROLE_COMPATIBILITY_RULES:
    if not isinstance(_link, RoleLink):
        raise SetEnrichmentCandidatesError(
            "role compatibility rules must contain RoleLink records."
        )

_ROLE_LINKS: Mapping[str, RoleLink] = {
    link.mechanism: link for link in ROLE_COMPATIBILITY_RULES
}

if len(_ROLE_LINKS) != len(ROLE_COMPATIBILITY_RULES):
    raise SetEnrichmentCandidatesError(
        "role compatibility rules must declare distinct mechanisms."
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


LOCAL_PROVE_REASON = "structured capability parameters prove compatibility"
LOCAL_CONFLICT_REASON_TEMPLATE = "structured capability parameters conflict: {field}"
LOCAL_UNRESOLVED_REASON = "structured capability parameters do not fully determine compatibility"


class CandidateResolutionBasis(StrEnum):
    """Origin of one per-pair candidate resolution."""

    LOCAL = "local"
    MODEL = "model"


class CandidateResolutionVerdict(StrEnum):
    """Terminal verdict of one per-pair candidate resolution."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class CandidateResolution:
    """One canonical local resolution of a constructed candidate package."""

    package: CandidatePackage
    basis: CandidateResolutionBasis
    verdict: CandidateResolutionVerdict
    reason: str

    def __post_init__(self) -> None:
        if type(self.package) is not CandidatePackage:
            raise SetEnrichmentCandidatesError("package must be a CandidatePackage.")
        if not isinstance(self.basis, CandidateResolutionBasis):
            raise SetEnrichmentCandidatesError("basis must be a CandidateResolutionBasis.")
        if not isinstance(self.verdict, CandidateResolutionVerdict):
            raise SetEnrichmentCandidatesError("verdict must be a CandidateResolutionVerdict.")
        if self.basis is CandidateResolutionBasis.LOCAL:
            if self.verdict is CandidateResolutionVerdict.UNRESOLVED:
                raise SetEnrichmentCandidatesError(
                    "local resolutions must be accepted or rejected."
                )
        elif self.verdict is not CandidateResolutionVerdict.UNRESOLVED:
            raise SetEnrichmentCandidatesError("model resolutions must be unresolved.")
        object.__setattr__(self, "reason", _identifier(self.reason, "reason"))

    @property
    def identity(self) -> tuple[str, int, str, int, str]:
        """Return the stable duplicate identity of the resolved candidate package."""
        return self.package.identity

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible candidate resolution object."""
        return {
            "package": self.package.to_json(),
            "basis": self.basis.value,
            "verdict": self.verdict.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CandidateResolutionSet:
    """Terminal result of one pure resolution of a canonically ordered package set.
    Every supplied package carries exactly one resolution; locally rejected and locally accepted
    packages are terminal, while unresolved packages still need model validation.
    """

    resolutions: tuple[CandidateResolution, ...]
    omissions: tuple[CandidateOmission, ...]

    def __post_init__(self) -> None:
        _record_tuple(
            self.resolutions,
            field_name="resolutions",
            expected_type=CandidateResolution,
            key=lambda item: item.identity,
        )
        _record_tuple(
            self.omissions,
            field_name="omissions",
            expected_type=CandidateOmission,
            key=lambda item: item.mechanism,
        )

    @property
    def model_packages(self) -> tuple[CandidatePackage, ...]:
        """Return the packages awaiting model validation, in canonical order."""
        return tuple(
            item.package
            for item in self.resolutions
            if item.basis is CandidateResolutionBasis.MODEL
        )

    @property
    def local_accepted(self) -> tuple[CandidatePackage, ...]:
        """Return the packages the structured parameters prove compatible, in canonical order."""
        return self._local_packages(CandidateResolutionVerdict.ACCEPTED)

    @property
    def local_rejected(self) -> tuple[CandidatePackage, ...]:
        """Return the packages the structured parameters prove incompatible, in canonical order."""
        return self._local_packages(CandidateResolutionVerdict.REJECTED)

    def _local_packages(
        self,
        verdict: CandidateResolutionVerdict,
    ) -> tuple[CandidatePackage, ...]:
        """Return the locally decided packages carrying one verdict, in canonical order."""
        return tuple(
            item.package
            for item in self.resolutions
            if item.basis is CandidateResolutionBasis.LOCAL and item.verdict is verdict
        )

    def to_json(self) -> dict[str, object]:
        """Return a fresh JSON-compatible candidate resolution set object."""
        return {
            "resolutions": [item.to_json() for item in self.resolutions],
            "omissions": [item.to_json() for item in self.omissions],
        }


# One structured comparison verdict with its reason, or None when the compared values are
# compatible. A routed mechanism reads its participants' fields, and reports an unresolved verdict
# only when those fields cannot settle the pair.
_CheckOutcome: TypeAlias = tuple[CandidateResolutionVerdict, str] | None

# The structured fields one comparison can decide. Both participants of a pair report into the same
# fields, so every comparison is ordered by one global precedence instead of by the order a route
# happens to inspect its participants.
_ACTION_FIELD = "action"
_ZONE_FIELD = "zone"
_CARD_TYPES_FIELD = "card_types"
_TOKEN_RESTRICTION_FIELD = "token_restriction"
_SUBTYPE_FIELD = "subtype"
_MANA_VALUE_FIELD = "mana_value"

# The global field precedence: every route field (`action`, `zone`, including each participant's
# source and destination zone) precedes every qualifier field, and the qualifier fields keep their
# directional order. An earlier field decides the pair before a later field is examined.
_FIELD_PRECEDENCE: tuple[str, ...] = (
    _ACTION_FIELD,
    _ZONE_FIELD,
    _CARD_TYPES_FIELD,
    _TOKEN_RESTRICTION_FIELD,
    _SUBTYPE_FIELD,
    _MANA_VALUE_FIELD,
)

# One comparison tagged with the field it decides, or None when the compared values are compatible.
_FieldCheck: TypeAlias = tuple[str, _CheckOutcome]
_RouteChecker: TypeAlias = Callable[[CardCapability, CardCapability], tuple[_FieldCheck, ...]]

# The one verdict a comparison the stored parameters cannot settle reports, shared by the fields
# that report it and by a mechanism no route declares.
_UNRESOLVED_OUTCOME: _CheckOutcome = (
    CandidateResolutionVerdict.UNRESOLVED,
    LOCAL_UNRESOLVED_REASON,
)


def _compatible(field_name: str) -> _FieldCheck:
    """Return the outcome of one field comparison the stored parameters do not contradict."""
    return (field_name, None)


def _unresolved(field_name: str) -> _FieldCheck:
    """Return the outcome of one field comparison the stored parameters cannot settle."""
    return (field_name, _UNRESOLVED_OUTCOME)


def _conflict(field_name: str) -> _FieldCheck:
    """Return the outcome of one conflicting structured field."""
    return (
        field_name,
        (
            CandidateResolutionVerdict.REJECTED,
            LOCAL_CONFLICT_REASON_TEMPLATE.format(field=field_name),
        ),
    )


def _check_action(capability: CardCapability, *expected: CapabilityAction) -> _FieldCheck:
    """Compare one participant action against the actions a route accepts.

    The declared role pair already proves the mechanism, so an unclassified action never
    contradicts the route; only an action the closed vocabulary states explicitly is a conflict.
    """
    if capability.action in expected or capability.action is CapabilityAction.OTHER:
        return _compatible(_ACTION_FIELD)
    return _conflict(_ACTION_FIELD)


def _check_zone(zone: CapabilityZone | None, expected: CapabilityZone) -> _FieldCheck:
    """Compare one participant zone against the zone an operated mechanism requires."""
    if zone is None or zone is expected:
        return _compatible(_ZONE_FIELD)
    return _conflict(_ZONE_FIELD)


def _check_required_card_type(
    qualifier: CapabilityQualifier,
    required: CapabilityCardType,
) -> _FieldCheck:
    """Require one participant qualifier to name a card type the mechanism needs.

    An unstated card type list leaves the requirement to the mechanism's declared role; only an
    explicit list the required type is missing from contradicts it.
    """
    if not qualifier.card_types or required in qualifier.card_types:
        return _compatible(_CARD_TYPES_FIELD)
    return _conflict(_CARD_TYPES_FIELD)


def _check_not_nontoken(qualifier: CapabilityQualifier) -> _FieldCheck:
    """Reject one participant that explicitly restricts the objects it acts on to nontokens."""
    if qualifier.token_restriction is CapabilityTokenRestriction.NONTOKEN:
        return _conflict(_TOKEN_RESTRICTION_FIELD)
    return _compatible(_TOKEN_RESTRICTION_FIELD)


def _check_card_types(
    source: CapabilityQualifier,
    target: CapabilityQualifier,
    *,
    implied_types: tuple[CapabilityCardType, ...] = (),
) -> _FieldCheck:
    """Compare the supplied card types against the card types the target selects.

    An unrestricted target accepts any supplied object. An unstated supply is read as the types the
    mechanism's role implies for the object its enabler supplies, and a route whose role implies
    none supplies an unrestricted object; only a disjoint comparison contradicts the pair.
    """
    if not target.card_types:
        return _compatible(_CARD_TYPES_FIELD)
    supplied = set(source.card_types) if source.card_types else set(implied_types)
    if not supplied or supplied & set(target.card_types):
        return _compatible(_CARD_TYPES_FIELD)
    return _conflict(_CARD_TYPES_FIELD)


def _check_token_restriction(
    source: CapabilityQualifier,
    target: CapabilityQualifier,
    *,
    implied_restriction: CapabilityTokenRestriction = CapabilityTokenRestriction.UNRESTRICTED,
) -> _FieldCheck:
    """Compare the supplied token restriction against the restriction the target selects.

    An unrestricted target accepts either restriction, and an unstated supply is read as the
    restriction the mechanism's role implies for the object its enabler supplies.
    """
    if target.token_restriction is CapabilityTokenRestriction.UNRESTRICTED:
        return _compatible(_TOKEN_RESTRICTION_FIELD)
    supplied = (
        implied_restriction
        if source.token_restriction is CapabilityTokenRestriction.UNRESTRICTED
        else source.token_restriction
    )
    if (
        supplied is CapabilityTokenRestriction.UNRESTRICTED
        or supplied is target.token_restriction
    ):
        return _compatible(_TOKEN_RESTRICTION_FIELD)
    return _conflict(_TOKEN_RESTRICTION_FIELD)


def _check_subtype(source: CapabilityQualifier, target: CapabilityQualifier) -> _FieldCheck:
    """Compare the supplied subtype against the subtype the target selects.

    An unstated subtype never contradicts a selection, because the mechanism's role already states
    that the enabler supplies the object the payoff selects.
    """
    if target.subtype is None or source.subtype is None:
        return _compatible(_SUBTYPE_FIELD)
    if source.subtype == target.subtype:
        return _compatible(_SUBTYPE_FIELD)
    return _conflict(_SUBTYPE_FIELD)


def _mana_value_interval(quantity: CapabilityQuantity) -> tuple[int, int | None] | None:
    """Convert one stated quantity into an inclusive mana-value interval, or None when unknown."""
    value = quantity.value
    if value is None:
        # A variable quantity states no bound of its own.
        return None
    if quantity.relation is QuantityRelation.EXACTLY:
        return (value, value)
    if quantity.relation is QuantityRelation.AT_LEAST:
        return (value, None)
    if quantity.relation is QuantityRelation.AT_MOST:
        # A mana value is never below zero, so an upper bound also bounds the interval from below.
        return (1, value)
    return None


def _check_mana_value(source: CapabilityQualifier, target: CapabilityQualifier) -> _FieldCheck:
    """Compare the supplied mana values against the mana value the target selects.

    Only a disjoint comparison contradicts the pair: an unstated supplied value, or one the stated
    intervals cannot place against the constraint, leaves the requirement to the mechanism's role.
    """
    constraint = target.mana_value
    if constraint is None or source.mana_value is None:
        return _compatible(_MANA_VALUE_FIELD)
    produced_interval = _mana_value_interval(source.mana_value)
    constraint_interval = _mana_value_interval(constraint)
    if produced_interval is None or constraint_interval is None:
        return _compatible(_MANA_VALUE_FIELD)
    low, high = produced_interval
    constraint_low, constraint_high = constraint_interval
    if (constraint_high is not None and low > constraint_high) or (
        high is not None and high < constraint_low
    ):
        return _conflict(_MANA_VALUE_FIELD)
    return _compatible(_MANA_VALUE_FIELD)


def _check_qualifiers(
    source: CapabilityQualifier,
    target: CapabilityQualifier,
    *,
    implied_types: tuple[CapabilityCardType, ...] = (),
    implied_restriction: CapabilityTokenRestriction = CapabilityTokenRestriction.UNRESTRICTED,
) -> tuple[_FieldCheck, ...]:
    """Return the supplied-object comparisons of one pair, each tagged with its own field."""
    return (
        _check_card_types(source, target, implied_types=implied_types),
        _check_token_restriction(source, target, implied_restriction=implied_restriction),
        _check_subtype(source, target),
        _check_mana_value(source, target),
    )


def _check_token_qualifiers(
    source: CapabilityQualifier,
    target: CapabilityQualifier,
    *,
    target_selects_creatures: bool,
) -> tuple[_FieldCheck, ...]:
    """Return the qualifier comparisons of one token pair, each tagged with its own field.

    A token maker's declared role states that it supplies creature tokens, so an unstated card type
    list or token restriction on that side is read as that object, and a payoff that excludes tokens
    contradicts the supply itself.
    """
    checks: list[_FieldCheck] = [
        _check_required_card_type(source, CapabilityCardType.CREATURE),
        _check_not_nontoken(source),
    ]
    if target_selects_creatures:
        checks.append(_check_required_card_type(target, CapabilityCardType.CREATURE))
    checks.extend(
        _check_qualifiers(
            source,
            target,
            implied_types=(CapabilityCardType.CREATURE,),
            implied_restriction=CapabilityTokenRestriction.TOKEN,
        )
    )
    return tuple(checks)


def _check_fodder_token_contradiction(
    source: CapabilityQualifier,
    target: CapabilityQualifier,
) -> _FieldCheck:
    """Reject a sacrifice fodder whose stated kind contradicts the kind the payoff selects.

    A sacrifice fodder's declared role states that it supplies its own creature body; when both
    participants state a token restriction, those explicit values settle the pair, so a nontoken
    fodder feeding a payoff that rewards nontokens only is exactly as compatible as a token fodder
    feeding a payoff that rewards tokens only.
    """
    if (
        source.token_restriction is CapabilityTokenRestriction.UNRESTRICTED
        or target.token_restriction is CapabilityTokenRestriction.UNRESTRICTED
        or source.token_restriction is target.token_restriction
    ):
        return _compatible(_TOKEN_RESTRICTION_FIELD)
    return _conflict(_TOKEN_RESTRICTION_FIELD)


def _check_fodder_qualifiers(
    source: CapabilityQualifier,
    target: CapabilityQualifier,
) -> tuple[_FieldCheck, ...]:
    """Return the qualifier comparisons of one sacrifice-fodder pair, tagged by their fields.

    A sacrifice fodder's declared role states that it supplies its own creature body, so an unstated
    card type list on that side is read as a creature, and the remaining comparisons run
    directionally from that supplied body to the objects the payoff selects.
    """
    return (
        _check_required_card_type(source, CapabilityCardType.CREATURE),
        _check_card_types(source, target, implied_types=(CapabilityCardType.CREATURE,)),
        _check_fodder_token_contradiction(source, target),
        _check_subtype(source, target),
        _check_mana_value(source, target),
    )


def _check_discard_recursion(
    source: CardCapability,
    target: CardCapability,
) -> tuple[_FieldCheck, ...]:
    """Return the ordered comparisons of one discard-recursion pair, shared by loot recursion."""
    return (
        _check_action(source, CapabilityAction.DISCARD),
        _check_zone(source.destination_zone, CapabilityZone.GRAVEYARD),
        _check_action(target, CapabilityAction.RETURN),
        _check_zone(target.source_zone, CapabilityZone.GRAVEYARD),
        # The discarded card must satisfy the card the payoff returns from the graveyard.
        *_check_qualifiers(source.qualifier, target.qualifier),
    )


def _check_mill_graveyard_payoff(
    source: CardCapability,
    target: CardCapability,
) -> tuple[_FieldCheck, ...]:
    """Return the ordered comparisons of one mill-graveyard pair.

    Only the milling enabler is checked: the payoff's own action and zone describe the reward it
    grants from the graveyard it observes, never the mechanism itself.
    """
    return (
        _check_action(source, CapabilityAction.MILL),
        _check_zone(source.destination_zone, CapabilityZone.GRAVEYARD),
        *_check_qualifiers(source.qualifier, target.qualifier),
    )


def _check_recursion_graveyard_payoff(
    source: CardCapability,
    target: CardCapability,
) -> tuple[_FieldCheck, ...]:
    """Return the ordered comparisons of one recursion-graveyard pair.

    The declared roles carry this mechanism: a recursion enabler moves cards out of a graveyard and
    a graveyard payoff rewards what a graveyard holds, so only the enabler's graveyard origin is
    checked and the payoff that states no contradiction is accepted on its role. The two
    capabilities never act on the same object, so no supplied-object comparison applies.
    """
    return (
        _check_action(source, CapabilityAction.RETURN, CapabilityAction.CAST),
        _check_zone(source.source_zone, CapabilityZone.GRAVEYARD),
    )


def _check_token_death_payoff(
    source: CardCapability,
    target: CardCapability,
) -> tuple[_FieldCheck, ...]:
    """Return the ordered comparisons of one token-death pair.

    The payoff's own zone describes where its reward lands, so only the objects it rewards dying
    and the zone they died from are checked.
    """
    return (
        _check_action(source, CapabilityAction.CREATE),
        _check_zone(source.destination_zone, CapabilityZone.BATTLEFIELD),
        _check_action(target, CapabilityAction.DIE),
        _check_zone(target.source_zone, CapabilityZone.BATTLEFIELD),
        *_check_token_qualifiers(
            source.qualifier,
            target.qualifier,
            target_selects_creatures=False,
        ),
    )


def _check_token_go_wide_payoff(
    source: CardCapability,
    target: CardCapability,
) -> tuple[_FieldCheck, ...]:
    """Return the ordered comparisons of one token-go-wide pair."""
    return (
        _check_action(source, CapabilityAction.CREATE),
        _check_zone(source.destination_zone, CapabilityZone.BATTLEFIELD),
        _check_action(target, CapabilityAction.CONTROL, CapabilityAction.COUNT),
        _check_zone(target.zone, CapabilityZone.BATTLEFIELD),
        *_check_token_qualifiers(
            source.qualifier,
            target.qualifier,
            target_selects_creatures=True,
        ),
    )


def _check_token_sacrifice_outlet(
    source: CardCapability,
    target: CardCapability,
) -> tuple[_FieldCheck, ...]:
    """Return the ordered comparisons of one token-sacrifice pair."""
    return (
        _check_action(source, CapabilityAction.CREATE),
        _check_zone(source.destination_zone, CapabilityZone.BATTLEFIELD),
        _check_action(target, CapabilityAction.SACRIFICE),
        _check_zone(target.zone, CapabilityZone.GRAVEYARD),
        _check_zone(target.source_zone, CapabilityZone.BATTLEFIELD),
        *_check_token_qualifiers(
            source.qualifier,
            target.qualifier,
            target_selects_creatures=True,
        ),
    )


def _check_fodder_dies_payoff(
    source: CardCapability,
    target: CardCapability,
) -> tuple[_FieldCheck, ...]:
    """Return the ordered comparisons of one sacrificed-fodder death pair.

    The payoff's own zone describes where its reward lands, so only the objects it rewards dying
    and the zone they died from are checked. The fodder role supplies its own creature body, never a
    token or a nontoken in particular, so the payoff's stated kind is compared with the fodder's
    stated kind instead of excluding any payoff that turns out to reward nontokens only.
    """
    return (
        _check_action(source, CapabilityAction.SACRIFICE),
        _check_zone(source.destination_zone, CapabilityZone.GRAVEYARD),
        _check_zone(source.source_zone, CapabilityZone.BATTLEFIELD),
        _check_action(target, CapabilityAction.DIE),
        _check_zone(target.source_zone, CapabilityZone.BATTLEFIELD),
        *_check_fodder_qualifiers(source.qualifier, target.qualifier),
    )


def _check_fodder_sacrifice_outlet(
    source: CardCapability,
    target: CardCapability,
) -> tuple[_FieldCheck, ...]:
    """Return the ordered comparisons of one fodder-sacrifice pair.

    Both participants sacrifice the fodder's own creature body, so the same directional qualifier
    comparisons apply as for a death payoff.
    """
    return (
        _check_action(source, CapabilityAction.SACRIFICE),
        _check_zone(source.destination_zone, CapabilityZone.GRAVEYARD),
        _check_zone(source.source_zone, CapabilityZone.BATTLEFIELD),
        _check_action(target, CapabilityAction.SACRIFICE),
        _check_zone(target.destination_zone, CapabilityZone.GRAVEYARD),
        _check_zone(target.source_zone, CapabilityZone.BATTLEFIELD),
        *_check_fodder_qualifiers(source.qualifier, target.qualifier),
    )


# Every declared mechanism is routed: the declared role pair proves the mechanism and the route
# reads the structured fields. A mechanism absent here is undeclared and stays unresolved for the
# model.
_MECHANISM_ROUTES: Mapping[str, _RouteChecker] = {
    "discard-recursion-payoff": _check_discard_recursion,
    "fodder-dies-payoff": _check_fodder_dies_payoff,
    "fodder-sacrifice-outlet": _check_fodder_sacrifice_outlet,
    "loot-recursion-payoff": _check_discard_recursion,
    "mill-graveyard-payoff": _check_mill_graveyard_payoff,
    "recursion-graveyard-payoff": _check_recursion_graveyard_payoff,
    "token-death-payoff": _check_token_death_payoff,
    "token-go-wide-payoff": _check_token_go_wide_payoff,
    "token-sacrifice-outlet": _check_token_sacrifice_outlet,
}

if not set(_MECHANISM_ROUTES).issubset({link.mechanism for link in ROLE_COMPATIBILITY_RULES}):
    raise SetEnrichmentCandidatesError("mechanism routes must describe declared mechanisms.")


def _decide(checks: tuple[_FieldCheck, ...]) -> _CheckOutcome:
    """Return the outcome of the first structured field the global precedence settles.

    Both participants report into one set of fields, so the reported reason never depends on which
    participant a route inspects first: an explicit conflict on one participant outranks a
    missing-evidence signal on the other within the same field, a comparison an earlier field cannot
    settle leaves the pair unresolved even when a later field conflicts, and an explicit conflict on
    an earlier field outranks any later field.
    """
    for field_name in _FIELD_PRECEDENCE:
        conflict: _CheckOutcome = None
        unresolved: _CheckOutcome = None
        for checked_field, outcome in checks:
            if outcome is None or checked_field != field_name:
                continue
            if outcome[0] is CandidateResolutionVerdict.REJECTED:
                conflict = outcome
                break
            if unresolved is None:
                unresolved = outcome
        if conflict is not None:
            return conflict
        if unresolved is not None:
            return unresolved
    return None


def _require_declared_roles(package: CandidatePackage) -> None:
    """Require a declared mechanism's package to carry exactly that rule's declared role pair.

    A mechanism no rule declares is the supported residual path: it constructs packages whose
    mechanism has no role rule and stays unresolved for the model whatever roles it carries.
    """
    link = _ROLE_LINKS.get(package.mechanism)
    if link is not None and (
        package.source.role is not link.enabler or package.target.role is not link.payoff
    ):
        raise SetEnrichmentCandidatesError(
            f"{package.mechanism} requires {link.enabler.value} as its source role "
            f"and {link.payoff.value} as its target role."
        )


def _resolution(package: CandidatePackage, outcome: _CheckOutcome) -> CandidateResolution:
    """Return the canonical resolution one comparison outcome yields for one package."""
    if outcome is None:
        return CandidateResolution(
            package=package,
            basis=CandidateResolutionBasis.LOCAL,
            verdict=CandidateResolutionVerdict.ACCEPTED,
            reason=LOCAL_PROVE_REASON,
        )
    verdict, reason = outcome
    return CandidateResolution(
        package=package,
        basis=(
            CandidateResolutionBasis.LOCAL
            if verdict is CandidateResolutionVerdict.REJECTED
            else CandidateResolutionBasis.MODEL
        ),
        verdict=verdict,
        reason=reason,
    )


def resolve_candidate_packages(
    packages: tuple[CandidatePackage, ...],
) -> CandidateResolutionSet:
    """Resolve every constructed candidate package from its structured capability parameters.
    The returned set holds one resolution per supplied package, in canonical package order. A
    package whose mechanism is declared must carry that rule's declared role pair, so a declared
    mechanism with unrelated participants is rejected as invalid candidate input.
    """
    _record_tuple(
        packages,
        field_name="packages",
        expected_type=CandidatePackage,
        key=lambda item: item.identity,
    )
    resolutions: list[CandidateResolution] = []
    for package in packages:
        _require_declared_roles(package)
        checker = _MECHANISM_ROUTES.get(package.mechanism)
        outcome = (
            _UNRESOLVED_OUTCOME
            if checker is None
            else _decide(checker(package.source, package.target))
        )
        resolutions.append(_resolution(package, outcome))
    return CandidateResolutionSet(resolutions=tuple(resolutions), omissions=())


__all__ = [
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
]
