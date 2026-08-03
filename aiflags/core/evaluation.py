"""The SDK hot path, as one pure function.

Applications call this (via :mod:`aiflags.sdk`) on every request, so it does no
I/O: it reads an already-fetched :class:`~aiflags.core.models.FlagSnapshot` and
returns a decision. Fetching, caching, and refreshing the snapshot are the SDK's
job; deciding is this module's.

The ordering in :func:`evaluate` is the safety property. Every branch that
represents uncertainty — no snapshot, unknown flag, stale data — resolves to
baseline, and the checks that force baseline run *before* anything that could
serve the experimental variant. There is no input for which uncertainty produces
a ramp-up.
"""

from __future__ import annotations

from datetime import datetime

from aiflags.core.bucketing import bucket
from aiflags.core.models import (
    EvaluationContext,
    EvaluationReason,
    EvaluationResult,
    FlagDefinition,
    FlagSnapshot,
    FlagStatus,
    TargetingKind,
    Variant,
    VariantKind,
)
from aiflags.core.targeting import match_targeting

UNKNOWN_BASELINE = Variant(key="__unknown_baseline__", kind=VariantKind.BASELINE)
"""Served when there is no flag definition to read a real baseline from.

An application that needs its own fallback config passes ``default_variant`` to
the SDK, which substitutes it for this sentinel.
"""

_TARGETING_REASONS: dict[TargetingKind, EvaluationReason] = {
    TargetingKind.BLOCKLIST: EvaluationReason.BLOCKLIST,
    TargetingKind.ALLOWLIST: EvaluationReason.ALLOWLIST,
    TargetingKind.SEGMENT: EvaluationReason.SEGMENT,
    TargetingKind.GEO: EvaluationReason.GEO,
    TargetingKind.METADATA: EvaluationReason.METADATA,
}

# Statuses that force baseline regardless of percentage or targeting. ROLLED_BACK
# is here because an automatic rollback must not be defeated by a leftover
# percentage on the record or by an operator's earlier allowlist entry.
_FORCED_BASELINE_STATUS: dict[FlagStatus, EvaluationReason] = {
    FlagStatus.ROLLED_BACK: EvaluationReason.ROLLED_BACK,
    FlagStatus.OFF: EvaluationReason.FLAG_OFF,
}


def evaluate(
    snapshot: FlagSnapshot | None,
    flag_key: str,
    context: EvaluationContext,
    evaluation_id: str,
    now: datetime | None = None,
    max_staleness_seconds: float | None = None,
) -> EvaluationResult:
    """Decide which variant ``context`` should be served for ``flag_key``.

    ``evaluation_id`` is supplied by the caller rather than generated here so
    this function stays pure and its output fully determined by its inputs; the
    SDK mints one per call to correlate the later quality outcome.

    ``max_staleness_seconds`` of ``None`` disables the staleness check entirely,
    which is what tests and offline replay want.
    """
    if snapshot is None:
        return _result(
            flag_key, UNKNOWN_BASELINE, EvaluationReason.NO_SNAPSHOT, 0, evaluation_id
        )

    flag = snapshot.flags.get(flag_key)

    if _is_stale(snapshot, now, max_staleness_seconds):
        # A stale snapshot may still name the flag's real baseline, which is a
        # better fallback than the sentinel — but it is never trusted to ramp.
        variant = flag.baseline if flag is not None else UNKNOWN_BASELINE
        return _result(
            flag_key,
            variant,
            EvaluationReason.SNAPSHOT_STALE,
            snapshot.version,
            evaluation_id,
        )

    if flag is None:
        return _result(
            flag_key,
            UNKNOWN_BASELINE,
            EvaluationReason.FLAG_UNKNOWN,
            snapshot.version,
            evaluation_id,
        )

    forced = _FORCED_BASELINE_STATUS.get(flag.status)
    if forced is not None:
        return _result(
            flag_key, flag.baseline, forced, snapshot.version, evaluation_id
        )

    if flag.status is FlagStatus.SHADOW:
        # Users see baseline; the application additionally runs the shadow
        # variant and reports it separately. Shadow scores never advance a
        # rollout, so this is safe to apply to all traffic.
        return _result(
            flag_key,
            flag.baseline,
            EvaluationReason.SHADOW,
            snapshot.version,
            evaluation_id,
            shadow_variant=flag.experimental,
        )

    matched = match_targeting(flag.targeting, context)
    if matched is not None:
        return _result(
            flag_key,
            _variant_for(flag, matched.variant_kind),
            _TARGETING_REASONS[matched.rule.kind],
            snapshot.version,
            evaluation_id,
        )

    if flag.status is FlagStatus.FULLY_ON:
        return _result(
            flag_key,
            flag.experimental,
            EvaluationReason.FULLY_ON,
            snapshot.version,
            evaluation_id,
        )

    # ROLLING_OUT and PAUSED both serve the recorded percentage. Pausing halts
    # the controller's advance; it does not change what users currently see.
    in_rollout = (
        bucket(context.subject_key, flag.key, flag.salt) < flag.rollout_percentage / 100.0
    )
    return _result(
        flag_key,
        flag.experimental if in_rollout else flag.baseline,
        EvaluationReason.PERCENTAGE_IN if in_rollout else EvaluationReason.PERCENTAGE_OUT,
        snapshot.version,
        evaluation_id,
    )


def _is_stale(
    snapshot: FlagSnapshot,
    now: datetime | None,
    max_staleness_seconds: float | None,
) -> bool:
    if max_staleness_seconds is None or now is None:
        return False
    return (now - snapshot.published_at).total_seconds() > max_staleness_seconds


def _variant_for(flag: FlagDefinition, kind: VariantKind) -> Variant:
    return flag.experimental if kind is VariantKind.EXPERIMENTAL else flag.baseline


def _result(
    flag_key: str,
    variant: Variant,
    reason: EvaluationReason,
    snapshot_version: int,
    evaluation_id: str,
    shadow_variant: Variant | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        flag_key=flag_key,
        variant=variant,
        reason=reason,
        snapshot_version=snapshot_version,
        evaluation_id=evaluation_id,
        shadow_variant=shadow_variant,
    )
