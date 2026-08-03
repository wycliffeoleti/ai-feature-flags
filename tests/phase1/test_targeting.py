"""Targeting precedence is load-bearing and therefore tested as a table.

The ordering exists so an operator can express "everyone in the beta segment,
except these three accounts we know are broken" and have it mean what it reads
like. Getting the order wrong silently exposes a blocked account.
"""

import unittest

from aiflags.core.models import (
    EvaluationContext,
    TargetingKind,
    TargetingRule,
    VariantKind,
)
from aiflags.core.targeting import match_targeting

BLOCK_USER = TargetingRule(
    kind=TargetingKind.BLOCKLIST,
    values=frozenset({"user-blocked"}),
    variant_kind=VariantKind.BASELINE,
)
ALLOW_USER = TargetingRule(
    kind=TargetingKind.ALLOWLIST,
    values=frozenset({"user-blocked", "user-allowed"}),
    variant_kind=VariantKind.EXPERIMENTAL,
)
SEGMENT_INTERNAL = TargetingRule(
    kind=TargetingKind.SEGMENT,
    values=frozenset({"internal"}),
    variant_kind=VariantKind.EXPERIMENTAL,
    attribute="segment",
)
GEO_DE = TargetingRule(
    kind=TargetingKind.GEO,
    values=frozenset({"DE"}),
    variant_kind=VariantKind.BASELINE,
    attribute="country",
)
METADATA_LONG = TargetingRule(
    kind=TargetingKind.METADATA,
    values=frozenset({"long_form"}),
    variant_kind=VariantKind.BASELINE,
    attribute="input_type",
)

ALL_RULES = (METADATA_LONG, GEO_DE, SEGMENT_INTERNAL, ALLOW_USER, BLOCK_USER)


class TargetingPrecedenceTests(unittest.TestCase):
    """Rules are declared out of order above; precedence must not depend on that."""

    def test_blocklist_beats_allowlist(self):
        context = EvaluationContext(subject_key="user-blocked")
        matched = match_targeting(ALL_RULES, context)
        self.assertIsNotNone(matched)
        self.assertEqual(matched.rule.kind, TargetingKind.BLOCKLIST)
        self.assertEqual(matched.variant_kind, VariantKind.BASELINE)

    def test_blocklist_beats_segment(self):
        context = EvaluationContext(
            subject_key="user-blocked", attributes={"segment": "internal"}
        )
        matched = match_targeting(ALL_RULES, context)
        self.assertEqual(matched.rule.kind, TargetingKind.BLOCKLIST)

    def test_allowlist_beats_segment_and_geo(self):
        context = EvaluationContext(
            subject_key="user-allowed",
            attributes={"segment": "external", "country": "DE"},
        )
        matched = match_targeting(ALL_RULES, context)
        self.assertEqual(matched.rule.kind, TargetingKind.ALLOWLIST)
        self.assertEqual(matched.variant_kind, VariantKind.EXPERIMENTAL)

    def test_segment_beats_geo(self):
        context = EvaluationContext(
            subject_key="user-ordinary",
            attributes={"segment": "internal", "country": "DE"},
        )
        matched = match_targeting(ALL_RULES, context)
        self.assertEqual(matched.rule.kind, TargetingKind.SEGMENT)

    def test_geo_beats_metadata(self):
        context = EvaluationContext(
            subject_key="user-ordinary",
            attributes={"country": "DE", "input_type": "long_form"},
        )
        matched = match_targeting(ALL_RULES, context)
        self.assertEqual(matched.rule.kind, TargetingKind.GEO)

    def test_metadata_matches_when_nothing_else_does(self):
        context = EvaluationContext(
            subject_key="user-ordinary", attributes={"input_type": "long_form"}
        )
        matched = match_targeting(ALL_RULES, context)
        self.assertEqual(matched.rule.kind, TargetingKind.METADATA)


class TargetingMissTests(unittest.TestCase):
    def test_no_rules_means_no_match(self):
        context = EvaluationContext(subject_key="user-ordinary")
        self.assertIsNone(match_targeting((), context))

    def test_unmatched_context_falls_through_to_percentage(self):
        context = EvaluationContext(
            subject_key="user-ordinary",
            attributes={"segment": "external", "country": "FR"},
        )
        self.assertIsNone(match_targeting(ALL_RULES, context))

    def test_missing_attribute_does_not_match(self):
        """An absent attribute must miss, never match on ``None``."""
        context = EvaluationContext(subject_key="user-ordinary", attributes={})
        self.assertIsNone(match_targeting((SEGMENT_INTERNAL,), context))

    def test_attribute_match_is_case_sensitive(self):
        context = EvaluationContext(
            subject_key="user-ordinary", attributes={"country": "de"}
        )
        self.assertIsNone(match_targeting((GEO_DE,), context))


class TargetingRuleValidationTests(unittest.TestCase):
    def test_attribute_rule_requires_an_attribute(self):
        with self.assertRaises(ValueError):
            TargetingRule(
                kind=TargetingKind.SEGMENT,
                values=frozenset({"internal"}),
                variant_kind=VariantKind.EXPERIMENTAL,
            )

    def test_rule_requires_at_least_one_value(self):
        with self.assertRaises(ValueError):
            TargetingRule(
                kind=TargetingKind.BLOCKLIST,
                values=frozenset(),
                variant_kind=VariantKind.BASELINE,
            )


if __name__ == "__main__":
    unittest.main()
