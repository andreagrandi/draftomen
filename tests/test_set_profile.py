from __future__ import annotations

import json
from pathlib import Path

import pytest

import draftomen.set_profile as set_profile_module

from draftomen.config import COLOR_PAIRS
from draftomen.semantic_roles import CompiledRoleProfile, ProfileCard, Role, RoleAssignment
from draftomen.set_profile import (
    AggregateEvidence,
    CardRating,
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
    unsupported["schema_version"] = 3
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
