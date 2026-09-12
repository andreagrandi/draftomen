from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import draftomen.set_profile as set_profile_module

from draftomen.config import COLOR_PAIRS
from draftomen.semantic_capability_records import (
    CapabilityQuantity,
    CapabilityZone,
    PrerequisiteKind,
    QuantityRelation,
)
from draftomen.semantic_enrichment import SEMANTIC_ENRICHMENT_SCHEMA_VERSION
from draftomen.semantic_enrichment_records import OracleEvidence
from draftomen.semantic_relationship_records import (
    CardRelationship,
    RelationshipParticipant,
    RelationshipPrerequisite,
    RelationshipPrerequisiteProjection,
    RelationshipTiming,
    RelationshipZone,
)
from draftomen.semantic_roles import CompiledRoleProfile, ProfileCard, Role, RoleAssignment
from draftomen.set_profile import (
    SET_PROFILE_SCHEMA_VERSION,
    AggregateEvidence,
    CardPairSynergy,
    CardRating,
    EnhancementCardData,
    EnhancementStatus,
    NumericTarget,
    PairProfile,
    ProfileMaturity,
    RateEstimate,
    RemovalTarget,
    RoleTarget,
    SetProfile,
    SetProfileError,
    SetProfileSchemaError,
    dump_set_profile,
    load_set_profile,
    load_scoring_profile,
    safe_load_set_profile,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "set-profiles"


def test_mature_profile_round_trip_covers_all_pairs_and_optional_sections() -> None:
    profile = load_set_profile(
        FIXTURE_DIR / "mature.json",
        expected_set_code="TST",
        expected_format="QuickDraft",
    )

    assert profile.maturity is ProfileMaturity.MATURE
    assert tuple(item.pair for item in profile.pairs) == COLOR_PAIRS
    assert sum(item.theme is not None for item in profile.pairs) == 2
    samples = profile.samples
    assert samples is not None
    assert samples.count_for("RG") == 120
    wu = profile.pair("WU")
    assert wu is not None
    assert wu.theme == "tempo flyers"
    wb = profile.pair("WB")
    assert wb is not None
    assert wb.theme is None
    rg = profile.pair("RG")
    assert rg is not None
    assert rg.theme == "landfall pressure"
    assert wu.structural_targets[0].name == "average_land_count"
    assert wu.role_targets[0].role is Role.DRAW
    assert wu.removal_targets[0].kind == "disable"
    assert wu.synergy[0].first_card == "oracle_id:wu-bomb"
    assert wu.scarcity[0].card_key == "oracle_id:wu-bomb"
    assert SetProfile.from_json(profile.to_json()).to_bytes() == profile.to_bytes()

def test_schema_one_baseline_round_trip_preserves_canonical_bytes() -> None:
    path = Path(__file__).parents[1] / "draftomen" / "baseline_profiles" / "hob-quickdraft.json"
    profile = load_set_profile(path, expected_set_code="HOB", expected_format="QuickDraft")
    assert profile.schema_version == 1
    assert profile.to_bytes() == path.read_bytes()

def test_profile_fingerprint_is_stable_across_round_trip() -> None:
    profile = load_set_profile(FIXTURE_DIR / "mature.json")
    equivalent = SetProfile.from_json(profile.to_json())

    assert profile.fingerprint
    assert equivalent.fingerprint == profile.fingerprint


def test_pair_profile_theme_round_trip_trims_and_omits_absent_theme() -> None:
    themed = PairProfile.from_json({"pair": " wu ", "theme": "  tempo flyers  "})

    assert themed.theme == "tempo flyers"
    assert themed.to_json() == {"pair": "WU", "theme": "tempo flyers"}
    assert PairProfile.from_json({"pair": "WB"}).theme is None
    assert "theme" not in PairProfile(pair="WB").to_json()


@pytest.mark.parametrize("theme", ("", "   ", 42, False))
def test_pair_profile_theme_rejects_blank_and_non_string_values(theme: object) -> None:
    with pytest.raises(SetProfileSchemaError, match="pair_profile.theme"):
        PairProfile.from_json({"pair": "WU", "theme": theme})


def test_sparse_early_profile_preserves_only_available_empirical_evidence() -> None:
    profile = load_set_profile(FIXTURE_DIR / "early.json")

    assert profile.maturity is ProfileMaturity.EARLY
    samples = profile.samples
    assert samples is not None
    assert samples.by_pair == (("WU", 17),)
    assert samples.count_for("WB") is None
    assert tuple(item.pair for item in profile.pair_profiles) == ("WU",)
    assert profile.pair("WU") is not None
    assert profile.pair("WB") is None
    serialized = profile.to_json()
    assert serialized["samples"] == {"by_pair": {"WU": 17}, "total": 17}
    assert serialized["pair_profiles"] == [
        {
            "pair": "WU",
            "structural_targets": [{"name": "average_land_count", "value": 17.0}],
        }
    ]


def test_metadata_and_semantic_only_profiles_omit_empirical_sections() -> None:
    metadata = load_set_profile(FIXTURE_DIR / "metadata-only.json")
    semantic = load_set_profile(FIXTURE_DIR / "semantic-only.json")

    assert metadata.maturity is ProfileMaturity.METADATA_ONLY
    assert metadata.samples is None
    assert semantic.maturity is ProfileMaturity.SEMANTIC_ONLY
    assert semantic.samples is None
    assert semantic.pair_profiles == (PairProfile(pair="WU", theme="tempo flyers"),)
    semantic_json = semantic.to_json()
    assert "samples" not in semantic_json
    assert semantic_json["pair_profiles"] == [{"pair": "WU", "theme": "tempo flyers"}]


def test_unknown_optional_fields_are_ignored_and_output_is_stable(tmp_path: Path) -> None:
    payload = json.loads((FIXTURE_DIR / "early.json").read_text(encoding="utf-8"))
    payload["unknown_optional"] = {"future": [1, 2, 3]}
    payload["pair_profiles"][0]["unknown_optional"] = "ignored"
    profile = SetProfile.from_json(payload)

    output = dump_set_profile(profile, tmp_path / "profile.json")
    assert output.read_bytes() == profile.to_bytes()
    assert "unknown_optional" not in output.read_text(encoding="utf-8")


def test_domain_graph_is_deeply_immutable() -> None:
    profile = load_set_profile(FIXTURE_DIR / "mature.json")
    serialized = profile.to_json()
    serialized["set_code"] = "other"
    assert profile.set_code == "tst"
    assert isinstance(serialized, dict)
    with pytest.raises(AttributeError):
        profile.pairs[0].structural_targets = ()  # type: ignore[misc]
    assert isinstance(profile.pairs, tuple)
    assert isinstance(profile.pairs[0].structural_targets, tuple)
    role_profile = profile.role_profile
    assert role_profile is not None
    assert isinstance(role_profile.cards, tuple)

def test_strict_parser_rejects_missing_required_duplicate_unknown_and_future_schema() -> None:
    payload = json.loads((FIXTURE_DIR / "early.json").read_text(encoding="utf-8"))
    payload.pop("confidence")
    with pytest.raises(SetProfileSchemaError, match="confidence"):
        SetProfile.from_json(payload)

    duplicate = json.loads((FIXTURE_DIR / "early.json").read_text(encoding="utf-8"))
    duplicate["pair_profiles"].append({"pair": "wu"})
    with pytest.raises(SetProfileSchemaError, match="duplicate"):
        SetProfile.from_json(duplicate)

    unknown_pair = json.loads((FIXTURE_DIR / "early.json").read_text(encoding="utf-8"))
    unknown_pair["pair_profiles"] = [{"pair": "XX"}]
    with pytest.raises(SetProfileSchemaError, match="Unsupported color pair"):
        SetProfile.from_json(unknown_pair)

    unsupported = json.loads((FIXTURE_DIR / "future-schema.json").read_text(encoding="utf-8"))
    unsupported["schema_version"] = SET_PROFILE_SCHEMA_VERSION + 1
    with pytest.raises(SetProfileSchemaError, match="Unsupported set profile schema"):
        SetProfile.from_json(unsupported)


def test_strict_loader_rejects_future_nested_role_schema_and_safe_loader_ignores_roles(tmp_path: Path) -> None:
    payload = json.loads((FIXTURE_DIR / "semantic-only.json").read_text(encoding="utf-8"))
    payload["role_profile"]["profile_schema_version"] = 999
    path = tmp_path / "future-role-schema.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SetProfileSchemaError, match="profile_schema_version"):
        load_set_profile(path)

    result = safe_load_set_profile("tst", "quickdraft", profile_path=path)
    assert result.source == "generic"
    assert result.profile.role_profile is None
    resolution = result.profile.resolve_roles(
        {
            "oracle_id": "wu-bomb",
            "name": "WU Bomb",
            "set": "tst",
            "oracle_text": "Draw a card.",
        }
    )
    assert resolution.source == "local_classifier"


def test_maturity_invariants_reject_inconsistent_evidence_labels() -> None:
    mature_without_evidence = json.loads((FIXTURE_DIR / "mature.json").read_text(encoding="utf-8"))
    mature_without_evidence.pop("samples")
    mature_without_evidence.pop("pair_profiles")
    with pytest.raises(SetProfileSchemaError, match="empirical evidence"):
        SetProfile.from_json(mature_without_evidence)

    early_without_evidence = json.loads((FIXTURE_DIR / "early.json").read_text(encoding="utf-8"))
    early_without_evidence.pop("samples")
    early_without_evidence.pop("pair_profiles")
    with pytest.raises(SetProfileSchemaError, match="empirical evidence"):
        SetProfile.from_json(early_without_evidence)

    metadata_with_samples = json.loads((FIXTURE_DIR / "metadata-only.json").read_text(encoding="utf-8"))
    metadata_with_samples["samples"] = {"total": 1}
    with pytest.raises(SetProfileSchemaError, match="cannot contain empirical evidence"):
        SetProfile.from_json(metadata_with_samples)
    metadata_with_semantic = json.loads((FIXTURE_DIR / "metadata-only.json").read_text(encoding="utf-8"))
    semantic_payload = json.loads((FIXTURE_DIR / "semantic-only.json").read_text(encoding="utf-8"))
    metadata_with_semantic["role_profile"] = semantic_payload["role_profile"]
    with pytest.raises(SetProfileSchemaError, match="semantic evidence"):
        SetProfile.from_json(metadata_with_semantic)

    semantic_without_roles = semantic_payload
    semantic_without_roles.pop("role_profile")
    with pytest.raises(SetProfileSchemaError, match="must contain semantic evidence"):
        SetProfile.from_json(semantic_without_roles)

    semantic_with_pair = json.loads((FIXTURE_DIR / "semantic-only.json").read_text(encoding="utf-8"))
    semantic_with_pair["pair_profiles"] = [
        {"pair": "WU", "structural_targets": [{"name": "lands", "value": 17}]}
    ]
    with pytest.raises(SetProfileSchemaError, match="cannot contain empirical evidence"):
        SetProfile.from_json(semantic_with_pair)


def test_safe_loader_precedence_prefers_mature_then_early_then_semantic_then_metadata() -> None:
    result = safe_load_set_profile(
        "tst",
        "quickdraft",
        profile_paths=(
            FIXTURE_DIR / "metadata-only.json",
            FIXTURE_DIR / "semantic-only.json",
            FIXTURE_DIR / "early.json",
            FIXTURE_DIR / "mature.json",
        ),
    )
    assert result.source == "local-mature"
    assert result.profile.maturity is ProfileMaturity.MATURE

    semantic_result = safe_load_set_profile(
        "tst",
        "quickdraft",
        profile_paths=(
            FIXTURE_DIR / "metadata-only.json",
            FIXTURE_DIR / "semantic-only.json",
        ),
    )
    assert semantic_result.source == "local-semantic-only"
    assert semantic_result.profile.maturity is ProfileMaturity.SEMANTIC_ONLY


def test_safe_loader_rejects_wrong_target_and_direct_missing_corrupt_future_fallbacks(tmp_path: Path) -> None:
    wrong_target = json.loads((FIXTURE_DIR / "early.json").read_text(encoding="utf-8"))
    wrong_target["set_code"] = "other"
    wrong_path = tmp_path / "wrong-target.json"
    wrong_path.write_text(json.dumps(wrong_target), encoding="utf-8")
    wrong_result = safe_load_set_profile("tst", "quickdraft", profile_path=wrong_path)
    assert wrong_result.source == "generic"
    assert wrong_result.profile.set_code == "tst"
    assert wrong_result.profile.event_format == "quickdraft"
    assert any("does not match requested set" in diagnostic for diagnostic in wrong_result.diagnostics)

    for fixture_name in ("missing.json", "corrupt.json", "future-schema.json"):
        result = safe_load_set_profile(
            "tst",
            "quickdraft",
            profile_path=FIXTURE_DIR / fixture_name,
        )
        assert result.source == "generic"
        assert result.profile.samples is None
        assert result.profile.pair_profiles == ()
        assert any("rejected:" in diagnostic for diagnostic in result.diagnostics)


@pytest.mark.parametrize("fixture_name", ("missing.json", "corrupt.json"))
def test_scoring_profile_loader_maps_generic_fallback_to_none(fixture_name: str) -> None:
    assert (
        load_scoring_profile(
            "tst",
            "quickdraft",
            profile_path=FIXTURE_DIR / fixture_name,
        )
        is None
    )


def test_scoring_profile_loader_preserves_compatible_last_valid_identity() -> None:
    last_valid = load_set_profile(FIXTURE_DIR / "mature.json")

    selected = load_scoring_profile(
        "tst",
        "quickdraft",
        profile_path=FIXTURE_DIR / "missing.json",
        last_valid_profile=last_valid,
    )

    assert selected is last_valid
    assert selected.maturity is ProfileMaturity.MATURE


def test_scoring_profile_loader_maps_generic_last_valid_to_none() -> None:
    generic = SetProfile.generic(set_code="TST", event_format="quickdraft")

    assert (
        load_scoring_profile(
            "tst",
            "quickdraft",
            profile_path=FIXTURE_DIR / "missing.json",
            last_valid_profile=generic,
        )
        is None
    )


@pytest.mark.parametrize("method", ("expanduser", "resolve"))
def test_safe_loader_candidate_discovery_failures_fall_back_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    last_valid = load_set_profile(FIXTURE_DIR / "semantic-only.json")

    def fail_candidate_discovery(*args: object, **kwargs: object) -> None:
        raise OSError("simulated candidate discovery failure")

    monkeypatch.setattr(Path, method, fail_candidate_discovery)

    generic = safe_load_set_profile("tst", "quickdraft", profile_path=tmp_path / "missing.json")
    assert generic.source == "generic"
    assert any(f":{method}:" in diagnostic for diagnostic in generic.diagnostics)

    from_last_valid = safe_load_set_profile(
        "tst",
        "quickdraft",
        profile_path=tmp_path / "missing.json",
        last_valid_profile=last_valid,
    )
    assert from_last_valid.source == "last-valid"
    assert from_last_valid.profile is last_valid
    assert any(f":{method}:" in diagnostic for diagnostic in from_last_valid.diagnostics)


def test_recursive_json_decoder_failure_is_wrapped_and_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    last_valid = load_set_profile(FIXTURE_DIR / "semantic-only.json")
    path = tmp_path / "recursive.json"
    path.write_text("{}", encoding="utf-8")

    def fail_json_loads(*args: object, **kwargs: object) -> None:
        raise RecursionError("simulated recursive JSON")

    monkeypatch.setattr(set_profile_module.json, "loads", fail_json_loads)

    with pytest.raises(SetProfileError, match="Could not load set profile"):
        load_set_profile(path)

    generic = safe_load_set_profile("tst", "quickdraft", profile_path=path)
    assert generic.source == "generic"
    assert any("simulated recursive JSON" in diagnostic for diagnostic in generic.diagnostics)

    from_last_valid = safe_load_set_profile(
        "tst",
        "quickdraft",
        profile_path=path,
        last_valid_profile=last_valid,
    )
    assert from_last_valid.source == "last-valid"
    assert from_last_valid.profile is last_valid


def test_safe_loader_uses_compatible_last_valid_then_generic() -> None:
    last_valid = load_set_profile(FIXTURE_DIR / "semantic-only.json")
    from_last_valid = safe_load_set_profile(
        "tst",
        "quickdraft",
        profile_path=FIXTURE_DIR / "missing.json",
        last_valid_profile=last_valid,
    )
    assert from_last_valid.source == "last-valid"
    assert from_last_valid.profile is last_valid

    wrong_last = SetProfile(
        set_code="other",
        event_format=last_valid.event_format,
        profile_version=last_valid.profile_version,
        generated_at=last_valid.generated_at,
        source=last_valid.source,
        maturity=ProfileMaturity.METADATA_ONLY,
        samples=last_valid.samples,
        confidence=last_valid.confidence,
        pairs=last_valid.pairs,
        role_profile=None,
    )
    generic = safe_load_set_profile(
        "tst",
        "quickdraft",
        profile_path=FIXTURE_DIR / "missing.json",
        last_valid_profile=wrong_last,
    )
    assert generic.source == "generic"
    assert any("rejected:last-valid" in diagnostic for diagnostic in generic.diagnostics)


def test_semantic_roles_survive_absent_empirical_sections_and_incompatible_data_does_not_merge() -> None:
    profile = load_set_profile(FIXTURE_DIR / "semantic-only.json")
    card = {
        "oracle_id": "wu-bomb",
        "name": "WU Bomb",
        "set": "tst",
        "oracle_text": "Draw a card.",
    }
    resolved = profile.resolve_roles(card)
    assert resolved.source == "compiled_profile"
    role_profile = profile.role_profile
    assert role_profile is not None
    assert resolved.assignments == role_profile.cards[0].assignments

    incompatible = CompiledRoleProfile(
        set_code="tst",
        cards=(ProfileCard(key="oracle_id:wu-bomb", assignments=(RoleAssignment(Role.RAMP),)),),
        role_schema_version=999,
    )
    profile_with_incompatible_roles = SetProfile(
        set_code=profile.set_code,
        event_format=profile.event_format,
        profile_version=profile.profile_version,
        generated_at=profile.generated_at,
        source=profile.source,
        maturity=profile.maturity,
        samples=profile.samples,
        confidence=profile.confidence,
        pairs=profile.pairs,
        role_profile=incompatible,
    )
    fallback = profile_with_incompatible_roles.resolve_roles(card)
    assert fallback.source == "local_classifier"
    assert fallback.diagnostics == ("profile_incompatible_versions:used_local_classifier",)
    assert Role.RAMP not in fallback.assignments
def _rate(
    *,
    raw_value: float | None = 0.55,
    value: float = 0.54,
    samples: int = 12,
    prior_value: float = 0.50,
    source: str = "17lands",
    aggregate_evidence: AggregateEvidence | None = None,
) -> RateEstimate:
    return RateEstimate(
        raw_value=raw_value,
        value=value,
        samples=samples,
        prior_value=prior_value,
        source=source,
        aggregate_evidence=aggregate_evidence,
    )


def test_early_card_and_pair_evidence_round_trip_is_deterministic_without_samples() -> None:
    profile = SetProfile(
        set_code="TST",
        event_format="QuickDraft",
        profile_version="generator-1",
        generated_at="2026-08-30T00:00:00+00:00",
        source=set_profile_module.SourceMetadata(provider="fixture"),
        maturity=ProfileMaturity.EARLY,
        samples=None,
        confidence=0.4,
        pairs=(PairProfile(pair="RG", performance=_rate()),),
        card_ratings=(
            CardRating(
                card_key="oracle_id:z",
                gih_win_rate=_rate(),
                average_last_seen_at=3.2,
            ),
            CardRating(card_key="oracle_id:a", gih_win_rate=_rate(value=0.53)),
        ),
    )

    assert tuple(item.card_key for item in profile.card_ratings) == (
        "oracle_id:a",
        "oracle_id:z",
    )
    rg = profile.pair("RG")
    assert rg is not None
    assert rg.performance == _rate()
    restored = SetProfile.from_json(profile.to_json())
    assert restored.card_ratings == profile.card_ratings
    assert restored.to_bytes() == profile.to_bytes()
    assert profile.to_bytes() == (
        json.dumps(
            profile.to_json(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


@pytest.mark.parametrize(
    ("target_type", "payload"),
    (
        (NumericTarget, {"name": "curve", "value": 3.0, "raw_value": 2.0}),
        (RoleTarget, {"role": "draw", "value": 1.0, "samples": 4}),
        (RemovalTarget, {"kind": "destroy", "value": 1.0, "source": "17lands"}),
    ),
)
def test_target_evidence_requires_all_fields(
    target_type: type[object],
    payload: dict[str, object],
) -> None:
    with pytest.raises(SetProfileSchemaError, match="evidence"):
        target_type.from_json(payload)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"raw_value": 1.1}, "raw_value"),
        ({"value": float("nan")}, "value"),
        ({"prior_value": -0.1}, "prior_value"),
        ({"samples": -1}, "samples"),
        ({"source": "  "}, "source"),
    ),
)
def test_rate_estimate_rejects_invalid_values(kwargs: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "raw_value": 0.55,
        "value": 0.54,
        "samples": 12,
        "prior_value": 0.50,
        "source": "17lands",
    }
    values.update(kwargs)
    with pytest.raises(SetProfileSchemaError, match=message):
        RateEstimate(**values)  # type: ignore[arg-type]


