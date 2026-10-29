"""Documented tunables for Draftomen.
Centralize defaults that later scoring and deck-building modules will share.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SplashConfig:
    """Conservative Limited splash defaults shared by picks and builds.
    Keep thresholds explicit so replay data can calibrate them safely.
    """

    enabled_by_default: bool = True
    maximum_colors: int = 1
    maximum_cards: int = 3
    maximum_off_color_pips: int = 2
    # Sources by splash symbol count, from scripts/measure_deck_rules.py on the 17Lands FRA
    # PremierDraft dump, retrieved 2026-10-29. Nearly all winning splash decks played 1 to 3
    # splash cards. 70% of decks splashing a two-symbol card had 4 or more splash sources, and
    # 14 to 22% had no dual for the splash. Basics fill the gap, so the deck always has them.
    single_symbol_sources: int = 3
    double_symbol_sources: int = 4
    supported_minimum_grade: str = "A-"
    speculative_minimum_grade: str = "A"
    minimum_score_advantage: float = 5.0
    ready_score_multiplier: float = 0.95
    speculative_score_multiplier: float = 0.85
    fixer_score_multiplier: float = 1.05
    aggressive_minimum_spells: int = 8
    aggressive_average_mana_value_max: float = 2.7
    aggressive_minimum_two_drop_ratio: float = 0.3


@dataclass(frozen=True)
class DeckBuilderConfig:
    """Deck-builder structural and optimizer defaults.
    Beam and local-improvement limits keep normal pools bounded.
    """

    deck_size: int = 40
    target_spell_count: int = 23
    pair_score_card_weight: float = 0.85
    pair_score_win_rate_weight: float = 0.15
    neutral_pair_win_rate: float = 0.5
    pair_score_decimal_places: int = 2
    default_land_count: int = 17
    aggressive_land_count: int = 16
    top_heavy_land_count: int = 18
    aggressive_average_mana_value_max: float = 2.7
    top_heavy_average_mana_value_min: float = 3.4
    creature_floor: int = 15
    creature_ceiling: int = 17
    minimum_two_drops: int = 5
    maximum_expensive_spells: int = 3
    two_drop_mana_value: float = 2.0
    expensive_spell_mana_value: float = 6.0
    near_tie_creature_preference_points: float = 2.0
    splash_max_cards: int = 3
    splash_elite_score_minimum: float = 70.0
    # Win rate gap by fixing piece count, from scripts/measure_deck_rules.py on the 17Lands
    # FRA PremierDraft dump, retrieved 2026-10-29. The last value covers that count and more.
    triad_fixing_win_rate_gaps: tuple[float, ...] = (
        -0.185,
        -0.075,
        -0.049,
        -0.023,
        -0.016,
        -0.003,
        0.020,
    )
    bench_card_count: int = 5
    land_count_iteration_limit: int = 4
    optimizer_beam_width: int = 24
    optimizer_local_improvement_rounds: int = 2
    optimizer_local_improvement_candidates: int = 8
    optimizer_max_evaluations: int = 4096
    optimizer_max_search_nodes: int = 32768
    optimizer_quality_weight: float = 1.0
    optimizer_curve_weight: float = 0.12
    optimizer_creature_structure_weight: float = 0.12
    optimizer_role_target_weight: float = 0.12
    optimizer_effective_removal_weight: float = 0.12
    optimizer_enabler_payoff_weight: float = 0.22
    optimizer_semantic_synergy_weight: float = 0.20
    optimizer_empirical_context_weight: float = 0.10
    optimizer_redundancy_weight: float = 0.12
    optimizer_unsupported_payoff_weight: float = 0.18
    optimizer_mana_strain_weight: float = 0.10
    maximum_unresolved_metadata_ratio: float = 0.25
    relaxation_order: tuple[str, ...] = (
        "expensive-spell cap",
        "minimum two-drop quota",
        "creature ceiling",
        "creature floor",
        "eligible-card shortage",
    )
    main_color_source_floor: int = 7
    # The lightest color of a winning three-color deck had 4 or more land sources in about
    # 90% of decks, from scripts/measure_deck_rules.py on the 17Lands FRA PremierDraft dump.
    triad_min_color_land_sources: int = 4
    # In winning three-color decks, about 85% of cards with two symbols of one color had 6 or
    # more land sources of that color, from scripts/measure_deck_rules.py on the 17Lands FRA
    # PremierDraft dump.
    double_symbol_min_color_land_sources: int = 6
    structure_maindeck_rate_threshold: float = 0.5
    structure_min_land_count: int = 14
    structure_max_land_count: int = 20


@dataclass(frozen=True)
class PickEngineConfig:
    """Pick-engine scoring defaults.
    Scores stay integer-only for scan-friendly draft tables.
    """

    neutral_prior_score: float = 50.0
    neutral_prior_win_rate: float = 0.55
    # Aggregate color-pair rates use a neutral prior independent of card rates.
    neutral_pair_win_rate: float = 0.5
    thin_sample_minimum: int = 500
    alsa_adjustment_max: float = 0.03
    alsa_early_pick: float = 1.0
    alsa_late_pick: float = 8.0
    normalization_lower_percentile: float = 5.0
    normalization_upper_percentile: float = 95.0
    normalization_min_half_span: float = 0.05
    score_decimal_places: int = 0
    # Picks 1-5 stay open; pick 6 starts the linear color ramp.
    open_pick_count: int = 5
    commitment_start_pick: int = 6
    # Pick 16 and later are treated as locked to the inferred pair.
    locked_pick_index: int = 16
    # Locked on-color cards get up to +15% score; off-color cards keep 75%.
    on_color_bonus_multiplier: float = 1.15
    off_color_penalty_multiplier: float = 0.75
    # Each picked card starts at 1.0 color weight, then ratings move it up/down.
    pool_weight_baseline: float = 1.0
    pool_weight_rating_scale: float = 10.0
    pool_weight_minimum: float = 0.25
    pool_weight_maximum: float = 2.0
    # Require two materially represented colors before naming a pair.
    pool_weight_epsilon: float = 0.01
    minimum_pair_colors: int = 2
    # Three-color decks are considered only in sets where at least 10% of
    # decks play them, and only when each color is on at least this many of
    # the top 23 playable spells. Fewer cards make the color a splash.
    three_color_share_threshold: float = 0.10
    triad_min_color_cards: int = 4
    premier_fallback_enabled: bool = True
    # Open picks use pair win rates only as a close-pick tiebreaker.
    early_pair_tiebreaker_score_threshold: float = 3.0
    early_pair_tiebreaker_pair_weight_threshold: float = 0.25
    early_pair_tiebreaker_max_offered_cards: int = 16
    # After pack 1, creatures get a bonus while the pool projects below the
    # creature floor. The cap matches
    # DeckBuilderConfig.near_tie_creature_preference_points, so the term only
    # changes close calls. Being this many creatures short gives the full bonus.
    creature_pace_enabled: bool = True
    creature_pace_max_bonus: float = 2.0
    creature_pace_shortfall_scale: float = 4.0


DECK_BUILDER = DeckBuilderConfig()
PICK_ENGINE = PickEngineConfig()
SPLASH = SplashConfig()

POLL_INTERVAL_SECONDS = 1.0
RATINGS_CACHE_TTL_HOURS = 24

COLOR_PAIRS = (
    "WU",
    "WB",
    "WR",
    "WG",
    "UB",
    "UR",
    "UG",
    "BR",
    "BG",
    "RG",
)

COLOR_TRIADS = (
    "WUB",
    "WUR",
    "WUG",
    "WBR",
    "WBG",
    "WRG",
    "UBR",
    "UBG",
    "URG",
    "BRG",
)

TRIAD_NAMES = {
    "WUB": "Esper",
    "WUR": "Jeskai",
    "WUG": "Bant",
    "WBR": "Mardu",
    "WBG": "Abzan",
    "WRG": "Naya",
    "UBR": "Grixis",
    "UBG": "Sultai",
    "URG": "Temur",
    "BRG": "Jund",
}


def deck_label(*, colors: str) -> str:
    """Name a three-color deck after its letters, such as `UBG Sultai`.
    Pairs and other values come back unchanged.
    """

    name = TRIAD_NAMES.get(colors)
    if name is None:
        return colors
    return f"{colors} {name}"
