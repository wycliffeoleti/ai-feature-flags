"""Statistical comparison of the experimental variant against baseline.

**Why non-inferiority rather than plain significance.** The obvious gate — run a
t-test, advance unless the experiment is significantly worse — has a perverse
property: a small, noisy sample is never significant, so the rollout ramps
fastest exactly when it knows least. The test's failure to detect a regression
gets read as evidence there isn't one.

This module inverts that. It asks whether a regression larger than ``margin``
can be *ruled out* at the configured confidence, and advances only when the
answer is yes. Three outcomes follow naturally:

* :attr:`~aiflags.core.models.CanaryVerdict.NO_WORSE` — the interval excludes a
  meaningful regression. Safe to advance.
* :attr:`~aiflags.core.models.CanaryVerdict.WORSE` — the interval excludes
  non-inferiority. The variant really is worse.
* :attr:`~aiflags.core.models.CanaryVerdict.INCONCLUSIVE` — neither. Not enough
  evidence either way, which the controller treats as hold.

This is the only module in :mod:`aiflags.core` that needs SciPy, which is why
:class:`~aiflags.core.models.CanaryVerdict` lives in ``models`` — the decision
logic consumes a verdict without taking the dependency.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import stats

from aiflags.core.models import CanaryVerdict


def _plain(value: float | None) -> float | None:
    """Coerce a possible ``numpy`` scalar to a JSON-encodable float."""
    return None if value is None else float(value)


DEFAULT_MINIMUM_SAMPLES = 30
NORMALITY_ALPHA = 0.05
"""Shapiro-Wilk below this rejects normality and selects the rank-based test."""

SHAPIRO_MAX_SAMPLES = 5000
"""Shapiro-Wilk is unreliable past this; large samples use Welch by the CLT."""

DEFAULT_MARGIN_EFFECT_SIZE = 0.2
"""Cohen's convention for a small effect, used to derive a margin when none is given.

An absolute default cannot work across signals — 0.2 is a meaningful drop on a
1-5 judge score and irrelevant on a latency in milliseconds — so the margin
defaults to a fraction of the baseline's own spread.
"""


@dataclass(frozen=True, slots=True)
class CanaryResult:
    """The comparison outcome, with everything needed to justify it later."""

    verdict: CanaryVerdict
    test: str
    n_experimental: int
    n_baseline: int
    effect: float | None = None
    p_value: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    margin: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Flat, JSON-friendly form for the audit log.

        Floats are coerced explicitly because SciPy returns ``numpy.float64``,
        which the standard library's JSON encoder refuses — and the first place
        that would surface is while writing the record explaining a rollback.
        """
        return {
            "verdict": self.verdict.value,
            "test": self.test,
            "n_experimental": self.n_experimental,
            "n_baseline": self.n_baseline,
            "effect": _plain(self.effect),
            "p_value": _plain(self.p_value),
            "ci_low": _plain(self.ci_low),
            "ci_high": _plain(self.ci_high),
            "margin": _plain(self.margin),
        }


def compare(
    experimental: list[float],
    baseline: list[float],
    confidence: float = 0.95,
    minimum_samples: int = DEFAULT_MINIMUM_SAMPLES,
    margin: float | None = None,
    higher_is_better: bool = True,
) -> CanaryResult:
    """Compare two samples and return a non-inferiority verdict.

    ``higher_is_better=False`` flips the orientation for signals like latency and
    error rate, where a smaller number is the good outcome.
    """
    n_exp, n_base = len(experimental), len(baseline)
    if n_exp < minimum_samples or n_base < minimum_samples:
        return CanaryResult(
            verdict=CanaryVerdict.INCONCLUSIVE,
            test="insufficient",
            n_experimental=n_exp,
            n_baseline=n_base,
        )

    # Orient both samples so that "larger is better" always holds internally.
    if not higher_is_better:
        experimental = [-value for value in experimental]
        baseline = [-value for value in baseline]

    if margin is None:
        margin = _default_margin(baseline)

    if _is_normal(experimental) and _is_normal(baseline):
        return _welch(experimental, baseline, confidence, margin, n_exp, n_base)
    return _mann_whitney(experimental, baseline, confidence, margin, n_exp, n_base)


def _default_margin(baseline: list[float]) -> float:
    spread = statistics.stdev(baseline) if len(baseline) > 1 else 0.0
    return DEFAULT_MARGIN_EFFECT_SIZE * spread


def _is_normal(values: list[float]) -> bool:
    """Shapiro-Wilk, with the degenerate and large-sample cases handled."""
    if len(values) > SHAPIRO_MAX_SAMPLES:
        # Past this size the test flags trivial departures, and the central limit
        # theorem makes Welch robust anyway.
        return True
    if len({round(v, 12) for v in values}) == 1:
        # Zero variance: Shapiro raises. Treat as normal so the Welch path runs
        # and produces a degenerate but correct interval.
        return True
    try:
        return stats.shapiro(values).pvalue >= NORMALITY_ALPHA
    except ValueError:
        return False


