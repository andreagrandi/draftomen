from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from draftomen.augmented_artifact import (
    AUGMENTED_ARTIFACT_MAX_DECOMPRESSED_BYTES,
    AUGMENTED_MAXIMUM_DELTA,
    AugmentedArtifact,
    AugmentedArtifactError,
    AugmentedArtifactIncompatibleError,
    pool_feature_counts,
)
from draftomen.augmented_training_data import FEATURE_NAMES
from draftomen.carddb import CardInfo
from tests.augmented_artifacts import (
    augmented_artifact,
    augmented_artifact_json,
    canonical_bytes,
    canonical_gzip_bytes,
)

BANNED_FRAMEWORKS = (
    "torch",
    "lightgbm",
    "onnxruntime",
    "tensorflow",
    "sklearn",
    "xgboost",
    "numpy",
    "polars",
    "pandas",
)


def test_canonical_round_trip_preserves_training_block() -> None:
    original = augmented_artifact(training={"seed": 7, "hidden_size": 2})
    restored = AugmentedArtifact.from_bytes(original.to_bytes())
    assert restored == original
    assert restored.training == {"seed": 7, "hidden_size": 2}
    assert restored.to_gzip_bytes() == original.to_gzip_bytes()


def test_non_canonical_json_bytes_rejected() -> None:
    raw = canonical_bytes(augmented_artifact_json())
    pretty = json.dumps(json.loads(raw.decode("utf-8")), indent=2).encode("utf-8")
    assert pretty != raw
    with pytest.raises(AugmentedArtifactError, match="not canonical"):
        AugmentedArtifact.from_bytes(pretty)


def test_wrong_checksum_rejected() -> None:
    payload = augmented_artifact().to_gzip_bytes()
    with pytest.raises(AugmentedArtifactError, match="checksum"):
        AugmentedArtifact.from_gzip_bytes(payload, expected_sha256="0" * 64)


def test_unsupported_schema_version_raises_incompatible() -> None:
    value = augmented_artifact_json()
    value["schema_version"] = 2
    with pytest.raises(AugmentedArtifactIncompatibleError):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_unknown_compatibility_raises_incompatible() -> None:
    value = augmented_artifact_json()
    value["compatibility"] = "other-model/v9"
    with pytest.raises(AugmentedArtifactIncompatibleError):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_uppercase_set_code_rejected() -> None:
    value = augmented_artifact_json(set_code="TST")
    with pytest.raises(AugmentedArtifactError, match="set_code"):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_wrong_expected_set_code_rejected() -> None:
    artifact = augmented_artifact()
    with pytest.raises(AugmentedArtifactError, match="expected 'xyz'"):
        AugmentedArtifact.from_bytes(artifact.to_bytes(), expected_set_code="xyz")
    with pytest.raises(AugmentedArtifactError, match="expected 'xyz'"):
        AugmentedArtifact.from_gzip_bytes(
            artifact.to_gzip_bytes(), expected_set_code="xyz"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("multiplier", 20.0),
        ("maximum_delta", 1e9),
        ("minimum_delta", 1.0),
        ("multiplier", float("nan")),
    ],
)
def test_invalid_calibration_rejected(field: str, value: float) -> None:
    payload = augmented_artifact_json()
    assert isinstance(payload["calibration"], dict)
    payload["calibration"][field] = value
    with pytest.raises(AugmentedArtifactError):
        AugmentedArtifact.from_bytes(canonical_bytes(payload))


