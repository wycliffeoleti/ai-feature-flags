"""Request and response models for the management API.

This is the one place Pydantic belongs: the input is genuinely untrusted, and
declarative validation with generated OpenAPI is exactly what it is good at. The
pure core stays on stdlib dataclasses so applications embedding the SDK do not
inherit a Pydantic version constraint — see ``docs/DECISIONS.md`` D1.

The payload shape mirrors :func:`aiflags.store.base.flag_to_dict` exactly, so
converting between the two is a ``model_dump`` and nothing more. Any drift shows
up immediately as a decode failure rather than as a silently dropped field.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from aiflags.core.models import (
    DEFAULT_ROLLOUT_PLAN,
    Comparison,
    FlagStatus,
    QualitySignal,
    Statistic,
    TargetingKind,
    VariantKind,
)


class Attribution(BaseModel):
    """Who is making this change and why.

    Required on every mutating endpoint. The repository rejects blank values too,
    but failing here turns an unattributed change into a 422 with a useful
    message instead of a 500.
    """

    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class VariantPayload(BaseModel):
    key: str = Field(min_length=1)
    kind: VariantKind
    config: dict[str, Any] = Field(default_factory=dict)


class TargetingRulePayload(BaseModel):
    kind: TargetingKind
    values: list[str] = Field(min_length=1)
    variant_kind: VariantKind
    attribute: str | None = None


class QualityGatePayload(BaseModel):
    signal: QualitySignal
    statistic: Statistic
    comparison: Comparison
    threshold: float
    sustained_evaluations: int = Field(default=50, ge=1)


class QualityPolicyPayload(BaseModel):
    gates: list[QualityGatePayload] = Field(min_length=1)
    minimum_samples: int = Field(default=30, ge=2)
    confidence: float = Field(default=0.95, gt=0.5, lt=1.0)


class StagePayload(BaseModel):
    percentage: float = Field(ge=0.0, le=100.0)
    dwell_seconds: float = Field(gt=0.0)


class RolloutPlanPayload(BaseModel):
    stages: list[StagePayload] = Field(min_length=1)
    cooldown_seconds: float = Field(default=3600.0, ge=0.0)


class FlagPayload(BaseModel):
    """A full flag definition."""

    key: str = Field(min_length=1, max_length=200)
    baseline: VariantPayload
    experimental: VariantPayload
    quality_policy: QualityPolicyPayload
    salt: str = ""
    status: FlagStatus = FlagStatus.OFF
    rollout_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    targeting: list[TargetingRulePayload] = Field(default_factory=list)
    rollout_plan: RolloutPlanPayload | None = None

    def to_domain_dict(self) -> dict[str, Any]:
        """Render in the exact shape :func:`flag_from_dict` consumes."""
        payload = self.model_dump(mode="json")
        if payload.get("rollout_plan") is None:
            payload["rollout_plan"] = {
                "cooldown_seconds": DEFAULT_ROLLOUT_PLAN.cooldown_seconds,
                "stages": [
                    {"percentage": s.percentage, "dwell_seconds": s.dwell_seconds}
                    for s in DEFAULT_ROLLOUT_PLAN.stages
                ],
            }
        return payload


class CreateFlagRequest(Attribution):
    flag: FlagPayload


class ReplaceFlagRequest(Attribution):
    flag: FlagPayload


class RolloutRequest(Attribution):
    percentage: float = Field(ge=0.0, le=100.0)


class MutationResponse(BaseModel):
    """Every mutation answers with the snapshot version it produced.

    A caller can poll ``GET /snapshot`` until it reports at least this version
    and know its own change is live, rather than guessing at propagation delay.
    """

    snapshot_version: int
    flag_key: str


class AuditEventResponse(BaseModel):
    flag_key: str
    action: str
    actor: str
    reason: str
    at: str
    snapshot_version: int
    detail: dict[str, Any]


class SnapshotResponse(BaseModel):
    version: int
    published_at: str
    flags: dict[str, dict[str, Any]]
