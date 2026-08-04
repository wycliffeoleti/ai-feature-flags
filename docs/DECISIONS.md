# Decisions

Design calls made during the build, with the reasoning. Where a decision
interprets the BASWE Project 12 guide rather than implementing it literally, that
is stated so the interpretation can be challenged.

---

## D1 — The pure core depends on the standard library only

`aiflags/core/` and `aiflags/sdk.py` use stdlib dataclasses, not Pydantic.

The SDK is embedded in applications' own request paths. A core that imports
Pydantic forces every adopting application into a compatible Pydantic version,
which is a real adoption cost for a library whose whole pitch is "three lines to
integrate". Keeping the core on dataclasses also means it runs with no install
step, so the decision logic is testable anywhere.

Pydantic still does the job it is good at — parsing untrusted JSON — but at the
API boundary in `aiflags/api/`, where the input actually is untrusted.

**Trade-off:** validation in the core is hand-written `__post_init__` checks
rather than declarative. That is more code, but it is confined to one module.

---

## D2 — `bucket < percentage`, with the flag key in the hash

Assignment is `sha256(flag_key ␟ salt ␟ subject_key)` mapped to `[0, 1)`, and a
subject is in the experiment when their bucket is below the rollout fraction.

Two properties fall out, and both are asserted directly in
`tests/phase1/test_evaluation.py`:

- **Stickiness** — a subject's bucket never moves, so republishing a snapshot
  does not reshuffle who sees what.
- **Monotonic ramp** — raising the percentage only ever adds subjects. If a
  ramp-up could return someone to baseline, the quality windows would fill with
  users switching variants mid-session, and the canary would be measuring churn
  rather than quality.

Including the flag key means a subject unlucky in the first 1% of one flag is not
systematically in the first 1% of every flag.

---

## D3 — Rollback checks run before every advance path

In `decide()`, quality gates are evaluated before dwell time, sample count, and
the canary. A stage whose dwell time has elapsed still rolls back if quality has
gone.

Everything that represents missing or ambiguous evidence resolves to `HOLD`: no
samples, thin samples, an inconclusive canary, an absent canary. An advance
requires positive evidence on all three counts.

The asymmetry is deliberate. Failing to advance costs a slower rollout; failing
to roll back costs users a broken feature.

---

## D4 — Unscored samples get their own gate

**Not in the guide.** When the judge times out or errors, the sample carries no
quality information. There are two obvious things to do with it and both are
wrong: averaging it in as a zero invents a regression that did not happen, and
dropping it silently lets a rollout ramp to 100% while the system is blind.

Instead an unscored sample is excluded from the quality statistics but counted in
`WindowStats.unscored_rate`, which a flag can gate on directly. A broken judge
now rolls the flag back rather than reading as "no bad scores observed".

`WindowStats.is_blind` exists for the same reason: a window with zero scored
samples must not be mistaken for a healthy one.

---

## D5 — "Sustained" means a trailing window

The guide says a rollback fires when, for example, "P10 is below 3.0 for more
than 50 consecutive evaluations". That admits two readings: the statistic
computed over the trailing 50 evaluations, or the statistic recomputed at each of
50 successive points.

This implementation uses the trailing window. It is the standard reading, and the
alternative is O(n²) on every controller tick while answering a subtly different
question.

A gate does not breach at all until it has a full window — a shorter run is a dip,
and absorbing dips is what the sustained count is for.

**Consequence worth knowing:** a P10 gate is sharp. Twenty percent of outputs
rated 1/5 drags the tenth percentile to 1.0 and fires the gate even though the
mean is still 4.2. That is intended — a mean-based gate would let one user in
five get a broken answer indefinitely — but P10 thresholds must be set against
the tail you are willing to serve, not the average. Asserted in
`tests/phase3/test_decision.py::test_a_p10_gate_fires_once_a_tenth_of_traffic_goes_bad`.

---

## D6 — The canary tests non-inferiority, not significance

**Interpretation of the guide.** The guide says a rollout "only advances when the
experimental variant is statistically no worse than baseline". The obvious
implementation — run a t-test, advance unless the experiment is significantly
worse — has a perverse property: a small, noisy sample is never significant, so
the rollout ramps fastest exactly when it knows least. The test's failure to
detect a regression gets read as evidence there is none.

`canary.compare()` instead asks whether a regression larger than `margin` can be
*ruled out* at the configured confidence:

| Interval on `experimental − baseline` | Verdict | Controller |
|---|---|---|
| Entirely above `−margin` | `NO_WORSE` | advance |
| Entirely below `−margin` | `WORSE` | pause |
| Straddles `−margin` | `INCONCLUSIVE` | hold |

This makes "we cannot tell yet" a first-class outcome instead of a silent pass.

Shapiro-Wilk selects Welch for normal data and Mann-Whitney with a seeded
percentile bootstrap otherwise, so both paths yield an interval and the same rule
applies to each. The bootstrap seed is fixed so a rollout decision is
reproducible from the audit log.

The margin defaults to `0.2 × stdev(baseline)` — Cohen's small effect. An
absolute default cannot work across signals: 0.2 is a meaningful drop on a 1–5
judge score and noise on a latency in milliseconds.

---

## D7 — Shadow mode costs the application a second inference call

The guide asks for shadow mode to "run the experimental variant on all traffic
but don't show results to users". The SDK cannot do this alone — it does not know
how to run the application's AI feature.

So `evaluate()` returns `served_variant=baseline` plus `shadow_variant`, and the
application runs both, reporting the shadow output through
`record_shadow_outcome()`. Shadow scores flow through the same evaluator and
windows but can never advance a rollout; they only gate whether a real rollout may
start.

The guide's "3–5 lines of integration" therefore holds for an ordinary rollout but
**not** for shadow mode. Documented rather than papered over.

---

## D8 — Time is injected everywhere

Nothing in the rollout logic reads the wall clock. `Clock` has three
implementations: `SystemClock`, `FakeClock` for tests, and `ScaledClock` for the
demo.

This is what lets the guide's real ramp (1%/2h → 5%/6h → … → 100%) be tested
instantly *and* demonstrated in under four minutes without a separate "demo mode"
branch in the controller. The plan stays written in hours in every case.

---

## D9 — Rollback is terminal until an operator resumes

An automatic rollback sets the percentage to 0 and moves the flag to
`ROLLED_BACK`, which the controller treats as inert. Automation that can undo its
own rollback can flap.

A cooldown covers the adjacent case: if an operator resumes quickly while the
underlying problem is still present, the controller takes no automatic action
until the cooldown expires.

Targeting cannot defeat this either — an allowlist entry does not resurrect a
rolled-back variant. Asserted in
`test_targeting_does_not_override_a_rolled_back_flag`.

---

## D10 — Testing with `unittest`, run under `pytest`

Tests are `unittest.TestCase` classes, matching `semantic-llm-cache` and
`llm-output-arbitration`. They run under `python -m unittest` with zero installs,
and under `uv run pytest` for the richer output and the phase-scoped exit
criteria.

The practical benefit showed up immediately: the entire pure core stayed testable
during a period when the machine had no working PyPI egress.
