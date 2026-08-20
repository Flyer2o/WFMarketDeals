#!/usr/bin/env python3
"""Consistency audit for the knowledge files.

The build and riven guides are hand-edited data, and the failure mode that
keeps recurring is not a crash — it is a rule that quietly never fires. A trait
declared but never set, an archetype nothing can reach, a stat named in the
guide that the weapon class cannot actually roll: all of these look fine and
silently do nothing.

Run this after editing riven_guide.json or build_guide.json:

    python3 audit.py

Checks, in order:
  1  traits declared in riven_guide vs traits riven_traits() can set
  2  archetypes declared vs archetypes any weapon can reach
  3  every stat named in the guide is rollable on the class it is listed for
  4  every weapon build package is reachable
  5  every mod and arcane name resolves against the live data
  6  no suggested build contains two mods of the same family
  6b verdict values the grader can actually score
  6c every frame fills both arcane slots with real mods
  7  arcane slot pickers only offer arcanes of the right kind

Needs the scanner running on 127.0.0.1:8737 for the family map in check 6.
"""
import json, re, sqlite3, sys, collections, pathlib
D=str(pathlib.Path(__file__).resolve().parent) + "/"
rg=json.load(open(D+"riven_guide.json"))
bg=json.load(open(D+"build_guide.json"))
src=open(D+"wfm_app.py").read()
conn=sqlite3.connect(D+"wfm_market.db"); conn.row_factory=sqlite3.Row
mods={r[0] for r in conn.execute("SELECT name FROM game_mods")}
mkt={r[0] for r in conn.execute("SELECT name FROM items")}
issues=[]
def flag(cat,msg): issues.append((cat,msg))

# 1. traits declared vs produced
declared=set()
for c in rg["negatives"]["candidates"]: declared |= set(c.get("unsafe_when") or [])
fn=src[src.index("def riven_traits"):src.index("def riven_stat_plan")]
produced=set(re.findall(r't\.add\("([a-z_]+)"\)', fn))
for m in re.findall(r't \|= \{([^}]*)\}', fn):
    produced |= {x.strip().strip('"') for x in m.split(",") if x.strip()}
for t in sorted(declared-produced): flag("dead-trait", f"'{t}' declared in riven_guide but never set")

# 2. archetypes declared vs reachable
arch=set(rg["positives"])
reach=set()
for w in conn.execute("SELECT * FROM riven_weapons"):
    c=w["crit_chance"] or 0; s=w["proc_chance"] or 0
    focus="raw" if max(c,s)<0.15 else ("crit" if c>=s else "status")
    is_m=(w["grp"] or "").lower() in ("melee","zaw") or "melee" in (w["wtype"] or "").lower()
    cls="melee" if is_m else "gun"
    for f in ([focus,"heavy"] if is_m else [focus]):
        k=f"{cls}_{f}"
        reach.add(k if k in arch else ("melee_crit" if is_m else "gun_crit"))
for a in sorted(arch-reach): flag("dead-archetype", f"'{a}' unreachable")

# 3. every stat named in the guide must be a real rollable attribute
attrs=rg["attributes"]
for key,rows in rg["positives"].items():
    pool=set(attrs["melee" if key.startswith("melee") else "gun"])
    for r in rows:
        if r["stat"] not in pool:
            flag("bad-stat", f"positives/{key}: '{r['stat']}' cannot roll on that class")
for c in rg["negatives"]["candidates"]:
    for cls in c["classes"]:
        pool=set(attrs["melee" if cls=="melee" else "gun"])
        if c["stat"] not in pool:
            flag("bad-stat", f"negatives/{c['stat']}: listed for {cls} but cannot roll there")
        if c["stat"] in attrs["positive_only"]:
            flag("bad-stat", f"negatives/{c['stat']}: is positive-only, can never be a negative")
allstats=set(attrs["gun"])|set(attrs["melee"])
for a in rg["negatives"]["avoid"]:
    if a["stat"] not in allstats:
        flag("bad-stat", f"avoid/'{a['stat']}' is not a rollable attribute")

# 4. build packages: reachable + names valid
wb=bg["weapon_builds"]
used=set()
for w in conn.execute("SELECT * FROM riven_weapons"):
    grp=(w["grp"] or "").lower(); wt=(w["wtype"] or "").lower()
    kind=("melee" if grp in ("melee","zaw") or "melee" in wt else
          "shotgun" if "shotgun" in wt or "shotgun" in grp else
          "secondary" if grp in ("secondary","pistol","kitgun") or "pistol" in wt else "primary")
    c=w["crit_chance"] or 0; s=w["proc_chance"] or 0
    focus="raw" if max(c,s)<0.15 else ("crit" if c>=s else "status")
    for f in ([focus,"heavy"] if kind=="melee" else [focus]):
        for cand in (f"{kind}_{f}", f"{kind}_crit", "primary_crit"):
            if cand in wb: used.add(cand); break
