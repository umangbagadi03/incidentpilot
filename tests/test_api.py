"""API tests. They use FastAPI's TestClient (needs `pip install httpx`), which runs background tasks before returning."""
import unittest

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

from incidentpilot.api import create_app
from tests.helpers import make_service


@unittest.skipIf(TestClient is None, "fastapi test client is not available")
class TestApi(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        self.client = TestClient(create_app(self.service))

    def test_health_and_scenarios(self):
        self.assertEqual(self.client.get("/health").json()["retrieval"], "bm25 only")
        names = []
        for scenario in self.client.get("/scenarios").json():
            names.append(scenario["name"])
        self.assertIn("bad_deploy_checkout", names)
        self.assertEqual(len(names), 6)

    def test_home_page_is_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("IncidentPilot", response.text)

    def test_full_flow_trigger_approve_report(self):
        incident = self.client.post("/scenarios/bad_deploy_checkout/trigger").json()
        incident_id = incident["id"]

        waiting = self.client.get("/incidents/" + incident_id).json()
        self.assertEqual(waiting["status"], "waiting_approval")
        self.assertEqual(waiting["state"]["approval"]["action"], "rollback_deploy")
        self.assertEqual(self.client.get("/incidents/" + incident_id + "/report").status_code, 409)

        answer = self.client.post("/incidents/" + incident_id + "/approval",
                                  json={"approved": True, "reviewer": "alice", "comment": "ok"})
        self.assertEqual(answer.status_code, 200)

        done = self.client.get("/incidents/" + incident_id).json()
        self.assertEqual(done["status"], "resolved")
        self.assertIn("# Incident report", self.client.get("/incidents/" + incident_id + "/report").text)
        self.assertEqual(len(self.client.get("/incidents").json()), 1)

    def test_rejecting_escalates(self):
        incident_id = self.client.post("/scenarios/memory_leak_payments/trigger").json()["id"]
        self.client.post("/incidents/" + incident_id + "/approval", json={"approved": False, "reviewer": "bob"})
        self.assertEqual(self.client.get("/incidents/" + incident_id).json()["status"], "escalated")

    def test_second_decision_is_a_conflict(self):
        incident_id = self.client.post("/scenarios/bad_deploy_checkout/trigger").json()["id"]
        self.client.post("/incidents/" + incident_id + "/approval", json={"approved": True})
        again = self.client.post("/incidents/" + incident_id + "/approval", json={"approved": True})
        self.assertEqual(again.status_code, 409)

    def test_unknown_things_are_404(self):
        self.assertEqual(self.client.get("/incidents/inc-nope").status_code, 404)
        self.assertEqual(self.client.post("/scenarios/nope/trigger").status_code, 404)
        self.assertEqual(self.client.post("/incidents/inc-nope/approval", json={"approved": True}).status_code, 404)

    def test_bad_alert_body_is_rejected(self):
        self.assertEqual(self.client.post("/incidents", json={"service": "checkout"}).status_code, 422)

    def test_alert_from_a_webhook_starts_an_incident(self):
        body = {"service": "checkout", "title": "HTTP 500 on checkout", "severity": "critical"}
        incident = self.client.post("/incidents", json=body).json()
        self.assertEqual(self.client.get("/incidents/" + incident["id"]).json()["status"], "waiting_approval")

    def test_metrics_endpoint(self):
        self.client.post("/scenarios/insufficient_evidence_search/trigger")
        self.assertIn("ip_incidents_finished_total", self.client.get("/metrics").text)


if __name__ == "__main__":
    unittest.main()
