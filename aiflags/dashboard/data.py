"""View models for the dashboard.

Assembles what an operator needs to answer three questions at a glance: what is
rolling out, is it healthy, and when will it finish. Kept separate from
rendering so the numbers can be tested without parsing HTML.

The estimate deliberately carries an honest name. ``optimistic_seconds_to_full``
assumes every remaining stage advances at its first opportunity, which is the
best case and never the expected one — a rollout that pauses for a canary, or
sits waiting for samples, takes longer. Calling it "estimated time remaining"
would invite it to be read as a forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aiflags.core.models import (
    FlagDefinition,
    FlagStatus,
    QualitySignal,
    VariantKind,
)
from aiflags.core.windows import Trend, WindowStats, summarize
from aiflags.store.base import FlagRepository
from aiflags.store.quality import (
    DecisionRecord,
    QualityStore,
    StoredRolloutState,
)

# Statuses where a rollout is genuinely in flight, as opposed to finished,
# never started, or withdrawn.
ACTIVE_STATUSES = frozenset(
    {FlagStatus.ROLLING_OUT, FlagStatus.PAUSED, FlagStatus.SHADOW}
)


@dataclass(frozen=True, slots=True)
class FlagOverview:
    """Everything the dashboard shows about one flag."""

    flag: FlagDefinition
    state: StoredRolloutState | None
    experimental: WindowStats
    baseline: WindowStats
    latest_decision: DecisionRecord | None
    stage_index: int
    stage_count: int
    next_percentage: float | None
    optimistic_seconds_to_full: float | None

    @property
    def key(self) -> str:
        return self.flag.key

    @property
    def is_active(self) -> bool:
        return self.flag.status in ACTIVE_STATUSES

    @property
    def stage_label(self) -> str:
        return f"{self.stage_index + 1} of {self.stage_count}"

    @property
    def quality_delta(self) -> float | None:
        """Experimental mean minus baseline mean, when both are measured."""
        if self.experimental.mean is None or self.baseline.mean is None:
            return None
        return self.experimental.mean - self.baseline.mean

    @property
    def is_blind(self) -> bool:
        """No scored samples at all — not the same as no problems."""
        return self.experimental.is_blind


@dataclass(frozen=True, slots=True)
class RollbackSummary:
    """One rollback, for the analytics view."""

    flag_key: str
    reason: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Analytics:
    """Historical view across all flags."""

    rollbacks: list[RollbackSummary]
    completed: dict[str, float]
    decision_counts: dict[str, int]

    @property
    def rollback_count(self) -> int:
        return len(self.rollbacks)


def build_overview(
    flag: FlagDefinition,
    quality: QualityStore,
    sample_limit: int = 200,
) -> FlagOverview:
    """Assemble the view model for one flag."""
    state = quality.get_rollout_state(flag.key)
    stage_index = state.stage_index if state else 0
    stages = flag.rollout_plan.stages
    stage_index = min(stage_index, len(stages) - 1)

    signal = _primary_signal(flag)
    experimental = summarize(
        quality.samples(flag.key, signal, VariantKind.EXPERIMENTAL, limit=sample_limit)
    )
    baseline = summarize(
        quality.samples(flag.key, signal, VariantKind.BASELINE, limit=sample_limit)
    )

    decisions = quality.decisions(flag.key, limit=1)
    next_index = stage_index + 1
    next_percentage = (
        stages[next_index].percentage if next_index < len(stages) else None
    )

    return FlagOverview(
        flag=flag,
        state=state,
        experimental=experimental,
        baseline=baseline,
        latest_decision=decisions[-1] if decisions else None,
        stage_index=stage_index,
        stage_count=len(stages),
        next_percentage=next_percentage,
        optimistic_seconds_to_full=_optimistic_remaining(flag, stage_index),
    )


def build_overviews(
    repository: FlagRepository, quality: QualityStore, sample_limit: int = 200
) -> list[FlagOverview]:
    return [
        build_overview(flag, quality, sample_limit)
        for flag in sorted(repository.list_flags(), key=lambda f: f.key)
    ]


def build_analytics(
    repository: FlagRepository, quality: QualityStore, limit: int = 500
) -> Analytics:
    """Summarise rollback history and completion across all flags."""
    rollbacks: list[RollbackSummary] = []
    counts: dict[str, int] = {}
    completed: dict[str, float] = {}

    for decision in quality.decisions(limit=limit):
        counts[decision.action] = counts.get(decision.action, 0) + 1
        if decision.action == "rollback":
            rollbacks.append(
                RollbackSummary(
                    flag_key=decision.flag_key,
                    reason=decision.reason,
                    at=decision.decided_at,
                )
            )

    for flag in repository.list_flags():
        if flag.status is not FlagStatus.FULLY_ON:
            continue
        seconds = _time_to_full_rollout(flag.key, repository)
        if seconds is not None:
            completed[flag.key] = seconds

    return Analytics(
        rollbacks=rollbacks, completed=completed, decision_counts=counts
    )


def _primary_signal(flag: FlagDefinition) -> QualitySignal:
    """The signal the dashboard charts.

    An unscored-rate gate has no samples of its own — the rate is derived from
    how the judge-score samples were judged — so charting it would show an empty
    series. Fall back to the judge score in that case.
    """
    gates = flag.quality_policy.gates
    if not gates:
        return QualitySignal.JUDGE_SCORE
    signal = gates[0].signal
    return (
        QualitySignal.JUDGE_SCORE
        if signal is QualitySignal.UNSCORED_RATE
        else signal
    )


def _optimistic_remaining(flag: FlagDefinition, stage_index: int) -> float | None:
    """Best-case seconds to 100%, assuming every stage advances immediately.

    ``None`` once the flag is no longer ramping — a rolled-back or fully-on flag
    has no meaningful remaining time.
    """
    if flag.status not in ACTIVE_STATUSES:
        return None
    remaining = flag.rollout_plan.stages[stage_index:]
    return sum(stage.dwell_seconds for stage in remaining)


def _time_to_full_rollout(flag_key: str, repository: FlagRepository) -> float | None:
    """Wall-clock seconds from a flag's creation to reaching 100%."""
    events = repository.audit_events(flag_key)
    if not events:
        return None
    created = events[0].at
    for event in reversed(events):
        if event.detail.get("status") == FlagStatus.FULLY_ON.value:
            return (event.at - created).total_seconds()
    return None


def format_duration(seconds: float | None) -> str:
    """Render a duration the way an operator reads a rollout schedule."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


TREND_SYMBOLS = {
    Trend.IMPROVING: "improving",
    Trend.STABLE: "stable",
    Trend.DEGRADING: "degrading",
    Trend.UNKNOWN: "unknown",
}
