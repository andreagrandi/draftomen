#!/usr/bin/env python3
"""Offline recruit/amass family recovery report for issue #590.

Read-only.  Network denial is installed before any ``draftomen`` module is
imported, the 414 scoped finding ids are rebuilt from the frozen #587
relationship ledger, the frozen confirmed artifact is compiled twice, and the
recovery is then proved through the published offline consumer
(``draftomen.cli.main(["generate-profile", ...])``) into a fresh temporary
directory instead of only a private compiler helper.

Checked-in audit utility; run from the repository root:

    PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python \
        docs/audits/hob_590_recovery_probe.py --out /tmp/hob-590-recovery.json

``HOB587_RUN_DIR`` overrides the saved enrichment run and ``HOB590_RATINGS_FILE``
the saved 17Lands ratings file.  Exit code 0 requires every gate to pass: 414
scoped rows accounted for, 410 useful findings qualified, the four hard negatives
rejected as ``token_subtype_contradiction``, unmutated paid inputs, no network or
provider access, and a generated profile that reconciles with the direct
compilation and round-trips canonically.  Any other outcome still writes the JSON
report, prints the failing checks with the exact finding ids and causes, and exits
non-zero.  A finding that the compiler cannot recover is reported as a
``compiler_gap``; a missing or mutated audit input is reported as an
``evidence_defect`` or ``input_mutation``.  The two are never conflated.

The probe is written against the recovery plan's shared contract: the existing
compiler APIs and outcome vocabulary are unchanged and the compiler wave adds only
the ``token_subtype_contradiction`` contradiction reason plus source-grounded
family qualifications.  Until that wave lands, the useful-row and hard-negative
gates fail with the exact finding ids they cover.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import importlib
import io
import json
import os
import sys
import tempfile
import time
import traceback
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

AUDIT_DIR = Path(__file__).resolve().parent
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
RATINGS_PATH = Path(
    os.environ.get(
        "HOB590_RATINGS_FILE",
        str(Path.home() / ".draftomen/17lands/HOB-QuickDraft.json"),
    )
)

LEDGER_PATH = AUDIT_DIR / "hob-587-relationship-ledger.json"
LEDGER_SHA256 = "604fc9925309acca1ad1644485431b48c5be17ebac7992a9285430644f730401"
RUN_TREE_SHA256 = "c518992a1735bfbfe358eaa5db9158865740b8f62dd90e11f5a86b69d2c24cb5"
CARD_DATABASE_PATH = RUN_DIR / "sources/card-database.json"
CARD_DATABASE_SHA256 = "70ebacdfa4bd8e45f485a6bf7392daf1f754414e0716c6587fcc9e8dc419b9b9"
ARTIFACT_NAME = "edc7d1666105fccdd38284367396400f3999d55f98d1469990bfde1a6773be84.json"
ARTIFACT_PATH = RUN_DIR / "artifacts" / ARTIFACT_NAME

SET_CODE = "HOB"
EVENT_FORMAT = "QuickDraft"
STAGE = "early"
GENERATED_AT = "2026-09-16T00:00:00Z"

SCOPED_MECHANISMS = ("token-go-wide-payoff", "token-sacrifice-outlet", "token-death-payoff")
RECRUIT_AMASS_MECHANICS = frozenset({"recruit", "amass"})
MISTY_CARD_NAME = "The Misty Mountains Cold"
MISTY_CAPABILITY_ID = "103482-dragon-token-1"
MISTY_FAMILY = "misty-mountains"

USEFUL_CLASSES = frozenset({"uc", "ucc", "uac"})
CONTRADICTION_CLASSES = frozenset({"mxe", "mxg"})
EXPECTED_CLASSES = {"uc": 379, "ucc": 18, "uac": 13, "mxe": 2, "mxg": 2}
EXPECTED_FAMILIES = {"recruit": 171, "amass": 226, MISTY_FAMILY: 17}
EXPECTED_SCOPED_ROWS = 414
EXPECTED_USEFUL_ROWS = 410
EXPECTED_CONTRADICTION_ROWS = 4
SUBTYPE_CONTRADICTION_REASON = "token_subtype_contradiction"

COMPILER_GAP = "compiler_gap"
EVIDENCE_DEFECT = "evidence_defect"
INPUT_MUTATION = "input_mutation"
PROVIDER_ACCESS = "provider_access"
GENERATION = "generation"
PROBE_ERROR = "probe_error"

PROVIDER_ENTRY_POINTS = (
    ("draftomen.openrouter_client", "OpenRouterClient", "complete"),
    ("draftomen.guide_client", "GuideClient", "fetch"),
    ("draftomen.card_data_client", "CardDataClient", "load"),
    ("draftomen.profile_client", "ProfileClient", "refresh"),
)
PROVIDER_MODULES = frozenset(name for name, _, _ in PROVIDER_ENTRY_POINTS)

if str(AUDIT_DIR) not in sys.path:
    sys.path.insert(0, str(AUDIT_DIR))
import hob_587_trace_probe as trace_probe  # noqa: E402 - the audit directory is added above

sha256_bytes = trace_probe.sha256_bytes
sha256_file = trace_probe.sha256_file
tree_digest = trace_probe.tree_digest


class ProviderAccessDenied(RuntimeError):
    """Raised when the probe reaches any provider, guide, or download entry point."""


def guard_provider_entry_points() -> dict[str, object]:
    """Record and fail every provider, guide, card-data and profile-network entry point."""
    calls: list[str] = []
    guarded: list[str] = []
    missing: list[str] = []

    def denial(entry: str):
        def _deny(*_args: object, **_kwargs: object) -> object:
            calls.append(entry)
            raise ProviderAccessDenied(f"hob-590 probe: {entry} is unavailable offline")

        return _deny

    for module_name, class_name, method_name in PROVIDER_ENTRY_POINTS:
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            missing.append(f"{module_name} ({type(error).__name__}: {error})")
            continue
        owner = getattr(module, class_name, None)
        if owner is None or not callable(getattr(owner, method_name, None)):
            missing.append(f"{module_name}.{class_name}.{method_name}")
            continue
        entry = f"{module_name}.{class_name}.{method_name}"
        setattr(owner, method_name, denial(entry))
        guarded.append(entry)
    return {
        "guarded": guarded,
        "missing": missing,
        "calls": calls,
        "modules_imported": sorted(name for name in sys.modules if name in PROVIDER_MODULES),
        "note": (
            "draftomen.cli imports its provider clients while loading, so module presence is "
            "recorded rather than gated; recorded entry-point calls and blocked sockets are the "
            "provider-access gates."
        ),
    }


class Report:
    """Collects the probe's JSON document and its pass/fail gates."""

    def __init__(self) -> None:
        self.checks: list[dict[str, object]] = []
        self.failures: list[dict[str, object]] = []
        self.data: dict[str, object] = {
            "probe": "hob-590-recovery",
            "probe_revision": 1,
            "schema_version": 1,
            "run_dir": str(RUN_DIR),
            "ledger_path": str(LEDGER_PATH),
            "ratings_path": str(RATINGS_PATH),
            "scoped_mechanisms": list(SCOPED_MECHANISMS),
            "findings": [],
        }

    def gate(
        self,
        name: str,
        *,
        ok: bool,
        detail: str | None = None,
        cause_class: str = PROBE_ERROR,
        finding_ids: Iterable[str] = (),
    ) -> None:
        """Record one check and, when it fails, the exact findings it covers."""
        ids = [str(item) for item in finding_ids]
        entry = {"check": name, "ok": bool(ok), "detail": detail}
        if ids:
            entry["finding_count"] = len(ids)
        self.checks.append(entry)
        if not ok:
            self.failures.append(
                {
                    "check": name,
                    "cause_class": cause_class,
                    "detail": detail,
                    "finding_count": len(ids),
                    "finding_ids": ids,
                }
            )

    def finalize(self, *, duration_seconds: float) -> None:
        self.data["checks"] = self.checks
        self.data["failures"] = self.failures
        self.data["duration_seconds"] = round(duration_seconds, 3)
        self.data["passed"] = not self.failures


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _canonical(value: object) -> str:
    """Return one deterministic JSON encoding for byte-level comparison."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fingerprint_file(path: Path) -> dict[str, object]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        return {"path": str(path), "sha256": None, "error": f"{type(error).__name__}: {error}"}
    return {"path": str(path), "sha256": sha256_bytes(payload), "bytes": len(payload)}


def fingerprint_inputs() -> dict[str, object]:
    """Fingerprint every paid or audited input this probe reads."""
    return {
        "run_tree": tree_digest(RUN_DIR),
        "ledger": _fingerprint_file(LEDGER_PATH),
        "artifact": _fingerprint_file(ARTIFACT_PATH),
        "card_database": _fingerprint_file(CARD_DATABASE_PATH),
        "guide": _fingerprint_file(RUN_DIR / "sources/guide.json"),
        "ratings": _fingerprint_file(RATINGS_PATH),
    }


def _summarize(counter: Counter) -> str:
    return ", ".join(f"{cause} x{count}" for cause, count in counter.most_common(6))


def _dominant_cause_class(records: list[dict[str, object]]) -> str:
    """Name the failure class of one failing row set without relabelling evidence defects."""
    for record in records:
        for cause in record["causes"]:  # type: ignore[union-attr]
            if str(cause).startswith("evidence:"):
                return EVIDENCE_DEFECT
    return COMPILER_GAP


def reconstruct_scoped_rows(
    ledger: dict[str, object],
) -> tuple[dict[str, dict[str, str]], dict[str, object]]:
    """Rebuild the scoped finding ids from the frozen ledger's indices and identities."""
    capabilities = ledger["capabilities"]  # type: ignore[index]
    findings = ledger["findings"]  # type: ignore[index]
    prefix = ledger["finding_id"]["prefix"]  # type: ignore[index]
    misty_indices = [
        index
        for index, capability in enumerate(capabilities)
        if capability["card_name"] == MISTY_CARD_NAME and capability["role"] == "token_maker"
    ]
    scoped: dict[str, dict[str, str]] = {}
    duplicates: list[str] = []
    families: Counter = Counter()
    all_ids: set[str] = set()
    ledger_rows = 0
    for mechanism, rows in findings.items():
        for source_index, target_index, audit_class, stage_gap in rows:
            source = capabilities[source_index]
            target = capabilities[target_index]
            ledger_rows += 1
            all_ids.add(
                f"{prefix}{mechanism}:{source['card_id']}:{source['id']}:"
                f"{target['card_id']}:{target['id']}"
            )
            if mechanism not in SCOPED_MECHANISMS:
                continue
            mechanics = RECRUIT_AMASS_MECHANICS & set(source["mechanics"])
            if mechanics:
                family = sorted(mechanics)[0]
            elif source_index in misty_indices:
                family = MISTY_FAMILY
            else:
                continue
            finding_id = (
                f"{prefix}{mechanism}:{source['card_id']}:{source['id']}:"
                f"{target['card_id']}:{target['id']}"
            )
            if finding_id in scoped:
                duplicates.append(finding_id)
                continue
            stage, _, gap = str(stage_gap).partition(":")
            scoped[finding_id] = {
                "mechanism": mechanism,
                "audit_class": str(audit_class),
                "stage": stage,
                "gap": gap,
                "family": family,
            }
            families[family] += 1
    facts = {
        "duplicates": duplicates,
        "families": dict(sorted(families.items())),
        "ledger_rows": ledger_rows,
        "ledger_ids": all_ids,
        "misty_capability_id": (
            capabilities[misty_indices[0]]["id"] if len(misty_indices) == 1 else None
        ),
        "misty_candidates": len(misty_indices),
    }
    return scoped, facts


