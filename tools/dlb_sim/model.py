"""Plant, house-load, fault and Linky-breaker models for long DLB runs (stress.py, test_dlb.py).

Everything is driven second by second; the real etek_dlb lambda (harness.py) is called at the instants the
firmware would run it (Acrel batch every 2 s, supervisor every 5 s, Linky frame every ~100 s).

Breaker model (disjoncteur de branchement, NOT documented by Enedis, calibrated on observations): first-order thermal
element, theta' = ((I/In)^2 - theta) / tau, trip when theta >= THETA_TRIP. Steady state never trips below
sqrt(THETA_TRIP) x In = 1.14 x In (34.2 A for In = 30 A), in line with the IEC 60898 conventional non-tripping current
(1.13 x In) and with the observed 7-8 kW (up to ~32.8 A) for hours without a trip. tau only sets how fast it trips above
that: "nominal" 600 s (1.45 x In from hot in ~3 min), "pessimistic" 150 s (4x faster). Current seen = L1 + L2 + L3.
"""
import math
import random
from array import array

PREF_KVA = 6.0
BREAKER_IN_A = PREF_KVA * 1000 / 200          # 30 A
LIMIT_A = BREAKER_IN_A * 1.10                 # 33 A, same as the firmware
REG109_SCALE = 167
THETA_TRIP = 1.30
BREAKERS = {"nominal": 600.0, "pessimistic": 150.0}
LOCKOUT_S = 60                                # ETEK status 19 after reg 89 = 2
MASK32 = 0xFFFFFFFF


class Breaker:
    def __init__(self, tau, in_a=BREAKER_IN_A, trip=THETA_TRIP):
        self.k = 1.0 - math.exp(-1.0 / tau)
        self.in_a, self.trip = in_a, trip
        self.theta = 0.0
        self.peak = 0.0
        self.trips = 0

    def step(self, amps):
        r = amps / self.in_a
        self.theta += (r * r - self.theta) * self.k
        if self.theta > self.peak:
            self.peak = self.theta
        if self.theta >= self.trip:
            self.trips += 1
            self.theta = 0.0

    @property
    def peak_equivalent_a(self):
        """Steady current that would bring the element to the peak temperature seen."""
        return self.in_a * math.sqrt(self.peak)


# ----------------------------------------------------------------------------------------------- house load
# name, per day, hour weights, amps (lo, hi), duration s (lo, hi), duty (on_s, period_s) or None
_KITCHEN = {7: 2, 8: 2, 12: 3, 13: 2, 18: 3, 19: 4, 20: 3, 21: 1}
_DAY = {h: 1 for h in range(7, 23)}
_EVENING = {18: 2, 19: 3, 20: 3, 21: 2, 22: 1}
_NIGHT = {22: 3, 23: 3, 0: 2, 1: 2, 2: 1, 3: 1}
APPLIANCES = [
    ("bouilloire", 6.0, _KITCHEN, (9.0, 10.0), (120, 240), None),
    ("micro-ondes", 3.0, _KITCHEN, (5.0, 6.0), (60, 300), None),
    ("four", 0.6, {11: 2, 12: 1, 18: 3, 19: 3}, (11.0, 12.0), (1800, 5400), (110, 200)),
    ("plaques", 2.0, _KITCHEN, (4.0, 16.0), (600, 2400), None),
    ("lave-linge", 0.6, {**_DAY, **_NIGHT}, (8.5, 9.5), (900, 1500), None),
    ("seche-linge", 0.3, _EVENING, (9.5, 10.5), (3600, 5400), (240, 300)),
    ("lave-vaisselle", 0.7, {**_EVENING, **_NIGHT}, (8.5, 9.5), (900, 1200), None),
    ("cumulus", 1.0, _NIGHT, (12.5, 13.5), (3600, 10800), None),
    ("cumulus jour", 0.3, _DAY, (12.5, 13.5), (1800, 5400), None),
    ("aspirateur", 0.3, _DAY, (4.5, 5.5), (600, 1200), None),
    ("seche-cheveux", 0.5, {7: 3, 8: 2, 21: 1, 22: 1}, (8.0, 9.0), (180, 420), None),
    ("grille-pain", 1.0, {7: 3, 8: 2, 19: 1}, (3.5, 4.5), (120, 240), None),
]


