"""Deterministic role and projected-deck accounting for Limited pools.
Keep stage-aware projection separate from completed-pool construction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, TypeAlias

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import COLOR_PAIRS, DECK_BUILDER, SPLASH
from draftomen.events import EXPECTED_PACK_COUNT, EXPECTED_PICKS_PER_PACK, EXPECTED_TOTAL_PICKS
from draftomen.semantic_capability_records import (
    CapabilityQuantity,
    CapabilityZone,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import card_source_sha256
from draftomen.semantic_relationship_records import (
    RelationshipParticipant,
    RelationshipPrerequisite,
    RelationshipPrerequisiteProjection,
    RelationshipZone,
    _death_payoff_clause,
    _declared_zone,
    _discard_clause,
    _draw_clause,
    _fodder_clause,
    _outlet_clause,
    _produced_token_clause,
    _return_clause,
    _self_mill_clause,
    _wide_payoff_clause,
)
from draftomen.semantic_roles import Role, RoleAssignment, ResolutionResult, resolve_card_roles
from draftomen.set_enrichment_candidates import ROLE_COMPATIBILITY_RULES
from draftomen.seventeen import SeventeenLandsData
from draftomen.splash import (
    COLOR_ORDER,
    SplashState,
    card_is_castable_in_pair,
    infer_splash_state,
    splash_requirement,
)

if TYPE_CHECKING:
    from draftomen.set_profile import PairProfile, SetProfile

LedgerNumber: TypeAlias = float
LedgerPairs: TypeAlias = tuple[tuple[str, LedgerNumber], ...]
TOTAL_DRAFT_PICKS = EXPECTED_TOTAL_PICKS
PICKS_PER_PACK = EXPECTED_PICKS_PER_PACK

ProjectionUnit: TypeAlias = tuple[int, CardInfo, tuple[RoleAssignment, ...]]


ROLE_PRIORITY_WEIGHTS: Mapping[Role, float] = {
    Role.HARD_REMOVAL: 5.0,
    Role.DAMAGE_REMOVAL: 4.0,
    Role.DISABLING_REMOVAL: 4.0,
    Role.CONDITIONAL_REMOVAL: 3.0,
    Role.BOUNCE: 3.0,
    Role.DRAW: 2.5,
    Role.EXTRA_DRAW_ENABLER: 2.0,
    Role.DRAW_SECOND_PAYOFF: 1.5,
    Role.CANTRIP: 2.0,
    Role.FIXING: 2.0,
    Role.MANA_PRODUCER: 1.5,
    Role.TOKEN_MAKER: 2.0,
    Role.GO_WIDE_ENABLER: 2.0,
    Role.GO_WIDE_PAYOFF: 1.5,
    Role.TYPAL_MEMBER: 2.0,
    Role.TYPAL_PAYOFF: 1.5,
    Role.SACRIFICE_FODDER: 2.0,
    Role.SACRIFICE_OUTLET: 2.0,
    Role.DEATH_PAYOFF: 1.5,
    Role.LOW_COST_CREATURE: 2.5,
    Role.EVASIVE_THREAT: 2.0,
    Role.LARGE_CREATURE: 1.0,
}
CARD_ADVANTAGE_ROLES = frozenset(
    {
        Role.DRAW,
        Role.EXTRA_DRAW_ENABLER,
        Role.LOOT,
        Role.RUMMAGE,
        Role.CANTRIP,
        Role.RECURSION,
        Role.CARD_SELECTION,
        Role.TUTOR,
    }
)
FIXING_ROLES = frozenset({Role.FIXING, Role.MANA_PRODUCER})


REMOVAL_ROLES = frozenset(
    {
        Role.HARD_REMOVAL,
        Role.DAMAGE_REMOVAL,
        Role.DISABLING_REMOVAL,
        Role.CONDITIONAL_REMOVAL,
        Role.BOUNCE,
        Role.TEMPORARY_TAP,
    }
)
REMOVAL_ROLE_KINDS: Mapping[Role, str] = {
    Role.HARD_REMOVAL: "destroy",
    Role.DAMAGE_REMOVAL: "damage",
    Role.DISABLING_REMOVAL: "disable",
    Role.BOUNCE: "bounce",
}

REMOVAL_KINDS = ("destroy", "exile", "damage", "disable", "conditional", "bounce", "temporary")
# These are deliberately distinct: unconditional hard answers are not equivalent
# to damage, disabling, bounce, or temporary interaction in a Limited pool.
REMOVAL_WEIGHTS: Mapping[str, float] = {
    "destroy": 1.0,
    "exile": 1.0,
    "damage": 0.75,
    "disable": 0.6,
    "conditional": 0.5,
    "bounce": 0.7,
    "temporary": 0.4,
}
GENERIC_TARGET_CONFIDENCE = 0.25

GENERIC_ROLE_TARGETS: Mapping[Role, float] = {
    Role.HARD_REMOVAL: 1.0,
    Role.DAMAGE_REMOVAL: 1.0,
    Role.DISABLING_REMOVAL: 1.0,
    Role.CONDITIONAL_REMOVAL: 1.0,
    Role.BOUNCE: 1.0,
    Role.TEMPORARY_TAP: 1.0,
    Role.DRAW: 1.0,
    Role.CANTRIP: 1.0,
    Role.CARD_SELECTION: 1.0,
    Role.FIXING: 2.0,
    Role.MANA_PRODUCER: 2.0,
    Role.LOW_COST_CREATURE: 4.0,
    Role.LARGE_CREATURE: 2.0,
    Role.EVASIVE_THREAT: 2.0,
}
PACKAGE_ROLES: Mapping[str, tuple[tuple[Role, ...], tuple[Role, ...]]] = {
    "draw": ((Role.EXTRA_DRAW_ENABLER,), (Role.DRAW_SECOND_PAYOFF,)),
    "go_wide": ((Role.TOKEN_MAKER, Role.GO_WIDE_ENABLER), (Role.GO_WIDE_PAYOFF,)),
    "typal": ((Role.TYPAL_MEMBER,), (Role.TYPAL_PAYOFF,)),
    "sacrifice": ((Role.SACRIFICE_FODDER, Role.SACRIFICE_OUTLET), (Role.DEATH_PAYOFF,)),
    "graveyard": ((Role.SELF_MILL, Role.DISCARD_ENABLER, Role.GRAVEYARD_FILLER), (Role.GRAVEYARD_PAYOFF,)),
    "artifact": ((Role.ARTIFACT_ENABLER,), (Role.ARTIFACT_PAYOFF,)),
    "enchantment": ((Role.ENCHANTMENT_ENABLER,), (Role.ENCHANTMENT_PAYOFF,)),
    "equipment": ((Role.EQUIPMENT,), (Role.EQUIPMENT_PAYOFF,)),
    "threshold": ((Role.POWER_THRESHOLD_ENABLER,), (Role.POWER_THRESHOLD_PAYOFF,)),
}


#
# Canonical prerequisite text is built only from validated enum values, IDs, and
# quantities so no free-text claim, quote, or guide text can reach pick evidence.
_PREREQUISITE_TEXT_PATTERN = re.compile(
    r"(?:source|target):[a-z_]+/[a-z_]+/[a-z_]+(?:;[a-z_]+=[^\s;]+)*"
)

class LedgerMode(StrEnum):
    """Identify whether a ledger has pre-pick stage context or is final."""

    PRE_PICK_PROJECTION = "pre_pick_projection"
    COMPLETED_POOL = "completed_pool"


PRE_PICK_PROJECTION = LedgerMode.PRE_PICK_PROJECTION
COMPLETED_POOL = LedgerMode.COMPLETED_POOL


@dataclass(frozen=True, slots=True)
class LedgerStage:
    """Coordinates and remaining opportunities for a pre-pick evaluation."""

    pack_number: int
    pick_number: int
    global_pick_index: int
    estimated_remaining_picks: int

    def __post_init__(self) -> None:
        values = (
            self.pack_number,
            self.pick_number,
            self.global_pick_index,
            self.estimated_remaining_picks,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("Ledger stage coordinates must be integers.")
        if not 0 <= self.pack_number < EXPECTED_PACK_COUNT:
            raise ValueError(
                f"Ledger pack_number must be within {EXPECTED_PACK_COUNT} draft packs."
            )
        if not 0 <= self.pick_number < PICKS_PER_PACK:
            raise ValueError(
                f"Ledger pick_number must be within a {PICKS_PER_PACK}-pick pack."
            )
        expected_index = self.pack_number * PICKS_PER_PACK + self.pick_number + 1
        if self.global_pick_index != expected_index:
            raise ValueError(
                "Ledger global_pick_index does not match pack_number/pick_number."
            )
        if self.estimated_remaining_picks < 0:
            raise ValueError("Ledger estimated_remaining_picks must be non-negative.")
        if self.estimated_remaining_picks > TOTAL_DRAFT_PICKS:
            raise ValueError(
                "Ledger estimated_remaining_picks exceeds the draft's total picks."
            )

    @property
    def pack(self) -> int:
        """Return the zero-based pack coordinate."""

        return self.pack_number

    @property
    def pick(self) -> int:
        """Return the zero-based within-pack pick coordinate."""

        return self.pick_number


@dataclass(frozen=True, slots=True)
class TargetCoverage:
    """Profile-relative role coverage and bounded diminishing-return evidence."""

    name: str
    count: float
    soft_minimum: float
    preferred_minimum: float
    preferred_maximum: float
    upper_soft_target: float
    coverage: float
    deficit: float
    saturation: float
    confidence: float
    diminishing_returns: float


@dataclass(frozen=True, slots=True)
class PackageSupport:
    """Enabler/payoff balance for one reusable semantic package."""

    package: str
    enabler_count: int
    payoff_count: int
    supported_payoff_count: int
    unsupported_payoff_count: int
    density: float


@dataclass(frozen=True, slots=True)
class RemovalContribution:
    """One normalized removal subtype contribution."""

    kind: str
    count: int
    value: float


@dataclass(frozen=True, slots=True)
class RelationshipSupport:
    """One exact, source-bound typed relationship satisfied by the projected pool.
    The source participant is a drafted card that survives the likely-deck
    projection; the target participant is joined later by exact offered grp_id.
    """

    finding_id: str
    mechanism: str
    source_card_id: int
    source_card_name: str
    source_card_count: int
    source_role: Role
    source_role_confidence: float
    target_card_id: int
    target_card_name: str
    target_role: Role
    target_role_confidence: float
    satisfied_prerequisites: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("finding_id", "mechanism", "source_card_name", "target_card_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Relationship support {field_name} must be a non-empty string.")
        for field_name in ("source_card_id", "source_card_count", "target_card_id"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Relationship support {field_name} must be a positive integer.")
        for field_name in ("source_role", "target_role"):
            if not isinstance(getattr(self, field_name), Role):
                raise ValueError(f"Relationship support {field_name} must be a Role.")
        for field_name in ("source_role_confidence", "target_role_confidence"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"Relationship support {field_name} must be bounded from 0 to 1.")
        prerequisites = self.satisfied_prerequisites
        if (
            not isinstance(prerequisites, tuple)
            or not prerequisites
            or any(
                not isinstance(item, str)
                or _PREREQUISITE_TEXT_PATTERN.fullmatch(item) is None
                for item in prerequisites
            )
        ):
            raise ValueError(
                "Relationship support satisfied_prerequisites must contain canonical prerequisite text."
            )

    def to_json(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "mechanism": self.mechanism,
            "source": {
                "count": self.source_card_count,
                "grp_id": self.source_card_id,
                "name": self.source_card_name,
                "role": self.source_role.value,
                "role_confidence": self.source_role_confidence,
            },
            "target": {
                "grp_id": self.target_card_id,
                "name": self.target_card_name,
                "role": self.target_role.value,
                "role_confidence": self.target_role_confidence,
            },
            "satisfied_prerequisites": list(self.satisfied_prerequisites),
        }


def _participant_identity(
    participant: RelationshipParticipant,
    *,
    set_profile: SetProfile,
    card_database: CardDatabase,
    projected: Mapping[int, tuple[CardInfo, int, ResolutionResult]],
) -> tuple[CardInfo, float] | None:
    card = card_database.cards.get(participant.card_id)
    if card is None or card.unknown:
        return None
    if card.set_code != set_profile.set_code:
        return None
    if card_source_sha256(card) != participant.card_source_sha256:
        return None
    if not _face_binding_matches(participant=participant, card=card):
        return None
    projected_entry = projected.get(participant.card_id)
    resolution = (
        projected_entry[2]
        if projected_entry is not None
        else resolve_card_roles(card, profile=set_profile.role_profile)
    )
    if resolution.source != "compiled_profile":
        return None
    confidence = next(
        (
            assignment.confidence
            for assignment in resolution.assignments
            if assignment.role == participant.role
        ),
        None,
    )
    if confidence is None:
        return None
    return card, float(confidence)


@dataclass(frozen=True, slots=True)
class PoolRoleLedger:
    """Immutable role ledger for a projected or completed pool.
    Pre-pick ledgers always carry explicit stage context; completed ledgers never do.
    """

    mode: LedgerMode
    pool_size: int
    unique_card_count: int
    likely_pair: str | None
    playable_count: int
    cut_count: int
    creature_count: int
    curve_bands: LedgerPairs
    fixing_count: int
    card_advantage_count: int
    role_counts: LedgerPairs
    enabler_counts: LedgerPairs
    payoff_counts: LedgerPairs
    supported_payoff_counts: LedgerPairs
    unsupported_payoff_counts: LedgerPairs
    package_density: LedgerPairs
    removal_contributions: tuple[RemovalContribution, ...]
    target_coverage: tuple[TargetCoverage, ...]
    target_deficit: LedgerPairs
    target_saturation: LedgerPairs
    target_confidence: LedgerPairs
    target_diminishing_returns: LedgerPairs
    urgency: float
    stage: LedgerStage | None = None
    profile_source: str = "generic"
    profile_fingerprint: str | None = None
    relationship_support: tuple[RelationshipSupport, ...] = ()

    def __post_init__(self) -> None:
        try:
            mode = self.mode if isinstance(self.mode, LedgerMode) else LedgerMode(self.mode)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Unsupported ledger mode {self.mode!r}.") from error
        object.__setattr__(self, "mode", mode)
        if mode is LedgerMode.PRE_PICK_PROJECTION and self.stage is None:
            raise ValueError("Pre-pick projection requires stage context.")
        if mode is LedgerMode.COMPLETED_POOL and self.stage is not None:
            raise ValueError("Completed-pool ledger cannot contain stage context.")
        if self.pool_size < 0 or self.unique_card_count < 0:
            raise ValueError("Ledger card counts must be non-negative.")
        if self.playable_count < 0 or self.cut_count < 0 or self.creature_count < 0:
            raise ValueError("Ledger structural counts must be non-negative.")
        if not 0.0 <= self.urgency <= 1.0:
            raise ValueError("Ledger urgency must be bounded from 0 to 1.")
        if any(not isinstance(item, RelationshipSupport) for item in self.relationship_support):
            raise ValueError("Ledger relationship support must contain RelationshipSupport records.")

    @property
    def pool_before_pick(self) -> bool:
        """Return whether this ledger represents a pre-pick pool."""

        return self.mode is LedgerMode.PRE_PICK_PROJECTION

    @property
    def remaining_picks(self) -> int | None:
        """Return remaining opportunities, absent for completed construction."""

        return None if self.stage is None else self.stage.estimated_remaining_picks

    @property
    def global_pick_index(self) -> int | None:
        """Return the one-based stage index, absent for completed construction."""

        return None if self.stage is None else self.stage.global_pick_index

    @property
    def role_counts_map(self) -> dict[str, float]:
        """Return role counts as a convenient immutable-value copy."""

        return dict(self.role_counts)

    @property
    def removal_by_kind(self) -> LedgerPairs:
        """Return normalized removal values in documented stable kind order."""

        return tuple((item.kind, item.value) for item in self.removal_contributions)

    @property
    def effective_removal(self) -> float:
        """Return additive normalized removal evidence across all subtypes."""

        return _round(sum(item.value for item in self.removal_contributions))

    @property
    def removal_saturation(self) -> float:
        """Return bounded saturation across all removal subtypes."""

        return _round(_clamp(self.effective_removal))


    @property
    def removal_saturation_by_kind(self) -> LedgerPairs:
        """Return bounded per-subtype removal saturation values."""

        return tuple(
            (item.kind, _round(_clamp(item.value)))
            for item in self.removal_contributions
        )

    @property
    def curve(self) -> LedgerPairs:
        """Return the mana-curve bands."""

        return self.curve_bands

    @property
    def target_coverage_map(self) -> dict[str, TargetCoverage]:
        """Return target entries indexed by deterministic target name."""

        return {item.name: item for item in self.target_coverage}

    def to_json(self) -> dict[str, object]:
        """Serialize enough deterministic evidence for audit and adapter use."""

        return {
            "card_advantage_count": self.card_advantage_count,
            "creature_count": self.creature_count,
            "cut_count": self.cut_count,
            "curve_bands": [[name, value] for name, value in self.curve_bands],
            "effective_removal": self.effective_removal,
            "enabler_counts": [[name, value] for name, value in self.enabler_counts],
            "fixing_count": self.fixing_count,
            "likely_pair": self.likely_pair,
            "mode": self.mode.value,
            "package_density": [[name, value] for name, value in self.package_density],
            "payoff_counts": [[name, value] for name, value in self.payoff_counts],
            "playable_count": self.playable_count,
            "pool_size": self.pool_size,
            "profile_fingerprint": self.profile_fingerprint,
            "profile_source": self.profile_source,
            "removal_contributions": [
                {"count": item.count, "kind": item.kind, "value": item.value}
                for item in self.removal_contributions
            ],
            "role_counts": [[name, value] for name, value in self.role_counts],
            "stage": None if self.stage is None else {
                "estimated_remaining_picks": self.stage.estimated_remaining_picks,
                "global_pick_index": self.stage.global_pick_index,
                "pack_number": self.stage.pack_number,
                "pick_number": self.stage.pick_number,
            },
            "supported_payoff_counts": [
                [name, value] for name, value in self.supported_payoff_counts
            ],
            "target_coverage": [
                {
                    "confidence": item.confidence,
                    "coverage": item.coverage,
                    "deficit": item.deficit,
                    "diminishing_returns": item.diminishing_returns,
                    "name": item.name,
                    "preferred_maximum": item.preferred_maximum,
                    "preferred_minimum": item.preferred_minimum,
                    "saturation": item.saturation,
                    "soft_minimum": item.soft_minimum,
                    "upper_soft_target": item.upper_soft_target,
                    "count": item.count,
                }
                for item in self.target_coverage
            ],
            "target_deficit": [[name, value] for name, value in self.target_deficit],
            "target_saturation": [[name, value] for name, value in self.target_saturation],
            "target_confidence": [[name, value] for name, value in self.target_confidence],
            "target_diminishing_returns": [
                [name, value] for name, value in self.target_diminishing_returns
            ],
            "unsupported_payoff_counts": [
                [name, value] for name, value in self.unsupported_payoff_counts
            ],
            "urgency": self.urgency,
            "relationship_support": [
                item.to_json() for item in self.relationship_support
            ],
            "unique_card_count": self.unique_card_count,
        }


def _cards_for_pool(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
) -> tuple[tuple[CardInfo, int], ...]:
    counts = Counter(pool_grp_ids)
    return tuple(
        (card_database.lookup(grp_id=grp_id), quantity)
        for grp_id, quantity in sorted(counts.items())
    )


def _likely_projection(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    set_profile: SetProfile | None,
    pair: str | None,
    ratings_data: SeventeenLandsData | None,
) -> tuple[int, ...]:
    """Choose a bounded, deterministic deck-shaped view of the raw pool.

    This deliberately mirrors the deck builder's important structural gates
    without importing it (the deck builder consumes this ledger).  Quantities,
    pair castability, splash support, curve, creature density, and expensive
    spell limits all participate before role evidence breaks ties.
    """

    if not pool_grp_ids:
        return ()
    splash_state = infer_splash_state(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        ratings_data=ratings_data,
        base_pair=pair,
        enabled=True,
    )
    resolved = tuple(
        (
            card,
            quantity,
            resolve_card_roles(
                card,
                profile=None if set_profile is None else set_profile.role_profile,
            ),
        )
        for card, quantity in _cards_for_pool(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
        )
    )
    spell_units: list[ProjectionUnit] = []
    land_units: list[ProjectionUnit] = []
    for card, quantity, resolution in resolved:
        if card.unknown:
            continue
        assignments = resolution.assignments
        if _is_land(card):
            if (
                any("Basic" not in type_line for type_line in card.types)
                and _land_is_eligible_for_projection(
                    card=card,
                    pair=pair,
                    splash_state=splash_state,
                )
            ):
                land_units.extend((card.grp_id, card, assignments) for _ in range(quantity))
            continue
        if pair is not None and not card_is_castable_in_pair(card=card, base_pair=pair):
            if not _splash_supported(
                card=card,
                pair=pair,
                splash_state=splash_state,
            ):
                continue
        spell_units.extend((card.grp_id, card, assignments) for _ in range(quantity))

    def unit_key(unit: ProjectionUnit) -> tuple[float, float, int, int, int, int]:
        grp_id, card, assignments = unit
        rating = 0.5
        if ratings_data is not None:
            rating_value = ratings_data.rating_for(grp_id=grp_id).gih_win_rate
            if rating_value is not None:
                rating = float(rating_value)
        role_value = sum(
            assignment.confidence * ROLE_PRIORITY_WEIGHTS.get(assignment.role, 0.0)
            for assignment in assignments
        )
        is_two_drop = _is_two_drop(card)
        is_creature = _is_creature(card)
        is_expensive = _is_expensive_spell(card)
        return (
            rating,
            role_value,
            int(is_two_drop),
            int(is_creature),
            -int(is_expensive),
            -grp_id,
        )

    ordered = sorted(spell_units, key=unit_key, reverse=True)
    selected: list[ProjectionUnit] = []
    available_counts = Counter(unit[0] for unit in spell_units)
    selected_counts: Counter[int] = Counter()
    target = min(DECK_BUILDER.target_spell_count, len(ordered))

    def take(unit: ProjectionUnit) -> bool:
        if len(selected) >= target or selected_counts[unit[0]] >= available_counts[unit[0]]:
            return False
        selected.append(unit)
        selected_counts[unit[0]] += 1
        return True

    def choose(
        candidates: Iterable[ProjectionUnit],
        *,
        limit: int,
        predicate: Callable[[ProjectionUnit], bool],
        enforce_limits: bool = False,
    ) -> None:
        chosen = 0
        for unit in candidates:
            if chosen >= limit or len(selected) >= target:
                return
            if selected_counts[unit[0]] >= available_counts[unit[0]] or not predicate(unit):
                continue
            if enforce_limits:
                creature_count = sum(_is_creature(item[1]) for item in selected)
                expensive_count = sum(
                    _is_expensive_spell(item[1]) for item in selected
                )
                if (
                    _is_creature(unit[1])
                    and creature_count >= DECK_BUILDER.creature_ceiling
                ):
                    continue
                if (
                    _is_expensive_spell(unit[1])
                    and expensive_count >= DECK_BUILDER.maximum_expensive_spells
                ):
                    continue
            if take(unit):
                chosen += 1

    choose(
        ordered,
        limit=min(DECK_BUILDER.minimum_two_drops, target),
        predicate=_unit_is_two_drop,
        enforce_limits=True,
    )
    creature_count = sum(_is_creature(unit[1]) for unit in selected)
    expensive_count = sum(_is_expensive_spell(unit[1]) for unit in selected)
    creature_floor_deficit = max(0, DECK_BUILDER.creature_floor - creature_count)
    choose(
        ordered,
        limit=min(creature_floor_deficit, target - len(selected)),
        predicate=_unit_is_creature,
        enforce_limits=True,
    )
    creature_count = sum(_is_creature(unit[1]) for unit in selected)
    expensive_count = sum(_is_expensive_spell(unit[1]) for unit in selected)
    for unit in ordered:
        if len(selected) >= target or selected_counts[unit[0]] >= available_counts[unit[0]]:
            continue
        is_creature = _is_creature(unit[1])
        is_expensive = _is_expensive_spell(unit[1])
        if is_creature and creature_count >= DECK_BUILDER.creature_ceiling:
            continue
        if is_expensive and expensive_count >= DECK_BUILDER.maximum_expensive_spells:
            continue
        take(unit)
        creature_count += int(is_creature)
        expensive_count += int(is_expensive)
    if len(selected) < target:
        for unit in ordered:
            if len(selected) >= target or selected_counts[unit[0]] >= available_counts[unit[0]]:
                continue
            take(unit)

    # Drafted nonbasic lands occupy land slots rather than spell slots, but
    # remain in the projection so fixing/mana evidence reflects the likely deck.
    selected_lands = sorted(
        land_units,
        key=lambda unit: (
            -int(bool(unit[2])),
            -sum(
                assignment.confidence
                for assignment in unit[2]
                if assignment.role in FIXING_ROLES
            ),
            unit[0],
        ),
    )[: DECK_BUILDER.default_land_count]
    return tuple(unit[0] for unit in (*selected, *selected_lands))


def _land_is_eligible_for_projection(
    *,
    card: CardInfo,
    pair: str | None,
    splash_state: SplashState,
) -> bool:
    if pair is None:
        return True

    source_colors = _land_source_colors(card=card)
    if not source_colors:
        return False
    if all(color in pair for color in source_colors):
        return True

    active_color = splash_state.active_color
    return (
        splash_state.enabled
        and active_color is not None
        and active_color in source_colors
    )


def _land_source_colors(*, card: CardInfo) -> tuple[str, ...]:
    source_colors = card.produced_mana or card.colors
    source_set = set(source_colors)
    return tuple(color for color in COLOR_ORDER if color in source_set)


def _splash_supported(
    *,
    card: CardInfo,
    pair: str,
    splash_state: SplashState,
) -> bool:
    active_color = splash_state.active_color
    if active_color is None:
        return False
    splash_color, off_color_pips = splash_requirement(card=card, base_pair=pair)
    if splash_color != active_color or off_color_pips > SPLASH.maximum_off_color_pips:
        return False
    fixing = splash_state.fixing_for(color=active_color)
    required_sources = (
        SPLASH.single_card_sources
        if splash_state.picked_card_count <= 1
        else SPLASH.multiple_card_sources
    )
    return fixing + SPLASH.planned_basic_sources >= required_sources


def _evaluate_pool(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None,
    set_profile: SetProfile | None,
    likely_pair: str | None,
    mode: LedgerMode,
    stage: LedgerStage | None,
) -> PoolRoleLedger:
    source_cards = _cards_for_pool(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
    )
    pair = _resolve_pair(cards=source_cards, likely_pair=likely_pair)
    projected_pool = _likely_projection(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        set_profile=set_profile,
        pair=pair,
        ratings_data=ratings_data,
    )
    return _evaluate(
        pool_grp_ids=projected_pool,
        pool_size=len(pool_grp_ids),
        unique_card_count=len(source_cards),
        card_database=card_database,
        mode=mode,
        stage=stage,
        set_profile=set_profile,
        likely_pair=pair,
    )


def project_pool_role_ledger(
    *,
    pool_before_pick: tuple[int, ...],
    pack_number: int,
    pick_number: int,
    global_pick_index: int,
    estimated_remaining_picks: int,
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    set_profile: SetProfile | None = None,
    likely_pair: str | None = None,
) -> PoolRoleLedger:
    """Evaluate only an authoritative saved/event pool before the current pick.
    Future offered picks and final-pool state are intentionally not accepted.
    """

    stage = LedgerStage(
        pack_number=pack_number,
        pick_number=pick_number,
        global_pick_index=global_pick_index,
        estimated_remaining_picks=estimated_remaining_picks,
    )
    return _evaluate_pool(
        pool_grp_ids=pool_before_pick,
        card_database=card_database,
        ratings_data=ratings_data,
        set_profile=set_profile,
        likely_pair=likely_pair,
        mode=LedgerMode.PRE_PICK_PROJECTION,
        stage=stage,
    )


def evaluate_completed_pool_role_ledger(
    *,
    final_pool: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    set_profile: SetProfile | None = None,
    likely_pair: str | None = None,
) -> PoolRoleLedger:
    """Evaluate only the final pool and never fabricate pre-pick stage context."""

    return _evaluate_pool(
        pool_grp_ids=final_pool,
        card_database=card_database,
        ratings_data=ratings_data,
        set_profile=set_profile,
        likely_pair=likely_pair,
        mode=LedgerMode.COMPLETED_POOL,
        stage=None,
    )


def _evaluate(
    *,
    pool_grp_ids: tuple[int, ...],
    pool_size: int,
    unique_card_count: int,
    card_database: CardDatabase,
    mode: LedgerMode,
    stage: LedgerStage | None,
    set_profile: SetProfile | None,
    likely_pair: str | None,
) -> PoolRoleLedger:
    cards = _cards_for_pool(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
    )
    pair = _resolve_pair(cards=cards, likely_pair=likely_pair)
    resolved = tuple(
        (
            card,
            quantity,
            resolve_card_roles(
                card,
                profile=None if set_profile is None else set_profile.role_profile,
            ),
        )
        for card, quantity in cards
    )
    role_counts_counter: Counter[str] = Counter()
    removal_counts: Counter[str] = Counter()
    removal_values: Counter[str] = Counter()
    creature_count = 0
    playable_count = 0
    curve_counts: Counter[str] = Counter()
    fixing_count = 0
    card_advantage_count = 0
    for card, quantity, resolution in resolved:
        if not _is_land(card):
            playable_count += quantity
            if _is_creature(card):
                creature_count += quantity
            if card.mana_value is not None:
                curve_counts[_curve_bucket(card.mana_value)] += quantity
        assignments = resolution.assignments
        assignment_roles = {assignment.role for assignment in assignments}
        if assignment_roles.intersection(FIXING_ROLES):
            fixing_count += quantity
        if assignment_roles.intersection(CARD_ADVANTAGE_ROLES):
            card_advantage_count += quantity
        for assignment in assignments:
            role_counts_counter[assignment.role.value] += quantity
            _add_removal(
                assignment=assignment,
                quantity=quantity,
                counts=removal_counts,
                values=removal_values,
            )
    enabler_counts, payoff_counts, supported_counts, unsupported_counts, density = _packages(
        role_counts=role_counts_counter,
    )
    profile_source, pair_profile, profile_confidence = _pair_profile(
        set_profile=set_profile,
        pair=pair,
    )
    target_coverage = _target_coverage(
        role_counts=role_counts_counter,
        removal_values=removal_values,
        pair_profile=pair_profile,
        profile_confidence=profile_confidence,
    )
    urgency = _urgency(target_coverage=target_coverage, stage=stage)
    relationship_support = _relationship_support(
        projected_cards=resolved,
        card_database=card_database,
        set_profile=set_profile,
        mode=mode,
    )
    removal_contributions = tuple(
        RemovalContribution(
            kind=kind,
            count=removal_counts[kind],
            value=_round(removal_values[kind]),
        )
        for kind in REMOVAL_KINDS
    )
    return PoolRoleLedger(
        mode=mode,
        pool_size=pool_size,
        unique_card_count=unique_card_count,
        likely_pair=pair,
        playable_count=playable_count,
        cut_count=max(0, pool_size - len(pool_grp_ids)),
        creature_count=creature_count,
        curve_bands=_counter_pairs(curve_counts),
        fixing_count=fixing_count,
        card_advantage_count=card_advantage_count,
        role_counts=_counter_pairs(role_counts_counter),
        enabler_counts=_counter_pairs(enabler_counts),
        payoff_counts=_counter_pairs(payoff_counts),
        supported_payoff_counts=_counter_pairs(supported_counts),
        unsupported_payoff_counts=_counter_pairs(unsupported_counts),
        package_density=_counter_pairs(density),
        removal_contributions=removal_contributions,
        target_coverage=target_coverage,
        target_deficit=tuple((item.name, item.deficit) for item in target_coverage),
        target_saturation=tuple((item.name, item.saturation) for item in target_coverage),
        target_confidence=tuple((item.name, item.confidence) for item in target_coverage),
        target_diminishing_returns=tuple((item.name, item.diminishing_returns) for item in target_coverage),
        urgency=_round(urgency),
        stage=stage,
        profile_source=profile_source,
        profile_fingerprint=None if set_profile is None else set_profile.fingerprint,
        relationship_support=relationship_support,
    )


def _relationship_support(
    *,
    projected_cards: tuple[tuple[CardInfo, int, ResolutionResult], ...],
    card_database: CardDatabase,
    set_profile: SetProfile | None,
    mode: LedgerMode,
) -> tuple[RelationshipSupport, ...]:
    """Project exact, source-bound relationship support for one pre-pick pool.
    Completed-pool evaluation stays relationship-neutral by construction.
    """

    if mode is not LedgerMode.PRE_PICK_PROJECTION or set_profile is None:
        return ()
    enhancement = set_profile.enhancement
    if enhancement is None:
        return ()
    projected = {
        card.grp_id: (card, quantity, resolution)
        for card, quantity, resolution in projected_cards
    }
    records: list[RelationshipSupport] = []
    for relationship in enhancement.relationships:
        record = _matched_relationship_support(
            relationship=relationship,
            set_profile=set_profile,
            card_database=card_database,
            projected=projected,
        )
        if record is not None:
            records.append(record)
    return tuple(
        sorted(
            records,
            key=lambda item: (
                item.target_card_id,
                item.mechanism,
                item.finding_id,
                item.source_card_id,
            ),
        )
    )


def _matched_relationship_support(
    *,
    relationship: CardRelationship,
    set_profile: SetProfile,
    card_database: CardDatabase,
    projected: Mapping[int, tuple[CardInfo, int, ResolutionResult]],
) -> RelationshipSupport | None:
    projection = relationship.prerequisite_projection
    if projection is None:
        return None
    link = next(
        (item for item in ROLE_COMPATIBILITY_RULES if item.mechanism == relationship.mechanism),
        None,
    )
    if link is None:
        return None
    if projection.source.role is not link.enabler or projection.target.role is not link.payoff:
        return None
    source_identity = _participant_identity(
        projection.source,
        set_profile=set_profile,
        card_database=card_database,
        projected=projected,
    )
    if source_identity is None or projection.source.card_id not in projected:
        return None
    target_identity = _participant_identity(
        projection.target,
        set_profile=set_profile,
        card_database=card_database,
        projected=projected,
    )
    if target_identity is None:
        return None
    source_card, source_card_count, _ = projected[projection.source.card_id]
    satisfied = _matched_prerequisites(
        mechanism=relationship.mechanism,
        source=projection.source,
        target=projection.target,
        source_count=source_card_count,
    )
    if satisfied is None:
        return None
    source_card, source_confidence = source_identity
    target_card, target_confidence = target_identity
    return RelationshipSupport(
        finding_id=relationship.finding_id,
        mechanism=relationship.mechanism,
        source_card_id=projection.source.card_id,
        source_card_name=source_card.name,
        source_card_count=source_card_count,
        source_role=projection.source.role,
        source_role_confidence=source_confidence,
        target_card_id=projection.target.card_id,
        target_card_name=target_card.name,
        target_role=projection.target.role,
        target_role_confidence=target_confidence,
        satisfied_prerequisites=satisfied,
    )




def _face_binding_matches(
    *,
    participant: RelationshipParticipant,
    card: CardInfo,
) -> bool:
    if participant.face_index is None:
        if participant.face_name is None:
            return True
        return any(face.name == participant.face_name for face in card.faces)
    index = participant.face_index
    if index < 0 or index >= len(card.faces):
        return False
    face = card.faces[index]
    return participant.face_name is None or face.name == participant.face_name


def _matched_prerequisites(
    *,
    mechanism: str,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    matcher = _MECHANISM_MATCHERS.get(mechanism)
    if matcher is None:
        return None
    return matcher(source=source, target=target, source_count=source_count)


def _supported_object_token_identity(
    clause: RelationshipPrerequisite,
    participant: RelationshipParticipant,
) -> str | None:
    """Return the proven token identity of one clause's supported object."""

    if clause.operation == "create" and clause.object_kind == "token":
        return "token"
    if clause.required_card_id == participant.card_id:
        return "nontoken"
    if clause.token_restriction == "unrestricted":
        return None
    return clause.token_restriction


