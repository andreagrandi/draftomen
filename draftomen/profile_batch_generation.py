"""Deterministic batch orchestration for staged profile generation.

The batch service consumes a refresh plan and its staged execution authority.
It writes no artifacts: eligible validated payloads remain in memory for the
later publication slice, while the report is a compact safe projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, TypeAlias

from draftomen.profile_generation import (
    DEFAULT_PROFILE_GENERATION_CONFIG,
    PROFILE_GENERATION_SCHEMA_VERSION,
    PROFILE_GENERATOR_VERSION,
    ProfileGenerationConfig,
    ProfileGenerationReport,
)
from draftomen.profile_generation_execution import (
    PROFILE_GENERATION_EXECUTION_SCHEMA_VERSION,
    ProfileGenerationEnvironmentOutcome,
    ProfileGenerationEnvironmentResult,
    ProfileGenerationExecutionError,
    ProfileGenerationFailurePhase,
    ProfileGenerationFailureReason,
    generate_staged_environment_profile,
)
from draftomen.profile_refresh_execution import (
    PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION,
    PROFILE_REFRESH_EXECUTOR_VERSION,
    ProfileRefreshEnvironmentOutcome,
    _bundle_id as _environment_bundle_id,
)
from draftomen.profile_generation_stage_policy import (
    DEFAULT_PROFILE_GENERATION_STAGE_THRESHOLDS,
    ProfileGenerationStageThresholds,
)
from draftomen.profile_statistics import STATISTICS_VERSION
from draftomen.public_dump import PUBLIC_DUMP_MANIFEST_SCHEMA_VERSION
from draftomen.refresh_plan import PlannedEnvironment, RefreshPlan
from draftomen.set_profile import SET_PROFILE_SCHEMA_VERSION


_BATCH_INPUT_SOURCE_KEYS = frozenset(
    {
        "role",
        "name",
        "sha256",
        "source_version",
        "requested_set",
        "requested_format",
        "source_format",
        "acquired_at",
        "outcome",
        "fallback_state",
    }
)
_BATCH_INPUT_SOURCE_ROLES = frozenset(
    {"card_database", "seventeen_lands_ratings", "seventeen_lands_public_drafts"}
)
_BATCH_INPUT_SOURCE_OUTCOMES = frozenset(
    {"acquired", "cached", "offline-reused", "stale", "missing", "corrupt", "unavailable"}
)
_BATCH_INPUT_SOURCE_FALLBACKS = frozenset(
    {"none", "verified-stale-cache", "verified-offline-cache"}
)
_BATCH_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_BATCH_INPUT_SOURCE_ROWS = 3
_MAX_BATCH_INPUT_SOURCE_FIELD_LENGTH = 256
PROFILE_BATCH_REPORT_SCHEMA_VERSION = 1
PathInput: TypeAlias = str | os.PathLike[str]


class ProfileBatchGenerationError(ValueError):
    """Raised when the batch input authority is malformed or mismatched."""


class ProfileBatchEnvironmentOutcome(str, Enum):
    """Safe batch-level outcome labels."""

    PUBLICATION_ELIGIBLE = "publication-eligible"
    FAILED = "failed"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, UnicodeError, ValueError, OverflowError) as error:
        raise ProfileBatchGenerationError("batch report cannot be canonicalized") from error
    return (encoded + "\n").encode("utf-8")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProfileBatchGenerationError("batch generated_at must be timezone-aware")
    return value.astimezone(UTC)


def _path(value: PathInput) -> Path:
    try:
        return Path(os.fspath(value))
    except (TypeError, ValueError, OSError) as error:
        raise ProfileBatchGenerationError("batch staged directory is invalid") from error


def _read_execution_authority(
    *, plan: RefreshPlan, staged_dir: Path
) -> tuple[str, tuple[dict[str, Any], ...]]:
    """Read only the aggregate authority and reconcile it with ``plan``.

    Bundle contents are not inspected here; each staged bundle's plan binding
    is checked immediately before generation, while the generation service
    owns complete bundle validation and its bounded failure model.
    """

    try:
        value = json.loads((staged_dir / "execution.json").read_bytes())
    except (OSError, TypeError, UnicodeDecodeError, ValueError, RecursionError):
        raise ProfileBatchGenerationError("batch execution authority is invalid") from None
    if not isinstance(value, Mapping):
        raise ProfileBatchGenerationError("batch execution authority is invalid")

    schema_version = value.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION
    ):
        raise ProfileBatchGenerationError("batch execution authority is invalid")
    if value.get("executor_version") != PROFILE_REFRESH_EXECUTOR_VERSION:
        raise ProfileBatchGenerationError("batch execution authority is invalid")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in {"online", "offline"}:
        raise ProfileBatchGenerationError("batch execution authority is invalid")
    if value.get("plan_sha256") != sha256(plan.to_bytes()).hexdigest():
        raise ProfileBatchGenerationError("batch execution authority does not match plan")

    counts = value.get("counts")
    expected_count_keys = {"failed", "metadata_only", "planned", "staged"}
    if not isinstance(counts, Mapping) or set(counts) != expected_count_keys:
        raise ProfileBatchGenerationError("batch execution authority counts are invalid")
    for count in counts.values():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ProfileBatchGenerationError("batch execution authority counts are invalid")
    if counts["planned"] != len(plan.environments):
        raise ProfileBatchGenerationError("batch execution authority counts do not match plan")

    environments = value.get("environments")
    if not isinstance(environments, list) or len(environments) != len(plan.environments):
        raise ProfileBatchGenerationError("batch execution authority environments are invalid")

    parsed: list[dict[str, str]] = []
    staged_count = 0
    failed_count = 0
    metadata_only_count = 0
    for expected, candidate in zip(plan.environments, environments, strict=True):
        if not isinstance(candidate, Mapping):
            raise ProfileBatchGenerationError("batch execution authority environment is invalid")
        if candidate.get("environment") != expected.to_json():
            raise ProfileBatchGenerationError("batch execution authority environment order is invalid")
        if candidate.get("bundle_id") != _environment_bundle_id(expected):
            raise ProfileBatchGenerationError("batch execution authority bundle identity is invalid")
        outcome = candidate.get("outcome")
        if (
            not isinstance(outcome, str)
            or outcome
            not in (
                ProfileRefreshEnvironmentOutcome.STAGED.value,
                ProfileRefreshEnvironmentOutcome.FAILED.value,
            )
        ):
            raise ProfileBatchGenerationError("batch execution authority environment outcome is invalid")
        if outcome == ProfileRefreshEnvironmentOutcome.STAGED.value:
            staged_count += 1
            roles = candidate.get("available_input_roles")
            if not isinstance(roles, list) or not roles or any(not isinstance(role, str) for role in roles):
                raise ProfileBatchGenerationError("batch execution authority input roles are invalid")
            if roles == ["card_database"]:
                metadata_only_count += 1
        else:
            failed_count += 1
        parsed.append(
            {
                "bundle_id": candidate["bundle_id"],
                "outcome": outcome,
                "skip_reasons": candidate.get("skip_reasons"),
            }
        )
    if counts["staged"] != staged_count or counts["failed"] != failed_count:
        raise ProfileBatchGenerationError("batch execution authority counts do not match environments")
    if counts["metadata_only"] != metadata_only_count:
        raise ProfileBatchGenerationError("batch execution authority counts do not match environments")
    return mode, tuple(parsed)


def _bounded_failure(
    environment: PlannedEnvironment,
    *,
    phase: ProfileGenerationFailurePhase,
    reason: ProfileGenerationFailureReason,
) -> ProfileGenerationEnvironmentResult:
    """Return the safe result for a batch-level failure."""

    return ProfileGenerationEnvironmentResult(
        environment=environment,
        outcome=ProfileGenerationEnvironmentOutcome.FAILED,
        failure_phase=phase,
        failure_reason=reason,
    )


def _safe_skip_reasons(values: Any) -> dict[str, int]:
    """Project the executor's recorded skip reasons into bounded counts."""

    if not isinstance(values, list):
        return {}
    reasons: dict[str, int] = {}
    for value in values[:64]:
        text = _safe_text(value)
        if text:
            reasons[text] = reasons.get(text, 0) + 1
    return dict(sorted(reasons.items()))


