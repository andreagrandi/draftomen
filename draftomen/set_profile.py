"""Versioned local set-profile data and safe loading boundary.

Profiles are intentionally independent from the card, ratings, and 17Lands
cache schemas.  They can carry only the evidence that is available while
keeping semantic roles usable when empirical sections are absent.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

from draftomen.config import COLOR_PAIRS
from draftomen.paths import app_data_dir
from draftomen.semantic_roles import (
    CompiledRoleProfile,
    ProfileCard,
    Role,
    RoleClassifier,
    RoleProfileError,
    RoleSchemaError,
    ResolutionResult,
)

if TYPE_CHECKING:
    from draftomen.carddb import CardInfo

SUPPORTED_SET_PROFILE_SCHEMA_VERSIONS = (1, 2)
SET_PROFILE_SCHEMA_VERSION = 2
SET_PROFILE_DIRECTORY_NAME = "set-profiles"
GENERIC_PROFILE_GENERATED_AT = "1970-01-01T00:00:00+00:00"

PathInput: TypeAlias = str | os.PathLike[str]


def profile_card_key(card: CardInfo) -> str:
    """Return the canonical identity key used by generated profile data."""

    if card.oracle_id:
        key = f"oracle_id:{card.oracle_id}"
    elif card.set_code and card.collector_number:
        key = f"set:{card.set_code}:{card.collector_number}"
    elif card.arena_id is not None:
        key = f"arena_id:{card.arena_id}"
    else:
        key = f"grp_id:{card.grp_id}"
    return key.casefold()


class SetProfileError(ValueError):
    """Base error raised for invalid set-profile data or loading failures."""


class SetProfileSchemaError(SetProfileError):
    """Raised when a profile does not match the supported JSON schema."""


class ProfileMaturity(str, Enum):
    """Evidence lifecycle state of a local profile artifact."""

    MATURE = "mature"
    EARLY = "early"
    METADATA_ONLY = "metadata-only"
    SEMANTIC_ONLY = "semantic-only"
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Identify the source artifact used to generate a profile."""

    provider: str
    artifact: str | None = None
    revision: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _non_empty_string(self.provider, "source.provider"))
        for field_name in ("artifact", "revision", "url"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _non_empty_string(value, f"source.{field_name}"))

    def to_json(self) -> dict[str, str]:
        result: dict[str, str] = {"provider": self.provider}
        for field_name in ("artifact", "revision", "url"):
            value = getattr(self, field_name)
            if value is not None:
                result[field_name] = value
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> SourceMetadata:
        _object(value, "source")
        provider = _required_string(value, "provider", "source.provider")
        return cls(
            provider=provider,
            artifact=_optional_string(value, "artifact", "source.artifact"),
            revision=_optional_string(value, "revision", "source.revision"),
            url=_optional_string(value, "url", "source.url"),
        )


@dataclass(frozen=True, slots=True)
class SampleSummary:
    """Record empirical sample counts without inventing missing evidence."""

    total: int
    by_pair: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        _non_negative_int(self.total, "samples.total")
        counts = (
            (_pair(pair), _non_negative_int(count, f"samples.by_pair.{pair}"))
            for pair, count in self.by_pair
        )
        normalized = tuple(sorted(counts, key=lambda item: COLOR_PAIRS.index(item[0])))
        if len({pair for pair, _ in normalized}) != len(normalized):
            raise SetProfileSchemaError("samples.by_pair contains duplicate color pairs.")
        object.__setattr__(self, "by_pair", normalized)

    def count_for(self, pair: str) -> int | None:
        normalized = _pair(pair)
        return next((count for candidate, count in self.by_pair if candidate == normalized), None)

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"total": self.total}
        if self.by_pair:
            result["by_pair"] = {pair: count for pair, count in self.by_pair}
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> SampleSummary:
        _object(value, "samples")
        by_pair_value = value.get("by_pair", {})
        if not isinstance(by_pair_value, Mapping):
            raise SetProfileSchemaError("samples.by_pair must be an object.")
        return cls(
            total=_required_int(value, "total", "samples.total"),
            by_pair=tuple(
                (_pair(pair), _required_int(by_pair_value, pair, f"samples.by_pair.{pair}"))
                for pair in by_pair_value
            ),
        )

