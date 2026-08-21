#!/usr/bin/env python3
"""
Build index.html for the 损友俱乐部 Season 8 FPL wagers dashboard.

Pulls the FPL API, computes the 10 block wagers + the season wager, and writes
a self-contained index.html to the repo root. Netlify serves that file verbatim.

Stdlib only - no pip install needed.

Run locally:  python3 build.py
              python3 build.py --dry-run     (print summary, don't write)
"""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- config

LEAGUE_ID = 589773

MANAGERS = [
    (2917927, "RX"),
    (6191412, "AC"),
    (4768733, "Willis"),
    (4588693, "Kaleb"),
    (762086,  "Jeremy"),
    (2161517, "JR"),
    (31161,   "Zac"),
]

# B1..B10 as inclusive gameweek ranges
BLOCKS = [
    (1,  4), (5,  8), (9,  12), (13, 16), (17, 20),
    (21, 24), (25, 28), (29, 32), (33, 36), (37, 38),
]

# Block wager: $10 each, $70 pot. Gross prize by finishing slot.
#   winner $60 (net +50), 2nd $10 (net 0), 3rd-7th $0 (net -10)
BLOCK_STAKE = 10
BLOCK_PRIZES = [60, 10, 0, 0, 0, 0, 0]

# Season wager: $30 each, $210 pot.
#   winner $150 (net +120), 2nd $60 (net +30), 3rd-7th $0 (net -30)
SEASON_STAKE = 30
SEASON_PRIZES = [150, 60, 0, 0, 0, 0, 0]

TOTAL_GWS = 38
UA = "sunyouepl-site/1.0 (+https://sunyouepl.netlify.app)"


# ---------------------------------------------------------------- fetch

def get_json(url, attempts=4):
    """GET a URL and parse JSON, with backoff. The FPL API rejects the
    default urllib User-Agent, so we set our own."""
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            # Deliberately broad: urlopen can raise things that are neither
            # URLError nor HTTPError (RemoteDisconnected, ssl.SSLError,
            # ConnectionResetError). An uncaught one of those aborts the whole
            # build instantly instead of retrying.
            status = getattr(exc, "code", None)
            last = exc
            print(f"  attempt {i + 1}/{attempts} failed on {url}: "
                  f"{type(exc).__name__}"
                  f"{f' (HTTP {status})' if status else ''}: {exc}",
                  file=sys.stderr)
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise SystemExit(f"FATAL: could not fetch {url} after {attempts} tries: "
                     f"{type(last).__name__}: {last}")


def fetch_events():
    """Return {gw_id: {'finished':bool,'data_checked':bool,'is_current':bool,
    'deadline':str}} plus the raw list."""
    data = get_json("https://fantasy.premierleague.com/api/bootstrap-static/")
    events = {}
    for ev in data.get("events", []):
        events[ev["id"]] = {
            "finished": bool(ev.get("finished")),
            "data_checked": bool(ev.get("data_checked")),
            "is_current": bool(ev.get("is_current")),
            "is_next": bool(ev.get("is_next")),
            "deadline": ev.get("deadline_time"),
        }
    return events


def fetch_history(entry_id):
    """Return {gw: cumulative_total_points} for one manager.

    We use the CUMULATIVE total_points rather than the per-GW points field.
    total_points is unambiguously net of transfer hits, so a block total
    computed as total_points(end) - total_points(start-1) needs no assumption
    about whether the per-GW 'points' field already has the hit deducted.
    """
    url = f"https://fantasy.premierleague.com/api/entry/{entry_id}/history/"
    data = get_json(url)
    cum = {}
    for row in data.get("current", []):
        cum[row["event"]] = row["total_points"]
    return cum


def fetch_league():
    """Return {entry_id: (event_total, total)} from the classic league table.
    Used only as a cross-check against the history-derived numbers."""
    url = f"https://fantasy.premierleague.com/api/leagues-classic/{LEAGUE_ID}/standings/"
    try:
        data = get_json(url, attempts=2)
    except SystemExit:
        return {}
    out = {}
    for row in data.get("standings", {}).get("results", []):
        out[row["entry"]] = (row.get("event_total"), row.get("total"))
    return out


# ---------------------------------------------------------------- scoring

def settled(events, gw):
    ev = events.get(gw)
    return bool(ev and ev["finished"] and ev["data_checked"])


def cum_at(cum, gw):
    """Cumulative total_points at the end of gameweek `gw`.
    Falls back to the latest gameweek at or below `gw` that we have, and 0
    before the season starts."""
    if gw <= 0:
        return 0
    for g in range(gw, 0, -1):
        if g in cum:
            return cum[g]
    return 0


