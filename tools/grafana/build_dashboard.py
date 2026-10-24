#!/usr/bin/env python3
"""Génère le dashboard Grafana (schéma v2) « Tableau Élec bmw » : Acrel, BMW et tout le DLB (config.yaml section 4).

    python3 tools/grafana/build_dashboard.py > tools/grafana/dashboard_elec_bmw.json

Import : Grafana > Dashboards > ... > Edit > Settings > JSON Model (ou New > Import). Datasource InfluxDB (uid ci-dessous),
bucket `buckbuck`, mesures `acrel` (tout ce que publie l'ESP, tag `topic`) et `bmw` (tag `property`). Python 3.8.
"""
import json

DS = "cfpmdg2x9bim8a"
PREFIX = "passerelle-energie-waveshare/sensor/"
VIZ_VERSION = "13.2.0-28084260209"

elements, items = {}, []
_next = [1]


def flux_series(sensor, label, fn="last", window="v.windowPeriod", var=None):
    """Une série de la mesure acrel, nommée `label` (colonne topic)."""
    return (
        'from(bucket: "buckbuck")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r._measurement == "acrel" and r.topic == "%s%s/state")\n'
        '  |> keep(columns: ["_time", "_value", "topic"])\n'
        '  |> map(fn: (r) => ({ r with topic: "%s" }))\n'
        '  |> group(columns: ["topic"])\n'
        "  |> aggregateWindow(every: %s, column: \"_value\", fn: %s, createEmpty: false)" % (PREFIX, sensor, label, window, fn))


def flux_union(series):
    """series : liste de (sensor, label[, fn]) -> union de requêtes nommées."""
    if len(series) == 1:
        return flux_series(*series[0])
    parts, names = [], []
    for i, s in enumerate(series):
        name = "S%d" % i
        parts.append("%s = %s" % (name, flux_series(*s)))
        names.append(name)
    return "\n\n".join(parts) + "\n\nunion(tables: [%s])" % ", ".join(names)


def flux_last(sensor):
    """Dernière valeur d'un capteur acrel dans la plage (stat)."""
    return (
        'from(bucket: "buckbuck")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r._measurement == "acrel" and r.topic == "%s%s/state")\n'
        '  |> keep(columns: ["_time", "_value"])\n'
        "  |> last()" % (PREFIX, sensor))


def flux_count_per_hour(sensor, label, cond):
    return (
        'from(bucket: "buckbuck")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        '  |> filter(fn: (r) => r._measurement == "acrel" and r.topic == "%s%s/state")\n'
        "  |> difference()\n"
        "  |> filter(fn: (r) => %s)\n"
        "  |> aggregateWindow(every: 1h, column: \"_value\", fn: count, createEmpty: true)\n"
        '  |> map(fn: (r) => ({ _time: r._time, _value: r._value, topic: "%s" }))\n'
        '  |> group(columns: ["topic"])' % (PREFIX, sensor, cond, label))


SOC_SERIES = '''import "date"
import "internal/debug"

debut = date.sub(d: 3d, from: v.timeRangeStart)

soc = from(bucket: "buckbuck")
  |> range(start: debut, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "bmw" and r.property == "vehicle.powertrain.electric.battery.stateOfCharge.displayed")
  |> aggregateWindow(every: 5m, fn: last, createEmpty: true)
  |> keep(columns: ["_time", "_value"])

cnt = from(bucket: "buckbuck")
  |> range(start: debut, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "acrel" and r.topic == "passerelle-energie-waveshare/sensor/acrel_energie_import__e_l2__ev_/state")
  |> aggregateWindow(every: 5m, fn: last, createEmpty: true)
  |> fill(usePrevious: true)
  |> keep(columns: ["_time", "_value"])

join(tables: {s: soc, c: cnt}, on: ["_time"])
  |> map(fn: (r) => ({
      _time: r._time,
      soc: r._value_s,
      c: r._value_c,
      e0: if exists r._value_s then r._value_c else debug.null(type: "float")
    }))
  |> fill(column: "soc", usePrevious: true)
  |> fill(column: "e0", usePrevious: true)
  |> filter(fn: (r) => exists r.soc and exists r.e0 and exists r.c and r._time >= v.timeRangeStart)
  |> map(fn: (r) => ({
      _time: r._time,
      topic: "SoC estimé (Acrel)",
      _value: if r.soc + (r.c - r.e0) * 0.86 / 38.1 * 100.0 > 100.0 then 100.0 else r.soc + (r.c - r.e0) * 0.86 / 38.1 * 100.0
    }))
  |> group(columns: ["topic"])'''

