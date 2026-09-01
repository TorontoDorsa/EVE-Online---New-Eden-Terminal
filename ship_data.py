#!/usr/bin/env python3
"""
Curated ship skill requirements + real hull stats
--------------------------------------------------
A small, hand-picked set of Mining and PVP ships with their real skill
requirements (resolved once from ESI's own dogma attributes —
requiredSkill1-3 / requiredSkill1-3Level, attribute IDs 182-184 / 277-279)
and, for Mining ships, real ore-hold capacity (generalMiningHoldCapacity,
attribute_id 1556) — all verified live against ESI, not guessed or
hardcoded from memory. See scratchpad `gen_ship_data.py`/`gen_ship_stats.py`
for the generators used to produce this file — these stats change rarely
enough that a static file avoids adding more live ESI calls to every
dashboard refresh.

Each value is `{"requires": [(skill_name, level), ...], "stats": {...}}`.
Every skill named in `requires` already appears in
skill_categories_data.SKILL_TYPE_IDS, so no new skill-name resolution is
needed to check a character's trained levels against these. PVP ships
carry no stats today (hardpoint-count personalization is computed
against the character's own CURRENT ship in dashboard.py, not a
ship-to-ship comparison here) — `"stats": {}` is kept for shape
consistency with `skill_plan.rank_ship_tips()`, which reads
`ship.get("stats", {})` either way.
"""

MINING_SHIPS = {
    "Venture": {"requires": [("Mining Frigate", 1)], "stats": {"ore_hold_capacity": 5000.0}},
    "Retriever": {"requires": [("Mining Barge", 1), ("Astrogeology", 3)], "stats": {"ore_hold_capacity": 27500.0}},
    "Covetor": {"requires": [("Mining Barge", 1), ("Astrogeology", 3)], "stats": {"ore_hold_capacity": 9000.0}},
    "Procurer": {"requires": [("Mining Barge", 1), ("Astrogeology", 3)], "stats": {"ore_hold_capacity": 16000.0}},
    "Hulk": {"requires": [("Exhumers", 1), ("Mining Barge", 5)], "stats": {"ore_hold_capacity": 11500.0}},
    "Mackinaw": {"requires": [("Exhumers", 1), ("Mining Barge", 5)], "stats": {"ore_hold_capacity": 31500.0}},
    "Skiff": {"requires": [("Exhumers", 1), ("Mining Barge", 5)], "stats": {"ore_hold_capacity": 18500.0}},
}

PVP_SHIPS = {
    "Rifter": {"requires": [("Minmatar Frigate", 1)], "stats": {}},
    "Merlin": {"requires": [("Caldari Frigate", 1)], "stats": {}},
    "Incursus": {"requires": [("Gallente Frigate", 1)], "stats": {}},
    "Punisher": {"requires": [("Amarr Frigate", 1)], "stats": {}},
    "Kestrel": {"requires": [("Caldari Frigate", 1)], "stats": {}},
    "Thrasher": {"requires": [("Minmatar Destroyer", 1)], "stats": {}},
}
