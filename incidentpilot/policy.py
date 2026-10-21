"""Safety rules. Plain Python, no LLM.

The LLM can only PROPOSE a fix. This file decides if the proposal is allowed.
Even an allowed fix still needs a human to approve it.
"""
from incidentpilot.schemas import WRITE_TOOLS


def deny(reason):
    return {"decision": "deny", "reason": reason}


def check_action(plan, alert_service, settings):
    """Return {"decision": "require_approval" or "deny", "reason": "..."}. `plan` is a dict."""
    action = plan["action"]
    service = plan.get("service", "")
    arguments = plan.get("arguments", {})

    if action not in WRITE_TOOLS:
        return deny("'" + str(action) + "' is not an allowed action")

    if settings.change_freeze:
        return deny("change freeze is on, no changes are allowed")

    # The agent may only touch the service that raised the alert (limits the damage of a wrong answer).
    if service != alert_service:
        return deny("the agent may only change the alerting service '" + alert_service + "', not '" + str(service) + "'")

    if service not in settings.allowed_services:
        return deny("service '" + service + "' is not in the allowed services list")

    if action == "scale_service":
        try:
            replicas = int(arguments.get("replicas", 0))
        except (TypeError, ValueError):
            return deny("replicas must be a number")
        if replicas < 1 or replicas > settings.max_replicas:
            return deny("replicas must be between 1 and " + str(settings.max_replicas))

    return {"decision": "require_approval", "reason": "every change to production needs human approval"}
