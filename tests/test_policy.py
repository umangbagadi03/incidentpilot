import unittest

from incidentpilot.policy import check_action
from tests.helpers import make_settings


def make_plan(action, service="checkout", arguments=None):
    if arguments is None:
        arguments = {}
    return {"action": action, "service": service, "arguments": arguments, "reason": "test"}


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()

    def test_normal_change_needs_approval(self):
        plan = make_plan("rollback_deploy", arguments={"to_version": "1.0"})
        self.assertEqual(check_action(plan, "checkout", self.settings)["decision"], "require_approval")

    def test_unknown_action_is_denied(self):
        self.assertEqual(check_action(make_plan("drop_database"), "checkout", self.settings)["decision"], "deny")

    def test_other_service_is_denied(self):
        result = check_action(make_plan("restart_service", service="payments"), "checkout", self.settings)
        self.assertEqual(result["decision"], "deny")

    def test_service_not_in_allow_list_is_denied(self):
        result = check_action(make_plan("restart_service", service="billing"), "billing", self.settings)
        self.assertEqual(result["decision"], "deny")

    def test_scale_too_high_is_denied(self):
        plan = make_plan("scale_service", arguments={"replicas": 50})
        self.assertEqual(check_action(plan, "checkout", self.settings)["decision"], "deny")

    def test_scale_with_text_is_denied(self):
        plan = make_plan("scale_service", arguments={"replicas": "many"})
        self.assertEqual(check_action(plan, "checkout", self.settings)["decision"], "deny")

    def test_scale_in_range_needs_approval(self):
        plan = make_plan("scale_service", arguments={"replicas": 5})
        self.assertEqual(check_action(plan, "checkout", self.settings)["decision"], "require_approval")

    def test_change_freeze_denies_everything(self):
        self.settings.change_freeze = True
        self.assertEqual(check_action(make_plan("restart_service"), "checkout", self.settings)["decision"], "deny")


if __name__ == "__main__":
    unittest.main()
