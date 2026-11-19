"""Model choice: "bring your own key", and keeping keys secret."""
import os
import unittest

from fastapi.testclient import TestClient

import incidentpilot.api as api_module
from incidentpilot.api import create_app
from incidentpilot.scripted_llm import ScriptedLLM
from incidentpilot.llm import make_llm, redact_secrets
from incidentpilot.service import run_to_end
from tests.helpers import approve_yes, make_service, make_settings

FAKE_KEY = "sk-test-SECRET-1234567890abcdef"


class OwnKeyLLM(ScriptedLLM):
    """Stands in for a model built from a visitor's key: scripted answers, but it carries the key."""
    label = "OpenAI · gpt-test"
    scripted = False

    def __init__(self, fail_on_rca=False):
        ScriptedLLM.__init__(self)
        self.api_key = FAKE_KEY
        self.calls = 0
        self.fail_on_rca = fail_on_rca

    def complete(self, system, user):
        self.calls = self.calls + 1
        if self.fail_on_rca and "TASK: write_rca" in user:
            raise RuntimeError("Incorrect API key provided: " + FAKE_KEY)
        return ScriptedLLM.complete(self, system, user)


class TestOwnKey(unittest.TestCase):
    def test_incident_uses_its_own_model_and_others_use_the_default(self):
        service = make_service()
        own = OwnKeyLLM()
        mine = run_to_end(service, service.trigger_scenario("bad_deploy_checkout", own)["id"], approve_yes)
        other = run_to_end(service, service.trigger_scenario("memory_leak_payments")["id"], approve_yes)
        self.assertGreater(own.calls, 0)
        self.assertEqual(mine["state"]["llm"], "OpenAI · gpt-test")
        self.assertEqual(other["state"]["llm"], "Scripted (offline)")

    def test_the_model_is_forgotten_when_the_incident_ends(self):
        service = make_service()
        incident = service.trigger_scenario("bad_deploy_checkout", OwnKeyLLM())
        run_to_end(service, incident["id"], approve_yes)
        self.assertNotIn(incident["id"], service.incident_llms)

    def test_the_key_is_never_written_to_disk(self):
        settings = make_settings()
        service = make_service(settings=settings)
        incident = service.trigger_scenario("bad_deploy_checkout", OwnKeyLLM(fail_on_rca=True))
        with self.assertLogs("incidentpilot.service", level="ERROR"):
            service.run(incident["id"], service.initial_input(incident))
        stored = service.get(incident["id"])
        self.assertEqual(stored["status"], "error")
        self.assertIn("[redacted]", stored["state"]["timeline"][-1]["message"])
        for name in os.listdir(settings.data_dir):
            path = os.path.join(settings.data_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    self.assertNotIn(FAKE_KEY.encode(), handle.read(), name)


class TestApiModelChoice(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(make_service()))
        self.real_make_llm = api_module.make_llm

    def tearDown(self):
        api_module.make_llm = self.real_make_llm

    def test_unknown_provider_is_rejected(self):
        response = self.client.post("/scenarios/bad_deploy_checkout/trigger",
                                    json={"provider": "nope", "api_key": FAKE_KEY})
        self.assertEqual(response.status_code, 422)

    def test_own_key_runs_the_incident_on_that_model(self):
        api_module.make_llm = lambda provider, key, model: OwnKeyLLM()
        incident = self.client.post("/scenarios/bad_deploy_checkout/trigger",
                                    json={"provider": "openai", "api_key": FAKE_KEY}).json()
        state = self.client.get("/incidents/" + incident["id"]).json()["state"]
        self.assertEqual(state["llm"], "OpenAI · gpt-test")
        self.assertNotIn(FAKE_KEY, self.client.get("/incidents/" + incident["id"]).text)

    def test_no_body_still_uses_the_server_model(self):
        incident = self.client.post("/scenarios/bad_deploy_checkout/trigger").json()
        self.assertEqual(self.client.get("/incidents/" + incident["id"]).json()["state"]["llm"], "Scripted (offline)")


class TestHelpers(unittest.TestCase):
    def test_keys_are_redacted(self):
        text = redact_secrets("bad key sk-proj-abcdefghij123 and AIzaSyAbCdEfGhIjKlMnOp and mine12345678", ["mine12345678"])
        self.assertNotIn("abcdefghij", text)
        self.assertNotIn("AIza", text)
        self.assertNotIn("mine12345678", text)

    def test_unknown_provider_is_an_error(self):
        with self.assertRaises(ValueError):
            make_llm("nope", "key")


if __name__ == "__main__":
    unittest.main()
