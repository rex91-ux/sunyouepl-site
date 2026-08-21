#!/usr/bin/env python3
"""Offline tests for build.py's scoring/payout logic.

The FPL API is not reachable from the build sandbox, so these tests inject
synthetic events + histories instead of hitting the network. They verify the
money math, tie handling, block settlement gating, and that the template
renders for every season state.
"""
import sys
from datetime import datetime, timezone

import build as B

NAMES = [n for _, n in B.MANAGERS]
FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got}\n        want {want}")
        FAILED.append(label)


def approx(d):
    return {k: round(v, 2) for k, v in d.items()}


def mk_events(settled_upto=0, current=None, nxt=None):
    ev = {}
    for g in range(1, 39):
        ev[g] = {
            "finished": g <= settled_upto,
            "data_checked": g <= settled_upto,
            "is_current": g == current,
            "is_next": g == nxt,
            "deadline": f"2026-08-{min(21 + g, 28):02d}T17:30:00Z",
        }
    return ev


def mk_cums(per_gw):
    """per_gw: {name: [pts_gw1, pts_gw2, ...]} -> cumulative dicts"""
    out = {}
    for name, seq in per_gw.items():
        cum, run = {}, 0
        for i, p in enumerate(seq, start=1):
            run += p
            cum[i] = run
        out[name] = cum
    return out


print("\n1. payout math — blocks")

# clean win, no ties
s = {"RX": 300, "AC": 290, "Willis": 280, "Kaleb": 270,
     "Jeremy": 260, "JR": 250, "Zac": 240}
p = B.payouts(s, B.BLOCK_STAKE, B.BLOCK_PRIZES)
check("winner +50", p["RX"], 50)
check("2nd 0", p["AC"], 0)
check("3rd -10", p["Willis"], -10)
check("7th -10", p["Zac"], -10)
check("zero sum", round(sum(p.values()), 6), 0)

# two tied for 1st -> spec example: +25 each
s2 = dict(s); s2["AC"] = 300
p2 = B.payouts(s2, B.BLOCK_STAKE, B.BLOCK_PRIZES)
check("2-way tie 1st = +25 each", (p2["RX"], p2["AC"]), (25, 25))
check("3rd unaffected", p2["Willis"], -10)
check("zero sum", round(sum(p2.values()), 6), 0)

# two tied for 2nd -> share slot2+slot3 = 10+0 = 10 -> 5 each -> net -5
s3 = dict(s); s3["Willis"] = 290
p3 = B.payouts(s3, B.BLOCK_STAKE, B.BLOCK_PRIZES)
check("winner still +50", p3["RX"], 50)
check("2-way tie 2nd = -5 each", (p3["AC"], p3["Willis"]), (-5, -5))
check("zero sum", round(sum(p3.values()), 6), 0)

# three tied for 1st -> 70/3 each -> net +13.33
s4 = dict(s); s4["AC"] = 300; s4["Willis"] = 300
p4 = B.payouts(s4, B.BLOCK_STAKE, B.BLOCK_PRIZES)
check("3-way tie 1st", round(p4["RX"], 2), 13.33)
check("zero sum", round(sum(p4.values()), 6), 0)

# all seven tied -> everyone flat
p5 = B.payouts({n: 100 for n in NAMES}, B.BLOCK_STAKE, B.BLOCK_PRIZES)
check("7-way tie = all flat", set(p5.values()), {0})

print("\n2. payout math — season")
ps = B.payouts(s, B.SEASON_STAKE, B.SEASON_PRIZES)
check("winner +120", ps["RX"], 120)
check("2nd +30", ps["AC"], 30)
check("3rd -30", ps["Willis"], -30)
check("zero sum", round(sum(ps.values()), 6), 0)

ps2 = B.payouts(s2, B.SEASON_STAKE, B.SEASON_PRIZES)
check("2-way tie 1st = +75 each", (ps2["RX"], ps2["AC"]), (75, 75))
check("zero sum", round(sum(ps2.values()), 6), 0)

print("\n3. block totals use net cumulative diff")
cums = mk_cums({n: [10] * 38 for n in NAMES})
check("B1 (GW1-4) = 40", B.block_points(cums["RX"], 1, 4), 40)
check("B2 (GW5-8) = 40", B.block_points(cums["RX"], 5, 8), 40)
check("B10 (GW37-38) = 20", B.block_points(cums["RX"], 37, 38), 20)
# a negative gameweek swing (big hit) must not break the diff
neg = mk_cums({"RX": [50, -5, 60, 40] + [10] * 34})
check("negative GW handled", B.block_points(neg["RX"], 1, 4), 145)

