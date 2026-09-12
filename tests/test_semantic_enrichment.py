from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Inexact, Rounded, localcontext
import hashlib
import json
from typing import Any

import pytest

from draftomen.carddb import CardFace, CardInfo
import draftomen.semantic_enrichment as semantic_enrichment_module
from draftomen.semantic_capability_records import (
    CapabilityQuantity,
    CapabilityZone,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import (
    SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
    EnrichmentSources,
    GuideSource,
    SemanticEnrichmentArtifact,
    card_source_sha256,
    set_source_sha256,
)
from draftomen.semantic_enrichment_records import (
    ArtifactReview,
    CardSourcePin,
    FindingReview,
    FindingStatus,
    GuideClaim,
    GuideEvidence,
    GuideSourcePin,
    ModelRun,
    OracleEvidence,
    OracleFact,
    ReasoningConfig,
    RejectedFinding,
    SemanticEnrichmentError,
)
from draftomen.semantic_relationship_records import (
    CardRelationship,
    PrerequisiteProjectionError,
    RelationshipParticipant,
    RelationshipPrerequisite,
    RelationshipPrerequisiteProjection,
    RelationshipTiming,
    RelationshipZone,
)
from draftomen.semantic_roles import Role


STARTED_AT = "2026-09-01T12:00:00Z"
COMPLETED_AT = "2026-09-01T12:01:00Z"
REVIEWED_AT = "2026-09-01T13:00:00+01:00"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _review(status: FindingStatus = FindingStatus.ACCEPTED) -> FindingReview:
    return FindingReview(status=status, reason=None if status is FindingStatus.ACCEPTED else "needs review")


def _oracle_evidence(card_id: int, quote: str = "Draw a card.") -> OracleEvidence:
    return OracleEvidence(card_id=card_id, face_index=None, quote=quote)


def _guide_evidence(guide_id: str = "guide-1", quote: str = "Draw matters.") -> GuideEvidence:
    return GuideEvidence(guide_id=guide_id, quote=quote)


def _reasoning() -> ReasoningConfig:
    return ReasoningConfig(enabled=True, effort=" high ", max_tokens=512, exclude=False)


def _run(
    run_id: str = "run-1",
    *,
    reasoning: ReasoningConfig | None = None,
    input_tokens: int | None = 20,
    output_tokens: int | None = 0,
    reasoning_tokens: int | None = None,
    cost_usd: str | None = "0.0100",
) -> ModelRun:
    return ModelRun(
        run_id=run_id,
        provider=" provider ",
        model=" model ",
        reasoning=_reasoning() if reasoning is None else reasoning,
        prompt_id="prompt-1",
        prompt_sha256=DIGEST_A,
        response_schema_id="schema-1",
        response_schema_sha256=DIGEST_B,
        started_at=STARTED_AT,
        completed_at=COMPLETED_AT,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=cost_usd,
    )


def test_every_leaf_record_round_trips_with_exact_declared_keys() -> None:
    finding_review = _review()
    oracle_evidence = _oracle_evidence(42)
    guide_evidence = _guide_evidence()
    records = (
        finding_review,
        oracle_evidence,
        guide_evidence,
        OracleFact(
            finding_id="fact-1",
            card_id=42,
            kind=" rules text ",
            claim="Draw a card.",
            evidence=(oracle_evidence,),
            review=finding_review,
            run_id="run-1",
        ),
        GuideClaim(
            finding_id="claim-1",
            category=" mechanic ",
            name=" draw ",
            claim="Draw matters.",
            card_ids=(42,),
            evidence=(guide_evidence,),
            review=finding_review,
            run_id="run-1",
        ),
        CardRelationship(
            finding_id="relationship-1",
            mechanism=" Draw Engine ",
            participants=(43, 42),
            claim="These cards work together.",
            prerequisites=("The activation cost can be paid.",),
            oracle_evidence=(
                OracleEvidence(card_id=43, face_index=0, quote="Create a token."),
                oracle_evidence,
            ),
            guide_evidence=(guide_evidence,),
            review=finding_review,
            run_id="run-1",
        ),
        RejectedFinding(
            finding_id="rejected-1",
            source_kind=" relationship ",
            summary="A malformed relationship.",
            reason="It lacked exact evidence.",
            run_id="run-1",
        ),
        CardSourcePin(card_id=42, oracle_id=" oracle-42 ", collector_number=" 1 ", sha256=DIGEST_A),
        GuideSourcePin(
            guide_id=" guide-1 ",
            url=" https://example.test/guide ",
            sha256=DIGEST_B,
            retrieved_at=REVIEWED_AT,
        ),
        ReasoningConfig(enabled=None, effort=" careful ", max_tokens=None, exclude=None),
        _run(),
        ArtifactReview(state="confirmed", reviewer_id=" reviewer ", reviewed_at=REVIEWED_AT),
    )
    expected_keys = (
        {"status", "reason"},
        {"card_id", "face_index", "quote"},
        {"guide_id", "quote"},
        {"finding_id", "card_id", "kind", "claim", "evidence", "review", "run_id"},
        {"finding_id", "category", "name", "claim", "card_ids", "evidence", "review", "run_id"},
        {
            "finding_id",
            "mechanism",
            "participants",
            "claim",
            "prerequisites",
            "oracle_evidence",
            "guide_evidence",
            "review",
            "run_id",
        },
        {"finding_id", "source_kind", "summary", "reason", "run_id"},
        {"card_id", "oracle_id", "collector_number", "sha256"},
        {"guide_id", "url", "sha256", "retrieved_at"},
        {"enabled", "effort", "max_tokens", "exclude"},
        {
            "run_id",
            "provider",
            "model",
            "reasoning",
            "prompt_id",
            "prompt_sha256",
            "response_schema_id",
            "response_schema_sha256",
            "started_at",
            "completed_at",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cost_usd",
        },
        {"state", "reviewer_id", "reviewed_at"},
    )
    classes = (
        FindingReview,
        OracleEvidence,
        GuideEvidence,
        OracleFact,
        GuideClaim,
        CardRelationship,
        RejectedFinding,
        CardSourcePin,
        GuideSourcePin,
        ReasoningConfig,
        ModelRun,
        ArtifactReview,
    )
    for record, record_class, keys in zip(records, classes, expected_keys, strict=True):
        encoded = record.to_json()
        assert set(encoded) == keys
        assert record_class.from_json(encoded) == record


def test_model_run_preserves_null_and_reported_zero_counts_and_normalizes_cost() -> None:
    reported_zero = _run(input_tokens=0, output_tokens=0, reasoning_tokens=0, cost_usd="1.50")
    unreported = _run(input_tokens=None, output_tokens=None, reasoning_tokens=None, cost_usd="0")

    assert reported_zero.cost_usd == "1.5"
    assert unreported.cost_usd == "0"
    assert ModelRun.from_json(reported_zero.to_json()).input_tokens == 0
    assert ModelRun.from_json(unreported.to_json()).input_tokens is None


def test_cost_normalization_cases() -> None:
    for supplied, normalized in (("0.0100", "0.01"), ("0", "0"), ("1.50", "1.5")):
        assert _run(cost_usd=supplied).cost_usd == normalized

    for invalid in (1.5, True, "NaN", "Infinity", "-0.1"):
        with pytest.raises(SemanticEnrichmentError):
            _run(cost_usd=invalid)  # type: ignore[arg-type]


def test_cost_normalization_is_independent_of_ambient_decimal_context() -> None:
    cases = (
        ("0.0100", "0.01"),
        ("1.50", "1.5"),
        ("1.234", "1.234"),
        ("0", "0"),
        ("-0", "0"),
        ("0.0000", "0"),
        ("1E-7", "0.0000001"),
        (
            "0.12345678901234567890123456789",
            "0.12345678901234567890123456789",
        ),
    )
    for supplied, expected in cases:
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            assert _run(cost_usd=supplied).cost_usd == expected

    exact_cost = "0.12345678901234567890123456789"
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        tight_run = _run(cost_usd=exact_cost)
        tight = _artifact(runs=(tight_run,))
    normal_run = _run(cost_usd=exact_cost)
    normal = _artifact(runs=(normal_run,))

    assert tight == normal
    assert tight.to_bytes() == normal.to_bytes()
    assert tight.runs[0].cost_usd == normal.runs[0].cost_usd == exact_cost
    assert (
        ModelRun.from_json(tight_run.to_json()).cost_usd
        == ModelRun.from_json(normal_run.to_json()).cost_usd
    )


def test_artifact_rejects_lone_surrogates_from_direct_construction() -> None:
    surrogate = "\ud800"
    factories = (
        lambda: replace(_fact(), claim=surrogate),
        lambda: replace(_fact(), kind=surrogate),
        lambda: replace(_guide_claim(), name=surrogate),
        lambda: replace(_relationship(), mechanism=surrogate),
        lambda: replace(_relationship(), prerequisites=(surrogate,)),
        lambda: RejectedFinding(
            finding_id="rejected",
            source_kind="oracle",
            summary=surrogate,
            reason="why",
            run_id="run",
        ),
        lambda: _guide_evidence(guide_id=surrogate),
        lambda: replace(_run(), model=surrogate),
        lambda: CardSourcePin(
            card_id=1,
            oracle_id=None,
            collector_number=surrogate,
            sha256=DIGEST_A,
        ),
        lambda: GuideSource(
            guide_id="guide",
            url="url",
            text=surrogate,
            retrieved_at=STARTED_AT,
        ),
        lambda: EnrichmentSources(
            set_code=surrogate,
            cards=_default_cards(),
            guides=_default_guides(),
        ),
        lambda: _sources(
            cards=(
                _source_card(70221, surrogate, WITCH_ORACLE_TEXT),
                *_default_cards()[1:],
            )
        ),
    )
    for factory in factories:
        with pytest.raises(SemanticEnrichmentError):
            factory()


def test_artifact_rejects_escaped_surrogates_from_json() -> None:
    sources = _sources()
    fact = replace(_fact(), claim="unique claim")
    artifact = _artifact(sources=sources, oracle_facts=(fact,))
    payload = artifact.to_bytes()
    needle = b'"claim":"unique claim"'
    replacement = b'"claim":"\\ud800"'
    assert payload.count(needle) == 1
    tampered = payload.replace(needle, replacement, 1)
    assert tampered != payload
    assert needle not in tampered
    assert replacement in tampered
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(tampered, sources=sources)

    value = _artifact_json(artifact)
    value["oracle_facts"][0]["claim"] = "\ud800"  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=sources)


