# Blocked

One item cannot be completed on this machine in its current state. Everything
else in Phases 1–5 is done and verified.

---

## B1 — The container image cannot be built (no PyPI egress)

**Blocks:** the second half of Phase 5's exit criterion —
`scripts/acceptance.sh` bringing up Compose and exiting 0.

**Symptom:** `docker build` hangs indefinitely on the `pip install` layer.

**Cause:** this machine has no HTTPS egress to PyPI. Diagnosed rather than
assumed:

| Check | Result |
|---|---|
| `getent hosts pypi.org` | resolves (IPv6 records) |
| `ping 1.1.1.1` | 0% loss, 45ms |
| `curl -4 https://pypi.org/simple/` | times out |
| `curl -6 https://pypi.org/simple/` | times out |
| `uv sync` (fresh packages) | times out |
| `docker build` (pip layer) | times out |

DNS and ICMP work; HTTPS to PyPI does not, over either address family. The
project's own dependencies were installed before egress was lost, which is why
the test suite runs — but `python:3.12-slim` has none of them, so the image build
cannot proceed.

**What was verified instead:** `scripts/acceptance_local.sh` runs the identical
assertions against a locally launched API backed by the *same real services*
(PostgreSQL 16 and Redis Stack in containers). It passes end to end:

```
--- running the offline rollout scenario
      broken variant  : rolled_back at 0%
        reason        : judge_score p10 of 2.5 is below the threshold 3
                        across 50 consecutive evaluations
      good variant    : hold -> advance -> advance -> advance -> complete
                        fully_on at 100%
--- asserting the end state
    flag is rolled_back at 0%
    audit trail complete: create_flag -> set_rollout_percentage -> rollback
    rollback reverted from 25.0%
    snapshot v3 serves the rolled-back flag
    dashboard, detail and analytics served
PASS
```

So the application code, the schema, the API, the data plane, and the dashboard
are all exercised against real PostgreSQL and Redis. What is **not** verified is
the containerisation itself: the `Dockerfile` and `compose.yaml` are written and
syntactically valid but have never been built or run.

**To unblock**, with network available:

```bash
docker build -t aiflags .          # should complete
./scripts/acceptance.sh            # should print PASS
```

If it passes, `compose.yaml` and the `Dockerfile` can be treated as verified and
Phase 5 marked complete. Until then, **do not claim the Compose stack runs** —
neither in the README, the guide matrix, nor any portfolio material.

**Not attempted, deliberately:** vendoring wheels from the local `uv` cache into
the image, or building from a `--find-links` directory. Both would produce an
image that builds only on this machine, which is a worse artefact than an honest
"unverified" — the point of the Dockerfile is that someone else can run it.

---

## Related open items (not blockers)

- **Streamlit** — same root cause. The dashboard is server-rendered HTML instead;
  recorded as a substitution in `DECISIONS.md` D14, not as a blocker, because the
  requirement (an operator dashboard) is met by other means.
- **Browser screenshots** — needs Wycliffe's Chrome session. Static HTML in
  `docs/assets/dashboard/` is the reviewable artefact meanwhile.
- **Real Slack delivery** — implemented and unit-tested; needs a webhook URL and
  Wycliffe's go-ahead.
- **Ollama judge** — implemented and boundary-tested; needs a running local
  Ollama and a model tag to produce real-model evidence.