def test_input_weights_with_21_rows_rejected() -> None:
    value = augmented_artifact_json()
    model = value["model"]
    assert isinstance(model, dict)
    runtime = model["runtime_parameters"]
    assert isinstance(runtime, dict)
    runtime["input_weights"] = [[0.1, 0.2]] * 21
    with pytest.raises(AugmentedArtifactError, match="input_weights"):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_bias_length_mismatch_rejected() -> None:
    value = augmented_artifact_json()
    model = value["model"]
    assert isinstance(model, dict)
    runtime = model["runtime_parameters"]
    assert isinstance(runtime, dict)
    runtime["bias"] = [0.0, 0.0, 0.0]
    with pytest.raises(AugmentedArtifactError, match="bias"):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_ragged_output_weights_rejected() -> None:
    value = augmented_artifact_json(
        output_weights=[[0.5, -0.5], [-0.25, 0.25, 0.0]]
    )
    with pytest.raises(AugmentedArtifactError, match="output_weights"):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_duplicate_candidate_ids_rejected() -> None:
    value = augmented_artifact_json(
        candidate_ids=(
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000001",
        )
    )
    with pytest.raises(AugmentedArtifactError, match="duplicates"):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_non_uuid_candidate_id_rejected() -> None:
    value = augmented_artifact_json(candidate_ids=("not-a-uuid", "alsobad"))
    with pytest.raises(AugmentedArtifactError, match="UUID"):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


@pytest.mark.parametrize(
    "field,path",
    [
        ("architecture", ("model", "architecture")),
        ("input_transform", ("model", "runtime_parameters", "input_transform")),
    ],
)
def test_unknown_model_strings_raise_incompatible(
    field: str, path: tuple[str, ...]
) -> None:
    value = augmented_artifact_json()
    node = value
    for key in path[:-1]:
        assert isinstance(node, dict)
        node = node[key]
    assert isinstance(node, dict)
    node[path[-1]] = "unknown-value"
    with pytest.raises(AugmentedArtifactIncompatibleError):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_wrong_feature_schema_raises_incompatible() -> None:
    value = augmented_artifact_json()
    model = value["model"]
    assert isinstance(model, dict)
    schema = model["feature_schema"]
    assert isinstance(schema, list)
    schema.pop()
    with pytest.raises(AugmentedArtifactIncompatibleError):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


def test_unknown_top_level_key_rejected() -> None:
    value = augmented_artifact_json()
    value["extra"] = 1
    with pytest.raises(AugmentedArtifactError, match="unsupported fields"):
        AugmentedArtifact.from_bytes(canonical_bytes(value))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_constants_rejected(constant: str) -> None:
    raw = canonical_bytes(augmented_artifact_json()).decode("utf-8")
    corrupted = raw.replace("0.1", constant, 1)
    with pytest.raises(AugmentedArtifactError):
        AugmentedArtifact.from_bytes(corrupted.encode("utf-8"))


def test_duplicate_json_keys_rejected() -> None:
    raw = canonical_bytes(augmented_artifact_json()).decode("utf-8")
    corrupted = raw.replace('"schema_version":1', '"set_code":"tst","set_code":"tst"', 1)
    with pytest.raises(AugmentedArtifactError, match="Duplicate"):
        AugmentedArtifact.from_bytes(corrupted.encode("utf-8"))


def test_candidate_deltas_match_hand_computed_expectation() -> None:
    artifact = augmented_artifact()
    pool = (1,) * len(FEATURE_NAMES)
    step = math.log1p(1)
    hidden = (
        math.tanh(22 * step * 0.1),
        math.tanh(22 * step * 0.2),
    )
    logit_a = hidden[0] * 0.5 + hidden[1] * -0.25
    logit_b = hidden[0] * -0.5 + hidden[1] * 0.25
    mean = (logit_a + logit_b) / 2
    expected = (logit_a - mean, logit_b - mean)
    deltas = artifact.candidate_deltas(
        pool_features=list(pool),
        candidate_ids=[
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        ],
    )
    assert deltas == pytest.approx(expected)
    assert all(abs(item) <= AUGMENTED_MAXIMUM_DELTA for item in deltas)


def test_candidate_deltas_unknown_ids_receive_zero() -> None:
    artifact = augmented_artifact()
    pool = (0,) * len(FEATURE_NAMES)
    deltas = artifact.candidate_deltas(
        pool_features=list(pool),
        candidate_ids=[
            "00000000-0000-0000-0000-000000000001",
            "not-a-uuid",
            None,
        ],
    )
    assert deltas[0] == 0.0
    assert deltas[1] == 0.0
    assert deltas[2] == 0.0


