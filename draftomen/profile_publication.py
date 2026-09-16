"""Validated local publication for deterministic set-profile artifacts.

This module is deliberately a local-only boundary.  It loads only caller-selected
cache files, validates the generated profile and report bytes before touching the
publication directory, and commits the immutable content object before replacing
the authoritative generation marker.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, TypeAlias
import zlib

from draftomen.carddb import CardDatabase, CardDatabaseError, load_card_database
from draftomen.enrichment_publications import (
    EnrichmentPublication,
    EnrichmentPublicationError,
    load_enrichment_publications,
    record_enrichment_publication,
)
from draftomen.guide_client import GuideClientError, _validate_url as _validate_guide_url
from draftomen.profile_generation import (
    DEFAULT_PROFILE_GENERATION_CONFIG,
    ProfileGenerationConfig,
    ProfileGenerationError,
    ProfileGenerationReport,
    ProfileGenerationResult,
    ProfileGenerationStage,
    ProfileEnhancementProvenance,
    generate_set_profile,
    _requested_card_database,
)
from draftomen.profile_enhancement import ProfileEnhancementError
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestArtifact,
    ProfileManifestError,
    load_profile_manifest,
    _timestamp as _manifest_timestamp,
)
from draftomen.public_dump import (
    PublicDumpChecksumError,
    PublicDumpError,
    PublicDumpManifest,
    PublicDumpSource,
    load_public_dump_manifest,
)
from draftomen.semantic_enrichment import (
    EnrichmentSources,
    GuideSource,
    SemanticEnrichmentArtifact,
)
from draftomen.semantic_enrichment_records import SemanticEnrichmentError
from draftomen.seventeen import SeventeenLandsError, load_17lands_format_data
from draftomen.set_profile import (
    EnhancementStatus,
    ProfileMaturity,
    SetProfile,
    SetProfileError,
)


PathInput: TypeAlias = str | os.PathLike[str]

PROFILE_BASE_URL = "https://www.draftomen.com/profiles/objects/"


class ProfilePublicationError(RuntimeError):
    """Raised when local profile generation or publication cannot be completed."""


@dataclass(frozen=True, slots=True)
class ValidatedProfileGeneration:
    """Canonical bytes that passed profile publication validation."""

    profile_bytes: bytes
    gzip_bytes: bytes
    report_bytes: bytes

    def __post_init__(self) -> None:
        for field_name in ("profile_bytes", "gzip_bytes", "report_bytes"):
            if not isinstance(getattr(self, field_name), bytes):
                raise TypeError(f"{field_name} must be bytes.")


_SOURCE_CHECKSUM_ERROR = "The selected draft source does not match its SHA-256 pin."
_SOURCE_VERIFY_ERROR = "Could not verify the selected local draft source."
_EARLY_EVIDENCE_ERROR = (
    "Early profile generation requires empirical ratings or accepted draft evidence."
)
_MATURE_EVIDENCE_ERROR = (
    "Mature profile generation requires accepted draft-deck evidence and Stage C "
    "targets for every accepted color pair."
)
_GENERATION_FALLBACK_ERROR = "Profile generation or validation failed before publication."
_FROZEN_GUIDE_ERROR = "The frozen guide record is inconsistent."
_ENRICHMENT_INPUT_ERROR = "Could not load the enrichment input."
_ENRICHMENT_ARTIFACT_NAME = re.compile(r"[0-9a-f]{64}\.json")
_ENRICHMENT_RETAINED_OBJECT_ERROR = "Could not read the retained profile object."
_ENRICHMENT_REPLACEMENT_ERROR = "Could not read the generated profile payload."

_KNOWN_GENERATION_VALIDATION_ERRORS = frozenset(
    {
        "Profile generation returned an invalid result.",
        "Generated profile gzip could not be validated.",
        "Generated profile JSON must be an object.",
        "Generated profile failed schema validation.",
        "Generated profile bytes are not canonical.",
        "Generated profile does not match the requested set and format.",
        "Generation report does not match the requested profile.",
        "Generation report enhancement provenance does not match the profile.",
        "Generation report schema does not match the profile.",
        "Generation report checksums or sizes do not reconcile.",
        "Generation report could not be serialized and parsed.",
        "Generation report bytes are not canonical.",
    }
)


@dataclass(frozen=True, slots=True)
class ProfilePublicationResult:
    """One successfully validated local profile publication."""

    generation: ProfileGenerationResult
    artifact_path: Path
    manifest_path: Path
    input_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.generation, ProfileGenerationResult):
            raise TypeError("generation must be a ProfileGenerationResult.")
        if not isinstance(self.artifact_path, Path):
            object.__setattr__(self, "artifact_path", Path(self.artifact_path))
        if not isinstance(self.manifest_path, Path):
            object.__setattr__(self, "manifest_path", Path(self.manifest_path))
        if (
            isinstance(self.input_count, bool)
            or not isinstance(self.input_count, int)
            or self.input_count < 0
        ):
            raise ValueError("input_count must be a non-negative integer.")

    @property
    def sample_count(self) -> int:
        """Return the generated report's aggregate sample count."""

        return self.generation.report.samples.total

    @property
    def skip_count(self) -> int:
        """Return the generated report's aggregate skipped-row count."""

        return sum(self.generation.report.skip_reasons.values())

    @property
    def error_count(self) -> int:
        """Return the generated report's aggregate error count."""

        return sum(self.generation.report.error_reasons.values())

    @property
    def validation_outcome(self) -> str:
        """Return the outcome for a result that passed publication validation."""

        return "passed"