@dataclass(frozen=True, slots=True)
class AggregateEvidence:
    """Authority metadata for one aggregate rate estimate."""

    source_format: str
    fallback_reason: str | None
    confidence: float

    def __post_init__(self) -> None:
        source_format = _non_empty_string(self.source_format, "aggregate_evidence.source_format").casefold()
        if "/" in source_format or "\\" in source_format:
            raise SetProfileSchemaError(
                "aggregate_evidence.source_format cannot contain path separators."
            )
        object.__setattr__(self, "source_format", source_format)
        if self.fallback_reason is not None:
            reason = _non_empty_string(
                self.fallback_reason,
                "aggregate_evidence.fallback_reason",
            )
            if reason not in {
                "missing-exact-evidence",
                "thin-exact-evidence",
                "invalid-exact-evidence",
            }:
                raise SetProfileSchemaError(
                    f"Unsupported aggregate fallback reason {reason!r}."
                )
            object.__setattr__(self, "fallback_reason", reason)
        object.__setattr__(
            self,
            "confidence",
            _bounded_number(self.confidence, "aggregate_evidence.confidence"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "source_format": self.source_format,
            "fallback_reason": self.fallback_reason,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> AggregateEvidence:
        _object(value, "aggregate_evidence")
        expected = {"source_format", "fallback_reason", "confidence"}
        if set(value) != expected:
            raise SetProfileSchemaError(
                "aggregate_evidence must contain exactly source_format, fallback_reason, and confidence."
            )
        fallback_reason = value["fallback_reason"]
        if fallback_reason is not None and not isinstance(fallback_reason, str):
            raise SetProfileSchemaError("aggregate_evidence.fallback_reason must be a string or null.")
        return cls(
            source_format=value["source_format"],
            fallback_reason=fallback_reason,
            confidence=value["confidence"],
        )


@dataclass(frozen=True, slots=True)
class RateEstimate:
    """One bounded rate with its raw observation and shrinkage provenance."""

    raw_value: float | None
    value: float
    samples: int
    prior_value: float
    source: str
    aggregate_evidence: AggregateEvidence | None = None

    def __post_init__(self) -> None:
        raw_value = None
        if self.raw_value is not None:
            raw_value = _bounded_number(self.raw_value, "rate.raw_value")
        value = _bounded_number(self.value, "rate.value")
        samples = _non_negative_int(self.samples, "rate.samples")
        prior_value = _bounded_number(self.prior_value, "rate.prior_value")
        source = _non_empty_string(self.source, "rate.source")
        if samples == 0 and raw_value is not None:
            raise SetProfileSchemaError("rate.raw_value must be None when rate.samples is zero.")
        if samples > 0 and raw_value is None:
            raise SetProfileSchemaError("rate.raw_value is required when rate.samples is positive.")
        if self.aggregate_evidence is not None and not isinstance(
            self.aggregate_evidence,
            AggregateEvidence,
        ):
            raise SetProfileSchemaError(
                "rate.aggregate_evidence must be an AggregateEvidence or None."
            )
        if samples == 0 and self.aggregate_evidence is not None:
            raise SetProfileSchemaError(
                "rate.aggregate_evidence must be None when rate.samples is zero."
            )
        object.__setattr__(self, "raw_value", raw_value)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "prior_value", prior_value)
        object.__setattr__(self, "source", source)

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "raw_value": self.raw_value,
            "value": self.value,
            "samples": self.samples,
            "prior_value": self.prior_value,
            "source": self.source,
        }
        if self.aggregate_evidence is not None:
            result["aggregate_evidence"] = self.aggregate_evidence.to_json()
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RateEstimate:
        _object(value, "rate")
        raw_value = value.get("raw_value")
        if "raw_value" not in value:
            raise SetProfileSchemaError("Missing required field rate.raw_value.")
        if raw_value is not None:
            raw_value = _bounded_number(raw_value, "rate.raw_value")
        aggregate_value = value.get("aggregate_evidence")
        aggregate_evidence = (
            None
            if aggregate_value is None
            else AggregateEvidence.from_json(aggregate_value)
        )
        return cls(
            raw_value=raw_value,
            value=_required_number(value, "value", "rate.value"),
            samples=_required_int(value, "samples", "rate.samples"),
            prior_value=_required_number(value, "prior_value", "rate.prior_value"),
            source=_required_string(value, "source", "rate.source"),
            aggregate_evidence=aggregate_evidence,
        )


@dataclass(frozen=True, slots=True)
class CardRating:
    """One card's shrunk games-in-hand win-rate estimate."""

    card_key: str
    gih_win_rate: RateEstimate
    average_last_seen_at: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "card_key",
            _non_empty_string(self.card_key, "card_rating.card_key").casefold(),
        )
        if not isinstance(self.gih_win_rate, RateEstimate):
            raise SetProfileSchemaError("card_rating.gih_win_rate must be a RateEstimate.")
        if self.average_last_seen_at is not None:
            object.__setattr__(
                self,
                "average_last_seen_at",
                _finite_non_negative(
                    self.average_last_seen_at,
                    "card_rating.average_last_seen_at",
                ),
            )

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "card_key": self.card_key,
            "gih_win_rate": self.gih_win_rate.to_json(),
        }
        if self.average_last_seen_at is not None:
            result["average_last_seen_at"] = self.average_last_seen_at
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CardRating:
        _object(value, "card rating")
        gih_win_rate = value.get("gih_win_rate")
        if not isinstance(gih_win_rate, Mapping):
            raise SetProfileSchemaError("card_rating.gih_win_rate must be an object.")
        average_last_seen_at = value.get("average_last_seen_at")
        if average_last_seen_at is not None:
            average_last_seen_at = _finite_non_negative(
                average_last_seen_at,
                "card_rating.average_last_seen_at",
            )
        return cls(
            card_key=_required_string(value, "card_key", "card_rating.card_key"),
            gih_win_rate=RateEstimate.from_json(gih_win_rate),
            average_last_seen_at=average_last_seen_at,
        )


def _target_evidence_json(
    *,
    raw_value: float | None,
    prior_value: float | None,
    samples: int | None,
    source: str | None,
) -> dict[str, object]:
    if all(value is None for value in (raw_value, prior_value, samples, source)):
        return {}
    if prior_value is None or samples is None or source is None:
        raise SetProfileSchemaError(
            "target evidence must include raw_value, prior_value, samples, and source."
        )
    return {
        "raw_value": raw_value,
        "prior_value": prior_value,
        "samples": samples,
        "source": source,
    }


def _parse_target_evidence(
    value: Mapping[str, Any],
    field_name: str,
) -> tuple[float | None, float | None, int | None, str | None]:
    keys = {"raw_value", "prior_value", "samples", "source"}
    present = keys.intersection(value)
    if present and present != keys:
        raise SetProfileSchemaError(
            f"{field_name} evidence must include raw_value, prior_value, samples, and source."
        )
    if not present:
        return None, None, None, None
    raw_value = value["raw_value"]
    if raw_value is not None:
        raw_value = _finite_non_negative(raw_value, f"{field_name}.raw_value")
    prior_value = _finite_non_negative(value["prior_value"], f"{field_name}.prior_value")
    samples = _non_negative_int(value["samples"], f"{field_name}.samples")
    source = _non_empty_string(value["source"], f"{field_name}.source")
    if samples == 0 and raw_value is not None:
        raise SetProfileSchemaError(f"{field_name}.raw_value must be None when samples is zero.")
    if samples > 0 and raw_value is None:
        raise SetProfileSchemaError(f"{field_name}.raw_value is required when samples is positive.")
    return raw_value, prior_value, samples, source


