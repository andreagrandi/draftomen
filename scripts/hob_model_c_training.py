"""Run the offline HOB coarse-context model training pilot."""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

from draftomen.augmented_training import (
    AugmentedTrainingError,
    train_and_gate_augmented_set,
)
from draftomen.augmented_training_data import AugmentedTrainingSource
from draftomen.carddb import CardDatabase, CardInfo
from draftomen.set_profile import SetProfile


def load_hob_card_database(*, path: Path) -> CardDatabase:
    """Load the checked-in HOB website card data for offline scoring.
    Website rows use arena_id as the Draft Omen grp_id.
    """

    with gzip.open(path, mode="rt", encoding="utf-8") as handle:
        value = json.load(handle)
    cards_value = value.get("cards") if isinstance(value, dict) else None
    if not isinstance(cards_value, list):
        raise AugmentedTrainingError("HOB card data has no cards array.")
    cards: dict[int, CardInfo] = {}
    for item in cards_value:
        if not isinstance(item, dict):
            raise AugmentedTrainingError("HOB card data contains an invalid card.")
        normalized = dict(item)
        normalized["grp_id"] = item.get("arena_id")
        normalized["source_provenance"] = ["website-public-card-data"]
        card = CardInfo.from_json(data=normalized)
        cards[card.grp_id] = card
    return CardDatabase(cards=cards)


def load_hob_set_profile(*, path: Path) -> SetProfile:
    """Load the checked-in HOB profile used by current Basic DO scoring.
    The profile format must match the public draft event.
    """

    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, mode="rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AugmentedTrainingError("HOB set profile must be a JSON object.")
    return SetProfile.from_json(value)


def _json_bytes(*, value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _arguments() -> argparse.Namespace:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Train and gate the HOB coarse-context Model C pilot.",
    )
    parser.add_argument("--draft-data", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--retrieved-at", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--card-data",
        type=Path,
        default=repository / "website/public/card-data/hob.json.gz",
    )
    parser.add_argument(
        "--set-profile",
        type=Path,
        default=(
            repository
            / "website/public/profiles/objects"
            / "51603f8922d594fca71bddd6d5fae02e301381180b169b155e402434c466fd23.json.gz"
        ),
    )
    arguments = parser.parse_args()
    if arguments.artifact.resolve() == arguments.report.resolve():
        parser.error("--artifact and --report must resolve to different paths.")
    return arguments


def main() -> int:
    """Run the complete offline HOB training and promotion workflow."""

    arguments = _arguments()
    card_database = load_hob_card_database(path=arguments.card_data)
    set_profile = load_hob_set_profile(path=arguments.set_profile)
    source = AugmentedTrainingSource(
        path=arguments.draft_data,
        url=arguments.source_url,
        sha256=arguments.source_sha256,
        retrieved_at=arguments.retrieved_at,
        attribution="17Lands public datasets",
        license="CC BY 4.0",
        event_type="PremierDraft",
    )
    result = train_and_gate_augmented_set(
        set_code="HOB",
        source=source,
        card_database=card_database,
        set_profile=set_profile,
    )
    artifact = result.artifact
    promoted = artifact is not None
    arguments.artifact.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    if artifact is not None:
        arguments.artifact.write_bytes(artifact.to_bytes())
    elif arguments.artifact.exists():
        arguments.artifact.unlink()
    arguments.report.write_bytes(_json_bytes(value=result.report))
    print(json.dumps(result.report, indent=2, sort_keys=True))
    return 0 if promoted else 1


if __name__ == "__main__":
    raise SystemExit(main())
