"""Tests of the LangGraph workflow, driven through the service (the same way the CLI and the API drive it)."""
import json
import os
import unittest

from evaluate import evaluate_one_run
from incidentpilot.demo_llm import DemoLLM
from incidentpilot.rag import build_retriever, load_documents
from incidentpilot.service import ServiceError, build_service, run_to_end
from tests.helpers import approve_no, approve_yes, json_reply, make_service, make_settings


def run_scenario(service, name, approver=approve_yes):
    incident = service.trigger_scenario(name)
    return run_to_end(service, incident["id"], approver)


class TestHappyPaths(unittest.TestCase):
    def test_bad_deploy_is_rolled_back_and_resolved(self):
        service = make_service()
        final = run_scenario(service, "bad_deploy_checkout")
        self.assertEqual(final["status"], "resolved")
        self.assertEqual(service.tools.actions[0]["action"], "rollback_deploy")
        self.assertTrue(final["state"]["verification"]["healthy"])
        self.assertIn("# Incident report", final["state"]["report"])

    def test_memory_leak_is_restarted_and_resolved(self):
        service = make_service()
        final = run_scenario(service, "memory_leak_payments")
        self.assertEqual(final["status"], "resolved")
        self.assertEqual(service.tools.actions[0]["action"], "restart_service")

    def test_weak_evidence_is_escalated_and_nothing_is_changed(self):
        service = make_service()
        final = run_scenario(service, "insufficient_evidence_search")
        self.assertEqual(final["status"], "escalated")
        self.assertEqual(service.tools.actions, [])
        self.assertIn("inconclusive", final["state"]["escalation_reason"])

    def test_rca_only_cites_existing_evidence(self):
        final = run_scenario(make_service(), "bad_deploy_checkout")
        existing = []
        for evidence in final["state"]["evidence"]:
            existing.append(evidence["id"])
        for cited in final["state"]["rca"]["evidence_ids"]:
            self.assertIn(cited, existing)

    def test_evaluation_passes_for_all_demo_scenarios(self):
        settings = make_settings()
        retriever = build_retriever(settings, load_documents(settings.knowledge_dir))
        for name in make_service().tools.list_scenarios():
            self.assertEqual(evaluate_one_run(settings, DemoLLM(), retriever, name), [], name)


class TestHarderScenarios(unittest.TestCase):
    def test_red_herring_deploy_is_not_rolled_back(self):
        service = make_service()
        final = run_scenario(service, "db_pool_red_herring_inventory")
        self.assertEqual(final["status"], "resolved")
        executed = []
        for item in service.tools.actions:
            executed.append(item["action"])
        self.assertEqual(executed, ["restart_service"])
        self.assertEqual(final["state"]["rca"]["category"], "db_pool_exhaustion")

    def test_unbacked_claim_lowers_the_confidence(self):
        # In this script one claim cites ERROR logs for a value that is only in the WARN logs.
        final = run_scenario(make_service(), "db_pool_red_herring_inventory")
        rca = final["state"]["rca"]
        self.assertEqual(rca["grounding_score"], 0.75)
        self.assertEqual(rca["confidence"], 0.75)
        unbacked = []
        for claim in rca["claims"]:
            if not claim["verified"]:
                unbacked.append(claim)
        self.assertEqual(len(unbacked), 1)
        self.assertIn("28400", unbacked[0]["missing_facts"])

    def test_failed_restart_is_caught_then_scaling_fixes_it(self):
        service = make_service()
        final = run_scenario(service, "traffic_spike_orders")
        self.assertEqual(final["status"], "resolved")
        executed = []
        for item in service.tools.actions:
            executed.append(item["action"])
        self.assertEqual(executed, ["restart_service", "scale_service"])
        self.assertEqual(final["state"]["attempts"], 2)
        refuted = []
        for hypothesis in final["state"]["hypotheses"]:
            if hypothesis["status"] == "refuted":
                refuted.append(hypothesis["id"])
        self.assertIn("H3", refuted)                 # "stuck workers" was refuted by the failed restart

    def test_hypothesis_board_is_kept_in_the_state(self):
        final = run_scenario(make_service(), "bad_deploy_checkout")
        board = final["state"]["hypotheses"]
        self.assertEqual(len(board), 3)
        self.assertEqual(board[0]["status"], "supported")
        self.assertIn("Hypothesis board", final["state"]["report"])

    def test_board_claims_without_evidence_are_downgraded(self):
        class OverconfidentLLM(DemoLLM):
            def complete(self, system, user):
                if "TASK: decide_next_step" in user:
                    return json_reply({"action": "finish", "hypotheses": [
                        {"id": "H1", "statement": "it is DNS", "status": "supported", "evidence_ids": ["E42"]}]})
                return DemoLLM.complete(self, system, user)

        service = make_service(OverconfidentLLM())
        incident = service.trigger_scenario("bad_deploy_checkout")
        service.run(incident["id"], service.initial_input(incident))
        board = service.get(incident["id"])["state"]["hypotheses"]
        self.assertEqual(board[0]["status"], "open")
        self.assertEqual(board[0]["evidence_ids"], [])


