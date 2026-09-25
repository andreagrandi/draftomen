from __future__ import annotations

import gzip
import http.client
import io
import json
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from draftomen.carddb import (
    SCRYFALL_BULK_DATA_URL,
    SCRYFALL_USER_AGENT,
    CardDatabase,
    CardDatabaseCacheMissingError,
    CardDatabaseError,
    CardFace,
    CardInfo,
    CardMetadataSeed,
    augment_card_database_with_mtgjson_set,
    build_card_database_from_arena_data_dir,
    build_card_database_from_arena_cards,
    build_card_database_from_bulk_file,
    build_card_database_from_scryfall_cards,
    download_scryfall_default_cards_bulk_file,
    augment_card_database_with_arena_data,
    iter_scryfall_default_cards,
    load_card_database,
)
from draftomen.cli import main
from draftomen.events import DraftCompletedEvent, PackOfferedEvent, PickMadeEvent, parse_events

FIXTURE_LOG_PATH = Path(__file__).parent / "fixtures" / "quick-draft-msh-player.log"
SCRYFALL_BULK_SAMPLE_PATH = (
    Path(__file__).parent / "fixtures" / "scryfall-default-cards-sample.jsonl"
)
SEMANTIC_SAMPLE_PATH = (
    Path(__file__).parent / "fixtures" / "carddb-semantic-sample.jsonl"
)


def test_scryfall_bulk_sample_builds_fixture_grp_id_lookup() -> None:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)

    fixture_grp_ids = _fixture_grp_ids()
    missing_grp_ids = [
        grp_id for grp_id in sorted(fixture_grp_ids) if database.lookup(grp_id=grp_id).unknown
    ]

    assert len(database) == len(fixture_grp_ids)
    assert missing_grp_ids == []

    spider = database.lookup(grp_id=105097)
    assert spider.name == "Fixture Spider"
    assert spider.colors == ("G",)
    assert spider.mana_value == 4.0
    assert spider.rarity == "rare"
    assert spider.types == ("Creature — Spider",)

    split_card = database.lookup(grp_id=104894)
    assert split_card.colors == ("W", "U")
    assert split_card.types == ("Creature — Front // Creature — Back",)


def test_scryfall_semantic_faces_normalize_layout_and_local_fields() -> None:
    database = build_card_database_from_bulk_file(path=SEMANTIC_SAMPLE_PATH)

    transform = database.lookup(grp_id=9001)
    assert transform.arena_id == transform.grp_id == 9001
    assert transform.layout == "transform"
    assert transform.oracle_text == "Front text // Back text"
    assert transform.keywords == ("Daybound", "Flying")
    assert transform.type_line == "Creature — Human // Creature — Zombie"
    assert transform.subtypes == ("Human", "Zombie")
    assert transform.set_code == "sem"
    assert transform.collector_number == "1"
    assert transform.power is None
    assert transform.toughness is None
    assert transform.source_provenance == ("scryfall",)
    assert transform.faces == (
        CardFace(
            name="Front",
            oracle_text="Front text",
            keywords=("Daybound",),
            type_line="Creature — Human",
            subtypes=("Human",),
            colors=("W",),
            mana_cost="{2}{W}",
            mana_value=3.0,
            power="2",
            toughness="2",
        ),
        CardFace(
            name="Back",
            oracle_text="Back text",
            keywords=("Flying",),
            type_line="Creature — Zombie",
            subtypes=("Zombie",),
            colors=("B",),
            mana_cost=None,
            mana_value=3.0,
            power="3",
            toughness="3",
        ),
    )


    split = database.lookup(grp_id=9002)
    assert split.layout == "split"
    assert split.faces[0].oracle_text == "Deal 2 damage."
    assert split.faces[1].type_line == "Instant"

    adventure = database.lookup(grp_id=9003)
    assert adventure.layout == "adventure"
    assert adventure.faces[0].subtypes == ("Adventure",)
    assert adventure.faces[1].subtypes == ("Human",)

    modal_dfc = database.lookup(grp_id=9004)
    assert modal_dfc.layout == "modal_dfc"
    assert modal_dfc.oracle_text == "Cast this // Add {R}."
    assert [face.name for face in modal_dfc.faces] == [
        "Modal Front",
        "Modal Back",
    ]
    assert [face.type_line for face in modal_dfc.faces] == ["Sorcery", "Land"]

    no_faces = database.lookup(grp_id=9005)
    assert no_faces.layout == "normal"
    assert no_faces.faces == ()