# Même calcul que tools/soc_estime.flux, indépendant de la plage du dashboard (dernier SoC BMW + énergie depuis).
SOC_NOW = '''import "array"
import "date"

bmw_last = (p) =>
  from(bucket: "buckbuck")
    |> range(start: -7d)
    |> filter(fn: (r) => r._measurement == "bmw" and r.property =~ p)
    |> last()
    |> findRecord(fn: (key) => true, idx: 0)

soc = bmw_last(p: /stateOfCharge\\.displayed/)
cap = bmw_last(p: /batteryManagement\\.maxEnergy/)

compteur_a = (stop) =>
  from(bucket: "buckbuck")
    |> range(start: -30d, stop: stop)
    |> filter(fn: (r) => r._measurement == "acrel" and r.topic == "passerelle-energie-waveshare/sensor/acrel_energie_import__e_l2__ev_/state")
    |> last()
    |> findRecord(fn: (key) => true, idx: 0)

e0 = compteur_a(stop: date.add(d: 1s, to: soc._time))
e1 = compteur_a(stop: now())
gain = (e1._value - e0._value) * 0.86 / cap._value * 100.0

array.from(rows: [{_time: now(), _value: if soc._value + gain > 100.0 then 100.0 else soc._value + gain}])'''

BMW_SOC = '''from(bucket: "buckbuck")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "bmw" and r.property == "vehicle.powertrain.electric.battery.stateOfCharge.displayed")
  |> keep(columns: ["_time", "_value"])
  |> map(fn: (r) => ({ r with topic: "SoC BMW (affiché)" }))
  |> group(columns: ["topic"])
  |> aggregateWindow(every: v.windowPeriod, column: "_value", fn: last, createEmpty: false)'''


def query(ref, text, hidden=False):
    return {"kind": "PanelQuery", "spec": {"query": {"kind": "DataQuery", "group": "influxdb", "version": "v0",
            "datasource": {"name": DS}, "spec": {"query": text, "range": True}}, "refId": ref, "hidden": hidden}}


def color(name, fixed, extra=None):
    props = [{"id": "color", "value": {"fixedColor": fixed, "mode": "fixed"}}]
    props += extra or []
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def ts_custom(**kw):
    c = {"axisBorderShow": False, "axisCenteredZero": False, "axisColorMode": "text", "axisLabel": "", "axisPlacement": "auto",
         "barAlignment": 0, "barWidthFactor": 0.9, "drawStyle": "line", "fillOpacity": 5, "gradientMode": "none",
         "hideFrom": {"legend": False, "tooltip": False, "viz": False}, "insertNulls": False, "lineInterpolation": "linear",
         "lineStyle": {"fill": "solid"}, "lineWidth": 2, "pointSize": 1, "scaleDistribution": {"type": "linear"},
         "showPoints": "never", "showValues": False, "spanNulls": False, "stacking": {"group": "A", "mode": "none"},
         "thresholdsStyle": {"mode": "off"}}
    c.update(kw)
    return c


def timeseries(title, desc, queries, unit=None, overrides=None, legend=("mean", "max"), legend_mode="table", steps=None,
               custom=None, minv=None, maxv=None, decimals=None, mappings=None):
    defaults = {"thresholds": {"mode": "absolute", "steps": steps or [{"value": 0, "color": "green"}]},
                "color": {"mode": "palette-classic", "seriesBy": "max"}, "custom": ts_custom(**(custom or {}))}
    for k, v in (("unit", unit), ("min", minv), ("max", maxv), ("decimals", decimals), ("mappings", mappings)):
        if v is not None:
            defaults[k] = v
    options = {"annotations": {"clustering": -1, "multiLane": False},
               "legend": {"calcs": list(legend), "displayMode": legend_mode, "enableFacetedFilter": False, "overflow": "ellipsis",
                          "placement": "bottom", "showLegend": True},
               "timezone": ["browser"], "tooltip": {"hideZeros": False, "mode": "multi", "sort": "none"}}
    return panel(title, desc, queries, "timeseries", options, {"defaults": defaults, "overrides": overrides or []})


