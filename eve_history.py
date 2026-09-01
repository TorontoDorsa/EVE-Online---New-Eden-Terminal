#!/usr/bin/env python3
"""
Eve History Tracker
----------------------
A local SQLite database that accumulates ESI data over time:
market prices, CCP's official daily price history, and corp mining
ledger entries.

Why this exists: ESI's own mining ledger only covers ~30 days, and
the live market order-book endpoint only gives you a current snapshot.
This logs snapshots on a schedule and also backfills CCP's own daily
aggregated price history (up to ~1 year) so you don't have to wait
for your own snapshots to accumulate.

Requires: pip install requests   (sqlite3 is in the Python standard library)

Usage:
    python eve_history.py track-market "Tritanium" --region "The Forge"
    python eve_history.py backfill-market "Tritanium" --region "The Forge"
    python eve_history.py track-mining --days 30

    python eve_history.py history market "Tritanium" --region "The Forge" --days 30
    python eve_history.py history daily "Tritanium" --region "The Forge" --days 30
    python eve_history.py history mining --days 30

Run the track-* commands on a schedule (cron / Task Scheduler) to build
up history. See the bottom of this file for example schedules.
"""

import os
import sys
import sqlite3
import argparse
import datetime

import eve_esi_terminal as esi

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eve_history.db")


# ---- schema ----

def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS market_history (
        timestamp TEXT NOT NULL,
        region_id INTEGER,
        region_name TEXT,
        type_id INTEGER,
        type_name TEXT,
        best_buy REAL,
        best_sell REAL,
        order_count INTEGER
    );

    CREATE TABLE IF NOT EXISTS daily_price_history (
        date TEXT NOT NULL,
        region_id INTEGER NOT NULL,
        region_name TEXT,
        type_id INTEGER NOT NULL,
        type_name TEXT,
        average REAL,
        highest REAL,
        lowest REAL,
        order_count INTEGER,
        volume INTEGER,
        UNIQUE(date, region_id, type_id)
    );

    CREATE TABLE IF NOT EXISTS mining_history (
        date TEXT NOT NULL,
        corp_id INTEGER NOT NULL,
        character_id INTEGER NOT NULL,
        character_name TEXT,
        type_id INTEGER NOT NULL,
        type_name TEXT,
        quantity INTEGER,
        isk_value REAL,
        volume_m3 REAL,
        logged_at TEXT,
        UNIQUE(date, corp_id, character_id, type_id)
    );

    CREATE INDEX IF NOT EXISTS idx_market_lookup ON market_history (type_id, region_id, timestamp);
    CREATE INDEX IF NOT EXISTS idx_mining_lookup ON mining_history (corp_id, date);
    """)
    conn.commit()
    conn.close()


# ---- logging ----

def log_market(item_name, region_name):
    init_db()
    region_id = esi.REGIONS.get(region_name.lower())
    if region_id is None:
        print(f"Unknown region '{region_name}'. Options: {', '.join(r.title() for r in esi.REGIONS)}")
        return

    resolved = esi.resolve_ids([item_name])
    matches = resolved.get("inventory_types", [])
    if not matches:
        print(f"No item type found matching '{item_name}'.")
        return
    type_id, type_name = matches[0]["id"], matches[0]["name"]

    orders = esi.get(f"/markets/{region_id}/orders/", datasource="tranquility",
                      order_type="all", type_id=type_id)
    buys = [o["price"] for o in orders if o["is_buy_order"]]
    sells = [o["price"] for o in orders if not o["is_buy_order"]]
    best_buy = max(buys) if buys else None
    best_sell = min(sells) if sells else None

    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    conn = _connect()
    conn.execute("""INSERT INTO market_history
                     (timestamp, region_id, region_name, type_id, type_name, best_buy, best_sell, order_count)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (now, region_id, region_name.title(), type_id, type_name, best_buy, best_sell, len(orders)))
    conn.commit()
    conn.close()
    print(f"Logged {type_name} in {region_name.title()}: buy {best_buy}, sell {best_sell} ({len(orders)} orders)")


