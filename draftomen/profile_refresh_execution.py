"""Portable normalized-input staging for refresh plans.

This module deliberately stops at the input boundary.  It acquires normalized
inputs, writes immutable content-addressed objects and path-free authorities,
and provides a loader for the existing profile generator.  No profile is
selected, generated, or published here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping, TypeAlias

from draftomen.carddb import CardDatabase, CardDatabaseError
from draftomen.profile_input_acquisition import (
    CARD_METADATA_SOURCE_NAME,
    PUBLIC_DRAFT_ATTRIBUTION,
    PUBLIC_DRAFT_LICENSE,
    PUBLIC_DRAFT_SOURCE_NAME,
    RATINGS_SOURCE_NAME,
    CardMetadataAdapter,
    Clock,
    ProfileBuildBundle,
    ProfileInputAcquisitionOutcome,
    ProfileInputAcquisitionResult,
    ProfileInputSourceReport,
    SeventeenLandsPublicDraftAdapter,
    SeventeenLandsRatingsAdapter,
    acquire_profile_build_bundle,
)
from draftomen.profile_input_cache import (
    ProfileInputCache,
    ProfileInputCacheOutcome,
    ProfileInputCachePolicy,
    ProfileInputSource,
)
from draftomen.public_dump import (
    PublicDumpError,
    PublicDumpManifest,
    PublicDumpSource,
)
from draftomen.refresh_plan import PlannedEnvironment, RefreshPlan
from draftomen.seventeen import SeventeenLandsError, SeventeenLandsFormatData


PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION = 1
PROFILE_REFRESH_EXECUTOR_VERSION = "1"

DEFAULT_PROFILE_REFRESH_CACHE_POLICY = ProfileInputCachePolicy(
    freshness_ttl=timedelta(days=7),
    max_entry_bytes=128 * 1024 * 1024,
    max_total_bytes=512 * 1024 * 1024,
    max_records=256,
    max_versions_per_source=3,
)

PathInput: TypeAlias = str | os.PathLike[str]

_INPUT_CARD = "card_database"
_INPUT_RATINGS = "ratings"
_INPUT_DRAFTS = "public_drafts"
_INPUT_ROLES = (_INPUT_CARD, _INPUT_RATINGS, _INPUT_DRAFTS)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_DIAGNOSTICS = 64
_MAX_DIAGNOSTIC_LENGTH = 128
# Plan environment reasons keep the refresh-plan schema's bound, not the
# token bound: they may contain spaces and run up to 512 characters.
_MAX_REASON_LENGTH = 512
_MAX_SOURCE_VERSION_LENGTH = 256
_CHUNK_SIZE = 1024 * 1024


class ProfileRefreshExecutionError(RuntimeError):
    """Raised for invalid execution inputs or invalid staged authorities."""


class ProfileRefreshEnvironmentOutcome(str, Enum):
    """Bounded outcome labels for one planned environment."""

    STAGED = "staged"
    FAILED = "failed"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise ProfileRefreshExecutionError("could not canonicalize refresh authority") from error


def _canonical_model_bytes(value: Any) -> bytes:
    """Serialize a normalized model exactly as its current cache format does."""

    return _canonical_bytes(value.to_json())


@dataclass(frozen=True, slots=True)
class ProfileRefreshEnvironmentResult:
    """One immutable, path-free result for one planned environment."""

    environment: PlannedEnvironment
    plan_sha256: str
    mode: str
    bundle_id: str
    outcome: ProfileRefreshEnvironmentOutcome | str
    sources: tuple[ProfileInputSourceReport | None, ...] = ()
    skip_reasons: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    schema_version: int = PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.environment, PlannedEnvironment):
            raise ProfileRefreshExecutionError("environment must be a PlannedEnvironment")
        _valid_hash(self.plan_sha256, "plan SHA-256")
        mode = _mode_value(self.mode)
        _valid_hash(self.bundle_id, "bundle id")
        expected_bundle_id = _bundle_id(self.environment)
        if self.bundle_id.casefold() != expected_bundle_id:
            raise ProfileRefreshExecutionError("bundle id does not match environment")
        object.__setattr__(self, "plan_sha256", self.plan_sha256.casefold())
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "bundle_id", self.bundle_id.casefold())
        try:
            normalized = (
                self.outcome
                if isinstance(self.outcome, ProfileRefreshEnvironmentOutcome)
                else ProfileRefreshEnvironmentOutcome(self.outcome)
            )
        except (TypeError, ValueError) as error:
            raise ProfileRefreshExecutionError("environment outcome is invalid") from error
        object.__setattr__(self, "outcome", normalized)
        reports = tuple(self.sources)
        if len(reports) != len(_INPUT_ROLES):
            raise ProfileRefreshExecutionError("environment source reports are invalid")
        if any(
            report is None
            for role, report in zip(_INPUT_ROLES, reports, strict=True)
            if role != _INPUT_DRAFTS
        ):
            raise ProfileRefreshExecutionError("required environment source report is missing")
        if any(
            report is not None and not isinstance(report, ProfileInputSourceReport)
            for report in reports
        ):
            raise ProfileRefreshExecutionError("environment source reports are invalid")
        object.__setattr__(self, "sources", reports)
        object.__setattr__(self, "skip_reasons", _tokens(self.skip_reasons))
        object.__setattr__(self, "diagnostics", _tokens(self.diagnostics))
        if self.schema_version != PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION:
            raise ProfileRefreshExecutionError("unsupported refresh execution schema version")

    @property
    def staged(self) -> bool:
        return self.outcome is ProfileRefreshEnvironmentOutcome.STAGED

    @property
    def available_input_roles(self) -> tuple[str, ...]:
        return tuple(
            role
            for role, report in zip(_INPUT_ROLES, self.sources, strict=True)
            if report is not None
            and report.sha256 is not None
            and report.content_bytes is not None
        )

    @property
    def metadata_only(self) -> bool:
        return self.staged and self.available_input_roles == (_INPUT_CARD,)

    def to_json(self) -> dict[str, Any]:
        """Return this environment's logical execution entry."""
        return {
            "available_input_roles": list(self.available_input_roles),
            "bundle_id": self.bundle_id,
            "environment": _environment_json(self.environment),
            "outcome": self.outcome.value,
            "skip_reasons": _diagnostics(self.skip_reasons),
        }

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_json())

