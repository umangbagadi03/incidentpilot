import os
import tempfile
import unittest

from incidentpilot.config import Settings
from incidentpilot.tools import ScenarioTools, ToolError


class TestTools(unittest.TestCase):
    def setUp(self):
        self.tools = ScenarioTools(Settings().scenarios_dir)

    def test_lists_all_scenarios(self):
        self.assertEqual(len(self.tools.list_scenarios()), 6)

    def test_status_of_broken_service(self):
        status = self.tools.call("get_service_status", {"service": "checkout"})
        self.assertEqual(status["health"], "degraded")

    def test_unknown_service_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            self.tools.call("get_service_status", {"service": "nope"})

    def test_unknown_tool_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            self.tools.call("delete_everything", {})

    def test_bad_argument_name_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            self.tools.call("get_service_status", {"service": "checkout", "minutes": 5})

    def test_bad_log_level_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            self.tools.call("search_logs", {"service": "checkout", "level": "LOUD"})

    def test_log_level_filter(self):
        errors = self.tools.call("search_logs", {"service": "checkout", "level": "ERROR"})
        warnings = self.tools.call("search_logs", {"service": "checkout", "level": "WARN"})
        self.assertGreater(errors["total_lines"], 0)
        self.assertGreater(warnings["total_lines"], errors["total_lines"])

    def test_log_query_filter(self):
        result = self.tools.call("search_logs", {"service": "checkout", "level": "ERROR", "query": "keyerror"})
        self.assertGreater(result["total_lines"], 0)
        for sample in result["samples"]:
            self.assertIn("keyerror", sample["message"].lower())

    def test_right_fix_makes_service_healthy(self):
        self.tools.call("rollback_deploy", {"service": "checkout", "to_version": "2.13.4"})
        status = self.tools.call("get_service_status", {"service": "checkout"})
        self.assertEqual(status["health"], "healthy")

    def test_wrong_fix_changes_nothing(self):
        self.tools.call("restart_service", {"service": "checkout"})
        status = self.tools.call("get_service_status", {"service": "checkout"})
        self.assertEqual(status["health"], "degraded")

    def test_rollback_to_unknown_version_fails(self):
        with self.assertRaises(ToolError):
            self.tools.call("rollback_deploy", {"service": "checkout", "to_version": "9.9.9"})

    def test_reset_breaks_it_again(self):
        self.tools.call("rollback_deploy", {"service": "checkout", "to_version": "2.13.4"})
        self.tools.reset()
        status = self.tools.call("get_service_status", {"service": "checkout"})
        self.assertEqual(status["health"], "degraded")

    def test_a_fix_belongs_to_one_incident_only(self):
        self.tools.call("rollback_deploy", {"service": "checkout", "to_version": "2.13.4"}, "inc-a")
        self.assertEqual(self.tools.call("get_service_status", {"service": "checkout"}, "inc-a")["health"], "healthy")
        self.assertEqual(self.tools.call("get_service_status", {"service": "checkout"}, "inc-b")["health"], "degraded")

    def test_the_llm_cannot_choose_the_incident(self):
        self.tools.call("rollback_deploy", {"service": "checkout", "to_version": "2.13.4"}, "inc-a")
        status = self.tools.call("get_service_status", {"service": "checkout", "incident_id": "inc-a"}, "inc-b")
        self.assertEqual(status["health"], "degraded")

    def test_fix_survives_a_restart(self):
        path = os.path.join(tempfile.mkdtemp(prefix="ip_replay_"), "state.db")
        first = ScenarioTools(Settings().scenarios_dir, state_path=path)
        first.call("restart_service", {"service": "payments"}, "inc-1")
        second = ScenarioTools(Settings().scenarios_dir, state_path=path)      # a new process, same file
        self.assertEqual(second.call("get_service_status", {"service": "payments"}, "inc-1")["health"], "healthy")

    def test_executed_actions_are_recorded(self):
        self.tools.call("restart_service", {"service": "payments"})
        self.assertEqual(self.tools.actions[0]["action"], "restart_service")


if __name__ == "__main__":
    unittest.main()
