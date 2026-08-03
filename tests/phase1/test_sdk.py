"""The SDK is the part an application embeds, so its contract is: never raise,
never block, never ramp on uncertainty.

Every failure mode of the flag service has to surface as *baseline traffic*, not
as an exception in someone else's request handler. These tests drive each failure
through a fake transport.
"""

import unittest
from datetime import UTC, datetime, timedelta

from aiflags.clock import FakeClock
from aiflags.core.models import (
    Comparison,
    EvaluationContext,
    EvaluationReason,
    FlagDefinition,
    FlagSnapshot,
    FlagStatus,
    QualityGate,
    QualityPolicy,
    QualitySignal,
    Statistic,
    Variant,
    VariantKind,
)
from aiflags.sdk import FlagClient, Outcome

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
BASELINE = Variant(key="v1", kind=VariantKind.BASELINE)
EXPERIMENTAL = Variant(key="v2", kind=VariantKind.EXPERIMENTAL)
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


def make_snapshot(version=1, percentage=100.0, published_at=EPOCH, status=None):
    flag = FlagDefinition(
        key="subject_line",
        baseline=BASELINE,
        experimental=EXPERIMENTAL,
        quality_policy=POLICY,
        status=status or FlagStatus.ROLLING_OUT,
        rollout_percentage=percentage,
    )
    return FlagSnapshot(version=version, published_at=published_at, flags={flag.key: flag})


class FakeSource:
    """A snapshot transport that can be told to fail."""

    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.failing = False
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        if self.failing:
            raise ConnectionError("flag service unreachable")
        return self.snapshot


class FakeSink:
    """An outcome transport that records what it received, or fails."""

    def __init__(self):
        self.batches: list[list[Outcome]] = []
        self.failing = False

    def send(self, batch):
        if self.failing:
            raise ConnectionError("queue unreachable")
        self.batches.append(list(batch))

    @property
    def outcomes(self):
        return [outcome for batch in self.batches for outcome in batch]


def make_client(source=None, sink=None, clock=None, **kwargs):
    return FlagClient(
        source=source if source is not None else FakeSource(make_snapshot()),
        sink=sink if sink is not None else FakeSink(),
        clock=clock if clock is not None else FakeClock(EPOCH),
        **kwargs,
    )


CONTEXT = EvaluationContext(subject_key="user-1")


class RefreshTests(unittest.TestCase):
    def test_refresh_loads_a_snapshot(self):
        client = make_client()
        self.assertTrue(client.refresh())
        self.assertEqual(client.snapshot_version, 1)

    def test_evaluate_before_any_refresh_serves_baseline(self):
        source = FakeSource(make_snapshot())
        client = make_client(source=source)
        result = client.evaluate("subject_line", CONTEXT)
        self.assertEqual(result.reason, EvaluationReason.NO_SNAPSHOT)
        self.assertEqual(source.fetches, 0, "evaluate must not fetch on the hot path")

    def test_refresh_failure_returns_false_and_does_not_raise(self):
        source = FakeSource(make_snapshot())
        client = make_client(source=source)
        client.refresh()
        source.failing = True
        self.assertFalse(client.refresh())

    def test_last_good_snapshot_survives_a_service_outage(self):
        source = FakeSource(make_snapshot(percentage=100.0))
        client = make_client(source=source)
        client.refresh()
        source.failing = True
        for _ in range(5):
            client.refresh()
        self.assertTrue(client.evaluate("subject_line", CONTEXT).is_experimental)

    def test_older_snapshot_versions_are_ignored(self):
        """Out-of-order delivery must not roll the data plane backwards."""
        source = FakeSource(make_snapshot(version=5, percentage=100.0))
        client = make_client(source=source)
        client.refresh()
        source.snapshot = make_snapshot(version=4, percentage=0.0)
        self.assertFalse(client.refresh())
        self.assertEqual(client.snapshot_version, 5)
        self.assertTrue(client.evaluate("subject_line", CONTEXT).is_experimental)

    def test_same_version_is_not_reapplied(self):
        source = FakeSource(make_snapshot(version=5))
        client = make_client(source=source)
        client.refresh()
        self.assertFalse(client.refresh())

    def test_empty_fetch_is_not_treated_as_an_update(self):
        source = FakeSource(None)
        client = make_client(source=source)
        self.assertFalse(client.refresh())
        self.assertEqual(client.snapshot_version, 0)


