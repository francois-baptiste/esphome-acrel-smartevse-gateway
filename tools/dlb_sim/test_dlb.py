#!/usr/bin/env python3
"""Scenario tests for the etek_dlb lambda in config.yaml (compiled with g++ against stub sensors).

    python3 tools/dlb_sim/test_dlb.py                    # test the working-tree config.yaml
    python3 tools/dlb_sim/test_dlb.py --baseline HEAD~1  # also compare write counts with an older revision

Scenarios run with SAFETY_MARGIN_A forced to 0 for round numbers (S1-S8); S9 checks the shipped margin.
A tick is 5 s, like the interval; the simulated EV draws min(applied cap, demand) unless forced.
"""
import argparse
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--baseline", help="git revision whose config.yaml is compared in S1 (write counts)")
args = ap.parse_args()

W = tempfile.mkdtemp(prefix="dlb_sim_")
MARKER = "ETEK_MIN_A = 6;"
cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
shipped_margin = float(re.search(r"SAFETY_MARGIN_A = ([0-9.]+)f", cfg).group(1))
new = harness.build(re.sub(r"SAFETY_MARGIN_A = [0-9.]+f", "SAFETY_MARGIN_A = 0.0f", cfg), MARKER, "dlb_new", W)
shipped = harness.build(cfg, MARKER, "dlb_shipped", W)
old = None
if args.baseline:
    old_cfg = subprocess.run(["git", "show", f"{args.baseline}:config.yaml"], capture_output=True, text=True, cwd=ROOT).stdout
    old = harness.build(old_cfg, MARKER, "dlb_old", W)

