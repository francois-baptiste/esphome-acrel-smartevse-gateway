// Estimation du SoC de la BMW i3 côté Influx (rien dans l'ESP : le DLB reste 100 % local).
// Usage : tools/flux.sh tools/soc_estime.flux
//
// SoC estimé = dernier SoC affiché remonté par BMW + énergie comptée par l'Acrel L2 depuis cet instant
//              x rendement de charge / capacité utile (maxEnergy BMW), plafonné à 100 %.
// Backtest 6-24 oct 2026 : BMW envoie un SoC à chaque début et fin de trajet (ainsi qu'environ toutes les 2-3 min pendant
// certaines charges), donc le dernier SoC est en général celui d'après le dernier trajet. Rendement mesuré (5 charges
// sans trajet et sans saturation à 100 %) : 0.82-0.89, groupé 0.864 ; mais une charge complète jusqu'à 100 % (59 -> 100 %, 24/10)
// demande 19.0 kWh au compteur, soit 0.82 (équilibrage de fin de charge) : 0.83 retenu, backtest ETA 100 % à +3 min.
// Limites : le SoC affiché plafonne à 100 % avant la fin de la charge (équilibrage) ; si la voiture a roulé sans SoC
// remonté, le trajet n'est pas déduit (estimation trop haute).
import "array"
import "date"

rendement = 0.83
soc_property = /stateOfCharge\.displayed/
compteur = /sensor\/acrel_energie_import__e_l2__ev_/

bmw_last = (p) =>
  from(bucket: "buckbuck")
    |> range(start: -7d)
    |> filter(fn: (r) => r._measurement == "bmw" and r.property =~ p)
    |> last()
    |> findRecord(fn: (key) => true, idx: 0)

soc = bmw_last(p: soc_property)
cap = bmw_last(p: /batteryManagement\.maxEnergy/)

compteur_a = (stop) =>
  from(bucket: "buckbuck")
    |> range(start: -30d, stop: stop)
    |> filter(fn: (r) => r._measurement == "acrel" and r.topic =~ compteur)
    |> last()
    |> findRecord(fn: (key) => true, idx: 0)

e0 = compteur_a(stop: date.add(d: 1s, to: soc._time))
e1 = compteur_a(stop: now())

kwh = e1._value - e0._value
gain = kwh * rendement / cap._value * 100.0
estime = if soc._value + gain > 100.0 then 100.0 else soc._value + gain

array.from(rows: [{
  soc_bmw_pct: soc._value,
  soc_bmw_heure: soc._time,
  energie_depuis_kwh: kwh,
  capacite_kwh: cap._value,
  rendement: rendement,
  gain_pct: gain,
  soc_estime_pct: estime,
  compteur_heure: e1._time,
}])