@dataclass(frozen=True, slots=True)
class ProfileRefreshExecutionResult:
    """Aggregate immutable, path-free execution result."""

    plan_sha256: str
    mode: str
    executed_at: datetime
    environments: tuple[ProfileRefreshEnvironmentResult, ...]
    diagnostics: tuple[str, ...] = ()
    schema_version: int = PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _valid_hash(self.plan_sha256, "plan SHA-256")
        mode = _mode_value(self.mode)
        object.__setattr__(self, "plan_sha256", self.plan_sha256.casefold())
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "executed_at", _timestamp(self.executed_at))
        environments = tuple(self.environments)
        if any(not isinstance(item, ProfileRefreshEnvironmentResult) for item in environments):
            raise ProfileRefreshExecutionError("execution environments are invalid")
        if any(item.mode != self.mode for item in environments):
            raise ProfileRefreshExecutionError("execution environment mode mismatch")
        if any(item.plan_sha256 != self.plan_sha256 for item in environments):
            raise ProfileRefreshExecutionError("execution environment plan SHA mismatch")
        object.__setattr__(self, "environments", environments)
        object.__setattr__(self, "diagnostics", _tokens(self.diagnostics))
        if self.schema_version != PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION:
            raise ProfileRefreshExecutionError("unsupported refresh execution schema version")

    @property
    def planned_count(self) -> int:
        return len(self.environments)

    @property
    def staged_count(self) -> int:
        return sum(item.staged for item in self.environments)

    @property
    def failed_count(self) -> int:
        return sum(not item.staged for item in self.environments)

    @property
    def metadata_only_count(self) -> int:
        return sum(item.metadata_only for item in self.environments)

    @property
    def succeeded(self) -> bool:
        return self.failed_count == 0

    def to_json(self) -> dict[str, Any]:
        return {
            "counts": {
                "failed": self.failed_count,
                "metadata_only": self.metadata_only_count,
                "planned": self.planned_count,
                "staged": self.staged_count,
            },
            "environments": [item.to_json() for item in self.environments],
            "executor_version": PROFILE_REFRESH_EXECUTOR_VERSION,
            "mode": self.mode,
            "plan_sha256": self.plan_sha256,
            "schema_version": self.schema_version,
        }


    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_json())


def execute_profile_refresh_plan(
    *,
    plan: RefreshPlan,
    cache: ProfileInputCache,
    output_dir: PathInput,
    offline: bool,
    card_metadata_adapter: CardMetadataAdapter | None = None,
    ratings_adapter: SeventeenLandsRatingsAdapter | None = None,
    public_draft_adapter: SeventeenLandsPublicDraftAdapter | None = None,
    clock: Clock | None = None,
    include_public_drafts: bool = True,
) -> ProfileRefreshExecutionResult:
    """Acquire and stage every plan environment sequentially, isolating
    failures per environment; no profile generator or publisher is called."""

    if not isinstance(include_public_drafts, bool):
        raise ProfileRefreshExecutionError("include_public_drafts must be a bool")
    normalized_plan, digest = _validate_execution_inputs(
        plan=plan,
        cache=cache,
        output_dir=output_dir,
        offline=offline,
        clock=clock,
    )
    output = _path(output_dir)
    _owned_directory(output, create=True)
    bundles = output / "bundles"
    _owned_directory(bundles, create=True)
    executed_at = _now(clock)
    mode = "offline" if offline else "online"
    records: list[ProfileRefreshEnvironmentResult] = []

    for environment in normalized_plan.environments:
        records.append(
            _execute_environment(
                environment=environment,
                plan_sha256=digest,
                mode=mode,
                cache=cache,
                output_dir=output,
                offline=offline,
                card_metadata_adapter=card_metadata_adapter,
                ratings_adapter=ratings_adapter,
                public_draft_adapter=public_draft_adapter,
                include_public_drafts=include_public_drafts,
                clock=clock,
            )
        )

    execution = ProfileRefreshExecutionResult(
        plan_sha256=digest,
        mode=mode,
        executed_at=executed_at,
        environments=tuple(records),
    )
    try:
        _atomic_write_bytes(output / "execution.json", execution.to_bytes())
    except Exception as error:  # noqa: BLE001 - never expose local details
        raise ProfileRefreshExecutionError("could not publish execution authority") from error
    return execution