def _type_qualifiers_agree(
    *,
    source: RelationshipPrerequisite,
    target: RelationshipPrerequisite,
) -> bool:
    if target.type_operator == "all_of":
        return source.type_operator == "all_of" and set(target.card_types) <= set(source.card_types)
    if target.type_operator == "any_of":
        if source.type_operator == "unrestricted":
            return False
        return bool(set(target.card_types) & set(source.card_types))
    return True


def _token_qualifiers_agree(
    *,
    source: RelationshipPrerequisite,
    target: RelationshipPrerequisite,
    source_participant: RelationshipParticipant,
) -> bool:
    object_token = _supported_object_token_identity(source, source_participant)
    if target.token_restriction == "nontoken" and object_token != "nontoken":
        return False
    if target.token_restriction == "token" and object_token != "token":
        return False
    return True


def _color_qualifiers_agree(
    *,
    source: RelationshipPrerequisite,
    target: RelationshipPrerequisite,
) -> bool:
    if target.color_operator == "exact":
        return source.color_operator == "exact" and set(source.colors) == set(target.colors)
    if target.color_operator == "all_of":
        if source.color_operator not in ("exact", "all_of"):
            return False
        return set(target.colors) <= set(source.colors)
    if target.color_operator == "any_of":
        if source.color_operator == "unrestricted":
            return False
        return bool(set(target.colors) & set(source.colors))
    return True


