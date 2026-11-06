/* Local evaluator. Source text is untrusted: render through textContent only. */
"use strict";
const $ = id => document.getElementById(id);
let responseData = null, busy = false;
let conversationData = null;
let sessionsEnabled = false, selectedSessionId = null, savedSessions = [];
let hasSavedContext = false;
const json = value => JSON.stringify(value, null, 2);
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function details(parent, title, value) {
  const d = el("details"); d.append(el("summary", title), el("pre", typeof value === "string" ? value : json(value))); parent.append(d);
}
function tab(name) {
  document.querySelectorAll(".view").forEach(node => { node.hidden = node.id !== name; });
  document.querySelectorAll("[data-tab]").forEach(node => { const active = node.dataset.tab === name; node.classList.toggle("active", active); node.setAttribute("aria-selected", String(active)); });
}
document.querySelectorAll("[data-tab]").forEach(node => node.addEventListener("click", () => tab(node.dataset.tab)));
$("compare").addEventListener("change", () => { $("location-b").hidden = !$("compare").checked; });
function scope() {
  return {radius_meters: Number($("radius").value), time_range: {start: $("start").value, end: $("end").value, calendar_timezone: $("timezone").value}};
}
function selectedLocation(id) {
  if (id === "B" && !$("compare").checked) throw new Error("Enable location B before selecting it.");
  return {id, latitude: Number($("lat-" + id.toLowerCase()).value), longitude: Number($("lng-" + id.toLowerCase()).value)};
}
function request() {
  return {question: $("question").value, locations: $("compare").checked ? [selectedLocation("A"), selectedLocation("B")] : [selectedLocation("A")], ...scope(),
    preferences: [$("domain-collisions").checked ? "traffic_collisions" : null, $("domain-requests").checked ? "service_requests" : null].filter(Boolean)};
}
function setBusy(value, message) {
  busy = value; $("ask").disabled = value;
  $("manual-run").disabled = value || (addressMode() && conversationData?.status !== "complete");
  $("location-mode").disabled = value;
  $("new-chat").disabled = value || !sessionsEnabled;
  document.querySelectorAll(".session-action").forEach(node => { node.disabled = value; });
  document.querySelectorAll(".conversation-action").forEach(node => { node.disabled = value; });
  document.querySelectorAll(".replay").forEach(node => { node.disabled = value; });
  if (message) $("status").textContent = message;
}
function error(value) { $("error").hidden = !value; $("error").textContent = value || ""; }
async function post(path, body) {
  const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: json(body)});
  const value = await res.json();
  if (!res.ok) throw new Error(typeof value.detail === "string" ? value.detail : json(value.detail || value));
  return value;
}
function claimLink(id) {
  const b = el("button", "Inspect " + id.slice(0, 14) + "…");
  b.addEventListener("click", () => { tab("evidence"); document.getElementById(id)?.scrollIntoView({behavior:"smooth", block:"center"}); }); return b;
}
function render(data) {
  responseData = data; $("download").disabled = false; $("raw-json").textContent = json(data);
  const answer = $("answer"); answer.replaceChildren(el("p", "THE AGENT’S RESPONSE", "eyebrow"), el("span", data.status, "badge"), el("h2", data.intent.replaceAll("_", " ")), el("p", data.answer, "answer-text"));
  if (data.limitation_codes?.length) answer.append(el("p", "Diagnostics: " + data.limitation_codes.join(", "), "hint"));
  for (const claim of data.claims || []) {
    const block = el("div", undefined, "log"); block.append(el("p", claim.text));
    const links = el("div", undefined, "actions"); claim.evidence_ids.forEach(id => links.append(claimLink(id))); block.append(links); answer.append(block);
  }
  if (data.comparison?.metrics?.length) {
    const t = el("table"), head = el("tr"); ["Metric", "A", "B", "B − A", "Circles overlap"].forEach(x => head.append(el("th", x))); t.append(head);
    data.comparison.metrics.forEach(m => { const row = el("tr"); [m.metric, `${m.location_a}: ${m.value_a}`, `${m.location_b}: ${m.value_b}`, m.difference_b_minus_a, m.overlapping_circles ? "Yes · shared records possible" : "No"].forEach(x => row.append(el("td", String(x)))); t.append(row); });
    const wrap = el("div", undefined, "table-scroll"); wrap.append(t); answer.append(wrap);
  }
  if (data.limitations?.length) details(answer, "Limitations and uncertainty", data.limitations);
  const calls = $("calls"); calls.replaceChildren(el("h2", "Executed tool calls"), el("p", "Logs appear after completion. These are executed arguments and results, not hidden LLM reasoning.", "muted"));
  details(calls, "Validated execution plan", data.execution_plan || []);
  if (!data.tool_calls?.length) calls.append(el("p", "No database tools were executed."));
  (data.tool_calls || []).forEach((log, i) => {
    const block = el("div", undefined, "log"); block.append(el("h3", `${i + 1}. ${log.tool} · ${log.arguments.location.id}`), el("span", `${log.status} · ${log.elapsed_ms.toFixed(1)} ms`, "badge"), el("p", log.query_id, "hint"));
    if (log.error_code) block.append(el("p", log.error_code, "error"));
    details(block, "Exact arguments", log.arguments); details(block, "SQL and bound parameters", {sql:log.parameterized_sql, parameters:log.parameters}); details(block, "SQL result rows", log.result_rows); details(block, "Copyable read-only SQL for psql / pgAdmin", log.read_only_sql);
    const actions = el("div", undefined, "actions"), replay = el("button", "Replay without LLM", "replay"), copy = el("button", "Copy SQL");
    replay.addEventListener("click", () => replayCall(log)); copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(log.read_only_sql); copy.textContent = "Copied"; } catch (_) { error("Clipboard unavailable. Expand the SQL and copy it manually."); } }); actions.append(replay, copy); block.append(actions); calls.append(block);
  });
  const evidence = $("evidence"); evidence.replaceChildren(el("h2", "Evidence records"));
  (data.evidence || []).forEach(e => {
    const card = el("article", undefined, "evidence-card"); card.id = e.evidence_id;
    card.append(el("span", `${e.domain} · ${e.location_id}`, "badge"), el("h3", `${e.value === null ? "Unavailable" : e.value} · ${e.metric}`), el("p", e.metric_definition), el("p", e.evidence_id, "hint"));
    details(card, "Scope and filters", {scope:e.scope, filters:e.filters}); details(card, "Provenance and quality", {provenance:e.provenance, quality:e.quality});
    if (e.groups?.length) details(card, "Service request groups", e.groups);
    if (e.buckets?.length) details(card, "Calendar buckets (including explicitly marked filled buckets)", e.buckets);
    if (e.records?.length) details(card, "Source report examples · untrusted data", e.records);
    details(card, "Caveats", e.caveats); evidence.append(card);
  });
  if (!data.evidence?.length) evidence.append(el("p", "No evidence was collected for this request."));
  $("verification-result").replaceChildren();
}
async function replayCall(log) {
  if (busy) return;
  const expected = responseData?.evidence?.find(e => e.query_id === log.query_id);
  await executeManual(log.arguments, expected);
}
async function executeManual(args, expected = null) {
  if (busy) return;
  setBusy(true, "Executing the SQL function directly…"); error(null); tab("verify");
  try {
    const result = await post("/api/v1/tools/run", args), area = $("verification-result");
    area.replaceChildren(el("h3", "Independent SQL result"));
    area.append(el("p", `${result.tool_call.status} · value: ${result.evidence.value === null ? "unavailable" : result.evidence.value}`));
    if (expected) {
      const comparable = expected.value !== null && result.evidence.value !== null;
      area.append(el("p", comparable ? (expected.value === result.evidence.value ? "MATCH: the SQL value matches the earlier evidence." : `DIFFERENCE: earlier evidence ${expected.value}; current SQL ${result.evidence.value}. Check for data updates.`) : "Comparison unavailable: at least one execution has no valid value.", "notice"));
    }
    details(area, "Executed read-only SQL", result.tool_call.read_only_sql); details(area, "SQL rows", result.tool_call.result_rows); details(area, "New evidence record", result.evidence);
    $("status").textContent = "SQL verification completed · no LLM call";
  } catch (e) { error(e.message); $("status").textContent = "SQL verification failed"; }
  finally { setBusy(false); }
}
$("investigate").addEventListener("submit", async event => {
  event.preventDefault(); if (busy) return;
  await configurationReady;
  if (addressMode() || (selectedSessionId && hasSavedContext && $("continue-context").checked)) {
    let action = "ask";
    const message = $("question").value.trim();
    if (pendingConversation()) {
      action = conversationData.status === "needs_clarification" ? "clarify" : "correct";
      if (/^(yes|confirm|confirmed)[.!]?$/i.test(message) && conversationData.status === "needs_confirmation") action = "confirm";
      if (/^cancel[.!]?$/i.test(message)) action = "cancel";
    }
    await conversationTurn(action, {message});
    return;
  }
  conversationData = null;
  setBusy(true, "Planning and executing… tool logs appear on completion."); error(null);
  try {
    await ensureSession();
    const body = request(); if (selectedSessionId) body.session_id = selectedSessionId;
    const data = await post("/api/v1/agent/query", body); render(data); tab("answer"); $("status").textContent = `${data.status} · ${data.tool_calls?.length || 0} calls · ${data.tool_trace_id}`;
    await refreshSavedConversation();
  }
  catch (e) { error(e.message); $("status").textContent = "Request failed"; }
  finally { setBusy(false); }
});
$("manual-run").addEventListener("click", async () => {
  if (addressMode() && conversationData?.status !== "complete") { error("Confirm and resolve the current addresses first, or select coordinate pins for manual SQL."); return; }
  try { const filters = JSON.parse($("filters").value); await executeManual({name:$("manual-tool").value, location:selectedLocation($("manual-location").value), ...scope(), collision_filters:filters, period:$("period").value, exclude_linked_duplicates:$("exclude").checked, limit:Math.min(100, window.configuredMaxRows || 100)}); }
  catch (e) { error(e.message); }
});
document.querySelectorAll("[data-preset]").forEach(button => button.addEventListener("click", () => {
  if (busy) return;
  $("location-mode").value = "coordinates"; conversationData = null; syncLocationMode();
  $("question-label").textContent = "Your question"; $("ask").textContent = "Ask the agent →";
  const mode = button.dataset.preset; $("compare").checked = mode !== "count"; $("location-b").hidden = mode === "count";
  $("continue-context").checked = false;
  $("domain-collisions").checked = mode === "compare"; $("domain-requests").checked = mode === "compare";
  $("question").value = mode === "compare" ? "Compare apartments A and B based on Get It Done reports and traffic collisions." : mode === "crime" ? "Which apartment has a lower crime rate?" : "How many traffic collisions were recorded near location A during this period?";
}));
$("download").addEventListener("click", () => {
  if (!responseData) return;
  const url = URL.createObjectURL(new Blob([json(conversationData || responseData)], {type:"application/json"}));
  const link = el("a"); link.href = url; link.download = "citylens-investigation.json"; link.click(); URL.revokeObjectURL(url);
});
async function loadConfiguration() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch("/api/v1/status", {signal: controller.signal});
    if (!response.ok) throw new Error("Status request failed");
    const s = await response.json();
    window.configuredMaxRows = s.max_rows;
    $("configuration").textContent = `OpenAI: ${s.openai_configured ? "configured" : "not configured"} · Database: ${s.database_configured ? "configured" : "not configured"}. Configuration presence only; connectivity is checked on execution.`;
    sessionsEnabled = s.sessions_configured === true;
  } catch (_) {
    $("configuration").textContent = "Could not read server configuration within 5 seconds. Check FastAPI and reload the page.";
  } finally {
    clearTimeout(timeout);
  }
  if (sessionsEnabled) {
    try {
      await loadHistory();
      const saved = currentSession();
      if (saved) await selectSession(saved);
    } catch (e) { error(e.message); $("history-status").textContent = "Conversation storage could not be loaded. Retry by reloading the page."; }
  } else $("history-status").textContent = "Conversation storage is not configured. Current requests will not be saved.";
  $("new-chat").disabled = !sessionsEnabled;
}
const configurationReady = loadConfiguration();

