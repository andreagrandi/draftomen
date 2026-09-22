"""Shared ranking modes for scored draft cards.
Keep UI sorting and backtest recommendations explicit and consistent.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

from draftomen.pickengine import ScoredCard

RankingMode: TypeAlias = Literal["win_rate", "score", "alsa", "mv"]

DEFAULT_RANKING_MODE: RankingMode = "score"
RANKING_MODES: tuple[RankingMode, ...] = ("score", "win_rate", "alsa", "mv")
RANKING_LABELS: dict[RankingMode, str] = {
    "win_rate": "17L WR",
    "score": "DO Score",
    "alsa": "ALSA",
    "mv": "MV",
}


def validate_ranking_mode(*, ranking_mode: str) -> RankingMode:
    """Return a supported ranking mode or fail loudly.
    Callers use this before producing user-visible recommendations.
    """

    if ranking_mode in RANKING_MODES:
        return ranking_mode  # type: ignore[return-value]

    allowed = ", ".join(RANKING_MODES)
    raise ValueError(f"Unsupported ranking mode {ranking_mode!r}; expected one of {allowed}.")


def ranking_label(*, ranking_mode: str) -> str:
    """Return the user-facing label for one ranking mode.
    Unknown values are validated so labels never silently lie.
    """

    return RANKING_LABELS[validate_ranking_mode(ranking_mode=ranking_mode)]


def rank_scored_cards(
    *,
    cards: tuple[ScoredCard, ...],
    ranking_mode: str,
) -> tuple[ScoredCard, ...]:
    """Return scored cards ordered by the selected ranking mode.
    Ties remain deterministic and prefer stronger DO scores where useful.
    """

    mode = validate_ranking_mode(ranking_mode=ranking_mode)
    if mode == "win_rate":
        return tuple(sorted(cards, key=_win_rate_sort_key))

    if mode == "score":
        return tuple(sorted(cards, key=_score_sort_key))

    if mode == "alsa":
        return tuple(sorted(cards, key=_alsa_sort_key))

    return tuple(sorted(cards, key=_mana_value_sort_key))


def _win_rate_sort_key(
    card: ScoredCard,
) -> tuple[bool, bool, float, float, float, int]:
    win_rate = card.rating.gih_win_rate
    if win_rate is None:
        return (
            card.freely_available_basic,
            True,
            0.0,
            -card.ordering_score,
            -card.raw_score,
            card.original_index,
        )

    return (
        card.freely_available_basic,
        False,
        -win_rate,
        -card.ordering_score,
        -card.raw_score,
        card.original_index,
    )


def _score_sort_key(
    card: ScoredCard,
) -> tuple[bool, int, float, float, float, int]:
    return (
        card.freely_available_basic,
        card.score_sort_index,
        -card.ordering_score,
        -card.raw_score,
        -card.base_rating,
        card.original_index,
    )


def _alsa_sort_key(card: ScoredCard) -> tuple[bool, float, float, float, int]:
    alsa = card.rating.average_last_seen_at
    sort_alsa = float("inf") if alsa is None else alsa
    return (
        card.freely_available_basic,
        sort_alsa,
        -card.ordering_score,
        -card.raw_score,
        card.original_index,
    )


def _mana_value_sort_key(
    card: ScoredCard,
) -> tuple[bool, float, float, float, int]:
    mana_value = card.card.mana_value
    sort_mana_value = float("inf") if mana_value is None else mana_value
    return (
        card.freely_available_basic,
        sort_mana_value,
        -card.ordering_score,
        -card.raw_score,
        card.original_index,
    )

