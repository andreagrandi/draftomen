"""Prepare leakage-safe candidate rows from public draft data.
The producer supplies the set, pinned dump, and matching card metadata.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from os import PathLike
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from draftomen.carddb import CardDatabase, CardInfo
from draftomen.public_dump import PublicDumpReader, PublicDumpSource

type PathInput = str | PathLike[str]
type TrainingPartition = Literal["train", "validation", "test"]

COLORS = ("W", "U", "B", "R", "G")
COMPOSITION = ("Creature", "Noncreature")
CURVE_BUCKETS = ("0-1", "2", "3", "4", "5", "6+")
CARD_TYPES = (
    "Artifact",
    "Battle",
    "Creature",
    "Enchantment",
    "Instant",
    "Kindred",
    "Land",
    "Planeswalker",
    "Sorcery",
)
FEATURE_NAMES = tuple(
    [f"color:{value}" for value in COLORS]
    + [f"composition:{value}" for value in COMPOSITION]
    + [f"curve:{value}" for value in CURVE_BUCKETS]
    + [f"type:{value}" for value in CARD_TYPES]
)
_REQUIRED_COLUMNS = {
    "expansion",
    "event_type",
    "draft_id",
    "draft_time",
    "pack_number",
    "pick_number",
    "pick",
    "pick_2",
}


class AugmentedTrainingDataError(RuntimeError):
    """Report invalid inputs that cannot produce trustworthy training rows."""


@dataclass(frozen=True, slots=True)
class AugmentedTrainingSource:
    """Describe one pinned local dump and its public provenance.
    The URL identifies the downloaded dataset, while path supplies its bytes.
    """

    path: PathInput
    url: str
    sha256: str
    retrieved_at: str
    attribution: str
    license: str
    event_type: str

    def __post_init__(self) -> None:
        for field_name in (
            "url",
            "sha256",
            "retrieved_at",
            "attribution",
            "license",
            "event_type",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise AugmentedTrainingDataError(
                    f"Training source {field_name} must be a non-empty string."
                )
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256):
            raise AugmentedTrainingDataError("Training source SHA-256 is invalid.")
        object.__setattr__(self, "sha256", self.sha256.lower())

    def public_dump_source(self) -> PublicDumpSource:
        """Return the pinned local source used by the shared dump reader."""

        return PublicDumpSource(
            name="17lands-public-draft-data",
            path=self.path,
            sha256=self.sha256,
            retrieved_at=self.retrieved_at,
            attribution=self.attribution,
            license=self.license,
        )


@dataclass(frozen=True, slots=True)
class CandidateTrainingRow:
    """Store one offered candidate and the pre-pick summary used for training.
    Source draft identifiers, timestamps, rank, and outcomes are excluded.
    """

    draft_index: int
    partition: TrainingPartition
    pick_index: int
    candidate_id: str
    chosen: bool
    features: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BasicScoringRow:
    """Store exact pre-pick inputs needed to reproduce Basic DO scoring.
    These identities support evaluation and never enter Model C features.
    """

    draft_index: int
    partition: TrainingPartition
    pick_index: int
    pack_number: int
    pick_number: int
    candidate_ids: tuple[str, ...]
    offered_grp_ids: tuple[int, ...]
    pool_grp_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AugmentedTrainingReport:
    """Summarize accepted data without retaining row-level source values."""

    set_code: str
    event_type: str
    source_url: str
    retrieved_at: str
    sha256: str
    attribution: str
    license: str
    rows_seen: int
    drafts_seen: int
    drafts_accepted: int
    drafts_skipped: int
    candidate_rows: int
    skipped_drafts: Mapping[str, int]
    partition_drafts: Mapping[TrainingPartition, int]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "skipped_drafts",
            MappingProxyType(dict(sorted(self.skipped_drafts.items()))),
        )
        object.__setattr__(
            self,
            "partition_drafts",
            MappingProxyType(dict(self.partition_drafts)),
        )


@dataclass(frozen=True, slots=True)
class _DraftSummary:
    draft_key: str
    draft_time: datetime
    pick_indices: frozenset[int]
    row_count: int
    candidate_rows: int
    problems: frozenset[str]


@dataclass(frozen=True, slots=True)
class _DraftAssignment:
    index: int
    partition: TrainingPartition


@dataclass(frozen=True, slots=True)
class PreparedAugmentedTrainingData:
    """Hold a validated split and stream its candidate rows on demand.
    Raw source rows and draft identifiers never appear in the public result.
    """

    set_code: str
    source: AugmentedTrainingSource
    report: AugmentedTrainingReport
    _assignments: Mapping[str, _DraftAssignment]
    _candidate_ids: Mapping[str, str]
    _candidate_grp_ids: Mapping[str, int]
    _memberships: Mapping[str, tuple[int, ...]]

    @property
    def feature_names(self) -> tuple[str, ...]:
        """Return the fixed feature order expected by the tested model."""

        return FEATURE_NAMES

    def iter_rows(self) -> Iterator[CandidateTrainingRow]:
        """Read the pinned dump again and yield rows from accepted drafts."""

        reader = PublicDumpReader(self.source.public_dump_source())
        for row in reader.iter_rows():
            assignment = self._assignments.get(
                _draft_key(value=row.get("draft_id", ""))
            )
            if assignment is None:
                continue
            pick_index = _pick_index(row=row)
            features = _pool_features(row=row, memberships=self._memberships)
            chosen = row["pick"]
            for candidate_name in _offered_candidates(row=row):
                yield CandidateTrainingRow(
                    draft_index=assignment.index,
                    partition=assignment.partition,
                    pick_index=pick_index,
                    candidate_id=self._candidate_ids[candidate_name],
                    chosen=candidate_name == chosen,
                    features=features,
                )

    def iter_basic_scoring_rows(self) -> Iterator[BasicScoringRow]:
        """Yield exact pre-pick inputs for the Basic DO evaluation baseline.
        Card identities stay outside the coarse-context model feature rows.
        """

        reader = PublicDumpReader(self.source.public_dump_source())
        for row in reader.iter_rows():
            assignment = self._assignments.get(
                _draft_key(value=row.get("draft_id", ""))
            )
            if assignment is None:
                continue
            candidate_names = _offered_candidates(row=row)
            pool_grp_ids = tuple(
                self._candidate_grp_ids[column.removeprefix("pool_")]
                for column in sorted(row)
                if column.startswith("pool_")
                for _ in range(_count(value=row[column]))
            )
            yield BasicScoringRow(
                draft_index=assignment.index,
                partition=assignment.partition,
                pick_index=_pick_index(row=row),
                pack_number=_count(value=row["pack_number"]),
                pick_number=_count(value=row["pick_number"]),
                candidate_ids=tuple(
                    self._candidate_ids[name] for name in candidate_names
                ),
                offered_grp_ids=tuple(
                    self._candidate_grp_ids[name] for name in candidate_names
                ),
                pool_grp_ids=pool_grp_ids,
            )


def prepare_augmented_training_data(
    *,
    set_code: str,
    source: AugmentedTrainingSource,
    card_database: CardDatabase,
    complete_draft_picks: int = 42,
    train_fraction: float = 0.70,
    validation_fraction: float = 0.15,
) -> PreparedAugmentedTrainingData:
    """Validate complete drafts and assign chronological whole-draft splits.
    HOB and every later supported set use this same entry point.
    """

    normalized_set = _set_code(value=set_code)
    if not isinstance(card_database, CardDatabase):
        raise AugmentedTrainingDataError("card_database must be a CardDatabase.")
    if complete_draft_picks <= 0:
        raise AugmentedTrainingDataError("complete_draft_picks must be positive.")
    if not 0.0 < train_fraction < 1.0 or not 0.0 < validation_fraction < 1.0:
        raise AugmentedTrainingDataError(
            "Split fractions must be between zero and one."
        )
    if train_fraction + validation_fraction >= 1.0:
        raise AugmentedTrainingDataError("Split fractions must leave a test partition.")

    cards_by_name = _cards_by_name(database=card_database, set_code=normalized_set)
    candidate_ids = {
        name: _candidate_id(card=card) for name, card in cards_by_name.items()
    }
    candidate_grp_ids = {
        name: card.grp_id for name, card in cards_by_name.items()
    }
    memberships = {
        name: _feature_membership(card=card) for name, card in cards_by_name.items()
    }
    summaries, rows_seen = _scan_drafts(
        set_code=normalized_set,
        source=source,
        memberships=memberships,
    )
    accepted: list[_DraftSummary] = []
    skipped: Counter[str] = Counter()
    for summary in summaries.values():
        problems = set(summary.problems)
        expected = set(range(complete_draft_picks))
        if (
            summary.row_count != complete_draft_picks
            or summary.pick_indices != expected
        ):
            problems.add("incomplete_or_inconsistent_history")
        if problems:
            skipped.update(problems)
        else:
            accepted.append(summary)
    accepted.sort(key=lambda item: (item.draft_time, item.draft_key))
    assignments, partition_counts = _assign_partitions(
        drafts=accepted,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    report = AugmentedTrainingReport(
        set_code=normalized_set,
        event_type=source.event_type,
        source_url=source.url,
        retrieved_at=source.retrieved_at,
        sha256=source.sha256,
        attribution=source.attribution,
        license=source.license,
        rows_seen=rows_seen,
        drafts_seen=len(summaries),
        drafts_accepted=len(accepted),
        drafts_skipped=len(summaries) - len(accepted),
        candidate_rows=sum(item.candidate_rows for item in accepted),
        skipped_drafts=skipped,
        partition_drafts=partition_counts,
    )
    return PreparedAugmentedTrainingData(
        set_code=normalized_set,
        source=source,
        report=report,
        _assignments=MappingProxyType(assignments),
        _candidate_ids=MappingProxyType(candidate_ids),
        _candidate_grp_ids=MappingProxyType(candidate_grp_ids),
        _memberships=MappingProxyType(memberships),
    )


def _scan_drafts(
    *,
    set_code: str,
    source: AugmentedTrainingSource,
    memberships: Mapping[str, tuple[int, ...]],
) -> tuple[dict[str, _DraftSummary], int]:
    mutable: dict[str, dict[str, object]] = {}
    reader = PublicDumpReader(source.public_dump_source())
    for row in reader.iter_rows():
        _validate_source_row(row=row, set_code=set_code, event_type=source.event_type)
        key = _draft_key(value=row["draft_id"])
        state = mutable.setdefault(
            key,
            {
                "times": [],
                "picks": set(),
                "rows": 0,
                "candidates": 0,
                "problems": set(),
            },
        )
        state["rows"] = int(state["rows"]) + 1
        problems = state["problems"]
        assert isinstance(problems, set)
        try:
            draft_time = _draft_time(value=row["draft_time"])
            pick_index = _pick_index(row=row)
            offered = _offered_candidates(row=row)
        except (AugmentedTrainingDataError, ValueError):
            problems.add("invalid_row")
            continue
        features = _pool_features(row=row, memberships=memberships)
        times = state["times"]
        picks = state["picks"]
        assert isinstance(times, list)
        assert isinstance(picks, set)
        times.append(draft_time)
        if pick_index in picks:
            problems.add("duplicate_pick")
        picks.add(pick_index)
        state["candidates"] = int(state["candidates"]) + len(offered)
        if row["pick_2"].strip():
            problems.add("second_pick")
        if row["pick"] not in offered:
            problems.add("chosen_card_not_offered")
        if sum(features[5:7]) != pick_index or sum(features[7:13]) != pick_index:
            problems.add("inconsistent_pool_history")

    report = reader.report
    if report is None:
        raise AugmentedTrainingDataError("Public-draft reader did not finish.")
    summaries: dict[str, _DraftSummary] = {}
    for key, state in mutable.items():
        times = state["times"]
        picks = state["picks"]
        problems = state["problems"]
        assert isinstance(times, list)
        assert isinstance(picks, set)
        assert isinstance(problems, set)
        if not times:
            problems.add("invalid_row")
            times = [datetime.min.replace(tzinfo=UTC)]
        if len(set(times)) != 1:
            problems.add("inconsistent_draft_time")
        summaries[key] = _DraftSummary(
            draft_key=key,
            draft_time=min(times),
            pick_indices=frozenset(picks),
            row_count=int(state["rows"]),
            candidate_rows=int(state["candidates"]),
            problems=frozenset(problems),
        )
    return summaries, report.rows_seen


def _assign_partitions(
    *,
    drafts: list[_DraftSummary],
    train_fraction: float,
    validation_fraction: float,
) -> tuple[dict[str, _DraftAssignment], dict[TrainingPartition, int]]:
    train_end = math.floor(len(drafts) * train_fraction)
    validation_end = train_end + math.floor(len(drafts) * validation_fraction)
    counts: dict[TrainingPartition, int] = {
        "train": train_end,
        "validation": validation_end - train_end,
        "test": len(drafts) - validation_end,
    }
    if any(value == 0 for value in counts.values()):
        raise AugmentedTrainingDataError(
            "The chronological split produced an empty partition."
        )
    assignments: dict[str, _DraftAssignment] = {}
    for index, draft in enumerate(drafts):
        partition: TrainingPartition
        if index < train_end:
            partition = "train"
        elif index < validation_end:
            partition = "validation"
        else:
            partition = "test"
        assignments[draft.draft_key] = _DraftAssignment(
            index=index,
            partition=partition,
        )
    return assignments, counts


def _validate_source_row(
    *, row: Mapping[str, str], set_code: str, event_type: str
) -> None:
    if not _REQUIRED_COLUMNS.issubset(row):
        raise AugmentedTrainingDataError(
            "Public draft data uses an unsupported schema."
        )
    if row["expansion"].strip().upper() != set_code:
        raise AugmentedTrainingDataError("Public draft data contains the wrong set.")
    if row["event_type"].strip().casefold() != event_type.casefold():
        raise AugmentedTrainingDataError(
            "Public draft data contains the wrong event type."
        )


def _offered_candidates(*, row: Mapping[str, str]) -> tuple[str, ...]:
    candidates = tuple(
        sorted(
            column.removeprefix("pack_card_")
            for column, value in row.items()
            if column.startswith("pack_card_") and _count(value=value) > 0
        )
    )
    if not candidates:
        raise AugmentedTrainingDataError("Draft row has no offered cards.")
    return candidates


def _pool_features(
    *,
    row: Mapping[str, str],
    memberships: Mapping[str, tuple[int, ...]],
) -> tuple[int, ...]:
    features = [0] * len(FEATURE_NAMES)
    pool_columns = {
        column.removeprefix("pool_"): value
        for column, value in row.items()
        if column.startswith("pool_")
    }
    pack_names = {
        column.removeprefix("pack_card_")
        for column in row
        if column.startswith("pack_card_")
    }
    if set(pool_columns) != pack_names:
        raise AugmentedTrainingDataError("Pack and pool card columns do not match.")
    for name, value in pool_columns.items():
        count = _count(value=value)
        membership = memberships.get(name)
        if membership is None:
            raise AugmentedTrainingDataError(
                "Card metadata does not cover the draft dump."
            )
        for index, included in enumerate(membership):
            features[index] += count * included
    return tuple(features)


def _feature_membership(*, card: CardInfo) -> tuple[int, ...]:
    names = set(card.types)
    if card.type_line:
        names.update(re.findall(r"[A-Za-z]+", card.type_line))
    for face in card.faces:
        if face.type_line:
            names.update(re.findall(r"[A-Za-z]+", face.type_line))
    card_types = names & set(CARD_TYPES)
    colors = set(card.colors)
    for face in card.faces:
        colors.update(face.colors)
    bucket = _curve_bucket(mana_value=card.mana_value)
    values = [int(color in colors) for color in COLORS]
    values.extend((int("Creature" in card_types), int("Creature" not in card_types)))
    values.extend(int(value == bucket) for value in CURVE_BUCKETS)
    values.extend(int(value in card_types) for value in CARD_TYPES)
    return tuple(values)


def card_feature_membership(*, card: CardInfo) -> tuple[int, ...]:
    """Return one card's contribution to the fixed coarse feature schema.
    Offline trainers use the same membership logic as prepared rows.
    """

    return _feature_membership(card=card)


def _cards_by_name(*, database: CardDatabase, set_code: str) -> dict[str, CardInfo]:
    cards: dict[str, CardInfo] = {}
    for card in database.cards.values():
        if card.unknown or (card.set_code and card.set_code.upper() != set_code):
            continue
        names = {card.name}
        names.update(face.name for face in card.faces if face.name)
        for name in names:
            previous = cards.get(name)
            if previous is not None and (
                _candidate_id(card=previous) != _candidate_id(card=card)
                or _feature_membership(card=previous) != _feature_membership(card=card)
            ):
                raise AugmentedTrainingDataError(
                    f"Card metadata maps {name!r} to conflicting Oracle cards."
                )
            cards[name] = card
    if not cards:
        raise AugmentedTrainingDataError(
            "Card metadata has no cards for the requested set."
        )
    return cards


def _candidate_id(*, card: CardInfo) -> str:
    if not card.oracle_id:
        raise AugmentedTrainingDataError(
            f"Card metadata has no Oracle ID for {card.name!r}."
        )
    try:
        return str(UUID(card.oracle_id))
    except ValueError as error:
        raise AugmentedTrainingDataError(
            f"Card metadata has an invalid Oracle ID for {card.name!r}."
        ) from error


def _pick_index(*, row: Mapping[str, str]) -> int:
    pack_number = _count(value=row["pack_number"])
    pick_number = _count(value=row["pick_number"])
    return pack_number * 14 + pick_number


def _draft_time(*, value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _count(*, value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise AugmentedTrainingDataError("Draft count is not an integer.") from error
    if parsed < 0:
        raise AugmentedTrainingDataError("Draft count cannot be negative.")
    return parsed


def _curve_bucket(*, mana_value: float | None) -> str:
    value = 0.0 if mana_value is None else mana_value
    if value <= 1.0:
        return "0-1"
    if value >= 6.0:
        return "6+"
    return str(int(value))


def _draft_key(*, value: str) -> str:
    if not value.strip():
        raise AugmentedTrainingDataError("Draft row has no draft identifier.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _set_code(*, value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9]{2,8}", value.strip()
    ):
        raise AugmentedTrainingDataError(
            "Set code must contain 2 to 8 letters or digits."
        )
    return value.strip().upper()
