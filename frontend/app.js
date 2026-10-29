/* Local evaluator. Source text is untrusted: render through textContent only. */
"use strict";
const $ = id => document.getElementById(id);
let responseData = null, busy = false;
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
  busy = value; $("ask").disabled = value; $("manual-run").disabled = value;
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
  setBusy(true, "Planning and executing… tool logs appear on completion."); error(null);
  try { const data = await post("/api/v1/agent/query", request()); render(data); tab("answer"); $("status").textContent = `${data.status} · ${data.tool_calls?.length || 0} calls · ${data.tool_trace_id}`; }
  catch (e) { error(e.message); $("status").textContent = "Request failed"; }
  finally { setBusy(false); }
});
$("manual-run").addEventListener("click", async () => {
  try { const filters = JSON.parse($("filters").value); await executeManual({name:$("manual-tool").value, location:selectedLocation($("manual-location").value), ...scope(), collision_filters:filters, period:$("period").value, exclude_linked_duplicates:$("exclude").checked, limit:Math.min(100, window.configuredMaxRows || 100)}); }
  catch (e) { error(e.message); }
});
document.querySelectorAll("[data-preset]").forEach(button => button.addEventListener("click", () => {
  const mode = button.dataset.preset; $("compare").checked = mode !== "count"; $("location-b").hidden = mode === "count";
  $("domain-collisions").checked = mode === "compare"; $("domain-requests").checked = mode === "compare";
  $("question").value = mode === "compare" ? "Compare apartments A and B based on Get It Done reports and traffic collisions." : mode === "crime" ? "Which apartment has a lower crime rate?" : "How many traffic collisions were recorded near location A during this period?";
}));
$("download").addEventListener("click", () => {
  if (!responseData) return;
  const url = URL.createObjectURL(new Blob([json(responseData)], {type:"application/json"}));
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
  } catch (_) {
    $("configuration").textContent = "Could not read server configuration within 5 seconds. Check FastAPI and reload the page.";
  } finally {
    clearTimeout(timeout);
  }
}
loadConfiguration();
