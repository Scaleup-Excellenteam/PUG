"use strict";

const state = { dashboard: null, sentenceOffset: 0, sentenceLimit: 25, action: null };
const actionDefinitions = {
  "reset-analytics": {
    title: "Reset all analytics?",
    description: "This permanently erases every stored search, selection, error and administrative event.",
    phrase: "RESET ANALYTICS",
  },
  "reset-popularity": {
    title: "Reset all popularity data?",
    description: "Every sentence usage_count will be set to zero and the persisted popularity file will be updated.",
    phrase: "RESET POPULARITY",
  },
  "rebuild-index": {
    title: "Build a replacement index?",
    description: "This starts a resource-intensive build from Archive.zip in a separate folder. The active index remains untouched.",
    phrase: "REBUILD INDEX",
  },
};

const byId = (id) => document.getElementById(id);
const number = (value) => new Intl.NumberFormat("en-US").format(value || 0);
const date = (value) => value ? new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "medium" }).format(new Date(value)) : "—";
const duration = (value) => `${Number(value || 0).toFixed(2)} ms`;
const bytes = (value) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(value || 0);
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount.toFixed(index === 0 ? 0 : 2)} ${units[index]}`;
};

function textCell(value, className = "") {
  const cell = document.createElement("td");
  cell.className = className;
  cell.textContent = value ?? "—";
  return cell;
}

function emptyRow(columns, message = "No data has been recorded yet") {
  const row = document.createElement("tr");
  const cell = textCell(message, "cell-muted");
  cell.colSpan = columns;
  row.append(cell);
  return row;
}

function replaceTable(id, rows, columns) {
  byId(id).replaceChildren(...(rows.length ? rows : [emptyRow(columns)]));
}

function renderMetrics(data) {
  const analytics = data.analytics;
  const searches = analytics.searches;
  const popularity = data.corpus.popularity;
  byId("metric-sentences").textContent = number(data.corpus.total_sentences);
  byId("metric-sources").textContent = `${number(data.corpus.source_files)} source files`;
  byId("metric-searches").textContent = number(searches.total);
  byId("metric-unique").textContent = `${number(searches.unique_normalized_queries)} unique queries`;
  byId("metric-success").textContent = `${Number(searches.success_rate_percent).toFixed(1)}%`;
  byId("metric-no-results").textContent = `${number(searches.no_results)} without results`;
  byId("metric-selections").textContent = number(analytics.selections);
  byId("metric-popularity").textContent = `${number(popularity.total_usage)} popularity events`;
  byId("metric-p95").textContent = duration(analytics.performance_ms.p95);
  byId("metric-average").textContent = `${duration(analytics.performance_ms.average)} average`;
  byId("metric-voice").textContent = number(searches.voice);
  byId("metric-typed").textContent = `${number(searches.typed)} typed searches`;
}

function renderActivity(items) {
  const chart = byId("activity-chart");
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Search activity will appear here after the first query.";
    chart.replaceChildren(empty);
    return;
  }
  const maximum = Math.max(...items.map((item) => item.count), 1);
  chart.replaceChildren(...items.map((item) => {
    const column = document.createElement("div"); column.className = "chart-column";
    const value = document.createElement("span"); value.className = "chart-column__value"; value.textContent = number(item.count);
    const bar = document.createElement("span"); bar.className = "chart-column__bar"; bar.style.height = `${Math.max(3, item.count / maximum * 145)}px`;
    const label = document.createElement("span"); label.className = "chart-column__label"; label.textContent = item.hour.slice(5).replace("T", " ");
    column.title = `${item.hour}: ${item.count} searches`; column.append(value, bar, label); return column;
  }));
}

function renderSystem(data) {
  const values = [
    ["Status", "Online"], ["Uptime", `${Math.floor(data.server.uptime_seconds)} seconds`],
    ["Backend", data.configuration.index_backend], ["Ranking", data.configuration.ranking_mode],
    ["Alpha", data.configuration.alpha], ["Node cache", data.configuration.max_node_cache_size],
    ["Python", data.server.python_version], ["Process", data.server.process_id],
  ];
  byId("system-health").replaceChildren(...values.map(([term, description]) => {
    const row = document.createElement("div"); const dt = document.createElement("dt"); const dd = document.createElement("dd");
    dt.textContent = term; dd.textContent = description; row.append(dt, dd); return row;
  }));
}

function renderTopQueries(items) {
  replaceTable("top-queries", items.slice(0, 20).map((item) => {
    const row = document.createElement("tr"); row.append(
      textCell(item.query || item.normalized_query, "cell-sentence"), textCell(number(item.count)),
      textCell(number(item.voice_count)), textCell(number(item.no_result_count)), textCell(duration(item.average_duration_ms)),
    ); return row;
  }), 5);
}

function renderTopSelections(items) {
  replaceTable("top-selections", items.slice(0, 20).map((item) => {
    const row = document.createElement("tr"); row.append(
      textCell(item.completed_sentence, "cell-sentence"), textCell(`${item.source_text}:${item.offset}`, "cell-muted"), textCell(number(item.count)),
    ); return row;
  }), 3);
}

function renderSources(items) {
  replaceTable("source-files", items.map((item) => {
    const row = document.createElement("tr"); row.append(
      textCell(item.source_path, "cell-sentence"), textCell(number(item.sentences)),
      textCell(number(item.searchable)), textCell(number(item.original_characters)),
    ); return row;
  }), 4);
}

function renderStorage(items) {
  replaceTable("storage-files", items.map((item) => {
    const row = document.createElement("tr"); row.append(textCell(item.name), textCell(bytes(item.bytes)), textCell(date(item.modified_at), "cell-muted")); return row;
  }), 3);
}

function eventDetails(event) {
  if (event.event_type === "search") return `“${event.query}” · ${event.result_count} results · ${duration(event.duration_ms)} · ${event.input_method}`;
  if (event.event_type === "selection") return `#${event.sentence_id} · ${event.completed_sentence} · usage ${event.usage_count}`;
  if (event.event_type === "admin_action") return String(event.action || "Administrative action");
  if (event.event_type === "error") return `${event.operation || "operation"}: ${event.error_message || event.error_type}`;
  return Object.entries(event).filter(([key]) => !["timestamp", "event_type", "session_id", "client_address", "user_agent"].includes(key)).map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`).join(" · ") || "—";
}

function renderRecentEvents(items, total) {
  byId("event-total").textContent = `${number(total)} events stored`;
  replaceTable("recent-events", items.map((item) => {
    const row = document.createElement("tr"); const type = document.createElement("td"); const badge = document.createElement("span");
    badge.className = "event-type"; badge.textContent = item.event_type; type.append(badge);
    row.append(textCell(date(item.timestamp), "cell-muted"), type, textCell(eventDetails(item), "cell-sentence"), textCell(item.client_address || "local", "cell-muted")); return row;
  }), 4);
}

function renderRebuild(rebuild) {
  byId("rebuild-state").textContent = rebuild.state[0].toUpperCase() + rebuild.state.slice(1);
  byId("rebuild-target").textContent = rebuild.target_directory || "No replacement build has been started.";
  const log = byId("rebuild-log");
  log.hidden = !rebuild.log_tail.length; log.textContent = rebuild.log_tail.join("\n");
  document.querySelector('[data-action="rebuild-index"]').disabled = rebuild.state === "running";
}

async function loadSentences() {
  const response = await fetch(`/api/admin/sentences?offset=${state.sentenceOffset}&limit=${state.sentenceLimit}`);
  if (!response.ok) throw new Error("Master Array records could not be loaded");
  const data = await response.json();
  replaceTable("master-records", data.records.map((item) => {
    const row = document.createElement("tr"); row.append(
      textCell(number(item.sentence_id)), textCell(item.original_text), textCell(item.normalized_text, "cell-muted"),
      textCell(item.source_path), textCell(number(item.line_number)), textCell(number(item.usage_count)),
    ); return row;
  }), 6);
  const first = data.total ? data.offset + 1 : 0; const last = Math.min(data.offset + data.records.length, data.total);
  byId("sentences-page").textContent = `${number(first)}–${number(last)} of ${number(data.total)}`;
  byId("sentences-previous").disabled = data.offset === 0;
  byId("sentences-next").disabled = data.offset + data.records.length >= data.total;
}

async function loadDashboard(showStatus = false) {
  if (showStatus) byId("dashboard-status").textContent = "Refreshing complete system data…";
  try {
    const response = await fetch("/api/admin/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error("Dashboard data could not be loaded");
    const data = await response.json(); state.dashboard = data;
    renderMetrics(data); renderActivity(data.analytics.searches_by_hour); renderSystem(data);
    renderTopQueries(data.analytics.top_queries); renderTopSelections(data.analytics.top_selections);
    renderSources(data.corpus.sources); renderStorage(data.storage);
    renderRecentEvents(data.analytics.recent_events, data.analytics.event_count); renderRebuild(data.rebuild);
    byId("last-updated").textContent = date(data.generated_at);
    byId("dashboard-status").textContent = `Tracking ${number(data.analytics.event_count)} complete events from ${number(data.corpus.total_sentences)} sentence records.`;
  } catch (error) { byId("dashboard-status").textContent = error.message; }
}

function showToast(message) {
  const toast = byId("toast"); toast.textContent = message; toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 4200);
}

function openConfirmation(action) {
  const definition = actionDefinitions[action]; state.action = action;
  byId("dialog-title").textContent = definition.title; byId("dialog-description").textContent = definition.description;
  byId("confirmation-phrase").textContent = definition.phrase; byId("confirmation-input").value = ""; byId("dialog-error").textContent = "";
  byId("confirmation-dialog").showModal(); byId("confirmation-input").focus();
}

async function executeAction(event) {
  event.preventDefault(); const definition = actionDefinitions[state.action]; const entered = byId("confirmation-input").value;
  if (entered !== definition.phrase) { byId("dialog-error").textContent = `Enter ${definition.phrase} exactly.`; return; }
  byId("dialog-confirm").disabled = true;
  try {
    const response = await fetch(`/api/admin/actions/${state.action}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmation: definition.phrase }),
    });
    const payload = await response.json(); if (!response.ok) throw new Error(payload.error || "The action failed");
    byId("confirmation-dialog").close(); showToast(payload.message); await loadDashboard(true);
  } catch (error) { byId("dialog-error").textContent = error.message; }
  finally { byId("dialog-confirm").disabled = false; }
}

byId("refresh-button").addEventListener("click", () => Promise.all([loadDashboard(true), loadSentences()]));
byId("sentences-previous").addEventListener("click", () => { state.sentenceOffset = Math.max(0, state.sentenceOffset - state.sentenceLimit); loadSentences(); });
byId("sentences-next").addEventListener("click", () => { state.sentenceOffset += state.sentenceLimit; loadSentences(); });
document.querySelectorAll(".admin-action").forEach((button) => button.addEventListener("click", () => openConfirmation(button.dataset.action)));
byId("dialog-confirm").addEventListener("click", executeAction);
byId("confirmation-dialog").addEventListener("close", () => { state.action = null; });

Promise.all([loadDashboard(), loadSentences()]);
fetch("/api/events", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ event_type: "page_view", details: { page: "admin" } }),
  keepalive: true,
}).catch(() => {});
window.setInterval(() => loadDashboard(), 10000);