def stat(title, desc, text, unit=None, steps=None, mappings=None, decimals=None, color_mode="value", graph="none"):
    defaults = {"thresholds": {"mode": "absolute", "steps": steps or [{"value": 0, "color": "green"}]}, "color": {"mode": "thresholds"}}
    for k, v in (("unit", unit), ("mappings", mappings), ("decimals", decimals)):
        if v is not None:
            defaults[k] = v
    options = {"colorMode": color_mode, "graphMode": graph, "justifyMode": "auto", "orientation": "auto",
               "percentChangeColorMode": "standard", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
               "showPercentChange": False, "text": {}, "textMode": "value", "wideLayout": True}
    return panel(title, desc, [query("A", text)], "stat", options, {"defaults": defaults, "overrides": []})


def state_timeline(title, desc, queries, mappings, overrides=None):
    defaults = {"thresholds": {"mode": "absolute", "steps": [{"value": 0, "color": "green"}]}, "color": {"mode": "thresholds"},
                "mappings": mappings, "custom": {"fillOpacity": 80, "lineWidth": 0, "hideFrom": {"legend": False, "tooltip": False, "viz": False}}}
    options = {"alignValue": "left", "mergeValues": True, "rowHeight": 0.8, "showValue": "auto",
               "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
               "tooltip": {"hideZeros": False, "mode": "single", "sort": "none"}}
    return panel(title, desc, queries, "state-timeline", options, {"defaults": defaults, "overrides": overrides or []})


def panel(title, desc, queries, group, options, fieldconfig):
    pid = _next[0]
    _next[0] += 1
    key = "panel-%d" % pid
    elements[key] = {"kind": "Panel", "spec": {"id": pid, "title": title, "description": desc, "links": [],
        "data": {"kind": "QueryGroup", "spec": {"queries": queries, "transformations": [], "queryOptions": {}}},
        "vizConfig": {"kind": "VizConfig", "group": group, "version": VIZ_VERSION, "spec": {"options": options, "fieldConfig": fieldconfig}}}}
    return key


def place(key, x, y, w, h):
    items.append({"kind": "GridLayoutItem", "spec": {"x": x, "y": y, "width": w, "height": h,
                  "element": {"kind": "ElementReference", "name": key}}})


def vmap(table):
    return [{"type": "value", "options": {str(k): {"text": t, "color": c, "index": i} for i, (k, (t, c)) in enumerate(table.items())}}]


STATUT = vmap({1: ("Repos", "blue"), 3: ("Transition", "blue"), 5: ("En charge", "green"), 19: ("Verrou 60 s (pause)", "red")})
ONOFF_WARN = vmap({0: ("Non", "green"), 1: ("Oui", "red")})
ONOFF_INFO = vmap({0: ("Non", "dark-green"), 1: ("Oui", "blue")})

# ---------------------------------------------------------------- 1. synthèse (stats)
y = 0
place(stat("Statut borne ETEK", "Registre 141 : 5 en charge, 1 repos, 3 transition, 19 verrou ~60 s après une pause (reg 89 = 2).",
           flux_last("etek_epc2_statut"), mappings=STATUT, steps=[{"value": 0, "color": "blue"}], color_mode="background"), 0, y, 5, 4)
place(stat("Consigne appliquée (A)", "Dernière valeur écrite au registre 109 par le DLB (-1 = aucune depuis le démarrage de l'ESP).",
           flux_last("dlb_consigne_appliquee"), unit="amp", decimals=0,
           steps=[{"value": -1, "color": "red"}, {"value": 6, "color": "orange"}, {"value": 10, "color": "green"}]), 5, y, 4, 4)
place(stat("Courant total / limite (A)", "L1+L2+L3 mesuré par l'Acrel. Limite 33 A = PREF 6 kVA / 200 x 1,10. Rouge au-delà de 33 A.",
           flux_last("dlb_courant_total_mesure"), unit="amp", decimals=1,
           steps=[{"value": 0, "color": "green"}, {"value": 30, "color": "orange"}, {"value": 33, "color": "red"}]), 9, y, 5, 4)