def test_rate_estimate_zero_samples_is_prior_only() -> None:
    estimate = _rate(raw_value=None, value=0.5, samples=0)
    assert estimate.raw_value is None
    assert RateEstimate.from_json(estimate.to_json()) == estimate

    with pytest.raises(SetProfileSchemaError, match="raw_value"):
        _rate(raw_value=0.55, samples=0)

    with pytest.raises(SetProfileSchemaError, match="raw_value"):
        _rate(raw_value=None, samples=1)

def test_aggregate_evidence_round_trip_is_strict() -> None:
    evidence = AggregateEvidence(" PremierDraft ", "thin-exact-evidence", 0.65)
    assert evidence.to_json() == {
        "source_format": "premierdraft",
        "fallback_reason": "thin-exact-evidence",
        "confidence": 0.65,
    }
    assert AggregateEvidence.from_json(evidence.to_json()) == evidence

    with pytest.raises(SetProfileSchemaError, match="exactly"):
        AggregateEvidence.from_json(
            {
                "source_format": "premierdraft",
                "fallback_reason": None,
                "confidence": 0.5,
                "extra": True,
            }
        )
    with pytest.raises(SetProfileSchemaError, match="fallback reason"):
        AggregateEvidence("premierdraft", "unknown", 0.5)
    with pytest.raises(SetProfileSchemaError, match="path separators"):
        AggregateEvidence("premier/draft", None, 0.5)


