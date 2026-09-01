#!/usr/bin/env python3
"""
Curated ship skill requirements + real hull stats + real skill-scaling
bonuses
--------------------------------------------------------------------------
A small, hand-picked set of Mining and PVP ships with their real skill
requirements (resolved once from ESI's own dogma attributes —
requiredSkill1-3 / requiredSkill1-3Level, attribute IDs 182-184 / 277-279)
and, for Mining ships, real ore-hold capacity (generalMiningHoldCapacity,
attribute_id 1556) — all verified live against ESI, not guessed or
hardcoded from memory. See scratchpad `gen_ship_data.py`/`gen_ship_stats.py`
for the generators used to produce this file — these stats change rarely
enough that a static file avoids adding more live ESI calls to every
dashboard refresh.

`skill_bonuses` holds each ship's REAL per-skill-level hull bonuses —
e.g. Retriever's real 3%/level Strip Miner yield and 5%/level ore-hold
capacity bonus from training Mining Barge. These are NOT discoverable
from the governing skill's own ESI description text (e.g. Mining Barge's
description is just "Skill at operating ORE Mining Barges.", no number
at all) — they live on the ship HULL's dogma_effects instead, and ESI
exposes no structured field linking an effect to "this is governed by
skill X." Verified live (dogma_effects -> /dogma/effects/{id}/'s
modifiers -> the hull's own dogma_attributes value for
modifying_attribute_id) AND cross-checked line-for-line against EVE
University's real per-ship wiki pages (wiki.eveuniversity.org/<Ship>) for
all 13 ships below — this second check matters because neither the
effect's func type nor its name reliably distinguishes a real per-level
skill bonus from a flat, non-scaling "Role Bonus" on its own: e.g.
Retriever's real 5%/level ore-hold bonus uses func=ItemModifier (not any
*RequiredSkillModifier variant), while Thrasher's flat 50% Role Bonus to
turret optimal range has no "shipRoleBonus" prefix to flag it, and
initially looked like a real per-level Minmatar Destroyer bonus until
checked against the wiki. So: manually curated and per-ship wiki-verified,
not an automatic name/func-based algorithm — re-verify against both ESI
and EVE University before trusting a change to this table.

Each `skill_bonuses[skill_name]` is a list of `{"label", "pct_per_level"}`
dicts — every real bonus line that skill grants on this specific hull,
in the order EVE University's own wiki lists them (so the first entry is
the ship's headline bonus for that skill — ore/damage yield where one
exists). A negative `pct_per_level` means the stat goes DOWN per level
(e.g. faster/shorter duration, lower activation cost) — the sign is real,
not a display artifact.

Each ship value is `{"requires": [(skill_name, level), ...], "stats": {...},
"skill_bonuses": {...}}`. Every skill named in `requires`/`skill_bonuses`
already appears in skill_categories_data.SKILL_TYPE_IDS, so no new
skill-name resolution is needed to check a character's trained levels
against these. PVP ships carry no `stats` today (hardpoint-count
personalization is computed against the character's own CURRENT ship in
dashboard.py, not a ship-to-ship comparison here) — `"stats": {}` is kept
for shape consistency with `skill_plan.rank_ship_tips()`, which reads
`ship.get("stats", {})` either way.
"""

