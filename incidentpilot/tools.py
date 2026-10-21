"""The tools the agent can use.

In this project the "production system" is a folder of recorded files (scenarios/).
A read tool returns what was recorded. A write tool records the action. If the action is the one
that really fixes the fault (the "fix" block in the scenario's expected.json), the service becomes
healthy FOR THAT INCIDENT and the read tools return the *_after_fix.json files from then on.
That lets us test the full loop (investigate -> fix -> verify) with no real infrastructure.

The "is it fixed?" flag is stored per incident id, in SQLite (ReplayState below). So:
  - a restart between "execute" and "verify" does not undo the fix (like the LangGraph checkpoints)
  - two incidents on the same service never share the flag

To use a real system, replace the bodies of these methods with real API calls
(Kubernetes, Prometheus, ...). The agent and the workflow do not change.
"""
import json
import os
import sqlite3
import time

LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}


class ToolError(Exception):
    """Raised when a tool call fails (unknown service, bad arguments, ...)."""
    pass


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize(message):
    """Replace every number with '#', so 'request_id=12' and 'request_id=98' count as the same pattern."""
    result = ""
    for char in message:
        if char.isdigit():
            if not result.endswith("#"):
                result = result + "#"
        else:
            result = result + char
    return result


def sort_by_count(item):
    return item["count"]


class ReplayState:
    """Which incidents already had the right fix applied (simulation only).

    Kept per INCIDENT, not per service. With a path it lives in SQLite and survives a restart;
    without a path (some tests) it is a plain list in memory.
    """

    def __init__(self, path=""):
        self.path = path
        self.memory = []
        if path != "":
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE IF NOT EXISTS replay_fixed (incident_id TEXT PRIMARY KEY, fixed_at REAL)")
            connection.commit()
            connection.close()

    def is_fixed(self, incident_id):
        if self.path == "":
            return incident_id in self.memory
        connection = sqlite3.connect(self.path)
        row = connection.execute("SELECT 1 FROM replay_fixed WHERE incident_id = ?", (incident_id,)).fetchone()
        connection.close()
        return row is not None

    def mark_fixed(self, incident_id):
        if self.path == "":
            if incident_id not in self.memory:
                self.memory.append(incident_id)
            return
        connection = sqlite3.connect(self.path)
        connection.execute("INSERT OR IGNORE INTO replay_fixed (incident_id, fixed_at) VALUES (?, ?)",
                           (incident_id, time.time()))
        connection.commit()
        connection.close()

    def clear(self, incident_id):
        if self.path == "":
            if incident_id in self.memory:
                self.memory.remove(incident_id)
            return
        connection = sqlite3.connect(self.path)
        connection.execute("DELETE FROM replay_fixed WHERE incident_id = ?", (incident_id,))
        connection.commit()
        connection.close()