def _safe_text(value: Any) -> str:
    if (
        not isinstance(value, str)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "/" in value
        or "\\" in value
        or "://" in value
    ):
        return ""
    return value


def _safe_sources(
    report: ProfileGenerationReport,
    input_sources: tuple[Mapping[str, str], ...],
) -> tuple[dict[str, str], ...]:
    try:
        descriptors = tuple(report.sources)
        staged = tuple(input_sources)
    except (AttributeError, TypeError, ValueError) as error:
        raise ProfileBatchGenerationError("batch source provenance is invalid") from error
    if len(descriptors) > 64 or len(staged) > _MAX_BATCH_INPUT_SOURCE_ROWS:
        raise ProfileBatchGenerationError("batch source provenance exceeds the bound")

    rows: list[dict[str, str]] = []
    for source in descriptors:
        digest = _safe_text(getattr(source, "sha256", ""))
        if digest and _BATCH_SHA256_PATTERN.fullmatch(digest) is None:
            digest = ""
        rows.append(
            {
                "attribution": _safe_text(getattr(source, "attribution", "")),
                "license": _safe_text(getattr(source, "license", "")),
                "name": _safe_text(getattr(source, "name", "")),
                "sha256": digest,
            }
        )

    staged_rows = [_validate_staged_source(item) for item in staged]
    if len({row["role"] for row in staged_rows}) != len(staged_rows):
        raise ProfileBatchGenerationError("batch staged source roles are duplicated")
    staged_rows.sort(key=lambda item: (item["role"], item["name"], item["sha256"]))
    consumed: set[int] = set()
    for row in rows:
        digest = row["sha256"]
        if not digest:
            continue
        matches = [
            index
            for index, staged_row in enumerate(staged_rows)
            if index not in consumed and staged_row["sha256"] == digest
        ]
        match = next(
            (
                index
                for index in matches
                if staged_rows[index]["name"] == row["name"]
            ),
            matches[0] if matches else None,
        )
        if match is not None:
            row.update(staged_rows[match])
            consumed.add(match)

    for index, staged_row in enumerate(staged_rows):
        if index not in consumed:
            rows.append(
                {
                    "attribution": "",
                    "license": "",
                    **staged_row,
                }
            )
    return tuple(rows)


