# Phase 4 evidence — dashboard and SDK documentation

**Exit criterion:** `uv run pytest tests/phase4 -q` exits 0 and every SDK doc
example runs as a passing doctest. **Met.**

```
39 passed                     # tests/phase4
48 doctest examples attempted, 0 failed   # docs/SDK.md
417 tests across the suite     # 360 pass with no services, 57 more with them
```

## Guide requirements

| Guide requirement (Phase 3 item 3, Phase 4) | Where | Evidence |
|---|---|---|
| All flags with status: off / rolling out / fully on / rolled back | `dashboard/render.py` | Route and rendering tests |
| Real-time quality metrics for active flags | `dashboard/data.py` | Experimental-vs-baseline means, delta, n, unscored rate |
| Rollout schedule with progress indicators | `render_flag_detail` | Stage table marking done / current / upcoming |
| One-click rollback with confirmation | `dashboard/views.py` | POST → 303, `confirm()` asserted, audit attribution asserted |
| Quality metrics over rollout history | `render_flag_detail` | Full decision history including holds |
| Summary of rollback events and their causes | `render_analytics` | Rollback table with cause text |
| Time to full rollout for successful features | `build_analytics` | Measured from audit trail |
| Upcoming stage transitions and conditions | `render_flag_detail` | Stage table plus quality-gate list |
| Estimated time to full rollout | `optimistic_seconds_to_full` | Sum of remaining dwells |
| SDK documentation with code examples | `docs/SDK.md` | 48 executed doctests |
| Examples for AI vs non-AI fallback, prompt A/B, model swap | `docs/SDK.md` | All three, each executed |

## Substitution

The guide specifies "React or Streamlit". This is **neither** — it is
server-rendered HTML with inline CSS and no build step. Streamlit could not be
installed (this machine lost PyPI egress mid-build) and React would need a Node
toolchain for read-mostly operational tables. Recorded in `DECISIONS.md` D14; the
README and guide matrix must not claim Streamlit.

`python-multipart` is also avoided — the single form endpoint parses its
urlencoded body with the standard library rather than taking a dependency for one
field.

## Behaviour asserted

- **A blind window never renders as a healthy one.** Twenty unscored samples
  render `no scored samples` in red rather than an empty or zero row. This is the
  failure the unscored-rate signal exists to prevent, and the presentation layer
  is where it would be easiest to reintroduce.
- **Everything interpolated is escaped.** A flag key of
  `<script>alert(1)</script>` and a rollback reason of
  `<img src=x onerror=alert(1)>` both come out inert. Reasons are free text and,
  in the demo, derive from model output.
- **The estimate is named honestly.** `optimistic_seconds_to_full` assumes every
  remaining stage advances at its first opportunity — the best case, never the
  expected one. Calling it "estimated time remaining" would invite it to be read
  as a forecast.
- **Holds are shown, not just changes.** The detail view lists every controller
  decision, which is what explains a rollout that has not moved.
- **The rollback button cannot fire from a link.** It is a POST with a browser
  `confirm()`, answering 303 so a refresh does not repeat it.
- **The rollback button is hidden once a flag is already rolled back.**
- **The dashboard is additive.** `create_app(repository)` without a quality store
  still serves the Phase 1 API and returns 404 for `/dashboard`, so the
  management surface has no dependency on the dashboard.
- **An unscored-rate gate charts the judge scores.** That gate has no samples of
  its own — the rate is derived — so charting it literally would show an empty
  series.

## Static artefacts

`scripts/render_dashboard_sample.py` renders the three pages to
`docs/assets/dashboard/` from seeded synthetic data, so they can be reviewed from
a checkout with nothing running:

```bash
PYTHONPATH=. uv run python scripts/render_dashboard_sample.py
```

The sample reproduces the state the demo reaches: one flag mid-ramp and healthy,
one rolled back by a quality gate with the cause recorded, and one blind because
its judge was failing. Data is synthetic and labelled as such.

## Open item

Browser screenshots were not captured. The static HTML above is the reviewable
artefact; taking screenshots means driving Wycliffe's own Chrome session, which
is his call to make rather than something to do unattended. Not a phase blocker —
the criterion explicitly allows logging it.
