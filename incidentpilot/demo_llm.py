"""A scripted stand-in for the LLM, so the project runs with no API key.

It "replies" with answers that were written by hand for the recorded scenarios.
It keeps no state: it finds the scenario from the alert in the prompt, and the step number
from how many evidence items the prompt already contains.

IMPORTANT: this only proves that the plumbing works (graph, tools, grounding, guardrail, policy,
approval, verification). It says nothing about how good a real LLM is. For that, run evaluate.py
with a real API key.

Script format, per alerting service:
  steps:  one entry per evidence count. Entry i is used when the prompt holds i evidence items.
          {"tool": ..., "board": [...]} = call this tool, {"finish": True, "board": [...]} = stop.
          After the last entry the agent finishes.
  board:  the hypothesis board the agent sends back with that step.
  rca, plan:                       the answers for write_rca and plan_fix.
  rca_after_failure, plan_after_failure: used once a fix has failed (optional).
"""
import json
import re
import time


def h(hypothesis_id, statement, status="open", evidence_ids=None):
    """Short helper to write a hypothesis."""
    if evidence_ids is None:
        evidence_ids = []
    return {"id": hypothesis_id, "statement": statement, "status": status, "evidence_ids": evidence_ids}


# ---------- checkout: bad deploy ----------
C1 = "Release 2.14.0 introduced a code bug"
C2 = "A downstream dependency (database, payment provider) is failing"
C3 = "Resource exhaustion or a load spike (CPU, memory, traffic)"

CHECKOUT = {
    "steps": [
        {"tool": "get_service_status", "arguments": {"service": "checkout"},
         "hypothesis": "How bad is it, and which SLO is broken?",
         "reason": "Start from the current state of the service.",
         "board": [h("H1", C1), h("H2", C2), h("H3", C3)]},
        {"tool": "get_metrics", "arguments": {"service": "checkout"},
         "hypothesis": "H3: is CPU, memory or traffic away from its baseline?",
         "reason": "Only the error rate is broken. Check the resources to rule load in or out.",
         "board": [h("H1", C1), h("H2", C2), h("H3", C3)]},
        {"tool": "list_deploys", "arguments": {"service": "checkout"},
         "hypothesis": "H1: did a deploy happen right before the errors started?",
         "reason": "Resources are flat, so look for a change.",
         "board": [h("H1", C1), h("H2", C2), h("H3", C3, "refuted", ["E2"])]},
        {"tool": "search_logs", "arguments": {"service": "checkout", "level": "ERROR"},
         "hypothesis": "H1 vs H2: are the errors our own exceptions, or timeouts from a dependency?",
         "reason": "A release went out 13 minutes ago. The error messages will separate H1 from H2.",
         "board": [h("H1", C1, "open", ["E3"]), h("H2", C2), h("H3", C3, "refuted", ["E2"])]},
        {"tool": "search_runbooks", "arguments": {"query": "bad deployment release errors rollback"},
         "hypothesis": "H1: what is the known procedure for a bad release?",
         "reason": "The errors are a KeyError raised after release 2.14.0. Check the runbook.",
         "board": [h("H1", C1, "supported", ["E3", "E4"]), h("H2", C2, "refuted", ["E4"]),
                   h("H3", C3, "refuted", ["E2"])]},
        {"finish": True,
         "board": [h("H1", C1, "supported", ["E3", "E4", "E5"]), h("H2", C2, "refuted", ["E4"]),
                   h("H3", C3, "refuted", ["E2"])]},
    ],
    "rca": {
        "root_cause": "Release 2.14.0 (a refactor of the discount calculation) raises KeyError 'discount_code' "
                      "on checkout requests, which causes the HTTP 500 errors.",
        "category": "bad_deploy",
        "summary": "Checkout error rate is 18.41% (SLO 2%) since release 2.14.0 went out 13 minutes ago.",
        "confidence": 0.9,
        "claims": [
            {"text": "Release 2.14.0 was deployed 13 minutes ago.", "evidence_ids": ["E3"]},
            {"text": "Since then checkout raises KeyError 'discount_code' and returns HTTP 500.",
             "evidence_ids": ["E4"]},
            {"text": "The error rate is 18.41% against an SLO of 2%.", "evidence_ids": ["E1"]},
            {"text": "CPU, memory and request rate stay at baseline (ratio 0.98 to 1.05), so it is not load.",
             "evidence_ids": ["E2"]},
        ],
        "evidence_ids": ["E1", "E2", "E3", "E4"],
        "inconclusive": False,
        "missing_evidence": [],
    },
    "plan": {
        "action": "rollback_deploy",
        "service": "checkout",
        "arguments": {"to_version": "2.13.4"},
        "reason": "The runbook says to roll back a bad release. 2.13.4 is the last known good version.",
    },
}