def _validate_staged_source(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ProfileBatchGenerationError("batch staged source is invalid")
    try:
        if set(value) != _BATCH_INPUT_SOURCE_KEYS:
            raise ProfileBatchGenerationError("batch staged source keys are invalid")
    except (TypeError, ValueError) as error:
        raise ProfileBatchGenerationError("batch staged source keys are invalid") from error

    allowed_empty = {
        "sha256",
        "source_version",
        "requested_format",
        "source_format",
        "acquired_at",
    }
    fields = {
        key: _safe_source_text(value[key], key, allow_empty=key in allowed_empty)
        for key in _BATCH_INPUT_SOURCE_KEYS
    }
    if fields["role"] not in _BATCH_INPUT_SOURCE_ROLES:
        raise ProfileBatchGenerationError("batch staged source role is invalid")
    if not fields["name"] or not fields["requested_set"]:
        raise ProfileBatchGenerationError("batch staged source identity is invalid")
    if fields["outcome"] not in _BATCH_INPUT_SOURCE_OUTCOMES:
        raise ProfileBatchGenerationError("batch staged source outcome is invalid")
    expected_fallback = {
        "stale": "verified-stale-cache",
        "offline-reused": "verified-offline-cache",
    }.get(fields["outcome"], "none")
    if fields["fallback_state"] not in _BATCH_INPUT_SOURCE_FALLBACKS:
        raise ProfileBatchGenerationError("batch staged source fallback is invalid")
    if fields["fallback_state"] != expected_fallback:
        raise ProfileBatchGenerationError("batch staged source fallback does not match outcome")

    digest = fields["sha256"]
    if digest and _BATCH_SHA256_PATTERN.fullmatch(digest) is None:
        raise ProfileBatchGenerationError("batch staged source digest is invalid")
    acquired_at = fields["acquired_at"]
    if acquired_at:
        fields["acquired_at"] = _source_timestamp(acquired_at)
    unavailable_optional = (
        fields["role"]
        in {"seventeen_lands_ratings", "seventeen_lands_public_drafts"}
        and fields["outcome"] in {"missing", "corrupt", "unavailable"}
    )
    if unavailable_optional:
        if digest or fields["source_version"] or acquired_at:
            raise ProfileBatchGenerationError(
                "batch unavailable optional source must have empty pins and timestamp"
            )
    elif not acquired_at:
        raise ProfileBatchGenerationError("batch staged source timestamp is required")
    return fields


def _safe_source_text(value: Any, field_name: str, *, allow_empty: bool) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > _MAX_BATCH_INPUT_SOURCE_FIELD_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "/" in value
        or "\\" in value
        or "://" in value
    ):
        raise ProfileBatchGenerationError(f"batch staged source {field_name} is invalid")
    return value


