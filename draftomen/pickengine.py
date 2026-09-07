"""Card scoring and ranked pack recommendations.
Keep pick-quality math isolated from CLI and TUI rendering code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from functools import cmp_to_key
from types import MappingProxyType
from typing import Mapping

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.config import COLOR_PAIRS, PICK_ENGINE, SPLASH, PickEngineConfig
from draftomen.events import EXPECTED_PICKS_PER_PACK, EXPECTED_TOTAL_PICKS
from draftomen.pool_ledger import (
    FIXING_ROLES,
    LedgerStage,
    PACKAGE_ROLES,
    PoolRoleLedger,
    PRE_PICK_PROJECTION,
    TargetCoverage,
    project_pool_role_ledger,
)
from draftomen.semantic_roles import Role, RoleAssignment, resolve_card_roles
from draftomen.set_profile import (
    CardRating,
    ProfileMaturity,
    SetProfile,
    profile_card_key,
)
from draftomen.seventeen import (
    FORMAT_RATING_SOURCE,
    NEUTRAL_PRIOR_SOURCE,
    PREMIER_DRAFT_FORMAT,
    QUICK_DRAFT_FORMAT,
    RatingSampleCounts,
    RatingSourceMetadata,
    ResolvedCardRating,
    SeventeenLandsData,
)
from draftomen.splash import (
    SplashAssessment,
    SplashState,
    assess_splash_card,
    card_is_castable_in_pair,
    infer_splash_state,
)

CLOSE_DO_SCORE_THRESHOLD = 3.0
CLOSE_WIN_RATE_THRESHOLD = 0.01
STANDARD_BASIC_LAND_NAMES = frozenset(
    {"Plains", "Island", "Swamp", "Mountain", "Forest"}
)
STANDARD_BASIC_LAND_TYPE_LINES = frozenset(
    f"Basic Land — {name}" for name in STANDARD_BASIC_LAND_NAMES
)

MAX_ROLE_TERM = 2.5
MAX_URGENCY_TERM = 3.0
MAX_SYNERGY_TERM = 1.5
MAX_REDUNDANCY_TERM = 2.0
MAX_UNSUPPORTED_PAYOFF_TERM = 2.0
MAX_FIXING_TERM = 1.5
MAX_CONTEXTUAL_ADJUSTMENT = 6.0
PAYOFF_PACKAGES: Mapping[Role, str] = {
    Role.DRAW_SECOND_PAYOFF: "draw",
    Role.GO_WIDE_PAYOFF: "go_wide",
    Role.TYPAL_PAYOFF: "typal",
    Role.DEATH_PAYOFF: "sacrifice",
    Role.GRAVEYARD_PAYOFF: "graveyard",
    Role.ARTIFACT_PAYOFF: "artifact",
    Role.ENCHANTMENT_PAYOFF: "enchantment",
    Role.EQUIPMENT_PAYOFF: "equipment",
    Role.POWER_THRESHOLD_PAYOFF: "threshold",
    Role.POWER_N_PAYOFF: "threshold",
}
_TERM_BOUNDS: Mapping[str, tuple[float, float]] = {
    "role": (0.0, MAX_ROLE_TERM),
    "urgency": (0.0, MAX_URGENCY_TERM),
    "synergy": (0.0, MAX_SYNERGY_TERM),
    "redundancy": (-MAX_REDUNDANCY_TERM, 0.0),
    "unsupported_payoff": (-MAX_UNSUPPORTED_PAYOFF_TERM, 0.0),
    "fixing": (0.0, MAX_FIXING_TERM),
}
PAIR_PROFILE_SAMPLE_SCALE = 100.0
PAIR_GAME_SAMPLE_SCALE = 500.0
PAIR_CARD_GIH_SAMPLE_SCALE = 500.0
PROFILE_RATING_SOURCE = "profile"
PROFILE_SOURCE_LABEL = "Profile"


@dataclass(frozen=True, slots=True)
class _ProfileRatingLookup:
    """Immutable runtime-card and canonical profile-rating views."""

    ratings_by_grp_id: Mapping[int, ResolvedCardRating]
    distribution: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ratings_by_grp_id",
            MappingProxyType(dict(self.ratings_by_grp_id)),
        )
        object.__setattr__(self, "distribution", tuple(self.distribution))

    def rating_for(self, *, grp_id: int) -> ResolvedCardRating | None:
        return self.ratings_by_grp_id.get(grp_id)


@dataclass(frozen=True, slots=True)
class ContextualScoreBreakdown:
    """Bounded structural and semantic additions to a base card score."""

    role: float = 0.0
    urgency: float = 0.0
    synergy: float = 0.0
    redundancy: float = 0.0
    unsupported_payoff: float = 0.0
    fixing: float = 0.0

    def __post_init__(self) -> None:
        for name, (lower, upper) in _TERM_BOUNDS.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"Contextual {name} term must be finite.")
            if not lower <= float(value) <= upper:
                raise ValueError(
                    f"Contextual {name} term must be between {lower:g} and {upper:g}."
                )
            object.__setattr__(self, name, float(f"{float(value):.6f}"))

    @property
    def aggregate(self) -> float:
        """Return the collectively capped contextual score contribution."""

        return float(
            f"{_clamp(value=sum(getattr(self, name) for name in _TERM_BOUNDS),
                       lower=-MAX_CONTEXTUAL_ADJUSTMENT,
                       upper=MAX_CONTEXTUAL_ADJUSTMENT):.6f}"
        )

    def to_json(self) -> dict[str, float]:
        """Serialize each term and its collectively capped aggregate."""

        return {
            **{name: getattr(self, name) for name in _TERM_BOUNDS},
            "aggregate": self.aggregate,
        }


@dataclass(frozen=True, slots=True)
class PickScoringContext:
    """Validated immutable context for one pre-pick scoring decision."""

    set_profile: SetProfile
    role_ledger: PoolRoleLedger

    def __post_init__(self) -> None:
        if not isinstance(self.set_profile, SetProfile):
            raise TypeError("PickScoringContext.set_profile must be a SetProfile.")
        if self.set_profile.maturity is ProfileMaturity.GENERIC:
            raise ValueError("PickScoringContext requires a non-generic profile.")
        if not isinstance(self.role_ledger, PoolRoleLedger):
            raise TypeError(
                "PickScoringContext.role_ledger must be a PoolRoleLedger."
            )
        if self.role_ledger.mode is not PRE_PICK_PROJECTION:
            raise ValueError(
                "PickScoringContext requires a pre-pick projection."
            )
        stage = self.role_ledger.stage
        if stage is None:
            raise ValueError(
                "PickScoringContext requires a pre-pick stage."
            )
        if not isinstance(stage, LedgerStage):
            raise TypeError(
                "PickScoringContext.role_ledger.stage must be a LedgerStage."
            )
        expected_source = f"profile:{self.set_profile.maturity.value}"
        if self.role_ledger.profile_fingerprint != self.set_profile.fingerprint:
            raise ValueError(
                "PickScoringContext ledger/profile fingerprint does not match."
            )
        if self.role_ledger.profile_source != expected_source:
            raise ValueError(
                "PickScoringContext ledger/profile evidence does not match."
            )

    @property
    def stage(self) -> LedgerStage:
        """Return the validated pre-pick stage."""

        assert self.role_ledger.stage is not None
        return self.role_ledger.stage


def _normalize_scoring_profile(
    profile: SetProfile | None,
) -> SetProfile | None:
    if profile is None or profile.maturity is ProfileMaturity.GENERIC:
        return None
    return profile


def _is_empirical_profile(*, profile: SetProfile | None) -> bool:
    return profile is not None and profile.maturity in {
        ProfileMaturity.EARLY,
        ProfileMaturity.MATURE,
    }


def _resolved_profile_rating(
    *,
    card: CardInfo,
    profile: SetProfile,
    card_rating: CardRating,
) -> ResolvedCardRating:
    gih_rate = card_rating.gih_win_rate
    return ResolvedCardRating(
        grp_id=card.grp_id,
        name=card.name,
        color="".join(card.colors) or "C",
        rarity=card.rarity,
        average_last_seen_at=card_rating.average_last_seen_at,
        gih_win_rate=gih_rate.value,
        opening_hand_win_rate=None,
        drawn_improvement_win_rate=None,
        sample_counts=RatingSampleCounts(
            seen=0,
            picked=0,
            games_played=0,
            opening_hand=0,
            games_in_hand=gih_rate.samples,
        ),
        letter_grade=None,
        neutral_prior_score=None,
        metadata=RatingSourceMetadata(
            requested_format=profile.event_format,
            source=PROFILE_RATING_SOURCE,
            source_format=profile.event_format,
            fallback_reason=None,
        ),
    )


def _profile_rating_lookup(
    *,
    profile: SetProfile | None,
    card_database: CardDatabase,
) -> _ProfileRatingLookup:
    if not _is_empirical_profile(profile=profile):
        return _ProfileRatingLookup(ratings_by_grp_id={}, distribution=())

    assert profile is not None
    profile_ratings = {rating.card_key: rating for rating in profile.card_ratings}
    runtime_ratings: dict[int, ResolvedCardRating] = {}
    distribution: list[float] = []
    matched_keys: set[str] = set()
    for grp_id, card in sorted(card_database.cards.items()):
        if (
            card.unknown
            or card.set_code is None
            or card.set_code.casefold() != profile.set_code
        ):
            continue
        card_key = profile_card_key(card)
        card_rating = profile_ratings.get(card_key)
        if card_rating is None:
            continue
        runtime_ratings[grp_id] = _resolved_profile_rating(
            card=card,
            profile=profile,
            card_rating=card_rating,
        )
        if card_key not in matched_keys:
            distribution.append(card_rating.gih_win_rate.value)
            matched_keys.add(card_key)

    return _ProfileRatingLookup(
        ratings_by_grp_id=runtime_ratings,
        distribution=tuple(distribution),
    )


def _normalize_scoring_context(
    scoring_context: PickScoringContext | None,
) -> PickScoringContext | None:
    if scoring_context is None:
        return None
    if not isinstance(scoring_context, PickScoringContext):
        raise TypeError("scoring_context must be a PickScoringContext.")
    if scoring_context.set_profile.maturity is ProfileMaturity.GENERIC:
        return None
    return scoring_context


def recommendation_confidence_summary(
    *,
    cards: tuple[ScoredCard, ...],
    ranking_mode: str,
    phase: str,
) -> str | None:
    """Summarize how decisive the current recommendation evidence is.
    The input cards must already be ordered by ``ranking_mode``.
    """

    close_label = _close_pick_label(cards=cards, ranking_mode=ranking_mode)
    if phase == "open":
        if close_label is not None:
            return f"early/open {close_label}; stay flexible"

        return "early/open pick — stay flexible"

    return close_label


def recommendation_explanation(
    *,
    scored_card: ScoredCard,
    inferred_pair: str | None,
) -> str:
    """Describe one recommendation using scoring and pool evidence.
    Wording describes supporting evidence rather than promising an outcome.
    """

    if scored_card.freely_available_basic:
        return (
            f"{scored_card.card.name} is freely available during deck building, "
            "so it receives 0 DO points and ranks after draftable cards."
        )

    fit = scored_card.color_fit.replace("-", " ")
    pair = inferred_pair or scored_card.contextual_pair
    pool_context = (
        f"the inferred {pair} pool"
        if pair is not None
        else "an open-color pool"
    )
    gih_win_rate = scored_card.rating.gih_win_rate
    alsa = scored_card.rating.average_last_seen_at
    if gih_win_rate is not None:
        rating_evidence = (
            f"{scored_card.source_label} GIH win rate "
            f"{gih_win_rate:.1%}"
        )
    elif scored_card.no_data and alsa is not None:
        rating_evidence = (
            f"neutral-prior estimate adjusted by ALSA "
            f"{alsa:.2f}"
        )
    elif scored_card.no_data:
        rating_evidence = "neutral-prior estimate with no GIH data"
    else:
        rating_evidence = f"{scored_card.source_label} rating data"

    context_notes: list[str] = []
    if scored_card.contextual_pair is not None:
        theme = (
            f", theme {scored_card.contextual_theme}"
            if scored_card.contextual_theme is not None
            else ""
        )
        maturity = scored_card.contextual_profile_maturity or "unknown"
        confidence = scored_card.contextual_profile_confidence
        confidence_note = (
            f"{confidence:.0%} confidence"
            if confidence is not None
            else "unknown confidence"
        )
        context_notes.append(
            f"context {scored_card.contextual_pair}{theme}; "
            f"{maturity} profile ({confidence_note})"
        )
    material_terms = tuple(
        (name, getattr(scored_card.contextual_breakdown, name))
        for name in _TERM_BOUNDS
        if abs(getattr(scored_card.contextual_breakdown, name)) > 0.01
    )
    if material_terms:
        term_text = ", ".join(
            f"{name} {value:+.2f}" for name, value in material_terms
        )
        term_text += f", aggregate {scored_card.contextual_breakdown.aggregate:+.2f}"
        if scored_card.contextual_evidence:
            term_text += ": " + "; ".join(scored_card.contextual_evidence)
        context_notes.append("material terms: " + term_text)
    elif scored_card.contextual_evidence:
        context_notes.append(
            "material terms: " + "; ".join(scored_card.contextual_evidence)
        )
    context_note = (
        " " + " ".join(context_notes) + "."
        if context_notes
        else ""
    )
    return (
        f"{fit.capitalize()} fit supports a {scored_card.score} DO-point "
        f"candidate for {pool_context}; {rating_evidence} informs the score."
        f"{context_note}"
    )


def _close_pick_label(
    *,
    cards: tuple[ScoredCard, ...],
    ranking_mode: str,
) -> str | None:
    draftable_cards = tuple(
        card for card in cards if not card.freely_available_basic
    )
    if len(draftable_cards) < 2:
        return None

    top_card, second_card = draftable_cards[:2]
    if ranking_mode == "score":
        score_delta = max(0.0, top_card.raw_score - second_card.raw_score)
        if score_delta <= CLOSE_DO_SCORE_THRESHOLD:
            return f"close pick — top two within {score_delta:.1f} DO points"

        return None

    if ranking_mode == "win_rate":
        top_win_rate = top_card.rating.gih_win_rate
        second_win_rate = second_card.rating.gih_win_rate
        if top_win_rate is None or second_win_rate is None:
            return None

        win_rate_delta = max(0.0, top_win_rate - second_win_rate)
        if win_rate_delta <= CLOSE_WIN_RATE_THRESHOLD:
            return f"close pick — top two within {win_rate_delta * 100:.1f}pp WR"

    return None


@dataclass(frozen=True, slots=True)
class ScoreNormalization:
    """Rating bounds used to normalize raw card ratings.
    The neutral prior is centered at score 50 by construction.
    """

    lower_rating: float
    upper_rating: float
    neutral_rating: float


@dataclass(frozen=True, slots=True)
class ColorCommitment:
    """Color inference for the current pick.
    The level is a 0.0-1.0 ramp from open to locked.
    """

    pick_index: int
    pool_size: int
    color_weights: tuple[tuple[str, float], ...]
    inferred_pair: str | None
    level: float

    @property
    def locked(self) -> bool:
        """Return whether commitment is fully locked.
        Locked picks may use pair-filtered ratings when available.
        """

        return self.level >= 1.0

    @property
    def phase(self) -> str:
        """Return a compact label for status-line rendering.
        This keeps CLI and TUI language consistent.
        """

        if self.level <= 0.0:
            return "open"

        if self.locked:
            return "locked"

        return "building"


@dataclass(frozen=True, slots=True)
class ScoredCard:
    """One offered card with its computed pick score.
    Rows keep source metadata so renderers can mark fallbacks clearly.
    """

    card: CardInfo
    rating: ResolvedCardRating
    original_index: int
    base_rating: float
    base_score: float
    color_factor: float
    adjusted_rating: float
    raw_score: float
    score: int
    source_label: str
    color_fit: str
    pair_tiebreaker_pair: str | None
    pair_tiebreaker_win_rate: float | None
    pair_tiebreaker_weight: float | None
    score_sort_index: int
    splash: SplashAssessment
    freely_available_basic: bool
    contextual_breakdown: ContextualScoreBreakdown = ContextualScoreBreakdown()
    contextual_evidence: tuple[str, ...] = ()
    contextual_pair: str | None = None
    contextual_theme: str | None = None
    contextual_profile_maturity: str | None = None
    contextual_profile_confidence: float | None = None

    @property
    def no_data(self) -> bool:
        """Return whether this row used the neutral-prior fallback.
        Renderers mark these cards because no resolved GIH rate was available.
        """

        return self.rating.neutral_prior and not self.freely_available_basic

    @property
    def prior_adjusted_by_alsa(self) -> bool:
        """Return whether ALSA moved the neutral prior up or down.
        Missing ALSA leaves the neutral prior exactly centered.
        """

        return self.no_data and self.rating.average_last_seen_at is not None


@dataclass(frozen=True, slots=True)
class ScoredPack:
    """A recommendation-ordered view of one offered pack.
    Source summary describes the actual data used in this pack.
    """

    cards: tuple[ScoredCard, ...]
    normalization: ScoreNormalization
    source_summary: str
    commitment: ColorCommitment
    splash_state: SplashState
    role_ledger: PoolRoleLedger | None = None
    scoring_context: PickScoringContext | None = None


class PickEngine:
    """Score offered cards using set-profile or 17Lands evidence and priors.
    Pool color weights progressively bias scores toward an inferred pair.
    """

    def __init__(
        self,
        *,
        ratings_data: SeventeenLandsData | None = None,
        config: PickEngineConfig = PICK_ENGINE,
        splash_enabled: bool = SPLASH.enabled_by_default,
        contextual_adjustments_enabled: bool = True,
        set_profile: SetProfile | None = None,
        scoring_context: PickScoringContext | None = None,
    ) -> None:
        scoring_context = _normalize_scoring_context(scoring_context)
        self.ratings_data = ratings_data
        self.config = config
        self.splash_enabled = splash_enabled
        self.contextual_adjustments_enabled = contextual_adjustments_enabled
        self.set_profile = _normalize_scoring_profile(set_profile)
        self.scoring_context = scoring_context
        self.normalization = _normalization_from_data(
            ratings_data=ratings_data,
            config=config,
        )

    def score_pack(
        self,
        *,
        offered_grp_ids: tuple[int, ...],
        card_database: CardDatabase,
        pool_grp_ids: tuple[int, ...] = (),
        pick_index: int | None = None,
        pack_number: int | None = None,
        pick_number: int | None = None,
        global_pick_index: int | None = None,
        estimated_remaining_picks: int | None = None,
        scoring_context: PickScoringContext | None = None,
    ) -> ScoredPack:
        """Return offered cards in recommendation order.

        Contextual terms use only the validated pre-pick ledger supplied by
        ``PickScoringContext``. A profile-backed ledger built for this exact
        pool and stage becomes that context when no explicit one is supplied.
        """
        candidate_context = _normalize_scoring_context(
            self.scoring_context if scoring_context is None else scoring_context
        )
        active_profile = (
            candidate_context.set_profile
            if candidate_context is not None
            else self.set_profile
        )
        profile_lookup = _profile_rating_lookup(
            profile=active_profile,
            card_database=card_database,
        )
        normalization = _normalization_from_data(
            ratings_data=self.ratings_data,
            config=self.config,
            profile_lookup=profile_lookup,
        )
        resolved_stage = _resolve_pre_pick_stage(
            pick_index=pick_index,
            pack_number=pack_number,
            pick_number=pick_number,
            global_pick_index=global_pick_index,
            estimated_remaining_picks=estimated_remaining_picks,
            scoring_context=candidate_context,
        )
        if resolved_stage is not None:
            resolved_pick_index = resolved_stage.global_pick_index
        else:
            resolved_pick_index = _pick_index(
                pool_grp_ids=pool_grp_ids,
                pick_index=(
                    pick_index if pick_index is not None else global_pick_index
                ),
            )
        commitment = _color_commitment(
            pool_grp_ids=pool_grp_ids,
            pick_index=resolved_pick_index,
            card_database=card_database,
            ratings_data=self.ratings_data,
            config=self.config,
            profile_lookup=profile_lookup,
        )
        splash_state = infer_splash_state(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
            ratings_data=self.ratings_data,
            base_pair=(
                commitment.inferred_pair
                if commitment.level > 0.0
                else None
            ),
            enabled=self.splash_enabled,
        )
        active_context = candidate_context
        if active_context is None and resolved_stage is not None:
            active_context = _build_pick_scoring_context(
                pool_grp_ids=pool_grp_ids,
                card_database=card_database,
                ratings_data=self.ratings_data,
                set_profile=active_profile,
                stage=resolved_stage,
                likely_pair=commitment.inferred_pair,
            )
        if active_context is not None:
            role_ledger = active_context.role_ledger
        elif resolved_stage is None:
            role_ledger = None
        else:
            role_ledger = project_pool_role_ledger(
                pool_before_pick=pool_grp_ids,
                pack_number=resolved_stage.pack_number,
                pick_number=resolved_stage.pick_number,
                global_pick_index=resolved_stage.global_pick_index,
                estimated_remaining_picks=resolved_stage.estimated_remaining_picks,
                card_database=card_database,
                ratings_data=self.ratings_data,
                set_profile=active_profile,
                likely_pair=commitment.inferred_pair,
            )
        require_material_rate_margin = _is_empirical_profile(
            profile=active_profile,
        )
        best_on_color_score = self._best_on_color_score(
            offered_grp_ids=offered_grp_ids,
            card_database=card_database,
            commitment=commitment,
            profile=active_profile,
            normalization=normalization,
            profile_lookup=profile_lookup,
        )
        scored_cards = tuple(
            self._score_card(
                grp_id=grp_id,
                original_index=index,
                card_database=card_database,
                commitment=commitment,
                splash_state=splash_state,
                best_on_color_score=best_on_color_score,
                scoring_context=active_context,
                contextual_adjustments_enabled=self.contextual_adjustments_enabled,
                profile=active_profile,
                normalization=normalization,
                profile_lookup=profile_lookup,
            )
            for index, grp_id in enumerate(offered_grp_ids)
        )
        sorted_cards = _score_sorted_cards(
            cards=scored_cards,
            commitment=commitment,
            ratings_data=self.ratings_data,
            profile=active_profile,
            offered_count=len(offered_grp_ids),
            config=self.config,
            require_material_rate_margin=require_material_rate_margin,
        )
        return ScoredPack(
            cards=sorted_cards,
            normalization=normalization,
            source_summary=_source_summary(cards=sorted_cards),
            commitment=commitment,
            splash_state=splash_state,
            role_ledger=role_ledger,
            scoring_context=active_context,
        )

    def _best_on_color_score(
        self,
        *,
        offered_grp_ids: tuple[int, ...],
        card_database: CardDatabase,
        commitment: ColorCommitment,
        profile: SetProfile | None,
        normalization: ScoreNormalization,
        profile_lookup: _ProfileRatingLookup,
    ) -> float | None:
        pair = commitment.inferred_pair
        if pair is None:
            return None

        scores: list[float] = []
        for grp_id in offered_grp_ids:
            card = card_database.lookup(grp_id=grp_id)
            if card.unknown or _is_freely_available_basic_land(card=card):
                continue
            if not card_is_castable_in_pair(card=card, base_pair=pair):
                continue

            rating = _rating_for(
                ratings_data=self.ratings_data,
                grp_id=grp_id,
                config=self.config,
                commitment=commitment,
                profile=profile,
                profile_lookup=profile_lookup,
            )
            scores.append(
                _normalized_score(
                    adjusted_rating=_effective_base_rating_for(
                        rating=rating,
                        ratings_data=self.ratings_data,
                        grp_id=grp_id,
                        config=self.config,
                        commitment=commitment,
                        profile=profile,
                    ),
                    normalization=normalization,
                )
            )

        return max(scores, default=None)

    def _score_card(
        self,
        *,
        grp_id: int,
        original_index: int,
        card_database: CardDatabase,
        commitment: ColorCommitment,
        splash_state: SplashState,
        best_on_color_score: float | None,
        scoring_context: PickScoringContext | None,
        contextual_adjustments_enabled: bool,
        profile: SetProfile | None,
        normalization: ScoreNormalization,
        profile_lookup: _ProfileRatingLookup,
    ) -> ScoredCard:
        card = card_database.lookup(grp_id=grp_id)
        freely_available_basic = _is_freely_available_basic_land(card=card)
        rating = _rating_for(
            ratings_data=self.ratings_data,
            grp_id=grp_id,
            config=self.config,
            profile=profile,
            commitment=commitment,
            profile_lookup=profile_lookup,
        )
        base_rating = (
            normalization.lower_rating
            if freely_available_basic
            else _effective_base_rating_for(
                rating=rating,
                ratings_data=self.ratings_data,
                grp_id=grp_id,
                config=self.config,
                commitment=commitment,
                profile=profile,
            )
        )
        base_score = _normalized_score(
            adjusted_rating=base_rating,
            normalization=normalization,
        )
        global_rating = _rating_for(
            ratings_data=self.ratings_data,
            grp_id=grp_id,
            config=self.config,
            profile_lookup=profile_lookup,
        )
        splash = assess_splash_card(
            card=card,
            grade=global_rating.letter_grade,
            base_score=base_score,
            best_on_color_score=best_on_color_score,
            locked=commitment.locked,
            state=splash_state,
        )
        color_fit = splash.classification
        color_factor = _color_factor(
            color_fit=color_fit,
            commitment=commitment,
            config=self.config,
        )
        if contextual_adjustments_enabled:
            contextual_breakdown, contextual_evidence = _contextual_score_for_card(
                card=card,
                scoring_context=scoring_context,
            )
        else:
            contextual_breakdown = ContextualScoreBreakdown()
            contextual_evidence = ()
        contextual_adjustment = contextual_breakdown.aggregate
        raw_score = _clamp(
            value=(base_score * color_factor) + contextual_adjustment,
            lower=0.0,
            upper=100.0,
        )
        if freely_available_basic:
            pair_tiebreaker_pair = None
            pair_tiebreaker_win_rate = None
            pair_tiebreaker_weight = None
        else:
            (
                pair_tiebreaker_pair,
                pair_tiebreaker_win_rate,
                pair_tiebreaker_weight,
            ) = _pair_tiebreaker_for_card(
                card=card,
                base_rating=base_rating,
                commitment=commitment,
                ratings_data=self.ratings_data,
                config=self.config,
                profile=profile,
            )
        return ScoredCard(
            card=card,
            rating=rating,
            original_index=original_index,
            base_rating=base_rating,
            base_score=base_score,
            color_factor=color_factor,
            adjusted_rating=base_rating * color_factor,
            raw_score=raw_score,
            score=_integer_score(raw_score=raw_score),
            source_label=(
                "Basic"
                if freely_available_basic
                else _source_label(rating=rating)
            ),
            color_fit=color_fit,
            pair_tiebreaker_pair=pair_tiebreaker_pair,
            pair_tiebreaker_win_rate=pair_tiebreaker_win_rate,
            pair_tiebreaker_weight=pair_tiebreaker_weight,
            score_sort_index=original_index,
            splash=splash,
            freely_available_basic=freely_available_basic,
            contextual_breakdown=contextual_breakdown,
            contextual_evidence=contextual_evidence,
            contextual_pair=(
                None
                if scoring_context is None
                else scoring_context.role_ledger.likely_pair
            ),
            contextual_theme=_contextual_theme(scoring_context=scoring_context),
            contextual_profile_maturity=_contextual_profile_maturity(
                scoring_context=scoring_context,
            ),
            contextual_profile_confidence=_contextual_profile_confidence(
                scoring_context=scoring_context,
            ),
        )


def _target_names(assignment: RoleAssignment) -> tuple[str, ...]:
    role_name = assignment.role.value
    if assignment.removal is None:
        return (role_name,)
    return (role_name, f"removal:{assignment.removal.kind}")


def _contextual_score_for_card(
    *,
    card: CardInfo,
    scoring_context: PickScoringContext | None,
) -> tuple[ContextualScoreBreakdown, tuple[str, ...]]:
    """Compute bounded contextual terms from one validated pre-pick context."""

    if (
        scoring_context is None
        or card.unknown
        or _is_freely_available_basic_land(card=card)
    ):
        return ContextualScoreBreakdown(), ()

    profile = scoring_context.set_profile
    ledger = scoring_context.role_ledger
    if ledger.likely_pair is None or ledger.profile_source == "generic":
        return ContextualScoreBreakdown(), ()

    evidence_weight = _profile_evidence_weight(profile=profile)
    stage_scale = _stage_scale(stage=scoring_context.stage)
    resolution = resolve_card_roles(
        card,
        profile=profile.role_profile,
    )
    if resolution.source != "compiled_profile":
        return ContextualScoreBreakdown(), ()
    assignments = resolution.assignments
    if not assignments:
        return ContextualScoreBreakdown(), ()

    target_map = ledger.target_coverage_map
    role_candidates: list[tuple[float, str, RoleAssignment]] = []
    redundancy_candidates: list[tuple[float, str, RoleAssignment]] = []
    for assignment in assignments:
        for target_name in _target_names(assignment):
            target = target_map.get(target_name)
            if target is None or target.preferred_minimum <= 0:
                continue
            pressure = _target_pressure(target=target)
            if target.deficit > 0:
                role_candidates.append(
                    (
                        pressure * target.confidence * assignment.confidence,
                        target.name,
                        assignment,
                    )
                )
            if target.count > 0:
                redundancy_candidates.append(
                    (
                        max(0.0, 1.0 - target.diminishing_returns)
                        * target.confidence
                        * assignment.confidence,
                        target.name,
                        assignment,
                    )
                )

    role_pressure = max((item[0] for item in role_candidates), default=0.0)
    role_term = _bounded_term(
        value=MAX_ROLE_TERM * role_pressure * stage_scale * evidence_weight,
        lower=0.0,
        upper=MAX_ROLE_TERM,
    )

    urgency_term = _bounded_term(
        value=(
            MAX_URGENCY_TERM
            * ledger.urgency
            * role_pressure
            * stage_scale
            * evidence_weight
        ),
        lower=0.0,
        upper=MAX_URGENCY_TERM,
    )

    synergy_term, synergy_evidence = _semantic_synergy_term(
        assignments=assignments,
        ledger=ledger,
        stage_scale=stage_scale,
        evidence_weight=evidence_weight,
    )
    redundancy_term, redundancy_evidence = _redundancy_term(
        candidates=redundancy_candidates,
        stage_scale=stage_scale,
        evidence_weight=evidence_weight,
    )
    unsupported_term, unsupported_evidence = _unsupported_payoff_term(
        assignments=assignments,
        ledger=ledger,
        stage_scale=stage_scale,
        evidence_weight=evidence_weight,
    )
    fixing_term, fixing_evidence = _fixing_term(
        assignments=assignments,
        target_map=target_map,
        ledger=ledger,
        stage_scale=stage_scale,
        evidence_weight=evidence_weight,
    )

    breakdown = ContextualScoreBreakdown(
        role=role_term,
        urgency=urgency_term,
        synergy=synergy_term,
        redundancy=redundancy_term,
        unsupported_payoff=unsupported_term,
        fixing=fixing_term,
    )
    evidence: list[str] = []
    if role_term > 0.01:
        target_name = max(
            role_candidates,
            key=lambda item: (item[0], item[1]),
            default=(0.0, "role", assignments[0]),
        )[1]
        evidence.append(
            f"fills {target_name} deficit "
            f"({target_map[target_name].count:g}/{target_map[target_name].preferred_minimum:g})"
        )
    if urgency_term > 0.01:
        urgency_label = (
            "late missing-role urgency"
            if scoring_context.stage.global_pick_index > EXPECTED_TOTAL_PICKS // 2
            else "emerging role urgency"
        )
        evidence.append(f"{urgency_label} {ledger.urgency:.2f}")
    if synergy_term > 0.01:
        evidence.extend(synergy_evidence)
    if redundancy_term < -0.01:
        evidence.extend(redundancy_evidence)
    if unsupported_term < -0.01:
        evidence.extend(unsupported_evidence)
    if fixing_term > 0.01:
        evidence.extend(fixing_evidence)
    return breakdown, tuple(evidence)


def _profile_evidence_weight(*, profile: SetProfile) -> float:
    maturity_weight = {
        "mature": 1.0,
        "early": 0.8,
        "semantic-only": 0.9,
        "metadata-only": 0.45,
        "generic": 0.0,
    }.get(profile.maturity.value, 0.0)
    return _clamp(value=profile.confidence * maturity_weight, lower=0.0, upper=1.0)


def _stage_scale(*, stage: LedgerStage) -> float:
    progress = _clamp(
        value=(stage.global_pick_index - 1) / max(1, EXPECTED_TOTAL_PICKS - 1),
        lower=0.0,
        upper=1.0,
    )
    return _clamp(value=0.15 + (0.85 * progress), lower=0.0, upper=1.0)


def _target_pressure(*, target: TargetCoverage) -> float:
    if target.preferred_minimum <= 0:
        return 0.0
    return _clamp(
        value=target.deficit / target.preferred_minimum,
        lower=0.0,
        upper=1.0,
    )


def _bounded_term(*, value: float, lower: float, upper: float) -> float:
    return float(f"{_clamp(value=value, lower=lower, upper=upper):.6f}")


def _semantic_synergy_term(
    *,
    assignments: tuple[RoleAssignment, ...],
    ledger: PoolRoleLedger,
    stage_scale: float,
    evidence_weight: float,
) -> tuple[float, tuple[str, ...]]:
    enablers = dict(ledger.enabler_counts)
    payoffs = dict(ledger.payoff_counts)
    candidates: list[tuple[float, str]] = []
    for assignment in assignments:
        for package, (enabler_roles, payoff_roles) in PACKAGE_ROLES.items():
            if assignment.role in payoff_roles and enablers.get(package, 0) > 0:
                other_count = enablers[package]
            elif assignment.role in enabler_roles and payoffs.get(package, 0) > 0:
                other_count = payoffs[package]
            else:
                continue
            total_count = enablers.get(package, 0) + payoffs.get(package, 0)
            support = _clamp(
                value=other_count / max(1, total_count),
                lower=0.0,
                upper=1.0,
            )
            value = (
                MAX_SYNERGY_TERM
                * support
                * assignment.confidence
                * stage_scale
                * evidence_weight
            )
            candidates.append(
                (
                    value,
                    f"supports {package} semantic package "
                    f"({enablers.get(package, 0)} enabler(s), "
                    f"{payoffs.get(package, 0)} payoff(s))",
                )
            )
    if not candidates:
        return 0.0, ()
    value, evidence = max(candidates, key=lambda item: (item[0], item[1]))
    return _bounded_term(value=value, lower=0.0, upper=MAX_SYNERGY_TERM), (evidence,)


def _redundancy_term(
    *,
    candidates: list[tuple[float, str, RoleAssignment]],
    stage_scale: float,
    evidence_weight: float,
) -> tuple[float, tuple[str, ...]]:
    if not candidates:
        return 0.0, ()
    pressure, target_name, assignment = max(candidates, key=lambda item: item[0])
    value = -(
        MAX_REDUNDANCY_TERM
        * _clamp(value=pressure, lower=0.0, upper=1.0)
        * stage_scale
        * evidence_weight
    )
    return _bounded_term(
        value=value,
        lower=-MAX_REDUNDANCY_TERM,
        upper=0.0,
    ), (f"redundancy pressure for {target_name}",)


def _unsupported_payoff_term(
    *,
    assignments: tuple[RoleAssignment, ...],
    ledger: PoolRoleLedger,
    stage_scale: float,
    evidence_weight: float,
) -> tuple[float, tuple[str, ...]]:
    unsupported = dict(ledger.unsupported_payoff_counts)
    payoffs = dict(ledger.payoff_counts)
    enablers = dict(ledger.enabler_counts)
    candidate_roles = frozenset(assignment.role for assignment in assignments)
    candidate_enabled_packages = {
        package
        for package, (enabler_roles, _) in PACKAGE_ROLES.items()
        if candidate_roles.intersection(enabler_roles)
    }
    candidates: list[tuple[float, str, RoleAssignment]] = []
    for assignment in assignments:
        package = PAYOFF_PACKAGES.get(assignment.role)
        if (
            package is None
            or unsupported.get(package, 0) <= 0
            or enablers.get(package, 0) > 0
            or package in candidate_enabled_packages
        ):
            continue
        pressure = _clamp(
            value=unsupported[package] / max(1, payoffs.get(package, 0)),
            lower=0.0,
            upper=1.0,
        )
        candidates.append(
            (
                pressure,
                f"unsupported {package} payoff (no enabler)",
                assignment,
            )
        )
    if not candidates:
        return 0.0, ()
    pressure, evidence, assignment = max(candidates, key=lambda item: item[0])
    value = -(
        MAX_UNSUPPORTED_PAYOFF_TERM
        * pressure
        * assignment.confidence
        * stage_scale
        * evidence_weight
    )
    return _bounded_term(
        value=value,
        lower=-MAX_UNSUPPORTED_PAYOFF_TERM,
        upper=0.0,
    ), (evidence,)


def _fixing_term(
    *,
    assignments: tuple[RoleAssignment, ...],
    target_map: dict[str, TargetCoverage],
    ledger: PoolRoleLedger,
    stage_scale: float,
    evidence_weight: float,
) -> tuple[float, tuple[str, ...]]:
    candidates: list[tuple[float, str, RoleAssignment]] = []
    for assignment in assignments:
        if assignment.role not in FIXING_ROLES:
            continue
        target = target_map.get(assignment.role.value)
        if target is None:
            need = _clamp(
                value=1.0 - (ledger.fixing_count / 2.0),
                lower=0.0,
                upper=1.0,
            )
            target_confidence = 1.0
        else:
            need = _target_pressure(target=target)
            target_confidence = target.confidence
        need *= target_confidence
        if need > 0:
            candidates.append(
                (
                    need,
                    f"fixing need ({ledger.fixing_count} source(s))",
                    assignment,
                )
            )
    if not candidates:
        return 0.0, ()
    need, evidence, assignment = max(candidates, key=lambda item: item[0])
    value = (
        MAX_FIXING_TERM
        * need
        * assignment.confidence
        * stage_scale
        * evidence_weight
    )
    return _bounded_term(value=value, lower=0.0, upper=MAX_FIXING_TERM), (evidence,)


def _contextual_theme(*, scoring_context: PickScoringContext | None) -> str | None:
    if scoring_context is None or scoring_context.role_ledger.likely_pair is None:
        return None
    pair_profile = scoring_context.set_profile.pair(
        scoring_context.role_ledger.likely_pair
    )
    return None if pair_profile is None else pair_profile.theme


def _contextual_profile_maturity(
    *,
    scoring_context: PickScoringContext | None,
) -> str | None:
    return None if scoring_context is None else scoring_context.set_profile.maturity.value


def _contextual_profile_confidence(
    *,
    scoring_context: PickScoringContext | None,
) -> float | None:
    return None if scoring_context is None else scoring_context.set_profile.confidence


def score_pack(
    *,
    offered_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    config: PickEngineConfig = PICK_ENGINE,
    pool_grp_ids: tuple[int, ...] = (),
    pick_index: int | None = None,
    splash_enabled: bool = SPLASH.enabled_by_default,
    contextual_adjustments_enabled: bool = True,
    set_profile: SetProfile | None = None,
    scoring_context: PickScoringContext | None = None,
    pack_number: int | None = None,
    pick_number: int | None = None,
    global_pick_index: int | None = None,
    estimated_remaining_picks: int | None = None,
) -> ScoredPack:
    """Convenience wrapper for callers that do not keep an engine instance.
    The reusable PickEngine class retains configuration between pack scores.
    """
    return PickEngine(
        ratings_data=ratings_data,
        config=config,
        splash_enabled=splash_enabled,
        contextual_adjustments_enabled=contextual_adjustments_enabled,
        set_profile=set_profile,
        scoring_context=scoring_context,
    ).score_pack(
        offered_grp_ids=offered_grp_ids,
        card_database=card_database,
        pool_grp_ids=pool_grp_ids,
        pick_index=pick_index,
        pack_number=pack_number,
        pick_number=pick_number,
        global_pick_index=global_pick_index,
        estimated_remaining_picks=estimated_remaining_picks,
    )


def _pick_index(
    *,
    pool_grp_ids: tuple[int, ...],
    pick_index: int | None,
) -> int:
    if pick_index is not None:
        return max(1, pick_index)

    return len(pool_grp_ids) + 1


def build_pick_scoring_context(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    config: PickEngineConfig = PICK_ENGINE,
    set_profile: SetProfile | None = None,
    pick_index: int | None = None,
    pack_number: int | None = None,
    pick_number: int | None = None,
    global_pick_index: int | None = None,
    estimated_remaining_picks: int | None = None,
    scoring_context: PickScoringContext | None = None,
) -> PickScoringContext | None:
    """Build validated pre-pick context from authoritative inputs."""

    scoring_context = _normalize_scoring_context(scoring_context)
    set_profile = _normalize_scoring_profile(set_profile)
    resolved_stage = _resolve_pre_pick_stage(
        pick_index=pick_index,
        pack_number=pack_number,
        pick_number=pick_number,
        global_pick_index=global_pick_index,
        estimated_remaining_picks=estimated_remaining_picks,
        scoring_context=scoring_context,
    )
    if scoring_context is not None:
        return scoring_context
    if set_profile is None or resolved_stage is None:
        return None
    profile_lookup = _profile_rating_lookup(
        profile=set_profile,
        card_database=card_database,
    )
    inferred_pair = _inferred_pair(
        weights=_pool_color_weights(
            pool_grp_ids=pool_grp_ids,
            card_database=card_database,
            ratings_data=ratings_data,
            config=config,
            profile_lookup=profile_lookup,
        ),
        config=config,
    )
    return _build_pick_scoring_context(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        ratings_data=ratings_data,
        set_profile=set_profile,
        stage=resolved_stage,
        likely_pair=inferred_pair,
    )


def _build_pick_scoring_context(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None,
    set_profile: SetProfile | None,
    stage: LedgerStage,
    likely_pair: str | None,
) -> PickScoringContext | None:
    set_profile = _normalize_scoring_profile(set_profile)
    if set_profile is None:
        return None
    role_ledger = project_pool_role_ledger(
        pool_before_pick=pool_grp_ids,
        pack_number=stage.pack_number,
        pick_number=stage.pick_number,
        global_pick_index=stage.global_pick_index,
        estimated_remaining_picks=stage.estimated_remaining_picks,
        card_database=card_database,
        ratings_data=ratings_data,
        set_profile=set_profile,
        likely_pair=likely_pair,
    )
    return PickScoringContext(set_profile=set_profile, role_ledger=role_ledger)


def _resolve_pre_pick_stage(
    *,
    pick_index: int | None,
    pack_number: int | None,
    pick_number: int | None,
    global_pick_index: int | None,
    estimated_remaining_picks: int | None,
    scoring_context: PickScoringContext | None,
) -> LedgerStage | None:
    explicit_indices = tuple(
        index
        for index in (pick_index, global_pick_index)
        if index is not None
    )
    if len(explicit_indices) == 2 and explicit_indices[0] != explicit_indices[1]:
        raise ValueError("pick_index and global_pick_index conflict.")
    if scoring_context is not None:
        context_stage = scoring_context.stage
        if (
            explicit_indices
            and explicit_indices[0] != context_stage.global_pick_index
        ):
            raise ValueError(
                "Explicit pick_index/global_pick_index conflicts with "
                "PickScoringContext.stage.global_pick_index."
            )
        for coordinate_name, explicit_value, context_value in (
            ("pack_number", pack_number, context_stage.pack_number),
            ("pick_number", pick_number, context_stage.pick_number),
            (
                "estimated_remaining_picks",
                estimated_remaining_picks,
                context_stage.estimated_remaining_picks,
            ),
        ):
            if explicit_value is not None and explicit_value != context_value:
                raise ValueError(
                    f"Explicit {coordinate_name} conflicts with "
                    f"PickScoringContext.stage.{coordinate_name}."
                )
        return context_stage
    explicit = (
        pack_number,
        pick_number,
        global_pick_index,
        estimated_remaining_picks,
    )
    if all(value is None for value in explicit):
        if pick_index is None:
            return None
        pack_number = (pick_index - 1) // EXPECTED_PICKS_PER_PACK
        pick_number = (pick_index - 1) % EXPECTED_PICKS_PER_PACK
        global_pick_index = pick_index
        estimated_remaining_picks = max(0, EXPECTED_TOTAL_PICKS - pick_index)
    elif any(value is None for value in explicit):
        raise ValueError(
            "Ledger scoring requires pack, pick, global index, and remaining "
            "picks together."
        )
    if pick_index is not None and global_pick_index != pick_index:
        raise ValueError("Ledger pick_index and global_pick_index must agree.")
    return LedgerStage(
        pack_number=pack_number,
        pick_number=pick_number,
        global_pick_index=global_pick_index,
        estimated_remaining_picks=estimated_remaining_picks,
    )


def _color_commitment(
    *,
    pool_grp_ids: tuple[int, ...],
    pick_index: int,
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None,
    config: PickEngineConfig,
    profile_lookup: _ProfileRatingLookup | None = None,
) -> ColorCommitment:
    weights = _pool_color_weights(
        pool_grp_ids=pool_grp_ids,
        card_database=card_database,
        ratings_data=ratings_data,
        config=config,
        profile_lookup=profile_lookup,
    )
    inferred_pair = _inferred_pair(weights=weights, config=config)
    level = _commitment_level(pick_index=pick_index, config=config)
    if inferred_pair is None:
        level = 0.0

    return ColorCommitment(
        pick_index=pick_index,
        pool_size=len(pool_grp_ids),
        color_weights=tuple((color, weights[color]) for color in weights),
        inferred_pair=inferred_pair,
        level=level,
    )


def _pool_color_weights(
    *,
    pool_grp_ids: tuple[int, ...],
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None,
    config: PickEngineConfig,
    profile_lookup: _ProfileRatingLookup | None = None,
) -> dict[str, float]:
    weights = _empty_color_weights()
    for grp_id in pool_grp_ids:
        card = card_database.lookup(grp_id=grp_id)
        if card.unknown or not card.colors:
            continue

        rating = _rating_for(
            ratings_data=ratings_data,
            grp_id=grp_id,
            config=config,
            profile_lookup=profile_lookup,
        )
        base_rating = _base_rating(rating=rating, config=config)
        weight = _pool_card_weight(base_rating=base_rating, config=config)
        for color in card.colors:
            if color in weights:
                weights[color] += weight

    return weights


def _empty_color_weights() -> dict[str, float]:
    colors: dict[str, float] = {}
    for pair in COLOR_PAIRS:
        for color in pair:
            colors.setdefault(color, 0.0)

    return colors


def _pool_card_weight(*, base_rating: float, config: PickEngineConfig) -> float:
    lower = min(config.pool_weight_minimum, config.pool_weight_maximum)
    upper = max(config.pool_weight_minimum, config.pool_weight_maximum)
    rating_delta = base_rating - config.neutral_prior_win_rate
    weight = config.pool_weight_baseline + (rating_delta * config.pool_weight_rating_scale)
    return _clamp(value=weight, lower=lower, upper=upper)


def _inferred_pair(
    *,
    weights: dict[str, float],
    config: PickEngineConfig,
) -> str | None:
    positive_colors = tuple(
        color for color, weight in weights.items() if weight > config.pool_weight_epsilon
    )
    if len(positive_colors) < config.minimum_pair_colors:
        return None

    return max(COLOR_PAIRS, key=lambda pair: _pair_weight(pair=pair, weights=weights))


def _pair_weight(*, pair: str, weights: dict[str, float]) -> float:
    return sum(weights.get(color, 0.0) for color in pair)


def _commitment_level(*, pick_index: int, config: PickEngineConfig) -> float:
    if pick_index <= config.open_pick_count:
        return 0.0

    ramp_start = max(config.commitment_start_pick, config.open_pick_count + 1)
    if pick_index < ramp_start:
        return 0.0

    if pick_index >= config.locked_pick_index:
        return 1.0

    ramp_span = config.locked_pick_index - ramp_start + 1
    if ramp_span <= 0:
        return 1.0

    return _clamp(
        value=(pick_index - ramp_start + 1) / ramp_span,
        lower=0.0,
        upper=1.0,
    )


def _color_factor(
    *,
    color_fit: str,
    commitment: ColorCommitment,
    config: PickEngineConfig,
) -> float:
    if commitment.level <= 0.0 or color_fit in {"open", "colorless", "unknown"}:
        return 1.0

    if color_fit == "on-color":
        factor = 1.0 + (commitment.level * (config.on_color_bonus_multiplier - 1.0))
        return max(0.0, factor)

    if color_fit == "off-color":
        penalty = 1.0 - config.off_color_penalty_multiplier
        factor = 1.0 - (commitment.level * penalty)
        return max(0.0, factor)

    if color_fit == "splash-ready":
        penalty = 1.0 - SPLASH.ready_score_multiplier
        factor = 1.0 - (commitment.level * penalty)
        return max(0.0, factor)

    if color_fit == "splash-speculative":
        penalty = 1.0 - SPLASH.speculative_score_multiplier
        factor = 1.0 - (commitment.level * penalty)
        return max(0.0, factor)

    if color_fit == "splash-fixer":
        bonus = SPLASH.fixer_score_multiplier - 1.0
        factor = 1.0 + (commitment.level * bonus)
        return max(0.0, factor)

    return 1.0


def _score_sorted_cards(
    *,
    cards: tuple[ScoredCard, ...],
    commitment: ColorCommitment,
    ratings_data: SeventeenLandsData | None,
    profile: SetProfile | None,
    offered_count: int,
    config: PickEngineConfig,
    require_material_rate_margin: bool,
) -> tuple[ScoredCard, ...]:
    base_sorted = tuple(sorted(cards, key=_scored_card_base_sort_key))
    if not _early_pair_tiebreaker_enabled(
        commitment=commitment,
        ratings_data=ratings_data,
        profile=profile,
        offered_count=offered_count,
        config=config,
    ):
        return _with_score_sort_indexes(cards=base_sorted)

    sorted_cards = _apply_early_pair_tiebreaker(
        cards=base_sorted,
        config=config,
        require_material_rate_margin=require_material_rate_margin,
    )
    return _with_score_sort_indexes(cards=sorted_cards)

def _early_pair_tiebreaker_enabled(
    *,
    commitment: ColorCommitment,
    ratings_data: SeventeenLandsData | None,
    profile: SetProfile | None,
    offered_count: int,
    config: PickEngineConfig,
) -> bool:
    if not _pair_performance_available(
        ratings_data=ratings_data,
        profile=profile,
    ):
        return False

    if commitment.level > 0.0 or commitment.pick_index > config.open_pick_count:
        return False

    if config.early_pair_tiebreaker_score_threshold <= 0.0:
        return False

    if config.early_pair_tiebreaker_max_offered_cards <= 0:
        return False

    return offered_count <= config.early_pair_tiebreaker_max_offered_cards


def _apply_early_pair_tiebreaker(
    *,
    cards: tuple[ScoredCard, ...],
    config: PickEngineConfig,
    require_material_rate_margin: bool,
) -> tuple[ScoredCard, ...]:
    sorted_cards: list[ScoredCard] = []
    group: list[ScoredCard] = []
    group_top_score = 0.0
    for card in cards:
        if not group:
            group = [card]
            group_top_score = card.raw_score
            continue

        score_delta = group_top_score - card.raw_score
        if score_delta <= config.early_pair_tiebreaker_score_threshold:
            group.append(card)
            continue

        sorted_cards.extend(
            _sort_early_pair_tiebreaker_group(
                group=group,
                config=config,
                require_material_rate_margin=require_material_rate_margin,
            )
        )
        group = [card]
        group_top_score = card.raw_score

    if group:
        sorted_cards.extend(
            _sort_early_pair_tiebreaker_group(
                group=group,
                config=config,
                require_material_rate_margin=require_material_rate_margin,
            )
        )

    return tuple(sorted_cards)


def _sort_early_pair_tiebreaker_group(
    *,
    group: list[ScoredCard],
    config: PickEngineConfig,
    require_material_rate_margin: bool,
) -> tuple[ScoredCard, ...]:
    return tuple(
        sorted(
            group,
            key=cmp_to_key(
                lambda left, right: _compare_early_pair_tiebreaker(
                    left=left,
                    right=right,
                    config=config,
                    require_material_rate_margin=require_material_rate_margin,
                ),
            ),
        )
    )


def _compare_early_pair_tiebreaker(
    *,
    left: ScoredCard,
    right: ScoredCard,
    config: PickEngineConfig,
    require_material_rate_margin: bool,
) -> int:
    left_win_rate = left.pair_tiebreaker_win_rate
    right_win_rate = right.pair_tiebreaker_win_rate
    left_weight = left.pair_tiebreaker_weight
    right_weight = right.pair_tiebreaker_weight
    if (
        left_win_rate is not None
        and right_win_rate is not None
        and left_weight is not None
        and right_weight is not None
        and abs(left.raw_score - right.raw_score)
        <= config.early_pair_tiebreaker_score_threshold
        and abs(left_weight - right_weight)
        <= config.early_pair_tiebreaker_pair_weight_threshold
        and (
            abs(left_win_rate - right_win_rate) > CLOSE_WIN_RATE_THRESHOLD
            if require_material_rate_margin
            else left_win_rate != right_win_rate
        )
    ):
        return -1 if left_win_rate > right_win_rate else 1

    return _compare_base_scored_cards(left=left, right=right)


def _compare_base_scored_cards(*, left: ScoredCard, right: ScoredCard) -> int:
    left_key = _scored_card_base_sort_key(left)
    right_key = _scored_card_base_sort_key(right)
    if left_key < right_key:
        return -1

    if left_key > right_key:
        return 1

    return 0


def _with_score_sort_indexes(*, cards: tuple[ScoredCard, ...]) -> tuple[ScoredCard, ...]:
    return tuple(
        replace(card, score_sort_index=index)
        for index, card in enumerate(cards)
    )


def _pair_tiebreaker_for_card(
    *,
    card: CardInfo,
    base_rating: float,
    commitment: ColorCommitment,
    ratings_data: SeventeenLandsData | None,
    config: PickEngineConfig,
    profile: SetProfile | None,
) -> tuple[str | None, float | None, float | None]:
    if not _pair_performance_available(
        ratings_data=ratings_data,
        profile=profile,
    ):
        return (None, None, None)

    colors = _recognized_card_colors(card=card)
    if not colors:
        return (None, None, None)

    compatible_pairs = _compatible_pairs_for_colors(colors=colors)
    if not compatible_pairs:
        return (None, None, None)

    weights = _candidate_pair_weights(
        colors=colors,
        base_rating=base_rating,
        commitment=commitment,
        config=config,
    )
    pair = _best_tiebreaker_pair(
        pairs=compatible_pairs,
        weights=weights,
        ratings_data=ratings_data,
        config=config,
        profile=profile,
    )
    if pair is None:
        return (None, None, None)

    return (
        pair,
        _pair_performance_rate(
            pair=pair,
            ratings_data=ratings_data,
            profile=profile,
            config=config,
        ),
        _pair_weight(pair=pair, weights=weights),
    )


def _recognized_card_colors(*, card: CardInfo) -> tuple[str, ...]:
    color_order = tuple(_empty_color_weights())
    colors: list[str] = []
    for color in card.colors:
        if color in color_order and color not in colors:
            colors.append(color)

    return tuple(colors)


def _compatible_pairs_for_colors(*, colors: tuple[str, ...]) -> tuple[str, ...]:
    color_set = set(colors)
    return tuple(
        pair for pair in COLOR_PAIRS if color_set.issubset(set(pair))
    )


def _candidate_pair_weights(
    *,
    colors: tuple[str, ...],
    base_rating: float,
    commitment: ColorCommitment,
    config: PickEngineConfig,
) -> dict[str, float]:
    weights = dict(commitment.color_weights)
    weight = _pool_card_weight(base_rating=base_rating, config=config)
    for color in colors:
        weights[color] = weights.get(color, 0.0) + weight

    return weights


def _best_tiebreaker_pair(
    *,
    pairs: tuple[str, ...],
    weights: dict[str, float],
    ratings_data: SeventeenLandsData | None,
    config: PickEngineConfig,
    profile: SetProfile | None,
) -> str | None:
    if not pairs:
        return None

    max_pair_weight = max(_pair_weight(pair=pair, weights=weights) for pair in pairs)
    weight_threshold = max(0.0, config.early_pair_tiebreaker_pair_weight_threshold)
    close_pairs = tuple(
        pair
        for pair in pairs
        if max_pair_weight - _pair_weight(pair=pair, weights=weights) <= weight_threshold
    )
    return max(
        close_pairs,
        key=lambda pair: _tiebreaker_pair_sort_key(
            pair=pair,
            weights=weights,
            ratings_data=ratings_data,
            config=config,
            profile=profile,
        ),
    )


def _tiebreaker_pair_sort_key(
    *,
    pair: str,
    weights: dict[str, float],
    ratings_data: SeventeenLandsData | None,
    config: PickEngineConfig,
    profile: SetProfile | None,
) -> tuple[bool, float, float, int]:
    win_rate = _pair_performance_rate(
        pair=pair,
        ratings_data=ratings_data,
        profile=profile,
        config=config,
    )
    return (
        win_rate is not None,
        0.0 if win_rate is None else win_rate,
        _pair_weight(pair=pair, weights=weights),
        -COLOR_PAIRS.index(pair),
    )


def _sample_influence(*, samples: int | None, scale: float) -> float:
    """Return bounded evidence influence using a saturating sample curve."""

    if samples is None or samples <= 0:
        return 0.0
    return _clamp(
        value=samples / (samples + max(1.0, scale)),
        lower=0.0,
        upper=1.0,
    )


def _pair_performance_available(
    *,
    ratings_data: SeventeenLandsData | None,
    profile: SetProfile | None,
) -> bool:
    if ratings_data is not None and ratings_data.pair_win_rates:
        return True
    return _is_empirical_profile(profile=profile) and any(
        pair.performance is not None
        for pair in profile.pairs
    )


def _profile_pair_influence(
    *,
    profile: SetProfile | None,
    pair: str,
) -> float | None:
    """Return profile evidence weight, or None for legacy raw behavior."""

    if not _is_empirical_profile(profile=profile):
        return None
    samples = profile.samples
    if samples is None:
        return 0.0
    pair_samples = samples.count_for(pair)
    if pair_samples is None:
        return 0.0
    return _clamp(
        value=(
            _profile_evidence_weight(profile=profile)
            * _sample_influence(samples=samples.total, scale=PAIR_PROFILE_SAMPLE_SCALE)
            * _sample_influence(samples=pair_samples, scale=PAIR_PROFILE_SAMPLE_SCALE)
        ),
        lower=0.0,
        upper=1.0,
    )


def _shrink_rate(*, observed: float, prior: float, influence: float) -> float:
    """Blend an empirical rate toward a prior with bounded influence."""

    bounded_influence = _clamp(value=influence, lower=0.0, upper=1.0)
    return float(
        f"{_clamp(value=prior + (observed - prior) * bounded_influence,
                  lower=min(prior, observed),
                  upper=max(prior, observed)):.6f}"
    )


def _pair_performance_rate(
    *,
    pair: str,
    ratings_data: SeventeenLandsData | None,
    profile: SetProfile | None,
    config: PickEngineConfig,
) -> float | None:
    """Resolve pair performance with profile precedence and legacy fallback."""

    if _is_empirical_profile(profile=profile):
        pair_profile = profile.pair(pair) if profile is not None else None
        if pair_profile is not None and pair_profile.performance is not None:
            return pair_profile.performance.value

    if ratings_data is None:
        return None
    pair_record = ratings_data.pair_win_rates.get(pair)
    if pair_record is None or pair_record.win_rate is None:
        return None
    profile_influence = _profile_pair_influence(profile=profile, pair=pair)
    if profile_influence is None:
        return pair_record.win_rate
    influence = profile_influence * _sample_influence(
        samples=pair_record.games,
        scale=PAIR_GAME_SAMPLE_SCALE,
    )
    return _shrink_rate(
        observed=pair_record.win_rate,
        prior=config.neutral_pair_win_rate,
        influence=influence,
    )


def _normalization_from_data(
    *,
    ratings_data: SeventeenLandsData | None,
    config: PickEngineConfig,
    profile_lookup: _ProfileRatingLookup | None = None,
) -> ScoreNormalization:
    distribution = (
        profile_lookup.distribution
        if profile_lookup is not None and profile_lookup.distribution
        else _strong_rating_distribution(ratings_data=ratings_data)
    )
    lower_percentile = _percentile(
        values=distribution,
        percentile=config.normalization_lower_percentile,
    )
    upper_percentile = _percentile(
        values=distribution,
        percentile=config.normalization_upper_percentile,
    )
    neutral = config.neutral_prior_win_rate
    half_span = config.normalization_min_half_span
    if lower_percentile is not None:
        half_span = max(half_span, neutral - lower_percentile)

    if upper_percentile is not None:
        half_span = max(half_span, upper_percentile - neutral)

    return ScoreNormalization(
        lower_rating=neutral - half_span,
        upper_rating=neutral + half_span,
        neutral_rating=neutral,
    )


def _strong_rating_distribution(
    *,
    ratings_data: SeventeenLandsData | None,
) -> tuple[float, ...]:
    if ratings_data is None:
        return ()

    return tuple(
        rating.gih_win_rate
        for rating in ratings_data.ratings.values()
        if rating.gih_win_rate is not None
    )


def _rating_for(
    *,
    ratings_data: SeventeenLandsData | None,
    grp_id: int,
    config: PickEngineConfig,
    commitment: ColorCommitment | None = None,
    profile: SetProfile | None = None,
    profile_lookup: _ProfileRatingLookup | None = None,
) -> ResolvedCardRating:
    if profile_lookup is not None:
        profile_rating = profile_lookup.rating_for(grp_id=grp_id)
        if profile_rating is not None:
            return profile_rating

    if ratings_data is None:
        return _neutral_rating(grp_id=grp_id, config=config)

    if commitment is not None and commitment.locked and commitment.inferred_pair is not None:
        pair = commitment.inferred_pair
        if ratings_data.pair_card_ratings.get(pair) is not None:
            profile_influence = _profile_pair_influence(profile=profile, pair=pair)
            return ratings_data.pair_rating_for(
                grp_id=grp_id,
                pair=pair,
                allow_thin=profile_influence is not None,
            )

    return ratings_data.rating_for(grp_id=grp_id)


def _effective_base_rating_for(
    *,
    rating: ResolvedCardRating,
    ratings_data: SeventeenLandsData | None,
    grp_id: int,
    config: PickEngineConfig,
    commitment: ColorCommitment | None,
    profile: SetProfile | None,
) -> float:
    """Return the score-only base rating, shrinking legacy pair evidence."""
    if rating.metadata.source == PROFILE_RATING_SOURCE:
        return _base_rating(rating=rating, config=config)
    if (
        ratings_data is None
        or commitment is None
        or not commitment.locked
        or commitment.inferred_pair is None
        or rating.gih_win_rate is None
    ):
        return _base_rating(rating=rating, config=config)

    profile_influence = _profile_pair_influence(
        profile=profile,
        pair=commitment.inferred_pair,
    )
    if profile_influence is None:
        return _base_rating(rating=rating, config=config)

    global_rating = ratings_data.rating_for(grp_id=grp_id)
    influence = profile_influence * _sample_influence(
        samples=rating.sample_counts.games_in_hand,
        scale=PAIR_CARD_GIH_SAMPLE_SCALE,
    )
    return _shrink_rate(
        observed=rating.gih_win_rate,
        prior=_base_rating(rating=global_rating, config=config),
        influence=influence,
    )


def _neutral_rating(*, grp_id: int, config: PickEngineConfig) -> ResolvedCardRating:
    return ResolvedCardRating(
        grp_id=grp_id,
        name=f"Unknown card {grp_id}",
        color=None,
        rarity=None,
        average_last_seen_at=None,
        gih_win_rate=None,
        opening_hand_win_rate=None,
        drawn_improvement_win_rate=None,
        sample_counts=RatingSampleCounts(
            seen=0,
            picked=0,
            games_played=0,
            opening_hand=0,
            games_in_hand=0,
        ),
        letter_grade=None,
        neutral_prior_score=config.neutral_prior_score,
        metadata=RatingSourceMetadata(
            requested_format=QUICK_DRAFT_FORMAT,
            source=NEUTRAL_PRIOR_SOURCE,
            source_format=None,
            fallback_reason="ratings-unavailable",
        ),
    )


def _base_rating(*, rating: ResolvedCardRating, config: PickEngineConfig) -> float:
    if rating.gih_win_rate is not None:
        return rating.gih_win_rate

    return config.neutral_prior_win_rate + _alsa_adjustment(
        average_last_seen_at=rating.average_last_seen_at,
        config=config,
    )


def _alsa_adjustment(
    *,
    average_last_seen_at: float | None,
    config: PickEngineConfig,
) -> float:
    if average_last_seen_at is None:
        return 0.0

    early = config.alsa_early_pick
    late = config.alsa_late_pick
    if late <= early:
        return 0.0

    clamped = _clamp(value=average_last_seen_at, lower=early, upper=late)
    midpoint = (early + late) / 2.0
    half_span = (late - early) / 2.0
    return ((midpoint - clamped) / half_span) * config.alsa_adjustment_max


def _normalized_score(
    *,
    adjusted_rating: float,
    normalization: ScoreNormalization,
) -> float:
    rating_span = normalization.upper_rating - normalization.lower_rating
    if rating_span <= 0:
        return 50.0

    score = ((adjusted_rating - normalization.lower_rating) / rating_span) * 100.0
    return _clamp(value=score, lower=0.0, upper=100.0)


def _integer_score(*, raw_score: float) -> int:
    return int(math.floor(_clamp(value=raw_score, lower=0.0, upper=100.0) + 0.5))


def _source_label(*, rating: ResolvedCardRating) -> str:
    if rating.metadata.source == PROFILE_RATING_SOURCE:
        return PROFILE_SOURCE_LABEL
    if rating.metadata.source == NEUTRAL_PRIOR_SOURCE:
        return "Prior*"

    if rating.metadata.source != FORMAT_RATING_SOURCE:
        return "Unknown"

    if rating.metadata.source_format == QUICK_DRAFT_FORMAT:
        return "Quick"

    if rating.metadata.source_format == PREMIER_DRAFT_FORMAT:
        return "Premier"

    return rating.metadata.source_format or "Unknown"


def _is_freely_available_basic_land(*, card: CardInfo) -> bool:
    return card.name in STANDARD_BASIC_LAND_NAMES or any(
        type_line in STANDARD_BASIC_LAND_TYPE_LINES
        for type_line in card.types
    )


def _source_summary(*, cards: tuple[ScoredCard, ...]) -> str:
    if not cards:
        return "none"

    uses_profile = any(card.source_label == PROFILE_SOURCE_LABEL for card in cards)
    uses_quick = any(card.source_label == "Quick" for card in cards)
    uses_premier = any(card.source_label == "Premier" for card in cards)
    uses_prior = any(card.no_data for card in cards)
    uses_basic_policy = any(card.freely_available_basic for card in cards)
    parts: list[str] = []
    if uses_profile:
        parts.append("set profile")
    if uses_quick:
        parts.append("QuickDraft")

    if uses_premier:
        parts.append("Premier fallback")

    if uses_prior:
        parts.append("neutral prior")

    if uses_basic_policy:
        parts.append("basic land policy")

    return " + ".join(parts) if parts else "unknown"


def _scored_card_base_sort_key(
    card: ScoredCard,
) -> tuple[bool, int, float, float, int]:
    return (
        card.freely_available_basic,
        -card.score,
        -card.raw_score,
        -card.base_rating,
        card.original_index,
    )


def _percentile(*, values: tuple[float, ...], percentile: float) -> float | None:
    if not values:
        return None

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    clamped_percentile = _clamp(value=percentile, lower=0.0, upper=100.0)
    rank = (len(ordered) - 1) * (clamped_percentile / 100.0)
    lower_index = math.floor(rank)
    upper_index = math.ceil(rank)
    if lower_index == upper_index:
        return ordered[lower_index]

    lower_value = ordered[lower_index]
    upper_value = ordered[upper_index]
    fraction = rank - lower_index
    return lower_value + ((upper_value - lower_value) * fraction)


def _clamp(*, value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)
