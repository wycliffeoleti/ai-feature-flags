"""Quality store contract, run against every implementation.

The invariant carried through to the database: an unscored observation must not
hold a value, and a scored one must. Enforced in the dataclass *and* as a CHECK
constraint, because this table outlives the process that wrote it and is read
directly by the analytics view.
"""

import os
import unittest
from datetime import UTC, datetime, timedelta

from aiflags.core.models import QualitySignal, VariantKind
from aiflags.store.quality import (
    DecisionRecord,
    InMemoryQualityStore,
    QualityObservation,
    StoredRolloutState,
)

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def observation(value=4.0, scored=True, offset=0, **overrides):
    params = {
        "flag_key": "subject_line",
        "evaluation_id": f"eval-{offset}",
        "variant_kind": VariantKind.EXPERIMENTAL,
        "signal": QualitySignal.JUDGE_SCORE,
        "value": value,
        "scored": scored,
        "occurred_at": EPOCH + timedelta(seconds=offset),
    }
    params.update(overrides)
    return QualityObservation(**params)


class QualityStoreContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    # -- observations -------------------------------------------------------- #

    def test_recorded_observations_come_back_as_samples(self):
        self.store.record_observations(
            [observation(4.0, offset=0), observation(5.0, offset=1)]
        )
        samples = self.store.samples(
            "subject_line", QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL
        )
        self.assertEqual([s.value for s in samples], [4.0, 5.0])

    def test_recording_an_empty_batch_is_a_no_op(self):
        self.assertEqual(self.store.record_observations([]), 0)

    def test_samples_are_oldest_first_within_the_window(self):
        """`summarize` reads the tail as most recent, so ordering is load-bearing."""
        self.store.record_observations(
            [observation(float(i), offset=i) for i in range(10)]
        )
        samples = self.store.samples(
            "subject_line", QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL
        )
        self.assertEqual([s.value for s in samples], [float(i) for i in range(10)])

    def test_the_limit_keeps_the_most_recent_samples(self):
        self.store.record_observations(
            [observation(float(i), offset=i) for i in range(100)]
        )
        samples = self.store.samples(
            "subject_line",
            QualitySignal.JUDGE_SCORE,
            VariantKind.EXPERIMENTAL,
            limit=10,
        )
        self.assertEqual([s.value for s in samples], [float(i) for i in range(90, 100)])

    def test_variants_are_kept_separate(self):
        self.store.record_observations(
            [
                observation(1.0, offset=0, variant_kind=VariantKind.EXPERIMENTAL),
                observation(5.0, offset=1, variant_kind=VariantKind.BASELINE),
            ]
        )
        experimental = self.store.samples(
            "subject_line", QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL
        )
        baseline = self.store.samples(
            "subject_line", QualitySignal.JUDGE_SCORE, VariantKind.BASELINE
        )
        self.assertEqual([s.value for s in experimental], [1.0])
        self.assertEqual([s.value for s in baseline], [5.0])

    def test_signals_are_kept_separate(self):
        self.store.record_observations(
            [
                observation(4.0, offset=0, signal=QualitySignal.JUDGE_SCORE),
                observation(900.0, offset=1, signal=QualitySignal.LATENCY_MS),
            ]
        )
        latency = self.store.samples(
            "subject_line", QualitySignal.LATENCY_MS, VariantKind.EXPERIMENTAL
        )
        self.assertEqual([s.value for s in latency], [900.0])

    def test_flags_are_kept_separate(self):
        self.store.record_observations(
            [observation(1.0, offset=0), observation(5.0, offset=1, flag_key="other")]
        )
        samples = self.store.samples(
            "subject_line", QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL
        )
        self.assertEqual(len(samples), 1)

    # -- unscored ------------------------------------------------------------- #

    def test_unscored_observations_round_trip_as_unscored(self):
        self.store.record_observations(
            [observation(None, scored=False, offset=0, reason="judge timed out")]
        )
        samples = self.store.samples(
            "subject_line", QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL
        )
        self.assertEqual(len(samples), 1)
        self.assertFalse(samples[0].scored)

    def test_an_unscored_observation_cannot_carry_a_value(self):
        with self.assertRaises(ValueError):
            observation(3.0, scored=False)

    def test_a_scored_observation_must_carry_a_value(self):
        with self.assertRaises(ValueError):
            observation(None, scored=True)

    # -- shadow --------------------------------------------------------------- #

    def test_shadow_samples_are_excluded_by_default(self):
        """Shadow output is scored, but must never reach a live rollout decision."""
        self.store.record_observations(
            [
                observation(5.0, offset=0),
                observation(1.0, offset=1, is_shadow=True),
            ]
        )
        samples = self.store.samples(
            "subject_line", QualitySignal.JUDGE_SCORE, VariantKind.EXPERIMENTAL
        )
        self.assertEqual([s.value for s in samples], [5.0])

    def test_shadow_samples_can_be_requested_explicitly(self):
        self.store.record_observations(
            [observation(1.0, offset=0, is_shadow=True)]
        )
        samples = self.store.samples(
            "subject_line",
            QualitySignal.JUDGE_SCORE,
            VariantKind.EXPERIMENTAL,
            include_shadow=True,
        )
        self.assertEqual(len(samples), 1)

    # -- rollout state --------------------------------------------------------- #

    def test_rollout_state_round_trips(self):
        self.store.save_rollout_state(
            StoredRolloutState(
                flag_key="subject_line", stage_index=2, stage_entered_at=EPOCH
            )
        )
        state = self.store.get_rollout_state("subject_line")
        self.assertEqual(state.stage_index, 2)
        self.assertEqual(state.stage_entered_at, EPOCH)
        self.assertIsNone(state.rolled_back_at)

    def test_unknown_rollout_state_is_none(self):
        self.assertIsNone(self.store.get_rollout_state("nope"))

    def test_rollout_state_is_upserted(self):
        for index in (0, 1, 2):
            self.store.save_rollout_state(
                StoredRolloutState(
                    flag_key="subject_line",
                    stage_index=index,
                    stage_entered_at=EPOCH + timedelta(hours=index),
                )
            )
        self.assertEqual(self.store.get_rollout_state("subject_line").stage_index, 2)

    def test_rollback_time_round_trips(self):
        self.store.save_rollout_state(
            StoredRolloutState(
                flag_key="subject_line",
                stage_index=1,
                stage_entered_at=EPOCH,
                rolled_back_at=EPOCH + timedelta(minutes=30),
            )
        )
        state = self.store.get_rollout_state("subject_line")
        self.assertEqual(state.rolled_back_at, EPOCH + timedelta(minutes=30))

    # -- decisions -------------------------------------------------------------- #

    def test_decisions_are_recorded_including_holds(self):
        """"Why did this sit at 5% for six hours" needs the holds, not just changes."""
        for action in ("hold", "hold", "advance"):
            self.store.record_decision(
                DecisionRecord(
                    flag_key="subject_line",
                    action=action,
                    reason=f"{action} reason",
                    decided_at=EPOCH,
                )
            )
        actions = [d.action for d in self.store.decisions("subject_line")]
        self.assertEqual(actions, ["hold", "hold", "advance"])

    def test_decision_evidence_round_trips(self):
        self.store.record_decision(
            DecisionRecord(
                flag_key="subject_line",
                action="rollback",
                reason="p10 below threshold",
                decided_at=EPOCH,
                evidence={"judge_score": {"p10": 1.8, "count": 50}},
                canary={"verdict": "worse", "p_value": 0.001},
            )
        )
        decision = self.store.decisions("subject_line")[0]
        self.assertEqual(decision.evidence["judge_score"]["p10"], 1.8)
        self.assertEqual(decision.canary["verdict"], "worse")

    def test_decisions_can_be_filtered_by_flag(self):
        for key in ("a", "a", "b"):
            self.store.record_decision(
                DecisionRecord(
                    flag_key=key, action="hold", reason="r", decided_at=EPOCH
                )
            )
        self.assertEqual(len(self.store.decisions("a")), 2)
        self.assertEqual(len(self.store.decisions()), 3)

    def test_decision_canary_may_be_absent(self):
        self.store.record_decision(
            DecisionRecord(
                flag_key="subject_line", action="hold", reason="r", decided_at=EPOCH
            )
        )
        self.assertIsNone(self.store.decisions("subject_line")[0].canary)


class InMemoryQualityStoreTests(QualityStoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryQualityStore()


@unittest.skipUnless(
    os.environ.get("AIFLAGS_TEST_POSTGRES_DSN"),
    "set AIFLAGS_TEST_POSTGRES_DSN to run the PostgreSQL contract tests",
)
class PostgresQualityStoreTests(QualityStoreContract, unittest.TestCase):
    def make_store(self):
        from aiflags.store.postgres import PostgresFlagRepository
        from aiflags.store.quality_postgres import PostgresQualityStore

        dsn = os.environ["AIFLAGS_TEST_POSTGRES_DSN"]
        PostgresFlagRepository(dsn).migrate()
        store = PostgresQualityStore(dsn)
        store.reset_for_tests()
        return store


if __name__ == "__main__":
    unittest.main()
