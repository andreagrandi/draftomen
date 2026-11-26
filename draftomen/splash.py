"""Shared splash eligibility and mana-support assessment.
Keep third-color decisions separate from primary-pair commitment.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import DECK_BUILDER, SPLASH, DeckBuilderConfig, SplashConfig
from draftomen.set_profile import SetProfile, empirical_card_ratings
from draftomen.seventeen import SeventeenLandsData

COLOR_ORDER = ("W", "U", "B", "R", "G")
BASIC_LANDS_BY_COLOR = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}
MANA_SYMBOL_PATTERN = re.compile(r"\{([^}]+)\}")


@dataclass(frozen=True, slots=True)
class SplashState:
    """Current one-color splash plan inferred from the drafted pool.
    Fixing counts include deterministic fixing lands usable by the base pair.
    """

    enabled: bool
    base_pair: str | None
    active_color: str | None
    picked_card_count: int
    fixing_sources: tuple[tuple[str, int], ...]
    aggressive: bool
    required_sources: int = 0
    splash_grp_ids: tuple[int, ...] = ()
    splash_basic_limits: tuple[tuple[str, int], ...] = ()
    base_source_minimums: tuple[tuple[str, int], ...] = ()

    def fixing_for(self, *, color: str) -> int:
        return dict(self.fixing_sources).get(color, 0)

    def basic_limit_for(self, *, color: str | None) -> int | None:
        if color is None:
            return None

        return dict(self.splash_basic_limits).get(color)


@dataclass(frozen=True, slots=True)
class SplashAssessment:
    """Explain whether and why one offered card participates in a splash.
    Every field is serializable so live audit records preserve the decision.
    """

    classification: str
    splash_color: str | None
    off_color_pips: int
    picked_card_count: int
    fixing_sources: int
    planned_basic_sources: int
    available_sources: int
    required_sources: int
    grade: str | None
    score_advantage: float | None
    aggressive: bool
    reasons: tuple[str, ...]

    @property
    def is_splash_candidate(self) -> bool:
        return self.classification == "splash-ready"


def infer_splash_state(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None,
    base_pair: str | None,
    enabled: bool,
    config: SplashConfig = SPLASH,
    set_profile: SetProfile | None = None,
) -> SplashState:
    """Infer the active splash color and deterministic fixing from the pool.
    A third-color card within the margin of the median maindeck spell sets the plan.
    Active-color cards that clear that floor are listed in splash_grp_ids.
    """

    fixing_sources = _fixing_counts(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        base_pair=base_pair,
    )
    if not enabled or base_pair is None or len(base_pair) > 2:
        return SplashState(
            enabled=enabled,
            base_pair=base_pair,
            active_color=None,
            picked_card_count=0,
            fixing_sources=tuple(fixing_sources.items()),
            aggressive=False,
        )

    eligible_by_color: dict[str, list[tuple[float, int]]] = {
        color: [] for color in COLOR_ORDER if color not in base_pair
    }
    required_by_color: dict[str, int] = {}
    profile_win_rates = _profile_win_rates(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        set_profile=set_profile,
    )
    win_rate_floor = _splash_win_rate_floor(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        ratings_data=ratings_data,
        base_pair=base_pair,
        profile_win_rates=profile_win_rates,
        config=config,
    )
    passed_grp_ids: dict[str, list[int]] = {color: [] for color in eligible_by_color}
    for index, grp_id in enumerate(pool_grp_ids):
        card = card_database.lookup(grp_id=grp_id)
        splash_color, off_color_pips = splash_requirement(card=card, base_pair=base_pair)
        if splash_color is None or off_color_pips > config.maximum_off_color_pips:
            continue

        win_rate = _global_win_rate(
            ratings_data=ratings_data,
            grp_id=grp_id,
            profile_win_rates=profile_win_rates,
        )
        if win_rate is None:
            continue

        if win_rate_floor is not None and win_rate < win_rate_floor:
            continue

        eligible_by_color[splash_color].append((win_rate, -index))
        passed_grp_ids[splash_color].append(grp_id)
        required_by_color[splash_color] = max(
            required_by_color.get(splash_color, 0),
            splash_required_sources(off_color_pips=off_color_pips, config=config),
        )

    active_color = _active_splash_color(
        eligible_by_color=eligible_by_color,
        fixing_sources=fixing_sources,
    )
    base_minimums: dict[str, int] = {}
    basic_limits: dict[str, int] = {}
    if len(base_pair) == 2:
        base_minimums = base_color_minimums(
            cards=_pool_base_spells(
                pool_grp_ids=pool_grp_ids,
                card_database=card_database,
                base_pair=base_pair,
            ),
            pair=base_pair,
            has_splash=True,
        )
        for color in eligible_by_color:
            basic_limits[color] = splash_basic_slots(
                land_count=DECK_BUILDER.default_land_count,
                nonbasic_lands=tuple(
                    land_source_colors(card=card)
                    for card in _pool_nonbasic_lands(
                        pool_grp_ids=pool_grp_ids,
                        card_database=card_database,
                        pair=base_pair,
                        splash_color=color,
                    )
                ),
                pair=base_pair,
                minimums=base_minimums,
            )
    picked_card_count = (
        0 if active_color is None else len(eligible_by_color[active_color])
    )
    return SplashState(
        enabled=True,
        base_pair=base_pair,
        active_color=active_color,
        picked_card_count=picked_card_count,
        fixing_sources=tuple(fixing_sources.items()),
        aggressive=_pool_is_aggressive(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
            base_pair=base_pair,
            config=config,
        ),
        required_sources=(
            0 if active_color is None else required_by_color[active_color]
        ),
        splash_grp_ids=(
            () if active_color is None else tuple(passed_grp_ids[active_color])
        ),
        splash_basic_limits=tuple(basic_limits.items()),
        base_source_minimums=tuple(base_minimums.items()),
    )


def assess_splash_card(
    *,
    card: CardInfo,
    grade: str | None,
    base_score: float,
    best_on_color_score: float | None,
    state: SplashState,
    config: SplashConfig = SPLASH,
) -> SplashAssessment:
    """Classify one card as ready, fixing, or ordinary off-color.
    Hard eligibility gates run before the smaller scoring multipliers.
    """

    if card.unknown:
        return _assessment(
            classification="unknown",
            state=state,
            grade=grade,
            reasons=("card metadata is unavailable",),
        )

    base_pair = state.base_pair
    if base_pair is None:
        return _assessment(
            classification="colorless" if not card.colors else "open",
            state=state,
            grade=grade,
            reasons=(
                ("card is colorless",)
                if not card.colors
                else ("primary colors are still open",)
            ),
        )

    if _is_splash_fixer(card=card, state=state, config=config):
        active_color = state.active_color
        fixing_sources = (
            0
            if active_color is None
            else state.fixing_for(color=active_color) + 1
        )
        required_sources = _plan_required_sources(state=state, config=config)
        return _assessment(
            classification="splash-fixer",
            state=state,
            splash_color=active_color,
            grade=grade,
            fixing_sources=fixing_sources,
            planned_basic_sources=_capped_basic_sources(
                state=state,
                color=active_color,
                required_sources=required_sources,
                fixing_sources=fixing_sources,
            )[0],
            required_sources=required_sources,
            reasons=("produces the active splash color",),
        )

    if not card.colors:
        return _assessment(
            classification="colorless",
            state=state,
            grade=grade,
            reasons=("card is colorless",),
        )

    if card_is_castable_in_pair(card=card, base_pair=base_pair):
        return _assessment(
            classification="on-color",
            state=state,
            grade=grade,
            reasons=("the card is castable with the primary pair",),
        )

    splash_color, off_color_pips = splash_requirement(
        card=card,
        base_pair=base_pair,
    )
    if len(base_pair) > 2:
        return _assessment(
            classification="off-color",
            state=state,
            off_color_pips=off_color_pips,
            grade=grade,
            reasons=("card is outside the three-color deck",),
        )

    if not state.enabled:
        return _assessment(
            classification="off-color",
            state=state,
            splash_color=splash_color,
            off_color_pips=off_color_pips,
            grade=grade,
            reasons=("splashing is disabled",),
        )

    if splash_color is None:
        return _assessment(
            classification="off-color",
            state=state,
            off_color_pips=off_color_pips,
            grade=grade,
            reasons=("card requires more than one color outside the primary pair",),
        )

    if off_color_pips > config.maximum_off_color_pips:
        return _assessment(
            classification="off-color",
            state=state,
            splash_color=splash_color,
            off_color_pips=off_color_pips,
            grade=grade,
            reasons=("card has too many off-color mana pips",),
        )

    if state.active_color is not None and splash_color != state.active_color:
        return _assessment(
            classification="off-color",
            state=state,
            splash_color=splash_color,
            off_color_pips=off_color_pips,
            grade=grade,
            reasons=(f"the active splash color is {state.active_color}",),
        )

    if state.picked_card_count >= config.maximum_cards:
        return _assessment(
            classification="off-color",
            state=state,
            splash_color=splash_color,
            off_color_pips=off_color_pips,
            grade=grade,
            reasons=("the splash-card limit is already reached",),
        )

    score_advantage = (
        None
        if best_on_color_score is None
        else base_score - best_on_color_score
    )
    if (
        score_advantage is not None
        and score_advantage <= config.minimum_score_advantage
    ):
        return _assessment(
            classification="off-color",
            state=state,
            splash_color=splash_color,
            off_color_pips=off_color_pips,
            grade=grade,
            score_advantage=score_advantage,
            reasons=("card is not enough better than the best on-color option",),
        )

    required_sources = max(
        splash_required_sources(off_color_pips=off_color_pips, config=config),
        state.required_sources,
    )
    fixing_sources = state.fixing_for(color=splash_color)
    planned_basics, basics_capped = _capped_basic_sources(
        state=state,
        color=splash_color,
        required_sources=required_sources,
        fixing_sources=fixing_sources,
    )
    available_sources = fixing_sources + planned_basics

    if grade is not None and available_sources >= required_sources:
        return _assessment(
            classification="splash-ready",
            state=state,
            splash_color=splash_color,
            off_color_pips=off_color_pips,
            grade=grade,
            score_advantage=score_advantage,
            fixing_sources=fixing_sources,
            planned_basic_sources=planned_basics,
            required_sources=required_sources,
            reasons=("card has sufficient mana support",),
        )

    reasons: list[str] = []
    if grade is None:
        reasons.append("card has no 17Lands rating")
    if available_sources < required_sources:
        missing = required_sources - available_sources
        if basics_capped:
            reasons.append(
                f"needs {missing} more splash {'land' if missing == 1 else 'lands'}: "
                f"{DECK_BUILDER.default_land_count} lands keep "
                f"{_format_base_minimums(state=state)} sources"
            )
        else:
            reasons.append(f"needs {missing} more deterministic sources")
    return _assessment(
        classification="off-color",
        state=state,
        splash_color=splash_color,
        off_color_pips=off_color_pips,
        grade=grade,
        score_advantage=score_advantage,
        fixing_sources=fixing_sources,
        planned_basic_sources=planned_basics,
        required_sources=required_sources,
        reasons=tuple(reasons) or ("splash requirements are not met",),
    )


def splash_required_sources(
    *,
    off_color_pips: int,
    config: SplashConfig = SPLASH,
) -> int:
    """Return the land sources a splash card needs, set by its symbol count.
    One symbol needs fewer sources than two symbols of the splash color.
    """

    if off_color_pips >= 2:
        return config.double_symbol_sources

    return config.single_symbol_sources


def splash_basic_slots(
    *,
    land_count: int,
    nonbasic_lands: tuple[tuple[str, ...], ...],
    pair: str,
    minimums: dict[str, int],
) -> int:
    """Return how many basics a splash can take after the base-color minimums.
    Each entry of nonbasic_lands lists the colors one drafted nonbasic land makes.
    """

    shortage = sum(
        max(
            0,
            minimums[color]
            - sum(color in source_colors for source_colors in nonbasic_lands),
        )
        for color in pair
    )
    return max(0, land_count - len(nonbasic_lands) - shortage)


def base_color_minimums(
    *,
    cards: tuple[CardInfo, ...],
    pair: str,
    has_splash: bool,
    config: DeckBuilderConfig = DECK_BUILDER,
) -> dict[str, int]:
    """Return the land-source minimum for each main color of the deck.
    A splash raises the minimum of the primary color, the one with more spells.
    """

    if not has_splash or len(pair) != 2:
        return {color: config.main_color_source_floor for color in pair}

    spell_counts = {color: 0 for color in pair}
    pip_totals = {color: 0 for color in pair}
    for card in cards:
        card_pips = card_pip_counts(card=card, pair=pair)
        for color in pair:
            spell_counts[color] += int(card_pips[color] > 0)
            pip_totals[color] += card_pips[color]

    primary = max(
        pair,
        key=lambda color: (spell_counts[color], pip_totals[color], -pair.index(color)),
    )
    return {
        color: (
            config.splash_primary_min_sources
            if color == primary
            else config.splash_secondary_min_sources
        )
        for color in pair
    }


def card_pip_counts(*, card: CardInfo, pair: str) -> dict[str, int]:
    """Count the mana symbols of each color in pair on one card.
    A card with no mana cost counts one symbol per card color.
    """

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


def is_land_card(*, card: CardInfo) -> bool:
    return any("Land" in type_line for type_line in card.types)


def is_basic_land_card(*, card: CardInfo) -> bool:
    if card.name in set(BASIC_LANDS_BY_COLOR.values()):
        return True

    return any(
        "Basic" in type_line and "Land" in type_line
        for type_line in card.types
    )


def land_source_colors(*, card: CardInfo) -> tuple[str, ...]:
    """Return the basic colors a land makes, falling back to its card colors.
    Colors come back in WUBRG order.
    """

    source_colors = land_mana_colors(card=card) or card.colors
    source_set = set(source_colors)
    return tuple(color for color in BASIC_LANDS_BY_COLOR if color in source_set)


def is_selected_nonbasic_land(
    *,
    card: CardInfo,
    pair: str,
    splash_colors: tuple[str, ...],
) -> bool:
    """Return whether the deck builder would play this drafted nonbasic land.
    The land must make only pair colors, or one of the splash colors.
    """

    if not is_land_card(card=card) or is_basic_land_card(card=card):
        return False

    source_colors = land_source_colors(card=card)
    if not source_colors:
        return False

    if all(color in pair for color in source_colors):
        return True

    return bool(splash_colors) and any(color in splash_colors for color in source_colors)


def _pool_base_spells(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    base_pair: str,
) -> tuple[CardInfo, ...]:
    cards = (card_database.lookup(grp_id=grp_id) for grp_id in pool_grp_ids)
    return tuple(card for card in cards if _is_base_spell(card=card, base_pair=base_pair))


def _pool_nonbasic_lands(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    pair: str,
    splash_color: str,
) -> tuple[CardInfo, ...]:
    cards = (card_database.lookup(grp_id=grp_id) for grp_id in pool_grp_ids)
    return tuple(
        card
        for card in cards
        if is_selected_nonbasic_land(
            card=card,
            pair=pair,
            splash_colors=(splash_color,),
        )
    )


def _capped_basic_sources(
    *,
    state: SplashState,
    color: str | None,
    required_sources: int,
    fixing_sources: int,
) -> tuple[int, bool]:
    wanted = splash_basic_sources(
        required_sources=required_sources,
        fixing_sources=fixing_sources,
    )
    limit = state.basic_limit_for(color=color)
    if limit is None or wanted <= limit:
        return wanted, False

    return limit, True


def _format_base_minimums(*, state: SplashState) -> str:
    names = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}
    minimums = dict(state.base_source_minimums)
    ordered = sorted(minimums, key=lambda color: -minimums[color])
    return " and ".join(f"{minimums[color]} {names[color]}" for color in ordered)


def splash_basic_sources(*, required_sources: int, fixing_sources: int) -> int:
    """Return the splash-color basics the deck plays beside drafted fixing.
    Drafted fixing lands replace basics one for one.
    """

    return max(0, required_sources - fixing_sources)


def splash_requirement(*, card: CardInfo, base_pair: str) -> tuple[str | None, int]:
    """Return one required off-pair color and its pip count when unambiguous.
    Hybrid symbols castable using a base color do not create a splash requirement.
    """

    outside_colors = tuple(
        color for color in COLOR_ORDER if color in card.colors and color not in base_pair
    )
    if len(outside_colors) != 1:
        return (None, len(outside_colors))

    splash_color = outside_colors[0]
    if not card.mana_cost:
        return (splash_color, 1)

    pip_count = 0
    castable_hybrid = False
    for symbol in MANA_SYMBOL_PATTERN.findall(card.mana_cost):
        symbol_colors = tuple(color for color in COLOR_ORDER if color in symbol)
        if not symbol_colors:
            continue
        if any(color in base_pair for color in symbol_colors):
            if splash_color in symbol_colors:
                castable_hybrid = True
            continue
        if splash_color in symbol_colors:
            pip_count += 1

    if pip_count == 0 and castable_hybrid:
        return (None, 0)

    return (splash_color, pip_count or 1)


def card_is_castable_in_pair(*, card: CardInfo, base_pair: str) -> bool:
    """Return whether a card can be cast using only the primary colors.
    Hybrid symbols satisfied by a primary color count as on-color.
    """

    if not card.colors or all(color in base_pair for color in card.colors):
        return True

    splash_color, off_color_pips = splash_requirement(
        card=card,
        base_pair=base_pair,
    )
    return splash_color is None and off_color_pips == 0


def _assessment(
    *,
    classification: str,
    state: SplashState,
    grade: str | None,
    reasons: tuple[str, ...],
    splash_color: str | None = None,
    off_color_pips: int = 0,
    score_advantage: float | None = None,
    fixing_sources: int = 0,
    planned_basic_sources: int = 0,
    required_sources: int = 0,
) -> SplashAssessment:
    return SplashAssessment(
        classification=classification,
        splash_color=splash_color,
        off_color_pips=off_color_pips,
        picked_card_count=state.picked_card_count,
        fixing_sources=fixing_sources,
        planned_basic_sources=planned_basic_sources,
        available_sources=fixing_sources + planned_basic_sources,
        required_sources=required_sources,
        grade=grade,
        score_advantage=score_advantage,
        aggressive=state.aggressive,
        reasons=reasons,
    )


def _fixing_counts(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    base_pair: str | None,
) -> dict[str, int]:
    counts = {
        color: 0
        for color in COLOR_ORDER
        if base_pair is None or color not in base_pair
    }
    if base_pair is None:
        return counts

    for grp_id in pool_grp_ids:
        card = card_database.lookup(grp_id=grp_id)
        if card.unknown or not card_is_castable_in_pair(
            card=card,
            base_pair=base_pair,
        ):
            continue
        if not _is_drafted_fixing_land(card=card):
            continue

        for color in land_mana_colors(card=card):
            if color in counts:
                counts[color] += 1

    return counts


_BASIC_LAND_TYPE_COLORS = (
    ("Plains", "W"),
    ("Island", "U"),
    ("Swamp", "B"),
    ("Mountain", "R"),
    ("Forest", "G"),
)
_QUOTED_TEXT_RE = re.compile(r'"[^"]*"|\u201c[^\u201d]*\u201d')
_ADD_CLAUSE_RE = re.compile(r":\s*Add ([^.\n]*)", re.IGNORECASE)
_FLEXIBLE_MANA_RE = re.compile(
    r"any color|chosen color|any type|any one color|any combination of colors",
    re.IGNORECASE,
)


def land_mana_colors(*, card: CardInfo) -> tuple[str, ...]:
    """Return the colours a land itself can produce, in WUBRG order.
    Ignores mana from tokens or granted abilities that Scryfall also lists.
    """
    ordered_produced = tuple(
        color for color in COLOR_ORDER if color in card.produced_mana
    )
    if card.faces:
        sides = [(face.type_line or "", face.oracle_text or "") for face in card.faces]
    else:
        sides = [
            (card.type_line or " ".join(card.types), card.oracle_text or ""),
        ]
    land_sides = [side for side in sides if "Land" in side[0]]
    if not land_sides:
        return ordered_produced

    found: set[str] = set()
    for type_line, text in land_sides:
        side_colors = {
            color
            for land_type, color in _BASIC_LAND_TYPE_COLORS
            if re.search(rf"\b{land_type}\b", type_line)
        }
        clauses = _ADD_CLAUSE_RE.findall(_QUOTED_TEXT_RE.sub("", text))
        if not clauses and not side_colors:
            return ordered_produced
        for clause in clauses:
            if _FLEXIBLE_MANA_RE.search(clause):
                return ordered_produced
            side_colors.update(re.findall(r"\{([WUBRG])\}", clause))
        found |= side_colors
    return tuple(color for color in COLOR_ORDER if color in found)


def _active_splash_color(
    *,
    eligible_by_color: dict[str, list[tuple[float, int]]],
    fixing_sources: dict[str, int],
) -> str | None:
    candidates = tuple(
        color for color, cards in eligible_by_color.items() if cards
    )
    if not candidates:
        return None

    return max(
        candidates,
        key=lambda color: (
            len(eligible_by_color[color]),
            max(eligible_by_color[color]),
            fixing_sources.get(color, 0),
            -COLOR_ORDER.index(color),
        ),
    )


def _pool_is_aggressive(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    base_pair: str,
    config: SplashConfig,
) -> bool:
    spells = tuple(
        card_database.lookup(grp_id=grp_id)
        for grp_id in pool_grp_ids
        if _is_base_spell(
            card=card_database.lookup(grp_id=grp_id),
            base_pair=base_pair,
        )
    )
    if len(spells) < config.aggressive_minimum_spells:
        return False

    mana_values = tuple(
        card.mana_value for card in spells if card.mana_value is not None
    )
    if not mana_values:
        return False

    average_mana_value = sum(mana_values) / len(mana_values)
    two_drop_ratio = sum(value == 2.0 for value in mana_values) / len(mana_values)
    return (
        average_mana_value <= config.aggressive_average_mana_value_max
        and two_drop_ratio >= config.aggressive_minimum_two_drop_ratio
    )


def _splash_win_rate_floor(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None,
    base_pair: str,
    profile_win_rates: dict[int, float],
    config: SplashConfig,
) -> float | None:
    win_rates = sorted(
        (
            win_rate
            for grp_id in pool_grp_ids
            if _is_base_spell(
                card=card_database.lookup(grp_id=grp_id),
                base_pair=base_pair,
            )
            and (
                win_rate := _global_win_rate(
                    ratings_data=ratings_data,
                    grp_id=grp_id,
                    profile_win_rates=profile_win_rates,
                )
            )
            is not None
        ),
        reverse=True,
    )
    if len(win_rates) < DECK_BUILDER.target_spell_count:
        return None

    best = win_rates[: DECK_BUILDER.target_spell_count]
    return statistics.median(best) - config.splash_median_margin


def _is_base_spell(*, card: CardInfo, base_pair: str) -> bool:
    if card.unknown or any("Land" in type_line for type_line in card.types):
        return False

    return card_is_castable_in_pair(card=card, base_pair=base_pair)


def _is_splash_fixer(
    *,
    card: CardInfo,
    state: SplashState,
    config: SplashConfig,
) -> bool:
    active_color = state.active_color
    if active_color is None or active_color not in land_mana_colors(card=card):
        return False
    if not _is_drafted_fixing_land(card=card):
        return False

    base_pair = state.base_pair
    if base_pair is None or not card_is_castable_in_pair(
        card=card,
        base_pair=base_pair,
    ):
        return False

    required_sources = _plan_required_sources(state=state, config=config)
    return state.fixing_for(color=active_color) < required_sources


def _is_drafted_fixing_land(*, card: CardInfo) -> bool:
    is_land = any("Land" in type_line for type_line in card.types)
    is_basic = any(
        "Basic" in type_line and "Land" in type_line
        for type_line in card.types
    )
    return is_land and not is_basic


def _plan_required_sources(*, state: SplashState, config: SplashConfig) -> int:
    return max(state.required_sources, config.single_symbol_sources)


def _global_grade(
    *,
    ratings_data: SeventeenLandsData | None,
    grp_id: int,
) -> str | None:
    if ratings_data is None:
        return None

    return ratings_data.rating_for(grp_id=grp_id).letter_grade


def _global_win_rate(
    *,
    ratings_data: SeventeenLandsData | None,
    grp_id: int,
    profile_win_rates: dict[int, float] | None = None,
) -> float | None:
    if ratings_data is not None:
        return ratings_data.rating_for(grp_id=grp_id).gih_win_rate

    if profile_win_rates is None:
        return None

    return profile_win_rates.get(grp_id)


def _profile_win_rates(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    set_profile: SetProfile | None,
) -> dict[int, float]:
    """Map pool cards to set-profile GIH win rates, used without 17Lands data.
    A rating with no games in hand holds only the prior, so it is left out.
    """

    ratings = empirical_card_ratings(
        profile=set_profile,
        cards=(card_database.lookup(grp_id=grp_id) for grp_id in set(pool_grp_ids)),
    )
    return {
        grp_id: rating.gih_win_rate.value
        for grp_id, rating in ratings.items()
        if rating.gih_win_rate.samples > 0
    }
