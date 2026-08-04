"""The rollout controller.

Drives the whole loop on a `FakeClock`, so the guide's real schedule — 1% for two
hours, then 5% for six — is exercised in milliseconds without a "test mode"
branch anywhere in the controller.

The controller holds no policy; `decide()` does. So these tests are about the
I/O: that the right evidence is gathered, the verdict is applied faithfully, the
stage clock is managed correctly, and no failure in a peripheral concern
(notification, publishing) can undo a safety action.
"""

import unittest
from datetime import UTC, datetime, timedelta

from aiflags.clock import FakeClock
from aiflags.core.decision import Action
from aiflags.core.models import (
    Comparison,
    FlagDefinition,
    FlagStatus,
    QualityGate,
    QualityPolicy,
    QualitySignal,
    RolloutPlan,
    Stage,
    Statistic,
    Variant,
    VariantKind,
)
from aiflags.notify.recording import RecordingNotifier
from aiflags.store.memory import InMemoryFlagRepository
from aiflags.store.quality import (
    InMemoryQualityStore,
    QualityObservation,
    StoredRolloutState,
)
from aiflags.workers.controller import RolloutController

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

PLAN = RolloutPlan(
    stages=(
        Stage(percentage=1.0, dwell_seconds=7200.0),
        Stage(percentage=5.0, dwell_seconds=21600.0),
        Stage(percentage=100.0, dwell_seconds=86400.0),
    ),
    cooldown_seconds=3600.0,
)
POLICY = QualityPolicy(
    gates=(
        QualityGate(
            signal=QualitySignal.JUDGE_SCORE,
            statistic=Statistic.P10,
            comparison=Comparison.BELOW,
            threshold=3.0,
            sustained_evaluations=40,
        ),
    ),
    minimum_samples=40,
)


class FailingNotifier:
    def send(self, notification):
        raise ConnectionError("slack unreachable")


class FailingPublisher:
    def publish(self, snapshot):
        raise ConnectionError("redis unreachable")


class RecordingPublisher:
    def __init__(self):
        self.published = []

    def publish(self, snapshot):
        self.published.append(snapshot)
        return True


class ControllerTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryFlagRepository()
        self.quality = InMemoryQualityStore()
        self.notifier = RecordingNotifier()
        self.clock = FakeClock(EPOCH)
        self.publisher = RecordingPublisher()
        self.controller = RolloutController(
            repository=self.repo,
            quality_store=self.quality,
            notifier=self.notifier,
            clock=self.clock,
            snapshot_publisher=self.publisher,
        )
        self.repo.create_flag(self.make_flag(), actor="w", reason="initial")

    def make_flag(self, **overrides):
        params = {
            "key": "subject_line",
            "baseline": Variant(key="v1", kind=VariantKind.BASELINE),
            "experimental": Variant(key="v2", kind=VariantKind.EXPERIMENTAL),
            "quality_policy": POLICY,
            "rollout_plan": PLAN,
            "status": FlagStatus.ROLLING_OUT,
            "rollout_percentage": 1.0,
        }
        params.update(overrides)
        return FlagDefinition(**params)

    def observe(self, value, count, kind=VariantKind.EXPERIMENTAL, scored=True):
        self.quality.record_observations(
            [
                QualityObservation(
                    flag_key="subject_line",
                    evaluation_id=f"eval-{kind}-{i}",
                    variant_kind=kind,
                    signal=QualitySignal.JUDGE_SCORE,
                    value=value if scored else None,
                    scored=scored,
                    occurred_at=EPOCH + timedelta(seconds=i),
                )
                for i in range(count)
            ]
        )

    def flag(self):
        return self.repo.get_flag("subject_line")

    def tick(self):
        return self.controller.tick()