def test_schema_two_aggregate_authority_is_required_and_schema_one_rejects_it() -> None:
    exact = AggregateEvidence("quickdraft", None, 1.0)
    fallback = AggregateEvidence("premierdraft", "missing-exact-evidence", 0.65)
    exact_rate = _rate(aggregate_evidence=exact)
    fallback_rate = _rate(aggregate_evidence=fallback)
    profile = SetProfile(
        set_code="TST",
        event_format="quickdraft",
        profile_version="generator-2",
        generated_at="2026-08-30T00:00:00+00:00",
        source=set_profile_module.SourceMetadata(provider="fixture"),
        maturity=ProfileMaturity.EARLY,
        samples=None,
        confidence=0.4,
        pairs=(PairProfile("WU", performance=exact_rate),),
        card_ratings=(CardRating("oracle_id:a", fallback_rate),),
        schema_version=2,
    )
    restored = SetProfile.from_json(profile.to_json())
    assert restored == profile
    assert restored.card_ratings[0].gih_win_rate.aggregate_evidence == fallback

    with pytest.raises(SetProfileSchemaError, match="schema-1"):
        SetProfile(
            set_code=profile.set_code,
            event_format=profile.event_format,
            profile_version=profile.profile_version,
            generated_at=profile.generated_at,
            source=profile.source,
            maturity=profile.maturity,
            samples=profile.samples,
            confidence=profile.confidence,
            pairs=(),
            card_ratings=(CardRating("oracle_id:a", exact_rate),),
            schema_version=1,
        )
    with pytest.raises(SetProfileSchemaError, match="require aggregate authority"):
        SetProfile(
            set_code=profile.set_code,
            event_format=profile.event_format,
            profile_version=profile.profile_version,
            generated_at=profile.generated_at,
            source=profile.source,
            maturity=profile.maturity,
            samples=profile.samples,
            confidence=profile.confidence,
            pairs=(),
            card_ratings=(CardRating("oracle_id:a", _rate()),),
            schema_version=2,
        )


