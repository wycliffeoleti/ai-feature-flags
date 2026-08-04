"""Repository contract for flag configuration and its audit trail.

Two rules are enforced by the interface rather than by convention, because both
are easy to erode once there is a deadline:

* **No unattributed mutation.** Every mutating method takes ``actor`` and
  ``reason`` as required keyword arguments. There is no overload without them, so
  an unattributed change cannot be written by accident.
* **One monotonic version.** Every mutation returns the new snapshot version.
  A single counter across all flags is what lets the SDK reject out-of-order
  snapshot delivery with a comparison rather than a merge.

The serialisation helpers live here too, so the in-memory and PostgreSQL stores
encode the domain identically and their shared contract test cannot pass for one
and fail for the other on a formatting difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from aiflags.core.models import (
    Comparison,
    FlagDefinition,
    FlagSnapshot,
    FlagStatus,
    QualityGate,
    QualityPolicy,
    QualitySignal,
    RolloutPlan,
    Stage,
    Statistic,
    TargetingKind,
    TargetingRule,
    Variant,
    VariantKind,
)


class RepositoryError(Exception):
    """Base class for repository failures."""


class FlagNotFound(RepositoryError):
    def __init__(self, key: str) -> None:
        super().__init__(f"no flag named {key!r}")
        self.key = key


class FlagAlreadyExists(RepositoryError):
    def __init__(self, key: str) -> None:
        super().__init__(f"a flag named {key!r} already exists")
        self.key = key


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One recorded change, with who made it and why.

    ``snapshot_version`` ties the change to the exact configuration the data
    plane was serving afterwards, so a rollback can be reconstructed later.
    """

    flag_key: str
    action: str
    actor: str
    reason: str
    at: datetime
    snapshot_version: int
    detail: dict[str, Any] = field(default_factory=dict)


class FlagRepository(Protocol):
    """Durable store for flag definitions and the audit trail."""

    def create_flag(self, flag: FlagDefinition, *, actor: str, reason: str) -> int: ...

    def get_flag(self, key: str) -> FlagDefinition | None: ...

    def list_flags(self) -> list[FlagDefinition]: ...

    def replace_flag(self, flag: FlagDefinition, *, actor: str, reason: str) -> int: ...

    def set_rollout_percentage(
        self, key: str, percentage: float, *, actor: str, reason: str
    ) -> int: ...

    def set_status(
        self, key: str, status: FlagStatus, *, actor: str, reason: str
    ) -> int: ...

    def rollback(self, key: str, *, actor: str, reason: str) -> int: ...

    def snapshot(self) -> FlagSnapshot: ...

    def audit_events(self, flag_key: str | None = None) -> list[AuditEvent]: ...


def require_attribution(actor: str, reason: str) -> None:
    """Reject a mutation that cannot be explained afterwards."""
    if not actor or not actor.strip():
        raise ValueError("every mutation requires a non-blank actor")
    if not reason or not reason.strip():
        raise ValueError("every mutation requires a non-blank reason")


# --------------------------------------------------------------------------- #
# Serialisation
#
# Shared by both stores so their contract test cannot pass for one and fail for
# the other over an encoding difference.
# --------------------------------------------------------------------------- #


def flag_to_dict(flag: FlagDefinition) -> dict[str, Any]:
    """Encode a flag as plain JSON-compatible data."""
    return {
        "key": flag.key,
        "salt": flag.salt,
        "status": flag.status.value,
        "rollout_percentage": flag.rollout_percentage,
        "baseline": _variant_to_dict(flag.baseline),
        "experimental": _variant_to_dict(flag.experimental),
        "targeting": [_rule_to_dict(rule) for rule in flag.targeting],
        "quality_policy": {
            "minimum_samples": flag.quality_policy.minimum_samples,
            "confidence": flag.quality_policy.confidence,
            "gates": [_gate_to_dict(gate) for gate in flag.quality_policy.gates],
        },
        "rollout_plan": {
            "cooldown_seconds": flag.rollout_plan.cooldown_seconds,
            "stages": [
                {"percentage": s.percentage, "dwell_seconds": s.dwell_seconds}
                for s in flag.rollout_plan.stages
            ],
        },
    }


def flag_from_dict(payload: dict[str, Any]) -> FlagDefinition:
    """Decode a flag from :func:`flag_to_dict` output."""
    policy = payload["quality_policy"]
    plan = payload["rollout_plan"]
    return FlagDefinition(
        key=payload["key"],
        salt=payload.get("salt", ""),
        status=FlagStatus(payload["status"]),
        rollout_percentage=payload["rollout_percentage"],
        baseline=_variant_from_dict(payload["baseline"]),
        experimental=_variant_from_dict(payload["experimental"]),
        targeting=tuple(_rule_from_dict(rule) for rule in payload.get("targeting", [])),
        quality_policy=QualityPolicy(
            gates=tuple(_gate_from_dict(gate) for gate in policy["gates"]),
            minimum_samples=policy["minimum_samples"],
            confidence=policy["confidence"],
        ),
        rollout_plan=RolloutPlan(
            stages=tuple(
                Stage(percentage=s["percentage"], dwell_seconds=s["dwell_seconds"])
                for s in plan["stages"]
            ),
            cooldown_seconds=plan["cooldown_seconds"],
        ),
    )


def _variant_to_dict(variant: Variant) -> dict[str, Any]:
    return {"key": variant.key, "kind": variant.kind.value, "config": variant.config}


def _variant_from_dict(payload: dict[str, Any]) -> Variant:
    return Variant(
        key=payload["key"],
        kind=VariantKind(payload["kind"]),
        config=payload.get("config", {}),
    )


def _rule_to_dict(rule: TargetingRule) -> dict[str, Any]:
    return {
        "kind": rule.kind.value,
        # Sorted so the encoding is stable: a frozenset has no order, and an
        # unstable encoding turns every republish into a spurious diff.
        "values": sorted(rule.values),
        "variant_kind": rule.variant_kind.value,
        "attribute": rule.attribute,
    }


def _rule_from_dict(payload: dict[str, Any]) -> TargetingRule:
    return TargetingRule(
        kind=TargetingKind(payload["kind"]),
        values=frozenset(payload["values"]),
        variant_kind=VariantKind(payload["variant_kind"]),
        attribute=payload.get("attribute"),
    )


def _gate_to_dict(gate: QualityGate) -> dict[str, Any]:
    return {
        "signal": gate.signal.value,
        "statistic": gate.statistic.value,
        "comparison": gate.comparison.value,
        "threshold": gate.threshold,
        "sustained_evaluations": gate.sustained_evaluations,
    }


def _gate_from_dict(payload: dict[str, Any]) -> QualityGate:
    return QualityGate(
        signal=QualitySignal(payload["signal"]),
        statistic=Statistic(payload["statistic"]),
        comparison=Comparison(payload["comparison"]),
        threshold=payload["threshold"],
        sustained_evaluations=payload["sustained_evaluations"],
    )
