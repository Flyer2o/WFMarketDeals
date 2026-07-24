#!/usr/bin/env python3
"""warframe.market deal notifier.

Polls the public warframe.market v2 API, compares each item's cheapest
in-game sell order against the going rate (median of the next few orders),
and notifies you when someone lists something well below market price.

Usage:
  python3 wfm_deals.py                     # scan the whole market, forever
  python3 wfm_deals.py --watch arcane_energize,octavia_prime_set
  python3 wfm_deals.py --once              # single sweep, then exit
  python3 wfm_deals.py --test-notify       # check notifications work

Stdlib only — no installs needed.
"""

import argparse
import json
import ssl
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# python.org builds on macOS ship without a CA bundle wired into ssl;
# use certifi's bundle when present so HTTPS verification works everywhere.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

API = "https://api.warframe.market/v2"
USER_AGENT = "wfm-deal-notifier/1.0 (personal price-alert tool)"

# ---------------- configuration ----------------
WATCHLIST = []              # item slugs (from the item's warframe.market URL),
                            # e.g. ["arcane_energize", "octavia_prime_set"].
                            # Empty list = sweep the entire market.
ONLY_INGAME_SELLERS = True  # only sellers who are in-game right now (instantly whisperable)
MIN_DISCOUNT = 0.30         # cheapest must be at least 30% below the reference price
MIN_SAVINGS_PLAT = 15       # ...and at least this many plat below it
MIN_REFERENCE_PRICE = 25    # ignore items whose going rate is below this (junk filter)
MIN_BUCKET_ORDERS = 4       # need at least this many comparable orders to judge a price
REQUEST_DELAY = 0.4         # seconds between API calls (~2.5 req/s, polite)
CHEAP_RECHECK_SWEEPS = 6    # full-market mode: re-check known-junk items only every Nth sweep
DISCORD_WEBHOOK_URL = ""    # optional: paste a Discord webhook URL to also get pinged there
STATE_FILE = Path(__file__).with_name("seen_deals.json")
MAX_SEEN = 5000
# ------------------------------------------------


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("  rate limited by API, backing off 30s...")
            time.sleep(30)
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def fetch_items():
    data = get_json(f"{API}/items")
    if not data:
        sys.exit("Could not fetch item list from warframe.market — check your connection.")
    return {it["slug"]: it["i18n"]["en"]["name"] for it in data["data"]}


def find_deals(slug, name, cfg):
    """Return (deals, best_reference) for one item, or (None, None) on fetch failure."""
    data = get_json(f"{API}/orders/item/{slug}")
    if data is None:
        return None, None
    sells = [
        o for o in data["data"]
        if o["type"] == "sell" and o.get("visible", True)
        and (o["user"]["status"] == "ingame"
             or (not cfg.only_ingame and o["user"]["status"] == "online"))
    ]
    # Same item can trade at very different prices per rank/subtype (e.g. rank 0
    # vs rank 5 arcane, intact vs radiant relic) — compare like with like.
    buckets = {}
    for o in sells:
        buckets.setdefault((o.get("rank"), o.get("subtype")), []).append(o)

    deals, best_reference = [], 0
    for (rank, subtype), orders in buckets.items():
        if len(orders) < MIN_BUCKET_ORDERS:
            continue
        orders.sort(key=lambda o: o["platinum"])
        cheapest = orders[0]
        reference = statistics.median(o["platinum"] for o in orders[1:6])
        best_reference = max(best_reference, reference)
        savings = reference - cheapest["platinum"]
        if (reference >= cfg.min_price
                and savings >= cfg.min_savings
                and cheapest["platinum"] <= reference * (1 - cfg.min_discount)):
            deals.append({
                "key": f'{cheapest["id"]}:{cheapest["platinum"]}',
                "name": name,
                "rank": rank,
                "subtype": subtype,
                "price": cheapest["platinum"],
                "reference": reference,
                "savings": savings,
                "seller": cheapest["user"]["ingameName"],
                "status": cheapest["user"]["status"],
            })
    return deals, best_reference


