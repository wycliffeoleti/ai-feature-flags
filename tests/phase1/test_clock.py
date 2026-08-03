"""Time is injected everywhere so rollout logic is testable and demoable.

A rollout plan is written in real durations (2 hours, 24 hours). Tests need those
to pass instantly and deterministically; the portfolio demo needs the *same* plan
to finish in minutes. Both are the clock's problem, not the controller's — the
controller never reads the wall clock.
"""

import unittest
from datetime import UTC, datetime, timedelta

from aiflags.clock import FakeClock, ScaledClock, SystemClock

EPOCH = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


class FakeClockTests(unittest.TestCase):
    def test_starts_at_the_given_instant_and_does_not_drift(self):
        clock = FakeClock(EPOCH)
        self.assertEqual(clock.now(), EPOCH)
        self.assertEqual(clock.now(), EPOCH)

    def test_advance_moves_time_forward(self):
        clock = FakeClock(EPOCH)
        clock.advance(90.0)
        self.assertEqual(clock.now(), EPOCH + timedelta(seconds=90))

    def test_advance_accepts_timedelta(self):
        clock = FakeClock(EPOCH)
        clock.advance(timedelta(hours=2))
        self.assertEqual(clock.now(), EPOCH + timedelta(hours=2))

    def test_time_never_runs_backwards(self):
        clock = FakeClock(EPOCH)
        with self.assertRaises(ValueError):
            clock.advance(-1.0)


class ScaledClockTests(unittest.TestCase):
    def test_scale_of_one_matches_the_underlying_clock(self):
        base = FakeClock(EPOCH)
        clock = ScaledClock(base, factor=1.0)
        base.advance(60.0)
        self.assertEqual(clock.now(), EPOCH + timedelta(seconds=60))

    def test_elapsed_time_is_multiplied_by_the_factor(self):
        """A 3600x demo turns one real second into one simulated hour."""
        base = FakeClock(EPOCH)
        clock = ScaledClock(base, factor=3600.0)
        base.advance(2.0)
        self.assertEqual(clock.now(), EPOCH + timedelta(hours=2))

    def test_origin_is_unchanged_before_any_elapsed_time(self):
        clock = ScaledClock(FakeClock(EPOCH), factor=3600.0)
        self.assertEqual(clock.now(), EPOCH)

    def test_factor_must_be_positive(self):
        with self.assertRaises(ValueError):
            ScaledClock(FakeClock(EPOCH), factor=0.0)


class SystemClockTests(unittest.TestCase):
    def test_returns_timezone_aware_utc(self):
        moment = SystemClock().now()
        self.assertIsNotNone(moment.tzinfo)
        self.assertEqual(moment.utcoffset(), timedelta(0))

    def test_is_monotonic_across_consecutive_reads(self):
        clock = SystemClock()
        self.assertLessEqual(clock.now(), clock.now())


if __name__ == "__main__":
    unittest.main()