class TestHumanApproval(unittest.TestCase):
    def test_graph_pauses_before_any_change(self):
        service = make_service()
        incident = service.trigger_scenario("bad_deploy_checkout")
        service.run(incident["id"], service.initial_input(incident))

        stored = service.get(incident["id"])
        self.assertEqual(stored["status"], "waiting_approval")
        self.assertEqual(stored["state"]["approval"]["status"], "pending")
        self.assertEqual(service.tools.actions, [])           # nothing was changed yet
        snapshot = service.graph.get_state(service.config_for(incident["id"]))
        self.assertIn("wait_for_approval", snapshot.next)     # LangGraph says: paused at this node

    def test_approve_resumes_and_changes_production(self):
        service = make_service()
        incident = service.trigger_scenario("bad_deploy_checkout")
        service.run(incident["id"], service.initial_input(incident))
        service.decide(incident["id"], True, "alice", "looks right")

        final = service.get(incident["id"])
        self.assertEqual(final["status"], "resolved")
        self.assertEqual(final["state"]["approval"]["reviewer"], "alice")
        self.assertEqual(len(service.tools.actions), 1)

    def test_reject_means_no_change(self):
        service = make_service()
        final = run_scenario(service, "bad_deploy_checkout", approve_no)
        self.assertEqual(final["status"], "escalated")
        self.assertEqual(service.tools.actions, [])
        self.assertEqual(final["state"]["approval"]["status"], "rejected")

    def test_cannot_decide_twice(self):
        service = make_service()
        incident = service.trigger_scenario("bad_deploy_checkout")
        service.run(incident["id"], service.initial_input(incident))
        service.decide(incident["id"], True, "alice")
        with self.assertRaises(ServiceError) as caught:
            service.decide(incident["id"], True, "bob")
        self.assertEqual(caught.exception.code, "conflict")

    def test_cannot_decide_an_unknown_incident(self):
        with self.assertRaises(ServiceError) as caught:
            make_service().decide("inc-nope", True, "alice")
        self.assertEqual(caught.exception.code, "not_found")

    def test_paused_incident_survives_a_restart(self):
        settings = make_settings()
        first = make_service(settings=settings)
        incident = first.trigger_scenario("bad_deploy_checkout")
        first.run(incident["id"], first.initial_input(incident))

        # A brand new service on the same database files = the server was restarted.
        second = make_service(settings=settings)
        second.decide(incident["id"], True, "alice")
        self.assertEqual(second.get(incident["id"])["status"], "resolved")


class TestSafety(unittest.TestCase):
    def test_change_freeze_blocks_the_fix(self):
        settings = make_settings()
        settings.change_freeze = True
        service = make_service(settings=settings)
        final = run_scenario(service, "bad_deploy_checkout")
        self.assertEqual(final["status"], "escalated")
        self.assertEqual(service.tools.actions, [])

    def test_fix_for_another_service_is_denied(self):
        plan = {"action": "restart_service", "service": "payments", "arguments": {}, "reason": "wrong service"}
        service = make_service(DemoLLM(plan_override=plan))
        final = run_scenario(service, "bad_deploy_checkout")
        self.assertEqual(final["state"]["policy"]["decision"], "deny")
        self.assertEqual(final["status"], "escalated")
        self.assertEqual(service.tools.actions, [])

    def test_freeze_that_starts_while_waiting_is_respected(self):
        settings = make_settings()
        service = make_service(settings=settings)
        incident = service.trigger_scenario("bad_deploy_checkout")
        service.run(incident["id"], service.initial_input(incident))
        settings.change_freeze = True                          # freeze starts while the human is thinking
        service.decide(incident["id"], True, "alice")
        final = service.get(incident["id"])
        self.assertEqual(final["status"], "escalated")
        self.assertEqual(service.tools.actions, [])