def _party_qualifiers_agree(
    *,
    source: RelationshipPrerequisite,
    target: RelationshipPrerequisite,
) -> bool:
    if target.controller in ("you", "opponent") and source.controller != target.controller:
        return False
    if target.owner in ("you", "opponent") and source.owner != target.owner:
        return False
    return True


def _timing_qualifiers_agree(
    *,
    source: RelationshipPrerequisite,
    target: RelationshipPrerequisite,
) -> bool:
    target_timing = target.timing
    if target_timing.window == "unrestricted" and target_timing.turn == "any":
        return True
    source_timing = source.timing
    if target_timing.turn != "any" and source_timing.turn != target_timing.turn:
        return False
    if target_timing.window != "unrestricted" and source_timing.window != target_timing.window:
        return False
    return True


def _quantity_qualifiers_agree(
    *,
    source: RelationshipPrerequisite,
    target: RelationshipPrerequisite,
    source_count: int,
) -> bool:
    """Prove one fixed target threshold from a fixed, uncapped source supply.

    An unknown or upper-bounded source amount proves nothing, and a supply that a
    per-turn cap shortens is no longer exact, so every such case withholds.
    """

    target_quantity = target.quantity
    if target_quantity is None:
        return True
    if target_quantity.relation is QuantityRelation.VARIABLE:
        return False
    source_quantity = source.quantity
    if source_quantity is None or source_quantity.relation is QuantityRelation.VARIABLE:
        return False
    if source_quantity.relation is QuantityRelation.AT_MOST:
        return False
    supply = source_quantity.value * max(1, source_count)
    caps = tuple(
        limit
        for limit in (target.timing.max_per_turn, source.timing.max_per_turn)
        if limit is not None
    )
    if caps:
        limit = min(caps)
        if source_quantity.relation is QuantityRelation.EXACTLY and supply > limit:
            return False
        supply = min(supply, limit)
    required = target_quantity.value
    if target_quantity.relation is QuantityRelation.EXACTLY:
        return source_quantity.relation is QuantityRelation.EXACTLY and supply == required
    if target_quantity.relation is QuantityRelation.AT_LEAST:
        return supply >= required
    return supply <= required


