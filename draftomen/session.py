"""Define immutable state and explicit commands for live Draftomen sessions.
Frontend adapters consume this contract without importing presentation frameworks.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from os import PathLike
from pathlib import Path
from threading import RLock
from typing import Protocol, TypeAlias

from draftomen.audit import DraftAuditStore
from draftomen.backtest import (
    BacktestReport as DomainBacktestReport,
    generate_backtest_report,
    load_persisted_backtest_state,
)
from draftomen.carddb import CardDatabase, CardFace, CardInfo
from draftomen.cardimages import CardImageError, CardImageService
from draftomen.config import POLL_INTERVAL_SECONDS, SPLASH
from draftomen.deckbuilder import (
    BuildPool,
    DeckBuilderError,
    ManaBase,
    PairSelection,
    SpellSelection,
    build_deck_from_pool,
)
from draftomen.events import (
    EXPECTED_PICKS_PER_PACK,
    EXPECTED_TOTAL_PICKS,
    AccountEvent,
    DraftCompletedEvent,
    DraftEvent,
    DraftLogParser,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
    QuickDraftDetectedEvent,
)
from draftomen.logfollow import LogFollower, is_log_readable
from draftomen.pool import (
    AccountProfile,
    DraftPoolError,
    DraftPoolStore,
    DraftState,
    list_account_profiles,
    list_draft_states,
)
from draftomen.pickengine import (
    ContextualScoreBreakdown,
    PickEngine,
    PickScoringContext,
    ScoredCard,
    ScoredPack,
    recommendation_confidence_summary,
    recommendation_explanation,
)
from draftomen.pool_ledger import PoolRoleLedger
from draftomen.ranking import (
    DEFAULT_RANKING_MODE,
    RANKING_MODES,
    RankingMode,
    rank_scored_cards,
    validate_ranking_mode,
)
from draftomen.set_profile import (
    ProfileMaturity,
    SetProfile,
    SetProfileError,
    load_scoring_profile,
)
from draftomen.profile_client import (
    ProfileClient,
    ProfileRefreshOutcome,
    ProfileRefreshResult,
)
from draftomen.seventeen import (
    DownloadProgressCallback,
    QUICK_DRAFT_FORMAT,
    SeventeenLandsData,
    SeventeenLandsDownloadProgress,
)

PathInput: TypeAlias = str | PathLike[str]
SnapshotPublisher: TypeAlias = Callable[["LiveSessionSnapshot"], None]
EventPublisher: TypeAlias = Callable[["LiveSessionEvent"], None]


class SetCardDataLoader(Protocol):
    """Load one validated set-scoped card database."""

    def __call__(self, set_code: str, *, allow_network: bool) -> CardDatabase:
        ...


RatingsLoader: TypeAlias = Callable[[str], SeventeenLandsData]
RatingsLoaderFactory: TypeAlias = Callable[[CardDatabase], RatingsLoader]


class RatingsProgressLoader(Protocol):
    def __call__(
        self,
        set_code: str,
        progress_callback: DownloadProgressCallback,
        *,
        refresh: bool,
    ) -> SeventeenLandsData:
        """Load ratings while reporting request progress.
        The callback receives download progress updates during loading.
        """
        ...


RatingsProgressLoaderFactory: TypeAlias = Callable[
    [CardDatabase],
    RatingsProgressLoader,
]
RatingsCacheChecker: TypeAlias = Callable[[str], bool]

LOG_SETUP_GUIDANCE = (
    "No draft or readable Player.log was detected. Enable Detailed Logs "
    "(Plugin Support) in Arena's Account settings (usually Settings → Account; "
    "the exact path may vary by platform or Arena version). Restart Arena if "
    "required, then return to Draft Omen and try again while Arena is running."
)

WAITING_FOR_DRAFT_MESSAGE = "Waiting for a Quick Draft."
RECENT_PICK_LIMIT = 24


class ApplicationPhase(StrEnum):
    """Identify the user-visible phase of the live application.
    Detailed failures remain separate so phases stay stable across frontends.
    """

    STARTING = "starting"
    WAITING_FOR_DRAFT = "waiting_for_draft"
    DRAFTING = "drafting"
    DRAFT_COMPLETE = "draft_complete"
    STOPPED = "stopped"


class OperationKind(StrEnum):
    """Identify long-running work reported through session progress.
    Frontends decide how each operation is scheduled and presented.
    """

    CARD_DATA = "card_data"
    RATINGS = "ratings"
    BUILD = "build"
    BACKTEST = "backtest"


class DataLoadPhase(StrEnum):
    """Identify reusable data readiness without presentation-specific state.
    Idle resources are configured but have not started loading yet.
    """

    UNAVAILABLE = "unavailable"
    IDLE = "idle"
    MISSING = "missing"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ApplicationStatus:
    """Describe the current application phase and concise status message.
    This state contains no frontend-specific formatting or widget state.
    """

    phase: ApplicationPhase = ApplicationPhase.STARTING
    message: str = "Starting Draft Omen."
    setup_guidance: bool = False


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """Identify one known Arena account using stable and display values.
    The account id remains authoritative when the screen name is unavailable.
    """

    account_id: str
    screen_name: str | None


@dataclass(frozen=True, slots=True)
class DraftIdentity:
    """Identify the active account-scoped draft and current pick position.
    Optional coordinates represent pre-draft and recovered draft states.
    """

    account_id: str
    draft_id: str
    event_name: str
    set_code: str
    course_id: str | None
    pack_number: int | None
    pick_number: int | None
    completed: bool


@dataclass(frozen=True, slots=True)
class CardView:
    """Provide framework-neutral card facts shared by visual surfaces.
    Cached image paths keep retrieval and filesystem work in Python services.
    """

    grp_id: int
    name: str
    colors: tuple[str, ...]
    rarity: str
    types: tuple[str, ...]
    mana_cost: str | None
    mana_value: float | None
    image_path: str | None
    unknown: bool = False
    produced_mana: tuple[str, ...] = ()
    oracle_text: str | None = None
    keywords: tuple[str, ...] = ()
    type_line: str | None = None
    subtypes: tuple[str, ...] = ()
    layout: str | None = None
    faces: tuple[CardFace, ...] = ()
    set_code: str | None = None
    collector_number: str | None = None
    arena_id: int | None = None
    source_provenance: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Recommendation:
    """Describe one ranked card recommendation and its scoring evidence.
    Primitive values let every frontend choose its own presentation.
    """

    rank: int
    card: CardView
    score: int
    win_rate: float | None
    average_last_seen_at: float | None
    source_label: str
    color_fit: str
    no_data: bool
    contextual_breakdown: ContextualScoreBreakdown = field(
        default_factory=ContextualScoreBreakdown
    )
    contextual_evidence: tuple[str, ...] = ()
    contextual_pair: str | None = None
    contextual_theme: str | None = None
    contextual_profile_maturity: str | None = None
    contextual_profile_confidence: float | None = None
    letter_grade: str | None = None
    explanation: str | None = None


@dataclass(frozen=True, slots=True)
class RecommendationState:
    """Hold the ordered recommendation rows for the current offered pack.
    Selection is state published by Python rather than QML mutation.
    """

    ranking_mode: RankingMode = "score"
    supported_ranking_modes: tuple[RankingMode, ...] = RANKING_MODES
    splash_enabled: bool = SPLASH.enabled_by_default
    cards: tuple[Recommendation, ...] = ()
    selected_grp_id: int | None = None
    source_summary: str | None = None
    confidence_summary: str | None = None


@dataclass(frozen=True, slots=True)
class CardDataState:
    """Describe shared card metadata readiness for scoring operations.
    The live session synchronously loads the selected set at the pre-draft
    boundary while frontends render this primitive state.
    """

    phase: DataLoadPhase = DataLoadPhase.UNAVAILABLE
    message: str = "Card metadata is not configured."
    last_successful_update: str | None = None


@dataclass(frozen=True, slots=True)
class RatingsState:
    """Describe ratings readiness for the active set and offered pack.
    Rated counts are populated after the current pack has been scored.
    """

    set_code: str | None = None
    phase: DataLoadPhase = DataLoadPhase.UNAVAILABLE
    message: str = "17Lands ratings are not configured."
    rated_cards: int | None = None
    total_cards: int | None = None
    last_successful_update: str | None = None


@dataclass(frozen=True, slots=True)
class SetProfileState:
    """Describe the active set profile without exposing profile internals.

    The profile itself remains session-owned for scoring.  This projection is
    deliberately limited to primitive values so frontends can render local
    cache readiness and asynchronous refresh outcomes without seeing client
    diagnostics.
    """

    set_code: str | None = None
    event_format: str | None = None
    maturity: str | None = None
    profile_version: str | None = None
    source: str | None = None
    phase: DataLoadPhase = DataLoadPhase.UNAVAILABLE
    refresh_outcome: str | None = None
    message: str = "Set profile is not configured."


@dataclass(frozen=True, slots=True)
class ProfileRefreshRequest:
    """Identify one adapter-owned asynchronous profile refresh."""

    generation: int
    set_code: str
    event_format: str


@dataclass(frozen=True, slots=True)
class CardImageState:
    """Describe focused-card or recent-pick image availability for adapters.
    Fetching stays in Python while frontends render the atomically published path.
    """

    grp_id: int | None = None
    image_path: str | None = None
    phase: DataLoadPhase = DataLoadPhase.UNAVAILABLE
    message: str = "No selected card image is available."


@dataclass(frozen=True, slots=True)
class CardImageRequest:
    """Identify one focused, recent-pick, or recommendation image fetch.
    The session validates completion before publishing its result.
    """

    generation: int
    grp_id: int
    image_uri: str | None


@dataclass(frozen=True, slots=True)
class CardImageFetchResult:
    """Carry an image path and the URI resolved by the fetch worker."""

    image_path: Path
    image_uri: str


@dataclass(frozen=True, slots=True)
class RecentPick:
    """Describe one chronological picked card and its gallery image state."""

    card: CardView
    image: CardImageState


@dataclass(frozen=True, slots=True)
class PoolCard:
    """Describe one distinct card and quantity in the drafted pool.
    Stable ordering allows frontends to render deterministic summaries.
    """

    card: CardView
    quantity: int


@dataclass(frozen=True, slots=True)
class PoolState:
    """Describe the drafted pool and its current color commitment.
    Aggregate values avoid forcing presentation code to redo domain work.
    """

    cards: tuple[PoolCard, ...] = ()
    recent_picks: tuple[RecentPick, ...] = ()
    total_cards: int = 0
    inferred_pair: str | None = None
    commitment: float = 0.0
    color_distribution: tuple[tuple[str, int], ...] = ()
    mana_curve: tuple[int, ...] = ()
    average_mana_value: float | None = None
    target_cards: int = 42
    current_colors: tuple[str, ...] = ()
    role_ledger: PoolRoleLedger | None = None


@dataclass(frozen=True, slots=True)
class ProgressState:
    """Report determinate or indeterminate progress for one operation.
    Missing counts represent work whose total is not yet known.
    """

    operation: OperationKind
    message: str
    completed: int | None = None
    total: int | None = None


@dataclass(frozen=True, slots=True)
class SessionError:
    """Describe one stable application error and its recovery capability.
    Error identifiers support explicit dismiss and retry commands.
    """

    error_id: str
    code: str
    message: str
    recoverable: bool
    operation: OperationKind | None = None


@dataclass(frozen=True, slots=True)
class BuildPairOption:
    """Describe one scored color-pair option for a deck build.
    The selected pair and automatic pair remain independently visible.
    """

    pair: str
    score: float
    selected: bool
    automatic: bool
    playable_count: int | None = None
    playable_score_sum: float | None = None
    pair_win_rate: float | None = None


@dataclass(frozen=True, slots=True)
class BuildCard:
    """Describe an ordered spell or bench card in a build result.
    Quantities preserve duplicate picks without repeating presentation rows.
    """

    card: CardView
    quantity: int
    score: int | None = None
    win_rate: float | None = None
    average_last_seen_at: float | None = None
    letter_grade: str | None = None
    source_label: str | None = None
    color_fit: str | None = None
    no_data: bool = False


@dataclass(frozen=True, slots=True)
class BuildLand:
    """Describe one basic or drafted nonbasic land in a build result.
    Optional card data distinguishes generated basics from drafted lands.
    """

    name: str
    quantity: int
    source_colors: tuple[str, ...]
    card: CardView | None = None


@dataclass(frozen=True, slots=True)
class BuildResult:
    """Provide structured pair, spell, land, bench, and warning output.
    Frontends render this data without parsing terminal-formatted build text.
    """

    selected_pair: str
    pair_options: tuple[BuildPairOption, ...]
    spells: tuple[BuildCard, ...]
    lands: tuple[BuildLand, ...]
    bench: tuple[BuildCard, ...]
    deck_size: int
    average_mana_value: float | None = None
    pair_override: str | None = None
    warnings: tuple[str, ...] = ()
    spell_count: int | None = None
    land_count: int | None = None
    creature_count: int | None = None
    instant_count: int | None = None
    role_ledger: PoolRoleLedger | None = None
    domain_pool: BuildPool | None = None
    domain_selection: PairSelection | None = None
    domain_spell_selection: SpellSelection | None = None
    domain_mana_base: ManaBase | None = None


@dataclass(frozen=True, slots=True)
class BacktestPickResult:
    """Describe the recommendation comparison for one persisted draft pick.
    Missing cards and match state preserve skipped-history outcomes.
    """

    pack_number: int
    pick_number: int
    recommended: CardView | None
    actual: CardView | None
    match: bool | None
    skipped_reason: str | None
    data_source: str | None
    pool_size: int | None = None
    offered_count: int | None = None
    recommended_score: int | None = None
    recommended_win_rate: float | None = None
    role_ledger: PoolRoleLedger | None = None
    scoring_context: PickScoringContext | None = None
    contextual_evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Provide structured backtest rows and their aggregate outcome counts.
    Data sources remain available for attribution and fallback disclosure.
    """

    ranking_mode: RankingMode
    rows: tuple[BacktestPickResult, ...]
    match_count: int
    compared_count: int
    skipped_count: int
    data_sources: tuple[str, ...]
    account_id: str | None = None
    account_screen_name: str | None = None
    draft_id: str | None = None
    set_code: str | None = None
    event_name: str | None = None
    completed: bool | None = None
    chosen_pick_count: int | None = None


@dataclass(frozen=True, slots=True)
class LiveSessionSnapshot:
    """Publish all UI-neutral state needed by Draftomen frontends.
    Replacing snapshots keeps presentation adapters out of domain mutation.
    """

    status: ApplicationStatus = field(default_factory=ApplicationStatus)
    accounts: tuple[AccountIdentity, ...] = ()
    active_account: AccountIdentity | None = None
    draft: DraftIdentity | None = None
    card_data: CardDataState = field(default_factory=CardDataState)
    ratings: RatingsState = field(default_factory=RatingsState)
    set_profile: SetProfileState = field(default_factory=SetProfileState)
    recommendations: RecommendationState = field(default_factory=RecommendationState)
    pool: PoolState = field(default_factory=PoolState)
    card_image: CardImageState = field(default_factory=CardImageState)
    current_pack_event: PackOfferedEvent | None = None
    current_scored_pack: ScoredPack | None = None
    progress: ProgressState | None = None
    errors: tuple[SessionError, ...] = ()
    build: BuildResult | None = None
    backtest: BacktestResult | None = None


@dataclass(frozen=True, slots=True)
class LiveSessionEvent:
    """Publish one consumed domain event with its resulting session state.
    Event adapters can preserve ordering without duplicating session orchestration.
    """

    event: DraftEvent
    snapshot: LiveSessionSnapshot
    scored_pack: ScoredPack | None = None


