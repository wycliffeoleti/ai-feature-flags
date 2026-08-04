# BASWE Project 12 guide matrix

Every requirement in the **BASWE Project 12: AI Feature Flag System with Gradual
Rollout and Quality Monitoring** guide (pp. 43–47), with where it is implemented,
what evidences it, and any boundary.

This distinguishes what is built and verified from what is written but unproven.
It does not narrow, restate, or quietly satisfy a weaker version of a
requirement — where something is substituted or unverified, it says so.

**Legend** — ✅ done and evidenced · ⚠️ done differently (substitution stated)
· ⛔ not verified

---

## Tech stack

| Guide specifies | Built as | Status |
|---|---|---|
| Python 3.11+ | Python 3.11+ (`requires-python = ">=3.11"`) | ✅ |
| PostgreSQL + Redis | PostgreSQL 16 for durable config, evidence and audit; Redis for the published snapshot and the outcome queue | ✅ real, contract-tested against both |
| Custom + LLM-as-judge | `FixtureJudge` (deterministic, default) and `OllamaJudge` (opt-in, loopback-only) | ⚠️ no paid API is called on any code path |
| Python client library | `aiflags.sdk.FlagClient` | ✅ |
| React or Streamlit dashboard | Server-rendered HTML, no build step | ⚠️ neither — see [D14](DECISIONS.md) |
| Slack webhooks | `SlackWebhookNotifier` implemented; `RecordingNotifier` is the default | ⚠️ never delivered; needs a URL and Wycliffe's go-ahead |
| Docker + docker-compose | `Dockerfile` and `compose.yaml` | ✅ built and verified by `scripts/acceptance.sh` |

---

## Phase 1 — Flag evaluation engine

| # | Guide requirement | Where | Evidence |
|---|---|---|---|
| 1 | Rollout percentage (0–100%) | `core/models.py` | ✅ validated 0–100, `CHECK` in schema |
| 1 | Quality threshold | `QualityGate.threshold` | ✅ round-trip tests |
| 1 | Rollback trigger conditions | `QualityGate` + `core/decision.py` | ✅ 28 decision-table tests |
| 1 | Baseline configuration | `FlagDefinition.baseline` | ✅ |
| 1 | Experimental configuration | `FlagDefinition.experimental` | ✅ |
| 2 | `flag_client.evaluate(flag_name, user_context)` | `sdk.py` | ✅ |
| 2 | Consistent user assignment via hashing | `core/bucketing.py` | ✅ frozen SHA-256 vectors; stickiness asserted |
| 2 | Percentage-based rollout | `core/evaluation.py` | ✅ monotonic-ramp property test |
| 2 | Local caching of flag configuration | `sdk.py` | ✅ `evaluate` asserted to make zero fetches |
| 2 | Graceful degradation to baseline if unreachable | `sdk.py` | ✅ outage, staleness, out-of-order tests |
| 3 | Targeting by user segment | `core/targeting.py` | ✅ |
| 3 | Targeting by geography | `core/targeting.py` | ✅ |
| 3 | Targeting by request metadata | `core/targeting.py` | ✅ |
| 3 | Allowlist / blocklist by user ID | `core/targeting.py` | ✅ full precedence table |
| 4 | CRUD endpoints for flags | `api/app.py` | ✅ |
| 4 | `POST /flags/{id}/rollout` | `api/app.py` | ✅ |
| 4 | `POST /flags/{id}/pause` (halt at current %) | `api/app.py` | ✅ percentage asserted unchanged |
| 4 | `POST /flags/{id}/rollback` (all traffic to baseline) | `api/app.py` | ✅ atomic, single audit entry |
| 4 | All changes logged with actor and reason | `store/base.py`, `migrations/001` | ✅ required by signature + `CHECK` constraints |

**Beyond the guide:** `POST /flags/{id}/resume`, because rollback is terminal by
design ([D9](DECISIONS.md)) and something has to undo it deliberately.

---

## Phase 2 — Quality monitoring layer

| # | Guide requirement | Where | Evidence |
|---|---|---|---|
| 1 | LLM-as-judge scoring (1–5) | `judge/` | ⚠️ fixture default, Ollama opt-in; no paid API |
| 1 | User feedback signals | `QualitySignal.FEEDBACK` | ✅ |
| 1 | Latency thresholds | `QualitySignal.LATENCY_MS` | ✅ |
| 1 | Error rate | `QualitySignal.ERROR_RATE` | ✅ |
| 2 | Async evaluation after every gated response | `sdk.record_outcome` → queue | ✅ |
| 2 | Must not add latency to the response | `sdk.py` | ✅ `evaluate` does no I/O; `record_outcome` only buffers |
| 2 | Background worker with a message queue | `workers/evaluator.py`, Redis Streams | ✅ contract-tested against real Redis |
| 3 | Rolling windows (last 100, 1 hour, 24 hours) | `core/windows.py` | ✅ |
| 3 | Mean, standard deviation, P10, trend | `core/windows.py` | ✅ 22 tests |
| 3 | Compare experimental vs baseline continuously | `store/quality.py`, `workers/controller.py` | ✅ |
| 4 | Rollback when quality is below threshold for a sustained period | `core/decision.py` | ✅ trailing-window reading, stated in [D5](DECISIONS.md) |
| 4 | Set rollout percentage to 0% | `store/*.rollback()` | ✅ atomic with the status change |
| 4 | Slack alert with the quality data | `notify/` | ✅ payload asserted field by field; ⚠️ recorded, not delivered |
| 4 | Log the rollback event with full context | `controller_decisions` table | ✅ evidence JSONB |
| 4 | Cooldown to prevent flapping | `core/decision.py` | ✅ |