def test_valid_non_bmp_unicode_round_trips_exactly() -> None:
    joker = "\U0001F0CF"
    double_struck_x = "𝕏"
    cjk = "中文"
    base_sources = _sources()
    base_cards = _default_cards()
    guide_text = f"{GUIDE_TEXT} {joker} {double_struck_x} {cjk}"
    guide = replace(base_sources.guides[0], text=guide_text)
    quote = f"{WITCH_DRAW_QUOTE} {joker} {double_struck_x} {cjk}"
    card = replace(base_cards[0], oracle_text=f"{WITCH_ORACLE_TEXT}\n{quote}")
    sources = _sources(
        cards=(card, *base_cards[1:]),
        guides=(guide, *base_sources.guides[1:]),
    )
    fact = replace(
        _fact(),
        claim=f"Claim {joker} {double_struck_x} {cjk}",
        evidence=(_oracle_evidence(70221, quote),),
    )
    guide_claim = replace(
        _guide_claim(),
        claim=guide_text,
        evidence=(_guide_evidence("guide-1", guide_text),),
    )
    artifact = _artifact(
        sources=sources,
        oracle_facts=(fact,),
        guide_claims=(guide_claim,),
    )
    payload = artifact.to_bytes()

    assert joker.encode("utf-8") in payload
    assert double_struck_x.encode("utf-8") in payload
    assert cjk.encode("utf-8") in payload
    assert b"\\ud83c\\udccf" not in payload
    restored = SemanticEnrichmentArtifact.from_bytes(payload, sources=sources)
    assert restored == artifact
    assert restored.to_bytes() == payload


def test_tuple_constructor_arguments_are_strict() -> None:
    with pytest.raises(SemanticEnrichmentError):
        OracleFact(
            finding_id="fact",
            card_id=1,
            kind="kind",
            claim="claim",
            evidence=[_oracle_evidence(1)],  # type: ignore[arg-type]
            review=_review(),
            run_id="run",
        )
    with pytest.raises(SemanticEnrichmentError):
        GuideClaim(
            finding_id="claim",
            category="mechanic",
            name="name",
            claim="claim",
            card_ids="1",  # type: ignore[arg-type]
            evidence=(_guide_evidence(),),
            review=_review(),
            run_id="run",
        )
    with pytest.raises(SemanticEnrichmentError):
        CardRelationship(
            finding_id="relationship",
            mechanism="mechanism",
            participants=(1, 2),
            claim="claim",
            prerequisites=("requires payment",),
            oracle_evidence=(_oracle_evidence(1), _oracle_evidence(2, "Make a token.")),
            guide_evidence="guide quote",  # type: ignore[arg-type]
            review=_review(),
            run_id="run",
        )


def test_bool_is_rejected_where_an_integer_is_required() -> None:
    with pytest.raises(SemanticEnrichmentError):
        OracleEvidence(card_id=True, face_index=None, quote="quote")  # type: ignore[arg-type]
    with pytest.raises(SemanticEnrichmentError):
        OracleEvidence(card_id=1, face_index=False, quote="quote")  # type: ignore[arg-type]
    with pytest.raises(SemanticEnrichmentError):
        ReasoningConfig(enabled=None, effort=None, max_tokens=True, exclude=None)  # type: ignore[arg-type]
    with pytest.raises(SemanticEnrichmentError):
        _run(input_tokens=True)  # type: ignore[arg-type]


def test_finding_status_requires_enum_directly_but_decodes_from_json() -> None:
    with pytest.raises(SemanticEnrichmentError):
        FindingReview(status="accepted", reason=None)  # type: ignore[arg-type]

    encoded = {"status": "accepted", "reason": None}
    restored = FindingReview.from_json(encoded)
    assert restored.status is FindingStatus.ACCEPTED


def test_uncertain_and_rejected_reviews_require_reasons() -> None:
    with pytest.raises(SemanticEnrichmentError):
        FindingReview(status=FindingStatus.UNCERTAIN, reason=None)
    with pytest.raises(SemanticEnrichmentError):
        FindingReview(status=FindingStatus.REJECTED, reason="  ")
    assert FindingReview(status=FindingStatus.ACCEPTED, reason=None).reason is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: OracleEvidence(card_id=1, face_index=None, quote="  "),
        lambda: GuideEvidence(guide_id="guide", quote="\t"),
        lambda: OracleFact(
            finding_id="fact",
            card_id=1,
            kind="  ",
            claim="claim",
            evidence=(_oracle_evidence(1),),
            review=_review(),
            run_id="run",
        ),
        lambda: GuideClaim(
            finding_id="claim",
            category="mechanic",
            name="  ",
            claim="claim",
            card_ids=(),
            evidence=(_guide_evidence(),),
            review=_review(),
            run_id="run",
        ),
        lambda: CardRelationship(
            finding_id="relationship",
            mechanism=" \t ",
            participants=(1, 2),
            claim="claim",
            prerequisites=("requires payment",),
            oracle_evidence=(_oracle_evidence(1), _oracle_evidence(2, "Make a token.")),
            guide_evidence=(),
            review=_review(),
            run_id="run",
        ),
        lambda: RejectedFinding(
            finding_id="rejected", source_kind="oracle", summary=" ", reason="why", run_id="run"
        ),
        lambda: RejectedFinding(
            finding_id="rejected", source_kind="oracle", summary="summary", reason="\n", run_id="run"
        ),
    ],
)
def test_blank_required_strings_raise(factory: object) -> None:
    with pytest.raises(SemanticEnrichmentError):
        factory()  # type: ignore[operator]


def test_claim_and_quote_text_are_preserved_exactly() -> None:
    claim = "  Exact café claim\n"
    quote = "\tExact café quote  "
    evidence = OracleEvidence(card_id=1, face_index=None, quote=quote)
    fact = OracleFact(
        finding_id=" fact ",
        card_id=1,
        kind=" kind ",
        claim=claim,
        evidence=(evidence,),
        review=_review(),
        run_id=" run ",
    )
    assert fact.claim == claim
    assert fact.evidence[0].quote == quote


def test_unordered_collections_are_sorted_and_duplicates_rejected() -> None:
    first = OracleEvidence(card_id=1, face_index=1, quote="later")
    second = OracleEvidence(card_id=1, face_index=None, quote="earlier")
    fact = OracleFact(
        finding_id="fact",
        card_id=1,
        kind="kind",
        claim="claim",
        evidence=(first, second),
        review=_review(),
        run_id="run",
    )
    assert fact.evidence == (second, first)
    with pytest.raises(SemanticEnrichmentError):
        OracleFact(
            finding_id="fact",
            card_id=1,
            kind="kind",
            claim="claim",
            evidence=(first, first),
            review=_review(),
            run_id="run",
        )

    claim = GuideClaim(
        finding_id="claim",
        category="mechanic",
        name="name",
        claim="claim",
        card_ids=(3, 1, 2),
        evidence=(_guide_evidence(),),
        review=_review(),
        run_id="run",
    )
    assert claim.card_ids == (1, 2, 3)
    with pytest.raises(SemanticEnrichmentError):
        GuideClaim(
            finding_id="claim",
            category="mechanic",
            name="name",
            claim="claim",
            card_ids=(1, 1),
            evidence=(_guide_evidence(),),
            review=_review(),
            run_id="run",
        )

    relationship = CardRelationship(
        finding_id="relationship",
        mechanism=" Engine ",
        participants=(3, 1, 2),
        claim="claim",
        prerequisites=("z", "a"),
        oracle_evidence=(_oracle_evidence(3, "z"), _oracle_evidence(1), _oracle_evidence(2, "y")),
        guide_evidence=(_guide_evidence("guide-2", "z"), _guide_evidence("guide-1", "a")),
        review=_review(),
        run_id="run",
    )
    assert relationship.mechanism == "engine"
    assert relationship.participants == (1, 2, 3)
    assert relationship.prerequisites == ("a", "z")
    assert [item.card_id for item in relationship.oracle_evidence] == [1, 2, 3]
    assert [item.guide_id for item in relationship.guide_evidence] == ["guide-1", "guide-2"]
    with pytest.raises(SemanticEnrichmentError):
        CardRelationship(
            finding_id="relationship",
            mechanism="engine",
            participants=(1, 1),
            claim="claim",
            prerequisites=("requires",),
            oracle_evidence=(_oracle_evidence(1), _oracle_evidence(1, "other")),
            guide_evidence=(),
            review=_review(),
            run_id="run",
        )
    with pytest.raises(SemanticEnrichmentError):
        CardRelationship(
            finding_id="relationship",
            mechanism="engine",
            participants=(1, 2),
            claim="claim",
            prerequisites=("requires", "requires"),
            oracle_evidence=(_oracle_evidence(1), _oracle_evidence(2, "other")),
            guide_evidence=(),
            review=_review(),
            run_id="run",
        )
    with pytest.raises(SemanticEnrichmentError):
        CardRelationship(
            finding_id="relationship",
            mechanism="engine",
            participants=(1, 2),
            claim="claim",
            prerequisites=("requires",),
            oracle_evidence=(_oracle_evidence(1), _oracle_evidence(1)),
            guide_evidence=(),
            review=_review(),
            run_id="run",
        )