class TestVerificationLoop(unittest.TestCase):
    def test_wrong_fix_is_caught_and_retried_then_escalated(self):
        plan = {"action": "restart_service", "service": "checkout", "arguments": {}, "reason": "wrong on purpose"}
        service = make_service(DemoLLM(plan_override=plan))
        final = run_scenario(service, "bad_deploy_checkout")

        self.assertEqual(final["status"], "escalated")
        self.assertEqual(len(service.tools.actions), 2)         # MAX_FIX_ATTEMPTS = 2
        tools_used = []
        for evidence in final["state"]["evidence"]:
            tools_used.append(evidence["tool"])
        self.assertEqual(tools_used.count("verify_after_fix"), 2)

    def test_second_attempt_can_succeed(self):
        class WrongThenRightLLM(DemoLLM):
            def complete(self, system, user):
                if "TASK: plan_fix" in user:
                    if "Failed checks:" in user:          # only appears once a fix has really failed
                        return json_reply({"action": "rollback_deploy", "service": "checkout",
                                           "arguments": {"to_version": "2.13.4"}, "reason": "second try"})
                    return json_reply({"action": "restart_service", "service": "checkout",
                                       "arguments": {}, "reason": "first try"})
                return DemoLLM.complete(self, system, user)

        service = make_service(WrongThenRightLLM())
        final = run_scenario(service, "bad_deploy_checkout")
        self.assertEqual(final["status"], "resolved")
        self.assertEqual(final["state"]["attempts"], 2)
        executed = []
        for item in service.tools.actions:
            executed.append(item["action"])
        self.assertEqual(executed, ["restart_service", "rollback_deploy"])


class TestInvestigation(unittest.TestCase):
    def test_step_limit_is_respected(self):
        class NeverStopsLLM(DemoLLM):
            def complete(self, system, user):
                if "TASK: decide_next_step" in user:
                    return json_reply({"action": "call_tool", "tool": "get_service_status", "arguments": {}})
                return DemoLLM.complete(self, system, user)

        settings = make_settings()
        settings.max_steps = 3
        service = make_service(NeverStopsLLM(), settings=settings)
        incident = service.trigger_scenario("bad_deploy_checkout")
        service.run(incident["id"], service.initial_input(incident))
        self.assertEqual(len(service.get(incident["id"])["state"]["evidence"]), 3)

    def test_llm_cannot_read_another_service(self):
        class CuriousLLM(DemoLLM):
            def complete(self, system, user):
                if "TASK: decide_next_step" in user:
                    if '"id": "E1"' not in user:
                        return json_reply({"action": "call_tool", "tool": "get_service_status",
                                           "arguments": {"service": "payments"}})
                    return json_reply({"action": "finish"})
                return json_reply({"root_cause": "unknown", "inconclusive": True, "evidence_ids": []})

        service = make_service(CuriousLLM())
        incident = service.trigger_scenario("bad_deploy_checkout")
        service.run(incident["id"], service.initial_input(incident))
        first_evidence = service.get(incident["id"])["state"]["evidence"][0]
        self.assertEqual(first_evidence["arguments"]["service"], "checkout")

    def test_tool_error_becomes_failed_evidence_not_a_crash(self):
        class BadArgumentLLM(DemoLLM):
            def complete(self, system, user):
                if "TASK: decide_next_step" in user:
                    if '"id": "E1"' not in user:
                        return json_reply({"action": "call_tool", "tool": "search_logs", "arguments": {"level": "LOUD"}})
                    return json_reply({"action": "finish"})
                return json_reply({"root_cause": "unknown", "inconclusive": True, "evidence_ids": []})

        service = make_service(BadArgumentLLM())
        final = run_scenario(service, "bad_deploy_checkout")
        self.assertFalse(final["state"]["evidence"][0]["ok"])
        self.assertEqual(final["status"], "escalated")

    def test_knowledge_search_evidence_is_labelled(self):
        final = run_scenario(make_service(), "bad_deploy_checkout")
        kinds = {}
        for evidence in final["state"]["evidence"]:
            kinds[evidence["tool"]] = evidence["kind"]
        self.assertEqual(kinds["get_service_status"], "REPLAY")
        self.assertIn(kinds["search_runbooks"], ["RUNBOOK", "HISTORICAL"])