def _battlefield_zone(controller: str) -> RelationshipZone:
    """Return the normalized battlefield zone of one declared controller."""

    return RelationshipZone(
        zone=CapabilityZone.BATTLEFIELD,
        player=controller if controller in ("you", "opponent") else "any",
    )


def _target_origin_zone(target: RelationshipPrerequisite) -> RelationshipZone | None:
    """Return where one target requirement's supported object must reside."""

    if target.source_zone is not None:
        return target.source_zone
    if target.object_kind == "permanent" and target.operation in (
        "die",
        "sacrifice",
        "control",
        "count",
    ):
        return _battlefield_zone(target.controller)
    return None


def _source_location_zone(
    source: RelationshipPrerequisite,
    participant: RelationshipParticipant,
) -> RelationshipZone | None:
    """Return where one source anchor leaves or holds its supported object."""

    if source.required_card_id == participant.card_id:
        return _battlefield_zone(source.controller)
    return source.destination_zone


def _zone_qualifiers_agree(
    *,
    source: RelationshipPrerequisite,
    target: RelationshipPrerequisite,
    source_participant: RelationshipParticipant,
) -> bool:
    """Compare declared object zones, treating an unbound player as unrestricted."""

    target_origin = _target_origin_zone(target)
    if target_origin is None:
        return True
    source_location = _source_location_zone(source, source_participant)
    if source_location is None or source_location.zone is not target_origin.zone:
        return False
    if "any" in (source_location.player, target_origin.player):
        return True
    return source_location.player == target_origin.player