def _qualification_entries(projection: object) -> dict[str, list[dict[str, object]]]:
    """Return the retained qualification kinds and selectors of one projection."""
    if projection is None:
        return {"source": [], "target": []}
    return {
        side: [
            {
                "kind": qualification.kind.value,
                "selector": qualification.selector,
                "occurrence": qualification.occurrence,
                "card_id": qualification.evidence.card_id,
                "face_index": qualification.evidence.face_index,
            }
            for qualification in participant.qualifications
        ]
        for side, participant in (("source", projection.source), ("target", projection.target))
    }


def _row_bindings(row: object) -> dict[str, object]:
    """Return the review, run, card, face and source bindings of one relationship row."""
    projection = row.prerequisite_projection
    return {
        "review": row.review.to_json(),
        "run_id": row.run_id,
        "participants": list(row.participants),
        "oracle_cards": sorted({evidence.card_id for evidence in row.oracle_evidence}),
        "guide_ids": sorted({evidence.guide_id for evidence in row.guide_evidence}),
        "source_capability_id": None if projection is None else projection.source.capability_id,
        "target_capability_id": None if projection is None else projection.target.capability_id,
        "source_face": (
            None
            if projection is None
            else [projection.source.face_index, projection.source.face_name]
        ),
        "target_face": (
            None
            if projection is None
            else [projection.target.face_index, projection.target.face_name]
        ),
        "source_card_sha256": (
            None if projection is None else projection.source.card_source_sha256
        ),
        "target_card_sha256": (
            None if projection is None else projection.target.card_source_sha256
        ),
    }