print("\n4. settlement gating")
# GW1-3 final, GW4 not -> B1 must NOT settle
ev = mk_events(settled_upto=3, current=4)
d = B.compute(ev, cums)
check("B1 not settled at 3/4 final", d["blocks"][0]["settled"], False)
check("B1 status live", d["blocks"][0]["status"], "live")
check("B1 books no money", d["blocks"][0]["payouts"], None)
check("P&L all zero", set(approx(d["pnl"]).values()), {0})

# GW4 final -> B1 settles
ev = mk_events(settled_upto=4, current=5)
d = B.compute(ev, cums)
check("B1 settled at 4/4", d["blocks"][0]["settled"], True)
check("B1 pays out (7-way tie -> flat)", set(approx(d["blocks"][0]["payouts"]).values()), {0})
check("live block moves to B2", d["live"]["n"], 2)

# distinct scores so a real winner emerges
per = {"RX": [20] * 38, "AC": [19] * 38, "Willis": [18] * 38, "Kaleb": [17] * 38,
       "Jeremy": [16] * 38, "JR": [15] * 38, "Zac": [14] * 38}
c2 = mk_cums(per)
d = B.compute(mk_events(settled_upto=4, current=5), c2)
check("B1 winner RX", d["blocks"][0]["table"][0][1], ["RX"])
check("B1 RX +50", d["blocks"][0]["payouts"]["RX"], 50)
check("cumulative P&L RX = 50", round(d["pnl"]["RX"], 2), 50)

# two blocks settled
d = B.compute(mk_events(settled_upto=8, current=9), c2)
check("2 blocks booked", sum(1 for b in d["blocks"] if b["settled"]), 2)
check("RX P&L = 100", round(d["pnl"]["RX"], 2), 100)
check("Zac P&L = -20", round(d["pnl"]["Zac"], 2), -20)
check("P&L zero sum", round(sum(d["pnl"].values()), 6), 0)

print("\n5. season settlement")
d = B.compute(mk_events(settled_upto=37, current=38), c2)
check("season not settled at GW37", d["season"]["settled"], False)
d = B.compute(mk_events(settled_upto=38), c2)
check("season settled at GW38", d["season"]["settled"], True)
check("season winner RX", d["season"]["table"][0][1], ["RX"])
check("RX season +120", d["season"]["payouts"]["RX"], 120)
# 10 blocks (10*50) + season 120 = 620
check("RX total P&L = 620", round(d["pnl"]["RX"], 2), 620)
check("full-season zero sum", round(sum(d["pnl"].values()), 6), 0)

print("\n6. pre-season state")
ev0 = mk_events(settled_upto=0, nxt=1)
c0 = {n: {} for n in NAMES}
d0 = B.compute(ev0, c0)
check("season_started False", d0["season_started"], False)
check("no blocks settled", sum(1 for b in d0["blocks"] if b["settled"]), 0)
check("live block = B1", d0["live"]["n"], 1)
check("B1 status not started", d0["blocks"][0]["status"], "not started")
check("all P&L zero", set(approx(d0["pnl"]).values()), {0})
check("deadline picked up", d0["deadline"], "2026-08-22T17:30:00Z")

print("\n7. render smoke test — every season state")
now = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
for label, (ee, cc) in {
    "pre-season": (ev0, c0),
    "mid-block":  (mk_events(settled_upto=2, current=3), c2),
    "post-block": (mk_events(settled_upto=8, current=9), c2),
    "final":      (mk_events(settled_upto=38), c2),
}.items():
    html = B.render(B.compute(ee, cc), now)
    ok = ("<!DOCTYPE html>" in html and "</html>" in html.strip()[-10:]
          and "updated 21 Aug 2026" in html and len(html) > 3000)
    check(f"{label} renders", ok, True)

print("\n8. money formatting")
check("positive", B.money(50), "+$50")
check("negative", B.money(-10), "−$10")
check("zero", B.money(0), "$0")
check("cents", B.money(13.333333), "+$13.33")
check("neg cents", B.money(-4.5), "−$4.5")

print("\n" + "=" * 46)
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("all tests passed")
