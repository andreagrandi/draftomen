#!/usr/bin/env python3
"""ARCHIVED HISTORICAL PROBE: targets the retired pre-#663 schema-3 profile compiler. This script is not runnable against current code and is not evidence of current runtime behavior.

The command below is preserved only as a historical capture record. Do not use it to verify current behavior.

Read-only.  Enforces network denial inside the process, imports no provider
client, and writes no audited input.  It traces saved paid-run evidence through
the real compiler, the generated profile representation, pool support, and the
offscreen QML explanation.

Historical reproduction command from the capture (not runnable against current code):

    QT_QPA_PLATFORM=offscreen uv run --no-sync python \
        docs/audits/hob_587_trace_probe.py --out /tmp/hob-587-trace-out.json

Stages are independent.  A stage that cannot run reports "blocked" with its
exception; a stage that reports byte-level facts without the runtime object
reports "partial".  Exit code 0 whenever the structured report was written.

Load path notes (probe revision 4; unchanged since revision 3):
  * The artifact is loaded with the frozen guide from `sources/guide.json`, the
    same coverage the production validator requires
    (draftomen/semantic_enrichment.py:332-341).  Revision 1 passed `guides=()`
    and every stage was correctly rejected.
  * When the artifact cannot be loaded, the pool-support and rendered-advice
    stages fall back to the enhancement already compiled inside the installed
    profile, and the report labels that origin.
  * `_deny_network()` fails getaddrinfo, create_connection and socket.connect
    for the life of the process instead of trusting that no client is built.
  * The rendered stage reads back both the first row and the first row that
    carries relationship support, records the label's theme-derived font size
    and colour, keeps the `guiPreferences` adapter alive for the whole stage,
    and destroys the Qt objects in an explicit teardown phase so Qt messages
    (including teardown warnings) are recorded with the phase that produced
    them.

This is the checked-in copy of the executed revision 3 (the executed copy lived
at /tmp/hob_587_trace_probe.py, sha256 79bb6834...), revised to 4 for issue
#589: the generated-representation classifier now runs the production
zone-supply gate itself and reports the compiler's own per-relationship
conversion outcomes, so the revision-3 probe hashes no longer describe this
file.  The default paths are unchanged from revision 3: REPO_ROOT is derived
from this file's location and RUN_DIR from the home directory, while
DRAFTOMEN_REPO, HOB587_RUN_DIR and HOB587_INSTALLED_PROFILE still override them."""


from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import traceback
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(os.environ.get("DRAFTOMEN_REPO", str(Path(__file__).resolve().parents[2])))
RUN_DIR = Path(
    os.environ.get(
        "HOB587_RUN_DIR",
        str(
            Path.home()
            / ".draftomen/set-enrichment/hob-quickdraft/enrichment-runs/hob"
            / "9574d202eef14943"
        ),
    )
)
INSTALLED_PROFILE = Path(
    os.environ.get(
        "HOB587_INSTALLED_PROFILE",
        str(Path.home() / ".draftomen/set-profiles/hob-quickdraft.json"),
    )
)

ARTIFACT_NAME = "edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84.json"
EXPECTED = {
    "run_manifest_sha256": "c518992a1735bfbfe358eaa5db9158865740b8f62dd90e11f5a86b69d2c24cb5",
    "artifact_sha256": "edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84",
    "card_database_sha256": "70ebacdfa4bd8e45f485a6bf7392daf1f754414e0716c6587fcc9e8dc419b9b9",
    "guide_sha256": "bd176ad3d3666822b98f68cb5f2ae67a0fc7d35c171d7816c266a741a49ac795",
    "guide_text_sha256": "537de10ab83704b308d78efd21a77f9faf93b71ca1e24422de445aee3de30cbf",
    "card_artifact_sha256": "8831da97958741163a6d4b93d5068c28a44d8146ed476c9a76618cc91d0ae7b7",
    "fixture_profile_sha256": "2407a8b64e24f884564a252b933b05cac0acce6d5f337c5da93bd21516b24a81",
    "fixture_state_sha256": "8a284c1fc0c2b8f80c29c9c06eed387e7d6ca24d3057193c63590780b6499fd2",
    "installed_profile_sha256": "c73802bf419756245d88d619b99bb9c638e06f19b283023284be7984f6281d24",
}
INSTALLED_PROFILE_EXPECTED_PROJECTIONS = (
    "local-c7a4f08030a1bfb7:relationship:mill-graveyard-payoff:103546:"
    "103546-f1-self-mill:103422:103422-0-threshold-graveyard-payoff",
    "local-c7a4f08030a1bfb7:relationship:mill-graveyard-payoff:103556:"
    "103556-f1-mill-self:103422:103422-0-threshold-graveyard-payoff",
    "local-c7a4f08030a1bfb7:relationship:recursion-graveyard-payoff:103442:"
    "103442-return-creature-card:103422:103422-0-threshold-graveyard-payoff",
)
R1_FINDING_ID = (
    "relationship:token-go-wide-payoff:103382:103382-token-maker:"
    "103526:103526-go-wide-payoff-1"
)
R6_FINDING_ID = (
    "relationship:token-sacrifice-outlet:103531:103531-3:"
    "103458:103458-sacrifice-outlet"
)