def test_unknown_missing_and_wrong_root_json_values_raise() -> None:
    encoded = _oracle_evidence(1).to_json()
    with pytest.raises(SemanticEnrichmentError):
        OracleEvidence.from_json([encoded])  # type: ignore[arg-type]
    with pytest.raises(SemanticEnrichmentError):
        OracleEvidence.from_json({"card_id": 1, "face_index": None, "quote": "quote", "extra": True})
    with pytest.raises(SemanticEnrichmentError):
        OracleEvidence.from_json({"card_id": 1, "face_index": None})
    with pytest.raises(SemanticEnrichmentError):
        OracleEvidence.from_json({"card_id": 1, "face_index": (None,), "quote": "quote"})


def test_records_are_frozen_and_json_collections_are_fresh() -> None:
    evidence = _oracle_evidence(1)
    fact = OracleFact(
        finding_id="fact",
        card_id=1,
        kind="kind",
        claim="claim",
        evidence=(evidence,),
        review=_review(),
        run_id="run",
    )
    with pytest.raises((FrozenInstanceError, AttributeError)):
        fact.claim = "changed"  # type: ignore[misc]
    encoded = fact.to_json()
    assert isinstance(encoded["evidence"], list)
    encoded["evidence"].clear()  # type: ignore[union-attr]
    assert len(fact.evidence) == 1


def test_timestamps_normalize_to_utc_and_reject_naive_values() -> None:
    pin = GuideSourcePin(
        guide_id="guide",
        url="https://example.test",
        sha256=DIGEST_A,
        retrieved_at="2026-09-01T13:00:00+01:00",
    )
    assert pin.retrieved_at == "2026-09-01T12:00:00Z"
    with pytest.raises(SemanticEnrichmentError):
        GuideSourcePin(
            guide_id="guide",
            url="https://example.test",
            sha256=DIGEST_A,
            retrieved_at="2026-09-01T12:00:00",
        )


def test_reasoning_config_preserves_nullable_values_and_effort() -> None:
    config = ReasoningConfig(enabled=None, effort="  custom  ", max_tokens=None, exclude=None)
    restored = ReasoningConfig.from_json(config.to_json())
    assert restored == config
    assert restored.effort == "custom"
    assert restored.enabled is None
    assert restored.max_tokens is None


def test_card_source_pin_normalizes_uppercase_digest() -> None:
    pin = CardSourcePin(card_id=1, oracle_id=None, collector_number=None, sha256="A" * 64)
    assert pin.sha256 == "a" * 64


def test_artifact_review_state_rules() -> None:
    assert ArtifactReview(state="pending", reviewer_id=None, reviewed_at=None).to_json() == {
        "state": "pending",
        "reviewer_id": None,
        "reviewed_at": None,
    }
    confirmed = ArtifactReview(state="confirmed", reviewer_id=" reviewer ", reviewed_at=REVIEWED_AT)
    cancelled = ArtifactReview(state="cancelled", reviewer_id=" reviewer ", reviewed_at=REVIEWED_AT)
    assert confirmed.reviewer_id == "reviewer"
    assert cancelled.state == "cancelled"
    for state, reviewer_id, reviewed_at in (
        ("pending", "reviewer", None),
        ("pending", None, REVIEWED_AT),
        ("confirmed", None, REVIEWED_AT),
        ("confirmed", "reviewer", None),
        ("cancelled", "  ", REVIEWED_AT),
    ):
        with pytest.raises(SemanticEnrichmentError):
            ArtifactReview(state=state, reviewer_id=reviewer_id, reviewed_at=reviewed_at)
WITCH_ORACLE_TEXT = (
    "When this artifact enters, scry 2. "
    "(Look at the top two cards of your library, then put any number of them on "
    "the bottom and the rest on top in any order.)\n"
    "{3}{U}, Sacrifice this artifact: Draw two cards."
)
WITCH_DRAW_QUOTE = "{3}{U}, Sacrifice this artifact: Draw two cards."
ALLIANCE_ORACLE_TEXT = (
    "Whenever you draw your second card each turn, create a 1/1 blue Faerie "
    "creature token with flying.\n{4}{U}{R}: Draw a card, then discard a card."
)
ALLIANCE_TRIGGER_QUOTE = (
    "Whenever you draw your second card each turn, create a 1/1 blue Faerie "
    "creature token with flying."
)
COW_ORACLE_TEXT = (
    "When this creature dies and when you discard this card, create a Food token. "
    "(It's an artifact with \"{2}, {T}, Sacrifice this token: You gain 3 life.\")"
)
COW_TRIGGER_QUOTE = "When this creature dies and when you discard this card, create a Food token."
FACE_FRONT_TEXT = "When this face enters, create a Clue token."
FACE_BACK_TEXT = "Sacrifice this face: Draw a card."
GUIDE_TEXT = "Blue-red rewards drawing your second card each turn."
GUIDE_TWO_TEXT = "Food rewards careful resource management."
ARTIFACT_CREATED_AT = "2026-09-01T12:02:00Z"
ARTIFACT_REVIEWED_AT = "2026-09-01T14:00:00Z"
FACE_CARD_ID = 90000


def _source_card(
    card_id: int,
    name: str,
    oracle_text: str | None,
    *,
    type_line: str = "Artifact",
    faces: tuple[CardFace, ...] = (),
    collector_number: str | None = None,
) -> CardInfo:
    return CardInfo(
        grp_id=card_id,
        name=name,
        colors=("U",),
        mana_value=2.0,
        rarity="common",
        types=("Artifact",),
        oracle_text=oracle_text,
        type_line=type_line,
        layout="normal" if not faces else "transform",
        faces=faces,
        set_code="ELD",
        collector_number=str(card_id) if collector_number is None else collector_number,
        arena_id=card_id,
        oracle_id=f"oracle-{card_id}",
        image_uri=f"https://images.example.test/{card_id}.jpg",
        source_provenance=("synthetic",),
    )


def _default_cards() -> tuple[CardInfo, ...]:
    return (
        _source_card(70221, "Witching Well", WITCH_ORACLE_TEXT),
        _source_card(70340, "Improbable Alliance", ALLIANCE_ORACLE_TEXT),
        _source_card(70153, "Bartered Cow", COW_ORACLE_TEXT, type_line="Creature — Ox"),
        _source_card(
            FACE_CARD_ID,
            "Two-Sided Test Card",
            None,
            type_line="Creature",
            faces=(
                CardFace(
                    name="Two-Sided Test Card",
                    type_line="Creature",
                    oracle_text=FACE_FRONT_TEXT,
                ),
                CardFace(
                    name="Two-Sided Test Card // Other Side",
                    type_line="Creature",
                    oracle_text=FACE_BACK_TEXT,
                ),
            ),
        ),
    )


def _default_guides() -> tuple[GuideSource, ...]:
    return (
        GuideSource(
            guide_id="guide-1",
            url="guide://synthetic/one",
            text=GUIDE_TEXT,
            retrieved_at="2026-09-01T12:00:00+00:00",
        ),
        GuideSource(
            guide_id="guide-2",
            url="not-a-url",
            text=GUIDE_TWO_TEXT,
            retrieved_at="2026-09-01T12:01:00Z",
        ),
    )


def _sources(
    *,
    cards: tuple[CardInfo, ...] | None = None,
    guides: tuple[GuideSource, ...] | None = None,
) -> EnrichmentSources:
    return EnrichmentSources(
        set_code=" ELD ",
        cards=_default_cards() if cards is None else cards,
        guides=_default_guides() if guides is None else guides,
    )


def _card_pins(sources: EnrichmentSources) -> tuple[CardSourcePin, ...]:
    return tuple(
        CardSourcePin(
            card_id=card.grp_id,
            oracle_id=card.oracle_id,
            collector_number=card.collector_number,
            sha256=card_source_sha256(card),
        )
        for card in sources.cards
    )


def _guide_pins(sources: EnrichmentSources) -> tuple[GuideSourcePin, ...]:
    return tuple(
        GuideSourcePin(
            guide_id=guide.guide_id,
            url=guide.url,
            sha256=guide.text_sha256,
            retrieved_at=guide.retrieved_at,
        )
        for guide in sources.guides
    )


def _fact(
    finding_id: str = "fact-1",
    *,
    card_id: int = 70221,
    quote: str = WITCH_DRAW_QUOTE,
    run_id: str = "run-1",
    review: FindingReview | None = None,
) -> OracleFact:
    return OracleFact(
        finding_id=finding_id,
        card_id=card_id,
        kind="rules text",
        claim="This card draws two cards.",
        evidence=(OracleEvidence(card_id=card_id, face_index=None, quote=quote),),
        review=_review() if review is None else review,
        run_id=run_id,
    )


