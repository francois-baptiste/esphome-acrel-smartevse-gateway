#!/bin/bash
# Run a Flux query against the local InfluxDB (org kimsufi, bucket buckbuck) and print CSV.
#   tools/flux.sh query.flux        # from a file
#   tools/flux.sh - < query.flux    # from stdin
# The token is read from Telegraf's config on the Windows side. That file has CRLF line endings:
# a trailing \r in the token makes InfluxDB answer "400 Bad Request", hence the tr below.
set -euo pipefail
TELEGRAF_CONF="${TELEGRAF_CONF:-/mnt/c/Program Files/Telegraf/telegraf.conf}"
TOKEN=$(grep -m1 '^  token' "$TELEGRAF_CONF" | tr -d '\r' | sed 's/.*"\(.*\)"/\1/' | tr -d '\n')
curl -s -X POST "http://localhost:8086/api/v2/query?org=kimsufi" \
  -H "Authorization: Token $TOKEN" \
  -H "Accept: application/csv" \
  -H "Content-Type: application/vnd.flux" \
  --data-binary @"$1" | tr -d '\r'
