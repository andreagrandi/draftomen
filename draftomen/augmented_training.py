"""Train and gate one augmented set model from compact draft arrays."""

from __future__ import annotations

import csv
import gzip
import hashlib
import math
import multiprocessing
import os
import shutil
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from draftomen.augmented_artifact import (
    AUGMENTED_MAXIMUM_DELTA,
    AUGMENTED_MAXIMUM_MULTIPLIER,
    AugmentedArtifact,
    AugmentedArtifactError,
    AugmentedCalibration,
    AugmentedMetricSet,
    AugmentedMetricSummary,
    AugmentedSource,
)
from draftomen.augmented_training_data import (
    FEATURE_NAMES,
    AugmentedTrainingDataError,
    AugmentedTrainingSource,
    _candidate_id,
    _cards_by_name,
    _draft_time,
    _set_code,
    card_feature_membership,
)
from draftomen.carddb import CardDatabase
from draftomen.pickengine import PickEngine
from draftomen.set_profile import SetProfile


class AugmentedTrainingError(RuntimeError):
    """Report inputs that cannot produce a trustworthy augmented model."""


@dataclass(frozen=True, slots=True)
class ModelCTrainingConfig:
    """Fix Model C training and bounded-delta calibration settings.
    The defaults define a reproducible per-set training run.
    """

    seed: int = 20260920
    hidden_size: int = 32
    epochs: int = 15
    patience: int = 3
    batch_size: int = 2048
    learning_rate: float = 0.01
    weight_decay: float = 0.00001
    maximum_delta: float = 8.0
    calibration_multipliers: tuple[float, ...] = (
        0.0,
        0.25,
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
    )


@dataclass(frozen=True, slots=True)
class AugmentedTrainingResult:
    """Return a validated artifact only when both held-out metrics improve."""

    artifact: AugmentedArtifact | None
    report: dict[str, object]


@dataclass(slots=True)
class _Model:
    bias: np.ndarray
    input_weights: np.ndarray
    output_weights: np.ndarray

    def copy(self) -> _Model:
        return _Model(
            bias=self.bias.copy(),
            input_weights=self.input_weights.copy(),
            output_weights=self.output_weights.copy(),
        )


@dataclass(frozen=True, slots=True)
class _ArrayTrainingData:
    card_names: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    grp_ids: np.ndarray
    features: np.ndarray
    pack_mask: np.ndarray
    targets: np.ndarray
    draft_indices: np.ndarray
    global_picks: np.ndarray
    pack_numbers: np.ndarray
    pick_numbers: np.ndarray
    pool_counts: np.ndarray
    split_rows: dict[str, np.ndarray]
    split_drafts: dict[str, np.ndarray]
    rows_seen: int
    drafts_seen: int


@dataclass(frozen=True, slots=True)
class _BasicArrayScores:
    """Store runtime totals with raw-score, base-rating, and input-order ties."""

    integer_scores: np.ndarray
    tie_order: np.ndarray

    def __getitem__(self, rows: slice | np.ndarray) -> _BasicArrayScores:
        return _BasicArrayScores(
            integer_scores=self.integer_scores[rows],
            tie_order=self.tie_order[rows],
        )




_BASIC_SCORE_WORKER_STATE: tuple[
    _ArrayTrainingData,
    PickEngine,
    CardDatabase,
] | None = None


def _decompress_training_source(*, source: AugmentedTrainingSource) -> Path:
    path = Path(source.path)
    digest = hashlib.sha256()
    is_gzip = path.suffix == ".gz"
    try:
        with path.open(mode="rb") as handle:
            prefix = handle.read(2)
            digest.update(prefix)
            is_gzip = is_gzip or prefix == b"\x1f\x8b"
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise AugmentedTrainingError("Pinned draft dump could not be read.") from error
    if digest.hexdigest() != source.sha256:
        raise AugmentedTrainingError("Pinned draft dump checksum does not match.")
    if not is_gzip:
        return path
    destination = Path("/tmp") / f"draftomen-augmented-{source.sha256[:16]}.csv"
    partial = destination.with_suffix(".csv.part")
    print("Decompressing the pinned draft dump", flush=True)
    try:
        with (
            gzip.open(path, mode="rb") as input_file,
            partial.open(mode="wb") as output_file,
        ):
            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        partial.replace(destination)
    except (OSError, EOFError, zlib.error) as error:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise AugmentedTrainingError(
            "Pinned draft dump could not be decompressed."
        ) from error
    return destination


