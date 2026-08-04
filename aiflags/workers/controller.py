"""Rollout controller.

The only process that changes what users see. On each tick it gathers the
observed quality for every active flag, runs the canary comparison, asks
:func:`~aiflags.core.decision.decide` what to do, and applies the answer.

The controller itself holds no policy. Every rule about when to advance, hold,
pause, or roll back lives in the pure decision function, which is why that logic
is a table test rather than something you have to run a rollout to observe. This
module is the I/O around it: read the evidence, apply the verdict, record what
happened, republish.

Every decision is recorded, including holds. "Why did this rollout sit at 5% for
six hours" is the question operators actually ask, and it cannot be answered from
a log that only records changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from aiflags.clock import Clock, SystemClock
from aiflags.core.canary import CanaryResult, compare
from aiflags.core.decision import Action, Decision, RolloutState, decide
from aiflags.core.models import (
    Comparison,
    FlagDefinition,
    FlagStatus,
    QualityPolicy,
    QualitySignal,
    VariantKind,
)
from aiflags.core.windows import Sample
from aiflags.notify.base import Notification, Notifier
from aiflags.store.base import FlagRepository
from aiflags.store.quality import (
    DecisionRecord,
    QualityStore,
    StoredRolloutState,
)

logger = logging.getLogger(__name__)

CONTROLLER_ACTOR = "rollout-controller"

# Statuses the controller has anything to do for. OFF and FULLY_ON flags are not
# rolling out; ROLLED_BACK ones are terminal until an operator resumes.
_ACTIVE_STATUSES = frozenset(
    {FlagStatus.ROLLING_OUT, FlagStatus.PAUSED, FlagStatus.SHADOW}
)

_SEVERITY = {
    Action.ROLLBACK: "critical",
    Action.PAUSE: "warning",
}


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one controller pass decided, per flag."""

    decisions: dict[str, Decision]
    canaries: dict[str, CanaryResult]

    def action_for(self, flag_key: str) -> Action | None:
        decision = self.decisions.get(flag_key)
        return decision.action if decision else None


