#!/usr/bin/env python3
"""warframe.market deal scanner with database, history and web UI.

Run it, then open http://127.0.0.1:8737 in your browser:
  python3 wfm_app.py            (macOS/Linux)
  python  wfm_app.py            (Windows)

- Scans the whole market continuously; watchlist items get a fast lane.
- SQLite database (wfm_market.db) keeps a 30-day price history per item
  variant; the item catalog auto-refreshes every 24 h.
- Detects three kinds of opportunities:
    snipe - one listing far below the current order book
    flip  - a buy order paying more than the cheapest sell listing
    dip   - an item trading well below its own 7-day average
- Web UI: live feed, price charts, item search, watchlist and settings.

Stdlib only. certifi is used for HTTPS trust if installed (needed on
python.org macOS builds).
"""

import argparse
import calendar
import json
import os
import queue
import re
import signal
import sqlite3
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

API = "https://api.warframe.market/v2"
USER_AGENT = "wfm-deal-notifier/2.0 (personal price-alert tool)"
APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "wfm_market.db"
UI_PATH = APP_DIR / "ui.html"

HISTORY_DAYS = 30          # snapshot retention
ITEMS_REFRESH_S = 24 * 3600
JUNK_RECHECK_S = 12 * 3600  # how often to re-examine items below min price
MIN_BUCKET_ORDERS = 4
SNAPSHOT_MIN_GAP_S = 600   # per-bucket downsampling for the history table
SET_FRESH_MIN = 20         # a set margin older than this needs re-verifying
DUCAT_FRESH_MIN = 60       # ditto for ducat-value ratios

DEFAULT_CONFIG = {
    "min_discount": 0.30,    # snipe: below reference by this fraction...
    "min_savings": 15,       # ...and by this many plat
    "min_price": 25,         # ignore items whose going rate is below this
    "only_ingame": False,    # True = in-game sellers only; False = in-game + online
                             # (offline sellers are never considered either way)
    "fast_interval": 60,     # seconds between watchlist re-checks
    "request_delay": 0.4,    # seconds between API calls
    "flip_min_profit": 15,   # flip: buy order pays this much over sell floor
    "jackpot_discount": 0.60,  # jackpot: this far below market (a "steal")...
    "jackpot_gap": 0.50,     # ...OR this far below even the 2nd-cheapest listing
    "mute_all": False,       # master switch for every notification channel
    "notify_snipes": True,
    "notify_jackpots": True,
    "notify_flips": True,
    "notify_targets": True,  # watchlist price-target hits
    "notify_baro": True,     # Baro arrives / is about to leave
    "notify_twitch": True,   # twitch.tv/warframe goes live (drops!)
    "notify_dips": False,    # dips always show in the UI; toast only if on
    "discord_webhook": "",
}

# sane bounds for numeric settings; protects against typos in the UI
CONFIG_LIMITS = {
    "min_discount": (0.01, 0.95), "jackpot_discount": (0.05, 0.95),
    "jackpot_gap": (0.05, 0.95), "min_savings": (0, 100000),
    "min_price": (0, 100000), "flip_min_profit": (1, 100000),
    "fast_interval": (10, 3600), "request_delay": (0.25, 5.0),
}

# Windows consoles can be cp1252; never crash on printing item names.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


# ---------------- database ----------------

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init():
    with db_connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS items(
            slug TEXT PRIMARY KEY, name TEXT NOT NULL,
            junk_until INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS snapshots(
            id INTEGER PRIMARY KEY, slug TEXT NOT NULL,
            rank INTEGER, subtype TEXT, ts INTEGER NOT NULL,
            low INTEGER, reference REAL, buy_high INTEGER, sellers INTEGER);
        CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(slug, ts);
        CREATE TABLE IF NOT EXISTS deals(
            id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT NOT NULL,
            slug TEXT NOT NULL, name TEXT NOT NULL, rank INTEGER, subtype TEXT,
            price INTEGER NOT NULL, reference REAL NOT NULL, pct INTEGER NOT NULL,
            seller TEXT, status TEXT, note TEXT, order_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS watchlist(slug TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS set_parts(
            set_slug TEXT NOT NULL, part_slug TEXT NOT NULL,
            qty INTEGER NOT NULL, PRIMARY KEY(set_slug, part_slug));
        """)
        for migration in (  # add columns introduced after the first release
                "ALTER TABLE items ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE deals ADD COLUMN status TEXT",
                "ALTER TABLE items ADD COLUMN wfm_id TEXT",
                "ALTER TABLE deals ADD COLUMN gone_ts INTEGER",
                "ALTER TABLE deals ADD COLUMN checked_ts INTEGER",
                "ALTER TABLE items ADD COLUMN ducats INTEGER",
                "ALTER TABLE watchlist ADD COLUMN target INTEGER",
                # revision counter: lets the UI fetch only what changed
                "ALTER TABLE deals ADD COLUMN rev INTEGER"):
            try:
                c.execute(migration)
            except sqlite3.OperationalError:
                pass
        c.executescript("""
        CREATE INDEX IF NOT EXISTS idx_deals_slug ON deals(slug, gone_ts);
        CREATE INDEX IF NOT EXISTS idx_deals_live ON deals(gone_ts, kind, checked_ts);
        CREATE INDEX IF NOT EXISTS idx_deals_rev ON deals(rev);
        CREATE INDEX IF NOT EXISTS idx_deals_ts ON deals(ts);
        """)
        c.execute("UPDATE deals SET rev = id WHERE rev IS NULL")


def item_category(tags_json):
    # "mod" must outrank "prime": WFM tags augments for prime weapons and
    # for abilities like Molecular Prime with a stray "prime" tag
    tags = set(json.loads(tags_json or "[]"))
    for tag, category in (("arcane_enhancement", "arcane"), ("relic", "relic"),
                          ("mod", "mod"), ("prime", "prime"),
                          ("weapon", "weapon")):
        if tag in tags:
            return category
    return "other"


class Config:
    """Thread-safe config, persisted in the DB, editable from the UI."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = dict(DEFAULT_CONFIG)
        with db_connect() as c:
            for row in c.execute("SELECT key, value FROM config"):
                if row["key"] in self._values:
                    self._values[row["key"]] = json.loads(row["value"])

    def snapshot(self):
        with self._lock:
            return dict(self._values)

    def update(self, changes):
        with self._lock:
            conn = db_connect()
            try:
                for key, value in changes.items():
                    if key not in DEFAULT_CONFIG:
                        continue
                    # coerce to the default's type so the UI can send strings
                    default = DEFAULT_CONFIG[key]
                    if isinstance(default, bool):
                        value = value in (True, "true", "on", 1, "1")
                    elif isinstance(default, float):
                        value = float(value)
                    elif isinstance(default, int):
                        value = int(float(value))
                    else:
                        value = str(value)
                    if key in CONFIG_LIMITS and not isinstance(value, str):
                        lo, hi = CONFIG_LIMITS[key]
                        value = max(lo, min(hi, value))
                    self._values[key] = value
                    conn.execute("INSERT OR REPLACE INTO config VALUES(?,?)",
                                 (key, json.dumps(value)))
                conn.commit()
            finally:
                conn.close()


# ---------------- warframe.market client ----------------

THROTTLE_COUNT = {"n": 0}  # 429s received; surfaced in /api/status


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            THROTTLE_COUNT["n"] += 1
            print(f"API throttled us (429 #{THROTTLE_COUNT['n']}) — backing off 8s")
            time.sleep(8)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


def analyze_orders(payload, only_ingame):
    """Group visible sell/buy orders into (rank, subtype) buckets.

    Sellers may be in-game or online (config); buy orders only count when the
    buyer is in-game — you can't sell to someone who isn't in the game, and
    inflated buy orders from absent users would fake flip opportunities.
    """
    sell_statuses = ("ingame",) if only_ingame else ("ingame", "online")
    buckets = {}
    for o in payload["data"]:
        if not o.get("visible", True):
            continue
        status = o["user"]["status"]
        key = (o.get("rank"), o.get("subtype"))
        if o["type"] == "sell" and status in sell_statuses:
            buckets.setdefault(key, {"sells": [], "buy_high": None})[
                "sells"].append(o)
        elif o["type"] == "buy" and status == "ingame":
            b = buckets.setdefault(key, {"sells": [], "buy_high": None})
            if b["buy_high"] is None or o["platinum"] > b["buy_high"]:
                b["buy_high"] = o["platinum"]
    for b in buckets.values():
        b["sells"].sort(key=lambda o: o["platinum"])
        sells = b["sells"]
        b["reference"] = (statistics.median(o["platinum"] for o in sells[1:6])
                          if len(sells) >= MIN_BUCKET_ORDERS else None)
    return buckets


# ---------------- Baro Ki'Teer schedule (void trader) ----------------

BARO_CACHE = {"ts": 0.0, "data": None, "fail_ts": 0.0}
RELAYS = {  # DE worldstate node codes -> relay names
    "EarthHUB": "Strata Relay (Earth)", "VenusHUB": "Vesper Relay (Venus)",
    "MercuryHUB": "Larunda Relay (Mercury)", "SaturnHUB": "Kronia Relay (Saturn)",
    "PlutoHUB": "Orcus Relay (Pluto)", "EuropaHUB": "Leonov Relay (Europa)",
    "ErisHUB": "Kuiper Relay (Eris)",
}


def _baro_from_mirror():
    d = get_json("https://api.warframestat.us/pc/voidTrader")
    if d and d.get("activation"):
        inv = [{"name": i.get("item") or "?", "unique": i.get("uniqueName", ""),
                "ducats": i.get("ducats"), "credits": i.get("credits")}
               for i in (d.get("inventory") or [])]
        return {"activation": d["activation"], "expiry": d.get("expiry"),
                "location": d.get("location", "a relay"), "inventory": inv}
    return None


def _camel_words(text):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).replace("_", " ").strip()


def _baro_from_de():
    """First-party fallback: DE's official worldstate."""
    d = get_json("https://api.warframe.com/cdn/worldState.php")
    try:
        vt = d["VoidTraders"][0]

        def iso(field):
            ms = int(vt[field]["$date"]["$numberLong"])
            return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms / 1000))

        inv = [{"name": _camel_words(m.get("ItemType", "").rsplit("/", 1)[-1]),
                "unique": m.get("ItemType", ""), "ducats": m.get("PrimePrice"),
                "credits": m.get("RegularPrice")}
               for m in (vt.get("Manifest") or [])]
        return {"activation": iso("Activation"), "expiry": iso("Expiry"),
                "location": RELAYS.get(vt.get("Node"), vt.get("Node", "a relay")),
                "inventory": inv}
    except (TypeError, KeyError, IndexError, ValueError):
        return None