def _load_array_training_data(
    *,
    set_code: str,
    source: AugmentedTrainingSource,
    card_database: CardDatabase,
    complete_draft_picks: int = 42,
) -> _ArrayTrainingData:
    try:
        normalized_set = _set_code(value=set_code)
    except AugmentedTrainingDataError as error:
        raise AugmentedTrainingError(str(error)) from error
    if not isinstance(source, AugmentedTrainingSource):
        raise AugmentedTrainingError("source must be an AugmentedTrainingSource.")
    if not isinstance(card_database, CardDatabase):
        raise AugmentedTrainingError("card_database must be a CardDatabase.")
    if (
        isinstance(complete_draft_picks, bool)
        or not isinstance(complete_draft_picks, int)
        or complete_draft_picks <= 0
    ):
        raise AugmentedTrainingError("complete_draft_picks must be positive.")
    path = _decompress_training_source(source=source)
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle))
    except (OSError, StopIteration) as error:
        raise AugmentedTrainingError("Draft dump has no readable CSV header.") from error
    pack_columns = [value for value in header if value.startswith("pack_card_")]
    pool_columns = [value for value in header if value.startswith("pool_")]
    card_names = tuple(value.removeprefix("pack_card_") for value in pack_columns)
    if not pack_columns or card_names != tuple(
        value.removeprefix("pool_") for value in pool_columns
    ):
        raise AugmentedTrainingError("Pack and pool card columns do not match.")
    metadata_columns = [
        "expansion",
        "event_type",
        "draft_id",
        "draft_time",
        "pack_number",
        "pick_number",
        "pick",
    ]
    missing_columns = sorted(set(metadata_columns) - set(header))
    if missing_columns:
        raise AugmentedTrainingError(
            f"Draft dump is missing required columns: {missing_columns}."
        )
    # 17Lands added pick_2 for Pick Two Draft; older dumps such as DFT lack it.
    has_second_pick_column = "pick_2" in header
    if has_second_pick_column:
        metadata_columns.append("pick_2")
    try:
        cards_by_name = _cards_by_name(
            database=card_database,
            set_code=normalized_set,
        )
    except AugmentedTrainingDataError as error:
        raise AugmentedTrainingError(str(error)) from error
    missing = sorted(set(card_names) - set(cards_by_name))
    if missing:
        raise AugmentedTrainingError(
            f"Card metadata does not cover the draft dump: {missing}"
        )
    candidate_ids: list[str] = []
    for name in card_names:
        card = cards_by_name[name]
        if not card.set_code or card.set_code.upper() != normalized_set:
            raise AugmentedTrainingError(
                f"Card metadata for {name!r} has no matching set code."
            )
        try:
            candidate_ids.append(_candidate_id(card=card))
        except AugmentedTrainingDataError as error:
            raise AugmentedTrainingError(str(error)) from error
    if len(set(candidate_ids)) != len(candidate_ids):
        raise AugmentedTrainingError(
            "Card metadata contains duplicate Oracle candidate IDs."
        )
    integer_columns = pack_columns + pool_columns + ["pack_number", "pick_number"]
    schema_overrides = {column: pl.UInt8 for column in integer_columns}
    print(
        f"Reading {len(card_names)} card columns from the {normalized_set} draft dump",
        flush=True,
    )
    frame = pl.read_csv(
        path,
        columns=metadata_columns + pack_columns + pool_columns,
        schema_overrides=schema_overrides,
        low_memory=False,
    )
    rows_seen = len(frame)
    try:
        expansions = {
            _set_code(value=value)
            for value in frame["expansion"].unique().to_list()
        }
    except AugmentedTrainingDataError as error:
        raise AugmentedTrainingError(str(error)) from error
    if expansions != {normalized_set}:
        raise AugmentedTrainingError("Public draft data contains the wrong set.")
    events = frame["event_type"].unique().to_list()
    if not events or any(
        not isinstance(value, str)
        or value.strip().casefold() != source.event_type.casefold()
        for value in events
    ):
        raise AugmentedTrainingError("Public draft data contains the wrong event type.")
    normalized_times: dict[str, str] = {}
    for value in frame["draft_time"].unique().to_list():
        if not isinstance(value, str):
            raise AugmentedTrainingError("Draft data contains an invalid timestamp.")
        try:
            normalized_times[value] = _draft_time(value=value).isoformat(
                timespec="microseconds"
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise AugmentedTrainingError(
                "Draft data contains an invalid timestamp."
            ) from error
    frame = frame.with_columns(
        pl.col("draft_time").replace_strict(normalized_times).alias("draft_time")
    )
    name_to_index = {name: index for index, name in enumerate(card_names)}
    frame = frame.with_columns(
        (
            pl.col("pack_number").cast(pl.UInt16) * 14
            + pl.col("pick_number").cast(pl.UInt16)
        ).alias("global_pick"),
        pl.col("pick")
        .replace_strict(name_to_index, default=None)
        .cast(pl.Int32)
        .alias("target"),
        pl.sum_horizontal(pool_columns).cast(pl.UInt16).alias("pool_total"),
    )
    target_known = frame["target"].is_not_null().to_numpy()
    target_indices = frame["target"].fill_null(0).to_numpy().astype(np.int32)
    pack_values = frame.select(pack_columns).to_numpy().astype(np.uint8, copy=False)
    row_indices = np.arange(len(frame))
    target_offered = pack_values[row_indices, target_indices] > 0
    if has_second_pick_column:
        no_second_pick = (
            frame["pick_2"].is_null() | (frame["pick_2"] == "")
        ).to_numpy()
    else:
        no_second_pick = np.ones(len(frame), dtype=bool)
    pool_consistent = (
        frame["pool_total"].to_numpy()
        == frame["global_pick"].to_numpy()
    )
    frame = frame.with_columns(
        pl.Series(
            "valid",
            target_known & target_offered & no_second_pick & pool_consistent,
            dtype=pl.Boolean,
        )
    )
    drafts_seen = frame["draft_id"].n_unique()
    complete = (
        frame.group_by("draft_id")
        .agg(
            pl.len().alias("row_count"),
            pl.col("global_pick").n_unique().alias("unique_picks"),
            pl.col("global_pick").min().alias("minimum_pick"),
            pl.col("global_pick").max().alias("maximum_pick"),
            pl.col("valid").sum().alias("valid_rows"),
            pl.col("draft_time").n_unique().alias("draft_time_count"),
            pl.col("draft_time").min().alias("draft_time"),
        )
        .filter(
            (pl.col("row_count") == complete_draft_picks)
            & (pl.col("unique_picks") == complete_draft_picks)
            & (pl.col("minimum_pick") == 0)
            & (pl.col("maximum_pick") == complete_draft_picks - 1)
            & (pl.col("valid_rows") == complete_draft_picks)
            & (pl.col("draft_time_count") == 1)
        )
        .sort(["draft_time", "draft_id"])
    )
    draft_ids = complete["draft_id"].to_numpy()
    train_end = math.floor(len(draft_ids) * 0.70)
    validation_end = train_end + math.floor(len(draft_ids) * 0.15)
    split_ids = {
        "train": draft_ids[:train_end],
        "validation": draft_ids[train_end:validation_end],
        "test": draft_ids[validation_end:],
    }
    if any(len(values) == 0 for values in split_ids.values()):
        raise AugmentedTrainingError("Training requires non-empty draft partitions.")
    draft_to_index = {
        draft_id: index for index, draft_id in enumerate(draft_ids)
    }
    frame = (
        frame.with_row_index("source_row")
        .filter(pl.col("draft_id").is_in(draft_ids))
        .with_columns(
            pl.col("draft_id")
            .replace_strict(draft_to_index)
            .cast(pl.Int32)
            .alias("draft_index")
        )
        .sort(["draft_index", "global_pick"])
    )
    ordered_rows = frame["source_row"].to_numpy()
    pack_mask = pack_values[ordered_rows].astype(bool, copy=False)
    pool_counts = frame.select(pool_columns).to_numpy().astype(np.uint8, copy=False)
    membership = np.asarray(
        [
            card_feature_membership(card=cards_by_name[name])
            for name in card_names
        ],
        dtype=np.uint8,
    )
    features = (pool_counts.astype(np.uint16) @ membership.astype(np.uint16)).astype(
        np.uint8
    )
    draft_indices = frame["draft_index"].to_numpy().astype(np.int32, copy=False)
    split_drafts = {
        name: np.asarray(
            [draft_to_index[draft_id] for draft_id in values],
            dtype=np.int32,
        )
        for name, values in split_ids.items()
    }
    split_rows = {
        name: np.flatnonzero(np.isin(draft_indices, values))
        for name, values in split_drafts.items()
    }
    print(
        f"Kept {len(draft_ids):,} complete drafts and {len(frame):,} picks",
        flush=True,
    )
    return _ArrayTrainingData(
        card_names=card_names,
        candidate_ids=tuple(candidate_ids),
        grp_ids=np.asarray(
            [cards_by_name[name].grp_id for name in card_names],
            dtype=np.int64,
        ),
        features=features,
        pack_mask=pack_mask,
        targets=frame["target"].to_numpy().astype(np.int32, copy=False),
        draft_indices=draft_indices,
        global_picks=frame["global_pick"].to_numpy().astype(np.uint8, copy=False),
        pack_numbers=frame["pack_number"].to_numpy().astype(np.uint8, copy=False),
        pick_numbers=frame["pick_number"].to_numpy().astype(np.uint8, copy=False),
        pool_counts=pool_counts,
        split_rows=split_rows,
        split_drafts=split_drafts,
        rows_seen=rows_seen,
        drafts_seen=drafts_seen,
    )



def _array_logits(
    *, model: _Model, features: np.ndarray, batch_size: int
) -> np.ndarray:
    logits = np.empty((len(features), len(model.bias)), dtype=np.float32)
    for offset in range(0, len(features), batch_size):
        selected = features[offset : offset + batch_size]
        hidden = np.tanh(np.log1p(selected.astype(np.float32)) @ model.input_weights)
        logits[offset : offset + len(selected)] = (
            hidden @ model.output_weights + model.bias
        )
    return logits


def _array_ranks(
    *, scores: np.ndarray, pack_mask: np.ndarray, targets: np.ndarray
) -> np.ndarray:
    target_scores = scores[np.arange(len(scores)), targets]
    candidate_indices = np.arange(scores.shape[1])[None, :]
    better = scores > target_scores[:, None]
    tied_before = (scores == target_scores[:, None]) & (
        candidate_indices < targets[:, None]
    )
    return (
        1 + np.sum(pack_mask & (better | tied_before), axis=1)
    ).astype(np.int16)


def _runtime_array_ranks(
    *,
    basic_scores: np.ndarray | _BasicArrayScores,
    pack_mask: np.ndarray,
    targets: np.ndarray,
    deltas: np.ndarray | None = None,
) -> np.ndarray:
    if isinstance(basic_scores, _BasicArrayScores):
        integer_scores = basic_scores.integer_scores
        tie_order = basic_scores.tie_order
        raw_scores = None
    else:
        raw_scores = basic_scores
        integer_scores = np.floor(np.clip(raw_scores, 0.0, 100.0) + 0.5)
        tie_order = None

    ordering_scores = (
        integer_scores
        if deltas is None
        else np.clip(integer_scores + deltas, 0.0, 100.0)
    )
    rows = np.arange(len(targets))
    target_scores = ordering_scores[rows, targets]
    tied = ordering_scores == target_scores[:, None]
    better = ordering_scores > target_scores[:, None]
    if tie_order is not None:
        target_order = tie_order[rows, targets]
        better |= tied & (tie_order < target_order[:, None])
    else:
        assert raw_scores is not None
        target_raw_scores = raw_scores[rows, targets]
        candidate_indices = np.arange(ordering_scores.shape[1])[None, :]
        better |= tied & (
            (raw_scores > target_raw_scores[:, None])
            | (
                (raw_scores == target_raw_scores[:, None])
                & (candidate_indices < targets[:, None])
            )
        )
    return (1 + np.sum(pack_mask & better, axis=1)).astype(np.int16)


def _rank_metrics(*, ranks: np.ndarray) -> dict[str, float | int]:
    return {
        "picks": len(ranks),
        "top_1": float(np.mean(ranks == 1)),
        "mean_reciprocal_rank": float(
            np.mean(1.0 / ranks.astype(np.float64))
        ),
    }


def _train_array_model(
    *, data: _ArrayTrainingData, config: ModelCTrainingConfig
) -> tuple[_Model, dict[str, object]]:
    rng = np.random.default_rng(config.seed)
    model = _Model(
        bias=np.zeros(len(data.card_names), dtype=np.float32),
        input_weights=rng.normal(
            loc=0.0,
            scale=0.02,
            size=(len(FEATURE_NAMES), config.hidden_size),
        ).astype(np.float32),
        output_weights=rng.normal(
            loc=0.0,
            scale=0.02,
            size=(config.hidden_size, len(data.card_names)),
        ).astype(np.float32),
    )
    parameters = (model.bias, model.input_weights, model.output_weights)
    first = [np.zeros_like(value) for value in parameters]
    second = [np.zeros_like(value) for value in parameters]
    train_rows = data.split_rows["train"]
    validation_rows = data.split_rows["validation"]
    best = model.copy()
    best_mrr = -math.inf
    best_epoch = 0
    stale = 0
    step = 0
    history: list[dict[str, float | int]] = []
    for epoch in range(1, config.epochs + 1):
        started = time.monotonic()
        order = rng.permutation(train_rows)
        loss_sum = 0.0
        for offset in range(0, len(order), config.batch_size):
            rows = order[offset : offset + config.batch_size]
            transformed = np.log1p(data.features[rows].astype(np.float32))
            hidden = np.tanh(transformed @ model.input_weights)
            logits = hidden @ model.output_weights + model.bias
            mask = data.pack_mask[rows]
            logits[~mask] = -1.0e9
            logits -= np.max(logits, axis=1, keepdims=True)
            probabilities = np.exp(logits)
            probabilities /= np.sum(probabilities, axis=1, keepdims=True)
            targets = data.targets[rows]
            target_probabilities = probabilities[np.arange(len(rows)), targets]
            loss_sum += float(
                np.sum(-np.log(target_probabilities + 1.0e-12))
            )
            probabilities[np.arange(len(rows)), targets] -= 1.0
            probabilities /= len(rows)
            gradients = [
                np.sum(probabilities, axis=0),
                transformed.T
                @ (
                    (probabilities @ model.output_weights.T)
                    * (1.0 - hidden * hidden)
                )
                + config.weight_decay * model.input_weights,
                hidden.T @ probabilities
                + config.weight_decay * model.output_weights,
            ]
            step += 1
            for index, parameter in enumerate(parameters):
                gradient = gradients[index]
                first[index] *= 0.9
                first[index] += 0.1 * gradient
                second[index] *= 0.999
                second[index] += 0.001 * gradient * gradient
                first_hat = first[index] / (1.0 - 0.9**step)
                second_hat = second[index] / (1.0 - 0.999**step)
                parameter -= config.learning_rate * first_hat / (
                    np.sqrt(second_hat) + 1.0e-8
                )
        validation_logits = _array_logits(
            model=model,
            features=data.features[validation_rows],
            batch_size=config.batch_size,
        )
        validation_ranks = _array_ranks(
            scores=validation_logits,
            pack_mask=data.pack_mask[validation_rows],
            targets=data.targets[validation_rows],
        )
        validation_mrr = float(
            np.mean(1.0 / validation_ranks.astype(np.float64))
        )
        history.append(
            {
                "epoch": epoch,
                "mean_loss": loss_sum / len(train_rows),
                "validation_mrr": validation_mrr,
            }
        )
        print(
            f"Epoch {epoch}: loss={history[-1]['mean_loss']:.5f}, "
            f"validation MRR={validation_mrr:.5f}, "
            f"seconds={time.monotonic() - started:.1f}",
            flush=True,
        )
        if validation_mrr > best_mrr:
            best = model.copy()
            best_mrr = validation_mrr
            best_epoch = epoch
            stale = 0
        else:
            stale += 1
            if stale >= config.patience:
                break
    return best, {
        "seed": config.seed,
        "best_epoch": best_epoch,
        "best_validation_mrr": best_mrr,
        "epochs_run": len(history),
        "history": history,
    }


def _score_basic_rows(
    *,
    data: _ArrayTrainingData,
    rows: np.ndarray,
    engine: PickEngine,
    card_database: CardDatabase,
) -> _BasicArrayScores:
    integer_scores = np.zeros(
        (len(rows), len(data.card_names)),
        dtype=np.uint8,
    )
    tie_order = np.full(
        (len(rows), len(data.card_names)),
        np.iinfo(np.uint16).max,
        dtype=np.uint16,
    )
    for position, row_value in enumerate(rows):
        row = int(row_value)
        offered_indices = np.flatnonzero(data.pack_mask[row])
        offered_ids = tuple(int(data.grp_ids[index]) for index in offered_indices)
        pool_ids = tuple(
            int(data.grp_ids[index])
            for index in np.flatnonzero(data.pool_counts[row])
            for _ in range(int(data.pool_counts[row, index]))
        )
        global_pick_index = int(data.global_picks[row]) + 1
        scored = engine.score_pack(
            offered_grp_ids=offered_ids,
            card_database=card_database,
            pool_grp_ids=pool_ids,
            pick_index=global_pick_index,
            pack_number=int(data.pack_numbers[row]),
            pick_number=int(data.pick_numbers[row]),
            global_pick_index=global_pick_index,
            estimated_remaining_picks=max(0, 42 - global_pick_index),
        )
        cards_by_id = {card.card.grp_id: card for card in scored.cards}
        row_cards = [
            cards_by_id[int(data.grp_ids[index])] for index in offered_indices
        ]
        integer_scores[position, offered_indices] = [
            card.basic_score for card in row_cards
        ]
        ordered_positions = sorted(
            range(len(row_cards)),
            key=lambda index: (
                -row_cards[index].raw_score,
                -row_cards[index].base_rating,
                row_cards[index].original_index,
            ),
        )
        row_tie_order = np.empty(len(row_cards), dtype=np.uint16)
        row_tie_order[ordered_positions] = np.arange(
            len(row_cards), dtype=np.uint16
        )
        tie_order[position, offered_indices] = row_tie_order
    return _BasicArrayScores(
        integer_scores=integer_scores,
        tie_order=tie_order,
    )



def _score_basic_chunk(
    task: tuple[int, np.ndarray],
) -> tuple[int, _BasicArrayScores]:
    if _BASIC_SCORE_WORKER_STATE is None:
        raise RuntimeError("Basic DO score worker state was not initialized.")
    data, engine, database = _BASIC_SCORE_WORKER_STATE
    offset, rows = task
    return offset, _score_basic_rows(
        data=data,
        rows=rows,
        engine=engine,
        card_database=database,
    )


def _build_array_basic_scores(
    *,
    data: _ArrayTrainingData,
    rows: np.ndarray,
    card_database: CardDatabase,
    set_profile: SetProfile,
) -> _BasicArrayScores:
    global _BASIC_SCORE_WORKER_STATE

    engine = PickEngine(set_profile=set_profile)
    score_shape = (len(rows), len(data.card_names))
    integer_scores = np.zeros(score_shape, dtype=np.uint8)
    tie_order = np.full(
        score_shape,
        np.iinfo(np.uint16).max,
        dtype=np.uint16,
    )
    chunk_size = 5000
    tasks = [
        (offset, rows[offset : offset + chunk_size])
        for offset in range(0, len(rows), chunk_size)
    ]
    worker_count = min(8, os.cpu_count() or 1)
    if (
        "fork" not in multiprocessing.get_all_start_methods()
        or worker_count == 1
        or len(tasks) == 1
    ):
        for offset, selected in tasks:
            chunk_scores = _score_basic_rows(
                data=data,
                rows=selected,
                engine=engine,
                card_database=card_database,
            )
            end = offset + len(selected)
            integer_scores[offset:end] = chunk_scores.integer_scores
            tie_order[offset:end] = chunk_scores.tie_order
        return _BasicArrayScores(
            integer_scores=integer_scores,
            tie_order=tie_order,
        )
    _BASIC_SCORE_WORKER_STATE = (data, engine, card_database)
    completed = 0
    started = time.monotonic()
    context = multiprocessing.get_context("fork")
    with context.Pool(processes=worker_count) as pool:
        for offset, chunk_scores in pool.imap_unordered(
            _score_basic_chunk,
            tasks,
        ):
            end = offset + len(chunk_scores.integer_scores)
            integer_scores[offset:end] = chunk_scores.integer_scores
            tie_order[offset:end] = chunk_scores.tie_order
            completed += end - offset
            print(
                f"Basic DO evaluated {completed:,}/{len(rows):,} picks in "
                f"{time.monotonic() - started:.1f} seconds",
                flush=True,
            )
    _BASIC_SCORE_WORKER_STATE = None
    return _BasicArrayScores(
        integer_scores=integer_scores,
        tie_order=tie_order,
    )


def _centered_array_deltas(
    *, logits: np.ndarray, pack_mask: np.ndarray
) -> np.ndarray:
    offered_counts = np.sum(pack_mask, axis=1, keepdims=True)
    offered_means = np.sum(logits * pack_mask, axis=1, keepdims=True) / offered_counts
    return logits - offered_means


def _calibrate_arrays(
    *,
    model: _Model,
    data: _ArrayTrainingData,
    rows: np.ndarray,
    basic_scores: np.ndarray | _BasicArrayScores,
    config: ModelCTrainingConfig,
) -> dict[str, object]:
    mask = data.pack_mask[rows]
    logits = _array_logits(
        model=model,
        features=data.features[rows],
        batch_size=config.batch_size,
    )
    centered = _centered_array_deltas(logits=logits, pack_mask=mask)
    candidates: list[dict[str, float]] = []
    for multiplier in config.calibration_multipliers:
        deltas = np.clip(
            centered * multiplier,
            -config.maximum_delta,
            config.maximum_delta,
        )
        ranks = _runtime_array_ranks(
            basic_scores=basic_scores,
            pack_mask=mask,
            targets=data.targets[rows],
            deltas=deltas,
        )
        metrics = _rank_metrics(ranks=ranks)
        candidates.append(
            {
                "multiplier": multiplier,
                "top_1": float(metrics["top_1"]),
                "mean_reciprocal_rank": float(
                    metrics["mean_reciprocal_rank"]
                ),
            }
        )
    selected = max(
        candidates,
        key=lambda value: (
            value["mean_reciprocal_rank"],
            value["top_1"],
            -value["multiplier"],
        ),
    )
    return {
        "method": "offered_mean_centered_logit_clip",
        "multiplier": selected["multiplier"],
        "minimum_delta": -config.maximum_delta,
        "maximum_delta": config.maximum_delta,
        "selection_partition": "validation",
        "selection_metric": "mean_reciprocal_rank_then_top_1",
        "candidates": candidates,
    }


def _evaluate_arrays(
    *,
    model: _Model,
    data: _ArrayTrainingData,
    rows: np.ndarray,
    basic_scores: np.ndarray | _BasicArrayScores,
    calibration: dict[str, object],
    config: ModelCTrainingConfig,
) -> dict[str, dict[str, float | int]]:
    mask = data.pack_mask[rows]
    targets = data.targets[rows]
    basic_ranks = _runtime_array_ranks(
        basic_scores=basic_scores,
        pack_mask=mask,
        targets=targets,
    )
    logits = _array_logits(
        model=model,
        features=data.features[rows],
        batch_size=config.batch_size,
    )
    centered = _centered_array_deltas(logits=logits, pack_mask=mask)
    deltas = np.clip(
        centered * float(calibration["multiplier"]),
        float(calibration["minimum_delta"]),
        float(calibration["maximum_delta"]),
    )
    augmented_ranks = _runtime_array_ranks(
        basic_scores=basic_scores,
        pack_mask=mask,
        targets=targets,
        deltas=deltas,
    )
    return {
        "basic_do": _rank_metrics(ranks=basic_ranks),
        "basic_plus_augmented": _rank_metrics(ranks=augmented_ranks),
    }




def _validate_config(*, config: ModelCTrainingConfig) -> None:
    if not isinstance(config, ModelCTrainingConfig):
        raise AugmentedTrainingError("config must be a ModelCTrainingConfig.")
    if (
        isinstance(config.seed, bool)
        or not isinstance(config.seed, int)
        or config.seed < 0
    ):
        raise AugmentedTrainingError("The training seed must be a non-negative integer.")
    counts = (config.hidden_size, config.epochs, config.patience, config.batch_size)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in counts
    ):
        raise AugmentedTrainingError("Training counts must be positive integers.")
    optimizer_values = (config.learning_rate, config.weight_decay)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in optimizer_values
    ) or config.learning_rate <= 0.0 or config.weight_decay < 0.0:
        raise AugmentedTrainingError("Optimizer settings are invalid.")
    multipliers = config.calibration_multipliers
    if (
        isinstance(config.maximum_delta, bool)
        or not isinstance(config.maximum_delta, (int, float))
        or not math.isfinite(config.maximum_delta)
        or not 0.0 < config.maximum_delta <= AUGMENTED_MAXIMUM_DELTA
        or not isinstance(multipliers, (tuple, list))
        or not multipliers
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= AUGMENTED_MAXIMUM_MULTIPLIER
            for value in multipliers
        )
    ):
        raise AugmentedTrainingError(
            "Calibration settings are invalid or exceed runtime bounds."
        )


