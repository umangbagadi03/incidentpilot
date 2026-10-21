import typing
import unittest

from incidentpilot.schemas import READ_TOOLS, WRITE_TOOLS, RCA, Alert, Decision, Plan


class TestSchemas(unittest.TestCase):
    def test_valid_decision(self):
        decision = Decision.model_validate({"action": "call_tool", "tool": "get_metrics"})
        self.assertEqual(decision.tool, "get_metrics")
        self.assertEqual(decision.arguments, {})

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            Decision.model_validate({"action": "call_tool", "tool": "delete_database"})

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            Decision.model_validate({"action": "dance"})

    def test_confidence_must_be_between_0_and_1(self):
        with self.assertRaises(ValueError):
            RCA.model_validate({"root_cause": "x", "confidence": 7})

    def test_rca_needs_a_root_cause(self):
        with self.assertRaises(ValueError):
            RCA.model_validate({"evidence_ids": ["E1"]})

    def test_plan_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            Plan.model_validate({"action": "format_disk"})

    def test_alert_needs_service_and_title(self):
        with self.assertRaises(ValueError):
            Alert.model_validate({"service": "checkout"})

    def test_tool_lists_match_the_literal_types(self):
        decision_tools = list(typing.get_args(typing.get_type_hints(Decision)["tool"]))
        decision_tools.remove("")
        self.assertEqual(decision_tools, READ_TOOLS)
        plan_actions = list(typing.get_args(typing.get_type_hints(Plan)["action"]))
        self.assertEqual(plan_actions, WRITE_TOOLS + ["escalate"])


if __name__ == "__main__":
    unittest.main()