@dataclass(frozen=True, slots=True)
class ChooseAccount:
    """Request that the session make one known account active.
    The session remains responsible for resolving its persisted draft.
    """

    account_id: str


@dataclass(frozen=True, slots=True)
class ChooseRecommendation:
    """Request that one recommendation become the selected card.
    Selection changes are published in the next immutable snapshot.
    """

    grp_id: int


@dataclass(frozen=True, slots=True)
class FocusBuildCard:
    """Request that one current build spell or bench card receive image focus."""

    grp_id: int


@dataclass(frozen=True, slots=True)
class ChangeRanking:
    """Request a supported recommendation and backtest ranking mode.
    The application layer validates and applies the functional choice.
    """

    ranking_mode: RankingMode


@dataclass(frozen=True, slots=True)
class ChangeSplashPreference:
    """Request whether supported splash recommendations are considered.
    The application layer owns rescoring and persistence consequences.
    """

    enabled: bool


@dataclass(frozen=True, slots=True)
class RequestRatingsDownload:
    """Request ratings retrieval for one active draft set.
    Progress and recoverable failures return through session state.
    """

    set_code: str


@dataclass(frozen=True, slots=True)
class RequestBuild:
    """Request a deck build with an optional explicit pair override.
    Structured build output returns through the immutable snapshot.
    """

    pair_override: str | None = None
    allow_splash: bool = True


@dataclass(frozen=True, slots=True)
class RequestBacktest:
    """Request a persisted-draft backtest using explicit identity filters.
    Missing filters allow the application to select the active draft.
    """

    account_id: str | None = None
    draft_id: str | None = None


@dataclass(frozen=True, slots=True)
class DismissError:
    """Request removal of one recoverable or acknowledged error.
    The error identifier avoids direct mutation of an errors collection.
    """

    error_id: str


@dataclass(frozen=True, slots=True)
class RetryError:
    """Request retry of the operation associated with one error.
    The application decides whether the current state permits a retry.
    """

    error_id: str


LiveSessionCommand: TypeAlias = (
    ChooseAccount
    | ChooseRecommendation
    | FocusBuildCard
    | ChangeRanking
    | ChangeSplashPreference
    | RequestRatingsDownload
    | RequestBuild
    | RequestBacktest
    | DismissError
    | RetryError
)


