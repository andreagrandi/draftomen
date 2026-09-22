"""Shared builders for augmented artifact tests."""

from __future__ import annotations

import gzip
import io
import json
import math
from collections.abc import Mapping
from typing import Any

from draftomen.augmented_training_data import FEATURE_NAMES

_CANDIDATE_IDS = (
    "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002",
)
_SOURCE_URL = (
    "https://17lands-public.s3.amazonaws.com/analysis_data/draft_data/"
    "draft_data_public.TST.PremierDraft.csv.gz"
)


def _source_json(*, event_type: str = "PremierDraft") -> dict[str, object]:
    return {
        "attribution": "17Lands public datasets",
        "event_type": event_type,
        "license": "CC BY 4.0",
        "retrieved_at": "2026-09-20T12:00:00+00:00",
        "sha256": "a" * 64,
        "url": _SOURCE_URL,
    }


def augmented_artifact_json(
    *,
    set_code: str = "tst",
    candidate_ids: tuple[str, ...] = _CANDIDATE_IDS,
    input_weights: list[list[float]] | None = None,
    output_weights: list[list[float]] | None = None,
    bias: list[float] | None = None,
    multiplier: float = 1.0,
    training: Mapping[str, object] | None = None,
) -> dict[str, object]:
    rows = (
        input_weights
        if input_weights is not None
        else [[0.1, 0.2] for _ in range(len(FEATURE_NAMES))]
    )
    outputs = (
        output_weights if output_weights is not None else [[0.5, -0.5], [-0.25, 0.25]]
    )
    value: dict[str, object] = {
        "calibration": {
            "candidates": [
                {
                    "mean_reciprocal_rank": 0.75,
                    "multiplier": 0.0,
                    "top_1": 0.5,
                },
                {
                    "mean_reciprocal_rank": 0.8,
                    "multiplier": 1.0,
                    "top_1": 0.6,
                },
            ],
            "maximum_delta": 8.0,
            "method": "offered_mean_centered_logit_clip",
            "minimum_delta": -8.0,
            "multiplier": multiplier,
            "selection_metric": "mean_reciprocal_rank_then_top_1",
            "selection_partition": "validation",
        },
        "compatibility": "coarse-context-model-c/v1",
        "evaluation": {
            "basic_do": {
                "mean_reciprocal_rank": 0.5,
                "picks": 4,
                "top_1": 0.25,
            },
            "basic_plus_augmented": {
                "mean_reciprocal_rank": 0.75,
                "picks": 4,
                "top_1": 0.5,
            },
        },
        "model": {
            "architecture": "bias_plus_tanh_low_rank_pool_context",
            "candidate_output_ids": list(candidate_ids),
            "feature_schema": list(FEATURE_NAMES),
            "name": "coarse_context_model_c",
            "runtime_parameters": {
                "bias": bias if bias is not None else [0.0, 0.0],
                "hidden_activation": "tanh",
                "input_transform": "log1p_counts",
                "input_weights": rows,
                "output_weights": outputs,
            },
        },
        "schema_version": 1,
        "set_code": set_code,
        "source": _source_json(),
    }
    if training is not None:
        value["training"] = dict(training)
    return value


def canonical_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def canonical_gzip_bytes(value: Mapping[str, object]) -> bytes:
    raw = canonical_bytes(value)
    stream = io.BytesIO()
    with gzip.GzipFile(
        fileobj=stream, mode="wb", filename="", mtime=0, compresslevel=9
    ) as writer:
        writer.write(raw)
    return stream.getvalue()


def augmented_artifact(set_code: str = "tst", **kwargs: Any):
    from draftomen.augmented_artifact import AugmentedArtifact

    return AugmentedArtifact.from_bytes(
        canonical_bytes(augmented_artifact_json(set_code=set_code, **kwargs))
    )


def fixed_delta_artifact_json(
    *,
    set_code: str,
    candidate_ids: tuple[str, ...],
    deltas: tuple[float, ...],
) -> dict[str, object]:
    """Return artifact JSON that applies exactly these mean-centered deltas."""

    if len(candidate_ids) != len(deltas):
        raise ValueError("candidate_ids and deltas must have the same length.")
    if not math.isclose(sum(deltas), 0.0, abs_tol=1e-9):
        raise ValueError("deltas must sum to zero to survive mean centering.")
    return augmented_artifact_json(
        set_code=set_code,
        candidate_ids=candidate_ids,
        input_weights=[[0.0] for _ in FEATURE_NAMES],
        output_weights=[[0.0] * len(candidate_ids)],
        bias=list(deltas),
        multiplier=1.0,
    )


def fixed_delta_artifact(
    *,
    set_code: str,
    candidate_ids: tuple[str, ...],
    deltas: tuple[float, ...],
):
    """Return one validated artifact that applies exactly these deltas."""

    from draftomen.augmented_artifact import AugmentedArtifact

    return AugmentedArtifact.from_bytes(
        canonical_bytes(
            fixed_delta_artifact_json(
                set_code=set_code,
                candidate_ids=candidate_ids,
                deltas=deltas,
            )
        )
    )


def augmented_manifest_entry_json(
    *,
    artifact_bytes: int = 131072,
    artifact_schema_version: int = 1,
    artifact_sha256: str = "b" * 64,
    compatibility: str = "coarse-context-model-c/v1",
    event_type: str = "PremierDraft",
) -> dict[str, object]:
    return {
        "artifact_bytes": artifact_bytes,
        "artifact_schema_version": artifact_schema_version,
        "artifact_sha256": artifact_sha256,
        "compatibility": compatibility,
        "metrics": {
            "basic_do": {"mean_reciprocal_rank": 0.61, "top_1": 0.42},
            "basic_plus_augmented": {"mean_reciprocal_rank": 0.63, "top_1": 0.44},
            "picks": 12345,
        },
        "source": _source_json(event_type=event_type),
    }


def augmented_manifest_json(
    *,
    sets: Mapping[str, Mapping[str, object]] | None = None,
    published_at: str = "2026-09-21T00:00:00+00:00",
    schema_version: int = 1,
) -> dict[str, object]:
    entries = (
        dict(sets) if sets is not None else {"hob": augmented_manifest_entry_json()}
    )
    return {
        "published_at": published_at,
        "schema_version": schema_version,
        "sets": entries,
    }
