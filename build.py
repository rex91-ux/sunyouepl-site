#!/usr/bin/env python3
"""
Build index.html for the 损友俱乐部 Season 8 FPL wagers page.

TEMPLATE-DRIVEN. This script does NOT own the page design. It reads
template.html and rewrites only three marked regions plus the date stamp:

    <!-- LIVE-BLOCK:START -->   ... <!-- LIVE-BLOCK:END -->
    <!-- BLOCK-LEDGER:START --> ... <!-- BLOCK-LEDGER:END -->
    <!-- PNL:START -->          ... <!-- PNL:END -->

Everything else in template.html - the tabs, the six side bets, the settlement
matrices, the managers table, all the CSS - is hand-maintained and passes
through untouched. Edit template.html by hand for those; never edit index.html
(it is overwritten on every run).

Stdlib only.

    python3 build.py              build index.html
    python3 build.py --dry-run    print the summary, write nothing
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")
except Exception:                                    # pragma: no cover
    UK = timezone.utc

# ---------------------------------------------------------------- config

LEAGUE_ID = 589773
TEMPLATE = "template.html"
OUTPUT = "index.html"

MANAGERS = [
    (2917927, "RX"),
    (6191412, "AC"),
    (4768733, "Willis"),
    (4588693, "Kaleb"),
    (762086,  "Jeremy"),
    (2161517, "JR"),
    (31161,   "Zac"),
]

BLOCKS = [
    (1,  4), (5,  8), (9,  12), (13, 16), (17, 20),
    (21, 24), (25, 28), (29, 32), (33, 36), (37, 38),
]

# Block wager: $10 each, $70 pot. Gross prize by finishing slot.
BLOCK_STAKE = 10
BLOCK_PRIZES = [60, 10, 0, 0, 0, 0, 0]

# Season wager: $30 each, $210 pot.
SEASON_STAKE = 30
SEASON_PRIZES = [150, 60, 0, 0, 0, 0, 0]

TOTAL_GWS = 38
UA = "sunyouepl-site/2.0 (+https://sunyouepl.netlify.app)"


# ---------------------------------------------------------------- fetch

def get_json(url, attempts=4):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            # Deliberately broad: urlopen can raise RemoteDisconnected,
            # ssl.SSLError or ConnectionResetError, none of which are URLError.
            status = getattr(exc, "code", None)
            last = exc
            print(f"  attempt {i+1}/{attempts} failed on {url}: "
                  f"{type(exc).__name__}"
                  f"{f' (HTTP {status})' if status else ''}: {exc}",
                  file=sys.stderr)
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise SystemExit(f"FATAL: could not fetch {url} after {attempts} tries: "
                     f"{type(last).__name__}: {last}")


def fetch_events():
    data = get_json("https://fantasy.premierleague.com/api/bootstrap-static/")
    out = {}
    for ev in data.get("events", []):
        out[ev["id"]] = {
            "finished": bool(ev.get("finished")),
            "data_checked": bool(ev.get("data_checked")),
            "is_current": bool(ev.get("is_current")),
            "is_next": bool(ev.get("is_next")),
            "deadline": ev.get("deadline_time"),
        }
    return out


def fetch_history(entry_id):
    """{gw: cumulative total_points}. We use cumulative totals rather than the
    per-gameweek 'points' field because total_points is unambiguously net of
    transfer hits - so a block total computed by differencing needs no
    assumption about whether a -4 has already been applied."""
    data = get_json(f"https://fantasy.premierleague.com/api/entry/{entry_id}/history/")
    return {row["event"]: row["total_points"] for row in data.get("current", [])}


def fetch_league():
    try:
        data = get_json(
            f"https://fantasy.premierleague.com/api/leagues-classic/{LEAGUE_ID}/standings/",
            attempts=2)
    except SystemExit:
        print("WARN: league endpoint unavailable, skipping cross-check",
              file=sys.stderr)
        return {}
    return {r["entry"]: (r.get("event_total"), r.get("total"))
            for r in data.get("standings", {}).get("results", [])}


# ---------------------------------------------------------------- scoring

def is_settled(events, gw):
    e = events.get(gw)
    return bool(e and e["finished"] and e["data_checked"])


def cum_at(cum, gw):
    if gw <= 0:
        return 0
    for g in range(gw, 0, -1):
        if g in cum:
            return cum[g]
    return 0


def block_points(cum, start, end):
    return cum_at(cum, end) - cum_at(cum, start - 1)


def gw_points(cum, gw):
    """Net points in a single gameweek, or None if not played yet."""
    if gw not in cum:
        return None
    return cum[gw] - cum_at(cum, gw - 1)


def payouts(scores, stake, prizes):
    """Net P&L per manager. Ties split the combined gross prize of the slots
    they jointly occupy, evenly. Two tied for 1st share $60+$10 -> +$25 each."""
    out, idx = {}, 0
    for value in sorted(set(scores.values()), reverse=True):
        tied = sorted([n for n, v in scores.items() if v == value])
        share = sum(prizes[idx:idx + len(tied)]) / len(tied)
        for n in tied:
            out[n] = share - stake
        idx += len(tied)
    return out


def rank_table(scores):
    rows, pos = [], 1
    for value in sorted(set(scores.values()), reverse=True):
        tied = sorted([n for n, v in scores.items() if v == value])
        rows.append((pos, tied, value))
        pos += len(tied)
    return rows


def compute(events, cums):
    names = [n for _, n in MANAGERS]
    blocks = []

    for i, (start, end) in enumerate(BLOCKS, start=1):
        gws = list(range(start, end + 1))
        final = [g for g in gws if is_settled(events, g)]
        started = any(events.get(g, {}).get("finished")
                      or events.get(g, {}).get("is_current") for g in gws)
        scores = {n: block_points(cums[n], start, end) for n in names}
        settled = len(final) == len(gws)
        blocks.append({
            "n": i, "start": start, "end": end, "gws": gws,
            "final": len(final), "total": len(gws),
            "scores": scores, "table": rank_table(scores),
            "settled": settled, "started": started,
            "status": "settled" if settled else ("in progress" if started else "not started"),
            "payouts": payouts(scores, BLOCK_STAKE, BLOCK_PRIZES) if settled else None,
            "provisional": payouts(scores, BLOCK_STAKE, BLOCK_PRIZES) if started else None,
        })

    season_scores = {n: cum_at(cums[n], TOTAL_GWS) for n in names}
    season_settled = all(is_settled(events, g) for g in range(1, TOTAL_GWS + 1))
    season_started = any(events.get(g, {}).get("finished")
                         or events.get(g, {}).get("is_current")
                         for g in range(1, TOTAL_GWS + 1))
    season = {
        "scores": season_scores, "table": rank_table(season_scores),
        "settled": season_settled, "started": season_started,
        "status": "settled" if season_settled else ("in progress" if season_started else "not started"),
        "payouts": payouts(season_scores, SEASON_STAKE, SEASON_PRIZES) if season_settled else None,
    }

    # per-manager tallies
    block_pnl = {n: 0.0 for n in names}
    won = {n: 0 for n in names}
    second = {n: 0 for n in names}
    for b in blocks:
        if not b["settled"]:
            continue
        for n, v in b["payouts"].items():
            block_pnl[n] += v
        for pos, tied, _ in b["table"]:
            if pos == 1:
                for n in tied:
                    won[n] += 1
            elif pos == 2:
                for n in tied:
                    second[n] += 1
    season_pnl = {n: (season["payouts"][n] if season_settled else 0.0) for n in names}

    live = next((b for b in blocks if not b["settled"] and b["started"]), None)
    if live is None:
        live = next((b for b in blocks if not b["settled"]), blocks[-1])

    current = next((g for g, e in events.items() if e["is_current"]), None)
    nxt = next((g for g, e in events.items() if e["is_next"]), None)
    deadline = events[nxt]["deadline"] if nxt else (
        events[current]["deadline"] if current else None)

    return {
        "blocks": blocks, "season": season, "live": live,
        "block_pnl": block_pnl, "season_pnl": season_pnl,
        "won": won, "second": second,
        "current_gw": current, "next_gw": nxt, "deadline": deadline,
        "settled_gws": sorted([g for g in events if is_settled(events, g)]),
        "season_started": season_started,
        "cums": cums,
    }


# ---------------------------------------------------------------- format

def money(v):
    if abs(v) < 0.005:
        return "$0"
    s = f"{abs(v):,.2f}".rstrip("0").rstrip(".")
    return ("+$" if v > 0 else "−$") + s


def net_class(v):
    if v > 0.005:
        return "pos-net"
    if v < -0.005:
        return "neg-net"
    return "zero-net"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def uk_time(iso):
    if not iso:
        return "TBC"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UK)
    return dt.strftime("%a %-d %b %Y, %H:%M UK")


# ---------------------------------------------------------------- regions

def render_live_block(d, stamp):
    b = d["live"]
    names = [n for _, n in MANAGERS]
    leader_pts = b["table"][0][2]

    pill_cls = "done" if b["settled"] else "prog"
    pill_txt = (f"settled · {b['final']} of {b['total']} GWs final"
                if b["settled"] else
                f"in progress · {b['final']} of {b['total']} GWs final")

    if not b["started"]:
        asof = (f"Not started — GW{b['start']} deadline {uk_time(d['deadline'])}. "
                f"Refreshes daily at 6pm SGT.")
    elif b["settled"]:
        asof = (f"All {b['total']} gameweeks final — this block is booked. "
                f"Updated {stamp}.")
    else:
        gw = d["current_gw"] or b["start"]
        asof = (f"GW{gw} live · next deadline {uk_time(d['deadline'])}. "
                f"Updated {stamp}. Refreshes daily at 6pm SGT.")

    proj_head = "Payout" if b["settled"] else "If it ended now"
    gw_heads = "".join(f"<th>GW{g}</th>" for g in b["gws"])

    # position lookup
    pos_of = {}
    for pos, tied, _ in b["table"]:
        for n in tied:
            pos_of[n] = pos

    ordered = sorted(names, key=lambda n: (pos_of[n], n))
    rows = []
    for n in ordered:
        pts = b["scores"][n]
        cum = d["cums"][n]
        cells = []
        for g in b["gws"]:
            p = gw_points(cum, g)
            cells.append(f'<td class="gw">{p if p is not None else "—"}</td>')

        if not b["started"]:
            pos_txt, gap, proj = "—", "—", '<span class="pending">—</span>'
        else:
            pos_txt = str(pos_of[n])
            gap = "—" if pts == leader_pts else f"−{leader_pts - pts}"
            v = b["provisional"][n]
            proj = f'<span class="{net_class(v)}">{money(v)}</span>'

        leader_cls = ' class="leader"' if b["started"] and pos_of[n] == 1 else ""
        rows.append(
            f'        <tr{leader_cls}><td class="scen">{pos_txt}</td>'
            f'<td class="name">{esc(n)}</td>{"".join(cells)}'
            f'<td class="tot">{pts}</td><td class="gap">{gap}</td>'
            f'<td class="proj">{proj}</td></tr>')

    note = ("Booked. These are the final block totals and the money that changed hands."
            if b["settled"] else
            'Provisional. "If it ended now" is what each person would collect on '
            "today's block totals — it is not booked until all "
            f"{b['total']} gameweeks are finalised.")

    return f"""  <div class="hero">
    <div class="hero-top">
      <span class="hero-title">Live · Block {b['n']} <span style="color:var(--muted);font-weight:500">· GW {b['start']}–{b['end']}</span></span>
      <span class="pill {pill_cls}">{pill_txt}</span>
    </div>
    <div class="asof">{asof}</div>
    <table class="live-tbl">
      <thead>
        <tr><th class="scen">#</th><th class="scen">Player</th>{gw_heads}<th>Block pts</th><th>Gap</th><th>{proj_head}</th></tr>
      </thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>
    <div class="note" style="margin:6px 0 10px">{note}</div>
  </div>"""


def render_ledger(d):
    rows = []
    for b in d["blocks"]:
        if b["settled"]:
            winners = b["table"][0][1]
            runners = b["table"][1][1] if len(b["table"]) > 1 else []
            w_amt = b["payouts"][winners[0]]
            r_amt = b["payouts"][runners[0]] if runners else 0.0
            w_cell = (f'<span class="pos-net">{esc(", ".join(winners))}</span>'
                      f'{f" ({money(w_amt)})" if len(winners) > 1 else ""}')
            r_cell = (f'{esc(", ".join(runners))}'
                      f'{f" ({money(r_amt)})" if len(runners) > 1 else ""}'
                      if runners else '<span class="pending">—</span>')
            status = "settled"
        elif b["started"]:
            lead = b["table"][0]
            w_cell = (f'<span class="pending">{esc(", ".join(lead[1]))} leading '
                      f'({lead[2]})</span>')
            r_cell = '<span class="pending">—</span>'
            status = f"in progress · {b['final']}/{b['total']}"
        else:
            w_cell = '<span class="pending">—</span>'
            r_cell = '<span class="pending">—</span>'
            status = '<span class="pending">not started</span>'
        rows.append(
            f'        <tr><td class="scen">{b["n"]}</td><td>GW {b["start"]}–{b["end"]}</td>'
            f'<td>{w_cell}</td><td>{r_cell}</td><td>{status}</td></tr>')

    s = d["season"]
    if s["settled"]:
        w = s["table"][0][1]
        r = s["table"][1][1] if len(s["table"]) > 1 else []
        s_w = (f'<span class="pos-net">{esc(", ".join(w))}</span> '
               f'({money(s["payouts"][w[0]])})')
        s_r = (f'{esc(", ".join(r))} ({money(s["payouts"][r[0]])})'
               if r else '<span class="pending">—</span>')
        s_status = "settled"
    elif s["started"]:
        lead = s["table"][0]
        s_w = f'<span class="pending">{esc(", ".join(lead[1]))} leading ({lead[2]})</span>'
        s_r = '<span class="pending">—</span>'
        s_status = f'<span class="pending">in progress · {len(d["settled_gws"])}/38</span>'
    else:
        s_w = '<span class="pending">— (+$120)</span>'
        s_r = '<span class="pending">— (+$30)</span>'
        s_status = '<span class="pending">not started</span>'

    return f"""  <h3 style="margin-top:24px">Block ledger</h3>
  <div class="card" style="padding-top:6px">
    <table>
      <thead>
        <tr><th class="scen">Block</th><th>Gameweeks</th><th>Winner (+$50)</th><th>2nd ($0)</th><th>Status</th></tr>
      </thead>
      <tbody>
{chr(10).join(rows)}
        <tr><td class="scen"><b>Season</b></td><td>GW 1–38</td><td>{s_w}</td><td>{s_r}</td><td>{s_status}</td></tr>
      </tbody>
    </table>
  </div>
