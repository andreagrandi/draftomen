"""Compare Card Pairs, Player Picks and Winner Picks with Pool Shape (Model C).
Every candidate is scored on Model C's chronological held-out split.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import gzip
import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from draftomen import augmented_training
from draftomen.augmented_card_pairs import (
    BOOTSTRAP_SAMPLES,
    bootstrap_counts as _bootstrap_counts,
    colours_and_rank,
    csv_header as _csv_header,
    deciles as _deciles,
    deck_measures,
    game_card_names,
    pair_table as _pair_table,
    read_decks as _read_decks,
    split_gap as _split_gap,
)
from draftomen.augmented_publication import (
    _dump_card_names,
    _load_published_profile,
    _with_bonus_sheet_cards,
)
from draftomen.augmented_training import (
    ModelCTrainingConfig,
    _array_logits,
    _array_ranks,
    _ArrayTrainingData,
    _BasicArrayScores,
    _build_array_basic_scores,
    _centered_array_deltas,
    _decompress_training_source,
    _load_array_training_data,
    _runtime_array_ranks,
    _train_array_model,
)
from draftomen.augmented_training_data import AugmentedTrainingSource
from draftomen.carddb import CardDatabase
from draftomen.set_card_data import SetCardData
from draftomen.test_draft import DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE

REPOSITORY = Path(__file__).resolve().parents[1]
EVENT_TYPE = "PremierDraft"
PICKS_PER_DRAFT = 42
HIGH_WINS = 7
RANK_ORDER = ("bronze", "silver", "gold", "platinum", "diamond", "mythic", "unknown")
MODEL_C = "Pool Shape on Basic DO"


@dataclass(frozen=True, slots=True)
class DraftOutcomes:
    """Hold each accepted draft's record and rank, indexed by draft_index."""

    wins: np.ndarray
    losses: np.ndarray
    ranks: np.ndarray
    validation_start: object
    held_out_ids: pl.Series


@dataclass(frozen=True, slots=True)
class Candidate:
    """Store one candidate's test ranks and optional calibration."""

    name: str
    test_ranks: np.ndarray
    multiplier: float | None = None


