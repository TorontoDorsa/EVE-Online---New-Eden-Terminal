#!/usr/bin/env python3
"""
Skill gap plan
-----------------
Compares the logged-in character's trained (and queued) skill levels
against every real, player-trainable skill in the game (510 of 526,
pulled from ESI's own skill groups — see skill_categories_data.py for
what's excluded and why), bucketed into five playstyle categories that
can share skills where it genuinely applies.

Each row is tagged is_core: True for the ~63 hand-picked "this actually
matters" skills (specific recommended levels), False for the rest of the
exhaustive list (defaulted to a target of 5). Callers decide whether to
show only core/in-progress rows or the full exhaustive list.

Requires the esi-skills.read_skills.v1 and esi-skills.read_skillqueue.v1
scopes. Called via:
    python eve_esi_terminal.py mining-skills
"""

import re
import sys
import requests
from concurrent.futures import ThreadPoolExecutor

import eve_esi_terminal as esi
from skill_categories_data import CORE_TARGETS, FULL_SKILL_NAMES, SKILL_TYPE_IDS
from skill_groups_data import SKILL_GROUPS

# Kept for backward compatibility — a couple of older callers reference this.
MINING_SKILLS = [(name, CORE_TARGETS["Mining"][name]) for name in CORE_TARGETS["Mining"]]

ROMAN = {0: "-", 1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}

_DESCRIPTION_CACHE = {}


def _clean_description(raw):
    """ESI skill descriptions carry EVE's own markup (showinfo links, font
    tags, literal newlines) — strip it down to plain hover-tooltip text."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def _fetch_description(skill_id):
    if skill_id not in _DESCRIPTION_CACHE:
        try:
            data = esi.get(f"/universe/types/{skill_id}/", datasource="tranquility")
            _DESCRIPTION_CACHE[skill_id] = _clean_description(data.get("description", ""))
        except requests.RequestException:
            _DESCRIPTION_CACHE[skill_id] = ""
    return _DESCRIPTION_CACHE[skill_id]


def get_skill_descriptions(skill_ids):
    """Plain-text descriptions for a set of skill type_ids, fetched from ESI
    in parallel and cached for the life of the process."""
    ids = sorted({sid for sid in skill_ids if sid not in _DESCRIPTION_CACHE})
    if ids:
        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(_fetch_description, ids))
    return dict(_DESCRIPTION_CACHE)


def get_current_training_data(char_id=None):
    """The skill queue's currently active entry (queue_position 0), if training is running."""
    if esi.eve_sso_auth is None:
        print("eve_sso_auth.py not found — put it in the same folder as this script.")
        sys.exit(1)

    char_id = char_id or esi.eve_sso_auth.get_character_id()
    try:
        queue = esi._auth_get(f"/characters/{char_id}/skillqueue/")
    except requests.RequestException:
        queue = []

    if not queue:
        return {"training": False, "queue_length": 0}

    current = min(queue, key=lambda q: q["queue_position"])
    if not current.get("finish_date"):
        # Queue exists but is paused — nothing has a start/finish date yet.
        return {"training": False, "queue_length": len(queue)}

    return {
        "training": True,
        "skill_name": esi.resolve_type_name(current["skill_id"]),
        "level": current["finished_level"],
        "level_roman": ROMAN.get(current["finished_level"], str(current["finished_level"])),
        "start_date": current["start_date"],
        "finish_date": current["finish_date"],
        "queue_length": len(queue),
    }


def _fetch_trained_and_queued(char_id):
    trained = {s["skill_id"]: s["trained_skill_level"]
               for s in esi._auth_get(f"/characters/{char_id}/skills/")["skills"]}

    try:
        queue = esi._auth_get(f"/characters/{char_id}/skillqueue/")
    except requests.RequestException:
        queue = []
    queued = {}
    for q in queue:
        sid, lvl = q.get("skill_id"), q.get("finished_level")
        if sid is not None and lvl is not None:
            queued[sid] = max(queued.get(sid, 0), lvl)

    return trained, queued