def _face_fact(
    finding_id: str = "face-fact",
    *,
    face_index: int | None = 0,
    quote: str = FACE_FRONT_TEXT,
) -> OracleFact:
    return OracleFact(
        finding_id=finding_id,
        card_id=FACE_CARD_ID,
        kind="face rules text",
        claim="The selected face creates a Clue.",
        evidence=(
            OracleEvidence(card_id=FACE_CARD_ID, face_index=face_index, quote=quote),
        ),
        review=_review(),
        run_id="run-1",
    )


def _guide_claim(
    finding_id: str = "claim-1",
    *,
    run_id: str = "run-1",
    review: FindingReview | None = None,
) -> GuideClaim:
    return GuideClaim(
        finding_id=finding_id,
        category="strategy",
        name="second-card rewards",
        claim=GUIDE_TEXT,
        card_ids=(70221, 70340),
        evidence=(GuideEvidence(guide_id="guide-1", quote=GUIDE_TEXT),),
        review=_review() if review is None else review,
        run_id=run_id,
    )


def _relationship(
    finding_id: str = "relationship-1",
    *,
    mechanism: str = "draw-engine",
    participants: tuple[int, ...] = (70221, 70340),
    review: FindingReview | None = None,
    run_id: str = "run-1",
) -> CardRelationship:
    evidence_by_card = {
        70221: WITCH_DRAW_QUOTE,
        70340: ALLIANCE_TRIGGER_QUOTE,
        70153: COW_TRIGGER_QUOTE,
    }
    return CardRelationship(
        finding_id=finding_id,
        mechanism=mechanism,
        participants=participants,
        claim="The cards support a second-card trigger.",
        prerequisites=(
            "The second-card trigger has not already occurred this turn.",
        ),
        oracle_evidence=tuple(
            OracleEvidence(card_id=card_id, face_index=None, quote=evidence_by_card[card_id])
            for card_id in participants
        ),
        guide_evidence=(
            GuideEvidence(guide_id="guide-1", quote=GUIDE_TEXT),
        ),
        review=_review() if review is None else review,
        run_id=run_id,
    )


def _artifact(
    *,
    sources: EnrichmentSources | None = None,
    review: ArtifactReview | None = None,
    confirmed_relationship_ids: tuple[str, ...] = (),
    runs: tuple[ModelRun, ...] | None = None,
    oracle_facts: tuple[OracleFact, ...] | None = None,
    guide_claims: tuple[GuideClaim, ...] | None = None,
    relationships: tuple[CardRelationship, ...] | None = None,
    rejected_findings: tuple[RejectedFinding, ...] = (),
) -> SemanticEnrichmentArtifact:
    actual_sources = _sources() if sources is None else sources
    return SemanticEnrichmentArtifact(
        schema_version=SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
        set_code=" ELD ",
        set_source_id="synthetic-carddb-2026",
        set_source_sha256=set_source_sha256(actual_sources),
        created_at=ARTIFACT_CREATED_AT,
        cards=_card_pins(actual_sources),
        guides=_guide_pins(actual_sources),
        runs=(_run("run-2"), _run("run-1")) if runs is None else runs,
        oracle_facts=(_fact(),) if oracle_facts is None else oracle_facts,
        guide_claims=(_guide_claim(),) if guide_claims is None else guide_claims,
        relationships=(_relationship(),) if relationships is None else relationships,
        rejected_findings=rejected_findings,
        review=(
            ArtifactReview(state="pending", reviewer_id=None, reviewed_at=None)
            if review is None
            else review
        ),
        confirmed_relationship_ids=confirmed_relationship_ids,
        sources=actual_sources,
    )


def _artifact_json(artifact: SemanticEnrichmentArtifact) -> dict[str, object]:
    return json.loads(artifact.to_bytes().decode("utf-8"))