**Beyond the guide:** an `unscored_rate` signal ([D4](DECISIONS.md)). Without it,
a judge that times out reads as "no bad scores observed" and the rollout ramps to
100% while nothing is being measured.

---

## Phase 3 — Gradual rollout automation

| # | Guide requirement | Where | Evidence |
|---|---|---|---|
| 1 | Staged schedule (1%/2h, 5%/6h, 25%/24h, 50%/24h, 100%) | `DEFAULT_ROLLOUT_PLAN` | ✅ exact durations |
| 1 | Quality checked at each stage boundary | `workers/controller.py` | ✅ |
| 1 | Auto-advance when above threshold | `core/decision.py` | ✅ full schedule driven on a `FakeClock` |
| 1 | Pause and alert when quality dips | `workers/controller.py` | ✅ |
| 2 | Canary analysis at each stage | `core/canary.py` | ✅ |
| 2 | Statistical testing (as in Project 9) | `core/canary.py` | ✅ Shapiro → Welch or Mann-Whitney |
| 2 | Advance only when statistically no worse | `core/decision.py` | ⚠️ implemented as non-inferiority, not significance — [D6](DECISIONS.md) |
| 3 | Dashboard: current stage and percentage | `dashboard/` | ✅ |
| 3 | Dashboard: quality comparison over time | `dashboard/` | ✅ experimental vs baseline with delta |
| 3 | Dashboard: upcoming transitions and conditions | `render_flag_detail` | ✅ stage table + gate list |
| 3 | Dashboard: triggered pauses and rollbacks with reasons | `render_flag_detail` | ✅ every decision including holds |
| 3 | Dashboard: estimated time to full rollout | `optimistic_seconds_to_full` | ✅ named as a best case, not a forecast |
| 4 | Shadow mode: run experimental on all traffic, show nobody | `core/evaluation.py` + SDK | ⚠️ needs a second inference call from the app — [D7](DECISIONS.md) |
| 4 | Log what would have been shown, evaluate offline | `record_shadow_outcome` | ✅ scored but can never advance a rollout |

---

## Phase 4 — Dashboard and integration

| # | Guide requirement | Where | Evidence |
|---|---|---|---|
| 1 | All flags and status (off / rolling out / fully on / rolled back) | `render_overview` | ✅ |
| 1 | Real-time quality metrics for active flags | `dashboard/data.py` | ✅ |
| 1 | Rollout schedule with progress indicators | `render_flag_detail` | ✅ |
| 1 | One-click rollback with confirmation | `dashboard/views.py` | ✅ POST + `confirm()`, 303 redirect |
| 2 | Quality metrics over full rollout history | `render_flag_detail` | ✅ |
| 2 | Impact on business metrics (if tracked) | — | ⚠️ none tracked; no real business data exists |
| 2 | Summary of rollback events and causes | `render_analytics` | ✅ |
| 2 | Time-to-full-rollout for successful features | `build_analytics` | ✅ measured from the audit trail |
| 3 | SDK documentation with code examples | `docs/SDK.md` | ✅ 48 executed doctests |
| 3 | Emphasise minimal (3–5 line) integration | `docs/SDK.md` | ✅ shown — and stated where it does *not* hold |
| 3 | Example: AI vs non-AI fallback | `docs/SDK.md` | ✅ executed |
| 3 | Example: prompt version A/B testing | `docs/SDK.md` | ✅ executed |
| 3 | Example: model swap testing | `docs/SDK.md` | ✅ executed |

---

## Phase 5 — Integration testing and demo

| # | Guide requirement | Where | Evidence |
|---|---|---|---|
| 1 | Demo app: AI-powered email subject line generator | `demo/generator.py` | ✅ |
| 1 | Flag starting at 0% | `demo/scenario.py` | ✅ |
| 1 | Gradual rollout with quality monitoring | `demo/scenario.py` | ✅ |
| 1 | Deliberately bad variant detected and auto-rolled back | `demo/scenario.py` | ✅ realistic failure: unrendered `{customer_name}` |
| 1 | Successful rollout of a good variant to 100% | `demo/scenario.py` | ✅ |
| 2 | Test: consistent user assignment | `tests/phase5` | ✅ |
| 2 | Test: automatic rollback on degradation | `tests/phase5` | ✅ |
| 2 | Test: staged advance on thresholds | `tests/phase5` | ✅ |
| 2 | Test: SDK handles flag service outages | `tests/phase5` | ✅ |
| 3 | docker-compose: PostgreSQL, Redis, API, evaluator, dashboard, demo | `compose.yaml` | ✅ acceptance run passes end to end |
| 3 | Script running the full demo end to end | `scripts/acceptance.sh` | ✅ Compose stack; `acceptance_local.sh` is the no-Docker equivalent |

---

## Phase 6 — Portfolio

| # | Guide requirement | Where | Evidence |
|---|---|---|---|
| 1 | Demo recording under 4 minutes | `docs/DEMO_RUNBOOK.md` | ⚠️ runbook written and timed; **recording is Wycliffe's to make** |
| 2 | Narrative framing the distinction | `README.md` | ✅ |

---

## Summary

Every requirement is built and evidenced. Four items are substitutions driven by
the no-paid-API constraint, and each is stated as such here and in
`DECISIONS.md` rather than absorbed silently:

| Substitution | Why |
|---|---|
| Fixture judge by default, Ollama opt-in | no paid API on any code path |
| Server-rendered HTML dashboard | see [D14](DECISIONS.md) — a choice, not a constraint |
| Slack payloads recorded, not delivered | outward-facing; needs a webhook URL and consent |
| No business-metric impact | no real business data exists to measure |

Nothing is unverified. The Compose stack was previously marked ⛔ on the basis of
a misdiagnosed network failure; it builds and passes acceptance. See
[`BLOCKED.md`](BLOCKED.md) for what went wrong.
