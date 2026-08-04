"""Server-rendered HTML for the dashboard.

Dependency-free by necessity and by preference. The guide offers "React or
Streamlit"; neither is installed here and neither is needed for read-mostly
operational views. What is needed is that the page renders from the same data the
controller decided on, with no build step between them. See the guide matrix for
the substitution.

Every interpolated value passes through :func:`escape`. The content includes flag
keys and rollback reasons, and a reason is free text written by whoever triggered
the rollback — including, in the demo, text derived from model output.

These are pure functions from view models to strings, so the numbers on the page
are asserted directly rather than by parsing rendered HTML.
"""

from __future__ import annotations

from html import escape

from aiflags.core.models import FlagStatus
from aiflags.dashboard.data import (
    Analytics,
    FlagOverview,
    TREND_SYMBOLS,
    format_duration,
)

STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif;
       margin: 0; padding: 2rem; max-width: 78rem; margin-inline: auto; }
h1, h2 { font-weight: 600; letter-spacing: -0.01em; }
h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.1rem; margin-top: 2.5rem; }
.sub { opacity: 0.7; margin-top: 0; }
table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
th, td { text-align: left; padding: 0.55rem 0.7rem; border-bottom: 1px solid
         color-mix(in srgb, currentColor 15%, transparent); vertical-align: top; }
th { font-weight: 600; font-size: 0.8rem; text-transform: uppercase;
     letter-spacing: 0.04em; opacity: 0.65; }
td.num { font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px;
         font-size: 0.78rem; font-weight: 600; white-space: nowrap;
         border: 1px solid currentColor; }
