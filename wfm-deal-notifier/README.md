# WFM Deal Scanner

Watches [warframe.market](https://warframe.market) and alerts you when someone
lists an item well below its going rate. Never touches the game — trades still
happen by whispering the seller in-game.

## Run

**macOS / Linux**
```
python3 wfm_app.py
```

**Windows** — install Python from https://python.org (tick "Add python.exe to
PATH" in the installer), then:
```
python wfm_app.py
```

The web UI opens at http://127.0.0.1:8737. No packages to install — everything
is Python standard library. Click **enable notifications** in the UI once to
get desktop notifications from the browser (works the same on Windows and Mac).

Start it with `--host 0.0.0.0` to open the UI from your phone on the same
Wi-Fi (use your computer's local IP).

## What it detects

| kind  | meaning |
|-------|---------|
| ⭐ jackpot | a "too good to be true" steal — ≥60% below market, **or** a lone outlier ≥50% under even the next-cheapest seller (the classic mispriced/dump listing). Gets a gold glowing badge, a distinct alert sound, and flashes the browser-tab title. |
| snipe | one listing well below the order book (default: ≥30% and ≥15p under, going rate ≥25p) |
| flip  | a buy order paying more than the cheapest sell listing — buy low, sell to them |
| dip   | an item's whole market trading ≥30% under its own 7-day average |

Note: warframe.market trades are done face-to-face in-game, so a jackpot that
looks "fake" carries no payment risk — it's usually a typo, a dump, or already
sold. Worst case is a wasted whisper, so it's always worth a shot.

## Dashboard

Aggregates of what the scanner has actually caught: deals in the last 24 h, how
many are live, median deal lifetime, market coverage, price-history size, sync
efficiency and API throttling — plus a per-hour bar chart, your biggest catches
of the day, and the most active items. Press <kbd>2</kbd> to jump there.

## Feed tools

- **Filter box** — free-text match on item or seller, instant (press <kbd>/</kbd>).
- **Sortable columns** — click *when*, *item*, *price* or *vs market*; click
  again to reverse.
- **export CSV** — downloads exactly the rows you're looking at, including each
  deal's warframe.market URL, ready for a spreadsheet.
- **Keyboard**: <kbd>1</kbd>–<kbd>8</kbd> tabs · <kbd>/</kbd> search ·
  <kbd>c</kbd> copy newest whisper · <kbd>m</kbd> mute · <kbd>Esc</kbd> clear.

## Price accuracy

Everything that recommends an action is checked against live prices before it
counts:

- **Deals** re-verify their comparison price on every re-check, not just that
  the listing still exists. If the market moves so a listing is no longer a
  real discount, the deal expires instead of advertising a stale percentage.
- **Set margins** and **ducat ratios** computed from old prices are labelled
  *unverified*, sorted below verified rows, and re-priced automatically when
  you open the tab. This matters: a stale margin can be inverted (one set
  showed "+36p profit" that was actually an 8p loss at live prices), and a
  stale-low price inflates a ducat ratio, which would otherwise float
  outdated rows to the top of the ranking.
- Every table shows a **data age** column, so you can always see how old a
  number is, and every row has a **refresh** button for an instant re-price.

## Sets tab — prime-set arbitrage

Every tradeable set's cheapest in-game listing vs the summed price of its
parts (part quantities included — Ak-weapons needing ×2 are handled).
**Positive margin**: buy the parts, sell the set. **Negative**: buying the
set and parting it out pays more. Set recipes are discovered once from the
API and cached; prices come from the scanner's snapshots — hit **refresh**
on a row to re-price that set and its parts live (~5 s).

## Baro tab

Everything Baro Ki'Teer is selling **right now**, joined with live market
prices. For each ware: its ducat and credit cost, the current cheapest
warframe.market listing, and **p/100d** — what 100 ducats is worth in
platinum terms (higher = better ducat value). If one of his wares also has an
active deal on the market, it shows here with its badge, so you can buy it
with plat instead of spending ducats. Cosmetics and other untradeable wares
are greyed out rather than hidden, so the list matches what you see in game.

While Baro is docked, his tradeable wares are re-priced automatically on a
fast lane, and you get a desktop notification when he **arrives** and again
about **6 h before he leaves** (toggle in Settings). Names are mapped from
the game's internal feed — including relic refinements and the faction-damage
mods whose feed name doesn't match their in-game name.

## Twitch

The header shows whether **twitch.tv/warframe** is live (purple pill, viewer
count, stream title on hover — click to open). Live status comes from
Twitch's own public endpoint with a credential-free fallback, and you get a
notification when the channel goes live, which is usually when drops are on.

## Ducats tab

Shows Baro Ki'Teer's schedule live (arrives-in countdown, location, and a
green "is HERE, leaves in…" banner while he's docked — from the community
worldstate API with DE's official feed as automatic fallback, cached 30 min). Below it, prime items ranked by
**ducats per platinum** at the
current cheapest in-game listing (≥ 12 d/p good, ≥ 20 excellent). Ducat
values come from warframe.market's item data, gathered during set discovery.
Click an item to open its chart, or refresh a row for live pricing. If the
whole interface ever appears frozen with an orange banner at the top, you're
looking at a static preview of the UI file — the running app is at
http://127.0.0.1:8737.

## Price targets

Any watchlist item can carry a target price ("alert me when this is ≤ N p").
The scanner checks the item's main variant (max rank / the set itself) every
fast-lane pass and fires a **target** alert the moment a listing crosses
your number. Set it in the Watchlist tab; toggle its notifications in
Settings.

## Notifications

- **🔔 alerts** button (header) — master mute for every channel (in-tab sound,
  desktop, browser, Discord). Persists across restarts.
- **sound** button — toggles just the in-tab alert beep.
- **notifications** button — on/off toggle for browser desktop notifications
  (asks for browser permission the first time you turn it on; shows
  "blocked by browser" if you've denied it in browser settings).
- Per-kind toggles for jackpots / snipes / flips / dips live in **Settings**,
  alongside the jackpot thresholds.

Sellers who are **in-game** or **online** (on the warframe.market site) are
considered; offline sellers never are. Each alert shows the seller's status —
in-game sellers can be whispered immediately, online ones are best messaged on
the site. Flip in **Settings** to restrict everything to in-game only.
Buy orders count **only from in-game buyers**, and buy orders sitting far
above the sell floor are ignored as bait (a real buyer would just buy the
floor listing). Ranked mods/arcanes and relic refinements are compared only
against the same rank/refinement.

The feed has two filter rows: by **kind** (jackpot/snipe/flip/dip) and by
**category** — 💠 Arcanes, Primes, Mods, Weapons, Relics, Other (derived from
the item catalog's tags; a mod tagged "prime" because it augments a prime
weapon or a *Prime ability* still counts as a mod). Your category choice is
remembered, so parking it on 💠 Arcanes gives you a permanent arcanes-only
view.

Search in the **Items** and **Watchlist** tabs is instant — the item catalog
is filtered locally in your browser. Opening an item always fetches its live
order book on the spot (~1 s), records it into history, and runs deal
detection on what it sees. Charts plot cheapest listing (gold), market
reference (grey) and best buy order (teal).

## How it scans

- **⚡ Live stream** — the app subscribes to warframe.market's own websocket
  and hears about every new order on the market within seconds; items with
  fresh activity are re-scanned immediately. This is what makes coverage
  effectively instant. Needs `aiohttp` (`pip install aiohttp`); without it
  the app still works on sweeps alone and says so at startup.
- **Hot lane** — every incoming sell-order event is triaged against the
  item's last known market price straight from the event payload; a listing
  that already looks like a deal jumps the whole queue and is confirmed
  within ~1–2 seconds of being posted. This is how new jackpots are caught
  at sniper speed.
- **Deal revalidation** — jackpots are re-verified every ~30 s, everything
  else at least every ~2 minutes (least-recently-checked first). Each row
  shows "✓ price verified Ns ago"; listings that sold, got delisted or
  changed price are struck through and hidden (the "show sold/expired" chip
  reveals them, with how long each deal survived). A listing that comes back
  (seller reconnects) revives its deal without a duplicate alert.
- **Instant UI** — the server pushes to the browser (server-sent events),
  so new and expired deals appear the moment they happen, not on the next
  poll.
- **Watchlist** items get a guaranteed fast lane (default every 60 s),
  ahead of stream traffic.
- A background **sweep** over all ~3,800 items backfills whatever the stream
  doesn't touch; its position survives restarts. During busy hours it yields
  to live traffic — untraded items aren't changing anyway.
- `wfm_market.db` (SQLite) stores the item catalog (auto-refreshed daily) and
  a rolling 30-day price history per item variant — this powers the charts in
  the Items tab, "30-day low" context on alerts, and dip detection. It grows
  on its own; old rows are pruned automatically.

## Files

- `wfm_app.py` — the app (scanner + database + web server)
- `ui.html` — the web interface it serves
- `wfm_market.db` — created on first run; delete it to start fresh
- `wfm_deals.py` — older simple CLI version, kept for reference

Settings (thresholds, Discord webhook for phone pings, etc.) are edited live
in the UI's Settings tab and persist in the database.
