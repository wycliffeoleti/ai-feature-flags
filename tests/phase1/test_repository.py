"""Repository contract, run against every implementation.

The same test body runs against the in-memory store and against PostgreSQL, so
the two cannot drift. The PostgreSQL case skips when no database is reachable —
the default suite must stay runnable with no services at all — but it is the same
assertions, not a weaker set.

Two invariants carry Phase 1's audit requirement:

* every mutation records an actor and a reason, with no way to mutate without
  them, and
* every mutation bumps a single monotonic snapshot version, so the data plane can
  tell staleness from a lost update.
"""

import os
import unittest

from aiflags.core.models import (
    Comparison,
    FlagDefinition,
    FlagStatus,
    QualityGate,
    QualityPolicy,
    QualitySignal,
    Statistic,
    Variant,
    VariantKind,
)
from aiflags.store.base import FlagAlreadyExists, FlagNotFound
from aiflags.store.memory import InMemoryFlagRepository

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
        "baseline": Variant(key="v1", kind=VariantKind.BASELINE, config={"prompt": "a"}),
        "experimental": Variant(
            key="v2", kind=VariantKind.EXPERIMENTAL, config={"prompt": "b"}
        ),
        "quality_policy": POLICY,
        "salt": "salt-a",
    }
    params.update(overrides)
    return FlagDefinition(**params)