class StalenessTests(unittest.TestCase):
    def test_stale_snapshot_degrades_to_baseline(self):
        clock = FakeClock(EPOCH)
        client = make_client(clock=clock, max_staleness_seconds=60.0)
        client.refresh()
        self.assertTrue(client.evaluate("subject_line", CONTEXT).is_experimental)
        clock.advance(61.0)
        result = client.evaluate("subject_line", CONTEXT)
        self.assertEqual(result.reason, EvaluationReason.SNAPSHOT_STALE)
        self.assertFalse(result.is_experimental)

    def test_a_successful_refresh_clears_staleness(self):
        clock = FakeClock(EPOCH)
        source = FakeSource(make_snapshot())
        client = make_client(source=source, clock=clock, max_staleness_seconds=60.0)
        client.refresh()
        clock.advance(61.0)
        source.snapshot = make_snapshot(version=2, published_at=clock.now())
        client.refresh()
        self.assertTrue(client.evaluate("subject_line", CONTEXT).is_experimental)


class DefaultVariantTests(unittest.TestCase):
    def test_caller_default_replaces_the_sentinel_for_unknown_flags(self):
        fallback = Variant(key="non_ai_path", kind=VariantKind.BASELINE)
        client = make_client(default_variant=fallback)
        client.refresh()
        result = client.evaluate("no_such_flag", CONTEXT)
        self.assertEqual(result.variant, fallback)
        self.assertEqual(result.reason, EvaluationReason.FLAG_UNKNOWN)

    def test_per_call_default_overrides_the_client_default(self):
        client_default = Variant(key="client", kind=VariantKind.BASELINE)
        call_default = Variant(key="call", kind=VariantKind.BASELINE)
        client = make_client(default_variant=client_default)
        client.refresh()
        result = client.evaluate("no_such_flag", CONTEXT, default_variant=call_default)
        self.assertEqual(result.variant, call_default)

    def test_default_is_not_used_when_the_flag_is_known(self):
        fallback = Variant(key="non_ai_path", kind=VariantKind.BASELINE)
        client = make_client(default_variant=fallback)
        client.refresh()
        self.assertTrue(client.evaluate("subject_line", CONTEXT).is_experimental)


class EvaluationIdTests(unittest.TestCase):
    def test_every_evaluation_gets_a_unique_id(self):
        client = make_client()
        client.refresh()
        ids = {client.evaluate("subject_line", CONTEXT).evaluation_id for _ in range(200)}
        self.assertEqual(len(ids), 200)


