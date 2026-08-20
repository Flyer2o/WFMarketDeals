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
import stat
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
LOG_MAX_BYTES = 8 * 1024 * 1024   # trim the log past this
GHOST_AFTER_H = 6          # "online" sellers last seen longer ago than this
                           # are treated as unreachable, not as live listings
REF_SANITY_MULT = 5.0      # a reference this far above the item's own 7-day
                           # norm is a joke listing, not a market move: 99% of
                           # real references sit within 4.4x of it
MAX_PLAUSIBLE_PLAT = 25000  # nothing on this market really sells for more. The
                            # dearest genuine item observed is a 20,000p
                            # collector pack; past that it is a joke listing,
                            # and on a thin book one of those becomes the
                            # "market price" every discount is measured from

DEFAULT_CONFIG = {
    "min_discount": 0.30,    # snipe: below reference by this fraction...
    "min_savings": 15,       # ...and by this many plat
    "min_price": 25,         # ignore items whose going rate is below this
    "only_ingame": False,    # True = in-game sellers only; False = in-game + online
                             # (offline sellers are never considered either way)
    "ghost_hours": 6,        # ignore "active" sellers last seen longer ago than
                             # this — their session is stale, they won't reply
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
    "notify_twitch": True,   # an official Warframe channel goes live (drops!)
    "auto_open_twitch": True,  # open the stream in your browser when it starts
    "notify_dips": False,    # dips always show in the UI; toast only if on
    # the sell side: how far over its own 7-day norm an item has to trade
    # before it is worth listing one of yours
    "peak_over": 1.40,
    "notify_peaks": False,
    "discord_webhook": "",
}

# sane bounds for numeric settings; protects against typos in the UI
CONFIG_LIMITS = {
    "min_discount": (0.01, 0.95), "jackpot_discount": (0.05, 0.95),
    "jackpot_gap": (0.05, 0.95), "min_savings": (0, 100000),
    "peak_over": (1.05, 10.0),
    "min_price": (0, 100000), "flip_min_profit": (1, 100000),
    "fast_interval": (10, 3600), "request_delay": (0.25, 5.0),
    "ghost_hours": (0.5, 72),
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
        CREATE TABLE IF NOT EXISTS game_mods(
            name TEXT PRIMARY KEY, mtype TEXT, polarity TEXT, drain INTEGER,
            compat TEXT, rarity TEXT, slot TEXT, stats TEXT, text TEXT);
        CREATE INDEX IF NOT EXISTS idx_mods_slot ON game_mods(slot, compat);
        CREATE TABLE IF NOT EXISTS frames(
            name TEXT PRIMARY KEY, health INTEGER, shield INTEGER, armor INTEGER,
            energy INTEGER, sprint REAL, mastery INTEGER, is_prime INTEGER,
            abilities TEXT, description TEXT);
        CREATE TABLE IF NOT EXISTS riven_weapons(
            slug TEXT PRIMARY KEY, name TEXT NOT NULL, grp TEXT, riven_type TEXT,
            disposition REAL, mastery INTEGER, wtype TEXT, category TEXT,
            crit_chance REAL, crit_mult REAL, proc_chance REAL,
            total_damage REAL, fire_rate REAL, multishot REAL, rating REAL,
            best_variant TEXT);
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
                "ALTER TABLE deals ADD COLUMN rev INTEGER",
                "ALTER TABLE deals ADD COLUMN seller_seen INTEGER",
                "ALTER TABLE deals ADD COLUMN seller_rep INTEGER",
                # the game's own path for a mod: it encodes which mods are the
                # same mod, which their display names often do not
                "ALTER TABLE game_mods ADD COLUMN unique_name TEXT",
                # which physical damage type a weapon actually deals: it decides
                # whether -Impact or -Puncture is really a free riven negative
                "ALTER TABLE riven_weapons ADD COLUMN phys TEXT",
                # "Held" means a continuous beam, which eats ammo; Projectile
                # means the shot travels. Both decide riven negatives, and both
                # were being guessed from the weapon type before.
                "ALTER TABLE riven_weapons ADD COLUMN fire_trigger TEXT",
                "ALTER TABLE riven_weapons ADD COLUMN shot_type TEXT",
                # farmable / partial / vaulted, for prime gear only: whether
                # its relics still drop. The main price driver for primes.
                "ALTER TABLE items ADD COLUMN vault TEXT"):
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
    # Some slugs carry non-ASCII characters (höllvanian_courtyard_simulacrum,
    # summoner’s_wrath with a curly apostrophe). http.client insists on an
    # ASCII request line, so without escaping these raise UnicodeEncodeError
    # on every attempt and the items can never be priced at all.
    parts = urllib.parse.urlsplit(url)
    if not parts.path.isascii():
        url = urllib.parse.urlunsplit(
            parts._replace(path=urllib.parse.quote(parts.path, safe="/")))
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


