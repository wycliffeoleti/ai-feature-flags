"""Dashboard view models, rendering, and routes.

Split deliberately: the numbers are asserted against the view model, not by
grepping HTML, so a layout change cannot break a test about arithmetic. The
rendering tests cover only what rendering is responsible for — escaping, and not
presenting a blind window as a healthy one.
"""

import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from aiflags.api.app import create_app
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
from aiflags.dashboard.data import (
    build_analytics,
    build_overview,
    build_overviews,
    format_duration,
)
from aiflags.dashboard.render import (
    render_analytics,
    render_flag_detail,
    render_overview,
)
from aiflags.store.memory import InMemoryFlagRepository
from aiflags.store.quality import (
    DecisionRecord,
    InMemoryQualityStore,
    QualityObservation,
    StoredRolloutState,
)

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

PLAN = RolloutPlan(
    stages=(
        Stage(percentage=1.0, dwell_seconds=7200.0),
        Stage(percentage=5.0, dwell_seconds=21600.0),
        Stage(percentage=100.0, dwell_seconds=86400.0),
    )
)
POLICY = QualityPolicy(
    gates=(
        QualityGate(
            signal=QualitySignal.JUDGE_SCORE,
            statistic=Statistic.P10,
            comparison=Comparison.BELOW,
            threshold=3.0,
        ),
    )
)


