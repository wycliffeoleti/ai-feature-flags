"""The quality evaluator worker.

The behaviours worth pinning are all about not fabricating an absence of
problems:

* a judge failure produces an *unscored* observation, never no observation;
* a store failure leaves the batch unacknowledged for redelivery, rather than
  acknowledging and losing it;
* an outcome that cannot be attributed to a variant is skipped rather than
  guessed at, because a mis-attributed sample corrupts the very comparison it
  feeds.
"""

import unittest
from datetime import UTC, datetime

from aiflags.clock import FakeClock
from aiflags.core.models import (
    Comparison,
    EvaluationReason,
    FlagDefinition,
    QualityGate,
    QualityPolicy,
    QualitySignal,
    Statistic,
    Variant,
    VariantKind,
)
from aiflags.judge.base import JudgeVerdict
from aiflags.judge.fixture import FixtureJudge
from aiflags.queue import InMemoryOutcomeQueue
from aiflags.sdk import Outcome
from aiflags.store.quality import InMemoryQualityStore
from aiflags.workers.evaluator import QualityEvaluator

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

FLAG = FlagDefinition(
    key="subject_line",
    baseline=Variant(key="v1", kind=VariantKind.BASELINE),
    experimental=Variant(key="v2", kind=VariantKind.EXPERIMENTAL),
    quality_policy=QualityPolicy(
        gates=(
            QualityGate(
                signal=QualitySignal.JUDGE_SCORE,
                statistic=Statistic.P10,
                comparison=Comparison.BELOW,
                threshold=3.0,
            ),
        )
    ),
)


class StubJudge:
    def __init__(self, verdict=None, raises=False):
        self._verdict = verdict or JudgeVerdict.scored_at(4.0, "fine")
        self._raises = raises
        self.calls = 0

    def score(self, output, context=None):
        self.calls += 1
        if self._raises:
            raise RuntimeError("judge exploded")
        return self._verdict


class ExplodingStore(InMemoryQualityStore):
    def record_observations(self, observations):
        raise ConnectionError("database unreachable")


def make_outcome(**overrides):
    params = {
        "evaluation_id": "eval-1",
        "flag_key": "subject_line",
        "variant_key": "v2",
        "reason": EvaluationReason.PERCENTAGE_IN,
        "occurred_at": EPOCH,
        "output": "Your invoice for March is ready",
        "latency_ms": 42.0,
    }
    params.update(overrides)
    return Outcome(**params)


class EvaluatorTestCase(unittest.TestCase):
    def setUp(self):
        self.queue = InMemoryOutcomeQueue()
        self.store = InMemoryQualityStore()
        self.judge = StubJudge()
        self.evaluator = self.make_evaluator()

    def make_evaluator(self, judge=None, store=None, lookup=None):
        return QualityEvaluator(
            queue=self.queue,
            store=store if store is not None else self.store,
            judge=judge if judge is not None else self.judge,
            flag_lookup=lookup or (lambda key: FLAG if key == "subject_line" else None),
            clock=FakeClock(EPOCH),
        )

    def samples(self, signal=QualitySignal.JUDGE_SCORE, kind=VariantKind.EXPERIMENTAL):
        return self.store.samples("subject_line", signal, kind, include_shadow=True)


class DrainingTests(EvaluatorTestCase):
    def test_an_empty_queue_does_nothing(self):
        result = self.evaluator.run_once(block_ms=10)
        self.assertEqual(result.consumed, 0)
        self.assertEqual(result.observations, 0)

    def test_outcomes_are_scored_and_recorded(self):
        self.queue.send([make_outcome()])
        result = self.evaluator.run_once(block_ms=10)
        self.assertEqual(result.consumed, 1)
        self.assertEqual([s.value for s in self.samples()], [4.0])

    def test_outcomes_are_acknowledged_after_persisting(self):
        self.queue.send([make_outcome()])
        result = self.evaluator.run_once(block_ms=10)
        self.assertEqual(result.acknowledged, 1)
        self.assertEqual(self.queue.pending(), 0)

    def test_a_batch_is_drained_in_one_pass(self):
        self.queue.send([make_outcome(evaluation_id=f"eval-{i}") for i in range(20)])
        result = self.evaluator.run_once(max_items=20, block_ms=10)
        self.assertEqual(result.consumed, 20)
        self.assertEqual(len(self.samples()), 20)

    def test_max_items_bounds_a_pass(self):
        self.queue.send([make_outcome(evaluation_id=f"eval-{i}") for i in range(20)])
        self.assertEqual(self.evaluator.run_once(max_items=5, block_ms=10).consumed, 5)


