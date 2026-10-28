#!/usr/bin/env python3
"""Backtest the creature pace pick term on a sampled 17Lands draft dump.
Reports top-1 agreement and the share of engine-drafted pools with enough creatures.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from draftomen.benchmark import (  # noqa: E402
    _card_name_index,
    _database_with_rating_metadata,
    _draft_row_sort_key,
    _public_pack_number,
    _public_pick_index,
    _public_pick_number,
    _resolve_card_name,
    _resolved_actual_pick_grp_ids,
    _resolved_offered_grp_ids,
    _rows_by_draft,
    _clean_text,
)
from draftomen.card_data_client import CardDataClient  # noqa: E402
from draftomen.carddb import CardDatabase  # noqa: E402
from draftomen.config import DECK_BUILDER, PICK_ENGINE  # noqa: E402
from draftomen.events import EXPECTED_TOTAL_PICKS  # noqa: E402
from draftomen.pickengine import PickEngine, ScoredPack  # noqa: E402
from draftomen.ranking import rank_scored_cards  # noqa: E402
from draftomen.seventeen import (  # noqa: E402
    iter_17lands_draft_data_rows,
    load_cached_17lands_data,
)
from draftomen.set_profile import load_scoring_profile  # noqa: E402
from draftomen.splash import card_is_castable_in_pair  # noqa: E402


def main() -> int:
    """Run one backtest with the creature pace term on or off.
    Prints one JSON line with the metrics and the run time.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-code", required=True)
    parser.add_argument("--format", default="PremierDraft")
    parser.add_argument("--draft-data-file", type=Path, required=True)
    parser.add_argument("--pace", choices=("on", "off"), required=True)
    parser.add_argument("--max-drafts", type=int, default=None)
    args = parser.parse_args()
    if not args.draft_data_file.is_file():
        parser.error(f"draft data file not found: {args.draft_data_file}")

    started = time.monotonic()
    ratings_data = load_cached_17lands_data(
        set_code=args.set_code,
        event_format=args.format,
    )
    if not ratings_data.ratings:
        parser.error("no cached 17Lands ratings for this set and format")
    card_database = CardDataClient(app_dir=None).load(
        args.set_code,
        allow_network=True,
    )
    database = _database_with_rating_metadata(
        card_database=card_database,
        ratings_data=ratings_data,
    )
    name_index = _card_name_index(
        card_database=database,
        ratings_data=ratings_data,
    )
    engine = PickEngine(
        ratings_data=ratings_data,
        set_profile=load_scoring_profile(
            set_code=args.set_code,
            event_format=args.format,
            app_dir=None,
        ),
        config=replace(PICK_ENGINE, creature_pace_enabled=args.pace == "on"),
    )
    rows_by_draft = _rows_by_draft(
        rows=iter_17lands_draft_data_rows(path=args.draft_data_file),
        set_code=args.set_code,
        event_format=args.format,
        max_drafts=args.max_drafts,
        trophy_only=True,
    )

    scored_picks = 0
    top1_matches = 0
    engine_pools_on_floor = 0
    human_pools_on_floor = 0
    unknown_type_cards = 0
    for draft_rows in rows_by_draft.values():
        human_pool: list[int] = []
        engine_pool: list[int] = []
        engine_pair: str | None = None
        human_pair: str | None = None
        for row in sorted(draft_rows, key=_draft_row_sort_key):
            offered = _resolved_offered_grp_ids(row=row, name_index=name_index)
            actual_name = _clean_text(row.get("pick"))
            actual = (
                None
                if actual_name is None
                else _resolve_card_name(name=actual_name, name_index=name_index)
            )
            if offered:
                human_pack = _score_row(
                    engine=engine,
                    row=row,
                    offered=offered,
                    pool=tuple(human_pool),
                    database=database,
                )
                human_pair = human_pack.commitment.inferred_pair
                if actual is not None and actual in offered:
                    scored_picks += 1
                    top_card = rank_scored_cards(
                        cards=human_pack.cards,
                        ranking_mode="score",
                    )[0]
                    top1_matches += int(top_card.card.grp_id == actual)
                engine_pack = _score_row(
                    engine=engine,
                    row=row,
                    offered=offered,
                    pool=tuple(engine_pool),
                    database=database,
                )
                engine_pair = engine_pack.commitment.inferred_pair
                engine_pool.append(
                    rank_scored_cards(
                        cards=engine_pack.cards,
                        ranking_mode="score",
                    )[0].card.grp_id
                )
            human_pool.extend(
                _resolved_actual_pick_grp_ids(row=row, name_index=name_index)
            )
        engine_pools_on_floor += int(
            _castable_creatures(pool=engine_pool, pair=engine_pair, database=database)
            >= DECK_BUILDER.creature_floor
        )
        human_pools_on_floor += int(
            _castable_creatures(pool=human_pool, pair=human_pair, database=database)
            >= DECK_BUILDER.creature_floor
        )
        unknown_type_cards += sum(
            1 for grp_id in human_pool if database.lookup(grp_id=grp_id).unknown
        )

    drafts = len(rows_by_draft)
    print(
        json.dumps(
            {
                "set_code": args.set_code,
                "pace": args.pace,
                "drafts": drafts,
                "scored_picks": scored_picks,
                "top1_agreement": round(top1_matches / max(1, scored_picks), 4),
                "engine_pool_floor_share": round(
                    engine_pools_on_floor / max(1, drafts), 4
                ),
                "human_pool_floor_share": round(
                    human_pools_on_floor / max(1, drafts), 4
                ),
                "unknown_type_cards": unknown_type_cards,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
    )
    return 0


def _score_row(
    *,
    engine: PickEngine,
    row: dict[str, str],
    offered: tuple[int, ...],
    pool: tuple[int, ...],
    database: CardDatabase,
) -> ScoredPack:
    pack_number = _public_pack_number(row=row)
    pick_number = _public_pick_number(row=row)
    pick_index = _public_pick_index(pack_number=pack_number, pick_number=pick_number)
    return engine.score_pack(
        offered_grp_ids=offered,
        card_database=database,
        pool_grp_ids=pool,
        pick_index=pick_index,
        pack_number=pack_number,
        pick_number=pick_number - 1,
        global_pick_index=pick_index,
        estimated_remaining_picks=max(0, EXPECTED_TOTAL_PICKS - pick_index),
    )


def _castable_creatures(
    *,
    pool: list[int],
    pair: str | None,
    database: CardDatabase,
) -> int:
    # Same rule as the session layer: non-land creatures the pair can cast.
    count = 0
    for grp_id in pool:
        card = database.lookup(grp_id=grp_id)
        if any("Land" in type_line for type_line in card.types):
            continue
        if pair is not None and not card_is_castable_in_pair(card=card, base_pair=pair):
            continue
        count += int(any("Creature" in type_line for type_line in card.types))
    return count


if __name__ == "__main__":
    raise SystemExit(main())