place(stat("SoC estimé (%)", "Dernier SoC BMW + énergie Acrel L2 depuis x 0,86 / capacité (tools/soc_estime.flux). Indépendant de la plage de temps.",
           SOC_NOW, unit="percent", decimals=0, steps=[{"value": 0, "color": "red"}, {"value": 20, "color": "orange"}, {"value": 50, "color": "green"}]), 14, y, 4, 4)
place(stat("Pause DLB", "1 pendant reg 89 = 2 (borne arrêtée).", flux_last("dlb_pause"), mappings=ONOFF_WARN,
           steps=[{"value": 0, "color": "green"}], color_mode="background"), 18, y, 3, 4)
place(stat("Données périmées", "1 si Acrel > 15 s ou Linky PREF > 300 s : consigne de secours 32 A.", flux_last("dlb_donnees_perimees"),
           mappings=ONOFF_WARN, steps=[{"value": 0, "color": "green"}], color_mode="background"), 21, y, 3, 4)
y += 4

# ---------------------------------------------------------------- 2. DLB
place(timeseries(
    "DLB - Courant total vs limite (A)",
    "Courant total mesuré (L1+L2+L3) contre la limite 33 A, headroom brut (limite - maison - 0,3 A, peut être < 6 ou < 0) et cible bornée 6..32 A.",
    [query("A", flux_union([("dlb_courant_total_mesure", "Total mesuré"), ("dlb_limite_totale", "Limite"),
                            ("dlb_headroom_brut", "Headroom brut"), ("dlb_cible_bornee", "Cible bornée")]))],
    unit="amp", legend=("mean", "max", "last"), overrides=[
        color("Total mesuré", "green", [{"id": "custom.lineWidth", "value": 3}]),
        color("Limite", "red", [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}, {"id": "custom.fillOpacity", "value": 0}]),
        color("Headroom brut", "blue", [{"id": "custom.fillOpacity", "value": 0}]),
        color("Cible bornée", "purple", [{"id": "custom.fillOpacity", "value": 0}, {"id": "custom.lineInterpolation", "value": "stepAfter"}])]),
    0, y, 24, 9)
y += 9
place(timeseries(
    "DLB - Consigne vs courant voiture (A)",
    "Consigne écrite (reg 109), relue côté ETEK (doit être égale) et courant réel de la voiture (Acrel L2). L2 suit la consigne en 3-5 s ; un écart durable = gel taper (fin de charge).",
    [query("A", flux_union([("dlb_consigne_appliquee", "Consigne appliquée"), ("dlb_consigne_etek_relue", "Consigne relue ETEK"),
                            ("acrel_courant_l2__voiture_ev_", "Courant voiture L2")]))],
    unit="amp", legend=("last", "min", "max"), overrides=[
        color("Consigne appliquée", "blue", [{"id": "custom.lineInterpolation", "value": "stepAfter"}, {"id": "custom.fillOpacity", "value": 0}]),
        color("Consigne relue ETEK", "light-blue", [{"id": "custom.lineInterpolation", "value": "stepAfter"},
                                                     {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 6]}}, {"id": "custom.fillOpacity", "value": 0}]),
        color("Courant voiture L2", "orange")]),
    0, y, 24, 8)
y += 8
place(state_timeline("Statut borne ETEK (reg 141)", "Chronologie du statut : 5 en charge, 1 repos, 3 transition, 19 verrou après pause.",
                     [query("A", flux_union([("etek_epc2_statut", "Statut ETEK", "last")]))], STATUT), 0, y, 12, 5)
place(state_timeline("DLB - Drapeaux", "Pause (reg 89 = 2), gel taper (consigne figée en fin de charge ou voiture au repos), données périmées (fallback 32 A), bit dépassement Linky (informatif).",
                     [query("A", flux_union([("dlb_pause", "Pause", "max"), ("dlb_gel_taper", "Gel taper", "max"),
                                             ("dlb_donnees_perimees", "Données périmées", "max"), ("dlb_depassement_linky", "Dépassement Linky (info)", "max")]))],
                     ONOFF_INFO, overrides=[
                         {"matcher": {"id": "byName", "options": n}, "properties": [{"id": "mappings", "value": ONOFF_WARN}]}
                         for n in ("Pause", "Données périmées")]), 12, y, 12, 5)
