"""Rule-based variant overrides, evaluated before the percentage ramp.

Targeting is what makes a rollout *controlled* rather than merely random: ship to
internal users first, then a beta segment, then everyone, while keeping an escape
hatch for specific accounts.

Precedence is fixed by :data:`~aiflags.core.models.TARGETING_PRECEDENCE` and does
not depend on the order rules were declared in. Blocklist wins over everything,
so "everyone internal except these accounts" reads the way an operator expects.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiflags.core.models import (
    TARGETING_PRECEDENCE,
    EvaluationContext,
    TargetingKind,
    TargetingRule,
    VariantKind,
)

_SUBJECT_KEY_KINDS = frozenset({TargetingKind.BLOCKLIST, TargetingKind.ALLOWLIST})


@dataclass(frozen=True, slots=True)
class TargetingMatch:
    """The winning rule and the variant it forces."""

    rule: TargetingRule
    variant_kind: VariantKind


def match_targeting(
    rules: tuple[TargetingRule, ...], context: EvaluationContext
) -> TargetingMatch | None:
    """Return the highest-precedence matching rule, or ``None`` to fall through.

    ``None`` means no rule applied and the caller should use the percentage ramp.
    """
    for kind in TARGETING_PRECEDENCE:
        for rule in rules:
            if rule.kind is kind and _rule_matches(rule, context):
                return TargetingMatch(rule=rule, variant_kind=rule.variant_kind)
    return None


def _rule_matches(rule: TargetingRule, context: EvaluationContext) -> bool:
    if rule.kind in _SUBJECT_KEY_KINDS:
        return context.subject_key in rule.values
    # An absent attribute must miss rather than match on a null sentinel.
    observed = context.attributes.get(rule.attribute or "")
    return observed is not None and observed in rule.values