def test_card_info_power_toughness_round_trip_preserves_text() -> None:
    card = CardInfo(
        grp_id=9010,
        name="Textual Stats",
        colors=("G",),
        mana_value=3.0,
        rarity="rare",
        types=("Creature",),
        power="*",
        toughness="1+*",
        oracle_id="oracle-textual-stats",
        faces=(CardFace(name="Face", power="X", toughness="*"),),
    )

    loaded = CardInfo.from_json(data=card.to_json())

    assert loaded == card
    with pytest.raises(CardDatabaseError, match="card.power"):
        CardInfo.from_json(data={**card.to_json(), "power": 2})


def test_scryfall_malformed_present_faces_fail() -> None:
    card = {
        "arena_id": 9100,
        "name": "Malformed Faces",
        "colors": [],
        "cmc": 1,
        "rarity": "common",
        "type_line": "Creature",
        "card_faces": None,
    }

    with pytest.raises(CardDatabaseError, match="card_faces"):
        build_card_database_from_scryfall_cards(cards=(card,))


def test_scryfall_oracle_id_is_normalized() -> None:
    database = build_card_database_from_scryfall_cards(
        cards=(
            {
                "arena_id": 9101,
                "oracle_id": "oracle-scryfall",
                "name": "Scryfall Identity",
                "colors": ["U"],
                "cmc": 2,
                "rarity": "common",
                "type_line": "Creature",
            },
        )
    )

    assert database.lookup(grp_id=9101).oracle_id == "oracle-scryfall"


def test_scryfall_bulk_keeps_mana_cost_produced_mana_and_image_uri(
    tmp_path: Path,
) -> None:
    bulk_path = tmp_path / "mana-fields.jsonl"
    bulk_path.write_text(
        "".join(
            (
                '{"arena_id":1,"name":"Pip Spell","colors":["W"],'
                '"cmc":2,"rarity":"common","type_line":"Creature",'
                '"mana_cost":"{W}{W}",'
                '"image_uris":{"normal":"https://cards.example/pip.jpg"}}\n',
                '{"arena_id":2,"name":"Dual Land","colors":[],'
                '"cmc":0,"rarity":"common","type_line":"Land",'
                '"produced_mana":["W","U"]}\n',
                '{"arena_id":3,"name":"Colorless Land","colors":[],'
                '"cmc":0,"rarity":"common","type_line":"Land",'
                '"produced_mana":["C"]}\n',
                '{"arena_id":4,"name":"Hybrid Fixer","colors":[],'
                '"cmc":0,"rarity":"common","type_line":"Land",'
                '"produced_mana":["C","G"]}\n',
                '{"arena_id":5,"name":"Faced Preview","card_faces":[{"colors":["R"],'
                '"type_line":"Creature","image_uris":'
                '{"normal":"https://cards.example/face.jpg"}}],'
                '"cmc":2,"rarity":"common","type_line":"Creature"}\n',
                '{"name":"Bulk Only","colors":["B"],"cmc":1,"rarity":"common",'
                '"type_line":"Creature","image_uris":'
                '{"normal":"https://cards.example/bulk-only.jpg"}}\n',
            )
        ),
        encoding="utf-8",
    )

    database = build_card_database_from_bulk_file(path=bulk_path)

    loaded = CardDatabase.from_json(data=database.to_json())

    assert database.lookup(grp_id=1).mana_cost == "{W}{W}"
    assert database.lookup(grp_id=1).image_uri == "https://cards.example/pip.jpg"
    assert database.image_uri_for_name(name="Pip Spell") == "https://cards.example/pip.jpg"
    assert (
        database.image_uri_for_name(name="Bulk Only")
        == "https://cards.example/bulk-only.jpg"
    )
    assert (
        loaded.image_uri_for_name(name="Faced Preview")
        == "https://cards.example/face.jpg"
    )
    assert database.lookup(grp_id=2).produced_mana == ("W", "U")
    assert database.lookup(grp_id=3).produced_mana == ()
    assert database.lookup(grp_id=4).produced_mana == ("G",)
    assert database.lookup(grp_id=5).image_uri == "https://cards.example/face.jpg"