def _poisson(rng, lam):
    if lam <= 0:
        return 0
    l, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= l:
            return k
        k += 1


def _pick_hour(rng, weights):
    hs = list(weights)
    return rng.choices(hs, [weights[h] for h in hs])[0]


def make_house(seed, days, load_scale=1.0, house_max_a=29.0):
    """L1 + L3 (A) per second: base load (fridge, standby, slow drift) + random appliances.

    Clipped at house_max_a: a house that alone exceeds its breaker would trip whatever the car does (no DLB can help),
    so the occupants are assumed to switch something off. 29 A leaves the car at the 6 A floor with total up to ~35 A."""
    rng = random.Random(seed * 1009 + 1)
    n = days * 86400
    house = array("f", [0.0]) * (n + 7200)
    fridge_off = rng.randint(0, 2399)
    noise = 0.0
    for t in range(n + 7200):
        noise = 0.98 * noise + rng.gauss(0, 0.03)
        day_frac = (t % 86400) / 86400.0
        base = 1.8 + 0.5 * math.sin(2 * math.pi * (day_frac - 0.3)) + (0.9 if (t + fridge_off) % 2400 < 900 else 0.0)
        house[t] = max(0.2, base + noise)
    for d in range(days):
        for _name, per_day, hours, (alo, ahi), (dlo, dhi), duty in APPLIANCES:
            for _ in range(_poisson(rng, per_day * load_scale)):
                start = d * 86400 + _pick_hour(rng, hours) * 3600 + rng.randint(0, 3599)
                dur = rng.randint(dlo, dhi)
                amp = rng.uniform(alo, ahi)
                for k in range(start, min(n + 7200, start + dur)):
                    if duty is None or (k - start) % duty[1] < duty[0]:
                        house[k] += amp
    for t in range(n + 7200):
        if house[t] > house_max_a:
            house[t] = house_max_a
    return house


def make_demand(seed, days):
    """What the car asks for (A) per second, and a phase tag: 0 idle, 1 full, 2 taper / car pause.

    Nightly session: full current, optional car-side pauses, then the end of charge seen on the BMW i3 (a ~3 A step
    down, a 20-30 min taper, sometimes a dip to ~0 and recovery to ~10 A in the middle), then idle (0 A).
    """
    rng = random.Random(seed * 1013 + 2)
    n = days * 86400 + 7200
    dem = array("f", [0.0]) * n
    ph = bytearray(n)
    for d in range(days):
        if rng.random() < 0.15:
            continue
        t = d * 86400 + int(rng.uniform(21.0, 23.5) * 3600)
        full = int(rng.uniform(0.4, 5.5) * 3600)
        pauses = []
        if rng.random() < 0.25:
            ps = t + rng.randint(300, max(301, full - 300))
            pauses.append((ps, ps + rng.randint(120, 600)))
        for k in range(t, min(n, t + full)):
            dem[k] = 0.2 if any(a <= k < b for a, b in pauses) else 32.0
            ph[k] = 2 if dem[k] < 1 else 1
        t += full
        tl = rng.randint(1200, 1800)
        dip = (t + rng.randint(200, tl - 300), rng.randint(60, 120)) if rng.random() < 0.5 else None
        for i in range(tl):
            k = t + i
            if k >= n:
                break
            v = 29.0 * (1 - i / float(tl)) + 1.5
            if dip and dip[0] <= k < dip[0] + dip[1]:
                v = 0.3
            elif dip and dip[0] + dip[1] <= k < dip[0] + dip[1] + 90:
                v = 10.0
            dem[k] = max(0.0, v + rng.gauss(0, 0.2))
            ph[k] = 2
    return dem, ph


# ----------------------------------------------------------------------------------------------- faults
class Faults:
    def __init__(self, reboots_per_day=0.0, reboot_period_s=None, acrel_out_per_day=0.0, linky_out_per_day=0.0,
                 drop_p=0.0, write_loss_p=0.0, boot_s=(8, 20), linky_delay_s=(0, 100)):
        self.reboots_per_day, self.reboot_period_s = reboots_per_day, reboot_period_s
        self.acrel_out_per_day, self.linky_out_per_day = acrel_out_per_day, linky_out_per_day
        self.drop_p, self.write_loss_p = drop_p, write_loss_p
        self.boot_s, self.linky_delay_s = boot_s, linky_delay_s