class HoldTests(ControllerTestCase):
    def test_a_stage_holds_before_its_dwell_time(self):
        self.observe(5.0, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        result = self.tick()
        self.assertEqual(result.action_for("subject_line"), Action.HOLD)
        self.assertEqual(self.flag().rollout_percentage, 1.0)

    def test_holds_are_recorded(self):
        """"Why did this sit at 1% for hours" must be answerable."""
        self.observe(5.0, 60)
        self.tick()
        decisions = self.quality.decisions("subject_line")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "hold")
        self.assertTrue(decisions[0].reason.strip())

    def test_a_hold_publishes_nothing_and_notifies_nobody(self):
        self.observe(5.0, 60)
        self.tick()
        self.assertEqual(self.publisher.published, [])
        self.assertEqual(self.notifier.sent, [])

    def test_thin_data_holds_even_after_the_dwell_time(self):
        self.observe(5.0, 5)
        self.observe(5.0, 5, kind=VariantKind.BASELINE)
        self.clock.advance(7200.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.HOLD)


class AdvanceTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.observe(5.0, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        # A live controller ticks continuously, so the first tick after a flag
        # is created is what starts its dwell clock. Advancing the clock before
        # ever ticking would model a controller that was switched off.
        self.tick()

    def test_the_dwell_clock_starts_when_the_controller_first_sees_the_flag(self):
        """A flag created while the controller was down does not arrive pre-aged."""
        self.clock.advance(7200.0)
        self.repo.create_flag(self.make_flag(key="late"), actor="w", reason="r")
        self.assertEqual(self.tick().action_for("late"), Action.HOLD)
        self.assertEqual(
            self.quality.get_rollout_state("late").stage_entered_at,
            EPOCH + timedelta(seconds=7200),
        )

    def test_a_healthy_stage_advances_after_its_dwell_time(self):
        self.clock.advance(7200.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.ADVANCE)
        self.assertEqual(self.flag().rollout_percentage, 5.0)

    def test_advancing_resets_the_stage_clock(self):
        """Otherwise the next stage would mature instantly off the old timestamp."""
        self.clock.advance(7200.0)
        self.tick()
        state = self.quality.get_rollout_state("subject_line")
        self.assertEqual(state.stage_index, 1)
        self.assertEqual(state.stage_entered_at, EPOCH + timedelta(seconds=7200))

    def test_the_next_stage_holds_for_its_own_dwell(self):
        self.clock.advance(7200.0)
        self.tick()
        self.clock.advance(21599.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.HOLD)
        self.assertEqual(self.flag().rollout_percentage, 5.0)

    def test_an_advance_is_attributed_to_the_controller(self):
        self.clock.advance(7200.0)
        self.tick()
        event = self.repo.audit_events("subject_line")[-1]
        self.assertEqual(event.actor, "rollout-controller")
        self.assertIn("advancing", event.reason)

    def test_an_advance_republishes_the_snapshot(self):
        self.clock.advance(7200.0)
        self.tick()
        self.assertEqual(len(self.publisher.published), 1)
        self.assertEqual(
            self.publisher.published[0].flags["subject_line"].rollout_percentage, 5.0
        )

    def test_the_full_schedule_runs_to_completion(self):
        """The guide's ramp, driven end to end on a fake clock."""
        actions = []
        for dwell in (7200.0, 21600.0, 86400.0):
            self.clock.advance(dwell)
            actions.append(self.tick().action_for("subject_line"))
        self.assertEqual(
            actions, [Action.ADVANCE, Action.ADVANCE, Action.COMPLETE]
        )
        self.assertEqual(self.flag().status, FlagStatus.FULLY_ON)
        self.assertEqual(self.flag().rollout_percentage, 100.0)


class RollbackTests(ControllerTestCase):
    def test_a_sustained_breach_rolls_back(self):
        self.observe(1.0, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.assertEqual(self.tick().action_for("subject_line"), Action.ROLLBACK)
        flag = self.flag()
        self.assertEqual(flag.status, FlagStatus.ROLLED_BACK)
        self.assertEqual(flag.rollout_percentage, 0.0)

    def test_a_rollback_beats_a_matured_stage(self):
        self.observe(1.0, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.clock.advance(99_999.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.ROLLBACK)

    def test_a_rollback_alerts_with_the_quality_data(self):
        self.observe(1.0, 60)
        self.tick()
        self.assertEqual(len(self.notifier.sent), 1)
        notification = self.notifier.sent[0]
        self.assertEqual(notification.severity, "critical")
        body = notification.as_slack_payload()["blocks"][0]["text"]["text"]
        self.assertIn("judge_score", body)
        self.assertIn("p10", body.lower())

    def test_a_rollback_records_its_evidence(self):
        self.observe(1.0, 60)
        self.tick()
        decision = self.quality.decisions("subject_line")[-1]
        self.assertEqual(decision.action, "rollback")
        self.assertEqual(decision.evidence["judge_score"]["count"], 40)
        self.assertIsNotNone(decision.evidence["judge_score"]["p10"])

    def test_a_rollback_stamps_the_rollback_time(self):
        self.observe(1.0, 60)
        self.tick()
        self.assertEqual(
            self.quality.get_rollout_state("subject_line").rolled_back_at,
            EPOCH,
        )

    def test_a_rolled_back_flag_is_left_alone_afterwards(self):
        self.observe(1.0, 60)
        self.tick()
        self.clock.advance(99_999.0)
        self.assertIsNone(self.tick().action_for("subject_line"))

    def test_a_blind_judge_rolls_back(self):
        """Every sample unscored is not the same as every sample fine."""
        flag = self.make_flag(
            quality_policy=QualityPolicy(
                gates=(
                    QualityGate(
                        signal=QualitySignal.UNSCORED_RATE,
                        statistic=Statistic.RATE,
                        comparison=Comparison.ABOVE,
                        threshold=0.25,
                        sustained_evaluations=20,
                    ),
                ),
                minimum_samples=10,
            ),
            key="blind_flag",
        )
        self.repo.create_flag(flag, actor="w", reason="r")
        self.quality.record_observations(
            [
                QualityObservation(
                    flag_key="blind_flag",
                    evaluation_id=f"eval-{i}",
                    variant_kind=VariantKind.EXPERIMENTAL,
                    signal=QualitySignal.JUDGE_SCORE,
                    value=None,
                    scored=False,
                    occurred_at=EPOCH,
                    reason="judge timed out",
                )
                for i in range(30)
            ]
        )
        self.assertEqual(self.tick().action_for("blind_flag"), Action.ROLLBACK)


class CanaryGateTests(ControllerTestCase):
    def test_a_worse_variant_pauses_instead_of_advancing(self):
        self.observe(3.5, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.tick()
        self.clock.advance(7200.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.PAUSE)
        self.assertEqual(self.flag().status, FlagStatus.PAUSED)
        self.assertEqual(self.flag().rollout_percentage, 1.0)

    def test_a_paused_flag_holds_at_its_percentage(self):
        self.observe(3.5, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.tick()
        self.clock.advance(7200.0)
        self.tick()
        self.clock.advance(7200.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.HOLD)
        self.assertEqual(self.flag().rollout_percentage, 1.0)

    def test_the_canary_result_is_recorded_with_the_decision(self):
        self.observe(5.0, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.tick()
        decision = self.quality.decisions("subject_line")[-1]
        self.assertIsNotNone(decision.canary)
        self.assertIn(decision.canary["verdict"], ("no_worse", "worse", "inconclusive"))
        self.assertIn("n_experimental", decision.canary)

    def test_no_baseline_traffic_holds_rather_than_advancing(self):
        """A 1% stage with no baseline samples cannot be compared."""
        self.observe(5.0, 60)
        self.tick()
        self.clock.advance(7200.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.HOLD)


class ShadowModeTests(ControllerTestCase):
    def test_a_shadow_flag_never_advances(self):
        self.repo.set_status(
            "subject_line", FlagStatus.SHADOW, actor="w", reason="dark launch"
        )
        self.observe(5.0, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.clock.advance(99_999.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.HOLD)
        self.assertEqual(self.flag().rollout_percentage, 1.0)

    def test_shadow_samples_do_not_feed_a_live_rollout_decision(self):
        """Shadow output is scored, but must not be what advances a rollout."""
        self.quality.record_observations(
            [
                QualityObservation(
                    flag_key="subject_line",
                    evaluation_id=f"shadow-{i}",
                    variant_kind=VariantKind.EXPERIMENTAL,
                    signal=QualitySignal.JUDGE_SCORE,
                    value=5.0,
                    scored=True,
                    occurred_at=EPOCH,
                    is_shadow=True,
                )
                for i in range(60)
            ]
        )
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.tick()
        self.clock.advance(7200.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.HOLD)


class ScopeTests(ControllerTestCase):
    def test_inactive_flags_are_skipped(self):
        for status in (FlagStatus.OFF, FlagStatus.FULLY_ON, FlagStatus.ROLLED_BACK):
            with self.subTest(status=status):
                self.repo.set_status(
                    "subject_line", status, actor="w", reason="r"
                )
                self.assertIsNone(self.tick().action_for("subject_line"))

    def test_one_broken_flag_does_not_stop_the_others(self):
        """A flag that needs rolling back must still be reached."""
        self.repo.create_flag(
            self.make_flag(key="healthy"), actor="w", reason="r"
        )
        self.observe(1.0, 60)
        original = self.quality.get_rollout_state

        def explode(flag_key):
            if flag_key == "healthy":
                raise RuntimeError("state unreadable")
            return original(flag_key)

        self.quality.get_rollout_state = explode
        result = self.tick()
        self.assertEqual(result.action_for("subject_line"), Action.ROLLBACK)
        self.assertIsNone(result.action_for("healthy"))


class PeripheralFailureTests(ControllerTestCase):
    def test_a_notification_failure_does_not_undo_the_rollback(self):
        controller = RolloutController(
            repository=self.repo,
            quality_store=self.quality,
            notifier=FailingNotifier(),
            clock=self.clock,
            snapshot_publisher=self.publisher,
        )
        self.observe(1.0, 60)
        self.assertEqual(
            controller.tick().action_for("subject_line"), Action.ROLLBACK
        )
        self.assertEqual(self.flag().status, FlagStatus.ROLLED_BACK)

    def test_a_publish_failure_does_not_undo_the_rollback(self):
        controller = RolloutController(
            repository=self.repo,
            quality_store=self.quality,
            notifier=self.notifier,
            clock=self.clock,
            snapshot_publisher=FailingPublisher(),
        )
        self.observe(1.0, 60)
        self.assertEqual(
            controller.tick().action_for("subject_line"), Action.ROLLBACK
        )
        self.assertEqual(self.flag().rollout_percentage, 0.0)

    def test_the_controller_runs_without_a_publisher(self):
        controller = RolloutController(
            repository=self.repo,
            quality_store=self.quality,
            notifier=self.notifier,
            clock=self.clock,
        )
        self.observe(1.0, 60)
        self.assertEqual(
            controller.tick().action_for("subject_line"), Action.ROLLBACK
        )


class CooldownTests(ControllerTestCase):
    """The cooldown only bites when it outlasts the stage dwell.

    With the default plan the 1-hour cooldown expires long before the 2-hour
    dwell, so it can never be the binding constraint. These tests use a plan
    whose cooldown is longer than the dwell — the configuration where an
    operator resuming a rolled-back flag could otherwise cause it to flap.
    """

    def setUp(self):
        super().setUp()
        long_cooldown = RolloutPlan(
            stages=PLAN.stages, cooldown_seconds=86400.0
        )
        self.repo.replace_flag(
            self.make_flag(rollout_plan=long_cooldown), actor="w", reason="r"
        )

    def test_no_action_is_taken_during_the_cooldown_after_a_rollback(self):
        self.quality.save_rollout_state(
            StoredRolloutState(
                flag_key="subject_line",
                stage_index=0,
                stage_entered_at=EPOCH,
                rolled_back_at=EPOCH,
            )
        )
        self.observe(5.0, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.clock.advance(7200.0)
        decision = self.tick().decisions["subject_line"]
        self.assertEqual(decision.action, Action.HOLD)
        self.assertIn("cooldown", decision.reason.lower())

    def test_action_resumes_once_the_cooldown_expires(self):
        self.quality.save_rollout_state(
            StoredRolloutState(
                flag_key="subject_line",
                stage_index=0,
                stage_entered_at=EPOCH,
                rolled_back_at=EPOCH,
            )
        )
        self.observe(5.0, 60)
        self.observe(5.0, 60, kind=VariantKind.BASELINE)
        self.clock.advance(86401.0)
        self.assertEqual(self.tick().action_for("subject_line"), Action.ADVANCE)


if __name__ == "__main__":
    unittest.main()
