#!/usr/bin/env python3
"""Replay a recorded charge through the real DLB lambda and compare two revisions.

    tools/flux.sh export.flux > charge.csv        # Acrel L1/L2/L3 currents, dlb_consigne_appliquee, etek_epc2_statut
    python3 tools/dlb_sim/replay.py charge.csv --baseline HEAD [--from 2026-10-21T20:00:00 --to ...] [--taper]

The recorded house load (L1 + L3) is replayed as is; the EV is simulated: it draws min(demand, effective cap),
the cap taking effect 2.7 s (down) / 4.2 s (up) after the register write, like the measured ETEK latency.
Demand is 32 A, or with --taper derived from the recorded L2 (L2 well below the recorded cap = the car asks less).
Working-tree config.yaml (event-driven: Acrel batch every 2 s + supervisor every 5 s) is compared with the baseline
revision (5 s timer fed by Acrel samples every 2 s). Both with the shipped constants.
Timestamps are UTC; Python 3.8 compatible.
"""
import argparse
import calendar
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("csv")
ap.add_argument("--baseline", default="HEAD")
ap.add_argument("--from", dest="t_from")
ap.add_argument("--to", dest="t_to")
ap.add_argument("--taper", action="store_true", help="derive EV demand from the recorded L2/cap (end-of-charge taper)")
ap.add_argument("--pref", type=float, default=6.0)
args = ap.parse_args()

MARKER = "ETEK_MIN_A = 6;"
W = tempfile.mkdtemp(prefix="dlb_replay_")


def parse_t(s):
    return calendar.timegm(datetime.strptime(s.split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S").timetuple())


series = {"l1": {}, "l2": {}, "l3": {}, "cap": {}, "st": {}}
for line in open(args.csv, encoding="utf-8"):
    f = line.rstrip("\r\n").split(",")
    if len(f) < 6 or not f[3].startswith("20"):
        continue
    topic = f[5]
    key = ("l1" if "courant_l1" in topic else "l2" if "courant_l2" in topic else "l3" if "courant_l3" in topic
           else "cap" if "consigne_appliquee" in topic else "st" if "etek_epc2_statut" in topic else None)
    if key:
        series[key][int(parse_t(f[3]))] = float(f[4])
t0 = min(series["l2"])
t1 = max(series["l2"])
if args.t_from:
    t0 = max(t0, int(parse_t(args.t_from)))
if args.t_to:
    t1 = min(t1, int(parse_t(args.t_to)))


def ffill(d):
    out, cur = {}, None
    ks = sorted(d)
    j = 0
    for t in range(t0 - 300, t1 + 1):
        while j < len(ks) and ks[j] <= t:
            cur = d[ks[j]]
            j += 1
        out[t] = cur
    return out


L1, L2, L3, CAP, ST = (ffill(series[k]) for k in ("l1", "l2", "l3", "cap", "st"))
# charge window = first to last second with status 5 (or L2 > 5 A when the status is missing)
active = [t for t in range(t0, t1 + 1) if (ST[t] == 5 if ST[t] is not None else (L2[t] or 0) > 5)]
if not active:
    sys.exit("no charging period in the selected window")
ta, tb = active[0], active[-1]
print("replay window %s -> %s UTC (%.2f h)" % (datetime.utcfromtimestamp(ta), datetime.utcfromtimestamp(tb), (tb - ta) / 3600))


def raw_demand(t):
    t = min(max(t, t0 - 300), t1)
    if not args.taper or CAP[t] is None or CAP[t] < 0:
        return 32.0
    return 32.0 if (L2[t] or 0) >= CAP[t] - 2.0 else (L2[t] or 0)


DEMAND = {t: max(raw_demand(u) for u in range(t - 6, t + 7, 2)) if args.taper else 32.0 for t in range(ta, tb + 1)}


def build(rev):
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8") if rev == "WORKTREE" else subprocess.run(
        ["git", "show", "%s:config.yaml" % rev], capture_output=True, text=True, cwd=ROOT).stdout
    return harness.build(cfg, MARKER, "r_" + re.sub(r"\W", "_", rev), W)


def replay(lib, event_driven):
    lib.h_reset()
    lib.h_set_time(1000)
    lib.h_feed_linky(args.pref)
    cap_eff, pend = 32.0, []   # pend: (effective_time, value)
    nw = 0
    stats = dict(n=0, over0=0, over06=0, over15=0, peak=0.0, ev=0.0, pauses=0, w109=0, over_s=0.0)
    limit = args.pref * 1000 / 200 * 1.10
    prev_pause = 0
    for t in range(ta, tb + 1):
        ms = (t - ta) * 1000 + 30000
        lib.h_set_time(ms)
        if (t - ta) % 100 == 0:
            lib.h_feed_linky(args.pref)
        # apply cap changes that reached the charger
        for e in [p for p in pend if p[0] <= t]:
            cap_eff = e[1]
        pend = [p for p in pend if p[0] > t]
        paused = lib.h_get(1) == 2
        ev = 0.0 if paused else min(DEMAND[t], cap_eff)
        house = (L1[t] or 0) + (L3[t] or 0)
        total = house + ev
        stats["n"] += 1
        stats["ev"] += ev
        stats["peak"] = max(stats["peak"], total - limit)
        stats["over0"] += total > limit
        stats["over06"] += total > limit + 0.6
        stats["over15"] += total > limit + 1.5
        if paused and not prev_pause:
            stats["pauses"] += 1
        prev_pause = paused
        # controller side
        if event_driven:
            if (t - ta) % 2 == 0:                 # Acrel batch (2 s)
                lib.h_feed_acrel(float(L1[t] or 0), ev, float(L3[t] or 0))
                lib.h_cycle()
            if (t - ta) % 5 == 0:
                lib.h_watchdog()
        else:
            if (t - ta) % 2 == 0:                 # Acrel sample (2 s)
                lib.h_feed_acrel(float(L1[t] or 0), ev, float(L3[t] or 0))
            if (t - ta) % 5 == 0:                 # DLB timer (5 s)
                lib.h_cycle()
        # collect new writes
        while nw < lib.h_nwrites():
            reg, val = lib.h_write_reg(nw), lib.h_write_val(nw)
            nw += 1
            if reg == 109:
                stats["w109"] += 1
                new_cap = val / 167.0
                pend.append((t + (2.7 if new_cap < cap_eff else 4.2), new_cap))
    hours = stats["n"] / 3600.0
    return dict(
        writes_per_h=stats["w109"] / hours,
        mean_ev=stats["ev"] / stats["n"],
        pct_over=100.0 * stats["over0"] / stats["n"],
        pct_over06=100.0 * stats["over06"] / stats["n"],
        pct_over15=100.0 * stats["over15"] / stats["n"],
        peak=stats["peak"],
        pauses=stats["pauses"],
    )


res = {}
res["baseline " + args.baseline] = replay(build(args.baseline), False)
res["working tree"] = replay(build("WORKTREE"), True)
cols = [("writes_per_h", "reg109 writes/h", "%.1f"), ("mean_ev", "mean EV A", "%.2f"),
        ("pct_over", "% time > limit", "%.1f"), ("pct_over06", "% > limit+0.6", "%.2f"),
        ("pct_over15", "% > limit+1.5", "%.2f"), ("peak", "peak over limit A", "%.1f"), ("pauses", "pauses", "%d")]
print("%-22s" % "" + "".join("%-20s" % c[1] for c in cols))
for name, r in res.items():
    print("%-22s" % name + "".join("%-20s" % (c[2] % r[c[0]]) for c in cols))
