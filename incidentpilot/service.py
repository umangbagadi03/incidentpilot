"""The glue between the outside world (CLI, API) and the LangGraph graph.

It starts incidents, runs the graph, resumes it after a human decision, retries after a crash,
and copies the latest graph state into the incidents table.
"""
import logging
import sqlite3
import time

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from incidentpilot.agent import IncidentAgent
from incidentpilot.llm import describe_llm, is_scripted, redact_secrets
from incidentpilot.metrics import INCIDENTS_FINISHED
from incidentpilot.rag import build_retriever, load_documents
from incidentpilot.schemas import Alert
from incidentpilot.store import TERMINAL_STATUSES, IncidentStore
from incidentpilot.tools import ScenarioTools, ToolError
from incidentpilot.tracing import get_callbacks
from incidentpilot.workflow import IncidentWorkflow, new_state

log = logging.getLogger("incidentpilot.service")


class ServiceError(Exception):
    """code is one of: bad_request, not_found, conflict."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


class IncidentService:
    def __init__(self, settings, graph, store, tools, retriever, llm):
        self.settings = settings
        self.graph = graph
        self.store = store
        self.tools = tools
        self.retriever = retriever
        self.llm = llm                       # the server's default model
        # incident id -> the model for that incident ("bring your own key", or a per-incident fallback).
        # Kept ONLY in memory: an API key is never written to SQLite, the state, the logs or the report.
        self.incident_llms = {}
        self.callbacks = get_callbacks(settings)

    def config_for(self, incident_id):
        # thread_id = the incident id. LangGraph keeps one checkpoint history per thread.
        config = {"configurable": {"thread_id": incident_id}, "recursion_limit": 80}
        if len(self.callbacks) > 0:
            config["callbacks"] = self.callbacks
        return config

    def health(self):
        return {
            "status": "ok",
            "llm": describe_llm(self.llm),
            "scripted": is_scripted(self.llm),
            "retrieval": self.retriever.describe(),
            "tools": "replay (recorded scenarios)",
        }

    # ---------- starting ----------

    def llm_for(self, incident_id):
        """The model for this incident: its own if one was chosen, else the server default."""
        if incident_id in self.incident_llms:
            return self.incident_llms[incident_id]
        return self.llm

    def trigger_scenario(self, scenario_name, llm=None):
        try:
            alert = self.tools.load_alert(scenario_name)
        except ToolError as error:
            raise ServiceError("not_found", str(error))
        return self.start_incident(alert, scenario_name, llm)

    def start_incident(self, alert, scenario="", llm=None):
        """Create the incident row. Call run() afterwards to start the graph.

        llm: an optional model for this incident only (for example built from the visitor's own API key).
        """
        try:
            alert = Alert.model_validate(alert).model_dump()
        except ValueError as error:
            raise ServiceError("bad_request", str(error))
        try:
            self.tools.scenario_for(alert["service"])
        except ToolError as error:
            raise ServiceError("bad_request", str(error))
        incident = self.store.create(alert, scenario)
        if llm is not None:
            self.incident_llms[incident["id"]] = llm
        elif hasattr(self.llm, "fresh"):
            self.incident_llms[incident["id"]] = self.llm.fresh()     # the fallback switch is per incident
        return incident

    def initial_input(self, incident):
        alert = {
            "service": incident["service"],
            "title": incident["title"],
            "severity": incident["severity"],
            "description": incident.get("description", ""),
        }
        if incident["scenario"] != "":
            # Only the known alert fields go to the LLM (an extra field in a file cannot sneak into the prompt).
            alert = Alert.model_validate(self.tools.load_alert(incident["scenario"])).model_dump()
        state = new_state(incident["id"], alert, incident["scenario"])
        state["llm"] = describe_llm(self.llm_for(incident["id"]))     # shown in the UI from the first second
        return state

    # ---------- running ----------

    def run(self, incident_id, graph_input):
        """Run the graph until it finishes or pauses for approval. Safe to call from a background thread.

        stream() gives us control back after every node, so we copy the state each time:
        the UI sees every tool call while the graph is still running.
        """
        try:
            for update in self.graph.stream(graph_input, self.config_for(incident_id), stream_mode="updates"):
                self.sync(incident_id)
        except Exception as error:
            log.error("incident %s failed: %s", incident_id, self.safe_error(incident_id, error))
            self.fail(incident_id, error)
            return
        self.sync(incident_id)

    def sync(self, incident_id):
        """Copy the latest graph state into the incidents table."""
        values = self.graph.get_state(self.config_for(incident_id)).values
        status = values.get("status", "running")
        previous = self.store.get(incident_id)["status"]
        self.store.update(incident_id, status, values)
        if status in TERMINAL_STATUSES and previous not in TERMINAL_STATUSES:
            INCIDENTS_FINISHED.labels(status=status).inc()
        if status in ["resolved", "escalated"]:
            self.incident_llms.pop(incident_id, None)       # forget the model (and any key) once it is done

    def safe_error(self, incident_id, error):
        """The error text with any API key removed (provider errors can quote part of a key)."""
        secret = getattr(self.llm_for(incident_id), "api_key", "")
        return redact_secrets(str(error), [secret])

    def fail(self, incident_id, error):
        """The graph crashed (for example the LLM API was down). The checkpoint is kept, so retry() can continue."""
        values = dict(self.graph.get_state(self.config_for(incident_id)).values)
        timeline = list(values.get("timeline", []))
        timeline.append({"time": time.strftime("%H:%M:%S"), "stage": "error", "message": "workflow error: " + self.safe_error(incident_id, error)})
        values["timeline"] = timeline
        self.store.update(incident_id, "error", values)
        INCIDENTS_FINISHED.labels(status="error").inc()

    def retry(self, incident_id):
        """Continue an incident that stopped with status 'error' from its last checkpoint."""
        incident = self.get(incident_id)
        if incident["status"] != "error":
            raise ServiceError("conflict", "only incidents with status 'error' can be retried")
        self.store.update(incident_id, "running")
        return incident

    def continue_from_checkpoint(self, incident_id):
        self.run(incident_id, None)       # None = "continue from the last checkpoint"

    def mark_interrupted(self):
        """Call once at start-up. An incident that was 'running' when the server stopped can be retried."""
        count = 0
        for incident in self.store.list():
            if incident["status"] == "running":
                self.store.update(incident["id"], "error")
                count = count + 1
        return count

    # ---------- human decisions ----------

    def begin_decision(self, incident_id, approved, reviewer, comment):
        """Check that the incident is waiting for approval, mark it running, and return the answer for the graph."""
        incident = self.get(incident_id)
        if incident["status"] != "waiting_approval":
            raise ServiceError("conflict", "incident is not waiting for approval (status: " + incident["status"] + ")")
        self.store.update(incident_id, "running")
        return {"approved": approved, "reviewer": reviewer, "comment": comment}

    def resume(self, incident_id, answer):
        """Wake up the paused graph. The answer is what interrupt() returns inside wait_for_approval."""
        self.run(incident_id, Command(resume=answer))

    def decide(self, incident_id, approved, reviewer, comment=""):
        answer = self.begin_decision(incident_id, approved, reviewer, comment)
        self.resume(incident_id, answer)

    # ---------- reading ----------

    def get(self, incident_id):
        incident = self.store.get(incident_id)
        if incident is None:
            raise ServiceError("not_found", "incident not found: " + incident_id)
        return incident

    def list_incidents(self):
        return self.store.list()


def build_service(settings, llm, retriever=None, verbose=False):
    """Create every part and connect them."""
    settings.make_dirs()
    # The replay "is it fixed?" flags live in the same SQLite file as the incidents, per incident id.
    tools = ScenarioTools(settings.scenarios_dir, state_path=settings.db_path)
    if retriever is None:
        documents = load_documents(settings.knowledge_dir, settings.learned_dir)
        retriever = build_retriever(settings, documents)
    agent = IncidentAgent(llm)          # llm_for is connected below, once the service exists
    workflow = IncidentWorkflow(tools, retriever, agent, settings, verbose=verbose)

    # Checkpoints are stored in SQLite, so a paused incident survives a restart of the server.
    connection = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL")
    graph = workflow.build(SqliteSaver(connection))

    store = IncidentStore(settings.db_path)
    service = IncidentService(settings, graph, store, tools, retriever, llm)
    agent.llm_for = service.llm_for
    return service


def run_to_end(service, incident_id, approver, max_rounds=10):
    """Run an incident until it is finished. `approver(approval)` returns the human's answer.

    Used by the command line, evaluate.py and the tests. The API does the same thing in two requests.
    """
    incident = service.get(incident_id)
    service.run(incident_id, service.initial_input(incident))
    for round_number in range(max_rounds):
        incident = service.get(incident_id)
        if incident["status"] != "waiting_approval":
            return incident
        answer = approver(incident["state"]["approval"])
        service.decide(incident_id, answer["approved"], answer["reviewer"], answer.get("comment", ""))
    return service.get(incident_id)