PROFILES = {
    "clean": Faults(),
    "faults": Faults(reboots_per_day=2, acrel_out_per_day=6, linky_out_per_day=1, drop_p=0.02, write_loss_p=0.01),
    "hostile": Faults(reboots_per_day=12, acrel_out_per_day=30, linky_out_per_day=4, drop_p=0.08),
    # 5 % of the reg 109 writes never reach the ETEK. The firmware never reads the register back to check, so a lost
    # write leaves the car on the old cap until the target moves again (known limitation, see CLAUDE.md).
    "lostwrites": Faults(write_loss_p=0.05),
    "reboot15": Faults(reboot_period_s=900),          # the old API-timeout reboot, every 15 min
}


def _schedule(f, n, seed):
    rng = random.Random(seed * 1019 + 3)
    days = n / 86400.0
    reboots = {}
    if f.reboot_period_s:
        for t in range(f.reboot_period_s, n, f.reboot_period_s):
            reboots[t] = (rng.randint(*f.boot_s), rng.randint(*f.linky_delay_s))
    for _ in range(_poisson(rng, f.reboots_per_day * days)):
        reboots[rng.randint(60, n - 60)] = (rng.randint(*f.boot_s), rng.randint(*f.linky_delay_s))

    def outages(per_day, durations):
        a = bytearray(n)
        for _ in range(_poisson(rng, per_day * days)):
            s, dur = rng.randint(0, n - 1), rng.choice(durations)
            for k in range(s, min(n, s + dur)):
                a[k] = 1
        return a
    acrel_out = outages(f.acrel_out_per_day, [3, 3, 8, 8, 20, 60, 300, 1800])
    linky_out = outages(f.linky_out_per_day, [200, 400, 900, 3600])
    drop, wloss = bytearray(n), bytearray(n)
    if f.drop_p:
        for t in range(0, n, 2):
            if rng.random() < f.drop_p:
                for k in range(t, min(n, t + 2 * (1 + (rng.random() < 0.3) + (rng.random() < 0.1)))):
                    drop[k] = 1
    if f.write_loss_p:
        for t in range(n):
            if rng.random() < f.write_loss_p:
                wloss[t] = 1
    return reboots, acrel_out, linky_out, drop, wloss