def profile_manifest_artifact_from_publication(
    result: ProfilePublicationResult,
    artifact_url: str,
) -> ProfileManifestArtifact:
    """Convert one validated local publication into a remote manifest artifact."""

    if not isinstance(result, ProfilePublicationResult):
        raise ProfilePublicationError("result must be a ProfilePublicationResult.")
    generation = result.generation
    try:
        profile = generation.profile
        report = generation.report
        if not isinstance(report, ProfileGenerationReport):
            raise ProfilePublicationError("publication result contains an invalid generation report.")
        validated = validate_profile_generation(
            generation=generation,
            set_code=profile.set_code,
            event_format=profile.event_format,
            stage=report.stage,
        )
        expected_maturity = {
            ProfileGenerationStage.METADATA.value: ProfileMaturity.METADATA_ONLY,
            ProfileGenerationStage.EARLY.value: ProfileMaturity.EARLY,
            ProfileGenerationStage.MATURE.value: ProfileMaturity.MATURE,
        }.get(report.stage)
        if expected_maturity is None or profile.maturity is not expected_maturity:
            raise ProfilePublicationError(
                "Generation report stage and profile maturity do not reconcile."
            )
        if report.generated_at != profile.generated_at:
            raise ProfilePublicationError(
                "Generation report timestamp and profile timestamp do not reconcile."
            )
        return ProfileManifestArtifact(
            set_code=report.set_code,
            event_format=report.event_format,
            set_profile_schema_version=report.set_profile_schema_version,
            profile_version=profile.profile_version,
            generated_at=report.generated_at,
            url=artifact_url,
            gzip_bytes=report.gzip_bytes,
            profile_bytes=report.profile_bytes,
            gzip_sha256=report.gzip_sha256,
            profile_sha256=report.profile_sha256,
            maturity=profile.maturity,
        )
    except ProfilePublicationError:
        raise
    except (
        AttributeError,
        ProfileManifestError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
    ) as error:
        raise ProfilePublicationError(
            "Could not convert the profile publication to a manifest artifact."
        ) from error


def build_profile_manifest(
    artifacts: Iterable[ProfileManifestArtifact],
    *,
    published_at: str | datetime,
) -> ProfileManifest:
    """Build a deterministic aggregate manifest from validated artifacts."""

    try:
        timestamp = _canonical_published_at(published_at)
        return ProfileManifest(artifacts=tuple(artifacts), published_at=timestamp)
    except ProfilePublicationError:
        raise
    except (ProfileManifestError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ProfilePublicationError("Could not build the profile manifest.") from error


def publish_profile_manifest(path: PathInput, manifest: ProfileManifest) -> Path:
    """Atomically publish one canonical aggregate profile manifest."""

    if not isinstance(manifest, ProfileManifest):
        raise ProfilePublicationError("manifest must be a ProfileManifest.")
    output = _path(value=path, field_name="profile_manifest_path")
    try:
        payload = manifest.to_bytes()
        _atomic_write(path=output, payload=payload)
    except ProfilePublicationError:
        raise
    except (ProfileManifestError, OSError, TypeError, ValueError, UnicodeError) as error:
        raise ProfilePublicationError("Could not publish the profile manifest.") from error
    return output


def publish_profile_object(path: PathInput, payload: bytes) -> Path:
    """Atomically publish one immutable content-addressed profile object.

    Identical bytes are reused without rewriting and a conflicting object is
    rejected without replacement.
    """

    if not isinstance(payload, bytes):
        raise ProfilePublicationError("payload must be bytes.")
    output = _path(value=path, field_name="profile_object_path")
    try:
        _reuse_or_publish_artifact(path=output, payload=payload)
    except ProfilePublicationError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError) as error:
        raise ProfilePublicationError("Could not publish the profile object.") from error
    return output


