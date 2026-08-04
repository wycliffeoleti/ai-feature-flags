# Blockers

**Nothing is currently blocked.** All six phases meet their exit criteria.

This file is kept because the one entry it held was a misdiagnosis, and the way
it went wrong is worth not repeating.

---

## B1 — "No PyPI egress" — RESOLVED, and it was never true

**Claimed:** the machine had no HTTPS egress to PyPI, so the container image
could not be built and Streamlit could not be installed.

**Actually:** PyPI was reachable the whole time. The diagnosis was wrong.

### How it went wrong

The reachability probe was `curl https://pypi.org/simple/`. That URL is the
**full package index** — every project on PyPI in a single HTML document,
40 MB and still streaming after 25 seconds. Every probe hit the timeout, and the
timeout was read as an outage.

The surrounding evidence looked consistent and reinforced it: DNS resolved, ICMP
worked, `uv sync` timed out, `docker build` hung on the pip layer. All true, and
all explained by something else — those commands ran in a **sandboxed shell with
no network**, which produces exactly the same symptom as a network outage.

Two independent causes, one shared symptom, and a probe that would have timed out
even on a perfect connection. The conclusion was over-determined and wrong.

### What actually establishes reachability

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://pypi.org/            # 200 in 0.11s
curl -sS -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/requests/
curl -sS -o /dev/null -w '%{http_code}\n' https://files.pythonhosted.org/
```

Never `/simple/` with no package name. It is not a health check; it is a bulk
download.

### The lesson

A probe has to be able to succeed quickly when the thing works. `/simple/`
cannot, so it could only ever produce evidence for the negative conclusion.
Before writing an environmental blocker, check that the check itself is sound —
especially when several signals agree, because agreement between symptoms of
different causes feels like corroboration and is not.

### Consequences that were reverted

- `Dockerfile` and `compose.yaml` are built and verified — `scripts/acceptance.sh`
  passes end to end.
- The guide matrix no longer marks the Compose stack unverified.
- The README no longer says the container stack does not run.

### Consequence deliberately left standing

The dashboard is still server-rendered HTML rather than Streamlit
([D14](DECISIONS.md)). That began as a workaround for the imagined blocker, but
the reasoning holds independently: it is read-mostly operational tables, it needs
no build step, and it renders from exactly the data the controller decided on.
Switching to Streamlit is now *possible* rather than *necessary* — a call for
Wycliffe, not a blocker.

---

## Open items (not blockers)

- **Demo recording** — runbook written and timed; Wycliffe's to make.
- **Real Slack delivery** — implemented and unit-tested; needs a webhook URL and
  explicit go-ahead.
- **Ollama judge** — implemented and boundary-tested; needs a running local
  Ollama and a model tag to produce real-model evidence.
- **Browser screenshots** — static HTML in `docs/assets/dashboard/` is the
  reviewable artefact; screenshots need Wycliffe's Chrome session.