function addressMode() { return $("location-mode").value !== "coordinates"; }
function pendingConversation() { return conversationData && !["complete", "cancelled", "error"].includes(conversationData.status); }
function syncLocationMode() {
  $("coordinate-inputs").hidden = addressMode();
  $("address-hint").hidden = !addressMode();
  $("location-confirmation").hidden = !addressMode() || !conversationData;
  ["lat-a", "lng-a", "lat-b", "lng-b", "compare"].forEach(id => { $(id).disabled = addressMode(); });
  ["start", "end", "radius"].forEach(id => { $(id).required = !addressMode(); });
  setBusy(busy);
}
$("location-mode").addEventListener("change", async () => {
  if (pendingConversation()) await conversationTurn("cancel");
  conversationData = null;
  $("question-label").textContent = "Your question";
  $("ask").textContent = "Ask the agent →";
  syncLocationMode();
});

async function conversationTurn(action, extra = {}) {
  if (busy) return;
  const defaults = scope();
  if (!$("start").value.trim() || !$("end").value.trim()) defaults.time_range = null;
  if (!$("radius").value.trim()) defaults.radius_meters = 800;
  const body = action === "ask" ? {...extra, ...defaults,
    preferences: [$("domain-collisions").checked ? "traffic_collisions" : null, $("domain-requests").checked ? "service_requests" : null].filter(Boolean)} :
    {conversation_id: conversationData.conversation_id, revision: conversationData.revision, ...extra};
  setBusy(true, action === "ask" ? "Interpreting addresses and query conditions…" : "Processing your confirmed conversation step…");
  error(null);
  try {
    await ensureSession();
    if (selectedSessionId) body.session_id = selectedSessionId;
    const data = await post("/api/v1/agent/chat", {action, ...body});
    conversationData = data;
    if (!["complete", "cancelled", "error"].includes(data.status)) $("location-mode").value = "addresses";
    renderConversation(data);
    $("status").textContent = data.status.replaceAll("_", " ");
    await refreshSavedConversation();
  } catch (e) { error(e.message); $("status").textContent = "Conversation step failed"; }
  finally { setBusy(false); }
}

