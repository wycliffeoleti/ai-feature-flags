#!/usr/bin/env bash
# Full-stack acceptance without Docker.
#
# Runs the same assertions as scripts/acceptance.sh against a locally launched
# API backed by real PostgreSQL and Redis. Use this when the container image
# cannot be built — it exercises identical code paths and the same real
# services; only the containerisation is out of scope.
#
# Requires PostgreSQL and Redis reachable at the URLs below.
set -euo pipefail

cd "$(dirname "$0")/.."

DSN="${AIFLAGS_POSTGRES_DSN:-postgresql://postgres:aiflags@127.0.0.1:55432/aiflags}"
REDIS="${AIFLAGS_REDIS_URL:-redis://127.0.0.1:56379/0}"
PORT="${AIFLAGS_PORT:-8123}"
API="http://127.0.0.1:${PORT}"

cleanup() {
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
  wait "${API_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT

echo "--- resetting the database"
.venv/bin/python - "$DSN" <<'PY'
import sys, psycopg
with psycopg.connect(sys.argv[1]) as conn, conn.cursor() as cur:
    cur.execute("""
        DO $$ BEGIN
          IF to_regclass('public.flags') IS NOT NULL THEN
            TRUNCATE flags, audit_events RESTART IDENTITY;
            UPDATE snapshot_version SET version = 0;
          END IF;
          IF to_regclass('public.quality_samples') IS NOT NULL THEN
            TRUNCATE quality_samples, rollout_state, controller_decisions
              RESTART IDENTITY;
          END IF;
        END $$;
    """)
    conn.commit()
print("    database reset")
PY

echo "--- starting the API against PostgreSQL and Redis"
AIFLAGS_POSTGRES_DSN="$DSN" AIFLAGS_REDIS_URL="$REDIS" \
  .venv/bin/python -m uvicorn aiflags.api.main:app \
  --host 127.0.0.1 --port "$PORT" --log-level warning &
API_PID=$!

for _ in $(seq 1 40); do
  curl -sf "$API/health" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf "$API/health" >/dev/null || { echo "FAIL: API never became healthy"; exit 1; }
echo "    API healthy on $API"

echo "--- running the offline rollout scenario"
PYTHONPATH=. .venv/bin/python -m aiflags.demo.scenario

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
curl -sf -X POST "$API/flags/acceptance_flag/rollout" -H 'content-type: application/json' \
  -d '{"actor": "acceptance", "reason": "ramp to 25%", "percentage": 25}' >/dev/null
curl -sf -X POST "$API/flags/acceptance_flag/rollback" -H 'content-type: application/json' \
  -d '{"actor": "acceptance", "reason": "simulated quality regression"}' >/dev/null

echo "--- asserting the end state"
python3 - "$(curl -sf "$API/flags/acceptance_flag")" <<'PY'
import json, sys
flag = json.loads(sys.argv[1])
assert flag["status"] == "rolled_back", f"status was {flag['status']}"
assert flag["rollout_percentage"] == 0.0, f"percentage was {flag['rollout_percentage']}"
print("    flag is rolled_back at 0%")
PY

python3 - "$(curl -sf "$API/audit?flag_key=acceptance_flag")" <<'PY'
import json, sys
events = json.loads(sys.argv[1])
actions = [e["action"] for e in events]
assert actions == ["create_flag", "set_rollout_percentage", "rollback"], actions
assert all(e["actor"] and e["reason"] for e in events), "unattributed audit entry"
assert events[-1]["detail"]["previous_percentage"] == 25.0, events[-1]["detail"]
print(f"    audit trail complete: {' -> '.join(actions)}")
print("    rollback reverted from 25.0%")
PY

echo "--- checking the data plane and dashboard"
python3 - "$(curl -sf "$API/snapshot")" <<'PY'
import json, sys
snapshot = json.loads(sys.argv[1])
assert snapshot["version"] > 0, snapshot["version"]
flag = snapshot["flags"]["acceptance_flag"]
assert flag["status"] == "rolled_back", flag["status"]
print(f"    snapshot v{snapshot['version']} serves the rolled-back flag")
PY

curl -sf "$API/dashboard" | grep -q "acceptance_flag" || {
  echo "FAIL: dashboard did not list the flag"; exit 1; }
curl -sf "$API/dashboard/analytics" >/dev/null
curl -sf "$API/dashboard/flags/acceptance_flag" >/dev/null
echo "    dashboard, detail and analytics served"

echo
echo "========================================================================"
echo "PASS  local full-stack acceptance"
echo "      scenario, live API, audit trail, snapshot and dashboard verified"
echo "      against real PostgreSQL and Redis (no container image built)"
echo "========================================================================"