def train_and_gate_augmented_set(
    *,
    set_code: str,
    source: AugmentedTrainingSource,
    card_database: CardDatabase,
    set_profile: SetProfile,
    complete_draft_picks: int = 42,
    config: ModelCTrainingConfig = ModelCTrainingConfig(),
) -> AugmentedTrainingResult:
    """Train and return a runtime artifact only after strict held-out promotion."""

    _validate_config(config=config)
    try:
        normalized_set = _set_code(value=set_code)
    except AugmentedTrainingDataError as error:
        raise AugmentedTrainingError(str(error)) from error
    if not isinstance(source, AugmentedTrainingSource):
        raise AugmentedTrainingError("source must be an AugmentedTrainingSource.")
    if not isinstance(card_database, CardDatabase):
        raise AugmentedTrainingError("card_database must be a CardDatabase.")
    if not isinstance(set_profile, SetProfile):
        raise AugmentedTrainingError("set_profile must be a SetProfile.")
    if set_profile.set_code != normalized_set.casefold():
        raise AugmentedTrainingError("Set profile does not match the requested set.")
    if set_profile.event_format != source.event_type.casefold():
        raise AugmentedTrainingError(
            "Set profile format does not match the training source event type."
        )
    try:
        runtime_source = AugmentedSource(
            attribution=source.attribution,
            event_type=source.event_type,
            license=source.license,
            retrieved_at=source.retrieved_at,
            sha256=source.sha256,
            url=source.url,
        )
    except AugmentedArtifactError as error:
        raise AugmentedTrainingError(
            f"Training source provenance is invalid: {error}"
        ) from error
    data = _load_array_training_data(
        set_code=normalized_set,
        source=source,
        card_database=card_database,
        complete_draft_picks=complete_draft_picks,
    )
    model, training = _train_array_model(data=data, config=config)
    validation_rows = data.split_rows["validation"]
    test_rows = data.split_rows["test"]
    evaluation_rows = np.concatenate((validation_rows, test_rows))
    print(
        f"Scoring {len(evaluation_rows):,} validation and test picks with Basic DO",
        flush=True,
    )
    evaluation_basic = _build_array_basic_scores(
        data=data,
        rows=evaluation_rows,
        card_database=card_database,
        set_profile=set_profile,
    )
    validation_count = len(validation_rows)
    calibration = _calibrate_arrays(
        model=model,
        data=data,
        rows=validation_rows,
        basic_scores=evaluation_basic[:validation_count],
        config=config,
    )
    evaluation = _evaluate_arrays(
        model=model,
        data=data,
        rows=test_rows,
        basic_scores=evaluation_basic[validation_count:],
        calibration=calibration,
        config=config,
    )
    promoted = all(
        evaluation["basic_plus_augmented"][metric]
        > evaluation["basic_do"][metric]
        for metric in ("top_1", "mean_reciprocal_rank")
    )
    training_metadata = {
        "seed": config.seed,
        "hidden_size": config.hidden_size,
        "epochs": config.epochs,
        "patience": config.patience,
        "batch_size": config.batch_size,
        "learning_rate": config.learning_rate,
        "weight_decay": config.weight_decay,
        "best_epoch": training["best_epoch"],
        "best_validation_mrr": training["best_validation_mrr"],
    }
    artifact: AugmentedArtifact | None = None
    artifact_sha256: str | None = None
    if promoted:
        basic_evaluation = evaluation["basic_do"]
        augmented_evaluation = evaluation["basic_plus_augmented"]
        picks = int(basic_evaluation["picks"])
        if picks != int(augmented_evaluation["picks"]) or picks <= 0:
            raise AugmentedTrainingError(
                "Held-out metric summaries must have equal positive pick counts."
            )
        try:
            artifact_calibration = AugmentedCalibration(
                method=str(calibration["method"]),
                multiplier=float(calibration["multiplier"]),
                minimum_delta=float(calibration["minimum_delta"]),
                maximum_delta=float(calibration["maximum_delta"]),
                selection_partition=str(calibration["selection_partition"]),
                selection_metric=str(calibration["selection_metric"]),
                candidates=tuple(
                    {
                        "multiplier": float(candidate["multiplier"]),
                        "top_1": float(candidate["top_1"]),
                        "mean_reciprocal_rank": float(
                            candidate["mean_reciprocal_rank"]
                        ),
                    }
                    for candidate in calibration["candidates"]
                ),
            )
            artifact = AugmentedArtifact(
                set_code=normalized_set.casefold(),
                candidate_ids=data.candidate_ids,
                input_weights=tuple(
                    tuple(float(value) for value in row)
                    for row in model.input_weights
                ),
                output_weights=tuple(
                    tuple(float(value) for value in row)
                    for row in model.output_weights
                ),
                bias=tuple(float(value) for value in model.bias),
                calibration=artifact_calibration,
                source=runtime_source,
                evaluation=AugmentedMetricSummary(
                    picks=picks,
                    basic_do=AugmentedMetricSet(
                        top_1=float(basic_evaluation["top_1"]),
                        mean_reciprocal_rank=float(
                            basic_evaluation["mean_reciprocal_rank"]
                        ),
                    ),
                    basic_plus_augmented=AugmentedMetricSet(
                        top_1=float(augmented_evaluation["top_1"]),
                        mean_reciprocal_rank=float(
                            augmented_evaluation["mean_reciprocal_rank"]
                        ),
                    ),
                ),
                training=training_metadata,
            )
        except (AugmentedArtifactError, KeyError, TypeError, ValueError) as error:
            raise AugmentedTrainingError(
                f"Trained model failed runtime artifact validation: {error}"
            ) from error
        artifact_sha256 = hashlib.sha256(artifact.to_bytes()).hexdigest()
    source_value = runtime_source.to_json()
    report: dict[str, object] = {
        "schema_version": 1,
        "set_code": normalized_set.casefold(),
        "model": "coarse_context_model_c",
        "promotion": {
            "passed": promoted,
            "rule": "both_metrics_strictly_improve",
        },
        "evaluation": evaluation,
        "calibration": calibration,
        "training": training,
        "feature_schema": list(FEATURE_NAMES),
        "source": source_value,
        "data": {
            "rows_seen": data.rows_seen,
            "drafts_seen": data.drafts_seen,
            "drafts_accepted": sum(
                len(values) for values in data.split_drafts.values()
            ),
            "picks_accepted": len(data.targets),
            "partition_drafts": {
                name: len(data.split_drafts[name])
                for name in ("train", "validation", "test")
            },
        },
        "artifact_sha256": artifact_sha256,
    }
    return AugmentedTrainingResult(artifact=artifact, report=report)
