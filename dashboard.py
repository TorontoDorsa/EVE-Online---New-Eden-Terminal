#!/usr/bin/env python3
"""
Character dashboard
----------------------
A single combined view of the logged-in character's current state and
plans: wallet, location, ship, skills, skill queue + mining skill
gaps, assets & blueprints, active industry jobs, and recent mining
throughput / ISK-per-hour.

Requires (beyond the base scopes): esi-location.read_ship_type.v1 and
esi-assets.read_assets.v1 — re-run `python eve_sso_auth.py login` if
your saved token predates these. Called via:
    python eve_esi_terminal.py dashboard [--mining-days 7] [--hours-per-day 3]
"""

import sys
import re
import base64
import datetime
from concurrent.futures import ThreadPoolExecutor

import requests

import eve_esi_terminal as esi
import eve_sso_auth as auth
import skill_plan
import mining_report as mr
import zkillboard
import corp_overview
import ship_data
import ship_data_generated

# Every real, ESI-published Mining/PVP ship (see gen_ship_data.py), merged
# with the 13 hand-curated ships in ship_data.py — curated entries win on
# name collision, since they additionally carry wiki-verified
# `skill_bonuses` that the generated set doesn't attempt to derive. Used
# only for skill_plan.rank_ship_tips()'s ship-progression tips; the
# `_apply_ship_hull_bonuses()` calls below still use ship_data.MINING_SHIPS
# / PVP_SHIPS directly, since hull-bonus flavor text is curated-only.
ALL_MINING_SHIPS = {**ship_data_generated.GENERATED_MINING_SHIPS, **ship_data.MINING_SHIPS}
ALL_PVP_SHIPS = {**ship_data_generated.GENERATED_PVP_SHIPS, **ship_data.PVP_SHIPS}
# No hand-curated Industry ship list exists (the Industry tab never had
# ship tips before), so this is just the generated set directly.
ALL_INDUSTRY_SHIPS = ship_data_generated.GENERATED_INDUSTRY_SHIPS

SECTION = "-" * 60

SCOPE_HINTS = {
    "wallet": "esi-wallet.read_character_wallet.v1",
    "location": "esi-location.read_location.v1",
    "ship": "esi-location.read_ship_type.v1",
    "skills": "esi-skills.read_skills.v1",
    "skillqueue": "esi-skills.read_skillqueue.v1",
    "assets": "esi-assets.read_assets.v1",
    "blueprints": "esi-characters.read_blueprints.v1",
    "active_jobs": "esi-industry.read_character_jobs.v1",
    "mining": "esi-industry.read_character_mining.v1",
}

# Plain-language names for the scopes above — shown to the player instead
# of the raw esi-x.y.v1 scope string, which means nothing to someone who
# isn't a developer of this app.
SCOPE_LABELS = {
    "wallet": "wallet access",
    "location": "location access",
    "ship": "current ship info",
    "skills": "skills access",
    "skillqueue": "skill queue access",
    "assets": "assets access",
    "blueprints": "blueprints access",
    "active_jobs": "industry jobs access",
    "mining": "mining ledger access",
}


def _classify_exception(e, section_key):
    """Turns an exception raised while fetching a scoped section into the
    same {"available": False, "reason", "fixable_by_login"} shape used
    throughout — shared by _scoped() (direct call) and _scoped_result()
    (already-submitted Future), so a single missing permission only takes
    out the one section that needs it, not every tab."""
    if isinstance(e, requests.HTTPError):
        code = e.response.status_code if e.response is not None else None
        if code == 401:
            scope = SCOPE_HINTS.get(section_key, "the required scope")
            label = SCOPE_LABELS.get(section_key, "this permission")
            if scope in auth.DEFAULT_SCOPES:
                reason = f"This needs {label}, which isn't authorized on this login — click below to log in again and grant it."
                fixable = True
            else:
                reason = (
                    f"This needs {label}, which this app doesn't currently request "
                    f"during login — logging in again won't grant it; the app itself needs to be "
                    f"updated to ask for that first."
                )
                fixable = False
        elif code == 403:
            reason = (
                "Your character doesn't have the access/role needed for this data — logging in "
                "again won't help, since this depends on your character's in-game permissions, "
                "not the login itself."
            )
            fixable = False
        else:
            reason = f"EVE's servers returned an error (HTTP {code})."
            fixable = False
        return {"available": False, "reason": reason, "fixable_by_login": fixable}
    return {"available": False, "reason": f"Couldn't reach EVE's servers: {e}", "fixable_by_login": False}


def _scoped(fetch_fn, section_key, default):
    """Runs fetch_fn() and returns (result, permission) — see
    _classify_exception() for what happens on a 401/403/other failure."""
    try:
        return fetch_fn(), {"available": True, "reason": None, "fixable_by_login": False}
    except requests.RequestException as e:
        return default, _classify_exception(e, section_key)


def _scoped_result(future, section_key, default):
    """Same contract as _scoped(), but resolves an already-submitted
    concurrent.futures.Future instead of calling fetch_fn() directly —
    lets get_dashboard_data() submit every independent ESI-backed section
    to a thread pool up front, so the sections only wait on each other
    when they actually have to, instead of paying for the full sum of
    every section's latency in sequence."""
    try:
        return future.result(), {"available": True, "reason": None, "fixable_by_login": False}
    except requests.RequestException as e:
        return default, _classify_exception(e, section_key)


def _header(title):
    print(f"\n{SECTION}\n{title}\n{SECTION}")


_HULL_STAT_ATTRS = {
    "shield_capacity": 263, "armor_hp": 265, "shield_recharge_ms": 479,
    # verified live against real ESI dogma_attributes (see project memory) —
    # generalMiningHoldCapacity, turretSlotsLeft, launcherSlotsLeft.
    "ore_hold_capacity": 1556, "turret_hardpoints": 102, "launcher_hardpoints": 101,
}


def get_ship_data(char_id):
    ship = esi._auth_get(f"/characters/{char_id}/ship/", datasource="tranquility")
    type_id = ship["ship_type_id"]
    type_data = esi.get(f"/universe/types/{type_id}/", datasource="tranquility")
    by_attr = {a["attribute_id"]: a["value"] for a in type_data.get("dogma_attributes", [])}
    hull_stats = {
        key: by_attr.get(attr_id)
        for key, attr_id in _HULL_STAT_ATTRS.items()
    }
    return {
        "type_id": type_id,
        "type_name": esi.resolve_type_name(type_id),
        "ship_name": ship.get("ship_name", "unnamed"),
        "hull_stats": hull_stats,
        "ship_item_id": ship.get("ship_item_id"),
    }


def get_portrait_data_uri(char_id):
    """Character portrait as an inline data: URI, for a self-contained
    dashboard header. Decorative only — any failure here shouldn't break
    the rest of the dashboard, so this degrades to "" rather than raising."""
    try:
        portrait = esi.get(f"/characters/{char_id}/portrait/", datasource="tranquility")
        url = portrait.get("px256x256") or portrait.get("px128x128") or portrait.get("px512x512")
        if not url:
            return ""
        img = requests.get(url, timeout=15)
        img.raise_for_status()
        ctype = img.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        b64 = base64.b64encode(img.content).decode("ascii")
        return f"data:{ctype};base64,{b64}"
    except requests.RequestException:
        return ""


def get_corporation_data(char_id):
    """Public endpoint — no auth needed beyond knowing the character's corp_id."""
    corp_id = esi.get_corporation_id(char_id)
    corp = esi.get_corporation(corp_id)
    return {"id": corp_id, "name": corp["name"], "ticker": corp["ticker"]}


def _ship(char_id):
    ship = get_ship_data(char_id)
    print(f"  Current ship : {ship['type_name']} ({ship['ship_name']})")


def _fetch_assets(char_id):
    """Raw /characters/{id}/assets/ list — shared by get_assets_data()
    (Industry tab summary) and get_current_fit_data() (real fitted-gear
    checks below) so the same character's asset list is only fetched
    once per dashboard load, not twice."""
    return esi._auth_get(f"/characters/{char_id}/assets/", datasource="tranquility")


def get_assets_data(assets, top_n=8):
    if not assets:
        return {"total_items": 0, "distinct_locations": 0, "top_items": []}

    locations = {a["location_id"] for a in assets}
    counts = {}
    for a in assets:
        counts[a["type_id"]] = counts.get(a["type_id"], 0) + a.get("quantity", 1)
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]
    return {
        "total_items": len(assets),
        "distinct_locations": len(locations),
        "top_items": [{"name": esi.resolve_type_name(tid), "quantity": qty} for tid, qty in top],
    }


_FIT_SLOT_PREFIXES = ("HiSlot", "MedSlot", "LoSlot", "RigSlot", "DroneBay")
_MODULE_TYPE_INFO_CACHE = {}


def _module_type_info(type_id):
    """Real dogma/market info for a fitted module or loaded charge —
    same verified pattern already used for ships and mining crystals
    this session. Cached: none of this changes for a given type_id, so
    repeat lookups (an unchanged fit, or a module already seen as an
    upgrade candidate for a different fitted item) cost zero extra ESI
    calls.

    Beyond requiredSkill/requiredSkillLevel (used by get_current_fit_data
    for skill-gap tips), also captures market_group_id/tech_level/
    meta_level — real, CCP-ranked fields (techLevel = attribute 422,
    metaLevel = attribute 633, both verified live via
    /dogma/attributes/{id}/) needed by _fit_upgrade_tips() to find a
    genuinely better variant of a fitted module (e.g. Miner I -> Miner II)
    via its real market-group siblings, not a guessed stat comparison.

    Also warms esi.resolve_type_name()'s own cache with the item's name
    from this same response — that endpoint (/universe/types/{id}/) was
    otherwise being fetched twice per fitted item (once here for
    dogma_attributes, once by resolve_type_name() for the name alone);
    get_current_fit_data() calls this function first so the second call
    hits the warmed cache instead of refetching."""
    if type_id not in _MODULE_TYPE_INFO_CACHE:
        t = esi.get(f"/universe/types/{type_id}/", datasource="tranquility")
        esi._TYPE_NAME_CACHE.setdefault(type_id, t["name"])
        by_attr = {a["attribute_id"]: a["value"] for a in t.get("dogma_attributes", [])}
        reqs = []
        for skill_attr, level_attr in ((182, 277), (183, 278), (184, 279)):
            skill_type_id = by_attr.get(skill_attr)
            if skill_type_id:
                level = int(by_attr.get(level_attr, 1))
                reqs.append((esi.resolve_type_name(int(skill_type_id)), level))
        _MODULE_TYPE_INFO_CACHE[type_id] = {
            "name": t["name"],
            "required_skills": reqs,
            "market_group_id": t.get("market_group_id"),
            "tech_level": by_attr.get(422, 1.0),
            "meta_level": by_attr.get(633, 0.0),
            "published": t.get("published", False),
        }
    return _MODULE_TYPE_INFO_CACHE[type_id]


