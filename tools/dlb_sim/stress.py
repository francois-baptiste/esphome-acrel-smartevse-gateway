#!/usr/bin/env python3
"""Long-run backtest of the real etek_dlb lambda with fault injection and a Linky breaker model.

    python3 tools/dlb_sim/stress.py [--days 14] [--seeds 4] [--baseline <git rev>] [--profiles clean,faults,hostile,reboot15,lostwrites]
                                    [--load-scale 1.0] [--csv charge.csv [--csv-from T --csv-to T]] [--millis-start N]

Compares, on the same house load / car demand / fault schedule: a control without DLB (ETEK fixed at 32 A: proves the
breaker model is not trivially permissive), the baseline revision (default: the commit before the 45 s floor pause)
and the working tree. Synthetic data come from model.py (appliances, nightly charge sessions with end-of-charge taper,
car pauses); --csv replays the real house load (Acrel L1 + L3) exported by tools/flux.sh, see replay.py.
Counts: reg 109 writes, pauses (reg 89 = 2 -> status 19), time over the limit, breaker trips, fallbacks, reboot cost.
Exit code 1 if an invariant of the working tree fails. Python 3.8 compatible.
"""
import argparse
import calendar
import re
import subprocess
import sys
import tempfile
from array import array
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402
import model  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=14)
ap.add_argument("--seeds", type=int, default=4)
ap.add_argument("--baseline", default="c4f864a^", help="git revision compared with the working tree")
ap.add_argument("--profiles", default="clean,faults,hostile,reboot15,lostwrites")
ap.add_argument("--load-scale", type=float, default=1.0, help="appliance rate multiplier (2-3 = very busy house)")
ap.add_argument("--house-max", type=float, default=29.0, help="clip of the synthetic house load, A")
ap.add_argument("--millis-start", type=int, default=0, help="initial millis() (e.g. 4294940000 to cross the 49-day wrap)")
ap.add_argument("--variant", help="extra row: working tree with constants overridden, e.g. FALLBACK_A=6,PAUSE_FLOOR_MS=60000")
ap.add_argument("--csv", help="exported Influx CSV (see replay.py): replay the real house load instead of synthetic data")
ap.add_argument("--csv-from")
ap.add_argument("--csv-to")
args = ap.parse_args()

MARKER = "ETEK_MIN_A = 6;"
W = tempfile.mkdtemp(prefix="dlb_stress_")


def build(rev):
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8") if rev == "WORKTREE" else subprocess.run(
        ["git", "show", "%s:config.yaml" % rev], capture_output=True, text=True, cwd=ROOT).stdout
    return harness.build(cfg, MARKER, "s_" + re.sub(r"\W", "_", rev), W)


