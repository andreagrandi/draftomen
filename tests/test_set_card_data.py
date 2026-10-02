from __future__ import annotations

import gzip
import json
from typing import Any

import pytest

from draftomen.carddb import CardDatabase, CardFace, CardInfo
from draftomen.semantic_roles import resolve_card_roles
from draftomen.set_card_data import (
    CARD_DATA_SCHEMA_VERSION,
    CARD_DATA_SOURCE,
    SetCardData,
    SetCardDataError,
)


def _face(*, name: str | None = "Front") -> CardFace:
    return CardFace(
        name=name,
        oracle_text="Draw a card." if name else None,
        keywords=("Flying",) if name else (),
        type_line="Creature — Bird" if name else None,
        subtypes=("Bird",) if name else (),
        colors=("W",) if name else (),
        mana_cost="{1}{W}" if name else None,
        mana_value=2.0 if name else None,
        produced_mana=("W",) if name else (),
        power="2" if name else None,
        toughness="2" if name else None,
    )


def _card(
    arena_id: int,
    *,
    set_code: str | None = "abc",
    name: str = "Sky Diviner",
    image_uri: str | None = "https://cards.example/sky.jpg",
) -> CardInfo:
    return CardInfo(
        grp_id=arena_id,
        name=name,
        colors=("W", "U"),
        mana_value=3.0,
        rarity="rare",
        types=("Creature",),
        mana_cost="{2}{W}",
        produced_mana=(),
        image_uri=image_uri,
        oracle_text="When this enters, draw a card.",
        keywords=("Flying",),
        type_line="Creature — Bird Wizard",
        subtypes=("Bird", "Wizard"),
        layout="transform",
        faces=(_face(), _face(name="Back")),
        set_code=set_code,
        collector_number="42",
        arena_id=arena_id,
        source_provenance=("scryfall",),
        power="3",
        toughness="3",
        oracle_id=f"oracle-{arena_id}",
    )


def _artifact() -> SetCardData:
    database = CardDatabase(
        cards={
            2: _card(2, name="Second Card", image_uri=None),
            1: _card(1),
            9: _card(9, name="Name With ’ Quote", image_uri="https://cards.example/quote.jpg"),
        },
        image_uris_by_name={
            "sky diviner": "https://cards.example/sky.jpg",
            "second card": "https://cards.example/second.jpg",
            "name with ' quote": "https://cards.example/quote.jpg",
            "front": "https://cards.example/sky.jpg",
            "back": "https://cards.example/sky.jpg",
        },
    )
    return SetCardData.from_card_database(
        database,
        set_code="ABC",
        set_name="Alpha Beta Cards",
    )


def test_v1_round_trip_has_exact_shape_and_canonical_bytes() -> None:
    artifact = _artifact()
    assert set(artifact.to_json()) == {
        "cards",
        "image_uris_by_name",
        "schema_version",
        "set_code",
        "set_name",
        "source",
    }
    assert artifact.schema_version == CARD_DATA_SCHEMA_VERSION
    assert artifact.source == CARD_DATA_SOURCE
    assert artifact.set_code == "abc"
    assert [card["arena_id"] for card in artifact.to_json()["cards"]] == [1, 2, 9]
    assert {card["set_code"] for card in artifact.to_json()["cards"]} == {"abc"}

    raw = artifact.to_bytes()
    loaded = SetCardData.from_bytes(raw)
    assert {card.set_code for card in loaded.cards} == {"abc"}
    assert loaded.to_bytes() == raw
    compressed = artifact.to_gzip_bytes()
    assert compressed[:2] == b"\x1f\x8b"
    assert compressed[3] == 0
    assert compressed[4:8] == b"\x00\x00\x00\x00"
    assert SetCardData.from_gzip_bytes(compressed) == artifact
    assert SetCardData.from_gzip_bytes(compressed).to_gzip_bytes() == compressed


