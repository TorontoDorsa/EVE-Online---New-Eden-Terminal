#!/usr/bin/env python3
"""
zKillboard integration
-------------------------
Pulls recent kills and losses for a character from zKillboard's public
API (a third-party community site, not CCP's own — no auth needed, but
we're polite about it: descriptive User-Agent, gzip requested, and only
two requests per refresh).

zKillboard's response already embeds the full killmail (attackers,
victim, system, time) alongside its own zkb metadata (ISK value, solo/
NPC flags), so no follow-up ESI killmail call is needed per entry.
"""

from datetime import datetime, timezone

import requests

import eve_esi_terminal as esi

ZKILL_HEADERS = {
    "User-Agent": "new-eden-data-terminal/1.0 (contact: you@example.com)",
    "Accept-Encoding": "gzip",
}


def _fetch(kind, character_id):
    url = f"https://zkillboard.com/api/{kind}/characterID/{character_id}/"
    r = requests.get(url, headers=ZKILL_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def _fetch_global(kind):
    """No character/corp/etc scope — zKillboard's live feed of the most
    recent kills across all of New Eden. EVE's kill volume is high enough
    that this typically only spans ~10-15 minutes of real time, not a
    meaningful 'since last snapshot' window for snapshots taken hours or
    days apart."""
    url = f"https://zkillboard.com/api/{kind}/"
    r = requests.get(url, headers=ZKILL_HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def _summarize(entries, character_id):
    rows = []
    for e in entries:
        victim = e["victim"]
        zkb = e["zkb"]
        final_blow_by_us = any(
            a.get("character_id") == character_id and a.get("final_blow")
            for a in e.get("attackers", [])
        )
        rows.append({
            "killmail_id": e["killmail_id"],
            "date": e["killmail_time"],
            "system_name": esi.resolve_system_name(e["solar_system_id"]),
            "ship_name": esi.resolve_type_name(victim["ship_type_id"]),
            "isk_value": zkb["totalValue"],
            "solo": zkb.get("solo", False),
            "npc": zkb.get("npc", False),
            "final_blow_by_us": final_blow_by_us,
            "attacker_count": len(e.get("attackers", [])),
            "zkill_url": f"https://zkillboard.com/kill/{e['killmail_id']}/",
        })
    return rows


def get_zkill_data(character_id, limit=10):
    """Recent kills and losses for a character, plus the biggest kill/loss
    by ISK value among everything zKillboard returned (not just the most
    recent `limit` shown in the list — a huge kill from a few days back
    shouldn't get missed). Degrades to an 'unavailable' flag rather than
    raising — zKillboard is a third-party site and can be slow or briefly
    down independent of ESI."""
    try:
        kills_raw = _fetch("kills", character_id)
        losses_raw = _fetch("losses", character_id)
    except requests.RequestException as e:
        return {"available": False, "error": str(e), "kills": [], "losses": [],
                "biggest_kill": None, "biggest_loss": None}

    def _top_and_biggest(raw):
        # Only resolve names for what's actually shown: the most recent
        # `limit` entries plus the single biggest by ISK (if it isn't
        # already among them) — mirrors get_global_top_kill()'s fix below
        # for the same "resolve everything just to throw most of it away"
        # cost, which previously applied here too (up to 200 uncached
        # system/ship-name ESI lookups just to keep 10 + 1 of each).
        if not raw:
            return [], None
        shown_count = min(limit, len(raw))
        biggest_idx = max(range(len(raw)), key=lambda i: raw[i]["zkb"]["totalValue"])
        indices = list(range(shown_count))
        if biggest_idx >= shown_count:
            indices.append(biggest_idx)
        resolved = dict(zip(indices, _summarize([raw[i] for i in indices], character_id)))
        return [resolved[i] for i in range(shown_count)], resolved[biggest_idx]

    kills, biggest_kill = _top_and_biggest(kills_raw)
    losses, biggest_loss = _top_and_biggest(losses_raw)

    return {
        "available": True,
        "kills": kills,
        "losses": losses,
        # zKillboard returns at most 200 per request — this is "how many came
        # back", not a lifetime count. Biggest kill/loss is computed over
        # this same returned set, so it's "biggest of the recent ~200", not
        # necessarily an all-time record.
        "recent_kills_count": len(kills_raw),
        "recent_losses_count": len(losses_raw),
        "biggest_kill": biggest_kill,
        "biggest_loss": biggest_loss,
    }


def get_global_top_kill():
    """The single biggest kill across all of New Eden in zKillboard's live
    global feed. There's no separate 'biggest loss' at this scope — every
    killmail has exactly one victim, so the biggest kill server-wide *is*
    the biggest loss server-wide, same event either way."""
    try:
        raw = _fetch_global("kills")
    except requests.RequestException as e:
        return {"available": False, "error": str(e), "biggest": None, "window_minutes": None}

    if not raw:
        return {"available": True, "biggest": None, "window_minutes": None}

    # Find the biggest kill by ISK value from the raw, unresolved feed
    # first, then resolve system/ship names for only that one entry.
    # Resolving every entry via _summarize() before picking the max used
    # to make this call take 40+ seconds — the global feed can hold
    # dozens of entries, each an uncached ESI name-lookup, just to throw
    # away all but one.
    top_raw = max(raw, key=lambda e: e["zkb"]["totalValue"])
    biggest = _summarize([top_raw], character_id=None)[0]

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    times = [datetime.strptime(e["killmail_time"], fmt).replace(tzinfo=timezone.utc) for e in raw]
    window_minutes = round((max(times) - min(times)).total_seconds() / 60)

    return {"available": True, "biggest": biggest, "window_minutes": window_minutes}