def test_arena_local_data_builds_current_set_metadata(tmp_path: Path) -> None:
    arena_data_dir = _write_arena_data_dir(directory=tmp_path)

    database = build_card_database_from_arena_data_dir(path=arena_data_dir)

    spider = database.lookup(grp_id=105097)
    assert spider.name == "Arena Spider"
    assert spider.colors == ("G",)
    assert spider.mana_value == 4.0
    assert spider.rarity == "rare"
    assert spider.types == ("Creature — Spider",)
    assert spider.mana_cost == "{3}{G}"

    land = database.lookup(grp_id=105200)
    assert land.name == "Arena Dual"
    assert land.colors == ()
    assert land.rarity == "common"
    assert land.types == ("Land",)
    assert land.produced_mana == ("W", "U")


def test_arena_linked_faces_keep_face_local_semantics() -> None:
    database = build_card_database_from_arena_cards(
        cards=(
            {
                "grpid": 7001,
                "oracle_id": "oracle-arena",
                "titleId": 1,
                "cmc": 2,
                "rarity": 3,
                "cardTypeTextId": 10,
                "subtypeTextId": 11,
                "colors": [5],
                "castingcost": "oG",
                "oracleText": "Front rules",
                "linkedFaces": [7002],
            },
            {
                "grpid": 7002,
                "titleId": 2,
                "cmc": 3,
                "rarity": 3,
                "cardTypeTextId": 12,
                "subtypeTextId": 13,
                "colors": [1],
                "castingcost": "o2oW",
                "oracleText": "Back rules",
                "linkedFaces": [],
            },
        ),
        localization={
            1: "Front",
            2: "Back",
            10: "Creature",
            11: "Human",
            12: "Creature",
            13: "Spirit",
        },
    )

    card = database.lookup(grp_id=7001)
    assert card.arena_id == card.grp_id == 7001
    assert card.type_line == "Creature — Human // Creature — Spirit"
    assert card.subtypes == ("Human", "Spirit")
    assert card.colors == ("W", "G")
    assert card.oracle_text == "Front rules // Back rules"
    assert [face.name for face in card.faces] == ["Front", "Back"]
    assert [face.oracle_text for face in card.faces] == [
        "Front rules",
        "Back rules",
    ]
    assert card.oracle_id == "oracle-arena"
    assert card.source_provenance == ("arena",)


def test_mtgjson_other_face_ids_augment_only_missing_canonical_fields() -> None:
    database = augment_card_database_with_mtgjson_set(
        CardDatabase(
            cards={
                8001: CardInfo(
                    grp_id=8001,
                    name="MTG Front",
                    colors=("G",),
                    mana_value=2.0,
                    rarity="rare",
                    types=("Creature — Front",),
                    mana_cost=None,
                    power="2",
                    toughness=None,
                    oracle_text=None,
                    type_line=None,
                    source_provenance=("scryfall",),
                    oracle_id="oracle-base",
                )
            }
        ),
        set_code="SEM",
        seeds=(
            CardMetadataSeed(
                grp_id=8001,
                name="MTG Front",
                colors=("R",),
                rarity="common",
            ),
        ),
        mtgjson_cards=(
            {
                "uuid": "front",
                "name": "MTG Front",
                "type": "Creature — Front",
                "text": "Front text",
                "manaValue": 2,
                "manaCost": "{1}{G}",
                "power": "*",
                "toughness": "1+*",
                "colors": ["G"],
                "rarity": "rare",
                "setCode": "SEM",
                "number": "4",
                "identifiers": {"scryfallOracleId": "oracle-overlay"},
                "otherFaceIds": ["back"],
            },
            {
                "uuid": "back",
                "name": "MTG Back",
                "type": "Creature — Back",
                "text": "Back text",
                "manaValue": 3,
                "manaCost": "{2}{G}",
                "colors": ["G"],
                "rarity": "rare",
            },
        ),
    )

    card = database.lookup(grp_id=8001)
    assert card.oracle_id == "oracle-base"
    assert card.name == "MTG Front"
    assert card.colors == ("G",)
    assert card.rarity == "rare"
    assert card.power == "2"
    assert card.toughness == "1+*"
    assert card.oracle_text == "Front text"
    assert card.mana_cost == "{1}{G} // {2}{G}"
    assert card.type_line == "Creature — Front"
    assert [face.oracle_text for face in card.faces] == ["Front text", "Back text"]
    assert [face.type_line for face in card.faces] == [
        "Creature — Front",
        "Creature — Back",
    ]
    assert card.source_provenance == ("scryfall", "mtgjson")