def test_schema_two_requires_pair_authority_and_rejects_invalid_combinations() -> None:
    base = {
        "set_code": "TST",
        "profile_version": "generator-2",
        "generated_at": "2026-08-30T00:00:00+00:00",
        "source": set_profile_module.SourceMetadata(provider="fixture"),
        "maturity": ProfileMaturity.EARLY,
        "samples": None,
        "confidence": 0.4,
        "card_ratings": (),
        "schema_version": 2,
    }
    with pytest.raises(SetProfileSchemaError):
        SetProfile(
            event_format="quickdraft",
            pairs=(PairProfile("WU", performance=_rate()),),
            **base,
        )

    invalid = (
        ("quickdraft", AggregateEvidence("quickdraft", "thin-exact-evidence", 0.5)),
        ("premierdraft", AggregateEvidence("traddraft", "missing-exact-evidence", 0.5)),
        ("quickdraft", AggregateEvidence("alchemydraft", "missing-exact-evidence", 0.5)),
        ("quickdraft", AggregateEvidence("premierdraft", None, 0.5)),
    )
    for event_format, evidence in invalid:
        with pytest.raises(SetProfileSchemaError):
            SetProfile(
                event_format=event_format,
                pairs=(
                    PairProfile(
                        "WU",
                        performance=_rate(aggregate_evidence=evidence),
                    ),
                ),
                **base,
            )


def test_card_ratings_reject_duplicate_card_identities() -> None:
    rating = CardRating(card_key="Oracle_ID:Bomb", gih_win_rate=_rate())
    with pytest.raises(SetProfileSchemaError, match="duplicate"):
        SetProfile(
            set_code="TST",
            event_format="quickdraft",
            profile_version="generator-1",
            generated_at="2026-08-30T00:00:00+00:00",
            source=set_profile_module.SourceMetadata(provider="fixture"),
            maturity=ProfileMaturity.EARLY,
            samples=None,
            confidence=0.4,
            pairs=(),
            card_ratings=(
                rating,
                CardRating(card_key="oracle_id:bomb", gih_win_rate=_rate()),
            ),
        )


@pytest.mark.parametrize(
    ("maturity", "pairs", "card_ratings"),
    (
        (ProfileMaturity.EARLY, (), (CardRating("a", _rate()),)),
        (ProfileMaturity.MATURE, (PairProfile("WU", performance=_rate()),), ()),
    ),
)
def test_card_and_pair_rates_count_as_empirical_evidence(
    maturity: ProfileMaturity,
    pairs: tuple[PairProfile, ...],
    card_ratings: tuple[CardRating, ...],
) -> None:
    profile = SetProfile(
        set_code="TST",
        event_format="quickdraft",
        profile_version="generator-1",
        generated_at="2026-08-30T00:00:00+00:00",
        source=set_profile_module.SourceMetadata(provider="fixture"),
        maturity=maturity,
        samples=None,
        confidence=0.4,
        pairs=pairs,
        card_ratings=card_ratings,
    )
    assert profile.maturity is maturity


