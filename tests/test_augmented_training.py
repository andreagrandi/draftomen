from __future__ import annotations

import csv
import gzip
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import draftomen.augmented_training as augmented_training
from draftomen.augmented_training import (
    AugmentedTrainingError,
    ModelCTrainingConfig,
    _load_array_training_data,
    train_and_gate_augmented_set,
)
from draftomen.augmented_artifact import AugmentedArtifact
from draftomen.augmented_training_data import (
    FEATURE_NAMES,
    AugmentedTrainingSource,
    prepare_augmented_training_data,
)
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.pickengine import PickEngine
from draftomen.set_profile import SetProfile


_CARD_A = "00000000-0000-0000-0000-000000000001"
_CARD_B = "00000000-0000-0000-0000-000000000002"
_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "17lands-public-drafts-hob-training.csv"
)
_CONFIG = ModelCTrainingConfig(
    seed=619,
    hidden_size=4,
    epochs=30,
    patience=5,
    batch_size=4,
    learning_rate=0.05,
    maximum_delta=2.0,
)


def _fixture_card_database(*, set_code: str = "HOB") -> CardDatabase:
    cards = (
        CardInfo(
            grp_id=1,
            name="Red Recruit",
            colors=("R",),
            mana_value=2.0,
            rarity="common",
            types=("Creature",),
            type_line="Creature — Human",
            set_code=set_code,
            oracle_id=_CARD_A,
        ),
        CardInfo(
            grp_id=2,
            name="Blue Trick",
            colors=("U",),
            mana_value=3.0,
            rarity="common",
            types=("Instant",),
            type_line="Instant",
            set_code=set_code,
            oracle_id=_CARD_B,
        ),
        CardInfo(
            grp_id=3,
            name="Clockwork Relic",
            colors=(),
            mana_value=1.0,
            rarity="common",
            types=("Artifact",),
            type_line="Artifact",
            set_code=set_code,
            oracle_id="00000000-0000-0000-0000-000000000003",
        ),
    )
    return CardDatabase(cards={card.grp_id: card for card in cards})


def _update_card(
    database: CardDatabase, *, card_name: str, **changes: Any
) -> CardDatabase:
    cards = dict(database.cards)
    grp_id = next(
        grp_id for grp_id, card in cards.items() if card.name == card_name
    )
    cards[grp_id] = replace(cards[grp_id], **changes)
    return CardDatabase(cards=cards)


def _source_for_path(
    path: Path, *, set_code: str, event_type: str = "PremierDraft"
) -> AugmentedTrainingSource:
    return AugmentedTrainingSource(
        path=path,
        url=f"https://example.test/passing-{set_code.casefold()}.csv",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        retrieved_at="2026-09-20T12:00:00+00:00",
        attribution="17Lands public datasets",
        license="CC BY 4.0",
        event_type=event_type,
    )


