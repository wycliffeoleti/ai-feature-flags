"""Quality judges.

The contract that matters is not "produces good scores" — it is **a judge that
fails must say so**. Every judge returns a verdict that is either scored or
explicitly unscored with a reason, and no failure path is allowed to invent a
number. A judge that returned 0.0 on timeout would manufacture a regression; one
that returned 5.0 would hide one. Both are worse than admitting ignorance, which
the unscored-rate gate then acts on.

The Ollama judge is checked for its boundary behaviour without a model running:
it must refuse a non-loopback endpoint outright, and it must degrade to unscored
rather than raise when nothing answers.
"""

import unittest

from aiflags.judge.base import JudgeVerdict
from aiflags.judge.fixture import FixtureJudge
from aiflags.judge.ollama import OllamaJudge


class VerdictTests(unittest.TestCase):
    def test_a_scored_verdict_carries_a_score(self):
        verdict = JudgeVerdict.scored_at(4.0, "looks fine")
        self.assertTrue(verdict.scored)
        self.assertEqual(verdict.score, 4.0)

    def test_an_unscored_verdict_has_no_score(self):
        verdict = JudgeVerdict.unscored("judge timed out")
        self.assertFalse(verdict.scored)
        self.assertIsNone(verdict.score)
        self.assertIn("timed out", verdict.reason)

    def test_a_scored_verdict_cannot_be_created_without_a_score(self):
        with self.assertRaises(ValueError):
            JudgeVerdict(score=None, reason="x", scored=True)

    def test_an_unscored_verdict_cannot_smuggle_a_score(self):
        """Otherwise an unscored sample could still move the statistics."""
        with self.assertRaises(ValueError):
            JudgeVerdict(score=3.0, reason="x", scored=False)


class FixtureJudgeTests(unittest.TestCase):
    def setUp(self):
        self.judge = FixtureJudge()

    def test_a_reasonable_output_scores_well(self):
        verdict = self.judge.score("Your invoice for March is ready")
        self.assertTrue(verdict.scored)
        self.assertGreaterEqual(verdict.score, 4.0)

    def test_scoring_is_deterministic(self):
        first = self.judge.score("Your invoice for March is ready")
        for _ in range(5):
            self.assertEqual(
                self.judge.score("Your invoice for March is ready").score, first.score
            )

    def test_an_empty_output_scores_at_the_floor(self):
        self.assertEqual(self.judge.score("").score, 1.0)

    def test_an_unrendered_template_placeholder_is_penalised(self):
        """The classic broken-prompt symptom: the template leaks to the user."""
        good = self.judge.score("Your invoice for March is ready").score
        leaked = self.judge.score("Your invoice for {month} is ready").score
        self.assertLess(leaked, good)

    def test_a_rambling_output_is_penalised(self):
        good = self.judge.score("Your invoice for March is ready").score
        rambling = self.judge.score("word " * 200).score
        self.assertLess(rambling, good)

    def test_shouting_is_penalised(self):
        good = self.judge.score("Your invoice for March is ready").score
        shouting = self.judge.score("YOUR INVOICE FOR MARCH IS READY").score
        self.assertLess(shouting, good)

    def test_scores_stay_within_the_one_to_five_band(self):
        for output in ("", "ok", "word " * 500, "{x}" * 50, "A normal subject line"):
            with self.subTest(output=output[:20]):
                score = self.judge.score(output).score
                self.assertGreaterEqual(score, 1.0)
                self.assertLessEqual(score, 5.0)

    def test_the_verdict_explains_itself(self):
        self.assertTrue(self.judge.score("{unrendered}").reason.strip())

    def test_a_none_output_is_unscored_rather_than_zero(self):
        verdict = self.judge.score(None)
        self.assertFalse(verdict.scored)


class OllamaJudgeBoundaryTests(unittest.TestCase):
    def test_a_non_loopback_endpoint_is_refused(self):
        """The judge is opt-in and local-only; it must not become an exfil path."""
        for endpoint in (
            "http://example.com:11434",
            "https://api.openai.com",
            "http://192.168.1.50:11434",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    OllamaJudge(endpoint=endpoint, model="llama3")

    def test_loopback_endpoints_are_accepted(self):
        for endpoint in ("http://127.0.0.1:11434", "http://localhost:11434"):
            with self.subTest(endpoint=endpoint):
                OllamaJudge(endpoint=endpoint, model="llama3")

    def test_an_unreachable_judge_returns_unscored_rather_than_raising(self):
        judge = OllamaJudge(
            endpoint="http://127.0.0.1:1", model="llama3", timeout_seconds=0.25
        )
        verdict = judge.score("anything")
        self.assertFalse(verdict.scored)
        self.assertTrue(verdict.reason.strip())

    def test_a_malformed_model_reply_is_unscored(self):
        judge = OllamaJudge(endpoint="http://127.0.0.1:11434", model="llama3")
        self.assertFalse(judge._verdict_from_reply("I'd rather not say").scored)

    def test_a_numeric_reply_is_parsed_and_clamped(self):
        judge = OllamaJudge(endpoint="http://127.0.0.1:11434", model="llama3")
        self.assertEqual(judge._verdict_from_reply("4").score, 4.0)
        self.assertEqual(judge._verdict_from_reply("Score: 2/5").score, 2.0)
        self.assertEqual(judge._verdict_from_reply("9").score, 5.0)
        self.assertEqual(judge._verdict_from_reply("0").score, 1.0)


if __name__ == "__main__":
    unittest.main()
