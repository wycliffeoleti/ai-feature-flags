"""The rollout decision function, as a table.

This is the module that decides whether users keep seeing an AI feature. It is
pure — no database, no clock, no model — so every rule below is asserted
directly rather than inferred from an integration run.

The invariant the table is built around: **no input produces an advance unless
the evidence positively supports one.** Thin data holds. Ambiguous statistics
hold. A blind judge rolls back. Only a satisfied dwell time plus a
statistically-no-worse canary advances.
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
        Stage(percentage=1.0, dwell_seconds=7200.0),
        Stage(percentage=5.0, dwell_seconds=21600.0),
        Stage(percentage=100.0, dwell_seconds=86400.0),
    ),
    cooldown_seconds=3600.0,
)

JUDGE_GATE = QualityGate(
    signal=QualitySignal.JUDGE_SCORE,
    statistic=Statistic.P10,
    comparison=Comparison.BELOW,
    threshold=3.0,
    sustained_evaluations=50,
)
UNSCORED_GATE = QualityGate(
    signal=QualitySignal.UNSCORED_RATE,
    statistic=Statistic.RATE,
    comparison=Comparison.ABOVE,
    threshold=0.25,
    sustained_evaluations=20,
)
LATENCY_GATE = QualityGate(
    signal=QualitySignal.LATENCY_MS,
    statistic=Statistic.P95,
    comparison=Comparison.ABOVE,
    threshold=2000.0,
    sustained_evaluations=20,
)

POLICY = QualityPolicy(gates=(JUDGE_GATE,), minimum_samples=30)


def scores(value, count, scored=True, start=EPOCH):
    return [
        Sample(value=value, at=start + timedelta(seconds=i), scored=scored)
        for i in range(count)
    ]


def state(
    stage_index=0,
    status=FlagStatus.ROLLING_OUT,
    entered_at=EPOCH,
    percentage=None,
    rolled_back_at=None,
):
    return RolloutState(
        flag_key="subject_line",
        status=status,
        stage_index=stage_index,
        rollout_percentage=(
            PLAN.stages[stage_index].percentage if percentage is None else percentage
        ),
        stage_entered_at=entered_at,
        rolled_back_at=rolled_back_at,
    )


def decide_at(
    seconds_elapsed=0.0,
    samples=None,
    canary=CanaryVerdict.NO_WORSE,
    policy=POLICY,
    **state_kwargs,
):
    return decide(
        state=state(**state_kwargs),
        plan=PLAN,
        policy=policy,
        samples=samples if samples is not None else {QualitySignal.JUDGE_SCORE: scores(5.0, 60)},
        canary=canary,
        now=EPOCH + timedelta(seconds=seconds_elapsed),
    )


class RollbackTests(unittest.TestCase):
    def test_sustained_breach_rolls_back(self):
        decision = decide_at(
            samples={QualitySignal.JUDGE_SCORE: scores(1.0, 60)}
        )
        self.assertEqual(decision.action, Action.ROLLBACK)
        self.assertEqual(decision.target_percentage, 0.0)

    def test_rollback_wins_over_a_satisfied_dwell_time(self):
        """A ready-to-advance stage must still roll back on bad quality."""
        decision = decide_at(
            seconds_elapsed=99999.0,
            samples={QualitySignal.JUDGE_SCORE: scores(1.0, 60)},
        )
        self.assertEqual(decision.action, Action.ROLLBACK)

    def test_a_handful_of_bad_scores_does_not_breach_a_p10_gate(self):
        """Three bad scores in fifty sit below the tenth percentile — noise."""
        samples = {QualitySignal.JUDGE_SCORE: scores(5.0, 47) + scores(1.0, 3)}
        self.assertNotEqual(decide_at(samples=samples).action, Action.ROLLBACK)

    def test_a_p10_gate_fires_once_a_tenth_of_traffic_goes_bad(self):
        """Documents how sharp a P10 gate is, because it surprises people.

        Twenty percent of outputs rated 1/5 drags the tenth percentile all the
        way to 1.0, so the gate fires even though the *mean* is still 4.2. That
        is the intended behaviour — a mean-based gate would let one user in five
        get a broken answer indefinitely — but it means P10 thresholds must be
        set against the tail you are willing to serve, not against the average.
        """
        samples = {QualitySignal.JUDGE_SCORE: scores(5.0, 40) + scores(1.0, 10)}
        self.assertEqual(decide_at(samples=samples).action, Action.ROLLBACK)

    def test_breach_requires_a_full_sustained_window(self):
        samples = {QualitySignal.JUDGE_SCORE: scores(1.0, 49)}
        self.assertNotEqual(decide_at(samples=samples).action, Action.ROLLBACK)

    def test_breach_fires_at_exactly_the_sustained_count(self):
        samples = {QualitySignal.JUDGE_SCORE: scores(1.0, 50)}
        self.assertEqual(decide_at(samples=samples).action, Action.ROLLBACK)

    def test_an_above_comparison_breaches_upward(self):
        policy = QualityPolicy(gates=(LATENCY_GATE,), minimum_samples=10)
        samples = {QualitySignal.LATENCY_MS: scores(5000.0, 30)}
        decision = decide_at(samples=samples, policy=policy)
        self.assertEqual(decision.action, Action.ROLLBACK)

    def test_a_fast_variant_does_not_breach_a_latency_gate(self):
        policy = QualityPolicy(gates=(LATENCY_GATE,), minimum_samples=10)
        samples = {QualitySignal.LATENCY_MS: scores(100.0, 30)}
        self.assertNotEqual(
            decide_at(samples=samples, policy=policy).action, Action.ROLLBACK
        )

    def test_a_blind_judge_rolls_back_rather_than_reading_as_healthy(self):
        """The failure this whole signal exists for: no scores is not good news."""
        policy = QualityPolicy(gates=(JUDGE_GATE, UNSCORED_GATE), minimum_samples=10)
        samples = {
            QualitySignal.JUDGE_SCORE: scores(0.0, 30, scored=False),
            QualitySignal.UNSCORED_RATE: scores(0.0, 30, scored=False),
        }
        decision = decide_at(samples=samples, policy=policy)
        self.assertEqual(decision.action, Action.ROLLBACK)
        self.assertIn("unscored", decision.reason.lower())

    def test_rollback_reason_names_the_gate_and_the_observed_value(self):
        decision = decide_at(samples={QualitySignal.JUDGE_SCORE: scores(1.0, 60)})
        self.assertIn("judge_score", decision.reason)
        self.assertIn("p10", decision.reason)
        self.assertIn(QualitySignal.JUDGE_SCORE, decision.evidence)

    def test_missing_samples_for_a_gate_do_not_roll_back(self):
        """A signal nobody has reported yet is absence of evidence, not a breach."""
        self.assertNotEqual(
            decide_at(samples={}).action, Action.ROLLBACK
        )


class TerminalAndPausedTests(unittest.TestCase):
    def test_a_rolled_back_flag_stays_put(self):
        decision = decide_at(
            seconds_elapsed=99999.0, status=FlagStatus.ROLLED_BACK
        )
        self.assertEqual(decision.action, Action.HOLD)

    def test_a_rolled_back_flag_is_never_rolled_back_twice(self):
        decision = decide_at(
            status=FlagStatus.ROLLED_BACK,
            samples={QualitySignal.JUDGE_SCORE: scores(1.0, 60)},
        )
        self.assertEqual(decision.action, Action.HOLD)

    def test_a_paused_flag_does_not_advance(self):
        decision = decide_at(seconds_elapsed=99999.0, status=FlagStatus.PAUSED)
        self.assertEqual(decision.action, Action.HOLD)

    def test_a_paused_flag_still_rolls_back_on_bad_quality(self):
        """Pausing stops the ramp; it does not stop the safety net."""
        decision = decide_at(
            status=FlagStatus.PAUSED,
            samples={QualitySignal.JUDGE_SCORE: scores(1.0, 60)},
        )
        self.assertEqual(decision.action, Action.ROLLBACK)

    def test_shadow_mode_never_advances(self):
        decision = decide_at(seconds_elapsed=99999.0, status=FlagStatus.SHADOW)
        self.assertEqual(decision.action, Action.HOLD)
        self.assertIn("shadow", decision.reason.lower())

    def test_a_fully_on_flag_is_complete(self):
        decision = decide_at(seconds_elapsed=99999.0, status=FlagStatus.FULLY_ON)
        self.assertEqual(decision.action, Action.HOLD)


class CooldownTests(unittest.TestCase):
    def test_no_automatic_action_during_the_cooldown_after_a_rollback(self):
        """Guards against flapping if an operator resumes immediately."""
        decision = decide_at(
            seconds_elapsed=99999.0,
            status=FlagStatus.ROLLING_OUT,
            rolled_back_at=EPOCH + timedelta(seconds=99000.0),
        )
        self.assertEqual(decision.action, Action.HOLD)
        self.assertIn("cooldown", decision.reason.lower())

    def test_action_resumes_once_the_cooldown_expires(self):
        decision = decide_at(
            seconds_elapsed=99999.0,
            status=FlagStatus.ROLLING_OUT,
            rolled_back_at=EPOCH,
        )
        self.assertEqual(decision.action, Action.ADVANCE)


class DwellTimeTests(unittest.TestCase):
    def test_a_stage_holds_until_its_dwell_time_elapses(self):
        decision = decide_at(seconds_elapsed=7199.0)
        self.assertEqual(decision.action, Action.HOLD)
        self.assertIn("dwell", decision.reason.lower())

    def test_a_stage_advances_once_the_dwell_time_elapses(self):
        decision = decide_at(seconds_elapsed=7200.0)
        self.assertEqual(decision.action, Action.ADVANCE)
        self.assertEqual(decision.target_stage_index, 1)
        self.assertEqual(decision.target_percentage, 5.0)

    def test_the_final_stage_completes_rather_than_advancing(self):
        decision = decide_at(seconds_elapsed=99999.0, stage_index=2)
        self.assertEqual(decision.action, Action.COMPLETE)
        self.assertEqual(decision.target_percentage, 100.0)


class CanaryGateTests(unittest.TestCase):
    def test_a_worse_canary_pauses_instead_of_advancing(self):
        decision = decide_at(seconds_elapsed=7200.0, canary=CanaryVerdict.WORSE)
        self.assertEqual(decision.action, Action.PAUSE)

    def test_an_inconclusive_canary_holds(self):
        """Fail closed: not enough evidence is not permission to ramp."""
        decision = decide_at(
            seconds_elapsed=7200.0, canary=CanaryVerdict.INCONCLUSIVE
        )
        self.assertEqual(decision.action, Action.HOLD)

    def test_a_missing_canary_holds(self):
        decision = decide_at(seconds_elapsed=7200.0, canary=None)
        self.assertEqual(decision.action, Action.HOLD)

    def test_thin_data_holds_even_with_a_no_worse_canary(self):
        decision = decide_at(
            seconds_elapsed=7200.0,
            samples={QualitySignal.JUDGE_SCORE: scores(5.0, 5)},
        )
        self.assertEqual(decision.action, Action.HOLD)
        self.assertIn("sample", decision.reason.lower())


class EvidenceTests(unittest.TestCase):
    def test_every_decision_records_the_windows_it_used(self):
        decision = decide_at()
        self.assertIn(QualitySignal.JUDGE_SCORE, decision.evidence)
        self.assertEqual(decision.evidence[QualitySignal.JUDGE_SCORE].count, 50)

    def test_every_decision_carries_a_human_readable_reason(self):
        for kwargs in (
            {},
            {"seconds_elapsed": 7200.0},
            {"samples": {QualitySignal.JUDGE_SCORE: scores(1.0, 60)}},
            {"status": FlagStatus.PAUSED},
        ):
            self.assertTrue(decide_at(**kwargs).reason.strip())


if __name__ == "__main__":
    unittest.main()