def _object_satisfies_target(
    *,
    source: RelationshipPrerequisite,
    target: RelationshipPrerequisite,
    source_participant: RelationshipParticipant,
    target_participant: RelationshipParticipant,
    source_count: int,
) -> bool:
    """Check one source-supplied object against one target requirement clause."""

    if not _type_qualifiers_agree(source=source, target=target):
        return False
    if not _token_qualifiers_agree(source=source, target=target, source_participant=source_participant):
        return False
    if target.subtype is not None and source.subtype != target.subtype:
        return False
    if not _color_qualifiers_agree(source=source, target=target):
        return False
    if not _party_qualifiers_agree(source=source, target=target):
        return False
    if not _timing_qualifiers_agree(source=source, target=target):
        return False
    if not _quantity_qualifiers_agree(source=source, target=target, source_count=source_count):
        return False
    if not _zone_qualifiers_agree(
        source=source,
        target=target,
        source_participant=source_participant,
    ):
        return False
    required_card_id = target.required_card_id
    if required_card_id is not None:
        if source.required_card_id != required_card_id:
            return False
        if required_card_id not in (source_participant.card_id, target_participant.card_id):
            return False
    return True


def _prerequisite_text(role: str, clause: RelationshipPrerequisite) -> str:
    parts = [f"{role}:{clause.kind.value}/{clause.operation}/{clause.object_kind}"]
    if clause.type_operator != "unrestricted":
        parts.append(f"types={clause.type_operator}:{','.join(clause.card_types)}")
    if clause.token_restriction != "unrestricted":
        parts.append(f"token={clause.token_restriction}")
    if clause.subtype is not None:
        parts.append(f"subtype={clause.subtype}")
    if clause.color_operator != "unrestricted":
        parts.append(f"color={clause.color_operator}:{','.join(clause.colors)}")
    if clause.controller not in ("any", "not_applicable"):
        parts.append(f"controller={clause.controller}")
    if clause.owner not in ("any", "not_applicable"):
        parts.append(f"owner={clause.owner}")
    if clause.quantity is not None:
        value = (
            "variable"
            if clause.quantity.relation is QuantityRelation.VARIABLE
            else str(clause.quantity.value)
        )
        parts.append(f"qty={clause.quantity.relation.value}/{value}")
    if clause.source_zone is not None or clause.destination_zone is not None:
        source_zone = (
            f"{clause.source_zone.zone.value}/{clause.source_zone.player}"
            if clause.source_zone is not None
            else "none"
        )
        destination_zone = (
            f"{clause.destination_zone.zone.value}/{clause.destination_zone.player}"
            if clause.destination_zone is not None
            else "none"
        )
        parts.append(f"zones={source_zone}->{destination_zone}")
    if (
        clause.timing.window != "unrestricted"
        or clause.timing.turn != "any"
        or clause.timing.max_per_turn is not None
    ):
        limit = str(clause.timing.max_per_turn) if clause.timing.max_per_turn is not None else "none"
        parts.append(f"timing={clause.timing.window}/{clause.timing.turn}/{limit}")
    if clause.exclusion != "none":
        parts.append(f"exclusion={clause.exclusion}")
    if clause.required_card_id is not None:
        parts.append(f"required={clause.required_card_id}")
    return ";".join(parts)