def test_unmatched_mtgjson_seed_uses_17lands_provenance_and_can_retry() -> None:
    seed = CardMetadataSeed(
        grp_id=8010,
        name="Rating Seed",
        colors=("W",),
        rarity="common",
    )
    unmatched = augment_card_database_with_mtgjson_set(
        CardDatabase(cards={}),
        set_code="SEM",
        seeds=(seed,),
        mtgjson_cards=({"name": "Different Card"},),
    )

    seeded = unmatched.lookup(grp_id=8010)
    assert seeded.source_provenance == ("17lands",)
    assert seeded.unknown is True

    retried = augment_card_database_with_mtgjson_set(
        unmatched,
        set_code="SEM",
        seeds=(seed,),
        mtgjson_cards=(
            {
                "name": "Rating Seed",
                "manaValue": 2,
                "manaCost": "{1}{W}",
                "colors": ["W"],
                "rarity": "common",
                "type": "Creature",
                "availability": ["arena"],
                "identifiers": {"scryfallOracleId": "oracle-retry"},
            },
        ),
    )
    retried_card = retried.lookup(grp_id=8010)
    assert retried_card.unknown is False
    assert retried_card.oracle_id == "oracle-retry"
    assert "17lands" in retried_card.source_provenance
    assert "mtgjson" in retried_card.source_provenance


def test_unmatched_mtgjson_seed_preserves_partial_arena_card() -> None:
    existing = CardInfo(
        grp_id=8011,
        name="Partial Arena Card",
        colors=("U",),
        mana_value=3.0,
        rarity="rare",
        types=("Unknown",),
        mana_cost="{2}{U}",
        unknown=True,
        oracle_text="Arena rules",
        type_line=None,
        set_code="SEM",
        source_provenance=("arena",),
    )
    database = CardDatabase(cards={existing.grp_id: existing})
    result = augment_card_database_with_mtgjson_set(
        database,
        set_code="SEM",
        seeds=(
            CardMetadataSeed(
                grp_id=existing.grp_id,
                name="Not In MTGJSON",
                colors=("R",),
                rarity="common",
            ),
        ),
        mtgjson_cards=({"name": "Different Card"},),
    )

    retained = result.lookup(grp_id=existing.grp_id)
    assert retained is existing
    assert retained.name == "Partial Arena Card"
    assert retained.colors == ("U",)
    assert retained.mana_value == 3.0
    assert retained.set_code == "SEM"
    assert retained.source_provenance == ("arena",)


def test_mtgjson_augmentation_does_not_redownload_after_partial_contribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = CardDatabase(
        cards={
            8002: CardInfo(
                grp_id=8002,
                name="Partial Card",
                colors=("G",),
                mana_value=2.0,
                rarity="common",
                types=("Creature",),
                oracle_text=None,
                type_line=None,
                mana_cost=None,
                source_provenance=("scryfall",),
            )
        }
    )
    seed = CardMetadataSeed(
        grp_id=8002,
        name="Partial Card",
        colors=("G",),
        rarity="common",
    )
    partial_mtgjson = (
        {
            "uuid": "partial",
            "name": "Partial Card",
            "colors": ["G"],
            "rarity": "common",
        },
    )

    augmented = augment_card_database_with_mtgjson_set(
        database,
        set_code="SEM",
        seeds=(seed,),
        mtgjson_cards=partial_mtgjson,
    )
    assert augmented.lookup(grp_id=8002).source_provenance == (
        "scryfall",
        "mtgjson",
    )
    assert augmented.lookup(grp_id=8002).oracle_text is None

    def fail_download(**_kwargs: object) -> tuple[object, ...]:
        raise AssertionError("MTGJSON should not be downloaded twice")

    monkeypatch.setattr(
        "draftomen.carddb.download_mtgjson_set_cards",
        fail_download,
    )

    assert (
        augment_card_database_with_mtgjson_set(
            augmented,
            set_code="SEM",
            seeds=(seed,),
        )
        is augmented
    )


