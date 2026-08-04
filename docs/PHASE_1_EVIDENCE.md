# Phase 1 evidence — flag evaluation engine

**Exit criterion:** `uv run pytest tests/phase1 -q` exits 0. **Met.**

```
127 passed, 22 skipped        # default: no services running
149 passed                    # with AIFLAGS_TEST_POSTGRES_DSN set
```

The 22 skips are the PostgreSQL half of the repository contract, which runs only
when a database is reachable. They are the same assertions as the in-memory half,
not a weaker set.

## Guide requirements

| Guide requirement (Phase 1) | Where | Evidence |
|---|---|---|
| Flag schema: rollout percentage, quality threshold, rollback trigger, baseline config, experimental config | `core/models.py`, `migrations/001_flags.sql` | Round-trip tests for targeting, policy, and plan in `test_repository.py` |
| `flag_client.evaluate(flag_name, user_context)` | `sdk.py`, `core/evaluation.py` | `test_sdk.py`, `test_evaluation.py` |
| Consistent user assignment via hashing | `core/bucketing.py` | Frozen SHA-256 vectors; stickiness across snapshot versions |
| Percentage-based rollout | `core/evaluation.py` | Monotonic-ramp property test; half-open boundary test |
| Local caching of flag configuration | `sdk.py` | `evaluate` asserted to make zero fetches |
| Graceful degradation when the service is unreachable | `sdk.py` | Outage, stale-snapshot, and out-of-order-version tests |
| Targeting: segment, geography, request metadata, allow/blocklist | `core/targeting.py` | Full precedence table, 12 tests |
| Flag CRUD API | `api/app.py` | `test_api.py` |
| `POST /flags/{id}/rollout`, `/pause`, `/rollback` | `api/app.py` | Percentage, pause-holds-percentage, rollback-zeroes tests |
| All changes logged with actor and reason | `store/base.py`, `migrations/001_flags.sql` | Attribution required by signature; `CHECK` constraints in SQL; audit-completeness tests |

## Verified against a real database

The repository contract runs against PostgreSQL 16 as well as the in-memory
store — the same test body, so the two cannot drift:

```bash
docker run -d --name aiflags-test-pg -e POSTGRES_PASSWORD=aiflags \
  -e POSTGRES_DB=aiflags -p 127.0.0.1:55432:5432 postgres:16
AIFLAGS_TEST_POSTGRES_DSN="postgresql://postgres:aiflags@127.0.0.1:55432/aiflags" \
  uv run pytest tests/phase1 -q          # 149 passed
```

## Live API smoke test

Served with uvicorn against that database, exercising the operational path:

```
POST /flags                    -> {"snapshot_version": 4, "flag_key": "smoke_line"}
POST /flags/smoke_line/rollout -> {"snapshot_version": 5}
POST /flags/smoke_line/rollback-> {"snapshot_version": 6}
GET  /snapshot                 -> version 6 | status rolled_back | pct 0.0
GET  /audit?flag_key=smoke_line
       create_flag            wycliffe   "smoke test"
       set_rollout_percentage controller "stage 1 passed"
       rollback               controller "p10 below 3.0"   previous_percentage: 25.0
```

The version starting at 4 rather than 1 is itself evidence: the counter had
survived the earlier contract-test run in the same database, confirming it is
durable and monotonic across processes rather than per-connection state.

## Notable behaviour asserted

- **Stickiness.** A subject's variant is unchanged when an unrelated flag is
  edited and the snapshot republished.
- **Monotonic ramp.** Raising the percentage never returns a subject to
  baseline. Without this, quality windows fill with variant churn rather than
  quality signal.
- **Fail-safe evaluation.** Missing snapshot, unknown flag, stale snapshot,
  unreachable service, and out-of-order snapshot delivery all serve baseline. No
  input causes a ramp-up.
- **Rollback beats everything.** A rolled-back flag serves baseline even at 100%
  and even for an allowlisted subject.
- **Atomic rollback.** Status and percentage change in one transaction producing
  one audit entry, recording the percentage reverted from.
- **Unattributed mutation is impossible.** `actor` and `reason` are required
  keyword arguments on every mutating repository method, with `CHECK` constraints
  in the schema behind them.

## Not done in this phase

The Redis-published snapshot path. The SDK reads through a `SnapshotSource`
protocol and the API exposes `/snapshot`; the Redis implementation of that
protocol lands with Phase 2, where Redis is also carrying the rolling counters.