y += 5
place(timeseries("DLB - Confirmations et compteurs",
                 "Confirmation en attente (signée : + montée, - baisse modérée), compteurs de cycles vers la pause et la reprise (télémétrie ; les décisions utilisent des minuteurs millis()).",
                 [query("A", flux_union([("dlb_confirmation_en_attente", "Confirmation en attente", "last"), ("dlb_compteur_pause", "Compteur pause", "max"),
                                         ("dlb_compteur_reprise", "Compteur reprise", "max")]))],
                 legend=("max", "last"), custom={"lineInterpolation": "stepAfter", "fillOpacity": 0}), 0, y, 12, 8)
place(timeseries("DLB - Réécritures reg 109 et pauses par heure",
                 "Nombre de changements de consigne (reg 109) par heure (attendu <= ~4/h, ancienne logique 14,8/h) et de pauses (reg 89 = 2). Un retour à -1 (redémarrage ESP) compte comme un changement.",
                 [query("A", flux_count_per_hour("dlb_consigne_appliquee", "Réécritures reg 109/h", "r._value != 0.0")),
                  query("B", flux_count_per_hour("dlb_pause", "Pauses/h", "r._value > 0.5"))],
                 legend=("sum", "max"), custom={"drawStyle": "bars", "fillOpacity": 60, "lineWidth": 1}, decimals=0,
                 overrides=[color("Réécritures reg 109/h", "blue"), color("Pauses/h", "red")]), 12, y, 12, 8)
y += 8

# ---------------------------------------------------------------- 3. Linky
place(timeseries("Linky - Courant, tension et puissance apparente",
                 "Mesures du Linky (TIC standard, ~1 échantillon / 100 s). Puissance apparente en VA sur l'axe de droite.",
                 [query("A", flux_union([("linky_courant", "Courant Linky (A)", "last"), ("linky_tension", "Tension Linky (V)", "last"),
                                         ("linky_puissance_apparente_instantan__e", "Puissance apparente (VA)", "last")]))],
                 legend=("mean", "max", "last"), custom={"showPoints": "auto", "pointSize": 4, "spanNulls": True, "fillOpacity": 0},
                 overrides=[color("Courant Linky (A)", "yellow", [{"id": "unit", "value": "amp"}]),
                            color("Tension Linky (V)", "green", [{"id": "unit", "value": "volt"}, {"id": "custom.axisPlacement", "value": "hidden"}]),
                            color("Puissance apparente (VA)", "red", [{"id": "unit", "value": "voltamp"}, {"id": "custom.axisPlacement", "value": "right"}])]),
      0, y, 12, 8)
place(timeseries("Linky - Puissance de référence souscrite (kVA)", "PREF : 6 kVA, base de la limite DLB (33 A). Une valeur absente > 300 s déclenche le fallback.",
                 [query("A", flux_union([("linky_puissance_de_r__f__rence_souscrite", "PREF (kVA)", "last")]))],
                 unit="none", legend=("last",), custom={"showPoints": "auto", "pointSize": 4, "spanNulls": True, "fillOpacity": 0}), 12, y, 12, 8)
y += 8

# ---------------------------------------------------------------- 4. panneaux Acrel d'origine
P = [("acrel_puissance_active_l1", "L1 (Rangée 1)"), ("acrel_puissance_active_l2", "L2 (Voiture EV)"),
     ("acrel_puissance_active_l3", "L3 (Rangée 2)"), ("acrel_puissance_active_totale", "Totale")]
place(timeseries("Puissance Active par phase (kW)", "L1=Rangée 1, L2=Voiture EV, L3=Rangée 2, plus totale. Source: Acrel via Waveshare.",
                 [query("A", flux_union(P))], unit="kwatt",
                 overrides=[color("L2 (Voiture EV)", "orange"), color("Totale", "green", [{"id": "custom.lineWidth", "value": 3}])]), 0, y, 24, 8)
