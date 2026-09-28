#!/usr/bin/env bash
# CPU smoke test for a built image: the gateway enforces auth, SwarmUI answers through it, and the
# image's backend is configured. Usage: tests/smoke/smoke.sh <image> <backend>
set -euo pipefail

IMAGE="$1"
BACKEND="$2"
PORT="${SMOKE_PORT:-17801}"
TOKEN="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)"
NAME="swarmui-smoke-$$"
BASE="http://127.0.0.1:${PORT}"

cleanup() {
    docker logs "$NAME" > smoke-container.log 2>&1 || true
    docker rm -f "$NAME" > /dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "SMOKE FAIL: $*" >&2
    exit 1
}

docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:7801" -e SWARMUI_WORKER_TOKEN="$TOKEN" "$IMAGE" > /dev/null

echo "Waiting for SwarmUI behind the gateway..."
for _ in $(seq 1 180); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' -d '{}' "$BASE/API/GetNewSession" || true)"
    [ "$code" = "200" ] && break
    if ! docker inspect -f '{{.State.Running}}' "$NAME" | grep -q true; then
        fail "container exited during startup"
    fi
    sleep 5
done
[ "$code" = "200" ] || fail "SwarmUI never answered through the gateway (last HTTP $code)"

echo "Checking the gateway refuses unauthenticated and wrong-token requests..."
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -d '{}' "$BASE/API/GetNewSession")"
[ "$code" = "401" ] || fail "no-token request got HTTP $code, expected 401"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Authorization: Bearer wrong' -d '{}' "$BASE/API/GetNewSession")"
[ "$code" = "401" ] || fail "wrong-token request got HTTP $code, expected 401"
code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/Text2Image?worker_token=$TOKEN")"
[ "$code" = "401" ] || fail "URL login is on by default (HTTP $code)"

echo "Checking the configured backend..."
session="$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' \
    "$BASE/API/GetNewSession" | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')"
backends="$(curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"session_id\": \"$session\"}" "$BASE/API/ListBackends")"
expected_type="$([ "$BACKEND" = comfyui ] && echo comfyui_selfstart || echo hartsyinference)"
echo "$backends" | python3 -c "
import sys, json
data = json.load(sys.stdin)
types = [b.get('type') for b in data.values() if isinstance(b, dict)]
assert types == ['$expected_type'], f'expected one $expected_type backend, got {types}'
print('backend ok:', types)
" || fail "backend check failed: $backends"

echo "Checking the token never reached the logs..."
docker logs "$NAME" 2>&1 | grep -q "$TOKEN" && fail "worker token appears in container logs"

echo "Checking SwarmUI listens on loopback only..."
docker exec "$NAME" sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://\$(hostname -i | cut -d' ' -f1):7810/ || true" \
    | grep -qv "^[23]" || fail "SwarmUI's internal port answers on a non-loopback address"

echo "SMOKE PASS ($BACKEND)"
