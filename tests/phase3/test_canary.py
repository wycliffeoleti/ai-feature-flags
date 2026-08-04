"""Canary comparison of experimental against baseline.

The framing here is **non-inferiority**, not ordinary significance testing, and
that distinction is the point of the module.

Asking "is the experiment significantly worse?" and advancing whenever the answer
is no rewards having too little data: a tiny, noisy sample is never significant,
so a naive gate would ramp fastest exactly when it knows least. Instead the gate
asks "can we rule out a regression larger than the margin?" and only advances
when the confidence interval says yes. Not knowing produces INCONCLUSIVE, which
the controller treats as hold.
"""

import random
import statistics
import unittest

from aiflags.core.canary import CanaryResult, compare
from aiflags.core.models import CanaryVerdict


def normal(mean, sigma, n, seed):
    rng = random.Random(seed)
    return [rng.gauss(mean, sigma) for _ in range(n)]


def centered_normal(mean, sigma, n, seed):
    """A normal draw shifted to have *exactly* the requested sample mean.

    The non-inferiority tests are claims about how wide the confidence interval
    is at a given sample size, so the observed effect has to be pinned at zero.
    Left to chance, a lucky draw shifts the sample mean enough to change the
    verdict and the test stops testing what it says it does.
    """
    values = normal(mean, sigma, n, seed)
    offset = mean - statistics.fmean(values)
    return [value + offset for value in values]


def skewed(scale, n, seed):
    """Heavily right-skewed, like latency — deliberately non-normal."""
    rng = random.Random(seed)
    return [rng.expovariate(1.0 / scale) for _ in range(n)]


class InsufficientDataTests(unittest.TestCase):
    def test_too_few_experimental_samples_is_inconclusive(self):
        result = compare(normal(4.0, 0.5, 5, 1), normal(4.0, 0.5, 100, 2))
        self.assertEqual(result.verdict, CanaryVerdict.INCONCLUSIVE)
        self.assertEqual(result.test, "insufficient")

    def test_too_few_baseline_samples_is_inconclusive(self):
        result = compare(normal(4.0, 0.5, 100, 1), normal(4.0, 0.5, 5, 2))
        self.assertEqual(result.verdict, CanaryVerdict.INCONCLUSIVE)

    def test_empty_input_is_inconclusive_rather_than_an_error(self):
        self.assertEqual(compare([], []).verdict, CanaryVerdict.INCONCLUSIVE)

    def test_insufficient_result_reports_the_sample_counts(self):
        result = compare(normal(4.0, 0.5, 5, 1), normal(4.0, 0.5, 100, 2))
        self.assertEqual(result.n_experimental, 5)
        self.assertEqual(result.n_baseline, 100)


class EquivalentVariantTests(unittest.TestCase):
    def test_an_identical_variant_is_no_worse(self):
        result = compare(normal(4.0, 0.5, 300, 1), normal(4.0, 0.5, 300, 2))
        self.assertEqual(result.verdict, CanaryVerdict.NO_WORSE)

    def test_a_clearly_better_variant_is_no_worse(self):
        result = compare(normal(4.6, 0.5, 300, 1), normal(4.0, 0.5, 300, 2))
        self.assertEqual(result.verdict, CanaryVerdict.NO_WORSE)
        self.assertGreater(result.effect, 0.0)


class RegressionTests(unittest.TestCase):
    def test_a_clearly_worse_variant_is_worse(self):
        result = compare(normal(3.0, 0.5, 300, 1), normal(4.0, 0.5, 300, 2))
        self.assertEqual(result.verdict, CanaryVerdict.WORSE)
        self.assertLess(result.effect, 0.0)

    def test_a_worse_variant_reports_a_significant_p_value(self):
        result = compare(normal(3.0, 0.5, 300, 1), normal(4.0, 0.5, 300, 2))
        self.assertLess(result.p_value, 0.05)


