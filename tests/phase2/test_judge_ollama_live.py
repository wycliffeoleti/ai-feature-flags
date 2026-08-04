"""Live `OllamaJudge` tests against a real local model.

Skipped unless ``AIFLAGS_OLLAMA_MODEL`` is set, because these make real inference
calls — a couple of seconds each — and the default suite must stay fast and
deterministic.

What they assert is deliberately narrow. A language model's scores are not
reproducible, so pinning exact values would produce a flaky test that says
nothing. What must hold is the property the rollout actually depends on: **the
judge separates a broken output from a clean one by enough to cross the gate.**

The outputs scored here come from the demo's own generator rather than
hand-written strings. That matters more than it sounds: an earlier version used
plausible-looking subject lines of its own invention, and `phi4-mini` rated some
of *those* 2.0 despite them being perfectly good — a ~19% false-alarm rate that
made the test flaky. On the text the system actually produces it does not misfire
at all. A judge's reliability is sensitive to phrasing, so a test about a judge
has to score the real thing.

The assertions use **P10**, because that is what the demo policy gates on.

Run with:

    AIFLAGS_OLLAMA_MODEL=phi4-mini uv run pytest tests/phase2/test_judge_ollama_live.py
"""

import os
import statistics
import unittest

from aiflags.demo.generator import (
    BROKEN_TEMPLATE,
    EMAILS,
    GOOD_TEMPLATE,
    SubjectLineGenerator,
)
from aiflags.judge.base import MAX_SCORE, MIN_SCORE
from aiflags.judge.ollama import OllamaJudge

MODEL = os.environ.get("AIFLAGS_OLLAMA_MODEL")
ENDPOINT = os.environ.get("AIFLAGS_OLLAMA_ENDPOINT", "http://127.0.0.1:11434")

_generator = SubjectLineGenerator()

# The demo's real outputs, not invented ones. See the module docstring.
CLEAN = [_generator.generate(e, {"template": GOOD_TEMPLATE}) for e in EMAILS]
BROKEN = [_generator.generate(e, {"template": BROKEN_TEMPLATE}) for e in EMAILS]

GATE_THRESHOLD = 3.0
"""The demo's P10 threshold. Separation has to straddle this to matter."""

REPEATS = 2
"""Each output is scored more than once.

A single sample per output cannot show the variance, and the variance is the
interesting part: it is what the P10 gate has to survive.
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

    def test_the_gate_tolerates_a_judge_that_misses_some_defects(self):
        """Documents the asymmetry P10 depends on.

        `phi4-mini` does not catch every broken output — measured at roughly
        three in four. P10 still fires, because the misses raise the *upper* part
        of the distribution while the bottom decile stays bad.

        What P10 could not survive is the opposite error: false alarms on clean
        output would drag the baseline's own P10 down and destroy the separation.
        That is asserted by `test_clean_output_clears_the_gate_on_p10`, which is
        the more fragile of the two and the one to watch.
        """
        scores = self._scores(self.broken)
        self.assertLess(
            percentile(scores, 0.10),
            max(scores),
            "broken scores showed no spread at all; the judge may have become "
            "deterministic, in which case this tolerance no longer applies",
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