# ---------- payments: memory leak ----------
P1 = "A memory leak pushes containers to the memory limit"
P2 = "A bad release"
P3 = "A traffic spike"

PAYMENTS = {
    "steps": [
        {"tool": "get_service_status", "arguments": {"service": "payments"},
         "hypothesis": "How unhealthy is payments right now?",
         "reason": "Start from the current state of the service.",
         "board": [h("H1", P1), h("H2", P2), h("H3", P3)]},
        {"tool": "get_metrics", "arguments": {"service": "payments"},
         "hypothesis": "H1 vs H3: is memory far above baseline while traffic is normal?",
         "reason": "The alert mentions memory. Compare memory and traffic with their baselines.",
         "board": [h("H1", P1, "open", ["E1"]), h("H2", P2), h("H3", P3)]},
        {"tool": "search_logs", "arguments": {"service": "payments", "level": "ERROR"},
         "hypothesis": "H1: are containers being OOMKilled?",
         "reason": "Memory is 2.85x baseline. Look for out of memory errors.",
         "board": [h("H1", P1, "supported", ["E1", "E2"]), h("H2", P2), h("H3", P3, "refuted", ["E2"])]},
        {"tool": "list_deploys", "arguments": {"service": "payments"},
         "hypothesis": "H2: did a release cause this?",
         "reason": "Rule out a bad release before choosing a fix.",
         "board": [h("H1", P1, "supported", ["E1", "E2", "E3"]), h("H2", P2), h("H3", P3, "refuted", ["E2"])]},
        {"tool": "search_runbooks", "arguments": {"query": "memory leak OOM killed restarts"},
         "hypothesis": "H1: what is the known procedure?",
         "reason": "Check the runbook for the usual fix.",
         "board": [h("H1", P1, "supported", ["E1", "E2", "E3"]), h("H2", P2, "refuted", ["E4"]),
                   h("H3", P3, "refuted", ["E2"])]},
        {"finish": True,
         "board": [h("H1", P1, "supported", ["E1", "E2", "E3", "E5"]), h("H2", P2, "refuted", ["E4"]),
                   h("H3", P3, "refuted", ["E2"])]},
    ],
    "rca": {
        "root_cause": "Memory in payments grows until the 2048Mi limit and the containers are OOMKilled and "
                      "restarted. This is a memory leak, and no recent deploy explains it.",
        "category": "memory_leak",
        "summary": "Payments memory is 2.85x its baseline, containers keep restarting, error rate is 3.3%.",
        "confidence": 0.85,
        "claims": [
            {"text": "Memory is 2050.29 MB, 2.85x its baseline of 719.87 MB.", "evidence_ids": ["E2"]},
            {"text": "Containers are OOMKilled after they exceed the 2048Mi memory limit.", "evidence_ids": ["E3"]},
            {"text": "The heap fills up in OrderCache.put (OutOfMemoryError).", "evidence_ids": ["E3"]},
            {"text": "There was no recent deploy, so a bad release does not explain it.", "evidence_ids": ["E4"]},
        ],
        "evidence_ids": ["E2", "E3", "E4"],
        "inconclusive": False,
        "missing_evidence": [],
    },
    "plan": {
        "action": "restart_service",
        "service": "payments",
        "arguments": {},
        "reason": "A restart frees the leaked memory. It is a temporary fix, the leak needs a code fix.",
    },
}

# ---------- search: not enough evidence ----------
S1 = "Cache misses on one shard slow some queries"
S2 = "A recent deploy made search slower"
S3 = "The service is overloaded"