class ScenarioTools:
    def __init__(self, scenarios_dir, state_path=""):
        self.folders = {}        # scenario name -> folder path
        self.replay = ReplayState(state_path)       # incident id -> fixed or not
        self.fixes = {}          # scenario name -> the action that fixes the fault (or None)
        self.service_to_scenario = {}
        self.actions = []        # every write action that was executed

        for name in sorted(os.listdir(scenarios_dir)):
            folder = os.path.join(scenarios_dir, name)
            alert_file = os.path.join(folder, "alert.json")
            if not os.path.isfile(alert_file):
                continue
            service = read_json(alert_file)["service"]
            if service in self.service_to_scenario:
                raise ValueError("two scenarios use the service '" + service + "', each needs its own service")
            self.folders[name] = folder
            self.fixes[name] = self.read_fix(folder)
            self.service_to_scenario[service] = name

    def read_fix(self, folder):
        expected_file = os.path.join(folder, "expected.json")
        if not os.path.isfile(expected_file):
            return None
        return read_json(expected_file).get("fix")

    def list_scenarios(self):
        return list(self.folders.keys())

    def describe_scenario(self, scenario_name):
        """Short info for the UI. The tagline says what the scenario tests (it is never shown to the LLM)."""
        alert = self.load_alert(scenario_name)
        tagline = ""
        expected_file = os.path.join(self.folders[scenario_name], "expected.json")
        if os.path.isfile(expected_file):
            tagline = read_json(expected_file).get("tagline", "")
        return {
            "name": scenario_name,
            "service": alert["service"],
            "title": alert["title"],
            "severity": alert.get("severity", "high"),
            "tagline": tagline,
        }

    def load_alert(self, scenario_name):
        if scenario_name not in self.folders:
            raise ToolError("unknown scenario: " + scenario_name)
        return read_json(os.path.join(self.folders[scenario_name], "alert.json"))

    def load_expected(self, scenario_name):
        """Ground truth, used by evaluate.py."""
        return read_json(os.path.join(self.folders[scenario_name], "expected.json"))

    def reset(self, incident_id=""):
        """Forget that this incident was fixed (simulation only, used by tests)."""
        self.replay.clear(incident_id)

    # ---------- helpers ----------

    def scenario_for(self, service):
        if service not in self.service_to_scenario:
            raise ToolError("unknown service: " + str(service))
        return self.service_to_scenario[service]

    def read(self, service, file_name, incident_id=""):
        """Read <file_name>.json. After the right fix for this incident, <file_name>_after_fix.json is used if it exists."""
        scenario = self.scenario_for(service)
        folder = self.folders[scenario]
        if self.replay.is_fixed(incident_id):
            fixed_path = os.path.join(folder, file_name + "_after_fix.json")
            if os.path.isfile(fixed_path):
                return read_json(fixed_path)
        return read_json(os.path.join(folder, file_name + ".json"))

    def does_action_fix_service(self, service, action, params):
        """True if this action is the one that fixes the scenario's fault.

        The "fix" block in expected.json says which action works, for example:
          {"action": "rollback_deploy", "arguments": {"to_version": "2.13.4"}}
          {"action": "scale_service", "arguments": {"replicas_at_least": 6}}
        """
        fix = self.fixes[self.scenario_for(service)]
        if fix is None or fix["action"] != action:
            return False
        wanted = fix.get("arguments", {})
        if "to_version" in wanted and params.get("to_version") != wanted["to_version"]:
            return False
        if "replicas_at_least" in wanted and int(params.get("replicas", 0)) < wanted["replicas_at_least"]:
            return False
        return True

    # ---------- read tools ----------

    def get_service_status(self, service, incident_id=""):
        return self.read(service, "status", incident_id)

    def get_metrics(self, service, incident_id=""):
        return self.read(service, "metrics", incident_id)

    def list_deploys(self, service, incident_id=""):
        return self.read(service, "deploys", incident_id)

    def search_logs(self, service, level="ERROR", query="", incident_id=""):
        """Return the most common log patterns (and 3 sample lines) at this level or above."""
        level = level.upper()
        if level not in LEVEL_ORDER:
            raise ToolError("level must be DEBUG, INFO, WARN or ERROR")

        matching_lines = []
        for line in self.read(service, "logs", incident_id)["lines"]:
            if LEVEL_ORDER[line["level"]] < LEVEL_ORDER[level]:
                continue
            if query != "" and query.lower() not in line["message"].lower():
                continue
            matching_lines.append(line)

        counts = {}
        first_line = {}
        for line in matching_lines:
            pattern = normalize(line["message"])
            if pattern not in counts:
                counts[pattern] = 0
                first_line[pattern] = line
            counts[pattern] = counts[pattern] + 1

        patterns = []
        for pattern in counts:
            patterns.append({
                "pattern": pattern,
                "count": counts[pattern],
                "first_seen": first_line[pattern]["ts"],
                "example": first_line[pattern]["message"],     # one real line, with its numbers
            })
        patterns.sort(key=sort_by_count, reverse=True)

        return {
            "service": service,
            "level": level,
            "total_lines": len(matching_lines),
            "patterns": patterns[:6],
            "samples": matching_lines[:3],
        }

    # ---------- write tools ----------

    def rollback_deploy(self, service, to_version, incident_id=""):
        deploys = self.read(service, "deploys", incident_id)
        known_versions = [deploys["current_version"], deploys["previous_version"]]
        if to_version not in known_versions:
            raise ToolError("unknown version: " + str(to_version))
        current_version = self.read(service, "status", incident_id)["version"]
        message = "rolled back " + service + " to " + to_version
        return self.record_action("rollback_deploy", service, {"to_version": to_version}, message, incident_id,
                                  changes_something=(to_version != current_version))

    def restart_service(self, service, incident_id=""):
        return self.record_action("restart_service", service, {}, "restarted all replicas of " + service, incident_id)

    def scale_service(self, service, replicas, incident_id=""):
        replicas = int(replicas)
        if replicas < 1 or replicas > 20:
            raise ToolError("replicas must be between 1 and 20")
        message = "scaled " + service + " to " + str(replicas) + " replicas"
        return self.record_action("scale_service", service, {"replicas": replicas}, message, incident_id)

    def record_action(self, action, service, params, message, incident_id, changes_something=True):
        """Remember the action. If it is the scenario's fix, the service is healthy for this incident from now on."""
        self.scenario_for(service)
        if changes_something and self.does_action_fix_service(service, action, params):
            self.replay.mark_fixed(incident_id)
        self.actions.append({"action": action, "service": service, "params": params, "incident_id": incident_id})
        return {"ok": True, "action": action, "service": service, "message": message}

    # ---------- one entry point for the workflow ----------

    def call(self, tool_name, arguments, incident_id=""):
        """Run a tool by name. Bad arguments from the LLM become a ToolError, not a crash.

        incident_id comes from the workflow, never from the LLM (an "incident_id" argument is dropped).
        """
        methods = {
            "get_service_status": self.get_service_status,
            "get_metrics": self.get_metrics,
            "list_deploys": self.list_deploys,
            "search_logs": self.search_logs,
            "rollback_deploy": self.rollback_deploy,
            "restart_service": self.restart_service,
            "scale_service": self.scale_service,
        }
        if tool_name not in methods:
            raise ToolError("unknown tool: " + str(tool_name))
        arguments = dict(arguments)
        arguments.pop("incident_id", None)
        try:
            return methods[tool_name](incident_id=incident_id, **arguments)
        except (TypeError, ValueError, KeyError) as error:
            raise ToolError("bad arguments for " + tool_name + ": " + str(error))


