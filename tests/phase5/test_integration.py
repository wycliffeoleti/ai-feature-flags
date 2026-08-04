"""The four integration tests the guide names, plus the demo scenario.

These run the real stack — SDK, queue, judge, evaluator, controller, stores —
wired together, rather than exercising modules in isolation. Every unit is
already tested; what these prove is that the seams hold when the pieces are
connected.

The whole suite runs on a `FakeClock`, so the guide's real schedule (1% for two
hours, 5% for six, and so on) is exercised without any duration being shortened
for the test. The plan under test is the plan that would ship.
"""

import unittest

from aiflags.core.models import (
    EvaluationContext,
    EvaluationReason,
    FlagStatus,
    QualitySignal,
    VariantKind,
)
from aiflags.demo.generator import BROKEN_TEMPLATE, GOOD_TEMPLATE
from aiflags.demo.scenario import (
    DEMO_PLAN,
    Demo,
    run_bad_variant,
    run_good_variant,
)
from aiflags.queue import InMemoryOutcomeQueue
from aiflags.sdk import FlagClient


class ConsistentAssignmentTests(unittest.TestCase):
    """Guide test 1: consistent user assignment across evaluations."""

    def setUp(self):
        self.demo = Demo()
        self.demo.create_flag("subject_line", GOOD_TEMPLATE)
        self.demo.start_rollout("subject_line")
        self.demo.client.refresh()

    def evaluate(self, subject):
        return self.demo.client.evaluate(
            "subject_line", EvaluationContext(subject_key=subject)
        )

    def test_a_subject_gets_the_same_variant_every_time(self):
        subjects = [f"user-{i}" for i in range(500)]
        first = {s: self.evaluate(s).variant.key for s in subjects}
        second = {s: self.evaluate(s).variant.key for s in subjects}
        self.assertEqual(first, second)

    def test_assignment_survives_an_unrelated_flag_being_edited(self):
        subjects = [f"user-{i}" for i in range(500)]
        before = {s: self.evaluate(s).variant.key for s in subjects}

        self.demo.create_flag("unrelated", GOOD_TEMPLATE)
        self.demo.repository.set_rollout_percentage(
            "unrelated", 50.0, actor="w", reason="unrelated change"
        )
        self.demo.client.refresh()

        after = {s: self.evaluate(s).variant.key for s in subjects}
        self.assertEqual(before, after)

    def test_raising_the_percentage_only_adds_subjects(self):
        """A ramp that moved subjects back would corrupt the quality comparison."""
        subjects = [f"user-{i}" for i in range(500)]
        previous: set[str] = set()
        for percentage in (1.0, 5.0, 25.0, 100.0):
            self.demo.repository.set_rollout_percentage(
                "subject_line", percentage, actor="w", reason=f"ramp to {percentage}"
            )
            self.demo.client.refresh()
            current = {s for s in subjects if self.evaluate(s).is_experimental}
            self.assertTrue(
                previous <= current,
                f"ramp to {percentage}% dropped {previous - current}",
            )
            previous = current


class AutomaticRollbackTests(unittest.TestCase):
    """Guide test 2: automatic rollback triggers on quality degradation."""

    def test_a_broken_prompt_variant_is_rolled_back(self):
        result = run_bad_variant()
        self.assertTrue(result.rolled_back)
        self.assertEqual(result.final_percentage, 0.0)

    def test_the_rollback_names_the_gate_and_the_observed_value(self):
        result = run_bad_variant()
        self.assertIsNotNone(result.rollback_reason)
        self.assertIn("judge_score", result.rollback_reason)
        self.assertIn("p10", result.rollback_reason)
        self.assertIn("3", result.rollback_reason)

    def test_the_rollback_alerts(self):
        self.assertGreaterEqual(run_bad_variant().notifications, 1)

    def test_the_rollback_is_attributed_in_the_audit_trail(self):
        demo = Demo()
        demo.create_flag("broken", BROKEN_TEMPLATE)
        demo.start_rollout("broken")
        demo.run("broken")
        event = demo.repository.audit_events("broken")[-1]
        self.assertEqual(event.action, "rollback")
        self.assertEqual(event.actor, "rollout-controller")
        self.assertIn("judge_score", event.reason)

    def test_the_rollback_evidence_is_recorded(self):
        demo = Demo()
        demo.create_flag("broken", BROKEN_TEMPLATE)
        demo.start_rollout("broken")
        demo.run("broken")
        decision = demo.quality.decisions("broken")[-1]
        self.assertEqual(decision.action, "rollback")
        self.assertGreaterEqual(decision.evidence["judge_score"]["count"], 50)
        self.assertLess(decision.evidence["judge_score"]["p10"], 3.0)

    def test_users_stop_seeing_the_broken_variant_after_rollback(self):
        """The point of the whole system, asserted on real traffic."""
        demo = Demo()
        demo.create_flag("broken", BROKEN_TEMPLATE)
        demo.start_rollout("broken")
        demo.run("broken")
        demo.client.refresh()
        for i in range(500):
            result = demo.client.evaluate(
                "broken", EvaluationContext(subject_key=f"user-{i}")
            )
            self.assertFalse(result.is_experimental)
            self.assertNotIn("{customer_name}", result.variant.config["template"])


