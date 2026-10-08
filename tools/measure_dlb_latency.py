#!/usr/bin/env python3
"""Mesure la latence de la boucle DLB ETEK (config.yaml, section 6).

Se branche sur le flux SSE d'ESPHome (/events, cf. web_server) et horodate
localement chaque mise a jour des capteurs/entites utiles.

Deux modes (--trigger) :

  load-step (defaut) : mesure la boucle complete, y compris la partie qui ne
    passe PAS par la voiture (detection de charge + decision logicielle).
    Toi : provoque un echelon de charge connu sur L1 ou L3 (allumer/eteindre
    un appareil resistif) pendant que l'EV charge. Le script detecte :
      1) l'echelon reel sur L1+L3 (conso hors-EV)
      2) l'ecriture modbus registre 109 qui suit (decision de la boucle DLB)
      3) le moment ou Acrel L2 (courant reellement tire par l'EV) se
         stabilise sur la nouvelle cible
    et rapporte latence de decision (1->2) et latence physique (2->3).
    Utile pour verifier le comportement bout-en-bout de la boucle DLB elle-
    meme (anti-oscillation, confirm cycles, etc.).

  direct : isole UNIQUEMENT le temps de reaction reel de l'EVSE + la voiture,
    sans dependre de la logique de decision de la boucle DLB ni d'un vrai
    echelon de charge ambiant. Toi : pendant que le script tourne et que l'EV
    charge, change simplement la valeur de "ETEK EPC2 Registre 109" a la main
    (UI web de l'ESP, ou Home Assistant si integre). Le script detecte ce
    changement de cible des qu'il apparait sur le flux (c'est le moment ou
    l'ETEK a reellement recu le nouvel ordre sur le bus Modbus - plus fiable
    comme t0 qu'un timestamp d'appel HTTP local, qui ajouterait son propre
    delai reseau), puis mesure le temps jusqu'a ce qu'Acrel L2 (mesure
    independante, cf. note sur le registre 146 dans config.yaml) reflete la
    nouvelle valeur. C'est la reponse a "la boucle commence quand l'EVSE
    declenche un changement de courant et finit quand le compteur le voit" -
    la voiture reste branchee et en charge, seule la cible est pilotee a la
    main pour avoir un t0 net et controle.

Usage :
  python3 tools/measure_dlb_latency.py <ip-ou-hostname> [--trigger load-step|direct] [--duration 180] [--out fichier.csv]

Rappel architecture (cf. config.yaml section 6) : une BAISSE de cible est
ecrite immediatement par la boucle DLB (1 cycle, 5s), une HAUSSE exige 3
cycles DLB consecutifs confirmes (~15s) avant ecriture. Ca ne s'applique
qu'au mode load-step (c'est la boucle DLB qui decide) ; en mode direct, le
changement de cible est immediat puisque c'est toi qui l'imposes. Lance un
test dans chaque sens (cible qui monte / qui descend) pour caracteriser les
deux, la reponse physique EVSE+voiture n'etant pas forcement symetrique non
plus (ramp-up cote vehicule souvent plus lent qu'un ramp-down de securite).
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


def run_load_step(url, duration, writer):
    state = {"acrel_l1": None, "acrel_l3": None}
    last_non_ev = None
    trigger_t = None
    write_t = None
    target_a = None
    settle_count = 0
    t_start = time.time()

    print("Mode load-step : provoque ton echelon de charge sur L1/L3 maintenant.")

    for ts, key, value in iter_events(url):
        t_rel = ts - t_start
        if t_rel > duration:
            break
        writer.writerow([f"{t_rel:.3f}", key, value])

        if key in ("acrel_l1", "acrel_l3"):
            state[key] = value
            if state["acrel_l1"] is not None and state["acrel_l3"] is not None:
                non_ev = state["acrel_l1"] + state["acrel_l3"]
                if last_non_ev is not None and trigger_t is None and abs(non_ev - last_non_ev) >= LOAD_STEP_THRESHOLD_A:
                    trigger_t = t_rel
                    print(f"[{t_rel:6.1f}s] echelon detecte sur L1+L3 : {last_non_ev:.1f}A -> {non_ev:.1f}A")
                last_non_ev = non_ev

        if key == "reg109_raw" and trigger_t is not None and write_t is None:
            new_target_a = value / REG109_SCALE
            if target_a is None:
                target_a = new_target_a
            elif new_target_a != target_a:
                write_t = t_rel
                print(f"[{t_rel:6.1f}s] ecriture reg109 detectee : cible {target_a:.0f}A -> {new_target_a:.0f}A "
                      f"(latence decision = {write_t - trigger_t:.1f}s)")
                target_a = new_target_a

        if key == "acrel_l2" and write_t is not None:
            if abs(value - target_a) <= SETTLE_TOLERANCE_A:
                settle_count += 1
                if settle_count >= SETTLE_SAMPLES:
                    print(f"[{t_rel:6.1f}s] L2 stabilise a {value:.1f}A (cible {target_a:.0f}A) -> "
                          f"latence physique = {t_rel - write_t:.1f}s, "
                          f"latence totale = {t_rel - trigger_t:.1f}s")
                    break
            else:
                settle_count = 0


def run_direct(url, duration, writer):
    target_a = None
    trigger_t = None
    settle_count = 0
    t_start = time.time()

    print("Mode direct : des que l'EV charge, change 'ETEK EPC2 Registre 109' a la main "
          "(UI web / Home Assistant) - le script detecte le changement et chronometre depuis ce moment.")

    for ts, key, value in iter_events(url):
        t_rel = ts - t_start
        if t_rel > duration:
            break
        writer.writerow([f"{t_rel:.3f}", key, value])

        if key == "reg109_raw":
            new_target_a = value / REG109_SCALE
            if target_a is None:
                target_a = new_target_a
                print(f"[{t_rel:6.1f}s] cible initiale lue = {new_target_a:.0f}A")
            elif trigger_t is None and new_target_a != target_a:
                trigger_t = t_rel
                print(f"[{t_rel:6.1f}s] commande EVSE detectee (t0) : {target_a:.0f}A -> {new_target_a:.0f}A")
                target_a = new_target_a

        if key == "acrel_l2" and trigger_t is not None:
            if abs(value - target_a) <= SETTLE_TOLERANCE_A:
                settle_count += 1
                if settle_count >= SETTLE_SAMPLES:
                    print(f"[{t_rel:6.1f}s] L2 stabilise a {value:.1f}A (cible {target_a:.0f}A) -> "
                          f"lag EVSE->compteur = {t_rel - trigger_t:.1f}s")
                    break
            else:
                settle_count = 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("host", help="IP ou hostname du device ESPHome (ex: 90.40.199.251)")
    ap.add_argument("--trigger", choices=["load-step", "direct"], default="load-step",
                    help="load-step = boucle DLB complete via un vrai echelon L1/L3 ; "
                         "direct = isole juste le temps de reaction EVSE+voiture via un changement manuel de cible")
    ap.add_argument("--duration", type=float, default=180.0, help="duree de capture en secondes (defaut 180)")
    ap.add_argument("--out", default="dlb_latency.csv", help="fichier CSV de sortie")
    args = ap.parse_args()

    url = f"http://{args.host}/events"
    print(f"Capture sur {url} pendant {args.duration:.0f}s...")

    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["t_rel_s", "key", "value"])

        if args.trigger == "direct":
            run_direct(url, args.duration, writer)
        else:
            run_load_step(url, args.duration, writer)

    print(f"CSV complet (toutes les lectures horodatees) : {args.out}")


if __name__ == "__main__":
    main()
