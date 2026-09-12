from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest

import draftomen.profile_publication as publication
from draftomen.carddb import CardDatabase, CardInfo, load_card_database
from draftomen.config import COLOR_PAIRS, DeckBuilderConfig
from draftomen.profile_generation import ProfileGenerationConfig
from draftomen.public_dump import PublicDumpManifest, PublicDumpSource
from draftomen.profile_statistics import BetaPrior
from draftomen.seventeen import (
    ColorPairWinRate,
    RatingSampleCounts,
    SeventeenCardStats,
    SeventeenLandsFormatData,
)
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestArtifact,
    load_profile_manifest,
)
from draftomen.semantic_enrichment import (
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
    ReasoningConfig,
)
from draftomen.semantic_relationship_records import CardRelationship
from draftomen.set_profile import EnhancementStatus, SetProfile

from tests.test_profile_generation import (
    TYPED_SOURCE_CARD_ID,
    TYPED_TARGET_CARD_ID,
    _typed_database,
    _typed_enrichment_artifact,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "profile-generation"
GENERATED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _database() -> CardDatabase:
    return CardDatabase(
        cards={
            1: CardInfo(
                grp_id=1,
                name="Support Creature",
                colors=("W", "U"),
                mana_value=2,
                rarity="common",
                types=("Creature",),
                type_line="Creature — Advisor",
                oracle_text="Whenever this enters the battlefield, draw a card.",
                oracle_id="support-id",
                set_code="TST",
            ),
            2: CardInfo(
                grp_id=2,
                name="Removal Spell",
                colors=("W", "U"),
                mana_value=2,
                rarity="common",
                types=("Instant",),
                type_line="Instant",
                oracle_text="Destroy target creature.",
                oracle_id="removal-id",
                set_code="TST",
            ),
        }
    )


def _ratings() -> SeventeenLandsFormatData:
    return SeventeenLandsFormatData(
        set_code="TST",
        event_format="QuickDraft",
        fetched_at=GENERATED_AT,
        card_ratings={
            1: SeventeenCardStats(
                grp_id=1,
                name="Support Creature",
                color="WU",
                rarity="common",
                average_last_seen_at=3.5,
                gih_win_rate=0.60,
                opening_hand_win_rate=None,
                drawn_improvement_win_rate=None,
                sample_counts=RatingSampleCounts(100, 50, 40, 20, 10),
            )
        },
        pair_win_rates={
            pair: ColorPairWinRate(pair=pair, wins=6, games=10, win_rate=0.60)
            for pair in COLOR_PAIRS[:1]
        },
    )


def _config() -> ProfileGenerationConfig:
    return ProfileGenerationConfig(
        card_prior=BetaPrior(mean=0.50, strength=500.0),
        pair_prior=BetaPrior(mean=0.50, strength=500.0),
        deck_builder_config=DeckBuilderConfig(
            deck_size=40,
            structure_min_land_count=14,
            structure_max_land_count=20,
        ),
    )


def _write_inputs(
    tmp_path: Path,
    *,
    ratings: bool = False,
    card_database: CardDatabase | None = None,
) -> tuple[Path, Path | None]:
    card_database_path = tmp_path / "cards.json"
    card_database_path.write_text(
        json.dumps((_database() if card_database is None else card_database).to_json()),
        encoding="utf-8",
    )
    ratings_path = None
    if ratings:
        ratings_path = tmp_path / "ratings.json"
        ratings_path.write_text(json.dumps(_ratings().to_json()), encoding="utf-8")
    return card_database_path, ratings_path


def _manifest(tmp_path: Path, *names: str) -> Path:
    sources = []
    for name in names:
        source_path = FIXTURE_DIR / name
        relative_path = os.path.relpath(source_path, start=tmp_path)
        sources.append(
            PublicDumpSource(
                name=name,
                path=relative_path,
                sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                attribution="fixture public data",
                license="CC0",
            )
        )
    path = tmp_path / "manifest.json"
    path.write_bytes(PublicDumpManifest(sources=tuple(sources)).to_bytes())
    return path


def _publish(
    tmp_path: Path,
    *,
    stage: str = "metadata",
    ratings: bool = False,
    manifest: Path | None = None,
    draft_source_name: str | None = None,
    enrichment: SemanticEnrichmentArtifact | None = None,
    generated_at: datetime = GENERATED_AT,
    card_database: CardDatabase | None = None,
) -> publication.ProfilePublicationResult:
    card_database_path, ratings_path = _write_inputs(
        tmp_path,
        ratings=ratings,
        card_database=card_database,
    )
    return publication.generate_local_profile_artifacts(
        set_code="TST",
        event_format="quickdraft",
        stage=stage,
        generated_at=generated_at,
        card_database_path=card_database_path,
        output_dir=tmp_path / "published",
        ratings_path=ratings_path,
        source_manifest_path=manifest,
        draft_source_name=draft_source_name,
        enrichment=enrichment,
        config=_config(),
    )


ENRICHMENT_GUIDE_TEXT = "TST rewards going wide with support creatures."
ENRICHMENT_CREATED_AT = "2026-09-01T12:02:00Z"
ENRICHMENT_REVIEWED_AT = "2026-09-01T14:00:00Z"


def _enrichment_sources(cards: CardDatabase, *, set_code: str = "TST") -> EnrichmentSources:
    return EnrichmentSources(
        set_code=set_code,
        cards=tuple(cards.cards.values()),
        guides=(
            GuideSource(
                guide_id="tst-guide",
                url="https://draftsim.example.test/tst/",
                text=ENRICHMENT_GUIDE_TEXT,
                retrieved_at="2026-09-01T12:00:00Z",
            ),
        ),
    )


def _enrichment_run(
    *,
    run_id: str = "run-1",
    provider: str = "openrouter",
    model: str = "example/model",
) -> ModelRun:
    return ModelRun(
        run_id=run_id,
        provider=provider,
        model=model,
        reasoning=ReasoningConfig(
            enabled=True, effort="medium", max_tokens=4096, exclude=None
        ),
        prompt_id="set-relationship-analysis",
        prompt_sha256="a" * 64,
        response_schema_id="set-relationship-analysis-response",
        response_schema_sha256="b" * 64,
        started_at="2026-09-01T12:00:00Z",
        completed_at="2026-09-01T12:01:00Z",
        input_tokens=1200,
        output_tokens=400,
        reasoning_tokens=120,
        cost_usd="0.31",
    )


def _enrichment_mechanic() -> GuideClaim:
    return GuideClaim(
        finding_id="mechanic-wide-board",
        category="mechanic",
        name="wide board",
        claim=ENRICHMENT_GUIDE_TEXT,
        card_ids=(1, 2),
        evidence=(GuideEvidence(guide_id="tst-guide", quote="rewards going wide"),),
        review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
        run_id="run-1",
    )


def _enrichment_strategy() -> GuideClaim:
    return GuideClaim(
        finding_id="strategy-go-wide",
        category="strategy",
        name="go wide",
        claim=ENRICHMENT_GUIDE_TEXT,
        card_ids=(1,),
        evidence=(GuideEvidence(guide_id="tst-guide", quote="rewards going wide"),),
        review=FindingReview(status=FindingStatus.UNCERTAIN, reason="Not reviewed."),
        run_id="run-1",
    )


def _enrichment_relationship() -> CardRelationship:
    return CardRelationship(
        finding_id="relationship-draw-payoff",
        mechanism="draw-payoff",
        participants=(1, 2),
        claim="Support creature pairs with removal.",
        prerequisites=("A support creature is on the battlefield.",),
        oracle_evidence=(
            OracleEvidence(
                card_id=1,
                face_index=None,
                quote="Whenever this enters the battlefield, draw a card.",
            ),
            OracleEvidence(card_id=2, face_index=None, quote="Destroy target creature."),
        ),
        guide_evidence=(GuideEvidence(guide_id="tst-guide", quote="rewards going wide"),),
        review=FindingReview(status=FindingStatus.ACCEPTED, reason=None),
        run_id="run-1",
    )


def _eld_cards(cards: CardDatabase) -> CardDatabase:
    return CardDatabase(
        cards={
            card_id: replace(card, set_code="ELD") for card_id, card in cards.cards.items()
        }
    )


def _enrichment_artifact(
    card_database: CardDatabase,
    *,
    review: ArtifactReview | None = None,
    set_source_id: str = "tst-card-data-v1",
    cards: CardDatabase | None = None,
    set_code: str = "TST",
    guide_claims: tuple[GuideClaim, ...] | None = None,
    relationships: tuple[CardRelationship, ...] | None = None,
    confirmed_relationship_ids: tuple[str, ...] | None = None,
    run: ModelRun | None = None,
) -> SemanticEnrichmentArtifact:
    sources = _enrichment_sources(card_database if cards is None else cards, set_code=set_code)
    resolved_review = (
        ArtifactReview(
            state="confirmed",
            reviewer_id="local-review",
            reviewed_at=ENRICHMENT_REVIEWED_AT,
        )
        if review is None
        else review
    )
    if confirmed_relationship_ids is None:
        # Only a confirmed artifact may select relationships, so an unreviewed
        # artifact defaults to no selection rather than an invalid fixture.
        confirmed_relationship_ids = (
            ("relationship-draw-payoff",) if resolved_review.state == "confirmed" else ()
        )
    return SemanticEnrichmentArtifact(
        set_code=set_code,
        set_source_id=set_source_id,
        set_source_sha256=set_source_sha256(sources),
        created_at=ENRICHMENT_CREATED_AT,
        cards=tuple(
            CardSourcePin(
                card_id=card.grp_id,
                oracle_id=card.oracle_id,
                collector_number=card.collector_number,
                sha256=card_source_sha256(card),
            )
            for card in sources.cards
        ),
        guides=tuple(
            GuideSourcePin(
                guide_id=guide.guide_id,
                url=guide.url,
                sha256=guide.text_sha256,
                retrieved_at=guide.retrieved_at,
            )
            for guide in sources.guides
        ),
        runs=(_enrichment_run() if run is None else run,),
        oracle_facts=(),
        guide_claims=(_enrichment_mechanic(),) if guide_claims is None else guide_claims,
        relationships=(_enrichment_relationship(),)
        if relationships is None
        else relationships,
        rejected_findings=(),
        review=resolved_review,
        confirmed_relationship_ids=confirmed_relationship_ids,
        sources=sources,
    )


@pytest.mark.parametrize(
    ("source_change", "expected_error"),
    [
        ("missing", "Could not verify the selected local draft source."),
        ("mismatched", "The selected draft source does not match its SHA-256 pin."),
    ],
)
def test_metadata_source_is_verified_before_generation(
    source_change: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "no-data.csv")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    source = payload["sources"][0]
    if source_change == "missing":
        source["path"] = str(tmp_path / "missing-source.csv")
    else:
        source["sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    generation_called = False

    def fail_if_called(**_: object) -> object:
        nonlocal generation_called
        generation_called = True
        raise AssertionError("generation must not run for an unverifiable source")

    monkeypatch.setattr(publication, "generate_set_profile", fail_if_called)
    with pytest.raises(
        publication.ProfilePublicationError,
        match=expected_error,
    ) as raised:
        _publish(tmp_path, manifest=manifest)
    assert "no-data.csv" not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    assert not generation_called



def test_early_source_checksum_failure_is_actionable_and_private(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "no-data.csv")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["sources"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        publication.ProfilePublicationError,
        match="The selected draft source does not match its SHA-256 pin.",
    ) as raised:
        _publish(tmp_path, stage="early", ratings=True, manifest=manifest)
    assert "no-data.csv" not in str(raised.value)
    assert str(FIXTURE_DIR) not in str(raised.value)
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize(
    ("loader_name", "with_ratings", "with_manifest", "expected_error"),
    [
        (
            "load_card_database",
            False,
            False,
            "Could not load the card database input.",
        ),
        ("load_17lands_format_data", True, False, "Could not load the ratings input."),
        ("load_public_dump_manifest", False, True, "Could not load the source manifest."),
    ],
)
def test_input_loader_recursion_errors_are_wrapped(
    loader_name: str,
    with_ratings: bool,
    with_manifest: bool,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path, "no-data.csv") if with_manifest else None

    def recurse(*_: object, **__: object) -> object:
        raise RecursionError("private loader details")

    monkeypatch.setattr(publication, loader_name, recurse)
    with pytest.raises(publication.ProfilePublicationError, match=expected_error):
        _publish(
            tmp_path,
            ratings=with_ratings,
            manifest=manifest,
        )
    assert not (tmp_path / "published").exists()


def test_artifact_helper_precedes_marker_and_failed_artifact_preserves_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _publish(tmp_path)
    first_marker = first.manifest_path.read_bytes()
    calls: list[str] = []
    publish_artifact = publication._reuse_or_publish_artifact
    publish_marker = publication._publish_generation_marker

    def record_artifact(**kwargs: object) -> None:
        calls.append("artifact")
        publish_artifact(**kwargs)

    def record_marker(**kwargs: object) -> None:
        calls.append("marker")
        publish_marker(**kwargs)

    monkeypatch.setattr(publication, "_reuse_or_publish_artifact", record_artifact)
    monkeypatch.setattr(publication, "_publish_generation_marker", record_marker)
    second = _publish(tmp_path, generated_at=GENERATED_AT + timedelta(seconds=1))
    assert calls == ["artifact", "marker"]
    assert second.manifest_path.read_bytes() != first_marker

    marker_before_failure = second.manifest_path.read_bytes()
    calls.clear()

    def fail_artifact(**_: object) -> None:
        calls.append("artifact")
        raise publication.ProfilePublicationError("artifact write failed")

    monkeypatch.setattr(publication, "_reuse_or_publish_artifact", fail_artifact)
    with pytest.raises(publication.ProfilePublicationError, match="artifact write failed"):
        _publish(tmp_path, generated_at=GENERATED_AT + timedelta(seconds=2))
    assert calls == ["artifact"]
    assert second.manifest_path.read_bytes() == marker_before_failure


class _CorruptGzipResult(publication.ProfileGenerationResult):
    @property
    def gzip_bytes(self) -> bytes:
        return b"not a gzip stream"


class _NoncanonicalProfileResult(publication.ProfileGenerationResult):
    @property
    def profile_bytes(self) -> bytes:
        value = json.loads(super().profile_bytes)
        return (json.dumps(value) + "\n").encode("utf-8")


def _valid_generation() -> publication.ProfileGenerationResult:
    return publication.generate_set_profile(
        set_code="TST",
        event_format="QuickDraft",
        stage="metadata",
        generated_at=GENERATED_AT,
        card_database=_database(),
        config=_config(),
    )

def test_validate_profile_generation_returns_immutable_canonical_payload() -> None:
    generation = _valid_generation()

    validated = publication.validate_profile_generation(
        generation=generation,
        set_code="tst",
        event_format="quickdraft",
        stage="metadata",
    )

    assert isinstance(validated, publication.ValidatedProfileGeneration)
    assert validated.profile_bytes == generation.profile_bytes
    assert validated.gzip_bytes == generation.gzip_bytes
    assert validated.report_bytes == generation.report.to_bytes()
    with pytest.raises(FrozenInstanceError):
        validated.profile_bytes = b""

def test_validate_profile_generation_rejects_report_schema_mismatch() -> None:
    generation = _valid_generation()
    malformed = replace(
        generation,
        report=replace(
            generation.report,
            set_profile_schema_version=generation.profile.schema_version + 1,
        ),
    )

    with pytest.raises(
        publication.ProfilePublicationError,
        match="Generation report schema does not match the profile",
    ):
        publication.validate_profile_generation(
            generation=malformed,
            set_code="tst",
            event_format="quickdraft",
            stage="metadata",
        )




@pytest.mark.parametrize(
    ("malformation", "expected_error"),
    [
        ("corrupt-gzip", "Generated profile gzip could not be validated."),
        ("noncanonical", "Generated profile bytes are not canonical."),
        ("report-checksum", "Generation report checksums or sizes do not reconcile."),
        ("report-size", "Generation report checksums or sizes do not reconcile."),
    ],
)
def test_malformed_generation_result_is_rejected_before_publication(
    malformation: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _valid_generation()
    if malformation == "corrupt-gzip":
        malformed: publication.ProfileGenerationResult = _CorruptGzipResult(
            profile=generation.profile,
            report=generation.report,
        )
    elif malformation == "noncanonical":
        malformed = _NoncanonicalProfileResult(
            profile=generation.profile,
            report=generation.report,
        )
    elif malformation == "report-checksum":
        malformed = replace(
            generation,
            report=replace(generation.report, gzip_sha256="0" * 64),
        )
    else:
        malformed = replace(
            generation,
            report=replace(generation.report, gzip_bytes=generation.report.gzip_bytes + 1),
        )
    monkeypatch.setattr(publication, "generate_set_profile", lambda **_: malformed)

    with pytest.raises(publication.ProfilePublicationError, match=expected_error):
        _publish(tmp_path)
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize(
    ("stage", "generator_error", "expected_error"),
    [
        (
            "early",
            "early profiles must contain empirical evidence.",
            "Early profile generation requires empirical ratings or accepted draft evidence.",
        ),
        (
            "mature",
            "Mature profile generation requires accepted deck evidence.",
            "Mature profile generation requires accepted draft-deck evidence and Stage C targets for every accepted color pair.",
        ),
        (
            "mature",
            "Mature profile generation requires Stage C targets for every accepted color pair.",
            "Mature profile generation requires accepted draft-deck evidence and Stage C targets for every accepted color pair.",
        ),
    ],
)
def test_stage_evidence_failures_have_stable_actionable_categories(
    stage: str,
    generator_error: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_generation(**_: object) -> object:
        raise publication.ProfileGenerationError(generator_error)

    monkeypatch.setattr(publication, "generate_set_profile", fail_generation)
    with pytest.raises(publication.ProfilePublicationError, match=expected_error):
        _publish(tmp_path, stage=stage)
    assert not (tmp_path / "published").exists()


def test_metadata_publication_writes_canonical_profile_and_report(tmp_path: Path) -> None:
    result = _publish(tmp_path)

    assert result.artifact_path == (
        tmp_path / "published" / "tst-quickdraft" / "artifacts" / f"{result.generation.report.gzip_sha256}.json.gz"
    )
    assert result.manifest_path.read_bytes() == result.generation.report.to_bytes()
    profile_bytes = gzip.decompress(result.artifact_path.read_bytes())
    assert SetProfile.from_json(json.loads(profile_bytes)) == result.generation.profile
    assert result.sample_count == 0
    assert result.validation_outcome == "passed"
    assert result.input_count == 1


def test_early_publication_is_deterministic_when_repeated(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "no-data.csv")
    first = _publish(tmp_path, stage="early", ratings=True, manifest=manifest)
    second = _publish(tmp_path, stage="early", ratings=True, manifest=manifest)

    assert second.artifact_path == first.artifact_path
    assert second.artifact_path.read_bytes() == first.artifact_path.read_bytes()
    assert second.manifest_path.read_bytes() == first.manifest_path.read_bytes()
    assert tuple((tmp_path / "published" / "tst-quickdraft" / "artifacts").iterdir()) == (
        first.artifact_path,
    )
    assert second.input_count == 3


def test_mature_publication_accepts_the_existing_generation_contract(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "mature-data.csv")
    result = _publish(tmp_path, stage="mature", ratings=True, manifest=manifest)

    assert result.generation.profile.maturity.value == "mature"
    assert result.sample_count == result.generation.report.samples.total
    assert result.skip_count == sum(result.generation.report.skip_reasons.values())
    assert result.error_count == sum(result.generation.report.error_reasons.values())


def test_manifest_selection_resolves_relative_paths_without_serializing_them(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "early-data.csv")
    result = _publish(tmp_path, stage="early", ratings=True, manifest=manifest)

    report_text = result.manifest_path.read_text(encoding="utf-8")
    assert "early-data.csv" in report_text
    assert '"path"' not in report_text
    assert str(FIXTURE_DIR) not in report_text

    ambiguous = _manifest(tmp_path, "early-data.csv", "no-data.csv")
    with pytest.raises(publication.ProfilePublicationError, match="multiple sources"):
        _publish(tmp_path, stage="early", ratings=True, manifest=ambiguous)


def test_url_only_source_is_rejected_before_generation(tmp_path: Path) -> None:
    card_database_path, _ = _write_inputs(tmp_path)
    manifest_path = tmp_path / "remote.json"
    manifest_path.write_bytes(
        PublicDumpManifest(
            sources=(
                PublicDumpSource(
                    name="remote",
                    url="https://example.test/draft.csv.gz",
                    sha256="a" * 64,
                ),
            )
        ).to_bytes()
    )

    with pytest.raises(publication.ProfilePublicationError, match="local path"):
        publication.generate_local_profile_artifacts(
            set_code="TST",
            event_format="QuickDraft",
            stage="metadata",
            generated_at=GENERATED_AT,
            card_database_path=card_database_path,
            output_dir=tmp_path / "published",
            source_manifest_path=manifest_path,
        )
    assert not (tmp_path / "published").exists()


def test_invalid_generated_bytes_are_rejected_before_output_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_database_path, _ = _write_inputs(tmp_path)
    monkeypatch.setattr(publication, "generate_set_profile", lambda **_: object())

    with pytest.raises(publication.ProfilePublicationError, match="invalid result"):
        publication.generate_local_profile_artifacts(
            set_code="TST",
            event_format="QuickDraft",
            stage="metadata",
            generated_at=GENERATED_AT,
            card_database_path=card_database_path,
            output_dir=tmp_path / "published",
        )
    assert not (tmp_path / "published").exists()


def test_existing_content_collision_does_not_replace_the_marker(tmp_path: Path) -> None:
    first = _publish(tmp_path)
    first_marker = first.manifest_path.read_bytes()
    first.artifact_path.write_bytes(b"wrong content")

    with pytest.raises(publication.ProfilePublicationError, match="different bytes"):
        _publish(tmp_path)
    assert first.manifest_path.read_bytes() == first_marker


def test_marker_failure_preserves_the_last_authoritative_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _publish(tmp_path)
    first_marker = first.manifest_path.read_bytes()

    def fail_marker(**_: object) -> None:
        raise publication.ProfilePublicationError("marker write failed")

    monkeypatch.setattr(publication, "_publish_generation_marker", fail_marker)
    with pytest.raises(publication.ProfilePublicationError, match="marker write failed"):
        _publish(tmp_path, generated_at=GENERATED_AT + timedelta(seconds=1))
    assert first.manifest_path.read_bytes() == first_marker



def test_manifest_conversion_matches_the_validated_generation_report(
    tmp_path: Path,
) -> None:
    result = _publish(tmp_path)
    artifact = publication.profile_manifest_artifact_from_publication(
        result,
        "https://cdn.example.test/tst-quickdraft.json.gz",
    )
    report = result.generation.report

    assert isinstance(artifact, ProfileManifestArtifact)
    assert artifact.set_code == report.set_code
    assert artifact.event_format == report.event_format
    assert artifact.set_profile_schema_version == report.set_profile_schema_version
    assert artifact.profile_version == result.generation.profile.profile_version
    assert artifact.generated_at == report.generated_at
    assert artifact.gzip_bytes == report.gzip_bytes
    assert artifact.profile_bytes == report.profile_bytes
    assert artifact.gzip_sha256 == report.gzip_sha256
    assert artifact.profile_sha256 == report.profile_sha256
    assert artifact.maturity == result.generation.profile.maturity

    generation_marker = result.manifest_path.read_bytes()
    manifest = publication.build_profile_manifest((artifact,), published_at=GENERATED_AT)
    manifest_path = publication.publish_profile_manifest(
        tmp_path / "published" / "profile-manifest.json",
        manifest,
    )
    assert load_profile_manifest(manifest_path) == manifest
    assert result.manifest_path.read_bytes() == generation_marker


def test_manifest_conversion_rejects_report_reconciliation_failure(
    tmp_path: Path,
) -> None:
    result = _publish(tmp_path)
    malformed_generation = replace(
        result.generation,
        report=replace(result.generation.report, profile_bytes=result.generation.report.profile_bytes + 1),
    )
    malformed_result = replace(result, generation=malformed_generation)

    with pytest.raises(
        publication.ProfilePublicationError,
        match="checksums or sizes do not reconcile",
    ):
        publication.profile_manifest_artifact_from_publication(
            malformed_result,
            "https://cdn.example.test/tst-quickdraft.json.gz",
        )


def test_manifest_publication_failure_preserves_previous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _publish(tmp_path)
    artifact = publication.profile_manifest_artifact_from_publication(
        result,
        "https://cdn.example.test/tst-quickdraft.json.gz",
    )
    manifest = publication.build_profile_manifest((artifact,), published_at=GENERATED_AT)
    path = tmp_path / "profile-manifest.json"
    publication.publish_profile_manifest(path, manifest)
    previous = path.read_bytes()

    def fail_atomic_write(**_: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(publication, "_atomic_write", fail_atomic_write)
    with pytest.raises(publication.ProfilePublicationError, match="Could not publish"):
        publication.publish_profile_manifest(path, manifest)
    assert path.read_bytes() == previous


def test_enhanced_publication_round_trips_and_validates_artifact_and_report(
    tmp_path: Path,
) -> None:
    card_database_path, _ = _write_inputs(tmp_path)
    artifact = _enrichment_artifact(load_card_database(cache_path=card_database_path))
    result = _publish(tmp_path, enrichment=artifact)

    validated = publication.validate_profile_generation(
        generation=result.generation,
        set_code="tst",
        event_format="quickdraft",
        stage="metadata",
    )
    report = result.generation.report
    assert result.artifact_path == (
        tmp_path
        / "published"
        / "tst-quickdraft"
        / "artifacts"
        / f"{report.gzip_sha256}.json.gz"
    )
    compressed = result.artifact_path.read_bytes()
    assert compressed == validated.gzip_bytes
    assert result.manifest_path.read_bytes() == validated.report_bytes
    assert result.manifest_path.read_bytes() == report.to_bytes()
    assert gzip.decompress(compressed) == result.generation.profile_bytes
    profile = SetProfile.from_json(json.loads(gzip.decompress(compressed)))
    assert profile == result.generation.profile
    assert profile.schema_version == 3
    assert profile.enhancement_status is EnhancementStatus.ENHANCED

    marker = json.loads(result.manifest_path.read_bytes())
    assert marker["set_profile_schema_version"] == 3
    assert marker["checksums"]["gzip"] == report.gzip_sha256
    assert marker["checksums"]["profile"] == report.profile_sha256
    assert marker["gzip_bytes"] == len(compressed)
    assert marker["profile_bytes"] == len(result.generation.profile_bytes)
    assert marker["enhancement"]["artifact_sha256"] == hashlib.sha256(
        artifact.to_bytes()
    ).hexdigest()

    tampered = replace(
        result.generation,
        report=replace(result.generation.report, enhancement=None),
    )
    with pytest.raises(
        publication.ProfilePublicationError,
        match="Generation report enhancement provenance does not match the profile.",
    ):
        publication.validate_profile_generation(
            generation=tampered,
            set_code="tst",
            event_format="quickdraft",
            stage="metadata",
        )


def test_enhanced_publication_retains_typed_relationship_prerequisites(
    tmp_path: Path,
) -> None:
    database = _typed_database()
    card_database_path, _ = _write_inputs(tmp_path, card_database=database)
    artifact = _typed_enrichment_artifact(
        sources=_enrichment_sources(load_card_database(cache_path=card_database_path)),
    )
    result = _publish(tmp_path, enrichment=artifact, card_database=database)

    profile = SetProfile.from_json(json.loads(gzip.decompress(result.artifact_path.read_bytes())))
    assert profile.schema_version == 3
    assert profile.enhancement_status is EnhancementStatus.ENHANCED
    enhancement = profile.enhancement
    assert enhancement is not None
    relationship = enhancement.relationships[0]
    projection = relationship.prerequisite_projection
    assert projection is not None
    assert relationship.participants == (TYPED_TARGET_CARD_ID, TYPED_SOURCE_CARD_ID)
    assert relationship.identity[2] == (
        TYPED_SOURCE_CARD_ID,
        "capability-typed-tokens",
        -1,
        TYPED_TARGET_CARD_ID,
        "capability-typed-anthem",
        -1,
    )
    assert projection.source.card_id == TYPED_SOURCE_CARD_ID
    assert projection.target.card_id == TYPED_TARGET_CARD_ID
    assert projection.source.prerequisites[0].colors == ("W",)
    assert projection.source.prerequisites[0].evidence.quote == (
        "Create two 1/1 white Soldier creature tokens."
    )
    marker = json.loads(result.manifest_path.read_bytes())
    assert marker["enhancement"]["artifact_sha256"] == hashlib.sha256(artifact.to_bytes()).hexdigest()


def _stale_card_artifact(loaded: CardDatabase) -> SemanticEnrichmentArtifact:
    # The stale database must still satisfy the artifact's own Oracle-evidence
    # substring rule, so card 2 keeps its quoted text and gains an extra line.
    stale_cards = CardDatabase(
        cards={
            **loaded.cards,
            2: replace(
                loaded.cards[2],
                oracle_text="Destroy target creature. Exile it instead.",
            ),
        }
    )
    return _enrichment_artifact(loaded, cards=stale_cards)


@pytest.mark.parametrize(
    ("build", "expected_error"),
    [
        (
            lambda loaded: _stale_card_artifact(loaded),
            "The enrichment artifact card data does not match the generation card database.",
        ),
        (
            lambda loaded: _enrichment_artifact(
                loaded,
                review=ArtifactReview(state="pending", reviewer_id=None, reviewed_at=None),
            ),
            "The enrichment artifact has not been confirmed.",
        ),
        (
            lambda loaded: _enrichment_artifact(
                loaded,
                review=ArtifactReview(
                    state="cancelled",
                    reviewer_id="local-review",
                    reviewed_at=ENRICHMENT_REVIEWED_AT,
                ),
            ),
            "The enrichment artifact review was cancelled.",
        ),
        (
            lambda loaded: _enrichment_artifact(
                loaded, cards=_eld_cards(loaded), set_code="ELD"
            ),
            "The enrichment artifact set code does not match the generated set.",
        ),
        (
            lambda loaded: _enrichment_artifact(loaded, set_source_id="tst/card-data"),
            "The enrichment artifact card data identity cannot be recorded in a profile.",
        ),
        (
            lambda loaded: _enrichment_artifact(
                loaded,
                guide_claims=(_enrichment_strategy(),),
                relationships=(),
                confirmed_relationship_ids=(),
            ),
            "The enrichment artifact contains no confirmed relationship or accepted mechanic finding.",
        ),
        (
            lambda loaded: _enrichment_artifact(
                loaded,
                run=_enrichment_run(model="/Users/alice/models/private-model.gguf"),
            ),
            "The enrichment artifact carries a local filesystem path where a published identity is required.",
        ),
    ],
    ids=[
        "stale-card-data",
        "pending-review",
        "cancelled-review",
        "set-mismatch",
        "card-data-identity",
        "no-findings",
        "local-path-identity",
    ],
)
def test_rejected_enrichment_writes_no_profile_bytes(
    tmp_path: Path,
    build: Callable[[CardDatabase], SemanticEnrichmentArtifact],
    expected_error: str,
) -> None:
    card_database_path, _ = _write_inputs(tmp_path)
    loaded = load_card_database(cache_path=card_database_path)

    with pytest.raises(publication.ProfilePublicationError) as raised:
        _publish(tmp_path, enrichment=build(loaded))

    assert str(raised.value) == expected_error
    assert not (tmp_path / "published").exists()


def test_enhanced_publication_leaves_prior_unenhanced_artifacts_untouched(
    tmp_path: Path,
) -> None:
    first = _publish(tmp_path)
    first_artifact_bytes = first.artifact_path.read_bytes()
    first_manifest_bytes = first.manifest_path.read_bytes()
    artifact = _enrichment_artifact(load_card_database(cache_path=tmp_path / "cards.json"))

    second = _publish(tmp_path, enrichment=artifact)

    assert first.artifact_path.exists()
    assert first.artifact_path.read_bytes() == first_artifact_bytes
    assert first.artifact_path.stat().st_size == len(first_artifact_bytes)
    assert second.artifact_path != first.artifact_path
    assert second.artifact_path.read_bytes() != first_artifact_bytes
    assert second.manifest_path.read_bytes() != first_manifest_bytes
    marker = json.loads(second.manifest_path.read_bytes())
    assert marker["set_profile_schema_version"] == 3
    assert marker["checksums"]["gzip"] == second.generation.report.gzip_sha256


def test_cli_boundary_smoke_publishes_the_enhanced_layout(tmp_path: Path) -> None:
    card_database_path, _ = _write_inputs(tmp_path)
    artifact = _enrichment_artifact(load_card_database(cache_path=card_database_path))
    result = _publish(tmp_path, stage="metadata", enrichment=artifact)

    output_dir = tmp_path / "published"
    report = result.generation.report
    assert report.enhancement is not None
    assert result.validation_outcome == "passed"
    assert result.input_count == 1
    assert result.generation.profile.maturity.value == "metadata-only"
    assert result.artifact_path == (
        output_dir / "tst-quickdraft" / "artifacts" / f"{report.gzip_sha256}.json.gz"
    )
    assert result.manifest_path == output_dir / "tst-quickdraft" / "generation.json"
    assert result.artifact_path.is_file()
    assert result.manifest_path.is_file()
    assert result.sample_count == report.samples.total
    assert result.skip_count == sum(report.skip_reasons.values())
    assert result.error_count == sum(report.error_reasons.values())

    marker = json.loads(result.manifest_path.read_bytes())
    assert marker["stage"] == "metadata"
    assert marker["set_code"] == "tst"
    assert marker["event_format"] == "quickdraft"
    assert marker["set_profile_schema_version"] == 3
    assert marker["checksums"] == {
        "gzip": report.gzip_sha256,
        "inputs": dict(report.input_checksums),
        "profile": report.profile_sha256,
    }
    assert marker["enhancement"] == report.enhancement.to_json()