def test_mtgjson_set_metadata_resolves_grp_ids_from_name_seeds() -> None:
    database = augment_card_database_with_mtgjson_set(
        CardDatabase(
            cards={},
            generated_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        ),
        set_code="MSH",
        seeds=(
            CardMetadataSeed(
                grp_id=105097,
                name="Arena Spider",
                colors=("G",),
                rarity="rare",
            ),
        ),
        mtgjson_cards=(
            {
                "name": "Arena Spider",
                "manaValue": 4,
                "manaCost": "{3}{G}",
                "colors": ["G"],
                "producedMana": None,
                "rarity": "rare",
                "type": "Creature — Spider",
                "availability": ["arena"],
            },
            {
                "name": "Arena Forest",
                "manaValue": 0,
                "manaCost": None,
                "colors": [],
                "colorIdentity": ["G"],
                "producedMana": ["G"],
                "rarity": "basic",
                "type": "Basic Land — Forest",
                "availability": ["arena"],
            },
        ),
    )

    assert database.generated_at == datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    spider = database.lookup(grp_id=105097)
    assert spider.name == "Arena Spider"
    assert spider.colors == ("G",)
    assert spider.mana_value == 4.0
    assert spider.rarity == "rare"
    assert spider.types == ("Creature — Spider",)
    assert spider.mana_cost == "{3}{G}"
    assert spider.unknown is False
    assert spider.faces == ()

    forest = database.lookup(grp_id=105098)
    assert forest.name == "Arena Forest"
    assert forest.types == ("Basic Land — Forest",)
    assert forest.produced_mana == ("G",)


def test_scryfall_data_is_augmented_with_arena_local_data(
    tmp_path: Path,
) -> None:
    arena_data_dir = _write_arena_data_dir(directory=tmp_path)
    bulk_path = tmp_path / "stale-scryfall.jsonl"
    bulk_path.write_text(
        '{"arena_id":105097,"name":"Scryfall Spider","colors":["R"],'
        '"cmc":2,"rarity":"common","type_line":"Creature — Fixture",'
        '"image_uris":{"normal":"https://cards.example/spider.jpg"}}\n',
        encoding="utf-8",
    )
    scryfall = build_card_database_from_bulk_file(path=bulk_path)

    database = augment_card_database_with_arena_data(
        scryfall,
        arena_data_dir=arena_data_dir,
    )

    assert database.lookup(grp_id=105097).name == "Scryfall Spider"
    assert database.lookup(grp_id=105097).colors == ("R",)
    assert database.lookup(grp_id=105097).image_uri == "https://cards.example/spider.jpg"
    assert database.lookup(grp_id=105200).name == "Arena Dual"
    assert database.unresolved_grp_ids(grp_ids=(105097, 999999, 105200)) == (999999,)


def test_unknown_grp_id_returns_explicit_marker() -> None:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)

    unknown = database.lookup(grp_id=999999)

    assert unknown.unknown is True
    assert unknown.name == "Unknown card 999999"
    assert unknown.colors == ()
    assert unknown.mana_value is None
    assert unknown.rarity == "unknown"
    assert unknown.types == ("Unknown",)


def test_schema_three_file_migrates_with_explicit_metadata_defaults(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "cards.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "source": "scryfall-default-cards",
                "generated_at": None,
                "cards": {
                    "42": {
                        "grp_id": 42,
                        "name": "Legacy Card",
                        "colors": ["U"],
                        "mana_value": 2,
                        "rarity": "common",
                        "types": ["Creature"],
                    }
                },
                "image_uris_by_name": {},
            }
        ),
        encoding="utf-8",
    )

    database = load_card_database(cache_path=cache_path)
    card = database.lookup(grp_id=42)
    assert card.arena_id == 42
    assert card.oracle_text is None
    assert card.faces == ()
    assert card.source_provenance == ("unknown",)
    assert card.oracle_id is None
    assert database.to_json()["schema_version"] == 5


def test_resolved_schema_three_cards_skip_mtgjson_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "cards.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "source": "scryfall-default-cards",
                "generated_at": None,
                "cards": {
                    "42": {
                        "grp_id": 42,
                        "name": "Legacy Card",
                        "colors": ["U"],
                        "mana_value": 2,
                        "rarity": "common",
                        "types": ["Creature"],
                    }
                },
                "image_uris_by_name": {},
            }
        ),
        encoding="utf-8",
    )

    def fail_mtgjson_download(**_kwargs: object) -> tuple[object, ...]:
        pytest.fail("resolved schema-3 cards must not download MTGJSON")

    monkeypatch.setattr(
        "draftomen.carddb.download_mtgjson_set_cards",
        fail_mtgjson_download,
    )

    database = load_card_database(cache_path=cache_path)

    assert database.lookup(grp_id=42).source_provenance == ("unknown",)
    augmented = augment_card_database_with_mtgjson_set(
        database,
        set_code="TST",
        seeds=(
            CardMetadataSeed(
                grp_id=42,
                name="Legacy Card",
                colors=("U",),
                rarity="common",
            ),
        ),
    )

    assert augmented is database