# Baro's faction-damage mods are named by weapon class in game but by a generic
# internal path in the feed: /Mods/<Class>/.../PrimedWeaponFactionDamage<Faction>
FACTION_VERB = {"Rifle": "Bane Of", "Shotgun": "Cleanse",
                "Pistol": "Expel", "Melee": "Smite"}
FACTION_NAME = {"murmurs": "The Murmur", "murmur": "The Murmur",
                "grineer": "Grineer", "corpus": "Corpus",
                "infested": "Infested", "corrupted": "Orokin",
                "orokin": "Orokin"}


def match_baro_item(name, unique, by_norm):
    """Map a Baro ware to a warframe.market slug. Returns (slug, subtype).

    Most wares are cosmetics with no market listing at all — returning None is
    the correct, honest answer there rather than guessing a similar name."""
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    if not name:
        return None, None
    # relics arrive as "Axi A5 Relic (Intact)" — the parenthetical is the subtype
    subtype = None
    m = re.match(r"^(.*?)\s*\((\w+)\)\s*$", name)
    if m:
        name, subtype = m.group(1), m.group(2).lower()
    for candidate in (name, name + " Set"):
        slug = by_norm.get(norm(candidate))
        if slug:
            return slug, subtype
    # faction-damage mods: rebuild the in-game name from the path
    fm = re.search(r"/Mods/(\w+)/.*?WeaponFactionDamage(\w+)$", unique or "")
    if fm and fm.group(1) in FACTION_VERB:
        faction = FACTION_NAME.get(fm.group(2).lower())
        if faction:
            built = f"Primed {FACTION_VERB[fm.group(1)]} {faction}"
            slug = by_norm.get(norm(built))
            if slug:
                return slug, None
    return None, None


def baro_wares(conn):
    """Baro's current stock joined with our market data: what each ware costs
    in ducats, what it trades for, and whether it's underpriced right now."""
    data = baro_status()
    if not data or not data.get("inventory"):
        return {"schedule": data, "wares": []}
    catalog = list(conn.execute("SELECT name, slug FROM items"))
    by_norm = {re.sub(r"[^a-z0-9]", "", r["name"].lower()): r["slug"]
               for r in catalog}
    slug_name = {r["slug"]: r["name"] for r in catalog}
    deals = {}
    for r in conn.execute(
            "SELECT slug, kind, price, pct, seller, status FROM deals "
            "WHERE gone_ts IS NULL ORDER BY price"):
        deals.setdefault(r["slug"], dict(r))
    wares = []
    for item in data["inventory"]:
        slug, subtype = match_baro_item(item["name"], item["unique"], by_norm)
        # the feed's name for some wares is an internal string
        # ("Primed Weapon Faction Damage Murmurs") — prefer the market's name
        display = slug_name.get(slug) or item["name"]
        if subtype:
            display += f" ({subtype})"
        row = {"name": display, "ducats": item["ducats"],
               "credits": item["credits"], "slug": slug, "subtype": subtype,
               "low": None, "reference": None, "per100": None,
               "sellers": None, "age_min": None, "deal": deals.get(slug)}
        if slug:
            # Baro sells mods unranked, so rank 0 is the comparable listing
            snap = conn.execute(
                "SELECT low, reference, sellers, ts FROM snapshots WHERE slug=? "
                "AND (subtype IS ? OR ? IS NULL) ORDER BY "
                "CASE WHEN rank=0 THEN 0 WHEN rank IS NULL THEN 1 ELSE 2 END, "
                "ts DESC LIMIT 1", (slug, subtype, subtype)).fetchone()
            if snap and snap["low"]:
                row.update(low=snap["low"], reference=snap["reference"],
                           sellers=snap["sellers"],
                           age_min=round((time.time() - snap["ts"]) / 60))
                if item["ducats"]:
                    row["per100"] = round(snap["low"] / item["ducats"] * 100, 1)
        wares.append(row)
    # best plat-per-ducat first; unpriced and untradeable wares sink to the end
    wares.sort(key=lambda w: (-(w["per100"] or -1), -(w["ducats"] or 0)))
    return {"schedule": data, "wares": wares}


# ---------------- analytics ----------------

