"""Train and gate the offline HOB coarse-context model.
The emitted JSON contains all parameters needed for framework-free inference.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import multiprocessing
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import numpy as np
import polars as pl

from draftomen.augmented_training_data import (
    FEATURE_NAMES,
    AugmentedTrainingSource,
    CandidateTrainingRow,
    PreparedAugmentedTrainingData,
    card_feature_membership,
)
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.pickengine import PickEngine
from draftomen.set_profile import SetProfile

BasicScoreKey: TypeAlias = tuple[int, int, str]
BasicScores: TypeAlias = Mapping[BasicScoreKey, float]


class AugmentedTrainingError(RuntimeError):
    """Report inputs that cannot produce a trustworthy HOB model.
    Training stops before it can write a publishable artifact.
    """


@dataclass(frozen=True, slots=True)
class ModelCTrainingConfig:
    """Fix Model C training and bounded-delta calibration settings.
    The defaults define the reproducible HOB pilot run.
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
class HobModelCTrainingResult:
    """Return the promotion verdict and the report written by one run.
    A failed verdict never includes a runtime artifact.
    """

    promoted: bool
    report: dict[str, object]


@dataclass(frozen=True, slots=True)
class _PickExample:
    partition: str
    features: np.ndarray
    candidate_indices: np.ndarray
    candidate_ids: tuple[str, ...]
    chosen_index: int
    basic_scores: np.ndarray


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


_BASIC_SCORE_WORKER_STATE: tuple[
    _ArrayTrainingData,
    PickEngine,
    CardDatabase,
] | None = None


def load_hob_card_database(*, path: Path) -> CardDatabase:
    """Load the checked-in HOB website card data for offline scoring.
    Website rows use arena_id as the Draft Omen grp_id.
    """

    with gzip.open(path, mode="rt", encoding="utf-8") as handle:
        value = json.load(handle)
    cards_value = value.get("cards") if isinstance(value, dict) else None
    if not isinstance(cards_value, list):
        raise AugmentedTrainingError("HOB card data has no cards array.")
    cards: dict[int, CardInfo] = {}
    for item in cards_value:
        if not isinstance(item, dict):
            raise AugmentedTrainingError("HOB card data contains an invalid card.")
        normalized = dict(item)
        normalized["grp_id"] = item.get("arena_id")
        normalized["source_provenance"] = ["website-public-card-data"]
        card = CardInfo.from_json(data=normalized)
        cards[card.grp_id] = card
    return CardDatabase(cards=cards)


def load_hob_set_profile(*, path: Path) -> SetProfile:
    """Load the checked-in HOB profile used by current Basic DO scoring.
    The profile format must match the public draft event.
    """

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, mode="rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AugmentedTrainingError("HOB set profile must be a JSON object.")
    return SetProfile.from_json(value)