y += 8
place(timeseries("Courant par phase (A)", "Courant par phase: L1=Rangée 1, L2=Voiture EV, L3=Rangée 2. Source: Acrel via Waveshare.",
                 [query("A", flux_union([("acrel_courant_l1__rang__e_1_", "L1 (Rangée 1)"), ("acrel_courant_l2__voiture_ev_", "L2 (Voiture EV)"),
                                         ("acrel_courant_l3__rang__e_2_", "L3 (Rangée 2)")]))], unit="amp",
                 overrides=[color("L2 (Voiture EV)", "orange")]), 0, y, 24, 8)
y += 8
place(timeseries("Tension par phase (V)", "Tension L1/L2/L3. Seuils EN 50160: 207V min, 246V max (normal), 253V critique.",
                 [query("A", flux_union([("acrel_tension_l1", "Tension L1"), ("acrel_tension_l2", "Tension L2"), ("acrel_tension_l3", "Tension L3")]))],
                 unit="volt", legend=("mean", "min", "max"),
                 steps=[{"value": 0, "color": "red"}, {"value": 207, "color": "green"}, {"value": 246, "color": "orange"}, {"value": 253, "color": "red"}]),
      0, y, 12, 8)
place(timeseries("Énergie cumulée (kWh)", "Compteurs d'énergie importée: total + par phase (L1=Rangée 1, L2=Voiture EV, L3=Rangée 2).",
                 [query("A", flux_union([("acrel_energie_import__e_l1", "L1 (Rangée 1)"), ("acrel_energie_import__e_l2__ev_", "L2 (Voiture EV)"),
                                         ("acrel_energie_import__e_l3", "L3 (Rangée 2)"), ("acrel_energie_active_import__e", "Total importé")]))],
                 unit="kilowatthour", legend=("last",),
                 overrides=[color("L2 (Voiture EV)", "orange"), color("Total importé", "green", [{"id": "custom.lineWidth", "value": 3}])]), 12, y, 12, 8)
y += 8
place(timeseries("Facteur de puissance (cos φ) & Fréquence (Hz)", "PF total + PF par phase (axe gauche) et fréquence réseau (axe droit). PF idéal ≈ 1.0.",
                 [query("A", flux_union([("acrel_facteur_de_puissance_totale", "PF total"), ("acrel_facteur_de_puissance_l1", "PF L1"),
                                         ("acrel_facteur_de_puissance_l2", "PF L2"), ("acrel_facteur_de_puissance_l3", "PF L3"),
                                         ("acrel_fr__quence", "Fréquence (Hz)")]))],
                 legend=("mean", "min"), legend_mode="list", custom={"fillOpacity": 0, "lineInterpolation": "smooth"},
                 overrides=[color("PF total", "green", [{"id": "custom.lineWidth", "value": 3}]),
                            color("Fréquence (Hz)", "purple", [{"id": "unit", "value": "hertz"}, {"id": "custom.axisPlacement", "value": "right"},
                                                                {"id": "custom.axisLabel", "value": "Hz"}])]), 0, y, 24, 8)
y += 8

# ---------------------------------------------------------------- 5. BMW
place(timeseries("BMW - Batterie EV : SoC affiché et estimé (%)",
                 "SoC affiché BMW (événementiel : début et fin de trajet, parfois pendant la charge) et SoC estimé par l'énergie Acrel L2 x 0,86 / capacité depuis le dernier SoC "
                 "(recalé à chaque SoC reçu, plafonné à 100 %). Rendement 0,86 mesuré sur 5 charges (oct. 2026).",
                 [query("A", BMW_SOC), query("B", SOC_SERIES)], unit="percent", minv=0, maxv=100, legend=("last", "min", "max"),
                 steps=[{"value": 0, "color": "red"}, {"value": 20, "color": "orange"}, {"value": 50, "color": "green"}],
                 custom={"fillOpacity": 0, "showPoints": "auto", "pointSize": 4, "spanNulls": True},
                 overrides=[color("SoC BMW (affiché)", "blue"), color("SoC estimé (Acrel)", "orange", [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 8]}}])]),
      0, y, 12, 8)
