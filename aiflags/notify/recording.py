"""Recording notifier — the default.

Writes the exact Slack payload that would have been delivered to an in-memory
list and, optionally, to a JSON Lines file. Nothing leaves the machine.

This is the default deliberately. Delivering to a real Slack workspace is an
outward-facing action needing a webhook URL and someone's consent, and it should
never happen as a side effect of running a demo. Recording keeps the alerting
path fully exercised and inspectable in tests and in the portfolio walkthrough:
the payload asserted here is the payload that would be posted, because both
notifiers call the same `Notification.as_slack_payload`.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiflags.notify.base import Notification


class RecordingNotifier:
    """Stores notifications instead of sending them."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(notification.as_slack_payload(), ensure_ascii=False)
                    + "\n"
                )
        return True

    @property
    def payloads(self) -> list[dict]:
        """The exact bodies that would have been posted to Slack."""
        return [notification.as_slack_payload() for notification in self.sent]