def log_mining(days=30):
    init_db()
    import mining_report as mr  # reuse observer/ledger fetch logic

    corp_id = mr.get_corp_id()
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

    try:
        observers = mr.fetch_observers(corp_id)
    except mr.requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 401:
            print("401 Unauthorized — your saved login doesn't have the "
                  "esi-industry.read_corporation_mining.v1 scope yet. Re-run:\n"
                  "  python eve_sso_auth.py login")
            return
        if status == 403:
            print("403 Forbidden — you need the Accountant or Director corp role "
                  "to view mining observer data.")
            return
        raise

    if not observers:
        print("No mining observers found for this corp.")
        return

    all_entries = []
    for obs in observers:
        try:
            entries = mr.fetch_observer_ledger(corp_id, obs["observer_id"])
        except mr.requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                continue
            raise
        all_entries.extend(e for e in entries if e["last_updated"] >= cutoff)

    if not all_entries:
        print(f"No mining ledger entries found in the last {days} days.")
        return

    prices = mr.get_prices()
    char_ids = {e["character_id"] for e in all_entries}
    names = mr.resolve_names(char_ids)

    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    conn = _connect()
    inserted = 0
    for e in all_entries:
        type_id = e["type_id"]
        qty = e["quantity"]
        price = prices.get(type_id, 0)
        isk_value = qty * price
        volume = qty * mr.get_volume(type_id)
        type_name = esi.resolve_type_name(type_id)
        cur = conn.execute("""INSERT OR REPLACE INTO mining_history
                     (date, corp_id, character_id, character_name, type_id, type_name, quantity, isk_value, volume_m3, logged_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (e["last_updated"], corp_id, e["character_id"], names.get(e["character_id"], ""),
                      type_id, type_name, qty, isk_value, volume, now))
        inserted += cur.rowcount
    conn.commit()
    conn.close()
    print(f"Logged {len(all_entries)} mining ledger entries ({inserted} new/updated rows).")


# ---- querying ----

def backfill_market_history(item_name, region_name):
    """Pulls CCP's own daily aggregated price history (avg/high/low/volume,
    up to ~1 year back) — no need to wait for our own snapshots to accumulate."""
    init_db()
    region_id = esi.REGIONS.get(region_name.lower())
    if region_id is None:
        print(f"Unknown region '{region_name}'. Options: {', '.join(r.title() for r in esi.REGIONS)}")
        return

    resolved = esi.resolve_ids([item_name])
    matches = resolved.get("inventory_types", [])
    if not matches:
        print(f"No item type found matching '{item_name}'.")
        return
    type_id, type_name = matches[0]["id"], matches[0]["name"]

    history = esi.get(f"/markets/{region_id}/history/", datasource="tranquility", type_id=type_id)
    if not history:
        print(f"No history returned for {type_name} in {region_name.title()}.")
        return

    conn = _connect()
    inserted = 0
    for row in history:
        cur = conn.execute("""INSERT OR REPLACE INTO daily_price_history
                     (date, region_id, region_name, type_id, type_name, average, highest, lowest, order_count, volume)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (row["date"], region_id, region_name.title(), type_id, type_name,
                      row["average"], row["highest"], row["lowest"], row["order_count"], row["volume"]))
        inserted += cur.rowcount
    conn.commit()
    conn.close()
    print(f"Backfilled {len(history)} days of history for {type_name} in {region_name.title()} "
          f"({inserted} new/updated rows).")


def show_daily_history(item_name, region_name, days=30):
    init_db()
    region_id = esi.REGIONS.get(region_name.lower())
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = _connect()
    rows = conn.execute("""SELECT * FROM daily_price_history
                            WHERE type_name = ? AND region_id = ? AND date >= ?
                            ORDER BY date""",
                         (item_name, region_id, since)).fetchall()
    conn.close()
    if not rows:
        print(f"No daily history for '{item_name}' in {region_name.title()}. "
              f"Run 'backfill-market \"{item_name}\" --region \"{region_name}\"' first.")
        return
    avgs = [r["average"] for r in rows]
    print(f"\n{item_name} — {region_name.title()} — {len(rows)} days")
    print(f"  Price range: {min(r['lowest'] for r in rows):,.2f} — {max(r['highest'] for r in rows):,.2f} ISK")
    print(f"  Avg of daily averages: {sum(avgs)/len(avgs):,.2f} ISK\n")
    for r in rows[-20:]:
        print(f"  {r['date']}  avg {r['average']:>14,.2f}  low {r['lowest']:>14,.2f}  "
              f"high {r['highest']:>14,.2f}  vol {r['volume']:>14,}")
    if len(rows) > 20:
        print(f"  ... showing last 20 of {len(rows)}")
    print()