def parse_t(s):
    return calendar.timegm(datetime.strptime(s.split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S").timetuple())


def load_csv(path, t_from, t_to):
    series = {"l1": {}, "l2": {}, "l3": {}, "cap": {}, "st": {}}
    for line in open(path, encoding="utf-8"):
        f = line.rstrip("\r\n").split(",")
        if len(f) < 6 or not f[3].startswith("20"):
            continue
        tp = f[5]
        key = ("l1" if "courant_l1" in tp else "l2" if "courant_l2" in tp else "l3" if "courant_l3" in tp
               else "cap" if "consigne_appliquee" in tp else "st" if "etek_epc2_statut" in tp else None)
        if key:
            series[key][int(parse_t(f[3]))] = float(f[4])
    t0, t1 = min(series["l2"]), max(series["l2"])
    if t_from:
        t0 = max(t0, parse_t(t_from))
    if t_to:
        t1 = min(t1, parse_t(t_to))

    def ffill(d):
        out, cur, j, ks = {}, None, 0, sorted(d)
        for t in range(t0 - 300, t1 + 1):
            while j < len(ks) and ks[j] <= t:
                cur = d[ks[j]]
                j += 1
            out[t] = cur
        return out
    L1, L2, L3, CAP, ST = (ffill(series[k]) for k in ("l1", "l2", "l3", "cap", "st"))
    act = [t for t in range(t0, t1 + 1) if (ST[t] == 5 if ST[t] is not None else (L2[t] or 0) > 5)]
    if not act:
        sys.exit("no charging period in the selected window")
    ta, tb = act[0], act[-1]

    def raw(t):
        if CAP[t] is None or CAP[t] < 0:
            return 32.0
        return 32.0 if (L2[t] or 0) >= CAP[t] - 2.0 else (L2[t] or 0)
    n = t1 - ta + 1
    house, dem, ph = array("f", [0.0]) * n, array("f", [0.0]) * n, bytearray(n)
    for i in range(n):
        t = ta + i
        house[i] = (L1[t] or 0) + (L3[t] or 0)
        if t <= tb:
            dem[i] = max(raw(min(max(u, t0 - 300), t1)) for u in range(t - 6, t + 7, 2))
            ph[i] = 1 if dem[i] >= 31.9 else 2
    return house, dem, ph


def run_all(libs, house, dem, ph, faults, seeds_data):
    out = {}
    for name, lib in libs:
        agg = None
        for seed, (h, d, p) in seeds_data:
            r = model.simulate(lib, h, d, p, faults, seed=seed, days=None if args.csv else args.days,
                               millis_start=args.millis_start)
            if agg is None:
                agg = {k: v for k, v in r.items() if k not in ("brk", "episodes")}
                agg["brk"] = {k: [v[0], v[1]] for k, v in r["brk"].items()}
            else:
                for k, v in r.items():
                    if k == "episodes":
                        continue
                    if k == "brk":
                        for b, (tr, pk) in v.items():
                            agg["brk"][b][0] += tr
                            agg["brk"][b][1] = max(agg["brk"][b][1], pk)
                    elif k in ("peak", "ep_max_s"):
                        agg[k] = max(agg[k], v)
                    else:
                        agg[k] += v
        out[name] = agg
    return out


def show(title, res):
    print("\n=== %s ===" % title)
    hdr = ("policy", "w109/h", "pauses/d", "act.p/d", "paused%", "%>lim", "%>+1.5", "ep>3A", "epmax s", "peakA", "trip nom", "trip pes",
           "eqA nom", "eqA pes", "ev/ideal", "fallb/d", "taper-w/d", "reb-w", "reboots", "lost-w", "viol")
    print(("%-14s" + "%-9s" * (len(hdr) - 1)) % hdr)
    for name, r in res.items():
        days = r["n"] / 86400.0
        hours = r["n"] / 3600.0
        row = (name, "%.1f" % (r["w109"] / hours), "%.2f" % (r["pauses"] / days), "%.2f" % (r["pauses_active"] / days), "%.2f" % (100.0 * r["paused_s"] / r["n"]),
               "%.2f" % (100.0 * r["over0"] / r["n"]), "%.3f" % (100.0 * r["over15"] / r["n"]),
               "%d" % r["ep_n"], "%d" % r["ep_max_s"], "%.1f" % r["peak"], "%d" % r["brk"]["nominal"][0],
               "%d" % r["brk"]["pessimistic"][0], "%.1f" % r["brk"]["nominal"][1], "%.1f" % r["brk"]["pessimistic"][1],
               "%.3f" % (r["ev"] / r["ideal"] if r["ideal"] else 0), "%.2f" % (r["fallback"] / days),
               "%.2f" % (r["quiet_w"] / days), "%d" % r["reboot_w"], "%d" % r["reboots"], "%d" % r["lost"], "%d" % r["viol"])
        print(("%-14s" + "%-9s" * (len(row) - 1)) % row)


fails = []
warns = []
# Profiles where long over-limit episodes / breaker trips are documented limitations, reported as WARN: a long Acrel or
# Linky outage (or a reboot waiting for the first Linky frame) puts the car on the 32 A fallback, an explicit owner choice
# (blind = full power, the Linky trips rather than a degraded mode), and a lost reg 109 write is never retried (the
# register is not read back).
KNOWN_LIMITS = ("faults", "hostile", "reboot15", "lostwrites")


def check(name, cond, detail="", soft=False):
    print(("PASS " if cond else ("WARN " if soft else "FAIL ")) + name + (("  -> " + detail) if detail else ""))
    if not cond:
        (warns if soft else fails).append(name)


libs = [("no DLB (32 A)", None), ("baseline", build(args.baseline)), ("working tree", build("WORKTREE"))]
if args.variant:
    vcfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    for kv in args.variant.split(","):
        k, v = kv.split("=")
        vcfg, cnt = re.subn(r"(\b%s\s*=\s*)[0-9.]+" % re.escape(k), lambda m: m.group(1) + v, vcfg)
        if cnt != 1:
            sys.exit("variant constant %s matched %d times" % (k, cnt))
    libs.append(("variant", harness.build(vcfg, MARKER, "s_variant", W)))
if args.csv:
    h, d, p = load_csv(args.csv, args.csv_from, args.csv_to)
    seeds_data = [(1, (h, d, p))]
    print("csv replay: %.1f h of real house load" % (len(h) / 3600.0))
else:
    seeds_data = []
    for s in range(1, args.seeds + 1):
        seeds_data.append((s, (model.make_house(s, args.days, args.load_scale, args.house_max), ) + model.make_demand(s, args.days)))
    print("synthetic: %d seeds x %d days, load scale %.1f, baseline %s" % (args.seeds, args.days, args.load_scale, args.baseline))

allres = {}
for pname in args.profiles.split(","):
    res = run_all(libs, None, None, None, model.PROFILES[pname], seeds_data)
    allres[pname] = res
    show("profile %s" % pname, res)

print()
print("== invariants (working tree) ==")
first = next(iter(allres.values()))
check("model sanity: without DLB the nominal breaker trips", first["no DLB (32 A)"]["brk"]["nominal"][0] > 0
      or args.csv is not None)
for pname, res in allres.items():
    w, b = res["working tree"], res["baseline"]
    soft = pname in KNOWN_LIMITS
    check("%s: never pauses an idle car (L2 < 0.5 A)" % pname, w["pauses_idle"] == 0, str(w["pauses_idle"]))
    check("%s: no frozen-state write (taper freeze honoured)" % pname, w["viol"] == 0, str(w["viol"]))
    check("%s: nominal breaker never trips" % pname, w["brk"]["nominal"][0] == 0, str(w["brk"]["nominal"]), soft)
    check("%s: not more pauses than the baseline" % pname, w["pauses"] <= b["pauses"], "%d vs %d" % (w["pauses"], b["pauses"]))
    check("%s: time > limit+3 A below 0.5 %%" % pname, 100.0 * w["over3"] / w["n"] < 0.5, "%.3f %%" % (100.0 * w["over3"] / w["n"]), soft)
    check("%s: no episode > limit+3 A longer than 60 s" % pname, w["ep_max_s"] <= 60, "%d s" % w["ep_max_s"], soft)
print()
print("%d known-limit warning(s)" % len(warns) if warns else "no warning")
print("FAILED: %d" % len(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