@pytest.mark.parametrize("maturity", (ProfileMaturity.EARLY, ProfileMaturity.MATURE))
def test_prior_only_pair_rates_do_not_count_as_empirical_evidence(maturity: ProfileMaturity) -> None:
    prior_only = _rate(raw_value=None, value=0.5, samples=0)
    pairs = tuple(PairProfile(pair=pair, performance=prior_only) for pair in COLOR_PAIRS)

    with pytest.raises(SetProfileSchemaError, match="empirical evidence"):
        SetProfile(
            set_code="TST",
            event_format="quickdraft",
            profile_version="generator-1",
            generated_at="2026-08-30T00:00:00+00:00",
            source=set_profile_module.SourceMetadata(provider="fixture"),
            maturity=maturity,
            samples=None,
            confidence=0.4,
            pairs=pairs,
        )


@pytest.mark.parametrize("maturity", (ProfileMaturity.EARLY, ProfileMaturity.MATURE))
def test_observed_pair_rates_count_as_empirical_evidence(maturity: ProfileMaturity) -> None:
    observed = _rate(samples=1)
    pairs = tuple(PairProfile(pair=pair, performance=observed) for pair in COLOR_PAIRS)

    profile = SetProfile(
        set_code="TST",
        event_format="quickdraft",
        profile_version="generator-1",
        generated_at="2026-08-30T00:00:00+00:00",
        source=set_profile_module.SourceMetadata(provider="fixture"),
        maturity=maturity,
        samples=None,
        confidence=0.4,
        pairs=pairs,
    )

    assert profile.maturity is maturity


def test_maturity_rejects_new_evidence_in_metadata_semantic_and_generic_profiles() -> None:
    common = {
        "set_code": "TST",
        "event_format": "quickdraft",
        "profile_version": "generator-1",
        "generated_at": "2026-08-30T00:00:00+00:00",
        "source": set_profile_module.SourceMetadata(provider="fixture"),
        "samples": None,
        "confidence": 0.4,
    }
    card_ratings = (CardRating("a", _rate()),)
    with pytest.raises(SetProfileSchemaError, match="cannot contain empirical evidence"):
        SetProfile(maturity=ProfileMaturity.METADATA_ONLY, pairs=(), card_ratings=card_ratings, **common)
    with pytest.raises(SetProfileSchemaError, match="cannot contain empirical evidence"):
        SetProfile(
            maturity=ProfileMaturity.SEMANTIC_ONLY,
            pairs=(PairProfile("WU", performance=_rate()),),
            role_profile=load_set_profile(FIXTURE_DIR / "semantic-only.json").role_profile,
            card_ratings=(),
            **common,
        )
    with pytest.raises(SetProfileSchemaError, match="cannot contain evidence"):
        SetProfile(maturity=ProfileMaturity.GENERIC, pairs=(), card_ratings=card_ratings, **common)


def test_target_evidence_round_trip_preserves_all_fields() -> None:
    targets = (
        NumericTarget(
            name="average_land_count",
            value=3.2,
            raw_value=3.0,
            prior_value=3.4,
            samples=17,
            source="17lands",
        ),
        RoleTarget(
            role=Role.DRAW,
            value=0.8,
            raw_value=0.75,
            prior_value=0.6,
            samples=17,
            source="17lands",
        ),
        RemovalTarget(
            kind="destroy",
            value=0.7,
            raw_value=0.65,
            prior_value=0.5,
            samples=17,
            source="17lands",
        ),
    )
    for target in targets:
        restored = type(target).from_json(target.to_json())
        assert restored == target


_REMOVED = object()


def _enhanced_payload() -> dict[str, object]:
    return json.loads((FIXTURE_DIR / "enhanced.json").read_text(encoding="utf-8"))


def _apply(
    payload: dict[str, object],
    *operations: tuple[tuple[object, ...], object],
) -> dict[str, object]:
    for path, value in operations:
        target: object = payload
        for key in path[:-1]:
            target = target[key]  # type: ignore[index]
        if callable(value):
            value = value(target[path[-1]])  # type: ignore[operator]
        if value is _REMOVED:
            del target[path[-1]]  # type: ignore[index]
        else:
            target[path[-1]] = value  # type: ignore[index]
    return payload


def _runs_with_unreferenced_copy(runs: object) -> object:
    first = runs[0]  # type: ignore[index]
    return [first, {**first, "run_id": "run-2"}]  # type: ignore[operator]


def test_enhanced_profile_round_trip_preserves_enhancement_content_and_provenance() -> None:
    profile = load_set_profile(
        FIXTURE_DIR / "enhanced.json",
        expected_set_code="TST",
        expected_format="QuickDraft",
    )

    assert profile.schema_version == 3
    assert profile.enhancement_status is EnhancementStatus.ENHANCED
    enhancement = profile.enhancement
    assert enhancement is not None
    assert enhancement.card_data == EnhancementCardData(
        "scryfall-default-cards",
        "e97753089bb1b800165c1e84a8ada89b18b082dad70a0b7f84eb744c303d332b",
        3,
    )
    assert [pin.card_id for pin in enhancement.cards] == [101, 102, 103]
    assert enhancement.artifact_sha256 == "e43b831bd69c7c8c73a32fe348ef887712ee67493c7b7b64d4612e2128709a87"
    assert (
        enhancement.runs[0].prompt_sha256
        == "f580e1722c8ceaf5602bacfd653f477192f481468eaf48177bc1f636455fefb2"
    )
    assert enhancement.review.state == "confirmed"
    assert enhancement.confidence == 0.72
    assert profile.confidence == 0.6
    assert SetProfile.from_json(profile.to_json()).to_bytes() == profile.to_bytes()


def test_explicitly_unenhanced_schema_three_profile_round_trips() -> None:
    profile = load_set_profile(
        FIXTURE_DIR / "unenhanced.json",
        expected_set_code="TST",
        expected_format="QuickDraft",
    )

    assert profile.schema_version == 3
    assert profile.enhancement is None
    assert profile.enhancement_status is EnhancementStatus.NOT_ENHANCED
    serialized = profile.to_json()
    assert serialized["enhancement_status"] == "not-enhanced"
    assert "enhancement" not in serialized
    assert SetProfile.from_json(serialized).to_bytes() == profile.to_bytes()


def test_schema_one_and_two_fixtures_load_as_not_enhanced_without_fabricating_metadata() -> None:
    early = load_set_profile(FIXTURE_DIR / "early.json")
    profiles = (
        load_set_profile(FIXTURE_DIR / "mature.json"),
        early,
        load_set_profile(FIXTURE_DIR / "metadata-only.json"),
        load_set_profile(FIXTURE_DIR / "semantic-only.json"),
        load_set_profile(Path(__file__).parents[1] / "draftomen" / "baseline_profiles" / "hob-quickdraft.json"),
        replace(early, schema_version=2),
    )

    for profile in profiles:
        assert profile.enhancement is None
        assert profile.enhancement_status is EnhancementStatus.NOT_ENHANCED
        serialized = profile.to_json()
        assert "enhancement" not in serialized
        assert "enhancement_status" not in serialized
        assert SetProfile.from_json(serialized).to_bytes() == profile.to_bytes()