def build_stats(conn):
    """Aggregates for the dashboard: what the scanner has actually caught."""
    now = int(time.time())
    day = now - 86400

    def scalar(sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return (row[0] if row else None) or 0

    by_hour = [0] * 24
    for r in conn.execute(
            "SELECT ts FROM deals WHERE ts > ?", (day,)):
        idx = int((now - r["ts"]) // 3600)
        if 0 <= idx < 24:
            by_hour[23 - idx] += 1

    kinds = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) AS n FROM deals WHERE ts > ? GROUP BY kind", (day,))}
    cats = {}
    for r in conn.execute(
            "SELECT i.tags AS tags, COUNT(*) AS n FROM deals d "
            "LEFT JOIN items i ON i.slug = d.slug WHERE d.ts > ? "
            "GROUP BY i.tags", (day,)):
        cats[item_category(r["tags"])] = cats.get(item_category(r["tags"]), 0) + r["n"]

    lifetimes = [r["life"] for r in conn.execute(
        "SELECT (gone_ts - ts) AS life FROM deals WHERE gone_ts IS NOT NULL "
        "AND ts > ? ORDER BY life", (now - 7 * 86400,))]
    best = [dict(r) for r in conn.execute(
        "SELECT name, kind, price, reference, pct, ts, slug FROM deals "
        "WHERE ts > ? AND kind IN ('jackpot','snipe') ORDER BY pct DESC LIMIT 8",
        (day,))]
    busiest = [dict(r) for r in conn.execute(
        "SELECT name, slug, COUNT(*) AS n FROM deals WHERE ts > ? "
        "GROUP BY slug ORDER BY n DESC LIMIT 8", (day,))]

    covered = scalar("SELECT COUNT(DISTINCT slug) FROM snapshots WHERE ts > ?",
                     (now - 6 * 3600,))
    return {
        "deals_24h": scalar("SELECT COUNT(*) FROM deals WHERE ts > ?", (day,)),
        "active": scalar("SELECT COUNT(*) FROM deals WHERE gone_ts IS NULL"),
        "by_hour": by_hour,
        "kinds": kinds,
        "categories": cats,
        "median_life_min": round(lifetimes[len(lifetimes) // 2] / 60) if lifetimes else None,
        "best": best,
        "busiest": busiest,
        "items_scanned_6h": covered,
        "catalog": scalar("SELECT COUNT(*) FROM items"),
        "snapshots": scalar("SELECT COUNT(*) FROM snapshots"),
        "history_days": round(
            (now - scalar("SELECT MIN(ts) FROM snapshots")) / 86400, 1)
            if scalar("SELECT COUNT(*) FROM snapshots") else 0,
        "throttled": THROTTLE_COUNT["n"],
    }


# ---------------- Warframe Twitch channel ----------------

TWITCH_CACHE = {"ts": 0.0, "data": None}
TWITCH_GQL_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # public web-player client id


def _twitch_from_gql():
    body = json.dumps([{"operationName": "WfmLive", "variables": {}, "query":
        'query WfmLive { user(login: "warframe") { displayName stream '
        '{ id createdAt viewersCount title game { displayName } } } }'}]).encode()
    req = urllib.request.Request("https://gql.twitch.tv/gql", data=body, headers={
        "Client-ID": TWITCH_GQL_ID, "Content-Type": "application/json",
        "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as resp:
            payload = json.load(resp)
        user = payload[0]["data"]["user"]
        stream = user.get("stream")
        if not stream:
            return {"live": False}
        return {"live": True, "title": stream.get("title") or "",
                "game": (stream.get("game") or {}).get("displayName", ""),
                "viewers": stream.get("viewersCount"),
                "started": stream.get("createdAt")}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            KeyError, IndexError, TypeError):
        return None


def _twitch_from_decapi():
    """Fallback: a public uptime endpoint that needs no credentials."""
    req = urllib.request.Request("https://decapi.me/twitch/uptime/warframe",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as resp:
            text = resp.read().decode("utf-8", "replace").strip()
        return {"live": "offline" not in text.lower(), "title": "",
                "game": "", "viewers": None, "uptime": text}
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def twitch_status():
    """Is twitch.tv/warframe live? Cached 90 s — cheap enough to poll."""
    now = time.time()
    if TWITCH_CACHE["data"] and now - TWITCH_CACHE["ts"] <= 90:
        return TWITCH_CACHE["data"]
    data = _twitch_from_gql()
    if data is None:
        data = _twitch_from_decapi()
    if data is not None:
        TWITCH_CACHE.update(data=data, ts=now)
    return TWITCH_CACHE["data"]


REV_LOCK = threading.Lock()


def next_rev(conn):
    """Monotonic revision stamp so clients can sync only what changed."""
    with REV_LOCK:
        row = conn.execute("SELECT COALESCE(MAX(rev), 0) + 1 AS n FROM deals").fetchone()
        return row["n"]


def flag_get(conn, key, default=None):
    """Small persisted key/value for cross-restart state (not user settings)."""
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    try:
        return json.loads(row["value"]) if row else default
    except (TypeError, ValueError):
        return default


def flag_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO config VALUES(?,?)",
                 (key, json.dumps(value)))
    conn.commit()


def iso_to_epoch(text):
    """Parse the worldstate's ISO timestamps (always UTC) to epoch seconds."""
    try:
        return calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


def baro_status():
    """Void-trader schedule: community mirror, then DE's own feed.
    Cached 30 min; on outages, back off 2 min and keep serving stale data."""
    now = time.time()
    if BARO_CACHE["data"] and now - BARO_CACHE["ts"] <= 1800:
        return BARO_CACHE["data"]
    if now - BARO_CACHE["fail_ts"] < 120:
        return BARO_CACHE["data"]  # possibly stale — better than a dead banner
    data = _baro_from_mirror() or _baro_from_de()
    if data:
        BARO_CACHE.update(data=data, ts=now)
    else:
        BARO_CACHE["fail_ts"] = now
    return BARO_CACHE["data"]


# ---------------- server-sent events (instant UI updates) ----------------

SSE_CLIENTS = []
SSE_LOCK = threading.Lock()


def sse_push(obj):
    """Notify every connected browser immediately (new deal / expired deal)."""
    data = json.dumps(obj)
    with SSE_LOCK:
        clients = list(SSE_CLIENTS)
    for q in clients:
        try:
            q.put_nowait(data)
        except queue.Full:
            pass


# ---------------- notifications ----------------

def applescript_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify_desktop(title, body, sound="Glass"):
    if sys.platform == "darwin":
        script = (f'display notification "{applescript_escape(body)}" '
                  f'with title "WFM deals" subtitle "{applescript_escape(title)}" '
                  f'sound name "{sound}"')
        subprocess.run(["osascript", "-e", script], capture_output=True)
    elif sys.platform == "win32":
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass
    # rich cross-platform notifications come from the browser UI


def notify_discord(webhook, text):
    if not webhook:
        return
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(webhook, data=body, headers={
        "User-Agent": USER_AGENT, "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT).read()
    except (urllib.error.URLError, TimeoutError, OSError):
        print("(Discord webhook failed)")


def whisper_line(name, rank, subtype, price, seller):
    variant = variant_label(rank, subtype)
    return (f'/w {seller} Hi! I want to buy: "{name}{variant}" '
            f'for {price} platinum. (warframe.market)')


def variant_label(rank, subtype):
    if rank is not None:
        return f" (rank {rank})"
    if subtype:
        return f" ({subtype})"
    return ""


# ---------------- scanner ----------------

class Scanner(threading.Thread):
    daemon = True

    def __init__(self, config):
        super().__init__(name="scanner")
        self.config = config
        self.status = {"phase": "starting", "sweep": 0, "position": 0,
                       "total": 0, "last_item": "", "deals_24h": 0,
                       "stream": "off", "queue": 0, "baro": False,
                       "twitch": False}
        self.req_times = deque(maxlen=120)
        self.oneshot = set()          # slugs the UI asked to scan now
        self.hot = set()              # stream-triaged probable deals: jump the queue
        self.reval = set()            # active-deal re-checks: must not drown in
                                      # stream traffic, or deals go stale for hours
        self.dirty = set()            # slugs the live stream saw orders on
        self._swept_any = False
        self.id2slug = {}
        self._snapshot_last = {}      # bucket key -> (ts, low, ref)
        self._snap_lock = threading.Lock()
        self._items = {}              # slug -> name
        self._slugs = []
        self._items_fetched = 0
        self._sets_todo = []          # set slugs awaiting one-time discovery

    # --- item catalog ---

    def refresh_items(self, conn):
        data = get_json(f"{API}/items")
        if not data:
            return False
        with conn:
            for it in data["data"]:
                conn.execute(
                    "INSERT INTO items(slug, name, tags, wfm_id) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(slug) DO UPDATE SET name = excluded.name, "
                    "tags = excluded.tags, wfm_id = excluded.wfm_id",
                    (it["slug"], it["i18n"]["en"]["name"],
                     json.dumps(it.get("tags", [])), it.get("id")))
        self._load_items(conn)
        self._items_fetched = time.time()
        print(f"item catalog: {len(self._slugs)} tradeable items")
        return True

    def _load_items(self, conn):
        rows = conn.execute(
            "SELECT slug, name, wfm_id FROM items ORDER BY slug").fetchall()
        self._items = {r["slug"]: r["name"] for r in rows}
        self._slugs = list(self._items)
        self.id2slug = {r["wfm_id"]: r["slug"] for r in rows if r["wfm_id"]}
        self.status["total"] = len(self._slugs)

    # --- scheduling ---

    def _next_slug(self, conn, fast_due, force_sweep=False):
        now = time.time()
        # priority: UI request > hot candidates > watchlist > stream > sweep.
        # force_sweep guarantees the sweep a share of the budget even when the
        # live lanes are saturated — otherwise quiet items go stale forever.
        if self.oneshot:
            slug = self.oneshot.pop()
            if slug in self._items:
                return slug, True
        if force_sweep:  # above hot: revalidation load must not starve coverage
            slug = self._next_sweep_slug(conn, now)
            if slug:
                return slug, False
        if self.hot:
            return self.hot.pop(), True
        for slug, due in fast_due.items():
            if due <= now:
                fast_due[slug] = now + self.config.snapshot()["fast_interval"]
                return slug, True
        if self.reval:
            return self.reval.pop(), True
        if self.dirty:
            return self.dirty.pop(), True
        return self._next_sweep_slug(conn, now), False

    def _next_sweep_slug(self, conn, now):
        """Advance the background sweep to the next non-junk item, or None."""
        for _ in range(len(self._slugs) or 1):
            pos = self.status["position"] % max(len(self._slugs), 1)
            self.status["position"] = pos + 1
            if pos % 100 == 0:  # survive restarts without re-sweeping from 'A'
                conn.execute("INSERT OR REPLACE INTO config VALUES('sweep_pos', ?)",
                             (json.dumps(pos),))
                conn.commit()
            if pos == 0 and self._swept_any:  # a genuine wrap, not startup
                self.status["sweep"] += 1
            slug = self._slugs[pos] if self._slugs else None
            if slug is None:
                return None
            junk_until = conn.execute(
                "SELECT junk_until FROM items WHERE slug=?", (slug,)
            ).fetchone()["junk_until"]
            if junk_until <= now:
                self._swept_any = True
                return slug
        return None

    def run(self):
        conn = db_connect()
        self._load_items(conn)
        row = conn.execute("SELECT value FROM config WHERE key='sweep_pos'").fetchone()
        if row:
            self.status["position"] = int(json.loads(row["value"]))
        # one-time discovery of set recipes (parts + quantities + ducats)
        done = {r["set_slug"] for r in
                conn.execute("SELECT DISTINCT set_slug FROM set_parts")}
        self._sets_todo = [r["slug"] for r in conn.execute(
            "SELECT slug FROM items WHERE tags LIKE '%\"set\"%'")
            if r["slug"] not in done]
        if self._sets_todo:
            print(f"discovering {len(self._sets_todo)} set recipes "
                  f"(one-time, interleaved with scanning)")
        self.status["phase"] = "fetching item catalog"
        if not self.refresh_items(conn) and not self._slugs:
            self.status["phase"] = "no connection - retrying"
            while not self.refresh_items(conn):
                time.sleep(30)
        fast_due = {}
        last_prune = 0
        last_revalidate = 0
        last_reval_jackpot = 0
        last_baro = 0
        last_twitch = 0
        tick = 0
        while True:
            try:
                tick += 1
                cfg = self.config.snapshot()
                now = time.time()
                if now - self._items_fetched > ITEMS_REFRESH_S:
                    self.refresh_items(conn)
                if now - last_reval_jackpot > 30:
                    # jackpots re-verify in a least-recent rotation. Capped at 7
                    # per cycle: at ~1.5 s per scan, re-checking every jackpot
                    # every cycle would eat the whole budget when many are live.
                    for r in conn.execute(
                            "SELECT slug FROM deals WHERE gone_ts IS NULL AND "
                            "kind='jackpot' GROUP BY slug "
                            "ORDER BY MIN(checked_ts) LIMIT 7"):
                        self.hot.add(r["slug"])
                    last_reval_jackpot = now
                if now - last_baro > 300:
                    # while Baro is docked his wares are what people are
                    # actually trading — keep their prices fresh for the tab
                    last_baro = now
                    try:
                        self.check_baro(conn, cfg, now)
                    except Exception as e:
                        print(f"baro refresh failed: {e!r}")
                if now - last_twitch > 120:
                    last_twitch = now
                    try:
                        self.check_twitch(conn, cfg)
                    except Exception as e:
                        print(f"twitch check failed: {e!r}")
                if now - last_revalidate > 120:
                    # re-check items with live deals, least-recently-verified
                    # first — in their own lane, above the stream flood
                    for r in conn.execute(
                            "SELECT slug FROM deals WHERE gone_ts IS NULL "
                            "GROUP BY slug ORDER BY MIN(checked_ts) LIMIT 40"):
                        self.reval.add(r["slug"])
                    last_revalidate = now
                if now - last_prune > 6 * 3600:
                    conn.execute("DELETE FROM snapshots WHERE ts < ?",
                                 (int(now - HISTORY_DAYS * 86400),))
                    # expired deals are history, not live data: keep two weeks
                    conn.execute("DELETE FROM deals WHERE gone_ts IS NOT NULL "
                                 "AND gone_ts < ?", (int(now - 14 * 86400),))
                    conn.commit()
                    last_prune = now
                # sync fast lane with watchlist
                watched = {r["slug"] for r in
                           conn.execute("SELECT slug FROM watchlist")}
                for slug in list(fast_due):
                    if slug not in watched:
                        del fast_due[slug]
                for slug in watched:
                    fast_due.setdefault(slug, 0)

                # every 3rd tick goes to set-recipe discovery until done,
                # replacing (not adding to) a market scan to keep the rate polite
                if self._sets_todo and tick % 3 == 0:
                    self.status["phase"] = "discovering sets"
                    self.discover_set(conn, self._sets_todo.pop())
                    self.req_times.append(time.time())
                    time.sleep(cfg["request_delay"])
                    continue
                slug, is_fast = self._next_slug(conn, fast_due,
                                                force_sweep=(tick % 4 == 0))
                self.status["queue"] = len(self.dirty)
                if slug is None:
                    time.sleep(5)
                    continue
                self.status["phase"] = "scanning"
                self.status["last_item"] = self._items.get(slug, slug)
                self.process_item(conn, slug, cfg)
                self.req_times.append(time.time())
                time.sleep(cfg["request_delay"])
            except Exception as e:
                print(f"scanner error on {self.status['last_item']}: {e!r}")
                time.sleep(5)

    def announce(self, cfg, enabled, title, body, sound="Glass"):
        print(f"[{title}] {body}")
        if cfg["mute_all"] or not enabled:
            return
        notify_desktop(title, body, sound=sound)
        notify_discord(cfg["discord_webhook"], f"**{title}**\n{body}")
        sse_push({"event": "notice", "title": title, "body": body})

    def check_baro(self, conn, cfg, now):
        """Keep Baro's wares priced while he's docked, and announce arrival
        and last call — a 48 h window is easy to sleep through."""
        data = baro_status()
        if not data or not data.get("expiry"):
            return
        start, end = iso_to_epoch(data["activation"]), iso_to_epoch(data["expiry"])
        active = start <= now < end
        self.status["baro"] = active
        was_active = flag_get(conn, "baro_active", False)
        if active:
            wares = baro_wares(conn)["wares"]
            tradeable = [w for w in wares if w["slug"]]
            for w in tradeable:
                self.reval.add(w["slug"])
            if not was_active:
                self.announce(cfg, cfg["notify_baro"], "Baro Ki'Teer has arrived",
                              f'{data["location"]} — {len(tradeable)} of '
                              f'{len(wares)} wares trade on warframe.market',
                              sound="Hero")
            hours_left = (end - now) / 3600
            if hours_left <= 6 and not flag_get(conn, "baro_last_call", False):
                flag_set(conn, "baro_last_call", True)
                self.announce(cfg, cfg["notify_baro"], "Baro leaves soon",
                              f"about {hours_left:.0f} h left at {data['location']}")
        elif was_active:
            flag_set(conn, "baro_last_call", False)
        flag_set(conn, "baro_active", active)

    def check_twitch(self, conn, cfg):
        """Announce when the official channel goes live (drops are common)."""
        t = twitch_status()
        if not t:
            return
        live = bool(t.get("live"))
        self.status["twitch"] = live
        if live and not flag_get(conn, "twitch_live", False):
            viewers = f' · {t["viewers"]:,} watching' if t.get("viewers") else ""
            self.announce(cfg, cfg["notify_twitch"], "Warframe is live on Twitch",
                          (t.get("title") or "twitch.tv/warframe") + viewers)
        flag_set(conn, "twitch_live", live)

    def discover_set(self, conn, set_slug):
        """Fetch a set's recipe once: parts, quantities and ducat values."""
        data = get_json(f"{API}/item/{set_slug}/set")
        if not data or "data" not in data or "items" not in data["data"]:
            self._sets_todo.append(set_slug)  # retry later
            return
        with conn:
            for it in data["data"]["items"]:
                if it.get("ducats") is not None:
                    conn.execute("UPDATE items SET ducats=? WHERE slug=?",
                                 (it["ducats"], it["slug"]))
                if not it.get("setRoot"):
                    conn.execute(
                        "INSERT OR REPLACE INTO set_parts VALUES(?,?,?)",
                        (set_slug, it["slug"], it.get("quantityInSet") or 1))

    # --- per-item work ---

    def process_item(self, conn, slug, cfg):
        payload = get_json(f"{API}/orders/item/{slug}")
        if payload is None:
            return
        name = self._items.get(slug, slug)
        buckets = analyze_orders(payload, cfg["only_ingame"])
        now = int(time.time())
        best_reference = 0

        for (rank, subtype), b in buckets.items():
            sells, ref = b["sells"], b["reference"]
            low = sells[0]["platinum"] if sells else None
            if ref:
                best_reference = max(best_reference, ref)
            qualifies = ref and ref >= cfg["min_price"]
            hist_low = ref_med_7d = None
            if qualifies:
                # read history BEFORE recording this scan, or the fresh
                # snapshot would poison hist_low and "30-day low" never fires
                hist_low, ref_med_7d = self.history_context(conn, slug, rank, subtype)
            self.record_snapshot(conn, slug, rank, subtype, now, low, ref,
                                 b["buy_high"], len(sells))
            if not qualifies:
                continue

            cheapest = sells[0]
            price = cheapest["platinum"]
            savings = ref - price
            second = sells[1]["platinum"] if len(sells) >= 2 else None
            if (savings >= cfg["min_savings"]
                    and price <= ref * (1 - cfg["min_discount"])):
                # A jackpot is a snipe so far below the field it looks fake:
                # a huge gap under market, or a lone outlier under the next seller.
                deep = price <= ref * (1 - cfg["jackpot_discount"])
                lone = second is not None and price <= second * (1 - cfg["jackpot_gap"])
                if deep or lone:
                    bits = []
                    if deep:
                        bits.append(f"{round(100 * savings / ref)}% below market")
                    if lone:
                        bits.append(f"next cheapest is {second}p")
                    if hist_low and price < hist_low:
                        bits.append("30-day low")
                    self.record_deal(conn, cfg, "jackpot", slug, name, rank, subtype,
                                     price=price, reference=ref,
                                     seller=cheapest["user"]["ingameName"],
                                     note=" · ".join(bits),
                                     order_key=f'{cheapest["id"]}:{price}',
                                     status=cheapest["user"]["status"])
                else:
                    note = "30-day low" if hist_low and price < hist_low else ""
                    self.record_deal(conn, cfg, "snipe", slug, name, rank, subtype,
                                     price=price, reference=ref,
                                     seller=cheapest["user"]["ingameName"], note=note,
                                     order_key=f'{cheapest["id"]}:{price}',
                                     status=cheapest["user"]["status"])

            # Buy orders far above the sell floor are bait or abandoned orders
            # (a real buyer would just buy the floor listing), not flip profit.
            if (b["buy_high"] is not None
                    and b["buy_high"] - cheapest["platinum"] >= cfg["flip_min_profit"]
                    and b["buy_high"] <= ref * 1.25):
                profit = b["buy_high"] - cheapest["platinum"]
                self.record_deal(conn, cfg, "flip", slug, name, rank, subtype,
                                 price=cheapest["platinum"], reference=b["buy_high"],
                                 seller=cheapest["user"]["ingameName"],
                                 note=f"+{profit}p buy-order spread",
                                 order_key=f"flip:{slug}:{rank}:{subtype}:"
                                           f'{cheapest["platinum"]}:{b["buy_high"]}',
                                 status=cheapest["user"]["status"])

            if ref_med_7d and ref <= ref_med_7d * 0.70:
                pct = round(100 * (1 - ref / ref_med_7d))
                self.record_deal(conn, cfg, "dip", slug, name, rank, subtype,
                                 price=int(ref), reference=ref_med_7d,
                                 seller="", note=f"market {pct}% under 7-day norm",
                                 order_key=f"dip:{slug}:{rank}:{subtype}:{now // 86400}")

        if best_reference and best_reference < cfg["min_price"]:
            conn.execute("UPDATE items SET junk_until=? WHERE slug=?",
                         (now + JUNK_RECHECK_S, slug))
            conn.commit()
        self.check_price_target(conn, cfg, slug, name, buckets)
        self.cleanup_stale_deals(conn, slug, buckets, cfg)
        return buckets

    def check_price_target(self, conn, cfg, slug, name, buckets):
        """Watchlist items can carry a personal price target; alert when the
        main variant's cheapest listing crosses it."""
        row = conn.execute("SELECT target FROM watchlist WHERE slug=?",
                           (slug,)).fetchone()
        if not row or not row["target"]:
            return
        # "main" variant = the most valuable bucket (max rank / the set itself)
        main = max((b for b in buckets.values() if b["reference"]),
                   key=lambda b: b["reference"], default=None)
        if not main or not main["sells"]:
            return
        cheapest = main["sells"][0]
        if cheapest["platinum"] <= row["target"]:
            self.record_deal(
                conn, cfg, "target", slug, name,
                cheapest.get("rank"), cheapest.get("subtype"),
                price=cheapest["platinum"], reference=main["reference"],
                seller=cheapest["user"]["ingameName"],
                note=f'hit your {row["target"]}p target',
                order_key=f'target:{cheapest["id"]}:{cheapest["platinum"]}',
                status=cheapest["user"]["status"])

    def cleanup_stale_deals(self, conn, slug, buckets, cfg):
        """Re-check every live deal for this item against the book we just
        fetched: expire it if the listing is gone OR if the market has moved
        so it is no longer a deal, and refresh the comparison price otherwise.
        Without the refresh a deal keeps advertising the discount it had when
        it was found, which drifts into fiction as the market moves."""
        now = int(time.time())
        rows = conn.execute(
            "SELECT id, kind, ts, rank, subtype, price, reference, pct, order_key "
            "FROM deals WHERE slug=? AND gone_ts IS NULL", (slug,)).fetchall()
        expired = 0
        for r in rows:
            b = buckets.get((r["rank"], r["subtype"]))
            alive = False
            reference = r["reference"]
            if r["kind"] in ("snipe", "jackpot", "target"):
                oid = r["order_key"].split(":")[-2] if r["kind"] == "target" \
                    else r["order_key"].split(":")[0]
                listed = b and any(o["id"] == oid and o["platinum"] == r["price"]
                                   for o in b["sells"])
                if listed:
                    # If the book is momentarily too thin for the normal
                    # reference, fall back to the rest of the book rather than
                    # keeping the price this deal was born with — a fossilised
                    # reference makes the discount pure fiction.
                    ref_now = b["reference"]
                    if not ref_now and len(b["sells"]) >= 2:
                        ref_now = statistics.median(
                            [o["platinum"] for o in b["sells"][1:6]])
                    if ref_now:
                        reference = ref_now
                        savings = ref_now - r["price"]
                        # targets are the user's own number, not a discount rule
                        still_a_deal = r["kind"] == "target" or (
                            savings >= cfg["min_savings"]
                            and r["price"] <= ref_now * (1 - cfg["min_discount"]))
                        alive = still_a_deal
                    else:
                        alive = True  # only listing in the book; nothing to compare
            elif r["kind"] == "flip":
                low = b["sells"][0]["platinum"] if b and b["sells"] else None
                alive = b and low == r["price"] and b["buy_high"] == r["reference"]
            elif r["kind"] == "dip":
                alive = now - r["ts"] < 86400  # dips expire after a day
            if alive:
                pct = (round(100 * (reference - r["price"]) / reference)
                       if reference else r["pct"])
                conn.execute(
                    "UPDATE deals SET checked_ts=?, reference=?, pct=?, rev=? "
                    "WHERE id=?", (now, reference, pct, next_rev(conn), r["id"]))
            else:
                conn.execute("UPDATE deals SET gone_ts=?, rev=? WHERE id=?",
                             (now, next_rev(conn), r["id"]))
                expired += 1
        conn.commit()
        if expired:
            sse_push({"event": "gone", "count": expired})

    def record_snapshot(self, conn, slug, rank, subtype, ts, low, ref, buy_high, sellers):
        if low is None and buy_high is None:
            return
        key = (slug, rank, subtype)
        # /api/item_live runs this from HTTP threads too — guard the dedup map
        with self._snap_lock:
            prev = self._snapshot_last.get(key)
        if prev and ts - prev[0] < SNAPSHOT_MIN_GAP_S and (low, ref) == prev[1]:
            return
        conn.execute("INSERT INTO snapshots(slug, rank, subtype, ts, low, reference,"
                     " buy_high, sellers) VALUES(?,?,?,?,?,?,?,?)",
                     (slug, rank, subtype, ts, low, ref, buy_high, sellers))
        conn.commit()
        with self._snap_lock:
            self._snapshot_last[key] = (ts, (low, ref))
            if len(self._snapshot_last) > 20000:  # bound it on long runs
                self._snapshot_last.clear()

    def history_context(self, conn, slug, rank, subtype):
        since30 = int(time.time()) - 30 * 86400
        since7 = int(time.time()) - 7 * 86400
        hist_low = conn.execute(
            "SELECT MIN(low) AS m FROM snapshots WHERE slug=? AND rank IS ? "
            "AND subtype IS ? AND ts>? AND low IS NOT NULL",
            (slug, rank, subtype, since30)).fetchone()["m"]
        rows = conn.execute(
            "SELECT ts, reference FROM snapshots WHERE slug=? AND rank IS ? "
            "AND subtype IS ? AND ts>? AND reference IS NOT NULL ORDER BY ts",
            (slug, rank, subtype, since7)).fetchall()
        refs = [r["reference"] for r in rows]
        # a "7-day norm" needs history actually spanning days, not five
        # samples from one afternoon (daily price swings would fake dips)
        span = rows[-1]["ts"] - rows[0]["ts"] if rows else 0
        ref_med = (statistics.median(refs)
                   if len(refs) >= 5 and span >= 3 * 86400 else None)
        return hist_low, ref_med

    def record_deal(self, conn, cfg, kind, slug, name, rank, subtype,
                    price, reference, seller, note, order_key, status=""):
        pct = round(100 * (reference - price) / reference) if reference else 0
        now = int(time.time())
        # a listing can disappear (seller offline) and come back: revive the
        # original deal row instead of silently ignoring the insert
        revived = conn.execute(
            "UPDATE deals SET gone_ts=NULL, checked_ts=?, rev=? "
            "WHERE order_key=? AND gone_ts IS NOT NULL",
            (now, next_rev(conn), order_key))
        if revived.rowcount:
            conn.commit()
            sse_push({"event": "deal", "kind": kind})
            return  # back in the feed; no repeat toast
        cur = conn.execute(
            "INSERT OR IGNORE INTO deals(ts, kind, slug, name, rank, subtype, price,"
            " reference, pct, seller, status, note, order_key, checked_ts, rev)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, kind, slug, name, rank, subtype, price,
             reference, pct, seller, status, note, order_key, now, next_rev(conn)))
        conn.commit()
        if cur.rowcount != 1:
            return  # already alerted
        sse_push({"event": "deal", "kind": kind})
        variant = variant_label(rank, subtype)
        title = f"{name}{variant}: {price}p (ref {round(reference)}p, -{pct}%)"
        seller_desc = seller + (f" ({status})" if status and status != "ingame" else "")
        print(f"[{kind.upper()}] {title}" + (f"  ({note})" if note else ""))
        if seller:
            print(f"  seller {seller_desc}")
            print(f"  {whisper_line(name, rank, subtype, price, seller)}")
        toggles = {"snipe": cfg["notify_snipes"], "jackpot": cfg["notify_jackpots"],
                   "flip": cfg["notify_flips"], "dip": cfg["notify_dips"],
                   "target": cfg["notify_targets"]}
        if not cfg["mute_all"] and toggles.get(kind, True):
            body = note or (f"seller: {seller_desc}" if seller else kind)
            notify_desktop(title, body, sound="Hero" if kind == "jackpot" else "Glass")
            text = f"**[{kind}] {title}**" + (f"\n{note}" if note else "")
            if seller:
                text += f"\n`{whisper_line(name, rank, subtype, price, seller)}`"
            notify_discord(cfg["discord_webhook"], text)


# ---------------- live market stream ----------------

try:
    import aiohttp
    import asyncio
    HAVE_AIOHTTP = True
except ImportError:
    HAVE_AIOHTTP = False

STREAM_URL = "wss://warframe.market/socket-v2?platform=pc"
STREAM_SUBSCRIBE = json.dumps({"route": "@wfm|cmd/subscribe/newOrders",
                               "payload": {}})
STREAM_ORDER_EVENT = "@wfm|event/subscriptions/newOrder"


class MarketStream(threading.Thread):
    """Subscribes to warframe.market's live order feed and marks items dirty.

    Every new/updated order on the whole market arrives here within seconds;
    the scanner then re-fetches just those items. The periodic sweep remains
    as a backfill for anything the stream misses.
    """
    daemon = True

    def __init__(self, scanner):
        super().__init__(name="stream")
        self.scanner = scanner
        self._recent = {}  # slug -> last enqueue ts, to debounce relist sprees

    def run(self):
        asyncio.run(self._loop())

    async def _loop(self):
        conn = db_connect()
        backoff = 5
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    # identify like our REST calls do — Cloudflare 403s the
                    # default "Python/aiohttp" agent after repeated connects
                    async with session.ws_connect(
                            STREAM_URL, ssl=SSL_CONTEXT, heartbeat=30,
                            headers={"Origin": "https://warframe.market",
                                     "User-Agent": USER_AGENT}) as ws:
                        await ws.send_str(STREAM_SUBSCRIBE)
                        self.scanner.status["stream"] = "live"
                        backoff = 5
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                break
                            self._handle(conn, msg.data)
            except Exception as e:
                print(f"stream error: {type(e).__name__}: {e}")
                if "403" in str(e):  # blocked at the edge: don't hammer it
                    backoff = max(backoff, 300)
            self.scanner.status["stream"] = "reconnecting"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    def _handle(self, conn, raw):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if data.get("route") != STREAM_ORDER_EVENT:
            return
        order = data.get("payload") or {}
        slug = self.scanner.id2slug.get(order.get("itemId"))
        if not slug or not order.get("visible", True):
            return
        now = time.time()
        if self._hot_candidate(conn, order, slug):
            return  # jumped the queue; bypasses debounce on purpose
        if now - self._recent.get(slug, 0) < 20:  # debounce bursts per item
            return
        if len(self.scanner.dirty) > 100:  # backlog cap; the sweep catches up
            return
        junk = conn.execute("SELECT junk_until FROM items WHERE slug=?",
                            (slug,)).fetchone()
        if junk and junk["junk_until"] > now:
            return
        self._recent[slug] = now
        self.scanner.dirty.add(slug)

    def _hot_candidate(self, conn, order, slug):
        """Triage straight from the event: the payload carries the price, and
        we know the item's going rate from our own snapshots. A listing that
        already looks like a deal skips the queue and gets confirmed in ~1-2 s
        instead of waiting behind ambient traffic."""
        if order.get("type") != "sell" or len(self.scanner.hot) > 30:
            return False
        cfg = self.scanner.config.snapshot()
        statuses = ("ingame",) if cfg["only_ingame"] else ("ingame", "online")
        if (order.get("user") or {}).get("status") not in statuses:
            return False
        row = conn.execute(
            "SELECT reference FROM snapshots WHERE slug=? AND rank IS ? AND "
            "subtype IS ? AND reference IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (slug, order.get("rank"), order.get("subtype"))).fetchone()
        ref = row["reference"] if row else None
        price = order.get("platinum", 0)
        if (ref and ref >= cfg["min_price"]
                and ref - price >= cfg["min_savings"]
                and price <= ref * (1 - cfg["min_discount"])):
            print(f"⚡ hot listing: {slug} at {price}p "
                  f"(last known market ~{round(ref)}p) — scanning now")
            self._recent[slug] = time.time()
            self.scanner.hot.add(slug)
            return True
        return False


# ---------------- web server ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "wfm-deal-notifier/2.0"

    def log_message(self, *args):
        pass  # keep the console for deals, not request logs

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _serve_events(self):
        """Server-sent events: holds the connection and pushes deal updates."""
        mailbox = queue.Queue(maxsize=100)
        with SSE_LOCK:
            SSE_CLIENTS.append(mailbox)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b": connected\n\n")  # immediate life sign
            self.wfile.flush()
            while True:
                try:
                    data = mailbox.get(timeout=20)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keep-alive
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SSE_LOCK:
                if mailbox in SSE_CLIENTS:
                    SSE_CLIENTS.remove(mailbox)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        route = url.path
        if route == "/api/events":
            self._serve_events()
            return
        conn = db_connect()
        try:
            if route == "/":
                body = UI_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/status":
                s = SCANNER.status
                day_ago = int(time.time()) - 86400
                deals_24h = conn.execute(
                    "SELECT COUNT(*) AS n FROM deals WHERE ts>?", (day_ago,)
                ).fetchone()["n"]
                jackpots_24h = conn.execute(
                    "SELECT COUNT(*) AS n FROM deals WHERE ts>? AND kind='jackpot'",
                    (day_ago,)).fetchone()["n"]
                # snapshot first: iterating the deque while the scanner
                # appends raises "deque mutated during iteration"
                recent = [t for t in list(SCANNER.req_times)
                          if t > time.time() - 60]
                self._json({**s, "deals_24h": deals_24h, "jackpots_24h": jackpots_24h,
                            "req_per_min": len(recent),
                            "throttled": THROTTLE_COUNT["n"],
                            "watch_count": conn.execute(
                                "SELECT COUNT(*) AS n FROM watchlist").fetchone()["n"]})
            elif route == "/api/deals":
                try:
                    limit = min(int(q.get("limit", ["100"])[0]), 500)
                except ValueError:
                    limit = 100
                since = q.get("since_rev", [None])[0]
                try:
                    since = int(since) if since is not None else None
                except ValueError:
                    since = None
                if since is None:  # first load: the newest window
                    rows = conn.execute(
                        "SELECT d.*, i.tags FROM deals d LEFT JOIN items i "
                        "ON i.slug = d.slug ORDER BY d.id DESC LIMIT ?",
                        (limit,)).fetchall()
                else:  # incremental: only what changed since the client's cursor
                    rows = conn.execute(
                        "SELECT d.*, i.tags FROM deals d LEFT JOIN items i "
                        "ON i.slug = d.slug WHERE d.rev > ? ORDER BY d.rev LIMIT ?",
                        (since, limit)).fetchall()
                deals = []
                for r in rows:
                    d = dict(r)
                    d["category"] = item_category(d.pop("tags", None))
                    if d["seller"]:
                        d["whisper"] = whisper_line(d["name"], d["rank"],
                                                    d["subtype"], d["price"],
                                                    d["seller"])
                    deals.append(d)
                max_rev = conn.execute(
                    "SELECT COALESCE(MAX(rev), 0) AS m FROM deals").fetchone()["m"]
                if since is None:
                    self._json({"deals": deals, "rev": max_rev, "full": True})
                else:
                    self._json({"deals": deals, "rev": max_rev, "full": False})
            elif route == "/api/stats":
                self._json(build_stats(conn))
            elif route == "/api/all_items":
                rows = conn.execute(
                    "SELECT slug, name, tags FROM items ORDER BY name").fetchall()
                self._json([{"slug": r["slug"], "name": r["name"],
                             "category": item_category(r["tags"])} for r in rows])
            elif route == "/api/item_live":
                slug = q.get("slug", [""])[0]
                row = conn.execute("SELECT name FROM items WHERE slug=?",
                                   (slug,)).fetchone()
                if not row:
                    self._json({"error": "unknown item"}, 404)
                    return
                buckets = SCANNER.process_item(conn, slug, CONFIG.snapshot())
                SCANNER.req_times.append(time.time())
                if buckets is None:
                    self._json({"error": "warframe.market unreachable"}, 502)
                    return
                out = []
                for (rank, subtype), b in buckets.items():
                    sells = b["sells"]
                    out.append({
                        "label": variant_label(rank, subtype).strip() or "standard",
                        "low": sells[0]["platinum"] if sells else None,
                        "reference": b["reference"], "buy_high": b["buy_high"],
                        "sellers": len(sells)})
                out.sort(key=lambda x: -(x["reference"] or 0))
                self._json({"slug": slug, "name": row["name"], "buckets": out})
            elif route == "/api/baro":
                data = baro_status()
                if data:
                    self._json(data)
                else:
                    self._json({"error": "worldstate unavailable"}, 502)
            elif route == "/api/baro_wares":
                self._json(baro_wares(conn))
            elif route == "/api/twitch":
                self._json(twitch_status() or {"live": False,
                                               "error": "twitch unavailable"})
            elif route == "/api/ducats":
                rows = conn.execute(
                    "SELECT i.slug, i.name, i.ducats, s.low, s.ts, s.sellers "
                    "FROM items i JOIN "
                    "(SELECT slug, MAX(id) AS mid FROM snapshots "
                    " WHERE rank IS NULL AND subtype IS NULL GROUP BY slug) m "
                    "ON m.slug = i.slug JOIN snapshots s ON s.id = m.mid "
                    "WHERE i.ducats IS NOT NULL AND i.ducats > 0 "
                    "AND s.low IS NOT NULL AND s.sellers >= 2").fetchall()
                now_ts = int(time.time())
                out = []
                for r in rows:
                    age = round((now_ts - r["ts"]) / 60)
                    out.append({
                        "slug": r["slug"], "name": r["name"],
                        "ducats": r["ducats"], "low": r["low"],
                        "ratio": round(r["ducats"] / r["low"], 1),
                        "sellers": r["sellers"], "age_min": age,
                        # a stale-low price inflates the ratio, which would
                        # float outdated rows to the top of the ranking
                        "stale": age > DUCAT_FRESH_MIN})
                out.sort(key=lambda x: (x["stale"], -x["ratio"]))
                self._json(out[:100])
            elif route == "/api/item":
                slug = q.get("slug", [""])[0]
                row = conn.execute("SELECT name, ducats FROM items WHERE slug=?",
                                   (slug,)).fetchone()
                if not row:
                    self._json({"error": "unknown item"}, 404)
                    return
                since = int(time.time()) - HISTORY_DAYS * 86400
                snaps = conn.execute(
                    "SELECT rank, subtype, ts, low, reference, buy_high, sellers "
                    "FROM snapshots WHERE slug=? AND ts>? ORDER BY ts",
                    (slug, since)).fetchall()
                buckets = {}
                for s in snaps:
                    key = variant_label(s["rank"], s["subtype"]).strip() or "standard"
                    buckets.setdefault(key, []).append(
                        [s["ts"], s["low"], s["reference"], s["buy_high"], s["sellers"]])
                self._json({"slug": slug, "name": row["name"],
                            "ducats": row["ducats"], "buckets": buckets})
            elif route == "/api/watchlist":
                rows = conn.execute(
                    "SELECT w.slug, i.name, w.target FROM watchlist w JOIN items i "
                    "ON i.slug = w.slug ORDER BY i.name").fetchall()
                self._json([dict(r) for r in rows])
            elif route == "/api/sets":
                latest = {}
                for r in conn.execute(
                        "SELECT s.slug, s.low, s.ts FROM snapshots s JOIN "
                        "(SELECT slug, MAX(id) AS mid FROM snapshots "
                        " WHERE rank IS NULL AND subtype IS NULL GROUP BY slug) m "
                        "ON s.id = m.mid WHERE s.low IS NOT NULL"):
                    latest[r["slug"]] = (r["low"], r["ts"])
                names = {r["slug"]: r["name"] for r in
                         conn.execute("SELECT slug, name FROM items")}
                recipes = {}
                for r in conn.execute("SELECT set_slug, part_slug, qty FROM set_parts"):
                    recipes.setdefault(r["set_slug"], []).append(
                        (r["part_slug"], r["qty"]))
                now_ts = int(time.time())
                out, incomplete = [], 0
                for set_slug, parts in recipes.items():
                    if set_slug not in latest or any(p not in latest
                                                     for p, _ in parts):
                        incomplete += 1
                        continue
                    set_low, set_ts = latest[set_slug]
                    parts_total = sum(latest[p][0] * qty for p, qty in parts)
                    oldest = min([set_ts] + [latest[p][1] for p, _ in parts])
                    age_min = round((now_ts - oldest) / 60)
                    out.append({
                        "slug": set_slug, "name": names.get(set_slug, set_slug),
                        "set_low": set_low, "parts_total": parts_total,
                        "margin": set_low - parts_total,
                        "age_min": age_min,
                        # a margin computed from hours-old parts can be flat
                        # wrong (or inverted) — never present it as actionable
                        "stale": age_min > SET_FRESH_MIN,
                        "part_slugs": [p for p, _ in parts],
                        "parts": [{"name": names.get(p, p), "qty": q,
                                   "low": latest[p][0]} for p, q in parts]})
                out.sort(key=lambda x: -x["margin"])
                self._json({"sets": out, "awaiting_data": incomplete,
                            "discovering": len(SCANNER._sets_todo)})
            elif route == "/api/config":
                self._json(CONFIG.snapshot())
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": repr(e)}, 500)
            except Exception:
                pass
        finally:
            conn.close()

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        body = self._read_body()
        conn = db_connect()
        try:
            if route == "/api/config":
                CONFIG.update(body)
                self._json(CONFIG.snapshot())
            elif route == "/api/watchlist":
                slug = str(body.get("slug", ""))
                known = conn.execute("SELECT 1 FROM items WHERE slug=?",
                                     (slug,)).fetchone()
                if body.get("action") == "add" and known:
                    with conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO watchlist(slug) VALUES(?)",
                            (slug,))
                    SCANNER.oneshot.add(slug)
                elif body.get("action") == "remove":
                    with conn:
                        conn.execute("DELETE FROM watchlist WHERE slug=?", (slug,))
                elif body.get("action") == "target" and known:
                    try:
                        target = max(1, int(float(body.get("target"))))
                    except (TypeError, ValueError):
                        target = None
                    with conn:
                        conn.execute("UPDATE watchlist SET target=? WHERE slug=?",
                                     (target, slug))
                    SCANNER.oneshot.add(slug)  # evaluate the new target now
                self._json({"ok": True})
            elif route == "/api/scan_set":
                slug = str(body.get("slug", ""))
                SCANNER.oneshot.add(slug)
                for r in conn.execute(
                        "SELECT part_slug FROM set_parts WHERE set_slug=?", (slug,)):
                    SCANNER.oneshot.add(r["part_slug"])
                self._json({"ok": True})
            elif route == "/api/verify":
                # re-price a batch of items now (set arbitrage verification)
                slugs = body.get("slugs") or []
                queued = 0
                for slug in slugs[:60]:
                    if isinstance(slug, str) and conn.execute(
                            "SELECT 1 FROM items WHERE slug=?", (slug,)).fetchone():
                        SCANNER.oneshot.add(slug)
                        queued += 1
                self._json({"queued": queued})
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": repr(e)}, 500)
            except Exception:
                pass
        finally:
            conn.close()


def main():
    p = argparse.ArgumentParser(description="warframe.market deal scanner + web UI")
    p.add_argument("--port", type=int, default=8737)
    p.add_argument("--host", default="127.0.0.1",
                   help="use 0.0.0.0 to reach the UI from other devices on your LAN")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    db_init()
    global CONFIG, SCANNER
    CONFIG = Config()
    SCANNER = Scanner(CONFIG)
    SCANNER.start()
    if HAVE_AIOHTTP:
        MarketStream(SCANNER).start()
    else:
        print("live order stream disabled — run: pip install aiohttp")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
    print(f"web UI: {url}   (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    def shutdown(*_):
        # SIGTERM is how this gets stopped in practice, so fold the
        # write-ahead log back into the db there too — not just on Ctrl-C
        try:
            conn = db_connect()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            print("checkpointed database")
        except sqlite3.Error as e:
            print(f"checkpoint failed: {e}")
        print("Stopped.")
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        shutdown()


if __name__ == "__main__":
    main()
