# Phase Plan

Implements BASWE Project 12 in order. Each phase ends with RED/GREEN test
evidence, a `PHASE_N_EVIDENCE.md`, a clean tree, and a commit on its own branch.
A phase is done when its exit criterion — a command with an exit code — passes,
not when it looks done.

## Status

| Phase | Scope | Exit criterion | Status |
|---|---|---|---|
| 1 | Flag schema, bucketing, targeting, evaluation, SDK, migrations, management API | `uv run pytest tests/phase1 -q` | **Done** — [evidence](PHASE_1_EVIDENCE.md) |
| 2 | Judge protocol, queue, evaluator worker, rolling windows, rollback rules, notifier | `uv run pytest tests/phase2 -q` | **Done** — [evidence](PHASE_2_EVIDENCE.md) |
| 3 | Staged schedules, canary gating, shadow mode, controller loop | `uv run pytest tests/phase3 -q` | Decision + canary done; controller loop pending |
| 4 | Dashboard, analytics view, SDK docs as doctests | `uv run pytest tests/phase4 -q` | Not started |
| 5 | Demo app, four integration tests, Compose stack | `uv run pytest tests/phase5 -q` and `scripts/acceptance.sh` | Not started |
| 6 | Timed runbook, README narrative, guide matrix | runbook under 4 minutes; matrix complete | Not started |

The pure core landed first and out of phase order. That was a response to a
temporary loss of PyPI egress on the build machine: the stdlib-only core (D1)
stayed fully buildable and testable while nothing could be installed. The
remaining work in each phase is the part that needs Postgres, Redis, FastAPI, or
a judge process.

## What exists now

149 tests, no network and no services required. All but `core/canary.py`
(which needs SciPy) run with no virtualenv at all:

| Module | Tests |
|---|---|
| `core/bucketing.py` | 7 — determinism, uniformity, cross-flag independence, frozen vectors |
| `core/targeting.py` | 12 — full precedence table, miss cases, validation |
| `core/evaluation.py` | 24 — stickiness, monotonic ramp, every fail-safe path, status handling |
| `clock.py` | 9 — fake and scaled time |
| `sdk.py` | 25 — outage degradation, bounded buffer, shadow reporting |
| `core/windows.py` | 22 — statistics, window selection, unscored handling, trend |
| `core/decision.py` | 28 — the full decision table |
| `core/canary.py` | 22 — non-inferiority verdicts, test selection, degenerate input |

## Test discipline

Record a failing test first, make the smallest change that passes it, then
refactor with the tests still green. Keep deterministic vectors for hashing,
statistics, clock behaviour, and async jobs. Mark synthetic inputs as synthetic.

Two tests were corrected during the build rather than the code being softened —
both cases where the assertion encoded an intuition the implementation was right
to contradict. They are called out in `DECISIONS.md` (D5, D6). Never weaken a
test to make a criterion pass; if a criterion turns out to be wrong, record why
and stop.

## Operating boundaries

Provider credentials, paid APIs, network egress, Slack delivery, deployment,
real traffic, and real user identifiers all require explicit authorisation and
are not implied by any phase below. The default judge is a deterministic fixture;
the optional one talks to a loopback Ollama. Neither costs money.