class SignalTests(EvaluatorTestCase):
    def test_latency_is_recorded(self):
        self.queue.send([make_outcome(latency_ms=137.0)])
        self.evaluator.run_once(block_ms=10)
        self.assertEqual(
            [s.value for s in self.samples(QualitySignal.LATENCY_MS)], [137.0]
        )

    def test_a_missing_latency_records_no_latency_sample(self):
        self.queue.send([make_outcome(latency_ms=None)])
        self.evaluator.run_once(block_ms=10)
        self.assertEqual(self.samples(QualitySignal.LATENCY_MS), [])

    def test_an_error_records_an_error_rate_of_one(self):
        self.queue.send([make_outcome(error="timeout")])
        self.evaluator.run_once(block_ms=10)
        self.assertEqual(
            [s.value for s in self.samples(QualitySignal.ERROR_RATE)], [1.0]
        )

    def test_a_clean_response_records_an_error_rate_of_zero(self):
        self.queue.send([make_outcome()])
        self.evaluator.run_once(block_ms=10)
        self.assertEqual(
            [s.value for s in self.samples(QualitySignal.ERROR_RATE)], [0.0]
        )

    def test_feedback_is_recorded_when_present(self):
        self.queue.send([make_outcome(feedback=1.0)])
        self.evaluator.run_once(block_ms=10)
        self.assertEqual([s.value for s in self.samples(QualitySignal.FEEDBACK)], [1.0])

    def test_absent_feedback_records_no_feedback_sample(self):
        self.queue.send([make_outcome()])
        self.evaluator.run_once(block_ms=10)
        self.assertEqual(self.samples(QualitySignal.FEEDBACK), [])


class JudgeFailureTests(EvaluatorTestCase):
    def test_an_unscored_verdict_is_recorded_as_unscored(self):
        evaluator = self.make_evaluator(
            judge=StubJudge(JudgeVerdict.unscored("judge timed out"))
        )
        self.queue.send([make_outcome()])
        evaluator.run_once(block_ms=10)
        samples = self.samples()
        self.assertEqual(len(samples), 1)
        self.assertFalse(samples[0].scored)

    def test_a_judge_that_raises_still_produces_an_observation(self):
        """A broken judge must be visible, not indistinguishable from silence."""
        evaluator = self.make_evaluator(judge=StubJudge(raises=True))
        self.queue.send([make_outcome()])
        evaluator.run_once(block_ms=10)
        samples = self.samples()
        self.assertEqual(len(samples), 1)
        self.assertFalse(samples[0].scored)

    def test_a_judge_failure_does_not_block_the_batch(self):
        evaluator = self.make_evaluator(judge=StubJudge(raises=True))
        self.queue.send([make_outcome(evaluation_id=f"eval-{i}") for i in range(5)])
        result = evaluator.run_once(block_ms=10)
        self.assertEqual(result.acknowledged, 5)

    def test_a_judge_failure_still_records_the_other_signals(self):
        evaluator = self.make_evaluator(judge=StubJudge(raises=True))
        self.queue.send([make_outcome(latency_ms=99.0)])
        evaluator.run_once(block_ms=10)
        self.assertEqual(
            [s.value for s in self.samples(QualitySignal.LATENCY_MS)], [99.0]
        )