def merge_profile_manifest_artifacts(
    manifest: ProfileManifest,
    artifacts: Iterable[ProfileManifestArtifact],
    *,
    published_at: str | datetime,
) -> ProfileManifest:
    """Merge replacement artifacts into a manifest by set and format identity.

    Every unrelated entry is retained.  The original manifest is returned
    unchanged when each supplied artifact already matches its entry.
    """

    if not isinstance(manifest, ProfileManifest):
        raise ProfilePublicationError("manifest must be a ProfileManifest.")
    if not isinstance(published_at, (str, datetime)):
        raise ProfilePublicationError("published_at must be a string or datetime.")
    timestamp = _canonical_published_at(published_at)
    try:
        supplied = tuple(artifacts)
    except TypeError as error:
        raise ProfilePublicationError(
            "artifacts must be an iterable of profile manifest artifacts."
        ) from error
    existing = {_artifact_identity(artifact): artifact for artifact in manifest.artifacts}
    replacements: dict[tuple[str, str], ProfileManifestArtifact] = {}
    for artifact in supplied:
        if not isinstance(artifact, ProfileManifestArtifact):
            raise ProfilePublicationError(
                "artifacts must contain only profile manifest artifacts."
            )
        identity = _artifact_identity(artifact)
        if identity in replacements:
            raise ProfilePublicationError("replacement artifacts contain a duplicate identity.")
        replacements[identity] = artifact
    if all(existing.get(identity) == artifact for identity, artifact in replacements.items()):
        return manifest
    merged = {**existing, **replacements}
    return build_profile_manifest(tuple(merged.values()), published_at=timestamp)


@dataclass(frozen=True, slots=True)
class PublishedProfilePublication:
    """One validated local publication installed in the repository profiles tree."""

    artifact: ProfileManifestArtifact
    object_path: Path
    manifest_path: Path
    manifest_changed: bool
    publications_path: Path | None


def publish_profile_publication(
    *,
    publication: ProfilePublicationResult,
    profiles_dir: PathInput,
    published_at: str | datetime,
    run_id: str | None = None,
) -> PublishedProfilePublication:
    """Install one validated local publication and record its enrichment provenance.

    The manifest is loaded before any repository write, the immutable object is
    installed before the provenance record, and the record is written before the
    manifest entry that names it, so a manifest consumer never observes an
    enriched entry whose provenance is missing while a record naming a
    not-yet-published identity stays harmless.  An identical republication
    rewrites neither the object nor the manifest, and a manifest failure leaves
    the previous manifest authoritative.  Every failure, including an unusable
    provenance record, is reported as :class:`ProfilePublicationError` so
    callers keep one publication error taxonomy.
    """

    if not isinstance(publication, ProfilePublicationResult):
        raise ProfilePublicationError("publication must be a ProfilePublicationResult.")
    profiles = _path(value=profiles_dir, field_name="profiles_dir")
    timestamp = _canonical_published_at(published_at)
    report = publication.generation.report
    artifact = profile_manifest_artifact_from_publication(
        publication,
        f"{PROFILE_BASE_URL}{report.gzip_sha256}.json.gz",
    )
    existing_manifest = load_profile_manifest(profiles / "manifest.json")
    payload = publication.artifact_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != report.gzip_sha256:
        raise ProfilePublicationError(
            "The published profile object does not match its validated gzip digest."
        )
    merged = merge_profile_manifest_artifacts(
        existing_manifest,
        (artifact,),
        published_at=timestamp,
    )
    manifest_changed = merged is not existing_manifest
    object_path = publish_profile_object(
        path=profiles / "objects" / f"{report.gzip_sha256}.json.gz",
        payload=payload,
    )
    publications_path: Path | None = None
    if report.enhancement is not None:
        if not isinstance(run_id, str) or not run_id:
            raise ProfilePublicationError(
                "A published enrichment profile requires its run identity."
            )
        reviewed_at = report.enhancement.reviewed_at
        if not reviewed_at:
            raise ProfilePublicationError(
                "A published enrichment profile requires its review timestamp."
            )
        try:
            publications_path = record_enrichment_publication(
                profiles_dir=profiles,
                publication=EnrichmentPublication(
                    set_code=report.set_code,
                    event_format=report.event_format,
                    artifact_sha256=report.enhancement.artifact_sha256,
                    run_id=run_id,
                    reviewed_at=reviewed_at,
                    published_at=timestamp,
                    profile_gzip_sha256=report.gzip_sha256,
                ),
            )
        except EnrichmentPublicationError as cause:
            raise ProfilePublicationError(str(cause)) from cause
    manifest_path = (
        publish_profile_manifest(profiles / "manifest.json", merged)
        if manifest_changed
        else profiles / "manifest.json"
    )
    return PublishedProfilePublication(
        artifact=artifact,
        object_path=object_path,
        manifest_path=manifest_path,
        manifest_changed=manifest_changed,
        publications_path=publications_path,
    )