class LiveSession:
    """Coordinate live Arena ingestion and persisted draft lifecycle state.
    Frontends schedule polling and receive immutable snapshots through one contract.
    """

    def __init__(
        self,
        *,
        log_path: PathInput,
        card_database: CardDatabase | None = None,
        set_card_data_loader: SetCardDataLoader | None = None,
        app_dir: PathInput | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        previous_log_path: PathInput | None = None,
        snapshot_publisher: SnapshotPublisher | None = None,
        event_publisher: EventPublisher | None = None,
        ranking_mode: RankingMode = DEFAULT_RANKING_MODE,
        ratings_loader: RatingsLoader | None = None,
        ratings_loader_factory: RatingsLoaderFactory | None = None,
        ratings_progress_loader: RatingsProgressLoader | None = None,
        ratings_progress_loader_factory: RatingsProgressLoaderFactory | None = None,
        ratings_cache_checker: RatingsCacheChecker | None = None,
        card_image_service: CardImageService | None = None,
        splash_enabled: bool = SPLASH.enabled_by_default,
        set_profile: SetProfile | None = None,
        profile_client: ProfileClient | None = None,
    ) -> None:
        if card_database is not None and set_card_data_loader is not None:
            raise ValueError(
                "card_database and set_card_data_loader are mutually exclusive."
            )
        configured_ratings_loaders = sum(
            loader is not None
            for loader in (
                ratings_loader,
                ratings_loader_factory,
                ratings_progress_loader,
                ratings_progress_loader_factory,
            )
        )
        if configured_ratings_loaders > 1:
            raise ValueError("Configure exactly one ratings loader or loader factory.")

        self.log_path = Path(log_path).expanduser().resolve(strict=False)
        initial_log_readable = is_log_readable(path=self.log_path)
        self.follower = LogFollower(
            log_path=self.log_path,
            app_dir=app_dir,
            poll_interval=poll_interval,
            previous_log_path=previous_log_path,
        )
        self.parser = DraftLogParser()
        self.store = DraftPoolStore(app_dir=app_dir)
        self.audit_store = DraftAuditStore(app_dir=self.store.root.parent)
        self._state_lock = RLock()
        self._snapshot_publisher = snapshot_publisher
        self._event_publisher = event_publisher
        self._ranking_mode = validate_ranking_mode(ranking_mode=ranking_mode)
        self._splash_enabled = splash_enabled
        self._card_database = card_database
        self._set_card_data_loader = set_card_data_loader
        self._card_database_set_code: str | None = None
        self._card_data_network_open = set_card_data_loader is not None
        self._card_data_local_lookup_attempted = False
        self._card_data_requested_set_code: str | None = None
        self._card_data_detection_event_name: str | None = None
        self._configured_set_profile = set_profile
        self._set_profile = set_profile
        self._profile_client = profile_client
        self._set_profiles_by_set: dict[str, SetProfile | None] = {}
        self._set_profile_states_by_set: dict[str, SetProfileState] = {}
        self._ratings_loader = ratings_loader
        self._ratings_loader_factory = ratings_loader_factory
        self._ratings_progress_loader = ratings_progress_loader
        self._ratings_progress_loader_factory = ratings_progress_loader_factory
        self._ratings_cache_checker = ratings_cache_checker
        self._ratings_data_by_set: dict[str, SeventeenLandsData | None] = {}
        self._ratings_state_by_set: dict[str, RatingsState] = {}
        self._ratings_progress_by_set: dict[str, ProgressState] = {}
        self._ratings_errors_by_set: dict[str, SessionError] = {}
        self._loading_rating_sets: set[str] = set()
        self._active_set_code_value: str | None = None
        self._profile_refresh_generation = 0
        self._profile_refresh_request: ProfileRefreshRequest | None = None
        self._current_pack_event: PackOfferedEvent | None = None
        self._current_scored_pack: ScoredPack | None = None
        self._transient_pool_grp_ids: tuple[int, ...] = ()
        self._last_build_request: RequestBuild | None = None
        self._card_image_request: CardImageRequest | None = None
        self._last_backtest_request: RequestBacktest | None = None
        self._build_request_generation = 0
        self._backtest_request_generation = 0
        self._login_generation = self.parser.login_generation
        self._log_account_id: str | None = None
        self._states_by_key: dict[tuple[str, str], DraftState] = {}
        self._screen_names_by_account_id: dict[str, str] = {}
        self._card_image_service = card_image_service
        self._card_image_paths_by_uri: dict[str, Path] = {}
        self._card_image_uris_by_grp_id: dict[int, str] = {}
        self._card_image_paths_by_grp_id: dict[int, Path] = {}
        self._card_image_generation = 0
        self._recommendation_image_generation = 0
        self._recommendation_image_pack: tuple[
            PackOfferedEvent | None,
            str | None,
            str | None,
        ] | None = None
        self._recommendation_image_requests: list[CardImageRequest] = []
        self._recommendation_image_failures: set[tuple[int, str | None]] = set()
        self._recent_pick_image_generation = 0
        self._recent_pick_image_requests: list[CardImageRequest] = []
        self._recent_pick_image_states: dict[int, CardImageState] = {}
        self._recent_pick_pool_grp_ids: tuple[int, ...] | None = None
        self._configure_ratings_loader_for_card_database()
        if card_database is not None:
            card_data = CardDataState(
                phase=DataLoadPhase.READY,
                message="Card metadata is ready.",
                last_successful_update=_last_successful_update(
                    database=card_database
                ),
            )
        elif set_card_data_loader is not None:
            card_data = CardDataState(
                phase=DataLoadPhase.IDLE,
                message="Card metadata is ready to load.",
            )
        else:
            card_data = CardDataState()
        ratings_state = self._initial_ratings_state()
        self._snapshot = LiveSessionSnapshot(
            status=_waiting_for_draft_status(setup_guidance=not initial_log_readable),
            accounts=self._known_accounts(),
            card_data=card_data,
            ratings=ratings_state,
            set_profile=SetProfileState(),
            recommendations=RecommendationState(
                ranking_mode=self._ranking_mode,
                splash_enabled=splash_enabled,
            ),
        )

    @property
    def snapshot(self) -> LiveSessionSnapshot:
        """Return the latest immutable application snapshot.
        Reading state has no parsing, persistence, or publication side effects.
        """

        return self._snapshot

    @property
    def card_database(self) -> CardDatabase | None:
        """Return the card database currently owned by the live session.
        Frontends may use it for presentation-only metadata lookups.
        """

        return self._card_database

    @property
    def current_pack_event(self) -> PackOfferedEvent | None:
        """Return the active immutable pack event, when one is available.
        Presentation adapters may use its coordinates and offered card ids.
        """

        return self.snapshot.current_pack_event

    @property
    def current_scored_pack(self) -> ScoredPack | None:
        """Return the active immutable scored pack, when one is available.
        Presentation adapters may retain their existing domain renderers.
        """

        return self.snapshot.current_scored_pack


    def profile_refresh_request(self) -> ProfileRefreshRequest | None:
        """Return the one pending profile refresh for adapter-owned work.

        The request remains available until its completion or failure is
        accepted.  Reading it never performs local or remote I/O.
        """

        with self._state_lock:
            return self._profile_refresh_request

    def complete_profile_refresh(
        self,
        *,
        request: ProfileRefreshRequest,
        result: ProfileRefreshResult | SetProfile,
    ) -> None:
        """Apply one adapter-owned refresh result if it is still current."""

        with self._state_lock:
            if not self._profile_refresh_request_is_current(request=request):
                return

            self._profile_refresh_request = None
            active_set_code = self._active_set_code_value
            if active_set_code is None:
                return

            if isinstance(result, SetProfile):
                profile = result
                outcome = ProfileRefreshOutcome.UPDATED.value
            elif isinstance(result, ProfileRefreshResult):
                profile = result.profile
                outcome = _profile_refresh_outcome_value(result.outcome)
            else:
                raise TypeError("result must be a ProfileRefreshResult or SetProfile.")

            if (
                profile.set_code.upper() != active_set_code
                or profile.event_format.casefold() != request.event_format.casefold()
            ):
                self._publish_profile_refresh_status_locked(
                    request=request,
                    outcome=ProfileRefreshOutcome.ARTIFACT_INVALID.value,
                    phase=DataLoadPhase.FAILED,
                )
                return

            current_profile = self._set_profile
            profile_changed = (
                profile.maturity is not ProfileMaturity.GENERIC
                and profile != current_profile
                and _profile_refresh_profile_is_adoptable(
                    profile=profile,
                    current_profile=current_profile,
                    outcome=outcome,
                )
            )
            if profile_changed:
                self._set_profile = profile
                self._set_profiles_by_set[active_set_code] = profile
                self._set_profile_states_by_set[active_set_code] = (
                    self._profile_state_for_profile(
                        profile=profile,
                        set_code=active_set_code,
                        source="remote",
                        phase=DataLoadPhase.READY,
                        refresh_outcome=outcome,
                    )
                )
                self._publish(
                    snapshot=replace(
                        self.snapshot,
                        set_profile=self._set_profile_states_by_set[active_set_code],
                    )
                )
                self._score_current_pack_locked()
                return

            if outcome == ProfileRefreshOutcome.UPDATED.value:
                outcome = ProfileRefreshOutcome.UNCHANGED.value
            phase = (
                DataLoadPhase.READY
                if outcome
                in {
                    ProfileRefreshOutcome.CACHED.value,
                    ProfileRefreshOutcome.UNCHANGED.value,
                }
                else DataLoadPhase.FAILED
            )
            self._publish_profile_refresh_status_locked(
                request=request,
                outcome=outcome,
                phase=phase,
            )

    def fail_profile_refresh(
        self,
        *,
        request: ProfileRefreshRequest,
        error_message: str | None = None,
    ) -> None:
        """Reject one adapter-owned refresh while retaining the last good profile."""

        del error_message
        with self._state_lock:
            if not self._profile_refresh_request_is_current(request=request):
                return
            self._profile_refresh_request = None
            if self._active_set_code_value is None:
                return
            self._publish_profile_refresh_status_locked(
                request=request,
                outcome=ProfileRefreshOutcome.REMOTE_FAILED.value,
                phase=DataLoadPhase.FAILED,
            )


    def selected_card_image_request(self) -> CardImageRequest | None:
        """Return the current selected-image fetch for an active frontend.
        Calling this method has no network or publication side effects.
        """

        return self._card_image_request

    def recent_pick_image_request(self) -> CardImageRequest | None:
        """Return the next queued recent-pick image fetch, if any."""

        return (
            self._recent_pick_image_requests[0]
            if self._recent_pick_image_requests
            else None
        )

    def recommendation_image_request(self) -> CardImageRequest | None:
        """Return the next queued current-pack recommendation image fetch."""

        return (
            self._recommendation_image_requests[0]
            if self._recommendation_image_requests
            else None
        )

    def _resolved_image_uri_for_request(
        self,
        *,
        request: CardImageRequest,
    ) -> str | None:
        """Return a request URI without performing metadata or network work."""

        return request.image_uri or self._card_image_uris_by_grp_id.get(request.grp_id)

    def _remember_card_image(
        self,
        *,
        grp_id: int,
        image_uri: str | None,
        image_path: Path,
    ) -> None:
        """Retain a successful image by card and by resolved URI."""

        self._card_image_paths_by_grp_id[grp_id] = image_path
        if image_uri is not None:
            self._card_image_uris_by_grp_id[grp_id] = image_uri
            self._card_image_paths_by_uri[image_uri] = image_path

    def recommendation_image_requests(self) -> tuple[CardImageRequest, ...]:
        """Return a stable view of all queued recommendation image fetches."""

        return tuple(self._recommendation_image_requests)

    def _retire_recommendation_requests_for_image(
        self,
        *,
        grp_id: int,
        image_uri: str | None,
    ) -> None:
        """Drop recommendation work satisfied by a focused image fetch."""

        self._recommendation_image_requests = [
            request
            for request in self._recommendation_image_requests
            if request.grp_id != grp_id
            and (image_uri is None or request.image_uri != image_uri)
        ]

    def complete_recommendation_image_request(
        self,
        *,
        request: CardImageRequest,
        image_path: Path,
        image_uri: str | None = None,
    ) -> None:
        """Publish one recommendation image when its pack generation is current."""

        with self._state_lock:
            if (
                request.generation != self._recommendation_image_generation
                or request not in self._recommendation_image_requests
            ):
                return
            self._recommendation_image_requests.remove(request)
            resolved_image_uri = image_uri or self._resolved_image_uri_for_request(
                request=request
            )
            self._remember_card_image(
                grp_id=request.grp_id,
                image_uri=resolved_image_uri,
                image_path=image_path,
            )
            recommendations = replace(
                self.snapshot.recommendations,
                cards=tuple(
                    replace(
                        recommendation,
                        card=replace(
                            recommendation.card,
                            image_path=str(image_path),
                        ),
                    )
                    if recommendation.card.grp_id == request.grp_id
                    else recommendation
                    for recommendation in self.snapshot.recommendations.cards
                ),
            )
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    recommendations=recommendations,
                )
            )

    def fail_recommendation_image_request(
        self,
        *,
        request: CardImageRequest,
        error_message: str,
    ) -> None:
        """Retire one failed recommendation image without stopping the queue."""

        del error_message
        with self._state_lock:
            if (
                request.generation != self._recommendation_image_generation
                or request not in self._recommendation_image_requests
            ):
                return
            self._recommendation_image_failures.add(
                (request.grp_id, request.image_uri)
            )
            self._recommendation_image_requests = [
                queued
                for queued in self._recommendation_image_requests
                if queued != request
            ]

    def complete_recent_pick_image_request(
        self,
        *,
        request: CardImageRequest,
        image_path: Path,
        image_uri: str | None = None,
    ) -> None:
        """Publish a successful gallery fetch when its pool generation is current."""

        with self._state_lock:
            if (
                request.generation != self._recent_pick_image_generation
                or request not in self._recent_pick_image_requests
                or request.grp_id not in self._recent_pick_image_states
            ):
                return
            self._recent_pick_image_requests.remove(request)
            resolved_image_uri = image_uri or self._resolved_image_uri_for_request(
                request=request
            )
            self._remember_card_image(
                grp_id=request.grp_id,
                image_uri=resolved_image_uri,
                image_path=image_path,
            )
            self._recent_pick_image_states[request.grp_id] = CardImageState(
                grp_id=request.grp_id,
                image_path=str(image_path),
                phase=DataLoadPhase.READY,
                message="Card image ready.",
            )
            self._publish_recent_pick_projection()

    def fail_recent_pick_image_request(
        self,
        *,
        request: CardImageRequest,
        error_message: str,
    ) -> None:
        """Publish a failed gallery fetch when its pool generation is current."""

        with self._state_lock:
            if (
                request.generation != self._recent_pick_image_generation
                or request not in self._recent_pick_image_requests
                or request.grp_id not in self._recent_pick_image_states
            ):
                return
            self._recent_pick_image_requests.remove(request)
            self._recent_pick_image_states[request.grp_id] = CardImageState(
                grp_id=request.grp_id,
                phase=DataLoadPhase.FAILED,
                message=f"Card image unavailable: {error_message}",
            )
            self._publish_recent_pick_projection()


    def fetch_card_image(self, *, request: CardImageRequest) -> CardImageFetchResult:
        """Resolve and fetch one image in the caller-owned image worker."""

        card_image_service = self._card_image_service
        if card_image_service is None:
            raise ValueError("Card images are not configured.")

        image_uri = self._resolved_image_uri_for_request(request=request)
        if image_uri is None:
            database = self._card_database
            if database is None:
                raise CardImageError("Card metadata is not configured.")
            card = database.lookup(grp_id=request.grp_id)
            image_uri = card_image_service.resolve_focused_image_uri(
                card=card,
                card_database=database,
            )
            if image_uri is None:
                raise CardImageError(f"No card image is available for {card.name}.")
        return CardImageFetchResult(
            image_path=card_image_service.fetch(image_uri=image_uri),
            image_uri=image_uri,
        )

    def complete_card_image_request(
        self,
        *,
        request: CardImageRequest,
        image_path: Path,
        image_uri: str | None = None,
    ) -> None:
        """Publish a successful adapter-owned image fetch when it is still current."""

        with self._state_lock:
            if request != self._card_image_request:
                return
            self._card_image_request = None
            resolved_image_uri = image_uri or self._resolved_image_uri_for_request(
                request=request
            )
            self._retire_recommendation_requests_for_image(
                grp_id=request.grp_id,
                image_uri=resolved_image_uri,
            )
            self._remember_card_image(
                grp_id=request.grp_id,
                image_uri=resolved_image_uri,
                image_path=image_path,
            )
            self._publish_selected_card_image(
                grp_id=request.grp_id,
                image_path=image_path,
            )

    def fail_card_image_request(
        self,
        *,
        request: CardImageRequest,
        error_message: str,
    ) -> None:
        """Publish an adapter-owned image failure when it is still current."""
        with self._state_lock:
            if request != self._card_image_request:
                return
            self._card_image_request = None
            self._retire_recommendation_requests_for_image(
                grp_id=request.grp_id,
                image_uri=self._resolved_image_uri_for_request(request=request),
            )
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    card_image=CardImageState(
                        grp_id=request.grp_id,
                        phase=DataLoadPhase.FAILED,
                        message=f"Card image unavailable: {error_message}",
                    ),
                )
            )

    def _retire_card_image(self) -> CardImageState:
        """Invalidate the active fetch and return the neutral image state.
        Callers publish the returned state while holding the session state lock.
        """

        self._card_image_generation += 1
        self._card_image_request = None
        return CardImageState()

    def ratings_data(self, *, set_code: str) -> SeventeenLandsData | None:
        """Return loaded ratings for presentation-adjacent legacy services.
        The live session remains the only owner of ratings loading and caching.
        """

        return self._ratings_data_by_set.get(set_code.upper())

    def known_accounts(self) -> tuple[AccountIdentity, ...]:
        """Return known accounts from current profiles and persisted drafts.
        Adapters can refresh choices added after session construction.
        """

        return self._known_accounts()

    def poll_once(self) -> LiveSessionSnapshot:
        """Process one follower polling cycle and return the latest snapshot.
        Frontends decide whether this call runs on a worker or event-loop callback.
        """

        snapshot = self._refresh_log_setup_status()
        if snapshot.status.setup_guidance:
            return snapshot

        lines = self.follower.poll()
        self.process_lines(lines=lines)
        return self._refresh_log_setup_status()

    def _refresh_log_setup_status(self) -> LiveSessionSnapshot:
        """Refresh neutral waiting guidance without overriding draft activity.
        Draft-specific statuses retain their normal event-driven state even if a
        later poll cannot open the log.
        """

        log_readable = is_log_readable(path=self.log_path)
        with self._state_lock:
            snapshot = self.snapshot
            if not _log_setup_refresh_is_eligible(snapshot=snapshot):
                return snapshot

            next_status = _waiting_for_draft_status(
                setup_guidance=not log_readable,
            )
            if snapshot.status == next_status:
                return snapshot

            self._publish(snapshot=replace(snapshot, status=next_status))
            return self.snapshot

    def scan_startup_files(
        self,
        *,
        include_previous: bool = True,
        include_pre_draft_detection: bool = True,
    ) -> LiveSessionSnapshot:
        """Process startup recovery logs in follower-defined chronological order.
        The follower advances its offset so later polling does not replay current lines.
        """

        self.process_lines(
            lines=self.follower.scan_startup_files(
                include_previous=include_previous,
            ),
            include_pre_draft_detection=include_pre_draft_detection,
        )
        return self._refresh_log_setup_status()

    def process_lines(
        self,
        *,
        lines: Iterable[str],
        include_pre_draft_detection: bool = True,
    ) -> LiveSessionSnapshot:
        """Consume complete Arena log lines and publish each resulting state change.
        Parser and account context remain incremental across repeated batches.
        """

        for line in lines:
            events = tuple(self.parser.parse_lines(lines=(line,)))
            self._discard_previous_login_account_context()
            for parsed_event in events:
                if (
                    isinstance(parsed_event, QuickDraftDetectedEvent)
                    and not include_pre_draft_detection
                ):
                    continue

                event = self._event_with_log_account(event=parsed_event)
                state = self._consume_store_event(event=event)
                if state is not None:
                    self._remember_state(state=state)
                    self._log_account_id = state.account_id
                    if _event_is_missing_account(event=event):
                        event = replace(event, account_id=state.account_id)
                self._consume_event(event=event, state=state)
                self._publish_event(event=event)
            self._persist_pending_login_name_for_observed_course()
        return self.snapshot

    def dispatch(self, *, command: LiveSessionCommand) -> LiveSessionSnapshot:
        """Apply one explicit frontend intention and publish the resulting snapshot.
        Blocking service work runs synchronously so frontend adapters own scheduling.
        """

        if isinstance(command, ChooseAccount):
            self._choose_account(account_id=command.account_id)
            return self.snapshot

        if isinstance(command, ChangeRanking):
            self._change_ranking(ranking_mode=command.ranking_mode)
            return self.snapshot

        if isinstance(command, ChooseRecommendation):
            self._choose_recommendation(grp_id=command.grp_id)
            return self.snapshot

        if isinstance(command, FocusBuildCard):
            self._focus_build_card(grp_id=command.grp_id)
            return self.snapshot

        if isinstance(command, ChangeSplashPreference):
            self._change_splash_preference(enabled=command.enabled)
            return self.snapshot

        if isinstance(command, RequestRatingsDownload):
            self._request_ratings_download(set_code=command.set_code)
            return self.snapshot

        if isinstance(command, RequestBuild):
            self._request_build(command=command)
            return self.snapshot

        if isinstance(command, RequestBacktest):
            self._request_backtest(command=command)
            return self.snapshot

        if isinstance(command, DismissError):
            self._dismiss_error(error_id=command.error_id)
            return self.snapshot

        if isinstance(command, RetryError):
            self._retry_error(error_id=command.error_id)
            return self.snapshot

        raise ValueError(f"Live session command is not implemented yet: {command!r}.")

    def _request_build(self, *, command: RequestBuild) -> None:
        with self._state_lock:
            self._last_build_request = command
            self._build_request_generation += 1
            generation = self._build_request_generation
            account = self.snapshot.active_account
            draft = self.snapshot.draft
            pool = self.snapshot.pool
            ratings = self.snapshot.ratings
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    progress=ProgressState(
                        operation=OperationKind.BUILD,
                        message="Building deck",
                    ),
                    errors=self._without_operation_error(
                        operation=OperationKind.BUILD,
                    ),
                )
            )
        try:
            build = self._build_result(command=command)
        except Exception as error:
            session_error = SessionError(
                error_id="build",
                code="build_failed",
                message=f"Deck build failed: {error}",
                recoverable=True,
                operation=OperationKind.BUILD,
            )
            with self._state_lock:
                if not self._build_request_is_current(
                    generation=generation,
                    account=account,
                    draft=draft,
                    pool=pool,
                    ratings=ratings,
                ):
                    return
                self._publish(
                    snapshot=replace(
                        self.snapshot,
                        progress=self._progress_after_operation(
                            operation=OperationKind.BUILD,
                        ),
                        errors=self._with_error(error=session_error),
                        build=None,
                    )
                )
            return

        with self._state_lock:
            if not self._build_request_is_current(
                generation=generation,
                account=account,
                draft=draft,
                pool=pool,
                ratings=ratings,
            ):
                return
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    progress=self._progress_after_operation(
                        operation=OperationKind.BUILD,
                    ),
                    errors=self._without_operation_error(
                        operation=OperationKind.BUILD,
                    ),
                    build=build,
                )
            )

    def _build_request_is_current(
        self,
        *,
        generation: int,
        account: AccountIdentity | None,
        draft: DraftIdentity | None,
        pool: PoolState,
        ratings: RatingsState,
    ) -> bool:
        return (
            generation == self._build_request_generation
            and account == self.snapshot.active_account
            and draft == self.snapshot.draft
            and pool == self.snapshot.pool
            and ratings == self.snapshot.ratings
        )

    def _build_result(self, *, command: RequestBuild) -> BuildResult:
        state = self._active_draft_state()
        if state is None:
            set_code = self._active_set_code()
            pool_grp_ids = self._transient_pool_grp_ids
            account = self.snapshot.active_account
            account_id = None if account is None else account.account_id
            draft_id = None
        else:
            set_code = state.set_code
            pool_grp_ids = state.pool_grp_ids
            account_id = state.account_id
            draft_id = state.draft_id

        if set_code is None or (state is None and not pool_grp_ids):
            raise DeckBuilderError("Deck build unavailable: no picked cards yet.")
        if self._card_database is None:
            raise DeckBuilderError("Deck build unavailable: card metadata is not ready.")

        pool = BuildPool(
            set_code=set_code,
            pool_grp_ids=pool_grp_ids,
            source_label="live draft",
            account_id=account_id,
            draft_id=draft_id,
        )
        selection, build_sheet = build_deck_from_pool(
            pool=pool,
            card_database=self._card_database,
            ratings_data=self._ratings_data_for_scoring(set_code=set_code),
            forced_pair=command.pair_override,
            allow_splash=command.allow_splash,
            set_profile=self._set_profile,
        )
        spell_selection = build_sheet.spell_selection
        mana_base = build_sheet.mana_base
        average_mana_value = (
            None
            if any(spell.card.mana_value is None for spell in spell_selection.spells)
            else mana_base.average_mana_value
        )
        return BuildResult(
            selected_pair=selection.chosen.pair,
            pair_options=tuple(
                BuildPairOption(
                    pair=score.pair,
                    score=score.blended_score,
                    selected=score.pair == selection.chosen.pair,
                    automatic=score.pair == selection.automatic.pair,
                    playable_count=score.playable_count,
                    playable_score_sum=score.playable_score_sum,
                    pair_win_rate=score.pair_win_rate,
                )
                for score in selection.ranked_scores
            ),
            spells=_build_cards(
                cards=tuple(
                    sorted(
                        spell_selection.spells,
                        key=_build_spell_curve_sort_key,
                    )
                ),
            ),
            lands=(
                tuple(
                    BuildLand(
                        name=land.card.name,
                        quantity=1,
                        source_colors=land.source_colors,
                        card=_card_view(card=land.card),
                    )
                    for land in mana_base.nonbasic_lands
                )
                + tuple(
                    BuildLand(
                        name=land.name,
                        quantity=land.count,
                        source_colors=(land.color,),
                    )
                    for land in mana_base.basic_lands
                )
            ),
            bench=_build_cards(cards=spell_selection.bench),
            deck_size=mana_base.total_cards,
            average_mana_value=average_mana_value,
            pair_override=selection.forced_pair,
            spell_count=spell_selection.counts.total,
            land_count=mana_base.total_cards - spell_selection.counts.total,
            creature_count=spell_selection.counts.creatures,
            instant_count=spell_selection.counts.instants,
            warnings=(
                tuple(
                    f"Applied spell-selection relaxation: {relaxation}"
                    for relaxation in spell_selection.applied_relaxations
                )
                + mana_base.caveats
            ),
            role_ledger=build_sheet.role_ledger,
            domain_pool=pool,
            domain_selection=selection,
            domain_spell_selection=spell_selection,
            domain_mana_base=mana_base,
        )

    def _request_backtest(self, *, command: RequestBacktest) -> None:
        with self._state_lock:
            self._last_backtest_request = command
            self._backtest_request_generation += 1
            generation = self._backtest_request_generation
            account = self.snapshot.active_account
            draft = self.snapshot.draft
            ranking_mode = self._ranking_mode
            splash_enabled = self._splash_enabled
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    progress=ProgressState(
                        operation=OperationKind.BACKTEST,
                        message="Running backtest",
                    ),
                    errors=self._without_operation_error(
                        operation=OperationKind.BACKTEST,
                    ),
                )
            )
        try:
            backtest = self._backtest_result(command=command)
        except Exception as error:
            session_error = SessionError(
                error_id="backtest",
                code="backtest_failed",
                message=f"Backtest failed: {error}",
                recoverable=True,
                operation=OperationKind.BACKTEST,
            )
            with self._state_lock:
                if not self._backtest_request_is_current(
                    generation=generation,
                    account=account,
                    draft=draft,
                    ranking_mode=ranking_mode,
                    splash_enabled=splash_enabled,
                ):
                    return
                self._publish(
                    snapshot=replace(
                        self.snapshot,
                        progress=self._progress_after_operation(
                            operation=OperationKind.BACKTEST,
                        ),
                        errors=self._with_error(error=session_error),
                        backtest=None,
                    )
                )
            return

        with self._state_lock:
            if not self._backtest_request_is_current(
                generation=generation,
                account=account,
                draft=draft,
                ranking_mode=ranking_mode,
                splash_enabled=splash_enabled,
            ):
                return
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    progress=self._progress_after_operation(
                        operation=OperationKind.BACKTEST,
                    ),
                    errors=self._without_operation_error(
                        operation=OperationKind.BACKTEST,
                    ),
                    backtest=backtest,
                )
            )

    def _backtest_request_is_current(
        self,
        *,
        generation: int,
        account: AccountIdentity | None,
        draft: DraftIdentity | None,
        ranking_mode: RankingMode,
        splash_enabled: bool,
    ) -> bool:
        return (
            generation == self._backtest_request_generation
            and account == self.snapshot.active_account
            and draft == self.snapshot.draft
            and ranking_mode == self._ranking_mode
            and splash_enabled == self._splash_enabled
        )

    def _progress_after_operation(
        self,
        *,
        operation: OperationKind,
    ) -> ProgressState | None:
        progress = self.snapshot.progress
        if progress is not None and progress.operation != operation:
            return progress

        return None

    def _retire_derived_operations(self) -> tuple[SessionError, ...]:
        self._build_request_generation += 1
        self._backtest_request_generation += 1
        self._last_build_request = None
        self._last_backtest_request = None
        return tuple(
            error
            for error in self.snapshot.errors
            if error.operation not in {OperationKind.BUILD, OperationKind.BACKTEST}
        )

    def _backtest_result(self, *, command: RequestBacktest) -> BacktestResult:
        if self._card_database is None:
            raise ValueError("Card metadata is not ready.")

        account_id, draft_id = self._backtest_identity(command=command)
        state = load_persisted_backtest_state(
            app_dir=self.store.app_dir,
            account_id=account_id,
            draft_id=draft_id,
        )
        report = generate_backtest_report(
            state=state,
            card_database=self._card_database,
            ratings_data=self._ratings_data_for_scoring(set_code=state.set_code),
            ranking_mode=self._ranking_mode,
            splash_enabled=self._splash_enabled,
            set_profile=self._set_profile,
        )
        return _backtest_result(report=report)

    def _backtest_identity(
        self,
        *,
        command: RequestBacktest,
    ) -> tuple[str | None, str | None]:
        active_draft = self.snapshot.draft
        active_account = self.snapshot.active_account
        account_id = command.account_id
        if account_id is None and active_account is not None:
            account_id = active_account.account_id

        draft_id = command.draft_id
        if (
            draft_id is None
            and active_draft is not None
            and active_draft.account_id == account_id
        ):
            draft_id = active_draft.draft_id

        return account_id, draft_id

    def _load_card_data_for_set(
        self,
        *,
        set_code: str,
        allow_network: bool,
    ) -> bool:
        """Synchronously load one selected set and publish readiness state."""

        normalized_set_code = set_code.upper()
        if self._card_database is not None and (
            self._set_card_data_loader is None
            or self._card_database_set_code == normalized_set_code
        ):
            return True
        if (
            self._set_card_data_loader is not None
            and self._card_database is not None
            and self._card_database_set_code != normalized_set_code
        ):
            self._prepare_card_database_for_set(set_code=normalized_set_code)
        loader = self._set_card_data_loader
        if loader is None:
            return False
        self._card_data_requested_set_code = normalized_set_code

        effective_allow_network = allow_network and self._card_data_network_open
        self._publish(
            snapshot=replace(
                self.snapshot,
                card_data=replace(
                    self.snapshot.card_data,
                    phase=DataLoadPhase.LOADING,
                    message="Loading card metadata.",
                ),
                progress=ProgressState(
                    operation=OperationKind.CARD_DATA,
                    message="Loading card metadata",
                ),
                errors=self._without_operation_error(
                    operation=OperationKind.CARD_DATA,
                ),
            )
        )
        try:
            database = loader(
                normalized_set_code,
                allow_network=effective_allow_network,
            )
            self._validate_card_database_for_set(
                database=database,
                set_code=normalized_set_code,
            )
        except Exception as error:
            self._finish_card_data_load(
                database=None,
                set_code=normalized_set_code,
                error_message=str(error),
            )
            return False

        self._finish_card_data_load(
            database=database,
            set_code=normalized_set_code,
            error_message=None,
        )
        return True

    def stop(self) -> LiveSessionSnapshot:
        """Publish the terminal stopped state without owning process shutdown.
        Frontends remain responsible for timers, workers, and event-loop teardown.
        """

        with self._state_lock:
            self._profile_refresh_generation += 1
            self._profile_refresh_request = None
            self._card_image_generation += 1
            self._card_image_request = None
            self._retire_recommendation_images()
            self._retire_recent_pick_images()
            card_image = self.snapshot.card_image
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    status=ApplicationStatus(
                        phase=ApplicationPhase.STOPPED,
                        message="Draft Omen stopped.",
                    ),
                    card_image=(
                        CardImageState(
                            grp_id=card_image.grp_id,
                            message="Card image loading stopped.",
                        )
                        if card_image.phase == DataLoadPhase.LOADING
                        else card_image
                    ),
                )
            )
        return self.snapshot

    def _initial_ratings_state(self) -> RatingsState:
        if self._ratings_configured():
            return RatingsState(
                phase=DataLoadPhase.IDLE,
                message="17Lands ratings are ready to load.",
            )

        return RatingsState()

    def _ratings_configured(self) -> bool:
        return any(
            loader is not None
            for loader in (
                self._ratings_loader,
                self._ratings_loader_factory,
                self._ratings_progress_loader,
                self._ratings_progress_loader_factory,
            )
        )

    def _configure_ratings_loader_for_card_database(self) -> None:
        database = self._card_database
        if database is None:
            return

        if self._ratings_loader_factory is not None:
            self._ratings_loader = self._ratings_loader_factory(database)
        if self._ratings_progress_loader_factory is not None:
            self._ratings_progress_loader = self._ratings_progress_loader_factory(
                database
            )

    @staticmethod
    def _validate_card_database_for_set(
        *,
        database: CardDatabase,
        set_code: str,
    ) -> None:
        """Reject malformed or mixed-set results from a configured loader."""

        if not isinstance(database, CardDatabase):
            raise TypeError("card data loader must return a CardDatabase.")
        if not database.cards:
            raise ValueError(f"Card metadata for {set_code} contains no cards.")
        try:
            cards = tuple(database.cards.items())
        except AttributeError as error:
            raise TypeError("Card database cards must be a mapping.") from error
        observed_set_codes: set[str] = set()
        for grp_id, card in cards:
            if not isinstance(grp_id, int) or isinstance(grp_id, bool):
                raise TypeError("Card database keys must be integer grpIds.")
            if not isinstance(card, CardInfo):
                raise TypeError("Card database values must be CardInfo values.")
            if card.grp_id != grp_id:
                raise ValueError("Card database key does not match card grpId.")
            if card.set_code is None:
                raise ValueError("Card database cards must declare their set code.")
            if not isinstance(card.set_code, str):
                raise TypeError("Card database card set codes must be strings.")
            observed_set_codes.add(card.set_code.casefold())
        normalized_set_code = set_code.casefold()
        if observed_set_codes and observed_set_codes != {normalized_set_code}:
            raise ValueError(
                f"Card metadata contains cards outside selected set {set_code}."
            )

    def _prepare_card_database_for_set(
        self,
        *,
        set_code: str,
        force_reload: bool = False,
    ) -> None:
        """Discard a dynamic database when the selected set changes."""

        if self._set_card_data_loader is None:
            return
        normalized_set_code = set_code.upper()
        if (
            not force_reload
            and self._card_database is not None
            and self._card_database_set_code == normalized_set_code
        ):
            return
        if self._card_database is None and self._card_database_set_code is None:
            return
        self._card_database = None
        self._card_database_set_code = None
        if self._ratings_loader_factory is not None:
            self._ratings_loader = None
        if self._ratings_progress_loader_factory is not None:
            self._ratings_progress_loader = None
        self._publish(
            snapshot=replace(
                self.snapshot,
                card_data=CardDataState(
                    phase=DataLoadPhase.IDLE,
                    message="Card metadata is ready to load.",
                ),
                progress=None,
                errors=self._without_error_id(error_id="card-data"),
            )
        )

    def _prepare_card_data_for_draft_start(self, *, set_code: str) -> None:
        """Close static networking and make one local-only late-load attempt."""

        self._prepare_card_database_for_set(set_code=set_code)
        self._card_data_network_open = False
        if (
            self._card_database is not None
            or self._set_card_data_loader is None
            or self._card_data_local_lookup_attempted
        ):
            return
        self._card_data_local_lookup_attempted = True
        self._load_card_data_for_set(set_code=set_code, allow_network=False)


    def _finish_card_data_load(
        self,
        *,
        database: CardDatabase | None,
        set_code: str,
        error_message: str | None,
    ) -> None:
        if database is None:
            if self._set_card_data_loader is not None:
                self._card_database = None
                self._card_database_set_code = None
            detail = error_message or "no card metadata was returned"
            session_error = SessionError(
                error_id="card-data",
                code="card_data_unavailable",
                message=f"Card metadata failed to load: {detail}.",
                recoverable=True,
                operation=OperationKind.CARD_DATA,
            )
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    card_data=replace(
                        self.snapshot.card_data,
                        phase=DataLoadPhase.FAILED,
                        message=session_error.message,
                    ),
                    progress=None,
                    errors=self._with_error(error=session_error),
                )
            )
            return

        self._card_database = database
        self._card_database_set_code = set_code.upper()
        self._configure_ratings_loader_for_card_database()
        self._publish(
            snapshot=replace(
                self.snapshot,
                card_data=CardDataState(
                    phase=DataLoadPhase.READY,
                    message="Card metadata is ready.",
                    last_successful_update=_last_successful_update(
                        database=database
                    ),
                ),
                progress=None,
                errors=self._without_error_id(error_id="card-data"),
                pool=self._pool_state_from_active_draft(),
            )
        )
        set_code = self._active_set_code()
        if set_code is not None:
            self._ensure_ratings_loaded(set_code=set_code)
        self._score_current_pack()

    def _ensure_ratings_loaded(self, *, set_code: str) -> None:
        normalized_set_code = set_code.upper()
        existing = self._ratings_state_by_set.get(normalized_set_code)
        waiting_for_card_data = (
            existing is not None
            and existing.phase == DataLoadPhase.IDLE
            and self._card_database is None
        )
        if existing is not None and (
            existing.phase != DataLoadPhase.IDLE or waiting_for_card_data
        ):
            self._publish_active_ratings_state(state=existing)
            return

        if not self._ratings_configured():
            state = RatingsState(
                set_code=normalized_set_code,
                phase=DataLoadPhase.UNAVAILABLE,
                message=(
                    f"17Lands ratings are unavailable for {normalized_set_code}; "
                    "neutral-prior scores are active."
                ),
            )
            self._ratings_state_by_set[normalized_set_code] = state
            self._publish_active_ratings_state(state=state)
            return

        if (
            self._card_database is None
            and (
                self._ratings_loader_factory is not None
                or self._ratings_progress_loader_factory is not None
            )
        ):
            state = RatingsState(
                set_code=normalized_set_code,
                phase=DataLoadPhase.IDLE,
                message=(
                    f"17Lands ratings for {normalized_set_code} are waiting for "
                    "card metadata."
                ),
            )
            self._ratings_state_by_set[normalized_set_code] = state
            self._publish_active_ratings_state(state=state)
            return

        if self._ratings_cache_checker is not None:
            try:
                cached = self._ratings_cache_checker(normalized_set_code)
            except Exception as error:
                self._finish_ratings_load(
                    set_code=normalized_set_code,
                    ratings_data=None,
                    error_message=str(error),
                )
                return

            if not cached:
                state = RatingsState(
                    set_code=normalized_set_code,
                    phase=DataLoadPhase.MISSING,
                    message=(
                        f"No local 17Lands data for {normalized_set_code}; "
                        "neutral-prior scores are active."
                    ),
                )
                self._ratings_data_by_set[normalized_set_code] = None
                self._ratings_state_by_set[normalized_set_code] = state
                self._publish_active_ratings_state(state=state)
                return

        self._load_ratings(set_code=normalized_set_code, refresh=False)

    def _request_ratings_download(self, *, set_code: str) -> None:
        normalized_set_code = set_code.upper()
        active_set_code = self._active_set_code()
        if active_set_code is None:
            raise ValueError("Ratings downloads require an active draft set.")
        if normalized_set_code != active_set_code:
            raise ValueError(
                f"Ratings download set {normalized_set_code!r} does not match "
                f"active set {active_set_code!r}."
            )

        self._load_ratings(set_code=normalized_set_code, refresh=True)

    def _load_ratings(self, *, set_code: str, refresh: bool) -> None:
        if self._ratings_loader is None and self._ratings_progress_loader is None:
            previous_state = self._ratings_state_by_set.get(set_code)
            state = RatingsState(
                set_code=set_code,
                phase=DataLoadPhase.UNAVAILABLE,
                message=f"No 17Lands ratings loader is available for {set_code}.",
                last_successful_update=(
                    None
                    if previous_state is None
                    else previous_state.last_successful_update
                ),
            )
            self._ratings_state_by_set[set_code] = state
            self._publish_active_ratings_state(state=state)
            return

        if not self._begin_ratings_load(set_code=set_code):
            return

        ratings_data = None
        error_message = None
        try:
            if self._ratings_progress_loader is not None:
                ratings_data = self._ratings_progress_loader(
                    set_code,
                    lambda progress: self._update_ratings_progress(
                        set_code=set_code,
                        progress=progress,
                    ),
                    refresh=refresh,
                )
            elif self._ratings_loader is not None:
                ratings_data = self._ratings_loader(set_code)
        except Exception as error:
            error_message = str(error)

        self._finish_ratings_load(
            set_code=set_code,
            ratings_data=ratings_data,
            error_message=error_message,
        )

    def _begin_ratings_load(self, *, set_code: str) -> bool:
        with self._state_lock:
            if set_code in self._loading_rating_sets:
                return False

            previous_state = self._ratings_state_by_set.get(set_code)
            loading_state = RatingsState(
                set_code=set_code,
                phase=DataLoadPhase.LOADING,
                message=f"Checking 17Lands data for {set_code}.",
                last_successful_update=(
                    None
                    if previous_state is None
                    else previous_state.last_successful_update
                ),
            )
            loading_progress = ProgressState(
                operation=OperationKind.RATINGS,
                message=f"Checking 17Lands data for {set_code}",
                completed=0,
            )

            self._loading_rating_sets.add(set_code)
            self._ratings_state_by_set[set_code] = loading_state
            self._ratings_progress_by_set[set_code] = loading_progress
            self._ratings_errors_by_set.pop(set_code, None)
            if self._active_set_code_value == set_code:
                self._publish(
                    snapshot=replace(
                        self.snapshot,
                        ratings=loading_state,
                        progress=loading_progress,
                        errors=self._without_operation_error(
                            operation=OperationKind.RATINGS,
                        ),
                    )
                )
                self._score_current_pack()

        return True

    def _update_ratings_progress(
        self,
        *,
        set_code: str,
        progress: SeventeenLandsDownloadProgress,
    ) -> None:
        with self._state_lock:
            state = self._ratings_state_by_set.get(set_code)
            if state is None or state.phase != DataLoadPhase.LOADING:
                return

            next_state = replace(state, message=progress.message)
            next_progress = ProgressState(
                operation=OperationKind.RATINGS,
                message=progress.message,
                completed=progress.completed_requests,
                total=progress.total_requests,
            )
            self._ratings_state_by_set[set_code] = next_state
            self._ratings_progress_by_set[set_code] = next_progress
            if self._active_set_code_value != set_code:
                return

            self._publish(
                snapshot=replace(
                    self.snapshot,
                    ratings=next_state,
                    progress=next_progress,
                )
            )

    def _finish_ratings_load(
        self,
        *,
        set_code: str,
        ratings_data: SeventeenLandsData | None,
        error_message: str | None,
    ) -> None:
        with self._state_lock:
            self._finish_ratings_load_locked(
                set_code=set_code,
                ratings_data=ratings_data,
                error_message=error_message,
            )

    def _finish_ratings_load_locked(
        self,
        *,
        set_code: str,
        ratings_data: SeventeenLandsData | None,
        error_message: str | None,
    ) -> None:
        self._loading_rating_sets.discard(set_code)
        previous_state = self._ratings_state_by_set.get(set_code)
        if ratings_data is None:
            detail = error_message or "no ratings were returned"
            session_error = SessionError(
                error_id=self._ratings_error_id(set_code=set_code),
                code="ratings_unavailable",
                message=f"17Lands ratings failed for {set_code}: {detail}.",
                recoverable=True,
                operation=OperationKind.RATINGS,
            )
            state = RatingsState(
                set_code=set_code,
                phase=DataLoadPhase.FAILED,
                message=(
                    f"{session_error.message} Neutral-prior scores remain active."
                ),
                last_successful_update=(
                    None
                    if previous_state is None
                    else previous_state.last_successful_update
                ),
            )
            self._ratings_data_by_set[set_code] = None
            self._ratings_state_by_set[set_code] = state
            self._ratings_progress_by_set.pop(set_code, None)
            self._ratings_errors_by_set[set_code] = session_error
            if self._active_set_code_value != set_code:
                return

            failed_snapshot = replace(
                self.snapshot,
                ratings=state,
                progress=None,
                errors=self._with_error(error=session_error),
            )
            if not self._score_current_pack_locked(snapshot=failed_snapshot):
                self._publish(snapshot=failed_snapshot)
            return

        if ratings_data.pair_card_ratings_loader is not None:
            ratings_data = replace(ratings_data, pair_card_ratings_loader=None)
        self._ratings_data_by_set[set_code] = ratings_data
        state = RatingsState(
            set_code=set_code,
            phase=DataLoadPhase.READY,
            message=f"17Lands ratings are ready for {set_code}.",
            last_successful_update=_ratings_last_successful_update(
                ratings_data=ratings_data
            ),
        )
        self._ratings_state_by_set[set_code] = state
        self._ratings_progress_by_set.pop(set_code, None)
        self._ratings_errors_by_set.pop(set_code, None)
        if self._active_set_code_value != set_code:
            return

        ready_snapshot = replace(
            self.snapshot,
            ratings=state,
            progress=None,
            errors=self._without_error_id(
                error_id=self._ratings_error_id(set_code=set_code),
            ),
        )
        if not self._score_current_pack_locked(snapshot=ready_snapshot):
            self._publish(snapshot=ready_snapshot)

    def _publish_active_ratings_state(self, *, state: RatingsState) -> None:
        with self._state_lock:
            set_code = state.set_code
            if set_code != self._active_set_code_value:
                return
            if (
                set_code is not None
                and self._ratings_state_by_set.get(set_code) != state
            ):
                return

            progress = (
                None
                if set_code is None
                else self._ratings_progress_by_set.get(set_code)
            )
            errors = self._without_operation_error(
                operation=OperationKind.RATINGS
            )
            ratings_error = (
                None
                if set_code is None
                else self._ratings_errors_by_set.get(set_code)
            )
            if ratings_error is not None:
                errors += (ratings_error,)
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    ratings=state,
                    progress=progress,
                    errors=errors,
                )
            )

    def _card_image_focuses_current_build(
        self,
        *,
        card_image: CardImageState,
        build: BuildResult | None,
    ) -> bool:
        """Return whether the shared image still belongs to the published build."""

        grp_id = card_image.grp_id
        if grp_id is None or build is None:
            return False
        return any(entry.card.grp_id == grp_id for entry in build.spells) or any(
            entry.card.grp_id == grp_id for entry in build.bench
        )

    def _score_current_pack(self) -> None:
        with self._state_lock:
            self._score_current_pack_locked()

    def _score_current_pack_locked(
        self,
        *,
        snapshot: LiveSessionSnapshot | None = None,
    ) -> bool:
        event = self._current_pack_event
        database = self._card_database
        if (
            event is None
            or database is None
            or event.set_code.upper() != self._active_set_code_value
        ):
            return False

        ratings_data = self._ratings_data_for_scoring(set_code=event.set_code)
        engine = PickEngine(
            ratings_data=ratings_data,
            splash_enabled=self._splash_enabled,
            set_profile=self._set_profile,
        )
        global_pick_index = _draft_pick_index(event=event)
        scored_pack = engine.score_pack(
            offered_grp_ids=event.offered_grp_ids,
            card_database=database,
            pool_grp_ids=event.pool_grp_ids,
            pick_index=global_pick_index,
            pack_number=event.pack_number,
            pick_number=event.pick_number,
            global_pick_index=global_pick_index,
            estimated_remaining_picks=max(0, EXPECTED_TOTAL_PICKS - global_pick_index),
        )
        self._current_scored_pack = scored_pack
        recommendations = self._recommendation_state(scored_pack=scored_pack)
        ratings = self._ratings_state_after_scoring(
            set_code=event.set_code,
            recommendations=recommendations,
        )
        state = self._active_draft_state()
        if state is not None:
            self.audit_store.record_decision(
                state=state,
                event=event,
                scored_pack=scored_pack,
                config=engine.config,
                ratings_data=ratings_data,
            )
        previous_snapshot = self.snapshot if snapshot is None else snapshot
        build_image_focus = self._card_image_focuses_current_build(
            card_image=previous_snapshot.card_image,
            build=previous_snapshot.build,
        )
        card_image = (
            previous_snapshot.card_image
            if build_image_focus
            or previous_snapshot.card_image.grp_id == recommendations.selected_grp_id
            else self._retire_card_image()
        )
        self._prepare_recommendation_image_requests(
            pack=event,
            recommendations=recommendations,
        )
        self._publish(
            snapshot=replace(
                previous_snapshot,
                ratings=ratings,
                recommendations=recommendations,
                pool=self._pool_state(
                    pool_grp_ids=event.pool_grp_ids,
                    scored_pack=scored_pack,
                ),
                card_image=card_image,
            )
        )
        if not build_image_focus:
            self._start_focused_card_image_load(
                grp_id=recommendations.selected_grp_id,
            )
        return True

    def _recommendation_state(self, *, scored_pack: ScoredPack) -> RecommendationState:
        ranked_cards = rank_scored_cards(
            cards=scored_pack.cards,
            ranking_mode=self._ranking_mode,
        )
        selected_grp_id = self.snapshot.recommendations.selected_grp_id
        available_grp_ids = {card.card.grp_id for card in ranked_cards}
        if selected_grp_id not in available_grp_ids:
            selected_grp_id = ranked_cards[0].card.grp_id if ranked_cards else None
        return RecommendationState(
            ranking_mode=self._ranking_mode,
            splash_enabled=self._splash_enabled,
            cards=tuple(
                self._recommendation(
                    rank=rank,
                    scored_card=scored_card,
                    inferred_pair=scored_pack.commitment.inferred_pair,
                )
                for rank, scored_card in enumerate(ranked_cards, start=1)
            ),
            selected_grp_id=selected_grp_id,
            source_summary=scored_pack.source_summary,
            confidence_summary=recommendation_confidence_summary(
                cards=ranked_cards,
                ranking_mode=self._ranking_mode,
                phase=scored_pack.commitment.phase,
            ),
        )

    def _recommendation(
        self,
        *,
        rank: int,
        scored_card: ScoredCard,
        inferred_pair: str | None = None,
    ) -> Recommendation:
        return Recommendation(
            rank=rank,
            card=self._card_view(card=scored_card.card),
            score=scored_card.score,
            win_rate=scored_card.rating.gih_win_rate,
            average_last_seen_at=scored_card.rating.average_last_seen_at,
            source_label=scored_card.source_label,
            color_fit=scored_card.color_fit,
            no_data=scored_card.no_data,
            contextual_breakdown=scored_card.contextual_breakdown,
            contextual_evidence=scored_card.contextual_evidence,
            contextual_pair=scored_card.contextual_pair,
            contextual_theme=scored_card.contextual_theme,
            contextual_profile_maturity=scored_card.contextual_profile_maturity,
            contextual_profile_confidence=scored_card.contextual_profile_confidence,
            letter_grade=scored_card.rating.letter_grade,
            explanation=recommendation_explanation(
                scored_card=scored_card,
                inferred_pair=inferred_pair,
            ),
        )

    def _card_image_uri(self, *, card: CardInfo) -> str | None:
        """Resolve a card image URI without doing metadata network work."""

        remembered_uri = self._card_image_uris_by_grp_id.get(card.grp_id)
        if remembered_uri is not None:
            return remembered_uri
        card_image_service = self._card_image_service
        database = self._card_database
        if card_image_service is None or database is None:
            return None
        return card_image_service.resolve_image_uri(
            card=card,
            card_database=database,
        )

    def _card_view(self, *, card: CardInfo) -> CardView:
        """Project a card with any image path already resolved by this session.
        Cache lookups consult local memory and the filesystem but never network.
        """

        image_uri = self._card_image_uri(card=card)
        image_path = (
            self._card_image_paths_by_grp_id.get(card.grp_id)
            if image_uri is None
            else self._cached_card_image_path(image_uri=image_uri)
        )
        return _card_view(
            card=card,
            image_path=None if image_path is None else str(image_path),
        )

    def _recommendation_image_pack_key(
        self,
        *,
        pack: PackOfferedEvent | None,
        snapshot: LiveSessionSnapshot | None = None,
    ) -> tuple[PackOfferedEvent | None, str | None, str | None]:
        current_snapshot = self.snapshot if snapshot is None else snapshot
        active_account = current_snapshot.active_account
        draft = current_snapshot.draft
        return (
            pack,
            None if active_account is None else active_account.account_id,
            None if draft is None else draft.draft_id,
        )

    def _retire_recommendation_images(self) -> None:
        """Invalidate all current-pack recommendation image work."""

        self._recommendation_image_generation += 1
        self._recommendation_image_pack = None
        self._recommendation_image_requests.clear()
        self._recommendation_image_failures.clear()

    def _cached_card_image_path(self, *, image_uri: str) -> Path | None:
        """Return a known or on-disk cached image without network access."""

        cached_path = self._card_image_paths_by_uri.get(image_uri)
        if cached_path is not None:
            return cached_path

        card_image_service = self._card_image_service
        if card_image_service is None:
            return None
        cached_path_method = getattr(card_image_service, "cached_path", None)
        if not callable(cached_path_method):
            return None
        cached_path = cached_path_method(image_uri=image_uri)
        if not isinstance(cached_path, Path):
            cached_path = Path(cached_path)
        if not cached_path.is_file():
            return None
        self._card_image_paths_by_uri[image_uri] = cached_path
        return cached_path

    def _prepare_recommendation_image_requests(
        self,
        *,
        pack: PackOfferedEvent,
        recommendations: RecommendationState,
    ) -> None:
        """Queue uncached recommendation thumbnails for one current pack."""

        pack_key = self._recommendation_image_pack_key(pack=pack)
        if pack_key != self._recommendation_image_pack:
            self._recommendation_image_generation += 1
            self._recommendation_image_pack = pack_key
            self._recommendation_image_requests.clear()
            self._recommendation_image_failures.clear()

        database = self._card_database
        if self._card_image_service is None or database is None:
            return

        queued = {
            (request.grp_id, request.image_uri)
            for request in self._recommendation_image_requests
        }
        for recommendation in recommendations.cards:
            if recommendation.card.image_path is not None:
                continue
            card = database.lookup(grp_id=recommendation.card.grp_id)
            image_uri = self._card_image_uri(card=card)
            if image_uri is None and card.unknown:
                continue
            if image_uri is not None and self._cached_card_image_path(
                image_uri=image_uri
            ) is not None:
                continue
            request_key = (recommendation.card.grp_id, image_uri)
            if (
                request_key in self._recommendation_image_failures
                or request_key in queued
            ):
                continue
            self._recommendation_image_requests.append(
                CardImageRequest(
                    generation=self._recommendation_image_generation,
                    grp_id=recommendation.card.grp_id,
                    image_uri=image_uri,
                )
            )
            queued.add(request_key)

    def _retire_recent_pick_images(self) -> None:
        """Invalidate all gallery work independently from focused-card work."""

        self._recent_pick_image_generation += 1
        self._recent_pick_image_requests.clear()
        self._recent_pick_image_states.clear()
        self._recent_pick_pool_grp_ids = None

    def _prepare_recent_pick_images(
        self,
        *,
        pool_grp_ids: tuple[int, ...],
    ) -> tuple[RecentPick, ...]:
        database = self._card_database
        if database is None:
            return ()

        recent_grp_ids = tuple(reversed(pool_grp_ids[-RECENT_PICK_LIMIT:]))
        if recent_grp_ids != self._recent_pick_pool_grp_ids:
            previous_states = self._recent_pick_image_states
            self._recent_pick_image_generation += 1
            self._recent_pick_image_requests.clear()
            active_grp_ids = set(recent_grp_ids)
            self._recent_pick_image_states = {
                grp_id: state
                for grp_id, state in previous_states.items()
                if grp_id in active_grp_ids
            }
            self._recent_pick_pool_grp_ids = recent_grp_ids

        recent_picks: list[RecentPick] = []
        queued_grp_ids = {
            request.grp_id for request in self._recent_pick_image_requests
        }
        for grp_id in dict.fromkeys(recent_grp_ids):
            card = database.lookup(grp_id=grp_id)
            image_uri = self._card_image_uri(card=card)
            cached_path = (
                None
                if image_uri is None
                else self._cached_card_image_path(image_uri=image_uri)
            )
            card_view = _card_view(
                card=card,
                image_path=None if cached_path is None else str(cached_path),
            )
            image = self._recent_pick_image_states.get(grp_id)
            if image is None:
                if cached_path is not None:
                    image = CardImageState(
                        grp_id=grp_id,
                        image_path=str(cached_path),
                        phase=DataLoadPhase.READY,
                        message="Card image ready.",
                    )
                elif image_uri is None:
                    image = CardImageState(
                        grp_id=grp_id,
                        phase=DataLoadPhase.UNAVAILABLE,
                        message=f"No image is available for {card.name}.",
                    )
                else:
                    image = CardImageState(
                        grp_id=grp_id,
                        phase=DataLoadPhase.LOADING,
                        message=f"Loading image for {card.name}.",
                    )
                self._recent_pick_image_states[grp_id] = image

            if (
                image.phase == DataLoadPhase.LOADING
                and image_uri is not None
                and grp_id not in queued_grp_ids
            ):
                self._recent_pick_image_requests.append(
                    CardImageRequest(
                        generation=self._recent_pick_image_generation,
                        grp_id=grp_id,
                        image_uri=image_uri,
                    )
                )
                queued_grp_ids.add(grp_id)

            recent_picks.append(
                RecentPick(
                    card=replace(card_view, image_path=image.image_path),
                    image=image,
                )
            )

        return tuple(
            pick
            for grp_id in recent_grp_ids
            for pick in recent_picks
            if pick.card.grp_id == grp_id
        )

    def _publish_recent_pick_projection(self) -> None:
        """Publish only gallery entries after an adapter fetch completes."""

        pool = self.snapshot.pool
        recent_picks = tuple(
            RecentPick(
                card=(
                    pick.card
                    if (image := self._recent_pick_image_states.get(pick.card.grp_id))
                    is None
                    or image.image_path is None
                    else replace(pick.card, image_path=image.image_path)
                ),
                image=(
                    pick.image
                    if (image := self._recent_pick_image_states.get(pick.card.grp_id))
                    is None
                    else image
                ),
            )
            for pick in pool.recent_picks
        )
        self._publish(
            snapshot=replace(
                self.snapshot,
                pool=replace(pool, recent_picks=recent_picks),
            )
        )

    def _start_focused_card_image_load(
        self,
        *,
        grp_id: int | None,
    ) -> None:
        """Publish one focused-card image request for the adapter worker.
        Metadata resolution and fetching happen only in that worker.
        """

        if self.snapshot.status.phase == ApplicationPhase.STOPPED:
            return

        card_image = self.snapshot.card_image
        if (
            card_image.grp_id == grp_id
            and card_image.phase in {DataLoadPhase.LOADING, DataLoadPhase.READY}
        ):
            return

        self._retire_card_image()
        card_image_service = self._card_image_service
        database = self._card_database
        if card_image_service is None or grp_id is None or database is None:
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    card_image=CardImageState(
                        grp_id=grp_id,
                        message="No selected card image is available.",
                    ),
                )
            )
            return

        card = database.lookup(grp_id=grp_id)
        self._card_image_generation += 1
        generation = self._card_image_generation
        self._publish(
            snapshot=replace(
                self.snapshot,
                card_image=CardImageState(
                    grp_id=grp_id,
                    phase=DataLoadPhase.LOADING,
                    message=f"Loading image for {card.name}.",
                ),
            )
        )
        image_uri = self._card_image_uri(card=card)
        if image_uri is not None:
            cached_path = self._cached_card_image_path(image_uri=image_uri)
            if cached_path is not None:
                with self._state_lock:
                    self._retire_recommendation_requests_for_image(
                        grp_id=grp_id,
                        image_uri=image_uri,
                    )
                self._publish_selected_card_image(
                    grp_id=grp_id,
                    image_path=cached_path,
                )
                return

        self._card_image_request = CardImageRequest(
            generation=generation,
            grp_id=grp_id,
            image_uri=image_uri,
        )

    def _publish_selected_card_image(self, *, grp_id: int, image_path: Path) -> None:
        """Publish the resolved focused-card path as a plain snapshot value."""

        recommendations = replace(
            self.snapshot.recommendations,
            cards=tuple(
                replace(
                    recommendation,
                    card=replace(recommendation.card, image_path=str(image_path)),
                )
                if recommendation.card.grp_id == grp_id
                else recommendation
                for recommendation in self.snapshot.recommendations.cards
            ),
        )
        self._publish(
            snapshot=replace(
                self.snapshot,
                recommendations=recommendations,
                card_image=CardImageState(
                    grp_id=grp_id,
                    image_path=str(image_path),
                    phase=DataLoadPhase.READY,
                    message="Card image ready.",
                ),
            )
        )

    def _ratings_state_after_scoring(
        self,
        *,
        set_code: str,
        recommendations: RecommendationState,
    ) -> RatingsState:
        normalized_set_code = set_code.upper()
        state = self._ratings_state_by_set.get(
            normalized_set_code,
            RatingsState(set_code=normalized_set_code),
        )
        rated_cards = sum(not card.no_data for card in recommendations.cards)
        next_state = replace(
            state,
            rated_cards=rated_cards,
            total_cards=len(recommendations.cards),
        )
        self._ratings_state_by_set[normalized_set_code] = next_state
        return next_state

    def _ratings_data_for_scoring(self, *, set_code: str) -> SeventeenLandsData | None:
        return self._ratings_data_by_set.get(set_code.upper())

    def _change_ranking(self, *, ranking_mode: str) -> None:
        with self._state_lock:
            self._ranking_mode = validate_ranking_mode(ranking_mode=ranking_mode)
            self._backtest_request_generation += 1
            self._last_backtest_request = None
            if self._current_scored_pack is None:
                recommendations = replace(
                    self.snapshot.recommendations,
                    ranking_mode=self._ranking_mode,
                )
            else:
                recommendations = self._recommendation_state(
                    scored_pack=self._current_scored_pack,
                )
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    recommendations=recommendations,
                    progress=self._progress_after_operation(
                        operation=OperationKind.BACKTEST,
                    ),
                    errors=self._without_operation_error(
                        operation=OperationKind.BACKTEST,
                    ),
                    backtest=None,
                )
            )

    def _choose_recommendation(self, *, grp_id: int) -> None:
        available_grp_ids = {
            recommendation.card.grp_id
            for recommendation in self.snapshot.recommendations.cards
        }
        if grp_id not in available_grp_ids:
            raise ValueError(f"Card {grp_id} is not in the current recommendations.")

        self._publish(
            snapshot=replace(
                self.snapshot,
                recommendations=replace(
                    self.snapshot.recommendations,
                    selected_grp_id=grp_id,
                ),
                card_image=self._retire_card_image(),
            )
        )
        self._start_focused_card_image_load(grp_id=grp_id)

    def _focus_build_card(self, *, grp_id: int) -> None:
        build = self.snapshot.build
        available_grp_ids = (
            set()
            if build is None
            else {
                entry.card.grp_id
                for entry in (*build.spells, *build.bench)
            }
        )
        if grp_id not in available_grp_ids:
            raise ValueError(f"Card {grp_id} is not in the current build.")
        card_image = self.snapshot.card_image
        if (
            card_image.grp_id == grp_id
            and card_image.phase in {DataLoadPhase.LOADING, DataLoadPhase.READY}
        ):
            return
        if card_image.grp_id == grp_id:
            self._retire_card_image()
        self._start_focused_card_image_load(grp_id=grp_id)

    def _change_splash_preference(self, *, enabled: bool) -> None:
        with self._state_lock:
            if enabled == self._splash_enabled:
                return

            self._splash_enabled = enabled
            self._build_request_generation += 1
            self._backtest_request_generation += 1
            self._last_build_request = None
            self._last_backtest_request = None
            errors = tuple(
                error
                for error in self.snapshot.errors
                if error.operation
                not in {OperationKind.BUILD, OperationKind.BACKTEST}
            )
            progress = self.snapshot.progress
            if progress is not None and progress.operation in {
                OperationKind.BUILD,
                OperationKind.BACKTEST,
            }:
                progress = None
            if self._current_pack_event is None:
                self._publish(
                    snapshot=replace(
                        self.snapshot,
                        recommendations=replace(
                            self.snapshot.recommendations,
                            splash_enabled=enabled,
                        ),
                        progress=progress,
                        errors=errors,
                        build=None,
                        backtest=None,
                    )
                )
                return

            self._publish(
                snapshot=replace(
                    self.snapshot,
                    progress=progress,
                    errors=errors,
                    build=None,
                    backtest=None,
                )
            )
            self._score_current_pack_locked()

    def _dismiss_error(self, *, error_id: str) -> None:
        if not any(error.error_id == error_id for error in self.snapshot.errors):
            raise ValueError(f"Unknown session error {error_id!r}.")

        self._publish(
            snapshot=replace(
                self.snapshot,
                errors=self._without_error_id(error_id=error_id),
            )
        )

    def _retry_error(self, *, error_id: str) -> None:
        error = next(
            (
                candidate
                for candidate in self.snapshot.errors
                if candidate.error_id == error_id
            ),
            None,
        )
        if error is None:
            raise ValueError(f"Unknown session error {error_id!r}.")
        if not error.recoverable:
            raise ValueError(f"Session error {error_id!r} is not recoverable.")
        if error.operation == OperationKind.CARD_DATA:
            set_code = (
                self._active_set_code() or self._card_data_requested_set_code
            )
            if set_code is None:
                raise ValueError("Card-data retry requires an active draft set.")
            self._load_card_data_for_set(
                set_code=set_code,
                allow_network=self._card_data_network_open,
            )
            return
        if error.operation == OperationKind.RATINGS:
            prefix = "ratings:"
            if not error_id.startswith(prefix):
                raise ValueError(f"Ratings error {error_id!r} has no set code.")
            self._request_ratings_download(set_code=error_id.removeprefix(prefix))
            return
        if error.operation == OperationKind.BUILD:
            if self._last_build_request is None:
                raise ValueError(f"Build error {error_id!r} has no saved request.")
            self._request_build(command=self._last_build_request)
            return
        if error.operation == OperationKind.BACKTEST:
            if self._last_backtest_request is None:
                raise ValueError(f"Backtest error {error_id!r} has no saved request.")
            self._request_backtest(command=self._last_backtest_request)
            return

        raise ValueError(f"Session error {error_id!r} has no retry operation.")

    def _with_error(self, *, error: SessionError) -> tuple[SessionError, ...]:
        return self._without_error_id(error_id=error.error_id) + (error,)

    def _without_error_id(self, *, error_id: str) -> tuple[SessionError, ...]:
        return tuple(
            error for error in self.snapshot.errors if error.error_id != error_id
        )

    def _without_operation_error(
        self,
        *,
        operation: OperationKind,
    ) -> tuple[SessionError, ...]:
        return tuple(
            error for error in self.snapshot.errors if error.operation != operation
        )

    def _ratings_error_id(self, *, set_code: str) -> str:
        return f"ratings:{set_code.upper()}"

    def _active_set_code(self) -> str | None:
        with self._state_lock:
            return self._active_set_code_value

    def _clear_active_set_code_locked(self) -> None:
        self._profile_refresh_generation += 1
        self._profile_refresh_request = None
        self._active_set_code_value = None
        self._set_profile = None

    def _set_active_set_code(self, *, set_code: str | None) -> None:
        normalized_set_code = None if set_code is None else set_code.upper()
        with self._state_lock:
            if normalized_set_code is None:
                self._clear_active_set_code_locked()
                self._publish(
                    snapshot=replace(
                        self.snapshot,
                        set_profile=SetProfileState(),
                    )
                )
                return
            if normalized_set_code == self._active_set_code_value:
                return

            self._profile_refresh_generation += 1
            generation = self._profile_refresh_generation
            self._profile_refresh_request = None
            self._active_set_code_value = normalized_set_code

            if self._configured_set_profile is not None:
                self._set_profile = self._configured_set_profile
                profile_state = self._profile_state_for_profile(
                    profile=self._configured_set_profile,
                    set_code=normalized_set_code,
                    source="injected",
                    phase=DataLoadPhase.READY,
                )
            else:
                profile, source = self._load_local_profile_for_set(
                    set_code=normalized_set_code,
                )
                self._set_profile = (
                    None
                    if profile.maturity is ProfileMaturity.GENERIC
                    else profile
                )
                self._set_profiles_by_set[normalized_set_code] = (
                    None
                    if profile.maturity is ProfileMaturity.GENERIC
                    else profile
                )
                profile_state = self._profile_state_for_profile(
                    profile=profile,
                    set_code=normalized_set_code,
                    source=source,
                    phase=DataLoadPhase.READY,
                    refresh_outcome=(
                        ProfileRefreshOutcome.CACHED.value
                        if profile.maturity is not ProfileMaturity.GENERIC
                        else None
                    ),
                )

            self._set_profile_states_by_set[normalized_set_code] = profile_state
            if (
                self._configured_set_profile is None
                and self._profile_refresh_allowed()
            ):
                self._profile_refresh_request = ProfileRefreshRequest(
                    generation=generation,
                    set_code=normalized_set_code,
                    event_format=QUICK_DRAFT_FORMAT,
                )
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    set_profile=profile_state,
                )
            )

    def _load_local_profile_for_set(
        self,
        *,
        set_code: str,
    ) -> tuple[SetProfile, str]:
        cached_profile = self._set_profiles_by_set.get(set_code)
        if set_code in self._set_profiles_by_set and cached_profile is not None:
            return cached_profile, f"local-{cached_profile.maturity.value}"

        if set_code in self._set_profiles_by_set and cached_profile is None:
            return (
                SetProfile.generic(
                    set_code=set_code,
                    event_format=QUICK_DRAFT_FORMAT,
                ),
                "generic",
            )

        if self._profile_client is not None:
            try:
                loaded = self._profile_client.load_cached(
                    set_code,
                    QUICK_DRAFT_FORMAT,
                )
            except (OSError, SetProfileError):
                return (
                    SetProfile.generic(
                        set_code=set_code,
                        event_format=QUICK_DRAFT_FORMAT,
                    ),
                    "generic",
                )
            profile = loaded.profile
            source = str(loaded.source)
            return profile, source

        profile = load_scoring_profile(
            set_code,
            QUICK_DRAFT_FORMAT,
            app_dir=self.store.root.parent,
        )
        if profile is None:
            return (
                SetProfile.generic(
                    set_code=set_code,
                    event_format=QUICK_DRAFT_FORMAT,
                ),
                "generic",
            )
        return profile, f"local-{profile.maturity.value}"

    @staticmethod
    def _profile_refresh_allowed_for_client(*, profile_client: object) -> bool:
        manifest_url = getattr(profile_client, "manifest_url", None)
        if not manifest_url:
            return False
        network_policy = getattr(profile_client, "network_policy", None)
        policy_value = getattr(network_policy, "value", network_policy)
        return str(policy_value).casefold() == "allowed"

    def _profile_refresh_allowed(self) -> bool:
        profile_client = self._profile_client
        return (
            profile_client is not None
            and self._profile_refresh_allowed_for_client(
                profile_client=profile_client,
            )
        )
    def _profile_refresh_request_is_current(
        self,
        *,
        request: ProfileRefreshRequest,
    ) -> bool:
        return (
            request == self._profile_refresh_request
            and request.generation == self._profile_refresh_generation
            and request.set_code == self._active_set_code_value
            and request.event_format.casefold() == QUICK_DRAFT_FORMAT.casefold()
        )

    @staticmethod
    def _profile_state_for_profile(
        *,
        profile: SetProfile,
        set_code: str,
        source: str | None,
        phase: DataLoadPhase,
        refresh_outcome: str | None = None,
    ) -> SetProfileState:
        maturity = profile.maturity.value
        resolved_source = source or (
            "generic" if profile.maturity is ProfileMaturity.GENERIC else f"local-{maturity}"
        )
        if phase is DataLoadPhase.FAILED:
            message = (
                f"Set profile refresh failed for {set_code}; "
                "using the last-good profile."
            )
        elif refresh_outcome == ProfileRefreshOutcome.UPDATED.value:
            message = f"Updated {set_code} set profile."
        elif refresh_outcome == ProfileRefreshOutcome.UNCHANGED.value:
            message = f"{set_code} set profile is current."
        elif profile.maturity is ProfileMaturity.GENERIC:
            message = f"No set profile for {set_code}; generic scoring is active."
        elif resolved_source == "injected":
            message = f"Using the configured {maturity} set profile for {set_code}."
        else:
            message = f"Using the cached {maturity} set profile for {set_code}."
        return SetProfileState(
            set_code=set_code,
            event_format=QUICK_DRAFT_FORMAT,
            maturity=maturity,
            profile_version=profile.profile_version,
            source=resolved_source,
            phase=phase,
            refresh_outcome=refresh_outcome,
            message=message,
        )

    def _publish_profile_refresh_status_locked(
        self,
        *,
        request: ProfileRefreshRequest,
        outcome: str,
        phase: DataLoadPhase,
    ) -> None:
        active_profile = self._set_profile or SetProfile.generic(
            set_code=request.set_code,
            event_format=request.event_format,
        )
        previous_state = self.snapshot.set_profile
        state = self._profile_state_for_profile(
            profile=active_profile,
            set_code=request.set_code,
            source=previous_state.source,
            phase=phase,
            refresh_outcome=outcome,
        )
        self._set_profile_states_by_set[request.set_code] = state
        self._publish(snapshot=replace(self.snapshot, set_profile=state))

    def _active_draft_state(self) -> DraftState | None:
        draft = self.snapshot.draft
        if draft is None:
            return None

        return self._states_by_key.get((draft.account_id, draft.draft_id))

    def _pool_state_from_active_draft(self) -> PoolState:
        state = self._active_draft_state()
        if state is None:
            return PoolState()

        return self._pool_state(pool_grp_ids=state.pool_grp_ids)

    @staticmethod
    def _pool_aggregates(
        *,
        cards: tuple[PoolCard, ...],
    ) -> tuple[tuple[tuple[str, int], ...], tuple[int, ...], float | None]:
        """Return authoritative color counts, spell curve, and average value.
        Lands and unknown mana values do not contribute to spell aggregates.
        """

        color_counts = {color: 0 for color in ("W", "U", "B", "R", "G", "C")}
        mana_curve = [0] * 7
        mana_value_total = 0.0
        mana_value_quantity = 0
        for pool_card in cards:
            quantity = pool_card.quantity
            colors = pool_card.card.colors or ("C",)
            for color in colors:
                color_counts[color if color in color_counts else "C"] += quantity
            mana_value = pool_card.card.mana_value
            if any("Land" in type_line for type_line in pool_card.card.types):
                continue
            if mana_value is None:
                continue
            bucket = min(6, max(0, int(mana_value)))
            mana_curve[bucket] += quantity
            mana_value_total += mana_value * quantity
            mana_value_quantity += quantity
        return (
            tuple(color_counts.items()),
            tuple(mana_curve),
            (
                mana_value_total / mana_value_quantity
                if mana_value_quantity > 0
                else None
            ),
        )
    def _pool_state(
        self,
        *,
        pool_grp_ids: tuple[int, ...],
        scored_pack: ScoredPack | None = None,
        retained_current_colors: tuple[str, ...] | None = None,
    ) -> PoolState:
        commitment = None if scored_pack is None else scored_pack.commitment
        role_ledger = None if scored_pack is None else scored_pack.role_ledger
        inferred_pair = (
            None if commitment is None else commitment.inferred_pair
        )
        current_colors = (
            retained_current_colors
            if retained_current_colors is not None
            else tuple(inferred_pair or ())
        )
        database = self._card_database
        if database is None:
            return PoolState(
                total_cards=len(pool_grp_ids),
                target_cards=42,
                current_colors=current_colors,
                role_ledger=role_ledger,
            )

        counts = Counter(pool_grp_ids)
        cards = tuple(
            PoolCard(
                card=self._card_view(card=database.lookup(grp_id=grp_id)),
                quantity=counts[grp_id],
            )
            for grp_id in dict.fromkeys(pool_grp_ids)
        )
        recent_picks = self._prepare_recent_pick_images(
            pool_grp_ids=pool_grp_ids,
        )
        color_distribution, mana_curve, average_mana_value = self._pool_aggregates(
            cards=cards,
        )
        return PoolState(
            cards=cards,
            recent_picks=recent_picks,
            total_cards=len(pool_grp_ids),
            target_cards=42,
            inferred_pair=inferred_pair,
            current_colors=current_colors,
            commitment=(
                0.0 if commitment is None else commitment.level
            ),
            color_distribution=color_distribution,
            mana_curve=mana_curve,
            average_mana_value=average_mana_value,
            role_ledger=role_ledger,
        )

    def _select_recovered_state(self, *, state: DraftState) -> None:
        self._current_pack_event = _pending_pack_event(state=state)
        self._current_scored_pack = None
        self._card_data_local_lookup_attempted = False
        self._prepare_card_database_for_set(set_code=state.set_code)
        self._card_data_network_open = False
        self._select_state(state=state, recovered=True)
        if (
            self._card_database is None
            and self._set_card_data_loader is not None
            and not self._card_data_local_lookup_attempted
        ):
            self._card_data_local_lookup_attempted = True
            self._load_card_data_for_set(set_code=state.set_code, allow_network=False)
        self._ensure_ratings_loaded(set_code=state.set_code)
        self._score_current_pack()

    def _discard_previous_login_account_context(self) -> None:
        if self.parser.login_generation == self._login_generation:
            return

        self._login_generation = self.parser.login_generation
        self._log_account_id = None
        self.store.clear_active_account()
        self._restore_pending_login_account_context()

    def _restore_pending_login_account_context(self) -> None:
        profile = self._pending_login_account_profile()
        if profile is None:
            return

        self._log_account_id = profile.account_id
        self.store.set_active_account(
            account_id=profile.account_id,
            screen_name=profile.screen_name,
        )
        active_account = self.snapshot.active_account
        if active_account is not None and active_account.account_id != profile.account_id:
            return

        if self.snapshot.draft is not None or self.snapshot.pool.total_cards:
            return

        state = self._latest_state_for_account(
            account_id=profile.account_id,
            require_pool=True,
        )
        if state is None:
            self._select_account_without_draft(account_id=profile.account_id)
        else:
            self._select_recovered_state(state=state)

    def _consume_store_event(self, *, event: DraftEvent) -> DraftState | None:
        try:
            return self.store.consume(event=event)
        except DraftPoolError as error:
            if _is_missing_account_error(event=event, error=error):
                return None

            raise

    def _event_with_log_account(self, *, event: DraftEvent) -> DraftEvent:
        if self._log_account_id is None or not _event_is_missing_account(event=event):
            return event

        return replace(event, account_id=self._log_account_id)

    def _consume_event(self, *, event: DraftEvent, state: DraftState | None) -> None:
        if isinstance(event, AccountEvent):
            self._consume_account_event(event=event)
            return

        if isinstance(event, QuickDraftDetectedEvent):
            self._consume_detected_event(event=event)
            return

        if state is None:
            self._consume_accountless_event(event=event)
            return

        if isinstance(event, DraftStartedEvent):
            self._current_pack_event = None
            self._current_scored_pack = None
            self.audit_store.record_draft_started(state=state)
            self._select_state(
                state=state,
                recovered=False,
                event=event,
                message=f"Draft started for {event.set_code}.",
            )
            self._prepare_card_data_for_draft_start(set_code=event.set_code)
            if self._card_database is not None:
                self._ensure_ratings_loaded(set_code=event.set_code)
            return

        if isinstance(event, PackOfferedEvent):
            self._current_pack_event = event
            self._current_scored_pack = None
            self._prepare_card_database_for_set(set_code=event.set_code)
            self._select_state(
                state=state,
                recovered=False,
                event=event,
                message=(
                    f"Pack {event.pack_number + 1}, pick {event.pick_number + 1}."
                ),
            )
            self._ensure_ratings_loaded(set_code=event.set_code)
            self._score_current_pack()
            return

        if isinstance(event, PickMadeEvent):
            self.audit_store.record_choice(
                state=state,
                event=event,
                ranking_mode=self._ranking_mode,
            )
            self._select_state(
                state=state,
                recovered=False,
                event=event,
                message=(
                    f"Pack {event.pack_number + 1}, pick "
                    f"{event.pick_number + 1} recorded."
                ),
            )
            return

        if isinstance(event, DraftCompletedEvent):
            self._current_pack_event = None
            self._current_scored_pack = None
            self.audit_store.record_draft_completed(state=state, event=event)
            self._select_state(
                state=state,
                recovered=False,
                event=event,
                message="Draft complete.",
            )
            self._ensure_ratings_loaded(set_code=event.set_code)

    def _consume_accountless_event(self, *, event: DraftEvent) -> None:
        if isinstance(event, DraftStartedEvent):
            self._current_pack_event = None
            self._current_scored_pack = None
            self._transient_pool_grp_ids = ()
            self._set_active_set_code(set_code=event.set_code)
            self._publish_accountless_state(
                event=event,
                phase=ApplicationPhase.WAITING_FOR_DRAFT,
                message="Draft detected; waiting for an Arena account ID.",
                keep_recommendations=False,
            )
            self._prepare_card_data_for_draft_start(set_code=event.set_code)
            if self._card_database is not None:
                self._ensure_ratings_loaded(set_code=event.set_code)
            return

        if isinstance(event, PackOfferedEvent):
            self._current_pack_event = event
            self._current_scored_pack = None
            self._transient_pool_grp_ids = event.pool_grp_ids
            self._set_active_set_code(set_code=event.set_code)
            self._prepare_card_database_for_set(set_code=event.set_code)
            self._publish_accountless_state(
                event=event,
                phase=ApplicationPhase.DRAFTING,
                message=(
                    f"Pack {event.pack_number + 1}, pick "
                    f"{event.pick_number + 1}; waiting for an account ID."
                ),
                keep_recommendations=False,
            )
            self._ensure_ratings_loaded(set_code=event.set_code)
            self._score_current_pack()
            return

        if isinstance(event, PickMadeEvent):
            self._transient_pool_grp_ids += (event.chosen_grp_id,)
            self._publish_accountless_state(
                event=event,
                phase=ApplicationPhase.DRAFTING,
                message=(
                    f"Pack {event.pack_number + 1}, pick "
                    f"{event.pick_number + 1} recorded without an account ID."
                ),
                keep_recommendations=True,
            )
            return

        if isinstance(event, DraftCompletedEvent):
            self._current_pack_event = None
            self._current_scored_pack = None
            self._transient_pool_grp_ids = event.picked_grp_ids
            self._set_active_set_code(set_code=event.set_code)
            self._publish_accountless_state(
                event=event,
                phase=ApplicationPhase.DRAFT_COMPLETE,
                message="Draft complete without an account ID.",
                keep_recommendations=False,
            )
            self._ensure_ratings_loaded(set_code=event.set_code)

    def _publish_accountless_state(
        self,
        *,
        event: DraftEvent,
        phase: ApplicationPhase,
        message: str,
        keep_recommendations: bool,
    ) -> None:
        set_code = event.set_code.upper()
        ratings = self._ratings_state_by_set.get(
            set_code,
            replace(self._initial_ratings_state(), set_code=set_code),
        )
        with self._state_lock:
            errors = self._retire_derived_operations()
            if not keep_recommendations:
                self._retire_recent_pick_images()
            card_image = (
                self.snapshot.card_image
                if keep_recommendations
                else self._retire_card_image()
            )
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    status=ApplicationStatus(phase=phase, message=message),
                    accounts=self._known_accounts(),
                    active_account=None,
                    draft=None,
                    ratings=ratings,
                    recommendations=(
                        self.snapshot.recommendations
                        if keep_recommendations
                        else RecommendationState(
                            ranking_mode=self._ranking_mode,
                            splash_enabled=self._splash_enabled,
                        )
                    ),
                    card_image=card_image,
                    pool=self._pool_state(
                        pool_grp_ids=self._transient_pool_grp_ids,
                        retained_current_colors=(
                            self.snapshot.pool.current_colors
                            if keep_recommendations
                            else None
                        ),
                    ),
                    progress=None,
                    errors=errors,
                    build=None,
                    backtest=None,
                )
            )

    def _consume_account_event(self, *, event: AccountEvent) -> None:
        self._log_account_id = event.client_id
        if event.screen_name is not None:
            self._screen_names_by_account_id[event.client_id] = event.screen_name

        active_account = self.snapshot.active_account
        if (
            active_account is not None
            and active_account.account_id != event.client_id
            and (self.snapshot.draft is not None or self.snapshot.pool.total_cards)
        ):
            self._refresh_accounts()
            return

        state = self._latest_state_for_account(
            account_id=event.client_id,
            require_pool=True,
        )
        if state is None:
            self._select_account_without_draft(account_id=event.client_id)
        else:
            self._select_recovered_state(state=state)

    def _consume_detected_event(self, *, event: QuickDraftDetectedEvent) -> None:
        normalized_set_code = event.set_code.upper()
        new_lifecycle = (
            event.event_name != self._card_data_detection_event_name
            or not self._card_data_network_open
        )
        self._card_data_detection_event_name = event.event_name
        if new_lifecycle:
            self._card_data_network_open = True
            self._card_data_local_lookup_attempted = False
        self._current_pack_event = None
        self._current_scored_pack = None
        self._transient_pool_grp_ids = ()
        self._set_active_set_code(set_code=normalized_set_code)
        self._prepare_card_database_for_set(set_code=normalized_set_code)
        account_id = event.account_id or self._log_account_id
        active_account = self._identity_for(account_id=account_id)
        with self._state_lock:
            errors = self._retire_derived_operations()
            self._retire_recent_pick_images()
            card_image = self._retire_card_image()
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    status=ApplicationStatus(
                        phase=ApplicationPhase.WAITING_FOR_DRAFT,
                        message=f"Preparing Quick Draft data for {event.set_code}.",
                    ),
                    accounts=self._known_accounts(),
                    active_account=active_account,
                    ratings=self._ratings_state_by_set.get(
                        normalized_set_code,
                        replace(
                            self._initial_ratings_state(),
                            set_code=normalized_set_code,
                        ),
                    ),
                    draft=None,
                    recommendations=RecommendationState(
                        ranking_mode=self._ranking_mode,
                        splash_enabled=self._splash_enabled,
                    ),
                    card_image=card_image,
                    pool=PoolState(),
                    progress=None,
                    errors=errors,
                    build=None,
                    backtest=None,
                )
            )
        if new_lifecycle:
            self._load_card_data_for_set(
                set_code=normalized_set_code,
                allow_network=True,
            )
        if self._card_database is not None:
            self._ensure_ratings_loaded(set_code=normalized_set_code)

    def _choose_account(self, *, account_id: str) -> None:
        known_account_ids = {account.account_id for account in self._known_accounts()}
        if account_id not in known_account_ids:
            raise ValueError(f"Unknown Arena account {account_id!r}.")

        state = self._latest_state_for_account(
            account_id=account_id,
            require_pool=False,
        )
        if state is None:
            self._select_account_without_draft(account_id=account_id)
        else:
            self._select_recovered_state(state=state)

    def _select_account_without_draft(self, *, account_id: str) -> None:
        identity = self._identity_for(account_id=account_id)
        if identity is None:
            raise ValueError(f"Unknown Arena account {account_id!r}.")

        self.store.set_active_account(
            account_id=account_id,
            screen_name=identity.screen_name,
        )
        self._current_pack_event = None
        self._current_scored_pack = None
        self._transient_pool_grp_ids = ()
        with self._state_lock:
            self._clear_active_set_code_locked()
            self._retire_recent_pick_images()
            errors = self._retire_derived_operations()
            card_image = self._retire_card_image()
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    set_profile=SetProfileState(),
                    status=ApplicationStatus(
                        phase=ApplicationPhase.WAITING_FOR_DRAFT,
                        message="Waiting for a Quick Draft.",
                    ),
                    accounts=self._known_accounts(),
                    active_account=identity,
                    draft=None,
                    ratings=self._initial_ratings_state(),
                    recommendations=RecommendationState(
                        ranking_mode=self._ranking_mode,
                        splash_enabled=self._splash_enabled,
                    ),
                    card_image=card_image,
                    pool=PoolState(),
                    progress=None,
                    errors=errors,
                    build=None,
                    backtest=None,
                )
            )

    def _select_state(
        self,
        *,
        state: DraftState,
        recovered: bool,
        event: DraftEvent | None = None,
        message: str | None = None,
    ) -> None:
        self._remember_state(state=state)
        self._transient_pool_grp_ids = state.pool_grp_ids
        self._set_active_set_code(set_code=state.set_code)
        self.store.set_active_account(
            account_id=state.account_id,
            screen_name=state.account_screen_name,
        )
        pack_number, pick_number = _draft_coordinates(state=state, event=event)
        if message is None:
            if state.completed:
                message = "Recovered completed draft."
            elif recovered:
                message = "Recovered draft in progress."
            else:
                message = "Draft in progress."
        phase = (
            ApplicationPhase.DRAFT_COMPLETE
            if state.completed
            else ApplicationPhase.DRAFTING
        )
        with self._state_lock:
            errors = self._retire_derived_operations()
            previous_draft = self.snapshot.draft
            switches_context = (
                self.snapshot.active_account is None
                or self.snapshot.active_account.account_id != state.account_id
                or previous_draft is None
                or previous_draft.account_id != state.account_id
                or previous_draft.draft_id != state.draft_id
            )
            if switches_context:
                self._retire_recent_pick_images()
            card_image = (
                self._retire_card_image()
                if switches_context or not isinstance(event, PickMadeEvent)
                else self.snapshot.card_image
            )
            self._publish(
                snapshot=replace(
                    self.snapshot,
                    status=ApplicationStatus(phase=phase, message=message),
                    accounts=self._known_accounts(),
                    active_account=AccountIdentity(
                        account_id=state.account_id,
                        screen_name=self._screen_name_for_state(state=state),
                    ),
                    draft=DraftIdentity(
                        account_id=state.account_id,
                        draft_id=state.draft_id,
                        event_name=state.event_name,
                        set_code=state.set_code,
                        course_id=state.course_id,
                        pack_number=pack_number,
                        pick_number=pick_number,
                        completed=state.completed,
                    ),
                    recommendations=(
                        self.snapshot.recommendations
                        if isinstance(event, PickMadeEvent)
                        else RecommendationState(
                            ranking_mode=self._ranking_mode,
                            splash_enabled=self._splash_enabled,
                        )
                    ),
                    card_image=card_image,
                    pool=self._pool_state(
                        pool_grp_ids=state.pool_grp_ids,
                        retained_current_colors=(
                            self.snapshot.pool.current_colors
                            if isinstance(event, PickMadeEvent)
                            else None
                        ),
                    ),
                    progress=None,
                    errors=errors,
                    build=None,
                    backtest=None,
                )
            )

    def _remember_state(self, *, state: DraftState) -> None:
        self._states_by_key[(state.account_id, state.draft_id)] = state
        if state.account_screen_name is not None:
            self._screen_names_by_account_id[state.account_id] = (
                state.account_screen_name
            )

    def _known_accounts(self) -> tuple[AccountIdentity, ...]:
        screen_names: dict[str, str | None] = dict(
            self._screen_names_by_account_id
        )
        for state in self._recovered_states():
            if state.account_screen_name is not None:
                screen_names.setdefault(state.account_id, state.account_screen_name)
            else:
                screen_names.setdefault(state.account_id, None)
        for profile in list_account_profiles(app_dir=self.store.app_dir):
            screen_names[profile.account_id] = profile.screen_name

        return tuple(
            sorted(
                (
                    AccountIdentity(
                        account_id=account_id,
                        screen_name=screen_name,
                    )
                    for account_id, screen_name in screen_names.items()
                ),
                key=lambda account: (
                    (account.screen_name or account.account_id).casefold(),
                    account.account_id,
                ),
            )
        )

    def _identity_for(self, *, account_id: str | None) -> AccountIdentity | None:
        if account_id is None:
            return None

        return next(
            (
                account
                for account in self._known_accounts()
                if account.account_id == account_id
            ),
            AccountIdentity(account_id=account_id, screen_name=None),
        )

    def _refresh_accounts(self) -> None:
        active_account_id = (
            None
            if self.snapshot.active_account is None
            else self.snapshot.active_account.account_id
        )
        self._publish(
            snapshot=replace(
                self.snapshot,
                accounts=self._known_accounts(),
                active_account=self._identity_for(account_id=active_account_id),
            )
        )

    def _pending_login_account_profile(self) -> AccountProfile | None:
        pending_screen_name = self.parser.pending_login_screen_name
        if pending_screen_name is None:
            return None

        pending_key = _account_screen_name_match_key(
            screen_name=pending_screen_name,
        )
        matches = tuple(
            profile
            for profile in list_account_profiles(app_dir=self.store.app_dir)
            if _account_screen_name_match_key(screen_name=profile.screen_name)
            == pending_key
        )
        if len(matches) != 1:
            return None

        return matches[0]

    def _recovered_states(self) -> tuple[DraftState, ...]:
        states = dict(self._states_by_key)
        states.update(
            {
                (state.account_id, state.draft_id): state
                for state in list_draft_states(app_dir=self.store.app_dir)
            }
        )
        return tuple(states.values())

    def _latest_state_for_account(
        self,
        *,
        account_id: str,
        require_pool: bool,
    ) -> DraftState | None:
        states = tuple(
            state
            for state in self._recovered_states()
            if state.account_id == account_id
            and (not require_pool or bool(state.pool_grp_ids))
        )
        if not states:
            return None

        return max(states, key=_latest_draft_state_sort_key)

    def _persist_pending_login_name_for_observed_course(self) -> None:
        screen_name = self.parser.pending_login_screen_name
        course_ids = self.parser.observed_quick_draft_course_ids
        if screen_name is None or not course_ids:
            return

        matching_account_ids = {
            state.account_id
            for state in self._recovered_states()
            if state.course_id in course_ids or state.draft_id in course_ids
        }
        if len(matching_account_ids) != 1:
            return

        account_id = matching_account_ids.pop()
        identity = self._identity_for(account_id=account_id)
        if identity is not None and identity.screen_name is not None:
            return

        self.store.set_active_account(
            account_id=account_id,
            screen_name=screen_name,
        )
        self._screen_names_by_account_id[account_id] = screen_name
        self._refresh_accounts()

    def _screen_name_for_state(self, *, state: DraftState) -> str | None:
        identity = self._identity_for(account_id=state.account_id)
        if identity is not None and identity.screen_name is not None:
            return identity.screen_name

        return state.account_screen_name

    def _publish(self, snapshot: LiveSessionSnapshot) -> None:
        with self._state_lock:
            snapshot = replace(
                snapshot,
                current_pack_event=self._current_pack_event,
                current_scored_pack=self._current_scored_pack,
            )
            pack_key = self._recommendation_image_pack_key(
                pack=self._current_pack_event,
                snapshot=snapshot,
            )
            if (
                self._recommendation_image_pack is not None
                and pack_key != self._recommendation_image_pack
            ):
                self._retire_recommendation_images()
            if snapshot == self._snapshot:
                return

            self._snapshot = snapshot
            if self._snapshot_publisher is not None:
                self._snapshot_publisher(snapshot)

    def _publish_event(self, *, event: DraftEvent) -> None:
        if self._event_publisher is None:
            return

        scored_pack = (
            self._current_scored_pack
            if isinstance(event, PackOfferedEvent)
            else None
        )
        self._event_publisher(
            LiveSessionEvent(
                event=event,
                snapshot=self.snapshot,
                scored_pack=scored_pack,
            )
        )


