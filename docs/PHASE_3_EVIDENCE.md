# Phase 3 evidence — gradual rollout automation

**Exit criterion:** `uv run pytest tests/phase3 -q` exits 0, and a `FakeClock`
test drives the full 1%→100% schedule without touching wall-clock time. **Met.**

```
81 passed                     # tests/phase3
378 passed                    # whole suite with PostgreSQL and Redis reachable
```

`AdvanceTests::test_the_full_schedule_runs_to_completion` runs the guide's ramp —
1%/2h → 5%/6h → 100%/24h — end to end in milliseconds, asserting
`[ADVANCE, ADVANCE, COMPLETE]` and a final state of `fully_on` at 100%. There is
no demo-mode branch anywhere in the controller; the clock is simply injected.

## Guide requirements

| Guide requirement (Phase 3) | Where | Evidence |
|---|---|---|
| Staged rollout schedules with dwell times | `core/models.py`, `core/decision.py` | Full-schedule test; per-stage dwell tests |
| Quality checked at each stage boundary | `workers/controller.py` | Advance/hold/pause/rollback tests |
| Auto-advance when quality is above threshold | `core/decision.py` | `test_a_healthy_stage_advances_after_its_dwell_time` |
| Pause and alert when quality dips | `workers/controller.py` | `test_a_worse_variant_pauses_instead_of_advancing` |
| Canary analysis against baseline | `core/canary.py` | 22 canary tests |
| Statistical testing (as Project 9) | `core/canary.py` | Welch / Mann-Whitney with Shapiro selection |
| Advance only when statistically no worse | `core/decision.py` | Inconclusive and absent canary both hold |
| Shadow mode | `core/evaluation.py`, `workers/controller.py` | Shadow served baseline; shadow samples cannot advance |

## Behaviour asserted

- **The dwell clock starts when the controller first sees a flag.** A flag
  created while the controller was down does not arrive pre-aged and instantly
  eligible to ramp. Conservative by design, and now asserted explicitly.
- **Advancing resets the stage clock.** Otherwise the next stage would mature
  immediately off the previous stage's timestamp, collapsing a staged rollout
  into a single jump.
- **Rollback beats a matured stage.** A stage ready to advance still rolls back
  when quality has gone, because the rollback check runs first.
- **Every decision is recorded, including holds.** "Why did this rollout sit at
  1% for six hours" is unanswerable from a log of changes only.
- **A blind judge rolls back.** Thirty unscored samples produce a rollback, not
  a quiet advance.
- **Shadow samples never advance a rollout.** Sixty perfect shadow scores plus a
  matured dwell still holds, because shadow output was never shown to anyone.
- **No baseline traffic holds.** At 1% there may be no comparison to make; the
  canary returns inconclusive and the controller waits.
- **Peripheral failures cannot undo a safety action.** A rollback still completes
  when Slack is unreachable and when the Redis publish fails. The change is
  durable in PostgreSQL before either is attempted.
- **One broken flag does not stop the others.** A flag whose state cannot be read
  is logged and skipped; other flags — including ones needing a rollback — are
  still evaluated in the same tick.
- **The cooldown binds only when it outlasts the dwell.** With the default plan
  the 1-hour cooldown expires before the 2-hour dwell and can never be the
  binding constraint; the tests use a longer cooldown to exercise the case an
  operator resuming a rolled-back flag would actually hit.

## Design note

The controller holds no policy. Every rule about when to advance, hold, pause, or
roll back lives in `decide()`, which is pure. That is why the policy is a table
test rather than something you must run a rollout to observe, and why this
module's tests are about I/O — gathering the right evidence, applying the verdict
faithfully, and managing the stage clock.

## Not done in this phase

The rollout dashboard, which the guide lists under Phase 3 item 3. It is built in
Phase 4 alongside the flag management UI and the analytics view, since both read
the same data and splitting them would mean building the query layer twice.