def test_all_card_and_face_fields_and_nullables_survive_reconstruction() -> None:
    artifact = _artifact()
    card = artifact.to_card_database().lookup(grp_id=1)
    expected = _card(1)
    assert card == expected
    assert card.grp_id == card.arena_id == 1
    assert card.name == expected.name
    assert card.colors == expected.colors
    assert card.mana_value == expected.mana_value
    assert card.rarity == expected.rarity
    assert card.types == expected.types
    assert card.mana_cost == expected.mana_cost
    assert card.produced_mana == expected.produced_mana
    assert card.image_uri == expected.image_uri
    assert card.oracle_text == expected.oracle_text
    assert card.keywords == expected.keywords
    assert card.type_line == expected.type_line
    assert card.subtypes == expected.subtypes
    assert card.layout == expected.layout
    assert card.collector_number == expected.collector_number
    assert card.oracle_id == expected.oracle_id
    assert card.power == expected.power
    assert card.toughness == expected.toughness
    assert card.unknown is False
    assert card.source_provenance == ("scryfall",)
    assert card.faces == expected.faces

    nullable = CardInfo(
        grp_id=20,
        name="Nullable",
        colors=(),
        mana_value=None,
        rarity="common",
        types=(),
        mana_cost=None,
        produced_mana=(),
        image_uri=None,
        oracle_text=None,
        keywords=(),
        type_line=None,
        subtypes=(),
        layout=None,
        faces=(_face(name=None),),
        set_code="abc",
        collector_number=None,
        arena_id=20,
        power=None,
        toughness=None,
        oracle_id=None,
    )
    data = SetCardData(set_code="abc", set_name="Alpha", cards=(nullable,))
    rebuilt = data.to_card_database().lookup(grp_id=20)
    assert rebuilt.mana_value is None
    assert rebuilt.faces == (_face(name=None),)
    assert rebuilt.image_uri is None


def test_lookup_and_image_index_preserve_existing_behavior() -> None:
    database = _artifact().to_card_database()
    assert database.lookup(grp_id=1).name == "Sky Diviner"
    assert database.lookup(grp_id=999999).unknown is True
    assert database.image_uri_for_name(name="SKY DIVINER") == "https://cards.example/sky.jpg"
    assert database.image_uri_for_name(name="Front") == "https://cards.example/sky.jpg"
    assert database.image_uri_for_name(name="not in set") is None


def test_semantic_role_resolution_is_unchanged_by_round_trip() -> None:
    before = _artifact().to_card_database().lookup(grp_id=1)
    after = SetCardData.from_bytes(_artifact().to_bytes()).to_card_database().lookup(grp_id=1)
    assert resolve_card_roles(before).assignments == resolve_card_roles(after).assignments


def test_expected_set_validation_is_exact() -> None:
    artifact = _artifact()
    artifact.validate_expected_set(set_code="ABC", set_name="Alpha Beta Cards")
    with pytest.raises(SetCardDataError, match="belongs to set"):
        artifact.validate_expected_set(set_code="xyz")
    with pytest.raises(SetCardDataError, match="named"):
        artifact.validate_expected_set(set_code="abc", set_name="Different")


def test_duplicate_ids_zero_cards_and_mixed_set_are_rejected() -> None:
    first = _card(1)
    with pytest.raises(SetCardDataError, match="duplicate"):
        SetCardData(set_code="abc", set_name="Alpha", cards=(first, first))
    with pytest.raises(SetCardDataError, match="at least one"):
        SetCardData(set_code="abc", set_name="Alpha", cards=())
    with pytest.raises(SetCardDataError, match="belongs to set"):
        SetCardData(set_code="abc", set_name="Alpha", cards=(_card(1, set_code="xyz"),))

    with pytest.raises(SetCardDataError, match="set_code"):
        SetCardData(set_code="abc", set_name="Alpha", cards=(_card(1, set_code=None),))
    with pytest.raises(SetCardDataError, match="lowercase"):
        SetCardData(set_code="abc", set_name="Alpha", cards=(_card(1, set_code="ABC"),))