_MARKET_GROUP_TYPES_CACHE = {}


def _market_group_types(market_group_id):
    """Real sibling type_ids sharing a module's market group — verified
    live (2026-09-03): this is the exact real "Variations" grouping the
    in-game fitting window shows (Tech I/II/Storyline/Faction tabs), not
    a guessed relationship. E.g. group 1039 "Mining Lasers" contains
    both Miner I and Miner II. Cached: market-group membership doesn't
    change during a session."""
    if market_group_id not in _MARKET_GROUP_TYPES_CACHE:
        try:
            g = esi.get(f"/markets/groups/{market_group_id}/", datasource="tranquility")
            _MARKET_GROUP_TYPES_CACHE[market_group_id] = g.get("types", [])
        except requests.RequestException:
            _MARKET_GROUP_TYPES_CACHE[market_group_id] = []
    return _MARKET_GROUP_TYPES_CACHE[market_group_id]


def get_current_fit_data(assets, ship_item_id):
    """Real modules (and loaded charges, e.g. mining crystals) currently
    fitted on the character's own current ship — not just the hull.
    Verified live: asset entries whose location_id matches the ship's
    real ship_item_id (a specific asset instance, distinct from
    ship_type_id) and whose location_flag names a real fitting slot are
    genuinely fitted. A loaded charge shares its turret's exact slot
    flag, so the specific crystal loaded right now is identifiable, not
    just what's carried as spares.

    DroneBay is included too — a real T2 drone (verified live: Hobgoblin
    II) carries the exact same requiredSkill/requiredSkillLevel dogma
    attributes a fitted module does (Light Drone Operation, Gallente
    Drone Specialization, Drones), so a carried drone is active,
    launchable gear the same way a loaded mining crystal is — unlike
    plain Cargo, which stays excluded as genuinely just carried."""
    if not ship_item_id:
        return []
    fitted = [
        a for a in assets
        if a.get("location_id") == ship_item_id
        and (a.get("location_flag") or "").startswith(_FIT_SLOT_PREFIXES)
    ]
    result = []
    for a in fitted:
        info = _module_type_info(a["type_id"])  # warms the name cache below
        result.append({
            "type_id": a["type_id"],
            "name": esi.resolve_type_name(a["type_id"]),
            "slot": a.get("location_flag"),
            "required_skills": info["required_skills"],
        })
    return result


def _assets_summary(char_id):
    data = get_assets_data(_fetch_assets(char_id))
    if not data["top_items"] and data["total_items"] == 0:
        print("  No assets found.")
        return
    print(f"  Total items         : {data['total_items']:,}")
    print(f"  Distinct locations  : {data['distinct_locations']}")
    print("  Top item types:")
    for item in data["top_items"]:
        print(f"    {item['name']:<28} x{item['quantity']:,}")


def get_active_jobs_data(char_id):
    jobs = esi._auth_get(f"/characters/{char_id}/industry/jobs/", datasource="tranquility")
    active = [j for j in jobs if j["status"] == "active"]
    prices = mr.get_prices()
    result = []
    for j in active:
        type_id = j.get("product_type_id") or j["blueprint_type_id"]
        result.append({
            "product": esi.resolve_type_name(type_id),
            "activity_id": j["activity_id"],
            "end_date": j["end_date"],
            "avg_sell_price": prices.get(type_id),
        })
    return result


def _active_jobs(char_id):
    active = get_active_jobs_data(char_id)
    if not active:
        print("  No active industry jobs.")
        return
    for j in active:
        price = f"{j['avg_sell_price']:,.2f} ISK" if j["avg_sell_price"] else "n/a"
        print(f"  {j['product']:<28} activity {j['activity_id']}  ends {j['end_date']}  avg sell {price}")


_WALLET_ACTIVITY_REF_TYPES = {
    "mission": ("agent_mission_reward", "agent_mission_time_bonus_reward"),
    "bounty": ("bounty_prizes",),
    "industry": ("industry_job_tax", "manufacturing", "researching_time_productivity",
                 "researching_material_productivity", "reprocessing_tax"),
}


def get_net_isk_data(char_id, days):
    """Net ISK (earned minus spent) from the wallet journal over the last N
    days, plus a real ref_type breakdown (mission rewards, NPC bounty
    prizes, industry-related entries — verified live against this
    character's real journal) used by _build_character_profile() to
    ground theme detection/tip personalization in real financial
    activity, not just skill training ratios."""
    entries = mr._paged_get(f"/characters/{char_id}/wallet/journal/")
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    recent = [e for e in entries if e.get("date", "") >= cutoff]

    earned = sum(e["amount"] for e in recent if e["amount"] > 0)
    spent = sum(-e["amount"] for e in recent if e["amount"] < 0)

    by_ref_type = {}
    for label, ref_types in _WALLET_ACTIVITY_REF_TYPES.items():
        matching = [e for e in recent if e.get("ref_type") in ref_types]
        by_ref_type[label] = {"count": len(matching), "isk": sum(e["amount"] for e in matching)}

    return {
        "window_days": days,
        "net_isk": earned - spent,
        "earned": earned,
        "spent": spent,
        "by_ref_type": by_ref_type,
    }


_TYPE_GROUP_CACHE = {}
_GROUP_INFO_CACHE = {}


def _categorize_mining_type(type_id):
    """Ore vs Ice vs Gas vs Other, verified against real ESI category/group
    data rather than guessed from names: ore and ice both sit under
    category_id 25 (Asteroid) — ice is specifically group "Ice", everything
    else in that category is ore (including moon-ore variants). Gas sits
    under category_id 2, group "Harvestable Cloud"."""
    if type_id not in _TYPE_GROUP_CACHE:
        t = esi.get(f"/universe/types/{type_id}/", datasource="tranquility")
        _TYPE_GROUP_CACHE[type_id] = t["group_id"]
    group_id = _TYPE_GROUP_CACHE[type_id]

    if group_id not in _GROUP_INFO_CACHE:
        g = esi.get(f"/universe/groups/{group_id}/", datasource="tranquility")
        _GROUP_INFO_CACHE[group_id] = (g["category_id"], g["name"])
    category_id, group_name = _GROUP_INFO_CACHE[group_id]

    if category_id == 2 and group_name == "Harvestable Cloud":
        return "Gas"
    if category_id == 25 and group_name == "Ice":
        return "Ice"
    if category_id == 25:
        return "Ore"
    return "Other"


def get_mining_breakdown_data(char_id, days):
    """Mined quantity/value over the window, bucketed into Ore/Ice/Gas/Other
    and broken out per resource type within each."""
    entries = esi._auth_get(f"/characters/{char_id}/mining/", datasource="tranquility")
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    entries = [e for e in entries if e["date"] >= cutoff]

    category_totals = {"Ore": 0.0, "Ice": 0.0, "Gas": 0.0, "Other": 0.0}
    if not entries:
        return {"window_days": days, "category_totals": category_totals, "by_type": []}

    prices = mr.get_prices()
    agg = {}
    for e in entries:
        tid = e["type_id"]
        row = agg.setdefault(tid, {"quantity": 0, "isk_value": 0.0})
        row["quantity"] += e["quantity"]
        row["isk_value"] += e["quantity"] * prices.get(tid, 0)

    rows = []
    for tid, data in agg.items():
        category = _categorize_mining_type(tid)
        rows.append({
            "type_id": tid,
            "name": esi.resolve_type_name(tid),
            "category": category,
            "quantity": data["quantity"],
            "isk_value": data["isk_value"],
        })
        category_totals[category] += data["isk_value"]

    rows.sort(key=lambda r: -r["isk_value"])

    return {
        "window_days": days,
        "category_totals": category_totals,
        "by_type": rows,
    }


def get_jita_resource_prices(type_ids):
    """Live Jita (The Forge) buy/sell order-book prices for a set of
    resource types, as of right now — distinct from the server-wide
    average used elsewhere on this dashboard. One order-book call per
    type; ESI has no bulk multi-type endpoint for this."""
    region_id = esi.REGIONS["the forge"]
    rows = []
    for tid in type_ids:
        orders = esi.get(f"/markets/{region_id}/orders/", datasource="tranquility",
                          order_type="all", type_id=tid)
        buys = [o["price"] for o in orders if o["is_buy_order"]]
        sells = [o["price"] for o in orders if not o["is_buy_order"]]
        rows.append({
            "type_id": tid,
            "name": esi.resolve_type_name(tid),
            "jita_buy": max(buys) if buys else None,
            "jita_sell": min(sells) if sells else None,
        })
    rows.sort(key=lambda r: -(r["jita_sell"] or 0))
    return rows


_REGION_NAME_CACHE = {}


def _resolve_region_name(region_id):
    if region_id not in _REGION_NAME_CACHE:
        try:
            data = esi.get(f"/universe/regions/{region_id}/", datasource="tranquility")
            _REGION_NAME_CACHE[region_id] = data["name"]
        except requests.RequestException:
            _REGION_NAME_CACHE[region_id] = f"region_id {region_id}"
    return _REGION_NAME_CACHE[region_id]


