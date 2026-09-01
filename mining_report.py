#!/usr/bin/env python3
"""
Corp mining ISK report
-----------------------
Pulls the corporation mining ledger from ESI's mining observers
(structures, e.g. a refinery, that have recorded nearby mining
activity), prices it against Jita-region average market prices, and
writes an HTML report plus a console summary.

Requires the esi-industry.read_corporation_mining.v1 scope and an
Accountant- or Director-level corp role. Called via:
    python eve_esi_terminal.py mining-report [--days 30] [--out report.html]

Caveat: ESI's mining observer endpoint reports a running total per
character/ore updated once a day, not a true per-day ledger — the
--days window filters on "last updated within N days", which
approximates recent activity but can include totals accrued before
the window if a character mined continuously.
"""

import sys
import datetime
import requests

import eve_esi_terminal as esi
import eve_sso_auth as auth


def _auth_headers():
    token = auth.get_access_token()
    return {**esi.HEADERS, "Authorization": f"Bearer {token}"}


def _paged_get(path, **params):
    """GET every page of a paginated ESI endpoint."""
    headers = _auth_headers()
    results = []
    page = 1
    while True:
        r = requests.get(f"{esi.ESI}{path}", headers=headers,
                          params={**params, "page": page}, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        results.extend(batch)
        if page >= int(r.headers.get("X-Pages", 1)):
            break
        page += 1
    return results


def _resolve_character_names(character_ids):
    if not character_ids:
        return {}
    resolved = esi.post("/universe/names/", list(character_ids), datasource="tranquility")
    return {r["id"]: r["name"] for r in resolved}


def _ore_prices():
    """type_id -> average market price, from ESI's global price list."""
    prices = esi.get("/markets/prices/", datasource="tranquility")
    return {p["type_id"]: p.get("average_price") or p.get("adjusted_price") or 0 for p in prices}


_VOLUME_CACHE = {}


def get_corp_id():
    """Corporation ID of the logged-in character."""
    return esi.get_corporation_id(auth.get_character_id())


def fetch_observers(corp_id):
    """Structures (e.g. refineries) that have recorded nearby mining activity."""
    return esi._auth_get(f"/corporation/{corp_id}/mining/observers/", datasource="tranquility")


def fetch_observer_ledger(corp_id, observer_id):
    """All ledger rows (character_id, type_id, quantity, last_updated) for one observer."""
    return _paged_get(f"/corporation/{corp_id}/mining/observers/{observer_id}/")


def get_prices():
    return _ore_prices()


def resolve_names(character_ids):
    return _resolve_character_names(character_ids)


def get_volume(type_id):
    """Cubic meters per unit for a type, e.g. an ore or ice type."""
    if type_id not in _VOLUME_CACHE:
        try:
            data = esi.get(f"/universe/types/{type_id}/", datasource="tranquility")
            _VOLUME_CACHE[type_id] = data.get("volume", 0)
        except requests.RequestException:
            _VOLUME_CACHE[type_id] = 0
    return _VOLUME_CACHE[type_id]


def generate_report(days=30, outfile="mining_report.html"):
    if esi.eve_sso_auth is None:
        print("eve_sso_auth.py not found — put it in the same folder as this script.")
        sys.exit(1)

    corp_id = get_corp_id()

    try:
        observers = fetch_observers(corp_id)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 401:
            print("\n401 Unauthorized — your saved login doesn't have the "
                  "esi-industry.read_corporation_mining.v1 scope yet. Re-run:\n"
                  "  python eve_sso_auth.py login\n")
            return
        if status == 403:
            print("\n403 Forbidden — you need the Accountant or Director corp role "
                  "to view mining observer data.\n")
            return
        raise

    if not observers:
        print("\nNo mining observers found for this corporation. Observers are structures "
              "(e.g. a refinery) that have recorded nearby mining activity — if your corp "
              "doesn't own one near where members mine, there's nothing to report.\n")
        return

    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

    entries = []
    for obs in observers:
        obs_id = obs["observer_id"]
        try:
            obs_entries = fetch_observer_ledger(corp_id, obs_id)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                continue
            raise
        entries.extend(row for row in obs_entries if row["last_updated"] >= cutoff)

    if not entries:
        print(f"\nNo mining ledger entries updated in the last {days} days.\n")
        return

    prices = get_prices()
    char_names = resolve_names({e["character_id"] for e in entries})

    totals = {}
    for e in entries:
        key = (e["character_id"], e["type_id"])
        totals[key] = totals.get(key, 0) + e["quantity"]

    per_character = {}
    grand_total_isk = 0.0
    rows = []
    for (cid, type_id), qty in totals.items():
        name = char_names.get(cid, f"character_id {cid}")
        ore = esi.resolve_type_name(type_id)
        price = prices.get(type_id, 0)
        isk = qty * price
        grand_total_isk += isk
        per_character[name] = per_character.get(name, 0) + isk
        rows.append((name, ore, qty, price, isk))

    rows.sort(key=lambda r: -r[4])
    ranked_characters = sorted(per_character.items(), key=lambda kv: -kv[1])

    print(f"\nCorp mining report — updated within last {days} days, {len(rows)} character/ore lines")
    print(f"Estimated total value: {grand_total_isk:,.0f} ISK\n")
    for name, isk in ranked_characters:
        print(f"  {name:<28} {isk:>16,.0f} ISK")

    _write_html(outfile, days, rows, ranked_characters, grand_total_isk)
    print(f"\nFull report written to {outfile}\n")


def _write_html(outfile, days, rows, ranked_characters, grand_total_isk):
    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    body_rows = "\n".join(
        f"<tr><td>{esc(name)}</td><td>{esc(ore)}</td><td>{qty:,}</td>"
        f"<td>{price:,.2f}</td><td>{isk:,.0f}</td></tr>"
        for name, ore, qty, price, isk in rows
    )
    summary_rows = "\n".join(
        f"<tr><td>{esc(name)}</td><td>{isk:,.0f}</td></tr>"
        for name, isk in ranked_characters
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Corp Mining Report</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; background:#05070c; color:#d7e1f2; padding:24px; }}
  h1, h2 {{ color:#ffb000; }}
  table {{ border-collapse: collapse; width:100%; margin-bottom:32px; }}
  th, td {{ border:1px solid #1c2a45; padding:6px 10px; text-align:left; }}
  th {{ background:#0c1220; color:#3ddcff; }}
  tr:nth-child(even) {{ background:#0c1220; }}
</style></head>
<body>
  <h1>Corp Mining Report</h1>
  <p>Updated within last {days} days &middot; estimated total value: <strong>{grand_total_isk:,.0f} ISK</strong></p>
  <h2>By character</h2>
  <table><tr><th>Character</th><th>Est. ISK</th></tr>{summary_rows}</table>
  <h2>By character &amp; ore</h2>
  <table><tr><th>Character</th><th>Ore</th><th>Quantity</th><th>Unit price</th><th>Est. ISK</th></tr>{body_rows}</table>
</body></html>"""
    with open(outfile, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    generate_report()