def seller_seen_epoch(order):
    """When the seller was last seen, in epoch seconds (feed times are UTC)."""
    raw = (order.get("user") or {}).get("lastSeen") or ""
    try:
        return calendar.timegm(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


def ghost_cutoff_h():
    try:
        return CONFIG.snapshot()["ghost_hours"]
    except Exception:
        return GHOST_AFTER_H


def is_reachable(order, now=None):
    """A status of 'ingame' is not proof anyone is there — roughly one in
    seven such sellers was last seen many hours ago (ghost sessions from
    clients that never disconnected cleanly). Whispering them goes nowhere,
    so a deal is only worth showing if its seller plausibly still exists."""
    seen = seller_seen_epoch(order)
    if not seen:
        return True  # no timestamp to judge by; trust the status
    return (now or time.time()) - seen <= ghost_cutoff_h() * 3600


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
        b["reference"] = compute_reference(b["sells"])
    return buckets


def compute_reference(sells):
    """What the item really costs if you don't take the cheapest listing.

    With a healthy book the median of the next few listings is robust. With a
    thin one it is not: [40, 44, 100] medians to 72, which overstates every
    discount measured against it. There the honest answer is simply the next
    price you would actually pay.

    Joke listings are dropped first. They barely move a healthy book's median,
    but on a thin one the second listing IS the reference, so a single 114,514p
    troll turns a 7p item into a fake 100%-off jackpot. A book left with fewer
    than two real listings says nothing, and None is the honest answer."""
    prices = [o["platinum"] for o in sells if o["platinum"] <= MAX_PLAUSIBLE_PLAT]
    if len(prices) >= MIN_BUCKET_ORDERS:
        return statistics.median(prices[1:6])
    if len(prices) >= 2:
        return prices[1]
    return None


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


# ---------------- builds (frames, weapons, companions) ----------------

# Helminth: you can subsume one ability onto a frame, replacing one of its own.
# The augment for a subsumed ability then becomes equippable on that frame,
# which is why the picker below unlocks it. These are the donations people
# actually use; the full list is far longer and mostly unused.
SUBSUMABLE = [
    {"ability": "Roar", "from": "Rhino", "augment": "Piercing Roar",
     "why": "a flat damage multiplier for you and the squad — the default pick"},
    {"ability": "Eclipse", "from": "Mirage", "augment": "Total Eclipse",
     "why": "huge damage buff in light, damage reduction in shadow"},
    {"ability": "Gloom", "from": "Sevagoth", "augment": "",
     "why": "slows everything nearby and heals you on kills"},
    {"ability": "Pillage", "from": "Hildryn", "augment": "Blazing Pillage",
     "why": "strips armour and shields and refills your own shields"},
    {"ability": "Nourish", "from": "Grendel", "augment": "",
     "why": "turns your damage Viral and fixes energy at the same time"},
    {"ability": "Dispensary", "from": "Protea", "augment": "Repair Dispensary",
     "why": "drops energy and health pickups on a timer"},
    {"ability": "Silence", "from": "Banshee", "augment": "Savage Silence",
     "why": "stuns everything that enters range — quiet, reliable safety"},
    {"ability": "Xata's Whisper", "from": "Xaku", "augment": "",
     "why": "adds void damage to your weapons"},
    {"ability": "Terrify", "from": "Nekros", "augment": "Creeping Terrify",
     "why": "armour strip that also scatters the room"},
    {"ability": "Tharros Strike", "from": "Styanax", "augment": "",
     "why": "instant armour and shield strip in a cone"},
    {"ability": "Elemental Ward", "from": "Chroma", "augment": "Everlasting Ward",
     "why": "big health or shield buff depending on element"},
    {"ability": "Warcry", "from": "Valkyr", "augment": "Eternal War",
     "why": "attack speed and armour for the squad"},
    {"ability": "Rest & Rage", "from": "Equinox", "augment": "Calm & Frenzy",
     "why": "sleep for stealth multipliers, or a damage debuff"},
    {"ability": "Condemn", "from": "Harrow", "augment": "Condemn Overshields",
     "why": "shields on cast plus crowd control"},
    {"ability": "Ensnare", "from": "Khora", "augment": "",
     "why": "groups enemies for anything that hits an area"},
    {"ability": "Larva", "from": "Nidus", "augment": "Larva Burst",
     "why": "the strongest grouping ability in the game"},
    {"ability": "Breach Surge", "from": "Wisp", "augment": "",
     "why": "chains blinding sparks off every hit"},
    {"ability": "Fire Blast", "from": "Ember", "augment": "Healing Flame",
     "why": "armour strip on a ring of fire"},
    {"ability": "Marked for Death", "from": "Kullervo", "augment": "",
     "why": "transfers the damage you deal to everything nearby"},
]

BUILD_PATH = APP_DIR / "build_guide.json"
BUILD_GUIDE = {}


def load_build_guide():
    """Curated build knowledge. Every mod, augment and arcane name in the file
    was validated against the mod list and the market catalogue — the point of
    keeping it as data is that it can be corrected without touching code."""
    global BUILD_GUIDE
    try:
        BUILD_GUIDE = json.loads(BUILD_PATH.read_text(encoding="utf-8"))
        print(f"build guide: {len(BUILD_GUIDE.get('frames', {}))} frame profiles "
              f"(updated {BUILD_GUIDE.get('updated', '?')})")
    except (OSError, ValueError) as e:
        print(f"build_guide.json unreadable ({e}); Builds tab will be limited")
        BUILD_GUIDE = {}
    return BUILD_GUIDE


RIVEN_PATH = APP_DIR / "riven_guide.json"
RIVEN_GUIDE = {}


def load_riven_guide():
    """What to roll a riven for, kept as data for the same reason: the roll
    multipliers are fixed by the game, but which negatives are harmless is a
    judgement that shifts with the meta."""
    global RIVEN_GUIDE
    try:
        RIVEN_GUIDE = json.loads(RIVEN_PATH.read_text(encoding="utf-8"))
        print(f"riven guide: {len(RIVEN_GUIDE.get('positives', {}))} archetypes "
              f"(updated {RIVEN_GUIDE.get('updated', '?')})")
    except (OSError, ValueError) as e:
        print(f"riven_guide.json unreadable ({e}); Rivens tab loses stat advice")
        RIVEN_GUIDE = {}
    return RIVEN_GUIDE


def riven_traits(w, focus):
    """Facts about this weapon that decide whether a negative is really free."""
    wtype = (w.get("wtype") or "").lower()
    grp = (w.get("grp") or "").lower()
    t = set()
    if "sniper" in wtype:
        t |= {"sniper", "small_magazine"}
    if "bow" in wtype:
        t |= {"bow", "projectile", "no_recoil"}
    if "launcher" in wtype or "throwing" in wtype:
        t |= {"launcher", "projectile"}
    # the game's own fire data beats inferring from the weapon type: Boltor is
    # a rifle that fires projectiles, and every beam weapon triggers on Held
    if (w.get("fire_trigger") or "") == "Held":
        t.add("beam")
    if (w.get("shot_type") or "") == "Projectile":
        t.add("projectile")
    if (w.get("fire_rate") or 0) >= 10:
        t.add("high_fire_rate")
    if focus == "status":
        t.add("status_build")
    if focus == "heavy":
        t.add("heavy_build")
    if grp == "melee" or "melee" in wtype or grp == "zaw":
        t.add("melee")
    # a physical type carrying most of the weapon's damage is not free to drop
    try:
        phys = json.loads(w.get("phys") or "{}")
    except (TypeError, ValueError):
        phys = {}
    if phys.get("impact", 0) >= 0.5:
        t.add("impact_heavy")
    if phys.get("puncture", 0) >= 0.5:
        t.add("puncture_heavy")
    if phys.get("slash", 0) >= 0.5:
        t.add("slash_build")     # Slash procs are the damage; keep the duration
    return t


def riven_stat_plan(w, focus):
    """Which stats to chase and which negative to accept, for THIS weapon.

    Deliberately independent of what rivens sell for: price tells you how good
    a particular roll is, not which roll the weapon wants."""
    g = RIVEN_GUIDE
    if not g:
        return None
    is_melee = ((w.get("grp") or "").lower() in ("melee", "zaw")
                or "melee" in (w.get("wtype") or "").lower())
    cls = "melee" if is_melee else "gun"
    key = f"{cls}_{focus}" if f"{cls}_{focus}" in (g.get("positives") or {}) \
        else ("melee_crit" if is_melee else "gun_crit")
    traits = riven_traits(w, focus)

    negatives, rejected = [], []
    for c in (g.get("negatives") or {}).get("candidates", []):
        if cls not in (c.get("classes") or []):
            continue
        blocked = traits & set(c.get("unsafe_when") or [])
        (rejected if blocked else negatives).append(
            {**{k: c[k] for k in ("stat", "why") if k in c},
             "verdict": c.get("verdict", ""),
             "blocked_by": sorted(blocked) or None})
    order = {"best": 0, "good": 1, "situational": 2}
    negatives.sort(key=lambda x: order.get(x["verdict"], 9))

    avoid = []
    for a in (g.get("negatives") or {}).get("avoid", []):
        s = a["stat"].lower()
        if not is_melee and s in ("attack speed", "melee range"):
            continue
        if is_melee and s == "fire rate":
            continue
        if s == "status chance" and focus != "status":
            continue
        if s in ("critical chance", "critical damage") and focus == "status":
            continue
        avoid.append(a)

    attrs = g.get("attributes") or {}
    # the two classes roll genuinely different attribute sets, so each is
    # listed in full rather than derived from a shared pool by subtraction
    rollable = list(attrs.get("melee" if is_melee else "gun", []))
    negatable = [a for a in rollable if a not in attrs.get("positive_only", [])]

    return {"archetype": key, "positives": (g.get("positives") or {}).get(key, []),
            "negatives": negatives, "negatives_rejected": rejected, "avoid": avoid,
            "rollable": sorted(set(rollable)), "negatable": sorted(set(negatable)),
            "mechanics": g.get("mechanics", {}), "updated": g.get("updated", ""),
            "sources": g.get("sources", [])}


# The auction API names stats its own way. This maps every key that actually
# appears in live auctions onto the vocabulary the guide and grader use, so a
# listing can be scored by the same rules as a roll you type in yourself.
AUCTION_STAT_MAP = {
    "base_damage_/_melee_damage": "Damage", "critical_chance": "Critical Chance",
    "critical_damage": "Critical Damage", "multishot": "Multishot",
    "status_chance": "Status Chance", "status_duration": "Status Duration",
    "toxin_damage": "Toxin", "heat_damage": "Heat", "cold_damage": "Cold",
    "electric_damage": "Electricity", "impact_damage": "Impact",
    "puncture_damage": "Puncture", "slash_damage": "Slash",
    "damage_vs_corpus": "Damage vs Corpus", "damage_vs_grineer": "Damage vs Grineer",
    "damage_vs_infested": "Damage vs Infested", "punch_through": "Punch Through",
    "reload_speed": "Reload Speed", "magazine_capacity": "Magazine Capacity",
    "ammo_maximum": "Ammo Maximum", "zoom": "Zoom", "recoil": "Weapon Recoil",
    "projectile_speed": "Projectile Speed", "range": "Melee Range",
    "combo_duration": "Combo Duration", "finisher_damage": "Finisher Damage",
    "channeling_efficiency": "Heavy Attack Efficiency",
    "critical_chance_on_slide_attack": "Slide Attack Critical Chance",
    "chance_to_gain_combo_count": "Additional Combo Count Chance",
    "chance_to_gain_extra_combo_count": "Additional Combo Count Chance",
    # heavy attack damage predates the current stat set and has no equivalent
    # in the guide; it scores as an off-build stat rather than being dropped
    "channeling_damage": "Heavy Attack Damage",
}


def auction_stat(key, is_melee):
    if key == "fire_rate_/_attack_speed":
        return "Attack Speed" if is_melee else "Fire Rate"
    return AUCTION_STAT_MAP.get(key)


def riven_market_offers(conn, slug, style="", limit=25):
    """Grade every riven for this weapon that is actually for sale right now.

    The app already knows what a good roll looks like and already reads the
    auction house. Putting the two together answers the question people
    actually have — not "what should I roll for" but "is there one worth
    buying, and which".  Offline sellers are dropped for the same reason they
    are dropped everywhere else: you cannot trade with them."""
    row = conn.execute("SELECT * FROM riven_weapons WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    w = dict(row)
    is_melee = ((w.get("grp") or "").lower() in ("melee", "zaw")
                or "melee" in (w.get("wtype") or "").lower())
    d = get_json("https://api.warframe.market/v1/auctions/search"
                 f"?type=riven&weapon_url_name={urllib.parse.quote(slug)}"
                 "&sort_by=price_asc")
    if d is None:
        # a throttled or failed fetch is not the same as "nobody is selling
        # one", and saying the latter would be a lie
        return {"weapon": w["name"], "slug": slug, "style": style or "default",
                "offers": [], "graded": 0, "skipped_offline": 0,
                "unavailable": True,
                "note": "the auction house did not answer just now — usually "
                        "rate limiting, and it clears on its own"}
    auctions = ((d or {}).get("payload") or {}).get("auctions") or []
    out, skipped_offline = [], 0
    for a in auctions:
        owner = a.get("owner") or {}
        if owner.get("status") not in ("ingame", "online"):
            skipped_offline += 1
            continue
        price = a.get("buyout_price") or a.get("starting_price")
        if not price:
            continue
        it = a.get("item") or {}
        pos, neg, shown = [], "", []
        for at in it.get("attributes", []):
            name = auction_stat(at.get("url_name"), is_melee)
            if not name:
                continue
            shown.append({"stat": name, "value": at.get("value"),
                          "positive": bool(at.get("positive"))})
            if at.get("positive"):
                pos.append(name)
            else:
                neg = name
        if not pos:
            continue
        g = grade_riven(conn, slug, pos[:3], neg, style)
        if not g:
            continue
        out.append({"price": price, "grade": g["grade"], "score": g["score"],
                    "verdict": g["verdict"], "shape": g["shape"],
                    "stats": shown, "seller": owner.get("ingame_name"),
                    "status": owner.get("status"),
                    "rerolls": it.get("re_rolls"), "rank": it.get("mod_rank"),
                    "mastery": it.get("mastery_level")})
    rank = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    out.sort(key=lambda x: (rank.get(x["grade"], 9), x["price"]))
    return {"weapon": w["name"], "slug": slug, "style": style or "default",
            "offers": out[:limit], "graded": len(out),
            "skipped_offline": skipped_offline,
            "note": "Graded against what this weapon wants, not against what "
                    "the roll would sell for. Offline sellers are left out."}


def grade_riven(conn, slug, positives, negative, style=""):
    """Score a riven you actually own against what this weapon wants.

    The shape multipliers are the game's own, so a 3+1 really does score above
    a 3+0 — the thing most people get backwards when they see a 'clean' roll."""
    row = conn.execute("SELECT * FROM riven_weapons WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    w = dict(row)
    focus = melee_style_focus(w, build_advice(w), style)
    plan = riven_stat_plan(w, focus)
    if not plan:
        return None

    want = [p["stat"] for p in plan["positives"]]
    safe = {n["stat"]: n["verdict"] for n in plan["negatives"]}
    blocked = {n["stat"] for n in plan["negatives_rejected"]}
    avoid = {a["stat"] for a in plan["avoid"]}

    notes, score = [], 0.0
    for s in positives:
        if s in want:
            rank = want.index(s)
            pts = [3.0, 2.5, 2.0, 1.5][rank] if rank < 4 else 1.0
            notes.append(f"+{pts:.1f}  {s} is one of this weapon's wanted stats"
                         f" (#{rank + 1} of {len(want)})")
        else:
            pts = 0.5
            notes.append(f"+0.5  {s} does nothing for the build, but costs you "
                         f"only the slot it occupies")
        score += pts

    # A negative that attacks the build is not worth a fixed number of points —
    # it degrades everything the riven does, so it scales the whole roll. A flat
    # penalty let a -Attack Speed melee riven tie with a clean one.
    if negative:
        if negative in avoid:
            score *= 0.45
            notes.append(f"x0.45  {negative} as the negative attacks the build "
                         f"itself — it drags down every other stat on the mod")
        elif negative in blocked:
            score *= 0.8
            notes.append(f"x0.8  {negative} is usually harmless, but not on "
                         f"this weapon")
        elif negative in safe:
            bump = {"best": 1.0, "good": 0.75, "situational": 0.4}[safe[negative]]
            score += bump
            notes.append(f"+{bump}  {negative} is a free negative here")
        else:
            notes.append(f"+0.0  {negative} is an unusual negative — judge it yourself")

    shape = f"{len(positives)} positives" + (" + 1 negative" if negative else ", no negative")
    mult = {("2", True): 1.2375, ("3", True): 0.9375,
            ("2", False): 0.99, ("3", False): 0.75}.get(
                (str(len(positives)), bool(negative)), 1.0)
    score *= mult
    notes.append(f"x{mult}  {shape}: the game rolls every value at this multiplier")

    disp = w["disposition"] or 1.0
    score *= disp
    notes.append(f"x{disp}  disposition — the same stats are worth this much here")

    grade = ("S" if score >= 9 else "A" if score >= 7 else "B" if score >= 5
             else "C" if score >= 3 else "D")
    verdict = {"S": "God roll for this weapon — keep it",
               "A": "Strong roll — worth ranking and using",
               "B": "Usable, but you would reroll it if kuva is cheap",
               "C": "Weak roll — reroll",
               "D": "Reroll"}[grade]
    return {"weapon": w["name"], "slug": slug, "grade": grade,
            "score": round(score, 2), "verdict": verdict, "shape": shape,
            "notes": notes, "archetype": plan["archetype"], "wanted": want}


# Stat lines come through as text like "+30% Ability Strength". Map the ones
# that actually change a build's numbers; everything else stays as prose.
STAT_KEYS = {
    "ability strength": "strength", "ability duration": "duration",
    "ability efficiency": "efficiency", "ability range": "range",
    "health": "health", "shield capacity": "shield", "armor": "armor",
    "energy max": "energy", "sprint speed": "sprint",
}
STAT_LINE = re.compile(r"^([+-][\d.]+)(%?)\s+(.+?)\s*$")

# Exilus-capable utility mods are not flagged in the data, so this is the
# list of what the game actually allows there.
EXILUS_HINTS = ("parkour velocity", "sprint speed", "mobility", "aim glide",
                "bullet jump", "slide", "friction", "enemy radar",
                "loot radar", "vacuum", "maglev", "rush", "drift")


# You cannot equip two mods from the same family: Stretch and Archon Stretch
# fight for the same slot in the game, as do Intensify / Umbral Intensify and
# Continuity / Primed Continuity / Archon Continuity. Stripping the variant
# prefix gives the family; a few pairs are renamed rather than prefixed.
MOD_PREFIXES = ("primed ", "umbral ", "archon ", "flawed ", "sacrificial ")
MOD_FAMILY_ALIASES = {
    "fiber": "steel fiber",            # Umbral Fiber ↔ Steel Fiber
    "sacrificial steel": "true steel",
    "sacrificial pressure": "pressure point",
}


# The game groups mods by their internal path, not their display name. Split
# Chamber and Galvanized Chamber are one mod as far as the game is concerned,
# and nothing in either name says so. These are the path segments that mark a
# variant of the same mod rather than a different mod.
MOD_PATH_CLASSES = {"Rifle", "Shotgun", "Pistol", "Melee", "Warframe",
                    "Powersuits", "Archwing", "Sentinel", "Kubrow", "Kavat",
                    "Companion", "Aura", "Zaw", "Kitgun"}
MOD_PATH_VARIANTS = ("SP", "Beginner", "Intermediate", "Expert", "Kahl",
                     "Umbra", "Primed")
# the status mods predate the rename from "stun" to "status"
MOD_PATH_RENAME = {"WeaponStunChance": "WeaponStatusChance"}
MOD_FAMILIES = {}          # mod name -> canonical family key


def mod_path_family(unique_name):
    if not unique_name or "/Upgrades/Mods/" not in unique_name:
        return None
    parts = unique_name.split("/")
    leaf = re.sub(r"Mod$", "", parts[-1])
    for tok in MOD_PATH_VARIANTS:
        leaf = leaf.replace(tok, "")
    leaf = MOD_PATH_RENAME.get(leaf, leaf)
    if not leaf:
        return None
    cls = next((p for p in parts if p in MOD_PATH_CLASSES), "")
    return f"{cls}:{leaf}"


def build_mod_families(conn):
    """Merge the two things that make mods exclusive into one map.

    The name rule catches Primed/Umbral/Archon/Sacrificial variants; the path
    rule catches the ones whose names share nothing at all, like Split Chamber
    with Galvanized Chamber or Argon Scope with Galvanized Scope. A mod linked
    by either rule belongs to one family, so the two are unioned rather than
    picked between."""
    global MOD_FAMILIES
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    rows = conn.execute("SELECT name, unique_name FROM game_mods").fetchall()
    for r in rows:
        name = r["name"]
        union(name, "name:" + mod_family_by_name(name))
        pf = mod_path_family(r["unique_name"])
        if pf:
            union(name, "path:" + pf)
    fams = {}
    for r in rows:
        fams[r["name"]] = find(r["name"])
    MOD_FAMILIES = fams
    groups = {}
    for n, f in fams.items():
        groups.setdefault(f, []).append(n)
    shared = sum(1 for v in groups.values() if len(v) > 1)
    print(f"mod families: {len(groups)} ({shared} with more than one mod)")
    return fams


def mod_family_by_name(name):
    n = (name or "").strip().lower()
    for p in MOD_PREFIXES:
        if n.startswith(p):
            n = n[len(p):]
            break
    return MOD_FAMILY_ALIASES.get(n, n)


def mod_family(name):
    """Two mods sharing a family cannot be equipped together, same as in game."""
    fam = MOD_FAMILIES.get(name)
    return fam if fam else "name:" + mod_family_by_name(name)


def parse_mod_stats(level_stats):
    """Turn the max-rank stat lines into numbers a build can add up."""
    out, prose = {}, []
    for raw in (level_stats or [{}])[-1].get("stats", []) or []:
        s = re.sub(r"<[^>]+>", "", str(raw)).strip()
        m = STAT_LINE.match(s)
        if not m:
            prose.append(s)
            continue
        value, _, label = m.groups()
        key = STAT_KEYS.get(label.strip().lower())
        if key:
            out[key] = out.get(key, 0) + float(value)
        else:
            prose.append(s)
    return out, prose


def mods_refresh(conn):
    """Cache every mod with its polarity, cost, compatibility and effects."""
    raw = get_json("https://api.warframestat.us/mods")
    if not raw:
        return False
    # 138 names appear more than once: the dataset carries Beginner /
    # Intermediate / Expert training variants alongside the real mod, and they
    # all share a name. The unqualified uniqueName is the one people equip —
    # taking the wrong one gave Intensify +55% strength instead of +30%.
    best = {}
    for m in raw:
        name = m.get("name")
        if not name or not (m.get("type") or ""):
            continue
        path = m.get("uniqueName") or ""
        variant = any(seg in path for seg in
                      ("/Beginner/", "/Intermediate/", "/Expert/"))
        prev = best.get(name)
        if prev is None or (prev[0] and not variant):
            best[name] = (variant, m)
    rows = []
    for name, (_, m) in best.items():
        mtype = m.get("type") or ""
        compat = (m.get("compatName") or "").strip()
        stats, prose = parse_mod_stats(m.get("levelStats"))
        blob = " ".join(prose).lower() + " " + name.lower()
        if compat == "AURA":
            slot = "aura"
        elif mtype.startswith("Warframe"):
            slot = ("exilus" if any(h in blob or h in " ".join(stats).lower()
                                    for h in EXILUS_HINTS) else "warframe")
        elif "Primary" in mtype or "Rifle" in mtype or "Shotgun" in mtype:
            slot = "primary"
        elif "Secondary" in mtype or "Pistol" in mtype:
            slot = "secondary"
        elif "Melee" in mtype:
            slot = "melee"
        elif any(k in mtype for k in ("Companion", "Sentinel", "Kubrow",
                                      "Kavat", "Pet", "Beast")):
            slot = "companion"
        else:
            slot = "other"
        # Each rank shifts drain by one, away from zero. Auras start negative
        # and get more negative, because they *grant* capacity rather than
        # spending it — adding here would have shown Corrosive Projection
        # as a cost instead of a +7 bonus.
        bd, ranks = (m.get("baseDrain") or 0), (m.get("fusionLimit") or 0)
        drain = bd - ranks if bd < 0 else bd + ranks
        rows.append((name, mtype, m.get("polarity"), drain, compat or "ANY",
                     m.get("rarity"), slot, json.dumps(stats),
                     " · ".join(prose)[:400], m.get("uniqueName") or ""))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO game_mods(name, mtype, polarity, drain,"
            " compat, rarity, slot, stats, text, unique_name)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    build_mod_families(conn)
    print(f"mods: {len(rows)} cached "
          f"({sum(1 for r in rows if r[6] == 'warframe')} warframe, "
          f"{sum(1 for r in rows if r[6] == 'aura')} aura)")
    return True


def vault_refresh(conn):
    """Work out which prime items are farmable right now, and which are vaulted.

    This is the single biggest price driver the app was blind to. A prime's
    price is mostly a function of whether its relics still drop: vaulted means
    the supply is fixed until a Prime Resurgence rotation, farmable means new
    copies flow in every day. warframe.market's relic entries carry a vaulted
    flag (Varzia's monthly resurgence relics count as unvaulted, which is
    exactly right for supply); the rewards of the unvaulted relics are the
    complete list of prime parts entering the game."""
    d = get_json(f"{API}/items")
    if not d or "data" not in d:
        return False
    open_relics = [it["slug"] for it in d["data"]
                   if it.get("vaulted") is False]
    if not open_relics:
        return False    # a feed hiccup must not mark the whole market vaulted
    farmable_parts = set()
    fetched = 0
    for slug in open_relics:
        # market slug "axi_a15_relic" -> warframestat name "Axi A15"
        name = slug.rsplit("_relic", 1)[0].replace("_", " ").title()
        ws = get_json("https://api.warframestat.us/items/"
                      + urllib.parse.quote(name))
        if isinstance(ws, list):
            ws = next((x for x in ws if x.get("rewards")), ws[0] if ws else {})
        for rw in (ws or {}).get("rewards") or []:
            n = (rw.get("item") or {}).get("name")
            if n:
                farmable_parts.add(n)
        fetched += 1
        time.sleep(0.2)
    if not farmable_parts:
        return False
    # Classify prime gear by name: "X Prime ..." but never "Primed X" mods.
    farm = vaulted = 0
    with conn:
        conn.execute("UPDATE items SET vault=NULL")
        primes = conn.execute(
            "SELECT slug, name FROM items WHERE name LIKE '% Prime%' "
            "AND name NOT LIKE 'Primed %'").fetchall()
        by_name = {}
        for r in primes:
            by_name[r["name"]] = r["slug"]
            if not r["name"].endswith(" Set"):
                status = ("farmable" if r["name"] in farmable_parts
                          else "vaulted")
                conn.execute("UPDATE items SET vault=? WHERE slug=?",
                             (status, r["slug"]))
                farm += status == "farmable"
                vaulted += status == "vaulted"
        # A set's supply is gated by its scarcest part: farmable only when
        # every part still drops, vaulted when none do.
        part_names = {r["slug"]: r["name"] for r in primes}
        for row in conn.execute(
                "SELECT DISTINCT set_slug FROM set_parts").fetchall():
            sslug = row["set_slug"]
            if sslug not in {v for v in by_name.values()}:
                continue
            parts = [r["part_slug"] for r in conn.execute(
                "SELECT part_slug FROM set_parts WHERE set_slug=?", (sslug,))]
            states = [part_names.get(p) in farmable_parts
                      for p in parts if p in part_names]
            if not states:
                continue
            status = ("farmable" if all(states)
                      else "vaulted" if not any(states) else "partial")
            conn.execute("UPDATE items SET vault=? WHERE slug=?",
                         (status, sslug))
    print(f"prime vault: {len(open_relics)} relics farmable, "
          f"{len(farmable_parts)} parts dropping — {farm} prime items "
          f"farmable, {vaulted} vaulted")
    return True


def frames_refresh(conn):
    """Cache warframe stats and abilities."""
    raw = get_json("https://api.warframestat.us/warframes")
    if not raw:
        return False
    rows = []
    for f in raw:
        if f.get("type") != "Warframe" or not f.get("name"):
            continue
        rows.append((f["name"], f.get("health"), f.get("shield"), f.get("armor"),
                     f.get("power"), f.get("sprintSpeed"), f.get("masteryReq"),
                     1 if f.get("isPrime") else 0,
                     json.dumps([a.get("name") for a in f.get("abilities") or []]),
                     (f.get("description") or "")[:400]))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO frames(name, health, shield, armor, energy,"
            " sprint, mastery, is_prime, abilities, description)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"warframes: {len(rows)} cached")
    return True


def frame_family(name):
    """Khora and Khora Prime share a build — profiles are keyed on the base."""
    return re.sub(r"\s+(prime|umbra)$", "", (name or "").strip(), flags=re.I)


# Which arcane goes where, by name family. Only the ones actually checked are
# marked verified: Cascadia reads like a Warframe arcane and is a secondary one,
# Residual reads like a Warframe arcane and is a Kitgun one. Guessing the rest
# would put illegal arcanes in front of you, so they stay labelled unknown.
ARCANE_FAMILIES = [
    ("Magus ", "operator", True),
    ("Virtuos ", "amp", True),
    ("Exodia ", "zaw", True),
    ("Pax ", "kitgun", True),
    ("Residual ", "kitgun", True),
    ("Cascadia ", "secondary", True),
    ("Primary ", "primary", True),
    ("Secondary ", "secondary", True),
    ("Melee ", "melee", True),
    ("Molt ", "warframe", True),
]


def arcane_category(name):
    if "Helmet" in name:
        return None, False          # Arcane Helmets are cosmetics, not arcanes
    if "Riven Mod" in name:
        return None, False          # "Melee Riven Mod (Veiled)" is not an arcane
    for prefix, cat, ok in ARCANE_FAMILIES:
        if name.startswith(prefix):
            return cat, ok
    # the wiki publishes the Warframe arcane table by name, so membership of
    # that list is a checked fact rather than an inference from the prefix
    arc = BUILD_GUIDE.get("arcanes") or {}
    if name in set(arc.get("warframe_names", [])):
        return "warframe", True
    if name in set(arc.get("operator_names", [])):
        return "operator", True
    if name.startswith(("Arcane ", "Theorem ", "Emergence ")):
        # whatever is left shares the prefix but is not on that table, so the
        # honest answer is that we do not know rather than a guess
        return "unclassified", False
    return None, False


def arcane_catalogue(conn):
    """Every arcane the market lists, with what it costs right now.

    The wiki tells you what an arcane does; only this app knows what it is
    going for today, which is the part that decides what you actually buy."""
    arc = BUILD_GUIDE.get("arcanes") or {}
    # the hand-written entries carry the "why bother" reasoning; the effects
    # table carries the exact max-rank numbers straight from the game data
    documented = {}
    for grp in ("warframe", "weapon"):
        for a in arc.get(grp, []):
            documented[a["name"]] = a
    effects = arc.get("effects") or {}
    out = []
    for r in conn.execute("SELECT slug, name FROM items ORDER BY name"):
        cat, verified = arcane_category(r["name"])
        if not cat:
            continue
        snap = conn.execute(
            "SELECT low, sellers, ts FROM snapshots WHERE slug=? AND low IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (r["slug"],)).fetchone()
        doc = documented.get(r["name"])
        fx = effects.get(r["name"]) or {}
        out.append({"slug": r["slug"], "name": r["name"], "category": cat,
                    "category_verified": verified,
                    "low": snap["low"] if snap else None,
                    "sellers": snap["sellers"] if snap else None,
                    "age_min": (round((time.time() - snap["ts"]) / 60)
                                if snap else None),
                    "effect": fx.get("effect") or (doc["effect"] if doc else ""),
                    "ranks": fx.get("ranks"), "rarity": fx.get("rarity"),
                    "why": doc["why"] if doc else ""})
    have = sum(1 for x in out if x["effect"])
    return {"arcanes": out, "with_effect": have,
            "slots": arc.get("slots", {}), "note": arc.get("note", ""),
            "effects_source": arc.get("effects_source", "")}


def weapon_build_profile(w, focus):
    """A fittable build for a weapon, in the same shape as a frame profile so
    the loadout screen can load it without knowing the difference.

    Chosen from the weapon's own crit/status numbers rather than curated per
    weapon: there are 418 riven-capable weapons and the archetype is what
    actually decides the mods."""
    grp = (w.get("grp") or "").lower()
    wtype = (w.get("wtype") or "").lower()
    if grp in ("melee", "zaw") or "melee" in wtype:
        kind = "melee"
    elif "shotgun" in wtype or "shotgun" in grp:
        kind = "shotgun"
    elif grp in ("secondary", "pistol", "kitgun") or "pistol" in wtype:
        kind = "secondary"
    else:
        kind = "primary"
    builds = BUILD_GUIDE.get("weapon_builds") or {}
    for key in (f"{kind}_{focus}", f"{kind}_crit", "primary_crit"):
        if key in builds:
            return {**builds[key], "archetype": key}
    return None


def companion_build_profile(name):
    c = (BUILD_GUIDE.get("companion_builds") or {}).get("survivability")
    return {**c, "archetype": "companion"} if c else None


def frame_build(conn, name):
    row = conn.execute("SELECT * FROM frames WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    f = dict(row)
    f["abilities"] = json.loads(f.get("abilities") or "[]")
    base = frame_family(name)
    profile = (BUILD_GUIDE.get("frames") or {}).get(base)
    survivability = []
    # Derive a survivability read from the frame's real stats: these are the
    # numbers that decide whether it wants armour scaling or shield gating.
    armor, shield, health = f.get("armor") or 0, f.get("shield") or 0, f.get("health") or 0
    if armor >= 250:
        survivability.append(f"High armour ({armor}) — armour scales multiplicatively "
                             f"with health, so Umbral Fiber and Umbral Vitality "
                             f"together are worth more than either alone.")
    elif shield >= 400:
        survivability.append(f"Big shield pool ({shield}) — shield gating gives you "
                             f"brief invulnerability on every shield break; "
                             f"Catalyzing Shields and Rolling Guard lean into it.")
    elif armor < 150 and health <= 400:
        survivability.append(f"Thin defensively ({health} health, {armor} armour) — "
                             f"this frame survives through its abilities, not its "
                             f"stat sheet. Adaptation and Rolling Guard are close "
                             f"to mandatory in Steel Path.")
    if (f.get("energy") or 0) >= 200:
        survivability.append(f"Large energy pool ({f['energy']}) — Primed Flow plus "
                             f"Arcane Energize turns that into constant casting.")
    return {"kind": "frame", "name": name, "family": base, "stats": f,
            "profile": profile, "derived": survivability,
            "shards": (BUILD_GUIDE.get("shards") or {}),
            "universal": (BUILD_GUIDE.get("universal") or {}),
            "damage_mechanics": (BUILD_GUIDE.get("damage_mechanics") or {}),
            "arcanes": (BUILD_GUIDE.get("arcanes") or {}),
            "updated": BUILD_GUIDE.get("updated"),
            "sources": BUILD_GUIDE.get("sources", [])}


# ---------------- rivens ----------------

# Riven stat names as the auction API spells them, mapped to plain English.
RIVEN_STAT_NAMES = {
    "critical_chance": "Critical Chance", "critical_damage": "Critical Damage",
    "base_damage_/_melee_damage": "Damage", "multishot": "Multishot",
    "fire_rate_/_attack_speed": "Fire Rate / Attack Speed",
    "toxin_damage": "Toxin", "heat_damage": "Heat", "cold_damage": "Cold",
    "electric_damage": "Electricity", "impact_damage": "Impact",
    "puncture_damage": "Puncture", "slash_damage": "Slash",
    "status_chance": "Status Chance", "status_duration": "Status Duration",
    "damage_vs_corpus": "Damage to Corpus", "damage_vs_grineer": "Damage to Grineer",
    "damage_vs_infested": "Damage to Infested", "punch_through": "Punch Through",
    "reload_speed": "Reload Speed", "magazine_capacity": "Magazine Capacity",
    "ammo_maximum": "Ammo Maximum", "zoom": "Zoom", "recoil": "Recoil",
    "projectile_speed": "Projectile Speed", "range": "Range",
    "combo_duration": "Combo Duration",
    "chance_to_gain_combo_count": "Extra Combo Count",
    "finisher_damage": "Finisher Damage",
    "channeling_damage": "Channeling Damage",
    "channeling_efficiency": "Channeling Efficiency",
    "critical_chance_on_slide_attack": "Slide Attack Crit Chance",
}


# Community meta standing, cross-checked across dated tier lists (see
# META_SOURCES). Keyed by weapon family, because a riven covers the family.
# Tier is the consensus; where lists genuinely disagree that is recorded
# rather than smoothed over, since a single list would be misleading.
META_AS_OF = "June 2026"
META_SOURCES = [
    "immortalboost.com weapon guide (June 2026)",
    "sportskeeda + warframetierlist Incarnon tier lists (July 2026)",
    "thegamer.com Coda weapon tier list (May 2025)",
    "ofzenandcomputing.com weapon tier list (updated June 2026)",
    "warframetierlist.com primary tier list (2026)",
]
# Keys are EXACT riven-weapon names (the family name — a Kuva Zarr uses a
# "Zarr" riven), verified against the live list, so no fuzzy matching can put
# e.g. Ether Reaper on Reaper Prime's tier.
# "inc" marks an Incarnon Genesis weapon: the adapter transforms these, so the
# tier is for the Incarnon form, not the base weapon you first build.
META_TIERS = {
    # primaries
    "Torid": ("S", True, "best Incarnon primary of 2026 — scaling gas clouds"),
    "Latron": ("S", True, "armour-stripping precision rifle"),
    "Phenmor": ("S", True, "burst damage, headshot scaling"),
    "Nataruk": ("S", False, "best non-Incarnon bow"),
    "Arca Plasmor": ("S", False, "Tenet Arca Plasmor is the strong variant"),
    "Felarx": ("S", True, ""),
    "Bubonico": ("S", False, ""),
    "Cedo": ("S", False, ""),
    "Trumna": ("S", False, ""),
    "Fulmin": ("S", False, ""),
    "Rubico": ("S", False, "sniper benchmark"),
    "Zarr": ("A", False, "Kuva Zarr is the strong variant"),
    "Proboscis Cernos": ("A", False, ""),
    "Acceltra": ("A", False, ""),
    "Tonkor": ("A", False, "Kuva Tonkor is the strong variant"),
    "Hek": ("A", False, "Vaykor Hek is the strong variant"),
    "Tigris": ("A", False, ""),
    "Tenora": ("A", False, ""),
    "Kuva Chakkhurr": ("A", False, ""),
    "Phantasma": ("A", False, ""),
    "Astilla": ("A", False, ""),
    "Stahlta": ("A", False, ""),
    "Dread": ("A", True, ""),
    "Burston": ("A", True, "weak base weapon, strong Incarnon"),
    "Sybaris": ("B", True, ""),
    "Boltor": ("B", True, "only worthwhile in its Incarnon form"),
    "Strun": ("B", True, "only worthwhile in its Incarnon form"),
    "Vectis": ("B", True, "new Incarnon, June 2026"),
    "Ignis": ("B", False, ""),
    "Soma": ("B", False, ""),
    "Kuva Bramma": ("B", False, "disputed: newer lists drop it from S over "
                                "damage falloff nerfs, older ones rate it top"),
    "Miter": ("C", True, ""),
    "Braton": ("C", True, ""),
    "Boar": ("C", True, ""),
    "Supra": ("C", False, ""),
    # secondaries
    "Laetum": ("S", True, "often called the best Incarnon outright"),
    "Dual Toxocyst": ("S", True, "chains with Viral"),
    "Nukor": ("S", False, "Kuva Nukor: chaining beam, armour strip"),
    "Epitaph": ("S", False, ""),
    "Cycron": ("S", False, "Tenet Cycron is the strong variant"),
    "Sporelacer": ("S", False, "kitgun"),
    "Plinx": ("S", False, "Tenet Plinx is the strong variant"),
    "Grimoire": ("S", False, ""),
    "Pyrana": ("A", False, ""),
    "Akstiletto": ("A", False, ""),
    "Zymos": ("A", False, ""),
    "Catchmoon": ("A", False, "kitgun"),
    "Brakk": ("A", False, "Kuva Brakk is the strong variant"),
    "Detron": ("A", False, "Mara Detron is the strong variant"),
    "Cyanex": ("A", False, ""),
    "Atomos": ("A", False, ""),
    "Angstrum": ("B", True, ""),
    "Furis": ("B", True, ""),
    "Ballistica": ("B", True, "new Incarnon, June 2026"),
    "Euphona Prime": ("B", False, ""),
    "Rattleguts": ("B", False, "kitgun"),
    "Tombfinger": ("B", False, "kitgun"),
    "Stug": ("C", True, "new Incarnon, June 2026"),
    # Coda (Infested lich) weapons from Warframe 1999: Techrot Encore.
    # Only three carry their own riven; the rest share the base Infested
    # weapon's riven, so a Coda Mire is judged under "Mire".
    "Coda Bassocyst": ("S", False, "strongest Coda weapon — Infested Arca "
                                   "Plasmor with Radiation/Blast and a Magnetic "
                                   "debuff on alt-fire"),
    "Coda Motovore": ("B", False, "Infested hammer, good crit for its class"),
    "Dual Coda Torxica": ("B", False, "Cold spores spread on kill"),
    "Pathocyst": ("S", False, "the Coda Pathocyst is S tier"),
    "Mire": ("S", False, "the Coda Mire is called the best sword in the game"),
    "Synapse": ("A", False, "rated on the Coda Synapse"),
    "Catabolyst": ("A", False, "rated on the Coda Catabolyst"),
    "Sporothrix": ("A", False, "rated on the Coda Sporothrix"),
    "Caustacyst": ("A", False, "rated on the Coda Caustacyst"),
    "Tysis": ("B", False, "rated on the Coda Tysis"),
    "Pox": ("C", False, "the Coda Pox underperforms for its mastery rank"),
    "Hema": ("C", False, "the Coda Hema underperforms for its mastery rank"),
    # melee
    "Praedos": ("S", True, "top-tier Incarnon melee"),
    "Kronen": ("S", False, "widely named best all-round melee"),
    "Glaive": ("S", False, "hybrid thrown/melee"),
    "Gram": ("S", False, ""),
    "Redeemer": ("S", False, ""),
    "Paracesis": ("S", False, ""),
    "Stropha": ("S", False, ""),
    "Sepfahn": ("S", False, "zaw"),
    "Innodem": ("S", True, ""),
    "Vitrica": ("S", False, ""),
    "Dual Ichor": ("S", False, ""),
    "Hate": ("S", False, ""),
    "Galatine": ("A", False, ""),
    "Nikana": ("A", False, ""),
    "Dragon Nikana": ("A", False, ""),
    "Hirudo": ("A", False, "the Coda Hirudo variant rates a step lower, at B"),
    "Tatsu": ("A", False, ""),
    "Fragor": ("A", False, ""),
    "Reaper Prime": ("A", False, ""),
    "Skana": ("B", True, ""),
    "Destreza": ("B", True, "new Incarnon, June 2026"),
    "Obex": ("B", True, "new Incarnon, June 2026"),
    "Orthos": ("B", False, ""),
    "Lecta": ("B", False, ""),
    "Ceramic Dagger": ("B", True, "finisher builds"),
}


def riven_stat_label(url_name):
    return RIVEN_STAT_NAMES.get(url_name,
                                url_name.replace("_", " ").replace("/", "/").title())


def phys_split(w):
    """How a weapon's physical damage divides into Impact, Puncture and Slash.

    This is what makes -Impact a free riven negative on one weapon and a real
    loss on another: Rubico is mostly Impact, Boltor mostly Puncture, and
    Kronen mostly Slash — which also decides whether Hunter Munitions is worth
    a slot."""
    atk = (w.get("attacks") or [{}])[0].get("damage") or {}
    phys = {k: float(atk.get(k) or 0) for k in ("impact", "puncture", "slash")}
    total = sum(phys.values())
    if total <= 0:
        return {}
    return {k: round(v / total, 3) for k, v in phys.items()}


def riven_refresh(conn):
    """Cache the riven-capable weapon list plus each weapon's combat stats.

    warframe.market is authoritative for which weapons can have rivens and for
    disposition; warframestat.us supplies the stats we judge the weapon on.
    Note rivens are per weapon *family*: a Kuva Nukor uses a Nukor riven and a
    Vasto Prime uses a Vasto riven, which is why the market list is shorter
    than the weapon list."""
    wfm = get_json(f"{API}/riven/weapons")
    if not wfm or "data" not in wfm:
        return False
    raw = get_json("https://api.warframestat.us/weapons") or []

    def score(s):
        fire = s.get("fireRate") or 1.0
        crit = s.get("criticalChance") or 0.0
        cmul = s.get("criticalMultiplier") or 1.0
        dmg = s.get("totalDamage") or 0.0
        shot = s.get("multishot") or 1.0
        # crude sustained-damage yardstick, only ever compared against weapons
        # in the same class — never presented as an absolute number
        return dmg * fire * shot * (1 + crit * max(cmul - 1, 0))

    # A riven covers the whole weapon family, so judge the family by its best
    # member: a Nukor riven is valuable because the *Kuva* Nukor is excellent,
    # even though the plain Nukor is not.
    VARIANTS = ("kuva", "tenet", "prisma", "wraith", "vandal", "prime", "dex",
                "rakta", "secura", "telos", "sancti", "synoid", "vaykor",
                "mk1", "mara", "ceti", "carmine")

    def core(name):
        parts = [p for p in re.split(r"[\s\-]+", (name or "").lower()) if p]
        parts = [p for p in parts if p not in VARIANTS]
        return " ".join(parts)

    family = {}
    for w in raw:
        if w.get("name"):
            family.setdefault(core(w["name"]), []).append(w)

    rows = []
    for it in wfm["data"]:
        name = (it.get("i18n") or {}).get("en", {}).get("name") or it.get("slug")
        members = family.get(core(name), [])
        exact = next((m for m in members
                      if (m.get("name") or "").lower() == (name or "").lower()), None)
        best = max(members, key=score) if members else {}
        s = exact or best or {}
        rating = score(best) if best else 0.0
        best_name = best.get("name") if best and best is not exact else None
        rows.append((it["slug"], name, it.get("group"), it.get("rivenType"),
                     it.get("disposition"), it.get("reqMasteryRank"),
                     s.get("type"), s.get("category"),
                     (best or s).get("criticalChance") or 0.0,
                     (best or s).get("criticalMultiplier") or 1.0,
                     (best or s).get("procChance") or 0.0,
                     (best or s).get("totalDamage") or 0.0,
                     (best or s).get("fireRate") or 1.0,
                     (best or s).get("multishot") or 1.0, rating, best_name,
                     json.dumps(phys_split(best or s)),
                     (best or s).get("trigger"),
                     (((best or s).get("attacks") or [{}])[0]).get("shot_type")))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO riven_weapons(slug, name, grp, riven_type,"
            " disposition, mastery, wtype, category, crit_chance, crit_mult,"
            " proc_chance, total_damage, fire_rate, multishot, rating,"
            " best_variant, phys, fire_trigger, shot_type)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"riven weapons: {len(rows)} cached "
          f"({sum(1 for r in rows if r[14])} with combat stats)")
    return True


def riven_auction_stats(slug):
    """What the market actually pays, and which stats the dear ones carry."""
    d = get_json("https://api.warframe.market/v1/auctions/search"
                 f"?type=riven&weapon_url_name={urllib.parse.quote(slug)}"
                 "&sort_by=price_asc")
    auctions = ((d or {}).get("payload") or {}).get("auctions") or []
    priced = [a for a in auctions
              if a.get("buyout_price") and a["owner"]["status"] in ("ingame", "online")]
    if len(priced) < 8:
        priced = [a for a in auctions if a.get("buyout_price")]
    if not priced:
        return None
    priced.sort(key=lambda a: a["buyout_price"])
    prices = [a["buyout_price"] for a in priced]
    overall = statistics.median(prices)

    def ranked(positive):
        """Rank stats by what rivens carrying them actually fetch.

        Comparing median price with and without a stat is far steadier than
        counting membership of a top quartile, which on a weapon with only a
        handful of auctions is mostly noise."""
        buckets = {}
        for a in priced:
            for at in a["item"].get("attributes", []):
                if bool(at.get("positive")) != positive:
                    continue
                key = at.get("url_name")
                if key:
                    buckets.setdefault(key, []).append(a["buyout_price"])
        out = []
        for key, vals in buckets.items():
            if len(vals) < max(3, len(priced) * 0.05):
                continue  # too rare here to say anything honest about
            med = statistics.median(vals)
            out.append({"stat": riven_stat_label(key), "key": key,
                        "seen_pct": round(100 * len(vals) / len(priced)),
                        "median": round(med),
                        "premium": round(100 * (med - overall) / overall)})
        out.sort(key=lambda x: -x["premium"])
        return out[:6]

    return {
        "auctions": len(priced),
        "cheapest": prices[0],
        "median": round(overall),
        "confident": len(priced) >= 25,
        "best_positives": ranked(True),
        "best_negatives": ranked(False),
    }


META_PATH = APP_DIR / "weapon_meta.json"


def load_meta():
    """Tier data lives in weapon_meta.json so the meta can be updated by
    editing a file rather than the code. The table above is the fallback."""
    global META_BY_NAME, META_AS_OF, META_SOURCES, META_UPDATED
    tiers = {k.lower(): v for k, v in META_TIERS.items()}
    try:
        d = json.loads(META_PATH.read_text(encoding="utf-8"))
        loaded = {k.lower(): (v.get("tier"), bool(v.get("incarnon")),
                              v.get("note", ""), bool(v.get("disputed")))
                  for k, v in (d.get("weapons") or {}).items() if v.get("tier")}
        if loaded:
            tiers = loaded
            META_AS_OF = d.get("meta_as_of", META_AS_OF)
            META_SOURCES = d.get("sources", META_SOURCES)
            META_UPDATED = d.get("updated", "")
            print(f"weapon meta: {len(tiers)} weapons from weapon_meta.json "
                  f"(updated {META_UPDATED})")
    except (OSError, ValueError) as e:
        print(f"weapon_meta.json unreadable ({e}); using built-in tiers")
    META_BY_NAME = tiers
    return META_BY_NAME


META_UPDATED = ""
META_BY_NAME = {k.lower(): v for k, v in META_TIERS.items()}


# ---- how to actually build the thing ----------------------------------
# Warframe damage rules that drive these: Viral (Cold+Toxin) multiplies damage
# to health and is the Steel Path default, usually paired with Heat because
# Heat also strips armour over time. Corrosive (Electricity+Toxin) is the
# direct armour-strip option. Multishot and base damage are near-mandatory on
# every gun, and Galvanized mods (Arbitrations) overtake their plain versions
# once you are holding kill stacks.
BUILD_MODS = {
    "primary": {"damage": "Serration", "multishot": "Split Chamber",
                "galv_multi": "Galvanized Chamber", "galv_status":
                "Galvanized Aptitude", "crit": "Point Strike + Vital Sense",
                "galv_crit": "Galvanized Scope"},
    "shotgun": {"damage": "Point Blank", "multishot": "Hell's Chamber",
                "galv_multi": "Galvanized Hell", "galv_status":
                "Galvanized Savvy", "crit": "Blunderbuss + Ravage",
                # there is no Galvanized shotgun crit mod — the shotgun
                # Galvanized set is multishot, status and projectile speed
                "galv_crit": ""},
    # Pistol Gambit and Target Cracker are the pistol crit pair. Targeting
    # Subsystem is an accuracy mod, and Hollow Point buys crit damage back
    # with -15% base damage, so neither belongs in a crit build.
    "secondary": {"damage": "Hornet Strike", "multishot": "Barrel Diffusion",
                  "galv_multi": "Galvanized Diffusion", "galv_status":
                  "Galvanized Shot",
                  "crit": "Primed Pistol Gambit + Primed Target Cracker",
                  "galv_crit": "Galvanized Crosshairs"},
    "melee": {"damage": "Pressure Point", "multishot": "", "galv_multi": "",
              "galv_status": "Weeping Wounds", "crit": "True Steel + Organ Shatter",
              "galv_crit": "Blood Rush"},
}


def build_advice(w, incarnon=False):
    """Turn a weapon's own stats into a build direction, using the damage
    rules above. Deliberately archetype-level: exact mod slots depend on
    forma and arcanes, but the direction is what people get wrong."""
    grp = (w.get("grp") or "").lower()
    wtype = (w.get("wtype") or "").lower()
    if "melee" in grp or grp in ("zaw",):
        kind = "melee"
    elif "shotgun" in wtype or "shotgun" in grp:
        kind = "shotgun"
    elif grp in ("secondary", "pistol", "kitgun"):
        kind = "secondary"
    else:
        kind = "primary"
    m = BUILD_MODS[kind]
    crit = w.get("crit_chance") or 0
    status = w.get("proc_chance") or 0
    steps, focus = [], ""
    # Almost every weapon in this game wants crit or status; "just raw damage"
    # is the rare exception, so follow whichever stat leads rather than
    # demanding it clear a high bar.
    if max(crit, status) < 0.15:
        focus = "raw damage"
        steps.append("Neither crit nor status stands out, so lean on raw damage "
                     "and multishot rather than forcing a hybrid.")
    elif crit >= status:
        focus = "critical"
        steps.append(f"Crit weapon at {round(crit * 100)}% base — build around it: "
                     f"{m['crit']}.")
        if kind == "melee":
            steps.append("Blood Rush turns combo count into crit chance, which is "
                         "how melee reaches red crits.")
        else:
            steps.append(f"{m['galv_crit']} pushes it further once you are "
                         f"landing kills.")
    else:
        focus = "status"
        steps.append(f"Status weapon at {round(status * 100)}% base — "
                     f"{m['galv_status']} scales with the number of different "
                     f"procs on the target.")
    if incarnon:
        steps.append("These are the base weapon's numbers: the Incarnon form has "
                     "its own crit and status, and its evolution choices "
                     "(extra multishot, crit chance, and so on) decide the final "
                     "build — pick the branch that matches the direction above.")
    steps.append(f"{m['damage']} for base damage"
                 + (f", {m['multishot']} → {m['galv_multi']} for multishot"
                    if m["multishot"] else "") + ".")
    steps.append("Viral (Cold + Toxin) plus Heat is the default Steel Path "
                 "pairing: Viral multiplies damage to health, Heat strips armour "
                 "as it burns. Use Corrosive (Electricity + Toxin) instead when "
                 "you want the armour gone outright.")
    if crit >= 0.25 and status >= 0.25:
        steps.append("It has the crit and the status to run a hybrid, which is "
                     "where Galvanized mods and Merciless arcanes pay off most.")
    return {"focus": focus, "kind": kind, "steps": steps}


def meta_tier(name):
    """Consensus tier for a riven weapon, or None if untracked. Returns
    (tier, incarnon, note, disputed).

    Exact name match on purpose: fuzzy matching once put Ether Reaper on
    Reaper Prime's tier."""
    v = META_BY_NAME.get((name or "").lower().strip())
    if not v:
        return (None, False, "", False)
    return v if len(v) == 4 else (v[0], v[1], v[2], False)


def melee_style_focus(w, build, style):
    """Heavy attack is a choice, not a stat you can read off the weapon.

    A Kronen can be a combo-crit weapon or a heavy-attack one and the riven you
    want is different for each, so the caller picks. Without this the whole
    melee_heavy archetype was unreachable."""
    focus = {"critical": "crit", "status": "status"}.get(build["focus"], "raw")
    is_melee = ((w.get("grp") or "").lower() in ("melee", "zaw")
                or "melee" in (w.get("wtype") or "").lower())
    if is_melee and style == "heavy":
        return "heavy"
    return focus


def riven_report(conn, slug, style=""):
    row = conn.execute("SELECT * FROM riven_weapons WHERE slug=?", (slug,)).fetchone()
    if not row:
        return None
    w = dict(row)
    peers = [r["rating"] for r in conn.execute(
        "SELECT rating FROM riven_weapons WHERE grp IS ? AND rating > 0",
        (w["grp"],))]
    pct = None
    if w["rating"] and len(peers) >= 5:
        pct = round(100 * sum(1 for p in peers if p < w["rating"]) / len(peers))
    disp = w["disposition"] or 1.0

    # Disposition is inverse to how strong a weapon already is: a high number
    # means rivens roll big, which is exactly where a riven changes the most.
    if disp >= 1.35:
        disp_note = "very high — rivens roll unusually strong here"
    elif disp >= 1.15:
        disp_note = "high — a riven adds a lot"
    elif disp >= 0.95:
        disp_note = "average"
    elif disp >= 0.8:
        disp_note = "low — rivens roll weak on this weapon"
    else:
        disp_note = "very low — a riven barely moves it"

    market = riven_auction_stats(slug)
    tier, incarnon, tier_note, disputed = meta_tier(w["name"])

    # How good the WEAPON is decides whether a riven is worth chasing. Riven
    # prices are deliberately not the lead signal: a god roll on a mediocre
    # weapon still sells high, so price measures the roll, not the weapon.
    why = []
    computed = False
    if tier in ("S", "A"):
        verdict = ("Meta weapon — worth a riven" if tier == "S"
                   else "Strong weapon — a riven pays off")
        why.append(f"community lists put it in {tier} tier as of {META_AS_OF}")
        if disp >= 1.3:
            why.append("and the high disposition means rolls land big, which "
                       "is the best combination there is")
        elif disp <= 0.8:
            why.append("though the low disposition limits how much a roll adds "
                       "— strong weapons get taxed this way")
    elif tier in ("B", "C", "D"):
        verdict = ("Playable, but not a meta riven target" if tier == "B"
                   else "Not worth a riven")
        why.append(f"community lists put it in {tier} tier as of {META_AS_OF}")
        if disp >= 1.35:
            why.append("the very high disposition makes it a fun project rather "
                       "than a serious one")
    else:
        # Two thirds of riven-capable weapons never appear on a community tier
        # list — they are the long tail nobody writes about, not an oversight.
        # Saying only "no consensus" gave a 19th-percentile weapon the same
        # answer as an 80th-percentile one, so fall back to the weapon's own
        # numbers and say plainly that this is what we are doing.
        computed = True
        if pct is None:
            verdict = "Not enough data to judge this one"
            why.append("no community tier, and not enough comparable weapons "
                       "in its class to rank it on its own numbers")
        elif pct >= 75:
            verdict = "No community tier, but the numbers are strong"
            why.append(f"nobody ranks this weapon, yet its raw damage beats "
                       f"{pct}% of its class — worth a riven if you enjoy it")
        elif pct >= 40:
            verdict = "No community tier — middling numbers"
            why.append(f"its raw damage sits at the {pct}th percentile of its "
                       f"class, so a riven makes it fun rather than meta")
        else:
            verdict = "No community tier — weak numbers"
            why.append(f"its raw damage beats only {pct}% of its class, and a "
                       f"riven multiplies a weapon rather than replacing it")
        if disp >= 1.35:
            why.append("the very high disposition does mean rolls land hard, "
                       "which is what makes these weapons fun projects")
        why.append("this reading is computed from the weapon's own stats — it "
                   "cannot see mechanics like an Incarnon form or an alt-fire")
    if incarnon:
        why.append("this tier is for its Incarnon Genesis form — the base "
                   "weapon is much weaker until you fit the adapter")
    if tier_note:
        why.append(tier_note)
    build = build_advice(w, incarnon)
    focus = melee_style_focus(w, build, style)
    is_melee = ((w.get("grp") or "").lower() in ("melee", "zaw")
                or "melee" in (w.get("wtype") or "").lower())
    return {"weapon": w, "percentile": pct, "disposition": disp,
            "style": style or "default", "style_choice": is_melee,
            "disposition_note": disp_note, "verdict": verdict, "why": why,
            "tier": tier, "incarnon": incarnon, "disputed": disputed,
            "computed_verdict": computed, "meta_as_of": META_AS_OF,
            "meta_sources": META_SOURCES, "meta_updated": META_UPDATED,
            "build": build, "stat_plan": riven_stat_plan(w, focus),
            "market": market}


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
TWITCH_CHANNELS = ["warframe", "warframeinternational"]


def _twitch_from_gql():
    """Both official channels in one request."""
    parts = " ".join(
        f'c{i}: user(login: "{c}") {{ login displayName stream '
        f'{{ id createdAt viewersCount title game {{ displayName }} }} }}'
        for i, c in enumerate(TWITCH_CHANNELS))
    body = json.dumps([{"operationName": "WfmLive", "variables": {},
                        "query": "query WfmLive { " + parts + " }"}]).encode()
    req = urllib.request.Request("https://gql.twitch.tv/gql", data=body, headers={
        "Client-ID": TWITCH_GQL_ID, "Content-Type": "application/json",
        "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as resp:
            payload = json.load(resp)
        data = payload[0]["data"]
        out = []
        for i, login in enumerate(TWITCH_CHANNELS):
            user = data.get(f"c{i}") or {}
            stream = user.get("stream")
            out.append({
                "login": login,
                "name": user.get("displayName") or login,
                "live": bool(stream),
                "stream_id": (stream or {}).get("id"),
                "title": (stream or {}).get("title") or "",
                "game": ((stream or {}).get("game") or {}).get("displayName", ""),
                "viewers": (stream or {}).get("viewersCount"),
                "url": f"https://www.twitch.tv/{login}"})
        return {"channels": out, "live": any(c["live"] for c in out)}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            KeyError, IndexError, TypeError):
        return None


def _twitch_from_decapi():
    """Fallback: a public uptime endpoint that needs no credentials."""
    out = []
    for login in TWITCH_CHANNELS:
        req = urllib.request.Request(
            f"https://decapi.me/twitch/uptime/{login}",
            headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as resp:
                text = resp.read().decode("utf-8", "replace").strip()
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        live = "offline" not in text.lower() and "error" not in text.lower()
        out.append({"login": login, "name": login, "live": live,
                    "stream_id": text if live else None, "title": "", "game": "",
                    "viewers": None, "url": f"https://www.twitch.tv/{login}"})
    return {"channels": out, "live": any(c["live"] for c in out)}


def twitch_status():
    """Are the official Warframe channels live? Cached 90 s."""
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
        # Live-stream backlog. A market listing decays fast, so this is
        # newest-first and evicts the oldest when full — the previous set
        # dropped incoming events while serving stale ones in random order.
        self.dirty = deque(maxlen=120)
        self.dirty_set = set()
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
        while self.dirty:
            slug = self.dirty.pop()  # newest event first
            self.dirty_set.discard(slug)
            if slug in self._items:
                return slug, True
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
        # Digital Extremes rebalances riven dispositions every Prime Access,
        # so a once-only fetch would quietly go stale — refresh weekly.
        if not conn.execute("SELECT COUNT(*) AS n FROM frames").fetchone()["n"] \
                or time.time() - flag_get(conn, "frames_fetched_ts", 0) > 7 * 86400:
            try:
                if frames_refresh(conn):
                    flag_set(conn, "frames_fetched_ts", int(time.time()))
            except Exception as e:
                print(f"warframe list unavailable: {e!r}")
        # also refresh when the cache predates unique_name: without those paths
        # the conflict map falls back to display names and misses Galvanized
        # against its base mod
        if not conn.execute("SELECT COUNT(*) AS n FROM game_mods").fetchone()["n"] \
                or not conn.execute("SELECT COUNT(*) AS n FROM game_mods "
                                    "WHERE unique_name IS NOT NULL "
                                    "AND unique_name != ''").fetchone()["n"] \
                or time.time() - flag_get(conn, "mods_fetched_ts", 0) > 7 * 86400:
            try:
                if mods_refresh(conn):
                    flag_set(conn, "mods_fetched_ts", int(time.time()))
            except Exception as e:
                print(f"mod list unavailable: {e!r}")
        if not conn.execute("SELECT COUNT(*) AS n FROM items "
                            "WHERE vault IS NOT NULL").fetchone()["n"] \
                or time.time() - flag_get(conn, "vault_fetched_ts", 0) > 7 * 86400:
            try:
                if vault_refresh(conn):
                    flag_set(conn, "vault_fetched_ts", int(time.time()))
            except Exception as e:
                print(f"vault status unavailable: {e!r}")
        have_rivens = conn.execute(
            "SELECT COUNT(*) AS n FROM riven_weapons").fetchone()["n"]
        fetched = flag_get(conn, "rivens_fetched_ts", 0)
        if not have_rivens or time.time() - fetched > 7 * 86400:
            try:
                if riven_refresh(conn):
                    flag_set(conn, "rivens_fetched_ts", int(time.time()))
            except Exception as e:
                print(f"riven weapon list unavailable: {e!r}")
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
                if now - last_revalidate > 90:
                    # Re-check items with live deals, least-recently-verified
                    # first, in their own lane above the stream flood. This
                    # cadence is what bounds how stale a shown discount can be:
                    # thin books move fast, so a slow cycle shows fiction.
                    for r in conn.execute(
                            "SELECT slug FROM deals WHERE gone_ts IS NULL "
                            "GROUP BY slug ORDER BY MIN(checked_ts) LIMIT 50"):
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
                # Proof of life. Without it a dead scanner thread leaves the UI
                # showing whatever item it stopped on, forever — the app looks
                # perfectly healthy while doing nothing at all.
                self.status["heartbeat"] = time.time()
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
        """Announce when an official channel goes live, and optionally open the
        stream in the default browser so it plays under the signed-in account
        (drops need real watch time on a real player)."""
        t = twitch_status()
        if not t or not t.get("channels"):
            return
        self.status["twitch"] = bool(t.get("live"))
        self.status["twitch_channels"] = [
            {"name": c["name"], "live": c["live"], "viewers": c["viewers"],
             "url": c["url"]} for c in t["channels"]]
        opened = flag_get(conn, "twitch_opened", {}) or {}
        for c in t["channels"]:
            was_live = flag_get(conn, f'twitch_live_{c["login"]}', False)
            if c["live"] and not was_live:
                viewers = f' · {c["viewers"]:,} watching' if c.get("viewers") else ""
                self.announce(cfg, cfg["notify_twitch"],
                              f'{c["name"]} is live on Twitch',
                              (c.get("title") or c["url"]) + viewers)
            # open once per broadcast, not once per poll
            if (c["live"] and cfg["auto_open_twitch"]
                    and c["stream_id"] and opened.get(c["login"]) != c["stream_id"]):
                opened[c["login"]] = c["stream_id"]
                flag_set(conn, "twitch_opened", opened)
                try:
                    webbrowser.open(c["url"])
                    print(f'opened {c["url"]} for drops')
                except Exception as e:
                    print(f"could not open browser: {e!r}")
            flag_set(conn, f'twitch_live_{c["login"]}', c["live"])

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
            # The snapshot keeps the raw reference on purpose: history has to
            # stay truthful so a genuine price climb lifts the 7-day norm and
            # releases the clamp below on its own after a few days.
            self.record_snapshot(conn, slug, rank, subtype, now, low, ref,
                                 b["buy_high"], len(sells))
            # A thin book plus one joke listing (Warm Coat: 100p against a
            # 114,514p "market") makes every discount measured against it
            # fiction. Judge deals against the norm instead.
            if ref and ref_med_7d and ref > REF_SANITY_MULT * ref_med_7d:
                ref = ref_med_7d
            if not qualifies:
                continue

            # The reference still uses the whole live book (a broader sample is
            # a steadier estimate), but the listing we actually advertise has
            # to be one you can reach — the cheapest non-ghost seller.
            cheapest = next((o for o in sells if is_reachable(o, now)), None)
            if cheapest is None:
                continue
            price = cheapest["platinum"]
            savings = ref - price
            reachable = [o for o in sells if is_reachable(o, now)]
            second = reachable[1]["platinum"] if len(reachable) >= 2 else None
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
                                     status=cheapest["user"]["status"],
                                     seen=seller_seen_epoch(cheapest),
                                     rep=cheapest["user"].get("reputation"))
                else:
                    note = "30-day low" if hist_low and price < hist_low else ""
                    self.record_deal(conn, cfg, "snipe", slug, name, rank, subtype,
                                     price=price, reference=ref,
                                     seller=cheapest["user"]["ingameName"], note=note,
                                     order_key=f'{cheapest["id"]}:{price}',
                                     status=cheapest["user"]["status"],
                                     seen=seller_seen_epoch(cheapest),
                                     rep=cheapest["user"].get("reputation"))

            # Buy orders far above the sell floor are bait or abandoned orders
            # (a real buyer would just buy the floor listing), not flip profit.
            if (b["buy_high"] is not None
                    and b["buy_high"] - price >= cfg["flip_min_profit"]
                    and b["buy_high"] <= ref * 1.25):
                profit = b["buy_high"] - price
                self.record_deal(conn, cfg, "flip", slug, name, rank, subtype,
                                 price=cheapest["platinum"], reference=b["buy_high"],
                                 seller=cheapest["user"]["ingameName"],
                                 note=f"+{profit}p buy-order spread",
                                 order_key=f"flip:{slug}:{rank}:{subtype}:"
                                           f'{cheapest["platinum"]}:{b["buy_high"]}',
                                 status=cheapest["user"]["status"],
                                 seen=seller_seen_epoch(cheapest),
                                 rep=cheapest["user"].get("reputation"))

            if ref_med_7d and ref <= ref_med_7d * 0.70:
                pct = round(100 * (1 - ref / ref_med_7d))
                note = f"market {pct}% under 7-day norm"
                # a dip on a vaulted prime is a different animal: supply is
                # fixed, so the price climbs back — this is the buy-and-hold
                # signal, not just a cheap week
                if self.vault_of(conn, slug) == "vaulted":
                    note += " · VAULTED — fixed supply, prices recover"
                self.record_deal(conn, cfg, "dip", slug, name, rank, subtype,
                                 price=int(ref), reference=ref_med_7d,
                                 seller="", note=note,
                                 order_key=f"dip:{slug}:{rank}:{subtype}:{now // 86400}")
            # The other half of the same signal, and the half this app has
            # never had: the real player loop is farm, build sets, SELL. An
            # item trading well above its own norm is the moment to list one,
            # and every number needed to spot it was already being computed
            # and thrown away.
            elif ref_med_7d and ref >= ref_med_7d * cfg["peak_over"]:
                pct = round(100 * (ref / ref_med_7d - 1))
                note = f"market {pct}% over 7-day norm — sell yours now"
                v = self.vault_of(conn, slug)
                if v == "vaulted":
                    note += " · VAULTED — scarcity spike, sellers market"
                elif v == "farmable":
                    # a farmable prime spiking is temporary by definition:
                    # supply flows in daily, so sell into it quickly
                    note += " · still farmable, spike will not last"
                self.record_deal(conn, cfg, "peak", slug, name, rank, subtype,
                                 price=int(ref), reference=ref_med_7d,
                                 seller="", note=note,
                                 order_key=f"peak:{slug}:{rank}:{subtype}:{now // 86400}")

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
        cheapest = next((o for o in main["sells"] if is_reachable(o)), None)
        if cheapest and cheapest["platinum"] <= row["target"]:
            self.record_deal(
                conn, cfg, "target", slug, name,
                cheapest.get("rank"), cheapest.get("subtype"),
                price=cheapest["platinum"], reference=main["reference"],
                seller=cheapest["user"]["ingameName"],
                note=f'hit your {row["target"]}p target',
                order_key=f'target:{cheapest["id"]}:{cheapest["platinum"]}',
                status=cheapest["user"]["status"],
                seen=seller_seen_epoch(cheapest),
                rep=cheapest["user"].get("reputation"))

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
            fresh = None            # the seller's live row, when this kind has one
            if r["kind"] in ("snipe", "jackpot", "target"):
                oid = r["order_key"].split(":")[-2] if r["kind"] == "target" \
                    else r["order_key"].split(":")[0]
                listed = b and any(o["id"] == oid and o["platinum"] == r["price"]
                                   for o in b["sells"])
                if listed:
                    order = next(o for o in b["sells"] if o["id"] == oid)
                    fresh = order
                    if not is_reachable(order, now):
                        conn.execute(
                            "UPDATE deals SET gone_ts=?, rev=? WHERE id=?",
                            (now, next_rev(conn), r["id"]))
                        expired += 1
                        continue  # seller vanished mid-listing: not actionable
                    # If the book is momentarily too thin for the normal
                    # reference, fall back to the rest of the book rather than
                    # keeping the price this deal was born with — a fossilised
                    # reference makes the discount pure fiction.
                    ref_now = b["reference"] or compute_reference(b["sells"])
                    if ref_now:
                        # same joke-listing guard as the scan path, or a deal
                        # born clean could be re-blessed by a troll price
                        norm = self.history_context(
                            conn, slug, r["rank"], r["subtype"])[1]
                        if norm and ref_now > REF_SANITY_MULT * norm:
                            ref_now = norm
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
                if alive:
                    fresh = b["sells"][0]
            elif r["kind"] in ("dip", "peak"):
                # both are market-wide observations rather than a listing, so
                # they age out rather than being confirmed against an order.
                # Leaving "peak" out of this loop retired every one of them the
                # moment it was found.
                alive = now - r["ts"] < 86400
            if alive:
                pct = (round(100 * (reference - r["price"]) / reference)
                       if reference else r["pct"])
                if fresh:
                    # The seller's presence is re-read from the same payload the
                    # price came from. Without this the "last seen" the UI shows
                    # is frozen at the moment the deal was found, so a seller who
                    # has been in-game all along reads as days absent.
                    u = fresh.get("user") or {}
                    conn.execute(
                        "UPDATE deals SET checked_ts=?, reference=?, pct=?, rev=?,"
                        " seller_seen=?, seller_rep=?, status=? WHERE id=?",
                        (now, reference, pct, next_rev(conn),
                         seller_seen_epoch(fresh), u.get("reputation"),
                         u.get("status"), r["id"]))
                else:
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

    def vault_of(self, conn, slug):
        r = conn.execute("SELECT vault FROM items WHERE slug=?",
                         (slug,)).fetchone()
        return r["vault"] if r else None

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
                    price, reference, seller, note, order_key, status="",
                    seen=None, rep=None):
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
            " reference, pct, seller, status, note, order_key, checked_ts, rev,"
            " seller_seen, seller_rep)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, kind, slug, name, rank, subtype, price,
             reference, pct, seller, status, note, order_key, now, next_rev(conn),
             seen, rep))
        conn.commit()
        if cur.rowcount != 1:
            return  # already alerted
        sse_push({"event": "deal", "kind": kind})
        variant = variant_label(rank, subtype)
        # pct is "how far under the reference", so a peak makes it negative.
        # Printing a hardcoded minus turned a +40% sell signal into "--40%".
        move = f"-{pct}%" if pct >= 0 else f"+{abs(pct)}% over norm"
        title = f"{name}{variant}: {price}p (ref {round(reference)}p, {move})"
        seller_desc = seller + (f" ({status})" if status and status != "ingame" else "")
        print(f"[{kind.upper()}] {title}" + (f"  ({note})" if note else ""))
        if seller:
            print(f"  seller {seller_desc}")
            print(f"  {whisper_line(name, rank, subtype, price, seller)}")
        toggles = {"snipe": cfg["notify_snipes"], "jackpot": cfg["notify_jackpots"],
                   "flip": cfg["notify_flips"], "dip": cfg["notify_dips"],
                   "target": cfg["notify_targets"], "peak": cfg["notify_peaks"]}
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
        if slug in self.scanner.dirty_set:
            return
        junk = conn.execute("SELECT junk_until FROM items WHERE slug=?",
                            (slug,)).fetchone()
        if junk and junk["junk_until"] > now:
            return
        self._recent[slug] = now
        dq = self.scanner.dirty
        if len(dq) == dq.maxlen:  # full: the oldest event is the one to lose
            self.scanner.dirty_set.discard(dq[0])
        dq.append(slug)
        self.scanner.dirty_set.add(slug)

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
                hb = s.get("heartbeat")
                self._json({**s, "deals_24h": deals_24h, "jackpots_24h": jackpots_24h,
                            "req_per_min": len(recent),
                            "throttled": THROTTLE_COUNT["n"],
                            # how long since the scanner last touched an item:
                            # the UI turns this into a visible warning rather
                            # than showing a frozen "scanning…" forever
                            "stalled_for": (round(time.time() - hb) if hb else None),
                            "scanner_alive": SCANNER.is_alive(),
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
            elif route == "/api/mods":
                slot = q.get("slot", ["warframe"])[0]
                owner = q.get("owner", [""])[0]        # frame name, for augments
                subsumed = q.get("subsumed", [""])[0]  # unlocks that augment
                base = frame_family(owner)
                rows = conn.execute(
                    "SELECT * FROM game_mods WHERE slot=? ORDER BY name",
                    (slot,)).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["stats"] = json.loads(d.get("stats") or "{}")
                    compat = d["compat"]
                    general = compat in ("ANY", "WARFRAME", "AURA")
                    if slot in ("warframe", "exilus", "aura") and not general:
                        # a frame-locked augment: show it only on its own frame,
                        # or on any frame that has subsumed the matching ability
                        # (Helminth makes that augment equippable elsewhere)
                        sub_ok = bool(subsumed) and (
                            subsumed.lower() in (d["text"] or "").lower()
                            or subsumed.lower() in d["name"].lower())
                        if compat != base and not sub_ok:
                            continue
                        d["via_helminth"] = compat != base
                    d["exclusive"] = not general
                    d["family"] = mod_family(d["name"])
                    out.append(d)
                self._json(out)
            elif route == "/api/arcanes":
                self._json(arcane_catalogue(conn))
            elif route == "/api/subsumable":
                self._json(BUILD_GUIDE.get("subsumable") or SUBSUMABLE)
            elif route == "/api/moddables":
                # everything you can put mods in, in one searchable list
                out = [{"kind": "frame", "name": r["name"],
                        "sub": "Warframe" + (" Prime" if r["is_prime"] else "")}
                       for r in conn.execute(
                           "SELECT name, is_prime FROM frames ORDER BY name")]
                out += [{"kind": "weapon", "name": r["name"], "slug": r["slug"],
                         "sub": (r["grp"] or "weapon").title()}
                        for r in conn.execute(
                            "SELECT name, slug, grp FROM riven_weapons ORDER BY name")]
                comp = (BUILD_GUIDE.get("companions") or {}).get("picks") or {}
                out += [{"kind": "companion", "name": n, "sub": "Companion"}
                        for n in sorted(comp)]
                self._json(out)
            elif route == "/api/build":
                kind = q.get("kind", [""])[0]
                name = q.get("name", [""])[0]
                if kind == "frame":
                    rep = frame_build(conn, name)
                elif kind == "weapon":
                    slug = q.get("slug", [""])[0]
                    # the riven advice on this page has to match the build
                    # shown beside it, so both read the same style
                    rep = riven_report(conn, slug, q.get("style", [""])[0])
                    if rep:
                        rep["kind"] = "weapon"
                        rep["universal"] = BUILD_GUIDE.get("universal") or {}
                        rep["profile"] = weapon_build_profile(
                            rep["weapon"], melee_style_focus(
                                rep["weapon"], rep["build"],
                                q.get("style", [""])[0]))
                elif kind == "companion":
                    c = BUILD_GUIDE.get("companions") or {}
                    rep = {"kind": "companion", "name": name,
                           "why": (c.get("picks") or {}).get(name, ""),
                           "companions": c, "updated": BUILD_GUIDE.get("updated"),
                           "sources": BUILD_GUIDE.get("sources", []),
                           "profile": companion_build_profile(name)}
                else:
                    rep = None
                if not rep:
                    self._json({"error": "unknown"}, 404)
                else:
                    self._json(rep)
            elif route == "/api/riven_weapons":
                rows = conn.execute(
                    "SELECT slug, name, grp, disposition, mastery FROM "
                    "riven_weapons ORDER BY name").fetchall()
                self._json([dict(r) for r in rows])
            elif route == "/api/riven":
                slug = q.get("slug", [""])[0]
                rep = riven_report(conn, slug, q.get("style", [""])[0])
                if not rep:
                    self._json({"error": "unknown weapon"}, 404)
                else:
                    self._json(rep)
            elif route == "/api/riven_market":
                rep = riven_market_offers(conn, q.get("slug", [""])[0],
                                          q.get("style", [""])[0])
                if not rep:
                    self._json({"error": "unknown weapon"}, 404)
                else:
                    self._json(rep)
            elif route == "/api/riven_grade":
                slug = q.get("slug", [""])[0]
                pos = [s for s in q.get("pos", [""])[0].split("|") if s][:3]
                neg = q.get("neg", [""])[0]
                rep = (grade_riven(conn, slug, pos, neg,
                                   q.get("style", [""])[0]) if pos else None)
                if not rep:
                    self._json({"error": "need a weapon and at least one "
                                         "positive stat"}, 400)
                else:
                    self._json(rep)
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
                row = conn.execute("SELECT name, ducats, vault FROM items "
                                   "WHERE slug=?", (slug,)).fetchone()
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
                            "ducats": row["ducats"], "vault": row["vault"],
                            "buckets": buckets})
            elif route == "/api/watchlist":
                # The table shows the live price, ducat value, how fresh that
                # price is and any deal currently open on the item, so the row
                # has to carry them — a bare slug renders as a line of dashes.
                rows = conn.execute(
                    "SELECT w.slug, i.name, w.target, i.ducats FROM watchlist w "
                    "JOIN items i ON i.slug = w.slug ORDER BY i.name").fetchall()
                now_ts = int(time.time())
                out = []
                for r in rows:
                    # Prefer the plain bucket, but arcanes and mods only ever
                    # have ranked ones — without the fallback every arcane on
                    # the watchlist shows a blank price.
                    snap = conn.execute(
                        "SELECT low, ts, sellers FROM snapshots WHERE slug=? "
                        "AND low IS NOT NULL "
                        "ORDER BY (rank IS NOT NULL), COALESCE(rank, 0), id DESC "
                        "LIMIT 1", (r["slug"],)).fetchone()
                    low = snap["low"] if snap else None
                    d = {"slug": r["slug"], "name": r["name"],
                         "target": r["target"], "low": low,
                         "sellers": snap["sellers"] if snap else None,
                         "age_min": (round((now_ts - snap["ts"]) / 60)
                                     if snap else None),
                         "per100": (round(100 * low / r["ducats"], 1)
                                    if r["ducats"] and low else None)}
                    deal = conn.execute(
                        "SELECT kind, price, pct, seller FROM deals WHERE slug=? "
                        "AND gone_ts IS NULL ORDER BY pct DESC LIMIT 1",
                        (r["slug"],)).fetchone()
                    d["deal"] = dict(deal) if deal else None
                    out.append(d)
                self._json(out)
            elif route == "/api/sets":
                latest = {}
                for r in conn.execute(
                        "SELECT s.slug, s.low, s.ts FROM snapshots s JOIN "
                        "(SELECT slug, MAX(id) AS mid FROM snapshots "
                        " WHERE rank IS NULL AND subtype IS NULL GROUP BY slug) m "
                        "ON s.id = m.mid WHERE s.low IS NOT NULL"):
                    latest[r["slug"]] = (r["low"], r["ts"])
                names, vaults = {}, {}
                for r in conn.execute("SELECT slug, name, vault FROM items"):
                    names[r["slug"]] = r["name"]
                    if r["vault"]:
                        vaults[r["slug"]] = r["vault"]
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
                        "vault": vaults.get(set_slug),
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
    load_meta()
    load_build_guide()
    load_riven_guide()
    # the conflict map lives in memory; rebuild it from whatever is cached so
    # the Builds tab enforces families before the first mod refresh runs
    boot = db_connect()
    try:
        build_mod_families(boot)
    finally:
        boot.close()
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

    def trim_log():
        """Keep the log from growing without bound.

        launchd (and the Windows --log flag) open the file in append mode, so
        truncating it is safe from here: the next write lands at offset 0. The
        log had reached 3MB and nothing had ever shortened it."""
        try:
            f = sys.stdout
            if not hasattr(f, "fileno"):
                return
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size < LOG_MAX_BYTES:
                return
            os.ftruncate(f.fileno(), 0)
            f.flush()
            print(f"--- log trimmed at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"(passed {LOG_MAX_BYTES // (1024 * 1024)}MB) ---")
        except (OSError, ValueError):
            pass

    def periodic_checkpoint():
        # This service is meant to run for weeks. Checkpointing only on a clean
        # stop let the write-ahead log grow without bound — it was already 4MB
        # after seven minutes of scanning, and an unclean kill loses all of it.
        # The Windows build had this and macOS did not.
        while True:
            time.sleep(60)
            try:
                c = db_connect()
                c.execute("PRAGMA wal_checkpoint(PASSIVE)")
                c.close()
            except sqlite3.Error:
                pass
            trim_log()

    threading.Thread(target=periodic_checkpoint, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        shutdown()


if __name__ == "__main__":
    main()