def load_staged_profile_build_bundle(
    bundle_path: PathInput,
    *,
    environment: PlannedEnvironment | None = None,
    expected_plan_sha256: str | None = None,
) -> ProfileBuildBundle:
    """Strictly verify and load one moved staged bundle; public-draft paths
    are runtime-only and never read from authority JSON."""

    root = _path(bundle_path)
    _owned_directory(root)
    if root.name in {"", ".", ".."}:
        raise ProfileRefreshExecutionError("staged bundle directory is invalid")
    bundle_file = root / "bundle.json"
    objects = root / "objects"
    _owned_file(bundle_file)
    _owned_directory(objects)
    try:
        entries = {item.name for item in root.iterdir()}
    except OSError as error:
        raise ProfileRefreshExecutionError("could not inspect staged bundle") from error
    if entries != {"bundle.json", "objects"}:
        raise ProfileRefreshExecutionError("staged bundle layout is invalid")

    value = _parse_canonical_object(_read_bytes(bundle_file), "bundle authority")
    _keys(
        value,
        {
            "schema_version",
            "executor_version",
            "bundle_id",
            "plan_sha256",
            "mode",
            "environment",
            "outcome",
            "inputs",
            "sources",
            "skip_reasons",
        },
        "bundle authority",
    )
    if value["schema_version"] != PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION:
        raise ProfileRefreshExecutionError("unsupported bundle schema version")
    if value["executor_version"] != PROFILE_REFRESH_EXECUTOR_VERSION:
        raise ProfileRefreshExecutionError("unsupported executor version")
    plan_sha256 = _valid_hash(value["plan_sha256"], "plan SHA-256")
    if expected_plan_sha256 is not None and plan_sha256 != expected_plan_sha256:
        raise ProfileRefreshExecutionError("staged bundle does not match requested plan")
    mode = _mode_value(value["mode"])
    outcome = _enum_value(value["outcome"], ProfileRefreshEnvironmentOutcome)
    if outcome is not ProfileRefreshEnvironmentOutcome.STAGED:
        raise ProfileRefreshExecutionError("failed profile bundle cannot be loaded")
    parsed_environment = _parse_environment(value["environment"])
    if environment is not None and parsed_environment != environment:
        raise ProfileRefreshExecutionError("staged bundle environment does not match requested environment")
    bundle_id = _valid_hash(value["bundle_id"], "bundle id")
    if bundle_id != _bundle_id(parsed_environment) or root.name != bundle_id:
        raise ProfileRefreshExecutionError("staged bundle id is invalid")
    skip_reasons = _parse_diagnostics(value["skip_reasons"], "bundle skip reasons")

    inputs = value["inputs"]
    sources = value["sources"]
    if not isinstance(inputs, Mapping) or set(inputs) != set(_INPUT_ROLES):
        raise ProfileRefreshExecutionError("staged bundle inputs are invalid")
    if not isinstance(sources, Mapping) or set(sources) != set(_INPUT_ROLES):
        raise ProfileRefreshExecutionError("staged bundle source reports are invalid")
    for role in _INPUT_ROLES:
        input_value = inputs[role]
        if input_value is None:
            continue
        if not isinstance(input_value, Mapping):
            raise ProfileRefreshExecutionError("staged bundle input is invalid")
        expected_keys = (
            {"content_bytes", "sha256", "source_name"}
            if role != _INPUT_DRAFTS
            else {"attribution", "content_bytes", "license", "sha256", "source_name"}
        )
        _keys(input_value, expected_keys, "staged bundle input")
        source_name = _safe_source_name(input_value["source_name"])
        if source_name != input_value["source_name"]:
            raise ProfileRefreshExecutionError("staged input source name is not canonical")
        _valid_hash(input_value["sha256"], "staged object digest")
        _positive_count(input_value["content_bytes"], "staged object size")
        if role == _INPUT_DRAFTS:
            if (
                not isinstance(input_value["attribution"], str)
                or not input_value["attribution"].strip()
                or not isinstance(input_value["license"], str)
                or not input_value["license"].strip()
            ):
                raise ProfileRefreshExecutionError("public-draft attribution is invalid")
            _portable_provenance(input_value["attribution"], PUBLIC_DRAFT_ATTRIBUTION)
            _portable_provenance(input_value["license"], PUBLIC_DRAFT_LICENSE)
    _parse_diagnostics(value["skip_reasons"], "bundle skip reasons")

    # Only objects pinned by the authority are part of the reconstructed
    # bundle.  Unreferenced entries are inert and intentionally ignored; each
    # referenced object is checked for ownership, digest, and size below.

    reports = tuple(
        _parse_source_report(sources[role], role=role, environment=parsed_environment)
        for role in _INPUT_ROLES
    )
    for role, report in zip(_INPUT_ROLES, reports, strict=True):
        input_value = inputs[role]
        if report is None:
            if role != _INPUT_DRAFTS or input_value is not None:
                raise ProfileRefreshExecutionError("staged source report is missing")
            continue
        if input_value is None:
            if report.sha256 is not None or report.content_bytes is not None:
                raise ProfileRefreshExecutionError("missing input has a content pin")
        elif (
            report.source.name != input_value["source_name"]
            or report.sha256 != input_value["sha256"]
            or report.content_bytes != input_value["content_bytes"]
        ):
            raise ProfileRefreshExecutionError("input source pin does not match authority")

    card_report, ratings_report, drafts_report = reports
    if card_report is None or ratings_report is None:
        raise ProfileRefreshExecutionError("required staged source report is missing")
    card_input = inputs[_INPUT_CARD]
    if card_input is None:
        raise ProfileRefreshExecutionError("card database input is missing")
    card_database = _load_role_model(
        role=card_input,
        report=card_report,
        objects=objects,
        environment=parsed_environment,
        model="cards",
    )
    if card_report.card_count != len(card_database):
        raise ProfileRefreshExecutionError("card sample availability does not match object")

    ratings: SeventeenLandsFormatData | None = None
    ratings_input = inputs[_INPUT_RATINGS]
    if ratings_input is not None:
        ratings = _load_role_model(
            role=ratings_input,
            report=ratings_report,
            objects=objects,
            environment=parsed_environment,
            model="ratings",
        )
        if ratings_report.rating_samples != sum(
            row.sample_counts.games_in_hand for row in ratings.card_ratings.values()
        ):
            raise ProfileRefreshExecutionError("ratings sample availability does not match object")

    public_manifest: PublicDumpManifest | None = None
    drafts_input = inputs[_INPUT_DRAFTS]
    if drafts_input is not None:
        if drafts_report is None:
            raise ProfileRefreshExecutionError("public-draft source report is missing")
        digest, content_bytes = _verify_role_object(
            role=drafts_input, objects=objects, model="public drafts"
        )
        source = drafts_report.source
        public_manifest = PublicDumpManifest(
            sources=(
                PublicDumpSource(
                    name=source.name,
                    path=objects / f"{digest}.bin",
                    sha256=digest,
                    retrieved_at=(
                        None
                        if drafts_report.acquired_at is None
                        else drafts_report.acquired_at.astimezone(UTC).isoformat()
                    ),
                    attribution=drafts_input["attribution"],
                    license=drafts_input["license"],
                ),
            )
        )
        if drafts_report.content_bytes != content_bytes or drafts_report.sha256 != digest:
            raise ProfileRefreshExecutionError("public-draft source pin does not match object")

    try:
        return ProfileBuildBundle(
            environment=parsed_environment,
            card_database=card_database,
            card_metadata=card_report,
            ratings=ratings,
            ratings_source=ratings_report,
            public_drafts=public_manifest,
            public_draft_source=drafts_report,
        )
    except (TypeError, ValueError, CardDatabaseError, SeventeenLandsError, PublicDumpError) as error:
        raise ProfileRefreshExecutionError("staged profile input bundle is invalid") from error


def _failed_result(
    *,
    environment: PlannedEnvironment,
    plan_sha256: str,
    mode: str,
    bundle_dir: Path,
    reports: tuple[ProfileInputSourceReport | None, ...],
    skip_reasons: tuple[str, ...] | list[str],
    diagnostics: tuple[str, ...] | list[str],
) -> ProfileRefreshEnvironmentResult:
    failure_authority_diagnostics = _publish_failure_bundle_bounded(
        environment=environment,
        plan_sha256=plan_sha256,
        mode=mode,
        bundle_dir=bundle_dir,
        reports=reports,
        skip_reasons=list(skip_reasons),
    )
    return _result(
        environment=environment,
        plan_sha256=plan_sha256,
        mode=mode,
        outcome=ProfileRefreshEnvironmentOutcome.FAILED,
        sources=reports,
        skip_reasons=skip_reasons,
        diagnostics=(
            *diagnostics,
            "required-source-failed",
            *failure_authority_diagnostics,
        ),
    )


