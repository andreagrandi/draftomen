"""Provide deterministic session state for the desktop QML mockup.
The provider exercises the production session contract without external services.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal, TypeAlias

from draftomen.session import (
    AccountIdentity,
    ApplicationPhase,
    ApplicationStatus,
    BacktestPickResult,
    BacktestResult,
    BuildCard,
    BuildLand,
    BuildPairOption,
    BuildResult,
    CardDataState,
    CardImageState,
    CardView,
    ChangeContextualScoring,
    ChangeRanking,
    ChangeSplashPreference,
    ChooseAccount,
    ChooseRecommendation,
    ContextualEvidenceState,
    ContextualEvidenceStatus,
    DataLoadPhase,
    DismissError,
    DraftIdentity,
    FocusBuildCard,
    LiveSessionCommand,
    LiveSessionSnapshot,
    OperationKind,
    PoolCard,
    PoolState,
    ProgressState,
    RatingsState,
    SetProfileState,
    Recommendation,
    RecommendationState,
    RecentPick,
    RequestBacktest,
    RequestBuild,
    RequestRatingsDownload,
    RetryError,
    SessionError,
)

MockScenario: TypeAlias = Literal[
    "loading",
    "ready",
    "empty",
    "progress",
    "warning",
    "error",
    "build_error",
    "backtest_missing",
    "backtest_error",
]
MOCK_SCENARIOS: tuple[MockScenario, ...] = (
    "loading",
    "ready",
    "empty",
    "progress",
    "warning",
    "error",
    "build_error",
    "backtest_missing",
    "backtest_error",
)


def _card(
    *,
    grp_id: int,
    name: str,
    colors: tuple[str, ...],
    rarity: str,
    types: tuple[str, ...],
    mana_cost: str,
    mana_value: float,
) -> CardView:
    return CardView(
        grp_id=grp_id,
        name=name,
        colors=colors,
        rarity=rarity,
        types=types,
        mana_cost=mana_cost,
        mana_value=mana_value,
        image_path=None,
    )


CARDS = (
    _card(
        grp_id=91001,
        name="Outcaster Trailblazer",
        colors=("G",),
        rarity="Rare",
        types=("Creature", "Human", "Druid"),
        mana_cost="2G",
        mana_value=3.0,
    ),
    _card(
        grp_id=91002,
        name="Beastbond Outcaster",
        colors=("G",),
        rarity="Uncommon",
        types=("Creature", "Human", "Druid"),
        mana_cost="2G",
        mana_value=3.0,
    ),
    _card(
        grp_id=91003,
        name="Freestrider Lookout",
        colors=("G",),
        rarity="Rare",
        types=("Creature", "Human", "Rogue"),
        mana_cost="2G",
        mana_value=3.0,
    ),
    _card(
        grp_id=91004,
        name="Tumbleweed Rising",
        colors=("G",),
        rarity="Uncommon",
        types=("Sorcery",),
        mana_cost="1G",
        mana_value=2.0,
    ),
    _card(
        grp_id=91005,
        name="Drover Grizzly",
        colors=("G",),
        rarity="Common",
        types=("Creature", "Bear"),
        mana_cost="2G",
        mana_value=3.0,
    ),
    _card(
        grp_id=91006,
        name="Mourner's Surprise",
        colors=("B",),
        rarity="Common",
        types=("Instant",),
        mana_cost="1B",
        mana_value=2.0,
    ),
    _card(
        grp_id=91007,
        name="Jailbreak Scheme",
        colors=("U",),
        rarity="Common",
        types=("Sorcery",),
        mana_cost="U",
        mana_value=1.0,
    ),
    _card(
        grp_id=91008,
        name="Jagged Barrens",
        colors=(),
        rarity="Common",
        types=("Land",),
        mana_cost="",
        mana_value=0.0,
    ),
)


def _recommendations() -> tuple[Recommendation, ...]:
    values = (
        (
            99,
            0.636,
            1.30,
            "Quick",
            "On color",
            "A-",
            "Highest pool-aware score; the card strengthens the active colors.",
        ),
        (87, 0.616, 3.87, "Quick", "On color", "B+", "Strong rate and clean fit."),
        (69, 0.584, 1.23, "Quick", "On color", "C+", "Efficient on-color option."),
        (68, 0.582, 6.27, "Quick", "On color", "C+", "Playable curve support."),
        (67, 0.579, 7.09, "Quick", "On color", "C+", "Solid creature depth."),
        (65, 0.577, 6.33, "Quick", "Splash?", "C+", "Powerful if fixing improves."),
        (62, 0.572, 5.37, "Quick", "Off color", "C", "Strong card outside the pool."),
        (62, 0.571, 5.48, "Quick", "Any", "C", "Flexible mana source."),
    )
    return tuple(
        Recommendation(
            rank=index,
            card=card,
            score=score,
            win_rate=win_rate,
            average_last_seen_at=alsa,
            source_label=source,
            color_fit=color_fit,
            no_data=False,
            letter_grade=letter_grade,
            explanation=explanation,
        )
        for index, (
            card,
            (score, win_rate, alsa, source, color_fit, letter_grade, explanation),
        ) in enumerate(zip(CARDS, values, strict=True), start=1)
    )


def _pool() -> PoolState:
    chronological_cards = CARDS * 3
    recent_picks = tuple(
        RecentPick(
            card=card,
            image=CardImageState(
                grp_id=card.grp_id,
                phase=DataLoadPhase.UNAVAILABLE,
                message=f"No image is available for {card.name}.",
            ),
        )
        for card in reversed(chronological_cards)
    )
    pool_cards = tuple(PoolCard(card=card, quantity=3) for card in CARDS)
    return PoolState(
        cards=pool_cards,
        recent_picks=recent_picks,
        total_cards=24,
        target_cards=42,
        inferred_pair="White · Green",
        current_colors=("W", "G"),
        commitment=0.64,
        color_distribution=(("W", 0), ("U", 3), ("B", 3), ("R", 0), ("G", 15), ("C", 3)),
        mana_curve=(0, 3, 6, 12, 0, 0, 0),
        average_mana_value=17 / 7,
    )


def _build() -> BuildResult:
    spells = tuple(
        BuildCard(
            card=card,
            quantity=quantity,
            score=99 - (index * 5),
            win_rate=0.636 - (index * 0.008),
            average_last_seen_at=1.3 + index,
            letter_grade=("A-", "B+", "B", "C+", "C", "C")[index],
            source_label="Quick Draft",
            color_fit="On color",
        )
        for index, (card, quantity) in enumerate(
            zip(CARDS[:6], (1, 2, 1, 2, 3, 1), strict=True)
        )
    )
    spell_quantity_total = sum(entry.quantity for entry in spells)
    weighted_mana_total = sum(
        entry.quantity * entry.card.mana_value
        for entry in spells
        if entry.card.mana_value is not None
    )
    average_mana_value = (
        weighted_mana_total / spell_quantity_total
        if all(entry.card.mana_value is not None for entry in spells)
        else None
    )
    bench = (
        BuildCard(
            card=CARDS[6],
            quantity=2,
            score=62,
            win_rate=0.572,
            average_last_seen_at=5.37,
            letter_grade="C",
            source_label="Quick Draft",
            color_fit="Off color",
        ),
    )
    return BuildResult(
        selected_pair="WG",
        pair_options=(
            BuildPairOption(
                pair="WG",
                score=82.4,
                selected=True,
                automatic=True,
                playable_count=25,
                playable_score_sum=1682.0,
                pair_win_rate=0.574,
            ),
            BuildPairOption(
                pair="BG",
                score=74.1,
                selected=False,
                automatic=False,
                playable_count=22,
                playable_score_sum=1448.0,
                pair_win_rate=0.558,
            ),
            BuildPairOption(
                pair="GU",
                score=70.8,
                selected=False,
                automatic=False,
                playable_count=21,
                playable_score_sum=1396.0,
                pair_win_rate=0.551,
            ),
        ),
        spells=spells,
        lands=(
            BuildLand(name="Plains", quantity=7, source_colors=("W",)),
            BuildLand(name="Forest", quantity=10, source_colors=("G",)),
        ),
        bench=bench,
        deck_size=40,
        average_mana_value=average_mana_value,
        warnings=("Two flexible slots use lower-confidence ratings.",),
        spell_count=23,
        land_count=17,
        creature_count=7,
        instant_count=1,
    )


def _backtest() -> BacktestResult:
    rows = (
        BacktestPickResult(
            pack_number=0,
            pick_number=0,
            recommended=CARDS[0],
            actual=CARDS[0],
            match=True,
            skipped_reason=None,
            data_source="Quick Draft",
            pool_size=0,
            offered_count=14,
            recommended_score=99,
            recommended_win_rate=0.636,
        ),
        BacktestPickResult(
            pack_number=0,
            pick_number=1,
            recommended=CARDS[1],
            actual=CARDS[3],
            match=False,
            skipped_reason=None,
            data_source="Quick Draft",
            pool_size=1,
            offered_count=13,
            recommended_score=87,
            recommended_win_rate=0.616,
        ),
        BacktestPickResult(
            pack_number=0,
            pick_number=2,
            recommended=None,
            actual=CARDS[4],
            match=None,
            skipped_reason="Pack history was incomplete.",
            data_source=None,
            pool_size=2,
            offered_count=None,
        ),
    )
    return BacktestResult(
        ranking_mode="score",
        rows=rows,
        match_count=1,
        compared_count=2,
        skipped_count=1,
        data_sources=("Quick Draft",),
        account_id="mock-account",
        account_screen_name="MagoAnubiTest",
        draft_id="mock-otj-draft",
        set_code="OTJ",
        event_name="Quick Draft",
        completed=True,
        chosen_pick_count=42,
    )


def _ready_snapshot() -> LiveSessionSnapshot:
    account = AccountIdentity(
        account_id="mock-account",
        screen_name="MagoAnubiTest",
    )
    return LiveSessionSnapshot(
        status=ApplicationStatus(
            phase=ApplicationPhase.DRAFTING,
            message="Watching Arena · Waiting for your pick",
        ),
        accounts=(account,),
        active_account=account,
        draft=DraftIdentity(
            account_id=account.account_id,
            draft_id="mock-otj-draft",
            event_name="Quick Draft",
            set_code="OTJ",
            course_id="QuickDraft_OTJ_2024",
            pack_number=2,
            pick_number=4,
            completed=False,
        ),
        card_data=CardDataState(
            phase=DataLoadPhase.READY,
            message="Card metadata ready.",
            last_successful_update="2026-08-23T12:00:00+00:00",
        ),
        ratings=RatingsState(
            set_code="OTJ",
            phase=DataLoadPhase.READY,
            message="Quick Draft ratings ready.",
            rated_cards=14,
            total_cards=14,
            last_successful_update="2026-08-23T12:00:00+00:00",
        ),
        set_profile=SetProfileState(
            set_code="OTJ",
            event_format="QuickDraft",
            maturity="mature",
            profile_version="mock-1",
            source="mock",
            phase=DataLoadPhase.READY,
            refresh_outcome="unchanged",
            message="Mock Quick Draft set profile is current.",
        ),
        contextual_evidence=ContextualEvidenceState(
            status=ContextualEvidenceStatus.EXACT,
            source_formats=("quickdraft",),
            message="Contextual · semantic + QuickDraft evidence",
        ),
        recommendations=RecommendationState(
            ranking_mode="score",
            splash_enabled=True,
            cards=_recommendations(),
            selected_grp_id=CARDS[0].grp_id,
            source_summary="Quick Draft · 17Lands",
            confidence_summary=None,
        ),
        pool=_pool(),
        build=_build(),
        backtest=_backtest(),
    )


def _snapshot_for_scenario(*, scenario: MockScenario) -> LiveSessionSnapshot:
    ready = _ready_snapshot()
    if scenario == "ready":
        return ready
    if scenario == "loading":
        return replace(
            ready,
            status=ApplicationStatus(
                phase=ApplicationPhase.STARTING,
                message="Preparing deterministic card metadata.",
            ),
            card_data=CardDataState(
                phase=DataLoadPhase.LOADING,
                message="Loading card metadata.",
            ),
            ratings=RatingsState(
                set_code="OTJ",
                phase=DataLoadPhase.IDLE,
                message="Ratings will load after metadata.",
            ),
            recommendations=RecommendationState(),
            pool=PoolState(),
            progress=ProgressState(
                operation=OperationKind.CARD_DATA,
                message="Loading card metadata",
            ),
        )
    if scenario == "empty":
        return LiveSessionSnapshot(
            status=ApplicationStatus(
                phase=ApplicationPhase.WAITING_FOR_DRAFT,
                message="Waiting for a Quick Draft.",
            ),
            accounts=ready.accounts,
            active_account=ready.active_account,
            card_data=ready.card_data,
            ratings=RatingsState(
                phase=DataLoadPhase.IDLE,
                message="Set will be detected when the draft starts.",
            ),
        )
    if scenario == "progress":
        return replace(
            ready,
            ratings=RatingsState(
                set_code="OTJ",
                phase=DataLoadPhase.LOADING,
                message="Downloading OTJ ratings.",
                rated_cards=340,
                total_cards=1000,
                last_successful_update="2026-08-23T12:00:00+00:00",
            ),
            progress=ProgressState(
                operation=OperationKind.RATINGS,
                message="Downloading OTJ ratings",
                completed=340,
                total=1000,
            ),
        )
    if scenario == "warning":
        return replace(
            ready,
            ratings=RatingsState(
                set_code="OTJ",
                phase=DataLoadPhase.MISSING,
                message="OTJ ratings are missing; neutral-prior scores remain available.",
            ),
            build=replace(
                ready.build,
                warnings=(
                    "Ratings are incomplete; the suggested deck uses neutral priors.",
                ),
            )
            if ready.build is not None
            else None,
        )
    if scenario == "build_error":
        return replace(
            ready,
            build=None,
            errors=(
                SessionError(
                    error_id="mock-build-error",
                    code="build_failed",
                    message="The suggested deck could not be built. Retry after ratings recover.",
                    recoverable=True,
                    operation=OperationKind.BUILD,
                ),
            ),
        )
    if scenario == "backtest_missing":
        missing_row = replace(
            _backtest().rows[-1],
            recommended=None,
            actual=CARDS[4],
            match=None,
            skipped_reason="Every offered-card history record is unavailable.",
        )
        return replace(
            ready,
            backtest=replace(
                _backtest(),
                rows=(missing_row,),
                match_count=0,
                compared_count=0,
                skipped_count=1,
            ),
        )
    if scenario == "backtest_error":
        return replace(
            ready,
            backtest=None,
            errors=(
                SessionError(
                    error_id="mock-backtest-error",
                    code="backtest_failed",
                    message="The persisted pick history could not be compared.",
                    recoverable=True,
                    operation=OperationKind.BACKTEST,
                ),
            ),
        )
    return replace(
        ready,
        errors=(
            SessionError(
                error_id="mock-ratings-error",
                code="ratings_download_failed",
                message="Ratings download failed. Existing recommendations still work.",
                recoverable=True,
                operation=OperationKind.RATINGS,
            ),
        ),
    )


class MockLiveSession:
    """Publish representative snapshots through the live-session boundary.
    Scenario selection is deterministic and never touches disk or network.
    """

    def __init__(self, *, scenario: MockScenario = "ready") -> None:
        self._scenario = scenario
        self._snapshot = _snapshot_for_scenario(scenario=scenario)
        self._contextual_adjustments_enabled = (
            self._snapshot.contextual_adjustments_enabled
        )

    @property
    def scenario(self) -> MockScenario:
        return self._scenario

    @property
    def snapshot(self) -> LiveSessionSnapshot:
        return self._snapshot

    def _with_contextual_mode(
        self,
        *,
        snapshot: LiveSessionSnapshot,
    ) -> LiveSessionSnapshot:
        evidence = (
            _snapshot_for_scenario(scenario=self._scenario).contextual_evidence
            if self._contextual_adjustments_enabled
            else ContextualEvidenceState(
                status=ContextualEvidenceStatus.DISABLED,
                message="Contextual · disabled",
            )
        )
        if (
            snapshot.contextual_adjustments_enabled
            is self._contextual_adjustments_enabled
            and snapshot.contextual_evidence == evidence
        ):
            return snapshot
        return replace(
            snapshot,
            contextual_adjustments_enabled=self._contextual_adjustments_enabled,
            contextual_evidence=evidence,
        )

    def select_scenario(self, *, scenario: MockScenario) -> LiveSessionSnapshot:
        if scenario not in MOCK_SCENARIOS:
            raise ValueError(f"Unsupported mock scenario: {scenario}")
        self._scenario = scenario
        self._snapshot = self._with_contextual_mode(
            snapshot=_snapshot_for_scenario(scenario=scenario)
        )
        return self._snapshot

    def dispatch(self, *, command: LiveSessionCommand) -> LiveSessionSnapshot:
        snapshot = self._snapshot
        if isinstance(command, ChooseRecommendation):
            known_ids = {
                recommendation.card.grp_id
                for recommendation in snapshot.recommendations.cards
            }
            if command.grp_id in known_ids:
                snapshot = replace(
                    snapshot,
                    recommendations=replace(
                        snapshot.recommendations,
                        selected_grp_id=command.grp_id,
                    ),
                    card_image=CardImageState(
                        grp_id=command.grp_id,
                        message="Mock card images are unavailable.",
                    ),
                )
        elif isinstance(command, FocusBuildCard):
            build = snapshot.build
            known_ids = (
                set()
                if build is None
                else {
                    entry.card.grp_id
                    for entry in (*build.spells, *build.bench)
                }
            )
            if command.grp_id not in known_ids:
                raise ValueError(f"Card {command.grp_id} is not in the current build.")
            snapshot = replace(
                snapshot,
                card_image=CardImageState(
                    grp_id=command.grp_id,
                    message="Mock card images are unavailable.",
                ),
            )
        elif isinstance(command, ChangeRanking):
            snapshot = replace(
                snapshot,
                recommendations=self._change_ranking(
                    state=snapshot.recommendations,
                    ranking_mode=command.ranking_mode,
                ),
            )
        elif isinstance(command, ChangeSplashPreference):
            snapshot = replace(
                snapshot,
                recommendations=replace(
                    snapshot.recommendations,
                    splash_enabled=command.enabled,
                ),
            )
        elif isinstance(command, ChangeContextualScoring):
            self._contextual_adjustments_enabled = command.enabled
        elif isinstance(command, ChooseAccount):
            matching = next(
                (
                    account
                    for account in snapshot.accounts
                    if account.account_id == command.account_id
                ),
                None,
            )
            if matching is not None:
                snapshot = replace(snapshot, active_account=matching)
        elif isinstance(command, RequestRatingsDownload):
            snapshot = _snapshot_for_scenario(scenario="progress")
            self._scenario = "progress"
        elif isinstance(command, RequestBuild):
            ready = _ready_snapshot()
            build = ready.build
            if build is not None and command.pair_override is not None:
                build = replace(
                    build,
                    selected_pair=command.pair_override,
                    pair_options=tuple(
                        replace(
                            option,
                            selected=option.pair == command.pair_override,
                        )
                        for option in build.pair_options
                    ),
                )
            if build is not None:
                build = replace(build, pair_override=command.pair_override)
            snapshot = replace(
                snapshot,
                build=build,
                progress=None,
                errors=tuple(
                    error
                    for error in snapshot.errors
                    if error.operation != OperationKind.BUILD
                ),
            )
        elif isinstance(command, RequestBacktest):
            snapshot = replace(
                snapshot,
                backtest=_backtest(),
                progress=None,
                errors=tuple(
                    error
                    for error in snapshot.errors
                    if error.operation != OperationKind.BACKTEST
                ),
            )
        elif isinstance(command, DismissError):
            snapshot = replace(
                snapshot,
                errors=tuple(
                    error
                    for error in snapshot.errors
                    if error.error_id != command.error_id
                ),
            )
        elif isinstance(command, RetryError):
            if any(error.error_id == command.error_id for error in snapshot.errors):
                snapshot = _ready_snapshot()
                self._scenario = "ready"
        self._snapshot = self._with_contextual_mode(snapshot=snapshot)
        return self._snapshot

    @staticmethod
    def _change_ranking(
        *,
        state: RecommendationState,
        ranking_mode: str,
    ) -> RecommendationState:
        if ranking_mode not in state.supported_ranking_modes:
            return state
        key = {
            "score": lambda recommendation: -recommendation.score,
            "win_rate": lambda recommendation: -(
                recommendation.win_rate
                if recommendation.win_rate is not None
                else float("-inf")
            ),
            "alsa": lambda recommendation: (
                recommendation.average_last_seen_at
                if recommendation.average_last_seen_at is not None
                else float("inf")
            ),
            "mana_value": lambda recommendation: (
                recommendation.card.mana_value
                if recommendation.card.mana_value is not None
                else float("inf")
            ),
        }[ranking_mode]
        cards = tuple(
            replace(recommendation, rank=index)
            for index, recommendation in enumerate(
                sorted(state.cards, key=key),
                start=1,
            )
        )
        return replace(state, ranking_mode=ranking_mode, cards=cards)