def _normalize_target_evidence(
    *,
    raw_value: float | None,
    prior_value: float | None,
    samples: int | None,
    source: str | None,
    field_name: str,
) -> tuple[float | None, float | None, int | None, str | None]:
    if all(value is None for value in (raw_value, prior_value, samples, source)):
        return None, None, None, None
    return _parse_target_evidence(
        {
            "raw_value": raw_value,
            "prior_value": prior_value,
            "samples": samples,
            "source": source,
        },
        field_name,
    )


@dataclass(frozen=True, slots=True)
class NumericTarget:
    """One named non-negative structural target."""

    name: str
    value: float
    raw_value: float | None = None
    prior_value: float | None = None
    samples: int | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _non_empty_string(self.name, "target.name").casefold())
        object.__setattr__(self, "value", _finite_non_negative(self.value, "target.value"))
        evidence = _normalize_target_evidence(
            raw_value=self.raw_value,
            prior_value=self.prior_value,
            samples=self.samples,
            source=self.source,
            field_name="target",
        )
        object.__setattr__(self, "raw_value", evidence[0])
        object.__setattr__(self, "prior_value", evidence[1])
        object.__setattr__(self, "samples", evidence[2])
        object.__setattr__(self, "source", evidence[3])

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"name": self.name, "value": self.value}
        result.update(
            _target_evidence_json(
                raw_value=self.raw_value,
                prior_value=self.prior_value,
                samples=self.samples,
                source=self.source,
            )
        )
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any], *, field_name: str = "target") -> NumericTarget:
        _object(value, field_name)
        raw_value, prior_value, samples, source = _parse_target_evidence(value, field_name)
        return cls(
            name=_required_string(value, "name", f"{field_name}.name"),
            value=_required_number(value, "value", f"{field_name}.value"),
            raw_value=raw_value,
            prior_value=prior_value,
            samples=samples,
            source=source,
        )


@dataclass(frozen=True, slots=True)
class RoleTarget:
    """One target count or weight for an existing semantic role."""

    role: Role
    value: float
    raw_value: float | None = None
    prior_value: float | None = None
    samples: int | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        try:
            role = self.role if isinstance(self.role, Role) else Role(self.role)
        except (TypeError, ValueError) as error:
            raise SetProfileSchemaError(f"Unsupported role target {self.role!r}.") from error
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "value", _finite_non_negative(self.value, "role_target.value"))
        evidence = _normalize_target_evidence(
            raw_value=self.raw_value,
            prior_value=self.prior_value,
            samples=self.samples,
            source=self.source,
            field_name="role_target",
        )
        object.__setattr__(self, "raw_value", evidence[0])
        object.__setattr__(self, "prior_value", evidence[1])
        object.__setattr__(self, "samples", evidence[2])
        object.__setattr__(self, "source", evidence[3])

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"role": self.role.value, "value": self.value}
        result.update(
            _target_evidence_json(
                raw_value=self.raw_value,
                prior_value=self.prior_value,
                samples=self.samples,
                source=self.source,
            )
        )
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RoleTarget:
        _object(value, "role target")
        raw_value, prior_value, samples, source = _parse_target_evidence(value, "role_target")
        return cls(
            role=_required_string(value, "role", "role_target.role"),
            value=_required_number(value, "value", "role_target.value"),
            raw_value=raw_value,
            prior_value=prior_value,
            samples=samples,
            source=source,
        )


@dataclass(frozen=True, slots=True)
class RemovalTarget:
    """One target for a normalized removal subtype."""

    kind: str
    value: float
    raw_value: float | None = None
    prior_value: float | None = None
    samples: int | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        kind = _non_empty_string(self.kind, "removal_target.kind").casefold()
        if kind not in {"destroy", "exile", "damage", "bounce", "disable", "tap"}:
            raise SetProfileSchemaError(f"Unsupported removal target kind {kind!r}.")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", _finite_non_negative(self.value, "removal_target.value"))
        evidence = _normalize_target_evidence(
            raw_value=self.raw_value,
            prior_value=self.prior_value,
            samples=self.samples,
            source=self.source,
            field_name="removal_target",
        )
        object.__setattr__(self, "raw_value", evidence[0])
        object.__setattr__(self, "prior_value", evidence[1])
        object.__setattr__(self, "samples", evidence[2])
        object.__setattr__(self, "source", evidence[3])

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind, "value": self.value}
        result.update(
            _target_evidence_json(
                raw_value=self.raw_value,
                prior_value=self.prior_value,
                samples=self.samples,
                source=self.source,
            )
        )
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RemovalTarget:
        _object(value, "removal target")
        raw_value, prior_value, samples, source = _parse_target_evidence(value, "removal_target")
        return cls(
            kind=_required_string(value, "kind", "removal_target.kind"),
            value=_required_number(value, "value", "removal_target.value"),
            raw_value=raw_value,
            prior_value=prior_value,
            samples=samples,
            source=source,
        )


