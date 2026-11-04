// IncidentPilot UI. Plain JavaScript, no framework.
//
// How it works:
//   - every second we ask the API for the selected incident (GET /incidents/{id})
//   - we build the HTML for it as a string (the render... functions below)
//   - we only touch the page when that HTML changed, so open panels and typed text are kept

var selectedId = null;
var lastHtml = "";
var openPanels = {};          // which "raw tool output" panels are open, by evidence id
var activeTab = "evidence";   // evidence, hypotheses or log

// ---------- small helpers ----------

function esc(value) {
  var div = document.createElement("div");
  div.textContent = String(value === undefined || value === null ? "" : value);
  return div.innerHTML;
}

function toast(message) {
  var box = document.createElement("div");
  box.className = "toast";
  box.setAttribute("role", "status");
  box.textContent = message;
  document.body.appendChild(box);
  setTimeout(function () { box.remove(); }, 4000);
}

async function api(path, options) {
  try {
    var response = await fetch(path, options);
    if (!response.ok) {
      var problem = await response.json().catch(function () { return {}; });
      var detail = problem.detail;
      if (typeof detail !== "string") { detail = "The server rejected the request (" + response.status + ")."; }
      toast(detail);
      return null;
    }
    return await response.json();
  } catch (error) {
    toast("Can't reach the server. If the demo was idle, it may be waking up: try again in a minute.");
    return null;
  }
}

