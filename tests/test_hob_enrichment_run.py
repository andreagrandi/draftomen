"""Behavior tests for reviewed HOB mechanic matching against card Oracle evidence."""

from __future__ import annotations

from draftomen.semantic_capability_records import CardCapability
from draftomen.semantic_enrichment_records import FindingReview, FindingStatus, OracleEvidence
from draftomen.semantic_roles import Role
from scripts.hob_enrichment_run import MINIMUM_OVERLAP_CHARS, _oracle_evidence_matches


RUN_ID = "run-1"
STORIED_CARD_ID = 103382
STORIED_QUOTE = (
    "Storied (If you control three or more artifacts, legendaries, and/or Sagas, you have an "
    "enduring story for the rest of the game.)"
)
ADVENTURES_CARD_ID = 103397
ADVENTURES_FACE_QUOTE = (
    "Create X 2/2 red Dwarf creature tokens. (Then exile this card. You may cast the "
    "enchantment later from exile.)"
)
REVIEW_REASON = "capability requires semantic review beyond exact-source validation."


def _entry(
    *,
    card_id: int,
    quote: str,
    face_index: int | None = None,
) -> dict[str, object]:
    """Build one reviewed mechanic expectation around exact Oracle evidence."""
    return {
        "name": "Storied",
        "guide_quote": "get you storied since so many of them are legendary.",
        "oracle_card_id": card_id,
        "oracle_face_index": face_index,
        "oracle_quote": quote,
        "required": True,
    }


def _capability(
    *,
    card_id: int,
    quote: str,
    face_index: int | None = None,
    role: Role = Role.MODIFIED,
    status: FindingStatus = FindingStatus.ACCEPTED,
) -> CardCapability:
    """Build one real capability record around a single Oracle evidence quote."""
    return CardCapability(
        finding_id=f"finding-{card_id}-{face_index}",
        card_id=card_id,
        card_name="Fíli the Pathfinder",
        face_index=face_index,
        face_name=None,
        role=role,
        quantity=None,
        timing=None,
        source_zone=None,
        destination_zone=None,
        prerequisites=(),
        evidence=(OracleEvidence(card_id=card_id, face_index=face_index, quote=quote),),
        review=FindingReview(
            status=status,
            reason=None if status is FindingStatus.ACCEPTED else REVIEW_REASON,
        ),
        run_id=RUN_ID,
    )


def test_exact_expected_span_on_pinned_card_matches() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    capability = _capability(
        card_id=STORIED_CARD_ID,
        quote=STORIED_QUOTE,
        role=Role.ARTIFACT_PAYOFF,
        status=FindingStatus.UNCERTAIN,
    )

    matches = _oracle_evidence_matches(entry=entry, capabilities=[capability])

    assert len(matches) == 1
    assert matches[0]["rule"] == "card-capability-quote"
    assert matches[0]["card_id"] == STORIED_CARD_ID
    assert matches[0]["finding_id"] == capability.finding_id
    assert matches[0]["role"] == Role.ARTIFACT_PAYOFF.value
    assert matches[0]["status"] == FindingStatus.UNCERTAIN.value


def test_quote_on_another_card_does_not_match() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    capability = _capability(card_id=103397, quote=STORIED_QUOTE)

    assert _oracle_evidence_matches(entry=entry, capabilities=[capability]) == []


def test_pinned_face_index_requires_the_matching_face() -> None:
    entry = _entry(
        card_id=ADVENTURES_CARD_ID,
        face_index=1,
        quote=ADVENTURES_FACE_QUOTE,
    )
    other_face = _capability(
        card_id=ADVENTURES_CARD_ID,
        face_index=0,
        quote=ADVENTURES_FACE_QUOTE,
    )
    pinned_face = _capability(
        card_id=ADVENTURES_CARD_ID,
        face_index=1,
        quote=ADVENTURES_FACE_QUOTE,
    )

    assert _oracle_evidence_matches(entry=entry, capabilities=[other_face]) == []
    assert len(_oracle_evidence_matches(entry=entry, capabilities=[pinned_face])) == 1


def test_larger_ability_quote_containing_the_expected_span_matches() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    capability = _capability(
        card_id=STORIED_CARD_ID,
        quote=f"Fíli the Pathfinder — {STORIED_QUOTE}",
    )

    assert len(_oracle_evidence_matches(entry=entry, capabilities=[capability])) == 1


def test_sub_span_matches_only_above_the_overlap_floor() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    long_sub_span = STORIED_QUOTE[: MINIMUM_OVERLAP_CHARS + 10]
    short_sub_span = STORIED_QUOTE[: MINIMUM_OVERLAP_CHARS - 10]

    assert len(long_sub_span) > MINIMUM_OVERLAP_CHARS
    assert len(short_sub_span) < MINIMUM_OVERLAP_CHARS
    assert (
        len(
            _oracle_evidence_matches(
                entry=entry,
                capabilities=[_capability(card_id=STORIED_CARD_ID, quote=long_sub_span)],
            )
        )
        == 1
    )
    assert (
        _oracle_evidence_matches(
            entry=entry,
            capabilities=[_capability(card_id=STORIED_CARD_ID, quote=short_sub_span)],
        )
        == []
    )


def test_capability_on_the_pinned_card_quoting_another_ability_does_not_match() -> None:
    entry = _entry(card_id=STORIED_CARD_ID, quote=STORIED_QUOTE)
    capability = _capability(
        card_id=STORIED_CARD_ID,
        quote="Whenever Fíli or another nontoken Dwarf you control enters, create a 2/2 red Dwarf creature token.",
    )

    assert _oracle_evidence_matches(entry=entry, capabilities=[capability]) == []