place(timeseries("BMW - Kilométrage (km)", "Odomètre du véhicule, rapporté par BMW CarData.",
                 [query("A", 'from(bucket: "buckbuck")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
                             '  |> filter(fn: (r) => r._measurement == "bmw" and r.property == "vehicle.vehicle.travelledDistance")\n'
                             '  |> keep(columns: ["_time", "_value"])\n'
                             '  |> map(fn: (r) => ({ r with topic: "Odomètre" }))\n  |> group(columns: ["topic"])\n'
                             '  |> aggregateWindow(every: v.windowPeriod, column: "_value", fn: last, createEmpty: false)')],
                 unit="lengthkm", legend=("last",), custom={"fillOpacity": 10, "showPoints": "auto", "pointSize": 4, "spanNulls": True},
                 overrides=[color("Odomètre", "purple")]), 12, y, 12, 8)
y += 8
place(stat("Consommation réelle EV (kWh / 100km)",
           "Énergie livrée à la voiture (Acrel L2) divisée par la distance parcourue (odomètre BMW) sur la période. Inclut les pertes de charge (rendement ~0,86) : "
           "c'est la consommation au compteur, pas celle du véhicule. Choisir une période couvrant des cycles complets de charge/roulage.",
           'distance = from(bucket: "buckbuck")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
           '  |> filter(fn: (r) => r._measurement == "bmw" and r.property == "vehicle.vehicle.travelledDistance")\n'
           '  |> spread()\n  |> set(key: "metric", value: "distance_km")\n\n'
           'energy = from(bucket: "buckbuck")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
           '  |> filter(fn: (r) => r._measurement == "acrel" and r.topic == "passerelle-energie-waveshare/sensor/acrel_energie_import__e_l2__ev_/state")\n'
           '  |> spread()\n  |> set(key: "metric", value: "energy_kwh")\n\n'
           'union(tables: [distance, energy])\n  |> pivot(rowKey: ["_start"], columnKey: ["metric"], valueColumn: "_value")\n'
           '  |> map(fn: (r) => ({\n      _time: r._stop,\n'
           '      _value: if exists r.distance_km and r.distance_km > 0.0 then r.energy_kwh / r.distance_km * 100.0 else 0.0\n    }))\n'
           '  |> keep(columns: ["_time", "_value"])',
           unit="none", decimals=1, steps=[{"value": 0, "color": "green"}, {"value": 20, "color": "orange"}, {"value": 25, "color": "red"}]), 0, y, 24, 5)
y += 5

dashboard = {
    "apiVersion": "dashboard.grafana.app/v2", "kind": "Dashboard",
    "metadata": {"name": "acrel-per-phase-bmw", "namespace": "stacks-1697085", "uid": "cda5dd9f-9b8f-4ebc-a2a4-bc33f516a13f",
                 "resourceVersion": "2086907949029208064", "generation": 1, "creationTimestamp": "2026-08-10T20:10:02Z",
                 "labels": {"grafana.app/deprecatedInternalID": "1741321414606848"},
                 "annotations": {"grafana.app/createdBy": "user:dfpl427v0j8xsb", "grafana.app/saved-from-ui": "Grafana Cloud"}},
    "spec": {
        "annotations": [{"kind": "AnnotationQuery", "spec": {"query": {"kind": "DataQuery", "group": "grafana", "version": "v0",
                         "datasource": {"name": "-- Grafana --"}, "spec": {}}, "enable": True, "hide": True,
                         "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "builtIn": True}}],
        "cursorSync": "Crosshair", "editable": True, "elements": elements,
        "layout": {"kind": "GridLayout", "spec": {"items": items}},
        "links": [], "liveNow": False, "preload": False,
        "tags": ["acrel", "waveshare", "dlb", "etek", "linky", "bmw", "ev"],
        "timeSettings": {"timezone": "browser", "from": "now-6h", "to": "now", "autoRefresh": "30s",
                         "autoRefreshIntervals": ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h", "2h", "1d"],
                         "hideTimepicker": False, "fiscalYearStartMonth": 0},
        "title": "Tableau Élec bmw", "variables": [], "preferences": {"layout": {"kind": "GridLayout", "spec": {"items": []}}}}}

print(json.dumps(dashboard, indent=2, ensure_ascii=False))