def _source_timestamp(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ProfileBatchGenerationError("batch staged source timestamp is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ProfileBatchGenerationError("batch staged source timestamp is invalid")
    return timestamp.astimezone(UTC).isoformat()


def _environment_identity(environment: PlannedEnvironment) -> dict[str, str | None]:
    # Selection reasons are caller-controlled and are not needed by publication.
    return {
        "event_format": environment.event_format,
        "lifecycle": environment.lifecycle,
        "set_code": environment.set_code,
    }


@dataclass(frozen=True, slots=True)
class ProfileBatchEnvironmentReport:
    """Privacy-safe report projection for one generated environment."""

    environment: PlannedEnvironment
    outcome: ProfileBatchEnvironmentOutcome | str
    selection: Mapping[str, Any] | None = None
    sources: tuple[Mapping[str, str], ...] = ()
    samples: Mapping[str, Any] | None = None
    card_games: int | None = None
    pair_games: int | None = None
    skip_count: int | None = None
    error_count: int | None = None
    skip_reasons: Mapping[str, int] = field(default_factory=dict)
    error_reasons: Mapping[str, int] = field(default_factory=dict)
    failure_phase: str | None = None
    failure_reason: str | None = None
    profile_sha256: str | None = None
    profile_bytes: int | None = None
    gzip_sha256: str | None = None
    gzip_bytes: int | None = None
    generation_report_sha256: str | None = None
    generation_report_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", ProfileBatchEnvironmentOutcome(self.outcome))

    def to_json(self) -> dict[str, Any]:
        return {
            "artifacts": {
                "gzip": {"bytes": self.gzip_bytes, "sha256": self.gzip_sha256},
                "profile": {"bytes": self.profile_bytes, "sha256": self.profile_sha256},
                "report": {"bytes": self.generation_report_bytes, "sha256": self.generation_report_sha256},
            },
            "card_games": self.card_games,
            "environment": _environment_identity(self.environment),
            "error_count": self.error_count,
            "error_reasons": dict(self.error_reasons),
            "failure_phase": self.failure_phase,
            "failure_reason": self.failure_reason,
            "outcome": self.outcome.value,
            "pair_games": self.pair_games,
            "samples": None if self.samples is None else dict(self.samples),
            "selection": None if self.selection is None else dict(self.selection),
            "skip_count": self.skip_count,
            "skip_reasons": dict(self.skip_reasons),
            "sources": [dict(source) for source in self.sources],
        }


@dataclass(frozen=True, slots=True)
class ProfileBatchGenerationReport:
    """Canonical, privacy-safe aggregate report."""

    plan_sha256: str
    selection_mode: str
    execution_mode: str
    counts: Mapping[str, int]
    environments: tuple[ProfileBatchEnvironmentReport, ...]
    generator_version: str = PROFILE_GENERATOR_VERSION
    profile_generation_schema_version: int = PROFILE_GENERATION_SCHEMA_VERSION
    profile_generation_execution_schema_version: int = PROFILE_GENERATION_EXECUTION_SCHEMA_VERSION
    set_profile_schema_version: int = SET_PROFILE_SCHEMA_VERSION
    public_dump_manifest_schema_version: int = PUBLIC_DUMP_MANIFEST_SCHEMA_VERSION
    statistics_version: int = STATISTICS_VERSION
    schema_version: int = PROFILE_BATCH_REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.execution_mode not in ("online", "offline"):
            raise ProfileBatchGenerationError("batch report execution mode is invalid")
        environments = tuple(self.environments)
        counts = dict(self.counts)
        expected = {
            "failed": sum(item.outcome is ProfileBatchEnvironmentOutcome.FAILED for item in environments),
            "planned": len(environments),
            "publication_eligible": sum(
                item.outcome is ProfileBatchEnvironmentOutcome.PUBLICATION_ELIGIBLE for item in environments
            ),
        }
        if counts != expected:
            raise ProfileBatchGenerationError("batch report counts do not reconcile")
        object.__setattr__(self, "environments", environments)
        object.__setattr__(self, "counts", counts)

    def to_json(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "environments": [environment.to_json() for environment in self.environments],
            "execution_mode": self.execution_mode,
            "plan_sha256": self.plan_sha256,
            "schema_version": self.schema_version,
            "selection_mode": self.selection_mode,
            "versions": {
                "generator_version": self.generator_version,
                "profile_generation_execution_schema_version": self.profile_generation_execution_schema_version,
                "profile_generation_schema_version": self.profile_generation_schema_version,
                "public_dump_manifest_schema_version": self.public_dump_manifest_schema_version,
                "set_profile_schema_version": self.set_profile_schema_version,
                "statistics_version": self.statistics_version,
            },
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_json())


def _environment_report(
    result: ProfileGenerationEnvironmentResult,
    executor_skip_reasons: Mapping[str, int] | None = None,
) -> ProfileBatchEnvironmentReport:
    if not result.publication_eligible:
        return ProfileBatchEnvironmentReport(
            environment=result.environment,
            outcome=ProfileBatchEnvironmentOutcome.FAILED,
            selection=None if result.selection is None else result.selection.to_json(),
            skip_reasons=dict(executor_skip_reasons or {}),
            failure_phase=None if result.failure_phase is None else result.failure_phase.value,
            failure_reason=None if result.failure_reason is None else result.failure_reason.value,
        )

    generation = result.generation
    report_bytes = result.report_bytes
    if generation is None or report_bytes is None:
        raise ProfileBatchGenerationError("eligible generation result is invalid")
    report = generation.report
    return ProfileBatchEnvironmentReport(
        environment=result.environment,
        outcome=ProfileBatchEnvironmentOutcome.PUBLICATION_ELIGIBLE,
        selection=None if result.selection is None else result.selection.to_json(),
        sources=_safe_sources(report, result.input_sources),
        samples=report.samples.to_json(),
        card_games=report.card_games,
        pair_games=report.pair_games,
        skip_count=result.skip_count,
        error_count=result.error_count,
        skip_reasons=report.skip_reasons,
        error_reasons=report.error_reasons,
        profile_sha256=result.profile_sha256,
        profile_bytes=result.profile_size,
        gzip_sha256=result.gzip_sha256,
        gzip_bytes=result.gzip_size,
        generation_report_sha256=sha256(report_bytes).hexdigest(),
        generation_report_bytes=len(report_bytes),
    )


@dataclass(frozen=True, slots=True)
class ProfileBatchGenerationResult:
    """Batch result retaining eligible validated payloads in memory."""

    plan_sha256: str
    selection_mode: str
    generated_at: datetime
    environments: tuple[ProfileGenerationEnvironmentResult, ...]
    report: ProfileBatchGenerationReport

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", _timestamp(self.generated_at))
        object.__setattr__(self, "environments", tuple(self.environments))
        if self.report.plan_sha256 != self.plan_sha256 or self.report.selection_mode != self.selection_mode:
            raise ProfileBatchGenerationError("batch result report does not reconcile")

    @property
    def planned_count(self) -> int:
        return len(self.environments)

    @property
    def publication_eligible_count(self) -> int:
        return sum(item.publication_eligible for item in self.environments)

    @property
    def failed_count(self) -> int:
        return self.planned_count - self.publication_eligible_count

    @property
    def succeeded(self) -> bool:
        return self.failed_count == 0

    @property
    def reconciled_counts(self) -> Mapping[str, int]:
        return {
            "failed": self.failed_count,
            "planned": self.planned_count,
            "publication_eligible": self.publication_eligible_count,
        }

    @property
    def eligible_results(self) -> tuple[ProfileGenerationEnvironmentResult, ...]:
        return tuple(item for item in self.environments if item.publication_eligible)

    @property
    def validated_payloads(self) -> tuple[Any, ...]:
        return tuple(item.validated for item in self.eligible_results)

    def to_json(self) -> dict[str, Any]:
        return self.report.to_json()

    def to_bytes(self) -> bytes:
        return self.report.to_bytes()


def generate_staged_profile_batch(
    *,
    plan: RefreshPlan,
    staged_dir: PathInput,
    generated_at: datetime,
    profile_version: str = "1.0",
    config: ProfileGenerationConfig = DEFAULT_PROFILE_GENERATION_CONFIG,
    thresholds: ProfileGenerationStageThresholds = DEFAULT_PROFILE_GENERATION_STAGE_THRESHOLDS,
) -> ProfileBatchGenerationResult:
    """Generate every plan environment from one reconciled staged execution."""

    if not isinstance(plan, RefreshPlan):
        raise ProfileBatchGenerationError("batch plan is invalid")
    timestamp = _timestamp(generated_at)
    if not isinstance(profile_version, str) or not profile_version.strip():
        raise ProfileBatchGenerationError("batch profile_version is invalid")
    root = _path(staged_dir)
    execution_mode, authorities = _read_execution_authority(plan=plan, staged_dir=root)
    plan_sha256 = sha256(plan.to_bytes()).hexdigest()

    results: list[tuple[ProfileGenerationEnvironmentResult, Mapping[str, int]]] = []
    for environment, authority in zip(plan.environments, authorities, strict=True):
        if authority["outcome"] == ProfileRefreshEnvironmentOutcome.FAILED.value:
            results.append(
                (
                    _bounded_failure(
                        environment,
                        phase=ProfileGenerationFailurePhase.REFRESH_EXECUTION,
                        reason=ProfileGenerationFailureReason.REFRESH_EXECUTION_FAILED,
                    ),
                    _safe_skip_reasons(authority["skip_reasons"]),
                )
            )
            continue
        try:
            generated = generate_staged_environment_profile(
                bundle_path=root / "bundles" / authority["bundle_id"],
                environment=environment,
                generated_at=timestamp,
                profile_version=profile_version,
                config=config,
                thresholds=thresholds,
                expected_plan_sha256=plan_sha256,
            )
            if not isinstance(generated, ProfileGenerationEnvironmentResult) or generated.environment != environment:
                raise ProfileBatchGenerationError("batch environment generation result is invalid")
        except (ProfileBatchGenerationError, ProfileGenerationExecutionError):
            # Only the bounded error families are isolated per environment;
            # genuine defects (TypeError, MemoryError, ...) must propagate.
            generated = _bounded_failure(
                environment,
                phase=ProfileGenerationFailurePhase.STAGED_BUNDLE_LOAD,
                reason=ProfileGenerationFailureReason.STAGED_BUNDLE_LOAD_FAILED,
            )
        results.append((generated, {}))

    generation_results = tuple(result for result, _ in results)
    report_environments = tuple(
        _environment_report(result, executor_skip_reasons=skips) for result, skips in results
    )
    report = ProfileBatchGenerationReport(
        plan_sha256=plan_sha256,
        selection_mode=plan.selection_mode,
        execution_mode=execution_mode,
        counts={
            "failed": sum(not item.publication_eligible for item in generation_results),
            "planned": len(generation_results),
            "publication_eligible": sum(item.publication_eligible for item in generation_results),
        },
        environments=report_environments,
        generator_version=config.generator_version,
        statistics_version=config.statistics_version,
    )
    return ProfileBatchGenerationResult(
        plan_sha256=report.plan_sha256,
        selection_mode=report.selection_mode,
        generated_at=timestamp,
        environments=generation_results,
        report=report,
    )


__all__ = [
    "PROFILE_BATCH_REPORT_SCHEMA_VERSION",
    "ProfileBatchEnvironmentOutcome",
    "ProfileBatchEnvironmentReport",
    "ProfileBatchGenerationError",
    "ProfileBatchGenerationReport",
    "ProfileBatchGenerationResult",
    "generate_staged_profile_batch",
]
