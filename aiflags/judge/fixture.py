"""Deterministic fixture judge — the default.

This is **not** an LLM-as-judge and does not pretend to be. It applies a fixed
rubric of surface heuristics that catch the ways a broken prompt actually breaks:
empty output, an unrendered template placeholder leaking to the user, rambling
past any sane length, or shouting.

Its purpose is to make the whole rollout pipeline deterministic and instant, so
the rollback machinery can be tested and demonstrated without a model, a
credential, or a cent. When a real model is wanted, :class:`OllamaJudge` is the
opt-in local alternative — same protocol, no code changes anywhere else.

Scores are reproducible and explain themselves, which matters because they end up
in the audit record justifying a rollback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aiflags.judge.base import MAX_SCORE, JudgeVerdict, clamp

_PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}|\{\{.*?\}\}|<[A-Z_]{3,}>")
_WORD = re.compile(r"\S+")


@dataclass(frozen=True, slots=True)
class Penalty:
    """One deduction, with the explanation that goes into the audit record."""

    amount: float
    reason: str


class FixtureJudge:
    """Scores an output against a fixed, deterministic rubric."""

    def __init__(
        self,
        max_words: int = 40,
        min_words: int = 2,
    ) -> None:
        self._max_words = max_words
        self._min_words = min_words

    def score(
        self, output: str | None, context: dict[str, Any] | None = None
    ) -> JudgeVerdict:
        if output is None:
            # No output at all is a pipeline failure, not a quality signal.
            return JudgeVerdict.unscored("no output was recorded for this evaluation")

        stripped = output.strip()
        if not stripped:
            return JudgeVerdict.scored_at(1.0, "empty output")

        penalties = list(self._penalties(stripped))
        total = sum(penalty.amount for penalty in penalties)
        reason = (
            "; ".join(penalty.reason for penalty in penalties)
            if penalties
            else "no rubric violations"
        )
        return JudgeVerdict.scored_at(clamp(MAX_SCORE - total), reason)

    def _penalties(self, output: str):
        words = _WORD.findall(output)

        if _PLACEHOLDER.search(output):
            # The classic broken-prompt symptom: the template leaks verbatim.
            yield Penalty(2.5, "contains an unrendered template placeholder")

        if len(words) > self._max_words:
            excess = len(words) - self._max_words
            # Scales with how far over the limit it ran, capped so that length
            # alone cannot bottom out a score that is otherwise fine.
            yield Penalty(
                min(2.5, 1.0 + excess / self._max_words),
                f"rambling: {len(words)} words against a limit of {self._max_words}",
            )
        elif len(words) < self._min_words:
            yield Penalty(1.5, f"terse: only {len(words)} word(s)")

        letters = [character for character in output if character.isalpha()]
        if len(letters) >= 8 and all(character.isupper() for character in letters):
            yield Penalty(1.5, "entirely upper case")

        if output.count("!") >= 3:
            yield Penalty(1.0, "excessive exclamation")
