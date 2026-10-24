from(bucket: "buckbuck")
  |> range(start: 2026-10-06T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "bmw" and r.property =~ /stateOfCharge\.displayed|travelledDistance|maxEnergy|charging\.(acAmpere|status|level)|isCharging|chargingStatus/)
  |> keep(columns: ["_time", "_value", "property"])