def get_market_order_tips(char_id, limit=3):
    """Tips about the character's own open market orders, comparing each
    order's price against the current live order book for that item in
    the SAME region the order is actually sitting in (regional prices
    vary a lot — comparing against a different region, e.g. always Jita,
    would be misleading). Uses esi-markets.read_character_orders.v1 —
    granted but otherwise unused elsewhere on this dashboard. A sell order
    priced above the current lowest ask is being undercut and won't move;
    a buy order priced below the current highest bid isn't competitive
    either. ESI omits `is_buy_order` entirely for sell orders (false is
    the omitted default), so it's read with a default rather than indexed
    directly. Ranked by ISK at stake (price x remaining volume) so the
    costliest issue surfaces first. Each tip is a {"text", "why"} dict —
    `why` carries the exact price comparison plus the region name. Never
    returns an empty list.

    Returns (tips, stats) — `stats` = {"order_count", "undercut_count",
    "isk_at_stake"} (across ALL non-competitive orders, not just the ones
    that made the `limit` cut) so callers like _industry_context() can
    ground Broker-Relations-family skill tips in the real order book
    without re-fetching it.
    """
    empty_stats = {"order_count": 0, "undercut_count": 0, "isk_at_stake": 0}
    try:
        orders = esi._auth_get(f"/characters/{char_id}/orders/", datasource="tranquility")
    except requests.RequestException:
        return [{"text": "Couldn't load your open market orders right now.", "why": "The request for your open orders failed."}], empty_stats

    if not orders:
        return [{"text": "You have no open market orders.", "why": "/characters/{id}/orders/ returned an empty list for this character."}], empty_stats

    book_cache = {}
    flagged = []
    competitive_count = 0
    for o in orders:
        region_id, type_id = o["region_id"], o["type_id"]
        key = (region_id, type_id)
        if key not in book_cache:
            book_orders = esi.get(f"/markets/{region_id}/orders/", datasource="tranquility",
                                   order_type="all", type_id=type_id)
            buys = [ob["price"] for ob in book_orders if ob.get("is_buy_order", False)]
            sells = [ob["price"] for ob in book_orders if not ob.get("is_buy_order", False)]
            book_cache[key] = {"buy": max(buys) if buys else None, "sell": min(sells) if sells else None}
        book = book_cache[key]

        isk_at_stake = o["price"] * o["volume_remain"]
        name = esi.resolve_type_name(type_id)
        region_name = _resolve_region_name(region_id)
        if o.get("is_buy_order", False):
            best = book["buy"]
            if best and o["price"] < best:
                flagged.append((isk_at_stake, {
                    "text": (
                        f"Your buy order for {name} ({o['price']:,.2f} ISK) is below the current "
                        f"top bid ({best:,.2f} ISK) — it won't fill until raised."
                    ),
                    "why": (
                        f"In {region_name}, the current highest buy order for {name} is {best:,.2f} ISK, "
                        f"above your {o['price']:,.2f} ISK. Sellers accept the highest-priced buy order "
                        f"first, so a lower buy order sits unfilled until it's raised or the competing "
                        f"order expires. {o['volume_remain']:,} units remain on this order "
                        f"({isk_at_stake:,.2f} ISK at stake)."
                    ),
                }))
            else:
                competitive_count += 1
        else:
            best = book["sell"]
            if best and o["price"] > best:
                flagged.append((isk_at_stake, {
                    "text": (
                        f"Your sell order for {name} ({o['price']:,.2f} ISK) is above the current "
                        f"lowest ask ({best:,.2f} ISK) — it's being undercut."
                    ),
                    "why": (
                        f"In {region_name}, the current lowest sell order for {name} is {best:,.2f} ISK, "
                        f"below your {o['price']:,.2f} ISK. Buyers pick the cheapest available sell order "
                        f"first, so a higher-priced order sits unsold until it's undercut-matched or the "
                        f"competing order expires. {o['volume_remain']:,} units remain on this order "
                        f"({isk_at_stake:,.2f} ISK at stake)."
                    ),
                }))
            else:
                competitive_count += 1

    flagged.sort(key=lambda f: -f[0])
    tips = [tip for _, tip in flagged[:limit]]
    stats = {
        "order_count": len(orders),
        "undercut_count": len(flagged),
        "isk_at_stake": sum(isk for isk, _ in flagged),
    }

    if not tips:
        tips.append({
            "text": f"All {len(orders)} of your open market order(s) are currently competitive.",
            "why": "Each open order's price is at or better than the best competing order in its own region, so none are currently being undercut or outbid.",
        })

    return tips, stats


def get_mining_throughput_data(char_id, days, hours_per_day):
    entries = esi._auth_get(f"/characters/{char_id}/mining/", datasource="tranquility")
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    entries = [e for e in entries if e["date"] >= cutoff]
    if not entries:
        return {"window_days": days, "active_days": 0, "total_isk": 0, "isk_per_active_day": 0,
                "isk_per_hour": None, "hours_per_day": hours_per_day, "daily": []}

    prices = mr.get_prices()
    daily_isk = {}
    for e in entries:
        isk = e["quantity"] * prices.get(e["type_id"], 0)
        daily_isk[e["date"]] = daily_isk.get(e["date"], 0) + isk

    total_isk = sum(daily_isk.values())
    active_days = len(daily_isk)
    isk_per_hour = (total_isk / (active_days * hours_per_day)) if hours_per_day else None

    return {
        "window_days": days,
        "active_days": active_days,
        "total_isk": total_isk,
        "isk_per_active_day": total_isk / active_days,
        "isk_per_hour": isk_per_hour,
        "hours_per_day": hours_per_day,
        "daily": [{"date": d, "isk": isk} for d, isk in sorted(daily_isk.items())],
    }


def get_sales_throughput_data(char_id, days, hours_per_day):
    """ISK actually received from market sell transactions — real transaction
    prices, not an estimate (unlike mining, which prices ore at market average
    since raw ore itself isn't what gets sold)."""
    transactions = esi._auth_get(f"/characters/{char_id}/wallet/transactions/", datasource="tranquility")
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    sells = [t for t in transactions if not t["is_buy"] and t["date"][:10] >= cutoff]
    if not sells:
        return {"window_days": days, "active_days": 0, "total_isk": 0, "isk_per_active_day": 0,
                "isk_per_hour": None, "hours_per_day": hours_per_day, "daily": []}

    daily_isk = {}
    for t in sells:
        date = t["date"][:10]
        isk = t["quantity"] * t["unit_price"]
        daily_isk[date] = daily_isk.get(date, 0) + isk

    total_isk = sum(daily_isk.values())
    active_days = len(daily_isk)
    isk_per_hour = (total_isk / (active_days * hours_per_day)) if hours_per_day else None

    return {
        "window_days": days,
        "active_days": active_days,
        "total_isk": total_isk,
        "isk_per_active_day": total_isk / active_days,
        "isk_per_hour": isk_per_hour,
        "hours_per_day": hours_per_day,
        "daily": [{"date": d, "isk": isk} for d, isk in sorted(daily_isk.items())],
    }


def _mining_throughput(char_id, days, hours_per_day):
    data = get_mining_throughput_data(char_id, days, hours_per_day)
    if not data["daily"]:
        print(f"  No mining ledger entries in the last {days} days.")
        return

    print(f"  Window            : last {days} days")
    print(f"  Active days       : {data['active_days']}")
    print(f"  Total mined value : {data['total_isk']:,.0f} ISK")
    print(f"  ISK / active day  : {data['isk_per_active_day']:,.0f} ISK")
    if data["isk_per_hour"] is not None:
        print(f"  ISK / hour (est, assuming {hours_per_day}h per active day): {data['isk_per_hour']:,.0f} ISK")
    else:
        print("  ESI doesn't track session length, so ISK/hour needs an assumption —")
        print("  pass --hours-per-day to estimate it.")


OMEGA_PLEX_COST = 500  # CCP's fixed rate: 500 PLEX = 30 days of Omega


def get_plex_data(char_id, vault_plex_owned=0):
    """Current PLEX price, plus the ISK cost of 1 month of Omega.

    PLEX no longer trades through the regular regional order book (CCP moved
    it to a separate per-region vault system years ago), so the region
    orders endpoint returns nothing for it. ESI's /markets/prices/ endpoint
    still tracks a live average_price for it — but that figure is a
    server-wide average, not a Jita-specific order-book price, since ESI
    doesn't expose the regional PLEX vaults publicly.

    Separately: PLEX you actually own normally sits in the account-wide
    PLEX Vault, which has NO ESI endpoint at all (confirmed via CCP's own
    support docs — it's deliberately excluded, account-level, not
    per-character). The only PLEX ESI can see is loose PLEX sitting as a
    plain tradeable asset outside the vault. So vault_plex_owned must be
    supplied manually — there's no way to fetch it.
    """
    resolved = esi.resolve_ids(["PLEX"])
    matches = resolved.get("inventory_types", [])
    if not matches:
        return {"available": False}
    type_id = matches[0]["id"]

    prices = mr.get_prices()
    avg_price = prices.get(type_id)

    assets = esi._auth_get(f"/characters/{char_id}/assets/", datasource="tranquility")
    loose_owned_qty = sum(a.get("quantity", 0) for a in assets if a["type_id"] == type_id)
    owned_qty = loose_owned_qty + vault_plex_owned

    plex_needed = max(0, OMEGA_PLEX_COST - owned_qty)

    return {
        "available": avg_price is not None,
        "loose_owned_qty": loose_owned_qty,
        "vault_plex_owned": vault_plex_owned,
        "avg_price": avg_price,
        "omega_plex_cost": OMEGA_PLEX_COST,
        "omega_isk_cost": avg_price * OMEGA_PLEX_COST if avg_price else None,
        "owned_qty": owned_qty,
        "owned_value_isk": owned_qty * avg_price if avg_price else None,
        "plex_needed": plex_needed,
        "purchase_cost_isk": plex_needed * avg_price if avg_price else None,
    }