class StagedAdvanceTests(unittest.TestCase):
    """Guide test 3: staged rollout advances correctly on quality thresholds."""

    def test_a_good_variant_reaches_full_rollout(self):
        result = run_good_variant()
        self.assertTrue(result.fully_rolled_out)
        self.assertEqual(result.final_percentage, 100.0)

    def test_the_rollout_advances_one_stage_at_a_time(self):
        result = run_good_variant()
        advances = [a for a in result.actions if a in ("advance", "complete")]
        self.assertEqual(len(advances), len(DEMO_PLAN.stages) - 1 + 1)

    def test_every_stage_percentage_is_visited_in_order(self):
        demo = Demo()
        demo.create_flag("good", GOOD_TEMPLATE)
        demo.start_rollout("good")
        demo.run("good")
        percentages = [
            event.detail["percentage"]
            for event in demo.repository.audit_events("good")
            if event.action == "set_rollout_percentage"
        ]
        self.assertEqual(percentages, [1.0, 5.0, 25.0, 100.0])

    def test_advances_are_attributed_to_the_controller(self):
        demo = Demo()
        demo.create_flag("good", GOOD_TEMPLATE)
        demo.start_rollout("good")
        demo.run("good")
        controller_events = [
            event
            for event in demo.repository.audit_events("good")
            if event.actor == "rollout-controller"
        ]
        self.assertTrue(controller_events)
        for event in controller_events:
            self.assertTrue(event.reason.strip())

    def test_the_first_tick_holds_before_any_dwell_has_elapsed(self):
        result = run_good_variant()
        self.assertEqual(result.actions[0], "hold")

    def test_a_good_rollout_records_no_rollback(self):
        self.assertIsNone(run_good_variant().rollback_reason)


class SdkOutageTests(unittest.TestCase):
    """Guide test 4: the SDK gracefully handles flag service outages."""

    def setUp(self):
        self.demo = Demo()
        self.demo.create_flag("subject_line", GOOD_TEMPLATE)
        self.demo.start_rollout("subject_line")
        self.demo.repository.set_rollout_percentage(
            "subject_line", 100.0, actor="w", reason="full rollout"
        )
        self.demo.client.refresh()

    def context(self, index=0):
        return EvaluationContext(subject_key=f"user-{index}")

    def test_traffic_keeps_flowing_when_the_service_dies(self):
        class DeadSource:
            def fetch(self):
                raise ConnectionError("flag service unreachable")

        self.demo.client._source = DeadSource()
        for _ in range(10):
            self.assertFalse(self.demo.client.refresh())

        # The last good snapshot keeps serving; the rollout does not stall.
        self.assertTrue(self.demo.client.evaluate("subject_line", self.context()).is_experimental)

    def test_a_client_that_never_reached_the_service_serves_baseline(self):
        class DeadSource:
            def fetch(self):
                raise ConnectionError("flag service unreachable")

        cold = FlagClient(source=DeadSource(), sink=InMemoryOutcomeQueue())
        result = cold.evaluate("subject_line", self.context())
        self.assertEqual(result.reason, EvaluationReason.NO_SNAPSHOT)
        self.assertTrue(result.is_degraded)

    def test_a_dead_outcome_sink_does_not_break_the_request_path(self):
        class DeadSink:
            def send(self, batch):
                raise ConnectionError("queue unreachable")

        self.demo.client._sink = DeadSink()
        for _ in range(100):
            result = self.demo.client.evaluate("subject_line", self.context())
            self.demo.client.record_outcome(result, output="x", latency_ms=1.0)
        self.assertEqual(self.demo.client.flush(), 0)
        # Still serving correctly despite telemetry being unable to leave.
        self.assertTrue(
            self.demo.client.evaluate("subject_line", self.context()).is_experimental
        )

    def test_outcomes_are_shed_rather_than_grown_without_limit(self):
        client = FlagClient(
            source=self.demo.client._source,
            sink=InMemoryOutcomeQueue(),
            clock=self.demo.clock,
            max_staleness_seconds=None,
            buffer_capacity=50,
        )
        client.refresh()
        for _ in range(500):
            result = client.evaluate("subject_line", self.context())
            client.record_outcome(result, output="x", latency_ms=1.0)
        self.assertEqual(client.pending_outcomes, 50)
        self.assertEqual(client.dropped_outcomes, 450)

    def test_a_stale_snapshot_degrades_to_baseline(self):
        client = FlagClient(
            source=self.demo.client._source,
            sink=InMemoryOutcomeQueue(),
            clock=self.demo.clock,
            max_staleness_seconds=60.0,
        )
        client.refresh()
        self.assertTrue(client.evaluate("subject_line", self.context()).is_experimental)
        self.demo.clock.advance(61.0)
        result = client.evaluate("subject_line", self.context())
        self.assertEqual(result.reason, EvaluationReason.SNAPSHOT_STALE)
        self.assertFalse(result.is_experimental)


