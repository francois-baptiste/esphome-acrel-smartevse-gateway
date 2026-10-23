"""Scenario tests for the etek_dlb script lambda in config.yaml (compiled with g++ against stub sensors).

    python3 tools/dlb_sim/test_dlb.py                    # test the working-tree config.yaml
    python3 tools/dlb_sim/test_dlb.py --baseline HEAD~1  # also compare write counts with an older revision

Scenarios run with SAFETY_MARGIN_A forced to 0 for round numbers; S9 checks the shipped margin.
The clock advances in 100 ms steps. An Acrel batch (the L3 on_value trigger) arrives every 2 s, the supervisor
interval fires every 5 s (a no-op on fresh data); the simulated EV draws min(applied cap, demand) unless forced.
The baseline revision (5 s timer, cycle counters) is driven by Sim(tick_ms=5000) with the same harness.
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

TICK = 2000           # Acrel batch period
WATCHDOG = 5000       # supervisor interval
STEP = 100
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
    """Event-driven clock: batch every `tick_ms` (+ optional jitter), supervisor every 5 s."""

    def __init__(self, lib, pref=6.0, ev_demand=40.0, tick_ms=TICK, jitter_ms=0, watchdog=True):
        self.lib, self.pref, self.demand = lib, pref, ev_demand
        self.tick_ms, self.jitter, self.watchdog = tick_ms, jitter_ms, watchdog
        self.linky = True
        lib.h_reset()
        self.t = 0
        lib.h_set_time(0)
        lib.h_feed_linky(pref)

    def _advance(self, ms, on_end=None):
        end = self.t + ms
        while self.t < end:
            self.t += STEP
            self.lib.h_set_time(self.t)
            if self.linky and self.t % 100000 < STEP:
                self.lib.h_feed_linky(self.pref)   # teleinfo refreshes about every 100 s
            if self.watchdog and self.t % WATCHDOG == 0:
                self.lib.h_watchdog()
        if on_end:
            on_end()

    def tick(self, l1, l3, l2=None, feed=True):
        """One batch period. feed=False: the Acrel stays silent (only the supervisor runs)."""
        lib = self.lib
        ms = self.tick_ms + (random.randint(-self.jitter, self.jitter) // STEP * STEP if self.jitter else 0)

        def batch():
            cap = lib.h_get(0)
            ll2 = l2
            if ll2 is None:
                ll2 = 0.0 if lib.h_get(1) == 2 else min(cap if cap >= 0 else 32, self.demand)
            lib.h_feed_acrel(l1, ll2, l3)
            lib.h_cycle()
        self._advance(ms, batch if feed else None)

    def silence(self, seconds):
        self._advance(int(seconds * 1000))

    def skewed(self, l1, l3, d1_ms=800, d2_ms=0):
        """A batch whose L1 timestamp is d1_ms older than L3 (split Modbus block)."""
        def batch():
            cap = self.lib.h_get(0)
            self.lib.h_feed_acrel_skewed(l1, min(cap if cap >= 0 else 32, self.demand), l3, d1_ms, d2_ms)
            self.lib.h_cycle()
        self._advance(self.tick_ms, batch)


def run_steady(lib, seed=1, minutes=60, base=3.0, sigma=0.35, tick_ms=TICK):
    random.seed(seed)
    s = Sim(lib, tick_ms=tick_ms, watchdog=tick_ms == TICK)   # the old lambda has no supervisor mode
    for _ in range(minutes * 60000 // tick_ms):
        n = random.gauss(0, sigma)
        s.tick(base * 0.7 + n * 0.6, base * 0.3 + n * 0.4)
    return writes(lib, 109)


print("== S1 steady house load with noise (1 h) ==")
wn = run_steady(new)
print("   new writes:", len(wn), amps(wn)[:12])
check("S1 at most 3 register-109 writes in an hour", len(wn) <= 3, str(len(wn)))
if old:
    wo = run_steady(old, tick_ms=5000)
    print("   baseline writes:", len(wo))
    check("S1 far fewer writes than the baseline", len(wn) <= max(2, len(wo) // 3), f"baseline={len(wo)} new={len(wn)}")

print("== S2 first write and strong step-down ==")
s = Sim(new)
s.tick(2.0, 1.0)
check("first write is floor(33-3)=30 A", amps(writes(new, 109)) == [30], str(amps(writes(new, 109))))
check("reg89 init write 0", any(r == 89 and v == 0 for _, r, v in writes(new)))
s.tick(8.0, 3.5)
w = writes(new, 109)
check("house step +8.5 A -> immediate write 21 A on the very next batch", amps(w)[-1] == 21 and len(w) == 2 and w[-1][0] == 4, str(w))

print("== S3 moderate drop and deadband ==")
s = Sim(new)
s.tick(2.0, 1.0)
n0 = new.h_nwrites()
s.tick(2.4, 1.0)
s.tick(2.5, 1.1)
check("drops <= 0.8 A ignored", new.h_nwrites() == n0)
s.tick(3.0, 1.5)
s.tick(3.0, 1.5)
s.tick(3.0, 1.5)
check("moderate drop not written after 3 batches (4 s)", new.h_nwrites() == n0)
s.tick(3.0, 1.5)
w = writes(new, 109)
check("moderate drop written after 4 batches (6 s) to 28", len(w) == 2 and amps(w)[-1] == 28, str(amps(w)))

print("== S4 rise needs 5 s and uses the prudent value ==")
s = Sim(new)
s.tick(8.0, 3.0)
s.tick(2.0, 1.0)
s.tick(2.5, 1.0)
s.tick(2.5, 1.0)
check("rise not written after 3 batches (4 s)", len(writes(new, 109)) == 1)
s.tick(2.5, 1.0)
w = writes(new, 109)
check("rise written at the 4th batch (6 s) at the prudent value 29", len(w) == 2 and amps(w)[-1] == 29, str(amps(w)))

print("== S5 massive overshoot (total >= 40 A) below 6 A: clamped, pause after 15 s ==")
s = Sim(new)
s.tick(2.0, 1.0)
for _ in range(8):
    s.tick(25.0, 12.0)   # 14 s below the floor
check("clamped to 6 A, no pause after 14 s", amps(writes(new, 109))[-1] == 6 and new.h_get(1) == 0)
s.tick(2.0, 1.0)
check("pause timer reset when headroom recovers", new.h_get(4) == 0 and new.h_get(9) == 0)
for _ in range(8):
    s.tick(25.0, 12.0)
check("still no pause after 14 s again", new.h_get(1) == 0)
s.tick(25.0, 12.0)
check("pause after 16 s observed (9th batch, threshold 15 s)", new.h_get(1) == 2 and amps(writes(new, 89))[-1] == "reg89=2")
n = new.h_nwrites()
for _ in range(5):
    s.tick(2.0, 1.0)
check("no resume after 5 batches (8 s)", new.h_get(1) == 2 and new.h_nwrites() == n)
s.tick(2.0, 1.0)
check("resume after 6 batches (10 s, threshold 9 s): reg89=0 then reg109", amps(writes(new))[-2:] == ["reg89=0", 30], str(amps(writes(new))[-3:]))
s2 = Sim(new)
s2.tick(2.0, 1.0)
for _ in range(9):
    s2.tick(25.0, 12.0)
n = new.h_nwrites()
s2.tick(25.0, 6.0)
s2.tick(26.0, 4.0)
s2.tick(23.0, 4.0)   # headroom 6 (< 8)
for _ in range(8):
    s2.tick(25.0, 4.0)
check("paused stays paused while headroom < 8", new.h_get(1) == 2 and new.h_nwrites() == n)

print("== S5b house peak with the car at the 6 A floor: no pause for 45 s ==")
s = Sim(new)
s.tick(2.0, 1.0)
for _ in range(15):
    s.tick(20.0, 10.0)   # 30 s above the floor, total 36 A (< 40 A), car at 6 A
check("S5b 30 s peak: car at 6 A, no pause, no reg89 write",
      new.h_get(0) == 6 and new.h_get(1) == 0 and not writes(new, 89)[1:], str(amps(writes(new))))
s.tick(2.0, 1.0)
check("S5b peak over: timers reset, still no pause", new.h_get(1) == 0 and new.h_get(9) == 0 and new.h_get(13) == 0)
for _ in range(6):
    s.tick(2.0, 1.0)
check("S5b setpoint comes back up afterwards (no status 19)", new.h_get(1) == 0 and amps(writes(new, 109))[-1] > 6)

s = Sim(new)
s.tick(2.0, 1.0)
for _ in range(23):
    s.tick(20.0, 10.0)   # 44 s observed
check("S5b no pause after 44 s at the floor", new.h_get(1) == 0)
s.tick(20.0, 10.0)       # 46 s observed, threshold 45 s
check("S5b pause after 46 s at the floor (threshold 45 s)", new.h_get(1) == 2 and amps(writes(new, 89))[-1] == "reg89=2")

print("== S5c massive overshoot while the car is at the floor stays reactive ==")
s = Sim(new)
s.tick(2.0, 1.0)
for _ in range(8):
    s.tick(25.0, 12.0)   # total 43 A
check("S5c no pause after 14 s", new.h_get(1) == 0)
s.tick(25.0, 12.0)
check("S5c pause after 16 s at total >= 40 A", new.h_get(1) == 2)

print("== S5d a short heavy spike does not shorten a running 45 s wait ==")
s = Sim(new)
s.tick(2.0, 1.0)
for _ in range(15):
    s.tick(20.0, 10.0)   # 30 s moderate
s.tick(25.0, 12.0)       # a single heavy sample (total 43 A)
check("S5d one heavy sample after 30 s: no pause", new.h_get(1) == 0)
s.tick(20.0, 10.0)
check("S5d heavy timer reset when total drops back below 40 A", new.h_get(13) == 0 and new.h_get(1) == 0)
for _ in range(9):
    s.tick(25.0, 12.0)   # heavy again for 16 s
check("S5d sustained heavy for 16 s: pause", new.h_get(1) == 2)

print("== S5e car not following its setpoint keeps the 15 s delay ==")
s = Sim(new)
s.tick(2.0, 1.0)
for _ in range(9):
    s.tick(20.0, 10.0, l2=12.0)   # setpoint 6 A but L2 stays at 12 A
check("S5e L2 well above the floor: pause after 16 s", new.h_get(1) == 2)

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

print("== S7 stale data fallback (supervisor) ==")
s = Sim(new)
s.tick(2.0, 1.0)
s.silence(15)   # last batch + 15 s: still not stale (limit is "> 15 s")
check("15 s of silence is not stale", new.h_get(7) == 0)
s.silence(5)
check("stale > 15 s -> fallback 10 A written by the supervisor", new.h_get(7) == 1 and amps(writes(new, 109))[-1] == 10)
n = new.h_nwrites()
s.silence(30)
check("fallback written only once", new.h_nwrites() == n)
for _ in range(3):
    s.tick(2.0, 1.0)
check("recovery: not raised after 3 batches", new.h_nwrites() == n and new.h_get(7) == 0)
s.tick(2.0, 1.0)
check("recovery: raised at the 4th batch to 30", amps(writes(new, 109))[-1] == 30)
s = Sim(new)
s.linky = False   # teleinfo silent after boot
s.tick(2.0, 1.0)
for _ in range(75):   # 150 s
    s.tick(2.0, 1.0)
check("Linky silent 150 s is not stale", new.h_get(7) == 0)
for _ in range(90):   # past 300 s
    s.tick(2.0, 1.0)
check("Linky silent > 300 s -> stale fallback", new.h_get(7) == 1 and amps(writes(new, 109))[-1] == 10)
s = Sim(new, watchdog=False)
s.silence(15)
check("no fallback write inside the 20 s boot grace", new.h_nwrites() == 0)
s = Sim(new)
s.silence(15)
check("supervisor: no fallback write inside the 20 s boot grace", new.h_nwrites() == 0)
s.silence(10)
check("supervisor: fallback after the boot grace if still blind", amps(writes(new, 109)) == [10])

print("== S8 other subscriptions / invalid PREF ==")
s = Sim(new, pref=9.0)
s.tick(2.0, 1.0)
check("9 kVA -> cap 32 A (ETEK maximum)", amps(writes(new, 109)) == [32])
s = Sim(new, pref=0.0)
s.tick(2.0, 1.0)
check("PREF=0 treated as invalid", new.h_get(7) == 1 and new.h_nwrites() == 0)

print("== S9 shipped constants ==")
check("shipped SAFETY_MARGIN_A is 0.3", abs(shipped_margin - 0.3) < 1e-9, str(shipped_margin))
s = Sim(shipped)
s.tick(2.0, 1.0)
check("shipped: first write floor(29.7)=29 A", amps(writes(shipped, 109)) == [29])
random.seed(2)
for _ in range(100):
    s.tick(2.0 + random.uniform(-0.3, 0.3), 1.0 + random.uniform(-0.1, 0.1))
check("shipped: +-0.4 A noise causes no rewrite in 5 min", len(writes(shipped, 109)) == 1)

print("== S10 event-driven safeguards ==")
# batch consistency: a split block must not drive the loop and must not look like fresh data
s = Sim(new)
s.tick(2.0, 1.0)
n = new.h_nwrites()
bm = new.h_get(11)
s.skewed(8.0, 3.5, d1_ms=800)
check("S10a skewed batch (L1 800 ms older) is ignored: no write, batch time unchanged", new.h_nwrites() == n and new.h_get(11) == bm)
s.skewed(8.0, 3.5, d1_ms=400)
check("S10b batch spread of 400 ms (< 500) is accepted: strong step-down written", new.h_nwrites() == n + 1)
s = Sim(new)
s.tick(2.0, 1.0)
n = new.h_nwrites()
for _ in range(6):
    s.skewed(2.0, 1.0, d1_ms=800)   # 12 s of rejected batches, fresh per-sensor timestamps
check("S10c rejected batches: not stale yet at 12 s", new.h_get(7) == 0 and new.h_nwrites() == n)
for _ in range(5):
    s.skewed(2.0, 1.0, d1_ms=800)
check("S10d rejected batches for > 15 s -> supervisor falls back to 10 A", new.h_get(7) == 1 and amps(writes(new, 109))[-1] == 10)
# supervisor must never advance debounce state nor write a normal setpoint
s = Sim(new, watchdog=False)
s.tick(8.0, 3.0)
s.tick(2.0, 1.0)
s.tick(2.5, 1.0)
before = [new.h_get(i) for i in (0, 1, 2, 3, 4, 5, 9, 12)]
nw = new.h_nwrites()
for _ in range(10):
    new.h_watchdog()
check("S10e supervisor on fresh data: no state change, no write", [new.h_get(i) for i in (0, 1, 2, 3, 4, 5, 9, 12)] == before and new.h_nwrites() == nw)
s = Sim(new)
s.tick(8.0, 3.0)
s.tick(2.0, 1.0)
s.tick(2.5, 1.0)
before = [new.h_get(i) for i in (2, 3, 12)]
s.silence(10)   # supervisor ticks twice on non-stale data
check("S10f pending rise untouched by supervisor ticks during a short silence", [new.h_get(i) for i in (2, 3, 12)] == before and new.h_get(7) == 0)

print("== S11 gap between batches resets the debounce timers ==")
s = Sim(new)
s.tick(8.0, 3.0)
s.tick(2.0, 1.0)
s.tick(2.5, 1.0)       # rise pending for 2 s
s.tick(2.5, 1.0, feed=False)
s.tick(2.5, 1.0, feed=False)   # 2 lost reads -> next batch is 6 s after the previous one (> 5 s)
n = new.h_nwrites()
s.tick(2.5, 1.0)
check("S11a rise is not confirmed across a data hole", new.h_nwrites() == n and new.h_get(2) == 1)
s.tick(2.5, 1.0)
s.tick(2.5, 1.0)
check("S11b ... not yet 4 s after the hole", new.h_nwrites() == n)
s.tick(2.5, 1.0)
check("S11b ... but confirmed 6 s after the hole", new.h_nwrites() == n + 1)
s = Sim(new)
s.tick(8.0, 3.0)
s.tick(2.0, 1.0)
s.tick(2.5, 1.0)
s.tick(2.5, 1.0, feed=False)   # a single lost read (4 s between batches) is tolerated
s.tick(2.5, 1.0)
check("S11b' one lost read (4 s between batches) keeps the timers: rise confirmed 6 s after its first candidate", len(writes(new, 109)) == 2)
s = Sim(new)
s.tick(2.0, 1.0)
for _ in range(6):
    s.tick(20.0, 10.0)   # 10 s below the floor
s.tick(20.0, 10.0, feed=False)
s.tick(20.0, 10.0, feed=False)
s.tick(20.0, 10.0)       # 6 s later = 16 s since the first sample, would pause without the reset
check("S11c pause timer does not span a data hole", new.h_get(1) == 0)
s = Sim(new)
s.tick(2.0, 1.0)
s.tick(2.5, 1.0, feed=False)
s.tick(2.5, 1.0, feed=False)
s.tick(8.0, 3.5)
check("S11d strong step-down still immediate right after a hole", amps(writes(new, 109))[-1] == 21)

print("== S12 millis-based timing is immune to bus jitter / retries ==")
bad = 0
for seed in range(200):
    random.seed(seed)
    s = Sim(new, jitter_ms=300)
    s.tick(8.0, 3.0)
    n = new.h_nwrites()
    s.tick(2.0, 1.0)
    s.tick(2.0, 1.0)
    s.tick(2.0, 1.0)
    early = new.h_nwrites() != n
    s.tick(2.0, 1.0)
    late = new.h_nwrites() == n
    bad += early or late
check("S12a rise confirmed at exactly the 4th batch with +-300 ms jitter (200 runs)", bad == 0, f"bad={bad}")
s = Sim(new)
s.tick(8.0, 3.0)
s.tick(2.0, 1.0)                # first candidate
s.tick(2.0, 1.0, feed=False)    # one lost read
s.tick(2.0, 1.0)                # 4 s after the candidate
n = new.h_nwrites()
s.tick(2.0, 1.0)                # 6 s after the candidate: confirmed one batch early compared with the count
check("S12b one lost Modbus read: rise confirmed by elapsed time (6 s), not by batch count", new.h_nwrites() == n + 1)

print("== S13 post-write holdoff for the taper freeze ==")
s = Sim(new)
s.tick(8.0, 3.0)     # t=2 s: write 22
for _ in range(4):
    s.tick(2.0, 1.0)  # candidate rise from t=4 s, written at t=10 s (30)
check("S13a rise to 30 written", amps(writes(new, 109))[-1] == 30)
s.tick(2.0, 1.0, l2=22.0)   # +2 s: L2 still at the old cap
check("S13b L2 still at the old cap 2 s after a write: no freeze", new.h_get(6) == 0)
s.tick(2.0, 1.0, l2=22.0)   # +4 s: L2 about to rise (4.8 s worst case)
check("S13b' L2 still low 4 s after a rise write: no freeze", new.h_get(6) == 0)
s.tick(2.0, 1.0, l2=29.5)   # +6 s: L2 caught up
check("S13c L2 caught up: no freeze", new.h_get(6) == 0)
s = Sim(new)
s.tick(8.0, 3.0)
for _ in range(4):
    s.tick(2.0, 1.0)        # rise written at t=10 s
s.tick(2.0, 1.0, l2=22.0)
s.tick(2.0, 1.0, l2=22.0)
s.tick(2.0, 1.0, l2=22.0)   # +6 s and L2 still low: real taper
check("S13d L2 still low 6 s after the write: freeze applies", new.h_get(6) == 1)
s = Sim(new)
s.tick(2.0, 1.0)
s.tick(18.0, 6.0)           # step-down write at t=6 s (strong)
check("S13e holdoff does not delay a strong step-down", amps(writes(new, 109))[-1] < 30 and len(writes(new, 109)) == 2)

print("== S14 reaction time to a house load step ==")
random.seed(5)
s = Sim(new, jitter_ms=100)
s.tick(2.0, 1.0)
s.tick(2.0, 1.0)
n = new.h_nwrites()
tstep = s.t + 1       # the step happens just after a sample
s.tick(2.0, 1.0)      # the batch that precedes the step
t0 = s.t
s.tick(10.0, 5.0)
lat = (writes(new, 109)[-1][0] * 1000 - t0)
check("S14 strong step written on the first batch after it (<= 2.3 s)", new.h_nwrites() == n + 1 and lat <= 2300, str(lat))

print()
print("FAILED: %d" % len(fails) if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