def make_flag(key="subject_line", **overrides):
    params = {
        "key": key,
        "baseline": Variant(key="v1", kind=VariantKind.BASELINE),
        "experimental": Variant(key="v2", kind=VariantKind.EXPERIMENTAL),
        "quality_policy": POLICY,
        "rollout_plan": PLAN,
        "status": FlagStatus.ROLLING_OUT,
        "rollout_percentage": 5.0,
    }
    params.update(overrides)
    return FlagDefinition(**params)


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryFlagRepository()
        self.quality = InMemoryQualityStore()
        self.repo.create_flag(make_flag(), actor="wycliffe", reason="initial")

    def observe(self, value, count, kind=VariantKind.EXPERIMENTAL, scored=True,
                flag_key="subject_line"):
        self.quality.record_observations(
            [
                QualityObservation(
                    flag_key=flag_key,
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

    def overview(self, key="subject_line"):
        return build_overview(self.repo.get_flag(key), self.quality)


class OverviewDataTests(DashboardTestCase):
    def test_quality_means_are_reported_for_both_variants(self):
        self.observe(4.0, 20)
        self.observe(5.0, 20, kind=VariantKind.BASELINE)
        overview = self.overview()
        self.assertAlmostEqual(overview.experimental.mean, 4.0)
        self.assertAlmostEqual(overview.baseline.mean, 5.0)

    def test_the_quality_delta_is_experimental_minus_baseline(self):
        self.observe(4.0, 20)
        self.observe(5.0, 20, kind=VariantKind.BASELINE)
        self.assertAlmostEqual(self.overview().quality_delta, -1.0)

    def test_the_delta_is_absent_without_baseline_traffic(self):
        self.observe(4.0, 20)
        self.assertIsNone(self.overview().quality_delta)

    def test_a_window_with_no_scored_samples_is_blind(self):
        self.observe(None, 20, scored=False)
        overview = self.overview()
        self.assertTrue(overview.is_blind)
        self.assertIsNone(overview.experimental.mean)

    def test_the_stage_label_is_one_indexed_for_humans(self):
        self.quality.save_rollout_state(
            StoredRolloutState("subject_line", 1, EPOCH)
        )
        self.assertEqual(self.overview().stage_label, "2 of 3")

    def test_the_next_percentage_is_the_upcoming_stage(self):
        self.quality.save_rollout_state(
            StoredRolloutState("subject_line", 1, EPOCH)
        )
        self.assertEqual(self.overview().next_percentage, 100.0)

    def test_the_final_stage_has_no_next_percentage(self):
        self.quality.save_rollout_state(
            StoredRolloutState("subject_line", 2, EPOCH)
        )
        self.assertIsNone(self.overview().next_percentage)

    def test_the_best_case_estimate_sums_the_remaining_dwells(self):
        self.quality.save_rollout_state(
            StoredRolloutState("subject_line", 1, EPOCH)
        )
        self.assertEqual(
            self.overview().optimistic_seconds_to_full, 21600.0 + 86400.0
        )

    def test_a_finished_flag_has_no_remaining_estimate(self):
        """A rolled-back or fully-on flag has no meaningful time to completion."""
        for status in (FlagStatus.FULLY_ON, FlagStatus.ROLLED_BACK, FlagStatus.OFF):
            with self.subTest(status=status):
                self.repo.set_status("subject_line", status, actor="w", reason="r")
                self.assertIsNone(self.overview().optimistic_seconds_to_full)

    def test_the_latest_decision_is_surfaced(self):
        for action in ("hold", "advance"):
            self.quality.record_decision(
                DecisionRecord(
                    flag_key="subject_line",
                    action=action,
                    reason=f"{action} reason",
                    decided_at=EPOCH,
                )
            )
        self.assertEqual(self.overview().latest_decision.action, "advance")

    def test_overviews_are_sorted_by_key(self):
        self.repo.create_flag(make_flag("aaa"), actor="w", reason="r")
        self.repo.create_flag(make_flag("zzz"), actor="w", reason="r")
        keys = [o.key for o in build_overviews(self.repo, self.quality)]
        self.assertEqual(keys, sorted(keys))

    def test_an_unscored_rate_gate_charts_the_judge_scores(self):
        """That gate has no samples of its own; charting it would show nothing."""
        self.repo.create_flag(
            make_flag(
                "blind",
                quality_policy=QualityPolicy(
                    gates=(
                        QualityGate(
                            signal=QualitySignal.UNSCORED_RATE,
                            statistic=Statistic.RATE,
                            comparison=Comparison.ABOVE,
                            threshold=0.25,
                        ),
                    )
                ),
            ),
            actor="w",
            reason="r",
        )
        self.observe(4.0, 10, flag_key="blind")
        self.assertAlmostEqual(self.overview("blind").experimental.mean, 4.0)


class DurationFormattingTests(unittest.TestCase):
    def test_durations_render_the_way_a_schedule_reads(self):
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(120), "2m")
        self.assertEqual(format_duration(7200), "2.0h")
        self.assertEqual(format_duration(172800), "2.0d")

    def test_an_absent_duration_renders_as_a_dash(self):
        self.assertEqual(format_duration(None), "—")


class AnalyticsDataTests(DashboardTestCase):
    def test_rollbacks_are_collected_with_their_causes(self):
        self.quality.record_decision(
            DecisionRecord(
                flag_key="subject_line",
                action="rollback",
                reason="judge_score p10 of 1.8 below 3.0",
                decided_at=EPOCH,
            )
        )
        analytics = build_analytics(self.repo, self.quality)
        self.assertEqual(analytics.rollback_count, 1)
        self.assertIn("1.8", analytics.rollbacks[0].reason)

    def test_decisions_are_counted_by_action(self):
        for action in ("hold", "hold", "advance", "rollback"):
            self.quality.record_decision(
                DecisionRecord(
                    flag_key="subject_line",
                    action=action,
                    reason="r",
                    decided_at=EPOCH,
                )
            )
        counts = build_analytics(self.repo, self.quality).decision_counts
        self.assertEqual(counts["hold"], 2)
        self.assertEqual(counts["advance"], 1)

    def test_time_to_full_rollout_is_measured_for_completed_flags(self):
        self.repo.set_status(
            "subject_line", FlagStatus.FULLY_ON, actor="controller", reason="done"
        )
        completed = build_analytics(self.repo, self.quality).completed
        self.assertIn("subject_line", completed)
        self.assertGreaterEqual(completed["subject_line"], 0.0)

    def test_incomplete_flags_have_no_completion_time(self):
        self.assertEqual(build_analytics(self.repo, self.quality).completed, {})


class RenderingTests(DashboardTestCase):
    def test_a_blind_window_is_never_rendered_as_healthy(self):
        """The whole reason unscored samples are tracked separately."""
        self.observe(None, 20, scored=False)
        html = render_overview(build_overviews(self.repo, self.quality))
        self.assertIn("no scored samples", html)

    def test_an_unscored_fraction_is_surfaced(self):
        self.observe(4.0, 15)
        self.observe(None, 5, scored=False)
        html = render_overview(build_overviews(self.repo, self.quality))
        self.assertIn("unscored", html)

    def test_flag_keys_are_escaped(self):
        self.repo.create_flag(
            make_flag("<script>alert(1)</script>"), actor="w", reason="r"
        )
        html = render_overview(build_overviews(self.repo, self.quality))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_decision_reasons_are_escaped(self):
        """A reason is free text, and in the demo derives from model output."""
        self.quality.record_decision(
            DecisionRecord(
                flag_key="subject_line",
                action="rollback",
                reason="<img src=x onerror=alert(1)>",
                decided_at=EPOCH,
            )
        )
        html = render_overview(build_overviews(self.repo, self.quality))
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_the_overview_renders_with_no_flags(self):
        render_overview([])

    def test_the_rollback_button_is_hidden_for_a_rolled_back_flag(self):
        self.repo.set_status(
            "subject_line", FlagStatus.ROLLED_BACK, actor="w", reason="r"
        )
        html = render_overview(build_overviews(self.repo, self.quality))
        self.assertNotIn("Roll back", html)

    def test_the_rollback_button_asks_for_confirmation(self):
        html = render_overview(build_overviews(self.repo, self.quality))
        self.assertIn("confirm(", html)

    def test_the_detail_view_shows_the_schedule_and_marks_the_current_stage(self):
        self.quality.save_rollout_state(
            StoredRolloutState("subject_line", 1, EPOCH)
        )
        html = render_flag_detail(self.overview(), [], [])
        self.assertIn("current", html)
        self.assertIn("upcoming", html)

    def test_the_detail_view_shows_holds_not_only_changes(self):
        decisions = [
            DecisionRecord(
                flag_key="subject_line",
                action="hold",
                reason="only 12 scored samples against a minimum of 30",
                decided_at=EPOCH,
            )
        ]
        html = render_flag_detail(self.overview(), decisions, [])
        self.assertIn("hold", html)
        self.assertIn("12 scored samples", html)

    def test_the_analytics_view_renders_when_empty(self):
        render_analytics(build_analytics(self.repo, self.quality))


class DashboardRouteTests(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.repo, self.quality))

    def test_the_overview_page_is_served(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("subject_line", response.text)

    def test_the_analytics_page_is_served(self):
        self.assertEqual(self.client.get("/dashboard/analytics").status_code, 200)

    def test_a_flag_detail_page_is_served(self):
        response = self.client.get("/dashboard/flags/subject_line")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Rollout schedule", response.text)

    def test_an_unknown_flag_detail_is_404(self):
        self.assertEqual(
            self.client.get("/dashboard/flags/nope").status_code, 404
        )

    def test_the_rollback_button_rolls_the_flag_back(self):
        response = self.client.post(
            "/dashboard/flags/subject_line/rollback",
            data={"reason": "looked wrong in the demo"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        flag = self.repo.get_flag("subject_line")
        self.assertEqual(flag.status, FlagStatus.ROLLED_BACK)
        self.assertEqual(flag.rollout_percentage, 0.0)

    def test_a_dashboard_rollback_is_attributed_and_reasoned(self):
        self.client.post(
            "/dashboard/flags/subject_line/rollback",
            data={"reason": "looked wrong in the demo"},
            follow_redirects=False,
        )
        event = self.repo.audit_events("subject_line")[-1]
        self.assertEqual(event.actor, "dashboard")
        self.assertEqual(event.reason, "looked wrong in the demo")

    def test_the_api_still_works_without_a_quality_store(self):
        """The dashboard is additive; Phase 1's surface must not depend on it."""
        client = TestClient(create_app(self.repo))
        self.assertEqual(client.get("/flags").status_code, 200)
        self.assertEqual(client.get("/dashboard").status_code, 404)


if __name__ == "__main__":
    unittest.main()
