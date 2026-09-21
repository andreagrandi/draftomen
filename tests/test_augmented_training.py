from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from draftomen.augmented_training_data import (
    FEATURE_NAMES,
    AugmentedTrainingSource,
    CandidateTrainingRow,
    PreparedAugmentedTrainingData,
    prepare_augmented_training_data,
)
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.set_profile import SetProfile
from scripts.hob_model_c_training import (
    AugmentedTrainingError,
    BasicScores,
    ModelCTrainingConfig,
    _load_array_training_data,
    build_basic_do_scores,
    train_and_gate_hob_model_c,
    train_and_gate_hob_model_c_arrays,
)

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


@dataclass(frozen=True, slots=True)
class _Prepared:
    set_code: str
    feature_names: tuple[str, ...]
    report: SimpleNamespace
    rows: tuple[CandidateTrainingRow, ...]

    def iter_rows(self) -> Iterator[CandidateTrainingRow]:
        """Yield the fixed candidate rows for one training pass."""

        yield from self.rows


def _prepared(*, set_code: str = "HOB") -> PreparedAugmentedTrainingData:
    rows: list[CandidateTrainingRow] = []
    partitions = ("train",) * 8 + ("validation",) * 4 + ("test",) * 4
    for draft_index, partition in enumerate(partitions):
        choose_a = draft_index % 2 == 0
        features = (3, 0, *([0] * 20)) if choose_a else (0, 3, *([0] * 20))
        for candidate_id in (_CARD_A, _CARD_B):
            rows.append(
                CandidateTrainingRow(
                    draft_index=draft_index,
                    partition=partition,
                    pick_index=0,
                    candidate_id=candidate_id,
                    chosen=(candidate_id == _CARD_A) == choose_a,
                    features=features,
                )
            )
    value = _Prepared(
        set_code=set_code,
        feature_names=FEATURE_NAMES,
        report=SimpleNamespace(
            event_type="PremierDraft",
            source_url="https://example.test/hob.csv.gz",
            retrieved_at="2026-09-20T12:00:00+00:00",
            sha256="a" * 64,
            attribution="17Lands public datasets",
            license="CC BY 4.0",
        ),
        rows=tuple(rows),
    )
    return cast(PreparedAugmentedTrainingData, value)


def _basic_scores(
    *, prepared: PreparedAugmentedTrainingData, chosen_score: float, other_score: float
) -> BasicScores:
    return {
        (row.draft_index, row.pick_index, row.candidate_id): (
            chosen_score if row.chosen else other_score
        )
        for row in prepared.iter_rows()
    }


def _fixture_card_database() -> CardDatabase:
    cards = (
        CardInfo(
            grp_id=1,
            name="Red Recruit",
            colors=("R",),
            mana_value=2.0,
            rarity="common",
            types=("Creature",),
            type_line="Creature — Human",
            set_code="HOB",
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
            set_code="HOB",
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
            set_code="HOB",
            oracle_id="00000000-0000-0000-0000-000000000003",
        ),
    )
    return CardDatabase(cards={card.grp_id: card for card in cards})


def _prepared_from_public_dump() -> PreparedAugmentedTrainingData:
    source = AugmentedTrainingSource(
        path=_FIXTURE_PATH,
        url="https://example.test/hob.csv.gz",
        sha256=hashlib.sha256(_FIXTURE_PATH.read_bytes()).hexdigest(),
        retrieved_at="2026-09-20T12:00:00+00:00",
        attribution="17Lands public datasets",
        license="CC BY 4.0",
        event_type="PremierDraft",
    )
    return prepare_augmented_training_data(
        set_code="HOB",
        source=source,
        card_database=_fixture_card_database(),
        complete_draft_picks=2,
    )