def _return_from_graveyard_clause(clause: RelationshipPrerequisite) -> bool:
    return _return_clause(clause) and _declared_zone(
        clause.source_zone,
        CapabilityZone.GRAVEYARD,
        None,
    )


def _typed_graveyard_payoff_clause(clause: RelationshipPrerequisite) -> bool:
    """Return whether one clause counts a graveyard it declares in typed fields."""

    if clause.kind not in (PrerequisiteKind.CONDITION, PrerequisiteKind.THRESHOLD):
        return False
    if clause.operation != "count":
        return False
    return _declared_zone(
        clause.source_zone,
        CapabilityZone.GRAVEYARD,
        None,
    ) or _declared_zone(
        clause.destination_zone,
        CapabilityZone.GRAVEYARD,
        None,
    )


def _fodder_reaches_graveyard_destination(
    anchor: RelationshipPrerequisite,
    requirement: RelationshipPrerequisite,
) -> bool:
    """Require one fodder transition to reach the target's declared graveyard."""

    destination = requirement.destination_zone
    if destination is None or destination.zone is not CapabilityZone.GRAVEYARD:
        return True
    source_destination = anchor.destination_zone
    if source_destination is None or source_destination.zone is not CapabilityZone.GRAVEYARD:
        return False
    return (
        source_destination.player == destination.player
        or "any" in (source_destination.player, destination.player)
    )


