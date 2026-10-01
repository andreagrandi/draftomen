"""Turn Moxgate draft page snapshots into typed draft events.
Moxgate sends no pick message, so picks and coordinates come from consecutive snapshots.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from draftomen.carddb import CardDatabase
from draftomen.draft_format import DraftFormat
from draftomen.events import (
    DraftCompletedEvent,
    DraftEvent,
    DraftStartedEvent,
    PackOfferedEvent,
    PickMadeEvent,
)

MOXGATE_SCHEMA_VERSION = 1


class MoxgateSnapshotError(ValueError):
    """Raised when a Moxgate snapshot is malformed or does not follow the last one.
    The message names the field or the expected and received values.
    """


@dataclass(frozen=True, slots=True)
class MoxgateSnapshotCard:
    """One card in a Moxgate snapshot pack or pool.
    The Scryfall id identifies the print and the name is kept for error messages.
    """

    scryfall_id: str
    name: str


@dataclass(frozen=True, slots=True)
class MoxgateSnapshot:
    """One Moxgate draft page snapshot.
    The pick index is 0-based and counts the picks already made.
    """

    schema_version: int
    pick_index: int
    total_picks: int
    pack: tuple[MoxgateSnapshotCard, ...]
    pool: tuple[MoxgateSnapshotCard, ...]

    @classmethod
    def from_json(cls, *, text: str | bytes) -> MoxgateSnapshot:
        """Decode and validate a snapshot from JSON text.
        Raises MoxgateSnapshotError when the text is not a valid snapshot.
        """

        try:
            decoded = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise MoxgateSnapshotError(
                f"Moxgate snapshot is not valid JSON: {error}"
            ) from error

        return cls.from_mapping(value=decoded)

    @classmethod
    def from_mapping(cls, *, value: object) -> MoxgateSnapshot:
        """Validate a decoded JSON object as a snapshot.
        Raises MoxgateSnapshotError naming the first field that is invalid.
        """

        if not isinstance(value, Mapping):
            raise MoxgateSnapshotError("Moxgate snapshot must be a JSON object")

        schema_version = _required_int(value=value, field_name="schema_version")
        if schema_version != MOXGATE_SCHEMA_VERSION:
            raise MoxgateSnapshotError(
                f"Unsupported Moxgate schema_version {schema_version}; "
                f"supported version is {MOXGATE_SCHEMA_VERSION}"
            )

        pick_index = _required_int(value=value, field_name="pick_index")
        total_picks = _required_int(value=value, field_name="total_picks")
        if total_picks < 1:
            raise MoxgateSnapshotError(
                f"Moxgate total_picks must be at least 1, got {total_picks}"
            )

        if pick_index > total_picks:
            raise MoxgateSnapshotError(
                f"Moxgate pick_index {pick_index} exceeds total_picks {total_picks}"
            )

        pack = _required_cards(value=value, field_name="pack")
        pool = _required_cards(value=value, field_name="pool")
        if len(pool) != pick_index:
            raise MoxgateSnapshotError(
                f"Moxgate pool has {len(pool)} cards but pick_index is {pick_index}"
            )

        if pick_index < total_picks and not pack:
            raise MoxgateSnapshotError(
                f"Moxgate pack is empty at pick_index {pick_index} of {total_picks}"
            )

        if pick_index == total_picks and pack:
            raise MoxgateSnapshotError(
                f"Moxgate pack must be empty once pick_index reaches total_picks {total_picks}"
            )

        return cls(
            schema_version=schema_version,
            pick_index=pick_index,
            total_picks=total_picks,
            pack=pack,
            pool=pool,
        )


class MoxgateAdapter:
    """Derive draft events from successive Moxgate snapshots.
    The adapter is pure and keeps only the state needed to diff the next snapshot.
    """

    def __init__(
        self,
        *,
        card_database: CardDatabase,
        canonical_grp_ids_by_scryfall_id: Mapping[str, int],
        draft_id: str,
        account_id: str | None,
    ) -> None:
        if not isinstance(draft_id, str) or draft_id == "":
            raise MoxgateSnapshotError("Moxgate draft_id must be a non-empty string")

        self._card_database = card_database
        self._grp_ids_by_scryfall_id = canonical_grp_ids_by_scryfall_id
        self._draft_id = draft_id
        self._account_id = account_id
        self._last_snapshot: MoxgateSnapshot | None = None
        self._set_code = ""
        self._picks_per_pack = 0
        self._total_picks = 0
        self._previous_pack: tuple[int, ...] = ()
        self._picks: tuple[int, ...] = ()
        self._completed = False

    @property
    def completed(self) -> bool:
        """Return whether the final snapshot has been accepted.
        Only an identical repeat of that snapshot is accepted afterwards.
        """

        return self._completed

    def process(self, *, snapshot: MoxgateSnapshot) -> tuple[DraftEvent, ...]:
        """Return the events this snapshot adds, in order.
        A rejected snapshot raises and leaves the adapter unchanged.
        """

        if snapshot == self._last_snapshot:
            return ()

        if self._completed:
            raise MoxgateSnapshotError(
                "Moxgate draft is already complete; no more snapshots expected"
            )

        pack = self._resolve_cards(cards=snapshot.pack)
        pool = self._resolve_cards(cards=snapshot.pool)
        if self._last_snapshot is None:
            return self._process_first(snapshot=snapshot, pack=pack)

        return self._process_next(snapshot=snapshot, pack=pack, pool=pool)

    def _process_first(
        self,
        *,
        snapshot: MoxgateSnapshot,
        pack: tuple[int, ...],
    ) -> tuple[DraftEvent, ...]:
        if snapshot.pick_index != 0:
            raise MoxgateSnapshotError(
                "Joining a Moxgate draft after its first pick is not supported; "
                f"first snapshot has pick_index {snapshot.pick_index}"
            )

        if not pack:
            raise MoxgateSnapshotError("First Moxgate snapshot has an empty pack")

        picks_per_pack = len(pack)
        if snapshot.total_picks % picks_per_pack != 0:
            raise MoxgateSnapshotError(
                f"Moxgate total_picks {snapshot.total_picks} is not a multiple of "
                f"the first pack size {picks_per_pack}"
            )

        set_code = self._majority_set_code(pack=pack)
        self._set_code = set_code
        self._picks_per_pack = picks_per_pack
        self._total_picks = snapshot.total_picks
        self._previous_pack = pack
        self._last_snapshot = snapshot
        return (
            DraftStartedEvent(
                event_name=self._event_name,
                set_code=set_code,
                course_id=self._draft_id,
                account_id=self._account_id,
            ),
            self._pack_offered(snapshot=snapshot, pack=pack),
        )

    def _process_next(
        self,
        *,
        snapshot: MoxgateSnapshot,
        pack: tuple[int, ...],
        pool: tuple[int, ...],
    ) -> tuple[DraftEvent, ...]:
        previous = self._last_snapshot
        assert previous is not None
        if snapshot.total_picks != self._total_picks:
            raise MoxgateSnapshotError(
                f"Moxgate total_picks changed from {self._total_picks} to {snapshot.total_picks}"
            )

        expected_index = previous.pick_index + 1
        if snapshot.pick_index != expected_index:
            raise MoxgateSnapshotError(
                f"Moxgate pick_index expected {expected_index} but received {snapshot.pick_index}"
            )

        picked_grp_id = self._picked_grp_id(snapshot=snapshot, pool=pool)
        pack_number, pick_number = self._coordinates(index=previous.pick_index)
        picks = (*self._picks, picked_grp_id)
        events: list[DraftEvent] = [
            PickMadeEvent(
                event_name=self._event_name,
                set_code=self._set_code,
                pack_number=pack_number,
                pick_number=pick_number,
                selected_grp_ids=(picked_grp_id,),
                account_id=self._account_id,
            )
        ]
        if snapshot.pick_index < snapshot.total_picks:
            _, next_pick_number = self._coordinates(index=snapshot.pick_index)
            expected_size = self._picks_per_pack - next_pick_number
            if len(pack) != expected_size:
                raise MoxgateSnapshotError(
                    f"Moxgate pack at pick_index {snapshot.pick_index} expected "
                    f"{expected_size} cards but received {len(pack)}"
                )

            events.append(self._pack_offered(snapshot=snapshot, pack=pack, picks=picks))
        else:
            events.append(
                DraftCompletedEvent(
                    event_name=self._event_name,
                    set_code=self._set_code,
                    pack_number=pack_number,
                    pick_number=pick_number,
                    picked_grp_ids=picks,
                    inferred=False,
                    account_id=self._account_id,
                )
            )
            self._completed = True

        self._picks = picks
        self._previous_pack = pack
        self._last_snapshot = snapshot
        return tuple(events)

    def _picked_grp_id(
        self, *, snapshot: MoxgateSnapshot, pool: tuple[int, ...]
    ) -> int:
        previous_counts = Counter(self._picks)
        new_counts = Counter(pool)
        added = new_counts - previous_counts
        removed = previous_counts - new_counts
        if removed or sum(added.values()) != 1:
            raise MoxgateSnapshotError(
                f"Moxgate pool at pick_index {snapshot.pick_index} must hold the previous "
                "pool plus exactly one new card"
            )

        (picked_grp_id,) = added
        if picked_grp_id not in self._previous_pack:
            raise MoxgateSnapshotError(
                f"Moxgate pool at pick_index {snapshot.pick_index} gained grpId "
                f"{picked_grp_id}, which was not in the previous pack"
            )

        return picked_grp_id

    def _pack_offered(
        self,
        *,
        snapshot: MoxgateSnapshot,
        pack: tuple[int, ...],
        picks: tuple[int, ...] | None = None,
    ) -> PackOfferedEvent:
        pack_number, pick_number = self._coordinates(index=snapshot.pick_index)
        return PackOfferedEvent(
            event_name=self._event_name,
            set_code=self._set_code,
            pack_number=pack_number,
            pick_number=pick_number,
            offered_grp_ids=pack,
            pool_grp_ids=self._picks if picks is None else picks,
            account_id=self._account_id,
            picks_per_pack=self._picks_per_pack,
            draft_format=DraftFormat.QUICK,
            cards_per_pick=1,
        )

    def _coordinates(self, *, index: int) -> tuple[int, int]:
        return index // self._picks_per_pack, index % self._picks_per_pack

    @property
    def _event_name(self) -> str:
        return f"QuickDraft_{self._set_code}_Moxgate_{self._draft_id}"

    def _resolve_cards(
        self, *, cards: tuple[MoxgateSnapshotCard, ...]
    ) -> tuple[int, ...]:
        grp_ids: list[int] = []
        for card in cards:
            grp_id = self._grp_ids_by_scryfall_id.get(card.scryfall_id)
            if grp_id is None:
                raise MoxgateSnapshotError(
                    f"Moxgate card {card.name!r} with Scryfall id {card.scryfall_id!r} "
                    "has no Arena grpId"
                )

            if self._card_database.unresolved_grp_ids(grp_ids=(grp_id,)):
                raise MoxgateSnapshotError(
                    f"Moxgate card {card.name!r} with Scryfall id {card.scryfall_id!r} "
                    f"maps to grpId {grp_id}, which the card database cannot resolve"
                )

            grp_ids.append(grp_id)

        return tuple(grp_ids)

    def _majority_set_code(self, *, pack: tuple[int, ...]) -> str:
        counts = Counter(
            code
            for grp_id in pack
            if (code := self._card_database.lookup(grp_id=grp_id).set_code)
        )
        if not counts:
            raise MoxgateSnapshotError(
                "No card in the first Moxgate pack has a set code"
            )

        # Ties break on the alphabetically first code.
        best = min(counts, key=lambda code: (-counts[code], code))
        return best.upper()


def _required_int(*, value: Mapping[object, object], field_name: str) -> int:
    if field_name not in value:
        raise MoxgateSnapshotError(f"Moxgate snapshot is missing {field_name}")

    item = value[field_name]
    if isinstance(item, bool) or not isinstance(item, int):
        raise MoxgateSnapshotError(f"Moxgate snapshot {field_name} must be an integer")

    if item < 0:
        raise MoxgateSnapshotError(
            f"Moxgate snapshot {field_name} must not be negative"
        )

    return item


def _required_cards(
    *,
    value: Mapping[object, object],
    field_name: str,
) -> tuple[MoxgateSnapshotCard, ...]:
    if field_name not in value:
        raise MoxgateSnapshotError(f"Moxgate snapshot is missing {field_name}")

    items = value[field_name]
    if not isinstance(items, list):
        raise MoxgateSnapshotError(f"Moxgate snapshot {field_name} must be a list")

    cards: list[MoxgateSnapshotCard] = []
    for position, item in enumerate(items):
        label = f"{field_name}[{position}]"
        if not isinstance(item, Mapping):
            raise MoxgateSnapshotError(f"Moxgate snapshot {label} must be an object")

        cards.append(
            MoxgateSnapshotCard(
                scryfall_id=_required_text(
                    value=item, field_name="scryfall_id", label=label
                ),
                name=_required_text(value=item, field_name="name", label=label),
            )
        )

    return tuple(cards)


def _required_text(
    *, value: Mapping[object, object], field_name: str, label: str
) -> str:
    item = value.get(field_name)
    if not isinstance(item, str) or item == "":
        raise MoxgateSnapshotError(
            f"Moxgate snapshot {label}.{field_name} must be a non-empty string"
        )

    return item
