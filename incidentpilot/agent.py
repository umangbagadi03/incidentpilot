"""The Incident Agent: the three things we ask the LLM to do.

1. decide_next_step: update the hypothesis board and pick the next tool (or: I have enough evidence)
2. write_rca:        explain the root cause as claims that cite evidence ids
3. plan_fix:         propose one fix

Every LLM answer is JSON. We parse it into a Pydantic model. If it does not fit the model
we ask once more, and if it still does not fit we use a safe fallback (finish, inconclusive, escalate).
The LLM proposes. Plain Python code checks the claims (grounding.py) and decides what may happen (policy.py).
"""
import json

from incidentpilot import prompts
from incidentpilot.grounding import ground_rca
from incidentpilot.llm import describe_llm, redact_secrets
from incidentpilot.schemas import Decision, Plan, RCA


class LLMError(RuntimeError):
    """An LLM call failed. The message never contains an API key."""
    pass


def call_llm(llm, prompt):
    """Call the model. A provider error can quote part of the API key, and LangGraph saves the error of a
    failed step in its checkpoint, so the error is re-raised with every key removed."""
    try:
        return llm.complete(prompts.SYSTEM, prompt)
    except Exception as error:
        raise LLMError(redact_secrets(str(error), [getattr(llm, "api_key", "")])) from None


def parse_json(text):
    """Find the JSON object inside the LLM reply. Raises ValueError if there is none."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in the reply")
    try:
        decoder = json.JSONDecoder()
        obj, index = decoder.raw_decode(text[start:])
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    end = text.rfind("}")
    if end == -1 or end < start:
        raise ValueError("no JSON object found in the reply")
    return json.loads(text[start:end + 1])


def evidence_for_prompt(evidence_list, max_data_chars=1500):
    """Turn the evidence into text for the prompt. Long tool output is cut so the prompt stays small.

    Quarantined text (see guardrails.py) is NOT included, only the flag that something was removed.
    """
    items = []
    for evidence in evidence_list:
        data_text = json.dumps(evidence["data"])
        if len(data_text) > max_data_chars:
            data_text = data_text[:max_data_chars] + "...(cut)"
        item = {
            "id": evidence["id"],
            "tool": evidence["tool"],
            "arguments": evidence["arguments"],
            "ok": evidence["ok"],
            "summary": evidence["summary"],
            "data": data_text,
        }
        if len(evidence.get("flags", [])) > 0:
            item["flags"] = evidence["flags"]
        items.append(item)
    if len(items) == 0:
        return "(none yet)"
    return json.dumps(items, indent=1)


def hypotheses_for_prompt(hypotheses):
    if not hypotheses:
        return "(empty, start one)"
    return json.dumps(hypotheses, indent=1)


def check_decision(decision):
    """Extra rule that a type alone cannot express: call_tool needs a tool."""
    if decision.action == "call_tool" and decision.tool == "":
        raise ValueError("action call_tool needs a tool name")


def check_plan(plan):
    """Check that action-specific arguments are present before asking a human to approve."""
    if plan.action == "rollback_deploy" and not plan.arguments.get("to_version"):
        raise ValueError("rollback_deploy needs to_version in arguments")
    if plan.action == "scale_service" and "replicas" not in plan.arguments:
        raise ValueError("scale_service needs replicas in arguments")


class IncidentAgent:
    def __init__(self, llm, llm_for=None):
        self.llm = llm               # the default model
        self.llm_for = llm_for       # optional: incident id -> the model chosen for that incident

    def model_for(self, incident_id):
        if self.llm_for is not None and incident_id != "":
            return self.llm_for(incident_id)
        return self.llm

    def label_for(self, incident_id):
        return describe_llm(self.model_for(incident_id))

    def ask(self, prompt, model_class, extra_check=None, incident_id=""):
        """Ask the LLM and parse the answer into a Pydantic model. One retry if the answer is invalid."""
        llm = self.model_for(incident_id)
        reply = call_llm(llm, prompt)
        try:
            return self.parse(reply, model_class, extra_check)
        except ValueError as error:      # JSON errors and Pydantic ValidationError are both ValueError
            retry_prompt = (
                prompt + "\n\nYour previous reply was invalid (" + str(error) + "). "
                "Reply again with only the JSON object."
            )
            reply = call_llm(llm, retry_prompt)
            return self.parse(reply, model_class, extra_check)

    def parse(self, reply, model_class, extra_check):
        model = model_class.model_validate(parse_json(reply))
        if extra_check is not None:
            extra_check(model)
        return model

    def decide_next_step(self, alert, evidence_list, step, max_steps, hypotheses=None, incident_id=""):
        prompt = prompts.fill(prompts.DECIDE_NEXT_STEP, {
            "ALERT": json.dumps(alert),
            "EVIDENCE": evidence_for_prompt(evidence_list),
            "HYPOTHESES": hypotheses_for_prompt(hypotheses),
            "STEP": step,
            "MAX_STEPS": max_steps,
            "SERVICE": alert["service"],
        })
        try:
            return self.ask(prompt, Decision, check_decision, incident_id)
        except ValueError:
            # Safe fallback: stop investigating. The RCA step says "inconclusive" if the evidence is thin.
            return Decision(action="finish", reason="could not get a valid decision from the LLM")

    def write_rca(self, alert, evidence_list, hypotheses=None, incident_id=""):
        prompt = prompts.fill(prompts.WRITE_RCA, {
            "ALERT": json.dumps(alert),
            "EVIDENCE": evidence_for_prompt(evidence_list),
            "HYPOTHESES": hypotheses_for_prompt(hypotheses),
        })
        try:
            rca = self.ask(prompt, RCA, None, incident_id)
        except ValueError:
            return RCA(
                root_cause="RCA could not be written (invalid LLM output)",
                inconclusive=True,
                missing_evidence=["a valid root cause analysis"],
            )

        # GROUNDING CHECK (grounding.py): every claim must cite real evidence, and every value in the
        # claim must appear in that evidence. Confidence is capped by the share of verified claims.
        checked = ground_rca(rca.model_dump(), evidence_list)
        return RCA.model_validate(checked)

    def plan_fix(self, alert, rca, evidence_list, incident_id=""):
        service = alert["service"]

        # Earlier fixes that did not work (the verify step stores them as evidence).
        failed_fixes = []
        for evidence in evidence_list:
            if evidence["tool"] == "verify_after_fix":
                failed_fixes.append(evidence["summary"])
        if len(failed_fixes) == 0:
            failed_text = "(none)"
        else:
            failed_text = "\n".join(failed_fixes)

        prompt = prompts.fill(prompts.PLAN_FIX, {
            "ALERT": json.dumps(alert),
            "RCA": json.dumps(rca),
            "EVIDENCE": evidence_for_prompt(evidence_list),
            "FAILED_FIXES": failed_text,
            "SERVICE": service,
        })
        try:
            plan = self.ask(prompt, Plan, check_plan, incident_id)
        except ValueError:
            return Plan(action="escalate", service=service, reason="could not get a valid fix from the LLM")
        if plan.service == "":
            plan.service = service
        return plan