def _gap_rows(category, trained, queued, descriptions=None):
    descriptions = descriptions or {}
    core_targets = CORE_TARGETS[category]
    rows = []
    for name in FULL_SKILL_NAMES[category]:
        target = core_targets.get(name, 5)
        is_core = name in core_targets
        skill_id = SKILL_TYPE_IDS.get(name)
        if skill_id is None:
            rows.append({"name": name, "trained": 0, "queued": 0, "target": target,
                         "status": "unknown", "is_core": is_core, "description": ""})
            continue
        have = trained.get(skill_id, 0)
        q_level = queued.get(skill_id, 0)
        if have >= target:
            status = "ok"
        elif q_level >= target:
            status = "queued"
        else:
            status = "gap"
        rows.append({"name": name, "trained": have, "queued": q_level, "target": target,
                     "status": status, "is_core": is_core,
                     "description": descriptions.get(skill_id, "")})
    return rows


def get_skill_plan_data(category="Mining"):
    if esi.eve_sso_auth is None:
        print("eve_sso_auth.py not found — put it in the same folder as this script.")
        sys.exit(1)

    char_id = esi.eve_sso_auth.get_character_id()
    trained, queued = _fetch_trained_and_queued(char_id)
    descriptions = get_skill_descriptions(SKILL_TYPE_IDS[name] for name in FULL_SKILL_NAMES[category])
    return _gap_rows(category, trained, queued, descriptions)


def get_all_skill_plans_data(char_id=None):
    """Every category's gap rows, keyed by category name, plus the full
    trained-skill dict (skill_id -> level, every trained skill, not just
    the ~510 tracked ones) so callers like ship-eligibility checks can
    reuse this one skills/queue fetch instead of repeating it."""
    if esi.eve_sso_auth is None:
        print("eve_sso_auth.py not found — put it in the same folder as this script.")
        sys.exit(1)

    char_id = char_id or esi.eve_sso_auth.get_character_id()
    trained, queued = _fetch_trained_and_queued(char_id)
    descriptions = get_skill_descriptions(SKILL_TYPE_IDS.values())

    plans = {
        category: _gap_rows(category, trained, queued, descriptions)
        for category in FULL_SKILL_NAMES
    }
    return plans, trained, queued


def get_skill_groups_data(trained, queued):
    """Every tracked skill, grouped by its real EVE skill group (not the
    5 curated playstyle categories used for tips/plans) — for a group
    browser showing each skill's trained/queued level as pips. Plain
    data only; the caller decides how to render levels 0-5 visually.
    """
    return {
        group_name: [
            {
                "name": name,
                "trained": trained.get(SKILL_TYPE_IDS[name], 0),
                "queued": queued.get(SKILL_TYPE_IDS[name], 0),
            }
            for name in skill_names
        ]
        for group_name, skill_names in SKILL_GROUPS.items()
    }


PCT_PER_LEVEL_RE = re.compile(
    r"(\d+(?:\.\d+)?)%[^.]*?per\s+(?:skill\s+)?level",
    re.IGNORECASE,
)


