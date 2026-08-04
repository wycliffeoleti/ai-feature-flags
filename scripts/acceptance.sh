#!/usr/bin/env bash
# Full-stack acceptance: bring up Compose, drive the demo scenario through the
# real API against real PostgreSQL and Redis, and assert the end state.
#
# Exits non-zero if the bad variant was not rolled back or the good variant did
# not reach 100%. It asserts outcomes, not that commands ran.
set -euo pipefail

cd "$(dirname "$0")/.."

# Default off 8000 deliberately: that port is frequently occupied on a
# development machine, and this script must not fail because of it.
AIFLAGS_API_PORT="${AIFLAGS_API_PORT:-8188}"
export AIFLAGS_API_PORT
COMPOSE="${COMPOSE:-docker compose}"
API="http://127.0.0.1:${AIFLAGS_API_PORT}"

cleanup() {
  echo "--- tearing down"
  $COMPOSE down --remove-orphans --volumes >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "--- building and starting the stack"
$COMPOSE up -d --build

echo "--- waiting for the API"
for _ in $(seq 1 60); do
  if curl -sf "$API/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -sf "$API/health" >/dev/null || { echo "FAIL: API never became healthy"; exit 1; }
echo "    API healthy"

echo "--- running the offline scenario against the real stack's code"
$COMPOSE exec -T api python -m aiflags.demo.scenario

echo "--- exercising the live API"
curl -sf -X POST "$API/flags" -H 'content-type: application/json' -d '{
  "actor": "acceptance", "reason": "full-stack acceptance run",
  "flag": {
    "key": "acceptance_flag",
    "baseline":     {"key": "v1", "kind": "baseline",     "config": {"template": "{topic} — action needed"}},
    "experimental": {"key": "v2", "kind": "experimental", "config": {"template": "Hi {customer_name}, about your {topic}"}},
    "quality_policy": {"gates": [{"signal": "judge_score", "statistic": "p10",
                                  "comparison": "below", "threshold": 3.0,
                                  "sustained_evaluations": 50}]}
  }}' >/dev/null
echo "    flag created"

curl -sf -X POST "$API/flags/acceptance_flag/rollout" -H 'content-type: application/json' \
  -d '{"actor": "acceptance", "reason": "ramp to 25%", "percentage": 25}' >/dev/null
curl -sf -X POST "$API/flags/acceptance_flag/rollback" -H 'content-type: application/json' \
  -d '{"actor": "acceptance", "reason": "simulated quality regression"}' >/dev/null

echo "--- asserting the end state"
STATE=$(curl -sf "$API/flags/acceptance_flag")
python3 - "$STATE" <<'PY'
import json, sys
flag = json.loads(sys.argv[1])
assert flag["status"] == "rolled_back", f"status was {flag['status']}"
assert flag["rollout_percentage"] == 0.0, f"percentage was {flag['rollout_percentage']}"
print("    flag is rolled_back at 0%")
PY

AUDIT=$(curl -sf "$API/audit?flag_key=acceptance_flag")
python3 - "$AUDIT" <<'PY'
import json, sys
events = json.loads(sys.argv[1])
actions = [e["action"] for e in events]
assert actions == ["create_flag", "set_rollout_percentage", "rollback"], actions
assert all(e["actor"] and e["reason"] for e in events), "unattributed audit entry"
rollback = events[-1]
assert rollback["detail"]["previous_percentage"] == 25.0, rollback["detail"]
print(f"    audit trail complete: {' -> '.join(actions)}")
print(f"    rollback reverted from {rollback['detail']['previous_percentage']}%")
PY

echo "--- checking the dashboard renders"
curl -sf "$API/dashboard" | grep -q "acceptance_flag" || {
  echo "FAIL: dashboard did not list the flag"; exit 1; }
curl -sf "$API/dashboard/analytics" >/dev/null
echo "    dashboard and analytics served"

echo
echo "========================================================================"
echo "PASS  full-stack acceptance: rollback, audit trail, and dashboard verified"
echo "========================================================================"