@dataclass(frozen=True, slots=True)
class CardPairSynergy:
    """Optional empirical synergy score for two cards in a color pair."""

    first_card: str
    second_card: str
    value: float
    samples: int | None = None

    def __post_init__(self) -> None:
        first = _non_empty_string(self.first_card, "synergy.first_card").casefold()
        second = _non_empty_string(self.second_card, "synergy.second_card").casefold()
        if first == second:
            raise SetProfileSchemaError("A card-pair synergy entry needs two distinct cards.")
        if second < first:
            first, second = second, first
        object.__setattr__(self, "first_card", first)
        object.__setattr__(self, "second_card", second)
        object.__setattr__(self, "value", _finite_number(self.value, "synergy.value"))
        if self.samples is not None:
            object.__setattr__(self, "samples", _non_negative_int(self.samples, "synergy.samples"))

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "first_card": self.first_card,
            "second_card": self.second_card,
            "value": self.value,
        }
        if self.samples is not None:
            result["samples"] = self.samples
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CardPairSynergy:
        _object(value, "synergy")
        samples = value.get("samples")
        return cls(
            first_card=_required_string(value, "first_card", "synergy.first_card"),
            second_card=_required_string(value, "second_card", "synergy.second_card"),
            value=_required_number(value, "value", "synergy.value"),
            samples=None if samples is None else _required_int(value, "samples", "synergy.samples"),
        )


@dataclass(frozen=True, slots=True)
class ScarcityTarget:
    """Optional empirical scarcity signal for one card."""

    card_key: str
    value: float
    samples: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_key", _non_empty_string(self.card_key, "scarcity.card_key").casefold())
        object.__setattr__(self, "value", _finite_non_negative(self.value, "scarcity.value"))
        if self.samples is not None:
            object.__setattr__(self, "samples", _non_negative_int(self.samples, "scarcity.samples"))

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"card_key": self.card_key, "value": self.value}
        if self.samples is not None:
            result["samples"] = self.samples
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ScarcityTarget:
        _object(value, "scarcity")
        samples = value.get("samples")
        return cls(
            card_key=_required_string(value, "card_key", "scarcity.card_key"),
            value=_required_number(value, "value", "scarcity.value"),
            samples=None if samples is None else _required_int(value, "samples", "scarcity.samples"),
        )


@dataclass(frozen=True, slots=True)
class PairProfile:
    """Evidence attached to one canonical two-color context."""

    pair: str
    structural_targets: tuple[NumericTarget, ...] = ()
    role_targets: tuple[RoleTarget, ...] = ()
    removal_targets: tuple[RemovalTarget, ...] = ()
    synergy: tuple[CardPairSynergy, ...] = ()
    scarcity: tuple[ScarcityTarget, ...] = ()
    theme: str | None = None
    performance: RateEstimate | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", _pair(self.pair))
        if self.theme is not None:
            object.__setattr__(
                self,
                "theme",
                _non_empty_string(self.theme, "pair_profile.theme"),
            )
        if self.performance is not None and not isinstance(self.performance, RateEstimate):
            raise SetProfileSchemaError("pair_profile.performance must be a RateEstimate or None.")
        for field_name, values, expected_type in (
            ("structural_targets", self.structural_targets, NumericTarget),
            ("role_targets", self.role_targets, RoleTarget),
            ("removal_targets", self.removal_targets, RemovalTarget),
            ("synergy", self.synergy, CardPairSynergy),
            ("scarcity", self.scarcity, ScarcityTarget),
        ):
            if not isinstance(values, tuple) or any(not isinstance(item, expected_type) for item in values):
                raise SetProfileSchemaError(f"{field_name} must contain the expected target objects.")
        object.__setattr__(
            self,
            "structural_targets",
            _sorted_unique(self.structural_targets, key=lambda item: item.name, field_name="structural_targets"),
        )
        object.__setattr__(
            self,
            "role_targets",
            _sorted_unique(self.role_targets, key=lambda item: item.role.value, field_name="role_targets"),
        )
        object.__setattr__(
            self,
            "removal_targets",
            _sorted_unique(self.removal_targets, key=lambda item: item.kind, field_name="removal_targets"),
        )
        object.__setattr__(
            self,
            "synergy",
            _sorted_unique(
                self.synergy,
                key=lambda item: (item.first_card, item.second_card),
                field_name="synergy",
            ),
        )
        object.__setattr__(
            self,
            "scarcity",
            _sorted_unique(self.scarcity, key=lambda item: item.card_key, field_name="scarcity"),
        )

    @property
    def structural(self) -> tuple[NumericTarget, ...]:
        return self.structural_targets

    @property
    def roles(self) -> tuple[RoleTarget, ...]:
        return self.role_targets

    @property
    def removals(self) -> tuple[RemovalTarget, ...]:
        return self.removal_targets

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"pair": self.pair}
        if self.performance is not None:
            result["performance"] = self.performance.to_json()
        for name, values in (
            ("removal_targets", self.removal_targets),
            ("role_targets", self.role_targets),
            ("scarcity", self.scarcity),
            ("structural_targets", self.structural_targets),
            ("synergy", self.synergy),
        ):
            if values:
                result[name] = [item.to_json() for item in values]
        if self.theme is not None:
            result["theme"] = self.theme
        return result

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> PairProfile:
        _object(value, "pair profile")
        performance = value.get("performance")
        if performance is not None:
            _object(performance, "pair_profile.performance")
            performance = RateEstimate.from_json(performance)
        return cls(
            pair=_required_string(value, "pair", "pair_profile.pair"),
            structural_targets=_array_of(
                value,
                "structural_targets",
                NumericTarget.from_json,
                "pair_profile.structural_targets",
            ),
            role_targets=_array_of(value, "role_targets", RoleTarget.from_json, "pair_profile.role_targets"),
            removal_targets=_array_of(
                value,
                "removal_targets",
                RemovalTarget.from_json,
                "pair_profile.removal_targets",
            ),
            synergy=_array_of(value, "synergy", CardPairSynergy.from_json, "pair_profile.synergy"),
            scarcity=_array_of(value, "scarcity", ScarcityTarget.from_json, "pair_profile.scarcity"),
            theme=_optional_string(value, "theme", "pair_profile.theme"),
            performance=performance,
        )