class RolloutController:
    """Advances, pauses, and rolls back staged rollouts."""

    def __init__(
        self,
        repository: FlagRepository,
        quality_store: QualityStore,
        notifier: Notifier,
        clock: Clock | None = None,
        snapshot_publisher=None,
        sample_limit: int = 500,
    ) -> None:
        self._repository = repository
        self._quality = quality_store
        self._notifier = notifier
        self._clock = clock if clock is not None else SystemClock()
        # Optional: when present, every applied action republishes the snapshot
        # so the data plane sees a rollback without waiting for a publisher tick.
        self._publisher = snapshot_publisher
        self._sample_limit = sample_limit

    def tick(self) -> TickResult:
        """Evaluate every active flag once. Never raises."""
        decisions: dict[str, Decision] = {}
        canaries: dict[str, CanaryResult] = {}

        for flag in self._repository.list_flags():
            if flag.status not in _ACTIVE_STATUSES:
                continue
            try:
                decision, canary = self.tick_flag(flag)
            except Exception:
                # One misbehaving flag must not stop the others from being
                # evaluated — including others that may need rolling back.
                logger.exception("controller tick failed for flag %r", flag.key)
                continue
            decisions[flag.key] = decision
            if canary is not None:
                canaries[flag.key] = canary

        return TickResult(decisions=decisions, canaries=canaries)

    def tick_flag(self, flag: FlagDefinition) -> tuple[Decision, CanaryResult | None]:
        """Evaluate and act on one flag."""
        now = self._clock.now()
        state = self._load_state(flag, now)
        samples = self._gather_samples(flag)
        canary = self._run_canary(flag, samples)

        decision = decide(
            state=state,
            plan=flag.rollout_plan,
            policy=flag.quality_policy,
            samples=samples,
            canary=canary.verdict if canary else None,
            now=now,
        )

        self._apply(flag, state, decision, now)
        self._record(flag, decision, canary, now)
        return decision, canary

    # -- evidence gathering -------------------------------------------------- #

    def _gather_samples(
        self, flag: FlagDefinition
    ) -> dict[QualitySignal, list[Sample]]:
        """Read the trailing samples each gate needs.

        An unscored-rate gate is fed the judge-score samples: the unscored rate
        is a property of how those were judged, not a separate measurement, and
        `summarize` derives it from their `scored` flags (see DECISIONS.md D12).
        """
        samples: dict[QualitySignal, list[Sample]] = {}
        for gate in flag.quality_policy.gates:
            source = (
                QualitySignal.JUDGE_SCORE
                if gate.signal is QualitySignal.UNSCORED_RATE
                else gate.signal
            )
            samples[gate.signal] = self._quality.samples(
                flag.key,
                source,
                VariantKind.EXPERIMENTAL,
                limit=self._sample_limit,
            )
        return samples

    def _run_canary(
        self, flag: FlagDefinition, samples: dict[QualitySignal, list[Sample]]
    ) -> CanaryResult | None:
        """Compare experimental against baseline on the policy's primary signal."""
        primary = _primary_gate(flag.quality_policy)
        if primary is None:
            return None

        experimental = [
            sample.value
            for sample in samples.get(primary.signal, [])
            if sample.scored
        ]
        baseline = [
            sample.value
            for sample in self._quality.samples(
                flag.key,
                primary.signal,
                VariantKind.BASELINE,
                limit=self._sample_limit,
            )
            if sample.scored
        ]

        return compare(
            experimental,
            baseline,
            confidence=flag.quality_policy.confidence,
            minimum_samples=flag.quality_policy.minimum_samples,
            # A gate that breaches downward (judge score below 3.0) is a signal
            # where bigger is better; one that breaches upward (latency above
            # 2000ms) is the reverse.
            higher_is_better=primary.comparison is Comparison.BELOW,
        )

    def _load_state(self, flag: FlagDefinition, now: datetime) -> RolloutState:
        stored = self._quality.get_rollout_state(flag.key)
        if stored is None:
            stored = StoredRolloutState(
                flag_key=flag.key, stage_index=0, stage_entered_at=now
            )
            self._quality.save_rollout_state(stored)
        return RolloutState(
            flag_key=flag.key,
            status=flag.status,
            stage_index=min(stored.stage_index, len(flag.rollout_plan.stages) - 1),
            rollout_percentage=flag.rollout_percentage,
            stage_entered_at=stored.stage_entered_at,
            rolled_back_at=stored.rolled_back_at,
        )

    # -- applying the decision ------------------------------------------------ #

    def _apply(
        self,
        flag: FlagDefinition,
        state: RolloutState,
        decision: Decision,
        now: datetime,
    ) -> None:
        if decision.action is Action.HOLD:
            return

        if decision.action is Action.ROLLBACK:
            self._repository.rollback(
                flag.key, actor=CONTROLLER_ACTOR, reason=decision.reason
            )
            self._quality.save_rollout_state(
                StoredRolloutState(
                    flag_key=flag.key,
                    stage_index=state.stage_index,
                    stage_entered_at=state.stage_entered_at,
                    rolled_back_at=now,
                )
            )

        elif decision.action is Action.PAUSE:
            self._repository.set_status(
                flag.key,
                FlagStatus.PAUSED,
                actor=CONTROLLER_ACTOR,
                reason=decision.reason,
            )

        elif decision.action is Action.ADVANCE:
            self._repository.set_rollout_percentage(
                flag.key,
                decision.target_percentage,
                actor=CONTROLLER_ACTOR,
                reason=decision.reason,
            )
            # stage_entered_at moves only because the stage actually changed.
            # Refreshing it on every tick would mean no stage ever matures.
            self._quality.save_rollout_state(
                StoredRolloutState(
                    flag_key=flag.key,
                    stage_index=decision.target_stage_index,
                    stage_entered_at=now,
                    rolled_back_at=state.rolled_back_at,
                )
            )

        elif decision.action is Action.COMPLETE:
            self._repository.set_rollout_percentage(
                flag.key,
                decision.target_percentage,
                actor=CONTROLLER_ACTOR,
                reason=decision.reason,
            )
            self._repository.set_status(
                flag.key,
                FlagStatus.FULLY_ON,
                actor=CONTROLLER_ACTOR,
                reason=decision.reason,
            )

        self._publish()
        self._notify(flag, decision, now)

    def _publish(self) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher.publish(self._repository.snapshot())
        except Exception:
            # The change is already durable in PostgreSQL; a failed publish
            # delays propagation, it does not lose the decision.
            logger.exception("failed to republish the snapshot")

    def _notify(
        self, flag: FlagDefinition, decision: Decision, now: datetime
    ) -> None:
        try:
            self._notifier.send(
                Notification(
                    flag_key=flag.key,
                    action=decision.action.value,
                    reason=decision.reason,
                    at=now,
                    snapshot_version=self._repository.snapshot().version,
                    severity=_SEVERITY.get(decision.action, "info"),
                    detail=_evidence_detail(decision),
                )
            )
        except Exception:
            # A rollback that happened but could not be announced is still a
            # rollback. Never let notification failure undo the safety action.
            logger.exception("failed to notify for flag %r", flag.key)

    def _record(
        self,
        flag: FlagDefinition,
        decision: Decision,
        canary: CanaryResult | None,
        now: datetime,
    ) -> None:
        self._quality.record_decision(
            DecisionRecord(
                flag_key=flag.key,
                action=decision.action.value,
                reason=decision.reason,
                decided_at=now,
                evidence=_evidence_detail(decision),
                canary=canary.as_dict() if canary else None,
            )
        )


def _primary_gate(policy: QualityPolicy):
    """The gate the canary compares on.

    The first gate is the primary one by convention, so the ordering in a policy
    is meaningful rather than incidental.
    """
    return policy.gates[0] if policy.gates else None


def _evidence_detail(decision: Decision) -> dict:
    return {
        signal.value: {
            "count": window.count,
            "mean": window.mean,
            "p10": window.p10,
            "p95": window.p95,
            "unscored_rate": window.unscored_rate,
            "trend": window.trend.value,
        }
        for signal, window in decision.evidence.items()
    }