def get_corp_tips(corp_overview_data):
    """A handful of factual observations about the character's corp — not
    skill-based, so it doesn't fit rank_skill_tips(). Deliberately plain
    facts rather than computed ISK figures: guessing which of the
    character's income streams are actually subject to corp tax (bounty
    and other NPC-derived income, not mining or market sales) risks
    stating something mechanically wrong about how the tax applies."""
    tips = []
    membership = corp_overview_data.get("membership") or {}
    tax_rate = membership.get("tax_rate")
    if tax_rate is not None:
        if tax_rate == 0:
            tips.append({
                "text": "Your corp's tax rate is 0% — no cut is taken from bounty or other NPC-derived income.",
                "why": "This is a fraction taken from bounty and other NPC-derived income specifically — it does not apply to mining yield or player-to-player market sales.",
            })
        else:
            tips.append({
                "text": (
                    f"Your corp's tax rate is {tax_rate * 100:.0f}% — this applies to bounty and "
                    f"other NPC-derived income, not to mining yield or market sales directly."
                ),
                "why": "This only describes the cut taken from bounty and other NPC-derived income — mining and market sales are unaffected by this figure, so it isn't safe to apply it to those ISK totals.",
            })

    wars = corp_overview_data.get("wars") or {}
    if wars.get("war_eligible"):
        tips.append({
            "text": "Your corp is currently war-eligible — worth keeping an eye on zKillboard for hostile activity.",
            "why": "War eligibility means another corp or alliance could file a formal wardec against yours, opening mutual PVP outside normal security-status restrictions.",
        })
    else:
        tips.append({
            "text": "Your corp isn't currently war-eligible, so a formal wardec against it isn't a live risk right now.",
            "why": "Your corp currently isn't war-eligible — a corp typically becomes war-eligible once it holds structures or meets other CONCORD criteria, none of which currently apply here.",
        })

    home = corp_overview_data.get("home") or {}
    if home.get("available"):
        tips.append({
            "text": f"Corp home base is {home.get('name')} in {home.get('system_name')}.",
            "why": "This is the corp's registered home station/structure as reported by the game's own corporation records.",
        })

    structures = corp_overview_data.get("structures") or {}
    if not structures.get("available"):
        tips.append({
            "text": (
                "Structure fuel status isn't visible without Director-level corp roles — "
                "ask a director to check the fuel bunkers directly if that matters to you."
            ),
            "why": "Structure data is only available to characters holding a Director role in the corp — this character doesn't currently have one, so the app can't fetch it on your behalf.",
        })

    return tips or [{"text": "No corp data available to build tips from.", "why": "The corp overview fetch returned no usable data."}]


_PVP_ACTIVITY_WINDOW_DAYS = 90


def _ship_display_label(ship):
    """"{custom name} ({hull type})" for tip text, e.g. "Wayfinder
    (Retriever)" — falls back to whichever single value is actually known
    rather than showing empty parens (a scope gap can leave either one
    missing)."""
    name = ship.get("ship_name")
    type_name = ship.get("type_name")
    if name and type_name:
        return f"{name} ({type_name})"
    return name or type_name or "your ship"


def _build_character_profile(skill_plans, mining_breakdown, active_jobs, ship, zkill, net_isk, market_stats, fit_data):
    """Cross-category signals computed once per dashboard load and reused
    by every tip category's context-builder below — blends each
    category's real skill-training completion ratio with real behavioral
    confirmation (mined ISK, active/historical industry activity,
    mission/bounty ISK from the wallet journal, zKillboard activity), so
    tips can weigh multiple real factors together ("what is this
    character actually built for") instead of skill investment alone.
    Every input here is already fetched elsewhere in get_dashboard_data()
    — this is pure computation, no new ESI calls except the by_ref_type
    breakdown already added to get_net_isk_data()."""
    theme = {}
    for category, rows in skill_plans.items():
        core_rows = [r for r in rows if r.get("is_core") and r.get("target")]
        skill_ratio = (
            sum(min(r["trained"], r["target"]) / r["target"] for r in core_rows) / len(core_rows)
            if core_rows else 0.0
        )
        theme[category] = {"skill_ratio": skill_ratio, "confirmed_by_activity": False}

    mining_totals = mining_breakdown.get("category_totals") or {}
    if sum(mining_totals.values()) > 0 and "Mining" in theme:
        theme["Mining"]["confirmed_by_activity"] = True

    by_ref_type = net_isk.get("by_ref_type") or {}
    mission_isk = by_ref_type.get("mission", {}).get("isk", 0)
    bounty_isk = by_ref_type.get("bounty", {}).get("isk", 0)
    industry_journal_isk = by_ref_type.get("industry", {}).get("isk", 0)
    if (mission_isk > 0 or bounty_isk > 0) and "Mission Running" in theme:
        theme["Mission Running"]["confirmed_by_activity"] = True

    active_manufacturing = [j for j in active_jobs if j.get("activity_id") == 1]
    active_research = [j for j in active_jobs if j.get("activity_id") in (3, 4, 5, 8)]
    if (active_manufacturing or active_research or industry_journal_isk) and "Industry" in theme:
        theme["Industry"]["confirmed_by_activity"] = True

    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=_PVP_ACTIVITY_WINDOW_DAYS)).isoformat()
    recent_kills = [k for k in (zkill.get("kills") or []) if k.get("date", "") >= cutoff]
    recent_losses = [l for l in (zkill.get("losses") or []) if l.get("date", "") >= cutoff]
    if (recent_kills or recent_losses) and "PVP" in theme:
        theme["PVP"]["confirmed_by_activity"] = True

    dominant_category = max(theme, key=lambda c: theme[c]["skill_ratio"]) if theme else None

    return {
        "theme": theme,
        "dominant_category": dominant_category,
        "mining_activity": {
            "totals": mining_totals, "by_type": mining_breakdown.get("by_type", []),
            "window_days": mining_breakdown.get("window_days"),
        },
        "industry_activity": {
            "active_manufacturing": active_manufacturing, "active_research": active_research,
            "journal_isk": industry_journal_isk,
        },
        "mission_activity": {
            "mission_isk": mission_isk, "mission_count": by_ref_type.get("mission", {}).get("count", 0),
            "bounty_isk": bounty_isk, "bounty_count": by_ref_type.get("bounty", {}).get("count", 0),
        },
        "pvp_activity": {"kills": recent_kills, "losses": recent_losses, "window_days": _PVP_ACTIVITY_WINDOW_DAYS},
        "ship_stats": ship.get("hull_stats") or {},
        "market_activity": market_stats,
        "current_fit": {"ship_name": _ship_display_label(ship), "modules": fit_data},
    }


# Which real ore types each Ore Processing skill boosts — copied verbatim
# from each skill's own real ESI description (verified live, not guessed;
# see the item-requirement sweep of the "Mining Crystal" group that found
# these 9 skills gate real mining-crystal tiers). Static because these
# descriptions don't change; re-verify against ESI if CCP ever adds an
# ore type.
_ORE_PROCESSING_ORE_TYPES = {
    "Simple Ore Processing": {"Plagioclase", "Pyroxeres", "Scordite", "Veldspar", "Mordunium"},
    "Coherent Ore Processing": {"Hedbergite", "Hemorphite", "Jaspet", "Kernite", "Omber", "Ytirium", "Griemeer", "Nocxite"},
    "Complex Ore Processing": {"Arkonor", "Bistot", "Spodumain", "Eifyrium", "Ducinium", "Hezorime", "Ueganite"},
    "Variegated Ore Processing": {"Crokite", "Dark Ochre", "Gneiss", "Kylixium"},
    "Erratic Ore Processing": {"Prismaticite"},
    "Abyssal Ore Processing": {"Bezdnacine", "Rakovene", "Talassonite"},
    "Ubiquitous Moon Ore Processing": {"Zeolites", "Sylvite", "Bitumens", "Coesite"},
    "Common Moon Ore Processing": {"Cobaltite", "Euxenite", "Titanite", "Scheelite"},
    "Uncommon Moon Ore Processing": {"Otavite", "Sperrylite", "Vanadinite", "Chromite"},
    "Rare Moon Ore Processing": {"Carnotite", "Zircon", "Pollucite", "Cinnabar"},
    "Exceptional Moon Ore Processing": {"Xenotime", "Monazite", "Loparite", "Ytterbite"},
}

_ORE_GRADE_SUFFIX_RE = re.compile(r"\s+[IVX]+-Grade$")


def _base_ore_name(name):
    """Real mined-ore rows include compressed/refined grade variants (e.g.
    "Veldspar II-Grade") that share their base ore's processing skill —
    strip the suffix so relevance-matching works against the plain name."""
    return _ORE_GRADE_SUFFIX_RE.sub("", name)


def _ship_hull_bonus_entry(ship_label, skill_name, bonus_lines, trained_level, target_level):
    """Real before/after text for a skill governed by the character's
    CURRENT ship's own hull-encoded bonus (ship_data.py's `skill_bonuses`
    — verified live against ESI dogma_effects AND cross-checked line for
    line against EVE University's real per-ship wiki page for every
    curated ship, since neither the effect's func type nor its name
    reliably flags a real per-level bonus vs. a flat Role Bonus on its
    own). `bonus_lines[0]` is the skill's headline bonus on this hull
    (ore/damage yield where one exists); any remaining lines are folded
    into the sentence, not the score. This is at least as concrete as a
    fitted-module match (_FIT_RELEVANCE_FACTOR) — it's the character's
    real, currently-flown ship, not just something carried — so it gets
    the same top-tier factor."""
    headline = bonus_lines[0]
    pct, label = headline["pct_per_level"], headline["label"]
    current_total, target_total = pct * trained_level, pct * target_level
    if pct >= 0:
        headline_sentence = (
            f"would raise your {ship_label}'s {label} bonus from {current_total:.1f}% "
            f"to {target_total:.1f}% ({pct:.1f}% per level)"
        )
    else:
        headline_sentence = (
            f"would improve your {ship_label}'s {label} from a {abs(current_total):.1f}% "
            f"to a {abs(target_total):.1f}% reduction ({abs(pct):.1f}% per level)"
        )
    extra = bonus_lines[1:]
    extra_sentence = ""
    if extra:
        parts = [f"a real {b['pct_per_level']:+.1f}%/level {b['label']} bonus" for b in extra]
        extra_sentence = " It also comes with " + ", ".join(parts) + "."
    trained_roman = skill_plan.ROMAN.get(trained_level, "?")
    target_roman = skill_plan.ROMAN.get(target_level, "?")
    why = (
        f"Training {skill_name} from {trained_roman} to {target_roman} {headline_sentence}."
        f"{extra_sentence}"
    )
    return {"quantified_pct": pct, "factor": _FIT_RELEVANCE_FACTOR, "why": why}


