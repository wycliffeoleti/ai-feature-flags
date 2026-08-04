"""Live `OllamaJudge` tests against a real local model.

Skipped unless ``AIFLAGS_OLLAMA_MODEL`` is set, because these make real inference
calls — a couple of seconds each — and the default suite must stay fast and
deterministic.

What they assert is deliberately narrow. A language model's scores are not
reproducible, so pinning exact values would produce a flaky test that says
nothing. What must hold is the property the rollout actually depends on: **the
judge separates a broken output from a clean one by enough to cross the gate.**

The assertions use **P10, not the mean**, because P10 is what the demo policy
gates on. That distinction is load-bearing here rather than pedantic. Measured
over 12 repeats, `phi4-mini` scores the *same* broken output 2.0 or 4.0 —
bimodal, right about half the time — giving a mean of exactly 3.00 against a
threshold of 3.0. Its P10 is 2.0. A judge that unreliable still fires the
rollback, because the gate reads the bad tail rather than the average. Asserting
on the mean would test a property the system never relies on, and would fail
while the system worked correctly.

Run with:

    AIFLAGS_OLLAMA_MODEL=phi4-mini uv run pytest tests/phase2/test_judge_ollama_live.py
"""

import os
import statistics
import unittest

from aiflags.judge.base import MAX_SCORE, MIN_SCORE
from aiflags.judge.ollama import OllamaJudge

MODEL = os.environ.get("AIFLAGS_OLLAMA_MODEL")
ENDPOINT = os.environ.get("AIFLAGS_OLLAMA_ENDPOINT", "http://127.0.0.1:11434")

CLEAN = [
    "Your March invoice is ready",
    "Order 4417 shipped — arrives Thursday",
    "Payment receipt for 42.00 EUR",
    "Your plan renews on 12 September",
]
BROKEN = [
    "Hi {customer_name}, about your March invoice",
    "Hi {customer_name}, about your order shipped",
    "Hi {customer_name}, about your payment receipt",
    "Hi {customer_name}, about your plan renewal",
]

GATE_THRESHOLD = 3.0
"""The demo's P10 threshold. Separation has to straddle this to matter."""

REPEATS = 3
"""Each prompt is scored several times.

A single sample per prompt cannot show the variance that matters, and the
variance is the interesting part: it is what the P10 gate exists to survive.
"""


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, int(fraction * (len(ordered) - 1)))]


@unittest.skipUnless(
    MODEL, "set AIFLAGS_OLLAMA_MODEL to run the live Ollama judge tests"
)
class LiveOllamaJudgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.judge = OllamaJudge(model=MODEL, endpoint=ENDPOINT, timeout_seconds=120)
        cls.clean = [
            cls.judge.score(text) for text in CLEAN for _ in range(REPEATS)
        ]
        cls.broken = [
            cls.judge.score(text) for text in BROKEN for _ in range(REPEATS)
        ]

    @staticmethod
    def _scores(verdicts):
        return [v.score for v in verdicts if v.scored]

    def test_the_model_actually_answered(self):
        """Guards against the suite passing because everything was unscored."""
        scored = [v for v in self.clean + self.broken if v.scored]
        self.assertGreaterEqual(
            len(scored),
            len(CLEAN) + len(BROKEN),
            "the model failed to score most outputs",
        )

    def test_scores_land_in_the_one_to_five_band(self):
        for verdict in self.clean + self.broken:
            if verdict.scored:
                with self.subTest(reason=verdict.reason):
                    self.assertGreaterEqual(verdict.score, MIN_SCORE)
                    self.assertLessEqual(verdict.score, MAX_SCORE)

    def test_clean_output_clears_the_gate_on_p10(self):
        """P10, because that is the statistic the demo policy gates on."""
        scores = self._scores(self.clean)
        self.assertTrue(scores)
        observed = percentile(scores, 0.10)
        self.assertGreaterEqual(
            observed,
            GATE_THRESHOLD,
            f"clean output had p10 {observed:.2f}, below the {GATE_THRESHOLD} "
            "gate — a good variant's rollout would stall or roll back",
        )

    def test_broken_output_breaches_the_gate_on_p10(self):
        """The unrendered placeholder must be recognised as a defect.

        Measured on P10 rather than the mean: `phi4-mini` scores these bimodally
        (2.0 or 4.0), so the mean sits right on the threshold while P10 is
        clearly below it. The gate reads the tail, and so does this test.
        """
        scores = self._scores(self.broken)
        self.assertTrue(scores)
        observed = percentile(scores, 0.10)
        self.assertLess(
            observed,
            GATE_THRESHOLD,
            f"broken output had p10 {observed:.2f}, at or above the "
            f"{GATE_THRESHOLD} gate — the rollback would never fire",
        )

    def test_the_gate_survives_an_unreliable_judge(self):
        """Documents why P10 was chosen over the mean.

        A judge that flags the defect only some of the time still triggers a
        rollback. If this ever fails while the P10 tests pass, the judge has
        become reliable enough that the distinction stopped mattering — worth
        knowing, not worth breaking the build over.
        """
        scores = self._scores(self.broken)
        self.assertLess(
            percentile(scores, 0.10),
            statistics.fmean(scores),
            "broken scores showed no downward spread at all",
        )

    def test_the_separation_is_the_right_way_round(self):
        clean = statistics.fmean(self._scores(self.clean))
        broken = statistics.fmean(self._scores(self.broken))
        self.assertGreater(
            clean,
            broken,
            f"clean {clean:.2f} did not outscore broken {broken:.2f}",
        )

    def test_every_verdict_explains_itself(self):
        for verdict in self.clean + self.broken:
            with self.subTest(scored=verdict.scored):
                self.assertTrue(verdict.reason.strip())


if __name__ == "__main__":
    unittest.main()