.badge.rolling_out { color: #1d6fd4; }
.badge.fully_on    { color: #12805c; }
.badge.rolled_back { color: #c0392b; }
.badge.paused      { color: #b7791f; }
.badge.shadow      { color: #6b46c1; }
.badge.off         { opacity: 0.6; }
.bar { position: relative; height: 0.5rem; border-radius: 999px; margin-top: 0.3rem;
       background: color-mix(in srgb, currentColor 15%, transparent); }
.bar > span { position: absolute; inset-block: 0; left: 0; border-radius: 999px;
              background: currentColor; }
.warn { color: #c0392b; font-weight: 600; }
.muted { opacity: 0.6; }
.reason { max-width: 34rem; font-size: 0.9rem; }
form { display: inline; }
button { font: inherit; padding: 0.3rem 0.7rem; border-radius: 6px; cursor: pointer;
         border: 1px solid currentColor; background: transparent; color: inherit; }
button.danger { color: #c0392b; font-weight: 600; }
nav a { margin-right: 1rem; }
"""


def page(title: str, body: str) -> str:
    """Wrap content in the shared document shell."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body>"
        "<nav><a href='/dashboard'>Flags</a>"
        "<a href='/dashboard/analytics'>Analytics</a></nav>"
        f"{body}</body></html>"
    )


def render_overview(overviews: list[FlagOverview]) -> str:
    """The flag management view: status, quality, progress, rollback."""
    if not overviews:
        body = "<h1>Flags</h1><p class='muted'>No flags defined yet.</p>"
        return page("Flags", body)

    rows = "".join(_overview_row(overview) for overview in overviews)
    body = (
        "<h1>Flags</h1>"
        "<p class='sub'>Rollout status and live quality for every flag.</p>"
        "<table><thead><tr>"
        "<th>Flag</th><th>Status</th><th>Rollout</th><th>Stage</th>"
        "<th>Quality (exp vs base)</th><th>Trend</th><th>Best case to 100%</th>"
        "<th>Last decision</th><th></th>"
        "</tr></thead><tbody>"
        f"{rows}"
        "</tbody></table>"
    )
    return page("Flags", body)


def _overview_row(overview: FlagOverview) -> str:
    flag = overview.flag
    key = escape(flag.key)

    quality = _quality_cell(overview)
    decision = (
        f"<div><strong>{escape(overview.latest_decision.action)}</strong></div>"
        f"<div class='reason muted'>{escape(overview.latest_decision.reason)}</div>"
        if overview.latest_decision
        else "<span class='muted'>—</span>"
    )

    rollback = (
        _rollback_form(flag.key)
        if flag.status not in (FlagStatus.ROLLED_BACK, FlagStatus.OFF)
        else ""
    )

    return (
        "<tr>"
        f"<td><a href='/dashboard/flags/{key}'>{key}</a></td>"
        f"<td>{_status_badge(flag.status)}</td>"
        f"<td class='num'>{flag.rollout_percentage:g}%"
        f"<div class='bar'><span style='width:{flag.rollout_percentage:g}%'></span></div></td>"
        f"<td class='num'>{escape(overview.stage_label)}</td>"
        f"<td class='num'>{quality}</td>"
        f"<td>{escape(TREND_SYMBOLS[overview.experimental.trend])}</td>"
        f"<td class='num'>{escape(format_duration(overview.optimistic_seconds_to_full))}</td>"
        f"<td>{decision}</td>"
        f"<td>{rollback}</td>"
        "</tr>"
    )


def _quality_cell(overview: FlagOverview) -> str:
    if overview.is_blind:
        # A blind window must never render as a clean one. This is the whole
        # point of tracking unscored samples separately.
        return "<span class='warn'>no scored samples</span>"

    experimental = f"{overview.experimental.mean:.2f}"
    baseline = (
        f"{overview.baseline.mean:.2f}"
        if overview.baseline.mean is not None
        else "—"
    )
    delta = overview.quality_delta
    suffix = ""
    if delta is not None:
        css = "warn" if delta < 0 else "muted"
        suffix = f" <span class='{css}'>({delta:+.2f})</span>"

    unscored = ""
    if overview.experimental.unscored_rate > 0:
        unscored = (
            f"<div class='warn'>{overview.experimental.unscored_rate:.0%} unscored</div>"
        )

    return (
        f"{experimental} vs {baseline}{suffix}"
        f"<div class='muted'>n={overview.experimental.count}</div>{unscored}"
    )


def _status_badge(status: FlagStatus) -> str:
    return f"<span class='badge {status.value}'>{escape(status.value)}</span>"


def _rollback_form(flag_key: str) -> str:
    """One-click rollback, with a confirmation the browser enforces."""
    key = escape(flag_key)
    return (
        f"<form method='post' action='/dashboard/flags/{key}/rollback' "
        f"onsubmit=\"return confirm('Roll {key} back to baseline for all users?')\">"
        "<button class='danger' type='submit'>Roll back</button></form>"
    )


def render_flag_detail(
    overview: FlagOverview, decisions: list, audit: list
) -> str:
    """Per-flag view: schedule, upcoming transitions, decision history."""
    flag = overview.flag
    key = escape(flag.key)

    stages = "".join(
        "<tr>"
        f"<td class='num'>{index + 1}</td>"
        f"<td class='num'>{stage.percentage:g}%</td>"
        f"<td class='num'>{escape(format_duration(stage.dwell_seconds))}</td>"
        f"<td>{_stage_marker(index, overview.stage_index)}</td>"
        "</tr>"
        for index, stage in enumerate(flag.rollout_plan.stages)
    )

    history = "".join(
        "<tr>"
        f"<td>{escape(decision.decided_at.isoformat(timespec='seconds'))}</td>"
        f"<td><strong>{escape(decision.action)}</strong></td>"
        f"<td class='reason'>{escape(decision.reason)}</td>"
        f"<td class='muted'>{escape(_canary_label(decision))}</td>"
        "</tr>"
        for decision in reversed(decisions)
    ) or "<tr><td colspan='4' class='muted'>No decisions recorded yet.</td></tr>"

    trail = "".join(
        "<tr>"
        f"<td>{escape(event.at.isoformat(timespec='seconds'))}</td>"
        f"<td>{escape(event.action)}</td>"
        f"<td>{escape(event.actor)}</td>"
        f"<td class='reason'>{escape(event.reason)}</td>"
        "</tr>"
        for event in reversed(audit)
    ) or "<tr><td colspan='4' class='muted'>No audit events.</td></tr>"

    gates = "".join(
        "<li>"
        f"{escape(gate.signal.value)} {escape(gate.statistic.value)} "
        f"{escape(gate.comparison.value)} {gate.threshold:g}, sustained over "
        f"{gate.sustained_evaluations} evaluations"
        "</li>"
        for gate in flag.quality_policy.gates
    )

    body = (
        f"<h1>{key}</h1>"
        f"<p class='sub'>{_status_badge(flag.status)} at "
        f"{flag.rollout_percentage:g}% &middot; stage {escape(overview.stage_label)}</p>"
        "<h2>Quality gates</h2>"
        f"<ul>{gates}</ul>"
        "<h2>Rollout schedule</h2>"
        "<table><thead><tr><th>Stage</th><th>Percentage</th><th>Dwell</th>"
        "<th></th></tr></thead>"
        f"<tbody>{stages}</tbody></table>"
        "<h2>Controller decisions</h2>"
        "<p class='sub'>Holds are recorded too — that is what explains a rollout "
        "that has not moved.</p>"
        "<table><thead><tr><th>When</th><th>Action</th><th>Reason</th>"
        "<th>Canary</th></tr></thead>"
        f"<tbody>{history}</tbody></table>"
        "<h2>Audit trail</h2>"
        "<table><thead><tr><th>When</th><th>Action</th><th>Actor</th>"
        "<th>Reason</th></tr></thead>"
        f"<tbody>{trail}</tbody></table>"
    )
    return page(flag.key, body)


def _stage_marker(index: int, current: int) -> str:
    if index < current:
        return "<span class='muted'>done</span>"
    if index == current:
        return "<strong>current</strong>"
    return "<span class='muted'>upcoming</span>"


def _canary_label(decision) -> str:
    if not decision.canary:
        return "—"
    verdict = decision.canary.get("verdict", "—")
    n = decision.canary.get("n_experimental")
    return f"{verdict} (n={n})" if n is not None else str(verdict)


def render_analytics(analytics: Analytics) -> str:
    """Historical view: rollbacks and their causes, completion times."""
    rollbacks = "".join(
        "<tr>"
        f"<td>{escape(item.at.isoformat(timespec='seconds'))}</td>"
        f"<td>{escape(item.flag_key)}</td>"
        f"<td class='reason'>{escape(item.reason)}</td>"
        "</tr>"
        for item in reversed(analytics.rollbacks)
    ) or "<tr><td colspan='3' class='muted'>No rollbacks recorded.</td></tr>"

    completed = "".join(
        "<tr>"
        f"<td>{escape(key)}</td>"
        f"<td class='num'>{escape(format_duration(seconds))}</td>"
        "</tr>"
        for key, seconds in sorted(analytics.completed.items())
    ) or "<tr><td colspan='2' class='muted'>No flag has reached 100% yet.</td></tr>"

    counts = "".join(
        f"<tr><td>{escape(action)}</td><td class='num'>{count}</td></tr>"
        for action, count in sorted(analytics.decision_counts.items())
    ) or "<tr><td colspan='2' class='muted'>No decisions recorded.</td></tr>"

    body = (
        "<h1>Analytics</h1>"
        f"<p class='sub'>{analytics.rollback_count} rollback(s) recorded.</p>"
        "<h2>Rollbacks and their causes</h2>"
        "<table><thead><tr><th>When</th><th>Flag</th><th>Cause</th></tr></thead>"
        f"<tbody>{rollbacks}</tbody></table>"
        "<h2>Time to full rollout</h2>"
        "<table><thead><tr><th>Flag</th><th>Elapsed</th></tr></thead>"
        f"<tbody>{completed}</tbody></table>"
        "<h2>Decisions by action</h2>"
        "<table><thead><tr><th>Action</th><th>Count</th></tr></thead>"
        f"<tbody>{counts}</tbody></table>"
    )
    return page("Analytics", body)