SEARCH = {
    "steps": [
        {"tool": "get_service_status", "arguments": {"service": "search"},
         "hypothesis": "Is the service unhealthy at all?",
         "reason": "See the current state of the service.",
         "board": [h("H1", S1), h("H2", S2), h("H3", S3)]},
        {"tool": "get_metrics", "arguments": {"service": "search"},
         "hypothesis": "H3: did any metric move away from its baseline?",
         "reason": "Status is healthy. Check the trends.",
         "board": [h("H1", S1), h("H2", S2), h("H3", S3)]},
        {"tool": "list_deploys", "arguments": {"service": "search"},
         "hypothesis": "H2: was there a deploy?",
         "reason": "Metrics are flat. Check for a change.",
         "board": [h("H1", S1), h("H2", S2), h("H3", S3, "refuted", ["E1", "E2"])]},
        {"tool": "search_logs", "arguments": {"service": "search", "level": "WARN"},
         "hypothesis": "H1: do the warnings point at a shard?",
         "reason": "Look at the warnings.",
         "board": [h("H1", S1), h("H2", S2, "refuted", ["E3"]), h("H3", S3, "refuted", ["E1", "E2"])]},
        {"finish": True,
         "board": [h("H1", S1, "open", ["E4"]), h("H2", S2, "refuted", ["E3"]),
                   h("H3", S3, "refuted", ["E1", "E2"])]},
    ],
    "rca": {
        "root_cause": "No root cause can be established. The service is healthy, metrics are flat, there "
                      "was no deploy, and the few warnings are not enough to prove anything.",
        "category": "unknown",
        "summary": "Weak, intermittent signals only.",
        "confidence": 0.2,
        "claims": [
            {"text": "Status is healthy and every metric is within 1.14x of its baseline.", "evidence_ids": ["E1", "E2"]},
            {"text": "There was no recent deploy.", "evidence_ids": ["E3"]},
        ],
        "evidence_ids": [],
        "inconclusive": True,
        "missing_evidence": ["slow request traces", "per-shard latency data"],
    },
    "plan": {
        "action": "escalate",
        "service": "search",
        "arguments": {},
        "reason": "Nothing to fix safely. A human should look into it.",
    },
}

# ---------- inventory: database pool exhaustion, the deploy is a red herring ----------
I1 = "Release 3.8.2 introduced a regression"
I2 = "The database connection pool is exhausted"
I3 = "A traffic spike overloads the service"