def applescript_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(deal):
    variant = ""
    if deal["rank"] is not None:
        variant = f' (rank {deal["rank"]})'
    elif deal["subtype"]:
        variant = f' ({deal["subtype"]})'
    pct = round(100 * deal["savings"] / deal["reference"])
    title = f'{deal["name"]}{variant}: {deal["price"]}p (market ~{deal["reference"]}p, -{pct}%)'
    whisper = (f'/w {deal["seller"]} Hi! I want to buy: "{deal["name"]}{variant}" '
               f'for {deal["price"]} platinum. (warframe.market)')

    print(f'\n*** DEAL *** {title}')
    print(f'    seller {deal["seller"]} is {deal["status"]} — paste in-game:')
    print(f'    {whisper}')

    if sys.platform == "darwin":
        script = (f'display notification "{applescript_escape("seller: " + deal["seller"])}" '
                  f'with title "WFM deal" subtitle "{applescript_escape(title)}" sound name "Glass"')
        subprocess.run(["osascript", "-e", script], capture_output=True)
    else:
        print("\a", end="", flush=True)  # terminal bell

    if DISCORD_WEBHOOK_URL:
        body = json.dumps({"content": f'**{title}**\n`{whisper}`'}).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL, data=body,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT).read()
        except (urllib.error.URLError, TimeoutError):
            print("    (Discord webhook failed)")


def load_seen():
    try:
        return dict(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    while len(seen) > MAX_SEEN:
        seen.pop(next(iter(seen)))
    STATE_FILE.write_text(json.dumps(seen))


def main():
    p = argparse.ArgumentParser(description="warframe.market deal notifier")
    p.add_argument("--watch", help="comma-separated item slugs (overrides WATCHLIST)")
    p.add_argument("--once", action="store_true", help="single sweep, then exit")
    p.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    p.add_argument("--min-discount", type=float, default=MIN_DISCOUNT)
    p.add_argument("--min-savings", type=int, default=MIN_SAVINGS_PLAT)
    p.add_argument("--min-price", type=int, default=MIN_REFERENCE_PRICE)
    cfg = p.parse_args()
    cfg.only_ingame = ONLY_INGAME_SELLERS

    if cfg.test_notify:
        notify({"key": "test", "name": "Test Item", "rank": 5, "subtype": None,
                "price": 42, "reference": 100, "savings": 58,
                "seller": "SomeTenno", "status": "ingame"})
        return

    items = fetch_items()
    watch = [s.strip() for s in cfg.watch.split(",")] if cfg.watch else WATCHLIST
    for slug in watch:
        if slug not in items:
            sys.exit(f'Unknown item slug "{slug}" — use the last part of the item\'s '
                     f'warframe.market URL, e.g. arcane_energize')
    targets = watch or sorted(items)
    mode = f"watchlist ({len(targets)} items)" if watch else f"full market ({len(targets)} items)"
    print(f"Scanning {mode}, alert when ≥{cfg.min_savings}p and ≥{round(cfg.min_discount*100)}% "
          f"below market (going rate ≥{cfg.min_price}p). Ctrl-C to stop.")

    seen = load_seen()
    skip_until = {}  # slug -> sweep number when we re-check a known-junk item
    sweep = 0
    while True:
        sweep += 1
        found = scanned = 0
        for i, slug in enumerate(targets, 1):
            if not watch and skip_until.get(slug, 0) > sweep:
                continue
            deals, best_reference = find_deals(slug, items[slug], cfg)
            time.sleep(REQUEST_DELAY)
            if deals is None:
                continue
            scanned += 1
            if not watch and best_reference < cfg.min_price:
                skip_until[slug] = sweep + CHEAP_RECHECK_SWEEPS
            for d in deals:
                if d["key"] in seen:
                    continue
                seen[d["key"]] = int(time.time())
                notify(d)
                save_seen(seen)
                found += 1
            if not watch and i % 250 == 0:
                print(f"  ...{i}/{len(targets)} items, {found} deals so far")
        print(f"Sweep {sweep} done: {scanned} items checked, {found} new deals.")
        if cfg.once:
            break
        if watch:
            time.sleep(30)  # small watchlist: pause between sweeps


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
