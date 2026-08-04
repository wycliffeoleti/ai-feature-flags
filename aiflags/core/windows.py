"""Rolling summaries of observed quality.

This is the layer that turns a stream of individually noisy scores into
something a rollout decision can be made from. Two choices here are
interpretations of the guide rather than mechanical translations, and both are
deliberate:

**Trailing windows, not per-point recomputation.** "P10 below 3.0 for more than
50 consecutive evaluations" is read as the statistic computed over the trailing
50 evaluations. Recomputing the statistic at each of 50 successive points would
be O(n^2) on every controller tick and answers a subtly different question.

**Unscored samples are neither dropped nor zeroed.** When the judge fails or
times out, the sample carries no quality information. Averaging it in as a zero
invents a regression; discarding it silently lets a rollout advance while the
system is blind. Instead it is excluded from the quality statistics and surfaced
as :attr:`WindowStats.unscored_rate`, which the rollout policy can gate on
directly.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MIN_SAMPLES_FOR_TREND = 10
"""Below this, half-versus-half comparison is noise rather than a trend."""

TREND_RELATIVE_EPSILON = 0.05
"""A half-over-half shift under 5% of the window mean counts as stable."""


class Trend(StrEnum):
    """Direction of travel across a window."""

    IMPROVING = "improving"
    STABLE = "stable"
    DEGRADING = "degrading"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Sample:
    """One observation of a quality signal.

    ``scored=False`` marks an observation the judge could not score. It occupies
    a slot in the window — so it counts toward window size and the unscored rate
    — but contributes no value to the quality statistics.
    """

    value: float
    at: datetime
    scored: bool = True


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Summary of one window. Quality statistics are ``None`` when nothing scored."""

    count: int
    unscored_rate: float
    trend: Trend
    mean: float | None = None
    stdev: float | None = None
    p10: float | None = None
    p95: float | None = None

    @property
    def is_blind(self) -> bool:
        """No scored samples at all — the opposite of "no problems observed"."""
        return self.count == 0


EMPTY_WINDOW = WindowStats(count=0, unscored_rate=0.0, trend=Trend.UNKNOWN)


def summarize(
    samples: list[Sample],
    now: datetime | None = None,
    within_seconds: float | None = None,
    last_n: int | None = None,
) -> WindowStats:
    """Summarize ``samples``, optionally restricted by age and count.

    ``within_seconds`` selects by age relative to ``now`` and requires it.
    ``last_n`` then takes the most recent remaining samples, so the two compose
    as "the last N evaluations within the last hour".
    """
    if within_seconds is not None:
        if now is None:
            raise ValueError("a time-bounded window requires `now`")
        cutoff = within_seconds
        samples = [s for s in samples if (now - s.at).total_seconds() <= cutoff]

    if last_n is not None:
        samples = samples[-last_n:]

    if not samples:
        return EMPTY_WINDOW

    scored = [s.value for s in samples if s.scored]
    unscored_rate = (len(samples) - len(scored)) / len(samples)

    if not scored:
        return WindowStats(
            count=0, unscored_rate=unscored_rate, trend=Trend.UNKNOWN
        )

    return WindowStats(
        count=len(scored),
        unscored_rate=unscored_rate,
        trend=_trend(scored),
        mean=statistics.fmean(scored),
        stdev=statistics.stdev(scored) if len(scored) > 1 else 0.0,
        p10=percentile(scored, 0.10),
        p95=percentile(scored, 0.95),
    )


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile, defined for a single value.

    ``statistics.quantiles`` raises below two data points, which is exactly the
    situation early in a 1% rollout stage, so the interpolation is done here.
    """
    if not values:
        raise ValueError("percentile of an empty sequence is undefined")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _trend(values: list[float]) -> Trend:
    """Compare the older half of the window against the newer half."""
    if len(values) < MIN_SAMPLES_FOR_TREND:
        return Trend.UNKNOWN
    midpoint = len(values) // 2
    older = statistics.fmean(values[:midpoint])
    newer = statistics.fmean(values[midpoint:])
    scale = abs(statistics.fmean(values)) or 1.0
    shift = (newer - older) / scale
    if shift > TREND_RELATIVE_EPSILON:
        return Trend.IMPROVING
    if shift < -TREND_RELATIVE_EPSILON:
        return Trend.DEGRADING
    return Trend.STABLE
