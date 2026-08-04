"""The rollout decision function.

This is the only place that decides whether users keep seeing an AI feature. It
is deliberately pure: given a rollout's current state, its plan and policy, the
observed samples, and a canary verdict, it returns what should happen. No
database, no clock, no model — so the whole policy is a table test rather than
something inferred from an integration run.

**The ordering is the safety property.** Rollback checks run before anything
that could advance, so a stage whose dwell time has elapsed still rolls back if
quality has gone. Below that, every branch that represents missing or ambiguous
evidence resolves to :attr:`Action.HOLD`. An advance requires positive evidence
on all three counts: dwell time satisfied, enough samples to judge, and a canary
verdict of :attr:`~aiflags.core.models.CanaryVerdict.NO_WORSE`.

The asymmetry is intentional. Failing to advance costs a slower rollout; failing
to roll back costs users a broken feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from aiflags.core.models import (
    CanaryVerdict,
    Comparison,
    FlagStatus,
    QualityGate,
    QualityPolicy,
    QualitySignal,
    RolloutPlan,
    Statistic,
)
from aiflags.core.windows import Sample, WindowStats, summarize


class Action(StrEnum):
    """What the controller should do with a rollout."""

    ADVANCE = "advance"
    HOLD = "hold"
    PAUSE = "pause"
    ROLLBACK = "rollback"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class RolloutState:
    """Where a rollout currently stands."""

    flag_key: str
    status: FlagStatus
    stage_index: int
    rollout_percentage: float
    stage_entered_at: datetime
    rolled_back_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do, why, and the evidence it was based on.

    ``evidence`` is carried so a rollback can be explained months later from the
    audit log without replaying the data that caused it.
    """

    action: Action
    reason: str
    target_percentage: float | None = None
    target_stage_index: int | None = None
    evidence: dict[QualitySignal, WindowStats] = field(default_factory=dict)


# Statuses in which the controller takes no automatic action at all.
_INERT_STATUS_REASONS: dict[FlagStatus, str] = {
    FlagStatus.ROLLED_BACK: (
        "flag is rolled back; resuming is an explicit operator action so that an "
        "automatic rollback is never undone automatically"
    ),
    FlagStatus.FULLY_ON: "flag is fully rolled out; nothing left to advance",
    FlagStatus.OFF: "flag is off",
    FlagStatus.SHADOW: (
        "flag is in shadow mode; shadow traffic is scored but never advances a "
        "rollout, because no user has seen the experimental variant yet"
    ),
}

_STATISTIC_ATTRIBUTES: dict[Statistic, str] = {
    Statistic.MEAN: "mean",
    Statistic.P10: "p10",
    Statistic.P95: "p95",
    Statistic.RATE: "mean",
}


