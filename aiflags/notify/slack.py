"""Slack incoming-webhook notifier — opt-in.

Fully implemented, never the default. Posting to a real workspace needs a webhook
URL that someone deliberately supplied, so this is only constructed when
`AIFLAGS_SLACK_WEBHOOK_URL` is set. Without it the system uses
`RecordingNotifier` and no traffic leaves the machine.

Delivery failure is logged and reported as `False`, never raised: a Slack outage
must not prevent a rollback from completing. The rollback is the safety action;
telling people about it is important but secondary.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from aiflags.notify.base import Notification

logger = logging.getLogger(__name__)


class SlackWebhookNotifier:
    """Posts notifications to a Slack incoming webhook."""

    def __init__(self, webhook_url: str, timeout_seconds: float = 10.0) -> None:
        if not webhook_url.startswith("https://"):
            raise ValueError("Slack webhook URL must be https")
        self._webhook_url = webhook_url
        self._timeout = timeout_seconds

    def send(self, notification: Notification) -> bool:
        payload = json.dumps(notification.as_slack_payload()).encode()
        request = urllib.request.Request(
            self._webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Deliberately swallowed. The rollback has already happened; failing
            # to announce it must not turn into a failure to perform it.
            logger.warning(
                "Slack notification for %s failed: %s", notification.flag_key, exc
            )
            return False