def test_source_context_normalizes_and_hashes_only_semantic_projection() -> None:
    sources = _sources()
    assert sources.set_code == "eld"
    assert [card.grp_id for card in sources.cards] == [70153, 70221, 70340, FACE_CARD_ID]
    assert [guide.guide_id for guide in sources.guides] == ["guide-1", "guide-2"]
    guide = sources.guides[0]
    assert guide.text == GUIDE_TEXT
    assert guide.text_sha256 == hashlib.sha256(GUIDE_TEXT.encode("utf-8")).hexdigest()

    card = sources.cards[0]
    changed_metadata = replace(
        card,
        image_uri="different-image",
        mana_value=99.0,
        rarity="mythic",
        colors=("R",),
        types=("Creature",),
        keywords=("changed",),
        subtypes=("changed",),
        produced_mana=("R",),
        mana_cost="{9}",
        power="9",
        toughness="9",
        arena_id=None,
        source_provenance=("different",),
        unknown=False,
    )
    assert card_source_sha256(changed_metadata) == card_source_sha256(card)
    reversed_sources = _sources(
        cards=tuple(reversed(sources.cards)),
        guides=tuple(reversed(sources.guides)),
    )
    assert set_source_sha256(reversed_sources) == set_source_sha256(sources)
    reversed_faces = replace(
        sources.cards[-1],
        faces=tuple(reversed(sources.cards[-1].faces)),
    )
    assert card_source_sha256(reversed_faces) != card_source_sha256(sources.cards[-1])
    projection = {
        "card_id": 70153,
        "oracle_id": "oracle-70153",
        "set_code": "eld",
        "collector_number": "70153",
        "name": "Bartered Cow",
        "layout": "normal",
        "type_line": "Creature — Ox",
        "oracle_text": COW_ORACLE_TEXT,
        "faces": [],
    }
    expected_bytes = (
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    assert card_source_sha256(card) == hashlib.sha256(expected_bytes).hexdigest()
    malformed_scalars = (
        replace(card, oracle_id=1),  # type: ignore[arg-type]
        replace(card, set_code=1),  # type: ignore[arg-type]
        replace(card, collector_number=1),  # type: ignore[arg-type]
        replace(card, name=1),  # type: ignore[arg-type]
        replace(card, layout=1),  # type: ignore[arg-type]
    )
    for malformed in malformed_scalars:
        with pytest.raises(SemanticEnrichmentError):
            card_source_sha256(malformed)
    malformed_face = replace(
        sources.cards[-1],
        faces=(replace(sources.cards[-1].faces[0], name=1),),  # type: ignore[arg-type]
    )
    with pytest.raises(SemanticEnrichmentError):
        card_source_sha256(malformed_face)


def test_source_context_rejects_untrusted_and_malformed_sources() -> None:
    sources = _sources()
    bad_card_collections: tuple[object, ...] = (
        list(sources.cards),
        iter(sources.cards),
        {"cards": sources.cards},
        "cards",
    )
    for cards in bad_card_collections:
        with pytest.raises(SemanticEnrichmentError):
            _sources(cards=cards)  # type: ignore[arg-type]
    for guides in (list(sources.guides), iter(sources.guides), {"guides": sources.guides}):
        with pytest.raises(SemanticEnrichmentError):
            _sources(guides=guides)  # type: ignore[arg-type]
    with pytest.raises(SemanticEnrichmentError):
        _sources(cards=())
    with pytest.raises(SemanticEnrichmentError):
        _sources(cards=(replace(sources.cards[0], unknown=True), *sources.cards[1:]))
    with pytest.raises(SemanticEnrichmentError):
        _sources(cards=(replace(sources.cards[0], grp_id=True), *sources.cards[1:]))  # type: ignore[arg-type]
    with pytest.raises(SemanticEnrichmentError):
        _sources(cards=(replace(sources.cards[0], name="  "), *sources.cards[1:]))
    with pytest.raises(SemanticEnrichmentError):
        _sources(cards=(replace(sources.cards[0], set_code="other"), *sources.cards[1:]))
    with pytest.raises(SemanticEnrichmentError):
        _sources(cards=(replace(sources.cards[0], arena_id=1), *sources.cards[1:]))
    with pytest.raises(SemanticEnrichmentError):
        _sources(cards=(replace(sources.cards[0], faces=[]), *sources.cards[1:]))  # type: ignore[arg-type]
    with pytest.raises(SemanticEnrichmentError):
        _sources(
            cards=(
                replace(sources.cards[0], faces=(object(),)),
                *sources.cards[1:],
            )  # type: ignore[arg-type]
        )
    with pytest.raises(SemanticEnrichmentError):
        _sources(cards=(sources.cards[0], sources.cards[0], *sources.cards[1:]))
    with pytest.raises(SemanticEnrichmentError):
        GuideSource(
            guide_id="guide",
            url="url",
            text="   ",
            retrieved_at=STARTED_AT,
        )
    with pytest.raises(SemanticEnrichmentError):
        GuideSource(
            guide_id="guide",
            url="url",
            text=GUIDE_TEXT,
            retrieved_at="2026-09-01T12:00:00",
        )
    with pytest.raises(SemanticEnrichmentError):
        _sources(
            guides=(
                sources.guides[0],
                replace(sources.guides[0], guide_id="guide-1"),
            )
        )


def test_artifact_round_trip_preserves_findings_provenance_and_exact_text() -> None:
    uncertain = _review(FindingStatus.UNCERTAIN)
    rejected = _review(FindingStatus.REJECTED)
    artifact = _artifact(
        runs=(_run("run-2", input_tokens=None, output_tokens=0, cost_usd=None), _run("run-1")),
        oracle_facts=(_fact(), _fact("fact-rejected", review=rejected, run_id="run-2")),
        guide_claims=(
            _guide_claim(),
            _guide_claim("claim-uncertain", review=uncertain, run_id="run-2"),
        ),
        relationships=(
            _relationship(),
            _relationship(
                "relationship-uncertain",
                mechanism="conditional-engine",
                participants=(70153, 70340),
                review=uncertain,
                run_id="run-2",
            ),
            _relationship(
                "relationship-rejected",
                mechanism="food-engine",
                participants=(70153, 70221, 70340),
                review=rejected,
                run_id="run-2",
            ),
        ),
        rejected_findings=(
            RejectedFinding(
                finding_id="diagnostic-1",
                source_kind="relationship",
                summary="Malformed attempted package.",
                reason="The model omitted exact evidence.",
                run_id="run-2",
            ),
        ),
        review=ArtifactReview(
            state="confirmed",
            reviewer_id=" reviewer-7 ",
            reviewed_at=ARTIFACT_REVIEWED_AT,
        ),
        confirmed_relationship_ids=("relationship-1",),
    )
    encoded = artifact.to_json()
    assert set(encoded) == {
        "schema_version",
        "set_code",
        "set_source_id",
        "set_source_sha256",
        "created_at",
        "cards",
        "guides",
        "runs",
        "oracle_facts",
        "guide_claims",
        "relationships",
        "rejected_findings",
        "review",
        "confirmed_relationship_ids",
    }
    assert "sources" not in encoded
    assert artifact.to_bytes() == SemanticEnrichmentArtifact.from_bytes(
        artifact.to_bytes(),
        sources=_sources(),
    ).to_bytes()
    restored = SemanticEnrichmentArtifact.from_json(encoded, sources=_sources())
    assert restored.runs[0].input_tokens == 20
    assert restored.runs[0].output_tokens == 0
    assert restored.runs[1].input_tokens is None
    assert restored.runs[1].output_tokens == 0
    assert restored.runs[1].cost_usd is None
    assert restored.runs[0].reasoning.effort == "high"
    encoded["cards"].clear()  # type: ignore[union-attr]
    encoded["runs"][0]["reasoning"]["effort"] = "mutated"  # type: ignore[index]
    assert len(artifact.cards) == len(_sources().cards)
    assert artifact.runs[0].reasoning.effort == "high"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        artifact.set_code = "other"  # type: ignore[misc]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        artifact.runs[0].provider = "other"  # type: ignore[misc]


def test_artifact_confirmed_relationships_filter_accepted_selection_in_order() -> None:
    first = _relationship(
        "z-first",
        mechanism="alpha-mechanism",
        participants=(70221, 70340),
    )
    second = _relationship(
        "a-second",
        mechanism="zeta-mechanism",
        participants=(70153, 70221, 70340),
    )
    unselected = _relationship(
        "not-selected",
        mechanism="middle-mechanism",
        participants=(70153, 70340),
    )
    artifact = _artifact(
        relationships=(second, unselected, first),
        review=ArtifactReview(
            state="confirmed",
            reviewer_id="reviewer",
            reviewed_at=ARTIFACT_REVIEWED_AT,
        ),
        confirmed_relationship_ids=("z-first", "a-second"),
    )
    assert artifact.confirmed_relationship_ids == ("a-second", "z-first")
    assert [item.finding_id for item in artifact.confirmed_relationships] == [
        "z-first",
        "a-second",
    ]
    assert [item.mechanism for item in artifact.confirmed_relationships] == [
        "alpha-mechanism",
        "zeta-mechanism",
    ]
    assert artifact.confirmed_relationships[0].participants == (70221, 70340)
    pending = _artifact(relationships=(first,), confirmed_relationship_ids=())
    cancelled = _artifact(
        relationships=(first,),
        review=ArtifactReview(
            state="cancelled",
            reviewer_id="reviewer",
            reviewed_at=ARTIFACT_REVIEWED_AT,
        ),
    )
    assert pending.confirmed_relationships == ()
    assert cancelled.confirmed_relationships == ()


def test_artifact_accepts_two_card_and_three_card_packages() -> None:
    two_card = _relationship()
    three_card = _relationship(
        "package-1",
        mechanism="food-package",
        participants=(70153, 70221, 70340),
    )
    artifact = _artifact(relationships=(three_card, two_card))
    assert artifact.relationships == (two_card, three_card)
    assert artifact.relationships[1].participants == (70153, 70221, 70340)


def test_artifact_rejects_unknown_cards_and_invalid_exact_evidence() -> None:
    artifact = _artifact()
    cases: list[dict[str, object]] = []
    value = _artifact_json(artifact)
    value["oracle_facts"][0]["card_id"] = 999999  # type: ignore[index]
    cases.append(value)
    value = _artifact_json(artifact)
    value["oracle_facts"][0]["evidence"][0]["card_id"] = 999999  # type: ignore[index]
    cases.append(value)
    value = _artifact_json(artifact)
    value["oracle_facts"][0]["evidence"][0]["quote"] = GUIDE_TEXT  # type: ignore[index]
    cases.append(value)
    value = _artifact_json(artifact)
    value["oracle_facts"][0]["evidence"][0]["quote"] = ""  # type: ignore[index]
    cases.append(value)
    value = _artifact_json(artifact)
    del value["oracle_facts"][0]["evidence"][0]["quote"]  # type: ignore[index]
    cases.append(value)
    value = _artifact_json(artifact)
    value["guide_claims"][0]["evidence"][0]["quote"] = "not in guide"  # type: ignore[index]
    cases.append(value)
    value = _artifact_json(artifact)
    value["guide_claims"][0]["evidence"][0]["guide_id"] = "missing-guide"  # type: ignore[index]
    cases.append(value)
    value = _artifact_json(artifact)
    value["guide_claims"][0]["evidence"][0]["quote"] = GUIDE_TWO_TEXT  # type: ignore[index]
    cases.append(value)
    for invalid in cases:
        with pytest.raises(SemanticEnrichmentError):
            SemanticEnrichmentArtifact.from_json(invalid, sources=_sources())


def test_artifact_keeps_guide_claims_advisory_with_guide_only_evidence() -> None:
    artifact = _artifact()
    accepted_claim = artifact.guide_claims[0]
    assert accepted_claim.review.status is FindingStatus.ACCEPTED
    assert set(accepted_claim.to_json()) == {
        "finding_id",
        "category",
        "name",
        "claim",
        "card_ids",
        "evidence",
        "review",
        "run_id",
    }
    assert set(accepted_claim.evidence[0].to_json()) == {"guide_id", "quote"}

    relationship = artifact.relationships[0]
    assert set(relationship.oracle_evidence[0].to_json()) == {"card_id", "face_index", "quote"}
    with pytest.raises(SemanticEnrichmentError):
        replace(relationship, oracle_evidence=())


def test_artifact_rejects_face_selection_boundaries_and_wrong_evidence_shapes() -> None:
    face_artifact = _artifact(oracle_facts=(_face_fact(), _fact("z-fact")))
    invalid_face_values = (None, 1, 2, "0")
    for face_index in invalid_face_values:
        value = _artifact_json(face_artifact)
        value["oracle_facts"][0]["evidence"][0]["face_index"] = face_index  # type: ignore[index]
        with pytest.raises(SemanticEnrichmentError):
            SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(face_artifact)
    value["oracle_facts"][0]["evidence"][0]["quote"] = FACE_BACK_TEXT  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(face_artifact)
    value["oracle_facts"][1]["evidence"][0]["face_index"] = 0  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(face_artifact)
    value["guide_claims"][0]["oracle_evidence"] = []  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(face_artifact)
    value["oracle_facts"][1]["guide_evidence"] = []  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())


def test_artifact_rejects_relationship_identity_and_evidence_coverage_errors() -> None:
    first = _relationship("first", mechanism="same", participants=(70221, 70340))
    duplicate = _relationship(
        "second",
        mechanism="same",
        participants=(70340, 70221),
    )
    with pytest.raises(SemanticEnrichmentError):
        _artifact(relationships=(first, duplicate))
    value = _artifact_json(_artifact())
    del value["relationships"][0]["oracle_evidence"][1]  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(_artifact())
    value["relationships"][0]["oracle_evidence"].append(  # type: ignore[index]
        OracleEvidence(card_id=70153, face_index=None, quote=COW_TRIGGER_QUOTE).to_json()
    )
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(_artifact())
    value["relationships"][0]["participants"] = [70221, 70221]  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(_artifact())
    value["relationships"][0]["participants"] = [70221]  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())


def test_artifact_source_pins_cover_sources_and_detect_tampering() -> None:
    artifact = _artifact()
    original = artifact.to_bytes()
    value = _artifact_json(artifact)
    value["set_source_sha256"] = DIGEST_B
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    value["cards"][0]["sha256"] = DIGEST_B  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    value["guides"][0]["sha256"] = DIGEST_B  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    value["cards"].pop()  # type: ignore[union-attr]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    altered_guide = replace(_sources().guides[0], text=GUIDE_TEXT + " Changed.")
    altered_sources = _sources(guides=(altered_guide, _sources().guides[1]))
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(original, sources=altered_sources)
    altered_card = replace(_sources().cards[0], oracle_text=WITCH_ORACLE_TEXT + " Changed.")
    altered_sources = _sources(cards=(altered_card, *_sources().cards[1:]))
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(original, sources=altered_sources)
    reversed_sources = _sources(
        cards=tuple(reversed(_sources().cards)),
        guides=tuple(reversed(_sources().guides)),
    )
    assert SemanticEnrichmentArtifact.from_bytes(original, sources=reversed_sources) == artifact


