"""Prepare and atomically publish per-set Arena card-data artifacts.

The exporter has a deliberately small boundary: 17Lands supplies the set-code
inventory, while one Scryfall default-cards source supplies all printed card
metadata and set names.  No artifact is written until the complete source has
been parsed and every pending candidate has passed the shared strict validator.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from draftomen.carddb import (
    CardDatabaseError,
    HTTP_TIMEOUT_SECONDS,
    PathInput,
    build_card_database_from_scryfall_cards,
    iter_scryfall_default_cards,
)

from draftomen.set_card_data import SetCardData, SetCardDataError
from draftomen.seventeen import (
    SeventeenLandsBasicLand,
    SeventeenLandsError,
    SeventeenLandsExpansionInventory,
    SeventeenLandsRatedCard,
    fetch_17lands_cards_csv,
    fetch_17lands_expansion_inventory,
    parse_17lands_basic_lands,
    parse_17lands_expansion_inventory,
)


_CARD_DATA_OUTPUT_DIR = Path("website/public/card-data")
_SET_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_EXCLUDED_SET_TOKEN_RE = re.compile(
    r"(?<![a-z0-9])(?:cube|chaos|remix)(?![a-z0-9])",
    re.IGNORECASE,
)
# A full Arena draft set has at least this many distinct Arena card identities
# in the 17Lands inventory intersection; this excludes isolated digital rows.
_MIN_ARENA_IDS_FOR_FULL_DRAFT = 200


class SetDataExportError(RuntimeError):
    """Raised when set discovery, candidate preparation, or publication fails."""


@dataclass(frozen=True, slots=True)
class SetDataIdentity:
    """One eligible set identity from the combined inventory and Scryfall source."""

    set_code: str
    set_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.set_code, str) or _SET_CODE_RE.fullmatch(self.set_code) is None:
            raise SetDataExportError("set_code must be a lowercase path-safe set code.")
        if not isinstance(self.set_name, str) or not self.set_name:
            raise SetDataExportError("set_name must be a non-empty string.")


@dataclass(frozen=True, slots=True)
class PreparedSetDataExport:
    """A validated candidate ready for one atomic publication."""

    identity: SetDataIdentity
    target_path: Path
    gzip_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SetDataIdentity):
            raise SetDataExportError("identity must be a SetDataIdentity.")
        if not isinstance(self.target_path, Path):
            try:
                target_path = Path(self.target_path)
            except (TypeError, ValueError) as error:
                raise SetDataExportError("target_path must be a valid path.") from error
            object.__setattr__(self, "target_path", target_path)
        if not isinstance(self.gzip_bytes, bytes):
            raise SetDataExportError("gzip_bytes must be bytes.")


@dataclass(frozen=True, slots=True)
class SetDataExportPlan:
    """Complete, deterministic export plan produced before any writes."""

    sets: tuple[SetDataIdentity, ...]
    already_valid: tuple[SetDataIdentity, ...]
    pending: tuple[PreparedSetDataExport, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(item, SetDataIdentity) for item in self.sets):
            raise SetDataExportError("sets must contain SetDataIdentity values.")
        if any(not isinstance(item, SetDataIdentity) for item in self.already_valid):
            raise SetDataExportError(
                "already_valid must contain SetDataIdentity values."
            )
        if any(not isinstance(item, PreparedSetDataExport) for item in self.pending):
            raise SetDataExportError(
                "pending must contain PreparedSetDataExport values."
            )
        expected = tuple(sorted(self.sets, key=lambda item: item.set_code.upper()))
        if expected != self.sets:
            raise SetDataExportError("sets must be sorted by set code.")

    @property
    def total(self) -> int:
        """Number of eligible sets in this invocation."""

        return len(self.sets)


def card_data_target_path(*, output_dir: PathInput, set_code: str) -> Path:
    """Return the canonical lower-case target path for one set artifact."""

    if not isinstance(set_code, str):
        raise SetDataExportError("set_code must be a string.")
    normalized_code = set_code.casefold()
    if _SET_CODE_RE.fullmatch(normalized_code) is None:
        raise SetDataExportError("set_code must be a lowercase path-safe set code.")
    try:
        return Path(output_dir) / f"{normalized_code}.json.gz"
    except (TypeError, ValueError) as error:
        raise SetDataExportError("output_dir must be a valid path.") from error


def prepare_set_data_export(
    *,
    selector: str | None,
    output_dir: PathInput = _CARD_DATA_OUTPUT_DIR,
    inventory_file: PathInput | None = None,
    bulk_file: PathInput | None = None,
    cards_file: PathInput | None = None,
    rated_cards: Mapping[str, Sequence[SeventeenLandsRatedCard]] | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> SetDataExportPlan:
    """Discover eligible sets and build all pending canonical candidates.

    The source inventory and Scryfall stream are consumed once.  Existing files
    are classified only after the complete source has been validated, ensuring
    malformed source data can never result in a partial publication.  Sets
    present in rated_cards (keyed by lower-case set code) also receive the
    bonus-sheet cards that 17Lands rates but Scryfall prints in another set.
    """

    _validate_timeout(timeout_seconds=timeout_seconds)
    try:
        inventory_codes = _load_inventory_codes(
            inventory_file=inventory_file,
            timeout_seconds=timeout_seconds,
        )
        source_cards = tuple(
            iter_scryfall_default_cards(
                bulk_file=bulk_file,
                timeout_seconds=timeout_seconds,
            )
        )
        _validate_source_cards(cards=source_cards)
        basic_lands = _load_basic_lands(
            cards_file=cards_file,
            timeout_seconds=timeout_seconds,
        )
        identities = _eligible_identities(
            inventory_codes=inventory_codes,
            source_cards=source_cards,
        )
        selected = _select_identities(identities=identities, selector=selector)
        output = Path(output_dir)
    except SetDataExportError:
        raise
    except (
        CardDatabaseError,
        OSError,
        SeventeenLandsError,
        SetCardDataError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise SetDataExportError(str(error)) from error

    selected_set_codes = {item.set_code for item in selected}
    pending: list[PreparedSetDataExport] = []
    already_valid: list[SetDataIdentity] = []
    for identity in selected:
        target = card_data_target_path(
            output_dir=output,
            set_code=identity.set_code,
        )
        try:
            scryfall_set_cards = tuple(
                card
                for card in source_cards
                if isinstance(card.get("set"), str)
                and card["set"].casefold() == identity.set_code
            )
            set_source_cards = scryfall_set_cards + _missing_basic_land_cards(
                identity=identity,
                scryfall_set_cards=scryfall_set_cards,
                basic_lands=basic_lands,
            )
            set_rated_cards = None if rated_cards is None else rated_cards.get(
                identity.set_code
            )
            if set_rated_cards is not None:
                # Bonus rows go last so the set's own printing keeps any
                # image URI for a name shared with a bonus card.
                set_source_cards += _bonus_sheet_cards(
                    identity=identity,
                    rated_cards=set_rated_cards,
                    set_cards=set_source_cards,
                    source_cards=source_cards,
                )
            # Validate every selected-set row before resolving legitimate
            # duplicate Arena identities from rebalances and print treatments.
            build_card_database_from_scryfall_cards(cards=set_source_cards)
            database = build_card_database_from_scryfall_cards(
                cards=_canonical_source_cards(cards=set_source_cards)
            )
            card_data = SetCardData.from_card_database(
                database,
                set_code=identity.set_code,
                set_name=identity.set_name,
            )
            gzip_bytes = card_data.to_gzip_bytes()
            # The factory and serializer are checked again through the same
            # bounded, canonical reader used by runtime and cache validation.
            validated = SetCardData.from_gzip_bytes(
                gzip_bytes,
                expected_set_code=identity.set_code,
                expected_set_name=identity.set_name,
            )
            if validated.to_gzip_bytes() != gzip_bytes:
                raise SetDataExportError("generated card data is not canonical.")
        except SetDataExportError:
            raise
        except (
            CardDatabaseError,
            OSError,
            RecursionError,
            RuntimeError,
            SetCardDataError,
            SeventeenLandsError,
            TypeError,
            ValueError,
            UnicodeError,
        ) as error:
            raise SetDataExportError(
                f"Could not prepare {identity.set_code}: {error}"
            ) from error

        if selector is None and _is_valid_existing_artifact(
            target=target,
            identity=identity,
            expected_bytes=gzip_bytes if set_rated_cards is not None else None,
        ):
            already_valid.append(identity)
            continue
        pending.append(
            PreparedSetDataExport(
                identity=identity,
                target_path=target,
                gzip_bytes=gzip_bytes,
            )
        )

    if selector is None:
        ordered_sets = tuple(sorted(selected, key=lambda item: item.set_code.upper()))
        ordered_valid = tuple(
            sorted(already_valid, key=lambda item: item.set_code.upper())
        )
        ordered_pending = tuple(
            sorted(pending, key=lambda item: item.identity.set_code.upper())
        )
    else:
        ordered_sets = tuple(selected)
        ordered_valid = ()
        ordered_pending = tuple(pending)

    # Keep this check close to plan construction: accidental source changes or
    # future filtering cannot produce a plan whose counts do not reconcile.
    if {item.set_code for item in ordered_sets} != selected_set_codes:
        raise SetDataExportError("set export plan identities changed unexpectedly.")
    return SetDataExportPlan(
        sets=ordered_sets,
        already_valid=ordered_valid,
        pending=ordered_pending,
    )


def publish_set_data_export(*, candidate: PreparedSetDataExport) -> Path:
    """Validate and atomically replace one prepared artifact target."""

    if not isinstance(candidate, PreparedSetDataExport):
        raise SetDataExportError("candidate must be a PreparedSetDataExport.")
    identity = candidate.identity
    target = Path(candidate.target_path)
    expected_name = f"{identity.set_code}.json.gz"
    if target.name != expected_name:
        raise SetDataExportError("candidate target path is not canonical.")

    try:
        validated = SetCardData.from_gzip_bytes(
            candidate.gzip_bytes,
            expected_set_code=identity.set_code,
            expected_set_name=identity.set_name,
        )
        canonical_bytes = validated.to_gzip_bytes()
        if canonical_bytes != candidate.gzip_bytes:
            raise SetDataExportError("candidate card data is not canonical.")
    except SetDataExportError:
        raise
    except (
        OSError,
        RecursionError,
        RuntimeError,
        SetCardDataError,
        TypeError,
        ValueError,
        UnicodeError,
    ) as error:
        raise SetDataExportError(
            f"Could not validate {identity.set_code} before publication: {error}"
        ) from error

    temporary_name: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=target.parent,
            prefix=f".{target.name}.",
        ) as temporary_file:
            temporary_name = temporary_file.name
            temporary_file.write(candidate.gzip_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
        _fsync_directory(path=target.parent)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SetDataExportError(
            f"Could not publish {identity.set_code}: {error}"
        ) from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
    return target


def resolve_set_card_data(
    *,
    set_code: str,
    output_dir: PathInput = _CARD_DATA_OUTPUT_DIR,
    inventory_file: PathInput | None = None,
    bulk_file: PathInput | None = None,
    cards_file: PathInput | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> Path:
    """Return an existing canonical set artifact or generate and publish it."""

    target = card_data_target_path(output_dir=output_dir, set_code=set_code)
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SetDataExportError(
            f"Could not inspect card data target {target}: {error}"
        ) from error
    else:
        try:
            payload = target.read_bytes()
        except OSError as error:
            raise SetDataExportError(
                f"Could not read card data target {target}: {error}"
            ) from error
        try:
            SetCardData.from_gzip_bytes(
                payload,
                expected_set_code=set_code.casefold(),
            )
        except SetCardDataError as error:
            raise SetDataExportError(
                f"Invalid card data at {target}: {error}"
            ) from error
        return target

    plan = prepare_set_data_export(
        selector=set_code.casefold(),
        output_dir=output_dir,
        inventory_file=inventory_file,
        bulk_file=bulk_file,
        cards_file=cards_file,
        timeout_seconds=timeout_seconds,
    )
    return publish_set_data_export(candidate=plan.pending[0])


def _validate_timeout(*, timeout_seconds: int) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise SetDataExportError("timeout_seconds must be a positive integer.")


def _load_inventory_codes(
    *,
    inventory_file: PathInput | None,
    timeout_seconds: int,
) -> tuple[str, ...]:
    try:
        if inventory_file is None:
            inventory = fetch_17lands_expansion_inventory(
                timeout_seconds=timeout_seconds
            )
        else:
            path = Path(inventory_file)
            payload = json.loads(path.read_text(encoding="utf-8"))
            inventory = parse_17lands_expansion_inventory(payload)
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise SetDataExportError(
            f"Could not load 17Lands expansion inventory: {error}"
        ) from error
    except SeventeenLandsError as error:
        raise SetDataExportError(str(error)) from error

    if not isinstance(inventory, SeventeenLandsExpansionInventory):
        raise SetDataExportError("17Lands expansion inventory has an invalid shape.")
    if inventory.diagnostics:
        raise SetDataExportError("17Lands expansion inventory contains invalid entries.")
    return tuple(
        code.casefold()
        for code in inventory.expansion_codes
        if isinstance(code, str) and _SET_CODE_RE.fullmatch(code.casefold())
    )


def _load_basic_lands(
    *,
    cards_file: PathInput | None,
    timeout_seconds: int,
) -> tuple[SeventeenLandsBasicLand, ...]:
    try:
        if cards_file is None:
            text = fetch_17lands_cards_csv(timeout_seconds=timeout_seconds)
        else:
            text = Path(cards_file).read_text(encoding="utf-8")
        return parse_17lands_basic_lands(text)
    except (OSError, TypeError, ValueError, UnicodeError, SeventeenLandsError) as error:
        raise SetDataExportError(
            f"Could not load 17Lands cards.csv: {error}"
        ) from error


def _missing_basic_land_cards(
    *,
    identity: SetDataIdentity,
    scryfall_set_cards: Iterable[Mapping[str, Any]],
    basic_lands: Iterable[SeventeenLandsBasicLand],
) -> tuple[dict[str, Any], ...]:
    """Build Scryfall-shaped rows for basic lands Scryfall lists without Arena ids.
    Rows are sorted by Arena id so the artifact stays deterministic.
    """

    known_ids = {
        card["arena_id"]
        for card in scryfall_set_cards
        if isinstance(card.get("arena_id"), int)
    }
    missing = sorted(
        {
            land.arena_id: land
            for land in basic_lands
            if land.expansion.casefold() == identity.set_code
            and land.arena_id not in known_ids
        }.values(),
        key=lambda land: land.arena_id,
    )
    return tuple(
        {
            "arena_id": land.arena_id,
            "name": land.name,
            "set": identity.set_code,
            "set_name": identity.set_name,
            "cmc": 0,
            "rarity": "common",
            "type_line": (
                land.types.replace(" - ", " \u2014 ")
                if land.types
                else f"Basic Land \u2014 {land.name}"
            ),
            "colors": [],
            "color_identity": list(land.color_identity),
            "produced_mana": list(land.color_identity),
            "layout": "normal",
        }
        for land in missing
    )


def _front_face_name(card: Mapping[str, Any]) -> str | None:
    faces = card.get("card_faces")
    if isinstance(faces, list) and faces and isinstance(faces[0], Mapping):
        face_name = faces[0].get("name")
        if isinstance(face_name, str) and face_name:
            return face_name
    name = card.get("name")
    if isinstance(name, str) and " // " in name:
        return name.split(" // ", 1)[0]
    return None


def _bonus_sheet_cards(
    *,
    identity: SetDataIdentity,
    rated_cards: Sequence[SeventeenLandsRatedCard],
    set_cards: Iterable[Mapping[str, Any]],
    source_cards: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Build rows for rated cards that the set's own rows do not contain.
    17Lands ids win over Scryfall ids; an unmatched rated name fails the export.
    """

    known_ids = {
        card["arena_id"]
        for card in set_cards
        if isinstance(card.get("arena_id"), int)
    }
    by_arena_id: dict[int, list[Mapping[str, Any]]] = {}
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for card in source_cards:
        arena_id = card.get("arena_id")
        if isinstance(arena_id, int):
            by_arena_id.setdefault(arena_id, []).append(card)
        names = {card.get("name"), _front_face_name(card)}
        for name in names:
            if isinstance(name, str) and name:
                by_name.setdefault(name, []).append(card)

    rows: dict[int, dict[str, Any]] = {}
    unmatched: list[str] = []
    for rated in rated_cards:
        if rated.arena_id in known_ids or rated.arena_id in rows:
            continue
        by_id = by_arena_id.get(rated.arena_id)
        if by_id:
            source = min(by_id, key=_source_card_rank)
        else:
            candidates = by_name.get(rated.name)
            if not candidates:
                unmatched.append(rated.name)
                continue
            latest = max(
                str(card.get("released_at") or "") for card in candidates
            )
            source = min(
                (
                    card
                    for card in candidates
                    if str(card.get("released_at") or "") == latest
                ),
                key=_source_card_rank,
            )
        rows[rated.arena_id] = {
            **source,
            "arena_id": rated.arena_id,
            "set": identity.set_code,
            "set_name": identity.set_name,
        }
    if unmatched:
        raise SetDataExportError(
            f"No Scryfall card matches 17Lands rated cards for {identity.set_code}: "
            + ", ".join(sorted(set(unmatched)))
        )
    return tuple(rows[arena_id] for arena_id in sorted(rows))


