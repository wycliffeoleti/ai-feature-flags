"""Management API surface.

Driven against the in-memory repository so the whole surface is exercised with no
database running. The repository contract test already proves PostgreSQL behaves
identically, so there is nothing gained by running these twice.

The assertions that matter operationally: no mutation is possible without an
actor and a reason, every mutation reports the snapshot version it produced, and
pause/rollback/resume mean exactly what an operator under pressure would assume.
"""

import unittest

from fastapi.testclient import TestClient

from aiflags.api.app import create_app
from aiflags.store.memory import InMemoryFlagRepository

FLAG = {
    "key": "subject_line",
    "salt": "salt-a",
    "baseline": {"key": "v1", "kind": "baseline", "config": {"prompt": "a"}},
    "experimental": {"key": "v2", "kind": "experimental", "config": {"prompt": "b"}},
    "quality_policy": {
        "gates": [
            {
                "signal": "judge_score",
                "statistic": "p10",
                "comparison": "below",
                "threshold": 3.0,
                "sustained_evaluations": 50,
            }
        ]
    },
}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = InMemoryFlagRepository()
        self.client = TestClient(create_app(self.repo))

    def create(self, flag=None, actor="wycliffe", reason="initial"):
        return self.client.post(
            "/flags",
            json={"actor": actor, "reason": reason, "flag": flag or FLAG},
        )


class CreateAndReadTests(ApiTestCase):
    def test_creating_a_flag_returns_201_and_the_snapshot_version(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["snapshot_version"], 1)
        self.assertEqual(response.json()["flag_key"], "subject_line")

    def test_a_created_flag_can_be_read_back(self):
        self.create()
        body = self.client.get("/flags/subject_line").json()
        self.assertEqual(body["baseline"]["config"], {"prompt": "a"})
        self.assertEqual(body["status"], "off")
        self.assertEqual(body["rollout_percentage"], 0.0)

    def test_a_new_flag_defaults_to_the_guides_rollout_plan(self):
        self.create()
        stages = self.client.get("/flags/subject_line").json()["rollout_plan"]["stages"]
        self.assertEqual([s["percentage"] for s in stages], [1.0, 5.0, 25.0, 50.0, 100.0])

    def test_listing_returns_every_flag(self):
        self.create()
        self.create({**FLAG, "key": "other"})
        self.assertEqual(len(self.client.get("/flags").json()), 2)

    def test_an_unknown_flag_is_404(self):
        self.assertEqual(self.client.get("/flags/nope").status_code, 404)

    def test_a_duplicate_key_is_409(self):
        self.create()
        self.assertEqual(self.create().status_code, 409)


class ValidationTests(ApiTestCase):
    def test_a_missing_actor_is_rejected(self):
        response = self.client.post("/flags", json={"reason": "r", "flag": FLAG})
        self.assertEqual(response.status_code, 422)

    def test_a_blank_reason_is_rejected(self):
        self.assertEqual(self.create(reason="").status_code, 422)

    def test_a_flag_with_no_quality_gate_is_rejected(self):
        """An AI flag with no definition of quality cannot be safely rolled out."""
        broken = {**FLAG, "quality_policy": {"gates": []}}
        self.assertEqual(self.create(broken).status_code, 422)

    def test_a_decreasing_rollout_plan_is_rejected_with_a_useful_message(self):
        broken = {
            **FLAG,
            "rollout_plan": {
                "stages": [
                    {"percentage": 50.0, "dwell_seconds": 60.0},
                    {"percentage": 10.0, "dwell_seconds": 60.0},
                ]
            },
        }
        response = self.create(broken)
        self.assertEqual(response.status_code, 400)
        self.assertIn("non-decreasing", response.json()["detail"])

    def test_mismatched_variant_kinds_are_rejected(self):
        broken = {**FLAG, "baseline": {"key": "v1", "kind": "experimental"}}
        self.assertEqual(self.create(broken).status_code, 400)

    def test_an_out_of_range_percentage_is_rejected(self):
        self.create()
        response = self.client.post(
            "/flags/subject_line/rollout",
            json={"actor": "w", "reason": "r", "percentage": 150.0},
        )
        self.assertEqual(response.status_code, 422)

    def test_a_body_key_that_disagrees_with_the_path_is_rejected(self):
        self.create()
        response = self.client.put(
            "/flags/subject_line",
            json={"actor": "w", "reason": "r", "flag": {**FLAG, "key": "different"}},
        )
        self.assertEqual(response.status_code, 400)


