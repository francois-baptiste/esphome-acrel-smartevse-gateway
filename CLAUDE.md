# CLAUDE.md - ESPHome gateway "passerelle-energie-waveshare"

ESP32-S3 (Waveshare RS485 board) that reads an Acrel ADL400 meter and a Linky meter, and runs a
dynamic load balancing (DLB) loop that caps the current of an ETEK EKEPC2-C EV charger so the house
never exceeds its breaker. Everything lives in `config.yaml` (comments are in French; keep that style).
This controls a real charger and a real home installation: read "Working agreements" before acting.

## Working agreements

- Commit and push to `master` only when the owner asks (they say "commit" / "push" explicitly; pushes to
  `master` have always been requested that way). Commit trailer used so far:
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (follow the attribution reminder of the session).
- Background sessions run inside a git worktree under `.claude/worktrees/`. The main checkout
  `/home/user/Documents/waveshare` is off-limits for edits and git commands from there; push with
  `git push origin <worktree-branch>:master`, then tell the owner to `git pull` in the main checkout.
- The Bash guard rejects heredocs, `git -C`, `cd` into the main checkout and `sleep` chains: write helper
  scripts to a file (job tmp dir) and run them; use Monitor / run_in_background to wait.
- Do not write to the live ETEK (registers 109 / 89) or flash the device unless asked. Flash when the car is
  idle (`etek_epc2_statut` = 1 and `acrel_courant_l2` ~ 0).
- The device is exposed on the owner's public IP through a temporary NAT with IP filtering. The owner knows
  and does not want it raised again.
- SmartEVSE is no longer used (code removed in 17f8833). Influx data before ~24 Aug is the SmartEVSE era.
- Python in WSL is 3.8: no `datetime.fromisoformat` on Influx timestamps, use `strptime` on the part before the dot.

## Build, validate, flash

```bash
# secrets.yaml is gitignored and exists only in the main checkout; copy it into a worktree, delete it afterwards
cp /home/user/Documents/waveshare/secrets.yaml ./secrets.yaml
ESPHOME=/home/user/Documents/waveshare/.venv/bin/esphome      # ESPHome 2026.7.0, ESP-IDF pinned to 5.4.4
$ESPHOME config config.yaml                       # validate (~1 min)
$ESPHOME compile --only-generate config.yaml      # generate C++ only; look at .esphome/build/passerelle-energie-waveshare/src/main.cpp
$ESPHOME compile config.yaml                      # full build, ~3 min with warm caches (run in background)
$ESPHOME upload config.yaml --device 90.40.199.251  # OTA on port 3232, no password, ~10 s, device reboots
rm secrets.yaml
python3 tools/dlb_sim/test_dlb.py [--baseline <git rev>]   # DLB logic tests, see below
```

Device (name `passerelle-energie-waveshare`, board `esp32-s3-devkitc-1`, public IP 90.40.199.251, may change):
- web UI `http://90.40.199.251/`; SSE stream `http://90.40.199.251/events` (`event: state` JSON with `id`
  (older firmware: `name_id`), `value`, `state`; `event: log` carries the DEBUG log, e.g. `etek_dlb` lines).
- native API on 6053 (noise key in secrets). `aioesphomeapi` hung at the ConnectRequest step on 2026-10-11;
  do not hammer it on the live device. Prefer SSE for reads and OTA for changes.
- The firmware's number entities are not writable through plain REST paths (404); use the web UI.

`tools/dlb_sim/` extracts the real `etek_dlb` lambda text from `config.yaml`, compiles it with g++ against
stub sensors and drives it with scenarios (needs g++): deadband, confirmation, clamp/pause/resume, taper
freeze, stale fallback, other subscriptions. Run it after any change to section 4. `tools/measure_dlb_latency.py`
measures write -> meter latency from the SSE feed (`--trigger direct` or `load-step`).
Quick checks, in order: `esphome config`, the g++ harness, `compile --only-generate`; the real `compile` is the
final one (the toolchain is cached in this WSL, so it works from a worktree).