def _source_card_identity(
    *,
    card: object,
    index: int,
) -> tuple[str, int] | None:
    """Parse and validate the canonical identity of one source card.
    Cards without an Arena identity are valid paper-only source rows.
    """

    if not isinstance(card, Mapping):
        raise SetDataExportError(
            f"Malformed Scryfall card source at item {index}: expected object."
        )
    arena_id = card.get("arena_id")
    if arena_id is None:
        return None
    if isinstance(arena_id, bool) or not isinstance(arena_id, int) or arena_id <= 0:
        raise SetDataExportError(
            f"Malformed Scryfall card source at item {index}: invalid arena_id."
        )
    set_value = card.get("set")
    if not isinstance(set_value, str) or _SET_CODE_RE.fullmatch(
        set_value.casefold()
    ) is None:
        raise SetDataExportError(
            f"Malformed Scryfall card source at item {index}: "
            "invalid or missing path-safe set code."
        )
    return set_value.casefold(), arena_id


def _validate_source_cards(*, cards: Iterable[object]) -> None:
    """Validate all source rows before set discovery or artifact inspection.
    No malformed Arena-bearing source row may reach eligibility grouping.
    """

    for index, card in enumerate(cards, start=1):
        _source_card_identity(card=card, index=index)


def _canonical_source_cards(
    *,
    cards: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Resolve duplicate Arena identities to one deterministic printing.
    Original printings precede rebalances, digital variants, and treatments.
    """

    card_objects = tuple(cards)
    identities = [
        _source_card_identity(card=card, index=index)
        for index, card in enumerate(card_objects, start=1)
    ]
    winners: dict[tuple[str, int], Mapping[str, Any]] = {}
    for card, identity in zip(card_objects, identities, strict=True):
        if identity is None:
            continue
        previous = winners.get(identity)
        if previous is None or _source_card_rank(card) < _source_card_rank(previous):
            winners[identity] = card

    canonical: list[Mapping[str, Any]] = []
    emitted: set[tuple[str, int]] = set()
    for card, identity in zip(card_objects, identities, strict=True):
        if identity is None:
            canonical.append(card)
            continue
        if identity in emitted:
            continue
        canonical.append(winners[identity])
        emitted.add(identity)
    return tuple(canonical)


def _source_card_rank(card: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the stable preference key for duplicate Scryfall printings.
    Collector number and Scryfall ID resolve otherwise equivalent treatments.
    """
    collector_value = card.get("collector_number")
    collector = (
        collector_value.casefold()
        if isinstance(collector_value, str)
        else ""
    )
    name_value = card.get("name")
    name = name_value.casefold() if isinstance(name_value, str) else ""
    collector_match = re.fullmatch(r"(\d+)(.*)", collector)
    if collector_match is None:
        collector_rank: tuple[int, int, str] = (1, 0, collector)
    else:
        collector_rank = (
            0,
            int(collector_match.group(1)),
            collector_match.group(2),
        )
    return (
        collector.startswith("a-") or name.startswith("a-"),
        card.get("digital") is True,
        collector_rank,
        str(card.get("id", "")),
        json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _eligible_identities(
    *,
    inventory_codes: Iterable[str],
    source_cards: Iterable[Mapping[str, Any]],
) -> tuple[SetDataIdentity, ...]:
    inventory = {
        code.casefold()
        for code in inventory_codes
        if isinstance(code, str) and _SET_CODE_RE.fullmatch(code.casefold())
    }
    names_by_code: dict[str, set[str]] = {}
    arena_ids_by_code: dict[str, set[int]] = {}
    for index, card in enumerate(source_cards, start=1):
        identity = _source_card_identity(card=card, index=index)
        if identity is None:
            continue
        code, arena_id = identity
        if code not in inventory:
            continue
        set_name = card.get("set_name")
        if not isinstance(set_name, str) or not set_name:
            raise SetDataExportError(
                f"Scryfall set {code!r} has a card without set_name."
            )
        names_by_code.setdefault(code, set()).add(set_name)
        arena_ids_by_code.setdefault(code, set()).add(arena_id)

    identities: list[SetDataIdentity] = []
    for code in sorted(names_by_code):
        names = names_by_code[code]
        if len(names) != 1:
            raise SetDataExportError(
                f"Scryfall set {code!r} has conflicting set names."
            )
        if len(arena_ids_by_code[code]) < _MIN_ARENA_IDS_FOR_FULL_DRAFT:
            continue
        name = next(iter(names))
        if _EXCLUDED_SET_TOKEN_RE.search(code) or _EXCLUDED_SET_TOKEN_RE.search(name):
            continue
        try:
            identities.append(SetDataIdentity(set_code=code, set_name=name))
        except (TypeError, ValueError, SetDataExportError) as error:
            raise SetDataExportError(
                f"Could not prepare eligible set {code}: {error}"
            ) from error
    return tuple(sorted(identities, key=lambda item: item.set_code.upper()))


def _select_identities(
    *,
    identities: tuple[SetDataIdentity, ...],
    selector: str | None,
) -> tuple[SetDataIdentity, ...]:
    if selector is None:
        return identities
    if not isinstance(selector, str):
        raise SetDataExportError("set selector must be a string.")
    normalized = selector.strip().casefold()
    if not normalized:
        raise SetDataExportError("set selector must not be empty.")
    matches = tuple(
        identity
        for identity in identities
        if identity.set_code.casefold() == normalized
        or identity.set_name.casefold() == normalized
    )
    if not matches:
        raise SetDataExportError(f"No eligible set matches {selector.strip()!r}.")
    if len(matches) > 1:
        raise SetDataExportError(
            f"Set selector {selector.strip()!r} is ambiguous."
        )
    return matches


def _is_valid_existing_artifact(
    *,
    target: Path,
    identity: SetDataIdentity,
    expected_bytes: bytes | None = None,
) -> bool:
    try:
        payload = target.read_bytes()
        SetCardData.from_gzip_bytes(
            payload,
            expected_set_code=identity.set_code,
            expected_set_name=identity.set_name,
        )
    except (OSError, SetCardDataError, TypeError, ValueError, UnicodeError):
        return False
    return expected_bytes is None or payload == expected_bytes


def _fsync_directory(*, path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


__all__ = [
    "PreparedSetDataExport",
    "SetDataExportError",
    "SetDataExportPlan",
    "SetDataIdentity",
    "card_data_target_path",
    "prepare_set_data_export",
    "publish_set_data_export",
    "resolve_set_card_data",
]