def _apply_ship_hull_bonuses(context, ship, curated_ships, rows):
    """Overlays real quantified_pct entries for any governing skill on the
    character's CURRENT ship (matched against a curated ship_data.py dict
    by real type_name) with a real trained-vs-target gap — e.g. Mining
    Barge on a Retriever, Minmatar Frigate on a Rifter. A character not
    flying one of the curated hulls gets no override here; the skill
    falls back to whatever the rest of the context builder already gave
    it. Always assigns a brand-new dict (see _apply_magic14_framing()'s
    docstring for why that matters when entries can be shared objects)."""
    ship_entry = curated_ships.get(ship.get("type_name"))
    if not ship_entry:
        return context
    ship_label = _ship_display_label(ship)
    rows_by_name = {r["name"]: r for r in rows}
    for skill_name, bonus_lines in ship_entry.get("skill_bonuses", {}).items():
        row = rows_by_name.get(skill_name)
        if not row or row.get("status") == "ok" or not bonus_lines:
            continue
        context[skill_name] = _ship_hull_bonus_entry(
            ship_label, skill_name, bonus_lines, row.get("trained", 0), row.get("target", 5)
        )
    return context


def _mining_context(profile):
    """Real activity-driven scoring context for Mining skill tips — a
    skill's ranking factor now reflects whether the character actually
    mines the resource it boosts (up to +0.5), not just its raw %/level
    bonus. Ice Harvesting/Gas Cloud Harvesting map cleanly onto the real
    Ice/Gas category totals. Deep Core Mining's Mercoxit-specific benefit
    can't be checked reliably — ESI exposes no flag for which ore variant
    triggers that mechanic — so it gets an honest "can't verify" note
    instead of a guessed yes/no, plus real overall ore context. Ore
    Processing skills (gate real mining-crystal items — see
    _ORE_PROCESSING_ORE_TYPES above) get relevance from the character's
    actual mined ore-type mix, not just an aggregate Ore total."""
    days = profile["mining_activity"]["window_days"]
    totals = profile["mining_activity"]["totals"]
    ore_isk = totals.get("Ore", 0) or 0
    ice_isk = totals.get("Ice", 0) or 0
    gas_isk = totals.get("Gas", 0) or 0

    def relevance(isk):
        return min(0.5, isk / 20_000_000) if isk > 0 else 0.0

    def entry(isk, label):
        why = (
            f"You've mined {isk:,.0f} ISK of {label} in the last {days} days — this bonus applies directly to that."
            if isk > 0 else
            f"You haven't mined any {label} in the last {days} days, so this bonus wouldn't currently affect your regular runs."
        )
        return {"factor": 1.0 + relevance(isk), "why": why}

    context = {
        "Ice Harvesting": entry(ice_isk, "Ice"),
        "Gas Cloud Harvesting": entry(gas_isk, "Gas"),
        "Deep Core Mining": {
            "factor": 1.0 + relevance(ore_isk),
            "why": (
                "This specifically reduces risk while mining Mercoxit — there's no reliable way to "
                "check that against your mining history, since the game's data doesn't expose which "
                f"ore variant triggers it. For context, you've mined {ore_isk:,.0f} ISK of ore overall in the last {days} days."
            ),
        },
    }
    general = entry(ore_isk, "ore")
    for name in ("Mining", "Astrogeology", "Mining Upgrades", "Mining Frigate", "Mining Barge", "Exhumers"):
        context[name] = general

    by_type = profile["mining_activity"]["by_type"]
    for skill_name, ore_types in _ORE_PROCESSING_ORE_TYPES.items():
        matched = [r for r in by_type if _base_ore_name(r["name"]) in ore_types]
        matched_isk = sum(r["isk_value"] for r in matched)
        if matched:
            matched_names = ", ".join(sorted({_base_ore_name(r["name"]) for r in matched}))
            why = (
                f"This gates the mining crystals for the ore types it boosts — you've mined "
                f"{matched_isk:,.0f} ISK of {matched_names} in the last {days} days, so this applies "
                f"directly there."
            )
        else:
            why = (
                f"This gates the mining crystals for a specific ore family "
                f"({', '.join(sorted(ore_types))}) — you haven't mined any of those in the last "
                f"{days} days, so it wouldn't help your regular runs right now."
            )
        context[skill_name] = {"factor": 1.0 + relevance(matched_isk), "why": why}

    return context


def _industry_context(profile):
    """Grounds Industry skill tips in this character's actual currently
    active industry jobs (real activity_id per job), real historical
    industry activity from the wallet journal (catches work that isn't
    running right at this moment), and real ore/ice mining totals for the
    reprocessing-yield skills."""
    manufacturing = profile["industry_activity"]["active_manufacturing"]
    research = profile["industry_activity"]["active_research"]
    journal_isk = profile["industry_activity"]["journal_isk"]

    def job_entry(jobs, label):
        factor = 1.0 + (0.4 if jobs else 0.0) + (0.2 if journal_isk else 0.0)
        if jobs:
            names = ", ".join(j["product"] for j in jobs[:3])
            why = f"You currently have {len(jobs)} active {label} job(s) ({names}) — this applies directly there."
        elif journal_isk:
            why = (
                f"No {label} jobs running right now, but your wallet shows {abs(journal_isk):,.0f} ISK worth "
                f"of real industry-related transactions recently — this has applied there before."
            )
        else:
            why = f"You have no active {label} jobs right now."
        return {"factor": factor, "why": why}

    manu_entry = job_entry(manufacturing, "manufacturing")
    research_entry = job_entry(research, "research")
    advanced_entry = {
        "factor": max(manu_entry["factor"], research_entry["factor"]),
        "why": f"{manu_entry['why']} {research_entry['why']}",
    }

    mining_totals = profile["mining_activity"]["totals"]
    days = profile["mining_activity"]["window_days"]
    reprocess_isk = (mining_totals.get("Ore", 0) or 0) + (mining_totals.get("Ice", 0) or 0)
    reprocess_entry = {
        "factor": 1.0 + (0.3 if reprocess_isk > 0 else 0.0),
        "why": (
            f"You've mined {reprocess_isk:,.0f} ISK of ore/ice in the last {days} days that this yield "
            f"bonus would apply to if you reprocess it."
            if reprocess_isk > 0 else
            f"You haven't mined any ore or ice in the last {days} days to reprocess with this bonus."
        ),
    }

    market = profile["market_activity"]
    order_count, undercut_count, isk_at_stake = (
        market["order_count"], market["undercut_count"], market["isk_at_stake"],
    )
    if order_count:
        accounting_entry = {
            "factor": 1.0 + 0.2,
            "why": f"You currently have {order_count} open market order(s) — this cuts the sales tax on every one of them.",
        }
        broker_entry = {
            "factor": 1.0 + 0.2,
            "why": f"You currently have {order_count} open market order(s) — this cuts the listing cost on every one of them.",
        }
    else:
        accounting_entry = {"factor": 1.0, "why": "You have no open market orders right now for this to apply to."}
        broker_entry = {"factor": 1.0, "why": "You have no open market orders right now for this to apply to."}

    if undercut_count:
        advanced_broker_entry = {
            "factor": 1.0 + 0.4,
            "why": (
                f"You have {undercut_count} order(s) currently being undercut/outbid "
                f"({isk_at_stake:,.0f} ISK at stake) — this makes relisting them at a competitive "
                f"price cheaper."
            ),
        }
    else:
        advanced_broker_entry = {
            "factor": 1.0,
            "why": "None of your open orders are currently being undercut, so relisting cost isn't an issue right now.",
        }

    return {
        "Industry": manu_entry,
        "Advanced Industry": advanced_entry,
        "Mass Production": manu_entry,
        "Advanced Mass Production": manu_entry,
        "Laboratory Operation": research_entry,
        "Advanced Laboratory Operation": research_entry,
        "Metallurgy": research_entry,
        "Research": research_entry,
        "Science": research_entry,
        "Reprocessing": reprocess_entry,
        "Reprocessing Efficiency": reprocess_entry,
        "Accounting": accounting_entry,
        "Broker Relations": broker_entry,
        "Advanced Broker Relations": advanced_broker_entry,
    }


def _mission_context(profile):
    """New: Mission Running skills previously got zero personalization at
    all. Grounds them in real mission-reward and NPC-bounty ISK from the
    wallet journal — verified live against this character's real journal
    (ref_type agent_mission_reward/agent_mission_time_bonus_reward for
    missions, bounty_prizes for NPC-kill bounties, a related but distinct
    real signal cited alongside rather than merged in, since it can come
    from plain ratting too, not only missions)."""
    m = profile["mission_activity"]
    days = profile["mining_activity"]["window_days"]  # same dashboard-wide window
    mission_isk, mission_count = m["mission_isk"], m["mission_count"]
    bounty_isk, bounty_count = m["bounty_isk"], m["bounty_count"]

    factor = 1.0 + (0.4 if mission_isk > 0 else 0.0)
    if mission_isk > 0:
        why = (
            f"You've earned {mission_isk:,.0f} ISK from {mission_count} mission reward(s) in the last "
            f"{days} days — this applies directly there."
        )
    else:
        why = f"You haven't turned in any missions in the last {days} days."
    if bounty_isk > 0:
        why += f" You've also earned {bounty_isk:,.0f} ISK from NPC bounties ({bounty_count} entries) in the same window."

    entry = {"factor": factor, "why": why}
    return {name: entry for name in skill_plan.CORE_TARGETS["Mission Running"]}


_PVP_STAT_SKILLS = {
    "Shield Management": ("shield_capacity", "shield capacity", "raise"),
    "Hull Upgrades": ("armor_hp", "armor hit points", "raise"),
    "Shield Operation": ("shield_recharge_ms", "shield recharge time (ms)", "lower"),
}