def _decompress_training_source(*, source: AugmentedTrainingSource) -> Path:
    path = Path(source.path)
    digest = hashlib.sha256()
    with path.open(mode="rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != source.sha256:
        raise AugmentedTrainingError("Pinned HOB draft dump checksum does not match.")
    if path.suffix != ".gz":
        return path
    destination = Path("/tmp") / f"draftomen-hob-{source.sha256[:16]}.csv"
    partial = destination.with_suffix(".csv.part")
    print("Decompressing the pinned HOB draft dump", flush=True)
    with (
        gzip.open(path, mode="rb") as input_file,
        partial.open(mode="wb") as output_file,
    ):
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
    partial.replace(destination)
    return destination


def _card_names(*, database: CardDatabase) -> dict[str, CardInfo]:
    result: dict[str, CardInfo] = {}
    for card in database.cards.values():
        names = {card.name, *(face.name for face in card.faces if face.name)}
        for name in names:
            previous = result.get(name)
            if previous is not None and previous.grp_id != card.grp_id:
                if (
                    previous.oracle_id != card.oracle_id
                    or card_feature_membership(card=previous)
                    != card_feature_membership(card=card)
                ):
                    raise AugmentedTrainingError(
                        f"HOB card name {name!r} maps to conflicting cards."
                    )
                result[name] = min(
                    (previous, card),
                    key=lambda value: value.grp_id,
                )
            else:
                result[name] = card
    return result


def _load_array_training_data(
    *,
    source: AugmentedTrainingSource,
    card_database: CardDatabase,
    complete_draft_picks: int = 42,
) -> _ArrayTrainingData:
    path = _decompress_training_source(source=source)
    with path.open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    pack_columns = [value for value in header if value.startswith("pack_card_")]
    pool_columns = [value for value in header if value.startswith("pool_")]
    card_names = tuple(value.removeprefix("pack_card_") for value in pack_columns)
    if card_names != tuple(value.removeprefix("pool_") for value in pool_columns):
        raise AugmentedTrainingError("Pack and pool card columns do not match.")
    cards_by_name = _card_names(database=card_database)
    missing = sorted(set(card_names) - set(cards_by_name))
    if missing:
        raise AugmentedTrainingError(
            f"HOB card metadata does not cover the draft dump: {missing}"
        )
    metadata_columns = [
        "expansion",
        "event_type",
        "draft_id",
        "draft_time",
        "pack_number",
        "pick_number",
        "pick",
        "pick_2",
    ]
    integer_columns = pack_columns + pool_columns + ["pack_number", "pick_number"]
    schema_overrides = {column: pl.UInt8 for column in integer_columns}
    print(
        f"Reading {len(card_names)} card columns from the HOB draft dump",
        flush=True,
    )
    frame = pl.read_csv(
        path,
        columns=metadata_columns + pack_columns + pool_columns,
        schema_overrides=schema_overrides,
        low_memory=False,
    )
    rows_seen = len(frame)
    if frame["expansion"].unique().to_list() != ["HOB"]:
        raise AugmentedTrainingError("Public draft data contains the wrong set.")
    if frame["event_type"].unique().to_list() != [source.event_type]:
        raise AugmentedTrainingError("Public draft data contains the wrong event type.")
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
    no_second_pick = (
        frame["pick_2"].is_null() | (frame["pick_2"] == "")
    ).to_numpy()
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
    cards = tuple(cards_by_name[name] for name in card_names)
    return _ArrayTrainingData(
        card_names=card_names,
        candidate_ids=tuple(str(card.oracle_id) for card in cards),
        grp_ids=np.asarray([card.grp_id for card in cards], dtype=np.int64),
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


def build_basic_do_scores(
    *,
    prepared: PreparedAugmentedTrainingData,
    card_database: CardDatabase,
    set_profile: SetProfile,
) -> dict[BasicScoreKey, float]:
    """Score every accepted pick with the current deterministic Basic DO path.
    Exact card identities are used only for this evaluation baseline.
    """

    engine = PickEngine(
        set_profile=set_profile,
        enhanced_relationships_enabled=False,
    )
    scores: dict[BasicScoreKey, float] = {}
    for row in prepared.iter_basic_scoring_rows():
        global_pick_index = row.pick_index + 1
        scored = engine.score_pack(
            offered_grp_ids=row.offered_grp_ids,
            card_database=card_database,
            pool_grp_ids=row.pool_grp_ids,
            pick_index=global_pick_index,
            pack_number=row.pack_number,
            pick_number=row.pick_number,
            global_pick_index=global_pick_index,
            estimated_remaining_picks=max(0, 42 - global_pick_index),
        )
        score_by_grp_id = {
            card.card.grp_id: float(card.raw_score) for card in scored.cards
        }
        if set(score_by_grp_id) != set(row.offered_grp_ids):
            raise AugmentedTrainingError(
                "Basic DO scoring did not return every offered candidate."
            )
        for candidate_id, grp_id in zip(
            row.candidate_ids,
            row.offered_grp_ids,
            strict=True,
        ):
            scores[(row.draft_index, row.pick_index, candidate_id)] = (
                score_by_grp_id[grp_id]
            )
    return scores


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
) -> np.ndarray:
    scores = np.full(
        (len(rows), len(data.card_names)),
        -np.inf,
        dtype=np.float32,
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
        score_by_id = {
            card.card.grp_id: float(card.raw_score) for card in scored.cards
        }
        scores[position, offered_indices] = [
            score_by_id[int(data.grp_ids[index])] for index in offered_indices
        ]
    return scores


def _score_basic_chunk(
    task: tuple[int, np.ndarray],
) -> tuple[int, np.ndarray]:
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
) -> np.ndarray:
    global _BASIC_SCORE_WORKER_STATE

    engine = PickEngine(
        set_profile=set_profile,
        enhanced_relationships_enabled=False,
    )
    scores = np.empty((len(rows), len(data.card_names)), dtype=np.float32)
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
            scores[offset : offset + len(selected)] = _score_basic_rows(
                data=data,
                rows=selected,
                engine=engine,
                card_database=card_database,
            )
        return scores
    _BASIC_SCORE_WORKER_STATE = (data, engine, card_database)
    completed = 0
    started = time.monotonic()
    context = multiprocessing.get_context("fork")
    with context.Pool(processes=worker_count) as pool:
        for offset, chunk_scores in pool.imap_unordered(
            _score_basic_chunk,
            tasks,
        ):
            scores[offset : offset + len(chunk_scores)] = chunk_scores
            completed += len(chunk_scores)
            print(
                f"Basic DO evaluated {completed:,}/{len(rows):,} picks in "
                f"{time.monotonic() - started:.1f} seconds",
                flush=True,
            )
    _BASIC_SCORE_WORKER_STATE = None
    return scores


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
    basic_scores: np.ndarray,
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
        scores = basic_scores + np.clip(
            centered * multiplier,
            -config.maximum_delta,
            config.maximum_delta,
        )
        ranks = _array_ranks(
            scores=scores,
            pack_mask=mask,
            targets=data.targets[rows],
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
    basic_scores: np.ndarray,
    calibration: dict[str, object],
    config: ModelCTrainingConfig,
) -> dict[str, dict[str, float | int]]:
    mask = data.pack_mask[rows]
    basic_ranks = _array_ranks(
        scores=basic_scores,
        pack_mask=mask,
        targets=data.targets[rows],
    )
    logits = _array_logits(
        model=model,
        features=data.features[rows],
        batch_size=config.batch_size,
    )
    centered = _centered_array_deltas(logits=logits, pack_mask=mask)
    augmented_scores = basic_scores + np.clip(
        centered * float(calibration["multiplier"]),
        float(calibration["minimum_delta"]),
        float(calibration["maximum_delta"]),
    )
    augmented_ranks = _array_ranks(
        scores=augmented_scores,
        pack_mask=mask,
        targets=data.targets[rows],
    )
    return {
        "basic_do": _rank_metrics(ranks=basic_ranks),
        "basic_plus_augmented": _rank_metrics(ranks=augmented_ranks),
    }


def train_and_gate_hob_model_c_arrays(
    *,
    data: _ArrayTrainingData,
    source: AugmentedTrainingSource,
    card_database: CardDatabase,
    set_profile: SetProfile,
    artifact_path: Path,
    report_path: Path,
    config: ModelCTrainingConfig = ModelCTrainingConfig(),
) -> HobModelCTrainingResult:
    """Train and gate Model C with compact arrays from the full HOB dump.
    The artifact is written only when both held-out metrics improve.
    """

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
    source_value = {
        "event_type": source.event_type,
        "url": source.url,
        "retrieved_at": source.retrieved_at,
        "sha256": source.sha256,
        "attribution": source.attribution,
        "license": source.license,
    }
    artifact: dict[str, object] = {
        "schema_version": 1,
        "set_code": "HOB",
        "model": {
            "name": "coarse_context_model_c",
            "architecture": "bias_plus_tanh_low_rank_pool_context",
            "feature_schema": list(FEATURE_NAMES),
            "candidate_output_ids": list(data.candidate_ids),
            "runtime_parameters": {
                "input_transform": "log1p_counts",
                "hidden_activation": "tanh",
                "bias": model.bias.tolist(),
                "input_weights": model.input_weights.tolist(),
                "output_weights": model.output_weights.tolist(),
            },
        },
        "calibration": calibration,
        "source": source_value,
        "training": {
            "seed": config.seed,
            "hidden_size": config.hidden_size,
            "epochs": config.epochs,
            "patience": config.patience,
            "batch_size": config.batch_size,
            "learning_rate": config.learning_rate,
            "weight_decay": config.weight_decay,
            "best_epoch": training["best_epoch"],
            "best_validation_mrr": training["best_validation_mrr"],
        },
        "evaluation": evaluation,
    }
    artifact_bytes = _json_bytes(value=artifact)
    report: dict[str, object] = {
        "schema_version": 1,
        "set_code": "HOB",
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
        "artifact_sha256": (
            hashlib.sha256(artifact_bytes).hexdigest() if promoted else None
        ),
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if promoted:
        artifact_path.write_bytes(artifact_bytes)
    elif artifact_path.exists():
        artifact_path.unlink()
    report_path.write_bytes(_json_bytes(value=report))
    return HobModelCTrainingResult(promoted=promoted, report=report)


def train_and_gate_hob_model_c(
    *,
    prepared: PreparedAugmentedTrainingData,
    basic_scores: BasicScores,
    artifact_path: Path,
    report_path: Path,
    config: ModelCTrainingConfig = ModelCTrainingConfig(),
) -> HobModelCTrainingResult:
    """Train Model C and write an artifact only after held-out promotion.
    This row-based entry point supports deterministic fixture coverage.
    """

    _validate_inputs(prepared=prepared, config=config)
    if artifact_path.resolve() == report_path.resolve():
        raise AugmentedTrainingError("Artifact and report paths must be different.")
    candidate_ids = tuple(sorted({row.candidate_id for row in prepared.iter_rows()}))
    candidate_indices = {value: index for index, value in enumerate(candidate_ids)}
    examples = _examples(
        prepared=prepared,
        candidate_indices=candidate_indices,
        basic_scores=basic_scores,
    )
    train = tuple(value for value in examples if value.partition == "train")
    validation = tuple(value for value in examples if value.partition == "validation")
    test = tuple(value for value in examples if value.partition == "test")
    if not train or not validation or not test:
        raise AugmentedTrainingError("Training requires non-empty pick partitions.")

    model, training = _train_model(
        examples=train,
        validation=validation,
        candidate_count=len(candidate_ids),
        config=config,
    )
    calibration = _calibrate(
        model=model,
        examples=validation,
        config=config,
    )
    evaluation = {
        "basic_do": _metrics(examples=test, model=None, calibration=None),
        "basic_plus_augmented": _metrics(
            examples=test,
            model=model,
            calibration=calibration,
        ),
    }
    promoted = all(
        evaluation["basic_plus_augmented"][metric]
        > evaluation["basic_do"][metric]
        for metric in ("top_1", "mean_reciprocal_rank")
    )
    artifact = _artifact(
        prepared=prepared,
        candidate_ids=candidate_ids,
        model=model,
        config=config,
        calibration=calibration,
        evaluation=evaluation,
        training=training,
    )
    artifact_bytes = _json_bytes(value=artifact)
    report: dict[str, object] = {
        "schema_version": 1,
        "set_code": "HOB",
        "model": "coarse_context_model_c",
        "promotion": {
            "passed": promoted,
            "rule": "both_metrics_strictly_improve",
        },
        "evaluation": evaluation,
        "calibration": calibration,
        "training": training,
        "feature_schema": list(FEATURE_NAMES),
        "source": artifact["source"],
        "artifact_sha256": (
            hashlib.sha256(artifact_bytes).hexdigest() if promoted else None
        ),
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if promoted:
        artifact_path.write_bytes(artifact_bytes)
    elif artifact_path.exists():
        artifact_path.unlink()
    report_path.write_bytes(_json_bytes(value=report))
    return HobModelCTrainingResult(promoted=promoted, report=report)


def _validate_inputs(
    *, prepared: PreparedAugmentedTrainingData, config: ModelCTrainingConfig
) -> None:
    if prepared.set_code != "HOB":
        raise AugmentedTrainingError("The pilot trainer accepts HOB data only.")
    if prepared.feature_names != FEATURE_NAMES or len(FEATURE_NAMES) != 22:
        raise AugmentedTrainingError("Model C requires the fixed 22-feature schema.")
    if config.seed < 0:
        raise AugmentedTrainingError("The training seed cannot be negative.")
    if min(config.hidden_size, config.epochs, config.patience, config.batch_size) <= 0:
        raise AugmentedTrainingError("Training counts must be positive.")
    if (
        not math.isfinite(config.learning_rate)
        or not math.isfinite(config.weight_decay)
        or config.learning_rate <= 0.0
        or config.weight_decay < 0.0
    ):
        raise AugmentedTrainingError("Optimizer settings are invalid.")
    if (
        not math.isfinite(config.maximum_delta)
        or config.maximum_delta <= 0.0
        or not config.calibration_multipliers
        or any(
            not math.isfinite(value) or value < 0.0
            for value in config.calibration_multipliers
        )
    ):
        raise AugmentedTrainingError("Calibration settings are invalid.")


def _examples(
    *,
    prepared: PreparedAugmentedTrainingData,
    candidate_indices: dict[str, int],
    basic_scores: BasicScores,
) -> tuple[_PickExample, ...]:
    grouped: dict[tuple[int, int], list[CandidateTrainingRow]] = {}
    for row in prepared.iter_rows():
        grouped.setdefault((row.draft_index, row.pick_index), []).append(row)
    examples: list[_PickExample] = []
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda value: value.candidate_id)
        chosen = [index for index, row in enumerate(rows) if row.chosen]
        if len(chosen) != 1:
            raise AugmentedTrainingError("Every pick must have one chosen candidate.")
        if any(row.features != rows[0].features for row in rows):
            raise AugmentedTrainingError(
                "Candidates in one pick have different features."
            )
        scores: list[float] = []
        for row in rows:
            score_key = (row.draft_index, row.pick_index, row.candidate_id)
            try:
                score = float(basic_scores[score_key])
            except KeyError as error:
                raise AugmentedTrainingError(
                    f"Basic DO score is missing for {score_key}."
                ) from error
            if not math.isfinite(score):
                raise AugmentedTrainingError("Basic DO scores must be finite.")
            scores.append(score)
        examples.append(
            _PickExample(
                partition=rows[0].partition,
                features=np.asarray(rows[0].features, dtype=np.float64),
                candidate_indices=np.asarray(
                    [candidate_indices[row.candidate_id] for row in rows],
                    dtype=np.int64,
                ),
                candidate_ids=tuple(row.candidate_id for row in rows),
                chosen_index=chosen[0],
                basic_scores=np.asarray(scores, dtype=np.float64),
            )
        )
    return tuple(examples)


def _train_model(
    *,
    examples: tuple[_PickExample, ...],
    validation: tuple[_PickExample, ...],
    candidate_count: int,
    config: ModelCTrainingConfig,
) -> tuple[_Model, dict[str, object]]:
    rng = np.random.default_rng(config.seed)
    model = _Model(
        bias=np.zeros(candidate_count, dtype=np.float64),
        input_weights=rng.normal(
            loc=0.0,
            scale=0.02,
            size=(len(FEATURE_NAMES), config.hidden_size),
        ),
        output_weights=rng.normal(
            loc=0.0,
            scale=0.02,
            size=(config.hidden_size, candidate_count),
        ),
    )
    parameters = (model.bias, model.input_weights, model.output_weights)
    first = [np.zeros_like(value) for value in parameters]
    second = [np.zeros_like(value) for value in parameters]
    best = model.copy()
    best_mrr = -math.inf
    best_epoch = 0
    stale = 0
    step = 0
    history: list[dict[str, float | int]] = []
    for epoch in range(1, config.epochs + 1):
        order = rng.permutation(len(examples))
        loss = 0.0
        for offset in range(0, len(order), config.batch_size):
            selected = [
                examples[index]
                for index in order[offset : offset + config.batch_size]
            ]
            gradients = [np.zeros_like(value) for value in parameters]
            for example in selected:
                loss += _accumulate_gradients(
                    model=model,
                    example=example,
                    gradients=gradients,
                )
            for index in (1, 2):
                gradients[index] += config.weight_decay * parameters[index]
            step += 1
            scale = 1.0 / len(selected)
            for index, parameter in enumerate(parameters):
                gradient = gradients[index] * scale
                first[index] *= 0.9
                first[index] += 0.1 * gradient
                second[index] *= 0.999
                second[index] += 0.001 * gradient * gradient
                first_hat = first[index] / (1.0 - 0.9**step)
                second_hat = second[index] / (1.0 - 0.999**step)
                parameter -= config.learning_rate * first_hat / (
                    np.sqrt(second_hat) + 1.0e-8
                )
        validation_mrr = _model_metrics(examples=validation, model=model)[1]
        history.append(
            {
                "epoch": epoch,
                "mean_loss": loss / len(examples),
                "validation_mrr": validation_mrr,
            }
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


def _accumulate_gradients(
    *, model: _Model, example: _PickExample, gradients: list[np.ndarray]
) -> float:
    transformed = np.log1p(example.features)
    hidden = np.tanh(transformed @ model.input_weights)
    candidates = example.candidate_indices
    logits = hidden @ model.output_weights[:, candidates] + model.bias[candidates]
    logits -= np.max(logits)
    probabilities = np.exp(logits)
    probabilities /= np.sum(probabilities)
    loss = -math.log(float(probabilities[example.chosen_index]) + 1.0e-12)
    probabilities[example.chosen_index] -= 1.0
    bias, inputs, outputs = gradients
    bias[candidates] += probabilities
    outputs[:, candidates] += np.outer(hidden, probabilities)
    hidden_gradient = model.output_weights[:, candidates] @ probabilities
    hidden_gradient *= 1.0 - hidden * hidden
    inputs += np.outer(transformed, hidden_gradient)
    return loss


def _calibrate(
    *,
    model: _Model,
    examples: tuple[_PickExample, ...],
    config: ModelCTrainingConfig,
) -> dict[str, object]:
    candidates: list[dict[str, float]] = []
    for multiplier in config.calibration_multipliers:
        top_1, mean_reciprocal_rank = _metrics_for_multiplier(
            examples=examples,
            model=model,
            multiplier=multiplier,
            maximum_delta=config.maximum_delta,
        )
        candidates.append(
            {
                "multiplier": multiplier,
                "top_1": top_1,
                "mean_reciprocal_rank": mean_reciprocal_rank,
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


def _metrics(
    *,
    examples: tuple[_PickExample, ...],
    model: _Model | None,
    calibration: dict[str, object] | None,
) -> dict[str, float | int]:
    if model is None:
        ranks = [
            _rank(example=example, scores=example.basic_scores)
            for example in examples
        ]
    else:
        assert calibration is not None
        ranks = [
            _rank(
                example=example,
                scores=example.basic_scores
                + _deltas(
                    model=model,
                    example=example,
                    multiplier=float(calibration["multiplier"]),
                    maximum_delta=float(calibration["maximum_delta"]),
                ),
            )
            for example in examples
        ]
    return {
        "picks": len(ranks),
        "top_1": sum(rank == 1 for rank in ranks) / len(ranks),
        "mean_reciprocal_rank": sum(1.0 / rank for rank in ranks) / len(ranks),
    }


def _model_metrics(
    *, examples: tuple[_PickExample, ...], model: _Model
) -> tuple[float, float]:
    ranks = [
        _rank(example=example, scores=_model_logits(model=model, example=example))
        for example in examples
    ]
    return (
        sum(rank == 1 for rank in ranks) / len(ranks),
        sum(1.0 / rank for rank in ranks) / len(ranks),
    )


def _metrics_for_multiplier(
    *,
    examples: tuple[_PickExample, ...],
    model: _Model,
    multiplier: float,
    maximum_delta: float,
) -> tuple[float, float]:
    ranks = [
        _rank(
            example=example,
            scores=example.basic_scores
            + _deltas(
                model=model,
                example=example,
                multiplier=multiplier,
                maximum_delta=maximum_delta,
            ),
        )
        for example in examples
    ]
    return (
        sum(rank == 1 for rank in ranks) / len(ranks),
        sum(1.0 / rank for rank in ranks) / len(ranks),
    )


def _model_logits(*, model: _Model, example: _PickExample) -> np.ndarray:
    hidden = np.tanh(np.log1p(example.features) @ model.input_weights)
    return hidden @ model.output_weights[:, example.candidate_indices] + model.bias[
        example.candidate_indices
    ]


def _deltas(
    *, model: _Model, example: _PickExample, multiplier: float, maximum_delta: float
) -> np.ndarray:
    logits = _model_logits(model=model, example=example)
    centered = logits - np.mean(logits)
    return np.clip(centered * multiplier, -maximum_delta, maximum_delta)


def _rank(*, example: _PickExample, scores: np.ndarray) -> int:
    order = sorted(
        range(len(scores)),
        key=lambda index: (-float(scores[index]), example.candidate_ids[index]),
    )
    return order.index(example.chosen_index) + 1


def _artifact(
    *,
    prepared: PreparedAugmentedTrainingData,
    candidate_ids: tuple[str, ...],
    model: _Model,
    config: ModelCTrainingConfig,
    calibration: dict[str, object],
    evaluation: dict[str, dict[str, float | int]],
    training: dict[str, object],
) -> dict[str, object]:
    report = prepared.report
    return {
        "schema_version": 1,
        "set_code": "HOB",
        "model": {
            "name": "coarse_context_model_c",
            "architecture": "bias_plus_tanh_low_rank_pool_context",
            "feature_schema": list(FEATURE_NAMES),
            "candidate_output_ids": list(candidate_ids),
            "runtime_parameters": {
                "input_transform": "log1p_counts",
                "hidden_activation": "tanh",
                "bias": model.bias.tolist(),
                "input_weights": model.input_weights.tolist(),
                "output_weights": model.output_weights.tolist(),
            },
        },
        "calibration": calibration,
        "source": {
            "event_type": report.event_type,
            "url": report.source_url,
            "retrieved_at": report.retrieved_at,
            "sha256": report.sha256,
            "attribution": report.attribution,
            "license": report.license,
        },
        "training": {
            "seed": config.seed,
            "hidden_size": config.hidden_size,
            "epochs": config.epochs,
            "patience": config.patience,
            "batch_size": config.batch_size,
            "learning_rate": config.learning_rate,
            "weight_decay": config.weight_decay,
            "best_epoch": training["best_epoch"],
            "best_validation_mrr": training["best_validation_mrr"],
        },
        "evaluation": evaluation,
    }


def _json_bytes(*, value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _arguments() -> argparse.Namespace:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Train and gate the HOB coarse-context Model C pilot.",
    )
    parser.add_argument("--draft-data", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--retrieved-at", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--card-data",
        type=Path,
        default=repository / "website/public/card-data/hob.json.gz",
    )
    parser.add_argument(
        "--set-profile",
        type=Path,
        default=(
            repository
            / "website/public/profiles/objects"
            / "51603f8922d594fca71bddd6d5fae02e301381180b169b155e402434c466fd23.json.gz"
        ),
    )
    return parser.parse_args()


def main() -> int:
    """Run the complete offline HOB training and promotion workflow.
    The exit status reports whether the strict promotion gate passed.
    """

    arguments = _arguments()
    card_database = load_hob_card_database(path=arguments.card_data)
    set_profile = load_hob_set_profile(path=arguments.set_profile)
    source = AugmentedTrainingSource(
        path=arguments.draft_data,
        url=arguments.source_url,
        sha256=arguments.source_sha256,
        retrieved_at=arguments.retrieved_at,
        attribution="17Lands public datasets",
        license="CC BY 4.0",
        event_type="PremierDraft",
    )
    data = _load_array_training_data(
        source=source,
        card_database=card_database,
    )
    result = train_and_gate_hob_model_c_arrays(
        data=data,
        source=source,
        card_database=card_database,
        set_profile=set_profile,
        artifact_path=arguments.artifact,
        report_path=arguments.report,
    )
    print(json.dumps(result.report, indent=2, sort_keys=True))
    return 0 if result.promoted else 1


if __name__ == "__main__":
    raise SystemExit(main())
