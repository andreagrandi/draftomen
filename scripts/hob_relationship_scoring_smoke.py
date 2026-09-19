"""Prove the calibrated HOB relationship scoring offline against one persisted draft.
Run it without network access or an API key; it prints one canonical JSON report or a failure line.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from draftomen.backtest import generate_backtest_report, load_persisted_backtest_state
from draftomen.carddb import CardDatabase
from draftomen.events import EXPECTED_PICKS_PER_PACK
from draftomen.pickengine import (
    MAX_CONTEXTUAL_ADJUSTMENT,
    MAX_SYNERGY_TERM,
    _RELATIONSHIP_OUTCOME_FACTORS,
    _RELATIONSHIP_SUPPORT_FACTORS,
    _TERM_BOUNDS,
)
from draftomen.pool import DraftState, save_draft_state
from draftomen.set_card_data import SetCardData
from draftomen.set_profile import load_set_profile

REPO_ROOT = Path(__file__).resolve().parents[1]
CARD_ARTIFACT_RELATIVE_PATH = Path("website/public/card-data/hob.json.gz")
PROFILE_RELATIVE_PATH = Path("tests/fixtures/hob-relationship-scoring-profile.json")
STATE_RELATIVE_PATH = Path("tests/fixtures/hob-relationship-scoring-state.json")
CARD_ARTIFACT_SHA256 = "8831da97958741163a6d4b93d5068c28a44d8146ed476c9a76618cc91d0ae7b7"
EXPECTED_SET_CODE = "hob"
EXPECTED_SET_NAME = "The Hobbit"
R1_FINDING_ID = (
    "relationship:token-go-wide-payoff:103382:103382-token-maker:103526:103526-go-wide-payoff-1"
)
R6_FINDING_ID = (
    "relationship:token-sacrifice-outlet:103531:103531-3:103458:103458-sacrifice-outlet"
)
R1_MECHANISM = "token-go-wide-payoff"
R6_MECHANISM = "token-sacrifice-outlet"
R1_RAW_SCORE_DELTA = 0.0
R6_RAW_SCORE_DELTA = 0.205793
ROW_NAMES = ("unsupported", "r1", "r6", "saturation")
EXPECTED_RECOMMENDED_GRP_IDS = {
    "unsupported": 103526,
    "r1": 103526,
    "r6": 103458,
    "saturation": 103526,
}
EXPECTED_ROW_COORDINATES = {
    "unsupported": (0, 4),
    "r1": (0, 5),
    "r6": (0, 6),
    "saturation": (2, 13),
}
CONTEXTUAL_REASON_KINDS = frozenset(_TERM_BOUNDS)
R1_PROJECTION_NOTE = (
    "R1's typed target clause encodes Bard's Company's flash-condition requirement as "
    "control of a creature you control, omitting the stated Human subtype; that clause is "
    "the closest statable proxy for the card's payoff requirement that other creatures you "
    "control be boosted, which the closed prerequisite vocabulary cannot express because "
    "the vocabulary excludes the ability source from other-creature clauses. The drafted "
    "Dwarf token maker could never satisfy a faithful Human subtype, while the reviewed "
    "synergy does not depend on that subtype."
)
FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompt_id",
        "prompt_sha256",
        "response",
        "response_schema_id",
        "response_schema_sha256",
        "claim",
        "guide",
        "guide_evidence",
        "oracle_text",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cost_usd",
        "model",
        "provider",
    }
)
FORBIDDEN_ORACLE_QUOTES = (
    "Whenever Fíli or another nontoken Dwarf you control enters",
    "Other creatures you control get +1/+1",
    "At the beginning of your upkeep, create a 2/2 green Wolf creature token",
    "you may sacrifice another creature",
)


def _fail(reason: str) -> int:
    print(f"HOB relationship scoring smoke failed: {reason}", file=sys.stderr)
    return 1


def _load_card_database() -> tuple[Path, str, CardDatabase]:
    artifact_path = REPO_ROOT / CARD_ARTIFACT_RELATIVE_PATH
    payload = artifact_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != CARD_ARTIFACT_SHA256:
        raise ValueError(f"card artifact sha256 {digest} does not match {CARD_ARTIFACT_SHA256}")
    data = SetCardData.from_gzip_bytes(
        payload,
        expected_set_code=EXPECTED_SET_CODE,
        expected_set_name=EXPECTED_SET_NAME,
    )
    return artifact_path, digest, data.to_card_database()


def _project_report(report) -> dict[str, object]:
    """Project one report into the canonical allowlisted comparison shape."""
    rows: list[dict[str, object]] = []
    for row in report.rows:
        recommended = row.recommended
        if recommended is None:
            raise ValueError("every calibration row must produce a recommendation")
        breakdown = recommended.contextual_breakdown
        rows.append(
            {
                "pack_number": row.pack_number,
                "pick_number": row.pick_number,
                "global_pick_index": (
                    row.pack_number * EXPECTED_PICKS_PER_PACK + row.pick_number + 1
                ),
                "recommended_grp_id": recommended.card.grp_id,
                "raw_score": recommended.raw_score,
                "score": recommended.score,
                "contextual_breakdown": breakdown.to_json(),
                "contextual_evidence": list(row.contextual_evidence),
                "rationale_reasons": [
                    reason.to_json() for reason in recommended.rationale.reasons
                ],
                "relationship_support": [
                    _safe_relationship_record(payload=support.to_json())
                    for support in row.role_ledger.relationship_support
                ]
                if row.role_ledger is not None
                else [],
                "relationship_contributions": [
                    _safe_relationship_record(payload=contribution.to_json())
                    for contribution in row.relationship_contributions
                ],
            }
        )
    return {"ranking_mode": report.ranking_mode, "rows": rows}


def _safe_relationship_record(*, payload: object) -> object:
    """Remove verbatim Oracle quotes from an offline report record."""

    if isinstance(payload, list):
        return [_safe_relationship_record(payload=item) for item in payload]
    if isinstance(payload, dict):
        return {
            key: _safe_relationship_record(payload=value)
            for key, value in payload.items()
            if key != "quote"
        }
    return payload


def _project_row_offers(
    *,
    projected_rows: list[dict[str, object]],
    state: DraftState,
) -> None:
    """Attach the authoritative saved offers and pools to the projected rows."""
    for projected, pick in zip(projected_rows, state.picks):
        projected["offered_grp_ids"] = (
            list(pick.offered_grp_ids) if pick.offered_grp_ids is not None else None
        )
        projected["pool_before_pick"] = (
            list(pick.pool_before_pick) if pick.pool_before_pick is not None else None
        )
        projected["chosen_grp_id"] = pick.chosen_grp_id



def _control_projections(
    *,
    state: DraftState,
    card_database,
    profile,
) -> dict[str, list[dict[str, object]]]:
    removed_profile = replace(profile, enhancement=None)
    controls = {
        "enhanced": (profile, True),
        "enhancement_removed": (removed_profile, True),
        "context_disabled": (profile, False),
    }
    projections: dict[str, list[dict[str, object]]] = {}
    for name, (control_profile, contextual_enabled) in controls.items():
        first = generate_backtest_report(
            state=state,
            card_database=card_database,
            set_profile=control_profile,
            contextual_adjustments_enabled=contextual_enabled,
        )
        second = generate_backtest_report(
            state=state,
            card_database=card_database,
            set_profile=control_profile,
            contextual_adjustments_enabled=contextual_enabled,
        )
        first_projection = _project_report(first)
        if first_projection != _project_report(second):
            raise ValueError(f"{name} control is not deterministic across repeated runs")
        projections[name] = first_projection["rows"]
        if len(projections[name]) != len(ROW_NAMES):
            raise ValueError(
                f"{name} control projected {len(projections[name])} rows, "
                f"expected {len(ROW_NAMES)}"
            )
        _project_row_offers(projected_rows=projections[name], state=state)
    return projections


def _factor_records() -> list[dict[str, object]]:
    return [
        {
            "mechanism": mechanism,
            "factor": _RELATIONSHIP_SUPPORT_FACTORS[mechanism],
            "evidence": (
                "hob-observed"
                if mechanism in (R1_MECHANISM, R6_MECHANISM)
                else "retained-unobserved"
            ),
        }
        for mechanism in sorted(_RELATIONSHIP_SUPPORT_FACTORS)
    ]


def _outcome_factor_records() -> list[dict[str, object]]:
    return [
        {
            "outcome": outcome.value,
            "factor": _RELATIONSHIP_OUTCOME_FACTORS[outcome],
        }
        for outcome in sorted(
            _RELATIONSHIP_OUTCOME_FACTORS,
            key=lambda item: item.value,
        )
    ]


def _reason_evidence_text(*, reason: dict[str, object]) -> str:
    """Join one rationale reason's retained evidence into a single scan string."""
    evidence = reason["evidence"]
    if evidence is None:
        return ""
    if isinstance(evidence, str):
        return evidence
    return " ".join(str(item) for item in evidence)


