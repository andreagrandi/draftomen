#!/usr/bin/env python3
"""ARCHIVED HISTORICAL PROBE: targets the retired pre-#663 schema-3 profile compiler. This script is not runnable against current code and is not evidence of current runtime behavior.

The command below is preserved only as a historical capture record. Do not use it to verify current behavior.

Read-only.  Network denial is installed before any ``draftomen`` module is
imported, the union of the frozen #587 ledger's landfall, ferocious and storied
findings is rebuilt from either endpoint across all seven mechanisms, the frozen
confirmed artifact and the derived condition map are each compiled twice, and the
recovery is proved through the published offline consumer
(``draftomen.cli.main(["generate-profile", ...])``) into two fresh temporary
directories instead of only a private compiler helper.

Historical reproduction command from the capture (not runnable against current code):

    PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python \
        docs/audits/hob_591_recovery_probe.py --out /tmp/hob-591-recovery.json

``HOB587_RUN_DIR`` overrides the saved enrichment run and ``HOB590_RATINGS_FILE``
the saved 17Lands ratings file, exactly as the #590 probe does.  Exit code 0
requires every gate to pass: the 143-row union (64 landfall, 37 ferocious and 49
storied incidences) rebuilt with zero duplicates, all 685 stored relationships
accounted for in saved order, all 136 useful rows projected with retained qualifications,
all 129 usable or conditional rows in the separate 143-row Adventure family report recovered,
the four audited subtype contradictions
rejected as ``token_subtype_contradiction``, the three known ``mrd`` token
replacement defects reported separately, the derived condition map's printed
targets and helper interactions present or absent exactly as declared, two
identical CLI generations whose published profiles round-trip with the complete
condition map and reviewed rows, and unmutated paid inputs with no network or
provider access.  Any other outcome still writes the JSON report, prints the
failing checks, and exits non-zero.  A finding the compiler cannot recover is
reported as a ``compiler_gap``; a missing or mutated audit input is reported as an
``evidence_defect`` or ``input_mutation``.  The two are never conflated.

The probe is written against the recovery plan's shared contract: the existing
relationship compiler keeps its public signature, stored order and outcome
vocabulary, and the compiler wave adds the ``compile_condition_map`` entry point
with the shared ``condition_statements`` recognizer, the creature-token and
mill-then-return family recognizers, and the four audited
``token_subtype_contradiction`` negatives.  Until those land, the useful-row,
contradiction, map, target and helper gates fail with the exact ids and cases they
cover.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import time
import traceback
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

sys.dont_write_bytecode = True

AUDIT_DIR = Path(__file__).resolve().parent

if str(AUDIT_DIR) not in sys.path:
    sys.path.insert(0, str(AUDIT_DIR))
import hob_587_trace_probe as trace_probe  # noqa: E402 - the audit directory is added above
import hob_590_recovery_probe as probe590  # noqa: E402 - the audit directory is added above

# The frozen paid inputs and the portable overrides are shared with the #590 probe.
RUN_DIR = probe590.RUN_DIR
RATINGS_PATH = probe590.RATINGS_PATH
LEDGER_PATH = probe590.LEDGER_PATH
ARTIFACT_PATH = probe590.ARTIFACT_PATH
ARTIFACT_NAME = probe590.ARTIFACT_NAME
CARD_DATABASE_PATH = probe590.CARD_DATABASE_PATH
LEDGER_SHA256 = probe590.LEDGER_SHA256
CARD_DATABASE_SHA256 = probe590.CARD_DATABASE_SHA256

GUIDE_SHA256 = "bd176ad3d3666822b98f68cb5f2ae67a0fc7d35c171d7816c266a741a49ac795"
GUIDE_PATH = RUN_DIR / "sources/guide.json"

FAMILY_TAGS = ("ferocious", "landfall", "storied")
# Every mechanism the frozen ledger stores; the union is reconstructed across all of them.
LEDGER_MECHANISMS = (
    "fodder-dies-payoff",
    "fodder-sacrifice-outlet",
    "mill-graveyard-payoff",
    "recursion-graveyard-payoff",
    "token-death-payoff",
    "token-go-wide-payoff",
    "token-sacrifice-outlet",
)
# The mechanisms that actually carry a family-tagged endpoint; the fodder mechanisms do not.
SCOPED_MECHANISMS = (
    "mill-graveyard-payoff",
    "recursion-graveyard-payoff",
    "token-death-payoff",
    "token-go-wide-payoff",
    "token-sacrifice-outlet",
)

USEFUL_CLASSES = frozenset({"uc", "uac", "ucc"})
CONTRADICTION_CLASSES = frozenset({"mxe", "mxg"})
RECORD_DEFECT_CLASSES = frozenset({"mrd"})

EXPECTED_CLASSES = {"uc": 130, "uac": 4, "ucc": 2, "mxe": 3, "mxg": 1, "mrd": 3}
EXPECTED_FAMILY_ROWS = {"landfall": 64, "ferocious": 37, "storied": 49}
EXPECTED_FAMILY_CLASSES = {
    "landfall": {"uc": 57, "uac": 2, "ucc": 0, "mxe": 3, "mxg": 1, "mrd": 1},
    "ferocious": {"uc": 34, "uac": 1, "ucc": 1, "mxe": 0, "mxg": 0, "mrd": 1},
    "storied": {"uc": 46, "uac": 1, "ucc": 1, "mxe": 0, "mxg": 0, "mrd": 1},
}
EXPECTED_UNION_ROWS = 143
EXPECTED_USEFUL_ROWS = 136
EXPECTED_QUALIFIED_ROWS = 136
EXPECTED_CONTRADICTION_ROWS = 4
EXPECTED_RECORD_DEFECT_ROWS = 3
EXPECTED_REPLACEMENT_SOURCES = (
    103375, 103378, 103380, 103381, 103382, 103386, 103390, 103392, 103397,
    103414, 103418, 103429, 103451, 103482, 103492, 103503, 103504, 103526,
    103531, 103542, 103546, 103571,
)
SUBTYPE_CONTRADICTION_REASON = "token_subtype_contradiction"
EXPECTED_ADVENTURE_ROWS = 143
EXPECTED_ADVENTURE_USEFUL_ROWS = 129
EXPECTED_ADVENTURE_CLASSES = {"uc": 123, "uac": 4, "ucc": 2, "mxd": 8, "mxe": 3, "mrd": 3}

# The one useful row the frozen artifact already carries as a decoded projection.
DECODED_SOURCE_CAPABILITY = "103546-f1-self-mill"
DECODED_TARGET_CAPABILITY = "103422-0-threshold-graveyard-payoff"

RECORD_DEFECT = "record_defect"
RECORD_DEFECT_NOTE = (
    "Known mrd token-replacement direction defects (Bard, King of Dale). Their repair belongs "
    "to the existing replacement-model work; the rows are neither recovered nor counted as "
    "newly successful exclusions, and reporting them is not evidence that the whole epic is "
    "complete."
)

# The printed family clause faces of the frozen set, plus their defining phrases.
EXPECTED_TARGET_FACES: Mapping[str, tuple[tuple[int, int | None], ...]] = {
    "landfall": (
        (103503, None),
        (103504, None),
        (103543, None),
        (103546, 0),
        (103409, None),
        (103421, None),
        (103495, None),
        (103500, None),
        (103501, None),
        (103549, None),
    ),
    "ferocious": (
        (103521, None),
        (103454, None),
        (103456, None),
        (103510, None),
        (103520, None),
        (103530, None),
    ),
    "storied": (
        (103382, None),
        (103376, None),
        (103385, None),
        (103391, None),
        (103463, None),
        (103464, None),
        (103484, None),
        (103527, None),
        (103545, None),
    ),
}
EXPECTED_ENDPOINT_FREE: Mapping[str, tuple[int, ...]] = {
    "landfall": (103409, 103421, 103495, 103500, 103501, 103549),
    "ferocious": (103454, 103456, 103510, 103520, 103530),
    "storied": (103376, 103385, 103391, 103463, 103464, 103484, 103527, 103545),
}
FAMILY_MARKERS = {
    "landfall": "Landfall —",
    "ferocious": "Ferocious —",
    "storied": "Storied (",
}
PAYOFF_EVIDENCE_PHRASES = {
    "landfall": ("Whenever a land you control enters",),
    "ferocious": ("power 4 or greater",),
    "storied": ("three or more artifacts, legendaries, and/or Sagas", "you have an enduring story"),
}

# Land instructions that move a land into a hand instead of onto the battlefield.
LAND_TO_HAND_SOURCES = ((103504, None), (103546, 1), (103563, None))
# Cards the storied definition itself excludes: an ordinary creature and two Forests.
STORIED_NONQUALIFYING_SOURCES = ((103454, None), (103577, None), (103582, None))
CONDITION_CAPABILITY_PREFIX = "condition-capability:"

POSITIVE_HELPER_CASES: tuple[Mapping[str, object], ...] = (
    {
        "name": "silvan_land_entry_to_dancing_can_enable",
        "description": (
            "Silvan Reveler's conditional land placement can enable Dancing from Dark to "
            "Dawn's landfall payoff."
        ),
        "enabler_kind": "land_entry",
        "enabler_card_id": 103543,
        "enabler_face_index": None,
        "enabler_quantity_value": None,
        "enabler_evidence_phrases": (
            "If you discard a land card this way",
            "put it from your graveyard onto the battlefield tapped",
        ),
        "payoff_family": "landfall",
        "payoff_card_id": 103503,
        "payoff_face_index": None,
        "payoff_quantity_value": None,
        "support": "can_enable",
    },
    {
        "name": "thranduil_land_permission_to_dancing_contributes",
        "description": (
            "Thranduil's Company's additional-land permission partially supports the same "
            "landfall payoff, retaining its Elf and per-turn restrictions."
        ),
        "enabler_kind": "additional_land_play",
        "enabler_card_id": 103549,
        "enabler_face_index": None,
        "enabler_quantity_value": None,
        "enabler_evidence_phrases": ("another Elf", "on each of your turns"),
        "payoff_family": "landfall",
        "payoff_card_id": 103503,
        "payoff_face_index": None,
        "payoff_quantity_value": None,
        "support": "contributes",
    },
    {
        "name": "misty_dragon_to_scrounger_can_enable",
        "description": (
            "The Misty Mountains Cold's conditional 6/6 Dragon can enable Wilderland "
            "Scrounger's power-four gate with its chapter, Treasure and sacrifice conditions."
        ),
        "enabler_kind": "created_creature_power",
        "enabler_card_id": 103482,
        "enabler_face_index": None,
        "enabler_quantity_value": 6,
        "enabler_evidence_phrases": (
            "I, II, III, IV",
            "four or more Treasures",
            "sacrifice this Saga",
            "6/6 red Dragon",
        ),
        "payoff_family": "ferocious",
        "payoff_card_id": 103521,
        "payoff_face_index": None,
        "payoff_quantity_value": 4,
        "support": "can_enable",
    },
    {
        "name": "misty_saga_to_fili_contributes",
        "description": (
            "The Misty Mountains Cold's Saga permanent contributes one qualifying permanent "
            "toward Fili's enduring-story condition."
        ),
        "enabler_kind": "qualifying_permanent",
        "enabler_card_id": 103482,
        "enabler_face_index": None,
        "enabler_quantity_value": None,
        "enabler_evidence_phrases": (),
        "payoff_family": "storied",
        "payoff_card_id": 103382,
        "payoff_face_index": None,
        "payoff_quantity_value": 3,
        "support": "contributes",
    },
    {
        "name": "misty_treasure_to_fili_contributes",
        "description": (
            "The Misty Mountains Cold's Treasure token contributes one qualifying artifact "
            "toward Fili's enduring-story condition."
        ),
        "enabler_kind": "created_qualifying_permanents",
        "enabler_card_id": 103482,
        "enabler_face_index": None,
        "enabler_quantity_value": None,
        "enabler_evidence_phrases": ("Create a Treasure token",),
        "payoff_family": "storied",
        "payoff_card_id": 103382,
        "payoff_face_index": None,
        "payoff_quantity_value": 3,
        "support": "contributes",
    },
    {
        "name": "fili_legendary_to_fili_contributes",
        "description": (
            "Fili the Pathfinder itself contributes one legendary permanent toward its own "
            "enduring-story condition."
        ),
        "enabler_kind": "qualifying_permanent",
        "enabler_card_id": 103382,
        "enabler_face_index": None,
        "enabler_quantity_value": None,
        "enabler_evidence_phrases": (),
        "payoff_family": "storied",
        "payoff_card_id": 103382,
        "payoff_face_index": None,
        "payoff_quantity_value": 3,
        "support": "contributes",
    },
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _canonical(value: object) -> str:
    """Return one deterministic JSON encoding for byte-level comparison."""
    return probe590._canonical(value)


def _face_sort_key(face: tuple[int, int | None]) -> tuple[int, int]:
    """Return a total ordering for one card face, keeping null faces first."""
    card_id, face_index = face
    return (card_id, -1 if face_index is None else face_index)


def _map_canonical(condition_map: object) -> str | None:
    """Return the canonical JSON of one condition map, or None when it is absent."""
    if condition_map is None:
        return None
    return _canonical(condition_map.to_json())


def _map_sha256(condition_map: object) -> str | None:
    """Return the canonical fingerprint of one condition map."""
    canonical = _map_canonical(condition_map)
    return None if canonical is None else probe590.sha256_bytes(canonical.encode("utf-8"))


def _capability_label(capability: object) -> str:
    """Return one readable capability label with its family, role and kind."""
    return f"{capability.capability_id} ({capability.family}/{capability.role}/{capability.kind})"


def _evidence_text(capability: object) -> str:
    """Return the concatenated retained evidence selectors of one capability."""
    return " ".join(item.selector for item in capability.evidence)


def _source_type_line(sources: Mapping[object, object], capability: object) -> str:
    """Return one capability source's printed type line, or an empty string."""
    face = (capability.source_card_id, capability.source_face_index)  # type: ignore[attr-defined]
    source = sources.get(face)
    if source is None or source.type_line is None:  # type: ignore[attr-defined]
        return ""
    return str(source.type_line)  # type: ignore[attr-defined]