class OutcomeBufferTests(unittest.TestCase):
    def test_record_outcome_does_not_reach_the_sink(self):
        """The hot path only appends to a buffer; I/O happens in flush()."""
        sink = FakeSink()
        client = make_client(sink=sink)
        client.refresh()
        result = client.evaluate("subject_line", CONTEXT)
        client.record_outcome(result, output="hello", latency_ms=12.0)
        self.assertEqual(sink.batches, [])
        self.assertEqual(client.pending_outcomes, 1)

    def test_flush_sends_buffered_outcomes(self):
        sink = FakeSink()
        client = make_client(sink=sink)
        client.refresh()
        result = client.evaluate("subject_line", CONTEXT)
        client.record_outcome(result, output="hello", latency_ms=12.0)
        self.assertEqual(client.flush(), 1)
        self.assertEqual(client.pending_outcomes, 0)
        self.assertEqual(sink.outcomes[0].evaluation_id, result.evaluation_id)

    def test_outcome_carries_the_served_variant_and_reason(self):
        sink = FakeSink()
        client = make_client(sink=sink)
        client.refresh()
        result = client.evaluate("subject_line", CONTEXT)
        client.record_outcome(result, output="hello", latency_ms=12.0)
        client.flush()
        outcome = sink.outcomes[0]
        self.assertEqual(outcome.variant_key, "v2")
        self.assertEqual(outcome.flag_key, "subject_line")
        self.assertEqual(outcome.reason, EvaluationReason.PERCENTAGE_IN)
        self.assertFalse(outcome.is_shadow)

    def test_buffer_is_bounded_and_drops_are_counted(self):
        client = make_client(buffer_capacity=3)
        client.refresh()
        for _ in range(10):
            result = client.evaluate("subject_line", CONTEXT)
            client.record_outcome(result, output="x", latency_ms=1.0)
        self.assertEqual(client.pending_outcomes, 3)
        self.assertEqual(client.dropped_outcomes, 7)

    def test_buffer_drops_oldest_first(self):
        sink = FakeSink()
        client = make_client(sink=sink, buffer_capacity=2)
        client.refresh()
        recorded = []
        for _ in range(4):
            result = client.evaluate("subject_line", CONTEXT)
            client.record_outcome(result, output="x", latency_ms=1.0)
            recorded.append(result.evaluation_id)
        client.flush()
        self.assertEqual(
            [o.evaluation_id for o in sink.outcomes], recorded[-2:]
        )

    def test_flush_failure_retains_outcomes_and_does_not_raise(self):
        sink = FakeSink()
        client = make_client(sink=sink)
        client.refresh()
        result = client.evaluate("subject_line", CONTEXT)
        client.record_outcome(result, output="hello", latency_ms=12.0)
        sink.failing = True
        self.assertEqual(client.flush(), 0)
        self.assertEqual(client.pending_outcomes, 1)
        sink.failing = False
        self.assertEqual(client.flush(), 1)

    def test_record_outcome_never_raises_when_everything_is_broken(self):
        source = FakeSource(make_snapshot())
        sink = FakeSink()
        client = make_client(source=source, sink=sink)
        source.failing = True
        sink.failing = True
        result = client.evaluate("subject_line", CONTEXT)
        client.record_outcome(result, output="hello", latency_ms=1.0, error="boom")
        client.flush()  # must not raise


class ShadowOutcomeTests(unittest.TestCase):
    def test_shadow_outcome_is_flagged_and_carries_the_shadow_variant(self):
        sink = FakeSink()
        source = FakeSource(make_snapshot(status=FlagStatus.SHADOW))
        client = make_client(source=source, sink=sink)
        client.refresh()
        result = client.evaluate("subject_line", CONTEXT)
        client.record_shadow_outcome(result, output="shadow text", latency_ms=30.0)
        client.flush()
        outcome = sink.outcomes[0]
        self.assertTrue(outcome.is_shadow)
        self.assertEqual(outcome.variant_key, "v2")

    def test_recording_a_shadow_outcome_without_a_shadow_variant_is_rejected(self):
        client = make_client()
        client.refresh()
        result = client.evaluate("subject_line", CONTEXT)
        with self.assertRaises(ValueError):
            client.record_shadow_outcome(result, output="x", latency_ms=1.0)


class ContextManagerTests(unittest.TestCase):
    def test_exiting_the_context_flushes_pending_outcomes(self):
        sink = FakeSink()
        source = FakeSource(make_snapshot())
        with FlagClient(source=source, sink=sink, clock=FakeClock(EPOCH)) as client:
            client.refresh()
            result = client.evaluate("subject_line", CONTEXT)
            client.record_outcome(result, output="hello", latency_ms=1.0)
        self.assertEqual(len(sink.outcomes), 1)

    def test_exit_does_not_raise_when_the_sink_is_down(self):
        sink = FakeSink()
        sink.failing = True
        source = FakeSource(make_snapshot())
        with FlagClient(source=source, sink=sink, clock=FakeClock(EPOCH)) as client:
            client.refresh()
            result = client.evaluate("subject_line", CONTEXT)
            client.record_outcome(result, output="hello", latency_ms=1.0)


if __name__ == "__main__":
    unittest.main()
