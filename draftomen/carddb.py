"""Local card metadata cache backed by Scryfall and Arena data.
Translate Arena grpIds into display-ready card facts for replay and scoring.
"""

from __future__ import annotations

import gzip
import http.client
import io
import json
import os
import platform
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import Any, TypeAlias

from draftomen import __version__
from draftomen.paths import app_data_dir

PathInput: TypeAlias = str | PathLike[str]

CARD_DATABASE_CACHE_FILENAME = "carddb.json"
SCRYFALL_BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
SCRYFALL_DEFAULT_CARDS_TYPE = "default_cards"
MTGJSON_SET_URL_TEMPLATE = "https://mtgjson.com/api/v5/{set_code}.json"
SCRYFALL_USER_AGENT = (
    f"draftomen/{__version__} "
    "(+https://github.com/andreagrandi/draftomen)"
)
ARENA_DATA_CARDS_PREFIX = "data_cards"
ARENA_DATA_LOC_PREFIX = "data_loc"
ARENA_DATA_FILE_SUFFIXES = (".mtga", ".json", ".js")
ARENA_PRODUCED_MANA_FIELDS = (
    "produced_mana",
    "producedMana",
    "producesMana",
    "manaProduced",
)
HTTP_TIMEOUT_SECONDS = 60
_BULK_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
COLOR_ORDER = ("W", "U", "B", "R", "G")
ARENA_COLOR_ID_MAP = {1: "W", 2: "U", 3: "B", 4: "R", 5: "G"}
ARENA_RARITY_ID_MAP = {
    0: "token",
    1: "basic",
    2: "common",
    3: "uncommon",
    4: "rare",
    5: "mythic",
}
CACHE_SCHEMA_VERSION = 5
UNKNOWN_SOURCE_PROVENANCE = "unknown"


@dataclass(frozen=True, slots=True)
class CardFace:
    """Normalized metadata for one card face.
    Missing source values stay explicit instead of being inferred.
    """

    name: str | None = None
    oracle_text: str | None = None
    keywords: tuple[str, ...] = ()
    type_line: str | None = None
    subtypes: tuple[str, ...] = ()
    colors: tuple[str, ...] = ()
    mana_cost: str | None = None
    mana_value: float | None = None
    produced_mana: tuple[str, ...] = ()
    power: str | None = None
    toughness: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CardFace:
        """Load one normalized face from cache JSON."""

        return cls(
            name=_optional_str(data.get("name"), field_name="face.name"),
            oracle_text=_optional_str(
                data.get("oracle_text"),
                field_name="face.oracle_text",
            ),
            keywords=_string_tuple(
                data.get("keywords", ()),
                field_name="face.keywords",
            ),
            type_line=_optional_str(
                data.get("type_line"),
                field_name="face.type_line",
            ),
            subtypes=_string_tuple(
                data.get("subtypes", ()),
                field_name="face.subtypes",
            ),
            colors=_string_tuple(data.get("colors", ()), field_name="face.colors"),
            mana_cost=_optional_str(
                data.get("mana_cost"),
                field_name="face.mana_cost",
            ),
            mana_value=_optional_float(
                data.get("mana_value"),
                field_name="face.mana_value",
            ),
            produced_mana=_string_tuple(
                data.get("produced_mana", ()),
                field_name="face.produced_mana",
            ),
            power=_optional_str(data.get("power"), field_name="face.power"),
            toughness=_optional_str(
                data.get("toughness"),
                field_name="face.toughness",
            ),
        )

    def to_json(self) -> dict[str, object]:
        """Convert one normalized face to deterministic cache JSON."""

        return {
            "colors": list(self.colors),
            "keywords": list(self.keywords),
            "mana_cost": self.mana_cost,
            "mana_value": self.mana_value,
            "name": self.name,
            "oracle_text": self.oracle_text,
            "power": self.power,
            "produced_mana": list(self.produced_mana),
            "subtypes": list(self.subtypes),
            "toughness": self.toughness,
            "type_line": self.type_line,
        }


class CardDatabaseError(RuntimeError):
    """Base error for card database load, refresh, and parse failures.
    Callers can catch this to show concise CLI diagnostics.
    """


class CardDatabaseCacheMissingError(CardDatabaseError):
    """Raised when the card database cache has not been built yet.
    Run refresh-data once before relying on fully offline lookups.
    """


class CardDatabaseCacheStaleError(CardDatabaseError):
    """Raised when the card cache schema needs a refresh.
    Watch mode can rebuild it automatically from Scryfall bulk data.
    """


@dataclass(frozen=True, slots=True)
class CardInfo:
    """Display metadata for one Arena card id.
    Unknown markers use the same shape so callers never crash on misses.
    """

    grp_id: int
    name: str
    colors: tuple[str, ...]
    mana_value: float | None
    rarity: str
    types: tuple[str, ...]
    mana_cost: str | None = None
    produced_mana: tuple[str, ...] = ()
    image_uri: str | None = None
    unknown: bool = False
    oracle_text: str | None = None
    keywords: tuple[str, ...] = ()
    type_line: str | None = None
    subtypes: tuple[str, ...] = ()
    layout: str | None = None
    faces: tuple[CardFace, ...] = ()
    set_code: str | None = None
    collector_number: str | None = None
    arena_id: int | None = None
    source_provenance: tuple[str, ...] = ()
    power: str | None = None
    toughness: str | None = None
    oracle_id: str | None = None

    @classmethod
    def unknown_card(cls, *, grp_id: int) -> CardInfo:
        """Build an explicit unknown-card marker.
        This keeps UI and replay paths total over arbitrary grpIds.
        """

        return cls(
            grp_id=grp_id,
            name=f"Unknown card {grp_id}",
            colors=(),
            mana_value=None,
            rarity="unknown",
            types=("Unknown",),
            unknown=True,
        )

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CardInfo:
        """Load a card entry from Draftomen's cache format.
        Cache parsing is strict so corrupted files fail loudly.
        """

        faces_value = data.get("faces", ())
        if not isinstance(faces_value, (list, tuple)):
            raise CardDatabaseError("Missing or invalid card.faces; expected object list.")

        faces: list[CardFace] = []
        for value in faces_value:
            if not isinstance(value, dict):
                raise CardDatabaseError("Missing or invalid card.faces; expected objects.")
            faces.append(CardFace.from_json(data=value))

        provenance = _string_tuple(
            data.get("source_provenance", ()),
            field_name="card.source_provenance",
        )
        set_code_value = data.get("set_code", data.get("set"))
        arena_id_value = data.get("arena_id", data.get("grp_id"))
        return cls(
            grp_id=_required_int(data.get("grp_id"), field_name="card.grp_id"),
            name=_required_str(data.get("name"), field_name="card.name"),
            colors=_string_tuple(data.get("colors"), field_name="card.colors"),
            mana_value=_optional_float(
                data.get("mana_value"),
                field_name="card.mana_value",
            ),
            rarity=_required_str(data.get("rarity"), field_name="card.rarity"),
            types=_string_tuple(data.get("types"), field_name="card.types"),
            mana_cost=_optional_str(data.get("mana_cost"), field_name="card.mana_cost"),
            produced_mana=_string_tuple(
                data.get("produced_mana", ()),
                field_name="card.produced_mana",
            ),
            image_uri=_optional_str(data.get("image_uri"), field_name="card.image_uri"),
            unknown=bool(data.get("unknown", False)),
            oracle_text=_optional_str(
                data.get("oracle_text"),
                field_name="card.oracle_text",
            ),
            keywords=_string_tuple(
                data.get("keywords", ()),
                field_name="card.keywords",
            ),
            type_line=_optional_str(data.get("type_line"), field_name="card.type_line"),
            subtypes=_string_tuple(
                data.get("subtypes", ()),
                field_name="card.subtypes",
            ),
            layout=_optional_str(data.get("layout"), field_name="card.layout"),
            faces=tuple(faces),
            set_code=_optional_str(set_code_value, field_name="card.set"),
            collector_number=_optional_str(
                data.get("collector_number"),
                field_name="card.collector_number",
            ),
            arena_id=(
                None
                if arena_id_value is None
                else _required_int(arena_id_value, field_name="card.arena_id")
            ),
            oracle_id=_optional_str(
                data.get("oracle_id"),
                field_name="card.oracle_id",
            ),
            source_provenance=provenance,
            power=_optional_str(data.get("power"), field_name="card.power"),
            toughness=_optional_str(
                data.get("toughness"),
                field_name="card.toughness",
            ),
        )

    def to_json(self) -> dict[str, object]:
        """Convert this card entry to Draftomen's cache format.
        The result intentionally stores only normalized fields.
        """

        return {
            "arena_id": self.arena_id,
            "collector_number": self.collector_number,
            "colors": list(self.colors),
            "faces": [face.to_json() for face in self.faces],
            "grp_id": self.grp_id,
            "image_uri": self.image_uri,
            "keywords": list(self.keywords),
            "layout": self.layout,
            "mana_cost": self.mana_cost,
            "mana_value": self.mana_value,
            "name": self.name,
            "oracle_text": self.oracle_text,
            "oracle_id": self.oracle_id,
            "power": self.power,
            "produced_mana": list(self.produced_mana),
            "rarity": self.rarity,
            "set": self.set_code,
            "source_provenance": list(self.source_provenance),
            "subtypes": list(self.subtypes),
            "toughness": self.toughness,
            "type_line": self.type_line,
            "types": list(self.types),
            "unknown": self.unknown,
        }


@dataclass(frozen=True, slots=True)
class CardMetadataSeed:
    """Set-scoped external metadata used to bridge grpIds to card names.
    17Lands supplies these rows before Scryfall exposes arena_id values.
    """

    grp_id: int
    name: str
    colors: tuple[str, ...]
    rarity: str


