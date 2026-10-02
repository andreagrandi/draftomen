"""Strict canonical per-set Arena card metadata artifacts.
Validate one deterministic card-data object and rebuild the runtime lookup table.
"""

from __future__ import annotations

import gzip
import io
import json
import math
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Self

from draftomen.carddb import CardDatabase, CardFace, CardInfo


CARD_DATA_SCHEMA_VERSION = 1
CARD_DATA_SOURCE = "scryfall-default-cards"
CARD_DATA_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
_SET_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CARD_KEYS = frozenset(
    {
        "arena_id",
        "collector_number",
        "colors",
        "faces",
        "image_uri",
        "keywords",
        "layout",
        "mana_cost",
        "mana_value",
        "name",
        "oracle_id",
        "oracle_text",
        "power",
        "produced_mana",
        "rarity",
        "set_code",
        "subtypes",
        "toughness",
        "type_line",
        "types",
    }
)
_FACE_KEYS = frozenset(
    {
        "colors",
        "keywords",
        "mana_cost",
        "mana_value",
        "name",
        "oracle_text",
        "power",
        "produced_mana",
        "subtypes",
        "toughness",
        "type_line",
    }
)
_TOP_LEVEL_KEYS = frozenset(
    {"cards", "image_uris_by_name", "schema_version", "set_code", "set_name", "source"}
)


class SetCardDataError(ValueError):
    """Raised when a per-set card artifact is invalid or non-canonical."""