@dataclass(frozen=True, slots=True)
class EnrichmentDowngradeConflict:
    """One rejected attempt to replace an enriched profile with a plain one."""

    set_code: str
    event_format: str
    retained: ProfileManifestArtifact
    rejected: ProfileManifestArtifact

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "set_code", _normalize_component(value=self.set_code, field_name="set_code")
        )
        object.__setattr__(
            self,
            "event_format",
            _normalize_component(value=self.event_format, field_name="event_format"),
        )
        if not isinstance(self.retained, ProfileManifestArtifact):
            raise ProfilePublicationError(
                "conflict retained artifact must be a profile manifest artifact."
            )
        if not isinstance(self.rejected, ProfileManifestArtifact):
            raise ProfilePublicationError(
                "conflict rejected artifact must be a profile manifest artifact."
            )

    def to_json(self) -> dict[str, str]:
        """Return the canonical conflict record published in refresh reports."""

        return {
            "event_format": self.event_format,
            "rejected_gzip_sha256": self.rejected.gzip_sha256,
            "rejected_url": self.rejected.url,
            "retained_gzip_sha256": self.retained.gzip_sha256,
            "retained_url": self.retained.url,
            "set_code": self.set_code,
        }


def filter_enriched_profile_downgrades(
    *,
    manifest: ProfileManifest,
    profiles_dir: PathInput,
    replacements: Iterable[tuple[ProfileManifestArtifact, bytes]],
) -> tuple[tuple[ProfileManifestArtifact, ...], tuple[EnrichmentDowngradeConflict, ...]]:
    """Split generated replacements into accepted artifacts and enriched downgrade conflicts.

    Producers that can emit plain profiles MUST route their replacements through
    this filter before merging, so a published enriched entry is never replaced
    by a non-enriched artifact.  A durable publication record protects only the
    publication it names: the identity counts as enriched when the record
    selects it and that entry's ``profile_gzip_sha256`` is the retained
    artifact's digest, or, when no such entry exists, when the retained profile
    object declares confirmed enrichment.  A record naming a different digest is
    not a claim about the retained entry -- an unsuccessful or superseded
    publication can leave one behind -- so the retained-object read decides that
    case.  Because the record is consulted before the object, a matching entry
    protects an entry even once its object file was removed, and a corrupt
    retained object no longer masks a recorded publication.
    Enriched-to-enriched replacement, unpublished identities, identical
    artifacts, and non-enriched entries are unaffected.  A missing record file
    keeps the retained-object behaviour, while an unreadable or invalid record
    fails closed with ``READ_ERROR``.
    """

    if not isinstance(manifest, ProfileManifest):
        raise ProfilePublicationError("manifest must be a ProfileManifest.")
    directory = _path(value=profiles_dir, field_name="profiles_dir")
    try:
        published_enrichment = load_enrichment_publications(profiles_dir=directory)
    except EnrichmentPublicationError as cause:
        raise ProfilePublicationError(str(cause)) from cause
    try:
        supplied = tuple(replacements)
    except TypeError as error:
        raise ProfilePublicationError(
            "replacements must be an iterable of profile manifest artifacts and gzip bytes."
        ) from error
    accepted: list[ProfileManifestArtifact] = []
    conflicts: list[EnrichmentDowngradeConflict] = []
    for element in supplied:
        artifact, payload = _replacement_pair(element=element)
        retained = manifest.select(set_code=artifact.set_code, event_format=artifact.event_format)
        if retained is None or retained == artifact:
            accepted.append(artifact)
            continue
        replacement_fields = _decode_profile_object(
            payload=payload, error=_ENRICHMENT_REPLACEMENT_ERROR
        )
        if _profile_declares_confirmed_enrichment(value=replacement_fields):
            accepted.append(artifact)
            continue
        recorded = published_enrichment.select(
            set_code=artifact.set_code, event_format=artifact.event_format
        )
        if recorded is not None and recorded.profile_gzip_sha256 == retained.gzip_sha256:
            conflicts.append(
                EnrichmentDowngradeConflict(
                    set_code=artifact.set_code,
                    event_format=artifact.event_format,
                    retained=retained,
                    rejected=artifact,
                )
            )
            continue
        retained_fields = _published_profile_object(
            path=directory / "objects" / f"{retained.gzip_sha256}.json.gz"
        )
        if retained_fields is None or not _profile_declares_confirmed_enrichment(
            value=retained_fields
        ):
            accepted.append(artifact)
            continue
        conflicts.append(
            EnrichmentDowngradeConflict(
                set_code=artifact.set_code,
                event_format=artifact.event_format,
                retained=retained,
                rejected=artifact,
            )
        )
    return tuple(accepted), tuple(conflicts)


def _replacement_pair(*, element: object) -> tuple[ProfileManifestArtifact, bytes]:
    """Validate one generated replacement as an artifact and its gzip payload."""

    error = "replacements must contain only profile manifest artifacts and gzip bytes."
    try:
        artifact, payload = element  # type: ignore[misc]
    except (TypeError, ValueError) as cause:
        raise ProfilePublicationError(error) from cause
    if not isinstance(artifact, ProfileManifestArtifact) or not isinstance(payload, bytes):
        raise ProfilePublicationError(error)
    return artifact, payload