QT_PHASE: dict[str, str] = {"name": "startup"}
QT_MESSAGES: dict[str, list[str]] = {}
QML_HOLD: list[object] = []


def install_qt_message_capture() -> None:
    """Record Qt messages with the probe phase that produced them."""
    from PySide6.QtCore import qInstallMessageHandler

    def handler(_mode: object, _context: object, message: str) -> None:
        try:
            QT_MESSAGES.setdefault(QT_PHASE["name"], []).append(str(message).strip())
        except Exception:  # noqa: BLE001 - a logging handler must never raise
            pass

    qInstallMessageHandler(handler)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tree_digest(root: Path) -> dict[str, object]:
    """Reproduce the documented manifest method: sorted `sha256  ./rel` lines."""
    lines: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = sha256_bytes(path.read_bytes())
        lines.append(f"{digest}  ./{path.relative_to(root).as_posix()}\n")
    manifest = "".join(lines).encode("utf-8")
    return {"sha256": sha256_bytes(manifest), "files": len(lines)}


def deny_network() -> dict[str, object]:
    """Fail every outbound socket operation for the life of this process."""
    blocked: list[str] = []

    def _deny(operation: str):
        def _raise(*_args: object, **_kwargs: object) -> object:
            blocked.append(operation)
            raise OSError(f"hob-587 probe: network access denied ({operation})")

        return _raise

    socket.getaddrinfo = _deny("getaddrinfo")  # type: ignore[assignment]
    socket.create_connection = _deny("create_connection")  # type: ignore[assignment]
    original_socket = socket.socket

    class _DeniedSocket(original_socket):  # type: ignore[misc, valid-type]
        def connect(self, *_args: object, **_kwargs: object) -> object:
            blocked.append("socket.connect")
            raise OSError("hob-587 probe: network access denied (socket.connect)")

        def connect_ex(self, *_args: object, **_kwargs: object) -> object:
            blocked.append("socket.connect_ex")
            raise OSError("hob-587 probe: network access denied (socket.connect_ex)")

    socket.socket = _DeniedSocket  # type: ignore[assignment]
    return {
        "installed": True,
        "mechanism": "socket.getaddrinfo, socket.create_connection, socket.connect and socket.connect_ex raise",
        "blocked_attempts": blocked,
    }


def blocked(stage: str, error: BaseException) -> dict[str, object]:
    return {
        "status": "blocked",
        "stage": stage,
        "error": f"{type(error).__name__}: {error}",
        "traceback_tail": traceback.format_exc().strip().splitlines()[-3:],
    }


class Report:
    def __init__(self) -> None:
        self.data: dict[str, object] = {
            "probe": "hob-587-trace",
            "probe_revision": 4,
            "schema_version": 1,
            "run_dir": str(RUN_DIR),
            "context": {},
            "stages": {},
            "blockers": [],
        }

    def stage(self, name: str, value: dict[str, object]) -> None:
        self.data["stages"][name] = value  # type: ignore[index]
        if value.get("status") == "blocked":
            self.data["blockers"].append(  # type: ignore[attr-defined]
                {"stage": name, "error": value.get("error")}
            )


# --------------------------------------------------------------------------- #
# shared context
# --------------------------------------------------------------------------- #


class Context:
    """Inputs every stage shares, each with its own load status."""

    def __init__(self) -> None:
        self.card_database = None
        self.card_database_facts: dict[str, object] = {}
        self.guide_source = None
        self.guide_facts: dict[str, object] = {}
        self.sources = None
        self.artifact = None
        self.artifact_facts: dict[str, object] = {}
        self.artifact_error: str | None = None
        self.enhancement = None
        self.enhancement_origin: str | None = None
        self.enhancement_error: str | None = None

    def require(self, name: str):
        value = getattr(self, name)
        if value is None:
            raise RuntimeError(f"context.{name} is unavailable")
        return value


def load_card_database(context: Context) -> None:
    from draftomen.set_card_data import SetCardData

    path = REPO_ROOT / "website/public/card-data/hob.json.gz"
    payload = path.read_bytes()
    data = SetCardData.from_gzip_bytes(
        payload,
        expected_set_code="hob",
        expected_set_name="The Hobbit",
    )
    context.card_database = data.to_card_database()
    context.card_database_facts = {
        "path": str(path),
        "sha256": sha256_bytes(payload),
        "digest_matches_audit": sha256_bytes(payload) == EXPECTED["card_artifact_sha256"],
        "cards": len(context.card_database.cards),
        "unknown_cards": sum(1 for card in context.card_database.cards.values() if card.unknown),
    }


