"""Validated local publication for deterministic set-profile artifacts.

This module is deliberately a local-only boundary.  It loads only caller-selected
cache files, validates the generated profile and report bytes before touching the
publication directory, and commits the immutable content object before replacing
the authoritative generation marker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Mapping, TypeAlias
import zlib

from draftomen.carddb import CardDatabaseError, load_card_database
from draftomen.profile_generation import (
    DEFAULT_PROFILE_GENERATION_CONFIG,
    ProfileGenerationConfig,
    ProfileGenerationError,
    ProfileGenerationReport,
    ProfileGenerationResult,
    ProfileGenerationStage,
    generate_set_profile,
)
from draftomen.profile_manifest import (
    ProfileManifest,
    ProfileManifestArtifact,
    ProfileManifestError,
)
from draftomen.public_dump import (
    PublicDumpChecksumError,
    PublicDumpError,
    PublicDumpManifest,
    PublicDumpSource,
    load_public_dump_manifest,
)
from draftomen.seventeen import SeventeenLandsError, load_17lands_format_data
from draftomen.set_profile import ProfileMaturity, SetProfile, SetProfileError


PathInput: TypeAlias = str | os.PathLike[str]


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

_KNOWN_GENERATION_VALIDATION_ERRORS = frozenset(
    {
        "Profile generation returned an invalid result.",
        "Generated profile gzip could not be validated.",
        "Generated profile JSON must be an object.",
        "Generated profile failed schema validation.",
        "Generated profile bytes are not canonical.",
        "Generated profile does not match the requested set and format.",
        "Generation report does not match the requested profile.",
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
        timestamp = (
            published_at.astimezone(UTC).isoformat()
            if isinstance(published_at, datetime)
            else published_at
        )
        if isinstance(published_at, datetime) and published_at.tzinfo is None:
            raise ProfilePublicationError("published_at must include a timezone.")
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
    profile_version: str = "1.0",
    config: ProfileGenerationConfig = DEFAULT_PROFILE_GENERATION_CONFIG,
) -> ProfilePublicationResult:
    """Generate and atomically publish one local profile artifact.

    Inputs are loaded strictly from the paths supplied by the caller.  The
    content-addressed gzip object is committed before ``generation.json``;
    replacing the latter is the sole authoritative commit operation.
    """

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
        input_count=1 + int(ratings is not None) + int(selected_source is not None),
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
    "ProfilePublicationError",
    "ProfilePublicationResult",
    "ValidatedProfileGeneration",
    "build_profile_manifest",
    "generate_local_profile_artifacts",
    "profile_manifest_artifact_from_publication",
    "publish_profile_manifest",
    "validate_profile_generation",
]