def _passing_compact_source(
    *,
    tmp_path: Path,
    set_code: str = "HOB",
    draft_count: int = 16,
    draft_times: tuple[str, ...] | None = None,
) -> AugmentedTrainingSource:
    path = tmp_path / f"passing-{set_code.casefold()}-{draft_count}.csv"
    fieldnames = (
        "expansion",
        "event_type",
        "draft_id",
        "draft_time",
        "rank",
        "event_match_wins",
        "event_match_losses",
        "pack_number",
        "pick_number",
        "pick",
        "pick_2",
        "pack_card_Red Recruit",
        "pack_card_Blue Trick",
        "pool_Red Recruit",
        "pool_Blue Trick",
    )
    if draft_times is None:
        draft_times = tuple(
            f"2026-09-{draft_index + 1:02}T10:00:00+00:00"
            for draft_index in range(draft_count)
        )
    assert len(draft_times) == draft_count
    with path.open(mode="w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for draft_index, draft_time in enumerate(draft_times):
            red_first = draft_index % 2 == 0
            first_pick = "Red Recruit" if red_first else "Blue Trick"
            second_pick = "Blue Trick" if red_first else "Red Recruit"
            shared = {
                "expansion": set_code,
                "event_type": "PremierDraft",
                "draft_id": f"passing-{draft_index:02}",
                "draft_time": draft_time,
                "rank": "gold",
                "event_match_wins": 3,
                "event_match_losses": 1,
                "pack_number": 0,
                "pick_2": "",
            }
            writer.writerow(
                {
                    **shared,
                    "pick_number": 0,
                    "pick": first_pick,
                    "pack_card_Red Recruit": int(red_first),
                    "pack_card_Blue Trick": int(not red_first),
                    "pool_Red Recruit": 0,
                    "pool_Blue Trick": 0,
                }
            )
            writer.writerow(
                {
                    **shared,
                    "pick_number": 1,
                    "pick": second_pick,
                    "pack_card_Red Recruit": 1,
                    "pack_card_Blue Trick": 1,
                    "pool_Red Recruit": int(red_first),
                    "pool_Blue Trick": int(not red_first),
                }
            )
    return _source_for_path(path, set_code=set_code)


def _public_dump_source() -> AugmentedTrainingSource:
    return _source_for_path(_FIXTURE_PATH, set_code="HOB")


def _copy_with_expansion(
    source: AugmentedTrainingSource, *, tmp_path: Path, set_code: str
) -> AugmentedTrainingSource:
    path = tmp_path / f"{Path(source.path).stem}-{set_code.casefold()}.csv"
    with (
        Path(source.path).open(encoding="utf-8", newline="") as input_file,
        path.open(mode="w", encoding="utf-8", newline="") as output_file,
    ):
        reader = csv.DictReader(input_file)
        assert reader.fieldnames is not None
        writer = csv.DictWriter(output_file, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerows({**row, "expansion": set_code} for row in reader)
    return _source_for_path(path, set_code=set_code)



def _assert_ordered_complete_splits(data: Any) -> None:
    names = ("train", "validation", "test")
    split_values = {
        name: tuple(int(value) for value in data.split_drafts[name]) for name in names
    }
    for values in split_values.values():
        assert values == tuple(sorted(values))
        assert len(values) == len(set(values))
    assert set(split_values["train"]).isdisjoint(split_values["validation"])
    assert set(split_values["train"]).isdisjoint(split_values["test"])
    assert set(split_values["validation"]).isdisjoint(split_values["test"])
    assert set.union(*(set(values) for values in split_values.values())) == set(
        int(value) for value in np.unique(data.draft_indices)
    )

    for name in names:
        rows = data.split_rows[name]
        assigned_drafts = set(split_values[name])
        assert set(int(value) for value in np.unique(data.draft_indices[rows])) == (
            assigned_drafts
        )
        for draft_index in assigned_drafts:
            draft_rows = rows[data.draft_indices[rows] == draft_index]
            assert data.global_picks[draft_rows].tolist() == [0, 1]


def _controlled_basic_scores(
    *,
    data: Any,
    rows: np.ndarray,
    card_database: CardDatabase,
    set_profile: SetProfile,
) -> np.ndarray:
    del card_database, set_profile
    scores = np.full(
        (len(rows), len(data.card_names)),
        -np.inf,
        dtype=np.float32,
    )
    for score_index, row_value in enumerate(rows):
        row = int(row_value)
        offered = np.flatnonzero(data.pack_mask[row])
        if len(offered) == 1:
            scores[score_index, offered[0]] = 0.0
            continue
        pool_colors = np.flatnonzero(data.pool_counts[row])
        assert len(pool_colors) == 1
        chosen = int(data.targets[row])
        assert int(pool_colors[0]) != chosen
        scores[score_index, pool_colors[0]] = 1.0
        scores[score_index, chosen] = 0.0
    return scores


def _rank_metrics(ranks: tuple[int, ...]) -> dict[str, float | int]:
    return {
        "top_1": sum(rank == 1 for rank in ranks) / len(ranks),
        "mean_reciprocal_rank": sum(1.0 / rank for rank in ranks) / len(ranks),
        "picks": len(ranks),
    }


def test_compact_loader_matches_hob_row_oracle_and_keeps_whole_drafts() -> None:
    source = _public_dump_source()
    database = _fixture_card_database()
    prepared = prepare_augmented_training_data(
        set_code="HOB",
        source=source,
        card_database=database,
        complete_draft_picks=2,
    )

    data = _load_array_training_data(
        set_code="HOB",
        source=source,
        card_database=database,
        complete_draft_picks=2,
    )

    assert data.rows_seen == prepared.report.rows_seen
    assert data.drafts_seen == prepared.report.drafts_seen
    assert len(data.targets) == prepared.report.drafts_accepted * 2
    assert tuple(
        len(data.split_drafts[name]) for name in ("train", "validation", "test")
    ) == (4, 1, 2)
    assert data.features.shape == (14, len(FEATURE_NAMES))
    prepared_features = {
        (row.draft_index, row.pick_index): row.features
        for row in prepared.iter_rows()
    }
    assert [tuple(value) for value in data.features] == [
        prepared_features[(draft_index, pick_index)]
        for draft_index in range(7)
        for pick_index in range(2)
    ]
    _assert_ordered_complete_splits(data)


def test_compact_loader_reads_gzip_cache_object_with_bin_suffix(
    tmp_path: Path,
) -> None:
    plain_source = _passing_compact_source(tmp_path=tmp_path)
    payload = gzip.compress(Path(plain_source.path).read_bytes(), mtime=0)
    digest = hashlib.sha256(payload).hexdigest()
    cache_object_path = (
        tmp_path / "profile-input-cache" / "objects" / f"{digest}.bin"
    )
    cache_object_path.parent.mkdir(parents=True)
    cache_object_path.write_bytes(payload)
    source = replace(plain_source, path=cache_object_path, sha256=digest)

    data = _load_array_training_data(
        set_code="HOB",
        source=source,
        card_database=_fixture_card_database(),
        complete_draft_picks=2,
    )

    assert data.rows_seen == 32
    assert data.drafts_seen == 16
    assert data.features.shape == (32, len(FEATURE_NAMES))


def test_compact_loader_rejects_corrupt_gzip_cache_object_without_partial(
    tmp_path: Path,
) -> None:
    plain_source = _passing_compact_source(tmp_path=tmp_path)
    payload = bytearray(
        gzip.compress(Path(plain_source.path).read_bytes(), mtime=0)
    )
    payload[-8] ^= 0xFF
    compressed = bytes(payload)
    digest = hashlib.sha256(compressed).hexdigest()
    cache_object_path = (
        tmp_path / "profile-input-cache" / "objects" / f"{digest}.bin"
    )
    cache_object_path.parent.mkdir(parents=True)
    cache_object_path.write_bytes(compressed)
    source = replace(plain_source, path=cache_object_path, sha256=digest)
    partial_path = Path("/tmp") / f"draftomen-augmented-{digest[:16]}.csv.part"

    with pytest.raises(AugmentedTrainingError, match="could not be decompressed"):
        _load_array_training_data(
            set_code="HOB",
            source=source,
            card_database=_fixture_card_database(),
            complete_draft_picks=2,
        )

    assert not partial_path.exists()


def test_synthetic_tst_fixture_matches_its_set_cards_and_source(
    tmp_path: Path,
) -> None:
    source = _copy_with_expansion(
        _public_dump_source(), tmp_path=tmp_path, set_code="TST"
    )
    database = _fixture_card_database(set_code="TST")
    profile = SetProfile.generic(set_code="TST", event_format="PremierDraft")
    prepared = prepare_augmented_training_data(
        set_code="TST",
        source=source,
        card_database=database,
        complete_draft_picks=2,
    )

    data = _load_array_training_data(
        set_code="TST",
        source=source,
        card_database=database,
        complete_draft_picks=2,
    )

    assert prepared.set_code == "TST"
    assert profile.set_code == "tst"
    assert data.candidate_ids == (
        _CARD_A,
        _CARD_B,
        "00000000-0000-0000-0000-000000000003",
    )
    assert source.event_type.casefold() == profile.event_format
    assert data.rows_seen == prepared.report.rows_seen
    assert tuple(
        len(data.split_drafts[name]) for name in ("train", "validation", "test")
    ) == (4, 1, 2)
    prepared_features = {
        (row.draft_index, row.pick_index): row.features
        for row in prepared.iter_rows()
    }
    assert [tuple(value) for value in data.features] == [
        prepared_features[(draft_index, pick_index)]
        for draft_index in range(7)
        for pick_index in range(2)
    ]
    _assert_ordered_complete_splits(data)


def test_compact_loader_sorts_offset_times_by_actual_time_at_partition_boundary(
    tmp_path: Path,
) -> None:
    times = [
        f"2026-09-{draft_index + 1:02}T10:00:00+00:00"
        for draft_index in range(16)
    ]
    times[10] = "2026-09-11T00:30:00+00:00"
    times[11] = "2026-09-10T21:00:00-04:00"
    assert times[11] < times[10]
    assert datetime.fromisoformat(times[10]).astimezone(UTC) < datetime.fromisoformat(
        times[11]
    ).astimezone(UTC)
    source = _passing_compact_source(tmp_path=tmp_path, draft_times=tuple(times))

    data = _load_array_training_data(
        set_code="HOB",
        source=source,
        card_database=_fixture_card_database(),
        complete_draft_picks=2,
    )

    assert tuple(
        len(data.split_drafts[name]) for name in ("train", "validation", "test")
    ) == (11, 2, 3)
    assert data.draft_indices[20:22].tolist() == [10, 10]
    assert data.draft_indices[22:24].tolist() == [11, 11]
    assert data.targets[[20, 22]].tolist() == [0, 1]
    _assert_ordered_complete_splits(data)


def test_compact_loader_rejects_invalid_draft_timestamp(tmp_path: Path) -> None:
    times = [
        f"2026-09-{draft_index + 1:02}T10:00:00+00:00"
        for draft_index in range(16)
    ]
    times[7] = "not-a-timestamp"
    source = _passing_compact_source(tmp_path=tmp_path, draft_times=tuple(times))

    with pytest.raises(AugmentedTrainingError):
        _load_array_training_data(
            set_code="HOB",
            source=source,
            card_database=_fixture_card_database(),
            complete_draft_picks=2,
        )


@pytest.mark.parametrize("card_set_code", ("TST", None))
def test_compact_loader_rejects_wrong_or_missing_set_on_referenced_cards(
    tmp_path: Path, card_set_code: str | None
) -> None:
    source = _passing_compact_source(tmp_path=tmp_path)
    database = _update_card(
        _fixture_card_database(), card_name="Blue Trick", set_code=card_set_code
    )

    with pytest.raises(AugmentedTrainingError):
        _load_array_training_data(
            set_code="HOB",
            source=source,
            card_database=database,
            complete_draft_picks=2,
        )


@pytest.mark.parametrize("oracle_id", (_CARD_A, "not-a-uuid", None))
def test_compact_loader_rejects_duplicate_or_invalid_oracle_ids(
    tmp_path: Path, oracle_id: str | None
) -> None:
    source = _passing_compact_source(tmp_path=tmp_path)
    database = _update_card(
        _fixture_card_database(), card_name="Blue Trick", oracle_id=oracle_id
    )

    with pytest.raises(AugmentedTrainingError):
        _load_array_training_data(
            set_code="HOB",
            source=source,
            card_database=database,
            complete_draft_picks=2,
        )


@pytest.mark.parametrize(
    ("set_code", "source_set_code"),
    (("HOB", "TST"), ("HOB", "HOB")),
)
def test_compact_loader_rejects_mismatched_csv_identity_or_source_metadata(
    tmp_path: Path, set_code: str, source_set_code: str
) -> None:
    source = _passing_compact_source(tmp_path=tmp_path, set_code=source_set_code)
    if source_set_code == set_code:
        source = replace(source, event_type="QuickDraft")

    with pytest.raises(AugmentedTrainingError):
        _load_array_training_data(
            set_code=set_code,
            source=source,
            card_database=_fixture_card_database(set_code=set_code),
            complete_draft_picks=2,
        )


def test_compact_loader_rejects_a_mismatched_source_checksum(tmp_path: Path) -> None:
    source = _passing_compact_source(tmp_path=tmp_path)
    source = replace(source, sha256="f" * 64)

    with pytest.raises(AugmentedTrainingError):
        _load_array_training_data(
            set_code="HOB",
            source=source,
            card_database=_fixture_card_database(),
            complete_draft_picks=2,
        )


def test_compact_loader_rejects_empty_training_partitions(tmp_path: Path) -> None:
    source = _passing_compact_source(tmp_path=tmp_path, draft_count=2)

    with pytest.raises(AugmentedTrainingError):
        _load_array_training_data(
            set_code="HOB",
            source=source,
            card_database=_fixture_card_database(),
            complete_draft_picks=2,
        )


def test_trainer_rejects_profile_set_and_event_mismatches_before_basic_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _passing_compact_source(tmp_path=tmp_path)
    database = _fixture_card_database()

    def unexpected_basic_scoring(**_kwargs: Any) -> np.ndarray:
        pytest.fail("Basic DO scoring must not run with an incompatible profile")

    monkeypatch.setattr(
        augmented_training, "_build_array_basic_scores", unexpected_basic_scoring
    )
    for profile in (
        SetProfile.generic(set_code="TST", event_format="PremierDraft"),
        SetProfile.generic(set_code="HOB", event_format="QuickDraft"),
    ):
        with pytest.raises(AugmentedTrainingError):
            train_and_gate_augmented_set(
                set_code="HOB",
                source=source,
                card_database=database,
                set_profile=profile,
                complete_draft_picks=2,
                config=_CONFIG,
            )


@pytest.mark.parametrize("set_code", ("HOB", "TST"))
def test_passing_set_trains_a_deterministic_validated_runtime_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    set_code: str,
) -> None:
    if set_code == "TST":
        source = _copy_with_expansion(
            _passing_compact_source(tmp_path=tmp_path),
            tmp_path=tmp_path,
            set_code=set_code,
        )
    else:
        source = _passing_compact_source(tmp_path=tmp_path, set_code=set_code)
    database = _fixture_card_database(set_code=set_code)
    profile = SetProfile.generic(set_code=set_code, event_format="PremierDraft")
    monkeypatch.setattr(
        augmented_training, "_build_array_basic_scores", _controlled_basic_scores
    )

    results = [
        train_and_gate_augmented_set(
            set_code=set_code,
            source=source,
            card_database=database,
            set_profile=profile,
            complete_draft_picks=2,
            config=_CONFIG,
        )
        for _ in range(2)
    ]

    assert all(result.artifact is not None for result in results)
    first = results[0]
    second = results[1]
    artifact = first.artifact
    repeated = second.artifact
    assert artifact is not None
    assert repeated is not None
    artifact_bytes = artifact.to_bytes()
    assert artifact_bytes == repeated.to_bytes()
    assert first.report == second.report
    assert artifact.set_code == set_code.casefold()
    assert artifact.candidate_ids == (_CARD_A, _CARD_B)
    assert AugmentedArtifact.from_bytes(
        artifact_bytes, expected_set_code=set_code.casefold()
    ) == artifact
    assert artifact.source.attribution == "17Lands public datasets"
    assert artifact.source.event_type == "PremierDraft"
    assert artifact.source.license == "CC BY 4.0"
    assert artifact.source.retrieved_at == "2026-09-20T12:00:00+00:00"
    assert artifact.source.sha256 == source.sha256
    assert artifact.source.url == source.url
    assert artifact.calibration.selection_partition == "validation"
    assert artifact.evaluation.picks == 6
    assert artifact.evaluation.basic_do.top_1 == 0.5
    assert artifact.evaluation.basic_do.mean_reciprocal_rank == 0.75
    assert artifact.evaluation.basic_plus_augmented.top_1 > (
        artifact.evaluation.basic_do.top_1
    )
    assert artifact.evaluation.basic_plus_augmented.mean_reciprocal_rank > (
        artifact.evaluation.basic_do.mean_reciprocal_rank
    )
    assert first.report["promotion"] == {
        "passed": True,
        "rule": "both_metrics_strictly_improve",
    }
    assert first.report["source"] == artifact.source.to_json()
    assert first.report["artifact_sha256"] == hashlib.sha256(
        artifact_bytes
    ).hexdigest()


def test_basic_array_scores_match_real_pick_engine_results() -> None:
    source = _public_dump_source()
    database = _fixture_card_database()
    profile = SetProfile.generic(set_code="HOB", event_format="PremierDraft")
    data = _load_array_training_data(
        set_code="HOB",
        source=source,
        card_database=database,
        complete_draft_picks=2,
    )
    rows = data.split_rows["test"]
    scores = augmented_training._build_array_basic_scores(
        data=data,
        rows=rows,
        card_database=database,
        set_profile=profile,
    )
    engine = PickEngine(set_profile=profile)

    assert scores.integer_scores.shape == (len(rows), len(data.card_names))
    for position, row_value in enumerate(rows):
        row = int(row_value)
        offered_indices = np.flatnonzero(data.pack_mask[row])
        pool_ids = tuple(
            int(data.grp_ids[index])
            for index in np.flatnonzero(data.pool_counts[row])
            for _ in range(int(data.pool_counts[row, index]))
        )
        global_pick_index = int(data.global_picks[row]) + 1
        scored = engine.score_pack(
            offered_grp_ids=tuple(
                int(data.grp_ids[index]) for index in offered_indices
            ),
            card_database=database,
            pool_grp_ids=pool_ids,
            pick_index=global_pick_index,
            pack_number=int(data.pack_numbers[row]),
            pick_number=int(data.pick_numbers[row]),
            global_pick_index=global_pick_index,
            estimated_remaining_picks=max(0, 42 - global_pick_index),
        )
        cards_by_id = {card.card.grp_id: card for card in scored.cards}
        expected_cards = [
            cards_by_id[int(data.grp_ids[index])] for index in offered_indices
        ]
        assert scores.integer_scores[position, offered_indices].tolist() == [
            card.basic_score for card in expected_cards
        ]
        ordered_positions = sorted(
            range(len(expected_cards)),
            key=lambda index: (
                -expected_cards[index].raw_score,
                -expected_cards[index].base_rating,
                expected_cards[index].original_index,
            ),
        )
        expected_tie_order = np.empty(len(expected_cards), dtype=np.uint16)
        expected_tie_order[ordered_positions] = np.arange(
            len(expected_cards), dtype=np.uint16
        )
        assert scores.tie_order[position, offered_indices].tolist() == (
            expected_tie_order.tolist()
        )
        unoffered_indices = np.flatnonzero(~data.pack_mask[row])
        assert np.all(
            scores.tie_order[position, unoffered_indices]
            == np.iinfo(np.uint16).max
        )




def test_calibration_and_held_out_ranks_match_rounded_runtime_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basic_scores = np.array([[50.49, 50.51]], dtype=np.float64)
    pack_mask = np.array([[True, True]])
    targets = np.array([0], dtype=np.int32)
    rows = np.array([0], dtype=np.int32)
    data = SimpleNamespace(
        features=np.zeros((1, 1), dtype=np.uint8),
        pack_mask=pack_mask,
        targets=targets,
    )
    logits = np.array([[0.01, -0.01]], dtype=np.float64)
    monkeypatch.setattr(
        augmented_training,
        "_array_logits",
        lambda **_kwargs: logits,
    )

    calibration = augmented_training._calibrate_arrays(
        model=object(),
        data=data,
        rows=rows,
        basic_scores=basic_scores,
        config=_CONFIG,
    )
    assert calibration["multiplier"] == 0.0
    assert all(
        candidate["top_1"] == 0.0
        and candidate["mean_reciprocal_rank"] == 0.5
        for candidate in calibration["candidates"]
    )

    evaluation = augmented_training._evaluate_arrays(
        model=object(),
        data=data,
        rows=rows,
        basic_scores=basic_scores,
        calibration={
            **calibration,
            "multiplier": 16.0,
        },
        config=_CONFIG,
    )
    assert evaluation["basic_do"] == {
        "picks": 1,
        "top_1": 0.0,
        "mean_reciprocal_rank": 0.5,
    }
    assert evaluation["basic_plus_augmented"] == evaluation["basic_do"]

    old_baseline_ranks = augmented_training._array_ranks(
        scores=basic_scores,
        pack_mask=pack_mask,
        targets=targets,
    )
    legacy_deltas = augmented_training._centered_array_deltas(
        logits=logits,
        pack_mask=pack_mask,
    ) * 16.0
    old_augmented_ranks = augmented_training._array_ranks(
        scores=basic_scores + legacy_deltas,
        pack_mask=pack_mask,
        targets=targets,
    )
    assert old_baseline_ranks.tolist() == [2]
    assert old_augmented_ranks.tolist() == [1]


def test_runtime_ties_keep_basic_do_tiebreak_order_after_deltas() -> None:
    basic_scores = augmented_training._BasicArrayScores(
        integer_scores=np.array([[50, 50, 50]], dtype=np.uint8),
        tie_order=np.array([[2, 1, 0]], dtype=np.uint16),
    )
    pack_mask = np.array([[True, True, True]])
    targets = np.array([1], dtype=np.int32)
    basic_ranks = augmented_training._runtime_array_ranks(
        basic_scores=basic_scores,
        pack_mask=pack_mask,
        targets=targets,
    )
    augmented_ranks = augmented_training._runtime_array_ranks(
        basic_scores=basic_scores,
        pack_mask=pack_mask,
        targets=targets,
        deltas=np.array([[0.25, 0.25, 0.25]]),
    )

    assert basic_ranks.tolist() == [2]
    assert augmented_ranks.tolist() == [2]


def test_one_metric_only_held_out_improvements_never_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _passing_compact_source(tmp_path=tmp_path)
    database = _fixture_card_database()
    profile = SetProfile.generic(set_code="HOB", event_format="PremierDraft")

    def neutral_basic_scores(
        *, data: Any, rows: np.ndarray, **_kwargs: Any
    ) -> np.ndarray:
        return np.zeros((len(rows), len(data.card_names)), dtype=np.float32)

    monkeypatch.setattr(
        augmented_training, "_build_array_basic_scores", neutral_basic_scores
    )
    rank_cases = (
        ((2, 2, 2, 2), (1, 3, 3, 3)),
        ((2, 3, 2, 3), (2, 2, 2, 2)),
    )
    for basic_ranks, augmented_ranks in rank_cases:
        evaluation = {
            "basic_do": _rank_metrics(basic_ranks),
            "basic_plus_augmented": _rank_metrics(augmented_ranks),
        }

        def injected_evaluation(**_kwargs: Any) -> dict[str, dict[str, float | int]]:
            return evaluation

        monkeypatch.setattr(
            augmented_training, "_evaluate_arrays", injected_evaluation
        )
        result = train_and_gate_augmented_set(
            set_code="HOB",
            source=source,
            card_database=database,
            set_profile=profile,
            complete_draft_picks=2,
            config=_CONFIG,
        )

        assert result.artifact is None
        assert result.report["promotion"] == {
            "passed": False,
            "rule": "both_metrics_strictly_improve",
        }
        assert result.report["evaluation"] == evaluation
        assert result.report["artifact_sha256"] is None

        if basic_ranks == (2, 2, 2, 2):
            assert evaluation["basic_plus_augmented"]["top_1"] > evaluation[
                "basic_do"
            ]["top_1"]
            assert evaluation["basic_plus_augmented"]["mean_reciprocal_rank"] == (
                evaluation["basic_do"]["mean_reciprocal_rank"]
            ) == 0.5
        else:
            assert evaluation["basic_plus_augmented"]["mean_reciprocal_rank"] > (
                evaluation["basic_do"]["mean_reciprocal_rank"]
            )
            assert evaluation["basic_plus_augmented"]["top_1"] == (
                evaluation["basic_do"]["top_1"]
            ) == 0.0
