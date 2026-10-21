"""The incident workflow, as a LangGraph graph.

    investigate (loop) -> analyze -> plan_fix -> policy_check -> request_approval -> wait_for_approval
        -> execute -> verify -> remember -> resolve        (any problem on the way -> escalate)
        verify fails and attempts are left -> back to investigate

Each node is a small method: it reads the state (a dict) and returns ONLY the keys it changes.
Each router method (after_...) looks at the state and returns the name of the next node.
Read build() first: it is the map of the whole project.
"""
import json
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from incidentpilot.grounding import clean_hypotheses
from incidentpilot.guardrails import clean_tool_output
from incidentpilot.metrics import APPROVALS, GUARDRAIL_HITS, POLICY_DECISIONS, TOOL_CALLS
from incidentpilot.policy import check_action
from incidentpilot.rag import incident_to_document, save_postmortem
from incidentpilot.report import build_report
from incidentpilot.schemas import READ_TOOLS, Evidence
from incidentpilot.tools import ToolError, summarize


class IncidentState(TypedDict, total=False):
    """Everything we know about one incident. LangGraph saves it after every node (a checkpoint).

    Note: a state key must not have the same name as a node, so the nodes are called
    analyze, plan_fix, policy_check... and the keys rca, plan, policy...
    """
    incident_id: str
    scenario: str
    alert: dict
    status: str                 # running, waiting_approval, resolved, escalated
    evidence: list              # list of Evidence dicts: E1, E2, ...
    hypotheses: list            # the hypothesis board: H1, H2, ... (supported / refuted / open)
    llm: str                    # which model works on this incident (a name only, never a key)
    steps: int                  # tool calls in the current investigation
    done_investigating: bool
    rca: dict
    plan: dict
    policy: dict
    approval: dict
    action_result: dict
    verification: dict
    attempts: int               # fixes executed so far
    escalation_reason: str
    timeline: list
    report: str


def new_state(incident_id, alert, scenario):
    """The input that starts a new run of the graph."""
    return {
        "incident_id": incident_id,
        "scenario": scenario,
        "alert": alert,
        "status": "running",
        "evidence": [],
        "hypotheses": [],
        "steps": 0,
        "done_investigating": False,
        "attempts": 0,
        "escalation_reason": "",
        "timeline": [],
    }


def check_recovered(status):
    """Recovered = healthy AND error rate and latency inside the SLO. Read from the service status, never from the LLM."""
    current = status["current"]
    slo = status["slo"]

    checks = []
    checks.append({
        "name": "health is healthy",
        "ok": status["health"] == "healthy",
        "detail": "health=" + status["health"],
    })
    checks.append({
        "name": "error rate inside SLO",
        "ok": current["error_rate"] <= slo["error_rate_max"],
        "detail": str(current["error_rate"]) + "% (max " + str(slo["error_rate_max"]) + "%)",
    })
    checks.append({
        "name": "latency p95 inside SLO",
        "ok": current["latency_p95"] <= slo["latency_p95_max"],
        "detail": str(current["latency_p95"]) + "ms (max " + str(slo["latency_p95_max"]) + "ms)",
    })

    healthy = True
    for check in checks:
        if not check["ok"]:
            healthy = False
    return {"healthy": healthy, "checks": checks}