def _profile_refresh_outcome_value(
    outcome: ProfileRefreshOutcome | str,
) -> str:
    return outcome.value if isinstance(outcome, ProfileRefreshOutcome) else str(outcome)
def _profile_refresh_profile_is_adoptable(
    *,
    profile: SetProfile,
    current_profile: SetProfile | None,
    outcome: str,
) -> bool:
    """Accept only a non-regressing profile from an adapter refresh.

    Cached and unchanged results can carry a cache entry installed by another
    client, so they are eligible when their validated content is newer.  An
    explicit updated result may also replace a same-timestamp profile version,
    preserving the existing refresh contract.
    """

    if profile.maturity is ProfileMaturity.GENERIC:
        return False
    if current_profile is None or current_profile.maturity is ProfileMaturity.GENERIC:
        return True

    maturity_rank = {
        ProfileMaturity.MATURE: 0,
        ProfileMaturity.EARLY: 1,
        ProfileMaturity.SEMANTIC_ONLY: 2,
        ProfileMaturity.METADATA_ONLY: 3,
        ProfileMaturity.GENERIC: 4,
    }
    profile_rank = maturity_rank[profile.maturity]
    current_rank = maturity_rank[current_profile.maturity]
    if profile_rank > current_rank:
        return False

    def profile_time(candidate: SetProfile) -> datetime:
        try:
            parsed = datetime.fromisoformat(
                candidate.generated_at.replace("Z", "+00:00")
            )
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("profile generated_at must be timezone-aware")
            return parsed.astimezone(UTC)
        except (OverflowError, TypeError, ValueError):
            return datetime.min.replace(tzinfo=UTC)

    candidate_time = profile_time(profile)
    current_time = profile_time(current_profile)
    if candidate_time < current_time:
        return False
    if profile_rank < current_rank or candidate_time > current_time:
        return True
    return outcome not in {
        ProfileRefreshOutcome.CACHED.value,
        ProfileRefreshOutcome.UNCHANGED.value,
    }