# ----------------------------------------------------------------------------------------------- simulation
def simulate(lib, house, demand, phase, faults=None, seed=1, days=None, millis_start=0, limit=LIMIT_A):
    """Run `days` of the loop (lib=None: no DLB, the ETEK stays at 32 A). Returns a metrics dict.

    ETEK model: register 109 takes effect 2.7 s (down) / 4.2 s (up) after the write, a lost write never arrives;
    reg 89 = 2 stops the charge and locks the ETEK out for 60 s (status 19); the ETEK keeps its settings when the ESP
    reboots. ESP model: a reboot wipes the firmware state (h_reset), nothing runs for `boot_s`, the first Linky frame
    arrives `linky_delay_s` later, millis() restarts at 0.
    """
    n = (days * 86400) if days else min(len(house), len(demand))
    f = faults or Faults()
    reboots, acrel_out, linky_out, drop, wloss = _schedule(f, n, seed)
    rng = random.Random(seed * 1021 + 4)
    brk = {k: Breaker(tau) for k, tau in BREAKERS.items()}
    m = dict(n=n, w109=0, pauses=0, resumes=0, paused_s=0, over0=0, over15=0, over3=0, peak=0.0, ev=0.0, ideal=0.0,
             fallback=0, quiet_w=0, reboot_w=0, lost=0, viol=0, reboots=0, stale_s=0, pauses_active=0, pauses_idle=0, ep_n=0, ep_max_s=0)
    ep_start, ep_peak = None, 0.0
    episodes = []
    esp = lib is not None
    cap_eff, pend = 32.0, []
    etek_on, lockout_until = True, -1
    ri, boot_s, clock0 = 0, 3, millis_start
    last_reboot = -10 ** 9
    linky_next = rng.randint(*f.linky_delay_s)
    nw = 0
    if esp:
        lib.h_reset()
    pref = PREF_KVA
    cur_ev = [0.0]
    split = 0.55

    def process(t, frozen_after, stale_flag):
        nonlocal nw, etek_on, lockout_until
        while nw < lib.h_nwrites():
            reg, val = lib.h_write_reg(nw), lib.h_write_val(nw)
            nw += 1
            if reg == 109:
                m["w109"] += 1
                if stale_flag:
                    m["fallback"] += 1
                elif frozen_after:
                    m["viol"] += 1
                if phase[t] != 1 and not stale_flag:
                    m["quiet_w"] += 1
                if t - last_reboot < 180:
                    m["reboot_w"] += 1
                if wloss[t]:
                    m["lost"] += 1
                    continue
                amp = val / REG109_SCALE
                latest = pend[-1][1] if pend else cap_eff
                pend.append((t + (2.7 if amp < latest else 4.2), amp))
                pend.sort()
            elif reg == 89:
                if val == 2:
                    m["pauses"] += 1
                    if demand[t] > 1.0:
                        m["pauses_active"] += 1
                    if cur_ev[0] < 0.5:
                        m["pauses_idle"] += 1      # the firmware must never do this (idle-car guard)
                    etek_on = False
                    lockout_until = t + LOCKOUT_S
                else:
                    if not etek_on:
                        m["resumes"] += 1
                    etek_on = True

    for t in range(n):
        if esp and t in reboots:
            boot_s, ldelay = reboots[t]
            lib.h_reset()
            nw = 0
            ri, clock0, last_reboot = t, 0, t
            linky_next = t + boot_s + ldelay
            m["reboots"] += 1
        while pend and pend[0][0] <= t:
            cap_eff = pend.pop(0)[1]
        locked = (not etek_on) or t < lockout_until
        want = demand[t]
        ev = 0.0 if locked else min(want, cap_eff)
        if locked and want > 1.0:
            m["paused_s"] += 1
        house_a = house[t]
        total = house_a + ev
        cur_ev[0] = ev
        m["ev"] += ev
        m["ideal"] += min(want, max(0.0, min(32.0, limit - house_a)))
        if total > limit:
            m["over0"] += 1
            if total > limit + 1.5:
                m["over15"] += 1
                if total > limit + 3.0:
                    m["over3"] += 1
        if total - limit > m["peak"]:
            m["peak"] = total - limit
        # over-limit episodes (> limit + 3 A): how long and how high, to separate one-sample steps from stuck caps
        if total > limit + 3.0:
            if ep_start is None:
                ep_start, ep_peak = t, 0.0
            ep_peak = max(ep_peak, total - limit)
        elif ep_start is not None:
            episodes.append((ep_start, t - ep_start, ep_peak, phase[ep_start], house_a, ev, cap_eff))
            m["ep_n"] += 1
            m["ep_max_s"] = max(m["ep_max_s"], t - ep_start)
            ep_start = None
        for b in brk.values():
            b.step(total)
        if not esp or t < ri + boot_s:
            continue
        ms = ((t - ri) * 1000 + clock0) & MASK32
        if t >= linky_next and not linky_out[t]:
            lib.h_set_time(ms)
            lib.h_feed_linky(pref)
            linky_next = t + 100
        if (t - ri - boot_s) % 2 == 0 and not acrel_out[t] and not drop[t]:
            lib.h_set_time((ms + rng.randint(0, 200)) & MASK32)
            lib.h_feed_acrel(house_a * split, ev, house_a * (1 - split))
            lib.h_cycle()
            process(t, lib.h_get(6) == 1, lib.h_get(7) == 1)
        if (t - ri) % 5 == 0:
            lib.h_set_time((ms + 300) & MASK32)
            lib.h_watchdog()
            process(t, False, lib.h_get(7) == 1)
        if lib.h_get(7) == 1:
            m["stale_s"] += 1
    m["episodes"] = episodes
    m["brk"] = {k: (b.trips, b.peak_equivalent_a) for k, b in brk.items()}
    return m