def _pvp_context(profile, ship, pvp_rows):
    """Real before/after hull-stat numbers (a small registry instead of
    hardcoded branches, so a future stat addition doesn't need new code),
    reduced-fidelity hardpoint-count context (turret/launcher slot COUNT
    on the character's current ship only — no claim about which weapon is
    actually fitted, since there's no reliable "what's fitted right now"
    data source without a much bigger fitting calculator), and real
    zKillboard activity — a character with real recent kills/losses gets
    a higher ranking factor than one who trained the same skills but
    never uses them, and one taking more losses than kills gets a further
    boost toward survivability skills specifically."""
    hull = ship.get("hull_stats") or {}
    ship_label = ship.get("type_name") or "your ship"
    rows_by_name = {r["name"]: r for r in pvp_rows}

    activity = profile["pvp_activity"]
    kills, losses = activity["kills"], activity["losses"]
    window_days = activity["window_days"]
    has_activity = bool(kills or losses)
    activity_boost = (0.3 if has_activity else 0.0) + (0.2 if len(losses) > len(kills) else 0.0)
    if has_activity:
        activity_sentence = (
            f" You've had {len(kills)} kill(s) and {len(losses)} loss(es) on zKillboard in the last "
            f"{window_days} days — this is currently relevant to how you actually play."
        )
    else:
        activity_sentence = (
            f" No kills or losses showed up on zKillboard in the last {window_days} days — "
            "this is ready whenever you use it."
        )

    def stat_line(skill_name, attr_key, unit, verb):
        base = hull.get(attr_key)
        row = rows_by_name.get(skill_name)
        if base is None or not row:
            return None
        levels_remaining = max(0, row.get("target", 0) - row.get("trained", 0))
        if levels_remaining <= 0:
            return None
        match = skill_plan.PCT_PER_LEVEL_RE.search(row.get("description") or "")
        if not match:
            return None
        pct_per_level = float(match.group(1))
        total_pct = pct_per_level * levels_remaining
        trained_roman = skill_plan.ROMAN.get(row["trained"], "?")
        target_roman = skill_plan.ROMAN.get(row["target"], "?")
        new_value = base * (1 + total_pct / 100) if verb == "raise" else base * (1 - total_pct / 100)
        return (
            f"Training from {trained_roman} to {target_roman} would {verb} your {ship_label}'s "
            f"base {unit} from {base:,.0f} to {new_value:,.0f} (base hull stat — fitted modules "
            f"and rigs aren't included)."
        )

    context = {}
    for skill_name, (attr_key, unit, verb) in _PVP_STAT_SKILLS.items():
        line = stat_line(skill_name, attr_key, unit, verb)
        if line:
            context[skill_name] = {"factor": 1.0 + activity_boost, "why": line + activity_sentence}

    turret_count = hull.get("turret_hardpoints")
    launcher_count = hull.get("launcher_hardpoints")
    if turret_count or launcher_count:
        hardpoint_sentence = (
            f"Your {ship_label} has {int(turret_count or 0)} turret and {int(launcher_count or 0)} "
            f"launcher hardpoint(s) — this benefits all of them (hardpoint count only, not which "
            f"weapon is actually fitted)."
        ) + activity_sentence
        for skill_name in ("Signature Analysis", "Target Management", "Long Range Targeting"):
            row = rows_by_name.get(skill_name)
            if not row or row.get("status") == "ok":
                continue
            existing = context.get(skill_name)
            if existing:
                context[skill_name] = {
                    "factor": max(existing["factor"], 1.0 + activity_boost + 0.1),
                    "why": f"{existing['why']} {hardpoint_sentence}",
                }
            else:
                context[skill_name] = {"factor": 1.0 + activity_boost + 0.1, "why": hardpoint_sentence}

    # DED Connections' real bonus is a flat ISK payout per pirate bounty —
    # the same bounty_prizes wallet-journal signal already computed for
    # Mission Running, a new consumer of the same real data.
    bounty = profile["mission_activity"]
    bounty_isk, bounty_count = bounty["bounty_isk"], bounty["bounty_count"]
    ded_row = rows_by_name.get("DED Connections")
    if ded_row and ded_row.get("status") != "ok":
        if bounty_isk > 0:
            why = (
                f"You've earned {bounty_isk:,.0f} ISK from {bounty_count} NPC bounty prize(s) in the "
                f"last {activity['window_days']} days — this bonus pays out on every one of those."
            )
        else:
            why = f"You haven't collected any NPC bounty prizes in the last {activity['window_days']} days."
        context["DED Connections"] = {"factor": 1.0 + (0.3 if bounty_isk > 0 else 0.0), "why": why}

    # Any PVP skill without a specific stat/hardpoint/bounty tie-in still
    # gets an honest why — the real activity signal when there is one,
    # or an honest "no activity yet" note when there isn't (mirrors
    # Mining/Industry, which never leave a skill with no personalized
    # reasoning at all just because the character hasn't done the
    # activity yet). activity_boost is already 0.0 when has_activity is
    # False, so this doesn't change ranking for an inactive character —
    # only fixes the missing `why` text.
    for row in pvp_rows:
        name = row["name"]
        if name not in context:
            context[name] = {"factor": 1.0 + activity_boost, "why": activity_sentence.strip()}

    return context


_SKILLS_TAB_CATEGORIES = ("Quality of Life", "Exploration")


_SKILLS_TAB_GENERIC_WHY = {
    "Quality of Life": (
        "This is a general utility skill that helps regardless of what you're actually flying "
        "or doing right now — it isn't tied to one specific playstyle."
    ),
    "Exploration": (
        "There's no reliable way to check real exploration activity from the game's data (no "
        "scan/hack/relic-site signal is exposed), so this is based on skill investment alone, "
        "not confirmed activity."
    ),
}


def _skills_theme_context(profile):
    """Skills-tab tips (Quality of Life + Exploration core skills) always
    get an honest why, not just when the character's single overall
    dominant training category happens to be one of these two — which is
    rare (Mining/Industry/PVP/Mission Running are far more commonly
    "dominant" by skill ratio for a focused character), and would
    otherwise leave every Quality of Life/Exploration skill with nothing
    but its bare description — the same class of gap found and fixed in
    _pvp_context(). When the dominant category IS one of these two, its
    core skills get the stronger theme-match boost/why instead of the
    generic fallback."""
    dominant = profile["dominant_category"]
    context = {}
    if dominant in _SKILLS_TAB_CATEGORIES:
        info = profile["theme"][dominant]
        pct = info["skill_ratio"] * 100
        if info["confirmed_by_activity"]:
            why = (
                f"Your training leans heavily {dominant} ({pct:.0f}% of core skills at target), and your "
                f"real activity backs it up — this keeps that going."
            )
        else:
            why = (
                f"You've trained heavily into {dominant} ({pct:.0f}% of core skills at target), but no "
                f"matching real activity showed up recently — these skills are ready whenever you use them."
            )
        entry = {"factor": 1.5, "why": why}
        context.update({name: entry for name in skill_plan.CORE_TARGETS[dominant]})

    for category, generic_why in _SKILLS_TAB_GENERIC_WHY.items():
        for name in skill_plan.CORE_TARGETS[category]:
            if name not in context:
                context[name] = {"factor": 1.0, "why": generic_why}

    return context


# CCP's own real, named set — verified live from
# eveonline.com/eve-academy/magic-14 — skills that improve every ship,
# not just one playstyle. Cross-referenced against this project's own
# skill_categories_data.py this session: 2 of the 14 (Capacitor
# Management, Capacitor Systems Operation) weren't core anywhere, and
# Mining tracked none of the 14 at all — both fixed in CORE_TARGETS.
_MAGIC_14 = frozenset({
    "Spaceship Command", "CPU Management", "Power Grid Management", "Capacitor Management",
    "Capacitor Systems Operation", "Mechanics", "Hull Upgrades", "Shield Management",
    "Shield Operation", "Long Range Targeting", "Signature Analysis", "Navigation",
    "Evasive Maneuvering", "Warp Drive Operation",
})

_MAGIC14_SUFFIX = (
    " This is one of the real Magic 14 — CCP's own named set of skills that improve "
    "every ship you fly, not just this one."
)


def _apply_magic14_framing(context):
    """Appends the real Magic 14 framing to any tip for one of those 14
    skills, in whichever category it surfaces — and gives a Magic 14
    skill with no other real signal a plain baseline entry instead of
    silently getting no personalization, since these are universally
    relevant by definition, not conditional on activity or a fit match.

    Several context-builders above intentionally share ONE entry dict
    object across multiple skill names (e.g. _mission_context() gives
    every Mission Running skill the same sentence) — safe as long as
    nothing ever mutates an entry in place. This function (and
    _apply_fit_relevance() below) must respect that: always assign a
    NEW dict to context[name] rather than editing entry["why"]/
    entry["factor"] in place, or a change meant for one Magic-14 skill
    would leak onto every other skill quietly sharing that same object
    (caught live during verification — Diplomacy was getting the Magic
    14 sentence purely because it shares Connections'/Repair Systems'
    entry object in _mission_context())."""
    for name in _MAGIC_14:
        entry = context.get(name, {"factor": 1.0, "why": ""})
        if _MAGIC14_SUFFIX not in entry["why"]:
            context[name] = {"factor": entry["factor"], "why": (entry["why"] + _MAGIC14_SUFFIX).strip()}
    return context


_FIT_RELEVANCE_FACTOR = 1.6


def _apply_fit_relevance(context, profile):
    """Boosts any skill in `context` that's required by a module actually
    fitted on the character's CURRENT ship right now to the strongest
    relevance tier — more concrete than "recently mined this ore" or
    "recent zKillboard activity," since it's directly and currently
    true. Shared by Mining and PVP context builders below. Always
    assigns a new dict to context[skill_name] rather than mutating the
    existing entry in place — see _apply_magic14_framing()'s docstring
    for why that matters here."""
    fit = profile["current_fit"]
    ship_name = fit["ship_name"]
    for module in fit["modules"]:
        for skill_name, _req_level in module["required_skills"]:
            if skill_name not in context:
                continue
            why_addition = (
                f"Your {ship_name} has a {module['name']} fitted right now — this applies directly to it."
            )
            entry = context[skill_name]
            if why_addition in entry["why"]:
                continue
            context[skill_name] = {
                "factor": max(entry["factor"], _FIT_RELEVANCE_FACTOR),
                "why": f"{entry['why']} {why_addition}".strip(),
            }
    return context