def _compilation_fingerprint(compilation: object) -> dict[str, object]:
    """Return the canonical identity of one compilation's rows and conversions."""
    relationships = [_canonical(row.to_json()) for row in compilation.relationships]
    conversions = [_canonical(item.to_json()) for item in compilation.conversions]
    return {
        "relationships": len(relationships),
        "conversions": len(conversions),
        "relationships_sha256": sha256_bytes("\n".join(relationships).encode("utf-8")),
        "conversions_sha256": sha256_bytes("\n".join(conversions).encode("utf-8")),
    }


def _printed_fields(text: str) -> dict[str, str]:
    """Parse the CLI's `key=value` result lines."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key and " " not in key:
            fields[key.strip()] = value.strip()
    return fields


def _same_instant(first: object, second: object) -> bool:
    """Return whether two ISO-8601 timestamps name the same instant."""
    try:
        return datetime.fromisoformat(str(first).replace("Z", "+00:00")) == datetime.fromisoformat(
            str(second).replace("Z", "+00:00")
        )
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #


def load_frozen_inputs() -> object:
    """Load the frozen card database, guide, sources and confirmed artifact."""
    from draftomen.carddb import CardDatabase

    card_database = CardDatabase.from_json(
        json.loads(CARD_DATABASE_PATH.read_text(encoding="utf-8"))
    )
    context = trace_probe.Context()
    context.card_database = card_database
    trace_probe.load_guide_source(context)
    trace_probe.build_sources(context)
    trace_probe.load_artifact(context)
    return context


def gate_inputs(report: Report, before: dict[str, object]) -> bool:
    """Gate the frozen identity of every audited input."""
    run_tree = before["run_tree"]  # type: ignore[index]
    missing = sorted(
        name
        for name in ("ledger", "artifact", "card_database", "guide", "ratings")
        if before[name]["sha256"] is None  # type: ignore[index]
    )
    report.data["inputs"] = {"before": before}
    report.gate(
        "inputs_present",
        ok=not missing and bool(run_tree["files"]),
        detail=(
            f"missing={missing} run_tree_files={run_tree['files']} "
            f"run_dir={RUN_DIR}"
        ),
        cause_class=EVIDENCE_DEFECT,
    )
    identities = {
        "ledger_digest_matches_pin": before["ledger"]["sha256"] == LEDGER_SHA256,  # type: ignore[index]
        "artifact_filename_is_digest": before["artifact"]["sha256"]  # type: ignore[index]
        == ARTIFACT_NAME.removesuffix(".json"),
        "card_database_digest_matches_pin": before["card_database"]["sha256"]  # type: ignore[index]
        == CARD_DATABASE_SHA256,
        "run_tree_digest_matches_manifest": run_tree["sha256"] == RUN_TREE_SHA256,
    }
    report.data["inputs"]["identity_checks"] = identities
    report.gate(
        "frozen_inputs_match_pins",
        ok=all(identities.values()),
        detail=_canonical(identities),
        cause_class=EVIDENCE_DEFECT,
    )
    return not missing and all(identities.values())


def gate_scope(
    report: Report,
    scoped: dict[str, dict[str, str]],
    facts: dict[str, object],
    artifact: object,
) -> None:
    """Gate the reconstructed denominator against the frozen ledger and artifact."""
    classes = Counter(row["audit_class"] for row in scoped.values())
    families = Counter(row["family"] for row in scoped.values())
    unknown = sorted(set(classes) - USEFUL_CLASSES - CONTRADICTION_CLASSES)
    confirmed_ids = {row.finding_id for row in artifact.confirmed_relationships}
    missing = sorted(set(scoped) - confirmed_ids)
    useful = sum(count for cls, count in classes.items() if cls in USEFUL_CLASSES)
    contradictions = sum(count for cls, count in classes.items() if cls in CONTRADICTION_CLASSES)
    report.data["scope"] = {
        "rows": len(scoped),
        "classes": dict(sorted(classes.items())),
        "families": dict(sorted(families.items())),
        "useful_rows": useful,
        "contradiction_rows": contradictions,
        "expected_classes": EXPECTED_CLASSES,
        "expected_families": EXPECTED_FAMILIES,
        "duplicate_ids": facts["duplicates"],
        "unknown_classes": unknown,
        "missing_from_artifact": missing,
        "misty_capability_id": facts["misty_capability_id"],
        "misty_capability_candidates": facts["misty_candidates"],
        "reconstruction_sha256": sha256_bytes("\n".join(sorted(scoped)).encode("utf-8")),
        "ledger_rows": facts["ledger_rows"],
        "ledger_ids_match_artifact_confirmed": facts["ledger_ids"] == confirmed_ids,  # type: ignore[operator]
    }
    report.gate(
        "scope_rows_reconstructed",
        ok=(
            len(scoped) == EXPECTED_SCOPED_ROWS
            and not facts["duplicates"]
            and dict(classes) == EXPECTED_CLASSES
            and not unknown
        ),
        detail=(
            f"rows={len(scoped)} expected={EXPECTED_SCOPED_ROWS} "
            f"classes={_canonical(dict(sorted(classes.items())))} "
            f"duplicates={len(facts['duplicates'])} unknown_classes={unknown}"  # type: ignore[arg-type]
        ),
        cause_class=EVIDENCE_DEFECT,
        finding_ids=facts["duplicates"],
    )
    report.gate(
        "scope_families_reconstructed",
        ok=dict(families) == EXPECTED_FAMILIES
        and facts["misty_capability_id"] == MISTY_CAPABILITY_ID,
        detail=(
            f"families={_canonical(dict(sorted(families.items())))} "
            f"expected={_canonical(EXPECTED_FAMILIES)} "
            f"misty_capability={facts['misty_capability_id']!r}"
        ),
        cause_class=EVIDENCE_DEFECT,
    )
    report.gate(
        "scope_ids_present_in_artifact",
        ok=not missing,
        detail=f"{len(missing)} scoped ids are absent from the frozen artifact",
        cause_class=EVIDENCE_DEFECT,
        finding_ids=missing,
    )
    report.gate(
        "scope_useful_and_contradiction_split",
        ok=useful == EXPECTED_USEFUL_ROWS and contradictions == EXPECTED_CONTRADICTION_ROWS,
        detail=(
            f"useful={useful} expected={EXPECTED_USEFUL_ROWS} "
            f"contradictions={contradictions} expected={EXPECTED_CONTRADICTION_ROWS}"
        ),
        cause_class=EVIDENCE_DEFECT,
    )


def stage_compilation(
    report: Report,
    *,
    artifact: object,
    card_database: object,
    scoped: dict[str, dict[str, str]],
) -> tuple[object, list[dict[str, object]]]:
    """Compile twice, gate determinism, and evaluate every scoped finding."""
    from draftomen.profile_relationship_projection import (
        compile_confirmed_relationship_projections,
    )

    first = compile_confirmed_relationship_projections(
        artifact=artifact, card_database=card_database
    )
    second = compile_confirmed_relationship_projections(
        artifact=artifact, card_database=card_database
    )
    fingerprints = [_compilation_fingerprint(item) for item in (first, second)]
    deterministic = fingerprints[0] == fingerprints[1]
    payload = ARTIFACT_PATH.read_bytes()
    records = _evaluate_rows(scoped=scoped, artifact=artifact, compilation=first)
    confirmed_ids = [row.finding_id for row in artifact.confirmed_relationships]
    conversion_ids = [item.finding_id for item in first.conversions]
    report.data["compilation"] = {
        "deterministic": deterministic,
        "fingerprints": fingerprints,
        "scoped_totals": dict(
            sorted(Counter(record["outcome"] for record in records).items())
        ),
        "scoped_reasons": dict(
            sorted(
                Counter(
                    f"{record['outcome']}/{record['reason']}"
                    for record in records
                    if record["outcome"] != "qualified"
                ).items()
            )
        ),
        "artifact_sha256_after": sha256_bytes(payload),
        "artifact_bytes_are_canonical": artifact.to_bytes() == payload,
        "accounted_rows": len(conversion_ids),
        "accounts_for_every_confirmed_row": conversion_ids == confirmed_ids,
    }
    report.gate(
        "compile_deterministic",
        ok=deterministic,
        detail=_canonical(fingerprints),
        cause_class=COMPILER_GAP,
    )
    report.gate(
        "compile_accounts_for_every_confirmed_row",
        ok=conversion_ids == confirmed_ids,
        detail=(
            f"conversions={len(conversion_ids)} confirmed={len(confirmed_ids)} "
            "ordered identities differ"
            if conversion_ids != confirmed_ids
            else f"{len(conversion_ids)} stored relationships accounted for in stored order"
        ),
        cause_class=COMPILER_GAP,
    )
    report.gate(
        "compiled_artifact_unchanged",
        ok=sha256_bytes(payload) == ARTIFACT_NAME.removesuffix(".json")
        and artifact.to_bytes() == payload,
        detail=f"artifact_sha256={sha256_bytes(payload)}",
        cause_class=INPUT_MUTATION,
    )

    failing_useful = [
        record
        for record in records
        if record["expected"] == "qualified" and record["causes"]
    ]
    failing_negatives = [
        record
        for record in records
        if record["expected"] == "contradiction" and record["causes"]
    ]
    report.gate(
        "useful_rows_qualified",
        ok=not failing_useful,
        detail=(
            f"{len(failing_useful)} of {EXPECTED_USEFUL_ROWS} useful findings are not qualified: "
            + _summarize(
                Counter(cause for record in failing_useful for cause in record["causes"])
            )
        ),
        cause_class=_dominant_cause_class(failing_useful),
        finding_ids=[record["finding_id"] for record in failing_useful],
    )
    report.gate(
        "hard_negatives_contradicted",
        ok=not failing_negatives,
        detail=(
            f"{len(failing_negatives)} of {EXPECTED_CONTRADICTION_ROWS} hard negatives are not "
            "explicit subtype contradictions: "
            + _summarize(
                Counter(cause for record in failing_negatives for cause in record["causes"])
            )
        ),
        cause_class=_dominant_cause_class(failing_negatives),
        finding_ids=[record["finding_id"] for record in failing_negatives],
    )
    return first, records


def _evaluate_rows(
    *,
    scoped: dict[str, dict[str, str]],
    artifact: object,
    compilation: object,
) -> list[dict[str, object]]:
    """Judge every scoped finding against its audit class and the compiler's own outcome."""
    compiled_rows = {row.finding_id: row for row in compilation.relationships}
    conversions = {item.finding_id: item for item in compilation.conversions}
    stored_rows = {row.finding_id: row for row in artifact.relationships}
    records: list[dict[str, object]] = []
    for finding_id in sorted(scoped):
        scope = scoped[finding_id]
        expected = "qualified" if scope["audit_class"] in USEFUL_CLASSES else "contradiction"
        record: dict[str, object] = {
            "finding_id": finding_id,
            "mechanism": scope["mechanism"],
            "family": scope["family"],
            "audit_class": scope["audit_class"],
            "ledger_stage": scope["stage"],
            "ledger_gap": scope["gap"],
            "expected": expected,
            "outcome": None,
            "reason": None,
            "projection": None,
            "qualifications": {"source": [], "target": []},
            "profile": None,
            "report": None,
            "causes": [],
        }
        compiled = compiled_rows.get(finding_id)
        conversion = conversions.get(finding_id)
        stored = stored_rows.get(finding_id)
        if compiled is None or conversion is None or stored is None:
            record["causes"].append(
                "evidence: finding id is not accounted for by the frozen artifact and its compilation"
            )
            records.append(record)
            continue
        record["outcome"] = conversion.outcome.value
        record["reason"] = conversion.reason
        projection = compiled.prerequisite_projection
        record["projection"] = None if projection is None else projection.outcome.value
        qualifications = _qualification_entries(projection)
        record["qualifications"] = qualifications
        if expected == "qualified":
            if conversion.outcome.value != "qualified":
                record["causes"].append(
                    f"compiler gap: outcome={conversion.outcome.value} reason={conversion.reason}"
                )
            elif projection is None:
                record["causes"].append("compiler gap: qualified conversion without a projection")
            elif not qualifications["source"] and not qualifications["target"]:
                record["causes"].append(
                    "compiler gap: qualified projection without a retained qualification"
                )
        elif conversion.outcome.value != "contradiction":
            record["causes"].append(
                f"compiler gap: expected contradiction, outcome={conversion.outcome.value} "
                f"reason={conversion.reason}"
            )
        elif conversion.reason != SUBTYPE_CONTRADICTION_REASON:
            record["causes"].append(
                f"compiler gap: contradiction reason {conversion.reason!r} differs from "
                f"{SUBTYPE_CONTRADICTION_REASON!r}"
            )
        elif projection is not None:
            record["causes"].append("compiler gap: contradicted relationship was still projected")
        elif _canonical(compiled.to_json()) != _canonical(stored.to_json()):
            record["causes"].append(
                "compiler gap: contradicted relationship was modified instead of left as reviewed"
            )
        records.append(record)
    return records


