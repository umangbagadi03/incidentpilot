import unittest

from incidentpilot.agent import IncidentAgent, parse_json
from tests.helpers import ScriptedLLM, json_reply, make_evidence

ALERT = {"service": "checkout", "title": "test alert"}


class TestParseJson(unittest.TestCase):
    def test_finds_json_inside_extra_text(self):
        self.assertEqual(parse_json('Sure! Here you go: {"a": 1} Hope that helps.'), {"a": 1})

    def test_no_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_json("I could not do it")


class TestDecisions(unittest.TestCase):
    def test_invalid_reply_is_retried_once(self):
        good = json_reply({"action": "call_tool", "tool": "get_metrics", "arguments": {}, "reason": "x"})
        llm = ScriptedLLM(["not json at all", good])
        decision = IncidentAgent(llm).decide_next_step(ALERT, [], 1, 6)
        self.assertEqual(decision.tool, "get_metrics")
        self.assertEqual(llm.calls, 2)

    def test_two_invalid_replies_fall_back_to_finish(self):
        decision = IncidentAgent(ScriptedLLM(["nope", "still nope"])).decide_next_step(ALERT, [], 1, 6)
        self.assertEqual(decision.action, "finish")

    def test_write_tool_is_not_accepted_for_investigation(self):
        bad = json_reply({"action": "call_tool", "tool": "restart_service", "arguments": {}})
        decision = IncidentAgent(ScriptedLLM([bad, bad])).decide_next_step(ALERT, [], 1, 6)
        self.assertEqual(decision.action, "finish")

    def test_call_tool_without_a_tool_is_not_accepted(self):
        bad = json_reply({"action": "call_tool"})
        decision = IncidentAgent(ScriptedLLM([bad, bad])).decide_next_step(ALERT, [], 1, 6)
        self.assertEqual(decision.action, "finish")


class TestRca(unittest.TestCase):
    def rca_reply(self, evidence_ids, inconclusive=False):
        return json_reply({"root_cause": "bad release", "evidence_ids": evidence_ids, "inconclusive": inconclusive})

    def test_fake_citations_are_removed(self):
        rca = IncidentAgent(ScriptedLLM([self.rca_reply(["E1", "E9"])])).write_rca(ALERT, [make_evidence("E1")])
        self.assertEqual(rca.evidence_ids, ["E1"])
        self.assertFalse(rca.inconclusive)

    def test_only_fake_citations_make_the_rca_inconclusive(self):
        rca = IncidentAgent(ScriptedLLM([self.rca_reply(["E7"])])).write_rca(ALERT, [make_evidence("E1")])
        self.assertTrue(rca.inconclusive)
        self.assertGreater(len(rca.missing_evidence), 0)

    def test_failed_tool_result_cannot_be_cited(self):
        rca = IncidentAgent(ScriptedLLM([self.rca_reply(["E1"])])).write_rca(ALERT, [make_evidence("E1", ok=False)])
        self.assertTrue(rca.inconclusive)

    def test_out_of_range_confidence_is_retried(self):
        bad = json_reply({"root_cause": "bad release", "evidence_ids": ["E1"], "confidence": 7})
        good = json_reply({"root_cause": "bad release", "evidence_ids": ["E1"], "confidence": 0.7})
        llm = ScriptedLLM([bad, good])
        rca = IncidentAgent(llm).write_rca(ALERT, [make_evidence("E1")])
        self.assertEqual(rca.confidence, 0.7)
        self.assertEqual(llm.calls, 2)

    def test_invalid_replies_give_inconclusive_rca(self):
        rca = IncidentAgent(ScriptedLLM(["bad", "bad"])).write_rca(ALERT, [make_evidence("E1")])
        self.assertTrue(rca.inconclusive)


class TestPlan(unittest.TestCase):
    def test_invalid_replies_give_escalate(self):
        plan = IncidentAgent(ScriptedLLM(["bad", "bad"])).plan_fix(ALERT, {}, [])
        self.assertEqual(plan.action, "escalate")

    def test_unknown_action_is_not_accepted(self):
        bad = json_reply({"action": "format_disk"})
        plan = IncidentAgent(ScriptedLLM([bad, bad])).plan_fix(ALERT, {}, [])
        self.assertEqual(plan.action, "escalate")

    def test_missing_service_is_filled_with_the_alert_service(self):
        good = json_reply({"action": "restart_service"})
        plan = IncidentAgent(ScriptedLLM([good])).plan_fix(ALERT, {}, [])
        self.assertEqual(plan.service, "checkout")

    def test_failed_fixes_are_shown_to_the_llm(self):
        seen = []

        class SpyLLM:
            def complete(self, system, user):
                seen.append(user)
                return json_reply({"action": "escalate"})

        failed = make_evidence("E5")
        failed["tool"] = "verify_after_fix"
        failed["summary"] = "UNIQUE-MARKER fix 'restart_service' did not work"
        IncidentAgent(SpyLLM()).plan_fix(ALERT, {}, [failed])
        self.assertIn("UNIQUE-MARKER", seen[0])

    def test_without_failed_fixes_the_prompt_says_none(self):
        seen = []

        class SpyLLM:
            def complete(self, system, user):
                seen.append(user)
                return json_reply({"action": "escalate"})

        IncidentAgent(SpyLLM()).plan_fix(ALERT, {}, [make_evidence("E1")])
        self.assertNotIn("UNIQUE-MARKER", seen[0])
        self.assertIn("(none)", seen[0])


if __name__ == "__main__":
    unittest.main()