def test_artifact_rejects_bad_run_references_and_confirmed_ids() -> None:
    value = _artifact_json(_artifact())
    value["oracle_facts"][0]["run_id"] = "missing-run"  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    accepted = _relationship("accepted", mechanism="accepted-engine")
    uncertain = _relationship(
        "uncertain",
        mechanism="uncertain-engine",
        participants=(70153, 70340),
        review=_review(FindingStatus.UNCERTAIN),
    )
    rejected = _relationship(
        "rejected",
        mechanism="rejected-engine",
        participants=(70153, 70221, 70340),
        review=_review(FindingStatus.REJECTED),
    )
    findings = _artifact(
        relationships=(accepted, uncertain, rejected),
        rejected_findings=(
            RejectedFinding(
                finding_id="diagnostic",
                source_kind="oracle",
                summary="Not enough text.",
                reason="The quote was absent.",
                run_id="run-1",
            ),
        ),
        review=ArtifactReview(
            state="confirmed",
            reviewer_id="reviewer",
            reviewed_at=ARTIFACT_REVIEWED_AT,
        ),
    )
    for invalid_id in ("uncertain", "rejected", "diagnostic", "fact-1", "claim-1", "missing"):
        with pytest.raises(SemanticEnrichmentError):
            replace(findings, confirmed_relationship_ids=(invalid_id,), sources=_sources())
    with pytest.raises(SemanticEnrichmentError):
        replace(
            findings,
            review=ArtifactReview(state="pending", reviewer_id=None, reviewed_at=None),
            confirmed_relationship_ids=("accepted",),
            sources=_sources(),
        )
    assert replace(
        findings,
        confirmed_relationship_ids=("accepted",),
        sources=_sources(),
    ).confirmed_relationships == (accepted,)


def test_artifact_dataclass_replace_requires_sources_and_keeps_context_unstored() -> None:
    sources = _sources()
    artifact = _artifact(sources=sources)
    assert not hasattr(artifact, "sources")
    with pytest.raises(ValueError):
        replace(artifact, set_source_id="other")
    rebuilt = replace(artifact, set_source_id="other", sources=sources)
    assert rebuilt.set_source_id == "other"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        artifact.confirmed_relationship_ids = ("relationship-1",)  # type: ignore[misc]


def test_artifact_unordered_inputs_are_canonical_and_face_order_is_semantic() -> None:
    sources = _sources()
    artifact = _artifact(
        sources=sources,
        runs=(_run("run-2"), _run("run-1")),
        oracle_facts=(_fact("fact-2"), _fact("fact-1")),
        guide_claims=(_guide_claim("claim-2"), _guide_claim("claim-1")),
        relationships=(
            _relationship("rel-2", mechanism="z-engine"),
            _relationship("rel-1", mechanism="a-engine"),
        ),
    )
    reversed_artifact = _artifact(
        sources=_sources(
            cards=tuple(reversed(sources.cards)),
            guides=tuple(reversed(sources.guides)),
        ),
        runs=(_run("run-1"), _run("run-2")),
        oracle_facts=(_fact("fact-1"), _fact("fact-2")),
        guide_claims=(_guide_claim("claim-1"), _guide_claim("claim-2")),
        relationships=(
            _relationship("rel-1", mechanism="a-engine"),
            _relationship("rel-2", mechanism="z-engine"),
        ),
    )
    assert artifact.to_bytes() == reversed_artifact.to_bytes()
    rebuilt = SemanticEnrichmentArtifact.from_bytes(artifact.to_bytes(), sources=sources)
    assert rebuilt.to_bytes() == artifact.to_bytes()
    changed_faces = replace(
        sources.cards[-1],
        faces=tuple(reversed(sources.cards[-1].faces)),
    )
    face_sources = _sources(cards=(*sources.cards[:-1], changed_faces))
    assert card_source_sha256(changed_faces) != card_source_sha256(sources.cards[-1])
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(artifact.to_bytes(), sources=face_sources)


def test_artifact_strict_json_boundaries_reject_extra_missing_and_numeric_fields() -> None:
    artifact = _artifact()
    diagnostic = RejectedFinding(
        finding_id="diagnostic",
        source_kind="oracle",
        summary="summary",
        reason="reason",
        run_id="run-1",
    )
    diagnostic_artifact = _artifact(rejected_findings=(diagnostic,))
    for boundary, field in (
        ("root", "score"),
        ("cards", "samples"),
        ("guides", "score_adjustment"),
        ("runs", "samples"),
        ("oracle_facts", "score"),
        ("guide_claims", "score_adjustment"),
        ("relationships", "samples"),
        ("rejected_findings", "score"),
    ):
        source_artifact = diagnostic_artifact if boundary == "rejected_findings" else artifact
        value = _artifact_json(source_artifact)
        if boundary == "root":
            value[field] = 1
        else:
            value[boundary][0][field] = 1  # type: ignore[index]
        with pytest.raises(SemanticEnrichmentError):
            SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    del value["review"]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    del value["oracle_facts"][0]["review"]  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    for schema_version in (0, 2, True, "1"):
        value = _artifact_json(artifact)
        value["schema_version"] = schema_version
        with pytest.raises(SemanticEnrichmentError):
            SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    value["runs"][0]["input_tokens"] = True  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    value["runs"][0]["cost_usd"] = 1.5  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    value["runs"][0]["completed_at"] = "2026-09-01T12:00:00"  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())
    value = _artifact_json(artifact)
    value["set_code"] = "other"
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(value, sources=_sources())


def test_artifact_from_bytes_is_strict_about_root_encoding_duplicates_and_constants() -> None:
    artifact = _artifact()
    payload = artifact.to_bytes()
    assert payload.endswith(b"\n")
    assert not payload.endswith(b"\n\n")
    assert SemanticEnrichmentArtifact.from_bytes(payload, sources=_sources()) == artifact
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(b"\xff", sources=_sources())
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(b"[]", sources=_sources())
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(b"{", sources=_sources())
    duplicate_key = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(duplicate_key, sources=_sources())
    nonfinite = payload.replace(b'"input_tokens":20', b'"input_tokens":NaN', 1)
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(nonfinite, sources=_sources())
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes("not bytes", sources=_sources())  # type: ignore[arg-type]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json([], sources=_sources())  # type: ignore[arg-type]


def test_semantic_enrichment_public_surface_has_no_record_reexports() -> None:
    assert set(semantic_enrichment_module.__all__) == {
        "SEMANTIC_ENRICHMENT_SCHEMA_VERSION",
        "GuideSource",
        "EnrichmentSources",
        "SemanticEnrichmentArtifact",
        "card_source_projection",
        "card_source_sha256",
        "set_source_sha256",
    }
    assert len(semantic_enrichment_module.__all__) == 7
    assert "OracleFact" not in semantic_enrichment_module.__all__


def test_empty_guide_sources_are_valid_without_guide_findings() -> None:
    sources = _sources(guides=())
    relationship = replace(_relationship(), guide_evidence=())
    artifact = _artifact(
        sources=sources,
        guide_claims=(),
        relationships=(relationship,),
    )
    assert artifact.guides == ()


def test_artifact_rejects_duplicate_run_and_global_finding_ids() -> None:
    with pytest.raises(SemanticEnrichmentError):
        _artifact(runs=(_run("run-1"), _run("run-1")))
    with pytest.raises(SemanticEnrichmentError):
        _artifact(oracle_facts=(_fact("same"), _fact("same")))
    with pytest.raises(SemanticEnrichmentError):
        _artifact(guide_claims=(_guide_claim("same"), _guide_claim("same")))
    rejected = RejectedFinding(
        finding_id="same",
        source_kind="oracle",
        summary="summary",
        reason="reason",
        run_id="run-1",
    )
    with pytest.raises(SemanticEnrichmentError):
        _artifact(rejected_findings=(rejected, rejected))
    with pytest.raises(SemanticEnrichmentError):
        _artifact(
            oracle_facts=(_fact("claim-1"),),
            guide_claims=(_guide_claim(),),
        )
    with pytest.raises(SemanticEnrichmentError):
        _artifact(runs=())


def test_artifact_time_and_pin_identity_boundaries_are_strict() -> None:
    artifact = _artifact()
    with pytest.raises(SemanticEnrichmentError):
        replace(artifact, created_at=STARTED_AT, sources=_sources())
    with pytest.raises(SemanticEnrichmentError):
        replace(
            artifact,
            review=ArtifactReview(
                state="confirmed",
                reviewer_id="reviewer",
                reviewed_at=STARTED_AT,
            ),
            sources=_sources(),
        )
    invalid_hash = _artifact_json(artifact)
    invalid_hash["set_source_sha256"] = "not-a-sha256"
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(invalid_hash, sources=_sources())
    invalid_card_identity = _artifact_json(artifact)
    invalid_card_identity["cards"][0]["oracle_id"] = "different"  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(invalid_card_identity, sources=_sources())
    invalid_collector = _artifact_json(artifact)
    invalid_collector["cards"][0]["collector_number"] = "different"  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(invalid_collector, sources=_sources())
    invalid_url = _artifact_json(artifact)
    invalid_url["guides"][0]["url"] = "different"  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(invalid_url, sources=_sources())
    invalid_retrieved_at = _artifact_json(artifact)
    invalid_retrieved_at["guides"][0]["retrieved_at"] = "2026-09-01T13:00:00Z"  # type: ignore[index]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_json(invalid_retrieved_at, sources=_sources())


