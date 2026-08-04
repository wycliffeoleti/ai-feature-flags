"""Worker entrypoints.

Thin glue, but glue that only runs inside containers — so a typo here surfaces as
a crash-looping service rather than as a test failure. These assert the wiring
without starting either loop.
"""

import unittest
from unittest import mock

from aiflags.workers import run


class EntrypointTests(unittest.TestCase):
    def test_both_workers_are_registered(self):
        self.assertEqual(set(run.COMMANDS), {"evaluator", "controller"})

    def test_an_unknown_command_reports_usage(self):
        self.assertEqual(run.main(["run", "nonsense"]), 2)

    def test_no_command_reports_usage(self):
        self.assertEqual(run.main(["run"]), 2)

    def test_a_known_command_is_dispatched(self):
        with mock.patch.dict(run.COMMANDS, {"evaluator": mock.Mock()}) as commands:
            self.assertEqual(run.main(["run", "evaluator"]), 0)
            commands["evaluator"].assert_called_once()

    def test_a_missing_variable_fails_fast_and_names_itself(self):
        """Crash-looping on a clear message beats connecting to nothing."""
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit) as caught:
                run._require("AIFLAGS_POSTGRES_DSN")
            self.assertIn("AIFLAGS_POSTGRES_DSN", str(caught.exception))

    def test_a_present_variable_is_returned(self):
        with mock.patch.dict("os.environ", {"AIFLAGS_REDIS_URL": "redis://x"}):
            self.assertEqual(run._require("AIFLAGS_REDIS_URL"), "redis://x")


class ShutdownTests(unittest.TestCase):
    def test_a_signal_requests_shutdown_rather_than_killing_mid_batch(self):
        """Finishing the iteration avoids needless redelivery on `compose down`."""
        shutdown = run.Shutdown()
        self.assertFalse(shutdown.requested)
        shutdown._request()
        self.assertTrue(shutdown.requested)


if __name__ == "__main__":
    unittest.main()