def stage_generation(
    report: Report,
    *,
    artifact: object,
    compilation: object,
    records: list[dict[str, object]],
    work_dir: Path,
) -> None:
    """Run the published offline consumer and reconcile its profile with the compilation."""
    from draftomen.cli import main as cli_main
    from draftomen.set_profile import SetProfile, dump_set_profile, load_set_profile

    output_dir = work_dir / "generate-profile"
    output_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        "generate-profile",
        "--set-code",
        SET_CODE,
        "--format",
        EVENT_FORMAT,
        "--stage",
        STAGE,
        "--generated-at",
        GENERATED_AT,
        "--card-database-file",
        str(CARD_DATABASE_PATH),
        "--ratings-file",
        str(RATINGS_PATH),
        "--enrichment",
        str(ARTIFACT_PATH),
        "--output-dir",
        str(output_dir),
    ]
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code: int | None = None
    error: str | None = None
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = cli_main(argv=argv)
    except BaseException as exception:  # noqa: BLE001 - the probe reports, never crashes
        error = f"{type(exception).__name__}: {exception}"
    fields = _printed_fields(stdout.getvalue())
    report.data["generation"] = {
        "argv": argv,
        "exit_code": exit_code,
        "error": error,
        "stdout_fields": fields,
        "stderr_tail": stderr.getvalue().strip().splitlines()[-5:],
        "output_dir": str(output_dir),
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    report.gate(
        "generation_cli",
        ok=exit_code == 0 and error is None,
        detail=f"exit_code={exit_code} error={error} stderr={report.data['generation']['stderr_tail']}",  # type: ignore[index]
        cause_class=GENERATION,
    )
    artifact_path = Path(fields["artifact"]) if "artifact" in fields else None
    if artifact_path is None or not artifact_path.is_file():
        report.gate(
            "generated_profile_published",
            ok=False,
            detail=f"generated artifact path is unavailable: {fields.get('artifact')!r}",
            cause_class=GENERATION,
        )
        return
    gzip_payload = artifact_path.read_bytes()
    report.data["generation"]["artifact_path"] = str(artifact_path)
    report.data["generation"]["artifact_sha256"] = sha256_bytes(gzip_payload)
    report.data["generation"]["generation_manifest"] = fields.get("generation_manifest")
    report.gate(
        "generated_profile_published",
        ok=sha256_bytes(gzip_payload) == artifact_path.name.removesuffix(".json.gz"),
        detail=(
            f"artifact={artifact_path} gzip_sha256={sha256_bytes(gzip_payload)} "
            f"name={artifact_path.name}"
        ),
        cause_class=GENERATION,
    )
    try:
        profile = SetProfile.from_json(json.loads(gzip.decompress(gzip_payload).decode("utf-8")))
    except BaseException as exception:  # noqa: BLE001 - recorded, later gates skip
        report.gate(
            "generated_profile_reads_back",
            ok=False,
            detail=f"{type(exception).__name__}: {exception}",
            cause_class=GENERATION,
        )
        return
    report.gate(
        "generated_profile_reads_back",
        ok=True,
        detail=(
            f"schema_version={profile.schema_version} maturity={profile.maturity.value} "
            f"enhancement={'present' if profile.enhancement is not None else 'absent'}"
        ),
    )
    if profile.enhancement is None:
        report.gate(
            "profile_enhancement_identity",
            ok=False,
            detail="the generated profile carries no confirmed enhancement block",
            cause_class=GENERATION,
        )
        return
    _gate_enhancement_identity(report, profile=profile, artifact=artifact)
    _gate_profile_rows(report, profile=profile, compilation=compilation, records=records)

    round_trip_path = work_dir / "round-trip-profile.json"
    round_trip: dict[str, object] = {
        "path": str(round_trip_path),
        "bytes_match": False,
        "error": None,
    }
    try:
        dump_set_profile(profile, round_trip_path)
        reloaded = load_set_profile(
            round_trip_path,
            expected_set_code=SET_CODE.casefold(),
            expected_format=EVENT_FORMAT,
        )
        round_trip["bytes_match"] = reloaded.to_bytes() == profile.to_bytes()
        round_trip["sha256"] = sha256_file(round_trip_path)
    except BaseException as exception:  # noqa: BLE001 - recorded as a generation failure
        round_trip["error"] = f"{type(exception).__name__}: {exception}"
    report.data["generation"]["round_trip"] = round_trip
    report.gate(
        "profile_round_trip_canonical",
        ok=bool(round_trip["bytes_match"]) and round_trip["error"] is None,
        detail=(
            f"round_trip={round_trip_path} bytes_match={round_trip['bytes_match']} "
            f"error={round_trip['error']} profile_sha256={sha256_bytes(profile.to_bytes())}"
        ),
        cause_class=GENERATION,
    )
    _gate_generation_report(
        report,
        artifact=artifact,
        compilation=compilation,
        records=records,
        manifest_path=fields.get("generation_manifest"),
        profile=profile,
        artifact_path=artifact_path,
    )


def _gate_enhancement_identity(report: Report, *, profile: object, artifact: object) -> None:
    """Require the published enhancement to preserve the frozen artifact's identity."""
    enhancement = profile.enhancement
    artifact_runs = {run.run_id: _canonical(run.to_json()) for run in artifact.runs}
    artifact_cards = {pin.card_id: _canonical(pin.to_json()) for pin in artifact.cards}
    artifact_guides = {pin.guide_id: _canonical(pin.to_json()) for pin in artifact.guides}
    finding_run_ids = {row.run_id for row in enhancement.relationships} | {
        claim.run_id for claim in enhancement.mechanics
    }
    identity = {
        "artifact_sha256_matches": enhancement.artifact_sha256
        == ARTIFACT_NAME.removesuffix(".json"),
        "set_code_matches": enhancement.set_code == artifact.set_code,
        "set_source_id_matches": enhancement.set_source_id == artifact.set_source_id,
        "set_source_sha256_matches": enhancement.set_source_sha256 == artifact.set_source_sha256,
        "card_data_matches": enhancement.card_data.source == artifact.set_source_id
        and enhancement.card_data.sha256 == artifact.set_source_sha256
        and enhancement.card_data.card_count == len(artifact.cards),
        "created_at_matches": enhancement.created_at == artifact.created_at,
        "review_matches": enhancement.review.to_json() == artifact.review.to_json(),
        "runs_retained_verbatim": all(
            artifact_runs.get(run.run_id) == _canonical(run.to_json()) for run in enhancement.runs
        ),
        "guides_retained_verbatim": all(
            artifact_guides.get(pin.guide_id) == _canonical(pin.to_json())
            for pin in enhancement.guides
        ),
        "cards_retained_verbatim": all(
            artifact_cards.get(pin.card_id) == _canonical(pin.to_json())
            for pin in enhancement.cards
        ),
        "finding_runs_present": finding_run_ids <= {run.run_id for run in enhancement.runs},
        "relationship_ids_match": {row.finding_id for row in enhancement.relationships}
        == set(artifact.confirmed_relationship_ids),
    }
    report.data["generation"]["enhancement_identity"] = identity
    report.data["generation"]["enhancement_relationships"] = len(enhancement.relationships)
    report.gate(
        "profile_enhancement_identity",
        ok=all(identity.values()),
        detail=_canonical(identity),
        cause_class=GENERATION,
    )


def _gate_profile_rows(
    report: Report,
    *,
    profile: object,
    compilation: object,
    records: list[dict[str, object]],
) -> None:
    """Reconcile every scoped finding's profile row with the direct compilation."""
    profile_rows = {row.finding_id: row for row in profile.enhancement.relationships}
    compiled_rows = {row.finding_id: row for row in compilation.relationships}
    missing: list[str] = []
    mismatched: list[str] = []
    binding_failures: list[str] = []
    qualification_failures: list[str] = []
    for record in records:
        finding_id = record["finding_id"]
        profile_row = profile_rows.get(finding_id)
        compiled_row = compiled_rows.get(finding_id)
        if profile_row is None or compiled_row is None:
            missing.append(finding_id)
            record["profile"] = {"present": False}
            record["causes"].append("published profile: scoped finding id is absent")
            continue
        projections_match = (
            (profile_row.prerequisite_projection is None)
            == (compiled_row.prerequisite_projection is None)
            and (
                profile_row.prerequisite_projection is None
                or profile_row.prerequisite_projection.outcome
                is compiled_row.prerequisite_projection.outcome
            )
        )
        qualifications_match = _canonical(
            _qualification_entries(profile_row.prerequisite_projection)
        ) == _canonical(_qualification_entries(compiled_row.prerequisite_projection))
        row_matches = _canonical(profile_row.to_json()) == _canonical(compiled_row.to_json())
        published = _row_bindings(profile_row)
        declared = _row_bindings(compiled_row)
        provenance_keys = ("review", "run_id", "participants", "oracle_cards", "guide_ids")
        provenance_match = all(published[key] == declared[key] for key in provenance_keys)
        source_keys = (
            "source_capability_id",
            "target_capability_id",
            "source_face",
            "target_face",
            "source_card_sha256",
            "target_card_sha256",
        )
        source_match = all(published[key] == declared[key] for key in source_keys)
        record["profile"] = {
            "present": True,
            "projection": (
                None
                if profile_row.prerequisite_projection is None
                else profile_row.prerequisite_projection.outcome.value
            ),
            "projections_match": projections_match,
            "qualifications_match": qualifications_match,
            "row_matches_compilation": row_matches,
            "provenance_bindings_match": provenance_match,
            "face_and_source_bindings_match": source_match,
        }
        if not row_matches:
            mismatched.append(finding_id)
            record["causes"].append("published profile row differs from the direct compilation")
        if not qualifications_match:
            qualification_failures.append(finding_id)
            record["causes"].append("published profile qualifications differ from the compilation")
        if not (provenance_match and source_match and projections_match):
            binding_failures.append(finding_id)
            record["causes"].append("published profile row lost a review/run/card/face/source binding")
    record_ids = [record["finding_id"] for record in records]
    report.gate(
        "profile_rows_reconciled",
        ok=not missing,
        detail=f"{len(missing)} scoped ids are absent from the generated profile",
        cause_class=GENERATION,
        finding_ids=missing,
    )
    report.gate(
        "profile_rows_match_compilation",
        ok=not mismatched and not binding_failures,
        detail=(
            f"row_mismatches={len(mismatched)} binding_failures={len(binding_failures)} "
            f"scoped_rows={len(record_ids)}"
        ),
        cause_class=GENERATION,
        finding_ids=sorted(set(mismatched) | set(binding_failures)),
    )
    report.gate(
        "profile_qualifications_match",
        ok=not qualification_failures,
        detail=f"{len(qualification_failures)} published rows changed their qualifications",
        cause_class=GENERATION,
        finding_ids=qualification_failures,
    )


def _gate_generation_report(
    report: Report,
    *,
    artifact: object,
    compilation: object,
    records: list[dict[str, object]],
    manifest_path: str | None,
    profile: object,
    artifact_path: Path,
) -> None:
    """Reconcile the published generation marker with the direct compilation."""
    if manifest_path is None:
        report.gate(
            "generation_report_matches",
            ok=False,
            detail="the CLI reported no generation marker path",
            cause_class=GENERATION,
        )
        return
    marker_path = Path(manifest_path)
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        report.gate(
            "generation_report_matches",
            ok=False,
            detail=f"{type(error).__name__}: {error}",
            cause_class=GENERATION,
        )
        return
    published = {
        item["finding_id"]: (item["outcome"], item["reason"])
        for item in marker.get("relationship_conversions", ())
    }
    declared = {
        item.finding_id: (item.outcome.value, item.reason) for item in compilation.conversions
    }
    scoped_ids = [record["finding_id"] for record in records]
    missing = [finding_id for finding_id in scoped_ids if finding_id not in published]
    differing = sorted(
        finding_id
        for finding_id in scoped_ids
        if finding_id in published and published[finding_id] != declared.get(finding_id)
    )
    for record in records:
        entry = published.get(record["finding_id"])
        record["report"] = (
            None
            if entry is None
            else {"outcome": entry[0], "reason": entry[1], "matches": entry == declared[record["finding_id"]]}
        )
    gzip_sha256 = sha256_file(artifact_path)
    identity = {
        "conversions_cover_confirmed_ids": set(published) == set(declared),
        "scoped_conversions_present": not missing,
        "scoped_conversions_match": not differing,
        "gzip_sha256_matches": marker.get("gzip_sha256") == gzip_sha256,
        "profile_sha256_matches": marker.get("profile_sha256")
        == sha256_bytes(profile.to_bytes()),
        "artifact_sha256_matches": marker.get("enhancement", {}).get("artifact_sha256")
        == ARTIFACT_NAME.removesuffix(".json"),
        "set_code_matches": str(marker.get("set_code", "")).casefold() == artifact.set_code,
        "generated_at_matches": _same_instant(marker.get("generated_at"), GENERATED_AT),
    }
    report.data["generation"]["report_identity"] = identity
    report.data["generation"]["report_path"] = str(marker_path)
    report.gate(
        "generation_report_matches",
        ok=all(identity.values()),
        detail=_canonical(identity),
        cause_class=GENERATION,
        finding_ids=sorted(set(missing) | set(differing)),
    )


def gate_safety(
    report: Report,
    *,
    before: dict[str, object],
    after: dict[str, object],
    network: dict[str, object],
) -> None:
    """Gate input mutation, network attempts, and provider access."""
    mutated = sorted(
        name
        for name in ("ledger", "artifact", "card_database", "guide", "ratings")
        if before[name] != after[name]  # type: ignore[index]
    )
    run_tree_unchanged = before["run_tree"] == after["run_tree"]
    report.data["inputs"]["after"] = after
    report.data["inputs"]["mutated"] = mutated
    report.data["inputs"]["run_tree_unchanged"] = run_tree_unchanged
    report.gate(
        "inputs_unmutated",
        ok=not mutated and run_tree_unchanged,
        detail=(
            f"mutated={mutated} run_tree_unchanged={run_tree_unchanged} "
            f"before={before['run_tree']['sha256']} after={after['run_tree']['sha256']}"  # type: ignore[index]
        ),
        cause_class=INPUT_MUTATION,
    )
    attempts = list(network["blocked_attempts"])  # type: ignore[arg-type]
    report.gate(
        "no_network_attempts",
        ok=not attempts,
        detail=f"blocked_attempts={attempts}",
        cause_class=PROVIDER_ACCESS,
    )
    provider = network["provider_entry_points"]
    calls = list(provider["calls"])  # type: ignore[index]
    guards_ok = not provider["missing"]  # type: ignore[index]
    report.data["network"]["provider_modules_imported_at_exit"] = sorted(
        name for name in sys.modules if name in PROVIDER_MODULES
    )
    report.gate(
        "provider_guards_installed",
        ok=guards_ok,
        detail=f"missing={provider['missing']}",  # type: ignore[index]
        cause_class=PROBE_ERROR,
    )
    report.gate(
        "no_provider_access",
        ok=not calls,
        detail=f"provider_entry_point_calls={calls}",
        cause_class=PROVIDER_ACCESS,
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline recruit/amass family recovery report for issue #590.",
    )
    parser.add_argument("--out", default="/tmp/hob-590-recovery.json", help="JSON report path.")
    args = parser.parse_args()

    started = time.monotonic()
    report = Report()
    denial = trace_probe.deny_network()
    provider = guard_provider_entry_points()
    report.data["network"] = {
        "denial": denial,
        "blocked_attempts": denial["blocked_attempts"],
        "provider_entry_points": provider,
    }
    before = fingerprint_inputs()
    inputs_ok = gate_inputs(report, before)

    work_dir = Path(tempfile.mkdtemp(prefix="hob-590-recovery-"))
    report.data["work_dir"] = str(work_dir)
    scoped: dict[str, dict[str, str]] = {}
    if inputs_ok:
        try:
            ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            scoped, facts = reconstruct_scoped_rows(ledger)
            context = load_frozen_inputs()
            gate_scope(report, scoped, facts, context.artifact)
            compilation, records = stage_compilation(
                report,
                artifact=context.artifact,
                card_database=context.card_database,
                scoped=scoped,
            )
            report.data["findings"] = records
            stage_generation(
                report,
                artifact=context.artifact,
                compilation=compilation,
                records=records,
                work_dir=work_dir,
            )
        except BaseException as error:  # noqa: BLE001 - the probe reports, never crashes
            report.gate(
                "family_recovery_run",
                ok=False,
                detail=f"{type(error).__name__}: {error}",
                cause_class=PROBE_ERROR,
            )
            report.data["traceback_tail"] = traceback.format_exc().strip().splitlines()[-6:]

    after = fingerprint_inputs()
    gate_safety(report, before=before, after=after, network=report.data["network"])  # type: ignore[arg-type]
    report.finalize(duration_seconds=time.monotonic() - started)

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report.data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    scope = report.data.get("scope", {})
    print(f"hob-590 recovery report: {out_path}")
    print(
        "scope: "
        f"rows={scope.get('rows')} classes={_canonical(scope.get('classes', {}))} "
        f"families={_canonical(scope.get('families', {}))}"
    )
    compilation_facts = report.data.get("compilation", {})
    print(
        "compile: "
        f"deterministic={compilation_facts.get('deterministic')} "
        f"scoped={_canonical(compilation_facts.get('scoped_totals', {}))}"
    )
    generation = report.data.get("generation", {})
    print(
        "generation: "
        f"exit_code={generation.get('exit_code')} "
        f"artifact={generation.get('artifact_path')} "
        f"round_trip={_canonical(generation.get('round_trip', {}))}"
    )
    print(f"provider calls: {len(provider['calls'])}")
    print(f"blocked network attempts: {len(denial['blocked_attempts'])}")
    print(f"checks: {sum(1 for check in report.checks if check['ok'])}/{len(report.checks)} passed")
    if report.failures:
        print(f"failures: {len(report.failures)}")
        for failure in report.failures:
            print(f"FAIL {failure['check']} [{failure['cause_class']}]: {failure['detail']}")
            for finding_id in failure["finding_ids"]:  # type: ignore[union-attr]
                print(f"  {finding_id}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001
        traceback.print_exc()
        print(f"hob-590 recovery probe failed to write its report: {error}", file=sys.stderr)
        raise SystemExit(2) from error
