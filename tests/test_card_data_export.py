from __future__ import annotations

import gzip
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import draftomen.card_data_export as card_data_export
from draftomen.card_data_export import (
    SetDataExportError,
    card_data_target_path,
    prepare_set_data_export,
    publish_set_data_export,
    resolve_set_card_data,
)
from draftomen.set_card_data import SetCardData
from draftomen.seventeen import parse_17lands_expansion_inventory

_PRODUCTION_MIN_ARENA_IDS_FOR_FULL_DRAFT = (
    card_data_export._MIN_ARENA_IDS_FOR_FULL_DRAFT
)


@pytest.fixture(autouse=True)
def _allow_compact_arena_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(card_data_export, "_MIN_ARENA_IDS_FOR_FULL_DRAFT", 1)


def _card(
    arena_id: int | None,
    set_code: str,
    set_name: str,
    *,
    name: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "arena_id": arena_id,
        "name": name or f"{set_code.upper()} Card {arena_id}",
        "colors": ["G"],
        "cmc": 3,
        "rarity": "common",
        "type_line": "Creature — Human",
        "set": set_code,
        "set_name": set_name,
        "collector_number": str(arena_id or 0),
    }
    card.update(overrides)
    return card


def _write_sources(
    directory: Path,
    inventory: list[str],
    cards: list[dict[str, Any]],
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    inventory_path = directory / "inventory.json"
    bulk_path = directory / "default-cards.jsonl"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    bulk_path.write_text(
        "".join(json.dumps(card, ensure_ascii=False) + "\n" for card in cards),
        encoding="utf-8",
    )
    return inventory_path, bulk_path


def _published_hob_payload() -> bytes:
    return (
        Path(__file__).resolve().parents[1]
        / "website"
        / "public"
        / "card-data"
        / "hob.json.gz"
    ).read_bytes()


def _forbid_source_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unexpected card-data source acquisition")

    monkeypatch.setattr(
        card_data_export,
        "fetch_17lands_expansion_inventory",
        fail_if_called,
    )
    monkeypatch.setattr(
        card_data_export,
        "iter_scryfall_default_cards",
        fail_if_called,
    )


def _canonical_gzip(value: dict[str, Any]) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    output = io.BytesIO()
    with gzip.GzipFile(
        fileobj=output,
        mode="wb",
        filename="",
        mtime=0,
        compresslevel=9,
    ) as stream:
        stream.write(raw)
    return output.getvalue()


def _prepare(
    tmp_path: Path,
    inventory: list[str],
    cards: list[dict[str, Any]],
    *,
    selector: str | None = None,
    output_dir: Path | None = None,
) -> Any:
    inventory_path, bulk_path = _write_sources(tmp_path, inventory, cards)
    return prepare_set_data_export(
        selector=selector,
        output_dir=output_dir or (tmp_path / "card-data"),
        inventory_file=inventory_path,
        bulk_file=bulk_path,
    )


def test_target_path_and_export_identity_are_canonical_lowercase(tmp_path: Path) -> None:
    assert card_data_target_path(output_dir=tmp_path, set_code="AbC-1") == (
        tmp_path / "abc-1.json.gz"
    )

    plan = _prepare(
        tmp_path,
        ["ABC"],
        [_card(11, "AbC", "Alpha Set")],
        selector="abc",
    )
    candidate = plan.pending[0]
    assert candidate.identity.set_code == "abc"
    assert candidate.target_path == tmp_path / "card-data" / "abc.json.gz"

    artifact = SetCardData.from_gzip_bytes(
        candidate.gzip_bytes,
        expected_set_code="ABC",
        expected_set_name="Alpha Set",
    )
    assert artifact.set_code == "abc"
    assert all(card.set_code == "abc" for card in artifact.cards)


def test_selector_matches_exact_code_or_full_name_case_insensitively(tmp_path: Path) -> None:
    cards = [
        _card(11, "aaa", "Alpha Set"),
        _card(12, "bbb", "Beta Set"),
    ]
    by_code = _prepare(tmp_path / "code", ["AAA", "BBB"], cards, selector=" aAa ")
    by_name = _prepare(tmp_path / "name", ["AAA", "BBB"], cards, selector="beta set")

    assert [item.set_code for item in by_code.sets] == ["aaa"]
    assert [item.set_code for item in by_name.sets] == ["bbb"]
    with pytest.raises(SetDataExportError, match="No eligible set matches"):
        _prepare(tmp_path / "missing", ["AAA", "BBB"], cards, selector="alp")


def test_selector_rejects_code_name_ambiguity(tmp_path: Path) -> None:
    cards = [
        _card(11, "abc", "XYZ"),
        _card(12, "xyz", "Other Set"),
    ]
    with pytest.raises(SetDataExportError, match="ambiguous"):
        _prepare(tmp_path, ["ABC", "XYZ"], cards, selector="xyz")


def test_eligibility_intersects_inventory_and_scryfall_arena_printings(
    tmp_path: Path,
) -> None:
    cards = [
        _card(1, "aaa", "Arena Set"),
        _card(None, "paper", "Paper Only Set"),
        _card(2, "cube", "Cube Set"),
        _card(3, "chaos", "Chaos Set"),
        _card(4, "remix", "Remix Set"),
        _card(5, "xyz", "Remix Masters"),
        _card(6, "not-in-inventory", "Not In Inventory"),
    ]
    plan = _prepare(
        tmp_path,
        ["AAA", "PAPER", "CUBE", "CHAOS", "REMIX", "XYZ"],
        cards,
    )

    assert [item.set_code for item in plan.sets] == ["aaa"]
    assert [item.identity.set_code for item in plan.pending] == ["aaa"]


def test_production_arena_threshold_excludes_isolated_paper_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _PRODUCTION_MIN_ARENA_IDS_FOR_FULL_DRAFT == 200
    monkeypatch.setattr(
        card_data_export,
        "_MIN_ARENA_IDS_FOR_FULL_DRAFT",
        _PRODUCTION_MIN_ARENA_IDS_FOR_FULL_DRAFT,
    )
    cards = [
        _card(None, "paper", "Paper Only Set"),
        _card(1, "paper", "Paper Only Set"),
        *(_card(index, "full", "Full Arena Set") for index in range(1, 201)),
    ]

    plan = _prepare(tmp_path, ["PAPER", "FULL"], cards)

    assert [item.set_code for item in plan.sets] == ["full"]
    assert len(plan.pending) == 1


def test_local_inventory_and_bulk_sources_make_zero_network_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path, bulk_path = _write_sources(
        tmp_path,
        ["AAA"],
        [_card(1, "aaa", "Arena Set")],
    )

    def unexpected_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline exporter inputs must not use the network")

    monkeypatch.setattr(
        "draftomen.card_data_export.fetch_17lands_expansion_inventory",
        unexpected_network,
    )
    monkeypatch.setattr("draftomen.carddb.urllib.request.urlopen", unexpected_network)
    plan = prepare_set_data_export(
        selector=None,
        output_dir=tmp_path / "out",
        inventory_file=inventory_path,
        bulk_file=bulk_path,
    )
    assert plan.total == 1


def test_missing_inventory_fetches_17lands_expansions_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bulk_path = tmp_path / "default-cards.jsonl"
    bulk_path.write_text(json.dumps(_card(1, "aaa", "Arena Set")) + "\n", encoding="utf-8")
    calls: list[int] = []
    inventory = parse_17lands_expansion_inventory(["AAA"])

    def fake_fetch(*, timeout_seconds: int) -> Any:
        calls.append(timeout_seconds)
        return inventory

    monkeypatch.setattr(
        "draftomen.card_data_export.fetch_17lands_expansion_inventory", fake_fetch
    )
    plan = prepare_set_data_export(
        selector=None,
        output_dir=tmp_path / "out",
        bulk_file=bulk_path,
        timeout_seconds=19,
    )

    assert plan.total == 1
    assert calls == [19]


def test_remote_scryfall_source_uses_one_metadata_and_one_bulk_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text('["AAA"]', encoding="utf-8")
    metadata = {
        "data": [
            {
                "type": "default_cards",
                "download_uri": "https://example.invalid/default.jsonl.gz",
            }
        ]
    }
    bulk_payload = gzip.compress(
        (json.dumps(_card(1, "aaa", "Arena Set")) + "\n").encode("utf-8")
    )
    calls: list[tuple[str, int]] = []

    class Response(io.BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def fake_urlopen(request: Any, timeout: int) -> Response:
        url = request.full_url
        calls.append((url, timeout))
        if url == "https://api.scryfall.com/bulk-data":
            return Response(json.dumps(metadata).encode("utf-8"))
        if url == "https://example.invalid/default.jsonl.gz":
            return Response(bulk_payload)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("draftomen.carddb.urllib.request.urlopen", fake_urlopen)
    plan = prepare_set_data_export(
        selector=None,
        output_dir=tmp_path / "out",
        inventory_file=inventory_path,
        timeout_seconds=23,
    )

    assert plan.total == 1
    assert calls == [
        ("https://api.scryfall.com/bulk-data", 23),
        ("https://example.invalid/default.jsonl.gz", 23),
    ]


def test_scryfall_source_is_consumed_once_by_exporter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text('["AAA"]', encoding="utf-8")
    rows = [_card(1, "aaa", "Arena Set")]

    class OneShotRows:
        def __init__(self, values: list[dict[str, Any]]) -> None:
            self.values = values
            self.iterations = 0

        def __iter__(self) -> Any:
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("Scryfall source was consumed more than once")
            return iter(self.values)

    source = OneShotRows(rows)
    monkeypatch.setattr(
        "draftomen.card_data_export.iter_scryfall_default_cards", lambda **kwargs: source
    )

    plan = prepare_set_data_export(
        selector=None,
        output_dir=tmp_path / "out",
        inventory_file=inventory_path,
    )

    assert plan.total == 1
    assert source.iterations == 1


def test_duplicate_arena_ids_across_sets_remain_set_scoped(tmp_path: Path) -> None:
    plan = _prepare(
        tmp_path,
        ["ONS", "HA3"],
        [
            _card(18321, "ons", "Onslaught", name="Krosan Tusker"),
            _card(18321, "ha3", "Historic Anthology 3", name="Krosan Tusker"),
        ],
    )

    assert [item.identity.set_code for item in plan.pending] == ["ha3", "ons"]
    for candidate in plan.pending:
        artifact = SetCardData.from_gzip_bytes(
            candidate.gzip_bytes,
            expected_set_code=candidate.identity.set_code,
            expected_set_name=candidate.identity.set_name,
        )
        assert [card.grp_id for card in artifact.cards] == [18321]
        assert {card.set_code for card in artifact.cards} == {
            candidate.identity.set_code
        }


@pytest.mark.parametrize("reverse", [False, True])
def test_same_set_duplicate_prefers_original_printing(
    tmp_path: Path,
    reverse: bool,
) -> None:
    cards = [
        _card(
            77119,
            "afr",
            "Adventures in the Forgotten Realms",
            name="A-Dwarfhold Champion",
            collector_number="A-14",
            digital=True,
        ),
        _card(
            77119,
            "afr",
            "Adventures in the Forgotten Realms",
            name="Dwarfhold Champion",
            collector_number="14",
            digital=False,
        ),
    ]
    if reverse:
        cards.reverse()

    candidate = _prepare(tmp_path, ["AFR"], cards).pending[0]
    artifact = SetCardData.from_gzip_bytes(
        candidate.gzip_bytes,
        expected_set_code="afr",
        expected_set_name="Adventures in the Forgotten Realms",
    )

    assert len(artifact.cards) == 1
    assert artifact.cards[0].name == "Dwarfhold Champion"
    assert artifact.cards[0].collector_number == "14"


def test_same_set_treatments_prefer_lowest_numeric_collector_number(
    tmp_path: Path,
) -> None:
    candidate = _prepare(
        tmp_path,
        ["MUL"],
        [
            _card(
                85519,
                "mul",
                "Multiverse Legends",
                name="Anafenza, Kin-Tree Spirit",
                collector_number="131",
            ),
            _card(
                85519,
                "mul",
                "Multiverse Legends",
                name="Anafenza, Kin-Tree Spirit",
                collector_number="66",
            ),
        ],
    ).pending[0]
    artifact = SetCardData.from_gzip_bytes(
        candidate.gzip_bytes,
        expected_set_code="mul",
        expected_set_name="Multiverse Legends",
    )

    assert len(artifact.cards) == 1
    assert artifact.cards[0].collector_number == "66"


@pytest.mark.parametrize(
    ("label", "cards"),
    [
        (
            "conflicting set names",
            [_card(1, "aaa", "First Name"), _card(2, "aaa", "Second Name")],
        ),
        (
            "invalid arena ID",
            [_card(0, "aaa", "Arena Set")],
        ),
        (
            "missing relevant set name",
            [_card(1, "aaa", "Arena Set")],
        ),
    ],
)
def test_malformed_relevant_source_fails_before_any_artifact_write(
    tmp_path: Path,
    label: str,
    cards: list[dict[str, Any]],
) -> None:
    if label == "missing relevant set name":
        cards[0].pop("set_name")
    with pytest.raises(SetDataExportError):
        _prepare(tmp_path, ["AAA", "BBB"], cards, output_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("malformed_set", ["missing", "empty", "unsafe"])
def test_malformed_arena_set_does_not_replace_valid_target(
    tmp_path: Path,
    malformed_set: str,
) -> None:
    output_dir = tmp_path / "out"
    seed = _prepare(
        tmp_path / "seed",
        ["AAA"],
        [_card(1, "aaa", "Arena Set")],
        output_dir=output_dir,
    )
    publish_set_data_export(candidate=seed.pending[0])
    target = output_dir / "aaa.json.gz"
    original = target.read_bytes()

    malformed = _card(2, "aaa", "Arena Set")
    if malformed_set == "missing":
        malformed.pop("set")
    elif malformed_set == "empty":
        malformed["set"] = ""
    else:
        malformed["set"] = "../aaa"

    with pytest.raises(SetDataExportError, match="set code"):
        _prepare(
            tmp_path / f"bad-{malformed_set}",
            ["AAA"],
            [malformed],
            output_dir=output_dir,
        )

    assert target.read_bytes() == original


def test_plan_counts_and_order_strictly_skip_only_valid_existing_siblings(
    tmp_path: Path,
) -> None:
    cards = [
        _card(30, "ccc", "Gamma Set"),
        _card(10, "aaa", "Alpha Set"),
        _card(20, "bbb", "Beta Set"),
    ]
    first = _prepare(tmp_path / "seed", ["AAA", "BBB", "CCC"], cards, selector="bbb")
    publish_set_data_export(candidate=first.pending[0])

    output_dir = tmp_path / "seed" / "card-data"
    plan = _prepare(
        tmp_path / "rerun",
        ["AAA", "BBB", "CCC"],
        cards,
        output_dir=output_dir,
    )

    assert plan.total == 3
    assert [item.set_code for item in plan.sets] == ["aaa", "bbb", "ccc"]
    assert [item.set_code for item in plan.already_valid] == ["bbb"]
    assert [item.identity.set_code for item in plan.pending] == ["aaa", "ccc"]

    invalid_candidate = _prepare(
        tmp_path / "invalid-source",
        ["AAA", "BBB", "CCC"],
        cards,
        selector="aaa",
        output_dir=output_dir,
    ).pending[0]
    invalid_payload = json.loads(gzip.decompress(invalid_candidate.gzip_bytes))
    invalid_payload["cards"][0]["set_code"] = "bbb"
    (output_dir / "aaa.json.gz").write_bytes(_canonical_gzip(invalid_payload))
    invalid_plan = _prepare(
        tmp_path / "rerun-invalid",
        ["AAA", "BBB", "CCC"],
        cards,
        output_dir=output_dir,
    )
    assert [item.set_code for item in invalid_plan.already_valid] == ["bbb"]
    assert [item.identity.set_code for item in invalid_plan.pending] == ["aaa", "ccc"]


def test_single_set_selection_always_rebuilds_even_when_target_is_valid(tmp_path: Path) -> None:
    cards = [_card(1, "aaa", "Arena Set")]
    first = _prepare(tmp_path, ["AAA"], cards, selector="AAA")
    publish_set_data_export(candidate=first.pending[0])

    selected_again = _prepare(tmp_path, ["AAA"], cards, selector="AAA")
    assert selected_again.already_valid == ()
    assert len(selected_again.pending) == 1


def test_resolver_reuses_published_artifact_without_source_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "card-data"
    output_dir.mkdir()
    target = output_dir / "hob.json.gz"
    original = _published_hob_payload()
    target.write_bytes(original)
    _forbid_source_acquisition(monkeypatch)

    result = resolve_set_card_data(set_code="HOB", output_dir=output_dir)

    assert result == target
    assert result.read_bytes() == original


def test_resolver_publishes_only_the_requested_missing_set(tmp_path: Path) -> None:
    inventory_file, bulk_file = _write_sources(
        tmp_path / "sources",
        ["HOB", "AAA"],
        [
            _card(1, "hob", "The Hobbit"),
            _card(2, "aaa", "Alpha Set"),
        ],
    )
    output_dir = tmp_path / "card-data"

    result = resolve_set_card_data(
        set_code="HOB",
        output_dir=output_dir,
        inventory_file=inventory_file,
        bulk_file=bulk_file,
    )

    assert result == output_dir / "hob.json.gz"
    assert sorted(path.name for path in output_dir.iterdir()) == ["hob.json.gz"]
    artifact = SetCardData.from_gzip_bytes(
        result.read_bytes(),
        expected_set_code="hob",
        expected_set_name="The Hobbit",
    )
    payload = artifact.to_json()
    assert payload["set_code"] == "hob"
    assert payload["set_name"] == "The Hobbit"


def test_resolver_rejects_invalid_and_wrong_set_existing_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "card-data"
    output_dir.mkdir()
    target = output_dir / "hob.json.gz"
    invalid_payload = b"not a gzip file"
    target.write_bytes(invalid_payload)
    wrong_set_plan = _prepare(
        tmp_path / "aaa-source",
        ["AAA"],
        [_card(1, "aaa", "Alpha Set")],
        selector="AAA",
    )
    wrong_set_payload = wrong_set_plan.pending[0].gzip_bytes
    _forbid_source_acquisition(monkeypatch)

    with pytest.raises(SetDataExportError, match="hob.json.gz"):
        resolve_set_card_data(set_code="HOB", output_dir=output_dir)
    assert target.read_bytes() == invalid_payload

    target.write_bytes(wrong_set_payload)
    with pytest.raises(SetDataExportError, match="hob.json.gz"):
        resolve_set_card_data(set_code="HOB", output_dir=output_dir)
    assert target.read_bytes() == wrong_set_payload


def test_resolver_rejects_unsupported_missing_set_without_replacing_siblings(
    tmp_path: Path,
) -> None:
    inventory_file, bulk_file = _write_sources(
        tmp_path / "sources",
        ["HOB"],
        [_card(1, "hob", "The Hobbit")],
    )
    output_dir = tmp_path / "card-data"
    output_dir.mkdir()
    target = output_dir / "hob.json.gz"
    original = _published_hob_payload()
    target.write_bytes(original)

    with pytest.raises(SetDataExportError, match="No eligible set matches 'zzz'"):
        resolve_set_card_data(
            set_code="ZZZ",
            output_dir=output_dir,
            inventory_file=inventory_file,
            bulk_file=bulk_file,
        )

    assert target.read_bytes() == original
    assert sorted(path.name for path in output_dir.iterdir()) == ["hob.json.gz"]


def test_resolver_treats_dangling_symlink_as_an_existing_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "card-data"
    output_dir.mkdir()
    target = output_dir / "hob.json.gz"
    try:
        target.symlink_to(tmp_path / "missing-target.json.gz")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    _forbid_source_acquisition(monkeypatch)

    with pytest.raises(SetDataExportError, match="hob.json.gz"):
        resolve_set_card_data(set_code="HOB", output_dir=output_dir)

    assert target.is_symlink()
    assert not target.exists()


def test_schema_preserves_database_fields_and_proves_mixed_set_membership(
    tmp_path: Path,
) -> None:
    cards = [
        _card(
            2,
            "aaa",
            "Alpha Set",
            name="Mana Keeper",
            colors=["W", "U"],
            cmc=2,
            rarity="rare",
            type_line="Creature — Wizard",
            mana_cost="{W}{U}",
            produced_mana=["W", "U"],
            oracle_text="Draw a card.",
            oracle_id="oracle-mana-keeper",
            image_uris={"normal": "https://example.invalid/card.jpg"},
        ),
        _card(1, "aaa", "Alpha Set", name="Alpha One"),
        _card(3, "bbb", "Beta Set", name="Beta One"),
    ]
    plan = _prepare(tmp_path, ["AAA", "BBB"], cards)
    assert [item.identity.set_code for item in plan.pending] == ["aaa", "bbb"]

    alpha = SetCardData.from_gzip_bytes(
        plan.pending[0].gzip_bytes,
        expected_set_code="aaa",
        expected_set_name="Alpha Set",
    )
    payload = alpha.to_json()
    assert set(payload) == {
        "cards",
        "image_uris_by_name",
        "schema_version",
        "set_code",
        "set_name",
        "source",
    }
    assert payload["set_code"] == "aaa"
    assert all(card["set_code"] == "aaa" for card in payload["cards"])
    assert all("seen" not in card and "pick_rate" not in card for card in payload["cards"])
    mana_keeper = next(card for card in alpha.cards if card.name == "Mana Keeper")
    assert mana_keeper.mana_cost == "{W}{U}"
    assert mana_keeper.produced_mana == ("W", "U")
    assert mana_keeper.oracle_text == "Draw a card."
    assert alpha.to_card_database().lookup(grp_id=2).set_code == "aaa"
    assert alpha.to_card_database().lookup(grp_id=2).image_uri == (
        "https://example.invalid/card.jpg"
    )
    assert {card.name for card in SetCardData.from_gzip_bytes(plan.pending[1].gzip_bytes).cards} == {
        "Beta One"
    }


def test_zero_eligible_arena_printings_produces_empty_plan(tmp_path: Path) -> None:
    plan = _prepare(
        tmp_path,
        ["AAA"],
        [_card(None, "aaa", "Paper Only Set")],
    )
    assert plan.total == 0
    assert plan.already_valid == ()
    assert plan.pending == ()


def test_resume_after_interruption_starts_with_first_still_pending(tmp_path: Path) -> None:
    cards = [
        _card(1, "aaa", "Alpha Set"),
        _card(2, "bbb", "Beta Set"),
        _card(3, "ccc", "Gamma Set"),
    ]
    initial = _prepare(tmp_path, ["AAA", "BBB", "CCC"], cards)
    with pytest.raises(RuntimeError, match="interrupt"):
        publish_set_data_export(candidate=initial.pending[0])
        raise RuntimeError("interrupt")

    resumed = _prepare(tmp_path, ["AAA", "BBB", "CCC"], cards)
    assert [item.set_code for item in resumed.already_valid] == ["aaa"]
    assert [item.identity.set_code for item in resumed.pending] == ["bbb", "ccc"]


@pytest.mark.parametrize("missing_field", ["name", "cmc", "rarity"])
def test_malformed_duplicate_loser_fails_before_deduplication(
    tmp_path: Path,
    missing_field: str,
) -> None:
    valid = _card(
        77119,
        "afr",
        "Adventures in the Forgotten Realms",
        name="Dwarfhold Champion",
        collector_number="14",
        digital=False,
    )
    duplicate_loser = _card(
        77119,
        "afr",
        "Adventures in the Forgotten Realms",
        name="A-Dwarfhold Champion",
        collector_number="A-14",
        digital=True,
    )
    duplicate_loser.pop(missing_field)

    with pytest.raises(SetDataExportError, match=missing_field):
        _prepare(
            tmp_path,
            ["AFR"],
            [valid, duplicate_loser],
            selector="AFR",
        )


@pytest.mark.parametrize("failure", ["invalid", "temporary", "fsync", "replace"])
def test_failed_publication_never_replaces_existing_valid_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    initial = _prepare(
        tmp_path,
        ["AAA"],
        [_card(1, "aaa", "Arena Set")],
        selector="AAA",
    )
    candidate = initial.pending[0]
    publish_set_data_export(candidate=candidate)
    target = candidate.target_path
    original = target.read_bytes()

    if failure == "invalid":
        candidate = replace(candidate, gzip_bytes=b"invalid gzip")
    elif failure == "temporary":
        def fail_temp(*args: object, **kwargs: object) -> None:
            raise OSError("injected temporary write failure")

        monkeypatch.setattr(card_data_export.tempfile, "NamedTemporaryFile", fail_temp)
    elif failure == "fsync":
        def fail_fsync(*args: object, **kwargs: object) -> None:
            raise OSError("injected fsync failure")

        monkeypatch.setattr(card_data_export.os, "fsync", fail_fsync)
    else:
        def fail_replace(*args: object, **kwargs: object) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr(card_data_export.os, "replace", fail_replace)

    with pytest.raises(SetDataExportError):
        publish_set_data_export(candidate=candidate)
    assert target.read_bytes() == original
    assert not list(target.parent.glob(f".{target.name}.*"))


def test_directory_fsync_failure_after_replace_is_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _prepare(
        tmp_path,
        ["AAA"],
        [_card(1, "aaa", "Arena Set")],
        selector="AAA",
    )
    candidate = plan.pending[0]
    real_fsync = card_data_export.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync is unsupported")
        real_fsync(descriptor)

    monkeypatch.setattr(card_data_export.os, "fsync", fail_directory_fsync)

    published = publish_set_data_export(candidate=candidate)

    assert published == candidate.target_path
    assert SetCardData.from_gzip_bytes(
        published.read_bytes(),
        expected_set_code="aaa",
        expected_set_name="Arena Set",
    ).set_code == "aaa"
    assert calls == 2


def test_publication_rejects_candidate_with_noncanonical_target_path(tmp_path: Path) -> None:
    plan = _prepare(
        tmp_path,
        ["AAA"],
        [_card(1, "aaa", "Arena Set")],
        selector="AAA",
    )
    candidate = replace(plan.pending[0], target_path=tmp_path / "wrong.json.gz")
    with pytest.raises(SetDataExportError, match="canonical"):
        publish_set_data_export(candidate=candidate)
    assert not (tmp_path / "wrong.json.gz").exists()
