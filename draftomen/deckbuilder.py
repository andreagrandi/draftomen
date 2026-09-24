"""Deck-builder pair selection, spells, mana base, and text output.
Cached 17Lands structure targets override consensus defaults when present.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from os import PathLike
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import COLOR_PAIRS, DECK_BUILDER, SPLASH, DeckBuilderConfig
from draftomen.pickengine import PickEngine, ScoredCard
from draftomen.pool import DraftState, list_draft_states
from draftomen.pool_ledger import (
    PACKAGE_ROLES,
    REMOVAL_ROLE_KINDS,
    REMOVAL_ROLES,
    REMOVAL_WEIGHTS,
    PoolRoleLedger,
    evaluate_completed_pool_role_ledger,
)
from draftomen.semantic_roles import Role, RoleAssignment, resolve_card_roles
from draftomen.set_profile import PairProfile, ProfileMaturity, SetProfile
from draftomen.seventeen import (
    SEVENTEEN_LANDS_ATTRIBUTION,
    SeventeenLandsData,
    StructuralTargets,
)
from draftomen.splash import (
    SplashState,
    card_is_castable_in_pair,
    grade_at_least,
    infer_splash_state,
    splash_requirement,
)
from draftomen.setinfo import format_set_label

PathInput: TypeAlias = str | PathLike[str]
CardQuantityKey: TypeAlias = tuple[str, str]
SPELL_TYPE_MARKERS = (
    "Creature",
    "Artifact",
    "Enchantment",
    "Planeswalker",
    "Battle",
    "Instant",
    "Sorcery",
)
BASIC_LANDS_BY_COLOR = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}
MANA_SYMBOL_PATTERN = re.compile(r"\{([^}]+)\}")
CURVE_BUCKET_LABELS = ("0", "1", "2", "3", "4", "5", "6+")


class DeckBuilderError(RuntimeError):
    """Raised when deck-builder input is missing or inconsistent.
    CLI callers surface this as a concise build diagnostic.
    """


@dataclass(frozen=True, slots=True)
class BuildPool:
    """A pool selected from a file or persisted draft state.
    The set code drives the offline 17Lands cache lookup.
    """

    set_code: str
    pool_grp_ids: tuple[int, ...]
    source_label: str
    account_id: str | None = None
    draft_id: str | None = None


@dataclass(frozen=True, slots=True)
class PairScore:
    """Computed score for one two-color pair.
    Card quality and aggregate 17Lands pair win rate stay visible separately.
    """

    pair: str
    playable_count: int
    playable_score_sum: float
    average_playable_score: float
    pair_win_rate: float | None
    pair_win_rate_score: float
    blended_score: float


@dataclass(frozen=True, slots=True)
class PairSelection:
    """Chosen pair plus sorted pair-score context.
    Forced selections keep the automatic best pair available for display.
    """

    chosen: PairScore
    runner_up: PairScore
    automatic: PairScore
    ranked_scores: tuple[PairScore, ...]
    forced_pair: str | None
    pool_size: int
    target_spell_count: int
    attribution: str = SEVENTEEN_LANDS_ATTRIBUTION

    @property
    def score_gap(self) -> float:
        """Return chosen score minus runner-up score.
        Forced selections can produce a negative gap by design.
        """

        return self.chosen.blended_score - self.runner_up.blended_score


@dataclass(frozen=True, slots=True)
class SpellCounts:
    """Structural counts for a selected spell set.
    Derived properties keep constraint checks readable and deterministic.
    """

    total: int
    creatures: int
    two_drops: int
    expensive: int
    splashes: int = 0
    instants: int = 0

    @property
    def noncreatures(self) -> int:
        """Return selected cards that are not creatures.
        This is used when enforcing the creature ceiling.
        """

        return self.total - self.creatures

    @property
    def non_expensive(self) -> int:
        """Return selected cards below the expensive-spell threshold.
        This is used when enforcing the high-mana-value soft cap.
        """

        return self.total - self.expensive


@dataclass(frozen=True, slots=True)
class SpellConstraints:
    """Effective constraints for a spell-selection attempt.
    Values may be relaxed when the pool cannot satisfy configured defaults.
    """

    spell_count: int
    creature_floor: int
    creature_ceiling: int
    minimum_two_drops: int
    maximum_expensive_spells: int
    maximum_splash_spells: int = 0


@dataclass(frozen=True, slots=True)
class SpellSelection:
    """Selected spells plus bench and constraint metadata.
    The requested count remains visible when a tiny fixture cannot make 23.
    """

    pair: str
    spells: tuple[ScoredCard, ...]
    bench: tuple[ScoredCard, ...]
    eligible_count: int
    requested_spell_count: int
    constraints: SpellConstraints
    counts: SpellCounts
    applied_relaxations: tuple[str, ...]
    allow_splash_requested: bool
    splash_color: str | None = None
    splash_fixing_sources: int = 0
    splash_planned_basic_sources: int = 0
    structure_targets: StructuralTargets | None = None


@dataclass(frozen=True, slots=True)
class LandCard:
    """One drafted nonbasic land selected for the mana base.
    Source colors are separated from card colors because lands are colorless.
    """

    card: CardInfo
    original_index: int
    source_colors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BasicLandCount:
    """Count of one basic land name in the recommended mana base.
    Colors stay explicit so source accounting is deterministic.
    """

    color: str
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class ManaBase:
    """Selected lands and source accounting for the final build sheet.
    Basics are split by colored pips after drafted in-pair nonbasics.
    """

    pair: str
    land_count: int
    spell_count: int
    deck_size: int
    nonbasic_lands: tuple[LandCard, ...]
    basic_lands: tuple[BasicLandCount, ...]
    pip_counts: tuple[tuple[str, int], ...]
    double_pip_counts: tuple[tuple[str, int], ...]
    source_counts: tuple[tuple[str, int], ...]
    average_mana_value: float
    reason: str
    caveats: tuple[str, ...]

    @property
    def total_cards(self) -> int:
        """Return the spell-plus-land total for the proposed deck.
        Build-sheet formatting uses this to prove the deck is exactly 40.
        """

        return self.spell_count + self.land_count


@dataclass(frozen=True, slots=True)
class BuildSheet:
    """Final selected spells plus mana base.
    The pair-selection context is formatted separately before this sheet.
    """

    spell_selection: SpellSelection
    mana_base: ManaBase
    role_ledger: PoolRoleLedger | None = None


@dataclass(frozen=True, slots=True)
class _ConstraintPlan:
    """One point in the documented stage-2 relaxation order.
    Later plans disable progressively more structural constraints.
    """

    enforce_expensive_cap: bool
    enforce_two_drop_minimum: bool
    enforce_creature_ceiling: bool
    enforce_creature_floor: bool

_PROFILE_MATURITY_WEIGHTS = {
    ProfileMaturity.MATURE: 1.0,
    ProfileMaturity.EARLY: 0.65,
    ProfileMaturity.METADATA_ONLY: 0.35,
    ProfileMaturity.GENERIC: 0.0,
}


@dataclass(frozen=True, slots=True)
class _OptimizerCardEvidence:
    """Prepared semantic and empirical evidence for one candidate card.
    Search expansions reuse this immutable evidence without reclassification.
    """

    original_index: int
    card_keys: tuple[str, ...]
    role_values: tuple[tuple[str, float], ...]
    removal_values: tuple[tuple[str, float], ...]
    package_enablers: tuple[tuple[str, float], ...]
    package_payoffs: tuple[tuple[str, float], ...]
    scarcity: float
    mana_strain: float


@dataclass(frozen=True, slots=True)
class _OptimizerAggregate:
    """Incremental package evidence used by bounded search and local improvement."""

    card_keys: tuple[tuple[str, ...], ...] = ()
    role_values: tuple[tuple[str, float], ...] = ()
    removal_values: tuple[tuple[str, float], ...] = ()
    package_enablers: tuple[tuple[str, float], ...] = ()
    package_payoffs: tuple[tuple[str, float], ...] = ()
    synergy_value: float = 0.0
    scarcity: float = 0.0
    mana_strain: float = 0.0


@dataclass(frozen=True, slots=True)
class _OptimizerContext:
    """Bounded profile evidence prepared once per spell-selection attempt."""

    pair: str = ""
    influence: float = 0.0
    pair_profile: PairProfile | None = None
    synergy_lookup: Mapping[tuple[str, str], float] = field(
        default_factory=lambda: MappingProxyType({})
    )
    scarcity_lookup: Mapping[str, float] = field(
        default_factory=lambda: MappingProxyType({})
    )
    card_evidence: tuple[_OptimizerCardEvidence, ...] = ()

    @property
    def active(self) -> bool:
        """Return whether profile evidence can affect package ranking."""

        pair_evidence = self.pair_profile is not None and any(
            (
                self.pair_profile.role_targets,
                self.pair_profile.removal_targets,
                self.synergy_lookup,
                self.scarcity_lookup,
            )
        )
        return self.influence > 0.0 and (
            pair_evidence
            or any(
                evidence.role_values
                or evidence.removal_values
                or evidence.scarcity
                for evidence in self.card_evidence
            )
        )


@dataclass(frozen=True, slots=True)
class _SuffixFeasibility:
    """Exact completion states for every candidate suffix in one selection."""

    states_by_start: tuple[frozenset[tuple[int, int, int, int, int]], ...]
    candidate_features: tuple[tuple[int, int, int, int], ...]
    spell_count: int
    minimum_two_drops: int

    def can_complete_values(
        self,
        *,
        total: int,
        creatures: int,
        two_drops: int,
        expensive: int,
        splashes: int,
        start: int,
        constraints: SpellConstraints,
    ) -> bool:
        slots_remaining = self.spell_count - total
        if slots_remaining < 0 or start < 0 or start >= len(self.states_by_start):
            return False
        if slots_remaining == 0:
            return _counts_satisfy_constraints(
                counts=SpellCounts(
                    total=total,
                    creatures=creatures,
                    two_drops=two_drops,
                    expensive=expensive,
                    splashes=splashes,
                ),
                constraints=constraints,
            )
        return (
            slots_remaining,
            creatures,
            min(two_drops, self.minimum_two_drops),
            expensive,
            splashes,
        ) in self.states_by_start[start]


def load_pool_file(*, path: PathInput, set_code: str | None = None) -> BuildPool:
    """Load a fixture pool JSON file for offline building.
    Draftomen state JSON and compact pool objects are both supported.
    """

    pool_path = Path(path)
    try:
        payload = json.loads(pool_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise DeckBuilderError(f"Pool file does not exist at {pool_path}.") from error
    except json.JSONDecodeError as error:
        raise DeckBuilderError(f"Malformed pool file {pool_path}: {error}.") from error

    pool = _pool_from_payload(payload=payload, source_label=str(pool_path))
    resolved_set_code = _resolve_set_code(
        explicit_set_code=set_code,
        payload_set_code=pool.set_code,
        source_label=str(pool_path),
    )
    return BuildPool(
        set_code=resolved_set_code,
        pool_grp_ids=pool.pool_grp_ids,
        source_label=str(pool_path),
        account_id=pool.account_id,
        draft_id=pool.draft_id,
    )



def load_persisted_pool(
    *,
    app_dir: PathInput | None = None,
    account_id: str | None = None,
    draft_id: str | None = None,
) -> BuildPool:
    """Load the requested persisted pool, defaulting to the latest one.
    Account and draft filters disambiguate local multi-account state.
    """

    matches = _matching_states(
        states=list_draft_states(app_dir=app_dir),
        account_id=account_id,
        draft_id=draft_id,
    )
    if not matches:
        raise DeckBuilderError(
            _missing_persisted_pool_message(account_id=account_id, draft_id=draft_id)
        )

    if draft_id is not None and len(matches) > 1:
        raise DeckBuilderError(
            f"Multiple persisted pools use draft id {draft_id!r}; pass --account."
        )

    state = _latest_state(states=matches)
    return BuildPool(
        set_code=state.set_code,
        pool_grp_ids=state.pool_grp_ids,
        source_label=f"persisted {state.account_id}/{state.draft_id}",
        account_id=state.account_id,
        draft_id=state.draft_id,
    )



def select_color_pair(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    forced_pair: str | None = None,
    config: DeckBuilderConfig = DECK_BUILDER,
    set_profile: SetProfile | None = None,
) -> PairSelection:
    """Score all two-color pairs and choose the best or forced pair.
    The pool is already drafted; early draft picks remain rating-first elsewhere.
    """

    _validate_deck_builder_config(config=config)
    _validate_blending_weights(config=config)
    _validate_metadata_coverage(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        config=config,
    )
    resolved_forced_pair = _optional_pair(value=forced_pair)
    scored_pool = PickEngine(
        ratings_data=ratings_data,
        set_profile=set_profile,
    ).score_pack(
        offered_grp_ids=pool_grp_ids,
        card_database=card_database,
        pool_grp_ids=(),
        pick_index=1,
    )
    scored_cards = _limit_cards_to_pool_quantities(
        cards=scored_pool.cards,
        available_quantities=_pool_card_quantities(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
        ),
    )
    scores = tuple(
        _score_pair(
            pair=pair,
            scored_cards=scored_cards,
            ratings_data=ratings_data,
            config=config,
        )
        for pair in COLOR_PAIRS
    )
    _validate_playable_pair_scores(
        scores=scores,
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        config=config,
    )
    ranked_scores = tuple(sorted(scores, key=_pair_score_sort_key))
    automatic = ranked_scores[0]
    chosen = (
        _score_for_pair(scores=scores, pair=resolved_forced_pair)
        if resolved_forced_pair is not None
        else automatic
    )
    runner_up = next(score for score in ranked_scores if score.pair != chosen.pair)
    return PairSelection(
        chosen=chosen,
        runner_up=runner_up,
        automatic=automatic,
        ranked_scores=ranked_scores,
        forced_pair=resolved_forced_pair,
        pool_size=len(pool_grp_ids),
        target_spell_count=config.target_spell_count,
        attribution=(
            ratings_data.attribution
            if ratings_data is not None
            else SEVENTEEN_LANDS_ATTRIBUTION
        ),
    )



def select_deck_spells(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    pair: str,
    ratings_data: SeventeenLandsData | None = None,
    allow_splash: bool = SPLASH.enabled_by_default,
    config: DeckBuilderConfig = DECK_BUILDER,
    set_profile: SetProfile | None = None,
) -> SpellSelection:
    """Select deck spells for a chosen pair under structural constraints.
    Cached pair targets and explicit splash eligibility are applied here.
    """

    _validate_deck_builder_config(config=config)
    _validate_metadata_coverage(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        config=config,
    )
    resolved_pair = _optional_pair(value=pair)
    if resolved_pair is None:
        raise DeckBuilderError("A color pair is required before selecting spells.")

    structure_targets = _structure_targets_for_pair(
        ratings_data=ratings_data,
        pair=resolved_pair,
    )
    effective_config = _config_with_structure_targets(
        config=config,
        structure_targets=structure_targets,
    )
    _validate_deck_builder_config(config=effective_config)
    splash_state = infer_splash_state(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        ratings_data=ratings_data,
        base_pair=resolved_pair,
        enabled=allow_splash,
    )
    splash_color = splash_state.active_color
    splash_fixing_sources = (
        0
        if splash_color is None
        else splash_state.fixing_for(color=splash_color)
    )
    splash_limit = _supported_splash_card_limit(
        state=splash_state,
        config=effective_config,
    )
    scored_pool = PickEngine(
        ratings_data=ratings_data,
        set_profile=set_profile,
    ).score_pack(
        offered_grp_ids=pool_grp_ids,
        card_database=card_database,
        pool_grp_ids=(),
        pick_index=1,
    )
    available_quantities = _pool_card_quantities(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
    )
    candidates = _limit_cards_to_pool_quantities(
        cards=tuple(
            card
            for card in scored_pool.cards
            if _is_eligible_spell_for_pair(
                card=card,
                pair=resolved_pair,
                allow_splash=allow_splash,
                splash_color=splash_color,
                splash_limit=splash_limit,
                config=effective_config,
            )
        ),
        available_quantities=available_quantities,
    )
    _validate_spell_candidates(
        candidates=candidates,
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        pair=resolved_pair,
        config=effective_config,
    )

    for plan in _constraint_plans():
        constraints = _constraints_for_plan(
            candidates=candidates,
            pair=resolved_pair,
            splash_limit=splash_limit,
            plan=plan,
            config=effective_config,
        )
        selected = _select_with_constraints(
            candidates=candidates,
            available_quantities=available_quantities,
            pair=resolved_pair,
            constraints=constraints,
            config=effective_config,
            set_profile=set_profile,
        )
        if selected is None:
            continue

        counts = _spell_counts(
            cards=selected,
            pair=resolved_pair,
            config=effective_config,
        )
        return SpellSelection(
            pair=resolved_pair,
            spells=selected,
            bench=_bench_cards(
                candidates=candidates,
                selected=selected,
                config=effective_config,
            ),
            eligible_count=len(candidates),
            requested_spell_count=effective_config.target_spell_count,
            constraints=constraints,
            counts=counts,
            applied_relaxations=_applied_relaxations(
                plan=plan,
                constraints=constraints,
                config=effective_config,
            ),
            allow_splash_requested=allow_splash,
            splash_color=splash_color,
            splash_fixing_sources=splash_fixing_sources,
            splash_planned_basic_sources=(
                SPLASH.planned_basic_sources
                if splash_color is not None and splash_limit > 0
                else 0
            ),
            structure_targets=structure_targets,
        )

    raise DeckBuilderError("Could not select deck spells with the configured constraints.")



def select_build_sheet(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    pair: str,
    ratings_data: SeventeenLandsData | None = None,
    allow_splash: bool = SPLASH.enabled_by_default,
    config: DeckBuilderConfig = DECK_BUILDER,
    set_profile: SetProfile | None = None,
) -> BuildSheet:
    """Select spells and lands for an exactly sized Limited deck.
    The spell count is reselected when 16- or 18-land curve rules apply.
    """

    _validate_deck_builder_config(config=config)
    resolved_pair = _optional_pair(value=pair)
    if resolved_pair is None:
        raise DeckBuilderError("A color pair is required before selecting a build sheet.")

    structure_targets = _structure_targets_for_pair(
        ratings_data=ratings_data,
        pair=resolved_pair,
    )
    effective_config = _config_with_structure_targets(
        config=config,
        structure_targets=structure_targets,
    )
    _validate_deck_builder_config(config=effective_config)
    spell_selection = select_deck_spells(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        pair=resolved_pair,
        ratings_data=ratings_data,
        allow_splash=allow_splash,
        config=effective_config,
        set_profile=set_profile,
    )
    seen_targets = {spell_selection.counts.total}
    for _ in range(effective_config.land_count_iteration_limit):
        land_count = _curve_land_count(
            selection=spell_selection,
            config=effective_config,
        )[0]
        desired_spell_count = max(0, effective_config.deck_size - land_count)
        if desired_spell_count == spell_selection.counts.total:
            break

        if desired_spell_count in seen_targets:
            break

        seen_targets.add(desired_spell_count)
        spell_selection = select_deck_spells(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
            pair=resolved_pair,
            ratings_data=ratings_data,
            allow_splash=allow_splash,
            config=replace(effective_config, target_spell_count=desired_spell_count),
            set_profile=set_profile,
        )

    mana_base = select_mana_base(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        pair=resolved_pair,
        spell_selection=spell_selection,
        config=effective_config,
    )
    if mana_base.total_cards != effective_config.deck_size:
        mana_base = select_mana_base(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
            pair=resolved_pair,
            spell_selection=spell_selection,
            land_count=max(0, effective_config.deck_size - spell_selection.counts.total),
            reason="deck-size fill after spell-count relaxation",
            config=effective_config,
        )

    return BuildSheet(
        spell_selection=spell_selection,
        mana_base=mana_base,
        role_ledger=evaluate_completed_pool_role_ledger(
            final_pool=pool_grp_ids,
            card_database=card_database,
            ratings_data=ratings_data,
            set_profile=set_profile,
            likely_pair=resolved_pair,
        ),
    )


def build_deck_from_pool(
    *,
    pool: BuildPool,
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    forced_pair: str | None = None,
    allow_splash: bool = SPLASH.enabled_by_default,
    config: DeckBuilderConfig = DECK_BUILDER,
    set_profile: SetProfile | None = None,
) -> tuple[PairSelection, BuildSheet]:
    """Run pair selection, spell selection, and mana-base selection.
    CLI, replay, and watch share this helper for identical build sheets.
    """

    selection = select_color_pair(
        pool_grp_ids=pool.pool_grp_ids,
        card_database=card_database,
        ratings_data=ratings_data,
        forced_pair=forced_pair,
        config=config,
        set_profile=set_profile,
    )
    build_sheet = select_build_sheet(
        pool_grp_ids=pool.pool_grp_ids,
        card_database=card_database,
        pair=selection.chosen.pair,
        ratings_data=ratings_data,
        allow_splash=allow_splash,
        config=config,
        set_profile=set_profile,
    )
    return selection, build_sheet


def select_mana_base(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    pair: str,
    spell_selection: SpellSelection,
    land_count: int | None = None,
    reason: str | None = None,
    config: DeckBuilderConfig = DECK_BUILDER,
) -> ManaBase:
    """Select drafted nonbasic lands and split basics by colored pips.
    Per-main-color source floors are enforced when the land slots allow it.
    """

    _validate_deck_builder_config(config=config)
    resolved_pair = _optional_pair(value=pair)
    if resolved_pair is None:
        raise DeckBuilderError("A color pair is required before selecting lands.")

    curve_land_count, curve_reason = _curve_land_count(
        selection=spell_selection,
        config=config,
    )
    resolved_land_count = curve_land_count if land_count is None else land_count
    if resolved_land_count < 0:
        raise DeckBuilderError("Mana base land count must be non-negative.")

    effective_reason = curve_reason if reason is None else reason
    nonbasic_lands = _selected_nonbasic_lands(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        pair=resolved_pair,
        land_count=resolved_land_count,
        splash_colors=_selected_splash_colors(
            cards=spell_selection.spells,
            pair=resolved_pair,
        ),
    )
    splash_colors = _selected_splash_colors(
        cards=spell_selection.spells,
        pair=resolved_pair,
    )
    mana_colors = resolved_pair + "".join(splash_colors)
    splash_basics = _splash_basic_land_counts(
        splash_colors=splash_colors,
        selected_spells=spell_selection.spells,
        pair=resolved_pair,
        nonbasic_lands=nonbasic_lands,
    )
    basic_slots = max(
        0,
        resolved_land_count - len(nonbasic_lands) - sum(
            basic.count for basic in splash_basics
        ),
    )
    pip_counts, double_pip_counts = _spell_pip_counts(
        cards=spell_selection.spells,
        pair=mana_colors,
    )
    source_counts = _source_counts(
        pair=mana_colors,
        nonbasic_lands=nonbasic_lands,
    )
    base_basic_counts = _basic_land_counts(
        pair=resolved_pair,
        slots=basic_slots,
        pip_counts=pip_counts,
        double_pip_counts=double_pip_counts,
        source_counts=source_counts,
        config=config,
    )
    basic_counts = base_basic_counts + splash_basics
    final_source_counts = _source_counts_with_basics(
        pair=mana_colors,
        nonbasic_lands=nonbasic_lands,
        basic_lands=basic_counts,
    )
    return ManaBase(
        pair=resolved_pair,
        land_count=resolved_land_count,
        spell_count=spell_selection.counts.total,
        deck_size=config.deck_size,
        nonbasic_lands=nonbasic_lands,
        basic_lands=basic_counts,
        pip_counts=_ordered_color_items(values=pip_counts, pair=mana_colors),
        double_pip_counts=_ordered_color_items(values=double_pip_counts, pair=mana_colors),
        source_counts=_ordered_color_items(values=final_source_counts, pair=mana_colors),
        average_mana_value=_average_mana_value(cards=spell_selection.spells),
        reason=effective_reason,
        caveats=_mana_base_caveats(
            land_count=resolved_land_count,
            nonbasic_lands=nonbasic_lands,
            config=config,
        ),
    )


def format_build_result(
    *,
    pool: BuildPool,
    selection: PairSelection,
    spell_selection: SpellSelection | None = None,
    mana_base: ManaBase | None = None,
    config: DeckBuilderConfig = DECK_BUILDER,
) -> str:
    """Format deterministic plain-text deck-builder output.
    Complete builds start with the suggested deck before diagnostics.
    """

    if spell_selection is not None and mana_base is not None:
        lines = _format_player_build_result(
            pool=pool,
            selection=selection,
            spell_selection=spell_selection,
            mana_base=mana_base,
            config=config,
        )
    else:
        lines = _format_pair_selection_result(
            pool=pool,
            selection=selection,
            config=config,
        )
        if spell_selection is not None:
            lines.extend(_format_spell_selection(selection=spell_selection, config=config))

    lines.append(selection.attribution)
    return "\n".join(lines) + "\n"


def _format_player_build_result(
    *,
    pool: BuildPool,
    selection: PairSelection,
    spell_selection: SpellSelection,
    mana_base: ManaBase,
    config: DeckBuilderConfig,
) -> list[str]:
    chosen_label = "forced" if selection.forced_pair is not None else "automatic"
    lines = [
        "Suggested deck",
        f"Set: {format_set_label(set_code=pool.set_code)}",
        f"Pool: {pool.source_label}",
        f"Pool size: {selection.pool_size} cards",
    ]
    _append_optional_pool_context(lines=lines, pool=pool)
    lines.append(
        f"Color pair: {selection.chosen.pair} "
        f"({chosen_label}; 17Lands WR {_format_win_rate(score=selection.chosen)})"
    )
    lines.extend(
        _format_player_build_sheet(
            spell_selection=spell_selection,
            mana_base=mana_base,
            config=config,
        )
    )
    lines.extend(
        _format_pair_analysis(
            selection=selection,
            config=config,
        )
    )
    return lines


def _format_pair_selection_result(
    *,
    pool: BuildPool,
    selection: PairSelection,
    config: DeckBuilderConfig,
) -> list[str]:
    lines = [
        "Deck builder pair selection",
        f"Pool: {pool.source_label}",
        f"Set: {format_set_label(set_code=pool.set_code)}",
        f"Pool size: {selection.pool_size} cards",
    ]
    _append_optional_pool_context(lines=lines, pool=pool)
    lines.extend(_format_pair_analysis(selection=selection, config=config))
    return lines


def _append_optional_pool_context(*, lines: list[str], pool: BuildPool) -> None:
    if pool.account_id is not None:
        lines.append(f"Account: {pool.account_id}")

    if pool.draft_id is not None:
        lines.append(f"Draft: {pool.draft_id}")


def _format_pair_analysis(
    *,
    selection: PairSelection,
    config: DeckBuilderConfig,
) -> list[str]:
    chosen_label = "forced" if selection.forced_pair is not None else "automatic"
    lines = [
        "",
        "Color-pair reasoning:",
        (
            "Diagnostic: compares playable-card quality with 17Lands color-pair "
            "context; it is not another decklist."
        ),
        f"Chosen pair: {selection.chosen.pair} ({chosen_label}, strength "
        f"{_format_score(selection.chosen.blended_score, config=config)})",
        f"Runner-up: {selection.runner_up.pair} (strength "
        f"{_format_score(selection.runner_up.blended_score, config=config)})",
        f"Strength gap: {_format_score(selection.score_gap, config=config)}",
    ]
    if selection.forced_pair is not None:
        lines.append(
            f"Best automatic pair: {selection.automatic.pair} (strength "
            f"{_format_score(selection.automatic.blended_score, config=config)})"
        )

    lines.append("Pair strengths:")
    for score in selection.ranked_scores:
        lines.append(
            _format_pair_score(
                score=score,
                target_spell_count=selection.target_spell_count,
                config=config,
            )
        )

    return lines



def _pool_from_payload(*, payload: Any, source_label: str) -> BuildPool:
    if isinstance(payload, dict):
        pool_value = payload.get("pool_grp_ids", payload.get("pool", payload.get("cards")))
        return BuildPool(
            set_code=_optional_string(payload.get("set_code")) or "",
            pool_grp_ids=_int_tuple(value=pool_value, field_name="pool_grp_ids"),
            source_label=source_label,
            account_id=_optional_string(payload.get("account_id")),
            draft_id=_optional_string(payload.get("draft_id")),
        )

    if isinstance(payload, list):
        return BuildPool(
            set_code="",
            pool_grp_ids=_int_tuple(value=payload, field_name="pool"),
            source_label=source_label,
        )

    raise DeckBuilderError("Pool file must contain a JSON object or list of grpIds.")



def _resolve_set_code(
    *,
    explicit_set_code: str | None,
    payload_set_code: str,
    source_label: str,
) -> str:
    set_code = explicit_set_code or payload_set_code
    if set_code == "":
        raise DeckBuilderError(
            f"Pool file {source_label} must include set_code or be used with --set-code."
        )

    return set_code.upper()



def _matching_states(
    *,
    states: tuple[DraftState, ...],
    account_id: str | None,
    draft_id: str | None,
) -> tuple[DraftState, ...]:
    return tuple(
        state
        for state in states
        if (account_id is None or state.account_id == account_id)
        and (draft_id is None or state.draft_id == draft_id)
    )



def _latest_state(*, states: tuple[DraftState, ...]) -> DraftState:
    return max(
        states,
        key=lambda state: (state.updated_at, state.account_id, state.draft_id),
    )



def _missing_persisted_pool_message(
    *,
    account_id: str | None,
    draft_id: str | None,
) -> str:
    if account_id is not None and draft_id is not None:
        return f"No persisted pool found for account {account_id!r} and draft {draft_id!r}."

    if account_id is not None:
        return f"No persisted pool found for account {account_id!r}."

    if draft_id is not None:
        return f"No persisted pool found for draft {draft_id!r}."

    return "No persisted pools found. Pass --pool or replay/watch a draft first."


def _validate_metadata_coverage(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    config: DeckBuilderConfig,
) -> None:
    if not pool_grp_ids:
        raise DeckBuilderError("Deck build unavailable: pool is empty.")

    unresolved_grp_ids = _unresolved_pool_grp_ids(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
    )
    if not unresolved_grp_ids:
        return

    unresolved_ratio = len(unresolved_grp_ids) / len(pool_grp_ids)
    if unresolved_ratio > config.maximum_unresolved_metadata_ratio:
        raise DeckBuilderError(
            _metadata_missing_message(
                pool_size=len(pool_grp_ids),
                unresolved_grp_ids=unresolved_grp_ids,
                detail=(
                    "Too much of the pool is unresolved for reliable "
                    "playable-card detection."
                ),
            )
        )


def _validate_playable_pair_scores(
    *,
    scores: tuple[PairScore, ...],
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    config: DeckBuilderConfig,
) -> None:
    best_playable_count = max((score.playable_count for score in scores), default=0)
    if best_playable_count <= 0:
        unresolved_grp_ids = _unresolved_pool_grp_ids(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
        )
        if unresolved_grp_ids:
            raise DeckBuilderError(
                _metadata_missing_message(
                    pool_size=len(pool_grp_ids),
                    unresolved_grp_ids=unresolved_grp_ids,
                    detail="No playable spells could be identified from the known cards.",
                )
            )

        raise DeckBuilderError(
            "Deck build unavailable: no playable spells were detected in the pool, "
            "so no automatic color pair can be trusted."
        )

    _validate_playable_count_with_metadata(
        playable_count=best_playable_count,
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        target_spell_count=config.target_spell_count,
        label="playable spells",
    )


def _validate_spell_candidates(
    *,
    candidates: tuple[ScoredCard, ...],
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    pair: str,
    config: DeckBuilderConfig,
) -> None:
    if not candidates:
        unresolved_grp_ids = _unresolved_pool_grp_ids(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
        )
        if unresolved_grp_ids:
            raise DeckBuilderError(
                _metadata_missing_message(
                    pool_size=len(pool_grp_ids),
                    unresolved_grp_ids=unresolved_grp_ids,
                    detail=(
                        f"No playable {pair} spells could be identified "
                        "from the known cards."
                    ),
                )
            )

        raise DeckBuilderError(
            f"Deck build unavailable: no playable spells were detected for pair {pair}."
        )

    _validate_playable_count_with_metadata(
        playable_count=len(candidates),
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        target_spell_count=config.target_spell_count,
        label=f"playable {pair} spells",
    )


def _validate_playable_count_with_metadata(
    *,
    playable_count: int,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    target_spell_count: int,
    label: str,
) -> None:
    unresolved_grp_ids = _unresolved_pool_grp_ids(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
    )
    if not unresolved_grp_ids:
        return

    required_count = min(target_spell_count, len(pool_grp_ids))
    if playable_count >= required_count:
        return

    raise DeckBuilderError(
        _metadata_missing_message(
            pool_size=len(pool_grp_ids),
            unresolved_grp_ids=unresolved_grp_ids,
            detail=(
                f"Only {playable_count} {label} could be identified, below the "
                f"{required_count}-card target for this pool."
            ),
        )
    )


def _unresolved_pool_grp_ids(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
) -> tuple[int, ...]:
    return tuple(
        grp_id
        for grp_id in pool_grp_ids
        if card_database.lookup(grp_id=grp_id).unknown
    )


def _metadata_missing_message(
    *,
    pool_size: int,
    unresolved_grp_ids: tuple[int, ...],
    detail: str,
) -> str:
    unresolved_count = len(unresolved_grp_ids)
    unresolved_percent = (unresolved_count / pool_size) * 100.0
    return (
        "Card metadata is missing for "
        f"{unresolved_count}/{pool_size} picked cards "
        f"({unresolved_percent:.0f}%). "
        f"{detail} "
        "The build cannot be trusted, so no deck was produced. "
        "Run `draftomen-tui refresh-data` or pass `--bulk-file` with current card data, "
        "then build again. "
        f"Unresolved grpIds: {_format_grp_id_preview(grp_ids=unresolved_grp_ids)}."
    )


def _format_grp_id_preview(*, grp_ids: tuple[int, ...]) -> str:
    unique_grp_ids = tuple(dict.fromkeys(grp_ids))
    preview = unique_grp_ids[:5]
    suffix = ""
    if len(unique_grp_ids) > len(preview):
        suffix = f", +{len(unique_grp_ids) - len(preview)} more"

    return ", ".join(str(grp_id) for grp_id in preview) + suffix


def _score_pair(
    *,
    pair: str,
    scored_cards: tuple[ScoredCard, ...],
    ratings_data: SeventeenLandsData | None,
    config: DeckBuilderConfig,
) -> PairScore:
    playable_cards = tuple(
        card for card in scored_cards if _is_base_eligible_spell_for_pair(card=card, pair=pair)
    )
    top_cards = playable_cards[: config.target_spell_count]
    playable_score_sum = sum(card.raw_score for card in top_cards)
    average_playable_score = playable_score_sum / config.target_spell_count
    pair_win_rate = _pair_win_rate(pair=pair, ratings_data=ratings_data)
    pair_win_rate_score = _pair_win_rate_score(pair_win_rate=pair_win_rate, config=config)
    blended_score = _blended_score(
        average_playable_score=average_playable_score,
        pair_win_rate_score=pair_win_rate_score,
        config=config,
    )
    return PairScore(
        pair=pair,
        playable_count=len(playable_cards),
        playable_score_sum=playable_score_sum,
        average_playable_score=average_playable_score,
        pair_win_rate=pair_win_rate,
        pair_win_rate_score=pair_win_rate_score,
        blended_score=blended_score,
    )



def _is_base_eligible_spell_for_pair(*, card: ScoredCard, pair: str) -> bool:
    return _is_spell_card(card=card.card) and _is_playable_in_pair(card=card, pair=pair)



def _is_eligible_spell_for_pair(
    *,
    card: ScoredCard,
    pair: str,
    allow_splash: bool,
    splash_color: str | None,
    splash_limit: int,
    config: DeckBuilderConfig,
) -> bool:
    if _is_base_eligible_spell_for_pair(card=card, pair=pair):
        return True

    if not allow_splash or splash_limit <= 0 or splash_color is None:
        return False

    if not _is_spell_card(card=card.card):
        return False

    if card.raw_score < config.splash_elite_score_minimum:
        return False

    candidate_color, off_color_pips = splash_requirement(
        card=card.card,
        base_pair=pair,
    )
    if candidate_color != splash_color:
        return False

    if off_color_pips > SPLASH.maximum_off_color_pips:
        return False

    return grade_at_least(
        grade=card.rating.letter_grade,
        minimum=SPLASH.supported_minimum_grade,
    )



def _is_playable_in_pair(*, card: ScoredCard, pair: str) -> bool:
    return _card_is_playable_in_pair(card=card.card, pair=pair)



def _card_is_playable_in_pair(*, card: CardInfo, pair: str) -> bool:
    return card_is_castable_in_pair(card=card, base_pair=pair)



def _is_spell_card(*, card: CardInfo) -> bool:
    for type_line in card.types:
        faces = tuple(part.strip() for part in type_line.split("//"))
        if any(_type_face_is_spell(face=face) for face in faces):
            return True

    return False



def _type_face_is_spell(*, face: str) -> bool:
    if "Land" in face:
        return False

    return any(marker in face for marker in SPELL_TYPE_MARKERS)



def _card_has_type(*, card: CardInfo, marker: str) -> bool:
    return any(
        marker in face
        for type_line in card.types
        for face in type_line.split("//")
    )


def _is_creature_card(*, card: CardInfo) -> bool:
    return _card_has_type(card=card, marker="Creature")


def _is_instant_card(*, card: CardInfo) -> bool:
    return _card_has_type(card=card, marker="Instant")



def _is_two_drop(*, card: ScoredCard, config: DeckBuilderConfig) -> bool:
    return card.card.mana_value == config.two_drop_mana_value



def _is_expensive_spell(*, card: ScoredCard, config: DeckBuilderConfig) -> bool:
    mana_value = card.card.mana_value
    return mana_value is not None and mana_value >= config.expensive_spell_mana_value



def _structure_targets_for_pair(
    *,
    ratings_data: SeventeenLandsData | None,
    pair: str,
) -> StructuralTargets | None:
    if ratings_data is None:
        return None

    return ratings_data.structure_targets_for(pair=pair)



def _config_with_structure_targets(
    *,
    config: DeckBuilderConfig,
    structure_targets: StructuralTargets | None,
) -> DeckBuilderConfig:
    if structure_targets is None:
        return config

    land_count = _clamp_int(
        value=_round_half_up(structure_targets.average_land_count),
        lower=0,
        upper=config.deck_size,
    )
    creature_center = _round_half_up(structure_targets.average_creature_count)
    creature_floor = _clamp_int(
        value=math.floor(structure_targets.average_creature_count),
        lower=0,
        upper=config.target_spell_count,
    )
    creature_ceiling = _clamp_int(
        value=max(creature_center, math.ceil(structure_targets.average_creature_count)),
        lower=creature_floor,
        upper=config.target_spell_count,
    )
    target_spell_count = config.target_spell_count
    if config.target_spell_count == DECK_BUILDER.target_spell_count:
        target_spell_count = config.deck_size - land_count

    return replace(
        config,
        target_spell_count=target_spell_count,
        default_land_count=land_count,
        creature_floor=creature_floor,
        creature_ceiling=creature_ceiling,
        minimum_two_drops=_clamp_int(
            value=_round_half_up(structure_targets.average_two_drop_count),
            lower=0,
            upper=target_spell_count,
        ),
        maximum_expensive_spells=_clamp_int(
            value=math.ceil(structure_targets.average_expensive_spell_count),
            lower=0,
            upper=target_spell_count,
        ),
    )



def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))



def _clamp_int(*, value: int, lower: int, upper: int) -> int:
    return min(max(value, lower), upper)



def _card_splash_colors(*, card: CardInfo, pair: str) -> tuple[str, ...]:
    return tuple(color for color in card.colors if color not in pair)



def _is_splash_card(*, card: CardInfo, pair: str) -> bool:
    return bool(_card_splash_colors(card=card, pair=pair))



def _selected_splash_colors(
    *,
    cards: tuple[ScoredCard, ...],
    pair: str,
) -> tuple[str, ...]:
    splash_colors = {
        color
        for scored_card in cards
        for color in _card_splash_colors(card=scored_card.card, pair=pair)
    }
    return tuple(color for color in BASIC_LANDS_BY_COLOR if color in splash_colors)


def _supported_splash_card_limit(
    *,
    state: SplashState,
    config: DeckBuilderConfig,
) -> int:
    color = state.active_color
    if not state.enabled or color is None:
        return 0

    available_sources = (
        state.fixing_for(color=color) + SPLASH.planned_basic_sources
    )
    if available_sources >= SPLASH.multiple_card_sources:
        return min(config.splash_max_cards, SPLASH.maximum_cards)

    if available_sources >= SPLASH.single_card_sources:
        return min(config.splash_max_cards, 1)

    return 0


def _constraints_for_plan(
    *,
    candidates: tuple[ScoredCard, ...],
    pair: str,
    splash_limit: int,
    plan: _ConstraintPlan,
    config: DeckBuilderConfig,
) -> SpellConstraints:
    requested_target = min(config.target_spell_count, len(candidates))
    pool_counts = _spell_counts(cards=candidates, pair=pair, config=config)
    resolved_splash_limit = min(splash_limit, requested_target)
    non_splash_count = pool_counts.total - pool_counts.splashes
    target = min(requested_target, non_splash_count + resolved_splash_limit)

    creature_floor = (
        min(config.creature_floor, pool_counts.creatures, target)
        if plan.enforce_creature_floor
        else 0
    )
    if plan.enforce_creature_ceiling:
        requested_ceiling = min(config.creature_ceiling, target)
        needed_creatures = max(0, target - pool_counts.noncreatures)
        creature_ceiling = min(
            target,
            max(requested_ceiling, needed_creatures, creature_floor),
        )
    else:
        creature_ceiling = target

    minimum_two_drops = (
        min(config.minimum_two_drops, pool_counts.two_drops, target)
        if plan.enforce_two_drop_minimum
        else 0
    )
    if plan.enforce_expensive_cap:
        requested_cap = min(config.maximum_expensive_spells, target)
        needed_expensive = max(0, target - pool_counts.non_expensive)
        maximum_expensive_spells = min(target, max(requested_cap, needed_expensive))
    else:
        maximum_expensive_spells = target

    return SpellConstraints(
        spell_count=target,
        creature_floor=creature_floor,
        creature_ceiling=creature_ceiling,
        minimum_two_drops=minimum_two_drops,
        maximum_expensive_spells=maximum_expensive_spells,
        maximum_splash_spells=min(resolved_splash_limit, target),
    )



def _constraint_plans() -> tuple[_ConstraintPlan, ...]:
    return (
        _ConstraintPlan(
            enforce_expensive_cap=True,
            enforce_two_drop_minimum=True,
            enforce_creature_ceiling=True,
            enforce_creature_floor=True,
        ),
        _ConstraintPlan(
            enforce_expensive_cap=False,
            enforce_two_drop_minimum=True,
            enforce_creature_ceiling=True,
            enforce_creature_floor=True,
        ),
        _ConstraintPlan(
            enforce_expensive_cap=False,
            enforce_two_drop_minimum=False,
            enforce_creature_ceiling=True,
            enforce_creature_floor=True,
        ),
        _ConstraintPlan(
            enforce_expensive_cap=False,
            enforce_two_drop_minimum=False,
            enforce_creature_ceiling=False,
            enforce_creature_floor=True,
        ),
        _ConstraintPlan(
            enforce_expensive_cap=False,
            enforce_two_drop_minimum=False,
            enforce_creature_ceiling=False,
            enforce_creature_floor=False,
        ),
    )


def _build_suffix_feasibility(
    *,
    candidates: tuple[ScoredCard, ...],
    pair: str,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
) -> _SuffixFeasibility:
    """Index exact completion states for every candidate suffix."""
    candidate_features = tuple(
        (
            int(_is_creature_card(card=card.card)),
            int(_is_two_drop(card=card, config=config)),
            int(_is_expensive_spell(card=card, config=config)),
            int(_is_splash_card(card=card.card, pair=pair)),
        )
        for card in candidates
    )
    minimum_two_drops = constraints.minimum_two_drops
    states_by_start: list[frozenset[tuple[int, int, int, int, int]]] = [
        frozenset(
            (
                (0, creatures, minimum_two_drops, expensive, splashes)
                for creatures in range(
                    constraints.creature_floor, constraints.creature_ceiling + 1
                )
                for expensive in range(constraints.maximum_expensive_spells + 1)
                for splashes in range(constraints.maximum_splash_spells + 1)
            )
        )
    ]
    for feature in reversed(candidate_features):
        next_states = states_by_start[-1]
        states = set(next_states)
        creature, two_drop, expensive, splash = feature
        for slots, creatures, two_drops, expensive_spells, splashes in next_states:
            if slots >= constraints.spell_count:
                continue
            if creatures < creature or expensive_spells < expensive:
                continue
            if splashes < splash:
                continue
            current_slots = slots + 1
            current_creatures = creatures - creature
            current_expensive = expensive_spells - expensive
            current_splashes = splashes - splash
            if two_drop:
                if minimum_two_drops == 0:
                    current_two_drops = (0,)
                elif two_drops == minimum_two_drops:
                    current_two_drops = (minimum_two_drops - 1, minimum_two_drops)
                elif two_drops > 0:
                    current_two_drops = (two_drops - 1,)
                else:
                    current_two_drops = ()
            else:
                current_two_drops = (two_drops,)
            states.update(
                (
                    current_slots,
                    current_creatures,
                    current_two_drops_value,
                    current_expensive,
                    current_splashes,
                )
                for current_two_drops_value in current_two_drops
            )
        states_by_start.append(frozenset(states))
    states_by_start.reverse()
    return _SuffixFeasibility(
        states_by_start=tuple(states_by_start),
        candidate_features=candidate_features,
        spell_count=constraints.spell_count,
        minimum_two_drops=minimum_two_drops,
    )


def _counts_after_feature(
    *,
    counts: SpellCounts,
    feature: tuple[int, int, int, int],
) -> SpellCounts:
    creature, two_drop, expensive, splash = feature
    return SpellCounts(
        total=counts.total + 1,
        creatures=counts.creatures + creature,
        two_drops=counts.two_drops + two_drop,
        expensive=counts.expensive + expensive,
        splashes=counts.splashes + splash,
        instants=0,
    )


def _can_add_spell_with_suffix(
    *,
    counts: SpellCounts,
    feature: tuple[int, int, int, int],
    suffix_start: int,
    suffix_feasibility: _SuffixFeasibility,
    constraints: SpellConstraints,
) -> bool:
    creature, two_drop, expensive, splash = feature
    next_total = counts.total + 1
    next_creatures = counts.creatures + creature
    next_two_drops = counts.two_drops + two_drop
    next_expensive = counts.expensive + expensive
    next_splashes = counts.splashes + splash
    if next_total > constraints.spell_count:
        return False
    if next_creatures > constraints.creature_ceiling:
        return False
    if next_expensive > constraints.maximum_expensive_spells:
        return False
    if next_splashes > constraints.maximum_splash_spells:
        return False
    return suffix_feasibility.can_complete_values(
        total=next_total,
        creatures=next_creatures,
        two_drops=next_two_drops,
        expensive=next_expensive,
        splashes=next_splashes,
        start=suffix_start,
        constraints=constraints,
    )


def _select_with_constraints(
    *,
    candidates: tuple[ScoredCard, ...],
    available_quantities: Counter[CardQuantityKey],
    pair: str,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
    set_profile: SetProfile | None = None,
) -> tuple[ScoredCard, ...] | None:
    """Select one feasible package with bounded deterministic beam search.
    The best complete candidate receives bounded local improvement.
    """

    if constraints.spell_count == 0:
        return ()

    candidates = _limit_cards_to_pool_quantities(
        cards=candidates,
        available_quantities=available_quantities,
    )
    objective_context = _build_optimizer_context(
        candidates=candidates,
        pair=pair,
        set_profile=set_profile,
    )
    suffix_feasibility = _build_suffix_feasibility(
        candidates=candidates,
        pair=pair,
        constraints=constraints,
        config=config,
    )
    empty_counts = SpellCounts(
        total=0,
        creatures=0,
        two_drops=0,
        expensive=0,
        splashes=0,
        instants=0,
    )
    initial_aggregate: _OptimizerAggregate | None = (
        _OptimizerAggregate() if objective_context.active else None
    )
    beam: list[
        tuple[
            float,
            tuple[int, ...],
            tuple[ScoredCard, ...],
            SpellCounts,
            float,
            float,
            _OptimizerAggregate | None,
        ]
    ] = [(0.0, (), (), empty_counts, 0.0, 0.0, initial_aggregate)]
    node_count = 0
    for _ in range(constraints.spell_count):
        expansions: list[
            tuple[
                float,
                tuple[int, ...],
                tuple[ScoredCard, ...],
                SpellCounts,
                float,
                float,
                _OptimizerAggregate | None,
            ]
        ] = []
        for _, indices, selected, counts, raw_score_sum, mana_value_sum, aggregate in beam:
            start = indices[-1] + 1 if indices else 0
            for candidate_index in range(start, len(candidates)):
                if node_count >= config.optimizer_max_search_nodes:
                    break
                node_count += 1
                if not _can_add_spell_with_suffix(
                    counts=counts,
                    feature=suffix_feasibility.candidate_features[candidate_index],
                    suffix_start=candidate_index + 1,
                    suffix_feasibility=suffix_feasibility,
                    constraints=constraints,
                ):
                    continue
                candidate = candidates[candidate_index]
                next_selected = (*selected, candidate)
                next_counts = _counts_after_feature(
                    counts=counts,
                    feature=suffix_feasibility.candidate_features[candidate_index],
                )
                next_raw_score_sum = raw_score_sum + candidate.raw_score
                next_mana_value_sum = mana_value_sum + (
                    candidate.card.mana_value or 0.0
                )
                if aggregate is None:
                    next_aggregate = None
                else:
                    next_aggregate = _optimizer_add_card(
                        aggregate=aggregate,
                        evidence=objective_context.card_evidence[candidate_index],
                        context=objective_context,
                    )
                expansions.append(
                    (
                        _optimizer_objective_from_aggregates(
                            counts=next_counts,
                            raw_score_sum=next_raw_score_sum,
                            mana_value_sum=next_mana_value_sum,
                            constraints=constraints,
                            config=config,
                            aggregate=next_aggregate,
                            objective_context=objective_context,
                        ),
                        (*indices, candidate_index),
                        next_selected,
                        next_counts,
                        next_raw_score_sum,
                        next_mana_value_sum,
                        next_aggregate,
                    )
                )
        if not expansions:
            break
        expansions.sort(key=lambda item: (-item[0], item[1]))
        beam = expansions[: config.optimizer_beam_width]

    complete: list[tuple[float | None, tuple[ScoredCard, ...]]] = [
        (score, cards)
        for score, _, cards, counts, _, _, _ in beam
        if len(cards) == constraints.spell_count
        and _counts_satisfy_constraints(counts=counts, constraints=constraints)
        and not _exceeds_available_card_quantities(
            cards=cards,
            available_quantities=available_quantities,
        )
    ]
    if not complete:
        fallback = _greedy_feasible_package(
            candidates=candidates,
            available_quantities=available_quantities,
            pair=pair,
            constraints=constraints,
            config=config,
        )
        if fallback is None:
            return None
        complete = [(None, fallback)]

    complete.sort(
        key=lambda item: (
            -float("-inf") if item[0] is None else -item[0],
            tuple(card.original_index for card in item[1]),
        )
    )
    package = complete[0][1]
    package, _, _ = _improve_complete_package(
        cards=package,
        candidates=candidates,
        available_quantities=available_quantities,
        pair=pair,
        constraints=constraints,
        config=config,
        evaluation_budget=max(0, config.optimizer_max_evaluations),
        objective_context=objective_context,
    )
    return _ordered_selected_package(
        cards=package,
        pair=pair,
        constraints=constraints,
        config=config,
    )




def _ordered_selected_package(
    *,
    cards: tuple[ScoredCard, ...],
    pair: str,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
) -> tuple[ScoredCard, ...]:
    """Restore the deterministic selection order for a chosen package.
    Package membership remains unchanged.
    """
    ordered: list[ScoredCard] = []
    remaining = list(cards)
    while remaining:
        counts = _spell_counts(cards=tuple(ordered), pair=pair, config=config)
        floor_unmet = counts.creatures < constraints.creature_floor
        next_index = min(
            range(len(remaining)),
            key=lambda index: _candidate_selection_sort_key(
                card=remaining[index],
                floor_unmet=floor_unmet,
                config=config,
            ),
        )
        ordered.append(remaining.pop(next_index))
    return tuple(ordered)


def _greedy_feasible_package(
    *,
    candidates: tuple[ScoredCard, ...],
    available_quantities: Counter[CardQuantityKey],
    pair: str,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
) -> tuple[ScoredCard, ...] | None:
    """Return a deterministic fallback when beam search finds no package.
    The fallback preserves normal feasibility checks.
    """

    selected: list[ScoredCard] = []
    remaining = list(candidates)
    while len(selected) < constraints.spell_count and remaining:
        counts = _spell_counts(cards=tuple(selected), pair=pair, config=config)
        floor_unmet = counts.creatures < constraints.creature_floor
        ordered_indices = tuple(
            sorted(
                range(len(remaining)),
                key=lambda index: _candidate_selection_sort_key(
                    card=remaining[index],
                    floor_unmet=floor_unmet,
                    config=config,
                ),
            )
        )
        picked_index = _first_feasible_index(
            ordered_indices=ordered_indices,
            selected=tuple(selected),
            remaining=tuple(remaining),
            available_quantities=available_quantities,
            constraints=constraints,
            pair=pair,
            config=config,
        )
        if picked_index is None:
            return None
        selected.append(remaining.pop(picked_index))
    result = tuple(selected)
    if not _counts_satisfy_constraints(
        counts=_spell_counts(cards=result, pair=pair, config=config),
        constraints=constraints,
    ):
        return None
    return result


def _improve_complete_package(
    *,
    cards: tuple[ScoredCard, ...],
    candidates: tuple[ScoredCard, ...],
    available_quantities: Counter[CardQuantityKey],
    pair: str,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
    evaluation_budget: int,
    objective_context: _OptimizerContext | None = None,
) -> tuple[tuple[ScoredCard, ...], int, float | None]:
    """Improve a feasible package through bounded one-card substitutions.
    Each accepted substitution is strictly better and remains constraint-safe.
    """

    current = tuple(sorted(cards, key=lambda card: card.original_index))
    if evaluation_budget <= 0 or config.optimizer_local_improvement_rounds <= 0:
        return current, 0, None

    evaluations = 0
    for _ in range(config.optimizer_local_improvement_rounds):
        if evaluations >= evaluation_budget:
            break
        selected_indices = {card.original_index for card in current}
        replacement_candidates = tuple(
            card for card in candidates if card.original_index not in selected_indices
        )[: config.optimizer_local_improvement_candidates]
        baseline = _optimizer_objective(
            cards=current,
            pair=pair,
            constraints=constraints,
            config=config,
            objective_context=objective_context,
        )
        evaluations += 1
        best = current
        best_score = baseline
        best_indices = tuple(card.original_index for card in current)
        for replacement in replacement_candidates:
            if evaluations >= evaluation_budget:
                break
            for position in range(len(current)):
                if evaluations >= evaluation_budget:
                    break
                trial = tuple(
                    sorted(
                        (*current[:position], replacement, *current[position + 1 :]),
                        key=lambda card: card.original_index,
                    )
                )
                if (
                    _exceeds_available_card_quantities(
                        cards=trial,
                        available_quantities=available_quantities,
                    )
                    or not _counts_satisfy_constraints(
                        counts=_spell_counts(cards=trial, pair=pair, config=config),
                        constraints=constraints,
                    )
                ):
                    continue
                score = _optimizer_objective(
                    cards=trial,
                    pair=pair,
                    constraints=constraints,
                    config=config,
                    objective_context=objective_context,
                )
                evaluations += 1
                if score > best_score + 1e-9:
                    best = trial
                    best_score = score
                    best_indices = tuple(card.original_index for card in trial)
                elif abs(score - best_score) <= 1e-9:
                    trial_indices = tuple(card.original_index for card in trial)
                    if trial_indices < best_indices:
                        best = trial
                        best_score = score
                        best_indices = trial_indices
        if best == current:
            break
        current = best
    return current, evaluations, best_score


def _optimizer_objective(
    *,
    cards: tuple[ScoredCard, ...],
    pair: str,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
    set_profile: SetProfile | None = None,
    objective_context: _OptimizerContext | None = None,
) -> float:
    """Score one complete package with generic and profile-backed terms.
    Profile evidence is bounded and absent from the generic fallback.
    """

    if not cards:
        return 0.0

    raw_score_sum = sum(card.raw_score for card in cards)
    counts = _spell_counts(cards=cards, pair=pair, config=config)
    mana_value_sum = sum(card.card.mana_value or 0.0 for card in cards)
    context = objective_context
    if context is None:
        context = _build_optimizer_context(
            candidates=cards,
            pair=pair,
            set_profile=set_profile,
        )
    if context.active:
        aggregate = _optimizer_aggregate_for_cards(cards=cards, context=context)
    else:
        aggregate = None
    return _optimizer_objective_from_aggregates(
        counts=counts,
        raw_score_sum=raw_score_sum,
        mana_value_sum=mana_value_sum,
        constraints=constraints,
        config=config,
        aggregate=aggregate,
        objective_context=context,
    )


def _optimizer_objective_from_aggregates(
    *,
    counts: SpellCounts,
    raw_score_sum: float,
    mana_value_sum: float,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
    aggregate: _OptimizerAggregate | None = None,
    objective_context: _OptimizerContext | None = None,
) -> float:
    """Score a package from exact structural, numeric, and role aggregates."""

    if counts.total == 0:
        return 0.0

    quality = raw_score_sum / counts.total
    curve = _optimizer_curve_score(
        counts=counts,
        mana_value_sum=mana_value_sum,
        constraints=constraints,
        config=config,
    )
    creature = _optimizer_creature_score(
        creature_count=counts.creatures,
        constraints=constraints,
    )
    score = (
        config.optimizer_quality_weight * quality
        + config.optimizer_curve_weight * curve
        + config.optimizer_creature_structure_weight * creature
    )
    if objective_context is None or not objective_context.active:
        return score
    evidence = aggregate or _OptimizerAggregate()
    role_fit = _optimizer_role_target_score(
        aggregate=evidence,
        pair_profile=objective_context.pair_profile,
    )
    removal = _optimizer_removal_score(
        aggregate=evidence,
        pair_profile=objective_context.pair_profile,
    )
    package = _optimizer_package_balance(aggregate=evidence, counts=counts)
    synergy = _optimizer_semantic_synergy(
        aggregate=evidence,
        counts=counts,
    )
    empirical = _optimizer_empirical_context(
        aggregate=evidence,
        counts=counts,
    )
    redundancy = _optimizer_redundancy(
        aggregate=evidence,
        pair_profile=objective_context.pair_profile,
        counts=counts,
    )
    unsupported = _optimizer_unsupported_payoffs(aggregate=evidence, counts=counts)
    mana_strain = _optimizer_mana_strain(aggregate=evidence, counts=counts)
    influence = objective_context.influence
    return score + influence * (
        config.optimizer_role_target_weight * role_fit
        + config.optimizer_effective_removal_weight * removal
        + config.optimizer_enabler_payoff_weight * package
        + config.optimizer_semantic_synergy_weight * synergy
        + config.optimizer_empirical_context_weight * empirical
        - config.optimizer_redundancy_weight * redundancy
        - config.optimizer_unsupported_payoff_weight * unsupported
        - config.optimizer_mana_strain_weight * mana_strain
    )


def _build_optimizer_context(
    *,
    candidates: tuple[ScoredCard, ...],
    pair: str,
    set_profile: SetProfile | None,
) -> _OptimizerContext:
    """Prepare bounded profile evidence once for one canonical pair."""
    if set_profile is None or set_profile.maturity is ProfileMaturity.GENERIC:
        return _OptimizerContext(pair=pair)
    maturity_weight = _PROFILE_MATURITY_WEIGHTS[set_profile.maturity]
    influence = _clamp(value=set_profile.confidence * maturity_weight, lower=0.0, upper=1.0)
    if influence <= 0.0:
        return _OptimizerContext(pair=pair)
    pair_profile = set_profile.pair(pair)
    pair_sample_count = None if set_profile.samples is None else set_profile.samples.count_for(pair)
    synergy_values: dict[tuple[str, str], float] = {}
    scarcity_values: dict[str, float] = {}
    if pair_profile is not None:
        for entry in pair_profile.synergy:
            sample_weight = _optimizer_sample_weight(
                entry_samples=entry.samples,
                pair_samples=pair_sample_count,
            )
            weighted_value = entry.value * sample_weight
            if weighted_value != 0.0:
                synergy_values[(entry.first_card, entry.second_card)] = weighted_value
        for entry in pair_profile.scarcity:
            sample_weight = _optimizer_sample_weight(
                entry_samples=entry.samples,
                pair_samples=pair_sample_count,
            )
            weighted_value = entry.value * sample_weight
            if weighted_value != 0.0:
                scarcity_values[entry.card_key] = weighted_value
    synergy_lookup = MappingProxyType(synergy_values)
    scarcity_lookup = MappingProxyType(scarcity_values)
    card_evidence = tuple(
        _optimizer_card_evidence(
            card=card,
            pair=pair,
            scarcity_lookup=scarcity_lookup,
        )
        for card in candidates
    )
    if pair_profile is None and not any(item.role_values for item in card_evidence):
        return _OptimizerContext(pair=pair)
    context = _OptimizerContext(
        pair=pair,
        influence=influence,
        pair_profile=pair_profile,
        synergy_lookup=synergy_lookup,
        scarcity_lookup=scarcity_lookup,
        card_evidence=card_evidence,
    )
    if not context.active:
        return _OptimizerContext(pair=pair)
    return context


def _optimizer_sample_weight(*, entry_samples: int | None, pair_samples: int | None) -> float:
    """Return deterministic bounded strength for one empirical entry."""

    sample_count = entry_samples if entry_samples is not None else pair_samples
    if sample_count is None or sample_count <= 0:
        return 0.0
    return _clamp(
        value=sample_count / (sample_count + 100.0),
        lower=0.0,
        upper=1.0,
    )



def _optimizer_card_evidence(
    *,
    card: ScoredCard,
    pair: str,
    scarcity_lookup: Mapping[str, float],
) -> _OptimizerCardEvidence:
    card_keys = _optimizer_card_keys(card=card.card)
    resolution = resolve_card_roles(card.card, profile=None)
    assignments = resolution.assignments
    role_values_counter: Counter[str] = Counter()
    removal_values_counter: Counter[str] = Counter()
    for assignment in assignments:
        role_values_counter[assignment.role.value] += assignment.confidence
        removal = _optimizer_removal_contribution(assignment=assignment)
        if removal is not None:
            kind, value = removal
            removal_values_counter[kind] += value
    package_enablers: Counter[str] = Counter()
    package_payoffs: Counter[str] = Counter()
    for package, (enabler_roles, payoff_roles) in PACKAGE_ROLES.items():
        package_enablers[package] = sum(
            role_values_counter[role.value] for role in enabler_roles
        )
        package_payoffs[package] = sum(
            role_values_counter[role.value] for role in payoff_roles
        )
    scarcity = _optimizer_profile_card_signal(
        card_keys=card_keys,
        scarcity_lookup=scarcity_lookup,
    )
    return _OptimizerCardEvidence(
        original_index=card.original_index,
        card_keys=card_keys,
        role_values=tuple(sorted(role_values_counter.items())),
        removal_values=tuple(sorted(removal_values_counter.items())),
        package_enablers=tuple(sorted(package_enablers.items())),
        package_payoffs=tuple(sorted(package_payoffs.items())),
        scarcity=scarcity,
        mana_strain=_optimizer_card_mana_strain(card=card.card, pair=pair),
    )

def _optimizer_card_keys(*, card: CardInfo) -> tuple[str, ...]:
    arena_id = card.arena_id if card.arena_id is not None else card.grp_id
    keys = [f"arena_id:{arena_id}", f"grp_id:{card.grp_id}"]
    if card.set_code and card.collector_number:
        keys.append(f"set:{card.set_code.casefold()}:{card.collector_number.casefold()}")
    oracle_id = card.oracle_id
    if oracle_id and oracle_id.strip():
        keys.append(f"oracle_id:{oracle_id.strip().casefold()}")
    if card.name.strip():
        keys.append(f"name:{card.name.casefold()}")
    return tuple(dict.fromkeys(keys))


def _optimizer_profile_card_signal(
    *,
    card_keys: tuple[str, ...],
    scarcity_lookup: Mapping[str, float],
) -> float:
    for key in card_keys:
        value = scarcity_lookup.get(key)
        if value is not None:
            return value
    return 0.0


def _optimizer_removal_contribution(
    *,
    assignment: RoleAssignment,
) -> tuple[str, float] | None:
    removal = assignment.removal
    if removal is None and assignment.role not in REMOVAL_ROLES:
        return None
    if assignment.role is Role.CONDITIONAL_REMOVAL:
        kind = "conditional"
    elif removal is not None:
        kind = "temporary" if removal.kind == "tap" else removal.kind
    elif assignment.role is Role.TEMPORARY_TAP:
        kind = "temporary"
    else:
        kind = REMOVAL_ROLE_KINDS.get(assignment.role)
    if kind is None or kind not in REMOVAL_WEIGHTS:
        return None
    value = 1.0 if removal is None else removal.effective_score
    if removal is not None and removal.conditional:
        value *= 0.75
    return kind, assignment.confidence * value * REMOVAL_WEIGHTS[kind]


def _optimizer_add_card(
    *,
    aggregate: _OptimizerAggregate,
    evidence: _OptimizerCardEvidence,
    context: _OptimizerContext,
) -> _OptimizerAggregate:
    synergy_value = aggregate.synergy_value
    for previous_keys in aggregate.card_keys:
        synergy_value += _optimizer_synergy_between(
            first_keys=previous_keys,
            second_keys=evidence.card_keys,
            context=context,
        )
    return _OptimizerAggregate(
        card_keys=(*aggregate.card_keys, evidence.card_keys),
        role_values=_optimizer_merge_values(aggregate.role_values, evidence.role_values),
        removal_values=_optimizer_merge_values(
            aggregate.removal_values,
            evidence.removal_values,
        ),
        package_enablers=_optimizer_merge_values(
            aggregate.package_enablers,
            evidence.package_enablers,
        ),
        package_payoffs=_optimizer_merge_values(
            aggregate.package_payoffs,
            evidence.package_payoffs,
        ),
        synergy_value=synergy_value,
        scarcity=aggregate.scarcity + evidence.scarcity,
        mana_strain=aggregate.mana_strain + evidence.mana_strain,
    )


def _optimizer_aggregate_for_cards(
    *,
    cards: tuple[ScoredCard, ...],
    context: _OptimizerContext,
) -> _OptimizerAggregate:
    aggregate = _OptimizerAggregate()
    for card in cards:
        evidence = next(
            (
                evidence
                for evidence in context.card_evidence
                if evidence.original_index == card.original_index
            ),
            None,
        )
        if evidence is None:
            evidence = _optimizer_card_evidence(
                card=card,
                pair=context.pair,
                scarcity_lookup=context.scarcity_lookup,
            )
        aggregate = _optimizer_add_card(
            aggregate=aggregate,
            evidence=evidence,
            context=context,
        )
    return aggregate


def _optimizer_merge_values(
    first: tuple[tuple[str, float], ...],
    second: tuple[tuple[str, float], ...],
) -> tuple[tuple[str, float], ...]:
    values: Counter[str] = Counter(dict(first))
    values.update(dict(second))
    return tuple(sorted(values.items()))


def _optimizer_synergy_between(
    *,
    first_keys: tuple[str, ...],
    second_keys: tuple[str, ...],
    context: _OptimizerContext,
) -> float:
    for first in first_keys:
        for second in second_keys:
            if first == second:
                continue
            pair = tuple(sorted((first, second)))
            value = context.synergy_lookup.get(pair)
            if value is not None:
                return value
    return 0.0


def _optimizer_role_target_score(
    *,
    aggregate: _OptimizerAggregate,
    pair_profile: PairProfile | None,
) -> float:
    if pair_profile is None or not pair_profile.role_targets:
        return 0.0
    values = dict(aggregate.role_values)
    fits = tuple(
        min(values.get(target.role.value, 0.0), target.value) / target.value
        for target in pair_profile.role_targets
        if target.value > 0
    )
    return 100.0 * (sum(fits) / len(fits) if fits else 0.0)


def _optimizer_removal_score(
    *,
    aggregate: _OptimizerAggregate,
    pair_profile: PairProfile | None,
) -> float:
    values = dict(aggregate.removal_values)
    if pair_profile is not None and pair_profile.removal_targets:
        fits = tuple(
            min(values.get("temporary" if target.kind == "tap" else target.kind, 0.0), target.value)
            / target.value
            for target in pair_profile.removal_targets
            if target.value > 0
        )
        return 100.0 * (sum(fits) / len(fits) if fits else 0.0)
    return 100.0 * _clamp(
        value=sum(values.values()) / 2.0,
        lower=0.0,
        upper=1.0,
    )


def _optimizer_diminishing_signal(value: float) -> float:
    """Map non-negative role evidence to a bounded diminishing-return signal."""
    return _clamp(
        value=1.0 - math.exp(-max(0.0, value)),
        lower=0.0,
        upper=1.0,
    )


def _optimizer_package_balance(
    *,
    aggregate: _OptimizerAggregate,
    counts: SpellCounts,
) -> float:
    enablers = dict(aggregate.package_enablers)
    payoffs = dict(aggregate.package_payoffs)
    fits = tuple(
        _optimizer_diminishing_signal(enablers.get(package, 0.0))
        * _optimizer_diminishing_signal(payoffs.get(package, 0.0))
        for package in PACKAGE_ROLES
        if enablers.get(package, 0.0) + payoffs.get(package, 0.0) > 0
    )
    return 100.0 * (sum(fits) / len(fits) if fits else 0.0)


def _optimizer_semantic_synergy(
    *,
    aggregate: _OptimizerAggregate,
    counts: SpellCounts,
) -> float:
    """Score selected-package semantic enabler/payoff support only."""

    enablers = dict(aggregate.package_enablers)
    payoffs = dict(aggregate.package_payoffs)
    supported = sum(
        _optimizer_diminishing_signal(enablers.get(package, 0.0))
        * _optimizer_diminishing_signal(payoffs.get(package, 0.0))
        for package in PACKAGE_ROLES
    )
    package_signal = _clamp(value=supported / max(1, counts.total), lower=0.0, upper=1.0)
    return 100.0 * package_signal


def _optimizer_empirical_context(
    *,
    aggregate: _OptimizerAggregate,
    counts: SpellCounts,
) -> float:
    """Score weighted empirical scarcity and signed pair synergy; negative evidence
    lowers the bounded package-ranking contribution."""

    scarcity = _clamp(
        value=aggregate.scarcity / max(1, counts.total),
        lower=0.0,
        upper=1.0,
    )
    direct_synergy = _clamp(
        value=aggregate.synergy_value / max(1.0, counts.total - 1),
        lower=-1.0,
        upper=1.0,
    )
    return 100.0 * _clamp(
        value=0.5 * scarcity + 0.5 * direct_synergy,
        lower=-1.0,
        upper=1.0,
    )


def _optimizer_redundancy(
    *,
    aggregate: _OptimizerAggregate,
    pair_profile: PairProfile | None,
    counts: SpellCounts,
) -> float:
    duplicate_excess = sum(
        max(0, count - 1)
        for count in Counter(keys[0] for keys in aggregate.card_keys).values()
    )
    role_excess = 0.0
    if pair_profile is not None:
        role_values = dict(aggregate.role_values)
        role_excess = sum(
            max(0.0, role_values.get(target.role.value, 0.0) - target.value * 1.5)
            for target in pair_profile.role_targets
        )
    enablers = dict(aggregate.package_enablers)
    payoffs = dict(aggregate.package_payoffs)
    package_excess = sum(
        max(0.0, enablers.get(package, 0.0) - payoffs.get(package, 0.0))
        for package in PACKAGE_ROLES
    )
    return 100.0 * _clamp(
        value=(duplicate_excess + role_excess + 0.25 * package_excess)
        / max(1, counts.total),
        lower=0.0,
        upper=1.0,
    )


def _optimizer_unsupported_payoffs(
    *,
    aggregate: _OptimizerAggregate,
    counts: SpellCounts,
) -> float:
    enablers = dict(aggregate.package_enablers)
    payoffs = dict(aggregate.package_payoffs)
    unsupported = sum(
        max(0.0, payoffs.get(package, 0.0) - enablers.get(package, 0.0))
        for package in PACKAGE_ROLES
    )
    return 100.0 * _clamp(
        value=unsupported / max(1, counts.total),
        lower=0.0,
        upper=1.0,
    )


def _optimizer_mana_strain(
    *,
    aggregate: _OptimizerAggregate,
    counts: SpellCounts,
) -> float:
    return 100.0 * _clamp(
        value=aggregate.mana_strain / max(1, counts.total),
        lower=0.0,
        upper=1.0,
    )


def _optimizer_card_mana_strain(*, card: CardInfo, pair: str) -> float:
    symbols = MANA_SYMBOL_PATTERN.findall(card.mana_cost or "")
    colored_symbols = tuple(
        symbol
        for symbol in symbols
        if any(color in symbol.upper() for color in BASIC_LANDS_BY_COLOR)
    )
    off_color = sum(
        1
        for symbol in colored_symbols
        if not any(color in symbol.upper() for color in pair)
    )
    pip_pressure = max(0, len(colored_symbols) - 1) * 0.25
    strain = off_color * 1.25 + pip_pressure
    return _clamp(
        value=strain / max(1.0, card.mana_value or 1.0),
        lower=0.0,
        upper=1.0,
    )


def _optimizer_curve_score(
    *,
    counts: SpellCounts,
    mana_value_sum: float,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
) -> float:
    average = mana_value_sum / counts.total if counts.total else 0.0
    two_drop_fit = min(1.0, counts.two_drops / max(1, constraints.minimum_two_drops))
    expensive_fit = 1.0 - min(
        1.0,
        counts.expensive / max(1, constraints.maximum_expensive_spells + 1),
    )
    center = (
        config.aggressive_average_mana_value_max
        + config.top_heavy_average_mana_value_min
    ) / 2.0
    average_fit = max(
        0.0,
        1.0 - abs(average - center) / max(center, 1.0),
    )
    return 100.0 * (
        0.45 * two_drop_fit
        + 0.25 * expensive_fit
        + 0.30 * average_fit
    )


def _optimizer_creature_score(
    *,
    creature_count: int,
    constraints: SpellConstraints,
) -> float:
    target = (constraints.creature_floor + constraints.creature_ceiling) / 2.0
    distance = abs(creature_count - target)
    return 100.0 * max(0.0, 1.0 - distance / max(1.0, target))


def _first_feasible_index(
    *,
    ordered_indices: tuple[int, ...],
    selected: tuple[ScoredCard, ...],
    remaining: tuple[ScoredCard, ...],
    available_quantities: Counter[CardQuantityKey],
    constraints: SpellConstraints,
    pair: str,
    config: DeckBuilderConfig,
) -> int | None:
    for index in ordered_indices:
        remaining_after = remaining[:index] + remaining[index + 1 :]
        if _can_add_spell(
            candidate=remaining[index],
            selected=selected,
            remaining_after=remaining_after,
            available_quantities=available_quantities,
            constraints=constraints,
            pair=pair,
            config=config,
        ):
            return index

    return None


def _can_add_spell(
    *,
    candidate: ScoredCard,
    selected: tuple[ScoredCard, ...],
    remaining_after: tuple[ScoredCard, ...],
    available_quantities: Counter[CardQuantityKey],
    constraints: SpellConstraints,
    pair: str,
    config: DeckBuilderConfig,
) -> bool:
    next_selected = (*selected, candidate)
    counts = _spell_counts(cards=next_selected, pair=pair, config=config)
    if counts.total > constraints.spell_count:
        return False

    if counts.creatures > constraints.creature_ceiling:
        return False

    if counts.expensive > constraints.maximum_expensive_spells:
        return False

    if counts.splashes > constraints.maximum_splash_spells:
        return False

    if _exceeds_available_card_quantities(
        cards=next_selected,
        available_quantities=available_quantities,
    ):
        return False

    return _can_complete_selection(
        counts=counts,
        remaining=remaining_after,
        constraints=constraints,
        pair=pair,
        config=config,
    )


def _can_complete_selection(
    *,
    counts: SpellCounts,
    remaining: tuple[ScoredCard, ...],
    constraints: SpellConstraints,
    pair: str,
    config: DeckBuilderConfig,
) -> bool:
    slots_remaining = constraints.spell_count - counts.total
    if slots_remaining == 0:
        return _counts_satisfy_constraints(counts=counts, constraints=constraints)

    if len(remaining) < slots_remaining:
        return False

    states = {(0, 0, 0, 0, 0)}
    creature_room = constraints.creature_ceiling - counts.creatures
    expensive_room = constraints.maximum_expensive_spells - counts.expensive
    splash_room = constraints.maximum_splash_spells - counts.splashes
    for card in remaining:
        creature = 1 if _is_creature_card(card=card.card) else 0
        two_drop = 1 if _is_two_drop(card=card, config=config) else 0
        expensive = 1 if _is_expensive_spell(card=card, config=config) else 0
        splash = 1 if _is_splash_card(card=card.card, pair=pair) else 0
        next_states = set(states)
        for selected_count, creatures, two_drops, expensive_spells, splashes in states:
            if selected_count >= slots_remaining:
                continue

            next_creatures = creatures + creature
            next_expensive = expensive_spells + expensive
            next_splashes = splashes + splash
            if next_creatures > creature_room or next_expensive > expensive_room:
                continue

            if next_splashes > splash_room:
                continue

            next_states.add(
                (
                    selected_count + 1,
                    next_creatures,
                    min(constraints.minimum_two_drops, two_drops + two_drop),
                    next_expensive,
                    next_splashes,
                )
            )

        states = next_states

    for selected_count, creatures, two_drops, expensive_spells, splashes in states:
        if selected_count != slots_remaining:
            continue

        final_counts = SpellCounts(
            total=constraints.spell_count,
            creatures=counts.creatures + creatures,
            two_drops=counts.two_drops + two_drops,
            expensive=counts.expensive + expensive_spells,
            splashes=counts.splashes + splashes,
        )
        if _counts_satisfy_constraints(counts=final_counts, constraints=constraints):
            return True

    return False


def _candidate_selection_sort_key(
    *,
    card: ScoredCard,
    floor_unmet: bool,
    config: DeckBuilderConfig,
) -> tuple[float, int, float, float, int]:
    effective_score = card.raw_score
    creature_preference = 0
    if floor_unmet:
        creature = _is_creature_card(card=card.card)
        if creature:
            effective_score += config.near_tie_creature_preference_points
        creature_preference = 0 if creature else 1

    return (
        -effective_score,
        creature_preference,
        -card.raw_score,
        -card.base_rating,
        card.original_index,
    )


def _pool_card_quantities(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
) -> Counter[CardQuantityKey]:
    return Counter(
        _card_quantity_key(card=card_database.lookup(grp_id=grp_id))
        for grp_id in pool_grp_ids
    )


def _limit_cards_to_pool_quantities(
    *,
    cards: tuple[ScoredCard, ...],
    available_quantities: Counter[CardQuantityKey],
) -> tuple[ScoredCard, ...]:
    used_quantities: Counter[CardQuantityKey] = Counter()
    limited_cards: list[ScoredCard] = []
    for card in cards:
        quantity_key = _card_quantity_key(card=card.card)
        if used_quantities[quantity_key] >= available_quantities[quantity_key]:
            continue

        used_quantities[quantity_key] += 1
        limited_cards.append(card)

    return tuple(limited_cards)


def _exceeds_available_card_quantities(
    *,
    cards: tuple[ScoredCard, ...],
    available_quantities: Counter[CardQuantityKey],
) -> bool:
    selected_quantities: Counter[CardQuantityKey] = Counter(
        _card_quantity_key(card=card.card) for card in cards
    )
    return any(
        count > available_quantities[quantity_key]
        for quantity_key, count in selected_quantities.items()
    )


def _card_quantity_key(*, card: CardInfo) -> CardQuantityKey:
    if card.unknown:
        return ("unknown", str(card.grp_id))

    return ("name", " ".join(card.name.casefold().split()))


def _bench_cards(
    *,
    candidates: tuple[ScoredCard, ...],
    selected: tuple[ScoredCard, ...],
    config: DeckBuilderConfig,
) -> tuple[ScoredCard, ...]:
    if config.bench_card_count <= 0:
        return ()

    selected_indices = {card.original_index for card in selected}
    unselected = tuple(
        card for card in candidates if card.original_index not in selected_indices
    )
    return tuple(sorted(unselected, key=_bench_sort_key)[: config.bench_card_count])



def _bench_sort_key(card: ScoredCard) -> tuple[int, float, float, int]:
    return (-card.score, -card.raw_score, -card.base_rating, card.original_index)



def _spell_counts(
    *,
    cards: tuple[ScoredCard, ...],
    config: DeckBuilderConfig,
    pair: str | None = None,
) -> SpellCounts:
    return SpellCounts(
        total=len(cards),
        creatures=sum(1 for card in cards if _is_creature_card(card=card.card)),
        two_drops=sum(1 for card in cards if _is_two_drop(card=card, config=config)),
        expensive=sum(
            1 for card in cards if _is_expensive_spell(card=card, config=config)
        ),
        splashes=(
            0
            if pair is None
            else sum(1 for card in cards if _is_splash_card(card=card.card, pair=pair))
        ),
        instants=sum(1 for card in cards if _is_instant_card(card=card.card)),
    )



def _counts_satisfy_constraints(
    *,
    counts: SpellCounts,
    constraints: SpellConstraints,
) -> bool:
    return (
        counts.total == constraints.spell_count
        and counts.creatures >= constraints.creature_floor
        and counts.creatures <= constraints.creature_ceiling
        and counts.two_drops >= constraints.minimum_two_drops
        and counts.expensive <= constraints.maximum_expensive_spells
        and counts.splashes <= constraints.maximum_splash_spells
    )



def _applied_relaxations(
    *,
    plan: _ConstraintPlan,
    constraints: SpellConstraints,
    config: DeckBuilderConfig,
) -> tuple[str, ...]:
    relaxations: list[str] = []
    default_expensive_cap = min(config.maximum_expensive_spells, constraints.spell_count)
    if (
        not plan.enforce_expensive_cap
        or constraints.maximum_expensive_spells > default_expensive_cap
    ):
        relaxations.append(config.relaxation_order[0])

    default_two_drops = min(config.minimum_two_drops, constraints.spell_count)
    if (
        not plan.enforce_two_drop_minimum
        or constraints.minimum_two_drops < default_two_drops
    ):
        relaxations.append(config.relaxation_order[1])

    default_creature_ceiling = min(config.creature_ceiling, constraints.spell_count)
    if (
        not plan.enforce_creature_ceiling
        or constraints.creature_ceiling > default_creature_ceiling
    ):
        relaxations.append(config.relaxation_order[2])

    default_creature_floor = min(config.creature_floor, constraints.spell_count)
    if (
        not plan.enforce_creature_floor
        or constraints.creature_floor < default_creature_floor
    ):
        relaxations.append(config.relaxation_order[3])

    if constraints.spell_count < config.target_spell_count:
        relaxations.append(config.relaxation_order[4])

    return tuple(dict.fromkeys(relaxations))


def _curve_land_count(
    *,
    selection: SpellSelection,
    config: DeckBuilderConfig,
) -> tuple[int, str]:
    average_mana_value = _average_mana_value(cards=selection.spells)
    if (
        average_mana_value >= config.top_heavy_average_mana_value_min
        or selection.counts.expensive > config.maximum_expensive_spells
    ):
        return (
            config.top_heavy_land_count,
            f"top-heavy curve: avg MV {average_mana_value:.2f}",
        )

    if (
        average_mana_value <= config.aggressive_average_mana_value_max
        and selection.counts.two_drops >= config.minimum_two_drops
    ):
        return (
            config.aggressive_land_count,
            "aggressive curve: "
            f"avg MV {average_mana_value:.2f}, "
            f"{selection.counts.two_drops} two-drops",
        )

    return (
        config.default_land_count,
        f"default curve: avg MV {average_mana_value:.2f}",
    )


def _selected_nonbasic_lands(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    pair: str,
    land_count: int,
    splash_colors: tuple[str, ...],
) -> tuple[LandCard, ...]:
    lands: list[LandCard] = []
    for index, grp_id in enumerate(pool_grp_ids):
        card = card_database.lookup(grp_id=grp_id)
        if not _is_selected_nonbasic_land(
            card=card,
            pair=pair,
            splash_colors=splash_colors,
        ):
            continue

        lands.append(
            LandCard(
                card=card,
                original_index=index,
                source_colors=_land_source_colors(card=card),
            )
        )

    return tuple(sorted(lands, key=lambda land: _nonbasic_land_sort_key(
        land=land,
        pair=pair,
        splash_colors=splash_colors,
    ))[:land_count])


def _is_selected_nonbasic_land(
    *,
    card: CardInfo,
    pair: str,
    splash_colors: tuple[str, ...],
) -> bool:
    if not _is_land_card(card=card) or _is_basic_land_card(card=card):
        return False

    source_colors = _land_source_colors(card=card)
    if not source_colors:
        return False

    if all(color in pair for color in source_colors):
        return True

    return bool(splash_colors) and any(color in splash_colors for color in source_colors)


def _nonbasic_land_sort_key(
    *,
    land: LandCard,
    pair: str,
    splash_colors: tuple[str, ...],
) -> tuple[int, int]:
    fixes_splash = any(color in splash_colors for color in land.source_colors)
    supports_pair = any(color in pair for color in land.source_colors)
    if fixes_splash and supports_pair:
        priority = 0
    elif supports_pair:
        priority = 1
    else:
        priority = 2

    return (priority, land.original_index)


def _is_in_pair_nonbasic_land(*, card: CardInfo, pair: str) -> bool:
    return _is_selected_nonbasic_land(card=card, pair=pair, splash_colors=())


def _is_land_card(*, card: CardInfo) -> bool:
    return any("Land" in type_line for type_line in card.types)


def _is_basic_land_card(*, card: CardInfo) -> bool:
    if card.name in set(BASIC_LANDS_BY_COLOR.values()):
        return True

    return any(
        "Basic" in type_line and "Land" in type_line
        for type_line in card.types
    )


def _land_source_colors(*, card: CardInfo) -> tuple[str, ...]:
    source_colors = card.produced_mana or card.colors
    source_set = set(source_colors)
    return tuple(color for color in BASIC_LANDS_BY_COLOR if color in source_set)


def _splash_basic_land_counts(
    *,
    splash_colors: tuple[str, ...],
    selected_spells: tuple[ScoredCard, ...],
    pair: str,
    nonbasic_lands: tuple[LandCard, ...],
) -> tuple[BasicLandCount, ...]:
    if not splash_colors or SPLASH.planned_basic_sources <= 0:
        return ()

    splash_color = splash_colors[0]
    splash_card_count = sum(
        _is_splash_card(card=scored_card.card, pair=pair)
        for scored_card in selected_spells
    )
    required_sources = (
        SPLASH.single_card_sources
        if splash_card_count <= 1
        else SPLASH.multiple_card_sources
    )
    drafted_sources = sum(
        splash_color in land.source_colors for land in nonbasic_lands
    )
    basic_count = min(
        SPLASH.planned_basic_sources,
        max(0, required_sources - drafted_sources),
    )
    if basic_count <= 0:
        return ()

    return (
        BasicLandCount(
            color=splash_color,
            name=BASIC_LANDS_BY_COLOR[splash_color],
            count=basic_count,
        ),
    )


def _spell_pip_counts(
    *,
    cards: tuple[ScoredCard, ...],
    pair: str,
) -> tuple[dict[str, int], dict[str, int]]:
    pip_counts = {color: 0 for color in pair}
    double_pip_counts = {color: 0 for color in pair}
    for scored_card in cards:
        card_counts = _card_pip_counts(card=scored_card.card, pair=pair)
        for color in pair:
            pips = card_counts[color]
            pip_counts[color] += pips
            double_pip_counts[color] += max(0, pips - 1)

    return pip_counts, double_pip_counts


def _card_pip_counts(*, card: CardInfo, pair: str) -> dict[str, int]:
    counts = {color: 0 for color in pair}
    if card.mana_cost:
        for symbol in MANA_SYMBOL_PATTERN.findall(card.mana_cost):
            for color in pair:
                if color in symbol:
                    counts[color] += 1

        return counts

    for color in card.colors:
        if color in counts:
            counts[color] += 1

    return counts


def _source_counts(
    *,
    pair: str,
    nonbasic_lands: tuple[LandCard, ...],
) -> dict[str, int]:
    counts = {color: 0 for color in pair}
    for land in nonbasic_lands:
        for color in land.source_colors:
            if color in counts:
                counts[color] += 1

    return counts


def _source_counts_with_basics(
    *,
    pair: str,
    nonbasic_lands: tuple[LandCard, ...],
    basic_lands: tuple[BasicLandCount, ...],
) -> dict[str, int]:
    counts = _source_counts(pair=pair, nonbasic_lands=nonbasic_lands)
    for basic in basic_lands:
        counts[basic.color] += basic.count

    return counts


def _basic_land_counts(
    *,
    pair: str,
    slots: int,
    pip_counts: dict[str, int],
    double_pip_counts: dict[str, int],
    source_counts: dict[str, int],
    config: DeckBuilderConfig,
) -> tuple[BasicLandCount, ...]:
    if slots <= 0:
        return ()

    colors = tuple(pair)
    desired_counts = _desired_basic_counts(
        colors=colors,
        slots=slots,
        pip_counts=pip_counts,
    )
    double_heavy_color = max(
        colors,
        key=lambda color: (double_pip_counts[color], pip_counts[color], -colors.index(color)),
    )
    pip_heavy_color = max(
        colors,
        key=lambda color: (pip_counts[color], -colors.index(color)),
    )
    best_counts = min(
        _two_color_basic_splits(colors=colors, slots=slots),
        key=lambda counts: _basic_split_sort_key(
            counts=counts,
            colors=colors,
            desired_counts=desired_counts,
            source_counts=source_counts,
            source_floor=config.main_color_source_floor,
            double_heavy_color=double_heavy_color,
            pip_heavy_color=pip_heavy_color,
        ),
    )
    return tuple(
        BasicLandCount(
            color=color,
            name=BASIC_LANDS_BY_COLOR[color],
            count=best_counts[color],
        )
        for color in colors
        if best_counts[color] > 0
    )


def _desired_basic_counts(
    *,
    colors: tuple[str, ...],
    slots: int,
    pip_counts: dict[str, int],
) -> dict[str, float]:
    total_pips = sum(pip_counts[color] for color in colors)
    if total_pips <= 0:
        return {color: slots / len(colors) for color in colors}

    return {
        color: slots * (pip_counts[color] / total_pips)
        for color in colors
    }


def _two_color_basic_splits(
    *,
    colors: tuple[str, ...],
    slots: int,
) -> tuple[dict[str, int], ...]:
    first, second = colors
    return tuple(
        {first: first_count, second: slots - first_count}
        for first_count in range(slots + 1)
    )


def _basic_split_sort_key(
    *,
    counts: dict[str, int],
    colors: tuple[str, ...],
    desired_counts: dict[str, float],
    source_counts: dict[str, int],
    source_floor: int,
    double_heavy_color: str,
    pip_heavy_color: str,
) -> tuple[float, float, int, int, int]:
    shortage = sum(
        max(0, source_floor - (source_counts[color] + counts[color]))
        for color in colors
    )
    proportion_error = sum(
        (counts[color] - desired_counts[color]) ** 2
        for color in colors
    )
    return (
        shortage,
        proportion_error,
        -counts[double_heavy_color],
        -counts[pip_heavy_color],
        counts[colors[0]],
    )


def _ordered_color_items(*, values: dict[str, int], pair: str) -> tuple[tuple[str, int], ...]:
    return tuple((color, values[color]) for color in pair)


def _average_mana_value(*, cards: tuple[ScoredCard, ...]) -> float:
    if not cards:
        return 0.0

    return sum(card.card.mana_value or 0.0 for card in cards) / len(cards)


def _mana_base_caveats(
    *,
    land_count: int,
    nonbasic_lands: tuple[LandCard, ...],
    config: DeckBuilderConfig,
) -> tuple[str, ...]:
    if land_count == config.aggressive_land_count and nonbasic_lands:
        return (
            "16-land caveat: prefer basics over slow taplands when curve pressure matters.",
        )

    return ()



def _format_player_build_sheet(
    *,
    spell_selection: SpellSelection,
    mana_base: ManaBase,
    config: DeckBuilderConfig,
) -> list[str]:
    counts = spell_selection.counts
    lines = [
        "",
        "Deck summary:",
        "Deck size: "
        f"{mana_base.total_cards} cards "
        f"({counts.total} spells + {mana_base.land_count} lands)",
        f"Average mana value: {mana_base.average_mana_value:.2f}",
        _format_mana_curve_summary(cards=spell_selection.spells),
        (
            f"Creatures: {counts.creatures}; "
            f"Non-creatures: {counts.noncreatures}; Lands: {mana_base.land_count}"
        ),
        f"Land count: {mana_base.land_count} ({mana_base.reason})",
        "Mana pips: " f"{_format_color_counts(mana_base.pip_counts)}",
        "Sources: "
        f"{_format_color_counts(mana_base.source_counts)} "
        f"(floor {config.main_color_source_floor})",
    ]
    similarity_line = _format_similarity_line(
        spell_selection=spell_selection,
        mana_base=mana_base,
    )
    if similarity_line is not None:
        lines.append(similarity_line)

    lines.extend(_format_selected_spell_cards(selection=spell_selection))
    lines.extend(_format_land_section(mana_base=mana_base))
    lines.extend(
        _format_structure_checks(
            selection=spell_selection,
            config=config,
        )
    )
    lines.extend(_format_bench_section(selection=spell_selection, config=config))
    return lines


def _format_mana_curve_summary(*, cards: tuple[ScoredCard, ...]) -> str:
    counts = [0 for _ in CURVE_BUCKET_LABELS]
    unknown_count = 0
    for card in cards:
        mana_value = card.card.mana_value
        if mana_value is None:
            unknown_count += 1
            continue

        counts[_curve_bucket(mana_value=mana_value)] += 1

    parts = [
        f"{label}: {counts[index]}"
        for index, label in enumerate(CURVE_BUCKET_LABELS)
    ]
    if unknown_count > 0:
        parts.append(f"?: {unknown_count}")

    return "Mana curve: " + " | ".join(parts)


def _curve_bucket(*, mana_value: float) -> int:
    rounded = max(0, int(mana_value))
    return min(rounded, len(CURVE_BUCKET_LABELS) - 1)


def _format_similarity_line(
    *,
    spell_selection: SpellSelection,
    mana_base: ManaBase,
) -> str | None:
    targets = spell_selection.structure_targets
    if targets is None:
        return None

    return (
        "Similarity: "
        f"17Lands trophy {targets.pair} decks in {targets.set_code} "
        f"(n={targets.sample_size}): avg "
        f"{targets.average_creature_count:.1f} creatures / "
        f"{targets.average_land_count:.1f} lands; your build: "
        f"{spell_selection.counts.creatures} / {mana_base.land_count}."
    )



def _format_land_section(*, mana_base: ManaBase) -> list[str]:
    lines = ["Lands:", "Nonbasic lands:"]
    if mana_base.nonbasic_lands:
        lines.extend(_format_nonbasic_land(land=land) for land in mana_base.nonbasic_lands)
    else:
        lines.append("- none")

    lines.append("Basics:")
    if mana_base.basic_lands:
        lines.extend(_format_basic_land(basic=basic) for basic in mana_base.basic_lands)
    else:
        lines.append("- none")

    if mana_base.caveats:
        lines.append("Mana notes:")
        lines.extend(f"- {caveat}" for caveat in mana_base.caveats)

    return lines


def _format_nonbasic_land(*, land: LandCard) -> str:
    return (
        f"- 1 {land.card.name} "
        f"({_format_colors(land.source_colors)} source; grpId {land.card.grp_id})"
    )


def _format_basic_land(*, basic: BasicLandCount) -> str:
    return f"- {basic.count} {basic.name}"


def _format_color_counts(counts: tuple[tuple[str, int], ...]) -> str:
    if not counts:
        return "none"

    return ", ".join(f"{color} {count}" for color, count in counts)


def _format_selected_spell_cards(*, selection: SpellSelection) -> list[str]:
    lines = ["", "Selected spells:", "Creatures:"]
    lines.extend(
        _format_spell_card(card=card)
        for card in _sorted_spell_cards(cards=selection.spells, creatures=True)
    )
    lines.append("Non-creatures:")
    lines.extend(
        _format_spell_card(card=card)
        for card in _sorted_spell_cards(cards=selection.spells, creatures=False)
    )
    return lines


def _format_structure_checks(
    *,
    selection: SpellSelection,
    config: DeckBuilderConfig,
) -> list[str]:
    counts = selection.counts
    constraints = selection.constraints
    return [
        "",
        "Structure checks:",
        f"Eligible spells for {selection.pair}: {selection.eligible_count}",
        f"Selected spells: {counts.total}/{selection.requested_spell_count}",
        f"Creature count: {counts.creatures} "
        f"(target {constraints.creature_floor}-{constraints.creature_ceiling})",
        f"Two-drops MV {config.two_drop_mana_value:g}: {counts.two_drops} "
        f"(minimum {constraints.minimum_two_drops})",
        f"Expensive spells MV >= {config.expensive_spell_mana_value:g}: "
        f"{counts.expensive} (soft cap {constraints.maximum_expensive_spells})",
        _format_splash_note(selection=selection),
        f"Relaxation order: {' -> '.join(config.relaxation_order)}",
        f"Applied relaxations: {_format_relaxations(selection.applied_relaxations)}",
    ]


def _format_spell_selection(
    *,
    selection: SpellSelection,
    config: DeckBuilderConfig,
    include_bench: bool = True,
) -> list[str]:
    counts = selection.counts
    constraints = selection.constraints
    lines = [
        "",
        "Spell selection:",
        f"Eligible spells for {selection.pair}: {selection.eligible_count}",
        f"Selected spells: {counts.total}/{selection.requested_spell_count}",
        f"Creature count: {counts.creatures} "
        f"(target {constraints.creature_floor}-{constraints.creature_ceiling})",
        f"Two-drops MV {config.two_drop_mana_value:g}: {counts.two_drops} "
        f"(minimum {constraints.minimum_two_drops})",
        f"Expensive spells MV >= {config.expensive_spell_mana_value:g}: "
        f"{counts.expensive} (soft cap {constraints.maximum_expensive_spells})",
        _format_splash_note(selection=selection),
        f"Relaxation order: {' -> '.join(config.relaxation_order)}",
        f"Applied relaxations: {_format_relaxations(selection.applied_relaxations)}",
        "Creatures:",
    ]
    lines.extend(
        _format_spell_card(card=card)
        for card in _sorted_spell_cards(cards=selection.spells, creatures=True)
    )
    lines.append("Non-creatures:")
    lines.extend(
        _format_spell_card(card=card)
        for card in _sorted_spell_cards(cards=selection.spells, creatures=False)
    )
    if include_bench:
        lines.extend(_format_bench_section(selection=selection, config=config))

    return lines


def _format_bench_section(
    *,
    selection: SpellSelection,
    config: DeckBuilderConfig,
) -> list[str]:
    lines = ["Bench:"]
    if selection.bench:
        lines.extend(
            _format_bench_card(card=card, selection=selection, config=config)
            for card in selection.bench
        )
    else:
        lines.append("- none")

    return lines



def _format_splash_note(*, selection: SpellSelection) -> str:
    if not selection.allow_splash_requested:
        return "Splash: disabled (--no-splash; off-pair cards excluded)"

    if selection.splash_color is None:
        return (
            "Splash: enabled; no eligible A- or better single-pip "
            "third-color card in the pool"
        )

    available_sources = (
        selection.splash_fixing_sources + SPLASH.planned_basic_sources
    )
    required_sources = (
        SPLASH.single_card_sources
        if selection.constraints.maximum_splash_spells <= 1
        else SPLASH.multiple_card_sources
    )
    if selection.constraints.maximum_splash_spells <= 0:
        return (
            f"Splash: {selection.splash_color} unsupported "
            f"({selection.splash_fixing_sources} drafted fixing + "
            f"{SPLASH.planned_basic_sources} planned basic = "
            f"{available_sources}/{SPLASH.single_card_sources} sources; "
            "off-pair cards excluded)"
        )

    return (
        f"Splash: {selection.splash_color} enabled "
        f"({selection.splash_fixing_sources} drafted fixing + "
        f"{selection.splash_planned_basic_sources} planned basic = "
        f"{available_sources}/{required_sources} sources; "
        f"selected {selection.counts.splashes}/"
        f"{selection.constraints.maximum_splash_spells} A- or better single-pip cards)"
    )



def _format_relaxations(relaxations: tuple[str, ...]) -> str:
    if not relaxations:
        return "none"

    return ", ".join(relaxations)



def _sorted_spell_cards(
    *,
    cards: tuple[ScoredCard, ...],
    creatures: bool,
) -> tuple[ScoredCard, ...]:
    matching = tuple(
        card for card in cards if _is_creature_card(card=card.card) == creatures
    )
    return tuple(sorted(matching, key=_curve_sort_key))



def _curve_sort_key(card: ScoredCard) -> tuple[float, str, int]:
    mana_value = 99.0 if card.card.mana_value is None else card.card.mana_value
    return (mana_value, card.card.name, card.original_index)



def _format_spell_card(*, card: ScoredCard) -> str:
    return (
        f"- MV {_format_mana_value(card.card.mana_value)} | "
        f"score {card.score} | {card.card.name} ({_format_colors(card.card.colors)})"
    )



def _format_bench_card(
    *,
    card: ScoredCard,
    selection: SpellSelection,
    config: DeckBuilderConfig,
) -> str:
    reason = _bench_reason(card=card, selection=selection, config=config)
    return f"{_format_spell_card(card=card)} ({reason})"



def _bench_reason(
    *,
    card: ScoredCard,
    selection: SpellSelection,
    config: DeckBuilderConfig,
) -> str:
    if (
        _is_expensive_spell(card=card, config=config)
        and selection.counts.expensive >= selection.constraints.maximum_expensive_spells
    ):
        return "cut: expensive-spell cap"

    if (
        _is_creature_card(card=card.card)
        and selection.counts.creatures >= selection.constraints.creature_ceiling
    ):
        return "cut: creature ceiling"

    if (
        _is_splash_card(card=card.card, pair=selection.pair)
        and selection.counts.splashes >= selection.constraints.maximum_splash_spells
    ):
        return "cut: splash cap"

    return "cut: lower score"



def _format_mana_value(mana_value: float | None) -> str:
    if mana_value is None:
        return "?"

    return f"{mana_value:g}"



def _format_colors(colors: tuple[str, ...]) -> str:
    if not colors:
        return "C"

    return "".join(colors)



def _pair_win_rate(*, pair: str, ratings_data: SeventeenLandsData | None) -> float | None:
    if ratings_data is None:
        return None

    pair_record = ratings_data.pair_win_rates.get(pair)
    if pair_record is None:
        return None

    return pair_record.win_rate



def _pair_win_rate_score(
    *,
    pair_win_rate: float | None,
    config: DeckBuilderConfig,
) -> float:
    win_rate = config.neutral_pair_win_rate if pair_win_rate is None else pair_win_rate
    return _clamp(value=win_rate * 100.0, lower=0.0, upper=100.0)



def _blended_score(
    *,
    average_playable_score: float,
    pair_win_rate_score: float,
    config: DeckBuilderConfig,
) -> float:
    weight_total = config.pair_score_card_weight + config.pair_score_win_rate_weight
    return (
        (average_playable_score * config.pair_score_card_weight)
        + (pair_win_rate_score * config.pair_score_win_rate_weight)
    ) / weight_total



def _validate_deck_builder_config(*, config: DeckBuilderConfig) -> None:
    if config.deck_size <= 0:
        raise DeckBuilderError("Deck-builder deck size must be greater than zero.")

    if config.target_spell_count <= 0:
        raise DeckBuilderError("Deck-builder target spell count must be greater than zero.")

    if min(
        config.default_land_count,
        config.aggressive_land_count,
        config.top_heavy_land_count,
    ) < 0:
        raise DeckBuilderError("Deck-builder land counts must be non-negative.")

    if config.land_count_iteration_limit <= 0:
        raise DeckBuilderError("Deck-builder land-count iterations must be positive.")

    if not 0.0 <= config.maximum_unresolved_metadata_ratio <= 1.0:
        raise DeckBuilderError(
            "Deck-builder unresolved metadata ratio must be between zero and one."
        )

    if config.main_color_source_floor < 0:
        raise DeckBuilderError("Deck-builder source floor must be non-negative.")

    if config.creature_floor < 0 or config.creature_ceiling < 0:
        raise DeckBuilderError("Deck-builder creature constraints must be non-negative.")

    if config.creature_floor > config.creature_ceiling:
        raise DeckBuilderError("Deck-builder creature floor cannot exceed its ceiling.")

    if config.minimum_two_drops < 0 or config.maximum_expensive_spells < 0:
        raise DeckBuilderError("Deck-builder curve constraints must be non-negative.")

    if config.near_tie_creature_preference_points < 0:
        raise DeckBuilderError("Deck-builder near-tie preference must be non-negative.")

    if config.splash_max_cards < 0:
        raise DeckBuilderError("Deck-builder splash maximum must be non-negative.")

    if config.splash_elite_score_minimum < 0:
        raise DeckBuilderError("Deck-builder splash score threshold must be non-negative.")

    if config.structure_maindeck_rate_threshold < 0:
        raise DeckBuilderError("Deck-builder structure threshold must be non-negative.")

    if config.structure_min_land_count > config.structure_max_land_count:
        raise DeckBuilderError("Deck-builder structure land range is invalid.")

    if len(config.relaxation_order) < 5:
        raise DeckBuilderError("Deck-builder relaxation order must describe all stages.")

    if config.optimizer_beam_width <= 0:
        raise DeckBuilderError("Deck-builder optimizer beam width must be positive.")
    if config.optimizer_local_improvement_rounds < 0:
        raise DeckBuilderError("Deck-builder local-improvement rounds must be non-negative.")
    if config.optimizer_local_improvement_candidates < 0:
        raise DeckBuilderError("Deck-builder local-improvement candidates must be non-negative.")
    if config.optimizer_max_evaluations < 0:
        raise DeckBuilderError("Deck-builder optimizer evaluations must be non-negative.")
    if config.optimizer_max_search_nodes <= 0:
        raise DeckBuilderError("Deck-builder optimizer search nodes must be positive.")
    _validate_optimizer_weights(config=config)


def _validate_optimizer_weights(*, config: DeckBuilderConfig) -> None:
    """Validate finite non-negative optimizer weights.
    Generic quality, curve, or creature structure must always participate.
    """

    generic_weights = (
        config.optimizer_quality_weight,
        config.optimizer_curve_weight,
        config.optimizer_creature_structure_weight,
    )
    profile_weights = (
        config.optimizer_role_target_weight,
        config.optimizer_effective_removal_weight,
        config.optimizer_enabler_payoff_weight,
        config.optimizer_semantic_synergy_weight,
        config.optimizer_empirical_context_weight,
        config.optimizer_redundancy_weight,
        config.optimizer_unsupported_payoff_weight,
        config.optimizer_mana_strain_weight,
    )
    weights = (*generic_weights, *profile_weights)
    if any(
        isinstance(weight, bool)
        or not isinstance(weight, (int, float))
        or not math.isfinite(weight)
        or weight < 0
        for weight in weights
    ):
        raise DeckBuilderError("Deck-builder optimizer weights must be finite and non-negative.")
    if sum(generic_weights) <= 0:
        raise DeckBuilderError(
            "At least one generic quality, curve, or creature-structure optimizer weight "
            "must be positive."
        )



def _validate_blending_weights(*, config: DeckBuilderConfig) -> None:
    if config.pair_score_card_weight < 0 or config.pair_score_win_rate_weight < 0:
        raise DeckBuilderError("Deck-builder blending weights must be non-negative.")

    if config.pair_score_card_weight + config.pair_score_win_rate_weight <= 0:
        raise DeckBuilderError("At least one deck-builder blending weight must be positive.")



def _optional_pair(value: str | None) -> str | None:
    if value is None:
        return None

    if value not in COLOR_PAIRS:
        raise DeckBuilderError(
            f"Invalid color pair {value!r}; expected one of {', '.join(COLOR_PAIRS)}."
        )

    return value



def _score_for_pair(*, scores: tuple[PairScore, ...], pair: str | None) -> PairScore:
    if pair is None:
        raise DeckBuilderError("Forced pair is missing.")

    for score in scores:
        if score.pair == pair:
            return score

    raise DeckBuilderError(f"No score was computed for pair {pair}.")



def _pair_score_sort_key(score: PairScore) -> tuple[float, float, float, int]:
    return (
        -score.blended_score,
        -score.playable_score_sum,
        -score.pair_win_rate_score,
        COLOR_PAIRS.index(score.pair),
    )



def _format_pair_score(
    *,
    score: PairScore,
    target_spell_count: int,
    config: DeckBuilderConfig,
) -> str:
    win_rate_label = _format_win_rate(score=score)
    return (
        f"- {score.pair}: strength {_format_score(score.blended_score, config=config)}; "
        f"top {target_spell_count} playable quality sum "
        f"{_format_score(score.playable_score_sum, config=config)}; "
        f"17Lands WR {win_rate_label}; playables {score.playable_count}"
    )



def _format_win_rate(*, score: PairScore) -> str:
    if score.pair_win_rate is None:
        return f"neutral {score.pair_win_rate_score:.1f}%"
    return f"{score.pair_win_rate * 100.0:.1f}%"



def _format_score(value: float, *, config: DeckBuilderConfig) -> str:
    places = max(0, config.pair_score_decimal_places)
    return f"{value:.{places}f}"



def _int_tuple(*, value: Any, field_name: str) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise DeckBuilderError(f"Missing or invalid {field_name}; expected integer list.")

    result: list[int] = []
    for item in value:
        if isinstance(item, bool):
            raise DeckBuilderError(f"Missing or invalid {field_name}; expected integers.")

        try:
            result.append(int(item))
        except (TypeError, ValueError) as error:
            raise DeckBuilderError(
                f"Missing or invalid {field_name}; expected integers."
            ) from error

    return tuple(result)



def _optional_string(value: Any) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise DeckBuilderError("Pool metadata values must be strings.")

    return value



def _clamp(*, value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)