def test_candidate_deltas_empty_pack_returns_zeros() -> None:
    artifact = augmented_artifact()
    pool = (0,) * len(FEATURE_NAMES)
    deltas = artifact.candidate_deltas(
        pool_features=list(pool), candidate_ids=["not-a-uuid", None]
    )
    assert deltas == (0.0, 0.0)


def test_candidate_deltas_rejects_short_pool() -> None:
    artifact = augmented_artifact()
    with pytest.raises(AugmentedArtifactError, match="pool_features"):
        artifact.candidate_deltas(
            pool_features=[0] * 21,
            candidate_ids=["00000000-0000-0000-0000-000000000001"],
        )


def test_pool_feature_counts_cover_blue_two_drop_creature() -> None:
    card = CardInfo(
        grp_id=1,
        name="Blue Two Drop",
        colors=("U",),
        mana_value=2.0,
        rarity="common",
        types=("Creature",),
        type_line="Creature — Wizard",
        set_code="tst",
        oracle_id="00000000-0000-0000-0000-000000000001",
    )
    counts = pool_feature_counts(pool_cards=[card])
    assert len(counts) == len(FEATURE_NAMES)
    assert counts[FEATURE_NAMES.index("color:U")] == 1
    assert counts[FEATURE_NAMES.index("color:W")] == 0
    assert counts[FEATURE_NAMES.index("composition:Creature")] == 1
    assert counts[FEATURE_NAMES.index("composition:Noncreature")] == 0
    assert counts[FEATURE_NAMES.index("curve:2")] == 1
    assert counts[FEATURE_NAMES.index("type:Creature")] == 1


def test_loading_and_inference_import_no_training_framework(tmp_path: Path) -> None:
    payload = augmented_artifact().to_gzip_bytes()
    target = tmp_path / "augmented" / "tst.json.gz"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from draftomen.augmented_artifact import AugmentedArtifact\n"
        "artifact = AugmentedArtifact.from_gzip_bytes("
        f"Path({str(target)!r}).read_bytes(), expected_set_code='tst')\n"
        "deltas = artifact.candidate_deltas("
        "pool_features=[1] * 22, "
        "candidate_ids=['00000000-0000-0000-0000-000000000001', None])\n"
        "banned = sorted(name for name in "
        f"{list(BANNED_FRAMEWORKS)!r} if name in sys.modules)\n"
        "print(json.dumps({'banned': banned, 'deltas': list(deltas)}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["banned"] == []
    assert all(abs(item) <= AUGMENTED_MAXIMUM_DELTA for item in result["deltas"])


def test_project_dependencies_exclude_training_frameworks() -> None:
    text = Path("pyproject.toml").read_bytes().decode("utf-8")
    parsed = tomllib.loads(text)
    dependencies = parsed["project"]["dependencies"]
    joined = "\n".join(dependencies).casefold()
    for name in BANNED_FRAMEWORKS:
        assert name not in joined


def test_expected_byte_size_mismatch_rejected() -> None:
    artifact = augmented_artifact()
    payload = artifact.to_gzip_bytes()
    with pytest.raises(AugmentedArtifactError, match="byte count"):
        AugmentedArtifact.from_gzip_bytes(
            payload, expected_byte_size=len(artifact.to_bytes()) + 1
        )


def test_gzip_container_canonical_check_rejected() -> None:
    import gzip as gzip_module
    import io as io_module

    raw = augmented_artifact().to_bytes()
    stream = io_module.BytesIO()
    with gzip_module.GzipFile(
        fileobj=stream, mode="wb", filename="custom-name", mtime=123, compresslevel=6
    ) as writer:
        writer.write(raw)
    payload = stream.getvalue()
    with pytest.raises(AugmentedArtifactError, match="canonical"):
        AugmentedArtifact.from_gzip_bytes(
            payload, max_decompressed_bytes=AUGMENTED_ARTIFACT_MAX_DECOMPRESSED_BYTES
        )


def test_sha256_expected_value_matches_payload() -> None:
    artifact = augmented_artifact()
    payload = artifact.to_gzip_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    restored = AugmentedArtifact.from_gzip_bytes(payload, expected_sha256=digest)
    assert restored == artifact
    assert canonical_gzip_bytes(augmented_artifact_json()) == payload