MINING_SHIPS = {
    "Venture": {
        "requires": [("Mining Frigate", 1)],
        "stats": {"ore_hold_capacity": 5000.0},
        "skill_bonuses": {
            "Mining Frigate": [
                {"label": "Ore Mining yield", "pct_per_level": 5.0},
                {"label": "Gas Cloud Harvesting duration", "pct_per_level": -5.0},
            ],
        },
    },
    "Retriever": {
        "requires": [("Mining Barge", 1), ("Astrogeology", 3)],
        "stats": {"ore_hold_capacity": 27500.0},
        "skill_bonuses": {
            "Mining Barge": [
                {"label": "Strip Miner yield", "pct_per_level": 3.0},
                {"label": "ore hold capacity", "pct_per_level": 5.0},
                {"label": "Ice Harvester duration", "pct_per_level": -2.0},
                {"label": "Gas Harvester duration", "pct_per_level": -2.0},
            ],
        },
    },
    "Covetor": {
        "requires": [("Mining Barge", 1), ("Astrogeology", 3)],
        "stats": {"ore_hold_capacity": 9000.0},
        "skill_bonuses": {
            "Mining Barge": [
                {"label": "Strip Miner yield", "pct_per_level": 3.0},
                {"label": "Strip Miner range", "pct_per_level": 6.0},
                {"label": "Ice Harvester range", "pct_per_level": 6.0},
                {"label": "Ice Harvester duration", "pct_per_level": -3.0},
                {"label": "Gas Harvester duration", "pct_per_level": -3.0},
            ],
        },
    },
    "Procurer": {
        "requires": [("Mining Barge", 1), ("Astrogeology", 3)],
        "stats": {"ore_hold_capacity": 16000.0},
        "skill_bonuses": {
            "Mining Barge": [
                {"label": "Strip Miner yield", "pct_per_level": 2.0},
                {"label": "ship shield hitpoints", "pct_per_level": 6.0},
                {"label": "Ice Harvester duration", "pct_per_level": -2.0},
                {"label": "Gas Harvester duration", "pct_per_level": -2.0},
            ],
        },
    },
    "Hulk": {
        "requires": [("Exhumers", 1), ("Mining Barge", 5)],
        "stats": {"ore_hold_capacity": 11500.0},
        "skill_bonuses": {
            "Mining Barge": [
                {"label": "Strip Miner yield", "pct_per_level": 3.0},
                {"label": "Strip Miner range", "pct_per_level": 6.0},
                {"label": "Ice Harvester range", "pct_per_level": 6.0},
                {"label": "Ice Harvester duration", "pct_per_level": -3.0},
                {"label": "Gas Harvester duration", "pct_per_level": -3.0},
            ],
            "Exhumers": [
                {"label": "Strip Miner yield", "pct_per_level": 6.0},
                {"label": "Strip Miner duration", "pct_per_level": -3.0},
                {"label": "Gas Harvester duration", "pct_per_level": -3.0},
                {"label": "Ice Harvester duration", "pct_per_level": -4.0},
                {"label": "all shield resistances", "pct_per_level": -4.0},
            ],
        },
    },
    "Mackinaw": {
        "requires": [("Exhumers", 1), ("Mining Barge", 5)],
        "stats": {"ore_hold_capacity": 31500.0},
        "skill_bonuses": {
            "Mining Barge": [
                {"label": "Strip Miner yield", "pct_per_level": 3.0},
                {"label": "ore hold capacity", "pct_per_level": 5.0},
                {"label": "Ice Harvester duration", "pct_per_level": -4.0},
                {"label": "Gas Harvester duration", "pct_per_level": -3.0},
            ],
            "Exhumers": [
                {"label": "Strip Miner yield", "pct_per_level": 4.0},
                {"label": "ore hold capacity", "pct_per_level": 2.5},
                {"label": "all shield resistances", "pct_per_level": -4.0},
                {"label": "Gas Harvester duration", "pct_per_level": -3.0},
            ],
        },
    },
    "Skiff": {
        "requires": [("Exhumers", 1), ("Mining Barge", 5)],
        "stats": {"ore_hold_capacity": 18500.0},
        "skill_bonuses": {
            "Mining Barge": [
                {"label": "Strip Miner yield", "pct_per_level": 2.0},
                {"label": "ship shield hitpoints", "pct_per_level": 6.0},
                {"label": "Ice Harvester duration", "pct_per_level": -4.0},
            ],
            "Exhumers": [
                {"label": "Strip Miner yield", "pct_per_level": 2.0},
                {"label": "all shield resistances", "pct_per_level": -4.0},
                {"label": "Gas Harvester duration", "pct_per_level": -3.0},
            ],
        },
    },
}

PVP_SHIPS = {
    "Rifter": {
        "requires": [("Minmatar Frigate", 1)],
        "stats": {},
        "skill_bonuses": {
            "Minmatar Frigate": [
                {"label": "Small Projectile Turret falloff", "pct_per_level": 10.0},
                {"label": "Small Projectile Turret rate of fire", "pct_per_level": -7.5},
            ],
        },
    },
    "Merlin": {
        "requires": [("Caldari Frigate", 1)],
        "stats": {},
        "skill_bonuses": {
            "Caldari Frigate": [
                {"label": "Small Hybrid Turret damage", "pct_per_level": 5.0},
                {"label": "all shield resistances", "pct_per_level": -4.0},
            ],
        },
    },
    "Incursus": {
        "requires": [("Gallente Frigate", 1)],
        "stats": {},
        "skill_bonuses": {
            "Gallente Frigate": [
                {"label": "Small Hybrid Turret damage", "pct_per_level": 5.0},
                {"label": "Armor Repairer amount", "pct_per_level": 7.5},
            ],
        },
    },
    "Punisher": {
        "requires": [("Amarr Frigate", 1)],
        "stats": {},
        "skill_bonuses": {
            "Amarr Frigate": [
                {"label": "Small Energy Turret activation cost", "pct_per_level": -10.0},
                {"label": "all armor resistances", "pct_per_level": -4.0},
            ],
        },
    },
    "Kestrel": {
        "requires": [("Caldari Frigate", 1)],
        "stats": {},
        "skill_bonuses": {
            "Caldari Frigate": [
                {"label": "Light Missile/Rocket damage", "pct_per_level": 5.0},
                {"label": "Light Missile/Rocket max velocity", "pct_per_level": 10.0},
            ],
        },
    },
    "Thrasher": {
        "requires": [("Minmatar Destroyer", 1)],
        "stats": {},
        "skill_bonuses": {
            "Minmatar Destroyer": [
                {"label": "Small Projectile Turret damage", "pct_per_level": 5.0},
                {"label": "Small Projectile Turret tracking speed", "pct_per_level": 10.0},
            ],
        },
    },
}
