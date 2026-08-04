# Phase 5 evidence — demo application and stack

**Exit criterion:** `uv run pytest tests/phase5 -q` exits 0, **and**
`scripts/acceptance.sh` brings up Compose, runs the scenario, and exits 0.
**Met.**

```
33 passed                     # tests/phase5
450 passed                    # whole suite with PostgreSQL and Redis reachable
```

## What is verified

### The demo scenario

`uv run python -m aiflags.demo.scenario` runs both lifecycles in about 1.6
seconds on a `FakeClock` — the guide's real schedule (1%/2h → 5%/6h → 25%/24h →
100%) with no stage duration shortened for the demo:

```
[1/2] Rolling out a BROKEN prompt variant
      template: 'Hi {customer_name}, about your {topic}'
      requests served : 6000
      controller      : rollback
      final status    : rolled_back at 0%
      rollback reason : judge_score p10 of 2.5 is below the threshold 3
                        across 50 consecutive evaluations
      alerts recorded : 1

[2/2] Rolling out a GOOD prompt variant
      template: '{topic} — action needed'
      requests served : 30000
      controller      : hold -> advance -> advance -> advance -> complete
      final status    : fully_on at 100%
```

**The failure it demonstrates is a real one.** The experimental template
references `{customer_name}`, which this pipeline never populates. Nothing
raises; the placeholder renders verbatim and ships to users. Every downstream
system reports success. That is exactly the shape of failure a boolean feature
flag cannot detect — the code path works, the output is wrong — and it is the
reason the quality gate exists.

### The four integration tests the guide names

| Guide test | Where |
|---|---|
| Consistent user assignment across evaluations | `ConsistentAssignmentTests` |
| Automatic rollback triggers on quality degradation | `AutomaticRollbackTests` |
| Staged rollout advances correctly on quality thresholds | `StagedAdvanceTests` |
| SDK gracefully handles flag service outages | `SdkOutageTests` |

Plus shadow mode end to end and determinism of the scenario itself.

### Compose acceptance

`scripts/acceptance.sh` builds the image, brings up PostgreSQL, Redis, the API,
the evaluator and the controller, runs the scenario inside the API container, and
asserts the end state through the live HTTP API:

```
--- building and starting the stack
    API healthy
--- running the offline scenario against the real stack's code
      broken variant : rolled_back at 0%
      good variant   : hold -> advance -> advance -> advance -> complete, 100%
--- asserting the end state
    flag is rolled_back at 0%
    audit trail complete: create_flag -> set_rollout_percentage -> rollback
    rollback reverted from 25.0%
    dashboard and analytics served
PASS
```

The API's host port is overridable via `AIFLAGS_API_PORT` (default 8188). 8000 is
frequently already taken on a development machine, and an acceptance run must not
fail because of it — it did, the first time, against an unrelated container.

### Local acceptance (no Docker)

`scripts/acceptance_local.sh` runs the same assertions against a locally launched
API backed by **real PostgreSQL 16 and Redis Stack**, for when containers are not
wanted:

```
    API healthy on http://127.0.0.1:8123
    (scenario runs: broken -> rolled_back, good -> fully_on at 100%)
    flag is rolled_back at 0%
    audit trail complete: create_flag -> set_rollout_percentage -> rollback
    rollback reverted from 25.0%
    snapshot v3 serves the rolled-back flag
    dashboard, detail and analytics served
PASS
```

## A correction

This phase was first recorded as *partially met*, on the grounds that the
container image could not be built without PyPI egress. **That was a
misdiagnosis** — PyPI was reachable throughout. The reachability probe used
`https://pypi.org/simple/`, which is a 40 MB bulk index that cannot return
quickly even on a perfect connection, so every check timed out. See
[`BLOCKED.md`](BLOCKED.md) for the full account.

The image builds, and the Compose acceptance passes.

## Three defects found and fixed by these tests

All three were found because the integration and acceptance runs asserted end
state rather than behaviour in isolation. In each case the code was wrong, not
the test:

1. **A redundant audit entry.** `COMPLETE` re-wrote the rollout percentage that
   the preceding `ADVANCE` had already set, recording a change that did not
   happen. An audit trail whose entries do not correspond to changes is worth
   less than none. The controller now writes only on an actual difference.

2. **Two timelines.** `InMemoryFlagRepository.snapshot()` stamped
   `published_at` from the wall clock while the SDK measured staleness against
   its injected clock, so staleness was computed across two unrelated timelines.
   The repository now takes a clock, and the demo shares one across every
   component.

3. **A hard-coded host port.** The acceptance run failed against an unrelated
   container already holding 8000. The published port is now
   `${AIFLAGS_API_PORT:-8000}` and the script defaults to 8188, because a script
   that fails on a developer's own machine layout is testing the wrong thing.

## Guide requirements

| Guide requirement (Phase 5) | Status |
|---|---|
| Demo app: AI-powered email subject line generator | Done |
| Flag starting at 0% | Done |
| Gradual rollout with quality monitoring | Done |
| Deliberately bad variant detected and auto-rolled back | Done |
| Successful rollout of a good variant to 100% | Done |
| Integration test: consistent user assignment | Done |
| Integration test: automatic rollback on degradation | Done |
| Integration test: staged advance on thresholds | Done |
| Integration test: SDK handles flag service outages | Done |
| docker-compose with PostgreSQL, Redis, API, evaluator, dashboard, demo | Done — builds and passes acceptance |
| Script running the full rollout demo end to end | Done (`acceptance.sh`, plus `acceptance_local.sh` without Docker) |