def _fit_completeness_tips(fit_data, trained, ship_name, limit=3):
    """New: Skills tab — is the character's CURRENT FIT actually
    maximized? For every real fitted module's required skill trained
    below level 5, a concrete tip citing the real fitted module(s) it
    affects. Not rows from the curated skill_plans lists (this is
    derived straight from real fit data), so these are built directly
    as {"text", "why"} dicts and concatenated onto the Skills tab's
    existing tip list, the same pattern already used for ship-
    eligibility tips on Mining/PVP."""
    by_skill = {}
    for module in fit_data:
        for skill_name, _req_level in module["required_skills"]:
            skill_id = skill_plan.SKILL_TYPE_IDS.get(skill_name)
            if not skill_id:
                continue
            have = trained.get(skill_id, 0)
            if have >= 5:
                continue
            by_skill.setdefault(skill_name, {"trained": have, "modules": []})["modules"].append(module["name"])

    tips = []
    for skill_name, info in sorted(by_skill.items(), key=lambda kv: kv[1]["trained"]):
        modules_str = ", ".join(sorted(set(info["modules"])))
        trained_roman = skill_plan.ROMAN.get(info["trained"], "?")
        tips.append({
            "text": f"{skill_name} is at {trained_roman}/V, and gates or improves what's fitted on your {ship_name} right now.",
            "why": (
                f"Fitted module(s) affected: {modules_str}. Training to V is the real ceiling for "
                f"everything currently fitted that depends on this skill."
            ),
        })
    return tips[:limit]


_TIER_SUFFIX_RE = re.compile(r"\s+(?:I|II|III|IV|V)$")


def _base_item_name(name):
    """Strips a trailing roman-numeral tier suffix (Miner I -> Miner,
    Hobgoblin II -> Hobgoblin) so plain tier variants of the same base
    module compare equal in _same_module_family()."""
    return _TIER_SUFFIX_RE.sub("", name)


def _same_module_family(name_a, name_b):
    """Real, name-grounded "same base module, different tier" check —
    needed because market_group_id ALONE is too coarse to trust for
    upgrade suggestions. Verified live (2026-09-03): ESI's "Mining
    Drones" market group bundles ore-mining drones AND ice-harvesting
    drones together (they share NEITHER a specific purpose nor even
    group_id, just the broad shopping category), and its mining-crystal
    groups bundle every ore-specific crystal Type A-F together — none
    of these are real upgrades of each other despite sharing a market
    group. Two items are the same family if their tier-stripped base
    names are equal, or one's word sequence is a trailing suffix of the
    other's — the latter covers real meta-named variants (e.g.
    "Enduring Multispectrum Shield Hardener", a brand-prefixed T1
    variant of "Multispectrum Shield Hardener II") without matching
    unrelated items that merely share a trailing word by coincidence."""
    a_words = _base_item_name(name_a).split()
    b_words = _base_item_name(name_b).split()
    if not a_words or not b_words:
        return False
    shorter, longer = (a_words, b_words) if len(a_words) <= len(b_words) else (b_words, a_words)
    return longer[-len(shorter):] == shorter


_FIT_UPGRADE_TAB_PRECEDENCE = ("Mining", "Industry", "PVP", "Mission Running")


def _fit_upgrade_tab(required_skills):
    """Which tab a fit-upgrade tip belongs to, based on the upgrade
    module's real required skills — checked against the same curated
    category lists (skill_plan.FULL_SKILL_NAMES) used everywhere else in
    this app, in a fixed precedence order, first match wins. Falls back
    to "Skills" when none of the skills land in any of the four activity
    tabs (e.g. a rig requiring only a generic engineering skill)."""
    skill_names = {name for name, _level in required_skills}
    for category in _FIT_UPGRADE_TAB_PRECEDENCE:
        if skill_names & set(skill_plan.FULL_SKILL_NAMES[category]):
            return category
    return "Skills"


def _fit_upgrade_tips(fit_data, trained, limit_per_tab=2):
    """For each real fitted module (or loaded charge/carried drone — same
    "active gear" scope get_current_fit_data already uses), is there a
    genuinely better variant available (e.g. Miner I -> Miner II), and
    does the character have the skill to fit it? Not a guessed
    stat comparison — uses the real market-group "Variations"
    relationship ESI exposes (the exact same grouping the in-game
    fitting window's own Variations tab shows: Tech I/II/Storyline/
    Faction, verified live 2026-09-03) plus CCP's own tech_level/
    meta_level ranking to find a real upgrade, filtered through
    _same_module_family() (a market group alone isn't a reliable enough
    "same item" signal — see that function's docstring for the real
    false positives this caught during testing), then checks the real
    skill gap against it.

    Each tip is routed to the tab it's actually about via
    _fit_upgrade_tab() — a mining module's upgrade lands on Mining, a
    shield/weapon/drone's on PVP, etc. — rather than every result piling
    onto the Skills tab regardless of subject. Within each tab: "you
    already qualify — go refit" (every required skill is already
    trained, so this costs nothing to act on) ranks above "train X to
    unlock this upgrade" (a real gap remains). Returns {category:
    [tips]} with only non-empty categories present, each independently
    capped at `limit_per_tab`."""
    market_group_ids = set()
    for module in fit_data:
        mgid = _module_type_info(module["type_id"]).get("market_group_id")
        if mgid:
            market_group_ids.add(mgid)
    if not market_group_ids:
        return {}

    sibling_ids = set()
    with ThreadPoolExecutor(max_workers=10) as ex:
        for types in ex.map(_market_group_types, market_group_ids):
            sibling_ids.update(types)
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_module_type_info, sibling_ids))

    seen_pairs = set()
    by_tab_ready = {}
    by_tab_training = {}

    for module in fit_data:
        current = _module_type_info(module["type_id"])
        market_group_id = current.get("market_group_id")
        if not market_group_id:
            continue
        current_rank = (current["tech_level"], current["meta_level"])

        best = None
        best_rank = current_rank
        for sibling_id in _market_group_types(market_group_id):
            if sibling_id == module["type_id"]:
                continue
            sibling = _module_type_info(sibling_id)
            if not sibling["published"]:
                continue
            if not _same_module_family(current["name"], sibling["name"]):
                continue
            rank = (sibling["tech_level"], sibling["meta_level"])
            if rank > best_rank:
                best, best_rank = sibling, rank

        if not best:
            continue
        pair_key = (module["type_id"], best["name"])
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        missing = []
        for skill_name, level in best["required_skills"]:
            skill_id = skill_plan.SKILL_TYPE_IDS.get(skill_name)
            have = trained.get(skill_id, 0) if skill_id else 0
            if have < level:
                missing.append(f"{skill_name} to {skill_plan.ROMAN.get(level, str(level))}")

        why = (
            f"{best['name']} is a real, higher-tier variant of {module['name']} "
            "(the same market-group \"Variations\" relationship the in-game fitting "
            "window shows), ranked by CCP's own tech/meta level, not a guessed stat comparison."
        )
        tab = _fit_upgrade_tab(best["required_skills"])
        if missing:
            by_tab_training.setdefault(tab, []).append({
                "text": f"{module['name']} has a real upgrade: {best['name']} — still need {', '.join(missing)}.",
                "why": why,
            })
        else:
            by_tab_ready.setdefault(tab, []).append({
                "text": (
                    f"You already qualify for {best['name']} — a real upgrade over the "
                    f"{module['name']} currently fitted. Consider refitting."
                ),
                "why": f"{why} Every skill it requires is already trained.",
            })

    return {
        tab: (by_tab_ready.get(tab, []) + by_tab_training.get(tab, []))[:limit_per_tab]
        for tab in set(by_tab_ready) | set(by_tab_training)
    }


_EMPTY_SKILL_PLANS = {cat: [] for cat in ("Mining", "Industry", "PVP", "Quality of Life", "Mission Running", "Exploration")}