class IncidentWorkflow:
    def __init__(self, tools, retriever, agent, settings, verbose=False):
        self.tools = tools
        self.retriever = retriever
        self.agent = agent
        self.settings = settings
        self.verbose = verbose        # print every event (used by the command line)

    # ---------- the map of the project ----------

    def build(self, checkpointer):
        graph = StateGraph(IncidentState)

        graph.add_node("investigate", self.investigate)
        graph.add_node("analyze", self.analyze)
        graph.add_node("plan_fix", self.plan_fix)
        graph.add_node("policy_check", self.policy_check)
        graph.add_node("request_approval", self.request_approval)
        graph.add_node("wait_for_approval", self.wait_for_approval)
        graph.add_node("execute", self.execute)
        graph.add_node("verify", self.verify)
        graph.add_node("remember", self.remember)
        graph.add_node("resolve", self.resolve)
        graph.add_node("escalate", self.escalate)

        graph.add_edge(START, "investigate")
        graph.add_conditional_edges("investigate", self.after_investigate,
                                    {"investigate": "investigate", "analyze": "analyze"})
        graph.add_conditional_edges("analyze", self.after_analyze,
                                    {"plan_fix": "plan_fix", "escalate": "escalate"})
        graph.add_conditional_edges("plan_fix", self.after_plan,
                                    {"policy_check": "policy_check", "escalate": "escalate"})
        graph.add_conditional_edges("policy_check", self.after_policy,
                                    {"request_approval": "request_approval", "escalate": "escalate"})
        graph.add_edge("request_approval", "wait_for_approval")
        graph.add_conditional_edges("wait_for_approval", self.after_approval,
                                    {"execute": "execute", "escalate": "escalate"})
        graph.add_conditional_edges("execute", self.after_execute,
                                    {"verify": "verify", "escalate": "escalate"})
        graph.add_conditional_edges("verify", self.after_verify,
                                    {"remember": "remember", "investigate": "investigate", "escalate": "escalate"})
        graph.add_edge("remember", "resolve")
        graph.add_edge("resolve", END)
        graph.add_edge("escalate", END)

        return graph.compile(checkpointer=checkpointer)

    # ---------- routers: which node comes next? ----------

    def after_investigate(self, state):
        if state["done_investigating"]:
            return "analyze"
        return "investigate"

    def after_analyze(self, state):
        if state["rca"]["inconclusive"]:
            return "escalate"
        return "plan_fix"

    def after_plan(self, state):
        if state["plan"]["action"] == "escalate":
            return "escalate"
        return "policy_check"

    def after_policy(self, state):
        if state["policy"]["decision"] == "deny":
            return "escalate"
        return "request_approval"

    def after_approval(self, state):
        if state["approval"]["status"] == "approved":
            return "execute"
        return "escalate"

    def after_execute(self, state):
        if state["action_result"]["ok"]:
            return "verify"
        return "escalate"

    def after_verify(self, state):
        if state["verification"]["healthy"]:
            return "remember"
        if state["attempts"] < self.settings.max_fix_attempts:
            return "investigate"      # the fix did not work: look again, with the new evidence
        return "escalate"

    # ---------- nodes ----------

    def investigate(self, state):
        """One tool call per run of this node. The graph loops back here, so every step is a checkpoint."""
        max_steps = self.settings.max_steps
        step = state["steps"] + 1
        timeline = state["timeline"]

        hypotheses = state.get("hypotheses", [])
        incident_id = state["incident_id"]
        decision = self.agent.decide_next_step(state["alert"], state["evidence"], step, max_steps, hypotheses, incident_id)

        # The board the LLM sent back is checked by code: no "supported" without real evidence.
        if len(decision.hypotheses) > 0:
            board = []
            for hypothesis in decision.hypotheses:
                board.append(hypothesis.model_dump())
            hypotheses = clean_hypotheses(board, state["evidence"])

        if decision.action == "finish":
            timeline = self.add_event(timeline, "investigate", "agent stops investigating: " + decision.reason)
            return {"done_investigating": True, "hypotheses": hypotheses, "timeline": timeline,
                    "llm": self.agent.label_for(incident_id)}

        message = "step " + str(step) + ": " + decision.tool
        if decision.hypothesis != "":
            message = message + " - testing: " + decision.hypothesis
        timeline = self.add_event(timeline, "investigate", message)

        evidence = self.collect_evidence(state, step, decision.tool, decision.arguments)
        timeline = self.add_event(timeline, "evidence", evidence["id"] + " " + evidence["summary"])
        if "prompt_injection" in evidence["flags"]:
            timeline = self.add_event(timeline, "guardrail", "quarantined " + str(len(evidence["quarantined"]))
                                      + " instruction-like text(s) in " + evidence["id"] + " before the LLM could read it")

        done = step >= max_steps
        if done:
            timeline = self.add_event(timeline, "investigate", "step limit (" + str(max_steps) + ") reached")
        return {
            "steps": step,
            "evidence": state["evidence"] + [evidence],
            "hypotheses": hypotheses,
            "done_investigating": done,
            "timeline": timeline,
            "llm": self.agent.label_for(incident_id),
        }

    def collect_evidence(self, state, step, tool_name, arguments):
        """Run one read-only tool and store the result as evidence E1, E2, ..."""
        evidence_id = "E" + str(len(state["evidence"]) + 1)
        arguments = dict(arguments)
        evidence = Evidence(id=evidence_id, step=step, tool=tool_name, arguments=arguments)

        outcome = "ok"
        try:
            if tool_name not in READ_TOOLS:
                raise ToolError("tool not allowed during investigation: " + tool_name)
            if tool_name == "search_runbooks":
                results = self.retriever.search(str(arguments.get("query", "")))
                evidence.data = {"results": results}
                evidence.kind = "RUNBOOK"
                if len(results) > 0 and results[0]["type"] == "past_incident":
                    evidence.kind = "HISTORICAL"
            else:
                # The LLM can only look at the service that raised the alert.
                arguments["service"] = state["alert"]["service"]
                evidence.arguments = arguments
                evidence.data = self.tools.call(tool_name, arguments, state["incident_id"])

            # GUARDRAIL: instruction-like text in tool output is removed before the LLM sees it.
            clean_data, removed = clean_tool_output(evidence.data)
            if len(removed) > 0:
                evidence.data = clean_data
                evidence.flags = ["prompt_injection"]
                evidence.quarantined = removed
                GUARDRAIL_HITS.inc()
            evidence.summary = summarize(tool_name, evidence.data)
        except ToolError as error:
            evidence.ok = False
            evidence.summary = "tool error: " + str(error)
            outcome = "error"

        TOOL_CALLS.labels(tool=tool_name, outcome=outcome).inc()
        return evidence.model_dump()

    def analyze(self, state):
        rca = self.agent.write_rca(state["alert"], state["evidence"], state.get("hypotheses", []),
                                   state["incident_id"]).model_dump()
        verified = 0
        for claim in rca["claims"]:
            if claim["verified"]:
                verified = verified + 1
        grounding = str(verified) + "/" + str(len(rca["claims"])) + " claims verified"
        if rca["inconclusive"]:
            message = "INCONCLUSIVE - " + rca["root_cause"] + " (" + grounding + ")"
        else:
            message = rca["root_cause"] + " [cites " + ", ".join(rca["evidence_ids"]) + "; " + grounding + "]"
        timeline = self.add_event(state["timeline"], "rca", message)
        return {"rca": rca, "timeline": timeline}

    def plan_fix(self, state):
        plan = self.agent.plan_fix(state["alert"], state["rca"], state["evidence"], state["incident_id"]).model_dump()
        message = plan["action"] + " " + str(plan["arguments"]) + " - " + plan["reason"]
        timeline = self.add_event(state["timeline"], "plan", message)
        return {"plan": plan, "timeline": timeline}

    def policy_check(self, state):
        policy = check_action(state["plan"], state["alert"]["service"], self.settings)
        POLICY_DECISIONS.labels(decision=policy["decision"]).inc()
        timeline = self.add_event(state["timeline"], "policy", policy["decision"] + " - " + policy["reason"])
        return {"policy": policy, "timeline": timeline}

    def request_approval(self, state):
        """Save the pending request in the state, so the API can show it while the graph is paused.

        This is a separate node on purpose: interrupt() pauses the node that calls it and throws away
        that node's own updates. Updates made by the node BEFORE it are kept.
        """
        plan = state["plan"]
        approval = {
            "status": "pending",
            "action": plan["action"],
            "service": plan["service"],
            "arguments": plan["arguments"],
            "reason": plan["reason"],
            "attempt": state["attempts"] + 1,
            "reviewer": "",
            "comment": "",
        }
        timeline = self.add_event(state["timeline"], "approval", "waiting for a human to approve " + plan["action"])
        return {"status": "waiting_approval", "approval": approval, "timeline": timeline}

    def wait_for_approval(self, state):
        """Pause the whole graph here. It continues when someone calls the graph with Command(resume=answer)."""
        approval = dict(state["approval"])
        answer = interrupt({
            "incident_id": state["incident_id"],
            "action": approval["action"],
            "service": approval["service"],
            "arguments": approval["arguments"],
        })

        # We get here only after a human answered. `answer` is what was passed to Command(resume=...).
        approved = bool(answer.get("approved", False))
        approval["reviewer"] = str(answer.get("reviewer", "unknown"))
        approval["comment"] = str(answer.get("comment", ""))
        updates = {"status": "running"}
        if approved:
            approval["status"] = "approved"
            APPROVALS.labels(decision="approved").inc()
        else:
            approval["status"] = "rejected"
            APPROVALS.labels(decision="rejected").inc()
            updates["escalation_reason"] = "a human rejected the fix (" + approval["reviewer"] + ")"
        updates["approval"] = approval
        updates["timeline"] = self.add_event(
            state["timeline"], "approval", approval["status"] + " by " + approval["reviewer"])
        return updates

    def execute(self, state):
        plan = state["plan"]
        timeline = state["timeline"]

        # Check the policy once more right before the change (a freeze may have started while we waited).
        policy = check_action(plan, state["alert"]["service"], self.settings)
        if policy["decision"] == "deny":
            error = "policy denied the change at execution time: " + policy["reason"]
            timeline = self.add_event(timeline, "execute", error)
            return {"action_result": {"ok": False, "error": error}, "escalation_reason": error, "timeline": timeline}

        arguments = dict(plan["arguments"])
        arguments["service"] = plan["service"]
        updates = {"attempts": state["attempts"] + 1}
        try:
            result = self.tools.call(plan["action"], arguments, state["incident_id"])
            updates["action_result"] = {"ok": True, "message": result["message"]}
        except ToolError as error:
            updates["action_result"] = {"ok": False, "error": str(error)}
            updates["escalation_reason"] = "the fix could not be executed: " + str(error)
        updates["timeline"] = self.add_event(timeline, "execute", str(updates["action_result"]))
        return updates

    def verify(self, state):
        """Check recovery from the service status (replayed from the scenario files). The LLM is NOT asked if it worked."""
        service = state["alert"]["service"]
        status = self.tools.call("get_service_status", {"service": service}, state["incident_id"])
        verification = check_recovered(status)
        timeline = state["timeline"]

        if verification["healthy"]:
            timeline = self.add_event(timeline, "verify", "all checks passed")
            return {"verification": verification, "timeline": timeline}

        # The fix did not work. Store that as evidence and reopen the investigation.
        failed_checks = []
        for check in verification["checks"]:
            if not check["ok"]:
                failed_checks.append(check["name"] + ": " + check["detail"])
        summary = ("fix '" + state["plan"]["action"] + "' did NOT recover the service. Failed checks: "
                   + "; ".join(failed_checks))
        evidence = Evidence(
            id="E" + str(len(state["evidence"]) + 1),
            tool="verify_after_fix",
            arguments={"fix": state["plan"]["action"]},
            summary=summary,
            data=verification,
        ).model_dump()
        timeline = self.add_event(timeline, "verify", summary)
        return {
            "verification": verification,
            "evidence": state["evidence"] + [evidence],
            "steps": 0,                      # a fresh investigation budget for the next attempt
            "done_investigating": False,
            "timeline": timeline,
        }

    def remember(self, state):
        """Learn from this incident: save a short postmortem and index it, so the next search can find it.

        Only incidents that were approved by a human AND verified healthy get here.
        """
        timeline = state["timeline"]
        if not self.settings.learn_from_incidents:
            return {"timeline": timeline}

        alert = state["alert"]
        plan = state["plan"]
        description = alert.get("description", "")
        if description == "":
            description = alert["title"]
        incident = {
            "id": "learned-" + state["incident_id"],
            "title": alert["title"],
            "service": alert["service"],
            "category": state["rca"]["category"],
            "symptoms": description,
            "root_cause": state["rca"]["root_cause"],
            "resolution": plan["action"] + " " + json.dumps(plan["arguments"]) + " (approved by a human, verified healthy)",
        }
        try:
            save_postmortem(self.settings.learned_dir, incident)
            self.retriever.add_document(incident_to_document(incident))
            timeline = self.add_event(timeline, "remember", "saved postmortem " + incident["id"])
        except Exception as error:
            timeline = self.add_event(timeline, "remember", "could not save the postmortem: " + str(error))
        return {"timeline": timeline}

    def resolve(self, state):
        timeline = self.add_event(state["timeline"], "resolved", "service is healthy again")
        final = dict(state)
        final["status"] = "resolved"
        final["timeline"] = timeline
        return {"status": "resolved", "timeline": timeline, "report": build_report(final)}

    def escalate(self, state):
        reason = self.explain_escalation(state)
        timeline = self.add_event(state["timeline"], "escalated", "handing over to a human: " + reason)
        final = dict(state)
        final["status"] = "escalated"
        final["escalation_reason"] = reason
        final["timeline"] = timeline
        return {"status": "escalated", "escalation_reason": reason, "timeline": timeline, "report": build_report(final)}

    # ---------- helpers ----------

    def explain_escalation(self, state):
        if state.get("escalation_reason", "") != "":
            return state["escalation_reason"]
        rca = state.get("rca")
        if rca and rca["inconclusive"]:
            return "root cause is inconclusive: " + "; ".join(rca["missing_evidence"])
        plan = state.get("plan")
        if plan and plan["action"] == "escalate":
            return "the agent found no safe fix: " + plan["reason"]
        policy = state.get("policy")
        if policy and policy["decision"] == "deny":
            return "policy denied the fix: " + policy["reason"]
        return "the service is still unhealthy after " + str(state.get("attempts", 0)) + " fix attempt(s)"

    def add_event(self, timeline, stage, message):
        event = {"time": time.strftime("%H:%M:%S"), "stage": stage, "message": message}
        if self.verbose:
            print("[" + stage + "] " + message)
        return timeline + [event]