def _ratings_last_successful_update(
    *, ratings_data: SeventeenLandsData
) -> str | None:
    """Return primary ratings freshness as a QML-safe UTC ISO-8601 string.
    Invalid or naive 17Lands cache metadata is treated as never updated.
    """

    fetched_at = getattr(
        getattr(ratings_data, "primary", None),
        "fetched_at",
        None,
    )
    if (
        not isinstance(fetched_at, datetime)
        or fetched_at.tzinfo is None
        or fetched_at.utcoffset() is None
    ):
        return None

    try:
        return fetched_at.astimezone(UTC).isoformat()
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _last_successful_update(*, database: CardDatabase) -> str | None:
    """Return a database refresh time as a QML-safe UTC ISO-8601 string.
    Unknown or naive cache metadata is treated as never updated.
    """

    generated_at = database.generated_at
    if (
        not isinstance(generated_at, datetime)
        or generated_at.tzinfo is None
        or generated_at.utcoffset() is None
    ):
        return None

    return generated_at.astimezone(UTC).isoformat()


def _waiting_for_draft_status(*, setup_guidance: bool) -> ApplicationStatus:
    return ApplicationStatus(
        phase=ApplicationPhase.WAITING_FOR_DRAFT,
        message=LOG_SETUP_GUIDANCE if setup_guidance else WAITING_FOR_DRAFT_MESSAGE,
        setup_guidance=setup_guidance,
    )