def show_market_history(item_name, region_name, days=30):
    init_db()
    conn = _connect()
    region_id = esi.REGIONS.get(region_name.lower())
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    rows = conn.execute("""SELECT * FROM market_history
                            WHERE type_name = ? AND region_id = ? AND timestamp >= ?
                            ORDER BY timestamp""",
                         (item_name, region_id, since)).fetchall()
    conn.close()
    if not rows:
        print(f"No market history for '{item_name}' in {region_name.title()}. "
              f"Run 'track-market \"{item_name}\" --region \"{region_name}\"' on a schedule to build it up.")
        return
    sells = [r["best_sell"] for r in rows if r["best_sell"] is not None]
    print(f"\n{item_name} — {region_name.title()} — {len(rows)} snapshots over {days} days")
    if sells:
        print(f"  Sell price range: {min(sells):,.2f} — {max(sells):,.2f} ISK")
    print()
    for r in rows[-20:]:
        print(f"  {r['timestamp']}  buy {r['best_buy']}  sell {r['best_sell']}  ({r['order_count']} orders)")
    if len(rows) > 20:
        print(f"  ... showing last 20 of {len(rows)}")
    print()


def show_mining_history(days=30):
    init_db()
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    conn = _connect()
    rows = conn.execute("""SELECT character_name, SUM(isk_value) as total_isk, SUM(volume_m3) as total_vol
                            FROM mining_history WHERE date >= ?
                            GROUP BY character_id ORDER BY total_isk DESC""",
                         (since,)).fetchall()
    conn.close()
    if not rows:
        print(f"No mining history in the last {days} days. Run 'track-mining' on a schedule to build it up.")
        return
    total = sum(r["total_isk"] for r in rows)
    print(f"\nMining history — last {days} days — {fmt_b(total)} total across {len(rows)} miner(s)\n")
    for r in rows[:25]:
        print(f"  {r['character_name']:<28} {fmt_b(r['total_isk']):>12}  {r['total_vol']:>14,.0f} m³")
    print()


def fmt_b(value):
    return f"{value / 1e9:.3f}B"


def main():
    parser = argparse.ArgumentParser(description="Eve History Tracker — local SQLite history for ESI data")
    sub = parser.add_subparsers(dest="command")

    p_tm = sub.add_parser("track-market", help="Log a live order-book snapshot for an item")
    p_tm.add_argument("item")
    p_tm.add_argument("--region", default="The Forge")

    p_bf = sub.add_parser("backfill-market", help="Pull CCP's official daily price history for an item (up to ~1 year)")
    p_bf.add_argument("item")
    p_bf.add_argument("--region", default="The Forge")

    p_tmi = sub.add_parser("track-mining", help="Log corp mining ledger entries (requires login + corp role)")
    p_tmi.add_argument("--days", type=int, default=30)

    p_hist = sub.add_parser("history", help="Query stored history")
    hist_sub = p_hist.add_subparsers(dest="history_type")

    p_hm = hist_sub.add_parser("market")
    p_hm.add_argument("item")
    p_hm.add_argument("--region", default="The Forge")
    p_hm.add_argument("--days", type=int, default=30)

    p_hd = hist_sub.add_parser("daily")
    p_hd.add_argument("item")
    p_hd.add_argument("--region", default="The Forge")
    p_hd.add_argument("--days", type=int, default=30)

    p_hmi = hist_sub.add_parser("mining")
    p_hmi.add_argument("--days", type=int, default=30)

    args = parser.parse_args()

    if args.command == "track-market":
        log_market(args.item, args.region)
    elif args.command == "backfill-market":
        backfill_market_history(args.item, args.region)
    elif args.command == "track-mining":
        log_mining(days=args.days)
    elif args.command == "history":
        if args.history_type == "market":
            show_market_history(args.item, args.region, days=args.days)
        elif args.history_type == "daily":
            show_daily_history(args.item, args.region, days=args.days)
        elif args.history_type == "mining":
            show_mining_history(days=args.days)
        else:
            p_hist.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Example schedules to build history automatically:
#
# Linux/macOS (crontab -e):
#   0 * * * *  cd /path/to/project && python3 eve_history.py track-market "Tritanium" --region "The Forge"
#   0 6 * * *  cd /path/to/project && python3 eve_history.py track-mining --days 2
#
# Windows Task Scheduler:
#   Program: python.exe
#   Arguments: eve_history.py track-market "Tritanium" --region "The Forge"
#   Start in: C:\path\to\project
#   Trigger: repeat every 1 hour
# ---------------------------------------------------------------------------