## Hardware and architecture

- Single-phase installation, Linky in Standard TIC mode (UART, `teleinfo` component), subscription **6 kVA**
  (`PREF`). The house breaker is assumed to be 30 A = PREF/200 (not verified in Enedis documentation).
- RS485 bus: GPIO17/18, flow control GPIO21, **9600 8N1** (the ETEK only answers at 9600 despite the manual),
  shared by the Acrel ADL400 (address 0x0B) and the ETEK EKEPC2-C (address 0x64); each polled every 2 s.
- Acrel channel mapping: **L1 = house row 1, L2 = EV circuit, L3 = house row 2**. House load = L1 + L3.
- ETEK registers: **109** = max charging current ("max output PWM duty"), R/W, **167 raw units per ampere**
  (confirmed 2026-10-08: 16 A target -> 15.9 A on the Acrel); **89** = activation, 0 = active, 2 = emergency
  stop -> the ETEK goes to **status 19 for ~60 s** whatever is written afterwards and the car reports
  "charging stopped"; **141** = status (5 charging, 1 idle/finished, 19 post-stop lockout, 3 transitional;
  manual documents 0-13). Registers 146/148/149 (current/voltage/power) read ~0 and are unreliable; use the Acrel.
- Car: BMW i3 120 Ah (VIN WBY8P210307D38638, ~38.3 kWh usable), single-phase AC up to 32 A. End of charge: a
  ~3 A step down then a slow 20-30 min taper; a dip to ~0 and recovery to ~10 A in the middle of the taper is normal.
- Measured latency (register write -> Acrel L2): 2.7 s down, 3.6-4.8 s up. The ETEK steps in one sample;
  the old SmartEVSE slewed over ~9 s.
- Telemetry path: the ESP publishes every entity to MQTT broker 37.187.129.84:1883 as
  `passerelle-energie-waveshare/<domain>/<object_id>/state`. Telegraf on Windows
  (`C:\Program Files\Telegraf\telegraf.conf`) reads a local broker (127.0.0.1:1883; how the two brokers are
  bridged was not inspected) and writes InfluxDB 2.9.1 on localhost:8086. **Only `sensor` topics are
  ingested** (`passerelle-energie-waveshare/sensor/+/state`), so numbers (reg 109/89) and binary sensors
  (Linky overshoot bit) are invisible in Influx unless mirrored as template sensors (the `dlb_*` ones).

## DLB loop (config.yaml section 4, `interval: 5s`, id `etek_dlb`)

limit = PREF x 1000 / 200 x **1.10** (= 33 A at 6 kVA); headroom = limit - (L1 + L3) - **0.3 A** margin;
bounded to 6..32 A (IEC 61851 floor, circuit maximum). The Linky overshoot bit (STGE, set whenever S > PREF,
i.e. permanently above ~26 A) is deliberately NOT used.

| rule | value |
|---|---|
| write only if setpoint differs from headroom by more than | **0.8 A** deadband |
| drop >= 2 A | written immediately |
| moderate drop (0.8-2 A) / rise >= 1 A | confirmed on 2 consecutive cycles (rise uses the most prudent value of the window) |
| headroom < 6 A | clamped to 6 A; **pause (reg 89 = 2) only after 3 consecutive cycles** |
| resume (reg 89 = 0) | headroom > 8 A for 2 cycles, then the setpoint is rewritten |
| taper freeze (`dlb_gel_taper` = 1) | L2 measured > 2 A below setpoint and headroom >= L2 + 6 A: no register writes |
| stale inputs | Acrel L1/L2/L3 older than **15 s** or Linky `PREF` older than **300 s** (teleinfo updates only every ~100 s) or invalid PREF (<1): setpoint 10 A (never raised while blind), pause kept; 20 s boot grace |
| first cycle after boot | reg 89 = 0, then first setpoint write |

