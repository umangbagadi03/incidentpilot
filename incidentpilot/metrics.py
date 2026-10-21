"""Prometheus counters. The API exposes them at /metrics."""
from prometheus_client import Counter

INCIDENTS_FINISHED = Counter("ip_incidents_finished_total", "Incidents that reached a final status", ["status"])
LLM_CALLS = Counter("ip_llm_calls_total", "LLM calls", ["outcome"])
LLM_TOKENS = Counter("ip_llm_tokens_total", "LLM tokens", ["direction"])
TOOL_CALLS = Counter("ip_tool_calls_total", "Tool calls made by the agent", ["tool", "outcome"])
POLICY_DECISIONS = Counter("ip_policy_decisions_total", "Policy decisions", ["decision"])
APPROVALS = Counter("ip_approvals_total", "Human approval decisions", ["decision"])
GUARDRAIL_HITS = Counter("ip_guardrail_hits_total", "Tool results where instruction-like text was quarantined")