def test_semantic_relationships_and_empirical_synergy_remain_separate_fields() -> None:
    profile = load_set_profile(
        FIXTURE_DIR / "enhanced.json",
        expected_set_code="TST",
        expected_format="QuickDraft",
    )
    enhancement = profile.enhancement
    assert enhancement is not None
    relationship = enhancement.relationships[0]
    assert isinstance(relationship, CardRelationship)

    serialized = profile.to_json()
    assert "synergy" not in serialized["enhancement"]  # type: ignore[operator]
    assert serialized["enhancement"]["relationships"][0]["mechanism"] == "token-go-wide-payoff"  # type: ignore[index]

    empirical = CardPairSynergy(first_card="oracle_id:a", second_card="oracle_id:b", value=0.4)
    early = load_set_profile(FIXTURE_DIR / "early.json")
    empirical_profile = replace(early, pairs=(replace(early.pairs[0], synergy=(empirical,)),))
    assert empirical_profile.to_json()["pair_profiles"][0]["synergy"] == [  # type: ignore[index]
        {"first_card": "oracle_id:a", "second_card": "oracle_id:b", "value": 0.4}
    ]
    assert isinstance(empirical_profile.pairs[0].synergy[0], CardPairSynergy)

    with pytest.raises(SetProfileSchemaError, match="must contain the expected target objects"):
        replace(empirical_profile.pairs[0], synergy=(relationship,))
    with pytest.raises(SetProfileSchemaError, match="enhancement.relationships must contain CardRelationship"):
        replace(enhancement, relationships=(empirical,))


def test_profile_fingerprint_includes_enhancement_content_and_provenance() -> None:
    profile = load_set_profile(
        FIXTURE_DIR / "enhanced.json",
        expected_set_code="TST",
        expected_format="QuickDraft",
    )
    fingerprint = profile.fingerprint
    assert SetProfile.from_json(profile.to_json()).fingerprint == fingerprint

    for operations in (
        ((("enhancement", "confidence"), 0.5),),
        ((("enhancement", "artifact_sha256"), "0" * 64),),
        ((("enhancement", "relationships", 0, "claim"), "changed claim"),),
    ):
        payload = _enhanced_payload()
        _apply(payload, *operations)
        assert SetProfile.from_json(payload).fingerprint != fingerprint


_ENHANCEMENT_REJECTION_CASES: tuple[tuple[str, tuple[tuple[tuple[object, ...], object], ...], str], ...] = (
    ("missing-status", ((("enhancement_status",), _REMOVED),), "Missing required field enhancement_status"),
    ("unknown-status", ((("enhancement_status",), "unenhanced"),), "Unsupported enhancement status"),
    (
        "status-not-enhanced-with-data",
        ((("enhancement_status",), "not-enhanced"),),
        "exactly when enhancement data is present",
    ),
    (
        "missing-enhancement-with-enhanced-status",
        ((("enhancement",), _REMOVED),),
        "exactly when enhancement data is present",
    ),
    (
        "future-artifact-schema",
        ((("enhancement", "artifact_schema_version"), SEMANTIC_ENRICHMENT_SCHEMA_VERSION + 1),),
        "Unsupported semantic enrichment schema",
    ),
    (
        "pending-review",
        ((("enhancement", "review"), {"state": "pending", "reviewer_id": None, "reviewed_at": None}),),
        "confirmed enrichment review",
    ),
    (
        "cancelled-review",
        (
            (
                ("enhancement", "review"),
                {"state": "cancelled", "reviewer_id": "local-review", "reviewed_at": "2026-08-27T10:10:00+00:00"},
            ),
        ),
        "confirmed enrichment review",
    ),
    ("set-code-mismatch", ((("enhancement", "set_code"), "other"),), "enhancement.set_code must match set_code"),
    (
        "no-confirmed-findings",
        ((("enhancement", "mechanics"), []), (("enhancement", "relationships"), [])),
        "at least one confirmed semantic relationship or mechanic finding",
    ),
    ("card-count-mismatch", ((("enhancement", "card_data", "card_count"), 4),), "cover the declared card data exactly"),
    ("empty-cards-with-declared-count", ((("enhancement", "cards"), []),), "enhancement.cards must not be empty"),
    (
        "unpinned-participants",
        (
            (("enhancement", "relationships", 0, "participants"), [102, 999]),
            (
                ("enhancement", "relationships", 0, "oracle_evidence"),
                [
                    {"card_id": 102, "face_index": None, "quote": "Create a 1/1 red Goblin creature token."},
                    {"card_id": 999, "face_index": None, "quote": "Unpinned card quote."},
                ],
            ),
        ),
        "pinned card data",
    ),
    (
        "unpinned-oracle-evidence",
        ((("enhancement", "relationships", 0, "oracle_evidence", 0, "card_id"), 999),),
        "Oracle evidence for exactly their participants",
    ),
    (
        "rejected-relationship",
        ((("enhancement", "relationships", 0, "review"), {"status": "rejected", "reason": "no support"}),),
        "accepted findings",
    ),
    (
        "rejected-mechanic",
        ((("enhancement", "mechanics", 0, "review"), {"status": "rejected", "reason": "no support"}),),
        "enhancement.mechanics must contain accepted findings",
    ),
    ("wrong-mechanic-category", ((("enhancement", "mechanics", 0, "category"), "archetype"),), "mechanic category"),
    ("missing-run-reference", ((("enhancement", "mechanics", 0, "run_id"), "run-missing"),), "recorded model run"),
    (
        "unrecorded-guide-source",
        ((("enhancement", "mechanics", 0, "evidence"), [{"guide_id": "other-guide", "quote": "x"}]),),
        "recorded guide source",
    ),
    (
        "duplicate-finding-id",
        ((("enhancement", "mechanics", 0, "finding_id"), "relationship-token-go-wide"),),
        "globally unique",
    ),
    (
        "unreferenced-run",
        ((("enhancement", "runs"), _runs_with_unreferenced_copy),),
        "referenced by an included finding",
    ),
    ("unbounded-confidence", ((("enhancement", "confidence"), 1.5),), "enhancement.confidence"),
    ("malformed-artifact-digest", ((("enhancement", "artifact_sha256"), "not-a-digest"),), "SHA-256 digest"),
    (
        "empty-cards-and-zero-count",
        ((("enhancement", "cards"), []), (("enhancement", "card_data", "card_count"), 0)),
        "card_count must be a positive integer",
    ),
    ("schema-two-cannot-declare-enhancement", ((("schema_version",), 2),), "cannot declare enhancement data"),
    ("schema-one-cannot-declare-enhancement", ((("schema_version",), 1),), "cannot declare enhancement data"),
)


@pytest.mark.parametrize(
    ("operations", "expected"),
    [row[1:] for row in _ENHANCEMENT_REJECTION_CASES],
    ids=[row[0] for row in _ENHANCEMENT_REJECTION_CASES],
)
def test_enhancement_rejects_malformed_unconfirmed_mismatched_and_incompatible_data(
    operations: tuple[tuple[tuple[object, ...], object], ...],
    expected: str,
) -> None:
    payload = _enhanced_payload()
    _apply(payload, *operations)
    with pytest.raises(SetProfileSchemaError, match=expected):
        SetProfile.from_json(payload)


