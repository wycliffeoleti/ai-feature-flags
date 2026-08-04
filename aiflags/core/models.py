"""Domain model for AI feature flags.

The distinction from a traditional feature flag is in :class:`QualityPolicy` and
:class:`RolloutPlan`: a flag does not merely say *whether* the experimental
variant is on, it says what "good enough" means for that variant and what the
system should do when the variant stops meeting it.

Everything here is inert, immutable data built on the standard library only.
Decisions live in :mod:`aiflags.core.decision`; evaluation lives in
:mod:`aiflags.core.evaluation`.

**Why no Pydantic here.** This module is imported by the SDK, which applications
embed in their own request path. Keeping the core on stdlib dataclasses means
adopting the SDK cannot conflict with an application's own Pydantic version, and
the pure core stays runnable with no install step at all. Pydantic still does the
work it is good at — parsing untrusted JSON — but at the API boundary in
:mod:`aiflags.api`, where the input is actually untrusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #


class VariantKind(StrEnum):
    """Which side of the rollout a variant sits on."""

    BASELINE = "baseline"
    EXPERIMENTAL = "experimental"


@dataclass(frozen=True, slots=True)
class Variant:
    """One servable configuration.

    ``config`` is opaque to this system — a prompt version, a model name, a
    temperature, or the sentinel an application reads to fall back to a non-AI
    code path. The flag service never interprets it.
    """

    key: str
    kind: VariantKind
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("variant key must not be empty")


# --------------------------------------------------------------------------- #
# Targeting
# --------------------------------------------------------------------------- #


class TargetingKind(StrEnum):
    """Targeting rule types, in the order they are evaluated.

    Order is deliberate and load-bearing: a blocklist must beat an allowlist, and
    every explicit rule must beat the percentage ramp. See
    :data:`TARGETING_PRECEDENCE`.
    """

    BLOCKLIST = "blocklist"
    ALLOWLIST = "allowlist"
    SEGMENT = "segment"
    GEO = "geo"
    METADATA = "metadata"


TARGETING_PRECEDENCE: tuple[TargetingKind, ...] = (
    TargetingKind.BLOCKLIST,
    TargetingKind.ALLOWLIST,
    TargetingKind.SEGMENT,
    TargetingKind.GEO,
    TargetingKind.METADATA,
)

_ATTRIBUTE_KINDS = frozenset(
    {TargetingKind.SEGMENT, TargetingKind.GEO, TargetingKind.METADATA}
)


@dataclass(frozen=True, slots=True)
class TargetingRule:
    """Force a variant for subjects matching an attribute.

    ``attribute`` names the key read from :attr:`EvaluationContext.attributes`.
    Blocklist and allowlist rules always match on the subject key itself, so they
    leave it unset.
    """

    kind: TargetingKind
    values: frozenset[str]
    variant_kind: VariantKind
    attribute: str | None = None

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError(f"{self.kind} targeting rule requires at least one value")
        if self.kind in _ATTRIBUTE_KINDS and not self.attribute:
            raise ValueError(f"{self.kind} targeting rule requires an attribute")
        object.__setattr__(self, "values", frozenset(self.values))


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Everything known about the request being evaluated.

    ``subject_key`` is whatever the application uses for stable identity — a user
    ID, an account ID, a session ID. The evaluation path hashes it and never
    stores it raw.
    """

    subject_key: str
    attributes: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Quality policy
# --------------------------------------------------------------------------- #


class QualitySignal(StrEnum):
    """Measurable dimensions of "is this AI feature working"."""

    JUDGE_SCORE = "judge_score"
    FEEDBACK = "feedback"
    LATENCY_MS = "latency_ms"
    ERROR_RATE = "error_rate"
    UNSCORED_RATE = "unscored_rate"


class Comparison(StrEnum):
    """Which direction of a signal counts as a breach."""

    BELOW = "below"
    ABOVE = "above"


class Statistic(StrEnum):
    """Which summary of the rolling window the threshold applies to."""

    MEAN = "mean"
    P10 = "p10"
    P95 = "p95"
    RATE = "rate"


@dataclass(frozen=True, slots=True)
class QualityGate:
    """One threshold the experimental variant must keep satisfying.

    ``sustained_evaluations`` is what separates a real regression from noise: the
    gate only breaches once the statistic has stayed on the wrong side of the
    threshold for that many consecutive scored evaluations.
    """

    signal: QualitySignal
    statistic: Statistic
    comparison: Comparison
    threshold: float
    sustained_evaluations: int = 50

    def __post_init__(self) -> None:
        if self.sustained_evaluations < 1:
            raise ValueError("sustained_evaluations must be at least 1")


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """The full definition of "good enough" for one flag.

    ``minimum_samples`` guards the canary comparison — below it the controller
    holds rather than advancing on thin data.
    """

    gates: tuple[QualityGate, ...] = ()
    minimum_samples: int = 30
    confidence: float = 0.95

    def __post_init__(self) -> None:
        if not self.gates:
            raise ValueError(
                "an AI feature flag needs at least one quality gate; a flag with "
                "no definition of quality cannot be safely rolled out"
            )
        if self.minimum_samples < 2:
            raise ValueError("minimum_samples must be at least 2")
        if not 0.5 < self.confidence < 1.0:
            raise ValueError("confidence must lie strictly between 0.5 and 1.0")


# --------------------------------------------------------------------------- #
# Rollout plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Stage:
    """One step of a staged rollout: hold this percentage for this long."""

    percentage: float
    dwell_seconds: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.percentage <= 100.0:
            raise ValueError("stage percentage must lie in [0, 100]")
        if self.dwell_seconds <= 0.0:
            raise ValueError("stage dwell_seconds must be positive")