class RolloutControlTests(ApiTestCase):
    def test_setting_the_rollout_percentage(self):
        self.create()
        response = self.client.post(
            "/flags/subject_line/rollout",
            json={"actor": "controller", "reason": "stage 1", "percentage": 5.0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/flags/subject_line").json()["rollout_percentage"], 5.0
        )

    def test_pausing_holds_the_current_percentage(self):
        """Pause stops the ramp; it must not change what users currently see."""
        self.create()
        self.client.post(
            "/flags/subject_line/rollout",
            json={"actor": "w", "reason": "ramp", "percentage": 25.0},
        )
        self.client.post(
            "/flags/subject_line/pause", json={"actor": "w", "reason": "investigating"}
        )
        body = self.client.get("/flags/subject_line").json()
        self.assertEqual(body["status"], "paused")
        self.assertEqual(body["rollout_percentage"], 25.0)

    def test_rollback_zeroes_the_percentage_and_sets_the_status(self):
        self.create()
        self.client.post(
            "/flags/subject_line/rollout",
            json={"actor": "w", "reason": "ramp", "percentage": 50.0},
        )
        self.client.post(
            "/flags/subject_line/rollback",
            json={"actor": "controller", "reason": "p10 below 3.0"},
        )
        body = self.client.get("/flags/subject_line").json()
        self.assertEqual(body["status"], "rolled_back")
        self.assertEqual(body["rollout_percentage"], 0.0)

    def test_resume_returns_a_rolled_back_flag_to_rolling_out(self):
        self.create()
        self.client.post(
            "/flags/subject_line/rollback", json={"actor": "w", "reason": "bad"}
        )
        self.client.post(
            "/flags/subject_line/resume", json={"actor": "w", "reason": "prompt fixed"}
        )
        self.assertEqual(
            self.client.get("/flags/subject_line").json()["status"], "rolling_out"
        )

    def test_controlling_an_unknown_flag_is_404(self):
        for path, body in (
            ("rollout", {"actor": "w", "reason": "r", "percentage": 1.0}),
            ("pause", {"actor": "w", "reason": "r"}),
            ("rollback", {"actor": "w", "reason": "r"}),
        ):
            with self.subTest(path=path):
                response = self.client.post(f"/flags/nope/{path}", json=body)
                self.assertEqual(response.status_code, 404)


class SnapshotTests(ApiTestCase):
    def test_an_empty_snapshot_is_version_zero(self):
        body = self.client.get("/snapshot").json()
        self.assertEqual(body["version"], 0)
        self.assertEqual(body["flags"], {})

    def test_the_snapshot_version_matches_the_last_mutation(self):
        self.create()
        version = self.client.post(
            "/flags/subject_line/rollout",
            json={"actor": "w", "reason": "ramp", "percentage": 5.0},
        ).json()["snapshot_version"]
        self.assertEqual(self.client.get("/snapshot").json()["version"], version)

    def test_the_snapshot_carries_the_full_flag_definition(self):
        self.create()
        flag = self.client.get("/snapshot").json()["flags"]["subject_line"]
        self.assertEqual(flag["experimental"]["config"], {"prompt": "b"})
        self.assertIn("quality_policy", flag)

    def test_the_snapshot_is_consumable_by_the_sdk(self):
        """The published shape must decode straight back into the domain model."""
        from aiflags.store.base import flag_from_dict

        self.create()
        flag = self.client.get("/snapshot").json()["flags"]["subject_line"]
        self.assertEqual(flag_from_dict(flag).key, "subject_line")


class AuditTests(ApiTestCase):
    def test_every_mutation_appears_in_the_audit_log(self):
        self.create()
        self.client.post(
            "/flags/subject_line/rollout",
            json={"actor": "controller", "reason": "stage 1", "percentage": 5.0},
        )
        self.client.post(
            "/flags/subject_line/rollback",
            json={"actor": "controller", "reason": "p10 below 3.0"},
        )
        events = self.client.get("/audit").json()
        self.assertEqual(
            [e["action"] for e in events],
            ["create_flag", "set_rollout_percentage", "rollback"],
        )

    def test_the_audit_log_records_who_and_why(self):
        self.create(actor="wycliffe", reason="launching subject line v2")
        event = self.client.get("/audit").json()[0]
        self.assertEqual(event["actor"], "wycliffe")
        self.assertEqual(event["reason"], "launching subject line v2")

    def test_a_rollback_records_the_percentage_it_reverted_from(self):
        self.create()
        self.client.post(
            "/flags/subject_line/rollout",
            json={"actor": "w", "reason": "ramp", "percentage": 25.0},
        )
        self.client.post(
            "/flags/subject_line/rollback", json={"actor": "c", "reason": "degraded"}
        )
        event = self.client.get("/audit").json()[-1]
        self.assertEqual(event["detail"]["previous_percentage"], 25.0)

    def test_the_audit_log_can_be_filtered_by_flag(self):
        self.create()
        self.create({**FLAG, "key": "other"})
        events = self.client.get("/audit", params={"flag_key": "other"}).json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["flag_key"], "other")


class HealthTests(ApiTestCase):
    def test_health_is_reported(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_the_openapi_schema_is_generated(self):
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)


if __name__ == "__main__":
    unittest.main()
