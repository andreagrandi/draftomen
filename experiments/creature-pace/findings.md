# Creature pace term backtest (#1003)

Run on 2026-10-28 with `run.py` against 17Lands PremierDraft public dumps,
using the first 800 seven-win drafts of a 2,000-draft awk sample per set.
A 100-draft FRA run took 131 seconds, so 2,000 drafts would have taken about
44 minutes, over the 20-minute limit per run.

Top-1 agreement scores each recorded pack with the drafter's real pool and
checks whether the engine's top card is the drafter's pick. The floor share
lets the engine draft its own pool from the recorded packs and counts pools
that end with 15 or more creatures castable in the engine's inferred pair.

| Set | Term | Drafts | Picks  | Top-1  | Engine pools with 15+ | Seconds |
| --- | ---- | ------ | ------ | ------ | --------------------- | ------- |
| FRA | off  | 800    | 33,600 | 46.51% | 37.13%                | 948     |
| FRA | on   | 800    | 33,600 | 46.50% | 37.50%                | 952     |
| HOB | off  | 800    | 33,599 | 50.08% | 91.87%                | 604     |
| HOB | on   | 800    | 33,599 | 50.07% | 92.13%                | 610     |

Drafters' own pools reach 15 castable creatures in 86.62% of FRA drafts and
94.63% of HOB drafts.

The floor share rose by 0.37 points on FRA and 0.26 points on HOB. Top-1
agreement changed by one pick in 33,600 in each set, which is noise, so
`creature_pace_enabled` defaults to on.

At these settings, with a 2.0-point cap and full strength at 4 creatures short,
the term is too weak to fix FRA, where the engine's own pools reach the floor
in only 37% of drafts.