def rank_skill_tips(rows, limit=3, category_label="these skills", context=None):
    """Up to `limit` concrete tips about untrained/undertrained core skills
    in `rows` (a skill_plans[category] list, or a concatenation of a few),
    built entirely from each skill's own real ESI description text — never
    a guessed or hardcoded number. Two tiers, mixed and ranked together:
      1. Skills whose description states an explicit "N% ... per level"
         bonus (e.g. "4% reduction in manufacturing time per skill level"
         on Industry) — ranked by remaining-levels x per-level-%, the
         biggest real payoff first.
      2. Skills with a real gap but no quantified bonus in their
         description (e.g. ship-unlock skills like Exhumers) — ranked by
         levels remaining, listed after the quantified ones.
    Each tip is a {"text", "why"} dict — `why` starts from the skill's
    full real ESI description, so the one-line `text` stays short and
    consistent whether or not a quantified bonus was found.

    `context` is an optional {skill_name: {"factor": float, "why": str,
    "quantified_pct": float}} dict — when a skill in `rows` has an entry,
    its base ranking score (pct_per_level x levels_remaining, or
    levels_remaining alone for unquantified skills) is multiplied by
    `factor`, and `why` is appended to the skill's description. A missing
    `factor` defaults to 1.0 (no change to today's ranking); a missing
    `why` appends nothing. An optional `quantified_pct` overrides/bypasses
    the `PCT_PER_LEVEL_RE` description-regex check entirely — some real
    per-level bonuses (e.g. a ship hull's own skill-scaling bonus) never
    appear in the governing skill's own ESI description text at all, so
    there'd otherwise be no way for a skill like that to ever reach the
    quantified tier no matter how real and large its bonus is. When
    present, it's trusted as-is (the caller already resolved a real
    number some other way); when absent, behavior is unchanged. Real
    relevance (`factor > 1.0`) is a ranking TIER, not just a multiplier —
    within each of the two tiers above, skills with a real activity
    signal behind them always outrank ones without one, regardless of raw
    %/level or levels-remaining size, so a big untrained-from-scratch gap
    with no real backing can't bury a smaller gap the character is
    actually acting on; `factor`'s magnitude still breaks ties within
    that relevance tier. This function stays generic (skills + ranking
    only); the caller (dashboard.py) owns deciding what's actually true
    about this specific character's real activity/ship, so no
    domain-specific guessing happens in here.
    If every core skill in `rows` is already at target, returns a single
    congratulatory message instead of an empty list, so callers never need
    to special-case "no tips."
    """
    context = context or {}
    gaps = [r for r in rows if r.get("is_core") and r.get("status") != "ok"]

    quantified = []
    unquantified = []
    for r in gaps:
        levels_remaining = max(0, r.get("target", 0) - r.get("trained", 0))
        if levels_remaining <= 0:
            continue
        factor = context.get(r["name"], {}).get("factor", 1.0)
        is_relevant = factor > 1.0
        quantified_pct = context.get(r["name"], {}).get("quantified_pct")
        match = None if quantified_pct is not None else PCT_PER_LEVEL_RE.search(r.get("description") or "")
        if quantified_pct is not None or match:
            pct_per_level = quantified_pct if quantified_pct is not None else float(match.group(1))
            base_score = pct_per_level * levels_remaining
            quantified.append((is_relevant, base_score * factor, pct_per_level, levels_remaining, r))
        else:
            unquantified.append((is_relevant, levels_remaining * factor, levels_remaining, r))

    quantified.sort(key=lambda c: (not c[0], -c[1]))
    unquantified.sort(key=lambda c: (not c[0], -c[1]))

    def _why(r):
        base = (r.get("description") or "").strip() or "No further detail available from ESI for this skill."
        extra = context.get(r["name"], {}).get("why")
        return f"{base} {extra}" if extra else base

    tips = []
    for _, _, pct_per_level, levels_remaining, r in quantified:
        trained_roman = ROMAN.get(r["trained"], "?")
        target_roman = ROMAN.get(r["target"], "?")
        tips.append({
            "text": (
                f"Train {r['name']} from {trained_roman} to {target_roman} "
                f"({pct_per_level:.0f}% per level, "
                f"{levels_remaining} level{'s' if levels_remaining != 1 else ''} to go)."
            ),
            "why": _why(r),
        })
    for _, _, levels_remaining, r in unquantified:
        trained_roman = ROMAN.get(r["trained"], "?")
        target_roman = ROMAN.get(r["target"], "?")
        tips.append({
            "text": f"Train {r['name']} ({trained_roman} -> {target_roman}).",
            "why": _why(r),
        })

    if not tips:
        return [{
            "text": f"All core skills for {category_label} are trained to target — nothing left to prioritize here.",
            "why": "Every core skill tracked for this category is already trained to its recommended level.",
        }]

    return tips[:limit]


_STAT_LABELS = {"ore_hold_capacity": "ore hold"}