for k in sorted(set(wb)-used): flag("dead-package", f"weapon_builds/'{k}' unreachable")

# 5. every mod/arcane name resolves
for k,p in wb.items():
    for n in (p.get("mods") or [])+([p["exilus"]] if p.get("exilus") else []):
        if n not in mods: flag("bad-name", f"weapon_builds/{k}: mod '{n}' not in game_mods")
    for a in p.get("arcanes") or []:
        if a not in mkt: flag("bad-name", f"weapon_builds/{k}: arcane '{a}' not in market")
for f,p in bg["frames"].items():
    for kk in ("mods","augments","arcanes"):
        for n in p.get(kk) or []:
            if n not in mods and n not in mkt:
                flag("bad-name", f"frames/{f}/{kk}: '{n}' resolves to nothing")
for a in (bg["arcanes"].get("warframe",[])+bg["arcanes"].get("weapon",[])):
    if a["name"] not in mkt: flag("bad-name", f"arcanes: '{a['name']}' not in market")

# 6. no suggested build may contain two mods of the same family
import urllib.request
fam={}
try:
    for slot in ("primary","secondary","melee","warframe","companion","aura","exilus"):
        for m in json.load(urllib.request.urlopen(
                f"http://127.0.0.1:8737/api/mods?slot={slot}", timeout=25)):
            fam[m["name"]]=m["family"]
except Exception as e:
    flag("audit", f"could not read the live family map ({e})")
def check_conf(label, names):
    seen={}
    for n in names:
        f=fam.get(n)
        if not f: continue
        if f in seen: flag("self-conflict", f"{label}: '{seen[f]}' + '{n}' share family {f}")
        seen[f]=n
for k,pk in wb.items():
    check_conf(f"weapon_builds/{k}", (pk.get("mods") or [])+([pk["exilus"]] if pk.get("exilus") else []))
check_conf("companion_builds", bg["companion_builds"]["survivability"]["mods"])
for f,pf in bg["frames"].items():
    check_conf(f"frames/{f}", (pf.get("mods") or [])+(pf.get("augments") or []))

# 6b. verdicts the grader maps to a score - an unknown one is a 500 at runtime
KNOWN_VERDICTS={"best","good","situational"}
for c in rg["negatives"]["candidates"]:
    if c.get("verdict") not in KNOWN_VERDICTS:
        flag("bad-verdict", f"negatives/{c['stat']}: verdict '{c.get('verdict')}' "
                            f"is not one the grader can score")

# 6c. every frame must fill both arcane slots and name a real aura/exilus
for f,pf in bg["frames"].items():
    if len(pf.get("arcanes") or []) < 2:
        flag("frame-gap", f"frames/{f}: only {len(pf.get('arcanes') or [])} arcane(s)")
    for kk in ("aura","exilus"):
        v=pf.get(kk)
        if v and v not in mods: flag("bad-name", f"frames/{f}/{kk}: '{v}' not a mod")

# 7. arcane slot pools must only offer legal arcanes
arc=bg["arcanes"]
wf=set(arc.get("warframe_names",[]))
PREFIX_WF=("Molt ",)      # same prefix families the code treats as Warframe
def is_wf(n): return n in wf or n.startswith(PREFIX_WF)
for a in arc.get("warframe",[]):
    if not is_wf(a["name"]):
        flag("arcane-slot", f"warframe picker offers '{a['name']}' which is not on the verified Warframe list")
for a in arc.get("weapon",[]):
    if not re.match(r"^(Primary|Secondary|Melee) ", a["name"]):
        flag("arcane-slot", f"weapon picker offers '{a['name']}' with no weapon-class prefix")

print(f"  checks run over {len(list(conn.execute('SELECT 1 FROM riven_weapons')))} weapons, "
      f"{len(wb)} build packages, {len(bg['frames'])} frames, {len(mods)} mods")
if not issues:
    print("\n  CLEAN — no dead config, no unreachable rules, every name resolves")
else:
    by=collections.defaultdict(list)
    for c,m in issues: by[c].append(m)
    print(f"\n  {len(issues)} issue(s):")
    for c in sorted(by):
        print(f"    [{c}]")
        for m in by[c]: print(f"       {m}")

sys.exit(1 if issues else 0)
