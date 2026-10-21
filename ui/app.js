// IncidentPilot UI. Plain JavaScript, no framework.
//
// How it works:
//   - every second we ask the API for the selected incident (GET /incidents/{id})
//   - we build the HTML for it as a string (render... functions below)
//   - we only touch the page when that HTML changed, so open panels and typed text are kept

var selectedId = null;
var lastHtml = "";
var openPanels = {};          // which "raw output" panels are open, by evidence id

// The LangGraph nodes, in order, as shown in the "flight path".
var NODES = ["investigate", "analyze", "plan fix", "policy", "approval", "execute", "verify", "resolve"];

// Which node a timeline event belongs to.
var STAGE_TO_NODE = {
  investigate: 0, evidence: 0, guardrail: 0,
  rca: 1, plan: 2, policy: 3, approval: 4, execute: 5, verify: 6,
  remember: 7, resolved: 7, escalated: 7
};

// ---------- small helpers ----------

function esc(value) {
  var div = document.createElement("div");
  div.textContent = String(value === undefined || value === null ? "" : value);
  return div.innerHTML;
}

function toast(message) {
  var box = document.createElement("div");
  box.className = "toast";
  box.textContent = message;
  document.body.appendChild(box);
  setTimeout(function () { box.remove(); }, 3500);
}