def _decode_profile_object(*, payload: bytes, error: str) -> Mapping[str, Any]:
    """Decode one canonical gzip profile object into its JSON object."""

    try:
        value = json.loads(gzip.decompress(payload).decode("utf-8"))
    except (EOFError, OSError, TypeError, UnicodeDecodeError, ValueError, zlib.error) as cause:
        raise ProfilePublicationError(error) from cause
    if not isinstance(value, Mapping):
        raise ProfilePublicationError(error)
    return value


def _published_profile_object(*, path: Path) -> Mapping[str, Any] | None:
    """Decode one published gzip profile object, or return None when it is absent."""

    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as cause:
        raise ProfilePublicationError(_ENRICHMENT_RETAINED_OBJECT_ERROR) from cause
    return _decode_profile_object(payload=payload, error=_ENRICHMENT_RETAINED_OBJECT_ERROR)


def _profile_declares_confirmed_enrichment(*, value: Mapping[str, Any]) -> bool:
    """Return True when decoded profile fields declare confirmed enhancement."""

    return (
        value.get("enhancement_status") == EnhancementStatus.ENHANCED.value
        and isinstance(value.get("enhancement"), Mapping)
    )


# Keep the public signature explicit: callers must opt into every input source.
def generate_local_profile_artifacts(
    *,
    set_code: str,
    event_format: str,
    stage: ProfileGenerationStage | str,
    generated_at: datetime,
    card_database_path: PathInput,
    output_dir: PathInput,
    ratings_path: PathInput | None = None,
    source_manifest_path: PathInput | None = None,
    draft_source_name: str | None = None,
    enrichment: SemanticEnrichmentArtifact | None = None,
    enrichment_path: PathInput | None = None,
    profile_version: str = "1.0",
    config: ProfileGenerationConfig = DEFAULT_PROFILE_GENERATION_CONFIG,
) -> ProfilePublicationResult:
    """Generate and atomically publish one local profile artifact.

    Inputs are loaded strictly from the paths supplied by the caller.  A
    confirmed enrichment artifact supplied as a path is revalidated against the
    requested set's frozen card data and guide.  The content-addressed gzip
    object is committed before ``generation.json``; replacing the latter is the
    sole authoritative commit operation.
    """

    if enrichment is not None and enrichment_path is not None:
        raise ProfilePublicationError("Supply either enrichment or enrichment_path, not both.")

    normalized_set = _normalize_component(value=set_code, field_name="set_code")
    normalized_format = _normalize_component(value=event_format, field_name="event_format")
    try:
        normalized_stage = ProfileGenerationStage.normalize(stage)
    except (ProfileGenerationError, TypeError, ValueError) as error:
        raise ProfilePublicationError("Invalid profile generation stage.") from error

    try:
        card_database = load_card_database(cache_path=card_database_path)
    except (
        CardDatabaseError,
        OSError,
        RecursionError,
        TypeError,
        ValueError,
        UnicodeError,
    ) as error:
        raise ProfilePublicationError("Could not load the card database input.") from error

    ratings = None
    if ratings_path is not None:
        try:
            ratings = load_17lands_format_data(
                set_code=normalized_set,
                event_format=normalized_format,
                cache_path=ratings_path,
            )
        except (
            SeventeenLandsError,
            OSError,
            RecursionError,
            TypeError,
            ValueError,
            UnicodeError,
        ) as error:
            raise ProfilePublicationError("Could not load the ratings input.") from error

    resolved_enrichment = enrichment
    if enrichment_path is not None:
        resolved_enrichment = _load_enrichment_artifact(
            path=enrichment_path,
            set_code=normalized_set,
            card_database=card_database,
        )

    if draft_source_name is not None and source_manifest_path is None:
        raise ProfilePublicationError(
            "draft_source_name requires source_manifest_path."
        )

    source_manifest: PublicDumpManifest | None = None
    selected_source: PublicDumpSource | None = None
    if source_manifest_path is not None:
        manifest_path = _path(value=source_manifest_path, field_name="source_manifest_path")
        try:
            loaded_manifest = load_public_dump_manifest(manifest_path)
        except (
            PublicDumpError,
            OSError,
            RecursionError,
            TypeError,
            ValueError,
            UnicodeError,
        ) as error:
            raise ProfilePublicationError("Could not load the source manifest.") from error

        selected_source = _select_source(
            manifest=loaded_manifest,
            requested_name=draft_source_name,
        )
        if selected_source.path is None:
            raise ProfilePublicationError(
                "The selected draft source must be a pinned local path, not a URL."
            )
        resolved_path = Path(selected_source.path).expanduser()
        if not resolved_path.is_absolute():
            resolved_path = manifest_path.expanduser().resolve().parent / resolved_path
        try:
            selected_source = PublicDumpSource(
                name=selected_source.name,
                path=resolved_path,
                sha256=selected_source.sha256,
                retrieved_at=selected_source.retrieved_at,
                attribution=selected_source.attribution,
                license=selected_source.license,
            )
            if normalized_stage == ProfileGenerationStage.METADATA:
                _verify_metadata_source(source=selected_source)
            # Pass only the selected source.  This makes the generated report
            # describe exactly the input consumed and never serializes paths.
            source_manifest = PublicDumpManifest(sources=(selected_source,))
        except (PublicDumpError, TypeError, ValueError) as error:
            raise ProfilePublicationError("The selected draft source is invalid.") from error

    try:
        generation = generate_set_profile(
            set_code=set_code,
            event_format=event_format,
            stage=normalized_stage,
            card_database=card_database,
            source_manifest=source_manifest,
            generated_at=generated_at,
            profile_version=profile_version,
            ratings=ratings,
            draft_source_name=None if selected_source is None else selected_source.name,
            enrichment=resolved_enrichment,
            config=config,
        )
        validated = validate_profile_generation(
            generation=generation,
            set_code=normalized_set,
            event_format=normalized_format,
            stage=normalized_stage,
        )
        gzip_bytes = validated.gzip_bytes
        report_bytes = validated.report_bytes
    except PublicDumpChecksumError as error:
        raise ProfilePublicationError(_SOURCE_CHECKSUM_ERROR) from error
    except ProfileEnhancementError as error:
        raise ProfilePublicationError(str(error)) from error
    except (ProfileGenerationError, SetProfileError) as error:
        evidence_error = _stage_evidence_error(stage=normalized_stage, error=error)
        raise ProfilePublicationError(
            _GENERATION_FALLBACK_ERROR if evidence_error is None else evidence_error
        ) from error
    except ProfilePublicationError as error:
        if str(error) in _KNOWN_GENERATION_VALIDATION_ERRORS:
            raise
        raise ProfilePublicationError(_GENERATION_FALLBACK_ERROR) from error
    except (
        AttributeError,
        CardDatabaseError,
        PublicDumpError,
        SeventeenLandsError,
        OSError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        gzip.BadGzipFile,
        EOFError,
        RecursionError,
        zlib.error,
    ) as error:
        raise ProfilePublicationError(_GENERATION_FALLBACK_ERROR) from error

    report = generation.report
    artifact_parent = _path(value=output_dir, field_name="output_dir") / (
        f"{normalized_set}-{normalized_format}"
    )
    artifact_path = artifact_parent / "artifacts" / f"{report.gzip_sha256}.json.gz"
    manifest_path = artifact_parent / "generation.json"

    try:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        _reuse_or_publish_artifact(path=artifact_path, payload=gzip_bytes)
        _publish_generation_marker(path=manifest_path, payload=report_bytes)
    except (OSError, TypeError, ValueError, UnicodeError) as error:
        raise ProfilePublicationError(
            "Could not publish the profile artifact or generation marker."
        ) from error

    return ProfilePublicationResult(
        generation=generation,
        artifact_path=artifact_path,
        manifest_path=manifest_path,
        input_count=1
        + int(ratings is not None)
        + int(selected_source is not None)
        + int(enrichment_path is not None),
    )


