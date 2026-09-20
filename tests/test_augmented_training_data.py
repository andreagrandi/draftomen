from __future__ import annotations

import csv
import hashlib
from dataclasses import fields
from pathlib import Path

import pytest

from draftomen.augmented_training_data import (
    CARD_TYPES,
    COLORS,
    COMPOSITION,
    CURVE_BUCKETS,
    FEATURE_NAMES,
    AugmentedTrainingDataError,
    AugmentedTrainingSource,
    CandidateTrainingRow,
    PreparedAugmentedTrainingData,
    prepare_augmented_training_data,
)
from draftomen.carddb import CardDatabase, CardFace, CardInfo

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "17lands-public-drafts-hob-training.csv"
)


def _card_database(*, set_code: str) -> CardDatabase:
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
            oracle_id="00000000-0000-0000-0000-000000000001",
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
            oracle_id="00000000-0000-0000-0000-000000000002",
        ),
        CardInfo(
            grp_id=3,
            name="Clockwork Relic // Wind Up",
            colors=(),
            mana_value=1.0,
            rarity="common",
            types=("Artifact",),
            type_line="Artifact // Sorcery",
            faces=(
                CardFace(name="Clockwork Relic", type_line="Artifact"),
                CardFace(name="Wind Up", type_line="Sorcery"),
            ),
            set_code=set_code,
            oracle_id="00000000-0000-0000-0000-000000000003",
        ),
    )
    return CardDatabase(cards={card.grp_id: card for card in cards})


def _source(*, path: Path) -> AugmentedTrainingSource:
    return AugmentedTrainingSource(
        path=path,
        url=f"https://example.test/{path.name}",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        retrieved_at="2026-09-20T12:00:00+00:00",
        attribution="17Lands public datasets",
        license="CC BY 4.0",
        event_type="PremierDraft",
    )


def _prepared(
    *, path: Path = FIXTURE_PATH, set_code: str = "HOB"
) -> PreparedAugmentedTrainingData:
    return prepare_augmented_training_data(
        set_code=set_code,
        source=_source(path=path),
        card_database=_card_database(set_code=set_code),
        complete_draft_picks=2,
    )


def test_hob_fixture_creates_one_candidate_row_per_offer_and_one_positive_label() -> (
    None
):
    prepared = _prepared()
    rows = tuple(prepared.iter_rows())

    assert prepared.report.drafts_seen == 10
    assert prepared.report.drafts_accepted == 7
    assert prepared.report.drafts_skipped == 3
    assert prepared.report.candidate_rows == 28
    assert len(rows) == 28
    assert sum(row.chosen for row in rows) == 14
    assert {
        (row.draft_index, row.pick_index): sum(
            candidate.chosen
            for candidate in rows
            if candidate.draft_index == row.draft_index
            and candidate.pick_index == row.pick_index
        )
        for row in rows
    } == {(draft, pick): 1 for draft in range(7) for pick in range(2)}