@dataclass(frozen=True, slots=True)
class CardDatabase:
    """Lookup table from Arena grpId to card metadata.
    Missing grpIds return explicit unknown markers instead of raising.
    """

    cards: dict[int, CardInfo]
    image_uris_by_name: dict[str, str] = field(default_factory=dict)
    generated_at: datetime | None = None

    def __len__(self) -> int:
        return len(self.cards)

    def lookup(self, *, grp_id: int) -> CardInfo:
        """Return card metadata or an explicit unknown marker.
        Lookup never raises for absent ids.
        """

        return self.cards.get(grp_id, CardInfo.unknown_card(grp_id=grp_id))

    def image_uri_for_name(self, *, name: str) -> str | None:
        """Return a cached Scryfall image URI by normalized card name.
        This avoids per-card Scryfall API lookups while browsing in the TUI.
        """

        return self.image_uris_by_name.get(_normalized_card_name(name=name))

    def unresolved_grp_ids(self, *, grp_ids: Iterable[int]) -> tuple[int, ...]:
        """Return unique ids that still resolve to unknown markers.
        UI code uses this to warn when metadata coverage is incomplete.
        """

        seen: set[int] = set()
        unresolved: list[int] = []
        for grp_id in grp_ids:
            if grp_id in seen:
                continue

            seen.add(grp_id)
            if self.lookup(grp_id=grp_id).unknown:
                unresolved.append(grp_id)

        return tuple(unresolved)

    def to_json(self) -> dict[str, object]:
        """Convert the database to Draftomen's cache format.
        Cards are sorted by grpId for stable cache diffs.
        """

        return {
            "schema_version": CACHE_SCHEMA_VERSION,
            "source": "scryfall-default-cards",
            "generated_at": _utc_isoformat(value=self.generated_at),
            "cards": {
                str(grp_id): card.to_json()
                for grp_id, card in sorted(self.cards.items())
            },
            "image_uris_by_name": dict(sorted(self.image_uris_by_name.items())),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CardDatabase:
        """Load a card database from Draftomen's cache format.
        Cache schema mismatches fail before any partial lookup is used.
        """

        schema_version = _required_int(
            data.get("schema_version"),
            field_name="schema_version",
        )
        if schema_version not in {CACHE_SCHEMA_VERSION, 4, 3}:
            raise CardDatabaseCacheStaleError(
                "Unsupported card database cache schema "
                f"{schema_version}; expected {CACHE_SCHEMA_VERSION}."
            )

        cards_value = data.get("cards")
        if not isinstance(cards_value, dict):
            raise CardDatabaseError("Card database cache is missing cards object.")

        cards: dict[int, CardInfo] = {}
        for key, value in cards_value.items():
            grp_id = _required_int(key, field_name="cards key")
            if not isinstance(value, dict):
                raise CardDatabaseError(f"Card cache entry {key!r} is not an object.")
            card = CardInfo.from_json(data=value)
            if schema_version == 3 and not card.source_provenance:
                card = replace(
                    card,
                    source_provenance=(UNKNOWN_SOURCE_PROVENANCE,),
                )
            if card.grp_id != grp_id:
                raise CardDatabaseError(
                    f"Card cache key {grp_id} does not match entry grp_id {card.grp_id}."
                )

            cards[grp_id] = card

        return cls(
            cards=cards,
            image_uris_by_name=_image_uris_by_name_from_json(data=data),
            generated_at=_optional_datetime(data.get("generated_at")),
        )


def _image_uris_by_name_from_json(*, data: Mapping[str, Any]) -> dict[str, str]:
    value = data.get("image_uris_by_name", {})
    if not isinstance(value, dict):
        raise CardDatabaseError("Card database cache image index is not an object.")

    image_uris: dict[str, str] = {}
    for key, uri in value.items():
        name = _required_str(key, field_name="image_uris_by_name key")
        image_uri = _required_str(uri, field_name=f"image URI for {name}")
        image_uris[_normalized_card_name(name=name)] = image_uri

    return image_uris


def card_database_cache_path(*, app_dir: PathInput | None = None) -> Path:
    """Return the default on-disk card database cache path.
    The parent directory is not created until a refresh writes the cache.
    """

    root = Path(app_data_dir() if app_dir is None else app_dir)
    return root / CARD_DATABASE_CACHE_FILENAME


def load_card_database(
    *,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
) -> CardDatabase:
    """Load the card database cache without making network calls.
    This is the fully offline path used after refresh-data has run once.
    """

    path = _cache_path(app_dir=app_dir, cache_path=cache_path)
    if not path.exists():
        raise CardDatabaseCacheMissingError(
            f"Card database cache does not exist at {path}. Run refresh-data first."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CardDatabaseError(f"Malformed card database cache {path}: {error}") from error

    if not isinstance(data, dict):
        raise CardDatabaseError(f"Malformed card database cache {path}: expected object.")

    return CardDatabase.from_json(data=data)


def save_card_database(
    database: CardDatabase,
    *,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
) -> Path:
    """Write a card database cache atomically.
    The destination parent directory is created if needed.
    """

    path = _cache_path(app_dir=app_dir, cache_path=cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            database.to_json(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
        ) as temporary_file:
            temporary_name = temporary_file.name
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)

    return path


def refresh_card_database(
    *,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
    bulk_file: PathInput | None = None,
    arena_data_dir: PathInput | None = None,
    allow_arena_fallback: bool = True,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> CardDatabase:
    """Build a grpId map from Scryfall and Arena local data.
    Successful Scryfall refreshes atomically replace the canonical cache. Runtime
    callers may use an Arena-only fallback without overwriting it; cache-building
    callers can reject that non-cacheable result.
    Passing bulk_file keeps tests and local fixtures completely offline.
    """

    cacheable = True
    if bulk_file is None:
        database, cacheable = _download_or_arena_card_database(
            arena_data_dir=arena_data_dir,
            timeout_seconds=timeout_seconds,
        )
    else:
        database = build_card_database_from_bulk_file(path=bulk_file)
        if arena_data_dir is not None:
            database = augment_card_database_with_arena_data(
                database,
                arena_data_dir=arena_data_dir,
            )

    if not cacheable and not allow_arena_fallback:
        raise CardDatabaseError(
            "Scryfall refresh did not produce a cacheable card metadata result."
        )
    if cacheable:
        database = replace(database, generated_at=datetime.now(tz=UTC))
        save_card_database(database, app_dir=app_dir, cache_path=cache_path)
    return database


def load_or_refresh_card_database(
    *,
    app_dir: PathInput | None = None,
    cache_path: PathInput | None = None,
    arena_data_dir: PathInput | None = None,
    refresh: bool = False,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> CardDatabase:
    """Load cached card data, refreshing only when explicitly requested.
    A missing cache triggers a runtime refresh, which may use uncached local
    Arena metadata when Scryfall is unavailable.
    """

    if refresh:
        return refresh_card_database(
            app_dir=app_dir,
            cache_path=cache_path,
            arena_data_dir=arena_data_dir,
            timeout_seconds=timeout_seconds,
        )

    try:
        database = load_card_database(app_dir=app_dir, cache_path=cache_path)
    except (CardDatabaseCacheMissingError, CardDatabaseCacheStaleError):
        return refresh_card_database(
            app_dir=app_dir,
            cache_path=cache_path,
            arena_data_dir=arena_data_dir,
            timeout_seconds=timeout_seconds,
        )

    return augment_card_database_with_arena_data(
        database,
        arena_data_dir=arena_data_dir,
    )


def download_scryfall_card_database(
    *,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> CardDatabase:
    """Download Scryfall's default-cards bulk file and build a database.
    Scryfall does not require an API key, only normal API headers.
    """

    return build_card_database_from_scryfall_cards(
        cards=iter_scryfall_default_cards(timeout_seconds=timeout_seconds)
    )


def iter_scryfall_default_cards(
    *,
    bulk_file: PathInput | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> Iterator[Mapping[str, Any]]:
    """Yield Scryfall's default-card records from one local or remote source.

    Local JSONL (including ``.jsonl.gz``) files are entirely offline.  The
    remote path performs exactly one bulk metadata request and one compressed
    default-cards download, preserving the streaming behavior of the original
    downloader.
    """

    if bulk_file is not None:
        path = Path(bulk_file)
        with _open_text_bulk_file(path=path) as source:
            yield from _iter_jsonl_objects(lines=source, source=str(path))
        return

    bulk_items = _fetch_bulk_data_items(timeout_seconds=timeout_seconds)
    download_uri = _default_cards_download_uri(bulk_items=bulk_items)
    yield from _iter_scryfall_jsonl_url(
        url=download_uri,
        timeout_seconds=timeout_seconds,
    )


def download_scryfall_default_cards_bulk_file(
    *,
    destination: PathInput,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download Scryfall's default-cards bulk source into one atomically replaced path.
    The compressed JSONL streams through a sibling temporary file, so a failed
    download never replaces an existing source and never installs a partial file.
    """

    target = Path(destination).expanduser()
    download_uri = _default_cards_download_uri(
        bulk_items=_fetch_bulk_data_items(timeout_seconds=timeout_seconds)
    )
    request = _request(url=download_uri)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response, tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as temporary:
            temporary_name = temporary.name
            total = _response_content_length(response=response)
            completed = 0
            while True:
                if should_stop is not None and should_stop():
                    raise CardDatabaseError("Scryfall default-cards download was cancelled.")
                chunk = response.read(_BULK_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                temporary.write(chunk)
                completed += len(chunk)
                if progress is not None:
                    progress(completed, total)
            if total is not None and completed != total:
                raise CardDatabaseError(
                    "Failed to download Scryfall default-cards bulk data: "
                    f"expected {total} bytes, received {completed}."
                )
            temporary.flush()
            os.fsync(temporary.fileno())
        _require_gzip_bulk(path=Path(temporary_name))
        os.replace(temporary_name, target)
        temporary_name = None
    except CardDatabaseError:
        raise
    except (OSError, http.client.HTTPException) as error:
        raise CardDatabaseError(
            f"Failed to download Scryfall default-cards bulk data: {error}"
        ) from error
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return target


def build_card_database_from_bulk_file(*, path: PathInput) -> CardDatabase:
    """Build the card database from a local Scryfall JSONL file.
    Both plain .jsonl and Scryfall-style .jsonl.gz files are supported.
    """

    bulk_path = Path(path)
    with _open_text_bulk_file(path=bulk_path) as bulk_file:
        return build_card_database_from_scryfall_cards(
            cards=_iter_jsonl_objects(lines=bulk_file, source=str(bulk_path))
        )


def build_card_database_from_scryfall_cards(
    *,
    cards: Iterable[Mapping[str, Any]],
) -> CardDatabase:
    """Build the grpId map from Scryfall card objects.
    Cards without arena_id are intentionally ignored.
    """

    database_cards: dict[int, CardInfo] = {}
    image_uris_by_name: dict[str, str] = {}
    for card_object in cards:
        _add_scryfall_image_uri_entries(
            card=card_object,
            image_uris_by_name=image_uris_by_name,
        )
        card = _card_info_from_scryfall(card=card_object)
        if card is None:
            continue

        database_cards[card.grp_id] = card

    return CardDatabase(cards=database_cards, image_uris_by_name=image_uris_by_name)


def build_card_database_from_arena_data_dir(*, path: PathInput) -> CardDatabase:
    """Build card metadata from MTG Arena's local data_cards/data_loc files.
    This covers day-one Arena grpIds before Scryfall publishes arena_id values.
    """

    data_dir = Path(path).expanduser()
    cards_path, loc_path = _arena_data_file_pair(path=data_dir, required=True)
    card_objects = _load_arena_json_array(path=cards_path, label="cards")
    loc_objects = _load_arena_json_array(path=loc_path, label="localization")
    localization = _arena_localization_map(items=loc_objects, source=str(loc_path))
    return build_card_database_from_arena_cards(
        cards=card_objects,
        localization=localization,
    )


def build_card_database_from_arena_cards(
    *,
    cards: Iterable[Mapping[str, Any]],
    localization: Mapping[int, str],
) -> CardDatabase:
    """Build the grpId map from Arena local card objects.
    Arena local data is authoritative for the client grpIds present in logs.
    """

    card_objects = tuple(cards)
    cards_by_grp_id: dict[int, Mapping[str, Any]] = {}
    for card_object in card_objects:
        grp_id_value = card_object.get("grpid", card_object.get("grpId"))
        if grp_id_value is None:
            continue

        grp_id = _required_int(grp_id_value, field_name="Arena card.grpid")
        cards_by_grp_id[grp_id] = card_object

    database_cards: dict[int, CardInfo] = {}
    for card_object in card_objects:
        card = _card_info_from_arena(
            card=card_object,
            localization=localization,
            cards_by_grp_id=cards_by_grp_id,
        )
        if card is None:
            continue

        database_cards[card.grp_id] = card

    return CardDatabase(cards=database_cards)


def find_default_arena_data_dir() -> Path | None:
    """Return the first default MTG Arena data dir with card metadata files.
    The function is best-effort and returns None when Arena is not installed.
    """

    for candidate in _default_arena_data_dir_candidates():
        if _arena_data_file_pair(path=candidate, required=False) is not None:
            return candidate

    return None


def augment_card_database_with_arena_data(
    database: CardDatabase,
    *,
    arena_data_dir: PathInput | None = None,
) -> CardDatabase:
    """Overlay local Arena card data when it is available.
    Local data wins because it is the source of grpIds emitted by Player.log.
    """

    arena_database = _load_arena_card_database_if_available(
        arena_data_dir=arena_data_dir,
    )
    if arena_database is None:
        return database

    return _merge_card_databases(base=database, overlay=arena_database)


def augment_card_database_with_mtgjson_set(
    database: CardDatabase,
    *,
    set_code: str,
    seeds: Iterable[CardMetadataSeed],
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    mtgjson_cards: Iterable[Mapping[str, Any]] | None = None,
) -> CardDatabase:
    """Augment missing metadata from MTGJSON set data.
    Existing Scryfall fields remain canonical during augmentation.
    """
    augmentation_seeds = tuple(
        seed
        for seed in seeds
        if _card_needs_mtgjson_augmentation(
            database=database,
            grp_id=seed.grp_id,
        )
    )
    if not augmentation_seeds:
        return database

    if mtgjson_cards is None:
        card_objects = tuple(
            download_mtgjson_set_cards(
                set_code=set_code,
                timeout_seconds=timeout_seconds,
            )
        )
    else:
        card_objects = tuple(mtgjson_cards)
    cards_by_uuid = _mtgjson_cards_by_uuid(cards=card_objects)
    cards_by_name = _mtgjson_cards_by_name(cards=card_objects)
    card_indices = {id(card): index for index, card in enumerate(card_objects)}
    cards = dict(database.cards)
    inferred_offset = _mtgjson_arena_id_offset(
        seeds=tuple(
            seed for seed in augmentation_seeds
            if database.lookup(grp_id=seed.grp_id).unknown
        ),
        cards_by_name=cards_by_name,
        card_indices=card_indices,
    )
    if inferred_offset is not None:
        _add_mtgjson_cards_by_inferred_arena_order(
            cards=cards,
            card_objects=card_objects,
            cards_by_uuid=cards_by_uuid,
            arena_id_offset=inferred_offset,
        )

    for seed in augmentation_seeds:
        mtgjson_card = _mtgjson_card_for_seed(seed=seed, cards_by_name=cards_by_name)
        if mtgjson_card is None:
            if seed.grp_id not in cards:
                cards[seed.grp_id] = _card_info_from_metadata_seed(seed=seed)
            continue

        augmented = _card_info_from_mtgjson(
            card=mtgjson_card,
            seed=seed,
            cards_by_uuid=cards_by_uuid,
        )
        existing = cards.get(seed.grp_id)
        cards[seed.grp_id] = (
            augmented
            if existing is None
            else _merge_card_info(base=existing, overlay=augmented)
        )

    return replace(database, cards=cards)


def _card_needs_mtgjson_augmentation(
    *,
    database: CardDatabase,
    grp_id: int,
) -> bool:
    card = database.cards.get(grp_id)
    if card is None:
        return True
    if "mtgjson" in card.source_provenance:
        return False
    if card.unknown:
        return True
    if UNKNOWN_SOURCE_PROVENANCE in card.source_provenance:
        return False

    return (
        card.oracle_text is None
        or card.type_line is None
        or card.mana_cost is None
    )


def download_mtgjson_set_cards(
    *,
    set_code: str,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> tuple[Mapping[str, Any], ...]:
    """Download one MTGJSON set file and return its card objects.
    MTGJSON includes current-set card names, mana values, and type lines.
    """

    url = MTGJSON_SET_URL_TEMPLATE.format(
        set_code=urllib.parse.quote(set_code.upper()),
    )
    request = _request(url=url)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise CardDatabaseError(f"Failed to query MTGJSON set metadata: {error}") from error
    except json.JSONDecodeError as error:
        raise CardDatabaseError(f"Malformed MTGJSON set metadata: {error}") from error

    if not isinstance(payload, dict):
        raise CardDatabaseError("Malformed MTGJSON set metadata: expected object.")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise CardDatabaseError("Malformed MTGJSON set metadata: missing data object.")

    cards_value = data.get("cards")
    if not isinstance(cards_value, list):
        raise CardDatabaseError("Malformed MTGJSON set metadata: missing cards list.")

    cards: list[Mapping[str, Any]] = []
    for item in cards_value:
        if not isinstance(item, dict):
            raise CardDatabaseError("Malformed MTGJSON set metadata: card is not object.")

        cards.append(item)

    return tuple(cards)


def _mtgjson_cards_by_uuid(
    *,
    cards: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    cards_by_uuid: dict[str, Mapping[str, Any]] = {}
    for card in cards:
        uuid = card.get("uuid")
        if isinstance(uuid, str) and uuid:
            cards_by_uuid[uuid] = card

    return cards_by_uuid


def _mtgjson_cards_by_name(
    *,
    cards: Iterable[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    cards_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for card in cards:
        for name in _mtgjson_card_names(card=card):
            cards_by_name.setdefault(_normalized_card_name(name=name), []).append(card)

    return cards_by_name


def _mtgjson_card_names(*, card: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    name = card.get("name")
    if isinstance(name, str) and name:
        names.append(name)

    face_name = card.get("faceName")
    if isinstance(face_name, str) and face_name:
        names.append(face_name)

    return tuple(dict.fromkeys(names))


def _mtgjson_card_for_seed(
    *,
    seed: CardMetadataSeed,
    cards_by_name: Mapping[str, list[Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    candidates = cards_by_name.get(_normalized_card_name(name=seed.name), [])
    if not candidates:
        return None

    return min(candidates, key=_mtgjson_card_sort_key)


def _mtgjson_arena_id_offset(
    *,
    seeds: Iterable[CardMetadataSeed],
    cards_by_name: Mapping[str, list[Mapping[str, Any]]],
    card_indices: Mapping[int, int],
) -> int | None:
    offset_counts: dict[int, int] = {}
    for seed in seeds:
        card = _mtgjson_card_for_seed(seed=seed, cards_by_name=cards_by_name)
        if card is None:
            continue

        index = card_indices.get(id(card))
        if index is None:
            continue

        offset = seed.grp_id - index
        offset_counts[offset] = offset_counts.get(offset, 0) + 1

    if not offset_counts:
        return None

    return max(offset_counts, key=lambda offset: (offset_counts[offset], -offset))


def _add_mtgjson_cards_by_inferred_arena_order(
    *,
    cards: dict[int, CardInfo],
    card_objects: tuple[Mapping[str, Any], ...],
    cards_by_uuid: Mapping[str, Mapping[str, Any]],
    arena_id_offset: int,
) -> None:
    for index, card in enumerate(card_objects):
        if not _mtgjson_card_is_arena_available(card=card):
            continue

        grp_id = arena_id_offset + index
        existing = cards.get(grp_id)
        if existing is not None and (
            not existing.unknown
            or "mtgjson" in existing.source_provenance
            or "17lands" in existing.source_provenance
        ):
            continue

        inferred = _card_info_from_mtgjson(
            card=card,
            seed=_metadata_seed_from_mtgjson_card(grp_id=grp_id, card=card),
            cards_by_uuid=cards_by_uuid,
        )
        cards[grp_id] = (
            inferred
            if existing is None
            else _merge_card_info(base=existing, overlay=inferred)
        )


def _mtgjson_card_is_arena_available(*, card: Mapping[str, Any]) -> bool:
    availability = card.get("availability")
    return isinstance(availability, list) and "arena" in availability


def _metadata_seed_from_mtgjson_card(
    *,
    grp_id: int,
    card: Mapping[str, Any],
) -> CardMetadataSeed:
    name = _required_str(card.get("name"), field_name=f"MTGJSON card {grp_id}.name")
    return CardMetadataSeed(
        grp_id=grp_id,
        name=name,
        colors=_mtgjson_colors(card=card, field_name=name),
        rarity=_required_str(
            card.get("rarity", "unknown"),
            field_name=f"MTGJSON card {name}.rarity",
        ),
    )


def _mtgjson_card_sort_key(card: Mapping[str, Any]) -> tuple[int, int, str]:
    availability = card.get("availability")
    available_on_arena = isinstance(availability, list) and "arena" in availability
    side = card.get("side")
    is_front_or_single = side in (None, "", "a")
    uuid = card.get("uuid")
    return (
        0 if available_on_arena else 1,
        0 if is_front_or_single else 1,
        uuid if isinstance(uuid, str) else "",
    )


def _card_info_from_metadata_seed(*, seed: CardMetadataSeed) -> CardInfo:
    return CardInfo(
        grp_id=seed.grp_id,
        name=seed.name,
        colors=seed.colors,
        mana_value=None,
        rarity=seed.rarity,
        types=("Unknown",),
        unknown=True,
        arena_id=seed.grp_id,
        source_provenance=("17lands",),
    )


def _card_face_from_mtgjson(
    *,
    card: Mapping[str, Any],
    field_name: str,
) -> CardFace:
    type_line = _optional_source_text(card.get("type", card.get("type_line")))
    colors_value = card.get("colors", card.get("colorIdentity"))
    colors = (
        ()
        if colors_value is None
        else _color_tuple(colors_value, field_name=f"{field_name}.colors")
    )
    produced_value = card.get("producedMana", card.get("produced_mana"))
    produced_mana = (
        ()
        if produced_value is None
        else _produced_mana_tuple(
            produced_value,
            field_name=f"{field_name}.producedMana",
        )
    )
    return CardFace(
        name=_optional_source_text(card.get("faceName", card.get("name"))),
        oracle_text=_optional_source_text(
            card.get("text", card.get("oracle_text", card.get("oracleText")))
        ),
        keywords=_source_string_tuple(card.get("keywords")),
        type_line=type_line,
        subtypes=_source_subtypes(type_line=type_line),
        colors=colors,
        mana_cost=_optional_mtgjson_mana_cost(
            card.get("manaCost", card.get("mana_cost"))
        ),
        mana_value=_optional_float(
            card.get("manaValue", card.get("mana_value", card.get("cmc"))),
            field_name=f"{field_name}.manaValue",
        ),
        produced_mana=produced_mana,
        power=_optional_source_text(card.get("power")),
        toughness=_optional_source_text(card.get("toughness")),
    )


def _card_info_from_mtgjson(
    *,
    card: Mapping[str, Any],
    seed: CardMetadataSeed,
    cards_by_uuid: Mapping[str, Mapping[str, Any]],
) -> CardInfo:
    related_cards = _mtgjson_related_faces(card=card, cards_by_uuid=cards_by_uuid)
    aggregate_faces = tuple(
        _card_face_from_mtgjson(
            card=face,
            field_name=f"MTGJSON card {seed.name}.face",
        )
        for face in related_cards
    )
    face_records = aggregate_faces if len(related_cards) > 1 else ()
    type_lines = tuple(
        face.type_line for face in aggregate_faces if face.type_line
    )
    type_line = _optional_source_text(card.get("type", card.get("type_line")))
    if type_line is None and type_lines:
        type_line = " // ".join(type_lines)
    oracle_text = _optional_source_text(
        card.get("text", card.get("oracle_text", card.get("oracleText")))
    )
    if oracle_text is None:
        oracle_text = _combined_face_oracle_text(faces=aggregate_faces)
    mana_cost = _mtgjson_combined_mana_cost(faces=related_cards)
    if mana_cost is None and aggregate_faces:
        mana_cost = _combined_face_mana_cost(faces=aggregate_faces)
    colors = seed.colors or _mtgjson_colors(card=card, field_name=seed.name)
    produced_mana = _mtgjson_produced_mana(card=card, field_name=seed.name)
    if not produced_mana and aggregate_faces:
        produced_mana = _combined_face_produced_mana(faces=aggregate_faces)
    raw_types = tuple(dict.fromkeys(type_lines))
    power = _optional_source_text(card.get("power"))
    if power is None and len(aggregate_faces) > 1:
        power = _shared_face_text(faces=aggregate_faces, field_name="power")
    toughness = _optional_source_text(card.get("toughness"))
    if toughness is None and len(aggregate_faces) > 1:
        toughness = _shared_face_text(faces=aggregate_faces, field_name="toughness")
    return CardInfo(
        grp_id=seed.grp_id,
        name=_optional_source_text(card.get("name")) or seed.name,
        colors=colors,
        mana_value=_optional_float(
            card.get("manaValue", card.get("mana_value", card.get("cmc"))),
            field_name=f"MTGJSON card {seed.name}.manaValue",
        ),
        rarity=_optional_source_text(card.get("rarity")) or seed.rarity,
        types=raw_types or ("Unknown",),
        mana_cost=mana_cost,
        produced_mana=produced_mana,
        unknown=not bool(raw_types),
        oracle_text=oracle_text,
        keywords=(
            _source_string_tuple(card.get("keywords"))
            or _combined_face_keywords(faces=aggregate_faces)
        ),
        type_line=type_line,
        subtypes=_combined_subtypes(faces=aggregate_faces, type_line=type_line),
        layout=_optional_source_text(card.get("layout")),
        faces=face_records,
        set_code=_optional_source_text(card.get("setCode", card.get("set"))),
        collector_number=_optional_source_text(card.get("number")),
        arena_id=seed.grp_id,
        oracle_id=_source_oracle_id(card=card),
        source_provenance=("mtgjson",),
        power=power,
        toughness=toughness,
    )


def _combined_face_mana_cost(*, faces: tuple[CardFace, ...]) -> str | None:
    costs = tuple(face.mana_cost for face in faces if face.mana_cost)
    return " // ".join(costs) if costs else None


def _combined_face_produced_mana(*, faces: tuple[CardFace, ...]) -> tuple[str, ...]:
    return _ordered_unique_colors(
        colors=(symbol for face in faces for symbol in face.produced_mana)
    )


def _shared_face_text(
    *, faces: tuple[CardFace, ...], field_name: str
) -> str | None:
    values = tuple(getattr(face, field_name) for face in faces)
    if not values or any(value is None for value in values):
        return None
    if len(set(values)) != 1:
        return None
    return values[0]


def _mtgjson_related_faces(
    *,
    card: Mapping[str, Any],
    cards_by_uuid: Mapping[str, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    faces = [card]
    seen_uuids: set[str] = set()
    other_faces_value = card.get("otherFaceIds", ())
    if isinstance(other_faces_value, (list, tuple)):
        for face_uuid in other_faces_value:
            if not isinstance(face_uuid, str) or not face_uuid or face_uuid in seen_uuids:
                continue

            seen_uuids.add(face_uuid)
            face = cards_by_uuid.get(face_uuid)
            if face is not None and face is not card:
                faces.append(face)

    return tuple(faces)


def _mtgjson_combined_mana_cost(*, faces: tuple[Mapping[str, Any], ...]) -> str | None:
    costs = tuple(
        cost
        for cost in (_optional_mtgjson_mana_cost(face.get("manaCost")) for face in faces)
        if cost is not None
    )
    if not costs:
        return None

    return " // ".join(costs)


def _optional_mtgjson_mana_cost(value: Any) -> str | None:
    if value is None:
        return None

    return _required_str(value, field_name="MTGJSON card.manaCost")


def _mtgjson_colors(*, card: Mapping[str, Any], field_name: str) -> tuple[str, ...]:
    colors_value = card.get("colors")
    if colors_value is not None:
        return _color_tuple(colors_value, field_name=f"MTGJSON card {field_name}.colors")

    return _color_tuple(
        card.get("colorIdentity", ()),
        field_name=f"MTGJSON card {field_name}.colorIdentity",
    )


def _mtgjson_produced_mana(
    *,
    card: Mapping[str, Any],
    field_name: str,
) -> tuple[str, ...]:
    produced_value = card.get("producedMana")
    if produced_value is None:
        return ()

    return _produced_mana_tuple(
        produced_value,
        field_name=f"MTGJSON card {field_name}.producedMana",
    )


def _normalized_card_name(*, name: str) -> str:
    return " ".join(name.casefold().replace("’", "'").split())


def _cache_path(
    *,
    app_dir: PathInput | None,
    cache_path: PathInput | None,
) -> Path:
    if cache_path is not None:
        return Path(cache_path)

    return card_database_cache_path(app_dir=app_dir)


def _source_oracle_id(*, card: Mapping[str, Any]) -> str | None:
    """Return an oracle identifier from a supported source card object."""

    for field_name in ("oracle_id", "oracleId"):
        value = card.get(field_name)
        if value is not None:
            return _optional_source_text(value)

    identifiers = card.get("identifiers")
    if isinstance(identifiers, Mapping):
        for field_name in ("scryfallOracleId", "oracle_id", "oracleId"):
            value = identifiers.get(field_name)
            if value is not None:
                return _optional_source_text(value)
    return None


def _optional_source_text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _source_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise CardDatabaseError("Invalid source string list.")
    return tuple(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _source_subtypes(*, type_line: str | None) -> tuple[str, ...]:
    if not type_line:
        return ()
    if "—" in type_line:
        subtype_text = type_line.split("—", 1)[1]
    elif "-" in type_line:
        subtype_text = type_line.split("-", 1)[1]
    else:
        return ()
    return tuple(dict.fromkeys(subtype_text.split()))


def _scryfall_faces(
    *,
    card: Mapping[str, Any],
    grp_id: int,
) -> tuple[CardFace, ...]:
    if "card_faces" not in card:
        return ()

    faces_value = card["card_faces"]
    if not isinstance(faces_value, list):
        raise CardDatabaseError(
            f"Missing or invalid card {grp_id}.card_faces; expected object list."
        )

    faces: list[CardFace] = []
    for face in faces_value:
        if not isinstance(face, dict):
            raise CardDatabaseError(
                f"Missing or invalid card {grp_id}.card_faces; expected objects."
            )
        faces.append(_card_face_from_scryfall(face=face, grp_id=grp_id))
    return tuple(faces)


def _card_face_from_scryfall(
    *,
    face: Mapping[str, Any],
    grp_id: int,
) -> CardFace:
    type_line = _optional_source_text(face.get("type_line"))
    colors_value = face.get("colors")
    colors = (
        ()
        if colors_value is None
        else _color_tuple(colors_value, field_name=f"card {grp_id}.face.colors")
    )
    produced_value = face.get("produced_mana")
    produced_mana = (
        ()
        if produced_value is None
        else _produced_mana_tuple(
            produced_value,
            field_name=f"card {grp_id}.face.produced_mana",
        )
    )
    return CardFace(
        name=_optional_source_text(face.get("name")),
        oracle_text=_optional_source_text(face.get("oracle_text")),
        keywords=_source_string_tuple(face.get("keywords")),
        type_line=type_line,
        subtypes=_source_subtypes(type_line=type_line),
        colors=colors,
        mana_cost=_optional_source_text(face.get("mana_cost")),
        mana_value=_optional_float(
            face.get("cmc"),
            field_name=f"card {grp_id}.face.cmc",
        ),
        produced_mana=produced_mana,
        power=_optional_source_text(face.get("power")),
        toughness=_optional_source_text(face.get("toughness")),
    )


def _combined_face_type_line(*, faces: tuple[CardFace, ...]) -> str | None:
    type_lines = tuple(face.type_line for face in faces if face.type_line)
    if not type_lines:
        return None
    return " // ".join(type_lines)


def _combined_face_keywords(*, faces: tuple[CardFace, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            keyword for face in faces for keyword in face.keywords
        )
    )


def _combined_face_oracle_text(*, faces: tuple[CardFace, ...]) -> str | None:
    oracle_texts = tuple(face.oracle_text for face in faces if face.oracle_text)
    if not oracle_texts:
        return None
    return " // ".join(oracle_texts)


def _combined_subtypes(
    *,
    faces: tuple[CardFace, ...],
    type_line: str | None,
) -> tuple[str, ...]:
    if type_line and " // " not in type_line:
        return _source_subtypes(type_line=type_line)
    return tuple(
        dict.fromkeys(
            subtype for face in faces for subtype in face.subtypes
        )
    )


def _arena_direct_type_line(*, card: Mapping[str, Any]) -> str | None:
    return _optional_source_text(
        card.get("type_line", card.get("typeLine", card.get("type")))
    )


def _arena_color_values(*, card: Mapping[str, Any]) -> Any:
    colors = card.get("colors")
    return colors if colors is not None else card.get("colorIdentity", ())


def _arena_card_face(
    *,
    card: Mapping[str, Any],
    localization: Mapping[int, str],
    grp_id: int,
) -> CardFace:
    type_line = _arena_direct_type_line(card=card)
    if type_line is None:
        type_line = _arena_type_line(
            card=card,
            localization=localization,
            grp_id=grp_id,
        )
    colors_value = _arena_color_values(card=card)
    colors = _arena_color_tuple(
        colors_value,
        field_name=f"Arena card {grp_id}.colors",
    )
    produced_value = next(
        (
            card.get(field_name)
            for field_name in ARENA_PRODUCED_MANA_FIELDS
            if card.get(field_name) is not None
        ),
        None,
    )
    produced_mana = (
        ()
        if produced_value is None
        else _arena_mana_symbol_tuple(
            produced_value,
            field_name=f"Arena card {grp_id}.produced_mana",
        )
    )
    name = _optional_source_text(
        card.get("name", card.get("title", card.get("cardName")))
    )
    if name is None:
        name = _arena_optional_localized_text(
            localization=localization,
            text_id=card.get("titleId"),
            field_name=f"Arena card {grp_id}.titleId",
        )
    oracle_text = _optional_source_text(
        card.get("oracle_text", card.get("oracleText", card.get("text")))
    )
    if oracle_text is None:
        for field_name in ("oracleTextId", "rulesTextId", "textId"):
            oracle_text = _arena_optional_localized_text(
                localization=localization,
                text_id=card.get(field_name),
                field_name=f"Arena card {grp_id}.{field_name}",
            )
            if oracle_text is not None:
                break
    return CardFace(
        name=name,
        oracle_text=oracle_text,
        keywords=_source_string_tuple(card.get("keywords")),
        type_line=type_line,
        subtypes=_source_subtypes(type_line=type_line),
        colors=colors,
        mana_cost=_arena_mana_cost(
            card.get("castingcost", card.get("castingCost"))
        ),
        mana_value=_optional_float(
            card.get("cmc", card.get("manaValue")),
            field_name=f"Arena card {grp_id}.cmc",
        ),
        produced_mana=produced_mana,
        power=_optional_source_text(card.get("power")),
        toughness=_optional_source_text(card.get("toughness")),
    )


def _card_info_from_scryfall(*, card: Mapping[str, Any]) -> CardInfo | None:
    arena_id = card.get("arena_id")
    if arena_id is None:
        return None

    grp_id = _required_int(arena_id, field_name="arena_id")
    name = _required_str(card.get("name"), field_name=f"card {grp_id}.name")
    mana_value = _required_float(card.get("cmc"), field_name=f"card {grp_id}.cmc")
    rarity = _required_str(card.get("rarity"), field_name=f"card {grp_id}.rarity")
    faces = _scryfall_faces(card=card, grp_id=grp_id)
    type_line = _optional_source_text(card.get("type_line"))
    if type_line is None:
        type_line = _combined_face_type_line(faces=faces)
    oracle_text = _optional_source_text(card.get("oracle_text"))
    if oracle_text is None:
        oracle_text = _combined_face_oracle_text(faces=faces)
    power = _optional_source_text(card.get("power"))
    if power is None and len(faces) > 1:
        power = _shared_face_text(faces=faces, field_name="power")
    toughness = _optional_source_text(card.get("toughness"))
    if toughness is None and len(faces) > 1:
        toughness = _shared_face_text(faces=faces, field_name="toughness")
    return CardInfo(
        grp_id=grp_id,
        rarity=rarity,
        name=name,
        colors=_card_colors(card=card, grp_id=grp_id),
        mana_value=mana_value,
        keywords=(
            _source_string_tuple(card.get("keywords"))
            or _combined_face_keywords(faces=faces)
        ),
        types=_card_types(card=card, grp_id=grp_id),
        mana_cost=_card_mana_cost(card=card),
        produced_mana=_card_produced_mana(card=card, grp_id=grp_id),
        image_uri=_card_image_uri(card=card),
        oracle_text=oracle_text,
        type_line=type_line,
        subtypes=_combined_subtypes(faces=faces, type_line=type_line),
        layout=_optional_source_text(card.get("layout")),
        faces=faces,
        set_code=_optional_source_text(card.get("set")),
        collector_number=_optional_source_text(card.get("collector_number")),
        arena_id=grp_id,
        oracle_id=_source_oracle_id(card=card),
        source_provenance=("scryfall",),
        power=power,
        toughness=toughness,
    )


def _card_info_from_arena(
    *,
    card: Mapping[str, Any],
    localization: Mapping[int, str],
    cards_by_grp_id: Mapping[int, Mapping[str, Any]],
) -> CardInfo | None:
    grp_id_value = card.get("grpid", card.get("grpId"))
    if grp_id_value is None:
        return None

    grp_id = _required_int(grp_id_value, field_name="Arena card.grpid")
    linked_faces = _arena_linked_face_cards(
        card=card,
        cards_by_grp_id=cards_by_grp_id,
    )
    primary_type_line = _arena_type_line(
        card=card,
        localization=localization,
        grp_id=grp_id,
    )
    type_line = _arena_combined_type_line(
        primary_type_line=primary_type_line,
        linked_faces=linked_faces,
        localization=localization,
        grp_id=grp_id,
    )
    face_records = (
        tuple(
            _arena_card_face(
                card=face,
                localization=localization,
                grp_id=grp_id,
            )
            for face in (card, *linked_faces)
        )
        if linked_faces
        else ()
    )
    oracle_text = _optional_source_text(
        card.get("oracle_text", card.get("oracleText", card.get("text")))
    )
    if face_records:
        face_oracle_text = _combined_face_oracle_text(faces=face_records)
        if face_oracle_text is not None:
            oracle_text = face_oracle_text
    produced_mana = _arena_card_produced_mana(
        card=card,
        primary_type_line=_arena_type_line(
            card=card,
            localization=localization,
            grp_id=grp_id,
        ),
        grp_id=grp_id,
    )
    if not produced_mana and face_records:
        produced_mana = _combined_face_produced_mana(faces=face_records)
    power = _optional_source_text(card.get("power"))
    if power is None and len(face_records) > 1:
        power = _shared_face_text(faces=face_records, field_name="power")
    toughness = _optional_source_text(card.get("toughness"))
    if toughness is None and len(face_records) > 1:
        toughness = _shared_face_text(faces=face_records, field_name="toughness")
    name = _optional_source_text(
        card.get("name", card.get("title", card.get("cardName")))
    )
    if name is None:
        name = _arena_optional_localized_text(
            localization=localization,
            text_id=card.get("titleId"),
            field_name=f"Arena card {grp_id}.titleId",
        )
    if name is None:
        name = f"Unknown card {grp_id}"
    return CardInfo(
        grp_id=grp_id,
        name=name,
        colors=_arena_card_colors(
            card=card,
            linked_faces=linked_faces,
            grp_id=grp_id,
        ),
        mana_value=_optional_float(
            card.get("cmc", card.get("manaValue")),
            field_name=f"Arena card {grp_id}.cmc",
        ),
        rarity=_arena_card_rarity(card=card, grp_id=grp_id),
        types=(type_line,),
        mana_cost=_arena_card_mana_cost(card=card, linked_faces=linked_faces),
        produced_mana=produced_mana,
        unknown=type_line == "Unknown",
        oracle_text=oracle_text,
        keywords=(
            _source_string_tuple(card.get("keywords"))
            or _combined_face_keywords(faces=face_records)
        ),
        type_line=type_line,
        subtypes=_combined_subtypes(faces=face_records, type_line=type_line),
        layout=_optional_source_text(card.get("layout", card.get("cardLayout"))),
        faces=face_records,
        set_code=_optional_source_text(
            card.get("set_code", card.get("setCode", card.get("set")))
        ),
        collector_number=_optional_source_text(
            card.get("collector_number", card.get("collectorNumber"))
        ),
        arena_id=grp_id,
        oracle_id=_source_oracle_id(card=card),
        source_provenance=("arena",),
        power=power,
        toughness=toughness,
    )


def _download_or_arena_card_database(
    *,
    arena_data_dir: PathInput | None,
    timeout_seconds: int,
) -> tuple[CardDatabase, bool]:
    """Return the current-run database and whether it is safe to cache canonically."""

    try:
        database = download_scryfall_card_database(timeout_seconds=timeout_seconds)
    except CardDatabaseError:
        arena_database = _load_arena_card_database_if_available(
            arena_data_dir=arena_data_dir,
        )
        if arena_database is None:
            raise

        return arena_database, False

    return (
        augment_card_database_with_arena_data(
            database,
            arena_data_dir=arena_data_dir,
        ),
        True,
    )


def _load_arena_card_database_if_available(
    *,
    arena_data_dir: PathInput | None,
) -> CardDatabase | None:
    if arena_data_dir is not None:
        return build_card_database_from_arena_data_dir(path=arena_data_dir)

    default_data_dir = find_default_arena_data_dir()
    if default_data_dir is None:
        return None

    return build_card_database_from_arena_data_dir(path=default_data_dir)


def _merge_card_faces_power(
    *, base: tuple[CardFace, ...], overlay: tuple[CardFace, ...]
) -> tuple[CardFace, ...]:
    if not base:
        return overlay
    if not overlay or len(base) != len(overlay):
        return base
    return tuple(
        replace(
            face,
            power=face.power if face.power is not None else overlay[index].power,
            toughness=(
                face.toughness
                if face.toughness is not None
                else overlay[index].toughness
            ),
        )
        for index, face in enumerate(base)
    )


def _merge_card_info(*, base: CardInfo, overlay: CardInfo) -> CardInfo:
    """Augment missing canonical fields without replacing valid source data."""

    all_sources = tuple(
        dict.fromkeys((*base.source_provenance, *overlay.source_provenance))
    )
    known_sources = tuple(
        source
        for source in ("scryfall", "arena", "mtgjson")
        if source in all_sources
    )
    provenance = known_sources + tuple(sorted(set(all_sources) - set(known_sources)))
    return replace(
        base,
        name=overlay.name if base.unknown else base.name,
        colors=overlay.colors if base.unknown else base.colors,
        mana_value=base.mana_value if base.mana_value is not None else overlay.mana_value,
        rarity=(
            base.rarity
            if base.rarity and base.rarity != "unknown"
            else overlay.rarity
        ),
        types=base.types if base.types and not base.unknown else overlay.types,
        mana_cost=base.mana_cost if base.mana_cost is not None else overlay.mana_cost,
        produced_mana=base.produced_mana or overlay.produced_mana,
        image_uri=base.image_uri if base.image_uri is not None else overlay.image_uri,
        unknown=base.unknown and overlay.unknown,
        oracle_text=(
            base.oracle_text
            if base.oracle_text is not None
            else overlay.oracle_text
        ),
        keywords=base.keywords or overlay.keywords,
        type_line=base.type_line if base.type_line is not None else overlay.type_line,
        subtypes=base.subtypes if base.subtypes else overlay.subtypes,
        layout=base.layout if base.layout is not None else overlay.layout,
        faces=_merge_card_faces_power(base=base.faces, overlay=overlay.faces),
        set_code=base.set_code if base.set_code is not None else overlay.set_code,
        collector_number=(
            base.collector_number
            if base.collector_number is not None
            else overlay.collector_number
        ),
        oracle_id=(
            base.oracle_id
            if base.oracle_id is not None
            else overlay.oracle_id
        ),
        arena_id=base.arena_id if base.arena_id is not None else overlay.arena_id,
        source_provenance=provenance,
        power=base.power if base.power is not None else overlay.power,
        toughness=base.toughness if base.toughness is not None else overlay.toughness,
    )


def _merge_card_databases(
    *,
    base: CardDatabase,
    overlay: CardDatabase,
) -> CardDatabase:
    cards = dict(base.cards)
    for grp_id, overlay_card in overlay.cards.items():
        base_card = cards.get(grp_id)
        if base_card is None:
            cards[grp_id] = overlay_card
        else:
            cards[grp_id] = _merge_card_info(base=base_card, overlay=overlay_card)

    image_uris_by_name = dict(base.image_uris_by_name)
    for name, image_uri in overlay.image_uris_by_name.items():
        image_uris_by_name.setdefault(name, image_uri)
    return replace(
        base,
        cards=cards,
        image_uris_by_name=image_uris_by_name,
    )


SCRYFALL_IMAGE_URI_KEYS = (
    "normal",
    "large",
    "small",
    "png",
    "border_crop",
    "art_crop",
)


def _add_scryfall_image_uri_entries(
    *,
    card: Mapping[str, Any],
    image_uris_by_name: dict[str, str],
) -> None:
    image_uri = _card_image_uri(card=card)
    if image_uri is None:
        return

    for name in _scryfall_card_image_names(card=card):
        image_uris_by_name.setdefault(_normalized_card_name(name=name), image_uri)


def _scryfall_card_image_names(*, card: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    name = card.get("name")
    if isinstance(name, str) and name:
        names.append(name)

    faces_value = card.get("card_faces")
    if isinstance(faces_value, list):
        for face in faces_value:
            if not isinstance(face, dict):
                continue

            face_name = face.get("name")
            if isinstance(face_name, str) and face_name:
                names.append(face_name)

    return tuple(dict.fromkeys(names))


def _card_image_uri(*, card: Mapping[str, Any]) -> str | None:
    image_uri = _image_uri_from_scryfall_image_uris(card.get("image_uris"))
    if image_uri is not None:
        return image_uri

    faces_value = card.get("card_faces")
    if not isinstance(faces_value, list):
        return None

    for face in faces_value:
        if not isinstance(face, dict):
            continue

        image_uri = _image_uri_from_scryfall_image_uris(face.get("image_uris"))
        if image_uri is not None:
            return image_uri

    return None


def _image_uri_from_scryfall_image_uris(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None

    for key in SCRYFALL_IMAGE_URI_KEYS:
        uri = value.get(key)
        if isinstance(uri, str) and uri:
            return uri

    return None


def _card_colors(*, card: Mapping[str, Any], grp_id: int) -> tuple[str, ...]:
    colors_value = card.get("colors")
    if colors_value is not None:
        return _color_tuple(colors_value, field_name=f"card {grp_id}.colors")

    face_colors: list[str] = []
    faces_value = card.get("card_faces")
    if isinstance(faces_value, list):
        for face in faces_value:
            if not isinstance(face, dict):
                continue

            face_colors.extend(
                _color_tuple(
                    face.get("colors", ()),
                    field_name=f"card {grp_id}.card_faces[].colors",
                )
            )

    return _ordered_unique_colors(colors=face_colors)


def _card_types(*, card: Mapping[str, Any], grp_id: int) -> tuple[str, ...]:
    type_line_value = card.get("type_line")
    if isinstance(type_line_value, str) and type_line_value:
        return (type_line_value,)

    faces_value = card.get("card_faces")
    face_types: list[str] = []
    if isinstance(faces_value, list):
        for face in faces_value:
            if not isinstance(face, dict):
                continue

            face_type = face.get("type_line")
            if isinstance(face_type, str) and face_type:
                face_types.append(face_type)

    if face_types:
        return tuple(face_types)

    raise CardDatabaseError(f"Scryfall card {grp_id} is missing type_line.")


def _card_mana_cost(*, card: Mapping[str, Any]) -> str | None:
    mana_cost_value = card.get("mana_cost")
    if isinstance(mana_cost_value, str) and mana_cost_value:
        return mana_cost_value

    faces_value = card.get("card_faces")
    face_costs: list[str] = []
    if isinstance(faces_value, list):
        for face in faces_value:
            if not isinstance(face, dict):
                continue

            face_cost = face.get("mana_cost")
            if isinstance(face_cost, str) and face_cost:
                face_costs.append(face_cost)

    if face_costs:
        return " // ".join(face_costs)

    return None


def _card_produced_mana(*, card: Mapping[str, Any], grp_id: int) -> tuple[str, ...]:
    produced_value = card.get("produced_mana")
    if produced_value is not None:
        return _produced_mana_tuple(
            produced_value,
            field_name=f"card {grp_id}.produced_mana",
        )

    faces_value = card.get("card_faces")
    face_mana: list[str] = []
    if isinstance(faces_value, list):
        for face in faces_value:
            if not isinstance(face, dict):
                continue

            face_value = face.get("produced_mana")
            if face_value is None:
                continue

            face_mana.extend(
                _produced_mana_tuple(
                    face_value,
                    field_name=f"card {grp_id}.card_faces[].produced_mana",
                )
            )

    return _ordered_unique_colors(colors=face_mana)


def _arena_linked_face_cards(
    *,
    card: Mapping[str, Any],
    cards_by_grp_id: Mapping[int, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    linked_faces_value = card.get("linkedFaces", ())
    if linked_faces_value is None:
        return ()

    if isinstance(linked_faces_value, (str, bytes)) or not isinstance(
        linked_faces_value,
        Iterable,
    ):
        raise CardDatabaseError("Missing or invalid Arena card.linkedFaces list.")

    linked_faces: list[Mapping[str, Any]] = []
    seen_grp_ids: set[int] = set()
    for linked_face_value in linked_faces_value:
        if isinstance(linked_face_value, dict):
            linked_grp_id_value = linked_face_value.get(
                "grpid",
                linked_face_value.get("grpId"),
            )
            if linked_grp_id_value is None:
                linked_faces.append(linked_face_value)
                continue
            linked_grp_id = _required_int(
                linked_grp_id_value,
                field_name="Arena card.linkedFaces[].grpId",
            )
        else:
            linked_grp_id = _required_int(
                linked_face_value,
                field_name="Arena card.linkedFaces[]",
            )
        if linked_grp_id in seen_grp_ids:
            continue
        seen_grp_ids.add(linked_grp_id)
        linked_card = cards_by_grp_id.get(linked_grp_id)
        if linked_card is not None:
            linked_faces.append(linked_card)

    return tuple(linked_faces)


def _arena_combined_type_line(
    *,
    primary_type_line: str,
    linked_faces: tuple[Mapping[str, Any], ...],
    localization: Mapping[int, str],
    grp_id: int,
) -> str:
    type_lines = [primary_type_line]
    type_lines.extend(
        _arena_type_line(
            card=face,
            localization=localization,
            grp_id=grp_id,
        )
        for face in linked_faces
    )
    unique_type_lines = tuple(dict.fromkeys(type_lines))
    return " // ".join(unique_type_lines)


def _arena_type_line(
    *,
    card: Mapping[str, Any],
    localization: Mapping[int, str],
    grp_id: int,
) -> str:
    direct_type_line = _arena_direct_type_line(card=card)
    if direct_type_line is not None:
        return direct_type_line

    card_type = _arena_optional_localized_text(
        localization=localization,
        text_id=card.get("cardTypeTextId"),
        field_name=f"Arena card {grp_id}.cardTypeTextId",
    )
    subtype = _arena_optional_localized_text(
        localization=localization,
        text_id=card.get("subtypeTextId"),
        field_name=f"Arena card {grp_id}.subtypeTextId",
    )
    if card_type is None:
        return "Unknown"
    if subtype is None:
        return card_type

    return f"{card_type} — {subtype}"


def _arena_card_colors(
    *,
    card: Mapping[str, Any],
    linked_faces: tuple[Mapping[str, Any], ...],
    grp_id: int,
) -> tuple[str, ...]:
    colors: list[str] = []
    for face in (card, *linked_faces):
        colors_value = _arena_color_values(card=face)
        colors.extend(
            _arena_color_tuple(
                colors_value,
                field_name=f"Arena card {grp_id}.colors",
            )
        )

    return _ordered_unique_colors(colors=colors)


def _arena_card_rarity(*, card: Mapping[str, Any], grp_id: int) -> str:
    rarity_value = card.get("rarity")
    if rarity_value is None:
        return "unknown"
    if isinstance(rarity_value, str):
        rarity = rarity_value.strip().lower().replace("mythic rare", "mythic")
        return _required_str(rarity, field_name=f"Arena card {grp_id}.rarity")

    rarity_id = _required_int(rarity_value, field_name=f"Arena card {grp_id}.rarity")
    try:
        return ARENA_RARITY_ID_MAP[rarity_id]
    except KeyError as error:
        raise CardDatabaseError(
            f"Invalid Arena rarity id in card {grp_id}.rarity: {rarity_id}."
        ) from error


def _arena_card_mana_cost(
    *,
    card: Mapping[str, Any],
    linked_faces: tuple[Mapping[str, Any], ...],
) -> str | None:
    costs = tuple(
        cost
        for cost in (
            _arena_mana_cost(face.get("castingcost", face.get("castingCost")))
            for face in (card, *linked_faces)
        )
        if cost is not None
    )
    if not costs:
        return None

    return " // ".join(costs)


def _arena_card_produced_mana(
    *,
    card: Mapping[str, Any],
    primary_type_line: str,
    grp_id: int,
) -> tuple[str, ...]:
    for field_name in ARENA_PRODUCED_MANA_FIELDS:
        produced_value = card.get(field_name)
        if produced_value is not None:
            return _arena_mana_symbol_tuple(
                produced_value,
                field_name=f"Arena card {grp_id}.{field_name}",
            )

    if "Land" not in primary_type_line:
        return ()

    return _arena_color_tuple(
        card.get("colorIdentity", ()),
        field_name=f"Arena card {grp_id}.colorIdentity",
    )


def _arena_mana_cost(value: Any) -> str | None:
    if not isinstance(value, str) or value in {"", "o0"}:
        return None

    parts = tuple(part for part in value.split("o") if part and part != "0")
    if not parts:
        return None

    return "".join(f"{{{part}}}" for part in parts)


def _arena_localization_map(
    *,
    items: Iterable[Mapping[str, Any]],
    source: str,
) -> dict[int, str]:
    language = _select_arena_english_localization(items=items, source=source)
    keys_value = language.get("keys")
    if not isinstance(keys_value, list):
        raise CardDatabaseError(
            f"Malformed Arena localization {source}: selected language has no keys list."
        )

    localization: dict[int, str] = {}
    for item in keys_value:
        if not isinstance(item, dict):
            raise CardDatabaseError(
                f"Malformed Arena localization {source}: key entry is not object."
            )

        text_id = _required_int(item.get("id"), field_name="Arena localization id")
        text = item.get("text")
        if not isinstance(text, str):
            raise CardDatabaseError("Missing or invalid Arena localization text.")

        localization[text_id] = text

    return localization


def _select_arena_english_localization(
    *,
    items: Iterable[Mapping[str, Any]],
    source: str,
) -> Mapping[str, Any]:
    languages = tuple(items)
    if not languages:
        raise CardDatabaseError(f"Malformed Arena localization {source}: empty list.")

    for language in languages:
        langkey = str(language.get("langkey", "")).lower()
        iso_code = str(language.get("isoCode", "")).lower()
        if langkey in {"en", "english"} or iso_code in {"en", "en-us"}:
            return language

    return languages[0]


def _arena_optional_localized_text(
    *,
    localization: Mapping[int, str],
    text_id: Any,
    field_name: str,
) -> str | None:
    if text_id is None:
        return None

    resolved_text_id = _required_int(text_id, field_name=field_name)
    if resolved_text_id == 0:
        return None

    text = localization.get(resolved_text_id)
    if text is None:
        raise CardDatabaseError(f"Missing localization for {field_name}.")

    if text == "":
        return None

    return text


def _arena_data_file_pair(
    *,
    path: Path,
    required: bool,
) -> tuple[Path, Path] | None:
    cards_path = _latest_arena_data_file(path=path, prefix=ARENA_DATA_CARDS_PREFIX)
    loc_path = _latest_arena_data_file(path=path, prefix=ARENA_DATA_LOC_PREFIX)
    if cards_path is not None and loc_path is not None:
        return cards_path, loc_path

    if required:
        raise CardDatabaseError(
            f"Arena local data at {path} is missing data_cards*.mtga or data_loc*.mtga."
        )

    return None


def _latest_arena_data_file(*, path: Path, prefix: str) -> Path | None:
    if not path.is_dir():
        return None

    candidates = tuple(
        candidate
        for candidate in path.iterdir()
        if candidate.is_file()
        and candidate.name.startswith(prefix)
        and candidate.suffix.lower() in ARENA_DATA_FILE_SUFFIXES
    )
    if not candidates:
        return None

    return max(candidates, key=_arena_data_file_sort_key)


def _arena_data_file_sort_key(path: Path) -> tuple[float, str]:
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        modified_at = 0.0

    return modified_at, path.name


def _load_arena_json_array(*, path: Path, label: str) -> tuple[Mapping[str, Any], ...]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise CardDatabaseError(f"Could not read Arena {label} file {path}: {error}.") from error

    payload = _strip_javascript_assignment(text=text)
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise CardDatabaseError(
            f"Malformed Arena {label} JSON at {path}: {error.msg}."
        ) from error

    if not isinstance(value, list):
        raise CardDatabaseError(f"Malformed Arena {label} JSON at {path}: expected list.")

    objects: list[Mapping[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise CardDatabaseError(
                f"Malformed Arena {label} JSON at {path}:{index}: expected object."
            )

        objects.append(item)

    return tuple(objects)


def _strip_javascript_assignment(*, text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("var ") and "=" in stripped:
        stripped = stripped.split("=", 1)[1].strip()

    return stripped.rstrip(";").strip()


def _default_arena_data_dir_candidates() -> tuple[Path, ...]:
    current_system = platform.system()
    home = Path.home()
    if current_system == "Darwin":
        return (
            home
            / "Library"
            / "Application Support"
            / "com.wizards.mtga"
            / "Downloads"
            / "Data",
        )

    if current_system == "Windows":
        candidates: list[Path] = []
        registry_path = _windows_registry_arena_data_dir()
        if registry_path is not None:
            candidates.append(registry_path)

        for root_name in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(root_name)
            if root is None:
                continue

            candidates.append(
                Path(root)
                / "Wizards of the Coast"
                / "MTGA"
                / "MTGA_Data"
                / "Downloads"
                / "Data"
            )

        return tuple(candidates)

    return ()


def _windows_registry_arena_data_dir() -> Path | None:
    if platform.system() != "Windows":
        return None

    try:
        import winreg
    except ImportError:
        return None

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Wizards of the Coast\MTGArena",
        ) as registry_key:
            install_path, _ = winreg.QueryValueEx(registry_key, "Path")
    except OSError:
        return None

    if not isinstance(install_path, str):
        return None

    return Path(install_path) / "MTGA_Data" / "Downloads" / "Data"


def _open_text_bulk_file(*, path: Path) -> io.TextIOBase:
    if path.suffix == ".gz":
        return gzip.open(path, mode="rt", encoding="utf-8")

    return path.open(mode="rt", encoding="utf-8")


def _iter_scryfall_jsonl_url(
    *,
    url: str,
    timeout_seconds: int,
) -> Iterator[Mapping[str, Any]]:
    request = _request(url=url)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            with gzip.GzipFile(fileobj=response) as compressed:
                text_stream = io.TextIOWrapper(compressed, encoding="utf-8")
                yield from _iter_jsonl_objects(lines=text_stream, source=url)
    except urllib.error.URLError as error:
        raise CardDatabaseError(f"Failed to download Scryfall bulk data: {error}") from error
    except OSError as error:
        raise CardDatabaseError(f"Failed to decompress Scryfall bulk data: {error}") from error


def _iter_jsonl_objects(
    *,
    lines: Iterable[str],
    source: str,
) -> Iterator[Mapping[str, Any]]:
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        try:
            item = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise CardDatabaseError(
                f"Malformed Scryfall bulk JSON at {source}:{line_number}: {error.msg}."
            ) from error

        if not isinstance(item, dict):
            raise CardDatabaseError(
                f"Malformed Scryfall bulk JSON at {source}:{line_number}: "
                "expected object."
            )

        yield item


def _fetch_bulk_data_items(*, timeout_seconds: int) -> list[Mapping[str, Any]]:
    request = _request(url=SCRYFALL_BULK_DATA_URL)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise CardDatabaseError(f"Failed to query Scryfall bulk metadata: {error}") from error
    except json.JSONDecodeError as error:
        raise CardDatabaseError(f"Malformed Scryfall bulk metadata: {error}") from error

    if not isinstance(payload, dict):
        raise CardDatabaseError("Malformed Scryfall bulk metadata: expected object.")

    data = payload.get("data")
    if not isinstance(data, list):
        raise CardDatabaseError("Malformed Scryfall bulk metadata: missing data list.")

    items: list[Mapping[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise CardDatabaseError("Malformed Scryfall bulk metadata: item is not object.")

        items.append(item)

    return items


def _default_cards_download_uri(*, bulk_items: Iterable[Mapping[str, Any]]) -> str:
    for item in bulk_items:
        if item.get("type") != SCRYFALL_DEFAULT_CARDS_TYPE:
            continue

        uri = item.get("jsonl_download_uri", item.get("download_uri"))
        return _required_str(uri, field_name="default_cards.download_uri")

    raise CardDatabaseError("Scryfall bulk metadata did not include default_cards.")


def _request(*, url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/json;q=0.9,*/*;q=0.8",
            "User-Agent": SCRYFALL_USER_AGENT,
        },
    )


def _response_content_length(*, response: Any) -> int | None:
    """Report one response's declared body length when it is trustworthy."""

    if getattr(response, "chunked", False):
        return None
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _require_gzip_bulk(*, path: Path) -> None:
    """Reject a default-cards payload that is not the gzip JSONL Scryfall serves."""

    with path.open("rb") as stream:
        magic = stream.read(2)
    if magic != b"\x1f\x8b":
        raise CardDatabaseError(
            "Scryfall default-cards download is not a gzip JSONL file."
        )


def _color_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    colors = _string_tuple(value, field_name=field_name)
    invalid = [color for color in colors if color not in COLOR_ORDER]
    if invalid:
        raise CardDatabaseError(f"Invalid color values in {field_name}: {invalid}.")

    return _ordered_unique_colors(colors=colors)


def _arena_color_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise CardDatabaseError(f"Missing or invalid {field_name}; expected id list.")

    colors: list[str] = []
    for item in value:
        color_id = _required_int(item, field_name=field_name)
        try:
            colors.append(ARENA_COLOR_ID_MAP[color_id])
        except KeyError as error:
            raise CardDatabaseError(
                f"Invalid Arena color id in {field_name}: {color_id}."
            ) from error

    return _ordered_unique_colors(colors=colors)


def _arena_mana_symbol_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise CardDatabaseError(f"Missing or invalid {field_name}; expected mana list.")

    colors: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item == "C":
                continue
            if item not in COLOR_ORDER:
                raise CardDatabaseError(
                    f"Invalid Arena mana value in {field_name}: {item}."
                )

            colors.append(item)
            continue

        color_id = _required_int(item, field_name=field_name)
        try:
            colors.append(ARENA_COLOR_ID_MAP[color_id])
        except KeyError as error:
            raise CardDatabaseError(
                f"Invalid Arena mana id in {field_name}: {color_id}."
            ) from error

    return _ordered_unique_colors(colors=colors)


def _produced_mana_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    mana_symbols = _string_tuple(value, field_name=field_name)
    invalid = [symbol for symbol in mana_symbols if symbol not in (*COLOR_ORDER, "C")]
    if invalid:
        raise CardDatabaseError(f"Invalid mana values in {field_name}: {invalid}.")

    return _ordered_unique_colors(
        colors=(symbol for symbol in mana_symbols if symbol in COLOR_ORDER)
    )


def _ordered_unique_colors(*, colors: Iterable[str]) -> tuple[str, ...]:
    color_set = set(colors)
    return tuple(color for color in COLOR_ORDER if color in color_set)


def _string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise CardDatabaseError(f"Missing or invalid {field_name}; expected string list.")

    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CardDatabaseError(
                f"Missing or invalid {field_name}; expected only strings."
            )

        result.append(item)

    return tuple(result)


def _required_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise CardDatabaseError(
            f"Missing or invalid {field_name}; expected non-empty string."
        )

    return value


def _optional_str(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None

    return _required_str(value, field_name=field_name)


def _optional_datetime(value: Any) -> datetime | None:
    """Parse a timezone-aware ISO-8601 timestamp when available.
    Legacy or malformed values are treated as unknown metadata.
    """

    if not isinstance(value, str) or value == "":
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None

    return parsed.astimezone(UTC)


def _utc_isoformat(*, value: datetime | None) -> str | None:
    """Serialize an aware timestamp as canonical UTC ISO-8601 text.
    Naive values cannot identify a successful UTC refresh.
    """

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        return None

    return value.astimezone(UTC).isoformat()


def _required_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise CardDatabaseError(f"Missing or invalid {field_name}; expected integer.")

    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise CardDatabaseError(
            f"Missing or invalid {field_name}; expected integer."
        ) from error


def _required_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise CardDatabaseError(f"Missing or invalid {field_name}; expected number.")

    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise CardDatabaseError(
            f"Missing or invalid {field_name}; expected number."
        ) from error


def _optional_float(value: Any, *, field_name: str) -> float | None:
    if value is None:
        return None

    return _required_float(value, field_name=field_name)

