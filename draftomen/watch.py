"""Adapt shared live-session events to plain-text watch output.
The adapter preserves the established command and output contracts.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterable
from dataclasses import replace
from os import PathLike
from threading import Lock
from typing import TextIO, TypeAlias

from draftomen.carddb import CardDatabase
from draftomen.profile_client import ProfileClient, ProfileRefreshResult
from draftomen.config import POLL_INTERVAL_SECONDS
from draftomen.deckbuilder import DeckBuilderError, format_build_result
from draftomen.events import (
    AccountEvent,
    DraftCompletedEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
    QuickDraftDetectedEvent,
)
from draftomen.replay import (
    format_draft_completed_event,
    format_pack_offered_event,
    format_pick_made_event,
)
from draftomen.session import (
    DataLoadPhase,
    LiveSession,
    LiveSessionEvent,
    OperationKind,
    ProfileRefreshRequest,
    RequestBuild,
    SetCardDataLoader,
)
from draftomen.seventeen import SeventeenLandsData

PathInput: TypeAlias = str | PathLike[str]
RatingsLoader: TypeAlias = Callable[[str], SeventeenLandsData]


class PlainLogWatcher:
    """Render one shared live session as incremental plain text.
    Tests can call poll_once to simulate one live polling cycle exactly.
    """

    def __init__(
        self,
        *,
        log_path: PathInput,
        card_database: CardDatabase | None = None,
        set_card_data_loader: SetCardDataLoader | None = None,
        app_dir: PathInput | None = None,
        profile_client: ProfileClient | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        previous_log_path: PathInput | None = None,
        ratings_loader: RatingsLoader | None = None,
        splash_enabled: bool = True,
    ) -> None:
        if card_database is None and set_card_data_loader is None:
            raise ValueError("card_database or set_card_data_loader is required.")
        if card_database is not None and set_card_data_loader is not None:
            raise ValueError(
                "card_database and set_card_data_loader are mutually exclusive."
            )
        self.splash_enabled = splash_enabled
        self._events: list[LiveSessionEvent] = []
        self._profile_client = profile_client
        self._profile_refresh_lock = Lock()
        self._profile_refresh_in_flight: ProfileRefreshRequest | None = None
        self._profile_refresh_executor = (
            None
            if profile_client is None
            else ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="draftomen-profile-refresh",
            )
        )
        self._closed = False
        self._last_profile_status: str | None = None
        self._selected_card_data = set_card_data_loader is not None
        self._last_card_data_status: str | None = None
        self.session = LiveSession(
            log_path=log_path,
            card_database=card_database,
            set_card_data_loader=set_card_data_loader,
            app_dir=app_dir,
            poll_interval=poll_interval,
            previous_log_path=previous_log_path,
            event_publisher=self._capture_event,
            profile_client=profile_client,
            ratings_loader=ratings_loader,
            splash_enabled=splash_enabled,
        )
        self.log_path = self.session.log_path

    @property
    def profile_refresh_in_flight(self) -> ProfileRefreshRequest | None:
        """Return the adapter-owned profile refresh currently running."""

        with self._profile_refresh_lock:
            return self._profile_refresh_in_flight

    def _schedule_profile_refresh(self) -> None:
        """Start one pending profile refresh without blocking log polling."""

        executor = self._profile_refresh_executor
        if executor is None:
            return
        request = self.session.profile_refresh_request()
        with self._profile_refresh_lock:
            if (
                self._closed
                or request is None
                or self._profile_refresh_in_flight is not None
            ):
                return
            self._profile_refresh_in_flight = request
        try:
            executor.submit(self._refresh_profile_worker, request)
        except RuntimeError:
            with self._profile_refresh_lock:
                if self._profile_refresh_in_flight == request:
                    self._profile_refresh_in_flight = None
            return

    def _refresh_profile_worker(self, request: ProfileRefreshRequest) -> None:
        """Run ProfileClient.refresh and return its result through LiveSession."""

        try:
            with self._profile_refresh_lock:
                if (
                    self._closed
                    or self._profile_refresh_in_flight != request
                ):
                    return
            if self.session.profile_refresh_request() != request:
                return
            if self._profile_client is None:
                return
            result = self._profile_client.refresh(
                request.set_code,
                request.event_format,
            )
            if not isinstance(result, ProfileRefreshResult):
                raise TypeError("ProfileClient.refresh returned an invalid result.")
            with self._profile_refresh_lock:
                if (
                    self._closed
                    or self._profile_refresh_in_flight != request
                ):
                    return
                self.session.complete_profile_refresh(request=request, result=result)
        except Exception as error:  # pragma: no cover - defensive adapter boundary.
            with self._profile_refresh_lock:
                if (
                    self._closed
                    or self._profile_refresh_in_flight != request
                ):
                    return
                self.session.fail_profile_refresh(
                    request=request,
                    error_message=str(error),
                )
        finally:
            with self._profile_refresh_lock:
                if self._profile_refresh_in_flight == request:
                    self._profile_refresh_in_flight = None
                    schedule_next = not self._closed
                else:
                    schedule_next = False
            if schedule_next:
                self._schedule_profile_refresh()

    def close(self) -> None:
        """Stop the session and synchronously release the adapter worker."""

        with self._profile_refresh_lock:
            if self._closed:
                return
            self._closed = True
            executor = self._profile_refresh_executor
            self._profile_refresh_executor = None

        try:
            self.session.stop()
        finally:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            with self._profile_refresh_lock:
                self._profile_refresh_in_flight = None


    def _capture_event(self, published: LiveSessionEvent) -> None:
        if (
            isinstance(published.event, DraftCompletedEvent)
            and published.snapshot.draft is not None
        ):
            snapshot = self.session.dispatch(
                command=RequestBuild(allow_splash=self.splash_enabled),
            )
            published = replace(published, snapshot=snapshot)

        self._events.append(published)

    def poll_once(self) -> str:
        """Process one follower poll cycle and return rendered output.
        Empty strings mean no complete draft events were available this cycle.
        """

        self._events.clear()
        self.session.poll_once()
        self._schedule_profile_refresh()
        return self._render_published_events()

    def scan_startup_files(self, *, include_previous: bool = True) -> str:
        """Process Player-prev.log and Player.log from the beginning.
        This is opt-in recovery for a watch session started mid-draft.
        """
        self._events.clear()
        self.session.scan_startup_files(
            include_previous=include_previous,
            include_pre_draft_detection=True,
        )
        self._schedule_profile_refresh()
        return self._render_published_events()

    def process_lines(self, *, lines: Iterable[str]) -> str:
        """Pass complete log lines through the shared live session.
        Formatting consumes only published session events and commands.
        """

        self._events.clear()
        self.session.process_lines(lines=lines)
        self._schedule_profile_refresh()
        return self._render_published_events()

    def _render_published_events(self) -> str:
        output_lines: list[str] = []
        for published in self._events:
            output_lines.extend(self._format_event(published=published))

        self._events.clear()
        card_data_status = self._card_data_status_text()
        if (
            card_data_status is not None
            and card_data_status != self._last_card_data_status
        ):
            output_lines.append(card_data_status)
        self._last_card_data_status = card_data_status

        profile_status = self._profile_status_text()
        if profile_status is not None and profile_status != self._last_profile_status:
            output_lines.append(profile_status)
            self._last_profile_status = profile_status
        return _join_output_lines(lines=output_lines)

    def _card_data_status_text(self) -> str | None:
        if not self._selected_card_data:
            return None
        state = self.session.snapshot.card_data
        if state.phase != DataLoadPhase.FAILED:
            return None
        return f"Status: {state.message}"

    def _profile_status_text(self) -> str | None:
        state = self.session.snapshot.set_profile
        if state.set_code is None:
            return None
        maturity = state.maturity or "unavailable"
        status = f"Profile: {maturity}"
        if state.refresh_outcome is not None:
            status += f" ({state.refresh_outcome})"
        return f"Status: {status}"

    def _presentation_card_database(self) -> CardDatabase:
        """Return the session's currently selected database for rendering."""

        database = self.session.card_database
        if database is None:
            raise RuntimeError("Card metadata is not ready.")
        return database


    def _format_event(self, *, published: LiveSessionEvent) -> list[str]:
        event = published.event
        if isinstance(event, AccountEvent):
            label = _account_label(published=published, account_id=event.client_id)
            if event.previous_client_id is None:
                headline = f"Active account: {label}"
            else:
                previous_label = _account_label(
                    published=published,
                    account_id=event.previous_client_id,
                )
                headline = f"Account switched: {previous_label} -> {label}"

            return [headline, f"Status: active account {label}", ""]

        if isinstance(event, QuickDraftDetectedEvent):
            return []

        if isinstance(event, DraftStartedEvent):
            account_label = _account_label(
                published=published,
                account_id=event.account_id,
            )
            return [
                "Draft started: "
                f"{event.event_name} (set {event.set_code}, draft {event.course_id})",
                f"Status: active account {account_label}, draft {event.course_id}",
                "",
            ]

        if isinstance(event, PackOfferedEvent):
            if published.scored_pack is None:
                if self.session.snapshot.card_data.phase == DataLoadPhase.FAILED:
                    return []
                raise RuntimeError("Shared live session did not score the offered pack.")
            card_database = self._presentation_card_database()
            pack_lines = format_pack_offered_event(
                event=event,
                card_database=card_database,
                scored_pack=published.scored_pack,
            )
            account_label = _account_label(
                published=published,
                account_id=event.account_id,
            )
            lines = [
                "Status: "
                f"active account {account_label}, "
                f"pick P{event.pack_number + 1}P{event.pick_number + 1}, "
                f"{_color_status_from_pack_lines(lines=pack_lines)}, "
                f"data {_data_source_from_pack_lines(lines=pack_lines)}"
            ]
            lines.extend(_pack_lines_without_color_status(lines=pack_lines))
            return lines

        if isinstance(event, PickMadeEvent):
            if self.session.snapshot.card_data.phase == DataLoadPhase.FAILED:
                return []
            lines = format_pick_made_event(
                event=event,
                card_database=self._presentation_card_database(),
            )
            lines.append("")
            return lines

        if isinstance(event, DraftCompletedEvent):
            lines = format_draft_completed_event(event=event)
            lines.append("")
            if (
                published.snapshot.draft is not None
                and self.session.snapshot.card_data.phase != DataLoadPhase.FAILED
            ):
                lines.extend(self._format_build_sheet(published=published))
            return lines

        return []

    def _format_build_sheet(self, *, published: LiveSessionEvent) -> list[str]:
        result = published.snapshot.build
        if result is None:
            error = next(
                (
                    error
                    for error in published.snapshot.errors
                    if error.operation == OperationKind.BUILD
                ),
                None,
            )
            if error is None:
                raise DeckBuilderError("Deck build did not return a result.")

            message = error.message.removeprefix("Deck build failed: ")
            raise DeckBuilderError(message)

        if (
            result.domain_pool is None
            or result.domain_selection is None
            or result.domain_spell_selection is None
            or result.domain_mana_base is None
        ):
            raise DeckBuilderError("Deck build did not include domain details.")

        pool = replace(
            result.domain_pool,
            source_label=(
                f"watch {result.domain_pool.account_id}/"
                f"{result.domain_pool.draft_id}"
            ),
        )
        return format_build_result(
            pool=pool,
            selection=result.domain_selection,
            spell_selection=result.domain_spell_selection,
            mana_base=result.domain_mana_base,
        ).rstrip("\n").splitlines()


