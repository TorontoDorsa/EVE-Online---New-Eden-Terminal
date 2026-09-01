#!/usr/bin/env python3
"""
New Eden Terminal (CLI edition)
-------------------------------------
Pulls live public data from EVE Online's ESI API.
No authentication required — public endpoints only.

Requires: pip install requests

Usage:
    python eve_esi_terminal.py status
    python eve_esi_terminal.py market "Tritanium" --region "The Forge"
    python eve_esi_terminal.py system "Jita"

Run with no arguments for an interactive menu.
"""

import sys
import argparse
import requests
import truststore

# Validates TLS certs against the OS trust store instead of the bundled
# certifi CA bundle — needed on machines where antivirus/corporate software
# (e.g. Avast) re-signs HTTPS traffic with its own locally-trusted root cert.
# Unlike pip-system-certs (a site-packages .pth hook, which a PyInstaller
# frozen build never executes), this is called explicitly here so it works
# identically whether run from source or as the packaged .exe.
truststore.inject_into_ssl()

try:
    import eve_sso_auth
except ImportError:
    eve_sso_auth = None  # authenticated commands will explain if this is missing

ESI = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "new-eden-data-terminal/1.0 (contact: you@example.com)"}

REGIONS = {
    "the forge": 10000002,      # Jita
    "domain": 10000043,         # Amarr
    "sinq laison": 10000032,    # Dodixie
    "metropolis": 10000042,     # Hek
    "heimatar": 10000030,       # Rens
}

_TYPE_NAME_CACHE = {}
_SYSTEM_NAME_CACHE = {}


def resolve_type_name(type_id):
    if type_id not in _TYPE_NAME_CACHE:
        try:
            data = get(f"/universe/types/{type_id}/", datasource="tranquility")
            _TYPE_NAME_CACHE[type_id] = data["name"]
        except requests.RequestException:
            _TYPE_NAME_CACHE[type_id] = f"type_id {type_id}"
    return _TYPE_NAME_CACHE[type_id]


def resolve_system_name(system_id):
    if system_id not in _SYSTEM_NAME_CACHE:
        try:
            data = get(f"/universe/systems/{system_id}/", datasource="tranquility")
            _SYSTEM_NAME_CACHE[system_id] = data["name"]
        except requests.RequestException:
            _SYSTEM_NAME_CACHE[system_id] = f"system_id {system_id}"
    return _SYSTEM_NAME_CACHE[system_id]


