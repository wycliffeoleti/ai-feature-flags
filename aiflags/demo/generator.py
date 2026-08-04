"""Email subject line generator — the demo's AI feature.

Deterministic and offline by default. The point of the demo is the rollout
machinery, not the model, and a deterministic generator makes the whole scenario
reproducible: the same run produces the same rollback for the same reason every
time.

**The failure it demonstrates is a real one.** The experimental prompt template
references `{customer_name}`, a field this pipeline does not populate. Nothing
raises. The template renders with the placeholder intact, users get
"Hi {customer_name}, your invoice is ready", and every downstream system reports
success. That is precisely the shape of AI feature failure a boolean flag cannot
detect — the code path works, the output is wrong — and it is why the quality
gate exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class _LeakyFields(dict):
    """Renders unknown placeholders verbatim instead of raising.

    Mirrors how prompt templating usually fails in production: a missing
    variable does not crash the request, it ships to the user.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True, slots=True)
class Email:
    """One synthetic inbound email."""

    subject_hint: str
    topic: str
    body: str

    def fields(self) -> dict[str, str]:
        return {"topic": self.topic, "hint": self.subject_hint, "body": self.body}


# Synthetic corpus. Small and fixed so the demo is reproducible; no real
# customer data is involved anywhere in this project.
EMAILS: tuple[Email, ...] = (
    Email("invoice", "March invoice", "Your March invoice is attached and due on the 30th."),
    Email("password", "password reset", "Someone requested a password reset for your account."),
    Email("shipping", "order shipped", "Order 4417 shipped and arrives Thursday."),
    Email("renewal", "plan renewal", "Your annual plan renews on 12 September."),
    Email("receipt", "payment receipt", "We received your payment of 42.00 EUR."),
    Email("meeting", "meeting moved", "The Tuesday review moved to Wednesday at 10:00."),
    Email("outage", "service restored", "The API outage this morning has been resolved."),
    Email("welcome", "getting started", "Welcome aboard — here is how to get started."),
)

GOOD_TEMPLATE = "{topic} — action needed"
"""Uses only fields the pipeline populates."""

BROKEN_TEMPLATE = "Hi {customer_name}, about your {topic}"
"""References a field this pipeline never supplies, so it leaks to the user."""


@dataclass
class SubjectLineGenerator:
    """Renders a subject line from a variant's config.

    ``config["template"]`` is the whole interface. The flag system treats it as
    opaque, which is what lets the same flag mechanism cover prompt changes,
    model swaps, and non-AI fallbacks without knowing the difference.
    """

    calls: int = field(default=0)

    def generate(self, email: Email, config: dict[str, Any]) -> str:
        self.calls += 1
        template = config.get("template", GOOD_TEMPLATE)
        return template.format_map(_LeakyFields(email.fields()))
