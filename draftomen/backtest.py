"""Post-draft recommendation backtesting.
Replay saved picks without mutating draft state.
"""

from __future__ import annotations

from dataclasses import dataclass
from os import PathLike
from typing import TypeAlias

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.events import EXPECTED_PICKS_PER_PACK, EXPECTED_TOTAL_PICKS
from draftomen.pickengine import PickEngine, PickScoringContext, ScoredCard
from draftomen.pool import DraftPick, DraftState, list_draft_states
from draftomen.pool_ledger import PoolRoleLedger
from draftomen.ranking import DEFAULT_RANKING_MODE, rank_scored_cards, ranking_label
from draftomen.replay import format_card_info
from draftomen.set_profile import SetProfile
from draftomen.seventeen import SeventeenLandsData

PathInput: TypeAlias = str | PathLike[str]


class BacktestError(RuntimeError):
    """Raised when a persisted draft cannot be backtested.
    CLI callers surface this as a concise diagnostic.
    """


@dataclass(frozen=True, slots=True)
class BacktestPickResult:
    """Recommendation comparison for one saved draft pick.
    Missing history keeps a skipped row instead of mutating state.
    """

    pack_number: int
    pick_number: int
    pool_size: int | None
    offered_count: int | None
    recommended: ScoredCard | None
    actual: CardInfo | None
    match: bool | None
    skipped_reason: str | None
    data_source: str | None
    role_ledger: PoolRoleLedger | None = None
    scoring_context: PickScoringContext | None = None
    contextual_evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Backtest rows plus the persisted draft metadata.
    Derived counts keep formatting and tests simple.
    """

    state: DraftState
    rows: tuple[BacktestPickResult, ...]
    ranking_mode: str

    @property
    def compared_rows(self) -> tuple[BacktestPickResult, ...]:
        """Return rows where recommendation and actual pick can be compared.
        Rows skipped for missing history are excluded.
        """

        return tuple(row for row in self.rows if row.match is not None)

    @property
    def skipped_rows(self) -> tuple[BacktestPickResult, ...]:
        """Return rows that could not produce a comparison.
        The row itself carries the user-facing reason.
        """

        return tuple(row for row in self.rows if row.match is None)

    @property
    def match_count(self) -> int:
        """Return how many comparable picks matched Draftomen's recommendation.
        A skipped pick is not counted as a miss.
        """

        return sum(1 for row in self.rows if row.match is True)

    @property
    def data_sources(self) -> tuple[str, ...]:
        """Return unique pack data-source summaries in first-seen order.
        Missing-history rows do not contribute a source label.
        """

        sources: list[str] = []
        for row in self.rows:
            if row.data_source is None or row.data_source in sources:
                continue

            sources.append(row.data_source)

        return tuple(sources)


def load_persisted_backtest_state(
    *,
    app_dir: PathInput | None = None,
    account_id: str | None = None,
    draft_id: str | None = None,
) -> DraftState:
    """Load the requested persisted draft state, defaulting to latest.
    Account and draft filters disambiguate local multi-account state.
    """

    matches = _matching_states(
        states=list_draft_states(app_dir=app_dir),
        account_id=account_id,
        draft_id=draft_id,
    )
    if not matches:
        raise BacktestError(
            _missing_persisted_draft_message(account_id=account_id, draft_id=draft_id)
        )

    if draft_id is not None and len(matches) > 1:
        raise BacktestError(
            f"Multiple persisted drafts use draft id {draft_id!r}; pass --account."
        )

    return _latest_state(states=matches)


def generate_backtest_report(
    *,
    state: DraftState,
    card_database: CardDatabase,
    ratings_data: SeventeenLandsData | None = None,
    pick_engine: PickEngine | None = None,
    ranking_mode: str = DEFAULT_RANKING_MODE,
    splash_enabled: bool = True,
    contextual_adjustments_enabled: bool = True,
    set_profile: SetProfile | None = None,
) -> BacktestReport:
    """Score each saved pick from the persisted pre-pick state.
    The function is read-only and never writes draft state.
    """

    engine = (
        pick_engine
        if pick_engine is not None
        else PickEngine(
            ratings_data=ratings_data,
            splash_enabled=splash_enabled,
            contextual_adjustments_enabled=contextual_adjustments_enabled,
            set_profile=set_profile,
        )
    )
    rows = tuple(
        _score_pick(
            pick=pick,
            card_database=card_database,
            pick_engine=engine,
            ranking_mode=ranking_mode,
        )
        for pick in state.picks
    )
    return BacktestReport(state=state, rows=rows, ranking_mode=ranking_mode)


def format_backtest_report(report: BacktestReport) -> str:
    """Format a post-draft recommendation comparison report.
    The output is stable plain text for CLI use and regression tests.
    """

    lines = _format_header(report=report)
    lines.append("")
    lines.extend(_format_rows(report=report))
    lines.append("")
    lines.extend(_format_summary(report=report))
    return "\n".join(lines).rstrip() + "\n"


def _score_pick(
    *,
    pick: DraftPick,
    card_database: CardDatabase,
    pick_engine: PickEngine,
    ranking_mode: str,
) -> BacktestPickResult:
    actual = (
        card_database.lookup(grp_id=pick.chosen_grp_id)
        if pick.chosen_grp_id is not None
        else None
    )
    if pick.offered_grp_ids is None:
        return _skipped_result(
            pick=pick,
            actual=actual,
            reason="missing offered-card history",
        )

    if not pick.offered_grp_ids:
        return _skipped_result(
            pick=pick,
            actual=actual,
            reason="empty offered-card history",
            offered_count=0,
        )

    if pick.pool_before_pick is None:
        return _skipped_result(
            pick=pick,
            actual=actual,
            reason="missing pool-before-pick snapshot",
            offered_count=len(pick.offered_grp_ids),
        )

    global_pick_index = _draft_pick_index(pick=pick)
    scored_pack = pick_engine.score_pack(
        offered_grp_ids=pick.offered_grp_ids,
        card_database=card_database,
        pool_grp_ids=pick.pool_before_pick,
        pick_index=global_pick_index,
        pack_number=pick.pack_number,
        pick_number=pick.pick_number,
        global_pick_index=global_pick_index,
        estimated_remaining_picks=max(0, EXPECTED_TOTAL_PICKS - global_pick_index),
    )
    ranked_cards = rank_scored_cards(
        cards=scored_pack.cards,
        ranking_mode=ranking_mode,
    )
    recommended = ranked_cards[0] if ranked_cards else None
    if recommended is None:
        return _skipped_result(
            pick=pick,
            actual=actual,
            reason="no recommended card",
            pool_size=len(pick.pool_before_pick),
            offered_count=len(pick.offered_grp_ids),
        )

    if pick.chosen_grp_id is None:
        return BacktestPickResult(
            pack_number=pick.pack_number,
            pick_number=pick.pick_number,
            pool_size=len(pick.pool_before_pick),
            offered_count=len(pick.offered_grp_ids),
            recommended=recommended,
            actual=None,
            match=None,
            skipped_reason="missing actual selected card",
            data_source=scored_pack.source_summary,
            role_ledger=scored_pack.role_ledger,
            scoring_context=scored_pack.scoring_context,
            contextual_evidence=recommended.contextual_evidence,
        )

    return BacktestPickResult(
        pack_number=pick.pack_number,
        pick_number=pick.pick_number,
        pool_size=len(pick.pool_before_pick),
        offered_count=len(pick.offered_grp_ids),
        recommended=recommended,
        actual=actual,
        match=recommended.card.grp_id == pick.chosen_grp_id,
        skipped_reason=None,
        data_source=scored_pack.source_summary,
        role_ledger=scored_pack.role_ledger,
        scoring_context=scored_pack.scoring_context,
        contextual_evidence=recommended.contextual_evidence,
    )


def _skipped_result(
    *,
    pick: DraftPick,
    actual: CardInfo | None,
    reason: str,
    pool_size: int | None = None,
    offered_count: int | None = None,
) -> BacktestPickResult:
    return BacktestPickResult(
        pack_number=pick.pack_number,
        pick_number=pick.pick_number,
        pool_size=pool_size,
        offered_count=offered_count,
        recommended=None,
        actual=actual,
        match=None,
        skipped_reason=reason,
        data_source=None,
    )


def _draft_pick_index(*, pick: DraftPick) -> int:
    return (pick.pack_number * EXPECTED_PICKS_PER_PACK) + pick.pick_number + 1


def _format_header(*, report: BacktestReport) -> list[str]:
    state = report.state
    return [
        "Draft Omen backtest",
        f"Account: {_format_account(state=state)}",
        f"Set: {state.set_code}",
        f"Event: {state.event_name}",
        f"Draft: {state.draft_id}",
        f"Completed: {_yes_no(state.completed)}",
        f"Ranking: {ranking_label(ranking_mode=report.ranking_mode)}",
        (
            "Picks: "
            f"{state.chosen_pick_count} chosen, "
            f"{len(report.compared_rows)} compared, "
            f"{len(report.skipped_rows)} skipped"
        ),
        f"Data sources: {_format_data_sources(report=report)}",
    ]


def _format_rows(*, report: BacktestReport) -> list[str]:
    if not report.rows:
        return ["No saved picks were found for this draft."]

    recommended_values = tuple(_format_recommended(row=row) for row in report.rows)
    actual_values = tuple(_format_actual(row=row) for row in report.rows)
    recommended_width = max(len("Recommended"), *(len(value) for value in recommended_values))
    actual_width = max(len("Actual"), *(len(value) for value in actual_values))
    lines = [
        "Pack  Pick  Pool  17L WR  DO   "
        f"{'Recommended':<{recommended_width}}  "
        f"{'Actual':<{actual_width}}  "
        "Match"
    ]
    for row, recommended, actual in zip(
        report.rows,
        recommended_values,
        actual_values,
        strict=True,
    ):
        lines.append(
            f"{row.pack_number + 1:>4}  "
            f"{row.pick_number + 1:>4}  "
            f"{_format_optional_int(row.pool_size):>4}  "
            f"{_format_win_rate(row=row):>6}  "
            f"{_format_score(row=row):>3}  "
            f"{recommended:<{recommended_width}}  "
            f"{actual:<{actual_width}}  "
            f"{_format_match(row=row)}"
        )

    return lines


def _format_summary(*, report: BacktestReport) -> list[str]:
    compared_count = len(report.compared_rows)
    if compared_count == 0:
        lines = [
            "Summary: no comparable picks; "
            f"{len(report.skipped_rows)} skipped."
        ]
    else:
        match_rate = report.match_count / compared_count
        lines = [
            "Summary: "
            f"{report.match_count}/{compared_count} recommendations matched "
            f"actual picks ({match_rate:.1%})."
        ]

    if report.skipped_rows:
        lines.append(
            "Skipped picks were not scored when saved offered-card or "
            "pool-before-pick history was missing."
        )

    return lines


def _format_recommended(*, row: BacktestPickResult) -> str:
    if row.recommended is not None:
        return format_card_info(row.recommended.card)

    reason = row.skipped_reason or "not scored"
    return f"skipped: {reason}"


def _format_actual(*, row: BacktestPickResult) -> str:
    if row.actual is not None:
        return format_card_info(row.actual)

    return row.skipped_reason or "missing actual selected card"


def _format_win_rate(*, row: BacktestPickResult) -> str:
    if row.recommended is None or row.recommended.rating.gih_win_rate is None:
        return "—"

    return f"{row.recommended.rating.gih_win_rate:.1%}"


def _format_score(*, row: BacktestPickResult) -> str:
    if row.recommended is None:
        return "—"

    return str(row.recommended.score)


def _format_match(*, row: BacktestPickResult) -> str:
    if row.match is None:
        return "skipped"

    return _yes_no(row.match)


def _format_optional_int(value: int | None) -> str:
    if value is None:
        return "—"

    return str(value)


def _format_account(*, state: DraftState) -> str:
    if state.account_screen_name is None:
        return state.account_id

    return f"{state.account_screen_name} ({state.account_id})"


def _format_data_sources(*, report: BacktestReport) -> str:
    if not report.data_sources:
        return "none"

    return "; ".join(report.data_sources)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


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


def _missing_persisted_draft_message(
    *,
    account_id: str | None,
    draft_id: str | None,
) -> str:
    if account_id is not None and draft_id is not None:
        return f"No persisted draft found for account {account_id!r} and draft {draft_id!r}."

    if account_id is not None:
        return f"No persisted draft found for account {account_id!r}."

    if draft_id is not None:
        return f"No persisted draft found for draft {draft_id!r}."

    return "No persisted drafts found. Replay/watch a draft first."