def _log_setup_refresh_is_eligible(
    *,
    snapshot: LiveSessionSnapshot,
) -> bool:
    status = snapshot.status
    return (
        snapshot.draft is None
        and status.phase == ApplicationPhase.WAITING_FOR_DRAFT
        and (
            status.setup_guidance
            or status.message == WAITING_FOR_DRAFT_MESSAGE
        )
    )


def _draft_coordinates(
    *,
    state: DraftState,
    event: DraftEvent | None,
) -> tuple[int | None, int | None]:
    if isinstance(event, (PackOfferedEvent, PickMadeEvent, DraftCompletedEvent)):
        return (event.pack_number, event.pick_number)

    if not state.picks:
        return (None, None)

    pick = max(state.picks, key=lambda candidate: candidate.coordinate)
    return pick.coordinate


def _pending_pack_event(*, state: DraftState) -> PackOfferedEvent | None:
    if state.completed:
        return None

    pending_picks = tuple(
        pick
        for pick in state.picks
        if pick.offered_grp_ids and pick.chosen_grp_id is None
    )
    if not pending_picks:
        return None

    pending_pick = max(pending_picks, key=lambda pick: pick.coordinate)
    offered_grp_ids = pending_pick.offered_grp_ids
    if offered_grp_ids is None:
        return None

    pool_grp_ids = (
        state.pool_grp_ids
        if pending_pick.pool_before_pick is None
        else pending_pick.pool_before_pick
    )
    return PackOfferedEvent(
        event_name=state.event_name,
        set_code=state.set_code,
        pack_number=pending_pick.pack_number,
        pick_number=pending_pick.pick_number,
        offered_grp_ids=offered_grp_ids,
        pool_grp_ids=pool_grp_ids,
        account_id=state.account_id,
    )