def rank_ship_tips(trained, ship_requirements, limit=3, current_stats=None):
    """Up to `limit` tips about ship access, built from a character's real
    trained-skill levels against `ship_requirements` (a {ship_name:
    {"requires": [(skill_name, level), ...], "stats": {...}}} dict, e.g.
    ship_data.MINING_SHIPS). Ships already flyable get a confirming tip;
    the closest not-yet-flyable ships get a "N levels away" tip, ranked by
    fewest levels needed first. Each tip is a {"text", "why"} dict — `why`
    gives the ship's complete real requirement list, not just the gap.

    `current_stats` is an optional dict of the character's own CURRENT
    ship's real stats (e.g. {"ore_hold_capacity": 4000.0}, from
    dashboard.get_ship_data()'s hull_stats) — when a candidate ship's
    `stats` shares a key with it, a real before/after delta is appended to
    `why` (e.g. real ore-hold capacity gained by moving up to it), not
    just the skill-level gap. Never returns an empty list.
    """
    current_stats = current_stats or {}
    already_flyable = []
    gaps = []
    for ship_name, ship in ship_requirements.items():
        reqs = ship.get("requires", ship) if isinstance(ship, dict) else ship
        stats = ship.get("stats", {}) if isinstance(ship, dict) else {}
        levels_needed = 0
        missing = []
        full_reqs = []
        for skill_name, level in reqs:
            skill_id = SKILL_TYPE_IDS.get(skill_name)
            have = trained.get(skill_id, 0) if skill_id else 0
            full_reqs.append(f"{skill_name} {ROMAN.get(level, str(level))}")
            if have < level:
                levels_needed += level - have
                missing.append(f"{skill_name} to {ROMAN.get(level, str(level))}")
        if levels_needed == 0:
            already_flyable.append(ship_name)
        else:
            gaps.append((levels_needed, ship_name, missing, full_reqs, stats))

    gaps.sort(key=lambda g: g[0])

    def _stat_delta_sentence(stats):
        for key, label in _STAT_LABELS.items():
            target_value = stats.get(key)
            current_value = current_stats.get(key)
            if target_value is None or not current_value:
                continue
            return (
                f" Its {label} ({target_value:,.0f} m³) is "
                f"{target_value / current_value:.1f}x your current ship's ({current_value:,.0f} m³)."
            )
        return ""

    tips = []
    if already_flyable:
        tips.append({
            "text": f"You can already fly: {', '.join(already_flyable)}.",
            "why": (
                "Based on comparing your trained skill levels against each ship's real "
                "ESI-listed skill requirements (the same requiredSkill/requiredSkillLevel "
                "dogma attributes the in-game fitting window uses) — not a guessed list."
            ),
        })
    for levels_needed, ship_name, missing, full_reqs, stats in gaps:
        tips.append({
            "text": (
                f"You're {levels_needed} level{'s' if levels_needed != 1 else ''} from flying "
                f"the {ship_name} — still need {', '.join(missing)}."
            ),
            "why": f"{ship_name} requires: {', '.join(full_reqs)}.{_stat_delta_sentence(stats)}",
        })

    if not tips:
        return [{"text": "No ship data available to compare against.", "why": "No curated ships were provided to check against."}]

    return tips[:limit]


def get_mining_plan_data():
    """Backward-compat wrapper — Mining category only."""
    return get_skill_plan_data("Mining")


def generate_mining_plan():
    by_name = {r["name"]: r for r in get_mining_plan_data()}
    rows = [by_name[name] for name in CORE_TARGETS["Mining"] if name in by_name]

    print("\nMining skill gap plan:\n")
    print(f"  {'Skill':<22} {'Trained':>7} {'Queued':>7} {'Target':>7}  Status")
    print(f"  {'-'*22} {'-'*7} {'-'*7} {'-'*7}  {'-'*10}")

    gaps = []
    for r in rows:
        if r["status"] == "unknown":
            print(f"  {r['name']:<22} {'?':>7} {'?':>7} {ROMAN[r['target']]:>7}  not found in ESI search")
            continue
        label = {"ok": "OK", "queued": "queued", "gap": "GAP"}[r["status"]]
        if r["status"] == "gap":
            gaps.append(r["name"])
        print(f"  {r['name']:<22} {ROMAN[r['trained']]:>7} {ROMAN[r['queued']]:>7} {ROMAN[r['target']]:>7}  {label}")

    print()
    if gaps:
        print(f"  {len(gaps)} skill(s) below target and not queued: {', '.join(gaps)}\n")
    else:
        print("  All target skills are trained or queued to target level.\n")


if __name__ == "__main__":
    generate_mining_plan()
