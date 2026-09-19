"""Verify cached HOB relationship advice through Draftmancer and QML.
The smoke does not exercise remote profile acquisition or deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from draftomen.profile_client import ProfileNetworkPolicy
from draftomen.qt_adapter import GuiPreferencesAdapter, SessionAdapter
from draftomen.qt_gui import DEFAULT_PROFILE_MANIFEST_URL, _fixed_font_family
from draftomen.session import ChangeAiEnhancedSuggestions, RequestBuild
from draftomen.test_draft import (
    DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE,
    DEFAULT_TEST_DRAFT_SERVER_URL,
    DEFAULT_TEST_DRAFT_TIMEOUT_SECONDS,
    TestDraftInspection,
    create_test_draft_runtime,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PICKS = 42


class HobPublishedProfileSmokeError(RuntimeError):
    """Report a failed cached-profile journey.
    The message is suitable for the command-line failure summary.
    """


def _profile_acquisition(*, app_dir: Path, profile_sha256: str) -> str:
    """Classify whether the cached profile came through the production manifest."""

    cache_path = app_dir / "set-profiles" / "v1" / "manifest.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        manifest = cache["manifest"]
        artifacts = manifest["artifacts"]
    except (FileNotFoundError, KeyError, TypeError, json.JSONDecodeError, OSError):
        return "preinstalled-offline"
    production_artifact = next(
        (
            artifact
            for artifact in artifacts
            if artifact.get("set_code") == "hob"
            and artifact.get("format") == "quickdraft"
        ),
        None,
    )
    if (
        cache.get("manifest_url") == DEFAULT_PROFILE_MANIFEST_URL
        and production_artifact is not None
        and production_artifact.get("profile_sha256") == profile_sha256
    ):
        return "production-refresh-cache"
    return "preinstalled-offline"


def build_parser() -> argparse.ArgumentParser:
    """Build the cached HOB profile smoke parser.
    Every external input stays explicit and local.
    """

    parser = argparse.ArgumentParser(
        description="Verify recovered HOB advice through real Draftmancer and QML"
    )
    parser.add_argument("--draftmancer-dir", type=Path, required=True)
    parser.add_argument(
        "--scryfall-bulk-file",
        type=Path,
        default=DEFAULT_TEST_DRAFT_SCRYFALL_BULK_FILE,
    )
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--server-url", default=DEFAULT_TEST_DRAFT_SERVER_URL)
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TEST_DRAFT_TIMEOUT_SECONDS
    )
    return parser


def _advice_row(inspection: TestDraftInspection):
    """Return the first relationship-bearing recommendation in one real offer.
    The offer order is retained so the evidence remains reproducible.
    """

    return next(
        (
            row
            for row in inspection.snapshot.recommendations.cards
            if row.relationship_contributions and row.relationship_advice
        ),
        None,
    )


def _offer_evidence(*, inspection: TestDraftInspection) -> dict[str, object]:
    """Project one server offer into bounded relationship-advice evidence."""

    advice_rows = []
    for row in inspection.snapshot.recommendations.cards:
        if not row.relationship_contributions:
            continue
        advice_rows.append(
            {
                "grp_id": row.card.grp_id,
                "name": row.card.name,
                "rank": row.rank,
                "relationships": [
                    {
                        "effective_contribution": contribution.effective_contribution,
                        "finding_id": contribution.support.finding_id,
                        "mechanism": contribution.support.mechanism,
                        "outcome": contribution.support.outcome.value,
                    }
                    for contribution in row.relationship_contributions
                ],
            }
        )
    return {
        "pack": inspection.offer.pack_number + 1,
        "pick": inspection.offer.pick_number + 1,
        "offered_grp_ids": list(inspection.offer.offered_grp_ids),
        "pool_grp_ids": list(inspection.offer.pool_grp_ids),
        "advice_rows": advice_rows,
        "no_advice_reason": (
            None
            if advice_rows
            else "no offered recommendation had relationship support from the drafted pool"
        ),
    }


def _render_advice(
    *,
    inspection: TestDraftInspection,
    grp_id: int,
    app_dir: Path,
    screenshot: Path,
) -> dict[str, object]:
    """Render one real recommendation through the production CardPreview QML.
    The label text is read back before the offscreen window is captured.
    """

    QQuickStyle.setStyle("Fusion")
    application = QGuiApplication.instance() or QGuiApplication([])
    recommendations = replace(
        inspection.snapshot.recommendations,
        selected_grp_id=grp_id,
    )
    snapshot = replace(inspection.snapshot, recommendations=recommendations)
    provider = SessionAdapter(snapshot=snapshot)
    preferences = GuiPreferencesAdapter(app_dir=app_dir, parent=application)
    engine = QQmlApplicationEngine()
    qml_directory = REPO_ROOT / "draftomen" / "qml"
    engine.addImportPath(str(qml_directory))
    context = engine.rootContext()
    context.setContextProperty("fixedFontFamily", _fixed_font_family())
    context.setContextProperty("sessionProvider", provider)
    context.setContextProperty("guiPreferences", preferences)
    context.setContextProperty("applicationTitle", "Draft Omen")
    context.setContextProperty("applicationVersion", "production-profile-smoke")
    context.setContextProperty("initialSurface", "live")
    context.setContextProperty("initialWindowWidth", 1540)
    context.setContextProperty("initialWindowHeight", 1020)
    engine.setInitialProperties({"provider": provider})
    engine.load(QUrl.fromLocalFile(str(qml_directory / "Main.qml")))
    if not engine.rootObjects():
        raise HobPublishedProfileSmokeError("the Draft Omen QML app did not load")
    host = engine.rootObjects()[0]
    application.processEvents()
    preview = host.findChild(QObject, "wideLiveCardPreview")
    explanation = (
        None
        if preview is None
        else preview.findChild(QObject, "cardPreviewExplanation")
    )
    heading = (
        None
        if preview is None
        else preview.findChild(QObject, "cardPreviewRelationshipHeading")
    )
    advice = (
        None
        if preview is None
        else preview.findChild(QObject, "cardPreviewRelationshipAdvice")
    )
    if explanation is None:
        raise HobPublishedProfileSmokeError("CardPreview explanation label is missing")
    if heading is None or advice is None:
        raise HobPublishedProfileSmokeError(
            "CardPreview relationship advice block is missing"
        )
    rendered_explanation = explanation.property("text")
    rendered_heading = heading.property("text")
    rendered_advice = advice.property("text")
    if not isinstance(rendered_explanation, str) or not rendered_explanation:
        raise HobPublishedProfileSmokeError("CardPreview explanation label is empty")
    if rendered_heading != "AI-ENHANCED RELATIONSHIP ADVICE":
        raise HobPublishedProfileSmokeError(
            "CardPreview relationship advice heading is incorrect"
        )
    if not isinstance(rendered_advice, str) or not rendered_advice:
        raise HobPublishedProfileSmokeError(
            "CardPreview relationship advice label is empty"
        )
    if not heading.isVisible() or not advice.isVisible():
        raise HobPublishedProfileSmokeError(
            "CardPreview relationship advice block is not visible"
        )
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    image = host.grabWindow()
    if image.isNull() or not image.save(str(screenshot)):
        raise HobPublishedProfileSmokeError(
            f"could not save QML screenshot to {screenshot}"
        )
    host.close()
    preferences.shutdown()
    del engine
    return {
        "advice": rendered_advice,
        "explanation": rendered_explanation,
        "heading": rendered_heading,
        "visible": True,
    }


def _run(args: argparse.Namespace) -> dict[str, object]:
    """Complete one rank-one HOB draft and prove advice on, off, and in QML.
    The profile client is forced offline and no model provider is constructed.
    """

    runtime = create_test_draft_runtime(
        draftmancer_dir=args.draftmancer_dir,
        scryfall_bulk_file=args.scryfall_bulk_file,
        server_url=args.server_url,
        set_code="HOB",
        timeout_seconds=args.timeout,
        source_app_dir=args.app_dir,
        profile_manifest_url=None,
        profile_network_policy=ProfileNetworkPolicy.OFFLINE,
        splash_enabled=True,
        contextual_adjustments_enabled=True,
        ai_enhanced_suggestions_enabled=True,
    )
    advice_inspection = None
    advice_grp_id = None
    advice_off_verified = False
    steps = []
    offers = []
    try:
        inspection = runtime.controller.start()
        while True:
            offer_evidence = _offer_evidence(inspection=inspection)
            advice = _advice_row(inspection)
            if advice is not None and advice_inspection is None:
                advice_inspection = inspection
                advice_grp_id = advice.card.grp_id
                runtime.session.dispatch(
                    command=ChangeAiEnhancedSuggestions(enabled=False)
                )
                disabled = next(
                    row
                    for row in runtime.session.snapshot.recommendations.cards
                    if row.card.grp_id == advice_grp_id
                )
                advice_off_verified = not disabled.relationship_contributions
                runtime.session.dispatch(
                    command=ChangeAiEnhancedSuggestions(enabled=True)
                )
                inspection = runtime.controller.inspect()
            top = inspection.snapshot.recommendations.cards[0]
            offer_evidence["chosen_grp_id"] = top.card.grp_id
            offers.append(offer_evidence)
            step = runtime.controller.confirm(
                grp_id=top.card.grp_id,
                expected_offer=inspection.offer,
            )
            steps.append(step)
            if step.after.draft is not None and step.after.draft.completed:
                break
            inspection = runtime.controller.inspect()
        build = runtime.session.dispatch(command=RequestBuild())
        if build.build is None:
            raise HobPublishedProfileSmokeError(
                "the completed draft did not produce a build"
            )
    finally:
        runtime.close()

    if len(steps) != EXPECTED_PICKS:
        raise HobPublishedProfileSmokeError(
            f"Draftmancer completed with {len(steps)} picks instead of {EXPECTED_PICKS}"
        )
    if advice_inspection is None or advice_grp_id is None:
        raise HobPublishedProfileSmokeError(
            "no relationship-bearing recommendation appeared in the complete draft"
        )
    if not advice_off_verified:
        raise HobPublishedProfileSmokeError(
            "the same offer retained relationship advice after the enhancement was disabled"
        )
    advice = next(
        row
        for row in advice_inspection.snapshot.recommendations.cards
        if row.card.grp_id == advice_grp_id
    )
    rendered = _render_advice(
        inspection=advice_inspection,
        grp_id=advice_grp_id,
        app_dir=args.app_dir,
        screenshot=args.screenshot,
    )
    if rendered["advice"] != advice.relationship_advice:
        raise HobPublishedProfileSmokeError(
            "CardPreview did not render relationship advice verbatim"
        )
    rejected_copy = (
        "confidence in that evidence",
        "mana producer gap",
        "produce the colors it needs",
    )
    rendered_text = f"{rendered['explanation']} {rendered['advice']}".casefold()
    unexpected_copy = next(
        (phrase for phrase in rejected_copy if phrase in rendered_text),
        None,
    )
    if unexpected_copy is not None:
        raise HobPublishedProfileSmokeError(
            f"CardPreview retained rejected copy: {unexpected_copy}"
        )
    profile_path = args.app_dir / "set-profiles" / "hob-quickdraft.json"
    profile_sha256 = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    profile_acquisition = _profile_acquisition(
        app_dir=args.app_dir,
        profile_sha256=profile_sha256,
    )
    report = {
        "schema_version": 1,
        "profile_acquisition": profile_acquisition,
        "profile_sha256": profile_sha256,
        "selection_policy": "rank-one",
        "offers": offers,
        "same_offer_ai_off": {
            "pack": advice_inspection.offer.pack_number + 1,
            "pick": advice_inspection.offer.pick_number + 1,
            "grp_id": advice_grp_id,
            "relationship_contributions_removed": advice_off_verified,
        },
        "qml": {
            "card": advice.card.name,
            "advice_matches": True,
            "heading": rendered["heading"],
            "rejected_copy_absent": True,
            "screenshot": str(args.screenshot),
            "visible": rendered["visible"],
        },
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n",
            encoding="utf-8",
        )
    return {
        "status": "ok",
        "set_code": "hob",
        "profile_acquisition": profile_acquisition,
        "selection_policy": "rank-one",
        "picks": len(steps),
        "packs": sorted({step.before.offer.pack_number + 1 for step in steps}),
        "advice_pack": advice_inspection.offer.pack_number + 1,
        "advice_pick": advice_inspection.offer.pick_number + 1,
        "advice_card": advice.card.name,
        "advice_off_verified": advice_off_verified,
        "qml_rendered": True,
        "report": None if args.report is None else str(args.report),
        "screenshot": str(args.screenshot),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the cached-profile smoke and print one canonical summary."""

    args = build_parser().parse_args(args=argv)
    try:
        result = _run(args)
    except Exception as error:  # noqa: BLE001 - keep one path-free smoke failure.
        print(f"HOB cached profile smoke failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
