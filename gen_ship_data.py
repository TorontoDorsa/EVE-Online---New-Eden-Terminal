#!/usr/bin/env python3
"""
Generates ship_data_generated.py — every real, published, player-flyable
ship in the game (not just the 13 hand-picked ones in ship_data.py),
bucketed into Mining/PVP for skill_plan.rank_ship_tips().

Run this offline, review its printed audit, then commit the resulting
ship_data_generated.py. It is NOT imported at dashboard-load time itself —
only its static output is. See ship_data.py's own docstring for why this
project prefers "resolve once, commit a static file" over live per-request
ESI lookups on every dashboard refresh.

How ships are found: walk ESI's Ship category (confirmed live below, not
assumed) -> its groups -> each group's published types. How ships are
bucketed: NOT by ESI ship group name — Venture (an ORE mining frigate) and
Rifter (a PVP frigate) are BOTH filed under the plain "Frigate" ESI group,
so group name can't tell them apart. Instead this buckets by the real
skill(s) a ship requires (skill_plan.rank_ship_tips already ranks ships by
comparing trained skills against exactly this list) — a ship needing
"Mining Frigate"/"Mining Barge"/"Exhumers" is Mining; a ship needing a
racial hull-class skill ("Minmatar Frigate", "Amarr Cruiser", "Minmatar
Strategic Cruiser", etc.) is PVP. Anything matching neither (freighters,
haulers, industrials, shuttles, logistics, command ships, force
auxiliaries, capitals) is excluded from this phase — no tab exists to
surface them on yet.

Required-skill dogma attributes: verified live (2026-09-02) that ships can
need up to 6 required skills, not just the 3 the existing
_module_required_skills() helper in dashboard.py checks for fitted
modules — e.g. the Loki (Tech III Strategic Cruiser) needs 5:
Minmatar Strategic Cruiser (hull) + 4 subsystem skills. Attribute ID pairs,
each confirmed via GET /dogma/attributes/{id}/ before trusting them:
  requiredSkill1/Level1 = 182/277   requiredSkill4/Level4 = 1285/1286
  requiredSkill2/Level2 = 183/278   requiredSkill5/Level5 = 1289/1287
  requiredSkill3/Level3 = 184/279   requiredSkill6/Level6 = 1290/1288
(Note requiredSkill5's id/level pair is NOT in numeric order — 1289 is the
skill id, 1287 is its level — confirmed by checking a real Loki response.)
"""

import json
import random
import time

import requests

from skill_categories_data import SKILL_TYPE_IDS

ESI = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"

REQUIRED_SKILL_ATTR_PAIRS = [
    (182, 277), (183, 278), (184, 279),
    (1285, 1286), (1289, 1287), (1290, 1288),
]

_HULL_STAT_ATTRS = {
    "shield_capacity": 263, "armor_hp": 265, "shield_recharge_ms": 479,
    "ore_hold_capacity": 1556, "turret_hardpoints": 102, "launcher_hardpoints": 101,
}

_RACES = ["Amarr", "Caldari", "Gallente", "Minmatar"]
_PVP_HULL_WORDS = [
    "Frigate", "Destroyer", "Cruiser", "Battlecruiser", "Battleship",
    "Strategic Cruiser", "Tactical Destroyer",
]
PVP_SKILL_NAMES = {f"{race} {hull}" for race in _RACES for hull in _PVP_HULL_WORDS} | {
    # T2/T3 subclass skills, as a safety net in case a ship's `requires`
    # ever lists one without also listing its underlying racial hull
    # skill (not expected, but cheap to guard against).
    "Assault Frigates", "Interceptors", "Covert Ops", "Electronic Attack Ships",
    "Heavy Assault Cruisers", "Heavy Interdiction Cruisers", "Recon Ships",
    "Interdictors",
}
# Verified live (2026-09-02): "Mining Destroyer" is a real skill (Pioneer,
# Perseverance, Outrider) — not a guess, confirmed via a first generator
# run whose audit showed these ships excluded for lacking a bucket match.
MINING_SKILL_NAMES = {"Mining Frigate", "Mining Barge", "Exhumers", "Mining Destroyer"}


def _get(path, **params):
    """GET against ESI with retry/backoff on 420/429/5xx — the shared
    eve_esi_terminal.get() has none of this and isn't touched here; this
    script alone needs it, since it issues far more calls than any
    existing runtime call site."""
    params.setdefault("datasource", DATASOURCE)
    delay = 1.0
    for attempt in range(6):
        r = requests.get(f"{ESI}{path}", params=params, timeout=20)
        if r.status_code in (420, 429) or r.status_code >= 500:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            time.sleep(wait + random.uniform(0, 0.5))
            delay = min(delay * 2, 30)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