class ShadowModeIntegrationTests(unittest.TestCase):
    """Shadow mode across the whole stack, since it spans SDK and controller."""

    def setUp(self):
        self.demo = Demo()
        self.demo.create_flag("shadowed", BROKEN_TEMPLATE)
        self.demo.repository.set_status(
            "shadowed", FlagStatus.SHADOW, actor="w", reason="dark launch"
        )

    def test_no_user_sees_the_shadow_variant(self):
        self.demo.serve("shadowed", requests=500)
        self.demo.score()
        self.demo.client.refresh()
        for i in range(200):
            result = self.demo.client.evaluate(
                "shadowed", EvaluationContext(subject_key=f"user-{i}")
            )
            self.assertFalse(result.is_experimental)
            self.assertNotIn("{customer_name}", result.variant.config["template"])

    def test_shadow_output_is_still_scored(self):
        self.demo.serve("shadowed", requests=500)
        self.demo.score()
        shadow = self.demo.quality.samples(
            "shadowed",
            QualitySignal.JUDGE_SCORE,
            VariantKind.EXPERIMENTAL,
            include_shadow=True,
        )
        self.assertTrue(shadow)
        self.assertTrue(all(s.value < 3.0 for s in shadow if s.scored))

    def test_shadow_scores_never_advance_the_rollout(self):
        for _ in range(4):
            self.demo.serve("shadowed", requests=2000)
            self.demo.score()
            self.demo.controller.tick()
            self.demo.clock.advance(86400.0)
        flag = self.demo.repository.get_flag("shadowed")
        self.assertEqual(flag.status, FlagStatus.SHADOW)
        self.assertEqual(flag.rollout_percentage, 0.0)


class ScenarioTests(unittest.TestCase):
    """The demo as a whole, which is also the portfolio artefact."""

    def test_the_scenario_is_deterministic(self):
        """A demo that varies between runs is not evidence of anything."""
        first, second = run_bad_variant(), run_bad_variant()
        self.assertEqual(first.actions, second.actions)
        self.assertEqual(first.rollback_reason, second.rollback_reason)

    def test_the_broken_variant_actually_produced_broken_output(self):
        """Guard against the demo passing because nothing was generated."""
        demo = Demo()
        demo.create_flag("broken", BROKEN_TEMPLATE)
        demo.start_rollout("broken")
        demo.run("broken")
        self.assertGreater(demo.generator.calls, 0)
        samples = demo.quality.samples(
            "broken", QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL, limit=500
        )
        self.assertGreaterEqual(len(samples), 50)
        self.assertTrue(all(s.value < 3.0 for s in samples if s.scored))

    def test_baseline_traffic_scored_well_throughout(self):
        """The rollback must be caused by the variant, not by a broken judge."""
        demo = Demo()
        demo.create_flag("broken", BROKEN_TEMPLATE)
        demo.start_rollout("broken")
        demo.run("broken")
        baseline = demo.quality.samples(
            "broken", QualitySignal.JUDGE_SCORE, VariantKind.BASELINE, limit=500
        )
        self.assertTrue(baseline)
        self.assertTrue(all(s.value >= 4.0 for s in baseline if s.scored))


if __name__ == "__main__":
    unittest.main()