def _sha256(*, path: Path) -> str:
    digest = hashlib.sha256()
    with path.open(mode="rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pack_ordered_dump(*, path: Path, work_dir: Path) -> Path:
    """Return a draft dump whose pool columns follow its pack column order.
    Model C's loader needs one order, and the WOE dump lists pool columns differently.
    """

    header = _csv_header(path=path)
    pack = [column for column in header if column.startswith("pack_card_")]
    pool = [f"pool_{column.removeprefix('pack_card_')}" for column in pack]
    if [column for column in header if column.startswith("pool_")] == pool:
        return path
    if sorted(pool) != sorted(column for column in header if column.startswith("pool_")):
        raise SystemExit("Draft dump pack and pool columns name different cards.")
    source = path
    if path.suffix == ".gz":
        source = work_dir / path.with_suffix("").name
        print(f"Decompressing {path.name} to reorder its pool columns", flush=True)
        with gzip.open(path, mode="rb") as input_file, source.open(mode="wb") as output:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
    ordered = work_dir / f"{source.stem}.pack-ordered.csv"
    print(f"Writing {ordered.name} with pool columns in pack order", flush=True)
    metadata = [column for column in header if column not in pack and column not in pool]
    pl.scan_csv(source, infer_schema=False).select(metadata + pack + pool).sink_csv(
        ordered
    )
    if source != path:
        source.unlink()
    return ordered


def _card_database(*, set_code: str, draft_path: Path) -> CardDatabase:
    card_data_dir = REPOSITORY / "website/public/card-data"
    card_data = SetCardData.from_gzip_bytes(
        (card_data_dir / f"{set_code.lower()}.json.gz").read_bytes(),
        expected_set_code=set_code.lower(),
    )
    return _with_bonus_sheet_cards(
        card_database=card_data.to_card_database(),
        card_names=_dump_card_names(path=draft_path),
        bulk_file=REPOSITORY / DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE,
    )


def _draft_outcomes(*, csv_path: Path, data: _ArrayTrainingData) -> DraftOutcomes:
    """Match accepted drafts to their dump rows by their 42-pick sequence.
    The shared loader drops draft IDs, ranks and records, so they are re-read here.
    """

    frame = pl.read_csv(
        csv_path,
        columns=[
            "draft_id",
            "draft_time",
            "rank",
            "event_match_wins",
            "event_match_losses",
            "pack_number",
            "pick_number",
            "pick",
        ],
        schema_overrides={
            "rank": pl.Utf8,
            "event_match_wins": pl.Int32,
            "event_match_losses": pl.Int32,
            "pack_number": pl.Int32,
            "pick_number": pl.Int32,
        },
        low_memory=False,
    )
    drafts = (
        frame.sort(["draft_id", "pack_number", "pick_number"])
        .group_by("draft_id", maintain_order=True)
        .agg(
            pl.len().alias("picks"),
            pl.col("pick").str.join("|").alias("signature"),
            pl.col("draft_time").first(),
            pl.col("rank").first(),
            pl.col("event_match_wins").first().alias("wins"),
            pl.col("event_match_losses").first().alias("losses"),
        )
        .filter(pl.col("picks") == PICKS_PER_DRAFT)
        # A few WOE drafts are logged twice under different IDs with the same
        # picks. Those match in draft-time order, the order draft indices use.
        .with_columns(pl.col("draft_time").str.to_datetime().alias("time"))
        .sort(["time", "draft_id"])
        .with_columns(pl.int_range(pl.len()).over("signature").alias("occurrence"))
    )
    targets = data.targets.reshape(-1, PICKS_PER_DRAFT)
    if not np.array_equal(
        data.draft_indices.reshape(-1, PICKS_PER_DRAFT)[:, 0],
        np.arange(len(targets)),
    ):
        raise SystemExit("Accepted drafts are not stored as contiguous 42-pick rows.")
    names = np.asarray(data.card_names, dtype=object)
    accepted = pl.DataFrame(
        {
            "draft_index": np.arange(len(targets)),
            "signature": ["|".join(names[row]) for row in targets],
        }
    ).with_columns(pl.int_range(pl.len()).over("signature").alias("occurrence"))
    matched = accepted.join(
        drafts, on=["signature", "occurrence"], how="left"
    ).sort("draft_index")
    if (
        len(matched) != len(targets)
        or matched["draft_id"].null_count()
        or not matched["time"].is_sorted()
    ):
        raise SystemExit("Accepted drafts could not be matched one-to-one to draft IDs.")
    validation = matched.filter(
        pl.col("draft_index").is_in(data.split_drafts["validation"].tolist())
    )
    held_out = np.concatenate(
        (data.split_drafts["validation"], data.split_drafts["test"])
    )
    return DraftOutcomes(
        wins=matched["wins"].to_numpy().astype(np.float64),
        losses=matched["losses"].to_numpy().astype(np.float64),
        ranks=np.asarray(
            [
                value.strip().lower() if value and value.strip() else "unknown"
                for value in matched["rank"].to_list()
            ],
            dtype=object,
        ),
        validation_start=validation["draft_time"].str.to_datetime().min(),
        held_out_ids=matched["draft_id"].gather(held_out),
    )


def _pair_scores(
    *,
    game_path: Path,
    card_names: tuple[str, ...],
    outcomes: DraftOutcomes,
) -> tuple[np.ndarray, int]:
    """Return each card pair's shrunk win-rate interaction in percentage points.
    Only games drafted before the validation period feed the table.
    """

    games, in_deck = _read_decks(game_path=game_path, card_names=card_names)
    keep = (
        games.select(
            (pl.col("draft_time") < outcomes.validation_start)
            & ~pl.col("draft_id").is_in(outcomes.held_out_ids.to_list())
        )
        .to_series()
        .to_numpy()
    )
    if not keep.any():
        raise SystemExit("Game data has no games before the validation period.")
    won = games["won"].cast(pl.Float32).to_numpy()
    return _pair_table(in_deck=in_deck[keep], won=won[keep]), int(keep.sum())


def _games_report(*, set_code: str, game_path: Path) -> str:
    """Test whether better card pairs win more among decks of equal card strength.
    Pair and card tables come from the earliest 70% of drafts; the rest are tested.
    """

    card_names = game_card_names(path=game_path)
    games, in_deck = _read_decks(game_path=game_path, card_names=card_names)
    won = games["won"].cast(pl.Float32).to_numpy()
    times = games["draft_time"]
    cutoff = times.sort()[int(len(times) * 0.70)]
    train = (times < cutoff).to_numpy()
    held = ~train
    _, strength, pairs = deck_measures(
        in_deck=in_deck, won=won, train=train, held=held
    )
    held_games = games.filter(pl.Series(held))
    _, draft_codes = np.unique(held_games["draft_id"].to_numpy(), return_inverse=True)
    counts = _bootstrap_counts(draft_count=int(draft_codes.max()) + 1)
    held_won = won[held].astype(np.float64)
    strength_deciles = _deciles(values=strength)
    colour_codes = colours_and_rank(games=held_games)
    pair_gap = _split_gap(
        strata=strength_deciles,
        split_by=pairs,
        won=held_won,
        draft_codes=draft_codes,
        counts=counts,
    )
    pair_gap_within_colours = _split_gap(
        strata=colour_codes * 10 + strength_deciles,
        split_by=pairs,
        won=held_won,
        draft_codes=draft_codes,
        counts=counts,
    )
    strength_gap = _split_gap(
        strata=_deciles(values=pairs),
        split_by=strength,
        won=held_won,
        draft_codes=draft_codes,
        counts=counts,
    )

    def points(values: tuple[float, float, float]) -> str:
        point, low, top = (value * 100.0 for value in values)
        return f"{point:+.2f} pp [{low:+.2f}, {top:+.2f}]"

    return "\n".join(
        [
            f"# {set_code} {EVENT_TYPE}: do better card pairs win more games?",
            "",
            (
                f"Pair and card win rates come from {int(train.sum()):,} games "
                f"drafted before {cutoff}. The test uses the other "
                f"{int(held.sum()):,} games from {int(draft_codes.max()) + 1:,} "
                f"drafts. Intervals resample those drafts {BOOTSTRAP_SAMPLES} times."
            ),
            "",
            "| Comparison | Win-rate gap [95%] |",
            "| --- | --- |",
            (
                "| Better pairs against worse pairs, among decks of equal card "
                f"strength | {points(pair_gap)} |"
            ),
            (
                "| Better pairs against worse pairs, among decks of equal card "
                "strength in the same main colours and player rank "
                f"| {points(pair_gap_within_colours)} |"
            ),
            (
                "| Stronger cards against weaker cards, among decks of equal pair "
                f"score, for reference | {points(strength_gap)} |"
            ),
            "",
            (
                "Card strength is the deck's average card win rate above the "
                "overall rate. Pair score is the deck's average Card Pairs "
                "interaction. Each comparison splits decks at the median within "
                "deciles of the other measure."
            ),
        ]
    )


@contextlib.contextmanager
def _card_identity_inputs(*, card_names: tuple[str, ...]) -> Iterator[None]:
    """Size the shared trainer's input layer for per-card pool counts.
    The Model C trainer reads its input width from its FEATURE_NAMES global.
    """

    original = augmented_training.FEATURE_NAMES
    augmented_training.FEATURE_NAMES = card_names
    try:
        yield
    finally:
        augmented_training.FEATURE_NAMES = original


def _train_imitation(
    *,
    data: _ArrayTrainingData,
    config: ModelCTrainingConfig,
    draft_filter: np.ndarray | None,
) -> augmented_training._Model:
    split_rows = dict(data.split_rows)
    if draft_filter is not None:
        for name in ("train", "validation"):
            rows = split_rows[name]
            split_rows[name] = rows[draft_filter[data.draft_indices[rows]]]
    imitation_data = dataclasses.replace(
        data,
        features=data.pool_counts,
        split_rows=split_rows,
    )
    with _card_identity_inputs(card_names=data.card_names):
        model, _ = _train_array_model(data=imitation_data, config=config)
    return model


def _standalone(
    *, name: str, data: _ArrayTrainingData, scores: dict[str, np.ndarray]
) -> Candidate:
    rows = data.split_rows["test"]
    return Candidate(
        name=name,
        test_ranks=_array_ranks(
            scores=scores["test"],
            pack_mask=data.pack_mask[rows],
            targets=data.targets[rows],
        ),
    )


def _with_basic(
    *,
    name: str,
    data: _ArrayTrainingData,
    scores: dict[str, np.ndarray],
    basic: dict[str, _BasicArrayScores],
    config: ModelCTrainingConfig,
) -> Candidate:
    """Add a bounded delta to Basic DO, calibrated as Model C calibrates.
    The multiplier with the best validation MRR, then top-1, is kept.
    """

    def ranks(*, split: str, multiplier: float) -> np.ndarray:
        rows = data.split_rows[split]
        mask = data.pack_mask[rows]
        centered = _centered_array_deltas(logits=scores[split], pack_mask=mask)
        return _runtime_array_ranks(
            basic_scores=basic[split],
            pack_mask=mask,
            targets=data.targets[rows],
            deltas=np.clip(
                centered * multiplier,
                -config.maximum_delta,
                config.maximum_delta,
            ),
        )

    def selection_key(multiplier: float) -> tuple[float, float, float]:
        values = ranks(split="validation", multiplier=multiplier)
        return (
            float(np.mean(1.0 / values)),
            float(np.mean(values == 1)),
            -multiplier,
        )

    multiplier = max(config.calibration_multipliers, key=selection_key)
    return Candidate(
        name=name,
        test_ranks=ranks(split="test", multiplier=multiplier),
        multiplier=multiplier,
    )


def _interval(*, point: float, samples: np.ndarray) -> str:
    low, high = np.percentile(samples, [2.5, 97.5])
    return f"{point:.4f} [{low:.4f}, {high:.4f}]"


def _metric_table(
    *,
    candidates: list[Candidate],
    selected: np.ndarray,
    counts: np.ndarray,
) -> list[str]:
    lines = [
        "| Candidate | Top-1 [95%] | MRR [95%] | MRR minus Pool Shape [95%] |",
        "| --- | --- | --- | --- |",
    ]
    per_draft = {
        candidate.name: (
            np.mean(candidate.test_ranks.reshape(-1, PICKS_PER_DRAFT) == 1, axis=1)[
                selected
            ],
            np.mean(
                1.0 / candidate.test_ranks.reshape(-1, PICKS_PER_DRAFT), axis=1
            )[selected],
        )
        for candidate in candidates
    }
    totals = counts.sum(axis=1)
    model_c_rr = per_draft[MODEL_C][1]
    for candidate in candidates:
        top_1, reciprocal = per_draft[candidate.name]
        difference = reciprocal - model_c_rr
        lines.append(
            f"| {candidate.name} "
            f"| {_interval(point=top_1.mean(), samples=counts @ top_1 / totals)} "
            f"| {_interval(point=reciprocal.mean(), samples=counts @ reciprocal / totals)} "
            f"| {_interval(point=difference.mean(), samples=counts @ difference / totals)} |"
        )
    return lines


def _rank_adjusted_gap(
    *,
    followed: np.ndarray,
    outcomes: DraftOutcomes,
    test_drafts: np.ndarray,
    weights: np.ndarray,
) -> np.ndarray:
    """Return the win-rate gap averaged over ranks for each weight row.
    Each rank's gap is weighted by n1 * n0 / (n1 + n0) of its two groups.
    """

    wins = outcomes.wins[test_drafts]
    matches = wins + outcomes.losses[test_drafts]
    ranks = outcomes.ranks[test_drafts]
    gap_sum = np.zeros(len(weights))
    weight_sum = np.zeros(len(weights))
    for rank in RANK_ORDER:
        groups = []
        for group in (followed, ~followed):
            mask = ((ranks == rank) & group).astype(np.float64)
            groups.append(
                (weights @ mask, weights @ (mask * wins), weights @ (mask * matches))
            )
        (n1, w1, m1), (n0, w0, m0) = groups
        valid = (m1 > 0) & (m0 > 0)
        gap = np.where(valid, w1 / np.maximum(m1, 1) - w0 / np.maximum(m0, 1), 0.0)
        weight = np.where(valid, n1 * n0 / np.maximum(n1 + n0, 1), 0.0)
        gap_sum += gap * weight
        weight_sum += weight
    return gap_sum / np.maximum(weight_sum, 1e-12)


def _outcome_section(
    *,
    candidate: Candidate,
    outcomes: DraftOutcomes,
    test_drafts: np.ndarray,
    counts: np.ndarray,
) -> list[str]:
    agreement = np.mean(
        candidate.test_ranks.reshape(-1, PICKS_PER_DRAFT) == 1, axis=1
    )
    median = float(np.median(agreement))
    lines = [f"### {candidate.name}", ""]
    # A candidate that most drafts follow leaves a tiny comparison group at
    # the more-than-half split, so a median split is reported as well.
    for label, threshold in (
        ("more than half of their picks", 0.5),
        (f"more than the median share of their picks ({median:.1%})", median),
    ):
        lines.extend(
            _outcome_split(
                label=label,
                followed=agreement > threshold,
                outcomes=outcomes,
                test_drafts=test_drafts,
                counts=counts,
            )
        )
    return lines


def _outcome_split(
    *,
    label: str,
    followed: np.ndarray,
    outcomes: DraftOutcomes,
    test_drafts: np.ndarray,
    counts: np.ndarray,
) -> list[str]:
    wins = outcomes.wins[test_drafts]
    matches = wins + outcomes.losses[test_drafts]
    ranks = outcomes.ranks[test_drafts]
    lines = [
        (
            f"{followed.mean():.1%} of held-out drafts took this candidate's "
            f"first choice on {label}."
        ),
        "",
        (
            "| Rank | Followed drafts | Followed win rate | Other drafts "
            "| Other win rate | Gap |"
        ),
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for rank in RANK_ORDER:
        rates = []
        for group in (followed, ~followed):
            mask = (ranks == rank) & group
            played = matches[mask].sum()
            rates.append((int(mask.sum()), wins[mask].sum() / played if played else None))
        (n1, r1), (n0, r0) = rates
        if n1 + n0 == 0:
            continue
        gap = f"{(r1 - r0) * 100:+.1f} pp" if r1 is not None and r0 is not None else "n/a"
        lines.append(
            f"| {rank} | {n1} | {'n/a' if r1 is None else f'{r1:.1%}'} "
            f"| {n0} | {'n/a' if r0 is None else f'{r0:.1%}'} | {gap} |"
        )
    point = _rank_adjusted_gap(
        followed=followed,
        outcomes=outcomes,
        test_drafts=test_drafts,
        weights=np.ones((1, len(test_drafts))),
    )[0]
    samples = _rank_adjusted_gap(
        followed=followed,
        outcomes=outcomes,
        test_drafts=test_drafts,
        weights=counts,
    )
    low, high = np.percentile(samples, [2.5, 97.5])
    lines.extend(
        [
            "",
            (
                f"Rank-adjusted gap: {point * 100:+.2f} pp "
                f"[{low * 100:+.2f}, {high * 100:+.2f}]"
            ),
            "",
        ]
    )
    return lines


def _report(
    *,
    set_code: str,
    data: _ArrayTrainingData,
    outcomes: DraftOutcomes,
    candidates: list[Candidate],
    pair_games: int,
) -> str:
    test_drafts = data.split_drafts["test"]
    all_counts = _bootstrap_counts(draft_count=len(test_drafts))
    high = outcomes.wins[test_drafts] >= HIGH_WINS
    high_counts = _bootstrap_counts(draft_count=int(high.sum()))
    lines = [
        f"# {set_code} {EVENT_TYPE}: candidate rankers against Pool Shape (Model C)",
        "",
        "Drafts per split: "
        + ", ".join(
            f"{name} {len(data.split_drafts[name]):,}"
            for name in ("train", "validation", "test")
        )
        + f". Pair table games: {pair_games:,}, all drafted before "
        f"{outcomes.validation_start}. Intervals resample held-out drafts "
        f"{BOOTSTRAP_SAMPLES} times.",
        "",
        "Calibrated multipliers: "
        + "; ".join(
            f"{candidate.name}: {candidate.multiplier:g}"
            for candidate in candidates
            if candidate.multiplier is not None
        )
        + ".",
        "",
        f"## All held-out drafts ({len(test_drafts):,})",
        "",
        *_metric_table(
            candidates=candidates,
            selected=np.ones(len(test_drafts), dtype=bool),
            counts=all_counts,
        ),
        "",
        f"## Held-out drafts with {HIGH_WINS} or more wins ({int(high.sum()):,})",
        "",
        *_metric_table(candidates=candidates, selected=high, counts=high_counts),
        "",
        "## Win rate of drafts that followed each candidate",
        "",
        (
            "Win rate is match wins over matches played. The rank-adjusted gap "
            "averages each rank's gap, weighted by n1 * n0 / (n1 + n0)."
        ),
        "",
    ]
    for candidate in candidates:
        lines.extend(
            _outcome_section(
                candidate=candidate,
                outcomes=outcomes,
                test_drafts=test_drafts,
                counts=all_counts,
            )
        )
    return "\n".join(lines)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare candidate rankers with Pool Shape (Model C).",
    )
    parser.add_argument("--set", dest="set_code", required=True)
    parser.add_argument("--draft-data", type=Path)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--games-only",
        action="store_true",
        help="Only test whether better card pairs win more held-out games.",
    )
    arguments = parser.parse_args()
    if arguments.draft_data is None and not arguments.games_only:
        parser.error("--draft-data is required unless --games-only is set.")
    for path in (arguments.draft_data, arguments.game_data):
        if path is not None and not path.is_file():
            parser.error(f"{path} does not exist.")
    return arguments