INVENTORY = {
    "steps": [
        {"tool": "get_service_status", "arguments": {"service": "inventory"},
         "hypothesis": "Which SLO is broken, and how badly?",
         "reason": "Start from the current state of the service.",
         "board": [h("H1", I1), h("H2", I2), h("H3", I3)]},
        {"tool": "list_deploys", "arguments": {"service": "inventory"},
         "hypothesis": "H1: the alert mentions a release today. What did it change?",
         "reason": "Latency is 2350ms with low CPU. Check the release the alert mentions.",
         "board": [h("H1", I1), h("H2", I2), h("H3", I3)]},
        {"tool": "search_logs", "arguments": {"service": "inventory", "level": "ERROR"},
         "hypothesis": "H1 vs H2: are the errors code exceptions or connection timeouts?",
         "reason": "The release only changed docs. The errors will tell us what really fails.",
         "board": [h("H1", I1, "open", ["E2"]), h("H2", I2), h("H3", I3)]},
        {"tool": "get_metrics", "arguments": {"service": "inventory"},
         "hypothesis": "H2 vs H3: is the pool full because of more traffic?",
         "reason": "Logs show PoolTimeout. Check pool usage and request rate against baseline.",
         "board": [h("H1", I1, "refuted", ["E2", "E3"]), h("H2", I2, "supported", ["E3"]), h("H3", I3)]},
        {"tool": "search_runbooks", "arguments": {"query": "database connection pool exhaustion PoolTimeout slow third-party"},
         "hypothesis": "H2: what is the known procedure?",
         "reason": "Traffic is normal and the pool is full. Check the runbook.",
         "board": [h("H1", I1, "refuted", ["E2", "E3"]), h("H2", I2, "supported", ["E3", "E4"]),
                   h("H3", I3, "refuted", ["E4"])]},
        {"finish": True,
         "board": [h("H1", I1, "refuted", ["E2", "E3"]), h("H2", I2, "supported", ["E3", "E4", "E5"]),
                   h("H3", I3, "refuted", ["E4"])]},
    ],
    "rca": {
        "root_cause": "The database connection pool is exhausted: all 20 connections are in use, so stock lookups "
                      "wait for a connection and fail with PoolTimeout. Release 3.8.2 only changed docs and is not the cause.",
        "category": "db_pool_exhaustion",
        "summary": "Inventory p95 latency is 2350ms (SLO 800ms) and 6.2% of requests fail.",
        "confidence": 0.85,
        "claims": [
            {"text": "All 20 pool connections are in use (baseline 7.2) and requests fail with PoolTimeout after 30s.",
             "evidence_ids": ["E3", "E4"]},
            {"text": "The supplier-api takes about 28400ms to answer, so each request holds a connection.",
             "evidence_ids": ["E3"]},
            {"text": "Release 3.8.2 only changed docs (README and OpenAPI descriptions).", "evidence_ids": ["E2"]},
            {"text": "Request rate is 1.03x baseline, so this is not a traffic spike.", "evidence_ids": ["E4"]},
        ],
        "evidence_ids": ["E2", "E3", "E4"],
        "inconclusive": False,
        "missing_evidence": [],
    },
    "plan": {
        "action": "restart_service",
        "service": "inventory",
        "arguments": {},
        "reason": "The runbook says a restart releases the hung connections. A rollback would not help: the "
                  "release only changed docs. Follow up: add a shorter timeout on supplier-api calls.",
    },
}

# ---------- orders: traffic spike. The first fix (restart) fails, verification catches it ----------
O1 = "Flash sale traffic overloads the 3 replicas"
O2 = "A bad release"
O3 = "Workers are stuck (a deadlock or a hung dependency)"

