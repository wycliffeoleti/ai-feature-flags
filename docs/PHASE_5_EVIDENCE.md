# Phase 5 evidence — demo application and stack

**Exit criterion:** `uv run pytest tests/phase5 -q` exits 0, **and**
`scripts/acceptance.sh` brings up Compose, runs the scenario, and exits 0.

**Status: partially met.** The first half passes. The second cannot be run on
this machine — the container image will not build without PyPI egress. See
[`BLOCKED.md`](BLOCKED.md) B1.

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

### Local full-stack acceptance

`scripts/acceptance_local.sh` runs the same assertions as the Compose script
against a locally launched API backed by **real PostgreSQL 16 and Redis Stack**:

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

## What is not verified

The containerisation. `Dockerfile` and `compose.yaml` are written and
syntactically valid but have never been built or run, because `pip install`
cannot reach PyPI from this machine. **Do not claim the Compose stack runs** in
the README, guide matrix, or portfolio material until `scripts/acceptance.sh`
has actually passed.

Vendoring wheels from the local cache to force a build was deliberately not
attempted: it would produce an image that builds only on this machine, which is a
worse artefact than an honest "unverified".

## Two defects found and fixed by these tests

Both were found because the integration tests asserted end state rather than
behaviour in isolation, and in both cases the code was wrong, not the test:

1. **A redundant audit entry.** `COMPLETE` re-wrote the rollout percentage that
   the preceding `ADVANCE` had already set, recording a change that did not
   happen. An audit trail whose entries do not correspond to changes is worth
   less than none. The controller now writes only on an actual difference.

2. **Two timelines.** `InMemoryFlagRepository.snapshot()` stamped
   `published_at` from the wall clock while the SDK measured staleness against
   its injected clock, so staleness was computed across two unrelated timelines.
   The repository now takes a clock, and the demo shares one across every
   component.

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
| docker-compose with PostgreSQL, Redis, API, evaluator, dashboard, demo | Written, **not built** |
| Script running the full rollout demo end to end | Done (`acceptance_local.sh`); Compose variant unverified |