class TestCrashRecovery(unittest.TestCase):
    def test_crash_then_retry_continues_from_the_checkpoint(self):
        class CrashOnceLLM(DemoLLM):
            def __init__(self):
                DemoLLM.__init__(self)
                self.crashed = False

            def complete(self, system, user):
                if "TASK: write_rca" in user and not self.crashed:
                    self.crashed = True
                    raise RuntimeError("LLM API is down")
                return DemoLLM.complete(self, system, user)

        service = make_service(CrashOnceLLM())
        incident = service.trigger_scenario("bad_deploy_checkout")
        with self.assertLogs("incidentpilot.service", level="ERROR"):       # the crash is logged on purpose
            service.run(incident["id"], service.initial_input(incident))

        crashed = service.get(incident["id"])
        self.assertEqual(crashed["status"], "error")
        self.assertEqual(len(crashed["state"]["evidence"]), 5)       # the investigation work was kept

        service.retry(incident["id"])
        service.continue_from_checkpoint(incident["id"])
        self.assertEqual(service.get(incident["id"])["status"], "waiting_approval")
        service.decide(incident["id"], True, "alice")
        self.assertEqual(service.get(incident["id"])["status"], "resolved")

    def test_only_errored_incidents_can_be_retried(self):
        service = make_service()
        incident = service.trigger_scenario("bad_deploy_checkout")
        with self.assertRaises(ServiceError):
            service.retry(incident["id"])

    def test_running_incidents_are_marked_after_a_restart(self):
        service = make_service()
        incident = service.trigger_scenario("bad_deploy_checkout")     # status "running", never started
        self.assertEqual(service.mark_interrupted(), 1)
        self.assertEqual(service.get(incident["id"])["status"], "error")


class TestLearning(unittest.TestCase):
    def test_resolved_incident_becomes_searchable_knowledge(self):
        settings = make_settings()
        settings.learn_from_incidents = True
        service = make_service(settings=settings)
        final = run_scenario(service, "bad_deploy_checkout")

        path = os.path.join(settings.learned_dir, "learned-" + final["id"] + ".json")
        self.assertTrue(os.path.isfile(path))
        with open(path) as handle:
            self.assertIn("rollback_deploy", json.load(handle)["resolution"])
        found = []
        for result in service.retriever.search("KeyError discount_code checkout release", top_k=6):
            found.append(result["id"])
        self.assertIn("learned-" + final["id"], found)

    def test_escalated_incident_is_not_learned(self):
        settings = make_settings()
        settings.learn_from_incidents = True
        service = make_service(settings=settings)
        run_scenario(service, "insufficient_evidence_search")
        self.assertEqual(os.listdir(settings.learned_dir), [])

    def test_learning_can_be_switched_off(self):
        settings = make_settings()
        service = make_service(settings=settings)
        run_scenario(service, "bad_deploy_checkout")
        self.assertEqual(os.listdir(settings.learned_dir), [])


class TestStartingIncidents(unittest.TestCase):
    def test_unknown_scenario_is_not_found(self):
        with self.assertRaises(ServiceError) as caught:
            make_service().trigger_scenario("nope")
        self.assertEqual(caught.exception.code, "not_found")

    def test_bad_alert_is_rejected(self):
        with self.assertRaises(ServiceError) as caught:
            make_service().start_incident({"service": "checkout"})
        self.assertEqual(caught.exception.code, "bad_request")

    def test_two_incidents_on_one_service_do_not_share_the_fix(self):
        service = make_service()
        first = service.trigger_scenario("bad_deploy_checkout")
        service.run(first["id"], service.initial_input(first))
        second = service.trigger_scenario("bad_deploy_checkout")
        service.run(second["id"], service.initial_input(second))

        service.decide(first["id"], True, "alice")
        self.assertEqual(service.get(first["id"])["status"], "resolved")
        self.assertEqual(service.get(second["id"])["status"], "waiting_approval")
        status = service.tools.call("get_service_status", {"service": "checkout"}, second["id"])
        self.assertEqual(status["health"], "degraded")        # the first incident's fix does not leak

    def test_same_scenario_can_run_twice(self):
        service = make_service()
        first = run_scenario(service, "bad_deploy_checkout")
        second = run_scenario(service, "bad_deploy_checkout")
        self.assertEqual(first["status"], "resolved")
        self.assertEqual(second["status"], "resolved")
        self.assertEqual(len(service.list_incidents()), 2)


if __name__ == "__main__":
    unittest.main()