@dataclass(frozen=True, slots=True)
class SetProfileLoadResult:
    """Result of safe loading with one usable immutable profile."""

    profile: "SetProfile"
    source: str
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile, SetProfile):
            raise TypeError("profile must be a SetProfile.")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string.")
        object.__setattr__(self, "diagnostics", tuple(dict.fromkeys(str(item) for item in self.diagnostics)))


@dataclass(frozen=True, slots=True)
class SetProfile:
    """Deeply immutable versioned profile for one set and limited format."""

    set_code: str
    event_format: str
    profile_version: str
    generated_at: str
    source: SourceMetadata
    maturity: ProfileMaturity | str
    samples: SampleSummary | None
    confidence: float
    pairs: tuple[PairProfile, ...]
    role_profile: CompiledRoleProfile | None = None
    card_ratings: tuple[CardRating, ...] = ()
    schema_version: int = 1
    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version not in SUPPORTED_SET_PROFILE_SCHEMA_VERSIONS
        ):
            raise SetProfileSchemaError(
                f"Unsupported set profile schema {self.schema_version!r}; "
                f"supported versions are {SUPPORTED_SET_PROFILE_SCHEMA_VERSIONS}."
            )
        object.__setattr__(self, "schema_version", self.schema_version)
        set_code = _non_empty_string(self.set_code, "set_code").casefold()
        event_format = _non_empty_string(self.event_format, "format").casefold()
        if "/" in set_code or "\\" in set_code or "/" in event_format or "\\" in event_format:
            raise SetProfileSchemaError("set_code and format cannot contain path separators.")
        object.__setattr__(self, "set_code", set_code)
        object.__setattr__(self, "event_format", event_format)
        object.__setattr__(self, "profile_version", _non_empty_string(self.profile_version, "profile_version"))
        generated_at = _non_empty_string(self.generated_at, "generated_at")
        _parse_datetime(generated_at, "generated_at")
        object.__setattr__(self, "generated_at", generated_at)
        if not isinstance(self.source, SourceMetadata):
            raise SetProfileSchemaError("source must be SourceMetadata.")
        try:
            raw_maturity = (
                self.maturity.value
                if isinstance(self.maturity, ProfileMaturity)
                else _non_empty_string(self.maturity, "maturity")
            )
            maturity = ProfileMaturity(raw_maturity)
        except (TypeError, ValueError, SetProfileSchemaError) as error:
            raise SetProfileSchemaError(f"Unsupported profile maturity {self.maturity!r}.") from error
        object.__setattr__(self, "maturity", maturity)
        if self.samples is not None and not isinstance(self.samples, SampleSummary):
            raise SetProfileSchemaError("samples must be SampleSummary or None.")
        object.__setattr__(self, "confidence", _bounded_number(self.confidence, "confidence"))
        if not isinstance(self.pairs, tuple) or any(not isinstance(item, PairProfile) for item in self.pairs):
            raise SetProfileSchemaError("pairs must contain PairProfile objects.")
        normalized_pairs = tuple(sorted(self.pairs, key=lambda item: COLOR_PAIRS.index(item.pair)))
        if len({item.pair for item in normalized_pairs}) != len(normalized_pairs):
            raise SetProfileSchemaError("pairs contains duplicate color pairs.")
        object.__setattr__(self, "pairs", normalized_pairs)
        if not isinstance(self.card_ratings, tuple) or any(
            not isinstance(item, CardRating) for item in self.card_ratings
        ):
            raise SetProfileSchemaError("card_ratings must contain CardRating objects.")
        normalized_card_ratings = _sorted_unique(
            self.card_ratings,
            key=lambda item: item.card_key,
            field_name="card_ratings",
        )
        object.__setattr__(self, "card_ratings", normalized_card_ratings)
        _validate_aggregate_authority(
            schema_version=self.schema_version,
            requested_format=event_format,
            pairs=normalized_pairs,
            card_ratings=normalized_card_ratings,
        )
        if self.role_profile is not None:
            if not isinstance(self.role_profile, CompiledRoleProfile):
                raise SetProfileSchemaError("role_profile must be a CompiledRoleProfile or None.")
            if self.role_profile.set_code != set_code:
                raise SetProfileSchemaError("role_profile.set_code must match set_code.")
        has_empirical_evidence = _has_empirical_evidence(
            self.samples,
            normalized_pairs,
            normalized_card_ratings,
        )
        if maturity in {ProfileMaturity.MATURE, ProfileMaturity.EARLY} and not has_empirical_evidence:
            raise SetProfileSchemaError(f"{maturity.value} profiles must contain empirical evidence.")
        if maturity is ProfileMaturity.METADATA_ONLY and self.role_profile is not None:
            raise SetProfileSchemaError("metadata-only profiles cannot contain semantic evidence.")
        if maturity is ProfileMaturity.SEMANTIC_ONLY and self.role_profile is None:
            raise SetProfileSchemaError("semantic-only profiles must contain semantic evidence.")
        if maturity in {ProfileMaturity.METADATA_ONLY, ProfileMaturity.SEMANTIC_ONLY} and has_empirical_evidence:
            raise SetProfileSchemaError(f"{maturity.value} profiles cannot contain empirical evidence.")
        if maturity is ProfileMaturity.GENERIC and (has_empirical_evidence or self.role_profile is not None):
            raise SetProfileSchemaError("generic profiles cannot contain evidence.")


    @property
    def pair_profiles(self) -> tuple[PairProfile, ...]:
        return self.pairs


    @property
    def card_roles(self) -> tuple[ProfileCard, ...]:
        return () if self.role_profile is None else self.role_profile.cards

    @property
    def roles_are_compatible(self) -> bool:
        return self.role_profile is not None and self.role_profile.is_compatible()

    def pair(self, pair: str) -> PairProfile | None:
        normalized = _pair(pair)
        return next((item for item in self.pairs if item.pair == normalized), None)

    def resolve_roles(
        self,
        card: Any,
        *,
        classifier: RoleClassifier | None = None,
    ) -> ResolutionResult:
        """Resolve roles through RoleClassifier without cross-version merging."""

        resolver = classifier or RoleClassifier()
        return resolver.resolve(card, profile=self.role_profile)

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "confidence": self.confidence,
            "format": self.event_format,
            "generated_at": self.generated_at,
            "maturity": self.maturity.value,
            "profile_version": self.profile_version,
            "schema_version": self.schema_version,
            "set_code": self.set_code,
            "source": self.source.to_json(),
        }
        if self.card_ratings:
            result["card_ratings"] = [item.to_json() for item in self.card_ratings]
        if self.pairs:
            result["pair_profiles"] = [item.to_json() for item in self.pairs]
        if self.samples is not None:
            result["samples"] = self.samples.to_json()
        if self.role_profile is not None:
            result["role_profile"] = self.role_profile.to_json()
        return result

    def to_bytes(self) -> bytes:
        return _json_bytes(self.to_json())

    @property
    def fingerprint(self) -> str | None:
        """Return the deterministic identity of profile-backed content."""

        if self.maturity is ProfileMaturity.GENERIC:
            return None
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> SetProfile:
        _object(value, "set profile")
        schema_version = _required_int(value, "schema_version", "schema_version")
        if schema_version not in SUPPORTED_SET_PROFILE_SCHEMA_VERSIONS:
            raise SetProfileSchemaError(
                f"Unsupported set profile schema {schema_version}; "
                f"supported versions are {SUPPORTED_SET_PROFILE_SCHEMA_VERSIONS}."
            )
        pair_values = value.get("pair_profiles", [])
        if not isinstance(pair_values, list):
            raise SetProfileSchemaError("pair_profiles must be an array.")
        pair_profiles = tuple(
            PairProfile.from_json(item) for item in _mapping_items(pair_values, "pair_profiles")
        )
        if len({item.pair for item in pair_profiles}) != len(pair_profiles):
            raise SetProfileSchemaError("pair_profiles contains duplicate color pairs.")
        card_rating_values = value.get("card_ratings", [])
        if not isinstance(card_rating_values, list):
            raise SetProfileSchemaError("card_ratings must be an array.")
        card_ratings = tuple(
            CardRating.from_json(item) for item in _mapping_items(card_rating_values, "card_ratings")
        )
        if len({item.card_key for item in card_ratings}) != len(card_ratings):
            raise SetProfileSchemaError("card_ratings contains duplicate card identities.")
        role_profile = _parse_role_profile(value.get("role_profile"), set_code=value.get("set_code"))
        samples_value = value.get("samples")
        samples = None if samples_value is None else SampleSummary.from_json(samples_value)
        return cls(
            set_code=_required_string(value, "set_code", "set_code"),
            event_format=_required_string(value, "format", "format"),
            profile_version=_required_string(value, "profile_version", "profile_version"),
            generated_at=_required_string(value, "generated_at", "generated_at"),
            source=SourceMetadata.from_json(_required_mapping(value, "source", "source")),
            maturity=_required_string(value, "maturity", "maturity"),
            samples=samples,
            confidence=_required_number(value, "confidence", "confidence"),
            pairs=pair_profiles,
            role_profile=role_profile,
            schema_version=schema_version,
            card_ratings=card_ratings,
        )

    @classmethod
    def generic(cls, *, set_code: str, event_format: str) -> SetProfile:
        """Create the immutable no-evidence fallback profile."""

        return cls(
            set_code=set_code,
            event_format=event_format,
            profile_version="generic-1",
            generated_at=GENERIC_PROFILE_GENERATED_AT,
            source=SourceMetadata(provider="builtin-generic"),
            maturity=ProfileMaturity.GENERIC,
            samples=None,
            confidence=0.0,
            pairs=(),
        )


