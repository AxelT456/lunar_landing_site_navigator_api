#!/bin/sh
# Smoke tests against a deployed API. Usage: BASE=http://host:8000/api/v1 scripts/smoke.sh
set -eu

: "${BASE:?set BASE, e.g. http://spaceapps-api-staging:8000/api/v1}"
fail=0

# expect <name> <expected status> <curl args...>
expect() {
  name=$1
  want=$2
  shift 2
  # Truncate first so a failed request never shows the previous response body.
  : > /tmp/smoke.body
  # curl already prints 000 on connection errors; `|| true` keeps set -e from exiting.
  status=$(curl -sS -o /tmp/smoke.body -w '%{http_code}' --max-time 60 "$@" || true)
  if [ "$status" = "$want" ]; then
    echo "PASS  $name ($status)"
  else
    echo "FAIL  $name (got $status, want $want): $(head -c 300 /tmp/smoke.body 2>/dev/null)"
    fail=1
  fi
}

# Retry health while the new container warms up.
i=0
until curl -fsS -o /dev/null --max-time 5 "$BASE/health" || [ "$i" -ge 15 ]; do
  i=$((i + 1))
  sleep 2
done

expect "GET /health" 200 "$BASE/health"
expect "GET /sites" 200 "$BASE/sites"
count=$(grep -o '"count":[0-9]*' /tmp/smoke.body | head -1 | cut -d: -f2)
if [ "${count:-0}" -ge 10 ]; then
  echo "PASS  /sites count=$count"
else
  echo "FAIL  /sites count=${count:-?} (want >= 10)"
  fail=1
fi
expect "POST /evaluate Shackleton" 200 -X POST "$BASE/evaluate" \
  -H 'content-type: application/json' \
  -d '{"lat":-89.66,"lon":222.7,"start_utc":"2026-12-01T00:00:00Z"}'
expect "GET /terrain/patch lat=95 rejected" 422 "$BASE/terrain/patch?lat=95&lon=10"
echo "      body: $(head -c 200 /tmp/smoke.body)"

exit "$fail"