TICK = 5000
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def writes(lib, reg=None):
    return [(lib.h_write_t(i) // 1000, lib.h_write_reg(i), lib.h_write_val(i))
            for i in range(lib.h_nwrites()) if reg is None or lib.h_write_reg(i) == reg]


def amps(w):
    return [round(v / 167) if r == 109 else f"reg{r}={int(v)}" for _, r, v in w]


class Sim:
    def __init__(self, lib, pref=6.0, ev_demand=40.0):
        self.lib, self.pref, self.demand = lib, pref, ev_demand
        lib.h_reset()
        self.t = 0
        lib.h_set_time(0)
        lib.h_feed_linky(pref)

    def tick(self, l1, l3, l2=None, feed=True):
        lib = self.lib
        self.t += TICK
        lib.h_set_time(self.t)
        if self.t % 100000 < TICK:
            lib.h_feed_linky(self.pref)   # teleinfo refreshes about every 100 s
        cap = lib.h_get(0)
        if l2 is None:
            l2 = 0.0 if lib.h_get(1) == 2 else min(cap if cap >= 0 else 32, self.demand)
        if feed:
            lib.h_feed_acrel(l1, l2, l3)
        lib.h_cycle()


def run_steady(lib, seed=1, minutes=60, base=3.0, sigma=0.35):
    random.seed(seed)
    s = Sim(lib)
    for _ in range(minutes * 12):
        n = random.gauss(0, sigma)
        s.tick(base * 0.7 + n * 0.6, base * 0.3 + n * 0.4)
    return writes(lib, 109)


print("== S1 steady house load with noise (1 h) ==")
wn = run_steady(new)
print("   new writes:", len(wn), amps(wn)[:12])
check("S1 at most 3 register-109 writes in an hour", len(wn) <= 3, str(len(wn)))
if old:
    wo = run_steady(old)
    print("   baseline writes:", len(wo))
    check("S1 far fewer writes than the baseline", len(wn) <= max(2, len(wo) // 3), f"baseline={len(wo)} new={len(wn)}")

print("== S2 first write and strong step-down ==")
s = Sim(new)
s.tick(2.0, 1.0)
check("first write is floor(33-3)=30 A", amps(writes(new, 109)) == [30], str(amps(writes(new, 109))))
check("reg89 init write 0", any(r == 89 and v == 0 for _, r, v in writes(new)))
s.tick(8.0, 3.5)
w = writes(new, 109)
check("house step +8.5 A -> immediate write 21 A", amps(w)[-1] == 21 and len(w) == 2, str(amps(w)))

print("== S3 moderate drop and deadband ==")
s = Sim(new)
s.tick(2.0, 1.0)
n0 = new.h_nwrites()
s.tick(2.4, 1.0)
s.tick(2.5, 1.1)
check("drops <= 0.8 A ignored", new.h_nwrites() == n0)
s.tick(3.0, 1.5)
check("moderate drop needs confirmation (no write after 1 cycle)", new.h_nwrites() == n0)
s.tick(3.0, 1.5)
w = writes(new, 109)
check("moderate drop written after 2 cycles to 28", len(w) == 2 and amps(w)[-1] == 28, str(amps(w)))

print("== S4 rise needs 2 cycles and uses the prudent value ==")
s = Sim(new)
s.tick(8.0, 3.0)
s.tick(2.0, 1.0)
check("rise not written after 1 cycle", len(writes(new, 109)) == 1)
s.tick(2.5, 1.0)
w = writes(new, 109)
check("rise written after 2 cycles at the prudent value 29", len(w) == 2 and amps(w)[-1] == 29, str(amps(w)))

print("== S5 short dips below 6 A are clamped, pause only after 3 cycles ==")
s = Sim(new)
s.tick(2.0, 1.0)
s.tick(20.0, 10.0)
s.tick(20.0, 10.0)
check("clamped to 6 A, no pause after 2 cycles", amps(writes(new, 109))[-1] == 6 and new.h_get(1) == 0)
s.tick(2.0, 1.0)
check("pause counter reset when headroom recovers", new.h_get(4) == 0)
for _ in range(3):
    s.tick(20.0, 10.0)
check("pause after 3 consecutive cycles", new.h_get(1) == 2 and amps(writes(new, 89))[-1] == "reg89=2")
n = new.h_nwrites()
s.tick(2.0, 1.0)
check("no resume after 1 cycle", new.h_get(1) == 2 and new.h_nwrites() == n)
s.tick(2.0, 1.0)
check("resume after 2 cycles: reg89=0 then reg109", amps(writes(new))[-2:] == ["reg89=0", 30], str(amps(writes(new))[-3:]))
s2 = Sim(new)
s2.tick(2.0, 1.0)
for _ in range(3):
    s2.tick(20.0, 10.0)
n = new.h_nwrites()
s2.tick(25.0, 6.0)
s2.tick(26.0, 4.0)
s2.tick(23.0, 4.0)   # headroom 6 (< 8)
check("paused stays paused while headroom < 8", new.h_get(1) == 2 and new.h_nwrites() == n)

print("== S6 taper: frozen unless the house load needs a lower cap ==")
s = Sim(new)
s.tick(2.0, 1.0)
n = new.h_nwrites()
for l2 in [26, 22, 18, 14, 10, 7, 5]:
    s.tick(2.0, 1.0, l2=l2)
check("no writes while tapering with ample headroom", new.h_nwrites() == n and new.h_get(6) == 1)
s.tick(10.0, 5.0, l2=7)
check("moderate house increase while tapering stays frozen", new.h_nwrites() == n)
s.tick(18.0, 6.0, l2=7)
check("house load needing a lower cap lifts the freeze (write 9 A)", amps(writes(new, 109))[-1] == 9, str(amps(writes(new, 109))))

print("== S7 stale data fallback ==")
s = Sim(new)
s.tick(2.0, 1.0)
for _ in range(3):
    s.tick(2.0, 1.0, feed=False)   # 15 s of silence: still not stale (limit is "> 15 s")
check("15 s of silence is not stale", new.h_get(7) == 0)
s.tick(2.0, 1.0, feed=False)
check("stale > 15 s -> fallback 10 A", new.h_get(7) == 1 and amps(writes(new, 109))[-1] == 10)
n = new.h_nwrites()
s.tick(2.0, 1.0, feed=False)
s.tick(2.0, 1.0, feed=False)
check("fallback written only once", new.h_nwrites() == n)
s.tick(2.0, 1.0)
check("recovery: not raised immediately", new.h_nwrites() == n and new.h_get(7) == 0)
s.tick(2.0, 1.0)
check("recovery: raised after 2 cycles to 30", amps(writes(new, 109))[-1] == 30)
s = Sim(new)
s.tick(2.0, 1.0)
new.h_feed_linky(6.0)
for _ in range(30):   # 150 s without a Linky refresh
    s.t += TICK
    new.h_set_time(s.t)
    new.h_feed_acrel(2.0, min(new.h_get(0), 40), 1.0)
    new.h_cycle()
check("Linky silent 150 s is not stale", new.h_get(7) == 0)
for _ in range(40):   # past 300 s
    s.t += TICK
    new.h_set_time(s.t)
    new.h_feed_acrel(2.0, 10.0, 1.0)
    new.h_cycle()
check("Linky silent > 300 s -> stale fallback", new.h_get(7) == 1 and amps(writes(new, 109))[-1] == 10)
s = Sim(new)
for _ in range(3):
    s.t += TICK
    new.h_set_time(s.t)
    new.h_cycle()
check("no fallback write inside the 20 s boot grace", new.h_nwrites() == 0)
s.t += TICK * 2
new.h_set_time(s.t)
new.h_cycle()
check("fallback after the boot grace if still blind", amps(writes(new, 109)) == [10])

print("== S8 other subscriptions / invalid PREF ==")
s = Sim(new, pref=9.0)
s.tick(2.0, 1.0)
check("9 kVA -> cap 32 A (ETEK maximum)", amps(writes(new, 109)) == [32])
s = Sim(new, pref=0.0)
s.tick(2.0, 1.0)
check("PREF=0 treated as invalid", new.h_get(7) == 1 or new.h_nwrites() == 0)

print("== S9 shipped constants ==")
check("shipped SAFETY_MARGIN_A is 0.3", abs(shipped_margin - 0.3) < 1e-9, str(shipped_margin))
s = Sim(shipped)
s.tick(2.0, 1.0)
check("shipped: first write floor(29.7)=29 A", amps(writes(shipped, 109)) == [29])
random.seed(2)
for _ in range(60):
    s.tick(2.0 + random.uniform(-0.3, 0.3), 1.0 + random.uniform(-0.1, 0.1))
check("shipped: +-0.4 A noise causes no rewrite in 5 min", len(writes(shipped, 109)) == 1)

print()
print("FAILED: %d" % len(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