_type_name_cache = {}


def _resolve_type_name(type_id):
    if type_id not in _type_name_cache:
        _type_name_cache[type_id] = _get(f"/universe/types/{type_id}/")["name"]
    return _type_name_cache[type_id]


def _ship_requires_and_stats(type_data):
    by_attr = {a["attribute_id"]: a["value"] for a in type_data.get("dogma_attributes", [])}
    requires = []
    for skill_attr, level_attr in REQUIRED_SKILL_ATTR_PAIRS:
        skill_type_id = by_attr.get(skill_attr)
        if not skill_type_id:
            continue
        level = int(by_attr.get(level_attr, 1))
        requires.append((_resolve_type_name(int(skill_type_id)), level))
    stats = {key: by_attr[attr_id] for key, attr_id in _HULL_STAT_ATTRS.items() if attr_id in by_attr}
    return requires, stats


def _bucket_for(requires):
    skill_names = {name for name, _level in requires}
    if skill_names & MINING_SKILL_NAMES:
        return "Mining"
    if skill_names & PVP_SKILL_NAMES:
        return "PVP"
    return None


def main():
    print("Confirming Ship category (id 6) live against ESI...")
    category = _get("/universe/categories/6/")
    assert category["name"] == "Ship", f"category 6 is {category['name']!r}, not Ship — aborting"
    assert category["published"], "Ship category is unpublished — aborting"
    print(f"  OK — {len(category['groups'])} groups under category 6 'Ship'.")

    mining_ships = {}
    pvp_ships = {}
    excluded = []       # (name, requires_skill_names) — no Mining/PVP match
    skipped_no_requires = []
    skipped_unresolved_skill = []

    for group_id in category["groups"]:
        group = _get(f"/universe/groups/{group_id}/")
        if not group["published"]:
            continue
        for type_id in group["types"]:
            type_data = _get(f"/universe/types/{type_id}/")
            if not type_data.get("published"):
                continue
            name = type_data["name"]
            requires, stats = _ship_requires_and_stats(type_data)
            if not requires:
                skipped_no_requires.append(name)
                continue
            unresolved = [n for n, _lvl in requires if n not in SKILL_TYPE_IDS]
            if unresolved:
                skipped_unresolved_skill.append((name, unresolved))
                continue
            bucket = _bucket_for(requires)
            entry = {"requires": requires, "stats": stats}
            if bucket == "Mining":
                mining_ships[name] = entry
            elif bucket == "PVP":
                pvp_ships[name] = entry
            else:
                excluded.append((name, [n for n, _lvl in requires]))

    print(f"\nMining ships found: {len(mining_ships)}")
    for name in sorted(mining_ships):
        print(f"  {name}: {mining_ships[name]['requires']}")
    print(f"\nPVP ships found: {len(pvp_ships)}")
    for name in sorted(pvp_ships):
        print(f"  {name}: {pvp_ships[name]['requires']}")

    print(f"\nExcluded (no Mining/PVP skill match — {len(excluded)}):")
    for name, skills in sorted(excluded):
        print(f"  {name}: requires {skills}")

    if skipped_no_requires:
        print(f"\nSkipped, no required skills at all ({len(skipped_no_requires)}): {sorted(skipped_no_requires)}")
    if skipped_unresolved_skill:
        print(f"\nSkipped, required skill name doesn't resolve in SKILL_TYPE_IDS ({len(skipped_unresolved_skill)}):")
        for name, unresolved in skipped_unresolved_skill:
            print(f"  {name}: {unresolved}")

    out_path = "ship_data_generated.py"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(
            '"""Generated by gen_ship_data.py — do not hand-edit.\n\n'
            "Every real, published ship in the game whose required skill(s) match\n"
            "a known Mining or PVP ship-license skill name (see gen_ship_data.py's\n"
            "own docstring for why skill name, not ESI ship group, is the bucketing\n"
            "signal). Consumed by dashboard.py, merged with the hand-curated 13\n"
            'ships in ship_data.py (which take precedence on name collision — they\n'
            'additionally carry wiki-verified `skill_bonuses`, which this file does\n'
            'not attempt to derive). Regenerate by re-running gen_ship_data.py.\n"""\n\n'
        )
        f.write("GENERATED_MINING_SHIPS = ")
        f.write(json.dumps(mining_ships, indent=4))
        f.write("\n\nGENERATED_PVP_SHIPS = ")
        f.write(json.dumps(pvp_ships, indent=4))
        f.write("\n")

    print(f"\nWrote {out_path} — {len(mining_ships)} Mining ships, {len(pvp_ships)} PVP ships.")


if __name__ == "__main__":
    main()