def load_guide_source(context: Context) -> None:
    """Build the frozen guide exactly as the production load path requires."""
    from draftomen.semantic_enrichment import GuideSource

    path = RUN_DIR / "sources/guide.json"
    payload = path.read_bytes()
    record = json.loads(payload.decode("utf-8"))
    context.guide_source = GuideSource(
        guide_id=record["guide_id"],
        url=record["url"],
        text=record["text"],
        retrieved_at=record["retrieved_at"],
    )
    context.guide_facts = {
        "path": str(path),
        "sha256": sha256_bytes(payload),
        "digest_matches_audit": sha256_bytes(payload) == EXPECTED["guide_sha256"],
        "guide_id": context.guide_source.guide_id,
        "url": context.guide_source.url,
        "retrieved_at": context.guide_source.retrieved_at,
        "text_sha256": context.guide_source.text_sha256,
        "text_sha256_matches_pin": context.guide_source.text_sha256
        == EXPECTED["guide_text_sha256"],
        "chars": len(context.guide_source.text),
    }


def build_sources(context: Context) -> None:
    from draftomen.semantic_enrichment import EnrichmentSources

    card_database = context.require("card_database")
    guide = context.require("guide_source")
    context.sources = EnrichmentSources(
        set_code="hob",
        cards=tuple(card_database.cards.values()),
        guides=(guide,),
    )


def load_artifact(context: Context) -> None:
    from draftomen.semantic_enrichment import SemanticEnrichmentArtifact

    artifact_path = RUN_DIR / "artifacts" / ARTIFACT_NAME
    payload = artifact_path.read_bytes()
    digest = sha256_bytes(payload)
    artifact = SemanticEnrichmentArtifact.from_bytes(payload, sources=context.require("sources"))
    run_ids = sorted({relationship.run_id for relationship in artifact.relationships})
    context.artifact = artifact
    context.artifact_facts = {
        "path": str(artifact_path),
        "sha256": digest,
        "digest_matches_audit": digest == EXPECTED["artifact_sha256"],
        "filename_is_digest": digest == ARTIFACT_NAME.removesuffix(".json"),
        "bytes_are_canonical": artifact.to_bytes() == payload,
        "cards": len(artifact.cards),
        "guide_pins": [
            {
                "guide_id": pin.guide_id,
                "url": pin.url,
                "retrieved_at": pin.retrieved_at,
                "sha256": pin.sha256,
            }
            for pin in artifact.guides
        ],
        "guide_pin_covered_by_frozen_source": all(
            pin.guide_id == context.guide_source.guide_id
            and pin.url == context.guide_source.url
            and pin.retrieved_at == context.guide_source.retrieved_at
            and pin.sha256 == context.guide_source.text_sha256
            for pin in artifact.guides
        ),
        "review_state": artifact.review.state,
        "set_source_sha256": artifact.set_source_sha256,
        "oracle_facts": len(artifact.oracle_facts),
        "relationships": len(artifact.relationships),
        "stored_projections": sum(
            1
            for relationship in artifact.relationships
            if relationship.prerequisite_projection is not None
        ),
        "relationship_run_ids": run_ids,
    }


def load_enhancement(context: Context) -> None:
    """Prefer the compiled artifact; fall back to the installed profile."""
    from draftomen.profile_enhancement import compile_profile_enhancement
    from draftomen.set_profile import load_set_profile

    if context.artifact is not None:
        try:
            compiled_enhancement = compile_profile_enhancement(
                artifact=context.artifact,
                set_code="hob",
                card_database=context.require("card_database"),
            )
            context.enhancement = compiled_enhancement.enhancement
            context.enhancement_origin = "artifact_compile"
            return
        except BaseException as error:  # noqa: BLE001 - fallback is recorded
            context.enhancement_error = f"{type(error).__name__}: {error}"
    if INSTALLED_PROFILE.exists():
        profile = load_set_profile(
            INSTALLED_PROFILE, expected_set_code="hob", expected_format="quickdraft"
        )
        if profile.enhancement is not None:
            context.enhancement = profile.enhancement
            context.enhancement_origin = "installed_profile"
            return
    if context.enhancement_error is None:
        context.enhancement_error = "no artifact and no installed profile enhancement"


# --------------------------------------------------------------------------- #
# stage A: saved evidence -> artifact
# --------------------------------------------------------------------------- #