def _execute_environment(
    *,
    environment: PlannedEnvironment,
    plan_sha256: str,
    mode: str,
    cache: ProfileInputCache,
    output_dir: Path,
    offline: bool,
    card_metadata_adapter: CardMetadataAdapter | None,
    ratings_adapter: SeventeenLandsRatingsAdapter | None,
    public_draft_adapter: SeventeenLandsPublicDraftAdapter | None,
    clock: Clock | None,
    include_public_drafts: bool,
) -> ProfileRefreshEnvironmentResult:
    bundle_id = _bundle_id(environment)
    diagnostics: list[str] = []
    skip_reasons: list[str] = []
    acquisition: ProfileInputAcquisitionResult | None = None
    try:
        acquisition = acquire_profile_build_bundle(
            environment=environment,
            cache=cache,
            card_metadata_adapter=card_metadata_adapter,
            ratings_adapter=ratings_adapter,
            public_draft_adapter=public_draft_adapter,
            include_public_drafts=include_public_drafts,
            offline=offline,
            clock=clock,
        )
    except Exception:  # noqa: BLE001 - isolate one environment
        diagnostics.append("acquisition-failed")
        return _failed_result(
            environment=environment,
            plan_sha256=plan_sha256,
            mode=mode,
            bundle_dir=output_dir / "bundles" / bundle_id,
            reports=_failure_reports(environment, include_public_drafts=include_public_drafts),
            skip_reasons=(
                "card-database-unavailable",
                "17lands-ratings-unavailable",
                *(
                    ("17lands-public-drafts-unavailable",)
                    if include_public_drafts
                    else ()
                ),
            ),
            diagnostics=diagnostics,
        )

    skip_reasons.extend(acquisition.skip_reasons)
    reports = _acquisition_reports(
        acquisition, environment, include_public_drafts=include_public_drafts
    )
    ratings_report = reports[1]
    if ratings_report is not None and ratings_report.sha256 is None and not any(
        reason.startswith("17lands-ratings-") for reason in skip_reasons
    ):
        skip_reasons.append("17lands-ratings-unavailable")
    drafts_report = reports[2]
    if (
        drafts_report is not None
        and drafts_report.sha256 is None
        and not any(reason.startswith("17lands-public-drafts-") for reason in skip_reasons)
    ):
        skip_reasons.append("17lands-public-drafts-unavailable")
    if acquisition.bundle is None:
        skip_reasons.append("card-database-required")
        return _failed_result(
            environment=environment,
            plan_sha256=plan_sha256,
            mode=mode,
            bundle_dir=output_dir / "bundles" / bundle_id,
            reports=reports,
            skip_reasons=skip_reasons,
            diagnostics=diagnostics,
        )
    if acquisition.bundle.environment != environment:
        skip_reasons.append("bundle-environment-mismatch")
        return _failed_result(
            environment=environment,
            plan_sha256=plan_sha256,
            mode=mode,
            bundle_dir=output_dir / "bundles" / bundle_id,
            reports=reports,
            skip_reasons=skip_reasons,
            diagnostics=diagnostics,
        )

    bundle_dir = output_dir / "bundles" / bundle_id
    try:
        staged_reports, stage_skips, stage_diagnostics = _stage_bundle(
            bundle=acquisition.bundle,
            reports=reports,
            bundle_dir=bundle_dir,
            plan_sha256=plan_sha256,
            mode=mode,
            skip_reasons=skip_reasons,
        )
    except Exception:  # noqa: BLE001 - isolate one environment
        return _failed_result(
            environment=environment,
            plan_sha256=plan_sha256,
            mode=mode,
            bundle_dir=bundle_dir,
            reports=reports,
            skip_reasons=(*skip_reasons, "required-input-staging-failed"),
            diagnostics=diagnostics,
        )

    return _result(
        environment=environment,
        plan_sha256=plan_sha256,
        mode=mode,
        outcome=ProfileRefreshEnvironmentOutcome.STAGED,
        sources=staged_reports,
        skip_reasons=(*skip_reasons, *stage_skips),
        diagnostics=(*diagnostics, *stage_diagnostics),
    )


class _RequiredStageFailure(RuntimeError):
    pass


def _stage_bundle(
    *,
    bundle: ProfileBuildBundle,
    reports: tuple[ProfileInputSourceReport | None, ...],
    bundle_dir: Path,
    plan_sha256: str,
    mode: str,
    skip_reasons: list[str],
) -> tuple[
    tuple[ProfileInputSourceReport | None, ...], tuple[str, ...], tuple[str, ...]
]:
    parent = bundle_dir.parent
    _owned_directory(parent, create=True)
    if bundle_dir.exists() or bundle_dir.is_symlink():
        if bundle_dir.is_symlink() or not bundle_dir.is_dir():
            raise _RequiredStageFailure("bundle destination is invalid")

    card_report, ratings_report, drafts_report = reports
    if card_report is None or ratings_report is None:
        raise _RequiredStageFailure("required source report is missing")
    if bundle.public_drafts is not None and drafts_report is None:
        raise _RequiredStageFailure("public-draft source report is missing")
    temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_dir.name}.", dir=str(parent)))
    objects = temporary / "objects"
    objects.mkdir()
    adjusted = list(reports)
    inputs: dict[str, dict[str, Any] | None] = {role: None for role in _INPUT_ROLES}
    skips = list(skip_reasons)
    diagnostics: list[str] = []
    try:
        card_digest, card_size = _write_bytes_object(objects, _canonical_model_bytes(bundle.card_database))
        inputs[_INPUT_CARD] = {
            "content_bytes": card_size,
            "sha256": card_digest,
            "source_name": _safe_source_name(card_report.source.name),
        }
        adjusted[0] = _reconcile_report(
            card_report, digest=card_digest, content_bytes=card_size
        )
        if bundle.ratings is None and (
            ratings_report.sha256 is not None or ratings_report.content_bytes is not None
        ):
            skips.append("17lands-ratings-unavailable")
            adjusted[1] = _optional_failure_report(
                ratings_report, "17lands-ratings-unavailable"
            )
        if drafts_report is not None and bundle.public_drafts is None and (
            drafts_report.sha256 is not None or drafts_report.content_bytes is not None
        ):
            skips.append("17lands-public-drafts-unavailable")
            adjusted[2] = _optional_failure_report(
                drafts_report, "17lands-public-drafts-unavailable"
            )
        if bundle.ratings is not None:
            try:
                ratings_digest, ratings_size = _write_bytes_object(
                    objects, _canonical_model_bytes(bundle.ratings)
                )
                inputs[_INPUT_RATINGS] = {
                    "content_bytes": ratings_size,
                    "sha256": ratings_digest,
                    "source_name": _safe_source_name(ratings_report.source.name),
                }
                adjusted[1] = _reconcile_report(
                    ratings_report, digest=ratings_digest, content_bytes=ratings_size
                )
            except Exception:  # noqa: BLE001 - optional role is isolated
                skips.append("17lands-ratings-staging-failed")
                diagnostics.append("ratings-input-unavailable")
                adjusted[1] = _optional_failure_report(
                    ratings_report, "17lands-ratings-staging-failed"
                )
        if bundle.public_drafts is not None:
            try:
                source = bundle.public_drafts.sources[0]
                source_path = Path(source.path) if source.path is not None else None
                if source_path is None:
                    raise OSError
                drafts_digest, drafts_size = _write_stream_object(objects, source_path)
                if source.sha256 != drafts_digest:
                    raise OSError
                inputs[_INPUT_DRAFTS] = {
                    "attribution": _portable_provenance(
                        source.attribution, PUBLIC_DRAFT_ATTRIBUTION
                    ),
                    "content_bytes": drafts_size,
                    "license": _portable_provenance(source.license, PUBLIC_DRAFT_LICENSE),
                    "sha256": drafts_digest,
                    "source_name": _safe_source_name(drafts_report.source.name),
                }
                adjusted[2] = _reconcile_report(
                    drafts_report, digest=drafts_digest, content_bytes=drafts_size
                )
            except Exception:  # noqa: BLE001 - optional role is isolated
                skips.append("17lands-public-drafts-staging-failed")
                diagnostics.append("public-drafts-input-unavailable")
                adjusted[2] = _optional_failure_report(
                    drafts_report, "17lands-public-drafts-staging-failed"
                )

        _prune_unreferenced_objects(objects, inputs)
        authority = _bundle_json(
            environment=bundle.environment,
            bundle_id=_bundle_id(bundle.environment),
            plan_sha256=plan_sha256,
            mode=mode,
            outcome=ProfileRefreshEnvironmentOutcome.STAGED,
            reports=tuple(adjusted),
            inputs=inputs,
            skip_reasons=skips,
        )
        _atomic_write_bytes(temporary / "bundle.json", _canonical_bytes(authority))
        _replace_bundle_directory(temporary, bundle_dir)
        temporary = Path()
    except _RequiredStageFailure:
        raise
    except Exception as error:  # noqa: BLE001 - required role must fail in isolation
        raise _RequiredStageFailure("could not commit input bundle") from error
    finally:
        if str(temporary) not in {"", "."}:
            shutil.rmtree(temporary, ignore_errors=True)

    return tuple(adjusted), tuple(skips), tuple(diagnostics)

