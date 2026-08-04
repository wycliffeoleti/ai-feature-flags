"""Redis snapshot publishing and the outcome queue.

Both run against real Redis when it is reachable and skip otherwise; the queue
additionally has an in-memory implementation exercised by the same contract.

The two properties worth asserting:

* **A snapshot never moves backwards.** Two publishers racing must not be able
  to reinstate a percentage an operator already changed.
* **A consumed outcome stays pending until acknowledged.** If the evaluator dies
  mid-batch those outcomes must be reclaimable, because a silently lost batch
  reads downstream as a gap in the quality window — the one signal that must
  never be fabricated.
"""

import os
import unittest
from datetime import UTC, datetime

from aiflags.core.models import (
    Comparison,
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
from aiflags.queue import InMemoryOutcomeQueue, decode_outcome, encode_outcome
from aiflags.sdk import Outcome

REDIS_URL = os.environ.get("AIFLAGS_TEST_REDIS_URL")
EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

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


def make_snapshot(version=1, percentage=25.0):
    flag = FlagDefinition(
        key="subject_line",
        baseline=Variant(key="v1", kind=VariantKind.BASELINE, config={"prompt": "a"}),
        experimental=Variant(
            key="v2", kind=VariantKind.EXPERIMENTAL, config={"prompt": "b"}
        ),
        quality_policy=POLICY,
        status=FlagStatus.ROLLING_OUT,
        rollout_percentage=percentage,
    )
    return FlagSnapshot(version=version, published_at=EPOCH, flags={flag.key: flag})


def make_outcome(index=0, **overrides):
    params = {
        "evaluation_id": f"eval-{index}",
        "flag_key": "subject_line",
        "variant_key": "v2",
        "reason": EvaluationReason.PERCENTAGE_IN,
        "occurred_at": EPOCH,
        "output": "Your invoice is ready",
        "latency_ms": 42.0,
    }
    params.update(overrides)
    return Outcome(**params)


class OutcomeCodecTests(unittest.TestCase):
    def test_an_outcome_round_trips(self):
        original = make_outcome(feedback=1.0, error=None, metadata={"tenant": "acme"})
        restored = decode_outcome(encode_outcome(original))
        self.assertEqual(restored, original)

    def test_a_shadow_outcome_round_trips(self):
        original = make_outcome(is_shadow=True)
        self.assertTrue(decode_outcome(encode_outcome(original)).is_shadow)

    def test_an_errored_outcome_round_trips(self):
        original = make_outcome(output=None, error="timeout")
        restored = decode_outcome(encode_outcome(original))
        self.assertIsNone(restored.output)
        self.assertEqual(restored.error, "timeout")


class OutcomeQueueContract:
    def make_queue(self):
        raise NotImplementedError

    def setUp(self):
        self.queue = self.make_queue()

    def test_sent_outcomes_are_consumable(self):
        self.queue.send([make_outcome(0), make_outcome(1)])
        consumed = self.queue.consume(block_ms=50)
        self.assertEqual(len(consumed), 2)
        self.assertEqual(
            {entry.outcome.evaluation_id for entry in consumed}, {"eval-0", "eval-1"}
        )

    def test_sending_an_empty_batch_is_a_no_op(self):
        self.queue.send([])
        self.assertEqual(self.queue.consume(block_ms=50), [])

    def test_consuming_an_empty_queue_returns_nothing(self):
        self.assertEqual(self.queue.consume(block_ms=50), [])

    def test_outcomes_are_not_redelivered_to_the_same_consumer(self):
        self.queue.send([make_outcome(0)])
        self.assertEqual(len(self.queue.consume(block_ms=50)), 1)
        self.assertEqual(self.queue.consume(block_ms=50), [])

    def test_max_items_limits_a_batch(self):
        self.queue.send([make_outcome(i) for i in range(10)])
        self.assertEqual(len(self.queue.consume(max_items=3, block_ms=50)), 3)

    def test_consumed_outcomes_stay_pending_until_acknowledged(self):
        """An evaluator that dies mid-batch must not silently lose the work."""
        self.queue.send([make_outcome(0)])
        consumed = self.queue.consume(block_ms=50)
        self.assertEqual(self.queue.pending(), 1)
        self.assertEqual(self.queue.acknowledge([entry.id for entry in consumed]), 1)
        self.assertEqual(self.queue.pending(), 0)

    def test_acknowledging_nothing_is_a_no_op(self):
        self.assertEqual(self.queue.acknowledge([]), 0)

    def test_payload_survives_the_transport(self):
        self.queue.send([make_outcome(0, output="Your March invoice", latency_ms=17.5)])
        outcome = self.queue.consume(block_ms=50)[0].outcome
        self.assertEqual(outcome.output, "Your March invoice")
        self.assertEqual(outcome.latency_ms, 17.5)
        self.assertEqual(outcome.reason, EvaluationReason.PERCENTAGE_IN)


class InMemoryOutcomeQueueTests(OutcomeQueueContract, unittest.TestCase):
    def make_queue(self):
        return InMemoryOutcomeQueue()

    def test_unacknowledged_outcomes_can_be_reclaimed(self):
        self.queue.send([make_outcome(0)])
        self.queue.consume(block_ms=50)
        self.assertEqual(len(self.queue.reclaim()), 1)
        self.assertEqual(len(self.queue.consume(block_ms=50)), 1)


@unittest.skipUnless(
    REDIS_URL, "set AIFLAGS_TEST_REDIS_URL to run the Redis transport tests"
)
class RedisOutcomeQueueTests(OutcomeQueueContract, unittest.TestCase):
    def make_queue(self):
        from aiflags.queue import RedisOutcomeQueue

        queue = RedisOutcomeQueue.from_url(REDIS_URL, stream="aiflags:test:outcomes")
        queue.clear()
        return queue


@unittest.skipUnless(
    REDIS_URL, "set AIFLAGS_TEST_REDIS_URL to run the Redis transport tests"
)
class RedisSnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        from aiflags.store.redis_snapshot import RedisSnapshotStore

        self.store = RedisSnapshotStore.from_url(
            REDIS_URL, key="aiflags:test:snapshot"
        )
        self.store.clear()

    def test_nothing_published_reads_as_none(self):
        self.assertIsNone(self.store.fetch())

    def test_a_published_snapshot_round_trips(self):
        self.assertTrue(self.store.publish(make_snapshot(version=3, percentage=25.0)))
        fetched = self.store.fetch()
        self.assertEqual(fetched.version, 3)
        self.assertEqual(fetched.flags["subject_line"].rollout_percentage, 25.0)
        self.assertEqual(
            fetched.flags["subject_line"].experimental.config, {"prompt": "b"}
        )

    def test_the_full_policy_survives_publication(self):
        self.store.publish(make_snapshot())
        gate = self.store.fetch().flags["subject_line"].quality_policy.gates[0]
        self.assertEqual(gate.signal, QualitySignal.JUDGE_SCORE)
        self.assertEqual(gate.threshold, 3.0)

    def test_a_newer_snapshot_replaces_an_older_one(self):
        self.store.publish(make_snapshot(version=1, percentage=1.0))
        self.assertTrue(self.store.publish(make_snapshot(version=2, percentage=5.0)))
        self.assertEqual(self.store.fetch().version, 2)

    def test_an_older_snapshot_is_refused(self):
        """Two publishers racing must not move the data plane backwards."""
        self.store.publish(make_snapshot(version=5, percentage=50.0))
        self.assertFalse(self.store.publish(make_snapshot(version=4, percentage=0.0)))
        fetched = self.store.fetch()
        self.assertEqual(fetched.version, 5)
        self.assertEqual(fetched.flags["subject_line"].rollout_percentage, 50.0)

    def test_republishing_the_same_version_is_refused(self):
        self.store.publish(make_snapshot(version=5))
        self.assertFalse(self.store.publish(make_snapshot(version=5)))

    def test_the_store_satisfies_the_sdk_snapshot_source_protocol(self):
        from aiflags.clock import FakeClock
        from aiflags.core.models import EvaluationContext
        from aiflags.sdk import FlagClient

        self.store.publish(make_snapshot(version=1, percentage=100.0))
        client = FlagClient(
            source=self.store,
            sink=InMemoryOutcomeQueue(),
            clock=FakeClock(EPOCH),
            max_staleness_seconds=None,
        )
        self.assertTrue(client.refresh())
        result = client.evaluate(
            "subject_line", EvaluationContext(subject_key="user-1")
        )
        self.assertTrue(result.is_experimental)


if __name__ == "__main__":
    unittest.main()
