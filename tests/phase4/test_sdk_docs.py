"""Executes every example in the SDK guide as a doctest.

Documentation that is not run is documentation that drifts. The integration
examples are the first thing a reader copies, so they are the last thing that
should be allowed to go stale silently — if the API changes and `docs/SDK.md`
does not, this fails.
"""

import doctest
import unittest
from pathlib import Path

SDK_DOC = Path(__file__).resolve().parents[2] / "docs" / "SDK.md"


class SdkDocumentationTests(unittest.TestCase):
    def test_the_sdk_guide_exists(self):
        self.assertTrue(SDK_DOC.exists(), f"{SDK_DOC} is missing")

    def test_every_sdk_example_runs(self):
        results = doctest.testfile(
            str(SDK_DOC),
            module_relative=False,
            optionflags=doctest.ELLIPSIS | doctest.NORMALIZE_WHITESPACE,
            verbose=False,
        )
        # Both assertions matter: zero failures is vacuous if nothing ran.
        self.assertGreater(results.attempted, 0, "no examples were executed")
        self.assertEqual(
            results.failed, 0, f"{results.failed} of {results.attempted} examples failed"
        )

    def test_the_guide_actually_contains_examples(self):
        """Guards against the doctest passing because there is nothing to run."""
        parser = doctest.DocTestParser()
        test = parser.get_doctest(
            SDK_DOC.read_text(encoding="utf-8"), {}, "SDK.md", str(SDK_DOC), 0
        )
        self.assertGreaterEqual(len(test.examples), 30)

    def test_the_guide_covers_the_three_integration_patterns(self):
        content = SDK_DOC.read_text(encoding="utf-8").lower()
        for pattern in ("non-ai fallback", "prompt version a/b", "model swap"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, content)


if __name__ == "__main__":
    unittest.main()