"""


def render_pnl(d, stamp):
    names = [n for _, n in MANAGERS]
    totals = {n: d["block_pnl"][n] + d["season_pnl"][n] for n in names}
    booked = sum(1 for b in d["blocks"] if b["settled"])

    rows = []
    for n in sorted(names, key=lambda x: (-totals[x], x)):
        bp, sp, tp = d["block_pnl"][n], d["season_pnl"][n], totals[n]
        rows.append(
            f'        <tr><td class="name">{esc(n)}</td>'
            f'<td>{d["won"][n]}</td><td>{d["second"][n]}</td>'
            f'<td class="{net_class(bp)}">{money(bp)}</td>'
            f'<td class="{net_class(sp)}">{money(sp)}</td>'
            f'<td class="{net_class(tp)}">{money(tp)}</td></tr>')

    if not d["season_started"]:
        note = (f"Season has not kicked off — GW1 deadline is "
                f"{uk_time(d['deadline'])}. All figures update once blocks settle.")
    elif booked == 0:
        note = (f"{len(d['settled_gws'])}/38 gameweeks final. No block has "
                f"completed yet, so nothing is booked.")
    else:
        note = (f"{booked} of 10 blocks booked · {len(d['settled_gws'])}/38 "
                f"gameweeks final.")

    return f"""  <h3 style="margin-top:24px">Running P&amp;L</h3>
  <div class="card" style="padding-top:6px">
    <table>
      <thead>
        <tr><th class="scen">Player</th><th>Blocks won</th><th>Blocks 2nd</th><th>Block P&amp;L</th><th>Season P&amp;L</th><th>Total</th></tr>
      </thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>
    <div class="note">{note}<br>Auto-refreshed daily at 6pm SGT from the FPL API. Blocks in progress show live totals but book no P&amp;L until every gameweek in them is finalised.<br>Built {stamp} ({datetime.now(timezone.utc).strftime('%H:%M')} UTC).</div>
  </div>