Rationale for the 0.3 A margin: replay of the 19->20 Oct night showed the deadband alone left the cap above the
limit in 26 % of cycles (worst +1.04 A); with 0.3 A it is 8 % (worst +0.57 A) with the same mean current (28.8 A)
and 3.6 register writes/h instead of 14.8/h. Set `SAFETY_MARGIN_A` to 0 for maximum current.

## Telemetry (InfluxDB)

Org `kimsufi`, bucket `buckbuck`, UTC timestamps (local time is CEST = UTC+2 until 2026-10-25, then CET = UTC+1).
`tools/flux.sh query.flux` runs a Flux file (token read from the Telegraf config; that file has CRLF line endings,
a trailing `\r` in the token gives `400 Bad Request`). Example (latest value of every DLB series):

```flux
from(bucket: "buckbuck")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "acrel" and r.topic =~ /sensor\/dlb_/)
  |> last()
  |> keep(columns: ["_time", "_value", "topic"])
```

Number of applied-setpoint changes (register 109 rewrites) in a window:

```flux
from(bucket: "buckbuck")
  |> range(start: -12h)
  |> filter(fn: (r) => r._measurement == "acrel" and r.topic =~ /sensor\/dlb_consigne_appliquee/)
  |> difference() |> filter(fn: (r) => r._value != 0) |> count()
```

Measurements: `acrel` (everything the ESP publishes; tag `topic`, field `value`), `bmw` (tags `vin`, `property`),
`smartevse` (legacy). Always match the sensor name after `/sensor/`: the node name contains "energie".

| topic (after `/sensor/`) | meaning |
|---|---|
| `acrel_courant_l1__rang__e_1_`, `..._l2__voiture_ev_`, `..._l3__rang__e_2_` | currents, A (L2 = EV) |
| `acrel_tension_l1/l2/l3`, `acrel_puissance_active_l1/l2/l3/totale` (kW), `acrel_puissance_apparente_*` | |
| `acrel_energie_import__e_l2__ev_` | EV circuit kWh counter |
| `linky_puissance_de_r__f__rence_souscrite` | PREF, kVA (6) |
| `linky_tension`, `linky_courant`, `linky_puissance_apparente_instantan__e` (VA), `linky_index_*` | ~1 sample / 100 s |
| `etek_epc2_statut` | reg 141 status (5 charging, 1 idle, 19 lockout); `etek_epc2_courant/tension/puissance` unreliable |
| `dlb_headroom_brut` | raw headroom, A (can be < 6 or < 0) |
| `dlb_limite_totale`, `dlb_courant_total_mesure` | limit (33) and measured L1+L2+L3, A |
| `dlb_cible_bornee` | headroom clamped to 6..32 and floored, A |
| `dlb_consigne_appliquee` | last value written to reg 109 by the loop, A (-1 = none since boot) |
| `dlb_consigne_etek_relue` | reg 109 read back from the ETEK / 167, A (should equal the line above) |
| `dlb_pause` | 1 while reg 89 = 2 |
| `dlb_compteur_pause`, `dlb_compteur_reprise` | consecutive cycles toward pause / resume |
| `dlb_confirmation_en_attente` | signed pending count (+ rise, - moderate drop) |
| `dlb_gel_taper` | 1 while the setpoint is frozen (taper or idle car) |
| `dlb_donnees_perimees` | 1 while inputs are stale |
| `dlb_depassement_linky` | Linky overshoot bit (informational) |

BMW properties of interest: `vehicle.powertrain.electric.battery.stateOfCharge.displayed`,
`vehicle.drivetrain.electricEngine.charging.acAmpere` (holds stale values), `...acVoltage`,
`vehicle.vehicle.travelledDistance`, `vehicle.drivetrain.batteryManagement.maxEnergy`. The feed is event driven
and sparse. Known Acrel/Telegraf logging gap: 2026-08-24 -> 2026-10-06.

## History worth knowing