def _prune_unreferenced_objects(
    objects: Path, inputs: Mapping[str, Mapping[str, Any] | None]
) -> None:
    referenced_names = {
        f"{input_value['sha256']}.bin"
        for input_value in inputs.values()
        if input_value is not None
    }
    for path in objects.iterdir():
        if path.name in referenced_names:
            continue
        _owned_file(path)
        path.unlink()

def _replace_bundle_directory(temporary: Path, destination: Path) -> None:
    """Atomically install a complete prepared bundle over the current one."""
    if not destination.exists() and not destination.is_symlink():
        os.replace(temporary, destination)
        return
    backup = Path(tempfile.mkdtemp(prefix=f".{destination.name}.old-", dir=str(destination.parent)))
    os.rmdir(backup)
    os.replace(destination, backup)
    try:
        os.replace(temporary, destination)
    except Exception:
        # If the restore also fails, the backup is the only surviving copy of
        # the prior bundle and must be preserved on disk.
        os.replace(backup, destination)
        raise
    if backup.exists() and not backup.is_symlink():
        shutil.rmtree(backup, ignore_errors=True)



def _publish_failure_bundle(
    *,
    environment: PlannedEnvironment,
    plan_sha256: str,
    mode: str,
    bundle_dir: Path,
    reports: tuple[ProfileInputSourceReport | None, ...],
    skip_reasons: list[str],
) -> None:
    parent = bundle_dir.parent
    _owned_directory(parent, create=True)
    authority = _canonical_bytes(
        _bundle_json(
            environment=environment,
            bundle_id=_bundle_id(environment),
            plan_sha256=plan_sha256,
            mode=mode,
            outcome=ProfileRefreshEnvironmentOutcome.FAILED,
            reports=reports,
            inputs={role: None for role in _INPUT_ROLES},
            skip_reasons=skip_reasons,
        )
    )
    if bundle_dir.exists() or bundle_dir.is_symlink():
        if bundle_dir.is_symlink() or not bundle_dir.is_dir():
            raise ProfileRefreshExecutionError("failed bundle destination is invalid")
        _atomic_write_bytes(bundle_dir / "bundle.json", authority)
        return
    temporary = Path(tempfile.mkdtemp(prefix=f".{bundle_dir.name}.", dir=str(parent)))
    try:
        (temporary / "objects").mkdir()
        _atomic_write_bytes(temporary / "bundle.json", authority)
        os.replace(temporary, bundle_dir)
        temporary = Path()
    except OSError as error:
        raise ProfileRefreshExecutionError("could not publish failed bundle authority") from error
    finally:
        if str(temporary) not in {"", "."}:
            shutil.rmtree(temporary, ignore_errors=True)



def _publish_failure_bundle_bounded(
    *,
    environment: PlannedEnvironment,
    plan_sha256: str,
    mode: str,
    bundle_dir: Path,
    reports: tuple[ProfileInputSourceReport | None, ...],
    skip_reasons: list[str],
) -> tuple[str, ...]:
    try:
        _publish_failure_bundle(
            environment=environment,
            plan_sha256=plan_sha256,
            mode=mode,
            bundle_dir=bundle_dir,
            reports=reports,
            skip_reasons=skip_reasons,
        )
    except Exception:  # noqa: BLE001 - isolate failed-authority publication
        return ("failure-authority-write-failed",)
    return ()


def _bundle_json(
    *,
    environment: PlannedEnvironment,
    bundle_id: str,
    plan_sha256: str,
    mode: str,
    outcome: ProfileRefreshEnvironmentOutcome,
    reports: tuple[ProfileInputSourceReport | None, ...],
    inputs: Mapping[str, Mapping[str, Any] | None],
    skip_reasons: list[str],
) -> dict[str, Any]:
    report_values = {
        role: _source_report_json(report)
        for role, report in zip(_INPUT_ROLES, reports, strict=False)
        if report is not None
    }
    for role in _INPUT_ROLES:
        report_values.setdefault(role, None)
    input_values = {role: inputs.get(role) for role in _INPUT_ROLES}
    return {
        "environment": _environment_json(environment),
        "executor_version": PROFILE_REFRESH_EXECUTOR_VERSION,
        "inputs": input_values,
        "mode": mode,
        "outcome": outcome.value,
        "plan_sha256": plan_sha256,
        "schema_version": PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION,
        "skip_reasons": _diagnostics(skip_reasons),
        "sources": report_values,
        "bundle_id": bundle_id,
    }


def _result(
    *,
    environment: PlannedEnvironment,
    plan_sha256: str,
    mode: str,
    outcome: ProfileRefreshEnvironmentOutcome,
    sources: tuple[ProfileInputSourceReport | None, ...],
    skip_reasons: tuple[str, ...] | list[str],
    diagnostics: tuple[str, ...] | list[str],
) -> ProfileRefreshEnvironmentResult:
    return ProfileRefreshEnvironmentResult(
        environment=environment,
        plan_sha256=plan_sha256,
        mode=mode,
        bundle_id=_bundle_id(environment),
        outcome=outcome,
        sources=sources,
        skip_reasons=tuple(skip_reasons),
        diagnostics=tuple(diagnostics),
    )


def _acquisition_reports(
    acquisition: ProfileInputAcquisitionResult,
    environment: PlannedEnvironment,
    *,
    include_public_drafts: bool,
) -> tuple[ProfileInputSourceReport | None, ...]:
    card = acquisition.source
    ratings = acquisition.ratings_source
    drafts = acquisition.public_draft_source
    if not isinstance(card, ProfileInputSourceReport):
        return _failure_reports(environment, include_public_drafts=include_public_drafts)
    if ratings is None:
        ratings = _unavailable_report(
            ProfileInputSource(
                name=RATINGS_SOURCE_NAME,
                set_code=environment.set_code,
                event_format=environment.event_format,
            ),
            "17lands-ratings-unavailable",
        )
    if include_public_drafts and drafts is None:
        drafts = _unavailable_report(
            ProfileInputSource(
                name=PUBLIC_DRAFT_SOURCE_NAME,
                set_code=environment.set_code,
                event_format=environment.event_format,
            ),
            "17lands-public-drafts-unavailable",
        )
    return (card, ratings, drafts if include_public_drafts else None)


