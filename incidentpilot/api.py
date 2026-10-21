"""The HTTP API (FastAPI). Open http://127.0.0.1:8000/docs to try every endpoint, or / for the small UI.

Starting an incident returns at once. The graph runs in a background task, so you poll
GET /incidents/{id}. When the status is "waiting_approval", answer with POST /incidents/{id}/approval.
"""
import os

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from incidentpilot.config import ROOT
from incidentpilot.service import ServiceError

STATUS_CODES = {"bad_request": 400, "not_found": 404, "conflict": 409}


class AlertRequest(BaseModel):
    service: str
    title: str
    description: str = ""
    severity: str = "high"


class ApprovalRequest(BaseModel):
    approved: bool
    reviewer: str = "anonymous"
    comment: str = ""


def http_error(error):
    """Turn a ServiceError into an HTTP error."""
    return HTTPException(status_code=STATUS_CODES.get(error.code, 400), detail=str(error))


def create_app(service):
    app = FastAPI(title="IncidentPilot", version="2.0.0",
                  description="An LLM agent that investigates incidents, with human approval for every change.")

    ui_dir = os.path.join(ROOT, "ui")
    app.mount("/ui", StaticFiles(directory=ui_dir), name="ui")      # style.css and app.js

    @app.get("/")
    def home():
        return FileResponse(os.path.join(ui_dir, "index.html"), media_type="text/html")

    @app.get("/health")
    def health():
        return service.health()

    @app.get("/metrics")
    def metrics():
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/scenarios")
    def list_scenarios():
        """The recorded scenarios, with a short line about what each one tests."""
        scenarios = []
        for name in service.tools.list_scenarios():
            scenarios.append(service.tools.describe_scenario(name))
        return scenarios

    @app.post("/scenarios/{name}/trigger")
    def trigger_scenario(name: str, background: BackgroundTasks):
        """Start an incident from a recorded scenario."""
        try:
            incident = service.trigger_scenario(name)
        except ServiceError as error:
            raise http_error(error)
        background.add_task(service.run, incident["id"], service.initial_input(incident))
        return incident

    @app.post("/incidents")
    def create_incident(alert: AlertRequest, background: BackgroundTasks):
        """Start an incident from an alert (the service must be one the recorded tools know)."""
        try:
            incident = service.start_incident(alert.model_dump())
        except ServiceError as error:
            raise http_error(error)
        background.add_task(service.run, incident["id"], service.initial_input(incident))
        return incident

    @app.get("/incidents")
    def list_incidents():
        return service.list_incidents()

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: str):
        try:
            return service.get(incident_id)
        except ServiceError as error:
            raise http_error(error)

    @app.post("/incidents/{incident_id}/approval")
    def decide(incident_id: str, body: ApprovalRequest, background: BackgroundTasks):
        """Approve or reject the fix the agent proposed. The paused graph continues in the background."""
        try:
            answer = service.begin_decision(incident_id, body.approved, body.reviewer, body.comment)
        except ServiceError as error:
            raise http_error(error)
        background.add_task(service.resume, incident_id, answer)
        return {"incident_id": incident_id, "accepted": True}

    @app.post("/incidents/{incident_id}/retry")
    def retry(incident_id: str, background: BackgroundTasks):
        """Continue an incident that stopped with status 'error' (for example the LLM API was down)."""
        try:
            incident = service.retry(incident_id)
        except ServiceError as error:
            raise http_error(error)
        background.add_task(service.continue_from_checkpoint, incident_id)
        return incident

    @app.get("/incidents/{incident_id}/report")
    def report(incident_id: str):
        try:
            incident = service.get(incident_id)
        except ServiceError as error:
            raise http_error(error)
        text = incident["state"].get("report", "")
        if text == "":
            raise HTTPException(status_code=409, detail="the report is written when the incident is finished")
        return Response(content=text, media_type="text/markdown")

    return app
