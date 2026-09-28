"""Describe Arena draft formats and the pack rules each one follows.
Format detection reads event names only, never pack contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DraftFormat(StrEnum):
    """Arena draft format an event name belongs to.
    Traditional and Premier differ in match rules, not in how packs are picked.
    """

    QUICK = "quick"
    PREMIER = "premier"
    TRADITIONAL = "traditional"
    PICK_TWO = "pick_two"


@dataclass(frozen=True, slots=True)
class DraftRules:
    """Pack layout of one draft format.
    A logical pick takes cards_per_pick cards from the pack in front of the player.
    """

    draft_format: DraftFormat
    pack_count: int
    picks_per_pack: int
    cards_per_pick: int

    def __post_init__(self) -> None:
        if min(self.pack_count, self.picks_per_pack, self.cards_per_pick) < 1:
            raise ValueError(f"Draft rules must be positive: {self!r}")

    @property
    def total_picks(self) -> int:
        """Return the logical picks in a complete draft.
        Pick-Two counts one logical pick for each pair of cards.
        """

        return self.pack_count * self.picks_per_pack

    @property
    def cards_per_pack(self) -> int:
        """Return the cards in a fresh pack.
        Later packs shrink by cards_per_pick after every logical pick.
        """

        return self.picks_per_pack * self.cards_per_pick

    @property
    def total_cards(self) -> int:
        """Return the cards in a complete draft pool.
        It is the pool size Arena reports when the draft ends.
        """

        return self.total_picks * self.cards_per_pick


QUICK_RULES = DraftRules(
    draft_format=DraftFormat.QUICK,
    pack_count=3,
    picks_per_pack=14,
    cards_per_pick=1,
)
PREMIER_RULES = DraftRules(
    draft_format=DraftFormat.PREMIER,
    pack_count=3,
    picks_per_pack=14,
    cards_per_pick=1,
)
TRADITIONAL_RULES = DraftRules(
    draft_format=DraftFormat.TRADITIONAL,
    pack_count=3,
    picks_per_pack=14,
    cards_per_pick=1,
)
PICK_TWO_RULES = DraftRules(
    draft_format=DraftFormat.PICK_TWO,
    pack_count=3,
    picks_per_pack=7,
    cards_per_pick=2,
)

RULES_BY_FORMAT: dict[DraftFormat, DraftRules] = {
    rules.draft_format: rules
    for rules in (QUICK_RULES, PREMIER_RULES, TRADITIONAL_RULES, PICK_TWO_RULES)
}

QUICK_DRAFT_PREFIX = "QuickDraft_"

# PickTwoTradDraft_ appears in 17Lands format lists but in no log we hold.
# PickTwoQuickDraft_ would be a bot draft and stays unsupported.
_FORMAT_BY_PREFIX: dict[str, DraftFormat] = {
    QUICK_DRAFT_PREFIX: DraftFormat.QUICK,
    "PremierDraft_": DraftFormat.PREMIER,
    "TradDraft_": DraftFormat.TRADITIONAL,
    "PickTwoDraft_": DraftFormat.PICK_TWO,
    "PickTwoTradDraft_": DraftFormat.PICK_TWO,
}
# Longest first, so a prefix that extends another one always wins.
DRAFT_EVENT_PREFIXES = tuple(sorted(_FORMAT_BY_PREFIX, key=len, reverse=True))


def detect_draft_format(*, event_name: str) -> DraftFormat | None:
    """Return the draft format for an Arena event name.
    Unknown prefixes return None so callers can leave the active draft alone.
    """

    for prefix in DRAFT_EVENT_PREFIXES:
        if event_name.startswith(prefix):
            return _FORMAT_BY_PREFIX[prefix]

    return None


def rules_for_format(*, draft_format: DraftFormat) -> DraftRules:
    """Return the pack rules of a draft format.
    Mocked Draft events keep the Quick rules despite their variable pack sizes.
    """

    return RULES_BY_FORMAT[draft_format]