async function api(path, options) {
  try {
    var response = await fetch(path, options);
    if (!response.ok) {
      var problem = await response.json().catch(function () { return {}; });
      toast(problem.detail || ("Request failed: " + response.status));
      return null;
    }
    return await response.json();
  } catch (error) {
    toast("The server is not reachable.");
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

function citeChips(ids) {
  var html = "";
  for (var i = 0; i < ids.length; i++) {
    html += '<button class="cite" data-ev="' + esc(ids[i]) + '" title="Show evidence ' + esc(ids[i]) + '">' + esc(ids[i]) + "</button>";
  }
  return html;
}

function formatArguments(args) {
  // {"to_version": "2.13.4"} -> to_version=2.13.4   (empty -> nothing)
  var parts = [];
  for (var key in args) { parts.push(key + "=" + args[key]); }
  return parts.join(" ");
}

function prettyStatus(status) {
  return String(status).replace("_", " ");
}

// ---------- top bar and sidebar ----------

async function loadSystem() {
  var info = await api("/health");
  if (!info) { return; }
  document.getElementById("system").innerHTML =
    '<span class="chip"><b>MODEL</b>' + esc(info.llm) + "</span>" +
    '<span class="chip"><b>RETRIEVAL</b>' + esc(info.retrieval) + "</span>" +
    '<span class="chip"><b>TOOLS</b>' + esc(info.tools) + "</span>";
}

async function loadScenarios() {
  var scenarios = await api("/scenarios");
  if (!scenarios) { return; }
  var html = "";
  for (var i = 0; i < scenarios.length; i++) {
    var s = scenarios[i];
    html +=
      '<button class="scenario" data-scenario="' + esc(s.name) + '">' +
        '<span class="scenario-top">' +
          '<span class="service-tag"><span class="sev ' + esc(s.severity) + '"></span>' + esc(s.service) + "</span>" +
          '<span class="run">run drill &rarr;</span>' +
        "</span>" +
        '<span class="scenario-title">' + esc(s.title) + "</span>" +
        '<span class="scenario-tagline">' + esc(s.tagline) + "</span>" +
      "</button>";
  }
  document.getElementById("scenarios").innerHTML = html;
}

async function trigger(name, button) {
  button.disabled = true;
  var incident = await api("/scenarios/" + encodeURIComponent(name) + "/trigger", { method: "POST" });
  button.disabled = false;
  if (incident) {
    select(incident.id);
  }
}

async function loadIncidents() {
  var incidents = await api("/incidents");
  if (!incidents || incidents.length === 0) { return; }
  var html = "";
  for (var i = 0; i < incidents.length; i++) {
    var item = incidents[i];
    var active = item.id === selectedId ? " active" : "";
    html +=
      '<button class="incident-row' + active + '" data-incident="' + esc(item.id) + '">' +
        '<span class="row-top"><span class="service-tag">' + esc(item.service) + "</span>" +
        '<span class="status ' + esc(item.status) + '">' + esc(prettyStatus(item.status)) + "</span></span>" +
        '<span class="row-title">' + esc(item.title) + "</span>" +
        '<span class="muted small mono">' + esc(item.id) + "</span>" +
      "</button>";
  }
  document.getElementById("incidents").innerHTML = html;
}

function select(id) {
  selectedId = id;
  lastHtml = "";
  openPanels = {};
  try { history.replaceState(null, "", "#" + id); } catch (error) { /* ignore */ }
  refresh();
  window.scrollTo(0, 0);
}

// ---------- the incident page ----------

async function loadDetail() {
  var html;
  if (!selectedId) {
    html = renderIntro();
  } else {
    var incident = await api("/incidents/" + encodeURIComponent(selectedId));
    if (!incident) { selectedId = null; return; }
    html = renderIncident(incident);
  }
  if (html === lastHtml) { return; }          // nothing changed: keep the page as it is
  lastHtml = html;
  var detail = document.getElementById("detail");
  detail.innerHTML = html;
  restorePanels();
  var reviewer = document.getElementById("reviewer");
  if (reviewer && reviewer.value === "") { reviewer.value = readSaved("reviewer"); }
}

function renderIntro() {
  return (
    '<section class="intro">' +
      '<p class="label">LLM incident agent &middot; LangGraph &middot; hybrid RAG</p>' +
      "<h1>Finds the root cause.<br><em>Proves it.</em> Then asks you.</h1>" +
      '<p class="lede">Pick a drill on the left. The agent investigates with read-only tools, keeps a board of competing ' +
      "hypotheses, writes a root cause where every claim is checked against evidence, and changes nothing until a policy " +
      "check and a human say yes. Recovery is then checked from the service status, not by asking the LLM.</p>" +
      '<div class="guards">' +
        '<div class="guard"><span class="num">01 / grounding</span><h3>Claims are checked by code</h3>' +
          "<p>Every version, number and error name in the RCA must appear in the evidence it cites. Confidence is capped by what was verified.</p></div>" +
        '<div class="guard"><span class="num">02 / guardrail</span><h3>Logs cannot give orders</h3>' +
          "<p>Instruction-like text in tool output is quarantined before the LLM reads it, and shown to you instead.</p></div>" +
        '<div class="guard"><span class="num">03 / control</span><h3>The LLM only proposes</h3>' +
          "<p>Plain-Python policy, then human approval, then verification. A failed fix re-opens the investigation.</p></div>" +
      "</div>" +
      '<p class="honesty">Tools replay recorded scenario evidence (logs, metrics, deploys written as JSON files), and verification is replay-based, so every run is ' +
      "repeatable and can be scored. Swap the tool bodies for Prometheus, Loki or Kubernetes calls and the agent does not change.</p>" +
    "</section>"
  );
}

function renderIncident(incident) {
  var state = incident.state || {};
  var html = renderHeader(incident, state);
  html += renderFlight(incident, state);
  html += renderBanner(incident, state);
  html +=
    '<div class="columns">' +
      '<div class="stack">' + renderRca(state) + renderEvidence(state) + "</div>" +
      '<div class="stack">' + renderBoard(state) + renderTimeline(state) + "</div>" +
    "</div>";
  return html;
}

function renderHeader(incident, state) {
  var alert = state.alert || {};
  var report = "";
  if (state.report) {
    report = '<a class="link-button" href="/incidents/' + esc(incident.id) + '/report" target="_blank" rel="noopener">report.md</a>';
  }
  var seconds = Math.max(0, incident.updated_at - incident.created_at);
  return (
    '<section class="incident-head">' +
      '<div class="incident-meta">' +
        '<span class="service-tag"><span class="sev ' + esc(incident.severity) + '"></span>' + esc(incident.service) + " &middot; " + esc(incident.severity) + "</span>" +
        "<span>" + esc(incident.id) + "</span>" +
        '<span class="status ' + esc(incident.status) + '">' + esc(prettyStatus(incident.status)) + "</span>" +
        "<span>" + seconds.toFixed(1) + "s since the alert</span>" +
        '<span class="head-actions">' + report + "</span>" +
      "</div>" +
      '<h1 class="incident-title">' + esc(incident.title) + "</h1>" +
      '<p class="incident-desc">' + esc(alert.description || incident.description) + "</p>" +
    "</section>"
  );
}

function currentNode(timeline) {
  // The node of the most recent event (an "error" event does not move the path).
  for (var i = timeline.length - 1; i >= 0; i--) {
    var stage = timeline[i].stage;
    if (stage in STAGE_TO_NODE) { return STAGE_TO_NODE[stage]; }
  }
  return 0;
}

function lastWorkNode(timeline) {
  // For an escalated incident: the last node that did real work before the hand-over.
  for (var i = timeline.length - 1; i >= 0; i--) {
    var node = STAGE_TO_NODE[timeline[i].stage];
    if (node !== undefined && node < 7) { return node; }
  }
  return 0;
}

function renderFlight(incident, state) {
  var timeline = state.timeline || [];
  var status = incident.status;
  var current = currentNode(timeline);
  var reached = current;
  if (status === "escalated") { reached = lastWorkNode(timeline); }

  var evidence = state.evidence || [];
  var toolSteps = 0;
  for (var i = 0; i < evidence.length; i++) {
    if (evidence[i].tool !== "verify_after_fix") { toolSteps++; }
  }
  var notes = ["", "", "", "", "", "", "", ""];
  var classes = ["", "", "", "", "", "", "", ""];

  for (var n = 0; n < NODES.length; n++) {
    if (status === "resolved" || status === "escalated") {
      classes[n] = n <= reached ? "done" : "";
    } else if (n < current) {
      classes[n] = "done";
    } else if (n === current) {
      classes[n] = status === "error" ? "fail" : "active";
    }
  }

  if (toolSteps > 0) { notes[0] = toolSteps + " tool calls"; }
  if (state.attempts > 0 && current === 0 && status === "running") { notes[0] = "attempt " + (state.attempts + 1); }
  if (state.rca) {
    var verified = 0;
    var claims = state.rca.claims || [];
    for (var c = 0; c < claims.length; c++) { if (claims[c].verified) { verified++; } }
    notes[1] = verified + "/" + claims.length + " claims ok";
    if (state.rca.inconclusive && reached >= 1) { classes[1] = "fail"; notes[1] = "inconclusive"; }
  }
  if (state.plan && reached >= 2) {
    notes[2] = state.plan.action.replace("_", " ");
    if (state.plan.action === "escalate") { classes[2] = "fail"; }
  }
  if (state.policy && reached >= 3) {
    notes[3] = state.policy.decision === "deny" ? "denied" : "allowed";
    if (state.policy.decision === "deny") { classes[3] = "fail"; }
  }
  if (state.approval && reached >= 4) {
    notes[4] = state.approval.status;
    if (state.approval.status === "rejected") { classes[4] = "fail"; }
  }
  if (state.action_result && reached >= 5) {
    notes[5] = state.action_result.ok ? "done" : "failed";
    if (!state.action_result.ok) { classes[5] = "fail"; }
  }
  if (state.verification) {
    if (state.verification.healthy) {
      notes[6] = "healthy";
    } else {
      notes[6] = "still broken";
      classes[6] = "warn";
    }
  }
  if (status === "resolved") { classes[7] = "ok"; notes[7] = "resolved"; }
  if (status === "escalated") { classes[7] = "warn"; notes[7] = "to a human"; }

  var html = '<nav class="flight" aria-label="Workflow progress">';
  for (var k = 0; k < NODES.length; k++) {
    var name = NODES[k];
    if (k === 7 && status === "escalated") { name = "escalate"; }
    html += '<div class="node ' + classes[k] + '"><span class="dot"></span>' +
            '<span class="node-name">' + esc(name) + '</span><span class="node-note">' + esc(notes[k]) + "</span></div>";
  }
  return html + "</nav>";
}

function renderBanner(incident, state) {
  var status = incident.status;

  if (status === "waiting_approval" && state.approval) {
    var a = state.approval;
    var policy = state.policy ? state.policy.reason : "";
    return (
      '<section class="banner signal ticket">' +
        "<div>" +
          '<p class="label">Change request &middot; attempt ' + esc(a.attempt) + " &middot; waiting for you</p>" +
          '<div class="ticket-action">' + esc(a.action) + ' <span class="args">' + esc(formatArguments(a.arguments)) + "</span></div>" +
          "<p>on <b>" + esc(a.service) + "</b>. " + esc(a.reason) + "</p>" +
          '<div class="policy-line">&#10003; policy check passed: ' + esc(policy) + "</div>" +
        "</div>" +
        '<div class="decide">' +
          '<input id="reviewer" placeholder="Your name" autocomplete="name">' +
          '<input id="comment" placeholder="Comment (optional)">' +
          '<div class="decide-buttons">' +
            '<button class="btn primary" data-decide="yes">Approve</button>' +
            '<button class="btn ghost" data-decide="no">Reject</button>' +
          "</div>" +
        "</div>" +
      "</section>"
    );
  }
  if (status === "resolved") {
    var checks = (state.verification && state.verification.checks) || [];
    var parts = [];
    for (var i = 0; i < checks.length; i++) { parts.push(checks[i].name + " (" + checks[i].detail + ")"); }
    return '<section class="banner ok"><h3>Resolved and verified</h3><p>Replay-based check of the service status, not by the LLM: ' +
           esc(parts.join(" · ")) + "</p></section>";
  }
  if (status === "escalated") {
    return '<section class="banner warn"><h3>Handed over to a human</h3><p>' + esc(state.escalation_reason) +
           ". Nothing was changed without approval.</p></section>";
  }
  if (status === "error") {
    return '<section class="banner bad"><h3>The workflow stopped</h3><p>The last checkpoint is saved. ' +
           'Retry continues from there, no work is lost.</p><p style="margin-top:10px">' +
           '<button class="link-button" data-retry="1">Retry from checkpoint</button></p></section>';
  }
  return "";
}

function renderRca(state) {
  var rca = state.rca;
  if (!rca) {
    return '<section class="card"><div class="card-head"><h2 class="label">Root cause</h2></div>' +
           '<p class="empty">Written after the investigation. Every claim will be checked against the evidence.</p></section>';
  }
  var claims = rca.claims || [];
  var verified = 0;
  var items = "";
  for (var i = 0; i < claims.length; i++) {
    var claim = claims[i];
    if (claim.verified) { verified++; }
    var missing = "";
    if (!claim.verified && claim.missing_facts.length > 0) {
      missing = '<span class="missing">not in cited evidence: ' + esc(claim.missing_facts.join(", ")) + "</span>";
    }
    items +=
      '<li class="claim ' + (claim.verified ? "verified" : "unbacked") + '">' +
        '<span class="mark">' + (claim.verified ? "&#10003;" : "&#10007;") + "</span>" +
        '<div><span class="claim-text">' + esc(claim.text) + "</span>" +
        '<div class="claim-foot">' + citeChips(claim.evidence_ids) + missing + "</div></div>" +
      "</li>";
  }

  var final = Math.round(rca.confidence * 100);
  var llm = Math.round((rca.llm_confidence || 0) * 100);
  var title = rca.inconclusive ? "Inconclusive" : "Root cause";
  var missingEvidence = "";
  if (rca.inconclusive && rca.missing_evidence.length > 0) {
    missingEvidence = '<p class="small muted" style="margin:0 0 14px">Missing: ' + esc(rca.missing_evidence.join("; ")) + "</p>";
  }
  return (
    '<section class="card">' +
      '<div class="card-head"><h2 class="label">' + title + "</h2>" +
      '<span class="muted small mono">' + esc(rca.category) + "</span></div>" +
      '<p class="root-cause' + (rca.inconclusive ? " inconclusive" : "") + '">' + esc(rca.root_cause) + "</p>" +
      missingEvidence +
      '<div class="confidence">' +
        '<div class="meter"><div class="meter-fill" style="width:' + final + '%"></div>' +
        '<div class="meter-llm" style="left:calc(' + llm + '% - 1px)" title="What the LLM said"></div></div>' +
        '<div class="confidence-text">confidence <strong>' + (rca.confidence).toFixed(2) + "</strong> &middot; " +
        '<span class="llm">LLM said ' + (rca.llm_confidence || 0).toFixed(2) + "</span> &middot; capped by grounding: " +
        verified + "/" + claims.length + " claims backed</div>" +
      "</div>" +
      '<ul class="claims">' + items + "</ul>" +
    "</section>"
  );
}

function renderEvidence(state) {
  var evidence = state.evidence || [];
  if (evidence.length === 0) {
    return '<section class="card"><div class="card-head"><h2 class="label">Evidence</h2></div>' +
           '<p class="empty">The agent is choosing its first tool&hellip;</p></section>';
  }
  var html = "";
  for (var i = 0; i < evidence.length; i++) {
    var e = evidence[i];
    var flags = "";
    if (!e.ok) { flags += '<span class="flag">tool error</span>'; }
    if (e.flags && e.flags.indexOf("prompt_injection") !== -1) { flags += '<span class="flag">prompt injection quarantined</span>'; }
    var quarantine = "";
    if (e.quarantined && e.quarantined.length > 0) {
      quarantine = '<div class="quarantine"><p class="label">Quarantined &middot; never sent to the LLM</p>';
      for (var q = 0; q < e.quarantined.length; q++) { quarantine += '<div class="q-text">' + esc(e.quarantined[q]) + "</div>"; }
      quarantine += "</div>";
    }
    var kind = e.tool === "verify_after_fix" ? "VERIFY" : e.kind;
    html +=
      '<article class="evidence' + (e.ok ? "" : " failed") + '" id="ev-' + esc(e.id) + '">' +
        '<div class="evidence-top"><span class="ev-id">' + esc(e.id) + '</span><span class="ev-tool">' + esc(e.tool) + "</span>" +
        '<span class="kind">' + esc(kind) + "</span>" + flags + "</div>" +
        '<p class="ev-summary">' + esc(e.summary) + "</p>" + quarantine +
        '<details class="raw" data-panel="' + esc(e.id) + '"><summary>raw tool output</summary><pre>' +
        esc(JSON.stringify(e.data, null, 2)) + "</pre></details>" +
      "</article>";
  }
  return '<section class="card"><div class="card-head"><h2 class="label">Evidence</h2>' +
         '<span class="muted small">' + evidence.length + " items</span></div>" +
         '<div class="evidence-list">' + html + "</div></section>";
}

function renderBoard(state) {
  var board = state.hypotheses || [];
  if (board.length === 0) {
    return '<section class="card"><div class="card-head"><h2 class="label">Hypothesis board</h2></div>' +
           '<p class="empty">Competing root causes appear here as the agent investigates.</p></section>';
  }
  var html = "";
  for (var i = 0; i < board.length; i++) {
    var h = board[i];
    html +=
      '<li class="hyp ' + esc(h.status) + '"><span class="hyp-id">' + esc(h.id) + "</span>" +
        '<div><span class="hyp-state">' + esc(h.status) + '</span><div class="hyp-text">' + esc(h.statement) + "</div>" +
        '<div class="hyp-foot">' + citeChips(h.evidence_ids) + "</div></div>" +
      "</li>";
  }
  return '<section class="card"><div class="card-head"><h2 class="label">Hypothesis board</h2>' +
         '<span class="muted small">differential diagnosis</span></div><ul class="board">' + html + "</ul></section>";
}

function renderTimeline(state) {
  var timeline = state.timeline || [];
  if (timeline.length === 0) {
    return '<section class="card"><div class="card-head"><h2 class="label">Timeline</h2></div><p class="empty">Starting&hellip;</p></section>';
  }
  var html = "";
  for (var i = timeline.length - 1; i >= 0; i--) {         // newest first
    var event = timeline[i];
    html += '<li class="stage-' + esc(event.stage) + '"><span class="t-time">' + esc(event.time) + "</span>" +
            '<span class="t-stage">' + esc(event.stage) + '</span><span class="t-msg">' + esc(event.message) + "</span></li>";
  }
  return '<section class="card"><div class="card-head"><h2 class="label">Timeline</h2>' +
         '<span class="muted small">newest first</span></div><ul class="timeline">' + html + "</ul></section>";
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

function showEvidence(id, scroll) {
  var card = document.getElementById("ev-" + id);
  if (!card) { return; }
  card.classList.add("lit");
  if (scroll) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(function () { card.classList.remove("lit"); }, 1600);
  }
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
  else if (target.dataset.ev) { showEvidence(target.dataset.ev, true); }
});

document.addEventListener("toggle", function (event) {
  var panel = event.target;
  if (panel.dataset && panel.dataset.panel) { openPanels[panel.dataset.panel] = panel.open; }
}, true);

document.addEventListener("mouseover", function (event) {
  var chip = event.target.closest(".cite");
  if (chip) { showEvidence(chip.dataset.ev, false); }
});

document.addEventListener("mouseout", function (event) {
  var chip = event.target.closest(".cite");
  if (!chip) { return; }
  var card = document.getElementById("ev-" + chip.dataset.ev);
  if (card) { card.classList.remove("lit"); }
});

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