@dataclass(frozen=True, slots=True)
class RolloutPlan:
    """An ordered ramp. Percentages must be non-decreasing."""

    stages: tuple[Stage, ...]
    cooldown_seconds: float = 3600.0

    def __post_init__(self) -> None:
        if not self.stages:
            raise ValueError("rollout plan requires at least one stage")
        percentages = [stage.percentage for stage in self.stages]
        if percentages != sorted(percentages):
            raise ValueError(
                f"rollout stages must be non-decreasing, got {percentages}; a "
                "decreasing ramp would move subjects back to baseline and "
                "corrupt the quality comparison"
            )
        if self.cooldown_seconds < 0.0:
            raise ValueError("cooldown_seconds must not be negative")


DEFAULT_ROLLOUT_PLAN = RolloutPlan(
    stages=(
        Stage(percentage=1.0, dwell_seconds=2 * 3600),
        Stage(percentage=5.0, dwell_seconds=6 * 3600),
        Stage(percentage=25.0, dwell_seconds=24 * 3600),
        Stage(percentage=50.0, dwell_seconds=24 * 3600),
        Stage(percentage=100.0, dwell_seconds=24 * 3600),
    )
)
"""The ramp the guide specifies: 1%/2h, 5%/6h, 25%/24h, 50%/24h, 100%."""


# --------------------------------------------------------------------------- #
# Flags and snapshots
# --------------------------------------------------------------------------- #


class FlagStatus(StrEnum):
    """Lifecycle of a flag.

    ``ROLLED_BACK`` is terminal until an operator explicitly resumes: an
    automatic rollback must not be undone by another automatic action.
    """

    OFF = "off"
    SHADOW = "shadow"
    ROLLING_OUT = "rolling_out"
    PAUSED = "paused"
    FULLY_ON = "fully_on"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class FlagDefinition:
    """A single AI feature flag as published to the data plane."""

    key: str
    baseline: Variant
    experimental: Variant
    quality_policy: QualityPolicy
    salt: str = ""
    status: FlagStatus = FlagStatus.OFF
    rollout_percentage: float = 0.0
    targeting: tuple[TargetingRule, ...] = ()
    rollout_plan: RolloutPlan = DEFAULT_ROLLOUT_PLAN

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("flag key must not be empty")
        if self.baseline.kind is not VariantKind.BASELINE:
            raise ValueError("baseline variant must have kind=baseline")
        if self.experimental.kind is not VariantKind.EXPERIMENTAL:
            raise ValueError("experimental variant must have kind=experimental")
        if not 0.0 <= self.rollout_percentage <= 100.0:
            raise ValueError("rollout_percentage must lie in [0, 100]")

    def with_percentage(self, percentage: float) -> FlagDefinition:
        """Return a copy at a new rollout percentage."""
        return replace(self, rollout_percentage=percentage)

    def with_status(self, status: FlagStatus) -> FlagDefinition:
        """Return a copy in a new lifecycle state."""
        return replace(self, status=status)


@dataclass(frozen=True, slots=True)
class FlagSnapshot:
    """The immutable, versioned view of all flags the SDK evaluates against.

    The SDK holds one of these in memory and evaluates entirely locally. The
    version is monotonic, so a client can distinguish staleness from a lost
    update.
    """

    version: int
    published_at: datetime
    flags: dict[str, FlagDefinition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version < 0:
            raise ValueError("snapshot version must not be negative")


# --------------------------------------------------------------------------- #
# Evaluation output
# --------------------------------------------------------------------------- #


class EvaluationReason(StrEnum):
    """Why a subject received the variant it did.

    Recorded on every evaluation so a rollback can be explained after the fact
    without replaying the decision.
    """

    FLAG_UNKNOWN = "flag_unknown"
    FLAG_OFF = "flag_off"
    ROLLED_BACK = "rolled_back"
    SNAPSHOT_STALE = "snapshot_stale"
    NO_SNAPSHOT = "no_snapshot"
    BLOCKLIST = "blocklist"
    ALLOWLIST = "allowlist"
    SEGMENT = "segment"
    GEO = "geo"
    METADATA = "metadata"
    PERCENTAGE_IN = "percentage_in"
    PERCENTAGE_OUT = "percentage_out"
    FULLY_ON = "fully_on"
    SHADOW = "shadow"


DEGRADED_REASONS: frozenset[EvaluationReason] = frozenset(
    {
        EvaluationReason.FLAG_UNKNOWN,
        EvaluationReason.SNAPSHOT_STALE,
        EvaluationReason.NO_SNAPSHOT,
    }
)
"""Reasons that mean the SDK fell back rather than made a real decision.

These are counted separately: a rollout that looks quiet because every client is
serving a stale snapshot is not a rollout that is going well.
"""


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """What the SDK returns to the application.

    The application serves :attr:`variant` to the user. If
    :attr:`shadow_variant` is present, it additionally runs that variant and
    reports the output via ``record_shadow_outcome``. Shadow output is scored but
    can never advance a rollout.
    """

    flag_key: str
    variant: Variant
    reason: EvaluationReason
    snapshot_version: int
    evaluation_id: str
    shadow_variant: Variant | None = None

    @property
    def is_experimental(self) -> bool:
        return self.variant.kind is VariantKind.EXPERIMENTAL

    @property
    def is_degraded(self) -> bool:
        return self.reason in DEGRADED_REASONS


class CanaryVerdict(StrEnum):
    """Result of comparing the experimental variant against baseline.

    Defined here rather than in :mod:`aiflags.core.canary` so that
    :mod:`aiflags.core.decision` can consume a verdict without importing SciPy.
    The decision logic must stay pure stdlib and instantly testable; only the
    statistics that produce the verdict need the heavyweight dependency.
    """

    NO_WORSE = "no_worse"
    WORSE = "worse"
    INCONCLUSIVE = "inconclusive"
