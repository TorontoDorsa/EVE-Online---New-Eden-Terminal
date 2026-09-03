#!/usr/bin/env python3
"""
Regenerates skill_categories_data.py and skill_groups_data.py — the full
list of real, player-trainable EVE skills this project's tips are built
from — against current live ESI data.

Run this offline, review the printed diff, then commit both output files
together (they're derived from the same live snapshot and must agree —
skill_groups_data.py's own header asserts its skill set exactly matches
skill_categories_data.SKILL_TYPE_IDS). Neither file is imported at
dashboard-load time by this script itself; only their static output is,
same "resolve once, commit a static file" convention as ship_data.py and
gen_ship_data.py.

**Preserves hand-curation.** Unlike gen_ship_data.py (which produces a
wholly-generated file), CORE_TARGETS here holds real curation history
accumulated across many sessions (Magic 14 promotions, real Social/Trade
group audits, mining-crystal skill sweeps — see
project_personalization_roadmap memory for the full history). This script
does NOT regenerate CORE_TARGETS from scratch. It only removes names that
are no longer real (see DEAD_SKILLS below) and leaves everything else
untouched — never add a name to CORE_TARGETS here; that's a curation
decision for a human/session to make deliberately, same as it always has
been.

**Why a skill can be missing or wrong even though "the file was already
regenerated once" (2026-09-02):** EVE's skill list isn't static. CCP both
adds new skills (this run is what caught the Triglavian/EDENCOM ship-
license skills and the real Electronic Systems/Planet Management skills
that a much earlier snapshot never had) and retires old ones — retiring
doesn't delete the type_id, it flips ESI's `published` flag to False,
which is easy to miss if you only check "does this name still resolve"
rather than "is it still published." A currently-live "DED Connections"
CORE_TARGETS entry, discovered dead the day after it was added, is exactly
this failure mode — this script's own DEAD_SKILLS-detection step is what
would have caught it immediately instead of a day later.

How it works:
1. Confirm the Skill category id live via GET /universe/categories/16/
   (verify name=="Skill", don't assume from memory).
2. Walk its (real, live-fetched) groups -> published types.
3. Diff against the CURRENT skill_categories_data.SKILL_TYPE_IDS:
   - Names present now but whose type_id ESI no longer reports as
     published -> DEAD, removed everywhere (SKILL_TYPE_IDS,
     FULL_SKILL_NAMES, CORE_TARGETS, skill_groups_data.SKILL_GROUPS).
   - Real published skills not yet tracked -> reported for review. This
     script does NOT auto-add them to FULL_SKILL_NAMES/SKILL_GROUPS on its
     own — categorizing a new skill (which of the 6 playstyle categories
     it belongs to, if any) is exactly the kind of judgment call this
     project has always made by checking real ESI descriptions rather
     than guessing (see the 2026-09-02 session: Electronic Systems ->
     PVP, Planet Management -> Industry, Sequencing excluded entirely
     after reading its real descriptions — governs manufacturing
     cosmetic SKINs, not a playstyle). Add a NEW_SKILLS_BY_CATEGORY entry
     below (mirroring the ones already there) once you've made that call
     for each newly-reported group, then re-run.
4. Writes both output files, preserving each one's existing formatting
   convention (SKILL_TYPE_IDS one entry per line; FULL_SKILL_NAMES one
   name per line per category; CORE_TARGETS single line per category;
   SKILL_GROUPS one line per group) so future diffs stay small and
   reviewable, not full-file rewrites.
"""

import sys
import time
import random

import requests

sys.path.insert(0, r"C:\Users\matpr\Downloads\Claude Project")
from skill_categories_data import SKILL_TYPE_IDS, FULL_SKILL_NAMES, CORE_TARGETS
from skill_groups_data import SKILL_GROUPS

ESI = "https://esi.evetech.net/latest"


def _get(path, **params):
    params.setdefault("datasource", "tranquility")
    delay = 1.0
    for _attempt in range(6):
        r = requests.get(f"{ESI}{path}", params=params, timeout=20)
        if r.status_code in (420, 429) or r.status_code >= 500:
            time.sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 2, 30)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