def _check_row(
    *,
    name: str,
    enhanced: dict[str, object],
    removed: dict[str, object],
    disabled: dict[str, object],
) -> dict[str, object]:
    expected = EXPECTED_RECOMMENDED_GRP_IDS[name]
    expected_coordinates = EXPECTED_ROW_COORDINATES[name]
    for control_name, row in (
        ("enhanced", enhanced),
        ("enhancement_removed", removed),
        ("context_disabled", disabled),
    ):
        if (row["pack_number"], row["pick_number"]) != expected_coordinates:
            raise ValueError(
                f"{name} row {control_name} coordinates "
                f"({row['pack_number']}, {row['pick_number']}) "
                f"do not match {expected_coordinates}"
            )
        if row["recommended_grp_id"] != expected:
            raise ValueError(
                f"{name} row {control_name} recommended "
                f"{row['recommended_grp_id']}, expected {expected}"
            )
    enhanced_breakdown = enhanced["contextual_breakdown"]
    removed_breakdown = removed["contextual_breakdown"]
    disabled_breakdown = disabled["contextual_breakdown"]
    for control_name, breakdown in (
        ("enhanced", enhanced_breakdown),
        ("enhancement_removed", removed_breakdown),
        ("context_disabled", disabled_breakdown),
    ):
        aggregate = breakdown["aggregate"]
        if abs(aggregate) > MAX_CONTEXTUAL_ADJUSTMENT:
            raise ValueError(f"{name} row {control_name} aggregate {aggregate} exceeds the cap")
        for term, value in breakdown.items():
            if term == "aggregate":
                continue
            lower, upper = _TERM_BOUNDS[term]
            if not lower <= value <= upper:
                raise ValueError(
                    f"{name} row {control_name} term {term}={value} exceeds its bound"
                )
    synergy_delta = round(enhanced_breakdown["synergy"] - removed_breakdown["synergy"], 6)
    raw_delta = round(enhanced["raw_score"] - removed["raw_score"], 6)
    if disabled_breakdown != {
        "role": 0.0,
        "urgency": 0.0,
        "synergy": 0.0,
        "redundancy": 0.0,
        "unsupported_payoff": 0.0,
        "fixing": 0.0,
        "aggregate": 0.0,
    }:
        raise ValueError(f"{name} row context_disabled breakdown is not zero")
    if disabled["contextual_evidence"]:
        raise ValueError(f"{name} row context_disabled carries contextual evidence")
    if any(
        reason["kind"] in CONTEXTUAL_REASON_KINDS
        for reason in disabled["rationale_reasons"]
    ):
        raise ValueError(f"{name} row context_disabled carries a contextual rationale")

    supports = enhanced["relationship_support"]
    finding_ids = {support["finding_id"] for support in supports}
    if name == "unsupported":
        if supports or synergy_delta != 0.0 or raw_delta != 0.0:
            raise ValueError("unsupported row must stay neutral without support")
    elif name == "r1":
        if R1_FINDING_ID not in finding_ids:
            raise ValueError("r1 row lacks the reviewed token-go-wide-payoff support")
        if synergy_delta != R1_RAW_SCORE_DELTA or raw_delta != R1_RAW_SCORE_DELTA:
            raise ValueError(
                f"r1 row delta {raw_delta} does not match the calibrated {R1_RAW_SCORE_DELTA}"
            )
        _check_relationship_row(
            name=name,
            enhanced=enhanced,
            finding_id=R1_FINDING_ID,
            mechanism=R1_MECHANISM,
            source_card_id=103382,
            target_card_id=103526,
            expected_effective_contribution=0.0,
        )
    elif name == "r6":
        if R6_FINDING_ID not in finding_ids:
            raise ValueError("r6 row lacks the reviewed token-sacrifice-outlet support")
        if synergy_delta != R6_RAW_SCORE_DELTA or raw_delta != R6_RAW_SCORE_DELTA:
            raise ValueError(
                f"r6 row delta {raw_delta} does not match the calibrated {R6_RAW_SCORE_DELTA}"
            )
        _check_relationship_row(
            name=name,
            enhanced=enhanced,
            finding_id=R6_FINDING_ID,
            mechanism=R6_MECHANISM,
            source_card_id=103531,
            target_card_id=103458,
            expected_effective_contribution=R6_RAW_SCORE_DELTA,
        )
    else:
        if enhanced_breakdown["synergy"] != MAX_SYNERGY_TERM:
            raise ValueError("saturation row synergy does not reach the cap")
        if removed_breakdown["synergy"] != MAX_SYNERGY_TERM:
            raise ValueError("saturation row enhancement-removed synergy is not at the cap")
        if synergy_delta != 0.0 or raw_delta != 0.0:
            raise ValueError("saturation row must not change the capped score")
        targeted = [
            support
            for support in supports
            if support["finding_id"] == R1_FINDING_ID
            and support["target"]["grp_id"] == enhanced["recommended_grp_id"]
        ]
        if len(targeted) != 1:
            raise ValueError(
                "saturation row lacks R1 support for its recommended candidate"
            )
        if any(
            R1_FINDING_ID in _reason_evidence_text(reason=reason)
            for reason in enhanced["rationale_reasons"]
        ):
            raise ValueError("saturation row rationale must stay generic at the cap")
        contributions = enhanced["relationship_contributions"]
        if (
            len(contributions) != 1
            or contributions[0]["finding_id"] != R1_FINDING_ID
            or contributions[0]["effective_contribution"] != 0.0
        ):
            raise ValueError("saturation row must retain zero-effective R1 provenance")

    return {
        "controls": {
            "enhanced": _control_row(enhanced),
            "enhancement_removed": _control_row(removed),
            "context_disabled": _control_row(disabled),
        },
        "deltas": {"raw_score": raw_delta, "synergy": synergy_delta},
    }