def block_points(cum, start, end):
    """Net points scored across an inclusive gameweek range."""
    return cum_at(cum, end) - cum_at(cum, start - 1)


def payouts(scores, stake, prizes):
    """Net P&L per manager. Ties split the combined gross prize of the slots
    they jointly occupy, evenly.

    e.g. two tied for 1st share slots 1+2 = $60+$10 = $70 -> $35 each
         -> net +$25 each after the $10 stake.
    """
    result = {}
    idx = 0
    for value in sorted(set(scores.values()), reverse=True):
        tied = sorted([n for n, v in scores.items() if v == value])
        gross = sum(prizes[idx:idx + len(tied)])
        share = gross / len(tied)
        for name in tied:
            result[name] = share - stake
        idx += len(tied)
    return result


def rank_table(scores):
    """[(position, [names], points)] with ties sharing a position."""
    rows = []
    pos = 1
    for value in sorted(set(scores.values()), reverse=True):
        tied = sorted([n for n, v in scores.items() if v == value])
        rows.append((pos, tied, value))
        pos += len(tied)
    return rows


def compute(events, cums):
    """Everything the template needs."""
    names = [n for _, n in MANAGERS]

    blocks = []
    pnl = {n: 0.0 for n in names}

    for i, (start, end) in enumerate(BLOCKS, start=1):
        gws = list(range(start, end + 1))
        done = [g for g in gws if settled(events, g)]
        any_started = any(
            events.get(g, {}).get("finished") or events.get(g, {}).get("is_current")
            for g in gws
        )
        scores = {n: block_points(cums[n], start, end) for n in names}
        is_settled = len(done) == len(gws)

        block = {
            "n": i,
            "start": start,
            "end": end,
            "gws_final": len(done),
            "gws_total": len(gws),
            "scores": scores,
            "table": rank_table(scores),
            "settled": is_settled,
            "status": "settled" if is_settled else ("live" if any_started else "not started"),
            "payouts": None,
        }
        if is_settled:
            block["payouts"] = payouts(scores, BLOCK_STAKE, BLOCK_PRIZES)
            for n, v in block["payouts"].items():
                pnl[n] += v
        blocks.append(block)

    # season wager - settles only when every gameweek is final
    season_scores = {n: cum_at(cums[n], TOTAL_GWS) for n in names}
    season_settled = all(settled(events, g) for g in range(1, TOTAL_GWS + 1))
    season = {
        "scores": season_scores,
        "table": rank_table(season_scores),
        "settled": season_settled,
        "status": "settled" if season_settled else (
            "live" if any(events.get(g, {}).get("finished") for g in range(1, TOTAL_GWS + 1))
            else "not started"),
        "payouts": None,
    }
    if season_settled:
        season["payouts"] = payouts(season_scores, SEASON_STAKE, SEASON_PRIZES)
        for n, v in season["payouts"].items():
            pnl[n] += v

    # the live block = first unsettled block that has started; else first unsettled
    live = next((b for b in blocks if b["status"] == "live"), None)
    if live is None:
        live = next((b for b in blocks if not b["settled"]), blocks[-1])

    current = next((g for g, e in events.items() if e["is_current"]), None)
    nxt = next((g for g, e in events.items() if e["is_next"]), None)
    deadline = None
    if nxt:
        deadline = events[nxt]["deadline"]
    elif current:
        deadline = events[current]["deadline"]

    return {
        "blocks": blocks,
        "season": season,
        "pnl": pnl,
        "live": live,
        "current_gw": current,
        "next_gw": nxt,
        "deadline": deadline,
        "settled_gws": sorted([g for g in events if settled(events, g)]),
        "season_started": any(
            events.get(g, {}).get("finished") or events.get(g, {}).get("is_current")
            for g in range(1, TOTAL_GWS + 1)
        ),
    }


# ---------------------------------------------------------------- render

def money(v):
    if abs(v) < 0.005:
        return "$0"
    s = f"{abs(v):,.2f}".rstrip("0").rstrip(".")
    return ("+$" if v > 0 else "−$") + s