ORDERS = {
    "steps": [
        {"tool": "get_service_status", "arguments": {"service": "orders"},
         "hypothesis": "How bad is it?",
         "reason": "Start from the current state of the service.",
         "board": [h("H1", O1), h("H2", O2), h("H3", O3)]},
        {"tool": "get_metrics", "arguments": {"service": "orders"},
         "hypothesis": "H1: is traffic far above baseline?",
         "reason": "CPU is 96.2%. Compare traffic and CPU with their baselines.",
         "board": [h("H1", O1, "open", ["E1"]), h("H2", O2), h("H3", O3)]},
        {"tool": "list_deploys", "arguments": {"service": "orders"},
         "hypothesis": "H2: was there a release?",
         "reason": "Rule out a bad release.",
         "board": [h("H1", O1, "supported", ["E1", "E2"]), h("H2", O2), h("H3", O3)]},
        {"tool": "search_logs", "arguments": {"service": "orders", "level": "WARN"},
         "hypothesis": "H3: do the logs show stuck workers?",
         "reason": "No deploy. Check the warnings for worker problems.",
         "board": [h("H1", O1, "supported", ["E1", "E2"]), h("H2", O2, "refuted", ["E3"]), h("H3", O3)]},
        {"finish": True,
         "board": [h("H1", O1, "supported", ["E1", "E2"]), h("H2", O2, "refuted", ["E3"]),
                   h("H3", O3, "open", ["E4"])]},
        # Only reached after the first fix failed (E5 = verify_after_fix):
        {"tool": "search_runbooks", "arguments": {"query": "traffic spike CPU saturation scale out flash sale"},
         "hypothesis": "H1: the restart did not help, so what does the runbook say about real load?",
         "reason": "The restart failed (E5), so H3 is refuted. Look up the procedure for real load.",
         "board": [h("H1", O1, "supported", ["E1", "E2"]), h("H2", O2, "refuted", ["E3"]),
                   h("H3", O3, "refuted", ["E5"])]},
        {"finish": True,
         "board": [h("H1", O1, "supported", ["E1", "E2", "E6"]), h("H2", O2, "refuted", ["E3"]),
                   h("H3", O3, "refuted", ["E5"])]},
    ],
    "rca": {
        "root_cause": "The flash sale tripled traffic and the 3 replicas are saturated: CPU is at 96.2% and the "
                      "worker pool is full, so requests queue and time out.",
        "category": "traffic_spike",
        "summary": "Orders p95 latency is 1650ms (SLO 600ms) and 4.1% of requests fail during the flash sale.",
        "confidence": 0.8,
        "claims": [
            {"text": "Request rate is 410.5 per second, 3.42x the baseline of 120.", "evidence_ids": ["E2"]},
            {"text": "CPU is at 96.2% on 3 replicas and p95 latency is 1650ms against an SLO of 600ms.",
             "evidence_ids": ["E1"]},
            {"text": "Logs show the worker pool saturated at max_workers=64.", "evidence_ids": ["E4"]},
            {"text": "There was no recent deploy.", "evidence_ids": ["E3"]},
        ],
        "evidence_ids": ["E1", "E2", "E3", "E4"],
        "inconclusive": False,
        "missing_evidence": [],
    },
    "plan": {
        "action": "restart_service",
        "service": "orders",
        "arguments": {},
        "reason": "The worker pool is saturated and some workers may be stuck. A rolling restart is the "
                  "lowest-risk first step.",
    },
    "rca_after_failure": {
        "root_cause": "Real load from the flash sale: traffic is 3.42x baseline and 3 replicas cannot serve it. "
                      "The restart did not help, which rules out stuck workers.",
        "category": "traffic_spike",
        "summary": "Orders is under-provisioned for the flash sale traffic.",
        "confidence": 0.9,
        "claims": [
            {"text": "Request rate is 410.5 per second, 3.42x the baseline of 120.", "evidence_ids": ["E2"]},
            {"text": "CPU is at 96.2% on 3 replicas.", "evidence_ids": ["E1"]},
            {"text": "A restart did not recover the service, so the workers were not stuck.", "evidence_ids": ["E5"]},
            {"text": "The runbook says a restart does not help with real load and to scale out instead.",
             "evidence_ids": ["E6"]},
        ],
        "evidence_ids": ["E1", "E2", "E5", "E6"],
        "inconclusive": False,
        "missing_evidence": [],
    },
    "plan_after_failure": {
        "action": "scale_service",
        "service": "orders",
        "arguments": {"replicas": 10},
        "reason": "Runbook: target = replicas x traffic ratio = 3 x 3.42 = 10.3, capped at the limit of 10.",
    },
}

# ---------- notifications: bad config release, and a prompt injection in the logs ----------
N1 = "Release 5.2.0 broke the SMTP configuration"
N2 = "The external SMTP provider is down"
N3 = "Bad data in one customer's order breaks the email template"