def _failure_reports(
    environment: PlannedEnvironment, *, include_public_drafts: bool
) -> tuple[ProfileInputSourceReport | None, ...]:
    drafts: ProfileInputSourceReport | None = (
        _unavailable_report(
            ProfileInputSource(
                name=PUBLIC_DRAFT_SOURCE_NAME,
                set_code=environment.set_code,
                event_format=environment.event_format,
            ),
            "17lands-public-drafts-unavailable",
        )
        if include_public_drafts
        else None
    )
    return (
        _unavailable_report(
            ProfileInputSource(name=CARD_METADATA_SOURCE_NAME, set_code=environment.set_code),
            "card-database-unavailable",
            card=True,
        ),
        _unavailable_report(
            ProfileInputSource(
                name=RATINGS_SOURCE_NAME,
                set_code=environment.set_code,
                event_format=environment.event_format,
            ),
            "17lands-ratings-unavailable",
        ),
        drafts,
    )




def _unavailable_report(
    source: ProfileInputSource,
    reason: str,
    *,
    card: bool = False,
) -> ProfileInputSourceReport:
    values: dict[str, Any] = {"card_count": 0}
    if source.name == RATINGS_SOURCE_NAME:
        values = {"rating_rows": 0, "rating_samples": 0}
    elif source.name == PUBLIC_DRAFT_SOURCE_NAME:
        values = {"draft_rows": 0}
    return ProfileInputSourceReport(
        source=source,
        outcome=ProfileInputAcquisitionOutcome.UNAVAILABLE,
        cache_lookup_outcome=None,
        cache_store_outcome=None,
        diagnostics=(reason,),
        **values,
    )


def _reconcile_report(
    report: ProfileInputSourceReport,
    *,
    digest: str,
    content_bytes: int,
) -> ProfileInputSourceReport:
    diagnostics = list(report.diagnostics)
    if report.sha256 is not None and (
        not isinstance(report.sha256, str) or report.sha256.casefold() != digest
    ):
        diagnostics.append("source-digest-reconciled")
    if report.content_bytes is not None and (
        not isinstance(report.content_bytes, int) or report.content_bytes != content_bytes
    ):
        diagnostics.append("source-size-reconciled")
    return replace(report, sha256=digest, content_bytes=content_bytes, diagnostics=tuple(diagnostics))
def _optional_failure_report(
    report: ProfileInputSourceReport, reason: str
) -> ProfileInputSourceReport:
    return replace(
        report,
        outcome=ProfileInputAcquisitionOutcome.UNAVAILABLE,
        source_version=None,
        acquired_at=None,
        sha256=None,
        content_bytes=None,
        diagnostics=(*report.diagnostics, reason),
    )

def _write_bytes_object(objects: Path, payload: bytes) -> tuple[str, int]:
    return _write_stream_object(objects, BytesIO(payload))