def _draft_pick_index(*, event: PackOfferedEvent) -> int:
    return (event.pack_number * EXPECTED_PICKS_PER_PACK) + event.pick_number + 1


def _card_view(*, card: CardInfo, image_path: str | None = None) -> CardView:
    return CardView(
        grp_id=card.grp_id,
        name=card.name,
        colors=card.colors,
        rarity=card.rarity,
        types=card.types,
        mana_cost=card.mana_cost,
        mana_value=card.mana_value,
        image_path=image_path,
        unknown=card.unknown,
        produced_mana=card.produced_mana,
        oracle_text=card.oracle_text,
        keywords=card.keywords,
        type_line=card.type_line,
        subtypes=card.subtypes,
        layout=card.layout,
        faces=card.faces,
        set_code=card.set_code,
        collector_number=card.collector_number,
        arena_id=card.arena_id,
        source_provenance=card.source_provenance,
    )


def _build_cards(*, cards: tuple[ScoredCard, ...]) -> tuple[BuildCard, ...]:
    grouped: dict[tuple[str, str], BuildCard] = {}
    for scored_card in cards:
        card = scored_card.card
        key = (
            ("unknown", str(card.grp_id))
            if card.unknown
            else ("name", " ".join(card.name.casefold().split()))
        )
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = BuildCard(
                card=_card_view(card=card),
                quantity=1,
                score=scored_card.score,
                win_rate=scored_card.rating.gih_win_rate,
                average_last_seen_at=scored_card.rating.average_last_seen_at,
                letter_grade=scored_card.rating.letter_grade,
                source_label=scored_card.source_label,
                color_fit=scored_card.color_fit,
                no_data=scored_card.no_data,
            )
            continue

        grouped[key] = replace(existing, quantity=existing.quantity + 1)

    return tuple(grouped.values())


