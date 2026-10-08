#!/usr/bin/env python3
"""Mesure la latence de la boucle DLB ETEK (config.yaml, section 6).

Se branche sur le flux SSE d'ESPHome (/events, cf. web_server) et horodate
localement chaque mise a jour des capteurs/entites utiles, pour calculer :

  - latence de decision : temps entre un vrai changement de charge sur
    L1/L3 (conso hors-EV, mesuree par l'Acrel) et la prochaine ecriture
    du registre 109 par la boucle DLB (etek_epc2_reg109).
  - latence physique : temps entre cette ecriture et le moment ou le
    courant reellement tire par l'EV (Acrel L2, mesure independante de
    l'ETEK - cf. note sur le registre 146 dans config.yaml) se stabilise
    sur la nouvelle cible.

Usage :
  python3 tools/measure_dlb_latency.py <ip-ou-hostname> [--duration 180] [--out fichier.csv]

Pendant la capture, provoquer manuellement un echelon de charge connu sur
L1 ou L3 (allumer/eteindre un appareil resistif, bouilloire/radiateur...)
pendant que l'EV charge, puis laisser tourner jusqu'a la fin de la capture.
Le script detecte lui-meme l'echelon, l'ecriture modbus qui suit, et le
moment ou L2 s'est stabilise sur la nouvelle cible ; le CSV complet reste
disponible pour une analyse plus fine (tracer les courbes, etc.).

Rappel architecture (cf. config.yaml section 6) : une BAISSE de cible est
ecrite immediatement (1 cycle DLB, 5s), une HAUSSE exige 3 cycles DLB
consecutifs confirmes (~15s) avant ecriture. Lancer un test dans chaque
sens (charge qui apparait / qui disparait) pour caracteriser les deux
chemins separement - ils n'ont pas la meme latence par construction.
"""
import argparse
import csv
import json
import time
import urllib.request

NAME_ID_MAP = {
    "sensor/Acrel Courant L1 (Rangée 1)": "acrel_l1",
    "sensor/Acrel Courant L3 (Rangée 2)": "acrel_l3",
    "sensor/Acrel Courant L2 (Voiture EV)": "acrel_l2",
    "number/ETEK EPC2 Registre 109 (courant max, brut = A x 167)": "reg109_raw",
}

REG109_SCALE = 167
LOAD_STEP_THRESHOLD_A = 1.0   # variation L1+L3 jugee significative (vraie charge, pas du bruit)
SETTLE_TOLERANCE_A = 0.5      # tolerance pour juger L2 stabilise sur la nouvelle cible
SETTLE_SAMPLES = 2            # nb de lectures L2 consecutives dans la tolerance pour valider


def iter_events(url):
    with urllib.request.urlopen(url) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            key = NAME_ID_MAP.get(evt.get("name_id"))
            if key is None or "value" not in evt:
                continue
            try:
                value = float(evt["value"])
            except (TypeError, ValueError):
                continue
            yield time.time(), key, value


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("host", help="IP ou hostname du device ESPHome (ex: 90.40.199.251)")
    ap.add_argument("--duration", type=float, default=180.0, help="duree de capture en secondes (defaut 180)")
    ap.add_argument("--out", default="dlb_latency.csv", help="fichier CSV de sortie")
    args = ap.parse_args()

    url = f"http://{args.host}/events"
    state = {"acrel_l1": None, "acrel_l3": None, "acrel_l2": None, "reg109_raw": None}
    last_non_ev = None
    load_step_t = None
    write_t = None
    target_a = None
    settle_count = 0

    t_start = time.time()
    print(f"Capture sur {url} pendant {args.duration:.0f}s... "
          f"provoque ton echelon de charge sur L1/L3 maintenant.")

    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["t_rel_s", "key", "value"])

        for ts, key, value in iter_events(url):
            t_rel = ts - t_start
            if t_rel > args.duration:
                break
            writer.writerow([f"{t_rel:.3f}", key, value])
            state[key] = value

            if key in ("acrel_l1", "acrel_l3") and state["acrel_l1"] is not None and state["acrel_l3"] is not None:
                non_ev = state["acrel_l1"] + state["acrel_l3"]
                if last_non_ev is not None and load_step_t is None and abs(non_ev - last_non_ev) >= LOAD_STEP_THRESHOLD_A:
                    load_step_t = t_rel
                    print(f"[{t_rel:6.1f}s] echelon detecte sur L1+L3 : {last_non_ev:.1f}A -> {non_ev:.1f}A")
                last_non_ev = non_ev

            if key == "reg109_raw" and load_step_t is not None and write_t is None:
                new_target_a = value / REG109_SCALE
                if target_a is None:
                    target_a = new_target_a
                elif new_target_a != target_a:
                    write_t = t_rel
                    print(f"[{t_rel:6.1f}s] ecriture reg109 detectee : cible {target_a:.0f}A -> {new_target_a:.0f}A "
                          f"(latence decision = {write_t - load_step_t:.1f}s)")
                    target_a = new_target_a

            if key == "acrel_l2" and write_t is not None:
                if abs(value - target_a) <= SETTLE_TOLERANCE_A:
                    settle_count += 1
                    if settle_count >= SETTLE_SAMPLES:
                        print(f"[{t_rel:6.1f}s] L2 stabilise a {value:.1f}A (cible {target_a:.0f}A) -> "
                              f"latence physique = {t_rel - write_t:.1f}s, "
                              f"latence totale = {t_rel - load_step_t:.1f}s")
                        break
                else:
                    settle_count = 0

    print(f"CSV complet (toutes les lectures horodatees) : {args.out}")


if __name__ == "__main__":
    main()