def set_profile_path(
    *,
    set_code: str,
    event_format: str,
    app_dir: PathInput | None = None,
) -> Path:
    """Return the deterministic local path for one set and format profile."""

    normalized_set = _safe_component(set_code, "set_code")
    normalized_format = _safe_component(event_format, "format")
    root = Path(app_data_dir() if app_dir is None else app_dir).expanduser()
    return root / SET_PROFILE_DIRECTORY_NAME / f"{normalized_set}-{normalized_format}.json"


def load_set_profile(
    path: PathInput,
    *,
    expected_set_code: str | None = None,
    expected_format: str | None = None,
) -> SetProfile:
    """Load and strictly validate one local set profile JSON artifact."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise SetProfileError(f"Could not load set profile {path}: {error}.") from error
    if not isinstance(value, Mapping):
        raise SetProfileSchemaError("Set profile JSON must be an object.")
    profile = SetProfile.from_json(value)
    _check_target(profile, expected_set_code=expected_set_code, expected_format=expected_format)
    return profile


def dump_set_profile(profile: SetProfile, path: PathInput) -> Path:
    """Write a set profile atomically in deterministic JSON form."""

    if not isinstance(profile, SetProfile):
        raise TypeError("profile must be a SetProfile.")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output.name}.",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(profile.to_bytes())
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    except OSError as error:
        raise SetProfileError(f"Could not write set profile {output}: {error}.") from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
    return output


def safe_load_set_profile(
    set_code: str,
    event_format: str,
    *,
    profile_path: PathInput | None = None,
    profile_paths: Iterable[PathInput] = (),
    app_dir: PathInput | None = None,
    last_valid_profile: SetProfile | None = None,
) -> SetProfileLoadResult:
    """Load a profile without raising, using explicit maturity precedence."""

    normalized_set = _safe_component(set_code, "set_code")
    normalized_format = _safe_component(event_format, "format")
    diagnostics: list[str] = []
    try:
        candidates = _candidate_paths(
            profile_path=profile_path,
            profile_paths=profile_paths,
            app_dir=app_dir,
            set_code=normalized_set,
            event_format=normalized_format,
            diagnostics=diagnostics,
        )
    except (OSError, RuntimeError) as error:
        diagnostics.append(f"candidate-discovery:boundary:{error}")
        candidates = ()
    valid: list[tuple[int, SetProfile, PathInput]] = []
    for index, candidate in enumerate(candidates):
        try:
            profile = load_set_profile(
                candidate,
                expected_set_code=normalized_set,
                expected_format=normalized_format,
            )
        except (OSError, SetProfileError, TypeError, ValueError) as error:
            diagnostics.append(f"rejected:{candidate}:{error}")
            continue
        if profile.maturity not in {
            ProfileMaturity.MATURE,
            ProfileMaturity.EARLY,
            ProfileMaturity.SEMANTIC_ONLY,
            ProfileMaturity.METADATA_ONLY,
        }:
            diagnostics.append(f"rejected:{candidate}:unsupported local maturity {profile.maturity.value}")
            continue
        valid.append((index, profile, candidate))

    rank = {
        ProfileMaturity.MATURE: 0,
        ProfileMaturity.EARLY: 1,
        ProfileMaturity.SEMANTIC_ONLY: 2,
        ProfileMaturity.METADATA_ONLY: 3,
    }
    if valid:
        _, selected, _ = min(valid, key=lambda item: (rank[item[1].maturity], item[0]))
        source = f"local-{selected.maturity.value}"
        return SetProfileLoadResult(profile=selected, source=source, diagnostics=tuple(diagnostics))

    if last_valid_profile is not None:
        try:
            _check_target(
                last_valid_profile,
                expected_set_code=normalized_set,
                expected_format=normalized_format,
            )
        except SetProfileError as error:
            diagnostics.append(f"rejected:last-valid:{error}")
        else:
            return SetProfileLoadResult(
                profile=last_valid_profile,
                source="last-valid",
                diagnostics=tuple(diagnostics),
            )

    generic = SetProfile.generic(set_code=normalized_set, event_format=normalized_format)
    return SetProfileLoadResult(profile=generic, source="generic", diagnostics=tuple(diagnostics))


def load_scoring_profile(
    set_code: str,
    event_format: str,
    *,
    profile_path: PathInput | None = None,
    profile_paths: Iterable[PathInput] = (),
    app_dir: PathInput | None = None,
    last_valid_profile: SetProfile | None = None,
) -> SetProfile | None:
    """Load the profile usable by scoring, without exposing generic fallback data."""

    result = safe_load_set_profile(
        set_code=set_code,
        event_format=event_format,
        profile_path=profile_path,
        profile_paths=profile_paths,
        app_dir=app_dir,
        last_valid_profile=last_valid_profile,
    )
    if result.source == "generic" or result.profile.maturity is ProfileMaturity.GENERIC:
        return None
    return result.profile


def _parse_role_profile(value: Any, *, set_code: Any) -> CompiledRoleProfile | None:
    if value is None:
        return None
    _object(value, "role_profile")
    normalized_set = _safe_component(set_code, "set_code")
    try:
        role_profile = CompiledRoleProfile.from_json(value)
    except (RoleProfileError, RoleSchemaError) as error:
        raise SetProfileSchemaError(f"Invalid role_profile: {error}") from error
    nested_set = _safe_component(role_profile.set_code, "role_profile.set_code")
    if nested_set != normalized_set:
        raise SetProfileSchemaError("role_profile.set_code must match set_code.")
    return role_profile


def _validate_aggregate_authority(
    *,
    schema_version: int,
    requested_format: str,
    pairs: tuple[PairProfile, ...],
    card_ratings: tuple[CardRating, ...],
) -> None:
    rates = [item.gih_win_rate for item in card_ratings]
    rates.extend(
        pair.performance
        for pair in pairs
        if pair.performance is not None
    )
    for rate in rates:
        evidence = rate.aggregate_evidence
        if schema_version == 1:
            if evidence is not None:
                raise SetProfileSchemaError(
                    "schema-1 profiles cannot contain aggregate authority."
                )
            continue
        if rate.samples > 0 and evidence is None:
            raise SetProfileSchemaError(
                "schema-2 positive aggregate rates require aggregate authority."
            )
        if evidence is None:
            continue
        if evidence.source_format == requested_format:
            if evidence.fallback_reason is not None:
                raise SetProfileSchemaError(
                    "Exact aggregate authority cannot include a fallback reason."
                )
            continue
        if (
            requested_format != "quickdraft"
            or evidence.source_format not in {"premierdraft", "traddraft"}
            or evidence.fallback_reason is None
        ):
            raise SetProfileSchemaError(
                "Cross-format aggregate authority is only valid for QuickDraft fallbacks."
            )


def _has_empirical_evidence(
    samples: SampleSummary | None,
    pairs: tuple[PairProfile, ...],
    card_ratings: tuple[CardRating, ...] = (),
) -> bool:
    if samples is not None and (
        samples.total > 0 or any(count > 0 for _, count in samples.by_pair)
    ):
        return True
    if any(item.gih_win_rate.samples > 0 for item in card_ratings):
        return True
    return any(
        pair.structural_targets
        or pair.role_targets
        or pair.removal_targets
        or pair.synergy
        or pair.scarcity
        or (pair.performance is not None and pair.performance.samples > 0)
        for pair in pairs
    )


def _candidate_paths(
    *,
    profile_path: PathInput | None,
    profile_paths: Iterable[PathInput],
    app_dir: PathInput | None,
    set_code: str,
    event_format: str,
    diagnostics: list[str] | None = None,
) -> tuple[Path, ...]:
    messages = diagnostics if diagnostics is not None else []
    values: list[Path] = []
    if profile_path is not None:
        try:
            values.append(Path(profile_path).expanduser())
        except (OSError, RuntimeError) as error:
            messages.append(f"candidate-discovery:{profile_path}:expanduser:{error}")
    try:
        for value in profile_paths:
            try:
                values.append(Path(value).expanduser())
            except (OSError, RuntimeError) as error:
                messages.append(f"candidate-discovery:{value}:expanduser:{error}")
    except (OSError, RuntimeError) as error:
        messages.append(f"candidate-discovery:profile_paths:iterate:{error}")
    if not values:
        try:
            values.append(set_profile_path(set_code=set_code, event_format=event_format, app_dir=app_dir))
        except (OSError, RuntimeError) as error:
            messages.append(f"candidate-discovery:default:expanduser:{error}")
    expanded: list[Path] = []
    for value in values:
        try:
            if value.is_dir():
                expanded.extend(sorted(value.glob("*.json"), key=lambda item: item.name))
            else:
                expanded.append(value)
        except (OSError, RuntimeError) as error:
            messages.append(f"candidate-discovery:{value}:directory:{error}")
    deduplicated: list[Path] = []
    seen: set[Path] = set()
    for value in expanded:
        try:
            resolved = value.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            messages.append(f"candidate-discovery:{value}:resolve:{error}")
            continue
        if resolved not in seen:
            seen.add(resolved)
            deduplicated.append(value)
    return tuple(deduplicated)


def _check_target(
    profile: SetProfile,
    *,
    expected_set_code: str | None,
    expected_format: str | None,
) -> None:
    if expected_set_code is not None and profile.set_code != _safe_component(expected_set_code, "set_code"):
        raise SetProfileSchemaError(
            f"Profile set {profile.set_code!r} does not match requested set {expected_set_code!r}."
        )
    if expected_format is not None and profile.event_format != _safe_component(expected_format, "format"):
        raise SetProfileSchemaError(
            f"Profile format {profile.event_format!r} does not match requested format {expected_format!r}."
        )


def _safe_component(value: str, field_name: str) -> str:
    normalized = _non_empty_string(value, field_name).casefold()
    if normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise SetProfileSchemaError(f"{field_name} must be a safe path component.")
    return normalized


def _pair(value: str) -> str:
    if not isinstance(value, str):
        raise SetProfileSchemaError("Color pair must be a string.")
    normalized = value.strip().upper()
    if normalized not in COLOR_PAIRS:
        raise SetProfileSchemaError(f"Unsupported color pair {value!r}.")
    return normalized


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SetProfileSchemaError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _required_string(value: Mapping[str, Any], key: str, field_name: str) -> str:
    if key not in value:
        raise SetProfileSchemaError(f"Missing required field {field_name}.")
    return _non_empty_string(value[key], field_name)


def _optional_string(value: Mapping[str, Any], key: str, field_name: str) -> str | None:
    if key not in value or value[key] is None:
        return None
    return _non_empty_string(value[key], field_name)


def _required_int(value: Mapping[str, Any], key: str, field_name: str) -> int:
    if key not in value or isinstance(value[key], bool) or not isinstance(value[key], int):
        raise SetProfileSchemaError(f"{field_name} must be an integer.")
    return value[key]


def _required_number(value: Mapping[str, Any], key: str, field_name: str) -> float:
    if key not in value:
        raise SetProfileSchemaError(f"{field_name} must be a number.")
    return _finite_number(value[key], field_name)


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SetProfileSchemaError(f"{field_name} must be a number.")
    number = float(value)
    if not math.isfinite(number):
        raise SetProfileSchemaError(f"{field_name} must be finite.")
    return number


def _finite_non_negative(value: Any, field_name: str) -> float:
    number = _finite_number(value, field_name)
    if number < 0:
        raise SetProfileSchemaError(f"{field_name} must not be negative.")
    return number


def _bounded_number(value: Any, field_name: str) -> float:
    number = _finite_number(value, field_name)
    if not 0 <= number <= 1:
        raise SetProfileSchemaError(f"{field_name} must be from 0 to 1.")
    return number


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SetProfileSchemaError(f"{field_name} must be a non-negative integer.")
    return value


def _parse_datetime(value: str, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SetProfileSchemaError(f"{field_name} must be an ISO-8601 timestamp.") from error


def _object(value: Any, field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise SetProfileSchemaError(f"{field_name} must be an object.")


def _required_mapping(value: Mapping[str, Any], key: str, field_name: str) -> Mapping[str, Any]:
    if key not in value:
        raise SetProfileSchemaError(f"Missing required field {field_name}.")
    nested = value[key]
    _object(nested, field_name)
    return nested


def _mapping_items(value: list[Any], field_name: str) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SetProfileSchemaError(f"{field_name}[{index}] must be an object.")
        result.append(item)
    return tuple(result)


def _array_of(
    value: Mapping[str, Any],
    key: str,
    parser: Any,
    field_name: str,
) -> tuple[Any, ...]:
    nested = value.get(key, [])
    if nested is None:
        return ()
    if not isinstance(nested, list):
        raise SetProfileSchemaError(f"{field_name} must be an array.")
    return tuple(parser(item) for item in _mapping_items(nested, field_name))


def _sorted_unique(values: Iterable[Any], *, key: Any, field_name: str) -> tuple[Any, ...]:
    result = tuple(sorted(values, key=key))
    if len({key(item) for item in result}) != len(result):
        raise SetProfileSchemaError(f"{field_name} contains duplicate entries.")
    return result



def _json_bytes(value: Mapping[str, Any]) -> bytes:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{serialized}\n".encode("utf-8")