def decide(
    state: RolloutState,
    plan: RolloutPlan,
    policy: QualityPolicy,
    samples: dict[QualitySignal, list[Sample]],
    canary: CanaryVerdict | None,
    now: datetime,
) -> Decision:
    """Decide the next action for one rollout."""
    evidence = _build_evidence(policy, samples)

    # 1. Rollback first, so a stage that is otherwise ready to advance still
    #    rolls back. Skipped only for statuses where there is nothing to protect.
    if state.status not in (FlagStatus.ROLLED_BACK, FlagStatus.OFF):
        breach = _find_breach(policy, samples)
        if breach is not None:
            return Decision(
                action=Action.ROLLBACK,
                reason=breach,
                target_percentage=0.0,
                target_stage_index=state.stage_index,
                evidence=evidence,
            )

    # 2. Statuses where no automatic progress is possible.
    inert = _INERT_STATUS_REASONS.get(state.status)
    if inert is not None:
        return Decision(action=Action.HOLD, reason=inert, evidence=evidence)

    if state.status is FlagStatus.PAUSED:
        return Decision(
            action=Action.HOLD,
            reason="rollout is paused; advancing requires an operator to resume it",
            evidence=evidence,
        )

    # 3. Cooldown after a rollback, so a quick manual resume cannot flap the
    #    flag back and forth while the underlying problem is still present.
    if state.rolled_back_at is not None:
        elapsed = (now - state.rolled_back_at).total_seconds()
        if elapsed < plan.cooldown_seconds:
            remaining = plan.cooldown_seconds - elapsed
            return Decision(
                action=Action.HOLD,
                reason=(
                    f"in cooldown for another {remaining:.0f}s after a rollback; "
                    "holding to prevent flapping"
                ),
                evidence=evidence,
            )

    # 4. Dwell time. A stage exists to accumulate evidence; cutting it short
    #    defeats the point of staging at all.
    stage = plan.stages[state.stage_index]
    dwelled = (now - state.stage_entered_at).total_seconds()
    if dwelled < stage.dwell_seconds:
        return Decision(
            action=Action.HOLD,
            reason=(
                f"stage {state.stage_index} at {stage.percentage:g}% has dwelled "
                f"{dwelled:.0f}s of {stage.dwell_seconds:.0f}s"
            ),
            evidence=evidence,
        )

    # 5. Enough data to judge at all.
    observed = max((window.count for window in evidence.values()), default=0)
    if observed < policy.minimum_samples:
        return Decision(
            action=Action.HOLD,
            reason=(
                f"only {observed} scored samples against a minimum of "
                f"{policy.minimum_samples}; advancing on thin data would ramp "
                "before the quality signal means anything"
            ),
            evidence=evidence,
        )

    # 6. The canary must positively say the experiment is no worse. Anything
    #    else — worse, inconclusive, or not computed — refuses to advance.
    if canary is CanaryVerdict.WORSE:
        return Decision(
            action=Action.PAUSE,
            reason=(
                "canary analysis found the experimental variant statistically "
                "worse than baseline"
            ),
            evidence=evidence,
        )
    if canary is not CanaryVerdict.NO_WORSE:
        return Decision(
            action=Action.HOLD,
            reason=(
                f"canary verdict is {canary or 'unavailable'}; holding until the "
                "comparison is conclusive"
            ),
            evidence=evidence,
        )

    # 7. Advance, or finish.
    next_index = state.stage_index + 1
    if next_index >= len(plan.stages):
        return Decision(
            action=Action.COMPLETE,
            reason="final stage passed its quality gates; rollout is complete",
            target_percentage=stage.percentage,
            target_stage_index=state.stage_index,
            evidence=evidence,
        )
    next_stage = plan.stages[next_index]
    return Decision(
        action=Action.ADVANCE,
        reason=(
            f"stage {state.stage_index} held {stage.percentage:g}% for its full "
            f"dwell with a no-worse canary; advancing to {next_stage.percentage:g}%"
        ),
        target_percentage=next_stage.percentage,
        target_stage_index=next_index,
        evidence=evidence,
    )


def _build_evidence(
    policy: QualityPolicy, samples: dict[QualitySignal, list[Sample]]
) -> dict[QualitySignal, WindowStats]:
    """Summarize each gate's trailing window, for the audit record."""
    return {
        gate.signal: summarize(
            samples.get(gate.signal, []), last_n=gate.sustained_evaluations
        )
        for gate in policy.gates
    }


def _find_breach(
    policy: QualityPolicy, samples: dict[QualitySignal, list[Sample]]
) -> str | None:
    """Return a description of the first breached gate, or ``None``."""
    for gate in policy.gates:
        observed = samples.get(gate.signal, [])
        # A gate only breaches over a full sustained window. A shorter window is
        # a dip, and dips are what the sustained count exists to absorb.
        if len(observed) < gate.sustained_evaluations:
            continue
        window = summarize(observed, last_n=gate.sustained_evaluations)
        value = _observed_value(gate, window)
        if value is None:
            continue
        if _is_breach(gate, value):
            return (
                f"{gate.signal} {gate.statistic} of {value:.3g} is "
                f"{gate.comparison} the threshold {gate.threshold:g} across "
                f"{gate.sustained_evaluations} consecutive evaluations"
            )
    return None


def _observed_value(gate: QualityGate, window: WindowStats) -> float | None:
    if gate.signal is QualitySignal.UNSCORED_RATE:
        # Read from the window's own bookkeeping rather than the sample values:
        # an unscored sample has no value to average.
        return window.unscored_rate
    return getattr(window, _STATISTIC_ATTRIBUTES[gate.statistic])


def _is_breach(gate: QualityGate, value: float) -> bool:
    if gate.comparison is Comparison.BELOW:
        return value < gate.threshold
    return value > gate.threshold
