# Demo runbook — under four minutes

A timed walkthrough for the portfolio recording. Every command below has been
run; the timings are measured, not estimated.

**The recording itself is not made.** This runbook exists so it can be, in one
take, by someone who has not memorised the system.

## Before recording

```bash
cd ~/projects/Personal/ai-feature-flags

# Real services. Both images are already local.
docker run -d --name aiflags-pg -e POSTGRES_PASSWORD=aiflags \
  -e POSTGRES_DB=aiflags -p 127.0.0.1:55432:5432 postgres:16
docker run -d --name aiflags-redis \
  -p 127.0.0.1:56379:6379 redis/redis-stack-server:7.4.0-v8

uv run pytest -q                    # confirm green before filming
```

Have two terminals and a browser tab ready. Total runtime below: **3m 05s** of
narration over ~5s of actual command execution, leaving margin.

---

## 0:00 — The distinction (25s, no commands)

> Feature flags assume software fails in a binary way: the code path works or it
> throws. AI features don't. A prompt change doesn't crash — it just starts
> producing slightly worse answers, and nothing in a conventional flag system
> notices. This system treats "is it working" as a measured quantity and ties the
> rollout schedule to it.

---

## 0:25 — The failure, made concrete (30s)

Show `aiflags/demo/generator.py`:

```python
GOOD_TEMPLATE   = "{topic} — action needed"
BROKEN_TEMPLATE = "Hi {customer_name}, about your {topic}"
```

> The new prompt references `customer_name`. This pipeline never populates it.
> Nothing raises — the placeholder renders verbatim and ships. Users get
> "Hi {customer_name}, about your March invoice", and every downstream system
> reports a successful request. That's what a boolean flag cannot see.

---

## 0:55 — The rollout, both outcomes (45s)

```bash
uv run python -m aiflags.demo.scenario
```

Runs in **~1.6 seconds**. Point at the two blocks:

```
[1/2] BROKEN variant
      controller      : rollback
      rollback reason : judge_score p10 of 2.5 is below the threshold 3
                        across 50 consecutive evaluations
[2/2] GOOD variant
      controller      : hold -> advance -> advance -> advance -> complete
      final status    : fully_on at 100%
```

> Same schedule, same gates, opposite outcomes. The first tick on the good
> variant is a *hold* — the stage hadn't dwelled long enough yet. It never
> advances just because nothing looked wrong.

> This runs on an injected clock, so the guide's real schedule — 1% for two
> hours, 5% for six — completes in seconds. No duration was shortened for the
> demo.

---

## 1:40 — The decision function (35s)

Open `aiflags/core/decision.py`.

> Every rule about advancing, holding, pausing and rolling back is one pure
> function. No database, no clock, no model — so the whole policy is a table
> test rather than something you have to run a rollout to observe.

> The ordering is the safety property. Rollback checks run before anything that
> could advance, so a stage that's ready to ramp still rolls back if quality has
> gone. And everything ambiguous — thin data, an inconclusive canary, no canary
> at all — resolves to hold. Failing to advance costs a slower rollout. Failing
> to roll back costs users a broken feature.

---

## 2:15 — Two decisions worth defending (30s)

> **A broken judge is not good news.** If the judge times out, the sample is
> stored as unscored — not as zero, not discarded. A high unscored rate is its
> own rollback trigger. Otherwise a judge outage looks exactly like "no bad
> scores observed" and the rollout ramps to 100% while blind.

> **The canary tests non-inferiority, not significance.** Advancing whenever the
> experiment isn't *significantly worse* rewards ignorance — a small noisy sample
> is never significant, so you'd ramp fastest when you know least. Instead it
> asks whether a regression larger than a margin can be ruled out, which makes
> "we can't tell yet" a verdict that holds the rollout.

---

## 2:45 — The operator view (20s)

```bash
bash scripts/acceptance_local.sh
```

Then open `http://127.0.0.1:8123/dashboard` — or, without running anything,
`docs/assets/dashboard/flags.html`.

Point at the three rows: one mid-ramp and healthy, one rolled back with its cause
in plain text, one showing **no scored samples** in red.

> That red row is the one I care about. A flag whose judge is failing renders as
> "no scored samples", never as a clean row. Presenting an unmeasured rollout as
> a healthy one is the exact failure the unscored signal exists to prevent, and
> the presentation layer is where it'd be easiest to lose.

---

## 3:05 — Close (10s)

> Traditional feature flags can't do this, because they assume failure is
> binary. That distinction is the whole project.

---

## Measured timings

| Command | Wall clock |
|---|---|
| `uv run pytest -q` | ~13s with services, ~1s without |
| `uv run python -m aiflags.demo.scenario` | 1.62s |
| `bash scripts/acceptance_local.sh` | 2.59s including API startup |

Narration is the constraint, not execution. The script above is 3m 05s read at a
normal pace, inside the guide's four-minute limit with margin for a fluffed line.

## Not included

The Compose stack (`docker compose up`) is deliberately absent — it has never
been built ([B1](BLOCKED.md)), so it must not appear in a recording. Once the
image builds, `scripts/acceptance.sh` replaces `acceptance_local.sh` at the 2:45
mark with no other change to the script.