def test_generic_profiles_reject_enhancement_data() -> None:
    enhancement = load_set_profile(FIXTURE_DIR / "enhanced.json").enhancement
    generic = SetProfile.generic(set_code="TST", event_format="quickdraft")

    with pytest.raises(SetProfileSchemaError, match="generic profiles cannot contain enhancement data"):
        replace(generic, schema_version=3, enhancement=enhancement)


FIXTURE_SOURCE_CARD_ID = 102
FIXTURE_TARGET_CARD_ID = 103
FIXTURE_TOKEN_QUOTE = "Create a 1/1 red Goblin creature token."
FIXTURE_ATTACK_QUOTE = "Whenever a creature you control attacks, it gets +1/+0 until end of turn."
FIXTURE_ANTHEM_QUOTE = "Creatures you control get +1/+1."


def _fixture_projection() -> RelationshipPrerequisiteProjection:
    """Build one self-consistent typed projection for the enhanced fixture pair."""
    payload = _enhanced_payload()
    pins = {
        pin["card_id"]: pin["sha256"]
        for pin in payload["enhancement"]["cards"]  # type: ignore[index]
    }
    source = RelationshipParticipant(
        card_id=FIXTURE_SOURCE_CARD_ID,
        capability_id="capability-token-maker",
        card_name="Goblin Enabler",
        face_index=None,
        face_name=None,
        card_source_sha256=pins[FIXTURE_SOURCE_CARD_ID],
        role=Role.TOKEN_MAKER,
        capability_prerequisites=(),
        prerequisites=(
            RelationshipPrerequisite(
                kind=PrerequisiteKind.CONDITION,
                subject="output",
                operation="create",
                object_kind="token",
                card_types=("creature",),
                type_operator="all_of",
                token_restriction="token",
                exclusion="none",
                subtype="Goblin",
                color_operator="exact",
                colors=("R",),
                controller="you",
                owner="not_applicable",
                quantity=CapabilityQuantity(value=1, relation=QuantityRelation.EXACTLY),
                source_zone=None,
                destination_zone=RelationshipZone(
                    zone=CapabilityZone.BATTLEFIELD,
                    player="you",
                ),
                timing=RelationshipTiming(window="unrestricted", turn="any", max_per_turn=None),
                required_card_id=None,
                evidence=OracleEvidence(
                    card_id=FIXTURE_SOURCE_CARD_ID,
                    face_index=None,
                    quote=FIXTURE_TOKEN_QUOTE,
                ),
                operation_quote="Create",
                operation_occurrence=0,
                object_quote="a 1/1 red Goblin creature token",
                object_occurrence=0,
                capability_prerequisite_indices=(),
            ),
        ),
    )
    target = RelationshipParticipant(
        card_id=FIXTURE_TARGET_CARD_ID,
        capability_id="capability-attack-payoff",
        card_name="Attack Payoff",
        face_index=None,
        face_name=None,
        card_source_sha256=pins[FIXTURE_TARGET_CARD_ID],
        role=Role.GO_WIDE_PAYOFF,
        capability_prerequisites=(),
        prerequisites=(
            RelationshipPrerequisite(
                kind=PrerequisiteKind.CONDITION,
                subject="participant",
                operation="control",
                object_kind="permanent",
                card_types=("creature",),
                type_operator="all_of",
                token_restriction="unrestricted",
                exclusion="none",
                subtype=None,
                color_operator="unrestricted",
                colors=(),
                controller="you",
                owner="not_applicable",
                quantity=None,
                source_zone=None,
                destination_zone=None,
                timing=RelationshipTiming(window="unrestricted", turn="any", max_per_turn=None),
                required_card_id=None,
                evidence=OracleEvidence(
                    card_id=FIXTURE_TARGET_CARD_ID,
                    face_index=None,
                    quote=FIXTURE_ANTHEM_QUOTE,
                ),
                operation_quote="control",
                operation_occurrence=0,
                object_quote="Creatures you control",
                object_occurrence=0,
                capability_prerequisite_indices=(),
            ),
        ),
    )
    return RelationshipPrerequisiteProjection(source=source, target=target)


def _enhanced_payload_with_projection() -> dict[str, Any]:
    """Return the enhanced fixture payload carrying the typed projection."""
    payload = _enhanced_payload()
    relationship = payload["enhancement"]["relationships"][0]  # type: ignore[index]
    # The payoff clause binds to a retained anthem paragraph: the fixture's attack trigger
    # quote states "until end of turn", which the bounded timing vocabulary rejects.
    relationship["oracle_evidence"].append(  # type: ignore[union-attr]
        {
            "card_id": FIXTURE_TARGET_CARD_ID,
            "face_index": None,
            "quote": FIXTURE_ANTHEM_QUOTE,
        }
    )
    relationship["prerequisite_projection"] = _fixture_projection().to_json()  # type: ignore[index]
    return payload


def test_legacy_enhanced_and_unenhanced_fixtures_stay_projection_free() -> None:
    enhanced = load_set_profile(
        FIXTURE_DIR / "enhanced.json",
        expected_set_code="TST",
        expected_format="QuickDraft",
    )
    enhancement = enhanced.enhancement
    assert enhancement is not None
    relationship = enhancement.relationships[0]
    assert relationship.prerequisite_projection is None
    assert relationship.prerequisites == ("a creature token is created",)
    assert relationship.identity[2] == ()
    serialized = enhanced.to_json()
    assert all(
        "prerequisite_projection" not in item
        for item in serialized["enhancement"]["relationships"]  # type: ignore[index]
    )
    assert SetProfile.from_json(serialized).to_bytes() == enhanced.to_bytes()

    unenhanced = load_set_profile(
        FIXTURE_DIR / "unenhanced.json",
        expected_set_code="TST",
        expected_format="QuickDraft",
    )
    assert unenhanced.enhancement is None
    assert SetProfile.from_json(unenhanced.to_json()).to_bytes() == unenhanced.to_bytes()


def test_typed_projection_survives_profile_serialization_with_direction() -> None:
    profile = SetProfile.from_json(_enhanced_payload_with_projection())
    enhancement = profile.enhancement
    assert enhancement is not None
    relationship = enhancement.relationships[0]
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert relationship.identity[2] == (
        FIXTURE_SOURCE_CARD_ID,
        "capability-token-maker",
        -1,
        FIXTURE_TARGET_CARD_ID,
        "capability-attack-payoff",
        -1,
    )
    assert projection.source.prerequisites[0].colors == ("R",)
    assert projection.source.prerequisites[0].quantity == CapabilityQuantity(
        value=1,
        relation=QuantityRelation.EXACTLY,
    )
    assert projection.target.prerequisites[0].operation == "control"
    assert projection.target.prerequisites[0].card_types == ("creature",)
    assert projection.target.prerequisites[0].controller == "you"
    assert projection.target.prerequisites[0].evidence.quote == FIXTURE_ANTHEM_QUOTE
    assert projection.source.prerequisites[0].evidence.quote == FIXTURE_TOKEN_QUOTE
    assert (
        OracleEvidence(
            card_id=FIXTURE_TARGET_CARD_ID,
            face_index=None,
            quote=FIXTURE_ATTACK_QUOTE,
        )
        in relationship.oracle_evidence
    )

    restored = SetProfile.from_json(profile.to_json())
    assert restored.to_bytes() == profile.to_bytes()
    restored_enhancement = restored.enhancement
    assert restored_enhancement is not None
    assert restored_enhancement.relationships[0].prerequisite_projection == projection
    assert restored_enhancement.relationships[0].claim == "A token maker pairs with a go-wide payoff."