# Skills known-good as of the 2026-09-02 regeneration, already reviewed
# against real ESI descriptions and added to CORE_TARGETS'/SKILL_GROUPS'
# categorization by hand — kept here so re-running this script (e.g. after
# a future skill/ship-content patch) doesn't silently re-report them as
# "needs review." New groups discovered by a future run should get their
# own entry here ONLY after the same real-description review, then be
# merged into skill_categories_data.py/skill_groups_data.py by hand (this
# script reports diffs; it doesn't auto-decide categorization).
NEW_SKILLS_BY_CATEGORY = {
    "PVP": [
        "Burst Projector Operation", "Cloaking", "Electronic Warfare", "Frequency Modulation",
        "Long Distance Jamming", "Propulsion Jamming", "Sensor Linking", "Signal Dispersion",
        "Signal Suppression", "Signature Focusing", "Signature Masking",
        "Tactical Logistics Reconfiguration", "Target Painting", "Weapon Destabilization",
        "Weapon Disruption",
        "EDENCOM Battleship", "EDENCOM Cruiser", "EDENCOM Frigate",
        "Precursor Battlecruiser", "Precursor Battleship", "Precursor Cruiser",
        "Precursor Destroyer", "Precursor Dreadnought", "Precursor Frigate",
    ],
    "Industry": [
        "Advanced Planetology", "Command Center Upgrades", "Interplanetary Consolidation",
        "Planetology", "Remote Sensing",
    ],
}
NEW_SKILLS_BY_GROUP = {
    "Electronic Systems": NEW_SKILLS_BY_CATEGORY["PVP"][:15],
    "Spaceship Command": NEW_SKILLS_BY_CATEGORY["PVP"][15:],
    "Planet Management": NEW_SKILLS_BY_CATEGORY["Industry"],
}
# Groups whose real skills don't fit any of the 6 playstyle categories —
# reviewed via real ESI descriptions, not guessed. Skills in these groups
# that are newly published show up in the "needs review" report but are
# expected to stay excluded unless a future review changes that.
EXCLUDED_GROUPS = {"Corporation Management", "Structure Management", "Sequencing", "Fake Skills"}
# Single skills excluded despite being in an otherwise-included group —
# reviewed and found too uncertain to place (see gen_skill_data.py history
# / skill_categories_data.py's header for the reasoning).
EXCLUDED_SKILLS = {"Flag Cruisers"}


def main():
    print("Confirming Skill category (id 16) live against ESI...")
    category = _get("/universe/categories/16/")
    assert category["name"] == "Skill", f"category 16 is {category['name']!r}, not Skill — aborting"
    assert category["published"], "Skill category is unpublished — aborting"
    print(f"  OK — {len(category['groups'])} groups under category 16 'Skill'.")

    published = {}   # type_id -> {"name", "group_name"}
    for group_id in category["groups"]:
        group = _get(f"/universe/groups/{group_id}/")
        for type_id in group["types"]:
            type_data = _get(f"/universe/types/{type_id}/")
            if type_data.get("published"):
                published[type_id] = {"name": type_data["name"], "group_name": group["name"]}

    live_ids = set(published)
    live_names_by_id = {tid: v["name"] for tid, v in published.items()}
    live_group_by_name = {v["name"]: v["group_name"] for v in published.values()}

    dead = sorted(name for name, tid in SKILL_TYPE_IDS.items() if tid not in live_ids)
    tracked_names = set(SKILL_TYPE_IDS) - set(dead)
    unreviewed = sorted(
        n for tid, n in live_names_by_id.items()
        if n not in tracked_names and n not in EXCLUDED_SKILLS
        and live_group_by_name.get(n) not in EXCLUDED_GROUPS
    )

    print(f"\nDead (SKILL_TYPE_IDS entries ESI no longer reports as published) — {len(dead)}:")
    for name in dead:
        print(f"  {name}")
    print(f"\nPublished live, not yet tracked, not already reviewed-and-excluded — {len(unreviewed)}:")
    for name in unreviewed:
        print(f"  {name} ({live_group_by_name.get(name)})")
    if unreviewed:
        print(
            "\n  ^ Review these against their real ESI descriptions (GET "
            "/universe/types/{id}/) before adding — do not guess. Add a "
            "NEW_SKILLS_BY_CATEGORY/NEW_SKILLS_BY_GROUP entry above once "
            "reviewed, then re-run."
        )

    new_names = [n for names in NEW_SKILLS_BY_CATEGORY.values() for n in names]
    name_to_id = {v["name"]: tid for tid, v in published.items()}
    missing_new = [n for n in new_names if n not in name_to_id]
    if missing_new:
        print(f"\nWARNING: NEW_SKILLS_BY_CATEGORY names not found live, skipping: {missing_new}")
        new_names = [n for n in new_names if n in name_to_id]

    # ---- SKILL_TYPE_IDS ----
    new_skill_type_ids = {k: v for k, v in SKILL_TYPE_IDS.items() if k not in dead}
    for name in new_names:
        new_skill_type_ids[name] = name_to_id[name]
    new_skill_type_ids = dict(sorted(new_skill_type_ids.items()))

    # ---- FULL_SKILL_NAMES ----
    new_full_skill_names = {}
    for cat, names in FULL_SKILL_NAMES.items():
        kept = [n for n in names if n not in dead]
        kept += NEW_SKILLS_BY_CATEGORY.get(cat, [])
        new_full_skill_names[cat] = sorted(set(kept))

    # ---- CORE_TARGETS (dead-removal only — never auto-add) ----
    new_core_targets = {
        cat: {k: v for k, v in targets.items() if k not in dead}
        for cat, targets in CORE_TARGETS.items()
    }

    # ---- SKILL_GROUPS ----
    new_skill_groups = {}
    for group, names in SKILL_GROUPS.items():
        kept = [n for n in names if n not in dead]
        kept += NEW_SKILLS_BY_GROUP.get(group, [])
        if kept:
            new_skill_groups[group] = sorted(kept)
    for group, names in NEW_SKILLS_BY_GROUP.items():
        if group not in new_skill_groups:
            new_skill_groups[group] = sorted(names)
    new_skill_groups = dict(sorted(new_skill_groups.items()))

    # sanity
    for cat, targets in new_core_targets.items():
        for name in targets:
            assert name in new_skill_type_ids, ("CORE_TARGETS", cat, name)
    for cat, names in new_full_skill_names.items():
        for name in names:
            assert name in new_skill_type_ids, ("FULL_SKILL_NAMES", cat, name)
    flat_groups = {n for names in new_skill_groups.values() for n in names}
    assert flat_groups == set(new_skill_type_ids), (
        "SKILL_GROUPS and SKILL_TYPE_IDS disagree",
        flat_groups ^ set(new_skill_type_ids),
    )

    print(f"\nSKILL_TYPE_IDS: {len(SKILL_TYPE_IDS)} -> {len(new_skill_type_ids)}")

    _write_skill_categories_data(new_core_targets, new_skill_type_ids, new_full_skill_names, len(published))
    _write_skill_groups_data(new_skill_groups)
    print("\nWrote skill_categories_data.py and skill_groups_data.py.")


