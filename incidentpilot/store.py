"""A tiny SQLite table with one row per incident, so the API can list incidents quickly.

The full workflow state also lives in the LangGraph checkpoints. We copy the latest state
into this table after every run so the API does not need to know about LangGraph.
"""
import json
import sqlite3
import time
import uuid

TERMINAL_STATUSES = ["resolved", "escalated", "error"]


class IncidentStore:
    def __init__(self, path):
        self.path = path
        connection = self.connect()
        connection.execute(
            "CREATE TABLE IF NOT EXISTS incidents ("
            "id TEXT PRIMARY KEY, scenario TEXT, service TEXT, title TEXT, description TEXT, severity TEXT, "
            "status TEXT, created_at REAL, updated_at REAL, state_json TEXT)"
        )
        connection.commit()
        connection.close()

    def connect(self):
        # One short-lived connection per call keeps this safe to use from several threads.
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, alert, scenario=""):
        incident_id = "inc-" + uuid.uuid4().hex[:8]
        now = time.time()
        connection = self.connect()
        connection.execute(
            "INSERT INTO incidents (id, scenario, service, title, description, severity, status, created_at, updated_at, state_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (incident_id, scenario, alert["service"], alert["title"], alert.get("description", ""), alert.get("severity", "high"),
             "running", now, now, "{}"),
        )
        connection.commit()
        connection.close()
        return self.get(incident_id)

    def update(self, incident_id, status, state=None):
        connection = self.connect()
        if state is None:
            connection.execute("UPDATE incidents SET status = ?, updated_at = ? WHERE id = ?",
                               (status, time.time(), incident_id))
        else:
            connection.execute("UPDATE incidents SET status = ?, updated_at = ?, state_json = ? WHERE id = ?",
                               (status, time.time(), json.dumps(state), incident_id))
        connection.commit()
        connection.close()

    def get(self, incident_id):
        connection = self.connect()
        row = connection.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        connection.close()
        if row is None:
            return None
        return self.row_to_dict(row, with_state=True)

    def list(self):
        connection = self.connect()
        rows = connection.execute("SELECT * FROM incidents ORDER BY created_at DESC").fetchall()
        connection.close()
        incidents = []
        for row in rows:
            incidents.append(self.row_to_dict(row, with_state=False))
        return incidents

    def row_to_dict(self, row, with_state):
        incident = {
            "id": row["id"],
            "scenario": row["scenario"],
            "service": row["service"],
            "title": row["title"],
            "description": row["description"] if "description" in row.keys() else "",
            "severity": row["severity"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if with_state:
            incident["state"] = json.loads(row["state_json"])
        return incident