function postJson(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

function readSaved(key) {
  try { return localStorage.getItem(key) || ""; } catch (error) { return ""; }
}

function save(key, value) {
  try { localStorage.setItem(key, value); } catch (error) { /* private window: ignore */ }
}

function refButtons(ids) {
  var html = "";
  for (var i = 0; i < ids.length; i++) {
    html += '<button class="ref" data-ev="' + esc(ids[i]) + '" title="Show evidence ' + esc(ids[i]) + '">' + esc(ids[i]) + "</button>";
  }
  return html;
}

function formatArguments(args) {
  // {"to_version": "2.13.4"} -> "to_version 2.13.4"   (empty -> "none")
  var parts = [];
  for (var key in args) { parts.push(key + " " + args[key]); }
  return parts.length > 0 ? parts.join(", ") : "none";
}

// One "item ........ value" checklist line.
function item(name, value, kind) {
  return '<li><span class="item">' + esc(name) + '</span><span class="leader"></span>' +
         '<span class="value' + (kind ? " " + kind : "") + '">' + esc(value) + "</span></li>";
}

var STATUS_WORDS = {
  running: "Investigating",
  waiting_approval: "Needs your approval",
  resolved: "Resolved",
  escalated: "Handed to a human",
  error: "Stopped by an error"
};

function capital(text) {
  text = String(text);
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function statusWord(status) {
  return STATUS_WORDS[status] || status;
}

// ---------- top bar, model choice, drills, recent incidents ----------

async function loadSystem() {
  var info = await api("/health");
  if (!info) { return; }
  document.getElementById("server-model").textContent = "Built-in: " + info.llm;
  if (info.scripted) {
    var notice = document.getElementById("notice");
    notice.innerHTML = "This live demo uses a scripted model and recorded incidents, so every run is free and repeatable. " +
      "To run a real model, choose <b>My own API key</b> in the Model menu, or run the project from " +
      '<a href="https://github.com/umangbagadi03/incidentpilot" target="_blank" rel="noopener">GitHub</a>.';
    notice.hidden = false;
  }
}

function usingOwnKey() {
  return document.getElementById("model-select").value === "own";
}

function ownModelChoice() {
  // The key is only read from the input box and sent with this request. It is never stored in the browser.
  return {
    provider: document.getElementById("provider").value,
    api_key: document.getElementById("api-key").value.trim(),
    model: document.getElementById("model-name").value.trim()
  };
}

async function loadScenarios() {
  var scenarios = await api("/scenarios");
  if (!scenarios) { return; }
  var html = "";
  for (var i = 0; i < scenarios.length; i++) {
    var s = scenarios[i];
    html += '<li><button class="drill" data-scenario="' + esc(s.name) + '">' +
              '<span class="drill-title">' + esc(s.title) + "</span>" +
              '<span class="drill-tests">' + esc(s.tagline) + "</span>" +
            "</button></li>";
  }
  document.getElementById("scenarios").innerHTML = html;
}

async function trigger(name, button) {
  var options = { method: "POST" };
  if (usingOwnKey()) {
    var choice = ownModelChoice();
    if (choice.api_key.length < 10) {
      toast("Paste your API key first, or switch the Model menu back to Built-in.");
      document.getElementById("api-key").focus();
      return;
    }
    options = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(choice) };
  }
  button.disabled = true;
  var incident = await api("/scenarios/" + encodeURIComponent(name) + "/trigger", options);
  button.disabled = false;
  if (incident) { select(incident.id); }
}

async function loadIncidents() {
  var incidents = await api("/incidents");
  if (!incidents || incidents.length === 0) { return; }
  var html = "";
  var shown = Math.min(incidents.length, 8);          // the newest 8 are enough here
  for (var i = 0; i < shown; i++) {
    var item = incidents[i];
    var active = item.id === selectedId ? " active" : "";
    html += '<li><button class="recent-item' + active + '" data-incident="' + esc(item.id) + '">' +
              '<span class="recent-title">' + esc(item.title) + "</span>" +
              '<span class="recent-status st-' + esc(item.status) + '">' + esc(statusWord(item.status)) + "</span>" +
            "</button></li>";
  }
  document.getElementById("incidents").innerHTML = html;
}

function select(id) {
  selectedId = id;
  lastHtml = "";
  openPanels = {};
  activeTab = "evidence";
  try { history.replaceState(null, "", "#" + id); } catch (error) { /* ignore */ }
  refresh();
  window.scrollTo(0, 0);
}

// ---------- the main area ----------

async function loadDetail() {
  var html;
  if (!selectedId) {
    html = renderWelcome();
  } else {
    var incident = await api("/incidents/" + encodeURIComponent(selectedId));
    if (!incident) { selectedId = null; return; }
    html = renderIncident(incident);
  }
  if (html === lastHtml) { return; }          // nothing changed: keep the page as it is
  lastHtml = html;
  document.getElementById("detail").innerHTML = html;
  restorePanels();
  var reviewer = document.getElementById("reviewer");
  if (reviewer && reviewer.value === "") { reviewer.value = readSaved("reviewer"); }
}

function renderWelcome() {
  return (
    '<section class="welcome">' +
      "<h1>Watch an AI agent work a production incident, and decide what it may change.</h1>" +
      "<p>Pick a drill on the left. The agent reads logs, metrics and deploys one tool at a time, weighs competing " +
      "explanations, and writes a root cause. Code then checks every number and name in that root cause against the " +
      "evidence it cites. Nothing changes until a policy check passes and you approve.</p>" +
      '<div class="card"><h2 class="card-title">What is checked by code, not by the model</h2><div class="card-body">' +
        '<ul class="items">' +
          item("Facts in the root cause", "must appear in the cited evidence") +
          item("Instructions hidden in logs", "removed before the model reads them") +
          item("Proposed changes", "policy check, then your approval") +
          item("Recovery", "measured against the SLO") +
          item("A failed fix", "becomes evidence, the agent tries again") +
        "</ul>" +
      "</div></div>" +
    "</section>"
  );
}

function renderIncident(incident) {
  var state = incident.state || {};
  return renderHeader(incident, state) +
         renderPhases(incident, state) +
         renderCallout(incident, state) +
         renderRootCause(state) +
         renderDetails(state);
}

function renderHeader(incident, state) {
  var alert = state.alert || {};
  var facts = '<li class="status st-' + esc(incident.status) + '">' + esc(statusWord(incident.status)) + "</li>" +
              "<li>Service: " + esc(incident.service) + "</li>" +
              "<li>Severity: " + esc(incident.severity) + "</li>";
  if (state.llm) { facts += "<li>Model: " + esc(state.llm) + "</li>"; }
  if (state.report) {
    facts += '<li><a href="/incidents/' + esc(incident.id) + '/report" target="_blank" rel="noopener">Download report</a></li>';
  }
  return '<ul class="facts">' + facts + "</ul>" +
         '<h1 class="title">' + esc(incident.title) + "</h1>" +
         '<p class="lede">' + esc(alert.description || incident.description) + "</p>";
}

// Five phases, each one done, now (agent working), wait (you), fail, or still to do.
function phaseList(incident, state) {
  var phases = [
    { name: "Investigate", kind: "", note: "" },
    { name: "Diagnose", kind: "", note: "" },
    { name: "Approve", kind: "", note: "" },
    { name: "Fix", kind: "", note: "" },
    { name: "Verify", kind: "", note: "" }
  ];
  var status = incident.status;
  var evidence = state.evidence || [];
  var tools = 0;
  for (var i = 0; i < evidence.length; i++) { if (evidence[i].tool !== "verify_after_fix") { tools++; } }

  // A failed fix sends the agent back to investigate: show the new attempt from the start.
  var retrying = status === "running" && state.verification && !state.verification.healthy;
  if (retrying) {
    phases[0].kind = "now";
    phases[0].note = "attempt " + (state.attempts + 1);
    return phases;
  }

  if (state.rca) {
    phases[0].kind = "done";
    phases[0].note = tools + " tool calls";
    var claims = state.rca.claims || [];
    var verified = 0;
    for (var c = 0; c < claims.length; c++) { if (claims[c].verified) { verified++; } }
    if (state.rca.inconclusive) {
      phases[1].kind = "fail";
      phases[1].note = "not enough evidence";
    } else {
      phases[1].kind = "done";
      phases[1].note = verified + " of " + claims.length + " claims checked";
    }
  } else if (status === "running") {
    phases[0].kind = "now";
    phases[0].note = tools + " tool calls so far";
  }

  if (state.plan && state.plan.action === "escalate") {
    phases[2].kind = "fail";
    phases[2].note = "no safe fix";
  } else if (state.policy && state.policy.decision === "deny") {
    phases[2].kind = "fail";
    phases[2].note = "blocked by policy";
  } else if (state.approval) {
    if (state.approval.status === "pending") { phases[2].kind = "wait"; phases[2].note = "waiting for you"; }
    if (state.approval.status === "approved") { phases[2].kind = "done"; phases[2].note = "approved"; }
    if (state.approval.status === "rejected") { phases[2].kind = "fail"; phases[2].note = "rejected"; }
  }

  if (state.action_result) {
    phases[3].kind = state.action_result.ok ? "done" : "fail";
    phases[3].note = state.action_result.ok ? state.plan.action.replace(/_/g, " ") : "did not run";
  }
  if (state.verification) {
    phases[4].kind = state.verification.healthy ? "done" : "fail";
    phases[4].note = state.verification.healthy ? "healthy" : "still unhealthy";
  }
  if (status === "error") {
    for (var p = 0; p < phases.length; p++) {
      if (phases[p].kind === "now" || (phases[p].kind === "" && (p === 0 || phases[p - 1].kind === "done"))) {
        phases[p].kind = "fail";
        phases[p].note = "stopped";
        break;
      }
    }
  }
  return phases;
}

function renderPhases(incident, state) {
  var phases = phaseList(incident, state);
  var html = '<ol class="phases" aria-label="Progress">';
  for (var i = 0; i < phases.length; i++) {
    html += '<li class="phase ' + phases[i].kind + '"><b>' + esc(phases[i].name) + "</b><span>" + esc(phases[i].note) + "</span></li>";
  }
  return html + "</ol>";
}

// The box that says what is happening right now, or what you need to do.
function renderCallout(incident, state) {
  var status = incident.status;

  if (status === "waiting_approval" && state.approval) {
    var a = state.approval;
    return (
      '<section class="card amber"><h2 class="card-title">Your decision: the agent wants to change production' +
        "<small>attempt " + esc(a.attempt) + "</small></h2>" +
        '<div class="card-body">' +
          '<ul class="items">' +
            item("Action", a.action.replace(/_/g, " ")) +
            item("Service", a.service) +
            item("Arguments", formatArguments(a.arguments)) +
            item("Policy check", "passed", "ok") +
          "</ul>" +
          '<p class="reason">' + esc(a.reason) + "</p>" +
          '<div class="decide">' +
            '<input id="reviewer" placeholder="Your name" autocomplete="name" aria-label="Your name">' +
            '<input id="comment" placeholder="Comment (optional)" aria-label="Comment">' +
            '<button class="btn go" data-decide="yes">Approve change</button>' +
            '<button class="btn stop" data-decide="no">Reject</button>' +
          "</div>" +
        "</div>" +
      "</section>"
    );
  }

  if (status === "resolved") {
    var checks = (state.verification && state.verification.checks) || [];
    var lines = "";
    for (var i = 0; i < checks.length; i++) {
      lines += item(capital(checks[i].name), checks[i].detail, checks[i].ok ? "ok" : "bad");
    }
    return '<section class="card green"><h2 class="card-title">Resolved: recovery measured by code, not reported by the model</h2>' +
           '<div class="card-body"><ul class="items">' + lines + "</ul></div></section>";
  }

  if (status === "escalated") {
    return '<section class="card red"><h2 class="card-title">Handed to a human</h2><div class="card-body">' +
           '<p class="reason">' + esc(state.escalation_reason) + ".</p>" +
           '<p class="empty">Nothing was changed without approval.</p></div></section>';
  }

  if (status === "error") {
    var errors = (state.timeline || []).filter(function (event) { return event.stage === "error"; });
    var message = errors.length > 0 ? errors[errors.length - 1].message : "The workflow stopped.";
    return '<section class="card red"><h2 class="card-title">Stopped by an error</h2><div class="card-body">' +
           '<p class="reason">' + esc(message) + "</p>" +
           '<p class="empty">The progress so far is saved. Retry continues from the last saved step.</p>' +
           '<button class="btn" data-retry="1">Retry from the last step</button></div></section>';
  }

  // Running: show what the agent is testing right now.
  var timeline = state.timeline || [];
  var latest = "Starting the investigation.";
  for (var t = timeline.length - 1; t >= 0; t--) {
    if (timeline[t].stage === "investigate" || timeline[t].stage === "rca" || timeline[t].stage === "plan") {
      latest = timeline[t].message;
      break;
    }
  }
  return '<section class="card blue"><h2 class="card-title">The agent is working</h2><div class="card-body">' +
         '<p class="reason">' + esc(latest) + "</p></div></section>";
}

function renderRootCause(state) {
  var rca = state.rca;
  if (!rca) { return ""; }
  var claims = rca.claims || [];
  var verified = 0;
  var list = "";
  for (var i = 0; i < claims.length; i++) {
    var claim = claims[i];
    if (claim.verified) { verified++; }
    var missing = "";
    if (!claim.verified && claim.missing_facts.length > 0) {
      missing = '<span class="missing">Not found in the cited evidence: ' + esc(claim.missing_facts.join("; ")) + "</span>";
    }
    list += '<li class="' + (claim.verified ? "ok" : "no") + '">' +
              '<span class="tick" aria-label="' + (claim.verified ? "backed by evidence" : "not backed") + '">' +
                (claim.verified ? "✓" : "✕") + "</span>" +
              '<div><span class="claim-text">' + esc(claim.text) + "</span>" +
              '<div class="claim-meta">Cites ' + refButtons(claim.evidence_ids) + " " + missing + "</div></div>" +
            "</li>";
  }
  var final = Math.round(rca.confidence * 100);
  var said = Math.round((rca.llm_confidence || 0) * 100);
  var title = rca.inconclusive ? "Root cause: not proven" : "Root cause";
  var missingEvidence = "";
  if (rca.inconclusive && rca.missing_evidence.length > 0) {
    missingEvidence = '<p class="reason">Missing: ' + esc(rca.missing_evidence.join("; ")) + "</p>";
  }
  return (
    '<section class="card"><h2 class="card-title">' + title + "<small>" + esc(rca.category.replace(/_/g, " ")) + "</small></h2>" +
      '<div class="card-body">' +
        '<p class="root' + (rca.inconclusive ? " unsure" : "") + '">' + esc(rca.root_cause) + "</p>" +
        missingEvidence +
        '<div class="confidence">' +
          '<span class="gauge" aria-hidden="true"><i style="width:' + final + '%"></i><u style="left:calc(' + said + '% - 1px)"></u></span>' +
          "<span>Confidence " + rca.confidence.toFixed(2) + ". The model said " + (rca.llm_confidence || 0).toFixed(2) +
          ", capped because " + verified + " of " + claims.length + " claims are backed by evidence.</span>" +
        "</div>" +
        '<ul class="claims">' + list + "</ul>" +
      "</div>" +
    "</section>"
  );
}

function renderDetails(state) {
  var evidence = state.evidence || [];
  var board = state.hypotheses || [];
  var timeline = state.timeline || [];
  var tabs = [
    { id: "evidence", name: "Evidence (" + evidence.length + ")" },
    { id: "hypotheses", name: "Hypotheses (" + board.length + ")" },
    { id: "log", name: "Log (" + timeline.length + ")" }
  ];
  var head = '<ul class="tabs" role="tablist">';
  for (var i = 0; i < tabs.length; i++) {
    head += '<li><button class="tab" role="tab" data-tab="' + tabs[i].id + '" aria-selected="' +
            (tabs[i].id === activeTab) + '">' + esc(tabs[i].name) + "</button></li>";
  }
  head += "</ul>";
  var body = "";
  if (activeTab === "evidence") { body = renderEvidence(evidence); }
  if (activeTab === "hypotheses") { body = renderBoard(board); }
  if (activeTab === "log") { body = renderLog(timeline); }
  return '<section aria-label="Details">' + head + '<div class="tab-panel" role="tabpanel">' + body + "</div></section>";
}

var SOURCES = { REPLAY: "recorded system data", RUNBOOK: "runbook", HISTORICAL: "past incident" };

function renderEvidence(evidence) {
  if (evidence.length === 0) { return '<p class="empty">The agent is choosing its first tool.</p>'; }
  var html = "";
  for (var i = 0; i < evidence.length; i++) {
    var e = evidence[i];
    var source = e.tool === "verify_after_fix" ? "check after the fix" : (SOURCES[e.kind] || e.kind);
    var flags = "";
    if (!e.ok) { flags += '<span class="flag">Tool error</span>'; }
    if (e.flags && e.flags.indexOf("prompt_injection") !== -1) { flags += '<span class="flag">Hidden instruction removed</span>'; }
    var quarantine = "";
    if (e.quarantined && e.quarantined.length > 0) {
      quarantine = '<div class="quarantine"><b>Removed before the model could read it</b>';
      for (var q = 0; q < e.quarantined.length; q++) { quarantine += "<div>" + esc(e.quarantined[q]) + "</div>"; }
      quarantine += "</div>";
    }
    html += '<article class="evidence" id="ev-' + esc(e.id) + '">' +
              '<div class="ev-head"><span class="ev-id">' + esc(e.id) + '</span><span class="ev-tool">' + esc(e.tool) + "</span>" +
              '<span class="ev-source">' + esc(source) + "</span>" + flags + "</div>" +
              '<p class="ev-summary">' + esc(e.summary) + "</p>" + quarantine +
              '<details class="raw" data-panel="' + esc(e.id) + '"><summary>Raw tool output</summary><pre>' +
              esc(JSON.stringify(e.data, null, 2)) + "</pre></details>" +
            "</article>";
  }
  return html;
}

var VERDICTS = { supported: "Supported", refuted: "Ruled out", open: "Still open" };

function renderBoard(board) {
  if (board.length === 0) { return '<p class="empty">Competing explanations appear here while the agent investigates.</p>'; }
  var html = '<p class="empty">The agent keeps several explanations at once and picks the tool that tells them apart. ' +
             "A verdict without evidence is reset to open by code.</p>";
  for (var i = 0; i < board.length; i++) {
    var h = board[i];
    html += '<div class="hypo ' + esc(h.status) + '"><span class="hypo-id">' + esc(h.id) + "</span>" +
              '<div><span class="hypo-text">' + esc(h.statement) + "</span>" +
              '<div class="hypo-refs">' + refButtons(h.evidence_ids) + "</div></div>" +
              '<span class="hypo-verdict">' + esc(VERDICTS[h.status] || h.status) + "</span>" +
            "</div>";
  }
  return html;
}

function renderLog(timeline) {
  if (timeline.length === 0) { return '<p class="empty">Starting.</p>'; }
  var html = '<ol class="log" reversed>';
  for (var i = timeline.length - 1; i >= 0; i--) {       // newest first
    var event = timeline[i];
    html += '<li><span class="when">' + esc(event.time) + '</span><span class="what">' + esc(event.stage) + "</span>" +
            '<span class="msg">' + esc(event.message) + "</span></li>";
  }
  return html + "</ol>";
}

// ---------- actions ----------

async function decide(approved, button) {
  var typed = document.getElementById("reviewer").value.trim();
  var comment = document.getElementById("comment").value.trim();
  if (typed !== "") { save("reviewer", typed); }
  var reviewer = typed || "anonymous";
  button.disabled = true;
  await postJson("/incidents/" + encodeURIComponent(selectedId) + "/approval",
                 { approved: approved, reviewer: reviewer, comment: comment });
  refresh();
}

async function retry() {
  await api("/incidents/" + encodeURIComponent(selectedId) + "/retry", { method: "POST" });
  refresh();
}

function showEvidence(id) {
  // Citations live in the Evidence tab: switch to it, then highlight the cited item.
  if (activeTab !== "evidence") {
    activeTab = "evidence";
    lastHtml = "";
    loadDetail().then(function () { highlight(id); });
    return;
  }
  highlight(id);
}

function highlight(id) {
  var card = document.getElementById("ev-" + id);
  if (!card) { return; }
  card.classList.add("lit");
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  setTimeout(function () { card.classList.remove("lit"); }, 1600);
}

function restorePanels() {
  var panels = document.querySelectorAll("details[data-panel]");
  for (var i = 0; i < panels.length; i++) {
    if (openPanels[panels[i].dataset.panel]) { panels[i].open = true; }
  }
}

// One click handler for the whole page (the HTML is rebuilt often, so we do not attach handlers to elements).
document.addEventListener("click", function (event) {
  var target = event.target.closest("button, summary");
  if (!target) { return; }
  if (target.dataset.scenario) { trigger(target.dataset.scenario, target); }
  else if (target.dataset.incident) { select(target.dataset.incident); }
  else if (target.dataset.decide) { decide(target.dataset.decide === "yes", target); }
  else if (target.dataset.retry) { retry(); }
  else if (target.dataset.ev) { showEvidence(target.dataset.ev); }
  else if (target.dataset.tab) { activeTab = target.dataset.tab; lastHtml = ""; loadDetail(); }
});

document.addEventListener("change", function (event) {
  if (event.target.id === "model-select") {
    document.getElementById("own-key").hidden = !usingOwnKey();
    if (usingOwnKey()) { document.getElementById("api-key").focus(); }
  }
});

document.addEventListener("toggle", function (event) {
  var panel = event.target;
  if (panel.dataset && panel.dataset.panel) { openPanels[panel.dataset.panel] = panel.open; }
}, true);

// ---------- start ----------

function refresh() {
  loadIncidents();
  loadDetail();
}

if (location.hash.length > 1) { selectedId = location.hash.slice(1); }
loadSystem();
loadScenarios();
refresh();
setInterval(refresh, 1000);