class FlagRepositoryContract:
    """Assertions every repository implementation must satisfy."""

    def make_repository(self):
        raise NotImplementedError

    def setUp(self):
        self.repo = self.make_repository()

    # -- creation and retrieval ------------------------------------------- #

    def test_a_created_flag_can_be_read_back(self):
        self.repo.create_flag(make_flag(), actor="wycliffe", reason="initial")
        stored = self.repo.get_flag("subject_line")
        self.assertEqual(stored.key, "subject_line")
        self.assertEqual(stored.baseline.config, {"prompt": "a"})
        self.assertEqual(stored.quality_policy.gates[0].threshold, 3.0)

    def test_an_unknown_flag_reads_as_none(self):
        self.assertIsNone(self.repo.get_flag("nope"))

    def test_creating_a_duplicate_key_is_rejected(self):
        self.repo.create_flag(make_flag(), actor="w", reason="initial")
        with self.assertRaises(FlagAlreadyExists):
            self.repo.create_flag(make_flag(), actor="w", reason="again")

    def test_listing_returns_every_flag(self):
        self.repo.create_flag(make_flag("a"), actor="w", reason="r")
        self.repo.create_flag(make_flag("b"), actor="w", reason="r")
        self.assertEqual({f.key for f in self.repo.list_flags()}, {"a", "b"})

    def test_mutating_an_unknown_flag_raises(self):
        with self.assertRaises(FlagNotFound):
            self.repo.set_rollout_percentage("nope", 5.0, actor="w", reason="r")

    # -- round-tripping the full model ------------------------------------ #

    def test_targeting_rules_survive_a_round_trip(self):
        from aiflags.core.models import TargetingKind, TargetingRule

        flag = make_flag(
            targeting=(
                TargetingRule(
                    kind=TargetingKind.SEGMENT,
                    values=frozenset({"internal", "beta"}),
                    variant_kind=VariantKind.EXPERIMENTAL,
                    attribute="segment",
                ),
            )
        )
        self.repo.create_flag(flag, actor="w", reason="r")
        stored = self.repo.get_flag("subject_line")
        self.assertEqual(len(stored.targeting), 1)
        self.assertEqual(stored.targeting[0].values, frozenset({"internal", "beta"}))
        self.assertEqual(stored.targeting[0].attribute, "segment")

    def test_the_rollout_plan_survives_a_round_trip(self):
        self.repo.create_flag(make_flag(), actor="w", reason="r")
        stored = self.repo.get_flag("subject_line")
        self.assertEqual(len(stored.rollout_plan.stages), 5)
        self.assertEqual(stored.rollout_plan.stages[0].percentage, 1.0)
        self.assertEqual(stored.rollout_plan.stages[0].dwell_seconds, 7200.0)

    # -- snapshot versioning ---------------------------------------------- #

    def test_the_snapshot_version_advances_on_every_mutation(self):
        first = self.repo.create_flag(make_flag(), actor="w", reason="r")
        second = self.repo.set_rollout_percentage(
            "subject_line", 5.0, actor="w", reason="ramp"
        )
        third = self.repo.set_status(
            "subject_line", FlagStatus.PAUSED, actor="w", reason="hold"
        )
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_the_snapshot_carries_the_current_flags_and_version(self):
        version = self.repo.create_flag(make_flag(), actor="w", reason="r")
        snapshot = self.repo.snapshot()
        self.assertEqual(snapshot.version, version)
        self.assertIn("subject_line", snapshot.flags)
        self.assertIsNotNone(snapshot.published_at.tzinfo)

    def test_the_snapshot_reflects_the_latest_percentage(self):
        self.repo.create_flag(make_flag(), actor="w", reason="r")
        self.repo.set_rollout_percentage("subject_line", 25.0, actor="w", reason="ramp")
        self.assertEqual(
            self.repo.snapshot().flags["subject_line"].rollout_percentage, 25.0
        )

    def test_an_empty_repository_has_a_zero_version_snapshot(self):
        snapshot = self.repo.snapshot()
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.flags, {})

    # -- audit -------------------------------------------------------------- #

    def test_every_mutation_is_audited_with_an_actor_and_a_reason(self):
        self.repo.create_flag(make_flag(), actor="wycliffe", reason="initial")
        self.repo.set_rollout_percentage(
            "subject_line", 5.0, actor="controller", reason="stage 1 passed"
        )
        self.repo.set_status(
            "subject_line", FlagStatus.ROLLED_BACK, actor="controller", reason="p10 low"
        )
        events = self.repo.audit_events()
        self.assertEqual(len(events), 3)
        for event in events:
            self.assertTrue(event.actor)
            self.assertTrue(event.reason)
            self.assertIsNotNone(event.at.tzinfo)

    def test_audit_records_the_action_and_the_resulting_version(self):
        version = self.repo.create_flag(make_flag(), actor="w", reason="initial")
        event = self.repo.audit_events()[0]
        self.assertEqual(event.action, "create_flag")
        self.assertEqual(event.flag_key, "subject_line")
        self.assertEqual(event.snapshot_version, version)

    def test_audit_records_what_changed(self):
        self.repo.create_flag(make_flag(), actor="w", reason="r")
        self.repo.set_rollout_percentage("subject_line", 25.0, actor="w", reason="ramp")
        event = self.repo.audit_events()[-1]
        self.assertEqual(event.detail["percentage"], 25.0)

    def test_audit_can_be_filtered_by_flag(self):
        self.repo.create_flag(make_flag("a"), actor="w", reason="r")
        self.repo.create_flag(make_flag("b"), actor="w", reason="r")
        self.repo.set_rollout_percentage("a", 5.0, actor="w", reason="ramp")
        self.assertEqual(len(self.repo.audit_events(flag_key="a")), 2)
        self.assertEqual(len(self.repo.audit_events(flag_key="b")), 1)

    def test_audit_is_ordered_oldest_first(self):
        self.repo.create_flag(make_flag(), actor="w", reason="one")
        self.repo.set_rollout_percentage("subject_line", 5.0, actor="w", reason="two")
        reasons = [event.reason for event in self.repo.audit_events()]
        self.assertEqual(reasons, ["one", "two"])

    def test_audit_is_append_only(self):
        """A rollback record that can be edited is not an audit trail."""
        self.repo.create_flag(make_flag(), actor="w", reason="r")
        events = self.repo.audit_events()
        events.clear()
        self.assertEqual(len(self.repo.audit_events()), 1)

    def test_rollback_clears_the_percentage_and_sets_the_status_together(self):
        self.repo.create_flag(make_flag(), actor="w", reason="r")
        self.repo.set_rollout_percentage("subject_line", 50.0, actor="w", reason="ramp")
        self.repo.rollback("subject_line", actor="controller", reason="p10 below 3.0")
        stored = self.repo.get_flag("subject_line")
        self.assertEqual(stored.status, FlagStatus.ROLLED_BACK)
        self.assertEqual(stored.rollout_percentage, 0.0)

    def test_rollback_is_a_single_audit_event(self):
        """One operational decision must not be split across two entries."""
        self.repo.create_flag(make_flag(), actor="w", reason="r")
        before = len(self.repo.audit_events())
        self.repo.rollback("subject_line", actor="controller", reason="p10 below 3.0")
        events = self.repo.audit_events()
        self.assertEqual(len(events), before + 1)
        self.assertEqual(events[-1].action, "rollback")

    def test_rollback_records_the_percentage_it_reverted_from(self):
        self.repo.create_flag(make_flag(), actor="w", reason="r")
        self.repo.set_rollout_percentage("subject_line", 25.0, actor="w", reason="ramp")
        self.repo.rollback("subject_line", actor="controller", reason="degraded")
        detail = self.repo.audit_events()[-1].detail
        self.assertEqual(detail["previous_percentage"], 25.0)
        self.assertEqual(detail["percentage"], 0.0)

    def test_rolling_back_an_unknown_flag_raises(self):
        with self.assertRaises(FlagNotFound):
            self.repo.rollback("nope", actor="w", reason="r")

    def test_a_blank_actor_or_reason_is_rejected(self):
        with self.assertRaises(ValueError):
            self.repo.create_flag(make_flag(), actor="", reason="r")
        with self.assertRaises(ValueError):
            self.repo.create_flag(make_flag("other"), actor="w", reason="   ")


class InMemoryFlagRepositoryTests(FlagRepositoryContract, unittest.TestCase):
    def make_repository(self):
        return InMemoryFlagRepository()


@unittest.skipUnless(
    os.environ.get("AIFLAGS_TEST_POSTGRES_DSN"),
    "set AIFLAGS_TEST_POSTGRES_DSN to run the PostgreSQL contract tests",
)
class PostgresFlagRepositoryTests(FlagRepositoryContract, unittest.TestCase):
    def make_repository(self):
        from aiflags.store.postgres import PostgresFlagRepository

        repo = PostgresFlagRepository(os.environ["AIFLAGS_TEST_POSTGRES_DSN"])
        repo.reset_for_tests()
        return repo


if __name__ == "__main__":
    unittest.main()
