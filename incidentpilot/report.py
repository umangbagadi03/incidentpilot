"""Build a markdown incident report from the final state."""


def build_report(state):
    alert = state["alert"]
    lines = []
    lines.append("# Incident report: " + alert["title"])
    lines.append("")
    lines.append("- Incident id: " + state["incident_id"])
    lines.append("- Service: " + alert["service"] + ", severity: " + alert.get("severity", "unknown"))
    lines.append("- **Final status: " + state["status"].upper() + "**")
    if state.get("escalation_reason", "") != "":
        lines.append("- Escalation reason: " + state["escalation_reason"])
    lines.append("- Fix attempts: " + str(state.get("attempts", 0)))
    lines.append("")

    lines.append("## Evidence")
    lines.append("")
    for evidence in state["evidence"]:
        mark = "ok"
        if not evidence["ok"]:
            mark = "FAILED"
        lines.append("- **" + evidence["id"] + "** `" + evidence["tool"] + "` [" + evidence["kind"] + ", " + mark + "]: "
                     + evidence["summary"])
        for text in evidence.get("quarantined", []):
            lines.append("  - GUARDRAIL quarantined this text (never shown to the LLM): `" + text + "`")
    lines.append("")

    hypotheses = state.get("hypotheses", [])
    if len(hypotheses) > 0:
        lines.append("## Hypothesis board")
        lines.append("")
        for hypothesis in hypotheses:
            cited = ""
            if len(hypothesis["evidence_ids"]) > 0:
                cited = " (" + ", ".join(hypothesis["evidence_ids"]) + ")"
            lines.append("- " + hypothesis["id"] + " **" + hypothesis["status"].upper() + "**: "
                         + hypothesis["statement"] + cited)
        lines.append("")

    rca = state.get("rca")
    if rca:
        lines.append("## Root cause analysis")
        lines.append("")
        lines.append("- Root cause: " + rca["root_cause"])
        lines.append("- Category: " + rca["category"])
        lines.append("- Confidence: " + str(rca["confidence"]) + " (the LLM said " + str(rca.get("llm_confidence", 0))
                     + ", grounding score " + str(rca.get("grounding_score", 0)) + ")")
        lines.append("- Cited evidence: " + ", ".join(rca["evidence_ids"]))
        if rca["inconclusive"]:
            lines.append("- INCONCLUSIVE. Missing: " + "; ".join(rca["missing_evidence"]))
        lines.append("")
        lines.append("Claims, each checked by code against the evidence it cites:")
        lines.append("")
        for claim in rca.get("claims", []):
            if claim["verified"]:
                mark = "VERIFIED"
            else:
                mark = "NOT BACKED (missing: " + ", ".join(claim["missing_facts"]) + ")"
            lines.append("- [" + mark + "] " + claim["text"] + " (" + ", ".join(claim["evidence_ids"]) + ")")
        lines.append("")

    plan = state.get("plan")
    if plan:
        lines.append("## Proposed fix")
        lines.append("")
        lines.append("- Action: `" + plan["action"] + "` " + str(plan["arguments"]))
        lines.append("- Reason: " + plan["reason"])
        policy = state.get("policy")
        if policy:
            lines.append("- Policy: " + policy["decision"] + " (" + policy["reason"] + ")")
        approval = state.get("approval")
        if approval and approval["status"] != "pending":
            line = "- Human decision: " + approval["status"] + " by " + approval["reviewer"]
            if approval["comment"] != "":
                line = line + " (" + approval["comment"] + ")"
            lines.append(line)
        lines.append("")

    verification = state.get("verification")
    if verification:
        lines.append("## Verification (service status checked by code, not by the LLM; replay-based in this project)")
        lines.append("")
        for check in verification["checks"]:
            mark = "PASS"
            if not check["ok"]:
                mark = "FAIL"
            lines.append("- " + mark + ": " + check["name"] + " (" + check["detail"] + ")")
        lines.append("")

    lines.append("## Timeline")
    lines.append("")
    for event in state["timeline"]:
        lines.append("- " + event["time"] + " [" + event["stage"] + "] " + event["message"])
    lines.append("")
    return "\n".join(lines)