"""


# ---------------------------------------------------------------- assemble

def replace_region(html, name, inner):
    start, end = f"<!-- {name}:START -->", f"<!-- {name}:END -->"
    i, j = html.find(start), html.find(end)
    if i == -1 or j == -1 or j < i:
        raise SystemExit(f"FATAL: marker {name}:START/END missing or out of "
                         f"order in {TEMPLATE}")
    return html[:i] + start + "\n" + inner.rstrip("\n") + "\n  " + html[j:]


def build(template, d, now):
    stamp = now.strftime("%-d %b %Y")
    html = template
    html = replace_region(html, "LIVE-BLOCK", render_live_block(d, stamp))
    html = replace_region(html, "BLOCK-LEDGER", render_ledger(d))
    html = replace_region(html, "PNL", render_pnl(d, stamp))

    html, n = re.subn(r'(<span class="tag">[^<]*?updated )[^<]*(</span>)',
                      lambda m: m.group(1) + stamp + m.group(2), html, count=1)
    if n != 1:
        raise SystemExit('FATAL: could not find the \'<span class="tag">'
                         '... updated ...\' date stamp in the template')
    return html


# ---------------------------------------------------------------- main

def main():
    dry = "--dry-run" in sys.argv

    try:
        with open(TEMPLATE, encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        raise SystemExit(f"FATAL: {TEMPLATE} not found. It holds the page "
                         f"design; index.html is generated from it.")

    events = fetch_events()
    cums = {}
    for entry_id, name in MANAGERS:
        cums[name] = fetch_history(entry_id)
        time.sleep(0.3)

    d = compute(events, cums)

    league = fetch_league()
    for entry_id, name in MANAGERS:
        if entry_id in league and cums[name]:
            _, lg = league[entry_id]
            mine = cum_at(cums[name], max(cums[name]))
            if lg is not None and mine != lg:
                print(f"WARN: {name} total mismatch — history {mine}, "
                      f"league table {lg}", file=sys.stderr)

    html = build(template, d, datetime.now(timezone.utc))

    print(f"settled GWs : {d['settled_gws'] or 'none'}")
    print(f"current GW  : {d['current_gw']}   next: {d['next_gw']}")
    print(f"live block  : B{d['live']['n']} ({d['live']['status']})")
    for b in d["blocks"]:
        if b["settled"]:
            print(f"  B{b['n']} SETTLED winner={b['table'][0][1]} "
                  f"{ {k: round(v, 2) for k, v in b['payouts'].items()} }")
    tot = {n: round(d['block_pnl'][n] + d['season_pnl'][n], 2)
           for _, n in MANAGERS}
    print(f"P&L         : {tot}")
    print(f"zero-sum    : {round(sum(tot.values()), 6)}")

    if dry:
        print("\n--dry-run: index.html not written")
        return

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nwrote {OUTPUT} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