def summarize(tool_name, data):
    """One short line per tool result. The LLM and the report both use this line."""
    if tool_name == "get_service_status":
        current = data["current"]
        return (
            data["service"] + " v" + data["version"] + " health=" + data["health"]
            + ", error_rate=" + str(current["error_rate"]) + "%"
            + ", latency_p95=" + str(current["latency_p95"]) + "ms"
            + ", memory=" + str(current["memory_mb"]) + "MB"
            + " (SLO error_rate max " + str(data["slo"]["error_rate_max"]) + "%)"
        )

    if tool_name == "get_metrics":
        parts = []
        for metric_name in data["metrics"]:
            metric = data["metrics"][metric_name]
            parts.append(metric_name + " latest=" + str(metric["latest"]) + " baseline=" + str(metric["baseline"])
                         + " (x" + str(metric["ratio"]) + ")")
        return "; ".join(parts)

    if tool_name == "search_logs":
        if data["total_lines"] == 0:
            return "no " + data["level"] + " log lines found"
        parts = []
        for pattern in data["patterns"][:3]:
            parts.append(str(pattern["count"]) + "x '''" + pattern["pattern"] + "'''")
        return str(data["total_lines"]) + " lines at " + data["level"] + "+. Top patterns: " + "; ".join(parts)

    if tool_name == "list_deploys":
        if len(data["deploys"]) == 0:
            return "no recent deploys, current version is " + data["current_version"]
        latest = data["deploys"][0]
        return ("latest deploy: " + latest["version"] + " " + str(latest["minutes_ago"]) + " minutes ago ("
                + latest["notes"] + "), previous version " + data["previous_version"])

    if tool_name == "search_runbooks":
        if len(data["results"]) == 0:
            return "no matching runbook or past incident"
        titles = []
        for result in data["results"]:
            titles.append(result["title"])
        return "matches: " + "; ".join(titles)

    return json.dumps(data)[:200]