def test_feature_schema_matches_the_tested_twenty_two_pool_counts() -> None:
    prepared = _prepared()
    rows = tuple(prepared.iter_rows())
    second_pick = next(
        row
        for row in rows
        if row.draft_index == 0
        and row.pick_index == 1
        and row.candidate_id == "00000000-0000-0000-0000-000000000002"
    )

    assert prepared.feature_names == FEATURE_NAMES
    assert len(FEATURE_NAMES) == 22
    assert FEATURE_NAMES == tuple(
        [f"color:{value}" for value in COLORS]
        + [f"composition:{value}" for value in COMPOSITION]
        + [f"curve:{value}" for value in CURVE_BUCKETS]
        + [f"type:{value}" for value in CARD_TYPES]
    )
    assert second_pick.features == (
        0,
        0,
        0,
        1,
        0,
        1,
        0,
        0,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    assert {row.candidate_id for row in rows} == {
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    }


def test_loader_reports_second_picks_and_bad_or_incomplete_histories() -> None:
    report = _prepared().report

    assert report.skipped_drafts == {
        "incomplete_or_inconsistent_history": 1,
        "inconsistent_pool_history": 1,
        "second_pick": 1,
    }
    assert report.drafts_seen == report.drafts_accepted + 3


def test_complete_drafts_stay_in_one_chronological_partition() -> None:
    rows = tuple(_prepared().iter_rows())
    partitions_by_draft = {
        draft_index: {row.partition for row in rows if row.draft_index == draft_index}
        for draft_index in range(7)
    }

    assert partitions_by_draft == {
        0: {"train"},
        1: {"train"},
        2: {"train"},
        3: {"train"},
        4: {"validation"},
        5: {"test"},
        6: {"test"},
    }
    assert _prepared().report.partition_drafts == {
        "train": 4,
        "validation": 1,
        "test": 2,
    }


def test_model_features_cannot_contain_source_identifiers_or_later_results(
    tmp_path: Path,
) -> None:
    row_fields = {field.name for field in fields(CandidateTrainingRow)}
    forbidden = {
        "draft_id",
        "draft_time",
        "rank",
        "event_match_wins",
        "event_match_losses",
        "pick",
        "pick_2",
        "pool_after_pick",
    }

    assert row_fields.isdisjoint(forbidden)
    assert all(
        name.startswith(("color:", "composition:", "curve:", "type:"))
        for name in FEATURE_NAMES
    )

    changed_path = tmp_path / "changed-private-fields.csv"
    with (
        FIXTURE_PATH.open(encoding="utf-8", newline="") as input_file,
        changed_path.open(mode="w", encoding="utf-8", newline="") as output_file,
    ):
        reader = csv.DictReader(input_file)
        assert reader.fieldnames is not None
        writer = csv.DictWriter(output_file, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            row["draft_id"] = f"changed-{row['draft_id']}"
            row["draft_time"] = row["draft_time"].replace("2026", "2027")
            row["rank"] = "changed-rank"
            row["event_match_wins"] = "99"
            row["event_match_losses"] = "88"
            if row["pick_number"] == "1" and not row["pick_2"]:
                offered = [
                    name.removeprefix("pack_card_")
                    for name, value in row.items()
                    if name.startswith("pack_card_") and value == "1"
                ]
                row["pick"] = next(name for name in offered if name != row["pick"])
            writer.writerow(row)

    original = tuple(_prepared().iter_rows())
    changed = tuple(_prepared(path=changed_path).iter_rows())

    assert [row.features for row in changed] == [row.features for row in original]


def test_requested_set_controls_validation_and_reported_provenance(
    tmp_path: Path,
) -> None:
    with pytest.raises(AugmentedTrainingDataError, match="wrong set"):
        _prepared(set_code="TST")

    second_fixture = tmp_path / "tst-public-drafts.csv"
    second_fixture.write_text(
        FIXTURE_PATH.read_text(encoding="utf-8").replace("HOB", "TST"),
        encoding="utf-8",
    )
    prepared = _prepared(path=second_fixture, set_code="tst")

    assert prepared.set_code == "TST"
    assert prepared.report.set_code == "TST"
    assert prepared.report.source_url == "https://example.test/tst-public-drafts.csv"
    assert (
        prepared.report.sha256
        == hashlib.sha256(second_fixture.read_bytes()).hexdigest()
    )
    assert prepared.report.retrieved_at == "2026-09-20T12:00:00+00:00"
    assert prepared.report.attribution == "17Lands public datasets"
    assert prepared.report.license == "CC BY 4.0"


def test_training_preparation_rejects_unpinned_or_mismatched_inputs() -> None:
    with pytest.raises(AugmentedTrainingDataError, match="SHA-256"):
        AugmentedTrainingSource(
            path=FIXTURE_PATH,
            url="https://example.test/draft.csv",
            sha256="not-a-checksum",
            retrieved_at="2026-09-20T12:00:00+00:00",
            attribution="17Lands public datasets",
            license="CC BY 4.0",
            event_type="PremierDraft",
        )

    wrong_event = AugmentedTrainingSource(
        path=FIXTURE_PATH,
        url="https://example.test/draft.csv",
        sha256=hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
        retrieved_at="2026-09-20T12:00:00+00:00",
        attribution="17Lands public datasets",
        license="CC BY 4.0",
        event_type="QuickDraft",
    )
    with pytest.raises(AugmentedTrainingDataError, match="wrong event type"):
        prepare_augmented_training_data(
            set_code="HOB",
            source=wrong_event,
            card_database=_card_database(set_code="HOB"),
            complete_draft_picks=2,
        )


def test_training_preparation_rejects_ambiguous_card_name_aliases() -> None:
    database = _card_database(set_code="HOB")
    database.cards[4] = CardInfo(
        grp_id=4,
        name="Different Card",
        colors=("G",),
        mana_value=4.0,
        rarity="rare",
        types=("Creature",),
        type_line="Creature — Shapeshifter",
        faces=(CardFace(name="Clockwork Relic", type_line="Creature"),),
        set_code="HOB",
        oracle_id="00000000-0000-0000-0000-000000000004",
    )

    with pytest.raises(AugmentedTrainingDataError, match="conflicting Oracle cards"):
        prepare_augmented_training_data(
            set_code="HOB",
            source=_source(path=FIXTURE_PATH),
            card_database=database,
            complete_draft_picks=2,
        )


def test_alternate_arena_printings_share_one_oracle_candidate() -> None:
    database = _card_database(set_code="HOB")
    database.cards[4] = CardInfo(
        grp_id=4,
        name="Red Recruit",
        colors=("R",),
        mana_value=2.0,
        rarity="common",
        types=("Creature",),
        type_line="Creature — Human",
        set_code="HOB",
        oracle_id="00000000-0000-0000-0000-000000000001",
    )

    prepared = prepare_augmented_training_data(
        set_code="HOB",
        source=_source(path=FIXTURE_PATH),
        card_database=database,
        complete_draft_picks=2,
    )

    assert {row.candidate_id for row in prepared.iter_rows()} == {
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    }
