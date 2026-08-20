# WFM Deal Scanner

Watches [warframe.market](https://warframe.market) and alerts you when someone
lists an item well below its going rate. Never touches the game — trades still
happen by whispering the seller in-game.

## Keep it running (macOS) — do this first

The scanner is only useful while it's running. Anything that depends on
watching over time — dip detection, Baro arrival and last-call alerts, Twitch
auto-open, a full sweep of the market — silently does nothing if the app only
runs while a terminal window happens to be open.

```
./autostart.sh install
```

That starts it at login and restarts it automatically if it ever dies
(verified: `kill -9` and it was back in under 30 s). Then:

```
./autostart.sh status      # is it alive, and what is it finding?
./autostart.sh log         # follow the scanner log
./autostart.sh stop        # pause it
./autostart.sh uninstall   # remove it completely
```

The UI stays at http://127.0.0.1:8737 whenever it's running.

**If the scanner ever stops working, the app says so.** A process that is up
but no longer scanning used to look identical to a healthy one — the header
kept showing whatever item it stopped on, forever. Now the scanner stamps a
heartbeat on every item, and both the header and `autostart.sh status` turn
into a plain warning if nothing has been scanned for two minutes, or if the
scanner thread has died outright.

## Run manually

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
| ↑ sell | **the sell side.** An item trading ≥40% *over* its own 7-day norm — the moment to list one of yours. Everything else here is "buy this"; this is the opposite, so it gets a green badge and its own filter chip. Threshold is `peak_over` in Settings. |
| target | a watchlist item reached the price you set for it |

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
- **Keyboard**: <kbd>1</kbd>–<kbd>9</kbd> and <kbd>0</kbd> for the ten tabs ·
  <kbd>/</kbd> search · <kbd>c</kbd> copy newest whisper · <kbd>m</kbd> mute ·
  <kbd>Esc</kbd> clear.

## Who counts as a seller

Offline sellers are never used. But a status of "in-game" isn't proof anyone
is there: measured against live data, **about one in seven "in-game" sellers
was last seen many hours ago** (the worst still claimed in-game after nearly
19 hours). Those are ghost sessions from clients that never disconnected
cleanly — whispering them goes nowhere.

So a deal is only raised against a seller who is *reachable*: active status
**and** last seen within `ignore sellers idle over` hours (default 6,
tunable in Settings). If the cheapest listing belongs to a ghost, the next
reachable seller is used instead, and a deal whose seller goes stale expires.
The market reference still uses the whole active book, since a broader sample
is a steadier estimate of what an item is worth.

Each deal shows the seller as a name plus a **status dot**: green means active
within the last 30 minutes (whisper them now), gold within 2 hours, grey means
idle a while and they may not answer, orange means they're on the site rather
than in game. **Hover the name** for the full detail — status, last-seen time
and warframe.market reputation, so a suspiciously cheap listing from a
zero-trade account is still one movement away.

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
- The **market reference** is the median of the next few listings when the book
  is deep enough to be meaningful, and simply the next price you'd actually pay
  when it isn't. A thin book like `[40, 44, 100]` medians to 72, which would
  overstate every discount measured against it.
- Each deal's **✓ verified** stamp is colour-graded — green under 2 min, gold
  under 8, grey beyond. Prices on thin items move within minutes, so a
  confirmation from ten minutes ago is a weaker claim than one from ten
  seconds, and the feed says which you're looking at.

## Sets tab — prime-set arbitrage

Every prime set carries its **Prime Vault status** — the single biggest price
driver for primes, worked out from which relics still drop. **VAULTED** means
every part is out of the drop tables: supply is fixed until a Prime Resurgence
rotation, so prices trend up and a positive margin is an investment, not just
a flip. **FARMABLE** means new copies flow in daily and prices stay soft.
Dips and sell signals on prime items say which case you are looking at —
a dip on a vaulted prime is the buy-and-hold signal, and a spike on a
farmable one will not last. Recomputed weekly from live relic data.

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

## Twitch — drops

Both official channels are tracked: **twitch.tv/warframe** and
**twitch.tv/warframeinternational**. The header shows a pill per channel
(viewer count, stream title on hover, click to open), and you get a
notification when either goes live — usually when drops are running.

With **auto-open the stream when it goes live** enabled (Settings, on by
default), the app opens that channel in your default browser the moment it
starts, so the real player runs under your signed-in Twitch account. It opens
**once per broadcast**, not once per poll, and a stream ending and restarting
counts as a new broadcast.

For drops to actually land, two things are on you:

1. **Link your accounts** — warframe.com → Account Management → link Twitch.
   Watch time earns nothing until that link exists.
2. **The campaign has to be running on that channel** — drops are per-campaign,
   so a live stream doesn't always mean drops are enabled.

The stream plays for real; nothing here fakes watch activity to Twitch. Note
that leaving a stream running unattended to accumulate drops is AFK farming —
very common, but Twitch's terms do frown on automating the service, and some
campaigns include active-viewer checks. Turn the setting off if you'd rather
just take the notification and open it yourself.

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

## Mod loadout builder

Under any frame, weapon or companion there is a working modding screen laid
out like the in-game one: **aura, exilus and eight mod slots**.

- **Drag a mod** from the picker into a slot, or click it to drop into the
  next free one. Click a filled slot to clear it.
- **Stats update live** — Strength, Duration, Efficiency and Range, plus
  Health, Shield, Armour and Energy recalculated from the frame's real base
  stats. Efficiency is capped at 175% as it is in game.
- **Capacity** is tracked against 60 (rank 30 with a Reactor). Auras *add*
  capacity. Going over turns the counter orange — real builds fit more by
  matching polarities with Forma, which the counter tells you.
- **Frame-locked augments only appear on their own frame.** Revenant sees
  Mesmer Shield, Blinding Reave and Thrall Pact; Mag does not.
- **Helminth**: subsume an ability from the dropdown and that ability's
  augment becomes equippable — pick Roar on Mag and Piercing Roar appears in
  the picker, marked as unlocked via Helminth. That is the real rule.
- **Mod families are enforced.** You cannot fit Stretch *and* Archon Stretch,
  or Intensify with Umbral Intensify — the game does not allow it, so nor does
  this. Fitting one evicts the other and says why, and anything conflicting
  with your current build is dimmed and marked in the picker. Mods that only
  look similar (Augur Reach, Precision Intensify) are left alone because they
  genuinely stack.
- **load suggested build** fills all ten slots including aura and exilus,
  chosen from what the frame wants — Growing Power for a Strength frame,
  Cunning Drift for a Range one. Frames without a written profile say so
  instead of the button doing nothing. Every loadout is saved per item in your
  browser.

Mod values come from the game's own mod data, deduplicated so you get the real
mod rather than its Beginner/Intermediate/Expert training variants — that trap
had Intensify reading +55% Strength instead of +30%.

## Builds tab

Search anything that takes mods — **543 things**: 121 warframes, 418 weapons
and the companions worth bringing. The Parazon is deliberately left out.

For a **warframe** you get its real stats and abilities, the stat priorities
that matter for it (Strength, Range, Duration, Efficiency), core mods, augments
worth a slot, arcanes, an archon shard recommendation and a Helminth
suggestion — plus a read derived from its own numbers, e.g. high armour means
Umbral Fiber and Umbral Vitality multiply each other, while a thin frame
survives on abilities and needs Adaptation and Rolling Guard.

**All 66 frames** have a profile, and every one fills both arcane slots. The
newest frames are marked *provisional* — community consensus on them is thin,
and the file says so rather than pretending otherwise.

**Weapons and companions get real loadouts too**, chosen from the weapon's own
crit/status numbers: ten archetype packages (primary, shotgun, secondary and
melee × crit/status/raw/heavy) with the exact mods, an exilus pick and the
right arcanes. All 418 weapons resolve one.

**"Load suggested build"** fills the slots for real — mods, aura, exilus and
arcanes. A frame has two arcane slots, a weapon one, a companion none, exactly
as in game, and the pickers only offer arcanes that fit (a rifle is never shown
Melee Influence).

**Mods that cannot coexist refuse to.** The game groups mods by an internal
path rather than by name, so Split Chamber and Galvanized Chamber are one mod
and neither name says so. The app derives families from that path — 85 groups —
and drops the loser with an explanation when you fit the other.

Always on screen:

- the **archon shard** table (all six colours, Tauforged ≈ 1.5x)
- **the numbers behind every build**, from the wiki's own formula tables:
  Viral ×2 at one stack rising to ×4.25 at ten, Corrosive 26% then +6% per
  stack to 80%, Heat up to 50% ramping over two seconds, the enemy and player
  armour formulas, and shield gating
- **every arcane — 158 of them** — grouped by what it attaches to, each with
  its exact max-rank effect *and what it costs on the market right now*. That
  last part is the bit no wiki can give you: Primary Merciless at 3p is a
  different decision from Primary Merciless at 300p.

**Every mod, augment and arcane name in `build_guide.json` was validated**
against the game's 1,803-mod list and the market catalogue. Names that could
not be verified were removed rather than guessed — the file says so, and you
can edit it and restart to change any of it.

## Rivens tab

Type a weapon name — it completes as you type (arrow keys and Enter work) —
and you get, for that weapon:

- **What to roll for** — judged from the game's own rules, not from what rolls
  happen to sell for. Three columns: the stats to chase, the negatives that are
  genuinely free *on this weapon*, and the ones that attack the build. It shows
  the roll-shape maths too, because most people have it backwards: three
  positives **with** a negative roll at ×0.9375 against ×0.75 without one, so a
  negative is how you buy bigger positives, not a penalty you tolerate. Heat,
  Cold, Electricity, Toxin and Punch Through can never be your negative — they
  only roll positive — and the app says so.

  The per-weapon filtering is the point. −Zoom is the classic free negative but
  not on a **sniper**; −Recoil is a straight upgrade but a **bow** has none;
  −Ammo Max is fine until the weapon is a **beam** or a launcher; −Impact costs
  nothing unless the weapon is 80% Impact like **Rubico**; −Status Duration is
  free on a crit build and ruinous on a Slash one. All of that comes from the
  weapon's real damage split and fire behaviour, not from its name.

- **Grade a roll you own** — pick your riven's actual stats and get a verdict
  with the arithmetic shown. Only stats that can roll on that class appear, and
  the five positive-only attributes are kept out of the negative box.

- **On sale right now, graded** — every riven for that weapon currently listed,
  scored by those same rules and sorted best-grade-then-cheapest, with offline
  sellers dropped because you cannot trade with them. This answers the question
  people actually have: not "what should I roll for" but "is one worth buying,
  and which". The grade reflects what the *weapon* wants, not what the roll is
  worth in plat — a 55p B can be the right buy and a 480p S can be overpriced.

- **Melee: combo or heavy attack.** A Kronen can be built either way and the
  riven you want differs, so it is a toggle rather than something guessed. It
  re-ranks the live listings too — the same market where a 300p roll is the
  best combo buy has a 120p roll as the best heavy-attack one.
- **Is it worth a riven** — led by the weapon's **meta tier**, a consensus of
  dated community tier lists, then adjusted by disposition. Riven prices are
  deliberately *not* the lead signal: a god roll on a mediocre weapon still
  sells high, so price measures the roll rather than the weapon. Boltor is the
  example — its rivens go for ~150p but it is C tier, so the answer is no.
  Weapons with no consensus tier say so instead of inventing a rank.
- **Disposition**, mastery, crit and status, and live riven prices.

Rivens go by weapon **family**: a Kuva Nukor uses a *Nukor* riven, a Kuva Zarr
uses a *Zarr* riven and a Vasto Prime uses a *Vasto* riven, so only base names
appear. Weapon strength is rated on the strongest member of the family, since
one riven fits them all.

**Coda weapons** (Warframe 1999) are covered. Three carry their own riven —
Coda Bassocyst, Coda Motovore and Dual Coda Torxica — while the rest share the
base Infested weapon's riven, so a Coda Mire is looked up under *Mire* and a
Coda Pathocyst under *Pathocyst*. Note that Coda weapons themselves are not
tradeable through warframe.market's API: it has auction types for riven, lich
(Kuva) and sister (Tenet) only, with no Coda type yet.

Weapons with an **Incarnon Genesis** adapter are marked, and their tier is for
the evolved form — Strun and Boltor are mediocre until you fit the adapter and
genuinely good afterwards, so a single tier without that context would be
misleading either way.

**How to build it** — each weapon also gets a build direction derived from its
own crit and status numbers plus Warframe's damage rules: whether it wants a
crit, status or raw-damage build, the specific base-damage and multishot mods
for its class, which Galvanized mods take over once you hold kill stacks, and
why Viral + Heat is the Steel Path default (Viral multiplies damage to health,
Heat strips armour) versus Corrosive when you want armour gone outright. For
Incarnon weapons it says plainly that the listed numbers are the base weapon's
and the evolution branch decides the final build.

**Keeping it current.** Dispositions come live from warframe.market and are
re-fetched weekly, because Digital Extremes rebalances them every Prime Access
release. Meta tiers live in **`weapon_meta.json`** — 141 weapons, each with a
tier, an Incarnon flag, a note and an optional `disputed` flag, plus the source
list and date. Where lists genuinely disagree the weapon is marked
**contested** in the UI rather than given a false-confident tier. Edit that
file and restart; nothing in the code needs touching. The UI shows when the
file was last edited so you can tell at a glance how stale it is (Kuva Bramma,
for instance, was dropped from S tier by newer lists over damage falloff nerfs
while older ones still rate it top).

Two thirds of riven-capable weapons never appear on any community tier list —
they are the long tail nobody writes about. Rather than invent a rank, those
fall back to the weapon's own damage numbers against its class and are labelled
**FROM STATS**, with the caveat that raw numbers cannot see an Incarnon form or
an alt-fire. It is the difference between "no consensus tier, weak numbers"
(Flux Rifle, 19th percentile) and "no consensus tier but the numbers are
strong" (Magnus, 80th) — the old build gave both the same non-answer.

## Watching an item (⭐)

Every deal row and every item page has a **star**. One click puts the item on
the watchlist, which does two things: it gets re-checked every minute instead
of waiting for the sweep, and it becomes eligible for a **price target**.
Click again to unwatch. The Watchlist tab is where you set the target price.

## Dashboard digest

Because the scanner runs around the clock, the useful question when you sit
down is "what did I miss". The Dashboard opens with a summary of everything
caught since your last visit — how many deals and jackpots, and the biggest
one, preferring a deal that's **still live** over a larger one that has gone.

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
- `build_guide.json` — build knowledge: frame profiles, weapon packages, the
  arcane catalogue, the damage-mechanics figures
- `riven_guide.json` — what to roll a riven for, and which negatives are free
- `weapon_meta.json` — the weapon tier list behind the riven verdicts
- `audit.py` — consistency check for the two guide files (see below)
- `autostart.sh` — install/remove the background service

All three JSON files are meant to be edited. Change one, restart, done — no
code involved.

### Checking your edits

The failure mode with hand-edited knowledge files is not a crash, it is a rule
that quietly never fires: a trait declared but never set, an archetype nothing
can reach, a stat named for a weapon class that cannot roll it. Those look
fine and silently do nothing.

```bash
python3 audit.py
```

Nine checks, exit code 1 if anything is wrong. It has caught real bugs
repeatedly — three unreachable rules in three consecutive runs — so it is worth
running after any edit. It needs the scanner running for one of the checks.

Settings (thresholds, Discord webhook for phone pings, etc.) are edited live
in the UI's Settings tab and persist in the database.
