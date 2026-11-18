// IncidentPilot UI. Plain JavaScript, no framework.
//
// How it works:
//   - every second we ask the API for the selected incident (GET /incidents/{id})
//   - we build the HTML for it as a string (the render... functions below)
//   - we only touch the page when that HTML changed, so open panels and typed text are kept

var selectedId = null;
var lastHtml = "";
var openPanels = {};          // which <details> panels are open: raw tool output by evidence id, and the log
var scenarios = [];           // loaded once, used by the sidebar and the start page

// Line icons (lucide, 1.5 stroke). Each one is the inside of a 24x24 svg.
var ICONS = {
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  play: '<path d="M7 4.5v15l12-7.5z"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
  retry: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>'
};

// ---------- small helpers ----------

function icon(name, extraClass) {
  return '<svg class="icon' + (extraClass ? " " + extraClass : "") + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
         'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + "</svg>";
}

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
      if (typeof detail !== "string") { detail = "The server rejected the request (" + response.status + ")"; }
      toast(detail);
      return null;
    }
    return await response.json();
  } catch (error) {
    toast("Can't reach the server. If the demo was idle it may be waking up, so try again in a minute.");
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

// Evidence tags: E1, E2 ... Clicking one highlights that evidence.
function tags(ids) {
  var html = "";
  for (var i = 0; i < ids.length; i++) {
    html += '<button class="tag" data-ev="' + esc(ids[i]) + '" title="Show evidence ' + esc(ids[i]) + '">' + esc(ids[i]) + "</button>";
  }
  return '<span class="tags">' + html + "</span>";
}

function formatArguments(args) {
  // {"to_version": "2.13.4"} -> "to_version 2.13.4"   (empty -> "none")
  var parts = [];
  for (var key in args) { parts.push(key + " " + args[key]); }
  return parts.length > 0 ? parts.join(", ") : "none";
}

// One "name    value" row.
function row(name, value, kind) {
  return '<li><span class="k">' + esc(name) + '</span><span class="v' + (kind ? " " + kind : "") + '">' + esc(value) + "</span></li>";
}

function capital(text) {
  text = String(text);
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function words(name) {
  return String(name).replace(/_/g, " ");       // rollback_deploy -> rollback deploy
}

// The proposed fix as a short sentence: "roll back checkout to 2.13.4".
function describeFix(action, service, args) {
  if (action === "rollback_deploy") { return "roll back " + service + " to " + args.to_version; }
  if (action === "restart_service") { return "restart " + service; }
  if (action === "scale_service") { return "scale " + service + " to " + args.replicas + " replicas"; }
  return words(action) + " on " + service;
}

var STATUS_WORDS = {
  running: "Investigating",
  waiting_approval: "Needs your approval",
  resolved: "Resolved",
  escalated: "Handed to a human",
  error: "Stopped by an error"
};

function statusWord(status) {
  return STATUS_WORDS[status] || status;
}

function ago(seconds) {
  var passed = Date.now() / 1000 - seconds;
  if (passed < 60) { return "now"; }
  if (passed < 3600) { return Math.floor(passed / 60) + "m"; }
  if (passed < 86400) { return Math.floor(passed / 3600) + "h"; }
  return Math.floor(passed / 86400) + "d";
}

// ---------- sidebar: model choice, scenarios, recent incidents ----------

async function loadSystem() {
  var info = await api("/health");
  if (!info) { return; }
  document.getElementById("server-model").textContent = "Built-in: " + info.llm;
  if (info.scripted) {
    var notice = document.getElementById("notice");
    notice.innerHTML = "The built-in model is scripted, so every run is free and repeatable. " +
      "Choose My own API key to run a real model, or run it from " +
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
  var list = await api("/scenarios");
  if (!list) { return; }
  scenarios = list;
  var html = "";
  for (var i = 0; i < scenarios.length; i++) {
    var s = scenarios[i];
    html += '<li><button class="nav-row" data-scenario="' + esc(s.name) + '" title="' + esc(s.title) + '">' +
              '<span class="name">' + esc(s.service) + "</span>" + icon("play") +
            "</button></li>";
  }
  document.getElementById("scenarios").innerHTML = html;
  if (!selectedId) { loadDetail(); }           // the start page lists the scenarios too
}

async function trigger(name, button) {
  var options = { method: "POST" };
  if (usingOwnKey()) {
    var choice = ownModelChoice();
    if (choice.api_key.length < 10) {
      toast("Paste your API key first, or switch the model back to Built-in");
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
    var incident = incidents[i];
    var active = incident.id === selectedId ? " active" : "";
    html += '<li><button class="nav-row' + active + '" data-incident="' + esc(incident.id) + '" ' +
              'title="' + esc(incident.title + ": " + statusWord(incident.status)) + '">' +
              '<span class="dot ' + esc(incident.status) + '"></span>' +
              '<span class="name">' + esc(incident.service) + "</span>" +
              '<span class="meta">' + esc(ago(incident.created_at)) + "</span>" +
            "</button></li>";
  }
  document.getElementById("incidents").innerHTML = html;
}

function select(id) {
  selectedId = id;
  lastHtml = "";
  openPanels = {};
  try { history.replaceState(null, "", "#" + id); } catch (error) { /* ignore */ }
  refresh();
  if (window.matchMedia("(max-width: 820px)").matches) {
    document.getElementById("detail").scrollIntoView();     // on a phone the sidebar sits above the incident
  } else {
    window.scrollTo(0, 0);
  }
}

// ---------- the main area ----------

async function loadDetail() {
  var html;
  if (!selectedId) {
    html = renderStart();
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

function renderStart() {
  var runs = "";
  for (var i = 0; i < scenarios.length; i++) {
    var s = scenarios[i];
    runs += '<li class="run">' +
              '<span class="run-service">' + esc(s.service) + "</span>" +
              '<div><span class="run-title">' + esc(s.title) + '</span><span class="run-tests">' + esc(s.tagline) + "</span></div>" +
              '<button class="btn" data-scenario="' + esc(s.name) + '">' + icon("play") + "Run</button>" +
            "</li>";
  }
  var checks = [
    ["Facts in the root cause", "Every number, version and name must appear in the evidence the claim cites"],
    ["Instructions hidden in logs", "Removed before the model reads the tool output"],
    ["Proposed change", "A policy check first, then your approval"],
    ["Recovery", "Read from the service status and compared with the SLO"],
    ["A fix that did not work", "Becomes new evidence, and the agent investigates again"]
  ];
  var checkList = "";
  for (var c = 0; c < checks.length; c++) {
    checkList += "<li><b>" + esc(checks[c][0]) + "</b><span>" + esc(checks[c][1]) + "</span></li>";
  }
  return (
    '<header class="head">' +
      '<p class="meta-line">' + scenarios.length + " recorded incidents</p>" +
      "<h1>Run a recorded incident</h1>" +
      '<p class="lede">The agent reads logs, metrics and deploys one tool at a time and writes a root cause. ' +
      "Code checks every claim in it against the evidence it cites, and nothing changes in production until a policy check passes and you approve.</p>" +
    "</header>" +
    '<div class="grid">' +
      '<section class="panel"><div class="panel-head"><h2>Scenarios</h2><span class="meta">replayed from files</span></div>' +
        '<ul class="runs">' + runs + "</ul></section>" +
      '<section class="panel"><div class="panel-head"><h2>Checked by code, not by the model</h2></div>' +
        '<ul class="checks">' + checkList + "</ul></section>" +
    "</div>"
  );
}

function renderIncident(incident) {
  var state = incident.state || {};
  return renderHeader(incident, state) +
         renderSteps(incident, state) +
         renderStatus(incident, state) +
         '<div class="grid">' +
           '<div class="col">' + renderProof(state) + renderLog(state.timeline || []) + "</div>" +
           '<div class="col">' + renderEvidence(state.evidence || []) + "</div>" +
         "</div>";
}

function renderHeader(incident, state) {
  var alert = state.alert || {};
  var meta = '<span class="state"><span class="dot ' + esc(incident.status) + '"></span>' + esc(statusWord(incident.status)) + "</span>" +
             "<span>" + esc(incident.service) + "</span>" +
             "<span>" + esc(incident.severity) + "</span>";
  if (state.llm) { meta += "<span>" + esc(state.llm) + "</span>"; }
  if (state.report) {
    meta += '<a href="/incidents/' + esc(incident.id) + '/report" target="_blank" rel="noopener">Report</a>';
  }
  return '<header class="head">' +
           '<p class="meta-line">' + meta + "</p>" +
           "<h1>" + esc(incident.title) + "</h1>" +
           '<p class="lede">' + esc(alert.description || incident.description) + "</p>" +
         "</header>";
}

// Five steps, each one done, now (agent working), wait (you), fail, or still to do.
function stepList(incident, state) {
  var steps = [
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
    steps[0].kind = "now";
    steps[0].note = "attempt " + (state.attempts + 1);
    return steps;
  }

  if (state.rca) {
    steps[0].kind = "done";
    steps[0].note = tools + " tool calls";
    var claims = state.rca.claims || [];
    var verified = 0;
    for (var c = 0; c < claims.length; c++) { if (claims[c].verified) { verified++; } }
    if (state.rca.inconclusive) {
      steps[1].kind = "fail";
      steps[1].note = "not enough evidence";
    } else {
      steps[1].kind = "done";
      steps[1].note = verified + " of " + claims.length + " claims backed";
    }
  } else if (status === "running") {
    steps[0].kind = "now";
    steps[0].note = tools + " tool calls so far";
  }

  if (state.plan && state.plan.action === "escalate") {
    steps[2].kind = "fail";
    steps[2].note = "no safe fix";
  } else if (state.policy && state.policy.decision === "deny") {
    steps[2].kind = "fail";
    steps[2].note = "blocked by policy";
  } else if (state.approval) {
    if (state.approval.status === "pending") { steps[2].kind = "wait"; steps[2].note = "waiting for you"; }
    if (state.approval.status === "approved") { steps[2].kind = "done"; steps[2].note = "approved"; }
    if (state.approval.status === "rejected") { steps[2].kind = "fail"; steps[2].note = "rejected"; }
  }

  if (state.action_result) {
    steps[3].kind = state.action_result.ok ? "done" : "fail";
    steps[3].note = state.action_result.ok ? words(state.plan.action) : "did not run";
  }
  if (state.verification) {
    steps[4].kind = state.verification.healthy ? "done" : "fail";
    steps[4].note = state.verification.healthy ? "healthy" : "still unhealthy";
  }
  if (status === "error") {
    for (var p = 0; p < steps.length; p++) {
      if (steps[p].kind === "now" || (steps[p].kind === "" && (p === 0 || steps[p - 1].kind === "done"))) {
        steps[p].kind = "fail";
        steps[p].note = "stopped";
        break;
      }
    }
  }
  return steps;
}

function renderSteps(incident, state) {
  var steps = stepList(incident, state);
  var html = '<ol class="steps" aria-label="Progress">';
  for (var i = 0; i < steps.length; i++) {
    var mark = "";
    if (steps[i].kind === "done") { mark = icon("check"); }
    if (steps[i].kind === "fail") { mark = icon("x"); }
    html += '<li class="step ' + steps[i].kind + '"><b>' + mark + esc(steps[i].name) + "</b>" +
            "<span>" + esc(steps[i].note || " ") + "</span></li>";
  }
  return html + "</ol>";
}

// The box that says what is happening right now, or what you need to do.
function renderStatus(incident, state) {
  var status = incident.status;

  if (status === "waiting_approval" && state.approval) {
    var a = state.approval;
    return (
      '<section class="panel status waiting">' +
        '<div class="status-title"><span class="dot waiting_approval"></span>' +
          "The agent wants to " + esc(describeFix(a.action, a.service, a.arguments)) + "</div>" +
        "<p>" + esc(a.reason) + "</p>" +
        '<ul class="rows">' +
          row("Arguments", formatArguments(a.arguments)) +
          row("Policy check", "passed", "ok") +
          row("Attempt", a.attempt) +
        "</ul>" +
        '<div class="decide">' +
          '<input id="reviewer" placeholder="Your name" autocomplete="name" aria-label="Your name">' +
          '<input id="comment" placeholder="Comment (optional)" aria-label="Comment">' +
          '<button class="btn primary" data-decide="yes">' + icon("check") + "Approve</button>" +
          '<button class="btn" data-decide="no">Reject</button>' +
        "</div>" +
      "</section>"
    );
  }

  if (status === "resolved") {
    var checks = (state.verification && state.verification.checks) || [];
    var lines = "";
    for (var i = 0; i < checks.length; i++) {
      lines += row(capital(checks[i].name), checks[i].detail, checks[i].ok ? "ok" : "bad");
    }
    return '<section class="panel status">' +
             '<div class="status-title"><span class="dot resolved"></span>Resolved</div>' +
             "<p>Recovery was read from the service status by code, not reported by the model.</p>" +
             '<ul class="rows">' + lines + "</ul></section>";
  }

  if (status === "escalated") {
    return '<section class="panel status">' +
             '<div class="status-title"><span class="dot escalated"></span>Handed to a human</div>' +
             "<p>" + esc(capital(state.escalation_reason || "")) + ". Nothing was changed without approval.</p></section>";
  }

  if (status === "error") {
    var errors = (state.timeline || []).filter(function (event) { return event.stage === "error"; });
    var message = errors.length > 0 ? errors[errors.length - 1].message : "The workflow stopped.";
    return '<section class="panel status">' +
             '<div class="status-title"><span class="dot error"></span>Stopped by an error</div>' +
             "<p>" + esc(message) + "</p>" +
             "<p>The progress so far is saved. Retry continues from the last saved step.</p>" +
             '<div><button class="btn" data-retry="1">' + icon("retry") + "Retry</button></div></section>";
  }

  // Running: show what the agent is doing right now.
  var timeline = state.timeline || [];
  var latest = "Starting the investigation";
  for (var t = timeline.length - 1; t >= 0; t--) {
    if (timeline[t].stage === "investigate" || timeline[t].stage === "rca" || timeline[t].stage === "plan") {
      latest = capital(timeline[t].message);
      break;
    }
  }
  return '<section class="panel status">' +
           '<div class="status-title"><span class="dot running"></span>The agent is working</div>' +
           "<p>" + esc(latest) + "</p></section>";
}

// The root cause, and the proof for each claim in it. This is the part of the page that matters most.
function renderProof(state) {
  var rca = state.rca;
  if (!rca) {
    return '<section class="panel"><div class="panel-head"><h2>Root cause</h2></div>' +
           '<p class="empty">Written when the agent stops investigating. Code then checks every claim in it against the evidence it cites.</p></section>';
  }
  var claims = rca.claims || [];
  var verified = 0;
  var list = "";
  var bar = "";
  for (var i = 0; i < claims.length; i++) {
    var claim = claims[i];
    if (claim.verified) { verified++; }
    var missing = "";
    if (!claim.verified && claim.missing_facts.length > 0) {
      missing = '<span class="missing">Not in the cited evidence: ' + esc(claim.missing_facts.join(", ")) + "</span>";
    }
    list += '<li class="' + (claim.verified ? "ok" : "no") + '">' +
              icon(claim.verified ? "check" : "x") +
              "<div>" + esc(claim.text) + missing + "</div>" +
              tags(claim.evidence_ids) +
            "</li>";
    bar += '<i class="' + (claim.verified ? "ok" : "no") + '" title="Claim ' + (i + 1) + '"></i>';
  }

  var confidence = rca.confidence.toFixed(2);
  var said = (rca.llm_confidence || 0).toFixed(2);
  var explain = "Code caps the confidence at the share of claims backed by evidence.";
  if (confidence !== said) {
    explain = "The model said " + said + ". Code lowered it to " + confidence + " because only " + verified + " of " +
              claims.length + " claims are backed by evidence.";
  }
  var missingEvidence = "";
  if (rca.inconclusive && rca.missing_evidence.length > 0) {
    missingEvidence = '<p class="explain">Still missing: ' + esc(rca.missing_evidence.join(", ")) + "</p>";
  }

  return (
    '<section class="panel">' +
      '<div class="panel-head"><h2>' + (rca.inconclusive ? "Root cause not proven" : "Root cause") + "</h2>" +
        '<span class="meta">' + esc(words(rca.category)) + "</span></div>" +
      '<p class="root' + (rca.inconclusive ? " unsure" : "") + '">' + esc(rca.root_cause) + "</p>" +
      '<div class="figures">' +
        '<div><div class="figure-label">Confidence</div><div class="figure-num">' + confidence + "</div>" +
          '<div class="figure-sub">model said ' + said + "</div></div>" +
        '<div><div class="figure-label">Claims backed by evidence</div>' +
          '<div class="figure-num">' + verified + "<small> of " + claims.length + "</small></div>" +
          '<div class="figure-sub">checked by code</div></div>' +
      "</div>" +
      (claims.length > 0 ? '<div class="proofbar" aria-hidden="true">' + bar + "</div>" : "") +
      '<ul class="claims">' + list + "</ul>" +
      '<p class="explain">' + esc(explain) + "</p>" +
      missingEvidence +
    "</section>"
  );
}

var SOURCES = { REPLAY: "recorded", RUNBOOK: "runbook", HISTORICAL: "past incident" };

function renderEvidence(evidence) {
  var head = '<div class="panel-head"><h2>Evidence</h2><span class="meta">' + evidence.length + " items</span></div>";
  if (evidence.length === 0) {
    return '<section class="panel">' + head + '<p class="empty">The agent is choosing its first tool.</p></section>';
  }
  var html = "";
  for (var i = 0; i < evidence.length; i++) {
    var e = evidence[i];
    var source = e.tool === "verify_after_fix" ? "check after the fix" : (SOURCES[e.kind] || e.kind);
    var flag = e.ok ? "" : '<span class="flag">tool error</span>';
    var quarantine = "";
    if (e.quarantined && e.quarantined.length > 0) {
      quarantine = '<div class="quarantine"><b>' + icon("shield") + "Removed before the model could read it</b>";
      for (var q = 0; q < e.quarantined.length; q++) { quarantine += "<code>" + esc(e.quarantined[q]) + "</code>"; }
      quarantine += "</div>";
    }
    html += '<li class="ev" id="ev-' + esc(e.id) + '">' +
              '<div class="ev-head"><span class="tag">' + esc(e.id) + '</span><span class="ev-tool">' + esc(e.tool) + "</span>" +
              flag + '<span class="ev-src">' + esc(source) + "</span></div>" +
              '<p class="ev-sum">' + esc(e.summary) + "</p>" + quarantine +
              '<details class="raw" data-panel="' + esc(e.id) + '"><summary>' + icon("chevron", "chev") + "Raw output</summary>" +
              "<pre>" + esc(JSON.stringify(e.data, null, 2)) + "</pre></details>" +
            "</li>";
  }
  return '<section class="panel">' + head + '<ul class="evidence">' + html + "</ul></section>";
}

function renderLog(timeline) {
  var items = "";
  for (var i = timeline.length - 1; i >= 0; i--) {       // newest first
    var event = timeline[i];
    items += '<li><span class="when">' + esc(event.time) + '</span><span class="what">' + esc(event.stage) + "</span>" +
             '<span class="msg">' + esc(event.message) + "</span></li>";
  }
  return '<details class="panel log-panel" data-panel="log"><summary><h2>Agent log</h2>' +
           '<span class="meta">' + timeline.length + " events</span>" + icon("chevron", "chev") + "</summary>" +
           '<ol class="log">' + items + "</ol></details>";
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
  var item = document.getElementById("ev-" + id);
  if (!item) { return; }
  item.classList.add("lit");
  item.scrollIntoView({ behavior: "smooth", block: "center" });
  setTimeout(function () { item.classList.remove("lit"); }, 1600);
}

function restorePanels() {
  var panels = document.querySelectorAll("details[data-panel]");
  for (var i = 0; i < panels.length; i++) {
    if (openPanels[panels[i].dataset.panel]) { panels[i].open = true; }
  }
}

// One click handler for the whole page (the HTML is rebuilt often, so we do not attach handlers to elements).
document.addEventListener("click", function (event) {
  var target = event.target.closest("button");
  if (!target) { return; }
  if (target.dataset.scenario) { trigger(target.dataset.scenario, target); }
  else if (target.dataset.incident) { select(target.dataset.incident); }
  else if (target.dataset.decide) { decide(target.dataset.decide === "yes", target); }
  else if (target.dataset.retry) { retry(); }
  else if (target.dataset.ev) { showEvidence(target.dataset.ev); }
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