def get(path, **params):
    r = requests.get(f"{ESI}{path}", headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def post(path, body, **params):
    r = requests.post(f"{ESI}{path}", headers=HEADERS, params=params, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def resolve_ids(names):
    return post("/universe/ids/", names, datasource="tranquility", language="en")


def cmd_status():
    data = get("/status/", datasource="tranquility")
    print(f"\nTRANQUILITY STATUS")
    print(f"  Players online : {data['players']:,}")
    print(f"  Server build   : {data['server_version']}")
    print(f"  Last restart   : {data['start_time']}\n")


def cmd_market(item_name, region_name):
    region_id = REGIONS.get(region_name.lower())
    if region_id is None:
        print(f"Unknown region '{region_name}'. Options: {', '.join(r.title() for r in REGIONS)}")
        return

    resolved = resolve_ids([item_name])
    matches = resolved.get("inventory_types", [])
    if not matches:
        print(f"No item type found matching '{item_name}'.")
        return
    type_id = matches[0]["id"]
    type_name = matches[0]["name"]

    orders = get(f"/markets/{region_id}/orders/", datasource="tranquility",
                 order_type="all", type_id=type_id)

    buys = sorted([o for o in orders if o["is_buy_order"]], key=lambda o: -o["price"])[:5]
    sells = sorted([o for o in orders if not o["is_buy_order"]], key=lambda o: o["price"])[:5]

    print(f"\n{type_name} (type_id {type_id}) — {region_name.title()} — {len(orders)} active orders\n")
    print("  Top buy orders:")
    for o in buys:
        print(f"    {o['price']:>14,.2f} ISK  x{o['volume_remain']:<10,} loc {o['location_id']}")
    print("  Top sell orders:")
    for o in sells:
        print(f"    {o['price']:>14,.2f} ISK  x{o['volume_remain']:<10,} loc {o['location_id']}")
    print()


def cmd_system(system_name):
    resolved = resolve_ids([system_name])
    matches = resolved.get("systems", [])
    if not matches:
        print(f"No solar system found matching '{system_name}'.")
        return
    sys_id = matches[0]["id"]

    sys_data = get(f"/universe/systems/{sys_id}/", datasource="tranquility")
    print(f"\n{sys_data['name']} (system_id {sys_id})")
    print(f"  Security status  : {sys_data['security_status']:.2f}")
    print(f"  Constellation ID : {sys_data.get('constellation_id')}")
    print(f"  Planets          : {len(sys_data.get('planets', []))}")
    print(f"  Stargates        : {len(sys_data.get('stargates', []))}\n")


def _auth_get(path, **params):
    """GET an authenticated ESI endpoint using the saved SSO token."""
    if eve_sso_auth is None:
        print("eve_sso_auth.py not found — put it in the same folder as this script.")
        sys.exit(1)
    token = eve_sso_auth.get_access_token()
    headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    r = requests.get(f"{ESI}{path}", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def cmd_whoami():
    if eve_sso_auth is None:
        print("eve_sso_auth.py not found — put it in the same folder as this script.")
        sys.exit(1)
    token = eve_sso_auth.get_access_token()
    info = eve_sso_auth.verify_token(token)
    print(f"\nLogged in as {info['CharacterName']} (character_id {info['CharacterID']})\n")


def get_wallet_balance():
    char_id = eve_sso_auth.get_character_id()
    return _auth_get(f"/characters/{char_id}/wallet/", datasource="tranquility")


def cmd_wallet():
    print(f"\nWallet balance: {get_wallet_balance():,.2f} ISK\n")


def get_skills_summary():
    char_id = eve_sso_auth.get_character_id()
    data = _auth_get(f"/characters/{char_id}/skills/", datasource="tranquility")
    return {
        "total_sp": data["total_sp"],
        "unallocated_sp": data.get("unallocated_sp", 0),
        "skills_trained": len(data["skills"]),
    }


def cmd_skills():
    data = get_skills_summary()
    print(f"\nTotal SP: {data['total_sp']:,}")
    if data["unallocated_sp"]:
        print(f"Unallocated SP: {data['unallocated_sp']:,}")
    print(f"Skills trained: {data['skills_trained']}\n")


def get_location_data():
    char_id = eve_sso_auth.get_character_id()
    loc = _auth_get(f"/characters/{char_id}/location/", datasource="tranquility")
    sys_data = get(f"/universe/systems/{loc['solar_system_id']}/", datasource="tranquility")
    return {"system_name": sys_data["name"], "security_status": sys_data["security_status"]}


def cmd_location():
    loc = get_location_data()
    print(f"\nCurrent system: {loc['system_name']} (security {loc['security_status']:.2f})\n")


def get_corporation_id(char_id):
    """Public endpoint — no auth needed to look up a character's corp."""
    data = get(f"/characters/{char_id}/", datasource="tranquility")
    return data["corporation_id"]


# ---- Industry & mining ----

def cmd_jobs():
    char_id = eve_sso_auth.get_character_id()
    jobs = _auth_get(f"/characters/{char_id}/industry/jobs/", datasource="tranquility")
    if not jobs:
        print("\nNo industry jobs (active or recent).\n")
        return
    print(f"\nIndustry jobs ({len(jobs)}):")
    for j in jobs:
        product = resolve_type_name(j.get("product_type_id") or j["blueprint_type_id"])
        print(f"  [{j['status']:<9}] {product} — activity {j['activity_id']} — ends {j['end_date']}")
    print()


def cmd_corp_jobs():
    char_id = eve_sso_auth.get_character_id()
    corp_id = get_corporation_id(char_id)
    jobs = _auth_get(f"/corporations/{corp_id}/industry/jobs/", datasource="tranquility")
    if not jobs:
        print("\nNo corporation industry jobs found.\n")
        return
    print(f"\nCorporation industry jobs ({len(jobs)}):")
    for j in jobs:
        product = resolve_type_name(j.get("product_type_id") or j["blueprint_type_id"])
        print(f"  [{j['status']:<9}] {product} — activity {j['activity_id']} — ends {j['end_date']}")
    print()


def cmd_mining():
    char_id = eve_sso_auth.get_character_id()
    entries = _auth_get(f"/characters/{char_id}/mining/", datasource="tranquility")
    if not entries:
        print("\nNo mining ledger entries in the last 30 days.\n")
        return
    print(f"\nMining ledger ({len(entries)} entries, last 30 days):")
    for e in entries[:20]:
        ore = resolve_type_name(e["type_id"])
        system = resolve_system_name(e["solar_system_id"])
        print(f"  {e['date']}  {ore:<28} x{e['quantity']:<10,} @ {system}")
    if len(entries) > 20:
        print(f"  … and {len(entries) - 20} more")
    print()


def get_blueprints_data(limit=25):
    char_id = eve_sso_auth.get_character_id()
    bps = _auth_get(f"/characters/{char_id}/blueprints/", datasource="tranquility")
    rows = []
    for b in bps[:limit]:
        rows.append({
            "name": resolve_type_name(b["type_id"]),
            "kind": "BPO" if b["quantity"] == -1 else "BPC",
            "material_efficiency": b["material_efficiency"],
            "time_efficiency": b["time_efficiency"],
            "runs": b.get("runs", -1),
        })
    return {"total": len(bps), "rows": rows}


def cmd_blueprints():
    data = get_blueprints_data(limit=25)
    if not data["rows"]:
        print("\nNo blueprints found.\n")
        return
    print(f"\nBlueprints ({data['total']}):")
    for b in data["rows"]:
        print(f"  [{b['kind']}] {b['name']} — ME {b['material_efficiency']} / TE {b['time_efficiency']} — runs {b['runs'] if b['runs'] != -1 else '-'}")
    if data["total"] > 25:
        print(f"  … and {data['total'] - 25} more")
    print()


def cmd_corp_blueprints():
    char_id = eve_sso_auth.get_character_id()
    corp_id = get_corporation_id(char_id)
    bps = _auth_get(f"/corporations/{corp_id}/blueprints/", datasource="tranquility")
    if not bps:
        print("\nNo corporation blueprints found.\n")
        return
    print(f"\nCorporation blueprints ({len(bps)}):")
    for b in bps[:25]:
        name = resolve_type_name(b["type_id"])
        kind = "BPO" if b["quantity"] == -1 else "BPC"
        print(f"  [{kind}] {name} — ME {b['material_efficiency']} / TE {b['time_efficiency']} — runs {b.get('runs', '-')}")
    if len(bps) > 25:
        print(f"  … and {len(bps) - 25} more")
    print()


# ---- Market orders ----

def cmd_orders():
    char_id = eve_sso_auth.get_character_id()
    orders = _auth_get(f"/characters/{char_id}/orders/", datasource="tranquility")
    if not orders:
        print("\nNo open market orders.\n")
        return
    print(f"\nOpen orders ({len(orders)}):")
    for o in orders:
        name = resolve_type_name(o["type_id"])
        kind = "BUY " if o["is_buy_order"] else "SELL"
        print(f"  [{kind}] {name:<28} {o['price']:>14,.2f} ISK  x{o['volume_remain']}/{o['volume_total']}")
    print()


def cmd_corp_orders():
    char_id = eve_sso_auth.get_character_id()
    corp_id = get_corporation_id(char_id)
    orders = _auth_get(f"/corporations/{corp_id}/orders/", datasource="tranquility")
    if not orders:
        print("\nNo open corporation market orders.\n")
        return
    print(f"\nCorporation open orders ({len(orders)}):")
    for o in orders:
        name = resolve_type_name(o["type_id"])
        kind = "BUY " if o["is_buy_order"] else "SELL"
        print(f"  [{kind}] {name:<28} {o['price']:>14,.2f} ISK  x{o['volume_remain']}/{o['volume_total']}")
    print()


def cmd_structure_market(structure_id):
    orders = _auth_get(f"/markets/structures/{structure_id}/", datasource="tranquility")
    if not orders:
        print(f"\nNo orders found in structure {structure_id} (or you lack docking/market access there).\n")
        return
    print(f"\nStructure {structure_id} — {len(orders)} active orders (showing 20):")
    for o in orders[:20]:
        name = resolve_type_name(o["type_id"])
        kind = "BUY " if o["is_buy_order"] else "SELL"
        print(f"  [{kind}] {name:<28} {o['price']:>14,.2f} ISK  x{o['volume_remain']}")
    print()


def interactive_menu():
    while True:
        print("\n== New Eden Terminal ==")
        print("1) Server status")
        print("2) Market orders")
        print("3) System lookup")
        print("q) Quit")
        choice = input("> ").strip().lower()
        try:
            if choice == "1":
                cmd_status()
            elif choice == "2":
                item = input("Item name: ").strip()
                region = input(f"Region ({', '.join(r.title() for r in REGIONS)}): ").strip() or "The Forge"
                cmd_market(item, region)
            elif choice == "3":
                sysname = input("System name: ").strip()
                cmd_system(sysname)
            elif choice == "q":
                break
        except requests.RequestException as e:
            print(f"ESI request failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="New Eden Terminal — live EVE Online ESI CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show Tranquility server status")

    p_market = sub.add_parser("market", help="Show top market orders for an item")
    p_market.add_argument("item", help="Item name, e.g. Tritanium")
    p_market.add_argument("--region", default="The Forge", help="Region name (default: The Forge)")

    p_system = sub.add_parser("system", help="Show solar system info")
    p_system.add_argument("system", help="Solar system name, e.g. Jita")

    sub.add_parser("whoami", help="Show which character is currently logged in")
    sub.add_parser("wallet", help="Show character wallet balance (requires login)")
    sub.add_parser("skills", help="Show character skill totals (requires login)")
    sub.add_parser("location", help="Show character's current solar system (requires login)")

    sub.add_parser("jobs", help="Show character industry jobs (requires login)")
    sub.add_parser("corp-jobs", help="Show corporation industry jobs (requires login + corp role)")
    sub.add_parser("mining", help="Show character mining ledger, last 30 days (requires login)")
    sub.add_parser("blueprints", help="Show character blueprints (requires login)")
    sub.add_parser("corp-blueprints", help="Show corporation blueprints (requires login + corp role)")

    sub.add_parser("orders", help="Show character open market orders (requires login)")
    sub.add_parser("corp-orders", help="Show corporation open market orders (requires login + corp role)")
    p_struct = sub.add_parser("structure-market", help="Show market orders in a player structure (requires login + docking access)")
    p_struct.add_argument("structure_id", type=int, help="Structure ID, e.g. from in-game or a killmail")

    p_mining_report = sub.add_parser("mining-report", help="Generate a live corp mining ISK report (requires login + corp role)")
    p_mining_report.add_argument("--days", type=int, default=30, help="Lookback window in days (default 30)")
    p_mining_report.add_argument("--out", default="mining_report.html", help="Output HTML file")

    sub.add_parser("mining-skills", help="Show mining skill gap plan for the logged-in character (requires login)")

    p_dash = sub.add_parser("dashboard", help="Combined current-state-and-plans view for the logged-in character (requires login)")
    p_dash.add_argument("--mining-days", type=int, default=7, help="Lookback window for mining throughput (default 7)")
    p_dash.add_argument("--hours-per-day", type=float, default=None, help="Assumed active hours per mining day, for an ISK/hour estimate")

    args = parser.parse_args()

    try:
        if args.command == "status":
            cmd_status()
        elif args.command == "market":
            cmd_market(args.item, args.region)
        elif args.command == "system":
            cmd_system(args.system)
        elif args.command == "whoami":
            cmd_whoami()
        elif args.command == "wallet":
            cmd_wallet()
        elif args.command == "skills":
            cmd_skills()
        elif args.command == "location":
            cmd_location()
        elif args.command == "jobs":
            cmd_jobs()
        elif args.command == "corp-jobs":
            cmd_corp_jobs()
        elif args.command == "mining":
            cmd_mining()
        elif args.command == "blueprints":
            cmd_blueprints()
        elif args.command == "corp-blueprints":
            cmd_corp_blueprints()
        elif args.command == "orders":
            cmd_orders()
        elif args.command == "corp-orders":
            cmd_corp_orders()
        elif args.command == "structure-market":
            cmd_structure_market(args.structure_id)
        elif args.command == "mining-report":
            import mining_report
            mining_report.generate_report(days=args.days, outfile=args.out)
        elif args.command == "mining-skills":
            import skill_plan
            skill_plan.generate_mining_plan()
        elif args.command == "dashboard":
            import dashboard
            dashboard.generate_dashboard(mining_days=args.mining_days, hours_per_day=args.hours_per_day)
        else:
            interactive_menu()
    except requests.RequestException as e:
        print(f"ESI request failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
