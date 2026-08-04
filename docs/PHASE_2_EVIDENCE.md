# Phase 2 evidence — quality monitoring layer

**Exit criterion:** `uv run pytest tests/phase2 -q` exits 0, and the decision
table covers every `Action` variant. **Met.**

```
113 passed, 35 skipped        # tests/phase2, no services running
347 passed                    # whole suite with PostgreSQL and Redis reachable
```

Decision coverage is asserted programmatically, not by inspection —
`test_every_action_variant_is_reachable` produces all five `Action` values from
real `decide()` calls and compares the set against `set(Action)`. Adding an
unreachable action fails that test.

## Guide requirements

| Guide requirement (Phase 2) | Where | Evidence |
|---|---|---|
| Quality metrics per flag: LLM-as-judge, user feedback, latency, error rate | `core/models.py`, `workers/evaluator.py` | Per-signal tests in `test_evaluator.py` |
| LLM-as-judge scoring 1–5 | `judge/fixture.py`, `judge/ollama.py` | 18 judge tests; fixture default, Ollama opt-in |
| Async evaluation that adds no user latency | `queue.py`, `workers/evaluator.py` | SDK asserted to make no I/O in `record_outcome`; queue sits between |
| Background worker with a message queue | Redis Streams + consumer group | Queue contract run against real Redis |
| Rolling windows: last-100 / 1h / 24h | `core/windows.py` | 22 window tests |
| Mean, stdev, P10, trend | `core/windows.py` | Statistic tests |
| Compare experimental vs baseline continuously | `store/quality.py` | Variant-separated sample queries |
| Automatic rollback on sustained breach | `core/decision.py` | Decision table, 28 tests |
| Rollback sets percentage to 0 | `store/*.rollback()` | Atomic rollback contract tests |
| Slack alert with the quality data | `notify/` | Payload asserted field by field |
| Log the rollback with full context | `migrations/002_quality.sql` | `controller_decisions` with evidence JSONB |
| Cooldown to prevent flapping | `core/decision.py` | Cooldown tests |

## Verified against real services

```bash
docker run -d --name aiflags-test-redis -p 127.0.0.1:56379:6379 \
  redis/redis-stack-server:7.4.0-v8

AIFLAGS_TEST_POSTGRES_DSN="postgresql://postgres:aiflags@127.0.0.1:55432/aiflags" \
AIFLAGS_TEST_REDIS_URL="redis://127.0.0.1:56379/0" \
  uv run pytest -q          # 347 passed
```

Both the quality store and the outcome queue run their contract against the real
service and the in-memory implementation — one test body, so they cannot drift.
The Redis snapshot store is additionally driven **through the SDK**
(`test_the_store_satisfies_the_sdk_snapshot_source_protocol`), proving the
published shape decodes back into a working evaluation rather than merely
round-tripping as JSON.

## Behaviour asserted

- **A judge that fails is visible.** A timeout, a malformed reply, or an
  exception all produce an *unscored* observation. `JudgeVerdict` makes the
  alternative unconstructable: a scored verdict needs a number, an unscored one
  refuses to hold one.
- **A store failure loses nothing.** The batch is left unacknowledged and
  redelivered. Persist-then-acknowledge is asserted directly, because
  acknowledging first would turn a database blip into a permanent gap in the
  quality window — and a gap reads as "no problems observed".
- **A snapshot never moves backwards.** Publishing an older or equal version is
  refused, so two publishers racing cannot reinstate a percentage an operator
  already changed.
- **Consumed work stays pending until acknowledged.** Redis consumer-group
  semantics, mirrored by the in-memory queue so the property is testable without
  Redis.
- **Unattributable outcomes are skipped, not guessed.** An outcome naming a
  variant the flag no longer defines is discarded rather than filed under the
  nearest match, which would corrupt the comparison it feeds.
- **The Ollama judge cannot leave the machine.** Non-loopback endpoints are
  refused in the constructor.
- **Recorded alerts are the real alerts.** Both notifiers build the payload from
  the same `Notification.as_slack_payload`, so what is recorded offline is
  byte-identical to what Slack would receive.

## Not done in this phase

The controller loop that consumes `decide()` and applies its actions — that is
Phase 3, together with staged schedules and canary gating. The decision function
and canary comparison it will call are already complete and tested.
