#!/usr/bin/env python3
"""
Corporation overview
---------------------
Public + role-gated corp/alliance data: member count, home base,
war status, and any upcoming structure fuel obligations.

Most of the "structure payments" side genuinely isn't visible to a
rank-and-file member — ESI gates the corp structures list behind both
the esi-corporations.read_structures.v1 scope and Director-level corp
roles. Where that's the case we say so instead of guessing. Modern EVE
also has no corp member cap, so "room until full" isn't a real number
ESI (or the game) tracks.

Requires: esi-location scopes already in DEFAULT_SCOPES for character
lookup. Everything else here is either a public endpoint or degrades
to an "unavailable" flag with a reason.
"""

import sys

import requests

import eve_esi_terminal as esi
import eve_sso_auth as auth


def get_corp_membership_data(corp):
    return {
        "name": corp["name"],
        "ticker": corp["ticker"],
        "member_count": corp["member_count"],
        "tax_rate": corp["tax_rate"],
        "date_founded": corp.get("date_founded"),
        "war_eligible": corp.get("war_eligible", False),
    }


def get_corp_home_data(corp):
    station_id = corp.get("home_station_id")
    if not station_id:
        return {"available": False}
    try:
        station = esi.get(f"/universe/stations/{station_id}/", datasource="tranquility")
        return {
            "available": True,
            "name": station["name"],
            "system_name": esi.resolve_system_name(station["system_id"]),
        }
    except requests.RequestException:
        # Home could be a player-owned citadel (/universe/structures/{id}/
        # requires docking-access auth we may not have) rather than an
        # NPC station — degrade rather than crash either way.
        return {"available": False}


def get_alliance_data(corp):
    alliance_id = corp.get("alliance_id")
    if not alliance_id:
        return None
    try:
        alliance = esi.get(f"/alliances/{alliance_id}/", datasource="tranquility")
        return {"name": alliance["name"], "ticker": alliance["ticker"]}
    except requests.RequestException:
        return None


def get_corp_wars_data(corp):
    """ESI has no endpoint that lists wars for a given corporation or
    alliance — only a global recent-wars feed (/wars) you'd have to walk
    war-by-war looking for a participant match, which isn't practical for
    a snapshot. war_eligible on the public corp record is the one
    reliable war-related signal ESI actually exposes."""
    return {"war_eligible": corp.get("war_eligible", False)}


def get_corp_structures_data(corp_id):
    """Upwell structure list (name, system, fuel_expires) — the source for
    'upcoming structure payments'. Needs esi-corporations.read_structures.v1
    (not in this app's authorized scopes) and Director-level corp roles on
    top of that, which most members don't hold."""
    try:
        structures = esi._auth_get(f"/corporations/{corp_id}/structures/")
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        scope = "esi-corporations.read_structures.v1"
        if code == 401 and scope in auth.DEFAULT_SCOPES:
            reason = f"requires the {scope} scope, which isn't authorized on this login — log in again to grant it"
            fixable = True
        elif code == 401:
            reason = (
                f"requires the {scope} scope, which this app doesn't currently request during "
                f"login — logging in again won't grant it; the app itself needs to be updated to "
                f"ask for that scope first"
            )
            fixable = False
        elif code == 403:
            reason = "requires Director-level corporation roles — logging in again won't help, since this depends on your character's corp role, not the login itself"
            fixable = False
        else:
            reason = f"ESI returned HTTP {code}"
            fixable = False
        return {"available": False, "reason": reason, "fixable_by_login": fixable, "structures": []}
    except requests.RequestException as e:
        return {"available": False, "reason": str(e), "fixable_by_login": False, "structures": []}

    rows = []
    for s in structures:
        type_id = s.get("type_id")
        system_id = s.get("system_id")
        rows.append({
            "structure_id": s["structure_id"],
            "name": esi.resolve_type_name(type_id) if type_id else "Unknown structure",
            "system_name": esi.resolve_system_name(system_id) if system_id else None,
            "state": s.get("state"),
            "fuel_expires": s.get("fuel_expires"),
        })
    return {"available": True, "reason": None, "fixable_by_login": False, "structures": rows}


def get_corp_overview_data(char_id=None):
    if esi.eve_sso_auth is None:
        print("eve_sso_auth.py not found — put it in the same folder as this script.")
        sys.exit(1)

    char_id = char_id or auth.get_character_id()
    corp_id = esi.get_corporation_id(char_id)
    corp = esi.get_corporation(corp_id)

    return {
        "membership": get_corp_membership_data(corp),
        "home": get_corp_home_data(corp),
        "alliance": get_alliance_data(corp),
        "wars": get_corp_wars_data(corp),
        "structures": get_corp_structures_data(corp_id),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_corp_overview_data(), indent=2))