def _check_relationship_row(
    *,
    name: str,
    enhanced: dict[str, object],
    finding_id: str,
    mechanism: str,
    source_card_id: int,
    target_card_id: int,
    expected_effective_contribution: float,
) -> None:
    matching = [
        support
        for support in enhanced["relationship_support"]
        if support["finding_id"] == finding_id
    ]
    if len(matching) != 1:
        raise ValueError(f"{name} row does not carry exactly one reviewed support record")
    support = matching[0]
    if support["mechanism"] != mechanism:
        raise ValueError(f"{name} row support mechanism does not match the reviewed relationship")
    if (
        support["source"]["grp_id"] != source_card_id
        or support["target"]["grp_id"] != target_card_id
    ):
        raise ValueError(f"{name} row support participants do not match the reviewed pair")
    contributions = [
        contribution
        for contribution in enhanced["relationship_contributions"]
        if contribution["finding_id"] == finding_id
    ]
    if len(contributions) != 1:
        raise ValueError(f"{name} row lacks structured relationship provenance")
    contribution = contributions[0]
    if contribution["effective_contribution"] != expected_effective_contribution:
        raise ValueError(f"{name} row has the wrong effective relationship contribution")
    if contribution["outcome"] != "supported":
        raise ValueError(f"{name} row relationship outcome is not supported")
    if expected_effective_contribution > 0.0:
        evidence = " ".join(enhanced["contextual_evidence"])
        if finding_id not in evidence or mechanism not in evidence:
            raise ValueError(
                f"{name} row contextual evidence lacks the reviewed relationship"
            )
        if f"[{source_card_id}]" not in evidence:
            raise ValueError(
                f"{name} row contextual evidence lacks the drafted source card"
            )