function renderConversation(data) {
  const panel = $("location-confirmation");
  panel.hidden = false;
  panel.replaceChildren(el("h2", data.status === "complete" ? "Resolved locations" : "Review locations"), el("p", data.message));
  if (data.diagnostic_codes?.length) panel.append(el("p", "Diagnostics: " + data.diagnostic_codes.join(", "), "error"));
  if (data.original_question) details(panel, "Original analytical question", data.original_question);
  for (const address of data.locations || []) {
    panel.append(el("p", `${address.id}: ${[address.house_number, address.direction, address.street_name, address.street_suffix, address.post_direction].filter(Boolean).join(" ")}, ${address.city || "San Diego"}, ${address.state || "CA"}${address.postal_code ? " " + address.postal_code : ""}`));
  }
  if (data.time_range) panel.append(el("p", `Conditions: ${data.radius_meters} meters · ${data.time_range.start} inclusive to ${data.time_range.end} exclusive · ${data.time_range.calendar_timezone}`));
  if (data.proposed_plan) details(panel, "Proposed analysis and filters", data.proposed_plan);
  for (const resolved of data.resolved_locations || []) {
    panel.append(el("p", `${resolved.location.id} resolved: ${resolved.matched_address} · ${resolved.provider} · ${resolved.precision.replaceAll("_", " ")}`));
  }
  if (data.result) {
    render(data.result);
    $("raw-json").textContent = json(data);
    data.resolved_locations.forEach((r, index) => {
      const slot = index === 0 ? "a" : "b";
      $("lat-" + slot).value = r.location.latitude;
      $("lng-" + slot).value = r.location.longitude;
    });
    $("compare").checked = data.resolved_locations.length === 2;
    $("location-b").hidden = !$("compare").checked;
    $("radius").value = data.radius_meters;
    $("start").value = data.time_range.start; $("end").value = data.time_range.end; $("timezone").value = data.time_range.calendar_timezone;
    tab("answer");
  } else {
    responseData = data;
    $("download").disabled = false;
    $("raw-json").textContent = json(data);
    $("answer").replaceChildren(el("h2", "Waiting for location confirmation"), el("p", data.message));
    $("calls").replaceChildren(el("h2", "Tool calls"), el("p", "No analytical tools were executed for this pending request."));
    $("evidence").replaceChildren(el("h2", "Evidence"), el("p", "Evidence is collected after all locations are confirmed and resolved."));
    $("verification-result").replaceChildren();
    tab("answer");
  }
  const actions = el("div", undefined, "actions");
  if (data.status === "needs_confirmation") {
    const confirm = el("button", "Confirm addresses and run", "conversation-action");
    confirm.type = "button"; confirm.addEventListener("click", () => conversationTurn("confirm")); actions.append(confirm);
  }
  if (data.status === "needs_candidate_confirmation") {
    const selects = {};
    for (const [id, candidates] of Object.entries(data.candidates)) {
      const select = el("select"), label = el("label", `${id}: choose the provider location you intended`);
      select.id = `candidate-choice-${id}`; label.htmlFor = select.id;
      const placeholder = el("option", "Choose an address…"); placeholder.value = ""; select.append(placeholder);
      candidates.forEach(c => { const point = c.location ? ` · ${c.location.latitude.toFixed(5)}, ${c.location.longitude.toFixed(5)}` : ""; const option = el("option", `${c.matched_address} · ${c.provider || "Census"} · ${(c.precision || "unknown").replaceAll("_", " ")}${point}`); option.value = c.candidate_id; select.append(option); });
      panel.append(label, select); selects[id] = select;
    }
    const choose = el("button", "Confirm selected provider candidates", "conversation-action"); choose.type = "button";
    choose.addEventListener("click", () => {
      const choices = Object.fromEntries(Object.entries(selects).map(([id, select]) => [id, select.value]));
      if (Object.values(choices).some(value => !value)) { error("Explicitly select a candidate for every pending location."); return; }
      conversationTurn("choose_candidates", {candidate_choices: choices});
    }); actions.append(choose);
  }
  if (pendingConversation()) {
    const cancel = el("button", "Cancel request", "conversation-action"); cancel.type = "button";
    cancel.addEventListener("click", () => conversationTurn("cancel")); actions.append(cancel);
    panel.append(el("p", "To correct an address or clarify the request, type your update in the question box. Revised addresses require confirmation again.", "hint"));
    $("question").value = "";
    $("question").placeholder = "For example: A is correct, but B should be 9500 Gilman Dr.";
    $("question-label").textContent = "Correction or clarification";
    $("ask").textContent = "Send update →";
  } else {
    $("question-label").textContent = "Your next question"; $("ask").textContent = "Ask the agent →";
    $("question").placeholder = "Ask a new question with one or two San Diego street addresses.";
    if (["complete", "cancelled"].includes(data.status)) $("question").value = "";
  }
  panel.append(actions);
}
syncLocationMode();

