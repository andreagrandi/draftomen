"""Drive a pinned Draftmancer draft through Draft Omen's typed event boundary.
Print one compact success summary or an actionable failure line.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys
import tempfile
import uuid

from draftomen.carddb import CardDatabase
from draftomen.draftmancer import (
    DRAFTMANCER_REVISION,
    DraftmancerAdapter,
    DraftmancerAdapterError,
    DraftmancerConfig,
    intersect_supported_set_codes,
)
from draftomen.events import (
    DraftCompletedEvent,
    DraftEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
)
from draftomen.session import LiveSession
from draftomen.set_card_data import SetCardData
from draftomen.test_draft import (
    _load_canonical_grp_ids_by_scryfall_id,
    _load_supported_set_codes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER_URL = "http://127.0.0.1:3000"
DEFAULT_SET_CODE = "HOB"
DEFAULT_SCRYFALL_BULK_FILE = (
    REPO_ROOT
    / ".draftomen"
    / "corpus-cache"
    / "sources"
    / "scryfall-default-cards.jsonl.gz"
)
DEFAULT_TIMEOUT_SECONDS = 10.0
DRAFTMANCER_SEAT_COUNT = 8
DRAFTMANCER_BOT_COUNT = 7
EXPECTED_PACK_NUMBERS = (0, 1, 2)


class DraftmancerSmokeError(RuntimeError):
    """Report a failed real Draftmancer smoke run.
    The message is suitable for the command-line failure summary.
    """


def build_parser() -> argparse.ArgumentParser:
    """Build the Draftmancer smoke command parser.
    The parser keeps simulator paths and network settings explicit.
    """

    parser = argparse.ArgumentParser(
        description="Smoke-test the pinned Draftmancer protocol adapter"
    )
    parser.add_argument(
        "--draftmancer-dir",
        type=Path,
        required=True,
        help="sibling checkout of the pinned Draftmancer repository",
    )
    parser.add_argument(
        "--scryfall-bulk-file",
        type=Path,
        default=DEFAULT_SCRYFALL_BULK_FILE,
        help=(
            "local Scryfall default-cards JSONL bulk file "
            f"(default: {DEFAULT_SCRYFALL_BULK_FILE})"
        ),
    )
    parser.add_argument(
        "--server-url",
        default=DEFAULT_SERVER_URL,
        help=f"Draftmancer server URL (default: {DEFAULT_SERVER_URL})",
    )
    parser.add_argument(
        "--set-code",
        default=DEFAULT_SET_CODE,
        help=f"set code to draft (default: {DEFAULT_SET_CODE})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"per-operation timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    return parser


def _draftomen_set_codes() -> tuple[str, ...]:
    """Enumerate Draft Omen's generated card-data set artifacts.
    Artifact names are the lowercase set-code capability boundary.
    """

    card_data_dir = REPO_ROOT / "website" / "public" / "card-data"
    if not card_data_dir.is_dir():
        raise DraftmancerSmokeError(f"missing Draft Omen card-data directory: {card_data_dir}")
    codes = tuple(
        path.name[: -len(".json.gz")]
        for path in sorted(card_data_dir.glob("*.json.gz"))
        if path.is_file()
    )
    if not codes:
        raise DraftmancerSmokeError(f"no Draft Omen card-data artifacts found in {card_data_dir}")
    return codes


def _prepare_card_database(*, set_code: str) -> CardDatabase:
    """Validate and load one canonical Draft Omen card-data artifact.
    The returned database is the identity boundary used by the adapter and session.
    """

    card_path = REPO_ROOT / "website" / "public" / "card-data" / f"{set_code}.json.gz"
    if not card_path.is_file():
        raise DraftmancerSmokeError(f"missing Draft Omen card-data artifact: {card_path}")
    try:
        card_data = SetCardData.from_gzip_bytes(
            card_path.read_bytes(),
            expected_set_code=set_code,
        )
    except (OSError, TypeError, ValueError) as error:
        raise DraftmancerSmokeError(
            f"card-data artifact {card_path} is invalid: {error}"
        ) from error
    return card_data.to_card_database()


def _validate_event_stream(
    *,
    events: tuple[DraftEvent, ...],
    expected_grp_ids: tuple[int, ...],
    session: LiveSession,
) -> None:
    """Validate the complete typed event stream and persisted session state.
    The checks reject fabricated, reordered, incomplete, or identity-mismatched runs.
    """

    if not events or not isinstance(events[0], DraftStartedEvent):
        raise DraftmancerSmokeError("event stream did not start with DraftStartedEvent")
    if not isinstance(events[-1], DraftCompletedEvent):
        raise DraftmancerSmokeError("event stream did not end with DraftCompletedEvent")
    body = events[1:-1]
    if len(body) % 2 != 0:
        raise DraftmancerSmokeError("event stream does not contain alternating offer/pick pairs")
    offers = body[::2]
    picks = body[1::2]
    if not all(isinstance(event, PackOfferedEvent) for event in offers):
        raise DraftmancerSmokeError("event stream contains a non-offer between lifecycle events")
    if not all(isinstance(event, PickMadeEvent) for event in picks):
        raise DraftmancerSmokeError("event stream contains a non-pick between lifecycle events")
    if len(offers) != len(expected_grp_ids) or len(picks) != len(expected_grp_ids):
        raise DraftmancerSmokeError("expected pick count does not match lifecycle event count")
    offer_coordinates = tuple((event.pack_number, event.pick_number) for event in offers)
    pick_coordinates = tuple((event.pack_number, event.pick_number) for event in picks)
    if pick_coordinates != offer_coordinates:
        raise DraftmancerSmokeError("PickMadeEvent coordinates do not match their offers")
    if tuple(event.chosen_grp_id for event in picks) != expected_grp_ids:
        raise DraftmancerSmokeError(
            "PickMadeEvent Arena IDs do not match the first card from each offered pack"
        )
    if not offers or offer_coordinates[0] != (0, 0):
        raise DraftmancerSmokeError("event stream did not begin with pack 0 pick 0")
    for previous, current in zip(offers, offers[1:]):
        same_pack = (
            current.pack_number == previous.pack_number
            and current.pick_number == previous.pick_number + 1
        )
        next_pack = (
            current.pack_number == previous.pack_number + 1
            and current.pick_number == 0
        )
        if not same_pack and not next_pack:
            raise DraftmancerSmokeError(
                "pack progression is not chronological: "
                f"{(previous.pack_number, previous.pick_number)} then "
                f"{(current.pack_number, current.pick_number)}"
            )
    pack_numbers = tuple(sorted({event.pack_number for event in offers}))
    if pack_numbers != EXPECTED_PACK_NUMBERS:
        raise DraftmancerSmokeError(
            f"pack numbers are {pack_numbers!r}, expected {EXPECTED_PACK_NUMBERS!r}"
        )
    completed = events[-1]
    if completed.picked_grp_ids != expected_grp_ids:
        raise DraftmancerSmokeError("completion pool does not match expected Arena IDs")
    if completed.inferred:
        raise DraftmancerSmokeError("Draftmancer completion was inferred")
    state = session._active_draft_state()
    if state is None:
        raise DraftmancerSmokeError("LiveSession did not retain the completed draft")
    if not state.completed:
        raise DraftmancerSmokeError("LiveSession draft state is not complete")
    if state.pool_grp_ids != expected_grp_ids:
        raise DraftmancerSmokeError(
            "LiveSession pool does not match the expected Arena IDs"
        )


def _run_smoke(
    *,
    draftmancer_dir: Path,
    scryfall_bulk_file: Path,
    server_url: str,
    set_code: str,
    timeout_seconds: float,
) -> dict[str, object]:
    """Run one deterministic first-instance draft against Draftmancer.
    This helper owns no recommendation or simulator service-management behavior.
    """

    normalized_set_code = set_code.strip().casefold()
    if not normalized_set_code:
        raise DraftmancerSmokeError("set code must not be empty")
    draftmancer_codes = _load_supported_set_codes(draftmancer_dir=draftmancer_dir)
    draftomen_codes = _draftomen_set_codes()
    try:
        supported_codes = intersect_supported_set_codes(
            draftomen_set_codes=draftomen_codes,
            draftmancer_set_codes=draftmancer_codes,
        )
    except DraftmancerAdapterError as error:
        raise DraftmancerSmokeError(f"set capability metadata is invalid: {error}") from error
    if normalized_set_code not in supported_codes:
        raise DraftmancerSmokeError(
            f"requested set {normalized_set_code!r} is not supported by both Draft Omen and Draftmancer"
        )
    card_database = _prepare_card_database(set_code=normalized_set_code)
    canonical_grp_ids_by_scryfall_id = _load_canonical_grp_ids_by_scryfall_id(
        bulk_path=scryfall_bulk_file,
        set_code=normalized_set_code,
        card_database=card_database,
    )
    run_id = uuid.uuid4().hex
    session_id = f"draftomen-smoke-{run_id}"
    user_id = f"draftomen-smoke-user-{run_id}"
    user_name = "Draft Omen smoke"
    event_stream: list[DraftEvent] = []
    expected_grp_ids: list[int] = []
    with tempfile.TemporaryDirectory(prefix="draftmancer-smoke-") as app_dir:
        session = LiveSession(
            log_path=None,
            app_dir=Path(app_dir),
            card_database=card_database,
        )

        def event_sink(event: DraftEvent) -> None:
            event_stream.append(event)
            session.process_events(events=(event,))

        adapter = DraftmancerAdapter(
            config=DraftmancerConfig(
                server_url=server_url,
                session_id=session_id,
                user_id=user_id,
                user_name=user_name,
                set_code=normalized_set_code,
                timeout_seconds=timeout_seconds,
            ),
            card_database=card_database,
            canonical_grp_ids_by_scryfall_id=canonical_grp_ids_by_scryfall_id,
            event_sink=event_sink,
        )
        try:
            adapter.connect_and_start()
            while not adapter.completed:
                offered_instance_ids = adapter.offered_instance_ids
                if not offered_instance_ids:
                    raise DraftmancerSmokeError(
                        "Draftmancer exposed no active card instance before completion"
                    )
                if not event_stream or not isinstance(event_stream[-1], PackOfferedEvent):
                    raise DraftmancerSmokeError(
                        "Draftmancer active offer was not published before pick"
                    )
                offer = event_stream[-1]
                if not offer.offered_grp_ids:
                    raise DraftmancerSmokeError("Draftmancer published an empty active offer")
                expected_grp_ids.append(offer.offered_grp_ids[0])
                adapter.pick(unique_card_id=offered_instance_ids[0])
            _validate_event_stream(
                events=tuple(event_stream),
                expected_grp_ids=tuple(expected_grp_ids),
                session=session,
            )
        finally:
            adapter.close()
    return {
        "status": "ok",
        "revision": DRAFTMANCER_REVISION,
        "set_code": normalized_set_code,
        "seats": DRAFTMANCER_SEAT_COUNT,
        "bots": DRAFTMANCER_BOT_COUNT,
        "packs": len(EXPECTED_PACK_NUMBERS),
        "picks": len(expected_grp_ids),
        "pool_matches": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the deterministic Draftmancer protocol smoke.
    Return a process-style status and keep failures actionable.
    """

    args = build_parser().parse_args(args=argv)
    try:
        result = _run_smoke(
            draftmancer_dir=args.draftmancer_dir,
            scryfall_bulk_file=args.scryfall_bulk_file,
            server_url=args.server_url,
            set_code=args.set_code,
            timeout_seconds=args.timeout,
        )
    except Exception as error:
        print(f"Draftmancer smoke failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
