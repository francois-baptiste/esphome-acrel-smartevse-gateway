"""Backtest of the SoC estimator (tools/soc_estime.flux): efficiency per charge and prediction error per SoC pair.

    tools/flux.sh tools/soc_backtest_bmw.flux > DIR/bmw_all.csv
    tools/flux.sh tools/soc_backtest_kwh.flux > DIR/kwh_all.csv
    python3 tools/soc_backtest.py DIR

Python 3.8 compatible. Result 2026-10-24 (6-24 Oct): pooled efficiency 0.864 on charges without trip or 100 % saturation.
"""
import bisect, sys
from datetime import datetime
import calendar

S = sys.argv[1]


def pt(s):
    return calendar.timegm(datetime.strptime(s.split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S").timetuple())


def load(path, filt=None):
    out = []
    for l in open(path):
        f = l.strip().split(",")
        if len(f) < 5 or not f[3].startswith("20"):
            continue
        if filt and filt not in l:
            continue
        out.append((pt(f[3]), float(f[4])))
    return sorted(out)


soc = load(S + "/bmw_all.csv", "stateOfCharge.displayed")
dist = load(S + "/bmw_all.csv", "travelledDistance")
cap = load(S + "/bmw_all.csv", "maxEnergy")
kwh = load(S + "/kwh_all.csv")
kt = [t for t, _ in kwh]


def at(series, ts, t):
    keys = [x[0] for x in series]
    i = bisect.bisect_right(keys, t) - 1
    return series[i][1] if i >= 0 else None


def e_at(t):
    i = bisect.bisect_right(kt, t) - 1
    return kwh[i][1] if i >= 0 else None


# dedupe consecutive identical SoC (BMW sends each value twice)
ev = []
for t, v in soc:
    if ev and ev[-1][1] == v and t - ev[-1][0] < 120:
        continue
    ev.append((t, v))
print("SoC events (dedup):", len(ev), " distance events:", len(dist), " kWh pts:", len(kwh))
print("counter range %s .. %s" % (kwh[0][1], kwh[-1][1]))
print("kWh data gaps > 30 min:", sum(1 for a, b in zip(kt, kt[1:]) if b - a > 1800))

print("\n%-17s %6s %7s %7s %7s %8s %8s %6s" % ("pair end (UTC)", "SoC", "dSoC", "dkm", "dkWh", "dt h", "eff impl", "pred"))
rows = []
for (t0, s0), (t1, s1) in zip(ev, ev[1:]):
    e0, e1 = e_at(t0), e_at(t1)
    if e0 is None or t1 - t0 > 3 * 86400:
        continue
    # gap in kWh data within the pair?
    i0, i1 = bisect.bisect_right(kt, t0), bisect.bisect_right(kt, t1)
    has_data = i1 - i0 > 0 and (t1 - t0) / 60.0 < (i1 - i0) * 3 or (e1 - e0) < 0.05
    d0, d1 = at(dist, None, t0), at(dist, None, t1)
    dkm = (d1 - d0) if d0 is not None and d1 is not None else float("nan")
    c = at(cap, None, t1) or 38.0
    dk = e1 - e0
    pred = s0 + dk * 0.89 / c * 100.0
    eff = ((s1 - s0) / 100.0 * c / dk) if dk > 0.5 else float("nan")
    rows.append((t1, s1, s1 - s0, dkm, dk, (t1 - t0) / 3600.0, eff, pred, has_data))
    print("%-17s %6.1f %+7.1f %7.1f %7.2f %8.2f %8.2f %6.1f%s" % (
        datetime.utcfromtimestamp(t1).strftime("%m-%d %H:%M"), s1, s1 - s0, dkm, dk, (t1 - t0) / 3600.0, eff, pred, "" if has_data else " (kWh gap)"))

print("\n== efficiency on pure-charge pairs (dkm == 0, dkWh > 2, no kWh gap) ==")
pure = [r for r in rows if r[3] == 0 and r[4] > 2 and r[8]]
for r in pure:
    print("  %s dSoC %+.1f dkWh %.2f eff %.2f" % (datetime.utcfromtimestamp(r[0]).strftime("%m-%d %H:%M"), r[2], r[4], r[6]))
if pure:
    tot_s = sum(r[2] for r in pure)
    tot_k = sum(r[4] for r in pure)
    c = 38.1
    print("  pooled: %.1f %% SoC for %.1f kWh -> eff %.3f (cap %.1f)" % (tot_s, tot_k, tot_s / 100 * c / tot_k, c))
    es = sorted(r[6] for r in pure)
    print("  median %.3f min %.3f max %.3f n=%d" % (es[len(es) // 2], es[0], es[-1], len(es)))
    err = [r[7] - r[1] for r in pure]
    print("  estimator error (eff 0.89): mean %+.2f max abs %.2f points" % (sum(err) / len(err), max(abs(x) for x in err)))

print("\n== pairs where the car drove (dkm > 0.5) ==")
dr = [r for r in rows if r[3] > 0.5]
for r in dr:
    cons = (-(r[2]) / 100 * 38.1 + r[4] * 0.89) / r[3] * 100 if r[3] else float("nan")
    print("  %s dSoC %+.1f dkm %.1f dkWh %.2f  pred err %+.1f  implied kWh/100km %.1f" % (
        datetime.utcfromtimestamp(r[0]).strftime("%m-%d %H:%M"), r[2], r[3], r[4], r[7] - r[1], cons))
