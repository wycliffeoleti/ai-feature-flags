# ai-feature-flags

A feature flag system for AI features: percentage-based gradual rollout with
continuous quality monitoring and automatic rollback.

Traditional feature flags assume a binary — the code path works or it doesn't. AI
features fail on a gradient. A prompt change does not throw; it just starts
producing slightly worse answers, and nothing in a conventional flag system
notices. This project treats "is it working" as a measured quantity, ties the
rollout schedule to it, and rolls back before the ramp continues.

> **Scope.** This is a local engineering project built against the BASWE Project
> 12 blueprint. It has no production deployment, no real users, and no external
> traffic. Quality scores come from a deterministic fixture judge by default and
> optionally from a loopback Ollama process. Nothing here calls a paid API.

## Status

Complete. The decision core, SDK, management API, quality evaluator, rollout
controller, operator dashboard, and an end-to-end demo — verified against real
PostgreSQL, Redis, a containerised stack, and a real language model.
**457 tests, all passing.**

The Compose stack builds and passes an end-to-end acceptance run.
Requirement-by-requirement status is in
[`docs/PROJECT_GUIDE_MATRIX.md`](docs/PROJECT_GUIDE_MATRIX.md).

```bash
uv run pytest -q                       # 393 tests, nothing installed but the deps
uv run python -m aiflags.demo.scenario # the full rollout lifecycle, 1.6s
```

The suite is layered — it stays useful with nothing running and gets stricter as
services appear:

| Running | Result |
|---|---|
| Nothing | 393 passed, 64 skipped |
| + PostgreSQL and Redis | 450 passed, 7 skipped |
| + a local Ollama model | **457 passed** |

The extra tests are not a stronger variant of the same checks — the store and
transport contracts run *one test body* against both the in-memory and real
implementations, so the two cannot drift.

## The demo

`uv run python -m aiflags.demo.scenario` runs both lifecycles in 1.6 seconds:

```
[1/2] BROKEN variant   template: "Hi {customer_name}, about your {topic}"
      controller      : rollback
      rollback reason : judge_score p10 of 2.5 is below the threshold 3
                        across 50 consecutive evaluations

[2/2] GOOD variant     template: "{topic} — action needed"
      controller      : hold -> advance -> advance -> advance -> complete
      final status    : fully_on at 100%
```

The failure is a realistic one. The experimental prompt references
`{customer_name}`, which the pipeline never populates, so it renders verbatim and
ships to users — while every downstream system reports success. That is the shape
of AI feature failure a boolean flag cannot see.

Both runs use the guide's real schedule (1% for two hours, 5% for six, and so on)
on an injected clock. No stage duration was shortened for the demo.

A walkthrough for recording it is in [`docs/DEMO_RUNBOOK.md`](docs/DEMO_RUNBOOK.md).

### Running the whole stack

```bash
bash scripts/acceptance.sh             # builds and runs the Compose stack

# or, without containers, against local PostgreSQL and Redis:
docker run -d --name aiflags-pg -e POSTGRES_PASSWORD=aiflags \
  -e POSTGRES_DB=aiflags -p 127.0.0.1:55432:5432 postgres:16
docker run -d --name aiflags-redis \
  -p 127.0.0.1:56379:6379 redis/redis-stack-server:7.4.0-v8
bash scripts/acceptance_local.sh
```

Either script asserts the end state rather than merely running: the bad variant
rolled back, the audit trail complete with the percentage it reverted from, and
the dashboard served.

### Judged by a real model

The default judge is a deterministic rubric, which keeps the pipeline
reproducible. The same rollout has also been driven end to end by **`phi4-mini`
running locally under Ollama** — 520 real inference calls, no paid API:

| Variant | Experimental | Baseline | Outcome |
|---|---|---|---|
| Broken template | mean 2.56, **p10 2.00** | mean 4.03 | rolled back |
| Good template | mean 4.06 | mean 4.04 | reached 100% |

