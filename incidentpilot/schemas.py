"""Typed data contracts (Pydantic).

Every answer from the LLM is parsed into one of these classes. If the answer does not fit
(wrong field, wrong type, confidence 7.0, unknown action), Pydantic raises a ValidationError
and the agent asks again.
"""
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

READ_TOOLS = ["get_service_status", "get_metrics", "search_logs", "list_deploys", "search_runbooks"]
WRITE_TOOLS = ["rollback_deploy", "restart_service", "scale_service"]


class Alert(BaseModel):
    """The alert that starts an incident."""
    service: str
    title: str
    description: str = ""
    severity: str = "high"


class Evidence(BaseModel):
    """One piece of evidence (one tool result). The RCA must cite these by id."""
    id: str                                   # E1, E2, ...
    step: int = 0
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    # REPLAY = read from a recorded scenario, RUNBOOK / HISTORICAL = from the knowledge base.
    kind: Literal["REPLAY", "RUNBOOK", "HISTORICAL"] = "REPLAY"
    ok: bool = True
    summary: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)
    flags: List[str] = Field(default_factory=list)          # for example "prompt_injection"
    quarantined: List[str] = Field(default_factory=list)    # text the guardrail removed (never sent to the LLM)


class Hypothesis(BaseModel):
    """One possible root cause on the agent's hypothesis board."""
    id: str = ""                              # H1, H2, ...
    statement: str
    status: Literal["open", "supported", "refuted"] = "open"
    evidence_ids: List[str] = Field(default_factory=list)


class Decision(BaseModel):
    """What the agent wants to do next while investigating."""
    action: Literal["call_tool", "finish"]
    tool: Literal["get_service_status", "get_metrics", "search_logs", "list_deploys", "search_runbooks", ""] = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)
    hypotheses: List[Hypothesis] = Field(default_factory=list)   # the updated board
    hypothesis: str = ""        # what the agent is testing with this step, in one sentence
    reason: str = ""


class Claim(BaseModel):
    """One factual sentence of the RCA, with the evidence that proves it.

    `verified` and `missing_facts` are filled by grounding.py (code), never trusted from the LLM.
    """
    text: str
    evidence_ids: List[str] = Field(default_factory=list)
    verified: bool = False
    missing_facts: List[str] = Field(default_factory=list)


class RCA(BaseModel):
    """Root cause analysis. Must be backed by evidence ids."""
    root_cause: str
    category: str = "unknown"
    summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_ids: List[str] = Field(default_factory=list)
    claims: List[Claim] = Field(default_factory=list)
    inconclusive: bool = False
    missing_evidence: List[str] = Field(default_factory=list)
    # Filled by code after the grounding check:
    llm_confidence: float = 0.0               # what the LLM said
    grounding_score: float = 0.0              # verified claims / all claims
    root_cause_verified: bool = False         # the root_cause sentence is backed by the verified claims' evidence
    root_cause_missing: List[str] = Field(default_factory=list)


class Plan(BaseModel):
    """The fix the agent proposes. Only a proposal: policy and a human decide."""
    action: Literal["rollback_deploy", "restart_service", "scale_service", "escalate"]
    service: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