def test_enhanced_profile_reader_rejects_invalid_present_projections() -> None:
    unsupported = _enhanced_payload_with_projection()
    unsupported["enhancement"]["relationships"][0]["prerequisite_projection"]["schema_version"] = 2  # type: ignore[index]
    with pytest.raises(SetProfileSchemaError, match="schema_version is unsupported"):
        SetProfile.from_json(unsupported)

    missing_target = _enhanced_payload_with_projection()
    del missing_target["enhancement"]["relationships"][0]["prerequisite_projection"]["target"]  # type: ignore[index]
    with pytest.raises(SetProfileSchemaError, match="Invalid enhancement"):
        SetProfile.from_json(missing_target)

    mismatched_hash = _enhanced_payload_with_projection()
    mismatched_hash["enhancement"]["relationships"][0]["prerequisite_projection"]["source"][  # type: ignore[index]
        "card_source_sha256"
    ] = "0" * 64
    with pytest.raises(SetProfileSchemaError, match="hash must match its card source pin"):
        SetProfile.from_json(mismatched_hash)

    mismatched_participants = _enhanced_payload_with_projection()
    mismatched_participants["enhancement"]["relationships"][0]["participants"] = [101, FIXTURE_TARGET_CARD_ID]  # type: ignore[index]
    with pytest.raises(SetProfileSchemaError, match="projection participants must match"):
        SetProfile.from_json(mismatched_participants)

    uncertain = _enhanced_payload_with_projection()
    uncertain["enhancement"]["relationships"][0]["review"] = {  # type: ignore[index]
        "status": "uncertain",
        "reason": "Needs a reviewer.",
    }
    with pytest.raises(SetProfileSchemaError, match="accepted review"):
        SetProfile.from_json(uncertain)


def _stored_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the enhanced fixture relationship's stored projection object."""
    return payload["enhancement"]["relationships"][0]["prerequisite_projection"]  # type: ignore[index]


def _stored_clause(payload: dict[str, Any], side: str) -> dict[str, Any]:
    """Return one stored participant's only atomic clause object."""
    return _stored_projection(payload)[side]["prerequisites"][0]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    (
        (
            lambda payload: _stored_clause(payload, "source").update({"colors": ["U"]}),
            "contradict their source evidence",
        ),
        (
            lambda payload: _stored_projection(payload)["source"].update({"face_index": 1}),
            "contradict their source evidence",
        ),
        (
            lambda payload: _stored_projection(payload)["source"].update(
                {"card_id": FIXTURE_TARGET_CARD_ID}
            ),
            "contradict their source evidence",
        ),
        (
            lambda payload: _stored_projection(payload)["source"].update(
                {"role": Role.GO_WIDE_PAYOFF.value}
            ),
            "relationship prerequisites are incomplete",
        ),
        (
            lambda payload: _stored_clause(payload, "target").update({"controller": "opponent"}),
            "contradict their source evidence",
        ),
        (
            lambda payload: _stored_clause(payload, "source").update({"required_card_id": 999}),
            "relationship prerequisites are incomplete",
        ),
        (
            lambda payload: payload["enhancement"]["relationships"][0].update(
                {"prerequisite_projection": None}
            ),
            "prerequisite_projection must be an object when present",
        ),
    ),
    ids=(
        "clause-color-contradicts-its-quotation",
        "participant-face-index-mismatches-its-clause",
        "participant-card-id-mismatches-its-clause",
        "participant-role-mismatches-its-clauses",
        "clause-controller-contradicts-its-quotation",
        "clause-required-card-is-not-a-participant",
        "explicit-null-projection-is-not-absent",
    ),
)
def test_enhanced_profile_reader_rejects_mutated_stored_projection_clauses(
    mutate: Any,
    expected: str,
) -> None:
    payload = _enhanced_payload_with_projection()
    mutate(payload)

    with pytest.raises(SetProfileSchemaError, match=expected):
        SetProfile.from_json(payload)

    profile = SetProfile.from_json(_enhanced_payload_with_projection())
    enhancement = profile.enhancement
    assert enhancement is not None
    assert enhancement.relationships[0].prerequisite_projection is not None


def test_unloadable_stored_projection_falls_back_instead_of_losing_prerequisites(tmp_path: Path) -> None:
    payload = _enhanced_payload_with_projection()
    _stored_clause(payload, "source").update({"colors": ["U"]})
    path = tmp_path / "invalid-projection.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SetProfileSchemaError, match="contradict their source evidence"):
        load_set_profile(path)

    result = safe_load_set_profile("tst", "quickdraft", profile_path=path)

    assert result.source == "generic"
    assert result.profile.enhancement is None
    assert result.profile.enhancement_status is EnhancementStatus.NOT_ENHANCED
    assert any("contradict their source evidence" in diagnostic for diagnostic in result.diagnostics)


def test_enhanced_profile_reader_rejects_score_weight_and_adjustment_fields() -> None:
    scored = _enhanced_payload_with_projection()
    scored["enhancement"]["relationships"][0]["score"] = 0.75  # type: ignore[index]
    with pytest.raises(SetProfileSchemaError, match="unknown fields"):
        SetProfile.from_json(scored)

    weighted = _enhanced_payload_with_projection()
    weighted["enhancement"]["relationships"][0]["prerequisite_projection"]["source"]["prerequisites"][0][  # type: ignore[index]
        "weight"
    ] = 2
    with pytest.raises(SetProfileSchemaError, match="unknown fields"):
        SetProfile.from_json(weighted)

    adjusted = _enhanced_payload_with_projection()
    adjusted["enhancement"]["relationships"][0]["prerequisite_projection"]["adjustment"] = 0.25  # type: ignore[index]
    with pytest.raises(SetProfileSchemaError, match="unknown fields"):
        SetProfile.from_json(adjusted)


def test_typed_projection_ignores_claim_prose_at_the_profile_boundary() -> None:
    plain = SetProfile.from_json(_enhanced_payload_with_projection())
    prose = _enhanced_payload_with_projection()
    prose["enhancement"]["relationships"][0]["claim"] = "Score 0.8: the enabler is worth four points."  # type: ignore[index]
    prose["enhancement"]["relationships"][0]["prerequisites"] = ["a different reading"]  # type: ignore[index]
    verbose = SetProfile.from_json(prose)

    plain_enhancement = plain.enhancement
    verbose_enhancement = verbose.enhancement
    assert plain_enhancement is not None
    assert verbose_enhancement is not None
    plain_projection = plain_enhancement.relationships[0].prerequisite_projection
    verbose_projection = verbose_enhancement.relationships[0].prerequisite_projection
    assert plain_projection is not None
    assert plain_projection == verbose_projection
    assert plain.enhancement.relationships[0].prerequisites != verbose.enhancement.relationships[0].prerequisites
    assert plain.fingerprint != verbose.fingerprint