def _control_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "recommended_grp_id": row["recommended_grp_id"],
        "raw_score": row["raw_score"],
        "score": row["score"],
        "synergy": row["contextual_breakdown"]["synergy"],
        "aggregate": row["contextual_breakdown"]["aggregate"],
        "contextual_breakdown": row["contextual_breakdown"],
        "contextual_evidence": row["contextual_evidence"],
        "rationale_reasons": row["rationale_reasons"],
        "relationship_contributions": row["relationship_contributions"],
    }


def _reject_unsafe_output(report_text: str) -> None:
    for key in FORBIDDEN_KEYS:
        if f'"{key}"' in report_text:
            raise ValueError(f"report leaks forbidden field {key}")
    for quote in FORBIDDEN_ORACLE_QUOTES:
        if quote in report_text:
            raise ValueError("report leaks relationship Oracle prose")


def generate_hob_relationship_scoring_report(*, app_dir: Path) -> dict[str, object]:
    """Run every calibration control over the tracked fixtures and project the report."""
    artifact_path, artifact_digest, card_database = _load_card_database()
    profile_path = REPO_ROOT / PROFILE_RELATIVE_PATH
    profile = load_set_profile(
        profile_path,
        expected_set_code=EXPECTED_SET_CODE,
        expected_format="quickdraft",
    )
    if profile.enhancement is None:
        raise ValueError("calibration profile must carry a compiled enhancement")
    state_path = REPO_ROOT / STATE_RELATIVE_PATH
    state = DraftState.from_json(json.loads(state_path.read_text()))
    if len(state.picks) != len(ROW_NAMES):
        raise ValueError(
            f"state fixture carries {len(state.picks)} saved picks, "
            f"expected {len(ROW_NAMES)}"
        )

    persisted_path = save_draft_state(state=state, app_dir=app_dir)
    persisted_bytes = persisted_path.read_bytes()
    reloaded = load_persisted_backtest_state(
        app_dir=app_dir,
        account_id=state.account_id,
        draft_id=state.draft_id,
    )
    if reloaded.to_json() != state.to_json():
        raise ValueError("persisted state does not reload to the same draft")

    projections = _control_projections(
        state=state,
        card_database=card_database,
        profile=profile,
    )
    rows: list[dict[str, object]] = []
    for name, enhanced, removed, disabled in zip(
        ROW_NAMES,
        projections["enhanced"],
        projections["enhancement_removed"],
        projections["context_disabled"],
    ):
        row_projection = _check_row(
            name=name,
            enhanced=enhanced,
            removed=removed,
            disabled=disabled,
        )
        rows.append(
            {
                "row": name,
                "pack_number": enhanced["pack_number"],
                "pick_number": enhanced["pick_number"],
                "global_pick_index": enhanced["global_pick_index"],
                "offered_grp_ids": enhanced["offered_grp_ids"],
                "pool_before_pick": enhanced["pool_before_pick"],
                "chosen_grp_id": enhanced["chosen_grp_id"],
                "relationship_support": enhanced["relationship_support"],
                **row_projection,
            }
        )

    if persisted_path.read_bytes() != persisted_bytes:
        raise ValueError("scoring mutated the persisted state bytes")
    if reloaded.to_json() != state.to_json():
        raise ValueError("post-run state no longer reloads to the same draft")

    enhancement = profile.enhancement
    return {
        "schema_version": 2,
        "card_artifact": {
            "path": str(CARD_ARTIFACT_RELATIVE_PATH),
            "sha256": artifact_digest,
        },
        "profile": {
            "set_code": profile.set_code,
            "format": profile.event_format,
            "version": profile.profile_version,
            "fingerprint": profile.fingerprint,
            "enhancement_status": "enhanced",
            "artifact_sha256": enhancement.artifact_sha256,
            "set_source_id": enhancement.set_source_id,
            "set_source_sha256": enhancement.set_source_sha256,
            "card_count": enhancement.card_data.card_count,
        },
        "relationship_support_factors": _factor_records(),
        "relationship_outcome_factors": _outcome_factor_records(),
        "relationship_projection_notes": [
            {"finding_id": R1_FINDING_ID, "note": R1_PROJECTION_NOTE},
        ],
        "caps": {
            "max_synergy_term": MAX_SYNERGY_TERM,
            "max_contextual_adjustment": MAX_CONTEXTUAL_ADJUSTMENT,
        },
        "rows": rows,
    }


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="hob-relationship-scoring-") as temp_dir:
            report = generate_hob_relationship_scoring_report(app_dir=Path(temp_dir))
        report_text = json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        _reject_unsafe_output(report_text)
    except Exception as error:
        return _fail(str(error))
    print(report_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