class NonInferiorityTests(unittest.TestCase):
    def test_a_noisy_small_sample_does_not_advance(self):
        """The failure a plain significance test has: quiet because it is blind.

        Thirty very noisy samples cannot rule out a real regression, so the
        honest answer is INCONCLUSIVE rather than "not significant, ship it".
        """
        result = compare(
            centered_normal(4.0, 3.0, 30, 1), centered_normal(4.0, 3.0, 30, 2)
        )
        self.assertEqual(result.verdict, CanaryVerdict.INCONCLUSIVE)

    def test_the_same_data_at_scale_becomes_conclusive(self):
        result = compare(
            centered_normal(4.0, 3.0, 3000, 1), centered_normal(4.0, 3.0, 3000, 2)
        )
        self.assertEqual(result.verdict, CanaryVerdict.NO_WORSE)

    def test_a_regression_smaller_than_the_margin_is_tolerated(self):
        result = compare(
            normal(3.98, 0.5, 500, 1), normal(4.0, 0.5, 500, 2), margin=0.2
        )
        self.assertEqual(result.verdict, CanaryVerdict.NO_WORSE)

    def test_a_regression_larger_than_the_margin_is_worse(self):
        result = compare(
            normal(3.4, 0.5, 500, 1), normal(4.0, 0.5, 500, 2), margin=0.2
        )
        self.assertEqual(result.verdict, CanaryVerdict.WORSE)

    def test_a_confidence_interval_is_reported(self):
        result = compare(normal(4.0, 0.5, 300, 1), normal(4.0, 0.5, 300, 2))
        self.assertIsNotNone(result.ci_low)
        self.assertIsNotNone(result.ci_high)
        self.assertLessEqual(result.ci_low, result.ci_high)


class DirectionTests(unittest.TestCase):
    def test_lower_is_better_inverts_the_comparison(self):
        """Latency and error rate are signals where smaller is the good outcome."""
        faster = compare(
            normal(100.0, 10.0, 300, 1),
            normal(200.0, 10.0, 300, 2),
            higher_is_better=False,
        )
        self.assertEqual(faster.verdict, CanaryVerdict.NO_WORSE)

    def test_lower_is_better_still_catches_a_regression(self):
        slower = compare(
            normal(400.0, 10.0, 300, 1),
            normal(200.0, 10.0, 300, 2),
            higher_is_better=False,
        )
        self.assertEqual(slower.verdict, CanaryVerdict.WORSE)


class TestSelectionTests(unittest.TestCase):
    def test_normal_data_uses_welch(self):
        result = compare(normal(4.0, 0.5, 300, 1), normal(4.0, 0.5, 300, 2))
        self.assertEqual(result.test, "welch")

    def test_skewed_data_falls_back_to_mann_whitney(self):
        result = compare(skewed(100.0, 300, 1), skewed(100.0, 300, 2))
        self.assertEqual(result.test, "mann_whitney")

    def test_mann_whitney_detects_a_shifted_distribution(self):
        result = compare(
            skewed(300.0, 300, 1), skewed(100.0, 300, 2), higher_is_better=False
        )
        self.assertEqual(result.verdict, CanaryVerdict.WORSE)

    def test_mann_whitney_accepts_an_unshifted_distribution(self):
        result = compare(
            skewed(100.0, 400, 1), skewed(100.0, 400, 2), higher_is_better=False
        )
        self.assertIn(
            result.verdict, (CanaryVerdict.NO_WORSE, CanaryVerdict.INCONCLUSIVE)
        )


class DegenerateInputTests(unittest.TestCase):
    def test_zero_variance_identical_samples_are_no_worse(self):
        result = compare([4.0] * 100, [4.0] * 100)
        self.assertEqual(result.verdict, CanaryVerdict.NO_WORSE)

    def test_zero_variance_regression_is_worse(self):
        result = compare([2.0] * 100, [4.0] * 100)
        self.assertEqual(result.verdict, CanaryVerdict.WORSE)

    def test_result_is_serialisable_for_the_audit_log(self):
        result = compare(normal(4.0, 0.5, 100, 1), normal(4.0, 0.5, 100, 2))
        self.assertIsInstance(result, CanaryResult)
        payload = result.as_dict()
        self.assertEqual(payload["verdict"], result.verdict.value)
        self.assertIn("p_value", payload)


if __name__ == "__main__":
    unittest.main()