def test_card_database_serialization_is_deterministic() -> None:
    database = CardDatabase(
        cards={
            2: CardInfo.unknown_card(grp_id=2),
            1: CardInfo.unknown_card(grp_id=1),
        },
        image_uris_by_name={
            "zeta": "https://cards.example/zeta.jpg",
            "alpha": "https://cards.example/alpha.jpg",
        },
        generated_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
    )

    first = json.dumps(database.to_json(), indent=2, sort_keys=True)
    second = json.dumps(database.to_json(), indent=2, sort_keys=True)

    assert first == second
    assert tuple(database.to_json()["cards"]) == ("1", "2")
    assert tuple(database.to_json()["image_uris_by_name"]) == ("alpha", "zeta")


@pytest.mark.parametrize("generated_at", [None, "not-a-timestamp"])
def test_cache_load_treats_missing_or_malformed_generated_at_as_unknown(
    tmp_path: Path,
    generated_at: object,
) -> None:
    database = build_card_database_from_bulk_file(path=SCRYFALL_BULK_SAMPLE_PATH)
    payload = database.to_json()
    if generated_at is None:
        payload.pop("generated_at")
    else:
        payload["generated_at"] = generated_at
    cache_path = tmp_path / "cards.json"
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_card_database(cache_path=cache_path)

    assert loaded.cards == database.cards
    assert loaded.generated_at is None


def test_load_of_a_missing_file_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "cards.json"

    with pytest.raises(CardDatabaseCacheMissingError) as error:
        load_card_database(cache_path=missing)

    assert str(missing) in str(error.value)


def test_refresh_data_cli_explains_that_card_data_downloads_per_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(argv=["refresh-data", "--app-dir", str(tmp_path)])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "no longer needed" in captured.out
    assert captured.err == ""
    assert list(tmp_path.iterdir()) == []


def test_scryfall_gzipped_jsonl_bulk_files_are_supported(tmp_path: Path) -> None:
    gzip_path = tmp_path / "scryfall-default-cards-sample.jsonl.gz"
    gzip_path.write_bytes(gzip.compress(SCRYFALL_BULK_SAMPLE_PATH.read_bytes()))

    database = build_card_database_from_bulk_file(path=gzip_path)

    assert database.lookup(grp_id=105182).name == "Fixture Final Pick"


def _fixture_grp_ids() -> set[int]:
    fixture_lines = FIXTURE_LOG_PATH.read_text(encoding="utf-8").splitlines()
    grp_ids: set[int] = set()
    for event in parse_events(lines=fixture_lines):
        if isinstance(event, PackOfferedEvent):
            grp_ids.update(event.offered_grp_ids)
            grp_ids.update(event.pool_grp_ids)
        elif isinstance(event, PickMadeEvent):
            grp_ids.add(event.chosen_grp_id)
        elif isinstance(event, DraftCompletedEvent):
            grp_ids.update(event.picked_grp_ids)

    return grp_ids


def _write_arena_data_dir(*, directory: Path) -> Path:
    arena_data_dir = directory / "arena-data"
    arena_data_dir.mkdir(parents=True, exist_ok=True)
    cards = [
        {
            "grpid": 105097,
            "titleId": 1001,
            "cmc": 4,
            "rarity": 4,
            "cardTypeTextId": 2001,
            "subtypeTextId": 2002,
            "colors": [5],
            "colorIdentity": [5],
            "castingcost": "o3oG",
            "linkedFaces": [],
        },
        {
            "grpid": 105200,
            "titleId": 1002,
            "cmc": 0,
            "rarity": 2,
            "cardTypeTextId": 2003,
            "subtypeTextId": 0,
            "colors": [],
            "colorIdentity": [1, 2],
            "castingcost": "o0",
            "linkedFaces": [],
        },
    ]
    localization = [
        {
            "langkey": "EN",
            "isoCode": "en-US",
            "keys": [
                {"id": 1001, "text": "Arena Spider"},
                {"id": 1002, "text": "Arena Dual"},
                {"id": 2001, "text": "Creature"},
                {"id": 2002, "text": "Spider"},
                {"id": 2003, "text": "Land"},
            ],
        }
    ]
    (arena_data_dir / "data_cards_fixture.mtga").write_text(
        json.dumps(cards),
        encoding="utf-8",
    )
    (arena_data_dir / "data_loc_fixture.mtga").write_text(
        json.dumps(localization),
        encoding="utf-8",
    )
    return arena_data_dir