NOTIFICATIONS = {
    "steps": [
        {"tool": "get_service_status", "arguments": {"service": "notifications"},
         "hypothesis": "How many emails are failing?",
         "reason": "Start from the current state of the service.",
         "board": [h("H1", N1), h("H2", N2), h("H3", N3)]},
        {"tool": "list_deploys", "arguments": {"service": "notifications"},
         "hypothesis": "H1: was there a release right before the errors?",
         "reason": "The error rate is 31.5%. Check for a recent change.",
         "board": [h("H1", N1), h("H2", N2), h("H3", N3)]},
        {"tool": "search_logs", "arguments": {"service": "notifications", "level": "ERROR"},
         "hypothesis": "H1 vs H2: is it a config error or a connection error to the provider?",
         "reason": "Release 5.2.0 moved the SMTP settings 9 minutes ago. Read the errors.",
         "board": [h("H1", N1, "open", ["E2"]), h("H2", N2), h("H3", N3)]},
        {"tool": "search_logs", "arguments": {"service": "notifications", "level": "WARN", "query": "warning"},
         "hypothesis": "H3: are there template warnings for specific orders?",
         "reason": "Check the warnings to rule out bad customer data.",
         "board": [h("H1", N1, "supported", ["E2", "E3"]), h("H2", N2, "refuted", ["E3"]), h("H3", N3)]},
        {"tool": "search_runbooks", "arguments": {"query": "bad release missing config environment variable rollback"},
         "hypothesis": "H1: what is the known procedure?",
         "reason": "Every send fails with the same config error, not only some orders. Check the runbook.",
         "board": [h("H1", N1, "supported", ["E2", "E3"]), h("H2", N2, "refuted", ["E3"]),
                   h("H3", N3, "refuted", ["E3"])]},
        {"finish": True,
         "board": [h("H1", N1, "supported", ["E2", "E3", "E5"]), h("H2", N2, "refuted", ["E3"]),
                   h("H3", N3, "refuted", ["E3"])]},
    ],
    "rca": {
        "root_cause": "Release 5.2.0 moved the SMTP settings to a new config service and SMTP_HOST is not set, "
                      "so every email send fails with SMTPConfigError.",
        "category": "bad_deploy",
        "summary": "31.5% of notification jobs fail since release 5.2.0. Order confirmation emails are not sent.",
        "confidence": 0.9,
        "claims": [
            {"text": "Release 5.2.0 was deployed 9 minutes ago and moved the SMTP settings to a new config service.",
             "evidence_ids": ["E2"]},
            {"text": "Since then sends fail with SMTPConfigError: SMTP_HOST is not set.", "evidence_ids": ["E3"]},
            {"text": "The error rate is 31.5% against an SLO of 2%.", "evidence_ids": ["E1"]},
        ],
        "evidence_ids": ["E1", "E2", "E3"],
        "inconclusive": False,
        "missing_evidence": [],
    },
    "plan": {
        "action": "rollback_deploy",
        "service": "notifications",
        "arguments": {"to_version": "5.1.3"},
        "reason": "Roll back to 5.1.3, the last version with working SMTP settings, then fix the config.",
    },
}

SCRIPTS = {
    "checkout": CHECKOUT,
    "payments": PAYMENTS,
    "search": SEARCH,
    "inventory": INVENTORY,
    "orders": ORDERS,
    "notifications": NOTIFICATIONS,
}


class DemoLLM:
    label = "Scripted (offline)"
    scripted = True          # never calls a real LLM

    def __init__(self, plan_override=None, delay_seconds=0.0):
        # plan_override lets a test force a specific (for example wrong) fix.
        self.plan_override = plan_override
        # The UI demo waits a little per "LLM call", so you can watch the steps arrive. Tests use 0.
        self.delay_seconds = delay_seconds

    def find_script(self, prompt):
        """Read the alerting service from the 'Alert:' line of the prompt."""
        match = re.search(r"Alert:\n(\{.*\})", prompt)
        if match:
            service = json.loads(match.group(1)).get("service", "")
            if service in SCRIPTS:
                return SCRIPTS[service]
        raise ValueError("DemoLLM has no script for this alert")

    def complete(self, system, user):
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        script = self.find_script(user)
        a_fix_failed = '"tool": "verify_after_fix"' in user      # evidence from a failed fix

        if "TASK: decide_next_step" in user:
            evidence_done = len(re.findall(r'"id": "E\d+"', user))
            steps = script["steps"]
            if evidence_done >= len(steps):
                return json.dumps({"action": "finish", "reason": "I have enough evidence."})
            step = steps[evidence_done]
            if step.get("finish"):
                return json.dumps({"action": "finish", "hypotheses": step["board"],
                                   "reason": "One hypothesis is supported by the evidence, the others are refuted."})
            return json.dumps({
                "action": "call_tool",
                "tool": step["tool"],
                "arguments": step["arguments"],
                "hypotheses": step["board"],
                "hypothesis": step["hypothesis"],
                "reason": step["reason"],
            })

        if "TASK: write_rca" in user:
            if a_fix_failed and "rca_after_failure" in script:
                return json.dumps(script["rca_after_failure"])
            return json.dumps(script["rca"])

        if "TASK: plan_fix" in user:
            if self.plan_override is not None:
                return json.dumps(self.plan_override)
            if a_fix_failed and "plan_after_failure" in script:
                return json.dumps(script["plan_after_failure"])
            return json.dumps(script["plan"])

        raise ValueError("DemoLLM does not know this task")
