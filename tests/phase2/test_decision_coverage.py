"""Every decision the controller can make must be reachable and exercised.

The rollback rules are Phase 2's responsibility while the staged-advance and
canary rules are Phase 3's, so the full decision table lives in
``tests/phase3/test_decision.py``. This module asserts the property that spans
both: no ``Action`` variant exists that nothing can produce.

Written as a live check rather than a comment because the failure it guards
against is silent. Adding an ``Action`` and forgetting to wire it up leaves a
decision the controller can never reach, and nothing else in the suite notices.
"""

import unittest
from datetime import UTC, datetime, timedelta

from aiflags.core.decision import Action, RolloutState, decide
from aiflags.core.models import (
    CanaryVerdict,
    Comparison,
    FlagStatus,
    QualityGate,
    QualityPolicy,
    QualitySignal,
    RolloutPlan,
    Stage,
    Statistic,
)
from aiflags.core.windows import Sample

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

PLAN = RolloutPlan(
    stages=(
        Stage(percentage=5.0, dwell_seconds=3600.0),
        Stage(percentage=100.0, dwell_seconds=3600.0),
    ),
    cooldown_seconds=600.0,
)
POLICY = QualityPolicy(
    gates=(
        QualityGate(
            signal=QualitySignal.JUDGE_SCORE,
            statistic=Statistic.P10,
            comparison=Comparison.BELOW,
            threshold=3.0,
            sustained_evaluations=30,
        ),
    ),
    minimum_samples=30,
)


def scores(value, count):
    return [
        Sample(value=value, at=EPOCH + timedelta(seconds=i), scored=True)
        for i in range(count)
    ]


GOOD = {QualitySignal.JUDGE_SCORE: scores(5.0, 40)}
BAD = {QualitySignal.JUDGE_SCORE: scores(1.0, 40)}


def state(stage_index=0, status=FlagStatus.ROLLING_OUT):
    return RolloutState(
        flag_key="subject_line",
        status=status,
        stage_index=stage_index,
        rollout_percentage=PLAN.stages[stage_index].percentage,
        stage_entered_at=EPOCH,
    )


def decide_at(seconds, samples, canary, **kwargs):
    return decide(
        state=state(**kwargs),
        plan=PLAN,
        policy=POLICY,
        samples=samples,
        canary=canary,
        now=EPOCH + timedelta(seconds=seconds),
    )


class DecisionCoverageTests(unittest.TestCase):
    def test_every_action_variant_is_reachable(self):
        produced = {
            decide_at(0, GOOD, CanaryVerdict.NO_WORSE).action,  # dwell not met
            decide_at(3600, GOOD, CanaryVerdict.NO_WORSE).action,  # advance
            decide_at(3600, GOOD, CanaryVerdict.WORSE).action,  # pause
            decide_at(0, BAD, CanaryVerdict.NO_WORSE).action,  # rollback
            decide_at(
                3600, GOOD, CanaryVerdict.NO_WORSE, stage_index=1
            ).action,  # complete
        }
        self.assertEqual(
            produced,
            set(Action),
            f"unreachable actions: {set(Action) - produced}",
        )

    def test_every_action_carries_a_non_empty_reason(self):
        for seconds, samples, canary, kwargs in (
            (0, GOOD, CanaryVerdict.NO_WORSE, {}),
            (3600, GOOD, CanaryVerdict.NO_WORSE, {}),
            (3600, GOOD, CanaryVerdict.WORSE, {}),
            (0, BAD, CanaryVerdict.NO_WORSE, {}),
            (3600, GOOD, CanaryVerdict.NO_WORSE, {"stage_index": 1}),
            (0, GOOD, None, {"status": FlagStatus.PAUSED}),
            (0, GOOD, None, {"status": FlagStatus.ROLLED_BACK}),
            (0, GOOD, None, {"status": FlagStatus.SHADOW}),
        ):
            decision = decide_at(seconds, samples, canary, **kwargs)
            with self.subTest(action=decision.action):
                self.assertTrue(decision.reason.strip())

    def test_no_status_produces_an_advance_without_a_conclusive_canary(self):
        """Fail-closed, asserted across the whole status space at once."""
        for status in FlagStatus:
            for canary in (None, CanaryVerdict.INCONCLUSIVE, CanaryVerdict.WORSE):
                decision = decide_at(99_999, GOOD, canary, status=status)
                with self.subTest(status=status, canary=canary):
                    self.assertNotIn(
                        decision.action, (Action.ADVANCE, Action.COMPLETE)
                    )


if __name__ == "__main__":
    unittest.main()