def _write_skill_categories_data(core_targets, skill_type_ids, full_skill_names, live_published_total):
    def fmt_core_targets():
        parts = []
        for cat, targets in core_targets.items():
            inner = ", ".join(f"{name!r}: {level}" for name, level in targets.items())
            parts.append(f"{cat!r}: {{{inner}}}")
        return "CORE_TARGETS = {" + ", ".join(parts) + "}\n"

    def fmt_skill_type_ids():
        lines = ["# name -> type_id, resolved directly (not via fuzzy search)", "SKILL_TYPE_IDS = {"]
        for name, tid in skill_type_ids.items():
            lines.append(f"    {name!r}: {tid},")
        lines.append("}\n")
        return "\n".join(lines)

    def fmt_full_skill_names():
        lines = ["FULL_SKILL_NAMES = {"]
        for cat, names in full_skill_names.items():
            lines.append(f"    {cat!r}: [")
            for name in names:
                lines.append(f"        {name!r},")
            lines.append("    ],")
        lines.append("}\n")
        return "\n".join(lines)

    header = (
        "# Auto-generated from live ESI skill-group data (all 25 real skill groups\n"
        "# under the Skill category, captured name+type_id directly from\n"
        "# /universe/groups/ + /universe/names/ — not the fuzzy /universe/ids/\n"
        "# search endpoint, which proved unreliable for exact skill names).\n"
        "# Do not hand-edit SKILL_TYPE_IDS/FULL_SKILL_NAMES; regenerate via\n"
        "# gen_skill_data.py instead if the game adds/removes skills.\n"
        "# CORE_TARGETS is hand-curated (see project_personalization_roadmap memory\n"
        "# for the history) and NOT auto-regenerated — gen_skill_data.py only ever\n"
        "# removes entries here that are no longer real skills.\n"
        "#\n"
        f"# {len(skill_type_ids)} of {live_published_total} real, currently-published,\n"
        "# player-trainable skills are covered. See gen_skill_data.py's docstring for\n"
        "# what's excluded and why, and how to review newly-added skills before\n"
        "# folding them in.\n"
        "#\n"
        "# CORE_TARGETS holds the hand-picked \"this actually matters\" skills per\n"
        "# category with specific recommended levels — always shown regardless of\n"
        "# trained level. Every other skill in FULL_SKILL_NAMES defaults to a target\n"
        "# of 5 (max).\n\n"
    )
    out_path = r"C:\Users\matpr\Downloads\Claude Project\skill_categories_data.py"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(fmt_core_targets())
        f.write("\n")
        f.write(fmt_skill_type_ids())
        f.write("\n")
        f.write(fmt_full_skill_names())


def _write_skill_groups_data(skill_groups):
    header = (
        "# Auto-generated: every skill in skill_categories_data.SKILL_TYPE_IDS,\n"
        "# grouped by its REAL EVE skill group — resolved once via each skill's\n"
        "# live group_id (/universe/types/{id}/) and that group's name\n"
        "# (/universe/groups/{id}/), not the 6 curated playstyle categories used\n"
        "# elsewhere. Do not hand-edit; regenerate via gen_skill_data.py instead if\n"
        "# the game adds/removes skills. A group with zero surviving skills after a\n"
        "# regeneration is dropped rather than kept as an empty dead end.\n\n"
        "SKILL_GROUPS = {\n"
    )
    lines = [header]
    for group, names in skill_groups.items():
        inner = ", ".join(repr(n) for n in names)
        lines.append(f"    {group!r}: [{inner}],\n")
    lines.append("}\n")

    out_path = r"C:\Users\matpr\Downloads\Claude Project\skill_groups_data.py"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))


if __name__ == "__main__":
    main()