def _build_spell_curve_sort_key(card: ScoredCard) -> tuple[float, int, str, int]:
    mana_value = 99.0 if card.card.mana_value is None else card.card.mana_value
    return (mana_value, -card.score, card.card.name, card.original_index)


def _backtest_result(*, report: DomainBacktestReport) -> BacktestResult:
    state = report.state
    return BacktestResult(
        ranking_mode=report.ranking_mode,
        rows=tuple(
            BacktestPickResult(
                pack_number=row.pack_number,
                pick_number=row.pick_number,
                recommended=(
                    None
                    if row.recommended is None
                    else _card_view(card=row.recommended.card)
                ),
                actual=(
                    None if row.actual is None else _card_view(card=row.actual)
                ),
                match=row.match,
                skipped_reason=row.skipped_reason,
                data_source=row.data_source,
                pool_size=row.pool_size,
                offered_count=row.offered_count,
                recommended_score=(
                    None if row.recommended is None else row.recommended.score
                ),
                recommended_win_rate=(
                    None
                    if row.recommended is None
                    else row.recommended.rating.gih_win_rate
                ),
                role_ledger=row.role_ledger,
                scoring_context=row.scoring_context,
                contextual_evidence=row.contextual_evidence,
            )
            for row in report.rows
        ),
        match_count=report.match_count,
        compared_count=len(report.compared_rows),
        skipped_count=len(report.skipped_rows),
        data_sources=report.data_sources,
        account_id=state.account_id,
        account_screen_name=state.account_screen_name,
        draft_id=state.draft_id,
        set_code=state.set_code,
        event_name=state.event_name,
        completed=state.completed,
        chosen_pick_count=state.chosen_pick_count,
    )


def _latest_draft_state_sort_key(state: DraftState) -> tuple[str, str, str]:
    return (state.updated_at, state.account_id, state.draft_id)


def _is_missing_account_error(*, event: DraftEvent, error: DraftPoolError) -> bool:
    return (
        str(error) == "Draft event is missing an MTGA account id."
        and _event_is_missing_account(event=event)
    )


def _event_is_missing_account(*, event: DraftEvent) -> bool:
    return (
        isinstance(
            event,
            (
                QuickDraftDetectedEvent,
                DraftStartedEvent,
                PackOfferedEvent,
                PickMadeEvent,
                DraftCompletedEvent,
            ),
        )
        and event.account_id is None
    )


def _account_screen_name_match_key(*, screen_name: str) -> str:
    normalized = screen_name.strip()
    name, separator, discriminator = normalized.rpartition("#")
    if separator and name and discriminator.isdigit():
        normalized = name

    return normalized.casefold()
