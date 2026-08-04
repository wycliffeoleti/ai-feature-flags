"""Rolling quality windows.

Two interpretation decisions are pinned here because the guide's wording admits
more than one reading, and both affect when a rollback fires:

* "P10 below 3.0 for more than 50 consecutive evaluations" is read as *the
  statistic computed over the trailing 50 evaluations*, not as recomputing the
  statistic at each of 50 successive points. The trailing-window reading is the
  standard one and avoids an O(n^2) recomputation on every controller tick.
* Unscored samples — where the judge failed or timed out — are excluded from
  quality statistics but counted in :attr:`WindowStats.unscored_rate`. Averaging
  them in as zeros would fake a regression; dropping them silently would hide
  a blind rollout. They get their own signal instead.
"""

import unittest
from datetime import UTC, datetime, timedelta

from aiflags.core.windows import Sample, Trend, summarize

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def samples(values, start=EPOCH, step_seconds=1.0, scored=True):
    return [
        Sample(
            value=value,
            at=start + timedelta(seconds=index * step_seconds),
            scored=scored,
        )
        for index, value in enumerate(values)
    ]


class BasicStatisticsTests(unittest.TestCase):
    def test_empty_window_is_reported_as_empty(self):
        stats = summarize([])
        self.assertEqual(stats.count, 0)
        self.assertIsNone(stats.mean)
        self.assertIsNone(stats.p10)
        self.assertEqual(stats.trend, Trend.UNKNOWN)

    def test_mean_and_stdev(self):
        stats = summarize(samples([1.0, 2.0, 3.0, 4.0, 5.0]))
        self.assertEqual(stats.count, 5)
        self.assertAlmostEqual(stats.mean, 3.0)
        self.assertAlmostEqual(stats.stdev, 1.5811388300841898)

    def test_stdev_of_a_single_sample_is_zero_not_an_error(self):
        stats = summarize(samples([4.0]))
        self.assertEqual(stats.count, 1)
        self.assertEqual(stats.stdev, 0.0)

    def test_p10_is_the_worst_tenth_percentile(self):
        stats = summarize(samples([float(v) for v in range(1, 11)]))
        self.assertAlmostEqual(stats.p10, 1.9)

    def test_p95_is_the_slow_tail(self):
        stats = summarize(samples([float(v) for v in range(1, 11)]))
        self.assertAlmostEqual(stats.p95, 9.55)

    def test_percentiles_of_a_single_sample_are_that_sample(self):
        stats = summarize(samples([7.0]))
        self.assertEqual(stats.p10, 7.0)
        self.assertEqual(stats.p95, 7.0)


class WindowSelectionTests(unittest.TestCase):
    def test_last_n_keeps_only_the_most_recent_samples(self):
        stats = summarize(samples([1.0] * 100 + [5.0] * 10), last_n=10)
        self.assertEqual(stats.count, 10)
        self.assertAlmostEqual(stats.mean, 5.0)

    def test_last_n_larger_than_the_data_keeps_everything(self):
        stats = summarize(samples([1.0, 2.0, 3.0]), last_n=100)
        self.assertEqual(stats.count, 3)

    def test_time_window_excludes_older_samples(self):
        data = samples([1.0] * 60, step_seconds=60.0)  # one per minute
        stats = summarize(data, now=data[-1].at, within_seconds=600.0)
        self.assertEqual(stats.count, 11)

    def test_time_window_requires_a_reference_instant(self):
        with self.assertRaises(ValueError):
            summarize(samples([1.0, 2.0]), within_seconds=60.0)

    def test_last_n_and_time_window_compose(self):
        data = samples([1.0] * 100, step_seconds=1.0)
        stats = summarize(data, now=data[-1].at, within_seconds=10.0, last_n=3)
        self.assertEqual(stats.count, 3)


class UnscoredSampleTests(unittest.TestCase):
    def test_unscored_samples_are_excluded_from_quality_statistics(self):
        data = samples([5.0, 5.0]) + samples([0.0], start=EPOCH, scored=False)
        stats = summarize(data)
        self.assertEqual(stats.count, 2)
        self.assertAlmostEqual(stats.mean, 5.0)

    def test_unscored_rate_is_reported_separately(self):
        data = samples([5.0] * 3) + samples([0.0], start=EPOCH, scored=False)
        stats = summarize(data)
        self.assertAlmostEqual(stats.unscored_rate, 0.25)

    def test_unscored_rate_is_zero_when_everything_scored(self):
        self.assertEqual(summarize(samples([1.0, 2.0])).unscored_rate, 0.0)

    def test_a_fully_unscored_window_has_no_quality_statistics(self):
        """A blind window must not read as a healthy one."""
        stats = summarize(samples([0.0] * 5, scored=False))
        self.assertEqual(stats.count, 0)
        self.assertIsNone(stats.mean)
        self.assertEqual(stats.unscored_rate, 1.0)

    def test_unscored_samples_count_toward_the_window_size(self):
        data = samples([5.0] * 5 + [0.0] * 5)
        data = data[:5] + [Sample(value=0.0, at=s.at, scored=False) for s in data[5:]]
        stats = summarize(data, last_n=5)
        self.assertEqual(stats.count, 0)
        self.assertEqual(stats.unscored_rate, 1.0)


class TrendTests(unittest.TestCase):
    def test_steady_values_are_stable(self):
        self.assertEqual(summarize(samples([4.0] * 40)).trend, Trend.STABLE)

    def test_rising_values_are_improving(self):
        rising = [1.0] * 20 + [4.0] * 20
        self.assertEqual(summarize(samples(rising)).trend, Trend.IMPROVING)

    def test_falling_values_are_degrading(self):
        falling = [4.0] * 20 + [1.0] * 20
        self.assertEqual(summarize(samples(falling)).trend, Trend.DEGRADING)

    def test_trend_is_unknown_below_the_minimum_sample_count(self):
        self.assertEqual(summarize(samples([1.0, 5.0])).trend, Trend.UNKNOWN)

    def test_small_fluctuations_do_not_register_as_a_trend(self):
        noisy = [4.0, 4.02, 3.98, 4.01] * 10
        self.assertEqual(summarize(samples(noisy)).trend, Trend.STABLE)


class RateTests(unittest.TestCase):
    def test_rate_treats_values_as_a_fraction_of_the_window(self):
        """Error rate is recorded as 1.0/0.0 per sample and averaged."""
        stats = summarize(samples([1.0, 0.0, 0.0, 0.0]))
        self.assertAlmostEqual(stats.mean, 0.25)


if __name__ == "__main__":
    unittest.main()