def _check_keys(
    value: Mapping[str, Any], expected: frozenset[str], label: str, *, strict: bool
) -> None:
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        raise SetCardDataError(f"{label} keys must be strings.")
    if not strict:
        # Unknown keys are ignored so published files can gain fields.
        keys &= expected
    if keys != expected:
        missing = sorted(expected - keys)
        unknown = sorted(keys - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise SetCardDataError(f"{label} has invalid keys ({'; '.join(details)}).")


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SetCardDataError(f"{field_name} must be a non-empty string.")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _set_code(value: Any, *, field_name: str = "set_code") -> str:
    value = _required_string(value, field_name)
    if _SET_CODE_RE.fullmatch(value) is None:
        raise SetCardDataError(
            f"{field_name} must be a lowercase path-safe set code."
        )
    return value


def _expected_set_code(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise SetCardDataError("expected set code must be a non-empty string.")
    return _set_code(value.casefold(), field_name="expected set code")


def _number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SetCardDataError(f"{field_name} must be a number.")
    try:
        result = float(value)
    except OverflowError as error:
        raise SetCardDataError(f"{field_name} must be finite.") from error
    if not math.isfinite(result):
        raise SetCardDataError(f"{field_name} must be finite.")
    return result


def _optional_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _number(value, field_name)


def _positive_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SetCardDataError(f"{field_name} must be a positive integer.")
    return value


def _string_array(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SetCardDataError(f"{field_name} must be an array of strings.")
    result: list[str] = []
    for item in value:
        result.append(_required_string(item, f"{field_name} item"))
    return tuple(result)


def _model_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise SetCardDataError(f"{field_name} must be a tuple of strings.")
    result: list[str] = []
    for item in value:
        result.append(_required_string(item, f"{field_name} item"))
    return tuple(result)


def _colors(value: Any, field_name: str) -> tuple[str, ...]:
    values = _string_array(value, field_name) if isinstance(value, list) else _model_string_tuple(value, field_name)
    invalid = [item for item in values if item not in {"W", "U", "B", "R", "G"}]
    if invalid:
        raise SetCardDataError(f"{field_name} has invalid color values: {invalid!r}.")
    return values


def _produced_mana(value: Any, field_name: str) -> tuple[str, ...]:
    values = _string_array(value, field_name) if isinstance(value, list) else _model_string_tuple(value, field_name)
    invalid = [item for item in values if item not in {"C", "W", "U", "B", "R", "G"}]
    if invalid:
        raise SetCardDataError(f"{field_name} has invalid mana values: {invalid!r}.")
    return values


def _normalized_name(value: str) -> str:
    return " ".join(value.casefold().replace("’", "'").split())


def _face_to_json(face: CardFace, *, field_name: str) -> dict[str, object]:
    if not isinstance(face, CardFace):
        raise SetCardDataError(f"{field_name} must be a CardFace.")
    name = _optional_string(face.name, f"{field_name}.name")
    oracle_text = _optional_string(face.oracle_text, f"{field_name}.oracle_text")
    keywords = _model_string_tuple(face.keywords, f"{field_name}.keywords")
    type_line = _optional_string(face.type_line, f"{field_name}.type_line")
    subtypes = _model_string_tuple(face.subtypes, f"{field_name}.subtypes")
    colors = _colors(face.colors, f"{field_name}.colors")
    mana_cost = _optional_string(face.mana_cost, f"{field_name}.mana_cost")
    mana_value = _optional_number(face.mana_value, f"{field_name}.mana_value")
    produced_mana = _produced_mana(face.produced_mana, f"{field_name}.produced_mana")
    power = _optional_string(face.power, f"{field_name}.power")
    toughness = _optional_string(face.toughness, f"{field_name}.toughness")
    return {
        "colors": list(colors),
        "keywords": list(keywords),
        "mana_cost": mana_cost,
        "mana_value": mana_value,
        "name": name,
        "oracle_text": oracle_text,
        "power": power,
        "produced_mana": list(produced_mana),
        "subtypes": list(subtypes),
        "toughness": toughness,
        "type_line": type_line,
    }


def _face_from_json(value: Any, *, field_name: str, strict: bool) -> CardFace:
    if not isinstance(value, Mapping):
        raise SetCardDataError(f"{field_name} must be an object.")
    _check_keys(value, _FACE_KEYS, field_name, strict=strict)
    return CardFace(
        name=_optional_string(value["name"], f"{field_name}.name"),
        oracle_text=_optional_string(value["oracle_text"], f"{field_name}.oracle_text"),
        keywords=_string_array(value["keywords"], f"{field_name}.keywords"),
        type_line=_optional_string(value["type_line"], f"{field_name}.type_line"),
        subtypes=_string_array(value["subtypes"], f"{field_name}.subtypes"),
        colors=_colors(value["colors"], f"{field_name}.colors"),
        mana_cost=_optional_string(value["mana_cost"], f"{field_name}.mana_cost"),
        mana_value=_optional_number(value["mana_value"], f"{field_name}.mana_value"),
        produced_mana=_produced_mana(value["produced_mana"], f"{field_name}.produced_mana"),
        power=_optional_string(value["power"], f"{field_name}.power"),
        toughness=_optional_string(value["toughness"], f"{field_name}.toughness"),
    )


def _card_to_json(card: CardInfo, *, set_code: str, index: int) -> dict[str, object]:
    if not isinstance(card, CardInfo):
        raise SetCardDataError(f"cards[{index}] must be a CardInfo.")
    arena_id = card.arena_id if card.arena_id is not None else card.grp_id
    arena_id = _positive_integer(arena_id, f"cards[{index}].arena_id")
    card_set_code = _set_code(card.set_code, field_name=f"cards[{index}].set_code")
    if card_set_code != set_code:
        raise SetCardDataError(
            f"cards[{index}] belongs to set {card_set_code!r}, not {set_code!r}."
        )
    if isinstance(card.grp_id, bool) or not isinstance(card.grp_id, int):
        raise SetCardDataError(f"cards[{index}].grp_id must be an integer.")
    name = _required_string(card.name, f"cards[{index}].name")
    collector_number = _optional_string(
        card.collector_number, f"cards[{index}].collector_number"
    )
    colors = _colors(card.colors, f"cards[{index}].colors")
    if not isinstance(card.faces, tuple):
        raise SetCardDataError(f"cards[{index}].faces must be a tuple of CardFace values.")
    faces = tuple(
        _face_to_json(face, field_name=f"cards[{index}].faces[{face_index}]")
        for face_index, face in enumerate(card.faces)
    )
    image_uri = _optional_string(card.image_uri, f"cards[{index}].image_uri")
    keywords = _model_string_tuple(card.keywords, f"cards[{index}].keywords")
    layout = _optional_string(card.layout, f"cards[{index}].layout")
    mana_cost = _optional_string(card.mana_cost, f"cards[{index}].mana_cost")
    mana_value = _optional_number(card.mana_value, f"cards[{index}].mana_value")
    oracle_id = _optional_string(card.oracle_id, f"cards[{index}].oracle_id")
    oracle_text = _optional_string(card.oracle_text, f"cards[{index}].oracle_text")
    power = _optional_string(card.power, f"cards[{index}].power")
    produced_mana = _produced_mana(card.produced_mana, f"cards[{index}].produced_mana")
    rarity = _required_string(card.rarity, f"cards[{index}].rarity")
    subtypes = _model_string_tuple(card.subtypes, f"cards[{index}].subtypes")
    toughness = _optional_string(card.toughness, f"cards[{index}].toughness")
    type_line = _optional_string(card.type_line, f"cards[{index}].type_line")
    types = _model_string_tuple(card.types, f"cards[{index}].types")
    return {
        "arena_id": arena_id,
        "collector_number": collector_number,
        "colors": list(colors),
        "faces": list(faces),
        "image_uri": image_uri,
        "keywords": list(keywords),
        "layout": layout,
        "mana_cost": mana_cost,
        "mana_value": mana_value,
        "name": name,
        "oracle_id": oracle_id,
        "oracle_text": oracle_text,
        "power": power,
        "produced_mana": list(produced_mana),
        "rarity": rarity,
        "set_code": set_code,
        "subtypes": list(subtypes),
        "toughness": toughness,
        "type_line": type_line,
        "types": list(types),
    }


def _card_from_json(value: Any, *, index: int, strict: bool) -> CardInfo:
    if not isinstance(value, Mapping):
        raise SetCardDataError(f"cards[{index}] must be an object.")
    _check_keys(value, _CARD_KEYS, f"cards[{index}]", strict=strict)
    arena_id = _positive_integer(value["arena_id"], f"cards[{index}].arena_id")
    faces_value = value["faces"]
    if not isinstance(faces_value, list):
        raise SetCardDataError(f"cards[{index}].faces must be an array.")
    faces = tuple(
        _face_from_json(
            face, field_name=f"cards[{index}].faces[{face_index}]", strict=strict
        )
        for face_index, face in enumerate(faces_value)
    )
    return CardInfo(
        grp_id=arena_id,
        name=_required_string(value["name"], f"cards[{index}].name"),
        colors=_colors(value["colors"], f"cards[{index}].colors"),
        mana_value=_optional_number(value["mana_value"], f"cards[{index}].mana_value"),
        rarity=_required_string(value["rarity"], f"cards[{index}].rarity"),
        types=_string_array(value["types"], f"cards[{index}].types"),
        mana_cost=_optional_string(value["mana_cost"], f"cards[{index}].mana_cost"),
        produced_mana=_produced_mana(value["produced_mana"], f"cards[{index}].produced_mana"),
        image_uri=_optional_string(value["image_uri"], f"cards[{index}].image_uri"),
        unknown=False,
        oracle_text=_optional_string(value["oracle_text"], f"cards[{index}].oracle_text"),
        keywords=_string_array(value["keywords"], f"cards[{index}].keywords"),
        type_line=_optional_string(value["type_line"], f"cards[{index}].type_line"),
        subtypes=_string_array(value["subtypes"], f"cards[{index}].subtypes"),
        layout=_optional_string(value["layout"], f"cards[{index}].layout"),
        faces=faces,
        set_code=_set_code(value["set_code"], field_name=f"cards[{index}].set_code"),
        collector_number=_optional_string(
            value["collector_number"], f"cards[{index}].collector_number"
        ),
        arena_id=arena_id,
        source_provenance=("scryfall",),
        power=_optional_string(value["power"], f"cards[{index}].power"),
        toughness=_optional_string(value["toughness"], f"cards[{index}].toughness"),
        oracle_id=_optional_string(value["oracle_id"], f"cards[{index}].oracle_id"),
    )


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return (serialized + "\n").encode("utf-8")
    except (TypeError, UnicodeError, ValueError, OverflowError) as error:
        raise SetCardDataError("Card data could not be serialized canonically.") from error


def _duplicate_checking_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SetCardDataError(f"Duplicate JSON object key {key!r}.")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> Any:
    raise SetCardDataError(f"JSON constant {value!r} is not allowed.")


def _decode_gzip(payload: bytes, *, max_decompressed_bytes: int) -> bytes:
    if isinstance(max_decompressed_bytes, bool) or not isinstance(max_decompressed_bytes, int):
        raise SetCardDataError("max_decompressed_bytes must be a positive integer.")
    if max_decompressed_bytes <= 0:
        raise SetCardDataError("max_decompressed_bytes must be a positive integer.")
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        raw = decompressor.decompress(payload, max_decompressed_bytes + 1)
        if len(raw) > max_decompressed_bytes:
            raise SetCardDataError("Card data gzip payload exceeds decompressed size limit.")
        if not decompressor.eof:
            raw += decompressor.flush(max_decompressed_bytes + 1 - len(raw))
        if len(raw) > max_decompressed_bytes:
            raise SetCardDataError("Card data gzip payload exceeds decompressed size limit.")
        if not decompressor.eof:
            raise SetCardDataError("Card data gzip payload is incomplete.")
        if decompressor.unused_data or decompressor.unconsumed_tail:
            raise SetCardDataError("Card data gzip payload has trailing data.")
        return raw
    except SetCardDataError:
        raise
    except (EOFError, OSError, zlib.error) as error:
        raise SetCardDataError("Card data gzip payload is invalid.") from error


def _deterministic_gzip(payload: bytes) -> bytes:

    output = io.BytesIO()
    try:
        with gzip.GzipFile(
            fileobj=output,
            mode="wb",
            filename="",
            mtime=0,
            compresslevel=9,
        ) as stream:
            stream.write(payload)
    except (OSError, TypeError, ValueError) as error:
        raise SetCardDataError("Card data gzip payload could not be serialized.") from error
    return output.getvalue()


@dataclass(frozen=True, slots=True)
class SetCardData:
    """Canonical Arena card metadata for one draft set.
    The object contains no draft statistics and reconstructs one CardDatabase.
    """

    set_code: str
    set_name: str
    cards: tuple[CardInfo, ...]
    image_uris_by_name: dict[str, str] = field(default_factory=dict)
    schema_version: int = CARD_DATA_SCHEMA_VERSION
    source: str = CARD_DATA_SOURCE

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise SetCardDataError("schema_version must be an integer.")
        if self.schema_version != CARD_DATA_SCHEMA_VERSION:
            raise SetCardDataError(
                f"Unsupported card data schema {self.schema_version}; "
                f"expected {CARD_DATA_SCHEMA_VERSION}."
            )
        if self.source != CARD_DATA_SOURCE:
            raise SetCardDataError("source must be scryfall-default-cards.")
        set_code = _set_code(self.set_code)
        set_name = _required_string(self.set_name, "set_name")
        if not isinstance(self.cards, (tuple, list)):
            raise SetCardDataError("cards must be a sequence of CardInfo values.")
        cards = tuple(self.cards)
        if not cards:
            raise SetCardDataError("cards must contain at least one card.")
        serialized_cards = tuple(
            _card_to_json(card, set_code=set_code, index=index)
            for index, card in enumerate(cards)
        )
        ids = tuple(item["arena_id"] for item in serialized_cards)
        if ids != tuple(sorted(ids)):
            raise SetCardDataError("cards must be sorted by ascending arena_id.")
        if len(set(ids)) != len(ids):
            raise SetCardDataError("cards must not contain duplicate arena_id values.")

        if not isinstance(self.image_uris_by_name, Mapping):
            raise SetCardDataError("image_uris_by_name must be an object.")
        image_uris: dict[str, str] = {}
        for name, image_uri in self.image_uris_by_name.items():
            if not isinstance(name, str) or not name:
                raise SetCardDataError("image_uris_by_name keys must be non-empty strings.")
            normalized_name = _normalized_name(name)
            if normalized_name != name:
                raise SetCardDataError(
                    f"image_uris_by_name key {name!r} is not normalized."
                )
            image_uris[name] = _required_string(image_uri, f"image_uris_by_name[{name!r}]")
        card_names = {
            _normalized_name(card.name)
            for card in cards
        }
        card_names.update(
            _normalized_name(face.name)
            for card in cards
            for face in card.faces
            if face.name is not None
        )
        unknown_names = sorted(set(image_uris) - card_names)
        if unknown_names:
            raise SetCardDataError(
                "image_uris_by_name contains names outside the declared set: "
                + ", ".join(unknown_names)
                + "."
            )
        object.__setattr__(self, "set_code", set_code)
        object.__setattr__(self, "set_name", set_name)
        object.__setattr__(self, "cards", cards)
        object.__setattr__(self, "image_uris_by_name", image_uris)

    def to_json(self) -> dict[str, object]:
        """Return the exact schema-versioned artifact object."""

        return {
            "cards": [
                _card_to_json(card, set_code=self.set_code, index=index)
                for index, card in enumerate(self.cards)
            ],
            "image_uris_by_name": dict(sorted(self.image_uris_by_name.items())),
            "schema_version": self.schema_version,
            "set_code": self.set_code,
            "set_name": self.set_name,
            "source": self.source,
        }

    def to_bytes(self) -> bytes:
        """Serialize this artifact as canonical UTF-8 JSON bytes."""

        return _canonical_json_bytes(self.to_json())

    def to_gzip_bytes(self) -> bytes:
        """Serialize canonical JSON bytes in a deterministic gzip container."""

        return _deterministic_gzip(self.to_bytes())

    def validate_expected_set(self, *, set_code: str, set_name: str | None = None) -> None:
        """Require an exact detected set code and optional set name."""

        expected_code = _expected_set_code(set_code)
        if self.set_code != expected_code:
            raise SetCardDataError(
                f"Card data belongs to set {self.set_code!r}, expected {expected_code!r}."
            )
        if set_name is not None and self.set_name != _required_string(set_name, "expected set_name"):
            raise SetCardDataError(
                f"Card data is named {self.set_name!r}, expected {set_name!r}."
            )

    @classmethod
    def from_json(
        cls,
        data: Mapping[str, Any],
        *,
        expected_set_code: str | None = None,
        expected_set_name: str | None = None,
        strict: bool = True,
    ) -> Self:
        """Parse one artifact object and check every value.
        Strict parsing rejects unknown keys; otherwise they are ignored.
        """

        if not isinstance(data, Mapping):
            raise SetCardDataError("Card data must be a JSON object.")
        _check_keys(data, _TOP_LEVEL_KEYS, "card data", strict=strict)
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise SetCardDataError("schema_version must be an integer.")
        if schema_version != CARD_DATA_SCHEMA_VERSION:
            raise SetCardDataError(
                f"Unsupported card data schema {schema_version}; "
                f"expected {CARD_DATA_SCHEMA_VERSION}."
            )
        if data["source"] != CARD_DATA_SOURCE:
            raise SetCardDataError("source must be scryfall-default-cards.")
        cards_value = data["cards"]
        if not isinstance(cards_value, list):
            raise SetCardDataError("cards must be an array.")
        cards = tuple(
            _card_from_json(card, index=index, strict=strict)
            for index, card in enumerate(cards_value)
        )
        image_value = data["image_uris_by_name"]
        if not isinstance(image_value, Mapping):
            raise SetCardDataError("image_uris_by_name must be an object.")
        image_uris: dict[str, str] = {}
        for name, image_uri in image_value.items():
            if not isinstance(name, str):
                raise SetCardDataError("image_uris_by_name keys must be strings.")
            image_uris[name] = _required_string(image_uri, f"image_uris_by_name[{name!r}]")
        result = cls(
            set_code=data["set_code"],
            set_name=data["set_name"],
            cards=cards,
            image_uris_by_name=image_uris,
            schema_version=schema_version,
            source=data["source"],
        )
        if expected_set_code is not None:
            result.validate_expected_set(
                set_code=expected_set_code,
                set_name=expected_set_name,
            )
        elif expected_set_name is not None:
            raise SetCardDataError("expected_set_name requires expected_set_code.")
        return result

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        expected_set_code: str | None = None,
        expected_set_name: str | None = None,
        strict: bool = True,
    ) -> Self:
        """Parse raw bytes and reject malformed JSON.
        Strict parsing also rejects unknown keys and non-canonical JSON.
        """

        if not isinstance(payload, bytes):
            raise SetCardDataError("Card data bytes must be bytes.")
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_duplicate_checking_object,
                parse_constant=_reject_json_constant,
            )
        except SetCardDataError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise SetCardDataError("Card data JSON could not be parsed.") from error
        result = cls.from_json(
            value,
            expected_set_code=expected_set_code,
            expected_set_name=expected_set_name,
            strict=strict,
        )
        if strict and payload != result.to_bytes():
            raise SetCardDataError("Card data JSON is not canonical.")
        return result

    @classmethod
    def from_gzip_bytes(
        cls,
        payload: bytes,
        *,
        max_decompressed_bytes: int = CARD_DATA_MAX_DECOMPRESSED_BYTES,
        expected_set_code: str | None = None,
        expected_set_name: str | None = None,
        strict: bool = True,
    ) -> Self:
        """Parse a bounded gzip artifact.
        Strict parsing also rejects unknown keys and non-canonical containers.
        """

        if not isinstance(payload, bytes):
            raise SetCardDataError("Card data gzip bytes must be bytes.")
        raw = _decode_gzip(payload, max_decompressed_bytes=max_decompressed_bytes)
        result = cls.from_bytes(
            raw,
            expected_set_code=expected_set_code,
            expected_set_name=expected_set_name,
            strict=strict,
        )
        if strict and payload != result.to_gzip_bytes():
            raise SetCardDataError("Card data gzip bytes are not canonical.")
        return result

    @classmethod
    def from_card_database(
        cls,
        database: CardDatabase,
        *,
        set_code: str,
        set_name: str,
    ) -> Self:
        """Build one set-specific artifact from an existing CardDatabase."""

        if not isinstance(database, CardDatabase):
            raise SetCardDataError("database must be a CardDatabase.")
        if not isinstance(set_code, str) or not set_code:
            raise SetCardDataError("set_code must be a non-empty string.")
        normalized_code = _set_code(set_code.casefold())
        selected = [
            replace(card, set_code=normalized_code)
            for card in database.cards.values()
            if isinstance(card, CardInfo)
            and isinstance(card.set_code, str)
            and card.set_code
            and card.set_code.casefold() == normalized_code
        ]
        if not selected:
            raise SetCardDataError(f"Card database has no cards for set {normalized_code!r}.")
        validated = tuple(
            (_card_to_json(card, set_code=normalized_code, index=index), card)
            for index, card in enumerate(selected)
        )
        selected = [
            card
            for _, card in sorted(validated, key=lambda item: item[0]["arena_id"])
        ]
        card_names = {_normalized_name(card.name) for card in selected}
        card_names.update(
            _normalized_name(face.name)
            for card in selected
            for face in card.faces
            if face.name is not None
        )
        if not isinstance(database.image_uris_by_name, Mapping):
            raise SetCardDataError("database image_uris_by_name must be an object.")
        image_uris: dict[str, str] = {}
        for name, image_uri in sorted(
            database.image_uris_by_name.items(),
            key=lambda item: repr(item[0]),
        ):
            if not isinstance(name, str) or not isinstance(image_uri, str) or not image_uri:
                continue
            normalized_name = _normalized_name(name)
            if normalized_name in card_names:
                image_uris.setdefault(normalized_name, image_uri)
        for card in selected:
            image_uri = card.image_uri
            if not isinstance(image_uri, str) or not image_uri:
                continue
            for name in (
                card.name,
                *(face.name for face in card.faces if face.name is not None),
            ):
                image_uris.setdefault(_normalized_name(name), image_uri)
        return cls(
            set_code=normalized_code,
            set_name=set_name,
            cards=tuple(selected),
            image_uris_by_name=image_uris,
        )

    def to_card_database(self) -> CardDatabase:
        """Rebuild the existing lookup model with deterministic source metadata."""

        cards = {
            card.arena_id if card.arena_id is not None else card.grp_id: CardInfo(
                grp_id=card.arena_id if card.arena_id is not None else card.grp_id,
                name=card.name,
                colors=card.colors,
                mana_value=card.mana_value,
                rarity=card.rarity,
                types=card.types,
                mana_cost=card.mana_cost,
                produced_mana=card.produced_mana,
                image_uri=card.image_uri,
                unknown=False,
                oracle_text=card.oracle_text,
                keywords=card.keywords,
                type_line=card.type_line,
                subtypes=card.subtypes,
                layout=card.layout,
                faces=card.faces,
                set_code=self.set_code,
                collector_number=card.collector_number,
                arena_id=card.arena_id if card.arena_id is not None else card.grp_id,
                source_provenance=("scryfall",),
                power=card.power,
                toughness=card.toughness,
                oracle_id=card.oracle_id,
            )
            for card in self.cards
        }
        return CardDatabase(cards=cards, image_uris_by_name=dict(self.image_uris_by_name))