def _welch(
    experimental: list[float],
    baseline: list[float],
    confidence: float,
    margin: float,
    n_exp: int,
    n_base: int,
) -> CanaryResult:
    """Welch's t-test plus a confidence interval on the difference of means."""
    mean_exp = statistics.fmean(experimental)
    mean_base = statistics.fmean(baseline)
    effect = mean_exp - mean_base

    var_exp = statistics.variance(experimental) if len(experimental) > 1 else 0.0
    var_base = statistics.variance(baseline) if len(baseline) > 1 else 0.0
    standard_error = math.sqrt(var_exp / len(experimental) + var_base / len(baseline))

    if standard_error == 0.0:
        # Both samples are constant. The comparison is exact, so the interval
        # collapses onto the observed difference.
        return _verdict_from_interval(
            effect, effect, effect, margin, None, "welch", n_exp, n_base
        )

    result = stats.ttest_ind(experimental, baseline, equal_var=False)
    degrees_of_freedom = _welch_degrees_of_freedom(
        var_exp, len(experimental), var_base, len(baseline)
    )
    critical = stats.t.ppf(1.0 - (1.0 - confidence) / 2.0, degrees_of_freedom)
    half_width = critical * standard_error

    return _verdict_from_interval(
        effect,
        effect - half_width,
        effect + half_width,
        margin,
        float(result.pvalue),
        "welch",
        n_exp,
        n_base,
    )


def _welch_degrees_of_freedom(
    var_a: float, n_a: int, var_b: float, n_b: int
) -> float:
    """Welch-Satterthwaite approximation."""
    term_a = var_a / n_a
    term_b = var_b / n_b
    denominator = term_a**2 / (n_a - 1) + term_b**2 / (n_b - 1)
    if denominator == 0.0:
        return float(n_a + n_b - 2)
    return (term_a + term_b) ** 2 / denominator


def _mann_whitney(
    experimental: list[float],
    baseline: list[float],
    confidence: float,
    margin: float,
    n_exp: int,
    n_base: int,
) -> CanaryResult:
    """Rank-based fallback for non-normal data, using a median-difference interval.

    A distribution-free interval for the shift is obtained by bootstrapping the
    median difference, which keeps the same non-inferiority logic as the Welch
    path rather than falling back to a bare significance check.
    """
    effect = statistics.median(experimental) - statistics.median(baseline)
    p_value = float(stats.mannwhitneyu(experimental, baseline).pvalue)
    ci_low, ci_high = _bootstrap_median_difference(experimental, baseline, confidence)
    return _verdict_from_interval(
        effect, ci_low, ci_high, margin, p_value, "mann_whitney", n_exp, n_base
    )


def _bootstrap_median_difference(
    experimental: list[float], baseline: list[float], confidence: float
) -> tuple[float, float]:
    """Percentile bootstrap interval for the difference in medians.

    Seeded so a rollout decision is reproducible from the audit log: rerunning
    the comparison on the same data must yield the same verdict.
    """
    result = stats.bootstrap(
        (experimental, baseline),
        lambda a, b, axis=-1: np.median(a, axis=axis) - np.median(b, axis=axis),
        confidence_level=confidence,
        n_resamples=999,
        method="percentile",
        random_state=20260804,
        vectorized=True,
    )
    return (
        float(result.confidence_interval.low),
        float(result.confidence_interval.high),
    )


def _verdict_from_interval(
    effect: float,
    ci_low: float,
    ci_high: float,
    margin: float,
    p_value: float | None,
    test: str,
    n_exp: int,
    n_base: int,
) -> CanaryResult:
    """Apply the non-inferiority rule to a confidence interval on the effect.

    The interval is for ``experimental - baseline`` under "higher is better".
    Non-inferiority holds when the whole interval sits above ``-margin``;
    inferiority is established when the whole interval sits below it.
    """
    if ci_low > -margin or (margin == 0.0 and ci_low >= 0.0):
        verdict = CanaryVerdict.NO_WORSE
    elif ci_high < -margin or (margin == 0.0 and ci_high < 0.0):
        verdict = CanaryVerdict.WORSE
    else:
        verdict = CanaryVerdict.INCONCLUSIVE

    return CanaryResult(
        verdict=verdict,
        test=test,
        n_experimental=n_exp,
        n_baseline=n_base,
        effect=effect,
        p_value=p_value,
        ci_low=ci_low,
        ci_high=ci_high,
        margin=margin,
    )
