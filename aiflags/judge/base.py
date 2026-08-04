"""Quality judge contract.

The important rule is encoded in :class:`JudgeVerdict` rather than left to each
implementation: **a judge that fails must say so.** A verdict is either scored,
carrying a number, or explicitly unscored, carrying a reason. The type refuses
any other combination.

This matters because the two obvious ways to handle a judge failure are both
wrong. Returning 0.0 on a timeout manufactures a regression that did not happen
and triggers a false rollback. Returning a neutral score hides a real one and
lets the rollout ramp while nothing is being measured. Admitting ignorance is the
only honest option, and the unscored-rate gate is what acts on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

MIN_SCORE = 1.0
MAX_SCORE = 5.0


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """The outcome of scoring one output."""

    score: float | None
    reason: str
    scored: bool

    def __post_init__(self) -> None:
        if self.scored and self.score is None:
            raise ValueError("a scored verdict must carry a score")
        if not self.scored and self.score is not None:
            raise ValueError(
                "an unscored verdict must not carry a score; it would move the "
                "quality statistics while claiming to be unmeasured"
            )

    @classmethod
    def scored_at(cls, score: float, reason: str) -> JudgeVerdict:
        return cls(score=score, reason=reason, scored=True)

    @classmethod
    def unscored(cls, reason: str) -> JudgeVerdict:
        return cls(score=None, reason=reason, scored=False)


class QualityJudge(Protocol):
    """Scores one output on a 1-5 scale, or declines to score it."""

    def score(self, output: str | None, context: dict[str, Any] | None = None) -> JudgeVerdict: ...


def clamp(score: float) -> float:
    """Constrain a score to the 1-5 band."""
    return max(MIN_SCORE, min(MAX_SCORE, score))