def stage_saved_evidence(context: Context) -> dict[str, object]:
    work = RUN_DIR / "work"
    kind_totals: dict[str, dict[str, int]] = {}
    outcomes: dict[str, int] = {}
    relationship_units = 0
    relationship_units_accepted = 0
    for path in sorted((work / "results").glob("*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        kind = str(result.get("identity", {}).get("work_kind"))
        payload = result.get("result", {})
        bucket = kind_totals.setdefault(
            kind, {"files": 0, "accepted": 0, "rejected": 0, "uncertain": 0}
        )
        bucket["files"] += 1
        for key, label in (
            ("accepted_capabilities", "accepted"),
            ("accepted_findings", "accepted"),
            ("rejected_capabilities", "rejected"),
            ("rejected_findings", "rejected"),
            ("rejected", "rejected"),
            ("uncertain_capabilities", "uncertain"),
            ("uncertain_findings", "uncertain"),
        ):
            value = payload.get(key)
            if isinstance(value, list):
                bucket[label] += len(value)
            elif isinstance(value, dict):
                bucket[label] += 1
        outcomes[str(payload.get("outcome"))] = outcomes.get(str(payload.get("outcome")), 0) + 1
        if kind == "relationship":
            relationship_units += 1
            if payload.get("relationship") is not None:
                relationship_units_accepted += 1

    value: dict[str, object] = {
        "work_units": {
            "results_by_kind": kind_totals,
            "outcomes": outcomes,
            "relationship_units": relationship_units,
            "relationship_units_with_accepted_payload": relationship_units_accepted,
        },
        "artifact": context.artifact_facts or {"status": "blocked", "error": context.artifact_error},
        "checks": {
            "artifact_filename_is_its_digest": context.artifact_facts.get(
                "filename_is_digest", False
            ),
            "artifact_digest_matches_audit": context.artifact_facts.get(
                "digest_matches_audit", False
            ),
            "artifact_bytes_are_canonical": context.artifact_facts.get(
                "bytes_are_canonical", False
            ),
            "card_database_digest_matches_audit": context.card_database_facts.get(
                "digest_matches_audit", False
            ),
            "guide_digest_matches_audit": context.guide_facts.get("digest_matches_audit", False),
            "guide_text_matches_artifact_pin": context.guide_facts.get(
                "text_sha256_matches_pin", False
            ),
            "paid_relationship_units_accepted_none": relationship_units_accepted == 0,
        },
    }
    if context.artifact is None:
        value["status"] = "partial"
        value["artifact_load_error"] = context.artifact_error
        return value
    value["status"] = "observed"
    rejected_by_kind: dict[str, int] = {}
    for finding in context.artifact.rejected_findings:
        rejected_by_kind[finding.source_kind] = rejected_by_kind.get(finding.source_kind, 0) + 1
    mechanisms: dict[str, int] = {}
    for relationship in context.artifact.relationships:
        mechanisms[relationship.mechanism] = mechanisms.get(relationship.mechanism, 0) + 1
    value["artifact_evidence"] = {
        "rejected_findings": len(context.artifact.rejected_findings),
        "rejected_by_source_kind": rejected_by_kind,
        "mechanisms": dict(sorted(mechanisms.items(), key=lambda item: -item[1])),
        "guide_claims": [
            {
                "finding_id": claim.finding_id,
                "category": claim.category,
                "review_status": claim.review.status.value,
            }
            for claim in context.artifact.guide_claims
        ],
    }
    return value


# --------------------------------------------------------------------------- #
# stage B: generated representation
# --------------------------------------------------------------------------- #


def classify_relationship(relationship, facts, pins, cards) -> str:
    """Cross-check one stored row against the production conversion path.

    The probe resolves the stored pair through the production helpers and runs
    the production zone-supply gate itself after the card gates; a row the gate
    rejects reports the production `contradiction` outcome, and every surviving
    row is delegated to the module's own per-row conversion, whose outcome the
    label is.  No gate string is restated here, so a moved gate fails loudly
    against the compiler's own conversions instead of drifting from the probe.
    """
    from draftomen.profile_relationship_projection import (
        _ROLE_LINKS,
        _compile_relationship,
        _local_pair,
        _zone_supply_contradiction,
        RelationshipConversionOutcome,
    )

    pair = _local_pair(relationship)
    link = None if pair is None else _ROLE_LINKS.get(pair.mechanism)
    if pair is not None and link is not None:
        source = facts.get((pair.source_card_id, pair.source_capability_id))
        target = facts.get((pair.target_card_id, pair.target_capability_id))
        if source is not None and target is not None:
            source_card = cards.get(source.card_id)
            target_card = cards.get(target.card_id)
            if (
                source.role is link.enabler
                and target.role is link.payoff
                and source_card is not None
                and target_card is not None
                and not source_card.unknown
                and not target_card.unknown
                and pins.get(source.card_id) is not None
                and pins.get(target.card_id) is not None
                and _zone_supply_contradiction(source=source, target=target, facts=facts)
            ):
                return RelationshipConversionOutcome.CONTRADICTION.value
    compiled = _compile_relationship(
        relationship=relationship, facts=facts, pins=pins, cards=cards
    )
    return compiled.outcome.value


def stage_generated_representation(context: Context) -> dict[str, object]:
    from draftomen.profile_relationship_projection import (
        RelationshipConversionOutcome,
        _capability_facts,
        compile_confirmed_relationship_projections,
    )

    value: dict[str, object] = {
        "status": "observed",
        "enhancement_origin": context.enhancement_origin,
        "artifact_load_ok": context.artifact is not None,
        "enhancement": None,
        "failure_reasons": None,
        "per_mechanism": None,
        "installed_profile": {"status": "missing"},
    }
    enhancement = context.require("enhancement")
    projected = [
        relationship
        for relationship in enhancement.relationships
        if relationship.prerequisite_projection is not None
    ]
    value["enhancement"] = {
        "artifact_sha256": enhancement.artifact_sha256,
        "artifact_sha256_matches_file": enhancement.artifact_sha256
        == EXPECTED["artifact_sha256"],
        "card_count": enhancement.card_data.card_count,
        "mechanics": len(enhancement.mechanics),
        "relationships": len(enhancement.relationships),
        "projected_relationships": len(projected),
        "projected_finding_ids": sorted(
            relationship.finding_id for relationship in projected
        ),
    }
    if context.artifact is not None:
        card_database = context.require("card_database")
        compiled = compile_confirmed_relationship_projections(
            artifact=context.artifact, card_database=card_database
        )
        facts = _capability_facts(context.artifact)
        pins = {pin.card_id: pin for pin in context.artifact.cards}
        cards = card_database.cards
        projected_outcomes = {
            RelationshipConversionOutcome.DECODED.value,
            RelationshipConversionOutcome.QUALIFIED.value,
        }
        reasons: dict[str, int] = {}
        per_mechanism: dict[str, dict[str, int]] = {}
        for relationship in context.artifact.relationships:
            reason = classify_relationship(relationship, facts, pins, cards)
            reasons[reason] = reasons.get(reason, 0) + 1
            entry = per_mechanism.setdefault(
                relationship.mechanism, {"total": 0, "projected": 0}
            )
            entry["total"] += 1
            if reason in projected_outcomes:
                entry["projected"] += 1
        compiler_outcomes: dict[str, int] = {}
        compiler_reasons: dict[str, int] = {}
        for conversion in compiled.conversions:
            label = conversion.outcome.value
            compiler_outcomes[label] = compiler_outcomes.get(label, 0) + 1
            compiler_reasons[conversion.reason] = compiler_reasons.get(conversion.reason, 0) + 1
        compiled_projected = sum(
            1
            for relationship in compiled.relationships
            if relationship.prerequisite_projection is not None
        )
        value["compile_recomputed"] = {
            "relationships": len(compiled.relationships),
            "projected_relationships": compiled_projected,
            "outcomes": compiler_outcomes,
            "reasons": compiler_reasons,
        }
        value["failure_reasons"] = dict(sorted(reasons.items(), key=lambda item: -item[1]))
        value["per_mechanism"] = dict(
            sorted(per_mechanism.items(), key=lambda item: -item[1]["total"])
        )
        value["classifier_matches_compiler"] = (
            reasons == compiler_outcomes
            and sum(reasons.get(label, 0) for label in projected_outcomes) == compiled_projected
            and compiled_projected == len(projected)
        )
    else:
        value["compile_recomputed"] = {
            "status": "blocked",
            "error": context.artifact_error,
        }
        value["classifier_matches_compiler"] = None
        value["enhancement_error"] = context.enhancement_error

    if INSTALLED_PROFILE.exists():
        installed_payload = INSTALLED_PROFILE.read_bytes()
        installed_json = json.loads(installed_payload.decode("utf-8"))
        installed_enhancement = installed_json.get("enhancement") or {}
        installed_relationships = installed_enhancement.get("relationships") or []
        installed_projected = sorted(
            item["finding_id"]
            for item in installed_relationships
            if item.get("prerequisite_projection") is not None
        )
        value["installed_profile"] = {
            "status": "observed",
            "path": str(INSTALLED_PROFILE),
            "sha256": sha256_bytes(installed_payload),
            "digest_matches_audit": sha256_bytes(installed_payload)
            == EXPECTED["installed_profile_sha256"],
            "generated_at": installed_json.get("generated_at"),
            "enhancement_artifact_sha256": installed_enhancement.get("artifact_sha256"),
            "enhancement_matches_paid_artifact": installed_enhancement.get("artifact_sha256")
            == EXPECTED["artifact_sha256"],
            "relationships": len(installed_relationships),
            "projected_relationships": len(installed_projected),
            "projected_finding_ids": installed_projected,
            "matches_audit_snapshot": installed_projected
            == sorted(INSTALLED_PROFILE_EXPECTED_PROJECTIONS),
        }
    return value


# --------------------------------------------------------------------------- #
# stage C: pool support
# --------------------------------------------------------------------------- #


def _reason_evidence_text(reason) -> str:
    evidence = getattr(reason, "evidence", None)
    if evidence is None:
        return ""
    if isinstance(evidence, str):
        return evidence
    return " ".join(str(item) for item in evidence)


def _row_projection(row) -> dict[str, object]:
    recommended = row.recommended
    ledger = row.role_ledger
    supports = () if ledger is None else ledger.relationship_support
    return {
        "pack_number": row.pack_number,
        "pick_number": row.pick_number,
        "recommended_grp_id": None if recommended is None else recommended.card.grp_id,
        "raw_score": None if recommended is None else recommended.raw_score,
        "synergy": None if recommended is None else recommended.contextual_breakdown.synergy,
        "aggregate": None if recommended is None else recommended.contextual_breakdown.aggregate,
        "support_count": len(supports),
        "support_finding_ids": [support.finding_id for support in supports],
        "support_targets": [support.target_card_id for support in supports],
        "contextual_evidence": list(row.contextual_evidence),
        "rationale_kinds": [
            reason.kind
            for reason in (() if recommended is None else recommended.rationale.reasons)
        ],
        "rationale_evidence": [
            _reason_evidence_text(reason)
            for reason in (() if recommended is None else recommended.rationale.reasons)
        ],
    }


def _fixture_state_and_profile():
    from draftomen.pool import DraftState
    from draftomen.set_profile import load_set_profile

    profile = load_set_profile(
        REPO_ROOT / "tests/fixtures/hob-relationship-scoring-profile.json",
        expected_set_code="hob",
        expected_format="quickdraft",
    )
    state = DraftState.from_json(
        json.loads(
            (REPO_ROOT / "tests/fixtures/hob-relationship-scoring-state.json").read_text(
                encoding="utf-8"
            )
        )
    )
    return profile, state


def stage_pool_support(context: Context) -> dict[str, object]:
    from draftomen.backtest import generate_backtest_report
    from draftomen.pickengine import MAX_CONTEXTUAL_ADJUSTMENT, MAX_SYNERGY_TERM

    card_database = context.require("card_database")
    enhancement = context.require("enhancement")
    fixture_profile, state = _fixture_state_and_profile()

    controls = {
        "fixture_enhancement": (fixture_profile, True, True),
        "fixture_enhancement_removed": (replace(fixture_profile, enhancement=None), True, True),
        "paid_enhancement": (replace(fixture_profile, enhancement=enhancement), True, True),
        "paid_enhancement_context_disabled": (
            replace(fixture_profile, enhancement=enhancement),
            False,
            True,
        ),
        "paid_enhancement_relationships_off": (
            replace(fixture_profile, enhancement=enhancement),
            True,
            False,
        ),
    }
    results: dict[str, object] = {}
    for name, (profile, contextual, enhanced) in controls.items():
        report = generate_backtest_report(
            state=state,
            card_database=card_database,
            set_profile=profile,
            contextual_adjustments_enabled=contextual,
            enhanced_relationships_enabled=enhanced,
        )
        results[name] = [_row_projection(row) for row in report.rows]

    def supports_for(name: str, finding_id: str) -> int:
        return sum(
            1
            for row in results[name]  # type: ignore[union-attr]
            for support in row["support_finding_ids"]
            if finding_id in support
        )

    saturation = next(
        (
            row
            for row in results["fixture_enhancement"]  # type: ignore[union-attr]
            if row["synergy"] == MAX_SYNERGY_TERM
        ),
        None,
    )
    saturation_check: dict[str, object] = {"found": saturation is not None}
    if saturation is not None:
        removed_same_row = next(
            (
                row
                for row in results["fixture_enhancement_removed"]  # type: ignore[union-attr]
                if (row["pack_number"], row["pick_number"])
                == (saturation["pack_number"], saturation["pick_number"])
            ),
            None,
        )
        evidence_text = " ".join(
            str(item) for item in saturation["rationale_evidence"]  # type: ignore[union-attr]
        )
        saturation_check.update(
            {
                "pack_number": saturation["pack_number"],
                "pick_number": saturation["pick_number"],
                "synergy": saturation["synergy"],
                "support_finding_ids": saturation["support_finding_ids"],
                "support_targets": saturation["support_targets"],
                "rationale_kinds": saturation["rationale_kinds"],
                "rationale_mentions_r1_finding": R1_FINDING_ID in evidence_text,
                "enhancement_removed_synergy": None
                if removed_same_row is None
                else removed_same_row["synergy"],
                "synergy_delta_vs_enhancement_removed": None
                if removed_same_row is None
                else round(saturation["synergy"] - removed_same_row["synergy"], 6),
                "raw_score_delta_vs_enhancement_removed": None
                if removed_same_row is None
                else round(saturation["raw_score"] - removed_same_row["raw_score"], 6),
                "cap_binds": bool(
                    removed_same_row is not None
                    and saturation["synergy"] == MAX_SYNERGY_TERM
                    and removed_same_row["synergy"] == MAX_SYNERGY_TERM
                    and saturation["raw_score"] == removed_same_row["raw_score"]
                ),
            }
        )

    return {
        "status": "observed",
        "enhancement_origin": context.enhancement_origin,
        "caps": {
            "max_synergy_term": MAX_SYNERGY_TERM,
            "max_contextual_adjustment": MAX_CONTEXTUAL_ADJUSTMENT,
        },
        "controls": results,
        "focused_checks": {
            "fixture_support_for_r1": supports_for("fixture_enhancement", R1_FINDING_ID),
            "fixture_support_for_r6": supports_for("fixture_enhancement", R6_FINDING_ID),
            "paid_support_for_r1": supports_for("paid_enhancement", R1_FINDING_ID),
            "paid_support_for_r6": supports_for("paid_enhancement", R6_FINDING_ID),
            "paid_any_support_rows": sum(
                1
                for row in results["paid_enhancement"]  # type: ignore[union-attr]
                if row["support_count"]
            ),
            "paid_any_synergy": any(
                (row["synergy"] or 0.0) > 0.0
                for row in results["paid_enhancement"]  # type: ignore[union-attr]
            ),
            "context_disabled_zero_breakdown": all(
                row["synergy"] == 0.0 and row["aggregate"] == 0.0 and not row["support_count"]
                for row in results["paid_enhancement_context_disabled"]  # type: ignore[union-attr]
            ),
            "relationships_off_zero_support": all(
                not row["support_count"]
                for row in results["paid_enhancement_relationships_off"]  # type: ignore[union-attr]
            ),
            "saturation_row": saturation_check,
        },
    }


# --------------------------------------------------------------------------- #
# stage D: rendered advice (offscreen QML)
# --------------------------------------------------------------------------- #


def stage_rendered_advice(context: Context) -> dict[str, object]:
    from draftomen.backtest import generate_backtest_report
    from draftomen.pickengine import (
        render_pick_rationale_concise,
        render_pick_rationale_detailed,
    )
    from draftomen.qt_adapter import GuiPreferencesAdapter, _to_qml_value
    from draftomen.session import Recommendation, _card_view

    card_database = context.require("card_database")
    enhancement = context.require("enhancement")
    fixture_profile, state = _fixture_state_and_profile()

    def recommendation(scored_card, rank: int) -> Recommendation:
        return Recommendation(
            rank=rank,
            card=_card_view(card=scored_card.card),
            score=scored_card.score,
            win_rate=scored_card.rating.gih_win_rate,
            average_last_seen_at=scored_card.rating.average_last_seen_at,
            source_label=scored_card.source_label,
            color_fit=scored_card.color_fit,
            no_data=scored_card.no_data,
            contextual_breakdown=scored_card.contextual_breakdown,
            contextual_evidence=scored_card.contextual_evidence,
            contextual_pair=scored_card.contextual_pair,
            contextual_theme=scored_card.contextual_theme,
            contextual_profile_maturity=scored_card.contextual_profile_maturity,
            contextual_profile_confidence=scored_card.contextual_profile_confidence,
            letter_grade=scored_card.rating.letter_grade,
            explanation=render_pick_rationale_detailed(scored_card=scored_card),
            rationale=scored_card.rationale,
            concise_explanation=render_pick_rationale_concise(scored_card=scored_card),
        )

    payloads: dict[str, dict[str, object]] = {}
    texts: dict[str, object] = {}
    for name, profile in (
        ("fixture_enhancement", fixture_profile),
        ("paid_enhancement", replace(fixture_profile, enhancement=enhancement)),
    ):
        report = generate_backtest_report(
            state=state,
            card_database=card_database,
            set_profile=profile,
            contextual_adjustments_enabled=True,
            enhanced_relationships_enabled=True,
        )
        support_row = next(
            (
                candidate
                for candidate in report.rows
                if candidate.role_ledger is not None
                and candidate.role_ledger.relationship_support
            ),
            None,
        )
        for label, row in (("no_support", report.rows[0]), ("support", support_row)):
            key = f"{name}.{label}"
            if row is None:
                texts[key] = {"row_present": False}
                continue
            value = recommendation(row.recommended, 1)
            payloads[key] = _to_qml_value(value)
            support_ids = (
                []
                if row.role_ledger is None
                else [support.finding_id for support in row.role_ledger.relationship_support]
            )
            explanation = value.explanation or ""
            texts[key] = {
                "row_present": True,
                "pack_number": row.pack_number,
                "pick_number": row.pick_number,
                "scored_grp_id": row.recommended.card.grp_id,
                "support_finding_ids": support_ids,
                "concise_explanation": value.concise_explanation,
                "detailed_explanation": value.explanation,
                "mentions_relationship_finding": any(
                    finding in explanation for finding in (R1_FINDING_ID, R6_FINDING_ID)
                ),
            }

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
    from PySide6.QtQuickControls2 import QQuickStyle

    from draftomen.qt_gui import _fixed_font_family

    QT_PHASE["name"] = "rendered_advice"
    install_qt_message_capture()
    QQuickStyle.setStyle("Fusion")
    application = QGuiApplication.instance() or QGuiApplication([])
    qml_directory = REPO_ROOT / "draftomen" / "qml"
    qml = b"""
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Rectangle {
    id: host
    property var payload: null
    width: 560
    height: 620
    visible: true
    CardPreview {
        objectName: "probePreview"
        anchors.fill: parent
        detailedIntel: true
        recommendation: host.payload
        imageState: null
    }
}
"""
    rendered: dict[str, object] = {}
    rendered_details: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="hob-587-qml-") as preferences_dir:
        preferences_adapter = GuiPreferencesAdapter(app_dir=Path(preferences_dir))
        engine = QQmlApplicationEngine()
        engine.addImportPath(str(qml_directory))
        context_properties = engine.rootContext()
        context_properties.setContextProperty("fixedFontFamily", _fixed_font_family())
        context_properties.setContextProperty("guiPreferences", preferences_adapter)
        component = QQmlComponent(engine)
        component.setData(qml, QUrl.fromLocalFile(str(qml_directory / "Hob587TraceProbe.qml")))
        if component.status() != QQmlComponent.Status.Ready:
            raise RuntimeError(
                "QML component failed: "
                + "; ".join(error.toString() for error in component.errors())
            )
        host = component.create()
        if host is None:
            raise RuntimeError("QML component produced no root object")
        for key, payload in payloads.items():
            host.setProperty("payload", payload)
            preview = host.findChild(QObject, "probePreview")
            if preview is None:
                raise RuntimeError("CardPreview instance missing")
            label = preview.findChild(QObject, "cardPreviewExplanation")
            if label is None:
                raise RuntimeError("cardPreviewExplanation label missing")
            deadline = time.monotonic() + 2.0
            text = ""
            while time.monotonic() < deadline:
                application.processEvents()
                text = str(label.property("text") or "")
                if text:
                    break
                time.sleep(0.005)
            rendered[key] = text
            font = label.property("font")
            color = label.property("color")
            rendered_details[key] = {
                "font_pixel_size": None if font is None else int(font.pixelSize()),
                "color": None if color is None else str(color.name()),
            }
        QML_HOLD.extend([host, component, engine, preferences_adapter])
    return {
        "status": "observed",
        "enhancement_origin": context.enhancement_origin,
        "renderer_texts": texts,
        "qml_rendered_texts": rendered,
        "qml_rendered_details": rendered_details,
        "qml_renderer_matches": {
            key: rendered.get(key) == texts[key].get("detailed_explanation")
            for key in rendered
        },
        "qml_theme_resolved": {
            key: bool((rendered_details[key] or {}).get("font_pixel_size"))
            for key in rendered_details
        },
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/hob-587-trace-out.json")
    args = parser.parse_args()

    report = Report()
    denial = deny_network()
    report.data["network_denial"] = denial
    report.data["provider_client_imported"] = "draftomen.openrouter_client" in sys.modules

    before_tree = tree_digest(RUN_DIR)
    report.data["run_tree_before"] = before_tree
    report.data["run_tree_matches_manifest"] = (
        before_tree["sha256"] == EXPECTED["run_manifest_sha256"]
    )
    report.data["installed_profile_digest_before"] = (
        sha256_file(INSTALLED_PROFILE) if INSTALLED_PROFILE.exists() else None
    )

    context = Context()
    for name, loader in (
        ("card_database", load_card_database),
        ("guide_source", load_guide_source),
    ):
        try:
            loader(context)
        except BaseException as error:  # noqa: BLE001 - recorded, stages degrade
            context.__dict__[f"{name}_error"] = f"{type(error).__name__}: {error}"
    try:
        build_sources(context)
    except BaseException as error:  # noqa: BLE001
        context.artifact_error = f"sources unavailable: {type(error).__name__}: {error}"
    if context.sources is not None:
        try:
            load_artifact(context)
        except BaseException as error:  # noqa: BLE001
            context.artifact_error = f"{type(error).__name__}: {error}"
    else:
        context.artifact_error = "sources unavailable"
    if context.card_database is not None:
        try:
            load_enhancement(context)
        except BaseException as error:  # noqa: BLE001
            context.enhancement_error = f"{type(error).__name__}: {error}"

    report.data["context"] = {
        "card_database": context.card_database_facts
        or {"status": "blocked", "error": getattr(context, "card_database_error", None)},
        "guide_source": context.guide_facts
        or {"status": "blocked", "error": getattr(context, "guide_source_error", None)},
        "sources": "available" if context.sources is not None else "blocked",
        "artifact": context.artifact_facts
        or {"status": "blocked", "error": context.artifact_error},
        "enhancement_origin": context.enhancement_origin,
        "enhancement_error": context.enhancement_error,
    }

    QT_PHASE["name"] = "stages"
    for name, stage in (
        ("saved_evidence", stage_saved_evidence),
        ("generated_representation", stage_generated_representation),
        ("pool_support", stage_pool_support),
        ("rendered_advice", stage_rendered_advice),
    ):
        try:
            value = stage(context)
        except BaseException as error:  # noqa: BLE001 - probe reports, never crashes
            value = blocked(name, error)
        report.stage(name, value)
        report.data.setdefault("stage_status", {})[name] = value.get("status")  # type: ignore[attr-defined]

    # Destroy the Qt objects the probe holds while the message handler is still
    # installed, so teardown warnings are attributed to a phase instead of being
    # lost after the report is written.
    QT_PHASE["name"] = "teardown"
    QML_HOLD.clear()
    gc.collect()
    report.data["qt_messages_by_phase"] = {
        phase: messages for phase, messages in QT_MESSAGES.items() if messages
    }
    report.data["qt_phases_seen"] = sorted(QT_MESSAGES)

    after_tree = tree_digest(RUN_DIR)
    report.data["run_tree_after"] = after_tree
    report.data["run_tree_unchanged"] = after_tree == before_tree
    report.data["installed_profile_digest_after"] = (
        sha256_file(INSTALLED_PROFILE) if INSTALLED_PROFILE.exists() else None
    )

    out_path = Path(args.out)
    out_path.write_text(
        json.dumps(report.data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"hob-587 trace report: {out_path}")
    print(f"stages: {json.dumps(report.data.get('stage_status', {}), sort_keys=True)}")
    print(f"enhancement origin: {context.enhancement_origin}")
    print(f"blockers: {len(report.data.get('blockers', []))}")
    print(f"network denial blocks: {len(denial['blocked_attempts'])}")
    print(
        "run tree unchanged: "
        f"{report.data['run_tree_unchanged']} "
        f"(manifest match: {report.data['run_tree_matches_manifest']})"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001
        traceback.print_exc()
        print(f"hob-587 trace probe failed to write its report: {error}", file=sys.stderr)
        raise SystemExit(2) from error