Full numbers in [`docs/OLLAMA_EVIDENCE.md`](docs/OLLAMA_EVIDENCE.md).

One finding worth the space. `phi4-mini` misses roughly one broken output in
four — it is not a reliable judge. The rollback fires anyway, because P10 reads
the bottom decile and the misses only lift the top of the distribution. But the
tolerance is one-directional: a judge that *false-alarms* on good output drags
the baseline's own P10 down and the gate stops discriminating entirely. Measured
on differently-phrased clean text, the same model false-alarms about 19% of the
time, and under that error rate only the mean separates the variants.

So the gate statistic has to be chosen against the judge's measured error
profile, not picked on principle — [D15](docs/DECISIONS.md) has the numbers.

## How it works

**Control plane / data plane split.** The flag service owns Postgres and
publishes an immutable, versioned snapshot to Redis. The SDK polls that snapshot
and evaluates **entirely in-process** — there is no network call on the request
path — then reports outcomes to a queue. A separate controller decides when to
advance or roll back; a separate worker scores quality.

```
operator ──► API ──► Postgres ──► snapshot vN ──► Redis
                                                    │
                                            (SDK polls)
                                                    ▼
application ──► client.evaluate() ──► variant   [no I/O]
            └─► client.record_outcome() ──► queue ──► evaluator ──► judge
                                                          │
                                                          ▼
                                     controller ──► decide() ──► advance
                                                              └─► rollback
```

Only the controller changes what users see, and it is the only place a decision
is made.

### The decision function

`aiflags/core/decision.py` is the centre of the project and is pure — no
database, no clock, no model. Given the rollout state, the plan and policy, the
observed samples, and a canary verdict, it returns one of `ADVANCE`, `HOLD`,
`PAUSE`, `ROLLBACK`, or `COMPLETE` with a reason and the evidence behind it.

Rollback checks run before every advance path, so a stage that is otherwise ready
to advance still rolls back on bad quality. Every branch representing missing or
ambiguous evidence resolves to `HOLD`. An advance needs positive evidence on all
three counts: dwell time satisfied, enough samples, and a canary verdict of
`NO_WORSE`.

The whole policy is therefore a table test rather than something inferred from an
integration run.

### Three things worth arguing about

**A broken judge is not good news.** If the judge times out, the sample is stored
as unscored, not as a zero and not discarded. A high unscored rate is its own
rollback trigger, because otherwise a judge outage reads as "no bad scores
observed" and the rollout ramps to 100% while blind. This is an addition to the
guide, not something it asks for.

**The canary tests non-inferiority, not significance.** Advancing whenever the
experiment is *not significantly worse* rewards ignorance — a small, noisy sample
is never significant, so the rollout would ramp fastest exactly when it knows
least. Instead the gate asks whether a regression larger than a margin can be
ruled out, which makes "we cannot tell yet" a real verdict that holds the
rollout.

**Shadow mode is not free.** The SDK cannot run your AI feature, so shadow mode
returns both the served baseline and the shadow variant and asks the application
to run both. The guide's "3–5 lines of integration" holds for an ordinary rollout
but not for shadow mode, which costs a second inference call.

Reasoning for these and the rest is in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Integration

```python
result = client.evaluate("subject_line", EvaluationContext(subject_key=user_id))
subject = generate(prompt=result.variant.config["prompt"])
client.record_outcome(result, output=subject, latency_ms=elapsed_ms)
```

`evaluate()` does no I/O and never raises. Every failure of the flag service —
unreachable, stale snapshot, unknown flag — resolves to baseline traffic rather
than an exception in your request handler. `record_outcome()` appends to a
bounded buffer and never blocks; network work happens in `refresh()` and
`flush()`, which the host application schedules.

## What this is not

- Not a production deployment, and not evidence of one.
- Not a provider integration. No paid API is called from any code path.
- Not a claim about real users, real traffic, or measured business impact.
- Not a replacement for LaunchDarkly or Flagsmith. It is a focused
  demonstration of the one thing those do not do: tie a rollout schedule to
  measured output quality.