- 2026-10-11 15:07-15:35 and 2026-10-12 22:03-22:57: repeated ~60 s stops (status 19). Cause: the loop then
  paused on the Linky overshoot bit (S > 6 kVA). Fixed by c6f0764 (bit ignored), then d2e39ee (pause debounce).
- 2026-10-12 14:53: "aggressive" mode (1.10 x breaker) introduced; ~32-33 A total ran for hours without a trip.
- Older SmartEVSE-era nights (Jul-Aug) show 2-30 current steps/h; ETEK 19 Oct (before d2e39ee) 11.7 steps/h.

## Current status

- `master` = 9ad5ed9 + this file. The flashed firmware was built from `config.yaml` at d2e39ee (unchanged in
  9ad5ed9), OTA-flashed 2026-10-21 ~13:48 CEST, ESPHome 2026.7.0, config_hash 0x36750ac7.
- Verified live at idle: boot, first write 31 A, read-back match, `dlb_*` series in Influx, loop cycling every 5 s.
- NOT yet verified on a real charge: deadband, taper freeze, 6 A clamp, pause/resume debounce, stale fallback.
- Open defect: 15-minute API reboot (known issue 1). Until it is fixed, count register writes per charging hour
  net of the post-boot ones (the series drops to -1 at each reboot).

## Checklist after the next charging session

1. `dlb_consigne_appliquee` changes per charging hour (query above): expect <= ~4/h (old logic 14.8/h).
2. `etek_epc2_statut` never 19 and `dlb_pause` stays 0 unless the house really exceeded the limit for > 15 s;
   no BMW "charging stopped" notifications.
3. `dlb_courant_total_mesure` <= `dlb_limite_totale` + ~0.6 A almost always (replay: 8 % of cycles slightly above).
4. `dlb_gel_taper` = 1 during the end-of-charge taper with no register writes in that period.
5. `dlb_donnees_perimees` = 0 all night; `dlb_consigne_etek_relue` ~ `dlb_consigne_appliquee` (writes land).
6. After a house load step (kettle, oven): a drop >= 2 A is written on the next 5 s cycle and L2 follows in ~3 s.
7. The ESP32 does not reboot during the session (see known issue below).

## Known issues / TODO

1. **The ESP32 reboots every 15 minutes** (found 2026-10-21; pre-existing, not caused by the DLB change).
   Evidence: setpoint series in Influx resets (`dlb_consigne_appliquee` -> -1) at 14:02:34, 14:17:34, 14:32:35,
   14:47:35, 15:02:36 (= flash time + n x 15:00); the log shows `[E][api:152]: No clients; rebooting` then
   `Storing reboot source: api` (SSE log capture at 15:17:36). Nothing keeps a native-API client connected, and
   `config.yaml` sets no `reboot_timeout`, so the ESPHome default (15 min) applies to `api:` (and also to `wifi:` / `mqtt:`,
   which only fire when their connection is lost).
   Effects: the DLB state is wiped every 15 min (debounce counters, applied setpoint, pause state; a paused
   charger gets `reg 89 = 0` at boot), one extra register-109 write per boot (~4/h on top of the 3.6/h from the
   replay, and it can move the setpoint by a deadband step), and ~10-20 s without DLB each time.
   Proposed fix (not applied; owner decides because it disables an auto-recovery watchdog): add
   `reboot_timeout: 0s` under `api:` (keep wifi/mqtt defaults), compile, flash with the car idle, then check that
   `dlb_consigne_appliquee` no longer drops to -1 every 15 min.
2. Teleinfo logs `[E][teleinfo] bad crc` about once a minute: Linky line noise, harmless, Linky values still update.
3. `dlb_donnees_perimees` is 1 for a few seconds after every boot (normal: no reading yet; the 20 s boot grace
   prevents a fallback write).
4. Aggressive mode (1.10 x breaker) rests on an unverified assumption about the breaker curve; ~33 A total has
   run for hours without a trip. Keep `ETEK_MAX_A` = 32 and the 33 A limit unless the owner changes them.