@pytest.mark.parametrize("compressed", [False, True])
def test_scryfall_default_card_iterator_reads_local_jsonl_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compressed: bool,
) -> None:
    rows = [
        {"arena_id": 101, "name": "Local One"},
        {"arena_id": 102, "name": "Local Two"},
    ]
    payload = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
    suffix = ".jsonl.gz" if compressed else ".jsonl"
    bulk_path = tmp_path / f"cards{suffix}"
    if compressed:
        bulk_path.write_bytes(gzip.compress(payload))
    else:
        bulk_path.write_bytes(payload)

    def unexpected_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("local Scryfall bulk iteration must not use the network")

    monkeypatch.setattr("draftomen.carddb.urllib.request.urlopen", unexpected_network)
    source = iter_scryfall_default_cards(bulk_file=bulk_path)

    assert list(source) == rows
    assert list(source) == []


def test_scryfall_default_card_iterator_remote_uses_one_metadata_and_download_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"arena_id": 201, "name": "Remote One"},
        {"arena_id": 202, "name": "Remote Two"},
    ]
    compressed_rows = gzip.compress(
        ("".join(json.dumps(row) + "\n" for row in rows)).encode("utf-8")
    )
    metadata = {
        "data": [
            {"type": "bulk_data", "download_uri": "https://example.invalid/other"},
            {
                "type": "default_cards",
                "jsonl_download_uri": "https://example.invalid/default.jsonl.gz",
            },
        ]
    }
    calls: list[tuple[str, int]] = []

    class Response(io.BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def fake_urlopen(request: object, timeout: int) -> Response:
        url = request.full_url
        calls.append((url, timeout))
        if url == "https://api.scryfall.com/bulk-data":
            return Response(json.dumps(metadata).encode("utf-8"))
        if url == "https://example.invalid/default.jsonl.gz":
            return Response(compressed_rows)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("draftomen.carddb.urllib.request.urlopen", fake_urlopen)
    source = iter_scryfall_default_cards(timeout_seconds=17)

    assert list(source) == rows
    assert calls == [
        ("https://api.scryfall.com/bulk-data", 17),
        ("https://example.invalid/default.jsonl.gz", 17),
    ]
    assert list(source) == []


_SCRYFALL_BULK_METADATA_PAYLOAD = json.dumps(
    {
        "data": [
            {
                "type": "default_cards",
                "jsonl_download_uri": "https://example.invalid/default.jsonl.gz",
            }
        ]
    }
).encode("utf-8")
_SCRYFALL_DEFAULT_CARDS_URL = "https://example.invalid/default.jsonl.gz"


class _BulkDownloadResponse(io.BytesIO):
    def __init__(self, body: bytes, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.headers: dict[str, str] = {} if headers is None else headers

    def __enter__(self) -> "_BulkDownloadResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _bulk_download_urlopen(
    *,
    download_response: io.BytesIO,
    requests: list[urllib.request.Request],
) -> Callable[..., io.BytesIO]:
    def fake_urlopen(request: urllib.request.Request, timeout: int) -> io.BytesIO:
        requests.append(request)
        if request.full_url == SCRYFALL_BULK_DATA_URL:
            return _BulkDownloadResponse(_SCRYFALL_BULK_METADATA_PAYLOAD)
        if request.full_url == _SCRYFALL_DEFAULT_CARDS_URL:
            return download_response
        raise AssertionError(f"unexpected URL: {request.full_url}")

    return fake_urlopen


def test_download_scryfall_bulk_file_streams_bytes_with_scryfall_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = gzip.compress(b'{"arena_id": 1}\n')
    destination = tmp_path / "sources" / "scryfall-default-cards.jsonl.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"previous bulk file")
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        "draftomen.carddb.urllib.request.urlopen",
        _bulk_download_urlopen(
            download_response=_BulkDownloadResponse(payload),
            requests=requests,
        ),
    )

    result = download_scryfall_default_cards_bulk_file(destination=destination)

    assert result == destination
    assert destination.read_bytes() == payload
    assert [entry.name for entry in destination.parent.iterdir()] == [destination.name]
    assert [request.full_url for request in requests] == [
        SCRYFALL_BULK_DATA_URL,
        _SCRYFALL_DEFAULT_CARDS_URL,
    ]
    assert [request.get_header("User-agent") for request in requests] == [
        SCRYFALL_USER_AGENT,
        SCRYFALL_USER_AGENT,
    ]


