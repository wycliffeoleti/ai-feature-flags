"""Notification payloads and delivery.

The payload is asserted field by field because it is the only artefact proving
the alerting path works without a Slack workspace: the recording notifier stores
exactly what the Slack notifier would post, since both call the same builder.

The delivery contract is that a notification failure never propagates. By the
time a notification is sent, the rollback has already happened — failing to
announce it must not turn into a failure to perform it.
"""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from aiflags.notify.base import Notification
from aiflags.notify.recording import RecordingNotifier
from aiflags.notify.slack import SlackWebhookNotifier

AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def make_notification(**overrides):
    params = {
        "flag_key": "subject_line",
        "action": "rollback",
        "reason": "judge_score p10 of 1.8 is below the threshold 3.0",
        "at": AT,
        "snapshot_version": 42,
        "severity": "critical",
        "detail": {"p10": 1.8, "threshold": 3.0, "samples": 50},
    }
    params.update(overrides)
    return Notification(**params)


class PayloadTests(unittest.TestCase):
    def test_the_payload_names_the_flag_and_the_action(self):
        payload = make_notification().as_slack_payload()
        self.assertIn("subject_line", payload["text"])
        self.assertIn("rollback", payload["text"])

    def test_the_payload_carries_the_reason(self):
        body = make_notification().as_slack_payload()["blocks"][0]["text"]["text"]
        self.assertIn("p10 of 1.8", body)

    def test_the_payload_carries_the_supporting_numbers(self):
        """An alert that says only 'rolled back' makes someone open a dashboard."""
        body = make_notification().as_slack_payload()["blocks"][0]["text"]["text"]
        for fragment in ("p10", "1.8", "threshold", "3.0", "samples", "50"):
            self.assertIn(fragment, body)

    def test_the_payload_carries_the_audit_version(self):
        body = make_notification().as_slack_payload()["blocks"][0]["text"]["text"]
        self.assertIn("v42", body)

    def test_critical_and_informational_alerts_are_distinguishable(self):
        critical = make_notification(severity="critical").as_slack_payload()["text"]
        info = make_notification(severity="info").as_slack_payload()["text"]
        self.assertNotEqual(critical, info)

    def test_the_payload_is_json_serialisable(self):
        json.dumps(make_notification().as_slack_payload())

    def test_detail_ordering_is_stable(self):
        """Two runs of the same rollback must produce identical evidence."""
        first = make_notification().as_slack_payload()
        second = make_notification().as_slack_payload()
        self.assertEqual(first, second)


class RecordingNotifierTests(unittest.TestCase):
    def test_notifications_are_recorded_rather_than_sent(self):
        notifier = RecordingNotifier()
        self.assertTrue(notifier.send(make_notification()))
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0].flag_key, "subject_line")

    def test_recorded_payloads_match_what_slack_would_receive(self):
        notifier = RecordingNotifier()
        notification = make_notification()
        notifier.send(notification)
        self.assertEqual(notifier.payloads[0], notification.as_slack_payload())

    def test_notifications_can_be_appended_to_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts" / "notifications.jsonl"
            notifier = RecordingNotifier(path)
            notifier.send(make_notification())
            notifier.send(make_notification(action="advance", severity="info"))
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("subject_line", json.loads(lines[0])["text"])


class SlackNotifierTests(unittest.TestCase):
    def test_a_non_https_webhook_is_refused(self):
        with self.assertRaises(ValueError):
            SlackWebhookNotifier("http://hooks.slack.com/services/x")

    def test_delivery_failure_is_reported_not_raised(self):
        """A Slack outage must not stop a rollback from completing."""
        notifier = SlackWebhookNotifier(
            "https://127.0.0.1:1/services/nothing", timeout_seconds=0.25
        )
        self.assertFalse(notifier.send(make_notification()))


if __name__ == "__main__":
    unittest.main()