def fmt_deadline(iso):
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.strftime("%a %d %b %Y, %H:%M UTC")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render(d, built_at):
    names = [n for _, n in MANAGERS]
    live = d["live"]

    # ---- live block rows, ordered by points desc
    live_rows = []
    for pos, tied, pts in live["table"]:
        for n in tied:
            live_rows.append(
                f'<tr><td class="pos">{pos}</td><td class="name">{esc(n)}</td>'
                f'<td class="num">{pts}</td></tr>'
            )
    live_body = "\n".join(live_rows)

    # ---- block ledger
    ledger = []
    for b in d["blocks"]:
        cls = {"settled": "s-settled", "live": "s-live", "not started": "s-idle"}[b["status"]]
        if b["settled"]:
            winners = b["table"][0][1]
            second = b["table"][1][1] if len(b["table"]) > 1 else []
            detail = (f'<strong>{esc(", ".join(winners))}</strong> '
                      f'({b["table"][0][2]} pts)')
            money_col = ", ".join(
                f"{esc(n)} {money(v)}" for n, v in sorted(
                    b["payouts"].items(), key=lambda kv: -kv[1])
            )
            second_col = esc(", ".join(second)) if second else "—"
        elif b["status"] == "live":
            leader = b["table"][0]
            detail = (f'{esc(", ".join(leader[1]))} leading, {leader[2]} pts '
                      f'<span class="muted">({b["gws_final"]}/{b["gws_total"]} final)</span>')
            money_col = '<span class="muted">books on settlement</span>'
            second_col = "—"
        else:
            detail = '<span class="muted">—</span>'
            money_col = '<span class="muted">—</span>'
            second_col = "—"
        ledger.append(
            f'<tr><td class="name">B{b["n"]}</td>'
            f'<td class="muted">GW {b["start"]}–{b["end"]}</td>'
            f'<td><span class="pill {cls}">{b["status"]}</span></td>'
            f'<td>{detail}</td><td>{second_col}</td><td class="mono">{money_col}</td></tr>'
        )
    ledger_body = "\n".join(ledger)

    # ---- season row
    s = d["season"]
    s_cls = {"settled": "s-settled", "live": "s-live", "not started": "s-idle"}[s["status"]]
    if s["settled"]:
        s_detail = (f'<strong>{esc(", ".join(s["table"][0][1]))}</strong> '
                    f'({s["table"][0][2]} pts)')
        s_money = ", ".join(f"{esc(n)} {money(v)}" for n, v in
                            sorted(s["payouts"].items(), key=lambda kv: -kv[1]))
    elif s["status"] == "live":
        lead = s["table"][0]
        s_detail = f'{esc(", ".join(lead[1]))} leading, {lead[2]} pts'
        s_money = '<span class="muted">books after GW38</span>'
    else:
        s_detail = '<span class="muted">—</span>'
        s_money = '<span class="muted">—</span>'

    # ---- P&L
    pnl_rows = []
    booked = sum(1 for b in d["blocks"] if b["settled"])
    for n, v in sorted(d["pnl"].items(), key=lambda kv: -kv[1]):
        cls = "up" if v > 0.005 else ("down" if v < -0.005 else "flat")
        pnl_rows.append(f'<tr><td class="name">{esc(n)}</td>'
                        f'<td class="num mono {cls}">{money(v)}</td></tr>')
    pnl_body = "\n".join(pnl_rows)

    stamp = built_at.strftime("%-d %b %Y") if sys.platform != "win32" else built_at.strftime("%d %b %Y")

    if not d["season_started"]:
        banner = (f'<div class="banner">Season has not started. '
                  f'GW1 deadline {fmt_deadline(d["deadline"])}.</div>')
    else:
        gw = d["current_gw"] or d["next_gw"]
        banner = (f'<div class="banner">GW{gw} · next deadline '
                  f'{fmt_deadline(d["deadline"])} · '
                  f'{len(d["settled_gws"])}/{TOTAL_GWS} gameweeks final</div>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FPL Wagers · 损友俱乐部 S8</title>
<style>
  :root {{
    --bg:#0e1117; --panel:#161b22; --line:#273039;
    --ink:#e6edf3; --dim:#8b949e; --accent:#58a6ff;
    --up:#3fb950; --down:#f85149;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.4px; }}
  h1 .sub {{ display:block; font-size:13px; font-weight:400; color:var(--dim);
    margin-top:6px; letter-spacing:0; }}
  h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.09em;
    color:var(--dim); margin:34px 0 10px; font-weight:600; }}
  .banner {{ background:var(--panel); border:1px solid var(--line);
    border-left:3px solid var(--accent); border-radius:6px;
    padding:11px 14px; font-size:13.5px; color:var(--dim); margin:18px 0 8px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel);
    border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
  th {{ text-align:left; font-size:11px; text-transform:uppercase;
    letter-spacing:.07em; color:var(--dim); font-weight:600;
    padding:10px 12px; border-bottom:1px solid var(--line); }}
  td {{ padding:10px 12px; border-bottom:1px solid var(--line); font-size:14px; }}
  tr:last-child td {{ border-bottom:none; }}
  .num, th.num {{ text-align:right; }}
  .pos {{ color:var(--dim); width:34px; }}
  .name {{ font-weight:600; }}
  .mono {{ font-variant-numeric:tabular-nums;
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }}
  .muted {{ color:var(--dim); }}
  .up {{ color:var(--up); }} .down {{ color:var(--down); }} .flat {{ color:var(--dim); }}
  .pill {{ display:inline-block; padding:2px 8px; border-radius:99px;
    font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.05em; }}
  .s-settled {{ background:rgba(63,185,80,.14); color:var(--up); }}
  .s-live {{ background:rgba(88,166,255,.14); color:var(--accent); }}
  .s-idle {{ background:rgba(139,148,158,.12); color:var(--dim); }}
  footer {{ margin-top:38px; padding-top:16px; border-top:1px solid var(--line);
    font-size:12px; color:var(--dim); }}
  .scroll {{ overflow-x:auto; }}
  @media (max-width:640px) {{ .wrap {{ padding:22px 14px 48px; }} h1 {{ font-size:21px; }} }}
</style>
</head>
<body>
<div class="wrap">

  <h1>FPL Wagers
    <span class="sub">损友俱乐部 · Season 8 · 2026/27 · updated {stamp}</span>
  </h1>

  {banner}

  <h2>Live block · B{live['n']} · GW {live['start']}–{live['end']}
      · {live['gws_final']}/{live['gws_total']} final</h2>
  <table>
    <thead><tr><th class="pos">#</th><th>Manager</th><th class="num">Block pts</th></tr></thead>
    <tbody>
{live_body}
    </tbody>
  </table>

  <h2>Block ledger</h2>
  <div class="scroll">
  <table>
    <thead><tr><th>Block</th><th>Gameweeks</th><th>Status</th>
      <th>Winner / leader</th><th>2nd</th><th>Net P&amp;L</th></tr></thead>
    <tbody>
{ledger_body}
      <tr><td class="name">Season</td><td class="muted">GW 1–38</td>
        <td><span class="pill {s_cls}">{s['status']}</span></td>
        <td>{s_detail}</td><td>—</td><td class="mono">{s_money}</td></tr>
    </tbody>
  </table>
  </div>

  <h2>Cumulative P&amp;L · {booked} of 10 blocks booked</h2>
  <table>
    <thead><tr><th>Manager</th><th class="num">Net</th></tr></thead>
    <tbody>
{pnl_body}
    </tbody>
  </table>

  <footer>
    Rebuilt daily at 10:00 UTC (6pm SGT) from the official FPL API by GitHub
    Actions. Blocks show live totals but book no P&amp;L until every gameweek in
    them is finished and data-checked. Blocks: $10 each, $70 pot — winner +$50,
    2nd $0, 3rd–7th −$10. Season: $30 each, $210 pot — winner +$120, 2nd +$30,
    3rd–7th −$30. Ties split the combined prize for the tied places evenly;
    FPL's own tiebreak is not used.
    <br>Built {built_at.strftime('%Y-%m-%d %H:%M')} UTC.
  </footer>

</div>
</body>
</html>
"""


# ---------------------------------------------------------------- main

def main():
    dry = "--dry-run" in sys.argv

    events = fetch_events()
    cums = {}
    for entry_id, name in MANAGERS:
        cums[name] = fetch_history(entry_id)
        time.sleep(0.3)

    d = compute(events, cums)

    # cross-check history-derived season totals against the league table
    league = fetch_league()
    for entry_id, name in MANAGERS:
        if entry_id in league:
            _, lg_total = league[entry_id]
            mine = cum_at(cums[name], max(cums[name]) if cums[name] else 0)
            if lg_total is not None and mine != lg_total:
                print(f"WARN: {name} total mismatch — history {mine}, "
                      f"league table {lg_total}", file=sys.stderr)

    html = render(d, datetime.now(timezone.utc))

    print(f"settled GWs: {d['settled_gws'] or 'none'}")
    print(f"current GW: {d['current_gw']}  next: {d['next_gw']}")
    print(f"live block: B{d['live']['n']} ({d['live']['status']})")
    for b in d["blocks"]:
        if b["settled"]:
            print(f"  B{b['n']} SETTLED  winner={b['table'][0][1]} "
                  f"payouts={ {k: round(v,2) for k,v in b['payouts'].items()} }")
    print(f"P&L: { {k: round(v,2) for k,v in d['pnl'].items()} }")

    if dry:
        print("\n--dry-run: index.html not written")
        return

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nwrote index.html ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