def test_guide_source_preserves_text_and_rejects_wrong_scalar_types() -> None:
    source = GuideSource(
        guide_id=" guide ",
        url="opaque provenance",
        text="  exact guide text\n",
        retrieved_at="2026-09-01T13:00:00+01:00",
    )
    assert source.guide_id == "guide"
    assert source.url == "opaque provenance"
    assert source.text == "  exact guide text\n"
    assert source.retrieved_at == "2026-09-01T12:00:00Z"
    assert source.text_sha256 == hashlib.sha256(source.text.encode("utf-8")).hexdigest()
    for kwargs in (
        {"guide_id": 1},
        {"url": 1},
        {"text": 1},
        {"retrieved_at": 1},
    ):
        with pytest.raises(SemanticEnrichmentError):
            GuideSource(
                guide_id=kwargs.get("guide_id", "guide"),  # type: ignore[arg-type]
                url=kwargs.get("url", "url"),  # type: ignore[arg-type]
                text=kwargs.get("text", GUIDE_TEXT),  # type: ignore[arg-type]
                retrieved_at=kwargs.get("retrieved_at", STARTED_AT),  # type: ignore[arg-type]
            )


TYPED_SOURCE_CARD_ID = 301
TYPED_TARGET_CARD_ID = 11
TYPED_SOURCE_CARD_NAME = "Typed Token Enabler"
TYPED_TARGET_CARD_NAME = "Typed Wide Payoff"
TYPED_TOKEN_PARAGRAPH = "Create two 1/1 white Soldier creature tokens."
TYPED_ANTHEM_PARAGRAPH = "Creatures you control get +1/+1."
TYPED_TOKEN_QUANTITY = CapabilityQuantity(value=2, relation=QuantityRelation.EXACTLY)


def _typed_source_card(*, oracle_text: str = TYPED_TOKEN_PARAGRAPH) -> CardInfo:
    return replace(
        _source_card(TYPED_SOURCE_CARD_ID, TYPED_SOURCE_CARD_NAME, oracle_text, type_line="Creature"),
        colors=("U", "B"),
    )


def _typed_target_card(*, oracle_text: str = TYPED_ANTHEM_PARAGRAPH) -> CardInfo:
    return _source_card(TYPED_TARGET_CARD_ID, TYPED_TARGET_CARD_NAME, oracle_text, type_line="Creature")


def _typed_sources(*, cards: tuple[CardInfo, ...] | None = None) -> EnrichmentSources:
    return _sources(
        cards=(
            (*_default_cards(), _typed_source_card(), _typed_target_card())
            if cards is None
            else cards
        )
    )


def _typed_zone() -> RelationshipZone:
    return RelationshipZone(zone=CapabilityZone.BATTLEFIELD, player="you")


def _typed_timing() -> RelationshipTiming:
    return RelationshipTiming(window="unrestricted", turn="any", max_per_turn=None)


def _typed_source_clause(**overrides: Any) -> RelationshipPrerequisite:
    """Build one complete source-side token output clause."""
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.CONDITION,
        "subject": "output",
        "operation": "create",
        "object_kind": "token",
        "card_types": ("creature",),
        "type_operator": "all_of",
        "token_restriction": "token",
        "exclusion": "none",
        "subtype": "Soldier",
        "color_operator": "exact",
        "colors": ("W",),
        "controller": "you",
        "owner": "not_applicable",
        "quantity": TYPED_TOKEN_QUANTITY,
        "source_zone": None,
        "destination_zone": _typed_zone(),
        "timing": _typed_timing(),
        "required_card_id": None,
        "evidence": OracleEvidence(
            card_id=TYPED_SOURCE_CARD_ID,
            face_index=None,
            quote=TYPED_TOKEN_PARAGRAPH,
        ),
        "operation_quote": "Create",
        "operation_occurrence": 0,
        "object_quote": "two 1/1 white Soldier creature tokens",
        "object_occurrence": 0,
        "capability_prerequisite_indices": (),
    }
    values.update(overrides)
    return RelationshipPrerequisite(**values)  # type: ignore[arg-type]


def _typed_target_clause(**overrides: Any) -> RelationshipPrerequisite:
    """Build one complete target-side creature-control condition clause."""
    values: dict[str, Any] = {
        "kind": PrerequisiteKind.CONDITION,
        "subject": "participant",
        "operation": "control",
        "object_kind": "permanent",
        "card_types": ("creature",),
        "type_operator": "all_of",
        "token_restriction": "unrestricted",
        "exclusion": "none",
        "subtype": None,
        "color_operator": "unrestricted",
        "colors": (),
        "controller": "you",
        "owner": "not_applicable",
        "quantity": None,
        "source_zone": None,
        "destination_zone": None,
        "timing": _typed_timing(),
        "required_card_id": None,
        "evidence": OracleEvidence(
            card_id=TYPED_TARGET_CARD_ID,
            face_index=None,
            quote=TYPED_ANTHEM_PARAGRAPH,
        ),
        "operation_quote": "control",
        "operation_occurrence": 0,
        "object_quote": "Creatures you control",
        "object_occurrence": 0,
        "capability_prerequisite_indices": (),
    }
    values.update(overrides)
    return RelationshipPrerequisite(**values)  # type: ignore[arg-type]


def _typed_source_participant(**overrides: Any) -> RelationshipParticipant:
    """Build the token-making participant of the typed fixture pair."""
    values: dict[str, Any] = {
        "card_id": TYPED_SOURCE_CARD_ID,
        "capability_id": "capability-typed-tokens",
        "card_name": TYPED_SOURCE_CARD_NAME,
        "face_index": None,
        "face_name": None,
        "card_source_sha256": card_source_sha256(_typed_source_card()),
        "role": Role.TOKEN_MAKER,
        "capability_prerequisites": (),
        "prerequisites": (_typed_source_clause(),),
    }
    values.update(overrides)
    return RelationshipParticipant(**values)  # type: ignore[arg-type]


def _typed_target_participant(**overrides: Any) -> RelationshipParticipant:
    """Build the go-wide payoff participant of the typed fixture pair."""
    values: dict[str, Any] = {
        "card_id": TYPED_TARGET_CARD_ID,
        "capability_id": "capability-typed-anthem",
        "card_name": TYPED_TARGET_CARD_NAME,
        "face_index": None,
        "face_name": None,
        "card_source_sha256": card_source_sha256(_typed_target_card()),
        "role": Role.GO_WIDE_PAYOFF,
        "capability_prerequisites": (),
        "prerequisites": (_typed_target_clause(),),
    }
    values.update(overrides)
    return RelationshipParticipant(**values)  # type: ignore[arg-type]


def _typed_projection(**overrides: Any) -> RelationshipPrerequisiteProjection:
    """Build one complete typed prerequisite projection."""
    values: dict[str, Any] = {
        "source": _typed_source_participant(),
        "target": _typed_target_participant(),
    }
    values.update(overrides)
    return RelationshipPrerequisiteProjection(**values)  # type: ignore[arg-type]


def _typed_relationship(**overrides: Any) -> CardRelationship:
    """Build one accepted relationship carrying the typed projection."""
    values: dict[str, Any] = {
        "finding_id": "relationship:token-go-wide-payoff:301:11",
        "mechanism": "token-go-wide-payoff",
        "participants": (TYPED_TARGET_CARD_ID, TYPED_SOURCE_CARD_ID),
        "claim": "The token maker feeds the go-wide payoff.",
        "prerequisites": ("A creature token is created.",),
        "oracle_evidence": (
            OracleEvidence(
                card_id=TYPED_SOURCE_CARD_ID,
                face_index=None,
                quote=TYPED_TOKEN_PARAGRAPH,
            ),
            OracleEvidence(
                card_id=TYPED_TARGET_CARD_ID,
                face_index=None,
                quote=TYPED_ANTHEM_PARAGRAPH,
            ),
        ),
        "guide_evidence": (),
        "review": _review(),
        "run_id": "run-1",
        "prerequisite_projection": _typed_projection(),
    }
    values.update(overrides)
    return CardRelationship(**values)  # type: ignore[arg-type]


def _typed_artifact(**overrides: Any) -> SemanticEnrichmentArtifact:
    """Build one artifact whose only relationship carries the typed projection."""
    values: dict[str, Any] = {
        "sources": _typed_sources(),
        "relationships": (_typed_relationship(),),
    }
    values.update(overrides)
    return _artifact(**values)  # type: ignore[arg-type]


def test_typed_projection_survives_artifact_serialization_with_direction() -> None:
    sources = _typed_sources()
    artifact = _typed_artifact(sources=sources)
    relationship = artifact.relationships[0]
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert relationship.participants == (TYPED_TARGET_CARD_ID, TYPED_SOURCE_CARD_ID)
    assert relationship.identity == (
        "token-go-wide-payoff",
        (TYPED_TARGET_CARD_ID, TYPED_SOURCE_CARD_ID),
        (
            TYPED_SOURCE_CARD_ID,
            "capability-typed-tokens",
            -1,
            TYPED_TARGET_CARD_ID,
            "capability-typed-anthem",
            -1,
        ),
    )
    assert projection.source.prerequisites[0].colors == ("W",)
    assert projection.source.prerequisites[0].quantity == TYPED_TOKEN_QUANTITY
    assert projection.target.prerequisites[0].controller == "you"

    restored = SemanticEnrichmentArtifact.from_bytes(artifact.to_bytes(), sources=sources)
    assert restored.relationships[0] == relationship
    assert restored.relationships[0].prerequisite_projection == projection


