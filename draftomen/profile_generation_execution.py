"""UI-neutral execution of one staged profile-generation environment.

This module consumes exactly one strict staged input bundle, chooses the
explicit generation stage, produces bounded classification diagnostics, and
runs the existing generator through the public publication validation gate.
No artifact or publication filesystem is touched here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, TypeAlias

from draftomen.profile_generation import (
    DEFAULT_PROFILE_GENERATION_CONFIG,
    ProfileGenerationConfig,
    ProfileGenerationResult,
    generate_set_profile,
)
from draftomen.set_profile import ProfileMaturity
from draftomen.profile_generation_stage_policy import (
    DEFAULT_PROFILE_GENERATION_STAGE_THRESHOLDS,
    ProfileGenerationStageSelection,
    ProfileGenerationStageThresholds,
    select_profile_generation_stage,
)
from draftomen.profile_publication import (
    ValidatedProfileGeneration,
    validate_profile_generation,
)
from draftomen.profile_refresh_execution import (
    PathInput,
    load_staged_profile_build_bundle,
)
from draftomen.refresh_plan import PlannedEnvironment
from draftomen.semantic_roles import RoleClassifier
from draftomen.profile_input_acquisition import ProfileInputAcquisitionOutcome


_GENERATION_INPUT_SOURCE_KEYS = frozenset(
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
_GENERATION_INPUT_SOURCE_ROLES = frozenset(
    {"card_database", "seventeen_lands_ratings", "seventeen_lands_public_drafts"}
)
_GENERATION_FALLBACK_STATES = frozenset(
    {"none", "verified-stale-cache", "verified-offline-cache"}
)
_GENERATION_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_INPUT_SOURCE_ROWS = 3
_MAX_INPUT_SOURCE_FIELD_LENGTH = 256


PROFILE_GENERATION_EXECUTION_SCHEMA_VERSION = 1
_MAX_DIAGNOSTICS = 64
_MAX_DIAGNOSTIC_FIELD_LENGTH = 128

# These strings are intentionally fixed.  In particular, classifier exception
# text is never carried into a result or its canonical serialization.
CLASSIFICATION_GAP_REASON = "classification produced no role assignments"
CLASSIFICATION_ERROR_REASON = "classification failed while inspecting the card"


class ProfileGenerationExecutionError(RuntimeError):
    """Raised when the execution result contract itself is malformed."""


class ProfileGenerationEnvironmentOutcome(str, Enum):
    """The finite outcomes of one environment execution."""

    PUBLICATION_ELIGIBLE = "publication-eligible"
    FAILED = "failed"


class ProfileGenerationFailurePhase(str, Enum):
    """The finite execution phase at which a bounded failure occurred."""

    REFRESH_EXECUTION = "refresh-execution"
    STAGED_BUNDLE_LOAD = "staged-bundle-load"
    STAGE_SELECTION = "stage-selection"
    GENERATION = "generation"
    VALIDATION = "validation"


class ProfileGenerationFailureReason(str, Enum):
    """Finite, path-free failure reasons exposed by this service."""

    REFRESH_EXECUTION_FAILED = "refresh-execution-failed"
    STAGED_BUNDLE_LOAD_FAILED = "staged-bundle-load-failed"
    STAGE_SELECTION_FAILED = "stage-selection-failed"
    GENERATION_FAILED = "generation-failed"
    VALIDATION_FAILED = "validation-failed"


@dataclass(frozen=True, slots=True)
class ProfileGenerationDiagnostic:
    """One bounded, path-free classification diagnostic."""

    card_key: str
    card_name: str | None
    mechanic: str | None
    reason: str

    def __post_init__(self) -> None:
        card_key = _bounded_required(self.card_key, "card_key")
        reason = _bounded_required(self.reason, "reason")
        card_name = _bounded_optional(self.card_name)
        mechanic = _bounded_optional(self.mechanic)
        object.__setattr__(self, "card_key", card_key)
        object.__setattr__(self, "card_name", card_name)
        object.__setattr__(self, "mechanic", mechanic)
        object.__setattr__(self, "reason", reason)

    def to_json(self) -> dict[str, str | None]:
        return {
            "card_key": self.card_key,
            "card_name": self.card_name,
            "mechanic": self.mechanic,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProfileGenerationEnvironmentResult:
    """One immutable, bounded result for one planned environment."""

    environment: PlannedEnvironment
    outcome: ProfileGenerationEnvironmentOutcome | str
    selection: ProfileGenerationStageSelection | None = None
    diagnostics: tuple[ProfileGenerationDiagnostic, ...] = ()
    diagnostic_total: int = 0
    diagnostics_omitted: int = 0
    skip_count: int | None = None
    error_count: int | None = None
    generation: ProfileGenerationResult | None = None
    validated: ValidatedProfileGeneration | None = None
    failure_phase: ProfileGenerationFailurePhase | str | None = None
    failure_reason: ProfileGenerationFailureReason | str | None = None
    input_sources: tuple[Mapping[str, str], ...] = ()
    schema_version: int = PROFILE_GENERATION_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.environment, PlannedEnvironment):
            raise ProfileGenerationExecutionError("environment must be a PlannedEnvironment")
        if self.schema_version != PROFILE_GENERATION_EXECUTION_SCHEMA_VERSION:
            raise ProfileGenerationExecutionError("unsupported profile generation execution schema")
        try:
            outcome = (
                self.outcome
                if isinstance(self.outcome, ProfileGenerationEnvironmentOutcome)
                else ProfileGenerationEnvironmentOutcome(self.outcome)
            )
        except (TypeError, ValueError) as error:
            raise ProfileGenerationExecutionError("profile generation outcome is invalid") from error
        object.__setattr__(self, "outcome", outcome)

        selection = self.selection
        if selection is not None and not isinstance(selection, ProfileGenerationStageSelection):
            raise ProfileGenerationExecutionError("profile generation stage selection is invalid")

        diagnostics = tuple(self.diagnostics)
        if any(not isinstance(item, ProfileGenerationDiagnostic) for item in diagnostics):
            raise ProfileGenerationExecutionError("profile generation diagnostics are invalid")
        diagnostics = tuple(sorted(set(diagnostics), key=_diagnostic_sort_key))
        if len(diagnostics) > _MAX_DIAGNOSTICS:
            raise ProfileGenerationExecutionError("profile generation diagnostics exceed the bound")
        object.__setattr__(self, "diagnostics", diagnostics)
        try:
            input_sources = tuple(self.input_sources)
        except (TypeError, ValueError) as error:
            raise ProfileGenerationExecutionError("profile generation input sources are invalid") from error
        if len(input_sources) > _MAX_INPUT_SOURCE_ROWS:
            raise ProfileGenerationExecutionError("profile generation input sources exceed the bound")
        normalized_sources: list[dict[str, str]] = []
        roles: set[str] = set()
        for item in input_sources:
            normalized = _validate_input_source(item)
            role = normalized["role"]
            if role in roles:
                raise ProfileGenerationExecutionError("profile generation input source roles are duplicated")
            roles.add(role)
            normalized_sources.append(normalized)
        object.__setattr__(self, "input_sources", tuple(normalized_sources))

        for name in ("diagnostic_total", "diagnostics_omitted"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProfileGenerationExecutionError(f"{name} is invalid")
        if self.diagnostic_total < len(diagnostics):
            raise ProfileGenerationExecutionError("diagnostic total is below retained diagnostics")
        if self.diagnostics_omitted != self.diagnostic_total - len(diagnostics):
            raise ProfileGenerationExecutionError("diagnostic omission count does not reconcile")

        if self.outcome is ProfileGenerationEnvironmentOutcome.PUBLICATION_ELIGIBLE:
            if selection is None or not isinstance(self.generation, ProfileGenerationResult):
                raise ProfileGenerationExecutionError("eligible result requires stage selection and generation")
            if not isinstance(self.validated, ValidatedProfileGeneration):
                raise ProfileGenerationExecutionError("eligible result requires validated payload")
            if self.failure_phase is not None or self.failure_reason is not None:
                raise ProfileGenerationExecutionError("eligible result cannot carry a failure")
            _validate_count(self.skip_count, "skip_count", required=True)
            _validate_count(self.error_count, "error_count", required=True)

            try:
                expected_validated = validate_profile_generation(
                    generation=self.generation,
                    set_code=self.environment.set_code.casefold(),
                    event_format=self.environment.event_format.casefold(),
                    stage=selection.stage.value,
                )
                if not isinstance(expected_validated, ValidatedProfileGeneration):
                    raise ProfileGenerationExecutionError(
                        "eligible result validator returned an invalid payload"
                    )
            except Exception as error:  # noqa: BLE001 - malformed success payloads are rejected
                raise ProfileGenerationExecutionError(
                    "eligible result generation failed publication validation"
                ) from error
            if (
                self.validated.profile_bytes != expected_validated.profile_bytes
                or self.validated.gzip_bytes != expected_validated.gzip_bytes
                or self.validated.report_bytes != expected_validated.report_bytes
            ):
                raise ProfileGenerationExecutionError(
                    "eligible result validated payload does not match generation"
                )

            report = self.generation.report
            expected_maturity = {
                "metadata": ProfileMaturity.METADATA_ONLY,
                "early": ProfileMaturity.EARLY,
                "mature": ProfileMaturity.MATURE,
            }.get(selection.stage.value)
            if (
                expected_maturity is None
                or self.generation.profile.maturity is not expected_maturity
                or report.generated_at != self.generation.profile.generated_at
            ):
                raise ProfileGenerationExecutionError(
                    "eligible result generation identity and stage do not reconcile"
                )
            try:
                expected_skip_count = sum(report.skip_reasons.values())
                expected_error_count = sum(report.error_reasons.values())
            except (AttributeError, TypeError, ValueError) as error:
                raise ProfileGenerationExecutionError(
                    "eligible result generation counts are invalid"
                ) from error
            if self.skip_count != expected_skip_count or self.error_count != expected_error_count:
                raise ProfileGenerationExecutionError(
                    "eligible result generator counts do not reconcile"
                )
        else:
            if self.generation is not None or self.validated is not None:
                raise ProfileGenerationExecutionError("failed result cannot carry success payloads")
            if self.failure_phase is None or self.failure_reason is None:
                raise ProfileGenerationExecutionError("failed result requires a bounded failure")
            try:
                phase = (
                    self.failure_phase
                    if isinstance(self.failure_phase, ProfileGenerationFailurePhase)
                    else ProfileGenerationFailurePhase(self.failure_phase)
                )
                reason = (
                    self.failure_reason
                    if isinstance(self.failure_reason, ProfileGenerationFailureReason)
                    else ProfileGenerationFailureReason(self.failure_reason)
                )
            except (TypeError, ValueError) as error:
                raise ProfileGenerationExecutionError("profile generation failure is invalid") from error
            expected_reason = {
                ProfileGenerationFailurePhase.REFRESH_EXECUTION: ProfileGenerationFailureReason.REFRESH_EXECUTION_FAILED,
                ProfileGenerationFailurePhase.STAGED_BUNDLE_LOAD: ProfileGenerationFailureReason.STAGED_BUNDLE_LOAD_FAILED,
                ProfileGenerationFailurePhase.STAGE_SELECTION: ProfileGenerationFailureReason.STAGE_SELECTION_FAILED,
                ProfileGenerationFailurePhase.GENERATION: ProfileGenerationFailureReason.GENERATION_FAILED,
                ProfileGenerationFailurePhase.VALIDATION: ProfileGenerationFailureReason.VALIDATION_FAILED,
            }[phase]
            if reason is not expected_reason:
                raise ProfileGenerationExecutionError("profile generation failure phase does not match reason")
            object.__setattr__(self, "failure_phase", phase)
            object.__setattr__(self, "failure_reason", reason)
            _validate_count(self.skip_count, "skip_count", required=False)
            _validate_count(self.error_count, "error_count", required=False)
            if self.skip_count is not None or self.error_count is not None:
                raise ProfileGenerationExecutionError("failed result cannot carry generator counts")

    @property
    def publication_eligible(self) -> bool:
        return self.outcome is ProfileGenerationEnvironmentOutcome.PUBLICATION_ELIGIBLE

    @property
    def profile_bytes(self) -> bytes | None:
        return None if self.validated is None else self.validated.profile_bytes

    @property
    def gzip_bytes(self) -> bytes | None:
        return None if self.validated is None else self.validated.gzip_bytes

    @property
    def report_bytes(self) -> bytes | None:
        return None if self.validated is None else self.validated.report_bytes

    @property
    def profile_sha256(self) -> str | None:
        return _sha256_or_none(self.profile_bytes)

    @property
    def gzip_sha256(self) -> str | None:
        return _sha256_or_none(self.gzip_bytes)

    @property
    def report_sha256(self) -> str | None:
        return _sha256_or_none(self.report_bytes)

    @property
    def profile_size(self) -> int | None:
        return _size_or_none(self.profile_bytes)

    @property
    def gzip_size(self) -> int | None:
        return _size_or_none(self.gzip_bytes)

    @property
    def report_size(self) -> int | None:
        return _size_or_none(self.report_bytes)

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "diagnostic_total": self.diagnostic_total,
            "diagnostics": [item.to_json() for item in self.diagnostics],
            "diagnostics_omitted": self.diagnostics_omitted,
            "environment": self.environment.to_json(),
            "input_sources": [dict(item) for item in self.input_sources],
            "failure_phase": None,
            "failure_reason": None,
            "generator_counts": None,
            "outcome": self.outcome.value,
            "schema_version": self.schema_version,
            "selection": None if self.selection is None else self.selection.to_json(),
            "validated": None,
        }
        if self.publication_eligible:
            value["generator_counts"] = {
                "errors": self.error_count,
                "skips": self.skip_count,
            }
            value["validated"] = {
                "gzip_bytes": self.gzip_size,
                "gzip_sha256": self.gzip_sha256,
                "profile_bytes": self.profile_size,
                "profile_sha256": self.profile_sha256,
                "report_bytes": self.report_size,
                "report_sha256": self.report_sha256,
            }
        else:
            value["failure_phase"] = self.failure_phase.value
            value["failure_reason"] = self.failure_reason.value
        return value

    def to_bytes(self) -> bytes:
        return (
            json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


def generate_staged_environment_profile(
    *,
    bundle_path: PathInput,
    environment: PlannedEnvironment,
    generated_at: datetime,
    profile_version: str = "1.0",
    config: ProfileGenerationConfig = DEFAULT_PROFILE_GENERATION_CONFIG,
    thresholds: ProfileGenerationStageThresholds = DEFAULT_PROFILE_GENERATION_STAGE_THRESHOLDS,
    expected_plan_sha256: str | None = None,
) -> ProfileGenerationEnvironmentResult:
    """Generate and validate exactly one staged environment profile."""

    if not isinstance(environment, PlannedEnvironment):
        raise ProfileGenerationExecutionError("environment must be a PlannedEnvironment")

    try:
        bundle = load_staged_profile_build_bundle(
            bundle_path, environment=environment, expected_plan_sha256=expected_plan_sha256
        )
    except Exception:  # noqa: BLE001 - convert all local failures to bounded output
        return _failure(
            environment=environment,
            phase=ProfileGenerationFailurePhase.STAGED_BUNDLE_LOAD,
            reason=ProfileGenerationFailureReason.STAGED_BUNDLE_LOAD_FAILED,
        )

    try:
        selection = select_profile_generation_stage(
            ratings_report=bundle.ratings_source if bundle.ratings is not None else None,
            public_draft_report=(
                bundle.public_draft_source if bundle.public_drafts is not None else None
            ),
            thresholds=thresholds,
        )
    except Exception:  # noqa: BLE001 - policy failures are finite and path-free
        return _failure(
            environment=environment,
            phase=ProfileGenerationFailurePhase.STAGE_SELECTION,
            reason=ProfileGenerationFailureReason.STAGE_SELECTION_FAILED,
        )

    diagnostics, total, omitted = _classify_cards(bundle.card_database, environment.set_code)

    try:
        generation = generate_set_profile(
            **bundle.generator_inputs(),
            stage=selection.stage,
            generated_at=generated_at,
            profile_version=profile_version,
            config=config,
        )
    except Exception:  # noqa: BLE001 - generation errors must not leak details
        return _failure(
            environment=environment,
            phase=ProfileGenerationFailurePhase.GENERATION,
            reason=ProfileGenerationFailureReason.GENERATION_FAILED,
            selection=selection,
            diagnostics=diagnostics,
            diagnostic_total=total,
            diagnostics_omitted=omitted,
        )

    try:
        validated = validate_profile_generation(
            generation=generation,
            set_code=environment.set_code.casefold(),
            event_format=environment.event_format.casefold(),
            stage=selection.stage.value,
        )
        if not isinstance(validated, ValidatedProfileGeneration):
            raise ProfileGenerationExecutionError("validator returned an invalid payload")
    except Exception:  # noqa: BLE001 - validator failures are finite and path-free
        return _failure(
            environment=environment,
            phase=ProfileGenerationFailurePhase.VALIDATION,
            reason=ProfileGenerationFailureReason.VALIDATION_FAILED,
            selection=selection,
            diagnostics=diagnostics,
            diagnostic_total=total,
            diagnostics_omitted=omitted,
        )

    report = generation.report
    try:
        input_sources = _project_input_sources(environment=environment, bundle=bundle)
    except Exception:  # noqa: BLE001 - malformed provenance becomes a bounded failure
        return _failure(
            environment=environment,
            phase=ProfileGenerationFailurePhase.VALIDATION,
            reason=ProfileGenerationFailureReason.VALIDATION_FAILED,
            selection=selection,
            diagnostics=diagnostics,
            diagnostic_total=total,
            diagnostics_omitted=omitted,
        )
    try:
        return ProfileGenerationEnvironmentResult(
            environment=environment,
            outcome=ProfileGenerationEnvironmentOutcome.PUBLICATION_ELIGIBLE,
            selection=selection,
            diagnostics=diagnostics,
            diagnostic_total=total,
            diagnostics_omitted=omitted,
            skip_count=sum(report.skip_reasons.values()),
            error_count=sum(report.error_reasons.values()),
            generation=generation,
            validated=validated,
            input_sources=input_sources,
        )
    except Exception:  # noqa: BLE001 - malformed success contracts become bounded failures
        return _failure(
            environment=environment,
            phase=ProfileGenerationFailurePhase.VALIDATION,
            reason=ProfileGenerationFailureReason.VALIDATION_FAILED,
            selection=selection,
            diagnostics=diagnostics,
            diagnostic_total=total,
            diagnostics_omitted=omitted,
        )


def _project_input_sources(*, environment: PlannedEnvironment, bundle: Any) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for role, source_report in (
        ("card_database", bundle.card_metadata),
        ("seventeen_lands_ratings", bundle.ratings_source),
        ("seventeen_lands_public_drafts", bundle.public_draft_source),
    ):
        if source_report is None:
            continue
        outcome = (
            source_report.outcome.value
            if isinstance(source_report.outcome, ProfileInputAcquisitionOutcome)
            else source_report.outcome
        )
        acquired_at = source_report.acquired_at
        rows.append(
            {
                "role": role,
                "name": source_report.source.name,
                "sha256": source_report.sha256 or "",
                "source_version": source_report.source_version or "",
                "requested_set": environment.set_code,
                "requested_format": environment.event_format or "",
                "source_format": source_report.source.event_format or "",
                "acquired_at": "" if acquired_at is None else _utc_timestamp(acquired_at),
                "outcome": outcome,
                "fallback_state": _fallback_state(outcome),
            }
        )
    return tuple(rows)


def _fallback_state(outcome: Any) -> str:
    if outcome == ProfileInputAcquisitionOutcome.STALE.value:
        return "verified-stale-cache"
    if outcome == ProfileInputAcquisitionOutcome.OFFLINE_REUSED.value:
        return "verified-offline-cache"
    return "none"


def _validate_input_source(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ProfileGenerationExecutionError("profile generation input source is invalid")
    try:
        if set(value) != _GENERATION_INPUT_SOURCE_KEYS:
            raise ProfileGenerationExecutionError("profile generation input source keys are invalid")
    except (TypeError, ValueError) as error:
        raise ProfileGenerationExecutionError("profile generation input source keys are invalid") from error

    fields = {
        key: _safe_input_source_text(value[key], key, allow_empty=key in {
            "sha256",
            "source_version",
            "requested_format",
            "source_format",
            "acquired_at",
        })
        for key in _GENERATION_INPUT_SOURCE_KEYS
    }
    if fields["role"] not in _GENERATION_INPUT_SOURCE_ROLES:
        raise ProfileGenerationExecutionError("profile generation input source role is invalid")
    if not fields["name"]:
        raise ProfileGenerationExecutionError("profile generation input source name is invalid")
    if not fields["requested_set"]:
        raise ProfileGenerationExecutionError("profile generation input source requested set is invalid")

    try:
        outcome = ProfileInputAcquisitionOutcome(fields["outcome"])
    except (TypeError, ValueError) as error:
        raise ProfileGenerationExecutionError("profile generation input source outcome is invalid") from error
    expected_fallback = _fallback_state(outcome.value)
    if fields["fallback_state"] not in _GENERATION_FALLBACK_STATES:
        raise ProfileGenerationExecutionError("profile generation input source fallback is invalid")
    if fields["fallback_state"] != expected_fallback:
        raise ProfileGenerationExecutionError("profile generation input source fallback does not match outcome")

    digest = fields["sha256"]
    if digest and _GENERATION_SHA256_PATTERN.fullmatch(digest) is None:
        raise ProfileGenerationExecutionError("profile generation input source digest is invalid")
    acquired_at = fields["acquired_at"]
    if acquired_at:
        fields["acquired_at"] = _utc_timestamp(acquired_at)

    unpinned_optional = (
        fields["role"]
        in {"seventeen_lands_ratings", "seventeen_lands_public_drafts"}
        and outcome
        in {
            ProfileInputAcquisitionOutcome.MISSING,
            ProfileInputAcquisitionOutcome.CORRUPT,
            ProfileInputAcquisitionOutcome.UNAVAILABLE,
        }
    )
    if unpinned_optional:
        if digest or fields["source_version"] or acquired_at:
            raise ProfileGenerationExecutionError(
                "unavailable optional provenance must have empty pins and timestamp"
            )
    elif not acquired_at:
        raise ProfileGenerationExecutionError("profile generation input source timestamp is required")
    return fields


def _safe_input_source_text(value: Any, field_name: str, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ProfileGenerationExecutionError(f"profile generation input source {field_name} is invalid")
    if not allow_empty and not value:
        raise ProfileGenerationExecutionError(f"profile generation input source {field_name} is invalid")
    if len(value) > _MAX_INPUT_SOURCE_FIELD_LENGTH or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ) or "/" in value or "\\" in value or "://" in value:
        raise ProfileGenerationExecutionError(f"profile generation input source {field_name} is invalid")
    return value


def _utc_timestamp(value: Any) -> str:
    if not isinstance(value, datetime) and not isinstance(value, str):
        raise ProfileGenerationExecutionError("profile generation input source timestamp is invalid")
    try:
        timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ProfileGenerationExecutionError("profile generation input source timestamp is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ProfileGenerationExecutionError("profile generation input source timestamp is invalid")
    return timestamp.astimezone(UTC).isoformat()

def _failure(
    environment: PlannedEnvironment,
    phase: ProfileGenerationFailurePhase,
    reason: ProfileGenerationFailureReason,
    selection: ProfileGenerationStageSelection | None = None,
    diagnostics: tuple[ProfileGenerationDiagnostic, ...] = (),
    diagnostic_total: int = 0,
    diagnostics_omitted: int = 0,
) -> ProfileGenerationEnvironmentResult:
    return ProfileGenerationEnvironmentResult(
        environment=environment,
        outcome=ProfileGenerationEnvironmentOutcome.FAILED,
        selection=selection,
        diagnostics=diagnostics,
        diagnostic_total=diagnostic_total,
        diagnostics_omitted=diagnostics_omitted,
        failure_phase=phase,
        failure_reason=reason,
    )


def _classify_cards(
    card_database: Any,
    set_code: str,
) -> tuple[tuple[ProfileGenerationDiagnostic, ...], int, int]:
    requested_set_code = set_code.casefold()
    values: list[ProfileGenerationDiagnostic] = []
    classifier = RoleClassifier()
    try:
        cards = sorted(card_database.cards.values(), key=lambda card: (card.oracle_id or "", card.grp_id))
    except Exception:  # noqa: BLE001 - malformed loaded data is handled as a gap
        return (), 0, 0

    for card in cards:
        try:
            if card.unknown or (card.set_code is not None and card.set_code.casefold() != requested_set_code):
                continue
            candidate = card
            if card.set_code is None:
                # Keep this in sync with the generator's role compiler: cards
                # without set metadata are classified as members of the
                # requested set without mutating the loaded database.
                candidate = replace(card, set_code=requested_set_code)
            result = classifier.classify(candidate)

        except Exception:  # noqa: BLE001 - classifier failures become fixed diagnostics
            values.append(
                ProfileGenerationDiagnostic(
                    card_key=_card_key_safe(card),
                    card_name=_card_name_safe(card),
                    mechanic=None,
                    reason=CLASSIFICATION_ERROR_REASON,
                )
            )
            continue

        if result.unknown_reports:
            try:
                for report in result.unknown_reports:
                    values.append(
                        ProfileGenerationDiagnostic(
                            card_key=report.card_key,
                            card_name=report.card_name,
                            mechanic=report.mechanic,
                            reason=report.reason,
                        )
                    )
            except Exception:  # noqa: BLE001 - malformed report becomes a fixed diagnostic
                values.append(
                    ProfileGenerationDiagnostic(
                        card_key=_card_key_safe(card),
                        card_name=_card_name_safe(card),
                        mechanic=None,
                        reason=CLASSIFICATION_ERROR_REASON,
                    )
                )
        elif not result.assignments:
            values.append(
                ProfileGenerationDiagnostic(
                    card_key=result.card_key,
                    card_name=result.card_name,
                    mechanic=None,
                    reason=CLASSIFICATION_GAP_REASON,
                )
            )


    unique = tuple(sorted(set(values), key=_diagnostic_sort_key))
    total = len(unique)
    retained = unique[:_MAX_DIAGNOSTICS]
    return retained, total, total - len(retained)

def _diagnostic_sort_key(value: ProfileGenerationDiagnostic) -> tuple[str, ...]:
    # Case-insensitive ordering is operator-friendly; raw values make ties total.
    folded = (
        value.card_key.casefold(),
        "" if value.card_name is None else value.card_name.casefold(),
        "" if value.mechanic is None else value.mechanic.casefold(),
        value.reason.casefold(),
    )
    raw = (
        value.card_key,
        "" if value.card_name is None else value.card_name,
        "" if value.mechanic is None else value.mechanic,
        value.reason,
    )
    return folded + raw


def _card_key_safe(card: Any) -> str:
    oracle_id = getattr(card, "oracle_id", None)
    if isinstance(oracle_id, str) and oracle_id:
        return f"oracle_id:{oracle_id}".casefold()
    set_code = getattr(card, "set_code", None)
    collector_number = getattr(card, "collector_number", None)
    if isinstance(set_code, str) and set_code and isinstance(collector_number, str) and collector_number:
        return f"set:{set_code}:{collector_number}".casefold()
    arena_id = getattr(card, "arena_id", None)
    if isinstance(arena_id, int) and not isinstance(arena_id, bool):
        return f"arena_id:{arena_id}".casefold()
    grp_id = getattr(card, "grp_id", None)
    return f"grp_id:{grp_id}".casefold() if isinstance(grp_id, int) else "card:unknown"


def _card_name_safe(card: Any) -> str | None:
    name = getattr(card, "name", None)
    return name if isinstance(name, str) else None


def _bounded_required(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileGenerationExecutionError(f"diagnostic {field_name} is invalid")
    return value.strip()[:_MAX_DIAGNOSTIC_FIELD_LENGTH]


def _bounded_optional(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_MAX_DIAGNOSTIC_FIELD_LENGTH]


def _validate_count(value: Any, field_name: str, *, required: bool) -> None:
    if value is None:
        if required:
            raise ProfileGenerationExecutionError(f"{field_name} is required")
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfileGenerationExecutionError(f"{field_name} is invalid")


def _sha256_or_none(value: bytes | None) -> str | None:
    return None if value is None else hashlib.sha256(value).hexdigest()


def _size_or_none(value: bytes | None) -> int | None:
    return None if value is None else len(value)


__all__ = [
    "CLASSIFICATION_ERROR_REASON",
    "CLASSIFICATION_GAP_REASON",
    "PROFILE_GENERATION_EXECUTION_SCHEMA_VERSION",
    "ProfileGenerationDiagnostic",
    "ProfileGenerationEnvironmentOutcome",
    "ProfileGenerationEnvironmentResult",
    "ProfileGenerationExecutionError",
    "ProfileGenerationFailurePhase",
    "ProfileGenerationFailureReason",
    "generate_staged_environment_profile",
]