def _edge_detail(
    *,
    interaction: object,
    enabler: object,
    payoff: object,
    saved_pairs: set[str],
) -> dict[str, object]:
    """Return one compiled interaction's identity, support and saved-row overlap."""
    return {
        "enabler_id": enabler.capability_id,
        "enabler": _capability_label(enabler),
        "payoff_id": payoff.capability_id,
        "payoff": _capability_label(payoff),
        "support": interaction.support,
        "enabler_evidence": [item.selector for item in enabler.evidence],
        "saved_pair": f"{enabler.source_card_id}->{payoff.source_card_id}" in saved_pairs,
        "saved_reverse_pair": f"{payoff.source_card_id}->{enabler.source_card_id}" in saved_pairs,
    }


# --------------------------------------------------------------------------- #
# scope reconstruction and gating
# --------------------------------------------------------------------------- #


def reconstruct_scoped_rows(
    ledger: Mapping[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Rebuild the family-scoped finding ids from either endpoint of every mechanism."""
    capabilities = ledger["capabilities"]  # type: ignore[index]
    findings = ledger["findings"]  # type: ignore[index]
    prefix = ledger["finding_id"]["prefix"]  # type: ignore[index]
    scoped: dict[str, dict[str, object]] = {}
    duplicates: list[str] = []
    classes: Counter = Counter()
    families: Counter = Counter()
    family_classes: Counter = Counter()
    mechanism_rows: Counter = Counter()
    endpoint_cards: set[int] = set()
    saved_pairs: set[str] = set()
    ledger_rows = 0
    all_ids: set[str] = set()
    for mechanism, rows in findings.items():  # type: ignore[union-attr]
        for source_index, target_index, audit_class, stage_gap in rows:
            source = capabilities[source_index]  # type: ignore[index]
            target = capabilities[target_index]  # type: ignore[index]
            ledger_rows += 1
            saved_pairs.add(f"{source['card_id']}->{target['card_id']}")
            all_ids.add(
                f"{prefix}{mechanism}:{source['card_id']}:{source['id']}:"
                f"{target['card_id']}:{target['id']}"
            )
            tags = tuple(
                family
                for family in FAMILY_TAGS
                if family in source["mechanics"] or family in target["mechanics"]
            )
            if not tags:
                continue
            endpoint_cards.update((source["card_id"], target["card_id"]))
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
                "families": list(tags),
                "source_card_id": source["card_id"],
                "source_capability_id": source["id"],
                "source_card_name": source["card_name"],
                "source_face_index": source["face_index"],
                "target_card_id": target["card_id"],
                "target_capability_id": target["id"],
                "target_card_name": target["card_name"],
                "target_face_index": target["face_index"],
            }
            classes[str(audit_class)] += 1
            mechanism_rows[mechanism] += 1
            for family in tags:
                families[family] += 1
                family_classes[(family, str(audit_class))] += 1
    facts: dict[str, object] = {
        "duplicates": duplicates,
        "classes": dict(sorted(classes.items())),
        "families": dict(sorted(families.items())),
        "family_classes": {
            family: {
                audit_class: family_classes[(family, audit_class)]
                for audit_class in EXPECTED_CLASSES
            }
            for family in FAMILY_TAGS
        },
        "mechanism_rows": dict(sorted(mechanism_rows.items())),
        "ledger_mechanisms": sorted(findings),  # type: ignore[arg-type]
        "endpoint_cards": sorted(endpoint_cards),
        "saved_pairs": sorted(saved_pairs),
        "ledger_rows": ledger_rows,
        "ledger_ids": all_ids,
    }
    return scoped, facts


def reconstruct_adventure_rows(ledger: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Return every ledger row with its Adventure endpoint and audit details."""
    capabilities = ledger["capabilities"]  # type: ignore[index]
    prefix = ledger["finding_id"]["prefix"]  # type: ignore[index]
    rows: dict[str, dict[str, object]] = {}
    for mechanism, findings in ledger["findings"].items():  # type: ignore[union-attr]
        for source_index, target_index, audit_class, _ in findings:
            source = capabilities[source_index]  # type: ignore[index]
            target = capabilities[target_index]  # type: ignore[index]
            if "adventures" not in source["mechanics"] and "adventures" not in target["mechanics"]:
                continue
            finding_id = (
                f"{prefix}{mechanism}:{source['card_id']}:{source['id']}:"
                f"{target['card_id']}:{target['id']}"
            )
            rows[finding_id] = {
                "audit_class": str(audit_class),
                "source": {
                    "card_id": source["card_id"],
                    "capability_id": source["id"],
                    "face_index": source["face_index"],
                    "adventure_face": "adventure_face" in source["conditions"],
                },
                "target": {
                    "card_id": target["card_id"],
                    "capability_id": target["id"],
                    "face_index": target["face_index"],
                    "adventure_face": "adventure_face" in target["conditions"],
                },
            }
    return rows


def _decoded_finding_id(scoped: Mapping[str, Mapping[str, object]]) -> str | None:
    """Return the one useful finding id the frozen artifact already decoded."""
    matches = [
        finding_id
        for finding_id, row in scoped.items()
        if row["source_capability_id"] == DECODED_SOURCE_CAPABILITY
        and row["target_capability_id"] == DECODED_TARGET_CAPABILITY
        and row["audit_class"] in USEFUL_CLASSES
    ]
    return matches[0] if len(matches) == 1 else None


def gate_scope(
    report: probe590.Report,
    *,
    scoped: Mapping[str, dict[str, object]],
    facts: Mapping[str, object],
    artifact: object,
) -> None:
    """Gate the reconstructed family union against the frozen ledger and artifact."""
    classes = Counter(row["audit_class"] for row in scoped.values())
    families = Counter(facts["families"])  # type: ignore[arg-type]
    family_classes = facts["family_classes"]
    mechanism_rows = Counter(facts["mechanism_rows"])  # type: ignore[arg-type]
    confirmed_ids = [
        row.finding_id for row in artifact.confirmed_relationships  # type: ignore[attr-defined]
    ]
    confirmed_set = set(confirmed_ids)
    missing = sorted(set(scoped) - confirmed_set)
    unknown = sorted(set(classes) - set(EXPECTED_CLASSES))
    scoped_in_order = [finding_id for finding_id in confirmed_ids if finding_id in scoped]
    useful = sum(classes[cls] for cls in USEFUL_CLASSES)
    contradictions = sum(classes[cls] for cls in CONTRADICTION_CLASSES)
    defects = sum(classes[cls] for cls in RECORD_DEFECT_CLASSES)
    decoded_id = _decoded_finding_id(scoped)
    ledger_ids_match = facts["ledger_ids"] == confirmed_set
    duplicate_count = len(facts["duplicates"])  # type: ignore[arg-type]
    report.data["scope"] = {
        "rows": len(scoped),
        "expected_rows": EXPECTED_UNION_ROWS,
        "classes": dict(sorted(classes.items())),
        "expected_classes": EXPECTED_CLASSES,
        "families": dict(sorted(families.items())),
        "expected_family_rows": EXPECTED_FAMILY_ROWS,
        "family_classes": family_classes,
        "expected_family_classes": EXPECTED_FAMILY_CLASSES,
        "mechanism_rows": dict(sorted(mechanism_rows.items())),
        "expected_mechanisms": list(SCOPED_MECHANISMS),
        "searched_mechanisms": facts["ledger_mechanisms"],
        "useful_rows": useful,
        "contradiction_rows": contradictions,
        "record_defect_rows": defects,
        "decoded_row_id": decoded_id,
        "duplicate_ids": facts["duplicates"],
        "unknown_classes": unknown,
        "missing_from_artifact": missing,
        "scoped_in_saved_order": len(scoped_in_order),
        "reconstruction_sha256": probe590.sha256_bytes("\n".join(sorted(scoped)).encode("utf-8")),
        "saved_order_sha256": probe590.sha256_bytes("\n".join(scoped_in_order).encode("utf-8")),
        "ledger_rows": facts["ledger_rows"],
        "ledger_ids_match_artifact_confirmed": ledger_ids_match,
    }
    report.gate(
        "scope_rows_reconstructed",
        ok=(
            len(scoped) == EXPECTED_UNION_ROWS
            and not duplicate_count
            and dict(classes) == EXPECTED_CLASSES
            and not unknown
        ),
        detail=(
            f"rows={len(scoped)} expected={EXPECTED_UNION_ROWS} "
            f"classes={_canonical(dict(sorted(classes.items())))} "
            f"duplicates={duplicate_count} unknown_classes={unknown}"
        ),
        cause_class=probe590.EVIDENCE_DEFECT,
        finding_ids=facts["duplicates"],  # type: ignore[arg-type]
    )
    report.gate(
        "scope_families_reconstructed",
        ok=dict(families) == EXPECTED_FAMILY_ROWS and family_classes == EXPECTED_FAMILY_CLASSES,
        detail=(
            f"families={_canonical(dict(sorted(families.items())))} "
            f"expected={_canonical(EXPECTED_FAMILY_ROWS)} "
            f"family_classes={_canonical(family_classes)}"
        ),
        cause_class=probe590.EVIDENCE_DEFECT,
    )
    report.gate(
        "scope_mechanisms_covered",
        ok=(
            sorted(mechanism_rows) == list(SCOPED_MECHANISMS)
            and facts["ledger_mechanisms"] == list(LEDGER_MECHANISMS)
            and sum(mechanism_rows.values()) == EXPECTED_UNION_ROWS
        ),
        detail=(
            f"scoped={_canonical(dict(sorted(mechanism_rows.items())))} "
            f"expected_scoped={list(SCOPED_MECHANISMS)} "
            f"searched={facts['ledger_mechanisms']} expected_searched={list(LEDGER_MECHANISMS)}"
        ),
        cause_class=probe590.EVIDENCE_DEFECT,
    )
    report.gate(
        "scope_ids_present_in_artifact",
        ok=(
            not missing
            and len(scoped_in_order) == EXPECTED_UNION_ROWS
            and bool(ledger_ids_match)
        ),
        detail=(
            f"scoped_ids={len(scoped)} missing_from_artifact={len(missing)} "
            f"saved_order={len(scoped_in_order)} "
            f"ledger_matches_artifact={ledger_ids_match}"
        ),
        cause_class=probe590.EVIDENCE_DEFECT,
        finding_ids=missing,
    )
    report.gate(
        "scope_bucket_split",
        ok=(
            useful == EXPECTED_USEFUL_ROWS
            and contradictions == EXPECTED_CONTRADICTION_ROWS
            and defects == EXPECTED_RECORD_DEFECT_ROWS
        ),
        detail=(
            f"useful={useful} expected={EXPECTED_USEFUL_ROWS} "
            f"contradictions={contradictions} expected={EXPECTED_CONTRADICTION_ROWS} "
            f"record_defects={defects} expected={EXPECTED_RECORD_DEFECT_ROWS}"
        ),
        cause_class=probe590.EVIDENCE_DEFECT,
    )
    report.gate(
        "scope_decoded_row_present",
        ok=decoded_id is not None,
        detail=f"decoded_row_id={decoded_id!r}",
        cause_class=probe590.EVIDENCE_DEFECT,
    )


def _gate_guide_digest(report: probe590.Report, before: Mapping[str, object]) -> None:
    """Gate the frozen guide digest the shared #590 input gate only checks for presence."""
    guide = before["guide"]  # type: ignore[index]
    report.data["inputs"]["guide_pin"] = {  # type: ignore[index]
        "expected": GUIDE_SHA256,
        "actual": guide["sha256"],  # type: ignore[index]
    }
    report.gate(
        "guide_digest_matches_pin",
        ok=guide["sha256"] == GUIDE_SHA256,  # type: ignore[index]
        detail=f"guide={GUIDE_PATH} sha256={guide['sha256']!r}",  # type: ignore[index]
        cause_class=probe590.EVIDENCE_DEFECT,
    )


# --------------------------------------------------------------------------- #
# relationship compilation and row evaluation
# --------------------------------------------------------------------------- #


def _bucket(audit_class: str) -> str:
    """Return the recovery bucket one audit class belongs to."""
    if audit_class in USEFUL_CLASSES:
        return "useful"
    if audit_class in CONTRADICTION_CLASSES:
        return "subtype_contradiction"
    return "record_defect"


def _expected(bucket: str) -> str:
    """Return the outcome one scoped finding must show without relabelling defects."""
    if bucket == "useful":
        return "qualified"
    if bucket == "subtype_contradiction":
        return "contradiction"
    return "record_defect"


def _card_faces(row: object) -> dict[str, object]:
    """Return the capability, card and face identity of one compiled relationship."""
    projection = row.prerequisite_projection  # type: ignore[attr-defined]
    if projection is None:
        return {}
    return {
        "source": {
            "capability_id": projection.source.capability_id,
            "card_id": projection.source.card_id,
            "card_name": projection.source.card_name,
            "face_index": projection.source.face_index,
            "face_name": projection.source.face_name,
            "card_source_sha256": projection.source.card_source_sha256,
        },
        "target": {
            "capability_id": projection.target.capability_id,
            "card_id": projection.target.card_id,
            "card_name": projection.target.card_name,
            "face_index": projection.target.face_index,
            "face_name": projection.target.face_name,
            "card_source_sha256": projection.target.card_source_sha256,
        },
    }


def evaluate_rows(
    *,
    scoped: Mapping[str, Mapping[str, object]],
    artifact: object,
    compilation: object,
) -> list[dict[str, object]]:
    """Judge every scoped finding against its audit class and the compiler's own outcome."""
    compiled_rows = {
        row.finding_id: row for row in compilation.relationships  # type: ignore[attr-defined]
    }
    conversions = {
        item.finding_id: item for item in compilation.conversions  # type: ignore[attr-defined]
    }
    stored_rows = {
        row.finding_id: row for row in artifact.relationships  # type: ignore[attr-defined]
    }
    records: list[dict[str, object]] = []
    for finding_id in sorted(scoped):
        scope = scoped[finding_id]
        bucket = _bucket(str(scope["audit_class"]))
        record: dict[str, object] = {
            "finding_id": finding_id,
            "mechanism": scope["mechanism"],
            "families": list(scope["families"]),  # type: ignore[arg-type]
            "audit_class": scope["audit_class"],
            "ledger_stage": scope["stage"],
            "ledger_gap": scope["gap"],
            "bucket": bucket,
            "expected": _expected(bucket),
            "outcome": None,
            "reason": None,
            "projection": None,
            "qualifications": {"source": [], "target": []},
            "card_faces": {},
            "profile": None,
            "report": None,
            "record_defect": None,
            "causes": [],
        }
        compiled = compiled_rows.get(finding_id)
        conversion = conversions.get(finding_id)
        stored = stored_rows.get(finding_id)
        if compiled is None or conversion is None or stored is None:
            record["causes"].append(
                "evidence: finding id is not accounted for by the frozen artifact "
                "and its compilation"
            )
            records.append(record)
            continue
        record["outcome"] = conversion.outcome.value
        record["reason"] = conversion.reason
        projection = compiled.prerequisite_projection
        record["projection"] = None if projection is None else projection.outcome.value
        qualifications = probe590._qualification_entries(projection)
        record["qualifications"] = qualifications
        record["card_faces"] = _card_faces(compiled)
        causes: list[str] = record["causes"]  # type: ignore[assignment]
        if bucket == "useful":
            if conversion.outcome.value != "qualified":
                causes.append(
                    f"compiler gap: outcome={conversion.outcome.value} reason={conversion.reason}"
                )
            elif projection is None:
                causes.append("compiler gap: qualified conversion without a projection")
            elif not qualifications["source"] and not qualifications["target"]:
                causes.append("compiler gap: qualified projection without a retained qualification")
        elif bucket == "subtype_contradiction":
            if conversion.outcome.value != "contradiction":
                causes.append(
                    f"compiler gap: expected contradiction, outcome={conversion.outcome.value} "
                    f"reason={conversion.reason}"
                )
            elif conversion.reason != SUBTYPE_CONTRADICTION_REASON:
                causes.append(
                    f"compiler gap: contradiction reason {conversion.reason!r} differs from "
                    f"{SUBTYPE_CONTRADICTION_REASON!r}"
                )
            elif projection is not None:
                causes.append("compiler gap: contradicted relationship was still projected")
            elif _canonical(compiled.to_json()) != _canonical(stored.to_json()):
                causes.append(
                    "compiler gap: contradicted relationship was modified instead of "
                    "left as reviewed"
                )
        else:
            record["record_defect"] = {
                "cause_class": RECORD_DEFECT,
                "outcome": conversion.outcome.value,
                "reason": conversion.reason,
                "note": RECORD_DEFECT_NOTE,
            }
        records.append(record)
    return records


def stage_compilation(
    report: probe590.Report,
    *,
    artifact: object,
    card_database: object,
    scoped: Mapping[str, Mapping[str, object]],
    adventure_rows: Mapping[str, Mapping[str, object]],
) -> tuple[object | None, list[dict[str, object]]]:
    """Compile twice, gate determinism, and evaluate every scoped finding."""
    first = None
    second = None
    error: str | None = None
    try:
        from draftomen.profile_relationship_projection import (
            compile_confirmed_relationship_projections,
        )

        first = compile_confirmed_relationship_projections(
            artifact=artifact, card_database=card_database
        )
        second = compile_confirmed_relationship_projections(
            artifact=artifact, card_database=card_database
        )
    except BaseException as caught:  # noqa: BLE001 - the probe reports, never crashes
        error = f"{type(caught).__name__}: {caught}"
    if first is None or second is None:
        report.data["compilation"] = {"deterministic": False, "error": error}
        report.gate(
            "compile_relationship_rows",
            ok=False,
            detail=f"the relationship compiler did not return two compilations: {error}",
            cause_class=probe590.COMPILER_GAP,
        )
        return None, []
    fingerprints = [probe590._compilation_fingerprint(item) for item in (first, second)]
    deterministic = fingerprints[0] == fingerprints[1]
    payload = ARTIFACT_PATH.read_bytes()
    decoded_id = _decoded_finding_id(scoped)
    records = evaluate_rows(scoped=scoped, artifact=artifact, compilation=first)
    confirmed_ids = [
        row.finding_id for row in artifact.confirmed_relationships  # type: ignore[attr-defined]
    ]
    conversion_ids = [item.finding_id for item in first.conversions]
    report.data["compilation"] = {
        "deterministic": deterministic,
        "fingerprints": fingerprints,
        "scoped_totals": dict(sorted(Counter(record["outcome"] for record in records).items())),
        "scoped_reasons": dict(
            sorted(
                Counter(
                    f"{record['outcome']}/{record['reason']}"
                    for record in records
                    if record["outcome"] != "qualified"
                ).items()
            )
        ),
        "artifact_sha256_after": probe590.sha256_bytes(payload),
        "artifact_bytes_are_canonical": artifact.to_bytes() == payload,  # type: ignore[attr-defined]
        "accounted_rows": len(conversion_ids),
        "accounts_for_every_confirmed_row": conversion_ids == confirmed_ids,
        "conversion_ids": conversion_ids,
        "conversion_order_sha256": probe590.sha256_bytes("\n".join(conversion_ids).encode("utf-8")),
    }
    report.gate(
        "compile_deterministic",
        ok=deterministic,
        detail=_canonical(fingerprints),
        cause_class=probe590.COMPILER_GAP,
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
        cause_class=probe590.COMPILER_GAP,
    )
    conversions = {item.finding_id: item for item in first.conversions}
    useful_adventures = {
        finding_id: conversions.get(finding_id)
        for finding_id, detail in adventure_rows.items()
        if detail["audit_class"] in USEFUL_CLASSES
    }
    unresolved_adventures = {
        finding_id: None if conversion is None else conversion.outcome.value
        for finding_id, conversion in useful_adventures.items()
        if conversion is None or conversion.outcome.value != "qualified"
    }
    adventure_classes = dict(
        sorted(Counter(detail["audit_class"] for detail in adventure_rows.values()).items())
    )
    compiled_rows = {row.finding_id: row for row in first.relationships}
    identity_failures: dict[str, list[str]] = {}
    for finding_id in useful_adventures:
        compiled = compiled_rows.get(finding_id)
        projection = None if compiled is None else compiled.prerequisite_projection
        failures: list[str] = []
        if projection is not None:
            for endpoint in ("source", "target"):
                detail = adventure_rows[finding_id][endpoint]
                if not detail["adventure_face"]:
                    continue
                participant = getattr(projection, endpoint)
                if (
                    participant.card_id != detail["card_id"]
                    or participant.capability_id != detail["capability_id"]
                    or participant.face_index != detail["face_index"]
                ):
                    failures.append(f"{endpoint}_component_identity")
                if not any(item.kind.value == "mode" for item in participant.qualifications):
                    failures.append(f"{endpoint}_sequence_mode")
                if compiled.participants.count(participant.card_id) != 1:
                    failures.append(f"{endpoint}_drafted_identity_count")
        if failures:
            identity_failures[finding_id] = failures
    report.data["adventure_family"] = {
        "rows": len(adventure_rows),
        "expected_rows": EXPECTED_ADVENTURE_ROWS,
        "classes": adventure_classes,
        "expected_classes": EXPECTED_ADVENTURE_CLASSES,
        "useful_rows": len(useful_adventures),
        "expected_useful_rows": EXPECTED_ADVENTURE_USEFUL_ROWS,
        "finding_ids": sorted(adventure_rows),
        "useful_finding_ids": sorted(useful_adventures),
        "unresolved_useful": unresolved_adventures,
        "identity_or_sequence_failures": identity_failures,
    }
    report.gate(
        "adventure_family_useful_rows_recovered",
        ok=(
            len(adventure_rows) == EXPECTED_ADVENTURE_ROWS
            and adventure_classes == EXPECTED_ADVENTURE_CLASSES
            and len(useful_adventures) == EXPECTED_ADVENTURE_USEFUL_ROWS
            and not unresolved_adventures
            and not identity_failures
        ),
        detail=(
            f"rows={len(adventure_rows)} useful={len(useful_adventures)} "
            f"unresolved={len(unresolved_adventures)} "
            f"identity_or_sequence={len(identity_failures)} "
            f"classes={_canonical(adventure_classes)}"
        ),
        cause_class=probe590.COMPILER_GAP,
        finding_ids=sorted(set(unresolved_adventures) | set(identity_failures)),
    )
    report.gate(
        "compiled_artifact_unchanged",
        ok=(
            probe590.sha256_bytes(payload) == ARTIFACT_NAME.removesuffix(".json")
            and artifact.to_bytes() == payload  # type: ignore[attr-defined]
        ),
        detail=f"artifact_sha256={probe590.sha256_bytes(payload)}",
        cause_class=probe590.INPUT_MUTATION,
    )

    useful = [record for record in records if record["bucket"] == "useful"]
    contradictions = [record for record in records if record["bucket"] == "subtype_contradiction"]
    defects = [record for record in records if record["bucket"] == "record_defect"]
    qualified = [record for record in useful if record["outcome"] == "qualified"]
    decoded = [record for record in useful if record["outcome"] == "decoded"]
    report.data["row_groups"] = {
        "useful": {
            "count": len(useful),
            "expected": EXPECTED_USEFUL_ROWS,
            "finding_ids": [record["finding_id"] for record in useful],
            "outcomes": dict(
                sorted(Counter(str(record["outcome"]) for record in useful).items())
            ),
        },
        "subtype_contradictions": {
            "count": len(contradictions),
            "expected": EXPECTED_CONTRADICTION_ROWS,
            "finding_ids": [record["finding_id"] for record in contradictions],
            "reasons": dict(
                sorted(Counter(str(record["reason"]) for record in contradictions).items())
            ),
        },
        "record_defects": {
            "count": len(defects),
            "expected": EXPECTED_RECORD_DEFECT_ROWS,
            "cause_class": RECORD_DEFECT,
            "finding_ids": [record["finding_id"] for record in defects],
            "outcomes": {
                str(record["finding_id"]): {
                    "outcome": record["outcome"],
                    "reason": record["reason"],
                }
                for record in defects
            },
            "note": RECORD_DEFECT_NOTE,
        },
        "qualified_count": len(qualified),
        "expected_qualified": EXPECTED_QUALIFIED_ROWS,
        "decoded_ids": [record["finding_id"] for record in decoded],
        "expected_decoded_row": decoded_id,
    }
    failing_useful = [record for record in useful if record["causes"]]
    failing_negatives = [record for record in contradictions if record["causes"]]
    defect_outcomes = {
        str(record["finding_id"]): record["outcome"] for record in defects
    }
    report.gate(
        "useful_rows_recovered",
        ok=(
            not failing_useful
            and len(useful) == EXPECTED_USEFUL_ROWS
            and len(qualified) == EXPECTED_QUALIFIED_ROWS
            and not decoded
        ),
        detail=(
            f"useful={len(useful)} expected={EXPECTED_USEFUL_ROWS} "
            f"qualified={len(qualified)} expected={EXPECTED_QUALIFIED_ROWS} "
            f"decoded={len(decoded)} expected=0 "
            f"failing={len(failing_useful)}: "
            + probe590._summarize(
                Counter(
                    cause
                    for record in failing_useful
                    for cause in record["causes"]  # type: ignore[union-attr]
                )
            )
        ),
        cause_class=probe590.COMPILER_GAP,
        finding_ids=[record["finding_id"] for record in failing_useful],  # type: ignore[misc]
    )
    report.gate(
        "subtype_negatives_contradicted",
        ok=not failing_negatives and len(contradictions) == EXPECTED_CONTRADICTION_ROWS,
        detail=(
            f"negatives={len(contradictions)} expected={EXPECTED_CONTRADICTION_ROWS} "
            f"failing={len(failing_negatives)}: "
            + probe590._summarize(
                Counter(
                    cause
                    for record in failing_negatives
                    for cause in record["causes"]  # type: ignore[union-attr]
                )
            )
        ),
        cause_class=probe590.COMPILER_GAP,
        finding_ids=[record["finding_id"] for record in failing_negatives],  # type: ignore[misc]
    )
    report.gate(
        "record_defects_reported_separately",
        ok=len(defects) == EXPECTED_RECORD_DEFECT_ROWS
        and all(record["record_defect"] is not None for record in defects),
        detail=(
            f"record_defects={len(defects)} expected={EXPECTED_RECORD_DEFECT_ROWS} "
            f"outcomes={_canonical(defect_outcomes)}"
        ),
        cause_class=probe590.EVIDENCE_DEFECT,
        finding_ids=[record["finding_id"] for record in defects],  # type: ignore[misc]
    )
    return first, records


# --------------------------------------------------------------------------- #
# derived condition map
# --------------------------------------------------------------------------- #


def _map_counts(condition_map: object) -> dict[str, object]:
    """Summarize one condition map by family, role, kind and support."""
    capabilities = condition_map.capabilities  # type: ignore[attr-defined]
    interactions = condition_map.interactions  # type: ignore[attr-defined]
    by_id = {capability.capability_id: capability for capability in capabilities}
    family_role_kind: dict[str, dict[str, dict[str, int]]] = {}
    for capability in capabilities:
        roles = family_role_kind.setdefault(capability.family, {})
        kinds = roles.setdefault(capability.role, {})
        kinds[capability.kind] = kinds.get(capability.kind, 0) + 1
    family_support: dict[str, dict[str, int]] = {}
    for interaction in interactions:
        family = by_id[interaction.enabler_id].family
        supports = family_support.setdefault(family, {})
        supports[interaction.support] = supports.get(interaction.support, 0) + 1
    return {
        "sources": len(condition_map.sources),
        "capabilities": len(capabilities),
        "capabilities_by_family": dict(sorted(Counter(c.family for c in capabilities).items())),
        "capabilities_by_role": dict(sorted(Counter(c.role for c in capabilities).items())),
        "capabilities_by_kind": dict(sorted(Counter(c.kind for c in capabilities).items())),
        "capabilities_family_role_kind": {
            family: {
                role: dict(sorted(kinds.items())) for role, kinds in sorted(roles.items())
            }
            for family, roles in sorted(family_role_kind.items())
        },
        "payoffs_by_family": dict(
            sorted(Counter(c.family for c in capabilities if c.role == "payoff").items())
        ),
        "enablers_by_family": dict(
            sorted(Counter(c.family for c in capabilities if c.role == "enabler").items())
        ),
        "interactions": len(interactions),
        "interactions_by_family": dict(
            sorted(Counter(by_id[i.enabler_id].family for i in interactions).items())
        ),
        "interactions_by_support": dict(sorted(Counter(i.support for i in interactions).items())),
        "interactions_family_support": {
            family: dict(sorted(supports.items()))
            for family, supports in sorted(family_support.items())
        },
        "capability_ids_unique": len({c.capability_id for c in capabilities}) == len(capabilities),
        "interactions_canonical": list(interactions)
        == sorted(interactions, key=lambda item: (item.enabler_id, item.payoff_id)),
    }


def stage_condition_map(
    report: probe590.Report, *, artifact: object, card_database: object
) -> object | None:
    """Compile the derived condition map twice and gate its presence and determinism."""
    first = None
    second = None
    error: str | None = None
    try:
        from draftomen.profile_condition_projection import compile_condition_map

        first = compile_condition_map(artifact=artifact, card_database=card_database)
        second = compile_condition_map(artifact=artifact, card_database=card_database)
    except BaseException as caught:  # noqa: BLE001 - the probe reports, never crashes
        error = f"{type(caught).__name__}: {caught}"
    first_canonical = _map_canonical(first)
    second_canonical = _map_canonical(second)
    deterministic = first_canonical is not None and first_canonical == second_canonical
    report.data["condition_map"] = {
        "present": first is not None,
        "deterministic": deterministic,
        "fingerprint_sha256": (
            None if first_canonical is None else probe590.sha256_bytes(first_canonical.encode("utf-8"))
        ),
        "counts": _map_counts(first) if first is not None else None,
        "error": error,
    }
    report.gate(
        "condition_map_compiled",
        ok=first is not None,
        detail=(
            f"the derived condition map is unavailable: {error}"
            if first is None
            else (
                f"fingerprint={report.data['condition_map']['fingerprint_sha256']} "  # type: ignore[index]
                f"counts={_canonical(report.data['condition_map']['counts'])}"  # type: ignore[index]
            )
        ),
        cause_class=probe590.COMPILER_GAP,
    )
    report.gate(
        "condition_map_deterministic",
        ok=deterministic,
        detail=f"first={report.data['condition_map']['fingerprint_sha256']} error={error}",  # type: ignore[index]
        cause_class=probe590.COMPILER_GAP,
    )
    return first


def _face_names(card_database: object) -> dict[tuple[int, int | None], str | None]:
    """Return the card name of every face of the frozen card database."""
    names: dict[tuple[int, int | None], str | None] = {}
    for card_id, card in card_database.cards.items():  # type: ignore[attr-defined]
        if card.faces:
            for index, face in enumerate(card.faces):
                names[(card_id, index)] = face.name
        else:
            names[(card_id, None)] = card.name
    return names


def printed_family_faces(card_database: object) -> dict[str, set[tuple[int, int | None]]]:
    """Return the printed family clause faces of the frozen card database."""
    found: dict[str, set[tuple[int, int | None]]] = {family: set() for family in FAMILY_TAGS}
    for card_id, card in card_database.cards.items():  # type: ignore[attr-defined]
        if card.faces:
            faces: tuple[tuple[int | None, str | None], ...] = tuple(
                (index, face.oracle_text) for index, face in enumerate(card.faces)
            )
        else:
            faces = ((None, card.oracle_text),)
        for family, marker in FAMILY_MARKERS.items():
            if any(text is not None and marker in text for _, text in faces):
                face_index = next(
                    index for index, text in faces if text is not None and marker in text
                )
                found[family].add((card_id, face_index))
    return found


def gate_printed_family_cards(report: probe590.Report, *, card_database: object) -> None:
    """Gate the declared target faces against the frozen card database's printed clauses."""
    printed = printed_family_faces(card_database)
    expected = {family: set(faces) for family, faces in EXPECTED_TARGET_FACES.items()}
    missing = {
        family: sorted(expected[family] - printed[family], key=_face_sort_key)
        for family in FAMILY_TAGS
    }
    unexpected = {
        family: sorted(printed[family] - expected[family], key=_face_sort_key)
        for family in FAMILY_TAGS
    }
    report.data["printed_family_faces"] = {
        family: sorted(faces, key=_face_sort_key) for family, faces in printed.items()
    }
    report.gate(
        "printed_family_cards_match",
        ok=printed == expected,
        detail=f"missing={_canonical(missing)} unexpected={_canonical(unexpected)}",
        cause_class=probe590.EVIDENCE_DEFECT,
    )


def _target_coverage(
    condition_map: object, *, card_database: object, endpoint_cards: set[int]
) -> dict[str, list[dict[str, object]]]:
    """Return the declared printed targets' payoff nodes, evidence and saved-row overlap."""
    sources = {
        (source.card_id, source.face_index): source for source in condition_map.sources  # type: ignore[attr-defined]
    }
    names = _face_names(card_database)
    coverage: dict[str, list[dict[str, object]]] = {}
    for family, faces in EXPECTED_TARGET_FACES.items():
        phrases = PAYOFF_EVIDENCE_PHRASES[family]
        entries: list[dict[str, object]] = []
        for card_id, face_index in faces:
            payoffs = [
                capability
                for capability in condition_map.capabilities  # type: ignore[attr-defined]
                if capability.family == family
                and capability.role == "payoff"
                and capability.source_card_id == card_id
                and capability.source_face_index == face_index
            ]
            evidence = " ".join(_evidence_text(capability) for capability in payoffs)
            source = sources.get((card_id, face_index))
            entries.append(
                {
                    "card_id": card_id,
                    "face_index": face_index,
                    "face_name": names.get((card_id, face_index)),
                    "source_type_line": None if source is None else source.type_line,
                    "payoff_count": len(payoffs),
                    "payoff_ids": [capability.capability_id for capability in payoffs],
                    "payoff_kind": payoffs[0].kind if len(payoffs) == 1 else None,
                    "controller": payoffs[0].controller if len(payoffs) == 1 else None,
                    "quantity": (
                        None
                        if len(payoffs) != 1 or payoffs[0].quantity is None
                        else {
                            "value": payoffs[0].quantity.value,
                            "relation": payoffs[0].quantity.relation.value,
                        }
                    ),
                    "evidence_phrases": list(phrases),
                    "evidence_ok": bool(payoffs) and all(phrase in evidence for phrase in phrases),
                    "saved_endpoint": card_id in endpoint_cards,
                    "derived_only": card_id not in endpoint_cards,
                }
            )
        coverage[family] = entries
    return coverage


def _expected_payoff_faces(condition_map: object) -> dict[str, list[tuple[int, int | None]]]:
    """Return the payoff faces the compiled map actually carries per family."""
    return {
        family: sorted(
            {
                (capability.source_card_id, capability.source_face_index)
                for capability in condition_map.capabilities  # type: ignore[attr-defined]
                if capability.family == family and capability.role == "payoff"
            },
            key=_face_sort_key,
        )
        for family in FAMILY_TAGS
    }


def gate_targets(
    report: probe590.Report,
    *,
    condition_map: object | None,
    card_database: object,
    endpoint_cards: set[int],
) -> None:
    """Gate every printed family target's payoff node, face identity and retained evidence."""
    if condition_map is None:
        report.data["targets"] = {"coverage": None, "endpoint_free": None}
        report.gate(
            "target_payoffs_present",
            ok=False,
            detail="the derived condition map is unavailable",
            cause_class=probe590.COMPILER_GAP,
        )
        return
    coverage = _target_coverage(
        condition_map, card_database=card_database, endpoint_cards=endpoint_cards
    )
    expected_faces = {
        family: sorted(faces, key=_face_sort_key) for family, faces in EXPECTED_TARGET_FACES.items()
    }
    derived_faces = _expected_payoff_faces(condition_map)
    missing = [
        f"{family}:{card_id}:{face_index}"
        for family, entries in coverage.items()
        for entry in entries
        if entry["payoff_count"] != 1
    ]
    evidence_gaps = [
        f"{family}:{entry['card_id']}"
        for family, entries in coverage.items()
        for entry in entries
        if not entry["evidence_ok"]
    ]
    computed_endpoint_free = {
        family: sorted(
            card_id
            for card_id, _face_index in EXPECTED_TARGET_FACES[family]
            if card_id not in endpoint_cards
        )
        for family in FAMILY_TAGS
    }
    expected_endpoint_free = {
        family: sorted(cards) for family, cards in EXPECTED_ENDPOINT_FREE.items()
    }
    report.data["targets"] = {
        "coverage": coverage,
        "payoff_faces": derived_faces,
        "expected_payoff_faces": expected_faces,
        "endpoint_free": computed_endpoint_free,
        "expected_endpoint_free": expected_endpoint_free,
        "missing_payoffs": missing,
        "evidence_gaps": evidence_gaps,
    }
    report.gate(
        "target_payoffs_present",
        ok=not missing,
        detail=f"{len(missing)} declared printed targets lack exactly one payoff node: {missing}",
        cause_class=probe590.COMPILER_GAP,
    )
    report.gate(
        "target_payoff_sets_exact",
        ok=derived_faces == expected_faces,
        detail=(
            f"derived={_canonical(derived_faces)} expected={_canonical(expected_faces)}"
        ),
        cause_class=probe590.COMPILER_GAP,
    )
    report.gate(
        "target_payoff_evidence_retained",
        ok=not evidence_gaps,
        detail=(
            f"{len(evidence_gaps)} payoff nodes lost their defining condition: {evidence_gaps} "
            f"phrases={_canonical(PAYOFF_EVIDENCE_PHRASES)}"
        ),
        cause_class=probe590.COMPILER_GAP,
    )
    report.gate(
        "target_endpoint_free_recovered",
        ok=computed_endpoint_free == expected_endpoint_free,
        detail=(
            f"endpoint_free={_canonical(computed_endpoint_free)} "
            f"expected={_canonical(expected_endpoint_free)}"
        ),
        cause_class=probe590.EVIDENCE_DEFECT,
    )


# --------------------------------------------------------------------------- #
# helper edge cases
# --------------------------------------------------------------------------- #


def _positive_case(
    spec: Mapping[str, object],
    *,
    interactions: Iterable[object],
    by_id: Mapping[str, object],
    saved_pairs: set[str],
) -> dict[str, object]:
    """Evaluate one required helper edge against the compiled condition map."""
    matched: list[tuple[object, object, object]] = []
    for interaction in interactions:
        enabler = by_id[interaction.enabler_id]  # type: ignore[attr-defined]
        payoff = by_id[interaction.payoff_id]  # type: ignore[attr-defined]
        if enabler.kind != spec["enabler_kind"] or enabler.source_card_id != spec["enabler_card_id"]:
            continue
        if enabler.source_face_index != spec["enabler_face_index"]:
            continue
        if spec["enabler_quantity_value"] is not None and (
            enabler.quantity is None or enabler.quantity.value != spec["enabler_quantity_value"]
        ):
            continue
        if payoff.role != "payoff" or payoff.family != spec["payoff_family"]:
            continue
        if (
            payoff.source_card_id != spec["payoff_card_id"]
            or payoff.source_face_index != spec["payoff_face_index"]
        ):
            continue
        if spec["payoff_quantity_value"] is not None and (
            payoff.quantity is None or payoff.quantity.value != spec["payoff_quantity_value"]
        ):
            continue
        matched.append((interaction, enabler, payoff))
    supported = [item for item in matched if item[0].support == spec["support"]]  # type: ignore[attr-defined]
    phrases = tuple(spec["enabler_evidence_phrases"])  # type: ignore[arg-type]
    retained = [
        item for item in supported if all(phrase in _evidence_text(item[1]) for phrase in phrases)
    ]
    return {
        "name": spec["name"],
        "description": spec["description"],
        "expected_support": spec["support"],
        "expected_evidence_phrases": list(phrases),
        "ok": bool(retained),
        "enabler_matches": len(matched),
        "matched": [
            _edge_detail(
                interaction=item[0], enabler=item[1], payoff=item[2], saved_pairs=saved_pairs
            )
            for item in supported
        ],
        "wrong_support": [
            _edge_detail(
                interaction=item[0], enabler=item[1], payoff=item[2], saved_pairs=saved_pairs
            )
            for item in matched
            if item[0].support != spec["support"]  # type: ignore[attr-defined]
        ],
        "missing_evidence_phrases": [
            phrase
            for phrase in phrases
            if not any(phrase in _evidence_text(item[1]) for item in supported)
        ],
    }


def _negative_checks(
    condition_map: object, *, reviewed_ids: set[str]
) -> list[dict[str, object]]:
    """Check every declared invalid helper association is absent from the compiled map."""
    capabilities = condition_map.capabilities  # type: ignore[attr-defined]
    interactions = condition_map.interactions  # type: ignore[attr-defined]
    by_id = {capability.capability_id: capability for capability in capabilities}
    checks: list[dict[str, object]] = []

    land_entries = [capability for capability in capabilities if capability.kind == "land_entry"]
    land_to_hand = sorted(
        capability.capability_id
        for capability in land_entries
        if (capability.source_card_id, capability.source_face_index) in LAND_TO_HAND_SOURCES
        or "onto the battlefield" not in _evidence_text(capability).casefold()
    )
    checks.append(
        {
            "name": "land_to_hand_never_an_entry_helper",
            "description": (
                "Land-to-hand tutoring and mill-to-hand returns never become land-entry helpers."
            ),
            "ok": not land_to_hand,
            "offending": land_to_hand,
            "detail": {
                "land_entry_capabilities": [
                    _capability_label(capability) for capability in land_entries
                ],
                "declared_land_to_hand_sources": [list(item) for item in LAND_TO_HAND_SOURCES],
            },
        }
    )

    target_payoffs = {
        capability.capability_id
        for capability in capabilities
        if capability.role == "payoff"
        and (capability.source_card_id, capability.source_face_index)
        in EXPECTED_TARGET_FACES[capability.family]
    }
    opponent_edges = sorted(
        f"{interaction.enabler_id}->{interaction.payoff_id}"
        for interaction in interactions
        if by_id[interaction.enabler_id].controller == "opponent"
        and by_id[interaction.payoff_id].controller == "you"
    )
    foreign_edges = sorted(
        f"{interaction.enabler_id}->{interaction.payoff_id}"
        for interaction in interactions
        if interaction.payoff_id in target_payoffs
        and by_id[interaction.enabler_id].controller != "you"
    )
    checks.append(
        {
            "name": "opponent_controller_never_links_own_payoffs",
            "description": (
                "Opponent-only or unresolved-controller sources never enable an own-controller "
                "family payoff."
            ),
            "ok": not opponent_edges and not foreign_edges,
            "offending": sorted({*opponent_edges, *foreign_edges}),
            "detail": {
                "declared_target_payoffs": len(target_payoffs),
                "controller_counts": dict(
                    sorted(Counter(capability.controller for capability in capabilities).items())
                ),
            },
        }
    )

    ferocious_short = sorted(
        f"{interaction.enabler_id}->{interaction.payoff_id}"
        for interaction in interactions
        if by_id[interaction.payoff_id].family == "ferocious"
        and interaction.support == "can_enable"
        and (
            by_id[interaction.enabler_id].quantity is None
            or by_id[interaction.payoff_id].quantity is None
            or by_id[interaction.enabler_id].quantity.value is None
            or by_id[interaction.payoff_id].quantity.value is None
            or by_id[interaction.enabler_id].quantity.value
            < by_id[interaction.payoff_id].quantity.value
        )
    )
    misty_ferocious = sorted(
        capability.capability_id
        for capability in capabilities
        if capability.family == "ferocious"
        and capability.source_card_id == 103482
        and not (
            capability.kind == "created_creature_power"
            and capability.quantity is not None
            and capability.quantity.value == 6
        )
    )
    treasure_only = sorted(
        capability.capability_id
        for capability in capabilities
        if capability.family == "ferocious"
        and "treasure" in _evidence_text(capability).casefold()
        and "dragon" not in _evidence_text(capability).casefold()
    )
    checks.append(
        {
            "name": "ferocious_edges_meet_the_power_gate",
            "description": (
                "Treasure-only production and a bare 0/0 Army never enable a power-four gate, "
                "and no can_enable edge is backed by a fixed power below the threshold."
            ),
            "ok": not ferocious_short and not misty_ferocious and not treasure_only,
            "offending": sorted({*ferocious_short, *misty_ferocious, *treasure_only}),
            "detail": {
                "ferocious_capabilities": [
                    _capability_label(capability)
                    for capability in capabilities
                    if capability.family == "ferocious"
                ]
            },
        }
    )

    sources = {
        (source.card_id, source.face_index): source
        for source in condition_map.sources  # type: ignore[attr-defined]
    }
    storied = [capability for capability in capabilities if capability.family == "storied"]
    nonqualifying = sorted(
        capability.capability_id
        for capability in storied
        if capability.kind == "qualifying_permanent"
        and not any(
            term in _source_type_line(sources, capability)
            for term in ("Artifact", "Legendary", "Saga")
        )
    )
    creation_without_qualification = sorted(
        capability.capability_id
        for capability in storied
        if capability.kind == "created_qualifying_permanents"
        and not any(
            term in _evidence_text(capability).casefold()
            for term in ("treasure", "artifact", "legendary", "saga")
        )
    )
    army_contributions = sorted(
        capability.capability_id
        for capability in storied
        if "goblin army" in _evidence_text(capability).casefold()
    )
    known_nonqualifiers = sorted(
        capability.capability_id
        for capability in storied
        if (capability.source_card_id, capability.source_face_index)
        in STORIED_NONQUALIFYING_SOURCES
    )
    checks.append(
        {
            "name": "storied_nonqualifiers_never_contribute",
            "description": (
                "A Forest, an ordinary creature and a Goblin Army never contribute to a "
                "storied condition; eligible contributions stay artifact, legendary or Saga."
            ),
            "ok": (
                not nonqualifying
                and not creation_without_qualification
                and not army_contributions
                and not known_nonqualifiers
            ),
            "offending": sorted(
                {
                    *nonqualifying,
                    *creation_without_qualification,
                    *army_contributions,
                    *known_nonqualifiers,
                }
            ),
            "detail": {
                "storied_capabilities": [
                    _capability_label(capability) for capability in storied
                ],
                "declared_nonqualifying_sources": [
                    list(item) for item in STORIED_NONQUALIFYING_SOURCES
                ],
            },
        }
    )

    derived_ids = {capability.capability_id for capability in capabilities}
    collisions = sorted(derived_ids & reviewed_ids)
    prefix_in_reviewed = sorted(
        finding_id for finding_id in reviewed_ids if CONDITION_CAPABILITY_PREFIX in finding_id
    )
    checks.append(
        {
            "name": "derived_identities_never_reviewed_findings",
            "description": (
                "No derived condition capability identity appears among the 685 reviewed "
                "relationship ids."
            ),
            "ok": not collisions and not prefix_in_reviewed,
            "offending": sorted({*collisions, *prefix_in_reviewed}),
            "detail": {
                "derived_capabilities": len(derived_ids),
                "reviewed_findings": len(reviewed_ids),
            },
        }
    )
    return checks


def evaluate_helper_cases(
    condition_map: object, *, reviewed_ids: set[str], saved_pairs: set[str]
) -> dict[str, object]:
    """Evaluate every declared positive and negative helper case on the production map."""
    capabilities = condition_map.capabilities  # type: ignore[attr-defined]
    by_id = {capability.capability_id: capability for capability in capabilities}
    positives = [
        _positive_case(
            spec,
            interactions=condition_map.interactions,  # type: ignore[attr-defined]
            by_id=by_id,
            saved_pairs=saved_pairs,
        )
        for spec in POSITIVE_HELPER_CASES
    ]
    negatives = _negative_checks(condition_map, reviewed_ids=reviewed_ids)
    return {
        "positives": positives,
        "negatives": negatives,
        "passed": all(case["ok"] for case in positives)
        and all(case["ok"] for case in negatives),
    }


def gate_helper_cases(
    report: probe590.Report,
    *,
    condition_map: object | None,
    reviewed_ids: set[str],
    saved_pairs: set[str],
) -> None:
    """Gate the declared positive and negative helper edges on the production map."""
    if condition_map is None:
        report.data["helper_cases"] = {"positives": [], "negatives": [], "passed": False}
        report.gate(
            "helper_positive_edges",
            ok=False,
            detail="the derived condition map is unavailable",
            cause_class=probe590.COMPILER_GAP,
        )
        report.gate(
            "helper_negative_edges",
            ok=False,
            detail="the derived condition map is unavailable",
            cause_class=probe590.COMPILER_GAP,
        )
        return
    cases = evaluate_helper_cases(
        condition_map, reviewed_ids=reviewed_ids, saved_pairs=saved_pairs
    )
    report.data["helper_cases"] = cases
    failing_positives = [case for case in cases["positives"] if not case["ok"]]  # type: ignore[union-attr]
    failing_negatives = [case for case in cases["negatives"] if not case["ok"]]  # type: ignore[union-attr]
    report.gate(
        "helper_positive_edges",
        ok=not failing_positives,
        detail=(
            f"{len(failing_positives)} of {len(cases['positives'])} required helper edges are "  # type: ignore[arg-type]
            "missing or unsupported: "
            + ", ".join(
                f"{case['name']}(matched={case['enabler_matches']}, "
                f"wrong_support={len(case['wrong_support'])}, "
                f"missing_phrases={case['missing_evidence_phrases']})"
                for case in failing_positives
            )
        ),
        cause_class=probe590.COMPILER_GAP,
    )
    report.gate(
        "helper_negative_edges",
        ok=not failing_negatives,
        detail=(
            f"{len(failing_negatives)} of {len(cases['negatives'])} invalid helper checks fired: "  # type: ignore[arg-type]
            + ", ".join(
                f"{case['name']}(offending={case['offending']})" for case in failing_negatives
            )
        ),
        cause_class=probe590.COMPILER_GAP,
    )


# --------------------------------------------------------------------------- #
# published generation
# --------------------------------------------------------------------------- #


def _normalized_argv(argv: object) -> list[str] | None:
    """Return the CLI argv with the temporary output directory replaced."""
    if not isinstance(argv, list):
        return None
    normalized = [str(item) for item in argv]
    for index, item in enumerate(normalized):
        if item == "--output-dir" and index + 1 < len(normalized):
            normalized[index + 1] = "<output-dir>"
    return normalized


def _generation_snapshot(
    report: probe590.Report,
    *,
    index: int,
    work_dir: Path,
    direct_sha256: str | None,
    reviewed_ids: set[str],
) -> dict[str, object]:
    """Capture one published generation's deterministic and round-trip facts."""
    generation = report.data.get("generation")
    generation = generation if isinstance(generation, dict) else {}
    snapshot: dict[str, object] = {
        "index": index,
        "work_dir": str(work_dir),
        "exit_code": generation.get("exit_code"),
        "error": generation.get("error"),
        "artifact_path": generation.get("artifact_path"),
        "artifact_sha256": generation.get("artifact_sha256"),
        "normalized_argv": _normalized_argv(generation.get("argv")),
        "stderr_tail": generation.get("stderr_tail"),
        "profile_sha256": None,
        "content_sha256": None,
        "reviewed_rows": None,
        "reviewed_rows_match": False,
        "condition_map_sha256": None,
        "condition_map_matches": False,
        "round_trip_condition_map_matches": False,
        "round_trip_reviewed_rows_match": False,
        "replacement_rows": None,
        "replacement_sources": None,
        "replacement_rows_valid": False,
        "gift_capability_valid": False,
        "hone_capabilities_valid": False,
        "hone_relationship_valid": False,
        "profile_error": None,
        "round_trip_error": None,
    }
    from draftomen.set_profile import SetProfile, load_set_profile

    artifact_path = snapshot["artifact_path"]
    if isinstance(artifact_path, str):
        try:
            content = gzip.decompress(Path(artifact_path).read_bytes())
            snapshot["content_sha256"] = probe590.sha256_bytes(content)
            profile = SetProfile.from_json(json.loads(content.decode("utf-8")))
            snapshot["profile_sha256"] = probe590.sha256_bytes(profile.to_bytes())
            role_card = (
                None
                if profile.role_profile is None
                else profile.role_profile.card("arena_id:103372")
            )
            if role_card is not None:
                gift = tuple(
                    assignment
                    for assignment in role_card.assignments
                    if assignment.role.value == "gift"
                )
                snapshot["gift_capability_valid"] = (
                    len(gift) == 1
                    and gift[0].evidence[0] == "Gift a Treasure"
                    and gift[0].evidence[1] == "players can't cast spells this turn"
                    and gift[0].parameters is not None
                    and gift[0].parameters.to_json()
                    == {
                        "gift": "treasure",
                        "kind": "gift",
                        "optional": True,
                        "qualified_effect": "players can't cast spells this turn",
                        "recipient": "opponent",
                    }
                    and all(
                        assignment.role.value != "token_maker"
                        for assignment in role_card.assignments
                    )
                )
            dwalin = (
                None
                if profile.role_profile is None
                else profile.role_profile.card("arena_id:103534")
            )
            sting = (
                None
                if profile.role_profile is None
                else profile.role_profile.card("arena_id:103562")
            )
            if dwalin is not None and sting is not None:
                dwalin_hone = tuple(
                    item for item in dwalin.assignments if item.role.value == "hone_counter_source"
                )
                sting_source = tuple(
                    item for item in sting.assignments if item.role.value == "hone_counter_source"
                )
                sting_payoff = tuple(
                    item for item in sting.assignments if item.role.value == "hone_equipment_payoff"
                )
                snapshot["hone_capabilities_valid"] = (
                    len(dwalin_hone) == len(sting_source) == len(sting_payoff) == 1
                    and dwalin_hone[0].parameters is not None
                    and dwalin_hone[0].parameters.to_json()
                    == {
                        "controller": "you",
                        "kind": "hone_source",
                        "quantity_basis": "one_each",
                        "self_only": False,
                        "target": "equipment",
                        "timing": "enters_or_attacks",
                    }
                    and sting_source[0].parameters is not None
                    and sting_source[0].parameters.to_json()["quantity_basis"]
                    == "opponent_creatures"
                    and sting_payoff[0].parameters is not None
                    and sting_payoff[0].parameters.to_json()["power_per_counter"] == 1
                    and any(item.role.value == "equipment" for item in sting.assignments)
                )
            enhancement = profile.enhancement
            if enhancement is not None:
                published_ids = {row.finding_id for row in enhancement.relationships}
                snapshot["reviewed_rows"] = len(published_ids)
                snapshot["reviewed_rows_match"] = published_ids == reviewed_ids
                replacement_rows = tuple(
                    row
                    for row in enhancement.relationships
                    if row.mechanism == "token-source-replacement"
                )
                snapshot["replacement_rows"] = len(replacement_rows)
                snapshot["replacement_sources"] = sorted(
                    row.prerequisite_projection.source.card_id
                    for row in replacement_rows
                    if row.prerequisite_projection is not None
                )
                snapshot["replacement_rows_valid"] = all(
                    row.prerequisite_projection is not None
                    and row.prerequisite_projection.source.role.value == "token_maker"
                    and row.prerequisite_projection.target.card_id == 103524
                    and row.prerequisite_projection.target.role.value == "token_replacement"
                    for row in replacement_rows
                )
                hone_rows = tuple(
                    row
                    for row in enhancement.relationships
                    if row.mechanism == "hone-equipment-payoff"
                )
                snapshot["hone_relationship_valid"] = (
                    len(hone_rows) == 1
                    and hone_rows[0].participants == (103534, 103562)
                    and hone_rows[0].prerequisite_projection is not None
                    and hone_rows[0].prerequisite_projection.source.role.value
                    == "hone_counter_source"
                    and hone_rows[0].prerequisite_projection.target.role.value
                    == "hone_equipment_payoff"
                    and {
                        item.selector
                        for item in hone_rows[0].prerequisite_projection.source.qualifications
                    }
                    == {
                        "Whenever Dwalin enters or attacks",
                        "put a hone counter on each Equipment you control",
                    }
                )
                snapshot["condition_map_sha256"] = _map_sha256(enhancement.condition_map)
                snapshot["condition_map_matches"] = (
                    direct_sha256 is not None
                    and snapshot["condition_map_sha256"] == direct_sha256
                )
        except BaseException as caught:  # noqa: BLE001 - the probe reports, never crashes
            snapshot["profile_error"] = f"{type(caught).__name__}: {caught}"
    round_trip_path = work_dir / "round-trip-profile.json"
    if round_trip_path.is_file():
        try:
            reloaded = load_set_profile(
                round_trip_path,
                expected_set_code=probe590.SET_CODE.casefold(),
                expected_format=probe590.EVENT_FORMAT,
            )
            enhancement = reloaded.enhancement
            if enhancement is not None:
                snapshot["round_trip_reviewed_rows_match"] = {
                    row.finding_id for row in enhancement.relationships
                } == reviewed_ids
                snapshot["round_trip_condition_map_matches"] = (
                    direct_sha256 is not None
                    and _map_sha256(enhancement.condition_map) == direct_sha256
                )
        except BaseException as caught:  # noqa: BLE001 - the probe reports, never crashes
            snapshot["round_trip_error"] = f"{type(caught).__name__}: {caught}"
    return snapshot


def stage_generations(
    report: probe590.Report,
    *,
    artifact: object,
    compilation: object | None,
    records: list[dict[str, object]],
    work_root: Path,
    condition_map: object | None,
    card_database: object,
) -> None:
    """Run the published offline consumer twice and compare both published profiles."""
    if compilation is None:
        report.data["generation_runs"] = []
        report.gate(
            "generation_runs_succeeded",
            ok=False,
            detail="the relationship compilation is unavailable",
            cause_class=probe590.GENERATION,
        )
        return
    direct_sha256 = _map_sha256(condition_map)
    from draftomen.profile_relationship_projection import (
        compile_hone_relationships,
        compile_token_replacement_relationships,
    )

    reviewed_ids = {row.finding_id for row in compilation.relationships}  # type: ignore[attr-defined]
    reviewed_ids.update(
        row.finding_id
        for row in compile_token_replacement_relationships(
            artifact=artifact,
            card_database=card_database,
        )
    )
    reviewed_ids.update(
        row.finding_id
        for row in compile_hone_relationships(
            artifact=artifact,
            card_database=card_database,
        )
    )
    runs: list[dict[str, object]] = []
    for index in (1, 2):
        work_dir = work_root / f"generation-{index}"
        work_dir.mkdir(parents=True, exist_ok=True)
        probe590.stage_generation(
            report,
            artifact=artifact,
            compilation=compilation,
            records=records,
            work_dir=work_dir,
        )
        runs.append(
            _generation_snapshot(
                report,
                index=index,
                work_dir=work_dir,
                direct_sha256=direct_sha256,
                reviewed_ids=reviewed_ids,
            )
        )
    report.data["generation_runs"] = runs
    exits_ok = all(
        run["exit_code"] == 0 and run["error"] is None and run["profile_error"] is None
        for run in runs
    )
    report.gate(
        "generation_runs_succeeded",
        ok=exits_ok,
        detail=_canonical(
            [
                {
                    "index": run["index"],
                    "exit_code": run["exit_code"],
                    "error": run["error"],
                    "profile_error": run["profile_error"],
                }
                for run in runs
            ]
        ),
        cause_class=probe590.GENERATION,
    )
    profile_hashes = [run["profile_sha256"] for run in runs]
    content_hashes = [run["content_sha256"] for run in runs]
    profiles_equal = (
        all(item is not None for item in profile_hashes)
        and len(set(profile_hashes)) == 1
        and all(item is not None for item in content_hashes)
        and len(set(content_hashes)) == 1
    )
    report.gate(
        "generation_profiles_identical",
        ok=profiles_equal,
        detail=(
            f"profile_sha256={_canonical(profile_hashes)} "
            f"content_sha256={_canonical(content_hashes)}"
        ),
        cause_class=probe590.GENERATION,
    )
    argv = [run["normalized_argv"] for run in runs]
    report.gate(
        "generation_inputs_identical",
        ok=argv[0] is not None and argv[0] == argv[1],
        detail=f"argv={_canonical(argv)}",
        cause_class=probe590.GENERATION,
    )
    maps_preserved = all(
        run["condition_map_matches"] and run["round_trip_condition_map_matches"] for run in runs
    )
    report.gate(
        "generation_condition_map_preserved",
        ok=maps_preserved,
        detail=(
            f"direct={direct_sha256} "
            f"published={_canonical([run['condition_map_sha256'] for run in runs])} "
            f"round_trip={_canonical([run['round_trip_condition_map_matches'] for run in runs])}"
        ),
        cause_class=probe590.COMPILER_GAP,
    )
    rows_preserved = all(
        run["reviewed_rows_match"] and run["round_trip_reviewed_rows_match"] for run in runs
    )
    report.gate(
        "generation_reviewed_rows_preserved",
        ok=rows_preserved,
        detail=(
            f"reviewed={_canonical([run['reviewed_rows'] for run in runs])} "
            f"expected={len(reviewed_ids)}"
        ),
        cause_class=probe590.GENERATION,
    )
    replacement_rows_valid = all(
        run["replacement_rows"] == len(EXPECTED_REPLACEMENT_SOURCES)
        and run["replacement_sources"] == list(EXPECTED_REPLACEMENT_SOURCES)
        and run["replacement_rows_valid"]
        for run in runs
    )
    report.gate(
        "generation_token_replacement_relationships",
        ok=replacement_rows_valid,
        detail=_canonical(
            [
                {
                    "rows": run["replacement_rows"],
                    "sources": run["replacement_sources"],
                    "valid": run["replacement_rows_valid"],
                }
                for run in runs
            ]
        ),
        cause_class=probe590.COMPILER_GAP,
    )
    report.gate(
        "generation_gift_capability",
        ok=all(run["gift_capability_valid"] for run in runs),
        detail=_canonical([run["gift_capability_valid"] for run in runs]),
        cause_class=probe590.COMPILER_GAP,
    )
    report.gate(
        "generation_hone_capabilities",
        ok=all(run["hone_capabilities_valid"] for run in runs),
        detail=_canonical([run["hone_capabilities_valid"] for run in runs]),
        cause_class=probe590.COMPILER_GAP,
    )
    report.gate(
        "generation_hone_relationship",
        ok=all(run["hone_relationship_valid"] for run in runs),
        detail=_canonical([run["hone_relationship_valid"] for run in runs]),
        cause_class=probe590.COMPILER_GAP,
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline landfall/ferocious/storied family recovery report for issue #591.",
    )
    parser.add_argument("--out", default="/tmp/hob-591-recovery.json", help="JSON report path.")
    args = parser.parse_args()

    started = time.monotonic()
    report = probe590.Report(probe="hob-591-recovery", scoped_mechanisms=SCOPED_MECHANISMS)
    report.data["expected_targets"] = {
        family: [list(face) for face in faces] for family, faces in EXPECTED_TARGET_FACES.items()
    }
    report.data["overrides"] = {
        "run_dir": str(RUN_DIR),
        "ratings_path": str(RATINGS_PATH),
        "note": (
            "HOB587_RUN_DIR and HOB590_RATINGS_FILE are the shared portable overrides; this "
            "probe never adds parallel environment names."
        ),
    }
    denial = trace_probe.deny_network()
    provider = probe590.guard_provider_entry_points(probe="hob-591-recovery")
    report.data["network"] = {
        "denial": denial,
        "blocked_attempts": denial["blocked_attempts"],
        "provider_entry_points": provider,
    }
    before = probe590.fingerprint_inputs()
    inputs_ok = probe590.gate_inputs(report, before)
    _gate_guide_digest(report, before)

    work_dir = Path(tempfile.mkdtemp(prefix="hob-591-recovery-"))
    report.data["work_dir"] = str(work_dir)
    if inputs_ok:
        try:
            ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            scoped, facts = reconstruct_scoped_rows(ledger)
            adventure_rows = reconstruct_adventure_rows(ledger)
            context = probe590.load_frozen_inputs()
            gate_scope(report, scoped=scoped, facts=facts, artifact=context.artifact)
            compilation, records = stage_compilation(
                report,
                artifact=context.artifact,
                card_database=context.card_database,
                scoped=scoped,
                adventure_rows=adventure_rows,
            )
            report.data["findings"] = records
            condition_map = stage_condition_map(
                report, artifact=context.artifact, card_database=context.card_database
            )
            gate_printed_family_cards(report, card_database=context.card_database)
            gate_targets(
                report,
                condition_map=condition_map,
                card_database=context.card_database,
                endpoint_cards=set(facts["endpoint_cards"]),  # type: ignore[arg-type]
            )
            gate_helper_cases(
                report,
                condition_map=condition_map,
                reviewed_ids={
                    row.finding_id for row in context.artifact.confirmed_relationships
                },
                saved_pairs=set(facts["saved_pairs"]),  # type: ignore[arg-type]
            )
            stage_generations(
                report,
                artifact=context.artifact,
                compilation=compilation,
                records=records,
                work_root=work_dir,
                condition_map=condition_map,
                card_database=context.card_database,
            )
        except BaseException as error:  # noqa: BLE001 - the probe reports, never crashes
            report.gate(
                "family_recovery_run",
                ok=False,
                detail=f"{type(error).__name__}: {error}",
                cause_class=probe590.PROBE_ERROR,
            )
            report.data["traceback_tail"] = traceback.format_exc().strip().splitlines()[-6:]

    after = probe590.fingerprint_inputs()
    probe590.gate_safety(report, before=before, after=after, network=report.data["network"])  # type: ignore[arg-type]
    report.finalize(duration_seconds=time.monotonic() - started)

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report.data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    scope = report.data.get("scope", {})
    print(f"hob-591 recovery report: {out_path}")
    print(
        "scope: "
        f"rows={scope.get('rows')} classes={_canonical(scope.get('classes', {}))} "
        f"families={_canonical(scope.get('families', {}))}"
    )  # type: ignore[union-attr]
    compilation_facts = report.data.get("compilation", {})
    row_groups = report.data.get("row_groups", {})
    print(
        "compile: "
        f"deterministic={compilation_facts.get('deterministic')} "  # type: ignore[union-attr]
        f"conversions={compilation_facts.get('accounted_rows')} "  # type: ignore[union-attr]
        f"qualified={row_groups.get('qualified_count')} "  # type: ignore[union-attr]
        f"decoded={len(row_groups.get('decoded_ids', []))} "  # type: ignore[union-attr]
        f"contradictions={row_groups.get('subtype_contradictions', {}).get('count')} "  # type: ignore[union-attr]
        f"record_defects={row_groups.get('record_defects', {}).get('count')}"  # type: ignore[union-attr]
    )
    adventure = report.data.get("adventure_family", {})
    print(
        "adventures: "
        f"rows={adventure.get('rows')} "  # type: ignore[union-attr]
        f"useful={adventure.get('useful_rows')} "  # type: ignore[union-attr]
        f"unresolved={len(adventure.get('unresolved_useful', {}))}"  # type: ignore[union-attr]
    )
    map_facts = report.data.get("condition_map", {})
    print(
        "condition map: "
        f"present={map_facts.get('present')} "  # type: ignore[union-attr]
        f"fingerprint={map_facts.get('fingerprint_sha256')}"  # type: ignore[union-attr]
    )
    runs = report.data.get("generation_runs", [])
    print(
        "generation: "
        f"runs={len(runs)} "  # type: ignore[arg-type]
        f"exits={[run.get('exit_code') for run in runs]}"  # type: ignore[union-attr]
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
        print(f"hob-591 recovery probe failed to write its report: {error}", file=sys.stderr)
        raise SystemExit(2) from error