class StoreFailureTests(EvaluatorTestCase):
    def test_a_store_failure_leaves_the_batch_unacknowledged(self):
        """A lost batch reads downstream as 'no problems observed'."""
        evaluator = self.make_evaluator(store=ExplodingStore())
        self.queue.send([make_outcome()])
        result = evaluator.run_once(block_ms=10)
        self.assertEqual(result.acknowledged, 0)
        self.assertEqual(self.queue.pending(), 1)

    def test_the_batch_is_redelivered_after_a_store_failure(self):
        evaluator = self.make_evaluator(store=ExplodingStore())
        self.queue.send([make_outcome()])
        evaluator.run_once(block_ms=10)
        self.queue.reclaim()
        self.assertEqual(self.evaluator.run_once(block_ms=10).observations, 3)

    def test_a_store_failure_does_not_raise(self):
        evaluator = self.make_evaluator(store=ExplodingStore())
        self.queue.send([make_outcome()])
        evaluator.run_once(block_ms=10)  # must not raise


class AttributionTests(EvaluatorTestCase):
    def test_the_experimental_variant_is_attributed_correctly(self):
        self.queue.send([make_outcome(variant_key="v2")])
        self.evaluator.run_once(block_ms=10)
        self.assertEqual(len(self.samples(kind=VariantKind.EXPERIMENTAL)), 1)
        self.assertEqual(len(self.samples(kind=VariantKind.BASELINE)), 0)

    def test_the_baseline_variant_is_attributed_correctly(self):
        self.queue.send([make_outcome(variant_key="v1")])
        self.evaluator.run_once(block_ms=10)
        self.assertEqual(len(self.samples(kind=VariantKind.BASELINE)), 1)

    def test_an_unknown_flag_is_skipped_and_acknowledged(self):
        self.queue.send([make_outcome(flag_key="deleted_flag")])
        result = self.evaluator.run_once(block_ms=10)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.acknowledged, 1)

    def test_an_unrecognised_variant_is_skipped_rather_than_guessed(self):
        """Mis-attributing a sample corrupts the comparison it feeds."""
        self.queue.send([make_outcome(variant_key="v_deleted")])
        result = self.evaluator.run_once(block_ms=10)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(len(self.samples()), 0)
        self.assertEqual(len(self.samples(kind=VariantKind.BASELINE)), 0)

    def test_a_degraded_evaluation_counts_as_baseline(self):
        """The SDK really did serve baseline output; it is a baseline sample."""
        self.queue.send(
            [
                make_outcome(
                    variant_key="__unknown_baseline__",
                    reason=EvaluationReason.SNAPSHOT_STALE,
                )
            ]
        )
        self.evaluator.run_once(block_ms=10)
        self.assertEqual(len(self.samples(kind=VariantKind.BASELINE)), 1)

    def test_shadow_outcomes_are_marked_as_shadow(self):
        self.queue.send([make_outcome(is_shadow=True)])
        self.evaluator.run_once(block_ms=10)
        visible = self.store.samples(
            "subject_line", QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL
        )
        self.assertEqual(visible, [])
        self.assertEqual(len(self.samples()), 1)


class EndToEndTests(EvaluatorTestCase):
    def test_a_bad_variant_produces_low_scores_through_the_fixture_judge(self):
        """The demo's failure mode, end to end: an unrendered template leaks."""
        evaluator = self.make_evaluator(judge=FixtureJudge())
        self.queue.send(
            [
                make_outcome(
                    evaluation_id=f"eval-{i}",
                    output="Your invoice for {month} is ready",
                )
                for i in range(10)
            ]
        )
        evaluator.run_once(max_items=10, block_ms=10)
        scores = [s.value for s in self.samples()]
        self.assertEqual(len(scores), 10)
        self.assertTrue(all(score < 3.0 for score in scores), scores)

    def test_a_good_variant_produces_high_scores(self):
        evaluator = self.make_evaluator(judge=FixtureJudge())
        self.queue.send(
            [
                make_outcome(
                    evaluation_id=f"eval-{i}", output="Your March invoice is ready"
                )
                for i in range(10)
            ]
        )
        evaluator.run_once(max_items=10, block_ms=10)
        self.assertTrue(all(s.value >= 4.0 for s in self.samples()))


if __name__ == "__main__":
    unittest.main()
