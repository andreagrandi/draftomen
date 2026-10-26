"""Shared splash eligibility and mana-support assessment.
Keep third-color decisions separate from primary-pair commitment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import SPLASH, SplashConfig
from draftomen.seventeen import GRADE_LABELS, SeventeenLandsData

COLOR_ORDER = ("W", "U", "B", "R", "G")
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

    def fixing_for(self, *, color: str) -> int:
        return dict(self.fixing_sources).get(color, 0)


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
        return self.classification in {"splash-ready", "splash-speculative"}


def infer_splash_state(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None,
    base_pair: str | None,
    enabled: bool,
    config: SplashConfig = SPLASH,
) -> SplashState:
    """Infer the active splash color and deterministic fixing from the pool.
    An isolated elite third-color card establishes the plan after pair commitment.
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
    for index, grp_id in enumerate(pool_grp_ids):
        card = card_database.lookup(grp_id=grp_id)
        splash_color, off_color_pips = splash_requirement(card=card, base_pair=base_pair)
        if splash_color is None or off_color_pips > config.maximum_off_color_pips:
            continue

        grade = _global_grade(ratings_data=ratings_data, grp_id=grp_id)
        if not grade_at_least(grade=grade, minimum=config.supported_minimum_grade):
            continue

        win_rate = _global_win_rate(ratings_data=ratings_data, grp_id=grp_id)
        eligible_by_color[splash_color].append(
            (0.0 if win_rate is None else win_rate, -index)
        )

    active_color = _active_splash_color(
        eligible_by_color=eligible_by_color,
        fixing_sources=fixing_sources,
        config=config,
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
    )


def assess_splash_card(
    *,
    card: CardInfo,
    grade: str | None,
    base_score: float,
    best_on_color_score: float | None,
    locked: bool,
    state: SplashState,
    config: SplashConfig = SPLASH,
) -> SplashAssessment:
    """Classify one card as ready, speculative, fixing, or ordinary off-color.
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
        required_sources = _required_sources(
            splash_card_count=max(1, state.picked_card_count),
            config=config,
        )
        return _assessment(
            classification="splash-fixer",
            state=state,
            splash_color=active_color,
            grade=grade,
            fixing_sources=fixing_sources,
            planned_basic_sources=min(
                config.planned_basic_sources,
                required_sources,
            ),
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
        and score_advantage < config.minimum_score_advantage
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

    prospective_count = state.picked_card_count + 1
    required_sources = _required_sources(
        splash_card_count=prospective_count,
        config=config,
    )
    fixing_sources = state.fixing_for(color=splash_color)
    planned_basics = min(config.planned_basic_sources, required_sources)
    available_sources = fixing_sources + planned_basics
    supported_grade = config.supported_minimum_grade
    if state.aggressive:
        supported_grade = config.speculative_minimum_grade

    if (
        available_sources >= required_sources
        and grade_at_least(grade=grade, minimum=supported_grade)
    ):
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
            reasons=("elite single-pip card has sufficient mana support",),
        )

    if (
        not locked
        and not state.aggressive
        and grade_at_least(
            grade=grade,
            minimum=config.speculative_minimum_grade,
        )
    ):
        return _assessment(
            classification="splash-speculative",
            state=state,
            splash_color=splash_color,
            off_color_pips=off_color_pips,
            grade=grade,
            score_advantage=score_advantage,
            fixing_sources=fixing_sources,
            planned_basic_sources=planned_basics,
            required_sources=required_sources,
            reasons=("exceptional card can be supported by future fixing",),
        )

    reasons: list[str] = []
    if not grade_at_least(grade=grade, minimum=supported_grade):
        reasons.append(f"grade is below {supported_grade}")
    if available_sources < required_sources:
        reasons.append(
            f"needs {required_sources - available_sources} more deterministic sources"
        )
    if locked:
        reasons.append("speculative splashes are disabled after color lock")
    if state.aggressive:
        reasons.append("aggressive pools require supported exceptional splashes")
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


def grade_at_least(*, grade: str | None, minimum: str) -> bool:
    if grade not in GRADE_LABELS or minimum not in GRADE_LABELS:
        return False

    return GRADE_LABELS.index(grade) >= GRADE_LABELS.index(minimum)


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

        for color in card.produced_mana:
            if color in counts:
                counts[color] += 1

    return counts


def _active_splash_color(
    *,
    eligible_by_color: dict[str, list[tuple[float, int]]],
    fixing_sources: dict[str, int],
    config: SplashConfig,
) -> str | None:
    candidates = tuple(
        color for color, cards in eligible_by_color.items() if cards
    )
    if not candidates:
        return None

    return max(
        candidates,
        key=lambda color: (
            _supported_card_count(
                card_count=len(eligible_by_color[color]),
                fixing_sources=fixing_sources.get(color, 0),
                config=config,
            ),
            len(eligible_by_color[color]),
            max(eligible_by_color[color]),
            fixing_sources.get(color, 0),
            -COLOR_ORDER.index(color),
        ),
    )


def _supported_card_count(
    *,
    card_count: int,
    fixing_sources: int,
    config: SplashConfig,
) -> int:
    available_sources = fixing_sources + config.planned_basic_sources
    if available_sources >= config.multiple_card_sources:
        return min(card_count, config.maximum_cards)
    if available_sources >= config.single_card_sources:
        return min(card_count, 1)

    return 0


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
    if active_color is None or active_color not in card.produced_mana:
        return False
    if not _is_drafted_fixing_land(card=card):
        return False

    base_pair = state.base_pair
    if base_pair is None or not card_is_castable_in_pair(
        card=card,
        base_pair=base_pair,
    ):
        return False

    required_sources = _required_sources(
        splash_card_count=max(1, state.picked_card_count),
        config=config,
    )
    available_sources = (
        state.fixing_for(color=active_color) + config.planned_basic_sources
    )
    return available_sources < required_sources


def _is_drafted_fixing_land(*, card: CardInfo) -> bool:
    is_land = any("Land" in type_line for type_line in card.types)
    is_basic = any(
        "Basic" in type_line and "Land" in type_line
        for type_line in card.types
    )
    return is_land and not is_basic


def _required_sources(
    *,
    splash_card_count: int,
    config: SplashConfig,
) -> int:
    if splash_card_count <= 1:
        return config.single_card_sources

    return config.multiple_card_sources


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
) -> float | None:
    if ratings_data is None:
        return None

    return ratings_data.rating_for(grp_id=grp_id).gih_win_rate