def run_plain_watch(
    *,
    log_path: PathInput,
    card_database: CardDatabase | None = None,
    set_card_data_loader: SetCardDataLoader | None = None,
    app_dir: PathInput | None = None,
    output: TextIO | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    once: bool = False,
    startup_scan: bool = False,
    stop_after_empty_polls: int | None = None,
    ratings_loader: RatingsLoader | None = None,
    splash_enabled: bool = True,
    profile_client: ProfileClient | None = None,
) -> int:
    """Run watch --plain until interrupted or a test stop condition fires.
    Profile refreshes run in a separate adapter-owned worker.
    """

    if output is None:
        output = sys.stdout

    watcher = PlainLogWatcher(
        log_path=log_path,
        card_database=card_database,
        set_card_data_loader=set_card_data_loader,
        app_dir=app_dir,
        poll_interval=poll_interval,
        ratings_loader=ratings_loader,
        splash_enabled=splash_enabled,
        profile_client=profile_client,
    )
    try:
        output.write("Draft Omen watch\n")
        output.write(f"Watching: {watcher.log_path}\n")
        output.write("Mode: plain-text\n\n")
        output.flush()

        if startup_scan:
            _write_if_present(output=output, text=watcher.scan_startup_files())

        if once:
            _write_if_present(output=output, text=watcher.poll_once())
            return 0

        empty_polls = 0
        while True:
            text = watcher.poll_once()
            if text:
                _write_if_present(output=output, text=text)
                empty_polls = 0
            else:
                empty_polls += 1

            if (
                stop_after_empty_polls is not None
                and empty_polls >= stop_after_empty_polls
            ):
                return 0

            time.sleep(poll_interval)
    finally:
        watcher.close()


def _write_if_present(*, output: TextIO, text: str) -> None:
    if not text:
        return

    output.write(text)
    output.flush()


def _account_label(*, published: LiveSessionEvent, account_id: str | None) -> str:
    if account_id is None:
        return "unknown"

    identity = next(
        (
            account
            for account in published.snapshot.accounts
            if account.account_id == account_id
        ),
        None,
    )
    if identity is None or identity.screen_name is None:
        return account_id

    return f"{identity.screen_name} ({account_id})"


def _data_source_from_pack_lines(*, lines: list[str]) -> str:
    for line in lines:
        if line.startswith("Data source: "):
            return line.removeprefix("Data source: ")

    return "unknown"


def _color_status_from_pack_lines(*, lines: list[str]) -> str:
    for line in lines:
        if line.startswith("Status: inferred pair "):
            return line.removeprefix("Status: ")

    return "inferred pair open, commitment 0% (open), pool unknown"


def _pack_lines_without_color_status(*, lines: list[str]) -> list[str]:
    return [line for line in lines if not line.startswith("Status: inferred pair ")]


def _join_output_lines(*, lines: list[str]) -> str:
    if not lines:
        return ""

    return "\n".join(lines) + "\n"
