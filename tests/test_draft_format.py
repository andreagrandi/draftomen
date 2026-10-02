from __future__ import annotations

import pytest

from draftomen.draft_format import (
    DRAFT_EVENT_PREFIXES,
    PICK_TWO_RULES,
    PREMIER_RULES,
    QUICK_RULES,
    TRADITIONAL_RULES,
    DraftFormat,
    DraftRules,
    augmented_model_formats,
    detect_draft_format,
    event_format_label,
    ratings_formats,
    rules_for_format,
)


@pytest.mark.parametrize(
    ("rules", "pack_count", "picks_per_pack", "cards_per_pick", "total_cards"),
    [
        (QUICK_RULES, 3, 14, 1, 42),
        (PREMIER_RULES, 3, 14, 1, 42),
        (TRADITIONAL_RULES, 3, 14, 1, 42),
        (PICK_TWO_RULES, 3, 7, 2, 42),
    ],
)
def test_rules_define_pack_layout_for_each_format(
    rules: DraftRules,
    pack_count: int,
    picks_per_pack: int,
    cards_per_pick: int,
    total_cards: int,
) -> None:
    assert rules.pack_count == pack_count
    assert rules.picks_per_pack == picks_per_pack
    assert rules.cards_per_pick == cards_per_pick
    assert rules.total_cards == total_cards
    assert rules.cards_per_pack == 14
    assert rules.total_picks == pack_count * picks_per_pack


@pytest.mark.parametrize("draft_format", list(DraftFormat))
def test_rules_for_format_returns_rules_of_the_same_format(
    draft_format: DraftFormat,
) -> None:
    assert rules_for_format(draft_format=draft_format).draft_format is draft_format


@pytest.mark.parametrize(
    ("event_name", "expected_format"),
    [
        ("QuickDraft_TST_20260101", DraftFormat.QUICK),
        ("QuickDraft_TST_Draftmancer_20260101", DraftFormat.QUICK),
        ("PremierDraft_TST_20260101", DraftFormat.PREMIER),
        ("TradDraft_TST_20260101", DraftFormat.TRADITIONAL),
        ("PickTwoDraft_TST_20260101", DraftFormat.PICK_TWO),
        ("PickTwoTradDraft_TST_20260101", DraftFormat.PICK_TWO),
    ],
)
def test_detect_draft_format_maps_event_prefixes_to_formats(
    event_name: str,
    expected_format: DraftFormat,
) -> None:
    assert detect_draft_format(event_name=event_name) is expected_format


@pytest.mark.parametrize(
    "event_name",
    [
        "",
        "Constructed_BestOf3",
        "PickTwoQuickDraft_TST_20260101",
        "Sealed_TST_20260101",
        "premierdraft_TST_20260101",
        "Draft_TST",
    ],
)
def test_detect_draft_format_returns_none_for_unknown_prefixes(event_name: str) -> None:
    assert detect_draft_format(event_name=event_name) is None


def test_event_prefixes_are_checked_longest_first() -> None:
    lengths = [len(prefix) for prefix in DRAFT_EVENT_PREFIXES]

    assert lengths == sorted(lengths, reverse=True)
    assert DRAFT_EVENT_PREFIXES[0] == "PickTwoTradDraft_"


@pytest.mark.parametrize(
    ("pack_count", "picks_per_pack", "cards_per_pick"),
    [(0, 14, 1), (3, 0, 1), (3, 14, 0), (-1, 14, 1)],
)
def test_rules_reject_non_positive_values(
    pack_count: int,
    picks_per_pack: int,
    cards_per_pick: int,
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        DraftRules(
            draft_format=DraftFormat.QUICK,
            pack_count=pack_count,
            picks_per_pack=picks_per_pack,
            cards_per_pick=cards_per_pick,
        )



@pytest.mark.parametrize(
    ("draft_format", "expected"),
    [
        (DraftFormat.QUICK, ("QuickDraft", "PremierDraft")),
        (DraftFormat.PREMIER, ("PremierDraft",)),
        (DraftFormat.TRADITIONAL, ("TradDraft", "PremierDraft")),
        (DraftFormat.PICK_TWO, ("PickTwoDraft", "PremierDraft")),
    ],
)
def test_ratings_formats_list_exact_format_then_fallbacks(
    draft_format: DraftFormat,
    expected: tuple[str, ...],
) -> None:
    assert ratings_formats(draft_format=draft_format) == expected


@pytest.mark.parametrize(
    ("draft_format", "expected"),
    [
        (DraftFormat.QUICK, ("QuickDraft", "PremierDraft")),
        (DraftFormat.PREMIER, ("PremierDraft",)),
        (DraftFormat.TRADITIONAL, ("TradDraft", "PremierDraft")),
        (DraftFormat.PICK_TWO, ("PickTwoDraft", "PremierDraft")),
    ],
)
def test_augmented_model_formats_always_accept_premier_models(
    draft_format: DraftFormat,
    expected: tuple[str, ...],
) -> None:
    assert augmented_model_formats(draft_format=draft_format) == expected


@pytest.mark.parametrize(
    ("event_format", "label"),
    [
        ("QuickDraft", "Quick"),
        ("premierdraft", "Premier"),
        ("TradDraft", "Trad"),
        ("picktwodraft", "Pick-Two"),
        ("CubeDraft", "CubeDraft"),
    ],
)
def test_event_format_label_names_known_formats(event_format: str, label: str) -> None:
    assert event_format_label(event_format=event_format) == label