function currentSession(value) {
  try {
    if (value === undefined) return window.localStorage?.getItem("citylens.session_id");
    if (value) window.localStorage?.setItem("citylens.session_id", value);
    else window.localStorage?.removeItem("citylens.session_id");
  } catch (_) { /* Storage may be blocked; PostgreSQL history still works. */ }
  return null;
}
async function readSaved(path, method = "GET") {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(path, {method, signal: controller.signal});
    if (response.status === 204) return null;
    const value = await response.json();
    if (!response.ok) {
      const failure = new Error(typeof value.detail === "string" ? value.detail : "Conversation request failed");
      failure.status = response.status; throw failure;
    }
    return value;
  } finally { clearTimeout(timeout); }
}
async function loadHistory(more = false) {
  const page = await readSaved(`/api/v1/sessions?limit=100&offset=${more ? savedSessions.length : 0}`);
  savedSessions = more ? savedSessions.concat(page) : page;
  $("history-more").hidden = page.length < 100;
  const list = $("session-list"); list.replaceChildren();
  for (const session of savedSessions) {
    const row = el("div", undefined, "session-row");
    const select = el("button", session.title, "session-action session-select");
    select.type = "button"; select.setAttribute("aria-pressed", String(session.session_id === selectedSessionId));
    select.title = `Last activity: ${new Date(session.updated_at).toLocaleString()}`;
    select.addEventListener("click", () => selectSession(session.session_id).catch(e => error(e.message)));
    const remove = el("button", "Delete", "session-action session-delete"); remove.type = "button";
    remove.setAttribute("aria-label", `Delete conversation: ${session.title}`);
    remove.addEventListener("click", () => deleteSession(session).catch(e => error(e.message)));
    row.append(select, remove); list.append(row);
  }
  $("history-status").textContent = savedSessions.length ? "Saved conversations · most recent first" : "No saved conversations yet.";
}
async function ensureSession() {
  if (!sessionsEnabled || selectedSessionId) return;
  const session = await post("/api/v1/sessions", {});
  selectedSessionId = session.session_id; currentSession(selectedSessionId);
}
function clearConversation() {
  conversationData = null; responseData = null; selectedSessionId = null; currentSession(null);
  hasSavedContext = false; $("continue-context-label").hidden = true; $("continue-context").checked = true;
  $("transcript").replaceChildren(); $("transcript").hidden = true;
  $("location-confirmation").replaceChildren(); $("location-confirmation").hidden = true;
  $("answer").replaceChildren(el("h2", "Start a new conversation"), el("p", "Ask a question with locations and a date interval."));
  ["calls", "evidence", "verification-result"].forEach(id => $(id).replaceChildren());
  $("raw-json").textContent = "No response yet."; $("download").disabled = true;
  $("question").value = ""; $("question-label").textContent = "Your question";
  $("ask").textContent = "Ask the agent →"; $("status").textContent = "Ready for a new conversation";
  syncLocationMode(); tab("answer");
}
function restoreScope(context) {
  hasSavedContext = !!context; $("continue-context-label").hidden = !context;
  if (!context) return;
  const req = context.request;
  $("radius").value = req.radius_meters;
  $("start").value = req.time_range.start; $("end").value = req.time_range.end;
  $("timezone").value = req.time_range.calendar_timezone;
  req.locations.forEach((loc, index) => { const slot = index === 0 ? "a" : "b"; $("lat-" + slot).value = loc.latitude; $("lng-" + slot).value = loc.longitude; });
  $("compare").checked = req.locations.length === 2; $("location-b").hidden = req.locations.length !== 2;
  $("domain-collisions").checked = context.plan.tools.some(name => name.includes("traffic_collisions"));
  $("domain-requests").checked = context.plan.tools.some(name => name.includes("service_requests"));
}
function renderTranscript(messages) {
  const panel = $("transcript"); panel.hidden = !messages.length;
  panel.replaceChildren(el("h2", "Conversation"), el("p", "Saved findings reflect their original execution. Follow-up analyses query the database again.", "hint"));
  messages.forEach(message => {
    const entry = el("article", undefined, "message " + message.role);
    entry.append(el("h3", message.role === "user" ? "You" : "CityLens"), el("p", message.content));
    if (message.response) {
      const inspect = el("button", "Inspect saved response", "session-action"); inspect.type = "button";
      inspect.addEventListener("click", () => {
        // Historical inspection must not replace the active confirmation state.
        const response = message.response.result || (message.response_kind === "query" ? message.response : null);
        if (response) { render(response); tab("answer"); }
        else { $("raw-json").textContent = json(message.response); tab("json"); }
      }); entry.append(inspect);
    }
    panel.append(entry);
  });
}
async function selectSession(id) {
  if (busy) return;
  setBusy(true, "Loading saved conversation…"); error(null);
  try {
    const saved = await readSaved("/api/v1/sessions/" + encodeURIComponent(id));
    clearConversation(); selectedSessionId = id; currentSession(id);
    $("location-mode").value = saved.response_kind === "query" ? "coordinates" : "addresses";
    restoreScope(saved.query_context);
    if (saved.latest_response) {
      if (saved.response_kind === "chat") { conversationData = saved.latest_response; renderConversation(conversationData); }
      else render(saved.latest_response);
    }
    renderTranscript(saved.messages); syncLocationMode();
    $("status").textContent = "Saved conversation restored";
    await loadHistory();
  } catch (e) {
    if (e.status === 404) { clearConversation(); await loadHistory(); }
    throw e;
  } finally { setBusy(false); }
}
async function refreshSavedConversation() {
  if (!sessionsEnabled || !selectedSessionId) return;
  // A history refresh failure must not discard a successfully verified answer.
  try {
    const saved = await readSaved("/api/v1/sessions/" + selectedSessionId);
    if (!pendingConversation()) restoreScope(saved.query_context);
    renderTranscript(saved.messages); await loadHistory();
  } catch (e) { error("The answer completed, but conversation history could not be refreshed: " + e.message); }
}
async function deleteSession(session) {
  if (busy || !window.confirm(`Delete “${session.title}” and all its saved messages and query context?`)) return;
  setBusy(true, "Deleting conversation…");
  try {
    await readSaved("/api/v1/sessions/" + session.session_id, "DELETE");
    if (selectedSessionId === session.session_id) clearConversation();
    await loadHistory(); $("status").textContent = "Conversation deleted";
  } finally { setBusy(false); }
}
$("new-chat").addEventListener("click", async () => {
  if (busy || !sessionsEnabled) return;
  setBusy(true, "Creating conversation…"); error(null);
  try {
    const session = await post("/api/v1/sessions", {});
    clearConversation(); selectedSessionId = session.session_id; currentSession(selectedSessionId);
    await loadHistory();
  } catch (e) { error(e.message); }
  finally { setBusy(false); }
});
$("history-more").addEventListener("click", () => { if (!busy) loadHistory(true).catch(e => error(e.message)); });
