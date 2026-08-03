"""Bucketing must be deterministic, uniform, and independent across flags.

The frozen vectors in ``vectors/bucket_vectors.json`` are a regression lock, not
a specification: they pin the current hash so a future refactor cannot silently
reshuffle live users mid-rollout.
"""

import json
import statistics
import unittest
from pathlib import Path

from aiflags.core.bucketing import bucket

VECTORS_PATH = Path(__file__).parent / "vectors" / "bucket_vectors.json"


class BucketRangeTests(unittest.TestCase):
    def test_bucket_is_in_unit_interval(self):
        for i in range(1000):
            value = bucket(f"user-{i}", "subject_line_v2", "salt-a")
            self.assertGreaterEqual(value, 0.0)
            self.assertLess(value, 1.0)

    def test_bucket_is_deterministic(self):
        first = bucket("user-42", "subject_line_v2", "salt-a")
        for _ in range(5):
            self.assertEqual(bucket("user-42", "subject_line_v2", "salt-a"), first)

    def test_bucket_depends_on_every_input(self):
        base = bucket("user-42", "subject_line_v2", "salt-a")
        self.assertNotEqual(bucket("user-43", "subject_line_v2", "salt-a"), base)
        self.assertNotEqual(bucket("user-42", "other_flag", "salt-a"), base)
        self.assertNotEqual(bucket("user-42", "subject_line_v2", "salt-b"), base)

    def test_separator_prevents_field_boundary_collisions(self):
        """``("ab", "c")`` and ``("a", "bc")`` must not hash to the same bucket."""
        self.assertNotEqual(
            bucket("user", "ab", "c"),
            bucket("user", "a", "bc"),
        )


class BucketDistributionTests(unittest.TestCase):
    def test_bucket_is_independent_across_flags(self):
        """A user unlucky in one flag must not be systematically unlucky in another."""
        a = [bucket(f"user-{i}", "flag_a", "salt") for i in range(2000)]
        b = [bucket(f"user-{i}", "flag_b", "salt") for i in range(2000)]
        mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
        covariance = statistics.fmean(
            (x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True)
        )
        correlation = covariance / (statistics.pstdev(a) * statistics.pstdev(b))
        self.assertLess(abs(correlation), 0.05)

    def test_bucket_is_approximately_uniform(self):
        values = [
            bucket(f"user-{i}", "subject_line_v2", "salt-a") for i in range(10_000)
        ]
        deciles = [0] * 10
        for value in values:
            deciles[int(value * 10)] += 1
        for count in deciles:
            self.assertTrue(
                850 <= count <= 1150, f"decile counts not uniform: {deciles}"
            )


class BucketRegressionLockTests(unittest.TestCase):
    def test_bucket_matches_frozen_vectors(self):
        self.assertTrue(
            VECTORS_PATH.exists(),
            f"{VECTORS_PATH} is missing; regenerate with "
            "`python -m aiflags.core.bucketing --freeze-vectors`",
        )
        vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
        self.assertTrue(vectors, "frozen vector file must not be empty")
        for case in vectors:
            actual = bucket(case["subject_key"], case["flag_key"], case["salt"])
            self.assertAlmostEqual(
                actual,
                case["bucket"],
                places=12,
                msg=f"bucketing changed for {case}; this reshuffles users mid-rollout",
            )


if __name__ == "__main__":
    unittest.main()