def test_fixed_seed_produces_the_same_promoted_model_c_artifact(
    tmp_path: Path,
) -> None:
    prepared = _prepared()
    scores = _basic_scores(prepared=prepared, chosen_score=0.0, other_score=0.25)
    paths = [
        (tmp_path / f"artifact-{index}.json", tmp_path / f"report-{index}.json")
        for index in range(2)
    ]

    results = [
        train_and_gate_hob_model_c(
            prepared=prepared,
            basic_scores=scores,
            artifact_path=artifact_path,
            report_path=report_path,
            config=_CONFIG,
        )
        for artifact_path, report_path in paths
    ]

    assert all(result.promoted for result in results)
    assert paths[0][0].read_bytes() == paths[1][0].read_bytes()
    assert paths[0][1].read_bytes() == paths[1][1].read_bytes()
    artifact = json.loads(paths[0][0].read_text(encoding="utf-8"))
    report = json.loads(paths[0][1].read_text(encoding="utf-8"))
    assert artifact["set_code"] == "HOB"
    assert artifact["model"]["architecture"] == (
        "bias_plus_tanh_low_rank_pool_context"
    )
    assert artifact["model"]["feature_schema"] == list(FEATURE_NAMES)
    assert len(artifact["model"]["feature_schema"]) == 22
    assert set(artifact["model"]["runtime_parameters"]) == {
        "bias",
        "hidden_activation",
        "input_transform",
        "input_weights",
        "output_weights",
    }
    assert artifact["source"] == {
        "attribution": "17Lands public datasets",
        "event_type": "PremierDraft",
        "license": "CC BY 4.0",
        "retrieved_at": "2026-09-20T12:00:00+00:00",
        "sha256": "a" * 64,
        "url": "https://example.test/hob.csv.gz",
    }
    calibration = artifact["calibration"]
    assert calibration["minimum_delta"] == -2.0
    assert calibration["maximum_delta"] == 2.0
    assert calibration["candidates"]
    basic = artifact["evaluation"]["basic_do"]
    augmented = artifact["evaluation"]["basic_plus_augmented"]
    assert basic["picks"] == augmented["picks"] == 4
    assert augmented["top_1"] > basic["top_1"]
    assert augmented["mean_reciprocal_rank"] > basic["mean_reciprocal_rank"]
    assert report["promotion"] == {
        "passed": True,
        "rule": "both_metrics_strictly_improve",
    }
    assert report["artifact_sha256"] is not None


def test_failed_promotion_writes_a_report_and_removes_the_artifact(
    tmp_path: Path,
) -> None:
    prepared = _prepared_from_public_dump()
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text("stale", encoding="utf-8")

    result = train_and_gate_hob_model_c(
        prepared=prepared,
        basic_scores=_basic_scores(
            prepared=prepared,
            chosen_score=100.0,
            other_score=0.0,
        ),
        artifact_path=artifact_path,
        report_path=tmp_path / "report.json",
        config=_CONFIG,
    )

    assert result.promoted is False
    assert artifact_path.exists() is False
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["promotion"]["passed"] is False
    assert report["artifact_sha256"] is None
    assert report["evaluation"]["basic_do"] == {
        "mean_reciprocal_rank": 1.0,
        "picks": 4,
        "top_1": 1.0,
    }


def test_basic_do_scores_cover_the_same_accepted_candidates() -> None:
    prepared = _prepared_from_public_dump()
    database = _fixture_card_database()

    scores = build_basic_do_scores(
        prepared=prepared,
        card_database=database,
        set_profile=SetProfile.generic(set_code="HOB", event_format="PremierDraft"),
    )

    expected_keys = {
        (row.draft_index, row.pick_index, row.candidate_id)
        for row in prepared.iter_rows()
    }
    assert set(scores) == expected_keys
    assert all(math.isfinite(score) for score in scores.values())


def test_compact_loader_matches_the_validated_fixture_rows() -> None:
    prepared = _prepared_from_public_dump()

    data = _load_array_training_data(
        source=prepared.source,
        card_database=_fixture_card_database(),
        complete_draft_picks=2,
    )

    assert data.rows_seen == prepared.report.rows_seen
    assert len(data.targets) == prepared.report.drafts_accepted * 2
    assert tuple(len(data.split_drafts[name]) for name in data.split_drafts) == (
        4,
        1,
        2,
    )
    assert data.features.shape == (14, 22)
    prepared_features = {
        (row.draft_index, row.pick_index): row.features
        for row in prepared.iter_rows()
    }
    assert [tuple(value) for value in data.features] == [
        prepared_features[(draft_index, pick_index)]
        for draft_index in range(7)
        for pick_index in range(2)
    ]


def test_compact_full_dump_path_removes_artifact_when_gate_fails(
    tmp_path: Path,
) -> None:
    prepared = _prepared_from_public_dump()
    database = _fixture_card_database()
    data = _load_array_training_data(
        source=prepared.source,
        card_database=database,
        complete_draft_picks=2,
    )
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text("stale", encoding="utf-8")

    result = train_and_gate_hob_model_c_arrays(
        data=data,
        source=prepared.source,
        card_database=database,
        set_profile=SetProfile.generic(
            set_code="HOB",
            event_format="PremierDraft",
        ),
        artifact_path=artifact_path,
        report_path=tmp_path / "report.json",
        config=replace(_CONFIG, calibration_multipliers=(0.0,)),
    )

    assert result.promoted is False
    assert artifact_path.exists() is False
    assert result.report["evaluation"]["basic_do"] == result.report["evaluation"][
        "basic_plus_augmented"
    ]


def test_pilot_rejects_non_hob_training_data(tmp_path: Path) -> None:
    prepared = _prepared(set_code="TST")

    with pytest.raises(AugmentedTrainingError, match="HOB data only"):
        train_and_gate_hob_model_c(
            prepared=prepared,
            basic_scores={},
            artifact_path=tmp_path / "artifact.json",
            report_path=tmp_path / "report.json",
            config=_CONFIG,
        )