def _write_stream_object(objects: Path, source: Path | Any) -> tuple[str, int]:
    if isinstance(source, Path):
        _owned_file(source)
        stream = source.open("rb")
        close = True
    else:
        stream = source
        close = False
    temporary_name: str | None = None
    digest = sha256()
    content_bytes = 0
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=objects, prefix=".object.", delete=False) as output:
            temporary_name = output.name
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if chunk == b"":
                    break
                if not isinstance(chunk, bytes):
                    raise OSError
                digest.update(chunk)
                content_bytes += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        value = digest.hexdigest()
        destination = objects / f"{value}.bin"
        if destination.exists() or destination.is_symlink():
            _owned_file(destination)
            if _file_digest_size(destination) != (value, content_bytes):
                raise OSError
            os.unlink(temporary_name)
            temporary_name = None
        else:
            os.replace(temporary_name, destination)
            temporary_name = None
        return value, content_bytes
    finally:
        if close:
            stream.close()
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _load_role_model(
    *,
    role: Any,
    report: ProfileInputSourceReport | None,
    objects: Path,
    environment: PlannedEnvironment,
    model: str,
) -> Any:
    if report is None:
        raise ProfileRefreshExecutionError(f"{model} source report is missing")
    path, expected_digest, expected_bytes = _role_object_reference(role=role, objects=objects, model=model)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ProfileRefreshExecutionError("could not read staged object") from error
    if sha256(payload).hexdigest() != expected_digest or len(payload) != expected_bytes:
        raise ProfileRefreshExecutionError(f"{model} object checksum is invalid")
    if report.sha256 != expected_digest or report.content_bytes != expected_bytes:
        raise ProfileRefreshExecutionError(f"{model} source pin does not match object")
    try:
        value = _parse_canonical_object(payload, f"{model} object")
        if model == "cards":
            parsed = CardDatabase.from_json(data=value)
        elif model == "ratings":
            parsed = SeventeenLandsFormatData.from_json(data=value)
        else:
            raise ValueError
    except (CardDatabaseError, SeventeenLandsError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileRefreshExecutionError(f"{model} object is not canonical") from error
    if model == "cards" and not isinstance(parsed, CardDatabase):
        raise ProfileRefreshExecutionError("card object is invalid")
    if model == "ratings":
        if not isinstance(parsed, SeventeenLandsFormatData):
            raise ProfileRefreshExecutionError("ratings object is invalid")
        if (
            parsed.set_code.casefold() != environment.set_code.casefold()
            or parsed.event_format.casefold() != environment.event_format.casefold()
        ):
            raise ProfileRefreshExecutionError("ratings object environment is invalid")
    return parsed


def _role_object_reference(*, role: Any, objects: Path, model: str) -> tuple[Path, str, int]:
    if not isinstance(role, Mapping) or not {"content_bytes", "sha256"} <= set(role):
        raise ProfileRefreshExecutionError(f"{model} input authority is invalid")
    digest = role["sha256"]
    _valid_hash(digest, f"{model} object digest")
    content_bytes = role["content_bytes"]
    _positive_count(content_bytes, f"{model} object size")
    path = objects / f"{digest.casefold()}.bin"
    _owned_file(path)
    return path, digest.casefold(), content_bytes


def _verify_role_object(*, role: Any, objects: Path, model: str) -> tuple[str, int]:
    path, expected_digest, expected_bytes = _role_object_reference(role=role, objects=objects, model=model)
    actual_digest, actual_size = _file_digest_size(path)
    if actual_digest != expected_digest or actual_size != expected_bytes:
        raise ProfileRefreshExecutionError(f"{model} object checksum is invalid")
    return actual_digest, actual_size


def _parse_source_report(value: Any, *, role: str, environment: PlannedEnvironment) -> ProfileInputSourceReport | None:
    if not isinstance(value, Mapping):
        if value is None:
            return None
        raise ProfileRefreshExecutionError("source report is invalid")
    expected = {
        "acquired_at",
        "acquisition_outcome",
        "cache_lookup_outcome",
        "cache_store_outcome",
        "content_bytes",
        "diagnostics",
        "sample_availability",
        "sha256",
        "source",
        "source_version",
    }
    _keys(value, expected, "source report")
    source_value = value["source"]
    if not isinstance(source_value, Mapping):
        raise ProfileRefreshExecutionError("source identity is invalid")
    try:
        source = ProfileInputSource.from_json(source_value)
    except Exception as error:  # noqa: BLE001 - normalize cache parser errors
        raise ProfileRefreshExecutionError("source identity is invalid") from error
    if role not in _INPUT_ROLES:
        raise ProfileRefreshExecutionError("source role is invalid")
    expected_event_format = None if role == _INPUT_CARD else environment.event_format
    if (
        source.set_code != environment.set_code
        or source.event_format != expected_event_format
    ):
        raise ProfileRefreshExecutionError("source identity does not match environment")
    outcome = _enum_value(value["acquisition_outcome"], ProfileInputAcquisitionOutcome)
    lookup = _optional_enum_value(value["cache_lookup_outcome"], ProfileInputCacheOutcome)
    store = _optional_enum_value(value["cache_store_outcome"], ProfileInputCacheOutcome)
    acquired_at = None if value["acquired_at"] is None else _timestamp_from_json(value["acquired_at"])
    source_version = value["source_version"]
    if source_version is not None:
        _safe_source_version(source_version)
    diagnostics = _parse_diagnostics(value["diagnostics"], "source diagnostics")
    sample = value["sample_availability"]
    if not isinstance(sample, Mapping):
        raise ProfileRefreshExecutionError("source sample availability is invalid")
    if role == _INPUT_CARD:
        _keys(sample, {"card_count"}, "card sample availability")
        report_values = {"card_count": _count(sample["card_count"])}
    elif role == _INPUT_RATINGS:
        _keys(sample, {"rating_rows", "rating_samples"}, "ratings sample availability")
        report_values = {
            "rating_rows": _count(sample["rating_rows"]),
            "rating_samples": _count(sample["rating_samples"]),
        }
    else:
        _keys(sample, {"draft_rows"}, "draft sample availability")
        report_values = {"draft_rows": _count(sample["draft_rows"])}
    report_digest = value["sha256"]
    if report_digest is not None:
        report_digest = _valid_hash(report_digest, "source report digest")
    report_size = value["content_bytes"]
    if report_size is not None:
        report_size = _count(report_size)
    if (report_digest is None) != (report_size is None):
        raise ProfileRefreshExecutionError("source report content pin is invalid")
    report = ProfileInputSourceReport(
        source=source,
        outcome=outcome,
        cache_lookup_outcome=lookup,
        cache_store_outcome=store,
        source_version=source_version,
        acquired_at=acquired_at,
        sha256=report_digest,
        content_bytes=report_size,
        diagnostics=diagnostics,
        **report_values,
    )
    if _source_report_json(report) != dict(value):
        raise ProfileRefreshExecutionError("source report is not canonical")
    return report

def _mode_value(value: Any) -> str:
    if not isinstance(value, str) or value not in {"online", "offline"}:
        raise ProfileRefreshExecutionError("refresh execution mode is invalid")
    return value



def _source_report_json(report: ProfileInputSourceReport) -> dict[str, Any]:
    source_version = _serialized_source_version(report.source_version)
    acquired_at = None if report.acquired_at is None else _timestamp(report.acquired_at).isoformat()
    if report.draft_rows is not None:
        sample = {"draft_rows": _count(report.draft_rows)}
    elif report.rating_rows is not None:
        sample = {"rating_rows": _count(report.rating_rows), "rating_samples": _count(report.rating_samples or 0)}
    else:
        sample = {"card_count": _count(report.card_count)}
    return {
        "acquired_at": acquired_at,
        "acquisition_outcome": _enum_value(report.outcome, ProfileInputAcquisitionOutcome).value,
        "cache_lookup_outcome": _optional_enum_value(report.cache_lookup_outcome, ProfileInputCacheOutcome),
        "cache_store_outcome": _optional_enum_value(report.cache_store_outcome, ProfileInputCacheOutcome),
        "content_bytes": report.content_bytes,
        "diagnostics": _diagnostics(report.diagnostics),
        "sample_availability": sample,
        "sha256": _serialized_digest(report.sha256),
        "source": _source_identity_json(report.source),
        "source_version": source_version,
    }


def _source_identity_json(source: ProfileInputSource) -> dict[str, str | None]:
    name = _token(source.name) or "source"
    return {"event_format": source.event_format, "name": name, "set_code": source.set_code}


def _environment_json(environment: PlannedEnvironment) -> dict[str, Any]:
    # Reasons are recorded verbatim: PlannedEnvironment already guarantees a
    # non-empty, stripped, sorted tuple, and rewriting them breaks the
    # environment match when the staged bundle is reloaded against the plan.
    return {
        "event_format": environment.event_format,
        "lifecycle": environment.lifecycle,
        "reasons": list(environment.reasons),
        "set_code": environment.set_code,
    }


def _parse_environment(value: Any) -> PlannedEnvironment:
    if not isinstance(value, Mapping):
        raise ProfileRefreshExecutionError("bundle environment is invalid")
    _keys(value, {"event_format", "lifecycle", "reasons", "set_code"}, "bundle environment")
    reasons_value = value["reasons"]
    if not isinstance(reasons_value, list) or not reasons_value:
        raise ProfileRefreshExecutionError("bundle environment is invalid")
    for reason in reasons_value:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _MAX_REASON_LENGTH:
            raise ProfileRefreshExecutionError("bundle environment is invalid")
        if any(ord(character) < 32 or ord(character) == 127 for character in reason):
            raise ProfileRefreshExecutionError("bundle environment is invalid")
    try:
        parsed = PlannedEnvironment(
            set_code=value["set_code"],
            event_format=value["event_format"],
            lifecycle=value["lifecycle"],
            reasons=tuple(value["reasons"]),
        )
        if _environment_json(parsed) != dict(value):
            raise ProfileRefreshExecutionError("bundle environment is not canonical")
        return parsed
    except (TypeError, ValueError) as error:
        raise ProfileRefreshExecutionError("bundle environment is invalid") from error


def _validate_execution_inputs(
    *,
    plan: RefreshPlan,
    cache: ProfileInputCache,
    output_dir: PathInput,
    offline: bool,
    clock: Clock | None,
) -> tuple[RefreshPlan, str]:
    if not isinstance(plan, RefreshPlan):
        raise ProfileRefreshExecutionError("plan must be a canonical RefreshPlan")
    if not isinstance(cache, ProfileInputCache):
        raise ProfileRefreshExecutionError("cache must be a ProfileInputCache")
    _path(output_dir)
    if not isinstance(offline, bool):
        raise ProfileRefreshExecutionError("offline must be a boolean")
    if clock is not None and not callable(clock):
        raise ProfileRefreshExecutionError("clock must be callable")
    if not plan.environments:
        raise ProfileRefreshExecutionError("plan contains no environments")
    identities: set[tuple[str, str]] = set()
    for environment in plan.environments:
        if not isinstance(environment, PlannedEnvironment):
            raise ProfileRefreshExecutionError("plan contains an invalid environment")
        identity = (environment.set_code.casefold(), environment.event_format.casefold())
        if identity in identities:
            raise ProfileRefreshExecutionError("plan contains duplicate environment identities")
        identities.add(identity)
    try:
        digest = sha256(plan.to_bytes()).hexdigest()
    except Exception as error:  # noqa: BLE001 - canonical plan failure
        raise ProfileRefreshExecutionError("could not hash refresh plan") from error
    return plan, digest


def _bundle_id(environment: PlannedEnvironment) -> str:
    return sha256(
        _canonical_bytes({"event_format": environment.event_format, "set_code": environment.set_code})
    ).hexdigest()


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProfileRefreshExecutionError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_from_json(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ProfileRefreshExecutionError("authority timestamp is invalid")
    try:
        return _timestamp(datetime.fromisoformat(value))
    except (TypeError, ValueError) as error:
        raise ProfileRefreshExecutionError("authority timestamp is invalid") from error


def _now(clock: Clock | None) -> datetime:
    return _timestamp(datetime.now(tz=UTC) if clock is None else clock())


def _serialized_digest(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _valid_hash(value, "source report digest")
    except ProfileRefreshExecutionError:
        return None


def _valid_hash(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value.casefold()) is None:
        raise ProfileRefreshExecutionError(f"{field_name} is invalid")
    return value.casefold()


def _safe_source_version(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_SOURCE_VERSION_LENGTH:
        raise ProfileRefreshExecutionError("source version is invalid")
    if any(character in value for character in ("/", "\\", "\x00", "\n", "\r", ":", "?", "#")):
        raise ProfileRefreshExecutionError("source version is invalid")
    return value
def _portable_provenance(value: Any, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        value = fallback
    if (
        len(value) > _MAX_SOURCE_VERSION_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "/" in value
        or "\\" in value
        or "http" in value.casefold()
        or "@" in value
    ):
        raise ProfileRefreshExecutionError("public-draft provenance is invalid")
    return value


def _token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (
        not value
        or len(value) > _MAX_DIAGNOSTIC_LENGTH
        or any(character in value for character in ("/", "\\", "\x00", "\n", "\r", "http", "@"))
        or any(character.isspace() for character in value)
    ):
        return None
    return value


def _safe_source_name(value: Any) -> str:
    name = _token(value)
    if name is None:
        raise ProfileRefreshExecutionError("source name is invalid")
    return name


def _tokens(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    try:
        result = [_token(value) for value in values]
    except TypeError:
        return ()
    return tuple(dict.fromkeys(value for value in result if value is not None))


def _diagnostics(values: Any) -> list[str]:
    return list(_tokens(values)[:_MAX_DIAGNOSTICS])


def _parse_diagnostics(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProfileRefreshExecutionError(f"{field_name} is invalid")
    if len(value) > _MAX_DIAGNOSTICS:
        raise ProfileRefreshExecutionError(f"{field_name} is invalid")
    parsed = _tokens(value)
    if len(parsed) != len(value):
        raise ProfileRefreshExecutionError(f"{field_name} is invalid")
    return parsed


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**63 - 1:
        raise ProfileRefreshExecutionError("source sample count is invalid")
    return value


def _positive_count(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 2**63 - 1:
        raise ProfileRefreshExecutionError(f"{field_name} is invalid")
    return value

def _enum_value(value: Any, enum_type: Any) -> Any:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as error:
        raise ProfileRefreshExecutionError("authority outcome is invalid") from error

def _serialized_source_version(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _safe_source_version(value)
    except ProfileRefreshExecutionError:
        return None


def _optional_enum_value(value: Any, enum_type: Any) -> Any:
    if value is None:
        return None
    return _enum_value(value, enum_type)


def _keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    if set(value) != expected:
        raise ProfileRefreshExecutionError(f"{field_name} fields are invalid")


def _parse_canonical_object(payload: bytes, field_name: str) -> Mapping[str, Any]:
    if not isinstance(payload, bytes):
        raise ProfileRefreshExecutionError(f"{field_name} is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError) as error:
        raise ProfileRefreshExecutionError(f"{field_name} is invalid") from error
    if not isinstance(value, Mapping):
        raise ProfileRefreshExecutionError(f"{field_name} is invalid")
    try:
        canonical = _canonical_bytes(value)
    except ProfileRefreshExecutionError:
        raise ProfileRefreshExecutionError(f"{field_name} is invalid") from None
    if canonical != payload:
        raise ProfileRefreshExecutionError(f"{field_name} is not canonical")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate authority key")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> Any:
    raise ValueError(f"non-finite JSON number {value}")


def _path(value: PathInput) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as error:
        raise ProfileRefreshExecutionError("path is invalid") from error
    if not str(path):
        raise ProfileRefreshExecutionError("path is invalid")
    return path


def _owned_directory(path: Path, *, create: bool = False) -> None:
    try:
        if path.is_symlink():
            raise ProfileRefreshExecutionError("directory cannot be a symbolic link")
        if path.exists() and not path.is_dir():
            raise ProfileRefreshExecutionError("directory is invalid")
        if create and not path.exists():
            path.mkdir(parents=True)
        if path.is_symlink() or not path.is_dir():
            raise ProfileRefreshExecutionError("directory is invalid")
    except OSError as error:
        raise ProfileRefreshExecutionError("could not access directory") from error


def _owned_file(path: Path) -> None:
    try:
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise ProfileRefreshExecutionError("file is invalid")
    except OSError as error:
        raise ProfileRefreshExecutionError("could not access file") from error


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ProfileRefreshExecutionError("could not read authority") from error


def _file_digest_size(path: Path) -> tuple[str, int]:
    digest = sha256()
    size = 0
    try:
        with path.open("rb") as input_file:
            while True:
                chunk = input_file.read(_CHUNK_SIZE)
                if chunk == b"":
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise ProfileRefreshExecutionError("could not read staged object") from error
    return digest.hexdigest(), size


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    parent = path.parent
    _owned_directory(parent, create=True)
    try:
        if path.is_symlink():
            raise ProfileRefreshExecutionError("authority cannot be a symbolic link")
    except OSError as error:
        raise ProfileRefreshExecutionError("could not inspect authority") from error
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=parent, prefix=f".{path.name}.", delete=False) as output:
            temporary_name = output.name
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(parent)
    except OSError as error:
        raise ProfileRefreshExecutionError("could not publish authority") from error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)


__all__ = [
    "DEFAULT_PROFILE_REFRESH_CACHE_POLICY",
    "PROFILE_REFRESH_EXECUTION_SCHEMA_VERSION",
    "PROFILE_REFRESH_EXECUTOR_VERSION",
    "ProfileRefreshEnvironmentOutcome",
    "ProfileRefreshEnvironmentResult",
    "ProfileRefreshExecutionError",
    "ProfileRefreshExecutionResult",
    "execute_profile_refresh_plan",
    "load_staged_profile_build_bundle",
]