def _matched_prerequisite_texts(
    *,
    requirements: tuple[RelationshipPrerequisite, ...],
    anchors: tuple[RelationshipPrerequisite, ...],
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
    required_source_clauses: tuple[RelationshipPrerequisite, ...] = (),
    pair_gate: Callable[[RelationshipPrerequisite, RelationshipPrerequisite], bool] | None = None,
) -> tuple[str, ...] | None:
    """Prove every target requirement with one compatible source anchor.

    Anchors are capability-shaped, so a source clause that proves no requirement is
    unused rather than disqualifying. Every matched pair contributes canonical
    prerequisite text, sorted and deduplicated for deterministic output.
    """

    if not requirements or not anchors:
        return None
    texts = {_prerequisite_text("source", clause) for clause in required_source_clauses}
    for requirement in requirements:
        for anchor in anchors:
            if pair_gate is not None and not pair_gate(anchor, requirement):
                continue
            if _object_satisfies_target(
                source=anchor,
                target=requirement,
                source_participant=source,
                target_participant=target,
                source_count=source_count,
            ):
                texts.add(_prerequisite_text("source", anchor))
                texts.add(_prerequisite_text("target", requirement))
                break
        else:
            return None
    return tuple(sorted(texts))


def _self_death_clause(
    clause: RelationshipPrerequisite,
    participant: RelationshipParticipant,
) -> bool:
    return (
        clause.kind in (PrerequisiteKind.TRIGGER, PrerequisiteKind.CONDITION)
        and clause.operation == "die"
        and clause.required_card_id == participant.card_id
        and "creature" in clause.card_types
    )


def _graveyard_feeder_clause(
    clause: RelationshipPrerequisite,
    participant: RelationshipParticipant,
) -> bool:
    """Return whether one clause independently puts a fixed object into the graveyard."""

    if _self_mill_clause(clause) or _discard_clause(clause):
        return True
    if _fodder_clause(clause=clause, participant=participant) and clause.operation == "sacrifice":
        return _declared_zone(clause.destination_zone, CapabilityZone.GRAVEYARD, None)
    return _self_death_clause(clause, participant)


def _match_discard_recursion_payoff(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause for clause in target.prerequisites if _return_from_graveyard_clause(clause)
        ),
        anchors=tuple(clause for clause in source.prerequisites if _discard_clause(clause)),
        source=source,
        target=target,
        source_count=source_count,
    )


def _match_fodder_dies_payoff(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause
            for clause in target.prerequisites
            if _death_payoff_clause(clause=clause, participant=target)
        ),
        anchors=tuple(
            clause
            for clause in source.prerequisites
            if _fodder_clause(clause=clause, participant=source)
            and clause.operation == "sacrifice"
            and _declared_zone(clause.destination_zone, CapabilityZone.GRAVEYARD, None)
        ),
        source=source,
        target=target,
        source_count=source_count,
        pair_gate=_fodder_reaches_graveyard_destination,
    )


def _match_fodder_sacrifice_outlet(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause
            for clause in target.prerequisites
            if _outlet_clause(clause=clause, participant=target)
        ),
        anchors=tuple(
            clause
            for clause in source.prerequisites
            if _fodder_clause(clause=clause, participant=source)
            and clause.operation == "sacrifice"
        ),
        source=source,
        target=target,
        source_count=source_count,
        pair_gate=_fodder_reaches_graveyard_destination,
    )


def _match_loot_recursion_payoff(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    draws = tuple(clause for clause in source.prerequisites if _draw_clause(clause))
    if not draws:
        return None
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause for clause in target.prerequisites if _return_from_graveyard_clause(clause)
        ),
        anchors=tuple(clause for clause in source.prerequisites if _discard_clause(clause)),
        source=source,
        target=target,
        source_count=source_count,
        required_source_clauses=(draws[0],),
    )


def _match_mill_graveyard_payoff(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause for clause in target.prerequisites if _typed_graveyard_payoff_clause(clause)
        ),
        anchors=tuple(clause for clause in source.prerequisites if _self_mill_clause(clause)),
        source=source,
        target=target,
        source_count=source_count,
    )


def _match_recursion_graveyard_payoff(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    if not any(_return_clause(clause) for clause in source.prerequisites):
        return None
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause for clause in target.prerequisites if _typed_graveyard_payoff_clause(clause)
        ),
        anchors=tuple(
            clause for clause in source.prerequisites if _graveyard_feeder_clause(clause, source)
        ),
        source=source,
        target=target,
        source_count=source_count,
    )


def _match_token_death_payoff(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause
            for clause in target.prerequisites
            if _death_payoff_clause(clause=clause, participant=target)
        ),
        anchors=tuple(clause for clause in source.prerequisites if _produced_token_clause(clause)),
        source=source,
        target=target,
        source_count=source_count,
    )


def _match_token_go_wide_payoff(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause for clause in target.prerequisites if _wide_payoff_clause(clause)
        ),
        anchors=tuple(clause for clause in source.prerequisites if _produced_token_clause(clause)),
        source=source,
        target=target,
        source_count=source_count,
    )


def _match_token_sacrifice_outlet(
    *,
    source: RelationshipParticipant,
    target: RelationshipParticipant,
    source_count: int,
) -> tuple[str, ...] | None:
    return _matched_prerequisite_texts(
        requirements=tuple(
            clause
            for clause in target.prerequisites
            if _outlet_clause(clause=clause, participant=target)
        ),
        anchors=tuple(clause for clause in source.prerequisites if _produced_token_clause(clause)),
        source=source,
        target=target,
        source_count=source_count,
    )