def get_dashboard_data(mining_days=7, hours_per_day=None, vault_plex_owned=0):
    """Everything the web dashboard needs, as plain JSON-able data. Every
    section that needs a specific ESI scope is wrapped with _scoped()/
    _scoped_result() so a single missing permission degrades just that
    section (surfaced to the frontend via the "permissions" dict below)
    instead of crashing the whole dashboard. Almost all sections are
    independent of each other, so they're all submitted to one thread
    pool up front and only actually waited on where a real dependency
    requires it (mined_type_ids needs mining_breakdown; skill_groups and
    tips personalization need trained_skills; tips need several other
    sections resolved first) — this is what keeps total wall-clock time
    close to the slowest single section instead of the sum of all of
    them."""
    info = auth.verify_token(auth.get_access_token())
    char_id = info["CharacterID"]

    with ThreadPoolExecutor(max_workers=20) as pool:
        # char_id is resolved once, above, and passed explicitly into every
        # section below rather than letting each one re-derive it via
        # auth.get_character_id() independently — with multi-character
        # support, "the active character" can change between when this
        # request started and when any given thread actually runs (a fetch
        # can take up to 90s), so re-deriving mid-flight could silently mix
        # two different characters' data into one response.
        f_mining_breakdown = pool.submit(get_mining_breakdown_data, char_id, mining_days)
        f_skill_plans = pool.submit(skill_plan.get_all_skill_plans_data, char_id)
        f_corp_overview = pool.submit(corp_overview.get_corp_overview_data, char_id)
        f_ship = pool.submit(get_ship_data, char_id)
        f_active_jobs = pool.submit(get_active_jobs_data, char_id)
        f_market_order_tips = pool.submit(get_market_order_tips, char_id, 1)
        f_wallet = pool.submit(esi.get_wallet_balance, char_id)
        f_net_isk = pool.submit(get_net_isk_data, char_id, mining_days)
        f_location = pool.submit(esi.get_location_data, char_id)
        f_skills_summary = pool.submit(esi.get_skills_summary, char_id)
        f_assets = pool.submit(_fetch_assets, char_id)
        f_blueprints = pool.submit(esi.get_blueprints_data, limit=25, char_id=char_id)
        f_current_training = pool.submit(skill_plan.get_current_training_data, char_id)
        f_mining = pool.submit(get_mining_throughput_data, char_id, mining_days, hours_per_day)
        f_sales = pool.submit(get_sales_throughput_data, char_id, mining_days, hours_per_day)
        f_zkill = pool.submit(zkillboard.get_zkill_data, char_id)
        f_zkill_global = pool.submit(zkillboard.get_global_top_kill)
        f_plex = pool.submit(get_plex_data, char_id, vault_plex_owned)
        f_corporation = pool.submit(get_corporation_data, char_id)
        f_portrait = pool.submit(get_portrait_data_uri, char_id)

        mining_breakdown, mining_breakdown_perm = _scoped_result(
            f_mining_breakdown, "mining",
            {"window_days": mining_days, "category_totals": {"Ore": 0.0, "Ice": 0.0, "Gas": 0.0, "Other": 0.0}, "by_type": []},
        )
        mined_type_ids = [r["type_id"] for r in mining_breakdown["by_type"]]
        f_jita_prices = pool.submit(get_jita_resource_prices, mined_type_ids)

        skill_result, skills_perm = _scoped_result(
            f_skill_plans, "skills", (_EMPTY_SKILL_PLANS, {}, {})
        )
        skill_plans, trained_skills, queued_skills = skill_result
        skill_groups = skill_plan.get_skill_groups_data(trained_skills, queued_skills) if skills_perm["available"] else {}

        corp_overview_data = f_corp_overview.result()
        ship, _ship_perm = _scoped_result(
            f_ship, "ship", {"type_id": None, "type_name": None, "ship_name": None, "hull_stats": {}}
        )
        active_jobs, active_jobs_perm = _scoped_result(f_active_jobs, "active_jobs", [])

        assets_raw, assets_perm = _scoped_result(f_assets, "assets", [])
        assets = get_assets_data(assets_raw)
        fit_data = get_current_fit_data(assets_raw, ship.get("ship_item_id"))

        # Resolved here (earlier than their "natural" position further down)
        # because the multi-factor tip context below needs them — wallet
        # journal activity and zKillboard activity are two of the real
        # signals _build_character_profile() blends together. The futures
        # were already submitted at the top of the pool, so pulling their
        # results forward doesn't change when the fetch itself runs.
        wallet_isk, wallet_perm = _scoped_result(f_wallet, "wallet", 0)
        net_isk, net_isk_perm = _scoped_result(
            f_net_isk, "wallet",
            {"window_days": mining_days, "net_isk": 0, "earned": 0, "spent": 0,
             "by_ref_type": {"mission": {"count": 0, "isk": 0}, "bounty": {"count": 0, "isk": 0},
                              "industry": {"count": 0, "isk": 0}}},
        )
        zkill = f_zkill.result()
        market_order_tips, market_stats = f_market_order_tips.result()

        if skills_perm["available"]:
            profile = _build_character_profile(
                skill_plans, mining_breakdown, active_jobs, ship, zkill, net_isk, market_stats, fit_data
            )
            # Magic 14 framing runs FIRST so its baseline entries exist for
            # _apply_fit_relevance() to find and boost — a Magic 14 skill
            # that a category's own logic doesn't otherwise track (e.g.
            # CPU Management for Mining) still needs to be present before
            # a real fitted-module match against it can be applied.
            mining_context = _apply_ship_hull_bonuses(
                _apply_fit_relevance(_apply_magic14_framing(_mining_context(profile)), profile),
                ship, ship_data.MINING_SHIPS, skill_plans["Mining"],
            )
            industry_context = _industry_context(profile)
            pvp_context = _apply_ship_hull_bonuses(
                _apply_fit_relevance(
                    _apply_magic14_framing(_pvp_context(profile, ship, skill_plans["PVP"])), profile
                ),
                ship, ship_data.PVP_SHIPS, skill_plans["PVP"],
            )
            mission_context = _apply_fit_relevance(_apply_magic14_framing(_mission_context(profile)), profile)
            theme_context = _skills_theme_context(profile)

            mining_skill_tips = skill_plan.rank_skill_tips(
                skill_plans["Mining"], category_label="Mining", context=mining_context
            )
            industry_skill_tips = skill_plan.rank_skill_tips(
                skill_plans["Industry"], category_label="Industry", context=industry_context
            )
            pvp_skill_tips = skill_plan.rank_skill_tips(
                skill_plans["PVP"], category_label="PVP", context=pvp_context
            )
            mission_skill_tips = skill_plan.rank_skill_tips(
                skill_plans["Mission Running"], category_label="Mission Running", context=mission_context
            )
            skills_tab_rows = skill_plans["Quality of Life"] + skill_plans["Exploration"]
            skills_tab_tips = skill_plan.rank_skill_tips(
                skills_tab_rows, category_label="Skills",
                context=_apply_magic14_framing(theme_context),
            )
            fit_completeness_tips = _fit_completeness_tips(
                fit_data, trained_skills, _ship_display_label(ship)
            )
            fit_upgrade_tips = _fit_upgrade_tips(fit_data, trained_skills, limit_per_tab=2)
            mission_running_stats = {
                "mission_isk": profile["mission_activity"]["mission_isk"],
                "mission_count": profile["mission_activity"]["mission_count"],
                "bounty_isk": profile["mission_activity"]["bounty_isk"],
                "bounty_count": profile["mission_activity"]["bounty_count"],
                "window_days": mining_days,
            }
        else:
            unavailable_tip = [{"text": "Skill-based tips aren't available right now.", "why": skills_perm["reason"]}]
            mining_skill_tips = industry_skill_tips = pvp_skill_tips = skills_tab_tips = mission_skill_tips = unavailable_tip
            fit_completeness_tips = []
            fit_upgrade_tips = {}
            mission_running_stats = {
                "mission_isk": 0, "mission_count": 0, "bounty_isk": 0, "bounty_count": 0,
                "window_days": mining_days,
            }

        tips = {
            "Mining": mining_skill_tips + skill_plan.rank_ship_tips(
                trained_skills, ALL_MINING_SHIPS, limit=2, current_stats=ship.get("hull_stats")
            ) + fit_upgrade_tips.get("Mining", []),
            "Industry": industry_skill_tips + market_order_tips + skill_plan.rank_ship_tips(
                trained_skills, ALL_INDUSTRY_SHIPS, limit=1
            ) + fit_upgrade_tips.get("Industry", []),
            "PVP": pvp_skill_tips + skill_plan.rank_ship_tips(
                trained_skills, ALL_PVP_SHIPS, limit=2
            ) + fit_upgrade_tips.get("PVP", []),
            "Mission Running": mission_skill_tips + fit_upgrade_tips.get("Mission Running", []),
            "Skills": skills_tab_tips + fit_completeness_tips + fit_upgrade_tips.get("Skills", []),
            "Corporation": get_corp_tips(corp_overview_data),
        }

        location, location_perm = _scoped_result(f_location, "location", {"system_name": None, "security_status": None})
        skills_summary, skills_summary_perm = _scoped_result(
            f_skills_summary, "skills", {"total_sp": 0, "unallocated_sp": 0, "skills_trained": 0}
        )
        blueprints, blueprints_perm = _scoped_result(f_blueprints, "blueprints", {"total": 0, "rows": []})
        current_training, current_training_perm = _scoped_result(
            f_current_training, "skillqueue", {"training": False, "queue_length": 0}
        )
        mining, mining_perm = _scoped_result(
            f_mining, "mining",
            {"window_days": mining_days, "active_days": 0, "total_isk": 0, "isk_per_active_day": 0,
             "isk_per_hour": None, "hours_per_day": hours_per_day, "daily": []},
        )
        sales, sales_perm = _scoped_result(
            f_sales, "wallet",
            {"window_days": mining_days, "active_days": 0, "total_isk": 0, "isk_per_active_day": 0,
             "isk_per_hour": None, "hours_per_day": hours_per_day, "daily": []},
        )
        jita_prices = f_jita_prices.result()
        zkill_global = f_zkill_global.result()
        plex = f_plex.result()
        corporation = f_corporation.result()
        portrait_data_uri = f_portrait.result()

    # Skills scope failing already means trained_skills is {}, so the
    # skills-summary permission is the more specific/accurate one to show
    # for the Overview skillpoints stat — but a wallet or mining 401 on the
    # very first scoped call of its kind is what determines whether that
    # whole section shows a banner, so combine with "or" per shared scope.
    permissions = {
        "wallet": wallet_perm if not wallet_perm["available"] else net_isk_perm,
        "mission_running": wallet_perm if not wallet_perm["available"] else net_isk_perm,
        "location": location_perm,
        "skills": skills_perm if not skills_perm["available"] else skills_summary_perm,
        "skillqueue": current_training_perm,
        "assets": assets_perm,
        "blueprints": blueprints_perm,
        "active_jobs": active_jobs_perm,
        "mining": mining_perm if not mining_perm["available"] else mining_breakdown_perm,
        "sales": sales_perm,
    }

    return {
        "character": {
            "name": info["CharacterName"],
            "id": char_id,
            "portrait_data_uri": portrait_data_uri,
        },
        "corporation": corporation,
        "corp_overview": corp_overview_data,
        "permissions": permissions,
        "wallet_isk": wallet_isk,
        "net_isk": net_isk,
        "mission_running": mission_running_stats,
        "location": location,
        "ship": ship,
        "skills": skills_summary,
        "assets": assets,
        "blueprints": blueprints,
        "current_training": current_training,
        "skill_plans": skill_plans,
        "skill_groups": skill_groups,
        "tips": tips,
        "active_jobs": active_jobs,
        "mining": mining,
        "mining_breakdown": mining_breakdown,
        "jita_prices": jita_prices,
        "sales": sales,
        "zkill": zkill,
        "zkill_global": zkill_global,
        "plex": plex,
    }


def generate_dashboard(mining_days=7, hours_per_day=None):
    if esi.eve_sso_auth is None:
        print("eve_sso_auth.py not found — put it in the same folder as this script.")
        sys.exit(1)

    info = auth.verify_token(auth.get_access_token())
    char_id = info["CharacterID"]
    banner = "=" * 60
    print(f"\n{banner}\nDASHBOARD — {info['CharacterName']} (character_id {char_id})\n{banner}")

    _header("CURRENT STATE")
    esi.cmd_wallet()
    esi.cmd_location()
    _ship(char_id)
    esi.cmd_skills()

    _header("ASSETS & BLUEPRINTS")
    _assets_summary(char_id)
    print()
    esi.cmd_blueprints()

    _header("PLANS: SKILL QUEUE & MINING GAPS")
    skill_plan.generate_mining_plan()

    _header("PLANS: ACTIVE INDUSTRY JOBS")
    _active_jobs(char_id)

    _header(f"MINING THROUGHPUT (last {mining_days} days)")
    _mining_throughput(char_id, mining_days, hours_per_day)
    print()


if __name__ == "__main__":
    generate_dashboard()