def test_artifact_guard_rejects_projection_evidence_from_another_paragraph() -> None:
    extended = replace(
        _typed_source_card(),
        oracle_text=f"{TYPED_TOKEN_PARAGRAPH} Draw a card.",
    )
    sources = _typed_sources(cards=(*_default_cards(), extended, _typed_target_card()))
    relationship = _typed_relationship(
        prerequisite_projection=_typed_projection(
            source=replace(
                _typed_source_participant(),
                card_source_sha256=card_source_sha256(extended),
            ),
        ),
    )

    with pytest.raises(PrerequisiteProjectionError) as error:
        _typed_artifact(sources=sources, relationships=(relationship,))

    assert error.value.code == "contradiction"


def test_artifact_reader_rejects_invalid_present_projection_without_dropping_it() -> None:
    sources = _typed_sources()
    unknown_version = _artifact_json(_typed_artifact())
    unknown_version["relationships"][0]["prerequisite_projection"]["schema_version"] = 2
    with pytest.raises(SemanticEnrichmentError, match="schema_version is unsupported"):
        SemanticEnrichmentArtifact.from_json(unknown_version, sources=sources)

    missing_target = _artifact_json(_typed_artifact())
    del missing_target["relationships"][0]["prerequisite_projection"]["target"]
    with pytest.raises(SemanticEnrichmentError):
        SemanticEnrichmentArtifact.from_bytes(
            json.dumps(missing_target).encode("utf-8"),
            sources=sources,
        )

    absent_projection = _artifact_json(_typed_artifact())
    absent_projection["relationships"][0]["prerequisite_projection"] = None
    with pytest.raises(SemanticEnrichmentError, match="must be an object when present"):
        SemanticEnrichmentArtifact.from_json(absent_projection, sources=sources)

    assert _typed_artifact().relationships[0].prerequisite_projection is not None


def _stored_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the only stored relationship's projection object."""
    return payload["relationships"][0]["prerequisite_projection"]


def _stored_clause(payload: dict[str, Any], side: str) -> dict[str, Any]:
    """Return one stored participant's only atomic clause object."""
    return _stored_projection(payload)[side]["prerequisites"][0]


@pytest.mark.parametrize(
    ("mutate", "expected", "code"),
    (
        (
            lambda payload: _stored_clause(payload, "source").update({"colors": ["U"]}),
            "contradict their source evidence",
            "contradiction",
        ),
        (
            lambda payload: _stored_projection(payload)["source"].update({"face_index": 1}),
            "contradict their source evidence",
            "contradiction",
        ),
        (
            lambda payload: _stored_projection(payload)["source"].update(
                {"card_id": TYPED_TARGET_CARD_ID}
            ),
            "contradict their source evidence",
            "contradiction",
        ),
        (
            lambda payload: _stored_projection(payload)["source"].update(
                {"role": Role.GO_WIDE_PAYOFF.value}
            ),
            "relationship prerequisites are incomplete",
            "incomplete",
        ),
        (
            lambda payload: _stored_clause(payload, "target").update({"controller": "opponent"}),
            "contradict their source evidence",
            "contradiction",
        ),
        (
            lambda payload: _stored_clause(payload, "source").update({"required_card_id": 90001}),
            "relationship prerequisites are incomplete",
            "incomplete",
        ),
    ),
    ids=(
        "clause-color-contradicts-its-quotation",
        "participant-face-index-mismatches-its-clause",
        "participant-card-id-mismatches-its-clause",
        "participant-role-mismatches-its-clauses",
        "clause-controller-contradicts-its-quotation",
        "clause-required-card-is-not-a-participant",
    ),
)
def test_artifact_reader_rejects_mutated_stored_projection_clauses(
    mutate: Any,
    expected: str,
    code: str,
) -> None:
    sources = _typed_sources()
    payload = _artifact_json(_typed_artifact())
    mutate(payload)

    with pytest.raises(PrerequisiteProjectionError, match=expected) as error:
        SemanticEnrichmentArtifact.from_json(payload, sources=sources)

    assert error.value.code == code
    restored = SemanticEnrichmentArtifact.from_bytes(_typed_artifact().to_bytes(), sources=sources)
    assert restored.relationships[0].prerequisite_projection is not None


@pytest.mark.parametrize(
    ("projection", "expected"),
    (
        (
            lambda: _typed_projection(
                source=replace(_typed_source_participant(), card_name="Renamed Enabler"),
            ),
            "card name does not match",
        ),
        (
            lambda: _typed_projection(
                source=replace(_typed_source_participant(), card_source_sha256="0" * 64),
            ),
            "hash must match its card source pin",
        ),
    ),
)
def test_artifact_guard_rejects_projection_source_and_pin_mismatches(
    projection: Any,
    expected: str,
) -> None:
    relationship = _typed_relationship(prerequisite_projection=projection())

    with pytest.raises(SemanticEnrichmentError) as error:
        _typed_artifact(relationships=(relationship,))

    assert expected in str(error.value)


def test_artifact_keeps_reversed_directed_projections_distinct() -> None:
    forward = _typed_relationship()
    reversed_relationship = _typed_relationship(
        finding_id="relationship:token-go-wide-payoff:11:301",
        prerequisite_projection=_typed_projection(
            source=_typed_target_participant(),
            target=_typed_source_participant(),
        ),
    )

    artifact = _typed_artifact(relationships=(forward, reversed_relationship))

    assert len(artifact.relationships) == 2
    assert len({item.identity for item in artifact.relationships}) == 2
    restored = SemanticEnrichmentArtifact.from_bytes(artifact.to_bytes(), sources=_typed_sources())
    by_id = {item.finding_id: item for item in restored.relationships}
    reversed_projection = by_id["relationship:token-go-wide-payoff:11:301"].prerequisite_projection
    assert reversed_projection is not None
    assert reversed_projection.source.card_id == TYPED_TARGET_CARD_ID
    assert reversed_projection.target.card_id == TYPED_SOURCE_CARD_ID

    with pytest.raises(SemanticEnrichmentError, match="duplicate"):
        _typed_artifact(relationships=(forward, _typed_relationship()))


def test_artifact_guard_leaves_projection_free_relationships_untouched() -> None:
    artifact = _typed_artifact(relationships=(_relationship(), _typed_relationship()))
    by_id = {item.finding_id: item for item in artifact.relationships}

    assert by_id["relationship-1"].prerequisite_projection is None
    assert by_id["relationship-1"].identity == (
        "draw-engine",
        (70221, 70340),
        (),
    )
    payload = _artifact_json(artifact)
    legacy = next(item for item in payload["relationships"] if item["finding_id"] == "relationship-1")
    assert "prerequisite_projection" not in legacy

    restored = SemanticEnrichmentArtifact.from_bytes(artifact.to_bytes(), sources=_typed_sources())
    assert {item.finding_id: item for item in restored.relationships}["relationship-1"].prerequisite_projection is None


def test_artifact_reader_rejects_score_weight_and_adjustment_fields() -> None:
    sources = _typed_sources()
    base = _artifact_json(_typed_artifact())
    relationship = base["relationships"][0]
    projection = relationship["prerequisite_projection"]
    clause = projection["source"]["prerequisites"][0]

    scored = _artifact_json(_typed_artifact())
    scored["relationships"][0]["score"] = 0.75
    with pytest.raises(SemanticEnrichmentError, match="unknown fields"):
        SemanticEnrichmentArtifact.from_json(scored, sources=sources)

    weighted = _artifact_json(_typed_artifact())
    weighted["relationships"][0]["prerequisite_projection"]["source"]["prerequisites"][0]["weight"] = 2
    with pytest.raises(SemanticEnrichmentError, match="unknown fields"):
        SemanticEnrichmentArtifact.from_json(weighted, sources=sources)

    adjusted = _artifact_json(_typed_artifact())
    adjusted["relationships"][0]["prerequisite_projection"]["adjustment"] = 0.25
    with pytest.raises(SemanticEnrichmentError, match="unknown fields"):
        SemanticEnrichmentArtifact.from_json(adjusted, sources=sources)

    assert clause["quantity"] == {"value": 2, "relation": "exactly"}
    assert projection["schema_version"] == 1


def test_projection_is_independent_of_claim_prose_and_legacy_prerequisites() -> None:
    first = _typed_artifact()
    second = _typed_artifact(
        relationships=(
            _typed_relationship(
                claim="Score 0.9: the enabler is worth three points more than the payoff.",
                prerequisites=("A different descriptive reading.", "An extra legacy line."),
            ),
        ),
    )

    first_projection = _artifact_json(first)["relationships"][0]["prerequisite_projection"]
    second_projection = _artifact_json(second)["relationships"][0]["prerequisite_projection"]

    assert first_projection == second_projection
    assert (
        first.relationships[0].prerequisite_projection
        == second.relationships[0].prerequisite_projection
    )
    assert second.relationships[0].prerequisites == (
        "A different descriptive reading.",
        "An extra legacy line.",
    )
