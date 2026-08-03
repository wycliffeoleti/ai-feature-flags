"""The pure evaluation path.

Two properties here are the ones that keep a rollout's quality data honest:

* **Stickiness** — a subject's variant does not change when an unrelated flag is
  edited and a new snapshot is published.
* **Monotonic ramp** — raising the percentage only adds subjects to the
  experimental variant. If a ramp-up could move someone *back* to baseline, the
  quality windows would fill with users switching variants mid-session and the
  canary comparison would be measuring churn rather than quality.

Everything else here is fail-safe behaviour: every uncertain path serves
baseline.
"""

import unittest
from datetime import UTC, datetime, timedelta

from aiflags.core.bucketing import bucket
from aiflags.core.evaluation import UNKNOWN_BASELINE, evaluate
from aiflags.core.models import (
    EvaluationContext,
    EvaluationReason,
    FlagDefinition,
    FlagSnapshot,
    FlagStatus,
    QualityGate,
    QualityPolicy,
    Comparison,
    QualitySignal,
    Statistic,
    TargetingKind,
    TargetingRule,
    Variant,
    VariantKind,
)

PUBLISHED_AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

BASELINE = Variant(key="v1", kind=VariantKind.BASELINE, config={"prompt": "v1"})
EXPERIMENTAL = Variant(
    key="v2", kind=VariantKind.EXPERIMENTAL, config={"prompt": "v2"}
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


def make_flag(**overrides) -> FlagDefinition:
    params = {
        "key": "subject_line",
        "baseline": BASELINE,
        "experimental": EXPERIMENTAL,
        "quality_policy": POLICY,
        "salt": "salt-a",
        "status": FlagStatus.ROLLING_OUT,
        "rollout_percentage": 50.0,
    }
    params.update(overrides)
    return FlagDefinition(**params)


def make_snapshot(flag: FlagDefinition | None = None, version: int = 1) -> FlagSnapshot:
    flag = flag if flag is not None else make_flag()
    return FlagSnapshot(
        version=version, published_at=PUBLISHED_AT, flags={flag.key: flag}
    )


def evaluate_subject(snapshot, subject_key, attributes=None, **kwargs):
    return evaluate(
        snapshot=snapshot,
        flag_key="subject_line",
        context=EvaluationContext(
            subject_key=subject_key, attributes=attributes or {}
        ),
        evaluation_id=f"eval-{subject_key}",
        **kwargs,
    )


class StickinessTests(unittest.TestCase):
    def test_same_subject_gets_same_variant_across_snapshot_versions(self):
        subjects = [f"user-{i}" for i in range(500)]
        first = {s: evaluate_subject(make_snapshot(version=1), s).variant.key
                 for s in subjects}
        later = {s: evaluate_subject(make_snapshot(version=99), s).variant.key
                 for s in subjects}
        self.assertEqual(first, later)

    def test_assignment_is_independent_of_evaluation_id(self):
        snapshot = make_snapshot()
        context = EvaluationContext(subject_key="user-7")
        a = evaluate(snapshot, "subject_line", context, evaluation_id="eval-a")
        b = evaluate(snapshot, "subject_line", context, evaluation_id="eval-b")
        self.assertEqual(a.variant, b.variant)
        self.assertNotEqual(a.evaluation_id, b.evaluation_id)


class MonotonicRampTests(unittest.TestCase):
    def test_raising_percentage_never_removes_a_subject(self):
        subjects = [f"user-{i}" for i in range(400)]
        previous: set[str] = set()
        for percentage in (0.0, 1.0, 5.0, 25.0, 50.0, 75.0, 100.0):
            snapshot = make_snapshot(make_flag(rollout_percentage=percentage))
            current = {
                s for s in subjects
                if evaluate_subject(snapshot, s).is_experimental
            }
            self.assertTrue(
                previous <= current,
                f"raising to {percentage}% dropped {previous - current}",
            )
            previous = current

    def test_zero_percent_serves_nobody_the_experiment(self):
        snapshot = make_snapshot(make_flag(rollout_percentage=0.0))
        for i in range(300):
            result = evaluate_subject(snapshot, f"user-{i}")
            self.assertFalse(result.is_experimental)
            self.assertEqual(result.reason, EvaluationReason.PERCENTAGE_OUT)

    def test_hundred_percent_serves_everybody_the_experiment(self):
        snapshot = make_snapshot(make_flag(rollout_percentage=100.0))
        for i in range(300):
            self.assertTrue(evaluate_subject(snapshot, f"user-{i}").is_experimental)

    def test_percentage_boundary_is_half_open(self):
        """``bucket < percentage`` — a subject exactly on the boundary stays out."""
        subject = "user-42"
        value = bucket(subject, "subject_line", "salt-a")
        snapshot = make_snapshot(make_flag(rollout_percentage=value * 100.0))
        self.assertFalse(evaluate_subject(snapshot, subject).is_experimental)


class FailSafeTests(unittest.TestCase):
    def test_missing_snapshot_serves_sentinel_baseline(self):
        result = evaluate_subject(None, "user-1")
        self.assertEqual(result.reason, EvaluationReason.NO_SNAPSHOT)
        self.assertEqual(result.variant, UNKNOWN_BASELINE)
        self.assertTrue(result.is_degraded)

    def test_unknown_flag_serves_sentinel_baseline(self):
        snapshot = FlagSnapshot(version=1, published_at=PUBLISHED_AT, flags={})
        result = evaluate_subject(snapshot, "user-1")
        self.assertEqual(result.reason, EvaluationReason.FLAG_UNKNOWN)
        self.assertEqual(result.variant, UNKNOWN_BASELINE)
        self.assertTrue(result.is_degraded)

    def test_stale_snapshot_serves_the_flags_own_baseline(self):
        snapshot = make_snapshot(make_flag(rollout_percentage=100.0))
        result = evaluate_subject(
            snapshot,
            "user-1",
            now=PUBLISHED_AT + timedelta(seconds=61),
            max_staleness_seconds=60.0,
        )
        self.assertEqual(result.reason, EvaluationReason.SNAPSHOT_STALE)
        self.assertEqual(result.variant, BASELINE)
        self.assertTrue(result.is_degraded)

    def test_fresh_snapshot_is_not_stale(self):
        snapshot = make_snapshot(make_flag(rollout_percentage=100.0))
        result = evaluate_subject(
            snapshot,
            "user-1",
            now=PUBLISHED_AT + timedelta(seconds=59),
            max_staleness_seconds=60.0,
        )
        self.assertTrue(result.is_experimental)

    def test_staleness_is_not_checked_when_no_limit_is_configured(self):
        snapshot = make_snapshot(make_flag(rollout_percentage=100.0))
        result = evaluate_subject(
            snapshot, "user-1", now=PUBLISHED_AT + timedelta(days=365)
        )
        self.assertTrue(result.is_experimental)

    def test_rolled_back_flag_serves_baseline_even_at_full_percentage(self):
        """Rollback must win over a stale percentage left behind on the record."""
        snapshot = make_snapshot(
            make_flag(status=FlagStatus.ROLLED_BACK, rollout_percentage=100.0)
        )
        result = evaluate_subject(snapshot, "user-1")
        self.assertEqual(result.reason, EvaluationReason.ROLLED_BACK)
        self.assertEqual(result.variant, BASELINE)

    def test_off_flag_serves_baseline(self):
        snapshot = make_snapshot(
            make_flag(status=FlagStatus.OFF, rollout_percentage=100.0)
        )
        result = evaluate_subject(snapshot, "user-1")
        self.assertEqual(result.reason, EvaluationReason.FLAG_OFF)


class StatusTests(unittest.TestCase):
    def test_fully_on_serves_experimental(self):
        snapshot = make_snapshot(make_flag(status=FlagStatus.FULLY_ON))
        result = evaluate_subject(snapshot, "user-1")
        self.assertEqual(result.reason, EvaluationReason.FULLY_ON)
        self.assertTrue(result.is_experimental)

    def test_paused_flag_keeps_serving_its_current_percentage(self):
        rolling = make_snapshot(make_flag(status=FlagStatus.ROLLING_OUT))
        paused = make_snapshot(make_flag(status=FlagStatus.PAUSED))
        for i in range(200):
            subject = f"user-{i}"
            self.assertEqual(
                evaluate_subject(rolling, subject).variant,
                evaluate_subject(paused, subject).variant,
            )

    def test_shadow_mode_serves_baseline_and_reports_the_shadow_variant(self):
        snapshot = make_snapshot(
            make_flag(status=FlagStatus.SHADOW, rollout_percentage=0.0)
        )
        result = evaluate_subject(snapshot, "user-1")
        self.assertEqual(result.reason, EvaluationReason.SHADOW)
        self.assertEqual(result.variant, BASELINE)
        self.assertEqual(result.shadow_variant, EXPERIMENTAL)

    def test_shadow_mode_applies_to_every_subject(self):
        snapshot = make_snapshot(make_flag(status=FlagStatus.SHADOW))
        for i in range(200):
            result = evaluate_subject(snapshot, f"user-{i}")
            self.assertFalse(result.is_experimental)
            self.assertIsNotNone(result.shadow_variant)

    def test_no_shadow_variant_outside_shadow_mode(self):
        snapshot = make_snapshot(make_flag())
        self.assertIsNone(evaluate_subject(snapshot, "user-1").shadow_variant)


class TargetingIntegrationTests(unittest.TestCase):
    def test_blocklist_overrides_a_fully_on_flag(self):
        """The escape hatch has to work after a flag reaches 100%."""
        flag = make_flag(
            status=FlagStatus.FULLY_ON,
            targeting=(
                TargetingRule(
                    kind=TargetingKind.BLOCKLIST,
                    values=frozenset({"user-blocked"}),
                    variant_kind=VariantKind.BASELINE,
                ),
            ),
        )
        snapshot = make_snapshot(flag)
        blocked = evaluate_subject(snapshot, "user-blocked")
        self.assertEqual(blocked.reason, EvaluationReason.BLOCKLIST)
        self.assertEqual(blocked.variant, BASELINE)
        self.assertTrue(evaluate_subject(snapshot, "user-other").is_experimental)

    def test_allowlist_overrides_a_zero_percent_rollout(self):
        flag = make_flag(
            rollout_percentage=0.0,
            targeting=(
                TargetingRule(
                    kind=TargetingKind.ALLOWLIST,
                    values=frozenset({"user-internal"}),
                    variant_kind=VariantKind.EXPERIMENTAL,
                ),
            ),
        )
        result = evaluate_subject(make_snapshot(flag), "user-internal")
        self.assertEqual(result.reason, EvaluationReason.ALLOWLIST)
        self.assertTrue(result.is_experimental)

    def test_targeting_does_not_override_a_rolled_back_flag(self):
        """An allowlist must not resurrect a variant the system rolled back."""
        flag = make_flag(
            status=FlagStatus.ROLLED_BACK,
            targeting=(
                TargetingRule(
                    kind=TargetingKind.ALLOWLIST,
                    values=frozenset({"user-internal"}),
                    variant_kind=VariantKind.EXPERIMENTAL,
                ),
            ),
        )
        result = evaluate_subject(make_snapshot(flag), "user-internal")
        self.assertEqual(result.reason, EvaluationReason.ROLLED_BACK)
        self.assertFalse(result.is_experimental)

    def test_segment_targeting_uses_context_attributes(self):
        flag = make_flag(
            rollout_percentage=0.0,
            targeting=(
                TargetingRule(
                    kind=TargetingKind.SEGMENT,
                    values=frozenset({"internal"}),
                    variant_kind=VariantKind.EXPERIMENTAL,
                    attribute="segment",
                ),
            ),
        )
        snapshot = make_snapshot(flag)
        matched = evaluate_subject(snapshot, "user-1", {"segment": "internal"})
        self.assertEqual(matched.reason, EvaluationReason.SEGMENT)
        missed = evaluate_subject(snapshot, "user-1", {"segment": "external"})
        self.assertEqual(missed.reason, EvaluationReason.PERCENTAGE_OUT)


class ResultMetadataTests(unittest.TestCase):
    def test_result_carries_snapshot_version_and_evaluation_id(self):
        result = evaluate_subject(make_snapshot(version=7), "user-1")
        self.assertEqual(result.snapshot_version, 7)
        self.assertEqual(result.evaluation_id, "eval-user-1")
        self.assertEqual(result.flag_key, "subject_line")

    def test_missing_snapshot_reports_version_zero(self):
        self.assertEqual(evaluate_subject(None, "user-1").snapshot_version, 0)


if __name__ == "__main__":
    unittest.main()