_MECHANISM_MATCHERS: Mapping[str, Callable[..., tuple[str, ...] | None]] = {
    "discard-recursion-payoff": _match_discard_recursion_payoff,
    "fodder-dies-payoff": _match_fodder_dies_payoff,
    "fodder-sacrifice-outlet": _match_fodder_sacrifice_outlet,
    "loot-recursion-payoff": _match_loot_recursion_payoff,
    "mill-graveyard-payoff": _match_mill_graveyard_payoff,
    "recursion-graveyard-payoff": _match_recursion_graveyard_payoff,
    "token-death-payoff": _match_token_death_payoff,
    "token-go-wide-payoff": _match_token_go_wide_payoff,
    "token-sacrifice-outlet": _match_token_sacrifice_outlet,
}


def _resolve_pair(
    *,
    cards: tuple[tuple[CardInfo, int], ...],
    likely_pair: str | None,
) -> str | None:
    if likely_pair is not None:
        normalized = likely_pair.strip().upper()
        if normalized not in COLOR_PAIRS:
            raise ValueError(f"Unsupported ledger color pair {likely_pair!r}.")
        return normalized
    weights = {color: 0 for color in "WUBRG"}
    for card, quantity in cards:
        for color in card.colors:
            if color in weights:
                weights[color] += quantity
    if not any(weights.values()):
        return None
    return max(
        COLOR_PAIRS,
        key=lambda pair: (sum(weights[color] for color in pair), -COLOR_PAIRS.index(pair)),
    )


def _pair_profile(
    *,
    set_profile: SetProfile | None,
    pair: str | None,
) -> tuple[str, PairProfile | None, float]:
    if set_profile is None:
        return "generic", None, GENERIC_TARGET_CONFIDENCE
    if pair is None:
        return f"profile:{set_profile.maturity.value}", None, set_profile.confidence
    return (
        f"profile:{set_profile.maturity.value}",
        set_profile.pair(pair),
        set_profile.confidence,
    )


def _target_coverage(
    *,
    role_counts: Counter[str],
    removal_values: Counter[str],
    pair_profile: PairProfile | None,
    profile_confidence: float,
) -> tuple[TargetCoverage, ...]:
    if pair_profile is not None and (pair_profile.role_targets or pair_profile.removal_targets):
        target_values: dict[str, tuple[float, float]] = {
            target.role.value: (target.value, profile_confidence)
            for target in pair_profile.role_targets
        }
        target_values.update(
            {
                f"removal:{target.kind}": (target.value, profile_confidence)
                for target in pair_profile.removal_targets
            }
        )
    else:
        target_values = {
            role.value: (value, GENERIC_TARGET_CONFIDENCE)
            for role, value in GENERIC_ROLE_TARGETS.items()
        }
    entries: list[TargetCoverage] = []
    for name, (preferred, confidence) in sorted(target_values.items()):
        if name.startswith("removal:"):
            removal_kind = name.removeprefix("removal:")
            if removal_kind == "tap":
                removal_kind = "temporary"
            count = removal_values.get(removal_kind, 0.0)
        else:
            count = role_counts.get(name, 0.0)
        soft_minimum = _round(preferred * 0.75)
        upper = _round(max(preferred, preferred * 1.5))
        coverage = _ratio(count, preferred)
        deficit = _round(max(0.0, preferred - count))
        saturation = _clamp(_ratio(count, preferred))
        diminishing = _clamp(1.0 - _ratio(count, upper))
        entries.append(
            TargetCoverage(
                name=name,
                count=_round(count),
                soft_minimum=soft_minimum,
                preferred_minimum=_round(preferred),
                preferred_maximum=_round(preferred),
                upper_soft_target=upper,
                coverage=_round(coverage),
                deficit=deficit,
                saturation=_round(saturation),
                confidence=_round(confidence),
                diminishing_returns=_round(diminishing),
            )
        )
    return tuple(entries)


def _urgency(*, target_coverage: tuple[TargetCoverage, ...], stage: LedgerStage | None) -> float:
    deficits = tuple(item for item in target_coverage if item.deficit > 0 and item.preferred_minimum > 0)
    if not deficits or stage is None:
        return 0.0
    deficit_pressure = max(
        _ratio(item.deficit, item.preferred_minimum) * item.confidence
        for item in deficits
    )
    opportunity_pressure = _clamp(
        1.0 - _ratio(stage.estimated_remaining_picks, TOTAL_DRAFT_PICKS),
    )
    return _clamp(deficit_pressure * opportunity_pressure)


def _packages(
    *,
    role_counts: Counter[str],
) -> tuple[Counter[str], Counter[str], Counter[str], Counter[str], Counter[str]]:
    enablers: Counter[str] = Counter()
    payoffs: Counter[str] = Counter()
    supported: Counter[str] = Counter()
    unsupported: Counter[str] = Counter()
    density: Counter[str] = Counter()
    for package, (enabler_roles, payoff_roles) in PACKAGE_ROLES.items():
        enabler_count = sum(role_counts[role.value] for role in enabler_roles)
        payoff_count = sum(role_counts[role.value] for role in payoff_roles)
        supported_count = payoff_count if enabler_count > 0 else 0
        enablers[package] = enabler_count
        payoffs[package] = payoff_count
        supported[package] = supported_count
        unsupported[package] = payoff_count - supported_count
        if payoff_count:
            density_value = _clamp(
                _ratio(supported_count, max(1, enabler_count + payoff_count))
            )
        else:
            density_value = 0.0
        density[package] = _round(density_value)
    return enablers, payoffs, supported, unsupported, density


def _removal_kind(assignment: RoleAssignment) -> str:
    removal = assignment.removal
    if assignment.role is Role.CONDITIONAL_REMOVAL:
        kind = "conditional"
    elif removal is not None:
        kind = "temporary" if removal.kind == "tap" else removal.kind
    elif assignment.role is Role.TEMPORARY_TAP:
        kind = "temporary"
    else:
        kind = REMOVAL_ROLE_KINDS[assignment.role]
    return "disable" if kind == "disabling" else kind


def _add_removal(
    *,
    assignment: RoleAssignment,
    quantity: int,
    counts: Counter[str],
    values: Counter[str],
) -> None:
    removal = assignment.removal
    if removal is None and assignment.role not in REMOVAL_ROLES:
        return
    kind = _removal_kind(assignment)
    if kind not in REMOVAL_WEIGHTS:
        return
    score = 1.0 if removal is None else removal.effective_score
    if removal is not None and removal.conditional:
        score *= 0.75
    counts[kind] += quantity
    values[kind] += quantity * assignment.confidence * score * REMOVAL_WEIGHTS[kind]


def _counter_pairs(counter: Counter[str]) -> LedgerPairs:
    return tuple((key, _round(counter[key])) for key in sorted(counter))


def _curve_bucket(value: float) -> str:
    return str(min(6, max(0, int(value)))) if value < 6 else "6+"


def _is_two_drop(card: CardInfo) -> bool:
    mana_value = card.mana_value
    return (
        mana_value is not None
        and mana_value == DECK_BUILDER.two_drop_mana_value
    )


def _unit_is_two_drop(unit: ProjectionUnit) -> bool:
    return _is_two_drop(unit[1])


def _unit_is_creature(unit: ProjectionUnit) -> bool:
    return _is_creature(unit[1])


def _is_expensive_spell(card: CardInfo) -> bool:
    mana_value = card.mana_value
    return (
        mana_value is not None
        and mana_value >= DECK_BUILDER.expensive_spell_mana_value
    )


def _is_creature(card: CardInfo) -> bool:
    return any("Creature" in type_line for type_line in card.types)


def _is_land(card: CardInfo) -> bool:
    return any("Land" in type_line for type_line in card.types)


def _ratio(value: float, denominator: float) -> float:
    return 0.0 if denominator <= 0 else value / denominator


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _round(value: float) -> float:
    return float(f"{value:.6f}")