def main() -> int:
    """Train, score and report every candidate for one set."""

    arguments = _arguments()
    set_code = arguments.set_code.upper()
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    if arguments.games_only:
        report = _games_report(set_code=set_code, game_path=arguments.game_data)
        arguments.report.write_text(report + "\n", encoding="utf-8")
        print(report, flush=True)
        return 0
    config = ModelCTrainingConfig()
    set_profile, _ = _load_published_profile(
        set_code=set_code,
        event_format=EVENT_TYPE,
        profiles_dir=REPOSITORY / "website/public/profiles",
    )
    card_database = _card_database(set_code=set_code, draft_path=arguments.draft_data)
    draft_path = _pack_ordered_dump(
        path=arguments.draft_data,
        work_dir=arguments.report.parent,
    )
    print("Hashing the draft dump", flush=True)
    source = AugmentedTrainingSource(
        path=draft_path,
        url=arguments.draft_data.resolve().as_uri(),
        sha256=_sha256(path=draft_path),
        retrieved_at="local",
        attribution="17Lands public datasets",
        license="CC BY 4.0",
        event_type=EVENT_TYPE,
    )
    data = _load_array_training_data(
        set_code=set_code,
        source=source,
        card_database=card_database,
    )
    outcomes = _draft_outcomes(
        csv_path=_decompress_training_source(source=source),
        data=data,
    )
    evaluation_rows = {
        split: data.split_rows[split] for split in ("validation", "test")
    }

    def logits(
        *, model: augmented_training._Model, features: np.ndarray
    ) -> dict[str, np.ndarray]:
        return {
            split: _array_logits(
                model=model,
                features=features[rows],
                batch_size=config.batch_size,
            )
            for split, rows in evaluation_rows.items()
        }

    # Basic DO scoring forks worker processes, so it runs first, while the
    # parent holds only the loaded dump. On WOE, forking later ran out of memory.
    basic_all = _build_array_basic_scores(
        data=data,
        rows=np.concatenate(tuple(evaluation_rows.values())),
        card_database=card_database,
        set_profile=set_profile,
    )
    validation_count = len(evaluation_rows["validation"])
    basic = {
        "validation": basic_all[:validation_count],
        "test": basic_all[validation_count:],
    }
    test_rows = evaluation_rows["test"]
    candidates = [
        Candidate(
            name="Basic DO",
            test_ranks=_runtime_array_ranks(
                basic_scores=basic["test"],
                pack_mask=data.pack_mask[test_rows],
                targets=data.targets[test_rows],
            ),
        ),
    ]
    print("Training Model C", flush=True)
    model_c, _ = _train_array_model(data=data, config=config)
    candidates.append(
        _with_basic(
            name=MODEL_C,
            data=data,
            scores=logits(model=model_c, features=data.features),
            basic=basic,
            config=config,
        )
    )

    pair_games = 0

    def pair_scores() -> dict[str, np.ndarray]:
        nonlocal pair_games
        pair_table, pair_games = _pair_scores(
            game_path=arguments.game_data,
            card_names=data.card_names,
            outcomes=outcomes,
        )
        return {
            split: data.pool_counts[rows].astype(np.float32) @ pair_table
            for split, rows in evaluation_rows.items()
        }

    def imitation_scores(
        *, draft_filter: np.ndarray | None
    ) -> dict[str, np.ndarray]:
        model = _train_imitation(data=data, config=config, draft_filter=draft_filter)
        return logits(model=model, features=data.pool_counts)

    # Each candidate's score tables use gigabytes on WOE, so they are built
    # one at a time and dropped before the next.
    for name, build_scores in (
        ("Card Pairs", pair_scores),
        ("Player Picks", lambda: imitation_scores(draft_filter=None)),
        (
            "Winner Picks",
            lambda: imitation_scores(draft_filter=outcomes.wins >= HIGH_WINS),
        ),
    ):
        print(f"Scoring {name}", flush=True)
        scores = build_scores()
        candidates.append(
            _standalone(name=f"{name} alone", data=data, scores=scores)
        )
        candidates.append(
            _with_basic(
                name=f"{name} on Basic DO",
                data=data,
                scores=scores,
                basic=basic,
                config=config,
            )
        )
        del scores
    report = _report(
        set_code=set_code,
        data=data,
        outcomes=outcomes,
        candidates=candidates,
        pair_games=pair_games,
    )
    arguments.report.write_text(report + "\n", encoding="utf-8")
    print(report, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

