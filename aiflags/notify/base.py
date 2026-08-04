"""Rollback and rollout notifications.

A rollback alert has one job: tell a human what happened and give them enough to
decide whether to act, without needing to open a dashboard first. So the payload
carries the flag, the action, the reason, the numbers behind it, and the audit
version — not just "flag X rolled back".

:class:`Notifier` is the seam. The default implementation records the exact
payload locally; the Slack one posts it. Both build the identical message, so
what gets recorded offline is what would have been delivered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Notification:
    """One thing worth telling a human about."""

    flag_key: str
    action: str
    reason: str
    at: datetime
    snapshot_version: int
    severity: str = "info"
    detail: dict[str, Any] = field(default_factory=dict)

    def as_slack_payload(self) -> dict[str, Any]:
        """Render as a Slack incoming-webhook body.

        Built here rather than in the Slack notifier so the recording sink stores
        byte-identical content to what would be delivered — otherwise the offline
        evidence proves nothing about the real path.
        """
        icon = "🚨" if self.severity == "critical" else "ℹ️"
        headline = f"{icon} `{self.flag_key}` — {self.action}"
        lines = [f"*{headline}*", self.reason]
        if self.detail:
            lines.append(
                "\n".join(f"• {key}: {value}" for key, value in sorted(self.detail.items()))
            )
        lines.append(
            f"_snapshot v{self.snapshot_version} · {self.at.isoformat()}_"
        )
        return {
            "text": headline,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "\n".join(lines)},
                }
            ],
        }


class Notifier(Protocol):
    """Delivers a notification somewhere."""

    def send(self, notification: Notification) -> bool: ...