@pytest.mark.parametrize(
    "failure",
    [OSError("connection reset"), http.client.IncompleteRead(b"", 10)],
    ids=["oserror", "incomplete-read"],
)
def test_download_scryfall_bulk_file_keeps_the_previous_file_when_the_stream_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    previous = gzip.compress(b'{"arena_id": 1}\n')
    destination = tmp_path / "scryfall-default-cards.jsonl.gz"
    destination.write_bytes(previous)
    requests: list[urllib.request.Request] = []

    class FailingResponse(_BulkDownloadResponse):
        def read(self, size: int = -1) -> bytes:
            chunk = super().read(size)
            if chunk:
                return chunk
            raise failure

    monkeypatch.setattr(
        "draftomen.carddb.urllib.request.urlopen",
        _bulk_download_urlopen(
            download_response=FailingResponse(gzip.compress(b'{"arena_id": 2}\n')),
            requests=requests,
        ),
    )

    with pytest.raises(
        CardDatabaseError,
        match="Failed to download Scryfall default-cards bulk data",
    ):
        download_scryfall_default_cards_bulk_file(destination=destination)

    assert destination.read_bytes() == previous
    assert tuple(destination.parent.glob(f".{destination.name}.*")) == ()


def test_download_scryfall_bulk_file_keeps_the_previous_file_when_the_body_is_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = gzip.compress(b'{"arena_id": 1}\n')
    destination = tmp_path / "scryfall-default-cards.jsonl.gz"
    destination.write_bytes(previous)
    body = gzip.compress(b'{"arena_id": 2}\n')
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        "draftomen.carddb.urllib.request.urlopen",
        _bulk_download_urlopen(
            download_response=_BulkDownloadResponse(
                body,
                headers={"Content-Length": str(len(body) + 1024)},
            ),
            requests=requests,
        ),
    )

    with pytest.raises(
        CardDatabaseError,
        match="Failed to download Scryfall default-cards bulk data",
    ):
        download_scryfall_default_cards_bulk_file(destination=destination)

    assert destination.read_bytes() == previous
    assert tuple(destination.parent.glob(f".{destination.name}.*")) == ()


def test_download_scryfall_bulk_file_rejects_a_non_gzip_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "sources" / "scryfall-default-cards.jsonl.gz"
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        "draftomen.carddb.urllib.request.urlopen",
        _bulk_download_urlopen(
            download_response=_BulkDownloadResponse(b'[{"id": "abc"}]'),
            requests=requests,
        ),
    )

    with pytest.raises(CardDatabaseError, match="is not a gzip JSONL file"):
        download_scryfall_default_cards_bulk_file(destination=destination)

    assert not destination.exists()
    assert tuple(destination.parent.glob(f".{destination.name}.*")) == ()


def test_download_scryfall_bulk_file_reports_progress_and_cancels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = gzip.compress(b'{"arena_id": 1}\n')
    destination = tmp_path / "sources" / "scryfall-default-cards.jsonl.gz"
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        "draftomen.carddb.urllib.request.urlopen",
        _bulk_download_urlopen(
            download_response=_BulkDownloadResponse(
                payload,
                headers={"Content-Length": str(len(payload))},
            ),
            requests=requests,
        ),
    )
    updates: list[tuple[int, int | None]] = []
    stop_calls = 0

    def record_progress(completed: int, total: int | None) -> None:
        updates.append((completed, total))

    def should_stop() -> bool:
        nonlocal stop_calls
        stop_calls += 1
        return stop_calls >= 2

    with pytest.raises(CardDatabaseError, match="was cancelled"):
        download_scryfall_default_cards_bulk_file(
            destination=destination,
            should_stop=should_stop,
            progress=record_progress,
        )

    assert updates == [(len(payload), len(payload))]
    assert not destination.exists()
    assert tuple(destination.parent.glob(f".{destination.name}.*")) == ()