def test_serialized_mixed_set_membership_is_rejected() -> None:
    artifact = _artifact()
    value = artifact.to_json()
    mixed_card = dict(value["cards"][0])
    mixed_card["set_code"] = "xyz"
    mixed_value = {
        **value,
        "cards": [mixed_card, *value["cards"][1:]],
    }
    with pytest.raises(SetCardDataError, match="belongs to set"):
        SetCardData.from_json(mixed_value)

    mixed_bytes = (
        json.dumps(
            mixed_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    with pytest.raises(SetCardDataError, match="belongs to set"):
        SetCardData.from_bytes(mixed_bytes)


def test_unsorted_cards_are_rejected() -> None:
    with pytest.raises(SetCardDataError, match="sorted"):
        SetCardData(
            set_code="abc",
            set_name="Alpha",
            cards=(_card(2), _card(1)),
        )


def test_unknown_and_missing_keys_are_rejected() -> None:
    value = _artifact().to_json()
    with pytest.raises(SetCardDataError, match="invalid keys"):
        SetCardData.from_json({**value, "extra": True})
    missing = dict(value)
    del missing["source"]
    with pytest.raises(SetCardDataError, match="missing source"):
        SetCardData.from_json(missing)
    card = dict(value["cards"][0])
    card["extra"] = True
    with pytest.raises(SetCardDataError, match="unknown extra"):
        SetCardData.from_json({**value, "cards": [card, *value["cards"][1:]]})
    missing_card_set_code = dict(value["cards"][0])
    del missing_card_set_code["set_code"]
    with pytest.raises(SetCardDataError, match="missing set_code"):
        SetCardData.from_json(
            {**value, "cards": [missing_card_set_code, *value["cards"][1:]]}
        )


def _with_unknown_keys(value: dict[str, Any]) -> dict[str, Any]:
    cards = []
    for card in value["cards"]:
        faces = [{**face, "future_face_field": 1} for face in card["faces"]]
        cards.append({**card, "faces": faces, "future_card_field": "x"})
    return {**value, "cards": cards, "future_field": True}


def test_loader_ignores_unknown_keys_and_returns_the_same_cards() -> None:
    artifact = _artifact()
    value = _with_unknown_keys(artifact.to_json())
    assert any(card["faces"] for card in value["cards"])
    payload = gzip.compress(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    )

    loaded = SetCardData.from_gzip_bytes(payload, strict=False)

    assert loaded == artifact
    assert SetCardData.from_json(value, strict=False) == artifact


def test_loader_still_rejects_missing_keys() -> None:
    value = _with_unknown_keys(_artifact().to_json())
    missing = dict(value)
    del missing["source"]
    with pytest.raises(SetCardDataError, match="missing source"):
        SetCardData.from_json(missing, strict=False)
    card = dict(value["cards"][0])
    del card["rarity"]
    with pytest.raises(SetCardDataError, match="missing rarity"):
        SetCardData.from_json({**value, "cards": [card, *value["cards"][1:]]}, strict=False)
    face_card = next(card for card in value["cards"] if card["faces"])
    face = dict(face_card["faces"][0])
    del face["name"]
    broken = {**face_card, "faces": [face, *face_card["faces"][1:]]}
    cards = [broken if card is face_card else card for card in value["cards"]]
    with pytest.raises(SetCardDataError, match="missing name"):
        SetCardData.from_json({**value, "cards": cards}, strict=False)


def test_strict_parsing_rejects_unknown_keys_at_every_level() -> None:
    value = _with_unknown_keys(_artifact().to_json())
    with pytest.raises(SetCardDataError, match="unknown future_field"):
        SetCardData.from_json(value)
    payload = gzip.compress(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    )
    with pytest.raises(SetCardDataError):
        SetCardData.from_gzip_bytes(payload)


def test_duplicate_json_object_keys_are_rejected() -> None:
    with pytest.raises(SetCardDataError, match="Duplicate JSON"):
        SetCardData.from_bytes(b'{"cards":[],"cards":[]}')


def test_noncanonical_json_is_rejected() -> None:
    artifact = _artifact()
    pretty = (json.dumps(artifact.to_json(), ensure_ascii=False, indent=2) + "\n").encode()
    with pytest.raises(SetCardDataError, match="not canonical"):
        SetCardData.from_bytes(pretty)


def test_noncanonical_gzip_is_rejected() -> None:
    artifact = _artifact()
    noncanonical = gzip.compress(artifact.to_bytes(), compresslevel=9, mtime=1)
    assert noncanonical != artifact.to_gzip_bytes()
    with pytest.raises(SetCardDataError, match="not canonical"):
        SetCardData.from_gzip_bytes(noncanonical)


def test_invalid_values_and_bounded_gzip_are_rejected() -> None:
    value = _artifact().to_json()
    bad_card = dict(value["cards"][0])
    bad_card["arena_id"] = 0
    with pytest.raises(SetCardDataError, match="positive"):
        SetCardData.from_json({**value, "cards": [bad_card, *value["cards"][1:]]})
    with pytest.raises(SetCardDataError, match="outside"):
        SetCardData.from_json({**value, "image_uris_by_name": {"outside": "https://x"}})
    with pytest.raises(SetCardDataError, match="size limit"):
        SetCardData.from_gzip_bytes(_artifact().to_gzip_bytes(), max_decompressed_bytes=1)


def test_from_card_database_is_set_specific() -> None:
    database = CardDatabase(cards={1: _card(1, set_code="ABC"), 2: _card(2, set_code="xyz")})
    artifact = SetCardData.from_card_database(
        database,
        set_code="abc",
        set_name="Alpha",
    )
    assert [card.arena_id for card in artifact.cards] == [1]
    assert artifact.cards[0].set_code == "abc"
    with pytest.raises(SetCardDataError, match="no cards"):
        SetCardData.from_card_database(database, set_code="none", set_name="None")