def _normalize_component(*, value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfilePublicationError(f"{field_name} must be a non-empty string.")
    normalized = value.strip().casefold()
    if "/" in normalized or "\\" in normalized or normalized in {".", ".."}:
        raise ProfilePublicationError(f"{field_name} must be a safe path component.")
    return normalized


def _path(*, value: PathInput, field_name: str) -> Path:
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError) as error:
        raise ProfilePublicationError(f"{field_name} must be a valid local path.") from error
    if not str(path):
        raise ProfilePublicationError(f"{field_name} must be a valid local path.")
    return path


_GUIDE_SCHEMA_VERSION = 1
_GUIDE_KEYS = frozenset(
    {
        "schema_version",
        "requested_url",
        "guide_id",
        "url",
        "text",
        "sha256",
        "retrieved_at",
    }
)


def _strict_json(payload: bytes) -> Any:
    """Decode strict UTF-8 JSON while rejecting duplicate keys and constants."""
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = item
        return value

    def constant(_value: str) -> Any:
        raise ValueError("non-finite JSON constant")

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=object_pairs,
        parse_constant=constant,
    )


def _guide_freeze_record(
    *, value: Any, guide_url: str | None, normalized_set: str
) -> GuideSource:
    """Validate one frozen guide record and reconstruct its source value."""
    if not isinstance(value, dict) or set(value) != _GUIDE_KEYS:
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR)
    if type(value["schema_version"]) is not int or value["schema_version"] != _GUIDE_SCHEMA_VERSION:
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR)
    guide_id = f"{normalized_set}-draftsim-guide"
    if value["guide_id"] != guide_id:
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR)
    if guide_url is None:
        # A reused record has no requested URL to compare against, so its stored
        # request must satisfy the same acquisition rules as a fresh request.
        try:
            _validate_guide_url(value["requested_url"])
        except (GuideClientError, TypeError, ValueError, UnicodeError) as error:
            raise ProfilePublicationError(_FROZEN_GUIDE_ERROR) from error
    elif value["requested_url"] != guide_url:
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR)
    if not isinstance(value["url"], str):
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR)
    try:
        # The private import is deliberate so the reuse path cannot drift from acquisition URL rules.
        _validate_guide_url(value["url"])
    except (GuideClientError, TypeError, ValueError, UnicodeError) as error:
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR) from error
    text = value["text"]
    if not isinstance(text, str):
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR)
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError as error:
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR) from error
    if value["sha256"] != digest:
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR)
    try:
        return GuideSource(
            guide_id=guide_id,
            url=value["url"],
            text=text,
            retrieved_at=value["retrieved_at"],
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise ProfilePublicationError(_FROZEN_GUIDE_ERROR) from error


def _load_enrichment_artifact(
    *,
    path: PathInput,
    set_code: str,
    card_database: CardDatabase,
) -> SemanticEnrichmentArtifact:
    """Load one confirmed enrichment artifact from its content-addressed run file."""

    artifact_path = _path(value=path, field_name="enrichment_path")
    if _ENRICHMENT_ARTIFACT_NAME.fullmatch(artifact_path.name) is None:
        raise ProfilePublicationError(_ENRICHMENT_INPUT_ERROR)
    try:
        payload = artifact_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != artifact_path.stem:
            raise ValueError("The enrichment artifact digest does not match its file name.")
        value = _strict_json(payload)
        if not isinstance(value, Mapping):
            raise ValueError("The enrichment artifact must be a JSON object.")
        requested = _requested_card_database(card_database, set_code)
        pins = value.get("guides")
        if not isinstance(pins, list):
            raise ValueError("The enrichment artifact guide pins must be a JSON array.")
        if len(pins) > 1:
            # One artifact may pin exactly one guide; anything else would need a
            # source the caller never supplied.
            raise ValueError("The enrichment artifact pins more than one guide.")
        guides: tuple[GuideSource, ...] = ()
        if pins:
            guide_path = artifact_path.parent.parent / "sources" / "guide.json"
            guide_value = _strict_json(guide_path.read_bytes())
            try:
                guide = _guide_freeze_record(
                    value=guide_value,
                    guide_url=None,
                    normalized_set=set_code,
                )
            except ProfilePublicationError as error:
                raise ProfilePublicationError(_ENRICHMENT_INPUT_ERROR) from error
            guides = (guide,)
        sources = EnrichmentSources(
            set_code=set_code,
            cards=tuple(requested.cards.values()),
            guides=guides,
        )
        return SemanticEnrichmentArtifact.from_bytes(payload, sources=sources)
    except (
        SemanticEnrichmentError,
        OSError,
        RecursionError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise ProfilePublicationError(_ENRICHMENT_INPUT_ERROR) from error


def _canonical_published_at(published_at: str | datetime) -> str:
    """Return the canonical manifest timestamp for one publication moment."""

    if isinstance(published_at, datetime):
        if published_at.tzinfo is None:
            raise ProfilePublicationError("published_at must include a timezone.")
        return published_at.astimezone(UTC).isoformat()
    try:
        return _manifest_timestamp(published_at, "published_at")
    except ProfileManifestError as error:
        raise ProfilePublicationError("Could not build the profile manifest.") from error


def _artifact_identity(artifact: ProfileManifestArtifact) -> tuple[str, str]:
    return artifact.set_code.casefold(), artifact.event_format.casefold()


def _verify_metadata_source(*, source: PublicDumpSource) -> None:
    """Verify a selected local source without materializing its contents."""

    if source.path is None or source.sha256 is None:
        raise ProfilePublicationError(_SOURCE_VERIFY_ERROR)
    digest = hashlib.sha256()
    try:
        with Path(source.path).open(mode="rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise ProfilePublicationError(_SOURCE_VERIFY_ERROR) from error
    if digest.hexdigest() != source.sha256:
        raise ProfilePublicationError(_SOURCE_CHECKSUM_ERROR)


def _stage_evidence_error(*, stage: str, error: BaseException) -> str | None:
    """Translate known evidence failures into stable operator diagnostics."""

    detail = str(error)
    if stage == ProfileGenerationStage.EARLY.value and detail in {
        "early profiles must contain empirical evidence.",
        _EARLY_EVIDENCE_ERROR,
    }:
        return _EARLY_EVIDENCE_ERROR
    if stage == ProfileGenerationStage.MATURE.value and detail in {
        "Mature profile generation requires accepted deck evidence.",
        "Mature profile generation requires Stage C targets for every "
        "accepted color pair.",
        _MATURE_EVIDENCE_ERROR,
    }:
        return _MATURE_EVIDENCE_ERROR
    return None


def _select_source(
    *,
    manifest: PublicDumpManifest,
    requested_name: str | None,
) -> PublicDumpSource:
    if requested_name is None:
        if len(manifest.sources) != 1:
            raise ProfilePublicationError(
                "source_manifest_path contains multiple sources; specify draft_source_name."
            )
        return manifest.sources[0]
    if not isinstance(requested_name, str) or not requested_name.strip():
        raise ProfilePublicationError("draft_source_name must be a non-empty source name.")
    selected = next(
        (source for source in manifest.sources if source.name == requested_name),
        None,
    )
    if selected is None:
        raise ProfilePublicationError("draft_source_name does not identify a manifest source.")
    return selected


def validate_profile_generation(
    *,
    generation: ProfileGenerationResult,
    set_code: str,
    event_format: str,
    stage: str,
) -> ValidatedProfileGeneration:
    if not isinstance(generation, ProfileGenerationResult):
        raise ProfilePublicationError("Profile generation returned an invalid result.")
    profile_bytes = generation.profile_bytes
    gzip_bytes = generation.gzip_bytes
    report = generation.report

    try:
        decompressed = gzip.decompress(gzip_bytes)
        decoded = decompressed.decode("utf-8")
        value = json.loads(decoded)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        EOFError,
        zlib.error,
    ) as error:
        raise ProfilePublicationError("Generated profile gzip could not be validated.") from error
    if not isinstance(value, Mapping):
        raise ProfilePublicationError("Generated profile JSON must be an object.")

    try:
        rebuilt = SetProfile.from_json(value)
        rebuilt_bytes = rebuilt.to_bytes()
    except (SetProfileError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ProfilePublicationError("Generated profile failed schema validation.") from error
    if (
        profile_bytes != decompressed
        or rebuilt_bytes != decompressed
        or rebuilt != generation.profile
    ):
        raise ProfilePublicationError("Generated profile bytes are not canonical.")
    if rebuilt.set_code != set_code or rebuilt.event_format != event_format:
        raise ProfilePublicationError("Generated profile does not match the requested set and format.")
    if report.set_profile_schema_version != rebuilt.schema_version:
        raise ProfilePublicationError("Generation report schema does not match the profile.")
    if report.set_code != set_code or report.event_format != event_format or report.stage != stage:
        raise ProfilePublicationError("Generation report does not match the requested profile.")
    expected_provenance = (
        None
        if rebuilt.enhancement is None
        else ProfileEnhancementProvenance.from_enhancement(rebuilt.enhancement)
    )
    if report.enhancement != expected_provenance:
        raise ProfilePublicationError(
            "Generation report enhancement provenance does not match the profile."
        )

    profile_sha256 = hashlib.sha256(decompressed).hexdigest()
    gzip_sha256 = hashlib.sha256(gzip_bytes).hexdigest()
    if (
        report.profile_bytes != len(decompressed)
        or report.profile_sha256 != profile_sha256
        or report.gzip_bytes != len(gzip_bytes)
        or report.gzip_sha256 != gzip_sha256
    ):
        raise ProfilePublicationError("Generation report checksums or sizes do not reconcile.")
    report_bytes = _validated_report_bytes(report=report)
    return ValidatedProfileGeneration(
        profile_bytes=profile_bytes,
        gzip_bytes=gzip_bytes,
        report_bytes=report_bytes,
    )


def _validated_report_bytes(*, report: ProfileGenerationReport) -> bytes:
    try:
        payload = report.to_bytes()
        value = json.loads(payload.decode("utf-8"))
        canonical = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (AttributeError, TypeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ProfilePublicationError("Generation report could not be serialized and parsed.") from error
    if not isinstance(value, Mapping) or canonical != payload or value != report.to_json():
        raise ProfilePublicationError("Generation report bytes are not canonical.")
    return payload


def _reuse_or_publish_artifact(*, path: Path, payload: bytes) -> None:
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        _atomic_write(path=path, payload=payload)
        return
    except (OSError, UnicodeError) as error:
        raise ProfilePublicationError("Could not inspect the existing profile artifact.") from error
    if existing != payload:
        raise ProfilePublicationError(
            "The content-addressed profile artifact exists with different bytes."
        )


def _publish_generation_marker(*, path: Path, payload: bytes) -> None:
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    except (OSError, UnicodeError) as error:
        raise ProfilePublicationError("Could not inspect the existing generation marker.") from error
    if existing == payload:
        return
    _atomic_write(path=path, payload=payload)


def _atomic_write(*, path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as error:
        raise ProfilePublicationError("Atomic profile publication failed.") from error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            except OSError:
                pass


__all__ = [
    "PROFILE_BASE_URL",
    "EnrichmentDowngradeConflict",
    "ProfilePublicationError",
    "ProfilePublicationResult",
    "PublishedProfilePublication",
    "ValidatedProfileGeneration",
    "build_profile_manifest",
    "filter_enriched_profile_downgrades",
    "generate_local_profile_artifacts",
    "merge_profile_manifest_artifacts",
    "profile_manifest_artifact_from_publication",
    "publish_profile_manifest",
    "publish_profile_object",
    "publish_profile_publication",
    "validate_profile_generation",
]

